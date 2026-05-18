#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""啟動前的 ctx 安全閘:預測「目前模型 + 目前 GPU + 要求的 ctx 上限」
會不會逼 Ollama 把 model offload 到 CPU。

呼叫方式 (aicode wrapper 用):
    python3 scripts/ctx_safety_check.py

讀取的環境變數:
    AICODE_MODEL                  必要;沒設則跳過檢查 (exit 0)
    AICODE_DYNAMIC_NUM_CTX_MAX    要檢查的 ctx 上限;預設 65536 (跟 config.py 同)
    AICODE_OLLAMA_BASE_URL        Ollama URL;預設 http://localhost:11434
    AICODE_ACCEPT_CTX_RISK        =1 時即使預測 UNSAFE 也 exit 0 (使用者覆蓋)
    AICODE_CTX_SAFETY_DISABLE     =1 時整個檢查跳過 (除錯 / 緊急逃生)

退出碼:
    0  SAFE / UNKNOWN / 使用者覆蓋  → 可以繼續 exec opencode
    2  UNSAFE 且使用者沒覆蓋        → wrapper 應該 abort

設計守則:
- fail-loud:UNSAFE 一定明確 print 出來,不偷偷 clamp。
- UNKNOWN (拿不到 GPU / Ollama / metadata) 一律放行,只 warn — 不能因為
  CI 環境或遠端 Ollama 沒辦法估算就 block 使用者。
- 給可複製貼上的 export 指令,方便使用者一行修好。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import gpu_safety  # noqa: E402


# 跟 config.py 的 DYNAMIC_NUM_CTX_MAX 預設保持一致。
# 若 config.py 改了預設,這裡也要改 (或乾脆 import config — 但 import config
# 會牽連 sys 模組,在 wrapper 啟動的瞬間多載入幾個 module 不划算)。
DEFAULT_CTX_MAX = 65536


def _print(line: str) -> None:
    print(f"[ctx-safety] {line}", flush=True)


def _print_block(prefix: str, lines: list[str]) -> None:
    for ln in lines:
        print(f"[ctx-safety] {prefix} {ln}", flush=True)


def main() -> int:
    if os.environ.get("AICODE_CTX_SAFETY_DISABLE", "").lower() in ("1", "true", "yes"):
        _print("disabled via AICODE_CTX_SAFETY_DISABLE")
        return 0

    model = os.environ.get("AICODE_MODEL", "").strip()
    if not model:
        # 沒指定模型代表使用者走 config.py 預設;這個檢查不是 doctor,
        # 用 AICODE_MODEL 當訊號表示「使用者有意識在用某個模型」
        # 否則跳過,避免綁死 config.DEFAULT_MODEL。
        _print("AICODE_MODEL 未設,跳過 ctx 安全檢查")
        return 0

    try:
        requested = int(os.environ.get("AICODE_DYNAMIC_NUM_CTX_MAX", "") or DEFAULT_CTX_MAX)
    except ValueError:
        _print(
            f"AICODE_DYNAMIC_NUM_CTX_MAX={os.environ.get('AICODE_DYNAMIC_NUM_CTX_MAX')!r}"
            f" 不是數字,跳過檢查"
        )
        return 0

    base_url = os.environ.get("AICODE_OLLAMA_BASE_URL", "http://localhost:11434")
    accept_risk = os.environ.get("AICODE_ACCEPT_CTX_RISK", "").lower() in ("1", "true", "yes")

    verdict = gpu_safety.check_safety(model, requested, base_url=base_url)

    if verdict.status == "SAFE":
        _print(
            f"SAFE: model={model} ctx={requested}"
            f" (safe cap≈{verdict.computed_max_ctx},"
            f" est need={verdict.vram_needed_gb:.1f}GB"
            f" / total={verdict.vram_total_gb:.1f}GB)"
        )
        return 0

    if verdict.status == "UNKNOWN":
        _print(f"UNKNOWN: {verdict.reason}")
        _print("        無法判定 — 放行繼續。若真的 offload 請參考 ollama ps")
        return 0

    # UNSAFE
    _print(f"UNSAFE: model={model} ctx={requested}")
    _print(f"        {verdict.reason}")
    _print_block("        ", verdict.detail_lines)
    _print("")
    _print("        建議任一處理:")
    if verdict.computed_max_ctx and verdict.computed_max_ctx > 0:
        # 給可貼上的指令;對齊到 1024,且不超過估算 safe cap
        suggested = (verdict.computed_max_ctx // 1024) * 1024
        _print(f"          (a) export AICODE_DYNAMIC_NUM_CTX_MAX={suggested}")
    else:
        _print(
            "          (a) 換較小的模型 (e.g. devstral:24b / gpt-oss:20b)"
            " — 這台 GPU 連此模型 weights 都裝不下"
        )
    _print("          (b) export AICODE_ACCEPT_CTX_RISK=1 (我知道會 offload,還是要跑)")
    _print("          (c) export AICODE_CTX_SAFETY_DISABLE=1 (永久關閉此檢查)")
    _print("")

    if accept_risk:
        _print("AICODE_ACCEPT_CTX_RISK=1 已設 — 放行,但預期會 offload")
        return 0

    _print("refuse to start.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
