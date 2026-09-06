"""codetrail_chat / client_events 的前端契約(headless 與事件流)。

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
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import client_engine  # noqa: E402
import client_events  # noqa: E402
import client_mcp  # noqa: E402
import client_policy  # noqa: E402
import client_prompt  # noqa: E402
import client_store  # noqa: E402
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
    exit_code = codetrail_chat.main(["run", "--root", str(root), "hi"])
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
    codetrail_chat.main(["run", "--root", str(root), "--persist", "hi"])
    capsys.readouterr()
    assert len(client_store.SessionStore(root).list_sessions()) == 1


@pytest.mark.smoke
def test_the_interactive_namespace_builds_engine_options(tmp_path):
    """互動路徑的 namespace **沒有** `--policy`(它只掛在 `run` 上)。

    紅燈長這樣:`AttributeError: 'Namespace' object has no attribute 'policy'` ——
    而且要跑完整套 preflight(profile / 模型 / n_ctx / 附屬 server / canary)之後
    才炸,所以任何只用手捏 namespace 的測試都看不到。這裡用**真的** parser 解出來
    的那一個。
    """
    args = codetrail_chat.build_parser().parse_args(["-c"])
    options = codetrail_chat._engine_options(
        tmp_path, args, types.SimpleNamespace(model="m", n_ctx=8192)
    )
    assert options.policy.name == "interactive"
    assert options.model == "m" and options.n_ctx == 8192


@pytest.mark.smoke
def test_the_readonly_policy_is_only_on_the_internal_entry():
    """`--policy` 不再是頂層旗標:使用者入口沒有它,只有內部的 `run` 有。

    留在頂層等於「互動 session 也能被要求跑 readonly」——那不是使用者需要的東西,
    而且它會出現在 `aicode --help` 裡。
    """
    parser = codetrail_chat.build_parser()
    args = parser.parse_args(["run", "--policy", "readonly", "hi"])
    assert codetrail_chat.POLICIES[args.policy] is client_policy.ReadOnlyPolicy
    with pytest.raises(SystemExit):
        parser.parse_args(["--policy", "readonly"])


@pytest.mark.smoke
def test_the_second_layer_travels_as_argv_not_environment(tmp_path):
    """第一層是客戶端的 deny;第二層是 MCP server 自己也關掉寫入與執行。

    第二層走 **argv**(`--readonly`),而且子行程的環境在交出去之前把整組
    `AICODE_* / AI_CODE_* / CODETRAIL_*` 剝掉 —— 殼層裡殘留的
    `AI_CODE_PATCH=1` 不得把它翻回來。
    """
    import client_mcp as mcp_module

    client = mcp_module.McpClient(tmp_path, readonly=True)
    assert client.readonly is True
    assert client._argv[-1] == "--readonly"  # noqa: SLF001 - 同一個包
    assert mcp_module.McpClient(tmp_path)._argv[-1] != "--readonly"  # noqa: SLF001


@pytest.mark.smoke
def test_the_server_actually_honours_readonly(tmp_path, monkeypatch):
    """第二層不是宣告而已:真的起一次 server,確認寫入工具被關掉。

    而且它是 **argv**:同時把舊世代那三個環境變數設成「開」,它們不得把
    `--readonly` 翻回來(殼層裡殘留的同名變數是真實情境 —— 兩份安裝共用一台機器)。

    2026-09-04(總審 F1-9):污染改放在**父行程的環境**(`monkeypatch.setenv`),
    不再經 `env=` 覆寫通道遞進去 —— 那條通道現在對三個前綴一律 fail-loud,
    而真實情境本來就是「殼層裡有殘留」,不是「呼叫端明確要求」。
    """
    import client_mcp as mcp_module

    for name in ("AI_CODE_PATCH", "AI_CODE_RUN_TESTS", "AI_CODE_ENABLE_BUILD_COMMANDS"):
        monkeypatch.setenv(name, "1")
    (tmp_path / "x.py").write_text("x = 1\n", encoding="utf-8")
    client = mcp_module.McpClient(
        tmp_path,
        readonly=True,
        # 設定來自 conftest 建的 tmp HOME(deployment.json 指向沒人聽的 port);
        # 附屬 server 的硬閘用 argv 跳過。
        argv=[
            sys.executable,
            str(mcp_module.SERVER_SCRIPT),
            "--root",
            str(tmp_path),
            "--readonly",
            "--skip-aux-preflight",
        ],
        env={"XDG_STATE_HOME": str(tmp_path / ".state")},
        start_timeout=120.0,
    )
    assert "--readonly" in client._argv  # noqa: SLF001 - 同一個包
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
            ["run", "--root", str(root), "--session", "20260101T000000-abcdef01", "hi"]
        )


class _FakeMcpClient(_FakeMcp):
    def start(self):
        return None

    def close(self):
        return None


# ============================================================
# readonly:連客戶端自己的 context metrics 也要關
# ============================================================
class _ReadonlyFakeMcpClient(_FakeMcpClient):
    readonly = True


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
    assert codetrail_chat.main(["run", "--root", str(root), "--policy", "readonly", "hi"]) == 0
    capsys.readouterr()
    assert not (root / ".codetrail" / "context_metrics.jsonl").exists()
    assert config.CTX_METRICS_ENABLED is False


# ── --model 走跟 wrapper 同一套正規化 ──

@pytest.mark.smoke
def test_a_provider_prefixed_model_is_normalised_like_the_wrapper(tmp_path):
    """`--model llamacpp/foo` 以前原樣進 EngineOptions,wrapper 與客戶端就用了兩個不同的
    模型名稱;外部 provider 一律拒絕。"""
    assert codetrail_chat._cli_model("llamacpp/foo") == "foo"
    assert codetrail_chat._cli_model("foo") == "foo"
    assert codetrail_chat._cli_model("  ") == ""
    with pytest.raises(SystemExit):
        codetrail_chat._cli_model("openai/gpt-4o")


# ── 總審 F1-11:headless `run` 也要跑 idle 壓縮,並把結果放進事件流 ──


@pytest.mark.smoke
def test_headless_run_compacts_after_a_completed_turn_and_reports_it(tmp_path, monkeypatch, capsys):
    """`session_eval --keep-compaction` 量的是壓縮品質;headless 不壓等於量一個
    不存在的東西,而 identity 上還寫著 `codetrail` mode。

    唯一建立 Compactor 的地方以前在互動 TUI 分支。這裡驗:headless 答完(finish=stop)
    之後會呼叫 `Compactor.compact()`,而且事件流裡出現 `compaction` 事件。
    """
    import client_compaction
    import llama_client

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    cfg = tmp_path / "client.json"
    cfg.write_text(json.dumps({"schema": 1, "compaction_mode": "codetrail"}), encoding="utf-8")
    cfg.chmod(0o600)
    tmp_path.chmod(0o700)
    monkeypatch.setattr(client_mcp, "shared_client", lambda *_a, **_k: _FakeMcpClient())
    monkeypatch.setattr(
        llama_client, "chat_completions",
        lambda **_k: iter([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]),
    )
    monkeypatch.setattr(codetrail_chat.config, "require_main_model", lambda: "m")
    calls: list[str] = []

    def fake_compact(self, *, manual=False):
        calls.append("manual" if manual else "idle")
        return client_compaction.CompactionOutcome("compacted", "threshold", "壓縮完成")

    monkeypatch.setattr(client_compaction.Compactor, "compact", fake_compact)
    exit_code = codetrail_chat.main(
        ["run", "--root", str(root), "--client-config", str(cfg), "hi"]
    )
    assert exit_code == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert calls == ["idle"], calls
    kinds = [e["type"] for e in events]
    assert "compaction" in kinds, kinds
    assert kinds[-1] == "step_finish", "終結事件仍然要在最後"
