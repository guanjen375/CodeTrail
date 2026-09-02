"""RAG 檢索端:reranker / MMR、檢索回歸、雙訊號(context_signals)與 strict KB 拒答閘。

合併自三份測試(2026-09-02),各自的脈絡分段保留如下。smoke 成員資格逐條保留:只有
test_contextual_signals.py 有逐條標的 smoke(拒答閘與 reranker passage 那幾條),
另外兩份沒有 → 本檔不用 module 層 pytestmark。

test_rag_rerank.py —— 重排序:rerank policy(何時呼叫、失敗如何降級)與 MMR 不得蓋掉
reranker 名次(它本身合併自 test_rag_rerank_policy.py 與 test_rerank_mmr_relevance.py,
2026-08-20)。

test_rag_retrieval_regressions.py —— 檢索回歸:BM25 tokenizer 保留數值／hex 字面、
hybrid search 的 BM25-only 候選、multi-query 不得覆寫 RRF 尺度、reranker 看到完整 pool
與晚段文字、source filter 先於 top_k、overlap 前綴不重複計入 BM25、embedding 輸入含
source、strict MCP 查詢必須把 is_strict_mode 傳進檢索。

test_contextual_signals.py —— 雙訊號(retrieval 含 ctx / gate 只看原文)的不變式回歸測試。
核心不變式(規格 §2):`ctx` 是 LLM 生成物,只准影響「哪些 chunk 被撈上來、排第幾」。
它不可以出現在任何證據文本,也不可以讓它抬高的**分數**通過決策門檻——後者是分數面的
循環 grounding:錯誤脈絡替弱原文背書。這一批測的是「訊號的形狀與去向」:組字、schema、
儲存、載入、六個決策點。生成端(窗、快取、安全、CLI)在 tests/test_rag_ingest.py
(原 tests/test_context_generation.py)。語料全部合成(`spec_a.md` / `toolchain_x`),
離線,不碰任何 server。
"""
from __future__ import annotations

import ast
import builtins
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import code_rag
import config
import context_signals
import knowledge
import RAG
import utils
from knowledge import Candidate, KnowledgeBase
from knowledge_store import KnowledgeStoreError


# ── 原 test_rag_rerank.py:rerank policy 與 MMR 不得蓋掉 reranker 名次 ──
# 本區段的 chunk helper 原名 `_chunk`,與下面兩個區段同名但簽章不同,改名 `_chunk_rerank`。
def _kb_candidates():
    rows = [(0.30, "a", "alpha"), (0.29, "b", "beta"), (0.28, "c", "gamma")]
    return [
        knowledge.Candidate(
            chunk_idx=i,
            chunk={"id": chunk_id, "content": content, "chunk_idx": i},
            rrf_score=score,
            retrieval_score=score,
            gate_score=score,
        )
        for i, (score, chunk_id, content) in enumerate(rows)
    ]


def test_knowledge_rerank_policy_embedding_does_not_call_main_model(monkeypatch, tmp_path):
    kb = knowledge.KnowledgeBase(str(tmp_path / "missing.json"))
    candidates = _kb_candidates()
    monkeypatch.setattr(config, "RERANK_FALLBACK_POLICY", "embedding")
    monkeypatch.setattr(kb, "_check_reranker_available", lambda: False)

    def fail_llm(*args, **kwargs):
        raise AssertionError("main model rerank must not be called")

    monkeypatch.setattr(kb, "_rerank_with_llm", fail_llm)

    out = kb._rerank_with_model("question", candidates, top_k=2, is_strict_mode=True)

    assert [chunk for _score, chunk in out] == [candidates[0].chunk, candidates[1].chunk]


def test_knowledge_rerank_policy_main_model_calls_llm(monkeypatch, tmp_path):
    kb = knowledge.KnowledgeBase(str(tmp_path / "missing.json"))
    candidates = _kb_candidates()
    sentinel = [{"id": "llm"}]
    called = {"value": False}
    monkeypatch.setattr(config, "RERANK_FALLBACK_POLICY", "main_model")
    monkeypatch.setattr(kb, "_check_reranker_available", lambda: False)

    def fake_llm(question, got_candidates, top_k):
        called["value"] = True
        assert got_candidates is candidates
        assert top_k == 2
        return sentinel

    monkeypatch.setattr(kb, "_rerank_with_llm", fake_llm)

    out = kb._rerank_with_model("question", candidates, top_k=2, is_strict_mode=True)
    assert [chunk for _score, chunk in out] == sentinel
    assert called["value"] is True
    assert called["value"] is True


def test_knowledge_rerank_policy_error_raises_when_unavailable(monkeypatch, tmp_path):
    kb = knowledge.KnowledgeBase(str(tmp_path / "missing.json"))
    monkeypatch.setattr(config, "RERANK_FALLBACK_POLICY", "error")
    monkeypatch.setattr(kb, "_check_reranker_available", lambda: False)

    with pytest.raises(RuntimeError, match="RAG reranker unavailable"):
        kb._rerank_with_model("question", _kb_candidates(), top_k=2, is_strict_mode=True)


def test_knowledge_rerank_policy_embedding_handles_rerank_exception(monkeypatch, tmp_path):
    kb = knowledge.KnowledgeBase(str(tmp_path / "missing.json"))
    candidates = _kb_candidates()
    monkeypatch.setattr(config, "RERANK_FALLBACK_POLICY", "embedding")
    monkeypatch.setattr(kb, "_check_reranker_available", lambda: True)
    monkeypatch.setattr(knowledge.llama_client, "rerank", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    def fail_llm(*args, **kwargs):
        raise AssertionError("main model rerank must not be called")

    monkeypatch.setattr(kb, "_rerank_with_llm", fail_llm)

    out = kb._rerank_with_model("question", candidates, top_k=2, is_strict_mode=True)

    assert [chunk for _score, chunk in out] == [candidates[0].chunk, candidates[1].chunk]


def _code_candidates():
    return [
        (0.30, 0.30, 0.0, {"symbol": "a", "path": "a.py", "context": "alpha"}),
        (0.29, 0.29, 0.0, {"symbol": "b", "path": "b.py", "context": "beta"}),
        (0.28, 0.28, 0.0, {"symbol": "c", "path": "c.py", "context": "gamma"}),
    ]


def test_code_rag_error_policy_raises_when_reranker_unavailable(monkeypatch, tmp_path):
    rag = code_rag.CodeRAG(str(tmp_path))
    monkeypatch.setattr(config, "RERANK_FALLBACK_POLICY", "error")
    monkeypatch.setattr(rag, "_check_reranker_available", lambda: False)

    with pytest.raises(RuntimeError, match="Code RAG reranker unavailable"):
        rag._rerank_code_candidates("question", _code_candidates(), top_k=2)


def test_code_rag_main_model_policy_keeps_embedding_order(monkeypatch, tmp_path):
    rag = code_rag.CodeRAG(str(tmp_path))
    candidates = _code_candidates()
    monkeypatch.setattr(config, "RERANK_FALLBACK_POLICY", "main_model")
    monkeypatch.setattr(rag, "_check_reranker_available", lambda: False)

    out = rag._rerank_code_candidates("question", candidates, top_k=2)
    # §5-1:未 rerank 走 fusion —— rerank_score 必須是 None(不是 0.0),
    # final_score = combined,順序保持 embedding/fusion 排序。
    assert [rc.item for rc in out] == [candidates[0][3], candidates[1][3]]
    assert all(rc.score_source == "fusion" for rc in out)
    assert all(rc.rerank_score is None for rc in out)
    assert [rc.final_score for rc in out] == [candidates[0][0], candidates[1][0]]


# --------------------------------------------------------------------------
# 併自 tests/test_rerank_mmr_relevance.py:MMR 與 reranker 的互動(G1 回歸)。
# --------------------------------------------------------------------------
def _chunk_rerank(chunk_id: str, embedding: list, *, content: str = "") -> dict:
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


@pytest.mark.parametrize(
    "other_id,mmr_kwargs,expected",
    [
        # rerank 第一名的 embedding 相似度較低時，仍然必須是 MMR 的第一名：
        # aligned 與 query 對齊但 rerank 分數低；winner 與 query 正交但 rerank 分數高。
        ("winner", {"relevance": [9.9, 0.1]}, "winner"),
        # 沒有 cross-encoder 分數時（跳過 rerank / fallback）行為不得改變。
        ("orthogonal", {}, "aligned"),
        # 半套的 relevance 比沒有更糟：任何一項缺分數就整批退回 embedding 相關度。
        ("orthogonal", {"relevance": [9.9, None]}, "aligned"),
    ],
    ids=[
        "reranker_winner_beats_low_embedding_similarity",
        "no_reranker_scores_keeps_the_old_embedding_behaviour",
        "partial_relevance_falls_back_instead_of_mixing_scales",
    ],
)
def test_mmr_first_pick_follows_complete_reranker_scores_only(
    tmp_path: Path, other_id: str, mmr_kwargs: dict, expected: str
):
    """MMR 的第一名由誰決定（G1 回歸）：

    - 有完整的 rerank 分數 → rerank 第一名（即使它的 embedding 相似度較低）仍然必須是
      MMR 的第一名。
    - 沒有 cross-encoder 分數（跳過 rerank / fallback）→ 行為不得改變，仍是 embedding
      對齊的那個。
    - 半套的 relevance 比沒有更糟：任何一項缺分數就整批退回 embedding 相關度。

    原 test_mmr_keeps_the_reranker_winner_even_with_low_embedding_similarity /
    test_mmr_without_reranker_scores_keeps_the_old_embedding_behaviour /
    test_partial_relevance_falls_back_instead_of_mixing_scales（2026-09-02 折成 parametrize）。
    """
    kb = _kb(tmp_path)
    query_vector = [1.0, 0.0]
    aligned = _chunk_rerank("aligned", [1.0, 0.0])      # 與 query 對齊
    other = _chunk_rerank(other_id, [0.0, 1.0])         # 與 query 正交

    selected = kb._mmr_select([other, aligned], query_vector, k=1, **mmr_kwargs)

    assert [c["id"] for c in selected] == [expected]


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
    first = _chunk_rerank("first", [1.0, 0.0])
    near_duplicate = _chunk_rerank("near_duplicate", [1.0, 0.0])   # 與 first 完全同向
    diverse = _chunk_rerank("diverse", [0.0, 1.0])

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
        Candidate(chunk_idx=i, chunk=_chunk_rerank(str(i), [1.0, 0.0]),
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
        Candidate(chunk_idx=i, chunk=_chunk_rerank(str(i), [1.0, 0.0]),
                  rrf_score=0.03, retrieval_score=0.9, gate_score=0.9)
        for i in range(3)
    ]
    monkeypatch.setattr(knowledge, "RERANKER_ALWAYS_ON", False)
    monkeypatch.setattr(knowledge, "RERANKER_SKIP_THRESHOLD", 0.5)

    ranked = kb._rerank_with_model("question", candidates, top_k=3)

    assert all(score is None for score, _chunk in ranked)


# ── 原 test_rag_retrieval_regressions.py:BM25 / hybrid search / multi-query / reranker pool 的檢索回歸 ──
# 本區段的 chunk helper 原名 `_chunk`,改名 `_chunk_regression`(理由同上)。
def _chunk_regression(chunk_id: str, content: str, *, source: str = "spec.md", embedding=None) -> dict:
    return {
        "id": chunk_id,
        "source": source,
        "page": 1,
        "chunk_index": 0,
        "type": "spec",
        "content": content,
        "embedding": embedding or [1.0, 0.0],
    }


def _loaded_kb(tmp_path: Path, chunks: list[dict]) -> knowledge.KnowledgeBase:
    kb = knowledge.KnowledgeBase(str(tmp_path / "missing.json"))
    kb.loaded = True
    kb.chunks = chunks
    kb.documents = sorted({c["source"] for c in chunks})
    kb._precompute_embeddings()
    kb._precompute_bm25_index()
    return kb


@pytest.mark.parametrize(
    "token", ["4096", "32", "0x4000", "0x1C", "7", "2.3.1", "v2.3.1"]
)
def test_bm25_tokenizer_preserves_numeric_and_hex_literals(tmp_path: Path, token: str):
    kb = knowledge.KnowledgeBase(str(tmp_path / "missing.json"))
    assert token.lower() in kb._tokenize_for_bm25(f"offset {token} value")


def test_hybrid_search_keeps_bm25_only_results(monkeypatch, tmp_path: Path):
    chunks = [
        _chunk_regression("target", "Register aperture starts at exact offset 0x4000 and spans 4096 bytes."),
        _chunk_regression("noise", "General accelerator overview with no address literal."),
    ]
    kb = _loaded_kb(tmp_path, chunks)
    monkeypatch.setattr(kb, "_get_embedding", lambda _text: [1.0, 0.0])
    monkeypatch.setattr(kb, "_embedding_search_numpy", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(kb, "_generate_multi_queries", lambda question: [question])

    results = kb._hybrid_search("0x4000", candidate_k=5)

    assert results
    assert results[0].chunk["id"] == "target"
    assert results[0].retrieval_bm25 > 0


def test_exact_literal_bm25_candidate_survives_dense_threshold(monkeypatch, tmp_path: Path):
    target = _chunk_regression(
        "target",
        "Register CTRL_ADDR is located at 0x4000. This authoritative sentence is long enough for filtering.",
        embedding=[0.0, 1.0],
    )
    kb = _loaded_kb(tmp_path, [target])
    monkeypatch.setattr(
        kb,
        "_hybrid_search",
        lambda *_args, **_kwargs: [
            knowledge.Candidate(
                chunk_idx=0, chunk=target, rrf_score=0.02,
                retrieval_bm25=1.0, gate_bm25=1.0,
            )
        ],
    )
    monkeypatch.setattr(
        kb,
        "_rerank_with_model",
        lambda _q, candidates, _top_k, **_kw: [(None, c.chunk) for c in candidates],
    )
    monkeypatch.setattr(kb, "_get_embedding", lambda _text: [1.0, 0.0])
    monkeypatch.setattr(knowledge, "USE_MMR", False)

    _model, _display, meta = kb.query("CTRL_ADDR 的 offset 是不是 0x4000？")

    assert meta["has_ref"] is True
    assert meta["refs"][0]["source"] == "spec.md"


def test_multi_query_adds_new_candidates_without_overwriting_rrf_scale(monkeypatch, tmp_path: Path):
    first = _chunk_regression("first", "alpha control path", embedding=[1.0, 0.0])
    rescued = _chunk_regression("rescued", "translated semantic reset path", embedding=[0.0, 1.0])
    kb = _loaded_kb(tmp_path, [first, rescued])
    monkeypatch.setattr(kb, "_get_embedding", lambda text: [0.0, 1.0] if text == "variant" else [1.0, 0.0])
    monkeypatch.setattr(kb, "_generate_multi_queries", lambda _question: ["original", "variant"])
    monkeypatch.setattr(kb, "_should_expand_query", lambda *_args, **_kwargs: True)

    results = kb._hybrid_search("original", candidate_k=2)
    ids = [row.chunk["id"] for row in results]

    assert "rescued" in ids
    assert max(row.rrf_score for row in results) < 0.2, "RRF score must not be replaced by cosine/linear score"


def test_reranker_sees_full_pool_and_late_passage_text(monkeypatch, tmp_path: Path):
    kb = knowledge.KnowledgeBase(str(tmp_path / "missing.json"))
    candidates = []
    for i in range(30):
        marker = "LATE_NUMERIC_FACT_0xBEEF" if i == 20 else f"item-{i}"
        content = ("prefix " * 160) + marker
        candidates.append(knowledge.Candidate(
            chunk_idx=i, chunk=_chunk_regression(str(i), content),
            rrf_score=0.03, retrieval_score=0.4, gate_score=0.4,
        ))

    captured: list[str] = []
    monkeypatch.setattr(kb, "_check_reranker_available", lambda: True)

    def fake_rerank(**kwargs):
        captured.extend(kwargs["documents"])
        return [10.0 if "LATE_NUMERIC_FACT_0xBEEF" in doc else 0.0 for doc in kwargs["documents"]]

    monkeypatch.setattr(knowledge.llama_client, "rerank", fake_rerank)

    results = kb._rerank_with_model("find the late fact", candidates, top_k=12, is_strict_mode=True)

    assert len(captured) == 30
    assert any("LATE_NUMERIC_FACT_0xBEEF" in doc for doc in captured)
    assert results[0][1]["id"] == "20"


def test_strict_rerank_is_not_skipped_when_candidates_fit_output_pool(monkeypatch, tmp_path: Path):
    kb = knowledge.KnowledgeBase(str(tmp_path / "missing.json"))
    candidates = [
        knowledge.Candidate(
            chunk_idx=i,
            chunk=_chunk_regression(str(i), f"candidate {i} with enough specification context"),
            rrf_score=0.03, retrieval_score=0.3, gate_score=0.3,
        )
        for i in range(5)
    ]
    calls = 0
    monkeypatch.setattr(kb, "_check_reranker_available", lambda: True)

    def fake_rerank(**kwargs):
        nonlocal calls
        calls += 1
        return list(range(len(kwargs["documents"])))

    monkeypatch.setattr(knowledge.llama_client, "rerank", fake_rerank)
    kb._rerank_with_model("low confidence", candidates, top_k=12, is_strict_mode=True)

    assert calls == 1


def test_source_filter_limits_recall_before_top_k(monkeypatch, tmp_path: Path):
    chunks = [
        _chunk_regression("a", "same reset threshold 32 cycles", source="rev_a.md", embedding=[1.0, 0.0]),
        _chunk_regression("b", "same reset threshold 64 cycles", source="rev_b.md", embedding=[1.0, 0.0]),
    ]
    kb = _loaded_kb(tmp_path, chunks)
    monkeypatch.setattr(kb, "_get_embedding", lambda _text: [1.0, 0.0])
    monkeypatch.setattr(kb, "_generate_multi_queries", lambda question: [question])

    results = kb._hybrid_search("reset threshold", candidate_k=1, metadata_filter={"source": "rev_b.md"})

    assert [row.chunk["source"] for row in results] == ["rev_b.md"]


def test_bm25_does_not_count_overlap_prefix_twice(tmp_path: Path):
    text = ("alpha " * 20) + "UNIQUEPREFIX\n" + ("beta " * 20)
    chunks = RAG.split_by_semantic_with_sections(
        text, max_chars=90, overlap_chars=40, include_heading=False
    )
    assert len(chunks) >= 2
    occurrences = [i for i, chunk in enumerate(chunks) if "UNIQUEPREFIX" in chunk["content"]]
    assert len(occurrences) >= 2, "fixture must duplicate the prefix"
    original_index = next(
        i for i in occurrences
        if "UNIQUEPREFIX" in chunks[i]["content"][chunks[i].get("overlap_prefix_chars", 0):]
    )

    kb = _loaded_kb(tmp_path, [
        _chunk_regression(str(i), c["content"]) | {
            "overlap_prefix_chars": c.get("overlap_prefix_chars", 0),
            "heading_prefix_chars": c.get("heading_prefix_chars", 0),
        }
        for i, c in enumerate(chunks)
    ])
    postings = kb._bm25_index.get("uniqueprefix", {})

    assert list(postings) == [original_index]


def test_embedding_input_includes_source_and_cache_key(monkeypatch, tmp_path: Path):
    calls: list[str] = []

    def fake_embed(**kwargs):
        calls.append(kwargs["content"])
        return [1.0, 0.0]

    monkeypatch.setattr(RAG.llama_client, "embed_one", fake_embed)
    chunks = [
        {"source": "revision_a.md", "content": "identical register description"},
        {"source": "revision_b.md", "content": "identical register description"},
    ]

    RAG.generate_embeddings(chunks, cache_dir=tmp_path)

    assert len(calls) == 2
    assert "revision_a.md" in calls[0]
    assert "revision_b.md" in calls[1]


def test_strict_mcp_query_passes_strict_flag_to_retrieval():
    tree = ast.parse(Path("mcp_server.py").read_text(encoding="utf-8"))
    strict_fn = next(
        node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "query_knowledge_strict"
    )
    kb_calls = [
        node for node in ast.walk(strict_fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "KB"
        and node.func.attr == "query"
    ]
    assert kb_calls
    assert any(
        kw.arg == "is_strict_mode" and isinstance(kw.value, ast.Constant) and kw.value.value is True
        for kw in kb_calls[0].keywords
    )


# ── 原 test_contextual_signals.py:雙訊號(retrieval 含 ctx / gate 只看原文)的不變式 ──
REPO_ROOT = Path(__file__).resolve().parent.parent

CTX_TEXT = "本節出自 spec_a.md 的 1.2 Core control，說明核心控制暫存器的測試步驟。"


def _chunk(
    chunk_id: str,
    content: str,
    *,
    source: str = "spec_a.md",
    section: str = "1.2 Core control",
    ctx: str | None = None,
    embedding=None,
    gate=None,
) -> dict:
    chunk = {
        "id": chunk_id,
        "source": source,
        "page": 1,
        "chunk_index": 0,
        "type": "spec",
        "section": section,
        "heading_hierarchy": "",
        "overlap_prefix_chars": 0,
        "heading_prefix_chars": 0,
        "content": content,
    }
    if ctx is not None:
        chunk["ctx"] = ctx
        chunk["ctx_meta"] = {
            "generation_fingerprint": "f" * 8,
            "prompt_version": 1,
            "absent_reason": None,
        }
    if embedding is not None:
        chunk["embedding"] = embedding
    if gate is not None:
        chunk["embedding_gate"] = gate
    return chunk


def _cache_npz(tmp_path: Path) -> Path:
    """向量現在住在程式自管的隱藏 cache（kb_cache 決定路徑），不再與 JSON 同目錄。"""
    import kb_cache

    return kb_cache.cache_file(tmp_path / config.KNOWLEDGE_FILE)


def _write_kb(
    tmp_path: Path, chunks: list[dict], *, with_gate: bool, schema: str | None = None
) -> Path:
    """把 chunks 寫成 JSON + NPZ（可選擇要不要有 gate 矩陣）。"""
    json_path = tmp_path / config.KNOWLEDGE_FILE
    retrieval_schema = schema or (
        context_signals.CONTEXTUAL_INPUT_SCHEMA
        if context_signals.has_any_ctx(chunks)
        else context_signals.CONTENT_INPUT_SCHEMA
    )
    generation = "gen-test"
    metadata = {
        "documents": sorted({c["source"] for c in chunks}),
        "embedding_model": config.EMBEDDING_MODEL,
        "store_generation": generation,
        "embedding_content_hash_schema": retrieval_schema,
    }
    plain = [
        {k: v for k, v in c.items() if k not in ("embedding", "embedding_gate")}
        for c in chunks
    ]
    json_path.write_text(
        json.dumps({"metadata": metadata, "chunks": plain}, ensure_ascii=False),
        encoding="utf-8",
    )

    def _matrix(key):
        rows = np.array([c[key] for c in chunks], dtype=np.float32)
        norms = np.linalg.norm(rows, axis=1, keepdims=True)
        return rows / np.where(norms > 0, norms, 1.0)

    payload = {
        "embeddings": _matrix("embedding"),
        "embedding_model": config.EMBEDDING_MODEL,
        "embedding_dimension": len(chunks[0]["embedding"]),
        "chunk_count": len(chunks),
        "content_hash": context_signals.chunks_content_hash(plain, schema=retrieval_schema),
        "content_hash_schema": retrieval_schema,
        "store_generation": generation,
    }
    if with_gate:
        payload.update(
            embeddings_gate=_matrix("embedding_gate"),
            gate_embedding_dimension=len(chunks[0]["embedding_gate"]),
            gate_chunk_count=len(chunks),
            gate_content_hash=context_signals.chunks_content_hash(
                plain, schema=context_signals.GATE_SCHEMA
            ),
            gate_content_hash_schema=context_signals.GATE_SCHEMA,
        )
    np.savez_compressed(tmp_path / config.KNOWLEDGE_EMB_FILE, **payload)
    return json_path


# ============================================================
# 組字：ctx 是插槽，不是替代
# ============================================================
def test_gate_input_never_contains_ctx():
    chunk = _chunk("a", "原文內容", ctx=CTX_TEXT)

    gate = context_signals.gate_embedding_input(chunk)

    assert CTX_TEXT not in gate
    assert "[SOURCE] spec_a.md" in gate and "[SECTION_METADATA]" in gate


def test_retrieval_input_adds_ctx_without_dropping_deterministic_prefixes():
    chunk = _chunk("a", "原文內容", ctx=CTX_TEXT)

    retrieval = context_signals.retrieval_embedding_input(chunk, use_ctx=True)

    assert "[SOURCE] spec_a.md" in retrieval
    assert "[SECTION_METADATA] 1.2 Core control" in retrieval
    assert f"[CTX] {CTX_TEXT}" in retrieval
    assert retrieval.endswith("原文內容")


@pytest.mark.parametrize("use_ctx", [True, False])
def test_chunk_without_ctx_has_identical_retrieval_and_gate_input(use_ctx):
    chunk = _chunk("a", "原文內容")

    assert context_signals.retrieval_embedding_input(
        chunk, use_ctx=use_ctx
    ) == context_signals.gate_embedding_input(chunk)


def test_bm25_and_reranker_text_are_byte_identical_without_ctx():
    chunk = _chunk("a", "原文內容", ctx=CTX_TEXT)

    assert context_signals.bm25_document_text(chunk, use_ctx=False) == (
        f"{chunk['section']} {chunk['source']} {chunk['content']}"
    )
    assert context_signals.reranker_passage(chunk, use_ctx=False, max_chars=100) == (
        f"Source: {chunk['source']}\nSection: {chunk['section']}\n{chunk['content']}"
    )
    assert CTX_TEXT in context_signals.bm25_document_text(chunk, use_ctx=True)
    assert CTX_TEXT in context_signals.reranker_passage(chunk, use_ctx=True, max_chars=999)


def test_canonical_helper_is_defined_exactly_once():
    """組字規則只准有一份定義（讀寫兩端 import 同一個模組）。"""
    hits = []
    for path in REPO_ROOT.glob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.startswith("def gate_embedding_input(") or line.startswith(
                "def retrieval_embedding_input("
            ):
                hits.append(f"{path.name}:{lineno}")
    assert len(hits) == 2, f"組字函式的定義數量不對: {hits}"
    assert all(h.startswith("context_signals.py:") for h in hits), (
        f"組字函式散落在多個模組: {hits}"
    )


# ============================================================
# Storage：雙矩陣
# ============================================================
def test_save_writes_both_matrices_under_one_generation(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(RAG, "generate_embeddings", lambda *a, **k: None)
    monkeypatch.setattr(RAG, "generate_gate_embeddings", lambda *a, **k: None)
    kb = {
        "metadata": {"documents": ["spec_a.md"]},
        "chunks": [
            _chunk("a", "原文一", ctx=CTX_TEXT, embedding=[1.0, 0.0], gate=[0.0, 1.0]),
            _chunk("b", "原文二", ctx=CTX_TEXT, embedding=[0.0, 1.0], gate=[1.0, 0.0]),
        ],
    }

    RAG.save_knowledge_base(kb, tmp_path / config.KNOWLEDGE_FILE)

    with np.load(_cache_npz(tmp_path), allow_pickle=False) as data:
        assert "embeddings_gate" in data.files
        assert data["embeddings"].shape == data["embeddings_gate"].shape
        assert str(data["content_hash_schema"]) == context_signals.CONTEXTUAL_INPUT_SCHEMA
        assert str(data["gate_content_hash_schema"]) == context_signals.GATE_SCHEMA
        # 同一次提交、同一個 generation
        assert str(data["store_generation"]) == json.loads(
            (tmp_path / config.KNOWLEDGE_FILE).read_text(encoding="utf-8")
        )["metadata"]["store_generation"]


def test_json_never_carries_any_vector(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(RAG, "generate_embeddings", lambda *a, **k: None)
    monkeypatch.setattr(RAG, "generate_gate_embeddings", lambda *a, **k: None)
    kb = {
        "metadata": {"documents": ["spec_a.md"]},
        "chunks": [_chunk("a", "原文", ctx=CTX_TEXT, embedding=[1.0, 0.0], gate=[0.0, 1.0])],
    }

    RAG.save_knowledge_base(kb, tmp_path / config.KNOWLEDGE_FILE)

    payload = json.loads((tmp_path / config.KNOWLEDGE_FILE).read_text(encoding="utf-8"))
    for chunk in payload["chunks"]:
        assert "embedding" not in chunk
        assert "embedding_gate" not in chunk
    assert payload["chunks"][0]["ctx"] == CTX_TEXT


def test_kb_without_ctx_stays_single_matrix_and_legacy_schema(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(RAG, "generate_embeddings", lambda *a, **k: None)
    kb = {
        "metadata": {"documents": ["spec_a.md"]},
        "chunks": [_chunk("a", "原文", embedding=[1.0, 0.0])],
    }

    RAG.save_knowledge_base(kb, tmp_path / config.KNOWLEDGE_FILE)

    with np.load(_cache_npz(tmp_path), allow_pickle=False) as data:
        assert "embeddings_gate" not in data.files
        assert str(data["content_hash_schema"]) == context_signals.CONTENT_INPUT_SCHEMA


def test_legacy_kb_aliases_the_single_matrix_as_gate(tmp_path: Path):
    chunks = [_chunk("a", "原文一", embedding=[1.0, 0.0]), _chunk("b", "原文二", embedding=[0.0, 1.0])]
    path = _write_kb(tmp_path, chunks, with_gate=False)

    kb = KnowledgeBase(str(path))

    assert kb.loaded
    assert kb._has_ctx is False
    assert kb._gate_embeddings is kb._embeddings
    assert kb._bm25_gate is kb._bm25


def test_ctx_kb_without_gate_matrix_never_uses_it_and_rebuilds(tmp_path: Path, monkeypatch):
    """有 ctx 卻沒有 gate 矩陣的 cache，一個位元組都不准拿去決策。

    2026-08-24 起處置從「拒載，請你自己重建整個 KB」改成「丟棄那份 cache，依
    knowledge.json 重算出**正確的** gate 矩陣」。安全性質一步都沒讓（gate 向量
    仍然只能來自 content-only 的組字，絕不別名 retrieval 矩陣），變的只是修復
    由程式做還是由人做。重算不出來的情形由下一條守。
    """
    chunks = [
        _chunk("a", "原文一", ctx=CTX_TEXT, embedding=[1.0, 0.0]),
        _chunk("b", "原文二", ctx=CTX_TEXT, embedding=[0.0, 1.0]),
    ]
    path = _write_kb(tmp_path, chunks, with_gate=False)
    monkeypatch.setattr(RAG.llama_client, "embed_one", lambda **_kw: [1.0, 0.0])

    kb = KnowledgeBase(str(path))

    assert kb.loaded, kb.load_error
    assert kb._has_ctx is True
    assert kb._gate_embeddings is not None
    assert kb._gate_embeddings is not kb._embeddings, "gate 不得別名 retrieval 矩陣"
    assert not (tmp_path / config.KNOWLEDGE_EMB_FILE).exists(), "沒有 gate 的舊 cache 要被淘汰"


def test_ctx_kb_without_gate_matrix_is_fatal_when_it_cannot_be_rebuilt(
    tmp_path: Path, monkeypatch
):
    """重算不出來時中止，而且訊息要說得出是缺 gate。絕不沿用那份 cache。"""
    chunks = [
        _chunk("a", "原文一", ctx=CTX_TEXT, embedding=[1.0, 0.0]),
        _chunk("b", "原文二", ctx=CTX_TEXT, embedding=[0.0, 1.0]),
    ]
    path = _write_kb(tmp_path, chunks, with_gate=False)
    monkeypatch.setattr(RAG.llama_client, "embed_one",
                        lambda **_kw: (_ for _ in ()).throw(OSError("no server")))

    with pytest.raises(KnowledgeStoreError, match="gate"):
        KnowledgeBase(str(path))


def test_gate_vectors_are_never_attached_to_chunks(tmp_path: Path):
    chunks = [
        _chunk("a", "原文一", ctx=CTX_TEXT, embedding=[1.0, 0.0], gate=[0.0, 1.0]),
        _chunk("b", "原文二", ctx=CTX_TEXT, embedding=[0.0, 1.0], gate=[1.0, 0.0]),
    ]
    path = _write_kb(tmp_path, chunks, with_gate=True)

    kb = KnowledgeBase(str(path))

    assert kb._gate_embeddings is not None
    assert all("embedding_gate" not in chunk for chunk in kb.chunks)
    assert all(chunk.get("embedding") for chunk in kb.chunks)


def test_remove_document_keeps_both_matrices_in_sync(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(RAG, "generate_embeddings", lambda *a, **k: None)
    monkeypatch.setattr(RAG, "generate_gate_embeddings", lambda *a, **k: None)
    kb = {
        "metadata": {"documents": ["keep.md", "drop.md"]},
        "chunks": [
            _chunk("a", "保留的原文", source="keep.md", ctx=CTX_TEXT,
                   embedding=[1.0, 0.0], gate=[0.9, 0.1]),
            _chunk("b", "要刪的原文", source="drop.md", ctx=CTX_TEXT,
                   embedding=[0.0, 1.0], gate=[0.1, 0.9]),
        ],
    }
    path = tmp_path / config.KNOWLEDGE_FILE
    RAG.save_knowledge_base(kb, path)

    RAG.remove_document_from_knowledge_base(path, "drop.md")

    with np.load(_cache_npz(tmp_path), allow_pickle=False) as data:
        assert data["embeddings"].shape[0] == 1
        assert data["embeddings_gate"].shape[0] == 1
    assert KnowledgeBase(str(path)).loaded


# ============================================================
# Schema：required 對照
# ============================================================
def test_required_schema_sets():
    assert context_signals.required_retrieval_schemas(has_ctx=False) == (
        context_signals.LEGACY_RETRIEVAL_SCHEMAS
    )
    assert context_signals.required_retrieval_schemas(has_ctx=True) == frozenset(
        {context_signals.CONTEXTUAL_INPUT_SCHEMA}
    )


def test_ctx_kb_with_legacy_retrieval_schema_never_uses_it_and_rebuilds(
    tmp_path: Path, monkeypatch
):
    """cache 自報的組字 schema 不是這種 KB 該有的 → 那批向量是用另一套字算的。

    同樣從「拒載」改成「丟棄並重建」：schema 白名單的比對一個字都沒放寬（仍然是
    required 對照，不是拿 cache 自報的 schema 重算自己），只是驗不過之後改成重算。
    """
    chunks = [
        _chunk("a", "原文一", ctx=CTX_TEXT, embedding=[1.0, 0.0], gate=[0.0, 1.0]),
        _chunk("b", "原文二", ctx=CTX_TEXT, embedding=[0.0, 1.0], gate=[1.0, 0.0]),
    ]
    path = _write_kb(
        tmp_path, chunks, with_gate=True, schema=context_signals.CONTENT_INPUT_SCHEMA
    )
    monkeypatch.setattr(RAG.llama_client, "embed_one", lambda **_kw: [1.0, 0.0])

    kb = KnowledgeBase(str(path))

    assert kb.loaded, kb.load_error
    assert not (tmp_path / config.KNOWLEDGE_EMB_FILE).exists(), "schema 不符的舊 cache 要被淘汰"
    with np.load(_cache_npz(tmp_path), allow_pickle=False) as data:
        assert str(data["content_hash_schema"]) == context_signals.CONTEXTUAL_INPUT_SCHEMA


def test_ctx_kb_with_legacy_retrieval_schema_is_fatal_when_it_cannot_be_rebuilt(
    tmp_path: Path, monkeypatch
):
    chunks = [
        _chunk("a", "原文一", ctx=CTX_TEXT, embedding=[1.0, 0.0], gate=[0.0, 1.0]),
    ]
    path = _write_kb(
        tmp_path, chunks, with_gate=True, schema=context_signals.CONTENT_INPUT_SCHEMA
    )
    monkeypatch.setattr(RAG.llama_client, "embed_one",
                        lambda **_kw: (_ for _ in ()).throw(OSError("no server")))

    with pytest.raises(KnowledgeStoreError, match="schema"):
        KnowledgeBase(str(path))


def test_legacy_content_v1_schema_still_loads(tmp_path: Path):
    chunks = [_chunk("a", "原文一", embedding=[1.0, 0.0]), _chunk("b", "原文二", embedding=[0.0, 1.0])]
    path = _write_kb(
        tmp_path, chunks, with_gate=False, schema=context_signals.LEGACY_CONTENT_HASH_SCHEMA
    )

    assert KnowledgeBase(str(path)).loaded


# ============================================================
# 決策點：一律讀 gate
# ============================================================
def _go_offline(kb: KnowledgeBase, monkeypatch, q_emb=(1.0, 0.0)) -> None:
    """把這個 KB 的所有對外呼叫掐掉：embedding / 擴寫 / reranker 都不連線。"""
    monkeypatch.setattr(kb, "_get_embedding", lambda _text: list(q_emb))
    monkeypatch.setattr(kb, "_should_expand_query", lambda *_a, **_k: False)
    monkeypatch.setattr(kb, "_check_reranker_available", lambda: False)


def _kb_with_split_signals(tmp_path: Path) -> KnowledgeBase:
    """retrieval 向量指向 query，gate 向量刻意指向別的方向。

    也就是「生成脈絡讓這個 chunk 看起來很相關，但原文其實不相關」。
    """
    chunks = [
        _chunk("a", "原文一夠長可以通過噪音過濾" * 4, ctx=CTX_TEXT,
               embedding=[1.0, 0.0], gate=[0.0, 1.0]),
        _chunk("b", "原文二夠長可以通過噪音過濾" * 4, ctx=CTX_TEXT,
               embedding=[0.9, 0.1], gate=[0.1, 0.9]),
    ]
    path = _write_kb(tmp_path, chunks, with_gate=True)
    return KnowledgeBase(str(path))


def test_candidate_gate_score_comes_from_the_gate_matrix(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "KB_CONTEXT_USE", True)
    kb = _kb_with_split_signals(tmp_path)
    _go_offline(kb, monkeypatch)

    candidates = kb._hybrid_search("query", candidate_k=5)

    assert candidates
    for candidate in candidates:
        # query 對齊 retrieval 方向、與 gate 方向正交
        assert candidate.retrieval_score > candidate.gate_score
        assert candidate.gate_score < 0.4


def test_should_expand_query_reads_gate_score(tmp_path: Path):
    kb = KnowledgeBase(str(tmp_path / "missing.json"))
    high_retrieval_low_gate = [
        Candidate(chunk_idx=i, chunk={"id": str(i)}, retrieval_score=0.99, gate_score=0.1)
        for i in range(3)
    ]

    assert kb._should_expand_query(high_retrieval_low_gate, question="解釋一下") is True


@pytest.mark.parametrize(
    "branch,candidates_kwargs,expected",
    [
        # margin 分支：gate 差距太小 → 要 rerank
        ("margin_gap", [(0.99, 0.50), (0.10, 0.499)], True),
        # top1 gate 太低 → 要 rerank
        ("low_top1", [(0.99, 0.10), (0.10, 0.01)], True),
        # gate 高又拉得開 → 不必 rerank
        ("high_and_separated", [(0.10, 0.90), (0.10, 0.70), (0.1, 0.65),
                                (0.1, 0.60), (0.1, 0.50)], False),
    ],
)
def test_should_rerank_branches_all_read_gate(
    tmp_path: Path, monkeypatch, branch, candidates_kwargs, expected
):
    kb = KnowledgeBase(str(tmp_path / "missing.json"))
    monkeypatch.setattr(knowledge, "RERANKER_ALWAYS_ON", False)
    monkeypatch.setattr(knowledge, "MARGIN_ENABLED", True)
    candidates = [
        Candidate(chunk_idx=i, chunk={"id": str(i)}, retrieval_score=r, gate_score=g)
        for i, (r, g) in enumerate(candidates_kwargs)
    ]

    assert kb._should_rerank(candidates, top_k=3, is_strict_mode=False) is expected, branch


def test_lexical_numeric_evidence_uses_gate_bm25(tmp_path: Path, monkeypatch):
    """數值題的 BM25-only 放行必須看 content-only 的分數。"""
    kb = KnowledgeBase(str(tmp_path / "missing.json"))
    chunk = _chunk("a", "最大值寫在別的地方")
    seen: list[float] = []
    real = kb._has_lexical_numeric_evidence

    def spy(question, bm25_score, chunk_arg):
        seen.append(bm25_score)
        return real(question, bm25_score, chunk_arg)

    monkeypatch.setattr(kb, "_has_lexical_numeric_evidence", spy)
    kb.loaded = True
    kb.chunks = [chunk]
    kb._index_chunks()
    monkeypatch.setattr(
        kb,
        "_hybrid_search",
        lambda *_a, **_k: [
            Candidate(chunk_idx=0, chunk=chunk, retrieval_score=0.1, gate_score=0.1,
                      retrieval_bm25=0.99, gate_bm25=0.0)
        ],
    )
    monkeypatch.setattr(kb, "_get_embedding", lambda _t: [1.0, 0.0])
    monkeypatch.setattr(kb, "_should_expand_query", lambda *_a, **_k: False)
    monkeypatch.setattr(kb, "_check_reranker_available", lambda: False)

    kb.query("最大值是多少")

    assert seen == [0.0], f"lexical 判定拿到的是 retrieval BM25: {seen}"


def test_merge_aggregates_gate_scores_without_contextual_fallback(tmp_path: Path):
    kb = _kb_with_split_signals(tmp_path)
    merged = kb._merge_adjacent_chunks(kb.chunks)

    assert merged
    assert all(chunk.get("member_chunk_idx") for chunk in merged)
    # 成員索引取 max：拿合併後的平均 contextual 向量重算是被禁止的
    score = kb._chunk_gate_score(merged[0], [0.0, 1.0])
    members = merged[0]["member_chunk_idx"]
    expected = max(kb._gate_scores_for(members, [0.0, 1.0]).values())
    assert score == pytest.approx(expected)


def test_metadata_top_emb_score_is_the_gate_score(tmp_path: Path, monkeypatch):
    """metadata["top_emb_score"]（= 拒答閘讀的數字）必須是 content-only 分數。"""
    # gate 方向與 query 夾角較大（0.5），retrieval 方向完全對齊（1.0）
    chunks = [
        _chunk("a", "原文一夠長可以通過噪音過濾。" * 4, ctx=CTX_TEXT,
               embedding=[1.0, 0.0], gate=[0.5, 0.8660254]),
        _chunk("b", "原文二夠長可以通過噪音過濾。" * 4, ctx=CTX_TEXT,
               embedding=[1.0, 0.0], gate=[0.5, 0.8660254]),
    ]
    path = _write_kb(tmp_path, chunks, with_gate=True)
    monkeypatch.setattr(config, "KB_CONTEXT_USE", True)
    kb = KnowledgeBase(str(path))
    _go_offline(kb, monkeypatch)
    monkeypatch.setattr(
        kb, "_rerank_with_model",
        lambda _q, candidates, _k, **_kw: [(None, c.chunk) for c in candidates],
    )

    _model, _display, meta = kb.query("CTRL 規格是什麼")

    assert meta["has_ref"] is True
    # 兩個數字算在同一組最終 chunk 上，所以可以直接比：gate 必須低於含 ctx 的分數
    assert meta["top_emb_score"] < meta["top_retrieval_score"], (
        "top_emb_score 必須是 content-only 的分數，不能被生成脈絡撐高"
    )

    # 旗標關掉時兩者必須相等（USE=off ≡ content-only）
    monkeypatch.setattr(config, "KB_CONTEXT_USE", False)
    _model, _display, meta_off = kb.query("CTRL 規格是什麼")
    assert meta_off["top_emb_score"] == pytest.approx(meta_off["top_retrieval_score"])


def test_refuse_answer_reads_top_emb_score_not_retrieval_score():
    """拒答閘只看 gate 分數：retrieval 再高也救不了弱原文。"""
    weak_content_strong_ctx = {
        "has_ref": True,
        "top_emb_score": config.WEAK_REF_THRESHOLD - 0.05,   # gate：弱
        "top_retrieval_score": 0.99,                          # 含 ctx：很高
        "has_authoritative_chunk": True,
    }

    assert utils.should_refuse_answer("這個 spec 的預設值是什麼", weak_content_strong_ctx) is True

    strong_content = dict(weak_content_strong_ctx, top_emb_score=config.WEAK_REF_THRESHOLD + 0.05)
    assert utils.should_refuse_answer("這個 spec 的預設值是什麼", strong_content) is False


@pytest.mark.smoke
@pytest.mark.parametrize(
    "question",
    (
        "The ingested synthetic specification does not define orbit_lock_epoch.",
        "已匯入的合成規格沒有定義 orbit_lock_epoch。",
    ),
)
def test_refuse_answer_rejects_explicitly_missing_identifier(question: str):
    """Strong same-document retrieval must not prove an explicitly absent field."""
    metadata = {
        "has_ref": True,
        "top_emb_score": config.WEAK_REF_THRESHOLD + 0.2,
        "top_retrieval_score": 0.99,
        "has_authoritative_chunk": True,
        "retrieved_chunks": ["The synthetic specification defines unrelated router limits."],
    }

    assert utils.should_refuse_answer(question, metadata) is True

    metadata["retrieved_chunks"] = [
        "This specification intentionally says nothing about orbit_lock_epoch."
    ]
    assert utils.should_refuse_answer(question, metadata) is True

    metadata["retrieved_chunks"] = [
        "The synthetic specification explicitly defines orbit_lock_epoch."
    ]
    assert utils.should_refuse_answer(question, metadata) is False


# ============================================================
# 證據文本面：ctx 不得出現
# ============================================================
def test_ctx_never_reaches_ref_text_or_retrieved_chunks(tmp_path: Path, monkeypatch):
    chunk = _chunk("a", "暫存器 CTRL 的重置值是 0x20，這一段夠長可以通過噪音過濾。" * 2,
                   ctx=CTX_TEXT, embedding=[1.0, 0.0], gate=[1.0, 0.0])
    path = _write_kb(tmp_path, [chunk, _chunk(
        "b", "另一段夠長的原文內容，用來讓候選不只一個。" * 2,
        ctx=CTX_TEXT, embedding=[0.0, 1.0], gate=[0.0, 1.0])], with_gate=True)
    kb = KnowledgeBase(str(path))
    _go_offline(kb, monkeypatch)
    monkeypatch.setattr(
        kb, "_rerank_with_model",
        lambda _q, candidates, _k, **_kw: [(None, c.chunk) for c in candidates],
    )

    model_output, display_output, meta = kb.query("CTRL 重置值")

    assert CTX_TEXT not in model_output, "ctx 出現在 [REF] 證據文本裡"
    assert CTX_TEXT not in display_output, "ctx 出現在 UI 來源顯示裡"
    assert all(CTX_TEXT not in text for text in meta["retrieved_chunks"]), (
        "ctx 出現在 strict 逐句驗證的來源裡"
    )


# ============================================================
# 旗標
# ============================================================
def test_use_off_makes_dense_ranking_read_the_gate_matrix(tmp_path: Path, monkeypatch):
    """USE 關掉時排序也要退回 content-only，不只是決策。"""
    kb = _kb_with_split_signals(tmp_path)
    _go_offline(kb, monkeypatch)

    monkeypatch.setattr(config, "KB_CONTEXT_USE", False)
    off = kb._hybrid_search("query", candidate_k=5)
    monkeypatch.setattr(config, "KB_CONTEXT_USE", True)
    on = kb._hybrid_search("query", candidate_k=5)

    assert all(c.retrieval_score == pytest.approx(c.gate_score) for c in off), (
        "USE=off 時 retrieval 分數必須就是 gate 分數"
    )
    assert any(c.retrieval_score > c.gate_score for c in on)


def test_use_off_falls_back_to_content_only_signals(tmp_path: Path, monkeypatch):
    kb = _kb_with_split_signals(tmp_path)
    monkeypatch.setattr(config, "KB_CONTEXT_USE", False)

    passages_off = [
        context_signals.reranker_passage(c, use_ctx=knowledge.use_generated_context(),
                                         max_chars=999)
        for c in kb.chunks
    ]
    monkeypatch.setattr(config, "KB_CONTEXT_USE", True)
    passages_on = [
        context_signals.reranker_passage(c, use_ctx=knowledge.use_generated_context(),
                                         max_chars=999)
        for c in kb.chunks
    ]

    assert all(CTX_TEXT not in p for p in passages_off)
    assert all(CTX_TEXT in p for p in passages_on)


@pytest.mark.parametrize(
    "generate,use,expect_ctx_in_kb,expect_ctx_used",
    [
        (False, False, False, False),   # 全關：現行行為
        (False, True, False, False),    # 只開 USE：KB 裡沒有 ctx，等於沒開
        (True, False, True, False),     # 只開 GENERATE：入庫有 ctx，查詢不吃
        (True, True, True, True),       # 全開
    ],
)
def test_generate_use_quadrants(
    tmp_path: Path, monkeypatch, generate, use, expect_ctx_in_kb, expect_ctx_used
):
    chunks = [
        _chunk("a", "原文一" * 20, ctx=CTX_TEXT if generate else None,
               embedding=[1.0, 0.0], gate=[1.0, 0.0]),
        _chunk("b", "原文二" * 20, ctx=CTX_TEXT if generate else None,
               embedding=[0.0, 1.0], gate=[0.0, 1.0]),
    ]
    path = _write_kb(tmp_path, chunks, with_gate=generate)
    monkeypatch.setattr(config, "KB_CONTEXT_USE", use)
    kb = KnowledgeBase(str(path))

    assert kb._has_ctx is expect_ctx_in_kb
    assert (knowledge.use_generated_context() and kb._has_ctx) is expect_ctx_used


def test_status_reports_ctx_coverage_without_leaking_text(tmp_path: Path, monkeypatch):
    chunks = [
        _chunk("a", "原文一", ctx=CTX_TEXT, embedding=[1.0, 0.0], gate=[1.0, 0.0]),
        _chunk("b", "原文二", embedding=[0.0, 1.0], gate=[0.0, 1.0]),
    ]
    chunks[1]["ctx"] = ""
    chunks[1]["ctx_meta"] = {
        "generation_fingerprint": "x" * 8,
        "prompt_version": 1,
        "absent_reason": "empty_response",
    }
    path = _write_kb(tmp_path, chunks, with_gate=True)

    kb = KnowledgeBase(str(path))
    monkeypatch.setattr(kb, "_check_reranker_available", lambda: False)
    status = kb.get_status()

    assert "ctx coverage 50%" in status
    assert "absent: 1" in status and "empty_response" in status
    assert CTX_TEXT not in status


# ============================================================
# 執行路徑
# ============================================================
def test_mcp_ingest_reports_cli_rebuild_when_generation_is_on():
    source = (REPO_ROOT / "mcp_server.py").read_text(encoding="utf-8")

    assert "KB_CONTEXT_GENERATE" in source
    # 建議命令必須用**真實的** `rag_script` 路徑與單行引用組出來:一般外部專案的
    # cwd 底下沒有 `./RAG.py`,而路徑含空白或 shell 字元時,沒引用的命令會被拆錯。
    # (以前這裡驗的是字面 `"RAG.py rebuild"` —— 那個字串在硬編相對路徑時才會出現,
    #  也就是說它剛好把**錯的**寫法釘住了。)
    assert "'rebuild', '--kb'" in source or '"rebuild", "--kb"' in source, source[:0]
    assert "_shell_join([sys.executable, str(rag_script), 'rebuild'" in source
    assert "'--context'" in source
    assert "python3 RAG.py rebuild" not in source, "又退回硬編的相對路徑"


def test_rebuild_flags_are_mutually_exclusive():
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "RAG.py"), "rebuild", "--kb", "x.json",
         "doc.md", "--context", "--no-context"],
        capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, check=False,
    )

    assert proc.returncode != 0
    assert "not allowed with argument" in proc.stderr


@pytest.mark.parametrize(
    "cli_flag,config_value,expected",
    [(None, True, True), (None, False, False), (True, False, True), (False, True, False)],
)
def test_context_flag_precedence(monkeypatch, cli_flag, config_value, expected):
    monkeypatch.setattr(config, "KB_CONTEXT_GENERATE", config_value)

    assert RAG.resolve_context_flag(cli_flag) is expected


def test_ctx_cannot_move_the_threshold_through_candidate_order(tmp_path: Path, monkeypatch):
    """決策讀的是位置性的分數（top1 / top2），排序不能被生成脈絡推動。

    候選本身照 RRF 排，而 RRF 受 ctx 影響。即使每個位置讀的都是 gate 分數，
    「誰站在第一位」被 ctx 換掉時，門檻與 margin 還是會跟著動。開著 ctx 時
    決策改看 gate 分數自己的排序。
    """
    kb = _kb_with_split_signals(tmp_path)
    monkeypatch.setattr(config, "KB_CONTEXT_USE", True)
    low_gate_first = [
        Candidate(chunk_idx=0, chunk=kb.chunks[0], rrf_score=0.9,
                  retrieval_score=0.99, gate_score=0.10),
        Candidate(chunk_idx=1, chunk=kb.chunks[1], rrf_score=0.1,
                  retrieval_score=0.20, gate_score=0.90),
    ]

    ranked = kb._decision_order(low_gate_first)

    assert [c.gate_score for c in ranked] == [0.90, 0.10]

    # 旗標關掉時必須原樣（RRF 順序），不得偷偷改變既有行為
    monkeypatch.setattr(config, "KB_CONTEXT_USE", False)
    assert kb._decision_order(low_gate_first) is low_gate_first


def test_is_high_risk_is_computed_from_gate_scores(tmp_path: Path, monkeypatch):
    """UI 的風險警告是 margin 決策：兩個候選的 gate 分數貼很近就要示警，

    即使含生成脈絡的檢索分數把它們拉得很開。
    """
    monkeypatch.setattr(config, "KB_CONTEXT_USE", True)
    monkeypatch.setattr(knowledge, "MARGIN_ENABLED", True)
    chunks = [
        _chunk("a", "原文一夠長可以通過噪音過濾。" * 4, ctx=CTX_TEXT,
               embedding=[1.0, 0.0], gate=[0.70, 0.71414284]),
        _chunk("b", "原文二夠長可以通過噪音過濾。" * 4, ctx=CTX_TEXT,
               embedding=[0.0, 1.0], gate=[0.70, 0.71414284]),
    ]
    path = _write_kb(tmp_path, chunks, with_gate=True)
    kb = KnowledgeBase(str(path))
    _go_offline(kb, monkeypatch)
    monkeypatch.setattr(
        kb, "_rerank_with_model",
        lambda _q, candidates, _k, **_kw: [(None, c.chunk) for c in candidates],
    )

    _model, _display, meta = kb.query("CTRL 重置值是什麼")

    # 兩個 chunk 的 gate 向量完全相同 → gate margin = 0 → 必須示警，
    # 而它們的 retrieval 向量是正交的（差距最大）。
    assert meta["is_high_risk"] is True


# ============================================================
# GPT review 回歸（2026-08-18）
# ============================================================
def test_ctx_kb_refuses_inline_vector_fallback(tmp_path: Path):
    """有 ctx 卻沒有 NPZ 時，不准回退到 JSON inline 向量。

    那條路徑會把 retrieval 矩陣直接別名成 gate，於是 KB_CONTEXT_USE=0、拒答門檻、
    信心判斷全都吃到含生成脈絡的向量——正是雙訊號要擋的東西。
    """
    chunks = [
        _chunk("a", "原文一" * 30, ctx=CTX_TEXT, embedding=[1.0, 0.0]),
        _chunk("b", "原文二" * 30, ctx=CTX_TEXT, embedding=[0.0, 1.0]),
    ]
    json_path = tmp_path / config.KNOWLEDGE_FILE
    json_path.write_text(
        json.dumps(
            {"metadata": {"documents": ["spec_a.md"],
                          "embedding_model": config.EMBEDDING_MODEL},
             "chunks": chunks},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert not (tmp_path / config.KNOWLEDGE_EMB_FILE).exists()

    with pytest.raises(KnowledgeStoreError, match="inline"):
        KnowledgeBase(str(json_path))


def test_precompute_embeddings_refuses_to_alias_gate_for_a_ctx_kb(tmp_path: Path):
    """第二道防線：直接呼叫 legacy 路徑也不准替 ctx KB 別名 gate。"""
    kb = KnowledgeBase(str(tmp_path / "missing.json"))
    kb.chunks = [_chunk("a", "原文", ctx=CTX_TEXT, embedding=[1.0, 0.0])]

    with pytest.raises(KnowledgeStoreError, match="inline"):
        kb._precompute_embeddings()


def test_concurrent_ingest_is_not_lost(tmp_path: Path, monkeypatch):
    """入庫途中別人寫進來的文件不得被過期快照覆蓋（lost update）。

    脈絡生成與 embedding 可能跑幾十分鐘；以前是「先載入 → 慢慢算 → 拿那份過期
    快照覆寫」，中間任何一次入庫都會整份消失。現在昂貴步驟在鎖外做完，
    載入→併入→寫回收在同一把 exclusive store lock 裡。
    """
    monkeypatch.chdir(tmp_path)   # embedding 快取不要落到 repo
    monkeypatch.setattr(RAG.llama_client, "embed_one", lambda **_kw: [1.0, 0.0])
    kb_path = tmp_path / config.KNOWLEDGE_FILE

    def _doc(name: str, body: str) -> Path:
        path = tmp_path / name
        path.write_text(f"# {name} 章節\n" + body * 200, encoding="utf-8")
        return path

    first = _doc("spec_a.md", "第一份內容")
    second = _doc("toolchain_x.md", "第二份內容")
    third = _doc("other_spec.md", "第三份內容")

    RAG.add_document(str(first), str(kb_path))

    original = RAG.generate_embeddings
    interleaved = {"done": False}

    def interleave(chunks, cache_dir=None, **kwargs):
        # 第二份還在算 embedding 時，另一個「行程」把第三份灌進同一個 KB
        if not interleaved["done"]:
            interleaved["done"] = True
            RAG.add_document(str(third), str(kb_path))
        return original(chunks, cache_dir, **kwargs)

    monkeypatch.setattr(RAG, "generate_embeddings", interleave)
    RAG.add_document(str(second), str(kb_path))

    documents = json.loads(kb_path.read_text(encoding="utf-8"))["metadata"]["documents"]
    assert sorted(documents) == ["other_spec.md", "spec_a.md", "toolchain_x.md"], (
        f"併發寫入的文件被覆蓋掉了: {documents}"
    )


def _run_ab_tool(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "kb_ab_compare.py"), *args],
        capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL, check=False,
    )


def test_audit_flags_a_ctx_kb_without_a_gate_matrix(tmp_path: Path):
    """離線體檢就要抓到「有 ctx 但沒有 gate 矩陣」，而且要回非零。"""
    chunks = [
        _chunk("a", "原文一", ctx=CTX_TEXT, embedding=[1.0, 0.0]),
        _chunk("b", "原文二", ctx=CTX_TEXT, embedding=[0.0, 1.0]),
    ]
    path = _write_kb(tmp_path, chunks, with_gate=False)

    proc = _run_ab_tool(str(path))

    assert proc.returncode == 1, proc.stdout
    assert "[FATAL]" in proc.stdout and "gate" in proc.stdout
    assert "gate 矩陣      : 無" in proc.stdout


def test_audit_matches_the_real_loader_on_gate_dimension_mismatch(tmp_path: Path):
    """列數對、維度不對：以前工具毫無警告 exit 0，正式 loader 卻拒載。

    判準只能有一份 —— 體檢直接跑正式 loader，不再自己重打一套結構檢查。
    """
    chunks = [_chunk("a", "原文", ctx=CTX_TEXT, embedding=[1.0, 0.0], gate=[1.0, 0.0])]
    path = _write_kb(tmp_path, chunks, with_gate=True)
    # 事後把 gate 換成不同維度（列數仍相同）
    with np.load(tmp_path / config.KNOWLEDGE_EMB_FILE, allow_pickle=False) as data:
        payload = {key: data[key] for key in data.files}
    payload["embeddings_gate"] = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
    payload["gate_embedding_dimension"] = np.array(3)
    np.savez_compressed(tmp_path / config.KNOWLEDGE_EMB_FILE, **payload)

    proc = _run_ab_tool(str(path))

    assert proc.returncode == 1, proc.stdout
    assert "會被拒載" in proc.stdout


def test_audit_reports_a_missing_npz_as_fatal(tmp_path: Path):
    """壞掉／缺失的 NPZ 不能只印 [WARN] 然後 exit 0。"""
    chunks = [_chunk("a", "原文一", embedding=[1.0, 0.0])]
    path = _write_kb(tmp_path, chunks, with_gate=False)
    (tmp_path / config.KNOWLEDGE_EMB_FILE).unlink()

    proc = _run_ab_tool(str(path))

    assert proc.returncode == 1, proc.stdout
    assert "[FATAL]" in proc.stdout


def test_audit_accepts_a_healthy_kb(tmp_path: Path):
    chunks = [
        _chunk("a", "原文一", ctx=CTX_TEXT, embedding=[1.0, 0.0], gate=[0.0, 1.0]),
        _chunk("b", "原文二", ctx=CTX_TEXT, embedding=[0.0, 1.0], gate=[1.0, 0.0]),
    ]
    path = _write_kb(tmp_path, chunks, with_gate=True)

    proc = _run_ab_tool(str(path))

    assert proc.returncode == 0, proc.stdout
    assert "查詢端載入      : OK" in proc.stdout


def test_diff_does_not_claim_vectors_are_reusable_when_section_changed(tmp_path: Path):
    """embedding 輸入含 source / section / ctx，不是只有 content。

    只比 content 的話，「同 content、不同 section」會被錯報成「既有向量仍可用」。
    """
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    body = "同樣的原文內容" * 10
    _write_kb(left, [_chunk("a", body, section="1.2 Core control", embedding=[1.0, 0.0])],
              with_gate=False)
    _write_kb(right, [_chunk("a", body, section="9.9 別的章節", embedding=[1.0, 0.0])],
              with_gate=False)

    proc = _run_ab_tool(str(left / config.KNOWLEDGE_FILE), str(right / config.KNOWLEDGE_FILE))

    assert proc.returncode == 0, proc.stderr
    assert "content 位元組差異: 0" in proc.stdout
    assert "既有向量仍可用" not in proc.stdout, "section 變了卻說向量還能用"
    assert "embedding 要重算" in proc.stdout


def test_audit_is_fatal_when_the_loader_cannot_be_imported(monkeypatch, tmp_path: Path):
    """判準跑不起來時不能宣告健康——那等於給一份沒驗過卻看起來沒問題的報告。"""
    import importlib.util
    import sys as _sys

    spec = importlib.util.spec_from_file_location(
        "kb_ab_compare", REPO_ROOT / "scripts" / "kb_ab_compare.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    real_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name == "knowledge":
            raise ImportError("simulated missing dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    monkeypatch.delitem(_sys.modules, "knowledge", raising=False)

    accepted, reason = module.loader_verdict(tmp_path / "knowledge.json")

    assert accepted is False
    assert "沒有被驗證" in reason


@pytest.mark.smoke
def test_reranker_passage_carries_the_figure_caption():
    """caption 也要進 cross-encoder 的 document 側。

    reranker 預設永遠啟用：caption 只進 embedding 與 BM25 的話，圖被召回之後仍可能
    在 rerank 的 top-N 被丟掉——以表名提問還是查不到那張圖。
    """
    chunk = _chunk("a", "原文內容")
    chunk["figure_caption"] = "Table 3-1 Register map"

    passage = context_signals.reranker_passage(chunk, use_ctx=False, max_chars=999)

    assert "Table 3-1 Register map" in passage, passage
    # 沒有 caption 的 chunk 逐位元組不變（既有 KB 的 rerank 分數不得被動到）
    plain = _chunk("a", "原文內容")
    assert context_signals.reranker_passage(plain, use_ctx=False, max_chars=999) == (
        f"Source: {plain['source']}\nSection: {plain['section']}\n{plain['content']}")
