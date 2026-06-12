#!/usr/bin/env bash
# 在背景 tmux session 啟動 CodeTrail 的 OpenCode web backend(帶 CodeTrail MCP)。
#
# web backend 與 standalone TUI 同源:沙箱 root、CodeTrail MCP 都靠「執行當下的目錄」
# 解析,所以必須先 cd 到你要分析的專案目錄再跑這支 —— 它會把 cwd 當成 backend 沙箱根。
# 腳本只是把 aicode web 包進背景 tmux;所有安全前置(拒 $HOME / /、能力偵測、密碼硬規則)
# 仍由 aicode web 本身執行,這支不繞過。
#
# 用法:
#   cd <分析專案>
#   /path/to/CodeTrail/scripts/start-web.sh
#   /path/to/CodeTrail/scripts/start-web.sh --dry-run        # 只印解析結果,不啟動
#   /path/to/CodeTrail/scripts/start-web.sh [其餘參數轉發 aicode web]
#
# headless server 連線(在你自己的電腦):
#   ssh -L 4096:127.0.0.1:4096 <你>@<server>
#   再用本機瀏覽器開 http://127.0.0.1:4096
#
# 環境變數(可選):
#   AICODE_WEB_PORT      backend port,預設 4096(要跟 attach / ssh tunnel 一致)
#   WEB_SESSION          tmux session 名稱,預設 codetrail-web
#   WEB_HEALTH_TIMEOUT   等待 backend ready 的秒數,預設 30
#   AICODE_MODEL 等 AICODE_* / AI_CODE_* 變數照常透傳給 aicode web
#
# 啟動後:
#   ./scripts/stop-web.sh      停止 backend
#   tmux a -t codetrail-web    接回去看 log(Ctrl-b d 退出)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/rag-server-lib.sh
source "$SCRIPT_DIR/rag-server-lib.sh"

CODETRAIL_HOME="$(cd "$SCRIPT_DIR/.." && pwd)"
AICODE="$CODETRAIL_HOME/aicode"
VENV_ACTIVATE="$CODETRAIL_HOME/.venv/bin/activate"

PORT="${AICODE_WEB_PORT:-4096}"
WEB_SESSION="${WEB_SESSION:-codetrail-web}"
WEB_HEALTH_TIMEOUT="${WEB_HEALTH_TIMEOUT:-30}"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    sed -n '1,27p' "$0"
    exit 0
fi
DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
    shift
fi
WEB_FORWARD=("$@")   # 其餘參數原樣轉發 aicode web

if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
    echo "ERROR: AICODE_WEB_PORT must be 1..65535 (got: $PORT)" >&2
    exit 1
fi
if [[ ! "$WEB_HEALTH_TIMEOUT" =~ ^[0-9]+$ ]] || (( WEB_HEALTH_TIMEOUT < 1 )); then
    echo "ERROR: WEB_HEALTH_TIMEOUT must be a positive integer" >&2
    exit 1
fi

PROJECT="$(pwd -P)"

# 防呆:cwd 必須是「你要分析的專案」,不能是 CodeTrail repo 本身 / 其子目錄 / $HOME / /。
# (aicode web 也會拒 $HOME 與 /,這裡多擋「誤把工具 repo / scripts 目錄當專案」並提早 fail-loud。)
if [[ "$PROJECT" == "/" ]]; then
    echo "ERROR: 不要從 / 啟動 web backend" >&2
    exit 1
fi
if [[ -n "${HOME:-}" && "$PROJECT" == "$HOME" ]]; then
    echo "ERROR: 不要從 \$HOME 啟動 web backend;cd 到具體專案目錄再跑" >&2
    exit 1
fi
if [[ "$PROJECT" == "$CODETRAIL_HOME" || "$PROJECT" == "$CODETRAIL_HOME"/* ]]; then
    echo "ERROR: 你人在 CodeTrail repo 內($PROJECT)。" >&2
    echo "       這支要從『你要分析的專案』目錄跑,不是從工具 repo / scripts 目錄。" >&2
    echo "       例:cd ~/work/my-repo && $CODETRAIL_HOME/scripts/start-web.sh" >&2
    exit 1
fi

# 組 backend 啟動指令。⚠️ tmux pane 不會繼承呼叫端 shell 的環境變數,所以這裡把
# CodeTrail 相關的 AICODE_* / AI_CODE_* / OPENCODE_* 明確 export 進去 —— 否則
# AICODE_WEB_PORT、AI_CODE_* 透傳、OPENCODE_SERVER_PASSWORD 會被默默丟掉。
# 之後 cd 專案 → activate venv(有的話)→ exec aicode web [forward]。
LAUNCH=""
while IFS= read -r _var; do
    [[ -n "$_var" ]] || continue
    LAUNCH+="export $_var=$(rag_shell_quote "${!_var}") && "
done < <(compgen -v | grep -E '^(AICODE_|AI_CODE_|OPENCODE_)' | sort || true)
# sandbox root 對齊 cwd(start-web 的合約:在專案目錄跑,該目錄就是 backend 沙箱根)
LAUNCH+="export AICODE_ROOT=$(rag_shell_quote "$PROJECT") && "
LAUNCH+="cd $(rag_shell_quote "$PROJECT")"
if [[ -f "$VENV_ACTIVATE" ]]; then
    LAUNCH+=" && source $(rag_shell_quote "$VENV_ACTIVATE")"
fi
LAUNCH+=" && exec $(rag_shell_quote "$AICODE") web"
for arg in "${WEB_FORWARD[@]}"; do
    LAUNCH+=" $(rag_shell_quote "$arg")"
done

HEALTH_URL="http://127.0.0.1:$PORT/"

if (( DRY_RUN )); then
    cat <<EOF
project=$PROJECT
port=$PORT
session=$WEB_SESSION
health_url=$HEALTH_URL
health_timeout=$WEB_HEALTH_TIMEOUT
aicode=$AICODE
venv_activate=$([[ -f "$VENV_ACTIVATE" ]] && echo "$VENV_ACTIVATE" || echo "none")
launch=$LAUNCH
EOF
    exit 0
fi

# --- 真正啟動 ---------------------------------------------------------------

for cmd in tmux curl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "ERROR: 找不到 $cmd,請先安裝後再啟動 web backend" >&2
        exit 1
    fi
done

if [[ ! -x "$AICODE" ]]; then
    echo "ERROR: 找不到可執行的 aicode: $AICODE" >&2
    exit 1
fi

if tmux has-session -t "$WEB_SESSION" 2>/dev/null; then
    echo "ERROR: tmux session '$WEB_SESSION' 已存在(web backend 可能已在跑)。" >&2
    echo "       接回去:tmux a -t $WEB_SESSION   或先停止:$CODETRAIL_HOME/scripts/stop-web.sh" >&2
    exit 1
fi

# port 占用檢查:只算「會跟 backend 的 127.0.0.1 bind 衝突」的 listener。
# tailscale serve 會在 tailscale IP:PORT listen(這正是建議設定),不衝突,別誤判成占用。
# ss 不在 PATH 時 rag_port_listener 回非 0,跳過檢查。
if raw_listener="$(rag_port_listener "$PORT")"; then
    loopback_listener="$(printf '%s\n' "$raw_listener" | grep -E "(^|[[:space:]])(127\.[0-9.]+|0\.0\.0\.0|\*|\[::1?\]):${PORT}([[:space:]]|\$)" || true)"
    if [[ -n "$loopback_listener" ]]; then
        echo "ERROR: port $PORT(loopback)已被占用:" >&2
        echo "$loopback_listener" >&2
        echo "       先停掉它,或設 AICODE_WEB_PORT 換 port(attach / ssh tunnel / tailscale serve 也要一致)。" >&2
        exit 1
    fi
fi

# 背景啟動:建立空 session(cwd=專案)再 send-keys 啟動指令(同 RAG 那組做法,避開 tmux 指令引號地雷)。
tmux new-session -d -s "$WEB_SESSION" -c "$PROJECT"
tmux send-keys -t "$WEB_SESSION" "$LAUNCH" Enter

echo "[+] 啟動 web backend 於 tmux session '$WEB_SESSION'(專案:$PROJECT)"

backend_session_gone() {
    ! tmux has-session -t "$WEB_SESSION" 2>/dev/null
}
http_up() {
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$HEALTH_URL" 2>/dev/null || true)"
    [[ -n "$code" && "$code" != "000" ]]
}

deadline=$((SECONDS + WEB_HEALTH_TIMEOUT))
while (( SECONDS < deadline )); do
    if http_up; then
        # 偵測這個 port 是否已掛在 tailscale serve(tailnet),有的話直接給固定網址。
        ts_url=""
        if command -v tailscale >/dev/null 2>&1; then
            ts_url="$(tailscale serve status 2>/dev/null | awk -v port="$PORT" '
                /^https:\/\// { url=$1 }
                $0 ~ ("127\\.0\\.0\\.1:" port "$") { print url; exit }
            ')"
        fi
        echo ""
        echo "[+] web backend ready → $HEALTH_URL"
        echo "    🧪 web 模式為實驗功能(開發中);穩定路徑是 standalone TUI(aicode)。"
        if [ -n "$ts_url" ]; then
            echo "    遠端(推薦):Tailscale → ${ts_url}/   ← 加到瀏覽器最愛,免 SSH tunnel"
        else
            echo "    本機有桌面:直接用瀏覽器開上面網址。"
            echo "    headless server 遠端連法(擇一):"
            echo "      a) Tailscale(推薦,固定網址):在 server 跑 \`tailscale serve --bg --https=$PORT $PORT\`,再開它印出的 ts.net 網址"
            echo "         ⚠️ 用 serve(tailnet 內),絕不可 funnel(公網)。"
            echo "      b) SSH tunnel:你的電腦跑 \`ssh -L $PORT:127.0.0.1:$PORT <你的帳號>@<此 server>\`,再開 http://127.0.0.1:$PORT"
        fi
        echo "    ⚠️ 沙箱鎖在這個專案;web UI『切換資料夾』對 CodeTrail 無效,請無視(換專案=另起 backend)。"
        echo "    停止:$CODETRAIL_HOME/scripts/stop-web.sh    看 log:tmux a -t $WEB_SESSION (Ctrl-b d 退出)"
        exit 0
    fi
    if backend_session_gone; then
        echo "ERROR: web backend 啟動後立刻結束(多半是 aicode web 的 preflight 擋下:" >&2
        echo "       llama-server 沒起 / 主模型未設 / hostname 非 loopback 未設密碼 / opencode 太舊)。" >&2
        echo "       前景重跑一次看完整錯誤訊息:" >&2
        echo "         cd $PROJECT && $AICODE web" >&2
        exit 1
    fi
    sleep 1
done

echo "ERROR: web backend 未在 ${WEB_HEALTH_TIMEOUT}s 內 ready($HEALTH_URL)。" >&2
echo "       看 log:tmux a -t $WEB_SESSION   (Ctrl-b d 退出);停止:$CODETRAIL_HOME/scripts/stop-web.sh" >&2
exit 1
