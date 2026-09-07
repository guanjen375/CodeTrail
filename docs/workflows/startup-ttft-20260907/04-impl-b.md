# Lane B 施工報告 — 預熱 engine 與 telemetry

- 施工者:Claude Opus 5.0(CLI 指定 `claude-opus-5`,effort MAX)。model 身分以 CLI init / modelUsage 另核對。
- 依據:`03-plan-final.md` §2 決策、§4.2 凍結介面、§5 B/C、§6 Lane B。初稿未重讀。
- worktree:`/tmp/codetrail-startup-ttft-20260907/worktrees/b`,base HEAD `f200f697ba54d38a102e8ef66dead652c4002e5f`(未 commit、未 push)。
- patch:`/tmp/codetrail-startup-ttft-20260907/patches/lane-b.patch`(`git diff --binary`)。
- **Dependency:無。** 本 lane 不依賴其他 lane 的任何產出;§4.2 的介面名稱由 Lane C 以 `getattr` 使用。

## 1. 可寫檔 / owner

| 檔 | owner | 本次是否修改 |
|---|---|---|
| `client_engine.py` | Lane B(唯一) | 是 |
| `config.py`(僅預熱常數) | Lane B(唯一) | 是 |
| `llama_client.py`(僅 `get_slots` quiet) | Lane B(唯一) | 是 |
| `context_budget.py`(僅新計數欄位) | Lane B(唯一) | 是 |
| `tests/test_client_engine.py` | Lane B(唯一) | 是 |
| `tests/test_context_budget.py` | Lane B(唯一) | 是 |
| `/home/david/CodeTrail/docs/workflows/startup-ttft-20260907/04-impl-b.md` | Lane B(唯一) | 是(本檔) |

`tests/test_smoke_gate.py` **未碰**(整合者 owner)。`.gitignore`、其他產品檔、主 repo 產品皆未動。

## 2. 實際修改檔案與內容

```
 client_engine.py             | 301 +++++++++++++++++++++++-
 config.py                    |   8 +
 context_budget.py            |  12 +
 llama_client.py              |  14 +-
 tests/test_client_engine.py  | 549 +++++++++++++++++++++++++++++++++++++++++++
 tests/test_context_budget.py |  42 ++++
 6 files changed, 922 insertions(+), 4 deletions(-)
```

### 2.1 `config.py`

- 新增 `CLIENT_PRIME_PROMPT_CACHE = True`,緊接 `CLIENT_MAX_OUTPUT_TOKENS` 的 import guard 之後(§4.2)。
- 註解寫明:只有互動 TUI 會走、headless 沒有呼叫點、readonly 在任何 I/O 之前被拒、縮短不了 reasoning 與硬體 prefill、是 repo 常數不是 `client.json` 的鍵、沒有環境變數。
- `CLIENT_MAX_OUTPUT_TOKENS` 的 import-time 上限與 `compaction_formula` 的真值**一個字都沒動**。

### 2.2 `llama_client.py`

- `get_slots(base_url, *, timeout=5, quiet=False)`。`quiet=True` 只跳過 `_log_probe_failure`,其餘行為與回傳值不變;預設仍會講話(既有三個呼叫端 `gpu_safety` / `scripts/doctor.py` / `utils.py` 與 `tests/test_endpoint_policy.py` 全部走預設,零變更)。

### 2.3 `context_budget.py`

- `ContextUsage` 新增 `prompt_tokens_processed: int | None = None`,放在 `output_tokens_per_second` 之後(`to_log_dict` 經 `asdict` 自動帶出)。
- `parse_usage_from_response()`:`timings.prompt_n` 為數值時**一律**填 `prompt_tokens_processed`,不看 `pec` 是否已有值;`actual_prompt_eval_count` 的既有語意與填值條件完全不變。
- `parse_usage_from_stream_chunk()` 未改(它委派給 `parse_usage_from_response`)。

### 2.4 `client_engine.py`

- 模組 docstring 由「三件事」改成「四件事」,新增第 4 條(預熱是零寫入的)。
- 新常數:`PRIME_SOURCE="prime"`、`PRIME_MAX_TOKENS=1`、`PRIME_SLOTS_TIMEOUT=2`、`PRIME_ABORT_WAIT=1.0`。
- 新型別 `PrimeOutcome(NamedTuple)`:`sent` / `reason` / `processed_tokens`,docstring 逐條列出 reason 值域。
- 新模組層 helper `_slot_is_busy()`(`is_processing is True` 或 `isinstance(state, int) and state != 0`;非 Mapping / 認不出的形狀一律當閒)與 `_PRIME_PLACEHOLDER_KEY`。
- `Engine.__init__` 新增 `_prime_guard` / `_prime_stream` / `_priming` / `_prime_abort` / `_prime_done`,**與回合狀態完全分開**(不共用 `_active_stream` / `_cancel` / `_turn_state` 之外的任何欄位)。
- 新增 `next_turn_prefix()` / `_prefix_from(history)` / `priming`(property)/ `prime_prompt_cache(*, reason="")` / `_prime_locked()` / `abort_prime(*, wait=PRIME_ABORT_WAIT)`。
- `new_session()` 與 `adopt()` 的**第一行**呼叫 `abort_prime()`(沒有預熱時是純 no-op,零 I/O)。
- `_ModelSlot`、`_open_stream`、`request_cancel` / `cancel` / `clear_cancel` / `_begin_turn` / `_end_turn` / `_decide_commit`、`payload_messages`、`send`、`complete`、store 相關防線**一行都沒改**。

`prime_prompt_cache()` 的實際順序(= §4.2 的 1–9):

1. `config.CLIENT_PRIME_PROMPT_CACHE` → `disabled`;`_loaded_tools` → `tools_not_loaded`;`options.policy.name != InteractivePolicy.name` → `policy`。**三個都在任何 I/O、任何鎖之前。**
2. `model_lock.acquire(blocking=False)` 失敗 → `model_busy`;成功後全部在 `try/finally`。
3. `with self._turn_state:` `_in_turn > 0` → `turn_in_progress`;否則同一臨界區 `history = list(self.messages)` + `_priming = True`。
4. `get_slots(base_url, timeout=PRIME_SLOTS_TIMEOUT, quiet=True)`;list 且非空且**每個** slot 都忙 → `server_busy`;None / 非 list / 空 list / 有 idle → 繼續。
5. `build_usage(source="prime", reserved_output_tokens=options.max_output_tokens).hard_overflow` → `next_turn_would_overflow`(**不 log**)。
6. `check_and_log(source="prime", reserved_output_tokens=PRIME_MAX_TOKENS, emit=False)`;`ContextOverflowError` → `gate`。
7. `chat_completions(... tools, tool_choice="auto", stream=True, extra={"max_tokens":1}, timeout=options.request_timeout)`;在 `_prime_guard` 內登記;登記前 abort 已到 → 關掉並回 `aborted`。
8. 逐 chunk `parse_usage_from_stream_chunk`,內容丟棄;正常結束 → `log_metrics` + `PrimeOutcome(True, "", usage.prompt_tokens_processed)`;被 abort 收掉 → `aborted`(**不 log**);其餘例外 → `error:<Type>`。
9. 不碰 `_begin_turn` / `_end_turn` / `_record` / `_cancel` / `_armed` / `_turn_completed` / `_active_stream` / `_active_call` / store / `on_event`(engine 這一層根本沒有 `on_event` 參數可傳)。

`finally` 的順序是 **清 `_prime_stream` → `_priming=False` → 關串流 → release 模型鎖 → `done.set()`**:等待者(`abort_prime`)醒來時,鎖一定已經放掉、`priming` 一定已經是 False(§5 B7 的三個斷言)。

## 3. 新增測試 node(全部 `@pytest.mark.smoke`,只寫不執行)

`tests/test_client_engine.py`(檔案有 module 層 `pytestmark = pytest.mark.smoke`,另加逐條 decorator):

1. `test_priming_sends_the_prefix_the_next_turn_will_send_and_records_nothing`
2. `test_priming_refuses_a_readonly_engine_before_any_probe_or_request`
3. `test_priming_yields_to_a_turn_that_already_began_and_never_reads_its_history`
4. `test_a_turn_submitted_during_priming_waits_for_the_lock_and_gets_the_primed_prefix`
5. `test_a_session_switch_aborts_an_in_flight_prime_and_frees_the_lock`
6. `test_priming_skips_only_when_no_slot_is_idle_and_never_raises_or_prints`
7. `test_priming_gates_the_one_token_it_sends_after_checking_the_next_turn_reserve`
8. `test_priming_is_invisible_to_cancel_and_leaves_the_turn_state_untouched`

`tests/test_context_budget.py`(該檔沒有 module 層 pytestmark,已加逐條 decorator):

9. `test_processed_prompt_tokens_are_recorded_separately_from_the_total`

**給整合者:上面 9 條要登記進 `tests/test_smoke_gate.py` 的既有檔名鍵**
(`test_client_engine.py` 8 條、`test_context_budget.py` 1 條),不新增檔名鍵。

HTTP 全部 monkeypatch(`llama_client.chat_completions` 與 `llama_client.get_slots`;
第 6 條末段另 monkeypatch `llama_client.get_session` 成會 raise 的替身,所以連 socket 都
不會建)。所有會寫 telemetry 的 node 都把 `config.CTX_METRICS_PATH` 指到 `tmp_path`。

各條對應的驗收項:

| node | §5 驗收 |
|---|---|
| 1 | B1(以真實後續 `send()` 的 payload 為準)、B2(零寫入)、B5(`source=prime` 一列、`reserved_output_tokens==1`、`prompt_tokens_processed` 有值)、`disabled` 零 HTTP |
| 2 | B6 前半(readonly 在任何 probe / request 之前拒絕、store append 0、capsys 空) |
| 3 | B2 / B3(`turn_in_progress` 零 HTTP + 鎖已放;`model_busy`) |
| 4 | prestart 空窗:預熱期間送出的一輪在鎖上等,拿到的 payload == prefix + user |
| 5 | B7(`new_session()` 與 `adopt()` 兩條路各 2 秒內 `aborted`、`closed_from` 長度 1、鎖可取、`priming` False、telemetry 零列) |
| 6 | B3(idle-slot 判準)、B8(`quiet=True` 零 stderr;預設呼叫端仍留原因)、`error:RuntimeError` 且鎖已放、capsys 空 |
| 7 | B5(gate 保留額 == 實送 max_tokens == 1)、`next_turn_would_overflow` 且零 HTTP、零 log(連 `check_and_log` 都沒走) |
| 8 | B2(`request_cancel().accepted is False`、`cancel() is False`、`_cancel` 未設、`_in_turn==0`、`_armed` / `_turn_seen_since_clear` / `_active_stream` / `_active_call` 皆未動;結束後 `clear_cancel()` 狀態如初、下一題正常) |
| 9 | B4(三種回應形狀:usage + timings 並存、串流只有 timings、native 沒有 timings) |

## 4. 動到的既有測試(逐條)

| 檔 | 位置 | 改了什麼 | 理由 |
|---|---|---|---|
| `tests/test_client_engine.py` | 模組 docstring | 加一條 bullet:「prompt cache 預熱是唯一一條沒有使用者訊息就打主模型的路徑…」 | 這份 docstring 是「本檔守哪些 §2 檢查點」的清單,新增檢查點就要同步;零斷言變更 |
| `tests/test_client_engine.py` | import 區 | 新增 `import copy` | node 1 要用 `copy.deepcopy` 證明歷史逐字不變(淺拷貝比不出巢狀 `tool_calls` 被動過) |

**既有斷言、既有測試本體、共用替身(`FakeMcp` / `engine_factory` / `_stream` / `_text_chunk` /
`_tool_chunk` / `_BlockingStream` / `_swallow_cancel`)一個字都沒改**;`_BlockingStream` 只是
被 node 5 沿用。新的 `_CountingStore` / `_HeldStream` / `_prime_final_chunk` / `_no_probe` /
`_no_request` / `_wire_call` / `_synthetic_history` 全部是新增,放在檔尾的預熱區塊。

`tests/test_context_budget.py` 只在檔尾**新增**一條 node,既有內容零變更。

## 5. 已執行的命令與結果

```
$ git rev-parse HEAD
f200f697ba54d38a102e8ef66dead652c4002e5f

$ python3 -m py_compile client_engine.py config.py llama_client.py context_budget.py \
      tests/test_client_engine.py tests/test_context_budget.py
COMPILE_OK          # 零輸出即成功

$ git status --porcelain=v1 --untracked-files=all
 M client_engine.py
 M config.py
 M context_budget.py
 M llama_client.py
 M tests/test_client_engine.py
 M tests/test_context_budget.py

$ git diff --stat            # 見 §2
$ git diff --binary > /tmp/codetrail-startup-ttft-20260907/patches/lane-b.patch
```

**沒有執行任何測試**(依施工單:本 lane 任何測試都不允許執行,smoke / full 由整合者與
Astra 負責)。沒有對任何 server 發請求、沒有部署變更 / 重啟、沒有讀私人 session、
沒有 `git commit` / `push`、沒有動 `.gitignore` 或別人 owned 的檔。

## 6. 計畫與實際程式不合之處(全部照實作,不縮減驗收)

1. **§4.2「確認最後一則仍是 placeholder(以 `is` 比對)」在實際程式上做不到。**
   `strip_historical_reasoning()` 與 `prune_old_tool_outputs()` 的第一行都是
   `out = [dict(message) for message in messages]` —— 兩個轉換都會重建每一則 dict,
   物件 identity 一定對不上,`is` 比對等於**永遠**失敗(或者被寫成永遠通過的死碼)。
   改用**標記鍵** `_PRIME_PLACEHOLDER_KEY`:佔位訊息帶一個私有 key,轉換後檢查
   `working[-1].get(_PRIME_PLACEHOLDER_KEY)`。這比位置檢查更強(不只確認「最後一則
   是 user」,而是確認「最後一則就是我掛上去的那一則」),而且能穿過 dict 複製。
   不合條件時丟 `EngineError`(呼叫端記成 `error:EngineError` 並跳過這次預熱),不送
   任何可能含佔位內容的 payload。標記鍵在 `to_wire()` 之前就隨整則訊息被 `pop()` 掉,
   所以**不會**出現在任何 payload 裡(node 1 有斷言釘住)。

2. **`gate` 這個 reason 在 `options.max_output_tokens >= 1` 時實際上不可達。**
   兩次計算吃的是**同一份** payload,第一次的保留額是 `max_output_tokens`(≥ 1)、
   第二次是 1,所以只要第二次會溢位,第一次一定先溢位並回 `next_turn_would_overflow`。
   `gate` 因此是防禦性分支(保留 `check_and_log` 作為這條路徑上唯一的閘入口),
   依 AGENTS.md §1.4 不為它寫儀式性測試;`next_turn_would_overflow` 有測試釘住。

3. **§4.2 的 `PrimeOutcome.reason` 值域裡沒有給 `reason=` 參數留落點。**
   `prime_prompt_cache(reason=...)` 的 `reason` 在 engine 內**刻意不被記錄**
   (零寫入包含 telemetry:`source=prime` 那一列沒有 reason 欄位)。已在 docstring
   寫明它只是呼叫端的標記,避免下一個人以為漏了。協調器 / `/status` 的顯示由 Lane C 負責。

4. **`abort_prime()` 與預熱的 `finally` 之間的「誰關那個串流」**在 §4.2 沒有明講。
   實作把責任放在 `abort_prime()`:它在 `_prime_guard` 內取走 `_prime_stream` **並清成
   None**,預熱的 `finally` 因此只會關到自己還握著的那一個。這是 §6 node 5 的
   `closed_from` 長度 1 能成立的原因(雙方各關一次的話會變成 2)。

5. 模組 docstring 從「三件事」改成「四件事」並加第 4 條:§4.4 只交代 Lane D 改
   `AGENTS.md` §2,沒有提 `client_engine.py` 自己的 docstring。這是本 lane owned 的檔,
   加一條讓「唯一沒有使用者訊息就打模型的路徑」在模組頂端就看得到;**沒有**任何測試
   釘這份 docstring(已確認 `tests/test_repo_consistency.py` 不掃它)。

## 7. 未做 / 明確不在本 lane

- `client_turns.py` / `client_app.py`(`prime_in_background`、狀態列相位、`/status` 那一行)= Lane C。
- `client_preflight.py` / `codetrail_chat.py` 的 banner = Lane A;`tests/test_client_cli.py` 的
  headless 契約(B06 後半)也是 Lane A。
- `README.md` / `docs/setup.md` / `docs/troubleshooting.md` / `README_DEV.md` / `AGENTS.md` = Lane D。
- `tests/test_smoke_gate.py` 的 `SAFETY_MODULES` 登記 = 整合者(§3 已列出 9 個 node 名)。
- §8 的 T0 量測 = David;施工階段**沒有**任何量測數字。

## 8. 誠實揭露(§5 B9 固定句)

reasoning 與硬體 prefill 成本未變;預熱只把**下一輪 prefix** 的 prefill 搬到打字之前;
熱 prefix 時無感;是否真的被重用只由 T0 的 `prompt_tokens_processed` 判定,規劃與施工
階段沒有數字。
