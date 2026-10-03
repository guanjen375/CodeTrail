"""Render compact MCP text while preserving evidence tools' structured payloads."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from mcp import types

import ingest_notify
from mcp_contract import EVIDENCE_TOOL_NAMES


DEFAULT_CONTEXT_FRACTION = 0.12


@dataclass(frozen=True)
class ResultBudget:
    token_limit: int
    char_limit: int
    explicit: bool
    context_risk: bool


def estimate_result_tokens(text: str) -> int:
    """Conservative mixed ASCII/CJK proxy required by the public contract."""
    ascii_count = sum(1 for char in text if ord(char) < 128)
    non_ascii_count = len(text) - ascii_count
    return math.ceil(ascii_count / 3) + math.ceil(non_ascii_count * 1.5)


def resolve_result_budget(
    *, n_ctx: int, requested_max_chars: int | None, safety_max_chars: int
) -> ResultBudget:
    if isinstance(n_ctx, bool) or not isinstance(n_ctx, int) or n_ctx <= 0:
        raise ValueError("n_ctx must be a positive integer")
    if isinstance(safety_max_chars, bool) or not isinstance(safety_max_chars, int) or safety_max_chars <= 0:
        raise ValueError("safety_max_chars must be a positive integer")

    default_tokens = max(1, math.floor(n_ctx * DEFAULT_CONTEXT_FRACTION))
    default_chars = min(safety_max_chars, default_tokens * 3)
    if requested_max_chars is None:
        return ResultBudget(
            token_limit=default_tokens,
            char_limit=default_chars,
            explicit=False,
            context_risk=False,
        )
    if isinstance(requested_max_chars, bool) or not isinstance(requested_max_chars, int):
        raise ValueError("requested_max_chars must be an integer or None")
    if requested_max_chars <= 0:
        raise ValueError("requested_max_chars must be positive")
    char_limit = min(requested_max_chars, safety_max_chars)
    return ResultBudget(
        token_limit=max(1, math.ceil(char_limit / 3)),
        char_limit=char_limit,
        explicit=True,
        context_risk=char_limit > default_chars,
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _source_label(ref: object) -> str:
    if not isinstance(ref, dict):
        return str(ref)
    source = str(ref.get("source") or ref.get("path") or "?")
    page = ref.get("page")
    section = ref.get("section")
    line = ref.get("line") or ref.get("start_line")
    suffix = f" p.{page}" if page is not None else (f" §{section}" if section else "")
    if line is not None:
        suffix += f":{line}"
    flags = []
    for key in ("verification_status", "truncated"):
        value = ref.get(key)
        if value not in (None, False, ""):
            flags.append(f"{key}={value}")
    return source + suffix + (" (" + ",".join(flags) + ")" if flags else "")


def _render_refs(refs: object) -> str:
    if not isinstance(refs, list) or not refs:
        return ""
    return "\n".join(f"source: {_source_label(ref)}" for ref in refs)


def _render_excluded(excluded: object, hint: object = "") -> str:
    if not isinstance(excluded, list) or not excluded:
        return ""
    rows = [_source_label(item) for item in excluded]
    lines = [f"excluded_figures: {rows[0]}"]
    lines.extend(f"excluded_figure: {row}" for row in rows[1:])
    if hint:
        lines.append(f"review: {hint}")
    return "\n".join(lines)


def _render_excluded_text(payload: dict[str, Any]) -> str:
    excluded = payload.get("excluded_text")
    if not isinstance(excluded, list) or not excluded:
        return ""
    return "\n".join([
        "excluded_text: " + _json(item) for item in excluded
    ] + ["review: use review_text(action=\"list\") to inspect the current OCR revisions."])


def _render_query_knowledge(payload: dict[str, Any]) -> str:
    if payload.get("error"):
        return str(payload["error"])
    # `display` and `refs` repeat the same evidence already present in `text`.
    # Prefer the actual evidence text, then append only compact source labels.
    primary = str(payload.get("text") or payload.get("display") or "")
    # Put bounded provenance before bulk REF text so result-budget truncation
    # cannot remove the source/page and review lane first.
    parts = [part for part in (
        _render_refs(payload.get("refs")),
        _render_excluded(payload.get("excluded_figures"), payload.get("review_hint")),
        _render_excluded_text(payload),
        primary,
    ) if part]
    return "\n".join(parts) or "No matching knowledge-base evidence."


def _render_query_knowledge_strict(payload: dict[str, Any]) -> str:
    if payload.get("refused"):
        primary = f"refused: {payload.get('reason') or 'insufficient evidence'}"
    else:
        primary = str(payload.get("answer") or payload.get("reason") or "No strict answer produced.")
    parts = [part for part in (
        _render_refs(payload.get("refs")),
        _render_excluded(payload.get("excluded_figures"), payload.get("review_hint")),
        _render_excluded_text(payload),
        primary,
    ) if part]
    return "\n".join(parts)


def _render_query_table(payload: dict[str, Any]) -> str:
    # Each cell and its provenance form a single JSON line. The common line-safe
    # budget fitter must drop a whole cell, never leave a shortened literal value.
    lines = [f"table_status: {payload.get('status', 'unknown')}",
             f"has_ref: {_json(payload.get('has_ref', False))}"]
    for key in ("scope", "reason", "ambiguous", "truncated", "match_count", "excluded_count", "error"):
        if key in payload:
            lines.append(f"{key}: {_json(payload[key])}")
    for item in payload.get("excluded", []):
        lines.append("excluded: " + _json(item))
    for item in payload.get("matches", []):
        lines.append("cell: " + _json(item))
    return "\n".join(lines)


def _render_review_text(payload: str) -> str:
    # The review API uses a JSON text protocol. Keep its failure status visible
    # and its CAS locator ahead of potentially large OCR/history values.
    try:
        result = json.loads(payload)
    except (ValueError, TypeError):
        return payload  # Busy and preflight errors retain their existing protocol.
    if not isinstance(result, dict):
        return payload
    if result.get("status") == "error":
        return "錯誤: " + str(result.get("reason") or "OCR review failed")
    bulk_keys = ("original_ocr", "text", "corrected_text", "text_locator",
                 "confirmation", "quality_issues", "history")
    lines = ["review_status: " + _json(result.get("status", "unknown")),
             "action: " + _json(result.get("action", ""))]
    units = result.get("units", [result])
    if not isinstance(units, list):
        return payload
    lines.append(f"units: {len(units)}")
    for unit in units:
        if not isinstance(unit, dict):
            continue
        metadata = {key: value for key, value in unit.items()
                    if key not in bulk_keys and key not in {"status", "action"}}
        lines.append("unit: " + _json(metadata))
        for key in bulk_keys:
            if key in unit:
                # A whole value fits or is omitted, never a shortened correction.
                lines.append(f"{key}: " + _json(unit[key]))
    return "\n".join(lines)


def _render_code_rag(payload: object) -> str:
    """Render line-atomic evidence with routing metadata first.

    The untouched core remains in structuredContent.  The client reads only this
    text lane, so it must keep graph/truncation/uncertainty facts even when the
    lower-priority source windows do not fit.
    """
    if not isinstance(payload, list):
        return f"result: {_json(payload)}"
    if not payload:
        return "results: 0"

    lines: list[str] = []
    for index, item in enumerate(payload, 1):
        if not isinstance(item, dict):
            lines.append(f"result[{index}]: {_json(item)}")
            continue

        mode = str(item.get("mode") or ("context" if "evidence" in item else "semantic"))
        if len(payload) > 1:
            lines.append(f"result: {index}/{len(payload)}")
        for key in (
            "mode", "build_context", "build_state", "query", "src", "dst", "graph_status", "truncated",
            "budget_chars", "used_chars", "error",
        ):
            if key in item:
                lines.append(f"{key}: {_json(item[key]) if not isinstance(item[key], str) else item[key]}")

        uncertainties = item.get("uncertainties")
        if isinstance(uncertainties, list):
            lines.extend(f"uncertainty: {_json(row)}" for row in uncertainties)

        if mode == "context":
            seeds = item.get("seeds")
            if isinstance(seeds, list):
                lines.extend(f"seed: {_json(row)}" for row in seeds)
            evidence = item.get("evidence")
            if isinstance(evidence, list):
                for row in evidence:
                    if not isinstance(row, dict):
                        lines.append(f"evidence: {_json(row)}")
                        continue
                    path = row.get("path", "?")
                    start = row.get("start_line", "?")
                    end = row.get("end_line", start)
                    symbol = row.get("symbol") or "?"
                    reason = row.get("reason") or "?"
                    lines.append(
                        f"evidence: {path}:{start}-{end} symbol={symbol} reason={reason}"
                    )
                    text = row.get("text")
                    if isinstance(text, str):
                        lines.extend(text.splitlines())
            continue

        if mode == "semantic":
            if item.get("results") == [] and "path" not in item:
                lines.append("results: 0")
                continue
            path = item.get("path", "?")
            line = item.get("line", "?")
            lines.append(
                f"hit: {path}:{line} symbol={item.get('symbol', '?')} "
                f"type={item.get('type', '?')} score={item.get('score', '?')}"
            )
            for key in ("score_components", "backend", "confidence", "relations"):
                if key in item:
                    lines.append(f"{key}: {_json(item[key])}")
            continue

        for key in ("truncation", "anchors", "paths", "edges", "nodes", "files"):
            value = item.get(key)
            if isinstance(value, list):
                lines.extend(f"{key}: {_json(row)}" for row in value)
            elif value is not None:
                lines.append(f"{key}: {_json(value)}")
    return "\n".join(lines)


def _render_payload(tool_name: str, payload: object) -> str:
    if tool_name == "review_text" and isinstance(payload, str):
        return _render_review_text(payload)
    if tool_name == "query_table" and isinstance(payload, dict):
        return _render_query_table(payload)
    if tool_name == "query_knowledge" and isinstance(payload, dict):
        return _render_query_knowledge(payload)
    if tool_name == "query_knowledge_strict" and isinstance(payload, dict):
        return _render_query_knowledge_strict(payload)
    if tool_name == "code_rag_search":
        return _render_code_rag(payload)
    if isinstance(payload, str):
        return payload
    return _json(payload)


def _narrowing_step(tool_name: str) -> str:
    if tool_name == "grep_code":
        return "Narrow path/include/pattern, then retry from the smaller search scope."
    if tool_name == "list_dir":
        return "Narrow path or reduce depth, then list the smaller directory scope."
    return "Narrow the query/path/include or continue from the reported position."


def _has_true_truncated(value: object) -> bool:
    if isinstance(value, dict):
        if value.get("truncated") is True:
            return True
        return any(_has_true_truncated(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_true_truncated(item) for item in value)
    return False


def _has_structured_error(value: object) -> bool:
    if isinstance(value, dict):
        return bool(value.get("error"))
    if isinstance(value, list):
        return any(_has_structured_error(item) for item in value)
    return False


def _read_next_step(body: str) -> str:
    visible_lines = re.findall(r"(?m)^\s*(\d+)\s+\|", body)
    if visible_lines:
        return (
            f"Use read_file with start_line={int(visible_lines[-1]) + 1} "
            "to continue without gaps."
        )
    return "Use read_file with a later start_line after the last reported line."


_PARTIAL_MARKERS: dict[str, tuple[str, ...]] = {
    "read_file": (
        "\n... [MCP wrapper 截斷",
        "⚠️ [CTX] 因 MAX_FILE_READ_CHARS",
        "\n... 用 read_file(",
    ),
    "grep_code": (
        "\n... [truncated: too many matches]",
        "\n[CTX] rg 結果不完整",
        "\n⚠️ [CTX] grep 輸出已達",
        "\n⚠️ [CTX] grep 已達",
    ),
    "list_dir": ("\n... [MCP wrapper 截斷",),
    "git_diff": ("\n... [略過 ", "\n... [截斷 "),
    "run_lint": ("\n... [略過 ", "\n... [截斷 "),
    "run_command": ("\n... [略過 ", "\n... [截斷 "),
    "analyze_file": ("\n...[截斷中段 ", "\n[Truncated: conflict/unknown totals"),
    "ingest_document": ("\n...[截斷中段 ", "✗ 輸出不完整"),
    "review_figures": ("\n\n…還有 ",),
}


def _status_for(tool_name: str, payload: object, body: str) -> tuple[str, str | None]:
    lower = body.lower()
    if tool_name == "query_table" and isinstance(payload, dict):
        scope = payload.get("scope") or {}
        scoped = isinstance(scope, dict) and (scope.get("requested_document_id") or scope.get("requested_figure_id"))
        if scoped and (payload.get("has_ref") is False or payload.get("status") == "error"):
            return ("error" if payload.get("status") == "error" else "partial",
                    "Keep the requested document_id and figure_id scope; verify the current document ID, "
                    "figure and review status before retrying. Do not broaden to another source or infer a value.")
        if payload.get("status") == "error":
            return "error", "Correct the reported table selector or source problem before retrying."
    if tool_name == "analyze_file" and body.startswith("Memory consistency: "):
        if body.startswith(("Memory consistency: unknown;", "Memory consistency: conflict;")):
            return "partial", "Inspect the reported ranges and sources; resolve conflicts or supply missing evidence before declaring consistency."
    if body.startswith("⚠ [重複呼叫偵測]"):
        return "partial", "Do not repeat the same call; use the existing result or change tool/path/pattern."
    # ingest 的成敗看的是子行程留下的 marker，不是前綴：一次 exit 0 的 ingest 也
    # 可能留下待覆核的 figure（`status: ok` 會讓模型直接拿去回答），而逾時 /
    # exit≠0 / 輸出不完整這幾種是 `=== ... ✗ ...` 開頭以外也可能發生的失敗。
    busy = ingest_notify.classify_busy_body(tool_name, body)
    if busy is not None:
        # 任何 KB 工具都可能收到 busy(ingest 進行中)。那代表「沒有執行」，
        # 不是「執行成功」——落成 ok 會讓模型停止重試並當作已完成。
        return busy
    if tool_name == "ingest_document":
        ingest_status, ingest_step = ingest_notify.classify_ingest_body(body)
        if ingest_status != "ok":
            return ingest_status, ingest_step
    if _has_structured_error(payload):
        return "error", "Correct the reported input or environment problem, then retry once."
    if isinstance(payload, dict) and (
        payload.get("refused") is True or payload.get("has_ref") is False
    ):
        return "partial", "Use another indexed source or ingest evidence; do not infer the missing answer."
    if body.lstrip().startswith("✗ 驗證未通過") and "patch 已套用" in body:
        return "partial", "The patch remains applied; run lint/tests separately and inspect the reported failure."
    stripped = body.lstrip()
    # These are tool-generated leading envelopes, not arbitrary content words.
    # Live ARC analysis used both formats and otherwise appeared as completed.
    if tool_name == "analyze_file" and stripped.startswith("[ELF 錯誤]"):
        return "error", "Correct the reported input or environment problem, then retry once."
    if tool_name == "run_command" and re.match(r"\A=== ✗ 失敗 \(exit -?\d+\) (?:===|\(無輸出\) ===)", stripped):
        return "error", "Correct the reported input or environment problem, then retry once."
    # file_info can legitimately start with a user path such as error.log.
    # Its success suffix is generated by ToolExecutor and wins over prefixes.
    file_info_success = tool_name == "file_info" and re.search(
        r": (?:檔案|目錄), ", stripped.splitlines()[0] if stripped else ""
    )
    error_prefixes = ("錯誤:", "[error]", "[fatal]", "✗ 失敗", "[kb reload 失敗")
    if not file_info_success and lower.startswith(error_prefixes):
        return "error", "Correct the reported input or environment problem, then retry once."
    if stripped.startswith("✗"):
        return "error", "Correct the reported input or environment problem, then retry once."
    partial = _has_true_truncated(payload) or any(
        marker in body for marker in _PARTIAL_MARKERS.get(tool_name, ())
    )
    if partial:
        if tool_name == "read_file":
            return "partial", _read_next_step(body)
        return "partial", _narrowing_step(tool_name)
    return "ok", None


def _measure(text: str, *, explicit: bool) -> int:
    return len(text) if explicit else estimate_result_tokens(text)


def _fit_body(
    body: str,
    budget: ResultBudget,
    *,
    reserved: int = 0,
    line_safe: bool = True,
) -> tuple[str, bool]:
    if not body:
        return body, False
    total_limit = budget.char_limit if budget.explicit else budget.token_limit
    body_limit = max(0, total_limit - reserved)
    over = _measure(body, explicit=budget.explicit) > body_limit
    limit_ok = lambda value: _measure(value, explicit=budget.explicit) <= body_limit
    if not over:
        return body, False

    lo, hi = 0, min(len(body), budget.char_limit)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if limit_ok(body[:mid]):
            lo = mid
        else:
            hi = mid - 1
    fitted = body[:lo].rstrip()
    if line_safe:
        boundary = fitted.rfind("\n")
        if boundary < 0:
            return "", True
        fitted = fitted[:boundary].rstrip()
    return fitted, True


def _fit_keeping_both_ends(
    body: str, budget: ResultBudget, *, reserved: int
) -> tuple[str, bool]:
    """砍中段、保留頭尾。preflight 報告專用。

    preflight 的報告**就是判斷依據本身**：頭是「超出了哪一項」，尾是三種處理
    方式與那條可以直接複製的 CLI 命令。一般的尾端截斷會把後半整段砍掉 ——
    使用者拿到一份看得到問題、卻看不到怎麼辦的報告，那比截斷更糟。
    """
    total_limit = budget.char_limit if budget.explicit else budget.token_limit
    body_limit = max(0, total_limit - reserved)
    if _measure(body, explicit=budget.explicit) <= body_limit:
        return body, False

    marker = "\n...[中段已截斷:preflight 報告的頭尾都是判斷依據]\n"
    lines = body.split("\n")
    head: list[str] = []
    tail: list[str] = []
    marker_cost = _measure(marker, explicit=budget.explicit)
    used = marker_cost
    front, back = 0, len(lines) - 1
    take_front = True
    while front <= back:
        line = lines[front] if take_front else lines[back]
        cost = _measure(line + "\n", explicit=budget.explicit)
        if used + cost > body_limit:
            break
        used += cost
        if take_front:
            head.append(line)
            front += 1
        else:
            tail.append(line)
            back -= 1
        take_front = not take_front
    if not head and not tail:
        return "", True
    return "\n".join(head) + marker + "\n".join(reversed(tail)), True


def _fit_review_figures_body(
    body: str, budget: ResultBudget, *, reserved: int
) -> tuple[str, bool]:
    """Keep whole figure blocks or replace the whole canonical payload."""
    total_limit = budget.char_limit if budget.explicit else budget.token_limit
    body_limit = max(0, total_limit - reserved)
    if _measure(body, explicit=budget.explicit) <= body_limit:
        return body, False
    if not body.startswith("── figure_id:"):
        return _fit_body(body, budget, reserved=reserved)

    blocks = re.split(r"(?m)(?=^── figure_id:)", body)
    kept: list[str] = []
    for block in blocks:
        if not block:
            continue
        candidate = "\n".join(kept + [block])
        if _measure(candidate, explicit=budget.explicit) <= body_limit:
            kept.append(block)
            continue
        payload_at = block.find("\n   payload")
        if payload_at >= 0:
            safe = block[:payload_at] + (
                "\n   canonical data omitted whole by context budget; narrow figure_id "
                "or read evidence_ref)"
            )
            candidate = "\n".join(kept + [safe])
            if _measure(candidate, explicit=budget.explicit) <= body_limit:
                kept.append(safe)
        break
    return "\n".join(kept).rstrip(), True


def adapt_tool_result(
    tool_name: str, core_payload: object, *, budget: ResultBudget
) -> types.CallToolResult:
    body = _render_payload(tool_name, core_payload)
    status, next_step = _status_for(tool_name, core_payload, body)

    def prefix_lines(current_status: str, current_next: str | None) -> list[str]:
        result = [f"status: {current_status}"]
        if current_next is not None:
            result.append(f"next: {current_next}")
        if budget.context_risk:
            result.append("context_risk: explicit max_chars exceeds the default 12% context budget")
        return result

    initial = prefix_lines(status, next_step)
    initial_text = "\n".join(initial + ([body] if body else []))
    total_limit = budget.char_limit if budget.explicit else budget.token_limit
    budget_truncated = _measure(initial_text, explicit=budget.explicit) > total_limit
    if budget_truncated:
        if status != "error":
            status = "partial"
        next_step = next_step or _narrowing_step(tool_name)
        suffix = "[result truncated by context budget]"
        full_body = body
        lines = prefix_lines(status, next_step)
        for _pass in range(2):
            shell = "\n".join(lines + ["", suffix])
            reserved = _measure(shell, explicit=budget.explicit)
            if tool_name == "review_figures":
                body, _ = _fit_review_figures_body(full_body, budget, reserved=reserved)
            elif ingest_notify.ZERO_WRITE_MARKER in full_body:
                body, _ = _fit_keeping_both_ends(full_body, budget, reserved=reserved)
            else:
                body, _ = _fit_body(full_body, budget, reserved=reserved)
            if tool_name != "read_file":
                break
            visible_lines = re.findall(r"(?m)^\s*(\d+)\s+\|", body)
            if not visible_lines:
                break
            next_step = (
                f"Use read_file with start_line={int(visible_lines[-1]) + 1} "
                "to continue without gaps."
            )
            lines = prefix_lines(status, next_step)
        if body:
            lines.append(body)
        lines.append(suffix)
    else:
        lines = initial
        if body:
            lines.append(body)

    structured = None
    if tool_name == "code_rag_search":
        # FastMCP wraps list return annotations in its public {"result": [...]}
        # output schema; CallToolResult.structuredContent itself must be an object.
        structured = {"result": core_payload}
    elif tool_name in EVIDENCE_TOOL_NAMES:
        structured = core_payload
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="\n".join(lines))],
        structuredContent=structured,
        isError=status == "error",
    )


def adapt_tool_error(
    tool_name: str, error: Exception, *, budget: ResultBudget
) -> types.CallToolResult:
    """Keep exception repair guidance in the text lane and schema-compatible."""
    message = f"{type(error).__name__}: {error}"
    # busy 不是「輸入或環境有問題」:那次呼叫**根本沒有執行**,而正確的下一步是
    # 等 ingest 結束再查(那時 KB 才是新版)。給通用的「修正後立即重試一次」會讓
    # 模型把唯一一次重試耗在 ingest 還沒結束的時候,然後放棄查詢。
    if str(error).lstrip().startswith(ingest_notify.BUSY_PREFIX):
        next_line = ("Wait for the in-flight ingest to finish (its result comes back "
                     "to the caller), then query again; this call did not run.")
    elif getattr(error, "readonly_refusal", False):
        # readonly server 的拒絕不是「輸入有問題」:重試同一個呼叫永遠是同一個
        # 結果。給「修正後重試一次」會讓模型把那一次重試白白花掉。
        next_line = ("This server refuses state-changing tools (read-only instance); "
                     "do not retry this call, gather evidence with read-only tools instead.")
    elif isinstance(error, PermissionError):
        # 一般檔案系統的 EACCES:與 readonly instance 無關,別把模型引去「放棄寫入工具」。
        next_line = ("The filesystem denied access to that path; pick a path this project "
                     "may read or write (or ask the user), then retry once.")
    else:
        next_line = "Correct the reported input or environment problem, then retry once."
    prefix = f"status: error\nnext: {next_line}\n"
    available = max(
        0,
        (budget.char_limit if budget.explicit else budget.token_limit)
        - _measure(prefix, explicit=budget.explicit),
    )
    error_budget = ResultBudget(
        token_limit=available if not budget.explicit else budget.token_limit,
        char_limit=available if budget.explicit else budget.char_limit,
        explicit=budget.explicit,
        context_risk=budget.context_risk,
    )
    message, _ = _fit_body(message, error_budget)
    structured: object | None
    if tool_name == "code_rag_search":
        # Its generated outputSchema is the wrapped list shape.
        structured = {"result": []}
    elif tool_name in EVIDENCE_TOOL_NAMES:
        structured = {"error": f"{type(error).__name__}: {error}"}
    else:
        structured = None
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=prefix + message)],
        structuredContent=structured,
        isError=True,
    )
