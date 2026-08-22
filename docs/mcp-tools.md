# MCP 工具清單

這份文件列出 CodeTrail MCP server 暴露給 OpenCode 的工具，以及工具使用原則。

[回到 README](../README.md)。

---

## CodeTrail 暴露的 19 個 MCP 工具

你不用手動寫 JSON 或自己呼 API。這些工具會出現在 frontend 的 MCP 工具列表裡；日常用法是在對話中直接要求模型「用工具 `<工具名>` 做某件事」。多數情況只講工具名就夠了，模型會自己補預設參數；需要指定檔案、行號、搜尋範圍時，再把那些條件寫進自然語言。

判斷有沒有真的執行,要看 frontend 的工具卡 / 回傳結果或結構化 `tool_use` event,不要看模型如何描述自己的工具清單。`/status` 的 Connected 只證明 MCP transport 已連線;模型輸出 `<codetrail_list_dir .../>` 之類純文字後自行宣稱成功,仍是假呼叫。完整診斷見 [Connected 但沒有實際 tool call](troubleshooting.md#mcp-connected-but-no-tool-call)。

### 最常用講法

| 你想做什麼 | 在 frontend 裡可以這樣說 | 主要工具 |
|---|---|---|
| 先看 repo 長什麼樣 | 請用工具 `list_dir` 看專案結構，找 entry point、測試和設定檔。 | `list_dir(...)` |
| 不知道程式在哪 | 請先用工具 `code_rag_search` 搜尋「初始化流程」，再用工具 `read_file` 讀最相關檔案。 | `code_rag_search(...)`、`read_file(...)` |
| 想一次取得有限推導證據 | 請用工具 `code_rag_search`,mode 設 "context",query 寫要分析的問題,max_chars 設 12000；依 path:line 區分已證實和 uncertainty。 | `code_rag_search(query="...", mode="context", max_chars=12000)` |
| 想看呼叫鏈 | 請用工具 `code_rag_search`,mode 設 "path",query 寫 "main -> uart_send",把每一步的檔案與行號列出來。 | `code_rag_search(query="main -> uart_send", mode="path")` |
| 找某個字串或錯誤訊息 | 請用工具 `grep_code` 搜尋錯誤訊息「panic: xxx」，範圍限 C/C++ 檔，並顯示上下文。 | `grep_code(...)` |
| 讀一個已知檔案 | 請用工具 `file_info` 看 `src/main.py` 大小，再用工具 `read_file` 讀前 120 行。 | `file_info(...)`、`read_file(...)` |
| 查已匯入的 spec | 請用工具 `query_knowledge` 查 reset timing 限制，回答要附 REF。 | `query_knowledge(...)` |
| 查不能答錯的規格數字 | 請用工具 `query_knowledge_strict` 查 reset assert 最小時間，證據不夠就拒答。 | `query_knowledge_strict(...)` |
| 看專案外的截圖/PDF/log | 請先用工具 `import_external_file` 匯入 `~/Downloads/error.png`，再分析回傳的新路徑。 | `import_external_file(...)` |
| 看圖片、PDF、ELF、firmware | 請用工具 `analyze_file` 分析 `.aicode_uploads/error.png`（或 `docs/spec.pdf`），做通用 VL 圖片分析、PDF 一次性抽文字或 binary 分析。 | `analyze_file(...)` |
| 把文件/圖片/binary 加進 KB | 請用工具 `ingest_document` 匯入 `docs/spec.pdf`（或 `arch.png`、`firmware.bin`）。之後查詢會自動載入；想立即確認 chunk 數再補 `reload_knowledge_base`。 | `ingest_document(...)`、`reload_knowledge_base()` |
| 圖很多的 PDF，先估成本 | 請用工具 `ingest_document` 對 `docs/datasheet.pdf` 設 `preflight_only=True`，回報候選數、VL 呼叫次數與是否超過上限。 | `ingest_document(path, preflight_only=True)` |
| 覆核 PDF 抽出來的表格 / log | 請用工具 `review_figures` 列出待覆核的圖，說明每一張的原因；我看過原圖再決定要不要修。 | `review_figures(action="list")`、`review_figures(action="fix", ...)` |
| 移除舊文件 | 請用工具 `remove_document` 移除 `old_spec.pdf`（查詢會自動偵測變更）。 | `remove_document(...)` |
| 準備改檔 | 請先用工具 `git_status` 和 `git_diff` 確認目前變更，再說明要改哪些檔案。 | `git_status(...)`、`git_diff(...)` |
| 套修改 | 請產生最小 unified diff（`@@` 不必帶行號，修改行前後 2–3 行 context 即可），先用工具 `apply_patch` 預覽，再正式套用。 | `apply_patch(...)` |
| 修改後檢查 | 請用工具 `run_lint` 檢查剛改的檔案，再用工具 `run_command` 跑最小相關測試。 | `run_lint(...)`、`run_command(...)` |
| 糾正模型的做事方式 | (糾正它之後)請用工具 `record_lesson` 把這條記成行為規則,之後的 session 都要遵守。 | `record_lesson(...)` |

### 依任務分類

| 類型 | 工具 | 白話用途 |
|---|---|---|
| 專案探索 | `list_dir(path=".", depth=2)` | 看目錄樹，不要叫模型跑 `ls` |
| 專案探索 | `code_rag_search(query, top_k=5, mode="semantic", hops=1, include_evidence=False, max_chars=12000)` | 依語意定位 symbol、建立 bounded evidence，或查 call/include graph；四種模式與保守解析契約見下節 |
| 專案探索 | `grep_code(pattern, path=".", include=None, context=0)` | 搜錯誤訊息、函式名、設定名；複雜 regex 會退回字面搜尋，並有 30 筆 match、單行 500 字元與整體 200,000 字元的硬上限，截斷會明示標記 |
| 專案探索 | `file_info(path)` | 讀檔前先看大小，避免一次塞爆 context |
| 專案探索 | `read_file(path, start_line=1, end_line=None, max_chars=50000)` | 讀檔案內容，長檔要分段 |
| 文件/外部檔案 | `import_external_file(path, dest_name=None)` | 把允許來源的外部檔案複製進 `.aicode_uploads/` |
| 文件/外部檔案 | `analyze_file(path)` | 用 VL 分析各類圖片、一次性抽 PDF 文字（不入 KB）、分析 ELF 或 firmware blob |
| 文件/外部檔案 | `ingest_document(path, mode="auto", preflight_only=False)` | 把 PDF / MD / TXT / 圖片(png/jpg/...) / binary(bin/elf/...) 匯入 `knowledge.json`；`mode` 預設依副檔名自動選，可顯式 `image` / `chat` / `binary` / `document`。PDF 的圖走兩條 lane（見下節）：**有結構性原生證據**的表格 / 向量文字 log 走結構化抽取並帶驗證狀態；純 raster 截圖、掃描頁、方塊圖仍走既有自由文字 VL（`origin="diagram"` 降權）。任一條失敗都整份不入庫、KB 不變。`preflight_only=True` 只估成本、零寫入（僅 .pdf） |
| 文件/外部檔案 | `review_figures(action="list", document_id="", figure_id="", expected_revision=0, payload_json="", confirm_against_image=False)` | 覆核 PDF 結構化抽取的表格 / 終端機 log：`list` 唯讀列出 figure_id、頁碼、bbox、kind、驗證狀態、原因、原圖路徑與 canonical payload；`fix` 只收該 kind schema 的 structured payload + `expected_revision`，`confirm_against_image=True` 才升 `human_verified`。permission 設 `ask` |
| 文件/外部檔案 | `remove_document(source)` | 從 KB 移除過期文件 |
| 文件/外部檔案 | `reload_knowledge_base()` | 立即載入 KB 並回報 chunk 數（查詢本身會自動偵測變更，這是「馬上確認」用） |
| 文件/外部檔案 | `query_knowledge(question, source=None)` | 查 KB；`source` 可用 basename 限定單一 spec/manual |
| 文件/外部檔案 | `query_knowledge_strict(question, source=None)` | 查高風險規格題，弱證據會拒答；可限定文件 |
| 修改/驗證 | `git_status()` | 看工作樹目前有沒有改動 |
| 修改/驗證 | `git_diff(path=None, staged=False)` | 看修改內容，不需要用 `run_command` 跑 git |
| 修改/驗證 | `apply_patch(diff, dry_run=False)` | 套 unified diff（行號選填，靠 context 定位），會真的寫檔 |
| 修改/驗證 | `run_lint(path, fix=True)` | 對單一檔案跑格式化/lint；`fix=False` 走 check-only(不改檔) |
| 修改/驗證 | `run_command(cmd)` | 跑白名單內的測試 / lint;build 命令(make/cmake/ninja/meson/bazel)需設 `AI_CODE_ENABLE_BUILD_COMMANDS=1` |
| 行為教訓 | `record_lesson(rule, scope="project")` | 你糾正模型行為後,把糾正「提案」成一條行為規則;經你核准(permission ask)寫入 lessons store,之後 session 注入 context([docs/lessons.md](lessons.md)) |

### `code_rag_search` 四種模式

- `mode="semantic"`：用自然語言找 function / class / method / macro / typedef / enum /
  translation-unit / namespace-scope global。`include_evidence=True` 時加上分數組成、
  parser backend、confidence、
  graph status 與最多 5 條一跳關係；預設維持精簡回傳。
- `mode="context"`：先取 semantic seeds，再加入 confirmed 1-hop caller / callee /
  include 與相關 test / header / config / trace lexical evidence，去重後裝進固定字元 budget。
  `max_chars` 合法範圍為 `2000..30000`，預設 `12000`；`used_chars` 只計
  `evidence[].text`，不是 tokenizer token。candidate、graph traversal 與 character budget 的
  截斷會分開回報。歧義、unresolved 與 Python attribute-call heuristic 只進
  `uncertainties`，不算 confirmed；graph 缺席時仍回 semantic-only evidence 並標示
  `graph_status`。
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
<MCP_PYTHON> <CODETRAIL_REPO>/code_graph.py --root <AICODE_ROOT>
```

舊版 DB 可用同一命令 transactionally 升級；DB 損壞則先移到不會 commit 的備份位置再建。
建好後會偵測檔案變更，依 visibility / callable catalog 影響選擇增量或完整重建。

Code-RAG 索引預設排除 ignored、虛擬環境、`third_party` / `vendor` / `external` / `build`
類目錄與自己的 cache；
這不會改變 `grep_code` / `list_dir`。要先看實際索引範圍，可在 CodeTrail checkout 跑
唯讀、離線且預設不印路徑的統計：

```bash
python scripts/index_stats.py --root <AICODE_ROOT>
```

部署層需要額外 include / exclude 時才使用
`~/.config/codetrail/index-scope.json`；schema 與 matcher 細節見
[README_DEV 的索引範圍章節](../README_DEV.md#索引範圍-index-scope)。這份檔不得放進
target repo，必須維持 owner-only 權限（POSIX `chmod 600`）；pattern 本身可能洩漏 NDA
目錄結構。

### PDF 圖片:結構化抽取與人工覆核

北極星是 **verified-or-abstain**:程式能以獨立證據確認,內容才進可信檢索;不能確認就保留
原圖、頁碼、框與格/行位置,正文放 `▯` 並記原因,或該份 PDF 零寫入。raster 上被遮住或低於
解析度的字元沒有任何程式能還原真值,能保證的只有「正確,或誠實拒絕」。

**兩條 lane,範圍不同(重要)**

| lane | 收哪些候選 | 產出 | 有沒有 `▯` / 逐格證據 / strict gate |
|---|---|---|---|
| 結構化 | **有結構性原生證據**者:原生 markdown 表格、`find_tables` 幾何、框線格、對齊的文字帶（無框線 memory map / register map）、**向量文字**的終端機 log | canonical JSON（table / terminal）+ 衍生文字 chunk | 有 |
| 既有自由文字 VL | **純 raster** 的終端機截圖、掃描頁表格、方塊圖 / 流程圖 | VL 的文字描述,`origin="diagram"`,檢索降權 | 沒有 |

換句話說:被拍成圖或掃描進來的表格,本輪**還是**走舊的 VL 描述路徑,不會出現在
`review_figures` 裡,也不受 strict gate 保護(它們一律被當成 `legacy_unverified`,strict 查詢
不會拿它們回答數值)。`diagram` 這個 kind 的 schema 保留給人工修正用,本輪沒有自動生產者。

**六種 `verification_status`**(structured chunk 專屬;兩個正交欄位之一,另一個是
`extraction_status ∈ {complete, failed}`)

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
指出是哪一項(上限是 `config.py` 的 `FIGURE_*`,可用同名 `AICODE_FIGURE_*` env 覆寫)。
MCP 每次工具呼叫有 client timeout,開始之後才超時等於沒有提示 —— 所以先估。

**範圍限制**:preflight 的欄位與「有沒有超過上限」**只涵蓋結構化 lane**。既有自由文字 VL lane(純 raster 內嵌圖、掃描頁)的呼叫**不受這些上限判定**——報告會另外印一個未受閘控的粗估(去重前、且**沒有** image-token 估算),所以「在預算內」不等於整份 PDF 的總成本在預算內。純 raster 圖很多的檔案仍可能發出大量 VL 呼叫。

**零部分成功**:結構化 lane 的 schema / validator / row width / line contract /
`finish_reason` 任一最終不合格 → 整份 PDF 零寫入,舊 KB 與向量保持原狀。需要 VL 的候選會在
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
從 review artifacts 讀)——「零部分成功」代表它們不在知識庫裡,但失敗原因看得到。

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

### 使用原則

- 分析、解釋、推導或找原因時，先用 `code_rag_search(mode="context")` 一次取得 bounded evidence；不足才做精準 `grep_code` / `read_file`，同一 query 不重複。
- 只想定位程式碼時，用工具 `code_rag_search` 或 `grep_code`，再用工具 `read_file`。
- 問「誰呼叫了 X」「X 怎麼一路呼叫到 Y」時,用 `code_rag_search` 的 `mode="neighbors"`(query 放 symbol 名)/ `mode="path"`;問「這個檔直接 include 了誰」時,`mode="neighbors"` 的 query 放 repo 相對檔案路徑。回傳的關係每一步都有 `檔:行` 證據,unresolved(function pointer / macro 間接呼叫)與歧義候選(同名多定義)會明講。graph 首次建置要在終端跑一次建立命令——沒建就查 graph 模式會明確報錯,**錯誤訊息就含完整可執行的那條命令**(實際 interpreter 與絕對路徑,直接複製貼上;semantic 不受影響);建好之後查詢自動偵測檔案變更做增量更新,安裝 tree-sitter grammar 或改 `AICODE_H_LANG` 後會自動整體重建。
- 檔案變更偵測有一個 30 秒的快照窗(`AICODE_CODE_RAG_REFRESH_TTL`,設 0 關閉):透過 CodeTrail 工具(`apply_patch` / `run_command` / `run_lint`)寫檔會立即失效重掃;**在外部編輯器改檔**則最長 30 秒內的查詢可能還看到舊索引,屬既知取捨。
- 長檔先用工具 `file_info` 看大小，再要求工具 `read_file` 分段讀。
- 查 spec 先用工具 `query_knowledge`；數字、限制、預設值這類答錯很糟的題目，用工具 `query_knowledge_strict`。多份相似版本並存時傳 `source="檔名"`，filter 會在 top-k 前套用。
- 外部檔案先用工具 `import_external_file`，再用工具 `analyze_file`、`ingest_document` 或 `read_file` 處理匯入後路徑。
- 新增或刪除文件後查詢會自動載入變更；要立即確認 chunk 數可用工具 `reload_knowledge_base`。
- 改檔前先看工具 `git_status` / `git_diff`；改檔用工具 `apply_patch`。
- 工具 `apply_patch` 和 `run_command` 有副作用；需要改檔或執行專案腳本時才允許。
- 工具 `record_lesson` 只在「你糾正了模型的做事方式」之後用;工具報錯或答案錯誤不是觸發條件。寫入需要你核准,細節與管理指令見 [docs/lessons.md](lessons.md)。
- 圖很多的 PDF 先用 `ingest_document(path, preflight_only=True)` 估成本（零寫入），再決定要不要在 MCP 裡跑或改走 CLI。
- REF 標「待覆核」的圖片內容不得當成規格數值的定論；`query_knowledge_strict` 的 `excluded_figures` 就是被 gate 擋下、但確實存在的圖，照實轉述頁碼與原因。**只有 structured figure（`excluded_figures` 帶 `figure_id` 的那些）能用 `review_figures` 覆核**（`fix` 會改 KB，permission 是 `ask`）；舊 KB / 純 raster 的 VL chunk 不會出現在 `review_figures` 裡，本輪沒有把它們升成 strict-trusted 的路徑，只能回去看原始 PDF 那一頁。

---
