#!/usr/bin/env bash
# 啟動 CodeTrail RAG 兩顆固定參數 server(embedding :8081 + reranker :8082)。
#
# 兩顆模型體積小、啟動秒開、參數不需要依硬體調整,合併在一個 tmux session
# (`codetrail-rag`)的兩個 window 內,使用者只需要管理一個 session。
#
# 用法:
#   ./scripts/start-rag-servers.sh
#
# 環境變數(可選):
#   MODELS_DIR   GGUF 模型根目錄              預設 ~/models
#   LLAMA_BIN    llama-server 執行檔路徑      預設 ~/llama.cpp/build/bin/llama-server
#   SESSION      tmux session 名稱           預設 codetrail-rag
#
# 啟動後操作:
#   tmux a -t codetrail-rag        接回去看 log
#   Ctrl-b n                       切換 embed / rerank window
#   Ctrl-b d                       退出 tmux,server 留在背景
#   tmux kill-session -t codetrail-rag   一次關掉兩顆 server

set -euo pipefail

MODELS_DIR="${MODELS_DIR:-$HOME/models}"
LLAMA_BIN="${LLAMA_BIN:-$HOME/llama.cpp/build/bin/llama-server}"
SESSION="${SESSION:-codetrail-rag}"

EMBED_MODEL="$MODELS_DIR/bge-m3/bge-m3-f16.gguf"
RERANK_MODEL="$MODELS_DIR/bge-reranker-v2-m3/bge-reranker-v2-m3-Q4_K_M.gguf"

# --- 前置檢查 ---------------------------------------------------------------

if ! command -v tmux >/dev/null 2>&1; then
    echo "ERROR: 找不到 tmux,請先 sudo apt install tmux" >&2
    exit 1
fi

if [[ ! -x "$LLAMA_BIN" ]]; then
    echo "ERROR: llama-server 不存在或不可執行: $LLAMA_BIN" >&2
    echo "       依 README §1.5 build llama.cpp,或設定 LLAMA_BIN=..." >&2
    exit 1
fi

if [[ ! -f "$EMBED_MODEL" ]]; then
    echo "ERROR: 找不到 embedding 模型: $EMBED_MODEL" >&2
    echo "       依 README §2.3 下載 bge-m3" >&2
    exit 1
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "ERROR: tmux session '$SESSION' 已存在。要重啟先砍掉:" >&2
    echo "         tmux kill-session -t $SESSION" >&2
    exit 1
fi

# --- 啟動 embedding (window 0) ---------------------------------------------

tmux new-session -d -s "$SESSION" -n embed
tmux send-keys -t "$SESSION:embed" \
    "'$LLAMA_BIN' -m '$EMBED_MODEL' --host 0.0.0.0 --port 8081 -c 8192 --embedding --pooling cls -ngl 99" Enter

echo "[+] 啟動 embedding server (:8081) 於 tmux $SESSION:embed"

# --- 啟動 reranker (window 1) ----------------------------------------------
# 沒下載 reranker 模型也不擋,RAG 會 fallback 到主模型排序。

if [[ -f "$RERANK_MODEL" ]]; then
    tmux new-window -t "$SESSION" -n rerank
    tmux send-keys -t "$SESSION:rerank" \
        "'$LLAMA_BIN' -m '$RERANK_MODEL' --host 0.0.0.0 --port 8082 -c 8192 --embedding --pooling rank --reranking -ngl 99" Enter
    echo "[+] 啟動 reranker server  (:8082) 於 tmux $SESSION:rerank"
else
    echo "[!] 找不到 reranker 模型 ($RERANK_MODEL),跳過。doctor 會列為 WARN 但不擋啟動。"
fi

# --- 收尾提示 ---------------------------------------------------------------

cat <<EOF

兩顆 server 大約 5–10 秒後 listening。驗證:
  curl -s -o /dev/null -w 'embed  :8081 -> %{http_code}\n' http://localhost:8081/health
  curl -s -o /dev/null -w 'rerank :8082 -> %{http_code}\n' http://localhost:8082/health

一次關掉兩顆:
  ./scripts/stop-rag-servers.sh

偵錯時看 log(平常不用):
  tmux a -t $SESSION         (Ctrl-b n 切 window,Ctrl-b d 退出)
EOF
