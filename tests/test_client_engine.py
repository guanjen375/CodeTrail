"""client_engine / client_prompt / client_policy / client_notify 的契約。

AGENTS.md §2 的新安全檢查點集中在這裡:
  - 送出去的那一份才算數:reasoning 剝除與 prune 只改 payload,session 檔與
    畫面保留原文;認不出最新真實使用者訊息就整段不動。
  - 權限 policy:readonly 必須 deny 全部 mutator(判準是 readOnlyHint,不是
    寫死名單);互動模式的六個 ask 工具沒核准就不得執行。
  - 只有工具結果的 text block 進模型;structuredContent 只給 UI / eval。
  - ingest marker 只認 ingest_document 的結果、只認行首。
"""
from __future__ import annotations

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


def test_interactive_policy_asks_for_the_six_write_tools():
    policy = client_policy.InteractivePolicy()
    assert client_policy.ASK_TOOLS == frozenset(
        {"apply_patch", "run_lint", "run_command", "remove_document", "record_lesson", "review_figures"}
    )
    for name in client_policy.ASK_TOOLS:
        assert policy.decide(name, read_only=False, arguments={}) is client_policy.Decision.ASK
    assert policy.decide("list_dir", read_only=True, arguments={}) is client_policy.Decision.ALLOW
    # 其餘一律 allow —— 與 OpenCode 時代的權限表逐條相同(codetrail_*: allow 再把
    # 六個覆成 ask)。改成「非唯讀就 ask」會讓一次 ingest 多跳一個核准框,那是
    # 使用者沒要求過的行為改變。fail-closed 的那一半在 ReadOnlyPolicy。
    for name in ("ingest_document", "import_external_file", "reload_knowledge_base"):
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
    monkeypatch.delenv(client_prompt.DISABLE_PROJECT_INSTRUCTIONS_ENV, raising=False)
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
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    (root / ".codetrail").mkdir(parents=True)
    (root / "AGENTS.md").write_text("PROJECT RULE", encoding="utf-8")
    # OpenCode 的 JS truthiness:任何非空值(含 "0")都代表關閉。
    monkeypatch.setenv(client_prompt.DISABLE_PROJECT_INSTRUCTIONS_ENV, "0")
    assert "PROJECT RULE" not in client_prompt.build_system_prompt(root).text


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
