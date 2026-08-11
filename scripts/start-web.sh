#!/usr/bin/env bash
# 在背景 tmux session 啟動 CodeTrail 的 OpenCode web backend(帶 CodeTrail MCP)。
#
# web backend 與 standalone TUI 同源:沙箱 root、CodeTrail MCP 都靠「執行當下的目錄」
# 解析,所以必須先 cd 到你要分析的專案目錄再跑這支 —— 它會把 cwd 當成 backend 沙箱根。
# 腳本只是把 aicode web 包進背景 tmux;所有安全前置(拒 $HOME / /、能力偵測、
# hostname / 密碼硬規則)仍由 aicode web 本身執行。--tailscale 只允許綁定
# `tailscale ip -4` 當下回報的 100.64.0.0/10 位址,不會改成 0.0.0.0。
#
# 用法:
#   cd <分析專案>
#   /path/to/CodeTrail/scripts/start-web.sh                    # 本機 loopback
#   /path/to/CodeTrail/scripts/start-web.sh --tailscale        # A/B 機 tailnet
#   /path/to/CodeTrail/scripts/start-web.sh --dry-run        # 只印解析結果,不啟動
#   /path/to/CodeTrail/scripts/start-web.sh [其餘參數轉發 aicode web]
#
# 沒有 Tailscale 時的 headless fallback(在你自己的電腦):
#   ssh -L 4096:127.0.0.1:4096 <你>@<server>
#   再用本機瀏覽器開 http://127.0.0.1:4096
#
# 環境變數(可選):
#   AICODE_WEB_PORT      backend port,預設 4096(要跟 attach / ssh tunnel 一致)
#   WEB_SESSION          tmux session 名稱,預設 codetrail-web
#   WEB_HEALTH_TIMEOUT   等待 backend ready 的秒數,預設 720(首次 tool canary 可能數分鐘)
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
WEB_HEALTH_TIMEOUT="${WEB_HEALTH_TIMEOUT:-720}"

usage() {
    cat <<'EOF'
用法:
  cd <分析專案>
  aicode_web [--dry-run]              # 推薦:A 機只綁 Tailscale IPv4,B 機開印出的網址
  scripts/start-web.sh [--dry-run]     # 進階:只綁 A 機 loopback

選項:
  --tailscale   只綁 `tailscale ip -4` 回報的位址(aicode_web 會自動加)
  --dry-run     只印解析結果,不啟動 tmux / OpenCode
  -h, --help    顯示本說明
  --            後面的參數原樣交給 aicode web

port 請用 AICODE_WEB_PORT 覆寫(預設 4096)。--tailscale 模式不接受
--hostname / --mdns / --port,避免繞過已驗證的 Tailscale bind 或讓 health check 漂移。
EOF
}

DRY_RUN=0
TAILSCALE_MODE=0
WEB_FORWARD=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --dry-run)
            DRY_RUN=1
            ;;
        --tailscale)
            TAILSCALE_MODE=1
            ;;
        --)
            shift
            WEB_FORWARD+=("$@")
            break
            ;;
        *)
            WEB_FORWARD+=("$1")
            ;;
    esac
    shift
done

if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
    echo "ERROR: AICODE_WEB_PORT must be 1..65535 (got: $PORT)" >&2
    exit 1
fi
if [[ ! "$WEB_HEALTH_TIMEOUT" =~ ^[0-9]+$ ]] || (( WEB_HEALTH_TIMEOUT < 1 )); then
    echo "ERROR: WEB_HEALTH_TIMEOUT must be a positive integer" >&2
    exit 1
fi

BIND_HOST="127.0.0.1"
EXPOSURE="loopback"
if (( TAILSCALE_MODE )); then
    for arg in "${WEB_FORWARD[@]}"; do
        case "$arg" in
            --hostname|--hostname=*|--mdns|--mdns=*|--port|--port=*)
                echo "ERROR: aicode_web 會自行鎖定 Tailscale IP 與 port;不可轉發 $arg" >&2
                echo "       換 port 請用:AICODE_WEB_PORT=<PORT> aicode_web" >&2
                exit 1
                ;;
        esac
    done
    if ! command -v tailscale >/dev/null 2>&1; then
        echo "ERROR: A 機找不到 tailscale;請先安裝並登入,再執行 aicode_web" >&2
        exit 1
    fi
    if ! tailscale_output="$(tailscale ip -4 2>&1)"; then
        echo "ERROR: A 機的 Tailscale 尚未連線;tailscale ip -4 回報:" >&2
        printf '%s\n' "$tailscale_output" >&2
        echo "       請先完成 A/B 機 Tailscale 登入,再重跑 aicode_web" >&2
        exit 1
    fi
    # `tailscale ip` 在 CLI / daemon 版本不同時,即使成功也可能先印 warning。
    # 不可假設第一個非空白行就是 IP;只接受輸出中「整行」為 CGNAT IPv4
    # 的項目,其餘診斷文字一律忽略。
    BIND_HOST=""
    while IFS= read -r candidate; do
        candidate="${candidate#"${candidate%%[![:space:]]*}"}"
        candidate="${candidate%"${candidate##*[![:space:]]}"}"
        if [[ "$candidate" =~ ^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.([0-9]{1,3})\.([0-9]{1,3})$ ]]; then
            BIND_HOST="$candidate"
            break
        fi
    done <<<"$tailscale_output"
    if [[ ! "$BIND_HOST" =~ ^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.([0-9]{1,3})\.([0-9]{1,3})$ ]]; then
        echo "ERROR: tailscale ip -4 沒有回報有效的 100.64.0.0/10 位址" >&2
        printf '%s\n' "$tailscale_output" | sed 's/^/       /' >&2
        exit 1
    fi
    IFS=. read -r _ts_a _ts_b _ts_c _ts_d <<<"$BIND_HOST"
    if (( 10#$_ts_c > 255 || 10#$_ts_d > 255 )); then
        echo "ERROR: tailscale ip -4 回報無效 IPv4:$BIND_HOST" >&2
        exit 1
    fi
    EXPOSURE="tailscale"
    export AICODE_WEB_TAILSCALE_IP="$BIND_HOST"
    WEB_FORWARD+=("--hostname" "$BIND_HOST")
fi

# 即使呼叫端沒 export，也要讓 tmux pane 與 health check 使用完全相同的固定 port。
export AICODE_WEB_PORT="$PORT"

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
    echo "       例:cd ~/work/my-repo && aicode_web" >&2
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
if (( TAILSCALE_MODE )); then
    # aicode_web 的 GUI 在 B 機。抑制 OpenCode 嘗試從純文字 A 機呼叫 xdg-open；
    # 低階 `aicode web` / loopback launcher 不改使用者既有的 browser 行為。
    LAUNCH+="export BROWSER=/bin/true && "
fi
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

HEALTH_URL="http://$BIND_HOST:$PORT/"
BROWSER_URL="$HEALTH_URL"

if (( DRY_RUN )); then
    cat <<EOF
project=$PROJECT
port=$PORT
session=$WEB_SESSION
exposure=$EXPOSURE
bind_host=$BIND_HOST
health_url=$HEALTH_URL
browser_url=$BROWSER_URL
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

backend_session_gone() {
    ! tmux has-session -t "$WEB_SESSION" 2>/dev/null
}
backend_pane_dead() {
    [[ "$(tmux display-message -p -t "$WEB_SESSION" '#{pane_dead}' 2>/dev/null || true)" == "1" ]]
}
http_up() {
    local code
    code="$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' --max-time 2 "$HEALTH_URL" 2>/dev/null || true)"
    [[ -n "$code" && "$code" != "000" ]]
}
print_ready() {
    echo ""
    echo "[+] web backend ready → $HEALTH_URL"
    echo "    🧪 web 模式為實驗功能(開發中);穩定路徑是 standalone TUI(aicode)。"
    if (( TAILSCALE_MODE )); then
        echo ""
        echo "    B 機瀏覽器請直接開:$BROWSER_URL"
        echo "    A 機不需要 GUI;backend 已在背景 tmux 執行。"
        echo "    只綁 A 機的 Tailscale IPv4($BIND_HOST),不會 listen 在 LAN / 公網介面。"
        echo "    傳輸走 Tailscale 加密隧道;可存取者由 tailnet ACL 決定。"
    else
        echo "    本機有桌面:直接用瀏覽器開上面網址。"
        echo "    headless fallback:B 機跑 `ssh -L $PORT:127.0.0.1:$PORT <帳號>@<A機>`,"
        echo "                      再開 http://127.0.0.1:$PORT"
    fi
    echo "    ⚠️ 沙箱鎖在這個專案;web UI『切換資料夾』對 CodeTrail 無效,請無視(換專案=另起 backend)。"
    echo "    停止:aicode_web stop    看 log:tmux a -t $WEB_SESSION (Ctrl-b d 退出)"
}
print_failed_pane() {
    echo "       tmux 最後輸出:" >&2
    tmux capture-pane -p -t "$WEB_SESSION" -S -80 2>/dev/null | sed 's/^/         /' >&2 || true
}

REUSE_SESSION=0
if tmux has-session -t "$WEB_SESSION" 2>/dev/null; then
    existing_project="$(tmux show-options -qv -t "$WEB_SESSION" @codetrail_project 2>/dev/null || true)"
    existing_host="$(tmux show-options -qv -t "$WEB_SESSION" @codetrail_host 2>/dev/null || true)"
    existing_port="$(tmux show-options -qv -t "$WEB_SESSION" @codetrail_port 2>/dev/null || true)"
    if [[ "$existing_project" != "$PROJECT" || "$existing_host" != "$BIND_HOST" || "$existing_port" != "$PORT" ]]; then
        echo "ERROR: tmux session '$WEB_SESSION' 已被另一個 web backend 使用。" >&2
        echo "       existing project=${existing_project:-unknown}, bind=${existing_host:-unknown}:${existing_port:-unknown}" >&2
        echo "       requested project=$PROJECT, bind=$BIND_HOST:$PORT" >&2
        echo "       先停止:aicode_web stop" >&2
        exit 1
    fi
    if backend_pane_dead; then
        echo "ERROR: 上一次 web backend 已退出。" >&2
        print_failed_pane
        echo "       清理後請重跑:aicode_web stop && aicode_web" >&2
        exit 1
    fi
    if http_up; then
        echo "[=] web backend 已在執行(沿用 tmux session '$WEB_SESSION')"
        print_ready
        exit 0
    fi
    REUSE_SESSION=1
    echo "[=] web backend 尚在 preflight;繼續等待既有 tmux session '$WEB_SESSION'"
fi

# port 占用檢查:只算會跟本次 bind host 衝突的 listener。loopback 模式不會把
# Tailscale IP 上既有的 listener 誤判成衝突;Tailscale 模式則會檢查該 Tailscale IP。
# ss 不在 PATH 時 rag_port_listener 回非 0,跳過檢查。
if (( ! REUSE_SESSION )); then
    if raw_listener="$(rag_port_listener "$PORT")"; then
        bind_host_re="${BIND_HOST//./\\.}"
        conflicting_listener="$(printf '%s\n' "$raw_listener" | grep -E "(^|[[:space:]])(${bind_host_re}|0\.0\.0\.0|\*|\[(::|::1)\]):${PORT}([[:space:]]|\$)" || true)"
        if [[ -n "$conflicting_listener" ]]; then
            echo "ERROR: $BIND_HOST:$PORT 已被占用:" >&2
            echo "$conflicting_listener" >&2
            echo "       先停掉它,或用 AICODE_WEB_PORT 換 port。" >&2
            exit 1
        fi
    fi

    # 背景啟動:metadata 讓重跑能辨識是否為同一個 project/bind。remain-on-exit
    # 保留 preflight 的最後畫面,即使 A 機只有文字介面也能直接看到失敗根因。
    tmux new-session -d -s "$WEB_SESSION" -c "$PROJECT"
    tmux set-option -t "$WEB_SESSION" @codetrail_project "$PROJECT"
    tmux set-option -t "$WEB_SESSION" @codetrail_host "$BIND_HOST"
    tmux set-option -t "$WEB_SESSION" @codetrail_port "$PORT"
    tmux set-option -w -t "$WEB_SESSION" remain-on-exit on
    tmux send-keys -t "$WEB_SESSION" "$LAUNCH" Enter

    echo "[+] 啟動 web backend 於 tmux session '$WEB_SESSION'(專案:$PROJECT)"
fi

deadline=$((SECONDS + WEB_HEALTH_TIMEOUT))
wait_started=$SECONDS
while (( SECONDS < deadline )); do
    if http_up; then
        print_ready
        exit 0
    fi
    if backend_session_gone; then
        echo "ERROR: web backend 啟動後立刻結束(多半是 aicode web 的 preflight 擋下:" >&2
        echo "       llama-server 沒起 / 主模型未設 / Tailscale 斷線 / opencode 太舊)。" >&2
        exit 1
    fi
    if backend_pane_dead; then
        echo "ERROR: web backend 在 preflight / 啟動階段退出。" >&2
        print_failed_pane
        tmux kill-session -t "$WEB_SESSION" 2>/dev/null || true
        echo "       已清理失敗的 tmux session;修正上面錯誤後直接重跑 aicode_web。" >&2
        exit 1
    fi
    elapsed=$((SECONDS - wait_started))
    if (( elapsed > 0 && elapsed % 15 == 0 )); then
        echo "[.] 等待 aicode preflight / web ready… ${elapsed}s / ${WEB_HEALTH_TIMEOUT}s (log:tmux a -t $WEB_SESSION)"
    fi
    sleep 1
done

echo "ERROR: web backend 未在 ${WEB_HEALTH_TIMEOUT}s 內 ready($HEALTH_URL)。" >&2
echo "       看 log:tmux a -t $WEB_SESSION   (Ctrl-b d 退出);停止:$CODETRAIL_HOME/scripts/stop-web.sh" >&2
exit 1
