#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主模型解析器:給 `aicode` wrapper 用的薄入口。

優先順序:
    1. AICODE_MODEL 環境變數
    2. CLI argv 內的 `-m <MODEL>` / `--model <MODEL>` / `-m=<MODEL>` / `--model=<MODEL>`
       (只接受 `ollama/<MODEL>` 或 bare Ollama model name)
    3. ~/.config/opencode/opencode.json 的 `"model"` 欄位 (必須是 `ollama/<MODEL>`)

行為:
    - 找到 → 印 bare model name (沒有 `ollama/` prefix) 到 stdout, exit 0。
    - 找不到 / placeholder (`<CODE_MODEL>` 之類) / 空字串 → 印 fail-loud 訊息到 stderr,
      exit 2。

刻意不 import config.py: aicode wrapper 在跑 ctx_safety_check 之前就需要解析,
而 config.py 會 parse 多個 env var, 任一錯誤都可能讓這個薄入口在印出可讀訊息
前先爆掉。同時保持邏輯與 config._resolve_main_model() 對齊。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _is_placeholder(value: str) -> bool:
    if not value:
        return True
    return "<" in value or ">" in value


def _strip_ollama_prefix(value: str) -> str:
    if value.startswith("ollama/"):
        return value[len("ollama/"):]
    return value


def _from_env() -> str:
    raw = os.environ.get("AICODE_MODEL", "").strip()
    if not raw or _is_placeholder(raw):
        return ""
    return _strip_ollama_prefix(raw)


def _from_argv(argv: list[str]) -> str:
    """掃 argv 找 `-m` / `--model`(及 `=` 形式)。

    只接受 `ollama/<MODEL>` 或 bare Ollama model name。明確帶其他 provider
    prefix (例如 `openai/`、`anthropic/`、`vertex/`) 視為非 Ollama, 回空字串
    讓呼叫端 fail-loud (CodeTrail 只支援 Ollama)。
    """
    i = 0
    raw = ""
    while i < len(argv):
        arg = argv[i]
        if arg == "-m" or arg == "--model":
            if i + 1 < len(argv):
                raw = argv[i + 1].strip()
                break
        elif arg.startswith("-m="):
            raw = arg[3:].strip()
            break
        elif arg.startswith("--model="):
            raw = arg[len("--model="):].strip()
            break
        i += 1
    if not raw or _is_placeholder(raw):
        return ""
    # 已知的非 Ollama provider prefix 不接受 — CodeTrail 只支援 Ollama。
    # Ollama 自己的 namespace (qllama/, library/, hf.co/, etc.) 看起來也含 slash,
    # 沒辦法只用 slash 判斷, 所以只 blacklist 明確的 cloud provider 名。
    _NON_OLLAMA = (
        "openai/", "anthropic/", "vertex/", "google/", "cohere/", "groq/",
        "azure/", "bedrock/", "aws/", "together/", "perplexity/", "mistral.ai/",
    )
    lowered = raw.lower()
    for prefix in _NON_OLLAMA:
        if lowered.startswith(prefix):
            return ""
    return _strip_ollama_prefix(raw)


def _from_opencode_config() -> str:
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if not home:
        return ""
    path = Path(home) / ".config" / "opencode" / "opencode.json"
    try:
        if not path.is_file():
            return ""
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    raw = data.get("model")
    if not isinstance(raw, str):
        return ""
    raw = raw.strip()
    if _is_placeholder(raw):
        return ""
    return _strip_ollama_prefix(raw)


def _fail(msg: str) -> int:
    print(f"[aicode] {msg}", file=sys.stderr, flush=True)
    print(
        "[aicode] CodeTrail 不內建主聊天 / 程式推導模型。請先 ollama pull 一顆 Ollama\n"
        "         模型, 然後任選一種方式設定 (擇一即可):\n"
        "           1) export AICODE_MODEL=<CODE_MODEL>            (最優先)\n"
        "           2) aicode -m ollama/<CODE_MODEL>               (per-run CLI 旗標)\n"
        "           3) ~/.config/opencode/opencode.json 設\n"
        '                \"model\": \"ollama/<CODE_MODEL>\"\n'
        "         <CODE_MODEL> 是佔位符, 必須替換成實際模型名稱\n"
        "         (例如 qwen3-coder:30b、devstral:24b、qllama/some-model:tag 等)。\n"
        "         詳見 README.md。",
        file=sys.stderr,
        flush=True,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])

    resolved = _from_env()
    source = "AICODE_MODEL"
    if not resolved:
        resolved = _from_argv(args)
        source = "-m/--model"
    if not resolved:
        resolved = _from_opencode_config()
        source = "opencode.json"

    if not resolved:
        return _fail("主模型未設定: AICODE_MODEL 未設、CLI 未帶 -m、opencode.json 也沒有有效 model。")

    if _is_placeholder(resolved):
        return _fail(
            f"主模型來源 ({source}) 是 placeholder ({resolved!r}), 必須替換成實際模型名稱。"
        )

    print(resolved, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
