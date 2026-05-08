"""Apply-patch 測試：解析 / 套用 / 拒絕逃逸 / 拒絕 context 不符。"""
from __future__ import annotations

from pathlib import Path

import pytest

import config
from agent_tools import ToolExecutor


@pytest.fixture
def patchable(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "PATCH_ENABLED", True)
    # 跳過 patch 之後的 lint/typecheck/test 自動驗證（這些會嘗試呼叫真的 ruff/mypy/pytest）
    monkeypatch.setattr(config, "PATCH_VERIFY_STEPS", [])
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", False)
    return tmp_path


def test_patch_disabled_returns_error(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "PATCH_ENABLED", False)
    ex = ToolExecutor(str(tmp_path))
    out = ex.apply_patch("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n")
    assert "已停用" in out


def test_parse_unified_diff_basic(patchable: Path):
    ex = ToolExecutor(str(patchable))
    diff = (
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,2 +1,2 @@\n"
        " keep\n"
        "-old\n"
        "+new\n"
    )
    changes = ex._parse_unified_diff(diff)
    assert "foo.py" in changes
    hunks = changes["foo.py"]
    assert len(hunks) == 1
    assert "old" in hunks[0]["remove"][0]
    assert "new" in hunks[0]["add"][0]


def test_apply_patch_to_file(patchable: Path):
    ex = ToolExecutor(str(patchable))
    target = patchable / "hello.py"
    target.write_text("print('old')\n", encoding="utf-8")
    diff = (
        "--- a/hello.py\n"
        "+++ b/hello.py\n"
        "@@ -1 +1 @@\n"
        "-print('old')\n"
        "+print('new')\n"
    )
    out = ex.apply_patch(diff)
    assert "✓" in out, out
    assert target.read_text(encoding="utf-8").strip() == "print('new')"


def test_apply_patch_rejects_path_outside_sandbox(patchable: Path, tmp_path_factory):
    ex = ToolExecutor(str(patchable))
    outside = tmp_path_factory.mktemp("ext")
    victim = outside / "victim.py"
    victim.write_text("print('safe')\n", encoding="utf-8")
    # patch 試圖用 ../ 逃出 sandbox
    diff = (
        f"--- a/../{outside.name}/victim.py\n"
        f"+++ b/../{outside.name}/victim.py\n"
        "@@ -1 +1 @@\n"
        "-print('safe')\n"
        "+print('pwned')\n"
    )
    out = ex.apply_patch(diff)
    assert "不在專案內" in out or "✗" in out
    # 檔案內容必須沒被改
    assert victim.read_text(encoding="utf-8").strip() == "print('safe')"


def test_apply_patch_rejects_mismatched_context(patchable: Path):
    """Patch context 必須對得上實際內容；對不上要拒絕該 hunk。"""
    ex = ToolExecutor(str(patchable))
    target = patchable / "a.py"
    target.write_text("real_line\n", encoding="utf-8")
    diff = (
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1 @@\n"
        "-totally_wrong_line\n"
        "+replacement\n"
    )
    out = ex.apply_patch(diff)
    # 檔案不該被改寫成 replacement
    assert target.read_text(encoding="utf-8").strip() == "real_line"
    # apply_patch 應該回報失敗
    assert "✗" in out or "失敗" in out or "不符" in out


def test_apply_patch_too_many_files(patchable: Path, monkeypatch):
    import agent_tools
    monkeypatch.setattr(config, "PATCH_MAX_FILES", 1)
    monkeypatch.setattr(agent_tools, "PATCH_MAX_FILES", 1)
    ex = ToolExecutor(str(patchable))
    (patchable / "a.py").write_text("a\n", encoding="utf-8")
    (patchable / "b.py").write_text("b\n", encoding="utf-8")
    diff = (
        "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+aa\n"
        "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-b\n+bb\n"
    )
    out = ex.apply_patch(diff)
    assert "超過限制" in out
