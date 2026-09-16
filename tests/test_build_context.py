"""Safety and silent-answer contracts; all inputs are synthetic and local."""
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from build_context import BuildContextError, import_build_context, load_build_context


def _write(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _database(root, entries, path="compile_commands.json"):
    return _write(root, path, json.dumps([
        {"directory": str(root), **entry} for entry in entries
    ]))


def _builtin(root):
    return _write(root, "builtin-macros.txt", "#define __STDC__ 1\n")


def _reader(root):
    def read(path, start, end):
        lines = (root / path).read_text().splitlines()
        return "\n".join(f"{n:4d} | {line}" for n, line in enumerate(lines, 1)
                         if start <= n <= end)
    return SimpleNamespace(root=root, _safe_path=lambda path: (root / path).resolve(), read_file=read)


@pytest.mark.smoke
def test_explicit_missing_target_never_falls_back_to_global_search(tmp_path):
    with pytest.raises(BuildContextError, match="target.*metadata|metadata.*target"):
        load_build_context(tmp_path, "firmware/debug")


@pytest.mark.smoke
def test_manifest_admits_exact_generated_inputs_and_never_executes_commands(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("build metadata must never spawn a process")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    _write(tmp_path, "main.c", '#include "config.h"\n#if MODE == 1\nint enabled(void){return 1;}\n#else\nint disabled(void){return 0;}\n#endif\n')
    _write(tmp_path, "build/generated/config.h", "#define HEADER_VALUE 1\n")
    _write(tmp_path, "build/generated/stranger.h", "int must_not_be_indexed;\n")
    _write(tmp_path, "build/args.rsp", '-DMODE=0 -UMODE -DMODE=1 -Ibuild/generated "-DSHELL=$(touch must_not_exist)"\n')
    database = _database(tmp_path, [{"file": "main.c", "arguments": ["arm-none-eabi-gcc", "@build/args.rsp", "-c", "main.c"],
                                  "command": "touch must_not_exist"}])
    import_build_context(tmp_path, "debug", compile_commands=database, builtin_macros=_builtin(tmp_path))
    context = load_build_context(tmp_path, "debug")
    from index_scope import IndexScope, walk_index_files
    default = IndexScope(tmp_path)
    selected = IndexScope(tmp_path, build_context=context)
    assert not default.should_index_file("build/generated/config.h")
    assert selected.should_index_file("build/generated/config.h")
    assert not selected.should_index_file("build/generated/stranger.h")
    assert not selected.should_index_file("build/args.rsp")
    assert {rel for _, rel in walk_index_files(selected)} == {"main.c", "build/generated/config.h"}
    assert context.state_for("main.c", 3) == "active"
    assert context.state_for("main.c", 5) == "inactive"
    manifest = json.loads((tmp_path / ".codetrail/build-context.json").read_text())
    admitted = manifest["targets"]["debug"]["admitted_files"]
    assert {(item["path"], item["kind"]) for item in admitted} == {
        ("build/args.rsp", "response"), ("build/generated/config.h", "header")}
    assert all(len(item["sha256"]) == 64 for item in admitted)
    assert not (tmp_path / "must_not_exist").exists()
    assert (tmp_path / ".codetrail/build-context.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.smoke
def test_selected_target_filters_semantic_graph_and_context_before_candidate_limits(tmp_path, monkeypatch):
    from code_graph import CodeGraph
    from code_rag import CodeRAG
    from code_context import build_code_context, collect_safe_lexical_hits
    _write(tmp_path, "main.c", '\n'.join([
        '#include "config.h"', '#if CHOICE == 1', 'int branch_a(void){return 1;}',
        '#else', 'int branch_b(void){return 2;}', '#endif', 'int entry(void){',
        '#if CHOICE == 1', 'return branch_a();', '#else', 'return branch_b();', '#endif', '}']) + '\n')
    _write(tmp_path, "unselected.c", "int outsider(void){return 3;}\n")
    _write(tmp_path, "build/a/config.h", "#define CHOICE 1\n")
    _write(tmp_path, "build/b/config.h", "#define CHOICE 2\n")
    database = _database(tmp_path, [
        {"target": "A", "file": "main.c", "arguments": ["aarch64-none-elf-gcc", "-Ibuild/a", "-Ibuild/b", "-c", "main.c"]},
        {"target": "B", "file": "main.c", "arguments": ["aarch64-none-elf-gcc", "-Ibuild/b", "-Ibuild/a", "-c", "main.c"]},
    ])
    builtin = _builtin(tmp_path)
    for target in ("A", "B"):
        import_build_context(tmp_path, target, compile_commands=database, builtin_macros=builtin)
    contexts = {name: load_build_context(tmp_path, name) for name in ("A", "B")}
    base_rag, base_graph = CodeRAG(str(tmp_path)), CodeGraph(str(tmp_path))
    cache_names = set()
    for target, wanted, forbidden in (("A", "branch_a", "branch_b"), ("B", "branch_b", "branch_a")):
        context = contexts[target]
        rag = base_rag.for_build_context(context)
        graph = base_graph.for_build_context(context)
        cache_names.add(str(rag.cache_meta_file))
        paths = rag._scan_code_files()
        assert "unselected.c" not in paths
        assert f"build/{target.lower()}/config.h" in paths
        assert f"build/{'b' if target == 'A' else 'a'}/config.h" not in paths
        for path, info in paths.items():
            symbols, _ = rag._index_single_file(info["filepath"], path, compute_embeddings=False)
            rag.index.extend(symbols)
        assert forbidden not in {item["symbol"] for item in rag.index}
        monkeypatch.setattr(rag, "_get_embedding", lambda text: [1.0, 0.0])
        monkeypatch.setattr(rag, "_rerank_code_candidates", lambda question, candidates, top_k, trace=None:
                            rag._fusion_candidates(candidates, top_k))
        semantic = rag.query(wanted, top_k=1)
        assert semantic and semantic[0]["symbol"] == wanted
        assert semantic[0]["build_state"] == "active"
        graph.build()
        graph.ensure_fresh()
        assert not graph.find_nodes(forbidden)
        anchor = graph.find_nodes("entry")[0]
        neighbors = graph.neighbors(anchor["id"])
        assert wanted in {node["name"] for node in neighbors["nodes"]}
        assert forbidden not in {node["name"] for node in neighbors["nodes"]}
        chains = graph.shortest_evidence_paths({"entry"}, {wanted})
        assert chains and all(edge["build_state"] == "active" for edge in chains[0])
        assert graph.file_includes("main.c") == [f"build/{target.lower()}/config.h"]
        reader = _reader(tmp_path)
        lexical = collect_safe_lexical_hits(reader, "branch", paths, build_context=context)
        bundle = build_code_context(query="branch", semantic_items=rag.index,
            index_items=rag.index, allowed_paths=paths, read_window=reader.read_file,
            max_chars=12000, graph=graph, lexical_hits=lexical, build_context=context)
        assert bundle["evidence"]
        assert forbidden not in "\n".join(item["text"] for item in bundle["evidence"])
        assert bundle["build_context"]["target"] == target
    assert len(cache_names) == 2
    assert base_rag.build_context is None and base_graph.build_context is None


@pytest.mark.smoke
def test_unknown_builtin_expression_and_duplicate_tu_never_form_confirmed_path(tmp_path):
    from code_graph import CodeGraph
    _write(tmp_path, "main.c", "int leaf(void){return 1;}\n#if __UNRECORDED_BUILTIN__\nint uncertain(void){return leaf();}\n#endif\n#if FEATURE(2)\nint expression(void){return leaf();}\n#endif\n")
    database = _database(tmp_path, [{"file": "main.c", "arguments": ["ccac", "-av2hs", "-c", "main.c"]}])
    import_build_context(tmp_path, "unknown", compile_commands=database)
    context = load_build_context(tmp_path, "unknown")
    graph = CodeGraph(str(tmp_path), build_context=context)
    graph.build()
    node = graph.find_nodes("uncertain")[0]
    assert node["build_state"] == "unknown"
    edges = graph.neighbors(node["id"])["edges"]
    assert edges and all(not edge["resolved"] and edge["build_state"] == "unknown" for edge in edges)
    assert graph.shortest_evidence_paths({"uncertain"}, {"leaf"}) == []
    assert context.summary()["status"] == "unknown"
    duplicate = _database(tmp_path, [
        {"file": "main.c", "output": "one.o", "arguments": ["gcc", "-DOPTION=1", "-c", "main.c"]},
        {"file": "main.c", "output": "two.o", "arguments": ["gcc", "-DOPTION=0", "-c", "main.c"]},
    ], "duplicates.json")
    import_build_context(tmp_path, "duplicate", compile_commands=duplicate)
    duplicate_context = load_build_context(tmp_path, "duplicate")
    assert duplicate_context.state_for("main.c", 1) == "unknown"
    assert any("ambiguous translation unit" in issue for issue in duplicate_context.issues)
    assert load_build_context(tmp_path).summary()["status"] == "unknown"


@pytest.mark.smoke
def test_dependency_hash_and_include_precedence_changes_invalidate_context(tmp_path):
    _write(tmp_path, "main.c", '#include "config.h"\n#if CHOICE == 1\nint one;\n#else\nint two;\n#endif\n')
    header = _write(tmp_path, "build/generated/config.h", "#define CHOICE 1\n")
    database = _database(tmp_path, [{"file": "main.c", "arguments": ["gcc", "-Ibuild/generated", "-c", "main.c"]}])
    import_build_context(tmp_path, "target", compile_commands=database, builtin_macros=_builtin(tmp_path))
    context = load_build_context(tmp_path, "target")
    original_stat = header.stat()
    header.write_text("#define CHOICE 2\n")
    os.utime(header, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    with pytest.raises(BuildContextError, match="changed"):
        context.assert_fresh()
    with pytest.raises(BuildContextError, match="reimport"):
        load_build_context(tmp_path, "target")
    import_build_context(tmp_path, "target", compile_commands=database, builtin_macros=tmp_path / "builtin-macros.txt")
    changed = load_build_context(tmp_path, "target")
    assert changed.fingerprint != context.fingerprint
    assert changed.state_for("main.c", 3) == "inactive"
    _write(tmp_path, "config.h", "#define CHOICE 1\n")
    with pytest.raises(BuildContextError, match="changed"):
        changed.assert_fresh()
    reordered = load_build_context(tmp_path, "target")
    assert reordered.state_for("main.c", 3) == "active"
    assert reordered.fingerprint != changed.fingerprint


@pytest.mark.smoke
def test_missing_unsupported_and_unsafe_inputs_stay_unknown(tmp_path):
    _write(tmp_path, "main.c", '#include "absent.h"\n#if FEATURE\nint maybe(void){return 1;}\n#endif\n')
    database = _database(tmp_path, [{"file": "main.c", "arguments": ["gcc", "-DFEATURE=1", "-unsupported-driver-flag", "@missing.rsp", "-c", "main.c"]}])
    import_build_context(tmp_path, "unknown", compile_commands=database)
    context = load_build_context(tmp_path, "unknown")
    assert context.state_for("main.c", 3) == "unknown"
    assert any("missing include" in reason for reason in context.issues)
    assert any("response unavailable" in reason for reason in context.issues)
    assert any("unsupported compiler flag" in reason for reason in context.issues)
    unsafe = tmp_path / "unsafe.json"
    unsafe.symlink_to(database)
    with pytest.raises((BuildContextError, OSError)):
        import_build_context(tmp_path, "unsafe", compile_commands=unsafe)
    nested = tmp_path / "linked"
    nested.symlink_to(tmp_path / "build", target_is_directory=True)
    _write(tmp_path, "build/response.rsp", "-DNEVER=1\n")
    link_database = _database(tmp_path, [{"file": "main.c", "arguments": ["gcc", "@linked/response.rsp", "-c", "main.c"]}], "linked.json")
    import_build_context(tmp_path, "linked", compile_commands=link_database)
    assert any("response unavailable" in reason for reason in load_build_context(tmp_path, "linked").issues)


@pytest.mark.smoke
def test_verbose_log_forced_include_and_imacros_share_graph_evidence(tmp_path):
    from code_graph import CodeGraph
    _write(tmp_path, "main.c", "#if LOG_ENABLED\nint start(void){return helper();}\n#endif\n")
    _write(tmp_path, "build/pre.h", "static inline int helper(void){return 7;}\n")
    _write(tmp_path, "build/macros.h", "#define LOG_ENABLED 1\nint discarded_by_imacros(void){return 9;}\n")
    log = _write(tmp_path, "verbose.log", "[1/1] ccac -av2hs -imacros build/macros.h -Hinclude=build/pre.h -c main.c -o build/main.o\n")
    import_build_context(tmp_path, "logged", build_log=log, builtin_macros=_builtin(tmp_path))
    context = load_build_context(tmp_path, "logged")
    graph = CodeGraph(str(tmp_path), build_context=context)
    graph.build()
    assert not graph.find_nodes("discarded_by_imacros")
    assert graph.find_nodes("helper")
    assert set(graph.file_includes("main.c")) == {"build/pre.h", "build/macros.h"}
    assert graph.shortest_evidence_paths({"start"}, {"helper"})


@pytest.mark.smoke
def test_inactive_grep_hits_cannot_consume_selected_lexical_budget(tmp_path, monkeypatch):
    import code_context
    _write(tmp_path, "main.c", "#if 0\n" + "int needle_inactive;\n" * 200 + "#endif\nint needle_active;\n")
    database = _database(tmp_path, [{"file": "main.c", "arguments": ["gcc", "-c", "main.c"]}])
    import_build_context(tmp_path, "selected", compile_commands=database)
    context = load_build_context(tmp_path, "selected")
    monkeypatch.setattr(code_context, "_MAX_LEXICAL_HITS", 1)
    hits = code_context.collect_safe_lexical_hits(_reader(tmp_path), "needle", ["main.c"], build_context=context)
    assert len(hits) == 1 and hits[0]["line"] == 203
