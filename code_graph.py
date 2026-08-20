#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""code_graph — Code RAG 的跨檔案關係圖(§7 最小切片)。

範圍(只有這三種關係;references / inherits / impacted_tests 都不做):
  - definitions:file → symbol(全語言,nodes 來自 ast_parser.parse_file)
  - includes / imports:file → file
      C/C++:tree-sitter 抽 quote include；angle include 只有非絕對、帶路徑
      namespace(如 <fw/service.h>)且唯一 suffix 命中 repo header 才納入
      visibility。bare <stdint.h> 的單一 repo shim 不會冒充 system header；
      多個同名 repo candidate 只留下 ambiguity edge。絕對 angle path 不做
      repo suffix 配對。
      Python:stdlib ast 抽 import / from-import,模組路徑對 repo 內
      ``x.py`` / ``x/__init__.py`` resolve;resolve 不到的(stdlib / 第三方)
      **不記**——Python 端分不出「作者宣稱專案內」,記下來全是噪音。
  - calls:symbol → symbol(resolved)或 symbol → NULL + unresolved_target。
      C/C++ 只依 same-file definition、included header 的 static-inline
      definition、可見 prototype 對應的唯一 external definition、或 exact
      qualified name resolve；全 repo 唯一同名不再足夠。
      同一 call site 多候選 → 多列共用 ambiguity_group,confidence=syntactic。
      macro 間接呼叫抽不到(能力限制,後續輪的 config extractor 補)。

儲存(§7.2):``<root>/.code_rag_graph.sqlite3``,WAL。寫端由
``<root>/.code_rag_graph.lock`` 的 flock 序列化;讀不取鎖,走 WAL snapshot。
鎖序固定:先 flock 再開 SQLite 連線(防死鎖)。

建置(§7.5):
  - DB 已存在 → 同一正式 DB 內單一 transaction(BEGIN IMMEDIATE;全刪全插;
    COMMIT)。**絕不對可能已被開啟的 DB 做 os.replace**——SQLite 明載那是
    未定義行為(兩個 DB 共用同名 WAL 會互相損毀)。
  - 首次建立才允許 staging:取鎖後**再檢查一次**檔案是否已出現(另一
    process 可能剛建完)→ 出現就走 in-place;確認不存在才 tmp 建置 →
    wal_checkpoint(TRUNCATE) → close → os.replace。
  - staging 殘留(``.code_rag_graph.sqlite3.tmp*``)只在持鎖時清,且只清這個
    精確前綴——拿不到鎖就不清,否則會刪到另一 process 的 active staging。
  - WAL sidecar(``-wal`` / ``-shm``)管理權在 SQLite,程式與人都不得手動刪。

staleness(§7.5):每次 graph query 先比對 §5-3 的掃描快照(與 code_rag
共用同一 TTL 快照)與 files.content_hash,不符就同步跑增量 transaction
(stderr 一行)再回答。graph 不是「只建一次」。

增量範圍:「變更檔 + includes/imports 直接指向它的 1-hop 反向依賴檔 +
callable catalog delta 的同名 caller 檔」。第三類讓同名函式的增刪(unique↔
ambiguous、unresolved↔resolved)也會把呼叫端一併重 resolve,增量結果與
整體 rebuild 對 call edges 的判定一致。
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import config
import fs_safety
from ast_parser import (
    CPP_LINKED_OBJECT_KINDS,
    HAS_TREE_SITTER,
    PARSER_SEMANTICS_VERSION,
    Symbol,
    _try_load_tree_sitter_language,
    parse_file,
)

GRAPH_SCHEMA_VERSION = 3

# 確定解析的 edge confidence。heuristic(Python attribute call 靠 repo 內同名
# 唯一猜出來的)不算確定,不得進 path 呼叫鏈,也不該當成 confirmed evidence
# (消費端對應 code_context._CONFIRMED_EDGE_CONFIDENCE)。
CONFIRMED_EDGE_CONFIDENCE = ("exact", "resolved")

# 只有這些語言有 includes/calls 抽取器;其他語言仍有 defines(symbols)。
_LANG_BY_EXT = {
    ".py": "python", ".pyx": "python", ".pyi": "python",
    ".c": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
}

_CALLABLE_KINDS = ("function", "method")
_HEADER_EXTENSIONS = frozenset({".h", ".hpp", ".hh", ".hxx"})


class CodeGraphError(RuntimeError):
    """graph 檔缺 / 損壞 / schema 不符時,neighbors/path 類查詢的明確錯誤(§2)。"""


def _h_ext_lang() -> str:
    value = os.environ.get("AICODE_H_LANG", "c").strip().lower()
    return value if value in ("c", "cpp") else "c"


def _lang_for(rel_path: str) -> str:
    ext = Path(rel_path).suffix.lower()
    if ext == ".h":
        return _h_ext_lang()
    return _LANG_BY_EXT.get(ext, ext.lstrip(".") or "unknown")


def _normalize_cpp_qualified(name: str) -> str:
    """Canonicalize an explicit global qualifier without making it bare lookup."""
    return str(name)[2:] if str(name).startswith("::") else str(name)


def normalize_signature(signature: str | None) -> str:
    """參數型別序列的正規化(§7.2 stable ID tie-break)。

    去空白、去參數名、保留型別序列:``foo(int a, uint32_t *out)`` →
    ``int,uint32_t*``。同 arity 不同型別必須得到不同結果。
    """
    m = re.search(r"\(([^)]*)\)", signature or "")
    if not m:
        return ""
    types = []
    for raw in m.group(1).split(","):
        raw = raw.strip()
        if not raw or raw == "void":
            continue
        # 去掉尾端的參數名(identifier);純型別參數(如宣告 "int")會被整個
        # 吃掉,fallback 回原文,不然型別本身就丟了。
        stripped = re.sub(r"\b[A-Za-z_][A-Za-z0-9_]*$", "", raw).strip()
        types.append("".join((stripped or raw).split()))
    return ",".join(types)


def make_node_id(rel_path: str, lang: str, kind: str, qualified_name: str,
                 discriminator: str = "") -> str:
    """stable node ID(§7.2)。

    base = hash(relative_path, lang, kind, qualified_name)。僅當 base 衝突
    (同檔同 kind 同 qualified_name,即 overload)時,呼叫端才傳
    discriminator(normalized signature hash;仍撞再附 occurrence index)。
    非 overload 函式修改 signature 不得改變 ID —— 所以 discriminator 平常是空。
    """
    payload = "\x1f".join([rel_path, lang, kind, qualified_name, discriminator])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]


def _assign_node_ids(rel_path: str, lang: str, symbols: list[Symbol]) -> list[tuple[str, Symbol]]:
    """整檔一次指派 stable ID,含 overload 消歧(§7.2)。"""
    groups: dict[tuple[str, str], list[Symbol]] = {}
    for sym in symbols:
        key = (sym.type, sym.qualified_name or sym.name)
        groups.setdefault(key, []).append(sym)

    out: list[tuple[str, Symbol]] = []
    for (kind, qualified), members in groups.items():
        if len(members) == 1:
            out.append((make_node_id(rel_path, lang, kind, qualified), members[0]))
            continue
        # overload:先用 normalized signature 消歧;normalized 後仍同
        # (#ifdef 兩臂的同款定義)再附出現序,保證 PRIMARY KEY 不撞。
        sig_seen: dict[str, int] = {}
        for sym in sorted(members, key=lambda s: s.start_line):
            sig_key = normalize_signature(sym.signature)
            occurrence = sig_seen.get(sig_key, 0)
            sig_seen[sig_key] = occurrence + 1
            discriminator = f"sig:{sig_key}"
            if occurrence:
                discriminator += f"#\x1f{occurrence}"
            out.append((make_node_id(rel_path, lang, kind, qualified, discriminator), sym))
    order = {id(sym): i for i, (_, sym) in enumerate(out)}
    out.sort(key=lambda pair: (pair[1].start_line, order[id(pair[1])]))
    return out


# ============================================================
# 抽取器(§7.3)
# ============================================================
def _extract_python_relations(rel_path: str, content: str,
                              nodes: list[tuple[str, Symbol]]):
    """Python:stdlib ast 抽 imports 與 calls。回傳 (imports, calls)。

    imports: [(module_parts: tuple, level: int, lineno)]
    calls:   [(caller_node_id, callee_name, lineno, kind)]  kind="name"|"attr"
    """
    import ast as _ast

    try:
        tree = _ast.parse(content)
    except SyntaxError:
        return [], []

    imports: list[tuple[tuple[str, ...], int, int]] = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                imports.append((tuple(alias.name.split(".")), 0, node.lineno))
        elif isinstance(node, _ast.ImportFrom):
            base = tuple(node.module.split(".")) if node.module else ()
            # alias 要接到 module path 尾端(審核 #7):
            # `from pkg import util` 得先試 pkg/util.py,不是只看 pkg;
            # alias 是符號(非子模組)時 _resolve_import 的逐段縮短會退回
            # pkg.py / pkg/__init__.py。`import *` 沒有 alias 可接,原樣。
            for alias in node.names:
                if alias.name == "*":
                    imports.append((base, node.level, node.lineno))
                else:
                    imports.append((base + tuple(alias.name.split(".")),
                                    node.level, node.lineno))

    # caller 定位:call 行號落在哪個 function/method 的範圍內(取最內層)
    callable_nodes = [
        (nid, sym) for nid, sym in nodes if sym.type in _CALLABLE_KINDS
    ]

    def _enclosing(lineno: int) -> str | None:
        best = None
        best_span = None
        for nid, sym in callable_nodes:
            if sym.start_line <= lineno <= sym.end_line:
                span = sym.end_line - sym.start_line
                if best_span is None or span < best_span:
                    best, best_span = nid, span
        return best

    calls: list[tuple[str, str, int, str]] = []
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        func = node.func
        if isinstance(func, _ast.Name):
            name, kind = func.id, "name"
        elif isinstance(func, _ast.Attribute):
            name, kind = func.attr, "attr"
        else:
            continue
        caller = _enclosing(node.lineno)
        if caller is None:
            continue  # module-level call:本輪不建 file→symbol 的 call 邊
        calls.append((caller, name, node.lineno, kind))
    return imports, calls


def _ts_condition(node) -> str | None:
    """回傳 tree-sitter node 所在的 preprocessor branch 描述。"""
    parts = []
    current = node.parent
    while current is not None:
        if current.type in {"preproc_if", "preproc_ifdef", "preproc_elif", "preproc_else"}:
            first_line = current.text.decode("utf-8", errors="replace").splitlines()[0]
            normalized = " ".join(first_line.split())[:300]
            if normalized:
                parts.append(normalized)
        current = current.parent
    return " > ".join(reversed(parts)) or None


def _ts_declarator_name(node) -> str | None:
    """解開 function/pointer/parenthesized declarator 取得 identifier。"""
    if node is None:
        return None
    if node.type in {
        "identifier",
        "field_identifier",
        "qualified_identifier",
        "scoped_identifier",
    }:
        return node.text.decode("utf-8", errors="replace")
    inner = node.child_by_field_name("declarator")
    if inner is not None:
        return _ts_declarator_name(inner)
    for child in node.children:
        name = _ts_declarator_name(child)
        if name:
            return name
    return None


def _function_declarations(node) -> list:
    """找 declaration 的 top-level function_declarator，排除參數內 callback。"""
    found = []

    def walk(current):
        if current.type == "function_declarator":
            found.append(current)
            return
        if current.type in {"parameter_list", "parameter_declaration"}:
            return
        for child in current.children:
            walk(child)

    walk(node)
    return found


def _ts_scope_prefix(node) -> str:
    """Return the enclosing named namespace/class scope for a declaration."""
    parts: list[str] = []
    current = node.parent
    while current is not None:
        if current.type in {
            "namespace_definition", "class_specifier", "struct_specifier",
        }:
            name_node = current.child_by_field_name("name")
            if name_node is not None:
                name = name_node.text.decode("utf-8", errors="replace").strip()
                if name:
                    parts.append(name)
        current = current.parent
    return "::".join(reversed(parts))


def _strip_c_comments(content: str) -> str:
    """Remove C comments while preserving strings, characters, and newlines."""
    out: list[str] = []
    index = 0
    state = "code"
    quote = ""
    while index < len(content):
        char = content[index]
        nxt = content[index + 1] if index + 1 < len(content) else ""
        if state == "block_comment":
            if char == "*" and nxt == "/":
                out.extend((" ", " "))
                index += 2
                state = "code"
                continue
            out.append("\n" if char == "\n" else " ")
            index += 1
            continue
        if state == "line_comment":
            if char == "\\" and nxt == "\n":
                out.extend((" ", "\n"))
                index += 2
                continue
            if char == "\n":
                out.append(char)
                state = "code"
            else:
                out.append(" ")
            index += 1
            continue
        if state == "quoted":
            out.append(char)
            if char == "\\" and index + 1 < len(content):
                out.append(content[index + 1])
                index += 2
                continue
            if char == quote:
                state = "code"
            index += 1
            continue
        if char == "/" and nxt == "*":
            out.extend((" ", " "))
            index += 2
            state = "block_comment"
            continue
        if char == "/" and nxt == "/":
            out.extend((" ", " "))
            index += 2
            state = "line_comment"
            continue
        if char in {'"', "'"}:
            quote = char
            state = "quoted"
        out.append(char)
        index += 1
    return "".join(out)


def _guard_macro_matches_path(rel_path: str, macro: str) -> bool:
    """Require a filename-correlated macro before calling a block a guard.

    A top-level ``#ifndef HAVE_FEATURE`` plus ``#define`` is syntactically
    indistinguishable from a weak-default feature block. Filename correlation
    intentionally trades some recall for avoiding a false visibility proof.
    """
    stem = re.sub(r"[^A-Za-z0-9]+", "_", Path(rel_path).stem).strip("_").upper()
    normalized = macro.strip("_").upper()
    if not stem or f"_{stem}_" not in f"_{normalized}_":
        return False
    return normalized.endswith(("_H", "_HH", "_HPP", "_HXX", "_INCLUDED", "_GUARD"))


def _include_guard_condition(rel_path: str, content: str) -> str | None:
    """辨識傳統 ``#ifndef X`` / ``#define X`` header guard。

    condition metadata 仍保存原始 guard；resolver 只把這個已由檔案形狀證明的
    outer guard 視為 visibility-neutral。只接受 header 與 filename-correlated
    macro；guard 前後可有 pragma/宣告，`.c` weak-default 與不相關 feature
    block 都不猜。
    """
    if Path(rel_path).suffix.lower() not in _HEADER_EXTENSIONS:
        return None

    # Do not use a regex comment stripper here: `"/*"` is ordinary source and
    # may otherwise consume the real guard through a later comment terminator.
    lines = [line.strip() for line in _strip_c_comments(content).splitlines()]
    nesting = 0
    for start, line in enumerate(lines):
        if re.match(r"#\s*(?:if|ifdef|ifndef)\b", line):
            if nesting == 0:
                match = re.fullmatch(
                    r"#\s*ifndef\s+([A-Za-z_][A-Za-z0-9_]*)", line
                )
                if match is not None:
                    macro = match.group(1)
                    if _guard_macro_matches_path(rel_path, macro):
                        next_index = start + 1
                        while next_index < len(lines) and not lines[next_index]:
                            next_index += 1
                        if next_index < len(lines) and re.fullmatch(
                            rf"#\s*define\s+{re.escape(macro)}",
                            lines[next_index],
                        ):
                            depth = 0
                            for candidate in lines[start:]:
                                if re.match(r"#\s*(?:if|ifdef|ifndef)\b", candidate):
                                    depth += 1
                                elif re.match(r"#\s*endif\b", candidate):
                                    depth -= 1
                                    if depth == 0:
                                        return " ".join(line.split())
                                    if depth < 0:
                                        break
            nesting += 1
        elif re.match(r"#\s*endif\b", line):
            nesting = max(0, nesting - 1)
    return None


def _extract_c_relations(rel_path: str, content: str, lang: str,
                         nodes: list[tuple[str, Symbol]]):
    """C/C++:tree-sitter 抽 includes、prototypes 與 call_expression。

    回傳 (includes, calls, declarations, include_guard):
      includes: [(raw_target, lineno, is_angle, condition)]
      calls:    [(caller_node_id, callee_name, lineno, condition)]
      declarations: [(name, qualified_name, lineno, linkage, condition)]
    """
    ts_lang = _try_load_tree_sitter_language(lang) if HAS_TREE_SITTER else None
    if ts_lang is None:
        return [], [], [], _include_guard_condition(rel_path, content)
    from tree_sitter import Parser

    try:
        tree = Parser(ts_lang).parse(bytes(content, "utf-8"))
    except Exception:
        return [], [], [], _include_guard_condition(rel_path, content)

    includes: list[tuple[str, int, bool, str | None]] = []
    calls: list[tuple[str, str, int, str | None]] = []
    declarations: list[tuple[str, str, int, str, str | None]] = []

    callable_nodes = [
        (nid, sym) for nid, sym in nodes if sym.type in _CALLABLE_KINDS
    ]

    def _enclosing(lineno: int) -> str | None:
        best = None
        best_span = None
        for nid, sym in callable_nodes:
            if sym.start_line <= lineno <= sym.end_line:
                span = sym.end_line - sym.start_line
                if best_span is None or span < best_span:
                    best, best_span = nid, span
        return best

    def walk(node):
        if node.type == "preproc_include":
            path_node = node.child_by_field_name("path")
            if path_node is not None and path_node.type in {"string_literal", "system_lib_string"}:
                raw_text = path_node.text.decode("utf-8", errors="replace")
                is_angle = path_node.type == "system_lib_string"
                raw = raw_text.strip("<>") if is_angle else raw_text.strip('"')
                # condition 一定要留:`#ifdef FEATURE` 內的 include 只在該
                # branch 成立時形成可見性,當成無條件會憑空造出 resolved 邊。
                includes.append(
                    (raw, node.start_point[0] + 1, is_angle, _ts_condition(node))
                )
        elif node.type == "declaration":
            linkage = "external"
            current = node.parent
            inside_class = False
            while current is not None:
                if current.type in {"class_specifier", "struct_specifier"}:
                    inside_class = True
                if (current.type == "namespace_definition"
                        and current.child_by_field_name("name") is None):
                    linkage = "internal"
                current = current.parent
            for child in node.children:
                if child.type == "storage_class_specifier":
                    storage = child.text.decode("utf-8", errors="replace").strip()
                    if storage == "static" and not inside_class:
                        linkage = "internal"
            for declarator in _function_declarations(node):
                inner = declarator.child_by_field_name("declarator")
                # `int (*fp)(void)` 是 function-pointer variable，不是 prototype。
                inner_text = inner.text.decode("utf-8", errors="replace") if inner else ""
                if "(*" in "".join(inner_text.split()):
                    continue
                name = _ts_declarator_name(declarator)
                if name:
                    scope = _ts_scope_prefix(node)
                    normalized_name = _normalize_cpp_qualified(name)
                    if name.startswith("::") or not scope \
                            or normalized_name == scope \
                            or normalized_name.startswith(f"{scope}::"):
                        qualified = normalized_name
                    else:
                        qualified = f"{scope}::{normalized_name}"
                    declarations.append(
                        (normalized_name.rsplit("::", 1)[-1], qualified,
                         node.start_point[0] + 1,
                         linkage, _ts_condition(node))
                    )
        elif node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type in ("identifier", "qualified_identifier",
                                              "field_identifier"):
                name = fn.text.decode("utf-8", errors="replace")
                lineno = node.start_point[0] + 1
                caller = _enclosing(lineno)
                if caller is not None:
                    calls.append((caller, name, lineno, _ts_condition(node)))
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return includes, calls, declarations, _include_guard_condition(rel_path, content)


# ============================================================
# CodeGraph
# ============================================================
class CodeGraph:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.db_file = self.root / config.CODE_RAG_GRAPH_FILE
        self.lock_file = self.root / config.CODE_RAG_GRAPH_LOCK_FILE
        # 掃描復用 code_rag 的 scope + TTL 快照(§5-3 共用;同 key 同快照,
        # invalidate_scan_cache 同步失效兩邊)。
        import code_rag as _code_rag

        self._scanner = _code_rag.CodeRAG(str(self.root))

    def build_command(self) -> str:
        """可直接複製執行的建圖命令(GPT 審核三輪 #3)。

        使用者的 cwd 通常是被分析的 firmware repo,不是 CodeTrail repo,
        而且 `python` 未必是 MCP 實際用的那顆(§0.3)——所以命令必須是:
        當前 interpreter 的絕對路徑 + 本檔絕對路徑 + 實際 sandbox root,
        全部 shell quote。
        """
        import shlex

        return (
            f"{shlex.quote(sys.executable)} "
            f"{shlex.quote(str(Path(__file__).resolve()))} "
            f"--root {shlex.quote(str(self.root))}"
        )

    # -------- 掃描 --------
    def _scan_files(self) -> dict:
        return self._scanner._scan_code_files()

    # -------- 連線 / 安全 --------
    def _assert_db_path_safe(self) -> None:
        """sqlite3.connect 不暴露 O_NOFOLLOW(§7.6):connect 前先以
        O_NOFOLLOW 預建立/驗證路徑是 regular file,再交給 SQLite。"""
        fs_safety.ensure_parent_within_root(self.db_file, self.root)
        if self.db_file.exists() or self.db_file.is_symlink():
            fd = fs_safety.open_regular_file_nofollow(self.db_file)
            os.close(fd)

    def _connect(self, path: Path | None = None) -> sqlite3.Connection:
        """開連線。正式 DB 每次 connect 前都過 symlink 防線(審核 #2):
        sqlite3.connect 會 follow symlink,graph 路徑被換成指向別的專案
        graph 的 symlink 時,查詢/增量會跨 root 讀寫。staging tmp(顯式傳
        path)由呼叫端以 O_NOFOLLOW 預建立,不重驗。"""
        if path is None:
            self._assert_db_path_safe()
        conn = sqlite3.connect(str(path or self.db_file))
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # -------- schema --------
    _SCHEMA_STATEMENTS = (
        """
        CREATE TABLE IF NOT EXISTS files(
            path TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            lang TEXT NOT NULL,
            backend TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS nodes(
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            backend TEXT NOT NULL,
            confidence TEXT NOT NULL,
            linkage TEXT NOT NULL,
            condition TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS declarations(
            path TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            line INTEGER NOT NULL,
            linkage TEXT NOT NULL,
            condition TEXT,
            backend TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS edges(
            src_kind TEXT NOT NULL,
            src_id   TEXT NOT NULL,
            dst_kind TEXT,
            dst_id   TEXT,
            unresolved_target TEXT,
            ambiguity_group TEXT,
            type TEXT NOT NULL,
            evidence_path TEXT NOT NULL,
            evidence_line INTEGER NOT NULL,
            backend TEXT NOT NULL,
            confidence TEXT NOT NULL,
            resolution_basis TEXT NOT NULL,
            condition TEXT,
            CHECK ((dst_id IS NULL) = (unresolved_target IS NOT NULL)
                   OR ambiguity_group IS NOT NULL)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name)",
        "CREATE INDEX IF NOT EXISTS idx_nodes_path ON nodes(path)",
        "CREATE INDEX IF NOT EXISTS idx_declarations_name ON declarations(name)",
        "CREATE INDEX IF NOT EXISTS idx_declarations_path ON declarations(path)",
        "CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id, type)",
        "CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id, type)",
        """
        CREATE TABLE IF NOT EXISTS index_metadata(
            schema_version INTEGER NOT NULL,
            scope_fingerprint TEXT NOT NULL,
            parser_versions TEXT NOT NULL,
            created TEXT NOT NULL,
            updated TEXT NOT NULL
        )
        """,
    )

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        # Execute statements individually: executescript() implicitly commits and
        # would make the v1→v2 drop/create migration non-atomic.
        for statement in CodeGraph._SCHEMA_STATEMENTS:
            conn.execute(statement)

    @staticmethod
    def _schema_version(conn: sqlite3.Connection) -> int | None:
        try:
            row = conn.execute(
                "SELECT schema_version FROM index_metadata LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return None
        return int(row[0]) if row is not None else None

    @staticmethod
    def _drop_schema(conn: sqlite3.Connection) -> None:
        for table in ("edges", "declarations", "nodes", "files", "index_metadata"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")

    def _parser_versions(self) -> str:
        """抽取能力指紋:進 index_metadata,ensure_fresh 比對不符 → full rebuild。

        涵蓋 tree-sitter core 與 c/cpp grammar 三個 distribution 的**實際版本**
        (importlib.metadata;grammar 是獨立釘版套件,「可載入」布林分不出
        版本升級,審核二輪 #5)、grammar 可載入性(裝了但 ABI 不相容也要能
        區分)、`.h` 語言模式:任一變動,舊 graph 都不得沿用。
        """
        import importlib.metadata as _im

        def _dist(name: str) -> str:
            try:
                return _im.version(name)
            except _im.PackageNotFoundError:
                return "absent"

        c_ok = bool(HAS_TREE_SITTER and _try_load_tree_sitter_language("c"))
        cpp_ok = bool(HAS_TREE_SITTER and _try_load_tree_sitter_language("cpp"))
        return (
            f"python-ast:{sys.version_info.major}.{sys.version_info.minor};"
            f"tree-sitter:{_dist('tree-sitter')};"
            f"c:{_dist('tree-sitter-c')}:{int(c_ok)};"
            f"cpp:{_dist('tree-sitter-cpp')}:{int(cpp_ok)};"
            f"h:{_h_ext_lang()};"
            f"parser-semantics:{PARSER_SEMANTICS_VERSION};"
            "resolver:conservative-3"
        )

    # -------- 抽取單檔 --------
    def _extract_file(self, rel_path: str, filepath: Path, file_hash: str):
        """回傳 (file_row, node_rows, raw_relations)。

        raw_relations 的 resolve(跨檔)在 build/增量彙整時做。
        """
        try:
            content = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        try:
            symbols = parse_file(filepath, content)
        except Exception:
            symbols = []

        lang = _lang_for(rel_path)
        backend = symbols[0].backend if symbols else "none"
        id_pairs = _assign_node_ids(rel_path, lang, symbols)

        def graph_linkage(sym: Symbol) -> str:
            # macro / typedef / enum / enum_constant 在 C/C++ 語意上根本沒有
            # linkage,維持 not_applicable;global 物件有,而且 parser 已經算好了
            # —— 舊版對非 callable 一律回 not_applicable,等於把它丟掉(§3 洞 2)。
            if lang not in ("c", "cpp"):
                return "not_applicable"
            if sym.type in CPP_LINKED_OBJECT_KINDS:
                if sym.backend != "tree-sitter":
                    return "unknown"
                return sym.linkage or "unknown"
            if sym.type not in _CALLABLE_KINDS:
                return "not_applicable"
            # Regex/ctags fallback does not prove storage duration.  Treating
            # absent metadata as external can create cross-file false resolves.
            if sym.backend != "tree-sitter":
                return "unknown"
            linkage = sym.linkage or "external"
            if linkage == "internal" and Path(rel_path).suffix.lower() in _HEADER_EXTENSIONS:
                signature = sym.signature or sym.context or ""
                if re.search(r"\b(?:inline|__inline|__inline__)\b", signature):
                    return "header_inline"
            return linkage

        node_rows = [
            (
                nid, rel_path, sym.type, sym.name,
                sym.qualified_name or sym.name,
                sym.start_line, sym.end_line,
                sym.backend or "unknown", "exact",
                graph_linkage(sym),
                sym.condition,
            )
            for nid, sym in id_pairs
        ]

        imports: list = []
        includes: list = []
        calls: list = []
        declarations: list = []
        include_guard: str | None = None
        if lang == "python":
            imports, calls_py = _extract_python_relations(rel_path, content, id_pairs)
            calls = [
                (caller, name, lineno, kind, None)
                for caller, name, lineno, kind in calls_py
            ]
        elif lang in ("c", "cpp"):
            includes, calls_c, declarations, include_guard = _extract_c_relations(
                rel_path, content, lang, id_pairs
            )
            calls = [
                (caller, name, lineno, "name", condition)
                for caller, name, lineno, condition in calls_c
            ]

        file_row = (rel_path, file_hash, lang, backend)
        return file_row, node_rows, {
            "imports": imports,
            "includes": includes,
            "calls": calls,
            "declarations": declarations,
            "include_guard": include_guard,
        }

    # -------- resolve --------
    @staticmethod
    def _resolve_import(parts: tuple, level: int, src_rel: str, all_files: set[str]) -> str | None:
        """Python 模組 → repo 檔案。resolve 不到回 None(不記邊)。"""
        if level:  # 相對 import:從 src 的 package 往上走 level-1 層
            base = Path(src_rel).parent
            for _ in range(level - 1):
                base = base.parent
            candidate_parts = list(base.parts) + list(parts)
        else:
            candidate_parts = list(parts)
        if not candidate_parts:
            return None
        # 逐漸縮短:from a.b import name 中 name 可能是符號不是模組
        for cut in range(len(candidate_parts), 0, -1):
            head = candidate_parts[:cut]
            for candidate in (
                "/".join(head) + ".py",
                "/".join(head + ["__init__.py"]),
            ):
                if candidate in all_files:
                    return candidate
        return None

    @staticmethod
    def _resolve_include(raw: str, all_files: set[str]) -> list[str]:
        """引號 include → repo 檔案候選(basename 比對;可能 0/1/多個)。"""
        target = raw.replace("\\", "/")
        # 先試完整相對路徑,再退 basename
        if target in all_files:
            return [target]
        base = target.rpartition("/")[2]
        return sorted(f for f in all_files if f.rpartition("/")[2] == base)

    # -------- build --------
    def build(self, verbose: bool = False) -> None:
        """整體建置(§7.5)。首建走 staging,已存在走 in-place transaction。"""
        files = self._scan_files()
        lock_fd = fs_safety.acquire_file_lock(self.lock_file, self.root)
        try:
            # staging 殘留清理:只在持鎖時、只清自家精確前綴(§7.5)
            for stale in self.root.glob(f"{self.db_file.name}.tmp*"):
                stale.unlink(missing_ok=True)

            self._assert_db_path_safe()
            if self.db_file.exists():
                self._rebuild_in_place(files, verbose=verbose)
            else:
                self._staging_create(files, verbose=verbose)
        finally:
            fs_safety.release_file_lock(lock_fd)

    def _collect_all(self, files: dict):
        file_rows, node_rows, per_file_relations = [], [], {}
        for rel_path in sorted(files):
            extracted = self._extract_file(rel_path, files[rel_path]["filepath"],
                                           files[rel_path]["hash"])
            if extracted is None:
                continue
            file_row, nodes, relations = extracted
            file_rows.append(file_row)
            node_rows.extend(nodes)
            per_file_relations[rel_path] = relations
        return file_rows, node_rows, per_file_relations

    def _edge_rows(self, file_rows, node_rows, per_file_relations,
                   extra_callables: list[tuple] | None = None):
        """把 raw relations resolve 成 edges rows(跨檔一致視角)。

        extra_callables:增量更新時,DB 內「未受影響檔案」的既有 callable
        nodes [(id, name, qualified_name)]。少了它,重抽 caller 檔時指向
        未變檔案的 call 全部誤判 unresolved(GPT 審核 #1)。
        """
        all_files = {row[0] for row in file_rows}
        node_by_id = {row[0]: row for row in node_rows}

        # callable catalog。extra_callables 是 Python-only 增量時未重抽的 DB nodes。
        callable_by_name: dict[str, list[str]] = {}
        callable_by_qualified: dict[str, list[str]] = {}

        def add_callable(row: tuple) -> None:
            nid, _path, kind, name, qualified = row[:5]
            if kind not in _CALLABLE_KINDS:
                return
            keys = {name, name.rsplit("::", 1)[-1]}
            for key in keys:
                callable_by_name.setdefault(key, []).append(nid)
            callable_by_qualified.setdefault(
                _normalize_cpp_qualified(qualified), []
            ).append(nid)

        for row in node_rows:
            add_callable(row)
        for raw in extra_callables or []:
            if len(raw) >= 7:
                nid, path, name, qualified, linkage, condition, kind = raw[:7]
            else:  # 舊型別只會出現在防禦性/internal call site
                nid, name, qualified = raw[:3]
                path, linkage, condition, kind = "", "not_applicable", None, "function"
            row = (nid, path, kind, name, qualified, 0, 0, "unknown", "exact",
                   linkage, condition)
            node_by_id[nid] = row
            add_callable(row)

        edges = []
        # path -> {included_path: [condition, ...]}(None = 無條件 include)。
        # 同一個 header 可能在多個 branch 各 include 一次(`#ifdef` 與 `#else`
        # 各一),只留第一個條件會讓另一邊的呼叫被誤判成互斥而消失,所以全存,
        # 用 OR 語意合併。
        unique_includes: dict[str, dict[str, list]] = {
            path: {} for path in all_files
        }

        def ambiguity_id(kind: str, rel_path: str, lineno: int, target: str,
                         candidates: list[str]) -> str:
            """穩定的 ambiguity group，讓 incremental/full 結果可逐列比較。"""
            payload = "\x1f".join(
                [kind, rel_path, str(lineno), target, *sorted(set(candidates))]
            )
            return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

        # definitions 與 include/import edges 先建立；call visibility 要吃 include closure。
        for rel_path, relations in per_file_relations.items():
            for row in node_rows:
                if row[1] == rel_path:
                    edges.append((
                        "file", rel_path, "symbol", row[0], None, None,
                        "defines", rel_path, row[5], row[7], "exact",
                        "definition", row[10],
                    ))
            lang = _lang_for(rel_path)
            edge_backend = "python-ast" if lang == "python" else "tree-sitter"
            for raw, lineno, is_angle, include_condition in relations["includes"]:
                if is_angle:
                    # A bare <stdint.h> does not identify the repo's
                    # include-search path.  A unique vendored shim with that
                    # basename must not impersonate the compiler/system header.
                    # Namespaced <fw/service.h> is accepted only by exact path
                    # suffix, never by basename alone.
                    target = raw.replace("\\", "/")
                    parts = target.split("/")
                    absolute = target.startswith("/") or bool(
                        re.match(r"^[A-Za-z]:/", target)
                    )
                    if absolute or any(part in {"", ".", ".."} for part in parts):
                        candidates = []
                    elif "/" in target:
                        candidates = sorted(
                            path for path in all_files
                            if path == target or path.endswith(f"/{target}")
                        )
                    else:
                        # One basename hit is indistinguishable from a vendored
                        # shim impersonating a system header. Multiple hits are
                        # useful only as explicit ambiguity; never visibility.
                        basename_hits = sorted(
                            path for path in all_files
                            if path.rpartition("/")[2] == target
                        )
                        candidates = basename_hits if len(basename_hits) > 1 else []
                else:
                    candidates = self._resolve_include(raw, all_files)
                if len(candidates) == 1:
                    known = unique_includes.setdefault(rel_path, {})
                    conditions = known.setdefault(candidates[0], [])
                    if include_condition not in conditions:
                        conditions.append(include_condition)
                    edges.append((
                        "file", rel_path, "file", candidates[0], None, None,
                        "includes", rel_path, lineno, edge_backend, "resolved",
                        "unique_repo_angle_include" if is_angle else "unique_quote_include",
                        include_condition,
                    ))
                elif not candidates:
                    if not is_angle:  # angle zero-match = system/external，不製造噪音邊
                        edges.append((
                            "file", rel_path, None, None, raw, None,
                            "includes", rel_path, lineno, edge_backend, "syntactic",
                            "unresolved_quote_include", include_condition,
                        ))
                else:
                    group = ambiguity_id("include", rel_path, lineno, raw, candidates)
                    for candidate in candidates:
                        edges.append((
                            "file", rel_path, "file", candidate, raw, group,
                            "includes", rel_path, lineno, edge_backend, "syntactic",
                            "ambiguous_include", include_condition,
                        ))

            for parts, level, lineno in relations["imports"]:
                dst = self._resolve_import(parts, level, rel_path, all_files)
                if dst is not None:
                    edges.append((
                        "file", rel_path, "file", dst, None, None,
                        "imports", rel_path, lineno, "python-ast", "resolved",
                        "module_import", None,
                    ))

        def include_closure(start: str, call_condition: str | None) -> tuple:
            """依 call 的 preprocessor condition 走 include closure。

            回傳 (active, possible):
              active   路徑上每個 #include 都與 call condition 相容 → 可見。
              possible 至少有一段 include 的條件無法證明相容 → 只能當
                       conditional 候選(ambiguous),不得算成可見。
            可證明互斥的 branch 直接剪掉。
            """
            state: dict[str, str] = {}
            frontier: list[tuple[str, str]] = [(start, "active")]
            while frontier:
                current, current_state = frontier.pop(0)
                for dst, conditions in sorted(unique_includes.get(current, {}).items()):
                    # OR:任一 include 點相容就算相容;全部互斥才剪掉。
                    relations = {
                        condition_relation(
                            effective_condition(current, condition), call_condition
                        )
                        for condition in conditions
                    }
                    if "active" in relations:
                        relation = "active"
                    elif "possible" in relations:
                        relation = "possible"
                    else:
                        continue
                    next_state = "active" if (
                        current_state == "active" and relation == "active"
                    ) else "possible"
                    if state.get(dst) == next_state or state.get(dst) == "active":
                        continue
                    state[dst] = next_state
                    frontier.append((dst, next_state))
            active = {path for path, value in state.items() if value == "active"}
            possible = {path for path, value in state.items() if value == "possible"}
            return active, possible - active

        declarations_by_file = {
            path: relations.get("declarations", [])
            for path, relations in per_file_relations.items()
        }

        def effective_condition(path: str, condition: str | None) -> str | None:
            """移除已證明是 header guard 的 outer condition，其餘不猜。"""
            if condition is None:
                return None
            guard = per_file_relations.get(path, {}).get("include_guard")
            if not guard:
                return condition
            if condition == guard:
                return None
            prefix = f"{guard} > "
            return condition[len(prefix):] if condition.startswith(prefix) else condition

        def scope_of(qualified: str) -> str:
            return _normalize_cpp_qualified(qualified).rpartition("::")[0]

        def definition_candidates(name: str, caller_id: str, lang: str) -> list[str]:
            if lang not in ("c", "cpp"):
                bare = name.rsplit("::", 1)[-1]
                return list(dict.fromkeys(
                    callable_by_qualified.get(name, []) + callable_by_name.get(bare, [])
                ))
            if "::" in name:
                # Qualified syntax is an exact assertion.  Falling back to the
                # bare catalog lets `ns::f()` bind to an unrelated global f.
                exact = _normalize_cpp_qualified(name)
                return list(dict.fromkeys(callable_by_qualified.get(exact, [])))

            caller_scope = scope_of(str(node_by_id[caller_id][4]))
            out = []
            for target in callable_by_name.get(name, []):
                target_qualified = str(node_by_id[target][4])
                target_scope = scope_of(target_qualified)
                # A bare call can see a qualified method/namespace member only
                # from the same lexical scope.  We do not model using-declarations,
                # so every other case remains unresolved.
                if target_scope and target_scope != caller_scope:
                    continue
                out.append(target)
            return list(dict.fromkeys(out))

        def target_condition(target_id: str) -> str | None:
            target_path = node_by_id[target_id][1]
            return effective_condition(target_path, node_by_id[target_id][10])

        def conditions_mutually_exclusive(left: str, right: str) -> bool:
            """Prove only same-preprocessor-chain alternatives as exclusive."""
            left_parts = left.split(" > ")
            right_parts = right.split(" > ")
            common = min(len(left_parts), len(right_parts))
            for index in range(common):
                if left_parts[index] == right_parts[index]:
                    continue
                return (
                    left_parts[index].startswith(("#else", "#elif"))
                    and right_parts[index].startswith(("#else", "#elif"))
                )
            if len(left_parts) == len(right_parts):
                return False
            longer = left_parts if len(left_parts) > common else right_parts
            return longer[common].startswith(("#else", "#elif"))

        def condition_literals(condition: str) -> frozenset:
            """把 condition chain 轉成 literal 合取(每個 literal = (指示詞, 正負))。

            tree-sitter 的 chain 是「條件樹上的一條路徑」:`#else`/`#elif`
            節點是它所屬 `#if` 的子節點,所以 `A > #else` 代表 ¬A(不是 A 再
            加一個條件),`A > #ifdef B` 代表 A ∧ B。轉成 literal 集合後,
            跨檔案、不同巢狀順序的條件才能比較。
            """
            literals: list[tuple[str, bool]] = []
            for part in condition.split(" > "):
                if part.startswith(("#else", "#elif")):
                    if literals:  # 前一段分支取反
                        head, positive = literals[-1]
                        literals[-1] = (head, not positive)
                    if part.startswith("#elif"):
                        literals.append((part, True))
                else:
                    literals.append((part, True))
            return frozenset(literals)

        def condition_implies(outer: str, inner: str) -> bool:
            """inner ⇒ outer:inner 的 literal 集合涵蓋 outer 的全部 literal。

            `#ifdef MODE` 的 include 對 `#ifdef FEATURE > #ifdef MODE` 的呼叫
            是確定可見(MODE 成立),即使兩者的巢狀順序/檔案不同。互斥的分支
            不可能是子集(literal 正負相反),所以這條不會吃掉互斥判定。
            """
            outer_literals = condition_literals(outer)
            inner_literals = condition_literals(inner)
            return outer_literals != inner_literals \
                and outer_literals <= inner_literals

        def condition_relation(condition: str | None,
                               call_condition: str | None) -> str:
            if condition is None or condition == call_condition:
                return "active"
            if call_condition is not None:
                if conditions_mutually_exclusive(condition, call_condition):
                    return "exclusive"
                if condition_implies(condition, call_condition):
                    return "active"
            return "possible"

        def condition_groups(targets: list[str], call_condition: str | None):
            active: list[str] = []
            possible: list[str] = []
            for target in targets:
                relation = condition_relation(target_condition(target), call_condition)
                if relation == "active":
                    active.append(target)
                elif relation == "possible":
                    possible.append(target)
            return active, possible

        def merge_stage(targets: list[str], call_condition: str | None,
                        deferred: list[str], default_ambiguity: str):
            """Merge a precedence stage without hiding condition candidates."""
            active, possible = condition_groups(targets, call_condition)
            if not active:
                return None, list(dict.fromkeys(deferred + possible))
            combined = list(dict.fromkeys(deferred + active + possible))
            if len(combined) == 1:
                return ("resolved", active[0], None), []
            active_conditions = {target_condition(target) for target in active}
            basis = default_ambiguity
            if deferred or possible or len(active_conditions) > 1:
                basis = "ambiguous_condition"
            return ("ambiguous", combined, basis), []

        def visible_declaration_identity(
            path: str, decl: tuple, call_name: str,
            caller_id: str, call_condition: str | None,
        ) -> str | None:
            name, qualified, _line, linkage, condition = decl
            if linkage == "internal":
                return None
            normalized_call = _normalize_cpp_qualified(call_name)
            normalized_qualified = _normalize_cpp_qualified(qualified)
            if "::" in call_name:
                name_matches = normalized_qualified == normalized_call
            else:
                caller_scope = scope_of(str(node_by_id[caller_id][4]))
                expected = f"{caller_scope}::{call_name}" if caller_scope else call_name
                # Unqualified lookup from a class/namespace may still reach a
                # global declaration after checking the local scope.
                name_matches = normalized_qualified in {call_name, expected}
            if not name_matches:
                return None
            condition = effective_condition(path, condition)
            if condition_relation(condition, call_condition) != "active":
                return None
            return normalized_qualified

        def add_unresolved(caller_id: str, name: str, rel_path: str, lineno: int,
                           backend: str, condition: str | None) -> None:
            edges.append((
                "symbol", caller_id, None, None, name, None,
                "calls", rel_path, lineno, backend, "syntactic",
                "syntactic_only", condition,
            ))

        def add_ambiguous(caller_id: str, name: str, targets: list[str], rel_path: str,
                          lineno: int, backend: str, condition: str | None,
                          basis: str) -> None:
            group = ambiguity_id("call", rel_path, lineno, name, targets)
            for target in sorted(set(targets)):
                edges.append((
                    "symbol", caller_id, "symbol", target, name, group,
                    "calls", rel_path, lineno, backend, "syntactic", basis, condition,
                ))

        def add_resolved(caller_id: str, target: str, rel_path: str, lineno: int,
                         backend: str, condition: str | None, basis: str,
                         confidence: str = "exact") -> None:
            edges.append((
                "symbol", caller_id, "symbol", target, None, None,
                "calls", rel_path, lineno, backend, confidence, basis, condition,
            ))

        def emit_decision(decision: tuple, caller_id: str, name: str,
                          rel_path: str, lineno: int, backend: str,
                          condition: str | None, resolved_basis: str) -> None:
            if decision[0] == "resolved":
                add_resolved(
                    caller_id, decision[1], rel_path, lineno, backend,
                    condition, resolved_basis,
                )
            else:
                add_ambiguous(
                    caller_id, name, decision[1], rel_path, lineno, backend,
                    condition, decision[2],
                )

        def emit_deferred_or_unresolved(
            deferred: list[str], caller_id: str, name: str, rel_path: str,
            lineno: int, backend: str, condition: str | None,
        ) -> None:
            deferred = list(dict.fromkeys(deferred))
            if deferred:
                basis = "conditional_candidate" if len(deferred) == 1 \
                    else "ambiguous_condition"
                add_ambiguous(
                    caller_id, name, deferred, rel_path, lineno, backend,
                    condition, basis,
                )
            else:
                add_unresolved(
                    caller_id, name, rel_path, lineno, backend, condition
                )

        for rel_path, relations in per_file_relations.items():
            lang = _lang_for(rel_path)
            backend = "python-ast" if lang == "python" else "tree-sitter"
            for caller_id, name, lineno, kind, call_condition in relations["calls"]:
                if caller_id not in node_by_id:
                    continue
                effective_call_condition = effective_condition(rel_path, call_condition)
                targets = definition_candidates(name, caller_id, lang)
                if lang not in ("c", "cpp"):
                    if len(targets) == 1:
                        add_resolved(
                            caller_id, targets[0], rel_path, lineno, backend, None,
                            "global_unique", "resolved" if kind == "name" else "heuristic",
                        )
                    elif targets:
                        add_ambiguous(
                            caller_id, name, targets, rel_path, lineno, backend, None,
                            "ambiguous_global",
                        )
                    else:
                        add_unresolved(caller_id, name, rel_path, lineno, backend, None)
                    continue

                # Candidate stages are merged instead of short-circuiting on a
                # condition-incompatible target. A proven alternate branch is
                # discarded; an unknown condition remains an explicit candidate.
                deferred: list[str] = []

                # 1. 同 translation unit definition 最強；static 可且只可走這條。
                same_file_all = [
                    target for target in targets
                    if node_by_id[target][1] == rel_path
                ]
                same_file_ambiguity = "ambiguous_qualified" if "::" in name \
                    else "ambiguous_same_file"
                decision, deferred = merge_stage(
                    same_file_all, effective_call_condition, deferred,
                    same_file_ambiguity,
                )
                if decision is not None:
                    emit_decision(
                        decision, caller_id, name, rel_path, lineno, backend,
                        call_condition, "same_file",
                    )
                    continue

                # 2. Qualified C++ syntax may only bind an exact qualified
                # definition.  Do this before prototype handling so a global bare
                # declaration cannot consume `namespace::method()`.
                if lang == "cpp" and "::" in name:
                    qualified_all = [
                        target for target in targets
                        if node_by_id[target][1] != rel_path
                        and node_by_id[target][9] == "external"
                    ]
                    decision, deferred = merge_stage(
                        qualified_all, effective_call_condition, deferred,
                        "ambiguous_qualified",
                    )
                    if decision is not None:
                        emit_decision(
                            decision, caller_id, name, rel_path, lineno, backend,
                            call_condition, "qualified",
                        )
                    else:
                        emit_deferred_or_unresolved(
                            deferred, caller_id, name, rel_path, lineno,
                            backend, call_condition,
                        )
                    continue

                # 3. Definitions in an actually included static-inline header are
                # part of this translation unit.  They have internal linkage, but
                # unlike a static function in another .c file they are visible here.
                active_includes, conditional_includes = include_closure(
                    rel_path, effective_call_condition
                )
                active_includes = active_includes - {rel_path}
                conditional_files = conditional_includes - {rel_path}
                visible_files = {rel_path} | active_includes
                header_inline_all = [
                    target for target in targets
                    if node_by_id[target][1] in active_includes
                    and node_by_id[target][9] == "header_inline"
                ]
                # 只在條件式 include 底下才看得到的定義不是可見性,
                # 是 conditional 候選(合併進 deferred → ambiguous)。
                deferred = list(dict.fromkeys(deferred + [
                    target for target in targets
                    if node_by_id[target][1] in conditional_files
                    and node_by_id[target][9] == "header_inline"
                ]))
                decision, deferred = merge_stage(
                    header_inline_all, effective_call_condition, deferred,
                    "ambiguous_header_inline",
                )
                if decision is not None:
                    emit_decision(
                        decision, caller_id, name, rel_path, lineno, backend,
                        call_condition, "visible_header_inline",
                    )
                    continue

                # 4. caller 本檔或 direct/transitive repo header 的 visible prototype。
                visible_declarations = {
                    identity
                    for path in visible_files
                    for decl in declarations_by_file.get(path, [])
                    if (identity := visible_declaration_identity(
                        path, decl, name, caller_id, effective_call_condition
                    )) is not None
                }
                # 條件式 include 進來的 prototype 同理:不算可見宣告,
                # 只能讓對應 definition 成為 conditional 候選。
                conditional_declarations = {
                    identity
                    for path in conditional_files
                    for decl in declarations_by_file.get(path, [])
                    if (identity := visible_declaration_identity(
                        path, decl, name, caller_id, effective_call_condition
                    )) is not None
                } - visible_declarations
                external_all = [
                    target for target in targets
                    if node_by_id[target][1] != rel_path
                    and node_by_id[target][9] == "external"
                    and _normalize_cpp_qualified(node_by_id[target][4])
                    in visible_declarations
                ]
                deferred = list(dict.fromkeys(deferred + [
                    target for target in targets
                    if node_by_id[target][1] != rel_path
                    and node_by_id[target][9] == "external"
                    and _normalize_cpp_qualified(node_by_id[target][4])
                    in conditional_declarations
                ]))
                if visible_declarations:
                    decision, deferred = merge_stage(
                        external_all, effective_call_condition, deferred,
                        "ambiguous_visible_declaration",
                    )
                    if decision is not None:
                        emit_decision(
                            decision, caller_id, name, rel_path, lineno, backend,
                            call_condition, "visible_declaration",
                        )
                    else:
                        emit_deferred_or_unresolved(
                            deferred, caller_id, name, rel_path, lineno,
                            backend, call_condition,
                        )
                    continue

                # No matching declaration: multiple external syntactic targets
                # remain useful ambiguity, but a singleton is never resolved.
                syntactic_all = [
                    target for target in targets
                    if node_by_id[target][1] != rel_path
                    and node_by_id[target][9] == "external"
                ]
                active, possible = condition_groups(
                    syntactic_all, effective_call_condition
                )
                combined = list(dict.fromkeys(deferred + active + possible))
                if len(active) > 1:
                    active_conditions = {target_condition(target) for target in active}
                    basis = "ambiguous_syntactic_candidates"
                    if deferred or possible or len(active_conditions) > 1:
                        basis = "ambiguous_condition"
                    add_ambiguous(
                        caller_id, name, combined, rel_path, lineno, backend,
                        call_condition, basis,
                    )
                elif deferred or possible:
                    basis = "conditional_candidate" if len(combined) == 1 \
                        else "ambiguous_condition"
                    add_ambiguous(
                        caller_id, name, combined, rel_path, lineno, backend,
                        call_condition, basis,
                    )
                else:
                    add_unresolved(
                        caller_id, name, rel_path, lineno, backend, call_condition
                    )
        return edges

    _INSERT_FILE = "INSERT INTO files(path, content_hash, lang, backend) VALUES (?,?,?,?)"
    _INSERT_NODE = (
        "INSERT INTO nodes(id, path, kind, name, qualified_name, start_line, end_line,"
        " backend, confidence, linkage, condition) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
    )
    _INSERT_DECLARATION = (
        "INSERT INTO declarations(path, name, qualified_name, line, linkage, condition,"
        " backend) VALUES (?,?,?,?,?,?,?)"
    )
    _INSERT_EDGE = (
        "INSERT INTO edges(src_kind, src_id, dst_kind, dst_id, unresolved_target,"
        " ambiguity_group, type, evidence_path, evidence_line, backend, confidence,"
        " resolution_basis, condition) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )

    @staticmethod
    def _declaration_rows(per_file_relations: dict) -> list[tuple]:
        rows = []
        for path in sorted(per_file_relations):
            for name, qualified, line, linkage, condition in per_file_relations[path].get(
                "declarations", []
            ):
                rows.append(
                    (path, name, qualified, line, linkage, condition, "tree-sitter")
                )
        return rows

    def _staging_create(self, files: dict, *, verbose: bool) -> None:
        # 取鎖後二次檢查(§7.5):另一 process 可能剛建完
        if self.db_file.exists():
            self._rebuild_in_place(files, verbose=verbose)
            return
        tmp_path = self.root / f"{self.db_file.name}.tmp{os.getpid()}"
        fd = fs_safety.open_regular_file_nofollow(tmp_path)
        os.close(fd)
        conn = self._connect(tmp_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            self._create_schema(conn)
            file_rows, node_rows, relations = self._collect_all(files)
            edges = self._edge_rows(file_rows, node_rows, relations)
            declarations = self._declaration_rows(relations)
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            with conn:
                conn.executemany(self._INSERT_FILE, file_rows)
                conn.executemany(self._INSERT_NODE, node_rows)
                conn.executemany(self._INSERT_DECLARATION, declarations)
                conn.executemany(self._INSERT_EDGE, edges)
                conn.execute(
                    "INSERT INTO index_metadata VALUES (?,?,?,?,?)",
                    (GRAPH_SCHEMA_VERSION, self._scanner.scope.fingerprint,
                     self._parser_versions(), now, now),
                )
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except BaseException:
            conn.close()
            tmp_path.unlink(missing_ok=True)
            Path(f"{tmp_path}-wal").unlink(missing_ok=True)
            Path(f"{tmp_path}-shm").unlink(missing_ok=True)
            raise
        conn.close()
        os.replace(tmp_path, self.db_file)
        if verbose:
            print(f"[CODE_GRAPH] built: {len(node_rows)} nodes", file=sys.stderr)

    def _rebuild_in_place(self, files: dict, *, verbose: bool) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            previous_schema = self._schema_version(conn)
            file_rows, node_rows, relations = self._collect_all(files)
            edges = self._edge_rows(file_rows, node_rows, relations)
            declarations = self._declaration_rows(relations)
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute("BEGIN IMMEDIATE")
            try:
                if previous_schema != GRAPH_SCHEMA_VERSION:
                    # DDL is transactional in SQLite when issued statement by
                    # statement.  A failed upgrade rolls back to the readable v1
                    # graph instead of leaving a half-created v2 database.
                    self._drop_schema(conn)
                self._create_schema(conn)
                conn.execute("DELETE FROM edges")
                conn.execute("DELETE FROM declarations")
                conn.execute("DELETE FROM nodes")
                conn.execute("DELETE FROM files")
                conn.execute("DELETE FROM index_metadata")
                conn.executemany(self._INSERT_FILE, file_rows)
                conn.executemany(self._INSERT_NODE, node_rows)
                conn.executemany(self._INSERT_DECLARATION, declarations)
                conn.executemany(self._INSERT_EDGE, edges)
                conn.execute(
                    "INSERT INTO index_metadata VALUES (?,?,?,?,?)",
                    (GRAPH_SCHEMA_VERSION, self._scanner.scope.fingerprint,
                     self._parser_versions(), now, now),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()
        if verbose:
            action = "migrated and rebuilt" if previous_schema != GRAPH_SCHEMA_VERSION \
                else "rebuilt in place"
            print(f"[CODE_GRAPH] {action}: {len(node_rows)} nodes", file=sys.stderr)

    # -------- staleness / 增量(§7.5) --------
    def _db_state(self) -> str:
        """Return missing, corrupt, schema, or ready without mutating the DB."""
        if not self.db_file.exists():
            return "missing"
        try:
            conn = self._connect()
        except sqlite3.Error:
            return "corrupt"
        try:
            try:
                row = conn.execute(
                    "SELECT schema_version, scope_fingerprint"
                    " FROM index_metadata LIMIT 1"
                ).fetchone()
            except sqlite3.OperationalError:
                # A valid SQLite file with an old/partial schema can be rebuilt
                # transactionally by the documented explicit build command.
                return "schema"
        except sqlite3.DatabaseError:
            return "corrupt"
        finally:
            conn.close()
        if row is None or row[0] != GRAPH_SCHEMA_VERSION:
            return "schema"
        return "ready"

    def _db_ready(self) -> bool:
        return self._db_state() == "ready"

    def ensure_fresh(self) -> None:
        """graph query 前置(§2 失效矩陣:缺 / 損壞 / schema 不符 → 明確錯誤):

        - 檔不存在 → CodeGraphError,訊息載明**可直接執行**的建立命令
          (build_command():實際 interpreter + 本檔絕對路徑 + 實際 root,
          shell-quoted;semantic 不受影響)。graph **不做隱式 lazy build**:
          首次建置是顯式維運動作,查詢不得靜默付全量建置成本 ——
          這是施工單 §2 的契約,不是實作偏好。
        - 檔存在但損壞 / schema 不符 → CodeGraphError(不砍不蓋)。
        - scope fingerprint 或 parser 能力指紋(tree-sitter/grammar 版本/
          AICODE_H_LANG)變了 → full rebuild;
        - 檔案 hash 落後 → 同步增量後回答(§7.5;前提是 graph 已存在)。
        """
        db_state = self._db_state()
        if db_state != "ready":
            if db_state == "corrupt":
                raise CodeGraphError(
                    "code graph DB 損壞，不能原地重建。請先將 "
                    f"`{self.db_file}` 移出或刪除（不要單獨刪除 SQLite 的 "
                    "-wal/-shm sidecar），再執行 "
                    f"`{self.build_command()}` (mode='semantic' 不受影響)"
                )
            if db_state == "schema":
                raise CodeGraphError(
                    "code graph schema 不符;請執行 "
                    f"`{self.build_command()}` 原地升級/重建"
                    "(mode='semantic' 不受影響)"
                )
            raise CodeGraphError(
                "code graph 尚未建立(mode='semantic' 不受影響)。"
                f"建立方式:在終端跑 `{self.build_command()}`"
                "(建好之後查詢會自動偵測變更做增量,不必重跑)"
            )

        files = self._scan_files()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT scope_fingerprint, parser_versions FROM index_metadata LIMIT 1"
            ).fetchone()
            db_rows = conn.execute("SELECT path, content_hash, lang FROM files").fetchall()
            db_hashes = {path: content_hash for path, content_hash, _lang in db_rows}
            db_langs = {path: lang for path, _content_hash, lang in db_rows}
        finally:
            conn.close()

        if row and row[0] != self._scanner.scope.fingerprint:
            print("[CODE_GRAPH] scope fingerprint changed; full rebuild", file=sys.stderr)
            self.build(verbose=False)
            return
        if row and row[1] != self._parser_versions():
            # grammar 裝上 / parser 升級 / .h 語言模式改變:degraded graph
            # 不得永久沿用(審核 #6)
            print("[CODE_GRAPH] parser capabilities changed; full rebuild", file=sys.stderr)
            self.build(verbose=False)
            return

        current = {rel: info["hash"] for rel, info in files.items()}
        added = sorted(set(current) - set(db_hashes))
        deleted = sorted(set(db_hashes) - set(current))
        changed = sorted(
            rel for rel, h in current.items()
            if rel in db_hashes and db_hashes[rel] != h
        )

        if added:
            # file catalog 擴張 → full rebuild(GPT 審核三輪 #1):新檔案沒有
            # 任何既有 incoming edge,反向相依查詢抓不到「本來 resolve 到別處
            # /resolve 不到、現在該指向新檔」的 import/include(`from pkg
            # import util` 原落在 pkg/__init__.py、新增 pkg/util.py 該切換;
            # 新增同名 header 該把唯一 include 變歧義)。刪除與內容修改仍走
            # 增量:指向被刪檔的邊查得到(reverse deps),callable 增刪由
            # catalog delta 覆蓋。
            print(
                f"[CODE_GRAPH] file catalog changed (+{len(added)}); full rebuild",
                file=sys.stderr,
            )
            self.build(verbose=False)
            return

        if not changed and not deleted:
            return

        c_family = {"c", "cpp"}
        if any(
            _lang_for(rel) in c_family or db_langs.get(rel) in c_family
            for rel in changed + deleted
        ):
            # C/C++ resolution depends on linkage, declarations, include closure and
            # condition context. A source hash change is therefore also the complete
            # visibility fingerprint; conservatively rebuilding guarantees that the
            # result is identical to a fresh graph instead of guessing a partial cone.
            print(
                f"[CODE_GRAPH] C/C++ visibility changed "
                f"({len(changed)} changed, {len(deleted)} deleted); full rebuild",
                file=sys.stderr,
            )
            self.build(verbose=False)
            return
        print(
            f"[CODE_GRAPH] stale ({len(changed)} changed, {len(deleted)} deleted); "
            "incremental update", file=sys.stderr,
        )
        if self._incremental_update(files, changed, deleted):
            print(
                "[CODE_GRAPH] Python catalog change reached C/C++ callers; full rebuild",
                file=sys.stderr,
            )
            self.build(verbose=False)

    def _incremental_update(self, files: dict, changed: list[str],
                            deleted: list[str]) -> bool:
        """單一 transaction 涵蓋「變更檔 + 1-hop 反向依賴檔 + 同名 caller 檔」
        的 delete+insert。

        「同名 caller 檔」(審核二輪 #1):受影響檔的 callable 名稱對應
        node-id 集合改變時,呼叫這些名稱的檔案即使自身沒變,其 call edges 也可能
        改變(unique→ambiguous、ambiguous→unique、unresolved→resolved),
        必須一併重抽重 resolve,否則會留下錯誤的「確定呼叫」。
        """
        lock_fd = fs_safety.acquire_file_lock(self.lock_file, self.root)
        try:
            conn = self._connect()
            try:
                dirty = set(changed) | set(deleted)
                placeholders = ",".join("?" * len(dirty))
                reverse = {
                    row[0] for row in conn.execute(
                        f"SELECT DISTINCT src_id FROM edges WHERE type IN"
                        f" ('includes','imports') AND dst_kind='file'"
                        f" AND dst_id IN ({placeholders})",
                        sorted(dirty),
                    )
                }
                affected = set((dirty | reverse) & (set(files) | set(deleted)))

                def _extract_batch(rels: list[str]):
                    rows, nodes_acc, rels_acc = [], [], {}
                    for rel in rels:
                        extracted = self._extract_file(
                            rel, files[rel]["filepath"], files[rel]["hash"])
                        if extracted is None:
                            continue
                        row, nodes, rel_relations = extracted
                        rows.append(row)
                        nodes_acc.extend(nodes)
                        rels_acc[rel] = rel_relations
                    return rows, nodes_acc, rels_acc

                re_extract = sorted(rel for rel in affected if rel in files)
                file_rows, node_rows, relations = _extract_batch(re_extract)

                # callable catalog delta:只取「名稱 → node-id 集合」新舊不同
                # 的 key。body-only edit 的 stable ID 不變，因此不 fan-out；
                # rename/add/delete/overload identity 改變仍會重抽同名 caller。
                aff_ph = ",".join("?" * len(affected)) or "''"
                old_catalog: dict[str, set[str]] = {}
                for row in conn.execute(
                    f"SELECT id, name, qualified_name FROM nodes"
                    f" WHERE kind IN ('function','method') AND path IN ({aff_ph})",
                    sorted(affected),
                ):
                    node_id, name, qualified = row
                    for key in (name, qualified):
                        if key is not None:
                            old_catalog.setdefault(key, set()).add(node_id)
                new_catalog: dict[str, set[str]] = {}
                for nrow in node_rows:
                    if nrow[2] in _CALLABLE_KINDS:
                        for key in (nrow[3], nrow[4]):
                            new_catalog.setdefault(key, set()).add(nrow[0])
                delta_names = {
                    name for name in old_catalog.keys() | new_catalog.keys()
                    if old_catalog.get(name, set()) != new_catalog.get(name, set())
                }

                if delta_names:
                    name_ph = ",".join("?" * len(delta_names))
                    sorted_delta = sorted(delta_names)
                    caller_files = {
                        row[0] for row in conn.execute(
                            f"""
                            SELECT DISTINCT evidence_path FROM edges
                            WHERE type='calls' AND (
                                unresolved_target IN ({name_ph})
                                OR dst_id IN (SELECT id FROM nodes
                                              WHERE name IN ({name_ph})
                                                 OR qualified_name IN ({name_ph}))
                            )
                            """,
                            sorted_delta * 3,
                        )
                    }
                    extra_callers = sorted(
                        (caller_files & set(files)) - affected)
                    if extra_callers:
                        affected.update(extra_callers)
                        more_rows, more_nodes, more_relations = _extract_batch(extra_callers)
                        file_rows.extend(more_rows)
                        node_rows.extend(more_nodes)
                        relations.update(more_relations)

                # A Python-only catalog delta can pull an unchanged C caller into
                # the re-resolution set via a colliding function name.  Resolving
                # that caller from a partial relation set omits declarations and
                # transitive include closure, silently degrading a proven edge.
                # Return before mutating the DB; the caller rebuilds from the full
                # snapshot after this lock/connection are released.
                if any(_lang_for(rel) in {"c", "cpp"} for rel in affected if rel in files):
                    return True

                all_dirty = sorted(affected)
                ph = ",".join("?" * len(all_dirty))

                # 重抽檔的 call resolve 必須看得到「未受影響檔案」的既有
                # callable nodes,否則跨檔呼叫全變 unresolved(審核 #1)。
                extra_callables = [
                    tuple(row)
                    for row in conn.execute(
                        f"SELECT id, path, name, qualified_name, linkage, condition, kind"
                        f" FROM nodes"
                        f" WHERE kind IN ('function','method')"
                        f" AND path NOT IN ({ph})",
                        all_dirty,
                    )
                ]
                # resolve 對照的檔案全集 = 現存所有檔(含未重抽的)
                all_files_rows = [(rel, files[rel]["hash"], _lang_for(rel), "") for rel in files]
                edges = self._edge_rows(all_files_rows, node_rows, relations,
                                        extra_callables=extra_callables)
                declarations = self._declaration_rows(relations)
                # _edge_rows 的 defines 只涵蓋重抽檔(relations keys),但它掃
                # node_rows 全集;這裡 node_rows 本來就只含重抽檔,一致。

                # dangling 的名稱要在 DELETE 之前蒐集:node 刪掉後就查不到
                # 名字,unresolved_target 會退化成 '?'(審核 #1)。
                doomed_names = dict(
                    conn.execute(
                        f"SELECT id, name FROM nodes WHERE path IN ({ph})", all_dirty)
                )

                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        f"DELETE FROM edges WHERE evidence_path IN ({ph})", all_dirty)
                    conn.execute(
                        f"DELETE FROM edges WHERE src_kind='file' AND src_id IN ({ph})",
                        all_dirty)
                    conn.execute(
                        f"DELETE FROM declarations WHERE path IN ({ph})", all_dirty)
                    conn.execute(f"DELETE FROM nodes WHERE path IN ({ph})", all_dirty)
                    conn.execute(f"DELETE FROM files WHERE path IN ({ph})", all_dirty)
                    conn.executemany(self._INSERT_FILE, file_rows)
                    conn.executemany(self._INSERT_NODE, node_rows)
                    conn.executemany(self._INSERT_DECLARATION, declarations)
                    conn.executemany(self._INSERT_EDGE, edges)
                    # 指向已消失 node 的 calls → 標 unresolved(檔案刪除語意);
                    # 名稱用 DELETE 前蒐集的 doomed_names,不留 '?'。
                    dangling = conn.execute(
                        "SELECT rowid, dst_id, unresolved_target FROM edges"
                        " WHERE type='calls' AND dst_id IS NOT NULL"
                        " AND dst_id NOT IN (SELECT id FROM nodes)"
                    ).fetchall()
                    for rowid, dst_id, existing_target in dangling:
                        target = existing_target or doomed_names.get(dst_id, "?")
                        conn.execute(
                            "UPDATE edges SET unresolved_target = ?, dst_kind = NULL,"
                            " dst_id = NULL, confidence = 'syntactic',"
                            " resolution_basis = 'syntactic_only' WHERE rowid = ?",
                            (target, rowid),
                        )
                    # parser_versions 刻意不動:它是 full-rebuild 的訊號
                    # (ensure_fresh 比對),增量只重抽部分檔案,不得洗白。
                    conn.execute(
                        "UPDATE index_metadata SET updated = ?",
                        (time.strftime("%Y-%m-%dT%H:%M:%S"),),
                    )
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            finally:
                conn.close()
        finally:
            fs_safety.release_file_lock(lock_fd)
        return False

    # ============================================================
    # Traversal API(§7.4;內部,不開新 MCP tool)
    # ============================================================
    def _require_ready(self):
        if not self._db_ready():
            raise CodeGraphError(
                "code graph 不存在或不可讀;semantic 查詢不受影響,"
                "neighbors/path 需要先成功 build"
            )

    @contextmanager
    def _read_snapshot(self):
        """單一讀連線 + 讀 transaction:traversal 的所有查詢共用同一個 WAL
        snapshot(審核 #4)。每個查詢各開連線的話,BFS 途中撞上另一 process
        的 COMMIT 會混到兩個 graph 世代,甚至組出從未同時存在過的路徑。
        BEGIN(deferred)後的第一個 SELECT 固定 snapshot,直到 rollback。
        """
        conn = self._connect()
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            try:
                conn.rollback()
            finally:
                conn.close()

    def _query(self, sql: str, params=(), conn: sqlite3.Connection | None = None):
        if conn is not None:
            return conn.execute(sql, params).fetchall()
        with self._read_snapshot() as snap:
            return snap.execute(sql, params).fetchall()

    _FIND_NODES_SQL = (
        "SELECT id, path, kind, name, qualified_name, start_line, end_line,"
        " backend, confidence, linkage, condition"
        " FROM nodes WHERE name = ? OR qualified_name = ?"
        " ORDER BY path, start_line LIMIT ?"
    )

    def find_nodes(self, name: str, limit: int = 20, *,
                   conn: sqlite3.Connection | None = None) -> list[dict]:
        # conn 給定 = 已在驗證過的 snapshot 內,跳過 _require_ready
        # (它會另開連線,破壞「單連線 traversal」的常數連線數契約)。
        if conn is None:
            self._require_ready()
        rows = self._query(self._FIND_NODES_SQL, (name, name, limit), conn=conn)
        return [self._node_dict(r) for r in rows]

    def find_file(self, rel_path: str, *,
                  conn: sqlite3.Connection | None = None) -> str | None:
        """files 表 exact match(正規化反斜線);找不到回 None。"""
        if conn is None:
            self._require_ready()
        normalized = rel_path.strip().replace("\\", "/")
        rows = self._query("SELECT path FROM files WHERE path = ?", (normalized,),
                           conn=conn)
        return rows[0][0] if rows else None

    @staticmethod
    def _node_dict(r) -> dict:
        return {
            "id": r[0], "path": r[1], "kind": r[2], "name": r[3],
            "qualified_name": r[4], "start_line": r[5], "end_line": r[6],
            "backend": r[7], "confidence": r[8],
            "linkage": r[9], "condition": r[10],
        }

    def _edge_dict(self, r, name_cache: dict,
                   conn: sqlite3.Connection | None = None) -> dict:
        src_name = self._id_name(r[1], name_cache, conn) if r[0] == "symbol" else r[1]
        dst_name = (
            self._id_name(r[3], name_cache, conn) if (r[2] == "symbol" and r[3]) else r[3]
        )
        return {
            "src_kind": r[0], "src_id": r[1], "src_name": src_name,
            "dst_kind": r[2], "dst_id": r[3], "dst_name": dst_name,
            "unresolved_target": r[4], "ambiguity_group": r[5],
            "type": r[6], "evidence_path": r[7], "evidence_line": r[8],
            "backend": r[9], "confidence": r[10],
            "resolution_basis": r[11], "condition": r[12],
            # resolved = 確定解析:歧義候選(ambiguity_group)有 dst_id 但
            # 只是候選之一,不得呈現成確定關係(審核 #3)。
            "resolved": r[3] is not None and r[5] is None,
        }

    _EDGE_COLS = ("src_kind, src_id, dst_kind, dst_id, unresolved_target,"
                  " ambiguity_group, type, evidence_path, evidence_line,"
                  " backend, confidence, resolution_basis, condition")

    def _id_name(self, node_id: str, cache: dict,
                 conn: sqlite3.Connection | None = None) -> str:
        if node_id not in cache:
            rows = self._query("SELECT name FROM nodes WHERE id = ?", (node_id,),
                               conn=conn)
            cache[node_id] = rows[0][0] if rows else node_id
        return cache[node_id]

    def _evidence_in_scope(self, edge: dict) -> bool:
        """§7.4 containment:evidence 路徑必須過 IndexScope.should_index_file。

        DB 被外部竄改塞進 scope 外路徑時,查詢端把它濾掉,不把 root 外
        (或已被 scope 排除)的路徑外洩到回答裡。刻意不耦合 media._safe_path。
        """
        return self._scanner.scope.should_index_file(edge.get("evidence_path", ""))

    def neighbors(self, node_id: str, edge_types: tuple = ("calls",),
                  direction: str = "both", hops: int = 1, limit: int = 50) -> dict:
        """BFS ≤2 hops。回傳 {nodes, edges, truncated}。deterministic ordering。

        整趟 BFS 在單一 read snapshot 內(審核 #4)。
        """
        self._require_ready()
        hops = min(max(int(hops), 1), 2)
        type_ph = ",".join("?" * len(edge_types))
        seen_nodes = {node_id}
        frontier = [node_id]
        edges_out = []
        truncated = False
        name_cache: dict = {}
        with self._read_snapshot() as conn:
            for _ in range(hops):
                next_frontier = []
                for nid in frontier:
                    clauses = []
                    if direction in ("out", "both"):
                        clauses.append(("src_id", "dst_id"))
                    if direction in ("in", "both"):
                        clauses.append(("dst_id", "src_id"))
                    for anchor_col, other_col in clauses:
                        rows = self._query(
                            f"SELECT {self._EDGE_COLS} FROM edges WHERE {anchor_col} = ?"
                            f" AND type IN ({type_ph})"
                            " ORDER BY evidence_path, evidence_line",
                            (nid, *edge_types), conn=conn,
                        )
                        for r in rows:
                            if len(edges_out) >= limit * 2:
                                truncated = True
                                break
                            edge = self._edge_dict(r, name_cache, conn)
                            if not self._evidence_in_scope(edge):
                                continue
                            edges_out.append(edge)
                            other = r[3] if other_col == "dst_id" else r[1]
                            if other and other not in seen_nodes:
                                if len(seen_nodes) >= limit:
                                    truncated = True
                                    continue
                                seen_nodes.add(other)
                                next_frontier.append(other)
                frontier = next_frontier
            node_rows = []
            for nid in sorted(seen_nodes):
                rows = self._query(
                    "SELECT id, path, kind, name, qualified_name, start_line, end_line,"
                    " backend, confidence, linkage, condition FROM nodes WHERE id = ?",
                    (nid,), conn=conn)
                node_rows.extend(self._node_dict(r) for r in rows)
        return {"nodes": node_rows, "edges": edges_out, "truncated": truncated}

    def file_neighbors(self, rel_path: str, hops: int = 1, limit: int = 50) -> dict:
        """file anchor 的 includes/imports 關係(審核 #5:MCP 的「這個檔
        include 誰」入口)。回傳 {files, edges, truncated};edges 含雙向
        (includes 與 included_by 都在,方向看 src/dst)。單一 snapshot。
        """
        self._require_ready()
        hops = min(max(int(hops), 1), 2)
        seen_files = {rel_path}
        frontier = [rel_path]
        edges_out: list[dict] = []
        truncated = False
        name_cache: dict = {}
        with self._read_snapshot() as conn:
            for _ in range(hops):
                next_frontier = []
                for current in frontier:
                    for anchor_col, other_col in (("src_id", "dst_id"), ("dst_id", "src_id")):
                        rows = self._query(
                            f"SELECT {self._EDGE_COLS} FROM edges WHERE {anchor_col} = ?"
                            " AND type IN ('includes','imports')"
                            " ORDER BY evidence_path, evidence_line",
                            (current,), conn=conn,
                        )
                        for r in rows:
                            if len(edges_out) >= limit * 2:
                                truncated = True
                                break
                            edge = self._edge_dict(r, name_cache, conn)
                            if not self._evidence_in_scope(edge):
                                continue
                            edges_out.append(edge)
                            other = r[3] if other_col == "dst_id" else r[1]
                            if other and other not in seen_files:
                                if len(seen_files) >= limit:
                                    truncated = True
                                    continue
                                seen_files.add(other)
                                next_frontier.append(other)
                frontier = next_frontier
        return {"files": sorted(seen_files), "edges": edges_out, "truncated": truncated}

    def shortest_evidence_paths(self, src_names: set, dst_names: set,
                                max_hops: int = 4, limit: int = 3) -> list[list[dict]]:
        """確定解析的 calls 邊上的最短路徑(≤max_hops、≤limit 條、cycle-safe)。

        歧義邊(ambiguity_group)不入路徑(審核 #3):候選之一不是確定呼叫,
        呈現成呼叫鏈會誤導。confidence 不在 CONFIRMED_EDGE_CONFIDENCE 的邊
        (Python attribute call 的 heuristic 同名猜測)同樣不入路徑——
        「只走確定解析的邊」是這個 mode 對外的契約。
        整趟搜尋在單一 read snapshot 內(審核 #4)。
        """
        self._require_ready()
        max_hops = min(int(max_hops), 4)
        limit = min(int(limit), 3)
        name_cache: dict = {}
        results: list[list[dict]] = []
        with self._read_snapshot() as conn:
            src_ids = {n["id"] for name in sorted(src_names)
                       for n in self.find_nodes(name, conn=conn)}
            dst_ids = {n["id"] for name in sorted(dst_names)
                       for n in self.find_nodes(name, conn=conn)}
            if not src_ids or not dst_ids:
                missing = "src" if not src_ids else "dst"
                raise CodeGraphError(
                    f"path 端點 resolve 失敗({missing});用 find_nodes 檢查名稱")

            # BFS(paths as edge lists);deterministic:鄰接按 evidence 排序
            confirmed_ph = ",".join("?" * len(CONFIRMED_EDGE_CONFIDENCE))
            queue: list[tuple[str, list]] = [(sid, []) for sid in sorted(src_ids)]
            best_depth: dict[str, int] = {sid: 0 for sid in src_ids}
            while queue and len(results) < limit:
                nid, path = queue.pop(0)
                if len(path) >= max_hops:
                    continue
                rows = self._query(
                    f"SELECT {self._EDGE_COLS} FROM edges WHERE src_id = ?"
                    " AND type='calls' AND dst_id IS NOT NULL"
                    " AND ambiguity_group IS NULL"
                    f" AND confidence IN ({confirmed_ph})"
                    " ORDER BY evidence_path, evidence_line",
                    (nid, *CONFIRMED_EDGE_CONFIDENCE), conn=conn,
                )
                for r in rows:
                    dst = r[3]
                    on_path = {e["src_id"] for e in path} | {nid}
                    if dst in on_path:
                        continue  # cycle
                    step = self._edge_dict(r, name_cache, conn)
                    if not self._evidence_in_scope(step):
                        continue
                    new_path = path + [step]
                    if dst in dst_ids:
                        results.append(new_path)
                        if len(results) >= limit:
                            break
                        continue
                    depth = len(new_path)
                    if best_depth.get(dst, max_hops + 1) > depth:
                        best_depth[dst] = depth
                        queue.append((dst, new_path))
        return results

    def _edges_for_ids(self, ids, anchor_col: str, name_cache: dict,
                       conn: sqlite3.Connection) -> list[dict]:
        out = []
        for nid in ids:
            rows = self._query(
                f"SELECT {self._EDGE_COLS} FROM edges WHERE {anchor_col} = ?"
                " AND type='calls' ORDER BY evidence_path, evidence_line",
                (nid,), conn=conn)
            out.extend(e for e in (self._edge_dict(r, name_cache, conn) for r in rows)
                       if self._evidence_in_scope(e))
        return out

    def callers(self, symbol_name: str) -> list[dict]:
        self._require_ready()
        name_cache: dict = {}
        with self._read_snapshot() as conn:
            ids = [n["id"] for n in self.find_nodes(symbol_name, conn=conn)]
            return self._edges_for_ids(ids, "dst_id", name_cache, conn)

    def callees(self, symbol_name: str) -> list[dict]:
        self._require_ready()
        name_cache: dict = {}
        with self._read_snapshot() as conn:
            ids = [n["id"] for n in self.find_nodes(symbol_name, conn=conn)]
            return self._edges_for_ids(ids, "src_id", name_cache, conn)

    def file_includes(self, rel_path: str) -> list[str]:
        """某檔的 direct includes/imports(resolved dst rel paths,排序)。"""
        self._require_ready()
        rows = self._query(
            "SELECT DISTINCT dst_id FROM edges WHERE src_kind='file' AND src_id = ?"
            " AND type IN ('includes','imports') AND dst_id IS NOT NULL"
            " ORDER BY dst_id", (rel_path,))
        return [r[0] for r in rows]

    def iter_call_edges(self) -> list[dict]:
        self._require_ready()
        name_cache: dict = {}
        with self._read_snapshot() as conn:
            rows = self._query(
                f"SELECT {self._EDGE_COLS} FROM edges WHERE type='calls'"
                " ORDER BY evidence_path, evidence_line", conn=conn)
            return [e for e in (self._edge_dict(r, name_cache, conn) for r in rows)
                    if self._evidence_in_scope(e)]

    def relations_for_symbol(self, name: str, limit: int = 5) -> list[dict]:
        """include_evidence 用的 1-hop 摘要(≤limit 條)。單一 snapshot。"""
        self._require_ready()
        name_cache: dict = {}
        with self._read_snapshot() as conn:
            ids = [n["id"] for n in self.find_nodes(name, conn=conn)]
            outs = self._edges_for_ids(ids, "src_id", name_cache, conn)
            ins = self._edges_for_ids(ids, "dst_id", name_cache, conn)
        return (outs + ins)[:limit]

    def suggest_names(self, text: str, limit: int = 5) -> list[str]:
        """neighbors 無 anchor 時的近似候選(substring match,§8.2)。"""
        self._require_ready()
        pattern = f"%{text.strip()}%"
        rows = self._query(
            "SELECT DISTINCT name FROM nodes WHERE name LIKE ?"
            " ORDER BY name LIMIT ?", (pattern, limit))
        return [r[0] for r in rows]

    def close(self) -> None:
        """目前連線都是 per-query 開關;保留此 API 供呼叫端對稱使用。"""


def _cli(argv: list[str]) -> int:
    """顯式建圖入口(§2:graph 缺席時查詢 fail-loud,首次建置走這裡)。

    用法:python code_graph.py --root <AICODE_ROOT>
    冪等:已存在就 in-place rebuild(§7.5)。
    """
    import argparse

    parser = argparse.ArgumentParser(description="Build the CodeTrail code graph")
    parser.add_argument("--root", required=True, help="專案根目錄(AICODE_ROOT)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"[CODE_GRAPH] root 不是目錄: {root}", file=sys.stderr)
        return 2
    graph = CodeGraph(str(root))
    graph.build(verbose=True)
    with graph._read_snapshot() as conn:
        nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    print(f"[CODE_GRAPH] done: {nodes} nodes, {edges} edges -> {graph.db_file}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
