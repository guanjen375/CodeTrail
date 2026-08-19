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
from typing import Any, Iterator
from urllib.parse import urlparse

import endpoint_policy
from http_client import get_session


def _redact_url(url: str) -> str:
    """去掉 URL 內嵌的 credentials(user:pass@)再進錯誤訊息 / log。"""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if not (parts.username or parts.password):
        return url
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


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
    if extra:
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
    """
    if not image_base64:
        raise ValueError("image_base64 must not be empty")
    if not mime_type.startswith("image/"):
        raise ValueError(f"unsupported image MIME type: {mime_type!r}")
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")

    data_url = image_base64
    if not data_url.startswith("data:"):
        data_url = f"data:{mime_type};base64,{data_url}"

    response = chat_completions(
        base_url=base_url,
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
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
