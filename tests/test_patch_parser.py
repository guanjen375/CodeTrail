"""apply_patch 的 unified-diff parser:多檔 hunk、header 變體、格式異常的拒絕。

合併自 tests/test_patch.py 與 tests/test_patch_parser_edge.py(2026-08-20)。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import config
from agent_tools import ToolExecutor

# smoke:安全層(AGENTS.md §2.1 第 2 款「無聲失敗風險的契約」)
# AGENTS.md §3 安全檢查點:apply_patch 的 parser —— 誤讀 diff 就會改錯檔案。
pytestmark = pytest.mark.smoke


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


# --------------------------------------------------------------------------
# 併自 tests/test_patch_parser_edge.py:parser 的邊界輸入。
# --------------------------------------------------------------------------
@pytest.fixture
def runner(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "PATCH_ENABLED", True)
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", False)
    # 關掉自動驗證,避免測試需要 lint 工具
    monkeypatch.setattr(config, "PATCH_AUTO_VERIFY", False)
    monkeypatch.setattr(config, "PATCH_VERIFY_STEPS", [])
    return ToolExecutor(str(tmp_path))


def test_trailing_newline_does_not_inject_blank_context(runner: ToolExecutor, tmp_path: Path):
    """patch 以 newline 結尾不能讓 parser 多算一個 context blank line。"""
    target = tmp_path / "x.txt"
    target.write_text("a\nb\nc\n", encoding="utf-8")

    # 注意:patch 末尾刻意留 newline,重現 split('\\n') 產生 EOF sentinel 的情況
    patch = (
        "--- a/x.txt\n"
        "+++ b/x.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " a\n"
        "-b\n"
        "+B\n"
        " c\n"
    )

    parsed = runner._parse_unified_diff(patch)
    assert "x.txt" in parsed, parsed
    hunks = parsed["x.txt"]
    assert len(hunks) == 1, hunks

    # 三行 hunk 內容: ' a', '-b', '+B', ' c' = 4 個 lines。
    # 舊版會多塞一個 (' ', '') sentinel,造成 5 個。
    types = [t for t, _ in hunks[0]["lines"]]
    assert types == [' ', '-', '+', ' '], (
        f"hunk lines 不該被注入 EOF sentinel,實際 types = {types}"
    )

    # 端到端: apply_patch 應成功且檔案內容正確
    out = runner.apply_patch(patch=patch, dry_run=False)
    assert "✓" in out and "✗" not in out, out
    assert target.read_text(encoding="utf-8") == "a\nB\nc\n"


def test_no_newline_at_eof_marker_is_skipped(runner: ToolExecutor, tmp_path: Path):
    """`\\ No newline at end of file` 應被跳過,不影響 context 比對。"""
    target = tmp_path / "y.txt"
    # 注意:檔案沒 trailing newline
    target.write_text("a\nb", encoding="utf-8")

    patch = (
        "--- a/y.txt\n"
        "+++ b/y.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " a\n"
        "-b\n"
        "\\ No newline at end of file\n"
        "+B\n"
        "\\ No newline at end of file\n"
    )

    parsed = runner._parse_unified_diff(patch)
    hunks = parsed["y.txt"]
    assert len(hunks) == 1, hunks
    # '\ No newline...' 不該出現在 hunk lines
    for tag, content in hunks[0]["lines"]:
        assert not content.startswith("\\ No newline"), (
            f"'No newline at EOF' marker 被當成 hunk 內容: {content!r}"
        )
    types = [t for t, _ in hunks[0]["lines"]]
    assert types == [' ', '-', '+'], types


def test_dry_run_does_not_write_or_backup(runner: ToolExecutor, tmp_path: Path):
    """dry_run 不該產 .orig 也不該改檔案。"""
    target = tmp_path / "z.txt"
    target.write_text("hello\n", encoding="utf-8")

    patch = (
        "--- a/z.txt\n"
        "+++ b/z.txt\n"
        "@@ -1 +1 @@\n"
        "-hello\n"
        "+HELLO\n"
    )

    out = runner.apply_patch(patch=patch, dry_run=True)
    assert "DRY RUN" in out, out
    # 檔案沒被改
    assert target.read_text(encoding="utf-8") == "hello\n"
    # 沒留 .orig
    assert not (tmp_path / "z.txt.orig").exists()
