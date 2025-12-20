# 智能程式碼分析器

基於 Ollama 的本地程式碼分析工具，支援知識庫檢索（RAG）、二進位/ELF 分析、圖片 OCR、Agent 動態探索等功能。

> **⚠️ 注意：此專案為個人使用工具，不適合公開發布**
>
> - 程式碼未經完整測試和安全審計
> - 部分功能依賴特定環境配置
> - 錯誤處理可能不完善
> - 僅供學習參考和個人使用

## 系統需求

### 軟體需求

| 項目 | 最低版本 | 建議版本 | 說明 |
|------|---------|---------|------|
| Python | 3.10+ | 3.11+ | 使用 type hints、match-case 等新語法 |
| Ollama | 0.1.0+ | 最新版 | 本地 LLM 推理引擎 |
| Git | 2.0+ | 最新版 | 用於專案分析和 `--web` 模式 |

### 硬體需求

#### 最低配置（可運行，但體驗較差）

| 項目 | 規格 | 說明 |
|------|------|------|
| RAM | 16GB | 僅能使用較小模型（如 7B） |
| VRAM | 8GB | 需大量 offload 到 RAM，速度慢 |
| 儲存 | 50GB | 模型檔案佔用空間 |

#### 建議配置（流暢使用 30B 模型）

| 項目 | 規格 | 說明 |
|------|------|------|
| RAM | 32GB+ | 搭配 VRAM offload 使用 |
| VRAM | 24GB+ | RTX 4090 / RTX 3090 / A5000 等 |
| 儲存 | 100GB SSD | 模型載入更快 |

#### 最佳配置（完整功能、大 context）

| 項目 | 規格 | 說明 |
|------|------|------|
| RAM | 64GB+ | 支援 128K context offload |
| VRAM | 32GB+ | A100 / RTX A6000 等 |
| 儲存 | NVMe SSD | 最佳 I/O 效能 |

> **Context 長度與記憶體對照**（以 30B 模型為例）：
> - 32K context：約需 24GB VRAM
> - 64K context：約需 32GB VRAM 或 24GB VRAM + 32GB RAM（offload）
> - 128K context：約需 32GB VRAM + 64GB RAM（offload）

### Python 套件

**核心依賴**（必須安裝，否則啟動會 `ModuleNotFoundError`）：

```
requests>=2.28.0    # HTTP 請求（與 Ollama API 通訊）
```

**強烈建議安裝**（未安裝不會報錯，但效能較差）：

```
numpy>=1.21.0       # 向量運算加速（RAG 搜尋、MMR 選擇）
                    # 未安裝仍可運作，但速度較慢
```

**可選依賴**（特定功能才需要）：

```
# RAG 知識庫建立（執行 RAG.py 從 PDF 建知識庫時需要）
pymupdf4llm>=0.0.5  # PDF 解析
ollama>=0.1.0       # Ollama Python SDK

# ELF 分析（系統工具，非 Python 套件）
# Linux: sudo apt install binutils
# macOS: brew install binutils
# Windows: 需安裝 MinGW 或 WSL
```

**安裝方式**：

```bash
# 最小安裝（僅核心功能）
pip install requests

# 建議安裝（完整功能）
pip install requests numpy

# 需要建立知識庫時
pip install requests numpy pymupdf4llm ollama
```

## 快速開始

```bash
# 日常問答（預設進入多輪對話模式）
python main.py .

# 有技術文件時（回答會引用文件）
python main.py . --kb=docs.json

# 需要 AI 幫忙改 code
python main.py . --patch

# Debug + 跑測試驗證
python main.py . --patch --run-tests

# 分析圖片/韌體/二進位檔案
python main.py .
>>> file:/path/to/file 這個韌體版本是什麼？

# 分析不信任的外部專案（容器隔離）
python main.py /path/to/untrusted --run-tests --container

# 分析 GitHub repo
python main.py --web https://github.com/user/repo

# === 不需要專案的快速問答（QA 模式）===

# QA 模式：不掃專案，直接問答
python main.py --qa
```

**重要提示**：
- **預設是多輪對話模式**：不帶問題參數時，進入互動式對話，可連續提問
- **單輪模式**：帶問題參數（用引號包住）則回答後直接結束

## 安裝

### 1. 安裝 Ollama

前往 https://ollama.ai/download 下載安裝。

### 2. 拉取模型

```bash
ollama pull qwen3-coder:30b              # 主模型
ollama pull qwen3-vl:30b-a3b             # VL 模型（圖片辨識）
ollama pull bge-m3                       # Embedding 模型
ollama pull qllama/bge-reranker-v2-m3    # Reranker 模型（可選）
```

### 3. 安裝 Python 依賴

```bash
pip install -r requirements.txt
```

## 命令列參數完整說明

```bash
python main.py [專案路徑] [問題] [選項]
```

### 基本參數

| 參數 | 說明 | 範例 |
|------|------|------|
| `專案路徑` | 要分析的專案目錄（預設 `.`） | `python main.py /path/to/project` |
| `"問題"` | 單輪模式：帶問題（需引號）則回答後結束；不帶則進入多輪對話 | `python main.py . "main 函式做了什麼？"` |

### 模式選擇

| 參數 | 說明 |
|------|------|
| `--qa` | QA 模式：不掃專案、不建 Code RAG，直接問答（適合解釋錯誤、一般問題） |
| `--full` | 強制完整模式（一次讀入所有程式碼，適合小專案 < 200KB） |
| `--agent` | 強制 Agent 模式（動態探索，適合大專案） |
| `--web URL` | 網頁模式，分析 GitHub/GitLab 上的公開 repo |

### 知識庫與規則

| 參數 | 說明 | 範例 |
|------|------|------|
| `--kb=路徑` | 指定知識庫檔案（RAG 檢索） | `--kb=docs.json` |
| `--sk=檔案` | 載入自定義系統規則 | `--sk=rules.md` |
| `--exclude=模式` | 排除符合的檔案 | `--exclude="*.test.py"` |
| `--include-dir=目錄` | 包含預設排除的目錄 | `--include-dir=third_party` |

### 進階功能

| 參數 | 說明 |
|------|------|
| `--run-tests` | 啟用測試執行工具（pytest, cargo test 等） |
| `--patch` | 啟用改碼工具（apply_patch, git_status, git_diff） |
| `--container` | 在 Docker/Podman 容器中安全執行測試 |

### 檔案分析（file:）

使用 `file:` 前綴分析檔案，系統自動偵測類型並處理：

```bash
>>> file:/path/to/error.png 這個錯誤怎麼解？
>>> file:/path/to/firmware.bin 版本號是什麼？
>>> file:/path/to/app.elf entry point 在哪？
```

支援：圖片（OCR）、二進位、ELF 等格式。路徑含空白時用引號包住。詳見[主要功能 - 檔案分析](#3-檔案分析file)。

## 主要功能

### 1. QA 模式（--qa）

不掃描專案、不建立 Code RAG，直接進行問答。適合解釋錯誤、一般問題、搭配檔案分析。

```bash
python main.py --qa "這個 error 是什麼意思: expected ';' before '}' token"
python main.py --qa --kb=api_docs.json "這個 API 的 timeout 預設值是多少？"
python main.py --qa "file:/path/to/build_error.png 這個編譯錯誤怎麼修？"
```

**優點**：啟動快、不需專案資料夾、仍保留 file:/知識庫功能

### 2. 知識庫 RAG（--kb）

整合技術文件（PDF/Markdown），回答時自動引用文件來源：

```bash
# 建立知識庫
python RAG.py /path/to/document.pdf output.json

# 使用知識庫
python main.py . --kb=output.json
>>> API 的 rate limit 是多少？
# 回答會標註：根據 REF1（API_Manual.pdf 第 15 頁）...
```

詳細說明請參考 [RAG_README.md](RAG_README.md)。

### 3. 自定義系統規則（--sk）

載入自定義規則檔案，讓 AI 遵循特定的開發規範或回答風格：

```bash
# 使用自定義規則
python main.py . --sk=rules.md

# 搭配 QA 模式
python main.py --qa --sk=rules.md

# 搭配知識庫
python main.py . --kb=docs.json --sk=rules.md
```

**支援的檔案格式**：
- `.md` - Markdown（推薦，支援標題/列表等結構）
- `.txt` - 純文字
- 任何 UTF-8 編碼的純文字檔案

**規則檔案範例**（`rules.md`）：

```markdown
# 開發規則
- 使用繁體中文回答
- Agent 邏輯放 agent.py，工具實作放 agent_tools.py
- 所有 config 集中在 config.py
- 優先使用現有的工具函式，避免重複造輪子
- 程式碼註解使用繁體中文
```

**注意事項**：
- 規則檔案最大 4000 字元（超過會自動截斷）
- 規則會注入到所有模式（QA、Full、Agent）的 system prompt 中
- 上下文佔用極小（< 1%），不影響效能
- 與 `--kb` 可同時使用，互不衝突

### 4. 檔案分析（file:）

使用 `file:` 前綴分析各種檔案，系統自動偵測類型：

```bash
>>> file:/path/to/error.png 這個錯誤怎麼解？
>>> file:/path/to/u-boot.bin 這個 U-Boot 的版本？
>>> file:/path/to/app.elf entry point 在哪？
```

**圖片**：OCR 辨識文字（支援 `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`）

**二進位**：Magic 偵測、Hex dump、智能字串提取（含 offset）、版本號優先顯示

**ELF**（需系統安裝 `readelf`）：Header、Sections、Symbols、.comment 編譯器資訊

### 5. 嚴格模式（自動啟用）

當問題涉及規格/文件內容時，自動啟用兩階段自我檢查：

1. **第一階段**：根據 REF 生成初稿
2. **第二階段**：逐句審核，刪除無根據的內容

觸發關鍵字：`規格`、`spec`、`manual`、`根據文件`、`最大值`、`限制` 等

### 6. 改碼閉環（需 --patch）

```bash
python main.py . --patch
>>> 幫我修正這個 bug
# AI 會使用 apply_patch 修改程式碼，自動執行 lint
```

**工具**：
- `apply_patch`：套用程式碼修改
- `git_status`/`git_diff`：查看變更
- `run_lint`：自動格式化

### 7. 測試執行（需 --run-tests）

```bash
python main.py . --run-tests
>>> 幫我跑測試看看有沒有問題
```

**支援的測試命令**：
- Python: `pytest`, `python -m pytest`
- C/C++: `ctest`
- Node.js: `npm test`, `yarn test`
- Rust: `cargo test`
- Go: `go test`

### 8. 容器化執行（需 --container）

在 Docker/Podman 中安全執行，適合分析不信任的專案：

```bash
python main.py /path/to/untrusted --run-tests --container
```

## 其他功能

### Code RAG（程式碼索引）

自動建立專案級程式碼索引，加速 Agent 模式的檔案定位：

- 使用 AST/tree-sitter 精準解析函式、類別等符號
- 支援增量更新（只重建變更的檔案）
- 快取儲存於 `.code_rag_cache_*.json` 和 `.npz`

### 網頁模式（--web）

直接分析 GitHub/GitLab/Bitbucket 上的公開專案：

```bash
python main.py --web https://github.com/user/repo
python main.py --web https://github.com/user/repo/tree/main/src  # 只分析子目錄
```

### 資料收集（Fine-tuning 用）

收集互動記錄，用於後續訓練 reranker 或微調模型：

```bash
# 啟用資料收集
export AI_CODE_COLLECT_DATA=1
python main.py .

# 查看統計
python data_flywheel.py stats

# 手動評分
python data_flywheel.py rate

# 匯出訓練資料
python data_flywheel.py export --output training.jsonl
```

### 追問偵測

互動模式下自動偵測追問，保持對話上下文：
- 觸發詞：`我是`、`我用的是`、`那這樣`、`那如果`、`所以是` 等
- 短回答自動關聯上文（如回答 `a53`、`cortex` 等）

## 設定檔

編輯 `config.py` 調整參數：

```python
# 模型設定
MODEL = "qwen3-coder:30b"
VL_MODEL = "qwen3-vl:30b-a3b"

# Context 長度（根據 GPU VRAM 調整）
NUM_CTX = 131072  # 128K，會自動 offload 到 RAM

# 知識庫設定
KNOWLEDGE_THRESHOLD = 0.30  # 相關度門檻
```

### Context 長度建議

| 配置 | 建議 NUM_CTX |
|------|-------------|
| 24GB VRAM | 32768 (32K) |
| 32GB VRAM | 65536 (64K) |
| 32GB+ VRAM + 大 RAM | 131072 (128K) |

## 環境變數

| 變數 | 說明 |
|------|------|
| `AI_CODE_RUN_TESTS=1` | 等同 `--run-tests` |
| `AI_CODE_PATCH=1` | 等同 `--patch` |
| `AI_CODE_USE_CONTAINER=1` | 等同 `--container` |
| `AI_CODE_COLLECT_DATA=1` | 啟用資料收集（用於 fine-tuning） |

## 常見問題

### Q: 回答說「知識庫中沒有找到足夠相關的參考資料」？

這是嚴格模式的保護機制。解決方法：
1. 確認知識庫有包含相關文件
2. 改用不含規格關鍵字的問法
3. 調低 `config.py` 中的 `WEAK_REF_THRESHOLD`

### Q: Agent 模式很慢？

1. 小專案可用 `--full` 強制完整模式
2. 問題描述更具體，減少探索範圍

### Q: 二進位檔案找不到版本資訊？

1. 確認版本字串存在：`strings firmware.bin | grep -i version`
2. ELF 檔案可查看 `.comment` section

### Q: ELF 解析資訊不完整？

需要系統安裝 `readelf`：
- Linux: `sudo apt install binutils`
- macOS: `brew install binutils`

## 安全注意事項

1. `knowledge.json` 可能包含內部文件，**請勿提交到公開 repo**
2. `--run-tests` 會執行專案腳本，對不信任的專案有風險
3. 分析外部專案時建議使用 `--container`

## License

MIT License
