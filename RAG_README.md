# RAG 知識庫使用指南

本工具支援 RAG（Retrieval-Augmented Generation）功能，可將技術文件（PDF、Markdown、純文字）整合到問答系統中，讓 AI 回答時能引用文件內容。

## 快速開始

```bash
# 1. 建立知識庫（將 PDF 加入）
python RAG.py manual.pdf knowledge.json

# 2. 使用知識庫進行問答
python main.py . --kb=knowledge.json
>>> API 的 rate limit 是多少？
# 回答會標註：根據 REF1（manual.pdf 第 15 頁）...
```

## 建立知識庫

### 基本用法

```bash
python RAG.py <輸入檔案> <輸出JSON>
```

### 支援的檔案類型

| 類型 | 副檔名 | 說明 |
|------|--------|------|
| PDF | `.pdf` | 使用 pymupdf4llm 提取，保留頁碼資訊 |
| Markdown | `.md` | 保留標題結構 |
| 純文字 | `.txt` | 按段落切分 |

### 增量更新

知識庫支援增量更新，可多次執行加入不同文件：

```bash
# 第一次：建立知識庫
python RAG.py api_manual.pdf knowledge.json

# 第二次：加入更多文件（自動 append）
python RAG.py hardware_spec.pdf knowledge.json
python RAG.py faq.md knowledge.json

# 更新已存在的文件（自動移除舊版再加入新版）
python RAG.py api_manual_v2.pdf knowledge.json
```

### 文件類型自動識別

根據檔名自動識別文件類型，影響回答時的優先級：

| 類型 | 檔名關鍵字 | 用途 |
|------|-----------|------|
| `spec` | `_spec`, `datasheet` | 規格書，優先級最高 |
| `manual` | `manual`, `handbook` | 操作手冊 |
| `guide` | `guide`, `tutorial`, `howto` | 使用指南 |
| `api` | `_api`, `reference` | API 文件 |
| `faq` | `faq`, `q&a` | 常見問題 |
| `warning` | 內容含 WARNING/CAUTION/危險 | 警告類，會特別標示 |

## 使用知識庫

### 啟動時指定

```bash
python main.py . --kb=knowledge.json
```

### 預設知識庫

若專案目錄下有 `knowledge.json`，會自動載入。

### 查詢範例

```
>>> 這個 API 的 timeout 預設是多少？
[REF 高信心] api_manual.pdf [spec] p.12, 15

回答會標註：
根據 REF1（api_manual.pdf 第 12 頁），預設 timeout 為 30 秒...
```

## 進階功能

### 混合搜尋（Hybrid Search）

同時使用語意搜尋（Embedding）和關鍵字搜尋，提高召回率：

- **Embedding 搜尋**：理解語意，找到意思相近的內容
- **關鍵字搜尋**：精確匹配專有名詞、型號、API 名稱

### Reranker 二次排序

使用專用 Reranker 模型對候選結果進行二次排序：

```bash
# 安裝 Reranker 模型（可選，會自動 fallback 到 LLM reranking）
ollama pull qllama/bge-reranker-v2-m3
```

### Query Expansion

當搜尋結果不足時，自動用 LLM 擴展搜尋關鍵字：

```
原始問題: "怎麼設定 baud rate？"
擴展關鍵字: baud rate, serial, UART, baudrate, communication
```

### MMR 多樣性選擇

使用 Max Marginal Relevance 演算法，避免返回過於相似的結果：

- 平衡相關性與多樣性
- 避免同一段落被重複引用

### 信心度標示

查詢結果會標示信心度，幫助判斷參考資料的可靠度：

| 信心度 | 分數範圍 | 說明 |
|--------|---------|------|
| 高信心 | ≥ 0.6 | 可直接引用 |
| 中信心 | 0.4-0.6 | 請謹慎使用 |
| 低信心 | < 0.4 | 僅供參考 |

## 嚴格模式

當問題涉及規格/文件內容時，自動啟用嚴格模式：

### 觸發關鍵字

`規格`、`spec`、`manual`、`根據文件`、`最大值`、`限制`、`是否支援` 等

### 兩階段自我檢查

1. **第一階段**：根據 REF 生成初稿
2. **第二階段**：逐句審核，刪除無根據的內容

### 拒答機制

當 REF 分數太低（< 0.35）時，會拒絕回答並提示：

```
這是規格/文件類問題，但知識庫中沒有找到足夠相關的參考資料。

建議：
1. 確認知識庫中有包含相關的規格文件
2. 嘗試用更具體的關鍵字描述問題
3. 若確定要用一般知識回答，請改用不含規格關鍵字的問法
```

## 設定調整

在 `config.py` 中可調整以下參數：

```python
# 知識庫設定
KNOWLEDGE_FILE = "knowledge.json"      # 預設知識庫檔案
KNOWLEDGE_TOP_K = 5                     # 返回的參考數量
KNOWLEDGE_THRESHOLD = 0.30              # 相關度門檻

# 嚴格模式
STRICT_MODE = True                      # 是否啟用嚴格模式
WEAK_REF_THRESHOLD = 0.35               # REF 太弱時拒答的門檻

# 搜尋優化
USE_RERANKER = True                     # 啟用 Reranker
USE_HYBRID_SEARCH = True                # 啟用混合搜尋
USE_QUERY_EXPANSION = True              # 啟用 Query Expansion
USE_MMR = True                          # 啟用 MMR 多樣性選擇
```

## 知識庫檔案格式

知識庫是 JSON 格式，結構如下：

```json
{
  "metadata": {
    "created_at": "2024-01-01T12:00:00",
    "updated_at": "2024-01-02T10:00:00",
    "embedding_model": "bge-m3",
    "chunk_size": 1200,
    "total_documents": 3,
    "total_chunks": 150,
    "documents": ["api_manual.pdf", "spec.pdf", "faq.md"]
  },
  "chunks": [
    {
      "id": "api_manual.pdf::p1::c0::a1b2c3d4",
      "source": "api_manual.pdf",
      "page": 1,
      "chunk_index": 0,
      "content": "...",
      "type": "spec",
      "section": "1. Introduction",
      "embedding": [0.1, 0.2, ...]
    }
  ]
}
```

## 常見問題

### Q: 為什麼有時候回答說「知識庫中沒有找到足夠相關的參考資料」？

這是嚴格模式的保護機制，避免 AI 在沒有足夠證據時亂回答規格類問題。

解決方法：
1. 確認知識庫有包含相關文件
2. 改用不含規格關鍵字的問法（如「一般來說 timeout 設多少？」）
3. 調低 `config.py` 中的 `WEAK_REF_THRESHOLD`

### Q: 知識庫太大，載入很慢？

- 知識庫會在首次載入時預計算 numpy embedding 矩陣
- 後續查詢會使用向量化運算加速
- 建議：只加入真正需要的文件，避免加入大量無關文件

### Q: 如何更新已存在的文件？

直接重新執行 `python RAG.py <檔案> <知識庫>` 即可，會自動移除舊版本再加入新版本。

### Q: 支援中文文件嗎？

支援。使用的 bge-m3 模型對中英文都有良好支援。

### Q: Embedding 模型不一致會怎樣？

若知識庫使用的 embedding 模型與目前設定不同，會發出警告：

```
[WARN] 知識庫 embedding model 不一致！
       知識庫使用: bge-m3
       目前設定: bge-large-en
       請執行 RAG.py 重建知識庫，否則搜尋結果可能不準確
```

建議重新執行 RAG.py 重建知識庫。

## 最佳實踐

1. **文件命名**：使用有意義的檔名，包含類型關鍵字（如 `api_spec.pdf`、`user_guide.md`）
2. **文件品質**：確保 PDF 是可選取文字的版本（非掃描圖片）
3. **適度分類**：將不同主題的文件分開，避免混淆
4. **定期更新**：文件有更新時，重新執行 RAG.py 更新知識庫
5. **測試查詢**：加入文件後，先測試幾個問題確認能正確引用

## 安全注意事項

`knowledge.json` 可能包含內部文件內容，**請勿提交到公開 repo**。

建議在 `.gitignore` 中加入：

```
knowledge.json
*.json
```
