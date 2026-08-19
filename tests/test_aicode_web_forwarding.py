"""`aicode web` 的參數轉發:port / hostname 注入、額外參數直通、preflight-only 早退。

從 test_cli.py → test_aicode_web.py 再拆一層(2026-08-20)。每條測試都真的跑一次
aicode preflight(約 0.4s),17 條合起來是全套件最慢的單檔,把它拆成兩半讓分片
能同時吃。行為與 assertion 未變。
"""
from __future__ import annotations

from tests._harness import (
    OPENCODE_WEB_OLD_STUB,
    contains_subsequence,
    read_stub_args,
    run_aicode_subcmd_with_stub,
)


def test_aicode_web_default_port_and_hostname(tmp_path):
    """`aicode web` 注入固定 port 4096 + loopback hostname,並沿用既有前置(備好 MCP launcher)。"""
    result, args_file = run_aicode_subcmd_with_stub(tmp_path, ["web"])

    assert result.returncode == 0, f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    assert read_stub_args(args_file) == ["web", "--port", "4096", "--hostname", "127.0.0.1"]
    # web 沿用既有前置:CodeTrail MCP launcher 要被備好(對照 attach 的純 client)
    assert (tmp_path / "project" / ".opencode" / "run-codetrail-mcp").is_file()

def test_aicode_web_respects_aicode_web_port_env(tmp_path):
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["web"], env_extra={"AICODE_WEB_PORT": "5000"}
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert read_stub_args(args_file) == ["web", "--port", "5000", "--hostname", "127.0.0.1"]

def test_aicode_web_forwards_extra_args_and_user_port(tmp_path):
    """使用者自帶 --port 覆寫預設;其餘參數原樣轉發 opencode web。"""
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["web", "--port", "7000", "--cors", "https://example.test"]
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    args = read_stub_args(args_file)
    assert args[0] == "web"
    assert contains_subsequence(args, ["--port", "7000"])
    assert contains_subsequence(args, ["--hostname", "127.0.0.1"])
    assert contains_subsequence(args, ["--cors", "https://example.test"])
    # 不應重複注入 --port(使用者已給)
    assert args.count("--port") == 1

def test_aicode_web_missing_port_value_fails(tmp_path):
    result, args_file = run_aicode_subcmd_with_stub(tmp_path, ["web", "--port"])

    assert result.returncode != 0
    assert "--port" in result.stderr
    assert not args_file.exists()

def test_aicode_web_preflight_only_passes_checks_without_exec(tmp_path):
    """AICODE_PREFLIGHT_ONLY=1(aicode_web 前景預檢用):跑完全部前置後退出,不 exec OpenCode。"""
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["web"], env_extra={"AICODE_PREFLIGHT_ONLY": "1"}
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert "preflight-only" in result.stdout
    # 只有 web 能力偵測(web --help)會碰 stub;真正的 exec 不能發生
    assert not args_file.exists()

def test_aicode_preflight_only_tui_path_also_exits_before_exec(tmp_path):
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, [], env_extra={"AICODE_PREFLIGHT_ONLY": "1"}
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert "preflight-only" in result.stdout
    assert not args_file.exists()

def test_aicode_web_old_opencode_prints_upgrade_and_exits(tmp_path):
    """spec E.3:舊版 opencode(web --help 無 synopsis)→ 印升級指引後退出,不 exec。"""
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["web"], opencode_stub=OPENCODE_WEB_OLD_STUB
    )

    assert result.returncode != 0
    assert "opencode-ai@latest" in result.stderr
    assert not args_file.exists()
