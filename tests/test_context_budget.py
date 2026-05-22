"""Tests for context_budget.py — token estimation, hard gate, metrics parsing,
JSONL logging privacy.

These tests intentionally do not touch the network. They exercise the gating
logic that runs *before* any LLM request.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config
import context_budget


# ============================================================
# Token estimation
# ============================================================

def test_estimate_tokens_from_plain_string():
    tokens, chars = context_budget.estimate_tokens(prompt="hello world" * 100)
    assert chars == len("hello world" * 100)
    assert tokens > 0
    # 4-ish chars per token heuristic ⇒ tokens roughly chars / CHARS_PER_TOKEN
    expected = int(chars / config.CHARS_PER_TOKEN)
    assert tokens == expected


def test_estimate_tokens_from_messages_string_content():
    messages = [
        {"role": "system", "content": "a" * 100},
        {"role": "user", "content": "b" * 200},
    ]
    tokens, chars = context_budget.estimate_tokens(messages=messages)
    assert chars == 300
    assert tokens == int(300 / config.CHARS_PER_TOKEN)


def test_estimate_tokens_from_messages_list_parts():
    # OpenAI multi-part content shape: list of {"type": "text", "text": ...}
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello "},
                {"type": "text", "text": "world"},
            ],
        },
    ]
    tokens, chars = context_budget.estimate_tokens(messages=messages)
    assert chars == len("hello world")
    assert tokens >= 0


def test_estimate_tokens_with_image_part_charges_a_budget():
    # Image-style parts should still consume budget so we don't silently
    # under-estimate vision prompts.
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {"type": "image", "image": "data:image/png;base64,xxxxx"},
            ],
        }
    ]
    tokens, chars = context_budget.estimate_tokens(messages=messages)
    # Should be at least the text length plus a chunk for the image
    assert chars > len("describe this")
    assert tokens > 0


def test_estimate_tokens_with_tools_schema_counts():
    tools = [
        {"type": "function", "function": {"name": "read_file", "parameters": {"a": "b" * 200}}},
    ]
    tokens_no_tools, _ = context_budget.estimate_tokens(prompt="hi")
    tokens_tools, _ = context_budget.estimate_tokens(prompt="hi", tools=tools)
    assert tokens_tools > tokens_no_tools


def test_estimate_tokens_empty_inputs():
    tokens, chars = context_budget.estimate_tokens()
    assert tokens == 0 and chars == 0
    tokens, chars = context_budget.estimate_tokens(prompt="", messages=[], tools=[])
    assert tokens == 0 and chars == 0


def test_estimate_tokens_messages_with_tool_calls():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "x" * 500}}},
            ],
        }
    ]
    tokens, chars = context_budget.estimate_tokens(messages=messages)
    # tool_calls JSON should be counted; chars must be > 0
    assert chars > 100


# ============================================================
# Context gate
# ============================================================

def test_gate_passes_under_budget(monkeypatch):
    monkeypatch.setattr(config, "CTX_HARD_THRESHOLD", 0.90)
    monkeypatch.setattr(config, "CTX_SOFT_THRESHOLD", 0.80)
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 512)
    usage = context_budget.build_usage(
        source="generate",
        requested_num_ctx=32768,
        prompt="hello",
    )
    assert not usage.hard_overflow
    assert not usage.soft_warning
    context_budget.enforce_gate(usage)  # should not raise


def test_gate_soft_threshold_marks_warning(monkeypatch):
    monkeypatch.setattr(config, "CTX_HARD_THRESHOLD", 0.90)
    monkeypatch.setattr(config, "CTX_SOFT_THRESHOLD", 0.50)
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 0)
    # Build a prompt that lands at ~60% of a 1000-token ctx
    chars = int(600 * config.CHARS_PER_TOKEN)
    usage = context_budget.build_usage(
        source="generate",
        requested_num_ctx=1000,
        prompt="x" * chars,
    )
    assert usage.soft_warning is True
    assert usage.hard_overflow is False
    # Gate must NOT raise on soft
    context_budget.enforce_gate(usage)


def test_gate_hard_threshold_raises(monkeypatch):
    monkeypatch.setattr(config, "CTX_HARD_THRESHOLD", 0.90)
    monkeypatch.setattr(config, "CTX_SOFT_THRESHOLD", 0.80)
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 0)
    # 95% of a 1000-token ctx
    chars = int(950 * config.CHARS_PER_TOKEN)
    usage = context_budget.build_usage(
        source="generate",
        requested_num_ctx=1000,
        prompt="x" * chars,
    )
    assert usage.hard_overflow is True
    with pytest.raises(context_budget.ContextOverflowError):
        context_budget.enforce_gate(usage)


def test_gate_reserved_output_pushes_over_hard_threshold(monkeypatch):
    """Reserved output tokens count toward the budget — a prompt that would
    fit on its own can still overflow once output reservation is added."""
    monkeypatch.setattr(config, "CTX_HARD_THRESHOLD", 0.90)
    monkeypatch.setattr(config, "CTX_SOFT_THRESHOLD", 0.80)
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 800)
    # Prompt itself is 200 tokens, ctx is 1000. Input alone is 20%, but
    # input + reserved = 100% ⇒ hard overflow.
    chars = int(200 * config.CHARS_PER_TOKEN)
    usage = context_budget.build_usage(
        source="generate",
        requested_num_ctx=1000,
        prompt="x" * chars,
    )
    assert usage.estimated_input_tokens == 200
    assert usage.reserved_output_tokens == 800
    assert usage.hard_overflow is True


def test_gate_can_be_disabled_via_env(monkeypatch):
    monkeypatch.setattr(config, "CTX_GATE_ENABLED", False)
    monkeypatch.setattr(config, "CTX_HARD_THRESHOLD", 0.90)
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 0)
    chars = int(950 * config.CHARS_PER_TOKEN)
    usage = context_budget.build_usage(
        source="generate",
        requested_num_ctx=1000,
        prompt="x" * chars,
    )
    assert usage.hard_overflow is True
    # Gate disabled ⇒ no exception even though usage says hard_overflow
    context_budget.enforce_gate(usage)


def test_overflow_message_includes_remediation():
    usage = context_budget.ContextUsage(
        estimated_input_tokens=64000,
        reserved_output_tokens=4096,
        effective_num_ctx=65536,
        utilization_pct=104.0,
        hard_overflow=True,
        source="agent_tools",
    )
    msg = context_budget.overflow_message(usage)
    assert "[CTX_OVERFLOW]" in msg
    assert "64000" in msg
    assert "4096" in msg
    assert "65536" in msg
    # Must tell the user how to fix it, not just say no.
    assert "How to fix" in msg or "縮小" in msg


# ============================================================
# Metrics parsing
# ============================================================

def test_parse_metrics_from_native_completion_response():
    """llama-server native /completion 回 tokens_evaluated / tokens_predicted + timings。"""
    usage = context_budget.ContextUsage()
    resp = {
        "content": "...",
        "tokens_evaluated": 1024,
        "tokens_predicted": 512,
        "timings": {
            "prompt_per_second": 512.0,
            "predicted_per_second": 128.0,
        },
    }
    context_budget.parse_usage_from_response(resp, usage)
    assert usage.actual_prompt_eval_count == 1024
    assert usage.actual_eval_count == 512
    assert usage.prompt_tokens_per_second == pytest.approx(512.0)
    assert usage.output_tokens_per_second == pytest.approx(128.0)


def test_parse_metrics_from_openai_v1_response():
    """/v1/chat/completions 回 usage{prompt_tokens, completion_tokens}。"""
    usage = context_budget.ContextUsage()
    resp = {
        "choices": [{"message": {"content": "..."}}],
        "usage": {"prompt_tokens": 800, "completion_tokens": 200, "total_tokens": 1000},
        "timings": {"prompt_per_second": 200.0, "predicted_per_second": 50.0},
    }
    context_budget.parse_usage_from_response(resp, usage)
    assert usage.actual_prompt_eval_count == 800
    assert usage.actual_eval_count == 200
    assert usage.prompt_tokens_per_second == pytest.approx(200.0)
    assert usage.output_tokens_per_second == pytest.approx(50.0)


def test_parse_metrics_streaming_final_chunk_native():
    """native /completion 串流結束信號是 stop: true。"""
    usage = context_budget.ContextUsage()
    mid = {"content": "tok", "stop": False}
    context_budget.parse_usage_from_stream_chunk(mid, usage)
    assert usage.actual_prompt_eval_count is None

    final = {
        "stop": True,
        "tokens_evaluated": 100,
        "tokens_predicted": 50,
        "timings": {"prompt_per_second": 100.0, "predicted_per_second": 100.0},
    }
    context_budget.parse_usage_from_stream_chunk(final, usage)
    assert usage.actual_prompt_eval_count == 100
    assert usage.actual_eval_count == 50
    assert usage.prompt_tokens_per_second == pytest.approx(100.0)
    assert usage.output_tokens_per_second == pytest.approx(100.0)


def test_parse_metrics_streaming_final_chunk_openai():
    """/v1 串流結束信號是 choices[0].finish_reason 非 null。"""
    usage = context_budget.ContextUsage()
    mid = {"choices": [{"delta": {"content": "tok"}, "finish_reason": None}]}
    context_budget.parse_usage_from_stream_chunk(mid, usage)
    assert usage.actual_prompt_eval_count is None

    final = {
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }
    context_budget.parse_usage_from_stream_chunk(final, usage)
    assert usage.actual_prompt_eval_count == 100
    assert usage.actual_eval_count == 50


def test_parse_metrics_missing_fields_does_not_crash():
    usage = context_budget.ContextUsage()
    context_budget.parse_usage_from_response({"content": "x"}, usage)
    assert usage.actual_prompt_eval_count is None
    assert usage.actual_eval_count is None
    assert usage.prompt_tokens_per_second is None
    assert usage.output_tokens_per_second is None


def test_parse_metrics_no_timings_skips_tps():
    """llama-server 沒給 timings → tps 維持 None,但 token counts 仍要拿到。"""
    usage = context_budget.ContextUsage()
    resp = {"tokens_evaluated": 100, "tokens_predicted": 50}
    context_budget.parse_usage_from_response(resp, usage)
    assert usage.actual_prompt_eval_count == 100
    assert usage.actual_eval_count == 50
    assert usage.prompt_tokens_per_second is None
    assert usage.output_tokens_per_second is None


def test_parse_metrics_non_dict_input_is_safe():
    usage = context_budget.ContextUsage()
    # None / bool / int / str inputs must not crash
    context_budget.parse_usage_from_response(None, usage)  # type: ignore[arg-type]
    context_budget.parse_usage_from_response("not a dict", usage)  # type: ignore[arg-type]
    context_budget.parse_usage_from_stream_chunk(None, usage)  # type: ignore[arg-type]
    assert usage.actual_prompt_eval_count is None


# ============================================================
# Telemetry JSONL log
# ============================================================

def test_log_writes_metadata_only_no_prompt(tmp_path, monkeypatch):
    log_path = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(config, "CTX_METRICS_ENABLED", True)
    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(log_path))
    monkeypatch.setattr(config, "CTX_HARD_THRESHOLD", 0.90)
    monkeypatch.setattr(config, "CTX_SOFT_THRESHOLD", 0.80)
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 256)

    # Build a usage including a recognisable secret string that should
    # NEVER end up in the JSONL log.
    secret = "TOP_SECRET_NDA_STRING_QWERTY_98765"
    messages = [{"role": "user", "content": secret + " " + ("x" * 500)}]
    usage = context_budget.build_usage(
        source="generate",
        requested_num_ctx=32768,
        messages=messages,
        model="example-large-model:35b",
    )
    context_budget.log_metrics(usage)

    body = log_path.read_text(encoding="utf-8")
    assert secret not in body, "Telemetry log leaked prompt content"
    # The metadata we DO want should be present
    line = json.loads(body.strip().splitlines()[0])
    assert line["model"] == "example-large-model:35b"
    assert line["source"] == "generate"
    assert line["estimated_input_tokens"] > 0
    assert line["effective_num_ctx"] == 32768
    assert "timestamp" in line
    # Sanity: serialized line must not contain a long contiguous stretch of
    # the user's content. We already check for the secret string; also assert
    # the log has no field whose value is the user text.
    for v in line.values():
        if isinstance(v, str):
            assert "x" * 50 not in v


def test_log_writes_refused_attempt(tmp_path, monkeypatch):
    """A refused overflow attempt still goes into the log so we can see
    when the gate fired."""
    log_path = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(config, "CTX_METRICS_ENABLED", True)
    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(log_path))
    monkeypatch.setattr(config, "CTX_HARD_THRESHOLD", 0.90)
    monkeypatch.setattr(config, "CTX_SOFT_THRESHOLD", 0.80)
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 0)
    monkeypatch.setattr(config, "CTX_GATE_ENABLED", True)

    chars = int(950 * config.CHARS_PER_TOKEN)
    with pytest.raises(context_budget.ContextOverflowError):
        context_budget.check_and_log(
            source="generate",
            requested_num_ctx=1000,
            prompt="x" * chars,
            emit=False,
        )
    assert log_path.exists()
    line = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert line["hard_overflow"] is True
    assert line["error_type"] == "ctx_overflow"


def test_log_disabled_does_not_write(tmp_path, monkeypatch):
    log_path = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(config, "CTX_METRICS_ENABLED", False)
    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(log_path))
    usage = context_budget.build_usage(
        source="generate", requested_num_ctx=32768, prompt="hi"
    )
    context_budget.log_metrics(usage)
    assert not log_path.exists()


def test_check_and_log_succeeds_when_under_budget(tmp_path, monkeypatch):
    log_path = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(config, "CTX_METRICS_ENABLED", True)
    monkeypatch.setattr(config, "CTX_METRICS_PATH", str(log_path))
    monkeypatch.setattr(config, "CTX_HARD_THRESHOLD", 0.90)
    monkeypatch.setattr(config, "CTX_SOFT_THRESHOLD", 0.80)
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 128)
    usage = context_budget.check_and_log(
        source="generate",
        requested_num_ctx=32768,
        prompt="short prompt",
        emit=False,
    )
    # Under budget ⇒ no log write yet (post-call log writes happen via
    # the wrappers in utils.py / agent.py once the response is in).
    assert not log_path.exists()
    assert usage.hard_overflow is False


# ============================================================
# Dynamic num_ctx interplay
# ============================================================

def test_effective_ctx_uses_requested_value():
    # `requested_num_ctx` represents the *effective* ctx after dynamic
    # clamping in the caller. If we asked for 32K we should be metered
    # against 32K, not against the model's max.
    usage = context_budget.build_usage(
        source="agent_tools",
        requested_num_ctx=32768,
        prompt="hello",
    )
    assert usage.effective_num_ctx == 32768


def test_dynamic_max_respected_when_caller_clamps():
    # Simulate caller clamping to DYNAMIC_NUM_CTX_MAX=65536 even though
    # AICODE_NUM_CTX = 131072. We should reflect the clamped value.
    usage = context_budget.build_usage(
        source="agent_tools",
        requested_num_ctx=65536,
        prompt="x" * 1000,
    )
    assert usage.effective_num_ctx == 65536
    assert usage.dynamic_ctx_max == config.DYNAMIC_NUM_CTX_MAX
