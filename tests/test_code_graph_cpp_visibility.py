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
