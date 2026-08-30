"""apply_patch 的 SEARCH/REPLACE 格式:canonical grammar、路徑安全、exact 定位、上限與 dry_run。

workflow A/E(2026-08-26)的新功能安全契約。S/R 沒有行號 hint:多處匹配一律拒絕、
僅縮排相似的候選只當提示絕不代套、所有 block 對同一份原始 snapshot 定位。

全部標 smoke(AGENTS.md §1.1 第 2 款「無聲失敗風險的契約」):S/R 走的是與 udiff
同一條 sandbox → limit → preflight → write → rollback 管線,任何一條鬆掉都是靜默寫錯檔。
"""
from __future__ import annotations

import ast
import inspect
import os
import re
from pathlib import Path

import pytest

import config
from agent_tools import _APPLY_PATCH_TOOL, ToolExecutor

pytestmark = pytest.mark.smoke

REPO_ROOT = Path(__file__).resolve().parent.parent

SEARCH = "<<<<<<< SEARCH"
SEP = "======="
REPLACE = ">>>>>>> REPLACE"
FENCE_TITLE = "✗ patch 格式錯誤: 參數已是字串,不要再包 Markdown fence（行 "
MISMATCH_MARKER = "（mismatch 預覽已達上限 40 行/2000 字元,省略 "


@pytest.fixture
def runner(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "PATCH_ENABLED", True)
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", False)
    monkeypatch.setattr(config, "PATCH_AUTO_VERIFY", False)
    monkeypatch.setattr(config, "PATCH_VERIFY_STEPS", [])
    return ToolExecutor(str(tmp_path))


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
    assert "codetrail_run_lint(fix=False)" in doc, doc
    assert "codetrail_run_command" in doc, doc
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
