"""context budget 與工具輸出 trim:預算計算、soft/hard 閘、未設定時的預設預算、
knowledge.py 的 context gate 唯一出口,以及 trim.py 的 marker / per-tool 策略 /
priority orchestration。

併入 tests/test_ctx_default_budget.py(2026-08-20)。
併入 tests/test_trim.py(2026-09-02):trim.py 是 context 預算的另一半——工具輸出
超過預算時要怎麼縮、縮了要留什麼(marker、錯誤行、檔案標頭、file:line 錨點),
以及 trim 絕不能碰 system / user 訊息與其中的 REF metadata。
"""
from __future__ import annotations

import json

import pytest

import config
import context_budget
import trim
import utils


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
    # Telemetry keeps the legacy field name, but its value is the one N_CTX.
    usage = context_budget.build_usage(
        source="agent_tools",
        requested_num_ctx=65536,
        prompt="x" * 1000,
    )
    assert usage.effective_num_ctx == 65536
    assert usage.dynamic_ctx_max == config.DYNAMIC_NUM_CTX_MAX


# --------------------------------------------------------------------------
# 併自 tests/test_ctx_default_budget.py:沒有明確設定時的預設預算。
# --------------------------------------------------------------------------
def _stub_gate(monkeypatch):
    """讓 check_and_log 記下 requested_num_ctx 後立即以 overflow 中斷，
    避免真的打到 llama-server。"""
    captured = {}

    def fake_check_and_log(*, source, requested_num_ctx, prompt=None,
                           messages=None, model=None, **kw):
        captured["ctx"] = requested_num_ctx
        usage = context_budget.ContextUsage(hard_overflow=True, source=source)
        raise context_budget.ContextOverflowError(usage)

    monkeypatch.setattr(utils.context_budget, "check_and_log", fake_check_and_log)
    monkeypatch.setattr(utils.config, "require_main_model", lambda: "dummy-model")
    return captured


def test_default_ctx_budget_is_server_truth():
    assert utils._default_ctx_budget() == config.N_CTX
    assert config.NUM_CTX == config.N_CTX
    assert config.DYNAMIC_NUM_CTX_MAX == config.N_CTX


@pytest.mark.parametrize(
    "caller",
    ["call_llm", "call_llm_stream"],
    ids=["call_llm", "call_llm_stream"],
)
def test_call_llm_defaults_to_server_truth(monkeypatch, caller):
    """未帶 num_ctx 時,call_llm 與 call_llm_stream 都要拿 server 真值(config.N_CTX)進 gate。

    call_llm_stream 那條:strict 路徑就是走這條。
    """
    captured = _stub_gate(monkeypatch)
    getattr(utils, caller)("hi")  # 未帶 num_ctx
    assert captured["ctx"] == config.N_CTX


def test_explicit_num_ctx_still_respected(monkeypatch):
    captured = _stub_gate(monkeypatch)
    utils.call_llm("hi", num_ctx=8192)
    assert captured["ctx"] == 8192


# ---------------------------------------------------------------------------
# knowledge.py 的主模型 call site 必須走 gate
# ---------------------------------------------------------------------------
# 這三個呼叫點以前直接打 llama_client。`_rerank_with_llm` 會把 15 個候選各 500
# 字塞進 prompt,爆掉的時候 llama-server 是從前面截掉,模型看到半份候選清單卻
# 照樣回一組 DOC_n —— 靜默錯答,沒有任何錯誤訊息。契約靠靜態檢查守:漏接的
# call site 不會自己喊。

def _knowledge_native_completion_callers() -> set[str]:
    """回傳 knowledge.py 內呼叫 llama_client.native_completion 的函式名集合。"""
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "knowledge.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    callers: set[str] = set()
    scopes: list[str] = []

    class Walker(ast.NodeVisitor):
        def visit_FunctionDef(self, node):  # noqa: N802
            scopes.append(node.name)
            self.generic_visit(node)
            scopes.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

        def visit_Call(self, node):  # noqa: N802
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "native_completion"
                and isinstance(func.value, ast.Name)
                and func.value.id == "llama_client"
            ):
                callers.add(scopes[-1] if scopes else "<module>")
            self.generic_visit(node)

    Walker().visit(tree)
    return callers


@pytest.mark.smoke
def test_knowledge_has_exactly_one_ungated_completion_entry():
    assert _knowledge_native_completion_callers() == {"_gated_completion"}, (
        "knowledge.py 的主模型 /completion 只能有 _gated_completion 一個出口。"
        "新增的 call site 要改走它,否則那條路徑沒有 context gate —— "
        "超長 prompt 會被 llama-server 從前面靜默截掉,不會報錯。"
    )


@pytest.mark.smoke
def test_gated_completion_refuses_overflow_without_calling_the_server(monkeypatch):
    import knowledge
    import llama_client

    def explode(**_kwargs):
        raise AssertionError("gate 應該在送出前就擋下來")

    monkeypatch.setattr(llama_client, "native_completion", explode)
    monkeypatch.setattr(config, "require_main_model", lambda: "test-model")
    monkeypatch.setattr(config, "N_CTX", 512)

    huge = "x" * (512 * 100)
    assert knowledge._gated_completion(
        source="test_overflow", prompt=huge, temperature=0, timeout=5
    ) == ""


@pytest.mark.smoke
def test_gated_completion_returns_model_text_when_within_budget(monkeypatch):
    import knowledge
    import llama_client

    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return {"content": "  keyword-a, keyword-b  "}

    monkeypatch.setattr(llama_client, "native_completion", fake)
    monkeypatch.setattr(config, "require_main_model", lambda: "test-model")

    out = knowledge._gated_completion(
        source="test_ok", prompt="short question", temperature=0.3, timeout=20
    )
    assert out == "keyword-a, keyword-b"
    assert seen["prompt"] == "short question"
    assert seen["temperature"] == 0.3
    assert seen["stream"] is False


# ── 原 test_trim.py:trim.py 的 markers、per-tool strategies、priority orchestration ──

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


@pytest.mark.parametrize(
    "trimmer,text",
    [
        ("trim_run_command_output", "small\noutput\n"),
        ("trim_read_file_output", "tiny file"),
    ],
    ids=["run_command_below_budget", "read_file_under_budget"],
)
def test_tool_output_under_budget_is_a_no_op(trimmer, text):
    """輸出沒超過預算時,run_command / read_file 的 trim 都要原樣回傳、trimmed=False。"""
    result, meta = getattr(trim, trimmer)(text, max_chars=10_000)
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


def test_unwired_code_rag_tool_name_uses_bounded_generic_trim():
    """The native-agent trim layer must not pretend to understand MCP output."""
    assert trim._priority_for_tool("code_rag_search") == trim.PRI_GENERIC
    payload = json.dumps({
        "evidence": [{"text": "x" * 5000}],
        "uncertainties": [],
        "seeds": None,
    })
    result, meta = trim.trim_tool_message(
        payload, "code_rag_search", max_chars=500, mode="auto"
    )
    assert len(result) <= 500
    assert trim.CTX_TRIMMED_MARKER in result
    assert meta["trimmed"] is True

    messages = [_msg("system", "s"), _msg("user", "q")]
    messages.extend(_build_tool_message_pair("code_rag_search", payload))
    output, summary = trim.trim_messages(messages, budget=500)
    assert summary.chars_after <= 500
    assert any(
        trim.CTX_TRIMMED_MARKER in row.get("content", "")
        for row in output if row.get("role") == "tool"
    )


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
