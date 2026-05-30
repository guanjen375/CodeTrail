# MCP 工具清單

這份文件列出 CodeTrail MCP server 暴露給 OpenCode / Codex CLI 的工具，以及工具使用原則。

[回到 README](../README.md)。

---

## CodeTrail 暴露的 17 個 MCP 工具

你不用手動寫 JSON 或自己呼 API。這些工具會出現在 frontend 的 MCP 工具列表裡；日常用法是在對話中直接要求模型「用工具 `<工具名>` 做某件事」。多數情況只講工具名就夠了，模型會自己補預設參數；需要指定檔案、行號、搜尋範圍時，再把那些條件寫進自然語言。

### 最常用講法

| 你想做什麼 | 在 frontend 裡可以這樣說 | 主要工具 |
|---|---|---|
| 先看 repo 長什麼樣 | 請用工具 `list_dir` 看專案結構，找 entry point、測試和設定檔。 | `list_dir(...)` |
| 不知道程式在哪 | 請先用工具 `code_rag_search` 搜尋「初始化流程」，再用工具 `read_file` 讀最相關檔案。 | `code_rag_search(...)`、`read_file(...)` |
| 找某個字串或錯誤訊息 | 請用工具 `grep_code` 搜尋錯誤訊息「panic: xxx」，範圍限 C/C++ 檔，並顯示上下文。 | `grep_code(...)` |
| 讀一個已知檔案 | 請用工具 `file_info` 看 `src/main.py` 大小，再用工具 `read_file` 讀前 120 行。 | `file_info(...)`、`read_file(...)` |
| 查已匯入的 spec | 請用工具 `query_knowledge` 查 reset timing 限制，回答要附 REF。 | `query_knowledge(...)` |
| 查不能答錯的規格數字 | 請用工具 `query_knowledge_strict` 查 reset assert 最小時間，證據不夠就拒答。 | `query_knowledge_strict(...)` |
| 看專案外的截圖/PDF/log | 請先用工具 `import_external_file` 匯入 `~/Downloads/error.png`，再分析回傳的新路徑。 | `import_external_file(...)` |
| 看圖片、ELF、firmware | 請用工具 `analyze_file` 分析 `.aicode_uploads/error.png`，做 OCR 或 binary 分析。 | `analyze_file(...)` |
| 把文件/圖片/binary 加進 KB | 請用工具 `ingest_document` 匯入 `docs/spec.pdf`（或 `arch.png`、`firmware.bin`），完成後用工具 `reload_knowledge_base`。 | `ingest_document(...)`、`reload_knowledge_base()` |
| 移除舊文件 | 請用工具 `remove_document` 移除 `old_spec.pdf`，完成後用工具 `reload_knowledge_base`。 | `remove_document(...)` |
| 準備改檔 | 請先用工具 `git_status` 和 `git_diff` 確認目前變更，再說明要改哪些檔案。 | `git_status(...)`、`git_diff(...)` |
| 套修改 | 請產生最小 unified diff，先用工具 `apply_patch` 預覽，再正式套用。 | `apply_patch(...)` |
| 修改後檢查 | 請用工具 `run_lint` 檢查剛改的檔案，再用工具 `run_command` 跑最小相關測試。 | `run_lint(...)`、`run_command(...)` |

### 依任務分類

| 類型 | 工具 | 白話用途 |
|---|---|---|
| 專案探索 | `list_dir(path=".", depth=2)` | 看目錄樹，不要叫模型跑 `ls` |
| 專案探索 | `code_rag_search(query, top_k=5)` | 用「這段程式在做什麼」去找可能的函式/class |
| 專案探索 | `grep_code(pattern, path=".", include=None, context=0)` | 搜錯誤訊息、函式名、設定名 |
| 專案探索 | `file_info(path)` | 讀檔前先看大小，避免一次塞爆 context |
| 專案探索 | `read_file(path, start_line=1, end_line=None, max_chars=50000)` | 讀檔案內容，長檔要分段 |
| 文件/外部檔案 | `import_external_file(path, dest_name=None)` | 把允許來源的外部檔案複製進 `.aicode_uploads/` |
| 文件/外部檔案 | `analyze_file(path)` | OCR 圖片、分析 ELF 或 firmware blob |
| 文件/外部檔案 | `ingest_document(path, mode="auto")` | 把 PDF / MD / TXT / 圖片(png/jpg/...) / binary(bin/elf/...) 匯入 `knowledge.json`；`mode` 預設依副檔名自動選，可顯式 `image` / `chat` / `binary` / `document` |
| 文件/外部檔案 | `remove_document(source)` | 從 KB 移除過期文件 |
| 文件/外部檔案 | `reload_knowledge_base()` | 讓剛匯入或刪除的 KB 內容立即生效 |
| 文件/外部檔案 | `query_knowledge(question)` | 查 KB，適合 spec / manual / datasheet |
| 文件/外部檔案 | `query_knowledge_strict(question)` | 查高風險規格題，弱證據會拒答 |
| 修改/驗證 | `git_status()` | 看工作樹目前有沒有改動 |
| 修改/驗證 | `git_diff(path=None, staged=False)` | 看修改內容，不需要用 `run_command` 跑 git |
| 修改/驗證 | `apply_patch(diff, dry_run=False)` | 套 unified diff，會真的寫檔 |
| 修改/驗證 | `run_lint(path, fix=True)` | 對單一檔案跑格式化/lint；`fix=False` 走 check-only(不改檔) |
| 修改/驗證 | `run_command(cmd)` | 跑白名單內的測試 / lint;build 命令(make/cmake/ninja/meson/bazel)需設 `AI_CODE_ENABLE_BUILD_COMMANDS=1` |

### 使用原則

- 找程式碼時，先請模型用工具 `code_rag_search` 或 `grep_code`，再用工具 `read_file`。
- 長檔先用工具 `file_info` 看大小，再要求工具 `read_file` 分段讀。
- 查 spec 先用工具 `query_knowledge`；數字、限制、預設值這類答錯很糟的題目，用工具 `query_knowledge_strict`。
- 外部檔案先用工具 `import_external_file`，再用工具 `analyze_file`、`ingest_document` 或 `read_file` 處理匯入後路徑。
- 新增或刪除文件後一定要用工具 `reload_knowledge_base`。
- 改檔前先看工具 `git_status` / `git_diff`；改檔用工具 `apply_patch`。
- 工具 `apply_patch` 和 `run_command` 有副作用；需要改檔或執行專案腳本時才允許。

---

