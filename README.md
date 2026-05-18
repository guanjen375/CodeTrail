# CodeTrail - OpenCode + Ollama 本地 MCP 工作台

CodeTrail 是一個給 OpenCode 使用的本地 MCP 後端。你在 OpenCode TUI 裡提問，模型可以透過 CodeTrail 讀專案、找程式碼、查已匯入的 spec、分析截圖或 binary、產生 patch，並在允許的白名單內跑驗證命令。

CodeTrail 目前定位是**成熟私有部署版**：適合本機、離線、NDA / firmware / private repo 分析；**不打算公開發布**成 PyPI package、Docker image 或 SaaS。安全邊界有自動測試保護，但未做公開產品級安全審計。

---

## 安裝與啟動

先安裝 OpenCode、Ollama、Python 依賴，完整步驟見 [docs/setup.md](docs/setup.md)。最小流程是：

```bash
cd <CODETRAIL_REPO>
pip install -r requirements.txt
pip install mcp pymupdf4llm ollama

ollama pull qwen3-coder:30b
ollama pull bge-m3
ollama pull qllama/bge-reranker-v2-m3

python scripts/doctor.py
```

接著做兩個一次性設定：

```bash
mkdir -p ~/.config/opencode
${EDITOR:-vi} ~/.config/opencode/opencode.json
```

把下面內容貼進 `~/.config/opencode/opencode.json`；它會設定 Ollama provider、CodeTrail MCP server 和 OpenCode 權限：

```json
{
  "$schema": "https://opencode.ai/config.json",

  "share": "disabled",
  "autoupdate": false,

  "enabled_providers": ["ollama"],
  "model": "ollama/qwen3-coder:30b",
  "small_model": "ollama/qwen3-coder:30b",

  "provider": {
    "ollama": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Ollama",
      "options": {
        "baseURL": "http://localhost:11434/v1"
      },
      "models": {
        "qwen3-coder:30b": {
          "name": "Qwen3 Coder 30B"
        },
        "qwen3.6:35b-a3b-q4_K_M": {
          "name": "Qwen3.6 35B A3B Q4_K_M"
        },
        "devstral:24b": {
          "name": "Devstral 24B"
        },
        "gpt-oss:20b": {
          "name": "GPT-OSS 20B"
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

貼完先確認 JSON 格式：

```bash
python3 -m json.tool ~/.config/opencode/opencode.json >/dev/null
```

再把 `aicode` 放進 PATH：

```bash
chmod +x ./aicode
mkdir -p "$HOME/.local/bin"
ln -sfn "$PWD/aicode" "$HOME/.local/bin/aicode"
command -v aicode
```

完成後，從要分析的專案根目錄啟動：

```bash
cd <PROJECT_TO_ANALYZE>
aicode
```

如果要讓模型讀專案外的附件，例如 `~/Downloads` 裡的 log、截圖、spec 或 firmware blob，啟動時先打開外部匯入：

```bash
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 aicode
```

注意：上面的 `opencode.json` 裡 `"codetrail_import_external_file": "allow"` 只是允許 OpenCode 呼叫這個工具；真正讓 CodeTrail 後端接受專案外檔案，仍要在啟動時設定 `AI_CODE_ALLOW_EXTERNAL_IMPORT=1`。

預設可匯入來源是 `~/Downloads` 和 `/tmp`。如果附件放在其他目錄，用 `AI_CODE_IMPORT_ROOTS` 指定白名單；多個目錄用冒號分隔：

```bash
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 \
AI_CODE_IMPORT_ROOTS="$HOME/Downloads:/tmp:$HOME/specs" \
aicode
```

兩個變數分工：

| 變數 | 用途 |
|---|---|
| `AI_CODE_ALLOW_EXTERNAL_IMPORT=1` | 外部附件匯入總開關；沒開時不能讀專案外檔案 |
| `AI_CODE_IMPORT_ROOTS="$HOME/Downloads:/tmp:$HOME/specs"` | 外部附件來源白名單；一旦設定就會取代預設清單 |

進入 TUI 後就可以照下一節做基本操作。若工具看起來沒接上，再用 `/status` 檢查是否有 `codetrail Connected`；完整逐步版見 [docs/basic-usage.md](docs/basic-usage.md)。

---

## 基本操作

進入 OpenCode 後，基本工作分成三類：正常對話、夾帶附件、注入 RAG。完整逐步說明見 [docs/basic-usage.md](docs/basic-usage.md)；這裡只保留最小 smoke test。下面的路徑請換成你專案裡真的存在的檔案；如果附件在專案外，啟動時先開 `AI_CODE_ALLOW_EXTERNAL_IMPORT=1`。

### 1. 正常對話

```text
請用工具 list_dir 看專案結構，找出可能的 entry point、測試目錄和設定檔。
```

```text
請用工具 read_file 讀 README.md 前 80 行，整理這個專案怎麼啟動。
```

### 2. 夾帶附件

```text
請先用工具 import_external_file 匯入 ~/Downloads/error.log，
再用 read_file 讀回傳的新路徑，找出最重要的錯誤訊息。
```

如果檔案已經在專案內，直接要求 `read_file` 或 `analyze_file` 即可。完整附件規則見 [docs/rag.md](docs/rag.md#在對話裡讓模型看到一個檔案)。

### 3. 注入 RAG

```text
請用工具 ingest_document 匯入 docs/spec.pdf，
完成後 reload_knowledge_base，回報目前載入幾個 chunks。
```

```text
請用工具 query_knowledge_strict 查這份 spec 裡最重要的限制或預設值，
證據不足就拒答，回答要附 REF。
```

`ingest_document` 會把文件切 chunk 寫進專案的 `knowledge.json`；`reload_knowledge_base` 後，才能用 `query_knowledge` / `query_knowledge_strict` 查。完整 RAG 用法見 [docs/rag.md](docs/rag.md#把附件做成知識庫讓模型隨時能查)。

---

## 常用模型

| 模型 | 用途 | 建議 |
|---|---|---|
| `qwen3-coder:30b` | 日常讀 repo、改 code、產 patch | 預設主力，穩定優先 |
| `qwen3.6:35b-a3b-q4_K_M` | 跨檔推理、規格 vs 實作比對 | 32GB VRAM 先用 `AICODE_DYNAMIC_NUM_CTX_MAX=32768` |
| `devstral:24b` | 快速 review、簡單 patch | 快，但工具鏈穩定度要再確認 |
| `gpt-oss:20b` | 快速理解、摘要、初步定位 | 適合探索，不適合作為最終 patch 主力 |
| `bge-m3` | RAG / Code-RAG embedding | 必要模型，不要當聊天模型選 |
| `qllama/bge-reranker-v2-m3` | RAG rerank | 建議安裝，查 spec 排序更穩 |
| `qwen3-vl:30b-a3b` | 截圖、UI error、圖片進 KB | 需要分析圖片時再 pull |

換模型時不要只在 TUI 裡 `/models` 切換。正確方式是退出 `aicode`，用 `AICODE_MODEL=<MODEL>` 重新啟動，讓 OpenCode TUI 與 CodeTrail MCP server 內部呼叫一致。

---

## 換顯卡 / 換模型

先在 Ollama 下載模型，再用同一個 `AICODE_MODEL` 啟動：

```bash
ollama pull <MODEL>

AICODE_MODEL=<MODEL> \
AICODE_DYNAMIC_NUM_CTX_MAX=32768 \
aicode
```

常見起點：

| 硬體 | 建議 |
|---|---|
| 32GB VRAM | `qwen3.6:35b-a3b-q4_K_M` 先配 `AICODE_DYNAMIC_NUM_CTX_MAX=32768`，穩定後再試 `65536` |
| 24GB VRAM | 優先 `qwen3-coder:30b` / `devstral:24b`，35B 要先降 context |
| 16GB 以下 | 用 `gpt-oss:20b` 或更小模型，避免大模型加大 context |

啟動後用另一個終端機看實際載入狀態：

```bash
ollama ps
```

- `100% GPU`：速度正常。
- `xx% CPU / xx% GPU`：VRAM 不夠，會明顯變慢；先把 `AICODE_DYNAMIC_NUM_CTX_MAX` 降到 `32768` 或 `16384`。
- `[CTX_OVERFLOW]`：正確性問題，代表 prompt 太大；拆小任務或縮小工具讀取範圍。

如果 Ollama 跑在另一台 GPU 主機上：

```bash
AICODE_OLLAMA_BASE_URL=http://<GPU_HOST>:11434 \
AICODE_MODEL=<MODEL> \
AICODE_DYNAMIC_NUM_CTX_MAX=32768 \
aicode
```

同時把 `~/.config/opencode/opencode.json` 的 Ollama `baseURL` 改成 `http://<GPU_HOST>:11434/v1`。遠端 Ollama 會收到 prompt、程式碼片段和 spec 摘要，只能指向可信內網 / VPN 主機。

完整模型、context、offload 說明見 [docs/models.md](docs/models.md)。

---

## 必守安全界線

- `AICODE_ROOT` 是本次 OpenCode 可讀寫的 sandbox 根目錄；不要從 `$HOME` 或 `/` 啟動。
- MCP server 啟動時會拒絕危險 root，並把工具限制在 `AICODE_ROOT` 內。
- `knowledge.json`、`knowledge_emb.npz`、`data/`、`.codetrail/`、`*.jsonl` 和 `.code_rag_cache_*` 通常含 NDA 片段，不要 commit。
- `apply_patch(...)` 有 context matching、max files、max lines 限制；不要放寬安全層。要完全關閉改檔，啟動時設 `AI_CODE_PATCH=0`。
- `run_command(...)` 只允許白名單命令，不支援 shell metacharacter。預設白名單只含測試 / lint；`make` / `cmake` / `ninja` / `meson` / `bazel build` 等 build 命令需要顯式 `AI_CODE_ENABLE_BUILD_COMMANDS=1` 才會掛上，避免在陌生 repo 上一鍵跑專案 build script。要完全關閉命令執行，設 `AI_CODE_RUN_TESTS=0`。
- 遠端 Ollama 會收到 prompt、程式碼片段、spec 摘要與工具輸出，只能指向可信內網 / VPN 主機。

完整安全說明見 [docs/security.md](docs/security.md)。

---

## 文件地圖

| 文件 | 內容 |
|---|---|
| [docs/setup.md](docs/setup.md) | 安裝、OpenCode config、`aicode`、啟動 |
| [docs/basic-usage.md](docs/basic-usage.md) | 基本操作：正常對話、夾帶附件、RAG 注入、最小驗收流程 |
| [docs/models.md](docs/models.md) | 模型比較、顯卡建議、遠端 Ollama、context / offload 後果 |
| [docs/rag.md](docs/rag.md) | 讀檔、匯入附件、建立知識庫、Code-RAG、查 spec |
| [docs/mcp-tools.md](docs/mcp-tools.md) | CodeTrail 暴露的 17 個 MCP 工具與使用原則 |
| [docs/security.md](docs/security.md) | sandbox、patch、run command、NDA 資料、工作節奏 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | `/status`、模型 404、查 spec、patch / command 被拒絕 |
| [README_DEV.md](README_DEV.md) | 開發者維護命令、測試、eval、context gate 設計 |
| [AGENTS.md](AGENTS.md) | AI coding agent 修改本 repo 時必讀的安全規範 |

---

## License

本專案以 MIT 授權釋出，程式碼以「現狀」（AS IS）提供，不附帶任何明示或默示的保證，
包括但不限於可商用性、特定用途適用性、不侵權、資安、隱私、合規、或 NDA 適用性。
完整法律文字見 [LICENSE](LICENSE)；補充免責說明見 [DISCLAIMER.md](DISCLAIMER.md)。

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
