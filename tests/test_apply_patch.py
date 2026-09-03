"""apply_patch 的完整契約:unified-diff parser、套用階段、byte-level 檔案安全、SEARCH/REPLACE 格式。

合併自 tests/test_patch_parser.py、tests/test_patch_apply.py、tests/test_patch_byte_safety.py
與 tests/test_patch_search_replace.py(2026-09-02);更早的來源是 tests/test_patch.py、
tests/test_patch_parser_edge.py、tests/test_patch_safety.py 與 tests/test_patch_context_locate.py
(2026-08-20)。

- parser:多檔 hunk、header 變體、格式異常的拒絕 —— 誤讀 diff 就會改錯檔案。
- 套用階段:context 必須匹配、行號定位、已套用偵測、max files / max lines 上限保護。
- byte-level 檔案安全(workflow B;兩格式共用同一契約)。真實 bug regression(2026-08-26
  base f865e93 上可重現):
    - 非 UTF-8 檔案被 errors='replace' 靜默改寫成 U+FFFD。
    - CRLF / BOM / 無 final newline 在 read_text/write_text 往返後遺失或走樣。
    - mixed newline 被 universal newlines 猜成 LF 後寫回。
    - 巢狀新檔沒有父目錄 → 寫入失敗,但舊的 rollback 對半成品目錄無能為力。
    - in-place write_text 不是原子替換,也不重驗 preimage。
  newline fixture 全部在測試內用 bytes 合成,不依賴 checkout 的 core.autocrlf。
- SEARCH/REPLACE 格式(workflow A/E,2026-08-26 的新功能安全契約):canonical grammar、
  路徑安全、exact 定位、上限與 dry_run。S/R 沒有行號 hint:多處匹配一律拒絕、僅縮排相似
  的候選只當提示絕不代套、所有 block 對同一份原始 snapshot 定位。S/R 走的是與 udiff
  同一條 sandbox → limit → preflight → write → rollback 管線,任何一條鬆掉都是靜默寫錯檔。

這些都是 AGENTS.md §2 點名的安全檢查點(context 必須匹配 / max files / max lines /
patch_engine 的 byte-safe 寫入 / S/R 的 sandbox 與唯一匹配),整份都標 smoke
(AGENTS.md §1.1:真實 bug 的 regression + 無聲失敗風險的契約)。
"""
from __future__ import annotations

import ast
import inspect
import os
import re
import stat
from pathlib import Path

import pytest

import config
from agent_tools import _APPLY_PATCH_TOOL, ToolExecutor

# smoke:安全層(AGENTS.md §1.1 第 2 款「無聲失敗風險的契約」)
# AGENTS.md §2 安全檢查點:apply_patch 的 parser、「context 必須匹配」與 max files / max lines、
# patch_engine 的 byte-safe 寫入、S/R 的 sandbox(path escape / symlink)與唯一匹配、不重疊。
pytestmark = pytest.mark.smoke

SEARCH = "<<<<<<< SEARCH"
SEP = "======="
REPLACE = ">>>>>>> REPLACE"


@pytest.fixture
def runner(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "PATCH_ENABLED", True)
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", False)
    # PATCH_AUTO_VERIFY=False = 停用自動 syntax check(steps=[] 則是 requested 為空、回報「驗證不完整」);
    # 兩者都不會 spawn 任何工具
    monkeypatch.setattr(config, "PATCH_AUTO_VERIFY", False)
    monkeypatch.setattr(config, "PATCH_VERIFY_STEPS", [])
    return ToolExecutor(str(tmp_path))


# ── 原 test_patch_parser.py:unified-diff parser 與 max files / sandbox 上限 ──
@pytest.fixture
def patchable(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "PATCH_ENABLED", True)
    # steps=[] = requested 為空,apply_patch 回報「驗證不完整」;它不會 spawn 任何工具
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


# ── 原 test_patch_apply.py:套用階段(context 必須匹配、行號定位、已套用偵測、上限保護)──
# ---------------------------------------------------------------------------
# 缺陷 1：header count 造假不能靜默刪行
#
# 2026-08-19 契約更新:header 行數不再參與任何計算(splice 位置靠 context
# 內容定位、splice 長度 = body 實際行數),所以「宣稱 N 行、body 給 M 行」
# 結構上就刪不到未列出的行 — 不再需要拒絕,行數錯誤直接忽略。
# 這裡守的 invariant 從「必須拒絕」改成「未列在 body 的行絕不消失」。
# (背景:小模型行數幾乎必錯,舊 strict 核對讓它陷入改 header 重試迴圈。)
# ---------------------------------------------------------------------------
def test_header_count_larger_than_body_cannot_delete_lines(runner: ToolExecutor, tmp_path: Path):
    """header 宣稱替換 4 行、body 只給第 1 行 → 只有 body 列出的 l1 被換,l2/l3/l4 必須留存。"""
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
    # 行數宣稱被忽略,body 列出的修改正常套用
    assert "✓" in out, out
    # 未列在 body 的 l2/l3/l4 絕不能消失(splice 長度由 body 決定)
    assert target.read_text(encoding="utf-8") == "X1\nl2\nl3\nl4\n"


def test_new_count_mismatch_is_ignored(runner: ToolExecutor, tmp_path: Path):
    """new_count 與 body 的 context+add 不符 → 行數宣稱忽略,依 body 套用。"""
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
    assert "✓" in out, out
    assert target.read_text(encoding="utf-8") == "a\nB\n"


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


# --------------------------------------------------------------------------
# 併自 tests/test_patch_context_locate.py:定位與 already-applied 判斷。
# --------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 行號錯誤 / 缺省不影響套用
# ---------------------------------------------------------------------------
def test_wrong_line_numbers_still_apply(runner: ToolExecutor, tmp_path: Path):
    """行號整組錯(-999)但 context 唯一 → 依內容定位套用成功。"""
    target = tmp_path / "f.py"
    target.write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    patch = (
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -999,3 +999,3 @@\n"
        " b\n"
        "-c\n"
        "+C\n"
        " d\n"
    )
    out = runner.apply_patch(patch)
    assert "✓" in out and "✗" not in out, out
    assert target.read_text(encoding="utf-8") == "a\nb\nC\nd\ne\n"
    # 回報應揭露實際定位(讓模型知道行號被修正,而不是默默吞掉)
    assert "定位" in out, out


def test_bare_hunk_header_applies(runner: ToolExecutor, tmp_path: Path):
    """`@@`(完全不帶行號)是一級公民。"""
    target = tmp_path / "g.py"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    patch = (
        "--- a/g.py\n"
        "+++ b/g.py\n"
        "@@\n"
        " one\n"
        "-two\n"
        "+TWO\n"
        " three\n"
    )
    out = runner.apply_patch(patch)
    assert "✓" in out and "✗" not in out, out
    assert target.read_text(encoding="utf-8") == "one\nTWO\nthree\n"


def test_hunk_header_with_section_text_applies(runner: ToolExecutor, tmp_path: Path):
    """`@@ -1,3 +1,3 @@ def foo()`(git 的 section heading)也要能解析。"""
    target = tmp_path / "h.py"
    target.write_text("x\ny\nz\n", encoding="utf-8")
    patch = (
        "--- a/h.py\n"
        "+++ b/h.py\n"
        "@@ -1,3 +1,3 @@ def foo()\n"
        " x\n"
        "-y\n"
        "+Y\n"
        " z\n"
    )
    out = runner.apply_patch(patch)
    assert "✓" in out and "✗" not in out, out
    assert target.read_text(encoding="utf-8") == "x\nY\nz\n"


# ---------------------------------------------------------------------------
# 空白 context 行被模型 strip 成 "" → 不截斷 hunk
# ---------------------------------------------------------------------------
def test_blank_context_line_without_space_prefix(runner: ToolExecutor, tmp_path: Path):
    """hunk 中段的空白 context 行以 `""` 出現(模型 strip 行尾空白)→ 照常解析。

    這是 error1/error2.png 的實際 diff 形狀:巨集定義後空一行再接註解。
    """
    target = tmp_path / "m.c"
    target.write_text("#include <a.h>\n\n// checks\nint main() {}\n", encoding="utf-8")
    # 注意第 4 行是真正的空字串(不是 " "):模擬模型輸出
    patch = (
        "--- a/m.c\n"
        "+++ b/m.c\n"
        "@@\n"
        " #include <a.h>\n"
        "+#define DBG 1\n"
        "\n"
        " // checks\n"
    )
    out = runner.apply_patch(patch)
    assert "✓" in out and "✗" not in out, out
    assert target.read_text(encoding="utf-8") == (
        "#include <a.h>\n#define DBG 1\n\n// checks\nint main() {}\n"
    )


def test_trailing_blank_after_last_hunk_is_not_context(runner: ToolExecutor, tmp_path: Path):
    """patch 結尾的空行仍是 sentinel,不能被算進 hunk(既有 EOF 契約不回歸)。"""
    target = tmp_path / "t.txt"
    target.write_text("a\nb\n", encoding="utf-8")
    patch = (
        "--- a/t.txt\n"
        "+++ b/t.txt\n"
        "@@\n"
        " a\n"
        "-b\n"
        "+B\n"
    )  # 字串結尾的 \n 會讓 split 產生 EOF sentinel ""
    parsed = runner._parse_unified_diff(patch)
    types = [t for t, _ in parsed["t.txt"][0]["lines"]]
    assert types == [' ', '-', '+'], types
    out = runner.apply_patch(patch)
    assert "✓" in out, out
    assert target.read_text(encoding="utf-8") == "a\nB\n"


# ---------------------------------------------------------------------------
# 多處匹配:fail loud / 行號 hint 消歧
# ---------------------------------------------------------------------------
AMBIG = "x = 1\nmarker\nx = 1\nmarker\ntail\n"


def test_ambiguous_context_without_hint_is_rejected(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "amb.py"
    target.write_text(AMBIG, encoding="utf-8")
    patch = (
        "--- a/amb.py\n"
        "+++ b/amb.py\n"
        "@@\n"
        "-x = 1\n"
        "+x = 2\n"
        " marker\n"
    )
    out = runner.apply_patch(patch)
    assert "✗" in out, out
    assert "2 處" in out or "出現" in out, out
    # 檔案必須原封不動
    assert target.read_text(encoding="utf-8") == AMBIG


def test_ambiguous_context_with_hint_picks_nearest(runner: ToolExecutor, tmp_path: Path):
    """行號 hint 指向第二處(行 3)→ 只改第二處。"""
    target = tmp_path / "amb2.py"
    target.write_text(AMBIG, encoding="utf-8")
    patch = (
        "--- a/amb2.py\n"
        "+++ b/amb2.py\n"
        "@@ -3,2 +3,2 @@\n"
        "-x = 1\n"
        "+x = 2\n"
        " marker\n"
    )
    out = runner.apply_patch(patch)
    assert "✓" in out and "✗" not in out, out
    assert target.read_text(encoding="utf-8") == "x = 1\nmarker\nx = 2\nmarker\ntail\n"


# ---------------------------------------------------------------------------
# 已套用過 → no-op(重試冪等)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_already_applied_hunk_with_new_start_is_noop(runner: ToolExecutor, tmp_path: Path):
    """`@@ -a,b +c,d @@` 重送:修改後內容正好在新檔行 c → 成功的 no-op。"""
    target = tmp_path / "idem.py"
    target.write_text("a\nb\nc\n", encoding="utf-8")
    patch = (
        "--- a/idem.py\n"
        "+++ b/idem.py\n"
        "@@ -1,3 +1,3 @@\n"
        " a\n"
        "-b\n"
        "+B\n"
        " c\n"
    )
    out1 = runner.apply_patch(patch)
    assert "✓" in out1, out1
    assert target.read_text(encoding="utf-8") == "a\nB\nc\n"

    # 同一份 patch 再套一次:必須是成功的 no-op,不能報錯、不能改壞內容
    out2 = runner.apply_patch(patch)
    assert "✓" in out2 and "✗" not in out2, out2
    assert "已套用" in out2, out2
    assert target.read_text(encoding="utf-8") == "a\nB\nc\n"
    # 不留備份殘骸
    leftovers = [p.name for p in tmp_path.iterdir() if ".orig" in p.name]
    assert leftovers == [], leftovers


@pytest.mark.smoke
def test_already_applied_bare_header_is_fail_loud(runner: ToolExecutor, tmp_path: Path):
    """裸 `@@` 重送:沒有新檔行號就無法證明是同一處 → 不宣稱 no-op。

    純內容比對分不出「已套用」與「目標漂移、別處剛好相同」,所以誠實報錯,
    並指出修改後內容在第幾行,讓模型自己決定要不要重送。
    """
    target = tmp_path / "idem_bare.py"
    target.write_text("a\nb\nc\n", encoding="utf-8")
    patch = (
        "--- a/idem_bare.py\n"
        "+++ b/idem_bare.py\n"
        "@@\n"
        " a\n"
        "-b\n"
        "+B\n"
        " c\n"
    )
    assert "✓" in runner.apply_patch(patch)
    assert target.read_text(encoding="utf-8") == "a\nB\nc\n"

    out = runner.apply_patch(patch)
    assert "✗" in out and "沒有寫新檔行號" in out, out
    assert "行 1" in out, out
    assert target.read_text(encoding="utf-8") == "a\nB\nc\n"


def test_pure_deletion_mismatch_stays_fail_loud(runner: ToolExecutor, tmp_path: Path):
    """純刪除 hunk 對不上 → 不能被「已套用」誤吞,必須 fail loud。"""
    target = tmp_path / "del.py"
    target.write_text("keep\n", encoding="utf-8")
    patch = (
        "--- a/del.py\n"
        "+++ b/del.py\n"
        "@@\n"
        "-not_here\n"
    )
    out = runner.apply_patch(patch)
    assert "✗" in out, out
    assert target.read_text(encoding="utf-8") == "keep\n"


# ---------------------------------------------------------------------------
# 純新增 hunk
# ---------------------------------------------------------------------------
def test_pure_insertion_without_context_or_numbers_rejected(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "ins.py"
    target.write_text("a\nb\n", encoding="utf-8")
    patch = (
        "--- a/ins.py\n"
        "+++ b/ins.py\n"
        "@@\n"
        "+new_line\n"
    )
    out = runner.apply_patch(patch)
    assert "✗" in out and "context" in out, out
    assert target.read_text(encoding="utf-8") == "a\nb\n"


def test_pure_insertion_with_line_hint(runner: ToolExecutor, tmp_path: Path):
    """`@@ -1,0 +2,1 @@` 純新增:插在第 1 行之後。"""
    target = tmp_path / "ins2.py"
    target.write_text("a\nb\n", encoding="utf-8")
    patch = (
        "--- a/ins2.py\n"
        "+++ b/ins2.py\n"
        "@@ -1,0 +2,1 @@\n"
        "+inserted\n"
    )
    out = runner.apply_patch(patch)
    assert "✓" in out, out
    assert target.read_text(encoding="utf-8") == "a\ninserted\nb\n"


# ---------------------------------------------------------------------------
# context 完全不符:診斷訊息要能導引下一步
# ---------------------------------------------------------------------------
def test_mismatch_reports_nearest_candidate(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "diag.py"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    patch = (
        "--- a/diag.py\n"
        "+++ b/diag.py\n"
        "@@\n"
        " alpha\n"
        "-BETA_TYPO\n"
        "+beta2\n"
        " gamma\n"
    )
    out = runner.apply_patch(patch)
    assert "✗" in out, out
    # 要有期望/實際對照,並提示 read_file 校正流程
    assert "期望" in out and "實際" in out, out
    assert "read_file" in out, out
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


# ---------------------------------------------------------------------------
# 定位重疊:拒絕
# ---------------------------------------------------------------------------
def test_overlapping_hunks_rejected(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "ov.py"
    target.write_text("p\nq\nr\n", encoding="utf-8")
    patch = (
        "--- a/ov.py\n"
        "+++ b/ov.py\n"
        "@@\n"
        " p\n"
        "-q\n"
        "+Q1\n"
        "@@\n"
        "-q\n"
        "+Q2\n"
        " r\n"
    )
    out = runner.apply_patch(patch)
    assert "✗" in out and "重疊" in out, out
    assert target.read_text(encoding="utf-8") == "p\nq\nr\n"


# ---------------------------------------------------------------------------
# 縮排不敏感備援(舊版 strip 容忍度不回歸)
# ---------------------------------------------------------------------------
def test_indentation_loose_match_still_applies(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "ind.py"
    target.write_text("def f():\n    x = 1\n    return x\n", encoding="utf-8")
    # context/移除行縮排錯(2 空格 vs 檔案 4 空格)→ loose 掃描仍應命中
    patch = (
        "--- a/ind.py\n"
        "+++ b/ind.py\n"
        "@@\n"
        " def f():\n"
        "-  x = 1\n"
        "+    x = 2\n"
        "-  return x\n"
        "+    return x\n"
    )
    out = runner.apply_patch(patch)
    assert "✓" in out and "✗" not in out, out
    assert target.read_text(encoding="utf-8") == "def f():\n    x = 2\n    return x\n"


# ---------------------------------------------------------------------------
# 多 hunk:各自定位,由後往前套,互不位移
# ---------------------------------------------------------------------------
def test_multi_hunk_bottom_up_no_drift(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "mh.py"
    target.write_text("h1\nb1\nm\nh2\nb2\n", encoding="utf-8")
    patch = (
        "--- a/mh.py\n"
        "+++ b/mh.py\n"
        "@@\n"
        " h1\n"
        "-b1\n"
        "+B1\n"
        "+B1x\n"
        "@@\n"
        " h2\n"
        "-b2\n"
        "+B2\n"
    )
    out = runner.apply_patch(patch)
    assert "✓" in out and "✗" not in out, out
    assert target.read_text(encoding="utf-8") == "h1\nB1\nB1x\nm\nh2\nB2\n"


# ---------------------------------------------------------------------------
# 純新增:越界行號 fail loud(舊版靜默 clamp 到 EOF)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_pure_insertion_out_of_range_hint_is_rejected(runner: ToolExecutor, tmp_path: Path):
    """`@@ -999,0 +1000,1 @@` 對兩行檔 → 必須拒絕,不能夾到檔尾當成功。"""
    target = tmp_path / "oob.py"
    target.write_text("a\nb\n", encoding="utf-8")
    patch = (
        "--- a/oob.py\n"
        "+++ b/oob.py\n"
        "@@ -999,0 +1000,1 @@\n"
        "+INSERTED\n"
    )
    out = runner.apply_patch(patch)
    assert "✗" in out and "超出檔案範圍" in out, out
    assert "共 2 行" in out, out
    assert target.read_text(encoding="utf-8") == "a\nb\n"


def test_pure_insertion_out_of_range_hint_rejected_in_dry_run(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "oob2.py"
    target.write_text("a\nb\n", encoding="utf-8")
    patch = (
        "--- a/oob2.py\n"
        "+++ b/oob2.py\n"
        "@@ -50,0 +51,1 @@\n"
        "+INSERTED\n"
    )
    out = runner.apply_patch(patch, dry_run=True)
    assert "✗" in out and "超出檔案範圍" in out, out
    assert target.read_text(encoding="utf-8") == "a\nb\n"


@pytest.mark.parametrize(
    "filename,original,expected",
    [
        pytest.param("eof.py", "a\nb\n", "a\nb\nAPPENDED\n", id="at_eof_keeps_trailing_newline"),
        pytest.param("nonl.py", "a\nb", "a\nb\nAPPENDED", id="into_file_without_trailing_newline"),
    ],
)
def test_pure_insertion_after_last_line(
    runner: ToolExecutor, tmp_path: Path, filename: str, original: str, expected: str,
):
    """插在最後一行之後(hint == 實際行數)是合法邊界,且不新增空行。

    - at_eof_keeps_trailing_newline:檔尾有換行 → 尾端換行保留。
    - into_file_without_trailing_newline:檔尾沒有換行 → 行數不含 sentinel,插到最後一行
      之後仍不加尾端換行。
    """
    target = tmp_path / filename
    target.write_text(original, encoding="utf-8")
    patch = (
        f"--- a/{filename}\n"
        f"+++ b/{filename}\n"
        "@@ -2,0 +3,1 @@\n"
        "+APPENDED\n"
    )
    out = runner.apply_patch(patch)
    assert "✓" in out and "✗" not in out, out
    assert target.read_text(encoding="utf-8") == expected


def test_pure_insertion_into_empty_file(runner: ToolExecutor, tmp_path: Path):
    """空檔只有 0 這個合法插入點;`@@ -0,0 +1,1 @@` 可用,越界的要拒。"""
    target = tmp_path / "empty.py"
    target.write_text("", encoding="utf-8")
    patch = (
        "--- a/empty.py\n"
        "+++ b/empty.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+FIRST\n"
    )
    out = runner.apply_patch(patch)
    assert "✓" in out and "✗" not in out, out
    assert target.read_text(encoding="utf-8") == "FIRST\n"

    target.write_text("", encoding="utf-8")
    bad = (
        "--- a/empty.py\n"
        "+++ b/empty.py\n"
        "@@ -3,0 +4,1 @@\n"
        "+FIRST\n"
    )
    out = runner.apply_patch(bad)
    assert "✗" in out and "超出檔案範圍" in out, out
    assert target.read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------------------
# 「已套用過」判定:post-image 也要唯一 / 與 hint 相符
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_already_applied_needs_unique_post_image(runner: ToolExecutor, tmp_path: Path):
    """目標區塊漂移、但檔案裡有兩處相同的修改後內容 → 不能當 no-op。"""
    target = tmp_path / "dup.py"
    original = "def a():\n    x = 2\n    return x\n\ndef b():\n    x = 2\n    return x\n"
    target.write_text(original, encoding="utf-8")
    patch = (
        "--- a/dup.py\n"
        "+++ b/dup.py\n"
        "@@\n"
        "-    x = 1\n"
        "+    x = 2\n"
        "     return x\n"
    )
    out = runner.apply_patch(patch)
    assert "✗" in out and "修改後內容" in out, out
    assert target.read_text(encoding="utf-8") == original


def test_already_applied_conflicting_hint_is_rejected(runner: ToolExecutor, tmp_path: Path):
    """唯一的修改後內容在檔案另一端(離 hint 很遠)→ fail loud,不報 no-op。"""
    target = tmp_path / "far.py"
    body = "".join(f"line{i}\n" for i in range(300))
    original = "PATCHED\ntail\n" + body
    target.write_text(original, encoding="utf-8")
    patch = (
        "--- a/far.py\n"
        "+++ b/far.py\n"
        "@@ -290,2 +290,2 @@\n"
        "-ORIGINAL\n"
        "+PATCHED\n"
        " tail\n"
    )
    out = runner.apply_patch(patch)
    assert "✗" in out and "新檔起始行是 290" in out, out
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.smoke
def test_already_applied_with_out_of_range_new_start_is_rejected(
    runner: ToolExecutor, tmp_path: Path,
):
    """行號整組亂寫(超出檔案)→ 沒有可用座標,不得宣稱已套用。"""
    target = tmp_path / "idem2.py"
    target.write_text("a\nb\nc\n", encoding="utf-8")
    patch = (
        "--- a/idem2.py\n"
        "+++ b/idem2.py\n"
        "@@ -999,3 +999,3 @@\n"
        " a\n"
        "-b\n"
        "+B\n"
        " c\n"
    )
    assert "✓" in runner.apply_patch(patch)
    out = runner.apply_patch(patch)
    assert "✗" in out and "超出檔案範圍" in out, out
    assert "行 1" in out, out
    assert target.read_text(encoding="utf-8") == "a\nB\nc\n"


@pytest.mark.smoke
@pytest.mark.parametrize(
    "filename,header,expected_error",
    [
        pytest.param("drift.py", "@@ -1,2 +1,2 @@\n", "新檔起始行是 1", id="when_hint_still_holds_pre_image"),
        pytest.param("drift_bare.py", "@@\n", "沒有寫新檔行號", id="for_bare_header_when_pre_image_remains"),
    ],
)
def test_already_applied_rejected_when_pre_image_remains(
    runner: ToolExecutor, tmp_path: Path, filename: str, header: str, expected_error: str,
):
    """目標區塊漂移(hint 那裡還像修改前)+ 別處唯一相同 post-image → fail loud。

    - when_hint_still_holds_pre_image:唯一性與 100 行窗都擋不住這種:必須用「hint 附近
      仍是 pre-image」當反證。
    - for_bare_header_when_pre_image_remains:裸 `@@`(沒有行號)也不能只靠「post-image
      唯一」宣稱已套用。目標區塊漂移時 post-image 一樣是唯一的;沒有 hint 就掃全檔找反證。
    """
    target = tmp_path / filename
    original = "x = 0\ntail\n\ndef other():\n    pass\n\nx = 2\ntail\n"
    target.write_text(original, encoding="utf-8")
    patch = (
        f"--- a/{filename}\n"
        f"+++ b/{filename}\n"
        f"{header}"
        "-x = 1\n"
        "+x = 2\n"
        " tail\n"
    )
    out = runner.apply_patch(patch)
    assert "✗" in out and expected_error in out, out
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.smoke
def test_already_applied_accepted_when_post_image_sits_on_hint(
    runner: ToolExecutor, tmp_path: Path,
):
    """post-image 正好在 hint 指的行 → 即使別處有高度相似的舊版,也必須 no-op。"""
    target = tmp_path / "exact.py"
    original = (
        "def a():\n    x = 2\n    return x\n"
        "\n"
        "def a_old():\n    x = 1\n    return x\n"
    )
    target.write_text(original, encoding="utf-8")
    patch = (
        "--- a/exact.py\n"
        "+++ b/exact.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def a():\n"
        "-    x = 1\n"
        "+    x = 2\n"
        "     return x\n"
    )
    out = runner.apply_patch(patch)
    assert "✓" in out and "✗" not in out, out
    assert "已套用" in out and "行 1" in out, out
    assert target.read_text(encoding="utf-8") == original


# ── 原 test_patch_byte_safety.py:byte-level 檔案安全(UTF-8 strict / newline / 原子替換 / rollback)──
FORMATS = ("search_replace", "unified_diff")
ROLLBACK_TITLE = "✗ 套用失敗；已執行 best-effort rollback"
DEGRADED_NOTE = "⚠ 本平台無 dir_fd 錨定,symlink 競態防線為逐層 lstat 重驗"


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
@pytest.mark.parametrize(
    "filename,original,expected_error",
    [
        pytest.param(
            "mixed.txt",
            b"a\r\nb\nc\r\n",
            "✗ mixed.txt: 換行格式不一致（mixed newline: LF 與 CRLF 並存）",
            id="mixed_newlines",
        ),
        pytest.param("cr.txt", b"a\rb\rc\r", "✗ cr.txt: 換行格式不支援（CR-only", id="cr_only_newlines"),
    ],
)
def test_unsupported_newlines_rejected_and_bytes_untouched(
    runner: ToolExecutor, tmp_path: Path, fmt: str, filename: str, original: bytes, expected_error: str,
):
    """mixed newline 與 CR-only 都拒絕,且 bytes 原封不動。"""
    target = tmp_path / filename
    target.write_bytes(original)
    out = runner.apply_patch(edit(fmt, filename, "b", "B"))
    assert expected_error in out, out
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


# ── 原 test_patch_search_replace.py:SEARCH/REPLACE 格式(grammar / 路徑安全 / exact 定位 / dry_run)──
REPO_ROOT = Path(__file__).resolve().parent.parent

FENCE_TITLE = "✗ patch 格式錯誤: 參數已是字串,不要再包 Markdown fence（行 "
MISMATCH_MARKER = "（mismatch 預覽已達上限 40 行/2000 字元,省略 "


def sr(path: str, search_lines: list[str], replace_lines: list[str]) -> str:
    """組一個 canonical S/R block(raw grammar,無 fence)。空 list = 空 SEARCH / 空 REPLACE。"""
    parts = [path, SEARCH, *search_lines, SEP, *replace_lines, REPLACE]
    return "\n".join(parts) + "\n"


def ud(path: str, old: str, new: str) -> str:
    return f"--- a/{path}\n+++ b/{path}\n@@\n-{old}\n+{new}\n"


def _names(tmp_path: Path) -> set[str]:
    return {p.name for p in tmp_path.iterdir()}


def _preview_lines(out: str) -> list[str]:
    """mismatch 預覽區 = 所有以兩個空白起始的行(含省略標記);✗ / ⚠ 標題行不算。"""
    return [line for line in out.splitlines() if line.startswith("  ")]


# ---------------------------------------------------------------------------
# 基本套用
# ---------------------------------------------------------------------------
def test_sr_exact_block_applies_and_reports_format(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "m.py"
    target.write_bytes(b"def f():\n    return 1\n")
    out = runner.apply_patch(sr("m.py", ["    return 1"], ["    return 2"]))
    assert "✓ m.py: 已修改 1 個區塊 [search_replace]" in out, out
    assert "✗" not in out, out
    assert target.read_bytes() == b"def f():\n    return 2\n"


def test_sr_trailing_whitespace_is_tolerated(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "w.py"
    target.write_bytes(b"x = 1\ny = 2\n")
    out = runner.apply_patch(sr("w.py", ["x = 1   "], ["x = 2"]))
    assert "✓ w.py: 已修改 1 個區塊 [search_replace]" in out, out
    assert target.read_bytes() == b"x = 2\ny = 2\n"


def test_sr_path_may_be_separated_from_marker_by_blank_lines(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "gap.py"
    target.write_bytes(b"a\n")
    text = "gap.py\n\n\n" + SEARCH + "\na\n" + SEP + "\nA\n" + REPLACE + "\n"
    out = runner.apply_patch(text)
    assert "✓ gap.py: 已修改 1 個區塊 [search_replace]" in out, out
    assert target.read_bytes() == b"A\n"


# ---------------------------------------------------------------------------
# 定位:exact + rstrip,禁止 strip / 相似度代套;多處匹配一律拒絕
# ---------------------------------------------------------------------------
def test_sr_indent_only_similarity_is_rejected_and_never_applied(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "ind.py"
    original = b"def f():\n    x = 1\n    return x\n"
    target.write_bytes(original)
    # SEARCH 用 2 空格縮排,檔案是 4 空格:udiff 的 loose 容忍在 S/R 不存在
    out = runner.apply_patch(sr("ind.py", ["  x = 1"], ["    x = 2"]))
    assert "✗ ind.py: SEARCH/REPLACE 區塊 1 找不到逐字匹配" in out, out
    assert "不會代套" in out, out
    assert "最接近的位置: 行 2" in out, out
    assert target.read_bytes() == original


def test_sr_first_diff_reports_expected_and_actual_with_read_file_hint(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "diag.py"
    original = b"alpha\nbeta\ngamma\n"
    target.write_bytes(original)
    out = runner.apply_patch(sr("diag.py", ["alpha", "BETA_TYPO", "gamma"], ["alpha", "beta2", "gamma"]))
    assert "✗ diag.py: SEARCH/REPLACE 區塊 1 找不到逐字匹配" in out, out
    assert "第一個差異: 行 2" in out, out
    assert "期望:" in out and "實際:" in out, out
    assert "read_file" in out, out
    assert target.read_bytes() == original


def test_sr_ambiguous_match_is_rejected_with_zero_writes(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "amb.py"
    original = b"x = 1\nmarker\nx = 1\nmarker\ntail\n"
    target.write_bytes(original)
    out = runner.apply_patch(sr("amb.py", ["x = 1"], ["x = 2"]))
    assert "✗ amb.py: SEARCH/REPLACE 區塊 1 在檔案中出現 2 處（行 1, 3）" in out, out
    assert "拒絕套用" in out, out
    assert target.read_bytes() == original


def test_sr_ambiguity_lists_at_most_five_candidates(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "dup.py"
    original = b"dup\n" * 8
    target.write_bytes(original)
    out = runner.apply_patch(sr("dup.py", ["dup"], ["DUP"]))
    assert "✗ dup.py: SEARCH/REPLACE 區塊 1 在檔案中出現 8 處" in out, out
    candidate_lines = [line for line in out.splitlines() if re.match(r"^\s+行 \d+: ", line)]
    assert len(candidate_lines) == 5, out
    assert target.read_bytes() == original


def test_sr_overlapping_blocks_rejected(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "ov.py"
    original = b"p\nq\nr\n"
    target.write_bytes(original)
    text = sr("ov.py", ["p", "q"], ["P", "Q"]) + sr("ov.py", ["q", "r"], ["Q2", "R"])
    out = runner.apply_patch(text)
    assert "✗ ov.py: SEARCH/REPLACE 區塊 1 與區塊 2 定位後重疊" in out, out
    assert target.read_bytes() == original


def test_sr_blocks_locate_against_original_snapshot(runner: ToolExecutor, tmp_path: Path):
    """block 2 的 SEARCH 只有在 block 1 套用之後才存在 → 必須拒絕(不靠中間狀態命中)。"""
    target = tmp_path / "snap.py"
    original = b"alpha\nbeta\n"
    target.write_bytes(original)
    text = sr("snap.py", ["alpha"], ["gamma"]) + sr("snap.py", ["gamma"], ["delta"])
    out = runner.apply_patch(text)
    assert "✗ snap.py: SEARCH/REPLACE 區塊 2 找不到逐字匹配" in out, out
    assert target.read_bytes() == original


def test_sr_blank_search_line_cannot_match_eof_sentinel(runner: ToolExecutor, tmp_path: Path):
    """檔尾換行產生的 sentinel 與 0-byte 檔都不是可匹配的「空白行」。"""
    with_newline = tmp_path / "nl.py"
    with_newline.write_bytes(b"a\nb\n")
    out = runner.apply_patch(sr("nl.py", [""], ["INSERTED"]))
    assert "✗ nl.py: SEARCH/REPLACE 區塊 1 找不到逐字匹配" in out, out
    assert with_newline.read_bytes() == b"a\nb\n"

    empty = tmp_path / "e.py"
    empty.write_bytes(b"")
    out = runner.apply_patch(sr("e.py", [""], ["INSERTED"]))
    assert "✗ e.py: SEARCH/REPLACE 區塊 1 找不到逐字匹配" in out, out
    assert empty.read_bytes() == b""


# ---------------------------------------------------------------------------
# grammar:同一輸入只允許一種格式;mixed / 孤立 marker / fence / 垃圾 = fail-loud
# ---------------------------------------------------------------------------
def test_sr_mixed_udiff_first_rejected(runner: ToolExecutor, tmp_path: Path):
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_bytes(b"a\n")
    b.write_bytes(b"b\n")
    out = runner.apply_patch(ud("a.py", "a", "A") + sr("b.py", ["b"], ["B"]))
    assert out.startswith("✗ patch 格式錯誤: 混用 unified diff 與 SEARCH/REPLACE"), out
    assert a.read_bytes() == b"a\n", "udiff-first mixed 不得偷套前半段"
    assert b.read_bytes() == b"b\n"


def test_sr_mixed_sr_first_rejected(runner: ToolExecutor, tmp_path: Path):
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_bytes(b"a\n")
    b.write_bytes(b"b\n")
    out = runner.apply_patch(sr("b.py", ["b"], ["B"]) + ud("a.py", "a", "A"))
    assert out.startswith("✗ patch 格式錯誤: 混用 unified diff 與 SEARCH/REPLACE"), out
    assert a.read_bytes() == b"a\n"
    assert b.read_bytes() == b"b\n"


@pytest.mark.parametrize(
    "text,expected",
    [
        pytest.param("a.py\n" + SEARCH + "\nx\n" + SEP + "\ny\n", "區塊 1 未結束", id="missing-replace-marker"),
        pytest.param("a.py\n" + SEARCH + "\nx\n" + REPLACE + "\n", "區塊 1: 缺少 =======", id="missing-separator"),
        pytest.param(
            "a.py\n" + SEARCH + "\nx\n" + SEP + "\ny\n" + SEP + "\nz\n" + REPLACE + "\n",
            "區塊 1: 重複的 =======",
            id="duplicate-separator",
        ),
        pytest.param("a.py\n" + SEP + "\n", "孤立 marker", id="orphan-separator"),
        pytest.param(SEARCH + "\nx\n" + SEP + "\ny\n" + REPLACE + "\n", "不符 canonical grammar", id="marker-without-path"),
        pytest.param("a.py\n<<<<<<< SEARCH extra\nx\n" + SEP + "\ny\n" + REPLACE + "\n", "不符 canonical grammar", id="marker-with-suffix"),
    ],
)
def test_sr_incomplete_block_rejected(runner: ToolExecutor, tmp_path: Path, text: str, expected: str):
    target = tmp_path / "a.py"
    target.write_bytes(b"x\n")
    out = runner.apply_patch(text)
    assert out.startswith("✗ patch 格式錯誤: "), out
    assert expected in out, out
    assert target.read_bytes() == b"x\n"


@pytest.mark.parametrize(
    "wrap,lineno",
    [
        pytest.param(lambda body: "```\n" + body + "```\n", 1, id="backtick-fence-around"),
        pytest.param(lambda body: "~~~\n" + body + "~~~\n", 1, id="tilde-fence-around"),
        pytest.param(lambda body: "```python\n" + body, 1, id="backtick-fence-with-lang"),
        pytest.param(lambda body: body + "```\n", 7, id="fence-after-valid-block"),
        pytest.param(lambda body: body + "~~~\n", 7, id="tilde-after-valid-block"),
    ],
)
def test_sr_markdown_fence_rejected(runner: ToolExecutor, tmp_path: Path, wrap, lineno: int):
    target = tmp_path / "f.py"
    target.write_bytes(b"x\n")
    out = runner.apply_patch(wrap(sr("f.py", ["x"], ["y"])))
    assert out.startswith(f"{FENCE_TITLE}{lineno}）"), out
    assert target.read_bytes() == b"x\n"
    assert _names(tmp_path) == {"f.py"}


def test_sr_garbage_outside_blocks_rejected(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "g.py"
    target.write_bytes(b"x\n")
    out = runner.apply_patch(sr("g.py", ["x"], ["y"]) + "Here is what I changed.\n")
    assert out.startswith("✗ patch 格式錯誤: "), out
    assert "marker 外有非空內容" in out, out
    assert target.read_bytes() == b"x\n"


def test_sr_lone_cr_in_patch_text_rejected(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "cr.py"
    target.write_bytes(b"x\n")
    out = runner.apply_patch("cr.py\r" + SEARCH + "\nx\n" + SEP + "\ny\n" + REPLACE + "\n")
    assert out.startswith("✗ patch 格式錯誤: "), out
    assert "孤立 CR" in out, out
    assert target.read_bytes() == b"x\n"


def test_sr_crlf_patch_text_applies_to_lf_and_crlf_targets(runner: ToolExecutor, tmp_path: Path):
    lf = tmp_path / "lf.py"
    lf.write_bytes(b"a\nb\n")
    crlf = tmp_path / "crlf.py"
    crlf.write_bytes(b"a\r\nb\r\n")
    text = (sr("lf.py", ["b"], ["B"]) + sr("crlf.py", ["b"], ["B"])).replace("\n", "\r\n")
    out = runner.apply_patch(text)
    assert "✓ lf.py: 已修改 1 個區塊 [search_replace]" in out, out
    assert "✓ crlf.py: 已修改 1 個區塊 [search_replace]" in out, out
    assert lf.read_bytes() == b"a\nB\n"
    assert crlf.read_bytes() == b"a\r\nB\r\n"


# ---------------------------------------------------------------------------
# path:repo-relative POSIX;絕對 / drive / UNC / . / .. / /dev/null / NUL / 控制字元全拒
# ---------------------------------------------------------------------------
_ABS = "絕對路徑 / UNC;必須是 repo 相對路徑"
_BACKSLASH = "含反斜線;請用 POSIX 的 /,且不接受 UNC"
_DOTS = "含 . 或 .. component"
_CONTROL = "含 NUL 或控制字元"
_EMPTY_COMPONENT = "含空 component:// 或結尾 /"
_WHITESPACE = "path 行含前後空白"


@pytest.mark.parametrize(
    "raw,display,reason",
    [
        pytest.param("/abs/x.py", "/abs/x.py", _ABS, id="absolute"),
        pytest.param("C:\\x.py", "C:\\x.py", _BACKSLASH, id="drive-backslash"),
        pytest.param("C:/x.py", "C:/x.py", "Windows drive", id="drive-slash"),
        pytest.param("\\\\server\\share\\x.py", "\\\\server\\share\\x.py", _BACKSLASH, id="unc"),
        pytest.param(".", ".", _DOTS, id="dot"),
        pytest.param("..", "..", _DOTS, id="dotdot"),
        pytest.param("../x.py", "../x.py", _DOTS, id="dotdot-escape"),
        pytest.param("a/../../x.py", "a/../../x.py", _DOTS, id="nested-dotdot"),
        pytest.param("./a.py", "./a.py", _DOTS, id="dot-prefix"),
        pytest.param("/dev/null", "/dev/null", _ABS, id="dev-null-absolute"),
        pytest.param("dev/null", "dev/null", "/dev/null", id="dev-null-relative"),
        pytest.param("a\x00b.py", "a\\x00b.py", _CONTROL, id="nul"),
        pytest.param("a\x1bb.py", "a\\x1bb.py", _CONTROL, id="c0-escape"),
        pytest.param("a\x85b.py", "a\\x85b.py", _CONTROL, id="c1-nel"),
        pytest.param("a\u202eb.py", "a\\u202eb.py", _CONTROL, id="bidi-override"),
        pytest.param("a\ud800.py", "a\\ud800.py", "含無法以 UTF-8 編碼的字元", id="surrogate"),
        pytest.param("a//b.py", "a//b.py", _EMPTY_COMPONENT, id="double-slash"),
        pytest.param("a/b.py/", "a/b.py/", _EMPTY_COMPONENT, id="trailing-slash"),
        pytest.param(" a.py", " a.py", _WHITESPACE, id="leading-space"),
        pytest.param("a.py ", "a.py ", _WHITESPACE, id="trailing-space"),
    ],
)
def test_sr_path_escapes_rejected(runner: ToolExecutor, tmp_path: Path, raw: str, display: str, reason: str):
    out = runner.apply_patch(sr(raw, [], ["pwned = True"]))
    title = f"✗ {display}: 路徑無效（{reason}）"
    assert out.startswith("✗ "), out
    assert title in out, f"缺少完整標題 {title!r}\n{out}"
    assert _names(tmp_path) == set(), "sandbox 內不得有任何新檔"
    assert not (tmp_path.parent / "x.py").exists()
    assert not (tmp_path.parent / "a.py").exists()


def test_sr_path_docs_dev_null_is_a_legal_relative_path(runner: ToolExecutor, tmp_path: Path):
    """只拒絕精確的 /dev/null 與 dev/null,不誤殺 docs/dev/null 這種合法子路徑。"""
    out = runner.apply_patch(sr("docs/dev/null", [], ["notes"]))
    assert "✓ docs/dev/null: 新建檔案 [search_replace]" in out, out
    assert (tmp_path / "docs" / "dev" / "null").read_bytes() == b"notes\n"


def test_sr_content_with_surrogate_is_rejected_with_zero_writes(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "s.py"
    target.write_bytes(b"x\n")
    out = runner.apply_patch(sr("s.py", ["x"], ["y\ud800"]))
    assert "✗ s.py: 區塊 1 含無法以 UTF-8 編碼的字元" in out, out
    assert target.read_bytes() == b"x\n"
    assert _names(tmp_path) == {"s.py"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink containment")
def test_sr_symlink_escape_rejected(runner: ToolExecutor, tmp_path: Path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "victim.py"
    outside.write_bytes(b"print('safe')\n")
    os.symlink(outside, tmp_path / "link.py")
    out = runner.apply_patch(sr("link.py", ["print('safe')"], ["print('pwned')"]))
    assert "✗ link.py: 目標或其路徑上有 symlink" in out, out
    assert outside.read_bytes() == b"print('safe')\n"
    assert (tmp_path / "link.py").is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink containment")
def test_sr_symlink_inside_root_is_refused_as_write_target(runner: ToolExecutor, tmp_path: Path):
    real = tmp_path / "real.py"
    real.write_bytes(b"v = 1\n")
    os.symlink(real, tmp_path / "alias.py")
    out = runner.apply_patch(sr("alias.py", ["v = 1"], ["v = 2"]))
    assert "✗ alias.py: 目標或其路徑上有 symlink" in out, out
    assert real.read_bytes() == b"v = 1\n"
    assert (tmp_path / "alias.py").is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink containment")
def test_sr_symlinked_directory_in_path_is_refused(runner: ToolExecutor, tmp_path: Path):
    realdir = tmp_path / "realdir"
    realdir.mkdir()
    (realdir / "x.py").write_bytes(b"v = 1\n")
    os.symlink(realdir, tmp_path / "lnkdir")
    out = runner.apply_patch(sr("lnkdir/x.py", ["v = 1"], ["v = 2"]))
    assert "✗ lnkdir/x.py: 目標或其路徑上有 symlink" in out, out
    assert (realdir / "x.py").read_bytes() == b"v = 1\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink containment")
def test_sr_broken_symlink_target_is_refused(runner: ToolExecutor, tmp_path: Path):
    os.symlink(tmp_path / "nowhere.py", tmp_path / "broken.py")
    out = runner.apply_patch(sr("broken.py", [], ["v = 1"]))
    assert "✗ broken.py: 目標或其路徑上有 symlink" in out, out
    assert (tmp_path / "broken.py").is_symlink()
    assert not (tmp_path / "nowhere.py").exists()


# ---------------------------------------------------------------------------
# 上限與全量 preflight
# ---------------------------------------------------------------------------
def test_sr_multi_file_preflight_failure_writes_nothing(runner: ToolExecutor, tmp_path: Path):
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_bytes(b"a\n")
    b.write_bytes(b"b\n")
    out = runner.apply_patch(sr("a.py", ["a"], ["A"]) + sr("b.py", ["nope"], ["B"]))
    assert "✗ b.py: SEARCH/REPLACE 區塊 1 找不到逐字匹配" in out, out
    assert "⚠ 因有檔案未通過 preflight,整份 patch 已被拒絕,未寫入任何檔案（全量 preflight）" in out, out
    assert a.read_bytes() == b"a\n"
    assert b.read_bytes() == b"b\n"


def test_file_limit_is_five_ok_six_rejected(runner: ToolExecutor, tmp_path: Path):
    assert config.PATCH_MAX_FILES == 5, "施工單釘的實值"
    for i in range(6):
        (tmp_path / f"f{i}.py").write_bytes(b"v = 0\n")
    five = "".join(sr(f"f{i}.py", ["v = 0"], ["v = 1"]) for i in range(5))
    out = runner.apply_patch(five)
    assert "✗" not in out, out
    for i in range(5):
        assert f"✓ f{i}.py: 已修改 1 個區塊 [search_replace]" in out, out
        assert (tmp_path / f"f{i}.py").read_bytes() == b"v = 1\n"

    for i in range(6):
        (tmp_path / f"f{i}.py").write_bytes(b"v = 0\n")
    six = "".join(sr(f"f{i}.py", ["v = 0"], ["v = 1"]) for i in range(6))
    out = runner.apply_patch(six)
    assert out.startswith("✗ 修改檔案數量超過限制（6 > 5）"), out
    for i in range(6):
        assert (tmp_path / f"f{i}.py").read_bytes() == b"v = 0\n"


def test_sr_payload_budget_200_ok_201_rejected(runner: ToolExecutor, tmp_path: Path):
    assert config.PATCH_MAX_LINES_PER_FILE == 200, "施工單釘的實值"
    body = "".join(f"L{i:03d}\n" for i in range(1, 301)).encode()
    target = tmp_path / "big.py"
    target.write_bytes(body)
    search = [f"L{i:03d}" for i in range(1, 101)]              # 100 行
    replace_ok = [f"R{i:03d}" for i in range(1, 101)]          # 100 行 → 200
    out = runner.apply_patch(sr("big.py", search, replace_ok))
    assert "✓ big.py: 已修改 1 個區塊 [search_replace]" in out, out
    assert target.read_bytes().startswith(b"R001\nR002\n")

    target.write_bytes(body)
    replace_over = replace_ok + ["EXTRA"]                      # 101 行 → 201
    out = runner.apply_patch(sr("big.py", search, replace_over))
    assert "✗ big.py: S/R payload budget 超過限制（201 > 200）" in out, out
    assert target.read_bytes() == body


# ---------------------------------------------------------------------------
# 空 SEARCH = 建新檔(且只在三條件同時成立時)
# ---------------------------------------------------------------------------
def test_sr_empty_search_creates_new_nested_file(runner: ToolExecutor, tmp_path: Path):
    out = runner.apply_patch(sr("newdir/sub/x.py", [], ["print('hi')"]))
    assert "✓ newdir/sub/x.py: 新建檔案 [search_replace]" in out, out
    assert "✗" not in out, out
    assert (tmp_path / "newdir" / "sub" / "x.py").read_bytes() == b"print('hi')\n"


def test_sr_empty_search_rejected_when_file_exists_even_zero_byte(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "z.py"
    target.write_bytes(b"")
    out = runner.apply_patch(sr("z.py", [], ["v = 1"]))
    assert "✗ z.py: 空 SEARCH 只能建立不存在的新檔（檔案已存在）" in out, out
    assert target.read_bytes() == b""


def test_sr_empty_search_rejected_for_multiple_blocks_or_empty_replace(runner: ToolExecutor, tmp_path: Path):
    out = runner.apply_patch(sr("n.py", [], ["v = 1"]) + sr("n.py", [], ["v = 2"]))
    assert "✗ n.py: 空 SEARCH 必須是該檔唯一的區塊（共 2 個區塊）" in out, out
    assert not (tmp_path / "n.py").exists()

    out = runner.apply_patch(sr("m.py", [], []))
    assert "✗ m.py: 空 SEARCH 且空 REPLACE" in out, out
    assert not (tmp_path / "m.py").exists()


def test_sr_nonempty_search_on_missing_file_rejected(runner: ToolExecutor, tmp_path: Path):
    out = runner.apply_patch(sr("ghost.py", ["x"], ["y"]))
    assert "✗ ghost.py: 檔案不存在" in out, out
    assert not (tmp_path / "ghost.py").exists()


# ---------------------------------------------------------------------------
# dry_run:固定欄位(成功或失敗每檔一行)、零副作用
# ---------------------------------------------------------------------------
def test_sr_dry_run_reports_plan_and_has_zero_side_effects(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "m.py"
    target.write_bytes(b"def f():\n    return 1\n")
    text = sr("m.py", ["    return 1"], ["    return 2"]) + sr("newdir/sub/x.py", [], ["print('hi')"])
    out = runner.apply_patch(text, dry_run=True)
    assert "[DRY RUN] m.py: format=search_replace blocks=1 budget=2/200 new_file=no locations=行 2-2" in out, out
    assert "[DRY RUN] newdir/sub/x.py: format=search_replace blocks=1 budget=1/200 new_file=yes locations=new file" in out, out
    assert "[DRY RUN] would apply: 2 個檔案" in out, out
    assert target.read_bytes() == b"def f():\n    return 1\n"
    assert not (tmp_path / "newdir").exists(), "dry_run 不得建目錄"
    assert _names(tmp_path) == {"m.py"}, "dry_run 不得留 temp / backup"


def test_sr_dry_run_lists_every_preflight_failure(runner: ToolExecutor, tmp_path: Path):
    (tmp_path / "ok.py").write_bytes(b"a\n")
    (tmp_path / "bad.py").write_bytes(b"b\n")
    out = runner.apply_patch(sr("ok.py", ["a"], ["A"]) + sr("bad.py", ["zzz"], ["B"]), dry_run=True)
    assert "[DRY RUN] ok.py: format=search_replace blocks=1 budget=2/200 new_file=no locations=行 1-1" in out, out
    assert "[DRY RUN] bad.py: format=search_replace blocks=1 budget=2/200 new_file=no locations=preflight failed" in out, out
    assert "✗ bad.py: SEARCH/REPLACE 區塊 1 找不到逐字匹配" in out, out
    assert "would apply" not in out, out
    assert "⚠ [DRY RUN] 上述 ✗ 檔案未通過 preflight" in out, out
    assert (tmp_path / "ok.py").read_bytes() == b"a\n"


# ---------------------------------------------------------------------------
# mismatch 回饋:整次 tool result 的總上限(省略標記計入,恰好一次)
# ---------------------------------------------------------------------------
def test_sr_mismatch_preview_is_bounded_per_tool_result(runner: ToolExecutor, tmp_path: Path):
    text = ""
    for i in range(5):
        (tmp_path / f"m{i}.py").write_bytes("".join(f"line{j:02d} of file {i}\n" for j in range(30)).encode())
        text += sr(f"m{i}.py", [f"wrong{j:02d}" for j in range(10)], ["x"])
    out = runner.apply_patch(text)
    for i in range(5):
        assert f"✗ m{i}.py: SEARCH/REPLACE 區塊 1 找不到逐字匹配" in out, out
    preview = _preview_lines(out)
    assert len(preview) <= 40, f"preview 行數 {len(preview)} 超過總上限\n{out}"
    assert sum(len(line) + 1 for line in preview) <= 2000, out
    assert out.count(MISMATCH_MARKER) == 1, out
    assert preview[-1].startswith("  " + MISMATCH_MARKER), preview[-1]


def test_sr_mismatch_preview_under_budget_has_no_marker(runner: ToolExecutor, tmp_path: Path):
    (tmp_path / "one.py").write_bytes(b"alpha\nbeta\n")
    out = runner.apply_patch(sr("one.py", ["gamma"], ["x"]))
    assert "✗ one.py: SEARCH/REPLACE 區塊 1 找不到逐字匹配" in out, out
    assert MISMATCH_MARKER not in out, out
    assert "提示: 先 read_file 確認現況" in out, out


# ---------------------------------------------------------------------------
# 公開契約:MCP 參數名 diff(不變)、executor 參數名 patch、schema 說明含新格式
# ---------------------------------------------------------------------------
def test_public_mcp_param_is_diff_and_executor_param_is_patch():
    tree = ast.parse((REPO_ROOT / "mcp_server.py").read_text(encoding="utf-8"))
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "apply_patch"
    )
    params = [arg.arg for arg in fn.args.args]
    assert params == ["diff", "dry_run"], params
    doc = ast.get_docstring(fn) or ""
    assert "SEARCH/REPLACE" in doc, "MCP docstring 必須說明第二格式"
    assert "不要再包 Markdown fence" in doc, doc
    assert "最多 5 個檔案" in doc, doc
    assert "S/R 單檔 payload budget = sum(SEARCH 行數 + REPLACE 行數) ≤ 200" in doc, doc
    assert "udiff 單檔 added+removed ≤ 200 行" in doc, doc
    # 驗證分層的肯定契約(C):syntax-only、advisory 不回滾、PATCH_AUTO_VERIFY=False、公開工具名
    assert "syntax check" in doc, doc
    assert "patch 已套用、未回滾" in doc, doc
    assert "PATCH_AUTO_VERIFY=False" in doc, doc
    assert "run_lint(fix=False)" in doc, doc
    assert "run_command" in doc, doc
    assert "套用後會自動跑 lint / typecheck / 相關測試" not in doc, "舊的完整宣稱句必須消失"
    # dry_run 七欄位
    for field in ("format", "檔案清單", "blocks", "budget", "locations", "new_file", "would apply"):
        assert field in doc, f"docstring 缺 dry_run 欄位 {field!r}"

    executor_params = list(inspect.signature(ToolExecutor.apply_patch).parameters)
    assert executor_params == ["self", "patch", "dry_run"], executor_params

    native = _APPLY_PATCH_TOOL["function"]
    assert "SEARCH/REPLACE" in native["description"], native["description"]
    props = native["parameters"]["properties"]
    assert set(props) == {"patch", "dry_run"}, sorted(props)
    assert "SEARCH/REPLACE" in props["patch"]["description"], props["patch"]["description"]
    for field in ("format", "檔案清單", "blocks", "budget", "locations", "new_file", "would apply"):
        assert field in native["description"], f"native description 缺 dry_run 欄位 {field!r}"
        assert field in props["dry_run"]["description"], f"dry_run 參數說明缺 {field!r}"
