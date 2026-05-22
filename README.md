# CodeTrail - OpenCode + llama.cpp 本地 MCP 工作台

CodeTrail 是一個給 OpenCode 使用的本地 MCP 後端。你在 OpenCode TUI 裡提問,模型可以透過 CodeTrail 讀專案、找程式碼、查已匯入的 spec、分析截圖或 binary、產生 patch,並在允許的白名單內跑驗證命令。

CodeTrail 目前定位是**成熟私有部署版**:適合本機、離線、NDA / firmware / private repo 分析;**不打算公開發布**成 PyPI package、Docker image 或 SaaS。安全邊界有自動測試保護,但未做公開產品級安全審計。

底層推理引擎使用 [llama.cpp](https://github.com/ggerganov/llama.cpp) `llama-server`(自己 build,需要 CUDA)。所有 LLM / embedding / reranker / VL 走它的 HTTP API。

---

## 安裝與啟動

先安裝 OpenCode、llama.cpp、Python 依賴,完整步驟見 [docs/setup.md](docs/setup.md)。最小流程:

```bash
cd <CODETRAIL_REPO>
pip install -r requirements.txt
pip install mcp pymupdf4llm
```

build llama.cpp(CUDA):

```bash
git clone https://github.com/ggerganov/llama.cpp
cd llama.cpp
cmake -B build -DGGML_CUDA=ON -DLLAMA_CURL=OFF
cmake --build build --config Release -j
# 拿到 build/bin/llama-server,丟到 PATH 或記住絕對路徑。
```

### 下載 GGUF 與啟動 llama-server

CodeTrail 不內建、也不替你預設任何「主聊天 / 程式推導模型」。你必須自己下載一顆 GGUF,下文用 `<CODE_MODEL>` 當佔位符代表它。選擇方式見 [docs/models.md](docs/models.md)。

CodeTrail 預期 4 個角色各自一個 llama-server instance:

| Port | 角色      | 必要 | 啟動旗標 |
|------|-----------|------|----------|
| 8080 | main      | 是   | `-c 65536 --jinja`(依模型) |
| 8081 | embedding | 是   | `--embedding --pooling cls` |
| 8082 | reranker  | 否   | `--reranking` |
| 8083 | VL        | 否   | `--mmproj <mmproj.gguf>` |

範例(假設你已下載好 GGUF 到 `~/models`):

```bash
# 主聊天 / 程式推導
llama-server -m ~/models/<CODE_MODEL>.gguf \
  --host 0.0.0.0 --port 8080 -c 65536 -ngl 99 --jinja \
  --cache-type-k q8_0 --cache-type-v q8_0 &

# embedding (bge-m3)
llama-server -m ~/models/bge-m3-Q4_K_M.gguf \
  --host 0.0.0.0 --port 8081 -c 8192 --embedding --pooling cls -ngl 99 &

# reranker (bge-reranker-v2-m3)
llama-server -m ~/models/bge-reranker-v2-m3-Q4_K_M.gguf \
  --host 0.0.0.0 --port 8082 -c 8192 --reranking -ngl 99 &
```

### 維護 model registry

讓 `AICODE_MODEL=qwen3-coder-32b` 這種 bare name 自動對應到 GGUF 路徑:

```bash
mkdir -p ~/.config/codetrail
cat > ~/.config/codetrail/models.json <<'EOF'
{
  "<CODE_MODEL>": "~/models/<CODE_MODEL>.gguf"
}
EOF
```

也可以跳過 registry,直接把 `AICODE_MODEL` 設成 GGUF 絕對路徑。

### 自檢

```bash
AICODE_MODEL=<CODE_MODEL> python3 scripts/doctor.py
```

接著做一個一次性 OpenCode 設定:

```bash
mkdir -p ~/.config/opencode
${EDITOR:-vi} ~/.config/opencode/opencode.json
```

把下面內容貼進 `~/.config/opencode/opencode.json`;它會設定 openai-compatible provider 指到 llama-server `/v1`、CodeTrail MCP server 和 OpenCode 權限。**把所有 `<CODE_MODEL>` 都替換成你的模型名稱**:

```json
{
  "$schema": "https://opencode.ai/config.json",

  "share": "disabled",
  "autoupdate": false,

  "model": "llamacpp/<CODE_MODEL>",
  "small_model": "llamacpp/<CODE_MODEL>",

  "provider": {
    "llamacpp": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "llama.cpp local",
      "options": {
        "baseURL": "http://localhost:8080/v1",
        "apiKey": "dummy"
      },
      "models": {
        "<CODE_MODEL>": {
          "name": "<CODE_MODEL>",
          "limit": { "context": 65536, "output": 8192 }
        }
      }
    }
  },

  "mcp": {
    "codetrail": {
      "type": "local",
      "command": [
        "bash",
        "-lc",
        "root=$(git rev-parse --show-toplevel 2>/dev/null || pwd -P); exec \"$root/.opencode/run-codetrail-mcp\""
      ],
      "enabled": true,
      "timeout": 10000
    }
  },

  "permission": {
    "*": "deny",

    "question": "allow",
    "todowrite": "allow",

    "codetrail_*": "allow",
    "codetrail_apply_patch": "ask",
    "codetrail_run_lint": "ask",
    "codetrail_run_command": "ask",
    "codetrail_import_external_file": "allow",

    "webfetch": "deny",
    "websearch": "deny",
    "bash": "deny",
    "read": "deny",
    "grep": "deny",
    "glob": "deny",
    "edit": "deny",
    "write": "deny",
    "apply_patch": "deny",
    "external_directory": "deny",
    "task": "deny",
    "skill": "deny",
    "lsp": "deny"
  }
}
```

`llamacpp` 是 provider key,你可以叫它別的名字(`local`、`llmcpp`、隨意),但要跟 `"model"` 那段的 prefix 對齊。`apiKey` llama-server 預設不檢查,填任意非空值即可。

貼完先確認 JSON 格式:

```bash
python3 -m json.tool ~/.config/opencode/opencode.json >/dev/null
```

再把 `aicode` 放進 PATH:

```bash
chmod +x ./aicode
mkdir -p "$HOME/.local/bin"
ln -sfn "$PWD/aicode" "$HOME/.local/bin/aicode"
command -v aicode
```

完成後,從要分析的專案根目錄啟動:

```bash
cd <PROJECT_TO_ANALYZE>
aicode
```

如果要讓模型讀專案外的附件,例如 `~/Downloads` 裡的 log、截圖、spec 或 firmware blob,啟動時先打開外部匯入:

```bash
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 aicode
```

預設可匯入來源是 `~/Downloads` 和 `/tmp`。其他目錄要設白名單:

```bash
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 \
AI_CODE_IMPORT_ROOTS="$HOME/Downloads:/tmp:$HOME/specs" \
aicode
```

| 變數 | 用途 |
|---|---|
| `AI_CODE_ALLOW_EXTERNAL_IMPORT=1` | 外部附件匯入總開關 |
| `AI_CODE_IMPORT_ROOTS="..."` | 外部附件來源白名單(設了就取代預設) |

進入 TUI 後就可以照下一節做基本操作。若工具看起來沒接上,再用 `/status` 檢查是否有 `codetrail Connected`;完整逐步版見 [docs/basic-usage.md](docs/basic-usage.md)。

---

## 基本操作

進入 OpenCode 後,基本工作分成三類:正常對話、夾帶附件、注入 RAG。完整逐步說明見 [docs/basic-usage.md](docs/basic-usage.md);這裡只保留最小 smoke test。

### 1. 正常對話

```text
請用工具 list_dir 看專案結構,找出可能的 entry point、測試目錄和設定檔。
```

```text
請用工具 read_file 讀 README.md 前 80 行,整理這個專案怎麼啟動。
```

### 2. 夾帶附件

```text
請先用工具 import_external_file 匯入 ~/Downloads/error.log,
再用 read_file 讀回傳的新路徑,找出最重要的錯誤訊息。
```

如果檔案已經在專案內,直接要求 `read_file` 或 `analyze_file` 即可。

### 3. 注入 RAG

```text
請用工具 ingest_document 匯入 docs/spec.pdf,
完成後 reload_knowledge_base,回報目前載入幾個 chunks。
```

```text
請用工具 query_knowledge_strict 查這份 spec 裡最重要的限制或預設值,
證據不足就拒答,回答要附 REF。
```

完整 RAG 用法見 [docs/rag.md](docs/rag.md)。

---

## 模型設定與用途

CodeTrail 不替你決定主聊天 / 程式推導模型。請依硬體與任務自己挑一顆 GGUF。完整選擇邏輯與 context 取捨見 [docs/models.md](docs/models.md)。

| 模型 / 角色 | 用途 | 對應 server port |
|---|---|---|
| `<CODE_MODEL>` (主) | 主聊天 / 程式推導 | 8080 |
| `bge-m3` (embedding) | RAG / Code-RAG 算 embedding | 8081 |
| `bge-reranker-v2-m3` | RAG rerank cross-encoder | 8082 |
| VL (qwen3-vl / llava / ...) | 截圖、UI error、圖片進 KB | 8083 |

主模型解析優先順序(找不到、是 placeholder、或被指到外部 provider 時 `aicode` 會 fail-loud,**不會** fallback 任何內建預設):

1. `AICODE_MODEL` 環境變數。
2. `aicode -m <MODEL>` / `--model <MODEL>` CLI 旗標。
3. `~/.config/opencode/opencode.json` 的 `"model"` 欄位(`<provider>/<MODEL>` 形式)。

`<MODEL>` 可以是 MODEL_REGISTRY 裡登記的 bare name 或 GGUF 絕對路徑。**不接受** `ollama/...`、`openai/...`、`anthropic/...` 這類外部 provider prefix。

換模型時不要只在 TUI 裡 `/models` 切換。正確方式是停掉舊的 llama-server、用新 GGUF 重啟、退出 `aicode`、用 `AICODE_MODEL=<新模型>` 重新啟動。

啟動 `aicode` 時會自動讀主 llama-server `/props`,確認你要求的 `AICODE_DYNAMIC_NUM_CTX_MAX` 不超過 server `-c <N>` 的真實 ctx:

```
[ctx-safety] UNSAFE: model=<CODE_MODEL> requested_ctx=65536
        requested ctx=65536 超過 llama-server 啟動時的 -c 8192 ...
        建議任一處理:
          (a) export AICODE_DYNAMIC_NUM_CTX_MAX=8192    ← 對齊 server n_ctx
          (b) 重啟 llama-server 並提高 `-c 65536`        ← server 端拉大
          (c) export AICODE_ACCEPT_CTX_RISK=1            ← 我知道會 truncate,照樣跑
          (d) export AICODE_CTX_SAFETY_DISABLE=1         ← 永久關掉這個檢查
```

server 沒啟動 / 不可連時會印 `[ctx-safety] UNKNOWN` 並放行,不會擋啟動。

如果你把模型固定寫進 `~/.bashrc`,也建議一起固定 dynamic cap:

```bash
export AICODE_MODEL=<CODE_MODEL>
export AICODE_DYNAMIC_NUM_CTX_MAX=32768
```

---

## 換顯卡 / 換模型

換到其他機器時:在那台主機 build llama.cpp、下載 GGUF、啟動 4 個 server、加進 registry、設 `AICODE_MODEL`,然後 `aicode`。

```bash
# 假設新機器已 build 好 llama.cpp 並下載完 GGUF
llama-server -m ~/models/<CODE_MODEL>.gguf --host 0.0.0.0 --port 8080 -c 65536 -ngl 99 &
# ... 啟動 8081 / 8082 / 8083 (見 docs/setup.md)

AICODE_MODEL=<CODE_MODEL> \
AICODE_DYNAMIC_NUM_CTX_MAX=32768 \
aicode
```

硬體起點(請把 `<CODE_MODEL>` 換成實際下載的 GGUF 名稱):

| 硬體 | 候選 |
|---|---|
| 5090 32GB | 30B-32B Q4_K_M @ `AICODE_DYNAMIC_NUM_CTX_MAX=65536`;穩定後可試 128K |
| 24GB VRAM | 30B Q4 / 20B Q5;`AICODE_DYNAMIC_NUM_CTX_MAX=32768` |
| 16GB 以下 | 14B Q4 / 20B Q4 或更小,不要硬開大 context |

VRAM 不夠 / OOM 時的調整順序(由小痛到大痛):

1. 降低 `AICODE_DYNAMIC_NUM_CTX_MAX`。
2. 重啟 server 用更小的 `-c <N>`。
3. server 啟動加 `--cache-type-k q8_0 --cache-type-v q8_0`(KV cache 量化)。
4. 降到 Q4 量化 GGUF。
5. 換更小的模型。

如果 llama-server 跑在另一台 GPU 主機上:

```bash
AICODE_LLAMA_BASE_URL=http://<GPU_HOST>:8080 \
AICODE_LLAMA_EMBED_BASE_URL=http://<GPU_HOST>:8081 \
AICODE_LLAMA_RERANK_BASE_URL=http://<GPU_HOST>:8082 \
AICODE_LLAMA_VL_BASE_URL=http://<GPU_HOST>:8083 \
AICODE_MODEL=<CODE_MODEL> \
AICODE_DYNAMIC_NUM_CTX_MAX=32768 \
aicode
```

同時把 `~/.config/opencode/opencode.json` 的 provider `baseURL` 改成 `http://<GPU_HOST>:8080/v1`。遠端 server 會收到 prompt、程式碼片段和 spec 摘要,只能指向可信內網 / VPN 主機(llama-server 預設不檢查 API key)。

完整模型、context 說明見 [docs/models.md](docs/models.md)。

---

## 必守安全界線

- `AICODE_ROOT` 是本次 OpenCode 可讀寫的 sandbox 根目錄;不要從 `$HOME` 或 `/` 啟動。
- MCP server 啟動時會拒絕危險 root,並把工具限制在 `AICODE_ROOT` 內。
- `knowledge.json`、`knowledge_emb.npz`、`data/`、`.codetrail/`、`*.jsonl` 和 `.code_rag_cache_*` 通常含 NDA 片段,不要 commit。
- `apply_patch(...)` 有 context matching、max files、max lines 限制;不要放寬安全層。要完全關閉改檔,啟動時設 `AI_CODE_PATCH=0`。
- `run_command(...)` 只允許白名單命令,不支援 shell metacharacter。預設白名單只含測試 / lint;`make` / `cmake` / `ninja` / `meson` / `bazel build` 等 build 命令需要顯式 `AI_CODE_ENABLE_BUILD_COMMANDS=1` 才會掛上。要完全關閉命令執行,設 `AI_CODE_RUN_TESTS=0`。
- 遠端 llama-server 會收到 prompt、程式碼片段、spec 摘要與工具輸出,只能指向可信內網 / VPN 主機。

完整安全說明見 [docs/security.md](docs/security.md)。

---

## 文件地圖

| 文件 | 內容 |
|---|---|
| [docs/setup.md](docs/setup.md) | 安裝、build llama.cpp、啟動 4 個 server、OpenCode config、`aicode` |
| [docs/basic-usage.md](docs/basic-usage.md) | 基本操作:正常對話、夾帶附件、RAG 注入、最小驗收流程 |
| [docs/models.md](docs/models.md) | 模型設定、硬體取捨、遠端 server、ctx 對齊 |
| [docs/rag.md](docs/rag.md) | 讀檔、匯入附件、建立知識庫、Code-RAG、查 spec |
| [docs/mcp-tools.md](docs/mcp-tools.md) | CodeTrail 暴露的 17 個 MCP 工具與使用原則 |
| [docs/security.md](docs/security.md) | sandbox、patch、run command、NDA 資料、工作節奏 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | `/status`、ctx-safety、server 不可連、查 spec、patch / command 被拒絕 |
| [README_DEV.md](README_DEV.md) | 開發者維護命令、測試、eval、context gate 設計 |
| [AGENTS.md](AGENTS.md) | AI coding agent 修改本 repo 時必讀的安全規範 |

---

## License

本專案以 MIT 授權釋出,程式碼以「現狀」(AS IS)提供,不附帶任何明示或默示的保證,
包括但不限於可商用性、特定用途適用性、不侵權、資安、隱私、合規、或 NDA 適用性。
完整法律文字見 [LICENSE](LICENSE);補充免責說明見 [DISCLAIMER.md](DISCLAIMER.md)。

This project is licensed under the MIT License. See [LICENSE](./LICENSE).

## Responsible use

This project is provided for lawful software development, research, education,
and code reasoning workflows.

Users are solely responsible for how they use, modify, deploy, combine, or
redistribute this software, including compliance with applicable laws,
contracts, licenses, NDAs, platform terms, model-provider terms, and third-party
rights.

The authors do not guarantee that any particular workflow is legally compliant,
NDA-compliant, secure, private, or suitable for a specific use case.

The software is provided "as is", without warranty of any kind. The authors do
not encourage, endorse, or provide support for unlawful use.

See [DISCLAIMER.md](./DISCLAIMER.md) for the full disclaimer.
