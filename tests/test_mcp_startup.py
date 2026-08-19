"""mcp_server.py 的啟動閘:AICODE_ROOT 驗證 + 真的能初始化到 listening。

合併自 tests/test_mcp_root_safety.py 與 tests/test_mcp_smoke.py(2026-08-20)。

root 驗證的實作在 root_safety.py:mcp_server.py 匯入它,scripts/index_stats.py 也
匯入同一份 —— 維護 CLI 不能 import mcp_server(會拉起 FastMCP / KnowledgeBase /
CodeRAG),又不准另寫一套 root 驗證。所以這裡分三層守:
1. 純函式層:validate_aicode_root 的每個拒絕理由(in-process,零成本)。
2. 接線層:mcp_server.py 原始碼真的有匯入且呼叫它(靜態檢查)。
3. 端對端層:真的 spawn 一次 server,確認拒絕路徑會 exit≠0、正常路徑會 listening。

第 3 層原本有四條 subprocess case(root='/'、$HOME、$HOME+override、正常),
其中 $HOME 與 $HOME+override 兩條與第 1 層完全重疊,各花約 0.45s。2026-08-20 移除
那兩條,保留 root='/' 當拒絕路徑的端對端錨點。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from root_safety import validate_aicode_root as _validate
from tests._harness import REPO_ROOT, spawn_mcp, terminate_proc, wait_for_marker

# smoke:安全層(AGENTS.md §2.1 第 2 款「無聲失敗風險的契約」)
# AGENTS.md §3 安全檢查點:mcp_server 啟動時的 AICODE_ROOT 驗證與 sandbox root 設定。
pytestmark = pytest.mark.smoke

# CI 沒裝 mcp 時 skip;日常 OpenCode 路線需要 mcp。
pytest.importorskip("mcp", reason="mcp 套件未安裝;OpenCode + MCP 路線才需要")


# ---- 1. validate_aicode_root 純函式 ---------------------------------------


def test_rejects_empty_root():
    resolved, err = _validate(None, "/home/x", allow_home_override=False)
    assert resolved is None
    assert err and "AICODE_ROOT" in err


def test_rejects_root_slash():
    resolved, err = _validate("/", "/home/x", allow_home_override=False)
    assert resolved is None
    assert err and "/" in err


def test_rejects_home(tmp_path: Path):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    resolved, err = _validate(str(fake_home), str(fake_home), allow_home_override=False)
    assert resolved is None
    assert err and "$HOME" in err


def test_allows_home_when_overridden(tmp_path: Path):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    resolved, err = _validate(str(fake_home), str(fake_home), allow_home_override=True)
    assert err is None
    assert resolved == str(fake_home.resolve())


def test_allows_normal_subdir(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    resolved, err = _validate(str(project), str(tmp_path), allow_home_override=False)
    assert err is None
    assert resolved == str(project.resolve())


def test_rejects_nonexistent_dir(tmp_path: Path):
    nope = tmp_path / "nope"
    resolved, err = _validate(str(nope), "/home/x", allow_home_override=False)
    assert resolved is None
    assert err and "不是目錄" in err


# ---- 2. 接線:別讓檢查被靜悄悄拿掉 ----------------------------------------


def test_mcp_server_still_wires_up_root_validation():
    """mcp_server.py 必須匯入並實際呼叫 root 檢查 —— 別讓它被靜悄悄拿掉。"""
    src = (REPO_ROOT / "mcp_server.py").read_text(encoding="utf-8")
    assert "from root_safety import validate_aicode_root" in src, (
        "mcp_server.py 沒有匯入 root_safety.validate_aicode_root — root safety 檢查被砍了?"
    )
    assert "_validate_aicode_root(" in src, "mcp_server.py 沒有呼叫 root 檢查"


def test_fastmcp_v1_import_contract():
    """requirements 不得解出已移除現行 import path 的 MCP SDK 2.x。"""
    from mcp.server.fastmcp import FastMCP

    assert FastMCP is not None


# ---- 3. 端對端 -------------------------------------------------------------


def test_mcp_server_initializes_and_is_listenable(tmp_path: Path):
    """正常 root 下,mcp_server.py 能走完所有初始化並進入 listening 狀態。

    不需要 llama-server、不下載模型、不跑 inference:只看 mcp.run() 前最後一條
    stderr 里程碑,看到就代表 import → root 檢查 → KnowledgeBase / CodeRAG /
    ToolExecutor → FastMCP 全部構造成功。
    """
    project = tmp_path / "fakeproj"
    project.mkdir()
    (project / "README.md").write_text("# fake\n", encoding="utf-8")

    proc = spawn_mcp(project)
    try:
        stderr = wait_for_marker(proc)
        assert "server ready, listening on stdio" in stderr, (
            f"mcp_server.py 沒走到 listening 階段。stderr 摘錄:\n{stderr[-2000:]}"
        )
        assert "Traceback" not in stderr, stderr[-2000:]
        assert "FATAL" not in stderr, stderr[-2000:]
        assert "ModuleNotFoundError" not in stderr, stderr[-2000:]
    finally:
        terminate_proc(proc)


def test_mcp_server_rejects_root_slash():
    """root='/' 必須在啟動階段被拒絕(配 stderr [FATAL])。

    拒絕路徑的端對端錨點:證明 root_safety 的判斷真的會讓 server exit≠0,
    而不只是回傳一個沒人理的錯誤字串。
    """
    proc = spawn_mcp(Path("/"))
    try:
        try:
            _, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate()
        out = (stderr or b"").decode("utf-8", errors="replace")
        assert proc.returncode != 0, f"應該 exit≠0,實際 {proc.returncode}\n{out}"
        assert "FATAL" in out and ("/" in out or "AICODE_ROOT" in out)
    finally:
        terminate_proc(proc)
