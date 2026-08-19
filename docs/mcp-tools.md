# MCP 工具清單

這份文件列出 CodeTrail MCP server 暴露給 OpenCode 的工具，以及工具使用原則。

[回到 README](../README.md)。

---

## CodeTrail 暴露的 18 個 MCP 工具

你不用手動寫 JSON 或自己呼 API。這些工具會出現在 frontend 的 MCP 工具列表裡；日常用法是在對話中直接要求模型「用工具 `<工具名>` 做某件事」。多數情況只講工具名就夠了，模型會自己補預設參數；需要指定檔案、行號、搜尋範圍時，再把那些條件寫進自然語言。

判斷有沒有真的執行,要看 frontend 的工具卡 / 回傳結果或結構化 `tool_use` event,不要看模型如何描述自己的工具清單。`/status` 的 Connected 只證明 MCP transport 已連線;模型輸出 `<codetrail_list_dir .../>` 之類純文字後自行宣稱成功,仍是假呼叫。完整診斷見 [Connected 但沒有實際 tool call](troubleshooting.md#mcp-connected-but-no-tool-call)。

### 最常用講法

| 你想做什麼 | 在 frontend 裡可以這樣說 | 主要工具 |
|---|---|---|
| 先看 repo 長什麼樣 | 請用工具 `list_dir` 看專案結構，找 entry point、測試和設定檔。 | `list_dir(...)` |
| 不知道程式在哪 | 請先用工具 `code_rag_search` 搜尋「初始化流程」，再用工具 `read_file` 讀最相關檔案。 | `code_rag_search(...)`、`read_file(...)` |
| 想看呼叫鏈 | 請用工具 `code_rag_search`,mode 設 "path",query 寫 "main -> uart_send",把每一步的檔案與行號列出來。 | `code_rag_search(query="main -> uart_send", mode="path")` |
| 找某個字串或錯誤訊息 | 請用工具 `grep_code` 搜尋錯誤訊息「panic: xxx」，範圍限 C/C++ 檔，並顯示上下文。 | `grep_code(...)` |
| 讀一個已知檔案 | 請用工具 `file_info` 看 `src/main.py` 大小，再用工具 `read_file` 讀前 120 行。 | `file_info(...)`、`read_file(...)` |
| 查已匯入的 spec | 請用工具 `query_knowledge` 查 reset timing 限制，回答要附 REF。 | `query_knowledge(...)` |
| 查不能答錯的規格數字 | 請用工具 `query_knowledge_strict` 查 reset assert 最小時間，證據不夠就拒答。 | `query_knowledge_strict(...)` |
| 看專案外的截圖/PDF/log | 請先用工具 `import_external_file` 匯入 `~/Downloads/error.png`，再分析回傳的新路徑。 | `import_external_file(...)` |
| 看圖片、PDF、ELF、firmware | 請用工具 `analyze_file` 分析 `.aicode_uploads/error.png`（或 `docs/spec.pdf`），做通用 VL 圖片分析、PDF 一次性抽文字或 binary 分析。 | `analyze_file(...)` |
| 把文件/圖片/binary 加進 KB | 請用工具 `ingest_document` 匯入 `docs/spec.pdf`（或 `arch.png`、`firmware.bin`）。之後查詢會自動載入；想立即確認 chunk 數再補 `reload_knowledge_base`。 | `ingest_document(...)`、`reload_knowledge_base()` |
| 移除舊文件 | 請用工具 `remove_document` 移除 `old_spec.pdf`（查詢會自動偵測變更）。 | `remove_document(...)` |
| 準備改檔 | 請先用工具 `git_status` 和 `git_diff` 確認目前變更，再說明要改哪些檔案。 | `git_status(...)`、`git_diff(...)` |
| 套修改 | 請產生最小 unified diff，先用工具 `apply_patch` 預覽，再正式套用。 | `apply_patch(...)` |
| 修改後檢查 | 請用工具 `run_lint` 檢查剛改的檔案，再用工具 `run_command` 跑最小相關測試。 | `run_lint(...)`、`run_command(...)` |
| 糾正模型的做事方式 | (糾正它之後)請用工具 `record_lesson` 把這條記成行為規則,之後的 session 都要遵守。 | `record_lesson(...)` |

### 依任務分類

| 類型 | 工具 | 白話用途 |
|---|---|---|
| 專案探索 | `list_dir(path=".", depth=2)` | 看目錄樹，不要叫模型跑 `ls` |
| 專案探索 | `code_rag_search(query, top_k=5, mode="semantic", hops=1, include_evidence=False)` | 用「這段程式在做什麼」去找可能的函式/class。`mode="neighbors"`(query 放 symbol 名看 1–2 hop 呼叫關係;放 repo 相對檔案路徑如 `src/uart.c` 看該檔的 include/import 關係);`mode="path"`(query 寫 `"SRC -> DST"`)拿跨檔案呼叫鏈,每一步都附 `檔:行` 證據且只走確定解析的邊(同名多候選的歧義邊不入鏈);`include_evidence=True` 讓 semantic 結果多帶分數組成 / parser backend / graph 1-hop 關係。graph 對 C/C++(tree-sitter)與 Python 抽 definitions / includes / calls;**首次建置是顯式動作**(終端跑 `python code_graph.py --root <AICODE_ROOT>`;graph 尚未建立或損壞時 graph 模式報錯、semantic 不受影響),建好後查詢自動增量;function pointer 與 macro 間接呼叫會誠實標 unresolved,不亂猜目標 |
| 專案探索 | `grep_code(pattern, path=".", include=None, context=0)` | 搜錯誤訊息、函式名、設定名 |
| 專案探索 | `file_info(path)` | 讀檔前先看大小，避免一次塞爆 context |
| 專案探索 | `read_file(path, start_line=1, end_line=None, max_chars=50000)` | 讀檔案內容，長檔要分段 |
| 文件/外部檔案 | `import_external_file(path, dest_name=None)` | 把允許來源的外部檔案複製進 `.aicode_uploads/` |
| 文件/外部檔案 | `analyze_file(path)` | 用 VL 分析各類圖片、一次性抽 PDF 文字（不入 KB）、分析 ELF 或 firmware blob |
| 文件/外部檔案 | `ingest_document(path, mode="auto")` | 把 PDF / MD / TXT / 圖片(png/jpg/...) / binary(bin/elf/...) 匯入 `knowledge.json`；`mode` 預設依副檔名自動選，可顯式 `image` / `chat` / `binary` / `document`。PDF 內嵌圖自動經 VL 入庫（`origin="diagram"` 降權；任一張 VL 失敗整份不入庫、KB 不變） |
| 文件/外部檔案 | `remove_document(source)` | 從 KB 移除過期文件 |
| 文件/外部檔案 | `reload_knowledge_base()` | 立即載入 KB 並回報 chunk 數（查詢本身會自動偵測變更，這是「馬上確認」用） |
| 文件/外部檔案 | `query_knowledge(question, source=None)` | 查 KB；`source` 可用 basename 限定單一 spec/manual |
| 文件/外部檔案 | `query_knowledge_strict(question, source=None)` | 查高風險規格題，弱證據會拒答；可限定文件 |
| 修改/驗證 | `git_status()` | 看工作樹目前有沒有改動 |
| 修改/驗證 | `git_diff(path=None, staged=False)` | 看修改內容，不需要用 `run_command` 跑 git |
| 修改/驗證 | `apply_patch(diff, dry_run=False)` | 套 unified diff，會真的寫檔 |
| 修改/驗證 | `run_lint(path, fix=True)` | 對單一檔案跑格式化/lint；`fix=False` 走 check-only(不改檔) |
| 修改/驗證 | `run_command(cmd)` | 跑白名單內的測試 / lint;build 命令(make/cmake/ninja/meson/bazel)需設 `AI_CODE_ENABLE_BUILD_COMMANDS=1` |
| 行為教訓 | `record_lesson(rule, scope="project")` | 你糾正模型行為後,把糾正「提案」成一條行為規則;經你核准(permission ask)寫入 lessons store,之後 session 注入 context([docs/lessons.md](lessons.md)) |

### 使用原則

- 找程式碼時，先請模型用工具 `code_rag_search` 或 `grep_code`，再用工具 `read_file`。
- 問「誰呼叫了 X」「X 怎麼一路呼叫到 Y」時,用 `code_rag_search` 的 `mode="neighbors"`(query 放 symbol 名)/ `mode="path"`;問「這個檔直接 include 了誰」時,`mode="neighbors"` 的 query 放 repo 相對檔案路徑。回傳的關係每一步都有 `檔:行` 證據,unresolved(function pointer / macro 間接呼叫)與歧義候選(同名多定義)會明講。graph 首次建置要在終端跑一次 `python code_graph.py --root <AICODE_ROOT>`(沒建就查 graph 模式會明確報錯,semantic 不受影響);建好之後查詢自動偵測檔案變更做增量更新,安裝 tree-sitter grammar 或改 `AICODE_H_LANG` 後會自動整體重建。
- 檔案變更偵測有一個 30 秒的快照窗(`AICODE_CODE_RAG_REFRESH_TTL`,設 0 關閉):透過 CodeTrail 工具(`apply_patch` / `run_command` / `run_lint`)寫檔會立即失效重掃;**在外部編輯器改檔**則最長 30 秒內的查詢可能還看到舊索引,屬既知取捨。
- 長檔先用工具 `file_info` 看大小，再要求工具 `read_file` 分段讀。
- 查 spec 先用工具 `query_knowledge`；數字、限制、預設值這類答錯很糟的題目，用工具 `query_knowledge_strict`。多份相似版本並存時傳 `source="檔名"`，filter 會在 top-k 前套用。
- 外部檔案先用工具 `import_external_file`，再用工具 `analyze_file`、`ingest_document` 或 `read_file` 處理匯入後路徑。
- 新增或刪除文件後查詢會自動載入變更；要立即確認 chunk 數可用工具 `reload_knowledge_base`。
- 改檔前先看工具 `git_status` / `git_diff`；改檔用工具 `apply_patch`。
- 工具 `apply_patch` 和 `run_command` 有副作用；需要改檔或執行專案腳本時才允許。
- 工具 `record_lesson` 只在「你糾正了模型的做事方式」之後用;工具報錯或答案錯誤不是觸發條件。寫入需要你核准,細節與管理指令見 [docs/lessons.md](lessons.md)。

---
