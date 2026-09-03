#!/usr/bin/env python3
"""Resolve CodeTrail's explicit main llama.cpp model for the aicode wrapper.

Priority:
  1. AICODE_MODEL
  2. CLI -m/--model
  3. deployment profile / local override main.model

Env and CLI may both be present only when they resolve to the same bare model
name, GGUF path, or registry aliases backed by the same canonical GGUF.

`opencode.json` 已經**不在**這條鏈上:CodeTrail 啟動的是自己的客戶端,不再有
第二個 TUI 需要對齊。沿用那份設定裡的模型等於「使用者以為在跑 A、實際在跑
B」;更糟的是一份壞掉的 opencode.json 會讓一台根本沒在用 OpenCode 的機器
拒絕啟動。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from model_resolution import (  # noqa: E402
    main_model_references_equivalent,
    normalize_main_model,
    parse_cli_model_arg_detail,
    resolve_main_model_from_env,
)


def _fail(msg: str) -> int:
    print(f"[aicode] {msg}", file=sys.stderr, flush=True)
    print(
        "[aicode] CodeTrail 不內建、不推薦主聊天 / 程式推導模型。\n"
        "         請先下載一顆 GGUF 並啟動 llama-server, 然後任選一種方式設定:\n"
        "           1) export AICODE_MODEL=<MODEL>\n"
        "           2) aicode -m <MODEL>\n"
        "           3) deployment profile / ~/.config/codetrail/deployment.json 設 main.model\n"
        "         <MODEL> 可以是:\n"
        "           - registry 裡登記的 bare name (例如 \"qwen3-coder-30b\")\n"
        "           - GGUF 絕對路徑 (例如 /models/foo.gguf)\n"
        "         registry 維護在 ~/.config/codetrail/models.json 或 AICODE_MODEL_REGISTRY env。\n"
        "         CodeTrail 不接受 ollama/、openai/、anthropic/ 等 provider prefix。\n"
        "         詳見 README.md。",
        file=sys.stderr,
        flush=True,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])

    env_raw = os.environ.get("AICODE_MODEL", "").strip()
    cli_arg = parse_cli_model_arg_detail(args)

    if cli_arg.error:
        return _fail(cli_arg.error)

    env_res = normalize_main_model(env_raw, "AICODE_MODEL") if env_raw else None
    cli_results = [
        normalize_main_model(raw, "-m/--model") for raw in cli_arg.values
    ]
    cli_res = cli_results[0] if cli_results else None

    if env_res and env_res.error:
        return _fail(env_res.error)
    for res in cli_results:
        if res.error:
            return _fail(res.error)
    cli_models = [res.model for res in cli_results if res.model]
    if cli_models and any(
        not main_model_references_equivalent(cli_models[0], model, os.environ)
        for model in cli_models[1:]
    ):
        return _fail(
            "multiple -m/--model flags point to different models: "
            f"{sorted(set(cli_models))}. Pass one model."
        )

    if (
        env_res
        and cli_res
        and not main_model_references_equivalent(env_res.model, cli_res.model, os.environ)
    ):
        return _fail(
            "AICODE_MODEL and --model point to different models: "
            f"{env_res.model!r} != {cli_res.model!r}. Pass one model."
        )

    if env_res and env_res.model:
        print(env_res.model, flush=True)
        return 0

    if cli_res and cli_res.model:
        print(cli_res.model, flush=True)
        return 0

    fallback = resolve_main_model_from_env(os.environ)
    if fallback.error:
        where = f" ({fallback.path})" if fallback.path else ""
        return _fail(f"{fallback.source}{where}: {fallback.error}")
    if fallback.model:
        print(fallback.model, flush=True)
        return 0

    return _fail(
        "主模型未設定: AICODE_MODEL 未設、CLI 未帶 -m、deployment profile/local override "
        "也沒有 main.model。"
    )


if __name__ == "__main__":
    sys.exit(main())
