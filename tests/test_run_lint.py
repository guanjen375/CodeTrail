"""run_lint(fix=False) 必須走 check-only,不偷偷改檔。

Review 找到的 bug: 舊版 LINT_COMMANDS 只有 fix 組命令 (--fix / -w / -i / --write),
agent_tools.run_lint 收了 `fix` 參數卻完全沒用,所以 fix=False 仍會跑會改檔的命令。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import config
from agent_tools import ToolExecutor


class TestLintCommandsStructure:
    """LINT_COMMANDS 必須是 {ext: {'fix': [...], 'check': [...]}} 結構。"""

    def test_lint_commands_is_nested_dict(self):
        for ext, spec in config.LINT_COMMANDS.items():
            assert isinstance(spec, dict), (
                f"{ext}: LINT_COMMANDS value 必須是 dict {{'fix': [...], 'check': [...]}},"
                f"得到 {type(spec).__name__}"
            )
            assert "fix" in spec, f"{ext}: 缺 'fix' key"
            assert isinstance(spec["fix"], list) and spec["fix"], f"{ext}: 'fix' 必須是非空 list"

    def test_check_mode_uses_non_mutating_flags(self):
        """check 組命令不能含會改檔的 flag。"""
        mutating = ["--fix", "--write", "-w", "-i"]
        for ext, spec in config.LINT_COMMANDS.items():
            check_cmds = spec.get("check")
            if not check_cmds:
                continue
            for cmd in check_cmds:
                parts = cmd.split()
                for bad in mutating:
                    assert bad not in parts, (
                        f"{ext} check 命令 {cmd!r} 含會改檔的 flag {bad!r} — "
                        "check mode 必須只回報、不寫檔"
                    )


class TestRunLintMode:
    """run_lint(fix=...) 必須依 fix 選對命令組,絕對不能偷偷跑 fix 組。"""

    @pytest.fixture
    def runner_and_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # mode 選擇測試需要 PATCH_ENABLED=True,否則 fix=True 會在 gate 階段
        # 就被擋掉,根本走不到 subprocess。唯讀模式的行為由 TestRunLintReadonlyMode 覆蓋。
        monkeypatch.setattr(config, "PATCH_ENABLED", True)
        runner = ToolExecutor(str(tmp_path))
        f = tmp_path / "x.py"
        f.write_text("x = 1\n", encoding="utf-8")
        return runner, f

    def _capture_run(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        """攔截 subprocess.run,記錄每次傳的 cmd_parts。"""
        calls: list[list[str]] = []

        def fake_run(cmd_parts: list[str], **kwargs: Any) -> Any:
            calls.append(list(cmd_parts))

            class R:
                returncode = 0
                stdout = ""
                stderr = ""

            return R()

        monkeypatch.setattr("agent_tools.subprocess.run", fake_run)
        return calls

    def test_fix_true_uses_fix_commands(self, runner_and_file, monkeypatch: pytest.MonkeyPatch):
        runner, f = runner_and_file
        calls = self._capture_run(monkeypatch)
        runner.run_lint("x.py", fix=True)
        assert calls, "run_lint 應該至少跑一個命令"
        first = calls[0]
        # fix 組第一個命令: ruff check --fix
        assert "--fix" in first, f"fix=True 必須跑 --fix,實際: {first}"

    def test_fix_false_uses_check_commands_not_fix(
        self, runner_and_file, monkeypatch: pytest.MonkeyPatch
    ):
        runner, f = runner_and_file
        calls = self._capture_run(monkeypatch)
        runner.run_lint("x.py", fix=False)
        assert calls, "run_lint(fix=False) 應該至少跑一個命令"
        # 不能有任何呼叫含 --fix / -w / --write / -i
        for parts in calls:
            for bad in ("--fix", "-w", "--write", "-i"):
                assert bad not in parts, (
                    f"fix=False 跑了會改檔的命令 {parts} (含 {bad!r}) — "
                    "這是 review 找到的 bug,patch 沒生效"
                )

    def test_fix_false_no_check_returns_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """若某副檔名沒提供 check 組,fix=False 必須回錯誤而非 fallback 跑 fix。"""
        runner = ToolExecutor(str(tmp_path))
        f = tmp_path / "y.fakelang"
        f.write_text("noop\n", encoding="utf-8")

        fake_lint = {".fakelang": {"fix": ["echo fix"]}}  # 故意沒 check key
        monkeypatch.setattr("agent_tools.LINT_COMMANDS", fake_lint)

        # 連 subprocess 都不應該被叫到 — 提早就拒絕
        called: list[Any] = []
        monkeypatch.setattr(
            "agent_tools.subprocess.run",
            lambda *a, **kw: called.append(a) or (_ for _ in ()).throw(
                AssertionError("不應呼叫 subprocess.run — 應該提早回錯誤")
            ),
        )

        out = runner.run_lint("y.fakelang", fix=False)
        assert "check" in out and ("不支援" in out or "沒有" in out), (
            f"fix=False 沒 check 命令時應回錯誤,實際: {out}"
        )
        assert not called, "fix=False 沒 check 命令時不能 fallback 跑 fix"


class TestRunLintReadonlyMode:
    """AI_CODE_PATCH=0 完全唯讀模式: fix=True 必須被擋,fix=False 仍可用。

    Review 後續發現的 gap: 文件承諾「AI_CODE_PATCH=0 = 完全唯讀」,但
    舊版 run_lint 不檢查 PATCH_ENABLED,fix=True 仍會無視旗標改檔。
    """

    def test_fix_true_blocked_when_patch_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(config, "PATCH_ENABLED", False)
        runner = ToolExecutor(str(tmp_path))
        f = tmp_path / "x.py"
        f.write_text("x = 1\n", encoding="utf-8")

        # subprocess 都不該被叫到 — 應在 stat / spawn 之前提早拒絕
        def fake_run(*a, **kw):
            raise AssertionError("PATCH_ENABLED=False 時不能跑 lint subprocess")

        monkeypatch.setattr("agent_tools.subprocess.run", fake_run)

        out = runner.run_lint("x.py", fix=True)
        assert "AI_CODE_PATCH" in out or "唯讀" in out, out
        # 檔案不能被動到
        assert f.read_text(encoding="utf-8") == "x = 1\n"

    def test_fix_false_still_works_when_patch_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """唯讀模式下 check-only 仍要可用 — 不然就無法 lint 確認了。"""
        monkeypatch.setattr(config, "PATCH_ENABLED", False)
        runner = ToolExecutor(str(tmp_path))
        f = tmp_path / "x.py"
        f.write_text("x = 1\n", encoding="utf-8")

        calls: list[list[str]] = []

        def fake_run(cmd_parts, **kwargs):
            calls.append(list(cmd_parts))

            class R:
                returncode = 0
                stdout = ""
                stderr = ""

            return R()

        monkeypatch.setattr("agent_tools.subprocess.run", fake_run)

        runner.run_lint("x.py", fix=False)
        assert calls, "fix=False 在 PATCH_ENABLED=False 時應該仍能跑 check"
        for parts in calls:
            for bad in ("--fix", "-w", "--write", "-i"):
                assert bad not in parts, parts
