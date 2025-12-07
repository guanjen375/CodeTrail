# 智能程式碼分析器

基於 Ollama 的本地程式碼分析工具，支援 RAG（知識庫檢索）和 Agent 模式。

**版本：v0.2.0** - 穩定性與除錯體驗強化版

## v0.2.0 版本亮點

- **Stack trace 快路徑**：BUG 類問題自動解析 `file:line`，直接定位錯誤來源，跳過多輪工具迴圈
- **Ollama 健康檢查**：eval 前自動檢測 Ollama 狀態，異常時可選擇自動重啟
- **工具使用上限**：預設 `MAX_READ_FILE_CALLS=15`、`MAX_GREP_CALLS=10`，避免 Agent 無限制讀檔
- **規格文件 RAG**：新增 `docs/spec.md` + `knowledge.json`，整理 NUM_CTX 設定建議、OOM 排解等規格
- **評測 100% 通過**：14 題 regression 測試集（SPEC/CODE/BUG）全數通過，平均分數 0.86

## 功能特色

- **完整模式**：小型專案（< 200KB）一次讀入全部程式碼分析
- **Agent 模式**：大型專案動態探索，按需讀取檔案
- **網頁模式**：直接分析 GitHub/GitLab/Bitbucket 上的程式碼（測試中）
- **知識庫 RAG**：整合技術文件（PDF/Markdown），回答時引用文件來源
- **Code RAG**：自動索引程式碼符號（函式/類別），支援 AST/Tree-sitter 解析 + Reranker 二次排序
- **圖片 OCR**：支援截圖中的錯誤訊息辨識（使用 VL 模型）
- **二進位分析**：韌體/執行檔的 Hex dump + 智能字串提取
- **嚴格模式（兩階段）**：規格類問題強制引用文件，並透過自我檢查過濾幻覺
- **改碼閉環**：apply_patch（含 context 驗證）、git_status、git_diff、run_lint（自動執行）
- **容器化執行**：在 Docker/Podman 中安全執行測試命令
- **資料飛輪**：收集互動記錄（含 tool_calls/files_read）用於後續 fine-tuning
- **動態 Context**：根據 prompt 長度自動調整 num_ctx，減少延遲

## 安裝需求

### 1. 安裝 Ollama

前往 https://ollama.ai/download 下載安裝。

### 2. 拉取模型

```bash
ollama pull qwen3-coder:30b              # 主模型
ollama pull qwen3-vl:30b-a3b             # VL 模型（圖片辨識用）
ollama pull bge-m3                       # Embedding 模型
ollama pull qllama/bge-reranker-v2-m3    # Reranker 模型
```

### 3. 安裝 Python 依賴

```bash
# 核心依賴
pip install -r requirements.txt

# 可選依賴（Tree-sitter 多語言解析、RAG 建立工具）
pip install -r requirements-extra.txt
```

**核心依賴**（`requirements.txt`）：
- `requests`：HTTP 請求
- `numpy`：向量運算加速

**可選依賴**（`requirements-extra.txt`）：
- `tree-sitter` + 語言套件：提升多語言 AST 解析精準度
- `pymupdf4llm` + `ollama`：從 PDF 建立知識庫

## 快速開始

### 推薦用法

| 情境 | 指令 | 說明 |
|------|------|------|
| 日常問答 | `python main.py .` | 最基本用法，自動選擇模式 |
| 有規格文件 | `python main.py . --kb=docs.json` | 搭配知識庫，回答會引用文件 |
| 需要 AI 改 code | `python main.py . --patch` | 啟用 apply_patch + git 工具 |
| Debug + 跑測試 | `python main.py . --patch --run-tests` | 完整改碼閉環，可執行測試 |
| 分析外部專案 | `python main.py /path --run-tests --container` | 在容器中安全執行（推薦） |
| 分析 GitHub repo | `python main.py --web https://github.com/user/repo` | 直接分析線上 repo |
| 收集訓練資料 | `AI_CODE_COLLECT_DATA=1 python main.py .` | 啟用資料飛輪，記錄互動 |

### 模式介紹

本工具有三種主要運作模式：

#### 1. 完整模式（Full Mode）
- **適用**：小型專案（< 200KB 程式碼）
- **原理**：一次讀入所有程式碼，整體分析
- **優點**：回答最完整，不會遺漏關聯
- **缺點**：大專案會超出 context 限制
- **強制使用**：`python main.py . --full`

#### 2. Agent 模式
- **適用**：大型專案、需要精準定位的問題
- **原理**：動態探索，按需讀取檔案（list_files → grep → read_file）
- **優點**：可處理任意大小專案，精準定位問題
- **缺點**：多輪工具呼叫較慢
- **強制使用**：`python main.py . --agent`
- **搭配功能**：
  - `--run-tests`：允許執行測試命令（pytest、cargo test 等）
  - `--patch`：允許修改程式碼（apply_patch + auto lint）
  - `--container`：在容器中執行（安全隔離）

#### 3. 網頁模式（測試中）
- **適用**：分析 GitHub/GitLab/Bitbucket 上的公開 repo
- **原理**：下載到暫存目錄後分析
- **使用**：`python main.py --web https://github.com/user/repo`

### 基本用法

```bash
# 分析當前目錄
python main.py .

# 分析指定目錄
python main.py /path/to/project

# 帶問題的單次分析
python main.py /path/to/project "這個專案的主要功能是什麼？"
```

### 模式選擇

```bash
# 強制使用 Agent 模式（大型專案推薦）
python main.py /path/to/project --agent

# 強制使用完整模式（小型專案）
python main.py /path/to/project --full
```

### 知識庫

```bash
# 使用自訂知識庫
python main.py /path/to/project --kb=/path/to/knowledge.json
```

### 排除/包含目錄

```bash
# 排除特定檔案
python main.py . --exclude="*.test.py"

# 包含預設排除的目錄（如 third_party）
python main.py . --include-dir=third_party
```

### 外部檔案（圖片、bin）

```bash
# 允許讀取專案目錄外的圖片和 bin 檔案
python main.py /path/to/project --allow-external
```

### 執行測試（run_command）

```bash
# 啟用 run_command 工具，允許 Agent 執行測試命令
python main.py /path/to/project --run-tests

# 也可以用環境變數啟用
AI_CODE_RUN_TESTS=1 python main.py /path/to/project
```

**支援的測試命令**（白名單）：
- Python: `pytest`, `python -m pytest`, `python -m unittest`
- C/C++: `ctest`
- Node.js: `npm test`, `npm run test`, `yarn test`
- Rust: `cargo test`
- Go: `go test`

**安全警告**：`run_command` 會執行專案內的測試腳本，對不信任的專案有安全風險。預設關閉，需明確啟用。

### 改碼閉環（Patch 模式）

```bash
# 啟用改碼工具（apply_patch、git_status、git_diff、run_lint）
python main.py /path/to/project --patch

# 也可以用環境變數啟用
AI_CODE_PATCH=1 python main.py /path/to/project
```

**新增工具**：
- `apply_patch`：套用 unified diff 格式的修改
- `git_status`：查看 Git 狀態
- `git_diff`：查看變更內容
- `run_lint`：執行 lint/format 工具

**支援的 Lint 工具**（自動偵測）：
- Python: `ruff check --fix`, `black`, `isort`
- JavaScript/TypeScript: `eslint --fix`, `prettier --write`
- Go: `gofmt -w`, `go vet`
- Rust: `rustfmt`, `cargo clippy --fix`
- C/C++: `clang-format -i`

**安全限制**：
- 每次最多修改 5 個檔案
- 每個檔案最多修改 200 行
- 需明確啟用

### 容器化執行

```bash
# 在 Docker/Podman 容器中執行測試（更安全）
python main.py /path/to/project --container

# 也可以用環境變數啟用
AI_CODE_USE_CONTAINER=1 python main.py /path/to/project

# 結合 --run-tests 使用
python main.py /path/to/project --run-tests --container
```

**容器安全設定**：
- 網路：預設停用（`--network none`）
- 檔案系統：專案目錄唯讀掛載
- 資源限制：CPU/記憶體上限
- 無特權執行

**環境變數**：
- `AI_CODE_CONTAINER_ENGINE`：指定容器引擎（`docker`/`podman`/`auto`）
- `AI_CODE_CONTAINER_IMAGE`：自訂映像檔
- `AI_CODE_CONTAINER_MEMORY`：記憶體限制（預設 `2g`）
- `AI_CODE_CONTAINER_CPU`：CPU 限制（預設 `2`）
- `AI_CODE_CONTAINER_TIMEOUT`：超時秒數（預設 `120`）

**容器化執行器 CLI**：

```bash
# 檢查容器環境
python container_runner.py check

# 在容器中執行命令
python container_runner.py run /path/to/project "pytest -v"

# 在容器中執行測試（自動偵測）
python container_runner.py test /path/to/project

# 預拉取所有映像檔
python container_runner.py pull --all
```

### 網頁模式（測試中）

> **注意**：此功能目前處於測試階段，僅支援 Git 平台的公開 repo。

```bash
# 分析 GitHub repo
python main.py --web https://github.com/user/repo

# 分析特定分支
python main.py --web https://github.com/user/repo/tree/develop

# 分析特定目錄
python main.py --web https://github.com/user/repo/tree/main/src

# 分析單一檔案
python main.py --web https://github.com/user/repo/blob/main/file.py

# 帶問題的單次模式
python main.py --web https://github.com/user/repo "這個專案的架構是什麼？"

# 搭配其他參數
python main.py --web https://github.com/user/repo --agent --kb=docs.json
```

**支援的平台**：
- GitHub: `https://github.com/user/repo`
- GitLab: `https://gitlab.com/user/repo`
- Bitbucket: `https://bitbucket.org/user/repo`

**限制**：
- 僅支援公開 repo（不支援私有 repo 或需要認證的 URL）
- 大型 repo 下載較慢，建議指定子目錄
- 程式結束後暫存目錄會自動清理

## 互動模式

啟動後進入互動模式：

```
💬 輸入問題 (Enter=整體分析, q=離開, clear=清除歷史)
>>> 這個函式 foo() 做了什麼？
```

### 支援的問題類型

- **程式碼理解**：「這個類別的用途是什麼？」
- **Bug 分析**：「這個錯誤是怎麼發生的？」（支援貼上 stack trace）
- **規格查詢**：「這個 API 的最大值限制是多少？」（需要知識庫）
- **圖片問題**：「img:/path/to/screenshot.png 這個錯誤怎麼解？」
- **二進位分析**：「bin:/path/to/firmware.bin 這個檔案的結構是什麼？」

## 圖片與二進位檔案

### 圖片 OCR（使用 VL 模型）

```bash
# 在問題中加入 img: 前綴
>>> img:/path/to/error_screenshot.png 這個錯誤怎麼解？
>>> img:~/Desktop/log.png 幫我分析這個 log

# 支援格式：.png, .jpg, .jpeg, .gif, .webp
# 大小限制：20MB
```

### 二進位檔案分析

```bash
# 在問題中加入 bin: 前綴
>>> bin:/path/to/firmware.bin 這個韌體的版本號是什麼？
>>> bin:./u-boot.bin 幫我分析這個 U-Boot

# 支援格式：.bin, .dat, .raw, .fw, .img, .rom, .hex
# 大小限制：50MB
```

**分析內容**：
- **Hex dump**：前 1KB，用於識別檔案格式和 magic number
- **智能字串提取**：使用 `strings` 提取整個檔案的可讀字串
- **優先分類**：版本號、編譯日期等重要資訊會優先顯示

### 讀取專案目錄外的檔案

預設情況下，圖片和 bin 檔案必須在專案目錄內。如需分析外部路徑的檔案，使用 `--allow-external` 參數：

```bash
# 允許讀取任意路徑的圖片和 bin 檔案
python main.py /path/to/project --allow-external

# 範例：分析專案目錄外的截圖
python main.py ./my_project --allow-external
>>> img:/home/user/screenshots/error.png 這個錯誤怎麼解？

# 範例：分析專案目錄外的 U-Boot
python main.py ./my_project --kb=knowledge.json --allow-external
>>> bin:/home/user/u-boot/u-boot.bin 這個 U-Boot 的版本是？
```

**安全說明**：
- 此選項影響圖片（`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`）和 bin 檔案（`.bin`, `.dat`, `.raw`, `.fw`, `.img`, `.rom`, `.hex`）
- 預設關閉，需明確啟用

## 知識庫格式

`knowledge.json` 格式：

```json
{
  "metadata": {
    "documents": [
      {"name": "API_Manual.pdf", "type": "spec"},
      {"name": "FAQ.md", "type": "guide"}
    ]
  },
  "chunks": [
    {
      "content": "文件內容片段...",
      "source": "API_Manual.pdf",
      "page": 15,
      "type": "spec",
      "section": "3.2 限制條件",
      "embedding": [0.1, 0.2, ...]
    }
  ]
}
```

type 類型：
- `spec`：規格文件（優先級最高）
- `guide`：使用指南
- `warning`：警告/限制條件
- `faq`：常見問題

### 建立知識庫

```bash
python RAG.py /path/to/document.pdf /path/to/output.json
```

## 嚴格模式（兩階段自我檢查）

當問題涉及規格/文件類內容時，系統會自動啟用嚴格模式：

**第一階段 - 生成初稿**：
- 根據程式碼與 `[REF]` 參考資料生成答案
- Temperature = 0.0（完全確定性）
- 要求每個論述標註 REF 編號

**第二階段 - 自我檢查**：
- 逐句審核初稿，確認是否有 REF 根據
- 有明確對應 → 保留並標註
- 合理推論但沒明說 → 改成「推測：...」
- 完全沒根據 → 刪除或標示「文件未提及」

觸發條件：問題包含 `規格`、`spec`、`manual`、`根據文件` 等關鍵字。

## Code RAG（AST/Tree-sitter）

程式碼索引使用多層解析策略：

1. **Python AST**：使用內建 `ast` 模組解析 Python 檔案
2. **Tree-sitter**：解析 JavaScript/TypeScript/C/C++/Go/Rust
3. **Regex 備援**：當 AST 解析失敗時使用正規表達式

**新增功能**：
- 符號包含 `end_line`（結束行號）和 `parent`（父符號）
- 支援巢狀符號（類別內的方法）
- 更精準的符號定位

**檢查解析器狀態**：

```python
from ast_parser import get_parser_status
print(get_parser_status())
# {'python_ast': True, 'tree_sitter': True, 'tree_sitter_languages': ['javascript', 'typescript', ...]}
```

## 資料飛輪（Fine-tuning 資料收集）

收集互動記錄用於後續模型微調：

```bash
# 啟用資料收集
AI_CODE_COLLECT_DATA=1 python main.py /path/to/project

# 資料儲存位置（預設）
# data/interactions.jsonl

# 長期啟用（加入 ~/.bashrc）
export AI_CODE_COLLECT_DATA=1
```

**使用建議**：

啟用後，問題品質 = 資料品質。建議多問專業問題：

- **高價值**：程式碼問題（debug、實作、架構）、技術判斷、領域知識
- **低價值**：閒聊、基礎問題、模糊問題

**Debug 用途**：

記錄檔可用於事後分析，透過 `timestamp` 欄位判斷同一次對話的前後文。

**資料格式**：
```json
{
  "timestamp": "2024-01-01T12:00:00",
  "question": "...",
  "question_type": "spec|code|bug|general",
  "refs": [{"source": "...", "score": 0.5, "content": "..."}],
  "code_snippets": [{"path": "...", "line": 123, "symbol": "..."}],
  "answer": "...",
  "rating": null,
  "metadata": {"mode": "agent", "kb_top_score": 0.5}
}
```

**資料飛輪 CLI**：

```bash
# 手動評分互動記錄
python data_flywheel.py rate --file data/interactions.jsonl

# 顯示資料統計
python data_flywheel.py stats

# 匯出訓練資料
python data_flywheel.py export --output data/training.jsonl --min-rating 0
```

**環境變數**：
- `AI_CODE_COLLECT_DATA`：啟用資料收集（`1`/`true`/`yes`）
- `AI_CODE_DATA_FILE`：資料檔案路徑（預設 `data/interactions.jsonl`）

## 評測機制（Regression Harness）

量化評估系統回答品質：

```bash
# 執行評測
python eval/run_eval.py --project /path/to/project --kb knowledge.json

# 指定測試集
python eval/run_eval.py --test-set spec --project . --kb kb.json

# 詳細輸出
python eval/run_eval.py --project . --verbose
```

**測試集類型**：
- `spec`：規格類問題（需要知識庫支援）
- `code`：程式碼定位問題
- `bug`：Bug 分析問題
- `all`：執行所有測試

**評分標準**：
- Spec 問題：檢查是否包含預期關鍵字和引用來源
- Code 問題：檢查是否定位到正確檔案和符號
- Bug 問題：檢查是否識別問題原因並提供修復建議

**測試案例格式**（`eval/spec_questions.json`）：
```json
[
  {
    "id": "spec_001",
    "question": "API 的最大請求頻率是多少？",
    "expected": {
      "keywords": ["rate limit", "每秒", "次"],
      "ref_required": true
    },
    "context": "."
  }
]
```

## 設定檔

編輯 `config.py` 調整參數：

```python
# 模型設定
MODEL = "qwen3-coder:30b"              # 主模型
VL_MODEL = "qwen3-vl:30b-a3b"          # VL 模型（圖片辨識）
EMBEDDING_MODEL = "bge-m3"             # Embedding 模型
RERANKER_MODEL = "qllama/bge-reranker-v2-m3"  # Reranker 模型

# Context 長度設定（重要！根據 GPU VRAM 調整）
NUM_CTX = 131072                       # Context 長度（128K，會自動 offload 到 RAM）
NUM_CTX_FULL_MODE = NUM_CTX            # Full 模式 Context（與 NUM_CTX 相同）

# 知識庫 RAG 設定
KNOWLEDGE_THRESHOLD = 0.25             # 相關度門檻（短問題用 0.20）
KNOWLEDGE_CONTENT_MAX_CHARS = 2000     # 每個 chunk 最大字元數
DYNAMIC_TOP_K_MIN = 3                  # 高相關度時返回數量
DYNAMIC_TOP_K_MAX = 6                  # 低相關度時返回數量

# 程式碼 RAG 設定
CODE_RAG_THRESHOLD = 0.30              # 程式碼相關度門檻
CODE_RAG_TOP_K = 8                     # 返回的程式碼片段數

# 嚴格模式
STRICT_MODE = True                     # 啟用嚴格模式
STRICT_MODE_TEMPERATURE = 0.0          # 嚴格模式溫度
WEAK_REF_THRESHOLD = 0.30              # REF 太弱時拒答

# 改碼閉環設定
PATCH_ENABLED = False                  # 預設關閉，用 --patch 啟用
PATCH_MAX_FILES = 5                    # 每次最多修改的檔案數
PATCH_MAX_LINES_PER_FILE = 200         # 每個檔案最多修改的行數
```

### Context 長度與 VRAM/RAM 建議

`NUM_CTX` 會影響 KV cache 記憶體用量。當 VRAM 不足時，Ollama 會自動將部分 KV cache offload 到系統 RAM：

| 配置 | 建議 NUM_CTX | 說明 |
|------|-------------|------|
| 24GB VRAM | 32768 (32K) | 保守設定，避免 OOM |
| 32GB VRAM | 65536 (64K) | 純 GPU，速度最快 |
| 32GB VRAM + 大 RAM | 131072 (128K) | 預設值，部分 offload 到 RAM |
| 48GB+ VRAM | 131072+ | 可開更大 |

**預設配置**（針對 32GB VRAM + 大 RAM）：
- `NUM_CTX = 131072`（128K）
- Full 模式與 Agent 模式使用相同 context 長度

**檢查 offload 狀態**：
```bash
ollama ps  # 查看 GPU% 比例，低於 100% 表示有 offload
```

**注意**：Offload 到 RAM 會降低推理速度（主要影響首 token 延遲），但能避免 OOM 並處理更長 context。如果速度太慢，可調低 NUM_CTX。

## 檔案結構

```
ai_code/
├── main.py              # 主程式入口
├── config.py            # 設定檔
├── utils.py             # 共用工具（LLM 呼叫、檔案掃描、嚴格模式）
├── agent.py             # Agent 模式（動態探索 + run_command + patch 工具）
├── context.py           # 完整模式（全量分析）
├── knowledge.py         # 知識庫 RAG（Reranker + MMR）
├── code_rag.py          # 程式碼索引 RAG（AST/Tree-sitter + Embedding）
├── ast_parser.py        # AST 解析器（Python AST + Tree-sitter + Regex）
├── media.py             # 媒體處理（圖片 OCR、二進位分析）
├── web.py               # 網頁模式（Git URL 下載，測試中）
├── http_client.py       # HTTP 連線池管理
├── container_runner.py  # 容器化執行器（Docker/Podman）
├── data_flywheel.py     # 資料飛輪收集器
├── RAG.py               # 知識庫建立工具（獨立腳本）
├── knowledge.json       # 知識庫（自行建立）
├── data/                # 資料目錄
│   └── interactions.jsonl  # 互動記錄
└── eval/                # 評測模組
    ├── run_eval.py      # 評測執行器
    ├── spec_questions.json   # 規格類測試案例
    ├── code_questions.json   # 程式碼類測試案例
    └── bug_questions.json    # Bug 類測試案例
```

## 環境變數總覽

| 變數 | 說明 | 預設值 |
|------|------|--------|
| `AI_CODE_RUN_TESTS` | 啟用 run_command 工具 | 關閉 |
| `AI_CODE_PATCH` | 啟用改碼工具 | 關閉 |
| `AI_CODE_USE_CONTAINER` | 啟用容器化執行 | 關閉 |
| `AI_CODE_CONTAINER_ENGINE` | 容器引擎 | `auto` |
| `AI_CODE_CONTAINER_IMAGE` | 自訂映像檔 | 自動偵測 |
| `AI_CODE_CONTAINER_MEMORY` | 容器記憶體限制 | `2g` |
| `AI_CODE_CONTAINER_CPU` | 容器 CPU 限制 | `2` |
| `AI_CODE_CONTAINER_TIMEOUT` | 容器超時秒數 | `120` |
| `AI_CODE_COLLECT_DATA` | 啟用資料收集 | 關閉 |
| `AI_CODE_DATA_FILE` | 資料檔案路徑 | `data/interactions.jsonl` |

## 常見問題

### Q: 為什麼回答說「知識庫中沒有找到足夠相關的參考資料」？

這是嚴格模式的保護機制。當你問規格類問題（如「最大值是多少」）但知識庫沒有相關文件時，系統會拒答而非瞎猜。

解決方法：
1. 確認知識庫有包含相關文件
2. 改用不含規格關鍵字的問法
3. 在 `config.py` 調低 `WEAK_REF_THRESHOLD`

### Q: Agent 模式很慢？

Agent 需要多輪工具呼叫，可以：
1. 使用 `--full` 強制完整模式（如果專案不大）
2. 調低 `MAX_TOOL_LOOPS`（預設 12）
3. 問題描述更具體，減少探索範圍

### Q: 嚴格模式兩階段都沒輸出？

兩階段都使用串流輸出，應該可以即時看到生成過程。如果卡住：
1. 確認 Ollama 服務正在運行：`ollama ps`
2. 確認模型已載入（首次需要時間）
3. 檢查 GPU VRAM 是否足夠

### Q: 如何調整 RAG 返回的內容量？

- `KNOWLEDGE_CONTENT_MAX_CHARS`：每個 chunk 最大字元數，增加可獲得更完整內容但佔用更多 context
- `DYNAMIC_TOP_K_MIN/MAX`：控制返回的參考資料數量
- `KNOWLEDGE_THRESHOLD`：提高門檻可減少不相關內容，但可能漏掉有用資訊

### Q: 二進位檔案找不到版本資訊？

預設使用 `strings` 提取整個檔案的可讀字串。如果找不到：
1. 確認版本資訊確實存在：`strings firmware.bin | grep -i version`
2. 版本字串可能使用非標準格式，嘗試更具體的問法

### Q: apply_patch 失敗？

可能的原因：
1. diff 格式不正確（需要 unified diff 格式）
2. 檔案內容已變更，context 不匹配
3. 超出修改限制（最多 5 個檔案，每檔 200 行）

解決方法：
1. 使用 `git_diff` 查看當前變更
2. 使用 `git_status` 確認狀態
3. 確保 diff 的 context 行與實際檔案一致

### Q: 容器化執行失敗？

1. 確認 Docker 或 Podman 已安裝：`python container_runner.py check`
2. 確認映像檔已拉取：`python container_runner.py pull --all`
3. 檢查容器日誌查看詳細錯誤

### Q: 資料收集沒有記錄？

1. 確認環境變數已設定：`AI_CODE_COLLECT_DATA=1`
2. 確認資料目錄存在且有寫入權限
3. 使用 `python data_flywheel.py stats` 查看統計

## 安全注意事項

1. **知識庫檔案**（`knowledge.json`）可能包含內部文件內容，**請勿提交到公開 repo**
2. **資料飛輪**（`data/interactions.jsonl`）包含使用者提問和回答，**請勿提交到公開 repo**
3. **`--run-tests`** 會執行專案內的測試腳本，對不信任的專案有安全風險
4. **`--patch`** 會直接修改檔案，建議先用 `dry_run=True` 預覽
5. 分析外部/不信任的專案時，建議使用 **`--container`** 在容器中執行

## License

MIT License - 詳見 [LICENSE](LICENSE) 檔案
