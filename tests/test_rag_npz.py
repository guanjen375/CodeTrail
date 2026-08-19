"""knowledge.json ↔ knowledge_emb.npz 的向量持久化契約。

合併自 tests/test_rag_incremental.py 與 tests/test_npz_embedding_attach.py(2026-08-20)。
兩份共用同一個 _content_hash helper(語意相同,只留一份)。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import config
from RAG import load_knowledge_base, save_knowledge_base

# numpy 未裝就整份 skip(AGENTS.md §5:離線/缺套件要 graceful skip,不是 collect error)
np = pytest.importorskip("numpy")


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


# --------------------------------------------------------------------------
# 併自 tests/test_npz_embedding_attach.py(P0-5):
# 從 .npz 載入 embeddings 後必須把向量掛回每個 chunk,否則下游 MMR /
# 污染控制 / 信心分數都會拿到空向量,相似度全當 0。
# --------------------------------------------------------------------------
import knowledge  # noqa: E402 - fixture must match the consumer's import-time model
from knowledge import KnowledgeBase  # noqa: E402 - keep optional numpy skip offline-safe

# smoke:AGENTS.md §2.1 第 1 款「真實發生過的 bug 的 regression」
# 真實 bug regression(P0-5):npz 向量沒掛回 chunk → 相似度全 0。
pytestmark = pytest.mark.smoke


def _build_kb_files(tmp_path: Path, n: int = 4, dim: int = 8):
    """造出「knowledge.json（無 inline embedding）+ 相容 .npz」的一組檔案。"""
    chunks = [
        {"content": f"chunk number {i} about spec value {i * 100}",
         "source": "doc.pdf", "type": "text"}
        for i in range(n)
    ]
    json_path = tmp_path / config.KNOWLEDGE_FILE
    json_path.write_text(
        json.dumps({
            "chunks": chunks,
            "metadata": {"embedding_model": knowledge.EMBEDDING_MODEL, "documents": []},
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    # L2-normalized 隨機向量（正規化與真實 .npz 一致）
    rng = np.arange(1, n * dim + 1, dtype=np.float32).reshape(n, dim)
    rng = rng / np.linalg.norm(rng, axis=1, keepdims=True)

    emb_path = tmp_path / config.KNOWLEDGE_EMB_FILE
    np.savez_compressed(
        emb_path,
        embeddings=rng,
        embedding_model=knowledge.EMBEDDING_MODEL,
        chunk_count=n,
        content_hash=_content_hash(chunks),
    )
    return json_path, rng


def test_npz_load_attaches_embeddings_to_chunks(tmp_path: Path):
    json_path, rng = _build_kb_files(tmp_path, n=4, dim=8)

    kb = KnowledgeBase(str(json_path))
    assert kb.loaded

    # self._embeddings 有載到
    assert kb._embeddings is not None
    assert kb._embeddings.shape == (4, 8)

    # 關鍵：每個 chunk 都要拿到非空 embedding，且與 .npz 對應列一致
    for i, chunk in enumerate(kb.chunks):
        emb = chunk.get("embedding")
        assert emb, f"chunk {i} 載入 .npz 後 embedding 仍是空的（P0-5 回歸）"
        assert len(emb) == 8
        assert emb == pytest.approx(rng[i].tolist(), rel=1e-5)


def test_npz_attached_embeddings_drive_nonzero_similarity(tmp_path: Path):
    """回填後，用 chunk["embedding"] 算 cosine 應該拿到非零分數（不再全 0）。"""
    json_path, rng = _build_kb_files(tmp_path, n=3, dim=8)
    kb = KnowledgeBase(str(json_path))

    # 直接用某個 chunk 自己的向量當 query，cosine 應接近 1（而非 0）
    q = kb.chunks[1]["embedding"]
    sims = [kb._cosine_similarity(q, c["embedding"]) for c in kb.chunks]
    assert max(sims) == pytest.approx(1.0, abs=1e-4)
    # 自我相似度必須是最高的那個
    assert sims.index(max(sims)) == 1
