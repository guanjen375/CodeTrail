# Step 5 — Astra 靜態審核 R2

審核者：Astra MAX，`ROLE=REVIEWER`。結論：**2 項 Blocker，均在預熱取消邊界；R1-B02 尚未關閉。其餘四項 R1 問題可按下表關閉靜態審核。**

**本輪沒有執行任何 tests／smoke／full。** 靜態 Blocker 未歸零，未進入獲准的 full 階段，也未建立 `full-r2.txt`。以下紅／綠結果是讀取 Fable 已保存的執行證據，不是 Astra 補跑的結果。不能宣告整包通過或工作完成。

## 1. 範圍與凍結身分

完整讀取最新 `AGENTS.md`、`03-plan-final.md`、`05-review-astra-r1.md`、`06-fix-r1-dependencies.md`、`06-fix-r1-engine.md`、`06-fix-r1-status.md`、`06-fix-r1.md`、`07-deferred.md`。以 R1 的產品樹 `9006b9244f1f3f5764f8a56ca7bbdcc4750e66ba` 比較本次增量，核對 10 個改動路徑及受影響的正常回合、session 切換、healing、壓縮與 TUI 呼叫端；其他 11 個產品路徑與 R1 相同。

| 欄位 | 值 |
|---|---|
| base HEAD | `f200f697ba54d38a102e8ef66dead652c4002e5f` |
| R2 開始審核、整合者凍結與交付前最後核對的 actual HEAD | `b25a551c0c3ca57b4f34035400bca04d7e516778` |
| 整合者凍結 index tree | `35c25fd4118112f6d417bbe9cdd2c70ee9ca3f47` |
| R2 product diff SHA-256 | `16bda189fe8d6d99edfd1ebc86b59a373ac2a1816389d98d5c59ab41731f9349` |

審核者重新計算下列兩種 digest，均與上表相同：

```text
git diff --cached --binary f200f697ba54d38a102e8ef66dead652c4002e5f -- . ':(exclude)docs/workflows' | sha256sum
git diff --binary f200f697ba54d38a102e8ef66dead652c4002e5f -- . ':(exclude)docs/workflows' | sha256sum
```

21 個產品／測試路徑皆為 `M `，沒有未暫存或未追蹤產品；交接目錄外的內容已凍結。**HEAD 只有交接 commit，產品未 commit；審核綁定 base 加上此 diff，不能說 HEAD 已包含產品修正。**

本輪沒有修改產品／測試／計畫，沒有 live HTTP、生成、部署、私人 session 讀取或 commit／push。唯一 repo 寫入為本檔。

## 2. R1 問題核對

| R1 項目 | R2 靜態結論 | 可核查依據與執行限制 |
|---|---|---|
| B01：quiet 測試呼叫殘留 stub | **靜態關閉** | `tests/test_client_engine.py:2393` 保存真正的 `get_slots`，`:2450` 在末段恢復它，再 mock 底層 session；quiet／預設兩側斷言保留。失效路徑已移除，但本條沒有新執行結果，不能稱已轉綠。 |
| B02：取得 stream 前中止不了 | **未關閉** | probe 等待與若干 POST 前邊界已修；下列 R2-B01／R2-B02 仍違反 B7 或中止契約。新 regression 的綠燈不涵蓋全部交錯，且其中明確斷言 POST 待 headers 時鎖仍不可取。 |
| B03：部分完成工具群組的 prefix 不等 | **靜態關閉** | `heal_in_place():238`–`:287` 先越過群組既有 tool 結果，再插入缺少結果；R1 的 `assistant(a,b) → tool(a)` 情境與真實 `send(q)` 等價。原始 messages／store 不由 prime 改寫。`payload_messages()`、`_prefix_from()`、`prune_for_summary()` 都使用此實作，原先無既有結果的 healing 位置不變。新 regression 已有一紅一綠。 |
| B04：壓縮後 `/status` 不更新 | **靜態關閉** | `client_turns.py:437`–`:447` 統一先 `on_prime(reason, outcome)` 再 `on_done`，兩個壓縮入口也經此路；TUI 建構時接入回呼，三元組的唯一產品讀取端與相關既有測試索引已同步。新 regression 比對手動及自動壓縮後不同 outcomes，已有一紅一綠；另新增回呼契約尚未執行。 |
| B05：非終結 EOF 記成 sent | **靜態關閉** | `client_engine.py:1228`–`:1253` 區分 `incomplete`／`no_timings`，兩者不寫 prime 列；正常 length 終結加 timings 才 sent，沒有用總 prompt 數代填。R1 要求的三種失敗形狀與正常形狀已在新 regression 留下一紅一綠。 |

「靜態關閉」只表示本次指出的失效路徑已修，不能替代尚未執行的 full，也不把上一版 smoke 的結果移植到本版。

## 3. R2-B01 — Blocker：B7 的一秒內放鎖仍未成立

- **位置：**`client_engine.py:1209`–`:1216` 將租約交棒，`:1118`–`:1122` 在鎖仍由背景持有時設定 done，`:1263`–`:1282` 將這種情況回報為預熱已結束；呼叫端 `new_session():853`、`adopt():931`。新測試 `tests/test_client_engine.py:2788` 明確斷言鎖不可取，直到 `:2789` 人工放開舊 POST 後才驗釋放。`docs/troubleshooting.md:217`–`:219` 已把這個等待寫入文件。
- **觸發與失效路徑：**預熱 POST 已送出，headers 延遲超過一秒。切換 session 後，控制預熱的執行緒可以迅速回 `aborted`、清 `priming`，但 `_Abandonable` 的舊 HTTP 仍在執行，模型鎖交給其 settle 執行緒，直到 response 返回並關閉才釋放。新 session 的預熱仍會 `model_busy`，真實第一題仍要等舊請求，最長受 `options.request_timeout` 限制。改成結束控制執行緒，沒有滿足「舊請求已收掉、鎖可用」的原驗收。
- **違反：**`03-plan-final.md` D6 與 §5 B7 明訂 `new_session()`／`adopt()` 後一秒內舊預熱 `aborted`、**鎖已放**、`priming=False`；§4.2 的收尾要求也沒有此例外。`06-fix-r1.md`、`07-deferred.md` 均明寫未修改此驗收、未正式擱置，所以不能據披露認定合格。
- **裁定：**保留仍在飛的 HTTP 租約，符合 AGENTS 的模型序列化安全要求；**它不等於 B7 已滿足**。R1 並未要求單獨提早放鎖，而是要求中止舊請求後仍滿足期限。本輪不能把「這種 transport 用法沒有可關 handle」擴張成所有修法都不可能，也不能以未量測的「通常毫秒級」關閉此項。
- **最低可驗證修復條件：**由 Fable 提交能同時維持 HTTP 序列化與仍適用 B7 的修法，並以超過一秒才會回 headers 的受控離線情境，核對新 session 的舊預熱收尾、outcome、`priming` 與模型鎖。不得只提早放仍有舊 HTTP 在跑的鎖，也不得以變更測試預期／docstring 代替驗收。若 Fable 判斷需變更驗收，須按使用者授權的規劃／審核流程明確處理；本輪維持 Blocker，**不是擱置**。

## 4. R2-B02 — Blocker：abort 登記、snapshot 與 POST 准入仍有 lost-abort 交錯

- **位置：**`client_engine.py:1095`–`:1100` 登記本次 abort Event，`:1141`–`:1144` 才取 epoch；`_Abandonable.wait():498`–`:503` 僅在等待未完成時讀 abort；`:1189`–`:1208` 的最後 epoch 檢查與背景 POST 啟動分離，helper `:488`–`:496` 無條件呼叫 `request()`；`_switch_session():950`–`:955` 只遞增 epoch，串流迴圈 `:1230`–`:1242` 到迭代結束才核對 epoch。
- **觸發與失效路徑（靜態可逐步核對，未執行）：**

  1. **已登記的中止被新 snapshot 吸收。** 預熱先登記 Event／done，尚未進 snapshot；`abort_prime()` 在此時遞增 epoch 並設該 Event。預熱恢復後卻把已遞增的 epoch 當成本次起點。若 GET／POST 都在 `wait()` 的第一次等待內完成，`wait()` 直接回 True，根本不讀已設的 abort Event；後續 epoch 比對也相等。這次已被要求中止的工作仍會 POST，甚至可回 sent／寫 prime 列。`new_session()` 正在 `abort_prime()` 的等待內時，歷史仍是舊 session，因此不是「執行當下取到新歷史」的准許情況。
  2. **通過檢查，不代表 POST 已開始。** epoch 檢查通過後，`codetrail-prime-http` 尚未執行 `request()`；使用者此時中止。控制執行緒可以回 `aborted` 並把租約交棒，但 helper 之後仍無條件執行那份舊 POST。這與 R2-B01「中止前 POST 已在飛」不同：**HTTP 在中止後才開始**，目前沒有最後的請求准入邊界攔它。
  3. **session 切換使 epoch 失效，沒有叫醒已登記串流。** `new_session()` 的第一次 abort 返回後、`store.create()`／實際 `_switch_session()` 之前，晚排到的舊工作可以取舊歷史並登記 stream。`_switch_session()` 只加 epoch，不設該工作的 Event、不關已登記 stream；串流迴圈也不在每次讀取前／chunk 後看 epoch。於是即使此時已有可關的 stream，切換也不會中止它，而要等到串流 EOF 才回 `aborted`。這不是 headers 無 handle 的例外。

- **違反：**AGENTS §2 本輪新增的「中止涵蓋 probe／headers 前／串流中，中止後不再發 POST」；定稿 D6／§5 B7；R1-B02 明訂須覆蓋中止與准入交錯。上述情境也直接否定 `_switch_session` docstring 的舊 snapshot 會被及時收掉保證。
- **現有證據缺口：**新 regression `tests/test_client_engine.py:2710`–`:2794` 涵蓋已卡在 probe、在 gate 回傳前中止、以及 POST 已進入替身之後中止。它沒有停在「Event 登記後／snapshot 前」、「最後 epoch 檢查後／helper 開始前」或「首次 abort 後／session 寫定前且 stream 已登記」；其綠燈不能證明上述交錯成立。
- **最低可驗證修復條件：**本次工作身分、中止狀態與實際 I/O 准入須對齊，已接受的中止不得因後取 epoch 或快速 I/O 而消失，尚未開始的 POST 不得在中止後補送；session 寫定時也必須能中止在首次 abort 空窗中開始、仍使用舊歷史的工作。用受控 barrier 分別覆蓋以上三個位置，核對零晚發 POST、無假 sent／prime 列、已登記 stream 被關閉及 B7 的收尾狀態。保留正常 `_cancel`／`_armed`／session append／事件流的零寫入邊界，以及已在飛 HTTP 不得提早解鎖的要求；具體修法由 Fable 決定。

## 5. 紅／綠證據與測試權責

已讀取 `/tmp/codetrail-startup-ttft-20260907/` 下八個完整 log。以下均是 Fable lane 的既有單 node 執行；每次 collected 1，非 exit-5 異常。

| node | log 檔前綴（各有 `-red.txt`／`-green.txt`） | 紅燈 | 綠燈 |
|---|---|---|---|
| `test_priming_matches_the_next_send_when_a_tool_group_is_only_partly_answered` | `engine-b03-partial-group` | 1 failed，exit 1；tool a／b 排序不等 | 1 passed，exit 0 |
| `test_priming_only_counts_as_sent_after_a_terminal_chunk_with_timings` | `engine-b05-terminal-chunk` | 1 failed，exit 1；空串流回 sent | 1 passed，exit 0 |
| `test_a_session_switch_aborts_a_prime_that_has_no_stream_yet_and_never_sends_after_the_abort` | `engine-b02-abort-no-stream` | 1 failed，exit 1；probe 中止後執行緒仍活著 | 1 passed，exit 0；不代表 B7 全部滿足 |
| `test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction` | `b04` | 1 failed，exit 1；壓縮後仍為 mount 的 sent | 1 passed，exit 0 |

命令均為 `python3 scripts/run_tests.py <檔案::node>`、stdout／stderr 純 redirect 保存。檔尾 exit 行由 lane 按 harness 狀態追記，兩份報告已揭露；紅燈本體與未修行為相符。本審核沒有重新執行或聲稱另行重建紅燈當時的產品 snapshot。

新增 5 個 node 均已登記 `SAFETY_MODULES`（engine 3、app 1、turns 1），函式存在且有 module 或逐條 smoke 標記。B01 的原測試恢復真函式、app 既有測試三元組索引更新及 gate 追加，皆已在交接逐條列明，未見刪弱既有斷言或新增 skip／xfail。新增 `on_prime` 契約只寫未跑，符合本輪分工。

**本版沒有整包 smoke／full 結果。** 上版 `6e92b5cf…` 的 2405 selected／1 failed 不代表本版通過或失敗，實際失敗 node 也未還原；沒有 full 基線，不能擅自把未知失敗認作既有。由於本輪仍有 Blocker，不執行 full 或任何額外測試。

## 6. 其餘指定事項的裁定

- **正常回合取消／模型鎖：**本輪 `_ModelSlot` 新增 `try_lease()`／`release()`；正常回合的 `__enter__`、`__exit__`、`hand_off`、`release_late` 與 `_open_stream` 使用方式未變，未發現需另列的正常回合回歸。R2-B01 不要求削弱這套 HTTP 序列化；R2-B02 要修的是新預熱生命週期。
- **healing／壓縮：**新插入順序保留群組相鄰性與原始資料；三個使用端已一起核對。沒有把與本次無關的歷史資料形狀或既有 call-id 行為擴成另一項要求。
- **`on_prime`／既有呼叫端：**產品建構點僅 TUI，新增參數是 optional keyword；測試協調器 helper 用 kwargs 轉交。TUI `_last_prime` 產品讀取端與測試形狀已同步，回呼例外各自隔離，不讓 prime 進 busy／cancel／對話事件。
- **`07-deferred.md` B-2／B-3：**Fable 本輪已明定 `/status` 記「最後落地」的 outcome，並顯示觸發點；這與「最後排程的工作」不同，不能拿這行當當前 session 的 cache 命中證明。僅就這個既定介面，以及晚到工作在切換**完成後**取新歷史的 D6 行為，不另列 Blocker，也不要求增加排序功能。R2-B02 指出的則是中止後發舊請求／舊 stream 未收掉，與這兩項揭露不同。
- **deferred：**`07-deferred.md` §A 原案範圍維持；§B 的 B7 問題本輪判為 Blocker，不能搬成已擱置。首次回修的披露不等於達到「分歧超過兩輪」門檻。B01 是否通過仍待後續整包執行；basic-usage 的概述、既有 app 替身多出背景回呼，未發現本輪可成立的額外 Blocker。
- **收益與離線邊界：**四 slot 准入、readonly／headless 防線與 gate 保留額 1 未改；沒有跑 live HTTP 或 T0。需求 b 的實機收益仍未量測。runner 純 `-m` 分片與原 AGENTS 文字的差異屬既有無關項，本輪不擴修。

## 7. 交付

集中交 Fable MAX 回修 R2-B01、R2-B02；Astra 不改產品，也不要求 root 代做設計裁決。回修依 AGENTS 留新的必要 regression 紅／綠證據、逐條說明既有測試變更，再重新凍結產品。靜態審核歸零後才對同一個被認可的 digest 執行一次 full，完整保留輸出與實際 exit code。

**本輪 2 項 Blocker；tests／full 均未執行，工作尚未完成。**
