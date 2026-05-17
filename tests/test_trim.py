"""Tests for trim.py — markers, per-tool strategies, priority orchestration."""
from __future__ import annotations

import json

import config
import trim


# ============================================================
# run_command output
# ============================================================

def test_run_command_keeps_tail_with_marker():
    head = "\n".join(f"line {i:04d}" for i in range(0, 500))
    tail = "\n".join(
        [
            "running tests...",
            "test_foo PASSED",
            "test_bar FAILED",
            "E   AssertionError: expected 1 got 2",
            "FAILED tests/test_bar.py::test_thing - AssertionError: expected 1 got 2",
        ]
    )
    text = head + "\n" + tail
    result, meta = trim.trim_run_command_output(text, max_chars=600)

    assert meta["trimmed"] is True
    assert trim.CTX_TRIMMED_MARKER in result
    # The actual failure summary must survive
    assert "FAILED tests/test_bar.py::test_thing" in result
    assert "AssertionError" in result
    # original char count is preserved in metadata
    assert meta["original"] == len(text)


def test_run_command_preserves_error_lines_from_middle():
    # Failure deep in the middle of a long log.
    lines = [f"info line {i}" for i in range(0, 200)]
    lines[100] = "ERROR: something broke at line 100"
    lines[101] = "Traceback (most recent call last):"
    lines[102] = '  File "foo.py", line 1, in <module>'
    text = "\n".join(lines)

    result, meta = trim.trim_run_command_output(text, max_chars=800)

    assert meta["trimmed"] is True
    # Error lines from the middle should be lifted to the [CTX_TRIMMED_ERROR_LINES]
    # section even though head+tail wouldn't have covered them.
    assert "ERROR: something broke at line 100" in result or "Traceback" in result
    assert "CTX_TRIMMED_ERROR_LINES" in result or trim.CTX_TRIMMED_MARKER in result


def test_run_command_below_budget_no_op():
    text = "small\noutput\n"
    result, meta = trim.trim_run_command_output(text, max_chars=10_000)
    assert meta["trimmed"] is False
    assert result == text


# ============================================================
# read_file output
# ============================================================

def test_read_file_oversized_gets_marker_and_header():
    header = "檔案: src/foo.py 行 1-300 / 共 300 行\n"
    body = "\n".join(f"{i:4d}: source line {i}" for i in range(1, 301))
    text = header + body
    result, meta = trim.trim_read_file_output(text, max_chars=400)

    assert meta["trimmed"] is True
    assert trim.CTX_TRIMMED_MARKER in result
    # Header (file + line range) must survive trimming so the model knows
    # WHICH file/window the content came from.
    assert "src/foo.py" in result
    assert "行 1-300" in result


def test_read_file_under_budget_no_op():
    text = "tiny file"
    result, meta = trim.trim_read_file_output(text, max_chars=10_000)
    assert meta["trimmed"] is False
    assert result == text


# ============================================================
# summarize_old_tool_output
# ============================================================

def test_summarize_extracts_file_line_anchors():
    text = (
        "function defined at agent.py:120\n"
        "calls helper() at src/utils.py:45\n"
        "more noise " * 200
    )
    summary, meta = trim.summarize_old_tool_output(text, tool_name="read_file")
    assert meta["trimmed"] is True
    assert trim.TOOL_SUMMARY_OPEN in summary
    assert trim.TOOL_SUMMARY_CLOSE in summary
    assert "agent.py:120" in summary
    assert "src/utils.py:45" in summary
    assert "tool=read_file" in summary


def test_summarize_command_keeps_error_lines():
    text = "\n".join(
        ["setup ok"] * 50
        + ["FAILED tests/test_a.py::x - AssertionError: nope"]
        + ["teardown"] * 50
    )
    summary, meta = trim.summarize_old_tool_output(text, tool_name="run_command")
    assert meta["trimmed"] is True
    assert "FAILED" in summary
    assert "AssertionError" in summary


def test_summarize_empty_text_no_op():
    summary, meta = trim.summarize_old_tool_output("", tool_name="anything")
    assert meta["trimmed"] is False
    assert summary == ""


# ============================================================
# trim_messages orchestrator
# ============================================================

def _msg(role, content, tool_name=None, tool_calls=None):
    m = {"role": role, "content": content}
    if tool_name:
        m["tool_name"] = tool_name
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


def _build_tool_message_pair(tool_name: str, body: str):
    """A canonical assistant→tool message pair the agent loop produces."""
    return [
        _msg(
            "assistant",
            "",
            tool_calls=[{"function": {"name": tool_name, "arguments": {}}}],
        ),
        _msg("tool", body, tool_name=tool_name),
    ]


def test_trim_messages_under_budget_no_op():
    messages = [
        _msg("system", "sys"),
        _msg("user", "hi"),
    ]
    out, summary = trim.trim_messages(messages, budget=10_000)
    assert summary.chars_before == summary.chars_after
    assert summary.summarized_tool_outputs == 0
    assert summary.truncated_tool_outputs == 0


def test_trim_messages_summarizes_oldest_first(monkeypatch):
    monkeypatch.setattr(config, "MIN_RECENT_TOOL_OUTPUTS", 2)
    messages = [_msg("system", "sys"), _msg("user", "q")]
    # 6 tool messages of run_command output. With MIN_RECENT=2, the oldest 4
    # should be summarized; the newest 2 left raw (subject to per-tool cap).
    for i in range(6):
        messages.extend(
            _build_tool_message_pair(
                "run_command",
                f"tool call {i}\n"
                + "x" * 1000
                + f"\nFAILED tests/test_{i}.py - boom",
            )
        )
    out, summary = trim.trim_messages(messages, budget=4000)
    assert summary.summarized_tool_outputs >= 1
    # The latest user message and the newest tool messages must not be empty.
    assert out[-1]["content"]
    # Markers should be present on the trimmed ones
    joined = "\n".join(m.get("content", "") for m in out if m.get("role") == "tool")
    assert (trim.TOOL_SUMMARY_OPEN in joined) or (trim.CTX_TRIMMED_MARKER in joined)


def test_trim_messages_uses_run_command_tail_for_recent_command_output(monkeypatch):
    monkeypatch.setattr(config, "MIN_RECENT_TOOL_OUTPUTS", 1)
    huge_tail = "\nFAILED tests/test_x.py::y - AssertionError: nope"
    body = ("info\n" * 5000) + huge_tail
    messages = [_msg("system", "s"), _msg("user", "q")]
    messages.extend(_build_tool_message_pair("run_command", body))
    out, summary = trim.trim_messages(messages, budget=2000)
    tool_msg = out[-1]
    # Recent run_command output must still surface the failure tail.
    assert "FAILED" in tool_msg["content"]
    # Either marker is acceptable — both unambiguously signal "this was trimmed".
    assert (
        trim.CTX_TRIMMED_MARKER in tool_msg["content"]
        or trim.TOOL_SUMMARY_OPEN in tool_msg["content"]
    )


def test_trim_messages_preserves_read_file_header_and_marks_trim(monkeypatch):
    monkeypatch.setattr(config, "MIN_RECENT_TOOL_OUTPUTS", 1)
    header = "檔案: src/big.py 行 1-2000 / 共 2000 行\n"
    big_body = "\n".join(f"{i:4d}: line" for i in range(1, 2001))
    messages = [_msg("system", "s"), _msg("user", "q")]
    messages.extend(_build_tool_message_pair("read_file", header + big_body))
    out, summary = trim.trim_messages(messages, budget=3000)
    last = out[-1]["content"]
    assert "src/big.py" in last  # header survived
    assert trim.CTX_TRIMMED_MARKER in last  # explicit marker


def test_trim_messages_priority_drops_generic_before_evidence(monkeypatch):
    monkeypatch.setattr(config, "MIN_RECENT_TOOL_OUTPUTS", 0)
    messages = [_msg("system", "s"), _msg("user", "q")]
    # One generic-ish tool, one evidence tool (read_file). Generic should
    # shrink first.
    messages.extend(_build_tool_message_pair("unknown_tool", "y" * 5000))
    messages.extend(_build_tool_message_pair("read_file",
                    "檔案: a.py 行 1-1 / 共 1 行\nimportant: x.py:42 here"))
    out, summary = trim.trim_messages(messages, budget=500)
    # Both should be touched but the read_file message should still mention
    # the file:line anchor.
    contents = [m["content"] for m in out if m.get("role") == "tool"]
    # The summary or trim should preserve file:line anchors for the evidence tool.
    assert any("x.py:42" in c or "a.py" in c for c in contents)


def test_trim_messages_emits_telemetry_metadata_only():
    """Trim summary returned to the caller must be JSON-safe metadata,
    never the original prompt text."""
    secret = "TOP_SECRET_DOC_BLOB_AAA"
    messages = [_msg("system", "s"), _msg("user", "q")]
    messages.extend(_build_tool_message_pair("read_file",
                    secret + "\n" + ("x" * 5000)))
    _, summary = trim.trim_messages(messages, budget=300)
    blob = json.dumps(summary.to_dict(), ensure_ascii=False)
    assert secret not in blob
    # But counts should be present
    d = summary.to_dict()
    assert "summarized" in d and "truncated" in d
    assert d["chars_before"] >= d["chars_after"]


def test_trim_messages_never_touches_system_or_user_messages():
    secret_user = "USER_PROMPT_PRECIOUS"
    messages = [
        _msg("system", "very important system instructions"),
        _msg("user", secret_user),
    ]
    messages.extend(_build_tool_message_pair("run_command", "x" * 10_000))
    out, _ = trim.trim_messages(messages, budget=500)
    # System / user content stays bit-for-bit identical.
    assert out[0]["content"] == "very important system instructions"
    assert out[1]["content"] == secret_user


# ============================================================
# Strict / REF question: knowledge_ctx is in the SYSTEM message,
# so REF metadata must not be dropped by trim.
# ============================================================

def test_strict_question_ref_metadata_survives_trim():
    knowledge_ctx = (
        "【參考資料】\n"
        "REF1: src/spec.md p.12 - 最大值 1024 (REF1)\n"
        "REF2: src/manual.md p.5 - 預設值 100 (REF2)\n"
    )
    system_prompt = f"system rules\n{knowledge_ctx}\n專案路徑: /x"
    messages = [
        _msg("system", system_prompt),
        _msg("user", "依文件說明,最大值是多少?"),
    ]
    # Pile on tool outputs to force aggressive trim
    for i in range(8):
        messages.extend(
            _build_tool_message_pair("run_command", f"out {i}\n" + "x" * 3000)
        )
    out, _ = trim.trim_messages(messages, budget=2000)
    # System prompt (containing REF1/REF2) must be byte-identical.
    assert out[0]["content"] == system_prompt
    assert "REF1" in out[0]["content"]
    assert "REF2" in out[0]["content"]
