#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FileKindPolicy 的契約(施工規格 §6 P3B,Level 1)。

NEW SILENT CONTRACT。兩份手寫清單漂掉是這個 repo 已經發生過的事:
``GREP_DEFAULT_EXTENSIONS`` 比 ``CODE_EXTENSIONS`` 還窄,連 ``.cc`` / ``.cxx`` /
``.pyi`` / ``.mk`` / ``.cmake`` / ``.tcl`` 這些**既有**格式都搜不到 —— 而且
沒有任何測試會因此變紅。這一包就是要讓那種漂移出聲。

同時鎖住 Level 1 的**承諾邊界**:新檔案類型只保證 grep / search 找得到,
不保證進 dense symbol retrieval。宣稱過頭跟漏做一樣糟。
"""
from __future__ import annotations

import fnmatch
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import code_rag  # noqa: E402
import config  # noqa: E402
import file_kind_policy as policy  # noqa: E402
import index_scope  # noqa: E402

pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def _fresh_scan_cache():
    code_rag._INDEX_SCAN_CACHE.clear()
    yield
    code_rag._INDEX_SCAN_CACHE.clear()


# ============================================================
# 單一來源:兩個 consumer 不得再各寫一份
# ============================================================
def test_grep_globs_cover_every_indexable_suffix():
    """grep 清單不得比索引清單窄 —— 那正是漂掉的方向。"""
    globs = set(config.GREP_DEFAULT_EXTENSIONS.split(","))
    for suffix in config.CODE_EXTENSIONS:
        assert f"*{suffix}" in globs, (
            f"{suffix} 進得了索引卻搜不到:兩份清單又漂了"
        )


def test_previously_missing_existing_formats_are_restored():
    """修新格式不能反而繼續漏舊格式(§6 P3B 明列的那一批)。"""
    globs = set(config.GREP_DEFAULT_EXTENSIONS.split(","))
    for suffix in (".cc", ".cxx", ".pyi", ".pyx", ".bash", ".txt",
                   ".mk", ".cfg", ".cmake", ".ini", ".conf", ".tcl"):
        assert f"*{suffix}" in globs, f"{suffix} 是既有格式,不得漏掉"


def test_firmware_file_kinds_are_indexable():
    for name in ("boot.s", "boot.S", "startup.asm", "link.ld", "layout.lds",
                 "soc.dts", "soc.dtsi", "regs.inc", "table.def"):
        assert policy.is_indexable(name), name


def test_uppercase_assembly_needs_its_own_case_sensitive_glob():
    """``rg -g`` 與 fallback 的 fnmatch 在 Linux 上都區分大小寫。

    canonical suffix 是小寫(``Path.suffix.lower()``),所以只產 ``*.s`` 的話,
    韌體最常見的 ``startup.S`` 會完全搜不到。
    """
    globs = config.GREP_DEFAULT_EXTENSIONS.split(",")
    assert "*.s" in globs and "*.S" in globs

    assert any(fnmatch.fnmatch("startup.S", g) for g in globs)
    assert any(fnmatch.fnmatch("startup.s", g) for g in globs)
    # 反證:只有小寫 glob 時大寫檔真的匹配不到(這條測試才有意義)。
    assert not fnmatch.fnmatch("startup.S", "*.s")


def test_extensionless_build_files_match_by_basename_rule():
    for name in ("Makefile", "makefile", "GNUmakefile", "Makefile.local",
                 "Kconfig", "Kconfig.debug"):
        assert policy.is_indexable(name), name
        assert policy.matches_basename_rule(name), name

    globs = config.GREP_DEFAULT_EXTENSIONS.split(",")
    for name in ("Makefile", "Kconfig", "Makefile.local", "Kconfig.debug"):
        assert any(fnmatch.fnmatch(name, g) for g in globs), name

    assert not policy.is_indexable("Makefilezzz")
    assert not policy.is_indexable("notes.bin")


# ============================================================
# Level 1 的承諾邊界
# ============================================================
def test_new_file_kinds_are_searchable_but_not_symbol_scanned():
    """Level 1 = grep discoverability。不宣稱它們進了 dense symbol retrieval。"""
    for name in ("startup.S", "link.ld", "soc.dts", "Makefile", "Kconfig"):
        assert policy.is_indexable(name), name
        assert not policy.enters_symbol_scan(name), (
            f"{name} 沒有 symbol parser,不該付 symbol 掃描成本"
        )


def test_doc_visibility_split_is_preserved():
    """.md/.txt 刻意留在可見範圍、排除於 symbol 掃描 —— 既有分工不得弄丟。"""
    assert ".md" in config.CODE_EXTENSIONS
    assert ".txt" in config.CODE_EXTENSIONS
    assert not policy.enters_symbol_scan("notes.md")
    assert not policy.enters_symbol_scan("todo.txt")


def test_config_formats_stay_in_the_symbol_scan():
    """.cfg/.json/.sh 本來就在掃描範圍。

    把它們一起排掉會縮小 ``_scan_code_files()``,而那份輸出同時是 bounded
    context 的 allowed_paths —— ``config/*.cfg`` 這類 gold evidence 會突然
    變成讀不到。這是成本最佳化換來檢索黑洞,不接受。
    """
    for name in ("layout.cfg", "app.json", "build.sh", "rules.mk"):
        assert policy.enters_symbol_scan(name), name


def test_index_scope_membership_uses_the_policy(tmp_path: Path):
    (tmp_path / "startup").mkdir()
    (tmp_path / "startup" / "vectors.S").write_text(".global x\n", encoding="utf-8")
    (tmp_path / "link.ld").write_text("ENTRY(reset)\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("all:\n\techo hi\n", encoding="utf-8")
    (tmp_path / "image.bin").write_bytes(b"\x00\x01")

    scope = index_scope.load_index_scope(tmp_path)
    assert scope.should_index_file("startup/vectors.S")
    assert scope.should_index_file("link.ld")
    assert scope.should_index_file("Makefile")
    assert not scope.should_index_file("image.bin")


def test_policy_rules_enter_the_scope_fingerprint(tmp_path: Path, monkeypatch):
    """規則(含 basename 規則)改了,舊 scope 快照就不能再被沿用。"""
    before = index_scope.load_index_scope(tmp_path).fingerprint

    monkeypatch.setattr(
        policy, "BASENAME_EXACT",
        frozenset(policy.BASENAME_EXACT | {"Kbuild"}),
    )
    after = index_scope.load_index_scope(tmp_path).fingerprint
    assert before != after, (
        "basename 規則改了 fingerprint 卻沒動 —— 成員資格會靜默漂移"
    )


def test_new_file_kinds_do_not_bypass_the_sandbox(tmp_path: Path):
    """沒有 parser 的檔案仍要走 _safe_path,不得因為「只是全文檢索」就放行。"""
    from agent_tools import ToolExecutor

    (tmp_path / "link.ld").write_text("ENTRY(reset_handler)\n", encoding="utf-8")
    executor = ToolExecutor(str(tmp_path))

    assert executor._safe_path("link.ld") is not None
    # containment 逃逸回 None(不是丟例外);grep/read 端據此拒絕。
    assert executor._safe_path("../outside.ld") is None
    assert executor._safe_path("/etc/passwd") is None
