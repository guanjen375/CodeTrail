#!/usr/bin/env bash
# Shared helpers for CodeTrail RAG llama-server launch/stop scripts.

rag_parse_base_url() {
    local url="${1%/}"
    local default_port="$2"
    local scheme hostport host port

    if [[ "$url" =~ ^([a-zA-Z][a-zA-Z0-9+.-]*)://([^/?#]+)(.*)$ ]]; then
        scheme="${BASH_REMATCH[1],,}"
        hostport="${BASH_REMATCH[2]}"
    else
        echo "ERROR: invalid llama-server base URL: $1" >&2
        return 1
    fi

    if [[ "$hostport" == \[*\]* ]]; then
        host="${hostport%%]*}"
        host="${host#[}"
        local after_bracket="${hostport#*]}"
        if [[ "$after_bracket" == :* ]]; then
            port="${after_bracket#:}"
        else
            port=""
        fi
    else
        if [[ "$hostport" == *:* ]]; then
            host="${hostport%:*}"
            port="${hostport##*:}"
        else
            host="$hostport"
            port=""
        fi
    fi

    if [[ -z "$host" ]]; then
        echo "ERROR: URL missing host: $1" >&2
        return 1
    fi

    if [[ -z "$port" ]]; then
        case "$scheme" in
            http) port="80" ;;
            https) port="443" ;;
            *) port="$default_port" ;;
        esac
    fi

    if [[ ! "$port" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
        echo "ERROR: URL has invalid port: $1" >&2
        return 1
    fi

    printf '%s\t%s\t%s\n' "$host" "$port" "$url"
}

rag_bind_host_for() {
    local host="$1"
    case "$host" in
        localhost|127.*|::1)
            printf '0.0.0.0\n'
            ;;
        *)
            printf '%s\n' "$host"
            ;;
    esac
}

rag_health_url() {
    printf '%s/health\n' "${1%/}"
}

rag_shell_quote() {
    printf '%q' "$1"
}

rag_quote_command() {
    local out=""
    local arg
    for arg in "$@"; do
        out+="$(rag_shell_quote "$arg") "
    done
    printf '%s\n' "${out% }"
}

rag_gpu_prefix() {
    local override_gpu="$1"
    local inherited_gpu="${CUDA_VISIBLE_DEVICES:-}"
    local gpu_value="${override_gpu:-$inherited_gpu}"
    if [[ -n "$gpu_value" ]]; then
        printf 'CUDA_VISIBLE_DEVICES=%s ' "$(rag_shell_quote "$gpu_value")"
    fi
}

rag_validate_rerank_policy() {
    local policy="$1"
    case "$policy" in
        embedding|main_model|error)
            return 0
            ;;
        *)
            echo "ERROR: AICODE_RERANK_FALLBACK_POLICY must be embedding, main_model, or error (got: $policy)" >&2
            return 1
            ;;
    esac
}

rag_policy_description() {
    local policy="$1"
    case "$policy" in
        embedding)
            printf '保留 embedding 原始排序,不呼叫主模型\n'
            ;;
        main_model)
            printf 'fallback 到主模型排序;嚴格模式下每條符合條件的 query 都可能觸發,成本較高\n'
            ;;
        error)
            printf 'reranker 不可用時直接報錯,不靜默降級\n'
            ;;
        *)
            printf '未知 policy\n'
            ;;
    esac
}

rag_port_listener() {
    local port="$1"
    if ! command -v ss >/dev/null 2>&1; then
        return 2
    fi
    ss -H -ltnp "sport = :$port" 2>/dev/null || true
}

rag_listener_pids() {
    sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | sort -u
}

rag_health_status() {
    local base_url="$1"
    local body status
    body="$(curl -fsS --max-time 2 "$(rag_health_url "$base_url")" 2>/dev/null || true)"
    if [[ -z "$body" ]]; then
        return 1
    fi
    status="$(
        printf '%s' "$body" | python3 -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
print(str(data.get("status", "")).lower() if isinstance(data, dict) else "")
' 2>/dev/null || true
    )"
    if [[ -z "$status" ]]; then
        return 1
    fi
    printf '%s\n' "$status"
}
