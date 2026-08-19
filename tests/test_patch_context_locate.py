"""apply_patch content 定位契約(2026-08-19 改版)。

背景:本機小模型套 patch 時 hunk header 行號/行數幾乎必錯,舊版
「old_start 直接當位置 + header 行數 strict 核對」讓它陷入
「改 header → 再被拒 → 再改」的重試迴圈(error1/error2.png)。

新契約:
  - 定位靠 (context+移除) 行內容,行號只當多處匹配時的提示。
  - `@@` 可以完全不帶行號;行數宣稱一律忽略。
  - 多處匹配且無法消歧 → fail loud 列出候選行號,絕不猜。
  - 修改後內容已存在 → 視為已套用過(no-op,重試冪等)。
  - 模型把空白 context 行的行尾空白 strip 成 `""` → 不截斷 hunk。
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
def test_already_applied_hunk_is_noop(runner: ToolExecutor, tmp_path: Path):
    target = tmp_path / "idem.py"
    target.write_text("a\nb\nc\n", encoding="utf-8")
    patch = (
        "--- a/idem.py\n"
        "+++ b/idem.py\n"
        "@@\n"
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
