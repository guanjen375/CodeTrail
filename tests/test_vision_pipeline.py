"""VL 圖片管線回歸測試；完全離線，不需要 llama-server。"""
from __future__ import annotations

import copy

import pytest

# smoke:AGENTS.md §2.1 第 1 款「真實發生過的 bug 的 regression」
# 真實 bug regression:VL 流程。
pytestmark = pytest.mark.smoke


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


class _BombSession:
    """任何 HTTP 動作都是失敗:參數驗證與 endpoint policy 必須在送出之前 raise。"""

    def post(self, *args, **kwargs):
        raise AssertionError("HTTP 不該被送出;檢查必須在 post() 之前 fail-loud")

    def get(self, *args, **kwargs):
        raise AssertionError("HTTP 不該被送出;檢查必須在 get() 之前 fail-loud")


# ------------------------------------------------------------
# structured output(vision_json_completion)共用 fixture
#
# 這裡刻意**不 import figure_extract**:T1 的契約是「收到什麼 wrapper 就原樣送出」,
# 測試自備一份形狀正確的 wrapper 才驗得到這件事。
# ------------------------------------------------------------
def _response_format(name: str = "figure_table") -> dict:
    """每次呼叫回一份全新的 nested json_schema wrapper(避免跨測試共用被改到)。"""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["columns", "rows", "footnotes"],
                "properties": {
                    "columns": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["label"],
                            "properties": {"label": {"type": "string"}},
                        },
                    },
                    "rows": {"type": "array", "items": {"type": "string"}},
                    "footnotes": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    }


def _vl_json_session(content: str = "{}", finish_reason="stop", usage=None):
    """假 VL server:回一個 structured-output 形狀的 chat completion。

    finish_reason=None 代表「server 根本沒回這個欄位」。
    """
    choice: dict = {"message": {"role": "assistant", "content": content}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    payload: dict = {"choices": [choice]}
    if usage is not None:
        payload["usage"] = usage
    return _CapturingSession(payload)


# CONTRACT §6.1 的三組 key 集合,**獨立寫死**在測試裡(不從實作反推,也不對實作
# 的模組常數做任何斷言 —— 那些是私有實作細節)。它們只用來驅動下面的行為測試:
# 白名單每個鍵都要送得進 payload、受保護鍵每個都要被拒、契約外的鍵一律被拒。
# 漏鍵 → 合法呼叫被拒 → forwarding 測試紅;多鍵 → 保護破口 → 拒絕測試紅。
_CONTRACT_SAMPLER_KEYS = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "seed",
        "repeat_penalty",
        "presence_penalty",
        "frequency_penalty",
        "typical_p",
        "dynatemp_range",
        "dynatemp_exponent",
        "mirostat",
        "mirostat_tau",
        "mirostat_eta",
    }
)

_CONTRACT_VISION_JSON_PROTECTED_KEYS = frozenset(
    {
        "model",
        "messages",
        "image",
        "image_data",
        "stream",
        "max_tokens",
        "response_format",
        "tools",
        "tool_choice",
        "cache_prompt",
        "chat_template_kwargs",
        "prompt",
    }
)

_CONTRACT_CHAT_EXTRA_PROTECTED_KEYS = frozenset({"model", "messages", "stream"})

# 每個合法取樣鍵至少要通過一次 payload forwarding;temperature / top_p / top_k
# 的值刻意與 base payload 不同,才驗得到「override 真的蓋掉預設」。
_SAMPLER_VALUES = {
    "temperature": 0.7,
    "top_p": 0.8,
    "top_k": 5,
    "min_p": 0.05,
    "seed": 1234,
    "repeat_penalty": 1.1,
    "presence_penalty": 0.2,
    "frequency_penalty": 0.3,
    "typical_p": 0.9,
    "dynatemp_range": 0.4,
    "dynatemp_exponent": 1.5,
    "mirostat": 2,
    "mirostat_tau": 5.0,
    "mirostat_eta": 0.1,
}

# 受保護鍵的測試值刻意**與 base payload 相同**(model/stream/max_tokens/
# cache_prompt/chat_template_kwargs),證明拒絕是 presence-based,
# 不是「值不一樣才拒絕」的寬鬆實作。
_PROTECTED_SAMPLER_VALUES = {
    "model": "vl-model",
    "messages": [],
    "image": "YWJj",
    "image_data": [],
    "stream": False,
    "max_tokens": 128,
    "response_format": None,
    "tools": [],
    "tool_choice": "auto",
    "cache_prompt": True,
    "chat_template_kwargs": {"enable_thinking": False},
    "prompt": "x",
}


def _call_vision_json(session, monkeypatch, **overrides):
    """把常用參數填好的 vision_json_completion 呼叫。"""
    import llama_client

    monkeypatch.setattr(llama_client, "get_session", lambda: session)
    kwargs = {
        "base_url": "http://127.0.0.1:8083",
        "prompt": "轉錄這張表",
        "image_base64": "YWJj",
        "mime_type": "image/png",
        "model": "vl-model",
        "max_tokens": 128,
        "response_format": _response_format(),
    }
    kwargs.update(overrides)
    return llama_client.vision_json_completion(**kwargs)


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


# ============================================================
# T1 ① 舊 vision lane:不帶 structured output 的 payload 逐鍵不變
# ============================================================
def test_vision_completion_payload_is_key_for_key_unchanged(monkeypatch):
    """workflow §5「tool / runtime contract ①」:舊 payload 逐鍵一致。

    這條刻意把整個 body 寫死(含 temperature=0.1 / top_p=0.95 / top_k=40 這些預設值)。
    少一鍵、多一鍵、任何值變了都會紅燈 —— 那就是刻意的 contract change,要先過使用者,
    不能靠 refactor 順手改掉。
    """
    import llama_client

    session = _CapturingSession(
        {"choices": [{"message": {"role": "assistant", "content": "看到了"}}]}
    )
    monkeypatch.setattr(llama_client, "get_session", lambda: session)

    llama_client.vision_completion(
        base_url="http://127.0.0.1:8083",
        prompt="忠實分析圖片",
        image_base64="YWJj",
        mime_type="image/png",
        model="vl-model",
        max_tokens=321,
        timeout=45,
    )

    expected = {
        "model": "vl-model",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "忠實分析圖片"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,YWJj"},
                    },
                ],
            }
        ],
        "temperature": 0.1,
        "stream": False,
        "cache_prompt": True,
        "top_p": 0.95,
        "top_k": 40,
        "max_tokens": 321,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    call = session.calls[0]
    body = call["json"]
    assert sorted(body) == sorted(expected), "舊 vision payload 的頂層 key 集合變了"
    assert body == expected
    assert "response_format" not in body
    assert call["url"] == "http://127.0.0.1:8083/v1/chat/completions"
    assert call["timeout"] == 45
    assert call["stream"] is False


# ============================================================
# T1 ② nested JSON Schema payload 正確送出
# ============================================================
def test_vision_json_completion_payload_is_key_for_key_exact(monkeypatch):
    """無 override 的 structured 呼叫,body 必須恰好是這 10 鍵。

    抽查幾個欄位不夠:意外多送 image_data / tools / prompt / grammar 之類的頂層鍵
    會改變 server 行為(例如把 grammar 換掉),卻不會讓抽查式斷言變紅。
    """
    session = _vl_json_session('{"columns": []}')
    rf = _response_format()
    rf_before = copy.deepcopy(rf)

    _call_vision_json(session, monkeypatch, response_format=rf, timeout=300)

    expected = {
        "model": "vl-model",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "轉錄這張表"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,YWJj"},
                    },
                ],
            }
        ],
        "temperature": 0.0,
        "stream": False,
        "cache_prompt": True,
        "top_p": 1.0,
        "top_k": 1,
        "max_tokens": 128,
        "response_format": rf_before,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    call = session.calls[0]
    body = call["json"]
    assert sorted(body) == sorted(expected), "structured payload 的頂層 key 集合不對"
    assert body == expected
    assert call["url"] == "http://127.0.0.1:8083/v1/chat/completions"
    assert call["timeout"] == 300
    assert call["stream"] is False
    # wrapper 原樣送出:值完全相同,且呼叫端傳進來的物件沒有被改寫。
    assert body["response_format"] == rf_before
    assert rf == rf_before, "response_format 不得被就地改寫"


def test_vision_json_completion_image_part_matches_legacy_vision_lane(monkeypatch):
    """兩條 vision 路徑的 image content part 必須完全一樣。

    形狀漂移是無聲的:舊的 top-level image_data 被新版 server 忽略後,VL 只看文字
    prompt 就照樣自信作答。structured lane 若漂移,回來的會是「通過 grammar 的合法
    JSON,但整片是幻覺」,更難察覺。
    """
    import llama_client

    legacy_session = _CapturingSession(
        {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    )
    monkeypatch.setattr(llama_client, "get_session", lambda: legacy_session)
    llama_client.vision_completion(
        base_url="http://127.0.0.1:8083",
        prompt="同一段 prompt",
        image_base64="YWJj",
        mime_type="image/webp",
        model="vl-model",
        max_tokens=64,
    )

    json_session = _vl_json_session()
    _call_vision_json(
        json_session,
        monkeypatch,
        prompt="同一段 prompt",
        image_base64="YWJj",
        mime_type="image/webp",
    )

    assert (
        legacy_session.calls[0]["json"]["messages"]
        == json_session.calls[0]["json"]["messages"]
    )
    assert (
        json_session.calls[0]["json"]["messages"][0]["content"][1]["image_url"]["url"]
        == "data:image/webp;base64,YWJj"
    )


def test_vision_json_completion_keeps_existing_data_url_as_is(monkeypatch):
    session = _vl_json_session()
    _call_vision_json(
        session, monkeypatch, image_base64="data:image/png;base64,YWJj"
    )
    parts = session.calls[0]["json"]["messages"][0]["content"]
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,YWJj"


# ============================================================
# T1 ② 白名單:合法鍵可覆寫、受保護鍵 ValueError
# ============================================================
def test_contract_key_fixtures_are_complete_and_disjoint():
    """測試檔自備的三組集合要完整、互斥,而且被值表完整覆蓋。

    這條**完全不碰實作**:白名單與受保護鍵對不對,由下面的行為測試證明 ——
    每個白名單鍵送得進 payload、每個受保護鍵被拒、契約外的鍵一律被拒。
    這裡只保證那幾個 parametrize 迴圈真的跑遍每一個鍵;少一個鍵沒被覆蓋,
    行為測試就等於沒測到它,而那是靜默的。
    """
    assert len(_CONTRACT_SAMPLER_KEYS) == 14
    assert len(_CONTRACT_VISION_JSON_PROTECTED_KEYS) == 12
    assert len(_CONTRACT_CHAT_EXTRA_PROTECTED_KEYS) == 3
    # 白名單與受保護鍵不得重疊,否則同一個鍵的判定取決於檢查順序。
    assert not (_CONTRACT_SAMPLER_KEYS & _CONTRACT_VISION_JSON_PROTECTED_KEYS)
    # 兩張值表必須完整覆蓋各自的集合,才不會有鍵從沒被實際送出 / 拒絕過。
    assert set(_SAMPLER_VALUES) == _CONTRACT_SAMPLER_KEYS
    assert set(_PROTECTED_SAMPLER_VALUES) == _CONTRACT_VISION_JSON_PROTECTED_KEYS


@pytest.mark.parametrize("key", sorted(_CONTRACT_SAMPLER_KEYS))
def test_vision_json_completion_forwards_every_whitelisted_sampler_key(monkeypatch, key):
    """14 個合法取樣鍵每一個都要真的進 payload,且不動到 structured 骨架。"""
    session = _vl_json_session()
    value = _SAMPLER_VALUES[key]
    rf = _response_format()

    _call_vision_json(
        session, monkeypatch, response_format=rf, sampler_overrides={key: value}
    )

    body = session.calls[0]["json"]
    assert body[key] == value
    # temperature / top_p / top_k 的測試值與 base 不同 → 同時證明 override 蓋得掉預設。
    if key in ("temperature", "top_p", "top_k"):
        assert body[key] != {"temperature": 0.0, "top_p": 1.0, "top_k": 1}[key]
    # 骨架四鍵不受 override 影響。
    assert body["max_tokens"] == 128
    assert body["response_format"] == rf
    assert body["cache_prompt"] is True
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize("key", sorted(_CONTRACT_VISION_JSON_PROTECTED_KEYS))
def test_vision_json_completion_rejects_protected_sampler_overrides(monkeypatch, key):
    import llama_client

    monkeypatch.setattr(llama_client, "get_session", lambda: _BombSession())
    with pytest.raises(ValueError) as exc:
        llama_client.vision_json_completion(
            base_url="http://127.0.0.1:8083",
            prompt="轉錄這張表",
            image_base64="YWJj",
            model="vl-model",
            max_tokens=128,
            response_format=_response_format(),
            sampler_overrides={key: _PROTECTED_SAMPLER_VALUES[key]},
        )
    message = str(exc.value)
    assert "sampler_overrides" in message
    assert key in message


@pytest.mark.parametrize("key", ["grammar", "n_probs", "temperatur", "json_schema", 1])
def test_vision_json_completion_rejects_unknown_sampler_keys(monkeypatch, key):
    """白名單語意:沒列在 14 鍵裡的一律拒,不管它是不是「看起來無害」。"""
    import llama_client

    monkeypatch.setattr(llama_client, "get_session", lambda: _BombSession())
    with pytest.raises(ValueError) as exc:
        llama_client.vision_json_completion(
            base_url="http://127.0.0.1:8083",
            prompt="轉錄這張表",
            image_base64="YWJj",
            max_tokens=128,
            response_format=_response_format(),
            sampler_overrides={key: "whatever"},
        )
    assert "sampler_overrides" in str(exc.value)


@pytest.mark.parametrize(
    "bad",
    [[], "", 0, [("temperature", 0.1)], {"temperature", 0.1}, ("temperature", 0.1), 0.0],
)
def test_vision_json_completion_rejects_non_dict_sampler_overrides(monkeypatch, bad):
    """falsey 非 dict 會被「or {}」靜默當成沒有 override,pair iterable 則會被
    dict() 收下 —— 兩種都讓白名單形同虛設,必須在送出前 TypeError。"""
    import llama_client

    monkeypatch.setattr(llama_client, "get_session", lambda: _BombSession())
    with pytest.raises(TypeError) as exc:
        llama_client.vision_json_completion(
            base_url="http://127.0.0.1:8083",
            prompt="轉錄這張表",
            image_base64="YWJj",
            max_tokens=128,
            response_format=_response_format(),
            sampler_overrides=bad,
        )
    assert "sampler_overrides" in str(exc.value)


# ============================================================
# T1 ③ chat_completions(extra=...) collision policy
# ============================================================
@pytest.mark.parametrize("key", sorted(_CONTRACT_CHAT_EXTRA_PROTECTED_KEYS))
def test_chat_completions_extra_cannot_override_transport_keys(monkeypatch, key):
    """presence-based:值刻意與 base payload 完全相同,仍必須 ValueError。"""
    import llama_client

    monkeypatch.setattr(llama_client, "get_session", lambda: _BombSession())
    messages = [{"role": "user", "content": "hi"}]
    same_as_base = {"model": "local", "messages": messages, "stream": False}[key]

    with pytest.raises(ValueError) as exc:
        llama_client.chat_completions(
            base_url="http://127.0.0.1:8080",
            messages=messages,
            model="",  # → base payload 的 model 就是 "local"
            stream=False,
            extra={key: same_as_base},
        )
    message = str(exc.value)
    assert "chat_completions(extra=...)" in message
    assert key in message


@pytest.mark.parametrize("bad", [[("model", "evil")], "", 0, [], ("model", "evil")])
def test_chat_completions_extra_must_be_a_dict(monkeypatch, bad):
    """pair iterable 會讓 payload.update() 塞進 model/messages 卻繞過 key 檢查。"""
    import llama_client

    monkeypatch.setattr(llama_client, "get_session", lambda: _BombSession())
    with pytest.raises(TypeError) as exc:
        llama_client.chat_completions(
            base_url="http://127.0.0.1:8080",
            messages=[{"role": "user", "content": "hi"}],
            extra=bad,
        )
    assert "chat_completions(extra=...)" in str(exc.value)


def test_chat_completions_extra_still_allows_generation_keys(monkeypatch):
    """既有 vision_completion 的 extra 用法(max_tokens / chat_template_kwargs)
    以及 structured lane 需要的 cache_prompt / response_format 都必須照過。"""
    import llama_client

    session = _CapturingSession({"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr(llama_client, "get_session", lambda: session)
    rf = _response_format()

    llama_client.chat_completions(
        base_url="http://127.0.0.1:8080",
        messages=[{"role": "user", "content": "hi"}],
        extra={
            "max_tokens": 99,
            "chat_template_kwargs": {"enable_thinking": False},
            "cache_prompt": False,
            "response_format": rf,
            "temperature": 0.9,
        },
    )

    body = session.calls[0]["json"]
    assert body["max_tokens"] == 99
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["cache_prompt"] is False
    assert body["response_format"] == rf
    assert body["temperature"] == 0.9


# ============================================================
# T1 cache_prompt:disagreement detection 的第二次取樣要能關掉 prompt cache
# ============================================================
@pytest.mark.parametrize("cache_prompt", [True, False])
def test_vision_json_completion_cache_prompt_reaches_payload(monkeypatch, cache_prompt):
    """第二次取樣必須真的關掉 cache,否則同 prompt 同 cache 的重播被當成獨立佐證,
    disagreement detection 會一路綠燈卻毫無證據力。"""
    session = _vl_json_session()
    _call_vision_json(session, monkeypatch, cache_prompt=cache_prompt)
    assert session.calls[0]["json"]["cache_prompt"] is cache_prompt


# ============================================================
# T1 ③ 截斷與 fail-loud
# ============================================================
@pytest.mark.parametrize(
    "finish_reason, truncated",
    [("stop", False), ("eos", False), ("length", True), ("tool_calls", True), ("", True)],
)
def test_vision_json_completion_flags_truncation_by_finish_reason(
    monkeypatch, finish_reason, truncated
):
    session = _vl_json_session('{"columns": []}', finish_reason=finish_reason)
    result = _call_vision_json(session, monkeypatch)
    assert result.finish_reason == finish_reason
    assert result.truncated is truncated


def test_vision_json_completion_treats_missing_finish_reason_as_truncated(monkeypatch):
    """server 沒回 finish_reason = 無法確認模型講完了 → 保守當截斷。"""
    session = _vl_json_session('{"columns": []}', finish_reason=None)
    result = _call_vision_json(session, monkeypatch)
    assert result.finish_reason == ""
    assert result.truncated is True


def test_vision_json_result_rejects_inconsistent_truncated_flag():
    """手工建構也不能造出 finish_reason 與 truncated 互相矛盾的物件。"""
    import llama_client

    ok = llama_client.VisionJsonResult(
        text="{}", finish_reason="length", truncated=True, usage={}, raw={}
    )
    assert ok.truncated is True
    with pytest.raises(ValueError, match="inconsistent"):
        llama_client.VisionJsonResult(
            text="{}", finish_reason="length", truncated=False, usage={}, raw={}
        )
    with pytest.raises(ValueError, match="inconsistent"):
        llama_client.VisionJsonResult(
            text="{}", finish_reason="stop", truncated=True, usage={}, raw={}
        )


_FENCED_TEXT = (
    "以下是結果：\n"
    "```json\n"
    '{"columns": [{"label": "Name"}]}\n'
    "```\n"
    "以上。\n"
)


@pytest.mark.parametrize(
    "content",
    [
        '  {"columns": []}\n',
        _FENCED_TEXT,
        "",
        '{"columns": []}',
    ],
)
def test_vision_json_completion_returns_text_verbatim(monkeypatch, content):
    """text 是原文:不 strip、不剝 code fence、不從中抽 JSON 子字串。

    任何「救回」邏輯都會把「模型沒遵守 grammar」變成無聲通過 —— workflow §4 Step 1
    明令沒有自由文字 fallback。空字串也原樣回傳(空 payload 的語意判定歸抽取端)。
    """
    usage = {"prompt_tokens": 7, "completion_tokens": 3}
    session = _vl_json_session(content, usage=usage)
    result = _call_vision_json(session, monkeypatch)

    assert result.text == content
    assert result.usage == usage
    assert result.raw == {
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }
    assert result.truncated is False


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": "nope"},
        {"choices": ["not-a-dict"]},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": {"text": "SECRET-CONTENT"}}}]},
        {
            "choices": [
                {
                    "message": {
                        "content": [{"type": "text", "text": "SECRET-CONTENT"}]
                    }
                }
            ]
        },
        "not-a-dict",
    ],
)
def test_vision_json_completion_fails_loud_on_bad_response_shape(monkeypatch, payload):
    """content 不是 str 一律 fail-loud;特別是 content parts —— 把它們併成一段
    文字就是在做自由文字救回。錯誤訊息不得回顯內容(可能含 NDA)。"""
    session = _CapturingSession(payload)
    with pytest.raises(RuntimeError) as exc:
        _call_vision_json(session, monkeypatch)
    assert "SECRET-CONTENT" not in str(exc.value)


# ============================================================
# T1 送出前的參數驗證(全部在任何 HTTP 動作之前)
# ============================================================
@pytest.mark.parametrize(
    "bad_response_format",
    [
        {},
        {"type": "json_object"},
        {"type": "json_schema"},
        {"type": "json_schema", "json_schema": "nope"},
        {"type": "json_schema", "json_schema": {}},
        {"type": "json_schema", "json_schema": {"name": "", "strict": True, "schema": {"type": "object"}}},
        {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}},
        {"type": "json_schema", "json_schema": {"name": "x", "strict": False, "schema": {"type": "object"}}},
        {"type": "json_schema", "json_schema": {"name": "x", "strict": True, "schema": "nope"}},
        {"type": "json_schema", "json_schema": {"name": "x", "strict": True, "schema": {}}},
        {"type": "json_schema", "json_schema": {"name": "x", "strict": True, "schema": {"properties": {}}}},
        # 把內層 schema 直接當 wrapper 傳(最常見的組錯)
        {"type": "object", "properties": {}},
    ],
)
def test_vision_json_completion_rejects_malformed_response_format(
    monkeypatch, bad_response_format
):
    import llama_client

    monkeypatch.setattr(llama_client, "get_session", lambda: _BombSession())
    with pytest.raises(ValueError):
        llama_client.vision_json_completion(
            base_url="http://127.0.0.1:8083",
            prompt="轉錄這張表",
            image_base64="YWJj",
            max_tokens=128,
            response_format=bad_response_format,
        )


@pytest.mark.parametrize("bad_response_format", [None, "json_schema", [], 0])
def test_vision_json_completion_requires_dict_response_format(
    monkeypatch, bad_response_format
):
    """response_format 的**所有**問題(含非 dict)都是 ValueError。

    它是 schema 產生端交過來的資料,呼叫端 catch 一個 ValueError 就要能涵蓋整個
    response_format 驗證;混用 TypeError 會讓只 catch ValueError 的呼叫端走進
    完全不同的控制流。
    """
    import llama_client

    monkeypatch.setattr(llama_client, "get_session", lambda: _BombSession())
    with pytest.raises(ValueError, match="response_format"):
        llama_client.vision_json_completion(
            base_url="http://127.0.0.1:8083",
            prompt="轉錄這張表",
            image_base64="YWJj",
            max_tokens=128,
            response_format=bad_response_format,
        )


@pytest.mark.parametrize("bad", [0, -1])
def test_vision_json_completion_requires_finite_output_budget(monkeypatch, bad):
    import llama_client

    monkeypatch.setattr(llama_client, "get_session", lambda: _BombSession())
    with pytest.raises(ValueError, match="max_tokens"):
        llama_client.vision_json_completion(
            base_url="http://127.0.0.1:8083",
            prompt="轉錄這張表",
            image_base64="YWJj",
            max_tokens=bad,
            response_format=_response_format(),
        )


@pytest.mark.parametrize(
    "bad", [True, 1.0, "128", float("inf"), float("nan"), None]
)
def test_vision_json_completion_requires_int_output_budget(monkeypatch, bad):
    """frozen signature 寫的是 int;bool / float / inf / nan 混進去會讓
    「有限輸出預算」的保證變成空話。"""
    import llama_client

    monkeypatch.setattr(llama_client, "get_session", lambda: _BombSession())
    with pytest.raises(TypeError, match="max_tokens"):
        llama_client.vision_json_completion(
            base_url="http://127.0.0.1:8083",
            prompt="轉錄這張表",
            image_base64="YWJj",
            max_tokens=bad,
            response_format=_response_format(),
        )


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"image_base64": ""}, "image_base64"),
        ({"mime_type": "application/pdf"}, "MIME"),
        ({"mime_type": ""}, "MIME"),
    ],
)
def test_vision_json_completion_validates_image_inputs(monkeypatch, kwargs, match):
    import llama_client

    monkeypatch.setattr(llama_client, "get_session", lambda: _BombSession())
    call = {
        "base_url": "http://127.0.0.1:8083",
        "prompt": "轉錄這張表",
        "image_base64": "YWJj",
        "max_tokens": 128,
        "response_format": _response_format(),
    }
    call.update(kwargs)
    with pytest.raises(ValueError, match=match):
        llama_client.vision_json_completion(**call)


def test_vision_json_completion_enforces_model_endpoint_policy(monkeypatch):
    """新的 prompt-bearing call site 也必須被 endpoint policy 擋住。

    prompt 與圖片可能含 NDA 內容;非 loopback 端點沒 opt-in 就得在任何 HTTP
    動作之前 fail-loud。
    """
    import endpoint_policy
    import llama_client

    monkeypatch.delenv(endpoint_policy.MODEL_REMOTE_OK_ENV, raising=False)
    monkeypatch.setattr(llama_client, "get_session", lambda: _BombSession())
    with pytest.raises(endpoint_policy.EndpointPolicyError):
        llama_client.vision_json_completion(
            base_url="http://10.9.8.7:8083",
            prompt="轉錄這張表",
            image_base64="YWJj",
            max_tokens=128,
            response_format=_response_format(),
        )


# ============================================================
# T1 protected-key gate 必須 fail-closed:檢查看到的 == 送出去的
# ============================================================
class _HiddenKeyDict(dict):
    """真實儲存有 hidden key,但 ``__iter__`` 看不到它。

    這正是舊實作的洞:檢查走 ``for k in mapping``(``__iter__``,看不到),
    送出走 ``payload.update(obj)``(dict.update 對「有覆寫 __iter__ 的 dict
    subclass」會改走 ``keys()``,而 ``keys()`` 誠實)—— 於是 protected key
    通過了白名單,卻照樣被送進 payload。
    """

    def __init__(self, visible: dict, hidden: dict):
        super().__init__({**visible, **hidden})
        self._visible = dict(visible)

    def __iter__(self):
        return iter(self._visible)


class _LyingKeysDict(dict):
    """``keys()`` 說謊隱藏 hidden key,``__iter__`` 誠實(與上面互為鏡像)。"""

    def __init__(self, visible: dict, hidden: dict):
        super().__init__({**visible, **hidden})
        self._visible = dict(visible)

    def keys(self):
        return self._visible.keys()

    def items(self):
        return self._visible.items()


@pytest.mark.parametrize("cls", [_HiddenKeyDict, _LyingKeysDict])
def test_chat_completions_extra_gate_reads_real_keys_not_the_subclass_story(
    monkeypatch, cls
):
    """dict subclass 讓 __iter__ 與 keys() 說法不一致時,gate 必須 fail-closed。

    正確行為是「以真實儲存為準 → 看見 protected key → ValueError → 零 HTTP」,
    而不是「檢查沒看到 → 照送」。
    """
    import llama_client

    monkeypatch.setattr(llama_client, "get_session", lambda: _BombSession())
    with pytest.raises(ValueError) as exc:
        llama_client.chat_completions(
            base_url="http://127.0.0.1:8080",
            messages=[{"role": "user", "content": "hi"}],
            extra=cls({"max_tokens": 8}, {"model": "evil"}),
        )
    assert "model" in str(exc.value)


@pytest.mark.parametrize("cls", [_HiddenKeyDict, _LyingKeysDict])
def test_vision_json_sampler_gate_reads_real_keys_not_the_subclass_story(
    monkeypatch, cls
):
    import llama_client

    monkeypatch.setattr(llama_client, "get_session", lambda: _BombSession())
    with pytest.raises(ValueError) as exc:
        llama_client.vision_json_completion(
            base_url="http://127.0.0.1:8083",
            prompt="轉錄這張表",
            image_base64="YWJj",
            max_tokens=128,
            response_format=_response_format(),
            sampler_overrides=cls({"seed": 1}, {"response_format": {"type": "none"}}),
        )
    message = str(exc.value)
    assert "sampler_overrides" in message
    assert "response_format" in message


def test_chat_completions_snapshots_extra_against_later_mutation(monkeypatch):
    """驗過之後,呼叫端(或另一條 thread)再改原 dict 都影響不到已送出的內容。"""
    import llama_client

    session = _CapturingSession({"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr(llama_client, "get_session", lambda: session)
    extra = {"max_tokens": 99, "chat_template_kwargs": {"enable_thinking": False}}

    llama_client.chat_completions(
        base_url="http://127.0.0.1:8080",
        messages=[{"role": "user", "content": "hi"}],
        extra=extra,
    )

    body = session.calls[0]["json"]
    extra["max_tokens"] = 1
    extra["chat_template_kwargs"]["enable_thinking"] = True
    extra["model"] = "evil"
    assert body["max_tokens"] == 99
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["model"] == "local"


def test_vision_json_completion_snapshots_inputs_against_later_mutation(monkeypatch):
    """response_format 與 sampler_overrides 都要遞迴脫鉤。

    只做淺層 snapshot 的話,``rf["json_schema"]["strict"] = False`` 這種深層改動
    仍然改得到已驗證的 payload —— grammar 約束就無聲消失了。
    """
    session = _vl_json_session()
    rf = _response_format()
    overrides = {"seed": 1}

    _call_vision_json(
        session, monkeypatch, response_format=rf, sampler_overrides=overrides
    )

    body = session.calls[0]["json"]
    rf["json_schema"]["strict"] = False
    rf["json_schema"]["name"] = "hijacked"
    rf["json_schema"]["schema"]["additionalProperties"] = True
    overrides["seed"] = 999

    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["name"] == "figure_table"
    assert (
        body["response_format"]["json_schema"]["schema"]["additionalProperties"]
        is False
    )
    assert body["seed"] == 1


# ============================================================
# T1 錯誤訊息不得洩漏 value(prompt / messages / 文件內容可能夾在裡面)
# ============================================================
_NDA_SENTINEL = "NDA-ONLY-IN-VALUE-9f3c"


def test_override_rejection_messages_never_leak_values(monkeypatch):
    """三條拒絕路徑的訊息都只能出現 key,不得出現 value。

    只斷言「訊息含 key」不夠:日後若改成印整個 mapping 的 repr,那些斷言照樣綠,
    但 prompt / messages / 文件內容就跟著被印進 log 與例外鏈了。
    """
    import llama_client

    monkeypatch.setattr(llama_client, "get_session", lambda: _BombSession())
    messages = [{"role": "user", "content": _NDA_SENTINEL}]

    with pytest.raises(ValueError) as chat_exc:
        llama_client.chat_completions(
            base_url="http://127.0.0.1:8080",
            messages=messages,
            extra={"messages": messages},
        )
    assert _NDA_SENTINEL not in str(chat_exc.value)

    with pytest.raises(ValueError) as protected_exc:
        llama_client.vision_json_completion(
            base_url="http://127.0.0.1:8083",
            prompt="轉錄這張表",
            image_base64="YWJj",
            max_tokens=128,
            response_format=_response_format(),
            sampler_overrides={"prompt": _NDA_SENTINEL},
        )
    assert _NDA_SENTINEL not in str(protected_exc.value)

    with pytest.raises(ValueError) as unknown_exc:
        llama_client.vision_json_completion(
            base_url="http://127.0.0.1:8083",
            prompt="轉錄這張表",
            image_base64="YWJj",
            max_tokens=128,
            response_format=_response_format(),
            sampler_overrides={"grammar": _NDA_SENTINEL},
        )
    assert _NDA_SENTINEL not in str(unknown_exc.value)
