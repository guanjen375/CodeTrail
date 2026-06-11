#!/usr/bin/env bash
# 啟動 CodeTrail 必要附屬 server(embedding + reranker + VL)。
#
# 三顆附屬模型合併在一個 tmux session (`codetrail-rag`) 的三個 window 內,
# 使用者只需要管理一個 session。
#
# 用法:
#   ./scripts/start-rag-servers.sh
#   ./scripts/start-rag-servers.sh --dry-run
#
# 環境變數(可選):
#   MODELS_DIR                         GGUF 模型根目錄,預設 ~/models
#   EMBED_MODEL                        embedding GGUF 完整路徑
#   RERANK_MODEL                       reranker GGUF 完整路徑
#   VL_GGUF                            VL GGUF 完整路徑
#   VL_MMPROJ                          VL mmproj GGUF 完整路徑
#   LLAMA_BIN                          llama-server 執行檔路徑
#   SESSION                            tmux session 名稱,預設 codetrail-rag
#   AICODE_LLAMA_EMBED_BASE_URL        embedding server base URL,預設 http://localhost:8081
#   AICODE_LLAMA_RERANK_BASE_URL       reranker server base URL,預設 http://localhost:8082
#   AICODE_LLAMA_VL_BASE_URL           VL server base URL,預設 http://localhost:8083
#   CUDA_VISIBLE_DEVICES               三顆 server 共用的 CUDA device filter
#   EMBED_GPU / RERANK_GPU / VL_GPU    單顆 server 的 CUDA_VISIBLE_DEVICES 覆寫
#   RAG_HEALTH_TIMEOUT                 /health status=ok 等待秒數,預設 60
#   AICODE_RERANK_FALLBACK_POLICY      embedding | main_model | error,預設 error
#
# 啟動後操作:
#   tmux a -t codetrail-rag        接回去看 log
#   Ctrl-b n                       切換 embed / rerank / vl window
#   Ctrl-b d                       退出 tmux,server 留在背景
#   ./scripts/stop-rag-servers.sh  一次關掉三顆 server

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/rag-server-lib.sh
source "$SCRIPT_DIR/rag-server-lib.sh"

MODELS_DIR="${MODELS_DIR:-$HOME/models}"
LLAMA_BIN="${LLAMA_BIN:-$HOME/llama.cpp/build/bin/llama-server}"
SESSION="${SESSION:-codetrail-rag}"
AICODE_LLAMA_EMBED_BASE_URL="${AICODE_LLAMA_EMBED_BASE_URL:-http://localhost:8081}"
AICODE_LLAMA_RERANK_BASE_URL="${AICODE_LLAMA_RERANK_BASE_URL:-http://localhost:8082}"
AICODE_LLAMA_VL_BASE_URL="${AICODE_LLAMA_VL_BASE_URL:-http://localhost:8083}"
RAG_HEALTH_TIMEOUT="${RAG_HEALTH_TIMEOUT:-60}"
AICODE_RERANK_FALLBACK_POLICY="${AICODE_RERANK_FALLBACK_POLICY:-error}"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
    shift
fi
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    sed -n '1,39p' "$0"
    exit 0
fi
if [[ $# -gt 0 ]]; then
    echo "ERROR: unknown argument: $1" >&2
    exit 1
fi

if [[ ! "$RAG_HEALTH_TIMEOUT" =~ ^[0-9]+$ ]] || (( RAG_HEALTH_TIMEOUT < 1 )); then
    echo "ERROR: RAG_HEALTH_TIMEOUT must be a positive integer" >&2
    exit 1
fi
rag_validate_rerank_policy "$AICODE_RERANK_FALLBACK_POLICY"

read -r EMBED_HOST EMBED_PORT EMBED_BASE_URL < <(rag_parse_base_url "$AICODE_LLAMA_EMBED_BASE_URL" 8081)
read -r RERANK_HOST RERANK_PORT RERANK_BASE_URL < <(rag_parse_base_url "$AICODE_LLAMA_RERANK_BASE_URL" 8082)
read -r VL_HOST VL_PORT VL_BASE_URL < <(rag_parse_base_url "$AICODE_LLAMA_VL_BASE_URL" 8083)
EMBED_BIND_HOST="$(rag_bind_host_for "$EMBED_HOST")"
RERANK_BIND_HOST="$(rag_bind_host_for "$RERANK_HOST")"
VL_BIND_HOST="$(rag_bind_host_for "$VL_HOST")"

find_model_path() {
    local default_path="$1"
    local search_dir="$2"
    local pattern="$3"
    local dry_run="$4"

    if [[ -f "$default_path" ]]; then
        printf '%s\n' "$default_path"
        return 0
    fi

    local had_nullglob=0
    shopt -q nullglob && had_nullglob=1 || true
    shopt -s nullglob
    local matches=("$search_dir"/$pattern)
    if (( had_nullglob == 0 )); then
        shopt -u nullglob
    fi

    if (( ${#matches[@]} > 0 )); then
        printf '%s\n' "${matches[0]}"
        return 0
    fi

    if (( dry_run )); then
        printf '%s\n' "$default_path"
        return 0
    fi

    return 1
}

DEFAULT_EMBED_MODEL="$MODELS_DIR/bge-m3/bge-m3-f16.gguf"
DEFAULT_RERANK_MODEL="$MODELS_DIR/bge-reranker-v2-m3/bge-reranker-v2-m3-Q8_0.gguf"
DEFAULT_VL_GGUF="$MODELS_DIR/qwen3-vl/Qwen3VL-8B-Instruct-Q4_K_M.gguf"
DEFAULT_VL_MMPROJ="$MODELS_DIR/qwen3-vl/mmproj-Qwen3VL-8B-Instruct-F16.gguf"

EMBED_MODEL="${EMBED_MODEL:-}"
RERANK_MODEL="${RERANK_MODEL:-}"
VL_GGUF="${VL_GGUF:-}"
VL_MMPROJ="${VL_MMPROJ:-}"
if [[ -z "$EMBED_MODEL" ]]; then
    EMBED_MODEL="$(find_model_path "$DEFAULT_EMBED_MODEL" "$MODELS_DIR/bge-m3" 'bge-m3*.gguf' "$DRY_RUN" || true)"
fi
if [[ -z "$RERANK_MODEL" ]]; then
    RERANK_MODEL="$(find_model_path "$DEFAULT_RERANK_MODEL" "$MODELS_DIR/bge-reranker-v2-m3" 'bge-reranker-v2-m3*.gguf' "$DRY_RUN" || true)"
fi
if [[ -z "$VL_GGUF" ]]; then
    VL_GGUF="$(find_model_path "$DEFAULT_VL_GGUF" "$MODELS_DIR/qwen3-vl" 'Qwen3VL-8B-Instruct*.gguf' "$DRY_RUN" || true)"
fi
if [[ -z "$VL_MMPROJ" ]]; then
    VL_MMPROJ="$(find_model_path "$DEFAULT_VL_MMPROJ" "$MODELS_DIR/qwen3-vl" 'mmproj-Qwen3VL-8B-Instruct*.gguf' "$DRY_RUN" || true)"
fi

embed_cmd_string() {
    local prefix
    prefix="$(rag_gpu_prefix "${EMBED_GPU:-}")"
    printf '%s%s\n' "$prefix" "$(rag_quote_command \
        "$LLAMA_BIN" -m "$EMBED_MODEL" \
        --host "$EMBED_BIND_HOST" --port "$EMBED_PORT" \
        -c 8192 --embedding --pooling cls -ngl 99)"
}

rerank_cmd_string() {
    local prefix
    prefix="$(rag_gpu_prefix "${RERANK_GPU:-}")"
    printf '%s%s\n' "$prefix" "$(rag_quote_command \
        "$LLAMA_BIN" -m "$RERANK_MODEL" \
        --host "$RERANK_BIND_HOST" --port "$RERANK_PORT" \
        -c 8192 --embedding --pooling rank --reranking -ngl 99)"
}

vl_cmd_string() {
    local prefix
    prefix="$(rag_gpu_prefix "${VL_GPU:-}")"
    printf '%s%s\n' "$prefix" "$(rag_quote_command \
        "$LLAMA_BIN" -m "$VL_GGUF" --mmproj "$VL_MMPROJ" \
        --host "$VL_BIND_HOST" --port "$VL_PORT" \
        -c 8192 -ngl 99)"
}

if (( DRY_RUN )); then
    cat <<EOF
embed_base_url=$EMBED_BASE_URL
embed_host=$EMBED_HOST
embed_bind_host=$EMBED_BIND_HOST
embed_port=$EMBED_PORT
embed_model=$EMBED_MODEL
embed_command=$(embed_cmd_string)
rerank_base_url=$RERANK_BASE_URL
rerank_host=$RERANK_HOST
rerank_bind_host=$RERANK_BIND_HOST
rerank_port=$RERANK_PORT
rerank_model=$RERANK_MODEL
rerank_command=$(rerank_cmd_string)
vl_base_url=$VL_BASE_URL
vl_host=$VL_HOST
vl_bind_host=$VL_BIND_HOST
vl_port=$VL_PORT
vl_gguf=$VL_GGUF
vl_mmproj=$VL_MMPROJ
vl_command=$(vl_cmd_string)
rerank_fallback_policy=$AICODE_RERANK_FALLBACK_POLICY
health_timeout=$RAG_HEALTH_TIMEOUT
EOF
    exit 0
fi

check_required_commands() {
    local missing=0
    for cmd in tmux curl python3; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            echo "ERROR: 找不到 $cmd,請先安裝後再啟動 RAG servers" >&2
            missing=1
        fi
    done
    if (( missing )); then
        exit 1
    fi
}

check_model_files() {
    if [[ -z "$EMBED_MODEL" || ! -f "$EMBED_MODEL" ]]; then
        echo "ERROR: 找不到 embedding 模型: ${EMBED_MODEL:-$DEFAULT_EMBED_MODEL}" >&2
        echo "       依 README §2.3 下載 bge-m3,或設定 EMBED_MODEL=/path/to/bge-m3*.gguf" >&2
        exit 1
    fi

    if [[ -z "$RERANK_MODEL" || ! -f "$RERANK_MODEL" ]]; then
        echo "ERROR: 找不到 reranker 模型: ${RERANK_MODEL:-$DEFAULT_RERANK_MODEL}" >&2
        echo "       依 README §2.3 下載 bge-reranker-v2-m3,或設定 RERANK_MODEL=/path/to/bge-reranker*.gguf" >&2
        exit 1
    fi

    if [[ -z "$VL_GGUF" || ! -f "$VL_GGUF" ]]; then
        echo "ERROR: 找不到 VL 模型: ${VL_GGUF:-$DEFAULT_VL_GGUF}" >&2
        echo "       依 README §2.4 下載 qwen3-vl GGUF,或設定 VL_GGUF=/path/to/Qwen3VL*.gguf" >&2
        exit 1
    fi

    if [[ -z "$VL_MMPROJ" || ! -f "$VL_MMPROJ" ]]; then
        echo "ERROR: 找不到 VL mmproj: ${VL_MMPROJ:-$DEFAULT_VL_MMPROJ}" >&2
        echo "       依 README §2.4 下載 mmproj,或設定 VL_MMPROJ=/path/to/mmproj*.gguf" >&2
        exit 1
    fi
}

port_error_and_exit() {
    local role="$1"
    local port="$2"
    local base_url="$3"
    local listener="$4"
    echo "ERROR: $role port $port 已被占用 (base URL: $base_url)" >&2
    if [[ -n "$listener" ]]; then
        echo "$listener" >&2
    fi
    echo "       請先停止該 process,或改用 AICODE_LLAMA_${role^^}_BASE_URL 指到未占用的 port。" >&2
    exit 1
}

check_port_free() {
    local role="$1"
    local port="$2"
    local base_url="$3"
    local listener

    if listener="$(rag_port_listener "$port")"; then
        if [[ -n "$listener" ]]; then
            port_error_and_exit "$role" "$port" "$base_url" "$listener"
        fi
        return 0
    fi

    if curl -fsS --max-time 1 "$(rag_health_url "$base_url")" >/dev/null 2>&1; then
        port_error_and_exit "$role" "$port" "$base_url" "health endpoint already responds"
    fi

    echo "[!] ss 不在 PATH,只能用 curl 粗略檢查 $role port $port" >&2
}

check_ports_free() {
    check_port_free "EMBED" "$EMBED_PORT" "$EMBED_BASE_URL"
    if [[ "$EMBED_PORT" == "$RERANK_PORT" && "$EMBED_HOST" == "$RERANK_HOST" ]]; then
        echo "ERROR: embedding 與 reranker 指到同一個 host:port ($EMBED_HOST:$EMBED_PORT)" >&2
        echo "       請調整 AICODE_LLAMA_EMBED_BASE_URL 或 AICODE_LLAMA_RERANK_BASE_URL。" >&2
        exit 1
    fi
    if [[ "$EMBED_PORT" == "$VL_PORT" && "$EMBED_HOST" == "$VL_HOST" ]]; then
        echo "ERROR: embedding 與 VL 指到同一個 host:port ($EMBED_HOST:$EMBED_PORT)" >&2
        echo "       請調整 AICODE_LLAMA_EMBED_BASE_URL 或 AICODE_LLAMA_VL_BASE_URL。" >&2
        exit 1
    fi
    if [[ "$RERANK_PORT" == "$VL_PORT" && "$RERANK_HOST" == "$VL_HOST" ]]; then
        echo "ERROR: reranker 與 VL 指到同一個 host:port ($RERANK_HOST:$RERANK_PORT)" >&2
        echo "       請調整 AICODE_LLAMA_RERANK_BASE_URL 或 AICODE_LLAMA_VL_BASE_URL。" >&2
        exit 1
    fi
    check_port_free "RERANK" "$RERANK_PORT" "$RERANK_BASE_URL"
    check_port_free "VL" "$VL_PORT" "$VL_BASE_URL"
}

wait_for_health() {
    local role="$1"
    local base_url="$2"
    local timeout="$3"
    local started_at="$SECONDS"
    local deadline=$((started_at + timeout))
    local last_status="unreachable"

    while (( SECONDS < deadline )); do
        last_status="$(rag_health_status "$base_url" || true)"
        if [[ "$last_status" == "ok" ]]; then
            echo "[+] $role health OK: $(rag_health_url "$base_url") status=ok"
            return 0
        fi
        [[ -n "$last_status" ]] || last_status="unreachable"
        sleep 1
    done

    echo "ERROR: $role server 未在 ${timeout}s 內 ready: $(rag_health_url "$base_url") last_status=$last_status" >&2
    echo "       看 log: tmux a -t $SESSION" >&2
    if [[ "$role" == "reranker" ]]; then
        echo "       若 log 顯示 unknown option,你的 llama.cpp build 可能不支援 reranking;" >&2
        echo "       --reranking / --pooling rank 旗標名稱跨版本改過,請更新 llama.cpp 或改用相容旗標。" >&2
    fi
    return 1
}

check_required_commands

if [[ ! -x "$LLAMA_BIN" ]]; then
    echo "ERROR: llama-server 不存在或不可執行: $LLAMA_BIN" >&2
    echo "       依 README §1.5 build llama.cpp,或設定 LLAMA_BIN=..." >&2
    exit 1
fi

check_model_files

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "ERROR: tmux session '$SESSION' 已存在。要重啟先砍掉:" >&2
    echo "         ./scripts/stop-rag-servers.sh" >&2
    exit 1
fi

check_ports_free

# --- 啟動 embedding (window 0) ---------------------------------------------

tmux new-session -d -s "$SESSION" -n embed
tmux send-keys -t "$SESSION:embed" "$(embed_cmd_string)" Enter

echo "[+] 啟動 embedding server ($EMBED_BASE_URL) 於 tmux $SESSION:embed"
wait_for_health "embedding" "$EMBED_BASE_URL" "$RAG_HEALTH_TIMEOUT"

# --- 啟動 reranker (window 1) ----------------------------------------------

tmux new-window -t "$SESSION" -n rerank
tmux send-keys -t "$SESSION:rerank" "$(rerank_cmd_string)" Enter
echo "[+] 啟動 reranker server  ($RERANK_BASE_URL) 於 tmux $SESSION:rerank"
wait_for_health "reranker" "$RERANK_BASE_URL" "$RAG_HEALTH_TIMEOUT"

# --- 啟動 VL (window 2) ------------------------------------------------------

tmux new-window -t "$SESSION" -n vl
tmux send-keys -t "$SESSION:vl" "$(vl_cmd_string)" Enter
echo "[+] 啟動 VL server        ($VL_BASE_URL) 於 tmux $SESSION:vl"
wait_for_health "VL" "$VL_BASE_URL" "$RAG_HEALTH_TIMEOUT"

# --- 收尾提示 ---------------------------------------------------------------

cat <<EOF

Aux model servers ready。驗證:
  curl -s $(rag_health_url "$EMBED_BASE_URL")
  curl -s $(rag_health_url "$RERANK_BASE_URL")
  curl -s $(rag_health_url "$VL_BASE_URL")
一次關掉三顆:
  ./scripts/stop-rag-servers.sh

偵錯時看 log(平常不用):
  tmux a -t $SESSION         (Ctrl-b n 切 embed/rerank/vl window,Ctrl-b d 退出)
EOF
