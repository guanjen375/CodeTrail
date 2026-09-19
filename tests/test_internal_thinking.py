"""Internal generators explicitly disable both parser and template thinking."""
from __future__ import annotations

import copy

import pytest

import config
import context_budget
import context_generation
import llama_client
from extracted_document import ExtractedDocument
from scripts import mcp_catalog
from scripts import tool_call_canary


pytestmark = pytest.mark.smoke


def test_chunk_and_summary_requests_disable_thinking_and_invalidate_older_cached_modes(tmp_path, monkeypatch):
    sent, fingerprints = [], []
    off = {"enable_thinking": False, "thinking": False}
    original_fingerprint = context_generation.generation_fingerprint

    def fingerprint(**kwargs):
        fingerprints.append(copy.deepcopy(kwargs))
        return original_fingerprint(**kwargs)

    def request(_session, method, url, *, timeout, json_body):
        assert method == "POST" and url.endswith("/v1/chat/completions")
        sent.append(copy.deepcopy(json_body))
        return {
            "choices": [{"message": {"content": "synthetic context"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }

    monkeypatch.setattr(context_generation, "model_identity", lambda *_a, **_k: {"n_ctx": 131072})
    monkeypatch.setattr(context_generation, "generation_fingerprint", fingerprint)
    monkeypatch.setattr(context_generation, "_request_json", request)
    monkeypatch.setattr(context_budget, "log_metrics", lambda *_a, **_k: None)
    generator = context_generation.ContextGenerator(
        kb_path=tmp_path / "knowledge.json", cache_dir=tmp_path / "cache",
        model="synthetic-model", n_ctx=131072,
    )
    document = ExtractedDocument(
        raw_text="Synthetic document text.", source="synthetic.txt",
        chunks=[{"content": "Synthetic document text.", "source": "synthetic.txt", "page": 1,
                 "chunk_index": 0, "char_start": 0, "char_end": 24}],
    )
    try:
        generator.generate_for_document(document)
        assert generator._summarize_segment("summary source", budget_tokens=64, extra={})
        assert len(sent) == 2 and {entry["kind"] for entry in fingerprints} == {"chunk", "summary"}
        for payload in sent:
            assert payload["chat_template_kwargs"] == off
        for entry in fingerprints:
            assert entry["params"]["chat_template_kwargs"] == off
            old_mode = copy.deepcopy(entry)
            old_mode["params"].pop("chat_template_kwargs")
            assert original_fingerprint(**old_mode) != original_fingerprint(**entry)
        generator.generate_for_document(document)
        generator._summarize_segment("summary source", budget_tokens=64, extra={})
        assert len(sent) == 2
    finally:
        generator.close()


@pytest.mark.parametrize("streaming", [False, True])
def test_legacy_agent_requests_explicitly_disable_thinking(monkeypatch, streaming):
    import agent

    sent = []
    messages = [{"role": "user", "content": "synthetic question"}]
    usage = context_budget.build_usage(
        source="synthetic-agent", requested_num_ctx=131072, messages=messages,
        model="synthetic", reserved_output_tokens=100,
    )
    monkeypatch.setattr(config, "require_main_model", lambda: "synthetic")
    monkeypatch.setattr(agent, "get_native_tools", lambda: [])
    monkeypatch.setattr(agent, "_pre_send_trim_if_needed", lambda *_a: (usage, {}))
    monkeypatch.setattr(agent, "_last_trim_summary", lambda: {})
    monkeypatch.setattr(context_budget, "emit_pre_call_lines", lambda *_a: None)
    monkeypatch.setattr(context_budget, "emit_post_call_line", lambda *_a: None)
    monkeypatch.setattr(context_budget, "log_metrics", lambda *_a: None)

    def chat(**kwargs):
        sent.append(kwargs)
        if kwargs["stream"]:
            return iter([{"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]}])
        return {"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(llama_client, "chat_completions", chat)
    if streaming:
        assert agent.call_llm_with_tools_stream(messages) == "answer"
    else:
        assert agent.call_llm_with_tools(messages)["content"] == "answer"
    assert len(sent) == 1
    assert sent[0]["extra"]["chat_template_kwargs"] == {"enable_thinking": False, "thinking": False}


def test_catalog_probe_generation_and_template_counts_share_explicit_off_mode():
    catalog = mcp_catalog.measure_catalog(
        [{"name": "read_file", "description": "read", "inputSchema": {"type": "object"}}],
        instructions="synthetic routing instructions", source="synthetic",
    )
    requests = []

    def post(path, payload):
        requests.append((path, copy.deepcopy(payload)))
        if path == "/v1/chat/completions":
            return {"choices": []}
        if path == "/apply-template":
            size = 1 + int("tools" in payload) + int(payload["messages"][0]["role"] == "system")
            return {"prompt": "x" * size}
        assert path == "/tokenize"
        return {"tokens": list(payload["content"])}

    measured = mcp_catalog.measure_catalog_prompt_tokens(model="synthetic", catalog=catalog, post_json=post)
    assert measured.catalog_prompt_tokens == 2
    chat_and_templates = [(path, payload) for path, payload in requests if path != "/tokenize"]
    assert len(chat_and_templates) == 6
    for _path, payload in chat_and_templates:
        assert payload["chat_template_kwargs"] == {"enable_thinking": False, "thinking": False}


def test_canary_fingerprint_includes_the_explicit_internal_off_mode(tmp_path, monkeypatch):
    original_digest = tool_call_canary._json_digest
    captured = []

    def digest(value):
        if "canary_version" in value:
            captured.append(copy.deepcopy(value))
        return original_digest(value)

    monkeypatch.setattr(tool_call_canary, "_json_digest", digest)
    tool_call_canary.build_fingerprint(root=tmp_path, selected_model="synthetic", props={}, env={})
    assert len(captured) == 1
    payload = captured[0]
    assert payload["chat_template_kwargs"] == {"enable_thinking": False, "thinking": False}
    previous_mode = copy.deepcopy(payload)
    previous_mode.pop("chat_template_kwargs")
    assert original_digest(previous_mode) != original_digest(payload)
