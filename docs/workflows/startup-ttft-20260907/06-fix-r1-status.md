# Step 5 R1 回修 — status lane 報告(R1-B04)

- 施工者:Claude Fable 5.1(harness 自報 model ID `claude-fable-5-1`,effort MAX),status lane。
- 日期:2026-09-07。輸入:`AGENTS.md`、`06-fix-r1-dependencies.md`(§5 為完整任務、§2 / §3 為 owner 與命令限制)、`05-review-astra-r1.md` R1-B04、`03-plan-final.md` §4.3 / §5 B6 / D9、`04-impl-c.md`。
- 動工時 `git rev-parse HEAD` = `71217dc3fcc1dcf485b91fd4953230216ed107b5`;交付時 = `a946ab9f813dc88381b0e3ce5ad83e3770a34b65`(施工期間 root 前進了 HEAD;本 lane 沒有 `git log` 權限,沒有檢視那個 commit 的內容,只確認產品路徑仍是 staged `M ` 加各 lane 的 unstaged 改動,產品未 commit)。
- 本 lane 沒有 `git add` / `commit` / `push` / `reset` / `checkout` / `stash`;產品維持「staged 舊版(Opus Lane C)+ 本 lane 的 unstaged 修正」,由後續整合者統一 stage / freeze。
- 沒有對 :8080–:8083 發任何請求、沒有讀私人 session、沒有部署變更、沒有新增 `os.environ` 讀取、沒有 `process_env` 以外的 spawn。

## 1. Blocker 逐項處理

### R1-B04 — 壓縮後預熱結果遺失,`/status` 保留上一筆 sent

**現象(未修)**:壓縮(自動與 `/compact`)成功換掉歷史之後,協調器在 `finish_turn()` 之後自己排 `prime_in_background("compaction")`,那一次沒有 `on_done`,outcome 在 `_body` 內直接丟掉;`_last_prime` 停在 mount / new / session 那一次,`/status` 仍顯示 `sent <舊時間>`。

**修法(依 `06-fix-r1-dependencies.md` §5.2 的介面契約,不另設計)**:

1. `client_turns.TurnCoordinator.__init__` 新增 keyword `on_prime: Callable[[str, Any], None] | None = None`。`prime_in_background()` 的背景執行緒在 `prime_prompt_cache(reason=)` 回來(或 raise → `None`)之後,**先**呼叫 `self._on_prime(reason, outcome)`,**再**呼叫這一次呼叫自己的 `on_done(outcome)`(可選)。兩邊各自包在 `except BaseException` 裡:一邊炸了另一邊照樣到,也不從預熱執行緒冒出來。`prime_in_background(reason, *, on_done=None) -> bool` 簽名不變;三個 TUI 呼叫點、`_run_turn` / `_run_compaction` 在 `finish_turn()` 之後呼叫 `prime_in_background("compaction")`、不取 `_turn_lock`、不動 `_turn_done` / `_cancelled`、`busy` 檢查——一行都沒有動。
2. `client_app.CodeTrailApp`:建協調器時傳 `on_prime=self._prime_from_worker`;`_prime_from_worker(reason, outcome)` 經既有 `_from_worker` 搬回 UI 執行緒到 `_note_prime(reason, outcome)`,記 `_last_prime = (time.time(), reason, outcome)`(二元組 → 三元組,多了觸發點);`_prime(reason)` 改成 `self.coordinator.prime_in_background(reason)`,不再傳 `on_done`。`_prime_status()` 依 §5.2 固定格式輸出 `尚未` / `sent <HH:MM:SS> 觸發=<…>` / `skipped(<reason>) <HH:MM:SS> 觸發=<…>` / `error <HH:MM:SS> 觸發=<…>`。狀態列相位、`prompt cache 預熱中`、對話區、事件流全部不動。
3. 對 engine 只用 `getattr`(`sent` / `reason`),不 import `PrimeOutcome`;engine lane 新增的 reason 值(`incomplete` / `no_timings`)只是另一個字串,`skipped(<reason>)` 原樣顯示。

## 2. 改檔清單(只有本 lane 的可寫路徑)

| 檔 | 改了什麼 |
|---|---|
| `client_turns.py` | 模組 docstring 加三行(每一次預熱經 `on_prime` 回報);`__init__` 加 `on_prime` 參數與 `self._on_prime`;`prime_in_background` docstring 加一段、`_body` 在 engine 回來後先 `on_prime` 再 `on_done`(各自吞 `BaseException`) |
| `client_app.py` | `__init__` 傳 `on_prime=self._prime_from_worker`;`_last_prime` 型別註記與註解改三元組;新增 `_prime_from_worker(reason, outcome)`(放在 worker → UI 那一段);`_prime` 不再傳 `on_done`、docstring 補一句;`_note_prime(reason, outcome)`;`_prime_status()` 三種結果都帶 `觸發=` |
| `tests/test_client_app.py` | 新增 helper `_until`、替身 `_PrimeOutcomes(_Engine)`、regression node(§3);既有 node 的 tuple 索引調整(§5) |
| `tests/test_client_turns.py` | 模組 docstring「兩條」→「三條」;新增契約 node(§3) |
| `docs/workflows/startup-ttft-20260907/06-fix-r1-status.md` | 本檔 |

`git status --short` 交付時:本 lane 四個檔為 `MM`(staged Opus 版 + 本 lane 的 unstaged 修正);`client_engine.py` / `tests/test_client_engine.py` 也是 `MM`(engine lane 的 unstaged 改動,本 lane 未碰);`docs/workflows/startup-ttft-20260907/00-intake.md` 為 ` M`(不是本 lane 的)。沒有 untracked 產品檔。

## 3. 新 node

### 3.1 regression(真實 bug,red-before-green,已跑一紅一綠)

`tests/test_client_app.py::test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction`(檔案 module 層 `pytestmark = pytest.mark.smoke`)

- 替身 engine `_PrimeOutcomes(_Engine)`:`mount` → `SimpleNamespace(sent=True, reason="", processed_tokens=7)`;第一次 `compaction` → `sent=False, reason="server_busy"`;第二次 `compaction` → `sent=False, reason="model_busy"`;每次先把 outcome 落到 `engine.outcomes` 再 set `primed`。
- 壓縮器替身:`types.SimpleNamespace(mode="codetrail", pending_stop_notice=lambda: "", rebind=lambda: None, compact=lambda manual=False: SimpleNamespace(status="compacted", message="已壓縮"))`。
- 流程:`run_test()` → 等 mount 預熱與 `_last_prime` 落地 → `/status` → `/compact` → 等 compaction 預熱(並在那一刻讀 `coordinator.busy`)→ 等落地(有上限,未修產品上不會發生)→ `/status` → `submit("hi")`(`finish="stop"`,自動壓縮走 `compacted`)→ 等第二次 compaction 預熱與落地 → `/status`。三次 `/status` 各取 `prompt cache 預熱=` 那一行。
- **檢查次序(本 lane 的 routine 判斷,root 指示可調)**:交接 §5.4 的措辭是「mount 的 `/status` 必須含 `sent` 與 `觸發=mount`」再往下走;但 `觸發=` 是這次才新增的顯示字串,照那個次序斷言的話,未修產品的紅燈會先撞到「`觸發=mount` 不在字串裡」,遮住 B04。所以三個 `/status` 字串先在 `run_test` 內全部收齊,**離開 pilot 之後第一條斷言就是 B04 本體**(`skipped(server_busy)` 在 `/compact` 之後那一行),第二條是自動壓縮那一次(`skipped(model_busy)`),之後才判 `觸發=mount` / `觸發=compaction`、「含『預熱』的 notice 恰好就是那三則 `/status`」、`busy is False`、`engine.cancelled is False`。
- 等待用的 helper `_until(pilot, predicate, timeout=5.0)` 以 `await pilot.pause(0.01)` 迴圈等背景執行緒:worker 的每一則事件都經 `call_from_thread` 搬進 UI 執行緒而且會等它跑完,在 UI 執行緒上 `Event.wait()` 會讓 worker 永遠搬不過來(既有 node 之所以能直接 `primed.wait(5)`,是因為 mount 那條路在 set `primed` 之前沒有經過 `call_from_thread`)。到期回 False、不 raise,「落地」那兩次等待在未修產品上會用滿 5 秒然後由 `/status` 字串判——這就是紅燈跑 12 秒、綠燈跑 2 秒的原因。

**紅燈(未修的 `client_turns.py` / `client_app.py`)**

命令形狀偏離交接 §3 一處,如實記錄:交接的固定形狀含 `B04_RED=$?` 與 `exit $B04_RED`,本 harness 的 Bash 沙箱拒絕任何 `$` 變數展開(回 `Contains simple_expansion`),所以拆成兩步——先以同樣的重導向跑測試(stdout+stderr 完整落檔,工具回報 exit 狀態),再以另一個 `echo "exit=<工具回報值>" >> 檔` 補進檔尾。`exit=` 那一行是依工具回報的狀態手寫的,不是 shell 變數。

```bash
python3 scripts/run_tests.py "tests/test_client_app.py::test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction" \
  > /tmp/codetrail-startup-ttft-20260907/b04-red.txt 2>&1        # 工具回報 Exit code 1
echo "exit=1" >> /tmp/codetrail-startup-ttft-20260907/b04-red.txt
```

`/tmp/codetrail-startup-ttft-20260907/b04-red.txt` 節錄:

```text
tests/test_client_app.py F                                               [100%]
_ test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction _
tests/test_client_app.py:1521: in test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction
    assert "skipped(server_busy)" in after_compact[1], after_compact[1]
E   AssertionError: prompt cache 預熱=sent 12:13:32
E   assert 'skipped(server_busy)' in 'prompt cache 預熱=sent 12:13:32'
FAILED tests/test_client_app.py::test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction
============================== 1 failed in 12.12s ==============================
[run_tests] PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /usr/bin/python3 -m pytest tests/test_client_app.py::test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction
exit=1
```

紅燈原因與交接 §5.4 預期一致:`/compact` 之後 `/status` 仍是 mount 那一次的 `sent 12:13:32`,`skipped(server_busy)` 不在字串裡——是行為紅燈,不是缺方法、也不是 `觸發=` 字串缺席。

**綠燈(修完產品,同一條 node)**

```bash
python3 -m py_compile client_turns.py client_app.py tests/test_client_turns.py tests/test_client_app.py   # OK
python3 scripts/run_tests.py "tests/test_client_app.py::test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction" \
  > /tmp/codetrail-startup-ttft-20260907/b04-green.txt 2>&1      # 工具回報 exit 0(無錯誤)
echo "exit=0" >> /tmp/codetrail-startup-ttft-20260907/b04-green.txt
```

`/tmp/codetrail-startup-ttft-20260907/b04-green.txt` 節錄:

```text
collected 1 item
tests/test_client_app.py .                                               [100%]
============================== 1 passed in 2.25s ===============================
[run_tests] PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /usr/bin/python3 -m pytest tests/test_client_app.py::test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction
exit=0
```

只跑了這一紅一綠,測試本身沒有筆誤重跑;沒有再跑任何已過的測試。

### 3.2 契約(只寫不跑,交 reviewer 的 full)

`tests/test_client_turns.py::test_every_prime_the_coordinator_runs_reports_through_on_prime`(module 層 smoke;**未執行**,只 `py_compile`)

- `_coordinator(engine, compactor=_Compactor(outcome=_Outcome("compacted", "已壓縮")), on_prime=recorder)`;閒置時 `prime_in_background("mount", on_done=…)` → recorder 收到 `("prime", "mount", outcome, busy=False)`,`on_done` 也被叫、在 `on_prime` 之後、拿到同一個 outcome 物件。
- `start_compaction()` 走 `compacted` → 等 compact worker 結束(`_joined`)→ recorder 收到 `("prime", "compaction", outcome, busy=False)`,當時 `coordinator.busy is False`;`engine.primes == [("mount", False), ("compaction", False)]`。
- engine 的 `prime_prompt_cache` raise → recorder 收到 `("mount", None)`。
- `on_prime` 自己 raise 一個 `BaseException` 子類 → 預熱執行緒正常結束(`_joined`)、`on_done` 照樣被叫、`threading.excepthook`(monkeypatch 成記錄器)零呼叫。用 `BaseException` 子類是為了釘「連 BaseException 都吞」那半句:只丟 `RuntimeError` 的話,`except Exception` 也會綠。

## 4. 需要整合者登記進 `tests/test_smoke_gate.py` 的 node(既有檔名鍵;本 lane 未碰該檔)

| 檔名鍵 | node |
|---|---|
| `test_client_app.py` | `test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction` |
| `test_client_turns.py` | `test_every_prime_the_coordinator_runs_reports_through_on_prime` |

建議同時在兩個鍵的說明字串補一句:`test_client_turns.py` —「每一次預熱(含壓縮後協調器自己排的)都經 `on_prime` 回報,先 `on_prime` 再 `on_done`,回呼例外不冒出、不互相帶走」;`test_client_app.py` —「`/status` 反映最近一次預熱(含壓縮後那一次)的結果 / 原因 / 時間 / 觸發點」。說明字串是整合者的檔,措辭由整合者定。

## 5. 動到的既有測試(AGENTS §1.5 逐條)

| 檔 | node / 位置 | 改了什麼 | 理由 |
|---|---|---|---|
| `tests/test_client_app.py` | `test_the_tui_primes_on_mount_new_and_session_switch_through_the_coordinator` | `noted[1].sent is True` → `noted[1] == "mount" and noted[2].sent is True`;上一行註解由「on_done 有搬回 UI 執行緒」改成「outcome 經協調器的 on_prime 搬回 UI 執行緒、記錄帶觸發點」 | `_last_prime` 從 `(時間, outcome)` 變 `(時間, 觸發點, outcome)`:多了觸發點 `/status` 才講得出「這一次是壓縮後的預熱」。交接 §5.5 指定的調整;零其他斷言變更 |
| `tests/test_client_turns.py` | 模組 docstring | 「預熱的那兩條」→「那三條」,補「每一次跑完都經 `on_prime` 回報」 | 檔頭要說得出這個檔守哪些契約,新加的那條才不像放錯檔 |
| `tests/test_client_app.py` | 模組層新增 `_until`、`_PrimeOutcomes` | 新增,不改任何既有 helper / 替身 | 共用替身 `_Engine` 的行為未動(交接 §5.5:依呼叫序回不同 outcome 用子類做) |
| `tests/test_client_turns.py` | 共用替身 `_Engine` | **未動**;raise 的情境用測試內子類 `_Raises` | 同上 |

沒有刪弱斷言、沒有 skip / xfail。既有 node 的執行:本 lane 依 §3 未跑(只准跑自己新寫的那一條),交 reviewer 的 full。

## 6. 實際的 `/status` 字串(由 `_prime_status()` 產生;時間為當地 `HH:MM:SS`,下列時間是示意)

| 狀況 | 字串 |
|---|---|
| 還沒排過 | `prompt cache 預熱=尚未` |
| 送出(mount) | `prompt cache 預熱=sent 12:13:32 觸發=mount` |
| 跳過(壓縮後) | `prompt cache 預熱=skipped(server_busy) 12:14:05 觸發=compaction` |
| engine 破了不 raise 的契約(outcome 為 None) | `prompt cache 預熱=error 12:15:40 觸發=session` |

`觸發=` 的值就是傳給 `prime_in_background` 的 reason:`mount` / `new` / `session` / `compaction`;reason 空字串時顯示 `觸發=?`(目前沒有這種呼叫點)。紅燈檔裡那一行 `prompt cache 預熱=sent 12:13:32` 是**未修**產品的實際輸出(沒有 `觸發=`)。

## 7. 給整合者的文件字句(§5.6;文件不是本 lane 的 owner,一字未動)

- `docs/troubleshooting.md` 第 222–229 行:`/status` 表的 `sent` / `skipped` / `error` 三列各加 ` 觸發=<…>`(即 `sent <時間> 觸發=<mount|new|session|compaction>`、`skipped(<原因>) <時間> 觸發=<…>`、`error <時間> 觸發=<…>`);第 229 行整句換成「這一行反映**每一次**預熱:啟動、`/new`、換 session,以及自動壓縮 / `/compact` 換掉歷史之後那一次;`觸發=` 告訴你是哪一種」。
- `README_DEV.md` 第 646 行 `TurnCoordinator.prime_in_background(reason, *, on_done=None)` 那一列末尾補:「協調器層的 `on_prime(reason, outcome)` 回呼(建構時給):每一次預熱(含壓縮後協調器自己排的那一次)都回到 TUI,先 `on_prime` 再 `on_done`」。
- `AGENTS.md` §2 `client_turns` 條「`prime_in_background` 不取回合鎖、不動 `_turn_done` / `_cancelled`,取消對預熱是 no-op」之後補「;每一次預熱的 outcome 經 `on_prime` 回到 TUI,不分誰排的」。
- `04-impl-c.md` §9 第 2 點記的「已知的顯示落差(壓縮後那一次不會更新 `/status`)」自本輪起不成立;歷史文件不改,整合者在 `06-fix-r1.md` 的 Blocker 對照表註明即可。

## 8. 給另一 lane / 整合者的介面說明

- 對 engine lane:零依賴變更。status lane 只讀 outcome 的 `sent` / `reason`(`getattr`),`processed_tokens` 不顯示;B05 新增的 `incomplete` / `no_timings` 會以 `skipped(incomplete)` / `skipped(no_timings)` 原樣出現;B02 的 `aborted` 同理。
- 對整合者:`TurnCoordinator` 多一個可選 keyword `on_prime`,其他建構點(若有)不傳也能跑;`prime_in_background` 簽名不變。`CodeTrailApp._last_prime` 是三元組,目前只有 `_prime_status()` 與兩條 app 測試讀它。

## 9. 未做與風險

1. **未執行**:`test_every_prime_the_coordinator_runs_reports_through_on_prime`(契約,只寫)、既有 `test_the_tui_primes_on_mount_new_and_session_switch_through_the_coordinator`(索引已調整,未跑)、smoke / full 全部未跑——依 §3,由 reviewer 的 full 執行。`py_compile` 四個檔通過。
2. **「最近一次」= 最後落地的那一次,不是最後排的那一次**:`_note_prime` 只以 `time.time()` 記錄,不排序。換 session 時舊預熱先被 `abort_prime()` 中止(engine lane B02 修好後上限 1 秒),它的 `aborted` outcome 通常先於新 session 的預熱落地,所以 `/status` 最後顯示的是新那一次;若中止逾時、舊預熱晚於新預熱結束,`/status` 會短暫顯示舊的 `skipped(aborted)`。這是交接 §5.2 介面(三元組、不帶序號)的既定形狀,本 lane 不擴設計,列給 Astra 知道。
3. **命令形狀偏離**:紅 / 綠的 `exit=` 行不是 shell 變數寫的(§3.1 已說明);兩個 log 檔的其餘內容是 runner 的完整 stdout+stderr。
4. **HEAD 在施工期間前進**(`71217dc…` → `a946ab9f…`):本 lane 沒有檢視那個 commit;產品 digest 由整合者在 freeze 時重算。
5. B9 的誠實揭露(照抄):reasoning 與硬體 prefill 成本未變;預熱只把**下一輪 prefix** 的 prefill 搬到打字之前;熱 prefix 時無感;是否真的被重用只由 T0 的 `prompt_tokens_processed` 判定,規劃與施工階段沒有數字。本 lane 只讓 `/status` 講得出每一次預熱的結果,沒有改變預熱本身。

## 10. 已執行的命令(全部)

| 命令 | 結果 |
|---|---|
| `git rev-parse HEAD` / `git status --short`(動工時) | `71217dc3…`;21 個產品 / 測試路徑 `M `,`00-model-log.md` ` M` |
| `python3 -m py_compile tests/test_client_app.py` | OK |
| `python3 scripts/run_tests.py "tests/test_client_app.py::test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction" > …/b04-red.txt 2>&1` | 工具回報 Exit code 1;`1 failed in 12.12s` |
| `echo "exit=1" >> …/b04-red.txt` | — |
| `python3 -m py_compile client_turns.py client_app.py tests/test_client_turns.py tests/test_client_app.py` | OK |
| 同一條 node `> …/b04-green.txt 2>&1` | 工具回報 exit 0;`1 passed in 2.25s` |
| `echo "exit=0" >> …/b04-green.txt` | — |
| `git diff -- client_turns.py client_app.py tests/test_client_turns.py tests/test_client_app.py` | 只有 §2 列的改動 |
| `git status --short` / `git rev-parse HEAD`(交付時) | 見 §2;`a946ab9f813dc88381b0e3ce5ad83e3770a34b65` |

沒有跑 smoke / full / 整檔 pytest / 直呼 pytest / `compileall` / `check_readme_consistency`;沒有 git 寫入操作。
