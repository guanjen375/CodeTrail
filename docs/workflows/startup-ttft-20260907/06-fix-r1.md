# Step 5 R1 回修 — 整合報告(Fable 5.1 MAX 整合者)

- 整合者:Claude Fable 5.1(harness 自報 model ID `claude-fable-5-1`;effort 由 root 的 argv 記進 `00-model-log.md`)。角色 = 整合者:只做 §6(`06-fix-r1-dependencies.md`)列的登記、文件字句同步、三件套與兩份交接;**沒有**做任何產品設計判斷、**沒有**改任何 engine / transport / context / TUI 產品或其測試 body。
- 日期:2026-09-07。cwd `/home/david/CodeTrail`,主工作樹。
- 輸入:`AGENTS.md`、`06-fix-r1-dependencies.md`、`06-fix-r1-engine.md`、`06-fix-r1-status.md`、`05-review-astra-r1.md`、`03-plan-final.md` §7 / §10、`04-integration.md`(只當前版 smoke 紅燈的證據,不當本版已通過)、`/tmp/codetrail-startup-ttft-20260907/` 的八個紅 / 綠 log 與 `r1-memory-cleanup.json`。
- 產品 base HEAD `f200f697ba54d38a102e8ef66dead652c4002e5f`;動工與交付時 `git rev-parse HEAD` 都是 `b25a551c0c3ca57b4f34035400bca04d7e516778`(只有交接 markdown;**產品未 commit,HEAD 不含任何修正**)。
- 沒有對 :8080–:8083 發任何請求、沒有讀私人 session、沒有動 live deployment、沒有新增 `os.environ` 讀取、沒有 `process_env` 以外的 spawn、沒有 `git commit` / `push` / `reset` / `checkout` / `stash`、沒有覆寫 `00-model-log.md` 與 01–05 / 兩份 lane 報告 / dependencies。
- **零測試執行**:沒有跑 smoke、full、任何單 node、任何會間接收集 pytest 的命令。本檔引用的紅 / 綠證據全部是兩個 lane 已經留下的檔案(§3),我只讀、只核對,不補跑基線(`04-integration.md` §5 的建議不是本輪授權)。

## 0. 一句話結論

五個 Blocker 的產品修正由兩個 Fable lane 完成並已凍結進 index(digest `16bda189…`,§7);其中 **R1-B02 有一個子情況(POST 已送出、headers 未到)未滿足 B7「1 秒內鎖已放」的字面條件**,lead 已明示交 Astra 裁定,本檔**不把它當已關**、也**不把它當已擱置**(目前只是第一次回修,未達使用者「分歧超過兩輪」的擱置門檻;見 §1 與 `07-deferred.md` §B)。smoke / full 本輪一次都沒跑;Astra 靜態收斂後對本 digest 執行 full。

## 1. 五個 Blocker 對照(處理 / 仍待審)

| Blocker | lane | 修法(摘要;細節在 lane 報告) | 紅 / 綠證據 | 本輪狀態 | 仍待 Astra |
|---|---|---|---|---|---|
| **R1-B01** quiet 契約測試仍呼叫 stub | engine | `test_priming_skips_only_when_no_slot_is_idle_and_never_raises_or_prints` 開頭捕捉真的 `llama_client.get_slots`,末段替換 `get_session` 之前把它還原,兩次 probe 真的走產品函式、只 mock 底層 session;兩側斷言一字未動 | **無**(測試缺陷,不是產品 bug;不新增 node;依 §1.2 未單跑) | 已修測試、**未執行** | full 時確認這條轉綠 |
| **R1-B02** 取得 stream 前無法中止舊預熱 | engine | `/slots` GET 與 POST 各在小執行緒(`_Abandonable`)跑、主流程每 50 ms 看**預熱自己的** abort Event;每個邊界比世代號 `_prime_epoch`;`new_session()` / `adopt()` 的狀態切換收進 `_switch_session()`(與 snapshot 同臨界區);中止落在 probe → 立刻 `aborted` 放鎖;probe 後 / gate 後 → 不發 POST;串流中 → 關 socket;**POST 已送出、headers 未到 → `aborted`、`priming=False`,但模型鎖 `hand_off` 給該請求,response 到了關掉才 `release_late()`** | `engine-b02-abort-no-stream-{red,green}.txt`(§3.3) | 已修產品,一紅一綠 | **是**:POST 在飛的子情況「鎖已放」的時間 = 舊請求 headers 到達並被關掉的時間,**不滿足** B7「1 秒內…鎖已放」的字面條件。lead 的立場:提早放鎖正是 Astra R1-B02 明訂不得做的事,兩者不可能同時成立,這是 B7 在該子情況下唯一合規的讀法。**本檔不裁定、不改 plan 驗收、不列為擱置**;請 Astra 判定它是「B7 的合規讀法」還是「偏離」。若判偏離 → R2 回修(不是擱置) |
| **R1-B03** 部分完成的工具群組,預熱與真 send 的 prefix 順序不同 | engine | `heal_in_place()` 把補的「已中斷」結果放在**該群組既有結果之後**(宣告順序不變、反向插入),與 `send()` 內 `heal_pending_tool_calls()` 的 append 順序一致;`payload_messages()` / `_prefix_from()` / `prune_for_summary()` 三者順序一致;原始歷史 / store / transcript 不改寫 | `engine-b03-partial-group-{red,green}.txt`(§3.1) | 已修產品,一紅一綠 | 既有兩條 heal node(群組無既有結果,插入位置不變)未重跑 → full |
| **R1-B04** 壓縮後預熱結果遺失,`/status` 停在上一筆 sent | status | `TurnCoordinator.__init__` 新 keyword `on_prime(reason, outcome)`;背景執行緒在 engine 回來(或 raise → None)後**先** `on_prime` **再** `on_done`,各自 `except BaseException`;`prime_in_background` 簽名不變;`CodeTrailApp` 以 `on_prime=self._prime_from_worker` 建協調器、`_last_prime` 改三元組 `(時間, 觸發點, outcome)`、`_prime()` 不再傳 `on_done`、`_prime_status()` 三種結果都帶 `觸發=` | `b04-{red,green}.txt`(§3.4) | 已修產品,一紅一綠;契約 node `test_every_prime_the_coordinator_runs_reports_through_on_prime` **只寫未跑** | **是**(揭露,不是 Blocker 主體):`_last_prime` 記的是**最後落地**的那一次,不是最後排程的;中止逾時、舊預熱晚於新預熱結束時 `/status` 會短暫顯示舊的 `skipped(aborted)`(status 報告 §9-2;交接 §5.2 的三元組形狀本來就不帶序號)。請 Astra 判定是否可接受 |
| **R1-B05** 沒有終結 chunk 的 clean EOF 記成成功 | engine | 預熱迴圈用 `_prime_chunk_is_final()`(與 `parse_usage_from_stream_chunk` 同判準)記錄終結 chunk;沒看到 → `PrimeOutcome(False, "incomplete", None)`;看到但 `usage.prompt_tokens_processed is None` → `PrimeOutcome(False, "no_timings", None)`;兩者都不 `log_metrics`、不 raise、不 print;`usage.prompt_tokens` 不代填;正常 `finish_reason=length` + timings 照舊 sent | `engine-b05-terminal-chunk-{red,green}.txt`(§3.2) | 已修產品,一紅一綠 | — |

補充:`04-impl-c.md` §9 第 2 點「壓縮後那一次不會更新 `/status`」自本輪起不成立(B04 已修);歷史文件不改,只在此註明。

## 2. `tests/test_smoke_gate.py` 登記(整合者唯一動到的測試檔)

依兩份 lane 報告(engine §3.4、status §4),在**既有檔名鍵**下追加 5 條 node,**沒有新增檔名鍵、沒有刪改任何既有 node**:

| 檔名鍵 | 追加的 node | 來源 |
|---|---|---|
| `test_client_engine.py` | `test_priming_matches_the_next_send_when_a_tool_group_is_only_partly_answered`(B03)、`test_priming_only_counts_as_sent_after_a_terminal_chunk_with_timings`(B05)、`test_a_session_switch_aborts_a_prime_that_has_no_stream_yet_and_never_sends_after_the_abort`(B02) | engine lane |
| `test_client_turns.py` | `test_every_prime_the_coordinator_runs_reports_through_on_prime`(B04 契約) | status lane |
| `test_client_app.py` | `test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction`(B04 regression) | status lane |

同時只**追加**三個鍵的說明字串(既有文字一字未刪):engine 鍵補「中止涵蓋 probe / 取得 headers 前 / 串流中且中止後不再發 POST(POST 在飛時鎖跟著請求走、不得提早放)、部分完成群組的預熱 prefix == 下一輪真 `send()` 減 user、只有終結 chunk + `timings.prompt_n` 才記成 sent」;turns 鍵補「每一次預熱經 `on_prime` 回報、先 `on_prime` 再 `on_done`、engine raise 時 outcome 是 None、回呼例外不冒出不互相帶走」;app 鍵補「`/status` 反映最近一次預熱(含壓縮後)的結果 / 原因 / 時間 / 觸發點」。

靜態核對(不執行 gate):5 個函式名都存在於工作樹的測試檔(`tests/test_client_engine.py` 第 2593 / 2647 / 2689 行、`tests/test_client_turns.py` 第 728 行、`tests/test_client_app.py` 第 1469 行);三個檔都有 module 層 `pytestmark = pytest.mark.smoke`(engine 第 41 行、app 第 35 行、turns 第 31 行),engine 的三條另帶逐條 `@pytest.mark.smoke`。**5 條都在 smoke 包裡。** 名稱在各鍵內唯一(gate 的重複檢查用 AST 掃原始碼)。`test_smoke_gate.py` 自身的四條測試本輪**沒有執行**。

## 3. 新 regression 的紅 / 綠證據(兩個 lane 執行;整合者只核對檔案)

命令形狀:兩個 lane 都無法用交接 §3 的「shell 變數 + `exit $VAR`」(沙箱拒絕 `$` 展開),改成純 redirect,exit code 由 harness 回報後追記到檔尾(engine:`exit=N (reported by the harness for the command above)`;status:另一個 `echo "exit=N" >>`)。**檔尾那一行是手寫 / 追記的,不是 shell 變數**;檔內其餘內容是 runner 的完整 stdout+stderr(含 `[run_tests] PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 … -m pytest <node>` 那一行)。

### 3.1 B03 — `tests/test_client_engine.py::test_priming_matches_the_next_send_when_a_tool_group_is_only_partly_answered`

```bash
python3 scripts/run_tests.py "tests/test_client_engine.py::test_priming_matches_the_next_send_when_a_tool_group_is_only_partly_answered" > /tmp/codetrail-startup-ttft-20260907/engine-b03-partial-group-red.txt 2>&1
```

紅(`engine-b03-partial-group-red.txt`,exit 1,`1 failed in 0.31s`):

```text
tests/test_client_engine.py:2632: in test_priming_matches_the_next_send_when_a_tool_group_is_only_partly_answered
    assert turn["messages"][:-1] == prime["messages"]
E   AssertionError: assert [{'role': 'sy...不要假設它已經執行過。'}] == [{'role': 'sy...ok\n(a 的結果)'}]
E     At index 3 diff: {'role': 'tool', 'tool_call_id': 'a', 'name': 'list_dir', 'content': 'status: ok\n(a 的結果)'} != {'role': 'tool', 'tool_call_id': 'b', 'name': 'read_file', 'content': 'status: error\n這次呼叫被中斷,沒有結果。\nnext: 需要的話重新呼叫一次;不要假設它已經執行過。'}
```

綠(`engine-b03-partial-group-green.txt`,exit 0):`1 passed in 0.29s`。紅燈是行為紅燈(index 3 真的是 `tool(b)` 排在 `tool(a)` 之前),與 Astra R1-B03 的推演一致。

### 3.2 B05 — `tests/test_client_engine.py::test_priming_only_counts_as_sent_after_a_terminal_chunk_with_timings`

紅(`engine-b05-terminal-chunk-red.txt`,exit 1,`1 failed in 0.23s`):

```text
tests/test_client_engine.py:2669: in test_priming_only_counts_as_sent_after_a_terminal_chunk_with_timings
    assert outcome == client_engine.PrimeOutcome(False, reason, None), (name, outcome)
E   AssertionError: ('empty', PrimeOutcome(sent=True, reason='', processed_tokens=None))
E     Differing attributes:
E     ['sent', 'reason']
```

綠(`engine-b05-terminal-chunk-green.txt`,exit 0):`1 passed in 0.21s`。覆蓋:空串流 / 非終結 delta 後 EOF / 終結但無 timings(帶 `usage.prompt_tokens=20000`)→ `incomplete` / `incomplete` / `no_timings`、零 `prime` 列、鎖已放、`priming` False;正常 `finish_reason=length` + timings → `sent`、一列、`prompt_tokens_processed=9`;capsys 空。

### 3.3 B02 — `tests/test_client_engine.py::test_a_session_switch_aborts_a_prime_that_has_no_stream_yet_and_never_sends_after_the_abort`

紅(`engine-b02-abort-no-stream-red.txt`,exit 1,`1 failed in 2.25s`):

```text
tests/test_client_engine.py:2726: in test_a_session_switch_aborts_a_prime_that_has_no_stream_yet_and_never_sends_after_the_abort
    assert not primer.is_alive()
E   assert not True
E    +  where True = is_alive()
E    +    where is_alive = <Thread(Thread-1 (<lambda>), started daemon 127916463789760)>.is_alive
```

(未修的產品:`new_session()` 裡的 `abort_prime()` 沒有東西可關、等滿 1 秒回 False,舊預熱仍卡在 probe。)綠(`engine-b02-abort-no-stream-green.txt`,exit 0):`1 passed in 0.32s`。

覆蓋(受控 barrier、全部離線替身):(1) 中止落在 probe → `new_session()` 後 1 秒內 `aborted`、`priming` False、鎖可取、放開 GET 後小執行緒全結束、**零 POST**;(2) 中止落在 gate 之後、POST 之前 → `aborted`、零 POST;(3) 中止落在 POST 已送出、headers 未到 → `adopt()` 後 1 秒內 `aborted`、`priming` False,**但 `model_lock.acquire(blocking=False)` 是 False**(鎖跟著請求走),放開 POST 後鎖在 1 秒內可取、串流被關恰好一次、POST 只有那一次;(4) 換過去之後的預熱照常准入、送的是新歷史;被中止的三次零 telemetry。**注意 (3) 的斷言本身就釘住了「鎖不提早放」——這就是 §1 B02 待 Astra 裁定的那一點,測試沒有把它藏起來。**

### 3.4 B04 — `tests/test_client_app.py::test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction`

```bash
python3 scripts/run_tests.py "tests/test_client_app.py::test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction" > /tmp/codetrail-startup-ttft-20260907/b04-red.txt 2>&1
echo "exit=1" >> /tmp/codetrail-startup-ttft-20260907/b04-red.txt
```

紅(`b04-red.txt`,exit 1,`1 failed in 12.12s`):

```text
tests/test_client_app.py:1521: in test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction
    assert "skipped(server_busy)" in after_compact[1], after_compact[1]
E   AssertionError: prompt cache 預熱=sent 12:13:32
E   assert 'skipped(server_busy)' in 'prompt cache 預熱=sent 12:13:32'
```

綠(`b04-green.txt`,exit 0):`1 passed in 2.25s`。紅燈是 B04 本體(`/compact` 之後 `/status` 仍是 mount 那一次的 `sent`,而且沒有 `觸發=`,是未修產品的實際輸出);status lane 刻意把三個 `/status` 字串先收齊、離開 pilot 後第一條斷言才是 B04,避免被新增的 `觸發=` 字串遮住(status 報告 §3.1 有說明,是 routine 判斷,root 可調)。紅燈跑 12 秒是兩次「落地」等待在未修產品上各用滿 5 秒。

### 3.5 這些證據的範圍(如實)

- 四對紅 / 綠都是**單 node**,各只跑一紅一綠(status lane 自述沒有筆誤重跑;engine lane 同)。沒有任何人對這一份 digest 跑過 smoke 或 full。
- `04-integration.md` §5 的 smoke(2405 selected、1 failed、失敗 node 未取得)是對**前一版** digest `6e92b5cf…` 的結果,**不是本版的證據**;那條失敗有可能就是 Astra R1-B01 靜態指出的必敗路徑,但沒有執行證據,不得這樣宣稱。
- 未執行的清單:B01 的測試修正;engine 既有 7 條預熱契約與 2 條 heal node;status 的契約 node `test_every_prime_the_coordinator_runs_reports_through_on_prime`;既有 `test_the_tui_primes_on_mount_new_and_session_switch_through_the_coordinator`(索引已調整);`test_smoke_gate.py` 的四條;整包 smoke / full。全部留給 Astra 的 full。

## 4. 動到的既有測試(AGENTS §1.5,兩 lane + 整合者逐條)

**零既有斷言被刪弱、零既有測試被刪、零 skip / xfail 新增。**

| 檔 | node / 位置 | 動作 | 理由 | 誰 |
|---|---|---|---|---|
| `tests/test_client_engine.py` | `test_priming_skips_only_when_no_slot_is_idle_and_never_raises_or_prints` | 開頭加 `real_get_slots = llama_client.get_slots`;末段替換 `get_session` 前加 `monkeypatch.setattr(llama_client, "get_slots", real_get_slots)`;註解兩行 | Astra R1-B01:末段本來還在呼叫回 None 的 stub,根本沒測到產品的 quiet 契約,而且 stderr 斷言必敗。零斷言變更。**未執行** | engine |
| `tests/test_client_engine.py` | 檔尾 | 新增 helper `_prime_threads_gone()` / `_prime_rows()` 與三條 node | 新增,不改既有 helper | engine |
| `tests/test_client_app.py` | `test_the_tui_primes_on_mount_new_and_session_switch_through_the_coordinator` | `noted[1].sent is True` → `noted[1] == "mount" and noted[2].sent is True`;註解改「outcome 經協調器的 on_prime 搬回 UI 執行緒、記錄帶觸發點」 | `_last_prime` 從二元組變三元組(多了觸發點),`/status` 才講得出「這一次是壓縮後的預熱」;交接 §5.5 指定。零其他斷言變更。**未執行** | status |
| `tests/test_client_app.py` | 模組層 | 新增 `_until` helper、`_PrimeOutcomes(_Engine)` 子類與一條 node | 共用替身 `_Engine` 未動(依呼叫序回不同 outcome 用子類做) | status |
| `tests/test_client_turns.py` | 模組 docstring | 「預熱的那兩條」→「那三條」,補「每一次跑完都經 `on_prime` 回報」 | 檔頭要說得出這個檔守哪些契約 | status |
| `tests/test_client_turns.py` | 檔尾 | 新增一條契約 node(raise 情境用測試內子類 `_Raises`) | 共用替身 `_Engine` 未動 | status |
| `tests/test_smoke_gate.py` | `SAFETY_MODULES` 三個既有檔名鍵 | **追加** 5 個 node + 補說明文字 | §2;零刪除、零改寫既有 node | 整合者 |

沿用自前一版、仍成立的既有測試**行為**副作用(`04-integration.md` §7 ⚠,Lane C 揭露):替身有 `prime_prompt_cache` 之後,`tests/test_client_app.py` 每個 app 測試在 `on_mount` 都多 spawn 一條預熱執行緒;本輪起那條執行緒還會經 `on_prime` → `call_from_thread` 記一次 `_last_prime`。兩個 lane 的判斷是畫面與斷言不受影響;仍請 Astra 在 full 時一併看。

## 5. 靜態命令(各一次)

| 命令 | 結果 |
|---|---|
| `python3 -m compileall -q .`(在所有產品 / 測試 / 文件編輯完成後) | 無輸出、exit 0(`-q` 只在失敗時印) |
| `python3 scripts/check_readme_consistency.py`(在 `docs/troubleshooting.md` / `README_DEV.md` / `AGENTS.md` 編輯之後) | `[readme-consistency] OK — README/docs ↔ mcp_server.py / config.py 一致` |

之後我只再改了 `AGENTS.md` 與 `README_DEV.md` 各一處措辭(「`send()` 之前」→「`send()` 內」,heal 是在 `send()` 內做的),**沒有重跑**兩個靜態命令:這兩個檔不在 consistency 腳本掃的範圍(`README.md` + `docs/*.md`),也不是 Python。Astra 對凍結內容重跑即可。

## 6. 文件 / 介面同步(整合者的檔)

| 檔 | 改了什麼 | 依據 |
|---|---|---|
| `README_DEV.md` | (a) 測試指南段:「預熱**只有**契約測試,沒有 red-before-green」改成「第一版只有契約測試…;審核之後修掉的四個真實 bug 各有一條 red-before-green 的 smoke regression」(原句本輪已不成立);(b) 模組分工表 `Engine.next_turn_prefix()` 列補 heal 順序(補的結果排在該群組既有結果之後,與 `send()` 內 `heal_pending_tool_calls()` 一致);(c) `Engine.prime_prompt_cache` 列補「只有終結 chunk + `timings.prompt_n` 才記成 sent;`incomplete` / `no_timings` 不寫 telemetry、`usage.prompt_tokens` 不代填」;(d) `Engine.abort_prime()` 列整列改寫(世代號 +1、叫醒 probe / 等 headers、關串流、上限 1 秒;中止後不再發 POST;POST 在飛時鎖跟著請求走);(e) `TurnCoordinator.prime_in_background` 列補 `on_prime(reason, outcome)` 回呼、先 `on_prime` 再 `on_done`、raise 時 None | engine §5、status §7 |
| `docs/troubleshooting.md` | (a) 「`/new` 與換 session 會中止還在飛的那一次」補「不管它走到哪一段;中止之後不會再送任何請求」;(b) 預熱進行中那段補「換 session 時若舊預熱的請求剛送出、server 還沒回 headers,新對話的第一題會等它回來被關掉之後才送(毫秒級;server 卡住時才會等到請求逾時)」;(c) `/status` 表三列加 ` 觸發=<觸發點>`,第 229 行整句換成「反映**每一次**預熱:啟動、`/new`、換 session,以及自動壓縮 / `/compact` 換掉歷史之後那一次;`觸發=` 告訴你是哪一種:`mount` / `new` / `session` / `compaction`。顯示的是**最近跑完**的那一次」;(d) 跳過原因表加 `incomplete`、`no_timings` 兩列,`aborted` 列改寫。零環境變數、不引用交接目錄 | engine §5、status §7 |
| `AGENTS.md` §2 | (a) `client_engine` 訊息轉換條:懸空 tool_call 那句後補「而且排在該群組**既有**結果之後(與 `send()` 內 `heal_pending_tool_calls()` 的 append 順序一致;插在既有結果之前,預熱送的 prefix 就與下一輪真的送的 payload 在群組中途分岔)」;(b) 同條預熱那句後補「;中止(`abort_prime`)涵蓋 `/slots` probe / 取得 headers 前 / 串流中,中止後不再發 POST,POST 已在飛時模型鎖跟著那個請求走、不得提早放(response 到了關掉才放);只有終結 chunk + `timings.prompt_n` 才記成 sent(`incomplete` / `no_timings` 不寫 telemetry)」;(c) `client_turns` 條末補「;每一次預熱的 outcome 經 `on_prime(reason, outcome)` 回到 TUI,不分誰排的(含壓縮後協調器自己排的那一次),先 `on_prime` 再 `on_done`」。**只加不刪**,原安全條款一字未削弱 | engine §5、status §7 |
| `README.md`、`docs/setup.md` | **未動**:兩檔都沒有預熱 / `/status` 格式的描述(`README.md:859` 只以「`/status` / `/mcp`…」指向 troubleshooting),本輪介面異動不影響它們 | 授權條件「只有實際受介面新行為影響才修改」 |

不在我 owner 內、順帶揭露:`docs/basic-usage.md:39` 對 `/status` 的括號概述沒有列「預熱」(Lane D 前一輪已揭露),本輪同樣未動。

介面異動總表(產品端,供 Astra 對照 lane 報告):`PrimeOutcome.reason` 多 `incomplete` / `no_timings`,`aborted` 語意擴大;`Engine.abort_prime()` 重寫;`Engine._switch_session()`、`_ModelSlot.try_lease()` / `release()`、`_Abandonable`、`_prime_chunk_is_final()`、`_prime_epoch`(私有);`heal_in_place()` 插入位置;`TurnCoordinator.__init__(…, on_prime=None)`;`CodeTrailApp._last_prime` 三元組、`_prime_from_worker()`、`_prime_status()` 帶 `觸發=`。`prime_in_background` 簽名、`prime_prompt_cache(reason=)`、`priming`、事件流、`command_run` 零變更。

## 7. 凍結三件套(`03-plan-final.md` §7)

```
BASE_HEAD      = f200f697ba54d38a102e8ef66dead652c4002e5f
actual HEAD    = b25a551c0c3ca57b4f34035400bca04d7e516778      (git rev-parse HEAD;只有交接 markdown)
index tree     = 35c25fd4118112f6d417bbe9cdd2c70ee9ca3f47      (git write-tree)
product digest = 16bda189fe8d6d99edfd1ebc86b59a373ac2a1816389d98d5c59ab41731f9349
                 (git diff --cached --binary $BASE_HEAD -- . ':(exclude)docs/workflows' | sha256sum)
```

### 7.1 逐字表(命令 → 輸出;寫完本檔之後又各跑一次,值相同)

| 命令 | 輸出 |
|---|---|
| `git rev-parse HEAD` | `b25a551c0c3ca57b4f34035400bca04d7e516778` |
| `git write-tree` | `35c25fd4118112f6d417bbe9cdd2c70ee9ca3f47` |
| `git diff --cached --binary f200f697ba54d38a102e8ef66dead652c4002e5f -- . ':(exclude)docs/workflows' \| sha256sum` | `16bda189fe8d6d99edfd1ebc86b59a373ac2a1816389d98d5c59ab41731f9349` |
| 同上不帶 `--cached`(工作樹 vs base) | `16bda189fe8d6d99edfd1ebc86b59a373ac2a1816389d98d5c59ab41731f9349`(= 上一列 ⇒ index 與工作樹的產品內容相同) |

- **產品未 commit;`actual HEAD` 不含本輪任何修正**,全部產品 / 測試變更只在 index + 工作樹。
- `git add -A -- . ':(exclude)docs/workflows'` 跑了兩次(第二次是 §5 那兩處措辭修正之後),兩次都 exit 1、訊息只有「`docs/workflows` 被 ignore」的 advice,staging 本身完成。上一次(措辭修正前)的 index tree `43cf6bb5c40c59f3a09b11a89d552a366cbe2151` / digest `6d8bedcdfb46a7dcbcfd3ccba52c303349ae25b7dc7b92749a0d73147afd78ea` **作廢**,只記在這裡防混淆。
- 凍結檢查(`git status --porcelain=v1 --untracked-files=all`):21 個產品 / 測試路徑全部 `M `(第二欄空白),零 `??`、零 `MM`、零 ` M` 產品路徑。唯一第二欄非空白的是 ` M docs/workflows/startup-ttft-20260907/00-model-log.md`——root 的檔、在 `docs/workflows` 底下、依 §7 排除在 digest 之外,未被 stage、未被覆寫。
- 兩份新交接(本檔、`07-deferred.md`)在被 ignore 的 `docs/workflows/` 底下,不進 index、不影響 digest;由 root 以 exact path `git add -f` commit。

`git diff --cached --stat f200f697… -- . ':(exclude)docs/workflows'`(21 檔,2630+/59-):

```
 AGENTS.md                      |  21 +-     client_preflight.py            | 119 ++++++-
 README.md                      |   4 +-     client_turns.py                |  84 ++++-
 README_DEV.md                  |  38 +-     codetrail_chat.py              |  11 +-
 client_app.py                  |  92 +++++   config.py                      |   8 +
 client_engine.py               | 514 ++++-   context_budget.py              |  12 +
 docs/setup.md                  |   3 +-     docs/troubleshooting.md        | 123 +++++++
 llama_client.py                |  14 +-     tests/test_client_app.py       | 219 ++++++++
 tests/test_client_cli.py       | 143 ++++++  tests/test_client_engine.py    | 791 +++++++++
 tests/test_client_preflight.py | 193 +++++-  tests/test_client_turns.py     | 197 ++++++++
 tests/test_context_budget.py   |  42 +++     tests/test_lessons.py          |   3 +-
 tests/test_smoke_gate.py       |  58 ++-
```

本輪相對於 Opus 版 index 的增量(`git diff --stat`,stage 之前):`client_engine.py` 305 / `tests/test_client_engine.py` 244(engine lane);`client_app.py` 38 / `client_turns.py` 19 / `tests/test_client_app.py` 114 / `tests/test_client_turns.py` 70(status lane);`AGENTS.md` 13 / `README_DEV.md` 15 / `docs/troubleshooting.md` 21 / `tests/test_smoke_gate.py` 20(整合者;行數是措辭修正前的數字,修正沒有改行數)。其餘 11 個產品路徑與 Opus 版逐字相同。

## 8. Claude 自動 memory 副作用清理(依 `r1-memory-cleanup.json`,只逆轉本任務的兩個寫入)

| 操作 | 結果 |
|---|---|
| op1 `replace_exact_once`(`MEMORY.md` 的 index entry) | **已逆轉**。以 Edit 工具做精確、唯一匹配的替換(`find` 文字只出現一次才會成功;不匹配就 error);逆轉後該檔對 `codetrail-startup-ttft-sprint.md` 的引用 0 次、對 `codetrail-four-in-one-sprint.md` 的引用 1 次、行數 32 → 31。其他 entries 一字未動 |
| op2 `delete_if_content_sha256_matches`(`codetrail-startup-ttft-sprint.md`) | **未刪、停在該項**。實際 sha256 `ad8ccef79f968c7f658a6598e64c5967f1e31e66b4adf971c35145204f3092c9`,descriptor 期望 `0acc378e7c88a7f88f648a7448038571406228d8f3c5c748605855db8096174a`,不符;去掉最後一個 byte 再算是 `f7d282d4664ec497f252cc18f555a73602ee3b8839aa5161cc0a0c40e255d72e`,也不符。檔案 2383 bytes / 19 行,mtime 12:18(descriptor 12:24)。依指示不猜測覆蓋、不刪,**請 root 用原始工具記錄重新產生 descriptor**(可能是 Write 之後 lead 又 Edit 過、或工具記錄的內容與落地 bytes 有尾端換行差異) |

過程揭露:我先寫了一個 Python 腳本到 `/tmp/codetrail-startup-ttft-20260907/r1-memory-cleanup.py` 想一次做兩個操作,harness 兩次要求核准(heredoc 與檔案形式)、未執行;已把那個未執行的腳本刪掉(該目錄回到只有 root 的 `r1-memory-cleanup.json`)。最後 op1 用 Edit、op2 用 `sha256sum` 只讀核對。沒有讀 / 改其他 memory 檔、沒有新增任何持久筆記、沒有把 memory 內容抄進 repo(本檔只記數量與雜湊)。

## 9. 風險 / 未做 / 待審(集中交 Astra)

1. **B02 的子情況**(§1):POST 已送出、headers 未到時,`aborted` 與 `priming=False` 在 1 秒內成立,但模型鎖跟著請求走、response 到了才放——不滿足 B7 字面「1 秒內鎖已放」。待 Astra 裁定;不是擱置。
2. **`_last_prime` = 最後落地而非最後排程**(§1 B04 列):中止逾時時 `/status` 可能短暫顯示舊的 `skipped(aborted)`。待 Astra 判定。
3. **幾乎不可達的交錯**(engine §6):排程晚到的預熱在 `_switch_session()` 之後才取歷史 → 送的是新 prefix,TUI 接著排的那一次回 `model_busy`,`/status` 顯示 `skipped(model_busy)` 而新 prefix 其實已被送過。需要「換 session 發生在 mount 預熱執行緒還沒排到 CPU 的微秒內」。lead 本輪不處理,列在 `07-deferred.md` §B 供 Astra 判定是否構成 Blocker。
4. **零 smoke / full**;§3.5 的未執行清單;前一版 smoke 的失敗 node 仍未知,對本版沒有任何測試證據。
5. `_Abandonable.wait()` 50 ms 輪詢:每次預熱最多多 ~100 ms 背景延遲(probe + POST 各一次),與 `_open_stream` 同數量級。新的 daemon 執行緒名 `codetrail-prime-probe` / `-http` / `-settle`。
6. `no_timings` 在本機 build 不預期出現;若出現,`/status` 誠實顯示 `skipped(no_timings)`。
7. 既有 app 測試的行為副作用(§4 末段)本輪多了 `on_prime` 那一步。
8. 我沒有獨立驗證「紅燈當時的產品是未修的」——只能核對紅燈訊息與描述的未修行為一致(§3),以及檔案 mtime 順序(紅 12:10 / 12:13,綠 12:15 / 12:16)。

## 10. 誠實揭露(`03-plan-final.md` §5 B9 固定句)

**reasoning 與硬體 prefill 成本未變;預熱只把下一輪 prefix 的 prefill 搬到打字之前;熱 prefix 時無感;是否真的被重用只由 T0 的 `prompt_tokens_processed` 判定,規劃、施工、回修與整合階段沒有數字。** 本輪沒有量測任何真實加速,不宣稱需求 b 已實測改善。

## 11. 已執行 / 未執行的命令

已執行:`git rev-parse HEAD`(數次)、`git log --oneline -8`、`git status --porcelain=v1 --untracked-files=all`(數次)、`git status --short` 形式的 `git diff --stat` / `git diff --cached --stat`、`git diff -- <六個 lane 檔>`、`git diff -- <我的四個檔>`、`git ls-files docs/workflows/startup-ttft-20260907/`、`ls` / `sha256sum` / `head -c -1 | sha256sum` / `wc` / `grep -c`(memory 清理核對)、`rm` 我自己剛寫的未執行腳本、`python3 -m compileall -q .` ×1、`python3 scripts/check_readme_consistency.py` ×1、`git add -A -- . ':(exclude)docs/workflows'` ×2、`git write-tree` ×2、兩種 digest ×2、`git diff --cached --stat <base>` ×2。

未執行(被 harness 要求核准而放棄,或本來就禁止):`git check-ignore`(要核准;不影響結論)、Python 清理腳本(要核准;改用 Edit / sha256sum)、任何 pytest / smoke / full / 單 node、任何 HTTP、`git commit` / `push` / `reset` / `checkout` / `stash`。

`Tests: 整合者零執行;四條新 regression 各一紅一綠由兩個 lane 執行(§3);smoke / full 未跑 — reviewer owns full execution.`
