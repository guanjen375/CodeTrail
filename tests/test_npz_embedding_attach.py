"""P0-5：從 .npz 載入 embeddings 後，必須把向量掛回每個 chunk。

RAG 存 knowledge.json 時為了體積不再 inline embedding（只留 knowledge_emb.npz）。
若載入 .npz 只設 self._embeddings 而不回填 chunk["embedding"]，下游 MMR / 污染控制
/ 信心分數（都讀 chunk.get("embedding")）會一律拿到空向量，相似度全當 0，最後只回
空的 [REF]…[/REF] 殼。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

import config
from knowledge import KnowledgeBase


def _content_hash(chunks) -> str:
    h = hashlib.md5()
    for c in chunks:
        h.update(c.get("content", "").encode("utf-8"))
    return h.hexdigest()


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
            "metadata": {"embedding_model": config.EMBEDDING_MODEL, "documents": []},
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
        embedding_model=config.EMBEDDING_MODEL,
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
