#!/usr/bin/env bash
# 關掉 CodeTrail RAG 兩顆 server(embedding + reranker)。
#
# 配套 `start-rag-servers.sh` 使用:那邊把 embed + rerank 包進同一個 tmux
# session(`codetrail-rag`)的兩個 window,這邊一次砍掉整個 session 就同時停掉兩顆。
#
# 用法:
#   ./scripts/stop-rag-servers.sh
#
# 環境變數(可選):
#   SESSION   tmux session 名稱   預設 codetrail-rag

set -euo pipefail

SESSION="${SESSION:-codetrail-rag}"

if ! command -v tmux >/dev/null 2>&1; then
    echo "ERROR: 找不到 tmux" >&2
    exit 1
fi

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "[!] tmux session '$SESSION' 不存在,沒有東西要關"
    exit 0
fi

tmux kill-session -t "$SESSION"
echo "[+] 已關閉 tmux session '$SESSION'(embedding + reranker 都停止)"
