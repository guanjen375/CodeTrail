#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C/C++ conservative graph resolution and rebuild-equivalence tests."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ast_parser  # noqa: E402
import code_rag  # noqa: E402
from code_graph import CodeGraph  # noqa: E402

pytestmark = pytest.mark.skipif(
    not ast_parser.HAS_TREE_SITTER
    or not ast_parser._try_load_tree_sitter_language("c")
    or not ast_parser._try_load_tree_sitter_language("cpp"),
    reason="tree-sitter c/cpp 未安裝",
)


@pytest.fixture(autouse=True)
def _fresh_scan_cache():
    code_rag._INDEX_SCAN_CACHE.clear()
    yield
    code_rag._INDEX_SCAN_CACHE.clear()


def _write(root: Path, rel_path: str, content: str) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _source_edges(graph: CodeGraph, path: str, symbol: str) -> list[dict]:
    [node] = [n for n in graph.find_nodes(symbol) if n["path"] == path]
    return [
        edge
        for edge in graph.neighbors(
            node["id"], edge_types=("calls",), direction="out", hops=1, limit=100
        )["edges"]
        if edge["src_id"] == node["id"]
    ]


def test_visible_declaration_uses_transitive_and_repo_angle_include(tmp_path):
    _write(
        tmp_path,
        "include/fw/service.h",
        "#ifndef FW_SERVICE_H\n"
        "#define FW_SERVICE_H\n"
        "int service_commit(unsigned generation);\n"
        "#endif\n",
    )
    _write(
        tmp_path,
        "include/fw/facade.h",
        "#ifndef FW_FACADE_H\n"
        "#define FW_FACADE_H\n"
        '#include "fw/service.h"\n'
        "#endif\n",
    )
    _write(
        tmp_path,
        "src/service.c",
        '#include "fw/service.h"\n'
        "int service_commit(unsigned generation) { return (int)generation; }\n",
    )
    _write(
        tmp_path,
        "src/direct_user.c",
        "#include <fw/service.h>\n"
        "int direct_flush(void) { return service_commit(7u); }\n",
    )
    _write(
        tmp_path,
        "src/transitive_user.c",
        '#include "fw/facade.h"\n'
        "int transitive_flush(void) { return service_commit(8u); }\n",
    )

    graph = CodeGraph(str(tmp_path))
    graph.build()

    assert graph.file_includes("src/direct_user.c") == ["include/fw/service.h"]
    for path, symbol in (
        ("src/direct_user.c", "direct_flush"),
        ("src/transitive_user.c", "transitive_flush"),
    ):
        [edge] = _source_edges(graph, path, symbol)
        assert edge["resolved"] is True
        assert edge["dst_name"] == "service_commit"
        assert edge["resolution_basis"] == "visible_declaration"

    [definition] = [
        node for node in graph.find_nodes("service_commit")
        if node["path"] == "src/service.c"
    ]
    assert definition["linkage"] == "external"

    conn = sqlite3.connect(graph.db_file)
    declaration = conn.execute(
        "SELECT linkage, condition FROM declarations"
        " WHERE path='include/fw/service.h' AND name='service_commit'"
    ).fetchone()
    angle_basis = conn.execute(
        "SELECT resolution_basis FROM edges"
        " WHERE src_id='src/direct_user.c' AND type='includes'"
    ).fetchone()
    conn.close()
    assert declaration == ("external", "#ifndef FW_SERVICE_H")
    assert angle_basis == ("unique_repo_angle_include",)


def test_static_condition_macro_and_callback_stay_conservative(tmp_path):
    _write(
        tmp_path,
        "src/a.c",
        "static int init(void) { return 1; }\n"
        "int boot_a(void) { return init(); }\n",
    )
    _write(
        tmp_path,
        "src/b.c",
        "static int init(void) { return 2; }\n"
        "int boot_b(void) { return init(); }\n",
    )
    _write(tmp_path, "src/rogue.c", "int rogue(void) { return init(); }\n")
    _write(
        tmp_path,
        "src/variant.c",
        "#if defined(BOARD_ALPHA)\n"
        "int variant_init(void) { return 1; }\n"
        "#else\n"
        "int variant_init(void) { return 2; }\n"
        "#endif\n"
        "int variant_boot(void) { return variant_init(); }\n",
    )
    _write(
        tmp_path,
        "src/indirect.c",
        "typedef int (*callback_t)(void);\n"
        "#define RUN_CALLBACK(cb) cb()\n"
        "int callback_entry(callback_t callback) { return callback(); }\n"
        "int macro_entry(callback_t callback) { return RUN_CALLBACK(callback); }\n",
    )
    # Deliberate lexical decoys: repo-global uniqueness must not create a C call edge.
    _write(
        tmp_path,
        "src/decoys.c",
        "int callback(void) { return 9; }\n"
        "int RUN_CALLBACK(callback_t callback) { return callback(); }\n",
    )

    graph = CodeGraph(str(tmp_path))
    graph.build()

    for path, source in (("src/a.c", "boot_a"), ("src/b.c", "boot_b")):
        [edge] = _source_edges(graph, path, source)
        assert edge["resolved"] is True
        assert edge["dst_name"] == "init"
        assert edge["resolution_basis"] == "same_file"

    [rogue] = _source_edges(graph, "src/rogue.c", "rogue")
    assert rogue["resolved"] is False
    assert rogue["unresolved_target"] == "init"
    assert rogue["resolution_basis"] == "syntactic_only"

    variant_edges = _source_edges(graph, "src/variant.c", "variant_boot")
    assert len(variant_edges) == 2
    assert all(not edge["resolved"] for edge in variant_edges)
    assert {edge["resolution_basis"] for edge in variant_edges} == {
        "ambiguous_condition"
    }
    assert len({edge["ambiguity_group"] for edge in variant_edges}) == 1
    assert graph.shortest_evidence_paths({"variant_boot"}, {"variant_init"}) == []
    assert len({
        node["condition"] for node in graph.find_nodes("variant_init")
    }) == 2

    [callback] = _source_edges(graph, "src/indirect.c", "callback_entry")
    [macro] = _source_edges(graph, "src/indirect.c", "macro_entry")
    assert (callback["resolved"], callback["unresolved_target"]) == (False, "callback")
    assert (macro["resolved"], macro["unresolved_target"]) == (False, "RUN_CALLBACK")


def test_cpp_exact_qualified_name_is_a_conservative_fallback(tmp_path):
    _write(
        tmp_path,
        "impl.cpp",
        "namespace service { int commit(void) { return 1; } }\n",
    )
    _write(
        tmp_path,
        "user.cpp",
        "int qualified_user(void) { return service::commit(); }\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "user.cpp", "qualified_user")
    assert edge["resolved"] is True
    assert edge["dst_name"] == "commit"
    assert edge["resolution_basis"] == "qualified"


def test_cpp_global_qualified_call_resolves_only_the_global_definition(tmp_path):
    _write(tmp_path, "impl.cpp", "int helper(void) { return 1; }\n")
    _write(
        tmp_path,
        "user.cpp",
        "namespace decoy { int helper(void) { return 2; } }\n"
        "int run(void) { return ::helper(); }\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "user.cpp", "run")
    assert edge["resolved"] is True
    assert edge["dst_id"] in {
        node["id"] for node in graph.find_nodes("helper")
        if node["path"] == "impl.cpp"
    }
    assert edge["resolution_basis"] == "qualified"


def test_cpp_unconditional_qualified_overloads_are_qualified_ambiguity(tmp_path):
    _write(
        tmp_path,
        "impl.cpp",
        "namespace service {\n"
        "int helper(int value) { return value; }\n"
        "int helper(double value) { return (int)value; }\n"
        "}\n",
    )
    _write(
        tmp_path,
        "user.cpp",
        "int run(void) { return service::helper(1); }\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    edges = _source_edges(graph, "user.cpp", "run")
    assert len(edges) == 2
    assert all(edge["resolved"] is False for edge in edges)
    assert {edge["resolution_basis"] for edge in edges} == {
        "ambiguous_qualified"
    }
    assert len({edge["ambiguity_group"] for edge in edges}) == 1


def test_cpp_bare_call_does_not_resolve_to_unrelated_method(tmp_path):
    _write(
        tmp_path,
        "same.cpp",
        "class Device { public: static int reset(void) { return 1; } };\n"
        "int boot(void) { return reset(); }\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "same.cpp", "boot")
    assert edge["resolved"] is False
    assert edge["unresolved_target"] == "reset"


def test_cpp_bare_call_within_same_class_still_resolves_method(tmp_path):
    _write(
        tmp_path,
        "same.cpp",
        "class Device { public:\n"
        "  static int reset(void) { return 1; }\n"
        "  static int boot(void) { return reset(); }\n"
        "};\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "same.cpp", "boot")
    assert edge["resolved"] is True
    assert edge["dst_name"] == "reset"
    assert edge["resolution_basis"] == "same_file"


def test_cpp_method_can_resolve_visible_global_function(tmp_path):
    _write(tmp_path, "api.h", "int global_reset(void);\n")
    _write(tmp_path, "impl.cpp", "int global_reset(void) { return 1; }\n")
    _write(
        tmp_path,
        "user.cpp",
        '#include "api.h"\n'
        "class Device { public: static int boot(void) { return global_reset(); } };\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "user.cpp", "boot")
    assert edge["resolved"] is True
    assert edge["dst_name"] == "global_reset"
    assert edge["resolution_basis"] == "visible_declaration"


def test_cpp_qualified_call_is_not_consumed_by_bare_declaration(tmp_path):
    _write(tmp_path, "api.h", "int commit(void);\n")
    _write(tmp_path, "impl.cpp", "int commit(void) { return 1; }\n")
    _write(
        tmp_path,
        "user.cpp",
        '#include "api.h"\nint run(void) { return service::commit(); }\n',
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "user.cpp", "run")
    assert edge["resolved"] is False
    assert edge["unresolved_target"] == "service::commit"


def test_bare_angle_include_does_not_resolve_to_vendored_shim(tmp_path):
    _write(tmp_path, "include/stdint.h", "typedef unsigned fake_uint32_t;\n")
    _write(tmp_path, "src/user.c", "#include <stdint.h>\nint run(void) { return 0; }\n")
    graph = CodeGraph(str(tmp_path))
    graph.build()

    assert graph.file_includes("src/user.c") == []
    rows = graph.file_neighbors("src/user.c", limit=20)["edges"]
    assert not any(edge["resolved"] for edge in rows)


def test_bare_angle_include_with_multiple_repo_candidates_stays_explicit(tmp_path):
    _write(tmp_path, "board_a/platform.h", "int board_a(void);\n")
    _write(tmp_path, "board_b/platform.h", "int board_b(void);\n")
    _write(tmp_path, "src/user.c", "#include <platform.h>\nint run(void) { return 0; }\n")
    graph = CodeGraph(str(tmp_path))
    graph.build()

    include_edges = [
        edge for edge in graph.file_neighbors("src/user.c", limit=20)["edges"]
        if edge["type"] == "includes"
    ]
    assert len(include_edges) == 2
    assert {edge["dst_id"] for edge in include_edges} == {
        "board_a/platform.h", "board_b/platform.h"
    }
    assert {edge["resolution_basis"] for edge in include_edges} == {
        "ambiguous_include"
    }
    assert all(edge["resolved"] is False for edge in include_edges)
    assert len({edge["ambiguity_group"] for edge in include_edges}) == 1


def test_absolute_angle_include_cannot_suffix_match_a_vendored_header(tmp_path):
    _write(tmp_path, "repo_headers/usr/include/platform.h", "int fake_platform(void);\n")
    _write(
        tmp_path,
        "src/user.c",
        "#include </usr/include/platform.h>\nint run(void) { return 0; }\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    assert graph.file_includes("src/user.c") == []
    assert not [
        edge for edge in graph.file_neighbors("src/user.c", limit=20)["edges"]
        if edge["type"] == "includes"
    ]


def test_single_conditional_definition_remains_an_explicit_candidate(tmp_path):
    _write(
        tmp_path,
        "src/conditional.c",
        "#ifdef FEATURE_X\n"
        "int optional_impl(void) { return 1; }\n"
        "#endif\n"
        "int run(void) { return optional_impl(); }\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/conditional.c", "run")
    assert edge["resolved"] is False
    assert edge["dst_name"] == "optional_impl"
    assert edge["ambiguity_group"] is not None
    assert edge["resolution_basis"] == "conditional_candidate"


def test_mutually_exclusive_same_file_definition_does_not_hide_external_target(
    tmp_path,
):
    _write(tmp_path, "include/api.h", "int helper(void);\n")
    _write(tmp_path, "src/impl.c", "int helper(void) { return 7; }\n")
    _write(
        tmp_path,
        "src/user.c",
        "#ifdef USE_LOCAL_HELPER\n"
        "static int helper(void) { return 1; }\n"
        "#else\n"
        '#include "api.h"\n'
        "int run(void) { return helper(); }\n"
        "#endif\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/user.c", "run")
    assert edge["resolved"] is True
    assert edge["dst_id"] in {
        node["id"] for node in graph.find_nodes("helper")
        if node["path"] == "src/impl.c"
    }
    assert edge["resolution_basis"] == "visible_declaration"


def test_weak_default_block_is_not_treated_as_include_guard(tmp_path):
    _write(
        tmp_path,
        "src/defaults.c",
        "#ifndef HAVE_PLATFORM_IMPL\n"
        "#define HAVE_PLATFORM_IMPL\n"
        "int platform_impl(void) { return 1; }\n"
        "#endif\n"
        "int run(void) { return platform_impl(); }\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/defaults.c", "run")
    assert edge["resolved"] is False
    assert edge["resolution_basis"] == "conditional_candidate"


def test_header_weak_default_block_is_not_treated_as_include_guard(tmp_path):
    _write(
        tmp_path,
        "include/defaults.h",
        "#ifndef HAVE_PLATFORM_IMPL\n"
        "#define HAVE_PLATFORM_IMPL\n"
        "int platform_impl(void);\n"
        "#endif\n",
    )
    _write(
        tmp_path,
        "src/platform.c",
        "int platform_impl(void) { return 1; }\n",
    )
    _write(
        tmp_path,
        "src/user.c",
        '#include "defaults.h"\n'
        "int run(void) { return platform_impl(); }\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/user.c", "run")
    assert edge["resolved"] is False
    assert edge["unresolved_target"] == "platform_impl"


def test_out_of_class_declaration_identity_matches_ast_definition(tmp_path):
    _write(
        tmp_path,
        "api.hpp",
        "namespace service {\n"
        "class Device { public: static int helper(); };\n"
        "int Device::helper();\n"
        "}\n",
    )
    _write(
        tmp_path,
        "impl.cpp",
        "namespace service {\n"
        "class Device;\n"
        "int Device::helper() { return 1; }\n"
        "}\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    conn = sqlite3.connect(graph.db_file)
    try:
        declaration = conn.execute(
            "SELECT qualified_name FROM declarations"
            " WHERE path='api.hpp' AND name='helper'"
        ).fetchone()
        definition = conn.execute(
            "SELECT qualified_name FROM nodes"
            " WHERE path='impl.cpp' AND name='Device::helper'"
        ).fetchone()
    finally:
        conn.close()
    assert declaration == definition == ("service::Device::helper",)


@pytest.mark.parametrize(
    ("header", "symbol", "body"),
    [
        (
            "include/pragma_api.h",
            "pragma_api",
            "#pragma once\n"
            "#ifndef PRAGMA_API_H\n#define PRAGMA_API_H\n"
            "int pragma_api(void);\n#endif\n",
        ),
        (
            "include/wrapped_api.h",
            "wrapped_api",
            "extern int header_prefix;\n"
            "#ifndef WRAPPED_API_H\n#define WRAPPED_API_H\n"
            "int wrapped_api(void);\n#endif\n"
            "extern int header_suffix;\n",
        ),
        (
            "include/string_api.h",
            "string_api",
            'static const char *comment_token = "/*";\n'
            "#ifndef STRING_API_H\n#define STRING_API_H\n"
            "int string_api(void);\n#endif\n"
            "/* a real trailing comment */\n",
        ),
    ],
)
def test_common_include_guard_wrappers_remain_visibility_neutral(
    tmp_path, header, symbol, body,
):
    _write(tmp_path, header, body)
    _write(tmp_path, f"src/{symbol}.c", f"int {symbol}(void) {{ return 1; }}\n")
    _write(
        tmp_path,
        f"src/use_{symbol}.c",
        f'#include "{Path(header).name}"\n'
        f"int use_{symbol}(void) {{ return {symbol}(); }}\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, f"src/use_{symbol}.c", f"use_{symbol}")
    assert edge["resolved"] is True
    assert edge["dst_id"] in {
        node["id"] for node in graph.find_nodes(symbol)
        if node["path"] == f"src/{symbol}.c"
    }
    assert edge["resolution_basis"] == "visible_declaration"


def test_visible_declaration_must_match_definition_namespace(tmp_path):
    _write(
        tmp_path,
        "include/api.hpp",
        "namespace service { int helper(void); }\n",
    )
    _write(tmp_path, "src/global.cpp", "int helper(void) { return 1; }\n")
    _write(
        tmp_path,
        "src/user.cpp",
        '#include "api.hpp"\n'
        "namespace service { int run(void) { return helper(); } }\n",
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/user.cpp", "run")
    assert edge["resolved"] is False
    assert edge["unresolved_target"] == "helper"
    assert edge["resolution_basis"] == "syntactic_only"


def test_visible_header_static_inline_resolves_for_including_translation_unit(tmp_path):
    _write(
        tmp_path,
        "include/registers.h",
        "#ifndef REGISTERS_H\n#define REGISTERS_H\n"
        "static inline int read_status(void) { return 7; }\n"
        "#endif\n",
    )
    _write(
        tmp_path,
        "src/user.c",
        '#include "registers.h"\nint poll(void) { return read_status(); }\n',
    )
    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/user.c", "poll")
    assert edge["resolved"] is True
    assert edge["dst_name"] == "read_status"
    assert edge["resolution_basis"] == "visible_header_inline"


def test_python_incremental_change_cannot_degrade_c_resolution(tmp_path):
    _write_equivalence_repo(tmp_path)
    _write(tmp_path, "helpers.py", "def unrelated():\n    return 0\n")
    graph = CodeGraph(str(tmp_path))
    graph.build()
    [before] = _source_edges(graph, "src/user.c", "user_entry")
    assert before["resolved"] is True

    _write(tmp_path, "helpers.py", "def api_call():\n    return 0\n")
    code_rag.invalidate_scan_cache(tmp_path)
    graph.ensure_fresh()

    [after] = _source_edges(graph, "src/user.c", "user_entry")
    assert after["resolved"] is True
    assert after["resolution_basis"] == "visible_declaration"


def _write_equivalence_repo(root: Path) -> None:
    _write(root, "include/api.h", "int api_call(void);\n")
    _write(root, "src/api.c", "int api_call(void) { return 1; }\n")
    _write(
        root,
        "src/user.c",
        '#include "api.h"\nint user_entry(void) { return api_call(); }\n',
    )


def _snapshot(graph: CodeGraph) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(graph.db_file)
    try:
        return {
            table: sorted(conn.execute(f"SELECT * FROM {table}").fetchall(), key=repr)
            for table in ("files", "nodes", "declarations", "edges")
        }
    finally:
        conn.close()


def test_c_add_change_delete_matches_full_rebuild(tmp_path, capsys):
    incremental_root = tmp_path / "incremental"
    full_root = tmp_path / "full"
    incremental_root.mkdir()
    full_root.mkdir()
    _write_equivalence_repo(incremental_root)
    _write_equivalence_repo(full_root)

    incremental = CodeGraph(str(incremental_root))
    rebuilt = CodeGraph(str(full_root))
    incremental.build()
    rebuilt.build()
    assert _snapshot(incremental) == _snapshot(rebuilt)

    def refresh_and_compare() -> None:
        code_rag.invalidate_scan_cache(incremental_root)
        code_rag.invalidate_scan_cache(full_root)
        incremental.ensure_fresh()
        rebuilt.build()
        assert _snapshot(incremental) == _snapshot(rebuilt)

    # declaration visibility change
    for root in (incremental_root, full_root):
        _write(root, "include/api.h", "int renamed_api_call(void);\n")
    refresh_and_compare()
    assert "C/C++ visibility changed" in capsys.readouterr().err
    [unresolved] = _source_edges(incremental, "src/user.c", "user_entry")
    assert unresolved["resolved"] is False

    # add a competing external definition, then delete it again
    for root in (incremental_root, full_root):
        _write(root, "src/second.c", "int api_call(void) { return 2; }\n")
    refresh_and_compare()
    for root in (incremental_root, full_root):
        (root / "src/second.c").unlink()
    refresh_and_compare()


# ---------------------------------------------------------------------------
# 條件式 include:不得當成無條件可見性
# ---------------------------------------------------------------------------
def _conditional_repo(tmp_path):
    _write(
        tmp_path,
        "include/service.h",
        "#ifndef SERVICE_H\n#define SERVICE_H\n"
        "int service(void);\n#endif\n",
    )
    _write(
        tmp_path,
        "src/service.c",
        '#include "service.h"\nint service(void) { return 1; }\n',
    )


def test_conditional_include_is_only_a_conditional_candidate(tmp_path):
    """`#ifdef FEATURE` 內的 include + branch 外的呼叫 → 不可 resolved。"""
    _conditional_repo(tmp_path)
    _write(
        tmp_path,
        "src/user.c",
        "#ifdef FEATURE\n"
        '#include "service.h"\n'
        "#endif\n"
        "int run(void) { return service(); }\n",
    )

    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/user.c", "run")
    assert edge["resolved"] is False
    assert edge["resolution_basis"] == "conditional_candidate"
    assert edge["ambiguity_group"] is not None

    conn = sqlite3.connect(graph.db_file)
    include_condition = conn.execute(
        "SELECT condition FROM edges"
        " WHERE src_id='src/user.c' AND type='includes'"
    ).fetchone()
    conn.close()
    # include edge 必須保存自己的 condition,否則 closure 無從判斷相容性。
    assert include_condition == ("#ifdef FEATURE",)


def test_call_inside_the_same_branch_still_resolves(tmp_path):
    """同一個 `#ifdef FEATURE` 內的呼叫仍然看得到該 include → 正常解析。"""
    _conditional_repo(tmp_path)
    _write(
        tmp_path,
        "src/user_same_branch.c",
        "#ifdef FEATURE\n"
        '#include "service.h"\n'
        "int run_same(void) { return service(); }\n"
        "#endif\n",
    )

    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/user_same_branch.c", "run_same")
    assert edge["resolved"] is True
    assert edge["dst_name"] == "service"
    assert edge["resolution_basis"] == "visible_declaration"


def test_transitive_conditional_include_does_not_grant_visibility(tmp_path):
    """無條件 include 的 header 裡再條件式 include → 整條路徑降為候選。"""
    _conditional_repo(tmp_path)
    _write(
        tmp_path,
        "include/facade.h",
        "#ifndef FACADE_H\n#define FACADE_H\n"
        "#ifdef FEATURE\n"
        '#include "service.h"\n'
        "#endif\n"
        "#endif\n",
    )
    _write(
        tmp_path,
        "src/transitive.c",
        '#include "facade.h"\n'
        "int run_transitive(void) { return service(); }\n",
    )

    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/transitive.c", "run_transitive")
    assert edge["resolved"] is False
    assert edge["resolution_basis"] == "conditional_candidate"


def test_conditional_static_inline_header_is_not_visible(tmp_path):
    """條件式 include 的 static-inline header 定義同樣不算 translation unit 的一部分。"""
    _write(
        tmp_path,
        "include/regs.h",
        "#ifndef REGS_H\n#define REGS_H\n"
        "static inline int read_status(void) { return 7; }\n#endif\n",
    )
    _write(
        tmp_path,
        "src/poll.c",
        "#ifdef FEATURE\n"
        '#include "regs.h"\n'
        "#endif\n"
        "int poll(void) { return read_status(); }\n",
    )

    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/poll.c", "poll")
    assert edge["resolved"] is False
    assert edge["resolution_basis"] == "conditional_candidate"


def test_call_in_nested_branch_sees_outer_conditional_include(tmp_path):
    """include 在 `#ifdef FEATURE`、呼叫在其內層 `#ifdef MODE` → 外層條件已成立。"""
    _conditional_repo(tmp_path)
    _write(
        tmp_path,
        "src/nested.c",
        "#ifdef FEATURE\n"
        '#include "service.h"\n'
        "#ifdef MODE\n"
        "int run_nested(void) { return service(); }\n"
        "#endif\n"
        "#endif\n",
    )

    graph = CodeGraph(str(tmp_path))
    graph.build()

    [edge] = _source_edges(graph, "src/nested.c", "run_nested")
    assert edge["resolved"] is True
    assert edge["dst_name"] == "service"
    assert edge["resolution_basis"] == "visible_declaration"


def test_header_included_in_both_branches_stays_visible_in_each(tmp_path):
    """同一 header 在 `#ifdef` 與 `#else` 各 include 一次 → 兩邊的呼叫都看得到。

    只保留第一個 condition 會讓 `#else` 那邊被誤判成互斥而消失。
    """
    _conditional_repo(tmp_path)
    _write(
        tmp_path,
        "src/branches.c",
        "#ifdef USE_A\n"
        '#include "service.h"\n'
        "int run_a(void) { return service(); }\n"
        "#else\n"
        '#include "service.h"\n'
        "int run_b(void) { return service(); }\n"
        "#endif\n",
    )

    graph = CodeGraph(str(tmp_path))
    graph.build()

    for symbol in ("run_a", "run_b"):
        [edge] = _source_edges(graph, "src/branches.c", symbol)
        assert edge["resolved"] is True, symbol
        assert edge["dst_name"] == "service", symbol
        assert edge["resolution_basis"] == "visible_declaration", symbol
