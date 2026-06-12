#!/usr/bin/env bash
# 停止 start-web.sh 啟動的 OpenCode web backend(tmux session codetrail-web)。
#
# 用法:
#   /path/to/CodeTrail/scripts/stop-web.sh
#   /path/to/CodeTrail/scripts/stop-web.sh --force   # tmux 關掉後 port 仍被占用時 kill PID
#
# 環境變數(可選):
#   AICODE_WEB_PORT   backend port,預設 4096
#   WEB_SESSION       tmux session 名稱,預設 codetrail-web

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/rag-server-lib.sh
source "$SCRIPT_DIR/rag-server-lib.sh"

PORT="${AICODE_WEB_PORT:-4096}"
WEB_SESSION="${WEB_SESSION:-codetrail-web}"
FORCE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force)
            FORCE=1
            ;;
        -h|--help)
            sed -n '1,10p' "$0"
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            exit 1
            ;;
    esac
    shift
done

if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
    echo "ERROR: AICODE_WEB_PORT must be 1..65535 (got: $PORT)" >&2
    exit 1
fi

if command -v tmux >/dev/null 2>&1; then
    if tmux has-session -t "$WEB_SESSION" 2>/dev/null; then
        tmux kill-session -t "$WEB_SESSION"
        echo "[+] 已關閉 tmux session '$WEB_SESSION'(web backend 已停止)"
    else
        echo "[!] tmux session '$WEB_SESSION' 不存在,沒有 web backend 要關"
    fi
else
    echo "[!] 找不到 tmux,跳過 session 關閉;仍會檢查目標 port" >&2
fi

# 確認 port 釋放。backend 收到 kill 到真正關閉 socket 之間有 race,所以先輪詢幾秒;
# 真的還占著才當孤兒處理。
if ! command -v ss >/dev/null 2>&1; then
    echo "[!] 無法檢查 port $PORT:ss 不在 PATH" >&2
else
    listener=""
    deadline=$((SECONDS + 5))
    while (( SECONDS < deadline )); do
        # 只看 backend 的 loopback listener;tailscale serve 的 tailscale IP:PORT 不算占用。
        listener="$(rag_port_listener "$PORT" 2>/dev/null | grep -E "(^|[[:space:]])(127\.[0-9.]+|0\.0\.0\.0|\*|\[::1?\]):${PORT}([[:space:]]|\$)" || true)"
        [[ -z "$listener" ]] && break
        sleep 1
    done

    if [[ -z "$listener" ]]; then
        echo "[+] port $PORT 已釋放"
    else
        pids="$(printf '%s\n' "$listener" | rag_listener_pids | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
        if [[ -z "$pids" ]]; then
            echo "[!] port $PORT 仍被占用,但 ss 沒提供 PID:" >&2
            echo "$listener" >&2
        elif (( FORCE )); then
            echo "[!] port $PORT 仍被 PID(s) $pids 占用;--force 送出 kill" >&2
            for pid in $pids; do
                kill "$pid" 2>/dev/null || true
            done
        else
            echo "[!] port $PORT 仍被孤兒 process 占用 (PID(s): $pids)" >&2
            echo "    手動停止: kill $pids" >&2
            echo "    或使用:   $SCRIPT_DIR/stop-web.sh --force" >&2
        fi
    fi
fi
