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

直接可貼的 `opencode.json` 放在 [README 的安裝與啟動](../README.md#安裝與啟動)。這裡只補充它的幾個關鍵點：

- Ollama provider 指到 `http://localhost:11434/v1`；遠端 Ollama 要同步改這裡，見 [模型與硬體建議](models.md)。
- MCP key 用 `codetrail`，所以 `/status` 會顯示 `codetrail Connected`。
- MCP command 會從 OpenCode 啟動目錄找 git root，然後執行該 root 內的 `.opencode/run-codetrail-mcp`。
- OpenCode 內建的 `bash` / `read` / `write` / `apply_patch` 預設 deny；日常讀檔、搜尋、patch 走 `codetrail_*` 工具。

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

`aicode` 會做六件事：

- 將目前目錄設成 `AICODE_ROOT`
- 拒絕 `AICODE_ROOT=/` 和 `AICODE_ROOT=$HOME`
- 在目前專案準備 `.opencode/run-codetrail-mcp`，讓 OpenCode config 裡的 MCP command 能啟動 CodeTrail server
- 啟動前跑 `scripts/ctx_safety_check.py` 預估「目前模型 + 目前 GPU + 要求的 ctx 上限」會不會把模型推到 CPU offload；預估會 offload 就直接 `exit 2` 拒絕啟動並提示安全 cap 值（拿不到 GPU 或 Ollama 時 graceful 放行，只 warn）
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

如果要讀專案外的 log、截圖、spec 或 firmware blob，啟動參數見 [README 的安裝與啟動](../README.md#安裝與啟動)；完整附件流程見 [RAG、附件與知識庫操作](rag.md)。

若工具看起來沒接上，再用 `/status` 檢查是否有 `codetrail Connected`（或你在 `opencode.json` 裡設定的 MCP key）。若啟動 root 不如預期，再看啟動畫面的 `[aicode] AICODE_ROOT=...`。

啟動時帶 `AICODE_MODEL`，TUI 右下角的對話模型跟 CodeTrail 後台用的模型會一起切到這顆：

```bash
AICODE_MODEL=qwen3-coder:30b aicode
```

換模型、context、offload、遠端 Ollama 的細節集中在 [模型與硬體建議](models.md)。不要只在 TUI 裡用 `/models` 切換；那只會換 OpenCode 前台對話模型，不會同步 CodeTrail MCP server 的內部呼叫。

RAG 流程不在這裡重複；可先照 README 的 smoke test 跑一輪，完整說明見 [RAG、附件與知識庫操作](rag.md)。

---
