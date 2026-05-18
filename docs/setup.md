# 安裝、設定與啟動

這份文件保留完整的 OpenCode + Ollama + CodeTrail MCP 安裝與啟動細節。

[回到 README](../README.md)。

---

## 安裝

以下用 `python` 表示 Python 3。如果你的系統只有 `python3`，把指令中的 `python` 改成 `python3`。

### 準備軟體

需要：

- Python 3.10+
- Node.js LTS + npm
- Ollama
- git
- ripgrep `rg`，建議安裝，搜尋會快很多

安裝 OpenCode：

```bash
npm install -g opencode-ai
```

安裝 Ollama 後確認服務可用：

```bash
ollama list
```

### 安裝 CodeTrail Python 依賴

```bash
cd <CODETRAIL_REPO>
pip install -r requirements.txt
pip install mcp pymupdf4llm ollama
```

`<CODETRAIL_REPO>` 是這個 CodeTrail repo 的路徑，不是你要分析的 firmware repo。

### 下載模型

先下載預設主模型與 RAG 必要模型：

```bash
ollama pull qwen3-coder:30b
ollama pull bge-m3
ollama pull qllama/bge-reranker-v2-m3
```

建議也把 OpenCode 設定檔列出的候選模型下載好，之後可以直接在 TUI 裡切換：

```bash
ollama pull qwen3.6:35b-a3b-q4_K_M
ollama pull devstral:24b
ollama pull gpt-oss:20b
```

這幾個名字後面的「:」加上一串符號代表模型不同的壓縮格式（影響大小、速度、品質）。同一個模型可以有多個版本，Ollama 上挑哪個版本就用對應的名字 pull。

`qwen3.6:35b-a3b-q4_K_M` 是 Linux 上能用的版本。另一個看起來很像的名字 `qwen3.6:35b-a3b-coding-nvfp4` 是 macOS 限定，Linux 下載會直接報錯 `412: this model requires macOS`，不要用這個。

如果會讓 OpenCode 分析截圖、UI error 或圖片，另外下載視覺模型：

```bash
ollama pull qwen3-vl:30b-a3b
```

模型怎麼選，見 [模型與硬體建議](models.md)。

### 自檢

```bash
python scripts/doctor.py
```

如果只想檢查本地檔案與設定，不連 Ollama：

```bash
python scripts/doctor.py --no-network
```

`PASS` 可以先略過；`FAIL` 要處理。常見問題是 OpenCode 不在 PATH、Ollama 沒啟動、模型還沒 pull、`aicode` 沒有執行權。

---


## 設定 OpenCode

### 建立 OpenCode config

```bash
mkdir -p ~/.config/opencode
${EDITOR:-vi} ~/.config/opencode/opencode.json
```

在檔案裡貼上下方內容。MCP command 會從 OpenCode 啟動目錄找 git root，然後執行該 root 內的 `.opencode/run-codetrail-mcp`：

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

`mcp` 裡的 key 會影響 OpenCode `/status` 顯示的名字。上面用 `codetrail`，所以應該看到 `codetrail Connected`。

檢查 JSON 格式：

```bash
python -m json.tool ~/.config/opencode/opencode.json >/dev/null
```

### 安裝 `aicode` 啟動指令

在 CodeTrail repo 根目錄執行。這段可以重跑；如果你剛換 repo，既有的舊 symlink 會被更新到目前這份 checkout：

```bash
chmod +x ./aicode
mkdir -p "$HOME/.local/bin"
ln -sfn "$PWD/aicode" "$HOME/.local/bin/aicode"
```

確認 shell 找得到它：

```bash
command -v aicode
```

如果沒有輸出，通常是 `$HOME/.local/bin` 還不在目前 shell 的 `PATH`。先讓這個 shell 生效：

```bash
export PATH="$HOME/.local/bin:$PATH"
command -v aicode
```

如果這樣有輸出，再把同一行 `export PATH="$HOME/.local/bin:$PATH"` 加到 `~/.bashrc` 或你的 shell 設定檔，之後新開終端機就會生效。

`aicode` 會做五件事：

- 將目前目錄設成 `AICODE_ROOT`
- 拒絕 `AICODE_ROOT=/` 和 `AICODE_ROOT=$HOME`
- 在目前專案準備 `.opencode/run-codetrail-mcp`，讓 OpenCode config 裡的 MCP command 能啟動 CodeTrail server
- 啟動 `opencode`，讓 OpenCode 子行程繼承同一個沙箱根目錄
- 如果有 `AICODE_MODEL`，自動把它轉成 OpenCode 的 `--model` 參數，讓 TUI 對話模型也預設成這顆（命令列自己帶 `-m` / `--model` 時不覆蓋）

---


## 啟動專案

日常建議優先使用 **OpenCode TUI + CodeTrail MCP**。這條路徑最完整：模型可以透過工具讀檔、搜尋程式碼、查 RAG、分析圖片/binary、必要時產生 patch，互動紀錄與工具結果也比較容易追蹤。

切到要分析或修改的專案，再啟動 OpenCode：

```bash
cd <PROJECT_TO_ANALYZE>
aicode
```

進入後可以直接問「請分析這個專案的整體架構」、「這個錯誤可能在哪裡」、「請查 RAG 裡某個規格限制」這類問題。涉及專案內容的問題，優先讓模型用工具讀實際檔案，不要只靠一般經驗回答。

進入 TUI 後先確認：

- 啟動畫面有 `[aicode] AICODE_ROOT=<PROJECT_TO_ANALYZE>`
- `/status` 顯示 `codetrail Connected`（或你在 `opencode.json` 裡設定的 MCP key）
- model selector 裡選的是 Ollama provider 的 coding model
- 第一輪工具呼叫沒有嘗試讀 `$HOME` 或 `/`

啟動時帶 `AICODE_MODEL`，TUI 右下角的對話模型跟 CodeTrail 後台用的模型會一起切到這顆：

```bash
AICODE_MODEL=qwen3-coder:30b aicode
```

換模型、context、offload、遠端 Ollama 的細節集中在 [模型與硬體建議](models.md)。不要只在 TUI 裡用 `/models` 切換；那只會換 OpenCode 前台對話模型，不會同步 CodeTrail MCP server 的內部呼叫。

外部附件匯入與 RAG 流程不在這裡重複；可先照 README 的 smoke test 跑一輪，完整說明見 [RAG、附件與知識庫操作](rag.md)。

---
