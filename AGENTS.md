這個 repo 是一個 **本地 RAG / Code-RAG / MCP 工具集,以及它自己的聊天客戶端**。
終端使用者透過 `aicode` wrapper 啟動 `codetrail_chat.py`(全螢幕 Textual TUI),
用本地 llama.cpp `llama-server` 跑模型,分析 NDA / 內部 firmware repo。
介面只有這一個:沒有網頁前端、沒有 attach。部署**不需要 Node / npm / opencode-ai**。

如果你是 AI coding agent（Codex / OpenCode 等）正在改這個 repo，請先把這份檔讀完。
維護命令、eval 漂移檢查見 [README_DEV.md](README_DEV.md)——那份檔是**閱讀用參考**，
裡面的測試命令誰能執行由角色決定（見 §1）。

---

## 1. 測試 policy

### 1.1 兩包制

- **smoke** ＝ 標 `@pytest.mark.smoke` 的測試：真實發生過的 bug 的 regression ＋ 無聲失敗風險的契約檢查。
  §2 的每個安全檢查點都必須在裡面（由 `tests/test_smoke_gate.py` 靜態守住）。整包目標 10 秒內。
- **full** ＝ 整個 `tests/`。
- 統一入口 `python3 scripts/run_tests.py`（無參數＝full，最多 16-shard 並行；帶任何 pytest 參數＝單行程逐字轉發）：
  - smoke：`python3 scripts/run_tests.py -m smoke`
  - full：`python3 scripts/run_tests.py`

### 1.2 執行權責

- **開發者**（預設角色）：改碼過程**不執行測試**。只允許兩種執行：
  1. 交付前跑一次 smoke。
  2. 修 bug 時單跑自己新寫的那條 regression test（見 §1.3）。
  除此之外禁止執行測試及任何會間接觸發測試的命令。交付時註明：
  `Tests: smoke only — reviewer owns full execution.`
- **審核者**（僅限使用者在本次 prompt 明示 `ROLE=REVIEWER`）：先完成靜態審核並集中提出問題；
  程式碼收斂後對目前 HEAD 執行一次 full，回報命令、結果與 HEAD。程式碼未變時不得重跑已通過的測試。
- **判定規則**：失敗 node ID 集合不得大於動工前基線
  （缺 tty / `llama-server` 執行檔的環境，環境相依測試的既有失敗屬於基線）。
  基線外任何新失敗＝未完成，不得回報成功。
  `0 tests collected`（pytest exit code 5）不是通過，必須回報異常。

### 1.3 修 bug 鐵則：red-before-green

1. 先寫 regression test，在**未修改**的程式碼上單跑它
   （`python3 scripts/run_tests.py tests/test_x.py::test_y`），貼出紅燈輸出節錄。
2. 再修程式碼，同一條測試轉綠，貼出綠燈節錄。
3. 交付內容＝紅燈證據＋綠燈證據＋diff。缺紅燈證據的 bug fix 一律視為未驗證。

這類 regression test 一律標 `@pytest.mark.smoke`。

### 1.4 什麼時候寫新測試

只有兩種情況：
1. 真實發生過的 bug → regression（走 §1.3）。
2. 無聲失敗風險的契約，含 §2 安全層檢查點的防護測試。

其餘一律不寫：不追 coverage 數字、不為新功能寫儀式性測試、不為 parser 寫 parser。

### 1.5 動到既有測試就要講

改既有測試不必事先請示，但**交付時要逐條列出動到哪些測試檔、測試名與理由**——
包含改斷言、刪測試、放寬容忍值。理由要說得出「行為為什麼該變」，不能是「這樣才會綠」。

`skip` / `xfail` 仍然只用在環境相依（缺 tty、缺 `llama-server`）的情境，不用來繞過失敗。

---

## 2. 安全相關不要砍

- `agent_tools.ToolExecutor._safe_path` — 所有檔案讀寫的 sandbox 入口
- `media._safe_path` — 圖片/ELF/binary 的 sandbox 入口
- `agent_tools._validate_command` — run_command 白名單 + dangerous-pattern 過濾
- `apply_patch` 的「context／SEARCH 必須逐字匹配（S/R 絕不用相似度代套）」、「max files / max lines
  （udiff added+removed；S/R payload budget）」邏輯；`patch_engine` 的 byte-safe 寫入（UTF-8 strict、
  BOM/CRLF 保留、symlink／dir-fd 防線、best-effort rollback）；`patch_verify` 的「驗證層不得 spawn
  subprocess、skipped 不得算 passed」；`run_command` 的 timeout 1..600 三層邊界
- `mcp_server.py` 啟動時 `set_sandbox_root(AICODE_ROOT, allow_external=False)`
- `client_mcp` 的取消契約——SDK 在 read timeout / task 取消時**不送**
  `notifications/cancelled`(契約測試釘住,不是註解),所以客戶端自己配發 request id、
  在 Ctrl-C 與 timeout 兩種情況都送取消、寬限期過就 SIGTERM 該 instance 並把**所有**
  進行中的呼叫回成 error 再重新 spawn;每次呼叫的 read timeout 固定
  (`config.MCP_CALL_TIMEOUT_SECONDS`,呼叫端不得放寬——能調小就等於 ingest 還在寫
  `knowledge.json` 時 client 已經放棄);`tools/list` 的順序在 `start()` 就驗;
  MCP stderr 預設不落檔(它含查詢原文與絕對路徑),記憶體尾端有上限
- `client_store` 的 session 檔——對話逐字含 NDA 內容:必須落在 state 目錄而不是被分析的
  repo(相對 `XDG_STATE_HOME` 與專案內的 state 目錄都要擋)、目錄 0700、檔 0600、
  讀寫兩端都拒 symlink 與 hard link、append 不得建出沒有 header 的檔、header 綁這個
  專案與這個 session
- `client_policy` 的兩個 policy——readonly 的判準是 `tools/list` 的 `readOnlyHint`
  (**只有 JSON true 才算唯讀**;`bool("false")` 是 True),不是寫死名單,所以漏加名單的
  新工具一樣被 deny;互動模式的六個 ask 工具沒核准就不得執行,核准框**完整顯示參數**
  (含整份 patch),重問有上限。readonly session 另有第二層:MCP server 以
  **argv** 的 `--readonly` 起(以前是四個環境變數),寫入 / 執行 / build 命令與
  context metrics 一次全關,而且 `client.json` 把 `build_commands` 開起來也翻不回來
- `client_engine` 的訊息轉換——reasoning 剝除只動 reasoning 欄位、只丟最新一則**真實**
  使用者訊息之前的、認不出那則訊息就整段不動;prune 只改送模型的那一份,session 檔與
  畫面保留原文;懸空的 tool_call 必須補在**宣告它的那則 assistant 之後**(補在尾端會排出
  `assistant(tool_calls) → user → tool` 這種不合法的相鄰順序);只有工具結果的 text block
  進模型(`structuredContent` 只給 UI / eval);多個 Engine 共用同一個 MCP instance 時
  **共用同一把模型鎖**(llama-server 單 slot,各自 new 一把等於沒有鎖)
- `client_engine` 的協作式取消——TUI 的 Ctrl-C 經 `client_turns` 走這條。串流每收一個
  chunk 看一次旗標、進行中的 MCP 呼叫要用 `begin_call` 登記給 `cancel()` 走完整取消契約
  (一步到位的 `call()` 只有 KeyboardInterrupt 一條路);中斷**不是答案**——歷史不得多出
  assistant 訊息,懸空的 tool_call 由 `run_tool_loop` 的 heal 補上「已中斷」結果;
  中斷後仍要送終結 `step_finish(reason=cancelled)`,否則看終結事件收工的一端永遠停在那裡
- `client_prompt` 的來源檔讀取——每一輪都會進 system prompt,所以父目錄被 symlink 重導
  就要 fail-loud(只驗最終檔案擋不住「把 `.codetrail` 換成 symlink」),而且用
  `O_NOFOLLOW` + `fstat` 讀,不是 path-based 檢查再 `read_text`
- `client_compaction` 的核對與節錄——空 / 只有 reasoning / 七欄格式漂移都必須停用該對話
  的自動壓縮並寫 ledger(跨行程保留,只記不可信的那幾種成因);節錄不得帶工具參數或輸出、
  不得含 pending / synthetic / 出錯回合(engine 要把不是答案的那則標成 error);同一錨點
  不重壓(錨點用內容身分,不是位置);摘要請求送的是與一般 payload **同一份** pruned 內容;
  壓縮**先落檔再換記憶體**(反過來就是畫面宣告成功、重開拿回原始歷史);換 session 要
  `rebind()`(上一段的摘要與停用狀態不得跟過去)
- `client_paths` 的 owner-only 三件套——`client.json`、session JSONL 與壓縮 ledger 共用
  同一套防線:dir fd 錨定父目錄、`O_NOFOLLOW` 開檔、`fstat` 驗普通檔 / owner /
  `st_nlink == 1`。path-based 的 `is_symlink()` 再 `read_text()` 是 check-then-use,
  兩次 lookup 之間換掉那個名字就穿過去了。讀取端**不得**順手把目錄建出來
  (「沒有設定檔 = 沒有接管」要連目錄都不留痕跡)
- `config.CLIENT_MAX_OUTPUT_TOKENS` 是**同一個數字**:實送的 `max_tokens`、context gate 的
  保留額、壓縮門檻推導的 `max_output`。上限綁 `compaction_formula.UPSTREAM_OUTPUT_TOKEN_MAX`
  (公式會把它夾在那裡、把 0 翻成它),超出範圍一律 import 時 fail-loud——不然就是
  「送 65536、門檻按 32000 算」而且完全無聲
- `client_turns` 的回合協調——`Engine` 自己看不到三個取消狀態,少一個就是「按了沒反應」:
  worker 還沒進 `send()` 的空窗(要 `request_cancel(arm_when_idle=True)` 預先武裝)、
  worker 阻塞在核准上(只能由協調器把 pending 核准**原子**回成拒絕並喚醒,engine 醒來看
  旗標丟 `TurnCancelled`、不記成一筆 denied)、取消與收尾互相搶跑(取鎖與「這一輪開始」
  必須同一個臨界區;收尾先標 turn_done 再清旗標,取消不得留到下一題)。閒置時的取消一律
  回 False(顯示成「已中斷」是謊報);慢速的 MCP 取消(等寬限期最長 10 秒)必須在協調器的
  鎖**外**做。核准:沒回答就是拒絕、只能回答一次(先 deny 再 grant 不得翻成核准)、
  只認真的 bool(`bool("false")` 是 True);同一個對話一次只跑一輪(模型鎖只序列化 HTTP
  呼叫,保護不到 session 狀態);notice 要在終結事件之前送,失敗也要送終結事件
- `client_app`(TUI)的畫面契約——核准框**完整且可捲動**顯示 `ApprovalRequest.render()`
  (含整份 patch),截斷過的核准等於沒有核准;框內 Esc / 拒絕只拒絕**這一個工具**(回合
  繼續),Ctrl-C 中斷**整輪**(核准框開著時也一樣);沒有 tty 一律拒絕並指向 headless
  `run`(靜默降級成另一種介面比擋下來更糟);輸入歷史檔逐字含使用者問過的問題,讀寫兩端
  都走 `client_paths` 的 owner-only 防線;Textual 接管畫面後不得有任何直接 stdout / stderr
- `opencode_migrate` ——唯一會寫使用者 OpenCode 設定的路徑,而且**只有使用者手動執行
  `python3 opencode_migrate.py` 時才跑**(runtime 一個模組都不 import 它;`set_config`
  不再順帶遷移)。只還原**現值仍等於 CodeTrail 寫入值**的鍵(既有 ownership 語意)、
  只移除 path 對得上的 plugin 項、`mcp.codetrail` 與 `permission` 不動、沒有狀態檔也沒有
  我們的 plugin 項的機器零寫入。ownership 狀態檔那一整半(受管鍵、`apply_mode`、
  `_restore_native`、plugin entry、`effective_drift`、owner-only 狀態檔)住在這個檔裡;
  **別份安裝的接管不動**——狀態檔記的 plugin 路徑不是本 repo 而且那個檔還在,就零寫入
  只提示(路徑不在 = 本 repo 搬過家,照舊走搬家那條路)
- `compaction_formula` ——門檻公式、canonical 規則文字與 `UPSTREAM_*` 常數的單一真值。
  它是 runtime 的那一半,**不得 import `opencode_migrate`**;`config.CLIENT_MAX_OUTPUT_TOKENS`
  的上限 fail-loud 綁的就是這裡的 `UPSTREAM_OUTPUT_TOKEN_MAX`
- 兩份安裝並存的共用檔——`compaction-stopped.jsonl` 與 `setconfig-last-transaction.json`
  在同一台機器上被兩個世代的 CodeTrail 共用,而且**不改名**(改名等於丟掉升級前記下的
  durable safety state)。隔離靠語意:ledger 只認自己的 schema、以 session 雜湊為鍵;
  restore 只接受「目標**全部**落在這一代會寫的四個檔」的 manifest,含任何其他目標就整份
  fail-loud、一個檔都不動(不退回逐檔模式、不部分還原——半套還原會把兩個世代拼在一起)。
  tool-call canary 的快取檔名帶 schema 號,兩世代各記各的、不再互相清空。
  main runtime 永不讀、寫、刪 `compaction.json` / `opencode.json` / 任何 opencode plugin 路徑
- `kb_cache` 的 embeddings 身分驗證（逐列 chunk id / generation / 內容雜湊 / model）
  與「重建不了就 fail-loud、絕不沿用舊向量」——放寬它就是靜默錯答
- `knowledge_store` 的文件身分驗證（`metadata["document_sources"]`）與
  `DocumentIdentityConflict`——KB 用 basename 當文件識別，所以 `a/spec.pdf` 與
  `b/spec.pdf` 是同一個身分；拿掉這道閘，後灌的那份會把前一份整份換掉，訊息
  跟正常更新一字不差，查詢照樣回答但答的是別份文件
- `knowledge._gated_completion`——knowledge.py 所有主模型 `/completion` 的唯一
  出口。繞過它等於沒有 context gate，超長 prompt 由 llama-server 從前面靜默截掉
- `elf_analysis.safe_regex` / `_regex_is_safe`——`analyze_file` 的 `target` 只接受正面表列的安全
  regex 子集（不收任何群組、`|` 只在最上層且 ≤ 8 分支、`*`/`+` 合計 ≤ 1、`?` ≤ 3、不接受 `{n,m}` /
  backreference / lookaround / inline flag），其餘改字面比對；比對主體只看前 300 字元。Python `re`
  沒有 timeout 也不釋放 GIL，放寬它就是讓一個 target 卡死整個同步的 MCP server
- `session_eval` 的私人 session 評測邊界——mined/curated 資料不得把歷史 assistant 回答當
  oracle；private writer 必須維持目錄 0700、檔案 0600、拒絕 symlink；read-only replay 必須
  deny 寫入／執行工具並以前後 project-state digest 偵測現場變動；checkpoint/resume 必須綁定
  suite digest、live model fingerprint、case 順序與逐題 project-state digest，單題 timeout 不得
  讓已完成結果無聲消失。原始 NDA prompt、工具輸出、candidate answer 不得寫入 checked-in
  `eval/` 或 privacy-safe aggregate
- `opencode_migrate` 的壓縮模式 ownership 狀態檔——owner-only(目錄 0700／檔 0600、拒
  symlink 與 symlink 父目錄、dir-fd 原子寫入)、`digest` 必須涵蓋 `prior`(還原時會被
  寫回設定的正是它)、狀態綁定單一目標 config、以及「沒有狀態檔 = 沒有接管」的
  fail-closed 預設。切回 native 只能還原**仍有 ownership 證據**的值(JSON 型別嚴格
  相等),放寬任何一條就是靜默改掉或刪掉使用者的 OpenCode 設定;
  `MANAGED_COMPACTION_KEYS`(接管/還原)與 `CONTRACT_COMPACTION_KEYS`(值不符就
  停用自動壓縮)是兩組,不得合併——把只影響 context 用量的鍵(`prune`)併進契約集合,
  等於為它停掉整個 session 的壓縮,而且每次新增受管鍵都會讓舊狀態檔的安裝在
  升級當天全部跳 config_drift;新增受管鍵時 **不得**由 runtime 或 contract check
  自己補寫(沒有 ownership 紀錄就還原不回去),只能由 `unmanaged_keys` 報出來、
  使用者重跑 set_config
- `opencode_plugins/*.js` 現在是**inert stub**(單一 export、零副作用、只 toast 一次
  「請執行遷移」)。上面那些壓縮 hook 契約(規則以 context 附加、`autocontinue=false`、
  壓縮後核對、`messages.transform` 只動歷史 reasoning、fail-open)已經整組搬進
  `client_compaction` / `client_engine`,由 `tests/test_client_compaction.py` 與
  `tests/test_client_engine.py` 守。stub 的契約(`tests/test_repo_consistency.py` 靜態
  釘住):每個檔恰好一個 export、只回傳 `event` 這一個 hook、只在 `session.created`
  toast 一次、不得有 `tool.execute*` / `chat.*` / `experimental.*` 之類的 hook、不得
  import / require 任何模組、不得讀檔或發網路請求;不要把邏輯加回 JS 那邊。

任何重構碰到上面這些東西，**新加測試**（開發者寫測試檔，執行依 §1.2 權責），
不要直接刪 / weaken / 移除檢查點。

新增安全檢查點時，守它的測試檔要標 smoke 並登記進 `tests/test_smoke_gate.py`
的 `SAFETY_MODULES`；漏標是無聲的（smoke 綠燈但那個檢查點根本沒跑）。
`SAFETY_MODULES` 記的是「檔名 → (說明, 必須存在且帶 smoke 的 node 名)」，不是
只記檔名：只驗「這個檔至少有一個 smoke」的話，刪掉那條檢查點測試、或把
module 層 `pytestmark` 換成單條 decorator，gate 都還是綠的。

---

## 3. 不要做的事

- 不要把 `from config import X`（snapshot）混 `import config; config.X = ...`（mutation）— 動態值只用 `import config`。
- 不要為了讓 lint 漂亮，刪未檢查影響的 unused import — 有些是 side-effect import。
- 不要把 ALLOWED_COMMANDS 加 `rm` / `sudo` / `curl` / `bash`。
- 不要把 `RUN_COMMAND_ENABLED` / `PATCH_ENABLED` 在 `config.py` 的預設改成 `True`。runtime 若要開，必須維持在 `mcp_server.py` 這類明確啟動點。
- 不要在 `mcp_server.py` 加新 tool 卻沒同步更新 `README.md` / `docs/mcp-tools.md` 工具清單 — 模型會誤用，使用者也會困惑（`aicode` 健檢會要求工具集合與文件精確一致）。新的**寫入**工具要不要人工核准是另一件事:互動 policy 的預設是 allow,要核准就得加進 `client_policy.ASK_TOOLS`(`apply_patch`、`run_lint`、`run_command`、`remove_document`、`record_lesson`、`review_figures`、`import_external_file`)。
- **不要新增 `os.environ` 讀取。** 客戶端與 MCP 的設定只有三個來源,全部是檔案:
  repo 常數 `config.py`、`~/.config/codetrail/{deployment,models}.json`、
  `~/.config/codetrail/client.json`;行程之間用 **argv** 交接。新設定要嘛是
  `config.py` 的常數(所有使用者一致),要嘛是 `client.json` 的鍵(每個使用者選)。
  環境變數不行的理由:同一台機器可能有兩份安裝,另一份的 `~/start.sh` export 的
  同名變數會靜默蓋過設定檔 —— 症狀是「使用者以為在跑 A、實際在跑 B」,沒有任何
  錯誤訊息。允許讀的只有「檔案在哪 / 行程介面」那幾個(`HOME`、`XDG_*`、`PATH`、
  `PYTHONIOENCODING`)與**啟動核心**(`deployment_profile` / launcher / `set_config`
  的 start.sh 產生器),由 `tests/test_repo_consistency.py` 的 allowlist 靜態守住。
- 不要 `git commit` 沒被使用者確認過的修改。

---

## 4. 預設離線

- CI 不可以依賴 llama-server / GPU / 大型 GGUF 下載。
- 任何測試用到 LLM 都要 mock 或 graceful skip（`pytest.importorskip` 或 `pytest.skip`）——
  這是**撰寫**測試的規範，執行權責見 §1.2。
