"""Required RAG backends must never become successful degraded retrieval."""
from __future__ import annotations

import builtins
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
import knowledge
from runtime_dependencies import DependencyError


pytestmark = pytest.mark.smoke


def _bare_kb():
    kb = knowledge.KnowledgeBase.__new__(knowledge.KnowledgeBase)
    kb._has_ctx = False
    kb._needs_gate = False
    return kb


def _candidates():
    return [knowledge.Candidate(
        chunk_idx=i, chunk={"content": f"passage {i}"},
        rrf_score=0.3, retrieval_score=0.3, gate_score=0.3,
    ) for i in range(3)]


def test_missing_jieba_never_returns_regex_tokens(monkeypatch):
    monkeypatch.setattr(knowledge, "HAS_JIEBA", False)
    kb = _bare_kb()
    for tokenizer in (kb._tokenize_for_bm25, kb._extract_keywords):
        with pytest.raises(DependencyError, match="jieba"):
            tokenizer("韌體中斷控制器的初始化流程")


def test_broken_jieba_reports_dependency_failure(monkeypatch):
    def broken_cut(*_args, **_kwargs):
        raise OSError("dictionary is unreadable")

    monkeypatch.setattr(knowledge, "HAS_JIEBA", True)
    monkeypatch.setattr(knowledge, "jieba", SimpleNamespace(cut=broken_cut), raising=False)
    for tokenizer in (_bare_kb()._tokenize_for_bm25, _bare_kb()._extract_keywords):
        with pytest.raises(DependencyError, match="jieba.*dictionary"):
            tokenizer("韌體中斷控制器")


def test_warm_kb_requires_numpy_and_jieba_before_query_work(monkeypatch):
    kb = _bare_kb()
    kb.loaded = True
    kb.chunks = [{"content": "cached evidence"}]
    calls = []
    kb._hybrid_search = lambda *_args, **_kwargs: calls.append("search") or []
    monkeypatch.setattr(knowledge, "BM25_ENABLED", True)
    for flag, name in (("HAS_NUMPY", "numpy"), ("HAS_JIEBA", "jieba")):
        with monkeypatch.context() as patch:
            patch.setattr(knowledge, flag, False)
            with pytest.raises(DependencyError, match=name):
                kb.query("cached question")
    assert calls == []


def test_missing_numpy_cannot_run_small_pool_mmr(monkeypatch):
    monkeypatch.setattr(knowledge, "HAS_NUMPY", False)
    chunks = [{"content": "one", "embedding": [1.0, 0.0]},
              {"content": "two", "embedding": [0.0, 1.0]}]
    with pytest.raises(DependencyError, match="numpy"):
        _bare_kb()._mmr_select(chunks, [1.0, 0.0], 1)


def test_required_dependency_error_is_not_swallowed_by_kb_load(monkeypatch, tmp_path):
    path = tmp_path / "knowledge.json"
    path.write_text(json.dumps({"chunks": [], "metadata": {}}), encoding="utf-8")

    def unavailable(_self):
        raise DependencyError("numpy ABI mismatch")

    monkeypatch.setattr(knowledge.KnowledgeBase, "_load_embeddings_from_npz", unavailable)
    with pytest.raises(DependencyError, match="numpy ABI"):
        knowledge.KnowledgeBase(str(path))


def test_unavailable_reranker_never_uses_a_legacy_policy(monkeypatch):
    kb = _bare_kb()
    kb._check_reranker_available = lambda: False
    monkeypatch.setattr(knowledge, "USE_RERANKER", True)
    monkeypatch.setattr(knowledge, "RERANKER_ALWAYS_ON", True)
    main_calls = []
    monkeypatch.setattr(knowledge, "_gated_completion",
                        lambda **_kwargs: main_calls.append("main") or "DOC_0, DOC_1")
    for policy in ("embedding", "main_model", "error"):
        monkeypatch.setattr(config, "RERANK_FALLBACK_POLICY", policy)
        with pytest.raises(DependencyError, match="RAG reranker unavailable"):
            kb._rerank_with_model("question", _candidates(), 2, is_strict_mode=True)
    assert main_calls == []


def test_failed_reranker_batch_never_returns_partial_or_original_ranks(monkeypatch):
    kb = _bare_kb()
    kb._check_reranker_available = lambda: True
    monkeypatch.setattr(knowledge, "USE_RERANKER", True)
    monkeypatch.setattr(knowledge, "RERANKER_ALWAYS_ON", True)
    monkeypatch.setattr(config, "RERANK_FALLBACK_POLICY", "embedding")
    candidates = _candidates() * 6
    for candidate in candidates:
        candidate.section_rrf_score = 0.1
    batches = []

    def rerank(**kwargs):
        batches.append(len(kwargs["documents"]))
        if len(batches) == 2:
            raise RuntimeError("selected server disconnected")
        return [0.8] * len(kwargs["documents"])

    monkeypatch.setattr(knowledge.llama_client, "rerank", rerank)
    with pytest.raises(DependencyError, match="RAG reranker unavailable.*disconnected"):
        kb._rerank_with_model("question", candidates, 2, is_strict_mode=True)
    assert batches == [15, 3]


def test_reranker_nonfinite_scores_are_a_protocol_error(monkeypatch):
    kb = _bare_kb()
    kb._check_reranker_available = lambda: True
    monkeypatch.setattr(knowledge, "USE_RERANKER", True)
    monkeypatch.setattr(knowledge, "RERANKER_ALWAYS_ON", True)
    monkeypatch.setattr(knowledge.llama_client, "rerank",
                        lambda **_kwargs: [float("nan"), 0.2, 0.3])
    with pytest.raises(DependencyError, match="reranker.*finite"):
        kb._rerank_with_model("question", _candidates(), 2, is_strict_mode=True)


def test_enabled_expansion_propagates_service_and_configuration_failures(monkeypatch):
    monkeypatch.setattr(knowledge, "USE_QUERY_EXPANSION", True)
    monkeypatch.setattr(knowledge, "MULTI_QUERY_ENABLED", True)
    monkeypatch.setattr(knowledge, "MULTI_QUERY_TYPES", ["key_terms"])
    monkeypatch.setattr(knowledge.config, "require_main_model", lambda: "test-model")
    kb = _bare_kb()

    def unavailable(**_kwargs):
        raise DependencyError("selected main server unavailable")

    monkeypatch.setattr(knowledge, "_gated_completion", unavailable)
    for generate in (kb._expand_query, kb._generate_multi_queries):
        with pytest.raises(DependencyError, match="selected main server"):
            generate("interrupt controller")

    def bad_config():
        raise RuntimeError("main model configuration is incomplete")

    monkeypatch.setattr(knowledge.config, "require_main_model", bad_config)
    with pytest.raises(RuntimeError, match="configuration is incomplete"):
        kb._generate_multi_queries("interrupt controller")


def test_pdf_probe_does_not_import_the_old_fitz_namespace(monkeypatch):
    import figure_extract
    import figure_verify

    original_import = builtins.__import__
    imports = []

    def without_pymupdf(name, *args, **kwargs):
        if name == "pymupdf":
            raise ImportError("pymupdf installation is missing")
        if name == "fitz":
            imports.append("fitz")
            return SimpleNamespace()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_pymupdf)
    with pytest.raises(figure_extract.FigureCapabilityError, match="PyMuPDF"):
        figure_verify._import_pymupdf()
    assert imports == []


def test_rag_rejects_incomplete_config_instead_of_standalone_defaults(monkeypatch):
    import RAG

    original_import = builtins.__import__

    def incomplete_config(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "config" and "EMBEDDING_MODEL" in (fromlist or ()):
            raise ImportError("required EMBEDDING_MODEL is absent")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", incomplete_config)
    spec = importlib.util.spec_from_file_location("_rag_missing_config", Path(RAG.__file__))
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    with pytest.raises(ImportError, match="EMBEDDING_MODEL"):
        spec.loader.exec_module(module)


def test_context_writer_requires_nofollow_before_any_write(monkeypatch, tmp_path):
    import context_generation

    root = tmp_path / "new-context-cache"
    lock = context_generation.SingleWriterLock(root)
    acquired = []
    monkeypatch.setattr(context_generation.os, "O_NOFOLLOW", 0)
    monkeypatch.setattr(lock, "_try_lock", lambda _fd: acquired.append(True))
    try:
        with pytest.raises(context_generation.ContextLockError, match="O_NOFOLLOW"):
            lock.acquire()
    finally:
        lock.release()
    assert not root.exists()
    assert acquired == []
    assert lock._fd is None


def test_ingest_requires_numpy_before_embedding_cache_or_generation(monkeypatch, tmp_path):
    import RAG

    calls = []

    def require_numpy():
        raise DependencyError("numpy ABI is unavailable")

    def cache_access(*_args):
        calls.append("cache")
        raise AssertionError("no cache access is allowed without numpy")

    monkeypatch.setattr(RAG.kb_cache, "_require_numpy", require_numpy)
    monkeypatch.setattr(RAG, "_load_embedding_cache", cache_access)
    chunks = [{"source": "spec.md", "content": "source evidence"}]
    for generate in (RAG.generate_embeddings, RAG.generate_gate_embeddings):
        with pytest.raises(DependencyError, match="numpy"):
            generate(chunks, cache_dir=tmp_path)
    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_ingest_requires_numpy_before_context_generation(monkeypatch, tmp_path):
    import RAG
    import context_generation

    calls = []

    def require_numpy():
        raise DependencyError("numpy ABI is unavailable")

    def generate_context(*_args, **_kwargs):
        calls.append("context")
        raise AssertionError("no context generation is allowed without numpy")

    monkeypatch.setattr(RAG.kb_cache, "_require_numpy", require_numpy)
    monkeypatch.setattr(context_generation, "generate_document_context", generate_context)
    document = SimpleNamespace(source="spec.md", chunks=[{"content": "source evidence"}])
    with pytest.raises(DependencyError, match="numpy"):
        RAG._commit_document_to_kb(document, str(tmp_path / "knowledge.json"),
                                   generate_context=True)
    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_store_lock_requires_safety_before_lockfile_creation(monkeypatch, tmp_path):
    import knowledge_store

    root = tmp_path / "new-kb"
    entered = []
    monkeypatch.setattr(knowledge_store.os, "O_NOFOLLOW", 0)
    with pytest.raises(knowledge_store.KnowledgeStoreError, match="O_NOFOLLOW"):
        with knowledge_store.knowledge_store_lock(root / "knowledge.json", exclusive=True):
            entered.append(True)
    assert entered == []
    assert not root.exists()


def test_store_lock_missing_backend_is_typed_before_creation(monkeypatch, tmp_path):
    import knowledge_store

    root = tmp_path / "new-kb"
    original_import = builtins.__import__

    def missing_lock(name, *args, **kwargs):
        if name in ("fcntl", "msvcrt"):
            raise ImportError("locking backend is unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_lock)
    with pytest.raises(knowledge_store.KnowledgeStoreError, match="locking backend"):
        with knowledge_store.knowledge_store_lock(root / "knowledge.json", exclusive=True):
            pytest.fail("lock acquisition must fail before entering the writer")
    assert not root.exists()


def test_failed_store_lock_cannot_be_swallowed_as_cache_write_warning(monkeypatch, tmp_path):
    import fcntl
    import kb_cache
    import knowledge_store

    def unsupported_lock(_fd, operation):
        if operation != fcntl.LOCK_UN:
            raise OSError("flock is not supported by this filesystem")

    monkeypatch.setattr(fcntl, "flock", unsupported_lock)
    with pytest.raises(knowledge_store.KnowledgeStoreError, match="flock.*not supported"):
        kb_cache._publish_rebuilt(tmp_path / "knowledge.json", {}, [], {})


def test_knowledge_load_requires_openat_before_lockfile_creation(monkeypatch, tmp_path):
    import kb_cache
    import knowledge_store

    path = tmp_path / "knowledge.json"
    path.write_text(json.dumps({"chunks": [], "metadata": {}}), encoding="utf-8")
    monkeypatch.setattr(kb_cache, "_HAS_OPENAT", False)
    with pytest.raises(knowledge_store.KnowledgeStoreError, match="openat"):
        knowledge.KnowledgeBase(str(path))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["knowledge.json"]


def test_rag_load_requires_openat_before_lockfile_creation(monkeypatch, tmp_path):
    import RAG
    import knowledge_store

    path = tmp_path / "knowledge.json"
    path.write_text(json.dumps({"chunks": [], "metadata": {}}), encoding="utf-8")
    monkeypatch.setattr(RAG.kb_cache, "_HAS_OPENAT", False)
    with pytest.raises(knowledge_store.KnowledgeStoreError, match="openat"):
        RAG.load_knowledge_base(path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["knowledge.json"]


def test_ingest_requires_openat_before_context_or_embedding_cache(monkeypatch, tmp_path):
    import RAG
    import context_generation
    import knowledge_store

    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("no context or embedding work without safe cache IO")

    monkeypatch.setattr(RAG.kb_cache, "_HAS_OPENAT", False)
    monkeypatch.setattr(context_generation, "generate_document_context", forbidden)
    monkeypatch.setattr(RAG, "_load_embedding_cache", forbidden)
    document = SimpleNamespace(source="spec.md", chunks=[{"content": "source evidence"}])
    with pytest.raises(knowledge_store.KnowledgeStoreError, match="openat"):
        RAG._commit_document_to_kb(document, str(tmp_path / "knowledge.json"),
                                   generate_context=True)
    for generate in (RAG.generate_embeddings, RAG.generate_gate_embeddings):
        with pytest.raises(knowledge_store.KnowledgeStoreError, match="openat"):
            generate(document.chunks, cache_dir=tmp_path)
    assert calls == []
    assert list(tmp_path.iterdir()) == []
