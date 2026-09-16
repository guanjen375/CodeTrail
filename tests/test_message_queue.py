"""Safety contracts: accepted user history, tool adjacency, and no silent queue execution."""
from __future__ import annotations

import asyncio
import copy
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import client_app
import client_engine
import client_events
import client_mcp
import client_policy
import client_prompt
import client_store
import client_turns
import context_budget
import llama_client

pytestmark = pytest.mark.smoke


def _answer(text="answer"):
    return {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}


def _calls(*names):
    return {"choices": [{"delta": {"tool_calls": [
        {"index": index, "id": f"t{index}", "type": "function",
         "function": {"name": name, "arguments": json.dumps({"path": "."})}}
        for index, name in enumerate(names)
    ]}, "finish_reason": "tool_calls"}]}


class _Mcp:
    def __init__(self):
        self.calls = []
        self.on_call = lambda _name: None
        self.cancelled = []

    def begin_call(self, name, arguments):
        self.calls.append(name)

        def result():
            self.on_call(name)
            return client_mcp.ToolCallResult(name, "status: ok\nresult", None, False)

        return SimpleNamespace(result=result, cancel=lambda reason: self.cancelled.append(reason))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(context_budget, "log_metrics", lambda _usage: None)
    monkeypatch.setattr(llama_client, "chat_completions", lambda **_kwargs: iter([_answer()]))
    # Match the offline model's existing small/oversized payload scale so the
    # supplement gate still rejects its large input without contacting a server.
    monkeypatch.setattr(
        llama_client, "count_chat_tokens",
        lambda **kwargs: context_budget.estimate_tokens(
            messages=kwargs["messages"], tools=kwargs.get("tools"),
        )[0],
    )

    def make(*, policy=None, max_steps=4, on_approval=None, compactor=None):
        root = tmp_path / "project"
        root.mkdir(exist_ok=True)
        mcp = _Mcp()
        engine = client_engine.Engine(
            client_engine.EngineOptions(
                root=root, model="test", base_url="http://127.0.0.1:65535", n_ctx=131072,
                max_output_tokens=128, max_tool_steps=max_steps,
                policy=policy or client_policy.InteractivePolicy(),
            ),
            mcp=mcp, store=client_store.EphemeralSessionStore(root),
            system_prompt=client_prompt.SystemPrompt(text="SYSTEM"),
        )
        engine._tool_specs = {
            name: client_mcp.ToolSpec(name=name, description=name,
                                     input_schema={"type": "object", "properties": {}},
                                     read_only=name != "apply_patch")
            for name in ("read_file", "list_dir", "apply_patch")
        }
        engine._openai_tools = [spec.as_openai_tool() for spec in engine._tool_specs.values()]
        engine._loaded_tools = True
        events, jobs, receipts = [], [], []

        def emit(event):
            if event["type"] == client_events.TYPE_QUEUE and event["part"]["status"] == "delivered":
                # A delivery receipt must follow both memory admission and the
                # trusted session-store write, never an enqueue or stream start.
                text = event["part"]["text"]
                receipts.append((
                    any(m.get("role") == "user" and m.get("content") == text for m in engine.messages),
                    any(m.get("role") == "user" and m.get("content") == text
                        for m in engine.store.read(engine.session_id)),
                ))
            events.append(copy.deepcopy(event))

        coordinator = client_turns.TurnCoordinator(
            engine, emit=emit, on_approval=on_approval, compactor=compactor,
        )
        monkeypatch.setattr(coordinator, "_spawn", lambda body, _name: jobs.append(body))
        return SimpleNamespace(engine=engine, mcp=mcp, c=coordinator, events=events, jobs=jobs, receipts=receipts)

    return make


def _users(engine):
    return [message["content"] for message in engine.messages if message["role"] == "user"]


def _drain(h):
    for _ in range(50):
        if not h.jobs:
            assert all(memory and stored for memory, stored in h.receipts)
            return
        h.jobs.pop(0)()
    raise AssertionError("queue failed to drain within its bound")


def test_queue_fifo_edit_cancel_and_receipts_follow_history(harness):
    h = harness()
    h.c.start_turn("first")
    removed = h.c.enqueue("never send")
    edited = h.c.enqueue("old text")
    last = h.c.enqueue("last")
    h.c.edit_queued(edited.id, "edited")
    h.c.cancel_queued(removed.id)
    assert _users(h.engine) == [] and not h.mcp.calls
    _drain(h)
    assert _users(h.engine) == ["first", "edited", "last"]
    states = {item.id: item.status for item in h.c.queue_snapshot()}
    assert states == {removed.id: "cancelled", edited.id: "delivered", last.id: "delivered"}
    with pytest.raises(client_turns.QueueError):
        h.c.edit_queued(last.id, "too late")


def test_queue_is_bounded_and_inspection_cannot_execute_or_cross_sessions(harness, monkeypatch):
    h = harness()
    monkeypatch.setattr(client_turns, "MAX_QUEUE_ITEMS", 2)
    first = h.c.enqueue("one")
    h.c.enqueue("two", mode="supplement")
    with pytest.raises(client_turns.QueueError):
        h.c.enqueue("x" * (client_turns.MAX_QUEUE_TEXT_BYTES + 1))
    with pytest.raises(client_turns.QueueError):
        h.c.enqueue("overflow")
    with pytest.raises(client_turns.QueueError):
        h.c.enqueue("wrong", session_id="another-session")
    with pytest.raises(client_turns.QueueError):
        h.c.assert_session_change_allowed()
    h.c.queue_snapshot()
    h.c.edit_queued(first.id, "edited")
    assert h.c.queue_paused and h.jobs == [] and h.engine.messages == []
    # An external session replacement cannot cause old pending inputs to run.
    old_session = h.engine.session_id
    h.engine.session_id = "another-session"
    assert h.c.queue_snapshot() == ()
    assert h.c.resume_queue() is False
    assert h.jobs == []
    h.engine.session_id = old_session
    for item in h.c.queue_snapshot(pending_only=True):
        h.c.cancel_queued(item.id)
    h.c.assert_session_change_allowed()


def test_late_turn_choice_cannot_supplement_a_different_task(harness):
    h = harness()
    h.c.start_turn("first")
    old_turn = h.c.turn_id
    _drain(h)
    h.c.start_turn("second")
    item = h.c.enqueue("late first detail", mode="supplement", turn_id=old_turn)
    assert item.status == "deferred"
    h.jobs.pop(0)()
    assert _users(h.engine) == ["first", "second"]
    assert not any(e["type"] == client_events.TYPE_QUEUE and e["part"]["status"] == "delivered"
                   for e in h.events)
    _drain(h)
    assert _users(h.engine) == ["first", "second", "late first detail"]


def test_supplements_wait_for_entire_tool_batch_and_are_real_user_messages(harness, monkeypatch):
    h = harness()
    wire = []

    def chat(**kwargs):
        wire.append(copy.deepcopy(kwargs["messages"]))
        return iter([_calls("read_file", "list_dir") if len(wire) == 1 else _answer()])

    monkeypatch.setattr(llama_client, "chat_completions", chat)

    def during_tool(name):
        if name == "read_file":
            h.c.enqueue("detail one", mode="supplement")
            h.c.enqueue("detail two", mode="supplement")
        assert _users(h.engine) == ["original"]

    h.mcp.on_call = during_tool
    h.c.start_turn("original")
    _drain(h)
    assert len(wire) == 2
    assert [message["role"] for message in wire[1]] == [
        "system", "user", "assistant", "tool", "tool", "user", "user",
    ]
    assert _users(h.engine) == ["original", "detail one", "detail two"]
    assert all(not message.get("synthetic") for message in h.engine.messages if message["role"] == "user")
    assert client_engine.pending_tool_call_ids(h.engine.messages) == []
    assert all(item.status == "delivered" for item in h.c.queue_snapshot())


def test_supplement_does_not_interrupt_approval_or_an_active_write(harness, monkeypatch):
    tickets, entered, release = [], threading.Event(), threading.Event()
    h = harness(on_approval=tickets.append)
    requests = []

    def chat(**kwargs):
        requests.append(kwargs)
        return iter([_calls("apply_patch") if len(requests) == 1 else _answer()])

    monkeypatch.setattr(llama_client, "chat_completions", chat)
    asked = threading.Event()
    h.c._on_approval = lambda ticket: (tickets.append(ticket), asked.set())

    def write(_name):
        entered.set()
        assert release.wait(3)

    h.mcp.on_call = write
    h.c.start_turn("write")
    worker = threading.Thread(target=h.jobs.pop(0))
    worker.start()
    try:
        assert asked.wait(3)
        first = h.c.enqueue("during approval", mode="supplement")
        assert h.c.pending_approvals() == (tickets[0].approval_id,)
        assert h.mcp.calls == [] and first.status == "waiting"
        assert h.c.answer_approval(tickets[0].approval_id, True)
        assert entered.wait(3)
        h.c.enqueue("during write", mode="supplement")
        assert h.mcp.cancelled == [] and _users(h.engine) == ["write"]
    finally:
        release.set()
        if tickets:
            h.c.answer_approval(tickets[0].approval_id, False)
        worker.join(3)
    assert not worker.is_alive()
    assert _users(h.engine) == ["write", "during approval", "during write"]
    assert len(requests) == 2 and h.mcp.cancelled == []
    assert len(h.receipts) == 2 and all(memory and stored for memory, stored in h.receipts)


@pytest.mark.parametrize("where", ["commit", "compaction"])
def test_late_supplement_defers_without_false_delivery(harness, monkeypatch, where):
    h = harness()
    queued = []
    original_record = h.engine._record

    def record(message, **kwargs):
        if where == "commit" and message.get("role") == "assistant" and not queued:
            queued.append(h.c.enqueue("late detail", mode="supplement"))
            assert h.engine._turn_completed
            assert h.c.cancel() is False
        return original_record(message, **kwargs)

    monkeypatch.setattr(h.engine, "_record", record)
    if where == "compaction":
        def compact():
            if not queued:
                queued.append(h.c.enqueue("late detail", mode="supplement"))
            return SimpleNamespace(status="skipped", message="")
        h.c.compactor = SimpleNamespace(compact=compact)
    h.c.start_turn("original")
    h.jobs.pop(0)()
    assert _users(h.engine) == ["original"]
    receipts = [e["part"]["status"] for e in h.events if e["type"] == client_events.TYPE_QUEUE]
    assert "deferred" in receipts and "delivered" not in receipts
    _drain(h)
    assert _users(h.engine) == ["original", "late detail"]
    roles = [message["role"] for message in h.engine.messages]
    assert roles == ["user", "assistant", "user", "assistant"]


@pytest.mark.parametrize("failure", ["cancelled", "error", "step_limit"])
def test_failed_turn_keeps_undelivered_fifo_until_explicit_resume(harness, monkeypatch, failure):
    h = harness(max_steps=1 if failure == "step_limit" else 4)
    first = True

    def chat(**_kwargs):
        nonlocal first
        if first:
            first = False
            return iter([_calls("read_file", "list_dir")])
        return iter([_answer()])

    monkeypatch.setattr(llama_client, "chat_completions", chat)

    def tool(name):
        if name != "read_file":
            return
        h.c.enqueue("next task")
        h.c.enqueue("unread detail", mode="supplement")
        if failure == "cancelled":
            assert h.c.cancel()
        elif failure == "error":
            raise RuntimeError("write transport failed")

    h.mcp.on_call = tool
    h.c.start_turn("original")
    h.jobs.pop(0)()
    assert _users(h.engine) == ["original"]
    assert h.c.queue_paused and h.jobs == []
    assert client_engine.pending_tool_call_ids(h.engine.messages) == []
    assert [item.status for item in h.c.queue_snapshot()] == ["waiting", "deferred"]
    assert not any(e["type"] == client_events.TYPE_QUEUE and e["part"]["status"] == "delivered"
                   for e in h.events)
    h.c.queue_snapshot()
    assert h.jobs == []
    assert h.c.resume_queue()
    _drain(h)
    assert _users(h.engine) == ["original", "next task", "unread detail"]


@pytest.mark.parametrize("when", ["before_worker", "engine_begin"])
def test_cancel_before_queued_worker_starts_preserves_its_input(harness, monkeypatch, when):
    h = harness()
    item = h.c.enqueue("not yet accepted")
    assert h.c.resume_queue()
    begin = h.engine._begin_turn
    if when == "before_worker":
        assert h.c.cancel()
    else:
        def cancel_at_begin():
            begin()
            assert h.c.cancel()
        monkeypatch.setattr(h.engine, "_begin_turn", cancel_at_begin)
    h.jobs.pop(0)()
    assert h.engine.messages == [] and h.c.queue_paused
    assert h.c.queue_snapshot()[0].status == "deferred"
    monkeypatch.setattr(h.engine, "_begin_turn", begin)
    h.c.edit_queued(item.id, "corrected")
    assert h.c.resume_queue()
    _drain(h)
    assert _users(h.engine) == ["corrected"]


def test_supplement_cannot_bypass_readonly_or_context_gate(harness, monkeypatch):
    h = harness(policy=client_policy.ReadOnlyPolicy())
    requests = []

    def chat(**kwargs):
        requests.append(copy.deepcopy(kwargs["messages"]))
        if len(requests) == 1:
            h.c.enqueue("new request", mode="supplement")
            return iter([_calls("apply_patch")])
        return iter([_answer()])

    monkeypatch.setattr(llama_client, "chat_completions", chat)
    h.c.start_turn("original")
    _drain(h)
    assert h.mcp.calls == []
    assert any(message.get("tool_status") == client_events.STATUS_DENIED for message in h.engine.messages)
    assert _users(h.engine) == ["original", "new request"]
    assert requests[1][-1] == {"role": "user", "content": "new request"}

    h2 = harness()
    h2.engine.options.n_ctx = 4096
    large_text = "large supplement " * 2000
    seen = []
    gate = context_budget.check_and_log

    def bounded_gate(**kwargs):
        seen.append(copy.deepcopy(kwargs["messages"]))
        return gate(**kwargs)

    monkeypatch.setattr(context_budget, "check_and_log", bounded_gate)
    requests.clear()

    def second_chat(**kwargs):
        requests.append(kwargs)
        h2.c.enqueue(large_text, mode="supplement")
        return iter([_calls("read_file")])

    monkeypatch.setattr(llama_client, "chat_completions", second_chat)
    h2.c.start_turn("original")
    _drain(h2)
    assert len(requests) == 1 and len(seen) == 2 and h2.c.queue_paused
    assert _users(h2.engine) == ["original", large_text]
    assert any("CTX_OVERFLOW" in event.get("message", "") for event in h2.events)


def test_tui_busy_choice_keeps_draft_and_pending_inputs_block_exit_and_session(harness, monkeypatch):
    h = harness()
    monkeypatch.setattr(h.engine, "prime_prompt_cache", lambda **_kw: SimpleNamespace(sent=False, reason="test"))

    async def run():
        app = client_app.CodeTrailApp(h.engine)
        jobs = []
        monkeypatch.setattr(app.coordinator, "_spawn", lambda body, _name: jobs.append(body))
        async with app.run_test() as pilot:
            app.submit("original")
            prompt = app.query_one("#prompt", client_app.PromptInput)
            prompt.text = "draft detail"
            app.on_prompt_input_submitted(client_app.PromptInput.Submitted(prompt.text))
            await pilot.pause()
            assert isinstance(app.screen, client_app.QueueChoiceScreen)
            assert prompt.text == "draft detail"
            assert len(app.query(client_app.UserMessage)) == 1
            app.screen.dismiss("queue")
            await pilot.pause()
            assert prompt.text == ""
            assert [item.text for item in app.coordinator.queue_snapshot()] == ["draft detail"]
            assert len(app.query(client_app.UserMessage)) == 1
            # No worker was run. Cancel the active turn, retaining the queued item.
            assert app.coordinator.cancel()
            app.coordinator.finish_turn()
            previous = h.engine.session_id
            app._cmd_new("")
            app._leave()
            assert h.engine.session_id == previous and app.is_running
            assert app.coordinator.queue_snapshot(pending_only=True)
            app.coordinator.cancel_queued(app.coordinator.queue_snapshot()[0].id)

    asyncio.run(run())


def test_approval_message_editor_never_answers_the_tool_and_preserves_unsent_draft(harness, monkeypatch):
    h = harness()

    async def run():
        app = client_app.CodeTrailApp(h.engine)
        monkeypatch.setattr(app.coordinator, "_spawn", lambda _body, _name: None)
        async with app.run_test() as pilot:
            app.submit("original")
            ticket = client_turns.ApprovalTicket(
                "pending", client_engine.ApprovalRequest(h.engine.session_id, "apply_patch", {"patch": "whole patch"}),
            )
            app.coordinator._approvals[ticket.approval_id] = ticket
            app._show_approval(ticket)
            await pilot.pause()
            await pilot.click("#approval-message")
            await pilot.press("y", "n")
            screen = app.screen
            editor = screen.query_one("#approval-message-text", client_app.TextArea)
            assert editor.text == "yn" and not ticket.event.is_set()
            await pilot.click("#approval-supplement")
            assert app.coordinator.pending_approvals() == ("pending",)
            assert [item.text for item in app.coordinator.queue_snapshot()] == ["yn"]
            assert not ticket.event.is_set() and h.mcp.calls == []
            await pilot.click("#approval-message")
            editor.text = "unsent draft"
            assert app.coordinator.cancel()
            await pilot.pause()
            assert app.query_one("#prompt", client_app.PromptInput).text == "unsent draft"
            assert ticket.event.is_set() and ticket.granted is False
            app.coordinator.finish_turn()
            for item in app.coordinator.queue_snapshot(pending_only=True):
                app.coordinator.cancel_queued(item.id)

    asyncio.run(run())
