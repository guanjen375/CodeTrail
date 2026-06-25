#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""啟動前的 ctx 安全閘:讀 llama-server 真實 n_ctx,要求它跟使用者要求的
dynamic ctx 上限「完全相等」。requested > server n_ctx → UNSAFE (prompt 會被
截斷);requested < server n_ctx → MISMATCH (對齊漂移,server 多出來的 ctx 用
不到、且 TUI/MCP 兩邊預算易不同步)。兩種情況都 refuse to start。

呼叫方式 (aicode wrapper 用):
    python3 scripts/ctx_safety_check.py

讀取的環境變數:
    AICODE_MODEL                  必填;aicode wrapper 會用 resolve_main_model.py
                                  解析好以後再 export。沒設 → 直接 fail-loud,
                                  CodeTrail 不假定任何預設主模型。
    AICODE_DYNAMIC_NUM_CTX_MAX    要檢查的 ctx 上限;預設 65532 (跟 config.py 同)
    AICODE_LLAMA_BASE_URL         主 llama-server URL;預設 http://localhost:8080
    AICODE_ACCEPT_CTX_RISK        =1 時即使 UNSAFE 也 exit 0 (使用者覆蓋)
    AICODE_CTX_SAFETY_DISABLE     =1 時整個檢查跳過 (除錯 / 緊急逃生)

退出碼:
    0  SAFE / UNKNOWN / 使用者覆蓋          → 可以繼續 exec opencode
    2  UNSAFE 或 AICODE_MODEL 未設且使用者沒覆蓋 → wrapper 應該 abort

設計守則:
- fail-loud:UNSAFE / 模型未設一定明確 print 出來,不偷偷 clamp / fallback。
- UNKNOWN (server 未啟動 / 拿不到 /props) 一律放行,只 warn — 不能因為 CI 環境
  或遠端 server 沒辦法觀測就 block 使用者。
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
from model_resolution import normalize_main_model  # noqa: E402


DEFAULT_CTX_MAX = 65532


def _print(line: str) -> None:
    print(f"[ctx-safety] {line}", flush=True)


def _print_block(prefix: str, lines: list[str]) -> None:
    for ln in lines:
        print(f"[ctx-safety] {prefix} {ln}", flush=True)


def main() -> int:
    if os.environ.get("AICODE_CTX_SAFETY_DISABLE", "").lower() in ("1", "true", "yes"):
        _print("disabled via AICODE_CTX_SAFETY_DISABLE")
        return 0

    model_res = normalize_main_model(os.environ.get("AICODE_MODEL", ""), "AICODE_MODEL")
    model = model_res.model
    if not model:
        _print("AICODE_MODEL 未設或無效 (例如 <MODEL> placeholder / provider prefix)。")
        if model_res.error:
            _print(f"        {model_res.error}")
        _print("        CodeTrail 不內建預設主模型, 無法做 ctx 安全檢查。")
        _print("        請啟動 llama-server (建議 port 8080) 並設定:")
        _print("          export AICODE_MODEL=<MODEL>  # registry name 或 GGUF 路徑")
        _print("        或透過 aicode wrapper 自動解析 (aicode 會讀 opencode.json 主模型)。")
        _print("        若刻意要跳過檢查 (例如 CI), 設 AICODE_CTX_SAFETY_DISABLE=1。")
        _print("refuse to start.")
        return 2

    try:
        requested = int(os.environ.get("AICODE_DYNAMIC_NUM_CTX_MAX", "") or DEFAULT_CTX_MAX)
    except ValueError:
        _print(
            f"AICODE_DYNAMIC_NUM_CTX_MAX={os.environ.get('AICODE_DYNAMIC_NUM_CTX_MAX')!r}"
            f" 不是數字,跳過檢查"
        )
        return 0

    base_url = os.environ.get("AICODE_LLAMA_BASE_URL", "http://localhost:8080")
    accept_risk = os.environ.get("AICODE_ACCEPT_CTX_RISK", "").lower() in ("1", "true", "yes")

    verdict = gpu_safety.check_safety(requested, base_url=base_url)

    if verdict.status == "UNKNOWN":
        _print(f"UNKNOWN: {verdict.reason}")
        _print(f"        無法判定 — 放行繼續。先用 `curl -s {base_url}/health` 確認 server 可連。")
        return 0

    # check_safety 的 SAFE 只保證 requested <= server n_ctx。CodeTrail 再嚴一階:
    # 要求 requested == server n_ctx,讓三條 ctx 預算 (AICODE_DYNAMIC_NUM_CTX_MAX /
    # OpenCode limit.context / server -c) 完全一致。只有相等才放行。
    if verdict.status == "SAFE" and verdict.server_n_ctx == requested:
        _print(
            f"SAFE: model={model} requested_ctx={requested}"
            f" == server n_ctx={verdict.server_n_ctx}"
        )
        return 0

    if verdict.status == "UNSAFE":
        # requested > server n_ctx — 超過 server 真實上限,prompt 會被截斷。
        _print(f"UNSAFE: model={model} requested_ctx={requested}")
        _print(f"        {verdict.reason}")
    else:
        # SAFE 但 requested < server n_ctx — 不截斷,純粹是對齊漂移。
        _print(
            f"MISMATCH: model={model} requested_ctx={requested}"
            f" < server n_ctx={verdict.server_n_ctx}"
        )
        _print("        AICODE_DYNAMIC_NUM_CTX_MAX 必須等於 llama-server 啟動時的 -c;")
        _print("        目前 server 開得比 CodeTrail 大,多出來的 ctx 用不到,")
        _print("        且 OpenCode TUI / CodeTrail MCP 兩邊 ctx 預算容易各走各的。")
    _print_block("        ", verdict.detail_lines)
    _print("")
    _print("        建議任一處理 (目標是讓兩邊數字完全相等):")
    if verdict.server_n_ctx and verdict.server_n_ctx > 0:
        _print(f"          (a) export AICODE_DYNAMIC_NUM_CTX_MAX={verdict.server_n_ctx}  (對齊 server n_ctx)")
        _print(f"          (b) 重啟 llama-server 用 `-c {requested}` (把 server 對齊到 CodeTrail;確認 VRAM 夠)")
    else:
        _print(f"          (a) export AICODE_DYNAMIC_NUM_CTX_MAX=<server 真實 n_ctx>")
        _print(f"          (b) 重啟 llama-server 把 `-c <N>` 設成跟 AICODE_DYNAMIC_NUM_CTX_MAX 相同")
    _print("          (c) export AICODE_ACCEPT_CTX_RISK=1 (本次接受不一致)")
    _print("          (d) export AICODE_CTX_SAFETY_DISABLE=1 (永久關閉此檢查)")
    _print("")

    if accept_risk:
        _print("AICODE_ACCEPT_CTX_RISK=1 已設 — 放行,但 CodeTrail 與 server ctx 不一致")
        return 0

    _print("refuse to start.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
