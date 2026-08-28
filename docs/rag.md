# RAG、附件與知識庫操作

這份文件整理附件匯入、知識庫建立與規格查詢方式。Code-RAG 的 semantic /
context / graph 模式改集中在 [MCP 工具清單](mcp-tools.md#code_rag_search-四種模式)。
CodeTrail 啟動聊天 frontend 前會硬性檢查 llama-server `:8081` (embedding)、
`:8082` (reranker) 與 `:8083` (VL) 都 ready。

[回到 README](../README.md)。

---

## 重點教學

啟動 aicode 進到對話之後，最常碰到兩件事：

1. 有一張錯誤截圖／一份韌體 binary／一段 log，想讓對話幫忙看。
2. 有一份產品規格書／datasheet／設計手冊，想讓之後對話遇到相關問題時答得準。

這兩件事分別對應下面的「在對話裡讓模型看到一個檔案」和「把附件做成知識庫讓模型隨時能查」。讀完這兩節就能開始實用。

另外，模型要不要**自發**去查知識庫（不等你在對話裡點名工具），取決於 frontend 與模型的
tool-call 能力，不是 RAG 本身。[OpenCode 全域 AGENTS.md 範本](opencode-agents-template.md)
只保留「內部規格要先用唯讀工具取證」這種短約束；不要再加入整段 RAG 流程，長全域 prompt
實際發生過反而讓模型不呼叫任何工具。重要問題請在當次 prompt 明講「先呼叫
`query_knowledge` 查證再回答」，工具的詳細選項以本輪 schema 與本文件為準。

### 在對話裡讓模型看到一個檔案

#### 場景一：檔案在當前專案目錄裡

最簡單。放到 `cd` 進去那個目錄底下任何位置，然後在對話裡點名工具和檔案路徑：

| 檔案類型 | 對話可以這樣說 |
|---|---|
| 程式碼 / log / 純文字 | 「請用工具 `read_file` 讀 `logs/build_fail.txt`」 |
| 截圖 / 圖片 | 「請用工具 `analyze_file` 分析 `screenshots/error.png`」 |
| PDF（只看一眼、不入庫） | 「請用工具 `analyze_file` 分析 `docs/npu_spec.pdf`」 |
| 韌體 / 執行檔 / 二進位（.bin / .elf / .img） | 「請用工具 `analyze_file` 分析 `firmware/boot.bin`」 |

兩個工具差別：

- `read_file` 直接把純文字內容讀進對話（拿到 PDF／二進位會回導引訊息，不會吐亂碼）。
- `analyze_file` 會先做處理 — 圖片由 VL 做通用視覺分析（文字辨識、UI／終端機、表格、圖表、架構／流程關係與一般照片都包含），PDF 抽各頁文字（內嵌圖會標註頁碼張數但不做 VL），二進位檔則抓出檔頭格式和可讀字串，ELF 給結構化總覽（header / 記憶體配置 / symbols 含 LOCAL / imports / relocation / DWARF / 字串分類），要深入時加 `view` 與 `target`（例如 `view="symbols", target="uart"`、`view="disasm", target="Reset_Handler"`、`view="dwarf", target="0x08001234"`，完整表見 [docs/mcp-tools.md](mcp-tools.md#analyze_file-的-elf-視角)） — 再把整理後的結果丟給模型。
  `analyze_file` 對 `.pdf` 是一次性抽文字，**不做**結構化圖片抽取（表格 / 終端機畫面的 canonical JSON 與驗證狀態只在 `ingest_document` 的入庫路徑產生）。

`analyze_file` 是「這一輪看一次就丟」，看完不會留在 KB 裡，未來其他對話查不到。如果想把這張截圖／這份 firmware 永久保存供之後查詢，改用 `ingest_document`（見「把附件做成知識庫讓模型隨時能查」），它接受相同的圖片／binary／ELF 副檔名，並會切 chunk、算 embedding 寫進 `knowledge.json`。

#### 場景二：檔案在專案目錄外

預設情況下系統不能讀專案目錄以外的東西。這是安全限制：避免分析陌生程式碼時模型意外讀到家目錄裡的 SSH key、密碼、別的專案這類敏感資料。

要讓外部檔案進到對話，要設兩個 env var，這兩個分工不同，**一起設才會生效**：

| Env var | 角色 | 預設 |
|---|---|---|
| `AI_CODE_ALLOW_EXTERNAL_IMPORT=1` | **總開關**。決定外部匯入功能能不能用 | 關閉 |
| `AI_CODE_IMPORT_ROOTS="<目錄1>:<目錄2>:..."` | **白名單**。決定哪些目錄底下的檔案可以匯入 | `~/Downloads:/tmp` |

只打開總開關，預設白名單只有 `~/Downloads` 和 `/tmp`，其他目錄底下的檔案還是拿不到。`AI_CODE_IMPORT_ROOTS` 一旦自己設了就**完全取代**預設清單 — 要保留 Downloads/tmp 記得自己列上。

幾種常見組合：

```bash
# 只用預設來源 (~/Downloads + /tmp)
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 aicode

# 保留預設 + 加一個自己的目錄
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 \
AI_CODE_IMPORT_ROOTS="$HOME/Downloads:/tmp:$HOME/u-boot" \
aicode

# 只開一個專用交換目錄（比放寬整個 home 安全）
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 \
AI_CODE_IMPORT_ROOTS="$HOME/codetrail-import" \
aicode
```

多個目錄用冒號分隔（跟 `$PATH` 一樣）。如果每次都用同一組設定，加進 `~/.bashrc` 就不用每次帶：

```bash
export AI_CODE_ALLOW_EXTERNAL_IMPORT=1
export AI_CODE_IMPORT_ROOTS="$HOME/Downloads:/tmp:$HOME/u-boot"
```

開啟後，對話裡先請模型把檔案複製進專案再分析：

```text
請用工具 import_external_file 匯入 ~/Downloads/error.png，
然後用回傳的新路徑做 analyze_file。
```

複製進來的檔案會放在專案根目錄底下的 `.aicode_uploads/` 資料夾，原始檔案不會被搬走或修改。後續對話就把它當成專案內檔案處理。

匯入被拒絕時錯誤訊息會印出目前生效的白名單，如果看到拒絕但不確定原因，第一件事先確認檔案路徑有沒有真的在白名單裡的某個目錄底下。

#### 完整範例

```text
我在 ~/Downloads/oops.png 拍到一個錯誤訊息畫面，
請先用 import_external_file 匯入，
再用 analyze_file 認出畫面上的錯誤文字，
最後用 grep_code 在當前專案找這串錯誤可能來自哪個 .c 檔。
```

#### 同時處理外部附件並注入 RAG

如果檔案在專案外，又想讓它同時進入目前對話和長期知識庫，流程是：

1. 先用 `import_external_file` 把外部檔案複製進專案，拿到 `.aicode_uploads/...` 新路徑。
2. （選用）對新路徑做 `read_file` 或 `analyze_file`，**只是讓模型這一輪先看一次**，不會寫入 `knowledge.json`。
3. 對**同一個路徑**做 `ingest_document`（它會重新讀那個原始檔案、自己切 chunk 算 embedding）。之後的查詢會**自動偵測** `knowledge.json` 變更並載入；想立即載入並確認 chunk 數，可補一句 `reload_knowledge_base`。

範例：

```text
請用工具 import_external_file 匯入 ~/Downloads/npu_spec.pdf，
用回傳的新路徑先做 file_info 確認檔名與大小，
再用同一個新路徑執行 ingest_document，
完成後 reload_knowledge_base，
最後用 query_knowledge 查這份 spec 的版本號並附 REF。
```

這個「先看一次、再把同一路徑入庫」的流程對**圖片 / ELF / firmware binary 都適用**（純文字 / log 第 2 步改用 `read_file`）。但三件事一定要分清楚，否則容易誤會：

- `analyze_file(path)` / `read_file(path)` **只讓目前這一輪對話先看附件**，不會寫入 `knowledge.json`。
- `ingest_document(path)` 會**重新讀同一個原始檔案**、切 chunk、算 embedding、寫入 `knowledge.json` —— 它**不會接收 `analyze_file` 的文字輸出**，所以第 2 步看一次只是給你參考，省略也不影響入庫結果。
  PDF 的結構化圖片抽取（表格 / 向量文字 log → canonical JSON + 驗證狀態）也是在這一步才發生，`analyze_file` 那一輪不會產生它。
- ingest／remove 之後**查詢會自動偵測檔案變更並重載**（code 層保證，不再依賴人工記得）。`reload_knowledge_base()` 仍可用來「立即」載入並回報 chunk 數。

（另外 `import_external_file` 只負責把外部檔案帶進沙箱，本身不寫 KB。）

### 把附件做成知識庫讓模型隨時能查

「知識庫」是這個專案放規格書、手冊、設計文件的地方。呼叫
`query_knowledge(...)` / `query_knowledge_strict(...)` 時，系統會找出最相關的幾段內容
作為回答依據，並用 `REF1`、`REF2` 標出來源位置。模型會不會在沒被點名時自行呼叫查詢，
仍取決於本輪行為規則；不是文件入庫後每一輪都會自動注入。

比起每次都重新貼一份 PDF 給對話，這樣比較不會超出上下文長度限制，也比較不會記錯。

#### 支援格式

- **文字**：`.pdf` / `.md` / `.txt`（抽文字。PDF 裡的圖走**兩條 lane**，範圍不一樣，詳見下面「[PDF 內的表格與終端機畫面](#pdf-內的表格與終端機畫面結構化抽取--人工覆核)」：
  1. **結構化 lane** —— 收原生 markdown 表格、`find_tables` 幾何、框線格、對齊文字帶、向量文字 log，也收夠大的純 raster / picture。raster 先由 VL 分類成 table / terminal / diagram，再產出 canonical JSON、逐格/逐行證據與驗證狀態；看不清的字元放 `▯` 而不是猜。
  2. **舊自由文字 VL 相容 lane** —— 只處理未被結構化候選覆蓋的舊 picture job，並維持既有 `origin="diagram"` KB chunk 的相容性；它沒有 canonical payload 或 strict gate。
  兩條 lane 任一失敗都是**整份文件不入庫**（零寫入）。走到 VL 的圖需要 VL server（:8083）在線；純文字＋原生表格的 PDF 則可能一次 VL 都不用呼叫）
- **圖片**：`.png` / `.jpg` / `.jpeg` / `.gif` / `.webp`（用 VL 模型看圖、抽出文字描述後切 chunk，需要先把 VL GGUF 掛在 llama-server :8083,設定見 [README §2.4](../README.md#24-vl-模型) 與 §3.2）
- **binary**：`.bin` / `.dat` / `.raw` / `.fw` / `.img` / `.rom` / `.hex`（抽 hex dump、可讀字串、magic 偵測；遇到 ELF magic 自動切到 ELF 解析）
- **ELF**：`.elf` / `.so` / `.o` / `.axf` / `.out` / `.ko`（走長版多視角報告：summary、完整 symbols、memmap、relocation caller、DWARF 函式與型別、分類 strings、sections / imports / dynamic）

純圖片掃描的 PDF（沒有可選文字）不再切不出內容：每頁會 render 後進入 raster 分類與結構化抽取。文字＋圖混合的 PDF（datasheet 類）文字照舊切 chunk，圖另外產生 table / terminal / diagram structured chunk。圖很多的 PDF 建議先跑 `ingest_document(path, preflight_only=True)` 估成本（零寫入，見下節）。VL server 是啟動必要條件，若圖片分析失敗（ingest 會整份中止、知識庫不變），先跑 `python3 scripts/required_model_servers_check.py` 看 `image_url` 多模態 probe。

#### 三個步驟

**步驟 1：檔名取對**

檔名會直接影響搜尋排序。同樣內容檔名清楚會排得比較前面：

| 檔名裡有這些字 | 系統當成 | 適合裝的內容 |
|---|---|---|
| `spec` / `datasheet` | 規格書（最權威） | 規格、限制、硬體行為 |
| `api` / `reference` | API 文件 | 函式定義、參數、回傳值 |
| `manual` / `handbook` | 手冊 | 操作流程 |
| `guide` / `tutorial` | 教學 | 上手指南 |
| `faq` | 常見問題 | 問答對 |

例如把 NPU 規格書命名成 `npu_spec.pdf`，會比叫 `doc.pdf` 在「最大張量大小是多少」這類規格問題裡更容易被優先找到。

**步驟 2：匯入並重新載入**

把檔案放進專案目錄（建議統一放在 `docs/`），對話裡：

```text
請用工具 ingest_document 匯入 docs/npu_spec.pdf，
完成後用工具 reload_knowledge_base 重新載入。
```

`ingest_document` 會把整份文件切成多段、算出每段的向量、存進專案根目錄的 `knowledge.json`。之後的 `query_knowledge` 會**自動偵測檔案變更並重載**，忘了 reload 也查得到；`reload_knowledge_base` 的用途是「立即」載入並回報 chunk 數（像上面範例那樣馬上確認匯入結果），或在自動偵測疑似失效時強制重載。

這裡的「吃進記憶體」是 **MCP server 的 KB singleton / 向量索引**,不是把整份文件塞進 OpenCode 聊天 context。`ingest_document` 回到當前對話的只有執行摘要,`reload_knowledge_base` 只有狀態;等你呼叫 `query_knowledge` 時,才會把命中的少量 chunks 當 tool result 帶進那個 session。因此 KB 文件數變多會增加索引與 retrieval 工作,但不會讓每個新 session 自動帶著全文。若模型在 ingest 後看似「失憶」,先依 [troubleshooting](troubleshooting.md#mcp-connected-but-no-tool-call)檢查實際 token、compaction 與真 / 假 tool call,不要直接歸因於 RAG context overflow。

預設依副檔名自動分派到對應的處理路徑（見上方「支援格式」清單）。圖片預設走「技術圖片」路徑（架構圖／流程圖／記憶體圖），抽出的是畫面說明；若這張是聊天截圖、想抽出對話內容，要顯式傳 `mode="chat"`：`ingest_document("teams.png", mode="chat")`。

一次匯入多份：

```text
請依序執行：
1. ingest_document docs/npu_spec.pdf
2. ingest_document docs/api_reference.md
3. ingest_document docs/faq.txt
4. reload_knowledge_base
最後回報目前載入幾個 chunks。
```

`chunks` 是「切好的文件段落」。回報 0 代表沒匯入到任何內容 — 常見原因：binary 太小或全是 0xff、圖片走 VL 卻抽不出可用描述。（純圖片掃描的 PDF 不再是原因：每頁會整頁 render 交給 VL；但 VL llama-server (:8083) 沒啟動時，PDF 會**整份 ingest 失敗**而不是回報 0。preflight 超過上限也是直接結束並報告超出的項目，同樣不是回報 0。）

**步驟 3：查**

匯進去之後用 `query_knowledge`：

```text
請用工具 query_knowledge 查 conv2d 的輸入大小限制，
回答時每個數字都要附 REF 標記。
```

回答長這樣：

```text
根據 REF1，conv2d 輸入張量的高/寬上限是 4096 (REF1: npu_spec.pdf §3.2.1)。
batch size 上限是 32 (REF1)。
```

#### 圖片附件：讓 VL 看圖，再進 RAG

前面三步示範的是 PDF／文字。**圖片附件（截圖、架構圖、被拍成圖的 datasheet 頁）走的是同一個 `ingest_document`，只是中間多一段 VL**：`auto` 模式看到 `.png` / `.jpg` 這類副檔名，會自動呼叫 VL server（:8083）把圖看成文字說明，再切 chunk、算 embedding 寫進 `knowledge.json`。所以「VL 看圖」和「RAG 查得到」不是兩個要分開操作的功能，而是同一條管線的前後段。

兩個工具都會用到 VL，差別只在會不會進知識庫：

| 你要的 | 用哪個 | 進 RAG？ |
|---|---|---|
| 只看這張圖一次，看完就丟 | `analyze_file('diagram.png')` | ✗ 只在這一輪對話 |
| 看完還要之後反覆查 | `ingest_document('diagram.png')`（之後查詢自動載入） | ✓ VL 抽完寫進 knowledge.json |

> **不用先 `analyze_file`。** `ingest_document` 餵圖片時會**重新讀那個原始圖檔**、內部自己呼叫 VL 看圖（`RAG.py --image` → `process_technical_image` → VL server :8083），抽出文字後才切 chunk、算 embedding 寫進 `knowledge.json`。`analyze_file` 是另一條獨立的入口（走 `media.py`），只在你想「這一輪先看一眼畫面」時用，它的輸出不會被 ingest 吃進去，**不是 ingest 的前置步驟**。

入庫後查詢時，圖片／截圖來源的 REF 會標 `origin: VL`（給人看的摘要則標 `·VL`），提醒模型這是視覺辨識的**機率性描述**、與原文抽取不同級。和文字抽取的 REF 衝突時**不會宣稱哪一邊必勝**——兩邊的數值與出處都會列出，並標明衝突未解，由你判斷（自動挑一邊等於把一個未解的矛盾包裝成結論）。

規格數字題的證據閘現在看的是**驗證狀態**，不只是文件類型：所有既有的 VL 圖片／截圖 chunk 都算 `legacy_unverified`，因此 `query_knowledge_strict` **一律不用它們回答數值**（會出現在回傳的 `excluded_figures` 裡，帶頁碼與原因）。要讓一份 PDF 裡的表格恢復可信度，必須重新 ingest 走結構化 lane，或用 `review_figures` 人工覆核。

圖片在專案目錄內（建議放 `docs/`）直接 ingest，之後就查得到：

```text
請用工具 ingest_document 匯入 docs/npu_block_diagram.png，
完成後 reload_knowledge_base，回報載入幾個 chunks。
```

```text
請用 query_knowledge 查這張方塊圖裡 DMA 跟 SRAM 怎麼連，結論附 REF。
```

圖片在專案外（例如 `~/Downloads` 的截圖），跟上面「同時處理外部附件並注入 RAG」一樣，只是把 PDF 換成圖片 —— 先 `import_external_file` 帶進沙箱再 ingest：

```text
請用工具 import_external_file 匯入 ~/Downloads/error_screen.png，
對回傳的新路徑做 ingest_document，最後 reload_knowledge_base。
（想在這一輪先看一眼畫面，可以在 ingest 前選用 analyze_file，但它不是 ingest 的前置步驟。）
```

兩個常踩的點：

- **預設走「技術圖片」路徑**（架構圖／流程圖／記憶體圖），抽的是畫面說明。若這張是**聊天截圖**、想抽的是對話內容，要顯式 `ingest_document('teams.png', mode='chat')`。
- chunks 回報 0，圖片來源最常見的原因是 **VL server（:8083）沒起來** —— 圖片分析失敗就切不出內容。先跑 `python3 scripts/required_model_servers_check.py` 看 `image_url` 多模態 probe。

#### PDF 內的表格與終端機畫面:結構化抽取 + 人工覆核

北極星是 **verified-or-abstain**:程式能以獨立證據確認,內容才進可信檢索;不能確認就保留
原圖、頁碼、框與格/行位置,正文放 `▯` 並記原因,或該份 PDF 零寫入。raster 上被遮住或低於
解析度的字元,沒有任何程式能還原真值 —— 能保證的只有「正確,或誠實拒絕」。所以
**「查得到但標了待覆核」是正常狀態,不是 bug**。

##### 涵蓋範圍

| lane | 收哪些 | 拿得到什麼 |
|---|---|---|
| **結構化** | 原生 markdown 表格、`find_tables` 幾何、框線格、對齊文字帶、向量文字 log，以及夠大的純 raster / picture | raster 先分類成 table / terminal / diagram；再產生 canonical JSON、逐格/逐行證據、`▯`、驗證狀態、strict gate，且可用 `review_figures` 覆核 |
| **舊自由文字 VL 相容路徑** | 未被結構化候選覆蓋的舊 picture job，以及既有 KB chunk | 只有 VL 文字描述（`origin="diagram"`，檢索降權），沒有 canonical payload |

掃描版 datasheet 的表格與手機拍的終端機畫面現在會成為 structured figure；因為通常沒有
獨立原生證據，狀態仍多半是 `unverified` / `needs_review`，`query_knowledge_strict` 會擋下，
直到人工對原圖確認。`diagram` 也有自動分類與 structured producer。

##### 六種驗證狀態

| 狀態 | 意思 | strict 查詢用不用 |
|---|---|---|
| `native_verified` | 原生表格 geometry 與**至少另一個原生** evidence channel 在 row/cell 結構與 critical token 上一致（單次 `find_tables().extract()` 不算） | ✔ |
| `corroborated` | 視覺抽取與獨立 PDF 文字/幾何證據**逐格或逐行**一致。terminal 的比對走空白正規化,所以**不等於**逐位元組一致（PDF 文字層證明不了 tab 還是多個 space） | ✔ |
| `human_verified` | 你對**指定 revision** 的原圖明確確認/修正,且修正後的 payload 通過 validator | ✔ |
| `needs_review` | 有 `▯`、衝突、漏 row/line、tile 縫合不確定、kind 歧義或截斷 | ✘ |
| `unverified` | 結構合法、未發現衝突,但沒有獨立證據（無 anchor 的同模型多次取樣即使全等也只到這級） | ✘ |
| `legacy_unverified` | 舊 KB 缺欄位的 figure chunk,含所有既有 VL 圖片 / 截圖 / diagram chunk | ✘ |

後三種合稱 **flagged**,那是查詢時的 filter,不是第七種狀態。一張圖切成多個 chunk 時,聚合
一律取**最差**的成員狀態(不會被第一個成員蓋掉)。

##### 查詢端會怎麼表現

- `query_knowledge_strict`:flagged 的圖片內容在 **code 層**就被擋掉,不進 REF、也不參與門檻
  計算。被擋下的會出現在回傳的 `excluded_figures`(source / page / figure_id / figure_index /
  kind / 狀態 / 原因)與 `review_hint`,而且**四條回傳路徑都有**(KB 未載入、證據太弱拒答、
  不走嚴格模式、正常回答)。所以就算全部候選都被擋,你仍看得到「哪一頁、哪一張圖可用但待覆核」,
  不會變成「查不到」的假象。
- `query_knowledge`:會回未驗證內容,但 REF 與 metadata 都帶狀態、原因與實際的 row/line 範圍;
  因預算截斷時會明說「未完整顯示」,不讓你以為整張 log 都在。

##### 圖多的 PDF:先跑 preflight(零寫入)

```text
請用工具 ingest_document 匯入 docs/datasheet.pdf,preflight_only 設 True,
回報候選數、tile 數、VL 呼叫次數、image token 估計,以及有沒有超過上限。
```

它在**任何 VL 呼叫、embedding 與 knowledge.json 寫入之前**算完就結束。超過上限會直接停下並
指出是哪一項;上限在 `config.py` 的 `FIGURE_*`,可用同名 `AICODE_FIGURE_*` 環境變數覆寫。

> preflight 涵蓋所有結構化候選，包含純 raster 的分類、雙樣本抽取與 image-token 估算。
> 只有未被結構化候選覆蓋的舊 picture 相容 job 不受這些上限判定；若存在，報告會另外
> 列出未受閘控的粗估。
>
> `VL 呼叫 最少 / 最多` 是真正的區間：`最少`＝每張 raster 都被分類成 diagram（單樣本）
> 的情形，`最多`＝全部走雙樣本再加重試。實際次數一定落在其中。
>
> 報告若出現「沒有任何 consumer 的 raster 區域」，代表那幾塊圖既沒有結構化候選、
> 舊相容 lane 也讀不到（它只讀 `page_boxes`），會逐筆列出頁碼與 bbox。正常情況是 0；
> 不是 0 就表示那幾張圖不會進知識庫，要當成待處理項目看。
在終端機的等價寫法(同樣零寫入):

```bash
python3 RAG.py docs/datasheet.pdf knowledge.json --preflight
```

##### 抽壞的那一張缺席,不是整份零寫入

結構化 lane 的 schema / validator / row width / line contract / `finish_reason` 任一最終不合格
→ **那一張圖不進 KB**(不冒充成功入庫,也不退回自由文字描述),其餘 figure 與全部文字 chunk
照常入庫;stdout 會印一行 `[figure] 失敗 N 張(不進 KB):p31 diagram truncated;…`,失敗的那幾張
仍留在同一份 review artifact 裡,用 `review_figures(action="list")` 看得到(`in_kb: False`)。
VL 的輸出本來就有隨機性,把它綁成文件級的全有全無,等於讓「整份文件進不進得去」看運氣。

**仍然整份 PDF 零寫入**的是「剩下的圖也不能信」那幾種:VL 連不上 / 逾時、預算超限、
capability probe 未過、來源檔中途被換掉,以及候選與結果對不上這類契約破裂——舊 KB 與向量
保持原狀(可能留下 `failed:true` 的 review artifact)。需要 VL 的候選會在動 KB 之前先做
capability probe:端點真的吃 image content part、
接受本專案的 nested `json_schema`、能完成一張極小且不含機敏內容的 canary 並通過外部 validator。
不通過就 fail-loud 指出缺哪一項,不以「OpenAI-compatible」推定品質。

##### 人工覆核:list → 改 → fix

```text
請用工具 review_figures,action 設 "list",列出目前待覆核的圖,說明每一張的原因。
```

回傳每一張的 `document_id`、`figure_id`、`revision`、頁碼與 bbox、kind、
`extraction_status` / `verification_status`、`reasons` / `reason_details`、原圖(crop)路徑與
`evidence_ref`。挑定一張後帶 `figure_id` 再 list 一次,就會附上完整的 canonical payload
(多筆列出時不附 payload,整份表格 / log 會塞爆對話;輸出的表頭每次都會講這件事)。

crop 那一行會標**模型到底有沒有看過這張圖**:`variants/` 裡的才是實際送模的,
`review_assets/` 是只為覆核 render、從未送模的。拿一張模型沒看過的圖去「確認」模型的
抽取結果,等於在確認一件沒發生過的事,所以工具會明講是哪一種。native lane(原生表格)
本來就零 VL 呼叫,它的 crop 一律只供覆核。

抽取失敗、因此沒有進知識庫的圖也會列出來(標 `in_kb: False` / `fixable: False`,從 review
artifacts 讀),失敗原因看得到,只是不能直接 `fix`——要救那一張就重新 ingest 那份 PDF。

改好之後送回:

```text
請用工具 review_figures,action 設 "fix",figure_id 設 <剛才那個>,
expected_revision 設 <list 顯示的 revision>,payload_json 貼改好的 JSON,
confirm_against_image 設 True。
```

要點:

- **只收該 kind 的 structured payload**,拒絕自由文字全段替換。JSON 物件不得有重複 key
  (Python 只會留最後一個 = 在 validator 之前無聲改寫你的值)。
- `kind` 以 **KB 記錄的為準**;payload 自報的 kind 不符會被拒絕(不允許用 fix 改變類別)。
- `expected_revision` 必填。revision 已被別人改過 → 回 **conflict、零寫入**,不做
  last-write-wins。重新 list 看現況、確認你的修改仍正確,再送一次。
- `confirm_against_image=True` 的意思是**你看著原圖確認過**。只把機器轉寫貼回來不算 ——
  `human_verified` 是使用者的確認,不是模型的自證。所以這個工具的 permission 是 `ask`,
  你會在核准框看到完整參數。
- 流程:validate → render → kind-aware 重切 chunk → 重算受影響的 embedding / id / hash →
  exclusive lock 內確認 revision 未變 → 原子替換。任一步失敗,舊 chunks / 向量 / manifest
  全部保持可用。

##### review artifacts:位置、NDA 與清除

```
<專案>/.codetrail/figures/<document_slug>/<run_id>/
├── manifest.json          canonical manifest(figures / preflight / stats)
├── assets/                原始 asset(從 PDF 抽出來的原圖)
├── variants/              **實際送給模型的每一張圖**(crop / tile)
├── review_assets/         **只為覆核 render、從未送給模型**的圖
├── review.md              給人看的摘要
└── revisions/<n>/         人工 fix 後的 canonical payload
```

- **可能含 NDA 內容**(原圖就是規格書的一塊)。`.gitignore` 已含 `.codetrail/`,不要 commit;
  在別的 target repo 用 CodeTrail 時,也請在那個 repo 的 `.gitignore` 補同一行。
- `FIGURE_REVIEW_MAX_RUNS_PER_DOC`(預設 5)是 **soft retention target,不是硬上限**:
  被 KB `evidence_ref` 引用、`created_at` 判讀不出來、或清理失敗的 run 一律 fail-closed
  保留下來,實際份數可能超過 5。**不要拿它當「機敏影像最多留幾份」的保證**;要確定清掉
  就顯式刪除對應目錄並自己確認結果(見下一點)。
- 要手動清:直接刪掉整個 `<document_slug>` 目錄。**清掉之後會發生什麼**(兩件事要分清楚):
  - **查詢完全不受影響** —— KB 是 revision 的唯一真相,已入庫的 chunk 與向量都還在,
    `query_knowledge` / strict 照常。
  - 但 **`review_figures` 會壞掉一半** —— `list` 那幾張會降級成 `payload: (讀不到)`,
    因為 canonical payload 與原圖都在 manifest 裡;**沒有 payload 就無法做 `fix`**。
    要恢復覆核能力,只能 `remove_document` 之後重新 `ingest_document` 那份 PDF。
  - `remove_document`(從 KB 移除文件)與清除 artifacts 是**兩件獨立的事**:前者讓查詢查不到,
    後者讓覆核做不了。要徹底清掉一份 NDA 文件的痕跡,兩邊都要做。

##### 舊 KB 怎麼辦

先前入庫的圖片 chunk 缺這些欄位,載入時會在**記憶體內**補成 `legacy_unverified`
(不回寫檔案,所以不會動到你的 `knowledge.json`)。影響:

- `query_knowledge` 照常查得到,只是 REF 會標待覆核。
- `query_knowledge_strict` **不再用它們回答數值**,改成在 `excluded_figures` 指出可用但待覆核。

要不要重 ingest?新版會同時處理原生與 raster 圖:

- 是 → **可能**拿得到可信狀態,值得試。那些表會走結構化 lane;但「有原生文字」不等於
  「一定 `native_verified`」—— 那需要**兩個一致的原生 evidence channel**,只有一個通道時
  結果是 `unverified`,通道互相矛盾時是 `needs_review`。而且 native lane 不呼叫 VL,
  所以**不會**產生 `corroborated`。實際拿到什麼狀態以 `review_figures(action="list")`
  的結果為準,不要預先假設。
- 不是(掃描版 / 拍照版 / 純 raster)→ 重 ingest 會先分類並產生 structured figure，可在
  `review_figures` 看 canonical payload 與原圖。因為沒有獨立原生證據，預設仍會是
  `unverified` / `needs_review`；人工逐圖確認後才能升成 `human_verified` 供 strict 查詢使用。

重 ingest 的做法就是既有的維護流程:`remove_document` 舊的,再 `ingest_document` 一次。

#### 規格題、數字題用嚴格模式

「最大值是多少」「預設值是什麼」「reset 訊號最少要拉幾毫秒」這種**答錯比不答更糟**的題目，改用 `query_knowledge_strict`：

```text
請用工具 query_knowledge_strict 查 reset assert 最小持續時間，
證據不夠就直接拒答，不要用常識補。
```

兩者差別：

- `query_knowledge`：把找到的文件段落丟給對話模型，模型自己組答案。
- `query_knowledge_strict`：在背後跑兩階段檢查 — 先看找到的內容是不是真的足以回答；確認後再驗證最終答案每一句話都有對應的 `REF` 出處；任何一句沒對到的會被刪掉，證據真的太弱就直接回「拒答」而不是亂編。

多份相似 spec 同時存在時可限定文件，例如
`query_knowledge("reset value", source="npu_core_rev_b.md")`；`source` 用 basename 精確比對，
而且在 dense/BM25 各自取 top-k 前就套用，不是事後把別份文件濾掉。嚴格版也接受同一參數。

代價是後者比較慢，而且因為是後台跑，TUI 不會顯示中間過程，只看得到定稿後的答案。

#### 維護

文件改版時把舊版刪掉再加新的：

```text
請用工具 remove_document 移除 old_spec.pdf，
完成後 ingest_document docs/new_spec.pdf，
最後 reload_knowledge_base。
```

想看目前知識庫有多少內容：

```text
請用工具 reload_knowledge_base，回報目前載入幾個 chunks。
```

想「整個重來，只留這一份」不用先 remove 再 ingest，一步就好：

```text
請用工具 ingest_document 匯入 docs/new_spec.pdf，fresh 設 True，
回報清掉幾個 chunk、保留幾筆 human_verified。
```

`fresh=True` 會在**同一次原子提交**裡清空既有 chunks、讓舊向量失效、只留這一份文件；
中途失敗整批回滾，不會出現「新 JSON 配舊向量」這種半套狀態。CLI 對應
`python3 RAG.py <file> knowledge.json --fresh`（`rebuild` 子命令也吃 `--fresh`，
只對第一份文件生效，之後照常合併；同一來源的 basename 會更新原文件）。

**fresh 不會額外整批清除 `.codetrail/figures/`**——那是花時間換來的人工資料，不是 cache。
（ingest PDF 本來就會在那裡寫入這一次的 run，提交成功後也可能依 retention 回收該文件
**沒有被 KB 引用**的舊 run；那是 ingest 一直以來的行為，與 fresh 無關。fresh 不會因為
「這份文件被移出 KB」就去刪它的 artifacts。）

但「檔案留著」和「還能用」是兩件事，這裡要講清楚：

| | 之後重新 ingest 時 |
| --- | --- |
| **同一份**文件 | 人工修正會沿用回來（來源像素 / 頁碼 / 正規化 bbox 全等時），revision 不倒退 |
| 被 fresh **移出 KB 的其他文件** | artifact 檔案都在，但人工確認**不會**自動恢復，revision 退回 1 |

原因是沿用的前提為「該 figure 仍在 KB 內」——KB 是 revision 的唯一真相，光有 artifact
證明不了使用者當初確認的是哪一版。要恢復只能重新覆核。刪掉 `knowledge.json` 也是同一
個情況。

#### 只有 `knowledge.json` 要管

向量不是使用者要維護的檔案：它是 `knowledge.json` 衍生出來的 cache，住在
`.codetrail/cache/embeddings/<kb-id>/`，由程式自己管。

- **備份 / 複製 / 刪除知識庫只要動 `knowledge.json`。** 複製到別的目錄照樣查得到
  （向量會自動重建）；刪掉它就是空知識庫，旁邊不會留下一份舊向量讓人以為還在。
- **cache 隨時可刪。** 下一次載入會印 `[INFO] embeddings cache 不存在或已過期，正在依
  knowledge.json 重建。` 然後自己長回來。文字→向量的增量快取讓重建通常是全部命中。
- **對不上就丟掉重算，不會硬配。** cache 帶著 embedding model、generation、內容雜湊、
  chunk 數與**逐列的 chunk id**；任何一項對不上（例如你用另一份 chunk 數剛好相同的
  `knowledge.json` 覆蓋原檔）都會丟棄重建，不存在「錯位一列、照樣回答」的情況。
- **重建不了就中止查詢。** embedding server 連不上時會看到
  `[FATAL] embeddings cache 無法重建，未使用舊向量；查詢已中止。`——寧可不回答，
  也不用來路不明的向量回答。
- **舊版本的 `knowledge_emb.npz`** 會在下一次載入時處理掉：完整驗證通過就遷移進
  cache 再收掉，驗不過就直接淘汰並重建。不需要手動處理。

#### 三件容易踩的事

1. **知識庫綁專案目錄**：`knowledge.json` 存在當前專案根目錄裡，換到另一個專案就要重新匯入。同一份規格書在多個專案要用就匯入多次。
1a. **同檔名的兩份文件會被擋下**：KB 裡的文件身分是 **basename**，所以 `a/spec.pdf` 和 `b/spec.pdf` 在裡面是同一份。灌第二份時會**直接失敗並列出兩邊的完整路徑，零寫入**，不會把前一份靜默換掉。三種處理方式：確定是同一份文件搬過位置 → 先 `remove_document("spec.pdf")` 再灌；兩份都要留 → 先改成唯一檔名（例如 `npu_a_spec.pdf` / `npu_b_spec.pdf`）；要用這一份重建整個 KB → `ingest_document(..., fresh=True)`。同一個檔改過內容再灌一次是正常的更新，不受影響。
    比對用的是解析過 symlink 的絕對路徑，記在 `knowledge.json` 的 `metadata.document_sources`。**舊 KB 沒有這份紀錄**，所以升級後第一次撞名只會警告並採用新的那份，第二次起才擋得住。PDF review artifacts 用的是含路徑與 hash 的 `document_id`，本來就不會互相覆蓋。
2. **不要 commit**：`knowledge.json` 切碎了原始文件內容，NDA 場景幾乎一定包含敏感片段。已經在 [安全邊界與工作節奏](security.md) 的「不要 commit 的資料」列入不該 commit 的清單，建議在專案的 `.gitignore` 也加一行。
3. **越具體越好**：把一整份 500 頁的手冊原封不動塞進去，不如先抽出實際會問到的章節整理成 markdown 再匯入。雜訊少，答案準。

一般 repo 對話、查 bug、改檔前的工作節奏放在 [基本操作](basic-usage.md)，這份文件只保留附件與知識庫細節。

---


## 文件與知識庫補充

操作流程的主體寫在上面的「把附件做成知識庫讓模型隨時能查」，這節只列幾個補充細節：

- `knowledge.json` 存在當前專案根目錄下，預設會被 `.gitignore` 忽略。它保存切碎後的文件內容，NDA 場景下幾乎一定有敏感片段，**不要 commit**。
- `.codetrail/figures/` 存 PDF 結構化圖片的 review artifacts（原圖、實際送模型的每個 variant、canonical manifest）。**同樣可能含 NDA 內容**，`.gitignore` 已含 `.codetrail/`，一樣不要 commit；清除方式與後果見下面的覆核章節。
- **文件身分是 basename**：`ingest_document` 與 `remove_document` 都以 basename 認人。不同目錄下的同名文件會在入庫時 fail-loud（列出兩邊完整路徑、零寫入），不再靜默覆蓋；`remove_document` 會一併清掉來源紀錄，所以刪完可以灌另一份同名的。review artifacts 用的是含路徑 hash 的 `document_id`、本來就不會覆蓋。
- `remove_document(...)` 用檔名 basename 比對，所以傳完整路徑（`docs/old_spec.pdf`）或單純檔名（`old_spec.pdf`）都可以。刪除會在同一把 store lock 內同步重寫 JSON 與剩餘的向量列；不會刪掉整份向量檔再期待 reload 偷偷重算。
- **embeddings 是程式自管的 cache**（`.codetrail/cache/embeddings/<kb-id>/`），不是要跟 `knowledge.json` 配對的檔案。缺了自動重建、身分對不上一律丟棄重建、重建不了就 fail-loud 中止查詢。詳見上面「只有 `knowledge.json` 要管」。
- 文件切段的大小、不同來源類型的搜尋權重，這些可調參數放在 `config.py` 的 `CHUNK_SETTINGS` 和 `SOURCE_TYPE_WEIGHTS`，預設值在大多數情境下已經夠用，要微調再去動。

---

## Chunk 脈絡（contextual retrieval）— 預設關閉

切碎之後每個 chunk 會失去父文件的脈絡：同一份規格書有十個「測試結果」節，任何一段脫離父章節後幾乎沒有鑑別度。這個功能會在入庫時替每個 chunk 生成一段 50–100 token 的定位文字（「本節出自 <文件> 的 <章節路徑>，說明 <主題>」），只拿去餵檢索訊號。

**兩個旗標都預設關閉**，因為開啟會改變兩件事：入庫從此需要主模型（不只 embedding server），而且如果你的主模型 URL 指到別台機器，整份文件的內容就會離開這台電腦（非 loopback 需要額外設 `AICODE_KB_CONTEXT_REMOTE_OK=1` 才放行）。

```bash
# 生成：只有這條路徑會生成，MCP 的 ingest_document 永遠不會
python3 RAG.py rebuild --kb knowledge.json spec_a.pdf --context

# 查詢時使用（也是緊急關閉開關，關掉不需要重建知識庫）
AICODE_KB_CONTEXT_USE=1 aicode
```

要知道的三件事：

1. **生成的文字不是證據。** 它只會影響「哪些 chunk 被撈上來、排第幾」，不會出現在 `[REF]` 的內容裡，也不會影響拒答判斷、信心度或數值證據判定——那些一律看原文算出來的分數。所以就算脈絡寫錯了，也不會讓一段不相關的原文被當成答案。
2. **成本是每個 chunk 一次主模型呼叫。** 實測約 10–20 秒一個 chunk，幾百個 chunk 的規格書要跑十幾分鐘到一小時。文件沒改的話重跑會全部命中快取、零呼叫。
3. **知識庫格式會變。** 開了之後 embeddings cache 會存兩組向量（retrieval 一組、content-only 的 gate 一組），舊版程式讀不了；要換回去就重新入庫一次（`--no-context`）。向量本身不用管——它是程式自管的 cache，`knowledge.json` 才是要備份的那個檔。

---
