#!/usr/bin/env python3
"""把主 llama-server 的真實 n_ctx 解析出來,印到 stdout 給 aicode wrapper 用。

設計重點:server 啟動時的 `-c <N>` (= /props 的 n_ctx) 是 runtime 真值。
aicode 啟動時跑這支,把讀到的值以 AICODE_N_CTX 傳給 CodeTrail，並自動同步
OpenCode 的鏡像欄位；使用者不需要維護另一個 max。

合約:
    stdout  只印一個整數 n_ctx;讀不到就「什麼都不印」(空字串)。
    stderr  人看的診斷訊息。
    exit    永遠 0 —— 這是「取值器」不是「閘」,讀不到也不擋啟動(aicode 會退回
            deployment profile 的 main.ctx)。

讀取的環境變數:
    AICODE_LLAMA_BASE_URL   主 llama-server URL;預設 http://localhost:8080
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import gpu_safety  # noqa: E402


def main() -> int:
    base_url = os.environ.get("AICODE_LLAMA_BASE_URL", "http://localhost:8080")
    try:
        server = gpu_safety.query_server_info(base_url)
    except Exception as exc:  # pragma: no cover - 防呆,任何 I/O 例外都不該擋啟動
        print(f"[resolve-ctx] 讀取 {base_url}/props 失敗: {exc}", file=sys.stderr)
        return 0

    if server is None or not server.n_ctx or server.n_ctx <= 0:
        print(
            f"[resolve-ctx] 無法從 llama-server ({base_url}) 取得 n_ctx — "
            "改用 deployment profile 的 main.ctx。",
            file=sys.stderr,
        )
        return 0

    # 只有這一行會進 stdout;aicode 直接拿來 export。
    print(int(server.n_ctx))
    return 0


if __name__ == "__main__":
    sys.exit(main())
