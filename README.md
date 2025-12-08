# 智能程式碼分析器

基於 Ollama 的本地程式碼分析工具，支援知識庫檢索（RAG）、二進位/ELF 分析、圖片 OCR、Agent 動態探索等功能。

## 快速開始

```bash
# 最基本用法：分析當前目錄
python main.py .

# 分析指定專案並提問
python main.py /path/to/project "這個專案的主要功能是什麼？"

# 搭配知識庫（技術文件）
python main.py . --kb=knowledge.json

# 分析韌體/二進位檔案
python main.py . --allow-external
>>> bin:/path/to/firmware.bin 這個韌體版本是什麼？

# 分析錯誤截圖
python main.py . --allow-external
>>> img:/path/to/error.png 這個錯誤怎麼解？
```

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
| `問題` | 單次模式的問題（不帶則進入互動模式） | `python main.py . "main 函式做了什麼？"` |

### 模式選擇

| 參數 | 說明 |
|------|------|
| `--full` | 強制完整模式（一次讀入所有程式碼，適合小專案 < 200KB） |
| `--agent` | 強制 Agent 模式（動態探索，適合大專案） |
| `--web URL` | 網頁模式，分析 GitHub/GitLab 上的公開 repo |

### 知識庫與過濾

| 參數 | 說明 | 範例 |
|------|------|------|
| `--kb=路徑` | 指定知識庫檔案 | `--kb=docs.json` |
| `--exclude=模式` | 排除符合的檔案 | `--exclude="*.test.py"` |
| `--include-dir=目錄` | 包含預設排除的目錄 | `--include-dir=third_party` |

### 進階功能

| 參數 | 說明 |
|------|------|
| `--allow-external` | 允許讀取專案目錄外的圖片、bin、elf 檔案 |
| `--run-tests` | 啟用測試執行工具（pytest, cargo test 等） |
| `--patch` | 啟用改碼工具（apply_patch, git_status, git_diff） |
| `--container` | 在 Docker/Podman 容器中安全執行測試 |

### 常用組合

```bash
# 日常問答
python main.py .

# 有技術文件時（回答會引用文件）
python main.py . --kb=docs.json

# 需要 AI 幫忙改 code
python main.py . --patch

# Debug + 跑測試驗證
python main.py . --patch --run-tests

# 分析不信任的外部專案（容器隔離）
python main.py /path/to/untrusted --run-tests --container

# 分析 GitHub repo
python main.py --web https://github.com/user/repo
```

## 互動模式

啟動後進入互動模式，可連續提問：

```
💬 輸入問題 (Enter=整體分析, q=離開, clear=清除歷史)
>>> 這個函式 foo() 做了什麼？
```

### 特殊前綴語法

在問題中使用特殊前綴可以分析外部檔案：

| 前綴 | 用途 | 範例 |
|------|------|------|
| `img:路徑` | 圖片 OCR | `img:/path/to/error.png 這個錯誤怎麼解？` |
| `bin:路徑` | 二進位分析 | `bin:/path/to/firmware.bin 版本號是什麼？` |
| `elf:路徑` | ELF 檔案解析 | `elf:/path/to/app.elf entry point 在哪？` |

**路徑含空白時使用引號**：
```
>>> img:"/path with spaces/error.png" 這個錯誤是什麼？
>>> bin:'/my folder/firmware.bin' 幫我分析
```

**注意**：讀取專案目錄外的檔案需要 `--allow-external` 參數。

## 主要功能

### 1. 知識庫 RAG

整合技術文件（PDF/Markdown），回答時自動引用文件來源：

```bash
# 建立知識庫
python RAG.py /path/to/document.pdf output.json

# 使用知識庫
python main.py . --kb=output.json
>>> API 的 rate limit 是多少？
# 回答會標註：根據 REF1（API_Manual.pdf 第 15 頁）...
```

### 2. 二進位/ELF 分析

分析韌體、執行檔等二進位檔案：

```bash
python main.py . --allow-external
>>> bin:/path/to/u-boot.bin 這個 U-Boot 的版本？
```

**分析內容**：
- Magic 偵測（ELF、uImage、gzip、squashfs 等）
- Hex dump（前 1KB）
- 智能字串提取（含 file offset）
- 版本號、編譯日期等重要資訊優先顯示

**ELF 專屬**（需系統安裝 `readelf`）：
- ELF Header（Class、Machine、Entry point）
- Sections（.text、.rodata、.data 等）
- Symbols（Top N functions/objects）
- .comment（編譯器資訊）

### 3. 圖片 OCR

辨識截圖中的錯誤訊息：

```bash
python main.py . --allow-external
>>> img:/path/to/error_screenshot.png 這個錯誤怎麼解？
```

支援格式：`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`

### 4. 嚴格模式（自動啟用）

當問題涉及規格/文件內容時，自動啟用兩階段自我檢查：

1. **第一階段**：根據 REF 生成初稿
2. **第二階段**：逐句審核，刪除無根據的內容

觸發關鍵字：`規格`、`spec`、`manual`、`根據文件`、`最大值`、`限制` 等

### 5. 改碼閉環（需 --patch）

```bash
python main.py . --patch
>>> 幫我修正這個 bug
# AI 會使用 apply_patch 修改程式碼，自動執行 lint
```

**工具**：
- `apply_patch`：套用程式碼修改
- `git_status`/`git_diff`：查看變更
- `run_lint`：自動格式化

### 6. 測試執行（需 --run-tests）

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

### 7. 容器化執行（需 --container）

在 Docker/Podman 中安全執行，適合分析不信任的專案：

```bash
python main.py /path/to/untrusted --run-tests --container
```

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
