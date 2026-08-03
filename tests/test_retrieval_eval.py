from __future__ import annotations

import json
import math
from pathlib import Path

import knowledge
from eval import run_retrieval_eval


def test_offline_fixture_expands_to_97_cases_with_numeric_and_version_subsets():
    chunks, cases = run_retrieval_eval.load_fixture()

    assert len(chunks) == 30
    assert len(cases) == 97
    assert sum("numeric" in case.tags for case in cases) == 62
    assert sum("version" in case.tags for case in cases) == 2
    assert sum(case.should_refuse for case in cases) == 5


def test_ranking_metrics_use_exact_ids_and_rank_positions():
    relevant = {"gold": 1.0}
    ranked = ["noise", "gold", "other"]

    assert run_retrieval_eval.recall_at_k(ranked, relevant, 1) == 0.0
    assert run_retrieval_eval.recall_at_k(ranked, relevant, 2) == 1.0
    assert run_retrieval_eval.reciprocal_rank(ranked, relevant) == 0.5
    assert run_retrieval_eval.ndcg_at_k(ranked, relevant, 5) == 1 / math.log2(3)


def test_offline_retrieval_never_calls_embedding_server(monkeypatch):
    monkeypatch.setattr(
        knowledge.llama_client,
        "embed_one",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("network embedding call")),
    )

    report = run_retrieval_eval.run_retrieval_evaluation()

    metrics = report["metrics"]
    assert metrics["case_count"] == 97
    assert metrics["llama_server_calls"] == 0
    assert metrics["recall_at_5"] >= 0.95
    assert metrics["mrr"] >= 0.95
    assert metrics["ndcg_at_5"] >= 0.95
    assert metrics["numeric_evidence_exact_at_5"] == 1.0
    assert report["answer_metrics"] is None


def test_prediction_scoring_requires_real_chunk_ids_and_numeric_entailment(tmp_path: Path):
    chunks, cases = run_retrieval_eval.load_fixture()
    predictions = []
    for case in cases:
        if case.should_refuse:
            predictions.append({"case_id": case.case_id, "refused": True})
            continue
        chunk_id = next(iter(case.relevant))
        answer = f"The supported claim is {case.expected_claims[0]}."
        if case.expected_values:
            answer = f"The exact value is {case.expected_values[0]}."
        predictions.append(
            {
                "case_id": case.case_id,
                "answer": answer,
                "cited_chunk_ids": [chunk_id],
                "refused": False,
            }
        )

    metrics = run_retrieval_eval.score_answer_predictions(cases, chunks, predictions)
    assert metrics["citation_entailment"] == 1.0
    assert metrics["numeric_answer_accuracy"] == 1.0
    assert metrics["refusal_accuracy"] == 1.0
    assert metrics["unanswerable_refusal_recall"] == 1.0
    assert metrics["answerable_refusal_false_positive_rate"] == 0.0

    # Merely spelling REF is not a citation and a hallucinated number is not entailed.
    first = next(case for case in cases if case.expected_values)
    bad = [dict(item) for item in predictions]
    bad_item = next(item for item in bad if item["case_id"] == first.case_id)
    bad_item["answer"] = "REF says the value is 0xDEADBEEF."
    bad_item["cited_chunk_ids"] = []
    bad_metrics = run_retrieval_eval.score_answer_predictions(cases, chunks, bad)
    assert bad_metrics["citation_entailment"] < 1.0
    assert bad_metrics["numeric_answer_accuracy"] < 1.0

    refused = [dict(item) for item in predictions]
    refused_item = next(item for item in refused if item["case_id"] == first.case_id)
    refused_item["refused"] = True
    refused_metrics = run_retrieval_eval.score_answer_predictions(cases, chunks, refused)
    assert refused_metrics["citation_entailment"] < 1.0
    assert refused_metrics["numeric_answer_accuracy"] < 1.0
    assert refused_metrics["answerable_refusal_false_positive_rate"] > 0.0


def test_cli_json_report_is_serializable(tmp_path: Path):
    report = run_retrieval_eval.run_retrieval_evaluation()
    output = tmp_path / "report.json"
    output.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    assert json.loads(output.read_text(encoding="utf-8"))["metrics"]["case_count"] == 97
