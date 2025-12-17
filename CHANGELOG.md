# Changelog

本專案的版本變更記錄。

## [3.0.0] - 2025-12-17

重大架構升級與使用者體驗優化版（個人里程碑）

### Added
- **Context 完整性告示系統**：所有「上下文不完整」的情況都會明確提示
  - Full 模式：LLM prompt 內加入 `[CTX_NOTICE]`，列出 skipped/skeleton 檔案清單
  - Agent 模式：工具輸出被摘要/截斷時對 user 顯示一次警告
  - QA 模式：對話歷史裁切時提示 user
  - grep/read_file：達到上限時在結果尾端加警告
  - REF 內容截斷時顯示原長度
- **Context 使用量顯示**：每次呼叫 LLM 前顯示 `[CTX] ~N tokens (X%)`，方便調整 knowledge/附件長度
  - 90% 以上顯示「接近上限」警告
  - 100% 以上顯示「超出上限，將被截斷」警告
- ELF 分析強化：新增 section 大小分析、symbol 統計、entry point 資訊
- Binary 分析：ASCII 藝術檢測、重複模式識別

### Changed
- Context 使用量百分比改為相對於 `NUM_CTX`（而非 `MAX_MESSAGES_BUDGET`），更精確反映實際截斷風險
- `_trim_messages_to_budget()` 回傳 `(messages, did_trim)` tuple，支援截斷警告

### Fixed
- 修正 eval 工具的 import 錯誤（`do_strict_mode` → `answer_with_self_check`）
- 修正 Windows 環境下的 UTF-8 編碼問題

---

## [0.2.0] - 2025-12-07

穩定性與除錯體驗強化版

### Added
- Stack trace 快路徑：BUG 類問題可直接根據 `file:line` 讀取對應程式碼，跳過多輪工具迴圈
- Ollama 健康檢查：在 eval 前檢查 `/api/tags`/`/api/ps`，偵測到異常時可選擇自動 `systemctl restart ollama`
- 工具使用上限：預設 `MAX_READ_FILE_CALLS=15`、`MAX_GREP_CALLS=10`，避免 Agent 無限制讀檔

### Changed
- eval CODE 類評分規則改為以 `symbol + file` 為主，`line` 與回答中的 `file:line` 為加分題，行號容忍度放寬至 ±15
- BUG 類評分支援 `cause_keywords`，可用多個關鍵字描述錯誤原因（如 `IndexError` / `list index out of range`）

### Documentation
- 新增 `docs/spec.md` 並透過 `RAG.py` 生成 `knowledge.json`（32 chunks），整理 NUM_CTX 設定建議、OOM 排解等規格
- 在 SPEC 類 eval 題目中覆蓋 NUM_CTX 推薦值（64K/32K）與 patch/安全相關規則

### Testing
- 建立初始 regression 測試集（SPEC/CODE/BUG 共 14 題），目前在 v0.2.0 達成 100% 通過率（平均分數 0.86）

## [0.1.0] - 2025-12-06

初始發布版本

### Added
- 完整模式：小型專案（< 200KB）一次讀入全部程式碼分析
- Agent 模式：大型專案動態探索，按需讀取檔案
- 網頁模式：直接分析 GitHub/GitLab/Bitbucket 上的程式碼（測試中）
- 知識庫 RAG：整合技術文件（PDF/Markdown），回答時引用文件來源
- Code RAG：自動索引程式碼符號（函式/類別），支援 AST/Tree-sitter 解析 + Reranker 二次排序
- 圖片 OCR：支援截圖中的錯誤訊息辨識（使用 VL 模型）
- 二進位分析：韌體/執行檔的 Hex dump + 智能字串提取
- 嚴格模式（兩階段）：規格類問題強制引用文件，並透過自我檢查過濾幻覺
- 改碼閉環：apply_patch（含 context 驗證）、git_status、git_diff、run_lint（自動執行）
- 容器化執行：在 Docker/Podman 中安全執行測試命令
- 資料飛輪：收集互動記錄（含 tool_calls/files_read）用於後續 fine-tuning
- 動態 Context：根據 prompt 長度自動調整 num_ctx，減少延遲
