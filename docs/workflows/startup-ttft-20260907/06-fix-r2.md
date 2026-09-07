# Step 5 R2 回修 — Astra MAX developer

- 日期：2026-09-07。使用者最新指示「你改成 astra max 修復 fable5.1 max 審核」；本輪 Astra (`gpt-6-astra`, effort `max`) 為唯一產品 writer，Fable 5.1 MAX 接任 reviewer。
- 範圍：只修 `05-review-astra-r2.md` 的 R2-B01 / R2-B02 與直接受影響的呼叫端。`03-plan-final.md` 驗收不變；R1 其他已關項不重開，歷史交接不回寫。
- 動工 base HEAD：`f200f697ba54d38a102e8ef66dead652c4002e5f`。
- 動工 actual HEAD：`500169a6d0d14ec206104bf6964403210d73d025`（只有交接 commit，產品未 commit）。
- 動工 product digest：`16bda189fe8d6d99edfd1ebc86b59a373ac2a1816389d98d5c59ab41731f9349`；現有 staged 產品修改全部保留。

## 1. Dependency、owner 與准許工具（產品編輯前登記）

目前沒有需要獨立平行施工的 READY lane。transport、生命週期與 regression 彼此依賴，由同一 writer 順序完成；root 不需等待其他 lane。

| 次序 | 工作 | Dependency | owner / 可寫檔案 |
|---|---|---|---|
| D0 | 讀規則與前輪證據、寫本交接 | 無 | Astra：本檔 |
| D1 | 離線 regression 釘住 pending headers 的 B7 與三處取消競態，先取得行為紅燈 | D0；對应產品仍未修 | Astra：`tests/test_client_engine.py`；必要的 transport regression 登記為新檔 `tests/test_http_cancel.py` |
| D2 | 預熱專用可取消 transport：在 HTTP bytes 送出前登記 socket，abort shutdown / close 已登記連線；取消後新取得的 socket 先關再拒絕送出。沿用 requests 的 TLS 驗證與 endpoint policy，不改共用 session | D1 | Astra：新檔 `http_cancel.py`（封裝 scoped requests adapter / socket cancellation）；`llama_client.py`（只為預熱提供選用 cancellation，既有預設路徑不變） |
| D3 | 原子登記預熱 token / 歷史、完整觀察 abort、封住 session 轉換空窗；保留模型鎖直到 HTTP 已關閉或未曾送出且取消後不可能再送 | D1、D2 | Astra：`client_engine.py`、`tests/test_client_engine.py` |
| D4 | 同一新 regression 轉綠、登記安全 gate、同步契約與文件 | D2、D3 | Astra：`tests/test_smoke_gate.py`、`AGENTS.md`（只相關 §2）、`README_DEV.md`、`docs/troubleshooting.md` |
| D5 | 靜態整合、stage / freeze、完整交付 | D4 | Astra：本檔、`07-deferred.md`；root：`00-intake.md`、`00-model-log.md` 與交接 markdown commit |
| D6 | Fable 靜態 Blocker 審核，歸零後同一 freeze full | D5 | Fable reviewer；Astra 不執行 full |

工具：`rg` / `sed` / `cat` / Python AST 與雜湊唯讀核查、`apply_patch` 編輯；`git diff/status/rev-parse/write-tree` 唯讀識別；只准 stage、不 commit / push / reset / stash / checkout。

本輪可執行的測試**僅**自己新增的 regression 單 node：

```text
python3 scripts/run_tests.py tests/<file>.py::<new_regression_node> > /tmp/codetrail-startup-ttft-20260907/astra-r2-<case>-red.txt 2>&1
python3 scripts/run_tests.py tests/<file>.py::<same_new_regression_node> > /tmp/codetrail-startup-ttft-20260907/astra-r2-<case>-green.txt 2>&1
```

先標 smoke，在未修的對應產品上跑紅；紅燈必須是本次錯誤行為。完整 stdout / stderr 第一次就 redirect，實際 exit 由 exec 工具記錄；不使用 pipeline 隱藏 exit，不為收回輸出重跑。其餘新增安全契約只寫不單跑，逐條登記 `SAFETY_MODULES`。既有已綠 node 不重跑。**不跑 smoke / full / 整檔 pytest / 直接 pytest / 間接測試**；唯一整合 smoke 額度已使用。

允許靜態檢查：`python3 -m compileall -q .`、`python3 scripts/check_readme_consistency.py`；必要時限定檔案的 `py_compile`。所有測試 HTTP 都是離線替身；零 live HTTP、零私人 session、零部署操作、零新增環境設定讀取、零 `process_env` 外 subprocess。

## 2. 修復要求與實作邊界

1. R2-B01：`new_session()` / `adopt()` 中止 pending headers 的預熱，1 秒內舊預熱 `aborted`、`priming=False` 且鎖可取得；舊連線必須先 shutdown，不能用提早放仍在飛的請求鎖換取測試綠燈。
2. R2-B02：job identity / abort Event / history snapshot 一起登記；完成得很快的 GET / POST 也觀察 abort；取消後尚未排到的 worker 不得送 HTTP；session 切換期間不得新登記舊歷史預熱，既有 job 在切換前中止。
3. 不動正常回合 `_cancel` / `_armed`、session append、事件流、共享模型鎖、payload / heal / gate / telemetry 成功條件；新 transport 預設不影響正常模型呼叫。HTTP cancellation 必須維持 TLS 驗證、endpoint policy、無 env proxy / netrc 與不跟 redirect。
4. 原 B7 不降級、不改計畫或以測試放寬消除 Blocker。TTFT 的 T0 仍未量測，不宣稱真實速度收益。

## 3. 執行結果

**兩項 Blocker 的修復已完成並凍結，待 Fable 5.1 MAX reviewer 裁定；writer 不自行關閉 Blocker。** 四條新 regression 各取得一次行為紅燈與一次綠燈，沒有重跑 smoke / full，沒有改計畫验收。

| 項目 | 最終修法 | 證據 / 待審 |
|---|---|---|
| R2-B01：pending headers 仍持鎖 | `http_cancel.RequestCancellation` 擁有預熱專用 requests session；adapter 在 `_new_conn()` 與 `send()` 登記 socket，HTTPS 完成包裝的 SSLSocket 也在送 bytes 前登記。`cancel()` 在同一 guard 內設定 Event 並 shutdown / close 所有已登記 socket；第二個取消者會等同一次 shutdown，不能只看到 Event 就放鎖。Engine 的 finally 呼叫 `request.close()`，然後才放模型鎖、清 priming / 登記、set done。headers 未到也不必等 response 或 600 秒 timeout。 | 新 `headers` regression 真正走 requests / urllib3，只替換離線 socket：`new_session()` 與 `adopt()` 都在 1 秒內 aborted / priming=False / 鎖可取；shutdown 當下鎖仍持有。 |
| R2-B02a：登記與 snapshot 之間 lost-abort | 模型鎖後在 `_turn_state` 判 `_in_turn==0`，再於 `_prime_guard` 同時登記 epoch、Event、done、request、歷史 snapshot、priming。`_prime_locked` 接受這份 snapshot，不重新吸收 epoch。`_Abandonable.wait()` 即使 I/O 已立即完成也檢查 abort。 | `lost-abort` regression 用 barrier 卡在登記後、I/O 前，再令 GET / POST 立即完成；原碼誤回 sent，本版 aborted、零 POST / telemetry。 |
| R2-B02b：取消後晚送 POST | `_Abandonable` worker 執行前檢查 abort；這個檢查與真正 I/O 之間若又取消，transport 的 socket 登記／shutdown 仍阻止實際 HTTP bytes。取消後才完成的 DNS / TCP connect / TLS 不取得發送資格。原 epoch 邊界檢查仍保留。 | `late-post` regression 把 HTTP worker 延後排程，取消後再釋放它；原碼補送 POST，本版零 POST。晚回 HTTP / HTTPS connect 另有僅寫未跑的 transport 契約。 |
| R2-B02c：session create 空窗 | `_session_transition()` 在 abort 前於 `_prime_guard` 關閉准入，整段 store.create / adopt / `_switch_session` 完成前都拒絕晚到預熱，回既有 `aborted`。finally 恢復准入，create / snapshot 建立失敗不替換 session 狀態。 | `switch-gap` regression 的原碼在換好 session 後仍卡舊串流，本版拒絕這個 job。create 失敗後可再次預熱的契約僅寫未跑。 |

產品介面變化：`llama_client.chat_completions(..., cancel=None)` / `get_slots(..., cancel=None)` 新增**選用** keyword；預熱傳自己的 `RequestCancellation`，預設仍用 `get_session()`。取消物件由 engine 持有並 close，絕不寫进模型 payload。`prime_prompt_cache(reason=)`、`abort_prime(wait=)`、`priming`、`PrimeOutcome` 形狀及 reason 集合不變。

`http_cancel` 沒有修改全域 pool mapping；專用 adapter 使用自己的 HTTP / HTTPS pool classes。沿用 `create_session()` 的 `trust_env=False`、TLS 驗證、`max_redirects=0`，llama_client 仍在所有 I/O 前驗 endpoint policy、傳 `allow_redirects=False` 並拒絕 3xx。預熱專用 adapter 不做透明 retry；一般回合、agent 與其他原有 HTTP 路徑保持原本設定。

直接呼叫端已靜態核對：Engine 的正常 `_open_stream()` / `_ModelSlot` / `complete()` / `_one_model_step()`、`agent.py` 兩個 chat 呼叫、llama_client 內 vision / canary 呼叫與 `gpu_safety.py` / `utils.py` / `scripts/doctor.py` 的 get_slots 呼叫都不傳新 keyword，走既有 transport；無需修改。`_Abandonable` 只有預熱的 GET / POST 兩個呼叫點，兩者都已傳 abort。

正常回合 `_cancel` / `_armed` / `_begin_turn` / `_end_turn` / `_record` / `_active_stream` / `_active_call`、session append、事件流、heal / payload / gate 保留額、terminal + timings 成功條件、TUI / status 本輪無修改。完成 telemetry 的最後身分檢查與記錄在 `_prime_guard` 內，與 abort 有一致先後順序。

## 4. 真實 red-before-green（唯一八次測試執行）

四條 regression 都先追加到測試檔，**四次紅燈全部完成後才修改產品碼**。紅燈所跑的產品源码來自 R1 的 `16bda189…` 凍結版；新增 regression 本身會改產品／測試 digest，因此不冒稱紅燈整棵樹的 digest 仍為 16bda189。

每次命令前都使用 `umask 077`；下面八個命令均直接 redirect 完整 stdout / stderr，沒有 pipeline。exit 是 exec 工具的實際結果，不是推測或檔尾手寫值；每次都 collected 1，沒有 exit 5。

```bash
python3 scripts/run_tests.py tests/test_client_engine.py::test_switching_session_shuts_down_pending_headers_before_releasing_the_model_lock > /tmp/codetrail-startup-ttft-20260907/astra-r2-headers-red.txt 2>&1
python3 scripts/run_tests.py tests/test_client_engine.py::test_abort_between_prime_registration_and_fast_io_cannot_be_lost > /tmp/codetrail-startup-ttft-20260907/astra-r2-lost-abort-red.txt 2>&1
python3 scripts/run_tests.py tests/test_client_engine.py::test_a_prime_http_worker_scheduled_after_abort_never_starts_the_post > /tmp/codetrail-startup-ttft-20260907/astra-r2-late-post-red.txt 2>&1
python3 scripts/run_tests.py tests/test_client_engine.py::test_a_prime_arriving_during_session_creation_cannot_keep_the_old_history_alive > /tmp/codetrail-startup-ttft-20260907/astra-r2-switch-gap-red.txt 2>&1

python3 scripts/run_tests.py tests/test_client_engine.py::test_switching_session_shuts_down_pending_headers_before_releasing_the_model_lock > /tmp/codetrail-startup-ttft-20260907/astra-r2-headers-green.txt 2>&1
python3 scripts/run_tests.py tests/test_client_engine.py::test_abort_between_prime_registration_and_fast_io_cannot_be_lost > /tmp/codetrail-startup-ttft-20260907/astra-r2-lost-abort-green.txt 2>&1
python3 scripts/run_tests.py tests/test_client_engine.py::test_a_prime_http_worker_scheduled_after_abort_never_starts_the_post > /tmp/codetrail-startup-ttft-20260907/astra-r2-late-post-green.txt 2>&1
python3 scripts/run_tests.py tests/test_client_engine.py::test_a_prime_arriving_during_session_creation_cannot_keep_the_old_history_alive > /tmp/codetrail-startup-ttft-20260907/astra-r2-switch-gap-green.txt 2>&1
```

| case / log stem | red：實際 exit / runner 結果 | green：實際 exit / runner 結果 | 紅燈核心節錄 |
|---|---|---|---|
| `astra-r2-headers` | 1；1 failed in 0.39s | 0；1 passed in 0.42s | `AssertionError: B7: headers 還沒回時，模型鎖仍被舊預熱持有` |
| `astra-r2-lost-abort` | 1；1 failed in 0.24s | 0；1 passed in 0.28s | `AssertionError: 已取消的登記不能記成 sent`；實得 `PrimeOutcome(sent=True, reason='', processed_tokens=7)` |
| `astra-r2-late-post` | 1；1 failed in 0.30s | 0；1 passed in 0.34s | `AssertionError: 已取消後才排到 CPU 的 HTTP worker 仍發出舊 POST`；`Left contains one more item` |
| `astra-r2-switch-gap` | 1；1 failed in 1.05s | 0；1 passed in 0.26s | `AssertionError: session 已換，空窗內登記的舊預熱仍未被中止`；primer `is_alive()` 為 True |

以上是真正的錯誤行為紅燈，沒有缺新方法／import 失敗造成的假紅。每條只跑一次 red、一次 green，沒有重跑取 log。綠燈後只有契約測試／gate／文件與產品 docstring 編輯，沒有再改修復邏輯。

## 5. 新安全節點與既有測試變動（AGENTS §1.5）

`SAFETY_MODULES` 追加 **8 個 node 名**：engine 原有鍵加 5、新增 `test_http_cancel.py` 鍵帶 3。兩個檔都有 module `pytestmark = pytest.mark.smoke`；四條 regression 與 engine 新契約另各帶 smoke decorator。靜態 AST 核對：8 名都存在、各登記恰好一次，未執行 gate 測試函式。

已執行的四條 node 是 §4。以下四個契約**只寫未跑**，由 reviewer full 覆蓋：

| 檔案 / node | 安全邊界 |
|---|---|
| `tests/test_client_engine.py::test_failed_session_creation_does_not_disable_future_priming` | create 失敗保留 session / history，finally 重開預熱准入；之後可送原歷史 prefix。 |
| `tests/test_http_cancel.py::test_cancellation_closes_a_late_connection_before_any_http_bytes` | HTTP / HTTPS 兩個參數案例：取消時 connect 還沒回，晚回 socket 被關，零 HTTP bytes。 |
| `tests/test_http_cancel.py::test_cancellation_owns_the_final_tls_socket_and_preserves_shared_transport` | 最終 TLS socket 可中止，取消後不補 body；共用 session / adapter / pool mapping 不變，專用 session 維持 trust_env=False、verify=True、max_redirects=0、零 retry。 |
| `tests/test_http_cancel.py::test_concurrent_cancellation_waits_for_the_same_socket_shutdown` | shutdown 尚未完成時第二個清理者不得提早返回，不能因 Event 已設而提前放模型鎖。 |

既有測試逐條變動（相對 R1 tree `35c25fd4118112f6d417bbe9cdd2c70ee9ca3f47`）：

| 檔 / node | 變動與行為理由 | 執行 |
|---|---|---|
| `tests/test_client_engine.py::test_a_session_switch_aborts_a_prime_that_has_no_stream_yet_and_never_sends_after_the_abort` | 只改第 (3) 子情況。舊 `_stuck_post` 沒有可取消 transport，人工 `release_post.set()` 才讓 headers 回；新版替身先透過 `kwargs['cancel'].register(pending_socket)` 登記離線 socket，shutdown 喚醒它。舊「abort 後鎖仍不可取」改成**先驗證 shutdown 已發生且當下鎖仍持有，再驗證目前鎖可取**；一秒、aborted、priming False、late stream 關恰好一次、只送一次 POST 等既有斷言保留。這是落實原 B7，不能保留與原計畫矛盾的鎖不放斷言。未用提早放鎖掩蓋仍在飛的 HTTP。 | 本輪未執行；真正 requests / urllib3 路徑由 §4 新 headers regression 實際紅綠驗證。 |
| `tests/test_smoke_gate.py::SAFETY_MODULES`（靜態登記，不是測試函式） | 追加上述 8 名，engine 說明由「response 到了關掉才放」更新成「headers 前 socket shutdown 後一秒內放鎖」，補競態條款與新 transport 鍵；無移除／改名任何既有 node。 | 未執行 gate；僅 AST metadata 核對。 |

AST 比對既有 engine 測試函式：只有上列一條 body 變動，刪除的既有 node 為空。其餘新增 helper、regression、契約不修改原測試；無 skip / xfail、無刪除或弱化 safety 斷言。R1 另外已綠的三 engine／一 app regression 均未重跑。

## 6. 文件、靜態整合與 diff

| 檔 | 本輪改動 |
|---|---|
| `http_cancel.py`（新增） | 專用 transport / socket cancellation，不加依賴或環境設定。 |
| `llama_client.py` | 選用 cancel keyword 與所有權 docstring；既有預設 transport 不變。 |
| `client_engine.py` | §3 的 atomic 登記、session transition、Abandonable 與 socket 收尾；正常模型回合取消路徑不改。 |
| `tests/test_client_engine.py` / `tests/test_http_cancel.py`（後者新增） / `tests/test_smoke_gate.py` | §4–5。 |
| `AGENTS.md` §2 | 明列原 B7 一秒要求、socket shutdown 在放鎖前、晚 connect 禁止補送、並行取消、TLS / transport 隔離、原子登記與 session 轉換錯誤路徑；不降級任何原安全邊界。 |
| `README_DEV.md` | 更新 abort 契約、專用 RequestCancellation 介面與測試分類；移除預熱必須等 headers 才放鎖的舊描述。 |
| `docs/troubleshooting.md` | `/new` / 換 session 一秒內收掉舊預熱、即使 headers 未到也不等 response / timeout；未宣称 live 速度收益。 |
| `07-deferred.md` | 頂部補本次修復完成待 Fable 審核、未正式擱置、full / T0 未做；保留歷史記錄並明示其適用版本。 |

完整 R2 增量 diff（R1 tree → 本輪 index，排除 workflows）：`/tmp/codetrail-startup-ttft-20260907/astra-r2.patch`，SHA256 `03cd32d28b0b75c0d3a4ddbe372c5a2080a6c292d54fa5794b9406297ba271a5`。產生命令（exit 0）：

```bash
git diff --cached --binary 35c25fd4118112f6d417bbe9cdd2c70ee9ca3f47 -- . ':(exclude)docs/workflows' > /tmp/codetrail-startup-ttft-20260907/astra-r2.patch
```

增量 diffstat：9 檔，689 insertions / 93 deletions。

```text
 AGENTS.md                   |  10 +-
 README_DEV.md               |   8 +-
 client_engine.py            | 187 +++++++++++++++++-------------
 docs/troubleshooting.md     |   4 +-
 http_cancel.py              | 131 +++++++++++++++++++++
 llama_client.py             |  15 ++-
 tests/test_client_engine.py | 276 +++++++++++++++++++++++++++++++++++++++++++-
 tests/test_http_cancel.py   | 133 +++++++++++++++++++++
 tests/test_smoke_gate.py    |  18 ++-
```

靜態命令（各執行一次，無 pytest 收集／測試函式呼叫）：

- `python3 -m compileall -q .`：實際 exit 0，無輸出。
- `python3 scripts/check_readme_consistency.py`：實際 exit 0，`[readme-consistency] OK — README/docs ↔ mcp_server.py / config.py 一致`。
- `git diff --check`：沒有 whitespace 問題輸出。
- Python AST 僅讀檔核對 gate 名称／smoke 與 `git show` 取出的 R1 測試函式結構，沒有匯入或執行測試。其餘是 `rg` / `sed` / `cat` / `git diff,status,rev-parse,write-tree` / 雜湊等只讀命令，以及 `apply_patch` / staging。

## 7. 最終 freeze（產品編輯已停止）

```text
base HEAD      = f200f697ba54d38a102e8ef66dead652c4002e5f
actual HEAD    = b5d2f15650a564c2969870c098b2e0eac4248b58
index tree     = 709bbe102da21b5145df4eefa0501d897f39a311
product digest = 61fda568fce324d31691892f51a01aa7f17831e4e2c3815d2c0e16868dac9b4a
```

**actual HEAD 只有交接 commit，產品仍未 commit。** `git add -A -- . ':(exclude)docs/workflows'` 執行一次，exit 1 的唯一輸出是 workflows 被 ignore 的 advice；接著 `git status --porcelain=v1 --untracked-files=all` 確認 **23 條產品路徑全部第二欄空白（21 M、2 A），無 MM / 未暫存 / 未追蹤產品**。只有 `07-deferred.md` 是未暫存交接修改；本檔最終補寫也只在被排除的 workflows 目錄。root 擁有交接 commit，Astra 不 stage 交接、不 commit / push 產品。

凍結 status 核對後，cached 與 working 兩個命令分別得到上列相同 SHA256；也與首次 staging 後核對結果一致：

```bash
git diff --cached --binary f200f697ba54d38a102e8ef66dead652c4002e5f -- . ':(exclude)docs/workflows' | sha256sum
git diff --binary f200f697ba54d38a102e8ef66dead652c4002e5f -- . ':(exclude)docs/workflows' | sha256sum
```

相對原產品 base 的完整 diffstat：23 檔，3233 insertions / 66 deletions。

```text
 AGENTS.md                      |   27 +-
 README.md                      |    4 +-
 README_DEV.md                  |   44 +-
 client_app.py                  |   92 ++++
 client_engine.py               |  555 ++++++++++++++++++++-
 client_preflight.py            |  119 ++++-
 client_turns.py                |   84 +++-
 codetrail_chat.py              |   11 +-
 config.py                      |    8 +
 context_budget.py              |   12 +
 docs/setup.md                  |    3 +-
 docs/troubleshooting.md        |  123 +++++
 http_cancel.py                 |  131 +++++
 llama_client.py                |   27 +-
 tests/test_client_app.py       |  219 +++++++++
 tests/test_client_cli.py       |  143 ++++++
 tests/test_client_engine.py    | 1055 ++++++++++++++++++++++++++++++++++++++++
 tests/test_client_preflight.py |  193 +++++++-
 tests/test_client_turns.py     |  197 ++++++++
 tests/test_context_budget.py   |   42 ++
 tests/test_http_cancel.py      |  133 +++++
 tests/test_lessons.py          |    3 +-
 tests/test_smoke_gate.py       |   74 ++-
```

## 8. 限制與下一步

1. **full 尚未執行，任務不能宣告完成。** Fable 5.1 MAX reviewer 先對本 digest 做集中靜態 Blocker 審核；歸零後執行一次 full，前後核對同一 product digest、HEAD / index / status，保留完整輸出與真正 exit / 失敗 node。若仍有 Blocker，回 Astra 修，不把 active 問題轉為 deferred。
2. 本轮四條 regression 是離線行為證據；只寫未跑的契約、更新的既有 node、整體既有呼叫端都仍待 full。原 Opus smoke 的 2405 selected / 2404 passed / 1 failed 屬舊 digest `6e92b5cf…`，失敗 node 仍未知，沒有重跑或宣称還原。
3. 被取消時還沒送任何 HTTP 的 DNS / connect / TLS worker 可能較晚才退場；它已被永久禁止送 HTTP bytes，不持模型鎖，不動 session / events。已送 HTTP 的 socket 必須先 shutdown；不是把尚在飛的 model request 丟給背景再提早放鎖。
4. 預熱使用專用 requests session / pool，避免中止牽連正常回合；每次預熱不保留跨 job TCP pool，也不透明 retry。這是背景一次性請求的 transport 取捨，payload、slots 准入、模型鎖與原驗收不變；沒有另外增加設定。
5. 沒有 live HTTP／生成、部署變更、私人 session 讀取；沒有新增產品環境設定讀取、`process_env` 外產品 subprocess、依賴套件或 Node/npm。R1 其他已靜態關閉項未擴修，原計畫未改。
6. **reasoning 與硬體 prefill 成本未變；預熱只把下一輪 prefix 的 prefill 搬到打字之前；熱 prefix 時無感；是否真的被重用只由 T0 的 `prompt_tokens_processed` 判定。** 本輪沒有真實 TTFT 量測，不宣稱需求 b 的收益已實證。

`Tests: smoke only — reviewer owns full execution.` 本輪實際只跑四條新 regression 單 node，各一次紅燈、一次綠燈；未再跑 smoke，full 由 Fable reviewer 在靜態收斂後執行。
