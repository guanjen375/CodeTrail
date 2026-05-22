#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
context_budget — CodeTrail 內部對 llama.cpp llama-server 呼叫的 context 觀測
+ 預算 + 硬性 gate。

設計重點:
1. context overflow 是正確性問題,不是只是速度問題。所以這裡的硬性 gate 會
   直接拒絕送出超出 effective_num_ctx*hard_threshold 的 prompt。
2. 攔 CodeTrail 自己送出去的 /completion 與 /v1/chat/completions 兩條路。
   OpenCode TUI 直接打 llama-server,不會經過這裡。
3. token 估算先用 CHARS_PER_TOKEN heuristic;llama-server 回的
   tokens_evaluated / usage.prompt_tokens 會被收下來給下一次校正(下版)。
4. telemetry 只寫 metadata(count、模型名、context 設定、速度、是否 trim 等),
   絕不寫 prompt / tool output / 檔案內容,以防 NDA / private repo 外洩。
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

import config


# ============================================================
# Dataclasses
# ============================================================

@dataclass
class ContextUsage:
    """單次 LLM call 的 context 使用快照。同時供 CLI 顯示與 JSONL log。

    所有欄位都是 metadata,不含任何 prompt / tool output / 檔案內容。
    """
    model: str = ""
    source: str = ""  # "generate" | "chat" | "agent_tools" | "strict_check" | ...
    requested_num_ctx: int = 0
    effective_num_ctx: int = 0
    dynamic_ctx_enabled: bool = False
    dynamic_ctx_min: int = 0
    dynamic_ctx_max: int = 0
    estimated_input_tokens: int = 0
    reserved_output_tokens: int = 0
    estimated_total_tokens: int = 0
    utilization_pct: float = 0.0  # estimated_total / effective_num_ctx
    soft_warning: bool = False
    hard_overflow: bool = False
    message_count: int = 0
    tool_message_count: int = 0
    total_chars: int = 0
    char_per_token_estimate: float = 0.0
    did_trim: bool = False
    trim_summary: dict[str, Any] = field(default_factory=dict)
    # Filled in after the response comes back (llama-server returns these in
    # the response JSON for non-streaming, or in the final chunk for streaming).
    actual_prompt_eval_count: int | None = None
    actual_eval_count: int | None = None
    prompt_tokens_per_second: float | None = None
    output_tokens_per_second: float | None = None
    # When the gate refuses, we still want to log the attempt for visibility.
    error_type: str | None = None
    timestamp: float = field(default_factory=time.time)

    def to_log_dict(self) -> dict[str, Any]:
        """轉成 JSON-safe dict 用於 JSONL log。

        嚴格只記 metadata。所有可能含 prompt / 檔案內容的欄位都不會出現。
        """
        d = asdict(self)
        # trim_summary 是結構化 metadata(類型/長度/數量),不含原文。
        return d


# Backwards-compatible alias — the original spec calls this ContextBudget too.
ContextBudget = ContextUsage


# ============================================================
# Token estimation
# ============================================================

def _chars_per_token() -> float:
    return float(getattr(config, "CHARS_PER_TOKEN", 3.5) or 3.5)


def _count_message_content_chars(content: Any) -> int:
    """Count chars across both native and OpenAI-style message content shapes.

    Supports:
        - plain str
        - list[dict] with {"type": "text", "text": "..."} or {"text": "..."}
        - list[dict] with image-ish parts ({"type": "image", ...}) — counted
          as a small fixed token-equivalent so the budget reflects them.
        - dict (rare; some clients pass {"text": "..."} directly)
    """
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, dict):
        # Generic dict — pick out anything that looks like text.
        text = content.get("text")
        if isinstance(text, str):
            return len(text)
        return 0
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, str):
                total += len(part)
                continue
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "image" or "image" in part:
                # Rough placeholder: a vision-style image part roughly costs a
                # few hundred tokens on most local VLMs. Use chars-equivalent
                # so the heuristic stays internally consistent.
                total += int(256 * _chars_per_token())
                continue
            text = part.get("text") or part.get("content")
            if isinstance(text, str):
                total += len(text)
        return total
    # Unknown shape — stringify defensively but don't crash.
    try:
        return len(str(content))
    except Exception:
        return 0


def estimate_message_chars(messages: list[dict]) -> tuple[int, int, int]:
    """Return (total_chars, message_count, tool_message_count)."""
    total = 0
    tool_count = 0
    for m in messages or []:
        total += _count_message_content_chars(m.get("content"))
        # Tool calls / tool name / arguments JSON also occupy context.
        tool_calls = m.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                try:
                    total += len(json.dumps(tc, ensure_ascii=False))
                except (TypeError, ValueError):
                    pass
        if m.get("role") == "tool":
            tool_count += 1
            tname = m.get("tool_name") or m.get("name")
            if isinstance(tname, str):
                total += len(tname)
    return total, len(messages or []), tool_count


def estimate_tools_chars(tools: list[dict] | None) -> int:
    """Estimate the JSON serialization length of the tools schema.

    llama-server forwards the tools schema to the model on every chat call, so
    it eats real context. We use the JSON string length, divided later by
    CHARS_PER_TOKEN, as a rough estimate.
    """
    if not tools:
        return 0
    try:
        return len(json.dumps(tools, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def chars_to_tokens(chars: int) -> int:
    return int(chars / max(_chars_per_token(), 0.1))


def estimate_tokens(
    *,
    prompt: str | None = None,
    messages: list[dict] | None = None,
    tools: list[dict] | None = None,
) -> tuple[int, int]:
    """Return (estimated_tokens, total_chars).

    Use this anywhere you need to predict prompt size before the request.
    """
    chars = 0
    if prompt:
        chars += len(prompt)
    if messages:
        msg_chars, _, _ = estimate_message_chars(messages)
        chars += msg_chars
    chars += estimate_tools_chars(tools)
    return chars_to_tokens(chars), chars


# ============================================================
# Building a ContextUsage from a request
# ============================================================

def build_usage(
    *,
    source: str,
    requested_num_ctx: int,
    prompt: str | None = None,
    messages: list[dict] | None = None,
    tools: list[dict] | None = None,
    model: str | None = None,
    did_trim: bool = False,
    trim_summary: dict[str, Any] | None = None,
) -> ContextUsage:
    """Compute a ContextUsage snapshot for a pending request.

    `requested_num_ctx` 是 CodeTrail dynamic_ctx 計算後想用的上限。
    llama-server 不接受 per-call num_ctx — 它的真實 ctx 在 server 啟動時
    用 `-c N` 鎖死,這裡的 requested 只是 CodeTrail 自己 budget 用的相對值。
    gate 仍照 requested == effective 算,跟模型實際 ctx 是否對齊由 doctor 報。
    """
    msg_chars = 0
    msg_count = 0
    tool_count = 0
    if messages:
        msg_chars, msg_count, tool_count = estimate_message_chars(messages)

    prompt_chars = len(prompt) if prompt else 0
    tools_chars = estimate_tools_chars(tools)
    total_chars = prompt_chars + msg_chars + tools_chars

    cpt = _chars_per_token()
    est_in = int(total_chars / max(cpt, 0.1))
    reserved = int(getattr(config, "RESERVED_OUTPUT_TOKENS", 4096) or 0)
    est_total = est_in + reserved

    effective_ctx = max(int(requested_num_ctx or 0), 1)
    util = est_total / effective_ctx

    soft = float(getattr(config, "CTX_SOFT_THRESHOLD", 0.80) or 0.0)
    hard = float(getattr(config, "CTX_HARD_THRESHOLD", 0.90) or 0.0)

    return ContextUsage(
        model=str(model or getattr(config, "MODEL", "") or ""),
        source=source,
        requested_num_ctx=int(requested_num_ctx or 0),
        effective_num_ctx=effective_ctx,
        dynamic_ctx_enabled=bool(getattr(config, "DYNAMIC_NUM_CTX_ENABLED", False)),
        dynamic_ctx_min=int(getattr(config, "DYNAMIC_NUM_CTX_MIN", 0) or 0),
        dynamic_ctx_max=int(getattr(config, "DYNAMIC_NUM_CTX_MAX", 0) or 0),
        estimated_input_tokens=est_in,
        reserved_output_tokens=reserved,
        estimated_total_tokens=est_total,
        utilization_pct=round(util * 100.0, 2),
        soft_warning=util >= soft and util < hard,
        hard_overflow=util >= hard,
        message_count=msg_count,
        tool_message_count=tool_count,
        total_chars=total_chars,
        char_per_token_estimate=cpt,
        did_trim=bool(did_trim),
        trim_summary=dict(trim_summary or {}),
    )


# ============================================================
# Hard gate
# ============================================================

class ContextOverflowError(RuntimeError):
    """Raised when prompt size exceeds the configured hard threshold.

    Callers translate this into the user-facing [CTX_OVERFLOW] message via
    `overflow_message()` and return it through their normal error path.
    """

    def __init__(self, usage: ContextUsage):
        self.usage = usage
        super().__init__(overflow_message(usage))


def overflow_message(usage: ContextUsage) -> str:
    hard_pct = int(round(float(getattr(config, "CTX_HARD_THRESHOLD", 0.90)) * 100))
    soft_pct = int(round(float(getattr(config, "CTX_SOFT_THRESHOLD", 0.80)) * 100))
    return (
        "[CTX_OVERFLOW] estimated input "
        f"{usage.estimated_input_tokens} + reserved output "
        f"{usage.reserved_output_tokens} exceeds effective context "
        f"{usage.effective_num_ctx} (hard={hard_pct}%, soft={soft_pct}%, "
        f"use={usage.utilization_pct:.0f}%). "
        "Refusing to send to avoid silent truncation.\n"
        "  How to fix:\n"
        "  - 縮小問題範圍 / 拆成多步\n"
        "  - 減少 tool output（read_file 指定行範圍、grep 縮小 pattern）\n"
        "  - 降低 AICODE_NUM_CTX 上限會讓 dynamic clamp 更早觸發,反而更明顯。\n"
        "    要的是更大上限請調 DYNAMIC_NUM_CTX_MAX,並確認 llama-server 啟動時\n"
        "    的 -c <N> 也夠大(server 啟動後 ctx 即固定,改 env 沒用)。\n"
        "  - RAG 太多 REF：縮小 KNOWLEDGE_TOP_K 或讓 query 更具體\n"
        "  - 設定 AICODE_RESERVED_OUTPUT_TOKENS 較小（預設 4096）若你只需要短回答"
    )


def enforce_gate(usage: ContextUsage) -> None:
    """Raise ContextOverflowError if utilization is over the hard threshold.

    Caller is responsible for logging the (refused) usage before re-raising
    or for converting the exception into a structured CLI / TUI error.
    """
    if not bool(getattr(config, "CTX_GATE_ENABLED", True)):
        return
    if usage.hard_overflow:
        usage.error_type = "ctx_overflow"
        raise ContextOverflowError(usage)


# ============================================================
# llama.cpp usage metrics capture
# ============================================================
# 兩種 endpoint 兩種欄位,加上 OpenAI /v1 兩種,在這層統一吸收掉:
#   - native /completion        → tokens_evaluated / tokens_predicted + timings{}
#   - /v1/chat/completions      → usage.{prompt_tokens, completion_tokens} (+ timings)


def parse_usage_from_response(data: dict, usage: ContextUsage) -> None:
    """Pull prompt / output token counts off a non-streaming response.

    支援 llama-server native /completion 與 OpenAI-compat /v1/chat/completions
    兩種 shape;欄位缺就維持 None,不假裝有資料。
    """
    if not isinstance(data, dict):
        return

    pec = data.get("tokens_evaluated")
    ec = data.get("tokens_predicted")

    if pec is None or ec is None:
        u = data.get("usage")
        if isinstance(u, dict):
            if pec is None and isinstance(u.get("prompt_tokens"), (int, float)):
                pec = u.get("prompt_tokens")
            if ec is None and isinstance(u.get("completion_tokens"), (int, float)):
                ec = u.get("completion_tokens")

    if isinstance(pec, (int, float)):
        usage.actual_prompt_eval_count = int(pec)
    if isinstance(ec, (int, float)):
        usage.actual_eval_count = int(ec)

    timings = data.get("timings")
    if isinstance(timings, dict):
        if isinstance(timings.get("prompt_per_second"), (int, float)):
            usage.prompt_tokens_per_second = float(timings["prompt_per_second"])
        if isinstance(timings.get("predicted_per_second"), (int, float)):
            usage.output_tokens_per_second = float(timings["predicted_per_second"])


def parse_usage_from_stream_chunk(chunk: dict, usage: ContextUsage) -> None:
    """Inspect a streaming chunk and pull usage metrics if it's the final chunk.

    最後一個 chunk 在不同 endpoint 上的訊號不同:
      - native /completion        → `stop: true`
      - /v1/chat/completions      → `choices[0].finish_reason` 非 null
    """
    if not isinstance(chunk, dict):
        return
    is_final = bool(chunk.get("stop"))
    if not is_final:
        choices = chunk.get("choices")
        if isinstance(choices, list) and choices:
            if choices[0].get("finish_reason"):
                is_final = True
    if not is_final:
        return
    parse_usage_from_response(chunk, usage)


# ============================================================
# CLI lines
# ============================================================

def format_ctx_line(usage: ContextUsage) -> str:
    return (
        f"[CTX] model={usage.model} ctx={usage.effective_num_ctx} "
        f"est_in={usage.estimated_input_tokens} "
        f"reserve={usage.reserved_output_tokens} "
        f"use={usage.utilization_pct:.0f}% source={usage.source}"
    )


def format_actual_line(usage: ContextUsage) -> str | None:
    if usage.actual_prompt_eval_count is None and usage.actual_eval_count is None:
        return None
    pec = usage.actual_prompt_eval_count if usage.actual_prompt_eval_count is not None else "?"
    ec = usage.actual_eval_count if usage.actual_eval_count is not None else "?"
    p_tps = f"{usage.prompt_tokens_per_second:.0f}" if usage.prompt_tokens_per_second else "?"
    o_tps = f"{usage.output_tokens_per_second:.0f}" if usage.output_tokens_per_second else "?"
    return (
        f"[CTX] actual_in={pec} eval={ec} "
        f"prompt={p_tps} tok/s output={o_tps} tok/s"
    )


_OFFLOAD_CHECK_FIRED = False


def _emit_runtime_offload_check_once() -> None:
    """Soft/hard threshold 觸發時順手查 llama-server /slots + /props,看
    server 自己的 n_ctx 和 slot 狀態。一個 process 只查一次,避免每個 call
    都打 HTTP。

    任何 import / I/O 失敗都靜默吞掉:這層是輔助診斷,絕不能擋使用者。
    """
    global _OFFLOAD_CHECK_FIRED
    if _OFFLOAD_CHECK_FIRED:
        return
    _OFFLOAD_CHECK_FIRED = True
    try:
        import gpu_safety
        base_url = getattr(config, "LLAMA_BASE_URL", "http://localhost:8080")
        status = gpu_safety.runtime_offload_check(base_url)
    except Exception:
        return
    if not status.available:
        return
    print(f"[CTX] runtime: {status.short()}", flush=True)


def emit_pre_call_lines(usage: ContextUsage) -> None:
    """Print [CTX] lines around a call. Quiet by default for low-risk calls
    so simple chats stay clean; warn/overflow always print.
    """
    if usage.hard_overflow:
        # The caller will also surface the structured overflow message.
        print(overflow_message(usage), file=sys.stderr, flush=True)
        _emit_runtime_offload_check_once()
        return
    if usage.soft_warning:
        print(format_ctx_line(usage), flush=True)
        print(
            f"[CTX] WARNING use={usage.utilization_pct:.0f}%; "
            "consider trimming tool outputs or narrowing the question",
            flush=True,
        )
        _emit_runtime_offload_check_once()
        return
    # Below the soft threshold — still print the budget line at INFO level
    # if utilization is non-trivial. Below 25% we stay silent to keep simple
    # Q&A flow uncluttered.
    if usage.utilization_pct >= 25.0:
        print(format_ctx_line(usage), flush=True)


def emit_post_call_line(usage: ContextUsage) -> None:
    line = format_actual_line(usage)
    if line:
        print(line, flush=True)


# ============================================================
# JSONL metrics log
# ============================================================

_LOG_LOCK = Lock()


def _resolve_log_path() -> Path | None:
    path_str = getattr(config, "CTX_METRICS_PATH", None)
    if not path_str:
        return None
    try:
        return Path(path_str).expanduser()
    except Exception:
        return None


def log_metrics(usage: ContextUsage) -> None:
    """Append a single JSON line for this LLM call.

    Strictly metadata: counts, model name, context settings, speed, trim flags.
    No prompt text, no tool output, no file content.
    """
    if not bool(getattr(config, "CTX_METRICS_ENABLED", True)):
        return
    path = _resolve_log_path()
    if path is None:
        return
    try:
        with _LOG_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(usage.to_log_dict(), ensure_ascii=False) + "\n")
    except OSError:
        # Telemetry must never break the user's flow.
        pass


# ============================================================
# Convenience helpers
# ============================================================

def check_and_log(
    *,
    source: str,
    requested_num_ctx: int,
    prompt: str | None = None,
    messages: list[dict] | None = None,
    tools: list[dict] | None = None,
    model: str | None = None,
    did_trim: bool = False,
    trim_summary: dict[str, Any] | None = None,
    emit: bool = True,
) -> ContextUsage:
    """Compute usage, enforce the hard gate, emit CLI lines, log to JSONL.

    Returns the ContextUsage (with metrics not yet filled in) on success.
    Raises ContextOverflowError if the request would overflow the hard
    threshold (after logging the refused attempt for observability).
    """
    usage = build_usage(
        source=source,
        requested_num_ctx=requested_num_ctx,
        prompt=prompt,
        messages=messages,
        tools=tools,
        model=model,
        did_trim=did_trim,
        trim_summary=trim_summary,
    )
    if emit:
        emit_pre_call_lines(usage)
    try:
        enforce_gate(usage)
    except ContextOverflowError:
        log_metrics(usage)
        raise
    return usage
