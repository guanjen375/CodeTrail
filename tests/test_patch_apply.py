"""apply_patch 的套用階段:context 必須匹配、行號定位、已套用偵測、上限保護。

合併自 tests/test_patch_safety.py 與 tests/test_patch_context_locate.py(2026-08-20)。
這是 AGENTS.md §3 點名的安全檢查點之一(context 必須匹配 / max files / max lines),
整份都標 smoke。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import config
from agent_tools import ToolExecutor

# smoke:安全層(AGENTS.md §2.1 第 2 款「無聲失敗風險的契約」)
# AGENTS.md §3 安全檢查點:apply_patch 的「context 必須匹配」與 max files / max lines。
pytestmark = pytest.mark.smoke


@pytest.fixture
def runner(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "PATCH_ENABLED", True)
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", False)
    monkeypatch.setattr(config, "PATCH_AUTO_VERIFY", False)
    monkeypatch.setattr(config, "PATCH_VERIFY_STEPS", [])
    return ToolExecutor(str(tmp_path))


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


def test_pure_insertion_at_eof_keeps_trailing_newline(runner: ToolExecutor, tmp_path: Path):
    """插在最後一行之後(hint == 實際行數)是合法邊界,且不新增空行。"""
    target = tmp_path / "eof.py"
    target.write_text("a\nb\n", encoding="utf-8")
    patch = (
        "--- a/eof.py\n"
        "+++ b/eof.py\n"
        "@@ -2,0 +3,1 @@\n"
        "+APPENDED\n"
    )
    out = runner.apply_patch(patch)
    assert "✓" in out and "✗" not in out, out
    assert target.read_text(encoding="utf-8") == "a\nb\nAPPENDED\n"


def test_pure_insertion_into_file_without_trailing_newline(runner: ToolExecutor, tmp_path: Path):
    """檔尾沒有換行:行數不含 sentinel,插到最後一行之後仍不加尾端換行。"""
    target = tmp_path / "nonl.py"
    target.write_text("a\nb", encoding="utf-8")
    patch = (
        "--- a/nonl.py\n"
        "+++ b/nonl.py\n"
        "@@ -2,0 +3,1 @@\n"
        "+APPENDED\n"
    )
    out = runner.apply_patch(patch)
    assert "✓" in out and "✗" not in out, out
    assert target.read_text(encoding="utf-8") == "a\nb\nAPPENDED"


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
def test_already_applied_rejected_when_hint_still_holds_pre_image(
    runner: ToolExecutor, tmp_path: Path,
):
    """目標區塊漂移(hint 那裡還像修改前)+ 別處唯一相同 post-image → fail loud。

    唯一性與 100 行窗都擋不住這種:必須用「hint 附近仍是 pre-image」當反證。
    """
    target = tmp_path / "drift.py"
    original = "x = 0\ntail\n\ndef other():\n    pass\n\nx = 2\ntail\n"
    target.write_text(original, encoding="utf-8")
    patch = (
        "--- a/drift.py\n"
        "+++ b/drift.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-x = 1\n"
        "+x = 2\n"
        " tail\n"
    )
    out = runner.apply_patch(patch)
    assert "✗" in out and "新檔起始行是 1" in out, out
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.smoke
def test_already_applied_rejected_for_bare_header_when_pre_image_remains(
    runner: ToolExecutor, tmp_path: Path,
):
    """裸 `@@`(沒有行號)也不能只靠「post-image 唯一」宣稱已套用。

    目標區塊漂移時 post-image 一樣是唯一的;沒有 hint 就掃全檔找反證。
    """
    target = tmp_path / "drift_bare.py"
    original = "x = 0\ntail\n\ndef other():\n    pass\n\nx = 2\ntail\n"
    target.write_text(original, encoding="utf-8")
    patch = (
        "--- a/drift_bare.py\n"
        "+++ b/drift_bare.py\n"
        "@@\n"
        "-x = 1\n"
        "+x = 2\n"
        " tail\n"
    )
    out = runner.apply_patch(patch)
    assert "✗" in out and "沒有寫新檔行號" in out, out
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
