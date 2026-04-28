# ai_code — OpenCode 後端工具集

把 ai_code 包成 **MCP server**,接到 OpenCode TUI。在 OpenCode 對話框問問題,
背後 LLM(Devstral / Qwen3-coder / GPT-OSS 等本地 Ollama 模型)會自動呼叫
ai_code 提供的 9 個工具:查 RAG 知識庫、語意搜程式碼、讀檔、grep、改檔、
跑命令、分析圖片/firmware、灌新文件進 KB。

> **部署目標:5090 32GB VRAM + 192GB RAM**,本地全離線推理,適合 NDA / 內部 firmware repo。
> **個人使用工具,沒做完整安全審計**,不建議公開部署。

---

## 系統架構

```
┌──────────────┐  stdio   ┌──────────────────┐  HTTP   ┌─────────┐
│   OpenCode   │ ───────▶ │  mcp_server.py   │ ──────▶ │ Ollama  │
│   TUI (你)   │  ◀────── │   (FastMCP)      │ ◀────── │ (LLM)   │
└──────────────┘  9 tools └──────────────────┘ embed   └─────────┘
                                  │
                                  ▼
                  ┌─────────────────────────────────┐
                  │  AICODE_ROOT(NDA 沙箱)         │
                  │  - 程式碼(read_file/grep)     │
                  │  - knowledge.json(RAG 知識庫)  │
                  │  - firmware.bin / .elf          │
                  │  - 螢幕截圖 / 規格 PDF          │
                  └─────────────────────────────────┘
```

OpenCode 是前端,模型自選工具,所有檔案操作都被沙箱在 `AICODE_ROOT` 內。

`main.py` CLI 模式(`--qa` / `--agent` / `--mcp` 遠端 SSH)仍保留可用,但
**這份文件只講 OpenCode + MCP 的玩法**。CLI 用法看 git log 與 `python main.py --help`。

---

## 1. 安裝

### 軟體
- **Python 3.10+**
- **Ollama**(<https://ollama.com/download>),裝完會自動背景常駐
- **Node.js LTS**(<https://nodejs.org/>),為了 npm 裝 OpenCode
- **OpenCode**:
  ```powershell
  npm install -g opencode-ai
  ```

### Python 套件
```powershell
cd <AICODE_REPO>
pip install -r requirements.txt
pip install pymupdf4llm ollama   # RAG / KB ingestion 才需要
```

### Ollama 模型
推薦組合(5090 上跑得很順):
```powershell
# 主力 LLM(挑一個)
ollama pull qwen3-coder:30b
ollama pull devstral:24b
ollama pull gpt-oss:20b

# RAG 用(必要)
ollama pull bge-m3
ollama pull qllama/bge-reranker-v2-m3

# 圖片 OCR 用(若你會用 analyze_file 分析截圖)
ollama pull qwen3-vl:30b-a3b
```

> 模型 tool calling 表現各有差異:Devstral 有 OpenHands fine-tune 不一定發
> 標準 tool_calls;Qwen3-coder 第一次 call 對、第二次容易丟 XML 文字;
> GPT-OSS 偶爾呼叫不存在的 `multi_tool_use.parallel`。
> **5090 速度上來後 context 不漂移,大多模型都能用**。先試 GPT-OSS 20B 或
> Mistral-Small3.2 24B。

---

## 2. 設定

### `mcp_server.py` 啟動環境
**`AICODE_ROOT` 是必填**,沒設 server 直接拒絕啟動(避免誤掃 cwd 或洩漏 NDA 內容):

```powershell
$env:AICODE_ROOT = "<PATH_TO_PROJECT_TO_ANALYZE>"
```

`AICODE_ROOT` 應該指向**你要分析的專案**(firmware repo / 你的 codebase),
而不是 ai_code 自己。

### OpenCode `opencode.json`

```powershell
notepad "$HOME\.config\opencode\opencode.json"
```

整個檔案內容:

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
        "qwen3-coder:30b": { "name": "Qwen3 Coder 30B" },
        "devstral:24b":    { "name": "Devstral 24B" },
        "gpt-oss:latest":  { "name": "GPT-OSS 20B" }
      }
    }
  },
  "mcp": {
    "ai_code": {
      "type": "local",
      "command": ["python", "<AICODE_REPO>/mcp_server.py"],
      "environment": {
        "AICODE_ROOT": "<PATH_TO_PROJECT_TO_ANALYZE>"
      },
      "enabled": true
    }
  }
}
```

把兩個 `<...>` placeholder 換成實際路徑。JSON 路徑用 **正斜線 `/`**,Windows 接受。

驗證 JSON:
```powershell
Get-Content "$HOME\.config\opencode\opencode.json" -Raw | ConvertFrom-Json
```

---

## 3. 啟動

```powershell
cd <PATH_TO_PROJECT_TO_ANALYZE>
opencode
```

進 TUI 後:
- 左下顯示 `⊙ 1 MCP /status` 綠燈 = ai_code MCP 接上
- `Ctrl+P` → `model` → 選你要用的 Ollama 模型
- 右下會顯示 `Qwen3 Coder 30B · Ollama`(或你選的)
- `/status` 可以確認 `ai_code Connected`

第一次跑問問題會等模型 load 進 VRAM(5090 上 5~10 秒;若 VRAM 不夠會 offload 到 RAM,首 token 延遲明顯增加)。

---

## 4. 暴露的 9 個工具

| Tool | 用途 |
|---|---|
| `query_knowledge(question)` | 查 RAG 知識庫(PDF/spec/manual),回 refs + 引用文字 |
| `code_rag_search(query, top_k=5)` | 依語意找程式碼位置(file:line + symbol) |
| `read_file(path, max_chars=50000)` | 讀檔(沙箱內,帶行號) |
| `grep_code(pattern, path=".")` | grep / ripgrep 搜程式碼 |
| `apply_patch(diff)` | 套 unified diff,**直接寫檔** |
| `run_command(cmd)` | 跑白名單命令(test / lint / build) |
| `analyze_file(path)` | 分析非文字檔:圖片 OCR、ELF 解析、binary 字串提取 |
| `ingest_document(path)` | 把 PDF / MD / TXT 灌進 knowledge.json |
| `reload_knowledge_base()` | 重載 KB singleton(灌完 KB 後呼叫才會生效) |

### 沙箱
- 所有檔案操作鎖在 `AICODE_ROOT` 內
- `set_sandbox_root(AICODE_ROOT, allow_external=False)` —— root 之外讀不到
- `apply_patch` 的 hunk context 必須與檔案實際內容相符,不符整個 hunk 拒絕
- `run_command` 走 `config.ALLOWED_COMMANDS` 白名單,`shell=False` + `shlex.split`

### `run_command` 白名單(啟動時 mcp_server.py 已自動加 build 系列)
- 測試:`pytest` / `ctest` / `npm test` / `cargo test` / `go test`
- 靜態:`mypy` / `tsc` / `ruff` / `black` / `isort` / `eslint` / `clang-format`
- 建置:`make` / `cmake` / `cmake --build` / `ninja` / `meson` / `bazel build`

要再加要編 `mcp_server.py` 的 `_EXTRA_BUILD_COMMANDS` 或 `config.py` 的 `ALLOWED_COMMANDS`。

---

## 5. 用法 — 對話範例

### 5.1 找程式碼位置
```
幫我找 conv2d 的 padding 怎麼算
```
模型會自動連發:
- ⚙ `code_rag_search(query="conv2d padding")` → 找到 src/ops/conv2d.c:142
- ⚙ `read_file(path="src/ops/conv2d.c", max_chars=...)` → 讀內容
- 中文總結

### 5.2 查 spec 文件
**前提:`knowledge.json` 已灌過 spec PDF**(見 §6)
```
這顆 NPU 的 conv2d 最大輸入大小是多少
```
- ⚙ `query_knowledge(question="conv2d 輸入上限")` → 從 PDF 抓 §A.2
- 模型用「根據 REF1 (xxx-spec.pdf p.45)...」格式引用

### 5.3 對比規格 vs 實作
```
看 conv2d 在規格書跟我們實作有什麼差
```
這就是 `example.png` 那張圖。模型會:
- ⚙ `query_knowledge` → 規格說 NHWC layout
- ⚙ `code_rag_search` → 找實作位置
- ⚙ `read_file` → 看實作
- 比對寫出差異

### 5.4 改 bug
```
ai_code/utils.py 的 should_ignore_dir 對大寫資料夾名沒 normalize,
幫我改成 case-insensitive
```
- ⚙ `read_file(path="utils.py")`
- 模型生成 unified diff
- ⚙ `apply_patch(diff=...)` ← 真的寫檔
- ⚙ `run_command(cmd="pytest tests/test_utils.py")` ← 跑測試

### 5.5 分析韌體
```
analyze firmware/boot.bin
```
- ⚙ `analyze_file(path="firmware/boot.bin")` → hex dump + magic + 字串
- 模型告訴你 magic header / 主要字串 / 推測格式

### 5.6 看截圖
複製 compile error 截圖到 `<AICODE_ROOT>/error.png`,然後:
```
analyze error.png 那是什麼錯誤
```
- ⚙ `analyze_file(path="error.png")` → VL 模型 OCR
- 模型解釋錯誤原因 + 建議修法

> **沙箱限制**:檔案必須在 AICODE_ROOT 內。`AICODE_ROOT/screenshots/` 之類的
> 子目錄是慣用做法。

---

## 6. RAG 知識庫(讓 `query_knowledge` 有東西可查)

### 6.1 從 OpenCode 內灌 PDF / MD / TXT(推薦)

把 PDF 放進 `<AICODE_ROOT>` 任何位置,在對話框打:
```
幫我把 <YOUR_SPEC>.pdf 灌進知識庫,完了重新載入
```

模型會:
1. ⚙ `ingest_document(path="<YOUR_SPEC>.pdf")` ← subprocess 跑 RAG.py,2~5 分鐘
2. ⚙ `reload_knowledge_base()` ← KB singleton 重載
3. 回報新的 chunk 數

之後 `query_knowledge` 立即看得到新內容。

> 模型若不會自己連兩個 call,分兩句話講:
> ```
> ingest <YOUR_SPEC>.pdf
> ```
> ```
> reload kb
> ```

### 6.2 互動式來源(截圖 / 圖片 / 網頁)— 必須 CLI 跑

`--chat` / `--image` / `--url` 模式要 Y/n 互動,沒辦法走 MCP。手動:

```powershell
cd <AICODE_REPO>
python RAG.py teams_chat.png      <AICODE_ROOT>/knowledge.json --chat
python RAG.py memory_map.png      <AICODE_ROOT>/knowledge.json --image
python RAG.py https://docs.x/api  <AICODE_ROOT>/knowledge.json --url
```

跑完回 OpenCode 打:
```
reload knowledge base
```

### 6.3 文件分類自動權重(取個好檔名)

RAG.py 看檔名決定文件類型,影響 query_knowledge 排序:

| 檔名含 | 類型 | 權重 |
|---|---|---|
| `*_spec*` / `*datasheet*` | spec | **1.30**(最高) |
| `*_api*` / `*reference*` | api | 1.25 |
| `*manual*` / `*handbook*` | manual | 1.20 |
| `*guide*` / `*tutorial*` | guide | 1.0 |
| `*faq*` | faq | 1.0 |

**檔名取貼切,query_knowledge 才會優先撈那份**。`xxx-spec.pdf` 比 `xxx.pdf` 好。

### 6.4 確認 KB 狀態

CLI:
```powershell
python -c "from knowledge import KnowledgeBase; kb = KnowledgeBase('<AICODE_ROOT>/knowledge.json'); print(kb.get_status())"
```

或在 OpenCode 內:
```
reload knowledge base
```
回應會顯示 `已載入 N 個 chunks`。

### 6.5 推薦灌庫順序(NDA firmware 場景)

1. **Spec / datasheet**(權重 1.30)— 一定要,查詢主力靠這個
2. **API reference / Manual**(權重 1.20~1.25)
3. **Guide / Tutorial / FAQ**(1.0)
4. **(可選)團隊 Teams/Slack 討論截圖** — 用 `--chat` 互動式入庫,只挑有結論的對話

---

## 7. 沒接 OpenCode 也想跑 — Inspector 單測

```powershell
$env:AICODE_ROOT = "<PATH_TO_PROJECT_TO_ANALYZE>"
npx @modelcontextprotocol/inspector python <AICODE_REPO>/mcp_server.py
```

開瀏覽器 `http://127.0.0.1:6274`,**Tools** 分頁逐個工具帶參數測。
連線設定:
- Transport: STDIO
- Command: `python`
- Arguments: `<AICODE_REPO>/mcp_server.py`
- Environment Variables: `AICODE_ROOT = <PATH>`

按 Connect。`apply_patch` 跳過(會真寫檔)。

---

## 8. 沒接 MCP 也想用 — `main.py` CLI 模式仍可用

`mcp_server.py` 完全獨立,跟 `main.py` 不衝突:

```powershell
# QA 模式(不掃專案)
python main.py --qa "解釋這個 compile error"

# 一般模式(掃完整專案,自動選 full / agent)
python main.py <AICODE_ROOT>

# 接知識庫問答
python main.py <AICODE_ROOT> --kb <AICODE_ROOT>/knowledge.json

# 啟用改碼閉環
python main.py <AICODE_ROOT> --patch --run-tests

# 遠端 SSH(透過 SSH 按需讀遠端檔案)
python main.py --mcp user@host "問題"

# 評測回歸
python eval/run_eval.py
```

---

## 9. 安全 / NDA 注意

### 不會 commit 的衍生物(`.gitignore` 已涵蓋)
- `knowledge.json` / `knowledge*.json` — RAG 知識庫(含 PDF chunks 全文)
- `.code_rag_cache_*` — Code RAG 索引快取
- `.rag_embedding_cache.json` — RAG 建索引時的 embedding 快取
- `.opencode/` — OpenCode session 紀錄
- `data/` / `*.jsonl` — 資料飛輪紀錄

### NDA 環境部署 checklist
- [ ] 5090 機器確認**不會自動 sync 到 OneDrive / git remote / 任何雲端**
- [ ] `AICODE_ROOT` 指向真實 NDA 專案,不是測試目錄
- [ ] `opencode.json` 路徑改成那台機器的實際路徑
- [ ] 灌完 RAG 後 `git status` 確認 `knowledge.json` 顯示 ignored
- [ ] `apply_patch` 寫檔前確認 NDA repo 在 git 控管下,出事可 `git checkout -- .`

### 風險點
- `apply_patch` 沒有 dry-run 介面,直接寫
- `run_command` 的 `make` / `npm install` 等會執行專案內腳本,**不要對不信任 repo 用**
- `analyze_file` 對 firmware blob 做 OCR/字串提取會把內容塞進 LLM context — 確認模型也是本地的(Ollama)不會外送

### 已驗證的 ai_code 程式碼掃描結果
- 0 個 hardcoded API key / token / password
- 0 個 NDA 客戶名 / 產品名 / 規格書檔名
- 9 個 MCP 工具皆走 sandbox `_safe_path` 驗證

---

## 10. 疑難排解

| 症狀 | 解法 |
|---|---|
| `[FATAL] 未設定 AICODE_ROOT` | 先 `$env:AICODE_ROOT = "<path>"` 再啟 OpenCode |
| OpenCode 看不到 ai_code 工具 | `opencode.json` JSON 格式或路徑寫錯,重新驗證 |
| `model 'xxx' not found` | `ollama list` 確認模型 tag,改 config 對齊 |
| 模型回 `multi_tool_use.parallel invalid` | gpt-oss 的 quirk,把問題拆成單步問,或換 Mistral-Small |
| `ingest_document` 超時 | 大型 PDF(>500 頁)請改 CLI:`python RAG.py xxx.pdf knowledge.json` |
| `query_knowledge` 永遠回 `not loaded` | 確認 `<AICODE_ROOT>/knowledge.json` 存在,server 啟動 log 應該印 `已載入 N chunks` |
| `analyze_file` 對截圖 OCR 失敗 | 檢查 `ollama pull qwen3-vl:30b-a3b` 跑過、`config.VL_MODEL` 對得上 |
| 模型亂選 `run_command` 跑 `dir` | 模型抽風,prompt 寫具體點(指定工具名) |
| VRAM 不夠跑 30B 很慢(部分 offload 到 CPU) | 預期行為;目標部署是 5090(32GB VRAM),小卡只當測試 |
