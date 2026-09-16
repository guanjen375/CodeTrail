#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""endpoint_policy — 模型 / KB 端點的共用安全 policy。

規則(所有 prompt-bearing 流量的單一真相):
  - loopback 端點無條件允許。
  - 非 loopback 端點必須有該 role 對應的顯式 opt-in,否則 fail-loud,
    錯誤訊息印出確切 env 名與會被送出去的內容。

roles:
  - "model":      llama_client 的所有 llama-server 呼叫
                  (completion / chat / embedding / reranking / props / slots / health)。
                  opt-in:`client.json` 的 `model_remote_ok`。
  - "kb_context": Contextual Retrieval 的 chunk 脈絡生成(context_generation)。
                  opt-in:`client.json` 的 `kb_context_remote_ok`。

兩個 opt-in 都**只來自 `~/.config/codetrail/client.json`**(由
`client_config.apply_to_config()` 在啟動時推進 `config`)。以前它們是兩個環境
變數;殼層裡一個殘留的值就等於「使用者以為 NDA 內容留在本機」而它正在離機。

is_loopback_host / 錯誤語氣沿用 context_generation 既有實作;那邊改為委派這裡。
"""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlparse, urlsplit, urlunsplit

_LOOPBACK_NAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})
MODEL_ROLES = ("main", "embedding", "reranker", "vl")
_ROLE_PATHS = {
    "main": {"", "/", "/health", "/props", "/slots", "/v1/models", "/completion",
             "/v1/chat/completions", "/tokenize", "/detokenize"},
    "embedding": {"", "/", "/health", "/props", "/v1/models", "/embedding", "/v1/embeddings"},
    "reranker": {"", "/", "/health", "/props", "/v1/models", "/reranking"},
    "vl": {"", "/", "/health", "/props", "/v1/models", "/v1/chat/completions"},
}
for _paths in _ROLE_PATHS.values():
    _paths.add("/slots")


def redact_url(url: str) -> str:
    """去掉 URL 內嵌的 credentials(user:pass@)。

    所有會進錯誤訊息 / stderr / doctor 輸出的 URL 都必須先過這裡:
    policy 拒絕的錯誤本身就會被印出與往上拋,錯誤裡帶密碼等於把
    credentials 洩進 log。

    刻意**不讀** parts.port / parts.username:那些屬性對 malformed port
    (``http://u:secret@h:bad/``)會拋 ValueError,一旦 fallback 成
    「原樣回傳」就把 secret 洩出去(GPT 審核三輪 #2)。改為直接對 raw
    netloc 字串砍掉最後一個 ``@`` 之前的 userinfo —— 無論 port / IPv6 /
    形狀多壞都砍得掉;連 urlsplit 都失敗時走純字串 fallback,寧可砍多
    也不放行 credentials。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return _redact_raw(url)
    if "@" not in parts.netloc:
        return url
    host_port = parts.netloc.rpartition("@")[2]
    try:
        return urlunsplit((parts.scheme, host_port, parts.path, parts.query, parts.fragment))
    except ValueError:
        return _redact_raw(url)


def _redact_raw(url: str) -> str:
    """字串層 fallback:找出 authority 區段,砍掉最後一個 @ 之前的內容。"""
    scheme_sep = url.find("://")
    start = scheme_sep + 3 if scheme_sep != -1 else 0
    end = len(url)
    for ch in "/?#":
        i = url.find(ch, start)
        if i != -1:
            end = min(end, i)
    netloc = url[start:end]
    if "@" not in netloc:
        return url
    return url[:start] + netloc.rpartition("@")[2] + url[end:]

#: 兩個 opt-in 的來源都是 `client.json`(經 `client_config.apply_to_config()`
#: 推進 `config`)。**兩個鍵,不是一個**:前者放行送出 prompt,後者等於把整份
#: 文件的窗送出去;合併會把前者的同意無聲擴大成後者。
MODEL_REMOTE_OK_KEY = "model_remote_ok"
KB_CONTEXT_REMOTE_OK_KEY = "kb_context_remote_ok"


class EndpointPolicyError(RuntimeError):
    """非 loopback 端點且沒有對應 opt-in。呼叫端可轉譯成自己的錯誤型別。"""


def _url_parts(url: str):
    try:
        if not isinstance(url, str) or not url or any(ord(c) < 33 or ord(c) == 127 for c in url):
            raise ValueError("invalid URL characters")
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or
                parts.username is not None or parts.password is not None or parts.query or parts.fragment):
            raise ValueError("invalid URL components")
        port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError("invalid port")
        return parts, port
    except (ValueError, TypeError) as exc:
        raise EndpointPolicyError("model endpoint must be an http(s) URL without credentials, query or fragment") from exc


def canonical_direct_base_url(url: str) -> str:
    """Native llama-server root on a literal private/loopback unicast IP.

    No hostname resolution or URL path rewriting can change this destination.
    The application policy is not a replacement for the A/B network ACL.
    """
    parts, port = _url_parts(url)
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError as exc:
        raise EndpointPolicyError("split model endpoint requires a literal private IP address") from exc
    if ("%" in parts.hostname or address.is_unspecified or address.is_multicast or
            not (address.is_private or address.is_loopback) or
            (address.is_reserved and not address.is_loopback) or parts.path not in {"", "/"}):
        raise EndpointPolicyError("split model endpoint must be a private unicast IP and native server root")
    host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"{parts.scheme}://{host}:{port}"


def validate_model_endpoints(value) -> dict[str, str]:
    if not isinstance(value, dict) or (value and set(value) != set(MODEL_ROLES)):
        raise EndpointPolicyError("model_endpoints must be empty or name all four model roles")
    result = {role: canonical_direct_base_url(url) for role, url in value.items()}
    if len(set(result.values())) != len(result):
        raise EndpointPolicyError("the four model roles require distinct model endpoints")
    return result


def _ensure_split(url: str, role: str) -> None:
    import config

    grants = validate_model_endpoints(getattr(config, "MODEL_ENDPOINTS", {}))
    if not grants:
        raise EndpointPolicyError("client deployment requires owner-only client.json model_endpoints for all four roles")
    parts, _ = _url_parts(url)
    origin = canonical_direct_base_url(urlunsplit((parts.scheme, parts.netloc, "", "", "")))
    configured = {"main": config.LLAMA_BASE_URL, "embedding": config.LLAMA_EMBED_BASE_URL,
                  "reranker": config.LLAMA_RERANK_BASE_URL, "vl": config.LLAMA_VL_BASE_URL}
    candidates = ("main",) if role == "kb_context" else ((role,) if role in MODEL_ROLES else MODEL_ROLES)
    matches = [name for name in candidates
               if grants.get(name) == origin
               and (role in MODEL_ROLES or canonical_direct_base_url(configured[name]) == origin)
               and parts.path in _ROLE_PATHS[name]]
    if not matches:
        raise EndpointPolicyError(f"{role} endpoint is not authorized by client.json model_endpoints")
    if role == "kb_context" and not _kb_context_remote_ok():
        raise EndpointPolicyError(_kb_context_error(redact_url(url)))


def is_loopback_host(host: str) -> bool:
    """host 正規化後是不是 loopback(雙棧)。"""
    if not host:
        return False
    cleaned = host.strip().strip("[]").lower()
    # IPv6 scope id(fe80::1%eth0)在比對前去掉
    cleaned = cleaned.split("%", 1)[0]
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        return cleaned in _LOOPBACK_NAMES


def _model_remote_ok() -> bool:
    # 動態讀 config attr(§3:動態值只用 `import config`):測試與 runtime 都
    # 從同一個地方拿,而 client.json 的值是在啟動時被推進去的。
    import config

    return bool(getattr(config, "MODEL_REMOTE_OK", False))


def _kb_context_remote_ok() -> bool:
    # 動態讀 config attr:既有測試 monkeypatch config.KB_CONTEXT_REMOTE_OK,
    # 不能在 import 時 snapshot。
    import config

    return bool(getattr(config, "KB_CONTEXT_REMOTE_OK", False))


def _model_error(base_url: str) -> str:
    # 刻意單行:這段會被包進其他錯誤訊息再經 subprocess 逐行擷取
    # (mcp_server 的 ingest fail-loud 路徑),多行會被截掉後半。
    return (
        f"模型端點 {base_url} 不是 loopback。CodeTrail 的模型呼叫會把 prompt"
        "(可能含 NDA 程式碼、文件內容與問題原文)送到該端點。"
        "確定要用遠端模型的話,在 ~/.config/codetrail/client.json 設 "
        f'"{MODEL_REMOTE_OK_KEY}": true;否則把對應的 base URL 指回本機的 llama-server。'
    )


def _kb_context_error(base_url: str) -> str:
    return (
        f"KB_CONTEXT_GENERATE 需要把文件內容送到 {base_url}，那不是 loopback。\n"
        "  會被送出去的東西：每個 chunk 的原文，以及它所在章節（文件太長時是整份\n"
        "  文件的摘要）——等於整份文件都會離開這台機器。\n"
        f'  確定要這樣做的話,在 ~/.config/codetrail/client.json 設 "{KB_CONTEXT_REMOTE_OK_KEY}": true;\n'
        "  否則重跑 ./set_config.sh 把主模型 base URL 指回本機的 llama-server。"
    )


_ROLES = {
    "model": (_model_remote_ok, _model_error),
    "kb_context": (_kb_context_remote_ok, _kb_context_error),
}


def ensure_allowed(url: str, role: str, *, split: bool = False) -> None:
    """url 的 host 非 loopback 且該 role 未 opt-in 時 raise EndpointPolicyError。

    錯誤訊息內的 URL 一律先 redact_url:policy 錯誤會被印出與往上拋,
    不得帶出 URL 內嵌的 credentials。
    """
    import config
    if role not in {*_ROLES, *MODEL_ROLES}:
        raise ValueError(f"unknown endpoint role: {role!r}")
    if split or getattr(config, "DEPLOYMENT_MODE", "local") == "client" or getattr(config, "MODEL_ENDPOINTS", {}):
        _ensure_split(url, role)
        return
    if role in MODEL_ROLES:
        role = "model"
    try:
        allowed_fn, error_fn = _ROLES[role]
    except KeyError:
        raise ValueError(f"unknown endpoint role: {role!r} (expected {sorted(_ROLES)})") from None
    host = urlparse(url).hostname or ""
    if is_loopback_host(host):
        return
    if allowed_fn():
        return
    raise EndpointPolicyError(error_fn(redact_url(url)))
