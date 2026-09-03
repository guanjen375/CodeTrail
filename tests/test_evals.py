"""eval/ 底下各 runner 的契約:tool-routing、code smoke、retrieval、semantic retrieval、
data flywheel 與私人 session eval。整檔離線:不起 OpenCode / MCP 子行程、不連 model
server、不碰網路。

合併自(2026-09-02):

* tests/test_tool_routing_eval.py —— tool-routing evaluator 的純合成契約:deterministic
  catalog counting、JSONL replay、classification、support gating、result privacy。
* tests/test_code_smoke_eval.py —— 擴充後的 code-inference smoke fixture / metric 契約。
* tests/test_retrieval_eval.py —— 離線 retrieval eval:fixture 展開數、ranking 指標、
  絕不打 embedding server、prediction 計分。
* tests/test_semantic_retrieval_eval.py —— real-vector semantic retrieval lane 的契約
  (施工規格 §6 P1A / §7)。全部是 **NEW SILENT CONTRACT**:這些行為壞掉不會有人
  看到紅字,只會看到一份「看起來很漂亮」的 eval 報表 —— 那正是最危險的失敗模式:
    - 向量 cache 靜默 miss 就退回合成向量 → 量到的根本不是 semantic 品質。
    - file ranking 不去重 → 同一檔的多個 symbol 各占一個名次,Recall@5 被灌水,
      而且 parser symbol 數一變排名就不公平地漂。
    - 用 seed_files 當 gold → edit2ripple 只量到起點,系統性高估 evidence recall。
    - union lane 不把 gold 正規化成 repo_id:path → 三個 repo 的同名路徑互撞。
* tests/test_data_flywheel.py —— reproducibility info 記的是 call-time 的主模型。
* tests/test_session_eval.py —— 私人 OpenCode session-model eval lane 的安全契約。

smoke 成員資格:semantic retrieval 與 session eval 兩段原本是整檔 smoke,合併後改成
逐條 `@pytest.mark.smoke`;tool-routing 只有 result privacy 那條是 smoke;其餘三段
不在 smoke 包。
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import knowledge  # noqa: E402
import session_eval  # noqa: E402
from eval import record_semantic_vectors as recorder  # noqa: E402
from eval import run_code_smoke_eval as smoke  # noqa: E402
from eval import run_retrieval_eval  # noqa: E402
from eval import semantic_retrieval as sr  # noqa: E402
from scripts import eval_tool_routing as routing  # noqa: E402
from scripts import mcp_catalog  # noqa: E402
from scripts import session_eval as session_eval_cli  # noqa: E402

# ── 原 test_tool_routing_eval.py:tool-routing evaluator 的純合成契約 ──

SCHEMAS = {
    "list_dir": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "depth": {"type": "integer"}},
        "required": ["path"],
        "additionalProperties": False,
    },
    "read_file": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    },
    "grep_code": {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
        "additionalProperties": False,
    },
    "code_rag_search": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "mode": {"enum": ["semantic", "neighbors", "path", "context"]},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


def _case(
    *,
    case_id: str = "synthetic",
    tools: list[str] | None = None,
    marker: str | None = "CT_MARK_OK",
    forbidden: str | None = None,
) -> dict:
    tools = ["read_file"] if tools is None else tools
    return {
        "id": case_id,
        "prompt": "synthetic prompt not persisted",
        "language": "en",
        "category": "read" if tools else "no_tool",
        "expected": {
            "tools": tools,
            "allowed_tools": tools,
            "arg_rules": {"read_file": {"path": {"equals": "docs/synthetic.md"}}}
            if "read_file" in tools
            else {},
            "required_markers": [marker] if marker else [],
            "forbidden_markers": [forbidden] if forbidden else [],
        },
    }


def _tool_event(
    tool: str = "codetrail_read_file",
    arguments: object = None,
    *,
    status: str = "completed",
) -> dict:
    if arguments is None:
        arguments = {"path": "docs/synthetic.md"}
    return {
        "type": "tool_use",
        "sessionID": "ses_synthetic",
        "part": {
            "tool": tool,
            "state": {"status": status, "input": arguments, "output": "not inspected"},
        },
    }


def _text_event(text: str) -> dict:
    return {"type": "text", "part": {"type": "text", "text": text}}


def _terminal_event(*, tokens: int = 3) -> dict:
    return {
        "type": "step_finish",
        "part": {
            "type": "step-finish",
            "reason": "stop",
            "tokens": {"input": tokens, "output": 1, "total": tokens + 1},
        },
    }


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events)


def _trace(*events: dict) -> routing.AttemptTrace:
    return routing.parse_event_stream(_stream(*events), latency_ms=7)


def test_catalog_count_rule_and_usage_token_probe_are_exact():
    tools = [
        {
            "name": "read_file",
            "description": "Read synthetic text.",
            "inputSchema": SCHEMAS["read_file"],
            "outputSchema": {"type": "object", "properties": {"result": {"type": "string"}}},
        }
    ]
    snapshot = mcp_catalog.measure_catalog(tools, instructions="route safely", source="synthetic")
    assert snapshot.description_chars == len("Read synthetic text.")
    assert snapshot.input_schema_chars == len(
        json.dumps(SCHEMAS["read_file"], ensure_ascii=False, sort_keys=True)
    )
    assert snapshot.catalog_chars == (
        snapshot.description_chars + snapshot.input_schema_chars + snapshot.output_schema_chars
    )

    calls = []

    def post(path, payload):
        calls.append((path, deepcopy(payload)))
        if path == "/v1/chat/completions":
            prompt_tokens = 11 if "tools" not in payload else 29
            return {"usage": {"prompt_tokens": prompt_tokens}}
        if path == "/apply-template":
            instructed = payload["messages"][0]["role"] == "system"
            return {"prompt": "instructed" if instructed else "plain"}
        assert path == "/tokenize"
        return {"tokens": [1] * (7 if payload["content"] == "instructed" else 5)}

    measured = mcp_catalog.measure_catalog_prompt_tokens(
        model="synthetic/model",
        catalog=snapshot,
        post_json=post,
    )
    assert measured.catalog_prompt_tokens == 20
    assert measured.instructions_prompt_tokens == 2
    assert measured.method == "usage.prompt_tokens+instructions(apply-template+tokenize)"
    assert len(calls) == 6
    assert calls[0][1]["messages"] == calls[1][1]["messages"] == [{"role": "user", "content": "."}]
    assert calls[0][1]["max_tokens"] == calls[1][1]["max_tokens"] == 1
    assert "tools" not in calls[0][1]
    assert "tools" in calls[1][1]
    with_tools = deepcopy(calls[1][1])
    with_tools.pop("tools")
    assert calls[0][1] == with_tools
    assert calls[2][0] == calls[3][0] == "/apply-template"
    assert calls[2][1]["add_generation_prompt"] is calls[3][1]["add_generation_prompt"] is True
    assert calls[2][1]["tools"] == calls[3][1]["tools"] == calls[1][1]["tools"]
    assert calls[2][1]["messages"][0]["role"] == "user"
    assert calls[3][1]["messages"][0] == {"role": "system", "content": "route safely"}


def test_catalog_token_probe_falls_back_symmetrically_without_usage():
    snapshot = mcp_catalog.measure_catalog(
        [{"name": "read_file", "description": "x", "inputSchema": SCHEMAS["read_file"]}],
        source="synthetic",
    )
    templates = []

    def post(path, payload):
        if path == "/v1/chat/completions":
            return {"choices": []}
        if path == "/apply-template":
            templates.append(deepcopy(payload))
            return {"prompt": "with tools" if "tools" in payload else "plain"}
        assert path == "/tokenize"
        return {"tokens": [1] * (12 if payload["content"] == "with tools" else 5)}

    measured = mcp_catalog.measure_catalog_prompt_tokens(
        model="synthetic/model",
        catalog=snapshot,
        post_json=post,
    )
    assert measured.method == "apply-template+tokenize"
    assert measured.catalog_prompt_tokens == 7
    assert templates[0]["messages"] == templates[1]["messages"]
    assert templates[0]["add_generation_prompt"] is True
    assert templates[1]["add_generation_prompt"] is True
    assert "tools" not in templates[0]
    assert "tools" in templates[1]


def test_model_probe_endpoint_must_match_effective_opencode_provider():
    config = {
        "provider": {
            "llamacpp": {
                "options": {"baseURL": "http://localhost:8080/v1"},
            }
        }
    }
    assert (
        routing._model_server_base_url(
            config,
            model="llamacpp/synthetic",
            environment={},
        )
        == "http://localhost:8080"
    )
    with pytest.raises(routing.EvalError):
        routing._model_server_base_url(
            config,
            model="llamacpp/synthetic",
            environment={"AICODE_LLAMA_BASE_URL": "http://localhost:9090"},
        )


def test_bilingual_fixture_covers_all_required_intents_and_ascii_query_contract(tmp_path):
    fixture = routing.load_cases()
    pairs = {(case["category"], case["language"]) for case in fixture["cases"]}
    for category in (
        "directory",
        "exact_grep",
        "read",
        "cross_file",
        "spec",
        "missing_data_refusal",
        "no_tool",
    ):
        assert (category, "en") in pairs
        assert (category, "zh") in pairs

    mixed = next(case for case in fixture["cases"] if case["language"] == "zh-en")
    query_rule = mixed["expected"]["arg_rules"]["code_rag_search"]["query"]
    assert query_rule["ascii_ratio_min"] >= 0.9
    assert "resolve_retry_budget" in mixed["prompt"]

    destination = tmp_path / "synthetic"
    routing.materialize_synthetic_fixture(fixture, destination)
    assert (destination / "src" / "retry_policy.py").is_file()
    assert (destination / fixture["fixture"]["root_canary"]).is_file()
    assert all(not item["path"].startswith("/") for item in fixture["fixture"]["files"])
    assert not (destination / "knowledge.json").exists()


def test_classifier_replays_all_seven_public_classifications():
    case = _case()
    traces = {
        routing.Classification.TOOL_SUCCESS: _trace(
            _tool_event(), _text_event("CT_MARK_OK"), _terminal_event()
        ),
        routing.Classification.WRONG_TOOL: _trace(
            _tool_event("codetrail_grep_code", {"pattern": "x"}),
            _text_event("CT_MARK_OK"),
            _terminal_event(),
        ),
        routing.Classification.INVALID_ARGS: _trace(
            _tool_event(arguments={"path": 7}), _text_event("CT_MARK_OK"), _terminal_event()
        ),
        routing.Classification.PROMISE_WITHOUT_CALL: _trace(
            _text_event("I will call the read tool now."), _terminal_event()
        ),
        routing.Classification.EMPTY_TURN: _trace(_terminal_event()),
        routing.Classification.MARKER_LEAK: _trace(
            _text_event("<tool_call>fake</tool_call>"), _terminal_event()
        ),
        routing.Classification.HARNESS_INVALID: _trace(_text_event("CT_MARK_OK")),
    }
    assert set(traces) == set(routing.Classification)
    for expected, trace in traces.items():
        outcome = routing.classify_attempt(case, trace, schemas=SCHEMAS)
        assert outcome.classification is expected


def test_no_tool_case_does_not_treat_an_unfulfilled_promise_as_success():
    trace = _trace(_text_event("Let me check that with a tool."), _terminal_event())
    outcome = routing.classify_attempt(
        _case(tools=[], marker=None),
        trace,
        schemas=SCHEMAS,
    )
    assert outcome.classification is routing.Classification.PROMISE_WITHOUT_CALL


def test_only_completed_structured_tool_event_can_succeed():
    case = _case()
    pending = _trace(
        _tool_event(status="running"),
        _text_event("CT_MARK_OK"),
        _terminal_event(),
    )
    outcome = routing.classify_attempt(case, pending, schemas=SCHEMAS)
    assert outcome.classification is routing.Classification.WRONG_TOOL

    prose = _trace(
        _text_event('codetrail_read_file({"path":"docs/synthetic.md"}) CT_MARK_OK'),
        _terminal_event(),
    )
    outcome = routing.classify_attempt(case, prose, schemas=SCHEMAS)
    assert outcome.classification is routing.Classification.WRONG_TOOL


def test_reasoning_text_never_becomes_assistant_text_or_marker_evidence():
    reasoning = {
        "type": "reasoning",
        "part": {
            "type": "reasoning",
            "text": "<tool_call>CT_MARK_OK must remain private reasoning",
        },
    }
    trace = _trace(
        reasoning,
        _tool_event(),
        _text_event("CT_MARK_OK"),
        _terminal_event(),
    )
    assert trace.assistant_text == "CT_MARK_OK"
    assert trace.marker_leak is False
    outcome = routing.classify_attempt(_case(), trace, schemas=SCHEMAS)
    assert outcome.classification is routing.Classification.TOOL_SUCCESS


def test_schema_metric_stays_distinct_from_case_argument_contract():
    trace = _trace(
        _tool_event(arguments={"path": "docs/other.md"}),
        _text_event("CT_MARK_OK"),
        _terminal_event(),
    )
    outcome = routing.classify_attempt(_case(), trace, schemas=SCHEMAS)
    assert outcome.classification is routing.Classification.INVALID_ARGS
    assert outcome.schema_calls == 1
    assert outcome.schema_valid_calls == 1


@pytest.mark.parametrize("marker", routing.MARKER_LEAK_TOKENS)
def test_every_marker_leak_spelling_is_deterministic(marker):
    trace = _trace(_text_event(f"prefix {marker} suffix"), _terminal_event())
    outcome = routing.classify_attempt(_case(), trace, schemas=SCHEMAS)
    assert outcome.classification is routing.Classification.MARKER_LEAK


def test_third_identical_completed_call_is_never_success():
    trace = _trace(
        _tool_event(),
        _tool_event(),
        _tool_event(),
        _text_event("CT_MARK_OK"),
        _terminal_event(),
    )
    outcome = routing.classify_attempt(_case(), trace, schemas=SCHEMAS)
    assert outcome.classification is routing.Classification.WRONG_TOOL
    assert outcome.third_identical_call is True


def test_missing_terminal_retries_once_and_harness_invalid_is_excluded():
    first = routing.parse_event_stream(
        _stream(_text_event("partial"), {"type": "compaction"}),
        latency_ms=5,
    )
    second = _trace(_tool_event(), _text_event("CT_MARK_OK"), _terminal_event(tokens=4))
    outcome = routing.evaluate_attempts(_case(), [first, second], schemas=SCHEMAS)
    assert outcome.classification is routing.Classification.TOOL_SUCCESS
    assert outcome.terminal_retry is True
    assert outcome.compaction_events == 1

    missing_twice = routing.evaluate_attempts(
        _case(case_id="invalid"),
        [first, first],
        schemas=SCHEMAS,
    )
    aggregate = routing.aggregate_outcomes([outcome, missing_twice])
    assert aggregate["case_count"] == 2
    assert aggregate["model_denominator"] == 1
    assert aggregate["harness_invalid_count"] == 1
    assert aggregate["compaction"]["terminal_retries"] == 2


def test_missing_data_bait_is_a_failure_even_with_required_refusal_marker():
    case = _case(marker="CT_REFUSE_NO_EVIDENCE", forbidden="CT_BAIT_ORBIT_991")
    trace = _trace(
        _tool_event(),
        _text_event("CT_REFUSE_NO_EVIDENCE but perhaps CT_BAIT_ORBIT_991"),
        _terminal_event(),
    )
    outcome = routing.classify_attempt(case, trace, schemas=SCHEMAS)
    assert outcome.classification is routing.Classification.WRONG_TOOL
    assert outcome.bait_assertion is True


def test_support_gate_uses_row_local_baseline_without_promoting_measured():
    row = {
        "id": "synthetic-row",
        "status": "measured",
        "baseline": {
            "catalog": {"catalog_prompt_tokens": 100},
            "routing": {
                "tool_needed": {"recall": 0.7},
                "no_tool": {"precision": 0.8},
                "grounding": {"adoption_rate": 0.8},
            },
        },
    }
    aggregate = {
        "harness_invalid_count": 0,
        # `aggregate_outcomes` 一定會出這個欄位（eval_tool_routing.py 的
        # `"model_denominator": len(valid)`）。structured-call 成功率門檻把
        # 「缺這個欄位」視為未量測而 fail-loud，所以手寫的 aggregate 也要跟
        # 真的產出同形狀，否則測的是一份production 不會出現的輸入。
        "model_denominator": 5,
        "tool_needed": {"count": 10, "recall": 0.9},
        "no_tool": {"precision": 0.9},
        "schema": {"valid_rate": 1.0},
        "grounding": {"adoption_rate": 0.9, "bait_assertions": 0},
        "failure_guards": {
            "promise_without_call": 0,
            "third_identical_call": 0,
            "marker_leak": 0,
            "empty_turn": 0,
            "fake_xml_counted_success": 0,
            # `aggregate_outcomes` 一定會出這個欄位（直接數「完全沒有 structured
            # call 的輪數」）。手寫 aggregate 要跟它同形狀，否則測到的是一份
            # production 不會出現的輸入。
            "no_structured_call": 0,
        },
    }
    original = deepcopy(row)
    verdict = routing.evaluate_support_gate(
        aggregate=aggregate,
        row=row,
        thresholds={},
        catalog_prompt_tokens=60,
        explicit_canary_rate=1.0,
        ask_permission_preserved=True,
    )
    assert verdict.passed is True
    assert row == original
    public = verdict.private_record(current_status=row["status"])
    assert public["matrix_status"] == "measured"
    assert public["manual_status_change_required"] is True


@pytest.mark.smoke
def test_result_privacy_allows_only_metrics_and_compatibility_identity():
    outcome = routing.classify_attempt(
        _case(),
        _trace(_tool_event(), _text_event("CT_MARK_OK"), _terminal_event()),
        schemas=SCHEMAS,
    )
    result = {
        "schema_version": 1,
        "mode": "model",
        "compatibility": {
            "matrix_row": "synthetic-row",
            "arm": "baseline",
            "tools_digest": "0" * 64,
        },
        "cases": [outcome.private_record()],
        "aggregate": routing.aggregate_outcomes([outcome]),
        "support_gate": {
            "passed": False,
            "checks": {"synthetic": False},
            "matrix_status": "measured",
            "manual_status_change_required": False,
        },
    }
    routing.validate_private_result(result)

    leaked_token_prompt = deepcopy(result)
    leaked_token_prompt["cases"][0]["tokens"]["prompt"] = "secret prompt"
    with pytest.raises(routing.EvalError):
        routing.validate_private_result(leaked_token_prompt)

    for forbidden in (
        {"root": "/private/project"},
        {"prompt": "secret prompt"},
        {"tool_args": {"path": "source.c"}},
        {"session_ids": ["ses_private"]},
        {"notes": "/absolute/project/path"},
    ):
        leaked = deepcopy(result)
        leaked["aggregate"]["leak"] = forbidden
        with pytest.raises(routing.EvalError):
            routing.validate_private_result(leaked)


def test_saved_baseline_reproduces_pre_t2_live_measurement_and_stays_measured():
    matrix = routing.load_support_matrix()
    row = matrix["rows"][0]
    catalog = row["baseline"]["catalog"]
    assert row["status"] == "measured"
    assert catalog["tool_count"] == 19
    assert catalog["description_chars"] == 25_847
    assert catalog["input_schema_chars"] == 5_044
    assert catalog["output_schema_chars"] == 2_408
    assert catalog["catalog_chars"] == 33_299
    assert catalog["opencode_effective_chars"] == 30_891
    assert catalog["instructions_chars"] == 0
    assert len(catalog["per_tool"]) == 19


def test_frozen_contract_accepts_only_the_exact_saved_historical_catalog():
    matrix = routing.load_support_matrix()
    row = matrix["rows"][0]
    saved = row["baseline"]["catalog"]
    tools = tuple(
        mcp_catalog.CatalogTool(item["name"], "", {}, None, {"name": item["name"]})
        for item in saved["per_tool"]
    )
    snapshot = mcp_catalog.CatalogSnapshot(
        source="synthetic_historical_stdio",
        tools=tools,
        instructions="",
        per_tool=tuple(mcp_catalog.ToolCatalogCount(**item) for item in saved["per_tool"]),
        description_chars=saved["description_chars"],
        input_schema_chars=saved["input_schema_chars"],
        output_schema_chars=saved["output_schema_chars"],
        catalog_chars=saved["catalog_chars"],
        opencode_effective_chars=saved["opencode_effective_chars"],
        tools_digest=saved["tools_digest"],
        instructions_digest=saved["instructions_digest"],
        canonical_tools_list_chars=saved["canonical_tools_list_chars"],
    )

    parsed = routing.build_parser().parse_args(
        [
            "--root",
            ".",
            "--matrix-row",
            row["id"],
            "--arm",
            "baseline",
            "--output",
            "synthetic-result.json",
            "--catalog-only",
            "--frozen-contract",
        ]
    )
    assert parsed.frozen_contract is True
    with pytest.raises(mcp_catalog.CatalogError):
        mcp_catalog.assert_public_tool_contract(snapshot)
    routing.assert_frozen_catalog_contract(snapshot, row)
    with pytest.raises(routing.EvalError):
        routing.assert_frozen_catalog_contract(
            replace(snapshot, tools_digest="f" * 64),
            row,
        )
    with pytest.raises(routing.EvalError):
        routing.assert_frozen_catalog_contract(
            replace(snapshot, tools=tuple(reversed(snapshot.tools))),
            row,
        )


# ── 原 test_code_smoke_eval.py:code-inference smoke fixture / metric 契約 ──

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
    with smoke.install_offline_stubs():
        smoke.build_rags(roots)
    after = {
        path.relative_to(smoke.FIXTURE_DIR)
        for path in smoke.FIXTURE_DIR.rglob(".code_rag*")
    }
    assert after == before
    assert any(path.name.startswith(".code_rag") for path in tmp_path.rglob(".code_rag*"))


def test_offline_stubs_restore_process_global_clients_and_reranker():
    import code_rag
    import http_client
    import llama_client

    originals = (
        llama_client.get_session,
        http_client.get_session,
        llama_client.embed_one,
        llama_client.embed_batch,
        code_rag.USE_RERANKER,
    )
    with smoke.install_offline_stubs():
        assert llama_client.get_session is smoke._poison_get_session
        assert http_client.get_session is smoke._poison_get_session
        assert code_rag.USE_RERANKER is False

    assert (
        llama_client.get_session,
        http_client.get_session,
        llama_client.embed_one,
        llama_client.embed_batch,
        code_rag.USE_RERANKER,
    ) == originals


# ── 原 test_retrieval_eval.py:離線 retrieval eval 的 fixture / 指標 / 計分契約 ──

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


# ── 原 test_semantic_retrieval_eval.py:real-vector semantic retrieval lane(整段 smoke) ──

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
@pytest.mark.smoke
def test_missing_vector_fails_closed():
    """cache miss 一定 raise,絕不偷偷合成一條向量頂替。"""
    documents = [_document("aaa", "r1", "src/a.c", "alpha", 1)]
    cache = _cache(documents, {"case_1": "question one"})

    unrecorded = _document("zzz", "r1", "src/z.c", "zeta", 9)
    with pytest.raises(sr.VectorCacheError, match="no recorded vector for document"):
        cache.document_vector(unrecorded)

    with pytest.raises(sr.VectorCacheError, match="no recorded query vector"):
        cache.query_vector("case_missing", "question one")


@pytest.mark.smoke
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


@pytest.mark.smoke
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


@pytest.mark.smoke
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


@pytest.mark.smoke
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
@pytest.mark.smoke
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


@pytest.mark.smoke
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


@pytest.mark.smoke
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
@pytest.mark.smoke
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


@pytest.mark.smoke
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
@pytest.mark.smoke
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


@pytest.mark.smoke
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


@pytest.mark.smoke
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


@pytest.mark.smoke
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


@pytest.mark.smoke
def test_blocking_family_set_comes_from_the_case_data(tmp_path: Path, monkeypatch):
    """哪些 family 算 blocking 要從 case 的 blocking 欄位推,不得寫死清單。"""
    _baseline_file(tmp_path, monkeypatch)
    report = _report(0.10, 0.10)
    # 把唯一的 blocking case 改成 non-blocking → 就不該再有任何 failure。
    for row in report["scopes"]["per_repo"]["cases"]:
        row["blocking"] = False
    assert smoke.semantic_gate_failures(report) == []


@pytest.mark.smoke
@pytest.mark.parametrize("artifact", ["vectors", "baseline"])
def test_checked_in_pipeline_identity_matches_the_current_code(artifact):
    """錄好的 artifact 的 pipeline 欄位必須與**現在的程式碼**相符。

    2026-08-21 踩到的無聲缺口:``RETRIEVAL_SCORER_VERSION`` 從 1 bump 到 2 之後,
    ``eval/run_code_smoke_eval.py`` 直接 GATE FAIL(``vector cache stale``),
    但整個 pytest 套件是綠的 —— 唯一碰真 artifact 的測試只 assert key 存在、
    不比對值,而 ``VectorCache.load()`` 只有 eval 腳本會呼叫,不在測試裡。
    於是「該重錄卻沒重錄」這件事只有真的去跑 eval 才看得到。

    這條測試就是把 ``VectorCache.verify_pipeline`` 的那半邊搬進 pytest。
    紅了就是要重錄:
        python3 eval/record_semantic_vectors.py --record-vectors
        python3 eval/run_code_smoke_eval.py --record-semantic-baseline
    """
    path = sr.VECTOR_MANIFEST_FILE if artifact == "vectors" else sr.SEMANTIC_BASELINE_FILE
    if not path.exists():
        pytest.skip(f"{path.name} not recorded in this checkout")

    recorded = json.loads(path.read_text(encoding="utf-8")).get("pipeline", {})
    drift = {
        key: (recorded.get(key), value)
        for key, value in sr.pipeline_identity().items()
        if recorded.get(key) != value
    }
    assert not drift, (
        f"{path.name} 的 pipeline 與現行程式碼不符(recorded, current):{drift};"
        " artifact 要重錄,否則 eval gate 會 FAIL 而 pytest 看不到"
    )


# ── 原 test_data_flywheel.py:reproducibility info 記的是 call-time 的主模型 ──

@pytest.mark.parametrize(
    "aicode_model,expected_tag",
    [("foo:bar", "foo:bar"), (None, "")],
    ids=["records_calltime_main_model", "omits_missing_main_model_without_crashing"],
)
def test_reproducibility_info_model_tag_follows_calltime_main_model(
    monkeypatch, tmp_path, aicode_model, expected_tag
):
    """AICODE_MODEL 有設時 model_tag 記下 call-time 的主模型;沒設時留空字串、不得 crash。"""
    import data_flywheel

    if aicode_model is None:
        monkeypatch.delenv("AICODE_MODEL", raising=False)
    else:
        monkeypatch.setenv("AICODE_MODEL", aicode_model)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    info = data_flywheel.get_reproducibility_info()

    assert info["model_tag"] == expected_tag


# ── 原 test_session_eval.py:私人 session eval lane 的安全契約(整段 smoke) ──

def _export(*, assistant_text: str = "SECRET MODEL ANSWER") -> dict:
    return {
        "info": {
            "id": "ses_private_123",
            "directory": "/private/project",
        },
        "messages": [
            {
                "info": {"role": "user"},
                "parts": [{"type": "text", "text": "使用者真實問題"}],
            },
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": assistant_text}],
            },
            {
                "info": {"role": "user"},
                "parts": [{"type": "text", "text": "你寫錯了吧，位址是十六進位"}],
            },
        ],
    }


def _suite() -> dict:
    return {
        "schema_version": 1,
        "name": "private_test",
        "source_policy": "user_and_external_evidence_only",
        "cases": [
            {
                "id": "code_case",
                "task_type": "code_qa",
                "project_root": "/private/project",
                "source": {
                    "session_hash": "0123456789abcdef",
                    "export_digest": "1" * 64,
                    "user_turn_indices": [0, 1],
                },
                "turns": [
                    {"kind": "prompt", "text": "問題"},
                    {"kind": "evidence", "text": "新增資訊或限制：位址是十六進位"},
                ],
                "read_only": True,
                "state_paths": [],
                "verifier": {
                    "oracle_kind": "human_pairwise",
                    "checks": [{"type": "terminal"}],
                    "human_dimensions": ["正確", "有證據"],
                },
            }
        ],
    }


def _result(label: str, fingerprint_char: str, answer: str) -> dict:
    suite = _suite()
    return {
        "schema_version": 1,
        "suite_digest": session_eval.suite_digest(suite),
        "candidate": {
            "label": label,
            "model": f"llamacpp/{label}",
            "fingerprint": fingerprint_char * 64,
        },
        "cases": [
            {
                "case_id": "code_case",
                "project_state_digest": "a" * 64,
                "turns": [
                    {"assistant_text": answer},
                    {"assistant_text": answer + " updated"},
                ],
                "automatic_checks": [{"type": "terminal", "passed": True}],
            }
        ],
        "aggregate": {"complete": True},
    }


@pytest.mark.smoke
def test_mined_draft_excludes_assistant_text_and_raw_session_id():
    draft = session_eval.draft_from_export(_export())
    encoded = json.dumps(draft, ensure_ascii=False)

    assert "SECRET MODEL ANSWER" not in encoded
    assert "ses_private_123" not in encoded
    assert "使用者真實問題" in encoded
    assert draft["mining"]["assistant_messages_excluded"] == 1
    assert draft["user_turns"][1]["replay_text"].startswith("新增資訊或限制：")


@pytest.mark.smoke
def test_suite_rejects_historical_model_answer_as_oracle():
    suite = _suite()
    suite["cases"][0]["verifier"]["expected_answer"] = "copy of old model prose"

    with pytest.raises(session_eval.SessionEvalError, match="not an oracle"):
        session_eval.validate_suite(suite)


@pytest.mark.smoke
def test_private_writer_refuses_symlink_target(tmp_path: Path):
    private = tmp_path / "private"
    private.mkdir()
    victim = tmp_path / "victim.json"
    victim.write_text("unchanged", encoding="utf-8")
    target = private / "drafts.json"
    target.symlink_to(victim)

    with pytest.raises(session_eval.SessionEvalError, match="non-regular"):
        session_eval.write_private_json(private, "drafts.json", {"secret": "NDA"})

    assert victim.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.smoke
def test_private_writer_uses_owner_only_modes(tmp_path: Path):
    private = tmp_path / "private"
    target = session_eval.write_private_json(private, "drafts.json", {"secret": "NDA"})

    assert stat_mode(private) == 0o700
    assert stat_mode(target) == 0o600


def stat_mode(path: Path) -> int:
    return os.stat(path, follow_symlinks=False).st_mode & 0o777










@pytest.mark.smoke
def test_blind_bundle_hides_candidate_identity():
    suite = _suite()
    left = _result("deepseek-secret", "b", "left answer")
    right = _result("glm-secret", "c", "right answer")

    bundle, key = session_eval.build_blind_bundle(
        suite,
        left,
        right,
        random_bytes=b"fixed-private-random-source-1234",
    )
    public = json.dumps(bundle, ensure_ascii=False)

    assert "deepseek-secret" not in public
    assert "glm-secret" not in public
    assert "llamacpp/" not in public
    assert {item["a"] for item in key["cases"]} | {item["b"] for item in key["cases"]} == {
        "deepseek-secret",
        "glm-secret",
    }


@pytest.mark.smoke
def test_opencode_timeout_is_a_scored_case_failure_not_a_suite_abort(
    monkeypatch,
    tmp_path: Path,
):
    partial_stream = "\n".join(
        [
            json.dumps(
                {
                    "type": "tool_use",
                    "sessionID": "ses_eval_timeout",
                    "part": {
                        "tool": "codetrail_read_file",
                        "state": {
                            "status": "completed",
                            "input": {"path": "private.c"},
                            "output": "PRIVATE TOOL OUTPUT MUST NOT BE SAVED",
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "type": "text",
                    "sessionID": "ses_eval_timeout",
                    "part": {"type": "text", "text": "partial answer"},
                }
            ),
        ]
    ).encode()

    def time_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["opencode", "run"],
            timeout=3,
            output=partial_stream,
            stderr=b"private stderr",
        )

    monkeypatch.setattr(session_eval_cli.subprocess, "run", time_out)

    turn, session_id = session_eval_cli._run_turn(
        root=tmp_path,
        prompt="question",
        model="llamacpp/candidate",
        env={},
        timeout=3,
        session_id="ses_eval_timeout",
    )

    assert session_id == "ses_eval_timeout"
    assert turn["timed_out"] is True
    assert turn["harness_error"] is True
    assert turn["terminal"] is False
    assert turn["assistant_text"] == "partial answer"
    assert "PRIVATE TOOL OUTPUT" not in json.dumps(turn)


@pytest.mark.smoke
def test_resume_checkpoint_rejects_project_state_drift(monkeypatch, tmp_path: Path):
    suite = _suite()
    suite["cases"][0]["project_root"] = str(tmp_path)
    second = json.loads(json.dumps(suite["cases"][0]))
    second["id"] = "second_case"
    second["source"]["session_hash"] = "fedcba9876543210"
    second["source"]["export_digest"] = "2" * 64
    suite["cases"].append(second)
    candidate = {
        "label": "candidate_one",
        "model": "llamacpp/candidate",
        "fingerprint": "b" * 64,
    }
    completed_case = {
        "case_id": "code_case",
        "project_state_digest": "a" * 64,
        "turns": [
            {
                "assistant_text": "answer",
                "terminal": True,
                "harness_error": False,
                "timed_out": False,
            }
        ],
        "automatic_pass": True,
        "cleanup_ok": True,
    }
    checkpoint = session_eval_cli._candidate_result_payload(
        suite,
        candidate,
        [completed_case],
    )

    assert checkpoint["aggregate"]["complete"] is False
    session_eval.validate_candidate_result(checkpoint)
    monkeypatch.setattr(session_eval_cli, "_validate_project_root", lambda _raw: tmp_path)
    monkeypatch.setattr(session_eval_cli, "project_state_digest", lambda _root, _paths: "c" * 64)

    with pytest.raises(session_eval.SessionEvalError, match="project state drifted"):
        session_eval_cli._resume_cases(suite, candidate, checkpoint)














# ── session eval 的 replay 契約(S4 審核回修)──
# 去 OpenCode 化之後 replay 跑的是我們自己的客戶端,所以壓縮語意由 client.json
# 決定、多輪要真的接得起來、而且 gitignore 掉的路徑一樣算 project state。

@pytest.mark.smoke
def test_the_replay_pins_its_own_compaction_mode(tmp_path):
    """replay 不讀使用者的 client.json。

    讀它等於同一份 suite 在兩台機器上量到不同東西;而一台從來沒設過
    client.json 的新部署會直接沒有壓縮,卻沒有任何欄位記得這件事。
    """
    off = session_eval_cli.replay_client_config(keep_compaction=False)
    kept = session_eval_cli.replay_client_config(keep_compaction=True)
    assert off["compaction_mode"] == "off"
    assert kept["compaction_mode"] == "codetrail"
    assert off["permission"] == {} and kept["permission"] == {}

    path = session_eval_cli._write_replay_client_config(tmp_path, kept)
    assert oct(path.stat().st_mode & 0o777) == "0o600"


@pytest.mark.smoke
def test_two_compaction_semantics_are_not_the_same_candidate():
    """兩個在這裡不同的 run 不可比,也不得共用 resume checkpoint。"""
    off = session_eval_cli._compaction_identity(keep_compaction=False, n_ctx=131072)
    kept = session_eval_cli._compaction_identity(keep_compaction=True, n_ctx=131072)
    assert off != kept
    assert kept["threshold"] > 0


@pytest.mark.smoke
def test_keep_compaction_needs_a_derivable_threshold():
    """推不出門檻會讓「有壓縮」靜默消失,而 compaction_events 照樣讀到 0。"""
    with pytest.raises(session_eval.SessionEvalError):
        session_eval_cli._compaction_identity(keep_compaction=True, n_ctx=None)
    with pytest.raises(session_eval.SessionEvalError):
        session_eval_cli._compaction_identity(keep_compaction=True, n_ctx=1024)


@pytest.mark.smoke
def test_a_multi_turn_case_actually_continues_the_conversation(monkeypatch, tmp_path):
    """每一輪各起一個 ephemeral 行程的話,模型完全看不到上一輪。

    「第二輪要引用第一輪的結論」這種 case 量到的會是一個不存在的能力。
    """
    commands: list[list[str]] = []

    def _fake_run(command, *, cwd, env, timeout):
        if command and command[0] == "git":
            return subprocess.CompletedProcess(command, 1, b"", b"")
        commands.append(list(command))
        payload = json.dumps(
            {"type": "step_finish", "sessionID": "20260101T000000-abcdef01",
             "part": {"type": "step-finish", "reason": "stop"}}
        )
        return subprocess.CompletedProcess(command, 0, payload.encode("utf-8"), b"")

    monkeypatch.setattr(session_eval_cli, "_bounded_run", _fake_run)
    monkeypatch.setattr(session_eval_cli, "_delete_generated_session", lambda *_a, **_k: True)
    root = tmp_path / "project"
    root.mkdir()

    session_eval_cli._run_case(
        {
            "id": "c1",
            "project_root": str(root),
            "task_type": "qa",
            "turns": [{"text": "第一輪"}, {"text": "第二輪"}],
            "verifier": {"oracle_kind": "manual", "checks": []},
        },
        model="llamacpp/m", env={}, timeout=30, keep_sessions=False,
    )
    assert len(commands) == 2
    assert "--persist" in commands[0]
    assert commands[1][commands[1].index("--session") + 1] == "20260101T000000-abcdef01"


@pytest.mark.smoke
def test_a_single_turn_case_never_writes_a_session_file(monkeypatch, tmp_path):
    """session 檔逐字含 NDA prompt 與工具輸出。單輪不需要落檔。"""
    commands: list[list[str]] = []

    def _fake_run(command, *, cwd, env, timeout):
        if command and command[0] == "git":
            return subprocess.CompletedProcess(command, 1, b"", b"")
        commands.append(list(command))
        payload = json.dumps(
            {"type": "step_finish", "sessionID": "20260101T000000-abcdef01",
             "part": {"type": "step-finish", "reason": "stop"}}
        )
        return subprocess.CompletedProcess(command, 0, payload.encode("utf-8"), b"")

    monkeypatch.setattr(session_eval_cli, "_bounded_run", _fake_run)
    root = tmp_path / "project"
    root.mkdir()
    session_eval_cli._run_case(
        {
            "id": "c1",
            "project_root": str(root),
            "task_type": "qa",
            "turns": [{"text": "只有一輪"}],
            "verifier": {"oracle_kind": "manual", "checks": []},
        },
        model="llamacpp/m", env={}, timeout=30, keep_sessions=False,
    )
    assert "--persist" not in commands[0]


@pytest.mark.smoke
def test_the_state_digest_covers_the_ignored_paths(tmp_path):
    """`.codetrail` / `knowledge.json` / `.aicode_uploads` 都在 .gitignore 裡。

    只靠 git diff 偵測的話,唯讀 replay 最可能寫到的那三個地方剛好全部漏掉。
    """
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text(
        ".codetrail/\nknowledge.json\n.aicode_uploads/\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=a@b", "-c", "user.name=x",
         "commit", "-qm", "init"],
        check=True,
    )
    paths = session_eval_cli._state_paths({})
    assert set(session_eval_cli.DEFAULT_STATE_PATHS) <= set(paths)

    before = session_eval_cli.project_state_digest(root, paths)
    (root / ".codetrail").mkdir()
    (root / ".codetrail" / "context_metrics.jsonl").write_text("{}\n", encoding="utf-8")
    assert session_eval_cli.project_state_digest(root, paths) != before


@pytest.mark.smoke
def test_the_store_export_uses_the_role_shape_the_validator_reads():
    """角色放在 `info.role`;頂層 `role` 會讓每一份匯出在第一則就被拒。"""
    export = session_eval.export_from_store(
        "20260101T000000-abcdef01",
        [
            {"type": "message", "role": "user", "content": "問題"},
            {"type": "message", "role": "assistant", "content": "答案"},
        ],
    )
    assert session_eval.validate_opencode_export(export) is export
    assert export["messages"][0]["info"]["role"] == "user"
    # raw export 是來源封存:助理回答留著(之後人工建 verifier 要用的證據)。
    assert export["messages"][1]["parts"] == [{"type": "text", "text": "答案"}]
    # 「歷史助理回答不得當 oracle」是挖掘層的契約:draft 只讀 user text。
    draft = session_eval.draft_from_export(export)
    assert "答案" not in json.dumps(draft, ensure_ascii=False)


@pytest.mark.smoke
def test_a_sanitized_export_strips_assistant_text_and_tool_output():
    """`--sanitize` 是給要分享出去的那一份:助理文字與工具輸出拿掉,工具名留著。"""
    records = [
        {"type": "message", "role": "user", "content": "問題"},
        {"type": "message", "role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}]},
        {"type": "message", "role": "tool", "tool_call_id": "c1", "name": "read_file",
         "content": "SECRET FILE BODY", "tool_status": "completed"},
        {"type": "message", "role": "assistant", "content": "答案 SECRET"},
    ]
    raw = session_eval.export_from_store("20260101T000000-abcdef01", records)
    clean = session_eval.export_from_store("20260101T000000-abcdef01", records, sanitized=True)
    assert "SECRET FILE BODY" in json.dumps(raw, ensure_ascii=False)
    assert "SECRET" not in json.dumps(clean, ensure_ascii=False)
    tools = [part["tool"] for message in clean["messages"] for part in message["parts"]
             if part.get("type") == "tool"]
    assert "read_file" in tools
    assert session_eval.validate_opencode_export(clean) is clean


# ── 總審第 1 輪回修:私人產物不得落進可追蹤路徑;例外路徑也要驗 project state ──

@pytest.mark.smoke
def test_private_eval_output_never_lands_in_a_tracked_repo_path(tmp_path):
    """candidate answer / prompt / 盲測 key 指到 `<repo>/eval/` 就會進下一次 commit。"""
    repo = session_eval_cli.REPO_ROOT
    with pytest.raises(session_eval.SessionEvalError, match="tracked path"):
        session_eval_cli._private_output_dir(repo / "eval" / "private-oops")
    with pytest.raises(session_eval.SessionEvalError, match="tracked path"):
        session_eval_cli._private_output_dir(repo)
    assert session_eval_cli._private_output_dir(repo / ".codetrail" / "session_eval" / "x")
    assert session_eval_cli._private_output_dir(tmp_path / "outside")


@pytest.mark.smoke
def test_the_state_digest_still_runs_when_the_replay_child_blows_up(monkeypatch, tmp_path):
    """replay child 改了現場之後丟例外:最後一道防線要照樣跑,而且要講的是現場變了。"""
    root = tmp_path / "project"
    root.mkdir()

    def _fake_run(command, *, cwd, env, timeout):
        if command and command[0] == "git":
            return subprocess.CompletedProcess(command, 1, b"", b"")
        (root / "knowledge.json").write_text("{}", encoding="utf-8")   # 改到現場
        raise RuntimeError("child exploded")

    monkeypatch.setattr(session_eval_cli, "_bounded_run", _fake_run)
    with pytest.raises(session_eval.SessionEvalError, match="project state changed"):
        session_eval_cli._run_case(
            {
                "id": "c1", "project_root": str(root), "task_type": "qa",
                "turns": [{"text": "q"}],
                "verifier": {"oracle_kind": "manual", "checks": []},
            },
            model="llamacpp/m", env={}, timeout=30, keep_sessions=False,
        )


# ── checkpoint / resume 的四個綁定(suite digest、model fingerprint、case 順序、完成標記)──

def _checkpoint_fixture(tmp_path: Path):
    suite = _suite()
    suite["cases"][0]["project_root"] = str(tmp_path)
    second = json.loads(json.dumps(suite["cases"][0]))
    second["id"] = "second_case"
    second["source"]["session_hash"] = "fedcba9876543210"
    second["source"]["export_digest"] = "2" * 64
    suite["cases"].append(second)
    candidate = {"label": "candidate_one", "model": "llamacpp/candidate", "fingerprint": "b" * 64}
    completed_case = {
        "case_id": "code_case",
        "project_state_digest": "a" * 64,
        "turns": [{"assistant_text": "answer", "terminal": True,
                   "harness_error": False, "timed_out": False}],
        "automatic_pass": True,
        "cleanup_ok": True,
    }
    checkpoint = session_eval_cli._candidate_result_payload(suite, candidate, [completed_case])
    session_eval.validate_candidate_result(checkpoint)
    return suite, candidate, checkpoint


@pytest.mark.smoke
def test_resume_checkpoint_rejects_a_different_suite(monkeypatch, tmp_path: Path):
    suite, candidate, checkpoint = _checkpoint_fixture(tmp_path)
    suite["cases"][1]["turns"][0]["text"] = "改過的題目"          # suite digest 變了
    with pytest.raises(session_eval.SessionEvalError, match="different suite"):
        session_eval_cli._resume_cases(suite, candidate, checkpoint)


@pytest.mark.smoke
def test_resume_checkpoint_rejects_a_different_live_model(monkeypatch, tmp_path: Path):
    suite, candidate, checkpoint = _checkpoint_fixture(tmp_path)
    live = dict(candidate, fingerprint="c" * 64)
    with pytest.raises(session_eval.SessionEvalError, match="fingerprint"):
        session_eval_cli._resume_cases(suite, live, checkpoint)


@pytest.mark.smoke
def test_resume_checkpoint_rejects_a_reordered_suite(monkeypatch, tmp_path: Path):
    suite, candidate, checkpoint = _checkpoint_fixture(tmp_path)
    # suite 本身沒變(digest 相同),但 checkpoint 完成的是第二題而不是第一題:
    # 已完成的集合不再是 suite 的有序前綴。
    checkpoint["cases"][0]["case_id"] = "second_case"
    with pytest.raises(session_eval.SessionEvalError, match="ordered suite prefix"):
        session_eval_cli._resume_cases(suite, candidate, checkpoint)


@pytest.mark.smoke
def test_resume_checkpoint_rejects_an_inconsistent_completion_marker(monkeypatch, tmp_path: Path):
    suite, candidate, checkpoint = _checkpoint_fixture(tmp_path)
    checkpoint["aggregate"]["complete"] = True                     # 兩題只跑了一題卻說完成
    with pytest.raises(session_eval.SessionEvalError, match="completion marker"):
        session_eval_cli._resume_cases(suite, candidate, checkpoint)


# ── 總審第 2 輪回修:raw export 是來源封存;private dir 每次都判 containment ──

@pytest.mark.smoke
def test_raw_export_keeps_the_original_conversation_across_a_compaction():
    """append-only 檔裡原始對話都還在;compaction 記錄只是送模型那一份的起點,
    遇到它不得把前段清掉(最後一筆是 compaction 時甚至會匯出空的)。"""
    records = [
        {"type": "message", "role": "user", "content": "第一問"},
        {"type": "message", "role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "grep_code", "arguments": '{"pattern": "NDA"}'}}]},
        {"type": "message", "role": "tool", "tool_call_id": "c1", "name": "grep_code",
         "content": "hit: secret.c:3", "tool_status": "completed"},
        {"type": "message", "role": "assistant", "content": "第一答"},
        {"type": "compaction", "history": [{"role": "user", "content": "摘要", "synthetic": True}]},
    ]
    raw = session_eval.export_from_store("20260101T000000-abcdef01", records)
    texts = json.dumps(raw, ensure_ascii=False)
    assert "第一問" in texts and "第一答" in texts and "hit: secret.c:3" in texts
    tool_parts = [p for m in raw["messages"] for p in m["parts"] if p.get("type") == "tool"]
    assert any(p.get("id") == "c1" and p.get("arguments") == '{"pattern": "NDA"}' for p in tool_parts)
    clean = session_eval.export_from_store("20260101T000000-abcdef01", records, sanitized=True)
    assert '"pattern"' not in json.dumps(clean, ensure_ascii=False)
    assert session_eval.validate_opencode_export(raw) is raw


@pytest.mark.smoke
def test_the_private_directory_guard_refuses_tracked_repo_paths_at_open_time(tmp_path):
    repo = session_eval._REPO_ROOT
    with pytest.raises(session_eval.SessionEvalError, match="not under"):
        session_eval._private_directory(repo / "eval" / "private-oops")
    with session_eval._private_directory(tmp_path / "outside") as private:
        assert private.path.is_dir()


# ── 總審第 3 輪回修(F3-9):routing eval 的 --model 要真的傳給逐題 client ──

@pytest.mark.smoke
def test_the_routing_client_attempt_sends_the_requested_model(monkeypatch, tmp_path):
    """15 題逐題跑的 client 以前沒帶 --model:跑的是呼叫環境 AICODE_MODEL 那顆(或啟動失敗),
    結果卻歸到指定模型的 identity。"""
    seen: dict = {}

    def _run(command, **kwargs):
        seen["command"] = list(command)
        seen["env"] = dict(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(routing.subprocess, "run", _run)
    routing._run_client_attempt(
        project=tmp_path, prompt="which tool?", model="llamacpp/expected-model",
        environment={"AICODE_MODEL": "different-model", "PATH": os.environ.get("PATH", "")},
        timeout_seconds=5,
    )
    command = seen["command"]
    # 送的是客戶端真正用的 bare name:直接執行的 codetrail_chat.py 不會剝 llamacpp/。
    assert command[command.index("--model") + 1] == "expected-model"
    assert command.index("--model") < command.index("run")
    assert seen["env"]["AICODE_MODEL"] == "expected-model"


@pytest.mark.smoke
def test_routing_probes_accept_the_same_model_forms_as_aicode():
    """`--model` 收 bare registry name / GGUF 路徑(同 `aicode -m`);舊式 llamacpp/ 仍接受,
    外部 provider 拒絕。以前 bare name 在連模型前就被 provider/model 語法檢查擋掉。"""
    for form in ("expected-model", "llamacpp/expected-model", "/models/foo.gguf"):
        assert routing._model_server_base_url({}, model=form, environment={}) == "http://localhost:8080"
    with pytest.raises(routing.EvalError):
        routing._model_server_base_url({}, model="openai/gpt-4o", environment={})


@pytest.mark.smoke
def test_session_eval_accepts_bare_gguf_and_legacy_models():
    """`--model` 依 help 傳 bare name 以前直接 IndexError;GGUF 絕對路徑被 split 砍成相對路徑。"""
    assert session_eval_cli.bare_model("qwen3-coder-30b") == "qwen3-coder-30b"
    assert session_eval_cli.bare_model("llamacpp/qwen3-coder-30b") == "qwen3-coder-30b"
    assert session_eval_cli.bare_model("/models/foo.gguf") == "/models/foo.gguf"
    with pytest.raises(session_eval.SessionEvalError):
        session_eval_cli.bare_model("openai/gpt-4o")


@pytest.mark.smoke
def test_every_direct_model_request_in_the_routing_eval_uses_the_normalised_model():
    """routing eval 實際打到 llama-server 的每個 request(base_url probe、catalog token probe、
    canary、逐題 client)都要用正規化後的 bare name;result identity 的 selected_model 才保留
    使用者原字串。以前 legacy `llamacpp/x` 只有逐題 client 正規化,直接 probe 的 payload 仍是原字串。"""
    import inspect

    source = inspect.getsource(routing)
    start = source.index("    probe_model = _bare_model(configured_model)")
    region = source[start:source.index("\ndef ", start)]
    assert "model=args.model," not in region
    assert region.count("model=configured_model,") == region.count("selected_model=configured_model,")
    assert region.count("model=probe_model,") >= 4        # base_url、token probe、canary、逐題 client
