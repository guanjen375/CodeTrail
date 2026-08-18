#!/usr/bin/env python3
"""Offline retrieval-only regression harness.

This runner never calls a llama-server.  It constructs a KnowledgeBase from a
checked-in synthetic firmware/spec fixture, resolves every query embedding from
a versioned cache, calls ``KnowledgeBase._hybrid_search`` directly, and reports
ranking metrics.  Missing vectors fail loudly.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from knowledge import KnowledgeBase  # noqa: E402


DEFAULT_FIXTURE = Path(__file__).with_name("retrieval_fixture.json")
DEFAULT_EMBEDDING_CACHE = Path(__file__).with_name("retrieval_embedding_cache.json")


@dataclass(frozen=True)
class RetrievalCase:
    case_id: str
    question: str
    relevant: dict[str, float]
    tags: tuple[str, ...]
    vector_key: str
    expected_values: tuple[str, ...] = ()
    expected_claims: tuple[str, ...] = ()
    source_filter: str | None = None
    should_refuse: bool = False


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"failed to read retrieval fixture {path}: {exc}") from exc


def load_fixture(path: Path = DEFAULT_FIXTURE) -> tuple[list[dict], list[RetrievalCase]]:
    data = _read_json(path)
    facts = data.get("facts", [])
    if not facts:
        raise RuntimeError(f"retrieval fixture has no facts: {path}")

    chunks = []
    cases = []
    seen_ids = set()
    for fact in facts:
        chunk_id = str(fact["id"])
        if chunk_id in seen_ids:
            raise RuntimeError(f"duplicate retrieval fixture id: {chunk_id}")
        seen_ids.add(chunk_id)
        register = str(fact["register"])
        source = str(fact["source"])
        offset = str(fact["offset"])
        reset = str(fact["reset"])
        version = str(fact.get("version", ""))
        purpose = str(fact["purpose"])
        chunks.append(
            {
                "id": chunk_id,
                "source": source,
                "page": int(fact.get("page", 1)),
                "chunk_index": int(fact.get("chunk_index", 0)),
                "section": str(fact.get("section", "Register Map")),
                "type": "spec",
                "content": (
                    "| Register | Offset | Reset | Description |\n"
                    "| --- | --- | --- | --- |\n"
                    f"| {register} | {offset} | {reset} | {purpose} |\n"
                    f"The {register} register in {source} is authoritative for {purpose}."
                    + (f" Its encoded interface version is {version}." if version else "")
                ),
            }
        )
        relevant = {chunk_id: 1.0}
        source_filter = source if bool(fact.get("filter_by_source", False)) else None
        cases.extend(
            [
                RetrievalCase(
                    case_id=f"{chunk_id}__offset",
                    question=f"What is the exact hex offset of register {register}?",
                    relevant=relevant,
                    tags=("numeric", "hex", "register-map"),
                    vector_key="__numeric__",
                    expected_values=(offset,),
                    expected_claims=(offset,),
                    source_filter=source_filter,
                ),
                RetrievalCase(
                    case_id=f"{chunk_id}__reset",
                    question=f"What reset value is specified for register {register}?",
                    relevant=relevant,
                    tags=("numeric", "register-map"),
                    vector_key="__numeric__",
                    expected_values=(reset,),
                    expected_claims=(reset,),
                    source_filter=source_filter,
                ),
                RetrievalCase(
                    case_id=f"{chunk_id}__purpose",
                    question=f"Which register is responsible for {purpose}?",
                    relevant=relevant,
                    tags=("semantic",),
                    vector_key=chunk_id,
                    expected_claims=(register,),
                    source_filter=source_filter,
                ),
            ]
        )
        if version:
            cases.append(
                RetrievalCase(
                    case_id=f"{chunk_id}__version",
                    question=f"What exact interface version does {register} expose?",
                    relevant=relevant,
                    tags=("numeric", "version"),
                    vector_key="__numeric__",
                    expected_values=(version,),
                    expected_claims=(version,),
                    source_filter=source_filter,
                )
            )

    for item in data.get("unanswerable", []):
        cases.append(
            RetrievalCase(
                case_id=str(item["id"]),
                question=str(item["question"]),
                relevant={},
                tags=("unanswerable",),
                vector_key=str(item.get("vector_key", "__numeric__")),
                should_refuse=True,
            )
        )
    return chunks, cases


def load_embedding_cache(path: Path, chunk_ids: list[str]) -> dict[str, list[float]]:
    data = _read_json(path)
    if data.get("schema_version") != 1:
        raise RuntimeError(f"unsupported retrieval embedding cache schema in {path}")
    basis_order = data.get("basis_order", [])
    if basis_order != chunk_ids:
        raise RuntimeError(
            "retrieval embedding cache basis_order does not match fixture facts; "
            "regenerate/update both files together"
        )
    dimension = int(data.get("dimension", 0))
    if dimension != len(basis_order) or dimension <= 0:
        raise RuntimeError(
            f"invalid retrieval embedding cache dimension={dimension}, basis={len(basis_order)}"
        )

    cache = {}
    for index, chunk_id in enumerate(basis_order):
        row = [0.0] * dimension
        row[index] = 1.0
        cache[chunk_id] = row
    numeric = data.get("numeric_query_vector")
    if not isinstance(numeric, list) or len(numeric) != dimension:
        raise RuntimeError("numeric_query_vector is missing or has the wrong dimension")
    cache["__numeric__"] = [float(value) for value in numeric]
    return cache


def recall_at_k(ranked_ids: list[str], relevant: dict[str, float], k: int = 5) -> float:
    if not relevant:
        return math.nan
    found = sum(1 for item_id in relevant if item_id in ranked_ids[:k])
    return found / len(relevant)


def reciprocal_rank(ranked_ids: list[str], relevant: dict[str, float]) -> float:
    for rank, item_id in enumerate(ranked_ids, 1):
        if item_id in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranked_ids: list[str], relevant: dict[str, float], k: int = 5) -> float:
    if not relevant:
        return math.nan
    dcg = sum(
        (2 ** relevant.get(item_id, 0.0) - 1) / math.log2(rank + 1)
        for rank, item_id in enumerate(ranked_ids[:k], 1)
    )
    ideal = sorted(relevant.values(), reverse=True)[:k]
    idcg = sum((2 ** grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(ideal, 1))
    return dcg / idcg if idcg else 0.0


_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:0x[0-9A-Fa-f]+|v?\d+(?:\.\d+)+|\d+)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def _normalized_values(text: str) -> set[str]:
    return {match.group(0).lower() for match in _VALUE_RE.finditer(text)}


def score_answer_predictions(
    cases: list[RetrievalCase], chunks: list[dict], predictions: list[dict]
) -> dict:
    """Score explicit citations/numbers/refusals; mere text containing 'REF' never passes."""
    by_case = {case.case_id: case for case in cases}
    chunk_text = {str(chunk["id"]): str(chunk["content"]) for chunk in chunks}
    by_prediction = {str(item.get("case_id")): item for item in predictions}
    if len(by_prediction) != len(predictions):
        raise RuntimeError("predictions contain duplicate or missing case_id values")
    missing = sorted(set(by_case) - set(by_prediction))
    extra = sorted(set(by_prediction) - set(by_case))
    if missing or extra:
        raise RuntimeError(
            f"prediction case ids differ from fixture (missing={missing[:5]}, extra={extra[:5]})"
        )

    citation_scores = []
    numeric_scores = []
    refusal_correct = []
    refused_count = 0
    answerable_refusals = 0
    unanswerable_refusals = 0
    unanswerable_count = 0
    for case in cases:
        prediction = by_prediction[case.case_id]
        refused = bool(prediction.get("refused", False))
        refused_count += int(refused)
        refusal_correct.append(float(refused == case.should_refuse))
        if case.should_refuse:
            unanswerable_count += 1
            unanswerable_refusals += int(refused)
        else:
            answerable_refusals += int(refused)

        if refused:
            if not case.should_refuse:
                citation_scores.append(0.0)
                if case.expected_values:
                    numeric_scores.append(0.0)
            continue
        answer = str(prediction.get("answer", ""))
        cited_ids = [str(value) for value in prediction.get("cited_chunk_ids", [])]
        cited = "\n".join(chunk_text.get(chunk_id, "") for chunk_id in cited_ids)
        answer_values = _normalized_values(answer)
        cited_values = _normalized_values(cited)
        citations_relevant = bool(cited_ids) and all(
            chunk_id in case.relevant for chunk_id in cited_ids
        )
        values_entailed = not answer_values or answer_values <= cited_values
        normalized_answer = answer.casefold()
        normalized_cited = cited.casefold()
        claims_answered = all(
            claim.casefold() in normalized_answer for claim in case.expected_claims
        )
        claims_entailed = all(
            claim.casefold() in normalized_cited for claim in case.expected_claims
        )
        citation_scores.append(float(
            citations_relevant
            and values_entailed
            and claims_answered
            and claims_entailed
        ))

        if case.expected_values:
            expected = {value.lower() for value in case.expected_values}
            numeric_scores.append(float(expected <= answer_values))

    answerable_count = len(cases) - unanswerable_count
    return {
        "citation_entailment": sum(citation_scores) / len(citation_scores) if citation_scores else 0.0,
        "numeric_answer_accuracy": sum(numeric_scores) / len(numeric_scores) if numeric_scores else 0.0,
        "refusal_rate": refused_count / len(cases) if cases else 0.0,
        "refusal_accuracy": sum(refusal_correct) / len(refusal_correct) if refusal_correct else 0.0,
        "unanswerable_refusal_recall": (
            unanswerable_refusals / unanswerable_count if unanswerable_count else math.nan
        ),
        "answerable_refusal_false_positive_rate": (
            answerable_refusals / answerable_count if answerable_count else math.nan
        ),
    }


def run_retrieval_evaluation(
    fixture_path: Path = DEFAULT_FIXTURE,
    embedding_cache_path: Path = DEFAULT_EMBEDDING_CACHE,
    *,
    top_k: int = 5,
    predictions_path: Path | None = None,
) -> dict:
    chunks, cases = load_fixture(fixture_path)
    chunk_ids = [str(chunk["id"]) for chunk in chunks]
    embeddings = load_embedding_cache(embedding_cache_path, chunk_ids)
    for chunk in chunks:
        vector = embeddings.get(str(chunk["id"]))
        if vector is None:
            raise RuntimeError(f"embedding cache miss for chunk {chunk['id']}")
        chunk["embedding"] = vector

    kb = KnowledgeBase(str(fixture_path.parent / ".missing-offline-kb.json"))
    kb.loaded = True
    kb.chunks = chunks
    kb.documents = sorted({str(chunk["source"]) for chunk in chunks})
    kb._precompute_embeddings()
    kb._precompute_bm25_index()
    # The retrieval-only contract forbids LLM query expansion.  Every embedding
    # must be an exact cache hit; the replacement never reaches llama_client.
    kb._should_expand_query = lambda *_args, **_kwargs: False

    case_by_question = {case.question: case for case in cases}

    def cached_query_embedding(question: str) -> list[float]:
        case = case_by_question.get(question)
        if case is None:
            raise RuntimeError(f"offline query embedding cache miss: {question!r}")
        vector = embeddings.get(case.vector_key)
        if vector is None:
            raise RuntimeError(
                f"offline query embedding cache miss: case={case.case_id}, key={case.vector_key}"
            )
        return vector

    kb._get_embedding = cached_query_embedding

    per_case = []
    recalls = []
    reciprocal_ranks = []
    ndcgs = []
    numeric_retrieval = []
    tag_totals: dict[str, int] = {}
    tag_hits: dict[str, int] = {}
    for case in cases:
        metadata_filter = {"source": case.source_filter} if case.source_filter else None
        rows = kb._hybrid_search(
            case.question,
            candidate_k=max(top_k, 30),
            metadata_filter=metadata_filter,
        )
        ranked_ids = [str(row.chunk.get("id", "")) for row in rows]
        recall = recall_at_k(ranked_ids, case.relevant, top_k)
        rr = reciprocal_rank(ranked_ids, case.relevant) if case.relevant else math.nan
        ndcg = ndcg_at_k(ranked_ids, case.relevant, top_k)
        if case.relevant:
            recalls.append(recall)
            reciprocal_ranks.append(rr)
            ndcgs.append(ndcg)
            for tag in case.tags:
                tag_totals[tag] = tag_totals.get(tag, 0) + 1
                tag_hits[tag] = tag_hits.get(tag, 0) + int(recall > 0)

        numeric_ok = None
        if case.expected_values:
            top_text = "\n".join(str(row.chunk.get("content", "")) for row in rows[:top_k])
            expected = {value.lower() for value in case.expected_values}
            numeric_ok = expected <= _normalized_values(top_text)
            numeric_retrieval.append(float(numeric_ok))
        per_case.append(
            {
                "case_id": case.case_id,
                "ranked_ids": ranked_ids[:top_k],
                "recall_at_5": None if math.isnan(recall) else recall,
                "reciprocal_rank": None if math.isnan(rr) else rr,
                "ndcg_at_5": None if math.isnan(ndcg) else ndcg,
                "numeric_evidence_exact": numeric_ok,
                "tags": list(case.tags),
            }
        )

    metrics = {
        "case_count": len(cases),
        "answerable_case_count": len(recalls),
        "unanswerable_case_count": sum(case.should_refuse for case in cases),
        "recall_at_5": sum(recalls) / len(recalls),
        "mrr": sum(reciprocal_ranks) / len(reciprocal_ranks),
        "ndcg_at_5": sum(ndcgs) / len(ndcgs),
        "numeric_evidence_exact_at_5": (
            sum(numeric_retrieval) / len(numeric_retrieval) if numeric_retrieval else math.nan
        ),
        "subset_recall_at_5": {
            tag: tag_hits.get(tag, 0) / total for tag, total in sorted(tag_totals.items())
        },
        "embedding_cache": str(embedding_cache_path),
        "llama_server_calls": 0,
    }
    answer_metrics = None
    if predictions_path:
        raw_predictions = _read_json(predictions_path)
        predictions = raw_predictions.get("predictions", raw_predictions)
        if not isinstance(predictions, list):
            raise RuntimeError("predictions file must be a list or {'predictions': [...]} object")
        answer_metrics = score_answer_predictions(cases, chunks, predictions)

    return {"metrics": metrics, "answer_metrics": answer_metrics, "cases": per_case}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline CodeTrail retrieval evaluation")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--embedding-cache", type=Path, default=DEFAULT_EMBEDDING_CACHE)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="print the complete JSON report")
    args = parser.parse_args(argv)
    if args.top_k <= 0:
        parser.error("--top-k must be positive")

    report = run_retrieval_evaluation(
        args.fixture,
        args.embedding_cache,
        top_k=args.top_k,
        predictions_path=args.predictions,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        metrics = report["metrics"]
        print(
            "offline retrieval: "
            f"cases={metrics['case_count']} "
            f"Recall@5={metrics['recall_at_5']:.3f} "
            f"MRR={metrics['mrr']:.3f} "
            f"nDCG@5={metrics['ndcg_at_5']:.3f} "
            f"numeric={metrics['numeric_evidence_exact_at_5']:.3f} "
            "llama_calls=0"
        )
        if report["answer_metrics"] is None:
            print("answer metrics: not scored (pass --predictions for citations/numbers/refusal)")
        else:
            print("answer metrics: " + json.dumps(report["answer_metrics"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
