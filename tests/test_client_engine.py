"""client_engine / client_prompt / client_policy / client_notify 的契約。

AGENTS.md §2 的新安全檢查點集中在這裡:
  - 送出去的那一份才算數:reasoning 剝除與 prune 只改 payload,session 檔與
    畫面保留原文;認不出最新真實使用者訊息就整段不動。
  - 權限 policy:readonly 必須 deny 全部 mutator(判準是 readOnlyHint,不是
    寫死名單);互動模式的七個 ask 工具沒核准就不得執行。
  - 只有工具結果的 text block 進模型;structuredContent 只給 UI / eval。
  - ingest marker 只認 ingest_document 的結果、只認行首。
  - prompt cache 預熱是唯一一條沒有使用者訊息就打主模型的路徑:送的是下一輪的
    prefix、零寫入、非互動 policy 在任何 I/O 之前就拒絕、保留額 == 實送的 max_tokens。
"""
from __future__ import annotations

import copy
import json
import socket
import sys
import time
import traceback
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import client_engine  # noqa: E402
import client_events  # noqa: E402
import client_mcp  # noqa: E402
import client_notify  # noqa: E402
import client_policy  # noqa: E402
import client_prompt  # noqa: E402
import client_store  # noqa: E402
import ingest_notify  # noqa: E402
import config  # noqa: E402
import context_budget  # noqa: E402
import llama_client  # noqa: E402
from mcp_contract import PUBLIC_TOOL_ORDER  # noqa: E402

pytestmark = pytest.mark.smoke

READ_ONLY = frozenset(
    {
        "list_dir", "read_file", "grep_code", "code_rag_search", "file_info",
        "query_knowledge", "query_knowledge_strict", "git_status", "git_diff",
        "analyze_file",
    }
)


class FakeMcp:
    """替身 MCP client:工具目錄與真的 server 完全一致,呼叫由測試決定。"""

    def __init__(self, results=None, read_only=READ_ONLY):
        self.calls: list[tuple[str, dict]] = []
        self.results = dict(results or {})
        self._specs = tuple(
            client_mcp.ToolSpec(
                name=name,
                description=f"{name} description",
                input_schema={"type": "object", "properties": {}},
                read_only=name in read_only,
            )
            for name in PUBLIC_TOOL_ORDER
        )

    def tools(self):
        return self._specs

    def call(self, name, arguments=None, **_kwargs):
        self.calls.append((name, dict(arguments or {})))
        result = self.results.get(name)
        if isinstance(result, Exception):
            raise result
        if result is None:
            result = client_mcp.ToolCallResult(name, f"status: ok\n{name} result", None, False)
        return result


def _stream(*chunks):
    def _fake(**_kwargs):
        return iter(chunks)

    return _fake


def _text_chunk(text, finish=None):
    return {"choices": [{"delta": {"content": text}, "finish_reason": finish}]}


def _tool_chunk(name, arguments, index=0, call_id="call_1", finish=None):
    return {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ]
                },
                "finish_reason": finish,
            }
        ]
    }


@pytest.fixture()
def engine_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()

    def _make(*, mcp=None, policy=None, store=None, **option_kwargs):
        options = client_engine.EngineOptions(
            root=root,
            model="test-model",
            base_url="http://127.0.0.1:65535",
            n_ctx=131072,
            policy=policy or client_policy.InteractivePolicy(),
            **option_kwargs,
        )
        engine = client_engine.Engine(
            options,
            mcp=mcp or FakeMcp(),
            store=store or client_store.EphemeralSessionStore(root),
            system_prompt=client_prompt.SystemPrompt(text="SYSTEM"),
        )
        engine.load_tools()
        return engine

    _make.root = root
    return _make


# ============================================================
# 訊息轉換
# ============================================================
def test_only_reasoning_before_the_latest_user_message_is_dropped():
    messages = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1", "reasoning_content": "old"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2", "reasoning_content": "current"},
    ]
    out = client_engine.strip_historical_reasoning(messages)
    assert "reasoning_content" not in out[1]
    assert out[3]["reasoning_content"] == "current"
    # 其他欄位一個都不准動。
    assert [m["content"] for m in out] == ["q1", "a1", "q2", "a2"]
    assert [m["role"] for m in out] == [m["role"] for m in messages]


def test_a_history_without_a_real_user_message_is_left_alone():
    messages = [{"role": "assistant", "content": "a", "reasoning_content": "keep"}]
    assert client_engine.strip_historical_reasoning(messages)[0]["reasoning_content"] == "keep"


def test_a_synthetic_user_message_is_not_the_anchor():
    messages = [
        {"role": "user", "content": "real"},
        {"role": "assistant", "content": "a", "reasoning_content": "keep"},
        {"role": "user", "content": "summary injected", "synthetic": True},
    ]
    out = client_engine.strip_historical_reasoning(messages)
    assert out[1]["reasoning_content"] == "keep"


def test_prune_only_fires_past_both_thresholds():
    big = "x" * (30_000 * 4)  # 約 34k tokens
    messages = [{"role": "user", "content": "q0"}]
    for index in range(4):
        messages.append({"role": "assistant", "content": None, "tool_calls": []})
        messages.append({"role": "tool", "tool_call_id": f"c{index}", "content": big})
        messages.append({"role": "user", "content": f"q{index + 1}"})
    pruned, count = client_engine.prune_old_tool_outputs(messages)
    assert count > 0
    assert any(m.get("content") == client_engine.PRUNE_PLACEHOLDER for m in pruned)
    # 最近兩個 user turn 之內的工具結果一律保留。
    assert pruned[-2]["content"] == big


def test_one_huge_old_tool_result_is_pruned_on_its_own():
    """單獨一筆就跨過 40k 門檻的巨大工具結果,正是最該被清掉的那一筆。

    先看舊的累計再加本筆的話,它會整個受保護 —— pruned=0,而 context 照樣被
    它推到 overflow。
    """
    huge = "x" * 180_000            # 約 51k tokens
    messages = [
        {"role": "user", "content": "q0"},
        {"role": "tool", "tool_call_id": "c", "content": huge},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]
    pruned, count = client_engine.prune_old_tool_outputs(messages)
    assert count == 1
    assert pruned[1]["content"] == client_engine.PRUNE_PLACEHOLDER


def test_a_short_history_is_never_pruned():
    messages = [
        {"role": "user", "content": "q"},
        {"role": "tool", "tool_call_id": "c", "content": "small"},
        {"role": "user", "content": "q2"},
    ]
    pruned, count = client_engine.prune_old_tool_outputs(messages)
    assert count == 0 and pruned[1]["content"] == "small"


def test_the_transforms_never_touch_the_session_file(engine_factory, monkeypatch, tmp_path):
    store = client_store.SessionStore(engine_factory.root)
    engine = engine_factory(store=store)
    engine.messages = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1", "reasoning_content": "old"},
        {"role": "user", "content": "q2"},
    ]
    for message in engine.messages:
        store.append(engine.session_id, {"type": "message", **message})
    payload, summary = engine.payload_messages()
    assert summary.get("stripped_reasoning") == 1
    assert "reasoning_content" not in payload[2]
    # in-memory 與 session 檔都保留原文。
    assert engine.messages[1]["reasoning_content"] == "old"
    persisted = list(client_store.iter_messages(store.read(engine.session_id)))
    assert persisted[1]["reasoning_content"] == "old"


def test_a_session_write_failure_is_reported_not_swallowed(engine_factory, monkeypatch):
    """吞掉的話這段對話只活在記憶體裡,重開之後整段消失而且從來沒有警告。"""
    engine = engine_factory()

    def _boom(*_args, **_kwargs):
        raise client_store.SessionStoreError("disk full")

    monkeypatch.setattr(engine.store, "append", _boom)
    monkeypatch.setattr(
        llama_client, "chat_completions", _stream(_text_chunk("ok", finish="stop"))
    )
    result = engine.send("hi", on_event=lambda _e: None)
    assert any("沒有落檔" in notice for notice in result.notices), result.notices
    assert engine.store_error and "disk full" in engine.store_error


def test_a_healed_tool_result_sits_next_to_its_call_after_a_resume(engine_factory, monkeypatch):
    """resume 之後再問一題:補救的 tool 結果必須緊接在宣告它的 assistant 之後。

    補在整段尾端會排成 `assistant(tool_calls) → user → tool` —— 那不是合法的
    相鄰配對,chat template 可能直接拒收,也可能讓模型把結果配到錯的呼叫上。
    """
    store = client_store.SessionStore(engine_factory.root)
    engine = engine_factory(store=store)
    session_id = store.create()
    store.append(session_id, {"type": "message", "role": "user", "content": "q"})
    store.append(
        session_id,
        {
            "type": "message",
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}
            ],
        },
    )
    engine.resume(session_id)
    monkeypatch.setattr(
        llama_client, "chat_completions", _stream(_text_chunk("ok", finish="stop"))
    )
    engine.send("下一題", on_event=lambda _e: None)
    payload, _ = engine.payload_messages()
    roles = [m["role"] for m in payload]
    assert roles == ["system", "user", "assistant", "tool", "user", "assistant"], roles
    # 補救也要落檔,否則每次 resume 都會再重現一次。
    persisted = [r["role"] for r in client_store.iter_messages(store.read(session_id))]
    assert persisted[:4] == ["user", "assistant", "tool", "user"]


def test_a_dangling_tool_call_is_healed_before_the_next_request(engine_factory):
    engine = engine_factory()
    engine.messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}
            ],
        },
    ]
    payload, summary = engine.payload_messages()
    assert summary.get("healed_tool_calls") == 1
    assert payload[-1]["role"] == "tool" and payload[-1]["tool_call_id"] == "c1"


# ============================================================
# 權限
# ============================================================
def test_engines_sharing_one_mcp_share_one_model_lock(engine_factory):
    """llama-server 是單 slot:兩條對話各自 new 一把鎖等於沒有鎖。"""
    mcp = FakeMcp()
    first = engine_factory(mcp=mcp)
    second = engine_factory(mcp=mcp)
    assert first.model_lock is second.model_lock
    assert engine_factory(mcp=FakeMcp()).model_lock is not first.model_lock


def test_a_string_false_read_only_hint_is_not_read_only():
    """`bool("false")` 是 True —— catalog 型別一漂移就會放行寫入工具。"""
    listed = {
        "tools": [
            {
                "name": "list_dir",
                "inputSchema": {"type": "object"},
                "annotations": {"readOnlyHint": "false"},
            }
        ]
    }
    assert client_mcp.tool_specs(listed)[0].read_only is False


def test_readonly_policy_denies_every_mutator():
    policy = client_policy.ReadOnlyPolicy()
    for name in client_policy.MUTATING_TOOLS:
        assert policy.decide(name, read_only=False, arguments={}) is client_policy.Decision.DENY
    for name in READ_ONLY:
        assert policy.decide(name, read_only=True, arguments={}) is client_policy.Decision.ALLOW


def test_readonly_policy_denies_a_new_tool_that_is_not_read_only():
    """判準是 readOnlyHint,不是寫死名單:新工具漏加名單也不得被放行。"""
    policy = client_policy.ReadOnlyPolicy()
    assert policy.decide("brand_new_writer", read_only=False, arguments={}) is client_policy.Decision.DENY


def test_interactive_policy_asks_for_the_seven_write_tools():
    """2026-09-04:`import_external_file` 加進 ASK_TOOLS。

    行為為什麼該變:plan.txt §6 第 12 條明訂「開了之後**每一次**匯入仍然要人工
    核准」——那個開關授權的是「可以從專案外複製檔案進來」這件事本身,不是每一
    次的來源與目的。以前它落在「不是唯讀但也不需要每次問」那一組。
    """
    policy = client_policy.InteractivePolicy()
    assert client_policy.ASK_TOOLS == frozenset(
        {"apply_patch", "run_lint", "run_command", "remove_document", "record_lesson",
         "review_figures", "import_external_file"}
    )
    for name in client_policy.ASK_TOOLS:
        assert policy.decide(name, read_only=False, arguments={}) is client_policy.Decision.ASK
    assert policy.decide("list_dir", read_only=True, arguments={}) is client_policy.Decision.ALLOW
    # 其餘一律 allow。改成「非唯讀就 ask」會讓一次 ingest 多跳一個核准框,那是
    # 使用者沒要求過的行為改變。fail-closed 的那一半在 ReadOnlyPolicy。
    for name in ("ingest_document", "reload_knowledge_base"):
        assert policy.decide(name, read_only=False, arguments={}) is client_policy.Decision.ALLOW


def test_a_denied_ask_never_reaches_the_mcp_server(engine_factory, monkeypatch):
    mcp = FakeMcp()
    engine = engine_factory(mcp=mcp)
    monkeypatch.setattr(
        llama_client,
        "chat_completions",
        _stream(
            _tool_chunk("run_command", '{"command": "pytest"}', finish="tool_calls"),
        ),
    )
    events: list[dict] = []
    asked: list[str] = []

    def _approve(request):
        asked.append(request.tool)
        return False

    result = engine.run_tool_loop(on_event=events.append, approve=_approve)
    assert mcp.calls == []
    assert result.denied >= 1
    tool_message = [m for m in engine.messages if m["role"] == "tool"][0]
    assert "permission denied" in tool_message["content"]
    # 重問有上限:模型一直重送同一個呼叫時,核准框不會一路跳到 max_tool_steps。
    assert len(asked) == client_policy.MAX_DENIED_RETRIES


def test_a_repeatedly_denied_tool_stops_asking_the_user(engine_factory, monkeypatch):
    mcp = FakeMcp()
    engine = engine_factory(mcp=mcp, max_tool_steps=6)
    monkeypatch.setattr(
        llama_client,
        "chat_completions",
        lambda **_k: iter([_tool_chunk("apply_patch", "{}", finish="tool_calls")]),
    )
    asked = []
    engine.run_tool_loop(on_event=lambda _e: None, approve=lambda r: asked.append(r.tool) or False)
    assert len(asked) == client_policy.MAX_DENIED_RETRIES
    assert mcp.calls == []


def test_the_approval_box_shows_every_argument_in_full():
    patch = "--- a/x.py\n+++ b/x.py\n@@\n-old\n+new\n" * 20
    request = client_engine.ApprovalRequest("s", "apply_patch", {"diff": patch, "dry_run": False})
    rendered = request.render()
    assert patch in rendered
    assert "dry_run" in rendered


# ============================================================
# 工具迴圈
# ============================================================
def test_only_the_text_block_is_fed_back_to_the_model(engine_factory, monkeypatch):
    mcp = FakeMcp(
        results={
            "query_knowledge": client_mcp.ToolCallResult(
                "query_knowledge", "status: ok\nREF1 ...", {"refs": ["secret structured"]}, False
            )
        }
    )
    engine = engine_factory(mcp=mcp)
    monkeypatch.setattr(
        llama_client,
        "chat_completions",
        _stream(
            _tool_chunk("query_knowledge", '{"question": "x"}', finish="tool_calls"),
        ),
    )
    engine.run_tool_loop(on_event=lambda _e: None)
    payload, _ = engine.payload_messages()
    serialised = json.dumps(payload, ensure_ascii=False)
    assert "REF1" in serialised
    assert "secret structured" not in serialised


def test_a_text_only_turn_emits_one_terminal_step(engine_factory, monkeypatch):
    engine = engine_factory()
    monkeypatch.setattr(
        llama_client, "chat_completions", _stream(_text_chunk("hello", finish="stop"))
    )
    events: list[dict] = []
    result = engine.send("hi", on_event=events.append)
    assert result.text == "hello"
    assert [e["type"] for e in events] == ["text", "step_finish"]
    assert client_events.is_terminal_event(events[-1])


def test_a_tool_step_is_not_a_terminal_step(engine_factory, monkeypatch):
    engine = engine_factory()
    calls = iter(
        [
            iter([_tool_chunk("list_dir", '{"path": "."}', finish="tool_calls")]),
            iter([_text_chunk("done", finish="stop")]),
        ]
    )
    monkeypatch.setattr(llama_client, "chat_completions", lambda **_k: next(calls))
    events: list[dict] = []
    engine.send("hi", on_event=events.append)
    tool_steps = [e for e in events if e["type"] == "step_finish"]
    assert client_events.is_terminal_event(tool_steps[0]) is False
    assert client_events.is_terminal_event(tool_steps[-1]) is True
    completed = [
        client_events.completed_tool_call(e)
        for e in events
        if e["type"] == client_events.TYPE_TOOL_USE
    ]
    assert [c.bare_tool for c in completed if c] == ["list_dir"]


def test_the_loop_stops_instead_of_spinning(engine_factory, monkeypatch):
    engine = engine_factory(max_tool_steps=3)
    monkeypatch.setattr(
        llama_client,
        "chat_completions",
        lambda **_k: iter([_tool_chunk("list_dir", '{"path": "."}', finish="tool_calls")]),
    )
    result = engine.run_tool_loop(on_event=lambda _e: None)
    assert result.steps == 3
    assert "已停止" in result.text


def test_broken_tool_arguments_are_reported_not_executed(engine_factory, monkeypatch):
    mcp = FakeMcp()
    engine = engine_factory(mcp=mcp)
    monkeypatch.setattr(
        llama_client,
        "chat_completions",
        _stream(_tool_chunk("list_dir", "{not json", finish="tool_calls")),
    )
    engine.run_tool_loop(on_event=lambda _e: None)
    assert mcp.calls == []
    assert "不是合法 JSON" in [m for m in engine.messages if m["role"] == "tool"][0]["content"]


def _repeated_grep_scenario(engine_factory, monkeypatch, *, nearby_patterns=False):
    """重現真實的搜尋空轉；結果經過正式 adapter 與帶遞增次數的 server banner。"""
    import repeat_guard
    import tool_result_adapter

    class _Search(FakeMcp):
        def __init__(self):
            super().__init__()
            self.guard = repeat_guard.RepeatGuard()

        def call(self, name, arguments=None, **_kwargs):
            args = dict(arguments or {})
            self.calls.append((name, args))
            body = f"=== rg '{args['pattern']}' (1 matches) ===\nsrc/loader.c:18:load_segment(image);"
            count = self.guard.observe(name, repeat_guard.args_key((), args), body)
            if count >= repeat_guard.BANNER_THRESHOLD:
                body = repeat_guard.banner(name, count) + body
            adapted = tool_result_adapter.adapt_tool_result(
                name, body,
                budget=tool_result_adapter.resolve_result_budget(
                    n_ctx=131072, requested_max_chars=None, safety_max_chars=200000,
                ),
            )
            return client_mcp.ToolCallResult(
                name, adapted.content[0].text, {"private": "UI only"}, adapted.isError,
            )

    mcp = _Search()
    engine = engine_factory(mcp=mcp)
    requests = []
    gates = []
    events = []
    summary = (
        "已證實：src/loader.c:18 呼叫 load_segment。尚未確認：兩份 ELF 的失敗差異。"
        "下一步：比對兩份 PT_LOAD 的位址與大小。"
    )
    check = context_budget.check_and_log

    def _gate(**kwargs):
        gates.append(kwargs)
        return check(**kwargs)

    def _model(**kwargs):
        requests.append(kwargs)
        if kwargs.get("tool_choice") == "none":
            return iter([_text_chunk(summary, finish="stop")])
        pattern = f"load_segment|unused_{len(requests)}" if nearby_patterns else "load_segment"
        return iter([
            _text_chunk("我理解了，找到關鍵差異。"),
            _tool_chunk("grep_code", json.dumps({"pattern": pattern}),
                        call_id=f"lookup_{len(requests)}", finish="tool_calls"),
        ])

    monkeypatch.setattr(context_budget, "check_and_log", _gate)
    monkeypatch.setattr(llama_client, "chat_completions", _model)
    result = engine.send("比較可執行與失敗的 ELF，列出證據、差異與下一步。", on_event=events.append)
    return engine, mcp, requests, gates, events, summary, result


@pytest.mark.smoke
def test_repeated_grep_gets_one_evidence_based_final_pass(engine_factory, monkeypatch):
    engine, mcp, requests, gates, events, summary, result = _repeated_grep_scenario(
        engine_factory, monkeypatch,
    )
    assert len(mcp.calls) == 2, "相同 grep 結果不應反覆執行到 24 步耗盡"
    assert [r["tool_choice"] for r in requests] == ["auto", "auto", "none"]
    assert result.steps == 3 and result.text == summary and result.finish == "stop"
    assert requests[-1]["tools"] == engine.openai_tools()
    assert gates[-1]["messages"] is requests[-1]["messages"]
    assert gates[-1]["tools"] is requests[-1]["tools"]
    assert gates[-1]["reserved_output_tokens"] == requests[-1]["extra"]["max_tokens"]
    assert "UI only" not in json.dumps(requests[-1]["messages"])
    assert len([e for e in events if client_events.is_terminal_event(e)]) == 1
    assert client_events.is_terminal_event(events[-1])
    assert not client_engine.pending_tool_call_ids(engine.messages)
    saved = list(client_store.iter_messages(engine.store.read(engine.session_id)))
    assert any(m.get("structured") == {"private": "UI only"} for m in saved)
    assert any("第 2 次" in str(m.get("content")) for m in saved if m["role"] == "tool")
    assert requests[-1]["messages"][0] != requests[0]["messages"][0]
    saved_users = [m for m in saved if m["role"] == "user"]
    assert len(saved_users) == 1 and saved_users[0]["content"] == engine.messages[0]["content"]
    assert not saved_users[0].get("synthetic")

    # 真實使用者的下一輪可以重新查證，收斂指示沒有污染歷史或共用工具 schema。
    expected_prefix = engine.next_turn_prefix()
    before = len(requests)
    engine.send("我已更新檔案，請重新比較。")
    assert len(mcp.calls) == 4
    assert requests[before]["tool_choice"] == "auto"
    assert requests[before]["messages"][:-1] == expected_prefix
    assert requests[before]["messages"][0] == requests[0]["messages"][0]


@pytest.mark.smoke
def test_nearby_grep_patterns_with_the_same_sources_converge(engine_factory, monkeypatch):
    engine, mcp, requests, _gates, _events, summary, result = _repeated_grep_scenario(
        engine_factory, monkeypatch, nearby_patterns=True,
    )
    assert len(mcp.calls) == 3, "微調 pattern、來源行不變不能讓停滯判斷持續重置"
    assert len({args["pattern"] for _name, args in mcp.calls}) == 3
    assert [r["tool_choice"] for r in requests] == ["auto", "auto", "auto", "none"]
    assert result.text == summary and result.finish == "stop" and result.steps == 4
    assert not client_engine.pending_tool_call_ids(engine.messages)


@pytest.mark.smoke
def test_convergence_refuses_all_returned_tools_and_preserves_group_order(engine_factory, monkeypatch):
    import client_compaction

    mcp = FakeMcp()
    engine = engine_factory(mcp=mcp, max_tool_steps=2)
    requests, events, asked = [], [], []

    def model(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            return iter([_tool_chunk("read_file", '{"path":"loader.c"}',
                                     call_id="source", finish="tool_calls")])
        return iter([
            _tool_chunk("apply_patch", '{"diff":"whole patch"}', index=0, call_id="write"),
            _tool_chunk("run_command", '{"command":"make"}', index=1, call_id="run"),
            _tool_chunk("grep_code", '{"pattern":"load"}', index=2, call_id="read", finish="tool_calls"),
        ])

    monkeypatch.setattr(llama_client, "chat_completions", model)
    result = engine.send("比對來源", on_event=events.append, approve=lambda r: asked.append(r) or True)
    assert [r["tool_choice"] for r in requests] == ["auto", "none"]
    assert mcp.calls == [("read_file", {"path": "loader.c"})] and not asked
    assert result.finish == "error" and result.text.startswith("[已停止]") and result.steps == 2
    tools = [m for m in engine.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tools] == ["source", "write", "run", "read"]
    assert all(m["tool_status"] == "error" and "未執行" in m["content"] for m in tools[1:])
    assert [m["role"] for m in engine.messages] == [
        "user", "assistant", "tool", "assistant", "tool", "tool", "tool", "assistant",
    ]
    tool_events = [e for e in events if e["type"] == client_events.TYPE_TOOL_USE]
    assert len(tool_events) == 4
    assert len([e for e in events if client_events.is_terminal_event(e)]) == 1
    assert engine.messages[-1]["tool_status"] == "error"
    assert client_compaction.completed_turns(engine.messages) == []
    assert not client_engine.pending_tool_call_ids(engine.messages)


@pytest.mark.smoke
def test_tool_call_budget_completes_unexecuted_members_of_one_batch(engine_factory, monkeypatch):
    monkeypatch.setattr(config, "CLIENT_MAX_TOOL_CALLS_PER_TURN", 2)
    mcp = FakeMcp()
    engine = engine_factory(mcp=mcp)
    requests, asked, events = [], [], []

    def model(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            return iter([
                _tool_chunk("read_file", '{"path":"a.c"}', index=0, call_id="a"),
                _tool_chunk("read_file", '{"path":"b.c"}', index=1, call_id="b"),
                _tool_chunk("apply_patch", '{"diff":"full patch"}', index=2, call_id="c"),
                _tool_chunk("read_file", '{"path":"c.c"}', index=3, call_id="d", finish="tool_calls"),
            ])
        return iter([_text_chunk("目前只有兩份讀取結果，尚未確認原因。下一步比對入口位址。", finish="stop")])

    monkeypatch.setattr(llama_client, "chat_completions", model)
    result = engine.send("分析差異", on_event=events.append, approve=lambda r: asked.append(r) or True)
    assert len(mcp.calls) == 2 and not asked
    assert [r["tool_choice"] for r in requests] == ["auto", "none"]
    assert result.tool_calls == 4 and result.steps == 2
    replies = [m for m in engine.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in replies] == ["a", "b", "c", "d"]
    assert [m["tool_status"] for m in replies] == ["completed", "completed", "error", "error"]
    assert all("未執行" in m["content"] for m in replies[2:])
    assert not client_engine.pending_tool_call_ids(engine.messages)
    assert len([e for e in events if e["type"] == client_events.TYPE_TOOL_USE]) == 4


@pytest.mark.smoke
def test_progress_rechecks_changed_results_and_reads_after_partial_writes(engine_factory, monkeypatch):
    class _Changed(FakeMcp):
        def __init__(self):
            super().__init__()
            self.reads = 0

        def call(self, name, arguments=None, **kwargs):
            super().call(name, arguments, **kwargs)
            if name == "apply_patch":
                return client_mcp.ToolCallResult(name, "status: error\npatch 已套用,驗證失敗", None, True)
            self.reads += 1
            text = "old" if self.reads == 1 else "new"
            return client_mcp.ToolCallResult(name, f"status: ok\n1 | {text}", None, False)

    mcp = _Changed()
    engine = engine_factory(mcp=mcp)
    requests = []

    def model(**kwargs):
        requests.append(kwargs)
        if kwargs["tool_choice"] == "none":
            return iter([_text_chunk("修改後讀到 new；驗證仍失敗。下一步檢查驗證錯誤。", finish="stop")])
        index = len(requests)
        name, args = ("apply_patch", '{"diff":"patch"}') if index == 3 else ("read_file", '{"path":"a.c"}')
        return iter([_tool_chunk(name, args, call_id=f"call_{index}", finish="tool_calls")])

    monkeypatch.setattr(llama_client, "chat_completions", model)
    result = engine.send("修改後重讀", approve=lambda _r: True)
    assert [name for name, _args in mcp.calls] == ["read_file", "read_file", "apply_patch", "read_file", "read_file"]
    assert [r["tool_choice"] for r in requests] == ["auto"] * 5 + ["none"]
    assert result.finish == "stop"


@pytest.mark.smoke
@pytest.mark.parametrize("failure", ["empty", "truncated", "length", "repeated_preamble"])
def test_failed_convergence_preserves_raw_output_without_completing_the_turn(engine_factory, monkeypatch, failure):
    import client_compaction

    engine = engine_factory(max_tool_steps=2)
    requests, events = [], []
    raw = "我理解了，找到關鍵差異。" if failure == "repeated_preamble" else "半段答案"
    if failure == "empty":
        raw = ""

    def model(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            return iter([
                _text_chunk("我理解了，找到關鍵差異。"),
                _tool_chunk("read_file", '{"path":"a.c"}', call_id="a", finish="tool_calls"),
            ])
        finish = None if failure == "truncated" else "length" if failure == "length" else "stop"
        return iter([_text_chunk(raw, finish=finish)])

    monkeypatch.setattr(llama_client, "chat_completions", model)
    result = engine.send("比較差異", on_event=events.append)
    assert requests[-1]["tool_choice"] == "none" and len(requests) == 2
    assert result.finish == "error" and result.text.startswith("[已停止]")
    assert engine.messages[-2]["content"] == (raw or None)
    assert engine.messages[-2]["tool_status"] == "error"
    assert engine.messages[-1]["tool_status"] == "error"
    assert client_compaction.completed_turns(engine.messages) == []
    assert len([e for e in events if client_events.is_terminal_event(e)]) == 1


@pytest.mark.smoke
@pytest.mark.parametrize("cancel_at", ["before_convergence", "streaming", "final_commit"])
def test_convergence_cancellation_never_commits_an_answer_or_posts_again(engine_factory, monkeypatch, cancel_at):
    import client_turns

    class _CancelBefore(FakeMcp):
        def call(self, name, arguments=None, **kwargs):
            result = super().call(name, arguments, **kwargs)
            if cancel_at == "before_convergence":
                assert engine.cancel() is True
            return result

    mcp = _CancelBefore()
    engine = engine_factory(mcp=mcp, max_tool_steps=2)
    requests, events = [], []

    def model(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            return iter([_tool_chunk("read_file", '{"path":"a.c"}', call_id="a", finish="tool_calls")])

        def stream():
            yield _text_chunk("尚未完成")
            if cancel_at == "streaming":
                assert engine.cancel() is True
            yield _text_chunk("", finish="stop")

        return stream()

    monkeypatch.setattr(llama_client, "chat_completions", model)
    commit = engine._commit_final

    def cancel_before_commit(message):
        if cancel_at == "final_commit":
            assert engine.cancel() is True
        commit(message)

    monkeypatch.setattr(engine, "_commit_final", cancel_before_commit)
    coordinator = client_turns.TurnCoordinator(engine, emit=events.append)
    monkeypatch.setattr(coordinator, "_spawn", lambda body, _name: body())
    coordinator.start_turn("比較差異")
    assert len(requests) == (1 if cancel_at == "before_convergence" else 2)
    assert not any(m["role"] == "assistant" and not m.get("tool_calls") for m in engine.messages)
    assert not client_engine.pending_tool_call_ids(engine.messages)
    terminal = [e for e in events if client_events.is_terminal_event(e)]
    assert len(terminal) == 1 and terminal[0]["part"]["reason"] == "cancelled"
    assert not coordinator.busy


# ============================================================
# 通知
# ============================================================
def test_only_ingest_document_is_trusted_to_emit_the_action_marker():
    body = "[CODETRAIL_ACTION_REQUIRED]\nfigure 3"
    assert client_notify.ingest_notice("ingest_document", body) is not None
    assert client_notify.ingest_notice("read_file", body) is None


def test_a_failed_ingest_wins_over_action_required():
    """兩個 marker 同時出現時 server 判成 error;通知不得說「有待覆核的圖」。

    說錯的話使用者會被導去處理一份根本沒有成功入庫的文件。
    """
    body = "[CODETRAIL_INGEST_FAILED]\nboom\n[CODETRAIL_ACTION_REQUIRED]\nfigure 3"
    assert ingest_notify.classify_ingest_body(body)[0] == "error"
    notice = client_notify.ingest_notice("ingest_document", body)
    assert notice is not None and notice.kind == "ingest_failed"


def test_a_marker_in_the_middle_of_a_line_never_notifies():
    body = "run: python3 RAG.py --file '[CODETRAIL_ACTION_REQUIRED].pdf'"
    assert client_notify.ingest_notice("ingest_document", body) is None


@pytest.mark.parametrize(
    "text",
    [
        "我現在就來呼叫 codetrail 的工具",
        "Let me call the list_dir tool now",
        "<tool_call>",
    ],
)
def test_claimed_tool_calls_are_detected(text):
    assert client_notify.claims_tool_call(text) is True


def test_a_fake_promise_after_a_real_call_is_still_detected(engine_factory, monkeypatch):
    """判準是**這一則**回應有沒有 tool call,不是整輪曾經呼叫過。

    整輪累計的話,先做了一次真呼叫就會讓最後那句「我現在來呼叫 read_file」
    完全不被偵測到。
    """
    engine = engine_factory()
    calls = iter(
        [
            iter([_tool_chunk("list_dir", '{"path": "."}', finish="tool_calls")]),
            iter([_text_chunk("Let me call the read_file tool now", finish="stop")]),
        ]
    )
    monkeypatch.setattr(llama_client, "chat_completions", lambda **_k: next(calls))
    result = engine.send("hi", on_event=lambda _e: None)
    assert any("結構化 tool call" in notice for notice in result.notices), result.notices


@pytest.mark.parametrize(
    "text",
    [
        "我沒有呼叫任何工具",
        "我會使用文字說明,而不呼叫任何工具",
        "I cannot call the tool right now",
        "目前沒有可用的工具",
        "I'll call out that the tool is unavailable",
    ],
)
def test_denials_are_never_counted_as_claims(text):
    assert client_notify.claims_tool_call(text) is False


# ============================================================
# system prompt
# ============================================================
def test_the_base_rules_stay_under_the_hard_budget():
    assert len(client_prompt.BASE_RULES) <= client_prompt.BASE_RULES_MAX_CHARS


def test_the_prompt_carries_mcp_instructions_and_project_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(client_prompt, "project_instructions_enabled", lambda: True)
    root = tmp_path / "project"
    (root / ".codetrail").mkdir(parents=True)
    (root / "AGENTS.md").write_text("PROJECT RULE", encoding="utf-8")
    (root / ".codetrail" / "lessons.md").write_text("- [L-001] LESSON", encoding="utf-8")
    prompt = client_prompt.build_system_prompt(root)
    assert "PROJECT RULE" in prompt.text
    assert "LESSON" in prompt.text
    assert "code_rag_search" in prompt.text  # MCP_INSTRUCTIONS
    assert {section.name for section in prompt.sections} >= {
        "base_rules", "mcp_instructions", "project_agents", "lessons"
    }


def test_project_instructions_can_be_turned_off(tmp_path, monkeypatch):
    """安全模式:`client.json` 的 `project_instructions: false` → 不讀專案內的指示。

    2026-09-04:開關從 `CODETRAIL_DISABLE_PROJECT_INSTRUCTIONS`(非空即關,所以
    `...=0` 是**關閉**)換成 client.json 的一個真 boolean。行為為什麼該變:
    它決定「被分析的 repo 能不能把文字塞進每一輪 system prompt」,那道邊界不該
    隨殼層漂移,而且「=0 代表關」正是設定不該有的形狀。
    """
    import config

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    (root / ".codetrail").mkdir(parents=True)
    (root / "AGENTS.md").write_text("PROJECT RULE", encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_INSTRUCTIONS_ENABLED", False)
    monkeypatch.setenv("CODETRAIL_DISABLE_PROJECT_INSTRUCTIONS", "")  # 殘留值無效
    assert "PROJECT RULE" not in client_prompt.build_system_prompt(root).text

    monkeypatch.setattr(config, "PROJECT_INSTRUCTIONS_ENABLED", True)
    assert "PROJECT RULE" in client_prompt.build_system_prompt(root).text


def test_a_symlinked_instructions_directory_is_refused(tmp_path, monkeypatch):
    """只驗最終檔案不夠:整個目錄被換成 symlink 時,檔案本身不是 symlink。

    不擋的話,不信任的 repo 可以把 `.codetrail` 指到外面,裡面放一份普通的
    lessons.md,那份內容從此進每一輪 system prompt。
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "lessons.md").write_text("INJECTED", encoding="utf-8")
    (root / ".codetrail").symlink_to(outside, target_is_directory=True)
    with pytest.raises(client_prompt.PromptError, match="symlink"):
        client_prompt.build_system_prompt(root)


def test_a_symlinked_instructions_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    (root / ".codetrail").mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("INJECTED", encoding="utf-8")
    (root / ".codetrail" / "lessons.md").symlink_to(outside)
    with pytest.raises(client_prompt.PromptError, match="symlink"):
        client_prompt.build_system_prompt(root)


def test_an_oversized_user_instructions_file_is_fail_loud(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target = home.joinpath(*client_prompt.USER_INSTRUCTIONS_PARTS)
    target.parent.mkdir(parents=True)
    target.write_text("x" * (client_prompt.USER_INSTRUCTIONS_MAX_CHARS + 1), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "project"
    root.mkdir()
    with pytest.raises(client_prompt.PromptError, match="字元上限"):
        client_prompt.build_system_prompt(root)


# ============================================================
# context gate
# ============================================================
def test_the_gate_counts_the_transformed_payload(engine_factory, monkeypatch):
    engine = engine_factory()
    engine.messages = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a", "reasoning_content": "t" * 100_000},
        {"role": "user", "content": "q2"},
    ]
    payload, _ = engine.payload_messages()
    sent, _chars = context_budget.estimate_tokens(messages=payload)
    kept, _chars2 = context_budget.estimate_tokens(messages=client_engine.to_wire(engine.messages))
    assert sent < kept


def test_the_gate_reserve_is_the_max_tokens_we_send(engine_factory, monkeypatch):
    engine = engine_factory()
    captured: dict = {}

    def _fake_check(**kwargs):
        captured.update(kwargs)
        return context_budget.build_usage(
            source=kwargs["source"],
            requested_num_ctx=kwargs["requested_num_ctx"],
            messages=kwargs["messages"],
            reserved_output_tokens=kwargs["reserved_output_tokens"],
        )

    monkeypatch.setattr(context_budget, "check_and_log", _fake_check)
    sent: dict = {}

    def _fake_chat(**kwargs):
        sent.update(kwargs)
        return iter([_text_chunk("ok", finish="stop")])

    monkeypatch.setattr(llama_client, "chat_completions", _fake_chat)
    engine.send("hi", on_event=lambda _e: None)
    assert captured["reserved_output_tokens"] == config.CLIENT_MAX_OUTPUT_TOKENS
    assert sent["extra"]["max_tokens"] == config.CLIENT_MAX_OUTPUT_TOKENS


# ============================================================
# 壓縮與 session 檔的交界(S3 審核回修)
# ============================================================
def test_a_compaction_that_cannot_be_persisted_does_not_change_the_history(engine_factory):
    """落檔失敗時,記憶體歷史必須維持原狀。

    先換記憶體再吞掉例外的話:畫面宣告壓縮成功、in-memory 歷史已經被換掉,
    重開卻拿回完整的原始歷史 —— 使用者看到的與檔案裡的不是同一段對話,而且
    同一個錨點在這個行程裡不會再試一次。
    """
    class _FullDisk:
        def create(self):
            return "20260101T000000-abcdef01"

        def append(self, *_a, **_k):
            raise OSError("disk full")

    engine = engine_factory(store=_FullDisk())
    engine.messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]
    before = [dict(m) for m in engine.messages]
    with pytest.raises(client_engine.HistoryPersistError):
        engine.replace_history([{"role": "user", "content": "summary", "synthetic": True}])
    assert engine.messages == before
    assert engine.store_error is not None


def test_a_session_that_never_persisted_can_still_compact(engine_factory):
    """一開始就落不了檔的對話本來就只在記憶體裡,使用者也已經被警告過。"""
    class _FullDisk:
        def create(self):
            return "20260101T000000-abcdef01"

        def append(self, *_a, **_k):
            raise OSError("disk full")

    engine = engine_factory(store=_FullDisk())
    engine.store_error = "OSError: disk full"
    engine.replace_history([{"role": "user", "content": "summary", "synthetic": True}])
    assert engine.messages == [{"role": "user", "content": "summary", "synthetic": True}]


def test_a_round_that_never_produced_an_answer_is_not_a_completed_turn(engine_factory, monkeypatch):
    """`max_tool_steps` 用完的那則「已停止」訊息不是答案。

    沒有標記的話,狀態校正節錄會把它當成已完成回合,摘要器於是把仍待處理的
    事項寫成做完了。
    """
    import client_compaction

    mcp = FakeMcp()
    engine = engine_factory(mcp=mcp, max_tool_steps=2)
    monkeypatch.setattr(
        llama_client, "chat_completions",
        _stream(_tool_chunk("list_dir", '{"path": "."}', finish="tool_calls")),
    )
    engine.send("問題", on_event=lambda _e: None)
    assert client_compaction.completed_turns(engine.messages) == []


def test_a_truncated_answer_is_not_a_completed_turn(engine_factory, monkeypatch):
    """`finish_reason=length`:回答被 max_tokens 切掉,不是完整答案。"""
    import client_compaction

    engine = engine_factory()
    monkeypatch.setattr(
        llama_client, "chat_completions", _stream(_text_chunk("半句話", finish="length"))
    )
    engine.send("問題", on_event=lambda _e: None)
    assert client_compaction.completed_turns(engine.messages) == []


def test_the_summary_request_sees_the_pruned_history_not_the_raw_one(engine_factory):
    """被 prune 掉的舊工具輸出不得在摘要請求裡整份回到模型面前。

    一般 payload 與門檻估算看到的是 pruned 版本,摘要請求卻重新序列化原始
    歷史的話,摘要請求自己會撞 context gate、壓縮從此停用。
    """
    engine = engine_factory()
    huge = "SECRET-OLD-OUTPUT " * 14000
    engine.messages = [
        {"role": "user", "content": "q1"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": huge},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
        {"role": "assistant", "content": "a3"},
    ]
    payload, summary = engine.payload_messages()
    assert summary.get("pruned_tool_results")
    assert "SECRET-OLD-OUTPUT" not in json.dumps(payload, ensure_ascii=False)

    pruned = engine.prune_for_summary(engine.messages)
    assert "SECRET-OLD-OUTPUT" not in json.dumps(pruned, ensure_ascii=False)


# ============================================================
# 協作式取消(web /api/cancel、attach Ctrl-C 走這條)
# ============================================================
def test_cancel_stops_the_model_stream_without_recording_an_answer(engine_factory, monkeypatch):
    """沒有這條路徑,瀏覽器關掉分頁之後 backend 那一輪照樣跑到底。

    中斷不是答案:歷史裡不得多出一則 assistant 訊息(半句話會被下一輪當成上文)。
    """
    import threading
    import time

    engine = engine_factory()
    started = threading.Event()

    def _slow(**_kwargs):
        def _gen():
            started.set()
            for _ in range(500):
                yield _text_chunk("x")
                time.sleep(0.01)
            yield _text_chunk("", finish="stop")

        return _gen()

    monkeypatch.setattr(llama_client, "chat_completions", _slow)
    outcome: dict[str, object] = {}

    def _run():
        try:
            engine.send("問題", on_event=lambda _e: None)
        except client_engine.TurnCancelled:
            outcome["cancelled"] = True
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = repr(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    assert started.wait(5)
    assert engine.cancel() is True
    worker.join(5)
    assert outcome == {"cancelled": True}
    assert [m for m in engine.messages if m["role"] == "assistant"] == []
    assert engine.messages[-1]["role"] == "user"


def test_cancel_reaches_an_active_mcp_call_and_heals_the_history(engine_factory, monkeypatch):
    """進行中的 MCP 呼叫要走 client_mcp 的取消契約,不是等它自己跑完。

    中斷後歷史必須是 assistant(tool_calls) → tool(已中斷),下一輪才送得出去。
    """
    import threading

    class _Pending:
        def __init__(self, name):
            self.name = name
            self.event = threading.Event()
            self.cancelled = False

        def result(self):
            self.event.wait(5)
            raise client_mcp.McpCallCancelledError("cancelled")

        def cancel(self, reason=""):
            self.cancelled = True
            self.event.set()

    class _Mcp(FakeMcp):
        def __init__(self):
            super().__init__()
            self.pending: list[_Pending] = []

        def begin_call(self, name, arguments=None, **_kwargs):
            pending = _Pending(name)
            self.pending.append(pending)
            return pending

    mcp = _Mcp()
    engine = engine_factory(mcp=mcp)
    monkeypatch.setattr(
        llama_client, "chat_completions",
        _stream(_tool_chunk("list_dir", '{"path": "."}', finish="tool_calls")),
    )
    outcome: dict[str, object] = {}

    def _run():
        try:
            engine.send("問題", on_event=lambda _e: None)
        except client_engine.TurnCancelled:
            outcome["cancelled"] = True
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = repr(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    for _ in range(500):
        if mcp.pending:
            break
        threading.Event().wait(0.01)
    assert mcp.pending, "tool call never started"
    assert engine.cancel() is True
    worker.join(5)
    assert outcome == {"cancelled": True}
    assert mcp.pending[0].cancelled is True
    roles = [m["role"] for m in engine.messages]
    assert roles == ["user", "assistant", "tool"]
    assert engine.messages[-1]["content"] == client_engine.CANCELLED_TOOL_RESULT


def test_cancel_with_nothing_running_is_a_no_op(engine_factory):
    engine = engine_factory()
    assert engine.cancel() is False


# ============================================================
# 總審第 1 輪回修:取消契約的接縫、store_error、空白答案、fragment index
# ============================================================
def test_ctrl_c_during_a_tool_call_sends_the_cancel(engine_factory, monkeypatch):
    """終端 REPL 的 Ctrl-C 落在 `pending.result()` 時,也要對 MCP 送取消。

    改用 begin_call 之後,一步式 `McpClient.call()` 裡那段 KeyboardInterrupt →
    cancel 的處理就不在路徑上了;不補的話畫面說「已中斷」,ingest 還在寫 KB。
    """
    cancelled: list[str] = []

    class _Pending:
        def __init__(self, name):
            self.name = name

        def result(self):
            raise KeyboardInterrupt

        def cancel(self, reason=""):
            cancelled.append(reason)

    class _Mcp(FakeMcp):
        def begin_call(self, name, arguments=None, **_kwargs):
            return _Pending(name)

    engine = engine_factory(mcp=_Mcp())
    monkeypatch.setattr(
        llama_client, "chat_completions",
        _stream(_tool_chunk("list_dir", '{"path": "."}', finish="tool_calls")),
    )
    with pytest.raises(KeyboardInterrupt):
        engine.send("問題", on_event=lambda _e: None)
    assert cancelled == ["user interrupt"]
    assert engine.messages[-1]["content"] == client_engine.CANCELLED_TOOL_RESULT


def test_a_cancel_that_arrives_before_the_turn_starts_is_not_lost(engine_factory, monkeypatch):
    """web 的 cancel 可能在 worker 進到 send() 之前就到:旗標不得在 send() 開頭被清掉。

    web 走的是 `request_cancel(arm_when_idle=True)`(它自己管理回合邊界:worker 還沒進 send()
    時的取消先「武裝」,下一個 turn 一開始就中斷);`cancel()` 是給不管理邊界的呼叫端用的,
    閒置時是 no-op(第 8 輪:否則誤殺下一輪)。"""
    engine = engine_factory()
    monkeypatch.setattr(llama_client, "chat_completions", _stream(_text_chunk("x", finish="stop")))
    assert engine.request_cancel(arm_when_idle=True).accepted is True
    with pytest.raises(client_engine.TurnCancelled):
        engine.send("q", on_event=lambda _e: None)
    # 這一輪結束旗標就清掉;下一輪照常。
    result = engine.send("q2", on_event=lambda _e: None)
    assert result.finish == client_events.REASON_STOP


def test_cancel_while_waiting_for_approval_does_not_record_a_denial(engine_factory, monkeypatch):
    """等核准時被中斷:不是使用者拒絕了這個工具,不得記成一筆 denied。"""
    import threading

    engine = engine_factory()
    monkeypatch.setattr(
        llama_client, "chat_completions",
        _stream(_tool_chunk("apply_patch", '{"diff": "x"}', finish="tool_calls")),
    )
    waiting = threading.Event()
    release = threading.Event()

    def _approve(_request):
        waiting.set()
        release.wait(5)
        return False          # web 的 cancel 會把等待中的核准回成「拒絕」來喚醒

    outcome: dict[str, object] = {}

    def _run():
        try:
            engine.send("q", on_event=lambda _e: None, approve=_approve)
        except client_engine.TurnCancelled:
            outcome["cancelled"] = True

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    assert waiting.wait(5)
    engine.cancel()
    release.set()
    worker.join(5)
    assert outcome == {"cancelled": True}
    assert not any(m.get("tool_status") == client_events.STATUS_DENIED for m in engine.messages)
    assert engine.messages[-1]["content"] == client_engine.CANCELLED_TOOL_RESULT


def test_store_error_does_not_follow_into_the_next_session(engine_factory):
    """store_error 是每個 session 的事:換 session 不重設,新 session 的壓縮記錄會被靜默跳過。"""
    store = client_store.SessionStore(engine_factory.root)
    engine = engine_factory(store=store)
    engine.store_error = "OSError: old disk full"
    engine.new_session()
    assert engine.store_error is None
    engine.store_error = "OSError: old disk full"
    engine.resume(store.create())
    assert engine.store_error is None


def test_an_empty_answer_is_a_visible_error_not_a_silent_success(engine_factory, monkeypatch):
    """finish=stop 卻沒有文字也沒有工具呼叫:要講出來,而且不得成為壓縮的錨點。"""
    engine = engine_factory()
    monkeypatch.setattr(llama_client, "chat_completions", _stream(_text_chunk("", finish="stop")))
    events: list[dict] = []
    result = engine.send("q", on_event=events.append)
    assert result.finish == client_events.REASON_ERROR
    assert any(client_engine.EMPTY_ANSWER_MESSAGE in notice for notice in result.notices)
    assert engine.messages[-1]["tool_status"] == client_events.STATUS_ERROR
    finishes = [e for e in events if e["type"] == "step_finish"]
    assert finishes[-1]["part"]["reason"] == client_events.REASON_ERROR


def test_tool_call_fragments_without_an_index_join_the_last_call():
    """沒有 index 的 fragment 屬於最後一個 slot,不是一個新的呼叫。"""
    calls: dict = {}
    client_engine._merge_tool_call_delta(
        calls, {"index": 0, "id": "c1", "type": "function",
                "function": {"name": "list_dir", "arguments": ""}},
    )
    client_engine._merge_tool_call_delta(calls, {"function": {"arguments": '{"path":'}})
    client_engine._merge_tool_call_delta(calls, {"function": {"arguments": ' "."}'}})
    assert list(calls) == [0]
    assert calls[0]["name"] == "list_dir" and calls[0]["arguments"] == '{"path": "."}'


# ============================================================
# 總審第 2 輪回修:壓縮期間的取消、沒有 finish_reason 的串流、session 切換的交易性
# ============================================================
def test_cancel_during_the_summary_call_stops_it_and_clears_the_flag(engine_factory, monkeypatch):
    """壓縮是同一輪的尾巴:取消要接得住,而且旗標不得留到下一題。"""
    import threading
    import time

    engine = engine_factory()
    started = threading.Event()

    def _slow(**_kwargs):
        def _gen():
            started.set()
            for _ in range(500):
                yield _text_chunk("x")
                time.sleep(0.01)
            yield _text_chunk("", finish="stop")

        return _gen()

    monkeypatch.setattr(llama_client, "chat_completions", _slow)
    outcome: dict[str, object] = {}

    def _run():
        try:
            engine.complete([{"role": "user", "content": "摘要"}], source="compaction")
        except client_engine.TurnCancelled:
            outcome["cancelled"] = True

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    assert started.wait(5)
    assert engine.cancel() is True
    worker.join(5)
    assert outcome == {"cancelled": True}
    monkeypatch.setattr(llama_client, "chat_completions", _stream(_text_chunk("ok", finish="stop")))
    assert engine.send("下一題", on_event=lambda _e: None).finish == client_events.REASON_STOP


def test_complete_reports_the_finish_reason(engine_factory, monkeypatch):
    engine = engine_factory()
    monkeypatch.setattr(llama_client, "chat_completions", _stream(_text_chunk("## 目標", finish="length")))
    assert engine.complete([{"role": "user", "content": "摘要"}], source="compaction").finish == "length"
    monkeypatch.setattr(llama_client, "chat_completions", _stream(_text_chunk("半截")))
    assert engine.complete([{"role": "user", "content": "摘要"}], source="compaction").finish == ""


def test_a_stream_without_a_finish_reason_is_an_error_not_an_answer(engine_factory, monkeypatch):
    """clean EOF / 缺 [DONE] / 終結 chunk 沒到:半截的文字不是答案,不得以 stop 落檔。"""
    engine = engine_factory()
    monkeypatch.setattr(llama_client, "chat_completions", _stream(_text_chunk("partial transport body")))
    events: list[dict] = []
    result = engine.send("q", on_event=events.append)
    assert result.finish == client_events.REASON_ERROR
    assert any(client_engine.TRUNCATED_STREAM_MESSAGE in n for n in result.notices)
    assert engine.messages[-1]["tool_status"] == client_events.STATUS_ERROR
    assert [e for e in events if e["type"] == "step_finish"][-1]["part"]["reason"] == client_events.REASON_ERROR


def test_a_truncated_tool_call_stream_never_executes_half_arguments(engine_factory, monkeypatch):
    mcp = FakeMcp()
    engine = engine_factory(mcp=mcp)
    monkeypatch.setattr(llama_client, "chat_completions", _stream(_tool_chunk("list_dir", '{"path": "/ha')))
    result = engine.send("q", on_event=lambda _e: None)
    assert mcp.calls == []
    assert result.finish == client_events.REASON_ERROR


class _FlakyStore:
    """第一次 create(建 engine)成功,之後失敗。"""

    def __init__(self):
        self.calls = 0

    def create(self, *_a, **_k):
        self.calls += 1
        if self.calls > 1:
            raise OSError("disk full")
        return "20260101T000000-abcdef01"

    def append(self, *_a, **_k):
        return None

    def read(self, _session_id):
        return [{"type": "compaction", "history": ["not a dict"]}]


def test_a_failed_new_session_keeps_the_current_conversation(engine_factory):
    engine = engine_factory(store=_FlakyStore())
    engine.messages = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    before, session = list(engine.messages), engine.session_id
    with pytest.raises(OSError):
        engine.new_session()
    assert engine.messages == before and engine.session_id == session


def test_load_session_leaves_the_engine_untouched_and_adopt_switches_atomically(engine_factory):
    """讀一段對話**不得**動到 engine;換過去是 adopt 的一次原子動作。

    畫面要先拿 transcript 把 widget 建好,建不出來就整個不換。讀取當場就換掉
    engine 的話,失敗會留下「engine 已經在新對話、畫面還是舊那段」的狀態:使用者
    對著舊畫面問下一題,那一題被寫進另一段對話,而畫面上完全看不出來。
    """
    store = client_store.SessionStore(engine_factory.root)
    engine = engine_factory(store=store)
    other = store.create()
    store.append(other, {"type": "message", "role": "user", "content": "另一段的問題"})
    store.append(other, {"type": "message", "role": "assistant", "content": "另一段的回答"})
    engine.messages = [{"role": "user", "content": "現在這一段"}]
    engine.store_error = "OSError: earlier"
    current = engine.session_id

    snapshot = engine.load_session(other)
    assert engine.session_id == current
    assert engine.messages == [{"role": "user", "content": "現在這一段"}]
    assert engine.store_error == "OSError: earlier"
    assert engine.resumed_snapshot is None
    assert [m["content"] for m in snapshot.messages] == ["另一段的問題", "另一段的回答"]
    assert [m["content"] for m in snapshot.transcript] == ["另一段的問題", "另一段的回答"]
    assert snapshot.session_id == other and snapshot.compactions == 0

    engine.adopt(snapshot)
    assert engine.session_id == other
    assert [m["content"] for m in engine.messages] == ["另一段的問題", "另一段的回答"]
    # store_error 是**這個** session 的事:上一段寫不進去不代表這一段也寫不進去。
    assert engine.store_error is None
    assert engine.resumed_snapshot is snapshot
    # 換上來的是複本:之後的對話不得回頭改到畫面還在用的那一份。
    engine.messages[0]["content"] = "被改過"
    assert snapshot.messages[0]["content"] == "另一段的問題"
    # resume = load + adopt,而且把快照交出去(啟動時接續的畫面要重播它)。
    assert engine.resume(other).transcript == snapshot.transcript


def test_the_snapshot_model_history_is_compacted_while_the_transcript_keeps_the_originals(
    engine_factory,
):
    """同一次讀取要產出兩份:模型看壓縮後的、畫面看壓縮前的原文。

    模型那一份不尊重壓縮切點的話,重開一個壓縮過的對話會把整段原始歷史再吃回
    context(壓縮等於白做);畫面那一份若也只剩摘要,使用者就再也調不出壓縮前
    問過什麼、工具回了什麼 —— 而那段對話明明還在檔案裡。壓縮本身只以一個標記
    呈現:把 tail 再畫一次的話,同一段問答在畫面上會出現兩次。
    """
    import client_compaction

    store = client_store.SessionStore(engine_factory.root)
    engine = engine_factory(store=store)
    session = engine.session_id
    originals = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]
    for message in originals:
        store.append(session, {"type": "message", **message})
    engine.messages = [dict(message) for message in originals]
    # 真的壓一次:replace_history 寫的就是 runtime 會寫進 session 檔的那一筆。
    engine.replace_history(
        [
            {
                "role": "user",
                "content": f"{client_compaction.SUMMARY_PREFIX}\n這段對話的摘要",
                "synthetic": True,
            },
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ]
    )
    store.append(session, {"type": "message", "role": "user", "content": "q3"})

    snapshot = engine.load_session(session)
    assert [m.get("content") for m in snapshot.messages] == [
        f"{client_compaction.SUMMARY_PREFIX}\n這段對話的摘要", "q2", "a2", "q3",
    ]
    assert [r.get("type") or r.get("role") for r in snapshot.transcript] == [
        "user", "assistant", "user", "assistant", "compaction", "user",
    ]
    marker = snapshot.transcript[4]
    assert marker["summary"] == "這段對話的摘要"
    # dropped = 當時的模型歷史長度 − 逐字保留的則數,與 Compactor._replace 的 len(head) 同數。
    assert marker["dropped"] == 2 and marker["kept"] == 2
    assert snapshot.compactions == 1
    # tail 不重畫:q2 / a2 在畫面歷史裡各只出現一次。
    contents = [r.get("content") for r in snapshot.transcript]
    assert contents.count("q2") == 1 and contents.count("a2") == 1


def test_a_malformed_compaction_record_does_not_half_switch_the_session(engine_factory):
    """id 換了、messages 還是舊對話:下一題會把舊對話寫進新 session。"""
    engine = engine_factory(store=_FlakyStore())
    engine.messages = [{"role": "user", "content": "private context"}]
    engine.store_error = "OSError: earlier"
    session = engine.session_id
    with pytest.raises(ValueError):
        engine.resume("20260101T000000-bbbbbbbb")
    assert engine.session_id == session
    assert engine.messages == [{"role": "user", "content": "private context"}]
    assert engine.store_error == "OSError: earlier"


# ── 總審第 3 輪回修(F3-1 / F3-2):等鎖期間的取消、length / malformed 串流 ──

@pytest.mark.smoke
def test_a_cancel_while_waiting_for_the_model_lock_never_sends_the_request(engine_factory, monkeypatch):
    """等共用 model_lock 期間被取消:拿到鎖之後不得再發 HTTP 請求(連第一個 chunk 都不該等)。"""
    engine = engine_factory()
    started: list[dict] = []

    def _chat(**kwargs):
        started.append(kwargs)
        return iter([_text_chunk("late", finish="stop")])

    monkeypatch.setattr(llama_client, "chat_completions", _chat)
    outcome: dict = {}

    def _worker():
        try:
            engine.send("q", on_event=lambda _e: None)
        except client_engine.TurnCancelled:
            outcome["cancelled"] = True

    engine.model_lock.acquire()
    try:
        thread = threading.Thread(target=_worker)
        thread.start()
        time.sleep(0.2)                      # worker 現在卡在 model_lock 上
        engine.cancel()
    finally:
        engine.model_lock.release()
    thread.join(5)
    assert started == []                     # 拿到鎖之後沒有再發請求
    assert outcome == {"cancelled": True}


@pytest.mark.smoke
def test_a_length_cut_tool_call_is_never_executed(engine_factory, monkeypatch):
    """finish_reason=length 的 tool call:參數可能被 max_tokens 切到一半,不得執行、不得當成答案。"""
    mcp = FakeMcp()
    engine = engine_factory(mcp=mcp)
    monkeypatch.setattr(
        llama_client, "chat_completions",
        _stream(_tool_chunk("list_dir", '{"path": "."}', finish="length")),
    )
    result = engine.send("q", on_event=lambda _e: None)
    assert mcp.calls == []
    assert result.finish != client_events.REASON_STOP
    assert client_engine.LENGTH_CUT_MESSAGE in result.notices
    assert engine.messages[-1]["tool_status"] == client_events.STATUS_ERROR
    assert not engine.messages[-1].get("tool_calls")


@pytest.mark.smoke
def test_a_malformed_sse_line_makes_the_answer_an_error_even_if_stop_follows(engine_factory, monkeypatch):
    """SSE 中間一行壞 JSON 以前被靜默略過:後面照樣收到 stop,缺字的回答就被當成完整的。"""
    engine = engine_factory()

    def _broken(**_kwargs):
        yield _text_chunk("first half")
        raise llama_client.StreamProtocolError("malformed SSE payload (11 bytes): 'data: {oops'")

    monkeypatch.setattr(llama_client, "chat_completions", lambda **kw: _broken(**kw))
    result = engine.send("q", on_event=lambda _e: None)
    assert result.finish == client_events.REASON_ERROR
    assert any("malformed SSE payload" in n for n in result.notices)
    assert engine.messages[-1]["tool_status"] == client_events.STATUS_ERROR


@pytest.mark.smoke
def test_the_openai_stream_iterator_refuses_a_malformed_line():
    """`data:` 行解析不了就要丟出來,不是 continue:之後的 stop 會把缺字的回答升成成功。"""

    class _Resp:
        def iter_lines(self):
            yield b'data: {"choices": []}'
            yield b'data: {"choices": [oops'
            yield b"data: [DONE]"

        def close(self):
            return None

    with pytest.raises(llama_client.StreamProtocolError):
        list(llama_client._iter_openai_stream(_Resp()))


@pytest.mark.smoke
def test_a_malformed_stream_in_a_summary_request_reads_as_unfinished(engine_factory, monkeypatch):
    """摘要器的請求中間掉了一塊:finish 必須是空的(沒寫完),壓縮層才會停用而不是採用半份摘要。"""
    engine = engine_factory()

    def _broken(**_kwargs):
        yield _text_chunk("摘要前半")
        raise llama_client.StreamProtocolError("malformed SSE payload")

    monkeypatch.setattr(llama_client, "chat_completions", lambda **kw: _broken(**kw))
    completion = engine.complete([{"role": "user", "content": "summarise"}], source="test")
    assert completion.finish == ""


# ── 總審第 4 輪回修(F4-1):取消要能叫醒卡在 recv 的串流;慢速取消不在快速路徑 ──

class _BlockingStream:
    """模擬 requests 串流:__next__ 阻塞到 close() 被(別的執行緒)呼叫。"""

    def __init__(self):
        self.released = threading.Event()
        self.closed_from: list[str] = []
        self._first = True

    def __iter__(self):
        return self

    def __next__(self):
        if self._first:
            self._first = False
            return _text_chunk("first")
        self.released.wait(5)          # 這裡對應 recv() 阻塞
        raise StopIteration            # socket shutdown 之後 iter_lines 看到的是 EOF

    def close(self):
        self.closed_from.append(threading.current_thread().name)
        self.released.set()


@pytest.mark.smoke
def test_cancel_unblocks_a_stream_stuck_waiting_for_the_next_line(engine_factory, monkeypatch):
    """串流卡在等下一行時取消:要關掉底層連線把它叫醒,而且醒來後是「中斷」不是「截斷」。
    以前 cancel() 對正在另一個執行緒跑的 generator 呼叫 close(),得到
    ValueError: generator already executing 被吞掉,連線沒關,要等到 600 秒 timeout。"""
    engine = engine_factory()
    stream = _BlockingStream()
    monkeypatch.setattr(llama_client, "chat_completions", lambda **_kw: stream)
    outcome: dict = {}

    def _worker():
        try:
            outcome["result"] = engine.send("q", on_event=lambda _e: None)
        except client_engine.TurnCancelled:
            outcome["cancelled"] = True

    thread = threading.Thread(target=_worker, name="turn-worker")
    thread.start()
    for _ in range(200):                # 等 worker 收到第一個 chunk、卡在第二個
        if not stream._first:
            break
        time.sleep(0.01)
    time.sleep(0.05)
    started = time.monotonic()
    engine.cancel()
    thread.join(5)
    assert not thread.is_alive()
    assert time.monotonic() - started < 2
    assert "MainThread" in stream.closed_from          # 從取消端關的
    assert outcome == {"cancelled": True}              # 不是 TRUNCATED 錯誤
    assert engine.messages[-1]["role"] == "user"       # 中斷不是答案:沒有多出 assistant


@pytest.mark.smoke
def test_the_stream_handle_shuts_the_socket_down_without_touching_the_generator():
    """OpenAIStream.close() 從別的執行緒呼叫:關 socket(shutdown)而不是 generator.close()。"""

    class _Sock:
        def __init__(self):
            self.calls: list = []
            self.eof = threading.Event()

        def shutdown(self, how):
            self.calls.append(how)
            self.eof.set()

    class _Resp:
        def __init__(self, sock):
            self._sock = sock
            self.raw = type("Raw", (), {})()
            fp = type("Fp", (), {})()
            fp.fp = type("Buffered", (), {})()
            fp.fp.raw = type("SockIO", (), {})()
            fp.fp.raw._sock = sock
            self.raw._fp = fp
            self.closed = False

        def iter_lines(self):
            yield b'data: {"choices": [{"delta": {"content": "a"}}]}'
            self._sock.eof.wait(5)     # recv 阻塞,shutdown 才醒
            return

        def close(self):
            self.closed = True

    sock = _Sock()
    handle = llama_client.OpenAIStream(_Resp(sock))
    seen: list = []
    reader = threading.Thread(target=lambda: seen.extend(handle))
    reader.start()
    for _ in range(200):
        if seen:
            break
        time.sleep(0.01)
    handle.close()                     # 另一個執行緒,generator 正在執行中
    reader.join(5)
    assert not reader.is_alive()
    assert sock.calls == [socket.SHUT_RDWR]
    assert handle._resp.closed is True


@pytest.mark.smoke
def test_request_cancel_never_waits_for_the_mcp_grace_period(engine_factory):
    """快速路徑(request_cancel)只設旗標、關串流、交回 pending call;等寬限期的
    MCP 取消是 cancel_pending 的事,呼叫端可以在自己的鎖外做。"""
    engine = engine_factory()
    blocked = threading.Event()

    class _SlowCall:
        def __init__(self):
            self.cancelled: list[str] = []

        def cancel(self, reason):
            self.cancelled.append(reason)
            blocked.wait(5)            # 模擬 cancel_grace

    call = _SlowCall()
    engine._begin_turn()                  # 有進行中的一輪,request_cancel 才會接受
    with engine._active_lock:
        engine._active_call = call
    started = time.monotonic()
    decision = engine.request_cancel()
    assert time.monotonic() - started < 0.5
    assert decision.accepted is True and decision.call is call and call.cancelled == []
    assert engine._cancel.is_set()
    blocked.set()
    assert engine.cancel_pending(decision.call) is True
    assert call.cancelled == ["cancelled by user"]
    engine._end_turn()


# ── 總審第 5 輪回修(F5-1):取消先於 response handle 的兩段時序 ──

@pytest.mark.smoke
def test_a_cancel_while_waiting_for_the_response_headers_ends_the_turn_promptly(engine_factory, monkeypatch):
    """llama-server 的 slot 被佔著、headers 遲遲不來:取消要立刻結束這一輪(worker 不再等),
    但 model lock **跟著被放棄的請求走**——等 response 真的到了、由背景把它關掉之後才放鎖
    (下一輪在鎖上排隊,不會跟舊請求重疊)。以前是整個 worker 卡到 600 秒 timeout。"""
    engine = engine_factory()
    headers = threading.Event()
    late = _BlockingStream()

    def _slow_post(**_kwargs):
        headers.wait(10)                 # 對應 session.post() 等 response headers
        return late

    monkeypatch.setattr(llama_client, "chat_completions", _slow_post)
    outcome: dict = {}

    def _worker():
        try:
            engine.send("q", on_event=lambda _e: None)
        except client_engine.TurnCancelled:
            outcome["cancelled"] = True

    thread = threading.Thread(target=_worker)
    thread.start()
    time.sleep(0.2)                      # worker 現在卡在等 headers
    started = time.monotonic()
    engine.cancel()
    thread.join(5)
    assert not thread.is_alive() and time.monotonic() - started < 2
    assert outcome == {"cancelled": True}
    # model lock 跟著被放棄的請求走:舊請求還在 llama-server 的 queue 裡,鎖不能先放
    # (第 6 輪:放了就會跟下一輪重疊,單 slot 的序列化被取消打破)。
    assert engine.model_lock.acquire(timeout=0.3) is False
    waiters = [t for t in threading.enumerate() if t.name == "codetrail-abandon"]
    assert len(waiters) == 1             # 一次取消只建一個 waiter
    headers.set()                        # response 晚到:必須被關掉,不留一個沒人收的串流
    for _ in range(200):
        if late.closed_from:
            break
        time.sleep(0.01)
    assert len(late.closed_from) == 1    # 關一次,不是兩次
    assert engine.model_lock.acquire(timeout=2)   # 舊請求結束之後鎖才放
    engine.model_lock.release()


@pytest.mark.smoke
def test_a_cancelled_request_never_overlaps_the_next_turn(engine_factory, monkeypatch):
    """取消後立刻送下一題:新請求必須排在被放棄的舊請求**之後**(同一把 model lock),
    不能在舊請求還 in flight 時就發出去——llama-server 單 slot,重疊等於排隊兩次。"""
    engine = engine_factory()
    headers = threading.Event()
    requests_seen: list[float] = []
    late = _BlockingStream()

    def _post(**_kwargs):
        requests_seen.append(time.monotonic())
        if len(requests_seen) == 1:
            headers.wait(10)             # 第一個請求:headers 遲遲不來
            return late
        return iter([_text_chunk("second", finish="stop")])

    monkeypatch.setattr(llama_client, "chat_completions", _post)
    first = threading.Thread(target=lambda: _swallow_cancel(engine, "first"))
    first.start()
    time.sleep(0.2)
    engine.cancel()
    first.join(5)
    assert not first.is_alive()
    engine.clear_cancel()
    second_result: dict = {}
    second = threading.Thread(target=lambda: second_result.setdefault("r", engine.send("second", on_event=lambda _e: None)))
    second.start()
    time.sleep(0.3)
    assert len(requests_seen) == 1       # 第二題還在 model lock 上排隊,沒有發出去
    assert second.is_alive()
    headers.set()                        # 舊請求結束(被背景關掉)→ 鎖放 → 第二題才發
    second.join(5)
    assert not second.is_alive()
    assert len(requests_seen) == 2
    assert requests_seen[1] > requests_seen[0]
    assert len(late.closed_from) == 1
    assert second_result["r"].finish == client_events.REASON_STOP


def _swallow_cancel(engine, text):
    try:
        engine.send(text, on_event=lambda _e: None)
    except client_engine.TurnCancelled:
        pass


@pytest.mark.smoke
def test_a_cancel_that_lands_before_the_handle_is_registered_closes_it_without_reading(engine_factory, monkeypatch):
    """取消落在「拿到 handle、登記為 active 之前」:當時沒有 socket 可關,所以拿到 handle
    之後必須自己關掉、直接結束,不能再進第一個 next() 卡一次。"""
    engine = engine_factory()
    stream = _BlockingStream()
    stream._first = False                # 第一個 next() 就阻塞

    def _post_then_cancelled(**_kwargs):
        engine._cancel.set()             # 取消發生在 handle 回來之前
        return stream

    monkeypatch.setattr(llama_client, "chat_completions", _post_then_cancelled)
    started = time.monotonic()
    with pytest.raises(client_engine.TurnCancelled):
        engine.send("q", on_event=lambda _e: None)
    assert time.monotonic() - started < 2
    assert stream.closed_from            # handle 被關掉,不是留給 600 秒 timeout
    assert engine._active_stream is None


# ── 總審第 7 輪回修(F7-1):排在被放棄請求後面、等 leased model lock 的那一輪也要能取消 ──

@pytest.mark.smoke
def test_a_turn_waiting_for_a_leased_model_lock_can_still_be_cancelled(engine_factory, monkeypatch):
    """取消 → 立刻重送 → 發現還在排隊 → 再按取消:第二輪卡在 model lock 的 acquire() 上,
    沒有 stream / MCP call 可關;以前只能等舊請求回 headers 或 600 秒 timeout。"""
    engine = engine_factory()
    headers = threading.Event()
    requests_seen: list[int] = []
    late = _BlockingStream()

    def _post(**_kwargs):
        requests_seen.append(1)
        headers.wait(10)
        return late

    monkeypatch.setattr(llama_client, "chat_completions", _post)
    first = threading.Thread(target=lambda: _swallow_cancel(engine, "first"))
    first.start()
    time.sleep(0.2)
    engine.cancel()
    first.join(5)
    assert not first.is_alive()
    engine.clear_cancel()
    outcome: dict = {}

    def _second():
        try:
            engine.send("second", on_event=lambda _e: None)
        except client_engine.TurnCancelled:
            outcome["cancelled"] = True

    second = threading.Thread(target=_second)
    second.start()
    time.sleep(0.3)
    assert second.is_alive() and len(requests_seen) == 1     # 第二輪在 leased lock 上排隊
    started = time.monotonic()
    assert engine.cancel() is True                           # 一輪進行中:取消算數
    second.join(5)
    assert not second.is_alive() and time.monotonic() - started < 2
    assert outcome == {"cancelled": True}
    assert len(requests_seen) == 1                           # 第二輪從沒發出請求
    assert engine.model_lock.acquire(timeout=0.3) is False   # 舊請求仍租著鎖
    headers.set()                                            # 舊請求結束 → 關串流 → 放鎖
    for _ in range(200):
        if late.closed_from:
            break
        time.sleep(0.01)
    assert len(late.closed_from) == 1
    assert engine.model_lock.acquire(timeout=2)
    engine.model_lock.release()


# ── 總審第 8 輪回修(F8-1):Engine.cancel() 與回合收尾互斥;閒置時取消是 no-op ──

@pytest.mark.smoke
def test_cancelling_an_idle_engine_is_a_no_op_that_never_poisons_the_next_turn(engine_factory, monkeypatch):
    """閒置時呼叫 cancel():回 False、**不設旗標**;下一題正常回答。以前旗標無條件設下,
    下一題連請求都沒發就被 TurnCancelled,而新問題已經寫進 session 檔沒有回答。"""
    engine = engine_factory()
    monkeypatch.setattr(llama_client, "chat_completions", _stream(_text_chunk("answer", finish="stop")))
    assert engine.cancel() is False
    assert not engine._cancel.is_set()
    result = engine.send("q", on_event=lambda _e: None)
    assert result.finish == client_events.REASON_STOP
    assert engine.messages[-1]["role"] == "assistant"


@pytest.mark.smoke
def test_cancel_racing_the_natural_end_of_a_turn_never_poisons_the_next_one(engine_factory, monkeypatch):
    """一輪正自然結束時另一個執行緒呼叫 cancel():兩邊必須互斥——收尾先做,cancel() 就看到
    「沒有進行中的一輪」回 False;不得出現「先讀到進行中、收尾清完旗標、再補設」的交錯。"""
    engine = engine_factory()
    monkeypatch.setattr(llama_client, "chat_completions", _stream(_text_chunk("answer", finish="stop")))
    real_lock = engine._turn_state
    entered = threading.Event()
    proceed = threading.Event()
    worker_name = "turn-worker"

    class _Probe:
        """worker 收尾(遞減)進臨界區時停住,讓 cancel() 排在後面。"""

        def __enter__(self):
            real_lock.acquire()
            if threading.current_thread().name == worker_name and any(
                f.name == "_end_turn" for f in traceback.extract_stack()
            ) and not entered.is_set():
                # 只在「收尾那一次」停(run_tool_loop 的 _end_turn;答案此時已寫定)。
                entered.set()
                proceed.wait(5)
            return self

        def __exit__(self, *_exc):
            real_lock.release()
            return False

    engine._turn_state = _Probe()
    outcome: dict = {}
    worker = threading.Thread(
        target=lambda: outcome.setdefault("first", engine.send("q1", on_event=lambda _e: None)),
        name=worker_name,
    )
    worker.start()
    assert entered.wait(5)                     # worker 正在收尾的臨界區裡
    results: list[bool] = []
    canceller = threading.Thread(target=lambda: results.append(engine.cancel()))
    canceller.start()
    time.sleep(0.05)                           # cancel() 現在卡在 turn-state 鎖上
    proceed.set()
    worker.join(5)
    canceller.join(5)
    assert outcome["first"].finish == client_events.REASON_STOP
    assert results == [False]                  # 收尾已在進行:取消是 no-op
    assert not engine._cancel.is_set()         # 旗標沒有留給下一題
    second = engine.send("q2", on_event=lambda _e: None)
    assert second.finish == client_events.REASON_STOP


# ── 總審第 9 輪回修(F9-1 / F9-2):cancel() 的線性化點 = 答案寫入歷史 ──

@pytest.mark.smoke
def test_a_cancel_after_the_answer_is_committed_is_refused_and_the_turn_ends_as_stop(engine_factory, monkeypatch):
    """答案已寫進歷史、還沒收尾時 cancel():必須回 False、不設旗標,結果是 stop。
    以前回 True 卻保留答案(web 也跟著送 step_finish(stop)):兩邊都不對。"""
    engine = engine_factory()
    monkeypatch.setattr(llama_client, "chat_completions", _stream(_text_chunk("answer", finish="stop")))
    real_lock = engine._turn_state
    committed = threading.Event()
    proceed = threading.Event()

    class _Probe:
        def __enter__(self):
            real_lock.acquire()
            return self

        def __exit__(self, *_exc):
            real_lock.release()
            # 答案剛寫定(這一次離開臨界區時 _turn_completed 已是 True):停在收尾之前,
            # 讓 cancel() 在這個空窗進來。
            if engine._turn_completed and not committed.is_set():
                committed.set()
                proceed.wait(5)
            return False

    engine._turn_state = _Probe()
    outcome: dict = {}
    worker = threading.Thread(target=lambda: outcome.setdefault("r", engine.send("q", on_event=lambda _e: None)))
    worker.start()
    assert committed.wait(5)
    assert engine.cancel() is False            # 答案已寫定:取消不算數
    assert not engine._cancel.is_set()
    proceed.set()
    worker.join(5)
    assert outcome["r"].finish == client_events.REASON_STOP
    assert engine.messages[-1]["role"] == "assistant"


@pytest.mark.smoke
def test_a_cancel_before_the_answer_is_committed_wins_and_nothing_is_recorded(engine_factory, monkeypatch):
    """取消先於「答案寫入歷史」:這一輪以 TurnCancelled 結束,歷史**不得**多出 assistant
    (中斷不是答案),cancel() 回 True。"""
    engine = engine_factory()
    cancelled = threading.Event()

    def _chat(**_kwargs):
        yield _text_chunk("answ")
        yield _text_chunk("er", finish="stop")

    monkeypatch.setattr(llama_client, "chat_completions", lambda **kw: _chat(**kw))
    results: list[bool] = []

    def _on_text(_token):
        # 串流中(答案還沒寫定):讓 cancel() 先完成再繼續。
        if not cancelled.is_set():
            results.append(engine.cancel())
            cancelled.set()

    with pytest.raises(client_engine.TurnCancelled):
        engine.send("q", on_event=lambda _e: None, on_text=_on_text)
    assert results == [True]
    assert [m["role"] for m in engine.messages] == ["user"]     # 沒有 assistant
    assert not engine._cancel.is_set()                          # 收尾清掉了


@pytest.mark.smoke
def test_a_cancel_while_the_user_message_is_being_persisted_is_honoured(engine_factory, monkeypatch):
    """send() 已開始(user 訊息在寫 session 檔)時 cancel():要算數——回 True、模型請求不發。
    以前 _in_turn 在 run_tool_loop 才加,這段空窗的取消是 no-op,模型請求照發。"""
    blocked = threading.Event()
    reached = threading.Event()

    class _SlowStore(client_store.EphemeralSessionStore):
        def append(self, session_id, record):
            if record.get("role") == "user":
                reached.set()
                blocked.wait(5)
            return super().append(session_id, record)

    engine = engine_factory(store=_SlowStore(engine_factory.root))
    requests: list[int] = []

    def _chat(**_kwargs):
        requests.append(1)
        return iter([_text_chunk("late", finish="stop")])

    monkeypatch.setattr(llama_client, "chat_completions", _chat)
    outcome: dict = {}

    def _worker():
        try:
            engine.send("q", on_event=lambda _e: None)
        except client_engine.TurnCancelled:
            outcome["cancelled"] = True

    worker = threading.Thread(target=_worker)
    worker.start()
    assert reached.wait(5)                      # 卡在寫 user 訊息
    assert engine.cancel() is True
    blocked.set()
    worker.join(5)
    assert outcome == {"cancelled": True}
    assert requests == []                       # 模型請求沒發


# ── 總審第 10 輪回修(F10-1 / F10-3 / F10-4):取消由 engine 原子決定;所有最後一則都走線性化點;鎖內不做 I/O ──

@pytest.mark.smoke
def test_a_cancel_during_the_max_step_wrap_up_records_nothing(engine_factory, monkeypatch):
    """max_tool_steps 耗盡的收尾也是「這一輪的最後一則」:取消先到就不寫那則 assistant error,
    這一輪以 TurnCancelled 結束。以前那個分支直接 _record,cancel 回 True 之後歷史還是多一則。"""
    results: list[bool] = []

    class _CancelDuringTool(FakeMcp):
        def call(self, name, arguments=None, **kwargs):
            results.append(engine.cancel())    # 工具跑到一半:取消先到
            return super().call(name, arguments, **kwargs)

    mcp = _CancelDuringTool()
    engine = engine_factory(mcp=mcp, max_tool_steps=1)
    monkeypatch.setattr(llama_client, "chat_completions",
                        _stream(_tool_chunk("list_dir", '{"path": "."}', finish="tool_calls")))
    with pytest.raises(client_engine.TurnCancelled):
        engine.send("q", on_event=lambda _e: None)
    assert results == [True]
    roles = [m["role"] for m in engine.messages]
    assert roles[-1] != "assistant" or engine.messages[-1].get("tool_calls")   # 沒有多出收尾那則
    assert not any((m.get("content") or "").startswith("[已停止]") for m in engine.messages if m["role"] == "assistant")


@pytest.mark.smoke
def test_the_final_commit_persists_outside_the_turn_state_lock(engine_factory, monkeypatch):
    """答案落檔(store append + fsync)不得在 turn-state 鎖內:否則 web 的 app lock 會跟著等 fsync。
    落檔期間鎖必須可取得、cancel() 立刻回 False(答案已決定寫定)。"""
    blocked = threading.Event()
    reached = threading.Event()

    class _SlowStore(client_store.EphemeralSessionStore):
        def append(self, session_id, record):
            if record.get("role") == "assistant":
                reached.set()
                blocked.wait(5)
            return super().append(session_id, record)

    engine = engine_factory(store=_SlowStore(engine_factory.root))
    monkeypatch.setattr(llama_client, "chat_completions", _stream(_text_chunk("answer", finish="stop")))
    outcome: dict = {}
    worker = threading.Thread(target=lambda: outcome.setdefault("r", engine.send("q", on_event=lambda _e: None)))
    worker.start()
    assert reached.wait(5)                                   # 正在落檔
    assert engine._turn_state.acquire(timeout=0.3)           # 鎖沒被扣住
    engine._turn_state.release()
    started = time.monotonic()
    assert engine.cancel() is False                          # 已決定寫定:立刻拒絕
    assert time.monotonic() - started < 0.5
    blocked.set()
    worker.join(5)
    assert outcome["r"].finish == client_events.REASON_STOP


@pytest.mark.smoke
def test_a_web_style_cancel_in_the_idle_gap_after_a_commit_is_refused(engine_factory, monkeypatch):
    """web 的 request_cancel(arm_when_idle=True):worker 還沒進 send() → 預先武裝(接受);
    答案已寫定、壓縮還沒開始(engine 閒置)→ 拒絕;web 收尾 clear_cancel() 之後又可以武裝。
    以前 web 自己 snapshot engine 狀態,跟 commit 不是原子,會「取消成功卻保留答案」。"""
    engine = engine_factory()
    monkeypatch.setattr(llama_client, "chat_completions", _stream(_text_chunk("answer", finish="stop")))
    result = engine.send("q1", on_event=lambda _e: None)
    assert result.finish == client_events.REASON_STOP
    assert engine.request_cancel(arm_when_idle=True).accepted is False   # 答案已寫定、閒置:拒絕
    assert not engine._cancel.is_set()
    engine.clear_cancel()                                                # web 的回合結束
    assert engine.request_cancel(arm_when_idle=True).accepted is True    # 新回合、worker 還沒進來:武裝
    with pytest.raises(client_engine.TurnCancelled):
        engine.send("q2", on_event=lambda _e: None)
    assert [m["role"] for m in engine.messages][-1] == "user"            # q2 沒有答案
    assert engine.request_cancel().accepted is False                     # 非 web 呼叫端閒置一律拒絕


# ── 總審第 11 輪回修(F11-2):send() 異常退出後的閒置空窗不是 prestart ──

@pytest.mark.smoke
def test_a_web_style_cancel_after_a_failed_turn_is_refused(engine_factory, monkeypatch):
    """send() 因例外退出(engine 閒置、什麼都沒寫定)、web 還沒收尾:這個空窗的取消要拒絕——
    沒有東西可取消,web 會照原結果送 error。以前「閒置且沒寫定」一律當 prestart 武裝,
    /api/cancel 回 True 之後 terminal 卻是 error。"""
    engine = engine_factory()

    def _boom(**_kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr(llama_client, "chat_completions", _boom)
    with pytest.raises(RuntimeError):
        engine.send("q", on_event=lambda _e: None)
    assert engine.request_cancel(arm_when_idle=True).accepted is False   # 這個回合 engine 已開始過 turn
    assert not engine._armed and not engine._cancel.is_set()
    engine.clear_cancel()                                                # web 收尾 → 新回合
    assert engine.request_cancel(arm_when_idle=True).accepted is True    # 真正的 prestart 才武裝


# ── 2026-09-04 真實使用踩到:llama-server 的 SSE keep-alive 註解行(`:`)被當成壞掉的 payload ──

@pytest.mark.smoke
def test_the_openai_stream_iterator_ignores_sse_comment_and_field_lines():
    """SSE 規格:以 `:` 開頭的是註解(llama-server 處理長 prompt 時送 keep-alive),`event:` /
    `id:` / `retry:` 是欄位,都不是 payload——只有 `data:` 行才是 JSON。第 4 輪的 fail-loud
    把這些行也拿去 json.loads,第一個長 prompt 的回合就被判成「串流沒有正常結束」。"""

    class _Resp:
        def iter_lines(self):
            yield b":"
            yield b": keep-alive"
            yield b"event: message"
            yield b"id: 7"
            yield b"retry: 3000"
            yield b'data: {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}'
            yield b"data: [DONE]"

        def close(self):
            return None

    chunks = list(llama_client._iter_openai_stream(_Resp()))
    assert len(chunks) == 1 and chunks[0]["choices"][0]["delta"]["content"] == "ok"


@pytest.mark.smoke
def test_external_import_is_gated_behind_a_per_call_approval():
    """`import_external_file` 每一次都要人工核准。

    `client.json` 的 `external_import` 把授權從「單次啟動」變成**跨專案持久**,
    所以實際動作必須逐次確認 —— 而且核准框要顯示來源與目的路徑。少了這道閘,
    開關一開就等於「模型可以把專案外的任何白名單檔複製進沙箱」,使用者只會在
    事後從檔案系統發現。
    """
    import client_policy

    assert "import_external_file" in client_policy.ASK_TOOLS
    policy = client_policy.InteractivePolicy()
    assert policy.decide(
        "import_external_file", read_only=False, arguments={"path": "~/Downloads/x.png"}
    ) is client_policy.Decision.ASK


# ── 總審 F1-6:`import_external_file` 的核准不得被 client.json 覆寫掉 ──


@pytest.mark.smoke
def test_the_import_approval_cannot_be_overridden_to_allow():
    """`permission: {"import_external_file": "allow"}` 不得把它翻成免核准。

    這個工具的「每一次都問」是 plan §6 第 12 條定的邊界,不是偏好:開關授權的
    是「可以從專案外複製檔案進來」這件事,每一次的來源與目的仍要人看過。
    覆寫能翻它,等於 client.json 一行就把邊界拆掉。`deny` 照舊可以(關掉是收緊)。
    """
    import client_config
    import client_policy

    settings = client_config.ClientSettings(
        path=Path("/nonexistent"), present=True,
        permission={"import_external_file": "allow", "run_lint": "allow"},
    )
    policy = client_config.policy_for(settings, client_policy.InteractivePolicy())
    assert policy.decide(
        "import_external_file", read_only=False, arguments={"source_path": "x"}
    ) is client_policy.Decision.ASK
    # 其他 ask 工具照舊可以被放寬(那是使用者的偏好)。
    assert policy.decide("run_lint", read_only=False, arguments={}) is client_policy.Decision.ALLOW
    denied = client_config.policy_for(
        client_config.ClientSettings(
            path=Path("/nonexistent"), present=True,
            permission={"import_external_file": "deny"},
        ),
        client_policy.InteractivePolicy(),
    )
    assert denied.decide("import_external_file", read_only=False, arguments={}) is client_policy.Decision.DENY


@pytest.mark.smoke
def test_the_import_approval_shows_where_the_file_will_land():
    """核准框要讓人看得到**目的路徑**,不只是模型丟進來的參數。

    實際落點是工具之後才算出來的(`.aicode_uploads/<安全化檔名>`,同名再加
    `_N`);只顯示 `source_path` 的核准等於核准一個不知道會寫到哪的動作。
    """
    request = client_engine.ApprovalRequest(
        "s", "import_external_file", {"source_path": "~/Downloads/my report (v2).pdf"}
    )
    text = request.render()
    assert ".aicode_uploads/" in text
    assert "my_report_v2.pdf" in text or "my report (v2).pdf" in text


# ── 總審 F1-8:沒有 server 端 id 的工具呼叫,退回的 id 不得跨回合重複 ──


@pytest.mark.smoke
def test_fallback_tool_call_ids_are_unique_across_steps():
    """OpenAI-compatible 串流不一定給 tool-call id。

    退回 `call_<位置>` 的話,每一個 step 的第一個工具都叫 `call_0`:TUI 以 id 當
    全域 key,第二次呼叫不建新 block,舊 block 掛著舊的工具名與參數卻顯示第二次
    的狀態與輸出。
    """
    first = client_engine._finalise_tool_calls({0: {"name": "list_dir", "arguments": "{}"}})  # noqa: SLF001
    second = client_engine._finalise_tool_calls({0: {"name": "read_file", "arguments": "{}"}})  # noqa: SLF001
    assert first[0]["id"] != second[0]["id"]


# ============================================================
# prompt cache 預熱(prime_prompt_cache):唯一一條沒有使用者訊息就打主模型的路徑
# ============================================================
# 這一區守的是「新增一條打模型的路徑」帶進來的四個無聲失敗:
#   1. 送的不是下一輪的 prefix → prompt cache 一個字也重用不到,而且完全看不出來。
#   2. 它其實寫了東西(session 檔 / 取消旗標 / 事件)→ 使用者的取消落在背景工作上。
#   3. readonly session 被它打了模型 → 評測邊界破掉,replay 前後的現場不再相同。
#   4. gate 的保留額與實送的 max_tokens 不是同一個數字 → 閘等於沒有對齊。


class _CountingStore(client_store.EphemeralSessionStore):
    """會逐筆記下 append 的 session store:預熱的「零寫入」只能用計數證明。"""

    def __init__(self, root):
        super().__init__(root)
        self.appended: list[dict] = []

    def append(self, session_id, record):
        self.appended.append(dict(record))
        return super().append(session_id, record)


class _HeldStream:
    """每個 chunk 都要等 ``release`` 才給的串流(模擬還在 prefill 的請求)。"""

    def __init__(self, release, chunks=()):
        self.release = release
        self._chunks = list(chunks)
        self.closed_from: list[str] = []

    def __iter__(self):
        return self

    def __next__(self):
        self.release.wait(5)
        if not self._chunks:
            raise StopIteration
        return self._chunks.pop(0)

    def close(self):
        self.closed_from.append(threading.current_thread().name)
        self.release.set()


def _prime_final_chunk(prompt_n=7, predicted_n=1):
    """預熱的最後一個 chunk:max_tokens=1 ⇒ finish_reason=length,而且只帶 timings。"""
    return {
        "choices": [{"delta": {"content": "x"}, "finish_reason": "length"}],
        "timings": {"prompt_n": prompt_n, "predicted_n": predicted_n},
    }


def _no_probe(*_args, **_kwargs):
    raise AssertionError("這一步不該走到 /slots probe")


def _no_request(**_kwargs):
    raise AssertionError("這一步不該打模型")


def _wire_call(call_id, name):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _synthetic_history():
    """同時踩到三個轉換的歷史:三個 user 回合、兩筆各 30k tokens 的工具輸出,
    最後一則 assistant 帶 reasoning **而且**有一個懸空的 tool_call。

    兩筆工具輸出的大小是刻意挑的:以「這一輪」的邊界算,兩筆都在保護範圍內
    (prune 一筆都不剪);多了下一則使用者訊息之後,第一筆才會跨過門檻被剪掉。
    預熱送錯邊界的話,這條歷史會讓兩份 payload 差一筆 30k tokens 的工具輸出。
    """
    big = "x" * int(30_000 * config.CHARS_PER_TOKEN)
    return [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": None, "tool_calls": [_wire_call("c1", "read_file")]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": big},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": None, "tool_calls": [_wire_call("c2", "grep_code")]},
        {"role": "tool", "tool_call_id": "c2", "name": "grep_code", "content": big},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
        # 懸空:宣告了 c3 卻沒有結果(上一輪被中斷)。
        {
            "role": "assistant",
            "content": "a3",
            "reasoning_content": "上一輪的 thinking",
            "tool_calls": [_wire_call("c3", "list_dir")],
        },
    ]


@pytest.mark.smoke
def test_priming_sends_the_prefix_the_next_turn_will_send_and_records_nothing(
    engine_factory, monkeypatch, tmp_path, capsys
):
    """預熱送的必須**逐字**是下一輪真的會送的那一份,而且什麼都不寫。

    判準不是「同一個函式自己跟自己比」——那樣改壞轉換時兩邊會一起錯:先預熱、
    再真的 `send("q4")`,拿**實際送出去的 payload** 來比。差一則 reasoning 或
    差一筆被剪掉的工具輸出,prefix 就從那個 token 起全部對不上,prompt cache
    一個字也重用不到,而且畫面上完全看不出來。
    """
    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    monkeypatch.setattr(llama_client, "get_slots", lambda *_a, **_k: None)
    store = _CountingStore(engine_factory.root)
    engine = engine_factory(store=store)
    engine.messages = _synthetic_history()
    before = copy.deepcopy(engine.messages)

    sent: list[dict] = []

    def _chat(**kwargs):
        sent.append(kwargs)
        if len(sent) == 1:
            return iter([_prime_final_chunk(prompt_n=7)])
        return iter([_text_chunk("answer", finish="stop")])

    monkeypatch.setattr(llama_client, "chat_completions", _chat)

    # 關掉開關 = 連 HTTP 都不該發生(這一條在任何 I/O 之前)。
    monkeypatch.setattr(config, "CLIENT_PRIME_PROMPT_CACHE", False)
    assert engine.prime_prompt_cache(reason="mount") == client_engine.PrimeOutcome(
        False, "disabled", None
    )
    assert sent == []
    monkeypatch.setattr(config, "CLIENT_PRIME_PROMPT_CACHE", True)

    appends_before = len(store.appended)
    outcome = engine.prime_prompt_cache(reason="mount")
    assert outcome.sent is True and outcome.reason == ""
    assert outcome.processed_tokens == 7          # 只來自 timings.prompt_n

    prime = sent[0]
    assert prime["stream"] is True
    assert prime["extra"] == {"max_tokens": client_engine.PRIME_MAX_TOKENS} == {"max_tokens": 1}
    assert prime["tools"] == engine.openai_tools() and prime["tool_choice"] == "auto"

    # 零寫入:歷史逐字不變、session 檔一筆都沒多、畫面事件一個都沒有(沒有 on_event 可傳)。
    assert engine.messages == before
    assert len(store.appended) == appends_before
    assert engine.priming is False
    assert capsys.readouterr() == ("", "")

    # 邊界真的是「下一輪」:這一輪的 payload 還留著第一筆工具輸出、也還留著最後一則
    # assistant 的 reasoning;prefix 兩樣都已經照下一輪的規則處理掉了。
    this_turn, _summary = engine.payload_messages()
    assert this_turn[3]["content"] == before[2]["content"]
    assert "reasoning_content" in this_turn[10]
    assert prime["messages"][3]["content"] == client_engine.PRUNE_PLACEHOLDER
    assert "reasoning_content" not in prime["messages"][10]     # 最後一則 assistant
    assert prime["messages"][11]["tool_call_id"] == "c3"        # 懸空呼叫已補上結果
    assert not any(
        client_engine._PRIME_PLACEHOLDER_KEY in message for message in prime["messages"]
    )

    # 真的送下一題:payload 必須是 prefix + 那一則 user,一個 token 都不差。
    result = engine.send("q4", on_event=lambda _e: None)
    assert result.finish == client_events.REASON_STOP
    turn = sent[1]
    assert turn["messages"][:-1] == prime["messages"]
    assert turn["messages"][-1] == {"role": "user", "content": "q4"}
    for key in ("model", "temperature", "top_p", "top_k", "min_p", "tools", "tool_choice"):
        assert prime[key] == turn[key], key

    # telemetry:`prime` 自成一列,保留額就是實送的 max_tokens。
    rows = [
        json.loads(line)
        for line in (tmp_path / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    primed = [row for row in rows if row["source"] == client_engine.PRIME_SOURCE]
    assert len(primed) == 1
    assert primed[0]["reserved_output_tokens"] == 1
    assert primed[0]["prompt_tokens_processed"] == 7


@pytest.mark.smoke
def test_priming_refuses_a_readonly_engine_before_any_probe_or_request(
    engine_factory, monkeypatch, capsys
):
    """readonly session(canary / eval / replay)一律不得預熱,而且是在**任何 I/O 之前**。

    那條邊界的意思是「這個 session 不會改變現場、也不會多打模型」;在 probe 之後
    才發現就已經晚了 —— `/slots` 也是一次對 server 的請求。
    """
    store = _CountingStore(engine_factory.root)
    engine = engine_factory(policy=client_policy.ReadOnlyPolicy(), store=store)
    monkeypatch.setattr(llama_client, "get_slots", _no_probe)
    monkeypatch.setattr(llama_client, "chat_completions", _no_request)

    assert engine.prime_prompt_cache(reason="mount") == client_engine.PrimeOutcome(
        False, "policy", None
    )
    assert store.appended == []
    assert engine.priming is False
    assert engine.model_lock.acquire(timeout=0.5) is True
    engine.model_lock.release()
    assert capsys.readouterr() == ("", "")


@pytest.mark.smoke
def test_priming_yields_to_a_turn_that_already_began_and_never_reads_its_history(
    engine_factory, monkeypatch, tmp_path, capsys
):
    """回合已經開始就讓路——**模型鎖是空的也一樣**。

    `send()` 從寫 user 訊息就算「進行中」,那段期間模型鎖還沒被取。只看鎖的話,
    預熱會在這個空窗擠進去,而且它 snapshot 到的歷史還沒有那則新問題:使用者
    的第一題於是排在一份**錯的** prefix 後面。
    """
    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    blocked = threading.Event()
    reached = threading.Event()

    class _SlowStore(client_store.EphemeralSessionStore):
        def append(self, session_id, record):
            if record.get("role") == "user":
                reached.set()
                blocked.wait(5)
            return super().append(session_id, record)

    engine = engine_factory(store=_SlowStore(engine_factory.root))
    monkeypatch.setattr(llama_client, "get_slots", _no_probe)
    sent: list[dict] = []
    holding = threading.Event()
    release = threading.Event()

    def _chat(**kwargs):
        sent.append(kwargs)
        if len(sent) == 2:
            holding.set()
            release.wait(5)
        return iter([_text_chunk("answer", finish="stop")])

    monkeypatch.setattr(llama_client, "chat_completions", _chat)
    worker = threading.Thread(target=lambda: engine.send("q", on_event=lambda _e: None))
    worker.start()
    assert reached.wait(5)                       # 卡在寫 user 訊息,鎖還沒取
    assert engine.prime_prompt_cache(reason="mount") == client_engine.PrimeOutcome(
        False, "turn_in_progress", None
    )
    assert sent == []                            # 零 HTTP:連 /slots 都沒走到
    assert engine.model_lock.acquire(timeout=0.5) is True   # 鎖已經放回去
    engine.model_lock.release()
    blocked.set()
    worker.join(5)
    assert not worker.is_alive()
    assert sent[0]["messages"][-1] == {"role": "user", "content": "q"}

    # 另一半:回合已經**持著**模型鎖時,非阻塞取鎖失敗就退開(不排隊)。
    second = threading.Thread(target=lambda: engine.send("q2", on_event=lambda _e: None))
    second.start()
    assert holding.wait(5)
    assert engine.prime_prompt_cache() == client_engine.PrimeOutcome(False, "model_busy", None)
    release.set()
    second.join(5)
    assert not second.is_alive()
    assert capsys.readouterr() == ("", "")


@pytest.mark.smoke
def test_a_turn_submitted_during_priming_waits_for_the_lock_and_gets_the_primed_prefix(
    engine_factory, monkeypatch, tmp_path
):
    """使用者在預熱進行中送出:那一輪在模型鎖上等,**不得**與預熱重疊。

    llama-server 單 slot 的序列化就在這把鎖上;重疊等於兩個請求同時排隊,而且
    使用者那一份可能先跑完、把預熱好的 slot 換掉。等到之後,它拿到的 payload
    必須就是 prefix + 那一則新問題。
    """
    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    monkeypatch.setattr(llama_client, "get_slots", lambda *_a, **_k: None)
    engine = engine_factory()
    engine.messages = _synthetic_history()
    release = threading.Event()
    held = _HeldStream(release, [_prime_final_chunk()])
    calls: list[dict] = []

    def _chat(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return held
        return iter([_text_chunk("answer", finish="stop")])

    monkeypatch.setattr(llama_client, "chat_completions", _chat)
    outcome: dict = {}
    primer = threading.Thread(
        target=lambda: outcome.setdefault("prime", engine.prime_prompt_cache(reason="mount"))
    )
    primer.start()
    for _ in range(200):
        if calls:
            break
        time.sleep(0.01)
    assert len(calls) == 1                       # 預熱已送出,而且還握著鎖

    sender = threading.Thread(
        target=lambda: outcome.setdefault("turn", engine.send("q", on_event=lambda _e: None))
    )
    sender.start()
    time.sleep(0.3)
    assert len(calls) == 1 and sender.is_alive()  # 使用者那一輪的請求還沒發出去

    release.set()
    primer.join(5)
    sender.join(5)
    assert not primer.is_alive() and not sender.is_alive()
    assert outcome["prime"].sent is True
    assert len(calls) == 2
    assert calls[1]["messages"][:-1] == calls[0]["messages"]
    assert calls[1]["messages"][-1] == {"role": "user", "content": "q"}


@pytest.mark.smoke
def test_a_session_switch_aborts_an_in_flight_prime_and_frees_the_lock(
    engine_factory, monkeypatch, tmp_path
):
    """換 session 要把進行中的預熱收掉:它送的是**上一段對話**的 prefix,而且租著模型鎖。

    不收的話,使用者在新對話問的第一題要排在一個已經沒有用處的請求後面 —— 症狀
    是「剛換過去就卡住」,而畫面上沒有任何東西在跑。收掉的那一份不寫 telemetry:
    一列半途而廢的請求會讓冷 / 熱的判讀多出假資料。
    """
    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    monkeypatch.setattr(llama_client, "get_slots", lambda *_a, **_k: None)
    engine = engine_factory()

    for switch in ("new_session", "adopt"):
        stream = _BlockingStream()
        monkeypatch.setattr(llama_client, "chat_completions", lambda **_kw: stream)
        outcome: dict = {}
        primer = threading.Thread(
            target=lambda: outcome.setdefault("r", engine.prime_prompt_cache(reason=switch))
        )
        primer.start()
        for _ in range(200):
            if not stream._first:                # 已經在讀第二個 chunk
                break
            time.sleep(0.01)
        time.sleep(0.05)
        assert engine.priming is True, switch

        started = time.monotonic()
        if switch == "new_session":
            engine.new_session()
        else:
            engine.adopt(client_engine.SessionSnapshot(session_id=engine.session_id))
        primer.join(2)
        assert not primer.is_alive(), switch
        assert time.monotonic() - started < 2, switch
        assert outcome["r"] == client_engine.PrimeOutcome(False, "aborted", None), switch
        assert len(stream.closed_from) == 1, switch      # 關一次,不是兩次
        assert engine.priming is False, switch
        assert engine.model_lock.acquire(timeout=0.5) is True, switch
        engine.model_lock.release()

    # 中止過的預熱一列 telemetry 都不留。
    log = tmp_path / "metrics.jsonl"
    assert not log.exists() or log.read_text(encoding="utf-8").strip() == ""


@pytest.mark.smoke
def test_priming_skips_only_when_no_slot_is_idle_and_never_raises_or_prints(
    engine_factory, monkeypatch, tmp_path, capsys
):
    """多 slot 的 server:只有**每一個** slot 都在忙才跳過;讀不到就當未知、照送。

    寫成「有人在忙就不送」的話,主 server 上任何一個 KB / 壓縮請求都會讓預熱
    永遠不發生 —— 而且是無聲的。反過來,失敗一律回 outcome:預熱是背景工作,
    它壞掉不該變成使用者看得到的錯誤,Textual 接管畫面之後更不能寫 stderr。
    """
    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    # 先把**真的** get_slots 留下來:末段的 quiet / 預設兩次 probe 要走產品的函式,
    # 不能還在呼叫下面那個只記 probe、回 None 的 stub(Astra R1-B01)。
    real_get_slots = llama_client.get_slots
    engine = engine_factory()
    sent: list[dict] = []

    def _chat(**kwargs):
        sent.append(kwargs)
        return iter([_prime_final_chunk()])

    monkeypatch.setattr(llama_client, "chat_completions", _chat)
    probes: list[dict] = []

    def _slots_returning(value):
        def _slots(base_url, **kwargs):
            probes.append({"base_url": base_url, **kwargs})
            return value

        return _slots

    monkeypatch.setattr(
        llama_client,
        "get_slots",
        _slots_returning([{"id": 0, "is_processing": True}, {"id": 1, "state": 0}]),
    )
    assert engine.prime_prompt_cache().sent is True          # 一忙三閒也照送
    assert probes[0]["quiet"] is True                        # 畫面被接管:probe 不得寫 stderr
    assert probes[0]["timeout"] == client_engine.PRIME_SLOTS_TIMEOUT

    monkeypatch.setattr(
        llama_client,
        "get_slots",
        _slots_returning([{"id": 0, "is_processing": True}, {"id": 1, "state": 2}]),
    )
    assert engine.prime_prompt_cache() == client_engine.PrimeOutcome(False, "server_busy", None)

    monkeypatch.setattr(llama_client, "get_slots", _slots_returning(None))
    assert engine.prime_prompt_cache().sent is True          # 讀不到 = 未知 = 照送
    assert len(sent) == 2

    def _boom(**_kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr(llama_client, "chat_completions", _boom)
    assert engine.prime_prompt_cache() == client_engine.PrimeOutcome(
        False, "error:RuntimeError", None
    )
    assert engine.model_lock.acquire(timeout=0.5) is True    # 例外路徑也把鎖放回去
    engine.model_lock.release()
    assert engine.priming is False
    assert capsys.readouterr() == ("", "")

    # llama_client 那一端:quiet 的 probe 失敗零 stderr,預設的呼叫端仍然要留原因
    # (把「被 policy 擋掉」偽裝成 server down 正是那一行要防的事)。兩次都走**真的**
    # get_slots,只 mock 底層的 session。
    class _DeadSession:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("connection refused (stub)")

    monkeypatch.setattr(llama_client, "get_slots", real_get_slots)
    monkeypatch.setattr(llama_client, "get_session", lambda: _DeadSession())
    assert llama_client.get_slots("http://127.0.0.1:65535", quiet=True) is None
    assert capsys.readouterr() == ("", "")
    assert llama_client.get_slots("http://127.0.0.1:65535") is None
    assert "/slots probe failed" in capsys.readouterr().err


@pytest.mark.smoke
def test_priming_gates_the_one_token_it_sends_after_checking_the_next_turn_reserve(
    engine_factory, monkeypatch, tmp_path
):
    """閘的保留額 == 實送的 max_tokens == 1;下一輪反正會溢位就根本不送。

    保留額與實送的 max_tokens 不同的話,這條路徑上的閘就不是同一個閘(見
    `config.CLIENT_MAX_OUTPUT_TOKENS` 的同一個理由)。而「下一輪會不會溢位」是
    另一件事:它只決定這次預熱值不值得做,所以用 `build_usage` 試算、**不寫 log**
    —— 用 `check_and_log` 的話 telemetry 會多出一列從來沒送出去的請求。
    """
    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    monkeypatch.setattr(llama_client, "get_slots", lambda *_a, **_k: None)
    engine = engine_factory()
    engine.messages = _synthetic_history()
    sent: list[dict] = []

    def _chat(**kwargs):
        sent.append(kwargs)
        return iter([_prime_final_chunk()])

    monkeypatch.setattr(llama_client, "chat_completions", _chat)
    gate_calls: list[dict] = []
    real_check = context_budget.check_and_log

    def _spy_check(**kwargs):
        gate_calls.append(kwargs)
        return real_check(**kwargs)

    logged: list = []
    real_log = context_budget.log_metrics

    def _spy_log(usage):
        logged.append(usage)
        return real_log(usage)

    monkeypatch.setattr(context_budget, "check_and_log", _spy_check)
    monkeypatch.setattr(context_budget, "log_metrics", _spy_log)

    assert engine.prime_prompt_cache().sent is True
    assert gate_calls[-1]["source"] == client_engine.PRIME_SOURCE
    assert gate_calls[-1]["reserved_output_tokens"] == client_engine.PRIME_MAX_TOKENS == 1
    assert sent[-1]["extra"]["max_tokens"] == gate_calls[-1]["reserved_output_tokens"]
    assert logged[-1].reserved_output_tokens == 1

    # 把 n_ctx 調到「下一輪的保留額會溢位、這一次的 1 個 token 不會」:預熱不做。
    prefix = engine.next_turn_prefix()
    estimated, _chars = context_budget.estimate_tokens(
        messages=prefix, tools=engine.openai_tools()
    )
    engine.options.n_ctx = int(
        (estimated + client_engine.PRIME_MAX_TOKENS) / config.CTX_HARD_THRESHOLD
    ) + 1
    sent.clear()
    gate_calls.clear()
    logged.clear()
    assert engine.prime_prompt_cache() == client_engine.PrimeOutcome(
        False, "next_turn_would_overflow", None
    )
    assert sent == [] and logged == []
    assert gate_calls == []          # 沒有走 check_and_log ⇒ 沒有寫下拒絕那一列


@pytest.mark.smoke
def test_priming_is_invisible_to_cancel_and_leaves_the_turn_state_untouched(
    engine_factory, monkeypatch, tmp_path
):
    """預熱不是一輪對話:取消看不到它,回合狀態一個欄位都不動。

    共用 `_cancel` / `_in_turn` / `_active_stream` 的話,使用者在預熱期間按 Ctrl-C
    會得到「已中斷」(其實沒有東西在跑),或者旗標留到下一題把真正的問題打掉。
    """
    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    monkeypatch.setattr(llama_client, "get_slots", lambda *_a, **_k: None)
    engine = engine_factory()
    release = threading.Event()
    held = _HeldStream(release, [_prime_final_chunk()])
    monkeypatch.setattr(llama_client, "chat_completions", lambda **_kw: held)
    outcome: dict = {}
    primer = threading.Thread(
        target=lambda: outcome.setdefault("r", engine.prime_prompt_cache(reason="mount"))
    )
    primer.start()
    for _ in range(200):
        if engine.priming:
            break
        time.sleep(0.01)
    assert engine.priming is True

    assert engine.request_cancel().accepted is False    # 閒置:沒有東西可取消
    assert engine.cancel() is False
    assert not engine._cancel.is_set()                  # 旗標不得留給下一題
    assert engine._in_turn == 0
    assert engine._armed is False and engine._turn_seen_since_clear is False
    assert engine._active_stream is None and engine._active_call is None

    release.set()
    primer.join(5)
    assert not primer.is_alive()
    assert outcome["r"].sent is True

    engine.clear_cancel()
    monkeypatch.setattr(
        llama_client, "chat_completions", _stream(_text_chunk("answer", finish="stop"))
    )
    result = engine.send("q", on_event=lambda _e: None)
    assert result.finish == client_events.REASON_STOP
    assert [m["role"] for m in engine.messages] == ["user", "assistant"]


# ── Step 5 R1 回修(Astra R1-B02 / R1-B03 / R1-B05):預熱的中止邊界、部分完成的工具群組、終結判定 ──


def _prime_threads_gone(timeout=5.0):
    """等預熱自己開的小執行緒(probe / http / settle)全部結束。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(t.name.startswith("codetrail-prime-") for t in threading.enumerate()):
            return True
        time.sleep(0.01)
    return False


def _prime_rows(path):
    if not path.exists():
        return []
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [row for row in rows if row["source"] == client_engine.PRIME_SOURCE]


@pytest.mark.smoke
def test_priming_matches_the_next_send_when_a_tool_group_is_only_partly_answered(
    engine_factory, monkeypatch, tmp_path
):
    """同一組兩個工具呼叫之間被中斷(a 有結果、b 沒有):預熱送的 prefix 必須逐字等於
    下一輪真的送出的 payload 減最後那則 user。

    真正的 `send()` 先由 `heal_pending_tool_calls()` 把 `tool(b, 已中斷)` **append 在既有
    `tool(a)` 之後**;預熱若把補的結果插在 assistant 之後、`tool(a)` 之前,兩份 payload 在
    工具群組中途就分岔 —— prompt cache 從那個 token 起一個字也重用不到,而且畫面上完全
    看不出來(兩邊都是合法的歷史)。工具結果仍必須緊接宣告它的群組。
    """
    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    monkeypatch.setattr(llama_client, "get_slots", lambda *_a, **_k: None)
    store = _CountingStore(engine_factory.root)
    engine = engine_factory(store=store)
    engine.messages = [
        {"role": "user", "content": "q1"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_wire_call("a", "list_dir"), _wire_call("b", "read_file")],
        },
        # a 跑完了;b 宣告了卻沒有結果(上一輪在兩個工具之間被中斷)。
        {"role": "tool", "tool_call_id": "a", "name": "list_dir", "content": "status: ok\n(a 的結果)"},
    ]
    before = copy.deepcopy(engine.messages)
    sent: list[dict] = []

    def _chat(**kwargs):
        sent.append(kwargs)
        if len(sent) == 1:
            return iter([_prime_final_chunk(prompt_n=5)])
        return iter([_text_chunk("answer", finish="stop")])

    monkeypatch.setattr(llama_client, "chat_completions", _chat)

    assert engine.prime_prompt_cache(reason="session").sent is True
    assert engine.messages == before                 # 預熱不改寫原始歷史
    assert store.appended == []                      # 也不落檔

    result = engine.send("q2", on_event=lambda _e: None)
    assert result.finish == client_events.REASON_STOP
    prime, turn = sent
    assert turn["messages"][-1] == {"role": "user", "content": "q2"}
    assert turn["messages"][:-1] == prime["messages"]
    # 結果緊接宣告群組、宣告順序不變:assistant → tool(a) → tool(b, 已中斷)。
    shape = [(m["role"], m.get("tool_call_id")) for m in prime["messages"]]
    assert shape == [
        ("system", None), ("user", None), ("assistant", None), ("tool", "a"), ("tool", "b"),
    ], shape
    assert prime["messages"][4]["content"] == client_engine.CANCELLED_TOOL_RESULT


@pytest.mark.smoke
def test_priming_only_counts_as_sent_after_a_terminal_chunk_with_timings(
    engine_factory, monkeypatch, tmp_path, capsys
):
    """沒有終結 chunk 的 clean EOF、或終結了卻沒有 `timings.prompt_n`,都不是成功的預熱。

    HTTP 200 之後只收到 keep-alive、或送了幾個 delta 就正常關線,`OpenAIStream` 不會丟
    例外;照樣記成 sent 的話,telemetry 多出一列 `prompt_tokens_processed` 為空的 `prime`、
    `/status` 顯示 sent,而 T0 的冷 / 熱判讀從此建立在一個根本沒完成的請求上。
    `usage.prompt_tokens` 也不得拿來代填 processed:那是「prompt 多長」,不是「重算了多少」。
    """
    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    monkeypatch.setattr(llama_client, "get_slots", lambda *_a, **_k: None)
    engine = engine_factory()
    log = tmp_path / "metrics.jsonl"

    finished_without_timings = {
        "choices": [{"delta": {"content": "x"}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 20000, "completion_tokens": 1},
    }
    cases = [
        ("empty", (), "incomplete"),                                  # 只有 keep-alive
        ("delta_then_eof", (_text_chunk("x"),), "incomplete"),        # 非終結 delta 後正常關線
        ("finished_without_timings", (finished_without_timings,), "no_timings"),
    ]
    for name, chunks, reason in cases:
        monkeypatch.setattr(llama_client, "chat_completions", _stream(*chunks))
        outcome = engine.prime_prompt_cache(reason="mount")
        assert outcome == client_engine.PrimeOutcome(False, reason, None), (name, outcome)
        assert _prime_rows(log) == [], name                           # 不生成冒充成功的列
        assert engine.priming is False, name
        assert engine.model_lock.acquire(timeout=0.5) is True, name
        engine.model_lock.release()

    # 正常情境不變:max_tokens=1 ⇒ finish_reason=length,最後一個 chunk 帶 timings。
    monkeypatch.setattr(llama_client, "chat_completions", _stream(_prime_final_chunk(prompt_n=9)))
    assert engine.prime_prompt_cache(reason="mount") == client_engine.PrimeOutcome(True, "", 9)
    primed = _prime_rows(log)
    assert len(primed) == 1 and primed[0]["prompt_tokens_processed"] == 9
    assert capsys.readouterr() == ("", "")


@pytest.mark.smoke
def test_a_session_switch_aborts_a_prime_that_has_no_stream_yet_and_never_sends_after_the_abort(
    engine_factory, monkeypatch, tmp_path
):
    """中止要能落在 `/slots` probe、probe 回來之後、POST 已送出但 headers 未到這三段。

    這三段都還沒有 stream 可關。只會關 stream 的中止在這裡等一秒就放棄,而 `new_session()` /
    `adopt()` 不看回傳值照樣換歷史:舊預熱繼續把**上一段對話**的 prefix 送出去、`priming`
    仍是 True、模型鎖仍被它租著 —— 新對話的預熱回 `model_busy`,使用者的第一題也排在一個
    沒有用處的請求後面。修法不得借回合的取消旗標,也不得在舊 POST 還在飛的時候提早放鎖
    (那會讓新對話的第一題與舊請求在 llama-server 上重疊)。
    """
    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    engine = engine_factory()
    engine.messages = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "history"}]
    posts: list[dict] = []
    outcome: dict = {}

    def _record_post(**kwargs):
        posts.append(kwargs)
        return iter([_prime_final_chunk()])

    # (1) 中止落在 probe:GET 還卡著,預熱要立刻結束、放鎖(GET 不占 slot),之後不得再發 POST。
    probing = threading.Event()
    release_probe = threading.Event()

    def _stuck_probe(*_args, **_kwargs):
        probing.set()
        release_probe.wait(5)
        return None

    monkeypatch.setattr(llama_client, "get_slots", _stuck_probe)
    monkeypatch.setattr(llama_client, "chat_completions", _record_post)
    primer = threading.Thread(
        target=lambda: outcome.setdefault("probe", engine.prime_prompt_cache(reason="mount")),
        daemon=True,
    )
    primer.start()
    assert probing.wait(5)
    assert engine.priming is True
    started = time.monotonic()
    engine.new_session()
    primer.join(1)
    assert not primer.is_alive()
    assert time.monotonic() - started < 1.0                        # B7:一秒內結束
    assert outcome["probe"] == client_engine.PrimeOutcome(False, "aborted", None)
    assert engine.priming is False
    assert engine.model_lock.acquire(timeout=0.5) is True          # 鎖已放
    engine.model_lock.release()
    release_probe.set()
    assert _prime_threads_gone()
    assert posts == []                                              # 中止之後沒有 POST

    # (2) 中止落在 probe 回來之後、POST 之前:什麼都還沒送,不得補送。
    monkeypatch.setattr(llama_client, "get_slots", lambda *_a, **_k: None)
    real_check = context_budget.check_and_log

    def _abort_at_the_gate(**kwargs):
        usage = real_check(**kwargs)
        engine.abort_prime(wait=0)
        return usage

    monkeypatch.setattr(context_budget, "check_and_log", _abort_at_the_gate)
    assert engine.prime_prompt_cache(reason="mount") == client_engine.PrimeOutcome(
        False, "aborted", None
    )
    assert posts == []
    monkeypatch.setattr(context_budget, "check_and_log", real_check)

    # (3) 中止落在 POST 已送出、headers 未到：transport 必須先 shutdown，B7 一秒內放鎖。
    engine.messages = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "history"}]
    posting = threading.Event()
    pending_socket = _PendingHeaderSocket(engine.model_lock)
    release_post = pending_socket.release
    late = _BlockingStream()

    def _stuck_post(**kwargs):
        # 替身也遵守新 transport 契約：在送出之前登記可取消的 socket。
        kwargs["cancel"].register(pending_socket)
        posts.append(kwargs)
        posting.set()
        release_post.wait(5)
        return late

    monkeypatch.setattr(llama_client, "chat_completions", _stuck_post)
    primer = threading.Thread(
        target=lambda: outcome.setdefault("post", engine.prime_prompt_cache(reason="session")),
        daemon=True,
    )
    primer.start()
    assert posting.wait(5)
    started = time.monotonic()
    engine.adopt(
        client_engine.SessionSnapshot(
            session_id=engine.session_id, messages=({"role": "user", "content": "new"},)
        )
    )
    primer.join(1)
    assert not primer.is_alive()
    assert time.monotonic() - started < 1.0
    assert outcome["post"] == client_engine.PrimeOutcome(False, "aborted", None)
    assert engine.priming is False
    assert pending_socket.shutdown_seen.is_set()                  # 仍在飛時不得提早放鎖
    assert pending_socket.lock_was_held_at_shutdown is True
    assert engine.model_lock.acquire(blocking=False) is True      # HTTP 已中止，不等 headers
    engine.model_lock.release()
    assert _prime_threads_gone()
    assert len(late.closed_from) == 1                               # 關一次,不是零次也不是兩次
    assert len(posts) == 1                                          # 只有那一次;沒有補送

    # (4) 換過去之後的預熱照常准入,送的是新歷史,不受舊的中止影響。
    monkeypatch.setattr(llama_client, "chat_completions", _record_post)
    assert engine.prime_prompt_cache(reason="session").sent is True
    assert posts[-1]["messages"][-1] == {"role": "user", "content": "new"}

    # 被中止的預熱一列 telemetry 都不留;只有 (4) 那一列。
    assert len(_prime_rows(tmp_path / "metrics.jsonl")) == 1


class _PendingHeaderSocket:
    """完全離線的 socket：request bytes 收下，headers 一直等到 shutdown。"""

    def __init__(self, lock):
        self.headers = threading.Event()
        self.shutdown_seen = threading.Event()
        self.release = threading.Event()
        self.sent = []
        self.lock = lock
        self.lock_was_held_at_shutdown = None

    def settimeout(self, _timeout):
        pass

    def sendall(self, data):
        if self.shutdown_seen.is_set():
            raise OSError("offline socket is shut down")
        self.sent.append(bytes(data))

    def makefile(self, _mode):
        return self

    def readline(self, _limit=-1):
        self.headers.set()
        self.release.wait(5)
        return b""

    def shutdown(self, _how):
        acquired = self.lock.acquire(blocking=False)
        self.lock_was_held_at_shutdown = not acquired
        if acquired:
            self.lock.release()
        self.shutdown_seen.set()
        self.release.set()

    def close(self):
        pass


@pytest.mark.smoke
def test_switching_session_shuts_down_pending_headers_before_releasing_the_model_lock(
    engine_factory, monkeypatch, tmp_path, capsys
):
    """R2-B01：真正走 requests/urllib3；不回 headers 也必須在 B7 的一秒內收掉。"""
    import http_client
    import urllib3.connection

    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    monkeypatch.setattr(llama_client, "get_slots", lambda *_a, **_k: None)
    session = http_client.create_session()
    monkeypatch.setattr(llama_client, "get_session", lambda: session)
    engine = engine_factory()
    for switch in ("new", "adopt"):
        sock = _PendingHeaderSocket(engine.model_lock)
        monkeypatch.setattr(urllib3.connection.HTTPConnection, "_new_conn", lambda _self: sock)
        outcome = []
        primer = threading.Thread(target=lambda: outcome.append(engine.prime_prompt_cache()), daemon=True)
        primer.start()
        try:
            assert sock.headers.wait(2), "未進入離線 headers 等待"
            assert b"POST /v1/chat/completions " in b"".join(sock.sent)
            started = time.monotonic()
            if switch == "new":
                engine.new_session()
            else:
                engine.adopt(client_engine.SessionSnapshot(session_id=engine.session_id))
            primer.join(max(0, 0.9 - (time.monotonic() - started)))
            assert not primer.is_alive()
            assert outcome == [client_engine.PrimeOutcome(False, "aborted", None)]
            assert engine.priming is False
            freed = engine.model_lock.acquire(blocking=False)
            if freed:
                engine.model_lock.release()
            assert freed, "B7: headers 還沒回時，模型鎖仍被舊預熱持有"
            assert time.monotonic() - started < 1
            assert sock.shutdown_seen.is_set(), "放鎖之前必須先 shutdown 舊 HTTP"
            assert sock.lock_was_held_at_shutdown is True
        finally:
            sock.release.set()
            primer.join(2)
            assert _prime_threads_gone()
    session.close()
    assert _prime_rows(tmp_path / "metrics.jsonl") == []
    assert capsys.readouterr() == ("", "")


@pytest.mark.smoke
def test_abort_between_prime_registration_and_fast_io_cannot_be_lost(
    engine_factory, monkeypatch, tmp_path
):
    """R2-B02a：登記後已取消，立即完成的 GET/POST 也不能洗掉那次 abort。"""
    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    engine = engine_factory()
    ready, resume = threading.Event(), threading.Event()
    real_locked = engine._prime_locked

    def paused_locked(*args, **kwargs):
        ready.set()
        resume.wait(3)
        return real_locked(*args, **kwargs)

    monkeypatch.setattr(engine, "_prime_locked", paused_locked)
    real_thread = threading.Thread

    def immediate_io_thread(*args, **kwargs):
        if kwargs.get("name") in {"codetrail-prime-probe", "codetrail-prime-http"}:
            class Immediate:
                def start(self):
                    kwargs["target"]()
            return Immediate()
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(client_engine.threading, "Thread", immediate_io_thread)
    posts = []
    monkeypatch.setattr(llama_client, "get_slots", lambda *_a, **_k: None)

    def chat(**kwargs):
        posts.append(kwargs)
        return iter([_prime_final_chunk()])

    monkeypatch.setattr(llama_client, "chat_completions", chat)
    outcome = []
    primer = real_thread(target=lambda: outcome.append(engine.prime_prompt_cache()), daemon=True)
    primer.start()
    try:
        assert ready.wait(2)
        engine.abort_prime(wait=0)
    finally:
        resume.set()
        primer.join(2)
    assert outcome == [client_engine.PrimeOutcome(False, "aborted", None)], "已取消的登記不能記成 sent"
    assert posts == []
    assert not primer.is_alive() and not engine.priming
    assert _prime_rows(tmp_path / "metrics.jsonl") == []


@pytest.mark.smoke
def test_a_prime_http_worker_scheduled_after_abort_never_starts_the_post(
    engine_factory, monkeypatch, tmp_path
):
    """R2-B02b：最後一次 epoch 檢查後、HTTP worker 開始前取消，不得晚送。"""
    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    engine = engine_factory()
    ready, resume = threading.Event(), threading.Event()
    real_thread = threading.Thread

    def delayed_http_thread(*args, **kwargs):
        if kwargs.get("name") == "codetrail-prime-http":
            target = kwargs["target"]

            def delayed():
                ready.set()
                resume.wait(3)
                target()

            kwargs["target"] = delayed
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(client_engine.threading, "Thread", delayed_http_thread)
    posts = []
    monkeypatch.setattr(llama_client, "get_slots", lambda *_a, **_k: None)

    def chat(**kwargs):
        posts.append(kwargs)
        return iter([_prime_final_chunk()])

    monkeypatch.setattr(llama_client, "chat_completions", chat)
    outcome = []
    primer = real_thread(target=lambda: outcome.append(engine.prime_prompt_cache()), daemon=True)
    primer.start()
    try:
        assert ready.wait(2)
        engine.abort_prime()
    finally:
        resume.set()
        primer.join(2)
        assert _prime_threads_gone()
    assert posts == [], "已取消後才排到 CPU 的 HTTP worker 仍發出舊 POST"
    assert outcome == [client_engine.PrimeOutcome(False, "aborted", None)]
    assert not engine.priming
    assert engine.model_lock.acquire(blocking=False)
    engine.model_lock.release()
    assert _prime_rows(tmp_path / "metrics.jsonl") == []


@pytest.mark.smoke
def test_a_prime_arriving_during_session_creation_cannot_keep_the_old_history_alive(
    engine_factory, monkeypatch, tmp_path
):
    """R2-B02c：abort 到 create 完成的空窗，晚到預熱不能仍送舊歷史。"""
    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    monkeypatch.setattr(llama_client, "get_slots", lambda *_a, **_k: None)
    engine = engine_factory()
    engine.messages = [{"role": "user", "content": "old session"}]
    ready, resume, finished = threading.Event(), threading.Event(), threading.Event()
    real_create = engine.store.create

    def paused_create():
        ready.set()
        resume.wait(3)
        return real_create()

    monkeypatch.setattr(engine.store, "create", paused_create)
    stream = _BlockingStream()
    monkeypatch.setattr(llama_client, "chat_completions", lambda **_k: stream)
    outcome = []

    def prime():
        outcome.append(engine.prime_prompt_cache())
        finished.set()

    switcher = threading.Thread(target=engine.new_session, daemon=True)
    primer = threading.Thread(target=prime, daemon=True)
    switcher.start()
    assert ready.wait(2)
    primer.start()
    try:
        # 新實作會拒絕這次准入；舊實作則已讀到串流第二個 chunk。
        deadline = time.monotonic() + 1
        while not finished.is_set() and stream._first and time.monotonic() < deadline:
            time.sleep(0.005)
        resume.set()
        switcher.join(1)
        primer.join(0.8)
        assert not primer.is_alive(), "session 已換，空窗內登記的舊預熱仍未被中止"
        assert not switcher.is_alive()
        assert outcome == [client_engine.PrimeOutcome(False, "aborted", None)]
        assert engine.messages == []
        assert not engine.priming
        assert engine.model_lock.acquire(blocking=False)
        engine.model_lock.release()
    finally:
        resume.set()
        engine.abort_prime(wait=0)
        switcher.join(2)
        primer.join(2)
    assert _prime_rows(tmp_path / "metrics.jsonl") == []


@pytest.mark.smoke
def test_failed_session_creation_does_not_disable_future_priming(engine_factory, monkeypatch, tmp_path):
    """轉換准入閘的錯誤路徑：create 失敗保留 session，之後預熱仍可正常准入。"""
    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(tmp_path / "metrics.jsonl"))
    engine = engine_factory(store=_FlakyStore())
    engine.messages = [{"role": "user", "content": "unchanged"}]
    before, session = copy.deepcopy(engine.messages), engine.session_id
    with pytest.raises(OSError):
        engine.new_session()
    assert engine.messages == before and engine.session_id == session
    monkeypatch.setattr(llama_client, "get_slots", lambda *_a, **_k: None)
    posts = []

    def chat(**kwargs):
        posts.append(kwargs)
        return iter([_prime_final_chunk()])

    monkeypatch.setattr(llama_client, "chat_completions", chat)
    assert engine.prime_prompt_cache().sent is True
    assert posts[0]["messages"][-1] == {"role": "user", "content": "unchanged"}
    assert engine.messages == before and engine.session_id == session
