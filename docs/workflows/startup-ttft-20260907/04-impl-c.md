# startup-ttft-20260907 — Lane C 施工報告(協調器與 TUI)

- 施工者:Claude Opus 5.0(CLI 指定 `claude-opus-5` / effort max;模型身分由主代理以 CLI init / modelUsage 另核對並記進 `00-model-log.md`)。
- 日期:2026-09-07。worktree:`/tmp/codetrail-startup-ttft-20260907/worktrees/c`。
- base HEAD:`f200f697ba54d38a102e8ef66dead652c4002e5f`(`git rev-parse HEAD` 實測相同;本 lane 未 commit、未 push)。
- 範圍:`03-plan-final.md` §4.3、§5 B(B2 / B6 的協調器與 TUI 那一半)、§6 Lane C。

---

## 1. Dependency

- 只依賴 §4.2 凍結的**介面名稱**:`Engine.prime_prompt_cache(reason=…)`、`Engine.priming`、`PrimeOutcome(sent, reason, processed_tokens)` 的欄位形狀。
- 本 worktree **沒有** Lane B 的實作,產品端全部走 `getattr` 相容:
  - `TurnCoordinator.prime_in_background()` 以 `getattr(self.engine, "prime_prompt_cache", None)` + `callable()` 判定,沒有就回 `False`(不是例外)。
  - `CodeTrailApp._refresh_status()` 以 `getattr(self.engine, "priming", False)` 判定。
  - `CodeTrailApp._prime_status()` 以 `getattr(outcome, "sent"/"reason", …)` 讀 outcome,不 import `client_engine.PrimeOutcome`。
- 測試端同樣不 import `PrimeOutcome`:兩個替身回 `types.SimpleNamespace(sent=…, reason=…, processed_tokens=…)`(只複述形狀;協調器只把 outcome 原樣交給 `on_done`,不看裡面)。
- 因此本 lane 的 patch 單獨套在 base 上就可編譯、可跑;Lane B 到位之後不需要再改 Lane C 的檔。

## 2. 可寫檔案與 owner

| 檔 | owner | 本 lane 有沒有動 |
|---|---|---|
| `client_turns.py` | Lane C(唯一) | 有 |
| `client_app.py` | Lane C(唯一) | 有 |
| `tests/test_client_turns.py` | Lane C(唯一) | 有 |
| `tests/test_client_app.py` | Lane C(唯一) | 有 |
| `docs/workflows/startup-ttft-20260907/04-impl-c.md` | Lane C(唯一交接檔) | 有(本檔) |
| `tests/test_smoke_gate.py` | 整合者 | **沒有**(新 node 見 §5,請整合者登記) |

`git status --porcelain=v1 --untracked-files=all` 只有上表前四個檔的 ` M`,沒有其他產品變更、沒有 untracked 檔。

## 3. 實際修改的產品檔

### 3.1 `client_turns.py`

1. **新方法 `TurnCoordinator.prime_in_background(reason, *, on_done=None) -> bool`**(§4.3 凍結簽名)。
   - engine 沒有 `prime_prompt_cache`(舊 engine / 替身)或 `self.busy` → 回 `False`,**不 spawn**。
   - 否則 daemon thread(名字 `codetrail-prime-<session id>`,沿用既有 `_spawn`)呼叫 `engine.prime_prompt_cache(reason=reason)`;engine 端的例外一律吞掉(outcome 記成 `None`);有 `on_done` 就在同一條執行緒上回呼。
   - **不取 `_turn_lock`、不動 `_turn_done` / `_cancelled`**,所以 `busy` 不會因為預熱變 True、`cancel()` 對預熱是 no-op。
2. **`_auto_compact()` 改回傳 outcome**(沒壓 / 壓不成回 `None`);行為與既有 notice 完全不變,只是把結果交給呼叫端。
3. **`_run_turn()`**:記下 `compacted = getattr(outcome, "status", None) == "compacted"`,在 `finally: self.finish_turn()` **之後**(try 陳述式外)`self.prime_in_background("compaction")`。
4. **`_run_compaction()`**(`/compact`):同樣在 `finish_turn()` 之後、對 `status == "compacted"` 呼叫。早退的兩條路徑(`compactor is None`、`compact()` 丟例外)`compacted` 維持 `False`,所以那兩條 `return` 略過後段不影響語意。
5. 模組 docstring 加一段「預熱不是一輪」。

### 3.2 `client_app.py`

1. **呼叫點**(全部經協調器,engine 沒有能力時是 no-op):
   - `on_mount`:`_replay_startup_session()` 之後、`_recount_context()` 之前 → `"mount"`。
   - `_cmd_new`:`coordinator.session_changed()` 之後 → `"new"`。
   - `_switch_session`:`coordinator.session_changed()` 之後 → `"session"`。
2. **`_prime(reason)` / `_note_prime(outcome)`**:`on_done` 經既有 `_from_worker` 搬回 UI 執行緒,只記 `self._last_prime = (time.time(), outcome)`;**不動對話區、不發事件、不落檔**。
3. **`/status` 追加一行** `prompt cache 預熱=…`:`尚未` / `sent <HH:MM:SS>` / `skipped(<reason>) <HH:MM:SS>`。不呼叫 engine 的任何方法(B08:沒有 `system_prompt.text`、沒有 `openai_tools()`、沒有 prefix 大小行)。
4. **狀態列相位**(`_refresh_status` 只**追加** parts):
   - `_turn_started` 非 None 時,在 spinner 那段後面接一段相位:`等待首個 token` / `thinking {n} 段` / `回答中`;`/compact` 那一輪固定 `壓縮中`。
   - `getattr(engine, "priming", False)` 為 True 時在最後加 `prompt cache 預熱中`。
   - 計數只由 `_on_reasoning` / `TYPE_TEXT_DELTA` / `TYPE_TEXT` 累加(`_reasoning_chunks` / `_answer_started`),`submit()` / `_cmd_compact()` 以 `_reset_phase()` 歸零,終結 `step_finish` 清除。**閒置時不顯示相位**,既有 `status_text` 斷言(含 `/thinking` 那條)因此完全不受影響。

## 4. 安全邊界的落點(對照 AGENTS.md §2 `client_turns` / `client_app` 條)

- 既有的三個取消狀態、核准三條、慢速 MCP 取消在鎖外、notice 在終結事件之前 —— 一行都沒有動,對應的既有測試斷言零變更。
- 新的邊界(由本 lane 的兩條 turns 契約守):預熱不進 `busy`、不被 `cancel()` 認得、回合進行中不得排、engine 沒有能力就跳過;壓縮換掉歷史才預熱,而且只在**放掉回合鎖之後**。
- 畫面契約:預熱**不進對話區**(app 契約以 `notices` 不含「預熱」釘住),核准框 / 重播 / 輸入歷史那幾條完全沒動。
- 沒有新增 `os.environ` 讀取、沒有 `process_env` 以外的 spawn(只有既有 `threading.Thread` 的 `_spawn`)、沒有新的 HTTP 呼叫點(HTTP 全在 engine 端)。

## 5. 新增的測試 node(全部帶 smoke;兩個檔都有 module 層 `pytestmark = pytest.mark.smoke`)

| 檔 | node | 守什麼 |
|---|---|---|
| `tests/test_client_turns.py` | `test_a_compaction_that_replaced_the_history_primes_after_the_turn_lock_is_released` | 壓縮真的換掉歷史(`status == "compacted"`)才預熱,`skipped` / `stopped` / `failed` 不呼叫;自動壓縮與 `/compact` 兩條路都涵蓋;預熱發生在**放掉回合鎖之後**(替身記下當下的 `coordinator.busy`,必須是 `False`) |
| `tests/test_client_turns.py` | `test_priming_is_invisible_to_busy_and_cancel_and_refused_while_a_turn_runs` | 閒置時排得出去(回 True)但 `busy` 仍 False、`cancel()` 回 False 且 engine 取消旗標沒被動到;回合進行中回 False 且**連 spawn 都沒有**;engine 沒有 `prime_prompt_cache` 時是跳過不是例外 |
| `tests/test_client_app.py` | `test_the_tui_primes_on_mount_new_and_session_switch_through_the_coordinator` | mount / `/new` / `/session <id>` 三個時刻各預熱一次且 reason 正確;`on_done` 有搬回 UI 執行緒(`_last_prime`);對話區不得多出任何「預熱」字樣;回合進行中的 `/new` 被擋下時不得偷排;`engine.priming` 為 True 時 `status_text` 含「預熱」,結束後不含 |

**請整合者登記進 `tests/test_smoke_gate.py`**:`test_client_turns.py` 2 條、`test_client_app.py` 1 條(檔名鍵已存在,不需新增鍵)。本 lane 依規定沒有碰那個檔。

相位顯示(`等待首個 token` / `thinking n 段` / `回答中` / `壓縮中`)依 §6「相位顯示零測試(UI 便利)」不寫測試。

## 6. 動到的既有測試(逐條)

沒有任何既有斷言被改動、放寬或刪除。動到的是**共用替身與註解**:

| 檔 | 位置 | 改了什麼 | 理由 |
|---|---|---|---|
| `tests/test_client_turns.py` | 模組 docstring | 加兩行:預熱的兩條契約也在這個檔 | 檔頭要說得出這個檔守哪些契約,不然新加的兩條看起來像放錯檔 |
| `tests/test_client_turns.py` | `import types` | 新增 stdlib import | 替身要回一個 `PrimeOutcome` 形狀的物件,又不能 import Lane B 還沒有的真型別 |
| `tests/test_client_turns.py` | 共用替身 `_Engine.__init__` | 新增 `coordinator` / `primes` / `priming` 三個屬性(全部只記錄) | §6 指定:替身要記下 `(reason, 當下的 coordinator.busy)`,才能證明預熱發生在放鎖之後。既有欄位與行為未改 |
| `tests/test_client_turns.py` | 共用替身 `_Engine` | 新增方法 `prime_prompt_cache(*, reason="")` | §4.2 的介面名稱;沒有這個方法的話協調器會直接跳過,兩條新契約都測不到 |
| `tests/test_client_turns.py` | 新增模組層 helper `_eventually` / `_joined` | 新增(不改既有 helper) | `_joined` 等 worker 執行緒真的結束,是「預熱有沒有被排出去」唯一不靠 sleep 的問法;`_eventually` 等背景執行緒記錄完成 |
| `tests/test_client_app.py` | 共用替身 `_Engine.__init__` | 新增 `primes` / `primed`(Event)/ `priming` | 同上;`primed` 讓測試等背景執行緒而不必 sleep |
| `tests/test_client_app.py` | 共用替身 `_Engine` | 新增方法 `prime_prompt_cache(*, reason="")` | 同上 |
| `tests/test_client_app.py` | 新增替身 `_SlowPrime(_Engine)` | 新增(只給新 node 用) | 要在「預熱進行中」那個瞬間讀狀態列,替身得先把 `priming` 設成 True 並停在那裡 |

**副作用揭露**:替身加了 `prime_prompt_cache` 之後,`tests/test_client_app.py` 的每一個 app 測試在 `on_mount` 都會多 spawn 一條背景執行緒(呼叫替身、append 一個字串、經 `call_from_thread` 記一次 `_last_prime`),`tests/test_client_turns.py` 的 `test_auto_compaction_runs_after_a_completed_answer` 也會多一次(它的 outcome 就是 `compacted`)。這幾個測試的斷言與畫面內容都不受影響(預熱不進對話區、不進事件流、不寫檔),但這是既有測試在**行為上**多出來的東西,列在這裡讓審核者知道。為了避免 app 收尾時 `call_from_thread` 以 `CancelledError`(BaseException)在背景執行緒噴 traceback,`prime_in_background` 的 `on_done` 回呼包在 `except BaseException` 裡(engine 呼叫本身仍是 `except Exception`)。

## 7. 已執行的命令與結果

| 命令 | 結果 |
|---|---|
| `python3 -m py_compile client_turns.py client_app.py` | OK(無輸出) |
| `python3 -m py_compile tests/test_client_turns.py tests/test_client_app.py` | OK(無輸出) |
| `python3 -m py_compile client_app.py client_turns.py tests/test_client_app.py tests/test_client_turns.py`(最終一次) | OK |
| `git status --porcelain=v1 --untracked-files=all` | 只有四個 ` M`(見 §2) |
| `git rev-parse HEAD` | `f200f697ba54d38a102e8ef66dead652c4002e5f` |
| `git diff --stat` | 見 §8 |
| `git diff --binary > /tmp/codetrail-startup-ttft-20260907/patches/lane-c.patch` | 27,815 bytes |

**沒有執行任何測試**(依指示:smoke / full 由整合者與 Astra 執行;pytest 不得直呼)。沒有對任何 live server 發請求、沒有部署變更 / 重啟、沒有讀私人 session、沒有 `git commit` / `push`、沒有改 `.gitignore`、沒有動別人 owned 的檔。

`git apply --check` 沒有跑:那不在本 lane 允許的命令清單裡。patch 由 `git diff --binary` 在乾淨的 worktree(只有這四個檔是 modified)產生。

## 8. patch 與 diff --stat

- patch:`/tmp/codetrail-startup-ttft-20260907/patches/lane-c.patch`(`git diff --binary`,base `f200f697…`)

```
 client_app.py              |  84 +++++++++++++++++++++++++++++
 client_turns.py            |  65 ++++++++++++++++++++--
 tests/test_client_app.py   | 109 +++++++++++++++++++++++++++++++++++++
 tests/test_client_turns.py | 131 +++++++++++++++++++++++++++++++++++++++++++++
 4 files changed, 384 insertions(+), 5 deletions(-)
```

## 9. 未做 / 偏離 / 給審核者的提醒

1. **計畫與實作一致,沒有縮減任何驗收條件。** 下列是微觀實作判斷,列出來讓審核者確認:
   - `/status` 那一行多一個 §4.3 沒有列的分支:`on_done` 拿到 `None`(engine 破了「不 raise」的契約)時顯示 `error <HH:MM:SS>`,不會顯示成 `sent`。§4.3 只列了 `sent` / `skipped(reason)` 兩種。
   - 相位是**獨立的一段 part**(插在 spinner 之後),不是接在 spinner 字串裡:`⠙ 3s(Ctrl-C 中斷) · 等待首個 token · <model> · …`。`" · "` 的分隔與既有 parts 一致。
   - `prompt cache 預熱中` 追加在 parts **最後**(§4.3 只寫「加」,沒指定位置)。
2. **已知的顯示落差(照計畫,不是漏做)**:壓縮後那一次預熱由協調器自己排(`_run_turn` / `_run_compaction` 呼叫 `prime_in_background("compaction")`,§4.3 凍結的形狀沒有 `on_done`),所以它**不會**更新 `/status` 的「上次預熱」——那一行只反映由 TUI 排的 mount / new / session 三種。要一起反映的話得讓協調器帶一個預設 `on_done`,那會改到凍結的介面,本次沒有做。
3. **時間戳用 `time.time()`(wall clock)**,`/status` 以本地時間顯示 `HH:MM:SS`;狀態列的 spinner 仍用 `time.monotonic()`(既有行為未動)。
4. **B9 的誠實揭露(照抄)**:reasoning 與硬體 prefill 成本未變;預熱只把**下一輪 prefix** 的 prefill 搬到打字之前;熱 prefix 時無感;是否真的被重用只由 T0 的 `prompt_tokens_processed` 判定,規劃與施工階段沒有數字。相位顯示只是把 thinking 那段時間**顯示出來**,不會縮短它。
5. **給整合者**:`tests/test_smoke_gate.py` 的三條登記(§5)是本 lane 唯一需要別人代勞的動作;沒登記的話 smoke 會綠,但這三條契約根本沒被 gate 認列。
