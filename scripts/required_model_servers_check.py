#!/usr/bin/env python3
"""Hard preflight for CodeTrail auxiliary llama-server roles.

When a CodeTrail chat/frontend session starts, the local main model is only
useful if the three auxiliary model servers are also available:

  - embedding: bge-m3 on /embedding
  - reranker: bge-reranker-v2-m3 on /reranking
  - VL: qwen3-vl-compatible server accepting image_data

This script is intentionally stricter than doctor.py: missing auxiliary
servers are FAIL, not WARN.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402
import llama_client  # noqa: E402

SKIP_ENV = "AICODE_REQUIRED_MODELS_CHECK_SKIP"

_TINY_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="


@dataclass(frozen=True)
class RequiredServer:
    role: str
    url: str
    model: str
    endpoint: str


@dataclass(frozen=True)
class ServerCheck:
    role: str
    url: str
    ok: bool
    message: str


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def required_servers() -> tuple[RequiredServer, ...]:
    return (
        RequiredServer("embedding", config.LLAMA_EMBED_BASE_URL, config.EMBEDDING_MODEL, "/embedding"),
        RequiredServer("reranker", config.LLAMA_RERANK_BASE_URL, config.RERANKER_MODEL, "/reranking"),
        RequiredServer("VL", config.LLAMA_VL_BASE_URL, config.VL_MODEL, "/completion image_data"),
    )


def _health_ok(server: RequiredServer) -> tuple[bool, str]:
    health = llama_client.get_health(server.url, timeout=3)
    if not isinstance(health, dict):
        return False, "health endpoint unreachable"
    status = str(health.get("status", "")).lower()
    if status != "ok":
        return False, f"health status={status!r}"
    return True, "health ok"


def _check_embedding(server: RequiredServer) -> str:
    vector = llama_client.embed_one(
        base_url=server.url,
        content="CodeTrail embedding preflight",
        model=server.model,
        timeout=20,
    )
    if not vector:
        raise RuntimeError("/embedding returned an empty vector")
    return f"/embedding ok dim={len(vector)}"


def _check_reranker(server: RequiredServer) -> str:
    scores = llama_client.rerank(
        base_url=server.url,
        query="CodeTrail reranker preflight",
        documents=[
            "CodeTrail uses a reranker for RAG result ordering.",
            "Unrelated filler text.",
        ],
        model=server.model,
        timeout=30,
    )
    if len(scores) != 2:
        raise RuntimeError(f"/reranking returned {len(scores)} scores, expected 2")
    return "/reranking ok"


def _check_vl(server: RequiredServer) -> str:
    data = llama_client.native_completion(
        base_url=server.url,
        prompt="請用三個字以內描述這張圖片。",
        n_predict=8,
        temperature=0.0,
        top_p=1.0,
        top_k=1,
        stream=False,
        image_data=[{"id": 10, "data": _TINY_PNG_BASE64}],
        timeout=60,
    )
    if not isinstance(data, dict):
        raise RuntimeError("/completion returned a non-JSON response")
    return "/completion image_data ok"


def check_server(server: RequiredServer) -> ServerCheck:
    ok, detail = _health_ok(server)
    if not ok:
        return ServerCheck(server.role, server.url, False, detail)

    try:
        if server.role == "embedding":
            detail = _check_embedding(server)
        elif server.role == "reranker":
            detail = _check_reranker(server)
        elif server.role == "VL":
            detail = _check_vl(server)
        else:
            detail = "health ok"
    except Exception as exc:
        return ServerCheck(server.role, server.url, False, f"{type(exc).__name__}: {exc}")

    return ServerCheck(server.role, server.url, True, detail)


def run_checks() -> list[ServerCheck]:
    return [check_server(server) for server in required_servers()]


def render_report(checks: list[ServerCheck], *, prefix: str = "[model-preflight]") -> list[str]:
    lines: list[str] = []
    for check in checks:
        label = "PASS" if check.ok else "FAIL"
        lines.append(f"{prefix} {label} {check.role} {check.url} -- {check.message}")
    if not all(c.ok for c in checks):
        lines.append(
            f"{prefix} refuse to start: embedding, reranker, and VL servers must all be ready."
        )
        lines.append(
            f"{prefix} start them with ./scripts/start-rag-servers.sh or point "
            "AICODE_LLAMA_EMBED_BASE_URL / AICODE_LLAMA_RERANK_BASE_URL / "
            "AICODE_LLAMA_VL_BASE_URL at ready servers."
        )
    return lines


def main() -> int:
    if _truthy(os.environ.get(SKIP_ENV)):
        print(
            f"[model-preflight] skipped via {SKIP_ENV}=1 "
            "(test/CI escape hatch; normal runtime should not set this)"
        )
        return 0

    checks = run_checks()
    for line in render_report(checks):
        print(line, flush=True)
    return 0 if all(c.ok for c in checks) else 2


if __name__ == "__main__":
    sys.exit(main())
