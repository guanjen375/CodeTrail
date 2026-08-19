#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Transport hardening 測試(§4):endpoint policy、proxy/redirect、llama_client 全 call site。

全部離線:HTTP 一律 mock;policy 在送出前就必須擋下,不需要真 server。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402
import endpoint_policy  # noqa: E402
import http_client  # noqa: E402
import llama_client  # noqa: E402


# ============================================================
# endpoint_policy 本體
# ============================================================
def test_loopback_hosts_always_allowed(monkeypatch):
    monkeypatch.delenv(endpoint_policy.MODEL_REMOTE_OK_ENV, raising=False)
    for url in (
        "http://127.0.0.1:8080",
        "http://localhost:8081",
        "http://[::1]:8082",
        "http://ip6-localhost:8083",
    ):
        endpoint_policy.ensure_allowed(url, "model")  # 不得 raise
        endpoint_policy.ensure_allowed(url, "kb_context")


def test_model_role_rejects_remote_without_opt_in(monkeypatch):
    monkeypatch.delenv(endpoint_policy.MODEL_REMOTE_OK_ENV, raising=False)
    with pytest.raises(endpoint_policy.EndpointPolicyError) as exc:
        endpoint_policy.ensure_allowed("http://10.0.0.5:8080", "model")
    message = str(exc.value)
    assert "AICODE_MODEL_REMOTE_OK" in message, "錯誤訊息必須印確切 env 名"
    assert "NDA" in message, "錯誤訊息必須講明會外送什麼"


def test_model_role_allows_remote_with_opt_in(monkeypatch):
    monkeypatch.setenv(endpoint_policy.MODEL_REMOTE_OK_ENV, "1")
    endpoint_policy.ensure_allowed("http://10.0.0.5:8080", "model")  # 不得 raise


def test_kb_context_role_uses_config_flag_not_model_env(monkeypatch):
    # model env 開著也擋:kb_context 只認 config.KB_CONTEXT_REMOTE_OK
    monkeypatch.setenv(endpoint_policy.MODEL_REMOTE_OK_ENV, "1")
    monkeypatch.setattr(config, "KB_CONTEXT_REMOTE_OK", False)
    with pytest.raises(endpoint_policy.EndpointPolicyError) as exc:
        endpoint_policy.ensure_allowed("http://10.0.0.5:8080", "kb_context")
    assert "AICODE_KB_CONTEXT_REMOTE_OK" in str(exc.value)

    monkeypatch.setattr(config, "KB_CONTEXT_REMOTE_OK", True)
    endpoint_policy.ensure_allowed("http://10.0.0.5:8080", "kb_context")


def test_model_role_ignores_kb_context_flag(monkeypatch):
    monkeypatch.delenv(endpoint_policy.MODEL_REMOTE_OK_ENV, raising=False)
    monkeypatch.setattr(config, "KB_CONTEXT_REMOTE_OK", True)
    with pytest.raises(endpoint_policy.EndpointPolicyError):
        endpoint_policy.ensure_allowed("http://10.0.0.5:8080", "model")


def test_unknown_role_is_a_programming_error():
    with pytest.raises(ValueError, match="unknown endpoint role"):
        endpoint_policy.ensure_allowed("http://127.0.0.1:8080", "nope")


# ============================================================
# http_client 硬化
# ============================================================
def test_shared_session_ignores_environment_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://evil.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://evil.invalid:3128")
    session = http_client.create_session()
    assert session.trust_env is False, "模型流量不得讀環境 proxy / .netrc"
    assert session.max_redirects == 0


# ============================================================
# llama_client:3xx fail-loud、全 call site policy
# ============================================================
class _FakeResponse:
    def __init__(self, status_code: int, headers: dict | None = None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self.response

    def get(self, url, **kwargs):
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self.response


SECRET_PROMPT = "NDA-SECRET-PAYLOAD-12345"


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_is_fail_loud_and_body_free(monkeypatch, status):
    fake = _FakeSession(
        _FakeResponse(status, headers={"Location": "http://evil.invalid/collect"})
    )
    monkeypatch.setattr(llama_client, "get_session", lambda: fake)

    with pytest.raises(RuntimeError) as exc:
        llama_client.native_completion(
            base_url="http://127.0.0.1:8080", prompt=SECRET_PROMPT
        )
    message = str(exc.value)
    assert str(status) in message
    assert "evil.invalid" in message, "訊息要含 Location host 供診斷"
    assert SECRET_PROMPT not in message, "redirect 錯誤訊息絕不得含 request body"
    assert fake.calls[0]["allow_redirects"] is False


def test_chat_completions_rejects_redirect(monkeypatch):
    fake = _FakeSession(_FakeResponse(302, headers={"Location": "http://evil.invalid/"}))
    monkeypatch.setattr(llama_client, "get_session", lambda: fake)
    with pytest.raises(RuntimeError, match="redirected"):
        llama_client.chat_completions(
            base_url="http://127.0.0.1:8080",
            messages=[{"role": "user", "content": SECRET_PROMPT}],
        )
    assert fake.calls[0]["allow_redirects"] is False


def test_embed_and_rerank_reject_redirect(monkeypatch):
    fake = _FakeSession(_FakeResponse(307, headers={"Location": "http://evil.invalid/"}))
    monkeypatch.setattr(llama_client, "get_session", lambda: fake)
    with pytest.raises(RuntimeError, match="redirected"):
        llama_client.embed_one(base_url="http://127.0.0.1:8081", content=SECRET_PROMPT)
    with pytest.raises(RuntimeError, match="redirected"):
        llama_client.rerank(
            base_url="http://127.0.0.1:8082", query="q", documents=[SECRET_PROMPT]
        )


@pytest.mark.parametrize(
    "call",
    [
        lambda: llama_client.native_completion(base_url="http://10.9.8.7:8080", prompt="x"),
        lambda: llama_client.chat_completions(
            base_url="http://10.9.8.7:8080", messages=[{"role": "user", "content": "x"}]
        ),
        lambda: llama_client.embed_one(base_url="http://10.9.8.7:8081", content="x"),
        lambda: llama_client.embed_batch(base_url="http://10.9.8.7:8081", contents=["x"]),
        lambda: llama_client.rerank(
            base_url="http://10.9.8.7:8082", query="q", documents=["d"]
        ),
    ],
)
def test_prompt_bearing_calls_reject_remote_without_opt_in(monkeypatch, call):
    monkeypatch.delenv(endpoint_policy.MODEL_REMOTE_OK_ENV, raising=False)

    class _Bomb:
        def __getattr__(self, name):
            raise AssertionError("policy 必須在任何 HTTP 動作之前 raise")

    monkeypatch.setattr(llama_client, "get_session", lambda: _Bomb())
    with pytest.raises(endpoint_policy.EndpointPolicyError, match="AICODE_MODEL_REMOTE_OK"):
        call()


def test_probes_log_policy_rejection_and_return_none(monkeypatch, capsys):
    monkeypatch.delenv(endpoint_policy.MODEL_REMOTE_OK_ENV, raising=False)

    class _Bomb:
        def __getattr__(self, name):
            raise AssertionError("policy 必須在任何 HTTP 動作之前 raise")

    monkeypatch.setattr(llama_client, "get_session", lambda: _Bomb())

    assert llama_client.get_health("http://10.9.8.7:8080") is None
    assert llama_client.get_props("http://10.9.8.7:8080") is None
    assert llama_client.get_slots("http://10.9.8.7:8080") is None

    err = capsys.readouterr().err
    assert err.count("AICODE_MODEL_REMOTE_OK") == 3, (
        "probe 吞例外回 None 前必須在 stderr 留下 policy 拒絕原因"
    )


def test_probe_calls_allowed_on_loopback(monkeypatch):
    fake = _FakeSession(_FakeResponse(200, payload={"status": "ok"}))
    monkeypatch.setattr(llama_client, "get_session", lambda: fake)
    assert llama_client.get_health("http://127.0.0.1:8080") == {"status": "ok"}
    assert fake.calls[0]["allow_redirects"] is False


def test_remote_calls_allowed_with_opt_in(monkeypatch):
    monkeypatch.setenv(endpoint_policy.MODEL_REMOTE_OK_ENV, "1")
    fake = _FakeSession(_FakeResponse(200, payload={"content": "hi"}))
    monkeypatch.setattr(llama_client, "get_session", lambda: fake)
    result = llama_client.native_completion(base_url="http://10.9.8.7:8080", prompt="x")
    assert result == {"content": "hi"}


# ============================================================
# RAG.py --url 行為不變(使用者顯式抓外部網頁,不經 model policy)
# ============================================================
def test_rag_fetch_url_does_not_go_through_model_policy(monkeypatch):
    import types

    import RAG

    monkeypatch.delenv(endpoint_policy.MODEL_REMOTE_OK_ENV, raising=False)

    # html2text 是 optional dep;fake 一份讓路徑走得下去(測的是 policy,不是轉換)
    class _FakeH2T:
        ignore_links = False
        ignore_images = True
        ignore_emphasis = False
        body_width = 0
        unicode_snob = True
        skip_internal_links = True

        def handle(self, html):
            return "hello markdown"

    fake_html2text = types.ModuleType("html2text")
    fake_html2text.HTML2Text = _FakeH2T
    monkeypatch.setitem(sys.modules, "html2text", fake_html2text)

    calls = {}

    class _Resp:
        status_code = 200
        text = "<html><title>t</title><body>hello</body></html>"
        apparent_encoding = "utf-8"
        encoding = "utf-8"

        def raise_for_status(self):
            return None

    def _fake_get(url, **kwargs):
        calls["url"] = url
        return _Resp()

    import requests

    monkeypatch.setattr(requests, "get", _fake_get)
    content, title = RAG.fetch_url_content("http://example.invalid/page")
    assert calls["url"] == "http://example.invalid/page", (
        "--url 是使用者顯式要抓的外部網頁,不得被 model endpoint policy 擋下"
    )
    assert content == "hello markdown"


# ============================================================
# GPT 審核修正的回歸測試(2026-08-19 二輪)
# ============================================================
def test_redirect_message_uses_hostname_and_redacts_credentials(monkeypatch):
    """審核 #10:Location 用 .hostname(netloc 會帶 user:pass);request URL
    內嵌 credentials 也要遮蔽。"""
    fake = _FakeSession(_FakeResponse(
        302, headers={"Location": "http://leak-user:leak-pass@evil.invalid:9999/x"}))
    monkeypatch.setattr(llama_client, "get_session", lambda: fake)
    monkeypatch.setenv(endpoint_policy.MODEL_REMOTE_OK_ENV, "1")

    with pytest.raises(RuntimeError) as exc:
        llama_client.native_completion(
            base_url="http://api-user:api-secret@127.0.0.1:8080", prompt="x")
    message = str(exc.value)
    assert "evil.invalid" in message
    assert "leak-pass" not in message and "leak-user" not in message, (
        "Location 的 credentials 不得進錯誤訊息")
    assert "api-secret" not in message and "api-user" not in message, (
        "request URL 內嵌的 credentials 必須遮蔽")
    assert "127.0.0.1:8080" in message


def test_probe_log_redacts_credentials(monkeypatch, capsys):
    monkeypatch.setenv(endpoint_policy.MODEL_REMOTE_OK_ENV, "1")

    class _Boom:
        def get(self, *a, **k):
            raise ConnectionError("down")

    monkeypatch.setattr(llama_client, "get_session", lambda: _Boom())
    assert llama_client.get_health("http://u:topsecret@127.0.0.1:8080") is None
    err = capsys.readouterr().err
    assert "topsecret" not in err
    assert "127.0.0.1" in err


def test_policy_error_redacts_credentials_without_opt_in(monkeypatch):
    """審核二輪 #2:真正的洩漏路徑 —— 未 opt-in 時 policy 例外不得帶密碼。"""
    monkeypatch.delenv(endpoint_policy.MODEL_REMOTE_OK_ENV, raising=False)
    with pytest.raises(endpoint_policy.EndpointPolicyError) as exc:
        endpoint_policy.ensure_allowed("http://user:secret@10.0.0.5:8080/v1", "model")
    message = str(exc.value)
    assert "secret" not in message and "user:" not in message
    assert "10.0.0.5:8080" in message, "host:port 要保留供診斷"

    monkeypatch.setattr(config, "KB_CONTEXT_REMOTE_OK", False)
    with pytest.raises(endpoint_policy.EndpointPolicyError) as exc:
        endpoint_policy.ensure_allowed("http://user:secret@10.0.0.5:8080", "kb_context")
    assert "secret" not in str(exc.value)


def test_prompt_call_policy_rejection_has_no_credentials(monkeypatch):
    monkeypatch.delenv(endpoint_policy.MODEL_REMOTE_OK_ENV, raising=False)

    class _Bomb:
        def __getattr__(self, name):
            raise AssertionError("policy 必須在任何 HTTP 動作之前 raise")

    monkeypatch.setattr(llama_client, "get_session", lambda: _Bomb())
    with pytest.raises(endpoint_policy.EndpointPolicyError) as exc:
        llama_client.embed_one(
            base_url="http://user:secret@10.0.0.5:8081", content="x")
    assert "secret" not in str(exc.value)


def test_probe_stderr_has_no_credentials_without_opt_in(monkeypatch, capsys):
    """probe 吞 policy 例外回 None 的 stderr 也不得帶密碼(exc 本身已 redact)。"""
    monkeypatch.delenv(endpoint_policy.MODEL_REMOTE_OK_ENV, raising=False)
    assert llama_client.get_health("http://user:secret@10.0.0.5:8080") is None
    err = capsys.readouterr().err
    assert "secret" not in err and "user:" not in err
    assert "10.0.0.5" in err


def test_redact_url_helper():
    assert endpoint_policy.redact_url("http://u:p@h:1/x?q=1") == "http://h:1/x?q=1"
    assert endpoint_policy.redact_url("http://h:1/x") == "http://h:1/x"
    assert endpoint_policy.redact_url("not a url") == "not a url"
