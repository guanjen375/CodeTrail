#!/usr/bin/env python3
"""啟動前的 ctx 容量閘:讀 llama-server 真實 n_ctx,確認 CodeTrail 使用的 n_ctx
不會「超過」它。正常情況下 aicode 已經用 scripts/resolve_server_ctx.py 把 server
n_ctx 自動帶進 AICODE_N_CTX,所以這裡幾乎都是 requested == server、
直接放行;這道閘真正擋的是「設定值比 server 實值還大」:requested >
server n_ctx → UNSAFE (prompt 會被截斷) → refuse to start。
requested <= server n_ctx 一律 SAFE 放行 ——「小於」不是安全問題,不再擋。

呼叫方式 (aicode wrapper 用):
    python3 scripts/ctx_safety_check.py

讀取的環境變數:
    AICODE_MODEL                  必填;aicode wrapper 會用 resolve_main_model.py
                                  解析好以後再 export。沒設 → 直接 fail-loud,
                                  CodeTrail 不假定任何預設主模型。
    AICODE_N_CTX                  主模型 n_ctx;正常由 aicode 自動帶入 server 實值
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
- 修法只談同一個主 n_ctx，不再要求使用者維護另一個 max。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import deployment_profile  # noqa: E402
import gpu_safety  # noqa: E402
import n_ctx  # noqa: E402
from model_resolution import normalize_main_model  # noqa: E402


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
        profile_ctx = deployment_profile.load_effective_profile(os.environ).service("main").ctx
        resolved_ctx = n_ctx.resolve_n_ctx(
            os.environ,
            default=profile_ctx or n_ctx.DEFAULT_N_CTX,
            default_source="deployment profile main.ctx",
        )
        requested = resolved_ctx.value
    except (ValueError, deployment_profile.ProfileError) as exc:
        _print(f"n_ctx 設定無效: {exc}")
        _print("refuse to start.")
        return 2

    base_url = os.environ.get("AICODE_LLAMA_BASE_URL", "http://localhost:8080")
    accept_risk = os.environ.get("AICODE_ACCEPT_CTX_RISK", "").lower() in ("1", "true", "yes")

    verdict = gpu_safety.check_safety(requested, base_url=base_url)

    if verdict.status == "UNKNOWN":
        _print(f"UNKNOWN: {verdict.reason}")
        _print(f"        無法判定 — 放行繼續。先用 `curl -s {base_url}/health` 確認 server 可連。")
        return 0

    # check_safety 的 SAFE 保證 requested <= server n_ctx。CodeTrail 只擋「超過」
    # (會截斷 prompt);requested < server 不是安全問題(只是沒用滿 server 容量),
    # 一律放行。正常情況下 aicode 已把 server n_ctx 自動帶進 requested,所以這裡
    # 多半是 requested == server。
    if verdict.status == "SAFE":
        _print(
            f"SAFE: model={model} requested_ctx={requested}"
            f" <= server n_ctx={verdict.server_n_ctx}"
        )
        return 0

    # verdict.status == "UNSAFE":requested > server n_ctx,超過 server 真實上限,
    # prompt 會被截斷。能走到這裡通常代表 profile / 手動 n_ctx 與實際 server
    # 啟動值不同。
    _print(f"UNSAFE: model={model} requested_ctx={requested}")
    _print(f"        {verdict.reason}")
    _print_block("        ", verdict.detail_lines)
    _print("")
    _print("        建議任一處理:")
    _print("          (a) 重跑 ./set_config.sh 設定主模型 n_ctx，然後重啟 server")
    if verdict.server_n_ctx and verdict.server_n_ctx > 0:
        _print(f"          (b) 或把本次 AICODE_N_CTX 設成 <= {verdict.server_n_ctx}")
        _print(f"          (c) 或重啟 llama-server 用 `-c {requested}` (確認 VRAM 夠)")
    else:
        _print("          (b) 或讓 AICODE_N_CTX <= server 真實 n_ctx")
        _print("          (c) 或重啟 llama-server 把 `-c <N>` 開到相同值")
    _print("          (d) export AICODE_ACCEPT_CTX_RISK=1 (本次接受截斷風險)")
    _print("          (e) export AICODE_CTX_SAFETY_DISABLE=1 (永久關閉此檢查)")
    _print("")

    if accept_risk:
        _print("AICODE_ACCEPT_CTX_RISK=1 已設 — 放行,但 prompt 超過 server ctx 會被截斷")
        return 0

    _print("refuse to start.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
