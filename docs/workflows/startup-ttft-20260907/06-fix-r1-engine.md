# Step 5 R1 回修 — engine lane 交付報告(R1-B01 / B02 / B03 / B05)

- 施工者:Claude Fable 5.1(harness 自報 model ID `claude-fable-5-1`;effort 由 root 的 argv 記進 `00-model-log.md`)。同一個 session 先寫了 `06-fix-r1-dependencies.md`,再做本 lane。
- 日期:2026-09-07。cwd `/home/david/CodeTrail`,主工作樹,與 status lane 並行。
- 產品 base HEAD `f200f697ba54d38a102e8ef66dead652c4002e5f`;交付時 `git rev-parse HEAD` = `a946ab9f813dc88381b0e3ce5ad83e3770a34b65`(只有交接 markdown;產品修改仍未 commit)。上一輪的 21 個產品路徑仍 staged;本 lane 的改動是 **unstaged**,只在 `client_engine.py` 與 `tests/test_client_engine.py`(`git diff --stat -- client_engine.py tests/test_client_engine.py`:305+/60-、244+/1-)。沒有 stage、沒有 commit、沒有動別人的檔。
- 沒有對任何 live server 發請求、沒有讀私人 session、沒有改部署、沒有新增 `os.environ` 讀取、沒有 `process_env` 以外的 spawn(新的小執行緒是 `threading.Thread`,與既有 `_open_stream` 同一種)。
- 執行過的命令:`git rev-parse` / `git status --short` / `git diff -- <自己的路徑>`、`python3 -m py_compile client_engine.py tests/test_client_engine.py`(零輸出)、三條**新** regression node 各一紅一綠(§3)。**沒有**跑 smoke、full、整檔 pytest、其他 node、`check_readme_consistency`(本 lane 不改文件)。

## 1. Blocker 逐項處理

### R1-B01 — quiet 契約測試的 stub 未還原(測試缺陷,不是產品 bug)

- 修法:`test_priming_skips_only_when_no_slot_is_idle_and_never_raises_or_prints` 開頭先 `real_get_slots = llama_client.get_slots`(在任何 monkeypatch 之前),末段在替換 `get_session` **之前** `monkeypatch.setattr(llama_client, "get_slots", real_get_slots)`。quiet / 預設兩次 probe 於是真的走產品的 `get_slots`(`get_session()` → `_DeadSession.get` raise → except → quiet 不印 / 預設印 `/slots probe failed`),只 mock 底層 session。兩側斷言一字未動、沒有刪弱、沒有 skip。
- **未執行**:這條不是我新寫的 regression node,依 AGENTS §1.2 與本輪指示不得單跑;結果留給 reviewer 的 full。靜態推演:`_log_probe_failure` 以 `print(..., file=sys.stderr)` 輸出 `[llama_client] /slots probe failed for …`,capsys 收得到;`monkeypatch` 的還原順序是反向的,teardown 後 `llama_client.get_slots` 仍是原函式。
- 不新增 node(AGENTS §1.4:既不是產品 bug 的 regression,也不是新契約)。

### R1-B02 — 取得 stream 之前無法中止舊預熱

修法(全在 `client_engine.py`):

1. **可放棄的阻塞 I/O**:新 helper `_Abandonable(request, name)` 在小執行緒裡跑 `/slots` GET 與 POST,主流程 `wait(abort)` 每 50 ms 看一次**預熱自己的** `threading.Event`(不借 `_cancel`,與既有 `_open_stream` 同一手法)。執行緒名 `codetrail-prime-probe` / `codetrail-prime-http` / `…-settle`。
2. **中止落在 probe**:`probe.wait()` 回 False → 立刻回 `aborted`;GET 不占 slot,鎖照常由 `finally` 放(放它不會讓兩個模型請求重疊);那個 GET 自己在 `PRIME_SLOTS_TIMEOUT` 內結束、結果丟掉。
3. **中止落在 probe 回來之後 / gate 之後、POST 之前**:每個邊界比一次**世代號** `_prime_epoch`(snapshot 歷史時記下;`abort_prime()` 與換 session 都 +1),不同就回 `aborted`,**不發 POST**。
4. **中止落在 POST 已送出、headers 未到**:`post.wait()` 回 False → `slot.hand_off()`、`post.settle(關串流 → slot.release_late())`、回 `aborted`、`priming=False`。**模型鎖跟著那個請求走**:response 到了就關掉(server 偵測斷線會中止生成),然後才放鎖;新對話的第一題在鎖上排隊,不與一個可能還在 server queue 裡的舊請求重疊。這是 Astra 明訂「不得提早放掉仍有舊 HTTP 在跑的共用模型鎖」的唯一合規做法,也是既有回合取消(`_open_stream` 的 `hand_off` / `release_late`)的同一規則。為此 `_ModelSlot` 多了 `try_lease(lock)`(非阻塞取鎖)與 `release()`(= `__exit__`,已 hand_off 就 no-op),預熱改用租約而不是直接 `model_lock.acquire/release`。
5. **中止落在串流中**:既有路徑(`abort_prime()` 關 socket),判定改看世代號。
6. **換 session 與 snapshot 互斥**:`new_session()` / `adopt()` 的狀態切換收進 `_switch_session()`,在 `_prime_guard` 內 `_prime_epoch += 1` 並換 `session_id` / `messages` / `store_error` / `resumed_snapshot`。原因:`abort_prime()` 只收得到當時已登記的那一次;一個排程晚到、在 `abort_prime()` 之後才取歷史的預熱,拿到的仍是舊對話。預熱取歷史與世代號在同一個臨界區(`_turn_state` → `_prime_guard`,鎖序固定),所以任何在切換前取的 snapshot 都會在下一個邊界回 `aborted`。
7. `abort_prime()`:世代號 +1、設中止 Event、關已登記串流、等 `done` 上限 1 秒;docstring 明寫「鎖已放,除非中止落在 POST 已送出、headers 未到 —— 那時鎖跟著請求走」。

**對 B7 的如實揭露(交 Astra 裁定,不自行擱置)**:B7 寫「1 秒內舊預熱結束(aborted)、鎖已放、priming False」。probe 中、probe 後、串流中三種情況三個條件都在 1 秒內成立(§3 的 node 釘住)。**POST 已送出、headers 未到**那一種,outcome 與 `priming` 在 1 秒內成立,但「鎖已放」的時間 = 舊請求 headers 到達並被關掉的時間 —— 提早放就是 Astra 禁止的事,兩者不可能同時成立。llama-server 對串流請求在 handler 回來時就寫 headers(不等 slot),所以實務上是毫秒級;server 卡住或連線逾時的極端情況下,鎖會被握到 `options.request_timeout`,與既有回合取消完全相同。若 Astra 認定這是 B7 的偏離,請記 `07-deferred.md`;本 lane 認為這是 B7 在該子情況下唯一正確的讀法。

### R1-B03 — 部分完成的多工具群組,預熱與真 send 的 prefix 順序不同

- 修法:`heal_in_place()` 把補的「已中斷」結果放在**該群組既有結果之後**(先跳過緊接在 assistant 後面連續的 `tool` 訊息,再插入;宣告順序不變,插入以反向套用避免索引位移)。與 `heal_pending_tool_calls()` 在 `send()` 時 append 的順序一致:`assistant(a,b) → tool(a) → tool(b, 已中斷)`。「結果緊接宣告群組」的既有契約不變:既有兩條 node(`test_a_healed_tool_result_sits_next_to_its_call_after_a_resume`、`test_a_dangling_tool_call_is_healed_before_the_next_request`)的情境是「群組沒有任何既有結果」,插入位置與以前相同。
- 影響範圍:`payload_messages()`、`_prefix_from()`、`prune_for_summary()`(壓縮摘要請求)都吃 `heal_in_place()`,三者對部分完成群組的順序現在一致。原始歷史 / store / transcript 不被改寫(node 有斷言)。

### R1-B05 — 沒有終結 chunk 的 clean EOF 被記成成功

- 修法:預熱迴圈用 `_prime_chunk_is_final()`(與 `parse_usage_from_stream_chunk` 同一判準:`stop` 或 `choices[0].finish_reason`)記錄有沒有看到終結 chunk。沒看到 → `PrimeOutcome(False, "incomplete", None)`;看到但 `usage.prompt_tokens_processed is None`(最後一個 chunk 沒有 `timings.prompt_n`)→ `PrimeOutcome(False, "no_timings", None)`。兩者都**不** `log_metrics`(不生成冒充成功的 `prime` 列),不 raise、不 print、不寫 session。`usage.prompt_tokens` 不拿來代填 processed(node 有斷言:帶 `usage.prompt_tokens=20000` 但無 timings 的終結 chunk → `no_timings`、processed None、零列)。正常的 `finish_reason=length` + timings 照舊 `sent`。
- `PrimeOutcome` docstring 加了兩個 reason 的說明。

## 2. 改動檔案

| 檔 | 改了什麼 |
|---|---|
| `client_engine.py` | `heal_in_place()` 插入位置與 docstring;`PrimeOutcome` docstring(`incomplete` / `no_timings` / `aborted` 說明);`_ModelSlot.try_lease()` / `release()`;新 `_Abandonable`、`_prime_chunk_is_final()`;`Engine.__init__` 的 `_prime_abort` 改成 `Event | None`、新 `_prime_epoch`;`new_session()` / `adopt()` 改走新 `_switch_session()`;`prime_prompt_cache()`(租約、Event 對、docstring 多一條「收得掉」)、新 `_prime_superseded()`、`_prime_locked(slot, abort)` 重寫(probe / POST 可放棄、世代號邊界、終結判定)、`abort_prime()` 重寫 |
| `tests/test_client_engine.py` | B01 的測試修正(§4);檔尾新增 helper `_prime_threads_gone()` / `_prime_rows()` 與三條 regression node(§3) |

沒有動:`llama_client.py`、`context_budget.py`、`tests/test_context_budget.py`(保留給本 lane 但用不到)、以及任何共用檔 / status lane 的檔。

## 3. 新 regression node(全部 `@pytest.mark.smoke`;檔案另有 module 層 `pytestmark`)

命令形狀(三條相同,`<node>` 換掉;exit code 由 harness 回報並追記進檔尾):

```bash
python3 scripts/run_tests.py "tests/test_client_engine.py::<node>" > /tmp/codetrail-startup-ttft-20260907/<縮寫>-<red|green>.txt 2>&1
```

(原定「shell 變數 + `exit $VAR`」的形狀被沙箱以 `simple_expansion` 拒絕,`bash run_node.sh` 需要核准而拿不到;改成上面這種,harness 直接回報該命令的 exit code,我把它逐字追記到檔尾:`exit=1 (reported by the harness …)` / `exit=0 (…)`。)

### 3.1 `test_priming_matches_the_next_send_when_a_tool_group_is_only_partly_answered`(B03)

- 紅:`/tmp/codetrail-startup-ttft-20260907/engine-b03-partial-group-red.txt`,exit 1

  ```
  tests/test_client_engine.py:2632: in test_priming_matches_the_next_send_when_a_tool_group_is_only_partly_answered
      assert turn["messages"][:-1] == prime["messages"]
  E   AssertionError: assert [{'role': 'sy...不要假設它已經執行過。'}] == [{'role': 'sy...ok\n(a 的結果)'}]
  E     At index 3 diff: {'role': 'tool', 'tool_call_id': 'a', 'name': 'list_dir', 'content': 'status: ok\n(a 的結果)'} != {'role': 'tool', 'tool_call_id': 'b', 'name': 'read_file', 'content': 'status: error\n這次呼叫被中斷,沒有結果。\nnext: 需要的話重新呼叫一次;不要假設它已經執行過。'}
  ============================== 1 failed in 0.31s ===============================
  ```

- 綠:`…/engine-b03-partial-group-green.txt`,exit 0:`1 passed in 0.29s`。

### 3.2 `test_priming_only_counts_as_sent_after_a_terminal_chunk_with_timings`(B05)

- 紅:`…/engine-b05-terminal-chunk-red.txt`,exit 1

  ```
  tests/test_client_engine.py:2669: in test_priming_only_counts_as_sent_after_a_terminal_chunk_with_timings
      assert outcome == client_engine.PrimeOutcome(False, reason, None), (name, outcome)
  E   AssertionError: ('empty', PrimeOutcome(sent=True, reason='', processed_tokens=None))
  E     Differing attributes:
  E     ['sent', 'reason']
  ============================== 1 failed in 0.23s ===============================
  ```

- 綠:`…/engine-b05-terminal-chunk-green.txt`,exit 0:`1 passed in 0.21s`。
- 覆蓋:空串流、非終結 delta 後 EOF、終結但無 timings(帶 `usage.prompt_tokens`)三種 → `incomplete` / `incomplete` / `no_timings`、零 `prime` 列、鎖已放、`priming` False;正常 `finish_reason=length` + timings → `sent`、一列、`prompt_tokens_processed=9`;capsys 空。

### 3.3 `test_a_session_switch_aborts_a_prime_that_has_no_stream_yet_and_never_sends_after_the_abort`(B02)

- 紅:`…/engine-b02-abort-no-stream-red.txt`,exit 1

  ```
  tests/test_client_engine.py:2726: in test_a_session_switch_aborts_a_prime_that_has_no_stream_yet_and_never_sends_after_the_abort
      assert not primer.is_alive()
  E   assert not True
  E    +  where True = is_alive()
  E    +    where is_alive = <Thread(Thread-1 (<lambda>), started daemon 127916463789760)>.is_alive
  ============================== 1 failed in 2.25s ===============================
  ```

  (未修的產品:`new_session()` 裡的 `abort_prime()` 沒有東西可關、等滿 1 秒回 False,舊預熱仍卡在 probe。)

- 綠:`…/engine-b02-abort-no-stream-green.txt`,exit 0:`1 passed in 0.32s`。
- 覆蓋(受控 barrier,全部離線替身):
  1. 中止落在 probe(GET 卡住):`new_session()` 後 1 秒內 `aborted`、`priming` False、鎖可取;放開 GET 之後預熱的小執行緒全部結束、**零 POST**。
  2. 中止落在 gate 之後、POST 之前(spy 在 `check_and_log` 回來時呼叫 `abort_prime(wait=0)`):`aborted`、零 POST。
  3. 中止落在 POST 已送出、headers 未到:`adopt()` 後 1 秒內 `aborted`、`priming` False,但 `model_lock.acquire(blocking=False)` 是 False(鎖跟著請求走);放開 POST 之後鎖在 1 秒內可取、串流被關**恰好一次**、POST 只有那一次。
  4. 換過去之後的預熱照常准入、送的是新歷史(`messages[-1]` 是新對話的 user);被中止的三次零 telemetry,只有第 4 次那一列。

### 3.4 給整合者:`tests/test_smoke_gate.py` 登記

在既有檔名鍵 `test_client_engine.py` 下追加三條(不新增檔名鍵):

- `test_priming_matches_the_next_send_when_a_tool_group_is_only_partly_answered`
- `test_priming_only_counts_as_sent_after_a_terminal_chunk_with_timings`
- `test_a_session_switch_aborts_a_prime_that_has_no_stream_yet_and_never_sends_after_the_abort`

說明文字可加:「預熱的中止涵蓋 probe / 取得 headers 前 / 串流中且中止後不再發 POST;部分完成的工具群組預熱 == 下一輪真 send;只有終結 chunk + `timings.prompt_n` 才算 sent」。

## 4. 動到的既有測試(AGENTS §1.5,逐條)

| 檔 | node | 動作 | 理由 |
|---|---|---|---|
| `tests/test_client_engine.py` | `test_priming_skips_only_when_no_slot_is_idle_and_never_raises_or_prints` | 開頭加 `real_get_slots = llama_client.get_slots`;末段在替換 `get_session` 前加 `monkeypatch.setattr(llama_client, "get_slots", real_get_slots)`;註解兩行 | Astra R1-B01:末段本來還在呼叫回 None 的 stub,根本沒測到產品的 quiet 契約,而且第 2450 行必敗。改完兩次 probe 真的走 `get_slots`。**零斷言變更、零刪除、零 skip**。未執行(§1) |

其餘既有 node(含這一區另外 7 條預熱契約)零變更。對它們的靜態核對:node 1 / 4 / 7 / 8 的 `_HeldStream` / `_synthetic_history` 路徑不受影響(POST 的替身立刻回串流,阻塞在迭代);node 5(串流中中止)改由世代號判定,`closed_from` 仍是 1(`abort_prime()` 關、`finally` 不再關);node 6 的 `_boom` 在 http 小執行緒 raise,由 `result()` 原樣重丟 → `error:RuntimeError`、鎖已放;node 3 的兩條讓路路徑都在 probe 之前回。`test_a_healed_tool_result_sits_next_to_its_call_after_a_resume` / `test_a_dangling_tool_call_is_healed_before_the_next_request` 的群組沒有既有結果,插入位置不變。**這些都沒有執行**,由 reviewer 的 full 驗證。

## 5. 介面異動與要同步的文件字句(整合者 owner)

介面(產品):

- `PrimeOutcome.reason` 多兩個值:`incomplete`(串流在終結 chunk 之前結束)、`no_timings`(終結但無 `timings.prompt_n`);兩者不寫 telemetry。`aborted` 的語意擴大:落在 probe / 取得 headers 前 / 串流中都算,中止後不再發 POST。
- `Engine.abort_prime()`:世代號 +1、設中止 Event、關已登記串流、等結束上限 1 秒;POST 在飛時鎖跟著請求走(見 §1 B02 的揭露)。
- `Engine._switch_session()`(私有):`new_session()` / `adopt()` 的狀態切換與預熱 snapshot 互斥。
- `_ModelSlot.try_lease()` / `release()`(私有)、`_Abandonable`(私有)、`_prime_chunk_is_final()`(私有)。
- `heal_in_place()`:補的結果放在該群組既有結果之後(宣告順序不變)。
- 協調器 / TUI 介面**零變更**;status lane 的 `on_prime` 不受影響(新 reason 只是字串)。

文件(請整合者改;本 lane 未動任何文件):

- `docs/troubleshooting.md` 跳過原因表(第 231–240 行)加兩列:「`incomplete` | 請求在終結 chunk 之前就結束(server 只送了 keep-alive 或幾個 delta 就關線);不算送出,不記 telemetry」、「`no_timings` | server 的最後一個 chunk 沒有 `timings.prompt_n`,量不到重算了多少;不算送出」;`aborted` 那一列改成「你換了 session 或開了新對話;不管中止落在哪一段,舊預熱之後不會再送任何請求」。第 216–218 行「這時候送出問題……在模型鎖上等預熱那一次送完」可補一句「換 session 時若舊預熱的請求剛送出、server 還沒回 headers,新對話的第一題會等它回來被關掉之後才送(毫秒級;server 卡住時才會等到請求逾時)」。
- `README_DEV.md` 第 645 行 `Engine.abort_prime()` 那一列改成:「`new_session()` / `adopt()` 的第一行:世代號 +1、叫醒卡在 `/slots` probe 或等 headers 的那一段、關掉已登記的串流,等它結束(上限 1 秒)。中止之後不再發 POST;POST 已在飛時模型鎖跟著那個請求走(`_ModelSlot.hand_off`),response 到了關掉才放,與回合取消同一規則。」第 644 行准入順序末尾可補「;串流只有看到終結 chunk 且拿到 `timings.prompt_n` 才記成 sent(`incomplete` / `no_timings` 不寫 telemetry)」。
- `AGENTS.md` §2 `client_engine` 條的預熱那一句(第 100 行起)可補:「;中止(`abort_prime`)涵蓋 probe / 取得 headers 前 / 串流中,中止後不再發 POST,POST 在飛時模型鎖跟著請求走、不得提早放;只有終結 chunk + `timings.prompt_n` 才記成 sent」。
- `tests/test_client_engine.py` 模組 docstring 的預熱 bullet 不需要改(仍成立)。

## 6. 未做與風險

- **B01 未執行**(§1);**既有 7 條預熱契約與兩條 heal node 未重跑**(§4)。全部留給 reviewer 的 full。
- **B7 在「POST 已送出、headers 未到」子情況的讀法**(§1 B02)交 Astra 裁定;若判定為偏離,記 `07-deferred.md`。
- `_Abandonable.wait()` 用 50 ms 輪詢:每次預熱最多多 ~100 ms 的背景延遲(probe + POST 各一次),與 `_open_stream` 同一數量級,不影響使用者。
- 幾乎不可達的一種交錯:一個排程晚到的預熱在 `_switch_session()` **之後**才取歷史,會拿新對話的 prefix 送出去,而 TUI 接著排的那一次會回 `model_busy` —— `/status` 顯示 `skipped(model_busy)`,實際上新 prefix 已被那個晚到的預熱送過。需要「換 session 發生在 mount 預熱執行緒還沒排到 CPU 的微秒內」,本輪不處理;列給 `07-deferred.md`。
- `no_timings` 在本機 build 不預期出現(`/v1/chat/completions` 串流最後一個 chunk 帶 `timings`,`context_budget` 既有註解已記);若哪個 build 拿掉了,`/status` 會誠實顯示 `skipped(no_timings)` 而不是假的 sent。
- 沒有量測任何真實加速;§5 B9 的固定句不變:reasoning 與硬體 prefill 成本未變;預熱只把**下一輪 prefix** 的 prefill 搬到打字之前;熱 prefix 時無感;是否真的被重用只由 T0 的 `prompt_tokens_processed` 判定,規劃與施工階段沒有數字。

## 7. 給 status lane / 整合者的介面說明

- status lane 不需要因本 lane 改任何東西:`prime_prompt_cache(reason=)` 簽名、`priming`、`PrimeOutcome` 形狀不變;`/status` 的 `skipped(<reason>)` 對 `incomplete` / `no_timings` 原樣顯示即可。
- 整合者:§3.4 的三條登記、§5 的文件字句、`06-fix-r1.md` 三件套(`git add -A -- . ':(exclude)docs/workflows'` 之後本 lane 的兩個檔會與 status lane 的四個檔一起進 index)。本 lane 交付時工作樹狀態:`MM client_engine.py`、`MM tests/test_client_engine.py`(本 lane);`MM client_turns.py` / `client_app.py` / `tests/test_client_app.py` / `tests/test_client_turns.py`(status lane 施工中);其餘 15 個產品路徑仍是上一輪的 `M `。

`Tests: 三條新 regression 各一紅一綠;沒有跑 smoke — reviewer owns full execution.`
