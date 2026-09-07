# Step 5 — Astra 靜態審核 R1

審核者：Astra MAX，`ROLE=REVIEWER`。本輪結論：**5 項 Blocker，不能放行。full 尚未執行，不能宣告完成。**

本輪完整讀取現行 `AGENTS.md`、`03-plan-final.md`、四份 `04-impl-*.md` 與 `04-integration.md`，以整合後的產品、測試與相關既有呼叫端做靜態審核。沒有執行 tests、full、live HTTP、產品程式或私人 session 讀取；沒有修改產品／測試／計畫，也沒有 commit／push。唯一寫入為本檔。

## 審核內容身分與既有執行證據

| 欄位 | 值 |
|---|---|
| 產品 base HEAD | `f200f697ba54d38a102e8ef66dead652c4002e5f` |
| smoke／開始審核時 actual HEAD | `22cfc19d2e1ef87bc5e6b68fb1e73c997b2a9319` |
| smoke／整合者原始凍結 index tree | `9006b9244f1f3f5764f8a56ca7bbdcc4750e66ba` |
| 本報告交付時 actual HEAD | `ca6548d2b5d7dca2e0124bf28613ad13f29313fa`；root 新提交的六份交接文件，沒有產品修改 |
| 本報告交付時 index tree | `bbe47fb2f032f0617e49c821cc3de7c8c6dc3c03`；依 `git ls-files --stage -z` 只讀計算 Git tree hash，未寫 Git objects |
| 產品 diff SHA-256 | `6e92b5cf7639e13e83b62b7ca172677c72a3d839427e68e1f928ca8f31d8c7da` |

本審核者以 `git diff --cached --binary <base> -- . ':(exclude)docs/workflows'` 及不帶 `--cached` 的同命令，分別重新計算 SHA-256，兩者均等於上列 digest，與開始審核時相同。21 個產品／測試路徑全部為 `M `，沒有未暫存產品變更或未追蹤產品。審核期間 root 提交了 `00-model-log.md`、四份 lane 報告與整合報告，因此 HEAD／完整 index tree 改變；這些交接內容在產品 digest 外，產品字節沒有改變。**審核對象是 base HEAD 加上這份凍結 diff，不能稱為產品已 commit 的 HEAD。**

整合者唯一一次 `python3 scripts/run_tests.py -m smoke` 的既有證據為：2405 selected、2404 passed、1 failed、0 errors、0 skipped，exit 1，失敗 shard 9。失敗段被 CLI 截斷，runner 的 TemporaryDirectory 已刪除，未還原實際失敗 node。本輪沒有重跑。下列 R1-B01 是由原始碼可確定的新測試失效路徑，**不是聲稱找回了那次 runner 的失敗 node**。compileall／README consistency 的通過僅引用整合報告，不能代替 smoke 或 full。

## R1-B01 — Blocker：quiet 契約測試仍呼叫 stub，末尾斷言必敗

- **位置：**`tests/test_client_engine.py:2424`、`:2446`–`:2450`；node 為 `test_priming_skips_only_when_no_slot_is_idle_and_never_raises_or_prints`，已登記於 `tests/test_smoke_gate.py:424`。
- **觸發與失效路徑：**第 2424 行把 `llama_client.get_slots` 替換為只記 probe、回 `None` 的 `_slots_returning(None)`。直到測試結束都未恢復。第 2446 行只替換 `get_session`，所以第 2447／2449 行仍呼叫前述 stub，根本不會進真正的 HTTP probe／錯誤列印。第 2438 與 2448 行又已清空 capture，第 2450 行要求 stderr 含 `/slots probe failed` 因而必敗。同時，前一個「quiet 零 stderr」斷言其實也沒有測到真正 `get_slots`。
- **違反：**AGENTS §1.2 基線外新失敗不得交付；定稿 §5 B8 的 quiet／既有呼叫端契約及 §6 整合 smoke。這條 node 是本次新增，不能列為動工前基線失敗。
- **最低可驗證修復條件：**quiet 與預設兩次 probe 必須實際走產品的 `get_slots`，只 mock 底層失敗的 session；保留「quiet 無輸出、預設仍印原因」兩側斷言，不刪除、放寬或 skip。後續執行依測試權責留下實際結果，不能把這份靜態推演當紅燈輸出。

## R1-B02 — Blocker：取得 stream 前無法中止舊預熱，切換後仍占模型鎖

- **位置：**`client_engine.py:1003`–`:1005`、`:1035`–`:1057`、`:1074`–`:1095`；既有呼叫端新增的 `new_session():746`、`adopt():827`；底層同步 POST 為 `llama_client.py:347`。現有新增測試 `tests/test_client_engine.py:2347`–`:2368` 只涵蓋已取得 stream 且正在讀 chunk 的情況。
- **觸發與失效路徑：**預熱持有共用模型鎖後，若 `/slots` 尚未返回，或 POST 尚未取得 response headers，`_prime_stream` 仍為 `None`。`abort_prime()` 只能設旗標並等一秒，沒有可關的 handle；逾時回 `False`，但兩個 session 切換呼叫端均忽略返回值並完成換歷史。舊 POST 仍可依 `options.request_timeout` 等待，舊預熱尚未結束、`priming` 仍為 True、鎖尚未釋放；新 session 的預熱回 `model_busy`，真實問題也等這把鎖。若中止落在 `/slots` 中，probe 返回後至 POST 前完全不檢查 abort，還會在切換後**新發出舊歷史的 POST**，直到取得 stream 才看見旗標。程式第 1082–1083 行「等不到就照常繼續，只有一個 token 會自己結束」無法限制 prefill／等 headers 的時間。
- **違反：**定稿 D6、§5 B7 明訂 session 切換後一秒內舊預熱為 `aborted`、模型鎖已放、`priming=False`；也使「中止上一段無用請求」的既有呼叫端新契約失效。
- **最低可驗證修復條件：**中止邊界須涵蓋 probe、取得 headers／stream 前及串流中，已中止的工作不得再發 POST；`new_session` 與 `adopt` 都須滿足 B7 的時間、outcome、鎖與旗標條件。以受控 barrier 的離線替身覆蓋尚無 stream 的兩條路徑及中止／准入交錯，不能只沿用目前已在讀 chunk 的測試。不得靠提早放掉仍有舊 HTTP 在跑的共用模型鎖，或借用正常回合取消旗標來達成表面通過；AGENTS 的模型序列化與零回合狀態寫入仍須保留。

## R1-B03 — Blocker：部分工具結果已保存時，預熱與真正 send 的 prefix 順序不同

- **位置：**新增 `client_engine.py:904`–`:919`；影響的既有呼叫鏈為 `heal_in_place():238`–`:274`、`send():1116`–`:1118`、`heal_pending_tool_calls():1309`–`:1322`、`payload_messages():854`–`:864`。新增等價測試的歷史 `tests/test_client_engine.py:2081`–`:2107` 只有尾端一個未完成 call。
- **觸發與失效路徑：**接續一份在同組兩個工具呼叫之間中斷的合法歷史：assistant 宣告 `a,b`，`tool(a)` 已落檔、`b` 沒結果。預熱的 `heal_in_place` 將缺少的 `tool(b, 已中斷)` 插在 assistant 後、既有 `tool(a)` 前；真實 `send(q)` 則先經 `heal_pending_tool_calls()` 把 `tool(b)` append 到已有 `tool(a)` 後，再記 user q。後續 `payload_messages()` 看見 a、b 都有結果，不會重排。

  ```text
  prime:   … assistant(calls a,b) → tool(b, 已中斷) → tool(a)
  send(q): … assistant(calls a,b) → tool(a) → tool(b, 已中斷) → user(q)
  ```

  因此 `send(q).messages[:-1] != prime.messages`，cache 可重用前綴在工具群組中途分岔。這不是要求擴修所有歷史 healing 問題，而是新增預熱介面對既有 `send()` 作出的等價保證已不成立。
- **違反：**定稿 D4、§5 B1 與 AGENTS §2 新增的 `next_turn_prefix()` 同下一輪轉換契約。
- **最低可驗證修復條件：**上述部分完成的多工具群組，預熱 payload 必須逐字等於後續**真實** `send(q)` 擷取的 payload 減最後一則 user；保留原始歷史／store／transcript 不被 prime 改寫，也保留工具結果緊接其宣告群組的既有安全要求。使用這種合成歷史加進離線契約／regression，不能以同一 helper 自我比對取代真實 send。

## R1-B04 — Blocker：壓縮後預熱結果遺失，/status 保留上一筆 sent

- **位置：**`client_turns.py:347`、`:375`、`:423`–`:426`；`client_app.py:1267`–`:1289`；文件已在 `docs/troubleshooting.md:229` 揭露此落差。
- **觸發與失效路徑：**mount 的預熱成功，`_last_prime` 記為 sent；之後自動或手動壓縮成功換掉歷史，協調器排新預熱，若這次回 `server_busy`／`model_busy`／HTTP error，因未提供 `on_done`，outcome 在第 423–424 行直接被丟掉。此時沒有本次 `source=prime` 列，而 `/status` 仍顯示上一次的 sent 與舊時間，使用者無法按驗收表判斷這次是 skipped，或取得原因。成功的壓縮後預熱也不更新「上次」。
- **違反：**定稿 D9 的「上次預熱來自協調器回呼」，以及 §5 C:188 的 skipped 判準；壓縮後預熱明列於 §5 B6，未被此驗收排除。**Lane C 確實照 §4.3:144 的字面呼叫形狀實作，但該形狀與上述驗收要求有落差；Lane D 記錄現象不能取代驗收。** 此處交 Fable 裁決並修正，不由 Astra 另擬介面。
- **最低可驗證修復條件：**自動與手動壓縮後，實際執行的預熱 outcome 都能回到 TUI，讓 `/status` 呈現最近一次結果、原因與時間；維持在回合鎖外排程，不產生對話事件或 busy／cancel 狀態副作用。以可區分的先後 outcomes 核對兩種壓縮入口，並同步更新第 229 行文件，不能僅加上「不會更新」的免責文字。

## R1-B05 — Blocker：沒有終結 chunk 的 clean EOF 被記成成功預熱

- **位置：**`client_engine.py:1059`–`:1072`；相關既有 transport／解析行為為 `llama_client.py:736`–`:763`、`context_budget.py:428`–`:445`。
- **觸發與失效路徑：**HTTP 200 串流只有 keep-alive、或送出非終結 delta 後正常關閉連線，沒有 finish_reason／timings。`OpenAIStream` 對這種 EOF 不會自行拋例外；預熱迴圈只嘗試解析 usage，從未確認有終結 chunk。只要不是 `abort_prime`，便無條件 `log_metrics` 並回 `PrimeOutcome(True, "", None)`。因此不完整回應會產生 `source=prime` 成功列與 `/status sent`，但 `prompt_tokens_processed` 為空。即使有 finish_reason，若缺少 timings，也同樣被當成符合驗收的成功列。
- **違反：**定稿 §4.2:127「正常結束」才記成功、§5 B5 要求 `prompt_tokens_processed` 有值，以及 §5 C:189 的 sent／冷熱判讀前提。一般模型步驟已有獨立的 finish 判斷；不能假定底層迭代器已做了這項檢查。
- **最低可驗證修復條件：**只有能確認正常終結且取得規定的 `timings.prompt_n` 的預熱才走目前成功記錄路徑；缺終結或缺必要 metrics 必須有可判讀的非成功 outcome，不生成冒充成功的 prime 列。以空串流、非終結 delta 後 EOF、終結但無 timings 三種離線回應核對，並保留正常 `finish_reason=length`、`max_tokens=1` 的成功情境。不得用 `usage.prompt_tokens` 或估算值代填 processed 欄位；維持不 raise／不 print／不寫 session 的邊界。

## 其餘指定事項的範圍核對

- **成功清 LOG／warning／status：**靜態核對 `banner_lines()` 組成、五個 keep 點、stderr 收集、全部壓縮狀態行與失敗 exit 路徑，未另列 Blocker。Lane A 的紅／綠輸出由其報告提供；實際執行次數為一紅兩綠，整合者已更正記錄。本輪沒有重跑或將靜態核對稱為測試通過。
- **Lane B 其餘偏離：**placeholder 的私有 marker 隨 dict copy 保留、在 `to_wire()` 前連同假 user 移除，沒有因改用 marker 而新增 Blocker；R1-B03 是另一個真實等價問題。保留不可達的防禦性 gate reason、reason 不另落 telemetry、stream 關閉 ownership 本身不構成額外 Blocker；取消生命週期的失效統一列 R1-B02。
- **Lane C／D：**callback 收尾捕捉 `BaseException`、相位 part 位置、wall-clock 時間戳、替身增加背景工作、prefix 表格按真實來源列六段，以及沒有擴改 `docs/basic-usage.md` 的概述，沒有可由本次靜態證據成立的額外 Blocker。compaction 不更新 status 已列 R1-B04，不能以「lane 已揭露」免除驗收。
- **共用狀態、權限與 I/O：**准入先拒絕非 interactive、headless 無呼叫點、使用共用模型鎖、prime 不進 `_begin_turn`／`_record`／正常取消欄位、coordinator 不把 prime 當 busy、gate `emit=False` 與 quiet probe，未發現本報告以外的新失效路徑。這不抵銷 R1-B01 對 quiet 測試有效性的缺口。
- **Dependency／owner／測試 gate：**四 lane 產品檔無重疊，C 按既定介面銜接，shared-file owner 由整合者登記 16 個新 node；已按函式名與 module／逐條 smoke 標記核對。沒有跨 lane 檔案衝突或漏登 node 的靜態證據。
- **多 slot 收益：**實作只在全部 slot 忙時跳過，符合 D7；4-slot cache 是否真的重用尚無本輪量測。計畫明確容許 T0 得到 reuse-failed 並關閉常數，因此不能把「沒有加速數字」另當施工 Blocker，也不能宣稱需求 b 已獲實機收益。
- **既有、無關本次：**runner 純 `-m smoke` 走分片與 AGENTS 入口文字的差異在 base 已存在，本輪不擴修。整合報告的補跑 smoke／先跑 full 建議不是本輪授權；本輪遵守禁止任何測試、靜態收斂後再由 reviewer 執行 full 的流程。

## 交接界線

交 Fable MAX 集中回修 R1-B01～R1-B05；Astra 不改碼或重規劃。回修若修 bug，依 AGENTS §1.3 先在未修產品上建立新 smoke regression 的實際紅燈證據，再修產品並記同條綠燈與 diff；既有測試的改動依 §1.5 逐條交代。不得把本報告的靜態失效推演冒充已執行的紅燈。

修正後須重新凍結產品內容，再讓靜態審核及後續測試對同一份 snapshot；不得拿本輪 digest 的審核套到另一份產品。**full 尚未執行；目前 smoke 紅、5 個 Blocker 未收斂，不能宣告完成。**
