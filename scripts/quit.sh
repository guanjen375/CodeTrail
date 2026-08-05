#!/usr/bin/env bash
# 關閉所有 CodeTrail llama-server tmux 視窗:主模型(codetrail-main)+
# embedding / reranker / VL 三個附屬模型(codetrail-rag)。
# 會等到 process 真正退出、VRAM 從 nvidia-smi 消失才返回(大模型的
# teardown 需要數十秒是正常的;上限 AICODE_STOP_TIMEOUT,預設 120s)。
# 孤兒 llama-server 仍占用 port 時:./scripts/quit.sh --force
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/stop_servers.py" --scope all "$@"
