# MCP 工具清單

這份文件列出 CodeTrail MCP server 暴露給聊天客戶端的工具，以及工具使用原則。

[回到 README](../README.md)。

---

## CodeTrail 暴露的 21 個 MCP 工具

live `tools/list` 的順序是公開契約：`list_dir`、`read_file`、`grep_code`、
`code_rag_search`、`file_info`、`query_knowledge`、`query_knowledge_strict`、`query_table`、
`git_status`、`git_diff`、`apply_patch`、`run_lint`、`run_command`、`analyze_file`、
`ingest_document`、`remove_document`、`reload_knowledge_base`、`review_figures`、`review_text`、
`import_external_file`、`record_lesson`。名稱與順序的唯一來源是
`mcp_contract.PUBLIC_TOOL_ORDER`；文件、canary 與 eval 都讀同一份契約。

你不用手動寫 JSON 或自己呼 API。這些工具會出現在 frontend 的 MCP 工具列表裡；日常用法是在對話中直接要求模型「用工具 `<工具名>` 做某件事」。多數情況只講工具名就夠了，模型會自己補預設參數；需要指定檔案、行號、搜尋範圍時，再把那些條件寫進自然語言。

判斷有沒有真的執行,要看畫面上的 `· <工具> → completed` 或結構化 `tool_use` event,不要看模型如何描述自己的工具清單。`/tools` 列得出來只證明 MCP transport 已連線;模型輸出 `<list_dir .../>` 之類純文字後自行宣稱成功,仍是假呼叫。完整診斷見 [列得出工具但沒有實際 tool call](troubleshooting.md#mcp-connected-but-no-tool-call)。

### 最常用講法

| 你想做什麼 | 在 frontend 裡可以這樣說 | 主要工具 |
|---|---|---|
| 先看 repo 長什麼樣 | 請用工具 `list_dir` 看專案結構，找 entry point、測試和設定檔。 | `list_dir(...)` |
| 不知道程式在哪 | 請先用工具 `code_rag_search` 搜尋「初始化流程」，再用工具 `read_file` 讀最相關檔案。 | `code_rag_search(...)`、`read_file(...)` |
| 想一次取得有限推導證據 | 請用工具 `code_rag_search`,mode 設 "context",query 寫要分析的問題；省略 max_chars 會依目前 n_ctx 配置結果預算。 | `code_rag_search(query="...", mode="context")` |
| 想看呼叫鏈 | 請用工具 `code_rag_search`,mode 設 "path",query 寫 "main -> uart_send",把每一步的檔案與行號列出來。 | `code_rag_search(query="main -> uart_send", mode="path")` |
| 找某個字串或錯誤訊息 | 請用工具 `grep_code` 搜尋錯誤訊息「panic: xxx」，範圍限 C/C++ 檔，並顯示上下文。 | `grep_code(...)` |
| 讀一個已知檔案 | 請用工具 `file_info` 看 `src/main.py` 大小，再用工具 `read_file` 讀前 120 行。 | `file_info(...)`、`read_file(...)` |
| 查已匯入的 spec | 請用工具 `query_knowledge` 查 reset timing 限制，回答要附 REF。 | `query_knowledge(...)` |
| 查不能答錯的規格數字 | 請用工具 `query_knowledge_strict` 查 reset assert 最小時間，證據不夠就拒答。 | `query_knowledge_strict(...)` |
| 看專案外的截圖/PDF/log | 請先用工具 `import_external_file` 匯入 `~/Downloads/error.png`，再分析回傳的新路徑。 | `import_external_file(...)` |
| 看圖片、PDF、ELF、firmware | 請用工具 `analyze_file` 分析 `.aicode_uploads/error.png`（或 `docs/spec.pdf`），做通用 VL 圖片分析、PDF 一次性抽文字或 binary 分析。 | `analyze_file(...)` |
| 深入看一個 ELF | 請用工具 `analyze_file` 分析 `build/app.elf`，`view` 設 "symbols"、`target` 設 "uart"（或 `view` 設 "disasm"、`target` 設 "Reset_Handler"；`view` 設 "dwarf"、`target` 設 "0x08001234"）。 | `analyze_file(path, view="symbols", target="uart")` |
| 把文件/圖片/binary 加進 KB | 請用工具 `ingest_document` 匯入 `docs/spec.pdf`（或 `arch.png`、`firmware.bin`）。之後查詢會自動載入；想立即確認 chunk 數再補 `reload_knowledge_base`。 | `ingest_document(...)`、`reload_knowledge_base()` |
| 圖很多的 PDF，先估成本 | 請用工具 `ingest_document` 對 `docs/datasheet.pdf` 設 `preflight_only=True`，回報候選數、VL 呼叫次數與是否超過上限。 | `ingest_document(path, preflight_only=True)` |
| 把 KB 重建成只有這一份文件 | 請用工具 `ingest_document` 匯入 `docs/spec_v2.pdf`，`fresh` 設 True，回報清掉幾個 chunk、保留幾筆 human_verified。 | `ingest_document(path, fresh=True)` |
| 覆核 PDF 抽出來的表格 / log | 請用工具 `review_figures` 列出待覆核的圖，說明每一張的原因；我看過原圖再決定要不要修。 | `review_figures(action="list")`、`review_figures(action="fix", ...)` |
| 移除舊文件 | 請用工具 `remove_document` 移除 `old_spec.pdf`（查詢會自動偵測變更）。 | `remove_document(...)` |
| 準備改檔 | 請先用工具 `git_status` 和 `git_diff` 確認目前變更，再說明要改哪些檔案。 | `git_status(...)`、`git_diff(...)` |
| 套修改 | 請產生 SEARCH/REPLACE 區塊（path 一行、`<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE` 各自獨佔一行，SEARCH 逐字抄檔案現況；或最小 unified diff），先用工具 `apply_patch` 的 `dry_run` 預覽，再正式套用。 | `apply_patch(...)` |
| 修改後檢查 | 請用工具 `run_lint`（`fix=False`）檢查剛改的檔案，再用工具 `run_command` 跑最小相關測試——`apply_patch` 不會代跑，這兩步各自需要你核准。 | `run_lint(...)`、`run_command(...)` |
| 糾正模型的做事方式 | (糾正它之後)請用工具 `record_lesson` 把這條記成行為規則,之後的 session 都要遵守。 | `record_lesson(...)` |

### 依任務分類

| 類型 | 工具 | 白話用途 |
|---|---|---|
| 專案探索 | `list_dir(path=".", depth=2)` | 看目錄樹，不要叫模型跑 `ls` |
| 專案探索 | `code_rag_search(query, top_k=5, mode="semantic", hops=1, include_evidence=False, max_chars=None, build_target="")` | 依語意定位 symbol、建立 bounded evidence，或查 call/include graph；省略 max_chars 時 transport 依 n_ctx 配置，四種模式與保守解析契約見下節 |
| 專案探索 | `grep_code(pattern, path=".", include=None, context=0)` | 搜錯誤訊息、函式名、設定名；複雜 regex 會退回字面搜尋，並有 30 筆 match、單行 500 字元與整體 200,000 字元的硬上限，截斷會明示標記 |
| 專案探索 | `file_info(path)` | 文字回報行數與字元數；二進位/非文字回報 bytes，支援的格式導向 `analyze_file` |
| 專案探索 | `read_file(path, start_line=1, end_line=None, max_chars=None)` | 讀檔案內容；省略 max_chars 時依 n_ctx 配置，長檔依結果的精確 start_line 分段 |
| 文件/外部檔案 | `import_external_file(path, dest_name=None)` | 把允許來源的外部檔案複製進 `.aicode_uploads/` |
| 文件/外部檔案 | `analyze_file(path, view="summary", target="", limit=0)` | 用 VL 分析各類圖片、一次性抽 PDF 文字（不入 KB）、分析 ELF 或 firmware blob。ELF 預設給總覽；`view` 可切到 `symbols` / `disasm` / `dwarf` / `strings` / `sections` / `memmap` / `relocs` / `imports` / `dynamic` / `headers`；另有多來源核對 `consistency`（見[記憶體一致性](memory-consistency.md)）。一般視角的 `target` 指定 symbol、0x 位址、regex 或 `key:value` 篩選，`limit` 控制筆數（上限 5000）；單次輸出上限 25,000 字元，截斷會指出該用哪個 view 縮小範圍。缺少或無法載入 pyelftools 時直接回錯誤；細節見[analyze_file 的 ELF 視角](#analyze_file-的-elf-視角) |
| 文件/外部檔案 | `ingest_document(path, mode="auto", preflight_only=False, fresh=False, mineru_content_list=None, mineru_pdf_sha256=None, resume=True, redo_pages=None, redo_figures=None, retry_failed=False)` | 把 PDF / MD / TXT / 圖片(png/jpg/...) / binary(bin/elf/...) 匯入 `knowledge.json`；`mode` 預設依副檔名自動選，可顯式 `image` / `chat` / `binary` / `document`。PDF 的原生表格 / 向量文字 log 與純 raster 截圖、掃描頁、方塊圖都走結構化抽取；raster 會先分類為 table / terminal / prose / diagram，再帶 canonical payload、證據、品質與驗證狀態。**單張抽壞只讓那一張缺席**（其餘 figure 與全部文字 chunk 照常入庫，結果會列出是哪幾張，`review_figures(action="list")` 看得到 `in_kb=false`）；整份零 KB 寫入的是 VL 連不上／逾時、預算超限、capability probe 未過、來源檔中途被換掉這類契約破裂。`preflight_only=True` 只估成本、零寫入（僅 .pdf）。`fresh=True` 一步到位重建：清空既有 chunks、讓舊 embeddings cache 失效、只留這一份文件（同一次原子提交，失敗全回滾）。**不會為了 reset 去整批清除** `.codetrail/figures/`（ingest 本來就會寫入這一次的 run，提交後也可能依 retention 回收該文件沒被 KB 引用的舊 run — 那與 fresh 無關）。同一份文件再 ingest 時人工修正會沿用；但**被移出 KB 的其他文件之後重新 ingest 不會自動恢復人工確認**（revision 退回 1）。不可與 `preflight_only` 併用。**執行期間 server 不會被卡住**：跑在 worker thread、每 2 秒送一次零內容的 MCP progress；同一時間所有 KB 工具與第二個 ingest 會立刻回「稍後重試」（不排隊）。結果的標頭下會帶這一次 run 的待辦（`[CODETRAIL_ACTION_REQUIRED]`：待人工判斷 / 需修復或品質排除 / 無法覆核 / 抽取失敗 / 可行動缺席，各附下一步；沒有待辦就完全不印），逾時、非零 exit 或輸出不完整則帶 `[CODETRAIL_INGEST_FAILED]` 並回 `status: error` |
| 文件/外部檔案 | `review_figures(action="list", document_id="", figure_id="", expected_revision=0, payload_json="", confirm_against_image=False)` | 覆核 PDF 結構化抽取的表格 / 終端機 log / diagram：`list` 唯讀列出 figure_id、頁碼、bbox、kind、驗證狀態、品質、人工確認狀態、處置、原因、原圖路徑與 canonical payload；`fix` 只收該 kind schema 的 structured payload + `expected_revision`，`confirm_against_image=True` 才升 `human_verified`。permission 設 `ask` |
| 文件/外部檔案 | `query_table(document_id="", figure_id="", register="", address="", row=None, column="", register_column="", address_column="")` | 按 register／位址／列欄精確查目前可信表格，附來源與版本；歧義不選第一筆，詳見 [表格查值](text-and-table-review.md) |
| 文件/外部檔案 | `review_text(action="list", source="", text_id="", expected_revision=0, expected_sha256="", text="", confirm_against_source=False)` | OCR 正文的 list／show／correct／confirm／revoke；修改綁版本與雜湊，permission 設 ask，詳見 [正文覆核](text-and-table-review.md) |
| 文件/外部檔案 | `remove_document(source)` | 從 KB 移除過期文件 |
| 文件/外部檔案 | `reload_knowledge_base()` | 立即載入 KB 並回報 chunk 數（查詢本身會自動偵測變更，這是「馬上確認」用） |
| 文件/外部檔案 | `query_knowledge(question, source=None)` | 查 KB；`source` 可用 basename 限定單一 spec/manual |
| 文件/外部檔案 | `query_knowledge_strict(question, source=None)` | 查高風險規格題，弱證據會拒答；可限定文件 |
| 修改/驗證 | `git_status()` | 看工作樹目前有沒有改動；非 git 專案回固定的跳過通知（`status: ok`，不是錯誤、不用重試） |
| 修改/驗證 | `git_diff(path=None, staged=False)` | 看修改內容，不需要用 `run_command` 跑 git；非 git 專案同樣回跳過通知 |
| 修改/驗證 | `apply_patch(diff, dry_run=False)` | 套 SEARCH/REPLACE 或 unified diff（同一次只能一種；參數已是字串，不要包 fence），會真的寫檔；最多 5 個檔案、單檔 200 行（udiff 算 added+removed；S/R 算 payload budget = SEARCH+REPLACE 行數，不是同一種計數）；UTF-8 strict，BOM／CRLF／檔尾換行／權限原樣保留，mixed newline 與 symlink 拒絕；套用後只做唯讀 syntax check（advisory、三態、失敗不回滾）；細節見[apply_patch 的兩種格式](#apply_patch-的兩種格式) |
| 修改/驗證 | `run_lint(path, fix=True)` | 對單一檔案跑格式化/lint；`fix=False` 走 check-only(不改檔) |
| 修改/驗證 | `run_command(cmd, timeout=60)` | 跑白名單中的裸命令；timeout 只接受整數 1..600 秒（server 端上限；client 可能更早截止），預設 60。預設為測試／靜態命令；build 命令(make/cmake/ninja/meson/bazel)需在 `client.json` 設 `"build_commands": true`。`/allow list` 查看，`/allow add <絕對目錄>` 驗證後寫入 `extra_allowed_command_dirs`，同 session 後續命令立即生效；相容既有 `extra_allowed_commands` PATH 名稱設定。每次重讀授權並驗目錄，失效或重名即拒絕；容器不能執行本機目錄工具。既有核准、readonly 與參數檢查仍適用；git 用 `git_status` / `git_diff` |
| 行為教訓 | `record_lesson(rule, scope="project")` | 你糾正模型行為後,把糾正「提案」成一條行為規則;經你核准(permission ask)寫入 lessons store,之後 session 注入 context([docs/lessons.md](lessons.md)) |

### `code_rag_search` 四種模式

四種模式都接受 `build_target="已匯入的target"`，共同限制 translation units、headers、
巨集與條件分支。回傳 `build_context`（target／fingerprint／未知原因）與 `build_state`。
未選 target 保留全 repo 搜尋並標 unknown；不存在的 target 直接報錯。沒有命中時會回
`[{"mode":"semantic","results":[],"build_context":...}]`，避免漏掉未知狀態。
匯入與 generated headers 的精確准入見 [build-context.md](build-context.md)。

- `mode="semantic"`：用自然語言找 function / class / method / macro / typedef / enum /
  translation-unit / namespace-scope global。`include_evidence=True` 時加上分數組成、
  parser backend、confidence、
  graph status 與最多 5 條一跳關係；預設維持精簡回傳。
- `mode="context"`：先取 semantic seeds，再加入 confirmed 1-hop caller / callee /
  include 與相關 test / header / config / trace lexical evidence，去重後裝進固定字元 budget。
  `max_chars` 合法範圍為 `2000..30000`；省略時 transport 依目前 `n_ctx` 的 12% 配置
  結果預算（core Python API 仍保留歷史 12,000 字元行為）。`used_chars` 只計
  `evidence[].text`，不是 tokenizer token。candidate、graph traversal 與 character budget 的
  截斷會分開回報。歧義、unresolved 與 Python attribute-call heuristic 只進
  `uncertainties`，不算 confirmed。graph 缺席或損壞時，lexical（grep / index）候選仍會
  參與選取，實際 evidence 仍受既有 candidate 與字元 budget 約束；只有呼叫關係證據缺席，
  `graph_status` 標示原因，`uncertainties` 會列出
  `呼叫關係證據不可用（relationship evidence unavailable: graph unavailable）；未看到 caller/callee 不代表不存在`
  （graph 查詢途中出錯時 `graph unavailable` 改為 `graph degraded`）。必要 parser / rg
  依賴失敗則整次回錯誤，不適用此資料缺席處理。
- `mode="neighbors"`：query 放 symbol 名可看 1–2 hop 關係；放 repo 相對檔案路徑
  （例如 `src/uart.c`）可看 include / import 關係。
- `mode="path"`：query 寫 `"SRC -> DST"`，回傳最多 3 條、最長 4 hop 的最短呼叫鏈。
  每一步都附 `path:line`，只走 confirmed edge；同名歧義與 heuristic edge 不會混入鏈。

graph 使用 tree-sitter 解析 C/C++，並解析 Python definitions / imports / calls。C/C++
definition 涵蓋函式、帶 body 的 class / struct / union、macro、typedef、enum / enum
constant 與 translation-unit / namespace scope global；純 prototype、`extern` 宣告、forward
tag、member / local variable 不算 definition。解析會尊重 linkage、實際 include visibility、
qualified identity 與可證明的 preprocessor condition；不夠確定的 function pointer、macro
間接呼叫或條件候選維持 unresolved。

graph 首次建置是顯式動作。DB 不存在或 schema 過舊時，錯誤訊息會附一條含實際 MCP
Python、CodeTrail `code_graph.py` 絕對路徑與實際 project root 的可複製命令；手動形式為：

```bash
<MCP_PYTHON> <CODETRAIL_REPO>/code_graph.py --root <SANDBOX_ROOT>
```

舊版 DB 可用同一命令 transactionally 升級；DB 損壞則先移到不會 commit 的備份位置再建。
建好後會偵測檔案變更，依 visibility / callable catalog 影響選擇增量或完整重建。

Code-RAG 索引預設排除 ignored、虛擬環境、`third_party` / `vendor` / `external` / `build`
類目錄與自己的 cache；
這不會改變 `grep_code` / `list_dir`。要先看實際索引範圍，可在 CodeTrail checkout 跑
唯讀、離線且預設不印路徑的統計：

```bash
python3 scripts/index_stats.py --root <SANDBOX_ROOT>
```

部署層需要額外 include / exclude 時才使用
`~/.config/codetrail/index-scope.json`；schema 與 matcher 細節見
[README_DEV 的索引範圍章節](../README_DEV.md#索引範圍-index-scope)。這份檔不得放進
target repo，必須維持 owner-only 權限（POSIX `chmod 600`）；pattern 本身可能洩漏 NDA
目錄結構。

### 可續跑、精確表格與正文覆核

`ingest_document` 新增 `resume=True`、`redo_pages=None`、`redo_figures=None`、
`retry_failed=False`。三種選擇式重做互斥且只用於 PDF，不能搭配 fresh、preflight 或
resume=False。頁碼從 1 起算，figure 使用精確 ID；重做後仍提交完整文件。
來源、模型或設定不相符時不會沿用舊結果，詳見 [ingest-resume.md](ingest-resume.md)。

`query_table` 與 `review_text` 都受 ingest busy 閘保護；前者唯讀且不呼叫模型，後者採人工
ask 並由 readonly server 拒絕。細節見 [text-and-table-review.md](text-and-table-review.md)。

`analyze_file(view="consistency", linker_map=..., linker_script=..., preload_log=...,
dram_config=..., map_format="auto")` 提供多來源記憶體核對。所有附加路徑都須在沙箱，
格式、缺失證據與精確區間見 [memory-consistency.md](memory-consistency.md)。

### 結果文字與預算契約

客戶端只把每次工具結果唯一的那個精簡文字 block 送進模型。第一行固定是
`status: ok|partial|error`；只有 partial、error 或截斷結果才在第二行給可操作的 `next:`。
`read_file` 的 next 會給實際下一個 `start_line`，`grep_code`／`list_dir` 的 next 會要求縮小
path、include、pattern 或 depth。`list_dir` 超過字元上限時先逐層降低 depth，回傳較淺層的
完整清單並標 partial（淺層檔案不會被截掉）；降到 depth 0 仍超過才截字元。錯誤的修復方式一定存在文字 block，不能只放在
`structuredContent`。

`analyze_file` 回傳的 `[ELF 錯誤]`（例如缺少依賴或 objdump 失敗）與
`run_command` 的非零 exit，都回報 `status: error` 與 MCP `isError=true`，
TUI 顯示 error。成功輸出的正文即使含有錯誤字樣，也不會因此被判定為工具失敗。

未明示 `max_chars` 時，結果 token 代理預算是目前 `n_ctx` 的 12%，估算固定為
`ceil(ASCII 字元數 / 3) + ceil(非 ASCII 字元數 × 1.5)`。明示值仍受工具既有安全上限
（`read_file` 50,000、`list_dir` 20,000、`code_rag_search` 30,000 字元）；高於 12% 預設
預算時文字結果會標 `context_risk`。

`code_rag_search`、`query_knowledge`、`query_knowledge_strict`、`query_table` 另外保留 core payload
於 `structuredContent`，供會採用它的 MCP client 使用；文字 renderer 不重複輸出
`text`／`display`／`refs` 三份同義內容。其他文字工具不宣告 `{"result": string}`
outputSchema，避免同一 payload 被 SDK 重複序列化。

### PDF 圖片:結構化抽取與人工覆核

北極星是 **verified-or-abstain**:程式能以獨立證據確認,內容才進可信檢索;不能確認就保留
原圖、頁碼、框與格/行位置,正文放 `▯` 並記原因,或該份 PDF 零寫入。raster 上被遮住或低於
解析度的字元沒有任何程式能還原真值,能保證的只有「正確,或誠實拒絕」。

**只有一條 lane,沒收就是缺席(重要)**

結構化管線內分 `native`／`vl` 來源，摘要分別標出原生通道缺失、原生核對失敗、VL 失敗與
文字轉錄回退。coverage 只計已偵測區域，不能當成全 PDF OCR 完整率。空表格／terminal
回退為 `prose` 逐行轉錄；仍為空就記失敗。有可靠來源位置才算來源行的覆蓋率，否則為 unknown。
程式碼與檔案樹優先保存逐行符號與縮排；章節／圖表目錄的確定導覽範圍不進檢索或脈絡生成，
混合頁正文與有資訊價值的檔案樹保留。舊 KB 需重新 ingest 才套用新判斷。

| 情況 | 收哪些候選 | 產出 | 有沒有 `▯` / 逐格證據 / strict gate |
|---|---|---|---|
| 結構化 lane 收錄 | 原生 markdown 表格、`find_tables` 幾何、框線格、對齊文字帶、向量文字 log，以及夠大的純 raster / picture | raster 先分類成 table / terminal / prose / diagram；再產生 canonical JSON + 衍生文字 chunk | 有 |
| 判定不是圖面 | 封面、logo、商標、裝飾線條、產品照片、單純的 GUI 圖示 | 零抽取、零 chunk（只留在 review artifact 與缺席清單） | 不適用 |
| lane 沒收 | 沒有結構性證據的區域、超出上限的候選、整頁 abstain 的頁 | **不入庫**，ingest 列出頁碼 / bbox / 原因 | 不適用 |

被拍成圖或掃描進來的表格與終端機會出現在 `review_figures`，但沒有獨立原生證據時通常是
`unverified` / `needs_review`，strict 查詢仍會擋下，直到人工對原圖確認。整頁散文走
`prose`（逐行轉錄），`diagram` 也有自動分類與 structured producer。2026-08-30 移除了舊的
自由文字 VL 相容 lane（既有 KB 的 `origin="diagram"` chunk 仍照原語意保留）。

figure chunk 會帶所在章節與 caption（`Table 3-1 …`）。兩者只當檢索訊號（embedding + BM25
+ REF 上一行標示），不進 canonical payload。原生表格被取代後留在文字層的 marker，在查詢期
會被跟回去，把對應的 figure chunk 一併帶進 REF。

品質與人工確認分开呈現：`quality_grade` 為 `usable / formatting_only / partial /
structure_error / unusable / unknown`，`review_state` 為 `unreviewed / confirmed`。
`auto_disposition` 分 `accept / manual_review / repair_required / excluded`：已知缺字、衝突與
缺行列入修復，重要結構錯誤／全不可讀不作為新 figure 入庫，仍保留完整轉錄與原圖供追查。
`fix` 必須先修掉內容損壞，再明示對照原圖確認；不得把仍有缺字的 payload 標成可信。
若目前 revision 已由可靠來源證明轉錄缺漏（`transcription_source_incomplete`），清單會標
`fixable=False` 並說明須重新 ingest 核對來源；現有 fix 不具完整來源，不能確認補字是否完整。
品質不替代原有 strict gate；`usable` 也不代表完整 OCR 或已經人工確認。

**六種 `verification_status`**(structured chunk 的驗證來源／信任狀態；抽取是否完成另看
`extraction_status ∈ {complete, failed, skipped}`)

| 狀態 | 意思 | strict 查詢用不用 |
|---|---|---|
| `native_verified` | 原生表格 geometry 與**至少另一個原生** evidence channel 在 row/cell 結構與 critical token 上一致（單次 `find_tables().extract()` 不算） | ✔ |
| `corroborated` | 視覺抽取與獨立 PDF 文字/幾何證據**逐格或逐行**一致。terminal 的比對走空白正規化,所以**不等於**逐位元組一致（PDF 文字層證明不了 tab vs 多個 space） | ✔ |
| `human_verified` | 你對**指定 revision** 的原圖明確確認/修正,且修正後的 payload 通過 validator | ✔ |
| `needs_review` | 有 `▯`、衝突、漏 row/line、tile 縫合不確定、kind 歧義或截斷 | ✘ |
| `unverified` | 結構合法、未發現衝突,但沒有獨立證據（無 anchor 的同模型多次取樣即使全等也只到這級） | ✘ |
| `legacy_unverified` | 舊 KB 缺欄位的 figure chunk,含所有既有的 VL diagram / 圖片 chunk | ✘ |

後三種合稱 **flagged** —— 那是查詢時的 filter,**不是第七種狀態**。一張圖切成多個 chunk 時,
聚合一律取**最差**的成員狀態。

**查詢端的差別**

- `query_knowledge_strict`:flagged 的圖片內容在 **code 層**就被排除,不進 REF、也不影響門檻
  計算,所以嚴格模式不會用未驗證的圖片數值回答 register / bit range / 規格數字。被排除的那些
  會出現在回傳的 `excluded_figures`（帶 source / page / figure_id / figure_index / kind /
  狀態 / 原因）與 `review_hint`,**四條回傳路徑都有**。全部候選都被擋下時你仍看得到「哪一頁、
  哪一張圖可用但待覆核」,不會變成「查不到」的假象。
- `query_knowledge`:可以回未驗證內容,但 REF 與 machine-readable metadata 都帶 status /
  reasons / row 或 line range / truncation,不是只靠 prompt 提醒模型。REF 因預算截斷時會顯示
  實際的 row/line 範圍與總數,不會讓你以為整張 log 都在。
- 與文字抽取的 REF 衝突時**不宣稱哪一邊必勝**:兩邊的數值與出處都會列出,並標明衝突未解。

**preflight(圖多的 PDF 先跑這個)**

```text
請用工具 ingest_document 匯入 docs/datasheet.pdf,preflight_only 設 True,
回報候選數、tile 數、VL 呼叫次數、image token 估計,以及有沒有超過上限。
```

它在**任何 VL 呼叫、embedding 與 KB 寫入之前**算完就結束,零寫入。超過上限會直接停下並
指出是哪一項(上限是 `config.py` 的 `FIGURE_*` 常數;改它是改 repo)。
MCP 每次工具呼叫有 client timeout,開始之後才超時等於沒有提示 —— 所以先估。

preflight 涵蓋所有結構化候選，包含純 raster 的分類、雙樣本抽取與 image-token 估算。
沒被收成候選的區域不進預算——它們根本不會被送出去，報告改在「不會進 KB 的頁 / 區域」
那一段逐筆列出。

**抽壞的那一張缺席**:結構化 lane 的 schema / validator / row width / line contract /
`finish_reason` 任一最終不合格 → **那一張圖不進 KB**(沒有自由文字退路),其餘 figure 與
全部文字 chunk 照常入庫,stdout 會印一行 `[figure] 失敗 N 張(不進 KB)` 說明是哪幾張。
仍然**整份 PDF 零寫入**的是:VL 連不上 / 逾時、預算超限、capability probe 未過、來源檔中途
被換掉,以及候選與結果對不上這類契約破裂;舊 KB 與向量保持原狀。需要 VL 的候選會在
動 KB 之前先做 capability probe(端點真的吃 image content part、接受 nested `json_schema`、
能完成一張極小且不含機敏內容的 canary 並通過外部 validator),不通過就 fail-loud 指出缺哪
一項,不以「OpenAI-compatible」推定品質。

**覆核流程**

```text
請用工具 review_figures,action 設 "list",列出待覆核的圖與原因。
（挑一張之後）請用 review_figures,action 設 "list",figure_id 設 <上面那個>,
把 canonical payload 完整貼出來。
```

改完再送回:`review_figures(action="fix", figure_id=..., expected_revision=<list 給的
revision>, payload_json=<改過的 JSON>, confirm_against_image=True)`。要點:

`list` 也會列出**抽取失敗、因此沒有進 KB 的圖**(標 `in_kb: False` / `fixable: False`,
從 review artifacts 讀)——缺席就是缺席,不會有一份猜出來的內容頂替,但失敗原因看得到。

- **只收該 kind 的 structured payload**,拒絕自由文字全段替換;JSON 物件不得有重複 key
  （Python 只留最後一個 = 無聲改寫）。
- `kind` 以 **KB 記錄的為準**,payload 自報的 kind 不符直接拒絕。
- `expected_revision` 必填。revision 已被別人改過 → 回 **conflict、零寫入**,不做
  last-write-wins;重新 list 看現況後再送。
- `confirm_against_image=True` 代表**你看著原圖確認過**。只把機器轉寫貼回來不算 ——
  `human_verified` 是使用者的確認,不是模型的自證。所以它的 permission 是 `ask`。
- 全流程:validate → render → kind-aware 重切 chunk → 重算受影響的 embedding / id / hash →
  exclusive lock 內確認 revision 未變 → 原子替換。任一步失敗,舊 chunks / 向量 / manifest
  全部保持可用。

**review artifacts 與 NDA**:`<專案>/.codetrail/figures/<document_slug>/<run_id>/` 存 canonical
manifest、原始 asset、`variants/`、`review_assets/` 與 `review.md`。**可能含 NDA 內容**;
`.gitignore` 已含 `.codetrail/`,不要 commit。

兩組影像**不一樣,不要混用**:`variants/`(對應 `variant_paths`)是**實際送給模型的**;
`review_assets/`(對應 `review_asset_paths`)是只為了讓人覆核而 render 的,**從未送給模型**。
`list` 每一張都會標 `crop_is_model_input`,只有標「模型輸入」的才是模型看過的那張。
native lane(原生表格,零 VL 呼叫)**沒有任何模型影像輸入**,它的 crop 一律只供覆核。

`FIGURE_REVIEW_MAX_RUNS_PER_DOC`(預設 5)是 **soft retention target,不是硬上限**:
被 KB `evidence_ref` 引用、`created_at` 判讀不出來或清理失敗的 run 一律 fail-closed 保留,
實際份數可能更多。**不要拿它當 NDA 影像份數的保證**;要確定清掉就顯式刪除對應目錄並確認結果。
手動清除方式與後果見
[RAG、附件與知識庫操作](rag.md#pdf-內的表格與終端機畫面結構化抽取--人工覆核)。

### 本地 MinerU 文字 lane

已有本地 MinerU 產物時，PDF 可選用其文字閱讀順序與 `text_level` 標題。
先在產物生成時記下 PDF SHA-256，保留對應的 flat `content_list.json`，再把兩份檔案
放進專案沙箱。只接受官方 legacy flat list；v2 巢狀格式不猜轉換。

```text
請用 ingest_document 匯入 docs/spec.pdf，
mineru_content_list 設 docs/mineru/content_list.json，
mineru_pdf_sha256 設產物生成時記錄的 64 位 SHA-256。
```

CLI 同樣需要成對參數，也適用單份 PDF 的 `rebuild`：

```bash
python3 RAG.py docs/spec.pdf knowledge.json \
  --mineru-content-list docs/mineru/content_list.json \
  --mineru-pdf-sha256 <產物生成時記錄的SHA256>
```

可加 `--preflight` 只驗來源與估算既有 figure lane 成本，零 KB／VL／embedding 寫入。
不提供 MinerU 參數就維持 native 文字流程；明示的產物缺漏、錯版、SHA 不符或途中換檔
都會失敗，不自動換 lane。不讀產物裡的 `img_path`，也不啟動或下載 MinerU，沒有雲端路徑。

一份文件只認 MinerU 的標題來源，頁碼為 `page_idx + 1`，缺頁保留原頁號並揭露。
圖表的章節以 page + bbox 配對；幾何不唯一時不猜標題。文字／code／terminal 由 MinerU
提供，既有 prose／terminal 圖面保留 artifact 與品質問題，不重複成另一份 KB 文字。
表格和 diagram 仍由既有 structured lane 收錄；MinerU 的 HTML 表格不再另建 chunk。
找不到唯一且已收錄的表格 owner、或正文與圖表無法安全分開時整份失敗，可省略 MinerU
參數改用 native lane。

MinerU 文字預設未獨立驗證。`query_knowledge` 的 REF 顯示 `text_lane=mineru`；
`query_knowledge_strict` 排除未符合驗證條件的段落，以 `metadata.excluded_text` 列來源、
頁碼與原因。經 `review_text` 對照來源確認後，只有目前內容、來源版本與品質均有效的
段落可進 strict；校字本身不等於確認。詳見 [正文覆核](text-and-table-review.md)。
這不提高任何 figure 的驗證／品質，也不代表全 PDF OCR 完成。

章節召回與 chunk 召回以 RRF 合併。命中節點會把整節 chunk 加入候選並去重，
通過各自證據門檻的成員全數送進本地 reranker；最後仍受 top-k 與 REF 預算限制。
節點分數不作 strict 證據，且不替代 chunk 層。舊 KB 的節點從現有 chunk metadata／正文
重建，無須重解析 PDF；新 schema 的 cache 重算需本地 embedding server，失敗就停止。

### `analyze_file` 的 ELF 視角

`analyze_file` 對 ELF（`.elf/.so/.o/.axf/.out/.ko`，以及內容是 ELF magic 的 `.bin`）不再只有一份固定摘要。`view` 選視角、`target` 指定要展開的東西、`limit` 控制筆數；每次輸出仍受 25,000 字元硬上限，但截斷訊息會明講「這是哪個 view、該用 target / limit 縮小範圍」，不會默默砍掉。

| `view` | 內容 | `target` 寫法 |
|---|---|---|
| `summary`（預設） | Key Facts（架構 / 型別 / entry 對應的 symbol / stripped / linkage / symbol 統計含 LOCAL·UND·size=0 / relocation 數 / DWARF / 記憶體估算）、ELF header、LOAD segment、`.dynamic`、`.modinfo`、entry 反組譯（失敗會列原因）、Top functions（含 LOCAL/static）、imports、relocation 統計、DWARF CU、字串分類（version / diagnostic / format / url / path / command / config） | 不用 |
| `symbols` | 完整 symbol 表：LOCAL / GLOBAL / WEAK、UND、size=0 全列，欄位 addr / size / type / bind / section / name（C++ 名稱附 demangle） | regex（`uart`）、篩選（`bind:LOCAL type:FUNC`、`ndx:UND`、`section:.text`、`table:.dynsym`，可混用）、`0x位址` = 反查落在哪個 symbol |
| `disasm` | 反組譯；只用 `client.json` 指定的 `objdump`，未指定則用 PATH 的 `objdump`；缺席、失敗或不支援架構即報錯 | symbol 名、`0x位址`、`0x起-0x迄`、`0x位址+bytes`；省略 = entry point；`.o/.ko` 可加 `section:.init.text`；`limit` = 指令數（預設 48、上限 1000） |
| `dwarf` | 無 target：CU 列表（producer / 語言 / 位址範圍）與函式統計；regex：函式（low/high pc、來源檔:行、external/inline）＋ struct / union / class / enum / typedef 成員（offset、型別、bit field）；`0x位址`：對應來源檔:行與函式 | regex、`0x位址`、`kind:func` / `kind:type` |
| `strings` | 全部可讀字串（ASCII 全檔 + UTF-16LE 前 4MB）含 offset、所屬 section、分類 | regex、`cat:diagnostic`（或 version / format / url / path / command / config / other）、`section:.rodata`、`min:12`、`enc:utf16` |
| `sections` | 全部 section（type / addr / offset / size / flags / 所屬 segment）；指定 section 時給 hex dump + 字串 + 內含 symbol | section 名、`0x位址`；`limit` = dump bytes（預設 512、上限 4096） |
| `memmap` | LOAD segment 的 VMA / LMA、section→segment、FLASH（Σ filesz）/ RAM（Σ 可寫 memsz）/ zero-init（Σ memsz−filesz）估算（規則明寫在輸出）、Cortex-M 向量表解讀（初始 SP、Reset、例外與 IRQ 對應的 handler symbol） | 不用；`limit` = IRQ 向量數 |
| `relocs` | relocation：各 section 的數量與 type 統計、被引用最多的 symbol（標 UND）；指定 target 時逐筆列出並標出 caller 函式（`.o/.ko` 的呼叫關係證據） | `*`（全部逐筆，含沒有 symbol 的 `R_*_RELATIVE`）、regex（symbol / caller / type）、`section:.rela.text`、`type:R_ARM_CALL`、`0x<offset>` |
| `imports` | 外部 symbol（`.dynsym` UND；`.o/.ko` 用 `.symtab` UND）依 API 家族分類，附 relocation 引用次數 | regex |
| `dynamic` | `.dynamic` 全部 tag（NEEDED / SONAME / RPATH / RUNPATH / FLAGS / INIT_ARRAY…） | regex |
| `headers` | ELF header、全部 program headers、section→segment、notes、`.comment`、`.modinfo` | 不用 |
| `consistency` | 核對 ELF、GNU／MetaWare linker map／script、preload 與 DRAM 配置，附確切區間與來源證據；格式及參數見 [記憶體一致性](memory-consistency.md) | 不接受 `target`／`limit`，使用 `linker_map`／`linker_script`／`preload_log`／`dram_config` 限定證據 |

- ELF 解析只使用 **pyelftools**（`requirements.txt` 已列入），包括已存在快取的查詢。缺席或載入失敗時回錯誤，不產生較差報告。C++ mangled symbol 需要 `c++filt`；無法執行、逾時或回應格式錯誤也會報錯。
- 跨架構反組譯需要可處理該 ELF 的 objdump。請自行安裝合適的 binutils，並在 `client.json` 設 `"objdump": "/path/to/arm-none-eabi-objdump"` 等實際路徑。程式不自動尋找其他 cross binary，也不改用 Capstone。
- `ingest_document` 對 ELF 走**長版**多視角報告（summary + 完整 symbol 表 + relocation 逐筆含 caller + DWARF CU / 函式 / 型別 + 全部分類字串 + memmap / sections / imports / dynamic），每個 view 以 `config.BIN_ELF_INGEST_MAX_CHARS`（400,000 字元）為**渲染階段的字元預算**（行容器達預算即停止收行，表格 / segment 分類這類來源都是 generator、預算用完就不再往下拉，所以各 view 在模型之外的中間資料與輸出預算成正比——模型本身的 symbol 表、字串清單、relocation 早就在記憶體裡，各有自己的安全上限；筆數上限則由「字元預算 ÷ 該 view 的最短行長」推出——symbols 一行至少 40 字元、imports 一行至少 5 字元，各自拿到不同的上限，都不可能比字元預算先到，也不會讓候選 heap / 清單長到遠超過預算能放的量），整份再以同一上限做比例分配，不受 `analyze_file` 單次 25K 的限制。沒超過上限就是完整（容器不預扣任何空間，剛好放得下的報告一個字都不少）；超過時各段**依比例截斷、各自註明**（原行數 / 字元數與該用哪個 view 分批查），不會有整段消失。`analyze_file` 的每個 view 也同樣在渲染階段以 25K 為字元預算，達到時才從尾端回收剛好夠的空間放一行說明（略過了幾行）。Cortex-M 向量表固定最多 512 筆（有向量 section 時以其大小為準）。解析層仍有安全上限並會在報告內註明：relocation 每個 section 保留前 20,000 筆（統計為全量）、DWARF 函式 30,000 個、型別 2,000 個（每個型別 256 個成員）、字串 100,000 條。
- `target` 的 regex 只接受**正面表列的安全子集**（逐字元驗證，不是 heuristic）：字面、`.`、`[...]`、`^ $ \b`、**最上層**的 `|`（≤ 8 分支）、單一 atom 的 `* +`（合計 ≤ 1）與 `?`（合計 ≤ 3）；**不收任何群組**（`(uart|spi)_init` 請寫成 `uart_init|spi_init`——群組串接才會產生指數級的切分方式，例如 30 個連續 `(a|aa)`），也不接受 `{n,m}`、backreference、lookaround、inline flag、超過 200 字元。不在子集內的樣式會改成**字面比對**並在輸出註明原因。比對主體只看每個字串 / 名稱的前 300 個字元，所以單次比對的成本有上界（約 起點 × 分支 × 主體長 × 2^可選 ≈ 6×10⁶ 步；Python 的 `re` 沒有 timeout、不釋放 GIL，災難性回溯會卡住整個 MCP server）。另外 symbols / relocs / strings 的篩選有 20 秒預算，每一筆都檢查、零匹配也會停，逾時中止並標明結果不完整——那是「很多筆加起來太久」的保險，不是硬 timeout。
- `objdump` 與 `c++filt` 以 `LC_ALL=C` 執行；工具依賴錯誤會一路傳到 MCP 的 `isError` 或 ingest 失敗狀態，不會被當成「没有 symbol」或部分成功。
- `view` / `target` / `limit` 只對 ELF 有效；對圖片、PDF、非 ELF 二進位會被忽略並在回覆開頭註明。

### apply_patch 的兩種格式

`apply_patch` 的 `diff` 參數同一次只接受一種格式；參數已是字串，**不要再包 Markdown fence**；混用、孤立 marker、fence、marker 外的說明文字都會被拒絕。路徑一律 repo-relative POSIX（`src/led.c`），兩種格式的規則不同：SEARCH/REPLACE 的 path 必須是 canonical 寫法——拒絕絕對路徑、Windows drive／UNC、反斜線、`/dev/null`、NUL 或控制字元，也拒絕 `.`／`..` component、`//` 與結尾 `/`（不做正規化、不 strip 前後空白）；unified diff 的 `--- a/` / `+++ b/` 路徑沿用既有正規化——容忍 `./` 前綴與重複 `/`（`.` component 會被丟掉），但同樣拒絕絕對路徑、drive、反斜線與 `..`。過去 `a/../b.py` 只要 resolve 進 root 就會被接受，現在兩種格式一律拒絕（安全面的行為變更）。

**格式 A — SEARCH/REPLACE**（建議本地模型優先使用；不需要行號、不需要 context 前綴）：

```text
src/led.c
<<<<<<< SEARCH
void led_toggle(void) {
    gpio_write(LED_PIN, !gpio_read(LED_PIN));
}
=======
void led_toggle(void) {
    gpio_toggle(LED_PIN);
}
>>>>>>> REPLACE
```

- path 是 marker 前一個非空行；三個 marker 必須各自獨佔完整的一行、逐字相同。內容本身需要一整行同樣的 marker 時改用 unified diff。
- SEARCH 逐行 exact 比對，只容忍行尾空白；縮排不同就是不匹配，工具不會拿相似的位置代套。SEARCH 在檔案中出現多處 → 拒絕（S/R 沒有行號提示，請多帶幾行讓它唯一）；多個區塊都對同一份原始檔定位，互相重疊 → 拒絕。
- 空 SEARCH（marker 之間沒有任何行）= 建立新檔，只在目標不存在、該檔恰一個區塊、REPLACE 非空時成立；檔案已存在（含 0 byte）一律拒絕。新檔為 LF、無 BOM，權限交給 umask。

**格式 B — unified diff**（`--- a/f` / `+++ b/f` / `@@`）：定位靠 context 內容，行號選填、不必計算行數；多處匹配靠 `@@` 行號提示消歧、已套用過的 hunk 會跳過、純新增沒有 context 時只能靠行號且必須落在檔案範圍內。

**上限**：最多 5 個檔案；udiff 單檔 200 行（added+removed）；S/R 單檔 payload budget = SEARCH 行數 + REPLACE 行數（同檔所有區塊合計）≤ 200——兩者不是同一種計數。

**檔案安全（兩格式相同）**：既有檔以 UTF-8 strict 讀取，非 UTF-8 → 整份 patch 拒絕、零寫入；BOM、CRLF、檔尾有無換行、權限位元原樣保留；CR-only 或 mixed newline 一律拒絕；目標或路徑上有 symlink 一律拒絕。單檔寫入：既有檔 = 同目錄唯一 temp（`O_EXCL` 建立、fsync）＋ `os.replace` 原子替換（寫入前重驗 preimage 未變）；新檔 = 同目錄 temp ＋ 不覆寫發布（必須支援 hard link；同名檔在 preflight 後冒出就中止；不支援 hard link 時整批中止並回滾）；缺少 dir_fd / `O_NOFOLLOW` / `O_DIRECTORY` 的平台拒絕操作。多檔是「全量 preflight＋失敗時 best-effort rollback」，不是跨檔交易——第二檔 preflight 失敗時第一檔也不會被改，寫入途中失敗會盡力還原已寫入的檔案，還原不了的項目如實回報。

**dry_run**：只做 preflight、零副作用（不建目錄、不留 temp、不跑驗證），逐檔固定回報 `format`、檔案清單、`blocks`（區塊數）、`budget`（payload 用量／上限）、`locations`（定位行）、`new_file`（是否新建）；全部通過才顯示唯一的一行 `would apply`。

**不匹配時的回饋**：回覆會附最接近位置的檔案現況（逐行編號）與第一個差異（期望／實際），提示從現況逐字重建 SEARCH／context 後重送；40 行／2000 字元是同一次結果中**所有 mismatch 預覽的合計**上限，不是整份結果的長度上限；相似度只用來排名提示，絕不代套。

**套用後的驗證**：只做同一 process、唯讀的 syntax check（`.py`/`.pyi` 用 ast；C/C++ 只在 tree-sitter grammar 載入時檢查；缺 grammar 或不支援的副檔名 = skipped，不算通過）。結果三態：`✓ 驗證完成且通過` / `⚠ 驗證不完整` / `✗ 驗證未通過——patch 已套用、未回滾`；syntax 是 advisory gate，失敗不回滾。apply_patch 不會自動執行 lint / typecheck / test，也不會呼叫會改檔的 formatter；`PATCH_AUTO_VERIFY=False` 時連 syntax check 也不做。lint 與測試請另行呼叫 `run_lint(fix=False)` 與 `run_command`，它們各自需要獨立核准。

### 使用原則

- 分析、解釋、推導或找原因時，先用 `code_rag_search(mode="context")` 一次取得 bounded evidence；不足才做精準 `grep_code` / `read_file`，同一 query 不重複。
- 只想定位程式碼時，用工具 `code_rag_search` 或 `grep_code`，再用工具 `read_file`。
- 問「誰呼叫了 X」「X 怎麼一路呼叫到 Y」時,用 `code_rag_search` 的 `mode="neighbors"`(query 放 symbol 名)/ `mode="path"`;問「這個檔直接 include 了誰」時,`mode="neighbors"` 的 query 放 repo 相對檔案路徑。回傳的關係每一步都有 `檔:行` 證據,unresolved(function pointer / macro 間接呼叫)與歧義候選(同名多定義)會明講。graph 首次建置要在終端跑一次建立命令——沒建就查 graph 模式會明確報錯,**錯誤訊息就含完整可執行的那條命令**(實際 interpreter 與絕對路徑,直接複製貼上;semantic 不受影響);建好之後查詢自動偵測檔案變更做增量更新,安裝 tree-sitter grammar 或改 `config.H_LANG` 後會自動整體重建。graph 可用時先查 `neighbors`;`graph_status` 為 unavailable 時改用 `mode="context"` / `grep_code`,並把 caller coverage 標為不完整——不能因為沒看到呼叫者就推論沒有呼叫者。
- 檔案變更偵測有一個 30 秒的快照窗(`config.CODE_RAG_REFRESH_TTL_SECONDS`,設 0 關閉):透過 CodeTrail 工具(`apply_patch` / `run_command` / `run_lint`)寫檔會立即失效重掃;**在外部編輯器改檔**則最長 30 秒內的查詢可能還看到舊索引,屬既知取捨。
- 長檔先用工具 `file_info` 看大小，再要求工具 `read_file` 分段讀。
- 查 spec 先用工具 `query_knowledge`；數字、限制、預設值這類答錯很糟的題目，用工具 `query_knowledge_strict`。多份相似版本並存時傳 `source="檔名"`，filter 會在 top-k 前套用。
- 外部檔案先用工具 `import_external_file`，再用工具 `analyze_file`、`ingest_document` 或 `read_file` 處理匯入後路徑。
- 新增或刪除文件後查詢會自動載入變更；要立即確認 chunk 數可用工具 `reload_knowledge_base`。
- git 專案改檔前先看工具 `git_status` / `git_diff`（非 git 專案會回跳過通知，直接改檔）；改檔用工具 `apply_patch`（SEARCH/REPLACE 或 unified diff 二擇一，先 `dry_run` 預覽）。
- `apply_patch`（寫檔）、`run_lint(fix=True)`（格式化）、`run_command`（執行命令）是三個不同的 ask，各自需要你核准。apply_patch 不會自動執行 lint / typecheck / test；需要改檔或執行專案腳本時才允許。
- 工具 `record_lesson` 只在「你糾正了模型的做事方式」之後用;工具報錯或答案錯誤不是觸發條件。寫入需要你核准,細節與管理指令見 [docs/lessons.md](lessons.md)。
- 圖很多的 PDF 先用 `ingest_document(path, preflight_only=True)` 估成本（零寫入），再決定要不要在 MCP 裡跑或改走 CLI。
- REF 標「待覆核」的圖片內容不得當成規格數值的定論；`query_knowledge_strict` 的 `excluded_figures` 就是被 gate 擋下、但確實存在的圖，照實轉述頁碼與原因。**structured figure（`excluded_figures` 帶 `figure_id`）能用 `review_figures` 覆核**（`fix` 會改 KB，permission 是 `ask`）；新 ingest 的純 raster 也屬 structured figure。只有舊 KB 的 legacy VL chunk 沒有 canonical payload，不能在這裡覆核。被分類器判定「不是
圖面」的（封面、logo）不會進 KB、也不進覆核清單，它們只出現在 ingest 的缺席清單裡。

---
