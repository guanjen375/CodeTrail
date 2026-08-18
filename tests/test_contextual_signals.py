"""雙訊號（retrieval 含 ctx / gate 只看原文）的不變式回歸測試。

核心不變式（規格 §2）：`ctx` 是 LLM 生成物，只准影響「哪些 chunk 被撈上來、
排第幾」。它不可以出現在任何證據文本，也不可以讓它抬高的**分數**通過決策門檻
——後者是分數面的循環 grounding：錯誤脈絡替弱原文背書。

這一批測的是「訊號的形狀與去向」：組字、schema、儲存、載入、六個決策點。
生成端（窗、快取、安全、CLI）在 tests/test_context_generation.py。

語料全部合成（`spec_a.md` / `toolchain_x`），離線，不碰任何 server。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import config
import context_signals
import knowledge
import RAG
import utils
from knowledge import Candidate, KnowledgeBase
from knowledge_store import KnowledgeStoreError

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

    with np.load(tmp_path / config.KNOWLEDGE_EMB_FILE, allow_pickle=False) as data:
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

    with np.load(tmp_path / config.KNOWLEDGE_EMB_FILE, allow_pickle=False) as data:
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


def test_ctx_kb_without_gate_matrix_is_refused(tmp_path: Path):
    chunks = [
        _chunk("a", "原文一", ctx=CTX_TEXT, embedding=[1.0, 0.0]),
        _chunk("b", "原文二", ctx=CTX_TEXT, embedding=[0.0, 1.0]),
    ]
    path = _write_kb(tmp_path, chunks, with_gate=False)

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

    with np.load(tmp_path / config.KNOWLEDGE_EMB_FILE, allow_pickle=False) as data:
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


def test_ctx_kb_with_legacy_retrieval_schema_is_refused(tmp_path: Path):
    chunks = [
        _chunk("a", "原文一", ctx=CTX_TEXT, embedding=[1.0, 0.0], gate=[0.0, 1.0]),
        _chunk("b", "原文二", ctx=CTX_TEXT, embedding=[0.0, 1.0], gate=[1.0, 0.0]),
    ]
    path = _write_kb(
        tmp_path, chunks, with_gate=True, schema=context_signals.CONTENT_INPUT_SCHEMA
    )

    with pytest.raises(KnowledgeStoreError, match="schema mismatch"):
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
        kb, "_rerank_with_model", lambda _q, candidates, _k, **_kw: [c.chunk for c in candidates]
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
        kb, "_rerank_with_model", lambda _q, candidates, _k, **_kw: [c.chunk for c in candidates]
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
    assert "RAG.py rebuild" in source


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
        kb, "_rerank_with_model", lambda _q, candidates, _k, **_kw: [c.chunk for c in candidates]
    )

    _model, _display, meta = kb.query("CTRL 重置值是什麼")

    # 兩個 chunk 的 gate 向量完全相同 → gate margin = 0 → 必須示警，
    # 而它們的 retrieval 向量是正交的（差距最大）。
    assert meta["is_high_risk"] is True
