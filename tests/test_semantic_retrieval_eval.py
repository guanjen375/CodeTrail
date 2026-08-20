#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Real-vector semantic retrieval lane 的契約(施工規格 §6 P1A / §7)。

全部是 **NEW SILENT CONTRACT**:這些行為壞掉不會有人看到紅字,只會看到一份
「看起來很漂亮」的 eval 報表 —— 那正是最危險的失敗模式:

* 向量 cache 靜默 miss 就退回合成向量 → 量到的根本不是 semantic 品質。
* file ranking 不去重 → 同一檔的多個 symbol 各占一個名次,Recall@5 被灌水,
  而且 parser symbol 數一變排名就不公平地漂。
* 用 seed_files 當 gold → edit2ripple 只量到起點,系統性高估 evidence recall。
* union lane 不把 gold 正規化成 repo_id:path → 三個 repo 的同名路徑互撞。

整檔離線,不碰任何 server。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval import record_semantic_vectors as recorder  # noqa: E402
from eval import run_code_smoke_eval as smoke  # noqa: E402
from eval import semantic_retrieval as sr  # noqa: E402

pytestmark = pytest.mark.smoke


def _document(doc_id: str, repo_id: str, path: str, symbol: str, line: int,
              kind: str = "function") -> dict:
    rendered = f"{path} {kind} {symbol}"
    return {
        "doc_id": doc_id,
        "repo_id": repo_id,
        "path": path,
        "kind": kind,
        "qualified_name": symbol,
        "symbol": symbol,
        "line": line,
        "evidence_kind": "definition",
        "rendered_text": rendered,
        "rendered_text_sha256": sr.text_sha256(rendered),
        "file_key": f"{repo_id}:{path}",
        "item": {"path": path, "symbol": symbol, "type": kind, "line": line,
                 "context": rendered},
    }


def _cache(documents: list[dict], queries: dict[str, str],
           dimension: int = 2) -> sr.VectorCache:
    manifest = {
        "model": {"dimension": dimension},
        "pipeline": {"normalization": "none"},
        "documents": [
            {
                "doc_id": doc["doc_id"],
                "rendered_text_sha256": doc["rendered_text_sha256"],
                "vector_row": index,
            }
            for index, doc in enumerate(documents)
        ],
        "queries": [
            {
                "case_id": case_id,
                "rendered_query_sha256": sr.text_sha256(text),
                "vector_row": len(documents) + index,
            }
            for index, (case_id, text) in enumerate(queries.items())
        ],
    }
    rows = [[1.0, 0.0] for _ in documents] + [[1.0, 0.0] for _ in queries]
    return sr.VectorCache(manifest, rows)


# ============================================================
# fail closed
# ============================================================
def test_missing_vector_fails_closed():
    """cache miss 一定 raise,絕不偷偷合成一條向量頂替。"""
    documents = [_document("aaa", "r1", "src/a.c", "alpha", 1)]
    cache = _cache(documents, {"case_1": "question one"})

    unrecorded = _document("zzz", "r1", "src/z.c", "zeta", 9)
    with pytest.raises(sr.VectorCacheError, match="no recorded vector for document"):
        cache.document_vector(unrecorded)

    with pytest.raises(sr.VectorCacheError, match="no recorded query vector"):
        cache.query_vector("case_missing", "question one")


def test_changed_render_fails_closed_instead_of_reusing_a_stale_vector():
    """embed text 一改,舊向量就不是這份文件的向量了 —— 必須 raise,不得沿用。"""
    documents = [_document("aaa", "r1", "src/a.c", "alpha", 1)]
    cache = _cache(documents, {"case_1": "question one"})

    mutated = dict(documents[0])
    mutated["rendered_text"] = "totally different render"
    mutated["rendered_text_sha256"] = sr.text_sha256(mutated["rendered_text"])
    with pytest.raises(sr.VectorCacheError, match="recorded with a different"):
        cache.document_vector(mutated)

    with pytest.raises(sr.VectorCacheError, match="question changed"):
        cache.query_vector("case_1", "an edited question")


def test_stale_pipeline_or_corpus_digest_fails_closed():
    """parser / render / scorer 版本或 corpus digest 一變,舊 artifact 立刻失效。"""
    cache = sr.VectorCache(
        {
            "model": {"dimension": 2},
            "pipeline": {"parser_semantics_version": 1, "normalization": "none"},
            "corpus": {"file_manifest_digest": "f0", "document_manifest_digest": "d0"},
            "documents": [],
            "queries": [],
        },
        [],
    )
    corpus = {"file_manifest_digest": "f0", "document_manifest_digest": "d0"}
    cache.verify_pipeline({"parser_semantics_version": 1}, corpus)  # 相符:不 raise

    with pytest.raises(sr.VectorCacheError, match="pipeline.parser_semantics_version"):
        cache.verify_pipeline({"parser_semantics_version": 2}, corpus)

    with pytest.raises(sr.VectorCacheError, match="document_manifest_digest"):
        cache.verify_pipeline(
            {"parser_semantics_version": 1},
            {"file_manifest_digest": "f0", "document_manifest_digest": "d1"},
        )


def test_degenerate_vectors_are_rejected():
    """零 norm / 非有限 / 維度不符都不能被當成有效向量。"""
    documents = [_document("aaa", "r1", "src/a.c", "alpha", 1)]
    cache = _cache(documents, {})
    cache.rows = [[0.0, 0.0]]
    with pytest.raises(sr.VectorCacheError, match="zero norm"):
        cache.document_vector(documents[0])

    cache.rows = [[float("nan"), 1.0]]
    with pytest.raises(sr.VectorCacheError, match="non-finite"):
        cache.document_vector(documents[0])

    cache.rows = [[1.0, 0.0, 0.0]]
    with pytest.raises(sr.VectorCacheError, match="dims, expected"):
        cache.document_vector(documents[0])


def test_l2_claim_is_actually_verified():
    """manifest 說 normalized 就要真的驗 norm,不能只是貼標籤。"""
    documents = [_document("aaa", "r1", "src/a.c", "alpha", 1)]
    cache = _cache(documents, {})
    cache.normalization = "l2"
    cache.rows = [[3.0, 4.0]]  # norm = 5
    with pytest.raises(sr.VectorCacheError, match="claims l2-normalized"):
        cache.document_vector(documents[0])


# ============================================================
# file aggregation
# ============================================================
def test_same_file_symbols_do_not_occupy_file_topk():
    """同一檔的多個 symbol 只能占 file ranking 的一個名次,分數取該檔最高。"""
    ranking = [
        dict(_document("d1", "r1", "src/a.c", "alpha_one", 1), score=0.9),
        dict(_document("d2", "r1", "src/a.c", "alpha_two", 20), score=0.8),
        dict(_document("d3", "r1", "src/a.c", "alpha_three", 40), score=0.7),
        dict(_document("d4", "r1", "src/b.c", "beta", 1), score=0.6),
        dict(_document("d5", "r1", "src/c.c", "gamma", 1), score=0.5),
    ]
    aggregated = sr.aggregate_files(ranking)

    assert [row["file_key"] for row in aggregated] == [
        "r1:src/a.c", "r1:src/b.c", "r1:src/c.c"
    ], "三個檔就是三個名次,src/a.c 的三個 symbol 不得吃掉三格"
    assert aggregated[0]["score"] == 0.9, "每檔取最高 document score"

    # 沒有聚合的話 gold 的 src/b.c + src/c.c 會被 src/a.c 的 symbol 擠出 top-3。
    metrics = sr.file_metrics(aggregated, {"r1:src/b.c", "r1:src/c.c"}, top_k=3)
    assert metrics["file_recall_at_k"] == 1.0


def test_file_aggregation_is_stable_under_symbol_count_changes():
    """parser 多抽到 symbol 不該讓 file ranking 漂 —— 這是 P2 之後的關鍵不變式。"""
    base = [
        dict(_document("d1", "r1", "src/a.c", "alpha", 1), score=0.9),
        dict(_document("d4", "r1", "src/b.c", "beta", 1), score=0.6),
    ]
    with_more_symbols = base + [
        dict(_document("d2", "r1", "src/a.c", "alpha_macro", 3, "macro"), score=0.55),
        dict(_document("d3", "r1", "src/a.c", "alpha_typedef", 5, "typedef"), score=0.5),
    ]
    assert [row["file_key"] for row in sr.aggregate_files(base)] == \
           [row["file_key"] for row in sr.aggregate_files(with_more_symbols)]


def test_union_scope_normalizes_gold_paths_per_repo():
    """三個 fixture repo 都有 src/app.c;union lane 不正規化就會互相認領。"""
    case = {"repo": "workflow_mini", "gold_files": ["src/app.c"]}
    other = {"repo": "ism_mini", "gold_files": ["src/app.c"]}
    assert sr.gold_file_keys(case) == {"workflow_mini:src/app.c"}
    assert sr.gold_file_keys(case).isdisjoint(sr.gold_file_keys(other))

    ranking = [dict(_document("d1", "ism_mini", "src/app.c", "main", 1), score=0.9)]
    metrics = sr.file_metrics(sr.aggregate_files(ranking), sr.gold_file_keys(case))
    assert metrics["file_recall_at_k"] == 0.0, "別的 repo 的同名路徑不算命中"


# ============================================================
# 計分對象
# ============================================================
def test_edit2ripple_scores_full_gold_files(tmp_path: Path):
    """主指標對完整 gold_files;seed_files 只另報 seed_recall,不得取代。"""
    from code_rag import CodeRAG

    rag = CodeRAG(str(tmp_path))  # 只用 tokenizer / lexical scorer,不建索引
    case = {
        "id": "synthetic_ripple",
        "repo": "r1",
        "family": "edit2ripple",
        "blocking": True,
        "question": "retry attempts budget change ripple",
        # seed 只有起點;gold 還包含應連帶找到的 header 與 test。
        "seed_files": ["src/retry.c"],
        "gold_files": ["src/retry.c", "include/retry_policy.h", "tests/test_retry.c"],
        "gold_symbols": ["retry_execute"],
    }
    documents = [
        _document("d1", "r1", "src/retry.c", "retry_execute", 1),
        _document("d2", "r1", "src/unrelated.c", "unrelated_helper", 1),
    ]

    result = smoke.eval_workflow_retrieval_case(case, {"r1": rag}, {"r1": documents})

    assert result["seed_hit"] is True, "起點有找到 —— 既有 gate 的語意不變"
    assert result["seed_recall_at_5"] == 1.0
    assert result["file_recall_at_5"] == pytest.approx(1 / 3), (
        "3 個 gold 只找到 1 個。拿 seed_files 當 gold 會顯示 1.0,"
        "那正是被修掉的高估"
    )
    assert result["seed_recall_at_5"] > result["file_recall_at_5"]


def test_symbol_recall_is_document_level_and_labels_evidence_kind():
    """symbol recall 用 document ranking 算,不拿 file 聚合的結果假裝成 symbol。"""
    ranking = [
        dict(_document("d1", "r1", "src/a.c", "alpha", 1), score=0.9),
        dict(_document("d2", "r1", "src/a.c", "beta", 20), score=0.8),
    ]
    metrics = sr.symbol_metrics(ranking, ["alpha", "beta", "gamma"])
    assert metrics["symbol_recall_at_k"] == pytest.approx(2 / 3)
    assert metrics["found_symbols"] == ["alpha", "beta"]
    assert metrics["missing_symbols"] == ["gamma"]
    assert metrics["hits_by_evidence_kind"] == {"definition": 2}
    # index 只收 definition;另外兩類要誠實標明來源,不得假造分桶。
    assert metrics["declaration_evidence"] == "not_indexed_by_design"
    assert metrics["unresolved_reference_evidence"] == "graph_lane_only"


# ============================================================
# recorder 的隔離契約
# ============================================================
def test_recorder_query_projection_cannot_leak_answer_fields():
    """query 文字只能來自 question;gold / seed 欄位在結構上就進不去。"""
    cases = [{
        "id": "core_x",
        "question": "where is the retry budget enforced",
        "gold_files": ["src/secret_answer.c"],
        "seed_files": ["src/secret_seed.c"],
        "gold_symbols": ["secret_symbol"],
    }]
    projected = recorder._query_projection(cases)
    assert projected == [{"case_id": "core_x",
                          "question": "where is the retry budget enforced"}]
    serialized = json.dumps(projected)
    for leaked in ("secret_answer", "secret_seed", "secret_symbol"):
        assert leaked not in serialized


def test_checked_in_vector_manifest_carries_verifiable_identity_without_local_paths():
    """manifest 要能驗 model/pooling/dimension/render/scorer/corpus,且不含本機路徑。"""
    if not sr.VECTOR_MANIFEST_FILE.exists():
        pytest.skip("semantic vector artifact not recorded in this checkout")
    manifest = json.loads(sr.VECTOR_MANIFEST_FILE.read_text(encoding="utf-8"))

    assert manifest["model"]["role"] == "embedding"
    assert manifest["model"]["pooling"]
    assert manifest["model"]["dimension"] > 0
    assert manifest["model"]["gguf"]["basename"]
    for key in ("parser_semantics_version", "embed_text_schema_version",
                "query_render_schema_version", "retrieval_scorer_version"):
        assert key in manifest["pipeline"]
    assert manifest["corpus"]["file_manifest_digest"]
    assert manifest["corpus"]["document_manifest_digest"]
    assert manifest["documents"] and manifest["queries"], "document 與 query 都要錄"

    serialized = sr.VECTOR_MANIFEST_FILE.read_text(encoding="utf-8")
    assert "/home/" not in serialized and "/mnt/" not in serialized, (
        "checked-in fixture 不得寫入本機絕對 model path"
    )
    rows = manifest["artifact"]["shape"][0]
    assert rows == len(manifest["documents"]) + len(manifest["queries"])


def test_recorder_requires_explicit_opt_in():
    """錄製是唯一會碰 loopback server 的模式,必須顯式帶旗標。"""
    with pytest.raises(SystemExit):
        recorder.main([])


# ============================================================
# gate 只能擋 blocking family
# ============================================================
def _report(blocking_recall: float, stretch_recall: float) -> dict:
    """最小 report:一個 blocking family、一個 non-blocking stretch family。"""
    return {
        "available": True,
        "primary_scope": "per_repo",
        "primary_lane": "runtime_hybrid",
        "pipeline": {"parser_semantics_version": 1},
        "corpus": {"document_manifest_digest": "d0"},
        "scopes": {
            "per_repo": {
                "cases": [
                    {"id": "core_x", "family": "code2test", "blocking": True},
                    {"id": "stretch_x", "family": "comment2context",
                     "blocking": False},
                ],
                "families": {
                    # 每條 lane 都要有 per-family 輸出(correctness gate 會驗),
                    # 所以 double 也把四條 lane 填滿,不是去弱化那個檢查。
                    lane: {
                        "code2test": {"cases": 1, "file_recall_at_k": blocking_recall,
                                      "mrr": 1.0, "seed_recall_at_k": 1.0,
                                      "symbol_recall_at_k": 1.0},
                        "comment2context": {"cases": 1,
                                            "file_recall_at_k": stretch_recall,
                                            "mrr": 1.0, "seed_recall_at_k": 1.0,
                                            "symbol_recall_at_k": 1.0},
                    }
                    for lane in sr.LANES
                },
            },
            "union": {
                "cases": [],
                "families": {lane: {"code2test": {
                    "cases": 1, "file_recall_at_k": 1.0, "mrr": 1.0,
                    "seed_recall_at_k": 1.0, "symbol_recall_at_k": 1.0,
                }} for lane in sr.LANES},
            },
        },
    }


def _baseline_file(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "semantic_retrieval_baseline.json"
    path.write_text(json.dumps({
        "pipeline": {"parser_semantics_version": 1},
        "corpus": {"document_manifest_digest": "d0"},
        "scopes": {"per_repo": {"families": {"runtime_hybrid": {
            "code2test": {"file_recall_at_k": 1.0, "mrr": 1.0,
                          "symbol_recall_at_k": 1.0},
            "comment2context": {"file_recall_at_k": 1.0, "mrr": 1.0,
                                "symbol_recall_at_k": 1.0},
        }}}},
    }), encoding="utf-8")
    monkeypatch.setattr(sr, "SEMANTIC_BASELINE_FILE", path)
    return path


def test_non_blocking_stretch_family_does_not_gate(tmp_path: Path, monkeypatch):
    """BUG REGRESSION:non-blocking stretch 被 baseline gate 升格成 blocking。

    `comment2context` 與 `low_lexical_overlap` 在 fixture 裡明確是
    `blocking: false`(provisional diagnostic family),但 no-regression 比較對
    baseline 裡**每一個** family 一視同仁地產生 failure —— 等於偷偷把 stretch
    變成擋 gate 的條件,和 fixture 與文件的宣稱直接矛盾。
    """
    _baseline_file(tmp_path, monkeypatch)

    # stretch 掉下去:只能是診斷,不得擋 gate。
    failures = smoke.semantic_gate_failures(_report(1.0, 0.10))
    assert failures == [], f"non-blocking family 不得擋 gate,卻擋了:{failures}"

    # blocking 掉下去:一定要擋。
    failures = smoke.semantic_gate_failures(_report(0.10, 1.0))
    assert any("code2test" in item for item in failures), (
        "blocking family 退步必須擋 gate"
    )
    assert not any("comment2context" in item for item in failures)


def test_blocking_family_set_comes_from_the_case_data(tmp_path: Path, monkeypatch):
    """哪些 family 算 blocking 要從 case 的 blocking 欄位推,不得寫死清單。"""
    _baseline_file(tmp_path, monkeypatch)
    report = _report(0.10, 0.10)
    # 把唯一的 blocking case 改成 non-blocking → 就不該再有任何 failure。
    for row in report["scopes"]["per_repo"]["cases"]:
        row["blocking"] = False
    assert smoke.semantic_gate_failures(report) == []
