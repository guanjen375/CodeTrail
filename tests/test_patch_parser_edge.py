"""patch parser edge cases — review 找出的 EOF 空行與 'No newline' marker 處理。

舊版 _parse_unified_diff 在 hunk 內看到空字串時 append (' ', '') 當 context blank line,
但 patch.split('\\n') 對結尾換行會產生最後一個 '',實際上是 sentinel,不是 context。
誤算後 _verify_hunk_context 會多吃一行檔案內容 → 合法 patch 在 EOF 附近誤判 mismatch。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import config
from agent_tools import ToolExecutor


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
