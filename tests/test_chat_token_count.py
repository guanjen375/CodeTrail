"""Exact chat counting contracts; all requests use synthetic, offline doubles."""
from __future__ import annotations

import copy
import threading

import pytest
import requests

import config
import endpoint_policy
import llama_client
from http_cancel import RequestCancellation, RequestCancelled


pytestmark = pytest.mark.smoke


class _Response:
    def __init__(self, payload=None, *, status=200, error=None):
        self.payload = payload
        self.status_code = status
        self.error = error
        self.headers = {"Location": "https://other.invalid/"}
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("synthetic-response-body-should-stay-private")

    def json(self):
        if self.error is not None:
            raise self.error
        return self.payload

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.before_return = None

    def post(self, url, **kwargs):
        self.calls.append((url, copy.deepcopy(kwargs)))
        if self.before_return is not None:
            self.before_return()
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _install_session(monkeypatch, session):
    monkeypatch.setattr(llama_client, "get_session", lambda: session)
    monkeypatch.setattr(RequestCancellation, "session", lambda self: session)


def test_exact_count_sends_the_same_complete_body_as_chat(monkeypatch):
    """Tools, reasoning and template controls must reach the real chat parser."""
    response = _Response({"input_tokens": 347})
    session = _Session(response)
    _install_session(monkeypatch, session)
    arguments = {
        "base_url": "http://127.0.0.1:65535",
        "model": "synthetic-model",
        "messages": [
            {"role": "system", "content": "Use the supplied tool."},
            {"role": "user", "content": "Look up one."},
            {"role": "assistant", "content": "", "reasoning_content": "Check one.",
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "lookup", "arguments": '{"key":"one"}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "Found one."},
        ],
        "tools": [{"type": "function", "function": {
            "name": "lookup", "parameters": {"type": "object", "properties": {
                "key": {"type": "string"}}, "required": ["key"]}}}],
        "tool_choice": "none",
        "temperature": 0.4,
        "top_p": 0.8,
        "top_k": 12,
        "min_p": 0.03,
        "stream": True,
        "extra": {"max_tokens": 1024, "return_progress": True,
                  "chat_template_kwargs": {"enable_thinking": True},
                  "add_generation_prompt": True, "reasoning_format": "deepseek"},
    }
    expected_arguments = copy.deepcopy(arguments)
    assert llama_client.count_chat_tokens(**arguments) == 347
    stream = llama_client.chat_completions(**arguments)
    stream.close()
    assert arguments == expected_arguments
    assert len(session.calls) == 2
    count_url, count_request = session.calls[0]
    chat_url, chat_request = session.calls[1]
    assert count_url.endswith("/v1/chat/completions/input_tokens")
    assert chat_url.endswith("/v1/chat/completions")
    assert count_request["json"] == chat_request["json"]
    assert "stream_options" not in count_request["json"]
    assert "include_usage" not in count_request["json"]
    assert count_request["allow_redirects"] is False
    assert 0 < count_request["timeout"] <= 30
    assert response.closed


@pytest.mark.parametrize("enabled", [None, False, True])
def test_chat_wire_thinking_defaults_off_and_keeps_parser_and_template_controls_together(monkeypatch, enabled):
    response = _Response({"input_tokens": 5, "choices": [{"message": {"content": "ok"}}]})
    session = _Session(response)
    _install_session(monkeypatch, session)
    arguments = {
        "base_url": "http://127.0.0.1:65535",
        "messages": [{"role": "user", "content": "synthetic"}],
    }
    if enabled is not None:
        arguments["extra"] = {"chat_template_kwargs": llama_client.thinking_template_kwargs(enabled)}
    assert llama_client.count_chat_tokens(**arguments) == 5
    llama_client.chat_completions(**arguments)
    counted, generated = [call[1]["json"] for call in session.calls]
    assert counted == generated
    assert generated["chat_template_kwargs"] == {
        "enable_thinking": enabled is True, "thinking": enabled is True,
    }


def test_native_completion_does_not_claim_chat_template_thinking_control(monkeypatch):
    session = _Session(_Response({"content": "native"}))
    _install_session(monkeypatch, session)
    llama_client.native_completion(base_url="http://127.0.0.1:65535", prompt="plain native prompt")
    url, request = session.calls[0]
    assert url.endswith("/completion") and request["json"]["prompt"] == "plain native prompt"
    assert "chat_template_kwargs" not in request["json"]


def test_exact_count_fails_closed_without_exposing_response_or_request_bodies(monkeypatch):
    """Malformed/failed counts may never become a heuristic or expose payloads."""
    marker = "synthetic-response-body-should-stay-private"
    malformed = [None, [], {}, {"input_tokens": True}, {"input_tokens": -1},
                 {"input_tokens": 1.5}, {"input_tokens": "347"}]
    responses = [_Response(item) for item in malformed]
    responses += [_Response({"error": marker}, status=status) for status in (302, 404, 500)]
    responses += [_Response(error=ValueError(marker)), requests.ConnectionError(marker)]
    for response in responses:
        session = _Session(response)
        _install_session(monkeypatch, session)
        with pytest.raises(llama_client.ChatTokenCountError) as failure:
            llama_client.count_chat_tokens(
                base_url="http://127.0.0.1:65535",
                messages=[{"role": "user", "content": marker}],
            )
        assert marker not in str(failure.value)
        assert failure.value.__cause__ is None
        assert len(session.calls) == 1
        if isinstance(response, _Response):
            assert response.closed
    session = _Session(_Response({"input_tokens": 0}))
    _install_session(monkeypatch, session)
    assert llama_client.count_chat_tokens(
        base_url="http://127.0.0.1:65535", messages=[]
    ) == 0


def test_exact_count_owns_only_its_scoped_cancellation_transport(monkeypatch):
    """Counting must use the cancellable session and preserve caller ownership."""
    session = _Session(_Response({"input_tokens": 10}))
    created = []

    class Cancellation:
        def __init__(self):
            self.event = threading.Event()
            self.closed = False
            created.append(self)

        def check(self):
            if self.event.is_set():
                raise RequestCancelled("synthetic cancellation")

        def session(self):
            self.check()
            return session

        def close(self):
            self.closed = True
            self.event.set()

    monkeypatch.setattr(llama_client, "RequestCancellation", Cancellation)
    monkeypatch.setattr(llama_client, "get_session", lambda: pytest.fail("shared session used"))
    arguments = {"base_url": "http://127.0.0.1:65535", "messages": []}
    assert llama_client.count_chat_tokens(**arguments, timeout=600) == 10
    assert len(created) == 1 and created[0].closed
    assert session.calls[0][1]["timeout"] == 600
    supplied = Cancellation()
    assert llama_client.count_chat_tokens(**arguments, cancel=supplied) == 10
    assert not supplied.closed
    supplied.event.set()
    with pytest.raises(RequestCancelled):
        llama_client.count_chat_tokens(**arguments, cancel=supplied)
    assert len(session.calls) == 2


def test_cancelled_count_cannot_publish_a_late_result_or_bypass_endpoint_policy(monkeypatch):
    """A late count must stay cancelled; unauthorized URLs never get a POST."""
    request = RequestCancellation()
    session = _Session(_Response({"input_tokens": 10}))
    session.before_return = request.cancel
    _install_session(monkeypatch, session)
    with pytest.raises(RequestCancelled):
        llama_client.count_chat_tokens(
            base_url="http://127.0.0.1:65535", messages=[], cancel=request
        )
    assert session.response.closed
    assert len(session.calls) == 1
    monkeypatch.setattr(config, "MODEL_REMOTE_OK", False)
    with pytest.raises(endpoint_policy.EndpointPolicyError):
        llama_client.count_chat_tokens(base_url="https://other.invalid", messages=[])
    assert len(session.calls) == 1
