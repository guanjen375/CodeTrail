# startup-ttft-20260907 — Step 1 初步計畫(Fable 5.1, effort MAX)

- 規劃者:Claude Fable 5.1(`claude-fable-5-1`;自述身分,另由 CLI init/modelUsage 核對)
- 日期:2026-09-07
- base HEAD:`f200f69`(workspace clean)
- 本檔是唯一可寫產物;規劃階段**沒有執行任何測試、沒有對 llama-server 發任何請求、沒有讀任何私有 session**。
- 交接目錄:`docs/workflows/startup-ttft-20260907/`(目前被 `.gitignore` 的 `workflows/` 規則忽略;交接 markdown 以 `git add -f` 提交,使用者僅授權 plan / review / deferred 文件 commit;產品 diff 保留可審、不自行 commit / push)。

## 0. 兩個需求與一句話結論

| 需求 | 一句話根因 | 修法形狀 |
|---|---|---|
| a. 自檢成功後 TUI 不該重播自檢 LOG | `client_preflight.run()` 把 stdout+stderr 整段 tee 進 `Preflight.lines`,`codetrail_chat.command_chat` 原封不動接成 `banner`,`client_app.on_mount` 逐行貼成 `NoticeLine` | preflight 把「進度 INFO」與「必須保留的警告 / 壓縮狀態行」分流;TUI 只拿後者加一行身分摘要;失敗路徑本來就在 TUI 之前 exit 2,不動 |
| b. server 長駐仍有首字延遲 | 客戶端**每一步只送一個**請求且已帶 `cache_prompt: true`,但每個請求都帶一份 18–24k token 的固定 prefix(system prompt + 19 個工具 schema);只要 llama-server 那個單一 slot 的 KV cache 不是這份 prefix(server 剛起、上一個請求是壓縮摘要 / MCP 端 `/completion` / 另一個客戶端、換專案、AGENTS.md 改了),第一題就要在使用者按 Enter 之後整份重算;之後再加模型自己的 reasoning 才有第一個可見字 | (1) 用**同一份** prefix 在 TUI 就緒、換 session、壓縮後於閒置時先預熱(`max_tokens=1`),把可避免的 prefill 搬到「打字之前」;(2) 用既有遙測量化 prefix 冷/熱與 prefill 速度,不宣稱消除硬體成本;(3) SSE 客戶端 buffering 查證後**不是**原因,不改 |

## 1. 可查證的現況(全部有 file:line)

### 1.1 需求 a:自檢輸出如何進到聊天畫面

1. `client_preflight.run()`(`client_preflight.py:379-408`)以 `_Tee` 同時把 stdout 與 stderr 收進同一個 `captured`,結束時 `result.lines = "".join(captured).splitlines()`。`_Tee` 的 docstring(47-54)與模組 docstring(15-18)明寫「留一份是為了讓 TUI 起來之後把同一段訊息放進對話區第一則」——這是舊需求,本次要反轉。
2. `codetrail_chat.command_chat`(`codetrail_chat.py:268-289`):`checks = client_preflight.run(root)`;失敗 → `print(f"[aicode] {exc}", file=sys.stderr); return 2`(**在 TUI 之前**,錯誤留在終端機,需求 a 的「失敗保留錯誤」已成立);成功 → `banner = tuple(checks.lines) + ("tools=… permission=… compaction=…",)` 交給 `CodeTrailApp(banner=…)`。
3. `client_app.CodeTrailApp.on_mount`(`client_app.py:710-727`):`for line in self.banner: self._append(NoticeLine(line))`,之後貼 `輸入 /help 看指令。`。所以畫面第一屏就是整段自檢 LOG(含 canary 每 15 秒的心跳行)。
4. 已被 smoke gate 釘住、**不得**因本次改動消失的契約(`tests/test_smoke_gate.py:63-85` 的 `test_client_preflight.py` 條目):`test_the_transcript_keeps_stderr_warnings`(`tests/test_client_preflight.py:251-255`,`result.lines` 必須含 stderr 的 canary WARNING)與 `test_the_transcript_carries_the_compaction_status`(258-286,`result.lines` 必須含「壓縮模式」;`compaction_status` 的 docstring `client_preflight.py:332-337` 說明理由:自動壓縮被停用時這是使用者唯一會看到的地方)。結論:**`Preflight.lines` 保持完整 transcript,只改 TUI 消費的那一份**。
5. 文件目前教的是舊行為,要同步:`README.md:706-707`、`docs/setup.md:248`、`tests/test_client_preflight.py:15-16`(模組 docstring)、`tests/test_lessons.py:402-403`(註解)。

### 1.2 需求 b:一輪對話真的送了什麼、送了幾次

**請求數審計(TUI 路徑)**:

| 時機 | 主模型請求 | 證據 |
|---|---|---|
| aicode 啟動 preflight | **0**(只有 GET `/props`、aux server 的 `/health` + embedding/rerank/VL 探測、MCP `tools/list` + `list_dir`) | `client_preflight.py:156-183, 290-329`;`scripts/required_model_servers_check.py`(只打 8081/8082/8083);canary 的 protocol lane `scripts/tool_call_canary.py:243-287` |
| canary model lane(指紋 miss / 24h 過期 / `--force`) | 2 次 headless 完整回合(explicit + implicit),**同一份 system prompt 與工具目錄**(`--policy readonly` 只擋執行,不改 catalog) | `tool_call_canary.py:872-935, 1078-1169` |
| MCP server 啟動 | 0(載 KB / lazy CodeRAG) | `mcp_server.py:330-386` |
| TUI mount | 0(`_recount_context` 只做估算) | `client_app.py:725, 1233-1252` |
| 每一個 model step | **恰好 1** 個 `POST /v1/chat/completions`(stream) | `client_engine.py:1169-1213` |
| 回合結束 | 只有門檻到了才 1 個摘要請求(`source="compaction"`) | `client_turns.py:361-376` → `client_compaction.py:643-772` → `client_engine.complete` |
| MCP 端 `query_knowledge*` | `kb_query_expansion` **每次**(`USE_QUERY_EXPANSION = True`,`config.py:688`)+ 條件式 multi-query(`MULTI_QUERY_ENABLED = True`, `config.py:785-789`)各 1 個 `POST /completion` | `knowledge.py:361-403, 1185-1242` |

結論:聊天路徑**沒有**多餘的模型請求;「額外模型請求」只存在於 KB 工具內部與(偶發的)canary / 壓縮。

**`cache_prompt`**:客戶端每個請求硬編碼 `"cache_prompt": True`(`llama_client.py:314-320`);MCP 端 `native_completion` 也是(`llama_client.py:86-94`)。所以「有沒有開」不是問題;問題是 **prefix 是否命中**與**誰把 slot 的 cache 換掉了**。

**每回合附帶的 prefix(每一步都送、位置在最前面)**:

| 段 | 來源 | 上限 / 估計 |
|---|---|---|
| `BASE_RULES` | `client_prompt.py:44-58` | ≤ 1,600 chars(import 期 fail-loud) |
| `MCP_INSTRUCTIONS` | `mcp_contract.py:36-39` | ≤ 700 chars |
| 專案 `AGENTS.md` | `client_prompt.py:193-201` | ≤ 40,000 chars(本 repo 自己的約 20k) |
| `.codetrail/lessons.md` | `client_prompt.py:202-209`;render 無時間戳(`lessons.py:473-494`) | 小 |
| `~/.config/codetrail/instructions.md` | `client_prompt.py:211-218` | ≤ 8,000 chars |
| root 行 | `client_prompt.py:220` | 1 行 |
| 19 個工具 schema(`tools=`) | `client_engine.py:512-517` → `ToolSpec.as_openai_tool`(`client_mcp.py:135-144`) | live catalog **33,299 chars**(2026-08-26 量測,記憶體紀錄;本次由 T5 的 `/status` 再量) |

合計 55–85k chars;以 `CHARS_PER_TOKEN = 3.5`(`config.py:364`)粗估 **16–24k tokens**。JSON schema 與 CJK 的實際 token 密度不同,真值以 T0 的 `actual_prompt_eval_count` 為準。

**穩定性**:system prompt 在 `_build` 建一次(`codetrail_chat.py:178`),工具 schema 在 `load_tools` 建一次,同一行程內逐字不變;跨行程也是決定性的(builder 純函式、lessons render 無時間戳、FastMCP schema 決定性、`requests` 的 `json=` 序列化保留插入順序)。只有 AGENTS.md / lessons / instructions / 工具程式碼改動或換專案時 prefix 才變。canary 指紋已把 `system_prompt_digest` 與 `live_tools_digest` 納入(`tool_call_canary.py:500-514`),可當跨行程穩定性的既有證據。

**prefix 會在什麼時候「冷」(下一個請求必須整份重算)**——這才是可避免的部分:

1. server 剛啟動(一次性,不可避免,但可以搬到打字之前)。
2. 上一個打到 8080 的請求不是聊天 prefix:
   - 壓縮摘要請求(`client_compaction.py:388-415`:system 是七條規則,不含客戶端 system prompt、不帶 tools)。壓縮在 idle 觸發、常是一段長對話的**最後一個請求**,所以「壓縮後下一題」與「壓縮後退出、下次 `aicode` 的第一題」都是冷 prefix。
   - MCP 端 `kb_query_expansion` / multi-query 的 `/completion`:單 slot 下 `query_knowledge` 之後的那一個聊天 step 要整份重算(工具結果回來後才發現首字很慢,就是這個)。
   - 另一個客戶端用了同一台 server(本機 `~/ai_opencode` 的 OpenCode 仍在用同一份 8080;記憶體紀錄)。
3. 換專案(AGENTS.md 段起全部不同,包含其後的 33k 工具 block 的 token 位置)。
4. AGENTS.md / lessons.md / instructions.md 改了。

**跨回合的固有重算(不是 bug、不改)**:`strip_historical_reasoning`(`client_engine.py:92-111`)只在新一則真實 user 之前剝 reasoning,所以新回合的 prefix 從上一回合第一則 assistant 起與 slot cache 分岔——重算的是上一回合的助理輸出與工具結果,不是整份 prefix。這是 context 節省與 cache 的既定取捨(`README_DEV.md:173-178`);DeepSeek 系模板本身也不 render 舊回合 thinking,所以就算不剝也不一定能命中。

**reasoning 成本**:主模型是 reasoning 模型;`show_reasoning` 預設 False(`client_config.py:104`),使用者在第一個可見字之前只看到 spinner。這段是模型成本,本計畫**不宣稱消除**;只把「還在 prefill」與「已在 thinking」在狀態列分開顯示(T5,選配)。

**SSE buffering(查證結果:不是原因,不改)**:`_iter_sse_lines`(`llama_client.py:123-138`)走 `resp.iter_lines()` → `iter_content(512)` → urllib3 `HTTPResponse.stream` → 因為回應是 chunked,走 `read_chunked(amt=512)`,每個 HTTP chunk **各自**回傳,不會為了湊 512 bytes 等下一個 chunk;llama-server 每個 SSE event 就是一個 chunk 且以 `\n\n` 結尾,`iter_lines` 不會扣住半行;`:` 開頭的 keep-alive 註解行已被略過(`llama_client.py:747-751`,有測試 `test_the_openai_stream_iterator_ignores_sse_comment_and_field_lines`)。`_open_stream` 的 50 ms 輪詢(`client_engine.py:879`)與 Textual 的 `call_from_thread` 都是毫秒級。**不動 SSE 路徑**;本段結論寫進 README_DEV 以免再被「修」一次。

**其他查過、排除的候選**:`http_client` 的 Retry 只重試 connect / 5xx(`http_client.py:40-52`),不影響首字;`context_budget.check_and_log(emit=False)` 只做估算,不打 HTTP;模型鎖是單 Engine;`_recount_context` 只在回合結束與換 session 跑。

### 1.3 規劃階段無法離線確認、必須由 T0 量測的事實

| 事實 | 為什麼重要 | 怎麼量(唯讀) |
|---|---|---|
| 本機 prefill 速度(tok/s)與 prefix 冷/熱的實際差距 | 決定 b 的收益量級與驗收數字 | 既有遙測 `.codetrail/context_metrics.jsonl` 的 `actual_prompt_eval_count`(= llama-server `timings.prompt_n`,**只算這次真的評估的 token**,cache 命中時很小;`client_app.py:1237-1240` 已實測過這個語意)與 `prompt_tokens_per_second`(`context_budget.py:401-413`) |
| 主 server 的 `n_parallel`、`build_info`、chat template 是否 render `reasoning_content`、tools 在 system 之後還是最後 | 決定 deferred 的 `id_slot` 方案要不要做;決定預熱 payload 的 LCP 能到哪 | `GET /props`(canary 也讀同一份:`tool_call_canary.py:475-495`) |
| 該 build 有沒有 host prompt cache(`--cache-ram`)且對 main 生效 | 有的話「壓縮 / KB expansion 打掉 slot」會被自動還原,b 的收益只剩 server 剛起與換專案 | 看 `query_knowledge` 之後那一步的 `actual_prompt_eval_count`(小=有還原) |
| `~/.config/codetrail/deployment.json` 與 `~/start.sh` 的 main 參數 | 規劃 session 讀取被權限拒絕;採用使用者給的數字(main=deepseek-v4-flash-0731-ud-q8-k-xl、ctx=131072、batch=2048、ubatch=512、n_cpu_moe=40) | 施工者 `cat` 一次即可(設定檔,非私有 session) |

本 repo 自己的 `.codetrail/context_metrics.jsonl`(610 行)**全是測試假資料**(`model: test-model`、actual 欄位皆 null),不能當證據;真實數字只存在於 David 實際使用 `aicode` 的專案目錄裡,由 David 自己跑 T0 的腳本(輸出只有計數)。

## 2. 設計決策(含要 Astra / 使用者裁決的假設)

D1. **`Preflight.lines` 不變**(完整 transcript,測試與 gate 契約不動);新增 `warnings` / `status` 兩個分流清單與 `banner_lines()`;TUI 只用 `banner_lines()`。
D2. **成功後畫面保留三種東西**:一行身分摘要(model / n_ctx / tools / permission / compaction)、壓縮狀態行(§2 契約:停用警告是使用者唯一會看到的地方)、警告行(所有 stderr 輸出 + `warn()` 標記的 ⚠ 行)。**假設**:使用者說的「清掉」是指進度 LOG,不是指警告;若使用者要「一行都不留」,只需把 `banner_lines()` 改成只回警告與停用行——在最終計畫前請使用者確認。
D3. 失敗路徑不動:`PreflightError` 在 TUI 之前 exit 2,整段 transcript 留在終端。
D4. **預熱(prime)是唯一的產品級 TTFT 修正**,形狀是「用下一輪一模一樣的 payload 先送一次 `max_tokens=1`」。理由:它不改任何送模內容、不改 server、對 prefix 熱的情況成本 < 0.5 s、對冷的情況把整份 prefill 搬到打字之前。**收益上限 = 使用者打第一題的時間**;打字比預熱快就排隊,總時間不變、不變差。
D5. 預熱開關是 **repo 常數** `config.CLIENT_PRIME_PROMPT_CACHE = True`(AGENTS §3 允許 config 常數或 client.json 鍵;不加 client.json 鍵是為了避免 schema / `as_json()` / set_config 測試 / 文件的連動改動)。runtime 的四個跳過條件(readonly / headless、模型鎖被占、`/slots` 有 slot 在忙、context gate 拒絕)不依賴開關。
D6. **headless `run` 與 readonly 永不預熱**(結構上沒有呼叫點 + 契約測試):canary / session_eval / routing eval 的 telemetry 與 replay 不得多出一個請求。
D7. MCP 端 expansion 打掉 cache、壓縮摘要打掉 cache:**不改 MCP 端與壓縮 payload**(retrieval 品質與摘要契約是另案),改成「壓縮成功後在回合鎖外預熱一次」;`id_slot` 釘 slot 的方案要等 T0 的 `n_parallel`,先列 deferred。
D8. **不做** prefix 瘦身(工具描述是路由唯一真值、eval baseline 綁 catalog digest)——這是最大的槓桿但是另一個決策,列 deferred 並揭露。
D9. SSE 不改(1.2 已查證)。
D10. 狀態列分相(prefill / thinking / 回答中)與 `/status` 印 prefix 大小為選配 T5,零測試(UI 便利,不是契約)。

## 3. 驗收(明確、可由審核者離線核對;live 數字由 David 補)

### A. 自檢 LOG

- A1 `client_preflight.Preflight` 新增 `warnings: list[str]`、`status: list[str]`、`warn()`、`banner_lines(*, tools, permission, compaction) -> tuple[str, ...]`;`lines` 語意與內容逐字不變。
- A2 成功後 `banner_lines()` 恰好是:`("自檢通過:model=<m> n_ctx=<n> tools=<k> permission=<p> compaction=<mode>",) + tuple(status) + tuple(warnings)`;**不含**任何進度行(`root=`、`deployment profile=`、獨立的 `model=` / `n_ctx=` 行、`ctx safety=SAFE`、`lessons:… 已注入`、`[model-preflight] PASS`、`[tool-health] MCP PASS` / `MODEL PASS` / `IMPLICIT … optimal` / `headless run 仍在執行…`)。
- A3 `warnings` 至少涵蓋:preflight 期間寫到 **stderr** 的每一行(canary 的 `WARNING` / `MODEL RETRY` / `MODEL FLAKY` / `IMPLICIT WARN` 都只走 stderr,`tool_call_canary.py:167-168`)、`⚠ … lessons 已過 review_by`、`⚠ 無法移除舊的 lessons.md`、`⚠ 升級前啟動的網頁 backend 還在跑`、`ctx safety=UNKNOWN(…);放行`、`n_ctx=…(來自 deployment profile;server 尚無法觀測)`。
- A4 `status` = `compaction_status()` 產生的全部行(照舊也印進 transcript)。
- A5 `codetrail_chat.command_chat` 用 `checks.banner_lines(...)` 取代 `tuple(checks.lines) + (...)`;`CodeTrailApp` 的 `banner` 參數與 `on_mount` 顯示方式不變。
- A6 失敗路徑行為不變(exit 2、訊息在 stderr、TUI 未啟動)。
- A7 新增 smoke regression(紅→綠證據)並登記 `SAFETY_MODULES["test_client_preflight.py"]`:`test_the_tui_banner_keeps_warnings_and_compaction_status_but_drops_the_progress_log`;`tests/test_client_cli.py` 新增 `test_the_tui_banner_after_a_passing_preflight_is_the_filtered_set`(`command_chat` 端對端,用替身)。
- A8 文件同步:`README.md:706-707`、`docs/setup.md:248`、`client_preflight.py` 模組與 `_Tee` docstring、`tests/test_client_preflight.py` 模組 docstring、`tests/test_lessons.py:402-403` 註解。

### B. 首字延遲

- B1 `Engine.prime_prompt_cache(*, reason: str = "") -> bool`:送出**一個** `POST /v1/chat/completions`,`stream=False`,`messages` 逐字等於當下 `payload_messages()[0]`、`tools` 等於 `openai_tools()`、`tool_choice="auto"`、`temperature/top_p/top_k/min_p` 與 `_one_model_step` 相同、`extra={"max_tokens": 1}`、`cache_prompt` 為 True(由 `chat_completions` 硬編碼);回 True。
- B2 零寫入:`engine.messages`、session store、事件、`resumed_snapshot` 在呼叫前後逐字相同;不呼叫 `on_event`;不進 `_begin_turn`(協調器 `busy` 維持 False)。
- B3 跳過條件(全部回 False、不 raise、不送請求):`config.CLIENT_PRIME_PROMPT_CACHE` 為 False;工具未載入;`model_lock.acquire(blocking=False)` 失敗;`/slots` 回報任一 slot 忙碌(`state != 0` 或 `is_processing is True`;`/slots` 拿不到或非 200 視為未知,**不**擋);`context_budget.check_and_log(source="prime")` 丟 `ContextOverflowError`;HTTP 任何例外。
- B4 請求期間持有 `model_lock`,結束(含例外)一定釋放;既有的「等鎖可取消」契約(`test_a_turn_waiting_for_a_leased_model_lock_can_still_be_cancelled`)因此自然涵蓋預熱中的中斷。
- B5 telemetry:`source="prime"` 一行,只有計數(既有 `log_metrics` 形狀);readonly 本來就 `CTX_METRICS_ENABLED=False`,而且永不預熱。
- B6 呼叫點:TUI `on_mount`(重播之後)、`/new` 成功後、`/session` / `/resume` 成功切換後、自動或手動壓縮 **status == "compacted"** 之後在 **回合鎖外**;全部在背景 daemon thread、不碰 widget。`command_run` 沒有呼叫點。
- B7 契約測試(smoke,登記 SAFETY_MODULES):
  - `tests/test_client_engine.py::test_priming_sends_the_next_turns_exact_prefix_and_records_nothing`
  - `tests/test_client_engine.py::test_priming_is_skipped_when_the_model_lock_or_the_server_slot_is_busy_and_never_raises`
  - `tests/test_client_cli.py::test_headless_run_never_primes_the_prompt_cache`
  - `tests/test_client_turns.py::test_a_compaction_that_replaced_the_history_primes_outside_the_turn_lock`
  - `tests/test_client_app.py::test_the_tui_primes_on_mount_new_and_session_switch_but_never_while_busy`
- B8 `llama_client.get_slots(base_url, *, timeout=5, quiet=False)`:`quiet=True` 時失敗不印 stderr(Textual 接管畫面後不得有直接 stderr,AGENTS §2 client_app 條)。既有呼叫端不受影響。
- B9 誠實揭露:交付報告必須寫「reasoning 與硬體 prefill 成本未變;預熱只把冷 prefix 的 prefill 搬到打字之前;熱 prefix 時無感」。live 驗收(David 自願):冷啟動 `aicode` 後等狀態列預熱結束再送第一題,遙測第一列 `actual_prompt_eval_count` 應接近「使用者訊息 token + 模板尾巴」(< 數百),而不是接近 `estimated_input_tokens`。

## 4. 子任務、Dependency、可寫檔案、介面凍結

工作方式:每個 lane 在自己的 git worktree(`git worktree add ../wt-<lane> f200f69`)上做,交付 patch;整合者(Fable,最終計畫 / 修 Blocker 那一步)把 patch 套到單一工作樹、跑唯一一次交付 smoke、算 digest。共享檔只有一個 owner;不在自己清單裡的檔一律不碰。

### T0 量測(唯讀,零產品檔;執行者 David,或 David 同意後由審核者)

在**真實使用 aicode 的專案目錄**執行,輸出只有計數與時間:

```bash
# (1) 主 server 靜態事實(GET,零副作用)
curl -s http://localhost:8080/props | python3 -c '
import json,sys; p=json.load(sys.stdin); g=p.get("default_generation_settings",{})
print("n_ctx",g.get("n_ctx"),"n_parallel",p.get("n_parallel"),"n_batch",p.get("n_batch"),"n_ubatch",p.get("n_ubatch"))
print("build",p.get("build_info"))
t=p.get("chat_template") or ""
print("template_len",len(t),"renders_reasoning_content","reasoning_content" in t,"tools_idx",t.find("tools"),"system_idx",t.find("system"))'

# (2) 既有遙測:每個 client 請求真的算了幾個 prompt token(cache 命中就很小)
python3 - <<'EOF'
import json, pathlib
rows=[json.loads(l) for l in pathlib.Path(".codetrail/context_metrics.jsonl").read_text().splitlines() if l.strip()]
rows=[r for r in rows if r.get("source") in ("client","compaction","prime") and r.get("actual_prompt_eval_count") is not None]
for r in rows[-60:]:
    n=r["actual_prompt_eval_count"]; tps=r.get("prompt_tokens_per_second") or 0
    print(f'{r["timestamp"]:.0f} {r["source"]:10s} msgs={r["message_count"]:3d} est_in={r["estimated_input_tokens"]:6d} prompt_n={n:6d} prefill_s={(n/tps if tps else 0):6.1f} out={r.get("actual_eval_count")}')
EOF

# (3) prefix 穩定性:同一專案跑兩次 digest 必須相同
PYTHONPATH=/home/david/CodeTrail python3 -c 'import client_prompt as c; p=c.build_system_prompt("."); print(p.chars, p.digest)'
```

判讀規則:`msgs=2` 的列是每段對話第一題;`prompt_n ≈ est_in` = 冷 prefix,`prompt_n ≪ est_in` = 命中。`query_knowledge` 之後那一步若 `prompt_n` 仍小 → build 有 host prompt cache 還原,D7 的 deferred 項目可以直接關閉。`n_parallel > 1` 才需要評估 `id_slot`。T0 不擋 T1–T6 開工;它決定 deferred 清單與最終報告的數字。

### Lane A:T1 + T2(同一位施工者,序列)

- **T1 `client_preflight.py`**(owner)+ `tests/test_client_preflight.py`(owner)
  - 介面(凍結):
    ```python
    @dataclass
    class Preflight:
        root: Path
        model: str = ""
        n_ctx: int = 0
        lines: list[str] = field(default_factory=list)      # 完整 transcript,不變
        warnings: list[str] = field(default_factory=list)   # 成功後仍要進 TUI 的行
        status: list[str] = field(default_factory=list)     # 壓縮狀態行
        def note(self, message: str) -> None                # 不變(INFO)
        def warn(self, message: str) -> None                # print("[aicode] ⚠ " + message) 且 append warnings
        def banner_lines(self, *, tools: int, permission: str, compaction: str) -> tuple[str, ...]
    ```
  - `_Tee(stream, sink, mirror: list[str] | None = None)`:stderr 的 tee 多一個 `mirror`;`run()` 結束時 `result.warnings.extend("".join(stderr_mirror).splitlines())`(空行略過)。
  - 改用 `warn()` 的呼叫點:`check_ctx_safety` UNKNOWN(197)、`observe_n_ctx` fallback(181)、`render_lessons` 過期(266-270)、`_drop_rendered` 失敗(281)、`legacy_web_backend_hint` 的回傳(401-402)。`compaction_status` 的每一行 `note()` 之外同時 `status.append()`。其餘 `note()` 不動。
  - 測試(先紅後綠):`test_the_tui_banner_keeps_warnings_and_compaction_status_but_drops_the_progress_log`——沿用 251-286 的 monkeypatch 樣式:fake `check_tool_health` 印一行 stdout `MODEL PASS — cached` 與一行 stderr `WARNING — implicit 診斷降級`,跑 `run()`,斷言 `banner_lines(tools=19, permission="interactive", compaction="manual")` 含 `WARNING…`、含 `壓縮模式`、首行以 `自檢通過:` 開頭,且不含 `root=`、`deployment profile=`、`MODEL PASS`;同時斷言 `result.lines` 仍含 `MODEL PASS`(既有契約)。
  - 交付清單要列出新增 node 名,整合者登記進 `SAFETY_MODULES`。
- **T2 `codetrail_chat.py`**(owner)+ `tests/test_client_cli.py`(owner)
  - `command_chat`:`banner = checks.banner_lines(tools=len(engine.tool_specs), permission=engine.options.policy.name, compaction=compactor.mode)`。
  - 測試 `test_the_tui_banner_after_a_passing_preflight_is_the_filtered_set`:monkeypatch `_has_tty`→True、`_resolve_root`、`_settings`(回 `ClientSettings(path=…)`)、`client_config.apply_to_config`→no-op、`client_preflight.run`→回預填 `lines/warnings/status` 的 `Preflight`、`_build`→(fake mcp with `close()`, fake engine with `tool_specs`/`options.policy.name`)、`_compactor`→`SimpleNamespace(mode="manual")`、`client_app.CodeTrailApp`→擷取 `banner` 的假類別(`run()` 回 0)。斷言 banner 逐項等於 A2。
  - 測試 `test_headless_run_never_primes_the_prompt_cache`:用既有 `_engine(tmp_path)` 樣式,monkeypatch `client_engine.Engine.prime_prompt_cache` 為會 raise 的函式(或計數器),跑 `command_run` 的替身路徑(monkeypatch `_build` 回該 engine),斷言從未被呼叫。此測試在 T3 之前以 `getattr(Engine, "prime_prompt_cache", None)` 形式寫,T3 合併後照樣成立。
  - 依賴:T1 的介面(同一人,序列)。

### Lane B:T3 `client_engine.py` + `config.py` + `llama_client.py`

- owner:`client_engine.py`、`config.py`(只加一個常數與註解)、`llama_client.py`(只加 `get_slots(..., quiet=False)`)、`tests/test_client_engine.py`。
- 介面(凍結):
  ```python
  # config.py(放在 CLIENT_MAX_OUTPUT_TOKENS 附近)
  CLIENT_PRIME_PROMPT_CACHE = True  # TUI 就緒 / 換 session / 壓縮後,用下一輪同一份 payload 送 max_tokens=1 預熱 llama-server 的 prompt cache

  # client_engine.py
  PRIME_MAX_TOKENS = 1
  PRIME_SOURCE = "prime"
  class Engine:
      def prime_prompt_cache(self, *, reason: str = "") -> bool: ...
  ```
- 實作骨架(規範,不是逐字):
  1. `config.CLIENT_PRIME_PROMPT_CACHE` 假或 `not self._loaded_tools` → False。
  2. `self.model_lock.acquire(blocking=False)` 失敗 → False。`try/finally` 釋放。
  3. `llama_client.get_slots(self.options.base_url, timeout=2, quiet=True)`;任一 slot `state`(int)≠0 或 `is_processing is True` → False;None / 非 list → 繼續。
  4. `payload, transform = self.payload_messages()`;`context_budget.check_and_log(source=PRIME_SOURCE, requested_num_ctx=self.options.n_ctx, messages=payload, tools=self._openai_tools, model=self.options.model, reserved_output_tokens=self.options.max_output_tokens, did_trim=bool(transform), trim_summary=transform, emit=False)`;overflow → False。保留額**刻意**用下一輪真正的 `max_output_tokens` 而不是 1:閘要跟真回合一致(真回合會被拒的 prefix,預熱沒有意義);docstring 要寫明,免得被「修」成 1。
  5. `llama_client.chat_completions(base_url=…, messages=payload, model=…, temperature=self.options.temperature, top_p=config.CHAT_TOP_P, top_k=config.CHAT_TOP_K, min_p=config.CHAT_MIN_P, tools=self._openai_tools, tool_choice="auto", stream=False, extra={"max_tokens": PRIME_MAX_TOKENS}, timeout=self.options.request_timeout)`。
  6. `context_budget.parse_usage_from_response(data, usage); context_budget.log_metrics(usage)`;回 True。任何例外 → False(不 raise、不 print)。
- 不碰:`_begin_turn` / `_cancel` / `_record` / 事件。
- 測試(smoke;先紅後綠):
  - `test_priming_sends_the_next_turns_exact_prefix_and_records_nothing`:用 `engine_factory`;monkeypatch `llama_client.chat_completions` 擷取 kwargs 並回 `{"choices":[{"message":{"content":""},"finish_reason":"length"}],"timings":{"prompt_n":7,"prompt_per_second":100.0}}`;monkeypatch `llama_client.get_slots` 回 `[]`;先 `send("hi")`(用 `_stream(_text_chunk("ok", finish="stop"))`)再 prime;斷言 prime 的 `messages == engine.payload_messages()[0]`、`tools == engine.openai_tools()`、`tool_choice == "auto"`、`stream is False`、`extra == {"max_tokens": 1}`、`temperature/top_p/top_k/min_p` 與 `send` 時擷取的相同;`engine.messages` 前後相等、store 未被 append(用會計數的 store 替身)、回 True;再斷言 `config.CLIENT_PRIME_PROMPT_CACHE=False` 時回 False 且不呼叫 HTTP。
  - `test_priming_is_skipped_when_the_model_lock_or_the_server_slot_is_busy_and_never_raises`:先 `engine.model_lock.acquire()` → prime 回 False 且未呼叫 HTTP;釋放;`get_slots` 回 `[{"id":0,"is_processing":True}]` → False;`get_slots` 回 `[{"id":0,"state":1}]` → False;`get_slots` 回 None → 仍送;`chat_completions` raise → False 且鎖已釋放(`model_lock.acquire(blocking=False)` 成立後再釋放)。
- 依賴:無。

### Lane C:T4 `client_app.py` + `client_turns.py`(+ 選配 T5)

- owner:`client_app.py`、`client_turns.py`、`tests/test_client_app.py`、`tests/test_client_turns.py`。
- 對 engine 只用 `getattr(self.engine, "prime_prompt_cache", None)`(替身沒有就跳過),所以**不依賴 T3 的實作**,只依賴介面名。
- `client_app.CodeTrailApp`:
  - `_prime(reason: str)`:`if self.coordinator.busy: return`;起 daemon thread 呼叫 `prime(reason=reason)`;thread 內只允許經 `_from_worker` 更新一個布林 `self._priming`(給狀態列顯示「prompt cache 預熱中」),不碰其他 widget。
  - 呼叫點:`on_mount` 在 `_replay_startup_session()` 之後;`_cmd_new` 在 `_recount_context()` 之前;`_switch_session` 在 `_recount_context()` 之前。
- `client_turns.TurnCoordinator`:`_run_turn` 與 `_run_compaction` 記下 `compacted = outcome.status == "compacted"`;`finally: self.finish_turn()` **之後**(鎖外)`if compacted: self._spawn(lambda: prime(reason="compaction"), f"codetrail-prime-{target}")`。`_auto_compact` 改為回傳 outcome(或 None)。
- 測試(smoke;先紅後綠):
  - `tests/test_client_app.py::test_the_tui_primes_on_mount_new_and_session_switch_but_never_while_busy`:`_Engine` 替身加 `prime_prompt_cache(reason)` 記錄 reasons 並回 True;mount → 含 `"mount"`;`/new` → 含 `"new"`;`/session <id>` → 含 `"session"`;回合進行中(用既有的慢 engine 樣式)`_prime("x")` 不得呼叫。
  - `tests/test_client_turns.py::test_a_compaction_that_replaced_the_history_primes_outside_the_turn_lock`:compactor 替身回 `CompactionOutcome("compacted", "", "已壓縮")`,engine 替身的 `prime_prompt_cache` 記錄「被呼叫時 `coordinator.busy` 是否為 False」並用 Event 等待;斷言呼叫發生、且發生在 `finish_turn` 之後(`busy` False);`status="skipped"` 時不呼叫。
- 選配 T5(同 owner,零測試):狀態列相位——`_turn_started` 後在第一個 reasoning/text token 之前顯示 `等待首個 token`,收到 reasoning token 起顯示 `thinking N tok`(累計 `_on_reasoning` 次數),第一個 content token 起顯示 `回答中`,terminal `step_finish` 清除;`/status` 多一行 `prefix≈{tokens} tokens(system {chars} 字元、tools {chars} 字元)`,用 `context_budget.estimate_tokens(messages=[{"role":"system","content":self.engine.system_prompt.text}], tools=self.engine.openai_tools())` 與 `len(json.dumps(tools, ensure_ascii=False))`。格式只**追加**,既有 `status_text` 斷言不受影響。

### Lane D:T6 文件 + AGENTS.md §2 + README_DEV

- owner:`README.md`、`docs/setup.md`、`docs/troubleshooting.md`、`README_DEV.md`、`AGENTS.md`(只在 §2 的 client_app / client_engine 條各加一句:banner 過濾不得丟掉 stderr 警告與壓縮狀態行;prime 零寫入、只送下一輪同一份 payload、headless / readonly 永不)。
- 內容:
  - `README.md:706-707` 改為「成功時對話區只留一行摘要、壓縮狀態行與警告;完整自檢輸出留在 TUI 之前的終端畫面;失敗時 aicode 不會進入 TUI,錯誤原樣留在終端」。
  - `docs/setup.md:248` 同上。
  - `docs/troubleshooting.md` 在「MoE 模型第一次對話 TTFT」節之後加一小節「開新對話首字慢:先分辨 prefill、reasoning 與 cache 冷熱」:寫明客戶端每步一個請求、`cache_prompt` 永遠開、prefix 由哪五段 + 工具 schema 組成、什麼事件會讓 prefix 冷、預熱做什麼與不做什麼、T0 的遙測讀法(`actual_prompt_eval_count` 語意)、`show_reasoning` / `/thinking` 只影響顯示。不得出現任何環境變數。
  - `README_DEV.md`:模組表加 `Engine.prime_prompt_cache` 與 `CLIENT_PRIME_PROMPT_CACHE`;加「SSE 客戶端 buffering 已查證不是首字延遲來源(urllib3 `read_chunked` 逐 chunk 回傳)」一段,避免重查。
- 依賴:最終措辭等 Lane A–C 收斂;可先起草。`python3 scripts/check_readme_consistency.py` 與 `python3 -m compileall -q .` 是允許的靜態檢查(不收集 pytest)。

### 整合者(Fable,最終計畫與修 Blocker 那一步)

- owner:`tests/test_smoke_gate.py`(登記 A7 / B7 的 node 到既有檔名鍵下;不新增檔名鍵)、`docs/workflows/startup-ttft-20260907/*.md`。
- 套 patch → `python3 -m compileall -q .` → `python3 scripts/check_readme_consistency.py` → 唯一一次 `python3 scripts/run_tests.py -m smoke` → 記錄 `git rev-parse HEAD`(仍是 `f200f69`)、`git status --short`、`git diff --stat`、`git diff | sha256sum`(產品內容 digest)。審核者與 full 一律對同一 digest。

## 5. 平行工作規則與紅燈門檻

1. 每個 lane 開工第一步是寫自己的 regression / 契約 node,並在**未改產品**的 worktree 上單跑該 node(`python3 scripts/run_tests.py tests/<file>::<node>`),把紅燈節錄貼進 lane 交付;沒有紅燈證據的產品修正一律視為未驗證(AGENTS §1.3)。
2. 只准跑:自己的新 node(紅、綠各一次)+ 交付前一次 `-m smoke`。禁止跑 full、禁止跑別人的檔、禁止任何會打到 8080 的命令(prime 的測試全部 monkeypatch `llama_client`)。
3. `0 tests collected`(exit 5)不是通過。
4. 共享檔零例外:`tests/test_smoke_gate.py` 只有整合者改;`client_app.py` 只有 Lane C;`codetrail_chat.py` 只有 Lane A。
5. 不新增 `os.environ` 讀取、不新增 spawn 出口(`process_env` 以外)、不新增 Node / npm。
6. 動到既有測試要逐條列出(檔、node、理由);本計畫預期**不改任何既有斷言**;唯一動到既有測試檔的是新增 node 與 `tests/test_client_preflight.py` / `tests/test_lessons.py` 的 docstring / 註解措辭。

## 6. 既有呼叫端相容性

- `client_preflight.run(root, *, skip_tool_health=False) -> Preflight`:簽名與 `lines` 不變;`tests/test_lessons.py:399-407`、`tests/test_deployment.py`、`tests/test_aicode.py` 直接建 `Preflight(root=…)` 並呼叫 `render_lessons` / `note` 的用法不變(新欄位有預設值)。
- `CodeTrailApp(engine, *, compactor, banner, state_dir, show_reasoning, keep_historical_reasoning)` 不變;`tests/test_client_app.py` 以 `banner=("root=/tmp",)` 斷言 banner 會顯示(`723`)——仍成立,因為顯示邏輯不變,只是 `command_chat` 交的內容變了。
- `Engine` 只新增方法;`EngineOptions` 不變;`client_compaction.EngineLike` protocol 不變。
- `llama_client.get_slots` 新增 keyword-only 參數,預設行為不變;`gpu_safety.runtime_offload_check` 不動。
- headless `run`、canary、session_eval、routing eval:零行為改變(沒有 prime 呼叫點;banner 只在 `command_chat`)。
- 舊的 `aicode` 使用者:唯一可見差異是畫面第一屏變短、狀態列多一段相位文字、開新對話第一題(冷 prefix 時)較快。

## 7. 隱私 / 取消 / 鎖 / 輸出安全邊界

- **隱私**:預熱送的是與下一輪完全相同的 payload,走同一台 loopback server 與同一個 `endpoint_policy`(`llama_client._ensure_allowed`);沒有新的資料出口。telemetry 只有計數。banner 過濾只是少顯示給同一位使用者。T0 腳本輸出只有時間與計數,不印任何 prompt / 內容;不讀任何 session JSONL。
- **取消**:預熱不是 turn,不進 `_begin_turn`,`TurnCoordinator.busy` 不變;使用者在預熱期間送出的回合會在 `_ModelSlot.__enter__` 等鎖,而那條路已可被 Ctrl-C 中斷(既有契約)。預熱本身不可取消:最壞情況是 server 多算一次本來就要算的 prefill(使用者秒退時)。這條要寫進 docstring 與 troubleshooting。
- **鎖**:預熱只用非阻塞 acquire,拿不到就放棄;拿到後 `finally` 一定放;不與 `_turn_state` 互動。
- **輸出安全**:Textual 接管後零 stdout / stderr——`get_slots(quiet=True)`、`check_and_log(emit=False)`、不 print。
- **共用 server**:`/slots` 忙碌就跳過,把對另一個客戶端(本機 `~/ai_opencode`)的干擾限制在「它的 cache 可能被換掉」,與任何一輪對話相同。
- **readonly / eval 邊界**:結構上沒有呼叫點,並有契約測試(B7)。
- **AGENTS §2 檢查點**:本次不削弱任何一條;新增兩條(banner 不丟警告 / 停用行;prime 零寫入)並登記。

## 8. 測試分工與執行權責

| 誰 | 跑什麼 | 何時 |
|---|---|---|
| Lane A/B/C 施工者(Opus 5.0) | 自己的新 node 紅 → 綠;交付前一次 `-m smoke`(在自己的 worktree) | 施工中 / 交付前 |
| 整合者(Fable) | 套 patch 後 compile + readme consistency + 唯一一次 `-m smoke`;記 digest | 整合後 |
| 審核者(Astra,ROLE=REVIEWER) | 靜態審核集中提 Blocker;Blocker 歸零後對**凍結**的產品 digest 執行一次 `python3 scripts/run_tests.py`(full);回報命令、結果、HEAD(`f200f69`)+ 產品 digest;失敗 node 集合不得大於基線(缺 tty / llama-server 的環境相依失敗屬基線);`0 collected` 必報異常 | 收斂後 |
| David | T0 量測(唯讀)與 B9 的 live 驗收(自願) | 任何時候 / 合併後 |

Blocker 定義(依使用者):本次變更新造成的測試紅、違反本計畫的驗收或凍結介面、違反 AGENTS.md。無關問題另記 `deferred`;分歧兩輪以上可擱置但要揭露。

## 9. 交接與狀態誠實

- 產品**不 commit**;每一輪報告寫 `base HEAD = f200f69` + `git diff | sha256sum` + `git diff --stat`,不得說「HEAD 含修正」。
- 交接檔:`01-plan-initial.md`(本檔)→ `02-review-astra.md` → `03-plan-final.md` → `04-impl-<lane>.md` → `05-review-astra-rN.md` / `06-fix-rN.md` → `07-deferred.md`。只有這些 markdown 以 `git add -f docs/workflows/startup-ttft-20260907/` 提交(使用者已授權)。
- 不動 live 部署、不重啟任何常駐服務、不對 8080 發任何測試請求(T0 的 GET 除外,且由 David 執行)。

## 10. Deferred(揭露,不在本次範圍)

1. **prefix 瘦身**(工具描述 / schema 預算、AGENTS.md 注入上限):最大的 TTFT 槓桿,但改的是路由唯一真值與 eval catalog digest(`tests/test_evals.py`、`eval/fixtures/tool_routing/*`),另案。
2. **`id_slot` 釘 slot**(聊天用 slot 0、MCP 端 `/completion` 與壓縮摘要用另一個 slot):只有 T0 量到 `n_parallel > 1` 才值得;`-np` 會把 `-c` 平分給各 slot,屬部署決策。
3. **KB 端 `USE_QUERY_EXPANSION` / multi-query 預設**:每次 `query_knowledge` 多 1–2 個主模型呼叫並打掉聊天 prefix;關掉是 retrieval 品質決策(eval baseline),另案;T0 若顯示 host prompt cache 會還原 prefix,成本只剩那一兩次短呼叫本身。
4. **壓縮摘要請求換 slot / 帶同一份 system prompt**:摘要契約(`docs/compaction-rules.md`)另案;本次以「壓縮後預熱」補回。
5. `gpu_safety.runtime_offload_check` 只認舊的 `/slots` `state` 欄位,新 build 的 `is_processing` 會被算成 idle:與本次無關,記給 doctor 維護。
6. `keep_historical_reasoning=True` 可換得跨回合更多 cache 命中(若模板 render 它),代價是 context 成長:使用者自己的 client.json 選擇,不改預設。
7. deployment 調參(`ubatch` / `n_cpu_moe` 對 prefill 吞吐的影響):不可更改 live 部署,另案且要 David 決定。

## 11. 給 Astra 的審核重點(只審 Blocker)

1. A2 / A3 的集合是否精確:任何 stderr 行或壓縮狀態行被過濾掉就是 Blocker(§2 契約);任何進度行殘留是驗收失敗。
2. `Preflight.lines` 是否逐字不變(既有 SAFETY node 綠)。
3. B1 的 payload 等價是否由**測試**釘住(比對 `payload_messages()` 與 `openai_tools()`,不是比對常數字串)。
4. B2 / B3 / B4:零寫入、非阻塞鎖、`finally` 釋放、不 raise、不 print。
5. `command_run` 零呼叫點 + `test_headless_run_never_primes_the_prompt_cache`。
6. `SAFETY_MODULES` 有登記 A7 / B7 且 node 存在、帶 smoke。
7. 沒有新的 `os.environ` 讀取、沒有 `process_env` 以外的 spawn、文件沒有教任何環境變數。
8. 交付報告的措辭:不得宣稱消除 reasoning / 硬體 prefill 成本;數字必須來自 T0 或標明「未量測」。
