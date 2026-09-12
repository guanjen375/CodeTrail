"""Section recall and MinerU evidence boundaries that otherwise fail silently.

All vectors/text are synthetic.  These contracts exercise real recall/query
logic; server calls are forbidden except the explicitly inspected rerank mock.
"""
from __future__ import annotations

import copy

import numpy as np
import pytest

import config
import context_signals
import kb_cache
import knowledge
import section_index
from knowledge import Candidate, KnowledgeBase


pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("section retrieval contracts must remain offline")

    monkeypatch.setattr(knowledge.llama_client, "embed_one", forbidden)
    monkeypatch.setattr(knowledge.llama_client, "rerank", forbidden)
    monkeypatch.setattr(knowledge, "_gated_completion", forbidden)
    monkeypatch.setattr(knowledge, "RRF_ENABLED", True)
    monkeypatch.setattr(knowledge, "BM25_ENABLED", True)
    monkeypatch.setattr(knowledge, "USE_MMR", False)
    monkeypatch.setattr(knowledge, "USE_RERANKER", False)
    monkeypatch.setattr(knowledge, "KNOWLEDGE_MERGE_ADJACENT", False)
    monkeypatch.setattr(config, "KB_CONTEXT_USE", True)


def _chunk(index, *, source="synthetic.md", section="Core control", **extra):
    return {
        "id": f"{source}:{index}", "source": source, "page": index // 10 + 1,
        "type": "doc", "section": section, "section_index": 0,
        "heading_hierarchy": section, "chunk_index": index,
        "char_start": index * 200, "char_end": (index + 1) * 200,
        "content": (f"member_{index} documents a distinct controller operation. "
                    "Its original paragraph is the only evidence for this member."),
        "embedding": [0.8, 0.6],
        **extra,
    }


def _memory_kb(tmp_path, monkeypatch, chunks, *, section_vectors=None):
    kb = KnowledgeBase(str(tmp_path / "missing.json"))
    kb.chunks = copy.deepcopy(chunks)
    kb.documents = sorted({c["source"] for c in kb.chunks})
    kb._index_chunks()
    kb._has_ctx = context_signals.has_any_ctx(kb.chunks)
    kb._needs_gate = context_signals.needs_gate_matrix(kb.chunks)

    def matrix(key):
        rows = np.asarray([c.get(key, c["embedding"]) for c in kb.chunks], dtype=np.float32)
        norms = np.linalg.norm(rows, axis=1, keepdims=True)
        return rows / np.where(norms > 0, norms, 1.0)

    nodes = section_index.build_sections(kb.chunks)
    section_rows = np.asarray([
        (section_vectors or {}).get(node.source, [1.0, 0.0]) for node in nodes
    ], dtype=np.float32).reshape(len(nodes), 2)
    if len(nodes):
        section_rows /= np.linalg.norm(section_rows, axis=1, keepdims=True)
    kb._attach_matrices(kb_cache.Matrices(
        matrix("embedding"), matrix("embedding_gate") if kb._needs_gate else None,
        "cache", None, section_rows, nodes,
    ))
    kb._precompute_bm25_index()
    kb.loaded = True
    monkeypatch.setattr(kb, "_get_embedding", lambda _text: [1.0, 0.0])
    monkeypatch.setattr(kb, "_should_expand_query", lambda *_a, **_kw: False)
    monkeypatch.setattr(kb, "_check_reranker_available", lambda: False)
    return kb


def test_section_members_expand_completely_without_duplicates_or_filter_leaks(
    tmp_path, monkeypatch,
):
    chunks = [_chunk(i) for i in range(60)]
    chunks.append(_chunk(60, type="chat"))
    chunks.extend(_chunk(i, source="unrelated.md") for i in range(20))
    kb = _memory_kb(tmp_path, monkeypatch, chunks)

    candidates = kb._hybrid_search(
        "controller", candidate_k=2,
        metadata_filter={"source": "synthetic.md", "type": "doc"},
    )

    assert len(candidates) == 60
    assert {c.chunk_idx for c in candidates} == set(range(60))
    assert all(c.chunk is kb.chunks[c.chunk_idx] for c in candidates)
    assert all(c.section_rrf_score > 0 for c in candidates)
    assert kb._embeddings.shape[0] == len(chunks)
    assert len(kb._bm25_gate.doc_lens) == len(chunks)
    assert len(kb._section_bm25.doc_lens) == 2


@pytest.mark.parametrize("has_ctx", [False, True])
def test_section_rank_never_substitutes_for_a_members_own_dense_or_lexical_gate(
    tmp_path, monkeypatch, has_ctx,
):
    chunks = [_chunk(i, embedding=[0.2, 0.9797959]) for i in range(12)]
    if has_ctx:
        for chunk in chunks:
            chunk.update(ctx="generated retrieval hint", embedding=[1.0, 0.0],
                         embedding_gate=[0.2, 0.9797959])
    kb = _memory_kb(tmp_path, monkeypatch, chunks)

    candidates = kb._hybrid_search("unrelatedneedle", candidate_k=1)

    assert len(candidates) == len(chunks)
    for candidate in candidates:
        assert candidate.gate_score == pytest.approx(0.2)
        assert candidate.retrieval_score == pytest.approx(1.0 if has_ctx else 0.2)
        assert candidate.retrieval_bm25 == candidate.gate_bm25 == 0.0
    model, _display, metadata = kb.query("unrelatedneedle", is_strict_mode=True)
    assert metadata["has_ref"] is False
    assert model == ""


def test_section_contribution_is_added_once_across_direct_and_variant_recall(
    tmp_path, monkeypatch,
):
    kb = _memory_kb(tmp_path, monkeypatch, [_chunk(i) for i in range(12)])
    with monkeypatch.context() as patch:
        patch.setattr(kb, "_section_embeddings", None)
        direct = {c.chunk_idx: c.rrf_score for c in kb._search_once(
            "controller", [1.0, 0.0], 1, None,
        )}
    monkeypatch.setattr(kb, "_should_expand_query", lambda *_a, **_kw: True)
    monkeypatch.setattr(knowledge, "MULTI_QUERY_ENABLED", True)
    monkeypatch.setattr(kb, "_generate_multi_queries", lambda q: [q, q, q])

    candidates = kb._hybrid_search("controller", candidate_k=1)

    assert len(candidates) == 12
    for candidate in candidates:
        assert candidate.section_rrf_score == pytest.approx(1.0 / knowledge.RRF_K)
        assert candidate.rrf_score == pytest.approx(
            direct.get(candidate.chunk_idx, 0.0) * 2.8 + 1.0 / knowledge.RRF_K
        )


def test_section_full_text_lexical_recall_reaches_a_long_sections_tail(
    tmp_path, monkeypatch,
):
    chunks = [_chunk(0, source=f"other_{i}.md") for i in range(4)]
    long_section = [_chunk(i, source="long.md", embedding=[0.0, 1.0],
                           content=(f"paragraph_{i} " + "background prose " * 80))
                    for i in range(12)]
    long_section[-1]["content"] += " terminal_sentinel"
    chunks.extend(long_section)
    kb = _memory_kb(tmp_path, monkeypatch, chunks,
                    section_vectors={"long.md": [0.0, 1.0]})
    assert len(next(n.text for n in kb._section_nodes if n.source == "long.md")) > 6000

    candidates = kb._hybrid_search("terminal_sentinel", candidate_k=1)

    assert {c.chunk["id"] for c in candidates if c.chunk["source"] == "long.md"} == {
        c["id"] for c in long_section
    }
    first_member = next(c for c in candidates if c.chunk["id"] == "long.md:0")
    assert first_member.gate_score == first_member.gate_bm25 == 0.0


def test_query_sends_all_sixty_gate_qualified_members_to_batched_reranker(
    tmp_path, monkeypatch,
):
    chunks = [_chunk(i) for i in range(60)]
    chunks[-1]["content"] += " TAIL_SENTINEL"
    kb = _memory_kb(tmp_path, monkeypatch, chunks)
    monkeypatch.setattr(knowledge, "KNOWLEDGE_CANDIDATE_K", 2)
    monkeypatch.setattr(knowledge, "USE_RERANKER", True)
    monkeypatch.setattr(knowledge, "RERANKER_ALWAYS_ON", True)
    monkeypatch.setattr(kb, "_check_reranker_available", lambda: True)
    batches = []

    def rerank(**kwargs):
        documents = kwargs["documents"]
        batches.append(list(documents))
        return [10.0 if "TAIL_SENTINEL" in text else 0.1 for text in documents]

    monkeypatch.setattr(knowledge.llama_client, "rerank", rerank)
    model, _display, metadata = kb.query("controller", top_k=3)

    passages = [text for batch in batches for text in batch]
    assert len(passages) == len(set(passages)) == 60
    assert len(batches) > 1 and max(map(len, batches)) <= 15
    assert any("TAIL_SENTINEL" in text for text in passages)
    assert "TAIL_SENTINEL" in model
    assert metadata["top_emb_score"] == pytest.approx(0.8)
    assert 0.0 < metadata["top_score"] < 0.1
    assert all("section node" not in text for text in passages)


def test_section_rank_cannot_change_expansion_rerank_or_threshold_decisions(
    tmp_path, monkeypatch,
):
    kb = _memory_kb(tmp_path, monkeypatch, [_chunk(i) for i in range(3)])
    candidates = [
        Candidate(0, kb.chunks[0], rrf_score=100.0, gate_score=0.1,
                  section_rrf_score=1.0 / knowledge.RRF_K),
        Candidate(1, kb.chunks[1], rrf_score=0.1, gate_score=0.7, gate_bm25=0.3),
        Candidate(2, kb.chunks[2], rrf_score=0.09, gate_score=0.69),
    ]
    assert [c.chunk_idx for c in kb._decision_order(candidates)] == [1, 2, 0]
    assert KnowledgeBase._should_expand_query(kb, candidates, threshold=0.6) is False
    monkeypatch.setattr(knowledge, "RERANKER_ALWAYS_ON", False)
    monkeypatch.setattr(knowledge, "RERANKER_SKIP_THRESHOLD", 0.65)
    assert kb._should_rerank(candidates, 3) is False
    monkeypatch.setattr(kb, "_hybrid_search", lambda *_a, **_kw: copy.deepcopy(candidates))
    seen = []

    def retain(_query, picked, _top_k, **_kwargs):
        seen.extend(c.chunk_idx for c in picked)
        return [(None, c.chunk) for c in picked]

    monkeypatch.setattr(kb, "_rerank_with_model", retain)
    _model, _display, metadata = kb.query("controller")
    assert seen == [1, 2]
    assert metadata["is_high_risk"] is True
    assert metadata["top_kw_score"] == 0.3


def test_inline_vectors_explicitly_disable_sections_without_embedding_calls(
    tmp_path, monkeypatch,
):
    kb = _memory_kb(tmp_path, monkeypatch, [_chunk(i) for i in range(12)])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("inline vectors may not trigger section preparation")

    monkeypatch.setattr(kb_cache, "prepare_sections", forbidden)
    kb._precompute_embeddings()
    candidates = kb._hybrid_search("controller", candidate_k=2)

    assert kb._section_embeddings is None and kb._section_nodes == ()
    assert len(candidates) == 2
    assert all(c.section_rrf_score == 0.0 for c in candidates)
    assert "off (inline vectors)" in kb.get_status()


@pytest.mark.parametrize("provenance", [
    {"origin": "mineru_text"}, {"text_lane": "mineru"},
    {"origin": "mineru_text", "text_lane": "mineru"},
])
def test_mineru_text_is_excluded_with_page_reasons_and_normal_refs_disclose_lane(
    tmp_path, monkeypatch, provenance,
):
    chunks = [_chunk(i, page=i + 1, **provenance) for i in range(2)]
    kb = _memory_kb(tmp_path, monkeypatch, chunks)

    model, display, metadata = kb.query("controller", is_strict_mode=True)

    assert metadata["has_ref"] is False and metadata.get("refs", []) == []
    assert {entry["page"] for entry in metadata["excluded_text"]} == {1, 2}
    assert all(entry["reason"] == "mineru_text_not_independently_verified"
               for entry in metadata["excluded_text"])
    assert "MinerU" in model and "未經獨立驗證" in model
    assert "OCR" in display and "member_0" not in model
    normal, _display, normal_meta = kb.query("controller")
    assert normal_meta["has_ref"] is True and normal_meta["excluded_text"] == []
    assert "text_lane: mineru" in normal
    assert all(ref["text_lane"] == "mineru" for ref in normal_meta["refs"])
    assert all(ref["verification_status"] == "" for ref in normal_meta["refs"])


def test_strict_mineru_text_exclusion_preserves_a_figures_own_verified_evidence(
    tmp_path, monkeypatch,
):
    text = _chunk(0, origin="mineru_text", text_lane="mineru")
    figure = _chunk(
        1, origin="figure_table", structured=True, figure_kind="table",
        figure_id="fig_1111222233334444", revision=1,
        verification_status=knowledge.VERIF_NATIVE, model_input_variant="native",
        text_lane="mineru", heading_source="mineru", embedding_gate=[0.8, 0.6],
        content=("| Register | Explanation |\n| --- | --- |\n"
                 "| SAFE_CTRL | verified controller register evidence preserved from native table |"),
    )
    kb = _memory_kb(tmp_path, monkeypatch, [text, figure])

    model, _display, metadata = kb.query("controller", is_strict_mode=True)

    assert metadata["has_ref"] is True
    assert "SAFE_CTRL" in model and "member_0" not in model
    assert len(metadata["excluded_text"]) == 1
    assert [ref["figure_id"] for ref in metadata["refs"]] == [figure["figure_id"]]
    assert metadata["refs"][0]["verification_status"] == knowledge.VERIF_NATIVE


@pytest.mark.parametrize("stage", ["neighbor", "merge"])
def test_strict_neighbor_and_merge_paths_cannot_reintroduce_mineru_text(
    tmp_path, monkeypatch, stage,
):
    chunks = [_chunk(0), _chunk(1, origin="mineru_text", text_lane="mineru")]
    kb = _memory_kb(tmp_path, monkeypatch, chunks)
    if stage == "neighbor":
        monkeypatch.setattr(kb, "_resolve_replaced_figures",
                            lambda _chunks: [kb.chunks[1]])
    else:
        # Even a merge that loses its top-level origin must be checked against
        # the original member rows before the content can become a strict REF.
        merged = dict(kb.chunks[1], origin="", text_lane="", member_chunk_idx=[1])
        monkeypatch.setattr(kb, "_merge_adjacent_chunks", lambda _chunks: [merged])

    model, _display, metadata = kb.query("controller", is_strict_mode=True)

    assert metadata["has_ref"] is False
    assert metadata["excluded_text"]
    assert "member_1" not in model


def test_mineru_figure_headings_cannot_alias_gate_when_no_generated_ctx_exists(
    tmp_path, monkeypatch,
):
    figure = _chunk(
        0, section="ocr_heading_token", heading_source="mineru",
        origin="figure_diagram", structured=True, figure_kind="diagram",
        figure_id="fig_1111222233334444", revision=1,
        verification_status=knowledge.VERIF_NATIVE,
        embedding=[1.0, 0.0], embedding_gate=[0.0, 1.0],
        content="A native diagram describes a separate bus and its verified signal routing.",
    )
    kb = _memory_kb(tmp_path, monkeypatch, [figure])
    assert kb._has_ctx is False and kb._needs_gate is True
    assert kb._bm25_gate is not kb._bm25
    assert kb._bm25_score(["ocr_heading_token"], index=kb._bm25_gate) == []
    assert kb._bm25_score(["ocr_heading_token"], index=kb._bm25)
    assert kb._hybrid_search("ocr_heading_token")[0].gate_score == 0.0
    _model, _display, metadata = kb.query("ocr_heading_token", is_strict_mode=True)
    assert metadata["has_ref"] is False

    monkeypatch.setattr(config, "KB_CONTEXT_USE", False)
    candidate = kb._hybrid_search("ocr_heading_token")[0]
    assert candidate.retrieval_score == candidate.gate_score == 0.0
    assert candidate.retrieval_bm25 == candidate.gate_bm25 == 0.0
    with pytest.raises(knowledge.KnowledgeStoreError, match="inline"):
        kb._precompute_embeddings()


def test_mineru_heading_only_numeric_literal_cannot_pass_strict_lexical_gate(
    tmp_path, monkeypatch,
):
    """A real body keyword hit must not launder an OCR heading's numeric value."""
    figure = _chunk(
        0, section="Controller address 0xCAFE", heading_source="mineru",
        origin="figure_table", structured=True, figure_kind="table",
        figure_id="fig_1111222233334444", revision=1,
        verification_status=knowledge.VERIF_NATIVE, model_input_variant="native",
        embedding=[1.0, 0.0], embedding_gate=[0.1, 0.9949874],
        content=("| Topic | Description |\n| --- | --- |\n"
                 "| controller address | The native table names a controller address "
                 "but supplies no numeric address value in its verified payload. |"),
    )
    kb = _memory_kb(tmp_path, monkeypatch, [figure])
    question = "controller address 0xCAFE"
    gate_text = context_signals.bm25_document_text(figure, use_ctx=False)
    assert "0xcafe" not in gate_text.lower()
    gate_bm25 = kb._bm25_score(kb._tokenize_for_bm25(question), index=kb._bm25_gate)[0][0]
    assert gate_bm25 > 0.0, "the verified body must supply a genuine lexical hit"

    _model, _display, metadata = kb.query(question, is_strict_mode=True)

    assert metadata["has_ref"] is False, "an OCR heading-only number crossed the strict gate"
    assert kb._has_lexical_numeric_evidence(question, gate_bm25, figure) is False


def test_section_lexical_corpus_counts_its_persisted_title_once(tmp_path, monkeypatch):
    """Adding a section index must not silently double the heading's term weight."""
    kb = _memory_kb(tmp_path, monkeypatch, [_chunk(
        0, section="zqx_title", content="A body with independently distinct original words.",
    )])

    assert kb._section_nodes[0].text.count("zqx_title") == 1
    assert kb._section_bm25.index["zqx_title"][0] == 1
