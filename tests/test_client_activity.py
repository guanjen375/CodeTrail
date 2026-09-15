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


def test_request_prompt_progress_reaches_activity_callback(activity_engine, monkeypatch):
    engine = activity_engine
    activity, events, requests, text = [], [], [], []
    _bind(engine, activity.append)
    streams = iter([
        [_progress(cache=800), _progress(929, cache=800), _tool(), _progress(1000)],
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
        {"operation": "response", "phase": "prompt_processing", "percent": 92},
        {"operation": "response", "phase": "prompt_processing", "percent": 92},
        {"operation": "compact", "phase": "prompt_processing", "percent": 92},
    ], "SSE prompt_progress must reach the UI; processed already includes cache"
    request_phases = ["preparing", "waiting_model", "waiting_response", "prompt_processing", "generating"]
    assert [part["phase"] for part in parts] == request_phases + ["tool"] + request_phases * 2
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
    ]
    chunks = [
        _progress(0), *({"prompt_progress": value} for value in invalid),
        _progress(29, 100), _progress(3.0, 4.0), _progress(920, cache=800),
        _progress(1e308, 1e308), _progress(10**400, 10**400),
        _delta({"role": "assistant", "content": "", "reasoning_content": "", "tool_calls": []}),
        _delta({"reasoning_content": "thought"}), _progress(0),
        _delta({"content": "answer"}, "stop"),
    ]
    monkeypatch.setattr(llama_client, "chat_completions", lambda **_kwargs: iter(chunks))
    result = engine.send("question", on_event=events.append, on_text=text.append, on_reasoning=reasoning.append)
    parts = [event["part"] for event in activity]
    assert [part.get("percent") for part in parts if part["phase"] == "prompt_processing"] == [
        0, None, 29, 75, 92, 100,
    ]
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
    assert requests[0]["extra"] == {"max_tokens": 137, "return_progress": True}
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
    assert requests[0]["extra"] == {"max_tokens": 1}
    assert requests[0]["messages"] == engine.next_turn_prefix()
    assert engine.messages == before and engine.store.read(engine.session_id) == stored
    assert engine._in_turn == 0 and not engine._cancel.is_set() and not engine.priming
