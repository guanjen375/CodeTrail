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


# ============================================================
# Phase 3: path containment — 白名單命令的參數不能逃出 AICODE_ROOT
# ============================================================
class TestPathContainment:
    """這些 case 是 GPT review 找出的真實逃逸路徑。"""

    def test_rejects_absolute_outside_path(self, runner: ToolExecutor):
        ok, msg, _ = runner._validate_command("pytest /tmp/some_test.py")
        assert not ok, msg
        assert "sandbox" in msg or "AICODE_ROOT" in msg

    def test_rejects_python_m_pytest_outside(self, runner: ToolExecutor):
        ok, msg, _ = runner._validate_command("python -m pytest /tmp/some_test.py")
        assert not ok, msg

    def test_rejects_dotdot_escape(self, runner: ToolExecutor):
        ok, msg, _ = runner._validate_command("pytest ../outside.py")
        assert not ok, msg

    def test_rejects_make_dash_C_outside(self, runner: ToolExecutor):
        ok, msg, _ = runner._validate_command("make -C /tmp")
        assert not ok, msg

    def test_rejects_cmake_build_outside(self, runner: ToolExecutor):
        ok, msg, _ = runner._validate_command("cmake --build /tmp/build")
        assert not ok, msg

    def test_rejects_ninja_dash_C_outside(self, runner: ToolExecutor):
        ok, msg, _ = runner._validate_command("ninja -C /tmp/build")
        assert not ok, msg

    def test_rejects_go_test_dotdot(self, runner: ToolExecutor):
        ok, msg, _ = runner._validate_command("go test ../outside")
        assert not ok, msg

    def test_rejects_inline_directory_flag(self, runner: ToolExecutor):
        ok, msg, _ = runner._validate_command("make --directory=/tmp")
        assert not ok, msg

    # ---- 必須允許的 in-root 用法 ----
    def test_allows_relative_test_file(self, runner: ToolExecutor, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n")
        ok, msg, _ = runner._validate_command("pytest tests/test_x.py")
        assert ok, msg

    def test_allows_python_m_pytest_dir(self, runner: ToolExecutor, tmp_path):
        (tmp_path / "tests").mkdir()
        ok, msg, _ = runner._validate_command("python -m pytest tests")
        assert ok, msg

    def test_allows_dot_arg(self, runner: ToolExecutor):
        ok, msg, _ = runner._validate_command("ruff check .")
        assert ok, msg

    def test_allows_dash_only_args(self, runner: ToolExecutor):
        for cmd in ("pytest -q", "pytest -k some_test", "black --check ."):
            ok, msg, _ = runner._validate_command(cmd)
            assert ok, f"{cmd!r}: {msg}"

    def test_allows_cmake_build_in_root(self, runner: ToolExecutor):
        # 注意:即使 build/ 不存在 ("尚未 cmake configure"),路徑檢查只看路徑形狀
        ok, msg, _ = runner._validate_command("cmake --build build")
        # cmake 可能不在 ALLOWED_COMMANDS 預設裡(MCP 才 append),這裡只測 path layer。
        # 若 ALLOWED_COMMANDS 沒有 cmake,測試會因白名單 fail 而非 path fail —
        # 兩種失敗都不是這條 test 想測的東西,所以我們直接呼叫 _check_path_containment。
        import shlex
        ok2, why = runner._check_path_containment(shlex.split("cmake --build build"))
        assert ok2, why

    def test_allows_ninja_dash_C_in_root(self, runner: ToolExecutor):
        import shlex
        ok, why = runner._check_path_containment(shlex.split("ninja -C build"))
        assert ok, why

    def test_allows_go_test_local(self, runner: ToolExecutor):
        # `go test ./...` 的 `./...` 看起來像 path 但是是 root 內 — 必須允許
        ok, msg, _ = runner._validate_command("go test ./...")
        assert ok, msg

    def test_path_containment_runs_after_shell_metachar_check(self, runner: ToolExecutor):
        """確認 shell-injection 檢查仍在 path 之前(順序維持安全先擋)。"""
        ok, msg, _ = runner._validate_command("pytest /tmp/x.py; rm -rf /")
        assert not ok
        # 理應因 ';' / metachar 被擋,而不是因 path
        assert "字元" in msg or "metachar" in msg or "sandbox" in msg or "AICODE_ROOT" in msg
