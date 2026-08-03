from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

import config
import RAG
import knowledge_store
from knowledge import KnowledgeBase


def _kb() -> dict:
    return {
        "metadata": {"documents": ["keep.md", "drop.md"]},
        "chunks": [
            {
                "source": "keep.md",
                "page": 1,
                "chunk_index": 0,
                "content": "KEEP register address 0x1000 and reset value 32.",
                "embedding": [1.0, 0.0],
            },
            {
                "source": "drop.md",
                "page": 1,
                "chunk_index": 0,
                "content": "DROP register address 0x2000 and reset value 64.",
                "embedding": [0.0, 1.0],
            },
        ],
    }


def test_incremental_restore_rejects_embedding_model_mismatch(tmp_path: Path):
    path = tmp_path / config.KNOWLEDGE_FILE
    kb = _kb()
    path.write_text(
        json.dumps({"metadata": kb["metadata"], "chunks": [{k: v for k, v in c.items() if k != "embedding"} for c in kb["chunks"]]}),
        encoding="utf-8",
    )
    np.savez_compressed(
        tmp_path / config.KNOWLEDGE_EMB_FILE,
        embeddings=np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
        embedding_model="different-embedding-model",
        chunk_count=2,
        content_hash=RAG._chunks_content_hash(kb["chunks"]),
    )

    with pytest.raises(RuntimeError, match="embedding model"):
        RAG.load_knowledge_base(path)


def test_save_rejects_mixed_dimensions_without_mutating_or_overwriting(tmp_path: Path):
    path = tmp_path / config.KNOWLEDGE_FILE
    path.write_text('{"sentinel": true}', encoding="utf-8")
    before = path.read_bytes()
    kb = _kb()
    kb["chunks"][1]["embedding"] = [0.0, 1.0, 2.0]

    with pytest.raises(RuntimeError, match="dimension"):
        RAG.save_knowledge_base(kb, path)

    assert path.read_bytes() == before
    assert all(chunk.get("embedding") for chunk in kb["chunks"]), "save must not pop caller vectors"


def test_atomic_pair_write_rolls_back_npz_before_unlock_on_json_publish_failure(
    monkeypatch, tmp_path: Path
):
    path = tmp_path / config.KNOWLEDGE_FILE
    embeddings_path = tmp_path / config.KNOWLEDGE_EMB_FILE
    RAG.save_knowledge_base(_kb(), path)
    original_json = path.read_bytes()
    original_npz = embeddings_path.read_bytes()

    changed = _kb()
    changed["chunks"][0]["content"] = "replacement content"
    real_replace = knowledge_store.os.replace

    def fail_json_publish(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path == path and ".tmp." in source_path.name:
            raise OSError("injected JSON publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr(knowledge_store.os, "replace", fail_json_publish)

    with pytest.raises(OSError, match="injected JSON publish failure"):
        RAG.save_knowledge_base(changed, path)

    assert path.read_bytes() == original_json
    assert embeddings_path.read_bytes() == original_npz
    assert not any("rollback" in item.name or ".tmp." in item.name for item in tmp_path.iterdir())


def test_remove_document_rewrites_remaining_npz_and_reload_keeps_dense_search(monkeypatch, tmp_path: Path):
    path = tmp_path / config.KNOWLEDGE_FILE
    kb = _kb()
    RAG.save_knowledge_base(kb, path)
    remove = getattr(RAG, "remove_document_from_knowledge_base", None)
    assert callable(remove), "RAG must expose the shared transactional removal path"

    result = remove(path, "drop.md")
    assert result["removed_chunks"] == 1

    data = np.load(tmp_path / config.KNOWLEDGE_EMB_FILE)
    assert data["embeddings"].shape == (1, 2)
    loaded = KnowledgeBase(str(path))
    assert loaded.loaded
    assert [c["source"] for c in loaded.chunks] == ["keep.md"]
    assert loaded._embeddings is not None
    assert loaded._embeddings.shape == (1, 2)

    monkeypatch.setattr(loaded, "_get_embedding", lambda _text: [1.0, 0.0])
    rows = loaded._hybrid_search("KEEP 0x1000", candidate_k=5)
    assert rows and rows[0][3]["source"] == "keep.md"


def test_remove_aborts_if_vectors_are_missing_and_leaves_json_unchanged(tmp_path: Path):
    path = tmp_path / config.KNOWLEDGE_FILE
    kb = _kb()
    RAG.save_knowledge_base(kb, path)
    (tmp_path / config.KNOWLEDGE_EMB_FILE).unlink()
    before = path.read_bytes()
    remove = getattr(RAG, "remove_document_from_knowledge_base", None)
    assert callable(remove)

    with pytest.raises(RuntimeError, match="embedding"):
        remove(path, "drop.md")

    assert path.read_bytes() == before
