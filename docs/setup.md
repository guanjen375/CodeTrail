# 安裝、設定與啟動

這份文件保留完整的 OpenCode + llama.cpp + CodeTrail MCP 安裝與啟動細節。

[回到 README](../README.md)。

---

## 安裝

以下用 `python` 表示 Python 3。如果你的系統只有 `python3`，把指令中的 `python` 改成 `python3`。

### 準備軟體

需要:

- Python 3.10+
- Node.js LTS + npm
- llama.cpp(自己 build,需要 CUDA toolchain 才能用 GPU;RTX 5090 走 CUDA 12.x)
- git
- ripgrep `rg`,建議安裝,搜尋會快很多

安裝 OpenCode:

```bash
npm install -g opencode-ai
```

build llama.cpp(CUDA):

```bash
git clone https://github.com/ggerganov/llama.cpp
cd llama.cpp
cmake -B build -DGGML_CUDA=ON -DLLAMA_CURL=OFF
cmake --build build --config Release -j
# 拿到 build/bin/llama-server,可以丟到 PATH 或記住絕對路徑。
```

### 安裝 CodeTrail Python 依賴

```bash
cd <CODETRAIL_REPO>
pip install -r requirements.txt
pip install mcp pymupdf4llm
```

`<CODETRAIL_REPO>` 是這個 CodeTrail repo 的路徑,不是你要分析的 firmware repo。

### 下載模型 (GGUF)

CodeTrail 不內建主聊天 / 程式推導模型 — 你必須自己挑一顆 GGUF,下面用 `<CODE_MODEL>` 當佔位符代表它。**`<CODE_MODEL>` 不是真實 tag**,請替換成實際模型名稱(選擇方式見 [模型設定與硬體取捨](models.md))。

從 huggingface 下載 GGUF 範例:

```bash
mkdir -p ~/models

# 主聊天 / 程式推導 (例如 qwen3-coder 30B Q4_K_M)
huggingface-cli download Qwen/Qwen2.5-Coder-32B-Instruct-GGUF \
  qwen2.5-coder-32b-instruct-q4_k_m.gguf --local-dir ~/models

# embedding (bge-m3)
huggingface-cli download CompendiumLabs/bge-m3-gguf \
  bge-m3-Q4_K_M.gguf --local-dir ~/models

# reranker (bge-reranker-v2-m3)
huggingface-cli download gpustack/bge-reranker-v2-m3-GGUF \
  bge-reranker-v2-m3-Q4_K_M.gguf --local-dir ~/models

# (選用) VL 多模態
huggingface-cli download <一個你選的 VL repo> ... --local-dir ~/models
# 注意:VL 模型需要 `.gguf` 主檔 + `mmproj-*.gguf` projection 檔
```

### 建立 model registry

讓 `AICODE_MODEL=qwen3-coder-32b` 這種 bare name 自動對到 GGUF 路徑:

```bash
mkdir -p ~/.config/codetrail
cat > ~/.config/codetrail/models.json <<'EOF'
{
  "qwen3-coder-32b": "~/models/qwen2.5-coder-32b-instruct-q4_k_m.gguf",
  "bge-m3": "~/models/bge-m3-Q4_K_M.gguf",
  "bge-reranker-v2-m3": "~/models/bge-reranker-v2-m3-Q4_K_M.gguf"
}
EOF
```

也可以跳過 registry,直接用絕對路徑當 `AICODE_MODEL`,例如 `AICODE_MODEL=/home/you/models/foo.gguf`。

### 啟動 4 個 llama-server

CodeTrail 預期 4 個角色各自一個 server(可省略選用的兩個):

| Port | 角色      | 啟動旗標(關鍵)                          | 必要 |
|------|-----------|----------------------------------------|------|
| 8080 | main      | `-c 65536 --jinja` (依模型)            | 是   |
| 8081 | embedding | `--embedding --pooling cls`            | 是   |
| 8082 | reranker  | `--reranking`                          | 否(USE_RERANKER=False 時可省) |
| 8083 | VL        | `--mmproj <mmproj.gguf>`               | 否(只在分析圖片時用) |

範例:

```bash
# 主聊天 (32B 模型在 5090 32GB 上 Q4_K_M + 64K ctx 還算 fits)
llama-server -m ~/models/qwen2.5-coder-32b-instruct-q4_k_m.gguf \
  --host 0.0.0.0 --port 8080 \
  -c 65536 \
  -ngl 99 \
  --jinja \
  --cache-type-k q8_0 --cache-type-v q8_0

# embedding
llama-server -m ~/models/bge-m3-Q4_K_M.gguf \
  --host 0.0.0.0 --port 8081 \
  -c 8192 \
  --embedding --pooling cls \
  -ngl 99

# reranker
llama-server -m ~/models/bge-reranker-v2-m3-Q4_K_M.gguf \
  --host 0.0.0.0 --port 8082 \
  -c 8192 \
  --reranking \
  -ngl 99
```

建議用 systemd unit / tmux / supervisord 管理這 4 個 process。實測 5090 上 main(qwen 32B Q4_K_M @ 64K) + embedding(bge-m3) + reranker(bge-reranker) 三顆同時跑,VRAM 約佔 25-28GB,還有空間給 VL。

### 自檢

```bash
AICODE_MODEL=<CODE_MODEL> python scripts/doctor.py
```

如果只想檢查本地檔案與設定,不連 llama-server:

```bash
AICODE_MODEL=<CODE_MODEL> python scripts/doctor.py --no-network
```

`PASS` 可以先略過;`FAIL` 要處理。常見問題是 OpenCode 不在 PATH、4 個 server 沒啟動、GGUF 路徑不對、`aicode` 沒有執行權。

---

## 設定 OpenCode

### 建立 OpenCode config

```bash
mkdir -p ~/.config/opencode
${EDITOR:-vi} ~/.config/opencode/opencode.json
```

llama-server 提供 OpenAI 相容 `/v1`,所以 OpenCode 用 openai-compatible provider 即可。下面是最小範例:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "llamacpp": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "llama.cpp local",
      "options": {
        "baseURL": "http://localhost:8080/v1",
        "apiKey": "dummy"
      },
      "models": {
        "qwen3-coder-32b": {
          "limit": { "context": 65536, "output": 8192 }
        }
      }
    }
  },
  "model": "llamacpp/qwen3-coder-32b",
  "mcp": {
    "codetrail": {
      "type": "local",
      "command": [".opencode/run-codetrail-mcp"]
    }
  }
}
```

說明:

- `llamacpp` 是 provider key,你可以叫它別的名字(`local`、`llmcpp`、隨意),但要跟下面 `"model"` 那段的 prefix 對齊。
- `baseURL` 指到主 llama-server 的 `/v1`。遠端的話改 IP 即可。
- `apiKey` 可以填任意非空值(llama-server 預設不檢查 key)。
- `limit.context` 對應啟動時的 `-c <N>`,兩邊建議對齊,不然 OpenCode 可能還沒撐到 server 上限就先 truncate。
- `model` 欄位帶 provider prefix(`llamacpp/`),CodeTrail wrapper 會 strip 它取得 bare name。

檢查 JSON 格式:

```bash
python -m json.tool ~/.config/opencode/opencode.json >/dev/null
```

### 安裝 `aicode` 啟動指令

在 CodeTrail repo 根目錄執行。這段可以重跑;如果你剛換 repo,既有的舊 symlink 會被更新到目前這份 checkout:

```bash
chmod +x ./aicode
mkdir -p "$HOME/.local/bin"
ln -sfn "$PWD/aicode" "$HOME/.local/bin/aicode"
```

確認 shell 找得到它:

```bash
command -v aicode
```

如果沒有輸出,通常是 `$HOME/.local/bin` 還不在目前 shell 的 `PATH`。先讓這個 shell 生效:

```bash
export PATH="$HOME/.local/bin:$PATH"
command -v aicode
```

如果這樣有輸出,再把同一行 `export PATH="$HOME/.local/bin:$PATH"` 加到 `~/.bashrc` 或你的 shell 設定檔,之後新開終端機就會生效。

`aicode` 會做六件事:

- 將目前目錄設成 `AICODE_ROOT`
- 拒絕 `AICODE_ROOT=/` 和 `AICODE_ROOT=$HOME`
- 在目前專案準備 `.opencode/run-codetrail-mcp`,讓 OpenCode config 裡的 MCP command 能啟動 CodeTrail server
- 啟動前跑 `scripts/ctx_safety_check.py` 讀 llama-server `/props` 拿真實 `n_ctx`,與 `AICODE_DYNAMIC_NUM_CTX_MAX` 比對;requested > server 就直接 `exit 2` 拒絕啟動並提示對齊辦法(server 沒啟動 / 不可連時 graceful 放行,只 warn)
- 啟動 `opencode`,讓 OpenCode 子行程繼承同一個沙箱根目錄
- 把使用者傳入的 `-m / --model` 原樣轉發給 OpenCode;沒傳就讓 OpenCode 自己讀 opencode.json `"model"` 欄位

---

## 啟動專案

日常建議優先使用 **OpenCode TUI + CodeTrail MCP**。這條路徑最完整:模型可以透過工具讀檔、搜尋程式碼、查 RAG、分析圖片/binary、必要時產生 patch,互動紀錄與工具結果也比較容易追蹤。

切到要分析或修改的專案,再啟動 OpenCode:

```bash
cd <PROJECT_TO_ANALYZE>
aicode
```

進入後可以直接問「請分析這個專案的整體架構」、「這個錯誤可能在哪裡」、「請查 RAG 裡某個規格限制」這類問題。涉及專案內容的問題,優先讓模型用工具讀實際檔案,不要只靠一般經驗回答。

如果要讀專案外的 log、截圖、spec 或 firmware blob,啟動參數見 [README 的安裝與啟動](../README.md#安裝與啟動);完整附件流程見 [RAG、附件與知識庫操作](rag.md)。

若工具看起來沒接上,再用 `/status` 檢查是否有 `codetrail Connected`(或你在 `opencode.json` 裡設定的 MCP key)。若啟動 root 不如預期,再看啟動畫面的 `[aicode] AICODE_ROOT=...`。

啟動時帶 `AICODE_MODEL`:

```bash
AICODE_MODEL=<CODE_MODEL> aicode
```

主模型解析優先順序:`AICODE_MODEL` env > `aicode -m / --model` CLI 旗標 > `OPENCODE_CONFIG` / `~/.config/opencode/opencode.json` 的 `"model"` 欄位。三個都沒有、或值是 `<CODE_MODEL>` 之類的 placeholder 時,`aicode` 會 fail-loud,不會 fallback 任何內建主模型。

換模型、context、遠端 server 的細節集中在 [模型設定與硬體取捨](models.md)。不要只在 TUI 裡用 `/models` 切換;那只會換 OpenCode 前台對話模型,不會 reload server,也不會同步 CodeTrail MCP server 的內部呼叫。

RAG 流程不在這裡重複;可先照 README 的 smoke test 跑一輪,完整說明見 [RAG、附件與知識庫操作](rag.md)。

---
