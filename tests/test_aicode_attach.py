"""`aicode attach` 子指令,以及「沒有子指令時不得誤觸 web/attach」的回歸。

從 test_cli.py 拆出(2026-08-20)。
"""

from __future__ import annotations

from tests._harness import (
    read_stub_args,
    run_aicode_subcmd_with_stub,
)

# ---- aicode attach -------------------------------------------------------


def test_aicode_attach_default_url(tmp_path):
    result, args_file = run_aicode_subcmd_with_stub(tmp_path, ["attach"], set_model=False)

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert read_stub_args(args_file) == ["attach", "http://127.0.0.1:4096"]


def test_aicode_attach_respects_aicode_web_port_env(tmp_path):
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["attach"], env_extra={"AICODE_WEB_PORT": "5000"}, set_model=False
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert read_stub_args(args_file) == ["attach", "http://127.0.0.1:5000"]


def test_aicode_attach_explicit_url_and_flags_forwarded(tmp_path):
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["attach", "http://host:9000", "-s", "SID", "-c"], set_model=False
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert read_stub_args(args_file) == ["attach", "http://host:9000", "-s", "SID", "-c"]


def test_aicode_attach_flag_only_uses_default_url(tmp_path):
    """第一個參數是 flag(非 url)時,用預設 url 並把 flag 轉發。"""
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["attach", "-c"], set_model=False
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert read_stub_args(args_file) == ["attach", "http://127.0.0.1:4096", "-c"]


def test_aicode_attach_is_thin_no_wrapper_no_sandbox(tmp_path):
    """attach 是純 client:不做沙箱 root 檢查(AICODE_ROOT=/ 也不擋)、不準備 MCP wrapper。"""
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["attach"], env_extra={"AICODE_ROOT": "/"}, set_model=False
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert read_stub_args(args_file) == ["attach", "http://127.0.0.1:4096"]
    # 純 client 不應建立 .opencode/run-codetrail-mcp launcher
    assert not (tmp_path / "project" / ".opencode" / "run-codetrail-mcp").exists()

# ---- regression: 既有 standalone TUI 路徑不受影響 -------------------------


def test_aicode_no_subcommand_does_not_trigger_web_or_attach(tmp_path):
    """spec E.4:無參數 → exec 純 opencode,不含 web/attach 子指令。"""
    result, args_file = run_aicode_subcmd_with_stub(tmp_path, [])

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    args = read_stub_args(args_file)
    assert args == []
    assert "web" not in args and "attach" not in args


def test_aicode_non_subcommand_first_arg_forwarded_verbatim(tmp_path):
    """第一個位置參數不是 web/attach(例如專案路徑)→ 完全走現行語義,原樣轉發。"""
    result, args_file = run_aicode_subcmd_with_stub(tmp_path, ["somedir"])

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert read_stub_args(args_file) == ["somedir"]
