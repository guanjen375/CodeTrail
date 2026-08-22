# 基本操作

這份文件整理 CodeTrail 的基本操作：環境驗收、正常對話、夾帶附件、注入 RAG。完整 OpenCode TUI 安裝主線放在 [README](../README.md)；進階/替代安裝補充放在 [setup.md](setup.md)；工具細節放在 [MCP 工具清單](mcp-tools.md)。

[回到 README](../README.md)。

---

## 0. 環境驗收

照 [README](../README.md) 的 OpenCode TUI 流程完成後，先在 CodeTrail repo 裡跑：

```bash
AICODE_MODEL=<CODE_MODEL> python3 scripts/doctor.py
```

`<CODE_MODEL>` 是佔位符,必須替換成 MODEL_REGISTRY 裡登記的 bare name 或 GGUF 絕對路徑;如果你已經在 OpenCode JSON 設好同一顆模型,doctor 也能從設定檔解析。`FAIL` 要先處理;`WARN` 可以依訊息判斷是否需要調整。接著切到要分析的專案根目錄:

```bash
cd <PROJECT_TO_ANALYZE>
aicode
```

進入 TUI 前應先看到兩行健康狀態：`MCP PASS — 19 tools + list_dir round-trip`，以及 live 或 cached 的 `MODEL PASS`。前者每次都實跑；後者只有首次、24 小時到期或模型／設定／chat template／專案規則改變時才重新要求模型做一次真實結構化工具呼叫，所以平常不用先手動問 19 個工具。需要讀專案外附件時看「夾帶附件」；若 TUI 內後續某一輪仍異常，再用 `/status` 與 [常見問題的分層診斷](troubleshooting.md#mcp-connected-but-no-tool-call)交叉檢查。Connected 只證明 MCP transport 已初始化，模型在單一對話輪次仍可能失手。

---

## 1. 正常對話

CodeTrail 的使用方式不是把整個 repo 貼進對話，而是讓模型透過 MCP 工具按需讀檔、搜尋、查 RAG。

第一次進一個陌生 repo，可以先問：

```text
先不要改檔。
請用工具 list_dir 看兩層目錄，找出主要 entry point、測試目錄和設定檔。
再用工具 grep_code 或 code_rag_search 找初始化流程。
最後用 file:line 列出「證據」和「推測」。
```

正常情況下，你會看到 frontend 顯示 `list_dir(...)`、`grep_code(...)`、`code_rag_search(...)`、`read_file(...)` 這類工具的呼叫卡與真實結果,模型再用檔名與行號回答。單純輸出 `<codetrail_list_dir .../>` 文字不算呼叫。如果它沒有讀檔就直接回答，可以要求：

```text
請先用工具查證，不要只根據一般經驗回答。
```

常用說法：

| 需求 | 可以這樣問 |
|---|---|
| 看 repo 架構 | `請用 list_dir 看專案結構，找 entry point、測試和設定檔。` |
| 找錯誤訊息 | `請用 grep_code 搜尋 "panic: xxx"，再讀最可能的檔案。` |
| 分析原因並控制 context | `請用 code_rag_search mode=context、max_chars=12000 收集證據，分成已證實與 uncertainty。` |
| 看 caller / callee | `請用 code_rag_search mode=neighbors 查 uart_send，逐步附 path:line。` |
| 看 A 到 B 的呼叫鏈 | `請用 code_rag_search mode=path，query="A -> B"，只列 confirmed edge。` |
| 看已知檔案 | `請用 file_info 看 src/main.c 大小，再用 read_file 讀前 120 行。` |
| 查已匯入 spec | `請用 query_knowledge 查 reset timing，回答要附 REF。` |
| 高風險規格數字 | `請用 query_knowledge_strict 查最大值，證據不足就拒答。` |

完整工具清單見 [MCP 工具清單](mcp-tools.md)。

`mode="semantic"` / `mode="context"` 可在 graph 尚未建立時使用；`neighbors` / `path`
需要先建立 graph DB。若尚未建立，工具錯誤會直接附上含實際 Python 與 project root 的
可複製命令。不要猜 DB 路徑，照錯誤中的命令執行即可。

---

## 2. 夾帶附件

附件有兩種情況：檔案已經在專案目錄內，或檔案還在專案外。

### 檔案在專案目錄內

把檔案放在 `AICODE_ROOT` 底下，例如 `logs/build_fail.txt`、`screenshots/error.png`、`firmware/boot.bin`。然後在對話裡明確要求使用工具：

```text
請用工具 read_file 讀 logs/build_fail.txt，找出最重要的錯誤訊息。
```

```text
請用工具 analyze_file 分析 screenshots/error.png，辨識畫面上的錯誤文字。
```

```text
請用工具 analyze_file 分析 firmware/boot.bin，整理檔頭、magic 和可讀字串。
```

`read_file(...)` 適合文字；`analyze_file(...)` 適合圖片、PDF（一次性抽文字）、ELF、firmware binary。這些操作只把附件帶進目前對話，不會建立可長期查詢的知識庫。想讓圖片或附件之後反覆查，改用 §3 的 `ingest_document(...)`（圖片會自動走 VL 看圖再進 RAG）。

### 檔案在專案目錄外

預設不能直接讀 `$HOME`、`Downloads` 或其他專案外路徑。要匯入外部附件，啟動時打開匯入功能：

```bash
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 aicode
```

`AI_CODE_ALLOW_EXTERNAL_IMPORT=1` 是總開關。預設可匯入來源是 `~/Downloads` 和 `/tmp`。如果附件在其他目錄，用 `AI_CODE_IMPORT_ROOTS` 指定白名單；一旦設定就會取代預設清單：

```bash
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 \
AI_CODE_IMPORT_ROOTS="$HOME/Downloads:/tmp:$HOME/specs" \
aicode
```

進入 TUI 後請模型先匯入，再分析回傳的新路徑：

```text
請用工具 import_external_file 匯入 ~/Downloads/error.log，
再用 read_file 讀回傳的新路徑，整理最重要的錯誤。
```

匯入後的檔案會複製到專案底下 `.aicode_uploads/`，原始檔不會被修改。更多副檔名、白名單與圖片/binary 細節見 [RAG、附件與知識庫操作](rag.md)。

如果外部 PDF / spec / 截圖圖片也要注入 RAG，先 `import_external_file`，再把回傳的 `.aicode_uploads/...` 路徑交給 `ingest_document`（圖片會自動走 VL）；完整串接範例見 [RAG、附件與知識庫操作](rag.md#同時處理外部附件並注入-rag)。

---

## 3. 注入 RAG

如果要讓模型之後能反覆查 spec、datasheet、manual 或設計文件，不要只用 `read_file(...)` 看一次。改成匯入知識庫：

```text
請用工具 ingest_document 匯入 docs/npu_spec.pdf，
完成後用工具 reload_knowledge_base，
最後回報目前載入幾個 chunks。
```

成功時 chunks 會大於 0。接著查詢：

```text
請用工具 query_knowledge 查 conv2d 的輸入大小限制，
回答時每個數字都要附 REF。
```

對「最大值、預設值、timing、reset 時間」這類答錯會造成風險的題目，用嚴格模式：

```text
請用工具 query_knowledge_strict 查 reset assert 最小持續時間，
證據不足就拒答，不要用常識補。
```

圖片附件（截圖、架構圖、被拍成圖的規格頁）也能進 RAG，跟 PDF 走同一套 —— `ingest_document` 看到圖片副檔名會自動用 VL 看圖、抽成文字再切 chunk（**不必先 `analyze_file`**），之後一樣用 `query_knowledge` 查：

```text
請用工具 ingest_document 匯入 docs/block_diagram.png，
完成後 reload_knowledge_base，
再用 query_knowledge 查圖裡兩個模組怎麼接，回答附 REF。
```

聊天截圖要抽對話內容改 `ingest_document('shot.png', mode='chat')`；圖片在專案外就先 `import_external_file` 再 ingest。

PDF 裡的**表格 / 終端機畫面**（datasheet、register map、log）多的話，先估成本再入庫，
最後覆核。preflight 是零寫入的（但它只算**結構化 lane** 的成本；純 raster 內嵌圖走的
自由文字 VL 不受那些上限判定，所以「在預算內」不等於整份 PDF 都便宜）：

```text
請用工具 ingest_document 匯入 docs/datasheet.pdf，preflight_only 設 True，
回報候選數、VL 呼叫次數與有沒有超過上限。
```

```text
請用工具 ingest_document 匯入 docs/datasheet.pdf，
完成後用工具 review_figures，action 設 "list"，列出待覆核的圖與原因。
```

REF 出現「待覆核」代表程式沒能用獨立證據佐證那張圖的內容 —— `query_knowledge_strict`
**不會**拿它回答數值，但會在 `excluded_figures` 裡告訴你是哪一頁、哪一張、為什麼
（那不是「查不到」）。**只有 structured figure（`excluded_figures` 帶 `figure_id` 的那些）**能用
`review_figures(action="fix", ..., confirm_against_image=True)` 人工覆核（會改知識庫，
permission 是 `ask`）。
**注意範圍**：純 raster 的掃描頁表格與拍照的終端機畫面本輪仍走舊的 VL 描述路徑，
不會出現在 `review_figures` 裡，本輪也沒有把它們升成 strict 可信的路徑——那些數字只能
自己回去看原始 PDF 那一頁。細節見 [RAG、附件與知識庫操作](rag.md#pdf-內的表格與終端機畫面結構化抽取--人工覆核)。

基本判斷：

- `query_knowledge(...)` 適合一般查文件，速度較快。
- `query_knowledge_strict(...)` 適合規格數字與限制，較慢但會做證據檢查。
- 新增或移除文件後查詢會自動載入變更；`reload_knowledge_base(...)` 用來立即確認 chunk 數。
- PDF 圖很多時先 `ingest_document(path, preflight_only=True)` 估成本（零寫入），再決定要在對話裡跑還是改用 CLI。
- `knowledge.json` 會保存切碎後的文件內容，NDA 場景不要 commit。

完整流程、支援格式、圖片 VL 分析、binary/ELF 匯入和舊文件移除見 [RAG、附件與知識庫操作](rag.md)。

---

## 4. 最小驗收流程

剛裝好時，建議照順序跑一次：

```text
請用工具 list_dir 看專案兩層目錄，列出 entry point、測試目錄和設定檔。
```

```text
請用工具 read_file 讀 README.md 前 80 行，整理這個專案怎麼啟動。
```

```text
請用工具 import_external_file 匯入 ~/Downloads/error.log，
再用 read_file 讀回傳的新路徑，整理最重要的錯誤。
```

```text
請用工具 ingest_document 匯入 docs/spec.pdf，
完成後 reload_knowledge_base，
再用 query_knowledge 查一個 spec 問題，回答要附 REF。
```

前兩個驗證正常對話與專案讀檔；第三個驗證附件匯入；第四個驗證 RAG。若暫時沒有外部 log 或 spec，可以先建立小型 `.txt` 測試檔放在 `~/Downloads` 或專案 `docs/` 底下。

---

## 5. 要改檔時

先讓模型查證，再允許 patch：

```text
根據上面的 file:line 證據，請做最小修改。
套用 patch 前先說會改哪些檔案；套用後跑最小相關測試。
如果 run_command 被白名單拒絕，請列出你原本想跑的命令。
```

`apply_patch(...)` 會真的寫檔，`run_command(...)` 會執行白名單命令。只想分析時要明講「不要改檔」。安全邊界與副作用工具說明見 [安全邊界與工作節奏](security.md)。

---

## 6. 糾正模型的做事方式(lessons)

同一種糾正不想每個 session 重講一次時,糾正完接一句:

```text
把這條記成 lesson,之後的 session 都要遵守。
```

模型會用 `record_lesson(...)` 提案一條祈使句行為規則,**你在核准框看到內容、同意才寫入**;下個 session 起由 `aicode` 自動注入(啟動輸出有 `[lessons] N 條 active lessons 已注入 ...`)。規則 90 天到期會停止注入並在啟動時提示複審。生命週期、上限與 `python3 lessons.py list / renew / delete` 管理指令見 [docs/lessons.md](lessons.md)。

---

## 7. Web 模式(瀏覽 / 續問歷史 session)

§0 的 `aicode` 是 standalone TUI。如果你想用瀏覽器瀏覽歷史 session、點任一筆續問，
或讓瀏覽器與 TUI client 連到**同一個 backend**，改用 web 模式並讓 TUI 端走
`aicode attach`。不要在同一個專案同時另開 standalone `aicode` 與 `aicode_web`；兩個
backend 會共用 session 資料庫而互相干擾。

web backend 會 spawn CodeTrail MCP。`set_config.sh` 會把 MCP Python 的絕對路徑寫進
OpenCode 設定；但如果 CodeTrail 依賴只裝在 venv，`aicode` / `aicode_web` 的啟動前置
仍應在 activate 後執行（見 [安裝、設定與啟動](setup.md)）。`aicode attach` 是純 client，
不跑 backend preflight，也不需要 Python 環境。

### 啟動 web backend

A 機和 B 機已登入同一個 tailnet 時,使用背景 launcher:

```bash
# A 機先啟動四個模型 server(每次開機一次)
~/start.sh

# 再鎖定要分析的專案並啟動 web
cd <PROJECT_TO_ANALYZE>
aicode_web
```

如果 A 機的啟動檔放在桌面,第一行可改成 `cd ~/Desktop && ./start.sh`；標準 `set_config.sh` 產物則是 `~/start.sh`。`aicode_web` 會讀 `tailscale ip -4`,只綁 A 機的 Tailscale IPv4 與固定 port `4096`(可用 `AICODE_WEB_PORT` 覆寫)，在 tmux 背景執行，ready 後印出 B 機要開的 `http://100.x.y.z:4096/`。A 機沒有 GUI 是預期情況。

沙箱 root 檢查、模型解析、ctx safety 與 `AI_CODE_*` 透傳全部跟 standalone TUI 一致 —— 例如要讀專案外附件一樣加 `AI_CODE_ALLOW_EXTERNAL_IMPORT=1 aicode_web`。停止 backend 用 `aicode_web stop`。

沒用 Tailscale時,低階入口仍可用 `aicode web`(前景、預設 `127.0.0.1:4096`)或 `aicode_web --local`(背景),再從 B 機做 SSH tunnel:`ssh -L 4096:127.0.0.1:4096 <帳號>@<A機>`。完整步驟見 [README §5.4](../README.md#54-web-模式目前測試中)。

首頁就是 session 清單,點任一筆即可載入該 session 繼續對話。

驗證 MCP 連通:在 web 介面挑一個 session 問「請用工具 list_dir 看當前目錄結構」，模型應該透過 CodeTrail 呼叫 `list_dir(...)` 回真實結果(OpenCode log 裡可能顯示成 `codetrail_list_dir`)。

### Attach TUI 到同一個 backend

另開一個終端:

```bash
aicode attach                              # 預設接 http://127.0.0.1:4096
aicode attach http://127.0.0.1:4096 -c     # 指定 url，並用 -c 續接上一個 session
aicode attach -s <SESSION_ID>              # 接上指定 session
```

attach 端與 web 端**共用同一份 session 與狀態**:web 發問後 TUI 看得到新訊息，TUI 切 session 也會反映在 web。CodeTrail MCP 只在 backend 冷啟一次，attach 端不會再起第二個。TUI 內 `/status` 應看到 `codetrail Connected`。

### 安全注意(重要)

未設 `OPENCODE_SERVER_PASSWORD` 時 OpenCode server 沒有應用層密碼。`aicode_web` 的無密碼例外非常窄:wrapper 傳入的 hostname、`tailscale ip -4` 當下值與 Tailscale `100.64.0.0/10` 必須三者吻合,且只 listen 該 virtual interface；傳輸由 Tailscale 加密、授權由 tailnet ACL 負責。普通 `aicode web` 若綁任何非 loopback 位址(`0.0.0.0` / LAN IP)或開 `--mdns`,仍會強制要求密碼。**絕不可用 `tailscale funnel`**。詳見 [安全邊界與工作節奏](security.md)。
