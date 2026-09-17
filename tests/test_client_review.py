"""Safety contracts for the ephemeral, readonly review execution boundary."""
from __future__ import annotations

import copy
import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import client_engine
import client_events
import client_mcp
import client_policy
import client_prompt
import client_review
import client_store
import client_turns
import config
import context_budget
import llama_client
import review_core
import review_source
from mcp_contract import PUBLIC_TOOL_ORDER

pytestmark = pytest.mark.smoke


class _Mcp:
    def __init__(self, root=None, **kwargs):
        self.root = root
        self.kwargs = kwargs
        self.closed = False
        self.started = False
        self.calls = []
        self.aborted = False
        self.on_notice = None
        self.specs = tuple(client_mcp.ToolSpec(
            name, name, {"type": "object", "properties": {}},
            name in client_review.REVIEW_TOOLS or name in {"git_diff", "git_status"},
        ) for name in PUBLIC_TOOL_ORDER)

    def tools(self):
        return self.specs

    def start(self):
        if self.aborted:
            raise client_mcp.McpCallCancelledError("startup cancelled")
        self.started = True

    def abort_start(self):
        if not self.started:
            self.aborted = True

    def close(self):
        self.closed = True

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return client_mcp.ToolCallResult(name, "background evidence", None, False)


def _chunk(text="", *, tool=None):
    delta = {"content": text}
    if tool:
        delta = {"tool_calls": [{"index": 0, "id": "call_review", "type": "function",
                                 "function": {"name": tool, "arguments": "{}"}}]}
    return {"choices": [{"delta": delta, "finish_reason": "tool_calls" if tool else "stop"}]}


@pytest.fixture
def review_setup(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    original = _Mcp(root)
    main = client_engine.Engine(
        client_engine.EngineOptions(root, "local-model", "http://127.0.0.1:65535", 65536,
                                    max_output_tokens=2048),
        mcp=original, store=client_store.EphemeralSessionStore(root),
        system_prompt=client_prompt.SystemPrompt("interactive system"), env={},
    )
    main.messages = [{"role": "user", "content": "PRIVATE CHAT"}]
    files = tuple(review_source.ReviewFile(
        path, "old\n", "new\n", "old-hash", "new-hash", "-old\n+new\n",
        frozenset({1}), frozenset({1}), "modified",
    ) for path in ("one.c", "two.c"))
    snapshot = review_source.ReviewSnapshot(str(root), "abc123", "snapshot-digest", files, (), ())
    clients, counts, requests = [], [], []

    def client_factory(*args, **kwargs):
        client = _Mcp(*args, **kwargs)
        clients.append(client)
        return client

    def count(**kwargs):
        counts.append(kwargs)
        return 100

    def model(**kwargs):
        requests.append(kwargs)
        return iter([_chunk('{"findings": []}')])

    monkeypatch.setattr(client_mcp, "McpClient", client_factory)
    monkeypatch.setattr(llama_client, "count_chat_tokens", count)
    monkeypatch.setattr(llama_client, "chat_completions", model)
    monkeypatch.setattr(review_source, "collect_workspace", lambda *a, **kw: snapshot)
    monkeypatch.setattr(review_source, "verify_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(review_core, "build_review_prompt", lambda snap, file: file.path + "\n" + file.new_text)
    monkeypatch.setattr(review_core, "validate_review_response",
                        lambda file, text: review_core.FileReview(file.path, "reviewed"))
    monkeypatch.setattr(context_budget, "log_metrics", lambda *a, **kw: pytest.fail("review wrote metrics"))
    return main, snapshot, clients, counts, requests


def _run_job(main):
    job = client_review.ReviewJob(main)
    progress = []
    return job.finish(job.run(progress.append)), progress


def test_review_isolated_mcp_ephemeral_history_shared_lock_and_exact_gate(review_setup, monkeypatch):
    main, snapshot, clients, counts, requests = review_setup
    before = copy.deepcopy(main.messages), copy.deepcopy(main.store.read(main.session_id)), main.session_id
    runtime = (config.CTX_METRICS_ENABLED, config.RUN_COMMAND_ENABLED, config.PATCH_ENABLED)
    real_engine = client_engine.Engine
    engines, usages = [], []
    original_gate = context_budget.enforce_gate

    def engine_factory(*args, **kwargs):
        engine = real_engine(*args, **kwargs)
        engines.append(engine)
        return engine

    monkeypatch.setattr(client_engine, "Engine", engine_factory)
    monkeypatch.setattr(context_budget, "enforce_gate", lambda usage: (usages.append(usage), original_gate(usage))[1])
    outcome, progress = _run_job(main)
    assert outcome.reason == "stop" and all(result.status == "reviewed" for result in outcome.results)
    assert len(clients) == 1 and clients[0].closed and clients[0].kwargs["readonly"] is True
    assert clients[0].kwargs["build_commands"] is False and clients[0].kwargs["restart_on_cancel"] is False
    assert clients[0].kwargs["n_ctx"] == main.options.n_ctx
    assert not main.mcp.closed and not main.mcp.calls
    assert (main.messages, main.store.read(main.session_id), main.session_id) == before
    assert (config.CTX_METRICS_ENABLED, config.RUN_COMMAND_ENABLED, config.PATCH_ENABLED) == runtime
    assert len(engines) == len(snapshot.files)
    assert all(engine.model_lock is main.model_lock and isinstance(engine.store, client_store.EphemeralSessionStore)
               and engine.options.policy.name == "readonly" for engine in engines)
    assert len({id(engine.store) for engine in engines}) == len(engines)
    for count, request, usage, file in zip(counts, requests, usages, snapshot.files, strict=True):
        assert len(request["messages"]) == 2 and file.path in request["messages"][1]["content"]
        assert "PRIVATE CHAT" not in str(request)
        assert count["messages"] == request["messages"] and count["tools"] == request["tools"]
        assert count["extra"] == request["extra"]
        assert request["extra"]["max_tokens"] == usage.reserved_output_tokens == 2048
        assert usage.estimated_input_tokens == 100 and usage.count_method == "llama_cpp_chat"
        assert {tool["function"]["name"] for tool in request["tools"]} == client_review.REVIEW_TOOLS
    assert all("findings" not in update for update in progress)


@pytest.mark.parametrize("hint", [False, "false", "true", 1, None])
def test_review_allowlist_and_json_true_guard_schema_and_dispatch(review_setup, hint):
    main, *_ = review_setup
    mcp = _Mcp()
    mcp.specs = tuple(replace(spec, read_only=hint) if spec.name == "read_file" else spec for spec in mcp.specs)
    engine = client_engine.Engine(
        replace(main.options, policy=client_policy.ReadOnlyPolicy(), tool_allowlist=client_review.REVIEW_TOOLS),
        mcp=mcp, store=client_store.EphemeralSessionStore(main.options.root),
        system_prompt=client_prompt.SystemPrompt("review"), env={},
    )
    engine.load_tools()
    assert set(engine.tool_specs) == client_review.REVIEW_TOOLS - {"read_file"}
    all_specs = {spec.name: spec for spec in mcp.specs}
    for name in ("read_file", "git_diff", "git_status", "apply_patch", "run_command", "code_rag_search"):
        result = engine._run_one_tool({"id": name, "name": name, "arguments": {}}, specs=all_specs,
                                      approve=lambda req: pytest.fail("review asked for approval"), denied_counts={})
        assert result["status"] == "denied"
    assert mcp.calls == []
    mcp.specs = mcp.specs[:-1]
    with pytest.raises(client_mcp.McpClientError, match="PUBLIC_TOOL_ORDER"):
        engine.load_tools()


def test_review_overflow_refuses_model_request_without_metrics_or_truncation(review_setup, monkeypatch):
    main, snapshot, clients, counts, requests = review_setup
    monkeypatch.setattr(llama_client, "count_chat_tokens", lambda **kw: 65536)
    outcome, _ = _run_job(main)
    assert outcome.reason == "error" and outcome.state == "incomplete"
    assert len(outcome.results) == len(snapshot.files)
    assert all(result.status == "failed" and "ContextOverflowError" in result.reason for result in outcome.results)
    assert not requests and clients[0].closed and not main.mcp.closed


@pytest.mark.parametrize("failure", ["invalid", "tool"])
def test_review_invalid_or_tool_failed_draft_is_never_published(review_setup, monkeypatch, failure):
    main, snapshot, clients, counts, requests = review_setup
    drafts = []
    if failure == "invalid":
        def reject(file, text):
            drafts.append(text)
            raise review_core.ReviewResponseError("invalid structured response")
        monkeypatch.setattr(review_core, "validate_review_response", reject)
        monkeypatch.setattr(llama_client, "chat_completions", lambda **kw: iter([_chunk("UNVALIDATED_DRAFT")]))
    else:
        calls = []
        def respond(**kw):
            calls.append(kw)
            return iter([_chunk(tool="git_diff") if len(calls) % 2 else _chunk('{"findings": []}')])
        monkeypatch.setattr(llama_client, "chat_completions", respond)
        monkeypatch.setattr(review_core, "validate_review_response", lambda *a: pytest.fail("tool failure became clean"))
    outcome, progress = _run_job(main)
    assert outcome.reason == "error" and all(result.status == "failed" for result in outcome.results)
    assert "UNVALIDATED_DRAFT" not in str(progress) + outcome.render()
    assert not clients[0].calls and not main.mcp.closed


@pytest.mark.parametrize("phase", ["before_worker", "collect", "before_spawn", "before_send", "between_files", "verify", "publish"])
def test_review_cancellation_covers_worker_source_file_and_publish_gaps(review_setup, monkeypatch, phase):
    main, snapshot, clients, counts, requests = review_setup
    job = client_review.ReviewJob(main)
    events, workers = [], []
    coordinator = client_turns.TurnCoordinator(main, emit=events.append)
    monkeypatch.setattr(coordinator, "_spawn", lambda body, name: workers.append(body))
    monkeypatch.setattr(main, "prime_prompt_cache", lambda **kw: pytest.fail("review primed"))
    real_run = job.run

    def run(progress):
        def at_progress(message):
            if phase == "between_files" and "已處理 1/" in message:
                assert coordinator.cancel()
            progress(message)
        outcome = real_run(at_progress)
        if phase == "publish":
            assert coordinator.cancel()
        return outcome

    monkeypatch.setattr(job, "run", run)
    if phase == "collect":
        def collect(*a, **kw):
            assert coordinator.cancel()
            assert kw["cancelled"]()
            raise review_source.ReviewCancelled("during collection")
        monkeypatch.setattr(review_source, "collect_workspace", collect)
    if phase == "before_spawn":
        original_register = job._register_client
        def register(client):
            assert coordinator.cancel()
            original_register(client)
        monkeypatch.setattr(job, "_register_client", register)
    if phase == "before_send":
        original_register = job._register_engine
        def register(engine):
            original_register(engine)
            assert coordinator.cancel()
        monkeypatch.setattr(job, "_register_engine", register)
    if phase == "verify":
        def verify(*a, **kw):
            assert coordinator.cancel()
            raise review_source.ReviewCancelled("during verification")
        monkeypatch.setattr(review_source, "verify_snapshot", verify)
    review_id = coordinator.start_review(job)
    assert coordinator.busy and coordinator.reviewing
    with pytest.raises(client_turns.TurnCoordinator.Busy):
        coordinator.start_turn("new chat")
    with pytest.raises(client_turns.TurnCoordinator.Busy):
        coordinator.start_compaction()
    with pytest.raises(client_turns.TurnCoordinator.Busy):
        coordinator.assert_session_change_allowed()
    with pytest.raises(client_turns.QueueError):
        coordinator.enqueue("supplement", mode="supplement")
    queued = coordinator.enqueue("retain chat input")
    if phase == "before_worker":
        assert coordinator.cancel()
    workers.pop(0)()
    terminal = [event for event in events if client_events.is_terminal_event(event)]
    assert len(terminal) == 1 and client_events.event_part(terminal[0])["reason"] == "cancelled"
    assert not coordinator.busy and not coordinator.reviewing and not coordinator.cancel()
    assert not workers and coordinator.queue_paused
    assert coordinator.queue_snapshot(pending_only=True)[0].id == queued.id
    assert main.messages == [{"role": "user", "content": "PRIVATE CHAT"}] and not main.mcp.closed
    assert all(client.closed for client in clients)
    assert len(requests) == (2 if phase in ("verify", "publish") else 1 if phase == "between_files" else 0)
    coordinator.cancel_queued(queued.id)
    coordinator.begin_turn()
    assert not coordinator.cancel(review_id=review_id), "late review close cancelled the next chat"
    coordinator.finish_turn()


@pytest.mark.parametrize("waiting", ["stream", "tool"])
def test_review_cancels_stream_and_pending_tool_without_next_file(review_setup, monkeypatch, waiting):
    main, snapshot, clients, counts, requests = review_setup
    entered, released, finished = threading.Event(), threading.Event(), threading.Event()
    cancellations, model_requests, events = [], [], []

    class Stream:
        def __iter__(self):
            entered.set()
            assert released.wait(3)
            return iter([_chunk("discarded draft")])
        def close(self):
            released.set()

    class Pending:
        def result(self):
            entered.set()
            assert released.wait(3)
            raise client_mcp.McpCallCancelledError("tool cancelled")
        def cancel(self, reason):
            cancellations.append(reason)
            released.set()

    if waiting == "tool":
        monkeypatch.setattr(_Mcp, "begin_call", lambda self, name, arguments: Pending(), raising=False)
    def respond(**kwargs):
        model_requests.append(kwargs)
        return Stream() if waiting == "stream" else iter([_chunk(tool="read_file")])
    monkeypatch.setattr(llama_client, "chat_completions", respond)
    coordinator = client_turns.TurnCoordinator(main, emit=lambda event: (
        events.append(event), finished.set() if client_events.is_terminal_event(event) else None,
    ))
    coordinator.start_review(client_review.ReviewJob(main))
    try:
        assert entered.wait(3)
        assert coordinator.cancel(block=False)
        assert finished.wait(3)
    finally:
        released.set()
    terminal = [event for event in events if client_events.is_terminal_event(event)]
    assert len(terminal) == 1 and client_events.event_part(terminal[0])["reason"] == "cancelled"
    assert len(model_requests) == 1 and not coordinator.busy and not main.model_lock.locked()
    assert clients[0].closed and not main.mcp.closed
    if waiting == "tool":
        assert cancellations


def test_review_queue_rejects_start_and_stale_snapshot_suppresses_findings(review_setup, monkeypatch):
    main, snapshot, clients, counts, requests = review_setup
    coordinator = client_turns.TurnCoordinator(main, emit=lambda event: None)
    queued = coordinator.enqueue("pending original chat")
    with pytest.raises(client_turns.QueueError):
        coordinator.start_review(client_review.ReviewJob(main))
    assert coordinator.queue_snapshot(pending_only=True) == (queued,) and not clients
    coordinator.cancel_queued(queued.id)
    finding = review_core.Finding("one.c", "new", 1, 1, "high", "old finding", "impact", "new")
    monkeypatch.setattr(review_core, "validate_review_response", lambda file, text: review_core.FileReview(file.path, "reviewed", (finding,)))
    def stale(*a, **kw):
        raise review_source.ReviewSourceError("changed after model response")
    monkeypatch.setattr(review_source, "verify_snapshot", stale)
    outcome, _ = _run_job(main)
    assert outcome.state == "stale" and outcome.reason == "error"
    assert all(result.status == "skipped" and not result.findings for result in outcome.results)


@pytest.mark.parametrize("phase", ["headers", "late_connect"])
def test_review_cancels_headers_and_late_connect_before_releasing_model_slot(review_setup, monkeypatch, phase):
    main, snapshot, clients, counts, requests = review_setup
    entered, release, settled, terminal = (threading.Event() for _ in range(4))
    shutdown, sent, events = [], [], []

    class Socket:
        def shutdown(self, how):
            shutdown.append(main.model_lock.locked())
            release.set()
        def close(self):
            pass

    sock = Socket()

    def model(**kwargs):
        cancellation = kwargs["cancel"]
        try:
            if phase == "headers":
                cancellation.register(sock)
                sent.append("request bytes")
            entered.set()
            assert release.wait(3)
            if phase == "late_connect":
                cancellation.register(sock)
                sent.append("late request bytes")
            cancellation.check()
            return iter([_chunk("unpublished response")])
        finally:
            settled.set()

    monkeypatch.setattr(llama_client, "chat_completions", model)
    def emit(event):
        events.append(event)
        if client_events.is_terminal_event(event):
            terminal.set()
    coordinator = client_turns.TurnCoordinator(main, emit=emit)
    coordinator.start_review(client_review.ReviewJob(main))
    try:
        assert entered.wait(3)
        started = time.monotonic()
        assert coordinator.cancel(block=False)
        assert terminal.wait(0.8)
        assert time.monotonic() - started < 1
        assert not main.model_lock.locked() and not coordinator.busy
        if phase == "headers":
            assert shutdown == [True], "model slot was released before socket shutdown"
        else:
            assert not sent
            release.set()
        assert settled.wait(1)
        assert len(sent) == (1 if phase == "headers" else 0)
        assert shutdown
        assert clients[0].closed and not main.mcp.closed
        finals = [event for event in events if client_events.is_terminal_event(event)]
        assert len(finals) == 1 and client_events.event_part(finals[0])["reason"] == "cancelled"
    finally:
        release.set()
