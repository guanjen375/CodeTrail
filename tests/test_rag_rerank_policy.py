from __future__ import annotations

import pytest

import code_rag
import config
import knowledge


def _kb_candidates():
    return [
        (0.30, 0.30, 0.0, {"id": "a", "content": "alpha"}),
        (0.29, 0.29, 0.0, {"id": "b", "content": "beta"}),
        (0.28, 0.28, 0.0, {"id": "c", "content": "gamma"}),
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

    assert out == [candidates[0][3], candidates[1][3]]


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

    assert kb._rerank_with_model("question", candidates, top_k=2, is_strict_mode=True) is sentinel
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

    assert out == [candidates[0][3], candidates[1][3]]


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

    assert rag._rerank_code_candidates("question", candidates, top_k=2) == [
        candidates[0][3],
        candidates[1][3],
    ]
