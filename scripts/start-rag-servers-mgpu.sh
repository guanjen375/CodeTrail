#!/usr/bin/env bash
# 掃描本機 NVIDIA GPU，讓使用者選一顆後啟動三個 CodeTrail RAG server。
#
# 用法:
#   ./scripts/start-rag-servers-mgpu.sh
#   ./scripts/start-rag-servers-mgpu.sh --gpu 1
#   ./scripts/start-rag-servers-mgpu.sh --gpu 1 --dry-run
#
# 選定後，embedding / reranker / VL 都會綁到同一顆 GPU；實際的模型定位、
# port 檢查、tmux 啟動與 health check 由 start-rag-servers.sh 負責。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAG_LAUNCHER="$SCRIPT_DIR/start-rag-servers.sh"

usage() {
    cat <<'EOF'
Usage: ./scripts/start-rag-servers-mgpu.sh [--gpu INDEX] [--dry-run]

掃描 nvidia-smi 列出的所有 NVIDIA GPU，選定一顆後在該 GPU 啟動
embedding、reranker、VL 三個 RAG server（預設 ports 8081-8083）。

Options:
  --gpu INDEX  直接選擇 nvidia-smi 顯示的 GPU index，不進入互動提示
  --dry-run    只印出三個 server 的設定與命令，不啟動 tmux
  -h, --help   顯示這份說明
EOF
}

trim_space() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

REQUESTED_GPU=""
LAUNCHER_ARGS=()
while (( $# > 0 )); do
    case "$1" in
        --gpu)
            if (( $# < 2 )); then
                echo "ERROR: --gpu 需要一個 GPU index" >&2
                exit 1
            fi
            REQUESTED_GPU="$2"
            shift 2
            ;;
        --gpu=*)
            REQUESTED_GPU="${1#*=}"
            shift
            ;;
        --dry-run)
            LAUNCHER_ARGS+=("--dry-run")
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ ! -f "$RAG_LAUNCHER" ]]; then
    echo "ERROR: 找不到底層 launcher: $RAG_LAUNCHER" >&2
    exit 1
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "ERROR: 找不到 nvidia-smi，無法掃描 NVIDIA GPU" >&2
    exit 1
fi

GPU_OUTPUT=""
if ! GPU_OUTPUT="$(nvidia-smi \
    --query-gpu=index,name,memory.total,memory.free,uuid \
    --format=csv,noheader,nounits 2>&1)"; then
    echo "ERROR: nvidia-smi 掃描 GPU 失敗" >&2
    [[ -z "$GPU_OUTPUT" ]] || echo "$GPU_OUTPUT" >&2
    exit 1
fi

GPU_INDEXES=()
GPU_NAMES=()
GPU_TOTAL_MEMORY=()
GPU_FREE_MEMORY=()
GPU_UUIDS=()
while IFS=',' read -r raw_index raw_name raw_total raw_free raw_uuid; do
    index="$(trim_space "${raw_index:-}")"
    name="$(trim_space "${raw_name:-}")"
    total="$(trim_space "${raw_total:-}")"
    free="$(trim_space "${raw_free:-}")"
    uuid="$(trim_space "${raw_uuid:-}")"

    [[ "$index" =~ ^[0-9]+$ ]] || continue
    GPU_INDEXES+=("$index")
    GPU_NAMES+=("${name:-unknown}")
    GPU_TOTAL_MEMORY+=("${total:-unknown}")
    GPU_FREE_MEMORY+=("${free:-unknown}")
    GPU_UUIDS+=("$uuid")
done <<< "$GPU_OUTPUT"

if (( ${#GPU_INDEXES[@]} == 0 )); then
    echo "ERROR: nvidia-smi 沒有回報任何可用的 NVIDIA GPU" >&2
    exit 1
fi

echo "偵測到 ${#GPU_INDEXES[@]} 顆 NVIDIA GPU:"
for position in "${!GPU_INDEXES[@]}"; do
    printf '  GPU %s: %s | VRAM %s MiB total, %s MiB free | %s\n' \
        "${GPU_INDEXES[$position]}" \
        "${GPU_NAMES[$position]}" \
        "${GPU_TOTAL_MEMORY[$position]}" \
        "${GPU_FREE_MEMORY[$position]}" \
        "${GPU_UUIDS[$position]:-UUID unavailable}"
done

SELECTED_POSITION=""
find_gpu_position() {
    local candidate="$1"
    local position
    SELECTED_POSITION=""
    for position in "${!GPU_INDEXES[@]}"; do
        if [[ "${GPU_INDEXES[$position]}" == "$candidate" ]]; then
            SELECTED_POSITION="$position"
            return 0
        fi
    done
    return 1
}

if [[ -n "$REQUESTED_GPU" ]]; then
    if ! find_gpu_position "$REQUESTED_GPU"; then
        echo "ERROR: GPU index '$REQUESTED_GPU' 不在 nvidia-smi 掃描結果中" >&2
        exit 1
    fi
else
    while true; do
        printf '請輸入要使用的 GPU index（q 取消）: '
        if ! IFS= read -r choice; then
            echo >&2
            echo "ERROR: 沒有讀到 GPU 選擇；無互動環境請使用 --gpu INDEX" >&2
            exit 1
        fi
        choice="$(trim_space "$choice")"
        if [[ "$choice" == "q" || "$choice" == "Q" ]]; then
            echo "已取消。"
            exit 0
        fi
        if find_gpu_position "$choice"; then
            break
        fi
        echo "無效的 GPU index: '$choice'，請從上方清單選擇。" >&2
    done
fi

SELECTED_INDEX="${GPU_INDEXES[$SELECTED_POSITION]}"
SELECTED_UUID="${GPU_UUIDS[$SELECTED_POSITION]}"
SELECTED_CUDA_DEVICE="$SELECTED_UUID"
if [[ -z "$SELECTED_CUDA_DEVICE" || "$SELECTED_CUDA_DEVICE" == "N/A" ]]; then
    SELECTED_CUDA_DEVICE="$SELECTED_INDEX"
fi

# 用 UUID 綁定可避免 PCI enumeration 順序改變後選到不同實體卡。三個 role
# override 也一併覆寫，避免呼叫端殘留的 EMBED_GPU 等變數繞過本次選擇。
export CUDA_VISIBLE_DEVICES="$SELECTED_CUDA_DEVICE"
export EMBED_GPU="$SELECTED_CUDA_DEVICE"
export RERANK_GPU="$SELECTED_CUDA_DEVICE"
export VL_GPU="$SELECTED_CUDA_DEVICE"

echo "[+] 已選擇 GPU $SELECTED_INDEX: ${GPU_NAMES[$SELECTED_POSITION]} ($SELECTED_CUDA_DEVICE)"
echo "[+] 將 embedding / reranker / VL 全部綁定到這顆 GPU"

exec bash "$RAG_LAUNCHER" "${LAUNCHER_ARGS[@]}"
