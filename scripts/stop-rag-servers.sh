#!/usr/bin/env bash
# 關掉 CodeTrail 附屬 server(embedding + reranker + VL)。
#
# 配套 `start-rag-servers.sh` 使用:那邊把 embed + rerank + vl 包進同一個 tmux
# session(`codetrail-rag`)的三個 window,這邊一次砍掉整個 session 就同時停掉三顆。
#
# 用法:
#   ./scripts/stop-rag-servers.sh
#   ./scripts/stop-rag-servers.sh --force   # tmux 關掉後仍占用目標 port 時,kill 該 PID
#
# 環境變數(可選):
#   SESSION                       tmux session 名稱,預設 codetrail-rag
#   AICODE_LLAMA_EMBED_BASE_URL   embedding server base URL,預設 http://localhost:8081
#   AICODE_LLAMA_RERANK_BASE_URL  reranker server base URL,預設 http://localhost:8082
#   AICODE_LLAMA_VL_BASE_URL      VL server base URL,預設 http://localhost:8083

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/rag-server-lib.sh
source "$SCRIPT_DIR/rag-server-lib.sh"

SESSION="${SESSION:-codetrail-rag}"
AICODE_LLAMA_EMBED_BASE_URL="${AICODE_LLAMA_EMBED_BASE_URL:-http://localhost:8081}"
AICODE_LLAMA_RERANK_BASE_URL="${AICODE_LLAMA_RERANK_BASE_URL:-http://localhost:8082}"
AICODE_LLAMA_VL_BASE_URL="${AICODE_LLAMA_VL_BASE_URL:-http://localhost:8083}"
FORCE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force)
            FORCE=1
            ;;
        -h|--help)
            sed -n '1,24p' "$0"
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            exit 1
            ;;
    esac
    shift
done

read -r EMBED_HOST EMBED_PORT EMBED_BASE_URL < <(rag_parse_base_url "$AICODE_LLAMA_EMBED_BASE_URL" 8081)
read -r RERANK_HOST RERANK_PORT RERANK_BASE_URL < <(rag_parse_base_url "$AICODE_LLAMA_RERANK_BASE_URL" 8082)
read -r VL_HOST VL_PORT VL_BASE_URL < <(rag_parse_base_url "$AICODE_LLAMA_VL_BASE_URL" 8083)

if command -v tmux >/dev/null 2>&1; then
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        tmux kill-session -t "$SESSION"
        echo "[+] 已關閉 tmux session '$SESSION'(embedding + reranker + VL 都停止)"
    else
        echo "[!] tmux session '$SESSION' 不存在,沒有 tmux session 要關"
    fi
else
    echo "[!] 找不到 tmux,跳過 tmux session 關閉;仍會檢查目標 port" >&2
fi

check_orphan_port() {
    local role="$1"
    local port="$2"
    local base_url="$3"
    local listener pids

    if ! listener="$(rag_port_listener "$port")"; then
        echo "[!] 無法檢查 $role port $port: ss 不在 PATH" >&2
        return 0
    fi

    if [[ -z "$listener" ]]; then
        echo "[+] $role port $port 已釋放 ($base_url)"
        return 0
    fi

    pids="$(printf '%s\n' "$listener" | rag_listener_pids | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
    if [[ -z "$pids" ]]; then
        echo "[!] $role port $port 仍被占用,但 ss 沒有提供 PID:" >&2
        echo "$listener" >&2
        return 0
    fi

    if (( FORCE )); then
        echo "[!] $role port $port 仍被 PID(s) $pids 占用;--force 將送出 kill" >&2
        local pid
        for pid in $pids; do
            kill "$pid" 2>/dev/null || true
        done
        return 0
    fi

    echo "[!] $role port $port 仍被孤兒 process 占用 (PID(s): $pids, base URL: $base_url)" >&2
    echo "    手動停止: kill $pids" >&2
    echo "    或使用: ./scripts/stop-rag-servers.sh --force" >&2
}

check_orphan_port "embedding" "$EMBED_PORT" "$EMBED_BASE_URL"
check_orphan_port "reranker" "$RERANK_PORT" "$RERANK_BASE_URL"
check_orphan_port "VL" "$VL_PORT" "$VL_BASE_URL"
