"""Main LLM call sites must resolve the model at call time."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, response_payload: dict):
        self.response_payload = response_payload
        self.last_json = None

    def post(self, _url, json=None, **_kwargs):
        self.last_json = json
        return FakeResponse(self.response_payload)


def test_utils_call_llm_uses_require_main_model(monkeypatch):
    import utils

    session = FakeSession({"response": "ok"})
    usage = SimpleNamespace(error_type=None)

    monkeypatch.setattr(utils.config, "require_main_model", lambda: "calltime-utils:latest")
    monkeypatch.setattr(utils, "get_session", lambda: session)
    monkeypatch.setattr(utils.context_budget, "check_and_log", lambda **_kw: usage)
    monkeypatch.setattr(utils.context_budget, "parse_usage_from_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(utils.context_budget, "emit_post_call_line", lambda *_a, **_kw: None)
    monkeypatch.setattr(utils.context_budget, "log_metrics", lambda *_a, **_kw: None)

    assert utils.call_llm("hello") == "ok"
    assert session.last_json["model"] == "calltime-utils:latest"


def test_agent_call_llm_with_tools_uses_require_main_model(monkeypatch):
    import agent

    session = FakeSession({"message": {"content": "ok", "tool_calls": []}, "done_reason": "stop"})
    usage = SimpleNamespace(error_type=None, did_trim=False, trim_summary=None)

    monkeypatch.setattr(agent.config, "require_main_model", lambda: "calltime-agent:latest")
    monkeypatch.setattr(agent, "get_session", lambda: session)
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
    assert session.last_json["model"] == "calltime-agent:latest"


def test_knowledge_expand_query_uses_require_main_model(monkeypatch):
    import knowledge

    session = FakeSession({"response": "alpha, beta"})
    monkeypatch.setattr(knowledge.config, "require_main_model", lambda: "calltime-knowledge:latest")
    monkeypatch.setattr(knowledge, "get_session", lambda: session)

    kb = knowledge.KnowledgeBase(str(REPO_ROOT / ".missing-knowledge-for-test.json"))
    expanded = kb._expand_query("What changed?", force=True)

    assert expanded
    assert session.last_json["model"] == "calltime-knowledge:latest"
