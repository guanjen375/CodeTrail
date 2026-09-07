# Step 5 R1 回修 — 分工與 Dependency(Fable 5.1 MAX 主導)

- 撰寫者:Claude Fable 5.1(harness 自報 model ID `claude-fable-5-1`),同時是 **engine lane** 的施工者。
- 日期:2026-09-07。輸入:`AGENTS.md`、`03-plan-final.md`、`04-impl-b.md`、`04-impl-c.md`、`04-integration.md`、`05-review-astra-r1.md`。
- 產品 base HEAD `f200f697ba54d38a102e8ef66dead652c4002e5f`;本檔撰寫時 `git rev-parse HEAD` = `71217dc3fcc1dcf485b91fd4953230216ed107b5`(只有交接 markdown);21 個產品 / 測試路徑 staged、未 commit;產品 digest `6e92b5cf7639e13e83b62b7ca172677c72a3d839427e68e1f928ca8f31d8c7da`。
- 本檔是**執行交接**,不是新總計畫。01 / 02 / 03 / 05 不改。修法與介面調整以本檔與兩份 lane 報告交 Astra 下一輪。

## 1. 分工定案

| lane | Blocker | 施工者 | 產品可寫 | 測試可寫 | 交付報告 |
|---|---|---|---|---|---|
| **engine**(本人保留) | R1-B01、R1-B02、R1-B03、R1-B05 | Fable 5.1 MAX(本 session) | `client_engine.py`;**保留給本 lane、需要才動**:`llama_client.py`、`context_budget.py` | `tests/test_client_engine.py`;保留:`tests/test_context_budget.py` | `06-fix-r1-engine.md` |
| **status**(root 另起一個 Fable 5.1 MAX) | R1-B04 | 另一個 Fable 5.1 MAX | `client_turns.py`、`client_app.py` | `tests/test_client_turns.py`、`tests/test_client_app.py` | `06-fix-r1-status.md` |

**Dependency:兩個 lane 互相獨立,可同時開工。**

- status lane 對 engine 的依賴只有 `PrimeOutcome` 的**形狀**(`sent` / `reason` / `processed_tokens`),而且產品端一律 `getattr`、測試端一律 `types.SimpleNamespace` 替身(Lane C 已經是這樣寫)。engine lane 新增的 reason 值(`incomplete`、`no_timings`,見 §4)對 status lane 只是另一個字串,`/status` 的 `skipped(<reason>)` 原樣顯示,不需要對表。
- engine lane 不依賴協調器 / TUI:B02 的中止介面(`abort_prime()` 由 `new_session()` / `adopt()` 呼叫)、B03 的 heal 順序、B05 的終結判定全在 `client_engine.py`;協調器的 `prime_in_background()` 簽名不變。
- 兩個 lane 的可寫路徑**兩兩不重疊**;在同一個主工作樹並行,靠路徑互斥。
- 不可平行的部分(整合者):`tests/test_smoke_gate.py` 登記兩個 lane 的新 node、`README_DEV.md` / `docs/troubleshooting.md` / `AGENTS.md` 的字句同步、`06-fix-r1.md` 三件套、`07-deferred.md`。這些等兩份 lane 報告齊了才做。

## 2. 共用檔 owner(兩個 lane 都不得碰)

| 檔 | owner |
|---|---|
| `tests/test_smoke_gate.py`、`README_DEV.md`、`docs/troubleshooting.md`、`AGENTS.md`、`README.md`、`docs/setup.md`、`06-fix-r1.md`、`07-deferred.md` | 後續 Fable 整合者 |
| `00-model-log.md` | root |
| `06-fix-r1-dependencies.md`(本檔) | engine lane(本人);status lane 只讀 |
| `01/02/03/04/05-*.md` | 歷史文件,不改 |
| 其他任何產品檔(`client_preflight.py`、`codetrail_chat.py`、`config.py`、`client_compaction.py`、`client_policy.py` …) | 本輪無 owner = 不得動 |

若任一 lane 發現必須擴可寫檔:先在自己的交付報告寫清「要動哪個檔、為什麼、與另一 lane 的路徑不重疊」,不得直接動另一 lane 或整合者的檔。

## 3. 兩個 lane 共同的規則(逐字照做)

- 工具:Read / Glob / Grep / Write / Edit / Bash。Bash 只准:`git diff -- <自己的路徑>`、`git status --short`、`git rev-parse HEAD`、`python3 -m py_compile <自己的檔>`、下面 §3 的單 node 測試命令。**不准**:`git add` / `commit` / `push` / `reset` / `checkout` / `stash`、`compileall .`、`check_readme_consistency`(兩個 lane 都不改文件,不需要)、任何 `-m smoke` / full / 整檔 pytest / 直呼 `pytest` / 其他會間接收集測試的命令。
- 所有 HTTP / LLM 一律 mock(`monkeypatch.setattr(llama_client, ...)` 或替身 engine)。禁止對 :8080 / :8081 / :8082 / :8083 發任何請求、讀私人 session、改部署、新增 `os.environ` 讀取、`process_env` 以外的 spawn。
- 測試(AGENTS §1.2–§1.5):
  - **真實 bug** → 新 `@pytest.mark.smoke` regression node,先在**未修**的產品上單跑那一條、留真 `AssertionError` 紅燈,再修產品、同一條轉綠。只准跑自己新寫的那一條 node,一紅一綠(修測試本身的筆誤可再跑,每次都要記)。
  - **契約**(無聲失敗風險)→ 只寫不跑,交 reviewer 的 full。
  - 既有測試若動到:逐條列檔 / node / 理由;不得刪弱斷言、不得 skip / xfail 讓綠。
  - 沒有實際執行的東西,報告如實寫「未執行」。不得把靜態推演冒充紅燈。
- 單 node 測試命令的**固定形狀**(stdout+stderr 完整落檔、exit code 不被 pipeline 吃掉):

  ```bash
  python3 scripts/run_tests.py "tests/test_client_app.py::test_<node>" \
    > /tmp/codetrail-startup-ttft-20260907/<node縮寫>-red.txt 2>&1; B04_RED=$?; \
  echo "exit=$B04_RED" >> /tmp/codetrail-startup-ttft-20260907/<node縮寫>-red.txt; exit $B04_RED
  ```

  之後用 Read 看那個檔,把紅 / 綠節錄(含 `AssertionError` 那幾行與 `1 failed` / `1 passed`)貼進報告;綠燈同一形狀、檔名 `-green.txt`。變數名各 lane 自取(`B04_RED` / `B04_GREEN` / `B03_RED` …),不要重用。
- 交付時 `git diff -- <自己的路徑>` 只看自己的檔;產品維持「staged 舊版 + 自己的 unstaged 改動」,不 stage;整合者之後統一 stage / freeze。
- 報告固定段落:Blocker 逐項處理、改檔清單、新 node 名 + 紅 / 綠命令 / exit / 節錄、需要整合者登記進 `SAFETY_MODULES` 的 node、動到的每個既有測試(檔 / node / 理由)、未做與風險、給另一 lane / 整合者的介面說明(含文件要改的字句)。

## 4. engine lane(本人)— 修法摘要(細節在 `06-fix-r1-engine.md`)

只寫結論,讓 status lane 與整合者知道介面會變成什麼;不需要 status lane 做任何事。

- **B01**:`test_priming_skips_only_when_no_slot_is_idle_and_never_raises_or_prints` 末段在替換 `get_session` 之前把 `llama_client.get_slots` 還原成**真的**函式(測試開頭先捕捉),quiet / 預設兩次 probe 真的走產品的 `get_slots`,只 mock 底層 session。兩側斷言(quiet 零 stderr、預設印 `/slots probe failed`)一字不改。這是測試缺陷不是產品 bug,不新增 node、不執行(依 §3;reviewer 的 full 執行)。
- **B02**:`/slots` probe 與 POST 各自在小執行緒發、主流程每 50 ms 看一次中止旗標(與既有 `_open_stream` 同一手法,**不借** `_cancel`);中止落在 probe → 立刻回 `aborted`、放鎖(GET 不占 slot,放鎖不破壞模型序列化);落在 probe 回來之後、POST 之前 → 不發 POST;落在 POST 已送出、headers 未到 → 回 `aborted`、`priming=False`,但**模型鎖跟著那個請求走**(`_ModelSlot.hand_off`),由背景在 response 到達並關掉之後 `release_late()`——這是 Astra 明訂「不得提早放掉仍有舊 HTTP 在跑的共用模型鎖」的唯一合規做法,也是既有回合取消的同一規則;落在串流中 → 關 socket(既有)。`new_session()` / `adopt()` 的狀態切換改在 `_turn_state` 內做並讓任何在切換前取的 snapshot 失效(世代號),所以「排程晚到的預熱」不會拿舊歷史送出。B7 的三個條件在 probe / POST 前 / 串流中三種情況都以離線 barrier 測試釘住;POST 在飛那一種,「鎖已放」的時間 = 舊請求 headers 到達的時間,報告會如實揭露。
- **B03**:`heal_in_place()` 把補的「已中斷」結果放在**該群組既有結果之後**(宣告順序不變),與 `heal_pending_tool_calls()` 在 `send()` 時 append 的順序一致;既有「結果緊接宣告群組」的契約不變。新 regression:`assistant(a,b) → tool(a)` 的部分完成群組先 prime 再真 `send(q)`,斷言 payload 逐字等於 prefix + user。
- **B05**:預熱迴圈記錄有沒有看到終結 chunk(`choices[0].finish_reason`);沒看到就回 `PrimeOutcome(False, "incomplete", None)`,看到但 `timings.prompt_n` 缺 → `PrimeOutcome(False, "no_timings", None)`;兩者都**不**寫 telemetry。`finish_reason=length` + timings 的正常情境照舊 `sent`。新 regression 覆蓋空串流、非終結 delta 後 EOF、終結但無 timings 三種。
- 介面異動(整合者要同步文件的字句,完整版在 engine 報告):`PrimeOutcome.reason` 多 `incomplete` / `no_timings`;`abort_prime()` docstring 改寫;`_ModelSlot` 多 `try_lease()` / `release()`;`heal_in_place()` docstring 改「補在該群組既有結果之後」。`docs/troubleshooting.md` 跳過原因表要加兩列、`README_DEV.md` 的 `abort_prime` 一列改寫。

## 5. status lane — 完整可執行交接(R1-B04)

### 5.1 任務目標

Astra R1-B04:壓縮(自動與 `/compact`)成功換掉歷史之後,協調器自己排的那一次預熱沒有 `on_done`,outcome 直接丟掉;`/status` 的「prompt cache 預熱」仍顯示上一次(mount / new / session)的結果與時間,使用者無法按 `03-plan-final.md` §5 C 判斷這一次是 skipped 還是 sent、也拿不到原因。要求:**協調器跑的每一次預熱**的 outcome 都回到 TUI,`/status` 顯示最近一次的結果、原因、時間與觸發點;仍在回合鎖外排程,不產生對話事件、不動 busy / cancel;以可區分的先後 outcomes 核對自動壓縮與 `/compact` 兩個入口。

### 5.2 介面契約(由本人定案;status lane 照此實作,不另設計)

```python
# client_turns.py
class TurnCoordinator:
    def __init__(self, engine, *, emit, on_approval=None, on_approval_closed=None,
                 on_reasoning=None, compactor=None, approval_timeout=...,
                 on_prime: Callable[[str, Any], None] | None = None) -> None: ...
        # on_prime(reason, outcome):協調器**每一次**真的跑完 prime_prompt_cache 之後呼叫,
        # 不分誰排的(TUI 的 mount / new / session,或協調器自己的 compaction)。
        # 在預熱那條背景執行緒上呼叫;outcome 是 engine 回的 PrimeOutcome 形狀,engine
        # raise 時是 None。on_prime 的例外一律吞掉(BaseException,與 on_done 同理)。
        # 呼叫順序:先 on_prime,再(有給的話)on_done。

    def prime_in_background(self, reason: str, *, on_done: Callable[[Any], None] | None = None) -> bool
        # 簽名不變(§4.3 凍結);on_done 仍是每次呼叫可選的回呼。TUI 從此不再傳 on_done。
```

```python
# client_app.py
class CodeTrailApp:
    # __init__:TurnCoordinator(..., on_prime=self._prime_from_worker)
    def _prime_from_worker(self, reason: str, outcome: Any) -> None:
        self._from_worker(self._note_prime, reason, outcome)
    def _note_prime(self, reason: str, outcome: Any) -> None:
        self._last_prime = (time.time(), reason, outcome)      # 三元組:時間、觸發點、outcome
    def _prime(self, reason: str) -> None:
        self.coordinator.prime_in_background(reason)            # 不再傳 on_done
```

`/status` 那一行的**固定格式**(整合者按這個改 `docs/troubleshooting.md` 的表):

| 狀況 | 字串 |
|---|---|
| 還沒排過 | `prompt cache 預熱=尚未` |
| 送出 | `prompt cache 預熱=sent <HH:MM:SS> 觸發=<mount\|new\|session\|compaction>` |
| 跳過 | `prompt cache 預熱=skipped(<reason>) <HH:MM:SS> 觸發=<…>` |
| engine 破了不 raise 的契約(outcome 為 None) | `prompt cache 預熱=error <HH:MM:SS> 觸發=<…>` |

其他不變:`_prime` 的三個呼叫點(`on_mount` / `_cmd_new` / `_switch_session`)、`_run_turn` / `_run_compaction` 在 `finish_turn()` **之後**呼叫 `prime_in_background("compaction")`、狀態列相位、`prompt cache 預熱中`。**不得**讓預熱進對話區、進事件流、改 `busy` / `cancelled`、取 `_turn_lock`。

### 5.3 可寫 / 不可寫

- 可寫:`client_turns.py`、`client_app.py`、`tests/test_client_turns.py`、`tests/test_client_app.py`、`docs/workflows/startup-ttft-20260907/06-fix-r1-status.md`。
- 不可寫:§2 的全部共用檔;engine lane 的 `client_engine.py` / `llama_client.py` / `context_budget.py` / `tests/test_client_engine.py` / `tests/test_context_budget.py`;`codetrail_chat.py`(協調器只在 `client_app.py` 建,不需要);本檔。
- 產品端對 engine 只用 `getattr`;測試替身不 import `client_engine.PrimeOutcome`(沿用 `types.SimpleNamespace`)。

### 5.4 測試

1. **red-before-green regression(真實 bug,必跑一紅一綠)**
   `tests/test_client_app.py::test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction`
   - 替身 engine:沿用檔內 `_Engine`,子類讓 `prime_prompt_cache(reason=)` 依 reason / 呼叫序回**可區分**的 outcome,例如 `mount` → `SimpleNamespace(sent=True, reason="", processed_tokens=7)`、第一次 `compaction` → `SimpleNamespace(sent=False, reason="server_busy", processed_tokens=None)`、第二次 `compaction` → `SimpleNamespace(sent=False, reason="model_busy", processed_tokens=None)`;每次呼叫 set 一個 Event 讓測試等。
   - 壓縮器替身:`types.SimpleNamespace(mode="codetrail", pending_stop_notice=lambda: "", rebind=lambda: None, compact=lambda manual=False: types.SimpleNamespace(status="compacted", message="已壓縮"))`(比照檔內第 1149 行的寫法)。
   - 流程:`app.run_test()` → 等 mount 的預熱與 `_last_prime` 落地 → `app._command("/status")` 抄下對話區最後一則 notice,必須含 `sent` 與 `觸發=mount`;→ `app._command("/compact")` → 等終結事件與第一次 compaction 預熱 → `/status` 必須含 `skipped(server_busy)` 與 `觸發=compaction`;→ `app.submit("hi")`(`_Engine.send` 回 `finish="stop"`,自動壓縮走 `compacted`)→ 等第二次 compaction 預熱 → `/status` 必須含 `skipped(model_busy)` 與 `觸發=compaction`。
   - 另斷言:對話區的 notice 沒有任何一則含「預熱」以外的預熱字樣(即預熱不進對話區,只有 `/status` 那一則含它);`app.coordinator.busy is False`(預熱在回合鎖外);`engine.cancelled is False`。
   - 未修產品上的紅燈原因:`/compact` 之後 `/status` 仍是 `sent …`(mount 那一次),`skipped(server_busy)` 不在字串裡 → `AssertionError`(行為紅燈,不是缺方法:未修的 `_prime_status()` 不認 `觸發=`,但斷言先撞到的是 outcome 沒更新;報告要貼實際訊息)。
2. **契約(只寫不跑,smoke)**
   `tests/test_client_turns.py::test_every_prime_the_coordinator_runs_reports_through_on_prime`
   - `_coordinator(engine, on_prime=recorder)`;閒置時 `prime_in_background("mount")` → recorder 收到 `("mount", outcome)`;`on_done` 同時給的話也被叫、而且在 `on_prime` 之後;`start_compaction()` 走 `compacted` 的壓縮器 → recorder 收到 `("compaction", outcome)`,而且當時 `coordinator.busy is False`;engine 的 `prime_prompt_cache` raise → recorder 收到 `("mount", None)`;`on_prime` 自己 raise → 預熱執行緒正常結束、`on_done` 照樣被叫、不冒泡。
3. 相位顯示仍零測試;既有 `status_text` 斷言不得變動。

### 5.5 預期會動到的既有測試(§1.5 逐條列進報告)

- `tests/test_client_app.py::test_the_tui_primes_on_mount_new_and_session_switch_through_the_coordinator`:`noted[1].sent` → `noted[2].sent`(`_last_prime` 從二元組變三元組,多了觸發點);可加一句 `noted[1] == "mount"`。理由:記錄多了觸發點,`/status` 才講得出「這一次是壓縮後的預熱」。零其他斷言變更。
- `tests/test_client_turns.py` 的 `_Engine` 替身若需要「依呼叫序回不同 outcome」,用**子類**做,不改共用替身的既有行為。
- 其餘既有 node 零變更。若真的必須改別的,報告要說得出行為為什麼該變。

### 5.6 交付報告 `06-fix-r1-status.md` 必含

§3 的固定段落,外加:實際的 `/status` 字串範例(sent / skipped / error 各一)、給整合者的 `SAFETY_MODULES` 登記清單(`test_client_app.py` 1 條、`test_client_turns.py` 1 條,既有檔名鍵)、要整合者改的文件字句:
- `docs/troubleshooting.md` 第 222–229 行:`/status` 表三列加 `觸發=<…>`,第 229 行改成「這一行反映**每一次**預熱:啟動、`/new`、換 session,以及自動壓縮 / `/compact` 換掉歷史之後那一次;`觸發=` 告訴你是哪一種」;
- `README_DEV.md` 第 646 行 `prime_in_background` 那一列補「協調器層的 `on_prime(reason, outcome)` 回呼:每一次預熱(含壓縮後)都回到 TUI」;
- `AGENTS.md` §2 `client_turns` 條那一句後面補「;每一次預熱的 outcome 經 `on_prime` 回到 TUI,不分誰排的」。

## 6. 整合者(兩份 lane 報告齊了之後)

1. 兩個 lane 的產品 / 測試 unstaged 改動一起看:`git diff --stat`、`python3 -m compileall -q .`。
2. `tests/test_smoke_gate.py` 登記兩個 lane 報告列出的新 node(既有檔名鍵)。
3. 文件同步:§4 與 §5.6 列出的字句(`README_DEV.md`、`docs/troubleshooting.md`、`AGENTS.md`);之後 `python3 scripts/check_readme_consistency.py`。
4. `06-fix-r1.md`:Blocker 對照、§1.5 彙整、三件套(`git add -A -- . ':(exclude)docs/workflows'` → `git status --porcelain=v1 --untracked-files=all` 全為 `M ` → `git rev-parse HEAD` / `git write-tree` / 產品 digest)。**不跑** smoke(上一輪整合者已跑;本輪 Astra 靜態歸零後對新 freeze 執行 full)。
5. `07-deferred.md`:engine 報告會列「POST 在飛時鎖跟著請求走」是否算 B7 的偏離,交 Astra 裁定。

READY_FOR_PARALLEL_EXECUTION
