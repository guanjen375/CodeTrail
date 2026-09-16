#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - AST 解析器

使用 AST 而非 regex 來提取程式碼符號，提供更精準的符號範圍和結構。

支援語言：
- Python: 使用內建 ast 模組
- JavaScript/TypeScript、C/C++、Go/Rust: 必須使用對應 tree-sitter grammar
- Java/Kotlin: 必須使用支援 JSON 的 Universal Ctags

依分析語言安裝相應依賴（缺失或不相容直接報錯）：
    pip install tree-sitter tree-sitter-python tree-sitter-javascript \
                tree-sitter-typescript tree-sitter-c tree-sitter-cpp \
                tree-sitter-go tree-sitter-rust
"""

import ast
import process_env
import os
import re
import shutil
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from runtime_dependencies import DependencyError


_CPP_EXTENSIONS = frozenset({
    '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.hh', '.hxx',
})

# Parser 的語意版本。symbol 的「哪些東西算 definition」、kind 拼法、linkage /
# condition / storage_class 的判定規則一改就要 bump。
#
# 版本消費矩陣(施工規格 §6 P2-5):
#   - CodeRAG cache:**是**(symbol 集合會變,舊 cache 的 index entry 不可信)
#   - CodeGraph fingerprint:**是**(node / defines edge 會變)
#   - eval vector manifest:**是**(corpus document 集合會變)
#
# v1(2026-08-19 之前的行為):C/C++ 只抽 class/struct/function/template。
# v2(P2):macro / macro_function / typedef / enum / enum_constant / global,
#        declaration 與 definition 分離,multi-declarator 逐一產生 symbol。
PARSER_SEMANTICS_VERSION = 4
# 只改 backend 准入；主 parser 的抽取語意及 eval 向量身分沒有改變。
PARSER_BACKEND_POLICY = "required-primary-v1"


class ParserDependencyError(DependencyError):
    """指定語言的主要 parser 缺失、不相容或無法執行。"""

# C/C++ 的 stable symbol kind。寫死成常數,避免 parser / cache / graph / test
# 各自用不同拼法(kind 字串會進持久 cache 與 graph node,拼錯是無聲的)。
CPP_MACRO_KIND = "macro"
CPP_MACRO_FUNCTION_KIND = "macro_function"
CPP_TYPEDEF_KIND = "typedef"
CPP_ENUM_KIND = "enum"
CPP_ENUM_CONSTANT_KIND = "enum_constant"
CPP_GLOBAL_KIND = "global"

# 只有 global 是有 linkage 的物件;macro / typedef / enum / enum_constant
# 在 C/C++ 語意上根本沒有 linkage,不得硬掰一個值。
CPP_LINKED_OBJECT_KINDS = frozenset({CPP_GLOBAL_KIND})
CPP_NEW_DEFINITION_KINDS = frozenset({
    CPP_MACRO_KIND, CPP_MACRO_FUNCTION_KIND, CPP_TYPEDEF_KIND,
    CPP_ENUM_KIND, CPP_ENUM_CONSTANT_KIND, CPP_GLOBAL_KIND,
})

# 檔頭 license / copyright 樣板。緊貼第一個 symbol 的檔頭註解不是那個 symbol
# 的說明,掛上去只會讓每個檔的第一個符號都帶著同一段法律文字進 embedding。
_FILE_HEADER_MARKERS = (
    "spdx-license-identifier", "copyright", "all rights reserved",
    "licensed under", "license:", "gnu general public",
)

# leading comment 的行數上限。超長的區塊註解(整段設計說明)不該整段擠進
# 每個 symbol 的表示式,那是 noise 不是訊號。
MAX_LEADING_COMMENT_LINES = 12

# translation-unit / namespace scope 的判定:往上走只能經過這些節點。
# 碰到 compound_statement(函式本體)、field_declaration_list(struct/class
# 本體)、parameter_list 等就不是 TU scope —— local 變數與 member 一律排除。
_TU_SCOPE_ANCESTORS = frozenset({
    "translation_unit",
    "preproc_if", "preproc_ifdef", "preproc_elif", "preproc_else",
    "linkage_specifier", "declaration_list", "namespace_definition",
    "type_definition", "declaration", "template_declaration",
})

# 能力 probe 不能讓不使用此語言的功能 import 失敗。
_TREE_SITTER_IMPORT_ERROR = ""
try:
    import tree_sitter
    from tree_sitter import Language, Parser
    HAS_TREE_SITTER = True
except Exception as exc:
    _TREE_SITTER_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    HAS_TREE_SITTER = False
    tree_sitter = None
    Language = None
    Parser = None

# 嘗試載入各語言的 tree-sitter
_TREE_SITTER_LANGUAGES = {}
_TREE_SITTER_LANGUAGE_ERRORS = {}


def _try_load_tree_sitter_language(lang_name: str):
    """嘗試載入 tree-sitter 語言模組"""
    if not HAS_TREE_SITTER:
        return None
    if lang_name in _TREE_SITTER_LANGUAGES:
        return _TREE_SITTER_LANGUAGES[lang_name]

    try:
        if lang_name == 'python':
            import tree_sitter_python as ts_python
            lang = Language(ts_python.language())
        elif lang_name == 'javascript':
            import tree_sitter_javascript as ts_js
            lang = Language(ts_js.language())
        elif lang_name == 'typescript':
            import tree_sitter_typescript as ts_ts
            lang = Language(ts_ts.language_typescript())
        elif lang_name == 'tsx':
            import tree_sitter_typescript as ts_ts
            lang = Language(ts_ts.language_tsx())
        elif lang_name == 'c':
            import tree_sitter_c as ts_c
            lang = Language(ts_c.language())
        elif lang_name == 'cpp':
            import tree_sitter_cpp as ts_cpp
            lang = Language(ts_cpp.language())
        elif lang_name == 'go':
            import tree_sitter_go as ts_go
            lang = Language(ts_go.language())
        elif lang_name == 'rust':
            import tree_sitter_rust as ts_rust
            lang = Language(ts_rust.language())
        else:
            lang = None

        if lang is not None:
            Parser(lang)  # 同 process 的 ABI probe；不能 spawn 或換 parser。
        _TREE_SITTER_LANGUAGES[lang_name] = lang
        return lang
    except Exception as exc:
        _TREE_SITTER_LANGUAGE_ERRORS[lang_name] = f"{type(exc).__name__}: {exc}"
        _TREE_SITTER_LANGUAGES[lang_name] = None
        return None


def _parser_dependency_error(lang_name: str, detail: str = "") -> ParserDependencyError:
    package = "typescript" if lang_name == "tsx" else lang_name
    reason = detail or _TREE_SITTER_LANGUAGE_ERRORS.get(lang_name) or _TREE_SITTER_IMPORT_ERROR
    return ParserDependencyError(
        f"Code parser unavailable for {lang_name}: {reason or 'tree-sitter core/grammar is not loaded'}. "
        f"Install compatible tree-sitter and tree-sitter-{package} packages in the CodeTrail Python environment."
    )


def require_tree_sitter_parser(lang_name: str):
    """取得必要 parser；probe 保留 None 介面供唯讀 advisory verifier 使用。"""
    language = _try_load_tree_sitter_language(lang_name)
    if not HAS_TREE_SITTER or language is None or Parser is None:
        raise _parser_dependency_error(lang_name)
    try:
        return Parser(language)
    except Exception as exc:
        raise _parser_dependency_error(lang_name, f"{type(exc).__name__}: {exc}") from exc


@dataclass
class Symbol:
    """程式碼符號 - P0 改進：擴充 embedding 內容"""
    name: str
    type: str  # 'function', 'class', 'method', 'interface', 'struct', etc.
    start_line: int  # 1-based
    end_line: int    # 1-based, 包含
    context: str     # 符號定義的上下文（前幾行）
    parent: Optional[str] = None  # 父類別名稱（如果是 method）
    # P0 改進：擴充欄位
    signature: Optional[str] = None  # 函式簽名（含參數和返回值）
    docstring: Optional[str] = None  # 文檔字串
    type_hints: Optional[str] = None  # 類型提示
    comments: Optional[str] = None  # 相關註解
    # graph 前置(§6.2-6):完整限定名(C++ A::B::f、Python Class.method;
    # 無 parent 即本名)與產生此符號的 parser backend
    # ("python-ast" | "tree-sitter" | "regex" | "ctags")。
    # graph 的 stable node ID 依賴 qualified_name;backend 進 evidence 揭露。
    qualified_name: Optional[str] = None
    backend: Optional[str] = None
    # C/C++ graph metadata。其他語言維持 None；definition 至少區分
    # internal(`static`)與 external，condition 保存外層 preprocessor branch。
    linkage: Optional[str] = None
    condition: Optional[str] = None
    # 原始 storage-class specifier("static" / "extern" / "inline" / ...,
    # 多個以空白相連)。linkage 是推論結果,storage_class 是原文事實 ——
    # 兩者分開存,推論規則之後改了還能回頭核對原始宣告。
    storage_class: Optional[str] = None


class PythonASTParser:
    """使用內建 ast 模組解析 Python

    改進：使用 NodeVisitor 追蹤父節點，正確排除巢狀函式（nested function）。
    只收錄：
    - 模組級別的 class
    - 模組級別的 function
    - class 內的第一層 method
    不收錄：
    - function 內的巢狀 function（如 decorator inner、closure helper）
    - method 內的巢狀 function
    """

    def parse(self, content: str, filepath: Path) -> list[Symbol]:
        """解析 Python 程式碼"""
        try:
            tree = ast.parse(content, filename=str(filepath))
        except SyntaxError:
            return []

        lines = content.split('\n')
        visitor = _PythonSymbolVisitor(lines, self)
        visitor.visit(tree)
        for sym in visitor.symbols:
            sym.backend = "python-ast"
            if sym.qualified_name is None:
                # 注意 class 的 parent 欄是「繼承的父類」不是 scope;只有
                # method 的 parent 是其 class,才進 qualified 鏈。
                if sym.type == "method" and sym.parent:
                    sym.qualified_name = f"{sym.parent}.{sym.name}"
                else:
                    sym.qualified_name = sym.name
        return visitor.symbols


class _PythonSymbolVisitor(ast.NodeVisitor):
    """Python AST Visitor - 追蹤 scope 層級以排除巢狀函式"""

    def __init__(self, lines: list, parser: 'PythonASTParser'):
        self.lines = lines
        self.parser = parser
        self.symbols = []
        # scope_stack 記錄當前的 scope 類型：'module', 'class', 'function'
        self.scope_stack = ['module']

    def visit_ClassDef(self, node: ast.ClassDef):
        """處理 class 定義"""
        # class 只在模組層級收錄
        if self.scope_stack[-1] == 'module':
            start_line = node.lineno
            end_line = self.parser._get_end_line(node)
            context = self.parser._get_context(self.lines, start_line, end_line)

            # P0 改進：提取 docstring
            docstring = ast.get_docstring(node)

            # P0 改進：提取父類名稱（繼承）
            bases = []
            for base in node.bases:
                if isinstance(base, ast.Name):
                    bases.append(base.id)
                elif isinstance(base, ast.Attribute):
                    bases.append(f"{base.attr}")
            parent_classes = ', '.join(bases) if bases else None

            # P0 改進：提取 class 簽名（含繼承）
            signature = f"class {node.name}"
            if bases:
                signature += f"({', '.join(bases)})"

            self.symbols.append(Symbol(
                name=node.name,
                type='class',
                start_line=start_line,
                end_line=end_line,
                context=context,
                signature=signature,
                docstring=docstring[:300] if docstring else None,
                parent=parent_classes
            ))

            # 進入 class scope，處理其中的 methods
            self.scope_stack.append('class')
            self._current_class = node.name
            self.generic_visit(node)
            self.scope_stack.pop()
            self._current_class = None
        else:
            # 巢狀 class（較少見），不收錄但仍遍歷
            self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        """處理 function/method 定義"""
        self._handle_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        """處理 async function/method 定義"""
        self._handle_function(node)

    def _handle_function(self, node):
        """統一處理 function 和 async function"""
        current_scope = self.scope_stack[-1]

        if current_scope == 'module':
            # 模組級別的 function → 收錄
            start_line = node.lineno
            end_line = self.parser._get_end_line(node)
            context = self.parser._get_context(self.lines, start_line, end_line)

            # P0 改進：提取 docstring
            docstring = ast.get_docstring(node)

            # P0 改進：提取函式簽名（含參數和類型提示）
            signature, type_hints = self._extract_function_signature(node)

            self.symbols.append(Symbol(
                name=node.name,
                type='function',
                start_line=start_line,
                end_line=end_line,
                context=context,
                signature=signature,
                docstring=docstring[:300] if docstring else None,
                type_hints=type_hints
            ))
            # 進入 function scope（其內的 function 不收錄）
            self.scope_stack.append('function')
            self.generic_visit(node)
            self.scope_stack.pop()

        elif current_scope == 'class':
            # class 內的第一層 method → 收錄
            start_line = node.lineno
            end_line = self.parser._get_end_line(node)
            context = self.parser._get_context(self.lines, start_line, end_line)

            # P0 改進：提取 docstring
            docstring = ast.get_docstring(node)

            # P0 改進：提取函式簽名（含參數和類型提示）
            signature, type_hints = self._extract_function_signature(node)

            self.symbols.append(Symbol(
                name=node.name,
                type='method',
                start_line=start_line,
                end_line=end_line,
                context=context,
                parent=getattr(self, '_current_class', None),
                signature=signature,
                docstring=docstring[:300] if docstring else None,
                type_hints=type_hints
            ))
            # 進入 function scope（method 內的 function 不收錄）
            self.scope_stack.append('function')
            self.generic_visit(node)
            self.scope_stack.pop()

        else:
            # current_scope == 'function'
            # 這是巢狀函式（nested function），不收錄
            # 但仍要遍歷其子節點（可能有更深的巢狀）
            self.scope_stack.append('function')
            self.generic_visit(node)
            self.scope_stack.pop()

    def _extract_function_signature(self, node) -> tuple[str, str]:
        """P0 改進：提取函式簽名和類型提示"""
        # 建構簽名
        is_async = isinstance(node, ast.AsyncFunctionDef)
        prefix = "async def" if is_async else "def"

        # 提取參數
        args = node.args
        params = []
        type_hints_parts = []

        # 處理一般參數
        for i, arg in enumerate(args.args):
            param_str = arg.arg
            if arg.annotation:
                ann = self._annotation_to_str(arg.annotation)
                param_str += f": {ann}"
                type_hints_parts.append(f"{arg.arg}: {ann}")
            params.append(param_str)

        # 處理 *args
        if args.vararg:
            param_str = f"*{args.vararg.arg}"
            if args.vararg.annotation:
                ann = self._annotation_to_str(args.vararg.annotation)
                param_str += f": {ann}"
            params.append(param_str)

        # 處理 **kwargs
        if args.kwarg:
            param_str = f"**{args.kwarg.arg}"
            if args.kwarg.annotation:
                ann = self._annotation_to_str(args.kwarg.annotation)
                param_str += f": {ann}"
            params.append(param_str)

        # 建構完整簽名
        signature = f"{prefix} {node.name}({', '.join(params)})"

        # 返回類型
        if node.returns:
            ret_ann = self._annotation_to_str(node.returns)
            signature += f" -> {ret_ann}"
            type_hints_parts.append(f"return: {ret_ann}")

        type_hints = ", ".join(type_hints_parts) if type_hints_parts else None
        return signature, type_hints

    def _annotation_to_str(self, annotation) -> str:
        """將 AST annotation 轉換為字串"""
        if isinstance(annotation, ast.Name):
            return annotation.id
        elif isinstance(annotation, ast.Constant):
            return str(annotation.value)
        elif isinstance(annotation, ast.Subscript):
            value = self._annotation_to_str(annotation.value)
            slice_val = self._annotation_to_str(annotation.slice)
            return f"{value}[{slice_val}]"
        elif isinstance(annotation, ast.Attribute):
            return f"{self._annotation_to_str(annotation.value)}.{annotation.attr}"
        elif isinstance(annotation, ast.Tuple):
            elts = [self._annotation_to_str(e) for e in annotation.elts]
            return f"({', '.join(elts)})"
        elif isinstance(annotation, ast.List):
            elts = [self._annotation_to_str(e) for e in annotation.elts]
            return f"[{', '.join(elts)}]"
        elif isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            # Union type: X | Y
            left = self._annotation_to_str(annotation.left)
            right = self._annotation_to_str(annotation.right)
            return f"{left} | {right}"
        else:
            return "..."


# PythonASTParser 的 helper methods（放在 class 外供 visitor 使用）
def _get_end_line(node) -> int:
    """取得節點的結束行號"""
    if hasattr(node, 'end_lineno') and node.end_lineno:
        return node.end_lineno
    # Fallback: 遍歷子節點找最大行號
    max_line = node.lineno
    for child in ast.walk(node):
        if hasattr(child, 'lineno') and child.lineno:
            max_line = max(max_line, child.lineno)
        if hasattr(child, 'end_lineno') and child.end_lineno:
            max_line = max(max_line, child.end_lineno)
    return max_line


def _get_context(lines: list, start_line: int, end_line: int, max_lines: int = 15) -> str:
    """取得符號的上下文（定義區塊）

    以 end_line 截斷:短函式的 context 不得吃到下一個函式的內容
    (embedding / rerank passage 都吃這份,吃錯鄰居會把檢索訊號污染掉)。
    上限仍是 max_lines(15 行);儲存端另有 500 chars 截斷。
    """
    start_idx = start_line - 1
    # signature + 前幾行:不超過 max_lines,也不超過符號自己的 end_line(1-based 含)
    context_end = min(start_idx + max_lines, end_line, len(lines))
    context_end = max(context_end, start_idx + 1)  # 保底含定義行自身
    context_lines = lines[start_idx:context_end]
    return '\n'.join(context_lines)


# 為 PythonASTParser 加上 instance methods（向後相容）
PythonASTParser._get_end_line = staticmethod(_get_end_line)
PythonASTParser._get_context = staticmethod(_get_context)


class TreeSitterParser:
    """使用 tree-sitter 解析各語言"""

    def __init__(self, language_name: str):
        self.language_name = language_name
        self.language = _try_load_tree_sitter_language(language_name)
        self.parser = require_tree_sitter_parser(language_name)

    def parse(self, content: str, filepath: Path) -> list[Symbol]:
        """解析程式碼"""
        try:
            tree = self.parser.parse(bytes(content, 'utf-8'))
        except Exception as exc:
            raise _parser_dependency_error(self.language_name, f"{type(exc).__name__}: {exc}") from exc

        symbols = []
        lines = content.split('\n')

        self._extract_symbols(tree.root_node, lines, symbols)
        for sym in symbols:
            sym.backend = "tree-sitter"
            if sym.qualified_name is None:
                if sym.type == "method" and sym.parent:
                    sep = "::" if self.language_name in ("c", "cpp") else "."
                    sym.qualified_name = f"{sym.parent}{sep}{sym.name}"
                else:
                    sym.qualified_name = sym.name
        return symbols

    def _extract_symbols(self, node, lines: list, symbols: list, parent_name: str = None):
        """遞迴提取符號"""
        node_type = node.type

        # JavaScript/TypeScript
        if self.language_name in ('javascript', 'typescript', 'tsx'):
            self._extract_js_symbols(node, lines, symbols, parent_name)
        # C/C++
        elif self.language_name in ('c', 'cpp'):
            self._extract_cpp_symbols(node, lines, symbols, parent_name)
        # Go
        elif self.language_name == 'go':
            self._extract_go_symbols(node, lines, symbols, parent_name)
        # Rust
        elif self.language_name == 'rust':
            self._extract_rust_symbols(node, lines, symbols, parent_name)
        # Python (通常用 ast 模組更好，但作為備用)
        elif self.language_name == 'python':
            self._extract_python_symbols(node, lines, symbols, parent_name)

    def _extract_js_symbols(self, node, lines: list, symbols: list, parent_name: str = None):
        """提取 JavaScript/TypeScript 符號"""
        node_type = node.type

        if node_type == 'class_declaration':
            name_node = node.child_by_field_name('name')
            if name_node:
                name = name_node.text.decode('utf-8')
                symbols.append(self._make_symbol(node, lines, name, 'class'))
                # 遞迴處理 class body
                body = node.child_by_field_name('body')
                if body:
                    for child in body.children:
                        self._extract_js_symbols(child, lines, symbols, name)

        elif node_type == 'function_declaration':
            name_node = node.child_by_field_name('name')
            if name_node:
                name = name_node.text.decode('utf-8')
                sym_type = 'method' if parent_name else 'function'
                symbols.append(self._make_symbol(node, lines, name, sym_type, parent_name))

        elif node_type == 'method_definition':
            name_node = node.child_by_field_name('name')
            if name_node:
                name = name_node.text.decode('utf-8')
                symbols.append(self._make_symbol(node, lines, name, 'method', parent_name))

        elif node_type in ('lexical_declaration', 'variable_declaration'):
            # const foo = () => {} or const foo = function() {}
            for decl in node.children:
                if decl.type == 'variable_declarator':
                    name_node = decl.child_by_field_name('name')
                    value_node = decl.child_by_field_name('value')
                    if name_node and value_node:
                        if value_node.type in ('arrow_function', 'function_expression'):
                            name = name_node.text.decode('utf-8')
                            symbols.append(self._make_symbol(node, lines, name, 'function'))

        elif node_type in ('interface_declaration', 'type_alias_declaration'):
            name_node = node.child_by_field_name('name')
            if name_node:
                name = name_node.text.decode('utf-8')
                sym_type = 'interface' if node_type == 'interface_declaration' else 'type'
                symbols.append(self._make_symbol(node, lines, name, sym_type))

        # 遞迴處理子節點
        for child in node.children:
            if child.type not in ('class_body', 'statement_block'):
                self._extract_js_symbols(child, lines, symbols, parent_name)

    def _extract_cpp_symbols(self, node, lines: list, symbols: list, parent_name: str = None,
                             qualified_prefix: str = ""):
        """提取 C/C++ 符號

        qualified_prefix 是外層 namespace/class 鏈("NS::A::"),隨顯式 body
        遞迴累積,用來組裝 qualified_name(graph stable ID 依賴它,§6.2-6)。
        """
        node_type = node.type

        if node_type in ('preproc_def', 'preproc_function_def'):
            # macro definition。preprocessor 沒有 C scope,所以不限 TU scope;
            # 但**不做** macro expansion / evaluation —— 只登記定義本身。
            self._extract_macro(node, lines, symbols, qualified_prefix)
            return

        if node_type == 'type_definition':
            self._extract_typedef(node, lines, symbols, parent_name, qualified_prefix)
            return

        if node_type == 'enum_specifier':
            self._extract_enum(node, lines, symbols, qualified_prefix, alias=None)
            return

        if node_type == 'declaration':
            # 物件定義(global / ops table / function pointer)。函式原型排除。
            self._extract_declaration_objects(node, lines, symbols, qualified_prefix)
            # 不 return:`struct S { ... } inst;` 的 struct 本體仍要走下面的遞迴。

        if node_type in ('class_specifier', 'struct_specifier', 'union_specifier'):
            name_node = node.child_by_field_name('name')
            # 只有帶 body 才是 type definition。`struct driver_ops;`(forward tag)
            # 與 `struct driver_ops *p;` 裡的型別**引用**都不是定義 —— 舊版把
            # 兩者都當 struct definition,是 §3 洞 1b 的假定義來源。
            has_body = node.child_by_field_name('body') is not None or any(
                child.type == 'field_declaration_list' for child in node.children
            )
            if name_node and has_body:
                name = name_node.text.decode('utf-8')
                sym_type = {
                    'class_specifier': 'class',
                    'struct_specifier': 'struct',
                    'union_specifier': 'union',
                }[node_type]
                symbols.append(self._make_symbol(
                    node, lines, name, sym_type,
                    qualified_name=f"{qualified_prefix}{name}",
                    condition=self._preprocessor_condition(node),
                ))
                # 顯式走完 body 後 return(§6.2-1):否則尾端的泛型遞迴會把
                # body 再走一次,class 內每個 method 都輸出兩份(一份 method、
                # 一份誤標 function)。
                body = node.child_by_field_name('body')
                if body:
                    for child in body.children:
                        self._extract_cpp_symbols(
                            child, lines, symbols, name, f"{qualified_prefix}{name}::"
                        )
                return
            if not has_body:
                # forward tag declaration 或型別引用:沒有 body 就沒有定義,
                # 底下也沒有東西可遞迴。
                return
            # 匿名 class/struct:自身不成符號,fall through 讓泛型遞迴進 body

        elif node_type == 'function_definition':
            declarator = node.child_by_field_name('declarator')
            if declarator:
                name = self._get_cpp_function_name(declarator)
                if name:
                    sym_type = 'method' if parent_name else 'function'
                    storage, _quals = self._declaration_specifiers(node)
                    symbols.append(self._make_symbol(
                        node, lines, name, sym_type, parent_name,
                        qualified_name=f"{qualified_prefix}{name}",
                        linkage=self._cpp_function_linkage(node),
                        condition=self._preprocessor_condition(node),
                        storage_class=" ".join(sorted(storage)) or None,
                    ))

        elif node_type == 'namespace_definition':
            name_node = node.child_by_field_name('name')
            if name_node:
                name = name_node.text.decode('utf-8')
                symbols.append(self._make_symbol(
                    node, lines, name, 'namespace',
                    qualified_name=f"{qualified_prefix}{name}",
                ))
                body = node.child_by_field_name('body')
                if body:
                    for child in body.children:
                        self._extract_cpp_symbols(
                            child, lines, symbols, parent_name,
                            f"{qualified_prefix}{name}::",
                        )
                return
            # 匿名 namespace:fall through,內容物不帶前綴

        elif node_type == 'template_declaration':
            # template<...> class/function
            for child in node.children:
                self._extract_cpp_symbols(child, lines, symbols, parent_name, qualified_prefix)
            return  # 已處理子節點

        # 遞迴
        for child in node.children:
            self._extract_cpp_symbols(child, lines, symbols, parent_name, qualified_prefix)

    # ------------------------------------------------------------
    # leading comment association(施工規格 §6 P3A-2)
    # ------------------------------------------------------------
    @staticmethod
    def _leading_comment(node, lines: list) -> Optional[str]:
        """緊貼在定義**上方**、同一 scope 的註解區塊。

        C 的 ``/** ... */`` 寫在定義行之上,而 context 是從定義行**往下**取 ——
        結構上永遠拿不到。這裡從 node 的前一個同層 sibling 往回收。

        邊界(四條都必要,少一條就會掛錯東西):
          * **同 scope**:只走 sibling,不跨父節點。定義包在 ``#if`` 裡而註解在
            外面時,兩者不是 sibling,自然不會被撈進來。
          * **不跨空行**:中間有空行就停 —— 那通常是上一個 symbol 的尾註。
          * **不跨 preprocessor / 其他節點**:sibling 不是 comment 就停。
          * **不吃檔頭 license**:檔案第一個節點又長得像版權標頭就跳過。
        """
        collected: list = []
        current = node
        while True:
            # `_make_symbol` 一直接受「只有 start_point / end_point」的輕量 node
            # (測試用的 double 就是這樣)。leading comment 是加值資訊,拿不到
            # sibling API 就當作沒有註解,不能因此把整個 symbol 抽取打爆。
            previous = getattr(current, "prev_named_sibling", None)
            if previous is None or previous.type != "comment":
                break
            # 註解結尾與下一個節點開頭之間不得有空行。
            if current.start_point[0] - previous.end_point[0] > 1:
                break
            if previous.prev_named_sibling is None:
                text = previous.text.decode("utf-8", errors="replace").lower()
                if any(marker in text for marker in _FILE_HEADER_MARKERS):
                    break
            collected.append(previous)
            current = previous

        if not collected:
            return None
        collected.reverse()
        start = collected[0].start_point[0]
        end = collected[-1].end_point[0]
        if end - start + 1 > MAX_LEADING_COMMENT_LINES:
            start = end - MAX_LEADING_COMMENT_LINES + 1
        block = "\n".join(lines[start:end + 1])
        return " ".join(block.split()) or None

    # ------------------------------------------------------------
    # C/C++ definition 語意(施工規格 §6 P2-2)
    # ------------------------------------------------------------
    @staticmethod
    def _is_translation_unit_scope(node) -> bool:
        """只有 translation-unit / namespace scope(含 preprocessor wrapper)算數。

        function-local 變數、struct/class member、parameter 一律不是定義候選。
        """
        current = node.parent
        while current is not None:
            if current.type not in _TU_SCOPE_ANCESTORS:
                return False
            current = current.parent
        return True

    @staticmethod
    def _declaration_specifiers(node) -> tuple[set, set]:
        """從宣告節點取 storage-class specifier 與 type qualifier 的原文。"""
        storage: set = set()
        qualifiers: set = set()
        for child in node.children:
            text = child.text.decode('utf-8', errors='replace').strip()
            if child.type == 'storage_class_specifier':
                storage.add(text)
            elif child.type == 'type_qualifier':
                qualifiers.add(text)
        return storage, qualifiers

    @classmethod
    def _declarator_name(cls, node) -> tuple:
        """展開 declarator,回傳 (name, shape)。

        shape:
          ``object``              一般物件(含 pointer / array)
          ``function_prototype``  ``int f(int);`` —— 宣告,不是定義,要排除
          ``function_pointer``    ``int (*handler)(int);`` —— 是**物件**定義,
                                  韌體的 ops table / callback slot 正是這一類
        """
        if node is None:
            return None, 'object'
        node_type = node.type
        if node_type in ('identifier', 'type_identifier', 'field_identifier',
                         'qualified_identifier', 'primitive_type'):
            return node.text.decode('utf-8', errors='replace'), 'object'
        if node_type == 'init_declarator':
            # 有 initializer 一定是定義,即使外面掛了 extern。
            name, _shape = cls._declarator_name(node.child_by_field_name('declarator'))
            return name, 'object'
        if node_type in ('pointer_declarator', 'array_declarator',
                         'reference_declarator'):
            return cls._declarator_name(node.child_by_field_name('declarator'))
        if node_type == 'parenthesized_declarator':
            for child in node.named_children:
                name, shape = cls._declarator_name(child)
                if name:
                    return name, shape
            return None, 'object'
        if node_type == 'function_declarator':
            inner = node.child_by_field_name('declarator')
            if inner is None:
                return None, 'object'
            name, _shape = cls._declarator_name(inner)
            if inner.type == 'parenthesized_declarator':
                return name, 'function_pointer'
            return name, 'function_prototype'
        return None, 'object'

    def _object_linkage(self, node, storage: set, qualifiers: set) -> str:
        """物件(非函式)的 linkage。C 精確;C++ 證明不了就 unknown,不猜 external。"""
        if 'static' in storage:
            return 'internal'
        current = node.parent
        while current is not None:
            if (current.type == 'namespace_definition'
                    and current.child_by_field_name('name') is None):
                return 'internal'
            if current.type in ('template_declaration', 'template_instantiation'):
                # template 內的 linkage 牽涉 instantiation 規則,證明不了。
                return 'unknown'
            current = current.parent
        if 'extern' in storage:
            return 'external'
        if self.language_name == 'c':
            # C:file-scope 無 static 即 external(tentative definition 也是)。
            return 'external'
        # C++:namespace-scope 的 non-volatile const / constexpr 預設 internal
        # linkage —— 「非 static 就是 external」在 C++ 是錯的。
        if 'inline' in storage or 'inline' in qualifiers:
            return 'external'
        const_like = ('const' in qualifiers or 'constexpr' in qualifiers
                      or 'constexpr' in storage)
        if const_like and 'volatile' not in qualifiers:
            return 'internal'
        if storage - {'extern', 'static', 'inline'}:
            # thread_local / mutable / 其他沒建模的 specifier:不猜。
            return 'unknown'
        return 'external'

    def _extract_macro(self, node, lines: list, symbols: list,
                       qualified_prefix: str) -> None:
        name_node = node.child_by_field_name('name')
        if not name_node:
            return
        name = name_node.text.decode('utf-8', errors='replace')
        kind = (CPP_MACRO_FUNCTION_KIND if node.type == 'preproc_function_def'
                else CPP_MACRO_KIND)
        symbols.append(self._make_symbol(
            node, lines, name, kind,
            qualified_name=f"{qualified_prefix}{name}",
            condition=self._preprocessor_condition(node),
        ))

    def _extract_enum(self, node, lines: list, symbols: list,
                      qualified_prefix: str, alias: Optional[str]) -> None:
        """enum 定義 + 每個 enumerator。

        匿名 enum 帶 typedef alias 時用 alias 當 enum 名(``typedef enum {..}
        state_t;``);完全匿名時不造假名字,enumerator 的 parent 明確設 None。
        """
        if not self._is_translation_unit_scope(node):
            return
        body = node.child_by_field_name('body') or next(
            (child for child in node.children if child.type == 'enumerator_list'), None
        )
        if body is None:
            return  # `enum color c;` 的型別引用,不是定義
        name_node = node.child_by_field_name('name')
        enum_name = name_node.text.decode('utf-8', errors='replace') if name_node else alias
        condition = self._preprocessor_condition(node)
        # C++ scoped enum(`enum class` / `enum struct`)的 enumerator 是
        # `State::Idle`,不是 `Idle`。unscoped enum 相反:enumerator 本來就落在
        # 外層 scope,加前綴才是錯的。固定產生裸名的話,兩個 scoped enum 只要有
        # 同名 enumerator 就會撞成同一個 qualified_name —— graph 的 stable node ID
        # 依賴它,撞名等於查找結果不準。
        is_scoped = any(child.type in ('class', 'struct') for child in node.children)
        enumerator_prefix = (
            f"{qualified_prefix}{enum_name}::" if is_scoped and enum_name
            else qualified_prefix
        )
        if enum_name:
            symbols.append(self._make_symbol(
                node, lines, enum_name, CPP_ENUM_KIND,
                qualified_name=f"{qualified_prefix}{enum_name}",
                condition=condition,
            ))
        for child in body.named_children:
            if child.type != 'enumerator':
                continue
            enumerator_name_node = child.child_by_field_name('name') or next(
                (grand for grand in child.children if grand.type == 'identifier'), None
            )
            if enumerator_name_node is None:
                continue
            enumerator_name = enumerator_name_node.text.decode('utf-8', errors='replace')
            symbols.append(self._make_symbol(
                child, lines, enumerator_name, CPP_ENUM_CONSTANT_KIND,
                parent=enum_name,
                qualified_name=f"{enumerator_prefix}{enumerator_name}",
                condition=condition,
            ))

    def _extract_typedef(self, node, lines: list, symbols: list,
                         parent_name: Optional[str], qualified_prefix: str) -> None:
        """typedef:**每個 declarator 都是一個 typedef**,不是只取第一個。"""
        if not self._is_translation_unit_scope(node):
            return
        condition = self._preprocessor_condition(node)
        names: list[str] = []
        for declarator in node.children_by_field_name('declarator'):
            name, _shape = self._declarator_name(declarator)
            if not name:
                continue
            names.append(name)
            symbols.append(self._make_symbol(
                node, lines, name, CPP_TYPEDEF_KIND,
                qualified_name=f"{qualified_prefix}{name}",
                condition=condition,
                anchor_node=declarator,
            ))

        # typedef 內嵌的型別定義自己也要進索引;走完就不再讓泛型遞迴重複處理。
        alias = names[0] if names else None
        for child in node.children:
            if child.type == 'enum_specifier':
                self._extract_enum(child, lines, symbols, qualified_prefix, alias)
            elif child.type in ('struct_specifier', 'union_specifier',
                                'class_specifier'):
                self._extract_cpp_symbols(
                    child, lines, symbols, parent_name, qualified_prefix
                )

    def _extract_declaration_objects(self, node, lines: list, symbols: list,
                                     qualified_prefix: str) -> None:
        """translation-unit / namespace scope 的物件定義,逐 declarator 產生。

        規則(§6 P2-2):
          ``uint32_t g_error_counter;``      1 個 global(C tentative definition)
          ``static int a, *b;``              2 個 internal-linkage definition
          ``extern int only_declared;``      0 個(純宣告)
          ``extern int defined_here = 1;``   1 個(有 initializer)
          ``int prototype_only(int);``       0 個(函式原型)
          ``static int (*handler)(int);``    1 個(function pointer 是物件)
        """
        if not self._is_translation_unit_scope(node):
            return
        storage, qualifiers = self._declaration_specifiers(node)
        linkage = self._object_linkage(node, storage, qualifiers)
        condition = self._preprocessor_condition(node)
        storage_text = " ".join(sorted(storage)) or None

        for declarator in node.children_by_field_name('declarator'):
            name, shape = self._declarator_name(declarator)
            if not name or shape == 'function_prototype':
                continue
            has_initializer = declarator.type == 'init_declarator'
            if 'extern' in storage and not has_initializer:
                continue  # 純宣告:別的 translation unit 才有定義
            symbols.append(self._make_symbol(
                node, lines, name, CPP_GLOBAL_KIND,
                qualified_name=f"{qualified_prefix}{name}",
                linkage=linkage,
                condition=condition,
                storage_class=storage_text,
                anchor_node=declarator,
            ))

    def _get_cpp_function_name(self, declarator) -> Optional[str]:
        """從 declarator 中提取函式名"""
        if declarator.type == 'function_declarator':
            inner = declarator.child_by_field_name('declarator')
            if inner:
                if inner.type == 'identifier':
                    return inner.text.decode('utf-8')
                elif inner.type == 'qualified_identifier':
                    # namespace::function_name
                    return inner.text.decode('utf-8')
                elif inner.type == 'field_identifier':
                    return inner.text.decode('utf-8')
                else:
                    return self._get_cpp_function_name(inner)
        elif declarator.type == 'identifier':
            return declarator.text.decode('utf-8')
        return None

    @staticmethod
    def _cpp_function_linkage(node) -> str:
        """C/C++ function_definition 的最低限度 linkage。"""
        current = node.parent
        inside_class = False
        while current is not None:
            if current.type in {"class_specifier", "struct_specifier"}:
                inside_class = True
            if (current.type == "namespace_definition"
                    and current.child_by_field_name("name") is None):
                return "internal"
            current = current.parent
        for child in node.children:
            if child.type != "storage_class_specifier":
                continue
            value = child.text.decode("utf-8", errors="replace").strip()
            # C++ static member function 仍有 class/namespace linkage；只有
            # namespace/file scope static function 是 translation-unit local。
            if value == "static" and not inside_class:
                return "internal"
        return "external"

    @staticmethod
    def _preprocessor_condition(node) -> Optional[str]:
        """保存 function 所在的 preprocessor branch，不猜實際 build variant。"""
        parts = []
        current = node.parent
        condition_nodes = {
            "preproc_if",
            "preproc_ifdef",
            "preproc_elif",
            "preproc_else",
        }
        while current is not None:
            if current.type in condition_nodes:
                first_line = current.text.decode("utf-8", errors="replace").splitlines()[0]
                normalized = " ".join(first_line.split())[:300]
                if normalized:
                    parts.append(normalized)
            current = current.parent
        return " > ".join(reversed(parts)) or None

    def _extract_go_symbols(self, node, lines: list, symbols: list, parent_name: str = None):
        """提取 Go 符號"""
        node_type = node.type

        if node_type == 'function_declaration':
            name_node = node.child_by_field_name('name')
            if name_node:
                name = name_node.text.decode('utf-8')
                symbols.append(self._make_symbol(node, lines, name, 'function'))

        elif node_type == 'method_declaration':
            name_node = node.child_by_field_name('name')
            receiver = node.child_by_field_name('receiver')
            if name_node:
                name = name_node.text.decode('utf-8')
                recv_name = None
                if receiver:
                    # 取得 receiver 類型名稱
                    recv_name = receiver.text.decode('utf-8')
                symbols.append(self._make_symbol(node, lines, name, 'method', recv_name))

        elif node_type == 'type_declaration':
            for child in node.children:
                if child.type == 'type_spec':
                    name_node = child.child_by_field_name('name')
                    if name_node:
                        name = name_node.text.decode('utf-8')
                        symbols.append(self._make_symbol(child, lines, name, 'type'))

        # 遞迴
        for child in node.children:
            self._extract_go_symbols(child, lines, symbols, parent_name)

    def _extract_rust_symbols(self, node, lines: list, symbols: list, parent_name: str = None):
        """提取 Rust 符號"""
        node_type = node.type

        if node_type == 'function_item':
            name_node = node.child_by_field_name('name')
            if name_node:
                name = name_node.text.decode('utf-8')
                sym_type = 'method' if parent_name else 'function'
                symbols.append(self._make_symbol(node, lines, name, sym_type, parent_name))

        elif node_type == 'struct_item':
            name_node = node.child_by_field_name('name')
            if name_node:
                name = name_node.text.decode('utf-8')
                symbols.append(self._make_symbol(node, lines, name, 'struct'))

        elif node_type == 'enum_item':
            name_node = node.child_by_field_name('name')
            if name_node:
                name = name_node.text.decode('utf-8')
                symbols.append(self._make_symbol(node, lines, name, 'enum'))

        elif node_type == 'impl_item':
            type_node = node.child_by_field_name('type')
            if type_node:
                impl_name = type_node.text.decode('utf-8')
                # 遞迴處理 impl 內的 methods
                body = node.child_by_field_name('body')
                if body:
                    for child in body.children:
                        self._extract_rust_symbols(child, lines, symbols, impl_name)
                return

        elif node_type == 'trait_item':
            name_node = node.child_by_field_name('name')
            if name_node:
                name = name_node.text.decode('utf-8')
                symbols.append(self._make_symbol(node, lines, name, 'trait'))

        elif node_type == 'mod_item':
            name_node = node.child_by_field_name('name')
            if name_node:
                name = name_node.text.decode('utf-8')
                symbols.append(self._make_symbol(node, lines, name, 'module'))

        # 遞迴
        for child in node.children:
            self._extract_rust_symbols(child, lines, symbols, parent_name)

    def _extract_python_symbols(self, node, lines: list, symbols: list, parent_name: str = None):
        """提取 Python 符號（備用，通常用 ast 模組）"""
        node_type = node.type

        if node_type == 'class_definition':
            name_node = node.child_by_field_name('name')
            if name_node:
                name = name_node.text.decode('utf-8')
                symbols.append(self._make_symbol(node, lines, name, 'class'))
                # 遞迴處理 body
                body = node.child_by_field_name('body')
                if body:
                    for child in body.children:
                        self._extract_python_symbols(child, lines, symbols, name)

        elif node_type == 'function_definition':
            name_node = node.child_by_field_name('name')
            if name_node:
                name = name_node.text.decode('utf-8')
                sym_type = 'method' if parent_name else 'function'
                symbols.append(self._make_symbol(node, lines, name, sym_type, parent_name))

        # 遞迴
        for child in node.children:
            if child.type not in ('block',):
                self._extract_python_symbols(child, lines, symbols, parent_name)

    def _make_symbol(self, node, lines: list, name: str, sym_type: str, parent: str = None,
                     qualified_name: str = None, linkage: str = None,
                     condition: str = None, storage_class: str = None,
                     anchor_node=None) -> Symbol:
        """建立 Symbol 物件

        ``anchor_node`` 讓 multi-declarator 的每個 symbol 指到自己的 declarator
        行號,而 context / signature 仍取自整條宣告(``node``)。
        """
        anchor = anchor_node if anchor_node is not None else node
        start_line = anchor.start_point[0] + 1  # 轉為 1-based
        end_line = max(anchor.end_point[0] + 1, start_line)

        # 取得 context:與 _get_context 同一契約 —— 以 end_line 截斷,
        # 短符號的 context 不得吃到下一個符號。
        context_start = node.start_point[0] + 1
        context_stop = max(node.end_point[0] + 1, context_start)
        start_idx = context_start - 1
        max_lines = 15
        context_end = min(start_idx + max_lines, context_stop, len(lines))
        context_end = max(context_end, start_idx + 1)
        context = '\n'.join(lines[start_idx:context_end])

        # 最小 signature(§6.2-3):node 首行起到第一個 '{' 前的文字。
        # 多行 signature(參數跨行)因此也拿得到完整參數列;空白正規化,
        # 防禦性截 300 chars(巨型初始化列表之類)。
        sig_source = ' '.join(lines[start_idx:context_end])
        brace = sig_source.find('{')
        signature = sig_source[:brace] if brace != -1 else sig_source
        signature = ' '.join(signature.split()).strip()[:300] or None

        return Symbol(
            comments=self._leading_comment(node, lines),
            name=name,
            type=sym_type,
            start_line=start_line,
            end_line=end_line,
            context=context,
            parent=parent,
            signature=signature,
            qualified_name=qualified_name,
            linkage=linkage,
            condition=condition,
            storage_class=storage_class,
        )


class GenericParser:
    """未支援副檔名沒有 symbol 表示法；不是缺主要 parser 時的替代。"""

    def parse(self, content: str, filepath: Path) -> list[Symbol]:
        return []


class CtagsParser:
    """Java/Kotlin 的主要 parser：需要支援 JSON 的 Universal Ctags。"""

    def __init__(self, language: str):
        self.language = language

    def require_available(self) -> None:
        try:
            result = process_env.run(
                ['ctags', '--version'], capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0 or 'Universal Ctags' not in result.stdout:
                raise RuntimeError('ctags must be Universal Ctags')
            features = process_env.run(
                ['ctags', '--list-features'], capture_output=True, text=True, timeout=5
            )
            if features.returncode != 0 or not any(
                line.strip().split()[:1] == ['json'] for line in features.stdout.splitlines()
            ):
                raise RuntimeError('Universal Ctags JSON support is unavailable')
            languages = process_env.run(
                ['ctags', '--list-languages'], capture_output=True, text=True, timeout=5
            )
            if languages.returncode != 0 or not any(
                line.lower().split()[:1] == [self.language]
                and '[disabled]' not in line.lower()
                for line in languages.stdout.splitlines()
            ):
                raise RuntimeError(f'Universal Ctags {self.language} parser is unavailable or disabled')
        except Exception as exc:
            raise ParserDependencyError(
                f"Code parser unavailable for {self.language}: {type(exc).__name__}: {exc}. "
                "Install Universal Ctags with JSON support and put ctags on PATH."
            ) from exc

    def parse(self, content: str, filepath: Path) -> list[Symbol]:
        self.require_available()
        import json
        import tempfile

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix=filepath.suffix,
                                             delete=False, encoding='utf-8') as handle:
                handle.write(content)
                temp_path = handle.name
            result = process_env.run(
                ['ctags', '-f', '-', '--output-format=json', '--fields=+n+e', temp_path],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                raise RuntimeError(f"ctags exited {result.returncode}: {result.stderr.strip()}")
            symbols = []
            lines = content.split('\n')
            for line in result.stdout.splitlines():
                if not line.strip():
                    continue
                tag = json.loads(line)
                if not isinstance(tag, dict):
                    raise ValueError('ctags output row must be a JSON object')
                if tag.get('_type') == 'ptag':
                    continue
                kind = tag.get('kind', '')
                sym_type = self._map_kind(kind)
                if not sym_type:
                    continue
                name = tag.get('name')
                line_num = tag.get('line')
                end_line = tag.get('end', line_num)
                if (not isinstance(name, str) or not name
                        or type(line_num) is not int or type(end_line) is not int
                        or not 1 <= line_num <= end_line <= len(lines)):
                    raise ValueError('ctags output has invalid symbol name or source range')
                parent = tag.get('scope')
                if parent is not None and not isinstance(parent, str):
                    raise ValueError('ctags scope must be a string')
                if parent and ':' in parent:
                    parent = parent.split(':')[-1]
                symbols.append(Symbol(
                    name=name, type=sym_type, start_line=line_num, end_line=end_line,
                    context='\n'.join(lines[line_num - 1:min(line_num + 14, len(lines))]),
                    parent=parent, backend='ctags',
                    qualified_name=f"{parent}.{name}" if sym_type == 'method' and parent else name,
                ))
            return symbols
        except Exception as exc:
            raise ParserDependencyError(
                f"Code parser unavailable for {self.language}: {type(exc).__name__}: {exc}. "
                "Check Universal Ctags JSON support and the CodeTrail temporary directory."
            ) from exc
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    @staticmethod
    def _map_kind(kind: str) -> Optional[str]:
        return {
            'class': 'class', 'interface': 'interface', 'method': 'method',
            'function': 'function', 'enum': 'enum', 'constructor': 'method',
        }.get(kind)


_TREE_SITTER_SUFFIXES = {
    '.js': 'javascript', '.jsx': 'javascript', '.ts': 'typescript', '.tsx': 'tsx',
    '.c': 'c', '.cpp': 'cpp', '.cc': 'cpp', '.cxx': 'cpp',
    '.hpp': 'cpp', '.hh': 'cpp', '.hxx': 'cpp', '.go': 'go', '.rs': 'rust',
}


def parser_language(filepath: Path) -> str | None:
    """以副檔名選 parser，無依賴探測或 subprocess。"""
    suffix = filepath.suffix.lower()
    if suffix == '.h':
        return _h_header_language()
    if suffix in ('.py', '.pyx', '.pyi'):
        return 'python'
    if suffix in ('.java', '.kt', '.kts'):
        return 'java' if suffix == '.java' else 'kotlin'
    return _TREE_SITTER_SUFFIXES.get(suffix)


def require_parsers_for_paths(paths, *, build_context=None) -> None:
    """快取與建立入口以語言去重檢查，沒有該語言就不要求它的工具。"""
    languages = {(build_context.parser_language(str(path)) if build_context is not None else None)
                 or parser_language(Path(path)) for path in paths}
    for language in sorted(lang for lang in languages if lang and lang != 'python'):
        if language in ('java', 'kotlin'):
            CtagsParser(language).require_available()
        else:
            require_tree_sitter_parser(language)


def get_parser(filepath: Path):
    """取得該語言唯一的主要 parser；環境不正確直接拋錯。"""
    language = parser_language(filepath)
    if language == 'python':
        return PythonASTParser()
    if language in ('java', 'kotlin'):
        return CtagsParser(language)
    if language is not None:
        return TreeSitterParser(language)
    return GenericParser()


def _h_header_language() -> str:
    """`.h` 的語言判定(§6.2-2):預設 C(firmware 大宗是 C header)。

    client.json 的 `h_lang`(c|cpp)可整體覆寫；非法設定不得換用另一個 grammar。
    """
    import config

    value = str(getattr(config, "H_LANG", "c")).strip().lower()
    if value not in ("c", "cpp"):
        raise ParserDependencyError(
            f"Code parser h_lang setting is invalid: {value!r}; set client.json h_lang to c or cpp."
        )
    return value


def parse_file(filepath: Path, content: str, *, build_context=None) -> list[Symbol]:
    """解析檔案並提取符號"""
    rel_path = None
    if build_context is not None and build_context.restricts_files:
        rel_path = filepath.relative_to(build_context.root).as_posix()
        content = build_context.mask_source(rel_path, content)
    language = build_context.parser_language(rel_path) if rel_path is not None else None
    parser = TreeSitterParser(language) if language in ("c", "cpp") else get_parser(filepath)
    try:
        symbols = parser.parse(content, filepath)
        if rel_path is not None:
            symbols = [symbol for symbol in symbols
                       if build_context.state_for(rel_path, symbol.start_line) != "inactive"]
        return symbols
    except DependencyError:
        raise
    except Exception as exc:
        raise ParserDependencyError(
            f"Code parser unavailable for {parser_language(filepath) or filepath.suffix}: "
            f"{type(exc).__name__}: {exc}. Check the primary parser and its compatible language package."
        ) from exc


# 提供解析器狀態資訊
def get_parser_status() -> dict:
    """逐語言能力探測；缺主要 parser 標 unavailable，實際使用會直接報錯。"""
    languages = {'python': 'python-ast'}
    for lang in ['c', 'cpp', 'javascript', 'typescript', 'tsx', 'go', 'rust']:
        try:
            require_tree_sitter_parser(lang)
        except ParserDependencyError:
            languages[lang] = 'unavailable'
        else:
            languages[lang] = 'tree-sitter'
    for lang in ('java', 'kotlin'):
        languages[lang] = 'unavailable'
        if shutil.which('ctags'):
            try:
                CtagsParser(lang).require_available()
            except ParserDependencyError:
                continue
            languages[lang] = 'ctags'
    return {'has_tree_sitter': HAS_TREE_SITTER, 'languages': languages}
