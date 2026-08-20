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


@pytest.mark.smoke
def test_struct_reference_is_not_a_definition():
    """BUG REGRESSION:型別**引用**與 forward tag 被當成 struct definition。

    重現(施工規格 §3 洞 1b):

        struct nowhere_defined_ops;
        extern struct nowhere_defined_ops g_ops;

    修正前輸出 2 個 symbol,兩個都是 `struct nowhere_defined_ops` —— 一個來自
    forward tag declaration,一個來自 extern 宣告裡的型別引用。兩者都不是定義,
    而真正該被記錄的 file-scope 物件反而完全沒抽到。假定義是 precision 債:
    檢索會把「這個 struct 定義在這裡」的錯誤證據餵給模型。
    """
    forward_only = (
        "struct nowhere_defined_ops;\n"
        "extern struct nowhere_defined_ops g_ops;\n"
    )
    assert _parse("a.c", forward_only) == [], (
        "forward tag declaration 與 extern 宣告都不產生定義"
    )

    pointer_object = (
        "struct referenced_ops;\n"
        "static const struct referenced_ops *table;\n"
    )
    symbols = _parse("b.c", pointer_object)
    assert [(sym.type, sym.name) for sym in symbols] == [("global", "table")], (
        "型別引用不是 struct definition;真正的 file-scope 物件 table 才是"
    )
    assert symbols[0].linkage == "internal"


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


# ============================================================
# C/C++ definition 語意(施工規格 §6 P2-2)
# 全部 NEW SILENT CONTRACT:壞掉不會有紅字,只會是索引裡少了東西 /
# 多了假定義,而檢索品質的退步查不到源頭。
# ============================================================
@pytest.mark.smoke
def test_firmware_top_level_entities_are_all_indexed():
    """韌體 C 檔的暫存器位址、timeout、feature gate、狀態機、ops table、全域旗標。

    修正前 11 個 top-level 實體只有 2 個進得了索引(而且那 2 個裡還有一個是
    假的 struct 定義)。掉的正好是韌體 repo 最需要被檢索到的那一類。
    """
    content = (
        "#define UART_BASE_ADDR 0x40001000\n"
        "#define WDT_TIMEOUT_MS 500\n"
        "#define LOG_ERR(fmt) log_write(2, fmt)\n"
        "typedef struct { volatile uint32_t dr; } uart_regs_t;\n"
        "typedef enum { STATE_IDLE = 0, STATE_BUSY = 1 } link_state_t;\n"
        "struct driver_ops;\n"
        "static const struct driver_ops uart_ops = {0};\n"
        "uint32_t g_error_counter;\n"
        "void uart_init(void) { }\n"
        "int uart_send(const char *b) { return 0; }\n"
    )
    found = {(sym.type, sym.name) for sym in _parse("fw.c", content)}
    assert found == {
        ("macro", "UART_BASE_ADDR"),
        ("macro", "WDT_TIMEOUT_MS"),
        ("macro_function", "LOG_ERR"),
        ("typedef", "uart_regs_t"),
        ("typedef", "link_state_t"),
        ("enum", "link_state_t"),
        ("enum_constant", "STATE_IDLE"),
        ("enum_constant", "STATE_BUSY"),
        ("global", "uart_ops"),
        ("global", "g_error_counter"),
        ("function", "uart_init"),
        ("function", "uart_send"),
    }


@pytest.mark.smoke
def test_multi_declarator_globals_and_typedefs_each_produce_a_symbol():
    """一條宣告有多個 declarator 時要逐一產生 symbol,不是只取第一個。"""
    symbols = _parse(
        "multi.c",
        "typedef int count_t, *count_ptr_t;\n"
        "static int a, *b, arr[4];\n",
    )
    assert [(sym.type, sym.name) for sym in symbols] == [
        ("typedef", "count_t"),
        ("typedef", "count_ptr_t"),
        ("global", "a"),
        ("global", "b"),
        ("global", "arr"),
    ]
    assert all(sym.linkage == "internal" for sym in symbols if sym.type == "global")


@pytest.mark.smoke
def test_extern_without_initializer_is_declaration_but_with_one_is_definition():
    """C 的 tentative definition / 純宣告 / extern+initializer 三者要分得開。"""
    symbols = _parse(
        "linkage.c",
        "uint32_t g_error_counter;\n"      # tentative definition
        "extern int only_declared;\n"      # 純宣告
        "extern int defined_here = 1;\n",  # 有 initializer:是定義
    )
    assert [(sym.type, sym.name) for sym in symbols] == [
        ("global", "g_error_counter"),
        ("global", "defined_here"),
    ]
    assert symbols[0].linkage == "external"
    assert symbols[1].storage_class == "extern"


@pytest.mark.smoke
def test_anonymous_typedef_enum_uses_the_alias_and_parents_its_enumerators():
    """匿名 enum 不假設 AST 有 enum name;有 typedef alias 就用 alias。"""
    symbols = _parse("e.c", "typedef enum { IDLE, BUSY } state_t;\n")
    kinds = {(sym.type, sym.name, sym.parent) for sym in symbols}
    assert kinds == {
        ("typedef", "state_t", None),
        ("enum", "state_t", None),
        ("enum_constant", "IDLE", "state_t"),
        ("enum_constant", "BUSY", "state_t"),
    }

    # 完全匿名(沒有 alias)時不得造假名字,enumerator 的 parent 明確是 None。
    bare = _parse("e2.c", "enum { LONE_A, LONE_B };\n")
    assert [(sym.type, sym.name, sym.parent) for sym in bare] == [
        ("enum_constant", "LONE_A", None),
        ("enum_constant", "LONE_B", None),
    ]


@pytest.mark.smoke
def test_struct_and_class_need_a_body_to_be_a_type_definition():
    """沒有 field_declaration_list 就不是型別定義,只是 forward tag 或引用。"""
    assert _parse("s.c", "struct opaque_t;\nunion other_u;\nenum color_e;\n") == []
    with_body = _parse("s2.c", "struct with_body { int x; };\n")
    assert [(sym.type, sym.name) for sym in with_body] == [("struct", "with_body")]


@pytest.mark.smoke
def test_prototypes_members_locals_and_parameters_are_excluded():
    """只處理 translation-unit / namespace scope 的定義。"""
    symbols = _parse(
        "scope.c",
        "int prototype_only(int x);\n"
        "struct holder { int member_field; };\n"
        "void fn(int param) { int local_var; static int local_static; }\n",
    )
    names = {sym.name for sym in symbols}
    assert names == {"holder", "fn"}
    for excluded in ("prototype_only", "member_field", "param",
                     "local_var", "local_static"):
        assert excluded not in names


@pytest.mark.smoke
def test_function_pointer_object_is_a_global_not_a_prototype():
    """`int (*handler)(int);` 是物件定義。韌體的 ops table / callback slot 靠這條。"""
    symbols = _parse(
        "ops.c",
        "static int (*handler)(int);\n"
        "static int (*const ops_table[4])(void);\n",
    )
    assert [(sym.type, sym.name) for sym in symbols] == [
        ("global", "handler"),
        ("global", "ops_table"),
    ]


@pytest.mark.smoke
def test_cpp_object_linkage_narrows_instead_of_guessing_external():
    """C++ namespace-scope const 是 internal;證明不了的標 unknown,不猜 external。"""
    symbols = {
        sym.name: sym
        for sym in _parse(
            "obj.cpp",
            "const int kInternalConst = 5;\n"
            "constexpr int kInternalConstexpr = 6;\n"
            "extern const int kExternalConst = 7;\n"
            "inline int kInlineVar = 8;\n"
            "int kPlainExternal = 9;\n"
            "volatile const int kVolatileConst = 11;\n"
            "thread_local int kThreadLocal = 12;\n"
            "namespace { int hidden_obj = 1; }\n",
        )
    }
    assert symbols["kInternalConst"].linkage == "internal"
    assert symbols["kInternalConstexpr"].linkage == "internal"
    assert symbols["kExternalConst"].linkage == "external"
    assert symbols["kInlineVar"].linkage == "external"
    assert symbols["kPlainExternal"].linkage == "external"
    # volatile 讓 const-implies-internal 的規則失效。
    assert symbols["kVolatileConst"].linkage == "external"
    assert symbols["kThreadLocal"].linkage == "unknown"
    assert symbols["hidden_obj"].linkage == "internal"


@pytest.mark.smoke
def test_preprocessor_condition_is_kept_for_new_definition_kinds():
    """#if 兩臂的同名 macro / global 都要保留各自的 condition,不得合併或丟失。"""
    symbols = _parse(
        "variant.c",
        "#if defined(BOARD_ALPHA)\n"
        "#define TIMEOUT_MS 100\n"
        "static int variant_flag = 1;\n"
        "#else\n"
        "#define TIMEOUT_MS 200\n"
        "static int variant_flag = 2;\n"
        "#endif\n",
    )
    conditions = [(sym.type, sym.name, sym.condition) for sym in symbols]
    assert conditions == [
        ("macro", "TIMEOUT_MS", "#if defined(BOARD_ALPHA)"),
        ("global", "variant_flag", "#if defined(BOARD_ALPHA)"),
        ("macro", "TIMEOUT_MS", "#if defined(BOARD_ALPHA) > #else"),
        ("global", "variant_flag", "#if defined(BOARD_ALPHA) > #else"),
    ]


@pytest.mark.smoke
def test_scoped_enum_enumerators_are_qualified_by_their_enum():
    """BUG REGRESSION:`enum class` 的 enumerator 少了 enum scope 前綴。

    C++ scoped enum 的 enumerator 是 `State::Idle`,不是 `Idle`。固定產生裸名的話,
    兩個 scoped enum 只要有同名 enumerator 就會撞成同一個 qualified name ——
    graph 的 stable node ID 依賴 qualified_name,撞名等於查找結果不準。
    unscoped enum 相反:enumerator 本來就在外層 scope,維持裸名才對。
    """
    symbols = _parse(
        "enums.cpp",
        "enum class State { Idle, Busy };\n"
        "enum struct Mode { Idle, Fast };\n"
        "enum Plain { PlainA };\n",
    )
    qualified = {
        (sym.name, sym.parent): sym.qualified_name
        for sym in symbols if sym.type == "enum_constant"
    }
    assert qualified[("Idle", "State")] == "State::Idle"
    assert qualified[("Idle", "Mode")] == "Mode::Idle"
    assert qualified[("Idle", "State")] != qualified[("Idle", "Mode")], (
        "兩個 scoped enum 的同名 enumerator 不得撞成同一個 qualified name"
    )
    # unscoped enum:enumerator 在外層 scope,不加前綴。
    assert qualified[("PlainA", "Plain")] == "PlainA"
