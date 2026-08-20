#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FileKindPolicy —— 檔案類型的**單一來源**(施工規格 §6 P3B,Level 1)。

以前有兩份手寫清單:``CODE_EXTENSIONS`` 與 ``GREP_DEFAULT_EXTENSIONS``。
兩份已經漂了 —— grep 的那份比索引的還窄,連 ``.cc`` / ``.cxx`` / ``.pyi`` /
``.mk`` / ``.cmake`` / ``.tcl`` 這些**既有**格式都搜不到。手寫兩份清單的
必然結果就是這樣,而且漂了不會有人收到通知。

這裡改成一份 policy、三個投影:

    FileKindPolicy
      ├─ searchable_text_patterns  → grep glob(case-sensitive,必須同時
      │                              產生 ``*.S`` 與 ``*.s``)
      ├─ index_scope_membership    → containment / scope fingerprint
      └─ symbol_parser_route       → c / cpp / asm / linker_script /
                                     devicetree / none

**Level 1 只承諾 grep / search discoverability。** 它不會讓 ``.S`` / ``.ld`` /
``Makefile`` / ``Kconfig`` 自動進 dense symbol retrieval —— 那需要另外設計
file-level fallback document 的表示式、size budget 與 eval,不在本包範圍。
Level 2(ASM / linker script 的 symbol 抽取)整包延後:ASM 要 two-pass
(先收 ``.globl`` / ``.global``,再只配對相應 label,排除 ``.L*`` local label),
linker script 要抽 MEMORY region / output section / ``ENTRY()`` / symbol
assignment。寧可延後,也不要用粗 regex 製造大量假 symbol 反而傷 code inference。

這個模組刻意**不 import config**(config 反過來 import 它),保持零相依。
"""
from __future__ import annotations

from pathlib import Path

# policy 規則本身的版本。改任何一組清單都要 bump —— 它會進 index scope
# fingerprint,舊 scope 快照才不會被沿用。
FILE_KIND_POLICY_VERSION = 1

# ============================================================
# canonical suffix(一律小寫;比對前用 Path(name).suffix.lower())
# ============================================================
_C_FAMILY = {".c", ".h"}
_CPP_FAMILY = {".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"}
_PYTHON_FAMILY = {".py", ".pyx", ".pyi"}
_OTHER_CODE = {
    ".rs", ".go", ".java", ".kt",
    ".js", ".ts", ".jsx", ".tsx",
}

# 韌體專用。`.S`(大寫,帶 preprocessor 的組語)經 Path.suffix.lower() 正規化
# 成 `.s`,所以 canonical 只留小寫;grep glob 兩種都要產(見 grep_globs)。
_FIRMWARE_SOURCE = {".s", ".asm", ".inc", ".def"}
_LINKER_SCRIPT = {".ld", ".lds"}
_DEVICETREE = {".dts", ".dtsi"}

# 設定 / build 檔:沒有 symbol parser,但要能被 grep 到。
_CONFIG_LIKE = {
    ".json", ".yaml", ".yml", ".toml",
    ".sh", ".bash", ".mk", ".cmake",
    ".tcl", ".cfg", ".ini", ".conf",
}

# 文件:刻意留在可見範圍供 grep / list_dir 使用,但**不進 symbol 掃描**。
# 這個分工是既有行為,P3B 不得把它弄丟。
_DOC_LIKE = {".txt", ".md"}

INDEX_SUFFIXES = frozenset(
    _C_FAMILY | _CPP_FAMILY | _PYTHON_FAMILY | _OTHER_CODE
    | _FIRMWARE_SOURCE | _LINKER_SCRIPT | _DEVICETREE
    | _CONFIG_LIKE | _DOC_LIKE
)

# ============================================================
# basename 規則(無副檔名的 build / 設定檔)
# ============================================================
# 走 basename / prefix 規則而不是 suffix。規則本身要進 scope fingerprint,
# 否則規則改了舊快照還會被沿用。
BASENAME_EXACT = frozenset({"Makefile", "makefile", "GNUmakefile", "Kconfig"})
BASENAME_PREFIXES = ("Makefile.", "makefile.", "Kconfig.")

# ============================================================
# parser route
# ============================================================
_ROUTES = (
    ("c", _C_FAMILY),
    ("cpp", _CPP_FAMILY),
    ("python", _PYTHON_FAMILY),
    ("asm", _FIRMWARE_SOURCE - {".inc", ".def"}),
    ("linker_script", _LINKER_SCRIPT),
    ("devicetree", _DEVICETREE),
)
_ROUTE_BY_SUFFIX = {
    ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin",
    ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
}

# Level 1 沒有 symbol parser 的 route。
ROUTES_WITHOUT_SYMBOL_PARSER = frozenset({
    "asm", "linker_script", "devicetree", "config", "doc", "none",
})

# CodeRAG symbol 掃描要跳過的 suffix。
#
# 刻意**只**含 doc(既有行為)與 P3B 新增的韌體類型 —— .json / .cfg / .sh /
# .mk 這些設定檔本來就在掃描範圍內,雖然同樣抽不到 symbol。把它們一起排除
# 會縮小 `_scan_code_files()` 的輸出,而那份輸出同時是 bounded context 的
# `allowed_paths`:config/*.cfg 這類 gold evidence 會突然變成讀不到。
# 成本最佳化不值得換一個檢索黑洞,要動請另開工作包並同步改 eval。
SYMBOL_SCAN_SKIP_SUFFIXES = frozenset(
    _DOC_LIKE | _FIRMWARE_SOURCE | _LINKER_SCRIPT | _DEVICETREE
)


def _suffix(name: str) -> str:
    return Path(name).suffix.lower()


def matches_basename_rule(name: str) -> bool:
    """無副檔名的 build / 設定檔(Makefile、Makefile.local、Kconfig.debug)。"""
    base = str(name).replace("\\", "/").rpartition("/")[2]
    return base in BASENAME_EXACT or base.startswith(BASENAME_PREFIXES)


def is_indexable(name: str) -> bool:
    """index scope 成員資格:canonical suffix 或 basename 規則其一成立。"""
    return _suffix(name) in INDEX_SUFFIXES or matches_basename_rule(name)


def parser_route(name: str) -> str:
    """這個檔走哪個 symbol parser。``none`` = Level 1 只提供全文檢索。"""
    if matches_basename_rule(name):
        return "config"
    suffix = _suffix(name)
    if not suffix:
        return "none"
    for route, members in _ROUTES:
        if suffix in members:
            return route
    if suffix in _ROUTE_BY_SUFFIX:
        return _ROUTE_BY_SUFFIX[suffix]
    if suffix in _CONFIG_LIKE:
        return "config"
    if suffix in _DOC_LIKE:
        return "doc"
    return "none"


def enters_symbol_scan(name: str) -> bool:
    """這個檔要不要進 CodeRAG 的 symbol 掃描。

    回 False 不代表看不到:grep / list_dir / index scope 的可見範圍不受影響,
    Level 1 承諾的正是「全文搜得到」,不是「進 dense symbol retrieval」。
    """
    if matches_basename_rule(name):
        return False  # Makefile / Kconfig:P3B 新增,沒有 symbol parser
    return _suffix(name) not in SYMBOL_SCAN_SKIP_SUFFIXES


def has_symbol_parser(name: str) -> bool:
    return parser_route(name) not in ROUTES_WITHOUT_SYMBOL_PARSER


def grep_globs() -> tuple[str, ...]:
    """grep 的 include glob。

    兩件事非做不可:
      1. **case-sensitive**:``rg -g`` 與 fallback 的 ``fnmatch`` 在 Linux 上
         都區分大小寫,所以 ``.S`` 必須另外產一條 ``*.S``,否則韌體最常見的
         帶 preprocessor 組語檔搜不到。
      2. 由同一份 policy 產生,不再手寫第二份清單。
    """
    globs = [f"*{suffix}" for suffix in sorted(INDEX_SUFFIXES)]
    # 大小寫變體:副檔名在實務上會用大寫寫(.S / .C / .H 之類),
    # canonical 是小寫,glob 這一側要補回來。
    globs += [f"*{suffix.upper()}" for suffix in sorted(_FIRMWARE_SOURCE)]
    globs += sorted(BASENAME_EXACT)
    globs += [f"{prefix}*" for prefix in sorted(BASENAME_PREFIXES)]
    return tuple(dict.fromkeys(globs))


def grep_default_extensions() -> str:
    """``ToolExecutor.grep`` 的 include 參數格式(逗號分隔)。"""
    return ",".join(grep_globs())


def policy_fingerprint() -> dict:
    """進 index scope fingerprint 的 canonical 快照。

    basename 規則也要在裡面 —— 只放 suffix 的話,``Makefile`` 規則改了
    scope 快照不會失效,那是無聲的成員資格漂移。
    """
    return {
        "version": FILE_KIND_POLICY_VERSION,
        "index_suffixes": sorted(INDEX_SUFFIXES),
        "basename_exact": sorted(BASENAME_EXACT),
        "basename_prefixes": sorted(BASENAME_PREFIXES),
        "routes_without_symbol_parser": sorted(ROUTES_WITHOUT_SYMBOL_PARSER),
        "symbol_scan_skip_suffixes": sorted(SYMBOL_SCAN_SKIP_SUFFIXES),
    }
