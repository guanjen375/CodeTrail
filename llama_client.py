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

import base64
import json
from pathlib import Path
from typing import Any, Iterator

from http_client import get_session


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
    image_data 用於多模態:[{"data": "<base64>", "id": 10}, ...]
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
        payload["image_data"] = image_data
    if extra:
        payload.update(extra)

    session = get_session()
    url = base_url.rstrip("/") + "/completion"

    if not stream:
        resp = session.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    resp = session.post(url, json=payload, timeout=timeout, stream=True)
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

    if not stream:
        resp = session.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    resp = session.post(url, json=payload, timeout=timeout, stream=True)
    resp.raise_for_status()
    return _iter_openai_stream(resp)


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
    resp = session.post(url, json=payload, timeout=timeout)
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
    """批次 embedding。回傳 [[float, ...], ...]。"""
    payload: dict[str, Any] = {"content": contents}
    if model:
        payload["model"] = model
    session = get_session()
    url = base_url.rstrip("/") + "/embedding"
    resp = session.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    if isinstance(data, list):
        return [_extract_first_embedding(item) for item in data]
    # 單筆 fallback
    return [_extract_first_embedding(data)]


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
    resp = session.post(url, json=payload, timeout=timeout)
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
# 多模態:把圖片轉成 image_data
# ============================================================
def file_to_image_data(path: str | Path, image_id: int = 10) -> dict:
    """讀圖片檔轉成 native /completion image_data 一筆。"""
    raw = Path(path).read_bytes()
    return {"id": image_id, "data": base64.b64encode(raw).decode("ascii")}


# ============================================================
# /props /slots /health
# ============================================================
def get_props(base_url: str, *, timeout: int = 5) -> dict | None:
    """讀 server props:回傳含 default_generation_settings.n_ctx、model_path、
    chat_template 等。連線失敗 / 非 200 → None。
    """
    session = get_session()
    url = base_url.rstrip("/") + "/props"
    try:
        resp = session.get(url, timeout=timeout)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def get_slots(base_url: str, *, timeout: int = 5) -> list[dict] | None:
    """讀 server slots:回傳每個 slot 的當前 ctx / 處理狀態。"""
    session = get_session()
    url = base_url.rstrip("/") + "/slots"
    try:
        resp = session.get(url, timeout=timeout)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, list) else None
    except Exception:
        return None


def get_health(base_url: str, *, timeout: int = 3) -> dict | None:
    """讀 server health:{"status": "ok" | "loading model" | "error", ...}。
    無法連線回 None。
    """
    session = get_session()
    url = base_url.rstrip("/") + "/health"
    try:
        resp = session.get(url, timeout=timeout)
        return resp.json() if resp.status_code == 200 else None
    except Exception:
        return None


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
