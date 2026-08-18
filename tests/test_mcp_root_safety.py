"""AICODE_ROOT 安全檢查(root_safety.validate_aicode_root)。

實作放在 root_safety.py:mcp_server.py 匯入它,scripts/index_stats.py 也匯入
同一份 —— 維護 CLI 不能 import mcp_server(會拉起 FastMCP / KnowledgeBase /
CodeRAG),又不准另寫一套 root 驗證。這裡順便守住「mcp_server 真的有接上去」。
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

from root_safety import validate_aicode_root as _validate  # noqa: E402


def test_mcp_server_still_wires_up_root_validation():
    """mcp_server.py 必須匯入並實際呼叫 root 檢查 —— 別讓它被靜悄悄拿掉。"""
    src = (REPO_ROOT / "mcp_server.py").read_text(encoding="utf-8")
    assert "from root_safety import validate_aicode_root" in src, (
        "mcp_server.py 沒有匯入 root_safety.validate_aicode_root — root safety 檢查被砍了?"
    )
    assert "_validate_aicode_root(" in src, "mcp_server.py 沒有呼叫 root 檢查"


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
