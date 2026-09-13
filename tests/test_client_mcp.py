"""client_mcp 的取消契約與 stdio 邊界。

這裡守的是 AGENTS.md §2 的新檢查點:
  - SDK 在 read timeout / task 取消時**不會**送 notifications/cancelled
    ——那正是客戶端不用 SDK 的 ClientSession 生命週期的理由,所以用契約測試
    釘住,不是寫在註解裡。
  - CodeTrail 客戶端自己配發 request id,Ctrl-C 與 timeout 兩種情況都送取消,
    並且真的讓 server 收掉它的 ingest process group。
  - 寬限期過仍無回應 → SIGTERM 該 instance,**所有**進行中的呼叫回成 error,
    再重新 spawn。
  - 每次呼叫的 read timeout 是固定的,呼叫端不得放寬。
  - MCP stderr 預設不落檔。
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import client_mcp  # noqa: E402
import config  # noqa: E402
from mcp_contract import PUBLIC_TOOL_ORDER  # noqa: E402

pytestmark = pytest.mark.smoke

STUB = Path(__file__).resolve().parent / "fixtures" / "stub_mcp_server.py"
SLOW = "ingest_document"
FAST = "list_dir"


def _stub_client(tmp_path: Path, **env: str) -> client_mcp.McpClient:
    return client_mcp.McpClient(
        tmp_path,
        argv=[sys.executable, str(STUB)],
        env=env,
        cancel_grace=1.0,
        terminate_grace=2.0,
        start_timeout=30.0,
    )


def _read_notifications(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _cancellations(path: Path) -> list[dict]:
    return [n for n in _read_notifications(path) if n.get("method") == "notifications/cancelled"]


def _pid_alive(pid: int | None) -> bool:
    """子行程由 Popen.wait() 收屍,所以走掉之後 pid 直接不存在。"""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not expected for our own child
        return True
    return True


def _pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ---------------------------------------------------------------- SDK contract
def test_the_sdk_never_sends_cancelled_on_its_own(tmp_path):
    """SDK 的 read timeout 與 task 取消都不會送 notifications/cancelled。

    這是客戶端自己實作 stdio JSON-RPC(而不是用 SDK 的 ClientSession)的理由:
    相信 SDK 會處理的話,ingest 期間的 Ctrl-C 會讓 RAG 子行程在背景繼續寫
    knowledge.json。
    """
    log = tmp_path / "notifications.jsonl"
    env = os.environ.copy()
    env.update({"STUB_SLOW_SECONDS": "30", "STUB_CANCEL_LOG": str(log)})

    async def _drive() -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable, args=[str(STUB)], env=env, cwd=str(tmp_path)
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # (a) read timeout
                with pytest.raises(Exception):
                    await session.call_tool(
                        SLOW, {}, read_timeout_seconds=timedelta(seconds=0.5)
                    )
                # (b) 呼叫端 task 被取消
                task = asyncio.ensure_future(session.call_tool(SLOW, {}))
                await asyncio.sleep(0.5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await asyncio.sleep(0.3)

    asyncio.run(_drive())
    methods = [n.get("method") for n in _read_notifications(log)]
    assert "notifications/cancelled" not in methods, methods


# ------------------------------------------------------------ our cancellation
def test_a_client_timeout_sends_cancelled_with_the_real_request_id(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MCP_CALL_TIMEOUT_SECONDS", 1)
    log = tmp_path / "notifications.jsonl"
    client = _stub_client(tmp_path, STUB_SLOW_SECONDS="30", STUB_CANCEL_LOG=str(log))
    try:
        client.start()
        pending = client.begin_call(SLOW, {})
        with pytest.raises(client_mcp.McpCallTimeoutError):
            pending.result()
        sent = _cancellations(log)
        assert sent, _read_notifications(log)
        assert sent[0]["params"]["requestId"] == pending.request_id
    finally:
        client.close()


def test_an_interrupt_cancels_the_in_flight_call(tmp_path):
    """真的送 SIGINT,走 call() 的 KeyboardInterrupt 分支。"""
    log = tmp_path / "notifications.jsonl"
    client = _stub_client(tmp_path, STUB_SLOW_SECONDS="30", STUB_CANCEL_LOG=str(log))
    try:
        client.start()
        target = os.getpid()
        timer = threading.Timer(0.8, lambda: os.kill(target, signal.SIGINT))
        timer.start()
        started = time.monotonic()
        with pytest.raises(client_mcp.McpCallCancelledError):
            client.call(SLOW, {})
        timer.cancel()
        assert time.monotonic() - started < 20
        assert _wait_until(lambda: bool(_cancellations(log)))
        assert _cancellations(log)[0]["params"]["reason"] == "user interrupt"
    finally:
        client.close()


def test_cancelling_reclaims_the_servers_child_process_group(tmp_path, monkeypatch):
    """取消必須真的傳到 server,讓它收掉自己的 ingest process group。

    server 端「被取消就回收子行程」由 tests/test_mcp_ingest.py 守;這裡守的是
    另一半——client 真的把取消送到了,而且送的是**那一次呼叫**的 id。
    """
    monkeypatch.setattr(config, "MCP_CALL_TIMEOUT_SECONDS", 2)
    pgid_file = tmp_path / "pgid"
    client = _stub_client(
        tmp_path,
        STUB_SLOW_SECONDS="120",
        STUB_SPAWN_CHILD="1",
        STUB_CHILD_PGID=str(pgid_file),
    )
    try:
        client.start()
        pending = client.begin_call(SLOW, {})
        assert _wait_until(lambda: pgid_file.exists())
        pgid = int(pgid_file.read_text(encoding="utf-8").strip())
        assert _pgid_alive(pgid)
        pending.cancel(reason="user interrupt")
        assert _wait_until(lambda: not _pgid_alive(pgid), timeout=15)
    finally:
        client.close()


def test_a_server_that_ignores_cancellation_is_terminated_and_respawned(tmp_path, monkeypatch):
    """寬限期過仍無回應 → SIGTERM,所有進行中的呼叫回成 error,再重新 spawn。"""
    monkeypatch.setattr(config, "MCP_CALL_TIMEOUT_SECONDS", 1)
    log = tmp_path / "notifications.jsonl"
    client = _stub_client(
        tmp_path,
        STUB_SLOW_SECONDS="120",
        STUB_IGNORE_CANCEL="1",
        STUB_CANCEL_LOG=str(log),
    )
    try:
        client.start()
        first_pid = client.pid
        # 第二個呼叫也在飛,它必須跟著回成 error(不是永遠等下去)。
        # 用同一個慢工具:已經完成的呼叫不該被事後改成失敗,所以 bystander
        # 必須是真的還在飛的那一種。
        bystander = client.begin_call(SLOW, {"path": "other"})
        doomed = client.begin_call(SLOW, {})
        with pytest.raises(client_mcp.McpCallTimeoutError):
            doomed.result()
        # 取消通知還是要送出去:SIGTERM 是 fallback,不是替代品。
        assert _cancellations(log), _read_notifications(log)
        assert _wait_until(lambda: not _pid_alive(first_pid))
        assert client.pid != first_pid
        assert client.generation == 2
        with pytest.raises(client_mcp.McpClientError):
            bystander.result()
        # 重新 spawn 之後照樣能用。
        assert client.call(FAST, {}).text == f"{FAST} done"
    finally:
        client.close()


def test_a_server_that_ignores_sigterm_is_killed(tmp_path):
    client = _stub_client(tmp_path, STUB_SLOW_SECONDS="0", STUB_IGNORE_SIGTERM="1")
    client.start()
    pid = client.pid
    client.close()
    assert _wait_until(lambda: not _pid_alive(pid), timeout=15)


def test_concurrent_calls_each_cancel_their_own_request(tmp_path, monkeypatch):
    """並行呼叫時 request id 不得綁錯:取消 B 不能收掉 A。"""
    monkeypatch.setattr(config, "MCP_CALL_TIMEOUT_SECONDS", 30)
    log = tmp_path / "notifications.jsonl"
    client = _stub_client(tmp_path, STUB_SLOW_SECONDS="20", STUB_CANCEL_LOG=str(log))
    try:
        client.start()
        first = client.begin_call(SLOW, {"path": "a"})
        second = client.begin_call(SLOW, {"path": "b"})
        assert first.request_id != second.request_id
        second.cancel(reason="user interrupt")
        sent = _cancellations(log)
        assert [n["params"]["requestId"] for n in sent] == [second.request_id]
        assert not first.done()
        first.cancel(reason="user interrupt")
    finally:
        client.close()


# ------------------------------------------------------------------- contracts
def test_the_read_timeout_is_fixed_and_cannot_be_widened_by_callers():
    """`call()` / `result()` 都沒有 timeout 參數,唯一來源是 config 常數。"""
    import inspect

    assert "timeout" not in inspect.signature(client_mcp.McpClient.call).parameters
    assert "timeout" not in inspect.signature(client_mcp.PendingCall.result).parameters
    assert config.MCP_CALL_TIMEOUT_SECONDS >= 660
    assert client_mcp.call_timeout_seconds() == float(config.MCP_CALL_TIMEOUT_SECONDS)


def test_the_timeout_constant_is_read_dynamically(monkeypatch):
    monkeypatch.setattr(config, "MCP_CALL_TIMEOUT_SECONDS", 3)
    assert client_mcp.call_timeout_seconds() == 3.0


# ------------------------------------------------------------------- stdio/env
def test_mcp_stderr_is_not_persisted_by_default(tmp_path, monkeypatch):
    """MCP stderr 預設不落檔(它含查詢原文與絕對路徑)。

    2026-09-04:`CODETRAIL_MCP_STDERR_LOG` 刪除,落檔只能由呼叫端明確指定
    (`McpClient(stderr_log=...)`)。行為為什麼該變:殼層裡一個忘了 unset 的值
    就等於每個 session 都把 NDA 內容寫進一個檔,而使用者不會知道。
    """
    monkeypatch.setenv("CODETRAIL_MCP_STDERR_LOG", str(tmp_path / "leak.log"))
    assert not hasattr(client_mcp, "STDERR_LOG_ENV")
    before = set(tmp_path.rglob("*"))
    client = _stub_client(tmp_path, STUB_SLOW_SECONDS="0")
    try:
        client.start()
        client.call(FAST, {})
    finally:
        client.close()
    assert set(tmp_path.rglob("*")) == before


def test_the_stderr_tail_is_bounded(tmp_path):
    client = _stub_client(tmp_path, STUB_SLOW_SECONDS="0")
    try:
        client.start()
        noise = b"x" * (client_mcp.STDERR_TAIL_BYTES * 4)
        client._stderr_tail.append(noise)  # noqa: SLF001 - 直接驗上限
        client._stderr_bytes += len(noise)  # noqa: SLF001
        assert len(client.stderr_tail()) <= client_mcp.STDERR_TAIL_BYTES
    finally:
        client.close()


def test_an_explicit_stderr_log_is_owner_only(tmp_path):
    target = tmp_path / "mcp-stderr.log"
    client = client_mcp.McpClient(
        tmp_path,
        argv=[sys.executable, str(STUB)],
        env={"STUB_SLOW_SECONDS": "0"},
        start_timeout=30.0,
    )
    client._stderr_log = target  # noqa: SLF001 - 建構後指定,不動使用者環境
    try:
        client.start()
    finally:
        client.close()
    assert target.exists()
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_an_explicit_stderr_log_refuses_a_symlink(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("", encoding="utf-8")
    link = tmp_path / "link.log"
    link.symlink_to(victim)
    with pytest.raises(client_mcp.McpClientError, match="symlink"):
        client_mcp._open_stderr_log(link)  # noqa: SLF001


def test_tools_list_requires_unprefixed_mcp_names():
    listed = {
        "tools": [
            {"name": "codetrail_list_dir", "inputSchema": {"type": "object"}},
        ]
    }
    with pytest.raises(client_mcp.McpClientError, match="codetrail_"):
        client_mcp.tool_specs(listed)


def test_the_catalog_contract_pins_order():
    specs = tuple(
        client_mcp.ToolSpec(name=name, description="", input_schema={"type": "object"}, read_only=True)
        for name in PUBLIC_TOOL_ORDER
    )
    client_mcp.assert_public_catalog(specs)
    with pytest.raises(client_mcp.McpClientError):
        client_mcp.assert_public_catalog(specs[::-1])


def test_a_server_with_a_drifted_catalog_is_refused_at_startup(tmp_path, monkeypatch):
    """目錄驗證在 start() 就跑,不是只在測試裡跑。"""
    monkeypatch.setattr(client_mcp, "PUBLIC_TOOL_ORDER", ("nope",))
    client = _stub_client(tmp_path, STUB_SLOW_SECONDS="0")
    with pytest.raises(client_mcp.McpUnavailableError):
        client.start()
    client.close()


def test_only_the_text_blocks_reach_the_model():
    blocks = [
        {"type": "text", "text": "a"},
        {"type": "image", "data": "..."},
        {"type": "text", "text": "b"},
    ]
    assert client_mcp._text_blocks(blocks) == "a\nb"  # noqa: SLF001


def test_a_live_roundtrip_exposes_the_real_catalog(tmp_path):
    """真的起一次 mcp_server.py,確認裸名、順序與唯讀註記。"""
    (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")
    client = client_mcp.McpClient(
        tmp_path,
        # 設定來自 conftest 建的 tmp HOME(deployment.json 指向沒人聽的 port);
        # 附屬 server 的硬閘用 **argv** 跳過,不是環境變數 —— 子行程的環境在交
        # 出去之前已經被剝乾淨,用環境變數傳這個意圖必然失效。
        argv=[
            sys.executable,
            str(client_mcp.SERVER_SCRIPT),
            "--root",
            str(tmp_path),
            "--skip-aux-preflight",
        ],
        env={
            # 放在 root 旁邊:data flywheel 拒絕把落點放進被分析的專案(root 就是 tmp_path)。
            "XDG_STATE_HOME": str(tmp_path.parent / f"{tmp_path.name}.state"),
            "PYTHONIOENCODING": "utf-8",
        },
        start_timeout=120.0,
    )
    try:
        specs = client.tools()
        assert tuple(spec.name for spec in specs) == PUBLIC_TOOL_ORDER
        assert {spec.name for spec in specs if spec.read_only} >= {"list_dir", "read_file"}
        assert not any(spec.read_only for spec in specs if spec.name == "apply_patch")
        result = client.call("list_dir", {"path": "."})
        assert "hello.txt" in result.text
        assert client.openai_tools()[0]["function"]["name"] == "list_dir"
    finally:
        client.close()


def test_one_engine_process_keeps_exactly_one_mcp_instance(tmp_path):
    """shared_client 對同一個 root 永遠回同一個 client(一個子行程)。"""
    client_mcp.reset_shared_clients()
    try:
        first = client_mcp.shared_client(
            tmp_path,
            argv=[sys.executable, str(STUB)],
            env={"STUB_SLOW_SECONDS": "0"},
            start_timeout=30.0,
        )
        second = client_mcp.shared_client(tmp_path)
        assert first is second
        first.start()
        assert first.pid == second.pid
    finally:
        client_mcp.reset_shared_clients()


def test_closing_a_shared_client_finishes_before_a_replacement_starts(tmp_path):
    """關閉與替換在同一把鎖裡:同一個 root 不得短暫存在兩個 server。"""
    client_mcp.reset_shared_clients()
    try:
        first = client_mcp.shared_client(
            tmp_path,
            argv=[sys.executable, str(STUB)],
            env={"STUB_SLOW_SECONDS": "0"},
            start_timeout=30.0,
        )
        first.start()
        old_pid = first.pid
        first.close()
        # 換上來的那一個必須指向同一個 stub —— 不帶 argv 的話它會去啟動真的
        # mcp_server.py,測試就變成需要 aux server 的線上測試(AGENTS.md §4)。
        second = client_mcp.shared_client(
            tmp_path,
            argv=[sys.executable, str(STUB)],
            env={"STUB_SLOW_SECONDS": "0"},
            start_timeout=30.0,
        )
        assert second is not first
        second.start()
        assert second.pid != old_pid
        assert not _pid_alive(old_pid)
    finally:
        client_mcp.reset_shared_clients()


# ── 總審 F1-9:override 通道不得把剝掉的前綴加回去 ──


@pytest.mark.smoke
@pytest.mark.parametrize("name", ["AICODE_ROOT", "AI_CODE_PATCH", "CODETRAIL_CLIENT_CONFIG"])
def test_env_overrides_cannot_reintroduce_a_stripped_prefix(tmp_path, name):
    """覆寫通道不得把三個 CodeTrail 設定前綴加回子行程。

    `McpClient(env={"AI_CODE_PATCH": "1"})` 也必須拒絕,避免繞過 readonly policy。
    """
    with pytest.raises((client_mcp.McpClientError, ValueError)):
        client_mcp.child_env({name: "x"})
    with pytest.raises(client_mcp.McpClientError):
        client_mcp.McpClient(tmp_path, env={name: "x"})


# ── 總審 F1-10:replay 的隔離設定與 skip 意圖要以 argv 一路交到 MCP ──


@pytest.mark.smoke
def test_client_config_and_skip_aux_preflight_reach_the_server_argv(tmp_path):
    """`run --client-config` 與 `--skip-aux-preflight` 不能只到客戶端。

    MCP 是獨立行程,自己讀 HOME 的 client.json:replay 指定了臨時設定、而真實 HOME
    的檔壞掉,parent 讀對了、MCP 卻 exit 2;skip 只略過外層檢查,每個 MCP child
    仍跑硬 preflight 而失敗。兩個意圖都要在 server argv 上。
    """
    cfg = tmp_path / "client.json"
    client = client_mcp.McpClient(
        tmp_path, client_config=cfg, skip_aux_preflight=True
    )
    argv = client._argv  # noqa: SLF001
    assert argv[argv.index("--client-config") + 1] == str(cfg)
    assert "--skip-aux-preflight" in argv
    plain = client_mcp.McpClient(tmp_path)._argv  # noqa: SLF001
    assert "--client-config" not in plain and "--skip-aux-preflight" not in plain


# ── 總審第 12 輪:spawn 只有一個出口 ──


@pytest.mark.smoke
def test_process_env_run_is_the_only_spawn_exit_and_never_takes_env(monkeypatch):
    """`process_env.run/popen/check_output` 自己用 child_env() 算環境:殼層的 `AICODE_*` /
    `CODETRAIL_*` 一定進不去子行程,`overrides` 進得去,`env=` 一律 TypeError,帶三類前綴的
    overrides 一律 fail-loud。靜態 gate 只需要禁「別處出現 subprocess」,不必推導 env 來源。"""
    import sys

    import process_env

    monkeypatch.setenv("AICODE_MODEL", "bogus")
    monkeypatch.setenv("CODETRAIL_CLIENT_CONFIG", "secret")
    probe = "import os; print(os.environ.get('AICODE_MODEL'), os.environ.get('CODETRAIL_CLIENT_CONFIG'), os.environ.get('MARK'))"
    out = process_env.run([sys.executable, "-c", probe], overrides={"MARK": "x"}, capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["None", "None", "x"], out.stdout
    proc = process_env.popen([sys.executable, "-c", probe], stdout=process_env.PIPE, text=True)
    assert proc.communicate(timeout=30)[0].split() == ["None", "None", "None"]
    for exit_ in (process_env.run, process_env.popen, process_env.check_output):
        with pytest.raises(TypeError):
            exit_(["true"], env={})
    with pytest.raises(process_env.ChildEnvError):
        process_env.run(["true"], overrides={"AICODE_X": "1"})


@pytest.mark.smoke
def test_process_env_popen_class_is_not_a_raw_spawn_bypass(monkeypatch):
    """總審 F12-2:`process_env.Popen` 若只是原始 `subprocess.Popen` 的 re-export,直接呼叫
    就繞過 child_env(繼承整份污染殼層)。它得是會自己剝環境、不吃 `env=` 的東西。"""
    import sys

    import process_env

    monkeypatch.setenv("AICODE_MODEL", "round12-leak")
    monkeypatch.setenv("CODETRAIL_CLIENT_CONFIG", "round12-secret")
    probe = "import os; print(os.environ.get('AICODE_MODEL'), os.environ.get('CODETRAIL_CLIENT_CONFIG'))"
    proc = process_env.Popen([sys.executable, "-c", probe], stdout=process_env.PIPE, text=True)
    assert proc.communicate(timeout=30)[0].split() == ["None", "None"]
    with pytest.raises(TypeError):
        process_env.Popen(["true"], env={})


@pytest.mark.smoke
def test_child_environment_isolated_from_parent_and_previous_calls(monkeypatch):
    """剝除與覆寫只作用於子行程副本,不得污染父行程或下一次呼叫。"""
    import os
    import process_env

    settings = {"AICODE_MODEL": "parent-model", "AI_CODE_PATCH": "1", "CODETRAIL_CLIENT_CONFIG": "parent-config"}
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LANG", "C")
    child = process_env.child_env({"LANG": "C.UTF-8"})
    assert all(name not in child for name in settings)
    assert child["LANG"] == "C.UTF-8"
    child.update(settings)
    assert all(os.environ[name] == value for name, value in settings.items())
    assert os.environ["LANG"] == "C"
    following = process_env.child_env()
    assert all(name not in following for name in settings)
    assert following["LANG"] == "C"
