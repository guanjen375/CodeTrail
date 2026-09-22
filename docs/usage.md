# CodeTrail 使用指南

安裝、準備模型與日常啟停見 [README](../README.md)。這份指南介紹聊天、附件、RAG、
人工覆核與程式分析；工具參數及上限見 [MCP 工具契約](mcp-tools.md)，
部署、設定、資料保存政策與故障排解見 [開發與維運](../developer.md)。

- [聊天與工具結果](#chat)、[選取與複製](#copy)、[thinking](#thinking)
- [歷史對話](#sessions)、[排隊與補充](#queue)、[工作區審查](#review)
- [附件](#attachments)、[知識入庫與查詢](#rag)、[PDF 圖面覆核](#pdf-review)
- [表格與 OCR](#text-table-review)、[入庫續跑](#ingest-resume)、[KB 維護](#knowledge-maintenance)
- [行為 lessons](#lessons)、[build target](#build-context)、[記憶體配置核對](#memory-consistency)

<a id="chat"></a>

## 聊天與工具結果

```bash
cd <PROJECT_TO_ANALYZE>
aicode
```

正常啟動對話區空白，啟動摘要、壓縮模式與全部 WARN 在 `/status`。
健康檢查失敗會在終端報錯並拒絕進入 TUI。`/tools` 查看 21 個 MCP 工具，
`/help` 查看互動指令。首次直接送出問題或 `/new` 才建立 session。

CodeTrail 的使用方式不是把整個 repo 貼進對話，而是讓模型透過 MCP 工具按需讀檔、搜尋、查 RAG。

第一次進一個陌生 repo，可以先問：

```text
先不要改檔。
請用工具 list_dir 看兩層目錄，找出主要 entry point、測試目錄和設定檔。
再用工具 grep_code 或 code_rag_search 找初始化流程。
最後用 file:line 列出「證據」和「推測」。
```

正常情況下，你會看到 frontend 顯示 `list_dir(...)`、`grep_code(...)`、`code_rag_search(...)`、`read_file(...)` 這類工具的呼叫卡與真實結果,模型再用檔名與行號回答。單純輸出 `<list_dir .../>` 文字不算呼叫。如果它沒有讀檔就直接回答，可以要求：

```text
請先用工具查證，不要只根據一般經驗回答。
```

常用說法：

| 需求 | 可以這樣問 |
|---|---|
| 看 repo 架構 | `請用 list_dir 看專案結構，找 entry point、測試和設定檔。` |
| 找錯誤訊息 | `請用 grep_code 搜尋 "panic: xxx"，再讀最可能的檔案。` |
| 分析原因並控制 context | `請用 code_rag_search mode=context 收集證據；省略 max_chars，依動態預算分成已證實與 uncertainty。` |
| 看 caller / callee | `請用 code_rag_search mode=neighbors 查 uart_send，逐步附 path:line。` |
| 看 A 到 B 的呼叫鏈 | `請用 code_rag_search mode=path，query="A -> B"，只列 confirmed edge。` |
| 看已知檔案 | `請用 file_info 看 src/main.c 大小，再用 read_file 讀前 120 行。` |
| 查已匯入 spec | `請用 query_knowledge 查 reset timing，回答要附 REF。` |
| 高風險規格數字 | `請用 query_knowledge_strict 查最大值，證據不足就拒答。` |

完整工具清單見 [MCP 工具清單](mcp-tools.md)。

回合進行中仍可輸入訊息，選擇「排到下一輪」或「補充目前任務」。等待／送達狀態及
查看、修改、取消操作見 [訊息排隊與途中補充](#queue)。

<a id="copy"></a>

### 選取、複製與中斷

用滑鼠左鍵拖選文字，放開即自動複製，不需按鍵。串流中的回答、完成的回答與接續歷史後
重播的回答都可選取；完成與重播的回答保留 Markdown 標題、清單及程式碼上色。
文字元件的原生雙擊／三擊選取也會自動複製。

| 操作與情境 | 行為 |
|---|---|
| **滑鼠左鍵拖曳選取後放開** | 自動複製本次選取；主畫面、輸入框、核准框與審查中的文字都可用，不會中斷、核准或關閉畫面 |
| **手動複製鍵（預設 F2）** | 再次複製目前畫面的選取文字；忙碌、核准框或其他選單開著時也可用 |
| **Ctrl+C**，閒置主畫面有選取 | 複製；優先取對話區選取，其次取目前焦點輸入框的選取 |
| **Ctrl+C**，閒置主畫面沒有選取 | 連按兩次離開 |
| **Ctrl+C**，回合／核准／審查進行中 | 中斷目前回合或審查；有選取文字也一樣。對話選單開著時則只收選單 |
| **Ctrl+D**，閒置主畫面 | 直接離開 |
| **Ctrl+Y**，輸入框 | 重做（redo） |

滑鼠自動複製只取本次手勢所在畫面的選取；選取輸入框時只取該輸入框，不會取被彈出視窗
遮住的對話或其他草稿。普通點擊、捲動條、右鍵與程式更新文字不觸發自動複製。
手動複製鍵優先取目前畫面的文字選取，其次取目前焦點輸入框的選取。
複製後保留反白，可再次選取或按手動複製鍵重新複製；要用 Ctrl+C 離開，先取消選取。
沒有選取時不清空剪貼簿。複製操作不顯示成功通知，請在本機貼上確認內容；
SSH、tmux 與終端支援的設定見[剪貼簿排查](../developer.md#clipboard-ssh-tmux)。


需要手動複製時，用 `/copykey` 查看目前鍵與可用鍵，`/copykey f3` 立即改成 F3，`/copykey reset` 回 F2。
允許 **F1、F2、F3、F4、F5、F8、F9、F10、F11、F12**；F6／F7 保留給輸入框選取，
Ctrl／Alt 組合與其他既有操作鍵不接受。新設定保存到 owner-only `client.json` 的 `copy_key`，
重新啟動仍生效；更新成功後舊鍵不再複製，寫入失敗則維持原鍵。
更換手動複製鍵不影響滑鼠自動複製與 Ctrl+C 中斷。

<a id="thinking"></a>

### 主聊天 thinking

每次啟動預設 `think=off`。在沒有回合、核准或審查進行時使用：

```text
/think           切換主聊天 thinking
/think on        開啟
/think off       關閉
```

狀態列顯示 `think=on|off`，這個選擇不寫入 `client.json`。`set_config.sh` 必須先從
主模型的 chat template 偵測出 thinking 控制鍵，才能開啟；未確認支援或沿用缺少能力
欄位的舊部署設定時會明確拒絕，可重跑設定後重新啟動客戶端。

實際收到 reasoning 時，對話區顯示紅字「思考中」；開始回答或工具活動，以及完成、
出錯或取消時會清掉這個暫時標記。`client.json` 的 `show_reasoning`(預設 `false`)
只控制 reasoning 本文與歷史重播是否顯示，不改生成開關。舊 reasoning 是否送回模型
仍由另一個鍵 `keep_historical_reasoning` 決定。

摘要、審查、RAG 與其他內部生成一律關閉 thinking。主聊天開啟 thinking 時不做
prompt cache 預熱；詳細延遲與快取診斷見 [常見問題](../developer.md#troubleshooting)。

### 等待回應與壓縮進度

每次模型請求從收到第一個 prompt 進度快照起算,前 10 秒整個狀態仍顯示「等待回應」。
若此時已開始生成,就立即顯示 thinking、答案或工具活動。較久的 prefill 才展開詳細進度,
之後每 10 秒取一次最新快照;工具執行後的下一次模型請求會重新計時。狀態列依終端寬度
最多展開至 3 行,生成或結束時恢復 1 行。例如:

```text
prompt processing(42%) · 8,400/20,000 tok · cache 2,000 · 213.3 tok/s · prefill 30s
```

數字來自**本次請求**的 llama.cpp SSE `prompt_progress`,與 tmux 中 llama-server
回報的處理計數同源。`processed/total` 是已處理/總 prompt token,`processed` 已含 cache,
百分比是 `floor(100 * processed / total)`。只有 cache 與 `time_ms` 可信、
`processed > cache` 且 `time_ms > 0` 時,才顯示 `(processed-cache)/(time_ms/1000)` 的
速率與 `prefill` 耗時。初始 `processed == cache` 快照只顯示計數、cache 與百分比,
即使已等了 10 秒,也不顯示 0 速率、prefill 時間或「距更新」。缺少或不可信的欄位會省略,
沒有可靠計數就只顯示等待/處理階段,不估算百分比、速率或剩餘時間。

初始快照之後,server 每完成一個最多 `n_batch` token 的批次才推送一次快照;
目前部署每批可花數秒到十餘秒。UI 的 10 秒是取樣顯示節奏,不是 server 保證的更新頻率。
已有 `processed > cache` 的批次快照且 10 秒沒有新資料時,會標示「距更新 Ns」;
這只表示快照距今多久,可能是下一批尚未完成,不表示停滯。`prefill Ns` 來自 server
快照中的耗時,回合秒數則是客戶端經過時間,兩者的起點、涵蓋階段與更新時機不同。

手動 `/compact` 與自動壓縮使用同一套進度,等待與 prompt 階段帶 `compact ·` 前綴;
接著是「產生摘要中」、「驗證摘要中」、「儲存摘要中」。`100%` 只表示摘要 prompt 已處理完,
摘要仍須生成、通過驗證並成功儲存。最後是否成功以壓縮結果通知為準。這些進度只存在
當前狀態列,不寫入對話、摘要或 headless JSON;結束、中斷或切換對話時清除。

### 怎麼讀工具結果

每個 CodeTrail 工具的精簡文字結果第一行固定是 `status: ok|partial|error`。只有
`partial`／`error` 或因 context budget 截斷時，第二行才有可直接照做的 `next:`；例如
`read_file` 會給不漏行的下一個 `start_line`，`grep_code` 會要求縮小
path/include/pattern。不要把 `partial` 當完整證據，也不要略過 `next:` 直接重送同一呼叫。

未明示 `max_chars` 時，結果預算在**每次呼叫當下**依主模型 `n_ctx` 的 12% 計算，使用固定
ASCII/CJK token 代理估算，再套各工具 safety cap；已不再是固定 12,000 字元。明示較大的
`max_chars` 仍受 schema 範圍與 safety cap 限制，而且超過 12% 預設時會標
`context_risk`。`code_rag_search(mode="context")` 回傳的 `used_chars` 仍只是
`evidence[].text` 字元數，不是 tokenizer token。完整契約見
[MCP 工具清單：結果文字與預算契約](mcp-tools.md#結果文字與預算契約)。

`mode="semantic"` / `mode="context"` 可在 graph 尚未建立時使用；`neighbors` / `path`
需要先建立 graph DB。若尚未建立，工具錯誤會直接附上含實際 Python 與 project root 的
可複製命令。不要猜 DB 路徑，照錯誤中的命令執行即可。

<a id="sessions"></a>

## 切換、接續與壓縮對話

對話是**每個專案**各自保存的(session 檔在 state 目錄,不在被分析的 repo 裡)。回到同一個
專案時直接執行 `aicode`，再由 TUI 選單接續；shell 入口不接受參數。

啟動只顯示空白畫面，不建立 session。`/new` 或直接送出第一則問題時才建立新對話；
先查看 `/status`、挑選歷史或直接退出，都不會留下空白 session 檔。

```text
/session         開選單挑一段(最多 50 筆;↑/↓ 移動、Enter 接續、Esc 取消)
/session <id>    不開選單,直接換到那一段
/new             開一段新對話
```

每一列的最後一欄是**大綱**:那段對話裡你自己問的第一句話(超長會截成一行)。它是從
session 檔算出來的純文字,**不會呼叫模型、也不會回寫 session 檔** —— 選單不該為了好看
多送一次 NDA 內容出門。壓縮注入的摘要與工具輸出都不算「你問的話」,所以不會出現在
那一欄。

換過去之後畫面會**重播那段對話的原始記錄**:你的問題、模型的回答與 thinking、每一次工具
呼叫的名稱、參數、狀態與完整結果。幾件事值得知道:

- **壓縮過的對話仍然看得到壓縮前的原文。** 摘要只在原文之後多一個可展開的「壓縮摘要」
  標記,寫明壓縮掉幾則、逐字保留幾則。畫面是給你看的紀錄,模型看到的仍然是壓縮後的
  歷史 —— 兩者刻意不一樣,notice 那一行會同時報「畫面 N 則、模型歷史 M 則、壓縮 K 次」。
- **當時沒有拿到結果的工具呼叫**會標成待處理並寫明「這次呼叫沒有結果」,不會假裝有
  結果;真的被你 Ctrl-C 中斷的那一次,結果是下一題送出前才補上「已中斷」的。
- `client.json` 的 `show_reasoning` 也控制重播的 reasoning 本文；`/think` 不改歷史內容。
- 這一輪還在跑的時候不會換(會告訴你「要等它結束;Ctrl-C 可以中斷它」);讀不到那段
  對話時也**什麼都不動** —— 畫面與模型歷史仍然是原來那一段,不會換到一半。

在 TUI 外面想先看有哪些對話,用客戶端的 `sessions` 子命令(輸出是 tab 分隔,適合
`grep`):

```bash
cd <PROJECT_TO_ANALYZE>
python3 <CODETRAIL_REPO>/codetrail_chat.py sessions
```

`/compact` 明示要求壓縮；`codetrail` 模式也會在回答完成或接續歷史後檢查門檻。
`manual`／`off` 不會自動摘要。摘要只改模型歷史，畫面保留原始記錄；
失敗不會用半份摘要替換對話。完整門檻、七條規則、停用狀態與代價見
[壓縮規則](compaction-rules.md)。`codetrail`／`manual` 仍是實驗功能。
<a id="queue"></a>

## 訊息排隊與途中補充

`aicode` 閒置時照常送出問題。回合忙碌時按 Enter，會先讓你選擇：

- **排到下一輪**：目前回合成功收尾後，依加入順序逐則開始新回合。
- **補充目前任務**：等整批工具結果都寫好，在下一次模型請求前加入原文。正在進行的 HTTP、工具寫入和核准保持原有流程。

剛啟動時尚未建立 session。`/queue add` 與 `/queue resume` 會提示先用 `/new` 或直接
輸入第一則問題，不會為了排隊先建立空白對話。接續 `/session` 選定的歷史後也可使用佇列。

Esc 只關閉這個選擇框，原文保留在輸入框。Ctrl-C 仍取消目前整輪；核准框的拒絕仍只拒絕該工具。加入佇列不等於工具核准，唯讀政策仍然生效。

等待工具核准時，可按核准框的「加入訊息」，輸入後選排隊或補充；該工具仍保持待核准。編輯訊息時輸入 y/n 不會回答核准。若核准框因逾時或取消收起，尚未送出的草稿會回到輸入框或輸入歷史。

| 指令 | 作用 |
| --- | --- |
| `/queue` 或 `/queue list` | 查看本對話的待送項目與近期收據，不送模型 |
| `/queue add <文字>` | 明確排到下一輪 |
| `/supplement <文字>` | 明確補充目前任務 |
| `/queue edit <id> <文字>` | 修改尚在 waiting/deferred 的完整原文 |
| `/queue cancel <id>` | 取消尚未接收的項目 |
| `/queue resume` | 本輪中斷或失敗後，明示繼續待送項目 |

`waiting` 表示仍在等待，`delivering` 表示引擎正在接收、已不能修改。`delivered` 只表示原文已進入真實 user 歷史，畫面才會出現 UserMessage；它不代表模型已回答或工具已成功。正常 session 會保存這些 user 訊息，落檔失敗沿用既有明確警告。

本輪已輸出最後答案、正在壓縮、被取消、出錯或用盡模型步數時，尚未加入歷史的補充會標成 `deferred`，保留到下一輪，不會冒稱已送本輪。成功收尾才自動繼續；取消或錯誤會暫停整個待送佇列，必須 `/queue resume`。檢視、修改、取消都不會自動續跑。閒置時直接加入佇列也會等待明示 resume。

每項都綁定加入時的 session 與 turn。選擇框停留到原回合結束後才選「補充」，會改為待下一輪，不會插入另一個正在執行的任務。尚有未送項目時，切換/新建對話、手動壓縮與正常退出會提示先處理佇列；可送完或逐項取消。`/session <id>` 接續指定對話；`/queue resume` 繼續目前對話的待送項目。

佇列只存在目前 TUI 的記憶體內，最多 32 項、單項 64 KiB、合計 256 KiB UTF-8 原文。近期已送/已取消收據會先被淘汰，待送內容不會為了騰出空間被丟棄。程序被強制終止時不能恢復尚未送出的排隊狀態；正常離開會先擋下並列出待處理提示。已送訊息仍按既有 session 保存政策保存。
<a id="review"></a>

## 工作區程式碼審查

在要審查的 Git repository 根目錄啟動 `aicode`，輸入：

```text
/review
```

不需要參數、外部 review CLI 或 Node/npm。審查使用目前設定的 llama.cpp 主模型；
A/B 分離部署時，沿用已授權的模型端點。

### 範圍與結果

範圍是 **HEAD 到目前工作目錄的淨變更，加上未追蹤且未忽略的新檔案**。
尚未有 HEAD 的新 repo 使用空基底。不提供 branch、commit 或全 repo scan 模式。
若 index 與工作目錄的修改互相抵銷，會列出 index 仍有變更，
不會把「目前淨變更為零」說成沒有 staged 內容。未解決的 merge conflict 會中止審查。

畫面依序顯示來源與逐檔進度，最後列出有位置、觸發條件、影響及原文證據的問題。
模型可以用唯讀工具查閱相關 caller/callee；結果必須通過檔名、changed hunk、
行號與逐字證據檢查，才會當成 finding 顯示。沒有通過格式檢查的回答不算零問題。

變更中的二進位、特殊檔案、未知 Git 內容轉換與超出資源上限的候選來源會明列缺口；
模型或工具錯誤、context gate 拒絕、取消與來源漂移也會顯示未完成。
審查完成前會重新核對快照，避免將過時行號套用到已變動的工作區。
只有全部選定來源完成時才顯示完整結果；空 findings 表示此次未確認問題。

### 操作與資料保留

結果在獨立、可捲動的 TUI 畫面顯示，**不寫入聊天歷史，也不自動匯出檔案**。
中間模型草稿不當成已確認問題呈現。關閉後可重新輸入 `/review` 取得新快照。

Ctrl-C 取消正在進行的審查，包括來源收集、MCP 啟動、模型串流與工具等待。
審查佔用目前對話的回合鎖；完成或取消後才可送出一般對話、壓縮或切換 session。
進行中關閉審查畫面同樣會先取消並收尾。

審查使用獨立的 `--readonly` MCP、唯讀 policy 與受限的 context 工具集合；
原本聊天的 MCP、工具權限及訊息保留原狀。模型請求沿用同一把模型鎖、live n_ctx、
精確 token 計數與 context gate，不啟動額外的預熱或自動壓縮。

### Git 來源與相容性

Git 來源收集不執行 repo 的 filter、hook 或 fsmonitor，並隔離繼承的 Git 設定。
一般 CRLF/text 正規化會核對 index 與屬性，並安全讀取全域 `core.autocrlf`。
先以有界串流掃描確認候選，已證明未變更的來源不佔逐檔審查與 payload 預算。
一般 clone 或 deinit 留下的空 submodule，在確認目錄為空且 gitlink 未變時不列缺口；
非空、指標變更或無法確認內容的 submodule 仍會明列。
未追蹤的巢狀 Git repo 會單獨列為缺口，其他檔案仍可完成審查。
單檔送審內容上限為 512 KiB；身分掃描另有每檔 64 MiB、總計 512 MiB 的上限。
無法在上限內證明未變更的來源仍會明列缺口，不以檔案時間戳猜測。
目前不重現全域 config include、外部 attributes 或 ignore 規則；偵測到這些來源時，
會明確中止並說明範圍無法確認，不會把被忽略的檔案偷偷加入審查。
Git LFS 或 filter 已轉換的工作區內容，若無法與 HEAD/index 核對為未變更，
會列為未知轉換缺口，不執行外部轉換程式。
全域 Git 設定支援 dotfiles 常見的檔案或父目錄 symlink，讀取仍有大小與時間上限，
並核對連結與目標是否改變；這項支援不放寬 repo 來源的 no-follow 防線。
<a id="allow"></a>

## 授權本機工具目錄

```text
/allow list
/allow add /absolute/path/to/toolchain/bin
```

`/allow` 等同 list，只讀設定與目前 MCP 的快照，不送模型或建立設定檔。add 一次接受
一個絕對目錄，含空白可加引號；驗證後只更新 `extra_allowed_command_dirs`，同 session
後續命令立即採用。忙碌、核准、審查與 readonly 期間只能列清單。

每次都重驗目錄和工具，拒絕 symlink、無效權限、空目錄及重名；失效不能沿用舊清單。
模型用裸名稱呼叫，既有核准、參數 sandbox、timeout 與 readonly 防線仍保留。
容器不掛入本機工具目錄，也不退回 host 執行。詳細信任邊界見
[安全邊界](../developer.md#security)。
<a id="attachments"></a>

## 夾帶附件

附件有兩種情況：檔案已經在專案目錄內，或檔案還在專案外。

### 檔案在專案目錄內

把檔案放在 sandbox root(= 你執行 `aicode` 的那個目錄)底下，例如 `logs/build_fail.txt`、`screenshots/error.png`、`firmware/boot.bin`。然後在對話裡明確要求使用工具：

```text
請用工具 read_file 讀 logs/build_fail.txt，找出最重要的錯誤訊息。
```

```text
請用工具 analyze_file 分析 screenshots/error.png，辨識畫面上的錯誤文字。
```

```text
請用工具 analyze_file 分析 firmware/boot.bin，整理檔頭、magic 和可讀字串。
請用工具 analyze_file 分析 build/app.elf，view 設 "symbols"、target 設 "uart"，列出所有 UART 相關的函式與位址。
請用工具 analyze_file 分析 build/app.elf，view 設 "disasm"、target 設 "Reset_Handler"，解釋啟動序列。
```

`read_file(...)` 適合文字；`analyze_file(...)` 適合圖片、PDF（一次性抽文字）、ELF、firmware binary。這些操作只把附件帶進目前對話，不會建立可長期查詢的知識庫。想讓圖片或附件之後反覆查，改用下節的 `ingest_document(...)`（圖片會自動走 VL 看圖再進 RAG）。

### 檔案在專案目錄外

預設不能直接讀 `$HOME`、`Downloads` 或其他專案外路徑。要匯入外部附件，在
`~/.config/codetrail/client.json` 打開匯入功能：

```json
{ "external_import": true }
```

`external_import` 是總開關。預設可匯入來源是 `~/Downloads` 和 `/tmp`。如果附件在其他
目錄，用 `external_import_roots` 指定白名單；一旦設定就會取代預設清單：

```json
{ "external_import": true,
  "external_import_roots": ["~/Downloads", "/tmp", "~/specs"] }
```

開了之後**每一次**匯入仍然要人工核准（核准框顯示來源與目的路徑）。

進入 TUI 後請模型先匯入，再分析回傳的新路徑：

```text
請用工具 import_external_file 匯入 ~/Downloads/error.log，
再用 read_file 讀回傳的新路徑，整理最重要的錯誤。
```

匯入後的檔案會複製到專案底下 `.aicode_uploads/`，原始檔不會被修改。更多副檔名、白名單與圖片/binary 細節見 [RAG、附件與知識庫操作](#rag)。

如果外部 PDF / spec / 截圖圖片也要注入 RAG，先 `import_external_file`，再把回傳的 `.aicode_uploads/...` 路徑交給 `ingest_document`（圖片會自動走 VL）；完整串接範例見 [RAG、附件與知識庫操作](#attachments)。
<a id="rag"></a>

## 知識入庫與查詢

`ingest_document` 把來源切成 chunks、計算向量，寫入專案根目錄的 `knowledge.json`。
`reload_knowledge_base` 只更新 MCP 記憶體索引並回報 chunk 數；後續查詢也會自動偵測變更。
全文不會因此塞進聊天 context，只有查詢命中的少量 REF 會進入當次對話。

```text
請依序 ingest_document docs/npu_spec.pdf、docs/api_reference.md、docs/faq.txt，
完成後 reload_knowledge_base，回報 chunks。
請用 query_knowledge 查 conv2d 輸入大小限制，每個數字附 REF。
```

| 格式 | 處理方式 |
|---|---|
| PDF／Markdown／text | 抽文字切段；PDF 圖面另走有來源與驗證狀態的結構化抽取 |
| PNG／JPG／JPEG／GIF／WebP | VL 分析後入庫；聊天截圖明示 `mode="chat"` |
| BIN／DAT／RAW／FW／IMG／ROM／HEX | hex、字串與 magic；遇 ELF magic 改用 ELF 解析 |
| ELF／SO／O／AXF／OUT／KO | symbols、memmap、relocation、DWARF、strings 等多視角報告 |

圖片直接使用 `ingest_document("docs/block_diagram.png")`；不必先 `analyze_file`，兩者各自
讀原圖，前者才保存知識庫。外部圖片先 `import_external_file`，再對回傳路徑 ingest。
圖片 REF 的 `origin: VL` 提醒內容是視覺辨識結果；與文字來源矛盾時應列出兩邊證據，不能猜一邊。

檔名會參與來源排序，`spec`／`datasheet`、`api`／`reference`、`manual` 等應反映文件真實用途。
文件身分是 basename，同名不同路徑會產生衝突，詳見 [KB 維護](#knowledge-maintenance)。

### 嚴格查詢與限定來源

```text
請用 query_knowledge_strict 查 reset assert 最小持續時間，證據不足就拒答。
```

strict 會檢查證據與回答的 REF 支持；明示 strict 後不因中英文題型分類退回一般模式。
未確認 OCR 出現在 `excluded_text`，未驗證圖面出現在 `excluded_figures`，不能拿來回答數值。
多份相似 spec 可用 `source="npu_core_rev_b.md"` 精確限定 basename，篩選在 dense／BM25
各自取候選前生效。一般版與嚴格版都支援。

結果 `status: partial` 不能當完整入庫或完整證據；先讀 `next:`、缺席、品質與待辦資訊。
`chunks=0` 也不算成功取得內容，应檢查來源、VL 狀態與品質排除原因。
<a id="pdf-review"></a>

## PDF 圖面抽取與人工覆核

北極星是 **verified-or-abstain**:程式能以獨立證據確認,內容才進可信檢索;不能確認就保留
原圖、頁碼、框與格/行位置,正文放 `▯` 並記原因,或該份 PDF 零寫入。raster 上被遮住或低於
解析度的字元,沒有任何程式能還原真值 —— 能保證的只有「正確,或誠實拒絕」。所以
**「查得到但標了待覆核」是正常狀態,不是 bug**。

### 涵蓋範圍

原生解析與 VL 的結果各自保留來源與失敗階段。VL server 已啟動，只代表可以呼叫 VL；
coverage 計的是「已偵測區域」，另外列出缺席與無法讀取的原生通道，不宣稱整份 PDF OCR 完整。
table／terminal 沒抽到內容會回退為 `prose` 逐行轉錄，不以 diagram 摘要替代；空轉錄明確失敗。
只有可靠來源位置能提供覆蓋率的母體，缺少來源 anchor 時記 unknown，兩次模型回答一致也不算來源證明。

程式碼與檔案樹有明確訊號時優先保留符號、縮排及母子關係；沒有可靠原文位置時列為缺席，
不強行轉成表格。章節、圖目錄及表目錄只排除有頁碼參照等證據的導覽行，不污染 section、caption
或生成的檢索脈絡。原文座標與頁碼保留，混合頁正文和檔案樹仍可檢索。舊 KB 需重新 ingest。

| 情況 | 收哪些 | 拿得到什麼 |
|---|---|---|
| **結構化 lane 收錄** | 原生 markdown 表格、`find_tables` 幾何、框線格、對齊文字帶、向量文字 log，以及夠大的純 raster / picture | raster 先分類成 table / terminal / prose / diagram；再產生 canonical JSON、逐格/逐行證據、`▯`、驗證狀態、strict gate，且可用 `review_figures` 覆核 |
| **判定不是圖面** | 封面、logo、商標、裝飾線條、產品照片、單純的 GUI 圖示 | 零 VL 抽取、不產生 figure chunk；判定紀錄留在 review artifact 與 ingest 的缺席清單，原生正文由文字 lane 處理 |
| **lane 沒收** | 沒有結構性證據的區域、超出上限被丟掉的候選、整頁 abstain 的頁 | **未收為 structured figure**；不代表原生正文缺席。ingest 列出頁碼、bbox、原因及文字通道狀態；需核對來源時用 `analyze_file(path="原本的PDF路徑")` |

掃描版 datasheet 的表格與手機拍的終端機畫面現在會成為 structured figure；因為通常沒有
獨立原生證據，狀態仍多半是 `unverified` / `needs_review`，`query_knowledge_strict` 會擋下，
直到人工對原圖確認。整頁散文走 `prose`（逐行轉錄），`diagram` 也有自動分類與 structured producer。

figure chunk 會帶所在章節與 caption（`Table 3-1 …` / `圖 2-4 …`）。兩者**只當檢索訊號**
（embedding + BM25 + REF 上一行標示），不進 canonical payload——它們來自鄰近的文字層，
不是從圖裡讀出來的。caption 只在「同一頁、同一家族的圖數與 caption 數完全相等」時才配對；
對不上就整頁不掛，掛錯 caption 比沒有 caption 更糟。原生表格被 structured chunk 取代後，
原位置那行 marker 在**查詢期**會被跟回去，把對應的 figure chunk 一併帶進 REF。

### 六種驗證狀態

內容品質另由 `quality_grade` 表示：`usable`（未發現缺陷且有驗證證據）、`formatting_only`
（已證明僅排版差異）、`partial`（部分可用）、`structure_error`（重要結構錯誤）、`unusable`
（無可用內容）、`unknown`（不足以判斷）。`review_state=confirmed` 只來自綁定目前 revision 的
合法原圖確認紀錄，其他為 `unreviewed`。這兩個欄位不改變下表的 strict 查詢信任邊界。

`auto_disposition` 決定後續處理：`accept`、`manual_review`、`repair_required` 或 `excluded`。
已知缺字、conflict 與缺行列為需修復；重要結構錯誤／全不可讀的 figure 不入庫，原生文字及完整
artifacts 保留。部分可用內容保留遮罩與原因，不能猜補缺字。人工確認不能把仍損壞的 payload 洗成可信。
已由可靠來源證明轉錄缺漏的 revision（`transcription_source_incomplete`）會標 `fixable=False`，
須重新 ingest 核對來源；現有 fix 不保留完整來源，任意補字或原樣確認都不能證明已補齊。

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

### 查詢端會怎麼表現

- `query_knowledge_strict`:flagged 的圖片內容在 **code 層**就被擋掉,不進 REF、也不參與門檻
  計算。被擋下的會出現在回傳的 `excluded_figures`(source / page / figure_id / figure_index /
  kind / 狀態 / 原因)與 `review_hint`,而且**四條回傳路徑都有**(KB 未載入、證據太弱拒答、
  不走嚴格模式、正常回答)。所以就算全部候選都被擋,你仍看得到「哪一頁、哪一張圖可用但待覆核」,
  不會變成「查不到」的假象。
- `query_knowledge`:會回未驗證內容,但 REF 與 metadata 都帶狀態、原因與實際的 row/line 範圍;
  因預算截斷時會明說「未完整顯示」,不讓你以為整張 log 都在。

### 圖多的 PDF:先跑 preflight(零寫入)

```text
請用工具 ingest_document 匯入 docs/datasheet.pdf,preflight_only 設 True,
回報候選數、tile 數、VL 呼叫次數、image token 估計,以及有沒有超過上限。
```

它在**任何 VL 呼叫、embedding 與 knowledge.json 寫入之前**算完就結束。超過上限會直接停下並
指出是哪一項;上限在 `config.py` 的 `FIGURE_*` 常數(改它是改 repo)。

> preflight 涵蓋所有結構化候選，包含純 raster 的分類、雙樣本抽取與 image-token 估算。
> `VL 呼叫 最少 / 最多` 是真正的區間：`最少`＝每張 raster 都被分類成 diagram（單樣本）
> 的情形，`最多`＝全部走雙樣本再加重試。實際次數一定落在其中。
>
> 報告若出現「沒有任何 consumer 的 raster 區域」，代表那幾塊圖沒有結構化候選，
> 報告會逐筆列出頁碼與 bbox。正常情況是 0；
> 不是 0 就表示那幾張圖不會進知識庫，要當成待處理項目看。

在終端機的等價寫法(同樣零寫入):

```bash
python3 <CODETRAIL_REPO>/RAG.py docs/datasheet.pdf knowledge.json --preflight
```

### 抽壞的那一張缺席,不是整份零寫入

結構化 lane 的 schema / validator / row width / line contract / `finish_reason` 任一最終不合格
→ **那一張圖不進 KB**(不冒充成功入庫,也沒有自由文字退路),其餘 figure 與全部文字 chunk
照常入庫;stdout 會印一行 `[figure] 失敗 N 張(不進 KB):p31 diagram truncated;…`,失敗的那幾張
仍留在同一份 review artifact 裡,用 `review_figures(action="list")` 看得到(`in_kb: False`)。
VL 的輸出本來就有隨機性,把它綁成文件級的全有全無,等於讓「整份文件進不進得去」看運氣。

**仍然整份 PDF 零寫入**的是「剩下的圖也不能信」那幾種:VL 連不上 / 逾時、預算超限、
capability probe 未過、來源檔中途被換掉,以及候選與結果對不上這類契約破裂——舊 KB 與向量
保持原狀(可能留下 `failed:true` 的 review artifact)。需要 VL 的候選會在動 KB 之前先做
capability probe:端點真的吃 image content part、
接受本專案的 nested `json_schema`、能完成一張極小且不含機敏內容的 canary 並通過外部 validator。
不通過就 fail-loud 指出缺哪一項,不以「OpenAI-compatible」推定品質。

### ingest 結束後怎麼知道要不要動

`ingest_document` 的結果**只在真的有待辦時**才會列出待辦。第一行的 `status:` 就是判準:

| `status:` | 意思 | 你要做什麼 |
|---|---|---|
| `ok` | 入庫完成，沒有可行動的品質待辦；不保證完整 OCR | 依 REF 的驗證與品質資訊查詢 |
| `partial`（正式 ingest） | 入庫完成，但有需修復、待判斷或缺席的內容 | 照結果裡 `[CODETRAIL_ACTION_REQUIRED]` 那一段做 |
| `partial`（preflight 超限） | **零寫入**:一個位元組都沒進 KB,只是估算超出上限 | 照 `[CODETRAIL_ACTION_REQUIRED]` 縮小範圍或調高上限,再**重新**呼叫一次(拿掉 `preflight_only`)。結果會同時帶 `[CODETRAIL_ZERO_WRITE]` |
| `error` | 逾時 / exit≠0 / 輸出不完整 | 依錯誤訊息排除後重跑;**不要**拿這次的結果當入庫成功 |

`[CODETRAIL_ACTION_REQUIRED]` 那一段最多分五類,每類最多列 5 筆(超出會註明還有幾筆),
而且每類都直接給下一步:

- **待覆核**(原圖可讀) → `review_figures(action="list", document_id=...)` 看原因,對照原圖後
  `action="fix"`。
- **需修復／品質排除**（缺字、衝突、缺行、結構錯誤）→ 修復內容或重新 ingest；不能只確認為正確。
- **無法覆核**(payload / 原圖讀不到,例如 review artifact 被清掉) → 就地修不了,
  `remove_document(...)` 後重新 ingest。
- **抽取失敗**(那一張不進 KB) → 接受它缺席(其餘內容已入庫),或 `remove_document(...)` 後重灌。
- **圖面未收錄／原生文字未讀取** → 圖面候選未形成或超出預算，只表示未收為 structured
  figure，不能推論原生正文沒有入庫。文字通道確實失敗才列「原生文字未讀取」，未知則明列
  未知；用 `analyze_file(path="原本的PDF路徑")` 核對來源。`structured_lane_inactive` 表示
  圖面 lane 未執行，依結果原因處理來源路徑後重新 ingest。

「偵測器判定這一塊不是結構化圖面」這類**沒有下一步**的缺席不列進這一段(完整清單在 ingest
自己的輸出裡)。僅 `unverified` / `legacy_unverified`、未發現確定缺陷時，也**不會**自動列為人工待辦。每次入庫都印一句
罐頭提示等於沒有提示:你會學會跳過它,真的有待覆核時也一起跳過。同理,這一段只算**這一次**
的 run——artifacts 裡上一次 run 留下的失敗不會被重報一次。

### 人工覆核:list → 改 → fix

```text
請用工具 review_figures,action 設 "list",列出目前待覆核的圖,說明每一張的原因。
```

回傳每一張的 `document_id`、`figure_id`、`revision`、頁碼與 bbox、kind、
`extraction_status` / `verification_status`、`quality_grade` / `review_state` / `auto_disposition`、
`quality_issues`、`reasons` / `reason_details`、原圖(crop)路徑與
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

原圖、payload、revision 與送模影像可能含 NDA，保存與刪除方式見 [review artifacts](../developer.md#review-artifacts)。
<a id="text-table-review"></a>

## 表格精確查值與 OCR 正文覆核

`query_table` 直接查目前入庫版本的 canonical table，沒有 LLM、embedding、reranker 或向量 top-k。`review_text` 保留 MinerU 原始 OCR 與人工校字，確認記錄綁定文字版本及 PDF／content_list 雜湊。

### 精確表格查值

```python
query_table(document_id="spec.pdf::<document-hash>", figure_id="fig_<16-hex>",
            register="CTRL", address="0x4000", column="Reset")
query_table(figure_id="fig_<16-hex>", row=3, column="2")
```

至少提供 `row`、`register` 或 `address` 之一；可用 `document_id`、`figure_id` 限定範圍。`row` 是 canonical 的 1-based `row_index`，不含表頭。`column`／`register_column`／`address_column` 接受完整欄名或 1-based 正整數字串；數字字串一律視為序號。未填 `column` 會回傳唯一符合列的全部格子。

`document_id` 接受目前 KB 的完整 ID、來源 basename（如 `registers.pdf`），或 ID 中的精確
相對路徑。先比對完整 ID，別名必須唯一；找不到回 `document_scope_not_found`，歧義回
`document_scope_ambiguous` 與候選 ID，不會自動改查全庫。另指定 `figure_id` 時取兩者交集。
歷史 artifacts 不參與別名解析。結果的 `scope` 同時出現在文字與 structured payload，記錄
原始 document／figure、解析結果與 `all_eligible_tables`；重試仍須保留使用者指定的範圍。

register 名稱逐字、區分大小寫比對，不做模糊搜尋。地址僅正規化十進位數字或 `0x` 十六進位整數，因此 `0x004000` 可匹配 `16384`；不推導 base + offset、不解析範圍或運算式。原始值不改寫。

未指定 selector 欄時，只接受唯一完整表頭：register 欄為 `Register`、`Register Name`、`Name`、`寄存器`、`暫存器`、`名稱`；地址欄為 `Address`、`Register Address`、`Addr`、`地址`、`位址`。此表頭辨識忽略頭尾空白及大小寫。無法唯一辨識時須明示 selector 欄，不能在任意數字格猜地址。

僅採用 verification 可信、品質為 accept、canonical payload revision 與 KB 完全一致的表格。多列符合或重名欄回 `ambiguous`；inherited／merged 格不代填，unreadable／conflict／空格不當作有效值。缺少目前 revision 的 artifact 不回退到歷史 payload。

回傳結構：

```json
{"has_ref":true,"status":"ok","reason":"...","ambiguous":false,
 "matches":[{"value":"0x0001","row_index":3,"column_id":"c2","column_label":"Reset",
             "cell_state":"observed","inherited_from_row":null,"source":"spec.pdf",
             "document_id":"...","page":7,"bbox":[1,2,3,4],"figure_id":"fig_...",
             "revision":2,"evidence_ref":".codetrail/figures/.../manifest.json"}],
 "excluded":[]}
```

`status` 為 `ok`、`not_found`、`ambiguous`、`unverified` 或 `error`。有歧義或選中的格不可信時 `matches` 為空；`excluded` 只含定位與原因，不帶未驗證數值。`ok` 僅表示在所選、可信、目前入庫的表中唯一符合；排除項目仍會列出。

### 正文校字與確認

```python
review_text(action="list", source="spec.pdf")
review_text(action="show", source="spec.pdf", text_id="text_<24-hex>")
review_text(action="correct", text_id="text_<24-hex>", expected_revision=1,
            expected_sha256="<show 中完整 text_content_sha256>", text="校正後的逐字正文")
review_text(action="confirm", text_id="text_<24-hex>", expected_revision=2,
            expected_sha256="<correct 後完整 text_content_sha256>", confirm_against_source=True)
review_text(action="revoke", text_id="text_<24-hex>", expected_revision=3,
            expected_sha256="<目前完整 text_content_sha256>")
```

每個 unit 是 MinerU 抽取後保留的段落／切片，具有 source、page、block 座標及原文 char span，`text_id` 綁定該位置。`show` 列出原始 OCR、目前正文、校字 overlay、bbox、來源路徑、版本、雜湊與覆核記錄。`text` 只替換正文，不包含系統加入的 `[HEADING]` 前綴；原始 OCR 永遠保留。

`correct`、`confirm`、`revoke` 都要求目前 `expected_revision` 與完整 `expected_sha256`，每次成功都增加版本。校字不等於確認；確認需要人對照來源 PDF 後明示 `confirm_against_source=True`。寫入經 MCP 互動核准及唯讀保護；list/show 的核心讀取不建立目錄、鎖檔或模型快取，也不連模型。

內容雜湊、來源 PDF、content_list、位置、rendered prefix 或品質紀錄有變化，原確認立即失效。已知缺行、截斷或來源不完整不能靠按 confirm 洗成可信；必須修復來源並重新 ingest。不可讀字元可先校字，再獨立確認。沒有新 provenance 的舊 OCR 可供檢視，須重新 ingest 後才能覆核。

寫入會在鎖外準備必要的 chunk／gate／section 向量，核對實際 embedding 模型身分，再於 KB exclusive lock 內比對整份快照和 revision。競態會回 `conflict`，不覆蓋他人更新。JSON／NPZ 仍使用現有原子 store，校字不沿用舊文字的向量。

### 重灌、續跑與嚴格查詢

同一來源 re-ingest／resume／局部 redo 僅在 PDF SHA256、content_list SHA256、穩定段落位置及原始 OCR SHA256 全部一致時，沿用目前 KB 的校字 overlay 與確認。checkpoint 的舊覆核不是權威來源；提交前還會比對 ingest 開始時的文字 revision 基線，拒絕覆蓋稍後的校字、確認或撤銷。同一 `text_id` 的來源變更會增加版本並列待覆核。

嚴格查詢共用同一 eligibility 判斷：正文內容、確認版本、目前來源及品質都有效才可入選；section 展開中的每個成員仍各自通過，未確認鄰段不會隨已確認段落合併入選。OCR 段落保留獨立單位，標題前綴只影響召回，不提升 gate 分數。回傳 REF 與 `excluded_text` 帶 `text_id`、`text_revision`、`text_content_sha256`，可直接定位覆核。

此功能只覆核已有 MinerU 正文，沒有宣稱整份 PDF 已完成 OCR。缺頁及 figure 品質仍由原本 ingest／figure 報告列出。校字上限 128 KiB；KB metadata 讀取上限 128 MiB，來源 hash 上限 1 GiB。路徑須在專案根內；根目錄與檔案須由目前使用者擁有，metadata／來源仍拒絕 symlink、hardlink 與非普通檔。既有專案 0775、來源／knowledge.json／store lock 0664 是合法模式，唯讀工具不修改其權限。新建 store lock 與原子發布的 knowledge.json 為 0600；其他 CodeTrail 私有 state 仍依各自的 0600／0700 防線，不因來源讀取相容性而放寬。寫入依賴 POSIX flock／dir-fd／nofollow 與 Linux `/proc/self/fd`，安全能力不足直接拒絕。
<a id="ingest-resume"></a>

## 文件入庫續跑與局部重做

文件入庫預設 `resume=True`。再次執行相同輸入，會重新驗證來源、parser／抽取設定與使用到的實際模型身分，再復用已原子完成的頁面、圖表與 embedding。文件提交仍會建立完整 `ExtractedDocument`，由原有 KB lock 與 JSON／NPZ 原子交易發布。

```sh
python3 <CODETRAIL_REPO>/RAG.py spec.pdf knowledge.json
python3 <CODETRAIL_REPO>/RAG.py spec.pdf knowledge.json --redo-pages 2,5-7
python3 <CODETRAIL_REPO>/RAG.py spec.pdf knowledge.json --redo-figures fig_0123456789abcdef
python3 <CODETRAIL_REPO>/RAG.py spec.pdf knowledge.json --retry-failed
python3 <CODETRAIL_REPO>/RAG.py spec.pdf knowledge.json --no-resume
python3 <CODETRAIL_REPO>/RAG.py ingest-status spec.pdf --root /path/to/project
```

`ingest-status` 只讀本機進度，不建立狀態、不呼叫模型。輸入路徑與 `--root` 必須指向同一專案；普通 CLI 的 checkpoint 根目錄是已設定 sandbox，或輸出 KB 的父目錄。原圖、正文、canonical payload 及向量都可能含 NDA，狀態與快取必須留在本機。

CLI 明確指定根外文件時保留原生抽取流程，並顯示「不支援 checkpoint/resume」；不啟用根外 structured figure，不接受根外 selective redo 或 MinerU。MCP 仍先依專案 sandbox 拒絕外部來源。根內來源 symlink 會在作業開始固定到 canonical 檔案讀取，保留原 basename，提交前另外檢查原 alias 沒有被重新指向。

`add_document()` 接受 `resume=True, redo_pages=None, redo_figures=None, retry_failed=False`；`rebuild` CLI 使用相同旗標。直接圖片／聊天截圖入庫與 `--image -y`／`--chat -y` 支援 `resume`／`--no-resume` 的文件級復用；互動式預覽與 URL 匯入維持原有流程。頁／圖選擇器僅適用 PDF，三種選擇器互斥，不能搭配 `fresh`、`preflight_only` 或 `resume=False`。選中的頁碼為從 1 起算的實際 PDF 頁碼，圖 ID 必須來自既有 checkpoint／figure 清單。

局部重做需要同來源、同抽取設定的既有 checkpoint。未知頁／圖 ID 在修改 checkpoint 前拒絕；來源或設定已變時，必須先執行一次完整入庫。實際 VL／embedding／context 模型改變會使相應快取失效；局部重做遇到模型漂移會拒絕，避免保留另一個模型世代的單元。無法取得可靠 live model identity 時 fail-loud。Split 部署使用核對過的版本 alias，是 A 端對權重版本的聲明，不冒稱 B 已讀過 A 的權重檔。

原生文字逐頁保存，structured figures 逐候選保存；成功的 figure 保存 canonical payload 與實際送模 variant bytes/hash，沒有依賴舊 `.codetrail/figures/` run 的路徑。原生幾何與候選計畫仍從來源重建，覆核圖片可重新 render。跨頁表格修復與頁內 figure 編號對全集重算；局部重做相同影像的任一 occurrence，會一併重做引用同一份模型輸入的影像群，避免 duplicate 指向不同世代的圖。

已完成但 `needs_review`／`unverified` 的抽取結果可復用，復用不提高 verification。品質抽取失敗保留在清單中，可用 `--retry-failed` 重做；transport failure 或中斷時尚未原子完成的單元會於續跑重新執行。空白／不可取得原生文字的頁、排除與抽取失敗分開列帳，不能由完成頁數推論全 PDF 已 OCR 或已核實。完成通知仍列既有的修復、缺席及待覆核項目；checkpoint 報告補上完整單元清單與成功／復用／重做數。終端顯示最多 40 個單元問題，完整狀態可由 `ingest-status` 取得。

checkpoint 存於 `<project>/.codetrail/ingest/doc-<來源相對路徑hash>/`，私有目錄 0700、檔案 0600；共用的 `.codetrail/` 可沿用同 owner 的 0775 目錄，但仍拒絕 world-write。所有私有讀寫沿 dir-fd 與 `O_NOFOLLOW`，拒絕 hardlink、錯誤 owner／權限與損壞內容。`runner.lock` 的 flock 覆蓋抽取、embedding 到 KB 提交，同文件只能有一個 runner。

成功單元依序 fsync 產物、追加並 fsync 單元 journal，再原子發布小型 committed head；每筆 embedding 不重寫全份清單。`state.json` 在模型身分、候選計畫與文件階段邊界保存完整快照和 journal watermark。續跑及唯讀 `ingest-status` 從快照重播已發布增量，核對 checkpoint 身分、序號、offset 與 SHA-256 鏈；舊版 v1 快照可保留完成單元並升級到 v2，舊版 writer 會拒絕新格式。

head 之外的未發布尾端不能復用，只有取得 runner lock 的續跑會截去該尾端；status 不截斷、不建檔、不探測模型。head 發布失敗會回滾，連回滾也失敗則明示拒絕恢復，不能猜測成功。已提交 journal／head 缺失或損壞會拒絕，不退回舊快照重新抽取；程序中斷留下 `running` 不會被當成功。狀態與單一產物有 128 MiB 上限，journal 有 512 MiB 上限，來源 hash 有 1 GiB 上限，超過會明示拒絕。

figure retention 不清這個目錄，`fresh` 也不刪它。未引用的舊產物目前不自動回收；需要釋放空間時，在無入庫作業執行的情況下刪除對應 checkpoint 目錄，後續入庫會重新抽取。不要只刪 state 指向的單元檔：下次讀取會以損壞 checkpoint 拒絕，不會偷偷重跑掩蓋資料缺損。

人工 figure／OCR 版本基線取自本次入庫開始時的現行 KB，不取自舊 checkpoint。提交前在 KB exclusive lock 內重驗；入庫期間的新校字、確認或撤銷會形成 conflict，舊 checkpoint 不得覆蓋較新覆核。OCR overlay 由正文覆核功能依來源、artifact、paragraph 與原始文字 hash 沿用，checkpoint 保留原始抽取與本次 revision provenance。
<a id="knowledge-maintenance"></a>

## 知識庫更新、備份與清除

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
`python3 <CODETRAIL_REPO>/RAG.py <file> knowledge.json --fresh`（`rebuild` 子命令也吃 `--fresh`，
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

### 只有 `knowledge.json` 要管

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

### 三件容易踩的事

1. **知識庫綁專案目錄**：`knowledge.json` 存在當前專案根目錄裡，換到另一個專案就要重新匯入。同一份規格書在多個專案要用就匯入多次。
2. **同檔名的兩份文件會被擋下**：KB 裡的文件身分是 **basename**，所以 `a/spec.pdf` 和 `b/spec.pdf` 在裡面是同一份。灌第二份時會**直接失敗並列出兩邊的完整路徑，零寫入**，不會把前一份靜默換掉。三種處理方式：確定是同一份文件搬過位置 → 先 `remove_document("spec.pdf")` 再灌；兩份都要留 → 先改成唯一檔名（例如 `npu_a_spec.pdf` / `npu_b_spec.pdf`）；要用這一份重建整個 KB → `ingest_document(..., fresh=True)`。同一個檔改過內容再灌一次是正常的更新，不受影響。
    比對用的是解析過 symlink 的絕對路徑，記在 `knowledge.json` 的 `metadata.document_sources`。**舊 KB 沒有這份紀錄**，所以升級後第一次撞名只會警告並採用新的那份，第二次起才擋得住。PDF review artifacts 用的是含路徑與 hash 的 `document_id`，本來就不會互相覆蓋。
3. **不要 commit**：`knowledge.json` 切碎了原始文件內容，NDA 場景幾乎一定包含敏感片段。已經在 [安全邊界與工作節奏](../developer.md#security) 的「不要 commit 的資料」列入不該 commit 的清單，建議在專案的 `.gitignore` 也加一行。
4. **越具體越好**：把一整份 500 頁的手冊原封不動塞進去，不如先抽出實際會問到的章節整理成 markdown 再匯入。雜訊少，答案準。

刪除 `knowledge.json` 只清掉目前 KB。來源檔、`.aicode_uploads/`、
`.codetrail/figures/`、`.codetrail/ingest/`、session 與 data flywheel 快照另有保存位置；
要清除 NDA 資料需逐處核對，詳見 [資料保存](../developer.md#review-artifacts)。

Chunk 脈絡生成預設關閉，只在明示 CLI 生成；生成文字只能影響召回，不能當證據。
設定、費用與遠端獨立同意見 [Contextual Retrieval](../developer.md#contextual-retrieval)。
<a id="lessons"></a>

## 行為 lessons

你在 TUI 裡糾正過模型的做事方式(「migration 前要先確認 backward compatibility」「不要每次都重跑整套測試」),下一個 session 它又忘了 —— lessons 機制把這類糾正變成**經你核准**的行為規則,每個 session 自動注入,直到過期複審。

跟知識庫嚴格分離:

| | knowledge.json(RAG) | lessons.json |
|---|---|---|
| 內容 | 客觀知識(spec / datasheet / 手冊) | 主觀行為教訓(你糾正過的做事方式) |
| 寫入 | `ingest_document` | `record_lesson` 提案 + 你核准(permission `ask`) |
| 取用 | 檢索(embedding + reranker) | 全量注入(上限 20 條,不做 embedding 檢索) |
| 位置 | `<SANDBOX_ROOT>/knowledge.json` | `~/.config/codetrail/lessons.json`(per-deployment,不跨部署共享) |


---

### 生命週期

```
你糾正模型行為
  → 模型呼叫 record_lesson 提案(rule 必須是單行祈使句行為規則)
  → permission ask:你在核准框看到 rule,核准才寫入 lessons.json
  → 下個 session:aicode 啟動時把 active lessons render 進
    <SANDBOX_ROOT>/.codetrail/lessons.md,客戶端組 system prompt 時
    連同專案 AGENTS.md 一起載入
  → 90 天後過 review_by:該條停止注入,aicode 啟動時醒目列出待複審清單
  → 你複審:renew(再延 90 天)或 delete
```

每條 lesson 的欄位:

```json
{
  "id": "L-001",
  "rule": "migration 前先確認 backward compatibility",
  "scope": "project",
  "project": "/path/to/that/project",
  "created": "2026-08-11",
  "review_by": "2026-11-09",
  "hit_count": 0,
  "last_triggered": null
}
```

- `scope: "project"`(預設)只注入到記錄它的那個專案;`"global"` 注入到此部署的所有專案。跨專案皆適用的工作習慣才用 global。
- `rule` 必須是可執行的祈使句行為規則,單行、≤200 字元。機械檢查只擋得住明顯的 log 形式(Traceback、timestamp、`[ERROR]`、`exit code N`);「上次 migration 壞了」這類事件敘述句它擋不住 —— 真正的品質把關是你的核准框,內容不對就拒絕,讓模型改寫再提。
- 日期(`created` / `review_by`)取本機時區的「今天」;id 單調遞增,刪掉最高編號的條目後新 lesson 也不會重用該編號(舊對話裡的 `[L-xxx]` 引用不會指到別條)。

### 什麼會觸發、什麼不會

模型只該在「**你糾正它的做事方式**」之後提案。以下都不是觸發條件（tool 說明與本文件都有明訂；不要為此把整段規則再複製進全域 `AGENTS.md`）：

- 工具執行失敗 / exception / lint 錯 —— 那是環境或程式問題;
- 答案內容錯誤被指正 —— 客觀知識修正請 `ingest_document` 進 KB;
- 模型自己覺得「這樣做比較好」—— 沒有人的糾正就不記。

被你拒絕的提案就結束,模型不該換句話重試;內容完全相同的重複提案也不會寫入第二條(回報既有編號,重試因此安全)。**沒有任何無審核的自動寫入路徑。**

核准閘不再依賴任何外部設定:`record_lesson` 寫死在 `client_policy.ASK_TOOLS` 裡,所以
「這個工具要不要人工核准」不會因為某份設定檔沒更新而靜默失效。`.codetrail/lessons.md`
也由客戶端自己讀進 system prompt,不需要註冊 `instructions` 項。

### 上限與 fail-loud

可注入的 active lessons 上限 **20 條**(每個專案看到的 global + 該專案 project 條目合計)。滿了之後 `record_lesson` 與 `renew` 都會拒絕並要求人工整併 —— 不會靜默丟掉舊的、也不會只注入前 20 條。條數就 20,所以全量注入、不做 embedding 檢索與自動 decay / 衝突解決。

session start(`aicode`)時:

- lessons store 損壞 → **拒絕啟動**(fail-loud);修復或移除 `~/.config/codetrail/lessons.json`。沒有緊急跳過的旗標 —— 帶著壞 store 啟動會讓你以為 lessons 有生效;
- active 超過 20(只可能手改 JSON 造成)→ 拒絕啟動,要求整併;
- 有條目過 review_by → 照常啟動,但該條停止注入,並醒目列出待複審清單與 renew / delete 指令;
- `.codetrail` 被 symlink/junction 指到專案外 → 拒絕啟動(沙箱寫入防線,render 一個 byte 都不寫;不信任的 repo 可能用這招把檔案導出沙箱)。

跳過與安全模式(兩者都會**移除**先前 render 的 `.codetrail/lessons.md`,避免上個 session 的規則殘留又被載入):

- 內部入口(`session_eval` 的 replay / eval)以呼叫端參數要求不注入:同一份 suite 在兩台機器上必須看到同一份指示;
- `client.json` 的 `"project_instructions": false`(分析不信任 repo 的安全模式,見 [開發與維運](../developer.md#security)):客戶端這個模式完全不讀專案內的 `AGENTS.md` 與 `.codetrail/lessons.md`,lessons **不會注入** —— `aicode` 會明講,不會謊報「已注入」。

### 管理指令

在 CodeTrail checkout 目錄執行:

```bash
python3 <CODETRAIL_REPO>/lessons.py list              # 全部條目(含 EXPIRED 標記、hit_count)
python3 <CODETRAIL_REPO>/lessons.py renew L-001      # 複審通過:review_by = 今天 + 90 天(--days 可調)
python3 <CODETRAIL_REPO>/lessons.py delete L-001     # 淘汰
python3 <CODETRAIL_REPO>/lessons.py hit L-001        # 人工記一次命中(見下)
```

進階:`--file` 可指定 store 路徑(預設 `~/.config/codetrail/lessons.json`)。

### hit_count 的誠實說明

注入的 lessons.md 會要求模型:套用某條規則時在回覆中標註 `[L-003]` 這樣的編號,讓你**看得到規則有沒有生效**。但那個標註在對話輸出裡,MCP server 這一端看不到,所以 `hit_count` 不會自動累計 —— 欄位保留給人工判斷:在對話裡看到模型標註了某條,想留下紀錄就 `python3 <CODETRAIL_REPO>/lessons.py hit L-003`。複審時 `hit_count` / `last_triggered` 是「這條還有沒有用」的參考,不是自動 decay 的依據(本機制刻意不做自動 decay)。

### 驗證注入有生效

1. TUI 接管前的終端應有一行 `[lessons] N 條 active lessons 已注入 .codetrail/lessons.md`；待複審警告保留在 `/status`。
2. 開新 session 問模型:「目前 context 裡有哪些 CodeTrail lessons?」它應能列出編號與內容。
3. 改用 `cat <SANDBOX_ROOT>/.codetrail/lessons.md` 直接看注入內容(此檔自動產生,勿手改;`.codetrail/` 已在 .gitignore)。

注意:寫入當下的 session 其 context 已載入完成,新 lesson 於**下一個** session 才注入(tool 回覆會提醒模型本 session 先直接遵守)。
<a id="build-context"></a>

## 使用實際 build target 分析程式

CodeTrail 可匯入既有編譯資料，再讓 `code_rag_search` 的 semantic、context、neighbors、path 使用同一個 `build_target`。匯入器只讀資料，不執行 compiler、shell、response file 或 build 指令。

在專案根目錄內準備 `compile_commands.json` 或 verbose build log：

```bash
python3 /path/to/CodeTrail/scripts/import_build_context.py \
  --root /path/to/firmware --target board-debug --variant debug \
  --compile-commands compile_commands.json

python3 /path/to/CodeTrail/scripts/import_build_context.py \
  --root /path/to/firmware --target board-release --build-log build/verbose.log

python3 /path/to/CodeTrail/scripts/import_build_context.py \
  --root /path/to/firmware --show

python3 /path/to/CodeTrail/code_graph.py \
  --root /path/to/firmware --build-target board-debug
```

匯入成功表示保存了輸入與未知項目，並不表示重建結果完全確定。最後一個命令是 CodeTrail 的靜態關聯圖建置，不會編譯 firmware；各 target 的圖需要各建一次，後續會依內容身份更新。

MCP 查詢使用 `code_rag_search(query="初始化流程", build_target="board-debug", mode="context")`。其餘三個 mode 使用同一個 `build_target` 欄位。省略 target 保留原本全專案搜尋，並顯示 target unknown。明示未匯入的 target 直接報錯，避免意外回答另一份編譯設定。

### Target、variant 與來源

`--target` 是使用者明確命名的 profile，不能從 object 路徑推測。可將不同 target 的 compilation database 分開匯入；若資料列含擴充 `target` / `variant` 欄位，則依明示的 target / `--variant` 選取。只有 `--variant` 標籤而輸入沒有 variant 欄位時，標籤不會自行替資料消歧。

同一 source 有多筆 entry 時會保留歧義，即使它們被放進同一個 profile。使用重複的 `--entry-output` 明確選取資料列的原始 `output` 或 `-o` 值；比對為逐字相等，不是 glob。不會自動挑第一筆或假設某個 object 目錄就是 target。

`arguments` 優先於 `command`，command 只解析引用規則。保留 directory、source、output、compiler、原始與展開參數、資料列或 log 行號。支援 GNU GCC/Clang 的 ARM/AArch64 driver 名稱以及 MetaWare `ccac` 等名稱；工具供應商與安裝位置不代表硬體 ISA，匯入器不推測板子的架構或記憶體位置。

Log 接受逐行 compiler driver invocation、Ninja `[n/m]` 前綴、make 的 entering/leaving directory，以及字面的 `cd directory && compiler ...`。其他 shell expansion、複合指令與未辨識 log 行均列 unknown；不宣稱任意內部 compiler stage 或平行交錯目錄 log 都能重建。

### 巨集與包含順序

保存並依序處理 `-D` / `-U`，包含 response file 展開後的位置；`-I`、`-iquote`、`-isystem`、`-idirafter` 保留各群組的輸入順序並按 compiler 的搜尋群組順序解析。quoted include 先查包含它的檔案目錄。相同目錄同時出現在 `-I` 與 system 群組時，保留 system 身分。

`-imacros` 先取得巨集且丟棄一般程式碼，之後依序處理 `-include`；MetaWare `-Hinclude=file` 也支援。response file 的 `@path` 相對於 compilation directory，僅解析參數；MetaWare 第一欄 `!` 註解可被辨識。檔案與遞迴深度都有上限。未知旗標不會被默認忽略；常見 CPU/endian/optimization 選項保留原文，builtin 巨集仍需要輸入證據。

可用 `--builtin-macros path/to/builtin-macros.txt` 提供已取得的完整 `#define` dump，必須對應同一 compiler 與 CPU/語言旗標，且是在這次顯式 `-D`/`-U`、forced include 與 source 之前的初始巨集集合。CodeTrail 不呼叫 compiler 取得它。空檔、缺 dump、缺少動態 builtin 或無法解析的表達式都保留 unknown；不從 `gcc` / `ccac` 的檔名猜 builtin 值。

條件使用 active / inactive / unknown。支援巢狀 if/elif/else、object 巨集、defined、常見有號整數及邏輯運算，並合併未知分支的 define/undef 與 include 副作用。Function macro 展開、unsigned promotion、未支援的 pragma、raw string / trigraph 等保守列 unknown。只有可證明 inactive 的行會排除；unknown 符號可供查閱，但不是已確認編譯或呼叫關係。path 只走確認的邊。compiler forced include 的 graph evidence_line 為 0，resolution_basis 為 `compiler_forced_include`，來源是保存的 compilation entry。

### Generated headers 與失效

Manifest 固定為專案內 `.codetrail/build-context.json`，目錄 0700、檔案 0600，採原子替換與匯入鎖。所有輸入必須在選定 project root 內；逐層 dir-fd / nofollow 讀取會拒絕 symlink、hard link、非一般檔案與根外路徑。

匯入時解析到的 generated headers、response files 保存精確相對路徑與 SHA-256；`--generated-header` 可另列精確檔案。只有實際參與所選 target 的 header 才會進索引。`build/` 仍是全域 hard ignore，索引只額外取 manifest 准入的指定 header，沒有開放整個目錄或改變 grep/list_dir 的範圍。

資料庫、log、response、builtin dump、generated header 改變或消失時必須重新匯入；正常 source/header 每次載入重新判定。指紋包含 target、variant、include 順序、所有正面及缺席依賴的內容身份，連較高優先路徑後來新增同名 header 也會失效。索引與圖以 target 分開保存，查詢前後重驗依賴；查詢中途資料改變會報錯，不沿用舊 target 的快取。

主要格式依據：[Compilation database specification](https://clang.llvm.org/docs/JSONCompilationDatabase.html)、[GNU preprocessing options](https://gcc.gnu.org/onlinedocs/gcc/Preprocessor-Options.html)、[Synopsys MWDT/GCC option matrix](https://foss-for-synopsys-dwc-arc-processors.github.io/toolchain/gcc/option-matrix.html)。MetaWare argument file、`-D/-U/-I` 與 `-Hinclude` 同時依本機已授權的 Programmer’s Guide 查核；合約測試僅使用合成資料。
<a id="memory-consistency"></a>

## ELF、linker、preload 與記憶體配置核對

在 `aicode` 對話指定檔案，請模型呼叫：

```python
analyze_file(
    path="build/firmware.elf", view="consistency",
    linker_map="build/firmware.map", linker_script="linker.ld",
    preload_log="logs/preload.json", dram_config="board-memory.json",
    map_format="auto",
)
```

所有輸入及 preload 引用的 binary 都必須位於目前 repo；符號連結、硬連結、越界路徑、
讀取中改變的檔案會被拒絕。此操作唯讀，不執行 linker、模擬器或 preload。
也可以只給 ELF；缺少的證據會列為 `unknown`，不需要先猜 DRAM 位址。

架構由 ELF header 判讀。Synopsys 工具與第三方硬體可以搭配使用；工具供應商不代表
板子的記憶體位址。第一版沒有預設硬體配置。

### 第一版格式

| 輸入 | 支援範圍 |
|---|---|
| ELF | pyelftools 解析 32／64 位元 ELF 的 SHF_ALLOC sections、PT_LOAD、NOBITS；報告 e_machine。ET_REL 沒有最終位址，ET_DYN 缺少執行時 load bias 時標未知。 |
| GNU map | `Linker script and memory map` 後的 output sections、換行 section 名稱、明示 load address；不把縮排的 input sections 或 discarded sections 當重複配置。 |
| MetaWare map | `SECTION SUMMARY`／`SECTIONS SUMMARY` 的 section、type、START、END、LENGTH；支援換行名稱。三個數字為十六進位，END 是 inclusive，會核對長度再轉半開區間。 |
| linker script | 常見 `MEMORY` ORIGIN/LENGTH、常數與有界算術、SECTIONS 明示地址／ALIGN／`>region`／`AT(address)`／`AT>region`；MetaWare GROUP 可繼承 region。 |
| preload | GDB `Loading section NAME, size 0xSIZE lma 0xADDRESS`；下述 JSON 記錄；獨立 `--preload ADDRESS FILE` 宣告。宣告本身不證明已執行。 |
| 記憶體配置 | 下述 JSON，或 MEMORY 宣告（視為 LMA region）。 |

格式不辨識、未展開的 INCLUDE／OVERLAY／INITDATA、未知表示式及缺少載入映射會保留
`unknown`。不會執行輸入中的命令，也不宣稱能解讀任意模擬器 log。GNU／MetaWare 的
格式相容性使用合成測資；MetaWare compiler 需要有效授權才能另外產生實際測試產物。

### 明示 preload 記錄

以下是合成示例，位址不能當作實際板級設定。數字可用 JSON 非負整數或 `0x` 字串；
`size` 以 bytes 計，`space` 必須明示。

```json
{
  "schema": 1,
  "records": [
    {"name": ".text", "start": "0x1000", "size": 4, "space": "lma", "operation": "load"},
    {"name": ".bss", "start": "0x1004", "size": 8, "space": "vma", "operation": "zero"}
  ]
}
```

`operation` 是 `load` 或 `zero`。可加 `file`，核對沙箱內原始 binary 的檔案長度；
這不是把 ELF 容器總長度當 PT_LOAD payload。此 JSON 表達提供者的載入／清零紀錄，
不是 CodeTrail 自動取得的硬體執行證明。已知 VMA＝LMA 映射才會跨空間比對。

### 明示可用與保留區域

```json
{
  "schema": 1,
  "complete": false,
  "regions": [
    {"name": "RAM", "start": "0x1000", "size": "0x100", "space": "vma"},
    {"name": "reserved", "start": "0x10f0", "size": "0x10", "space": "vma", "reserved": true}
  ]
}
```

`complete=false` 是預設，表示只提供一部分記憶體配置。完全落在此範圍之外的區間會列未知，
跨出已提供區域的部分會列越界。只有掌握完整配置時才設 `complete=true`；此時未涵蓋的區間
一律視為越界。VMA 和 LMA 的配置分開提供，不能只因數值相同就推定相通。

### 結果解讀

`pass` 表示本次提供且支援的證據相符；`conflict` 列出具體不一致；`unknown` 表示資料
不足或格式未完全解析。每個衝突帶 `[start,end)`、長度、交集／缺口、來源行或 ELF
section／segment index 與 SHA-256。section 包含在 segment 中是正常關係。

檢查包含位址／長度、同層配置重疊、對齊、檔案範圍、filesz≤memsz、map／script 約束、
preload 覆蓋及保留區。NOBITS 和 PT_LOAD 尾端需要清零證據；缺少紀錄會標未知，
不會因 ELF 宣告 `.bss` 就認為執行時已清零。結果過長會保留總數並明示截斷。

格式參考：[GNU LMA](https://sourceware.org/binutils/docs/ld/Output-Section-LMA.html)、
[GNU MEMORY](https://sourceware.org/binutils/docs/ld/MEMORY.html)、
[Synopsys 公開 ARC lab](https://github.com/foss-for-synopsys-dwc-arc-processors/arc_labs/blob/master/doc/documents/labs/level2/lab8.rst)。
