#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - AST 解析器

使用 AST 而非 regex 來提取程式碼符號，提供更精準的符號範圍和結構。

支援語言：
- Python: 使用內建 ast 模組
- JavaScript/TypeScript: 使用 tree-sitter（若可用）或 fallback 到 regex
- C/C++: 使用 tree-sitter（若可用）或 fallback 到 regex
- Go/Rust: 使用 tree-sitter（若可用）或 fallback 到 regex

安裝可選依賴（提升精準度）：
    pip install tree-sitter tree-sitter-python tree-sitter-javascript \
                tree-sitter-typescript tree-sitter-c tree-sitter-cpp \
                tree-sitter-go tree-sitter-rust
"""

import ast
import os
import re
import shutil
from pathlib import Path
from typing import Optional
from dataclasses import dataclass


_CPP_EXTENSIONS = frozenset({
    '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.hh', '.hxx',
})

# 嘗試導入 tree-sitter
try:
    import tree_sitter
    from tree_sitter import Language, Parser
    HAS_TREE_SITTER = True
except ImportError:
    HAS_TREE_SITTER = False
    tree_sitter = None
    Language = None
    Parser = None

# 嘗試載入各語言的 tree-sitter
_TREE_SITTER_LANGUAGES = {}


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

        _TREE_SITTER_LANGUAGES[lang_name] = lang
        return lang
    except ImportError:
        _TREE_SITTER_LANGUAGES[lang_name] = None
        return None


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
        if self.language and HAS_TREE_SITTER:
            self.parser = Parser(self.language)
        else:
            self.parser = None

    def parse(self, content: str, filepath: Path) -> list[Symbol]:
        """解析程式碼"""
        if not self.parser:
            return []

        try:
            tree = self.parser.parse(bytes(content, 'utf-8'))
        except Exception:
            return []

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

        if node_type in ('class_specifier', 'struct_specifier'):
            name_node = node.child_by_field_name('name')
            if name_node:
                name = name_node.text.decode('utf-8')
                sym_type = 'class' if node_type == 'class_specifier' else 'struct'
                symbols.append(self._make_symbol(
                    node, lines, name, sym_type,
                    qualified_name=f"{qualified_prefix}{name}",
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
            # 匿名 class/struct:自身不成符號,fall through 讓泛型遞迴進 body

        elif node_type == 'function_definition':
            declarator = node.child_by_field_name('declarator')
            if declarator:
                name = self._get_cpp_function_name(declarator)
                if name:
                    sym_type = 'method' if parent_name else 'function'
                    symbols.append(self._make_symbol(
                        node, lines, name, sym_type, parent_name,
                        qualified_name=f"{qualified_prefix}{name}",
                        linkage=self._cpp_function_linkage(node),
                        condition=self._preprocessor_condition(node),
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
                     condition: str = None) -> Symbol:
        """建立 Symbol 物件"""
        start_line = node.start_point[0] + 1  # 轉為 1-based
        end_line = node.end_point[0] + 1

        # 取得 context:與 _get_context 同一契約 —— 以 end_line 截斷,
        # 短符號的 context 不得吃到下一個符號。
        start_idx = start_line - 1
        max_lines = 15
        context_end = min(start_idx + max_lines, end_line, len(lines))
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
        )


class RegexFallbackParser:
    """Regex fallback 解析器（當 tree-sitter 不可用時）

    這是從 code_rag.py 中提取的原始 regex 解析邏輯
    """

    def parse(self, content: str, filepath: Path) -> list[Symbol]:
        """解析程式碼（使用 regex）"""
        symbols = []
        lines = content.split('\n')
        ext = filepath.suffix.lower()

        if ext in ('.py', '.pyx', '.pyi'):
            symbols = self._parse_python(lines)
        elif ext in _CPP_EXTENSIONS:
            symbols = self._parse_cpp(lines)
        elif ext in ('.js', '.ts', '.jsx', '.tsx'):
            symbols = self._parse_js(lines)
        elif ext == '.rs':
            symbols = self._parse_rust(lines)
        elif ext == '.go':
            symbols = self._parse_go(lines)

        sep = "::" if ext in _CPP_EXTENSIONS else "."
        for sym in symbols:
            sym.backend = "regex"
            if sym.qualified_name is None:
                if sym.type == "method" and sym.parent:
                    sym.qualified_name = f"{sym.parent}{sep}{sym.name}"
                else:
                    sym.qualified_name = sym.name
        return symbols

    def _parse_python(self, lines: list) -> list[Symbol]:
        """解析 Python"""
        symbols = []
        pattern = r'^(\s*)(class|def|async\s+def)\s+(\w+)'
        pending_decorator = None
        current_class = None
        class_indent = -1

        for i, line in enumerate(lines):
            if re.match(r'^\s*@\w+', line):
                pending_decorator = i
                continue

            m = re.match(pattern, line)
            if m:
                indent = len(m.group(1))
                keyword = m.group(2)
                name = m.group(3)

                if keyword == 'class':
                    current_class = name
                    class_indent = indent
                    sym_type = 'class'
                    parent = None
                else:
                    if current_class and indent > class_indent:
                        sym_type = 'method'
                        parent = current_class
                    else:
                        sym_type = 'function'
                        parent = None
                        if indent <= class_indent:
                            current_class = None
                            class_indent = -1

                start_line = pending_decorator + 1 if pending_decorator is not None else i + 1
                end_line = self._find_block_end(lines, i)
                context = '\n'.join(lines[start_line-1:min(start_line+14, len(lines))])

                symbols.append(Symbol(
                    name=name,
                    type=sym_type,
                    start_line=start_line,
                    end_line=end_line,
                    context=context,
                    parent=parent
                ))
                pending_decorator = None

        return symbols

    def _parse_cpp(self, lines: list) -> list[Symbol]:
        """解析 C/C++"""
        symbols = []

        class_pattern = r'^(?:template\s*<[^>]*>\s*)?(class|struct)\s+(\w+)'
        namespace_pattern = r'^namespace\s+(\w+)'
        func_pattern = r'^(?:template\s*<[^>]*>\s*)?[\w\s\*\&\<\>\[\]:,]+\s+(?:(\w+)::)?(\w+)\s*\([^;]*\)\s*(?:const|override|noexcept|final|\s)*\{'

        for i, line in enumerate(lines):
            m = re.match(namespace_pattern, line)
            if m:
                symbols.append(Symbol(
                    name=m.group(1),
                    type='namespace',
                    start_line=i + 1,
                    end_line=self._find_brace_end(lines, i),
                    context='\n'.join(lines[i:min(i+15, len(lines))])
                ))
                continue

            m = re.match(class_pattern, line)
            if m:
                sym_type = 'class' if m.group(1) == 'class' else 'struct'
                symbols.append(Symbol(
                    name=m.group(2),
                    type=sym_type,
                    start_line=i + 1,
                    end_line=self._find_brace_end(lines, i),
                    context='\n'.join(lines[i:min(i+15, len(lines))])
                ))
                continue

            m = re.match(func_pattern, line)
            if m:
                func_name = m.group(2)
                parent = m.group(1) if m.group(1) else None
                symbols.append(Symbol(
                    name=func_name,
                    type='method' if parent else 'function',
                    start_line=i + 1,
                    end_line=self._find_brace_end(lines, i),
                    context='\n'.join(lines[i:min(i+15, len(lines))]),
                    parent=parent
                ))

        return symbols

    def _parse_js(self, lines: list) -> list[Symbol]:
        """解析 JavaScript/TypeScript"""
        symbols = []
        patterns = [
            (r'^(?:export\s+)?(?:default\s+)?(class)\s+(\w+)', 'class'),
            (r'^(?:export\s+)?(?:default\s+)?(async\s+)?function\s+(\w+)', 'function'),
            (r'^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>', 'function'),
            (r'^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?function', 'function'),
            (r'^(?:export\s+)?(?:interface|type)\s+(\w+)', 'interface'),
        ]

        for i, line in enumerate(lines):
            for pattern, sym_type in patterns:
                m = re.match(pattern, line)
                if m:
                    name = m.group(m.lastindex)
                    if name in ('if', 'else', 'for', 'while', 'switch', 'catch', 'try', 'finally'):
                        continue
                    symbols.append(Symbol(
                        name=name,
                        type=sym_type,
                        start_line=i + 1,
                        end_line=self._find_brace_end(lines, i),
                        context='\n'.join(lines[i:min(i+15, len(lines))])
                    ))
                    break

        return symbols

    def _parse_rust(self, lines: list) -> list[Symbol]:
        """解析 Rust"""
        symbols = []
        pattern = r'^(\s*)(pub\s+)?(fn|struct|enum|impl|trait|mod)\s+(\w+)'

        for i, line in enumerate(lines):
            m = re.match(pattern, line)
            if m:
                keyword = m.group(3)
                name = m.group(4)
                sym_type = {
                    'fn': 'function',
                    'struct': 'struct',
                    'enum': 'enum',
                    'impl': 'impl',
                    'trait': 'trait',
                    'mod': 'module'
                }.get(keyword, 'function')

                symbols.append(Symbol(
                    name=name,
                    type=sym_type,
                    start_line=i + 1,
                    end_line=self._find_brace_end(lines, i),
                    context='\n'.join(lines[i:min(i+15, len(lines))])
                ))

        return symbols

    def _parse_go(self, lines: list) -> list[Symbol]:
        """解析 Go"""
        symbols = []
        func_pattern = r'^func\s+(?:\([^)]+\)\s+)?(\w+)'
        type_pattern = r'^type\s+(\w+)'

        for i, line in enumerate(lines):
            m = re.match(func_pattern, line)
            if m:
                symbols.append(Symbol(
                    name=m.group(1),
                    type='function',
                    start_line=i + 1,
                    end_line=self._find_brace_end(lines, i),
                    context='\n'.join(lines[i:min(i+15, len(lines))])
                ))
                continue

            m = re.match(type_pattern, line)
            if m:
                symbols.append(Symbol(
                    name=m.group(1),
                    type='type',
                    start_line=i + 1,
                    end_line=self._find_brace_end(lines, i),
                    context='\n'.join(lines[i:min(i+15, len(lines))])
                ))

        return symbols

    def _find_block_end(self, lines: list, start: int) -> int:
        """找 Python 縮排區塊的結尾"""
        if start >= len(lines):
            return start + 1

        base_indent = len(lines[start]) - len(lines[start].lstrip())
        for i in range(start + 1, len(lines)):
            line = lines[i]
            if not line.strip():
                continue
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= base_indent:
                return i
        return len(lines)

    def _find_brace_end(self, lines: list, start: int) -> int:
        """找大括號配對的結尾"""
        depth = 0
        for i in range(start, len(lines)):
            for ch in lines[i]:
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        return i + 1
        return len(lines)


class CtagsFallbackParser:
    """P0 改進：使用 ctags 解析 Java/Kotlin（當 tree-sitter 不可用時）

    需要系統安裝 universal-ctags：
    - macOS: brew install universal-ctags
    - Ubuntu: apt install universal-ctags
    """

    def __init__(self, language: str):
        self.language = language
        self._ctags_available = None

    def _check_ctags_available(self) -> bool:
        """檢查 ctags 是否可用"""
        if self._ctags_available is not None:
            return self._ctags_available

        import subprocess
        try:
            result = subprocess.run(
                ['ctags', '--version'],
                capture_output=True, text=True, timeout=5
            )
            self._ctags_available = result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self._ctags_available = False

        return self._ctags_available

    def parse(self, content: str, filepath: Path) -> list[Symbol]:
        """使用 ctags 解析 Java/Kotlin"""
        if not self._check_ctags_available():
            # Fallback 到 regex
            return self._parse_with_regex(content, filepath)

        import subprocess
        import tempfile

        # 寫入臨時檔案
        ext = filepath.suffix
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix=ext, delete=False, encoding='utf-8') as f:
                f.write(content)
                temp_path = f.name

            # 執行 ctags
            result = subprocess.run(
                ['ctags', '-f', '-', '--output-format=json', '--fields=+n+e', temp_path],
                capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0:
                return self._parse_with_regex(content, filepath)

            # 解析 ctags JSON 輸出
            import json
            symbols = []
            lines = content.split('\n')

            for line in result.stdout.strip().split('\n'):
                if not line:
                    continue
                try:
                    tag = json.loads(line)
                    name = tag.get('name', '')
                    kind = tag.get('kind', '')
                    line_num = tag.get('line', 1)
                    end_line = tag.get('end', line_num)

                    # 映射 ctags kind 到我們的 type
                    sym_type = self._map_kind(kind)
                    if not sym_type:
                        continue

                    # 取得 context
                    start_idx = line_num - 1
                    context_end = min(start_idx + 15, len(lines))
                    context = '\n'.join(lines[start_idx:context_end])

                    # 取得 parent（class/interface）
                    parent = tag.get('scope', None)
                    if parent and ':' in parent:
                        parent = parent.split(':')[-1]

                    symbols.append(Symbol(
                        name=name,
                        type=sym_type,
                        start_line=line_num,
                        end_line=end_line,
                        context=context,
                        parent=parent
                    ))
                except json.JSONDecodeError:
                    continue

            for sym in symbols:
                sym.backend = "ctags"
                if sym.qualified_name is None:
                    if sym.type == "method" and sym.parent:
                        sym.qualified_name = f"{sym.parent}.{sym.name}"
                    else:
                        sym.qualified_name = sym.name
            return symbols

        except Exception:
            return self._parse_with_regex(content, filepath)
        finally:
            import os
            try:
                os.unlink(temp_path)
            except Exception:
                pass

    def _map_kind(self, kind: str) -> Optional[str]:
        """映射 ctags kind 到 Symbol type"""
        kind_map = {
            'class': 'class',
            'interface': 'interface',
            'method': 'method',
            'function': 'function',
            'field': None,  # 不收錄 field
            'variable': None,
            'enum': 'enum',
            'enumConstant': None,
            'constructor': 'method',
        }
        return kind_map.get(kind)

    def _parse_with_regex(self, content: str, filepath: Path) -> list[Symbol]:
        """Regex fallback for Java/Kotlin"""
        symbols = []
        lines = content.split('\n')
        ext = filepath.suffix.lower()

        if ext in ('.java',):
            symbols = self._parse_java(lines)
        elif ext in ('.kt', '.kts'):
            symbols = self._parse_kotlin(lines)

        for sym in symbols:
            sym.backend = "regex"
            if sym.qualified_name is None:
                if sym.type == "method" and sym.parent:
                    sym.qualified_name = f"{sym.parent}.{sym.name}"
                else:
                    sym.qualified_name = sym.name
        return symbols

    def _parse_java(self, lines: list) -> list[Symbol]:
        """解析 Java"""
        symbols = []
        class_pattern = r'^\s*(?:public|private|protected)?\s*(?:static)?\s*(?:final)?\s*(class|interface|enum)\s+(\w+)'
        method_pattern = r'^\s*(?:public|private|protected)?\s*(?:static)?\s*(?:final)?\s*(?:<[^>]+>\s*)?(\w+(?:\[\])?)\s+(\w+)\s*\('
        current_class = None

        for i, line in enumerate(lines):
            m = re.match(class_pattern, line)
            if m:
                sym_type = m.group(1)  # class, interface, or enum
                name = m.group(2)
                current_class = name
                symbols.append(Symbol(
                    name=name,
                    type=sym_type,
                    start_line=i + 1,
                    end_line=self._find_brace_end(lines, i),
                    context='\n'.join(lines[i:min(i+15, len(lines))])
                ))
                continue

            m = re.match(method_pattern, line)
            if m and current_class:
                return_type = m.group(1)
                name = m.group(2)
                # 排除 Java 關鍵字
                if name in ('if', 'else', 'for', 'while', 'switch', 'catch', 'try', 'new', 'return'):
                    continue
                symbols.append(Symbol(
                    name=name,
                    type='method',
                    start_line=i + 1,
                    end_line=self._find_brace_end(lines, i),
                    context='\n'.join(lines[i:min(i+15, len(lines))]),
                    parent=current_class,
                    signature=f"{return_type} {name}(...)"
                ))

        return symbols

    def _parse_kotlin(self, lines: list) -> list[Symbol]:
        """解析 Kotlin"""
        symbols = []
        class_pattern = r'^\s*(?:open|data|sealed|abstract)?\s*(class|interface|object|enum\s+class)\s+(\w+)'
        func_pattern = r'^\s*(?:private|public|internal|protected)?\s*(?:suspend)?\s*fun\s+(?:<[^>]+>\s*)?(\w+)\s*\('

        current_class = None

        for i, line in enumerate(lines):
            m = re.match(class_pattern, line)
            if m:
                kind = m.group(1).split()[0]  # 'class', 'interface', 'object', 'enum'
                name = m.group(2)
                current_class = name
                symbols.append(Symbol(
                    name=name,
                    type=kind if kind != 'object' else 'class',
                    start_line=i + 1,
                    end_line=self._find_brace_end(lines, i),
                    context='\n'.join(lines[i:min(i+15, len(lines))])
                ))
                continue

            m = re.match(func_pattern, line)
            if m:
                name = m.group(1)
                sym_type = 'method' if current_class else 'function'
                symbols.append(Symbol(
                    name=name,
                    type=sym_type,
                    start_line=i + 1,
                    end_line=self._find_brace_end(lines, i),
                    context='\n'.join(lines[i:min(i+15, len(lines))]),
                    parent=current_class
                ))

        return symbols

    def _find_brace_end(self, lines: list, start: int) -> int:
        """找大括號配對的結尾"""
        depth = 0
        for i in range(start, len(lines)):
            for ch in lines[i]:
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        return i + 1
        return len(lines)


def get_parser(filepath: Path):
    """取得適合檔案類型的解析器

    優先使用 AST/tree-sitter，fallback 到 regex/ctags
    """
    ext = filepath.suffix.lower()

    # Python: 使用內建 ast 模組
    if ext in ('.py', '.pyx', '.pyi'):
        return PythonASTParser()

    # Java/Kotlin: 使用 ctags fallback
    if ext in ('.java', '.kt', '.kts'):
        return CtagsFallbackParser('java' if ext == '.java' else 'kotlin')

    # 嘗試使用 tree-sitter
    if HAS_TREE_SITTER:
        lang_map = {
            '.js': 'javascript',
            '.jsx': 'javascript',
            '.ts': 'typescript',
            '.tsx': 'tsx',
            '.c': 'c',
            '.h': _h_header_language(),
            '.cpp': 'cpp',
            '.cc': 'cpp',
            '.cxx': 'cpp',
            '.hpp': 'cpp',
            '.hh': 'cpp',
            '.hxx': 'cpp',
            '.go': 'go',
            '.rs': 'rust',
        }
        lang = lang_map.get(ext)
        if lang:
            ts_lang = _try_load_tree_sitter_language(lang)
            if ts_lang:
                return TreeSitterParser(lang)

    # Fallback 到 regex
    return RegexFallbackParser()


def _h_header_language() -> str:
    """`.h` 的語言判定(§6.2-2):預設 C(firmware 大宗是 C header)。

    env AICODE_H_LANG=c|cpp 可整體覆寫;非法值靜默回 c(header 判定不值得
    fail-loud,錯了頂多少抽 C++ 特有結構)。鄰檔推斷屬 Phase B。
    """
    value = os.environ.get("AICODE_H_LANG", "c").strip().lower()
    return value if value in ("c", "cpp") else "c"


def parse_file(filepath: Path, content: str) -> list[Symbol]:
    """解析檔案並提取符號"""
    parser = get_parser(filepath)
    return parser.parse(content, filepath)


# 提供解析器狀態資訊
def get_parser_status() -> dict:
    """逐語言的實際 parser backend(§6.2-4,誠實化)。

    - python 恆為 "python-ast"(stdlib ast;裝不裝 tree-sitter 都一樣,
      不得誤報 regex)。
    - tree-sitter 語言:core + 該語言 grammar 都在才是 "tree-sitter",
      否則 "regex-degraded"(doctor / build_index WARN / evidence 模式都吃
      這個字串)。
    - java/kotlin:探測 ctags 是否在 PATH → "ctags" | "regex-degraded"。
    """
    languages = {'python': 'python-ast'}
    for lang in ['c', 'cpp', 'javascript', 'typescript', 'tsx', 'go', 'rust']:
        ts_lang = _try_load_tree_sitter_language(lang) if HAS_TREE_SITTER else None
        languages[lang] = 'tree-sitter' if ts_lang else 'regex-degraded'
    ctags_backend = 'ctags' if shutil.which('ctags') else 'regex-degraded'
    languages['java'] = ctags_backend
    languages['kotlin'] = ctags_backend

    return {
        'has_tree_sitter': HAS_TREE_SITTER,
        'languages': languages,
    }
