#!/usr/bin/env bash
# 從 nvidia-smi 檢查 CodeTrail 的 llama-server GPU processes。
#
# 預期 main + embedding + reranker + VL 共四個不同的 llama-server PID。
# nvidia-smi 無法辨認各 PID 的角色或 port，因此這裡只確認 process 數量。
#
# 用法:
#   ./scripts/check-status.sh
#   ./scripts/check-status.sh --strict
#
# 環境變數(可選):
#   EXPECTED_LLAMA_SERVERS   預期的 llama-server PID 數，預設 4
#
# 預設是人工查看模式：數量不足仍 exit 0，避免啟用 `set -e` 的 SSH shell
# 因狀態不符而退出。CI / 自動化請加 --strict，數量不足時 exit 1。

set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ./scripts/check-status.sh [--strict]

列出所有 GPU 上的 llama-server process；預期 main、embedding、reranker、VL
共四個不同 PID。預設只報告狀態並 exit 0；--strict 會在數量不足時 exit 1。
EOF
}

STRICT=0
while (( $# > 0 )); do
    case "$1" in
        --strict)
            STRICT=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

EXPECTED_LLAMA_SERVERS="${EXPECTED_LLAMA_SERVERS:-4}"
if [[ ! "$EXPECTED_LLAMA_SERVERS" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: EXPECTED_LLAMA_SERVERS 必須是正整數" >&2
    exit 2
fi

report_only_exit() {
    local strict_exit_code="$1"
    if (( STRICT )); then
        exit "$strict_exit_code"
    fi
    echo "[INFO] report-only mode: exit 0；自動化檢查請使用 --strict"
    exit 0
}

if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "[FAIL] 找不到 nvidia-smi，無法檢查 GPU processes" >&2
    report_only_exit 2
fi

GPU_PROCESS_OUTPUT=""
if ! GPU_PROCESS_OUTPUT="$(nvidia-smi \
    --query-compute-apps=pid,process_name,gpu_uuid,used_gpu_memory \
    --format=csv,noheader,nounits 2>&1)"; then
    echo "[FAIL] nvidia-smi 無法查詢 GPU processes" >&2
    [[ -z "$GPU_PROCESS_OUTPUT" ]] || echo "$GPU_PROCESS_OUTPUT" >&2
    report_only_exit 2
fi

trim_space() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

declare -A SEEN_LLAMA_PIDS=()
unique_count=0

echo "CodeTrail llama-server GPU processes (all GPUs):"
while IFS=',' read -r raw_pid raw_process_name raw_gpu_uuid raw_used_memory; do
    pid="$(trim_space "${raw_pid:-}")"
    process_name="$(trim_space "${raw_process_name:-}")"
    gpu_uuid="$(trim_space "${raw_gpu_uuid:-}")"
    used_memory="$(trim_space "${raw_used_memory:-}")"

    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    process_basename="${process_name##*/}"
    [[ "$process_basename" == llama-server* ]] || continue

    printf '[GPU] PID=%-7s GPU=%s VRAM=%s MiB process=%s\n' \
        "$pid" "${gpu_uuid:-unknown}" "${used_memory:-unknown}" "$process_name"

    if [[ -z "${SEEN_LLAMA_PIDS[$pid]+present}" ]]; then
        SEEN_LLAMA_PIDS["$pid"]=1
        unique_count=$((unique_count + 1))
    fi
done <<< "$GPU_PROCESS_OUTPUT"

if (( unique_count >= EXPECTED_LLAMA_SERVERS )); then
    echo "[PASS] 偵測到 $unique_count 個不同的 llama-server PID（預期至少 $EXPECTED_LLAMA_SERVERS）"
    echo "[INFO] nvidia-smi 只能確認 PID 數量，無法分辨 main / embedding / reranker / VL"
    exit 0
fi

echo "[WARN] 只偵測到 $unique_count 個不同的 llama-server PID（預期至少 $EXPECTED_LLAMA_SERVERS）" >&2
echo "[INFO] nvidia-smi 只能確認 PID 數量，無法分辨 main / embedding / reranker / VL" >&2
report_only_exit 1
