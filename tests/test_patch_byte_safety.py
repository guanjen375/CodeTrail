"""apply_patch 的 byte-level 檔案安全(workflow B;兩格式共用同一契約)。

真實 bug regression(2026-08-26 base f865e93 上可重現):
  - 非 UTF-8 檔案被 errors='replace' 靜默改寫成 U+FFFD。
  - CRLF / BOM / 無 final newline 在 read_text/write_text 往返後遺失或走樣。
  - mixed newline 被 universal newlines 猜成 LF 後寫回。
  - 巢狀新檔沒有父目錄 → 寫入失敗,但舊的 rollback 對半成品目錄無能為力。
  - in-place write_text 不是原子替換,也不重驗 preimage。

全部標 smoke(AGENTS.md §2.1:真實 bug 的 regression + 無聲失敗風險的契約)。
newline fixture 全部在測試內用 bytes 合成,不依賴 checkout 的 core.autocrlf。
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import config
from agent_tools import ToolExecutor

pytestmark = pytest.mark.smoke

SEARCH = "<<<<<<< SEARCH"
SEP = "======="
REPLACE = ">>>>>>> REPLACE"
FORMATS = ("search_replace", "unified_diff")
ROLLBACK_TITLE = "✗ 套用失敗；已執行 best-effort rollback"
DEGRADED_NOTE = "⚠ 本平台無 dir_fd 錨定,symlink 競態防線為逐層 lstat 重驗"


@pytest.fixture
def runner(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "PATCH_ENABLED", True)
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", False)
    monkeypatch.setattr(config, "PATCH_AUTO_VERIFY", False)
    monkeypatch.setattr(config, "PATCH_VERIFY_STEPS", [])
    return ToolExecutor(str(tmp_path))


def edit(fmt: str, path: str, old: str, new: str) -> str:
    """同語意的單行替換,兩種格式各一。"""
    if fmt == "search_replace":
        return "\n".join([path, SEARCH, old, SEP, new, REPLACE]) + "\n"
    return f"--- a/{path}\n+++ b/{path}\n@@\n-{old}\n+{new}\n"


def sr_new(path: str, lines: list[str]) -> str:
    return "\n".join([path, SEARCH, SEP, *lines, REPLACE]) + "\n"


def _litter(tmp_path: Path) -> list[str]:
    return sorted(
        str(p.relative_to(tmp_path))
        for p in tmp_path.rglob("*")
        if ".tmp" in p.name or ".orig" in p.name
    )


# ---------------------------------------------------------------------------
# 編碼:UTF-8 strict;失敗 = 整份 patch 拒絕、零寫入
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fmt", FORMATS)
def test_non_utf8_file_is_rejected_and_bytes_untouched(runner: ToolExecutor, tmp_path: Path, fmt: str):
    target = tmp_path / "latin.txt"
    original = b"caf\xe9\nnext\n"
    target.write_bytes(original)
    out = runner.apply_patch(edit(fmt, "latin.txt", "next", "NEXT"))
    assert "✗ latin.txt: 不是 UTF-8 文字（strict decode 失敗於 byte 3）" in out, out
    assert "⚠ 因有檔案未通過 preflight,整份 patch 已被拒絕,未寫入任何檔案（全量 preflight）" in out, out
    assert target.read_bytes() == original


@pytest.mark.parametrize("fmt", FORMATS)
def test_second_file_non_utf8_blocks_first_file_write(runner: ToolExecutor, tmp_path: Path, fmt: str):
    ok = tmp_path / "ok.txt"
    ok.write_bytes(b"keep\n")
    latin = tmp_path / "latin.txt"
    latin.write_bytes(b"caf\xe9\n")
    out = runner.apply_patch(edit(fmt, "ok.txt", "keep", "CHANGED") + edit(fmt, "latin.txt", "x", "y"))
    assert "✗ latin.txt: 不是 UTF-8 文字" in out, out
    assert ok.read_bytes() == b"keep\n", "第二檔 decode 失敗時第一檔也不能寫"
    assert latin.read_bytes() == b"caf\xe9\n"


# ---------------------------------------------------------------------------
# newline / BOM / final newline:保留原樣;mixed / CR-only 拒絕
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fmt", FORMATS)
def test_crlf_file_keeps_crlf_after_patch(runner: ToolExecutor, tmp_path: Path, fmt: str):
    target = tmp_path / "dos.txt"
    target.write_bytes(b"a\r\nb\r\nc\r\n")
    out = runner.apply_patch(edit(fmt, "dos.txt", "b", "B"))
    assert "✓ dos.txt: 已修改 1 個區塊" in out, out
    assert target.read_bytes() == b"a\r\nB\r\nc\r\n"


def test_udiff_crlf_patch_text_applies_to_lf_and_crlf_targets(runner: ToolExecutor, tmp_path: Path):
    """patch 文字本身是 CRLF 編碼(transport):對 LF target 不得注入 CR,對 CRLF target 保持 CRLF。"""
    lf = tmp_path / "lf.txt"
    lf.write_bytes(b"a\nb\n")
    crlf = tmp_path / "crlf.txt"
    crlf.write_bytes(b"a\r\nb\r\n")
    text = (edit("unified_diff", "lf.txt", "b", "B") + edit("unified_diff", "crlf.txt", "b", "B")).replace("\n", "\r\n")
    out = runner.apply_patch(text)
    assert "✓ lf.txt: 已修改 1 個區塊" in out, out
    assert "✓ crlf.txt: 已修改 1 個區塊" in out, out
    assert lf.read_bytes() == b"a\nB\n"
    assert crlf.read_bytes() == b"a\r\nB\r\n"


@pytest.mark.parametrize("fmt", FORMATS)
def test_utf8_bom_is_preserved(runner: ToolExecutor, tmp_path: Path, fmt: str):
    target = tmp_path / "bom.txt"
    target.write_bytes(b"\xef\xbb\xbfhello\nworld\n")
    # 改第一行:BOM 不得被當成第一行內容、也不得遺失
    out = runner.apply_patch(edit(fmt, "bom.txt", "hello", "HELLO"))
    assert "✓ bom.txt: 已修改 1 個區塊" in out, out
    assert target.read_bytes() == b"\xef\xbb\xbfHELLO\nworld\n"


def test_missing_final_newline_is_preserved(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "nonl.txt"
    target.write_bytes(b"a\nb")
    out = runner.apply_patch(edit("search_replace", "nonl.txt", "b", "B"))
    assert "✓ nonl.txt: 已修改 1 個區塊 [search_replace]" in out, out
    assert target.read_bytes() == b"a\nB"


def test_new_file_written_with_lf_and_no_bom(runner: ToolExecutor, tmp_path: Path):
    out = runner.apply_patch(sr_new("fresh.py", ["x = 1", "y = 2"]))
    assert "✓ fresh.py: 新建檔案 [search_replace]" in out, out
    assert (tmp_path / "fresh.py").read_bytes() == b"x = 1\ny = 2\n"


def test_new_file_keeps_trailing_blank_line_from_replace(runner: ToolExecutor, tmp_path: Path):
    """新檔序列化固定 '\\n'.join(lines) + '\\n':REPLACE 尾端的空白行是內容,不能被吃掉。"""
    out = runner.apply_patch(sr_new("blank.txt", ["value", ""]))
    assert "✓ blank.txt: 新建檔案 [search_replace]" in out, out
    assert (tmp_path / "blank.txt").read_bytes() == b"value\n\n"


@pytest.mark.parametrize("fmt", FORMATS)
def test_mixed_newlines_rejected_and_bytes_untouched(runner: ToolExecutor, tmp_path: Path, fmt: str):
    target = tmp_path / "mixed.txt"
    original = b"a\r\nb\nc\r\n"
    target.write_bytes(original)
    out = runner.apply_patch(edit(fmt, "mixed.txt", "b", "B"))
    assert "✗ mixed.txt: 換行格式不一致（mixed newline: LF 與 CRLF 並存）" in out, out
    assert target.read_bytes() == original


@pytest.mark.parametrize("fmt", FORMATS)
def test_cr_only_newlines_rejected(runner: ToolExecutor, tmp_path: Path, fmt: str):
    target = tmp_path / "cr.txt"
    original = b"a\rb\rc\r"
    target.write_bytes(original)
    out = runner.apply_patch(edit(fmt, "cr.txt", "b", "B"))
    assert "✗ cr.txt: 換行格式不支援（CR-only" in out, out
    assert target.read_bytes() == original


# ---------------------------------------------------------------------------
# 原子替換:mode 保留、inode 更新、無 temp 殘留
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits / inode")
def test_mode_bits_preserved_and_inode_replaced(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "exec.sh"
    target.write_bytes(b"echo one\n")
    target.chmod(0o750)
    before = target.stat()
    out = runner.apply_patch(edit("search_replace", "exec.sh", "echo one", "echo two"))
    assert "✓ exec.sh: 已修改 1 個區塊 [search_replace]" in out, out
    after = target.stat()
    assert stat.S_IMODE(after.st_mode) == 0o750, oct(after.st_mode)
    assert after.st_ino != before.st_ino, "必須是同目錄 temp + os.replace 的原子替換,不是 in-place 覆寫"
    assert target.read_bytes() == b"echo two\n"
    assert _litter(tmp_path) == []


def test_udiff_new_nested_file_creates_parent_dirs(runner: ToolExecutor, tmp_path: Path):
    out = runner.apply_patch("--- /dev/null\n+++ b/deep/x/y.txt\n@@\n+hello\n")
    assert "✓ deep/x/y.txt: 新建檔案" in out, out
    assert "✗" not in out, out
    assert (tmp_path / "deep" / "x" / "y.txt").read_bytes() == b"hello\n"
    assert _litter(tmp_path) == []


def test_udiff_dry_run_uses_structured_plan_fields(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "z.txt"
    target.write_bytes(b"hello\n")
    out = runner.apply_patch(edit("unified_diff", "z.txt", "hello", "HELLO"), dry_run=True)
    assert "[DRY RUN] z.txt: format=unified_diff blocks=1 budget=2/200 new_file=no locations=行 1-1" in out, out
    assert "[DRY RUN] would apply: 1 個檔案" in out, out
    assert target.read_bytes() == b"hello\n"


def test_udiff_duplicate_path_spellings_rejected(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "x.py"
    target.write_bytes(b"a\nb\n")
    text = edit("unified_diff", "x.py", "a", "A") + "--- ./x.py\n+++ ./x.py\n@@\n-b\n+B\n"
    out = runner.apply_patch(text)
    assert "✗ 同一檔案以多個 path 寫法出現: x.py, ./x.py" in out, out
    assert target.read_bytes() == b"a\nb\n"


def test_dry_run_creates_no_directories(runner: ToolExecutor, tmp_path: Path):
    out = runner.apply_patch(sr_new("deep/a/b/new.py", ["v = 1"]), dry_run=True)
    assert "[DRY RUN] deep/a/b/new.py: format=search_replace blocks=1 budget=1/200 new_file=yes locations=new file" in out, out
    assert not (tmp_path / "deep").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# journaled write:全量 preflight + best-effort rollback(不是跨檔交易)
# ---------------------------------------------------------------------------
def test_nested_new_file_then_batch_failure_removes_file_and_empty_dirs(
    runner: ToolExecutor, tmp_path: Path, monkeypatch
):
    existing = tmp_path / "e.txt"
    existing.write_bytes(b"old\n")
    text = sr_new("deep/a/b/new.py", ["v = 1"]) + edit("search_replace", "e.txt", "old", "new")

    def boom(original, plan):
        raise RuntimeError("simulated failure on existing file")

    monkeypatch.setattr(runner, "_compute_patched_content", boom)
    out = runner.apply_patch(text)
    assert out.startswith(ROLLBACK_TITLE), out
    assert "simulated failure on existing file" in out, out
    assert not (tmp_path / "deep").exists(), "本次新建的空目錄鏈必須反向移除"
    assert existing.read_bytes() == b"old\n"
    assert _litter(tmp_path) == []


def test_rollback_restores_original_bytes_exactly(runner: ToolExecutor, tmp_path: Path, monkeypatch):
    a = tmp_path / "a.txt"
    original_a = b"\xef\xbb\xbfone\r\ntwo\r\n"
    a.write_bytes(original_a)
    b = tmp_path / "b.txt"
    b.write_bytes(b"bbb\n")
    text = edit("search_replace", "a.txt", "two", "TWO") + edit("search_replace", "b.txt", "bbb", "BBB")

    calls = {"n": 0}
    orig = runner._compute_patched_content

    def boom(original, plan):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated failure on 2nd file")
        return orig(original, plan)

    monkeypatch.setattr(runner, "_compute_patched_content", boom)
    out = runner.apply_patch(text)
    assert out.startswith(ROLLBACK_TITLE), out
    assert a.read_bytes() == original_a, "rollback 必須逐 byte 還原(BOM + CRLF)"
    assert b.read_bytes() == b"bbb\n"
    assert _litter(tmp_path) == []


def test_preimage_changed_after_preflight_aborts_and_rolls_back(
    runner: ToolExecutor, tmp_path: Path, monkeypatch
):
    a = tmp_path / "a.txt"
    a.write_bytes(b"aaa\n")
    b = tmp_path / "b.txt"
    b.write_bytes(b"bbb\n")
    text = edit("search_replace", "a.txt", "aaa", "AAA") + edit("search_replace", "b.txt", "bbb", "BBB")

    orig = runner._compute_patched_content
    calls = {"n": 0}

    def drift(original, plan):
        calls["n"] += 1
        if calls["n"] == 1:
            # 第一檔計算期間,第二檔被外部改掉(preflight 之後)
            b.write_bytes(b"changed by editor\n")
        return orig(original, plan)

    monkeypatch.setattr(runner, "_compute_patched_content", drift)
    out = runner.apply_patch(text)
    assert out.startswith(ROLLBACK_TITLE), out
    assert "b.txt: preimage 已變更" in out, out
    assert a.read_bytes() == b"aaa\n", "已寫入的第一檔必須回滾"
    assert b.read_bytes() == b"changed by editor\n", "外部修改不得被覆寫"
    assert _litter(tmp_path) == []


def test_rollback_does_not_overwrite_third_party_modification(
    runner: ToolExecutor, tmp_path: Path, monkeypatch
):
    a = tmp_path / "a.txt"
    a.write_bytes(b"aaa\n")
    b = tmp_path / "b.txt"
    b.write_bytes(b"bbb\n")
    text = edit("search_replace", "a.txt", "aaa", "AAA") + edit("search_replace", "b.txt", "bbb", "BBB")

    orig = runner._compute_patched_content
    calls = {"n": 0}

    def clobber_then_fail(original, plan):
        calls["n"] += 1
        if calls["n"] == 2:
            # 第一檔已 committed,第三方接著改了它,然後第二檔失敗
            a.write_bytes(b"third party\n")
            raise RuntimeError("simulated failure on 2nd file")
        return orig(original, plan)

    monkeypatch.setattr(runner, "_compute_patched_content", clobber_then_fail)
    out = runner.apply_patch(text)
    assert out.startswith(ROLLBACK_TITLE), out
    assert "⚠ rollback conflict: a.txt 在本次寫入後又被外部修改,保留現況" in out, out
    assert a.read_bytes() == b"third party\n"
    assert b.read_bytes() == b"bbb\n"
    assert _litter(tmp_path) == []


def test_new_target_created_by_competitor_after_preflight_aborts(
    runner: ToolExecutor, tmp_path: Path, monkeypatch
):
    existing = tmp_path / "e.txt"
    existing.write_bytes(b"old\n")
    competitor = tmp_path / "n.txt"
    text = edit("search_replace", "e.txt", "old", "new") + sr_new("n.txt", ["mine"])

    orig = runner._compute_patched_content

    def sneak(original, plan):
        competitor.write_bytes(b"competitor\n")   # preflight 之後、發布之前冒出同名檔
        return orig(original, plan)

    monkeypatch.setattr(runner, "_compute_patched_content", sneak)
    out = runner.apply_patch(text)
    assert out.startswith(ROLLBACK_TITLE), out
    assert "n.txt: 新檔在 preflight 後被其他程序建立" in out, out
    assert competitor.read_bytes() == b"competitor\n", "競爭者的檔案不得被覆寫或刪除"
    assert existing.read_bytes() == b"old\n", "已寫入的既有檔必須回滾"
    assert _litter(tmp_path) == []


def test_no_temp_litter_after_success(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "t.py"
    target.write_bytes(b"a\n")
    out = runner.apply_patch(edit("search_replace", "t.py", "a", "b") + sr_new("sub/new.py", ["v = 1"]))
    assert "✓ t.py: 已修改 1 個區塊 [search_replace]" in out, out
    assert "✓ sub/new.py: 新建檔案 [search_replace]" in out, out
    assert _litter(tmp_path) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink containment")
def test_dirfd_fallback_reports_degraded_note_and_still_blocks_symlinks(
    runner: ToolExecutor, tmp_path: Path, tmp_path_factory, monkeypatch
):
    # 先用公開的 S/R 行為建立 base 紅燈(anchored 路徑:成功且沒有降級警告)
    target = tmp_path / "p.py"
    target.write_bytes(b"v = 1\n")
    out = runner.apply_patch(edit("search_replace", "p.py", "v = 1", "v = 2"))
    assert "✓ p.py: 已修改 1 個區塊 [search_replace]" in out, out
    assert DEGRADED_NOTE not in out, out
    assert target.read_bytes() == b"v = 2\n"

    import patch_engine  # 延後 import:base 沒有這個模組,上面的斷言已先讓 base 紅

    monkeypatch.setattr(patch_engine, "DIRFD_ANCHORING", False)
    other = tmp_path / "q.py"
    other.write_bytes(b"w = 1\n")
    out = runner.apply_patch(edit("search_replace", "q.py", "w = 1", "w = 2"))
    assert "✓ q.py: 已修改 1 個區塊 [search_replace]" in out, out
    assert DEGRADED_NOTE in out, out
    assert other.read_bytes() == b"w = 2\n"

    # 退回路徑的失敗結果也要保留降級警告行
    out = runner.apply_patch(edit("search_replace", "q.py", "nope", "x"))
    assert "✗ q.py: SEARCH/REPLACE 區塊 1 找不到逐字匹配" in out, out
    assert DEGRADED_NOTE in out, out

    outside = tmp_path_factory.mktemp("outside") / "victim.py"
    outside.write_bytes(b"safe\n")
    os.symlink(outside, tmp_path / "link.py")
    out = runner.apply_patch(edit("search_replace", "link.py", "safe", "pwned"))
    assert "✗ link.py: 目標或其路徑上有 symlink" in out, out
    assert outside.read_bytes() == b"safe\n"
    assert _litter(tmp_path) == []
