# Step 5 — Fable 5.1 靜態審核 R3（書面交付補齊）

- 審核者：Claude Fable 5.1（harness 自報 model ID `claude-fable-5-1`，argv 明確 `--effort max`），`ROLE=REVIEWER`。日期 2026-09-07。角色依 `00-intake.md` 最新調整：Astra MAX 修復、Fable 5.1 MAX 審核。
- 本檔性質：**延續同型號、同任務 R3 的書面交付**。前一個 Fable 5.1 MAX CLI（session `1cbd8d21-3c8a-4a1d-8e42-ebdc6e65f675`）已對同一份 product digest 完成靜態審核，最後可見回覆為「Static review found zero Blockers」，隨即以 `run_in_background=true` 啟動唯一一次 full 並結束 CLI；該背景 task 被標 `[killed]`，本檔當時沒有寫出來。root 的執行記錄見 `05-review-fable-r3-execution.md`。
- 本 CLI **零測試**：沒有啟動、續跑或補跑任何 test、collect-only、smoke 或 full；沒有修改產品、測試、計畫或其他檔案；沒有 live HTTP、生成、部署、私人 session 讀取、commit／stage／push。唯一 repo 寫入是本檔。
- 本檔**不冒稱**這次又完整重審全部 23 個產品／測試路徑，也不冒稱重跑了任何東西。前次 zero Blockers 的來源、本 CLI 實際做的唯讀核對範圍，在 §2 分開列。四條 Astra regression 的紅／綠 log 只讀取核對，沒有為了核對而重跑。

**結論一句話：靜態 Blocker 為 0（前次 Fable 裁定；本 CLI 對 R2 增量的必要核對沒有發現新 Blocker），但 full 沒有完整結果、沒有 full 基線、T0 未量測，這個版本仍然未完成驗收。**

## 1. 凍結身分（本 CLI 開始與交付各核對一次）

| 欄位 | 值 |
|---|---|
| base HEAD | `f200f697ba54d38a102e8ef66dead652c4002e5f` |
| 前次 R3 審核與 full 啟動時 actual HEAD／index tree | `b7edcd7b52dba7b5f5c00b12976e139191b4e49f`／`91fe953c1034944af4fabb7338715f975d10993b` |
| 本 CLI 開始時 actual HEAD／index tree | `73a215941c4b67f4042e50ae182e12d3eb03f3ba`／`177d9de92f8225e70d6ada65b141e94ab7947e40` |
| product digest（cached 與 worktree 兩個命令） | 均為 `61fda568fce324d31691892f51a01aa7f17831e4e2c3815d2c0e16868dac9b4a` |
| `git status --porcelain=v1` | 23 條產品／測試路徑全部第二欄空白（21 `M `、2 `A `：`http_cancel.py`、`tests/test_http_cancel.py`）；沒有未暫存或未追蹤產品 |

- 兩個 digest 命令與交接一致：`git diff --cached --binary <base> -- . ':(exclude)docs/workflows' | sha256sum` 與不帶 `--cached` 的同命令。
- base 之後的 14 個 commit 全部只動 `docs/workflows/startup-ttft-20260907/`（`git diff --stat <base> HEAD`：20 個交接檔、3081 insertions、零產品路徑）。**HEAD 只有交接 commit，產品未 commit**；審核對象是 base 加上這份凍結 diff，不能說 HEAD 已含產品修正。
- 前次 R3 的 index tree `91fe953c…` 與本 CLI 的 `177d9de9…` 差異只有三個交接檔（`00-model-log.md` +7、`05-review-fable-r3-execution.md` +36、`07-deferred.md` +2），產品 bytes 相同。
- 相對 base 的產品 diffstat：23 檔、3233 insertions／66 deletions，與 `06-fix-r2.md` §7 相同。
- R2 增量身分：`git diff --cached --binary 35c25fd4118112f6d417bbe9cdd2c70ee9ca3f47 -- . ':(exclude)docs/workflows' | sha256sum` 得 `03cd32d28b0b75c0d3a4ddbe372c5a2080a6c292d54fa5794b9406297ba271a5`，與 private `astra-r2.patch` 的 `sha256sum` 相同；本 CLI 讀的就是這份 patch（9 檔、689／93）。
- 交付前（本檔寫入之後）再核對一次 HEAD／index／status／兩個 digest：與上表相同。本檔位於被 ignore、且排除在 digest 之外的交接目錄，不影響任何一項。

## 2. 前次靜態 zero Blockers 的來源，與本 CLI 的核對範圍

### 2.1 來源（root 從前次 CLI 擷取的可見記錄，已排除 reasoning）

- private `fable-r3-visible-evidence.json`：init `model=claude-fable-5-1`、CLI `2.1.263`、session 如上。`00-model-log.md` 記全部 82 個主 assistant frame 同型號、無 fallback event。
- 可見文字結論（逐字）：「The full run is in progress: 3568 nodes across 42 files on 16 shards. Static review found zero Blockers, so the run is authorized; I will resume when the background task completes, then do the post-run identity check and write the report.」這是 Fable 的裁定，root 沒有另行判定產品。
- 前次可見的審核路徑：對 base 的 23 個產品／測試路徑分五批 `git diff --cached`；讀 `client_engine.py`（全文＋兩段重讀）、`llama_client.py`、`context_budget.py` 395–475、`http_client.py`、`scripts/run_tests.py`、`tests/test_smoke_gate.py` 855–1155、`tests/test_repo_consistency.py`（environ／spawn gate 結構）、`client_policy.py`（policy name）；grep `client_app.py`／`client_turns.py` 的呼叫端、`abort_prime`／`hand_off`／`release_late`／`_spawn` 全 repo、五個測試檔的 `pytestmark`、產品與文件是否引用交接目錄；查 requests／urllib3／Python 版本；讀八份 Astra 紅／綠 log；full 前重做 freeze 核對（HEAD `b7edcd7…`、index `91fe953c…`、兩個 digest `61fda568…`，與開始時相同）。
- 執行方式與結局：`python3 scripts/run_tests.py > /tmp/codetrail-startup-ttft-20260907/full-fable-r3.txt 2>&1`，Bash `timeout=600000`、`run_in_background=true`，task `bml0umksx`；CLI result `success`、`is_error=false`、`duration_ms=681838`；task output 檔內容只有 `[killed]`。modelUsage 只有主模型 `claude-fable-5-1` 與 harness 的 `claude-haiku-4-5` 輔助（2996 in／25 out）。**CLI process exit 0 不是 full exit 0。**

### 2.2 本 CLI 的唯讀核對（必要增量，不是完整重審）

1. 交接文件：`AGENTS.md`、`00-intake.md`、`03-plan-final.md`、`05-review-astra-r1.md`、`05-review-astra-r2.md`、`06-fix-r2.md`、`06-fix-r2-execution.md`、`05-review-fable-r3-execution.md`、`07-deferred.md`、`00-model-log.md` 的 R3 段。
2. R2 增量全文（`astra-r2.patch`，digest 已核）；`http_cancel.py` 與 `tests/test_http_cancel.py` 全文；`http_client.create_session()`；`llama_client.chat_completions()`／`get_slots()` 現行實作；`client_engine.py` 的預熱生命週期現行實作（`_ModelSlot` 383–444、`_Abandonable` 474–527、`new_session`／`adopt`／`_session_transition`／`_switch_session` 849–976、`next_turn_prefix`／`_prefix_from` 1033–1063、`prime_prompt_cache`／`_prime_superseded`／`_prime_locked`／`abort_prime` 1071–1313）與正常回合未動的部分（`_open_stream` 1348–1416、`_begin_turn`／`_end_turn`／`request_cancel`／`cancel` 1418–1525）；`heal_in_place` 238–288；`client_turns.prime_in_background` 408–450；`tests/test_smoke_gate.py` 的 `SAFETY_MODULES` 登記與 gate 函式 902–993。
3. 靜態 grep：九個產品檔的 `environ`（只剩 base 既有的 HOME／USERPROFILE 與 `client_engine.py:652` 那行；後者在 base 的第 473 行就是同一行）；`http_cancel.py`／`client_engine.py`／`llama_client.py` 零 `subprocess`／`Popen`／`os.system`／`execv`／`os.spawn`；兩個檔的 module 層 `pytestmark = pytest.mark.smoke`（`tests/test_client_engine.py:41`、`tests/test_http_cancel.py:11`）。
4. 執行證據只讀：八份 Astra 紅／綠 log、`full-fable-r3.txt`、task output、`full-fable-r3-partial/manifest.json` 與 16 個 shard 的 log／`nodes.txt`／14 個 JUnit；`pgrep -af "run_tests|pytest"` 只回本次查詢的 shell，目前沒有任何 runner／shard 行程。

## 3. R2-B01 關閉理由：pending headers 先 shutdown 舊 HTTP，B7 的一秒內放鎖成立

Astra R2 的 Blocker：POST 已送出、headers 未到時，舊修法把模型鎖交給背景 settle，`new_session()`／`adopt()` 之後鎖要等 response 才放，違反定稿 D6／§5 B7「一秒內 aborted、鎖已放、`priming=False`」。R2 修法（靜態逐段核對，行號為現行檔案）：

- **transport 端**（`http_cancel.py`）：`_CancellationAdapter` 用自己的 pool classes（126–131，不改 `urllib3.poolmanager.pool_classes_by_scheme` 共用 dict），連線在 `_new_conn()`（99–103）與 `send()`（105–112）兩處把 socket 交給 `RequestCancellation.register()`，HTTPS 完成包裝後的 SSLSocket 也在送 headers／body 之前登記。`cancel()`（59–64）在同一 `_guard` 內設 Event 並對所有已登記 socket `shutdown(SHUT_RDWR)`＋`close()`，**回傳即代表 shutdown 完成**；第二個取消者在同一把 guard 上等，不能只看到 Event 已設就走。取消後的 `register()` 先關再 raise `RequestCancelled`（52–57），`session()` 取消後 raise（66–68），所以晚回的 connect 拿不到發送資格。
- **engine 端**：`abort_prime()`（1289–1313）在同一 `_prime_guard` 臨界區作廢世代號、設該次的 Event、取走 request 與已登記串流，鎖外關串流、`request.cancel()`（同步 shutdown），再等 `done` 上限 1 秒。`_prime_locked()` 的 POST 等 headers 被中止時（1240–1245）改為 `request.cancel()`＋`post.settle(_close_quietly)` 回 `aborted`，**不再 `hand_off`**。`prime_prompt_cache()` 的 finally（1138–1156）順序固定：先 `request.close()`（等同一次 shutdown 完成，1148），再在 `_prime_guard` 內 `slot.release()`（1150）、清 `priming`／登記、`done.set()`（1156）。因此 `abort_prime()` 回 True 時，舊 HTTP 已 shutdown、模型鎖已放、`priming` 已 False；順序是「shutdown → 放鎖」，不是提早放一個仍在飛的請求。
- **正常回合不受影響**：`_ModelSlot.hand_off()`／`release_late()` 與 `_open_stream()` 的「headers 未到時鎖跟著請求走」仍是正常回合取消的路徑（1348–1416），R2 沒有動；`chat_completions()`／`get_slots()` 的 `cancel` 預設 `None` 走共用 `get_session()`。
- **regression**：`test_switching_session_shuts_down_pending_headers_before_releasing_the_model_lock` 真走 requests／urllib3，只把 `urllib3.connection.HTTPConnection._new_conn` 換成離線 socket；`new` 與 `adopt` 兩條路各斷言：`POST /v1/chat/completions ` 已寫進 socket、一秒內 primer 結束、outcome `aborted`、`priming False`、鎖可取、`shutdown_seen` 且 `lock_was_held_at_shutdown is True`（shutdown 當下鎖仍持有）、零 prime telemetry、capsys 空。紅燈節錄 `AssertionError: B7: headers 還沒回時，模型鎖仍被舊預熱持有`（1 failed），綠燈 1 passed（§7）；這條 node 也在 full 的 shard-3 通過（§8）。
- **既有 node 的變動（§1.5）**：`test_a_session_switch_aborts_a_prime_that_has_no_stream_yet_and_never_sends_after_the_abort` 只改第 (3) 子情況——舊斷言「abort 後鎖仍不可取，人工放 headers 才可取」把 Astra R2 裁定違反 B7 的行為寫成契約；新斷言更嚴：先驗 shutdown 已發生且當下鎖仍持有，再驗目前鎖可取；一秒、aborted、priming False、late stream 關恰好一次、只送一次 POST、telemetry 只有一列等既有斷言保留。行為為什麼該變：這是落實原 B7，不是為了轉綠。
- **精確度註記（非 Blocker）**：一秒上限由 probe／等 headers／串流中三個 I/O 阻塞態的 socket shutdown 保證。若中止時預熱正在 CPU 段（`_prefix_from` 或 `build_usage`），`abort_prime()` 逾時回 False、呼叫端照常換 session；該預熱在下一個邊界（1172、1190、1218、1260、1284 都看 abort／世代號）回 `aborted`、不會 POST，鎖在它結束時放。這是定稿 D6「等不到就回 False，呼叫端照常繼續」的既有語意，不是 R2 新增的偏離。
- 文件同步：`AGENTS.md` §2 `client_engine` 條、`README_DEV.md` 的 `abort_prime`／`http_cancel.RequestCancellation` 兩列、`docs/troubleshooting.md` 的「一秒內收掉預熱並放掉模型鎖；即使 server 還沒回 headers，也不必等它回應或逾時」與實作一致；舊的「response 到了關掉才放」描述已移除。

**裁定：R2-B01 關閉。** 沒有改 B7 驗收、沒有放寬測試、沒有提早放仍在飛的鎖。

## 4. R2-B02 關閉理由：三個 lost-abort 交錯都有准入邊界

- **(a) 登記後、snapshot 前的中止被新 snapshot 吸收**：`prime_prompt_cache()` 1116–1129 在 `_turn_state` → `_prime_guard` 的同一臨界區完成世代號、歷史 snapshot、Event、done、request、`priming` 的登記；`_prime_locked()` 吃這份 snapshot 與這個 epoch，不再重取。`_prime_locked()` 第一行（1172）就看 abort／世代號；`_Abandonable.wait()`（500–507）即使 I/O 已完成也回 `not abort.is_set()`。regression `test_abort_between_prime_registration_and_fast_io_cannot_be_lost` 用 barrier 停在登記後、I/O 前，再讓 GET／POST 立即完成：紅燈實得 `PrimeOutcome(sent=True, reason='', processed_tokens=7)`；綠燈 `aborted`、零 POST、零 telemetry。
- **(b) 最後一次 epoch 檢查通過後、HTTP worker 開始前的中止**：`_Abandonable._run`（490–491）執行前先查 abort；這個檢查與真正 I/O 之間若又取消，transport 的 `check()`／`register()` 在 HTTP bytes 之前擋下，`session()` 取消後 raise。regression `test_a_prime_http_worker_scheduled_after_abort_never_starts_the_post` 把 HTTP worker 延後排程、取消後再放行：紅燈 `Left contains one more item`（補送了舊 POST）；綠燈零 POST、`aborted`、鎖可取。晚回 HTTP／HTTPS connect 的 transport 契約在 `tests/test_http_cancel.py`（只寫未跑，full 也未達，見 §8）。
- **(c) 首次 abort 後、session 寫定前的空窗**：`_session_transition()`（944–958）先在 `_prime_guard` 內 `_session_switching += 1`，再 `abort_prime()`；整段 `store.create()`／`adopt`／`_switch_session()` 期間，新到的預熱在 1120–1121 直接回 `aborted`、不登記、不取舊歷史；finally 恢復准入，create 失敗不替換 session 狀態。regression `test_a_prime_arriving_during_session_creation_cannot_keep_the_old_history_alive`：紅燈 primer `is_alive()` 為 True（換好 session 後仍卡在舊串流）；綠燈 `aborted`、`engine.messages == []`、鎖可取。錯誤路徑契約 `test_failed_session_creation_does_not_disable_future_priming` 只寫未單跑，但在 full 的 shard-3 通過。
- **串流中與最後落地**：迴圈每個 chunk 先看 abort／世代號（1260–1261）；`log_metrics` 與 sent 的判定在 `_prime_guard` 內再核一次（1283–1287），與 `abort_prime()` 設 Event 的臨界區線性化，不會寫出假 sent。
- **鎖序**：`_turn_state → _prime_guard` 只出現在 `prime_prompt_cache()`；`_switch_session`／`abort_prime`／`_session_transition` 只取 `_prime_guard`；`_begin_turn`／`_end_turn`／`clear_cancel`／`request_cancel`／`_decide_commit` 只取 `_turn_state`（`request_cancel` 內嵌 `_active_lock`）；`RequestCancellation._guard` 不回呼 engine。沒有反向取鎖，`abort_prime()` 等 `done` 時不持任何 engine 鎖。
- **零寫入邊界保留**：預熱仍不進 `_begin_turn`／`_end_turn`、不 `_record`、不發事件、不碰 `_cancel`／`_armed`／`_active_stream`／`_active_call`；`request_cancel()`／`cancel()` 未動。

**裁定：R2-B02 關閉。**

## 5. 既有四項 R1 closure（維持 Astra R2 §2 的靜態關閉，補 full 落點）

| R1 項目 | 現行依據 | full 落點 |
|---|---|---|
| B01 quiet 契約測試殘留 stub | R2 增量未動該 node；恢復真 `get_slots`、mock 底層 session 的修法不變 | `test_priming_skips_only_when_no_slot_is_idle_and_never_raises_or_prints` 在完整的 shard-3 通過 |
| B03 部分完成工具群組的 prefix 不等 | `heal_in_place` 238–288 未動：先越過群組既有 tool 結果再插入缺少的結果 | `test_priming_matches_the_next_send_when_a_tool_group_is_only_partly_answered` 在 shard-3 通過 |
| B04 壓縮後 `/status` 不更新 | `client_turns.prime_in_background` 408–450 未動：每次預熱先 `on_prime(reason, outcome)` 再 `on_done`，兩個壓縮入口同路 | turns 側 `test_a_compaction_that_replaced_the_history_primes_after_the_turn_lock_is_released`、`test_every_prime_the_coordinator_runs_reports_through_on_prime` 在完整的 shard-1 通過；app 側 regression `test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction` 在**未完成的 shard-9**，唯一執行證據仍是 R1 的 `b04-red.txt`／`b04-green.txt` |
| B05 非終結 EOF 記成 sent | 1274–1282 的 `incomplete`／`no_timings` 保留，最後 sent 再多一道 abort／世代號核對 | `test_priming_only_counts_as_sent_after_a_terminal_chunk_with_timings` 在 shard-3 通過 |

## 6. AGENTS §2 增文、安全 gate、既有測試變動與正常呼叫端

- **AGENTS §2 `client_engine` 條的新句**與實作逐句對應：「HTTP bytes 送出前登記 socket」＝`http_cancel.py` 99–112；「headers 未到也先 shutdown 舊 HTTP 才放模型鎖」＝1243 與 1148→1150 的順序；「取消後才完成的 connect 先關再拒絕送出」＝52–57；「並行取消都等 shutdown 完成」＝59–64 同一 guard；「保留 TLS 驗證、無 env proxy／netrc 與不跟 redirect，不改共用 session／pool」＝`session()` 以 `create_session()` 起（`trust_env=False`、`max_redirects=0`）、只換自己的 adapter、pool classes 只設在 instance；「token、Event 與歷史同時登記」＝1119–1129；「session 轉換期間不准入舊歷史，create／adopt 失敗也要恢復准入」＝944–958；「一秒內 aborted、鎖已放、`priming=False`」＝§3。
- **`SAFETY_MODULES`**：`test_client_engine.py` 鍵追加 5 名（433–440 的後五條），新增 `test_http_cancel.py` 鍵帶 3 名（443–451）；8 名在檔內都存在、各登記一次；兩檔都有 module 層 smoke，四條 regression 與 engine 契約另各帶 decorator。gate 函式 951–973 檢查「node 存在」與「帶 smoke」兩件事，不只驗檔名。以上為本 CLI 的靜態核對；**gate 本身（`tests/test_smoke_gate.py` 35 條）在這次 full 沒有執行證據**（§8）。
- **既有測試變動（§1.5）**：R2 增量只動一條既有 node 的第 (3) 子情況（理由見 §3）與 `SAFETY_MODULES` 的說明文字；沒有刪除、skip、xfail 或放寬任何 safety 斷言。R1 的既有測試變動已在 `06-fix-r1.md` 逐條列出並經 Astra R2 核對，不重列。
- **正常呼叫端**：`chat_completions(..., cancel=None)`／`get_slots(..., cancel=None)` 是選用 keyword，engine 正常回合、`agent.py`、vision／canary、`gpu_safety`／`utils`／`scripts/doctor.py` 都不傳它，走既有共用 session；`get_slots()` 把取 session 移到 `_ensure_allowed()` 之後、`try` 內，對呼叫端行為不變。`llama_client` 新 import `http_cancel`，後者只依賴 `http_client.create_session` 與 requests／urllib3，沒有循環 import；預熱專用 session 不透明 retry、每次預熱各自建立並在 finally 關閉。
- **環境與 spawn**：新增檔零 `environ` 讀取、零 spawn；產品其餘 `environ` 只有 base 既有的 HOME／USERPROFILE（檔案在哪）與 `client_engine.py:652` 的整份環境轉交（base 第 473 行同一行）。R2 增量的三處文件文字沒有環境變數、沒有引用交接目錄。靜態 gate `tests/test_repo_consistency.py` 在 shard-4，這次 full 沒有完整結果（§8）。

## 7. 紅／綠證據核對（只讀，未重跑）

八份 log 位於 owner-only `/tmp/codetrail-startup-ttft-20260907/`，命令均為 `python3 scripts/run_tests.py tests/test_client_engine.py::<node>`，stdout／stderr 純 redirect；每份 `collected 1 item`，不是 exit 5。log 檔本身不含 exit code 行，exit 值以 `06-fix-r2.md` §4 記錄的 exec 工具結果為準。

| node | 紅燈（`-red.txt`） | 綠燈（`-green.txt`） |
|---|---|---|
| `test_switching_session_shuts_down_pending_headers_before_releasing_the_model_lock` | 1 failed in 0.39s；`AssertionError: B7: headers 還沒回時，模型鎖仍被舊預熱持有` | 1 passed in 0.42s |
| `test_abort_between_prime_registration_and_fast_io_cannot_be_lost` | 1 failed in 0.24s；`已取消的登記不能記成 sent`，實得 `PrimeOutcome(sent=True, reason='', processed_tokens=7)` | 1 passed in 0.28s |
| `test_a_prime_http_worker_scheduled_after_abort_never_starts_the_post` | 1 failed in 0.30s；`已取消後才排到 CPU 的 HTTP worker 仍發出舊 POST`，`Left contains one more item` | 1 passed in 0.34s |
| `test_a_prime_arriving_during_session_creation_cannot_keep_the_old_history_alive` | 1 failed in 1.05s；`session 已換，空窗內登記的舊預熱仍未被中止`，primer `is_alive()` 為 True | 1 passed in 0.26s |

四個紅燈都是本次錯誤行為的斷言失敗，不是缺方法／import 失敗的假紅。R1 的四份紅／綠（`engine-b02-abort-no-stream`、`engine-b03-partial-group`、`engine-b05-terminal-chunk`、`b04`）維持 Astra R2 §5 的核對，本輪未再讀改。

## 8. full 的真實狀態（本 CLI 未執行任何測試）

### 8.1 執行與中斷

- 唯一命令、task 與結局見 §2.1。runner stdout `full-fable-r3.txt` 只有選取與 16 個 shard 的分配行（3568 條／42 檔），沒有任何 summary 行；task output 檔只有 `[killed]`。**沒有 full 的完整 exit code；CLI 的 exit 0／result success 不是 full exit 0，不可推測為通過。** 目前沒有殘留的 runner／shard 行程。

### 8.2 保存證據的核對（`full-fable-r3-partial/`，root 從 runner 暫存目錄 `/tmp/codetrail-pytest-m9__dowk` 複製）

| shard | 選取 | JUnit `tests`／`failures`／`errors`／`skipped` |
|---|---|---|
| 1 | 232 | 232／0／0／0 |
| 2 | 232 | 232／0／0／0 |
| 3 | 233 | 233／0／0／0 |
| 5 | 225 | 225／0／0／0 |
| 6 | 230 | 230／0／0／0 |
| 7 | 225 | 225／0／0／0 |
| 8 | 218 | 218／0／0／0 |
| 10 | 217 | 217／0／0／0 |
| 11 | 219 | 219／0／0／0 |
| 12 | 218 | 218／0／0／0 |
| 13 | 218 | 218／0／0／0 |
| 14 | 220 | 220／0／0／0 |
| 15 | 217 | 217／0／0／0 |
| 16 | 217 | 217／0／0／0 |
| **合計 14 個完整 shard** | **3121** | **3121 passed；14 個 JUnit 內沒有任何 `<failure>`／`<error>`／`<skipped>` 標籤** |
| 4 | 232 | **無 JUnit**；log 最後可見進度在 `tests/test_set_config.py`，沒有 summary 行 |
| 9 | 215 | **無 JUnit**；log 最後可見進度在 `tests/test_client_app.py`，沒有 summary 行 |

與 manifest 的 `selected=3568`、`completed=3121`、`incomplete_shards=[shard-4, shard-9]`、`task_output="[killed]"` 一致。依 root 規則，**進度點不用來推算任何個別 node 的狀態**：兩個未完成 shard 的 447 條全部視為「沒有完整結果」。

### 8.3 447 條沒有完整結果的 node 是哪些（依 `nodes.txt`）

- shard-4（232）：`tests/test_aicode.py` 27、`tests/test_repo_consistency.py` 64、`tests/test_set_config.py` 106、`tests/test_smoke_gate.py` 35。
- shard-9（215）：`tests/test_client_app.py` 46、`tests/test_code_rag_search.py` 93、`tests/test_http_cancel.py` 4（三條契約，其中一條 parametrize 為 http／https 兩個 node）、`tests/test_mcp_ingest.py` 72。

### 8.4 本次變更相關 node 的落點

- **有完整通過結果**：`tests/test_client_engine.py` 107 條全部在 shard-3，JUnit 逐名可見全部 16 條預熱 node（8 條 Lane B 契約、3 條 R1 regression、被改的既有 node、4 條 Astra regression、create 失敗契約）；`tests/test_client_turns.py` 在 shard-1（含 3 條預熱 node）；`tests/test_context_budget.py` 在 shard-13（含 `test_processed_prompt_tokens_are_recorded_separately_from_the_total`）；`tests/test_client_cli.py` 在 shard-5（含需求 a 的 red-before-green node 與 `test_headless_run_never_primes_the_prompt_cache`）；`tests/test_client_preflight.py` 在 shard-7（含兩條 banner／note 契約）；`tests/test_lessons.py` 在 shard-10。
- **沒有完整結果**：`tests/test_http_cancel.py` 的三條 transport 安全契約（Astra 只寫未跑，這次 full 也未達，**至今零執行證據**）；`tests/test_client_app.py` 的 `test_the_tui_primes_on_mount_new_and_session_switch_through_the_coordinator` 與 R1-B04 的 app 側 regression（後者只有 R1 單 node 紅／綠）；`tests/test_smoke_gate.py` 全部 35 條（含 `[test_client_engine.py]`、`[test_http_cancel.py]` 兩個 gate 案例）；`tests/test_repo_consistency.py` 的 environ／spawn／docs gate；`07-deferred.md` B-6 擔心的 app 測試背景預熱副作用正好落在 shard-9 的 `test_client_app.py`，這次 full 無法確認或否定。

### 8.5 判定

- AGENTS §1.2：失敗 node 集合不得大於動工前基線。**本版沒有 full 基線**（前版 smoke 對 `6e92b5cf…` 的 1 failed node 從未還原；R1／R2 各版都沒有整包結果），所以不能把 447 條中的任何未知失敗當成既有。
- 3121 條完整通過是對同一 digest `61fda568…` 的真實 per-node 證據，可以保留；但 full 沒有完整 exit、447 條沒有結果，**full 未完成＝未通過**，不得回報成功。
- 這不是靜態產品 Blocker，不是模型技術分歧，也不是正式 deferred；是測試執行未完成。

### 8.6 補跑（待使用者授權，本 CLI 不假設、不啟動）

- root 已在 `05-review-fable-r3-execution.md` 說明：再執行一次 full 需要使用者針對這次 CLI 中斷的明確授權；AGENTS §1.2 同時規定程式碼未變時不得重跑已通過的測試。兩種可行形狀，由使用者決定：
  1. **只補兩個未完成 shard 的 447 條**（建議）：`python3 scripts/run_tests.py tests/test_aicode.py tests/test_repo_consistency.py tests/test_set_config.py tests/test_smoke_gate.py tests/test_client_app.py tests/test_code_rag_search.py tests/test_http_cancel.py tests/test_mcp_ingest.py`（帶檔案參數＝單行程逐字轉發），與 14 個既有 JUnit 合併成本版的完整 per-node 結果。不重跑 3121 條已通過的 node，符合 §1.2；但「一次 full」變成兩段執行，需使用者接受。
  2. **整包重跑一次 full**：`python3 scripts/run_tests.py`，取得單一完整 exit；代價是重跑已通過的 3121 條。
- 不論哪一種：同一 product digest `61fda568…`、執行前後各記 HEAD／index／status／兩個 digest、前景 Bash 等到完整結果（禁止 `run_in_background=true`、禁止提前結束 CLI）、完整 stdout／stderr 直接 redirect 保存、記實際 exit code。任何失敗 node 都是基線外新失敗，回 Astra 修，不轉 deferred。

## 9. 驗收狀態、deferred 與誠實揭露

- **未完成驗收**：靜態 Blocker 0，但 full 未完成（§8）、T0 未量測。
- **固定句**：reasoning 與硬體 prefill 成本未變；預熱只把**下一輪 prefix** 的 prefill 搬到打字之前；熱 prefix 時無感；是否真的被重用只由 T0 的 `prompt_tokens_processed` 判定。規劃、施工、回修、審核到本輪為止沒有任何 TTFT 數字，**不宣稱需求 b 的首字延遲收益已實證**。
- **deferred**：沒有新增正式擱置；`07-deferred.md` §A 原案十項狀態不變。§B 現況：B-1 由 R2 修法解決（§3），不再是落差；B-2／B-3 依 Astra R2 §6 不列 Blocker，本輪未變；B-4 被本版 3121 條完整通過取代，但 447 條未知；B-5（`docs/basic-usage.md:39` 未列預熱行）仍未動，非安全項、非 Blocker；B-6 因 shard-9 未完成而無法確認。
- **本 CLI 的邊界**：零測試、零產品／測試修改、零 live HTTP、零私人 session、零 commit／stage／push、零 memory 與全域設定變更。

## 10. 結論

1. **靜態：Blocker 0。** 前次 Fable 5.1 MAX 的 zero Blockers 裁定來源如 §2.1；本 CLI 對 R2 增量做的必要唯讀核對（§3–§6）沒有發現新 Blocker，R2-B01／R2-B02 關閉，R1 四項維持關閉。
2. **驗收：未通過、未完成。** full 沒有完整 exit，447 條（含 `test_smoke_gate.py`、`test_http_cancel.py`、`test_client_app.py` 的預熱 node、`test_repo_consistency.py` 的 gate）沒有完整結果；沒有 full 基線；T0 未量測。
3. **下一步**：使用者答覆補跑授權（§8.6 兩種形狀，建議只補 447 條）；reviewer 前景執行、前後核對同一 digest、保留完整輸出與實際 exit；全綠才算本版 full 通過，任何失敗回 Astra；產品仍不 commit／push。
