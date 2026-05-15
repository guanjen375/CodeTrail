# ai_code - OpenCode + Ollama 本地 MCP 工作台

ai_code 是一個給 OpenCode 使用的本地 MCP 後端。你在 OpenCode TUI 裡提問，模型可以透過 ai_code 讀專案、找程式碼、查已匯入的 spec、分析截圖或 binary、產生 patch，並在允許的白名單內跑驗證命令。

這份 README 只說明 **OpenCode + Ollama + ai_code MCP** 的使用方式。照順序完成設定後，日常操作就是：

```bash
cd <要分析或修改的專案>
aicode
```

ai_code 目前定位是**成熟私有部署版**：適合本機、離線、NDA / firmware / private repo 分析；**不打算公開發布**成 PyPI package、Docker image 或 SaaS。安全邊界有自動測試保護，但未做公開產品級安全審計。

---

## 你會用到什麼

- **OpenCode**：對話式 TUI，負責跟模型互動、顯示工具呼叫。
- **Ollama**：在本機跑主要 coding model、embedding model、reranker 和視覺模型。
- **ai_code MCP server**：把目前專案限制在 `AICODE_ROOT` 沙箱內，提供 17 個 MCP 工具給 OpenCode。
- **`aicode` wrapper**：從目前目錄啟動 OpenCode，並把目前目錄自動設成 `AICODE_ROOT`。

資料流：

```text
OpenCode TUI
  -> Ollama coding model
  -> ai_code MCP server
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

### 1.2 安裝 ai_code Python 依賴

```bash
cd <AICODE_REPO>
pip install -r requirements.txt
pip install mcp pymupdf4llm ollama
```

`<AICODE_REPO>` 是這個 ai_code repo 的路徑，不是你要分析的 firmware repo。

### 1.3 下載模型

先下載預設主模型與 RAG 必要模型：

```bash
ollama pull qwen3-coder:30b
ollama pull bge-m3
ollama pull qllama/bge-reranker-v2-m3
```

建議也把 OpenCode 設定檔列出的候選模型下載好，之後可以直接在 TUI 裡切換：

```bash
ollama pull qwen3.6:35b-a3b-coding-nvfp4
ollama pull devstral:24b
ollama pull gpt-oss:20b
```

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

`PASS` 可以先略過；`FAIL` 要處理。常見問題是 OpenCode 不在 PATH、Ollama 沒啟動、模型還沒 pull、`bin/aicode` 沒有執行權。

---

## 2. 設定 OpenCode

### 2.1 建立 OpenCode config

```bash
mkdir -p ~/.config/opencode
cp examples/opencode.example.json ~/.config/opencode/opencode.json
${EDITOR:-vi} ~/.config/opencode/opencode.json
```

把 `<AICODE_REPO>` 換成 ai_code repo 的實際絕對路徑。完整內容如下：

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
        "qwen3-coder:30b":              { "name": "Qwen3 Coder 30B" },
        "qwen3.6:35b-a3b-coding-nvfp4": { "name": "Qwen3.6 35B Coding NVFP4" },
        "devstral:24b":                 { "name": "Devstral 24B" },
        "gpt-oss:20b":                  { "name": "GPT-OSS 20B" }
      }
    }
  },
  "mcp": {
    "ai_code": {
      "type": "local",
      "command": ["python", "<AICODE_REPO>/mcp_server.py"],
      "enabled": true
    }
  }
}
```

如果系統沒有 `python` 指令，把 `command` 改成：

```json
"command": ["python3", "<AICODE_REPO>/mcp_server.py"]
```

檢查 JSON 格式：

```bash
python -m json.tool ~/.config/opencode/opencode.json >/dev/null
```

### 2.2 安裝 `aicode` 啟動指令

在 ai_code repo 根目錄執行：

```bash
chmod +x bin/aicode
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/bin/aicode" "$HOME/.local/bin/aicode"
```

確認 shell 找得到它：

```bash
which aicode
```

`aicode` 會做三件事：

- 將目前目錄設成 `AICODE_ROOT`
- 拒絕 `AICODE_ROOT=/` 和 `AICODE_ROOT=$HOME`
- 啟動 `opencode`，讓 OpenCode 子行程繼承同一個沙箱根目錄

---

## 3. 啟動專案

切到你要分析或修改的專案，再啟動 OpenCode：

```bash
cd <PROJECT_TO_ANALYZE>
aicode
```

進入 TUI 後先確認：

- 啟動畫面有 `[aicode] AICODE_ROOT=<PROJECT_TO_ANALYZE>`
- `/status` 顯示 `ai_code Connected`
- model selector 裡選的是 Ollama provider 的 coding model
- 第一輪工具呼叫沒有嘗試讀 `$HOME` 或 `/`

如果要讓 MCP server 內部也使用同一顆主模型，可以啟動時帶 `AICODE_MODEL`：

```bash
AICODE_MODEL=qwen3-coder:30b aicode
```

OpenCode 右下角選到的模型負責主要對話與工具決策；`AICODE_MODEL` 影響 ai_code 內部需要直接呼叫 Ollama 的流程。通常兩邊用同一顆比較好排查。

如果要讓 OpenCode 匯入專案外的截圖、PDF、log 或 firmware blob，啟動時明確開啟外部匯入入口：

```bash
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 aicode
```

預設只允許從 `~/Downloads` 和 `/tmp` 匯入。要加其他來源，用 `AI_CODE_IMPORT_ROOTS` 指定：

```bash
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 AI_CODE_IMPORT_ROOTS="$HOME/Downloads:/mnt/share" aicode
```

---

## 4. 第一輪操作方式

### 4.1 先建立專案地圖

第一次接到一個 repo，先要求模型只讀不改：

```text
先不要改檔。
請用 list_dir 看兩層目錄，找出主要 entry point、測試目錄、設定檔。
再用 code_rag_search 找初始化流程、工具呼叫、資料載入相關程式。
最後用 file:line 列出這個 repo 的架構判斷，分成「證據」和「推測」。
```

這會逼模型先走 `list_dir(...)`、`code_rag_search(...)`、`read_file(...)`，避免一開始就憑印象回答。

### 4.2 查 bug 或行為

```text
請追這個錯誤的來源：<貼錯誤訊息>
先用 grep_code / code_rag_search 找可能位置，再讀檔確認。
不要改檔，先列出最可能的 3 個原因與 file:line 證據。
```

如果有 log 檔，先放進專案內，例如 `logs/build_fail.txt`，再問：

```text
請 read_file logs/build_fail.txt，根據錯誤訊息找最可能的實作位置。
```

### 4.3 要它改檔

等模型已經列出證據，再允許 patch：

```text
根據上面的證據，請做最小修改。
套用 patch 前先說會改哪些檔案；套用後跑最小相關測試。
如果 run_command 被白名單拒絕，請列出你原本想跑的命令。
```

`apply_patch(...)` 會真的寫檔。建議在 git worktree 乾淨時使用；不想改檔時要明講「不要 apply_patch」。

### 4.4 查規格或文件

先把 PDF / Markdown / TXT 放進 `<PROJECT_TO_ANALYZE>` 裡，例如：

```text
docs/npu_spec.pdf
```

然後在 OpenCode 裡說：

```text
請用 ingest_document 把 docs/npu_spec.pdf 加進 knowledge base，完成後 reload_knowledge_base。
```

如果文件在專案外，且啟動時已設 `AI_CODE_ALLOW_EXTERNAL_IMPORT=1`，可以先要求：

```text
請 import_external_file ~/Downloads/npu_spec.pdf，然後用回傳的新路徑 ingest_document，完成後 reload_knowledge_base。
```

之後查規格：

```text
請用 query_knowledge 查 conv2d 輸入大小限制，回答時附 REF 來源。
```

如果是「答錯比不答更糟」的數值/規格題（例如 timing、寄存器位寬、預設值），改走嚴格模式：

```text
請用 query_knowledge_strict 查 reset assert 最小持續時間，KB 證據不夠就直接拒答，不要靠常識補。
```

`query_knowledge_strict(...)` 會在 server 端跑兩階段自我檢查（用 `AICODE_MODEL` 那顆模型），定稿才回傳；OpenCode TUI 看不到中間 streaming，但 server stderr 有完整日誌。

### 4.5 看截圖、ELF、firmware

檔案一樣要先放進 `AICODE_ROOT` 內，或先用 `import_external_file(...)` 受控匯入：

```text
screenshots/error.png
firmware/boot.bin
build/output.elf
```

在 OpenCode 裡問：

```text
請 analyze_file screenshots/error.png，先 OCR 錯誤文字，再找相關 source file。
```

```text
請 analyze_file firmware/boot.bin，列出 magic、可讀字串與可能格式。
```

外部檔案範例：

```text
請 import_external_file /tmp/error.png，然後 analyze_file 它回傳的 .aicode_uploads 路徑。
```

---

## 5. ai_code 暴露的 17 個 MCP 工具

你不用手動寫 JSON 或自己呼 API。這些工具會出現在 OpenCode 的工具列表裡；日常用法是在對話中直接要求模型「用某個工具做某件事」。如果你怕模型亂猜，直接點名工具名最有效。

### 5.1 最常用講法

| 你想做什麼 | 在 OpenCode 裡可以這樣說 | 主要工具 |
|---|---|---|
| 先看 repo 長什麼樣 | 請用 `list_dir(path=".", depth=2)` 看專案結構，找 entry point、測試和設定檔。 | `list_dir(...)` |
| 不知道程式在哪 | 請先用 `code_rag_search("初始化流程")` 找相關 symbol，再讀最相關檔案。 | `code_rag_search(...)`、`read_file(...)` |
| 找某個字串或錯誤訊息 | 請用 `grep_code("panic: xxx", path=".", include="*.c,*.h", context=3)` 找位置。 | `grep_code(...)` |
| 讀一個已知檔案 | 請用 `file_info("src/main.py")` 看大小，再用 `read_file("src/main.py", start_line=1, end_line=120)` 讀。 | `file_info(...)`、`read_file(...)` |
| 查已匯入的 spec | 請用 `query_knowledge("reset timing 限制")` 查 KB，回答要附 REF。 | `query_knowledge(...)` |
| 查不能答錯的規格數字 | 請用 `query_knowledge_strict("reset assert 最小時間")`，證據不夠就拒答。 | `query_knowledge_strict(...)` |
| 看專案外的截圖/PDF/log | 請先用 `import_external_file("~/Downloads/error.png")` 匯入，再分析回傳的新路徑。 | `import_external_file(...)` |
| 看圖片、ELF、firmware | 請用 `analyze_file(".aicode_uploads/error.png")` OCR 或分析 binary。 | `analyze_file(...)` |
| 把 PDF/MD/TXT 加進 KB | 請用 `ingest_document("docs/spec.pdf")`，完成後 `reload_knowledge_base()`。 | `ingest_document(...)`、`reload_knowledge_base()` |
| 移除舊文件 | 請用 `remove_document("old_spec.pdf")`，完成後 `reload_knowledge_base()`。 | `remove_document(...)` |
| 準備改檔 | 請先用 `git_status()` 和 `git_diff()` 確認目前變更，再說明要改哪些檔案。 | `git_status(...)`、`git_diff(...)` |
| 套修改 | 請產生最小 unified diff，先用 `apply_patch(diff, dry_run=True)` 預覽，再正式套用。 | `apply_patch(...)` |
| 修改後檢查 | 請用 `run_lint("src/main.py", fix=True)`，再用 `run_command("pytest tests/test_x.py")` 跑最小測試。 | `run_lint(...)`、`run_command(...)` |

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
| 文件/外部檔案 | `ingest_document(path)` | 把 PDF / MD / TXT 匯入 `knowledge.json` |
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

- 找程式碼時，先 `code_rag_search(...)` 或 `grep_code(...)`，再 `read_file(...)`。
- 長檔先 `file_info(...)`，再用 `read_file(path, start_line, end_line)` 分段讀。
- 查 spec 先 `query_knowledge(...)`；數字、限制、預設值這類答錯很糟的題目，用 `query_knowledge_strict(...)`。
- 外部檔案先 `import_external_file(...)`，再用 `analyze_file(...)`、`ingest_document(...)` 或 `read_file(...)` 處理匯入後路徑。
- 新增或刪除文件後一定要 `reload_knowledge_base()`。
- 改檔前先看 `git_status(...)` / `git_diff(...)`；改檔用 `apply_patch(...)`。
- `apply_patch(...)` 和 `run_command(...)` 有副作用；需要改檔或執行專案腳本時才允許。

---

## 6. 文件與知識庫

`knowledge.json` 放在 `AICODE_ROOT` 裡，通常會被 `.gitignore` 忽略。它保存匯入文件切 chunk 後的文字內容，所以不要 commit。

### 6.1 匯入文件

支援：

- `.pdf`
- `.md`
- `.txt`

操作：

```text
請 ingest_document docs/spec.pdf，完成後 reload_knowledge_base。
```

匯入完成後，查詢時要求模型引用 REF：

```text
請 query_knowledge 查 reset timing 的限制，回答每個數字都要附 REF。
```

### 6.2 檔名會影響權重

RAG 會根據檔名推斷來源類型。檔名越清楚，排序越穩：

| 檔名包含 | 來源類型 | 適合內容 |
|---|---|---|
| `spec` / `datasheet` | spec | 規格、限制、硬體行為 |
| `api` / `reference` | api | API 定義、參數、return code |
| `manual` / `handbook` | manual | 使用手冊、操作流程 |
| `guide` / `tutorial` | guide | 教學文件 |
| `faq` | faq | 常見問題 |

例如 `npu_spec.pdf` 通常比 `doc.pdf` 更容易被排在前面。

### 6.3 移除過期文件

```text
請 remove_document old_spec.pdf，完成後 reload_knowledge_base。
```

`remove_document(...)` 用 basename 比對，所以傳 `docs/old_spec.pdf` 或 `old_spec.pdf` 都可以。

---

## 7. 模型比較

下面比較的是這個 repo 的 OpenCode 設定檔已列出的模型，以及 ai_code 內部會用到的 RAG / 視覺模型。

| 模型 | 建議用途 | 優點 | 注意事項 |
|---|---|---|---|
| `qwen3-coder:30b` | 預設主力；讀 repo、改 code、產 patch、跑驗證閉環 | coding 能力和工具使用穩定度最均衡；適合作為日常預設 | 比 20B 模型慢；長工具鏈任務建議拆成「先查證、再修改」 |
| `qwen3.6:35b-a3b-coding-nvfp4` | 跨檔推理、規格 vs 實作比對、較複雜重構 | 推理上限較高；大 context 任務表現較好 | 更吃 VRAM / RAM；不穩或太慢時把 `AICODE_NUM_CTX` 降到 `65536` |
| `devstral:24b` | 快速 code review、找 bug、簡單 patch | 速度和 coding 能力平衡；回答通常直接 | 工具呼叫格式不一定比 Qwen Coder 穩；大型修改前建議切回 Qwen |
| `gpt-oss:20b` | 快速理解陌生 repo、摘要、初步定位 | 輕量、啟動快、硬體壓力低 | 複雜改檔與長工具鏈較弱；適合探索，不適合作為最終 patch 主力 |
| `qwen3-vl:30b-a3b` | `analyze_file(...)` 處理截圖、圖片 OCR、UI error | 讀圖中文字與畫面資訊較好 | 不是主要 coding model；只有需要圖片分析時才必須 pull |
| `bge-m3` | `query_knowledge(...)` / `code_rag_search(...)` 的 embedding | 多語檢索穩定；中文 spec 與英文程式碼混用時有幫助 | 不是聊天模型，不要在 OpenCode model selector 裡選 |
| `qllama/bge-reranker-v2-m3` | RAG rerank | 能改善 spec 查詢排序，降低抓到弱相關 chunk 的機率 | 會增加查詢延遲；模型未 pull 時 RAG 品質會下降或報錯 |

實務選法：

- 要穩定完成「查證 -> patch -> test」：用 `qwen3-coder:30b`。
- 任務跨很多檔、要比對規格或做設計判斷：用 `qwen3.6:35b-a3b-coding-nvfp4`。
- 只想先看懂 repo 或做初步 review：用 `gpt-oss:20b` 或 `devstral:24b`。
- 要讀截圖：保留主聊天模型不變，讓 `analyze_file(...)` 使用 `qwen3-vl:30b-a3b`。

Context 建議：

```bash
AICODE_NUM_CTX=65536 aicode
```

64K 通常比較穩；128K 適合 32GB VRAM + 大 RAM 的機器，但 prompt ingest 和首 token 會變慢。

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
- `import_external_file(...)` 是唯一外部入口，預設關閉；開啟後也只會把允許來源目錄內的檔案複製進 `.aicode_uploads/`。
- `apply_patch(...)` 只能改沙箱內檔案，且 patch context 必須跟現有檔案相符。
- `aicode` 會拒絕把 `/` 或 `$HOME` 當 root。

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

### 8.5 資料飛輪（選用）

設 `AI_CODE_COLLECT_DATA=1` 啟動 `aicode`，KB-shaped 工具（`query_knowledge` / `query_knowledge_strict` / `code_rag_search`）的每次呼叫會 append 一筆到 `data/interactions.jsonl`，含問題、回答（或 `[REFUSED]` / `[SKIPPED_STRICT:...]`）、refs、KB 分數與當下 git commit。預設關閉。

該檔在 NDA 場景必然含敏感片段，已在 §8.4 列入「不要 commit 的資料」。要看統計或匯出訓練語料，跑：

```bash
python data_flywheel.py stats
```

---

## 9. 常見問題

### `/status` 沒看到 `ai_code Connected`

檢查：

```bash
python -m json.tool ~/.config/opencode/opencode.json >/dev/null
which aicode
which opencode
```

再確認 `opencode.json` 裡的 `<AICODE_REPO>/mcp_server.py` 是實際路徑。

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
ollama pull qwen3.6:35b-a3b-coding-nvfp4
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
