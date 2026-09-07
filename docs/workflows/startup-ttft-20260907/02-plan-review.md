# Step 2 計畫靜態審核 — Astra MAX

- 日期：2026-09-07。
- 角色：使用者指定 `ROLE=REVIEWER`；本步只做靜態審核。
- 審核對象：`01-plan-initial.md`，SHA-256 `26ac2d945f9ceaf3b69371bada01aede676a698a59cf26dd34a08d14397e14db`。
- 動工 base HEAD／本步觀測 HEAD：`f200f697ba54d38a102e8ef66dead652c4002e5f`；本步未修改產品或測試。
- 已完整閱讀 `AGENTS.md`。本步沒有執行測試、沒有向 live server 發請求、沒有讀私人 session、沒有 commit 或 push；唯一寫入為本檔。
- 本機 llama.cpp 靜態來源 HEAD：`6ea215d171fd31df943bf1ac8227129f2b963160`，與 intake 所記 build `b10276-6ea215d17` 對應。4 slots／每 slot n_ctx 131072／觀測時其中 1 slot 忙，引用 `00-intake.md:17` 的既有查核，未重做 live GET。

結論：**10 項 Blocker，B08 僅在定稿保留選配 T5 時適用。** 以下只列會違反初稿驗收、既有呼叫端／測試或 AGENTS 的問題。方案取捨由 Fable 定稿；本審核不替換方案。保留哪些成功摘要、warning、status 由 Fable 在使用者已確認的需求內裁決，不需要再向使用者確認一般實作取捨。

下列「計畫」行號均指 `docs/workflows/startup-ttft-20260907/01-plan-initial.md`。

## B01 — 當下 payload 不等於下一輪的可重用 prefix

**位置：** 計畫 `94`、`117`、`198–215`、`267`；`client_engine.py:92–111`、`119–140`、`762–795`、`815–820`；本機 `llama.cpp/models/templates/deepseek-ai-DeepSeek-V4-Flash-0731.jinja:90–103`。

**失效路徑：** 接續或壓縮後的歷史若以 `user(q1) → assistant(a1, reasoning_content=R)` 結束，prime 呼叫當下的 `payload_messages()` 仍保留 R。真正下一輪的 `send(q2)` 會先記入新 user，然後同一個轉換函式把 R 剝除；prune 的 user-turn 邊界也會前移。對帶 tools 且啟用 thinking 的本機 DeepSeek 模板，reasoning 的存在確實影響渲染。兩個請求因此會在歷史中間分岔，並非僅多出「使用者訊息 token + 模板尾巴」。甚至可能 prime 因長 R 撞 gate 而略過，下一輪剝除 R 後卻能正常送出。

計畫中的測試只把 prime 與「同一時刻再呼叫一次 `payload_messages()`」比對，會在上述情境仍然通過；它沒有守住 D4、B7 名稱與新增 AGENTS 文字所承諾的「下一輪同一份 payload」。

**最低修正條件：** 定稿須明訂實際要保證的可重用 prefix 範圍，與下一輪的真實 reasoning／prune 邊界一致；不能以同函式自我比對宣稱已證明下一輪等價。若仍預熱完整歷史，加入合成的 reasoning／prune 邊界契約案例，將 prime 與後續真實 `send()` 擷取的內容比較；保留 session／UI 原文及既有轉換安全契約。若縮小承諾，B1、B7、B9、文件與 AGENTS 新文字須同步，不得仍聲稱完整下一輪等價。

## B02 — 背景工作的准入不是原子操作，會在真回合開始後補發 prime

**位置：** 計畫 `118–128`、`207–213`、`224–229`、`268–269`；`client_turns.py:109–133`、`234–266`、`334–359`；`client_engine.py:749–754`、`815–820`、`1175–1197`；`client_app.py:1031–1063`、`1111–1138`。

**失效路徑：** `_prime()` 在 UI 執行緒看到 `busy=False`，只把工作排到另一條 thread；該 thread 尚未執行時，使用者已經 `start_turn()`，engine 寫入 user、組 payload，尚未取得 model lock。prime 此時仍可先拿到 model lock、讀到進行中回合的 history 並發 HTTP，真回合反而等它。壓縮後 `finish_turn()` 與 `_spawn(prime)` 之間也有相同空窗。model lock 只序列化 HTTP，正如 `client_turns.py:8–9` 說明，沒有保護回合狀態。

另外，prime 排程沒有 session／history 身分：等待排程期間 `/new` 或切換 session 後，舊工作仍可執行。兩個 `_prime()` worker 各自設定同一個 `_priming` bool 時，第二個因取鎖失敗先返回，就能清掉第一個仍在執行的預熱狀態；B9 所依賴的「狀態列預熱結束」會提早出現。這裡沒有證據顯示 prime 自己會寫壞 session，問題是准入、內容身分與就緒訊號不符合計畫。

**最低修正條件：** 定稿須定義與真回合開始／session 切換協調的准入與內容快照邊界，淘汰過期排程，並讓狀態由實際有效工作擁有。以受控 Event／barrier 契約覆蓋「排程後才送出」、「壓縮收尾後立刻送出」、「切換後舊工作才醒來」及重複 prime。真回合的 Ctrl-C 仍須及時結束、保留完整模型鎖租約契約；prime 不得借用真回合的取消／commit 狀態而污染 session。僅測呼叫 `_prime()` 前已經 busy 不足以守住此路徑。

## B03 — B9 的 cache 命中與「不變差」驗收缺少有效前提

**位置：** 計畫 `14`、`60–68`、`83–84`、`94–97`、`119–130`、`163`、`209–211`、`271`、`295`；`00-intake.md:17–18`；本機 `llama.cpp/tools/server/server-context.cpp:1574–1686`、`3272–3365`。

**失效路徑：** 本機已知是 4 slots。初稿仍把「上一個請求不是聊天 prefix」推成下一題整份重算，並把 `n_parallel` 留待 T0。該 build 會在可用 slots 間按 LCP 選擇、必要時走 LRU／host cache；SWA／hybrid checkpoint 又可能令已匹配的 prefix 重算。多 slot 本身不能證明預熱無效，也不能支持初稿的單 slot 因果結論。

B3 又規定任一 slot 忙就跳過，沒有之後重試；這正包含 intake 已觀測過的「1 忙、3 閒」情況。此時狀態結束只代表略過，B9 卻仍要求第一題只評估數百 token。即使 prime 成功，前後另一個客戶端的請求或 checkpoint 條件也不在目前驗收前提內。D4 的「打字比預熱快，總時間不變、不變差」同樣無法成立：取得鎖後額外的 `/slots` 等待、一次請求／生成及 B01 的重算都會增加真回合的等待。

**最低修正條件：** 定稿先納入已知 build／4 slots 事實，明確區分略過、HTTP 完成與已證實 cache 可重用；為 B9 寫出可檢驗的前提及失效判準，不能將所有情況都認成成功預熱或保證 TTFT 不增加。若 b 仍以此機制交付，需說明上述可發生分支如何符合所選驗收；未量測的收益維持未量測。這不要求指定 `id_slot`、更改部署或新增 live 生成，修法由 Fable 判斷。

## B04 — 新增非串流 prime 後，T0 會把總 prompt tokens 誤當成實際 prefill tokens

**位置：** 計畫 `82`、`121`、`149–156`、`211–215`；`context_budget.py:385–407`；本機 `llama.cpp/tools/server/server-task.cpp:393–399`、`469–484`，`server-context.cpp:546–553`、`2150–2151`。

**失效路徑：** 該 build 的非串流 chat response 同時帶 `usage.prompt_tokens`（全部輸入 token）及 `timings.prompt_n`（本次實際評估 token）。`parse_usage_from_response()` 先採 `usage.prompt_tokens`，有值時不再採 `timings.prompt_n`。計畫新增的 `stream=False` prime 因此把總數寫進 `actual_prompt_eval_count`；T0 卻將所有 `source=prime` 列的這個欄位直接當 `prompt_n`，用它判冷熱並除以 processed-token 吞吐計算 prefill 秒數。熱 cache 也會被報成冷 cache，所需量測無法驗收。

初稿的 mock response 刻意只有 timings，會漏掉真實 response 的兩組欄位並存情況。本次不要求全面修改既有 telemetry；Blocker 是新 prime／T0 明確依賴了不成立的欄位語意。

**最低修正條件：** 為本次冷熱判讀選定真實的 processed-token 訊號，與既有總輸入計數分清楚；在定稿更新相應 owner、介面與 T0。新增契約應涵蓋同時有 `usage.prompt_tokens` 和較小 `timings.prompt_n` 的回應，不能只測缺 usage 的替身。

## B05 — prime 的本次 gate／telemetry 保留額刻意不等於實送 max_tokens

**位置：** 計畫 `117`、`198–211`；`AGENTS.md:120–123`；`context_budget.py:261–269`；既有契約 `tests/test_client_engine.py:664–687`。

**失效路徑：** T3 第 4 步把下一輪的 `options.max_output_tokens` 記為 `source="prime"` 的本次請求保留額，第 5 步卻實送 1。初稿雖寫了理由，現行 AGENTS 明訂實送 `max_tokens`、gate 保留額與壓縮推導的單一真值；`build_usage()` 也把保留額定義為「這一次 request 實送的 max_tokens」。保守多留額度不會造成少算 context，但不能一面新增不一致的 request snapshot，一面宣稱沒有變更這條契約。

**最低修正條件：** 定稿須將「下一輪是否適合預熱」的判斷與「本次 request 的 gate／usage」語意釐清，讓實送與記錄一致，並保留正常回合 `CLIENT_MAX_OUTPUT_TOKENS` 的 import-time 上限及壓縮公式唯一真值。相應契約要涵蓋新 prime；不得只在 docstring 註記例外、讓既有安全規範與新增行為互相矛盾。

## B06 — readonly 永不 prime 的凍結契約沒有實作入口防護

**位置：** 計畫 `95–96`、`119`、`121`、`126`、`207–213`、`234`、`272`；`client_engine.py:425–439`、`459–491`；`client_policy.py:88–94`；`codetrail_chat.py:107–117`、`148–189`。

**失效路徑：** D5／D6 和擬新增 AGENTS 文字保證 readonly 永不預熱；T3 的所有提前返回條件卻沒有 policy／readonly 判斷。對已載入工具的 `ReadOnlyPolicy` Engine 直接呼叫新方法，鎖與 slot 閒置時仍會 GET／POST。`command_run` 不呼叫它只能證明目前 CLI headless 路徑，不能守住新增 Engine／coordinator 介面的 readonly 保證；列出的 B7 也只有 headless 測試。

**最低修正條件：** 將 readonly 不發 prime 請求的條件落在有效入口，測試確認在發 GET／POST 之前返回、沒有事件或 session／project 寫入，並依新增安全契約登記 smoke gate。headless 無呼叫點的測試保留。

## B07 — warn() 的凍結輸出形狀會改寫 A1 要求逐字不變的 transcript

**位置：** 計畫 `91`、`106`、`175–184`；`client_preflight.py:84–85`、`181`、`197`、`266–281`、`371–375`、`391–407`。

**失效路徑：** A1 要 `Preflight.lines` 的內容逐字不變。T1 卻規定 `warn()` 印 `[aicode] ⚠ ` 加 message，再把原來的 `note()` 呼叫改過去。UNKNOWN／n_ctx fallback 原本沒有 ⚠，會被新增；原本已帶 ⚠ 的 lessons／legacy hint 若逐字改呼叫，又會得到兩個 ⚠。完整 transcript 由實際輸出 tee 得到，因此即使 `lines` 的收集程式不改，內容仍已改變。現有擬新增測試只有 substring 比對，抓不到 A1 違反。

**最低修正條件：** 定稿統一 warn 分流介面與 A1；在保留 A1 時，分類不得重寫既有輸出內容，含多行警告及後續處置行。契約須能確認分流之後完整 transcript 沒有增加／重複前綴，同時 TUI 保留所選警告／狀態、移除進度行。成功畫面要保留哪些資訊仍由 Fable 裁決。

## B08 — 若保留 T5，新增 /status 依賴會使既有 TUI 測試呼叫端失敗

**位置：** 計畫 `230`、`254`、`259–263`；`client_app.py:995–1003`、`1149–1165`；`tests/test_client_app.py:80–98`、`1070–1085`；`context_budget.py:206–223`。

**失效路徑：** T5 直接讀 `engine.system_prompt.text` 並呼叫 `engine.openai_tools()`。既有 `_Engine` 替身的 system_prompt 只有 `sections`，也沒有 `openai_tools()`；`test_slash_commands_never_reach_the_model` 真的經 `/status` 路徑呼叫。依初稿施工會得到 AttributeError，不能以「格式只追加，所以既有斷言不受影響」排除。另 `estimate_tokens()` 回 `(tokens, chars)`，須先解包才符合所定 `prefix≈<tokens>` 顯示介面。

**最低修正條件：** 若定稿保留 T5，將必要的既有呼叫端／替身相容調整列入 Lane C owner 與交付變動說明；不要刪除或弱化既有 slash-command 契約。若刪去選配 T5，本項自然關閉，不要求額外 UI 儀式性測試。

## B09 — 單 node red/green 的授權被擴張到非 bug 契約，且一項必改測試檔沒有 owner

**位置：** 計畫 `113`、`167–190`、`221`、`234`、`244`、`249–254`、`279–281`；`AGENTS.md:26–30`、`38–53`；`codetrail_chat.py:295–347`；`tests/test_lessons.py:399–403`。

**失效路徑：** §5.1 把每個 regression／契約 node 都要求單跑紅、綠；AGENTS 施工中的單 node 例外只授權「修 bug 時自己新寫的 regression」。例如 `test_headless_run_never_primes_the_prompt_cache` 是新增防護契約，既有 `command_run` 本來就不 prime；依 T2 指示使用 `getattr` 相容舊 Engine 後，它在未改產品上應直接通過，不可能提供真實的行為紅燈。為湊紅而故意使替身缺方法或破壞 production 不是 red-before-green 證據。

此外 A8 明定要改 `tests/test_lessons.py` 註解，但 Lane A、Lane D、整合者的 owner 清單均未列它，§4 又禁止改 owner 清單外檔案；照計畫無人能完成這項驗收。

**最低修正條件：** 定稿逐項區分實際 bug regression 與新增安全契約；只有授權的 regression 在未修產品上單 node 紅→綠，非 bug 契約在交付 smoke／reviewer full 執行，不偽造紅燈。為 `tests/test_lessons.py` 指定唯一 owner，並保持既有測試異動逐項說明。最後整合產品的 smoke／full 仍需對同一份凍結內容；lane 的局部結果不能替代整合後驗證。

## B10 — 固定宣稱 HEAD 仍是 f200f69，與已授權交接 commit 的流程不相容

**位置：** 計畫 `7`、`134`、`245`、`281`、`288–289`；`00-intake.md:10`。

**失效路徑：** 流程要求 commit 計畫／review／deferred markdown，故執行後 `git rev-parse HEAD` 會前進；`245` 卻指定它「仍是 f200f69」，`281` 也把 reviewer 回報 HEAD 寫死。產品不能自行 commit 是正確邊界，但不能因此把 docs commit 後的實際 HEAD 說成動工 base。

另 `git diff | sha256sum` 只識別未暫存的 tracked diff；若存在 staged 產品或新增未追蹤產品，它不會涵蓋該內容。此指令只有在把這些前提列入凍結檢查時，才能當本次所有測試與審核所指的產品識別。

**最低修正條件：** 分別記錄固定動工 `base HEAD=f200f697…`、每次實際工作樹 HEAD，以及同一份凍結產品內容 digest。digest 方法需明確涵蓋完整產品／測試內容，或明確拒絕使 diff 漏項的 staged／untracked 形狀；交接 markdown 不得令未變產品被誤判成另一份。保留產品未 commit／未 push 的事實，不以「對目前 HEAD full 通過」冒稱修正已進 HEAD。

## 本步邊界與非 Blocker 紀錄

- 保持 `Preflight.lines`，由 `command_chat` 改用另一份 banner，與現行 TUI `banner` 介面相容；失敗 preflight 留在 TUI 之前 exit 2 的方向沒有發現 Blocker。
- 既有共享 model lock、`_ModelSlot` 等鎖可取消和 session 先落檔再換記憶體，應原樣保留。上述 B02 沒有要求移除這些防線。
- `get_slots(..., quiet=False)` 新 keyword-only 參數可保留既有呼叫端預設，並解決新增 TUI 背景 probe 的 stderr 邊界；初稿所列 gate 鍵均已存在，可由整合者追加指定 node。
- `gpu_safety.runtime_offload_check` 只認舊 `state` 欄位是初稿已列的既有問題，留給 deferred，不作本次 Blocker。
- 未執行 smoke、full、compileall 或任何會間接觸發測試的命令；沒有紅／綠或效能量測結果可宣稱。

交接：Fable 依以上集中回報寫 `03-plan-final.md`，並自行決定符合使用者 a／b 需求的定稿；本 reviewer 沒有改寫初稿或產品。
