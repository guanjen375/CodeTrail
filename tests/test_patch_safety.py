"""P0-1 apply_patch 資料安全 invariant 測試。

對應 review 找到的四個資料安全缺陷：
1. hunk header 宣稱的行數 > body 實際行數 → 不驗證會靜默刪掉未列出的行。
2. dry_run 沒驗 context（與工具說明不符）。
3. 多檔 patch 非交易式，第二檔失敗時第一檔已被改。
4. 固定 .orig 備份會覆蓋並刪除使用者既有檔案。
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
    monkeypatch.setattr(config, "PATCH_AUTO_VERIFY", False)
    monkeypatch.setattr(config, "PATCH_VERIFY_STEPS", [])
    return ToolExecutor(str(tmp_path))


# ---------------------------------------------------------------------------
# 缺陷 1：header count 造假不能靜默刪行
# ---------------------------------------------------------------------------
def test_header_count_larger_than_body_is_rejected(runner: ToolExecutor, tmp_path: Path):
    """header 宣稱替換 4 行、body 只給第 1 行 → 必須拒絕，後 3 行不能被刪。"""
    target = tmp_path / "four.txt"
    target.write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")

    # @@ -1,4 +1,1 @@ 宣稱移除 4 行、換成 1 行，
    # 但 body 只有一組 -/+（context+remove = 1 ≠ 4）。
    evil = (
        "--- a/four.txt\n"
        "+++ b/four.txt\n"
        "@@ -1,4 +1,1 @@\n"
        "-l1\n"
        "+X1\n"
    )
    out = runner.apply_patch(evil)
    # 必須被拒絕
    assert "✗" in out or "拒絕" in out or "失敗" in out, out
    # 檔案原封不動，l2/l3/l4 不能消失
    assert target.read_text(encoding="utf-8") == "l1\nl2\nl3\nl4\n"


def test_new_count_mismatch_is_rejected(runner: ToolExecutor, tmp_path: Path):
    """new_count 與 body 的 context+add 不符 → 拒絕。"""
    target = tmp_path / "n.txt"
    target.write_text("a\nb\n", encoding="utf-8")
    bad = (
        "--- a/n.txt\n"
        "+++ b/n.txt\n"
        "@@ -1,2 +1,5 @@\n"  # 宣稱結果 5 行，實際 body 只給 2 行
        " a\n"
        "-b\n"
        "+B\n"
    )
    out = runner.apply_patch(bad)
    assert "✗" in out or "拒絕" in out or "失敗" in out, out
    assert target.read_text(encoding="utf-8") == "a\nb\n"


def test_valid_multi_line_hunk_still_applies(runner: ToolExecutor, tmp_path: Path):
    """header 與 body 一致的合法 patch 仍要成功（strict 驗證不能誤傷）。"""
    target = tmp_path / "ok.txt"
    target.write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")
    good = (
        "--- a/ok.txt\n"
        "+++ b/ok.txt\n"
        "@@ -1,4 +1,4 @@\n"
        " l1\n"
        "-l2\n"
        "+L2\n"
        " l3\n"
        " l4\n"
    )
    out = runner.apply_patch(good)
    assert "✓" in out and "✗" not in out, out
    assert target.read_text(encoding="utf-8") == "l1\nL2\nl3\nl4\n"


# ---------------------------------------------------------------------------
# 缺陷 2：dry_run 要驗 context
# ---------------------------------------------------------------------------
def test_dry_run_reports_context_mismatch(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "c.txt"
    target.write_text("real\n", encoding="utf-8")
    patch = (
        "--- a/c.txt\n"
        "+++ b/c.txt\n"
        "@@ -1 +1 @@\n"
        "-wrong\n"
        "+new\n"
    )
    out = runner.apply_patch(patch, dry_run=True)
    # dry_run 必須偵測到 context 不符，而不是回報「將修改」
    assert "✗" in out or "不符" in out or "preflight" in out, out
    assert target.read_text(encoding="utf-8") == "real\n"


# ---------------------------------------------------------------------------
# 缺陷 3：多檔 patch 交易性 —— 第二檔 preflight 失敗，第一檔不能被改
# ---------------------------------------------------------------------------
def test_multi_file_is_atomic(runner: ToolExecutor, tmp_path: Path):
    good = tmp_path / "good.txt"
    good.write_text("keep\n", encoding="utf-8")
    bad = tmp_path / "bad.txt"
    bad.write_text("actual\n", encoding="utf-8")

    patch = (
        "--- a/good.txt\n"
        "+++ b/good.txt\n"
        "@@ -1 +1 @@\n"
        "-keep\n"
        "+CHANGED\n"
        "--- a/bad.txt\n"
        "+++ b/bad.txt\n"
        "@@ -1 +1 @@\n"
        "-does_not_match\n"  # context 對不上 → 整個 patch 應被拒
        "+nope\n"
    )
    out = runner.apply_patch(patch)
    assert "✗" in out, out
    # good.txt 不能因為排在前面就先被改
    assert good.read_text(encoding="utf-8") == "keep\n", "多檔 patch 非交易式：第一檔被改了"
    assert bad.read_text(encoding="utf-8") == "actual\n"


# ---------------------------------------------------------------------------
# 缺陷 4：備份不能覆蓋/刪除使用者既有 .orig
# ---------------------------------------------------------------------------
def test_apply_preserves_user_orig_file(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "s.py"
    target.write_text("v1\n", encoding="utf-8")
    # 使用者自己也有一份 s.py.orig（重要資料，不能被工具動到）
    user_orig = tmp_path / "s.py.orig"
    user_orig.write_text("USER_PRECIOUS_BACKUP\n", encoding="utf-8")

    patch = (
        "--- a/s.py\n"
        "+++ b/s.py\n"
        "@@ -1 +1 @@\n"
        "-v1\n"
        "+v2\n"
    )
    out = runner.apply_patch(patch)
    assert "✓" in out, out
    assert target.read_text(encoding="utf-8").strip() == "v2"
    # 使用者的 .orig 內容原封不動，也沒被刪掉
    assert user_orig.exists(), "使用者既有 .orig 被刪除了"
    assert user_orig.read_text(encoding="utf-8") == "USER_PRECIOUS_BACKUP\n"


def test_rollback_on_mid_batch_write_failure(runner: ToolExecutor, tmp_path: Path, monkeypatch):
    """多檔套用時，若第二檔寫入中途拋錯，第一檔必須被回滾、且不留備份。"""
    a = tmp_path / "a.txt"
    a.write_text("aaa\n", encoding="utf-8")
    b = tmp_path / "b.txt"
    b.write_text("bbb\n", encoding="utf-8")
    patch = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-aaa\n+AAA\n"
        "--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-bbb\n+BBB\n"
    )

    calls = {"n": 0}
    orig = runner._compute_patched_content

    def boom(original, hunks):
        calls["n"] += 1
        if calls["n"] == 2:  # 第二檔（b.txt）寫入前引爆
            raise RuntimeError("simulated failure on 2nd file")
        return orig(original, hunks)

    monkeypatch.setattr(runner, "_compute_patched_content", boom)

    out = runner.apply_patch(patch)
    assert "回滾" in out or "atomic" in out, out
    # 第一檔（先成功寫入的 a.txt）必須被還原
    assert a.read_text(encoding="utf-8") == "aaa\n", "第一檔沒被回滾"
    assert b.read_text(encoding="utf-8") == "bbb\n"
    # 不留任何備份
    leftovers = [p.name for p in tmp_path.iterdir() if ".orig" in p.name]
    assert leftovers == [], f"殘留備份檔: {leftovers}"


def test_success_leaves_no_backup_litter(runner: ToolExecutor, tmp_path: Path):
    """成功套用後不該殘留任何本次產生的備份檔。"""
    target = tmp_path / "t.py"
    target.write_text("a\n", encoding="utf-8")
    patch = (
        "--- a/t.py\n"
        "+++ b/t.py\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
    )
    runner.apply_patch(patch)
    leftovers = [p.name for p in tmp_path.iterdir() if ".orig" in p.name]
    assert leftovers == [], f"殘留備份檔: {leftovers}"
