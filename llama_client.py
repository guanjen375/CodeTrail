#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llama_client — CodeTrail 對 llama.cpp llama-server 的薄封裝。

設計守則
- 一個角色一個 server / port:主 LLM (8080) / embedding (8081) / reranker (8082) / VL (8083)。
- 兩種 endpoint 都用:
    /v1/chat/completions  → tool-calling 流(agent.py 用)
    /completion           → 純文字生成 + 完整 sampling 參數 + stream(utils.py 用)
    /embedding            → embedding
    /reranking            → reranker (含 cross-encoder score)
    /props /slots /health → metadata 與 ready 檢查
- 本檔不做任何 retry / context budget 邏輯,呼叫端負責。
- 不抓 exception:讓底層 requests 例外往上拋,呼叫端轉成中文錯誤訊息。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import urlparse

import endpoint_policy
from http_client import get_session


# credentials 遮蔽集中在 endpoint_policy(policy 錯誤本身也要乾淨,見該處)
_redact_url = endpoint_policy.redact_url


def _ensure_allowed(url: str) -> None:
    """所有 llama-server 呼叫送出前的端點 policy(role="model")。

    loopback 無條件放行;非 loopback 需要 AICODE_MODEL_REMOTE_OK=1,否則
    fail-loud。做在每個 call site 而不是 session 層:session 擋不住新增
    call site 忘記掛 policy 的情況。
    """
    endpoint_policy.ensure_allowed(url, "model")


def _reject_redirect(resp, url: str) -> None:
    """3xx 一律 fail-loud。訊息含 status 與 Location 的 **host**(用 .hostname,
    不用 netloc —— netloc 可能帶 user:password@),request URL 內嵌的
    credentials 也遮蔽;絕不含 request body。"""
    if 300 <= resp.status_code < 400:
        location_host = urlparse(resp.headers.get("Location", "")).hostname or "?"
        raise RuntimeError(
            f"llama-server request to {_redact_url(url)} was redirected "
            f"(HTTP {resp.status_code} -> host {location_host!r}); "
            "refusing to follow redirects (request body was not resent)"
        )


# ============================================================
# Native /completion  (用於 utils.call_llm / call_llm_stream / VL)
# ============================================================
def native_completion(
    *,
    base_url: str,
    prompt: str,
    n_predict: int = -1,
    temperature: float = 0.2,
    top_p: float = 0.95,
    top_k: int = 40,
    min_p: float | None = None,
    stream: bool = False,
    stop: list[str] | None = None,
    image_data: list[dict] | None = None,
    extra: dict | None = None,
    timeout: int = 600,
):
    """Call llama-server /completion (native).

    回傳:
        stream=False → dict (parsed JSON response)
        stream=True  → 迭代器,yield 每個 chunk dict

    n_predict=-1 表示「直到 EOS / 上下文滿」。
    image_data 是舊 llama.cpp 格式，只保留參數讓舊呼叫 fail-loud；圖片請改用
    vision_completion()，它會走目前支援的 image_url content part。
    extra 是直接合併進 payload 的 raw dict,供高階參數覆寫(seed / mirostat / grammar 等)。
    """
    payload: dict[str, Any] = {
        "prompt": prompt,
        "n_predict": n_predict,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "stream": stream,
        "cache_prompt": True,
    }
    if min_p is not None:
        payload["min_p"] = min_p
    if stop:
        payload["stop"] = stop
    if image_data:
        raise ValueError(
            "native_completion(image_data=...) is obsolete and may be silently ignored "
            "by current llama.cpp; use vision_completion()"
        )
    if extra:
        payload.update(extra)

    session = get_session()
    url = base_url.rstrip("/") + "/completion"
    _ensure_allowed(url)

    if not stream:
        resp = session.post(url, json=payload, timeout=timeout, allow_redirects=False)
        _reject_redirect(resp, url)
        resp.raise_for_status()
        return resp.json()

    resp = session.post(url, json=payload, timeout=timeout, stream=True, allow_redirects=False)
    _reject_redirect(resp, url)
    resp.raise_for_status()
    return _iter_native_stream(resp)


def _iter_native_stream(resp) -> Iterator[dict]:
    """llama.cpp stream 是 SSE 格式:每行 `data: {json}\\n\\n`。"""
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw:
            continue
        line = raw.strip()
        if line.startswith("data:"):
            line = line[len("data:"):].strip()
        if not line or line == "[DONE]":
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


# ============================================================
# OpenAI-compat /v1/chat/completions (用於 agent.py tool-calling)
# ============================================================
# payload 覆寫政策 —— 本檔刻意有**兩個不同的嚴格度**,不要為了「一致」把其中一邊拉齊:
#
#   chat_completions(extra=...)
#       只擋 _CHAT_EXTRA_PROTECTED_KEYS 這三個 transport 鍵。這是通用逃生口:
#       換掉 model/messages 等於偷換送出去的內容,換掉 stream 等於偷換回傳形狀
#       (呼叫端拿到 iterator 卻當 dict 解析)。除此之外的 generation 參數
#       (max_tokens / cache_prompt / response_format / chat_template_kwargs / sampler)
#       都還能覆寫,因為各呼叫端有正當需求。
#
#   vision_json_completion(sampler_overrides=...)
#       白名單:只收 _SAMPLER_OVERRIDE_ALLOWED_KEYS 的取樣鍵,其餘(含
#       _VISION_JSON_PROTECTED_KEYS 與任何沒列到的鍵)一律拒絕。structured 抽取的
#       payload 骨架(JSON schema / 輸出預算 / prompt cache / chat template)是契約
#       的一部分,不是呼叫端可調的旋鈕 —— 能改 response_format 就等於能把 grammar
#       約束整個關掉,而回應仍然「看起來正常」。
_CHAT_EXTRA_PROTECTED_KEYS = frozenset({"model", "messages", "stream"})

# 只有這些鍵可以經 sampler_overrides 進入 structured vision payload。
_SAMPLER_OVERRIDE_ALLOWED_KEYS = frozenset({
    "temperature", "top_p", "top_k", "min_p", "seed", "repeat_penalty",
    "presence_penalty", "frequency_penalty", "typical_p",
    "dynatemp_range", "dynatemp_exponent",
    "mirostat", "mirostat_tau", "mirostat_eta",
})

# sampler_overrides 明確拒絕的鍵。白名單本身已經擋掉這些,這份清單存在的目的是
# 讓錯誤訊息能說「這是受保護鍵」而不是含糊的「不認得的鍵」。
_VISION_JSON_PROTECTED_KEYS = frozenset({
    "model", "messages", "image", "image_data", "stream", "max_tokens",
    "response_format", "tools", "tool_choice", "cache_prompt",
    "chat_template_kwargs", "prompt",
})

# finish_reason 屬於這一組才算「模型自己說完了」。其餘(含缺欄位)一律視為截斷。
_COMPLETE_FINISH_REASONS = frozenset({"stop", "eos"})


def _format_keys(keys) -> str:
    """把 key 集合排成穩定、可讀、型別安全的字串(給錯誤訊息用)。

    刻意**只放 key 的 repr,不放 value**:extra / sampler_overrides 的值可能夾帶
    prompt 或文件內容,而這些錯誤訊息會被印出並往上拋(比照 _reject_redirect 的
    「訊息絕不含 request body」)。用 repr 排序也讓 non-str key 不會在 sorted()
    撞 TypeError。
    """
    return ", ".join(sorted(repr(k) for k in keys))


def _json_snapshot(value):
    """遞迴複製成純 dict / list,切斷與呼叫端物件的所有別名。

    dict 一律用 ``dict.items(value)`` 讀取,**不透過** subclass 可以覆寫的
    ``items`` / ``keys`` / ``__iter__``。這是 protected-key gate 能 fail-closed 的
    前提:檢查與送出必須看到同一份、之後不會再變的內容。

    tuple 會變成 list —— 那本來就是 JSON 序列化的結果,snapshot 只是提早做。
    非容器(str / int / bool / None / 其他物件)原樣帶過,不深拷貝。
    """
    if isinstance(value, dict):
        return {k: _json_snapshot(v) for k, v in dict.items(value)}
    if isinstance(value, (list, tuple)):
        return [_json_snapshot(v) for v in value]
    return value


def _dict_arg_snapshot(value, label: str) -> dict:
    """value 必須是 dict 或 None,否則 TypeError。回傳**去別名的純 dict snapshot**。

    兩件事一起做,因為它們是同一個安全性質的兩半:

    1. 型別:刻意不用 `dict(value or {})`。`[]` / `""` / `0` 會被當成「沒有
       override」靜默放行,而 `[("model", "evil")]` 這種 pair iterable 會被 dict()
       接受 —— 兩者都讓 key 檢查形同虛設(前者跳過檢查,後者迭代出 tuple 而不是 key)。
    2. snapshot:檢查與送出**必須是同一份 plain dict**。原本兩邊都讀呼叫端物件,
       而 `for k in obj`(檢查)走 ``__iter__``、`payload.update(obj)`(送出)走
       ``keys()`` —— 只要 subclass 讓兩者說法不一致,protected key 就能通過檢查
       卻照樣被送出去;另一條 thread 在檢查與送出之間插 key 也是同一個洞。
       snapshot 之後,原物件怎麼變都影響不到已驗證的內容。
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a dict or None, got {type(value).__name__}")
    return _json_snapshot(value)


def _reject_forbidden_keys(mapping: dict, forbidden: frozenset, label: str) -> None:
    """mapping 出現任何 forbidden 的 key 就 ValueError。

    **presence-based**:只要 key 出現就拒,不管值是否與 base payload 相同。
    「值一樣就放行」的寬鬆版本會讓保護在 refactor 改動預設值時無聲失效。
    """
    bad = [k for k in mapping if k in forbidden]
    if bad:
        raise ValueError(
            f"{label} must not override protected payload keys: {_format_keys(bad)}"
        )


def chat_completions(
    *,
    base_url: str,
    messages: list[dict],
    model: str = "",
    temperature: float = 0.2,
    top_p: float | None = None,
    top_k: int | None = None,
    min_p: float | None = None,
    tools: list[dict] | None = None,
    tool_choice: str | dict = "auto",
    stream: bool = False,
    extra: dict | None = None,
    timeout: int = 600,
):
    """Call llama-server /v1/chat/completions (OpenAI compat).

    回傳:
        stream=False → OpenAI response dict (choices[0].message.{content,tool_calls})
        stream=True  → 迭代器,yield 每個 delta chunk

    model 在 llama.cpp 是 informational(server 一啟動就鎖死一顆),仍要帶,寫進 telemetry。

    top_p / top_k / min_p 預設 None = 不送,沿用 server 啟動旗標的取樣預設;
    呼叫端(agent.py)會帶入 config.CHAT_* 把 Qwen 建議值釘住。

    extra 的 collision policy(明訂):
      - extra 必須是 dict 或 None,其他型別一律 TypeError。**不**接受 pair
        iterable —— dict() 會把 [("model", "evil")] 收下,等於繞過下面的檢查。
      - extra 進來後立刻做成去別名的純 dict snapshot(遞迴),之後的檢查與送出
        都只用 snapshot。呼叫端事後改動原 dict、dict subclass 讓 __iter__ 與
        keys() 說法不一致、或另一條 thread 在中間插 key,都改不動已驗證的內容。
        (messages 不做 snapshot:它沒有「先驗後送」的落差,而且可能很大。)
      - extra **不得**帶 _CHAT_EXTRA_PROTECTED_KEYS(model / messages / stream)的
        任何一個 key,出現即 ValueError,且在任何 HTTP 動作之前。判定是
        presence-based:即使值與 base payload 完全相同也照拒。
      - 其餘 key 可以覆寫(max_tokens / cache_prompt / response_format /
        chat_template_kwargs / 各種 sampler),覆寫發生在 base payload 組完之後。
      - 這一層刻意比 vision_json_completion(sampler_overrides=...) 寬鬆,原因見
        本區塊開頭的「payload 覆寫政策」註解。
    """
    payload: dict[str, Any] = {
        "model": model or "local",
        "messages": messages,
        "temperature": temperature,
        "stream": stream,
        "cache_prompt": True,
    }
    # top_p / top_k / min_p:None 表示「不送,沿用 server 啟動旗標的取樣預設值」。
    if top_p is not None:
        payload["top_p"] = top_p
    if top_k is not None:
        payload["top_k"] = top_k
    if min_p is not None:
        payload["min_p"] = min_p
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
    # snapshot 後,下面的檢查與 update() 讀的是同一份純 dict(見 _dict_arg_snapshot)。
    extra = _dict_arg_snapshot(extra, "chat_completions(extra=...)")
    if extra:
        _reject_forbidden_keys(extra, _CHAT_EXTRA_PROTECTED_KEYS, "chat_completions(extra=...)")
        payload.update(extra)

    session = get_session()
    url = base_url.rstrip("/") + "/v1/chat/completions"
    _ensure_allowed(url)

    if not stream:
        resp = session.post(url, json=payload, timeout=timeout, allow_redirects=False)
        _reject_redirect(resp, url)
        resp.raise_for_status()
        return resp.json()

    resp = session.post(url, json=payload, timeout=timeout, stream=True, allow_redirects=False)
    _reject_redirect(resp, url)
    resp.raise_for_status()
    return _iter_openai_stream(resp)


def _vision_image_messages(prompt: str, image_base64: str, mime_type: str) -> list[dict]:
    """組出目前 llama.cpp 支援的 image content part(兩條 vision 路徑共用)。

    共用而不是各寫一份,是因為形狀漂移會無聲失效:舊的 top-level ``image_data``
    被新版 server 直接忽略,VL 模型只看得到文字 prompt,然後照樣回一段語氣自信的
    描述 —— structured lane 的版本會更難察覺(回來的是通過 grammar 的合法 JSON,
    只是內容整片是幻覺)。兩條路徑必須永遠送同一種 content part。

    image_base64 已經是 ``data:`` URL 時原樣沿用,不重複加前綴。
    """
    if not image_base64:
        raise ValueError("image_base64 must not be empty")
    if not mime_type.startswith("image/"):
        raise ValueError(f"unsupported image MIME type: {mime_type!r}")

    data_url = image_base64
    if not data_url.startswith("data:"):
        data_url = f"data:{mime_type};base64,{data_url}"

    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]


def vision_completion(
    *,
    base_url: str,
    prompt: str,
    image_base64: str,
    mime_type: str = "image/png",
    model: str = "",
    max_tokens: int = 1024,
    temperature: float = 0.1,
    top_p: float = 0.95,
    top_k: int = 40,
    timeout: int = 180,
) -> str:
    """Call a multimodal llama-server through the current OpenAI-compatible API.

    Current llama.cpp accepts images as ``image_url`` content parts on
    ``/v1/chat/completions``.  The former top-level ``image_data`` field on
    ``/completion`` is silently ignored by newer servers, which makes a VL
    model answer from the text prompt alone and hallucinate image contents.

    ``max_tokens`` is deliberately required to be finite.  A VL model that
    misses EOS must not occupy the MCP server indefinitely.

    這是**自由文字**路徑,回傳型別是 str。要 JSON schema 約束的結構化抽取請用
    vision_json_completion();本函式送出的 payload 逐鍵凍結,不加 response_format。
    """
    messages = _vision_image_messages(prompt, image_base64, mime_type)
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")

    response = chat_completions(
        base_url=base_url,
        model=model,
        messages=messages,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        stream=False,
        timeout=timeout,
        extra={
            "max_tokens": max_tokens,
            # Qwen VL 的 thinking 對忠實轉錄沒有幫助，反而會消耗輸出預算。
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )

    choices = response.get("choices") if isinstance(response, dict) else None
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("VL server returned no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part).strip()
    raise RuntimeError("VL server returned no text content")


@dataclass(frozen=True)
class VisionJsonResult:
    """structured VL 呼叫的回傳:模型輸出原文 + 是否被截斷 + 原始回應。

    欄位:
        text           模型輸出原文。**未 parse、未 strip、未做任何救回**。
        finish_reason  server 回的 finish_reason;缺欄位或型別不對 → ""。
        truncated      finish_reason not in _COMPLETE_FINISH_REASONS(缺欄位 → True)。
        usage          回應的 usage 區塊;缺少或型別不對 → {}。
        raw            整份回應 dict。

    usage / raw **不做深拷貝**,與回應共用物件;需要保存的呼叫端自己複製。

    truncated 與 finish_reason 的一致性由 __post_init__ 強制,手工建構也不能造出
    finish_reason="length" 但 truncated=False 的矛盾物件。
    """

    text: str
    finish_reason: str
    truncated: bool
    usage: dict
    raw: dict

    def __post_init__(self) -> None:
        expected = self.finish_reason not in _COMPLETE_FINISH_REASONS
        if self.truncated is not expected:
            raise ValueError(
                f"VisionJsonResult inconsistent: finish_reason={self.finish_reason!r} "
                f"implies truncated={expected}, got truncated={self.truncated!r}"
            )

    @classmethod
    def from_response(cls, response: Any) -> VisionJsonResult:
        """從 /v1/chat/completions 回應建出 VisionJsonResult。

        只驗**傳輸層形狀**,不碰語意:choices 存在、message.content 是 str。
        content 不是 str(None / list of content parts / dict)一律 RuntimeError ——
        structured output 的契約就是「單一 JSON 字串」,把 content parts 併起來
        等於在做自由文字救回。

        錯誤訊息只帶型別名,**不回顯 content**(可能含 NDA 內容)。
        """
        choices = response.get("choices") if isinstance(response, dict) else None
        if not isinstance(choices, list) or not choices:
            raise RuntimeError(
                "VL server returned no choices for a structured-output request"
            )
        choice = choices[0]
        if not isinstance(choice, dict):
            raise RuntimeError(
                "VL server returned a malformed choice for a structured-output request "
                f"(got {type(choice).__name__})"
            )
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise RuntimeError(
                "VL server returned non-text content for a structured-output request "
                f"(got {type(content).__name__}); structured output must be a single "
                "JSON string"
            )

        reason = choice.get("finish_reason")
        finish_reason = reason if isinstance(reason, str) else ""
        usage = response.get("usage")
        return cls(
            text=content,
            finish_reason=finish_reason,
            truncated=finish_reason not in _COMPLETE_FINISH_REASONS,
            usage=usage if isinstance(usage, dict) else {},
            raw=response,
        )


def _validate_response_format(response_format: Any) -> dict:
    """驗 response_format 的 **wrapper 外殼**,回傳去別名的純 dict snapshot。

    期望形狀(完整 nested wrapper):
        {"type": "json_schema",
         "json_schema": {"name": <非空 str>, "strict": True, "schema": {"type": ..., ...}}}

    刻意**不**遞迴檢查每層的 required / additionalProperties:那是 schema 產生端
    的契約,在這裡再驗一份會變成兩個會各自漂移的真相來源。這裡只擋「wrapper 根本
    組錯」——最常見的是把內層 schema 直接當 response_format 傳(反之亦然),那種
    payload 送出去後 server 可能默默不套任何約束。

    回傳的是**遞迴 snapshot**,不是呼叫端傳進來的那個物件:驗過的那一份就是送出去
    的那一份。否則呼叫端(或另一條 thread)可以在驗證之後把 strict 改成 False、
    把 schema 抽換掉,而 payload 仍然照送 —— grammar 約束就這樣無聲消失了。

    非 dict 也走 ValueError(不是 TypeError):response_format 是由 schema 產生端
    交過來的**資料**,它的所有問題都是同一類「wrapper 不合格」,呼叫端 catch 一個
    ValueError 就涵蓋整個 response_format 的驗證。(相對地,max_tokens /
    sampler_overrides 是呼叫端自己寫死的引數,型別錯屬程式 bug,用 TypeError。)

    訊息只含結構性字面量,不回顯 schema 內容。
    """
    if not isinstance(response_format, dict):
        raise ValueError(
            f"response_format must be a dict, got {type(response_format).__name__}"
        )
    response_format = _json_snapshot(response_format)
    if response_format.get("type") != "json_schema":
        raise ValueError(
            "response_format must be the nested json_schema wrapper "
            "{'type': 'json_schema', 'json_schema': {...}}; got type="
            f"{response_format.get('type')!r}"
        )
    json_schema = response_format.get("json_schema")
    if not isinstance(json_schema, dict):
        raise ValueError(
            "response_format['json_schema'] must be a dict, got "
            f"{type(json_schema).__name__}"
        )
    name = json_schema.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("response_format['json_schema']['name'] must be a non-empty str")
    if json_schema.get("strict") is not True:
        raise ValueError("response_format['json_schema']['strict'] must be True")
    schema = json_schema.get("schema")
    if not isinstance(schema, dict) or not schema:
        raise ValueError(
            "response_format['json_schema']['schema'] must be a non-empty dict"
        )
    if "type" not in schema:
        raise ValueError(
            "response_format['json_schema']['schema'] must declare a top-level 'type' "
            "(looks like the wrapper and the inner schema got swapped)"
        )
    return response_format


def vision_json_completion(
    *,
    base_url: str,
    prompt: str,
    image_base64: str,
    mime_type: str = "image/png",
    model: str = "",
    max_tokens: int,
    response_format: dict,
    temperature: float = 0.0,
    top_p: float = 1.0,
    top_k: int = 1,
    timeout: int = 300,
    cache_prompt: bool = True,
    sampler_overrides: dict | None = None,
) -> VisionJsonResult:
    """帶 JSON Schema 約束的 multimodal 呼叫。回傳 VisionJsonResult(不 parse)。

    response_format 要的是**完整 nested wrapper**
    (``{"type": "json_schema", "json_schema": {"name", "strict", "schema"}}``),
    由 schema 產生端提供;本函式不改寫、不補欄位,只在進來時做一份遞迴 snapshot,
    然後驗證與送出都用那一份 —— 驗過之後沒有任何東西能再改動它。
    sampler_overrides 同樣先 snapshot 再驗。

    ── 「JSON 可解析」不等於正確 ────────────────────────────────────────────
    llama.cpp 是把 JSON Schema 轉成 GBNF grammar 來約束解碼,而那個轉換**只支援
    JSON Schema 的一個子集**。``additionalProperties: false``、``pattern``、
    ``format``、``minItems``/``maxItems``、較複雜的 ``oneOf``/``anyOf`` 等 constraint
    可能被靜默略過 —— 回來的東西是合法 JSON,卻不見得符合你送出的 schema,更不
    保證內容忠於圖片。

    因此本函式**只**負責:送出正確的 payload、驗回應的傳輸層形狀、把 finish_reason
    與 truncated 誠實揭露出來。呼叫端**必須自己**:
      1. json.loads(result.text)(失敗即失敗,不要剝 code fence、不要抽取子字串);
      2. 檢查 result.truncated —— 截斷的輸出即使湊巧能 parse 也不得採用;
      3. 跑完整的 schema validator 與語意 validator(欄寬、row/line contract、
         critical token 等)。
    本函式沒有任何自由文字 fallback,也不做救回。

    ── 取樣與可重現性 ──────────────────────────────────────────────────────
    預設以單一 ``top_k=1`` 收斂取樣。**不要**因為 ``temperature=0`` 就宣稱輸出是
    greedy 或 deterministic:實際可重現性必須用 runtime repeatability test 實測,
    continuous batching、KV cache 重用與浮點非結合性都可能改變輸出。

    ── cache_prompt ───────────────────────────────────────────────────────
    預設 True。要拿「同一模型跑兩次」當佐證時(disagreement detection),第二次
    **必須**傳 ``cache_prompt=False``:同 prompt 同 cache 的重播不是獨立證據。

    ── sampler_overrides ──────────────────────────────────────────────────
    白名單,不是逃生口。只收 _SAMPLER_OVERRIDE_ALLOWED_KEYS 的取樣鍵;
    _VISION_JSON_PROTECTED_KEYS 的鍵覆寫即 ValueError,其他沒列到的鍵一樣拒絕
    (ValueError)。非 dict/None 一律 TypeError。所有檢查都在任何 HTTP 動作之前。

    錯誤:
        TypeError   max_tokens 不是 int / sampler_overrides 不是 dict 或 None
                    (呼叫端自己寫死的引數,型別錯屬程式 bug)
        ValueError  max_tokens <= 0、image/mime 不合法、response_format 不合格
                    (含非 dict 與 wrapper 組錯)、sampler_overrides 帶了不被允許的 key
        RuntimeError
                    回應沒有 choices,或 message.content 不是 str
    (HTTP 層的 4xx/5xx 沿用 requests 的 HTTPError 往上拋,錯誤語意不在這裡加工。)
    """
    if type(max_tokens) is not int:
        raise TypeError(
            f"max_tokens must be an int, got {type(max_tokens).__name__}"
        )
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")

    # 之後只用這份 snapshot:驗過的內容 == 送出的內容。
    response_format = _validate_response_format(response_format)

    overrides = _dict_arg_snapshot(sampler_overrides, "sampler_overrides")
    if overrides:
        _reject_forbidden_keys(overrides, _VISION_JSON_PROTECTED_KEYS, "sampler_overrides")
        unknown = [k for k in overrides if k not in _SAMPLER_OVERRIDE_ALLOWED_KEYS]
        if unknown:
            raise ValueError(
                "sampler_overrides only accepts sampling keys; rejected "
                f"{_format_keys(unknown)} (allowed: "
                f"{_format_keys(_SAMPLER_OVERRIDE_ALLOWED_KEYS)})"
            )

    messages = _vision_image_messages(prompt, image_base64, mime_type)

    # 這四個鍵是 structured 契約的骨架,已列入 _VISION_JSON_PROTECTED_KEYS,
    # 所以下面的 update() 只可能被取樣鍵動到,骨架動不了。
    extra: dict[str, Any] = {
        "max_tokens": max_tokens,
        "response_format": response_format,
        # chat_completions 的 base payload 硬編碼 cache_prompt=True;
        # 這裡覆寫成呼叫端指定的值(第二次取樣要 False 才算獨立佐證)。
        "cache_prompt": cache_prompt,
        # thinking 會把 reasoning 吐進 content,直接毀掉「content 是純 JSON」的契約。
        "chat_template_kwargs": {"enable_thinking": False},
    }
    extra.update(overrides)

    response = chat_completions(
        base_url=base_url,
        model=model,
        messages=messages,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        stream=False,
        timeout=timeout,
        extra=extra,
    )
    return VisionJsonResult.from_response(response)


def _iter_openai_stream(resp) -> Iterator[dict]:
    """OpenAI SSE:`data: {json}` 一行一個 delta,結束時是 `data: [DONE]`。"""
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw:
            continue
        line = raw.strip()
        if line.startswith("data:"):
            line = line[len("data:"):].strip()
        if not line or line == "[DONE]":
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


# ============================================================
# /embedding
# ============================================================
class EmbeddingContractError(RuntimeError):
    """/v1/embeddings 回應違反嚴格契約(cardinality / index / 維度 / 空向量)。

    與「連不上 server」分開:契約錯誤代表 server 版本行為不符,重試無用,
    呼叫端不得把它包裝成 unreachable。
    """


def embed_one(
    *,
    base_url: str,
    content: str,
    model: str = "",
    timeout: int = 60,
) -> list[float]:
    """單筆 embedding。回傳 1D float list。

    llama-server 必須以 --embedding 啟動。
    """
    payload = {"content": content}
    if model:
        payload["model"] = model
    session = get_session()
    url = base_url.rstrip("/") + "/embedding"
    _ensure_allowed(url)
    resp = session.post(url, json=payload, timeout=timeout, allow_redirects=False)
    _reject_redirect(resp, url)
    resp.raise_for_status()
    data = resp.json()

    # llama-server 回傳格式:
    #   單筆: {"embedding": [[...]]} 或 {"embedding": [...]}
    #   批次: [{"embedding": [[...]]}, ...]
    return _extract_first_embedding(data)


def embed_batch(
    *,
    base_url: str,
    contents: list[str],
    model: str = "",
    timeout: int = 300,
) -> list[list[float]]:
    """批次 embedding,走 /v1/embeddings(OpenAI-compat)。回傳 [[float, ...], ...]。

    llama.cpp 只在 /v1/embeddings 明確定義 array input;legacy /embedding 塞
    list 會在某些版本退化成單一向量(32 筆進、1 條出),silent 對不上列。
    嚴格契約(任一不符 → RuntimeError,不部分接受):
      - 回應 data 筆數 == 輸入筆數
      - data[].index 集合 == range(n)(按 index 還原順序,不信回傳排序)
      - 維度全相等且無空向量
    """
    if not contents:
        return []
    payload: dict[str, Any] = {"input": list(contents), "model": model or "local"}
    session = get_session()
    url = base_url.rstrip("/") + "/v1/embeddings"
    _ensure_allowed(url)
    resp = session.post(url, json=payload, timeout=timeout, allow_redirects=False)
    _reject_redirect(resp, url)
    resp.raise_for_status()
    data = resp.json()

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise EmbeddingContractError(
            f"/v1/embeddings returned unexpected shape (no 'data' list) from {url}"
        )
    n = len(contents)
    if len(items) != n:
        raise EmbeddingContractError(
            f"/v1/embeddings cardinality mismatch: sent {n} inputs, got {len(items)} rows"
        )

    out: list[list[float] | None] = [None] * n
    for entry in items:
        if not isinstance(entry, dict):
            raise EmbeddingContractError("/v1/embeddings row is not an object")
        idx = entry.get("index")
        if not isinstance(idx, int) or not (0 <= idx < n):
            raise EmbeddingContractError(f"/v1/embeddings row has invalid index {idx!r} (n={n})")
        if out[idx] is not None:
            raise EmbeddingContractError(f"/v1/embeddings returned duplicate index {idx}")
        emb = entry.get("embedding")
        if not isinstance(emb, list) or not emb:
            raise EmbeddingContractError(f"/v1/embeddings row {idx} has an empty embedding")
        try:
            out[idx] = [float(x) for x in emb]
        except (TypeError, ValueError) as exc:
            raise EmbeddingContractError(f"/v1/embeddings row {idx} has non-numeric values") from exc

    # index 集合 == range(n) 由「筆數相等 + 範圍內 + 不重複」三者聯合保證
    dimensions = {len(v) for v in out}  # type: ignore[arg-type]
    if len(dimensions) != 1:
        raise EmbeddingContractError(
            f"/v1/embeddings dimension mismatch across rows: {sorted(dimensions)}"
        )
    return out  # type: ignore[return-value]


def _extract_first_embedding(data: Any) -> list[float]:
    """從 /embedding 各種回傳形狀拆出一條 1D 向量。"""
    if isinstance(data, list):
        if not data:
            return []
        first = data[0]
        if isinstance(first, dict) and "embedding" in first:
            emb = first["embedding"]
        else:
            emb = first
    elif isinstance(data, dict):
        emb = data.get("embedding", [])
    else:
        return []

    if isinstance(emb, list) and emb and isinstance(emb[0], list):
        # 2D → 取第一個 pooled 向量
        emb = emb[0]
    return [float(x) for x in emb] if isinstance(emb, list) else []


# ============================================================
# /reranking
# ============================================================
def rerank(
    *,
    base_url: str,
    query: str,
    documents: list[str],
    model: str = "",
    timeout: int = 120,
) -> list[float]:
    """對 documents 回傳相對於 query 的相關性分數(越大越相關)。

    llama-server 必須以 --reranking 啟動(cross-encoder 模型)。
    """
    payload: dict[str, Any] = {"query": query, "documents": documents}
    if model:
        payload["model"] = model
    session = get_session()
    url = base_url.rstrip("/") + "/reranking"
    _ensure_allowed(url)
    resp = session.post(url, json=payload, timeout=timeout, allow_redirects=False)
    _reject_redirect(resp, url)
    resp.raise_for_status()
    data = resp.json()

    # 標準回傳:{"results": [{"index": i, "relevance_score": s}, ...]}
    results = data.get("results") if isinstance(data, dict) else None
    if isinstance(results, list):
        scores = [0.0] * len(documents)
        for entry in results:
            try:
                idx = int(entry.get("index"))
                score = float(entry.get("relevance_score"))
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(scores):
                scores[idx] = score
        return scores
    return [0.0] * len(documents)


# ============================================================
# /props /slots /health
# ============================================================
def get_props(base_url: str, *, timeout: int = 5) -> dict | None:
    """讀 server props:回傳含 default_generation_settings.n_ctx、model_path、
    chat_template 等。連線失敗 / 非 200 → None。

    回 None 前先在 stderr 留一行原因(policy 拒絕 / redirect 也是),
    避免把「被 policy 擋掉」偽裝成 server down。
    """
    session = get_session()
    url = base_url.rstrip("/") + "/props"
    try:
        _ensure_allowed(url)
        resp = session.get(url, timeout=timeout, allow_redirects=False)
        _reject_redirect(resp, url)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as exc:
        _log_probe_failure("/props", url, exc)
        return None


def get_slots(base_url: str, *, timeout: int = 5) -> list[dict] | None:
    """讀 server slots:回傳每個 slot 的當前 ctx / 處理狀態。"""
    session = get_session()
    url = base_url.rstrip("/") + "/slots"
    try:
        _ensure_allowed(url)
        resp = session.get(url, timeout=timeout, allow_redirects=False)
        _reject_redirect(resp, url)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, list) else None
    except Exception as exc:
        _log_probe_failure("/slots", url, exc)
        return None


def get_health(base_url: str, *, timeout: int = 3) -> dict | None:
    """讀 server health:{"status": "ok" | "loading model" | "error", ...}。
    無法連線回 None。
    """
    session = get_session()
    url = base_url.rstrip("/") + "/health"
    try:
        _ensure_allowed(url)
        resp = session.get(url, timeout=timeout, allow_redirects=False)
        _reject_redirect(resp, url)
        return resp.json() if resp.status_code == 200 else None
    except Exception as exc:
        _log_probe_failure("/health", url, exc)
        return None


def _log_probe_failure(endpoint: str, url: str, exc: Exception) -> None:
    """probe(health/props/slots)吞例外回 None 之前,stderr 留一行真實原因。

    policy 拒絕與 3xx 的訊息尤其重要:沒有這行,遠端端點未 opt-in 會被
    誤讀成「server 沒開」。連線層的例外(ConnectionError/timeout)訊息
    不含 body,直接印。
    """
    print(
        f"[llama_client] {endpoint} probe failed for {_redact_url(url)}: "
        f"{type(exc).__name__}: {exc}",
        file=sys.stderr,
    )


def is_ready(base_url: str, *, timeout: int = 3) -> bool:
    """server 是否可用(/health 回 200 且 status=ok)。"""
    h = get_health(base_url, timeout=timeout)
    if not isinstance(h, dict):
        return False
    return str(h.get("status", "")).lower() == "ok"


# ============================================================
# usage 萃取(把 llama.cpp 的回傳格式對齊到 context_budget 期望的欄位)
# ============================================================
def extract_native_usage(data: dict) -> dict:
    """從 native /completion 回應萃取 token 計數。

    llama.cpp 用 tokens_predicted / tokens_evaluated 兩個欄位:
        tokens_evaluated → prompt 階段被處理掉的 token 數
        tokens_predicted → decode 階段生成的 output token 數
    `timings` 區塊可選,含 prompt_per_second / predicted_per_second。
    """
    out: dict[str, Any] = {}
    if not isinstance(data, dict):
        return out
    pe = data.get("tokens_evaluated")
    ec = data.get("tokens_predicted")
    if isinstance(pe, (int, float)):
        out["prompt_eval_count"] = int(pe)
    if isinstance(ec, (int, float)):
        out["eval_count"] = int(ec)
    timings = data.get("timings")
    if isinstance(timings, dict):
        if isinstance(timings.get("prompt_per_second"), (int, float)):
            out["prompt_tokens_per_second"] = float(timings["prompt_per_second"])
        if isinstance(timings.get("predicted_per_second"), (int, float)):
            out["output_tokens_per_second"] = float(timings["predicted_per_second"])
    return out


def extract_openai_usage(data: dict) -> dict:
    """從 /v1/chat/completions 回應萃取 usage 欄位。

    OpenAI 風格:`usage: {prompt_tokens, completion_tokens, total_tokens}`。
    """
    out: dict[str, Any] = {}
    if not isinstance(data, dict):
        return out
    usage = data.get("usage")
    if isinstance(usage, dict):
        if isinstance(usage.get("prompt_tokens"), (int, float)):
            out["prompt_eval_count"] = int(usage["prompt_tokens"])
        if isinstance(usage.get("completion_tokens"), (int, float)):
            out["eval_count"] = int(usage["completion_tokens"])
    timings = data.get("timings")
    if isinstance(timings, dict):
        if isinstance(timings.get("prompt_per_second"), (int, float)):
            out["prompt_tokens_per_second"] = float(timings["prompt_per_second"])
        if isinstance(timings.get("predicted_per_second"), (int, float)):
            out["output_tokens_per_second"] = float(timings["predicted_per_second"])
    return out
