"""Real strict-query regressions: English numeric routing and explicit intent.

Also the two-stage self-check contract: the check request extends the draft
request verbatim, and both generations share one output limit with the gate.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

import config
import context_budget
import knowledge
import llama_client
import utils
from runtime_dependencies import DependencyError
from tests._harness import import_mcp_module, tool_fn


pytestmark = pytest.mark.smoke

ENGLISH_QUESTION = "Warm reset delay is how many ms?"
CHINESE_QUESTION = "Warm reset delay 是多少毫秒？"
VERIFIED_CONTEXT = "[REF1] startup.pdf p.1: Warm reset delay is 37 ms."
EXCLUDED_TEXT = [{
    "source": "startup.pdf", "page": 1, "origin": "mineru_text",
    "text_lane": "mineru", "text_id": "text_reset_delay", "text_revision": 2,
    "reason": "mineru_text_not_independently_verified",
}]
EXCLUDED_FIGURES = [{
    "source": "startup.pdf", "page": 1, "figure_id": "fig_reset_delay",
    "figure_kind": "table", "verification_status": "needs_review",
    "reasons": ["glyph_conflict"],
}]


def _strong_metadata():
    return {
        "has_ref": True, "top_score": 0.03, "top_emb_score": 0.7158,
        "top_retrieval_score": 0.99, "has_authoritative_chunk": True,
        "retrieved_chunks": ["Warm reset delay is 37 ms."],
        "refs": [{"source": "startup.pdf", "page": 1,
                  "verification_status": "human_verified"}],
        "excluded_text": [], "excluded_figures": [],
    }


def _mcp_with_query(monkeypatch, tmp_path, query):
    mcp = import_mcp_module(monkeypatch, tmp_path)
    monkeypatch.setattr(mcp, "_ensure_kb_fresh", lambda: None)
    monkeypatch.setattr(mcp, "_record_kb_interaction", lambda **_kwargs: None)
    monkeypatch.setattr(mcp, "KB", SimpleNamespace(loaded=True, query=query))
    return mcp


def test_english_numeric_query_skips_expansion_without_hiding_dependency_failure(
    tmp_path, monkeypatch,
):
    """The reported English query must never reach the 20-second expansion call."""
    kb = knowledge.KnowledgeBase(str(tmp_path / "missing.json"))
    kb.loaded = True
    kb.chunks = [{"id": "reset", "source": "startup.pdf", "content": "37 ms"}]
    candidates = [knowledge.Candidate(
        0, kb.chunks[0], rrf_score=0.02, gate_score=0.1,
    )]
    monkeypatch.setattr(kb, "_get_embedding", lambda _question: [1.0, 0.0])
    monkeypatch.setattr(kb, "_search_once", lambda *_args: candidates)
    monkeypatch.setattr(knowledge, "MULTI_QUERY_SKIP_NUMERIC", True)
    monkeypatch.setattr(knowledge, "MULTI_QUERY_ENABLED", True)
    monkeypatch.setattr(knowledge, "MULTI_QUERY_TYPES", ["key_terms"])
    monkeypatch.setattr(knowledge, "USE_QUERY_EXPANSION", True)
    calls = []

    def unavailable(**kwargs):
        calls.append(kwargs["source"])
        raise DependencyError("selected expansion server unavailable")

    monkeypatch.setattr(knowledge, "_gated_completion", unavailable)
    for question in (
        ENGLISH_QUESTION, CHINESE_QUESTION,
        "HOW MUCH memory does reset require?",
        "How long is the reset delay?",
        "What is the maximum reset delay?",
        "What is the minimum reset delay?",
    ):
        assert kb._hybrid_search(question) == candidates
        assert kb._last_expansion == {"triggered": False, "queries": []}
        assert utils.needs_grounding(question)[0] is True
    assert calls == []

    # The fast path is a query decision, never an exception fallback. Both
    # enabled expansion backends must still fail loudly for other questions.
    question = "Explain the warm reset sequence"
    assert utils.needs_grounding(question)[0] is False
    with pytest.raises(DependencyError, match="selected expansion server"):
        kb._hybrid_search(question)
    monkeypatch.setattr(knowledge, "MULTI_QUERY_ENABLED", False)
    with pytest.raises(DependencyError, match="selected expansion server"):
        kb._hybrid_search(question)
    assert calls == ["kb_multi_query", "kb_query_expansion"]


def test_explicit_strict_answers_verified_evidence_when_automatic_grounding_is_off(
    tmp_path, monkeypatch,
):
    """Explicit strict is independent of language and automatic-mode settings."""
    queries, answers = [], []
    metadata = _strong_metadata()

    def query(question, **kwargs):
        queries.append((question, kwargs))
        return VERIFIED_CONTEXT, "display", metadata

    mcp = _mcp_with_query(monkeypatch, tmp_path, query)
    monkeypatch.setattr(utils, "NEEDS_GROUNDING_ENABLED", False)
    monkeypatch.setattr(utils, "STRICT_MODE", False)

    def answer(question, base_ctx, knowledge_ctx, binary_ctx=""):
        answers.append((question, knowledge_ctx))
        return "37 ms [REF1]"

    monkeypatch.setattr(mcp, "answer_with_self_check", answer)
    for question in (ENGLISH_QUESTION, CHINESE_QUESTION):
        assert utils.needs_grounding(question)[0] is False
        result = tool_fn(mcp, "query_knowledge_strict")(
            question, source="startup.pdf",
        )
        assert result["strict"] is True, result
        assert result["refused"] is False, result
        assert result["answer"] == "37 ms [REF1]"
        assert result["refs"] == metadata["refs"]
    assert queries == [
        (q, {"is_strict_mode": True, "source": "startup.pdf"})
        for q in (ENGLISH_QUESTION, CHINESE_QUESTION)
    ]
    assert answers == [(q, VERIFIED_CONTEXT) for q in (ENGLISH_QUESTION, CHINESE_QUESTION)]


@pytest.mark.parametrize("evidence", [
    "unverified", "weak", "nonauthoritative", "missing_identifier", "empty", "no_context",
])
def test_explicit_strict_refuses_unverified_weak_or_missing_evidence(
    tmp_path, monkeypatch, evidence,
):
    """Explicit intent must strengthen refusal, never admit untrusted content."""
    metadata = _strong_metadata()
    metadata.update(excluded_text=EXCLUDED_TEXT, excluded_figures=EXCLUDED_FIGURES)
    context = VERIFIED_CONTEXT
    question = ENGLISH_QUESTION
    if evidence == "unverified":
        metadata.update(has_ref=False, refs=[], top_emb_score=0.0)
        context = "Only unverified OCR is available; use review_text."
    elif evidence == "weak":
        metadata["top_emb_score"] = config.WEAK_REF_THRESHOLD - 0.05
    elif evidence == "nonauthoritative":
        metadata.update(has_authoritative_chunk=False,
                        top_emb_score=config.WEAK_REF_THRESHOLD + 0.05)
    elif evidence == "missing_identifier":
        question = "The guide does not define reset_delay_ticks."
    elif evidence == "empty":
        metadata = {}
    elif evidence == "no_context":
        context = ""
    mcp = _mcp_with_query(
        monkeypatch, tmp_path, lambda *_args, **_kwargs: (context, "", metadata),
    )
    answers = []
    monkeypatch.setattr(mcp, "answer_with_self_check",
                        lambda *_args, **_kwargs: answers.append("called"))

    result = tool_fn(mcp, "query_knowledge_strict")(question, source="startup.pdf")

    assert result["refused"] is True, result
    assert result["strict"] is True, result
    assert result["answer"] is None, result
    assert result["excluded_text"] == metadata.get("excluded_text", [])
    assert result["excluded_figures"] == metadata.get("excluded_figures", [])
    assert answers == []


def test_english_numeric_lexical_recall_still_requires_verification_and_grounding(
    tmp_path, monkeypatch,
):
    """English BM25 admission reaches rerank but cannot bypass either evidence gate."""
    kb = knowledge.KnowledgeBase(str(tmp_path / "missing.json"))
    kb.loaded = True
    kb.chunks = [
        {"id": "native", "content": "Reset timing appears in the startup section. " * 3},
        {"id": "figure", "content": "Warm reset delay is 73 ms.",
         "origin": "figure_table", "structured": True, "figure_kind": "table",
         "figure_id": "fig_reset_delay", "verification_status": "needs_review",
         "reasons": ["glyph_conflict"]},
    ]
    for chunk in kb.chunks:
        chunk.update(source="startup.pdf", page=1, type="spec",
                     embedding=[0.1, 0.9949874])
    kb._precompute_embeddings()
    candidates = [knowledge.Candidate(
        i, chunk, rrf_score=0.03, retrieval_score=0.99, gate_score=0.1,
        retrieval_bm25=1.0, gate_bm25=1.0,
    ) for i, chunk in enumerate(kb.chunks)]
    monkeypatch.setattr(kb, "_hybrid_search",
                        lambda *_args, **_kwargs: copy.deepcopy(candidates))
    monkeypatch.setattr(kb, "_get_embedding", lambda _question: [1.0, 0.0])
    monkeypatch.setattr(knowledge, "USE_MMR", False)
    monkeypatch.setattr(knowledge, "KNOWLEDGE_MERGE_ADJACENT", False)
    reranked, answers = [], []

    def rerank(question, selected, top_k, **kwargs):
        reranked.append(([c.chunk["id"] for c in selected], kwargs))
        return [(0.99, c.chunk) for c in selected]

    monkeypatch.setattr(kb, "_rerank_with_model", rerank)
    mcp = _mcp_with_query(monkeypatch, tmp_path, kb.query)
    monkeypatch.setattr(mcp, "answer_with_self_check",
                        lambda *_args, **_kwargs: answers.append("called"))

    result = tool_fn(mcp, "query_knowledge_strict")(
        ENGLISH_QUESTION, source="startup.pdf",
    )

    assert reranked == [(["native"], {"is_strict_mode": True})]
    assert result["top_emb_score"] == pytest.approx(0.1)
    assert result["refused"] is True, result
    assert result["strict"] is True, result
    assert [item["figure_id"] for item in result["excluded_figures"]] == ["fig_reset_delay"]
    assert answers == []


# A value no default uses: answer_with_self_check must read it at call time.
STRICT_LIMIT = 1536


def _stub_main_model(monkeypatch, replies):
    """Real context gate and real stream loop; only the /completion stream is fake.

    `replies` holds one (text, stop_type) per request. Returns the recorded
    native_completion kwargs and (gate kwargs, returned usage) per gate call.
    """
    calls, gates = [], []
    real_gate = context_budget.check_and_log

    def gate(**kwargs):
        usage = real_gate(**kwargs)
        gates.append((kwargs, usage))
        return usage

    def native_completion(**kwargs):
        calls.append(kwargs)
        text, stop_type = replies[len(calls) - 1]
        return iter([
            {"content": text, "stop": False},
            {"content": "", "stop": True, "stop_type": stop_type},
        ])

    monkeypatch.setattr(config, "require_main_model", lambda: "test-model")
    monkeypatch.setattr(config, "N_CTX", 65536)
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", STRICT_LIMIT)
    monkeypatch.setattr(config, "CTX_METRICS_ENABLED", False)
    monkeypatch.setattr(context_budget, "check_and_log", gate)
    monkeypatch.setattr(llama_client, "native_completion", native_completion)
    return calls, gates


@pytest.mark.smoke
def test_strict_self_check_extends_the_draft_request_under_one_output_limit(monkeypatch):
    """The check request must start with the draft request plus the raw draft.

    Rewording or reordering any earlier part makes the single-slot server
    recompute all evidence from the fork point: answers stay correct, only
    silently slower. A cap that differs from the gate reserve lets an admitted
    prompt overflow the context mid-generation.
    """
    draft = "Warm reset delay is 37 ms [REF1].\n"
    final = "Warm reset delay is 37 ms [REF1]. The watchdog timeout is 250 ms."
    calls, gates = _stub_main_model(monkeypatch, [(draft, "word"), (final, "eos")])
    checked = []

    def claim_check(answer, knowledge_ctx):
        checked.append((answer, knowledge_ctx))
        return f"  checked: {answer}  "

    monkeypatch.setattr(utils, "CLAIM_TO_EVIDENCE_ENABLED", True)
    monkeypatch.setattr(utils, "validate_claim_to_evidence", claim_check)

    answer = utils.answer_with_self_check(
        ENGLISH_QUESTION, "專案路徑: /project", VERIFIED_CONTEXT,
    )

    draft_request, check_request = (call["prompt"] for call in calls)
    assert draft_request.startswith(f"專案路徑: /project\n{VERIFIED_CONTEXT}\n")
    assert draft_request.endswith("\n\n[draft]\n")
    assert check_request.startswith(draft_request + draft + "\n[/draft]\n\n")
    check_rules = check_request[len(draft_request + draft):]
    assert "1. 逐句檢查：每句話是否能在 [REF] 內容裡找到明確根據" in check_rules
    assert "2. 凡是答案中沒有標註來源的句子，一律視為不可靠" in check_rules
    assert check_rules.endswith("- 不要解釋檢查過程，只輸出修正後的最終回答")
    assert calls[0]["stop"] == ["[/draft]"]
    assert "stop" not in calls[1]
    assert [call["n_predict"] for call in calls] == [STRICT_LIMIT, STRICT_LIMIT]
    assert [kwargs["prompt"] for kwargs, _ in gates] == [draft_request, check_request]
    assert [kwargs["reserved_output_tokens"] for kwargs, _ in gates] == [STRICT_LIMIT] * 2
    assert [usage.reserved_output_tokens for _, usage in gates] == [STRICT_LIMIT] * 2
    # Claim-to-evidence runs on the final answer only, and its output is returned.
    assert checked == [(final, VERIFIED_CONTEXT)]
    assert answer == f"checked: {final}"


@pytest.mark.smoke
@pytest.mark.parametrize("stage", ["draft", "final"])
def test_strict_output_limit_is_an_error_never_a_partial_or_unverified_answer(
    monkeypatch, stage,
):
    """A capped generation is unfinished: no self-check of half a draft, and a
    capped final never falls back to the unverified draft."""
    draft = "Warm reset delay is 37 ms [REF1]."
    replies = ([(draft, "limit")] if stage == "draft"
               else [(draft, "word"), ("Warm reset delay is", "limit")])
    calls, _gates = _stub_main_model(monkeypatch, replies)
    checked = []
    monkeypatch.setattr(utils, "CLAIM_TO_EVIDENCE_ENABLED", True)
    monkeypatch.setattr(utils, "validate_claim_to_evidence",
                        lambda answer, _ctx: checked.append(answer) or answer)

    answer = utils.answer_with_self_check(ENGLISH_QUESTION, "", VERIFIED_CONTEXT)

    label = "初稿" if stage == "draft" else "定稿"
    assert answer == (
        f"[ERROR] strict {label}達到輸出上限 {STRICT_LIMIT} tokens，未完成；請縮小問題。"
    )
    assert len(calls) == len(replies)
    assert checked == []


@pytest.mark.smoke
def test_default_stream_call_stays_uncapped_with_the_internal_reserve(monkeypatch):
    """Callers that pass no limit (agent.py) send exactly what they sent before.

    They never read stop_type, so a default cap would silently cut long answers.
    """
    calls, gates = _stub_main_model(monkeypatch, [("ok", "eos")])

    assert utils.call_llm_stream("plain prompt") == "ok"

    assert "n_predict" not in calls[0] and "stop" not in calls[0]
    gate_kwargs, usage = gates[0]
    assert "reserved_output_tokens" not in gate_kwargs
    assert usage.reserved_output_tokens == STRICT_LIMIT
    assert usage.reserve_is_explicit is False
