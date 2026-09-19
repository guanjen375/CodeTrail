"""Request activity stays truthful, transient, and outside cancellation locks."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import client_engine  # noqa: E402
import client_events  # noqa: E402
import client_mcp  # noqa: E402
import client_policy  # noqa: E402
import client_prompt  # noqa: E402
import client_store  # noqa: E402
import config  # noqa: E402
import context_budget  # noqa: E402
import llama_client  # noqa: E402
from mcp_contract import PUBLIC_TOOL_ORDER  # noqa: E402

pytestmark = pytest.mark.smoke


class ActivityMcp:
    def __init__(self):
        self.calls = []

    def tools(self):
        return tuple(
            client_mcp.ToolSpec(
                name=name, description=name,
                input_schema={"type": "object", "properties": {}},
                read_only=name != "apply_patch",
            )
            for name in PUBLIC_TOOL_ORDER
        )

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return client_mcp.ToolCallResult(name, "status: ok\nsource", None, False)


@pytest.fixture()
def activity_engine(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(context_budget, "log_metrics", lambda *_args, **_kwargs: None)
    # This fixture's model is offline; keep its synthetic budget scale while
    # exercising the real engine's activity, gate and cancellation boundaries.
    monkeypatch.setattr(
        llama_client, "count_chat_tokens",
        lambda **kwargs: context_budget.estimate_tokens(
            messages=kwargs["messages"], tools=kwargs.get("tools"),
        )[0],
    )
    engine = client_engine.Engine(
        client_engine.EngineOptions(
            root=root, model="activity-model", base_url="http://127.0.0.1:65535",
            n_ctx=131072, policy=client_policy.InteractivePolicy(),
        ),
        mcp=ActivityMcp(), store=client_store.EphemeralSessionStore(root),
        system_prompt=client_prompt.SystemPrompt(text="SYSTEM"), env={},
    )
    engine.load_tools()
    return engine


def _bind(engine, callback):
    # Let the regression reach the real missing behavior before the setter exists.
    setter = getattr(engine, "set_activity_callback", None)
    if callable(setter):
        setter(callback)
    else:
        engine._activity_callback = callback


def _progress(processed=920, total=1000, **extra):
    return {"prompt_progress": {"processed": processed, "total": total, **extra}}


def _delta(delta, finish=None):
    return {"choices": [{"delta": delta, "finish_reason": finish}]}


def _tool(name="read_file"):
    return _delta({"tool_calls": [{
        "index": 0, "id": "activity_call", "type": "function",
        "function": {"name": name, "arguments": '{"path":"source.c"}'},
    }]}, "tool_calls")


def test_activity_progress_is_private_detached_and_backward_compatible():
    raw = {
        "processed": 8400.0, "total": 20000.0, "cache": 2000.0, "time_ms": 30000.0,
        "prompt": "private prompt", "slots": [{"id": 3, "prompt": "other request"}],
        "content": "private answer",
    }
    expected = {"processed": 8400, "total": 20000, "cache": 2000, "time_ms": 30000}
    snapshot = client_events.prompt_progress_snapshot(raw)
    event = client_events.activity_event(
        "session", operation="response", phase="prompt_processing", percent=42, progress=raw,
    )
    assert snapshot == expected and all(type(value) is int for value in snapshot.values())
    assert event == {
        "type": "activity", "sessionID": "session",
        "part": {"operation": "response", "phase": "prompt_processing", "percent": 42, "progress": expected},
    }
    raw["processed"] = 1
    snapshot["cache"] = 1
    assert event["part"]["progress"] == expected, "Neither source nor consumer may change another snapshot"
    assert "private" not in client_events.dumps(event) and "slots" not in client_events.dumps(event)
    assert client_events.activity_event(
        "session", operation="response", phase="tool", percent=50, tool="read_file",
    )["part"] == {"operation": "response", "phase": "tool", "percent": 50, "tool": "read_file"}


def test_optional_progress_metrics_never_invent_cache_or_time(activity_engine):
    activity = []
    _bind(activity_engine, activity.append)
    chunks = [
        _progress(800, cache=800, time_ms=7),
        _progress(time_ms=1000), _progress(cache=800), _progress(cache=800, time_ms=1200),
        *(_progress(cache=800, time_ms=value) for value in (
            None, True, -1, 1.5, float("nan"), float("inf"), 2**53, 10**400,
        )),
    ]
    assert list(activity_engine._activity_chunks(iter(chunks), operation="response")) == chunks
    assert [event["part"]["progress"] for event in activity] == [
        {"processed": 800, "total": 1000, "cache": 800, "time_ms": 7},
        {"processed": 920, "total": 1000, "time_ms": 1000},
        {"processed": 920, "total": 1000, "cache": 800},
        {"processed": 920, "total": 1000, "cache": 800, "time_ms": 1200},
        {"processed": 920, "total": 1000, "cache": 800},
    ]
    assert [event["part"]["percent"] for event in activity] == [80, 92, 92, 92, 92]


def test_progress_snapshots_do_not_cross_requests_or_sessions(activity_engine):
    engine = activity_engine
    activity = []
    _bind(engine, activity.append)
    original_session = engine.session_id
    stored = copy.deepcopy(engine.store.read(original_session))
    original = engine.load_session(original_session)

    def request(operation):
        chunks = [_progress(200, cache=100, time_ms=500), _delta({}, "stop")]
        assert list(engine._activity_chunks(iter(chunks), operation=operation)) == chunks

    request("response")
    request("compact")
    new_session = engine.new_session()
    request("response")
    engine.adopt(original)
    request("response")
    assert new_session != original_session
    assert [event["sessionID"] for event in activity] == [
        original_session, original_session, new_session, original_session,
    ]
    assert [event["part"]["operation"] for event in activity] == ["response", "compact", "response", "response"]
    assert all(event["part"]["progress"] == {
        "processed": 200, "total": 1000, "cache": 100, "time_ms": 500,
    } for event in activity)
    assert engine.messages == [] and engine.store.read(original_session) == stored
    assert not any(record.get("type") == "activity" for record in engine.store.read(new_session))


def test_request_prompt_progress_reaches_activity_callback(activity_engine, monkeypatch):
    engine = activity_engine
    activity, events, requests, text = [], [], [], []
    _bind(engine, activity.append)
    streams = iter([
        [
            _progress(cache=800, time_ms=1000), _progress(929, cache=800, time_ms=1100),
            _progress(929, cache=800, time_ms=1200), _progress(929, cache=800, time_ms=1200),
            _tool(), _progress(1000),
        ],
        [_progress(cache=800), _delta({"content": "answer"}, "stop")],
        [_progress(cache=800), _delta({"content": "private summary"}, "stop")],
    ])

    def request(**kwargs):
        requests.append(kwargs)
        return iter(next(streams))

    monkeypatch.setattr(llama_client, "chat_completions", request)
    result = engine.send("question", on_event=events.append, on_text=text.append)
    history = copy.deepcopy(engine.messages)
    stored = copy.deepcopy(engine.store.read(engine.session_id))
    completion = engine.complete([{"role": "user", "content": "summarize"}], source="compaction")

    parts = [event["part"] for event in activity]
    assert [part for part in parts if part["phase"] == "prompt_processing"] == [
        {"operation": "response", "phase": "prompt_processing", "percent": 92,
         "progress": {"processed": 920, "total": 1000, "cache": 800, "time_ms": 1000}},
        {"operation": "response", "phase": "prompt_processing", "percent": 92,
         "progress": {"processed": 929, "total": 1000, "cache": 800, "time_ms": 1100}},
        {"operation": "response", "phase": "prompt_processing", "percent": 92,
         "progress": {"processed": 929, "total": 1000, "cache": 800, "time_ms": 1200}},
        {"operation": "response", "phase": "prompt_processing", "percent": 92,
         "progress": {"processed": 920, "total": 1000, "cache": 800}},
        {"operation": "compact", "phase": "prompt_processing", "percent": 92,
         "progress": {"processed": 920, "total": 1000, "cache": 800}},
    ], "Every changed SSE snapshot must reach the UI; processed already includes cache"
    request_phases = ["preparing", "waiting_model", "waiting_response", "prompt_processing", "generating"]
    assert [part["phase"] for part in parts] == (
        request_phases[:3] + ["prompt_processing"] * 3 + ["generating", "tool"] + request_phases * 2
    )
    assert next(part for part in parts if part["phase"] == "tool")["tool"] == "read_file"
    assert all(event["type"] == "activity" and event["sessionID"] == engine.session_id for event in activity)
    assert all(request["extra"]["return_progress"] is True for request in requests)
    assert result.text == "answer" and text == ["answer"]
    assert completion.text == "private summary"
    assert engine.messages == history and engine.store.read(engine.session_id) == stored
    assert all(event["type"] != "activity" for event in events)
    assert "private summary" not in repr(events) + repr(activity)


def test_bad_progress_cannot_become_output_or_stall_the_response(activity_engine, monkeypatch):
    engine = activity_engine
    activity, events, text, reasoning = [], [], [], []
    _bind(engine, activity.append)
    invalid = [
        None, [], "92%", {}, {"total": 1000}, {"processed": 920},
        {"processed": True, "total": 1000}, {"processed": 0, "total": False},
        {"processed": "920", "total": 1000}, {"processed": 920, "total": "1000"},
        {"processed": float("nan"), "total": 1000},
        {"processed": 1, "total": float("inf")},
        {"processed": float("inf"), "total": 1000},
        {"processed": 0, "total": float("nan")},
        {"processed": -1, "total": 1000}, {"processed": 0, "total": -1},
        {"processed": 1001, "total": 1000}, {"processed": 0, "total": 0},
        {"processed": 920.5, "total": 1000}, {"processed": 920, "total": 1000.5},
        {"processed": 920, "total": 1000, "cache": 921},
        {"processed": 920, "total": 1000, "cache": True},
        {"processed": 920, "total": 1000, "cache": -1},
        {"processed": 920, "total": 1000, "cache": "800"},
        {"processed": 920, "total": 1000, "cache": 800.5},
    ]
    chunks = [
        _progress(0), *({"prompt_progress": value} for value in invalid),
        _progress(29, 100), _progress(3.0, 4.0), _progress(920, cache=800),
        _progress(2**53 - 1, 2**53 - 1), _progress(2**53, 2**53),
        _progress(1e308, 1e308), _progress(10**400, 10**400),
        _delta({"role": "assistant", "content": "", "reasoning_content": "", "tool_calls": []}),
        _delta({"reasoning_content": "thought"}), _progress(0),
        _delta({"content": "answer"}, "stop"),
    ]
    monkeypatch.setattr(llama_client, "chat_completions", lambda **_kwargs: iter(chunks))
    result = engine.send("question", on_event=events.append, on_text=text.append, on_reasoning=reasoning.append)
    parts = [event["part"] for event in activity]
    progress_parts = [part for part in parts if part["phase"] == "prompt_processing"]
    assert [part.get("percent") for part in progress_parts] == [
        0, None, 29, 75, 92, 100, None,
    ]
    assert [part.get("progress") for part in progress_parts] == [
        {"processed": 0, "total": 1000}, None,
        {"processed": 29, "total": 100}, {"processed": 3, "total": 4},
        {"processed": 920, "total": 1000, "cache": 800},
        {"processed": 2**53 - 1, "total": 2**53 - 1}, None,
    ]
    assert all("percent" not in part and "progress" not in part for part in progress_parts if part.get("percent") is None)
    assert parts[-1] == {"operation": "response", "phase": "generating"}
    assert sum(part["phase"] == "generating" for part in parts) == 1
    assert result.text == "answer" and result.finish == "stop"
    assert text == ["answer"] and reasoning == ["thought"]
    assert all(event["type"] != "activity" for event in events)
    assert "prompt_progress" not in repr(engine.store.read(engine.session_id))


@pytest.mark.parametrize("operation", ["response", "compact"])
def test_activity_callback_failure_preserves_gate_payload_and_history(activity_engine, monkeypatch, operation):
    engine = activity_engine
    engine.options.max_output_tokens = 137
    engine.messages = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer", "reasoning_content": "old thought"},
    ]
    before = copy.deepcopy(engine.messages)
    stored = copy.deepcopy(engine.store.read(engine.session_id))
    gates, requests, activity, events = [], [], [], []
    gate = context_budget.check_and_log

    def check(**kwargs):
        gates.append(copy.deepcopy(kwargs))
        return gate(**kwargs)

    def report(event):
        activity.append(event)
        raise RuntimeError("UI unavailable")

    def request(**kwargs):
        requests.append(copy.deepcopy(kwargs))
        return iter([
            _progress(cache=800), _delta({"reasoning_content": "private thought"}),
            _delta({"content": "private answer"}, "stop"),
        ])

    _bind(engine, report)
    monkeypatch.setattr(context_budget, "check_and_log", check)
    monkeypatch.setattr(llama_client, "chat_completions", request)
    if operation == "compact":
        payload = [{"role": "user", "content": "private summary request"}]
        result = engine.complete(payload, source="compaction")
        assert engine.messages == before and engine.store.read(engine.session_id) == stored
        assert requests[0]["messages"] == payload
        assert requests[0].get("tools") is None
    else:
        result = engine.send("question", on_event=events.append)
        assert engine.messages[:2] == before
        assert "reasoning_content" not in requests[0]["messages"][2]
        assert gates[0]["tools"] == requests[0]["tools"] == engine.openai_tools()
    assert result.text == "private answer" and result.finish == "stop"
    assert gates[0]["messages"] == requests[0]["messages"]
    assert gates[0]["reserved_output_tokens"] == requests[0]["extra"]["max_tokens"] == 137
    assert gates[0]["requested_num_ctx"] == engine.options.n_ctx
    assert requests[0]["extra"] == {
        "max_tokens": 137, "return_progress": True,
        "chat_template_kwargs": {"enable_thinking": False, "thinking": False},
    }
    assert [event["part"]["phase"] for event in activity] == [
        "preparing", "waiting_model", "waiting_response", "prompt_processing", "generating",
    ]
    assert all(event["part"]["operation"] == operation for event in activity)
    assert "private" not in repr(activity) and all(event["type"] != "activity" for event in events)


class ActivityStream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.read = 0
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        chunk = next(self.chunks)
        self.read += 1
        return chunk

    def close(self):
        self.closed = True


@pytest.mark.parametrize("operation", ["response", "compact"])
@pytest.mark.parametrize("delta,finish", [
    ({"content": "answer"}, None),
    ({"reasoning_content": "thought"}, None),
    ({"tool_calls": [{"index": 0}]}, None),
    ({"content": "", "reasoning_content": "", "tool_calls": []}, "stop"),
], ids=["content", "reasoning", "tool_calls", "finish_without_output"])
def test_generation_and_finish_close_request_progress_observer(activity_engine, operation, delta, finish):
    activity = []
    _bind(activity_engine, activity.append)
    closing = {**_delta(delta, finish), **_progress(999)}
    chunks = [_progress(200), closing, _progress(1000), _progress(0)]
    stream = ActivityStream(chunks)
    observed = activity_engine._activity_chunks(stream, operation=operation)
    for index, chunk in enumerate(chunks):
        assert next(observed) is chunk, "Activity must not rewrite or buffer model output"
        assert stream.read == index + 1
        expected = [{
            "operation": operation, "phase": "prompt_processing", "percent": 20,
            "progress": {"processed": 200, "total": 1000},
        }]
        if index > 0 and finish is None:
            expected.append({"operation": operation, "phase": "generating"})
        assert [event["part"] for event in activity] == expected
    with pytest.raises(StopIteration):
        next(observed)


def test_compaction_activity_is_advisory_and_phase_limited(activity_engine):
    engine = activity_engine
    before = copy.deepcopy(engine.messages)
    stored = copy.deepcopy(engine.store.read(engine.session_id))
    activity = []

    def report(event):
        activity.append(event)
        raise RuntimeError("UI unavailable")

    _bind(engine, report)
    for phase in ("validating", "persisting"):
        engine.report_compaction_activity(phase)
    with pytest.raises(ValueError, match="unsupported compaction activity phase"):
        engine.report_compaction_activity("generating")
    assert [event["part"] for event in activity] == [
        {"operation": "compact", "phase": "validating"},
        {"operation": "compact", "phase": "persisting"},
    ]
    _bind(engine, None)
    for phase in ("validating", "persisting"):
        engine.report_compaction_activity(phase)
    assert engine.messages == before and engine.store.read(engine.session_id) == stored
    assert engine._in_turn == 0 and not engine._cancel.is_set()


@pytest.mark.parametrize("phase,committed", [("validating", False), ("persisting", True)])
def test_compaction_activity_preserves_cancel_commit_boundary(activity_engine, phase, committed):
    engine = activity_engine
    activity, accepted, locks = [], [], []
    before = copy.deepcopy(engine.messages)
    stored = copy.deepcopy(engine.store.read(engine.session_id))

    def report(event):
        activity.append(event)
        for lock in (engine._turn_state, engine._active_lock):
            acquired = lock.acquire(blocking=False)
            locks.append(acquired)
            if not acquired:
                return
            lock.release()
        accepted.append(engine.cancel())
        raise RuntimeError("UI closed during cancellation")

    _bind(engine, report)
    with engine.turn_scope():
        if committed:
            engine.commit_point()
            engine.report_compaction_activity(phase)
        else:
            with pytest.raises(client_engine.TurnCancelled):
                engine.report_compaction_activity(phase)
            # Already accepted cancellation must also prevent another callback.
            with pytest.raises(client_engine.TurnCancelled):
                engine.report_compaction_activity(phase)
    assert locks == [True, True] and accepted == [not committed]
    assert [event["part"] for event in activity] == [{"operation": "compact", "phase": phase}]
    assert engine.messages == before and engine.store.read(engine.session_id) == stored
    assert engine._in_turn == 0 and not engine._cancel.is_set()


@pytest.mark.parametrize("phase", ["preparing", "validating", "persisting"])
def test_activity_callback_cannot_swallow_turn_cancelled(activity_engine, phase):
    cancellation = client_engine.TurnCancelled("cancelled by observer")

    def report(_event):
        raise cancellation

    _bind(activity_engine, report)
    with activity_engine.turn_scope():
        with pytest.raises(client_engine.TurnCancelled) as caught:
            if phase == "preparing":
                activity_engine._report_activity("response", phase)
            else:
                activity_engine.report_compaction_activity(phase)
    assert caught.value is cancellation
    assert activity_engine._in_turn == 0 and not activity_engine._cancel.is_set()


@pytest.mark.parametrize("operation", ["response", "compact"])
@pytest.mark.parametrize("phase", ["preparing", "waiting_model", "waiting_response", "prompt_processing", "generating"])
def test_activity_callback_cancellation_prevents_request_or_more_output(activity_engine, monkeypatch, operation, phase):
    engine = activity_engine
    activity, requests, text, reasoning, accepted, locks = [], [], [], [], [], []
    stream = ActivityStream([
        _progress(), _delta({"content": "unfinished answer", "reasoning_content": "unfinished thought"}),
        _delta({"content": "must not read"}, "stop"),
    ])

    def report(event):
        activity.append(event)
        if event["part"]["phase"] != phase:
            return
        for lock in (engine._turn_state, engine._active_lock):
            acquired = lock.acquire(blocking=False)
            locks.append(acquired)
            if not acquired:
                return
            lock.release()
        accepted.append(engine.cancel())
        raise RuntimeError("UI closed during cancellation")

    def request(**kwargs):
        requests.append(kwargs)
        return stream

    _bind(engine, report)
    monkeypatch.setattr(llama_client, "chat_completions", request)
    with pytest.raises(client_engine.TurnCancelled):
        if operation == "compact":
            engine.complete([{"role": "user", "content": "summarize"}], source="compaction")
        else:
            engine.send("question", on_text=text.append, on_reasoning=reasoning.append)
    assert locks == [True, True] and accepted == [True]
    assert activity[-1]["part"]["phase"] == phase
    assert all(event["part"]["operation"] == operation for event in activity)
    assert text == [] and reasoning == []
    assert not any(message["role"] == "assistant" for message in engine.messages)
    if phase in ("preparing", "waiting_model", "waiting_response"):
        assert requests == [] and stream.read == 0
    else:
        assert len(requests) == 1 and stream.closed
        assert stream.read == (1 if phase == "prompt_processing" else 2)
    assert engine._in_turn == 0 and not engine._cancel.is_set()
    assert engine.model_lock.acquire(blocking=False)
    engine.model_lock.release()


@pytest.mark.parametrize("phase", ["approval", "tool"])
def test_tool_activity_cancellation_prevents_approval_and_dispatch(activity_engine, monkeypatch, phase):
    engine = activity_engine
    activity, approvals, requests, accepted = [], [], [], []

    def report(event):
        activity.append(event)
        if event["part"]["phase"] == phase:
            accepted.append(engine.cancel())

    def approve(request):
        approvals.append(request)
        return True

    def request(**kwargs):
        requests.append(kwargs)
        return iter([_tool("apply_patch")])

    _bind(engine, report)
    monkeypatch.setattr(llama_client, "chat_completions", request)
    with pytest.raises(client_engine.TurnCancelled):
        engine.send("question", approve=approve)
    assert accepted == [True] and len(requests) == 1
    assert activity[-1]["part"] == {"operation": "response", "phase": phase, "tool": "apply_patch"}
    assert len(approvals) == (0 if phase == "approval" else 1)
    assert engine.mcp.calls == []
    assert engine.messages[-1]["role"] == "tool"
    assert engine.messages[-1]["content"] == client_engine.CANCELLED_TOOL_RESULT
    assert engine.messages[-1]["tool_status"] == client_events.STATUS_ERROR


@pytest.mark.parametrize("operation", ["response", "compact"])
def test_preparation_gate_failure_releases_turn_without_post(activity_engine, monkeypatch, operation):
    engine = activity_engine
    activity, requests = [], []
    _bind(engine, activity.append)

    def reject(**_kwargs):
        raise RuntimeError("context gate refused")

    monkeypatch.setattr(context_budget, "check_and_log", reject)
    monkeypatch.setattr(llama_client, "chat_completions", lambda **kwargs: requests.append(kwargs))
    with pytest.raises(RuntimeError, match="context gate refused"):
        if operation == "compact":
            engine.complete([{"role": "user", "content": "summarize"}], source="compaction")
        else:
            engine.send("question")
    assert [event["part"] for event in activity] == [{"operation": operation, "phase": "preparing"}]
    assert requests == [] and engine._in_turn == 0 and not engine._cancel.is_set()
    assert not any(message["role"] == "assistant" for message in engine.messages)


def test_activity_callback_does_not_change_zero_event_prime(activity_engine, monkeypatch):
    engine = activity_engine
    activity, requests = [], []
    _bind(engine, activity.append)
    engine.messages = [{"role": "user", "content": "old question"}, {"role": "assistant", "content": "old answer"}]
    before = copy.deepcopy(engine.messages)
    stored = copy.deepcopy(engine.store.read(engine.session_id))
    monkeypatch.setattr(config, "CLIENT_PRIME_PROMPT_CACHE", True)
    monkeypatch.setattr(llama_client, "get_slots", lambda *_args, **_kwargs: [])

    def request(**kwargs):
        requests.append(kwargs)
        final = _delta({"content": "invisible"}, "stop")
        final["timings"] = {"prompt_n": 12}
        return iter([_progress(), final])

    monkeypatch.setattr(llama_client, "chat_completions", request)
    outcome = engine.prime_prompt_cache(reason="mount")
    assert outcome == client_engine.PrimeOutcome(True, "", 12)
    assert activity == []
    assert requests[0]["extra"] == {
        "max_tokens": 1,
        "chat_template_kwargs": {"enable_thinking": False, "thinking": False},
    }
    assert requests[0]["messages"] == engine.next_turn_prefix()
    assert engine.messages == before and engine.store.read(engine.session_id) == stored
    assert engine._in_turn == 0 and not engine._cancel.is_set() and not engine.priming
