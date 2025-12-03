# 智能程式碼分析器

基於 Ollama 的本地程式碼分析工具，支援 RAG（知識庫檢索）和 Agent 模式。

## 功能特色

- **完整模式**：小型專案一次讀入全部程式碼分析
- **Agent 模式**：大型專案動態探索，按需讀取檔案
- **知識庫 RAG**：整合技術文件，回答時引用文件來源
- **Code RAG**：自動索引程式碼符號（函式/類別），快速定位相關程式碼
- **圖片 OCR**：支援截圖中的錯誤訊息辨識
- **嚴格模式**：規格類問題強制引用文件，避免幻覺

## 安裝需求

```bash
# 安裝 Ollama
# https://ollama.ai/download

# 拉取模型
ollama pull qwen3-coder:30b      # 主模型
ollama pull bge-m3               # Embedding 模型
ollama pull qllama/bge-reranker-v2-m3   # Reranker 模型（可選）

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

# 知識庫格式見下方說明
```

### 排除/包含目錄

```bash
# 排除特定檔案
python main.py . --exclude="*.test.py"

# 包含預設排除的目錄（如 third_party）
python main.py . --include-dir=third_party
```

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

## 設定檔

編輯 `config.py` 調整參數：

```python
# 模型設定
MODEL = "qwen3-coder:30b"        # 主模型
EMBEDDING_MODEL = "bge-m3"       # Embedding 模型
NUM_CTX = 65536                  # Context 長度

# RAG 設定
KNOWLEDGE_THRESHOLD = 0.25       # 知識庫相關度門檻
CODE_RAG_THRESHOLD = 0.30        # 程式碼 RAG 門檻

# 嚴格模式
STRICT_MODE = True               # 啟用嚴格模式
WEAK_REF_THRESHOLD = 0.30        # REF 太弱時拒答
```

## 檔案結構

```
AI/
├── main.py          # 主程式入口
├── config.py        # 設定檔
├── agent.py         # Agent 模式（動態探索）
├── context.py       # 完整模式（全量分析）
├── knowledge.py     # 知識庫 RAG
├── code_rag.py      # 程式碼索引 RAG
├── utils.py         # 共用工具函式
├── ocr.py           # 圖片 OCR 處理
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

### Q: 如何建立知識庫？

可以用 `RAG.py` 處理 PDF/Markdown 文件：

```bash
python RAG.py /path/to/doc /path/to/output
```

## License

MIT
