#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trim — explainable, priority-aware trimming for messages sent to Ollama.

Design goals:
1. Every drop / shrink leaves an explicit marker in the content so neither
   the model nor a future reviewer can mistake a trimmed message for an
   accurate one. Two markers are used:
     - [CTX_TRIMMED] ... — content was cut, with original/kept char counts.
     - [TOOL_SUMMARY] ... [/TOOL_SUMMARY] — older tool output collapsed
       into a deterministic, fact-only summary.
2. Tool-specific strategies. run_command keeps the tail + error lines
   because that's where pytest / make / ninja put the actual failure.
   read_file tries to preserve a window around the requested location.
   List-like outputs get aggressively compressed.
3. Priority tiers based on which tool produced the output. The agent must
   never accidentally drop a stack-trace anchor or a high-confidence REF
   block before dropping a generic list_dir blob.

This module does not know about num_ctx; it only sees the message list and
a char budget. The caller (agent loop) is responsible for picking the
budget — usually MAX_MESSAGES_BUDGET, but sometimes a tighter value derived
from the context_budget gate when soft warning fires.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import config


# ============================================================
# Markers
# ============================================================
CTX_TRIMMED_MARKER = "[CTX_TRIMMED]"
TOOL_SUMMARY_OPEN = "[TOOL_SUMMARY]"
TOOL_SUMMARY_CLOSE = "[/TOOL_SUMMARY]"


# ============================================================
# Priorities
# ============================================================
# 1 = highest — file/line anchored evidence. Drop last.
# 2 = test / command output — keep tail + error lines.
# 3 = generic / list-y. Drop first.
PRI_EVIDENCE = 1
PRI_COMMAND = 2
PRI_GENERIC = 3


# Heuristics: pattern → priority. Used when we don't know the originating
# tool (e.g. message["content"] was prefixed by agent code).
_HIGH_PRIORITY_TOOLS = {"read_file", "grep", "find", "list_files"}
_COMMAND_TOOLS = {"run_command", "run_tests"}


# ============================================================
# Telemetry shape
# ============================================================

@dataclass
class TrimSummary:
    """What got trimmed, in metadata form. Caller will attach this to
    ContextUsage.trim_summary so JSONL telemetry stays prompt-free."""
    summarized_tool_outputs: int = 0
    truncated_tool_outputs: int = 0
    run_command_tail_kept: int = 0
    read_file_window_kept: int = 0
    chars_before: int = 0
    chars_after: int = 0
    rounds: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summarized": self.summarized_tool_outputs,
            "truncated": self.truncated_tool_outputs,
            "run_command_tail_kept": self.run_command_tail_kept,
            "read_file_window_kept": self.read_file_window_kept,
            "chars_before": self.chars_before,
            "chars_after": self.chars_after,
            "rounds": self.rounds,
            "details": self.details,
        }


# ============================================================
# Helpers
# ============================================================

def _calc_messages_size(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, str):
            total += len(c)
    return total


def _resolve_tool_name(messages: list[dict], tool_idx: int) -> str:
    """Walk backwards from a 'tool' message to find the assistant
    tool_call that produced it. Returns "" if not found."""
    msg = messages[tool_idx]
    name = msg.get("tool_name") or msg.get("name")
    if isinstance(name, str) and name:
        return name
    for i in range(tool_idx - 1, -1, -1):
        prev = messages[i]
        if prev.get("role") == "assistant" and prev.get("tool_calls"):
            tcs = prev.get("tool_calls", []) or []
            if tcs:
                fn = tcs[0].get("function", {})
                got = fn.get("name", "")
                if isinstance(got, str):
                    return got
            break
    return ""


def _priority_for_tool(tool_name: str) -> int:
    if tool_name in _HIGH_PRIORITY_TOOLS:
        return PRI_EVIDENCE
    if tool_name in _COMMAND_TOOLS:
        return PRI_COMMAND
    return PRI_GENERIC


# ============================================================
# Per-tool trim strategies
# ============================================================

# Error-ish lines we always try to keep in command output, even at low budgets.
# Mirrors config.RUN_COMMAND_ERROR_PATTERNS but as compiled regexes so we can
# scan tail-first quickly.
_DEFAULT_ERROR_PATTERNS = [
    r"\bFAIL(?:ED)?\b",
    r"\bERROR\b",
    r"\berror:\s",
    r"Traceback",
    r"Exception\b",
    r"AssertionError\b",
    r"\bexpected\b",
    r"\bactual\b",
    r"\bassert\b",
    r"^E\s",  # pytest E lines
    r"PASS(?:ED)?\b",
    r"SKIPPED\b",
]


def _error_regexes() -> list[re.Pattern[str]]:
    pats = getattr(config, "RUN_COMMAND_ERROR_PATTERNS", None)
    sources = list(pats) if pats else _DEFAULT_ERROR_PATTERNS
    out: list[re.Pattern[str]] = []
    for p in sources:
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error:
            continue
    return out


def trim_run_command_output(text: str, max_chars: int) -> tuple[str, dict[str, Any]]:
    """Keep head, tail, and any line matching an error pattern.

    Tests / build commands put the actual failure at the *bottom*, so cutting
    from the front blindly is the worst possible heuristic. Strategy:
        1. If text fits, return as-is.
        2. Otherwise keep first `head` chars, last `tail` chars, and any
           line in the middle matching an error pattern.
        3. Insert a [CTX_TRIMMED] marker on each break and report metadata.
    """
    original = len(text)
    if original <= max_chars:
        return text, {"kept": original, "original": original, "trimmed": False}

    # Reserve ~70% of budget for the tail (errors live there), ~25% head.
    # Leave room for error-line callouts and the marker itself.
    tail_ratio = float(getattr(config, "RUN_COMMAND_TAIL_RATIO", 0.7) or 0.7)
    tail_ratio = max(0.4, min(0.9, tail_ratio))
    tail_budget = int(max_chars * tail_ratio)
    head_budget = max_chars - tail_budget - 200  # 200 chars for markers + error lines
    head_budget = max(0, head_budget)

    head = text[:head_budget] if head_budget else ""
    tail = text[-tail_budget:] if tail_budget else ""

    # Pull a small number of error-bearing middle lines that the head/tail
    # didn't already cover.
    middle = text[head_budget : original - tail_budget]
    error_lines: list[str] = []
    if middle:
        regexes = _error_regexes()
        for line in middle.splitlines():
            for rx in regexes:
                if rx.search(line):
                    error_lines.append(line)
                    break
            if len(error_lines) >= 8:
                break

    parts = []
    if head:
        parts.append(head.rstrip())
        parts.append(
            f"\n{CTX_TRIMMED_MARKER} run_command output: middle omitted, "
            f"original_chars={original} kept_head={len(head)} kept_tail={len(tail)} "
            f"reason=budget"
        )
    if error_lines:
        parts.append(
            "\n[CTX_TRIMMED_ERROR_LINES] preserved error/test lines from middle:"
        )
        parts.extend(f"    {ln}" for ln in error_lines)
    if tail:
        if not head:
            parts.append(
                f"{CTX_TRIMMED_MARKER} run_command output: head omitted, "
                f"original_chars={original} kept_tail={len(tail)} reason=budget"
            )
        parts.append(tail.lstrip("\n"))

    result = "\n".join(p for p in parts if p)
    meta = {
        "kept": len(result),
        "original": original,
        "tail_kept": len(tail),
        "head_kept": len(head),
        "error_lines_kept": len(error_lines),
        "trimmed": True,
    }
    return result, meta


# read_file output from agent_tools.read_file looks like:
#     "檔案: agent.py 行 100-200 / 共 500 行\n<content>\n"
# We try to detect that header so the user knows which window survived.
_READ_FILE_HEADER_RE = re.compile(
    r"^(?P<header>.*?行\s*(?P<start>\d+)\s*-\s*(?P<end>\d+)\s*/\s*共\s*(?P<total>\d+)\s*行.*?)\n",
    re.DOTALL,
)


def trim_read_file_output(text: str, max_chars: int) -> tuple[str, dict[str, Any]]:
    """Preserve the read_file header (path + line range) and as much of the
    body as fits. Mark omissions with [CTX_TRIMMED]."""
    original = len(text)
    if original <= max_chars:
        return text, {"kept": original, "original": original, "trimmed": False}

    m = _READ_FILE_HEADER_RE.match(text)
    header = m.group("header") if m else ""
    body = text[m.end():] if m else text

    # Budget body to remaining space after header + marker overhead
    overhead = len(header) + 220 if header else 220
    body_budget = max(200, max_chars - overhead)

    if len(body) <= body_budget:
        # Header was huge or body was tiny; just hard-cut from the top of the
        # whole text.
        result = (
            f"{CTX_TRIMMED_MARKER} read_file output truncated from start; "
            f"original_chars={original} kept_chars={max_chars} reason=budget\n"
            + text[-max_chars + 200:]
        )
        return result, {"kept": len(result), "original": original, "trimmed": True, "window_kept": False}

    # Keep front portion of the body (which is where line N starts). For very
    # large files the user usually asked for a specific window already, so
    # the head of `body` IS the window.
    kept = body[:body_budget]
    truncated = len(body) - body_budget
    parts = []
    if header:
        parts.append(header.rstrip())
    parts.append(
        f"{CTX_TRIMMED_MARKER} read_file: body trimmed, "
        f"original_chars={original} kept_chars={len(kept)} omitted_chars={truncated} "
        f"reason=budget"
    )
    parts.append(kept.rstrip())
    parts.append(
        f"{CTX_TRIMMED_MARKER} ... {truncated} more chars omitted. "
        f"Re-call read_file with a narrower line range if needed."
    )
    result = "\n".join(parts)
    return result, {
        "kept": len(result),
        "original": original,
        "trimmed": True,
        "window_kept": True,
    }


def summarize_old_tool_output(text: str, tool_name: str) -> tuple[str, dict[str, Any]]:
    """Collapse an older tool output into a deterministic, fact-only summary.

    No LLM is invoked. We extract the cheap-to-compute anchors that the model
    might still need: file:line mentions, error lines (for command output),
    read_file headers, and approximate counts. The rest is dropped with a
    [TOOL_SUMMARY] block.
    """
    original = len(text)
    if not text:
        return text, {"kept": 0, "original": 0, "trimmed": False}

    facts: list[str] = []

    # 0) read_file header (file + line range) — must survive so the model
    #    can see WHICH file/window the (now-summarized) content came from.
    header_m = _READ_FILE_HEADER_RE.match(text)
    if header_m:
        facts.append(f"- file={header_m.group('header').strip()}")

    # 1) file:line anchors anywhere in the text (the agent often follows up
    #    on these on the next loop).
    file_line_hits = re.findall(
        r"\b([A-Za-z0-9_./\-]+\.(?:py|c|cpp|cc|h|hpp|go|rs|js|ts|tsx|jsx|java|kt|sh|md)):(\d+)",
        text,
    )
    seen: set[tuple[str, str]] = set()
    for f, ln in file_line_hits[:20]:
        if (f, ln) in seen:
            continue
        seen.add((f, ln))
        facts.append(f"- ref {f}:{ln}")

    # 2) error lines (for run_command-shaped outputs)
    if tool_name in _COMMAND_TOOLS or tool_name == "":
        regexes = _error_regexes()
        for line in text.splitlines():
            for rx in regexes:
                if rx.search(line):
                    facts.append(f"- err: {line.strip()[:140]}")
                    break
            if len(facts) > 30:
                break

    # 3) very short trailing snippet (the bottom of the original — useful for
    #    test summaries like "FAILED tests/test_foo.py::bar - AssertionError")
    trail = text.rstrip().splitlines()[-3:]
    trail_str = "\n".join(f"    {ln}" for ln in trail if ln.strip())

    summary_lines = [
        f"{TOOL_SUMMARY_OPEN}",
        f"tool={tool_name or 'unknown'}",
        f"omitted_chars={original}",
        f"kept=facts_only",
    ]
    if facts:
        summary_lines.append("facts:")
        summary_lines.extend(facts[:30])
    if trail_str:
        summary_lines.append("tail:")
        summary_lines.append(trail_str)
    summary_lines.append(TOOL_SUMMARY_CLOSE)
    summary = "\n".join(summary_lines)
    return summary, {
        "kept": len(summary),
        "original": original,
        "trimmed": True,
        "facts_extracted": len(facts),
    }


# ============================================================
# Per-message trim entrypoint
# ============================================================

def trim_tool_message(
    content: str, tool_name: str, max_chars: int, *, mode: str = "auto"
) -> tuple[str, dict[str, Any]]:
    """Pick the right strategy based on `tool_name` and call it.

    mode:
        "auto" — pick based on tool_name and length.
        "summarize" — force the deterministic [TOOL_SUMMARY] form (used for
                      older tool outputs once newer ones exist).
    """
    if mode == "summarize":
        return summarize_old_tool_output(content, tool_name)

    if tool_name in _COMMAND_TOOLS:
        return trim_run_command_output(content, max_chars)
    if tool_name in _HIGH_PRIORITY_TOOLS:
        return trim_read_file_output(content, max_chars)

    # Generic / unknown tool: cut from the middle, keep head + tail, with
    # an explicit marker.
    if len(content) <= max_chars:
        return content, {"kept": len(content), "original": len(content), "trimmed": False}
    head_budget = max_chars // 3
    tail_budget = max_chars - head_budget - 120
    result = (
        content[:head_budget].rstrip()
        + f"\n{CTX_TRIMMED_MARKER} generic output trimmed; "
        f"original_chars={len(content)} kept_chars={max_chars} "
        f"tool={tool_name or 'unknown'} reason=budget\n"
        + content[-tail_budget:].lstrip("\n")
    )
    return result, {
        "kept": len(result),
        "original": len(content),
        "trimmed": True,
    }


# ============================================================
# Whole-list orchestrator
# ============================================================

# Per-priority preferred maximum size for an individual tool message in the
# "still recent enough to keep raw" tier. Older tool outputs (beyond
# MIN_RECENT_TOOL_OUTPUTS) get summarized regardless of size.
_RAW_MAX_BY_PRIORITY = {
    PRI_EVIDENCE: 5000,
    PRI_COMMAND: 4000,
    PRI_GENERIC: 1500,
}


def trim_messages(
    messages: list[dict],
    budget: int | None = None,
    *,
    min_recent_tool_outputs: int | None = None,
) -> tuple[list[dict], TrimSummary]:
    """Apply priority-aware, marker-emitting trim to a message list.

    The returned `TrimSummary` is metadata-only (counts and categories);
    callers will attach it to context_budget.ContextUsage so it lands in
    telemetry without leaking content.

    Algorithm:
      1. Compute total size; if under budget, no-op.
      2. Walk tool messages oldest → newest. Anything older than
         MIN_RECENT_TOOL_OUTPUTS gets summarized with [TOOL_SUMMARY].
      3. For the recent tier, apply per-tool trim_tool_message() based on
         tool_name when the message exceeds the priority's raw cap.
      4. If still over budget, drop low-priority tool outputs first
         (replaced by a short [CTX_TRIMMED] stub), then mid, then high.
    """
    if budget is None:
        budget = int(getattr(config, "MAX_MESSAGES_BUDGET", 250000))
    if min_recent_tool_outputs is None:
        min_recent_tool_outputs = int(getattr(config, "MIN_RECENT_TOOL_OUTPUTS", 4))

    summary = TrimSummary(chars_before=_calc_messages_size(messages))
    if summary.chars_before <= budget:
        summary.chars_after = summary.chars_before
        return messages, summary

    if len(messages) <= 2:
        summary.chars_after = summary.chars_before
        return messages, summary

    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if not tool_indices:
        summary.chars_after = summary.chars_before
        return messages, summary

    summary.rounds += 1

    # Pass 1: summarize older tool outputs.
    if len(tool_indices) > min_recent_tool_outputs:
        old_count = len(tool_indices) - min_recent_tool_outputs
        for idx in tool_indices[:old_count]:
            msg = messages[idx]
            content = msg.get("content", "")
            if not isinstance(content, str) or not content:
                continue
            tool_name = _resolve_tool_name(messages, idx)
            new_content, meta = trim_tool_message(
                content, tool_name, max_chars=400, mode="summarize"
            )
            if meta.get("trimmed"):
                msg["content"] = new_content
                summary.summarized_tool_outputs += 1
                summary.details.append({
                    "idx": idx,
                    "tool": tool_name,
                    "action": "summarize",
                    "original": meta["original"],
                    "kept": meta["kept"],
                })

    if _calc_messages_size(messages) <= budget:
        summary.chars_after = _calc_messages_size(messages)
        return messages, summary

    summary.rounds += 1
    # Pass 2: for the still-recent tier, apply per-tool trim where the
    # message exceeds the raw cap for its priority.
    recent_indices = tool_indices[-min_recent_tool_outputs:] if min_recent_tool_outputs else tool_indices
    for idx in recent_indices:
        msg = messages[idx]
        content = msg.get("content", "")
        if not isinstance(content, str) or not content:
            continue
        tool_name = _resolve_tool_name(messages, idx)
        priority = _priority_for_tool(tool_name)
        cap = _RAW_MAX_BY_PRIORITY[priority]
        if len(content) <= cap:
            continue
        new_content, meta = trim_tool_message(content, tool_name, max_chars=cap, mode="auto")
        if meta.get("trimmed"):
            msg["content"] = new_content
            summary.truncated_tool_outputs += 1
            if tool_name in _COMMAND_TOOLS:
                summary.run_command_tail_kept += meta.get("tail_kept", 0)
            if tool_name in _HIGH_PRIORITY_TOOLS:
                summary.read_file_window_kept += meta.get("kept", 0)
            summary.details.append({
                "idx": idx,
                "tool": tool_name,
                "action": "trim",
                "priority": priority,
                "original": meta["original"],
                "kept": meta["kept"],
            })

    if _calc_messages_size(messages) <= budget:
        summary.chars_after = _calc_messages_size(messages)
        return messages, summary

    summary.rounds += 1
    # Pass 3: still over budget. Collapse to a deterministic summary so we
    # at least preserve file:line anchors and error lines. Generic tools go
    # first (less likely to carry irreplaceable evidence).
    while _calc_messages_size(messages) > budget:
        # Re-evaluate tool indices each round (content shrinks).
        live_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        groups = {
            PRI_GENERIC: [i for i in live_indices if _priority_for_tool(_resolve_tool_name(messages, i)) == PRI_GENERIC],
            PRI_COMMAND: [i for i in live_indices if _priority_for_tool(_resolve_tool_name(messages, i)) == PRI_COMMAND],
            PRI_EVIDENCE: [i for i in live_indices if _priority_for_tool(_resolve_tool_name(messages, i)) == PRI_EVIDENCE],
        }
        candidates = groups[PRI_GENERIC] or groups[PRI_COMMAND] or groups[PRI_EVIDENCE]
        if not candidates:
            break
        max_idx = max(candidates, key=lambda i: len(messages[i].get("content", "")))
        content = messages[max_idx].get("content", "")
        if len(content) <= 200:
            # Already small enough; nothing more to gain on this candidate.
            # Mark it so we don't loop on it forever.
            break
        tool_name = _resolve_tool_name(messages, max_idx)
        # Use the deterministic summary form, which preserves file:line
        # anchors for read_file/grep and error lines for run_command.
        # Already-summarized messages still get shortened by this call.
        already_summary = TOOL_SUMMARY_OPEN in content
        new_content, meta = summarize_old_tool_output(content, tool_name)
        if not meta.get("trimmed") or len(new_content) >= len(content):
            # Couldn't shrink it any further (e.g. all anchors), fall back
            # to a stub but keep the tool name and anchors visible.
            file_line_hits = re.findall(
                r"\b([A-Za-z0-9_./\-]+\.(?:py|c|cpp|cc|h|hpp|go|rs|js|ts)):(\d+)",
                content,
            )
            anchors = ", ".join(f"{f}:{ln}" for f, ln in file_line_hits[:5])
            stub = (
                f"{CTX_TRIMMED_MARKER} dropped to fit budget; "
                f"tool={tool_name or 'unknown'} original_chars={len(content)} "
                f"reason=over_budget"
            )
            if anchors:
                stub += f" anchors={anchors}"
            new_content = stub
        messages[max_idx]["content"] = new_content
        summary.truncated_tool_outputs += 1
        summary.details.append({
            "idx": max_idx,
            "tool": tool_name,
            "action": "summarize_pass3" if already_summary else "shrink_to_summary",
            "original": len(content),
            "kept": len(new_content),
        })
        if len(new_content) >= len(content):
            # No progress — break to avoid an infinite loop.
            break

    summary.chars_after = _calc_messages_size(messages)
    return messages, summary
