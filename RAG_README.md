# RAG 知識庫使用指南

本工具支援 RAG（Retrieval-Augmented Generation）功能，可將技術文件（PDF、Markdown、純文字）、**聊天截圖**、**技術圖片**及**網頁**整合到問答系統中，讓 AI 回答時能引用文件內容。

## 快速開始

```bash
# 1. 建立知識庫（將 PDF 加入）
python RAG.py manual.pdf knowledge.json

# 2. 從聊天截圖加入知識（互動式，會詢問是否入庫）
python RAG.py teams_chat.png knowledge.json --chat

# 3. 從技術圖片加入知識（互動式）
python RAG.py npx6_arch.png knowledge.json --image

# 4. 從網頁加入知識（互動式）
python RAG.py https://docs.example.com/api knowledge.json --url

# 5. 使用知識庫進行問答
python main.py . --kb=knowledge.json
>>> API 的 rate limit 是多少？
# 回答會標註：根據 REF1（manual.pdf 第 15 頁）...
```

**互動式模式**：使用 `--chat`、`--image`、`--url` flag 時，系統會：
1. 分析/抓取內容並顯示完整結果
2. 詢問：「是否將此內容加入 knowledge.json？[Y/n]」
3. 若是則入庫，若否則結束

## 建立知識庫

### 基本用法

```bash
# 一般文件模式（直接入庫）
python RAG.py <輸入檔案> <輸出JSON>

# 互動式模式（分析後詢問是否入庫）
python RAG.py <截圖檔案> <輸出JSON> --chat
python RAG.py <圖片檔案> <輸出JSON> --image
python RAG.py <網址> <輸出JSON> --url
```

### 支援的檔案類型

| 類型 | 副檔名 | 說明 |
|------|--------|------|
| PDF | `.pdf` | 使用 pymupdf4llm 提取，保留頁碼資訊 |
| Markdown | `.md` | 保留標題結構 |
| 純文字 | `.txt` | 按段落切分 |
| 圖片 | `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp` | 聊天截圖（`--chat`）或技術圖片（`--image`） |
| 網頁 | `http://`, `https://` | 網頁內容（`--url`），自動轉換為 Markdown |

### 增量更新

知識庫支援增量更新，可多次執行加入不同文件：

```bash
# 第一次：建立知識庫
python RAG.py api_manual.pdf knowledge.json

# 第二次：加入更多文件（自動 append）
python RAG.py hardware_spec.pdf knowledge.json
python RAG.py faq.md knowledge.json

# 加入聊天截圖
python RAG.py --chat slack_discussion.png knowledge.json

# 加入技術圖片
python RAG.py --image memory_map.png knowledge.json

# 加入網頁
python RAG.py --url https://docs.banana-pi.org/zh/BPI-F3 knowledge.json

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
| `chat` | 聊天截圖（`--chat` 模式） | 對話摘要 |
| `diagram` | 技術圖片（`--image` 模式） | 架構/流程圖描述 |
| `web` | 網頁（`--url` 模式） | 網頁內容 |

### 聊天截圖模式（--chat）

將 Teams、Slack、Discord 等通訊軟體的對話截圖轉換為結構化知識：

```bash
python RAG.py zebu_discussion.png knowledge.json --chat

# 執行後會顯示：
# [INFO] 使用 VL 模型分析截圖: zebu_discussion.png
# ============================================================
# [VL 模型分析結果完整內容]
# ============================================================
#
# 是否將此內容加入 knowledge.json？ [Y/n]: y
# [INFO] 新增截圖知識: chat_zebu_discussion.png
# ...
```

**處理流程**：
1. 使用 VL 模型（config.py 中的 `VL_MODEL`）分析截圖
2. **顯示完整分析結果**（讓你確認內容正確）
3. 詢問是否加入知識庫
   - 是 → 生成 embedding 並入庫
   - 否 → 結束
4. 存入知識庫（來源標記為 `chat_<檔名>`）

**自動整理格式**：
- 原始對話摘錄（忠實轉錄）
- 主題標題
- 背景/問題
- 重點摘要
- 詳細步驟
- 注意事項
- 相關檔案/工具

**使用場景**：
- 同事分享的技術知識對話
- 團隊討論的解決方案
- 問題排查的經驗記錄
- 專案會議的重點截圖

**注意事項**：
- 需要安裝支援視覺的模型（如 `qwen3-vl`、`llava` 等）
- 截圖品質越清晰，識別效果越好
- 建議一張截圖只包含一個主題的對話
- 看不清的內容會標註 `[看不清楚]` 或 `[模糊]`

### 技術圖片模式（--image）

將架構圖、流程圖、記憶體映射圖等技術圖片轉換為結構化知識：

```bash
python RAG.py npx6_architecture.png knowledge.json --image

# 執行後會顯示：
# [INFO] 使用 VL 模型分析技術圖片: npx6_architecture.png
# ============================================================
# [VL 模型分析結果完整內容]
# ============================================================
#
# 是否將此內容加入 knowledge.json？ [Y/n]: y
# [INFO] 新增圖片知識: image_npx6_architecture.png
# ...
```

**處理流程**：
1. 使用 VL 模型分析圖片內容
2. **顯示完整分析結果**（讓你確認內容正確）
3. 詢問是否加入知識庫
   - 是 → 生成 embedding 並入庫
   - 否 → 結束
4. 存入知識庫（來源標記為 `image_<檔名>`）

**自動整理格式**：
- 原始文字摘錄（圖中可辨識的文字）
- 圖片概述
- 主要元件/模組
- 連接關係/資料流
- 位址/數值資訊（如有）
- 重要細節
- 相關術語

**適用類型**：
- 系統架構圖 / 方塊圖
- 記憶體映射圖 / 位址空間
- 硬體連接圖 / 介面圖
- 流程圖 / 狀態機
- 資料流程圖
- 時序圖

**與 --chat 的差異**：

| 模式 | 用途 | Prompt 重點 |
|------|------|------------|
| `--chat` | 聊天對話截圖 | 整理對話重點、步驟、注意事項 |
| `--image` | 技術圖片 | 分析元件、連接關係、位址數值 |

**注意事項**：
- 複雜圖片（混合對話+架構圖）建議手動整理成 txt/md
- VL 模型對圖形關係的理解有限，重要資訊建議人工確認
- 位址、數值等精確資訊可能需要校正
- 看不清的內容會標註 `[模糊]`

### 網頁模式（--url）

將網頁內容轉換為知識庫，適合技術文件網站、API 文件等：

```bash
python RAG.py https://docs.banana-pi.org/zh/BPI-F3 knowledge.json --url

# 執行後會顯示：
# [INFO] 正在抓取網頁: https://docs.banana-pi.org/zh/BPI-F3
# [INFO] 網頁標題: BPI-F3 - Banana Pi Wiki
# ============================================================
# [網頁 Markdown 內容完整顯示]
# ============================================================
#
# 是否將此內容加入 knowledge.json？ [Y/n]: y
# [INFO] 新增網頁知識: url_docs_banana-pi_org_BPI-F3
# ...
```

**處理流程**：
1. 檢查網路連線，若連線失敗會立即通知
2. 使用 `html2text` 將 HTML 轉換為乾淨的 Markdown
3. 自動清理：移除導航列、頁尾、JavaScript 連結等雜訊
4. **顯示完整抓取結果**（讓你確認內容正確）
5. 詢問是否加入知識庫
   - 是 → 生成 embedding 並入庫
   - 否 → 結束
6. 存入知識庫（來源標記為 `url_<網域>_<路徑>`，並保存標題和抓取時間）

**使用場景**：
- 技術文件網站（API 文件、SDK 文件）
- 產品規格頁面
- Wiki 或知識庫頁面
- 部落格技術文章

**錯誤處理**：
- **連線失敗**：無法連接到伺服器時，會顯示錯誤訊息並終止
- **HTTP 錯誤**：404、403 等錯誤會明確提示
- **逾時**：超過 30 秒未回應會自動終止

**注意事項**：
- 需要安裝 `html2text` 套件：`pip install html2text`
- 僅擷取單一頁面，不會自動爬取子頁面
- 動態載入的內容（JavaScript 渲染）可能無法擷取
- 部分網站可能有反爬蟲機制，導致擷取失敗
- 建議優先使用官方提供的 PDF/Markdown 文件

**與手動複製的差異**：

| 方式 | 優點 | 缺點 |
|------|------|------|
| `--url` 模式 | 自動清理雜訊、格式統一 | 動態內容可能遺漏 |
| 手動複製貼上 | 可選擇性複製 | 常混入圖片檔名、導航連結等雜訊 |

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
KNOWLEDGE_FILE = "knowledge.json"       # 預設知識庫檔案
KNOWLEDGE_TOP_K = 5                     # 返回的參考數量
KNOWLEDGE_THRESHOLD = 0.30              # 相關度門檻

# 嚴格模式
STRICT_MODE = True                      # 是否啟用嚴格模式
WEAK_REF_THRESHOLD = 0.35               # REF 太弱時拒答的門檻
SKIP_LOW_CONFIDENCE_KB = True           # 低信心度時不注入 KB context
LOW_CONFIDENCE_KB_THRESHOLD = 0.30      # 低信心度門檻

# 搜尋優化
USE_RERANKER = True                     # 啟用 Reranker
USE_HYBRID_SEARCH = True                # 啟用混合搜尋
USE_QUERY_EXPANSION = True              # 啟用 Query Expansion
USE_MMR = True                          # 啟用 MMR 多樣性選擇
MMR_LAMBDA = 0.7                        # MMR 多樣性權重（0=多樣 1=相關）
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
