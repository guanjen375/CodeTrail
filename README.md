# CodeTrail - OpenCode + Ollama 本地 MCP 工作台

CodeTrail 是一個給 OpenCode 使用的本地 MCP 後端。你在 OpenCode TUI 裡提問，模型可以透過 CodeTrail 讀專案、找程式碼、查已匯入的 spec、分析截圖或 binary、產生 patch，並在允許的白名單內跑驗證命令。

這份 README 只說明 **OpenCode + Ollama + CodeTrail MCP** 的使用方式。照順序完成設定後，日常操作就是：

```bash
cd <要分析或修改的專案>
aicode
```

CodeTrail 目前定位是**成熟私有部署版**：適合本機、離線、NDA / firmware / private repo 分析；**不打算公開發布**成 PyPI package、Docker image 或 SaaS。安全邊界有自動測試保護，但未做公開產品級安全審計。

開發者用的回歸評測、資料飛輪、文件一致性檢查不屬於 OpenCode 日常工具；需要維護或清理這些基礎設施時看 [README_DEV.md](README_DEV.md)。

---

## 你會用到什麼

- **OpenCode**：對話式 TUI，負責跟模型互動、顯示工具呼叫。
- **Ollama**：在本機跑主要 coding model、embedding model、reranker 和視覺模型。
- **CodeTrail MCP server**：把目前專案限制在 `AICODE_ROOT` 沙箱內，提供 17 個 MCP 工具給 OpenCode。
- **`aicode` wrapper**：從目前目錄啟動 OpenCode，並把目前目錄自動設成 `AICODE_ROOT`。

資料流：

```text
OpenCode TUI
  -> Ollama coding model
  -> CodeTrail MCP server
  -> AICODE_ROOT 內的程式碼 / spec / log / 圖片 / firmware
```

重點是 `AICODE_ROOT`。它就是這次 OpenCode 可以讀寫的專案根目錄。不要從 `$HOME` 或 `/` 啟動。

---

## 1. 安裝

以下用 `python` 表示 Python 3。如果你的系統只有 `python3`，把指令中的 `python` 改成 `python3`。

### 1.1 準備軟體

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

### 1.2 安裝 CodeTrail Python 依賴

```bash
cd <CODETRAIL_REPO>
pip install -r requirements.txt
pip install mcp pymupdf4llm ollama
```

`<CODETRAIL_REPO>` 是這個 CodeTrail repo 的路徑，不是你要分析的 firmware repo。

### 1.3 下載模型

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

模型怎麼選，見「模型比較」。

### 1.4 自檢

```bash
python scripts/doctor.py
```

如果只想檢查本地檔案與設定，不連 Ollama：

```bash
python scripts/doctor.py --no-network
```

`PASS` 可以先略過；`FAIL` 要處理。常見問題是 OpenCode 不在 PATH、Ollama 沒啟動、模型還沒 pull、`aicode` 沒有執行權。

---

## 2. 設定 OpenCode

### 2.1 建立 OpenCode config

```bash
mkdir -p ~/.config/opencode
${EDITOR:-vi} ~/.config/opencode/opencode.json
```

在檔案裡貼上下方內容，並把 `<CODETRAIL_REPO>` 換成 CodeTrail repo 的實際絕對路徑：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "ollama": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Ollama",
      "options": {
        "baseURL": "http://localhost:11434/v1"
      },
      "models": {
        "qwen3-coder:30b":          { "name": "Qwen3 Coder 30B" },
        "qwen3.6:35b-a3b-q4_K_M":   { "name": "Qwen3.6 35B A3B Q4_K_M" },
        "devstral:24b":             { "name": "Devstral 24B" },
        "gpt-oss:20b":              { "name": "GPT-OSS 20B" }
      }
    }
  },
  "mcp": {
    "codetrail": {
      "type": "local",
      "command": ["python", "<CODETRAIL_REPO>/mcp_server.py"],
      "enabled": true
    }
  }
}
```

如果系統沒有 `python` 指令，把 `command` 改成：

```json
"command": ["python3", "<CODETRAIL_REPO>/mcp_server.py"]
```

`mcp` 裡的 key 會影響 OpenCode `/status` 顯示的名字。上面用 `codetrail`；如果你沿用舊設定的 `ai_code` key，看到 `ai_code Connected` 也正常。

檢查 JSON 格式：

```bash
python -m json.tool ~/.config/opencode/opencode.json >/dev/null
```

### 2.2 安裝 `aicode` 啟動指令

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

`aicode` 會做四件事：

- 將目前目錄設成 `AICODE_ROOT`
- 拒絕 `AICODE_ROOT=/` 和 `AICODE_ROOT=$HOME`
- 啟動 `opencode`，讓 OpenCode 子行程繼承同一個沙箱根目錄
- 如果有 `AICODE_MODEL`，自動把它轉成 OpenCode 的 `--model` 參數，讓 TUI 對話模型也預設成這顆（命令列自己帶 `-m` / `--model` 時不覆蓋）

---

## 3. 啟動專案

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

`aicode` wrapper 會把這個值轉成 `opencode --model ollama/<名字>` 一起傳進去，所以一個 env var 兩邊對齊。要選別顆只切其中一邊的話，命令列自己帶 `-m / --model` 就會蓋過 wrapper 的自動行為。

OpenCode 右下角的選擇主要管整個對話和工具決策；`AICODE_MODEL` 額外影響 CodeTrail 內部少數需要直接呼叫 Ollama 的流程（主要是 `query_knowledge_strict`）。通常兩邊用同一顆比較好排查。

> ⚠️ **不要在 TUI 內按 `/models` 換模型**。`/models` 只會換 OpenCode 對話那邊那顆,**不會通知 MCP server**,後者仍然用 `AICODE_MODEL` 啟動時的那顆跑 RAG / `query_knowledge_strict`。兩邊不一致時 Ollama 會反覆 swap 模型進 VRAM,首 token 延遲明顯變高,而且不會有任何錯誤訊息提示這件事。
>
> **要換模型的正確流程**:退出 `aicode` → 改 `AICODE_MODEL` 環境變數 → 重新啟動 `aicode`。這樣 launcher 會把 OpenCode 跟 MCP server 兩邊一起對齊。

換成 35B 級的模型時，第一次跑建議搭配比較小的 `AICODE_DYNAMIC_NUM_CTX_MAX`。模型本身的權重就佔掉一大塊顯卡記憶體，如果 context 開太大，剩下的空間不夠用，模型會被自動拆一部分放到一般記憶體跑，速度會變很慢。

```bash
# 第一次跑 35B：把 context 開到 32K
AICODE_MODEL=qwen3.6:35b-a3b-q4_K_M AICODE_DYNAMIC_NUM_CTX_MAX=32768 aicode

# 用一陣子確定沒問題，再升到 64K
AICODE_MODEL=qwen3.6:35b-a3b-q4_K_M AICODE_DYNAMIC_NUM_CTX_MAX=65536 aicode
```

啟動前可以先確認模型已經下載完成，並看一下載入位置：

```bash
ollama pull qwen3.6:35b-a3b-q4_K_M
ollama ps
```

`ollama ps` 列出目前載入中的模型，最後一欄 `PROCESSOR`：

- `100% GPU`：完全放在顯卡裡，速度正常。
- `xx% / xx% CPU`：有一部分被搬到一般記憶體跑，回應會明顯變慢。出現這個就把 `AICODE_NUM_CTX` 再調小。

### 3.1 瀏覽器介面（測試中）

web 介面目前是測試中的輔助入口，適合在內網或 Tailscale 裡讓其他人上傳截圖、PDF、log、firmware，或做簡單的 RAG 問答。需要深入讀專案檔案、grep、產生 patch 或長時間除錯時，仍建議回到 OpenCode TUI。

只給本機自己連：

```bash
cd <PROJECT_TO_ANALYZE>
AICODE_WEB_USER=aiuser AICODE_WEB_PASSWORD='換成你的密碼' python <CODETRAIL_REPO>/web_server.py --project .
```

打開：

```text
http://127.0.0.1:8088/
```

要讓同一個 LAN / Wi-Fi 裡其他電腦也能連，listen `0.0.0.0`：

```bash
cd <PROJECT_TO_ANALYZE>
AICODE_WEB_USER=aiuser AICODE_WEB_PASSWORD='換成你的密碼' \
  python <CODETRAIL_REPO>/web_server.py --project . --host 0.0.0.0 --port 8088
```

server 會印出可嘗試的 LAN URL，例如：

```text
http://192.168.1.23:8088/
```

其他人用瀏覽器開這個網址，輸入你設定的帳號密碼。若連不上，先確認本機防火牆有放行 `8088/tcp`，以及對方跟你在同一個網段。這個 web server 是 plain HTTP，適合信任的內網；不要直接曝露到公網，公網請放在 HTTPS reverse proxy 或 VPN 後面。

沒有設定 `AICODE_WEB_PASSWORD` 時，server 會在終端機印出一次性的臨時密碼。需要固定密碼但不想放 shell history，可以用：

```bash
AICODE_WEB_USER=aiuser AICODE_WEB_PASSWORD_FILE=/path/to/password.txt python <CODETRAIL_REPO>/web_server.py --project .
```

也可以直接用 CLI 指定帳密，但密碼會出現在本機 `ps` 行程列表，只有在可信任機器上才建議這樣用：

```bash
python <CODETRAIL_REPO>/web_server.py --project . --host 0.0.0.0 --user aiuser --password '換成你的密碼'
```

要從真正公網連進來，不能只改 `--host 0.0.0.0`。還需要：

- 路由器做 port forwarding：`<公網 IP>:8088` → `<PROJECT_HOST_LAN_IP>:8088`
- Linux 防火牆放行：`sudo ufw allow 8088/tcp`
- 若 ISP 沒給可被連入的公網 IP（CGNAT），改用 Tailscale / VPN / reverse proxy

Tailscale 方式不需要路由器轉發，直接用 `http://<PROJECT_HOST_TAILSCALE_IP>:8088/`。

web 介面的檔案處理方式：

- 上傳檔案會先存到目前專案底下的 `.aicode_web/uploads/`，這個目錄已在 `.gitignore`，避免 NDA 檔案被 commit。
- 在對話中勾選「附加到問題」時，文字 / log / 程式碼檔會直接讀入本輪上下文；圖片、ELF、binary 會轉成既有的 `file:"..."` 流程交給 `media.py`；PDF 會嘗試用 `RAG.py` 的文件抽取流程做一次性上下文。
- 預設送出是快速問答，會用本輪問題、附件與 RAG 回答，且使用較小的 web context 以降低等待時間；如果要讓模型讀專案檔案、grep 或做 Code RAG 探索，勾選「專案 Agent 探索」再送出。
- 點「加入 RAG」時，server 會呼叫既有 `RAG.py` 入庫流程，更新專案內的 `knowledge.json` / `knowledge_emb.npz`，再重新載入知識庫。
- web 模式不允許直接讀專案外的任意路徑；瀏覽器檔案必須先上傳進專案內的暫存目錄。

如果要讓 OpenCode 匯入專案外的截圖、PDF、log 或 firmware blob，啟動時明確開啟外部匯入入口：

```bash
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 aicode
```

預設只允許從 `~/Downloads` 和 `/tmp` 匯入。要加其他來源，用 `AI_CODE_IMPORT_ROOTS` 指定：

```bash
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 AI_CODE_IMPORT_ROOTS="$HOME/Downloads:/mnt/share" aicode
```

---

## 3.2 Context、Offload 與 Silent Truncation

模型「看不到」想看到的內容，有兩種完全不同的原因，常常被混在一起談：

| 現象 | 是什麼問題 | 怎麼看出來 |
|---|---|---|
| **Context overflow**（爆 ctx）| **正確性問題**。Prompt 比模型 context 還大,後面被裁掉,模型實際上沒讀到。 | CodeTrail 的 `[CTX]` log、`.codetrail/context_metrics.jsonl`、`[CTX_OVERFLOW]` 錯誤訊息 |
| **Offload**（CPU/GPU split）| **速度問題**。權重或 KV cache 被放到 RAM 而不是 VRAM,首 token 延遲拉長,但內容看得到。 | `ollama ps` 的 `PROCESSOR` 欄;只看這個 **無法** 判斷有沒有 ctx overflow |

**只看 `ollama ps` 不會發現 prompt overflow** — `ollama ps` 報告的是模型權重和 KV cache 的擺放位置,跟使用者送進去的 prompt 大小無關。CodeTrail 自己會在送 prompt 前估算大小,觸發三個機制:

- 使用率低於 ~25% → 安靜過;
- 80–90% → 印 `[CTX] WARNING use=...%; consider trimming...`;
- ≥ 90% → 印 `[CTX_OVERFLOW] ... Refusing to send to avoid silent truncation.`,**不會** 把 prompt 送出去。

每次呼叫的 metadata(model、效能數據、是否裁切等,**不含 prompt 內容**)會寫進 `.codetrail/context_metrics.jsonl`,已被 `.gitignore` 涵蓋。

### 兩條獨立的 context 管線

CodeTrail 與 OpenCode TUI 用兩條不同的 Ollama 路徑,context 設定**不會自動同步**:

| 路徑 | 用什麼控制 context | 影響什麼 |
|---|---|---|
| CodeTrail 內部 native call(`/api/generate` / `/api/chat`)| `AICODE_NUM_CTX` + `DYNAMIC_NUM_CTX_MIN/MAX` | RAG strict 模式、agent 工具迴圈、`query_knowledge_strict` |
| OpenCode TUI 主對話(走 Ollama `/v1` OpenAI-compatible)| Ollama server 的 `OLLAMA_CONTEXT_LENGTH` + OpenCode `model.limit.context` | 你在 TUI 裡 chat 看到的那個對話本身 |

要讓 OpenCode TUI 的 context 真的變大,光改 `AICODE_NUM_CTX` 是不夠的:server 端也要設,而且要 restart Ollama 才會生效。

```bash
OLLAMA_CONTEXT_LENGTH=65536 \
OLLAMA_FLASH_ATTENTION=1 \
OLLAMA_KV_CACHE_TYPE=q8_0 \
OLLAMA_NUM_PARALLEL=1 \
ollama serve
```

systemd 版本:

```bash
sudo systemctl edit ollama.service
```

```
[Service]
Environment="OLLAMA_CONTEXT_LENGTH=65536"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_NUM_PARALLEL=1"
```

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
ollama ps
```

### 三種典型啟動組合

```bash
# 日常 35B(穩、快、夠用)
AICODE_MODEL=qwen3.6:35b-a3b-q4_K_M AICODE_DYNAMIC_NUM_CTX_MAX=32768 aicode

# 深度 35B(要看比較長的 context,速度會慢一點)
AICODE_MODEL=qwen3.6:35b-a3b-q4_K_M AICODE_DYNAMIC_NUM_CTX_MAX=65536 aicode

# 70B Q4 當 reviewer(慢但精準;不適合快速 agent loop)
AICODE_MODEL=<70B-Q4-model> AICODE_DYNAMIC_NUM_CTX_MAX=16384 aicode
```

`ollama ps` 看 `PROCESSOR`:

- `100% GPU` → 速度正常。
- `xx% CPU / xx% GPU` → 速度退化但內容完整。日常 30B/35B 出現這個就把 `AICODE_DYNAMIC_NUM_CTX_MAX`(dynamic 模式下真正生效的上限)/ `OLLAMA_CONTEXT_LENGTH` 調小,或檢查 `OLLAMA_FLASH_ATTENTION=1` 跟 `OLLAMA_KV_CACHE_TYPE=q8_0`。70B Q4 split 是預期行為,不用追求 100% GPU。

如果看到 CodeTrail 印 `[CTX_OVERFLOW]`,就是**正確性問題**了,要做的事:

- 縮小問題範圍,把任務拆成多步;
- 工具呼叫指定 `path` + `start/end_line`,不要一次 dump 整個檔案;
- 降低 RAG 的 `KNOWLEDGE_TOP_K`,讓 query 更具體;
- 設定 `AICODE_RESERVED_OUTPUT_TOKENS=2048`(若你只需要短回答);
- 確認你改的是 `AICODE_DYNAMIC_NUM_CTX_MAX`(dynamic 啟用時真正的上限),`AICODE_NUM_CTX` 在這個模式下不影響 per-call ctx。

可以調整的 env vars:

| Env var | 預設 | 作用 |
|---|---|---|
| `AICODE_DYNAMIC_NUM_CTX_MAX` | 65536 | **動態 ctx 上限**。dynamic 開啟(預設)時,真正的 per-call ctx 上限,要調 ctx 改這個 |
| `AICODE_NUM_CTX` | 131072 | dynamic **關閉**時的 fallback 上限;dynamic 開啟時只影響 banner 顯示,不影響實際 per-call ctx |
| `AICODE_RESERVED_OUTPUT_TOKENS` | 4096 | 估算時保留給輸出的 token,影響 gate 觸發點 |
| `AICODE_CTX_SOFT_THRESHOLD` | 0.80 | 印 WARN 的使用率門檻 |
| `AICODE_CTX_HARD_THRESHOLD` | 0.90 | 拒絕送出的使用率門檻 |
| `AICODE_CTX_GATE_ENABLED` | 1 | 設 0 可暫時關閉硬性 gate(僅供除錯)|
| `AICODE_CTX_METRICS_ENABLED` | 1 | 是否寫 telemetry |
| `AICODE_CTX_METRICS_PATH` | `.codetrail/context_metrics.jsonl` | telemetry 路徑 |
| `OLLAMA_CONTEXT_LENGTH` | (Ollama 預設)| Ollama server 層,影響 OpenCode TUI 主對話 |

`scripts/doctor.py` 會自動檢查上面這些設定是否相互打架,以及 `ollama ps` 上的模型是否 CPU/GPU split。任何錯配都會印 `[WARN]`,但**不會** 自動改動使用者的設定。

---

## 4. 重點教學

啟動 aicode 進到對話之後，最常碰到兩件事：

1. 有一張錯誤截圖／一份韌體 binary／一段 log，想讓對話幫忙看。
2. 有一份產品規格書／datasheet／設計手冊，想讓之後對話遇到相關問題時答得準。

這兩件事分別對應 §4.1 和 §4.2。讀完這兩節就能開始實用。

### 4.1 在對話裡讓模型看到一個檔案

#### 場景一：檔案在當前專案目錄裡

最簡單。放到 `cd` 進去那個目錄底下任何位置，然後在對話裡點名工具和檔案路徑：

| 檔案類型 | 對話可以這樣說 |
|---|---|
| 程式碼 / log / 純文字 | 「請用工具 `read_file` 讀 `logs/build_fail.txt`」 |
| 截圖 / 圖片 | 「請用工具 `analyze_file` 分析 `screenshots/error.png`」 |
| 韌體 / 執行檔 / 二進位（.bin / .elf / .img） | 「請用工具 `analyze_file` 分析 `firmware/boot.bin`」 |

兩個工具差別：

- `read_file` 直接把純文字內容讀進對話。
- `analyze_file` 會先做處理 — 圖片做文字辨識、二進位檔抓出檔頭格式和可讀字串 — 再把整理後的結果丟給模型。

`analyze_file` 是「這一輪看一次就丟」，看完不會留在 KB 裡，未來其他對話查不到。如果想把這張截圖／這份 firmware 永久保存供之後查詢，改用 `ingest_document`（見 §4.2），它接受相同的圖片／binary／ELF 副檔名，並會切 chunk、算 embedding 寫進 `knowledge.json`。

#### 場景二：檔案在專案目錄外

預設情況下系統不能讀專案目錄以外的東西。這是安全限制：避免分析陌生程式碼時模型意外讀到家目錄裡的 SSH key、密碼、別的專案這類敏感資料。

要讓外部檔案進到對話，要設兩個 env var，這兩個分工不同，**一起設才會生效**：

| Env var | 角色 | 預設 |
|---|---|---|
| `AI_CODE_ALLOW_EXTERNAL_IMPORT=1` | **總開關**。決定外部匯入功能能不能用 | 關閉 |
| `AI_CODE_IMPORT_ROOTS="<目錄1>:<目錄2>:..."` | **白名單**。決定哪些目錄底下的檔案可以匯入 | `~/Downloads:/tmp` |

只打開總開關，預設白名單只有 `~/Downloads` 和 `/tmp`，其他目錄底下的檔案還是拿不到。`AI_CODE_IMPORT_ROOTS` 一旦自己設了就**完全取代**預設清單 — 要保留 Downloads/tmp 記得自己列上。

幾種常見組合：

```bash
# 只用預設來源 (~/Downloads + /tmp)
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 aicode

# 保留預設 + 加一個自己的目錄
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 \
AI_CODE_IMPORT_ROOTS="$HOME/Downloads:/tmp:$HOME/u-boot" \
aicode

# 整個家目錄都放開（最寬鬆，沒敏感檔的話最省事）
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 AI_CODE_IMPORT_ROOTS="$HOME" aicode
```

多個目錄用冒號分隔（跟 `$PATH` 一樣）。如果每次都用同一組設定，加進 `~/.bashrc` 就不用每次帶：

```bash
export AI_CODE_ALLOW_EXTERNAL_IMPORT=1
export AI_CODE_IMPORT_ROOTS="$HOME/Downloads:/tmp:$HOME/u-boot"
```

開啟後，對話裡先請模型把檔案複製進專案再分析：

```text
請用工具 import_external_file 匯入 ~/Downloads/error.png，
然後用回傳的新路徑做 analyze_file。
```

複製進來的檔案會放在專案根目錄底下的 `.aicode_uploads/` 資料夾，原始檔案不會被搬走或修改。後續對話就把它當成專案內檔案處理。

匯入被拒絕時錯誤訊息會印出目前生效的白名單，如果看到拒絕但不確定原因，第一件事先確認檔案路徑有沒有真的在白名單裡的某個目錄底下。

#### 完整範例

```text
我在 ~/Downloads/oops.png 拍到一個錯誤訊息畫面，
請先用 import_external_file 匯入，
再用 analyze_file 認出畫面上的錯誤文字，
最後用 grep_code 在當前專案找這串錯誤可能來自哪個 .c 檔。
```

### 4.2 把附件做成知識庫讓模型隨時能查

「知識庫」是這個專案放規格書、手冊、設計文件的地方。一旦把文件匯進去，之後對話遇到相關問題時，系統會自動找出最相關的幾段內容當作回答依據，並用 `REF1` `REF2` 標出每段是引用自哪份文件的哪個位置。

比起每次都重新貼一份 PDF 給對話，這樣比較不會超出上下文長度限制，也比較不會記錯。

#### 支援格式

- **文字**：`.pdf` / `.md` / `.txt`（直接抽文字）
- **圖片**：`.png` / `.jpg` / `.jpeg` / `.gif` / `.webp`（用 VL 模型看圖、抽出文字描述後切 chunk，需要先 `ollama pull` 設定的 VL_MODEL，預設 `qwen3-vl:30b-a3b`）
- **binary**：`.bin` / `.dat` / `.raw` / `.fw` / `.img` / `.rom` / `.hex`（抽 hex dump、可讀字串、magic 偵測；遇到 ELF magic 自動切到 ELF 解析）
- **ELF**：`.elf` / `.so` / `.o` / `.axf` / `.out` / `.ko`（抽 header / sections / symbols）

純圖片掃描的 PDF（沒有可選文字）切不出內容，先把每頁存成 `.png` 再用 `ingest_document` 走圖片路徑，或先用 OCR 工具轉成文字檔再匯入。

#### 三個步驟

**步驟 1：檔名取對**

檔名會直接影響搜尋排序。同樣內容檔名清楚會排得比較前面：

| 檔名裡有這些字 | 系統當成 | 適合裝的內容 |
|---|---|---|
| `spec` / `datasheet` | 規格書（最權威） | 規格、限制、硬體行為 |
| `api` / `reference` | API 文件 | 函式定義、參數、回傳值 |
| `manual` / `handbook` | 手冊 | 操作流程 |
| `guide` / `tutorial` | 教學 | 上手指南 |
| `faq` | 常見問題 | 問答對 |

例如把 NPU 規格書命名成 `npu_spec.pdf`，會比叫 `doc.pdf` 在「最大張量大小是多少」這類規格問題裡更容易被優先找到。

**步驟 2：匯入並重新載入**

把檔案放進專案目錄（建議統一放在 `docs/`），對話裡：

```text
請用工具 ingest_document 匯入 docs/npu_spec.pdf，
完成後用工具 reload_knowledge_base 重新載入。
```

`ingest_document` 會把整份文件切成多段、算出每段的向量、存進專案根目錄的 `knowledge.json`。`reload_knowledge_base` 把剛存進去的內容立刻吃進記憶體 — **每次匯入或刪除文件後都要呼叫**，不然查不到。

預設依副檔名自動分派到對應的處理路徑（見上方「支援格式」清單）。圖片預設走「技術圖片」路徑（架構圖／流程圖／記憶體圖），抽出的是畫面說明；若這張是聊天截圖、想抽出對話內容，要顯式傳 `mode="chat"`：`ingest_document("teams.png", mode="chat")`。

一次匯入多份：

```text
請依序執行：
1. ingest_document docs/npu_spec.pdf
2. ingest_document docs/api_reference.md
3. ingest_document docs/faq.txt
4. reload_knowledge_base
最後回報目前載入幾個 chunks。
```

`chunks` 是「切好的文件段落」。回報 0 代表沒匯入到任何內容 — 常見原因：純圖片掃描的 PDF（沒可選文字）、binary 太小或全是 0xff、VL 模型沒 pull 導致圖片分析失敗。

**步驟 3：查**

匯進去之後用 `query_knowledge`：

```text
請用工具 query_knowledge 查 conv2d 的輸入大小限制，
回答時每個數字都要附 REF 標記。
```

回答長這樣：

```text
根據 REF1，conv2d 輸入張量的高/寬上限是 4096 (REF1: npu_spec.pdf §3.2.1)。
batch size 上限是 32 (REF1)。
```

#### 規格題、數字題用嚴格模式

「最大值是多少」「預設值是什麼」「reset 訊號最少要拉幾毫秒」這種**答錯比不答更糟**的題目，改用 `query_knowledge_strict`：

```text
請用工具 query_knowledge_strict 查 reset assert 最小持續時間，
證據不夠就直接拒答，不要用常識補。
```

兩者差別：

- `query_knowledge`：把找到的文件段落丟給對話模型，模型自己組答案。
- `query_knowledge_strict`：在背後跑兩階段檢查 — 先看找到的內容是不是真的足以回答；確認後再驗證最終答案每一句話都有對應的 `REF` 出處；任何一句沒對到的會被刪掉，證據真的太弱就直接回「拒答」而不是亂編。

代價是後者比較慢，而且因為是後台跑，TUI 不會顯示中間過程，只看得到定稿後的答案。

#### 維護

文件改版時把舊版刪掉再加新的：

```text
請用工具 remove_document 移除 old_spec.pdf，
完成後 ingest_document docs/new_spec.pdf，
最後 reload_knowledge_base。
```

想看目前知識庫有多少內容：

```text
請用工具 reload_knowledge_base，回報目前載入幾個 chunks。
```

#### 三件容易踩的事

1. **知識庫綁專案目錄**：`knowledge.json` 存在當前專案根目錄裡，換到另一個專案就要重新匯入。同一份規格書在多個專案要用就匯入多次。
2. **不要 commit**：`knowledge.json` 切碎了原始文件內容，NDA 場景幾乎一定包含敏感片段。已經在 §8.4 列入不該 commit 的清單，建議在專案的 `.gitignore` 也加一行。
3. **越具體越好**：把一整份 500 頁的手冊原封不動塞進去，不如先抽出實際會問到的章節整理成 markdown 再匯入。雜訊少，答案準。

### 4.3 先建立專案地圖

第一次接到一個 repo，先要求對話只讀不改：

```text
先不要改檔。
請用 list_dir 看兩層目錄，找出主要 entry point、測試目錄、設定檔。
再用 code_rag_search 找初始化流程、工具呼叫、資料載入相關程式。
最後用 file:line 列出這個 repo 的架構判斷，分成「證據」和「推測」。
```

這會逼對話先走 `list_dir(...)`、`code_rag_search(...)`、`read_file(...)`，避免一開始就憑印象回答。

很大的 repo（例如 U-Boot 這種數萬檔的）第一次跑 `code_rag_search` 要先建索引，可能會超時。這類情況先用 `list_dir` + `grep_code` 縮小範圍，把問題鎖在一兩個子目錄裡再用 `code_rag_search`。

### 4.4 查 bug 或行為

```text
請追這個錯誤的來源：<貼錯誤訊息>
先用 grep_code / code_rag_search 找可能位置，再讀檔確認。
不要改檔，先列出最可能的 3 個原因與 file:line 證據。
```

如果有 log 檔，先放進專案內，例如 `logs/build_fail.txt`，再問：

```text
請 read_file logs/build_fail.txt，根據錯誤訊息找最可能的實作位置。
```

### 4.5 要它改檔

等對話已經列出證據，再允許 patch：

```text
根據上面的證據，請做最小修改。
套用 patch 前先說會改哪些檔案；套用後跑最小相關測試。
如果 run_command 被白名單拒絕，請列出你原本想跑的命令。
```

`apply_patch(...)` 會真的寫檔。建議在 git worktree 乾淨時使用；不想改檔時要明講「不要 apply_patch」。

---

## 5. CodeTrail 暴露的 17 個 MCP 工具

你不用手動寫 JSON 或自己呼 API。這些工具會出現在 OpenCode 的工具列表裡；日常用法是在對話中直接要求模型「用工具 `<工具名>` 做某件事」。多數情況只講工具名就夠了，模型會自己補預設參數；需要指定檔案、行號、搜尋範圍時，再把那些條件寫進自然語言。

### 5.1 最常用講法

| 你想做什麼 | 在 OpenCode 裡可以這樣說 | 主要工具 |
|---|---|---|
| 先看 repo 長什麼樣 | 請用工具 `list_dir` 看專案結構，找 entry point、測試和設定檔。 | `list_dir(...)` |
| 不知道程式在哪 | 請先用工具 `code_rag_search` 搜尋「初始化流程」，再用工具 `read_file` 讀最相關檔案。 | `code_rag_search(...)`、`read_file(...)` |
| 找某個字串或錯誤訊息 | 請用工具 `grep_code` 搜尋錯誤訊息「panic: xxx」，範圍限 C/C++ 檔，並顯示上下文。 | `grep_code(...)` |
| 讀一個已知檔案 | 請用工具 `file_info` 看 `src/main.py` 大小，再用工具 `read_file` 讀前 120 行。 | `file_info(...)`、`read_file(...)` |
| 查已匯入的 spec | 請用工具 `query_knowledge` 查 reset timing 限制，回答要附 REF。 | `query_knowledge(...)` |
| 查不能答錯的規格數字 | 請用工具 `query_knowledge_strict` 查 reset assert 最小時間，證據不夠就拒答。 | `query_knowledge_strict(...)` |
| 看專案外的截圖/PDF/log | 請先用工具 `import_external_file` 匯入 `~/Downloads/error.png`，再分析回傳的新路徑。 | `import_external_file(...)` |
| 看圖片、ELF、firmware | 請用工具 `analyze_file` 分析 `.aicode_uploads/error.png`，做 OCR 或 binary 分析。 | `analyze_file(...)` |
| 把文件/圖片/binary 加進 KB | 請用工具 `ingest_document` 匯入 `docs/spec.pdf`（或 `arch.png`、`firmware.bin`），完成後用工具 `reload_knowledge_base`。 | `ingest_document(...)`、`reload_knowledge_base()` |
| 移除舊文件 | 請用工具 `remove_document` 移除 `old_spec.pdf`，完成後用工具 `reload_knowledge_base`。 | `remove_document(...)` |
| 準備改檔 | 請先用工具 `git_status` 和 `git_diff` 確認目前變更，再說明要改哪些檔案。 | `git_status(...)`、`git_diff(...)` |
| 套修改 | 請產生最小 unified diff，先用工具 `apply_patch` 預覽，再正式套用。 | `apply_patch(...)` |
| 修改後檢查 | 請用工具 `run_lint` 檢查剛改的檔案，再用工具 `run_command` 跑最小相關測試。 | `run_lint(...)`、`run_command(...)` |

### 5.2 依任務分類

| 類型 | 工具 | 白話用途 |
|---|---|---|
| 專案探索 | `list_dir(path=".", depth=2)` | 看目錄樹，不要叫模型跑 `ls` |
| 專案探索 | `code_rag_search(query, top_k=5)` | 用「這段程式在做什麼」去找可能的函式/class |
| 專案探索 | `grep_code(pattern, path=".", include=None, context=0)` | 搜錯誤訊息、函式名、設定名 |
| 專案探索 | `file_info(path)` | 讀檔前先看大小，避免一次塞爆 context |
| 專案探索 | `read_file(path, start_line=1, end_line=None, max_chars=50000)` | 讀檔案內容，長檔要分段 |
| 文件/外部檔案 | `import_external_file(path, dest_name=None)` | 把允許來源的外部檔案複製進 `.aicode_uploads/` |
| 文件/外部檔案 | `analyze_file(path)` | OCR 圖片、分析 ELF 或 firmware blob |
| 文件/外部檔案 | `ingest_document(path, mode="auto")` | 把 PDF / MD / TXT / 圖片(png/jpg/...) / binary(bin/elf/...) 匯入 `knowledge.json`；`mode` 預設依副檔名自動選，可顯式 `image` / `chat` / `binary` / `document` |
| 文件/外部檔案 | `remove_document(source)` | 從 KB 移除過期文件 |
| 文件/外部檔案 | `reload_knowledge_base()` | 讓剛匯入或刪除的 KB 內容立即生效 |
| 文件/外部檔案 | `query_knowledge(question)` | 查 KB，適合 spec / manual / datasheet |
| 文件/外部檔案 | `query_knowledge_strict(question)` | 查高風險規格題，弱證據會拒答 |
| 修改/驗證 | `git_status()` | 看工作樹目前有沒有改動 |
| 修改/驗證 | `git_diff(path=None, staged=False)` | 看修改內容，不需要用 `run_command` 跑 git |
| 修改/驗證 | `apply_patch(diff, dry_run=False)` | 套 unified diff，會真的寫檔 |
| 修改/驗證 | `run_lint(path, fix=True)` | 對單一檔案跑格式化/lint |
| 修改/驗證 | `run_command(cmd)` | 跑白名單內的測試、lint、build |

### 5.3 使用原則

- 找程式碼時，先請模型用工具 `code_rag_search` 或 `grep_code`，再用工具 `read_file`。
- 長檔先用工具 `file_info` 看大小，再要求工具 `read_file` 分段讀。
- 查 spec 先用工具 `query_knowledge`；數字、限制、預設值這類答錯很糟的題目，用工具 `query_knowledge_strict`。
- 外部檔案先用工具 `import_external_file`，再用工具 `analyze_file`、`ingest_document` 或 `read_file` 處理匯入後路徑。
- 新增或刪除文件後一定要用工具 `reload_knowledge_base`。
- 改檔前先看工具 `git_status` / `git_diff`；改檔用工具 `apply_patch`。
- 工具 `apply_patch` 和 `run_command` 有副作用；需要改檔或執行專案腳本時才允許。

---

## 6. 文件與知識庫補充

操作流程的主體寫在 §4.2，這節只列幾個補充細節：

- `knowledge.json` 存在當前專案根目錄下，預設會被 `.gitignore` 忽略。它保存切碎後的文件內容，NDA 場景下幾乎一定有敏感片段，**不要 commit**。
- `remove_document(...)` 用檔名 basename 比對，所以傳完整路徑（`docs/old_spec.pdf`）或單純檔名（`old_spec.pdf`）都可以。
- 文件切段的大小、不同來源類型的搜尋權重，這些可調參數放在 `config.py` 的 `CHUNK_SETTINGS` 和 `SOURCE_TYPE_WEIGHTS`，預設值在大多數情境下已經夠用，要微調再去動。

---

## 7. 模型比較

下面比較的是這個 repo 的 OpenCode 設定檔已列出的模型，以及 CodeTrail 內部會用到的 RAG / 視覺模型。

| 模型 | 建議用途 | 優點 | 注意事項 |
|---|---|---|---|
| `qwen3-coder:30b` | 預設主力；讀 repo、改 code、產 patch、跑驗證閉環 | coding 能力和工具使用穩定度最均衡；適合作為日常預設 | 比 20B 模型慢；長工具鏈任務建議拆成「先查證、再修改」 |
| `qwen3.6:35b-a3b-q4_K_M` | 跨檔推理、規格 vs 實作比對、較複雜重構 | 推理上限較高；大 context 任務表現較好 | 顯卡 32GB 的話第一次跑先設 `AICODE_DYNAMIC_NUM_CTX_MAX=32768`，用一陣子沒問題再升到 `65536`。**不要**用 `qwen3.6:35b-a3b-coding-nvfp4`（macOS 限定的版本，Linux 拉會報錯 412） |
| `devstral:24b` | 快速 code review、找 bug、簡單 patch | 速度和 coding 能力平衡；回答通常直接 | 工具呼叫格式不一定比 Qwen Coder 穩；大型修改前建議切回 Qwen |
| `gpt-oss:20b` | 快速理解陌生 repo、摘要、初步定位 | 輕量、啟動快、硬體壓力低 | 複雜改檔與長工具鏈較弱；適合探索，不適合作為最終 patch 主力 |
| `qwen3-vl:30b-a3b` | `analyze_file(...)` 處理截圖、UI error；`ingest_document(...)` 把圖片切 chunk 進 KB 也用它 | 讀圖中文字與畫面資訊較好 | 不是主要 coding model；不分析圖片、也不把圖片進 KB 就不用 pull |
| `bge-m3` | `query_knowledge(...)` / `code_rag_search(...)` 的 embedding | 多語檢索穩定；中文 spec 與英文程式碼混用時有幫助 | 不是聊天模型，不要在 OpenCode model selector 裡選 |
| `qllama/bge-reranker-v2-m3` | RAG rerank | 能改善 spec 查詢排序，降低抓到弱相關 chunk 的機率 | 會增加查詢延遲；模型未 pull 時 RAG 品質會下降或報錯 |

實務選法：

- 要穩定完成「查證 -> patch -> test」：用 `qwen3-coder:30b`。
- 任務跨很多檔、要比對規格或做設計判斷：用 `qwen3.6:35b-a3b-q4_K_M`（Linux 上能用的版本；`qwen3.6:35b-a3b-coding-nvfp4` 是 macOS 限定，不要用）。
- 只想先看懂 repo 或做初步 review：用 `gpt-oss:20b` 或 `devstral:24b`。
- 要讀截圖或把圖片進 KB：保留主聊天模型不變，讓 `analyze_file(...)` / `ingest_document(...)` 使用 `qwen3-vl:30b-a3b`。

Context 建議：

`AICODE_DYNAMIC_NUM_CTX_MAX` 控制每次能塞給模型的文字量上限（單位是 token，1 token 大約 3–4 個字元）。值越大可以一次給越多檔案內容或對話歷史，但模型在 VRAM 裡要額外佔的空間也越大。

> 註：早期文件叫使用者調 `AICODE_NUM_CTX`，但那個變數在 dynamic mode 開啟（預設）時只是 banner 顯示與 dynamic-off fallback，不會真正影響 per-call ctx。要實際改上限請用 `AICODE_DYNAMIC_NUM_CTX_MAX`。

```bash
# 30B 以下模型：直接 64K 通常最穩
AICODE_DYNAMIC_NUM_CTX_MAX=65536 aicode

# 35B 級的模型（如 qwen3.6:35b-a3b-q4_K_M）：先用 32K 跑一次，確認沒問題再升
AICODE_MODEL=qwen3.6:35b-a3b-q4_K_M AICODE_DYNAMIC_NUM_CTX_MAX=32768 aicode
```

判斷要不要升上去：

- 開新的視窗跑 `ollama ps`，看載入中模型那行的 `PROCESSOR` 欄位。
- 顯示 `100% GPU`：模型完全放在顯卡裡，速度正常，可以考慮把 `AICODE_NUM_CTX` 升到 65536 再跑一輪。
- 顯示 `xx% / xx% CPU`：顯卡記憶體不夠，模型有一部分被搬到一般記憶體跑，回應會明顯變慢（首字出來特別久）。這時把 `AICODE_NUM_CTX` 改小一點再啟動。

128K（131072）需要顯卡記憶體 + 系統記憶體都很充裕才合理，35B 級的模型不建議直接開到 128K。

---

## 8. 安全邊界

### 8.1 沙箱

MCP server 啟動時會執行：

```text
set_sandbox_root(AICODE_ROOT, allow_external=False)
```

結果：

- `read_file(...)`、`grep_code(...)`、`list_dir(...)` 只能看 `AICODE_ROOT` 內的檔案。
- `analyze_file(...)`、`ingest_document(...)` 的輸入也必須在 `AICODE_ROOT` 內。
- `import_external_file(...)` 是唯一外部入口，預設關閉；開啟後也只會把允許來源目錄內的檔案複製進 `.aicode_uploads/`。設定方式見 §4.1 場景二。
- `apply_patch(...)` 只能改沙箱內檔案，且 patch context 必須跟現有檔案相符。
- `aicode` 會拒絕把 `/` 或 `$HOME` 當 root。

注意這層沙箱**只蓋 CodeTrail 的 17 個 MCP 工具**。OpenCode 自己內建了 `bash`、`read`、`write` 等工具，這些是 OpenCode 的東西，不走 CodeTrail 的 MCP 沙箱，因此能讀寫整個檔案系統（在目前 user 權限範圍內）。實務上常碰到的場景：CodeTrail 的 `import_external_file` 因為白名單擋下時，模型有時會 fallback 去用 OpenCode 內建的 `$ cp` 把檔案搬進專案目錄，照樣達到目的。要徹底鎖死，得從 OpenCode 設定那邊關掉它的內建工具，CodeTrail 沙箱層面控制不到。

### 8.2 Patch

`apply_patch(...)` 限制：

- 單次最多 5 個檔案
- 單檔最多 200 行修改
- hunk context 不符會拒絕

這些限制是保護用的，不要為了方便把它拿掉。大型修改請拆小步。

### 8.3 Run Command

`run_command(...)` 只允許白名單命令，例如：

- Python：`pytest`、`python -m pytest`、`python -m unittest`
- C/C++：`ctest`、`make`、`cmake`、`cmake --build`、`ninja`
- Node：`npm test`、`npm run test`、`yarn test`
- Rust：`cargo test`、`cargo clippy`
- Go：`go test`、`go vet`
- Lint / format：`ruff`、`black`、`isort`、`eslint`、`clang-format`

即使有白名單，`make`、`cmake`、`npm test` 仍可能執行專案內腳本。只在可信專案使用。

### 8.4 不要 commit 的資料

這些通常含有 NDA 內容或本地快取，應留在 `.gitignore`：

- `knowledge.json`
- `knowledge_emb.npz`
- `.code_rag_cache_*`
- `.rag_embedding_cache.json`
- `.opencode/`
- `.aicode_uploads/`
- `data/`
- `*.jsonl`

### 8.5 開發者資料飛輪（選用）

這不是 OpenCode 日常必用功能。只有在你想收集互動樣本、日後做 reranker / fine-tuning / prompt regression 時才開。

設 `AI_CODE_COLLECT_DATA=1` 啟動 `aicode`，KB-shaped 工具（`query_knowledge` / `query_knowledge_strict` / `code_rag_search`）的每次呼叫會 append 一筆到 `data/interactions.jsonl`，含問題、回答（或 `[REFUSED]` / `[SKIPPED_STRICT:...]`）、refs、KB 分數與當下 git commit。預設關閉。

該檔在 NDA 場景必然含敏感片段，已在 §8.4 列入「不要 commit 的資料」。要看統計或匯出訓練語料，跑：

```bash
python data_flywheel.py stats
```

`eval/` 也是開發者用的固定題庫 / 回歸評測，不會自動記錄對話。兩者差異與清理方式見 [README_DEV.md](README_DEV.md)。

---

## 9. 常見問題

### `/status` 沒看到 CodeTrail MCP Connected

檢查：

```bash
python -m json.tool ~/.config/opencode/opencode.json >/dev/null
command -v aicode
command -v opencode
```

再確認 `opencode.json` 裡的 `<CODETRAIL_REPO>/mcp_server.py` 是實際路徑，且 `/status` 裡的名字會跟 `mcp` key 一致；如果 key 是 `codetrail`，應該看到 `codetrail Connected`。

### 啟動時拒絕 `AICODE_ROOT`

你可能在 `$HOME` 或 `/` 執行了 `aicode`。切到具體專案：

```bash
cd ~/work/some-firmware-repo
aicode
```

### 模型 404 或找不到模型

代表 Ollama 沒有該 tag：

```bash
ollama pull qwen3-coder:30b
ollama pull qwen3.6:35b-a3b-q4_K_M
ollama pull devstral:24b
ollama pull gpt-oss:20b
```

### 查 spec 沒結果

先確認文件已經匯入並 reload：

```text
請 reload_knowledge_base，回報目前載入幾個 chunks。
```

如果 chunks 是 0，重新要求：

```text
請 ingest_document docs/spec.pdf，完成後 reload_knowledge_base。
```

### `apply_patch(...)` 被拒絕

常見原因：

- 模型讀到的是舊內容，先 `read_file(...)` 重讀目標區段。
- patch context 不夠或不匹配。
- 一次改超過檔案數或行數限制。

把任務拆小，要求模型一次只改一個行為。

### `run_command(...)` 被拒絕

命令不在白名單，或含 shell metacharacter。請模型改用已允許的最小命令，例如：

```text
請改跑 python -m pytest tests/test_x.py，不要使用 &&、|、; 或 shell script。
```

---

## 10. 建議工作節奏

1. `cd <PROJECT_TO_ANALYZE>` 後跑 `aicode`。
2. 第一輪只允許讀取，要求列 file:line 證據。
3. 有 spec 先 `ingest_document(...)` + `reload_knowledge_base()`。
4. 修改前要求模型說明將改哪些檔案與原因。
5. 修改後要求模型跑最小相關驗證。
6. 結束前自己看一次 git diff，確認沒有把 `knowledge.json`、cache、log 或 NDA 衍生資料納入 commit。

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
