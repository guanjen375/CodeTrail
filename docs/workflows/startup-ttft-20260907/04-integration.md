# startup-ttft-20260907 — Step 4 整合報告

- 整合者:Claude Opus 5.0 MAX(harness 自報 model ID `claude-opus-5`)。角色 = 整合者,不是規劃者、不是審核者。
- 日期:2026-09-07。cwd:`/home/david/CodeTrail`。
- 依 `03-plan-final.md`(Fable 5.1 定稿)§6「整合者」與 §7 執行;**沒有**重新規劃、**沒有**變更任何設計決策。
- 本步未對任何 live server 發請求、未讀私人 session、未動部署、未新增 `os.environ` 讀取、未 `git commit` / `push`、未改 `.gitignore`。

---

## 1. 依賴與 owner

四個 lane 全部完成後才開始整合;整合者只在自己的 owner 範圍內寫入。

| 產出 | owner | 整合者有沒有改 |
|---|---|---|
| `client_engine.py` / `config.py` / `llama_client.py` / `context_budget.py` / `tests/test_client_engine.py` / `tests/test_context_budget.py` | Lane B | 否(逐字套 patch) |
| `client_turns.py` / `client_app.py` / `tests/test_client_turns.py` / `tests/test_client_app.py` | Lane C | 否(逐字套 patch) |
| `client_preflight.py` / `codetrail_chat.py` / `tests/test_client_cli.py` / `tests/test_client_preflight.py` / `tests/test_lessons.py` | Lane A | 否(逐字套 patch) |
| `README.md` / `README_DEV.md` / `AGENTS.md` / `docs/setup.md` / `docs/troubleshooting.md` | Lane D | 否(逐字套 patch) |
| **`tests/test_smoke_gate.py`** | **整合者(唯一 shared-file owner)** | **是**(§3) |
| `docs/workflows/startup-ttft-20260907/04-integration.md` | 整合者 | 是(本檔) |
| `docs/workflows/startup-ttft-20260907/04-impl-a.md` | Lane A,整合者僅一句事實更正 | 是(§7,只改「執行次數」那一句) |
| `00-model-log.md` | root | 否(主樹的 ` M` 是 root 的,未覆寫、未 stage) |
| `01/02/03-plan-*.md` | 規劃者 | 否(未覆寫) |

四份 lane 報告都自述 Dependency:A / B 無依賴;C 只依賴 §4.2 的**介面名稱**(產品端一律 `getattr`,測試替身不 import `PrimeOutcome`);D 依賴 A–C 的交付表確認字串,且已逐項核對。整合時沒有發生跨 lane 的檔案衝突(四份 patch 的檔案兩兩不重疊)。

## 2. 套用的 patch 與 hash

四份 patch 的 sha256 與 root 交付的值**逐字相符**(整合者以 `sha256sum` 自行複核):

| lane | patch | sha256 | 核對 |
|---|---|---|---|
| A | `/tmp/codetrail-startup-ttft-20260907/patches/lane-a.patch` | `d25ebcccdbc9013d7faeafd43353511b476b3875cfad8c89ccc9c16814cf6aea` | ✅ 相符 |
| B | `…/lane-b.patch` | `8ff060d83fa9963425784a2c166e99c975ac30db511575ffaf429342bc78d8c5` | ✅ 相符 |
| C | `…/lane-c.patch` | `4b4de2e310fa5952897c82708d773ec224fb8d88169d7232509ace32b667dec2` | ✅ 相符 |
| D | `…/lane-d.patch` | `271642fadb107bda6d62ee6528029fe5281e4816e5f8b796f73457008b4e04dc` | ✅ 相符 |

套用順序 **B → C → A → D**,每一份先 `git apply --check` 再 `git apply`,全部 `Applied … cleanly`、零 fuzz、零 `.rej`:

```
git apply --check --verbose <lane-b|c|a|d>.patch     # 四份都通過,無輸出以外的訊息
git apply --verbose lane-b.patch  → client_engine.py / config.py / context_budget.py /
                                     llama_client.py / tests/test_client_engine.py /
                                     tests/test_context_budget.py  (6 檔 cleanly)
git apply --verbose lane-c.patch  → client_app.py / client_turns.py /
                                     tests/test_client_app.py / tests/test_client_turns.py (4 檔 cleanly)
git apply --verbose lane-a.patch  → client_preflight.py / codetrail_chat.py /
                                     tests/test_client_cli.py / tests/test_client_preflight.py /
                                     tests/test_lessons.py (5 檔 cleanly)
git apply --verbose lane-d.patch  → AGENTS.md / README.md / README_DEV.md /
                                     docs/setup.md / docs/troubleshooting.md (5 檔 cleanly)
```

套完後工作樹的 20 個產品 / 測試檔 = 四份 lane `git diff --stat` 的總和(A 445+/24-、B 922+/4-、C 384+/5-、D 166+/6- = 1917+/39-),沒有多、沒有少。

## 3. 整合者自己的寫入:`tests/test_smoke_gate.py`

依 §6,把四個 lane 的 **16 條**新 smoke node 登記到 `SAFETY_MODULES` 的**既有檔名鍵**下(**沒有新增檔名鍵**,沒有刪除或改寫任何既有 node):

| 檔名鍵 | 新增 node | 條數 |
|---|---|---|
| `test_client_preflight.py` | `test_the_banner_keeps_stderr_warnings_and_compaction_status_but_drops_the_progress_log`、`test_keep_does_not_change_what_note_prints` | 2 |
| `test_client_cli.py` | `test_headless_run_never_primes_the_prompt_cache`、`test_the_tui_banner_after_a_passing_preflight_drops_the_progress_log_and_keeps_warnings` | 2 |
| `test_client_engine.py` | `test_priming_sends_the_prefix_the_next_turn_will_send_and_records_nothing`、`test_priming_refuses_a_readonly_engine_before_any_probe_or_request`、`test_priming_yields_to_a_turn_that_already_began_and_never_reads_its_history`、`test_a_turn_submitted_during_priming_waits_for_the_lock_and_gets_the_primed_prefix`、`test_a_session_switch_aborts_an_in_flight_prime_and_frees_the_lock`、`test_priming_skips_only_when_no_slot_is_idle_and_never_raises_or_prints`、`test_priming_gates_the_one_token_it_sends_after_checking_the_next_turn_reserve`、`test_priming_is_invisible_to_cancel_and_leaves_the_turn_state_untouched` | 8 |
| `test_context_budget.py` | `test_processed_prompt_tokens_are_recorded_separately_from_the_total` | 1 |
| `test_client_turns.py` | `test_a_compaction_that_replaced_the_history_primes_after_the_turn_lock_is_released`、`test_priming_is_invisible_to_busy_and_cancel_and_refused_while_a_turn_runs` | 2 |
| `test_client_app.py` | `test_the_tui_primes_on_mount_new_and_session_switch_through_the_coordinator` | 1 |
| **合計** | | **16**(= 2 preflight + 2 cli + 8 engine + 1 context + 2 turns + 1 app) |

同時依 manifest 的既有寫法(「檔名 → (**說明**, node)」)把六個檔名鍵的**說明**各補上這次新增的安全檢查點文字,對齊 Lane D 寫進 `AGENTS.md` §2 的三句(預熱是唯一沒有使用者訊息就打主模型的路徑 / `prime_in_background` 不取回合鎖 / 啟動橫幅只拿 `banner_lines()`),外加 `prompt_tokens_processed` 與 `banner_lines()` 兩條。**只加說明,沒有刪改任何既有說明文字。**

整合者另**靜態核對**(不執行測試)16 條 node 的存在與 smoke 標記:

- 16 個函式名全部存在於套用後的測試檔(以 diff 中新增的 `def test_…` 列逐一比對)。
- `tests/test_{client_preflight,client_cli,client_engine,client_turns,client_app}.py` 都有 module 層 `pytestmark = pytest.mark.smoke`;`tests/test_context_budget.py` 沒有 module 層標記,它那一條新 node 帶逐條 `@pytest.mark.smoke`(第 994 行)。**16 條都在 smoke 包裡。**

## 4. 實際執行的工具與命令

| 命令 | 結果 |
|---|---|
| `sha256sum`(四份 patch) | 與 root 交付值全部相符(§2) |
| `git apply --check`(四份) | 全部通過 |
| `git apply`(B → C → A → D) | 全部 `Applied … cleanly` |
| `python3 -m compileall -q .` | 無輸出(`-q` 只在失敗時印錯誤)⇒ 全樹編譯通過。跑了兩次,兩次都無輸出 |
| `python3 scripts/check_readme_consistency.py` | `[readme-consistency] OK — README/docs ↔ mcp_server.py / config.py 一致` |
| `python3 scripts/run_tests.py -m smoke`(**唯一一次**) | **exit code 1**,詳見 §5 |
| `git add -A -- . ':(exclude)docs/workflows'` | 21 個路徑進 index;命令回 exit 1,原因只是 git 對 `.` 命中被 ignore 的 `docs/workflows` 印了 advice,**staging 本身完成**(§6 的 `git status` 已逐行確認) |
| `git rev-parse HEAD` / `git write-tree` / `git diff --cached --binary <base> … \| sha256sum` | §6 |

**沒有**執行:full、任何其他 `tests/` 命令、直呼 `pytest`、live server 請求、部署變更。沒有啟用任何 hook。

## 5. smoke 結果(唯一一次執行)

命令:`python3 scripts/run_tests.py -m smoke`

```
[run_tests] PYTEST_DISABLE_PLUGIN_AUTOLOAD=1; 選取「-m smoke」:2405 條 / 41 個檔 / 16 個並行 shard
[run_tests] FAILED shards: [9]
[run_tests] 合計 2405 條被選中(failed=1 errors=0 skipped=0);各 shard 內耗時總和 122.99s
```

| 項目 | 值 |
|---|---|
| exit code | **1** |
| collected | 2405(41 個檔 / 16 shard) |
| passed | 2404 |
| **failed** | **1** |
| errors | 0 |
| skipped | 0 |
| 失敗 shard | 9(其餘 15 個 shard 全部 `exit=0`) |

### ⚠ 失敗 node 未能取得 —— 這是本次交付最重要的待審事項

`0 tests collected` 沒有發生(2405 條真的被選中),所以這不是 §1.2 的 exit-5 異常;但**那一條失敗的 node ID 我拿不到**,原因三點疊在一起:

1. `scripts/run_tests.py:737` 用 `tempfile.TemporaryDirectory(prefix="codetrail-pytest-")` 存放各 shard 的 junit 與 log,**行程結束時整個目錄被刪掉**。
2. shard 9 的 console 區塊(含 `FAILURES` 節錄)剛好落在整合者 harness 的輸出截斷區(shards 5–9 共 6532 字元被截掉),shard 1–4 與 10–16 則完整顯示、全部 `exit=0`。
3. 執行當下該 temp 目錄在本 session 的可讀路徑之外,`Read` / `Grep` / `cp` 皆被沙箱擋下(允許路徑只有 `/home/david/CodeTrail` 與 `…/patches`)。

**沒有重跑**:本次 prompt 明示 smoke 只跑一次、紅燈不重跑。也**沒有**去改任何 lane 的產品檔。

可以確定的事(靜態核對,不是猜測):

- 這 16 條新 node **全部存在且全部帶 smoke 標記**(§3),所以失敗**不是**「gate 登記了不存在或沒標記的 node」。`tests/test_smoke_gate.py` 的四條自身測試(重複鍵、node 存在 + 帶 smoke、每個模組至少一個 node、marker 已登記)若失敗會直接指向我的登記,而我對這 16 條的存在性與標記已逐一比對過。
- 失敗 shard 只有一個,`errors=0`、`skipped=0`,所以不是 collect error、不是環境性 skip。
- `.pytest_cache/shard_weights.smoke.json` **不能**用來反推:`run_tests.py:825-829` 的 `COMPLETED_SESSION_CODES` 判準是「pytest session 有沒有正常跑完」而**不是**「有沒有全綠」,所以 shard 9(exit=1)的 node 一樣被併進權重檔,存在與否無法區分通過 / 失敗。

**還有一件必須揭露的事**:這次施工**沒有任何人記錄 base HEAD `f200f697…` 的 smoke 基線**(A 只跑了自己那一條 regression,B / C 零測試,D 只跑靜態 consistency)。因此我**無法**判定這條失敗是「本次引入的新失敗」還是「動工前就存在的基線失敗 / 已知 flaky」。依 §1.2,基線外的新失敗 = 未完成;但基線未知時我不得單方面宣告任一結論。

**給 Astra / Step 5 的建議**:§7 的 full 執行會直接把該 node 印出來(full 不受本次「只跑一次 smoke」的限制),請以那次執行的失敗 node 集合為準;若要更快,對 base HEAD `f200f697…` 與本凍結內容各跑一次 smoke 即可分辨新失敗與基線。修復依分工由 Fable 執行,不由整合者代改。

`Tests: smoke only — reviewer owns full execution.`

## 6. 凍結三件套(§7)

```
BASE_HEAD    = f200f697ba54d38a102e8ef66dead652c4002e5f
actual HEAD  = 22cfc19d2e1ef87bc5e6b68fb1e73c997b2a9319
index tree   = 9006b9244f1f3f5764f8a56ca7bbdcc4750e66ba      (git write-tree)
product digest = 6e92b5cf7639e13e83b62b7ca172677c72a3d839427e68e1f928ca8f31d8c7da
                 (git diff --cached --binary $BASE_HEAD -- . ':(exclude)docs/workflows' | sha256sum)
```

- **產品未 commit。`actual HEAD` 22cfc19 不含本次任何修正** —— 22cfc19 與 8496c36 兩個 intervening commit 都只有交接 markdown,產品內容仍等同 base `f200f697…`。全部產品 / 測試變更只存在於 **index + 工作樹**。
- 凍結檢查(`git status --porcelain=v1 --untracked-files=all`):21 個路徑全是 `M `(**第二欄空白** = 已暫存且無未暫存殘留),零 `??`、零 `MM`、零 ` M`。唯一第二欄非空白的是 ` M docs/workflows/startup-ttft-20260907/00-model-log.md` —— 它在 `docs/workflows` 底下(交接檔、root owner),依 §7 本來就排除在產品 digest 之外,未被 stage、未被覆寫。
- `docs/workflows/` 已被 `.gitignore` 涵蓋,`04-impl-*.md` / `03-plan-final.md` / 本檔屬未追蹤+ignored,不會進 index、不影響 product digest;交接 commit 由 root 按 exact paths(`git add -f`)執行。

`git diff --cached --stat f200f697… -- . ':(exclude)docs/workflows'`:

```
 AGENTS.md                      |  12 +-      client_preflight.py            | 119 +++++++--
 README.md                      |   4 +-      client_turns.py                |  65 ++++-
 README_DEV.md                  |  35 ++-     codetrail_chat.py              |  11 +-
 client_app.py                  |  84 +++++   config.py                      |   8 +
 client_engine.py               | 301 +++++    context_budget.py              |  12 +
 docs/setup.md                  |   3 +-      docs/troubleshooting.md        | 118 +++++
 llama_client.py                |  14 +-      tests/test_client_app.py       | 109 +++++
 tests/test_client_cli.py       | 143 +++++    tests/test_client_engine.py    | 549 +++++
 tests/test_client_preflight.py | 193 ++++     tests/test_client_turns.py     | 131 +++++
 tests/test_context_budget.py   |  42 ++       tests/test_lessons.py          |   3 +-
 tests/test_smoke_gate.py       |  44 ++-
 21 files changed, 1955 insertions(+), 45 deletions(-)
```

(= 四個 lane 的 1917+/39- 加上整合者在 `tests/test_smoke_gate.py` 的 38+/6-。)

## 7. 既有測試變更彙整(§1.5;四份報告逐條合併)

**零既有斷言被改動、放寬或刪除;零既有測試被刪除;零 skip / xfail 新增。** 動到的全是 docstring、註解、import 與共用替身的**新增**:

| 檔 | 位置 / 測試名 | 動作 | 理由(owner 自述) |
|---|---|---|---|
| `tests/test_client_preflight.py` | 模組 docstring 15–18 行 | 改措辭 | 契約變了:transcript 仍逐字完整,但「通過後進 TUI 的」只有 `banner_lines()`;docstring 說的是本檔守什麼,不改就是教錯契約(Lane A) |
| `tests/test_lessons.py` | `_render()` docstring(402–403) | 改措辭 | 同上;對話區第一則不再是整段 transcript,過期提示變成 banner 的警告之一。零行為變更(Lane A) |
| `tests/test_client_engine.py` | 模組 docstring | 加一條 bullet | 該 docstring 是「本檔守哪些 §2 檢查點」的清單,新增檢查點就要同步(Lane B) |
| `tests/test_client_engine.py` | import 區 | 新增 `import copy` | 新 node 1 要用 `deepcopy` 證明歷史逐字不變(淺拷貝比不出巢狀 `tool_calls` 被動過)(Lane B) |
| `tests/test_context_budget.py` | 檔尾 | 只新增一條 node | 既有內容零變更(Lane B) |
| `tests/test_client_turns.py` | 模組 docstring | 加兩行 | 檔頭要說得出這個檔守哪些契約(Lane C) |
| `tests/test_client_turns.py` | `import types` | 新增 stdlib import | 替身要回 `PrimeOutcome` 形狀的物件,又不能 import Lane B 當時還沒有的真型別(Lane C) |
| `tests/test_client_turns.py` | 共用替身 `_Engine` | 新增 `coordinator` / `primes` / `priming` 屬性 + `prime_prompt_cache(*, reason="")` 方法 | §6 指定:替身要記下 `(reason, 當下的 coordinator.busy)` 才能證明預熱在放鎖之後;沒有這個方法協調器會直接跳過,兩條新契約測不到。既有欄位與行為未改(Lane C) |
| `tests/test_client_turns.py` | 新增 helper `_eventually` / `_joined` | 新增 | 等 worker 執行緒真的結束,不靠 sleep(不改既有 helper)(Lane C) |
| `tests/test_client_app.py` | 共用替身 `_Engine` | 新增 `primes` / `primed`(Event)/ `priming` + `prime_prompt_cache` | 同上(Lane C) |
| `tests/test_client_app.py` | 新增替身 `_SlowPrime(_Engine)` | 新增 | 要在「預熱進行中」那個瞬間讀狀態列(Lane C) |
| `tests/test_client_cli.py` | 檔內 helper `_write_deployment` | 新增 | 新的 helper,沒有覆寫任何既有名稱(Lane A) |
| `tests/test_smoke_gate.py` | `SAFETY_MODULES` 六個既有檔名鍵 | **新增** 16 個 node + 補說明文字 | §6 指定的整合者工作(§3)。零刪除、零改寫既有 node(整合者) |

### ⚠ Lane C 主動揭露的既有測試**行為**副作用(交 Astra 判定,整合者不代為認定免審)

替身加上 `prime_prompt_cache` 之後,`tests/test_client_app.py` 的**每一個** app 測試在 `on_mount` 都會多 spawn 一條背景執行緒(呼叫替身、append 一個字串、經 `call_from_thread` 記一次 `_last_prime`);`tests/test_client_turns.py::test_auto_compaction_runs_after_a_completed_answer` 也會多一次(它的 outcome 就是 `compacted`)。Lane C 的判斷是斷言與畫面內容都不受影響(預熱不進對話區、不進事件流、不寫檔),但**這是既有測試在行為上多出來的東西**。相關實作決定:為了避免 app 收尾時 `call_from_thread` 以 `CancelledError`(BaseException)在背景執行緒噴 traceback,`prime_in_background` 的 `on_done` 回呼包在 `except BaseException` 裡(engine 呼叫本身仍是 `except Exception`)。

> 整合者註:§5 那條失敗落在 shard 9,而 `tests/test_client_app.py` 的 node 分散在 shard 1/2/3/4/11/13/14/15(全部 `exit=0`);但我沒有 shard 5–9 的檔案清單,**不能**據此排除這個副作用是失敗成因。請 Astra 在 full 執行時一併確認。

## 8. Lane A 的 red-before-green 證據位置

- node:`tests/test_client_cli.py::test_the_tui_banner_after_a_passing_preflight_drops_the_progress_log_and_keeps_warnings`
- 命令:`python3 scripts/run_tests.py tests/test_client_cli.py::test_the_tui_banner_after_a_passing_preflight_drops_the_progress_log_and_keeps_warnings`
- **紅燈證據**:`04-impl-a.md` §2「紅(產品未改,只有測試檔)」節錄 —— `1 failed in 0.35s`,失敗訊息是 `AssertionError: 自檢進度 LOG 進了 banner:['[aicode] root=…', '[aicode] deployment profile=…', 'MODEL PASS — cached', …]`。是**行為紅燈**(進度 LOG 真的被重播進 banner),不是 fixture / 缺方法的錯。
- **綠燈證據**:`04-impl-a.md` §2「綠(產品改完,同一條、同一個命令)」節錄 —— `1 passed in 0.28s`。
- 執行次數:**三次**(一紅兩綠)。第二次綠是把測試區段註解移到正確位置之後的重跑,見 `04-impl-a.md` §5 的三列紀錄。
- 整合者**沒有重跑**這條 regression(§9-1)。

## 9. 偏離 / 未做 / 待審事項彙整

以下全部**照原樣整合、不自行認定免審**,請 Astra 逐條判定:

### 9-1 整合者自己的偏離

1. **`04-impl-a.md` §2 的一句事實更正**:原文寫「共執行兩次」,但同檔 §5 的表格與 raw log 記的是**一紅兩綠共三次**。已把該句改成「共執行**三次**:一紅兩綠 …(由整合者於 Step 4 更正,原文誤寫「兩次」;regression 未重跑)」。**只改文句,沒有重跑 regression、沒有動 A 的任何產品或測試檔。** 這是本次授權的唯一一處 lane 交接文件修改。
2. **smoke 失敗 node 未取得**(§5)。這是「未做」而不是「做了但沒寫」:受限於 runner 刪 temp 目錄 + harness 輸出截斷 + 沙箱路徑,且不得重跑。

### 9-2 Lane B 揭露的偏離(4 條)

1. **§4.2「以 `is` 比對確認最後一則仍是 placeholder」在實際程式上做不到** —— `strip_historical_reasoning()` / `prune_old_tool_outputs()` 第一行都是 `[dict(m) for m in messages]`,物件 identity 一定對不上,`is` 比對等於永遠失敗(或寫成永遠通過的死碼)。改用私有標記鍵 `_PRIME_PLACEHOLDER_KEY`:轉換後檢查 `working[-1].get(_PRIME_PLACEHOLDER_KEY)`,不合就丟 `EngineError`(記成 `error:EngineError`、不送任何可能含佔位內容的 payload);標記鍵在 `to_wire()` 之前隨整則訊息被 `pop()` 掉,不會出現在 payload(node 1 有斷言釘住)。Lane B 主張這比位置檢查更強。
2. **`gate` 這個 reason 在 `options.max_output_tokens >= 1` 時實際不可達** —— 兩次計算吃同一份 payload,第一次保留額 ≥ 1、第二次是 1,第二次會溢位則第一次必先回 `next_turn_would_overflow`。`gate` 因此是防禦性分支(保留 `check_and_log` 作為這條路徑唯一的閘入口),依 §1.4 不寫儀式性測試。
3. **`prime_prompt_cache(reason=…)` 的 `reason` 在 engine 內刻意不被記錄**(零寫入包含 telemetry:`source=prime` 那列沒有 reason 欄位);已在 docstring 寫明它只是呼叫端的標記。
4. **「誰關那個串流」§4.2 沒明講** —— 實作把責任放在 `abort_prime()`:在 `_prime_guard` 內取走 `_prime_stream` 並清成 `None`,預熱的 `finally` 只會關到自己還握著的那一個(這是 node 5 的 `closed_from` 長度為 1 能成立的原因)。
5. (附帶)`client_engine.py` 模組 docstring 由「三件事」改成「四件事」:§4.4 只交代 Lane D 改 `AGENTS.md`,沒提這個檔自己的 docstring;Lane B 自述已確認 `tests/test_repo_consistency.py` 不掃它。

### 9-3 Lane C 揭露的偏離(含使用者點名的那一項)

1. **壓縮後的預熱沒有 `on_done` 回呼 → `/status` 的「上次預熱」不更新**。協調器自己排的那一次(`_run_turn` / `_run_compaction` 呼叫 `prime_in_background("compaction")`)沿用 §4.3 凍結的形狀,該形狀沒有 `on_done`,所以 `/status` 那一行只反映由 TUI 排的 **mount / new / session** 三種。要一起反映得讓協調器帶一個預設 `on_done`,那會改到凍結介面,本次**沒有**做。Lane D 已把這個落差**逐字寫進 `docs/troubleshooting.md`**。**依原樣整合,交 Astra 審。**
2. `/status` 多一個 §4.3 沒列的分支:`on_done` 拿到 `None`(engine 破了「不 raise」的契約)時顯示 `error <HH:MM:SS>`,不會顯示成 `sent`。
3. 相位是**獨立的一段 part**(插在 spinner 之後),不是接在 spinner 字串裡;`prompt cache 預熱中` 追加在 parts **最後**(§4.3 只寫「加」,沒指定位置)。
4. 時間戳用 `time.time()`(wall clock),`/status` 以本地時間顯示 `HH:MM:SS`;狀態列 spinner 仍用 `time.monotonic()`(既有行為未動)。
5. 既有測試的行為副作用(多 spawn 的背景執行緒)—— 見 §7 的 ⚠ 段。
6. Lane C 沒有跑 `git apply --check`(不在該 lane 的允許命令清單),patch 由乾淨 worktree 的 `git diff --binary` 產生;整合者已代為 `--check` 並確認乾淨。

### 9-4 Lane D 揭露的偏離

1. **`check_readme_consistency.py` 在 Lane D 只跑過「只有 D 變更」的樹**;整合者已於**整合後的完整樹**重跑一次 → `OK`(§4)。
2. **偏離 §4.4 一處,方向是「更精確」**:§4.4 寫「prefix 五段 + 工具 schema」,但 `client_prompt.build_system_prompt()` 實際是五份文字來源 + 一行沙箱根目錄,所以 troubleshooting 的表格寫成六列 + 工具 schema,沒有寫死「五段」。
3. **`docs/basic-usage.md:39` 沒有動**:那行是 `/status` 內容的括號概述而非完整清單,新增一行不會讓它變錯;且該檔不在 Lane D 可寫清單。**若要一起提「預熱」那一行,是另一個 owner 的事**,列此待審。
4. Lane D 沒有跑 `tests/test_repo_consistency.py`(pytest,該 lane 不得執行);文件層面的靜態 gate 由整合者這次 smoke 涵蓋 —— 但注意 §5 的失敗 node 未知,不能宣稱該 gate 已綠。
5. Lane D 交出的唯一跨 lane 耦合:若 Step 5 回修改了 `config.CLIENT_PRIME_PROMPT_CACHE`、`PrimeOutcome` reason 值域、`/status` 或狀態列的任一顯示字串,`docs/troubleshooting.md` 與 `README_DEV.md` 要跟著改。

### 9-5 Lane A 揭露的未做

1. A8 的 `README.md:706-707` / `docs/setup.md:248` 不在 Lane A 可寫清單,由 **Lane D 補上**;整合後兩處都已是新措辭。A8 其餘四項(`client_preflight.py` 四處 docstring、`tests/test_client_preflight.py` 模組 docstring、`tests/test_lessons.py` 註解)Lane A 已完成。

### 9-6 仍在計畫內、本次不做(§10 deferred,原樣保留)

prefix 瘦身、`id_slot` 釘 slot、KB 端查詢改寫預設、壓縮摘要 payload、`gpu_safety.runtime_offload_check` 的 `is_processing`、`keep_historical_reasoning` 預設、部署調參、reuse-failed 的自動停用、使用者送出時是否中止預熱、預熱 payload 被 server 拒絕時的佔位形狀。§8 的 T0 量測由 David 執行,施工與整合階段**一個數字都沒有**。

## 10. 誠實揭露(§5 B9 固定句)

**reasoning 與硬體 prefill 成本未變;預熱只把下一輪 prefix 的 prefill 搬到打字之前;熱 prefix 時無感;是否真的被重用只由 T0 的 `prompt_tokens_processed` 判定,規劃、施工與整合階段沒有數字。**

本次整合**沒有量測任何真實加速**,**不保證**硬體 prefill 或 reasoning 成本縮短。相位顯示只是把 thinking 那段時間顯示出來,不會縮短它。收益是否成立,依 `03-plan-final.md` §5 C 的四類判讀(skipped / sent / reuse-verified / reuse-failed),只能由 David 的 T0 決定;`reuse-failed` 是計畫預期內的分支,不是失敗的施工。
