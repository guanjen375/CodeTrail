# 智能程式碼分析器

基於 Ollama 的本地程式碼分析工具，支援 RAG（知識庫檢索）和 Agent 模式。

## 功能特色

- **完整模式**：小型專案（< 200KB）一次讀入全部程式碼分析
- **Agent 模式**：大型專案動態探索，按需讀取檔案
- **網頁模式**：直接分析 GitHub/GitLab/Bitbucket 上的程式碼（測試中）
- **知識庫 RAG**：整合技術文件（PDF/Markdown），回答時引用文件來源
- **Code RAG**：自動索引程式碼符號（函式/類別），快速定位相關程式碼
- **圖片 OCR**：支援截圖中的錯誤訊息辨識（使用 VL 模型）
- **二進位分析**：韌體/執行檔的 Hex dump + 智能字串提取
- **嚴格模式（兩階段）**：規格類問題強制引用文件，並透過自我檢查過濾幻覺

## 安裝需求

```bash
# 安裝 Ollama
# https://ollama.ai/download

# 拉取模型
ollama pull qwen3-coder:30b              # 主模型
ollama pull qwen3-vl:30b-a3b             # VL 模型（圖片辨識用）
ollama pull bge-m3                       # Embedding 模型
ollama pull qllama/bge-reranker-v2-m3    # Reranker 模型

# 安裝 Python 依賴
pip install requests
```

## 快速開始

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
├── main.py          # 主程式入口
├── config.py        # 設定檔
├── utils.py         # 共用工具（LLM 呼叫、檔案掃描、嚴格模式）
├── agent.py         # Agent 模式（動態探索 + run_command）
├── context.py       # 完整模式（全量分析）
├── knowledge.py     # 知識庫 RAG（Reranker + MMR）
├── code_rag.py      # 程式碼索引 RAG（符號提取 + Embedding）
├── media.py         # 媒體處理（圖片 OCR、二進位分析）
├── web.py           # 網頁模式（Git URL 下載，測試中）
├── http_client.py   # HTTP 連線池管理
├── RAG.py           # 知識庫建立工具（獨立腳本）
└── knowledge.json   # 知識庫（自行建立）
```

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

## License

MIT
