# RAG、附件與知識庫操作

這份文件整理附件匯入、知識庫建立、Code-RAG 搜尋與規格查詢方式。CodeTrail 啟動聊天 frontend 前會硬性檢查 llama-server `:8081` (embedding)、`:8082` (reranker) 與 `:8083` (VL) 都 ready。

[回到 README](../README.md)。

---

## 重點教學

啟動 aicode 進到對話之後，最常碰到兩件事：

1. 有一張錯誤截圖／一份韌體 binary／一段 log，想讓對話幫忙看。
2. 有一份產品規格書／datasheet／設計手冊，想讓之後對話遇到相關問題時答得準。

這兩件事分別對應下面的「在對話裡讓模型看到一個檔案」和「把附件做成知識庫讓模型隨時能查」。讀完這兩節就能開始實用。

### 在對話裡讓模型看到一個檔案

#### 場景一：檔案在當前專案目錄裡

最簡單。放到 `cd` 進去那個目錄底下任何位置，然後在對話裡點名工具和檔案路徑：

| 檔案類型 | 對話可以這樣說 |
|---|---|
| 程式碼 / log / 純文字 | 「請用工具 `read_file` 讀 `logs/build_fail.txt`」 |
| 截圖 / 圖片 | 「請用工具 `analyze_file` 分析 `screenshots/error.png`」 |
| 韌體 / 執行檔 / 二進位（.bin / .elf / .img） | 「請用工具 `analyze_file` 分析 `firmware/boot.bin`」 |

兩個工具差別：

- `read_file` 直接把純文字內容讀進對話。
- `analyze_file` 會先做處理 — 圖片做文字辨識、二進位檔抓出檔頭格式和可讀字串 — 再把整理後的結果丟給模型。

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

1. 先用 `import_external_file` 把外部檔案複製進專案。
2. 對回傳的新路徑做 `read_file` 或 `analyze_file`，讓模型這一輪先看內容。
3. 對同一個新路徑做 `ingest_document`，再 `reload_knowledge_base`，讓之後的對話也查得到。

範例：

```text
請用工具 import_external_file 匯入 ~/Downloads/npu_spec.pdf，
用回傳的新路徑先做 file_info 確認檔名與大小，
再用同一個新路徑執行 ingest_document，
完成後 reload_knowledge_base，
最後用 query_knowledge 查這份 spec 的版本號並附 REF。
```

若是外部 log 或純文字檔，可以在 `ingest_document` 前先 `read_file` 摘要；若是圖片、ELF 或 firmware binary，可以先 `analyze_file`。重點是 **`import_external_file` 只負責把檔案帶進沙箱，不會自動寫入 KB**；要長期查詢一定還要呼叫 `ingest_document` 和 `reload_knowledge_base`。

### 把附件做成知識庫讓模型隨時能查

「知識庫」是這個專案放規格書、手冊、設計文件的地方。一旦把文件匯進去，之後對話遇到相關問題時，系統會自動找出最相關的幾段內容當作回答依據，並用 `REF1` `REF2` 標出每段是引用自哪份文件的哪個位置。

比起每次都重新貼一份 PDF 給對話，這樣比較不會超出上下文長度限制，也比較不會記錯。

#### 支援格式

- **文字**：`.pdf` / `.md` / `.txt`（直接抽文字）
- **圖片**：`.png` / `.jpg` / `.jpeg` / `.gif` / `.webp`（用 VL 模型看圖、抽出文字描述後切 chunk，需要先把 VL GGUF 掛在 llama-server :8083,設定見 [README §2.4](../README.md#24-vl-模型) 與 §3.2）
- **binary**：`.bin` / `.dat` / `.raw` / `.fw` / `.img` / `.rom` / `.hex`（抽 hex dump、可讀字串、magic 偵測；遇到 ELF magic 自動切到 ELF 解析）
- **ELF**：`.elf` / `.so` / `.o` / `.axf` / `.out` / `.ko`（抽 header / sections / symbols）

純圖片掃描的 PDF（沒有可選文字）切不出內容，先把每頁存成 `.png` 再用 `ingest_document` 走圖片路徑，或先用 OCR 工具轉成文字檔再匯入。VL server 是啟動必要條件，若圖片分析仍失敗，先跑 `python scripts/required_model_servers_check.py` 看 image_data probe。

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

`ingest_document` 會把整份文件切成多段、算出每段的向量、存進專案根目錄的 `knowledge.json`。`reload_knowledge_base` 把剛存進去的內容立刻吃進記憶體 — **每次匯入或刪除文件後都要呼叫**，不然查不到。

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
| 看完還要之後反覆查 | `ingest_document('diagram.png')` → `reload_knowledge_base()` | ✓ VL 抽完寫進 knowledge.json |

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
對回傳的新路徑做 analyze_file 讓我這一輪先看到畫面，
再對同一個新路徑做 ingest_document，最後 reload_knowledge_base。
```

兩個常踩的點：

- **預設走「技術圖片」路徑**（架構圖／流程圖／記憶體圖），抽的是畫面說明。若這張是**聊天截圖**、想抽的是對話內容，要顯式 `ingest_document('teams.png', mode='chat')`。
- chunks 回報 0，圖片來源最常見的原因是 **VL server（:8083）沒起來** —— 圖片分析失敗就切不出內容。先跑 `python scripts/required_model_servers_check.py` 看 image_data probe。

#### 規格題、數字題用嚴格模式

「最大值是多少」「預設值是什麼」「reset 訊號最少要拉幾毫秒」這種**答錯比不答更糟**的題目，改用 `query_knowledge_strict`：

```text
請用工具 query_knowledge_strict 查 reset assert 最小持續時間，
證據不夠就直接拒答，不要用常識補。
```

兩者差別：

- `query_knowledge`：把找到的文件段落丟給對話模型，模型自己組答案。
- `query_knowledge_strict`：在背後跑兩階段檢查 — 先看找到的內容是不是真的足以回答；確認後再驗證最終答案每一句話都有對應的 `REF` 出處；任何一句沒對到的會被刪掉，證據真的太弱就直接回「拒答」而不是亂編。

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
- `remove_document(...)` 用檔名 basename 比對，所以傳完整路徑（`docs/old_spec.pdf`）或單純檔名（`old_spec.pdf`）都可以。
- 文件切段的大小、不同來源類型的搜尋權重，這些可調參數放在 `config.py` 的 `CHUNK_SETTINGS` 和 `SOURCE_TYPE_WEIGHTS`，預設值在大多數情境下已經夠用，要微調再去動。

---
