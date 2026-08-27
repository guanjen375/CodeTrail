"""Purely synthetic contracts for the tool-routing evaluator.

These tests never start OpenCode, MCP subprocesses, a model server, or a
network client.  They exercise deterministic catalog counting, JSONL replay,
classification, support gating, and result privacy only.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from scripts import eval_tool_routing as routing
from scripts import mcp_catalog

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
        "tool_needed": {"recall": 0.9},
        "no_tool": {"precision": 0.9},
        "schema": {"valid_rate": 1.0},
        "grounding": {"adoption_rate": 0.9, "bait_assertions": 0},
        "failure_guards": {
            "promise_without_call": 0,
            "third_identical_call": 0,
            "marker_leak": 0,
            "empty_turn": 0,
            "fake_xml_counted_success": 0,
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
