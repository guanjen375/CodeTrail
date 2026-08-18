"""MMR 不得蓋掉 cross-encoder 的排序（G1）。

以前的順序是：rerank 排好 → MMR 再用 `cosine(query, chunk_embedding)` 重算相關度
→ 等於把 cross-encoder 的結果整份丟掉，reranker 實際上只剩「篩候選」的作用。
真實 spec 上重現過：reranker 排第一的 chunk 被 MMR 直接剔除，換上一段雜訊。

現在 rerank 分數會一路帶到 MMR 當相關度，embedding 只負責算多樣性懲罰。
沒有走到 cross-encoder 的路徑（跳過 rerank、fallback）分數是 None，MMR 退回
原本的 embedding 相關度——那條路徑的行為必須保持不變。

離線，不碰任何 server。
"""
from __future__ import annotations

from pathlib import Path

import knowledge
from knowledge import Candidate, KnowledgeBase


def _chunk(chunk_id: str, embedding: list, *, content: str = "") -> dict:
    return {
        "id": chunk_id,
        "source": "spec_a.md",
        "page": 1,
        "chunk_index": 0,
        "type": "spec",
        "section": f"section-{chunk_id}",
        "content": content or f"chunk {chunk_id} 的內容夠長可以通過噪音過濾。" * 3,
        "embedding": embedding,
        "chunk_idx": 0,
    }


def _kb(tmp_path: Path) -> KnowledgeBase:
    return KnowledgeBase(str(tmp_path / "missing.json"))


def test_mmr_keeps_the_reranker_winner_even_with_low_embedding_similarity(tmp_path: Path):
    """rerank 第一名的 embedding 相似度較低時，仍然必須是 MMR 的第一名。"""
    kb = _kb(tmp_path)
    query_vector = [1.0, 0.0]
    aligned = _chunk("aligned", [1.0, 0.0])      # 與 query 對齊，但 rerank 分數低
    winner = _chunk("winner", [0.0, 1.0])        # 與 query 正交，但 rerank 分數高

    selected = kb._mmr_select(
        [winner, aligned], query_vector, k=1, relevance=[9.9, 0.1]
    )

    assert [c["id"] for c in selected] == ["winner"]


def test_mmr_without_reranker_scores_keeps_the_old_embedding_behaviour(tmp_path: Path):
    """沒有 cross-encoder 分數時（跳過 rerank / fallback）行為不得改變。"""
    kb = _kb(tmp_path)
    query_vector = [1.0, 0.0]
    aligned = _chunk("aligned", [1.0, 0.0])
    orthogonal = _chunk("orthogonal", [0.0, 1.0])

    selected = kb._mmr_select([orthogonal, aligned], query_vector, k=1)

    assert [c["id"] for c in selected] == ["aligned"]


def test_partial_relevance_falls_back_instead_of_mixing_scales(tmp_path: Path):
    """半套的 relevance 比沒有更糟：任何一項缺分數就整批退回 embedding 相關度。"""
    kb = _kb(tmp_path)
    query_vector = [1.0, 0.0]
    aligned = _chunk("aligned", [1.0, 0.0])
    orthogonal = _chunk("orthogonal", [0.0, 1.0])

    selected = kb._mmr_select(
        [orthogonal, aligned], query_vector, k=1, relevance=[9.9, None]
    )

    assert [c["id"] for c in selected] == ["aligned"]


def test_relevance_is_min_max_normalized_to_match_the_diversity_penalty():
    """reranker 回的是 logit（可能是負的），要壓到 [0,1] 才跟餘弦同量級。"""
    normalize = KnowledgeBase._normalized_relevance

    assert normalize([-8.0, 0.0, 2.0]) == [0.0, 0.8, 1.0]
    assert normalize([3.0, 3.0]) == [1.0, 1.0]      # 全部同分 → 退化成純多樣性
    assert normalize(None) is None
    assert normalize([1.0, None]) is None


def test_diversity_breaks_ties_within_the_reranker_order(tmp_path: Path):
    """相關度換成 rerank 分數之後，多樣性懲罰仍然有效——但只在相關度打平時決勝。

    後兩者 rerank 同分（正規化後都是 0），此時與已選項完全同向的那個會吃到
    多樣性懲罰而落後。rerank 分數拉得開時就該由 rerank 決定，那正是這次要修的。
    """
    kb = _kb(tmp_path)
    query_vector = [1.0, 0.0]
    first = _chunk("first", [1.0, 0.0])
    near_duplicate = _chunk("near_duplicate", [1.0, 0.0])   # 與 first 完全同向
    diverse = _chunk("diverse", [0.0, 1.0])

    selected = kb._mmr_select(
        [first, near_duplicate, diverse], query_vector, k=2,
        relevance=[10.0, 9.0, 9.0],
    )

    assert selected[0]["id"] == "first"
    assert selected[1]["id"] == "diverse", "近乎重複的 chunk 應該被多樣性懲罰壓下去"


def test_rerank_returns_scores_alongside_chunks(monkeypatch, tmp_path: Path):
    """cross-encoder 路徑要把分數帶出來，不能像以前那樣排完就丟掉。"""
    kb = _kb(tmp_path)
    candidates = [
        Candidate(chunk_idx=i, chunk=_chunk(str(i), [1.0, 0.0]),
                  rrf_score=0.03, retrieval_score=0.4, gate_score=0.4)
        for i in range(3)
    ]
    monkeypatch.setattr(kb, "_check_reranker_available", lambda: True)
    monkeypatch.setattr(
        knowledge.llama_client, "rerank",
        lambda **kwargs: [1.0, 5.0, 3.0][: len(kwargs["documents"])],
    )

    ranked = kb._rerank_with_model("question", candidates, top_k=3, is_strict_mode=True)

    assert [chunk["id"] for _score, chunk in ranked] == ["1", "2", "0"]
    assert [score for score, _chunk in ranked] == [5.0, 3.0, 1.0]


def test_skipped_rerank_reports_no_scores(monkeypatch, tmp_path: Path):
    kb = _kb(tmp_path)
    candidates = [
        Candidate(chunk_idx=i, chunk=_chunk(str(i), [1.0, 0.0]),
                  rrf_score=0.03, retrieval_score=0.9, gate_score=0.9)
        for i in range(3)
    ]
    monkeypatch.setattr(knowledge, "RERANKER_ALWAYS_ON", False)
    monkeypatch.setattr(knowledge, "RERANKER_SKIP_THRESHOLD", 0.5)

    ranked = kb._rerank_with_model("question", candidates, top_k=3)

    assert all(score is None for score, _chunk in ranked)
