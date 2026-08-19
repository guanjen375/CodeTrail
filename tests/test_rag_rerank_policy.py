from __future__ import annotations

import pytest

import code_rag
import config
import knowledge


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
