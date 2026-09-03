"""codetrail_chat / client_tui / client_events 的前端契約。

事件流是 canary、routing eval 與 session_eval replay 共用的介面,所以它的形狀
與解析器是契約,不是實作細節:各自寫一份解析器時,同一次 run 在兩邊會得到不同
判定,而兩邊都不會報錯。

headless 預設 **ephemeral**:canary 與 eval 不得在使用者的 session 清單裡留下
對話。
"""
from __future__ import annotations

import io
import json
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
import client_tui  # noqa: E402
import codetrail_chat  # noqa: E402
from mcp_contract import PUBLIC_TOOL_ORDER  # noqa: E402

pytestmark = pytest.mark.smoke


class _FakeMcp:
    def __init__(self):
        self.calls = []

    def tools(self):
        return tuple(
            client_mcp.ToolSpec(
                name=name, description=name, input_schema={"type": "object"}, read_only=True
            )
            for name in PUBLIC_TOOL_ORDER
        )

    def call(self, name, arguments=None, **_kwargs):
        self.calls.append((name, dict(arguments or {})))
        return client_mcp.ToolCallResult(name, f"status: ok\n{name}", None, False)


def _engine(tmp_path, store=None, policy=None):
    options = client_engine.EngineOptions(
        root=tmp_path,
        model="m",
        base_url="http://127.0.0.1:65535",
        n_ctx=131072,
        policy=policy or client_policy.InteractivePolicy(),
    )
    engine = client_engine.Engine(
        options,
        mcp=_FakeMcp(),
        store=store or client_store.EphemeralSessionStore(tmp_path),
        system_prompt=client_prompt.SystemPrompt(text="SYSTEM"),
    )
    engine.load_tools()
    return engine


# ============================================================
# 事件格式與解析器
# ============================================================
def test_a_completed_tool_event_is_recognised_by_the_shared_parser():
    event = client_events.tool_event(
        "20260101T000000-abcdef01",
        tool="list_dir",
        call_id="c1",
        status=client_events.STATUS_COMPLETED,
        arguments={"path": ".", "depth": 1},
    )
    call = client_events.completed_tool_call(event)
    assert call is not None
    assert call.bare_tool == "list_dir"
    assert call.arguments == {"path": ".", "depth": 1}
    assert client_events.event_session_ids(event) == ["20260101T000000-abcdef01"]


def test_a_denied_or_failed_tool_is_not_a_completed_call():
    for status in (client_events.STATUS_DENIED, client_events.STATUS_ERROR):
        event = client_events.tool_event(
            "s", tool="apply_patch", call_id="c", status=status, arguments={}
        )
        assert client_events.completed_tool_call(event) is None


def test_a_tool_calls_step_is_not_terminal_but_stop_is():
    assert not client_events.is_terminal_event(
        client_events.step_finish_event("s", reason=client_events.REASON_TOOL_CALLS)
    )
    assert client_events.is_terminal_event(
        client_events.step_finish_event("s", reason=client_events.REASON_STOP)
    )


def test_reasoning_never_reaches_the_event_stream(tmp_path, monkeypatch):
    """thinking 只用於終端串流顯示,寫進 JSONL 等於逐字落到收集輸出的地方。"""
    import llama_client

    engine = _engine(tmp_path)
    monkeypatch.setattr(
        llama_client,
        "chat_completions",
        lambda **_k: iter(
            [{"choices": [{"delta": {"content": "a", "reasoning_content": "SECRET"}, "finish_reason": "stop"}]}]
        ),
    )
    events: list[dict] = []
    engine.send("hi", on_event=events.append)
    assert "SECRET" not in json.dumps(events, ensure_ascii=False)


def test_the_shared_parser_reads_our_own_stream():
    session = "20260101T000000-abcdef01"
    stream = "\n".join(
        client_events.dumps(event)
        for event in (
            client_events.session_event(session, root="/x", model="m"),
            client_events.step_finish_event(session, reason=client_events.REASON_TOOL_CALLS),
            client_events.tool_event(
                session, tool="list_dir", call_id="c", status="completed", arguments={"path": "."}
            ),
            client_events.text_event(session, "answer"),
            client_events.step_finish_event(
                session, reason=client_events.REASON_STOP, tokens={"input": 5, "output": 3}
            ),
        )
    )
    from scripts.eval_tool_routing import parse_event_stream

    trace = parse_event_stream(stream)
    assert [call.bare_tool for call in trace.completed_calls] == ["list_dir"]
    assert trace.assistant_text == "answer"
    assert trace.terminal is True
    assert trace.session_ids == (session,)
    assert (trace.prompt_tokens, trace.output_tokens) == (5, 3)


# ============================================================
# headless
# ============================================================
def test_headless_defaults_to_ephemeral(tmp_path, monkeypatch, capsys):
    """canary 與 eval 走這條路徑,不得在 session 清單裡留下對話。"""
    import llama_client

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(client_mcp, "shared_client", lambda *_a, **_k: _FakeMcpClient())
    monkeypatch.setattr(
        llama_client,
        "chat_completions",
        lambda **_k: iter([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]),
    )
    monkeypatch.setattr(codetrail_chat.config, "require_main_model", lambda: "m")
    exit_code = codetrail_chat.main(["--root", str(root), "run", "hi"])
    assert exit_code == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert [e["type"] for e in events] == ["session", "text", "step_finish"]
    assert client_store.SessionStore(root).list_sessions() == []


def test_headless_can_persist_when_asked(tmp_path, monkeypatch, capsys):
    import llama_client

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(client_mcp, "shared_client", lambda *_a, **_k: _FakeMcpClient())
    monkeypatch.setattr(
        llama_client,
        "chat_completions",
        lambda **_k: iter([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]),
    )
    monkeypatch.setattr(codetrail_chat.config, "require_main_model", lambda: "m")
    codetrail_chat.main(["--root", str(root), "run", "--persist", "hi"])
    capsys.readouterr()
    assert len(client_store.SessionStore(root).list_sessions()) == 1


def test_the_readonly_policy_is_selectable_from_the_cli():
    parser = codetrail_chat.build_parser()
    args = parser.parse_args(["--policy", "readonly", "run", "hi"])
    assert codetrail_chat.POLICIES[args.policy] is client_policy.ReadOnlyPolicy


def test_readonly_turns_off_the_servers_write_tools_as_a_second_layer():
    """第一層是客戶端的 deny;第二層是 MCP server 自己也關掉寫入與執行。

    只有第一層的話,任何繞過 policy 的路徑(bug、未來的新前端)都會直接寫到
    使用者的專案。context metrics 也要關:replay 的契約是前後 project state
    不變,而它預設寫進 `<root>/.codetrail/`。
    """
    env = codetrail_chat._server_env("readonly")
    assert env["AI_CODE_PATCH"] == "0"
    assert env["AI_CODE_RUN_TESTS"] == "0"
    assert env["AI_CODE_ENABLE_BUILD_COMMANDS"] == "0"
    assert env["AICODE_CTX_METRICS_ENABLED"] == "0"
    assert codetrail_chat._server_env("interactive") == {}


def test_the_server_actually_honours_the_readonly_env(tmp_path):
    """第二層不是宣告而已:真的起一次 server,確認寫入工具被關掉。"""
    import client_mcp as mcp_module

    (tmp_path / "x.py").write_text("x = 1\n", encoding="utf-8")
    client = mcp_module.McpClient(
        tmp_path,
        env={
            "XDG_STATE_HOME": str(tmp_path / ".state"),
            "AICODE_LLAMA_BASE_URL": "http://127.0.0.1:65535",
            "AICODE_MODEL": "example-code-model",
            "AICODE_REQUIRED_MODELS_CHECK_SKIP": "1",
            **codetrail_chat.READONLY_SERVER_ENV,
        },
        start_timeout=120.0,
    )
    try:
        result = client.call(
            "apply_patch",
            {"diff": "x.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"},
        )
    finally:
        client.close()
    assert "x = 1" in (tmp_path / "x.py").read_text(encoding="utf-8")
    assert result.is_error or "disabled" in result.text.lower() or "未啟用" in result.text


def test_run_with_a_session_requires_persist(tmp_path, monkeypatch):
    """headless 預設不落檔,所以 --session 沒有可以接續的對話。"""
    root = tmp_path / "project"
    root.mkdir()
    with pytest.raises(SystemExit, match="--persist"):
        codetrail_chat.main(
            ["--root", str(root), "--session", "20260101T000000-abcdef01", "run", "hi"]
        )


class _FakeMcpClient(_FakeMcp):
    def start(self):
        return None

    def close(self):
        return None


# ============================================================
# TUI
# ============================================================
def test_slash_commands_do_not_call_the_model(tmp_path, monkeypatch, capsys):
    engine = _engine(tmp_path)
    tui = client_tui.Tui(engine, state_dir=tmp_path / "state")
    assert tui._command("/help") is None
    assert tui._command("/tools") is None
    assert tui._command("/exit") is False
    output = capsys.readouterr().out
    assert "list_dir" in output
    assert "/compact" in output


def test_the_approval_prompt_shows_the_whole_patch(tmp_path, monkeypatch, capsys):
    engine = _engine(tmp_path)
    tui = client_tui.Tui(engine, state_dir=tmp_path / "state")
    patch = "--- a/x.py\n+++ b/x.py\n@@\n-old\n+new\n" * 30
    monkeypatch.setattr("builtins.input", lambda *_a: "n")
    granted = tui.approve(client_engine.ApprovalRequest("s", "apply_patch", {"diff": patch}))
    assert granted is False
    assert patch in capsys.readouterr().out


def test_an_eof_at_the_approval_prompt_is_a_refusal(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    tui = client_tui.Tui(engine, state_dir=tmp_path / "state")

    def _eof(*_args):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    assert tui.approve(client_engine.ApprovalRequest("s", "run_command", {})) is False


# ============================================================
# 總審第 1 輪回修:readonly 連客戶端自己的 metrics 也關;換 session 不消耗停用警告
# ============================================================
class _ReadonlyFakeMcpClient(_FakeMcpClient):
    _env_overrides = dict(codetrail_chat.READONLY_SERVER_ENV)


def test_a_readonly_run_never_writes_context_metrics_into_the_project(tmp_path, monkeypatch, capsys):
    """MCP 子行程的 metrics 由 env 關掉;寫 `<root>/.codetrail/context_metrics.jsonl` 的
    還有客戶端行程自己(engine 每一步都 log_metrics)。"""
    import config
    import llama_client

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(config, "CTX_METRICS_ENABLED", True)
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(client_mcp, "shared_client", lambda *_a, **_k: _ReadonlyFakeMcpClient())
    monkeypatch.setattr(
        llama_client, "chat_completions",
        lambda **_k: iter([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]),
    )
    monkeypatch.setattr(codetrail_chat.config, "require_main_model", lambda: "m")
    assert codetrail_chat.main(["--root", str(root), "--policy", "readonly", "run", "hi"]) == 0
    capsys.readouterr()
    assert not (root / ".codetrail" / "context_metrics.jsonl").exists()
    assert config.CTX_METRICS_ENABLED is False


def test_changing_session_rebinds_without_consuming_the_stop_notice(tmp_path):
    """`/new` / `/resume` 只重綁;「每個 session 只講一次」的停用警告留給送出當下。"""
    engine = _engine(tmp_path)
    calls = {"rebind": 0, "notice": 0}

    def _rebind():
        calls["rebind"] += 1

    def _notice():
        calls["notice"] += 1
        return "已停用"

    tui = client_tui.Tui(
        engine, state_dir=tmp_path / "state", before_send=_notice, on_session_change=_rebind
    )
    tui._session_changed()
    assert calls == {"rebind": 1, "notice": 0}


def test_new_session_resets_store_error_through_the_tui(tmp_path, capsys):
    engine = _engine(tmp_path)
    engine.store_error = "OSError: disk full"
    tui = client_tui.Tui(engine, state_dir=tmp_path / "state")
    tui._command("/new")
    assert engine.store_error is None


# ============================================================
# 總審第 2 輪回修:/new 失敗不得帶走 REPL;history 檔的防線
# ============================================================
class _FlakyStore(client_store.EphemeralSessionStore):
    def __init__(self, root):
        super().__init__(root)
        self.calls = 0

    def create(self, *args, **kwargs):
        self.calls += 1
        if self.calls > 1:
            raise OSError("disk full")
        return super().create(*args, **kwargs)


def test_a_failed_new_keeps_the_repl_and_the_current_session(tmp_path, capsys):
    engine = _engine(tmp_path, store=_FlakyStore(tmp_path))
    engine.messages = [{"role": "user", "content": "q"}]
    session = engine.session_id
    tui = client_tui.Tui(engine, state_dir=tmp_path / "state")
    assert tui._command("/new") is None
    assert engine.session_id == session and engine.messages == [{"role": "user", "content": "q"}]
    assert "無法開新對話" in capsys.readouterr().out


class _FakeReadline:
    def __init__(self, items):
        self.items = list(items)

    def get_current_history_length(self):
        return len(self.items)

    def get_history_item(self, index):
        return self.items[index - 1]


@pytest.mark.smoke
def test_saving_history_never_writes_through_a_hard_link(tmp_path):
    """舊做法先 O_TRUNC 開檔再驗 nlink:被指向的別人的檔案在拒絕之前就先被清空了。"""
    import os

    engine = _engine(tmp_path)
    state = tmp_path / "sessions" / "hash"
    state.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    os.link(victim, state / client_tui.HISTORY_FILENAME)
    tui = client_tui.Tui(engine, state_dir=state)
    tui._save_history(_FakeReadline(["問過的 NDA 問題"]))
    assert victim.read_text(encoding="utf-8") == "keep me\n"
    written = state / client_tui.HISTORY_FILENAME
    assert written.read_text(encoding="utf-8") == "問過的 NDA 問題\n"
    assert written.stat().st_nlink == 1 and oct(written.stat().st_mode & 0o777) == "0o600"


@pytest.mark.smoke
def test_saving_history_never_follows_a_symlink(tmp_path):
    engine = _engine(tmp_path)
    state = tmp_path / "sessions" / "hash"
    state.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    (state / client_tui.HISTORY_FILENAME).symlink_to(victim)
    tui = client_tui.Tui(engine, state_dir=state)
    tui._save_history(_FakeReadline(["問過的 NDA 問題"]))
    assert victim.read_text(encoding="utf-8") == "keep me\n"
    assert not (state / client_tui.HISTORY_FILENAME).is_symlink()


@pytest.mark.smoke
def test_loading_history_never_reads_through_a_symlink(tmp_path):
    """path-based read_history_file 會跟著 symlink 走,把別人的檔讀進歷史。"""
    readline = pytest.importorskip("readline")
    engine = _engine(tmp_path)
    state = tmp_path / "sessions" / "hash"
    state.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("SOMEONE ELSES SECRET LINE\n", encoding="utf-8")
    (state / client_tui.HISTORY_FILENAME).symlink_to(victim)
    readline.clear_history()
    client_tui.Tui(engine, state_dir=state)._setup_readline()
    items = [readline.get_history_item(i) for i in range(1, readline.get_current_history_length() + 1)]
    assert "SOMEONE ELSES SECRET LINE" not in items


# ── 總審第 3 輪回修(F3-6):頂層 --session 要傳進 attach ──

@pytest.mark.smoke
def test_a_global_session_reaches_the_attach_command(monkeypatch):
    """`codetrail_chat.py --session S attach`:頂層 --session 解析進 args.session,
    attach 以前只讀自己的 -s,指定的 session 被靜默忽略。"""
    seen: dict = {}

    def _run(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return 0

    import client_attach

    monkeypatch.setattr(client_attach, "run", _run)
    assert codetrail_chat.main(["--session", "S123", "attach"]) == 0
    assert seen["session"] == "S123"
    assert codetrail_chat.main(["--session", "S123", "attach", "-s", "S456"]) == 0
    assert seen["session"] == "S456"          # attach 自己的 -s 優先


# ── 總審第 4 輪回修(F4-3):--model 走跟 wrapper 同一套正規化 ──

@pytest.mark.smoke
def test_a_provider_prefixed_model_is_normalised_like_the_wrapper(tmp_path):
    """`--model llamacpp/foo` 以前原樣進 EngineOptions,web 與終端就用了兩個不同的模型名稱;
    外部 provider 一律拒絕。"""
    assert codetrail_chat._cli_model("llamacpp/foo") == "foo"
    assert codetrail_chat._cli_model("foo") == "foo"
    assert codetrail_chat._cli_model("  ") == ""
    with pytest.raises(SystemExit):
        codetrail_chat._cli_model("openai/gpt-4o")
