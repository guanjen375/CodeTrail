# RAG、附件與知識庫操作

這份文件整理附件匯入、知識庫建立、Code-RAG 搜尋與規格查詢方式。CodeTrail 啟動聊天 frontend 前會硬性檢查 llama-server `:8081` (embedding)、`:8082` (reranker) 與 `:8083` (VL) 都 ready。

[回到 README](../README.md)。

---

## 重點教學

啟動 aicode 進到對話之後，最常碰到兩件事：

1. 有一張錯誤截圖／一份韌體 binary／一段 log，想讓對話幫忙看。
2. 有一份產品規格書／datasheet／設計手冊，想讓之後對話遇到相關問題時答得準。

這兩件事分別對應下面的「在對話裡讓模型看到一個檔案」和「把附件做成知識庫讓模型隨時能查」。讀完這兩節就能開始實用。

另外,模型要不要**自發**去查知識庫(不等你在對話裡點名工具),取決於模型行為規則,不是 RAG 本身。想提高自發查詢的機率,安裝 [OpenCode 全域 AGENTS.md 範本](opencode-agents-template.md):其中「知識庫(RAG)使用原則」用觸發條件式規則,讓模型遇到規格 / 數值 / 型號類問題先查一次 KB —— 不強制,也不拖慢一般對話。

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
- `analyze_file` 會先做處理 — 圖片由 VL 做通用視覺分析（文字辨識、UI／終端機、表格、圖表、架構／流程關係與一般照片都包含），PDF 抽各頁文字（內嵌圖會標註頁碼張數但不做 VL），二進位檔則抓出檔頭格式和可讀字串 — 再把整理後的結果丟給模型。

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

# 整個家目錄都放開（最寬鬆，沒敏感檔的話最省事）
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 AI_CODE_IMPORT_ROOTS="$HOME" aicode
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
- ingest／remove 之後**查詢會自動偵測檔案變更並重載**（code 層保證，不再依賴人工記得）。`reload_knowledge_base()` 仍可用來「立即」載入並回報 chunk 數。

（另外 `import_external_file` 只負責把外部檔案帶進沙箱，本身不寫 KB。）

### 把附件做成知識庫讓模型隨時能查

「知識庫」是這個專案放規格書、手冊、設計文件的地方。一旦把文件匯進去，之後對話遇到相關問題時，系統會自動找出最相關的幾段內容當作回答依據，並用 `REF1` `REF2` 標出每段是引用自哪份文件的哪個位置。

比起每次都重新貼一份 PDF 給對話，這樣比較不會超出上下文長度限制，也比較不會記錯。

#### 支援格式

- **文字**：`.pdf` / `.md` / `.txt`（直接抽文字。**PDF 只抽文字**：內嵌圖不經 VL、不入庫，有內嵌圖時 ingest 輸出會列出 `[WARN]` 張數與頁碼——需要圖的內容就把該頁另存 `.png` 走圖片路徑補灌）
- **圖片**：`.png` / `.jpg` / `.jpeg` / `.gif` / `.webp`（用 VL 模型看圖、抽出文字描述後切 chunk，需要先把 VL GGUF 掛在 llama-server :8083,設定見 [README §2.4](../README.md#24-vl-模型) 與 §3.2）
- **binary**：`.bin` / `.dat` / `.raw` / `.fw` / `.img` / `.rom` / `.hex`（抽 hex dump、可讀字串、magic 偵測；遇到 ELF magic 自動切到 ELF 解析）
- **ELF**：`.elf` / `.so` / `.o` / `.axf` / `.out` / `.ko`（抽 header / sections / symbols）

純圖片掃描的 PDF（沒有可選文字）切不出內容（chunks=0 直接失敗），先把每頁存成 `.png` 再用 `ingest_document` 走圖片路徑，或先用 OCR 工具轉成文字檔再匯入。文字＋圖混合的 PDF（datasheet 類）會成功入庫**但只有文字部分**——看 ingest 輸出的 `[WARN]` 就知道哪幾頁的圖被略過。VL server 是啟動必要條件，若圖片分析仍失敗，先跑 `python scripts/required_model_servers_check.py` 看 `image_url` 多模態 probe。

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

`chunks` 是「切好的文件段落」。回報 0 代表沒匯入到任何內容 — 常見原因：純圖片掃描的 PDF（沒可選文字）、binary 太小或全是 0xff、VL llama-server (:8083) 沒啟動導致圖片分析失敗。

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

入庫後查詢時，圖片／截圖來源的 REF 會標 `origin: VL`（給人看的摘要則標 `·VL`），提醒模型這是視覺辨識的**機率性描述**、與原文抽取不同級——與文字 REF 衝突時以文字為準。規格數字題的拒答判斷也把 diagram/chat 排除在權威類型（spec/manual/api）之外。

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
- chunks 回報 0，圖片來源最常見的原因是 **VL server（:8083）沒起來** —— 圖片分析失敗就切不出內容。先跑 `python scripts/required_model_servers_check.py` 看 `image_url` 多模態 probe。

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

#### 三件容易踩的事

1. **知識庫綁專案目錄**：`knowledge.json` 存在當前專案根目錄裡，換到另一個專案就要重新匯入。同一份規格書在多個專案要用就匯入多次。
2. **不要 commit**：`knowledge.json` 切碎了原始文件內容，NDA 場景幾乎一定包含敏感片段。已經在 [安全邊界與工作節奏](security.md) 的「不要 commit 的資料」列入不該 commit 的清單，建議在專案的 `.gitignore` 也加一行。
3. **越具體越好**：把一整份 500 頁的手冊原封不動塞進去，不如先抽出實際會問到的章節整理成 markdown 再匯入。雜訊少，答案準。

一般 repo 對話、查 bug、改檔前的工作節奏放在 [基本操作](basic-usage.md)，這份文件只保留附件與知識庫細節。

---


## 文件與知識庫補充

操作流程的主體寫在上面的「把附件做成知識庫讓模型隨時能查」，這節只列幾個補充細節：

- `knowledge.json` 存在當前專案根目錄下，預設會被 `.gitignore` 忽略。它保存切碎後的文件內容，NDA 場景下幾乎一定有敏感片段，**不要 commit**。
- `remove_document(...)` 用檔名 basename 比對，所以傳完整路徑（`docs/old_spec.pdf`）或單純檔名（`old_spec.pdf`）都可以。刪除會在同一把 store lock 內同步重寫 JSON 與剩餘 NPZ 向量；不會刪掉整份向量檔再期待 reload 偷偷重算。
- 文件切段的大小、不同來源類型的搜尋權重，這些可調參數放在 `config.py` 的 `CHUNK_SETTINGS` 和 `SOURCE_TYPE_WEIGHTS`，預設值在大多數情境下已經夠用，要微調再去動。

---
