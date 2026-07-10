"""P0-3：internal LLM 呼叫在未帶 num_ctx 時，預設 ctx 預算必須是 server 真值
（DYNAMIC_NUM_CTX_MAX），而非舊的 aspirational NUM_CTX=131072。

漏掉的 production 路徑：
    query_knowledge_strict → answer_with_self_check → call_llm_stream()
這兩處沒帶 num_ctx，過去落到 131072，在 `llama-server -c 65536` 下會讓 gate
以為 prompt 還安全，實際被 server 截斷。
"""
from __future__ import annotations

import pytest

import config
import context_budget
import utils


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
    assert utils._default_ctx_budget() == min(config.NUM_CTX, config.DYNAMIC_NUM_CTX_MAX)
    # 在典型 `-c 65536` 部署下，絕不能是舊的 131072
    assert utils._default_ctx_budget() <= config.DYNAMIC_NUM_CTX_MAX


def test_call_llm_defaults_to_server_truth(monkeypatch):
    captured = _stub_gate(monkeypatch)
    utils.call_llm("hi")  # 未帶 num_ctx
    assert captured["ctx"] == min(config.NUM_CTX, config.DYNAMIC_NUM_CTX_MAX)


def test_call_llm_stream_defaults_to_server_truth(monkeypatch):
    captured = _stub_gate(monkeypatch)
    utils.call_llm_stream("hi")  # 未帶 num_ctx（strict 路徑就是走這條）
    assert captured["ctx"] == min(config.NUM_CTX, config.DYNAMIC_NUM_CTX_MAX)


def test_explicit_num_ctx_still_respected(monkeypatch):
    captured = _stub_gate(monkeypatch)
    utils.call_llm("hi", num_ctx=8192)
    assert captured["ctx"] == 8192
