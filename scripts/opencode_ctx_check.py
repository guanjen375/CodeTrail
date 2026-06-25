#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Preflight check: OpenCode TUI limit.context must match CodeTrail ctx cap.

This catches the common split-brain state where llama-server and CodeTrail are
configured for 64K but OpenCode still compacts at 32K.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import opencode_context  # noqa: E402


def _print(line: str) -> None:
    print(f"[ctx-align] {line}", flush=True)


def _truthy(value: str) -> bool:
    return value.lower() in ("1", "true", "yes")


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])

    if _truthy(os.environ.get("AICODE_CTX_SAFETY_DISABLE", "")):
        _print("disabled via AICODE_CTX_SAFETY_DISABLE")
        return 0

    try:
        requested = opencode_context.dynamic_ctx_max_from_env(os.environ)
    except ValueError:
        _print(
            f"UNKNOWN: AICODE_DYNAMIC_NUM_CTX_MAX={os.environ.get('AICODE_DYNAMIC_NUM_CTX_MAX')!r}"
            " 不是數字,跳過 OpenCode ctx 對齊檢查"
        )
        return 0

    limit = opencode_context.resolve_active_opencode_context_limit(os.environ, args)
    if limit.error:
        where = f" ({limit.path})" if limit.path else ""
        _print(f"UNKNOWN: OpenCode context limit 讀取失敗{where}: {limit.error}")
        return 0
    if not limit.present:
        _print("UNKNOWN: 找不到 opencode.json,跳過 OpenCode ctx 對齊檢查")
        return 0
    if limit.context is None:
        where = f" ({limit.path})" if limit.path else ""
        model = limit.raw_model or limit.model or "<unknown>"
        _print(f"UNKNOWN: OpenCode model={model}{where} 沒有 limit.context,跳過對齊檢查")
        return 0

    label = limit.raw_model or limit.model
    if limit.context == requested:
        _print(f"SAFE: OpenCode model={label} limit.context={limit.context} == AICODE_DYNAMIC_NUM_CTX_MAX={requested}")
        return 0

    _print(f"MISMATCH: OpenCode model={label} limit.context={limit.context}")
    _print(f"          AICODE_DYNAMIC_NUM_CTX_MAX={requested}")
    _print("          這會讓 OpenCode TUI 和 CodeTrail MCP 在不同 ctx 預算下工作。")
    _print("")
    _print("          建議任一處理:")
    _print(f"            (a) 把 opencode.json active model 的 limit.context 改成 {requested}")
    _print(f"            (b) export AICODE_DYNAMIC_NUM_CTX_MAX={limit.context}")
    _print("            (c) 啟動時傳 -m/--model 指到另一個已對齊的 OpenCode model entry")
    _print("            (d) export AICODE_ACCEPT_CTX_RISK=1 (本次接受不一致)")
    _print("            (e) export AICODE_CTX_SAFETY_DISABLE=1 (跳過 ctx safety/alignment 檢查)")
    _print("")

    if _truthy(os.environ.get("AICODE_ACCEPT_CTX_RISK", "")):
        _print("AICODE_ACCEPT_CTX_RISK=1 已設 — 放行,但 ctx 行為會不一致")
        return 0

    _print("refuse to start.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
