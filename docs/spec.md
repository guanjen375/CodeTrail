# 智能程式碼分析器 - 規格說明

## 1. 支援的程式語言與檔案類型

### 程式碼檔案
- Python: `.py`, `.pyx`, `.pyi`
- C/C++: `.c`, `.cpp`, `.h`, `.hpp`, `.cc`, `.cxx`
- JavaScript/TypeScript: `.js`, `.ts`, `.jsx`, `.tsx`
- Go: `.go`
- Rust: `.rs`
- Java/Kotlin: `.java`, `.kt`
- Shell: `.sh`, `.bash`

### 設定檔
- JSON: `.json`
- YAML: `.yaml`, `.yml`
- TOML: `.toml`
- INI: `.cfg`, `.ini`, `.conf`

### 文件
- Markdown: `.md`
- 純文字: `.txt`

## 2. 模型與 Context 設定

### 預設模型
- 主模型: `qwen3-coder:30b` (用於程式碼分析)
- 視覺模型: `qwen3-vl:30b-a3b` (用於圖片分析)
- Embedding: `bge-m3`
- Reranker: `qllama/bge-reranker-v2-m3`

### Context 長度建議
- 5090 32GB + 192GB RAM: 可開 128K，VRAM 不足時自動 offload 到 RAM
- 純 GPU 模式: 建議 64K 以內避免 OOM
- 注意：Offload 到 RAM 會降低推理速度（主要是首 token 延遲）

### NUM_CTX 設定指南
- 小型專案 (<50 檔案): 16K-32K 足夠
- 中型專案 (50-200 檔案): 32K-64K
- 大型專案 (>200 檔案): 64K-128K
- 若遇到 OOM，優先降低 NUM_CTX 而非換小模型

### OOM 問題排解
若執行時出現 OOM (Out of Memory)：
1. **首選方案**: 將 NUM_CTX 降到 64K 或 32K
2. 啟用 GPU offload：讓部分 context 卸載到系統 RAM
3. 若已設 128K 以上：建議先降到 64K 測試
4. 注意：降低 NUM_CTX 比換小模型更能保持回答品質

## 3. Agent 工具使用限制

### 工具回合數
- 預設最大工具回合數: 10 (`MAX_TOOL_LOOPS`)
- Bug 類問題建議: 8-12 輪
- Code 類問題建議: 6-8 輪

### 檔案讀取限制
- 單次讀取上限: 50000 字元 (`MAX_FILE_READ_CHARS`)
- Grep 結果上限: 30 筆 (`MAX_GREP_RESULTS`)
- 目錄列出深度: 3 層 (`MAX_LIST_DEPTH`)

## 4. 安全限制

### Patch 功能
- 預設停用，需透過 `--patch` 或 `AI_CODE_PATCH=1` 啟用
- 單次最多修改 5 個檔案
- 單一檔案最多修改 200 行

### Run Command 功能
- 預設停用，需透過 `--run-tests` 或 `AI_CODE_RUN_TESTS=1` 啟用
- 執行 timeout: 60 秒
- 只允許白名單命令: pytest, cargo test, go test 等

## 5. RAG 知識庫設定

### 相關度門檻
- 一般問題: 0.30 (`KNOWLEDGE_THRESHOLD`)
- 短問題 (<10 token): 0.25 (`KNOWLEDGE_THRESHOLD_SHORT`)
- Code RAG 一般: 0.35 (`CODE_RAG_THRESHOLD`)
- Code RAG Bug 類: 0.25 (`CODE_RAG_THRESHOLD_BUG`)

### 嚴格模式
- 當問題包含「依文件」「規格」「spec」等關鍵字時自動啟用
- 嚴格模式下溫度設為 0.0
- 若 REF 分數低於 0.35，會警告引用可靠性不足
