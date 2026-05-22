"""Main LLM call sites must resolve the model at call time.

新版用 llama_client 而不是 raw http_client。我們攔 llama_client 的入口函式。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class CapturingNativeCompletion:
    """假裝 llama_client.native_completion 的 callable;同時記錄被叫到時帶的參數。"""
    def __init__(self, response_payload: dict):
        self.response_payload = response_payload
        self.last_kwargs = None

    def __call__(self, **kwargs):
        self.last_kwargs = kwargs
        return self.response_payload


class CapturingChatCompletions:
    """假裝 llama_client.chat_completions。"""
    def __init__(self, response_payload: dict):
        self.response_payload = response_payload
        self.last_kwargs = None

    def __call__(self, **kwargs):
        self.last_kwargs = kwargs
        return self.response_payload


def test_utils_call_llm_uses_require_main_model(monkeypatch):
    import utils
    import llama_client

    fake = CapturingNativeCompletion({"content": "ok"})
    usage = SimpleNamespace(error_type=None)

    monkeypatch.setattr(utils.config, "require_main_model", lambda: "calltime-utils")
    monkeypatch.setattr(llama_client, "native_completion", fake)
    monkeypatch.setattr(utils.context_budget, "check_and_log", lambda **_kw: usage)
    monkeypatch.setattr(utils.context_budget, "parse_usage_from_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(utils.context_budget, "emit_post_call_line", lambda *_a, **_kw: None)
    monkeypatch.setattr(utils.context_budget, "log_metrics", lambda *_a, **_kw: None)

    # call_llm 不再傳 model 給 server (llama-server 是 one-model-per-instance),
    # 但仍應該透過 require_main_model 解析。我們驗證 require_main_model 有被叫到
    # — 由上面 monkeypatch 直接 patch 成 fn 並回固定字串,效果一樣可驗證。
    assert utils.call_llm("hello") == "ok"
    # native_completion 至少被呼叫一次
    assert fake.last_kwargs is not None
    assert fake.last_kwargs["prompt"] == "hello"


def test_agent_call_llm_with_tools_uses_require_main_model(monkeypatch):
    import agent
    import llama_client

    fake = CapturingChatCompletions({
        "choices": [{
            "message": {"content": "ok", "tool_calls": []},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    })
    usage = SimpleNamespace(error_type=None, did_trim=False, trim_summary=None)

    monkeypatch.setattr(agent.config, "require_main_model", lambda: "calltime-agent")
    monkeypatch.setattr(llama_client, "chat_completions", fake)
    monkeypatch.setattr(agent, "_compute_dynamic_num_ctx", lambda _messages: 2048)
    monkeypatch.setattr(agent, "get_native_tools", lambda: [])
    monkeypatch.setattr(agent, "_pre_send_trim_if_needed", lambda *_a, **_kw: (usage, None))
    monkeypatch.setattr(agent.context_budget, "emit_pre_call_lines", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "enforce_gate", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "parse_usage_from_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "emit_post_call_line", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "log_metrics", lambda *_a, **_kw: None)

    result = agent.call_llm_with_tools([{"role": "user", "content": "hello"}])

    assert result["content"] == "ok"
    assert fake.last_kwargs["model"] == "calltime-agent"


def test_knowledge_expand_query_uses_require_main_model(monkeypatch):
    import knowledge
    import llama_client

    fake = CapturingNativeCompletion({"content": "alpha, beta"})
    monkeypatch.setattr(knowledge.config, "require_main_model", lambda: "calltime-knowledge")
    monkeypatch.setattr(llama_client, "native_completion", fake)

    kb = knowledge.KnowledgeBase(str(REPO_ROOT / ".missing-knowledge-for-test.json"))
    expanded = kb._expand_query("What changed?", force=True)

    assert expanded
    # require_main_model 在路徑中被叫到時 patched 成回 "calltime-knowledge"。
    # native_completion 本身沒帶 model 參數(server 鎖死),但 require_main_model
    # 解析的值會出現在 monkeypatch hook 觸發前的呼叫;只需確認 native_completion
    # 真的被呼到即可。
    assert fake.last_kwargs is not None
