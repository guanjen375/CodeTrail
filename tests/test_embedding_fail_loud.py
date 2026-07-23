from __future__ import annotations

import json

import pytest

import RAG
import code_rag
import knowledge


EMBEDDING_MODULES = (code_rag, knowledge)


@pytest.fixture(autouse=True)
def clear_embedding_lru_caches():
    for module in EMBEDDING_MODULES:
        module._cached_get_embedding.cache_clear()
    yield
    for module in EMBEDDING_MODULES:
        module._cached_get_embedding.cache_clear()


def _assert_actionable_error(exc: pytest.ExceptionInfo[RuntimeError], module) -> None:
    message = str(exc.value)
    assert module.LLAMA_EMBED_BASE_URL in message
    assert "8081 llama-server" in message
    assert "AICODE_LLAMA_EMBED_BASE_URL" in message


def test_code_rag_query_raises_when_embedding_server_is_unreachable(monkeypatch, tmp_path):
    rag = code_rag.CodeRAG(str(tmp_path))
    rag.index = [
        {
            "path": "sample.py",
            "symbol": "target_symbol",
            "type": "function",
            "line": 1,
            "context": "def target_symbol(): pass",
            "embedding": [1.0, 0.0],
        }
    ]
    monkeypatch.setattr(
        code_rag.llama_client,
        "embed_one",
        lambda **kwargs: (_ for _ in ()).throw(ConnectionError("connection refused")),
    )

    with pytest.raises(RuntimeError, match="embedding server unreachable") as exc:
        rag.query("target_symbol")

    _assert_actionable_error(exc, code_rag)


def test_code_rag_lazy_embedding_retries_after_server_recovers(monkeypatch, tmp_path):
    rag = code_rag.CodeRAG(str(tmp_path))
    rag.index = [
        {
            "path": "sample.py",
            "symbol": "target_symbol",
            "type": "function",
            "line": 1,
            "context": "def target_symbol(): pass",
        }
    ]
    rag._lazy_embed = True
    rag._lazy_embed_top_k = 1
    attempts = 0

    def flaky_embed(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise ConnectionError("temporary lazy-embed outage")
        return [1.0, 0.0]

    monkeypatch.setattr(code_rag.llama_client, "embed_one", flaky_embed)

    with pytest.raises(RuntimeError, match="temporary lazy-embed outage"):
        rag.query("target_symbol", top_k=1)

    results = rag.query("target_symbol", top_k=1)
    assert results[0]["symbol"] == "target_symbol"
    assert attempts == 3


def test_code_rag_build_failure_does_not_leave_partial_index(monkeypatch, tmp_path):
    rag = code_rag.CodeRAG(str(tmp_path))
    files = {}
    for name in ("a.py", "b.py"):
        path = tmp_path / name
        path.write_text("def target():\n    pass\n", encoding="utf-8")
        files[name] = {"filepath": path, "hash": name}

    outage = True

    def fake_index(filepath, rel_path, compute_embeddings=True):
        if rel_path == "b.py" and outage:
            raise RuntimeError("embedding server unreachable at test URL")
        return (
            [
                {
                    "path": rel_path,
                    "symbol": f"target_{rel_path[0]}",
                    "type": "function",
                    "line": 1,
                    "context": "def target(): pass",
                }
            ],
            [[1.0, 0.0]],
        )

    monkeypatch.setattr(rag, "_load_cache", lambda: False)
    monkeypatch.setattr(rag, "_scan_code_files", lambda: files)
    monkeypatch.setattr(rag, "_index_single_file", fake_index)
    monkeypatch.setattr(rag, "_save_cache", lambda: None)
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", False)

    with pytest.raises(RuntimeError, match="embedding server unreachable"):
        rag.build_index(verbose=False)

    assert rag.index == []

    outage = False
    rag.build_index(verbose=False)
    assert [item["path"] for item in rag.index] == ["a.py", "b.py"]


def test_knowledge_query_raises_when_embedding_server_is_unreachable(monkeypatch, tmp_path):
    kb = knowledge.KnowledgeBase(str(tmp_path / "missing.json"))
    kb.loaded = True
    kb.chunks = [
        {
            "id": "chunk-1",
            "source": "manual.md",
            "content": "target behavior",
            "embedding": [1.0, 0.0],
        }
    ]
    monkeypatch.setattr(
        knowledge.llama_client,
        "embed_one",
        lambda **kwargs: (_ for _ in ()).throw(ConnectionError("connection refused")),
    )

    with pytest.raises(RuntimeError, match="embedding server unreachable") as exc:
        kb.query("target behavior")

    _assert_actionable_error(exc, knowledge)


@pytest.mark.parametrize("module", EMBEDDING_MODULES)
def test_empty_embedding_vector_raises(module, monkeypatch):
    monkeypatch.setattr(module.llama_client, "embed_one", lambda **kwargs: [])

    with pytest.raises(RuntimeError, match="returned an empty vector") as exc:
        module._cached_get_embedding("empty-vector-probe")

    _assert_actionable_error(exc, module)


@pytest.mark.parametrize("module", EMBEDDING_MODULES)
def test_embedding_exception_is_not_cached_and_retry_succeeds(module, monkeypatch):
    attempts = 0

    def flaky_embed(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("temporary outage")
        return [0.25, 0.5]

    monkeypatch.setattr(module.llama_client, "embed_one", flaky_embed)

    with pytest.raises(RuntimeError, match="temporary outage"):
        module._cached_get_embedding("same-query-after-recovery")

    assert module._cached_get_embedding("same-query-after-recovery") == (0.25, 0.5)
    assert attempts == 2


def test_rag_ingestion_raises_instead_of_writing_empty_embedding(monkeypatch, tmp_path):
    chunks = [{"content": "document chunk"}]
    monkeypatch.setattr(
        RAG.llama_client,
        "embed_one",
        lambda **kwargs: (_ for _ in ()).throw(ConnectionError("connection refused")),
    )

    with pytest.raises(RuntimeError, match="embedding server unreachable") as exc:
        RAG.generate_embeddings(chunks, cache_dir=tmp_path)

    _assert_actionable_error(exc, RAG)
    assert "embedding" not in chunks[0]


def test_rag_ingestion_retries_legacy_empty_disk_cache(monkeypatch, tmp_path):
    content = "document chunk"
    cache_path = tmp_path / RAG.EMBEDDING_CACHE_FILE
    cache_path.write_text(
        json.dumps(
            {
                "model": RAG.EMBEDDING_MODEL,
                "cache": {RAG._content_hash(content): []},
            }
        ),
        encoding="utf-8",
    )
    calls = 0

    def recovered_embed(**kwargs):
        nonlocal calls
        calls += 1
        return [0.25, 0.5]

    monkeypatch.setattr(RAG.llama_client, "embed_one", recovered_embed)
    chunks = RAG.generate_embeddings([{"content": content}], cache_dir=tmp_path)

    assert calls == 1
    assert chunks[0]["embedding"] == [0.25, 0.5]
