"""Regression contracts: unavailable primary code backends never produce evidence."""
from pathlib import Path
from types import SimpleNamespace

import pytest

import ast_parser
import code_context
import code_graph
import code_rag
import config
from runtime_dependencies import DEPENDENCY_ERROR_PREFIX, DependencyError


def _raise(exc):
    raise exc


@pytest.mark.smoke
@pytest.mark.parametrize("failure", ["core", "grammar", "constructor", "parse", "ctags_missing", "ctags_failure", "ctags_json"])
def test_primary_code_parser_unavailability_never_uses_regex(monkeypatch, failure):
    """Missing AST/ctags used to silently return regex or empty symbols."""
    path = Path("sample.java" if failure.startswith("ctags") else "sample.c")
    content = "class Example {}" if failure.startswith("ctags") else "int launch(void) { return 1; }"
    if failure == "core":
        monkeypatch.setattr(ast_parser, "HAS_TREE_SITTER", False)
    elif failure == "grammar":
        monkeypatch.setattr(ast_parser, "_try_load_tree_sitter_language", lambda lang: None)
    elif failure in ("constructor", "parse"):
        monkeypatch.setattr(ast_parser, "HAS_TREE_SITTER", True)
        monkeypatch.setattr(ast_parser, "_try_load_tree_sitter_language", lambda lang: object())
        if failure == "constructor":
            monkeypatch.setattr(ast_parser, "Parser", lambda lang: _raise(ValueError("ABI mismatch")))
        else:
            monkeypatch.setattr(ast_parser, "Parser", lambda lang: SimpleNamespace(
                parse=lambda data: _raise(RuntimeError("parser unavailable"))))
    else:
        def run(cmd, **kwargs):
            if failure == "ctags_missing":
                raise FileNotFoundError("ctags")
            if "--version" in cmd:
                return SimpleNamespace(returncode=0, stdout="Universal Ctags", stderr="")
            if "--list-features" in cmd:
                return SimpleNamespace(returncode=0, stdout="json\n", stderr="")
            if "--list-languages" in cmd:
                return SimpleNamespace(returncode=0, stdout="Java\nKotlin\n", stderr="")
            return SimpleNamespace(returncode=1 if failure == "ctags_failure" else 0,
                                   stdout="not-json", stderr="ctags failed")
        monkeypatch.setattr(ast_parser.process_env, "run", run)
    with pytest.raises(DependencyError):
        ast_parser.parse_file(path, content)


@pytest.mark.smoke
def test_incompatible_grammar_probe_stays_none_for_advisory_verification(monkeypatch):
    """ABI failures must be a missing capability probe, never a different parser."""
    import sys
    monkeypatch.setattr(ast_parser, "HAS_TREE_SITTER", True)
    monkeypatch.setattr(ast_parser, "_TREE_SITTER_LANGUAGES", {})
    monkeypatch.setitem(sys.modules, "tree_sitter_c", SimpleNamespace(language=lambda: object()))
    monkeypatch.setattr(ast_parser, "Language", lambda value: _raise(ValueError("incompatible ABI")))
    assert ast_parser._try_load_tree_sitter_language("c") is None
    with pytest.raises(DependencyError):
        ast_parser.parse_file(Path("sample.c"), "int value;")


@pytest.mark.smoke
def test_incompatible_parser_constructor_probe_reports_unavailable(monkeypatch):
    import sys
    monkeypatch.setattr(ast_parser, "HAS_TREE_SITTER", True)
    monkeypatch.setattr(ast_parser, "_TREE_SITTER_LANGUAGES", {})
    monkeypatch.setitem(sys.modules, "tree_sitter_c", SimpleNamespace(language=lambda: object()))
    monkeypatch.setattr(ast_parser, "Language", lambda value: value)
    monkeypatch.setattr(ast_parser, "Parser", lambda value: _raise(ValueError("Parser ABI mismatch")))
    assert ast_parser._try_load_tree_sitter_language("c") is None


@pytest.mark.smoke
def test_invalid_header_language_does_not_choose_a_different_grammar(monkeypatch):
    monkeypatch.setattr(config, "H_LANG", "invalid-cpp")
    with pytest.raises(DependencyError, match="h_lang"):
        ast_parser.get_parser(Path("sample.h"))
    with pytest.raises(DependencyError, match="h_lang"):
        code_graph._h_ext_lang()


@pytest.mark.smoke
def test_invalid_parser_response_cannot_become_an_empty_index(tmp_path, monkeypatch):
    monkeypatch.setattr(ast_parser, "HAS_TREE_SITTER", True)
    monkeypatch.setattr(ast_parser, "_try_load_tree_sitter_language", lambda lang: object())
    monkeypatch.setattr(ast_parser, "Parser", lambda lang: SimpleNamespace(parse=lambda data: None))
    rag = code_rag.CodeRAG(str(tmp_path))
    with pytest.raises(DependencyError):
        rag._extract_symbols(tmp_path / "sample.c", "int launch(void) { return 1; }")


@pytest.mark.smoke
@pytest.mark.parametrize("languages", ["Java\n", "Java\nKotlin [disabled]\n"])
def test_ctags_missing_language_cannot_reuse_cached_symbols(tmp_path, monkeypatch, languages):
    def run(cmd, **kwargs):
        output = ("Universal Ctags" if "--version" in cmd else
                  "json\n" if "--list-features" in cmd else languages)
        return SimpleNamespace(returncode=0, stdout=output, stderr="")
    monkeypatch.setattr(ast_parser.process_env, "run", run)
    monkeypatch.setattr(code_rag, "USE_RERANKER", False)
    rag = code_rag.CodeRAG(str(tmp_path))
    rag.index = [{"path": "sample.kt", "symbol": "Example", "line": 1,
                  "type": "class", "embedding": [1., 0.]}]
    monkeypatch.setattr(rag, "_get_embedding", lambda text: [1., 0.])
    with pytest.raises(DependencyError, match="Kotlin|kotlin"):
        rag.query("Example")


@pytest.mark.smoke
@pytest.mark.parametrize("consumer", ["rag_extract", "graph_extract", "graph_relations", "rag_warm", "graph_warm"])
def test_parser_dependency_errors_cannot_publish_or_reuse_indexes(tmp_path, monkeypatch, consumer):
    source = tmp_path / "sample.c"
    source.write_text("int launch(void) { return 1; }\n", encoding="utf-8")
    rag = code_rag.CodeRAG(str(tmp_path))
    graph = code_graph.CodeGraph(str(tmp_path))
    error = DependencyError("required parser is unavailable")
    if consumer == "rag_extract":
        monkeypatch.setattr(code_rag, "parse_file", lambda *args: _raise(error))
        operation = lambda: rag._extract_symbols(source, source.read_text())
    elif consumer == "graph_extract":
        monkeypatch.setattr(code_graph, "parse_file", lambda *args: _raise(error))
        operation = lambda: graph._extract_file("sample.c", source, "hash")
    elif consumer == "graph_relations":
        monkeypatch.setattr(ast_parser, "_try_load_tree_sitter_language", lambda lang: None)
        monkeypatch.setattr(code_graph, "_try_load_tree_sitter_language", lambda lang: None)
        operation = lambda: code_graph._extract_c_relations("sample.c", source.read_text(), "c", [])
    elif consumer == "rag_warm":
        rag.index = [{"path": "sample.c", "symbol": "launch", "line": 1,
                      "type": "function", "embedding": [1., 0.]}]
        monkeypatch.setattr(rag, "_get_embedding", lambda text: [1., 0.])
        monkeypatch.setattr(code_rag, "USE_RERANKER", False)
        monkeypatch.setattr(ast_parser, "HAS_TREE_SITTER", False)
        operation = lambda: rag.query("launch")
    else:
        # A valid existing graph must not become an empty graph when a grammar vanishes.
        graph.build()
        before = graph.db_file.read_bytes()
        monkeypatch.setattr(ast_parser, "HAS_TREE_SITTER", False)
        monkeypatch.setattr(code_graph, "HAS_TREE_SITTER", False)
        operation = graph.ensure_fresh
    with pytest.raises(DependencyError):
        operation()
    if consumer == "graph_warm":
        assert graph.db_file.read_bytes() == before
    else:
        assert not rag.cache_meta_file.exists()


@pytest.mark.smoke
@pytest.mark.parametrize("operation", ["build", "load", "save", "query", "materialize"])
def test_missing_numpy_refuses_code_operations_before_cache_changes(tmp_path, monkeypatch, operation):
    rag = code_rag.CodeRAG(str(tmp_path))
    rag.cache_meta_file.write_text("preserve metadata", encoding="utf-8")
    rag.cache_emb_file.write_bytes(b"preserve vectors")
    rag.index = [{"path": "x.py", "symbol": "answer", "line": 1,
                  "type": "function", "embedding": [1., 0.]}]
    monkeypatch.setattr(code_rag, "HAS_NUMPY", False)
    monkeypatch.setattr(rag, "_get_embedding", lambda text: [1., 0.])
    monkeypatch.setattr(code_rag, "USE_RERANKER", False)
    calls = {
        "build": lambda: rag.build_index(verbose=False),
        "load": rag._load_file_cache,
        "save": rag._save_cache,
        "query": lambda: rag.query("answer"),
        "materialize": rag._materialize_dense_index,
    }
    with pytest.raises(DependencyError, match="numpy"):
        calls[operation]()
    assert rag.cache_meta_file.read_text() == "preserve metadata"
    assert rag.cache_emb_file.read_bytes() == b"preserve vectors"


@pytest.mark.smoke
@pytest.mark.parametrize("policy", ["error", "embedding", "main_model"])
@pytest.mark.parametrize("failure", ["unavailable", "request"])
def test_code_reranker_failure_never_returns_fusion(tmp_path, monkeypatch, policy, failure):
    rag = code_rag.CodeRAG(str(tmp_path))
    monkeypatch.setattr(config, "RERANK_FALLBACK_POLICY", policy)
    monkeypatch.setattr(code_rag, "USE_RERANKER", True)
    monkeypatch.setattr(rag, "_should_rerank", lambda *args: True)
    monkeypatch.setattr(rag, "_check_reranker_available", lambda: failure == "request")
    monkeypatch.setattr(code_rag.llama_client, "rerank", lambda **kwargs: _raise(RuntimeError("offline")))
    candidates = [(0.8, 0.8, 0.8, {"path": "a.py", "symbol": "a"}),
                  (0.7, 0.7, 0.7, {"path": "b.py", "symbol": "b"})]
    with pytest.raises(DependencyError, match="Code RAG reranker unavailable"):
        rag._rerank_code_candidates("question", candidates, 1)


@pytest.mark.smoke
@pytest.mark.parametrize("source", ["grep", "graph"])
def test_context_propagates_dependency_failures_instead_of_partial_evidence(tmp_path, monkeypatch, source):
    if source == "grep":
        executor = SimpleNamespace(grep=lambda *args, **kwargs: DEPENDENCY_ERROR_PREFIX + "rg missing")
        operation = lambda: code_context.collect_safe_lexical_hits(executor, "launch", ["sample.c"])
    else:
        monkeypatch.setattr(code_context, "_graph_candidates", lambda *args: _raise(DependencyError("grammar missing")))
        operation = lambda: code_context.build_code_context(
            query="launch", semantic_items=[], index_items=[], allowed_paths=[],
            read_window=lambda *args: "", max_chars=2000, graph=object())
    with pytest.raises(DependencyError):
        operation()


@pytest.mark.smoke
def test_backend_policy_invalidation_does_not_change_semantic_vector_identity(tmp_path):
    from eval.semantic_retrieval import pipeline_identity
    identity = code_rag.cache_identity()
    assert identity.get("parser_backend_policy"), "old degraded caches need an independent invalidation key"
    assert identity["parser_backend_policy"] in code_graph.CodeGraph(str(tmp_path))._parser_versions()
    assert ast_parser.PARSER_SEMANTICS_VERSION == 4
    assert "parser_backend_policy" not in pipeline_identity()
