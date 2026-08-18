from __future__ import annotations

import ast
from pathlib import Path

import pytest

import knowledge
import RAG


def _chunk(chunk_id: str, content: str, *, source: str = "spec.md", embedding=None) -> dict:
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
        _chunk("target", "Register aperture starts at exact offset 0x4000 and spans 4096 bytes."),
        _chunk("noise", "General accelerator overview with no address literal."),
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
    target = _chunk(
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
    first = _chunk("first", "alpha control path", embedding=[1.0, 0.0])
    rescued = _chunk("rescued", "translated semantic reset path", embedding=[0.0, 1.0])
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
            chunk_idx=i, chunk=_chunk(str(i), content),
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
            chunk=_chunk(str(i), f"candidate {i} with enough specification context"),
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
        _chunk("a", "same reset threshold 32 cycles", source="rev_a.md", embedding=[1.0, 0.0]),
        _chunk("b", "same reset threshold 64 cycles", source="rev_b.md", embedding=[1.0, 0.0]),
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
        _chunk(str(i), c["content"]) | {
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
