# startup-ttft-20260907 — Lane D 施工報告(文件與 AGENTS)

- 施工者:Claude Opus 5.0(CLI 指定 `claude-opus-5`,effort MAX;模型身分由主代理以 CLI init / modelUsage 核對並記進 `00-model-log.md`)。
- 日期:2026-09-07。worktree:`/tmp/codetrail-startup-ttft-20260907/worktrees/d`。
- base HEAD / actual HEAD:`f200f697ba54d38a102e8ef66dead652c4002e5f`(未 commit、未 push;產品變更全部留在 worktree)。
- 依據:`03-plan-final.md` §2、§4.4、§5 C、§6 Lane D、§8、§10。依 Fable 的定稿施工,**沒有**重新規劃。
- 未執行任何測試(smoke / full / pytest 一律沒跑)、未對任何 live server 發請求、未讀私人 session、未動部署、未 `git commit` / `push`、未改 `.gitignore`、未動別人 owned 的檔或主 repo 產品。

---

## 1. Dependency 與核對狀態

| 依賴 | 狀態 |
|---|---|
| Lane A(`Preflight.banner_lines()` / 進度 LOG 不重播) | 交付檔 `04-impl-a.md` 已出,**已核對**:`banner_lines(*, tools, permission, compaction)` = 摘要一行 + `status`(壓縮狀態行)+ `warnings`(含 preflight 期間所有 stderr 行);失敗路徑不變。A8 未做的兩項(`README.md` / `docs/setup.md`)正是本 lane 的可寫檔,已補上。 |
| Lane B(engine 預熱 / telemetry) | 交付檔 `04-impl-b.md` 在本 lane 起草**之後、收尾之前**出現(11:18),**已核對**:`config.CLIENT_PRIME_PROMPT_CACHE`、`Engine.next_turn_prefix()` / `prime_prompt_cache(*, reason)` / `abort_prime()`、`PrimeOutcome(sent, reason, processed_tokens)` 的 reason 值域、`ContextUsage.prompt_tokens_processed`(只由 `timings.prompt_n` 填、`actual_prompt_eval_count` 語意不變)全部與 §4.2 一致,文件用到的名稱與字串都對得上。 |
| Lane C(協調器 / TUI) | 交付檔 `04-impl-c.md` 同樣在收尾前出現(11:14),**已核對**:`TurnCoordinator.prime_in_background(reason, *, on_done=None)`、`engine.priming`、狀態列相位 `等待首個 token` / `thinking {n} 段` / `回答中` / `壓縮中`、`prompt cache 預熱中`、`/status` 的 `prompt cache 預熱=尚未|sent <HH:MM:SS>|skipped(<reason>) <HH:MM:SS>|error <HH:MM:SS>`。**採納了 C §9.2 的已知落差**:壓縮後那一次預熱不會更新 `/status` 那一行,已逐字寫進 troubleshooting。 |

文件先依 §4 凍結介面起草,再逐項比對三份交付檔;**沒有**任何一處是靠猜介面寫的。
整合者仍應在套完 B → C → A → D 之後,以整合後的樹再跑一次
`python3 scripts/check_readme_consistency.py`(本 lane 只在自己的 worktree 跑過)。

## 2. 可寫檔案與 owner

| 檔 | owner | 本 lane 有沒有動 |
|---|---|---|
| `README.md` | Lane D(唯一) | 有 |
| `docs/setup.md` | Lane D(唯一) | 有 |
| `docs/troubleshooting.md` | Lane D(唯一) | 有 |
| `README_DEV.md` | Lane D(唯一) | 有 |
| `AGENTS.md`(§2 只加 §4.4 指定的三句) | Lane D(唯一) | 有 |
| `docs/workflows/startup-ttft-20260907/04-impl-d.md` | Lane D(唯一交接檔) | 有(本檔) |

產品程式碼、測試、`tests/test_smoke_gate.py`、`.gitignore` **一個字都沒動**
(`git status` 只有上表前五個檔的 ` M`,無 untracked)。

## 3. 實際修改內容

### 3.1 `README.md`(§5.2「啟動 TUI」,原 706–707 行)

原本寫「啟動前置…的輸出會留在對話區第一則,所以 TUI 接管畫面之後仍然看得到」——
Lane A 之後這句話是錯的。改成:通過時對話區只留**一行摘要、壓縮狀態行與警告**
(含工具健檢寫到 stderr 的行),逐項進度的完整輸出留在 TUI 接管畫面**之前**的終端;
失敗時 `aicode` 不進 TUI、錯誤原樣留在終端。

### 3.2 `docs/setup.md`(`aicode` wrapper 詳細行為,第 10 項)

原本「上面每一行都留在對話區第一則,所以清屏之後仍然看得到」改成:通過時對話區只有
一行摘要 + 壓縮狀態行 + 警告(含第 7、8 項寫到 stderr 的工具健檢警告);上面每一行仍
完整留在 TUI 之前的終端;失敗時不進 TUI。

### 3.3 `docs/troubleshooting.md`

1. 「快速分流」表新增一列:`server 沒關過,開新對話第一個字仍要等很久` → 搜尋 `cache 冷熱`、`預熱`。
2. 在「MoE 模型第一次對話 TTFT」之後、`mcp-connected-but-no-tool-call` 錨點之前,新增
   **「開新對話首字慢:先分辨 prefill、reasoning 與 cache 冷熱」**(§4.4 指定的標題),內容:
   - **三段相位表**:`等待首個 token`(prefill)/ `thinking N 段`(reasoning,`show_reasoning`
     預設關)/ `回答中`,逐段寫「能不能縮短」;`/thinking` 只影響畫面,`keep_historical_reasoning`
     是另一個鍵。
   - **prefill 機制**:每個 model step 只送一個 `POST /v1/chat/completions`、`cache_prompt` 硬編碼
     開啟(沒有設定能關),所以問題是「prefix 在不在 KV cache 裡」。
   - **prefix 組成表**:內建基底規則(≤ 1,600 字元)、MCP 使用說明、專案 `AGENTS.md`
     (≤ 40,000 字元)、`.codetrail/lessons.md`、`~/.config/codetrail/instructions.md`
     (≤ 8,000 字元)、沙箱根目錄一行、19 個工具的 JSON schema。
   - **什麼時候會冷**:server 剛起 / 換模型、換專案、改過那三份檔、中間插進別的 prefix
     (壓縮摘要、`query_knowledge` 內部的查詢改寫、另一個客戶端)。4 slots 與最長共同前綴
     挑 slot、SWA / hybrid checkpoint 可能重算,**明寫成「本機這顆 build 的實作行為,不是所有
     版本的保證」**(§0 註記:此事實引用 `02-plan-review.md` B03 的查核,本 lane 未重驗)。
   - **預熱做什麼 / 不做什麼**:四個時刻(TUI 就緒、`/new`、換 session、壓縮成功換掉歷史)、
     只送下一輪的 prefix、`max_tokens=1`、零對話內容、readonly 與 headless `run` 永不、
     `/new` 與換 session 會中止、**不能**縮短 thinking、也不會讓硬體變快、熱 prefix 時無感;
     成本帳寫明「最多多一次 `/slots` 查詢 + 一個 token 的生成」,預熱中送出的題目會在模型鎖上
     等(可 Ctrl-C)。
   - **`/status` 的 `prompt cache 預熱=`**:四種顯示 + 跳過原因表(`disabled` /
     `tools_not_loaded` / `policy` / `model_busy` / `turn_in_progress` / `server_busy` /
     `next_turn_would_overflow` / `gate` / `aborted` / `error:<類型>`),並註明這一行只反映
     mount / `/new` / 換 session 三種(Lane C §9.2 的已知落差)。
   - **遙測讀法**:`.codetrail/context_metrics.jsonl` 的 `source` / `estimated_input_tokens` /
     `prompt_tokens_processed` / `message_count`,附一段唯讀的 `python3` 列印片段;判讀規則照
     §5 C(prime 之後那一列 `client` 的 `prompt_tokens_processed` 小 = 真的被重用;仍接近自己的
     `estimated_input_tokens` = 這顆 build 在這個情境下不重用)。明寫
     `actual_prompt_eval_count` **不能**拿來判冷熱(server 同時回 `usage` 與 `timings` 時取 `usage`)。
   - 全節**零環境變數**、**未引用本交接目錄**、沒有任何 TTFT 加速數字。

### 3.4 `README_DEV.md`

1. 「測試指南」段補一段:預熱(`Engine.prime_prompt_cache` / `TurnCoordinator.prime_in_background`)
   **只有契約測試,沒有 red-before-green**,並寫出要釘的是哪四件會無聲失敗的事。
2. 「Telemetry 隱私政策」的欄位清單補 `prompt_tokens_processed`。
3. 新增 `### prompt cache 預熱(prime)與首字延遲`(放在 context_budget 那一節末尾),含
   **模組分工表**:`config.CLIENT_PRIME_PROMPT_CACHE`、`Engine.next_turn_prefix()`、
   `Engine.prime_prompt_cache(*, reason)`(准入順序逐步列出)、`Engine.abort_prime()`、
   `TurnCoordinator.prime_in_background(reason, *, on_done=None)`、
   `ContextUsage.prompt_tokens_processed`;另寫明「預熱這一次的 gate 保留額與實送 `max_tokens`
   都是 1、下一輪適用性另判一次且不寫 telemetry」。
4. 同一節寫入 §4.4 指定的結論:**SSE 客戶端 buffering 已查證不是首字延遲來源,不要再修**
   (urllib3 chunked 逐 chunk 回傳、`: ` keep-alive 註解行已略過,有契約測試釘住)。

### 3.5 `AGENTS.md` §2(只加三句,零刪除、零弱化)

| 條目 | 追加的那一句 |
|---|---|
| `client_engine` 的訊息轉換 | `prime_prompt_cache` 是唯一沒有使用者訊息就打主模型的路徑:零寫入(不進 `_begin_turn`、不 `_record`、不發事件、不動取消旗標)、只送 `next_turn_prefix()`(與下一輪同一套轉換)、實送 `max_tokens=1` 且 gate 保留額就是 1、非 interactive policy 一律拒絕、headless 沒有呼叫點 |
| `client_turns` 的回合協調 | `prime_in_background` 不取回合鎖、不動 `_turn_done` / `_cancelled`,取消對預熱是 no-op |
| `client_app`(TUI)的畫面契約 | 啟動橫幅只拿 `Preflight.banner_lines()`:摘要一行、壓縮狀態行、警告(含 preflight 期間所有 stderr 行);進度行不進畫面,但 `Preflight.lines` 仍是完整 transcript |

三句都是**追加在既有條目末尾**(分號接續),既有安全條文一字未刪、未改弱;§1 / §3 / §4 未動。

## 4. 執行過的命令與結果

| 命令 | 結果 |
|---|---|
| `python3 scripts/check_readme_consistency.py`(共 4 次,最後一次在**最終**文字上,即 patch 匯出後的同一份內容) | 四次都是 `[readme-consistency] OK — README/docs ↔ mcp_server.py / config.py 一致` |
| `git status --porcelain=v1 --untracked-files=all` | 只有 `AGENTS.md` / `README.md` / `README_DEV.md` / `docs/setup.md` / `docs/troubleshooting.md` 五個 ` M`,無 untracked |
| `git rev-parse HEAD` | `f200f697ba54d38a102e8ef66dead652c4002e5f` |
| `git diff --stat` | 見 §5 |
| `git diff --binary > /tmp/codetrail-startup-ttft-20260907/patches/lane-d.patch` | 18,984 bytes,sha256 `271642fadb107bda6d62ee6528029fe5281e4816e5f8b796f73457008b4e04dc` |

**零測試執行**(本 lane 依 §6 為零測試 lane:沒有跑 smoke、沒有跑 full、沒有直呼 pytest,
也沒有跑 `run_tests.py`)。`check_readme_consistency.py` 是靜態檢查、不收集 pytest。

## 5. patch 與 `git diff --stat`

- patch:`/tmp/codetrail-startup-ttft-20260907/patches/lane-d.patch`(`git diff --binary`,base `f200f697…`)

```
 AGENTS.md               |  12 +++--
 README.md               |   4 +-
 README_DEV.md           |  35 +++++++++++++-
 docs/setup.md           |   3 +-
 docs/troubleshooting.md | 118 ++++++++++++++++++++++++++++++++++++++++++++++++
 5 files changed, 166 insertions(+), 6 deletions(-)
```

## 6. 尚未核對 / 偏離 / 未做

1. **`python3 scripts/check_readme_consistency.py` 只在「只有 Lane D 變更」的樹上跑過。**
   B / C / A 的 patch 尚未套進本 worktree(不是本 lane 可寫的檔),整合後請再跑一次;
   同理,本 lane 沒有跑 `tests/test_repo_consistency.py`(它是 pytest,依 §6 本 lane 不得執行),
   文件層面的靜態 gate 由整合者的那一次 smoke 涵蓋。
2. **偏離 §4.4 的地方:一處,而且是往「更精確」的方向。** §4.4 寫「prefix 五段 + 工具 schema」;
   實際的 `client_prompt.build_system_prompt()` 是五份文字來源 + 一行沙箱根目錄,所以表格寫成
   六列 + 工具 schema,沒有寫死「五段」這個數字。其餘每一項(README / setup 兩處、
   troubleshooting 的節名與必寫內容、README_DEV 的三件事、AGENTS 的三句)逐項照做。
3. **`docs/basic-usage.md:39` 沒有動**:那一行以括號概述 `/status` 的內容
   (「模型、context、壓縮模式與 session 位置」),不是完整清單,新增一行不會讓它變成錯的;
   而且那個檔不在本 lane 的可寫清單裡。若整合者認為要一起提「預熱」那一行,那是另一個 owner。
4. **沒有寫任何 TTFT 數字**:全份文件不宣稱加速了多少、不宣稱消除 reasoning 或硬體 prefill 成本;
   「是否真的被重用」一律指向 §5 C 的判讀(David 的 T0),未量測就寫未量測。
5. **沒有教環境變數、沒有引用本交接目錄**:troubleshooting 只提到 `client.json` 的鍵、
   repo 常數與 `~/.config/codetrail/*` 這幾個既有來源;`docs/workflows/**` 一次都沒被使用者文件引用。
6. **`tests/` 與 `tests/test_smoke_gate.py` 完全沒碰**(整合者 owner);本 lane 沒有新增或修改任何測試,
   所以 §1.5「動到既有測試」的清單是空的。
7. 文件裡出現的 Lane B / C 介面字串(`config.CLIENT_PRIME_PROMPT_CACHE`、`PrimeOutcome` 的
   reason 值域、`/status` 與狀態列的顯示字串)已對照 `04-impl-b.md` / `04-impl-c.md` 核對過;
   若 Step 5 的回修改了任一字串,`docs/troubleshooting.md` 與 `README_DEV.md` 要跟著改(這是
   本 lane 交出的唯一跨 lane 耦合)。

## 7. 誠實揭露(§5 B9 固定句)

reasoning 與硬體 prefill 成本未變;預熱只把**下一輪 prefix** 的 prefill 搬到打字之前;
熱 prefix 時無感;是否真的被重用只由 T0 的 `prompt_tokens_processed` 判定,規劃與施工
階段沒有數字。
