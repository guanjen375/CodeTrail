#!/usr/bin/env python3
"""Deterministic CodeTrail tool-routing evaluation.

Catalog-only mode performs a live MCP ``initialize``/``tools/list`` capture and
never starts a model.  The model path uses a synthetic project embedded in the
fixture, accepts only completed structured client events, retries one missing
terminal event once, and persists only privacy-safe classifications and
aggregates.

Example::

    python3 scripts/eval_tool_routing.py --root ROOT --matrix-row ROW_ID \
      --arm baseline --output result.json --catalog-only

Running without ``--catalog-only`` is a real local-model evaluation and is
intentionally left to an explicitly authorised operator.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import client_events  # noqa: E402
import client_mcp  # noqa: E402
import client_policy  # noqa: E402
import client_prompt  # noqa: E402
import model_resolution  # noqa: E402
from scripts.mcp_catalog import (  # noqa: E402
    CatalogError,
    CatalogSnapshot,
    StdioMcpCommand,
    TokenMeasurementError,
    assert_public_tool_contract,
    catalog_from_fastmcp,
    catalog_from_stdio,
    json_digest,
    measure_catalog_prompt_tokens,
    text_digest,
)

DEFAULT_CASES_PATH = REPO_ROOT / "eval" / "fixtures" / "tool_routing" / "cases.json"
DEFAULT_MATRIX_PATH = REPO_ROOT / "eval" / "fixtures" / "tool_routing" / "support_matrix.json"
RESULT_SCHEMA_VERSION = 2
FIXTURE_SCHEMA_VERSION = 1
MATRIX_SCHEMA_VERSION = 2
DEFAULT_MCP_TIMEOUT_SECONDS = 120
DEFAULT_MODEL_TIMEOUT_SECONDS = 180
MAX_HTTP_RESPONSE_BYTES = 4 * 1024 * 1024
MODEL_SERVER_PREFLIGHT_SKIP_ENV = "AICODE_REQUIRED_MODELS_CHECK_SKIP"
REQUIRED_BILINGUAL_CATEGORIES = frozenset(
    {
        "directory",
        "exact_grep",
        "read",
        "cross_file",
        "spec",
        "missing_data_refusal",
        "no_tool",
    }
)

MARKER_LEAK_TOKENS = ("<｜DSML｜>", "<tool_call>", "<|")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_PROMISE_RE = re.compile(
    r"(?:\b(?:i(?:'ll|\s+will|\s+am\s+going\s+to)|let\s+me)\b"
    r".{0,48}\b(?:call|invoke|use|search|grep|read|inspect|check)\b)"
    r"|(?:(?:我(?:將|會|要)|讓我|接下來).{0,24}(?:呼叫|使用|查詢|搜尋|讀取|檢查))",
    re.IGNORECASE | re.DOTALL,
)


class EvalError(RuntimeError):
    """The harness or fixture is invalid; messages must not include model data."""


class Classification(str, Enum):
    TOOL_SUCCESS = "tool_success"
    WRONG_TOOL = "wrong_tool"
    INVALID_ARGS = "invalid_args"
    PROMISE_WITHOUT_CALL = "promise_without_call"
    EMPTY_TURN = "empty_turn"
    MARKER_LEAK = "marker_leak"
    HARNESS_INVALID = "harness_invalid"


@dataclass(frozen=True)
class CompletedToolCall:
    tool: str
    arguments: dict[str, Any] | None

    @property
    def bare_tool(self) -> str:
        return self.tool.removeprefix("codetrail_")

    @property
    def identity(self) -> str:
        return json.dumps(
            [self.bare_tool, self.arguments],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class AttemptTrace:
    """In-memory event evidence.  Text, args and sessions are never serialised."""

    completed_calls: tuple[CompletedToolCall, ...]
    assistant_text: str
    terminal: bool
    marker_leak: bool
    prompt_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_tokens: int
    total_tokens: int
    compaction_events: int
    latency_ms: int
    session_ids: tuple[str, ...] = ()
    harness_error: bool = False


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    classification: Classification
    prompt_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_tokens: int
    total_tokens: int
    latency_ms: int
    compaction_events: int
    terminal_retry: bool
    tool_needed: bool
    schema_calls: int
    schema_valid_calls: int
    grounding_required: bool
    grounded: bool
    bait_assertion: bool
    third_identical_call: bool

    def private_record(self) -> dict[str, Any]:
        """Return the only per-case shape allowed in a result JSON."""

        return {
            "case_id": self.case_id,
            "classification": self.classification.value,
            "tokens": {
                "prompt": self.prompt_tokens,
                "output": self.output_tokens,
                "reasoning": self.reasoning_tokens,
                "cache": self.cache_tokens,
                "total": self.total_tokens,
            },
            "latency": {"milliseconds": self.latency_ms},
            "compaction": {
                "events": self.compaction_events,
                "terminal_retry": self.terminal_retry,
            },
        }


@dataclass(frozen=True)
class GateVerdict:
    passed: bool
    checks: dict[str, bool]

    def private_record(self, *, current_status: str) -> dict[str, Any]:
        # Passing computes eligibility only.  The matrix is never mutated and a
        # measured row never silently becomes supported.
        return {
            "passed": self.passed,
            "checks": dict(sorted(self.checks.items())),
            "matrix_status": current_status,
            "manual_status_change_required": self.passed and current_status != "supported",
        }


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvalError(f"{label} fixture is missing") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvalError(f"{label} fixture is unreadable or invalid JSON") from exc
    if not isinstance(value, dict):
        raise EvalError(f"{label} fixture root must be a JSON object")
    return value


def _safe_fixture_path(raw: object) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise EvalError("fixture file path must be a non-empty POSIX relative path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise EvalError("fixture file path must stay inside the synthetic project")
    return path


def _validate_case(case: object) -> dict[str, Any]:
    if not isinstance(case, dict):
        raise EvalError("each routing case must be a JSON object")
    case_id = case.get("id")
    if not isinstance(case_id, str) or not _SAFE_ID_RE.fullmatch(case_id):
        raise EvalError("routing case id is missing or unsafe")
    if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
        raise EvalError(f"case {case_id} has no prompt")
    if case.get("language") not in ("en", "zh", "zh-en"):
        raise EvalError(f"case {case_id} has an unsupported language")
    if not isinstance(case.get("category"), str) or not case["category"]:
        raise EvalError(f"case {case_id} has no category")
    expected = case.get("expected")
    if not isinstance(expected, dict):
        raise EvalError(f"case {case_id} has no expected contract")
    expected_tools = expected.get("tools")
    if not isinstance(expected_tools, list) or not all(
        isinstance(tool, str) and tool and not tool.startswith("codetrail_") for tool in expected_tools
    ):
        raise EvalError(f"case {case_id} expected.tools must contain bare MCP names")
    allowed_tools = expected.get("allowed_tools", expected_tools)
    if not isinstance(allowed_tools, list) or not all(
        isinstance(tool, str) and tool and not tool.startswith("codetrail_") for tool in allowed_tools
    ):
        raise EvalError(f"case {case_id} expected.allowed_tools must contain bare MCP names")
    if not set(expected_tools).issubset(allowed_tools):
        raise EvalError(f"case {case_id} expected tools must be allowed")
    arg_rules = expected.get("arg_rules", {})
    if not isinstance(arg_rules, dict) or not all(
        isinstance(tool, str) and isinstance(rules, dict) for tool, rules in arg_rules.items()
    ):
        raise EvalError(f"case {case_id} expected.arg_rules must be an object")
    for key in ("required_markers", "forbidden_markers"):
        values = expected.get(key, [])
        if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
            raise EvalError(f"case {case_id} expected.{key} must be a string list")
    return case


def load_cases(path: Path = DEFAULT_CASES_PATH) -> dict[str, Any]:
    data = _load_json_object(path, label="routing cases")
    if data.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        raise EvalError("unsupported routing cases schema")
    fixture = data.get("fixture")
    if not isinstance(fixture, dict) or not isinstance(fixture.get("files"), list):
        raise EvalError("routing fixture must contain a synthetic file list")
    seen_paths: set[PurePosixPath] = set()
    for item in fixture["files"]:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise EvalError("each synthetic fixture file needs path and string content")
        path_value = _safe_fixture_path(item.get("path"))
        if path_value in seen_paths:
            raise EvalError("synthetic fixture contains a duplicate path")
        seen_paths.add(path_value)
    root_canary = _safe_fixture_path(fixture.get("root_canary"))
    if len(root_canary.parts) != 1 or root_canary not in seen_paths:
        raise EvalError("fixture.root_canary must name a synthetic top-level file")
    documents = fixture.get("knowledge_documents", [])
    if not isinstance(documents, list):
        raise EvalError("fixture.knowledge_documents must be a list")
    for raw in documents:
        if _safe_fixture_path(raw) not in seen_paths:
            raise EvalError("knowledge document is absent from fixture.files")

    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise EvalError("routing fixture has zero cases")
    validated = [_validate_case(case) for case in cases]
    ids = [case["id"] for case in validated]
    if len(ids) != len(set(ids)):
        raise EvalError("routing fixture contains duplicate case ids")
    pairs = {(case["category"], case["language"]) for case in validated}
    for category in REQUIRED_BILINGUAL_CATEGORIES:
        if (category, "en") not in pairs or (category, "zh") not in pairs:
            raise EvalError("routing fixture lacks required bilingual intent coverage")
    mixed_cases = [case for case in validated if case["language"] == "zh-en"]
    if not any(_mixed_case_has_ascii_coderag_contract(case) for case in mixed_cases):
        raise EvalError("routing fixture lacks the zh-en CodeRAG ASCII query contract")
    return data


def _mixed_case_has_ascii_coderag_contract(case: Mapping[str, Any]) -> bool:
    expected = case.get("expected")
    if not isinstance(expected, Mapping) or "code_rag_search" not in expected.get("tools", []):
        return False
    arg_rules = expected.get("arg_rules")
    code_rules = arg_rules.get("code_rag_search") if isinstance(arg_rules, Mapping) else None
    query_rule = code_rules.get("query") if isinstance(code_rules, Mapping) else None
    ratio = query_rule.get("ascii_ratio_min") if isinstance(query_rule, Mapping) else None
    return isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and ratio >= 0.9


def load_support_matrix(path: Path = DEFAULT_MATRIX_PATH) -> dict[str, Any]:
    data = _load_json_object(path, label="support matrix")
    if data.get("schema_version") != MATRIX_SCHEMA_VERSION:
        raise EvalError("unsupported support matrix schema")
    if not isinstance(data.get("arms"), dict) or not data["arms"]:
        raise EvalError("support matrix has no arms")
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        raise EvalError("support matrix has no rows")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise EvalError("support matrix row must be an object")
        row_id = row.get("id")
        if not isinstance(row_id, str) or not _SAFE_ID_RE.fullmatch(row_id):
            raise EvalError("support matrix row id is missing or unsafe")
        if row_id in seen:
            raise EvalError("support matrix contains duplicate row ids")
        seen.add(row_id)
        if row.get("status") not in ("measured", "supported", "unsupported"):
            raise EvalError(f"matrix row {row_id} has an invalid status")
        if not isinstance(row.get("compatibility"), dict):
            raise EvalError(f"matrix row {row_id} has no compatibility identity")
        # Every row owns its comparison baseline.  There is intentionally no
        # global baseline fallback that could make one model impersonate another.
        if not isinstance(row.get("baseline"), dict):
            raise EvalError(f"matrix row {row_id} has no row-local baseline")
        if row.get("status") == "supported" and not isinstance(row["baseline"].get("routing"), dict):
            raise EvalError(f"supported matrix row {row_id} lacks routing baseline metrics")
    return data


def select_matrix_row(matrix: Mapping[str, Any], row_id: str, arm: str) -> dict[str, Any]:
    arms = matrix.get("arms")
    if not isinstance(arms, Mapping) or arm not in arms:
        raise EvalError("requested arm is not declared in the support matrix")
    rows = matrix.get("rows")
    if not isinstance(rows, list):
        raise EvalError("support matrix rows are invalid")
    for row in rows:
        if isinstance(row, dict) and row.get("id") == row_id:
            enabled_arms = row.get("arms")
            if not isinstance(enabled_arms, list) or arm not in enabled_arms:
                raise EvalError("requested arm is not enabled for this matrix row")
            return row
    raise EvalError("requested matrix row does not exist")


_FROZEN_CATALOG_FIELDS = (
    "schema_json_rule",
    "tool_count",
    "description_chars",
    "input_schema_chars",
    "output_schema_chars",
    "catalog_chars",
    "opencode_effective_chars",
    "instructions_chars",
    "tools_digest",
    "instructions_digest",
    "canonical_tools_list_chars",
    "tool_order",
    "per_tool",
)


def assert_frozen_catalog_contract(snapshot: CatalogSnapshot, row: Mapping[str, Any]) -> None:
    """Accept a historical server only when every frozen catalog field matches."""

    baseline = row.get("baseline")
    expected = baseline.get("catalog") if isinstance(baseline, Mapping) else None
    if not isinstance(expected, Mapping) or any(field not in expected for field in _FROZEN_CATALOG_FIELDS):
        raise EvalError("matrix row lacks a complete frozen catalog contract")
    actual = snapshot.summary(include_per_tool=True)
    actual["tool_order"] = list(snapshot.tool_names)
    if any(actual[field] != expected[field] for field in _FROZEN_CATALOG_FIELDS):
        raise EvalError("live catalog differs from the frozen matrix baseline contract")


def materialize_synthetic_fixture(data: Mapping[str, Any], destination: Path) -> None:
    """Create the embedded synthetic project in a new, empty directory."""

    fixture = data.get("fixture")
    files = fixture.get("files") if isinstance(fixture, Mapping) else None
    if not isinstance(files, list):
        raise EvalError("fixture file list is invalid")
    destination.mkdir(parents=True, exist_ok=False)
    for item in files:
        if not isinstance(item, Mapping) or not isinstance(item.get("content"), str):
            raise EvalError("fixture file entry is invalid")
        relative = _safe_fixture_path(item.get("path"))
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["content"], encoding="utf-8")


# 事件流的形狀與解析集中在 client_events:canary、routing eval 與 session_eval
# replay 走同一份。各自寫一份解析器的代價是靜默的——某一端多認一種形狀時,
# 同一次 run 在兩邊會得到不同判定,而兩邊都不會報錯。
_event_part = client_events.event_part
_safe_nonnegative_int = client_events.safe_nonnegative_int
_event_session_ids = client_events.event_session_ids


_event_generated_text = client_events.event_generated_text


def _completed_tool_call(event: Mapping[str, Any]) -> CompletedToolCall | None:
    call = client_events.completed_tool_call(event)
    if call is None:
        return None
    return CompletedToolCall(call.tool, call.arguments)


_is_terminal_event = client_events.is_terminal_event


_token_values = client_events.token_values


def parse_event_stream(
    output: str,
    *,
    latency_ms: int = 0,
    harness_error: bool = False,
) -> AttemptTrace:
    """Deterministically parse the client's JSONL event stream without retaining raw events."""

    completed: list[CompletedToolCall] = []
    generated: list[str] = []
    sessions: list[str] = []
    terminal = False
    prompt_tokens = output_tokens = reasoning_tokens = cache_tokens = total_tokens = 0
    compactions = 0

    for event in client_events.iter_events(output):
        for session_id in _event_session_ids(event):
            if session_id not in sessions:
                sessions.append(session_id)
        call = _completed_tool_call(event)
        if call is not None:
            completed.append(call)
        text = _event_generated_text(event)
        if text:
            generated.append(text)
        terminal = terminal or _is_terminal_event(event)
        event_type = str(event.get("type", "")).lower()
        part_type = str(_event_part(event).get("type", "")).lower()
        if "compact" in event_type or "compact" in part_type:
            compactions += 1
        prompt, emitted, reasoning, cache, total = _token_values(event)
        prompt_tokens += prompt
        output_tokens += emitted
        reasoning_tokens += reasoning
        cache_tokens += cache
        total_tokens += total

    assistant_text = "\n".join(generated)
    return AttemptTrace(
        completed_calls=tuple(completed),
        assistant_text=assistant_text,
        terminal=terminal,
        marker_leak=any(marker in assistant_text for marker in MARKER_LEAK_TOKENS),
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cache_tokens=cache_tokens,
        total_tokens=total_tokens,
        compaction_events=compactions,
        latency_ms=max(0, latency_ms),
        session_ids=tuple(sessions),
        harness_error=harness_error,
    )


def ascii_ratio(value: str) -> float:
    units = [char for char in value if not char.isspace()]
    if not units:
        return 0.0
    return sum(ord(char) < 128 for char in units) / len(units)


def validate_json_schema(instance: object, schema: Mapping[str, Any]) -> bool:
    """Small deterministic validator for the JSON-Schema subset in tools/list."""

    if "allOf" in schema:
        values = schema["allOf"]
        return isinstance(values, list) and all(
            isinstance(item, Mapping) and validate_json_schema(instance, item) for item in values
        )
    if "anyOf" in schema:
        values = schema["anyOf"]
        return isinstance(values, list) and any(
            isinstance(item, Mapping) and validate_json_schema(instance, item) for item in values
        )
    if "oneOf" in schema:
        values = schema["oneOf"]
        return (
            isinstance(values, list)
            and sum(isinstance(item, Mapping) and validate_json_schema(instance, item) for item in values)
            == 1
        )
    if "const" in schema and instance != schema["const"]:
        return False
    enum = schema.get("enum")
    if isinstance(enum, list) and instance not in enum:
        return False

    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        return any(validate_json_schema(instance, {**schema, "type": item}) for item in expected_type)
    if expected_type == "null":
        return instance is None
    if expected_type == "boolean":
        return isinstance(instance, bool)
    if expected_type == "integer":
        if not isinstance(instance, int) or isinstance(instance, bool):
            return False
    elif expected_type == "number":
        if not isinstance(instance, (int, float)) or isinstance(instance, bool):
            return False
    elif expected_type == "string":
        if not isinstance(instance, str):
            return False
        if isinstance(schema.get("minLength"), int) and len(instance) < schema["minLength"]:
            return False
        if isinstance(schema.get("maxLength"), int) and len(instance) > schema["maxLength"]:
            return False
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                if re.search(pattern, instance) is None:
                    return False
            except re.error:
                return False
    elif expected_type == "array":
        if not isinstance(instance, list):
            return False
        items = schema.get("items")
        if isinstance(items, Mapping) and not all(validate_json_schema(item, items) for item in instance):
            return False
    elif expected_type == "object" or "properties" in schema or "required" in schema:
        if not isinstance(instance, Mapping):
            return False
        required = schema.get("required", [])
        if isinstance(required, list) and any(key not in instance for key in required):
            return False
        properties = schema.get("properties", {})
        if isinstance(properties, Mapping):
            for key, value in instance.items():
                child = properties.get(key)
                if isinstance(child, Mapping) and not validate_json_schema(value, child):
                    return False
                if key not in properties and schema.get("additionalProperties") is False:
                    return False

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        exclusive_min = schema.get("exclusiveMinimum")
        exclusive_max = schema.get("exclusiveMaximum")
        if isinstance(minimum, (int, float)) and instance < minimum:
            return False
        if isinstance(maximum, (int, float)) and instance > maximum:
            return False
        if isinstance(exclusive_min, (int, float)) and instance <= exclusive_min:
            return False
        if isinstance(exclusive_max, (int, float)) and instance >= exclusive_max:
            return False
    return True


def _matches_arg_rule(value: object, rule: Mapping[str, Any]) -> bool:
    if "equals" in rule and value != rule["equals"]:
        return False
    one_of = rule.get("one_of")
    if isinstance(one_of, list) and value not in one_of:
        return False
    contains = rule.get("contains")
    if isinstance(contains, str) and (not isinstance(value, str) or contains not in value):
        return False
    ratio = rule.get("ascii_ratio_min")
    if isinstance(ratio, (int, float)) and (not isinstance(value, str) or ascii_ratio(value) < float(ratio)):
        return False
    minimum = rule.get("minimum")
    maximum = rule.get("maximum")
    if isinstance(minimum, (int, float)) and (
        not isinstance(value, (int, float)) or isinstance(value, bool) or value < minimum
    ):
        return False
    if isinstance(maximum, (int, float)) and (
        not isinstance(value, (int, float)) or isinstance(value, bool) or value > maximum
    ):
        return False
    return True


def _call_schema_valid(
    call: CompletedToolCall,
    *,
    schemas: Mapping[str, Mapping[str, Any]],
) -> bool:
    arguments = call.arguments
    schema = schemas.get(call.bare_tool)
    if arguments is None or not isinstance(schema, Mapping):
        return False
    return validate_json_schema(arguments, schema)


def _call_args_valid(
    call: CompletedToolCall,
    *,
    schemas: Mapping[str, Mapping[str, Any]],
    arg_rules: Mapping[str, Any],
) -> bool:
    arguments = call.arguments
    if arguments is None or not _call_schema_valid(call, schemas=schemas):
        return False
    tool_rules = arg_rules.get(call.bare_tool, {})
    if not isinstance(tool_rules, Mapping):
        return False
    for name, rule in tool_rules.items():
        if not isinstance(name, str) or not isinstance(rule, Mapping) or name not in arguments:
            return False
        if not _matches_arg_rule(arguments[name], rule):
            return False
    return True


def _case_expected(case: Mapping[str, Any]) -> Mapping[str, Any]:
    expected = case.get("expected")
    if not isinstance(expected, Mapping):
        raise EvalError("case expected contract is invalid")
    return expected


def classify_attempt(
    case: Mapping[str, Any],
    trace: AttemptTrace,
    *,
    schemas: Mapping[str, Mapping[str, Any]],
    terminal_retry: bool = False,
    accumulated: Sequence[AttemptTrace] | None = None,
) -> CaseOutcome:
    """Classify one terminal trace using completed structured calls only."""

    case_id = case.get("id")
    if not isinstance(case_id, str):
        raise EvalError("case has no valid id")
    expected = _case_expected(case)
    expected_tools = tuple(expected.get("tools", ()))
    allowed_tools = set(expected.get("allowed_tools", expected_tools))
    arg_rules = expected.get("arg_rules", {})
    if not isinstance(arg_rules, Mapping):
        raise EvalError(f"case {case_id} arg_rules is invalid")
    required_markers = tuple(expected.get("required_markers", ()))
    forbidden_markers = tuple(expected.get("forbidden_markers", ()))

    traces = tuple(accumulated or (trace,))
    prompt_tokens = sum(item.prompt_tokens for item in traces)
    output_tokens = sum(item.output_tokens for item in traces)
    reasoning_tokens = sum(item.reasoning_tokens for item in traces)
    cache_tokens = sum(item.cache_tokens for item in traces)
    total_tokens = sum(item.total_tokens for item in traces)
    latency_ms = sum(item.latency_ms for item in traces)
    compaction_events = sum(item.compaction_events for item in traces)

    calls = trace.completed_calls
    counts = Counter(call.identity for call in calls)
    third_identical = any(count >= 3 for count in counts.values())
    marker_leak = trace.marker_leak
    forbidden = any(marker in trace.assistant_text for marker in forbidden_markers)
    grounded = all(marker in trace.assistant_text for marker in required_markers)
    schema_valid = tuple(_call_schema_valid(call, schemas=schemas) for call in calls)
    case_args_valid = tuple(_call_args_valid(call, schemas=schemas, arg_rules=arg_rules) for call in calls)

    if trace.harness_error or not trace.terminal:
        classification = Classification.HARNESS_INVALID
    elif marker_leak:
        classification = Classification.MARKER_LEAK
    elif third_identical:
        classification = Classification.WRONG_TOOL
    elif not calls:
        if not trace.assistant_text.strip():
            classification = Classification.EMPTY_TURN
        elif _PROMISE_RE.search(trace.assistant_text):
            classification = Classification.PROMISE_WITHOUT_CALL
        elif not expected_tools and not forbidden and grounded:
            classification = Classification.TOOL_SUCCESS
        else:
            classification = Classification.WRONG_TOOL
    elif not expected_tools:
        classification = Classification.WRONG_TOOL
    elif any(call.bare_tool not in allowed_tools for call in calls):
        classification = Classification.WRONG_TOOL
    elif any(tool not in {call.bare_tool for call in calls} for tool in expected_tools):
        classification = Classification.WRONG_TOOL
    elif not all(case_args_valid):
        classification = Classification.INVALID_ARGS
    elif not trace.assistant_text.strip():
        classification = Classification.EMPTY_TURN
    elif forbidden or not grounded:
        classification = Classification.WRONG_TOOL
    else:
        classification = Classification.TOOL_SUCCESS

    return CaseOutcome(
        case_id=case_id,
        classification=classification,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cache_tokens=cache_tokens,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
        compaction_events=compaction_events,
        terminal_retry=terminal_retry,
        tool_needed=bool(expected_tools),
        schema_calls=len(calls),
        schema_valid_calls=sum(schema_valid),
        grounding_required=bool(required_markers),
        grounded=grounded,
        bait_assertion=forbidden,
        third_identical_call=third_identical,
    )


def evaluate_attempts(
    case: Mapping[str, Any],
    attempts: Sequence[AttemptTrace],
    *,
    schemas: Mapping[str, Mapping[str, Any]],
) -> CaseOutcome:
    """Replay at most two attempts; retry only a missing terminal event."""

    if not attempts or len(attempts) > 2:
        raise EvalError("terminal replay needs one or two attempts")
    first = attempts[0]
    if first.terminal:
        return classify_attempt(case, first, schemas=schemas)
    if len(attempts) == 1:
        return classify_attempt(case, first, schemas=schemas)
    second = attempts[1]
    return classify_attempt(
        case,
        second,
        schemas=schemas,
        terminal_retry=True,
        accumulated=(first, second),
    )


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def aggregate_outcomes(outcomes: Sequence[CaseOutcome]) -> dict[str, Any]:
    classifications = Counter(item.classification.value for item in outcomes)
    valid = [item for item in outcomes if item.classification is not Classification.HARNESS_INVALID]
    tool_needed = [item for item in valid if item.tool_needed]
    no_tool = [item for item in valid if not item.tool_needed]
    grounded = [item for item in valid if item.grounding_required]
    schema_calls = sum(item.schema_calls for item in valid)
    schema_valid = sum(item.schema_valid_calls for item in valid)

    def tool_success(item: CaseOutcome) -> bool:
        return item.classification is Classification.TOOL_SUCCESS

    return {
        "case_count": len(outcomes),
        "model_denominator": len(valid),
        "harness_invalid_count": classifications[Classification.HARNESS_INVALID.value],
        "classifications": {
            classification.value: classifications[classification.value] for classification in Classification
        },
        "tool_needed": {
            "count": len(tool_needed),
            "successes": sum(tool_success(item) for item in tool_needed),
            "recall": _rate(sum(tool_success(item) for item in tool_needed), len(tool_needed)),
        },
        "no_tool": {
            "count": len(no_tool),
            "successes": sum(tool_success(item) for item in no_tool),
            "precision": _rate(sum(tool_success(item) for item in no_tool), len(no_tool)),
        },
        "schema": {
            "completed_calls": schema_calls,
            "valid_calls": schema_valid,
            "valid_rate": _rate(schema_valid, schema_calls),
        },
        "grounding": {
            "count": len(grounded),
            "adopted": sum(item.grounded and not item.bait_assertion for item in grounded),
            "adoption_rate": _rate(
                sum(item.grounded and not item.bait_assertion for item in grounded),
                len(grounded),
            ),
            "bait_assertions": sum(item.bait_assertion for item in valid),
        },
        "failure_guards": {
            "promise_without_call": classifications[Classification.PROMISE_WITHOUT_CALL.value],
            "third_identical_call": sum(item.third_identical_call for item in valid),
            "marker_leak": classifications[Classification.MARKER_LEAK.value],
            "empty_turn": classifications[Classification.EMPTY_TURN.value],
            "fake_xml_counted_success": 0,
            # **直接**數「該呼叫工具、卻一個 structured call 都沒有」的次數。
            #   * 用直接計數而不是把 promise_without_call / empty_turn /
            #     marker_leak 三個分類相加：只要有一種「沒發呼叫」的情況被歸到
            #     別的分類（wrong_tool / invalid_args 的無呼叫變體），代理值就
            #     漏算，成功率虛報成 1.0 而通過 100% 門檻。
            #   * 但**必須限定在 `tool_needed`**：`expected_tools=[]` 的案例
            #     本來就不該呼叫工具，它們必然 `schema_calls == 0`。把它們算進來
            #     的話，一次完美路由也達不到 1.0 —— 門檻直接變成不可達。
            "no_structured_call": sum(
                item.schema_calls == 0 for item in valid if item.tool_needed
            ),
        },
        "tokens": {
            "prompt": sum(item.prompt_tokens for item in outcomes),
            "output": sum(item.output_tokens for item in outcomes),
            "reasoning": sum(item.reasoning_tokens for item in outcomes),
            "cache": sum(item.cache_tokens for item in outcomes),
            "total": sum(item.total_tokens for item in outcomes),
        },
        "latency": {
            "total_milliseconds": sum(item.latency_ms for item in outcomes),
            "mean_milliseconds": round(sum(item.latency_ms for item in outcomes) / len(outcomes))
            if outcomes
            else None,
        },
        "compaction": {
            "events": sum(item.compaction_events for item in outcomes),
            "terminal_retries": sum(item.terminal_retry for item in outcomes),
        },
    }


def _nested_number(data: Mapping[str, Any], *keys: str) -> float | None:
    value: object = data
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def structured_call_success_rate(aggregate: Mapping[str, Any]) -> float | None:
    """Share of measured turns decided by structured events, not by model text.

    Release gating must never rest on a model claiming "I called the tool".
    This reuses what ``Classification`` / ``aggregate_outcomes`` already count —
    no second statistic is collected:

    * ``schema.valid_rate``: every structured call that did arrive validated
      against its own tool schema.
    * ``failure_guards.no_structured_call``: a **direct** count of turns where a
      tool was needed and no structured call reached the server at all.  Summing
      the ``promise_without_call`` / ``empty_turn`` / ``marker_leak``
      classifications was a proxy: any no-call turn filed under another label
      dropped out of it and the rate silently read 1.0.  The count is scoped to
      ``tool_needed`` turns -- correct no-tool cases have no call by design, and
      counting them would put the 1.0 threshold out of reach.

    The rate is the product of both, so a run only scores 1.0 when every
    measured turn was settled by structured evidence.  ``None`` means the run
    cannot be judged -- no schema measurement, or a missing/zero/negative
    ``tool_needed.count`` -- and the gate treats that as a failure, never as a
    pass.  An absent denominator must not silently degrade to
    ``schema.valid_rate``: that number says how many structured calls that did
    arrive were well-formed, and it stays at 1.0 for a run where no call ever
    arrived.  Releasing on it would be releasing on an unmeasured aggregate.
    """

    schema_rate = _nested_number(aggregate, "schema", "valid_rate")
    if schema_rate is None:
        return None
    guards = aggregate.get("failure_guards")
    guards = guards if isinstance(guards, Mapping) else {}
    # 直接用「完全沒有 structured call 的輪數」，不要再把幾個分類名相加當代理值:
    # 分類法一改（或某個「沒發呼叫」的情況被歸到 wrong_tool / invalid_args），
    # 那個代理值就會漏算，成功率虛報成 1.0 而通過 100% 門檻。
    text_only = guards.get("no_structured_call")
    if not isinstance(text_only, int) or isinstance(text_only, bool) or text_only < 0:
        return None
    # 分母要跟分子同一個母體:分子只數「該呼叫工具卻沒呼叫」的輪次,分母就得是
    # 「該呼叫工具」的輪次數。**不接受 `model_denominator` 當退路** —— 那是全部
    # valid 案例,母體不同會把 share 稀釋掉。量不到就是量不到,回 None 讓 gate 擋下。
    denominator = _nested_number(aggregate, "tool_needed", "count")
    if denominator is None or denominator <= 0:
        return None
    share = min(text_only / denominator, 1.0)
    return round(schema_rate * (1.0 - share), 6)


def evaluate_support_gate(
    *,
    aggregate: Mapping[str, Any],
    row: Mapping[str, Any],
    thresholds: Mapping[str, Any],
    catalog_prompt_tokens: int | None = None,
    explicit_canary_rate: float | None = None,
    ask_permission_preserved: bool | None = None,
) -> GateVerdict:
    """Apply all gates against this row's own baseline without changing status."""

    baseline = row.get("baseline")
    if not isinstance(baseline, Mapping):
        raise EvalError("matrix row has no row-local baseline")
    baseline_metrics = baseline.get("routing")
    baseline_metrics = baseline_metrics if isinstance(baseline_metrics, Mapping) else {}

    recall = _nested_number(aggregate, "tool_needed", "recall")
    precision = _nested_number(aggregate, "no_tool", "precision")
    schema_rate = _nested_number(aggregate, "schema", "valid_rate")
    grounding_rate = _nested_number(aggregate, "grounding", "adoption_rate")
    baseline_recall = _nested_number(baseline_metrics, "tool_needed", "recall")
    baseline_precision = _nested_number(baseline_metrics, "no_tool", "precision")
    baseline_grounding = _nested_number(baseline_metrics, "grounding", "adoption_rate")
    baseline_catalog_tokens = _nested_number(baseline, "catalog", "catalog_prompt_tokens")

    recall_min = float(thresholds.get("tool_needed_recall_min", 0.9))
    precision_min = float(thresholds.get("no_tool_precision_min", 0.8))
    grounding_min = float(thresholds.get("evidence_adoption_min", 0.9))
    improvement = float(thresholds.get("low_baseline_recall_improvement", 0.1))
    catalog_ratio = float(thresholds.get("catalog_token_ratio_max", 0.6))
    structured_min = float(thresholds.get("structured_call_success_min", 1.0))

    failure_guards = aggregate.get("failure_guards")
    failure_guards = failure_guards if isinstance(failure_guards, Mapping) else {}
    grounding = aggregate.get("grounding")
    grounding = grounding if isinstance(grounding, Mapping) else {}

    structured_rate = structured_call_success_rate(aggregate)

    checks = {
        "harness_valid_all_cases": aggregate.get("harness_invalid_count") == 0,
        "explicit_canary_100_percent": explicit_canary_rate == 1.0,
        # Release eligibility = every explicit canary attempt passed AND the
        # structured-call success rate met its floor.  A model asserting that it
        # used a tool is not evidence and never moves this check.
        "structured_call_success": (
            structured_rate is not None and structured_rate >= structured_min
        ),
        "tool_needed_recall": recall is not None and recall >= recall_min,
        "schema_valid_100_percent": schema_rate == 1.0,
        "no_tool_precision": (
            precision is not None
            and precision >= precision_min
            and (baseline_precision is None or precision >= baseline_precision)
        ),
        "evidence_adoption": (
            grounding_rate is not None
            and grounding_rate >= grounding_min
            and (baseline_grounding is None or grounding_rate >= baseline_grounding)
        ),
        "no_bait_assertion": grounding.get("bait_assertions") == 0,
        "no_promise_without_call": failure_guards.get("promise_without_call") == 0,
        "no_third_identical_call": failure_guards.get("third_identical_call") == 0,
        "no_marker_leak": failure_guards.get("marker_leak") == 0,
        "no_empty_turn": failure_guards.get("empty_turn") == 0,
        "fake_xml_never_success": failure_guards.get("fake_xml_counted_success") == 0,
        "ask_permission_preserved": ask_permission_preserved is True,
        "row_baseline_recall_not_regressed": (
            recall is not None and (baseline_recall is None or recall >= baseline_recall)
        ),
        "low_baseline_recall_improved": (
            recall is not None
            and baseline_recall is not None
            and (baseline_recall >= recall_min or recall - baseline_recall >= improvement)
        ),
        "catalog_token_reduction": (
            catalog_prompt_tokens is not None
            and baseline_catalog_tokens is not None
            and catalog_prompt_tokens <= baseline_catalog_tokens * catalog_ratio
        ),
    }
    return GateVerdict(passed=all(checks.values()), checks=checks)


_FORBIDDEN_RESULT_KEYS = frozenset(
    {
        "prompt",
        "root",
        "path",
        "args",
        "arguments",
        "tool_args",
        "tool_output",
        "content",
        "text",
        "session_id",
        "session_ids",
        "request",
        "response",
    }
)


def validate_private_result(value: object) -> None:
    """Reject project-identifying fields before an eval result reaches disk."""

    if not isinstance(value, dict):
        raise EvalError("result root must be an object")
    allowed_top = {
        "schema_version",
        "mode",
        "compatibility",
        "cases",
        "aggregate",
        "support_gate",
    }
    unexpected = set(value) - allowed_top
    if unexpected:
        raise EvalError("result has fields outside the privacy contract")

    def walk(item: object, *, parent: str | None = None) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                key_name = key.lower() if isinstance(key, str) else ""
                numeric_prompt_tokens = (
                    key_name == "prompt"
                    and parent == "tokens"
                    and isinstance(child, int)
                    and not isinstance(child, bool)
                    and child >= 0
                )
                if not isinstance(key, str) or (
                    key_name in _FORBIDDEN_RESULT_KEYS and not numeric_prompt_tokens
                ):
                    raise EvalError("result contains a forbidden privacy field")
                # Numeric generated-token counts are permitted; arbitrary
                # model/tool output fields are not.
                if key_name == "output" and parent != "tokens":
                    raise EvalError("result contains a forbidden privacy field")
                walk(child, parent=key_name)
        elif isinstance(item, list):
            for child in item:
                walk(child, parent=parent)
        elif isinstance(item, str):
            if "\n" in item or item.startswith("/") or _WINDOWS_ABSOLUTE_RE.match(item):
                raise EvalError("result contains free-form text or an absolute path")
        elif item is not None and not isinstance(item, (int, float, bool)):
            raise EvalError("result contains a non-JSON value")

    walk(value)


def write_private_result(path: Path, value: Mapping[str, Any]) -> None:
    validate_private_result(dict(value))
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def client_identity(root: Path) -> str:
    """跑這次評測的客戶端身分。

    以前這一格是 `opencode --version` —— 那是決定「模型看到什麼、工具怎麼被
    呼叫」的那一端。現在那一端是我們自己的客戶端,所以身分換成 engine /
    prompt / 進入點三個檔的內容雜湊加上 system prompt 的 digest。
    """
    parts = []
    for name in ("client_engine.py", "client_prompt.py", "codetrail_chat.py"):
        try:
            parts.append(text_digest((REPO_ROOT / name).read_text(encoding="utf-8")))
        except OSError:
            parts.append("missing")
    try:
        import client_prompt

        parts.append(client_prompt.build_system_prompt(root).digest)
    except Exception:  # noqa: BLE001
        parts.append("prompt-unavailable")
    return text_digest("|".join(parts))[:16]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LocalJsonClient:
    """No-proxy/no-redirect JSON client used only on the authorised live path."""

    def __init__(self, base_url: str, *, timeout_seconds: int = 120):
        try:
            parsed = urllib.parse.urlsplit(base_url.rstrip("/"))
            hostname = parsed.hostname
        except ValueError as exc:
            raise EvalError("main model base URL is invalid") from exc
        remote_ok = os.environ.get("AICODE_MODEL_REMOTE_OK", "").lower() in (
            "1",
            "true",
            "yes",
        )
        if parsed.scheme not in ("http", "https") or not hostname or parsed.query or parsed.fragment:
            raise EvalError("main model base URL is invalid")
        if hostname not in ("localhost", "127.0.0.1", "::1") and not remote_ok:
            raise EvalError("non-loopback model endpoint requires AICODE_MODEL_REMOTE_OK=1")
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[:-3]
        self.timeout_seconds = timeout_seconds
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
        )

    def get_json(self, path: str) -> dict[str, Any]:
        return self._request("GET", path, None)

    def post_json(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, body)

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                payload = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise EvalError("local model endpoint request failed") from exc
        if len(payload) > MAX_HTTP_RESPONSE_BYTES:
            raise EvalError("local model endpoint response is too large")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvalError("local model endpoint returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise EvalError("local model endpoint returned a non-object")
        return value


def _normalise_server_root(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    return value[:-3] if value.endswith("/v1") else value


def _looks_like_model_path(value: str) -> bool:
    return value.startswith(("/", "~", "./", "../")) or value.lower().endswith(".gguf")


def _bare_model(value: str) -> str:
    """逐題 client 與 canary 拿到的模型名稱必須是客戶端真正用的那一個:bare registry
    name 或 GGUF 路徑。舊式 `llamacpp/<name>` 剝掉前綴;外部 provider 一律拒絕。"""
    resolved = model_resolution.normalize_main_model(str(value or ""), "--model")
    if not resolved.ok:
        raise EvalError(resolved.error or f"invalid --model {value!r}")
    return resolved.model


def _model_server_base_url(
    config: Mapping[str, Any],
    *,
    model: str,
    environment: Mapping[str, str],
) -> str:
    """Bind direct token/props probes to the endpoint the client will use.

    `config` 是空的(去 OpenCode 化之後沒有第二份 provider 設定);留著參數是為了
    result identity 的 digest 形狀不變。真正的來源是 `AICODE_LLAMA_BASE_URL`。
    """

    # `--model` 跟 `aicode -m` 一樣收 bare registry name / GGUF 路徑;舊式
    # `llamacpp/<name>` 仍接受(provider 段只用來對照 config 裡的 baseURL,沒有就是本地)。
    _bare_model(model)                       # 外部 provider 在這裡就拒絕
    provider_name, separator, _model_name = model.partition("/")
    if not separator or _looks_like_model_path(model):
        provider_name = "llamacpp"
    providers = config.get("provider")
    provider = providers.get(provider_name) if isinstance(providers, Mapping) else None
    options = provider.get("options") if isinstance(provider, Mapping) else None
    configured = options.get("baseURL") if isinstance(options, Mapping) else None
    if configured is not None and not isinstance(configured, str):
        raise EvalError("effective provider baseURL is invalid")
    if isinstance(configured, str) and not configured.strip():
        raise EvalError("effective provider baseURL is invalid")
    environment_url = environment.get("AICODE_LLAMA_BASE_URL")
    configured_root = _normalise_server_root(configured) if configured else None
    environment_root = _normalise_server_root(environment_url) if environment_url else None
    if configured_root and environment_root and configured_root != environment_root:
        raise EvalError("model endpoint environment and configured provider baseURL disagree")
    return environment_root or configured_root or "http://localhost:8080"


def compatibility_identity(
    *,
    row: Mapping[str, Any],
    arm: str,
    client_version: str | None,
    catalog: CatalogSnapshot,
    props: Mapping[str, Any] | None = None,
    effective_config: Mapping[str, Any] | None = None,
    selected_model: str | None = None,
    fixture_digest: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    expected = row.get("compatibility")
    expected = expected if isinstance(expected, Mapping) else {}
    props = props or {}
    template = props.get("chat_template")
    caps = props.get("chat_template_caps")
    build_info = props.get("build_info")
    runtime_model = props.get("model_alias", props.get("model_path"))
    settings = props.get("default_generation_settings")
    settings = settings if isinstance(settings, Mapping) else {}
    n_ctx = props.get("n_ctx", settings.get("n_ctx", expected.get("n_ctx")))
    if not isinstance(n_ctx, int) or isinstance(n_ctx, bool):
        n_ctx = None
    effective_config = effective_config or {}
    environment = environment or {}
    agent = effective_config.get("agent")
    build = agent.get("build") if isinstance(agent, Mapping) else None
    prompt_value = build.get("prompt") if isinstance(build, Mapping) else None
    build_prompt_digest = _effective_prompt_digest(prompt_value)
    permission = effective_config.get("permission")
    todowrite = permission.get("todowrite") if isinstance(permission, Mapping) else None
    if todowrite not in ("allow", "ask", "deny"):
        todowrite = "custom" if todowrite is not None else "absent"
    if isinstance(build_info, (Mapping, list)):
        build_digest = json_digest(build_info)
    elif build_info is not None:
        build_digest = text_digest(str(build_info))
    else:
        build_digest = None
    # 「這一臂的契約」= 模型實際看到的東西。OpenCode 時代那兩格是 build prompt
    # 與 todowrite 權限;現在那兩件事的等價物是客戶端的基底規則與 ask 工具集合
    # (`effective_config` 永遠是空的,留著它們等於把兩個常數 hash 進去)。
    arm_contract_digest = json_digest(
        {
            "tools_digest": catalog.tools_digest,
            "instructions_digest": catalog.instructions_digest,
            "client_rules_digest": _client_rules_digest(),
            "ask_tools": sorted(client_policy.ASK_TOOLS),
        }
    )
    # 以前這一格 hash 的是 OpenCode 的全域 AGENTS.md;現在模型每一輪看到的
    # 使用者層規則是 `~/.config/codetrail/instructions.md`,hash 它(鍵名不改,
    # result / matrix 的形狀維持;歷史 row 的值本來就對不上現行客戶端)。
    global_agents_digest = None
    if environment.get("HOME") or environment.get("USERPROFILE"):
        global_agents_digest = _file_digest_state(client_prompt.user_instructions_path(environment))
    return {
        "matrix_row": row.get("id"),
        "arm": arm,
        "model_family": expected.get("model_family"),
        "quantization": expected.get("quantization"),
        "template_family": expected.get("template_family"),
        "selected_model_digest": text_digest(selected_model) if selected_model else None,
        "runtime_model_digest": (text_digest(runtime_model) if isinstance(runtime_model, str) else None),
        "chat_template_digest": text_digest(template) if isinstance(template, str) else None,
        "chat_template_caps": dict(caps) if isinstance(caps, Mapping) else None,
        "chat_template_caps_digest": json_digest(caps) if isinstance(caps, Mapping) else None,
        "n_ctx": n_ctx,
        "client_version": client_version,
        "llama_cpp_build_digest": build_digest,
        "tools_digest": catalog.tools_digest,
        "instructions_digest": catalog.instructions_digest,
        "build_prompt_digest": build_prompt_digest,
        "todowrite_permission": todowrite,
        "arm_contract_digest": arm_contract_digest,
        "effective_config_digest": json_digest(effective_config),
        "global_agents_digest": global_agents_digest,
        "fixture_digest": fixture_digest,
    }


def _client_rules_digest() -> str:
    """客戶端基底規則的 digest(OpenCode 時代 build prompt 的等價物)。"""
    return text_digest(client_prompt.BASE_RULES)


def _effective_prompt_digest(value: object) -> str:
    """Hash effective prompt content/reference state without persisting a path."""

    if not isinstance(value, str):
        return json_digest({"state": "absent" if value is None else "invalid_type"})
    if value.startswith("{file:") and value.endswith("}"):
        raw_path = value[len("{file:") : -1]
        try:
            path = Path(raw_path)
            if not path.is_absolute():
                return json_digest({"state": "invalid_reference"})
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError):
            return json_digest({"state": "unreadable_reference"})
        return text_digest(content)
    return text_digest(value)


def _file_digest_state(path: Path) -> str:
    try:
        return text_digest(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return json_digest({"state": "missing"})
    except (OSError, UnicodeError):
        return json_digest({"state": "unreadable"})


def assert_compatibility(row: Mapping[str, Any], identity: Mapping[str, Any]) -> None:
    expected = row.get("compatibility")
    if not isinstance(expected, Mapping):
        raise EvalError("matrix row compatibility is invalid")
    comparable = (
        "model_family",
        "quantization",
        "template_family",
        "selected_model_digest",
        "runtime_model_digest",
        "chat_template_digest",
        "chat_template_caps",
        "chat_template_caps_digest",
        "n_ctx",
        "client_version",
        "llama_cpp_build_digest",
        "effective_config_digest",
        "global_agents_digest",
        "fixture_digest",
    )
    for key in comparable:
        wanted = expected.get(key)
        if wanted is not None and identity.get(key) != wanted:
            raise EvalError("runtime compatibility identity does not match the matrix row")


def assert_arm_contract(
    matrix: Mapping[str, Any],
    arm: str,
    identity: Mapping[str, Any],
) -> None:
    arms = matrix.get("arms")
    spec = arms.get(arm) if isinstance(arms, Mapping) else None
    if not isinstance(spec, Mapping):
        raise EvalError("support matrix arm contract is invalid")
    expected = spec.get("contract_digest")
    if expected is not None and identity.get("arm_contract_digest") != expected:
        raise EvalError("effective schema/prompt/permission state does not match the selected arm")


def _schemas(catalog: CatalogSnapshot) -> dict[str, Mapping[str, Any]]:
    return {tool.name: tool.input_schema for tool in catalog.tools}


def _run_client_attempt(
    *,
    project: Path,
    prompt: str,
    model: str | None,
    environment: Mapping[str, str],
    timeout_seconds: int,
) -> AttemptTrace:
    # 真的跑一次 headless 客戶端,而且是**唯讀**權限:routing eval 只驗模型會
    # 不會選對工具,它沒有理由能寫到 fixture 專案裡。不帶 --persist,所以
    # 評測不會在使用者的 session 清單裡留下對話(也就沒有「刪掉暫存 session」
    # 這一步)。
    command = [
        sys.executable,
        str(REPO_ROOT / "codetrail_chat.py"),
        "--root",
        str(project),
        "--policy",
        "readonly",
    ]
    child_env = dict(environment)
    if model:
        # 跟 canary 一樣把 --model 真的送出去:不然 15 題逐題跑的是呼叫環境
        # AICODE_MODEL 指的那顆(或直接啟動失敗),結果卻歸到指定模型的 identity。
        # 送的是正規化後的 bare name(直接執行的 codetrail_chat.py 不會像 aicode
        # wrapper 那樣剝掉 llamacpp/ 前綴)。
        bare = _bare_model(model)
        command.extend(["--model", bare])
        child_env["AICODE_MODEL"] = bare
    command.extend(["run", "--format", "json", prompt])
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            cwd=str(project),
            env=child_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        stdout = completed.stdout
        harness_error = completed.returncode != 0
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout_value = exc.stdout
        if isinstance(stdout_value, bytes):
            stdout = stdout_value.decode("utf-8", errors="replace")
        else:
            stdout = stdout_value or ""
        harness_error = False
    except OSError:
        return parse_event_stream("", harness_error=True)
    latency = round((time.monotonic() - started) * 1000)
    trace = parse_event_stream(stdout, latency_ms=latency, harness_error=harness_error)
    # A timeout with a completed terminal response is still a harness failure;
    # without terminal it follows the one permitted terminal retry path.
    if timed_out and trace.terminal:
        trace = replace(trace, harness_error=True)
    return trace


def _delete_sessions(
    session_ids: Sequence[str],
    *,
    project: Path,
    environment: Mapping[str, str],
) -> bool:
    """headless 預設 ephemeral —— 評測的對話從來沒有落檔,沒有東西要刪。

    保留這個函式(永遠回 True)是為了讓報告欄位與呼叫端的形狀不變;真正的
    保證在 `codetrail_chat run` 不帶 `--persist`。
    """
    del session_ids, project, environment
    return True


def _run_explicit_canary_gate(
    *,
    project: Path,
    model: str | None,
    environment: Mapping[str, str],
    timeout_seconds: int,
) -> dict[str, Any]:
    """Run T4's named probe once, retrying one failure as flaky evidence."""

    try:
        from scripts import tool_call_canary
    except ImportError:
        return {"attempts": 0, "successes": 0, "rate": 0.0, "flaky": False}
    attempts = 0
    successes = 0
    sessions_ok = True
    for _ in range(2):
        attempts += 1
        try:
            evidence = tool_call_canary.run_model_attempt(
                root=project,
                env=environment,
                model_override=model or "",
                timeout=timeout_seconds,
            )
        except Exception:
            continue
        sessions_ok = (
            _delete_sessions(
                evidence.session_ids,
                project=project,
                environment=environment,
            )
            and sessions_ok
        )
        if evidence.success and sessions_ok:
            successes += 1
            break
    return {
        "attempts": attempts,
        "successes": successes,
        "rate": round(successes / attempts, 6),
        "flaky": attempts > 1 and successes == 1,
    }


def _ask_permission_contract(_config: Mapping[str, Any] | None = None) -> bool:
    """六個寫入工具仍然是 ask。

    以前這是讀 opencode.json 的 `permission`;現在權限是客戶端的 policy,所以
    契約檢查的對象換成那份 policy 本身 —— 而 eval 一律跑 readonly,連 ask 都
    不會發生。保留這個函式是為了讓報告欄位語意不變。
    """
    import client_policy

    return client_policy.ASK_TOOLS == frozenset(
        {"apply_patch", "run_lint", "run_command", "remove_document",
         "record_lesson", "review_figures"}
    )


async def _prepare_synthetic_knowledge(
    command: StdioMcpCommand,
    *,
    project: Path,
    root_canary: str,
    documents: Sequence[str],
    environment: Mapping[str, str],
    timeout_seconds: int,
) -> None:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise EvalError("mcp package is required to prepare the synthetic KB") from exc
    server_env = dict(environment)
    server_env.update(command.environment)
    server_env["AICODE_ROOT"] = str(project)
    server_env["AI_CODE_COLLECT_DATA"] = "0"
    params = StdioServerParameters(
        command=command.argv[0],
        args=list(command.argv[1:]),
        env=server_env,
        cwd=str(project),
    )

    async def prepare() -> None:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    root_probe = await session.call_tool(
                        "list_dir",
                        {"path": ".", "depth": 1},
                    )
                    if bool(getattr(root_probe, "isError", False)) or root_canary not in _result_text(
                        root_probe
                    ):
                        raise EvalError("synthetic root binding probe failed")
                    for document in documents:
                        result = await session.call_tool(
                            "ingest_document",
                            {"path": document, "mode": "document"},
                        )
                        if bool(getattr(result, "isError", False)):
                            raise EvalError("synthetic knowledge preparation failed")
                    result = await session.call_tool("reload_knowledge_base", {})
                    if bool(getattr(result, "isError", False)):
                        raise EvalError("synthetic knowledge reload failed")

    try:
        await asyncio.wait_for(prepare(), timeout=timeout_seconds)
    except TimeoutError as exc:
        raise EvalError("synthetic knowledge preparation timed out") from exc
    except EvalError:
        raise
    except Exception as exc:
        raise EvalError(f"synthetic knowledge preparation failed ({type(exc).__name__})") from exc


def _result_text(result: object) -> str:
    content = getattr(result, "content", ())
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return ""
    values: list[str] = []
    for block in content:
        text = block.get("text") if isinstance(block, Mapping) else getattr(block, "text", None)
        if isinstance(text, str):
            values.append(text)
    return "\n".join(values)


async def _acquire_catalog(
    *,
    args: argparse.Namespace,
    root: Path,
    effective_config: Mapping[str, Any],
    environment: Mapping[str, str],
) -> tuple[CatalogSnapshot, StdioMcpCommand | None]:
    if args.catalog_source == "in-process":
        overrides = {
            "AICODE_ROOT": str(root),
            MODEL_SERVER_PREFLIGHT_SKIP_ENV: "1",
            "AI_CODE_COLLECT_DATA": "0",
        }
        previous = {key: os.environ.get(key) for key in overrides}
        try:
            os.environ.update(overrides)
            module = importlib.import_module(args.in_process_module)
            server = getattr(module, "mcp", None)
            if server is None:
                raise EvalError("in-process module has no mcp object")
            catalog = await catalog_from_fastmcp(server)
        except SystemExit as exc:
            raise EvalError("in-process MCP catalog exited during import") from exc
        except Exception as exc:
            if isinstance(exc, (CatalogError, EvalError)):
                raise
            raise EvalError(f"in-process MCP catalog failed ({type(exc).__name__})") from exc
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return catalog, None
    # 客戶端啟動 MCP server 的方式就是這一條;沒有第二份 OpenCode 設定可以抽。
    # 走 client_mcp 的常數而不是自己拼路徑,兩邊才不會各自漂移。
    command = StdioMcpCommand(
        (sys.executable, str(client_mcp.SERVER_SCRIPT)),
        {"AICODE_ROOT": str(root)},
    )
    if args.catalog_only:
        command_environment = dict(command.environment)
        command_environment[MODEL_SERVER_PREFLIGHT_SKIP_ENV] = "1"
        command = StdioMcpCommand(command.argv, command_environment)
    catalog = await catalog_from_stdio(
        command,
        root=root,
        environment=environment,
        timeout_seconds=args.mcp_timeout,
    )
    return catalog, command


def _catalog_result(
    *,
    row: Mapping[str, Any],
    arm: str,
    client_version: str | None,
    catalog: CatalogSnapshot,
    effective_config: Mapping[str, Any],
    fixture_digest: str,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    configured_model = effective_config.get("model")
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "mode": "catalog_only",
        "compatibility": compatibility_identity(
            row=row,
            arm=arm,
            client_version=client_version,
            catalog=catalog,
            effective_config=effective_config,
            selected_model=configured_model if isinstance(configured_model, str) else None,
            fixture_digest=fixture_digest,
            environment=environment,
        ),
        "aggregate": {"catalog": catalog.summary()},
        "support_gate": {
            "passed": False,
            "checks": {"model_evaluation_authorised_and_complete": False},
            "matrix_status": row.get("status"),
            "manual_status_change_required": False,
        },
    }


def _model_result(
    *,
    row: Mapping[str, Any],
    arm: str,
    identity: Mapping[str, Any],
    outcomes: Sequence[CaseOutcome],
    aggregate: dict[str, Any],
    catalog: CatalogSnapshot,
    catalog_tokens: Mapping[str, Any],
    gate: GateVerdict,
) -> dict[str, Any]:
    aggregate = dict(aggregate)
    aggregate["catalog"] = catalog.summary()
    aggregate["catalog_tokens"] = dict(catalog_tokens)
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "mode": "model",
        "compatibility": dict(identity),
        "cases": [outcome.private_record() for outcome in outcomes],
        "aggregate": aggregate,
        "support_gate": gate.private_record(current_status=str(row.get("status"))),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        raise EvalError("--root must be an existing directory")
    cases_data = load_cases(args.cases)
    matrix = load_support_matrix(args.support_matrix)
    row = select_matrix_row(matrix, args.matrix_row, args.arm)
    environment = os.environ.copy()
    if args.catalog_only:
        # mcp_server's startup preflight normally exercises embedding/reranker/
        # VL endpoints.  Catalog-only is explicitly a zero-model mode, and the
        # preflight does not affect initialize/tools/list contract bytes.
        environment[MODEL_SERVER_PREFLIGHT_SKIP_ENV] = "1"

    # The in-process path is the deliberately offline CI path.  The stdio path
    # now starts our own ``mcp_server.py`` through the same command the client
    # uses, so there is no external client to version-check any more; the
    # identity that matters is the CodeTrail client itself.
    effective_config: dict[str, Any] = {}
    if args.catalog_source == "in-process":
        if not args.catalog_only:
            raise EvalError("in-process catalog source is catalog-only")
        client_version = None
    else:
        client_version = client_identity(root)
    catalog, command = await _acquire_catalog(
        args=args,
        root=root,
        effective_config=effective_config,
        environment=environment,
    )
    if getattr(args, "frozen_contract", False):
        if args.arm != "baseline":
            raise EvalError("--frozen-contract is valid only for the baseline arm")
        assert_frozen_catalog_contract(catalog, row)
    else:
        assert_public_tool_contract(catalog)

    if args.catalog_only:
        return _catalog_result(
            row=row,
            arm=args.arm,
            client_version=client_version,
            catalog=catalog,
            effective_config=effective_config,
            fixture_digest=json_digest(cases_data),
            environment=environment,
        )
    assert client_version is not None
    if command is None:
        raise EvalError("model evaluation requires the effective stdio MCP catalog")
    # 命令是我們自己組的,AICODE_ROOT 一定指向這次評測的 sandbox root;
    # 這裡守的是「不得在評測途中打開 data flywheel」。
    if command.environment.get("AI_CODE_COLLECT_DATA", "").lower() in ("1", "true", "yes"):
        raise EvalError("routing eval refuses an MCP command that persists data-flywheel records")

    configured_model = args.model
    if not configured_model:
        configured_model = effective_config.get("model")
    if not isinstance(configured_model, str) or not configured_model:
        raise EvalError("model evaluation requires --model or effective config.model")
    # 實際打到 llama-server 的每一個 request(直接 token probe、canary、逐題 client)
    # 都用正規化後的 bare name;result identity 的 selected_model 保留使用者原字串。
    probe_model = _bare_model(configured_model)
    base_url = _model_server_base_url(
        effective_config,
        model=probe_model,
        environment=environment,
    )
    client = LocalJsonClient(base_url, timeout_seconds=args.model_timeout)
    props = client.get_json("/props")
    caps = props.get("chat_template_caps")
    if isinstance(caps, Mapping) and caps.get("supports_tools") is False:
        raise EvalError("chat template explicitly reports supports_tools=false")
    identity = compatibility_identity(
        row=row,
        arm=args.arm,
        client_version=client_version,
        catalog=catalog,
        props=props,
        effective_config=effective_config,
        selected_model=configured_model,
        fixture_digest=json_digest(cases_data),
        environment=environment,
    )
    assert_compatibility(row, identity)
    assert_arm_contract(matrix, args.arm, identity)

    token_measurement = measure_catalog_prompt_tokens(
        model=probe_model,
        catalog=catalog,
        post_json=client.post_json,
    )

    fixture = cases_data["fixture"]
    documents = fixture.get("knowledge_documents", [])
    cases = cases_data["cases"]
    outcomes: list[CaseOutcome] = []
    explicit_canary: dict[str, Any]
    with tempfile.TemporaryDirectory(prefix="codetrail-routing-eval-") as temp_dir:
        project = Path(temp_dir) / "synthetic"
        materialize_synthetic_fixture(cases_data, project)
        model_env = environment.copy()
        model_env["AICODE_ROOT"] = str(project)
        # Evaluation prompts/results may exist transiently in OpenCode memory,
        # but must not enter the persistent data-flywheel JSONL.
        model_env["AI_CODE_COLLECT_DATA"] = "0"
        await _prepare_synthetic_knowledge(
            command,
            project=project,
            root_canary=fixture["root_canary"],
            documents=documents,
            environment=model_env,
            timeout_seconds=args.mcp_timeout,
        )
        explicit_canary = _run_explicit_canary_gate(
            project=project,
            model=probe_model,
            environment=model_env,
            timeout_seconds=min(args.model_timeout, 120),
        )
        schemas = _schemas(catalog)
        for case in cases:
            attempts: list[AttemptTrace] = []
            first = _run_client_attempt(
                project=project,
                prompt=case["prompt"],
                model=probe_model,
                environment=model_env,
                timeout_seconds=args.model_timeout,
            )
            attempts.append(first)
            sessions_deleted = _delete_sessions(
                first.session_ids,
                project=project,
                environment=model_env,
            )
            if not first.terminal:
                second = _run_client_attempt(
                    project=project,
                    prompt=case["prompt"],
                    model=probe_model,
                    environment=model_env,
                    timeout_seconds=args.model_timeout,
                )
                attempts.append(second)
                sessions_deleted = (
                    _delete_sessions(
                        second.session_ids,
                        project=project,
                        environment=model_env,
                    )
                    and sessions_deleted
                )
            outcome = evaluate_attempts(case, attempts, schemas=schemas)
            if not sessions_deleted:
                outcome = replace(outcome, classification=Classification.HARNESS_INVALID)
            outcomes.append(outcome)

    aggregate = aggregate_outcomes(outcomes)
    aggregate["explicit_canary"] = explicit_canary
    ask_permission_preserved = _ask_permission_contract(effective_config)
    aggregate["permissions"] = {"ask_preserved": ask_permission_preserved}
    thresholds = matrix.get("gates")
    thresholds = thresholds if isinstance(thresholds, Mapping) else {}
    gate = evaluate_support_gate(
        aggregate=aggregate,
        row=row,
        thresholds=thresholds,
        catalog_prompt_tokens=token_measurement.catalog_prompt_tokens,
        explicit_canary_rate=_nested_number(explicit_canary, "rate"),
        ask_permission_preserved=ask_permission_preserved,
    )
    return _model_result(
        row=row,
        arm=args.arm,
        identity=identity,
        outcomes=outcomes,
        aggregate=aggregate,
        catalog=catalog,
        catalog_tokens=token_measurement.summary(),
        gate=gate,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Measure CodeTrail tool routing")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--matrix-row", required=True)
    parser.add_argument("--arm", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", help="model override for the headless client (a registry name or GGUF path, as aicode -m)")
    parser.add_argument(
        "--catalog-only",
        action="store_true",
        help="capture live tools/list only; never call the model",
    )
    parser.add_argument(
        "--frozen-contract",
        action="store_true",
        help="require an exact match to this row's saved historical baseline catalog",
    )
    parser.add_argument(
        "--catalog-source",
        choices=("stdio", "in-process"),
        default="stdio",
        help="stdio is the runtime default; in-process exists for offline CI",
    )
    parser.add_argument("--in-process-module", default="mcp_server", help=argparse.SUPPRESS)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH, help=argparse.SUPPRESS)
    parser.add_argument(
        "--support-matrix",
        type=Path,
        default=DEFAULT_MATRIX_PATH,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--mcp-timeout",
        type=int,
        default=DEFAULT_MCP_TIMEOUT_SECONDS,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--model-timeout",
        type=int,
        default=DEFAULT_MODEL_TIMEOUT_SECONDS,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mcp_timeout < 1 or args.model_timeout < 1:
        parser.error("timeouts must be positive")
    try:
        result = asyncio.run(run(args))
        write_private_result(args.output, result)
    except (CatalogError, EvalError, TokenMeasurementError, OSError) as exc:
        # Exception classes are actionable, but omit exception text because an
        # upstream process error could contain a project-local path.
        print(f"routing eval failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    print("routing eval result written (privacy-safe aggregate only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
