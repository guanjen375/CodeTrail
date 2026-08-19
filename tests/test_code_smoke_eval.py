"""Expanded code-inference smoke fixture/metric contracts; fully offline."""
from __future__ import annotations

from pathlib import Path

import pytest

from eval import run_code_smoke_eval as smoke


def test_fixture_has_legacy_floor_twenty_core_and_bounded_stretch():
    data = smoke.load_cases()
    cases = data["cases"]
    legacy = [case for case in cases if case["id"].startswith("smoke_")]
    core = [case for case in cases if case.get("blocking") is True]
    stretch = [case for case in cases if case["id"].startswith("stretch_")]

    assert len(legacy) == smoke.LEGACY_CASE_COUNT == 16
    assert len(core) == smoke.BLOCKING_CORE_COUNT == 20
    assert 1 <= len(stretch) <= smoke.MAX_STRETCH_CASES
    assert {
        family: sum(case.get("family") == family for case in core)
        for family in smoke.CORE_FAMILIES
    } == {family: 4 for family in smoke.CORE_FAMILIES}
    assert not any("validation_command" in case for case in cases)


def test_fixture_repos_exist_and_use_only_checked_in_synthetic_roots():
    data = smoke.load_cases()
    for info in data["repos"].values():
        root = smoke.FIXTURE_DIR / info["root"]
        assert root.is_dir()
        assert root.resolve().is_relative_to(smoke.FIXTURE_DIR.resolve())
    serialized = smoke.CASES_FILE.read_text(encoding="utf-8").lower()
    assert "knowledge.json" not in serialized
    assert "data/" not in serialized
    assert ".jsonl" not in serialized


def test_pseudo_embedding_is_deterministic_plumbing_stub():
    first = smoke.pseudo_embedding("  alpha\n beta  ")
    second = smoke.pseudo_embedding("alpha beta")
    different = smoke.pseudo_embedding("alpha gamma")
    assert first == second
    assert first != different
    assert len(first) == smoke.PSEUDO_EMBED_DIM
    assert all(-1.0 <= value <= 1.0 for value in first)


def test_offline_poison_session_rejects_every_http_operation():
    session = smoke._poison_get_session()
    with pytest.raises(RuntimeError, match="attempted HTTP"):
        session.post("http://127.0.0.1:1/embedding")
    with pytest.raises(RuntimeError, match="attempted HTTP"):
        session.get("http://127.0.0.1:1/health")


def test_retrieval_metrics_compute_recall_and_mrr_by_rank():
    results = [
        {"path": "noise.c"},
        {"path": "src/root.c"},
        {"path": "tests/root_test.c"},
    ]
    metrics = smoke.retrieval_metrics(results, ["src/root.c", "tests/root_test.c"])
    assert metrics["file_recall_at_5"] == 1.0
    assert metrics["mrr"] == 0.5
    assert metrics["hit_files"] == ["src/root.c", "tests/root_test.c"]


def test_budgeted_context_metrics_count_only_actual_evidence_text_chars():
    bundle = {
        "budget_chars": 20,
        "used_chars": 7,
        "evidence": [
            {"path": "src/a.c", "text": "abc"},
            {"path": "tests/a.c", "text": "defg"},
            {"path": "noise.c", "text": ""},
        ],
        "truncated": True,
    }
    metrics = smoke.budgeted_context_metrics(bundle, ["src/a.c", "tests/a.c"])
    assert metrics["used_chars"] == 7
    assert metrics["within_budget"] is True
    assert metrics["gold_file_coverage"] == 1.0
    assert metrics["evidence_precision"] == pytest.approx(2 / 3)
    assert metrics["truncated"] is True


def test_fixture_copy_keeps_generated_cache_out_of_checked_in_tree(tmp_path: Path):
    data = smoke.load_cases()
    before = {
        path.relative_to(smoke.FIXTURE_DIR)
        for path in smoke.FIXTURE_DIR.rglob(".code_rag*")
    }
    roots = smoke.copy_fixture_repos(data, tmp_path)
    smoke.install_offline_stubs()
    smoke.build_rags(roots)
    after = {
        path.relative_to(smoke.FIXTURE_DIR)
        for path in smoke.FIXTURE_DIR.rglob(".code_rag*")
    }
    assert after == before
    assert any(path.name.startswith(".code_rag") for path in tmp_path.rglob(".code_rag*"))
