"""run_command policy tests：白名單、危險字元、shell 注入。"""
from __future__ import annotations

from pathlib import Path

import pytest

import config
from agent_tools import ToolExecutor


@pytest.fixture
def runner(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", True)
    return ToolExecutor(str(tmp_path))


def test_run_command_disabled_blocks(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", False)
    ex = ToolExecutor(str(tmp_path))
    out = ex.run_command("pytest -h")
    assert "已停用" in out


def test_validate_rejects_non_whitelisted(runner: ToolExecutor):
    ok, msg, parts = runner._validate_command("rm -rf /")
    assert not ok
    assert "不允許" in msg


def test_validate_rejects_shell_metacharacters(runner: ToolExecutor):
    """白名單命令也不該夾帶 shell 元字元。"""
    bad = [
        "pytest; rm -rf /",
        "pytest && rm -rf /",
        "pytest | tee /tmp/x",
        "pytest > /tmp/x",
        "pytest $(whoami)",
        "pytest `whoami`",
    ]
    for cmd in bad:
        ok, msg, _ = runner._validate_command(cmd)
        assert not ok, f"應該擋下: {cmd!r} -> {msg}"


def test_validate_allows_whitelisted(runner: ToolExecutor):
    ok, msg, parts = runner._validate_command("pytest -h")
    assert ok, msg
    assert parts and parts[0] == "pytest"


def test_validate_rejects_empty(runner: ToolExecutor):
    ok, _, _ = runner._validate_command("")
    assert not ok


def test_validate_rejects_path_traversal_via_arg(runner: ToolExecutor):
    """白名單命令但參數試圖逃逸（這層只擋 shell metachar，路徑逃逸交給 cwd 限制）。"""
    # 但至少不能塞反引號或 $()
    ok, _, _ = runner._validate_command("pytest `cat /etc/passwd`")
    assert not ok
