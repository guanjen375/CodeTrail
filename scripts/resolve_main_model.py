#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resolve CodeTrail's explicit main Ollama model for the aicode wrapper.

Priority:
  1. AICODE_MODEL
  2. CLI -m/--model
  3. OPENCODE_CONFIG, then ~/.config/opencode/opencode.json

Env and CLI may both be present only when they resolve to the same bare Ollama
model name. This prevents OpenCode TUI and CodeTrail MCP from silently using
different models.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from model_resolution import (  # noqa: E402
    normalize_main_model,
    parse_cli_model_arg,
    resolve_opencode_main_model,
)


def _fail(msg: str) -> int:
    print(f"[aicode] {msg}", file=sys.stderr, flush=True)
    print(
        "[aicode] CodeTrail 不內建、不推薦主聊天 / 程式推導模型。\n"
        "         請先 ollama pull 一顆 Ollama 模型,然後任選一種方式設定:\n"
        "           1) export AICODE_MODEL=<CODE_MODEL>\n"
        "           2) aicode -m ollama/<CODE_MODEL>\n"
        "           3) OPENCODE_CONFIG / ~/.config/opencode/opencode.json 設\n"
        '                "model": "ollama/<CODE_MODEL>"\n'
        "         <CODE_MODEL> 是佔位符,必須替換成實際模型名稱。\n"
        "         CodeTrail MCP 只呼叫 Ollama native API,不可填 openai/、anthropic/ 等 provider。\n"
        "         詳見 README.md。",
        file=sys.stderr,
        flush=True,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])

    env_raw = os.environ.get("AICODE_MODEL", "").strip()
    cli_raw = parse_cli_model_arg(args)

    env_res = normalize_main_model(env_raw, "AICODE_MODEL") if env_raw else None
    cli_res = normalize_main_model(cli_raw, "-m/--model") if cli_raw else None

    if env_res and env_res.error:
        return _fail(env_res.error)
    if cli_res and cli_res.error:
        return _fail(cli_res.error)

    if env_res and cli_res and env_res.model != cli_res.model:
        return _fail(
            "AICODE_MODEL and --model point to different models: "
            f"{env_res.model!r} != {cli_res.model!r}. "
            "Use one model for both OpenCode TUI and CodeTrail MCP."
        )

    if env_res and env_res.model:
        print(env_res.model, flush=True)
        return 0

    if cli_res and cli_res.model:
        print(cli_res.model, flush=True)
        return 0

    oc_res = resolve_opencode_main_model(os.environ)
    if oc_res.error:
        where = f" ({oc_res.path})" if oc_res.path else ""
        return _fail(f"{oc_res.source}{where}: {oc_res.error}")
    if oc_res.model:
        print(oc_res.model, flush=True)
        return 0

    return _fail(
        "主模型未設定: AICODE_MODEL 未設、CLI 未帶 -m、"
        "OPENCODE_CONFIG / opencode.json 也沒有有效 model。"
    )


if __name__ == "__main__":
    sys.exit(main())
