"""取樣參數釘住測試(離線,不需要 llama-server)。

背景:llama-server 啟動沒帶 sampling 旗標時,內建預設是 temp 0.8 / top_k 40 /
min_p 0.05,偏離 Qwen3-235B-A22B-Thinking-2507 官方建議,容易讓模型杜撰具體事實。
CodeTrail 自己的呼叫除了壓 temperature,也把 top_p/top_k/min_p 釘在 Qwen 建議值
(config.CHAT_*),不再依賴 server 端預設。這支測試鎖住「參數真的有進 request payload」
與「agent 路徑真的帶了 config.CHAT_*」。
"""
from __future__ import annotations

from types import SimpleNamespace


class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _CapturingSession:
    """假 requests session:記錄送出的 json payload,回固定假 response。"""

    def __init__(self, payload: dict):
        self._payload = payload
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None, stream=False):
        self.calls.append({"url": url, "json": json, "stream": stream})
        return _FakeResp(self._payload)


# ------------------------------------------------------------
# llama_client 層:參數有沒有進 payload
# ------------------------------------------------------------
def test_chat_completions_forwards_sampling(monkeypatch):
    import llama_client

    sess = _CapturingSession({"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr(llama_client, "get_session", lambda: sess)

    llama_client.chat_completions(
        base_url="http://x:8080",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.0,
        top_p=0.95,
        top_k=20,
        min_p=0.0,
    )

    body = sess.calls[0]["json"]
    assert body["temperature"] == 0.0
    assert body["top_p"] == 0.95
    assert body["top_k"] == 20
    assert body["min_p"] == 0.0
    assert sess.calls[0]["url"].endswith("/v1/chat/completions")


def test_chat_completions_omits_sampling_when_unset(monkeypatch):
    """預設 None → 不送,沿用 server 啟動旗標的取樣預設(向後相容)。"""
    import llama_client

    sess = _CapturingSession({"choices": []})
    monkeypatch.setattr(llama_client, "get_session", lambda: sess)

    llama_client.chat_completions(
        base_url="http://x:8080",
        messages=[{"role": "user", "content": "hi"}],
    )

    body = sess.calls[0]["json"]
    assert "top_p" not in body
    assert "top_k" not in body
    assert "min_p" not in body


def test_native_completion_min_p_is_opt_in(monkeypatch):
    import llama_client

    # 不帶 min_p → payload 無 min_p(top_p/top_k 仍有舊預設,維持向後相容)
    sess = _CapturingSession({"content": "ok"})
    monkeypatch.setattr(llama_client, "get_session", lambda: sess)
    llama_client.native_completion(base_url="http://x:8080", prompt="hi")
    body = sess.calls[0]["json"]
    assert "min_p" not in body
    assert body["top_p"] == 0.95
    assert body["top_k"] == 40

    # 帶 min_p=0 → 進 payload
    sess2 = _CapturingSession({"content": "ok"})
    monkeypatch.setattr(llama_client, "get_session", lambda: sess2)
    llama_client.native_completion(base_url="http://x:8080", prompt="hi", min_p=0.0)
    assert sess2.calls[0]["json"]["min_p"] == 0.0


# ------------------------------------------------------------
# config 層:Qwen 建議值預設 + env 覆寫
# ------------------------------------------------------------
def test_config_chat_sampling_defaults(monkeypatch):
    import importlib
    import config

    for key in ("AICODE_CHAT_TOP_P", "AICODE_CHAT_TOP_K", "AICODE_CHAT_MIN_P"):
        monkeypatch.delenv(key, raising=False)
    importlib.reload(config)

    assert config.CHAT_TOP_P == 0.95
    assert config.CHAT_TOP_K == 20
    assert config.CHAT_MIN_P == 0.0
    assert isinstance(config.CHAT_TOP_P, float)
    assert isinstance(config.CHAT_TOP_K, int)
    assert isinstance(config.CHAT_MIN_P, float)


def test_config_chat_sampling_env_override(monkeypatch):
    import importlib
    import config

    monkeypatch.setenv("AICODE_CHAT_TOP_K", "40")
    importlib.reload(config)
    try:
        assert config.CHAT_TOP_K == 40
    finally:
        monkeypatch.delenv("AICODE_CHAT_TOP_K", raising=False)
        importlib.reload(config)


# ------------------------------------------------------------
# agent 層:互動 agent 路徑真的帶了 config.CHAT_*
# ------------------------------------------------------------
def test_agent_call_pins_chat_sampling(monkeypatch):
    """call_llm_with_tools 必須把 config.CHAT_* 帶進 chat_completions。"""
    import agent
    import config
    import llama_client

    captured: dict = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return {
            "choices": [{
                "message": {"content": "ok", "tool_calls": []},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        }

    usage = SimpleNamespace(error_type=None, did_trim=False, trim_summary=None)
    monkeypatch.setattr(agent.config, "require_main_model", lambda: "m")
    monkeypatch.setattr(llama_client, "chat_completions", fake_chat)
    monkeypatch.setattr(agent, "_compute_dynamic_num_ctx", lambda _m: 2048)
    monkeypatch.setattr(agent, "get_native_tools", lambda: [])
    monkeypatch.setattr(agent, "_pre_send_trim_if_needed", lambda *_a, **_kw: (usage, None))
    monkeypatch.setattr(agent.context_budget, "emit_pre_call_lines", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "enforce_gate", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "parse_usage_from_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "emit_post_call_line", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "log_metrics", lambda *_a, **_kw: None)

    agent.call_llm_with_tools([{"role": "user", "content": "hi"}])

    assert captured["top_p"] == config.CHAT_TOP_P
    assert captured["top_k"] == config.CHAT_TOP_K
    assert captured["min_p"] == config.CHAT_MIN_P
