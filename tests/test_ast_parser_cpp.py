#!/usr/bin/env python3
"""C/C++ tree-sitter parser golden tests(§6.3)。

需要 tree-sitter + tree-sitter-c + tree-sitter-cpp(requirements.txt 已釘版);
沒裝時整檔 skip —— 但 doctor / build_index 會把 degraded 顯式標出來,
不是安靜跳過。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ast_parser  # noqa: E402
from ast_parser import get_parser_status, parse_file  # noqa: E402

pytestmark = pytest.mark.skipif(
    not ast_parser.HAS_TREE_SITTER
    or not ast_parser._try_load_tree_sitter_language("c")
    or not ast_parser._try_load_tree_sitter_language("cpp"),
    reason="tree-sitter c/cpp 未安裝(requirements.txt 有釘版)",
)


def _parse(name: str, content: str):
    return parse_file(Path(name), content)


# ============================================================
# 多行 signature(regex 抓不到的核心場景)
# ============================================================
def test_multiline_signature_c_function_is_extracted():
    content = (
        "#include <stdint.h>\n"
        "\n"
        "int drain_pending(\n"
        "    uint32_t max_events,\n"
        "    uint32_t *processed_out)\n"
        "{\n"
        "    return 0;\n"
        "}\n"
    )
    symbols = _parse("m.c", content)
    names = [s.name for s in symbols]
    assert names == ["drain_pending"]
    sym = symbols[0]
    assert sym.start_line == 3
    assert sym.end_line == 8
    assert sym.backend == "tree-sitter"
    # 最小 signature(§6.2-3):首行到 '{' 前,含完整參數列
    assert sym.signature is not None
    assert "max_events" in sym.signature and "processed_out" in sym.signature
    assert "{" not in sym.signature


# ============================================================
# nested namespace/class:重複 node rate = 0(§6.2-1 回歸)
# ============================================================
def test_nested_class_methods_are_not_duplicated():
    content = (
        "namespace outer {\n"
        "class Widget {\n"
        "public:\n"
        "    int area() {\n"
        "        return 4;\n"
        "    }\n"
        "    int perimeter() {\n"
        "        return 8;\n"
        "    }\n"
        "};\n"
        "}\n"
    )
    symbols = _parse("m.cpp", content)
    from collections import Counter

    counts = Counter((s.name, s.type) for s in symbols)
    dup = {k: v for k, v in counts.items() if v > 1}
    assert not dup, f"重複 node(雙重遞迴回歸): {dup}"
    by_name = {s.name: s for s in symbols}
    assert by_name["area"].type == "method"
    assert by_name["area"].parent == "Widget"
    assert by_name["perimeter"].type == "method"
    # method 不得再以 'function' 身分多出一份
    assert sum(1 for s in symbols if s.name == "area") == 1


def test_qualified_name_chain_namespace_class_method():
    content = (
        "namespace ns {\n"
        "namespace inner {\n"
        "class A {\n"
        "public:\n"
        "    void run() {\n"
        "    }\n"
        "};\n"
        "}\n"
        "}\n"
    )
    symbols = _parse("m.cpp", content)
    by_name = {s.name: s for s in symbols}
    assert by_name["ns"].qualified_name == "ns"
    assert by_name["inner"].qualified_name == "ns::inner"
    assert by_name["A"].qualified_name == "ns::inner::A"
    assert by_name["run"].qualified_name == "ns::inner::A::run"
    assert by_name["run"].parent == "A", "parent 維持 immediate parent,不是全鏈"


# ============================================================
# template / duplicate decl+def / malformed / UTF-8 / node range
# ============================================================
def test_basic_template_function_and_class():
    content = (
        "template <typename T>\n"
        "T biggest(T a, T b) {\n"
        "    return a > b ? a : b;\n"
        "}\n"
        "\n"
        "template <class U>\n"
        "class Holder {\n"
        "public:\n"
        "    U get() {\n"
        "        return value;\n"
        "    }\n"
        "    U value;\n"
        "};\n"
    )
    symbols = _parse("m.cpp", content)
    names = {s.name for s in symbols}
    assert "biggest" in names
    assert "Holder" in names
    assert "get" in names
    from collections import Counter

    counts = Counter((s.name, s.type) for s in symbols)
    assert not {k: v for k, v in counts.items() if v > 1}, "template 內不得有重複 node"


def test_declaration_is_not_a_definition():
    content = (
        "int compute(int a);\n"          # 宣告:不抽
        "\n"
        "int compute(int a) {\n"          # 定義:抽
        "    return a * 2;\n"
        "}\n"
    )
    symbols = _parse("m.c", content)
    computes = [s for s in symbols if s.name == "compute"]
    assert len(computes) == 1, "宣告與定義只有定義入索引"
    assert computes[0].start_line == 3


def test_c_definition_linkage_and_preprocessor_condition_are_preserved():
    content = (
        "static int local_helper(void) { return 1; }\n"
        "#if defined(BOARD_ALPHA)\n"
        "int variant_init(void) { return 2; }\n"
        "#else\n"
        "int variant_init(void) { return 3; }\n"
        "#endif\n"
    )
    symbols = _parse("variant.c", content)
    local = next(sym for sym in symbols if sym.name == "local_helper")
    variants = [sym for sym in symbols if sym.name == "variant_init"]

    assert local.linkage == "internal"
    assert local.condition is None
    assert len(variants) == 2
    assert all(sym.linkage == "external" for sym in variants)
    assert variants[0].condition == "#if defined(BOARD_ALPHA)"
    assert variants[1].condition == "#if defined(BOARD_ALPHA) > #else"


def test_cpp_static_member_is_not_translation_unit_internal():
    symbols = _parse(
        "member.cpp",
        "class Device { public: static int ready(void) { return 1; } };\n",
    )
    ready = next(sym for sym in symbols if sym.name == "ready")
    assert ready.linkage == "external"


def test_cpp_anonymous_namespace_function_is_internal():
    symbols = _parse(
        "anonymous.cpp",
        "namespace { int hidden(void) { return 1; } }\n",
    )
    hidden = next(sym for sym in symbols if sym.name == "hidden")
    assert hidden.linkage == "internal"


def test_malformed_source_does_not_crash_and_extracts_best_effort():
    content = (
        "int ok_before(void) {\n"
        "    return 1;\n"
        "}\n"
        "int broken(int a, {\n"           # malformed
        "\n"
        "int ok_after(void) {\n"
        "    return 2;\n"
        "}\n"
    )
    symbols = _parse("m.c", content)  # 不得 raise
    names = {s.name for s in symbols}
    assert "ok_before" in names, "malformed 段落不得毀掉整檔抽取"


def test_utf8_identifier_and_comments():
    content = (
        "// 初始化佇列(中文註解)\n"
        "int init_queue_模組(void) {\n"
        "    return 0;\n"
        "}\n"
    )
    symbols = _parse("m.c", content)  # 不得 raise;identifier 抽不抽到皆可接受
    for s in symbols:
        assert isinstance(s.name, str)


def test_overloads_are_both_extracted_with_same_qualified_name():
    content = (
        "int scale(int v) {\n"
        "    return v;\n"
        "}\n"
        "float scale(float v) {\n"
        "    return v;\n"
        "}\n"
    )
    symbols = _parse("m.cpp", content)
    overloads = [s for s in symbols if s.name == "scale"]
    assert len(overloads) == 2, "overload 兩個定義都要抽出(stable ID 消歧在 graph 層)"
    assert {s.qualified_name for s in overloads} == {"scale"}
    sigs = {s.signature for s in overloads}
    assert len(sigs) == 2, "兩個 overload 的 signature 必須不同(ID tie-break 依賴它)"


@pytest.mark.parametrize("extension", [".hh", ".hxx"])
def test_cpp_header_extensions_use_cpp_tree_sitter(extension):
    symbols = _parse(
        f"registers{extension}",
        "namespace device { inline int read_status(void) { return 7; } }\n",
    )
    [symbol] = [row for row in symbols if row.name == "read_status"]
    assert symbol.backend == "tree-sitter"
    assert symbol.qualified_name == "device::read_status"


def test_every_node_range_is_within_file_bounds():
    content = (
        "namespace n {\n"
        "class C {\n"
        "public:\n"
        "    void m() {\n"
        "    }\n"
        "};\n"
        "}\n"
        "int f(\n"
        "    int a)\n"
        "{\n"
        "    return a;\n"
        "}\n"
    )
    total_lines = content.count("\n") + 1
    for sym in _parse("m.cpp", content):
        assert 1 <= sym.start_line <= sym.end_line <= total_lines, (
            f"{sym.name}: range {sym.start_line}-{sym.end_line} 超出檔案 {total_lines} 行"
        )


# ============================================================
# .h 語言判定(§6.2-2)與 parser status 誠實化(§6.2-4)
# ============================================================
def test_h_defaults_to_c_and_env_overrides(monkeypatch):
    monkeypatch.delenv("AICODE_H_LANG", raising=False)
    parser = ast_parser.get_parser(Path("x.h"))
    assert isinstance(parser, ast_parser.TreeSitterParser)
    assert parser.language_name == "c"

    monkeypatch.setenv("AICODE_H_LANG", "cpp")
    parser = ast_parser.get_parser(Path("x.h"))
    assert parser.language_name == "cpp"

    monkeypatch.setenv("AICODE_H_LANG", "bogus")
    parser = ast_parser.get_parser(Path("x.h"))
    assert parser.language_name == "c", "非法值回預設 c"


def test_parser_status_reports_python_ast_and_ts_backends():
    status = get_parser_status()
    languages = status["languages"]
    assert languages["python"] == "python-ast", "python 恆為 stdlib ast,不受 tree-sitter 影響"
    assert languages["c"] == "tree-sitter"
    assert languages["cpp"] == "tree-sitter"
    # 沒裝 grammar 的語言必須顯式標 degraded,不是報 'regex' 這種中性詞
    for lang in ("go", "rust"):
        assert languages[lang] in ("tree-sitter", "regex-degraded")
    assert languages["java"] in ("ctags", "regex-degraded")
