from __future__ import annotations

import hashlib
import json

import numpy as np

import config
from RAG import load_knowledge_base, save_knowledge_base


def _content_hash(chunks: list[dict]) -> str:
    hasher = hashlib.md5()
    for chunk in chunks:
        hasher.update(chunk.get("content", "").encode("utf-8"))
    return hasher.hexdigest()


def test_load_knowledge_base_restores_external_npz_embeddings(tmp_path):
    kb_path = tmp_path / "knowledge.json"
    chunks = [
        {"source": "old.md", "page": 1, "chunk_index": 0, "content": "alpha"},
        {"source": "old.md", "page": 1, "chunk_index": 1, "content": "beta"},
    ]
    kb_path.write_text(
        json.dumps({"metadata": {"documents": ["old.md"]}, "chunks": chunks}, ensure_ascii=False),
        encoding="utf-8",
    )
    np.savez_compressed(
        tmp_path / config.KNOWLEDGE_EMB_FILE,
        embeddings=np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
        embedding_model=config.EMBEDDING_MODEL,
        chunk_count=2,
        content_hash=_content_hash(chunks),
    )

    kb = load_knowledge_base(kb_path)

    assert kb["chunks"][0]["embedding"] == [1.0, 0.0]
    assert kb["chunks"][1]["embedding"] == [0.0, 1.0]


def test_incremental_save_preserves_old_embeddings_from_npz(tmp_path):
    kb_path = tmp_path / "knowledge.json"
    old_chunks = [
        {"source": "old.md", "page": 1, "chunk_index": 0, "content": "alpha"},
        {"source": "old.md", "page": 1, "chunk_index": 1, "content": "beta"},
    ]
    kb_path.write_text(
        json.dumps(
            {
                "metadata": {
                    "documents": ["old.md"],
                    "total_documents": 1,
                    "total_chunks": 2,
                },
                "chunks": old_chunks,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    np.savez_compressed(
        tmp_path / config.KNOWLEDGE_EMB_FILE,
        embeddings=np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
        embedding_model=config.EMBEDDING_MODEL,
        chunk_count=2,
        content_hash=_content_hash(old_chunks),
    )
    kb = load_knowledge_base(kb_path)
    kb["chunks"].append(
        {
            "source": "new.md",
            "page": 1,
            "chunk_index": 0,
            "content": "gamma",
            "embedding": [1.0, 1.0],
        }
    )
    kb["metadata"]["documents"].append("new.md")

    save_knowledge_base(kb, kb_path)

    data = np.load(tmp_path / config.KNOWLEDGE_EMB_FILE)
    embeddings = data["embeddings"]
    assert embeddings.shape == (3, 2)
    assert np.allclose(embeddings[0], [1.0, 0.0])
    assert np.allclose(embeddings[1], [0.0, 1.0])
    assert np.allclose(embeddings[2], [0.70710677, 0.70710677])


def test_save_empty_knowledge_base_removes_stale_npz(tmp_path):
    kb_path = tmp_path / "knowledge.json"
    stale_npz = tmp_path / config.KNOWLEDGE_EMB_FILE
    np.savez_compressed(
        stale_npz,
        embeddings=np.array([[1.0]], dtype=np.float32),
        embedding_model=config.EMBEDDING_MODEL,
        chunk_count=1,
        content_hash="stale",
    )

    save_knowledge_base(
        {
            "metadata": {
                "created_at": "now",
                "embedding_model": config.EMBEDDING_MODEL,
                "chunk_size": 1200,
                "documents": [],
            },
            "chunks": [],
        },
        kb_path,
    )

    assert kb_path.exists()
    assert not stale_npz.exists()
