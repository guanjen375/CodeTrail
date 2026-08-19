"""VL 圖片管線回歸測試；完全離線，不需要 llama-server。"""
from __future__ import annotations

import pytest


class _FakeResp:
    status_code = 200

    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _CapturingSession:
    def __init__(self, payload: dict):
        self._payload = payload
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None, stream=False, allow_redirects=True):
        self.calls.append(
            {
                "url": url,
                "json": json,
                "timeout": timeout,
                "stream": stream,
            }
        )
        return _FakeResp(self._payload)


def test_vision_completion_uses_current_llamacpp_image_url_api(monkeypatch):
    import llama_client

    session = _CapturingSession(
        {"choices": [{"message": {"role": "assistant", "content": "看到了"}}]}
    )
    monkeypatch.setattr(llama_client, "get_session", lambda: session)

    result = llama_client.vision_completion(
        base_url="http://127.0.0.1:8083",
        prompt="忠實分析圖片",
        image_base64="YWJj",
        mime_type="image/png",
        model="vl-model",
        max_tokens=321,
        timeout=45,
    )

    assert result == "看到了"
    call = session.calls[0]
    assert call["url"] == "http://127.0.0.1:8083/v1/chat/completions"
    assert call["timeout"] == 45
    body = call["json"]
    assert body["max_tokens"] == 321
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "image_data" not in body
    parts = body["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "忠實分析圖片"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,YWJj"


def test_vision_completion_requires_finite_output_budget():
    import llama_client

    with pytest.raises(ValueError, match="max_tokens"):
        llama_client.vision_completion(
            base_url="http://127.0.0.1:8083",
            prompt="x",
            image_base64="YWJj",
            max_tokens=-1,
        )


def test_legacy_native_image_data_fails_loud():
    import llama_client

    with pytest.raises(ValueError, match="vision_completion"):
        llama_client.native_completion(
            base_url="http://127.0.0.1:8083",
            prompt="x",
            image_data=[{"id": 10, "data": "YWJj"}],
        )


def test_analyze_image_is_general_and_bounded(monkeypatch, tmp_path):
    import config
    import media

    image = tmp_path / "screen.png"
    image.write_bytes(b"not-a-real-png")
    media.set_sandbox_root(str(tmp_path), allow_external=False)
    captured: dict = {}

    def fake_vision(**kwargs):
        captured.update(kwargs)
        return "通用圖片分析結果"

    monkeypatch.setattr(media.llama_client, "vision_completion", fake_vision)

    assert media.ocr_image("screen.png") == "通用圖片分析結果"
    assert captured["max_tokens"] == config.VL_ANALYZE_MAX_TOKENS
    assert captured["timeout"] == config.VL_ANALYZE_TIMEOUT
    assert captured["mime_type"] == "image/png"
    assert "終端機" in captured["prompt"]
    assert "架構圖" in captured["prompt"]
    assert "照片" in captured["prompt"]
    assert "不要猜測" in captured["prompt"]


@pytest.mark.parametrize(
    "extractor_name",
    ["extract_chat_from_screenshot", "extract_info_from_image"],
)
def test_rag_image_extractors_use_larger_bounded_budget(
    monkeypatch,
    tmp_path,
    extractor_name,
):
    import config
    import RAG

    image = tmp_path / "screen.webp"
    image.write_bytes(b"not-a-real-webp")
    captured: dict = {}

    def fake_vision(**kwargs):
        captured.update(kwargs)
        return "可入庫的圖片分析"

    monkeypatch.setattr(RAG.llama_client, "vision_completion", fake_vision)
    extractor = getattr(RAG, extractor_name)

    assert extractor(str(image)) == "可入庫的圖片分析"
    assert captured["max_tokens"] == config.VL_INGEST_MAX_TOKENS
    assert captured["timeout"] == config.VL_INGEST_TIMEOUT
    assert captured["mime_type"] == "image/webp"


def test_http_client_does_not_retry_generation_read_timeouts():
    import http_client

    session = http_client.create_session()
    try:
        retries = session.get_adapter("http://").max_retries
        assert retries.connect == http_client.RETRY_TOTAL
        assert retries.status == http_client.RETRY_TOTAL
        assert retries.read == 0
        assert retries.other == 0
    finally:
        session.close()
