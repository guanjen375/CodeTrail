# startup-ttft-20260907 — Deferred 與尚未裁定事項

> **發布授權（2026-09-07）**：使用者已批准產品 commit 並直接 push `main`、不開 PR，詳見 `00-intake.md` 最新段。本段所在提交收錄下述已驗收的 23 個產品／測試路徑；產品 digest 維持 `61fda568fce324d31691892f51a01aa7f17831e4e2c3815d2c0e16868dac9b4a`。沒有新增正式擱置，§A 原案十項維持，T0 仍未量測；root 已於 push 前向使用者揭露。以下未 commit／push 的描述保留為驗收當時的歷史狀態。

> **最新交付狀態：程式／離線驗收完成。** Astra MAX 修復、Fable 5.1 MAX 審核；`05-review-fable-r3.md` 的靜態Blocker為0。使用者批准的精確447條前景補測全部通過（exit0），與14個完整shard的3121條合併為**3568條全通過，0失敗／錯誤／跳過、0重複／缺漏**；原full的killed記錄不回寫為成功。完整證據見`08-test-results.md`，root已獨立核對node聯集。
>
> 審核、兩段測試、最後產品均為同一digest：`61fda568fce324d31691892f51a01aa7f17831e4e2c3815d2c0e16868dac9b4a`。本次無新增正式擱置；§A原案十項維持。原B-4的本版測試證據缺口已補齊，原B-6涉及的46條app測試也全通過，結論限於那些斷言的覆蓋範圍；B-5文件概述未動，非Blocker。**T0實機TTFT仍未量測、仍由David依原案執行，不能宣稱真實首字改善已實證。** 產品23個路徑保持staged，未commit／push。
>
> 以下逐次狀態保留各自時點的歷史紀錄；其中等待授權、待審與補測未完成的舊句，現況以上述交付與08-test-results為準。

> **使用者已批准補測**：最新回覆「好」，授權只補未完成447條、與既有3121條合併驗收。Fable 5.1 MAX 依 `08-test-recovery.md` 前景執行；改用精確node清單，避免R3報告整檔命令重複已通過node。此時補測尚未完成，靜態Blocker維持0；T0未量測、產品未commit／push，其他歷史狀態保留為原時點紀錄。

> **Fable R3 正式書面交付完成**：`05-review-fable-r3.md` 已補齊，R2-B01／R2-B02 關閉、R1 四項維持關閉，靜態 Blocker 0；本次補交接零測試、產品 digest 未變。驗收仍未通過：3121 條有完整通過證據，447 條所在的兩個 shard 缺完整結果，full 無完整 exit，T0 未量測。沒有新增正式擱置。補跑安排見報告 §8.6；使用者尚未答覆 root 的補跑授權問題，未啟動補測。

> **Fable R3 更新（root 執行紀錄）**：Fable 5.1 MAX 可見結論為靜態 Blocker 歸零，但它在啟動背景 full 後提前結束 CLI，該 task 被標記 `[killed]`。保存的 14 份完整 shard JUnit 合計 3121 passed，另兩個 shard 共 447 條缺完整結果；**full 未通過、任務未完成**。产品 digest 仍為 `61fda568fce324d31691892f51a01aa7f17831e4e2c3815d2c0e16868dac9b4a`。詳見 `05-review-fable-r3-execution.md`，書面審核待補齊；測試中斷不列正式擱置，T0 仍未量測。

> **Astra developer R2 回修交付狀態（2026-09-07）**：R2-B01 / R2-B02 已實作修復，四條新 regression 各一次行為紅燈、一次綠燈（每次 collected 1，紅 exit 1 / 綠 exit 0）。pending headers 改為先 shutdown 專用連線再放模型鎖，未改 B7；登記／快回應／晚到 worker／session create 空窗皆有保護。兩項目前是**修復完成待 Fable 5.1 MAX 審核**，不是正式擱置，也不由 writer 自行宣告關閉。最終凍結資料與完整證據見 `06-fix-r2.md`。
>
> 本輪未再跑 smoke、未跑 full；新 transport 三個安全契約與 create 失敗重新准入契約只寫未跑。Fable 靜態 Blocker 歸零後才對同一 freeze 跑 full。原 T0 仍未量測，不宣稱 TTFT 實際收益。下文 R1 清單保留歷史事實：其中「等 headers 才放鎖」已被本次 transport 修復替換，「待 Astra 裁定」已在 R2 審核裁定並進入本次回修；沒有新增正式 deferred。

> **使用者最新授權**：已改派 Astra MAX 修復、Fable 5.1 MAX 審核，替換下述「等待另一 Claude 型號授權」狀態。R2-B01／R2-B02 仍 active，接續回修中；其他原案擱置與驗收狀態不變。角色變更見 `00-intake.md`，修復交接將寫入 `06-fix-r2.md`。

> **R2 執行狀態更新（root，僅流程紀錄）**：Astra 已在 `05-review-astra-r2.md` 裁定兩項 active Blocker（R2-B01／R2-B02）。Fable 5.1 MAX 兩次回修嘗試均被模型自動安全審查以 `cyber` 拒絕，沒有產品／測試修改、沒有新測試；詳見 `06-fix-r2-execution.md`。產品 digest 仍為 `16bda189fe8d6d99edfd1ebc86b59a373ac2a1816389d98d5c59ab41731f9349`。這兩項**未擱置**、也不因模型拒絕而滿足技術分歧輪數；下一步需使用者決定是否授權另一個 Claude 型號回修。下文保留 R1 原始清單，其中「待 Astra 裁定」的後續裁定以 R2 審核報告為準。full 尚未執行，任務未完成。

- 撰寫者:Claude Fable 5.1(harness 自報 model ID `claude-fable-5-1`),Step 5 R1 整合者。日期 2026-09-07。
- 對應的產品內容:base HEAD `f200f697ba54d38a102e8ef66dead652c4002e5f` + product digest `16bda189fe8d6d99edfd1ebc86b59a373ac2a1816389d98d5c59ab41731f9349`(`06-fix-r1.md` §7);產品未 commit。
- **分類規則(照使用者的規則,不擴張)**:`03-plan-final.md` §7 寫「分歧超過兩輪才可擱置,擱置事項寫進本檔並在 push 前告知使用者」。目前是 **Step 5 的第一次回修**,任何本輪新出現的分歧都**沒有**達到擱置門檻。所以本檔分三節:§A 是原案(`03-plan-final.md` §10)本來就揭露不做的範圍,原樣帶入;§B 是本輪**待 Astra 裁定**的 plan 落差與揭露,**一項都不是正式擱置**;§C 是本輪已關閉、不再列的項目。**不宣稱需求 b 已實測改善。**

## A. 原案 deferred(`03-plan-final.md` §10,原樣帶入、狀態不變)

| # | 事項 | 為什麼不在本次 |
|---|---|---|
| 1 | prefix 瘦身(工具 schema / AGENTS.md 注入預算) | 最大槓桿,但動路由真值與 eval catalog digest |
| 2 | `id_slot` 釘 slot | 4 slots + LCP 之下暫無必要;T0 若證明聊天 slot 常被逐出再議(部署決策) |
| 3 | KB 端 `USE_QUERY_EXPANSION` / multi-query 預設 | retrieval 品質決策 |
| 4 | 壓縮摘要請求帶同一份 system prompt / 換 slot | 摘要契約另案;本次以「壓縮後預熱」補回 |
| 5 | `gpu_safety.runtime_offload_check` 只認 `state`,新 build 的 `is_processing` 會被算成 idle | doctor 維護項 |
| 6 | `keep_historical_reasoning=True` 換跨回合 cache 命中 | 使用者 `client.json` 選擇,不改預設 |
| 7 | 部署調參(`ubatch` / `n_cpu_moe` 對 prefill 吞吐) | 不可動 live 部署,David 決定 |
| 8 | reuse-failed 分支(§5 C):若 T0 證明此 build 不重用,`CLIENT_PRIME_PROMPT_CACHE` 改 False 並記錄;自動偵測「預熱後下一請求仍全量重算」並自動停用 | 列為後續 |
| 9 | 使用者送出時是否中止進行中的預熱 | 取決於 llama-server 中止時保不保留已處理的 KV(本 session 無法查證);現行行為是回合在鎖上等預熱完成(可 Ctrl-C) |
| 10 | 預熱 payload 被 server 拒絕時的佔位 user 形狀(§8 末段) | 施工階段不得擅自改 payload 形狀;T0 若看到 `error:HTTPError` 再議 |

### A'. T0 未量測(不是 deferred,是「還沒做的驗收」)

- `03-plan-final.md` §8 的 T0(`/props` 靜態事實、`context_metrics.jsonl` 的 `prompt_tokens_processed`、prefix digest 穩定性)由 David 在真實使用 `aicode` 的專案目錄執行;規劃、施工、審核、回修、整合**都不執行**,到本輪為止**一個數字都沒有**。
- 因此需求 b(開新對話首字慢)的機制是否成立,只能由 T0 依 §5 C 四類(skipped / sent / reuse-verified / reuse-failed)判讀;`reuse-failed` 是計畫預期內的分支(→ §A 第 8 項),不是失敗的施工。
- 固定句:reasoning 與硬體 prefill 成本未變;預熱只把下一輪 prefix 的 prefill 搬到打字之前;熱 prefix 時無感;是否真的被重用只由 T0 的 `prompt_tokens_processed` 判定。

## B. 本輪待 Astra 裁定的 plan 落差與揭露(**尚未裁定,不是擱置**)

| # | 事項 | 來源 | 現況 | 若 Astra 判定為 Blocker / 偏離 |
|---|---|---|---|---|
| B-1 | **R1-B02 的子情況:POST 已送出、headers 未到時的模型鎖**。B7 寫「`new_session()` / `adopt()` 之後 1 秒內舊預熱結束(aborted)、鎖已放、`priming` False」。回修後 probe 中 / probe 後 / 串流中三種情況三個條件都在 1 秒內成立(離線 barrier 測試釘住);**POST 在飛**這一種,outcome 與 `priming` 在 1 秒內成立,但鎖 `hand_off` 給該請求、response 到了關掉才 `release_late()`——「鎖已放」的時間 = 舊請求 headers 到達的時間(llama-server 對串流請求在 handler 回來時就寫 headers,實務上毫秒級;server 卡住時等到 `options.request_timeout`,與既有回合取消相同)。lead 的立場:提早放鎖正是 Astra R1-B02 明訂不得做的事(AGENTS 的模型序列化),兩者不可能同時成立,這是 B7 在該子情況下唯一合規的讀法 | `06-fix-r1-engine.md` §1 B02、`06-fix-r1.md` §1 | **待 Astra 裁定**;整合者未改 plan 驗收、未把 B02 當已關、未列擱置 | 進 R2 回修(由 Fable 決定修法,例如改 B7 的驗收字句或改鎖的處理);仍不是擱置,除非之後分歧超過兩輪 |
| B-2 | **`/status` 的「最近一次」= 最後落地的那一次,不是最後排程的那一次**。`_note_prime` 只以 `time.time()` 記錄、不排序、三元組不帶序號(交接 §5.2 的既定形狀)。換 session 時舊預熱通常先被 `abort_prime()` 收掉(B02 修好後上限 1 秒),它的 `aborted` 先落地,所以最後顯示新那一次;若中止逾時、舊預熱晚於新預熱結束,`/status` 會短暫顯示舊的 `skipped(aborted)` | `06-fix-r1-status.md` §9-2 | **待 Astra 判定**是否可接受 | R2 回修(例如帶世代號或排程序號) |
| B-3 | **幾乎不可達的交錯**:一個排程晚到的預熱在 `_switch_session()` **之後**才取歷史,會拿新對話的 prefix 送出去,而 TUI 接著排的那一次回 `model_busy`——`/status` 顯示 `skipped(model_busy)`,實際上新 prefix 已被那個晚到的預熱送過(結果是正確的、只是顯示落差)。需要「換 session 發生在 mount 預熱執行緒還沒排到 CPU 的微秒內」 | `06-fix-r1-engine.md` §6 | lead 本輪不處理;**待 Astra 判定**是否構成 Blocker | R2 回修 |
| B-4 | **前一版 smoke 的失敗 node 未取得**(2405 selected、1 failed、shard 9,runner temp 目錄已刪、輸出被截斷)。那次是對 digest `6e92b5cf…` 跑的;本版 digest `16bda189…` **沒有任何 smoke / full 證據**。它可能就是 R1-B01 靜態指出的必敗路徑,但沒有執行證據,不得宣稱 | `04-integration.md` §5、`05-review-astra-r1.md` | 由 Astra 對本版凍結內容執行 full 直接得到失敗 node 集合;整合者依指示未補跑基線 | 依 AGENTS §1.2 判定(基線外新失敗 = 未完成) |
| B-5 | `docs/basic-usage.md:39` 對 `/status` 的括號概述沒有列「預熱」那一行。不在 Lane D 與本輪整合者的可寫清單 | `04-integration.md` §9-4 第 3 點 | 未動 | 指定 owner 補一句;非安全項 |
| B-6 | 既有 app 測試的**行為**副作用:替身有 `prime_prompt_cache` 之後每個 app 測試在 `on_mount` 多 spawn 一條預熱執行緒;本輪起還多一次 `on_prime` → `call_from_thread`。兩個 lane 判斷斷言與畫面不受影響 | `04-integration.md` §7 ⚠、`06-fix-r1.md` §4 | 待 Astra 在 full 時確認 | 若 full 出現與此相關的失敗,R2 回修 |

**再說一次分類**:§B 六項沒有任何一項符合「分歧超過兩輪」;目前只是第一次回修,它們是**待審**,不是擱置。若 Astra 裁定 B-1 為 B7 的偏離,處理方式是 R2 回修,不是把它搬到 §A。

## C. 本輪已關閉、不再列的項目

| 事項 | 關閉依據 |
|---|---|
| `04-impl-c.md` §9 第 2 點 / `04-integration.md` §9-3 第 1 點:「壓縮後那一次預熱不會更新 `/status`」 | R1-B04 已修(`on_prime` 回呼;regression 一紅一綠;`docs/troubleshooting.md` 第 229 行的免責句已改成「反映每一次預熱」) |
| `04-integration.md` §9-2 第 4 點「誰關那個串流」 | 本輪 `abort_prime()` 重寫後仍是「`abort_prime()` 取走登記並關、預熱的 `finally` 只關自己還握著的那一個」;POST 在飛時由 `settle()` 關,`closed_from` 長度 1 有測試釘住 |

## D. push 前要告知使用者的事(依 §7)

- 本檔 §A 之外**沒有**新的正式擱置。
- §B-1 / B-2 / B-3 是本輪的待審項目,由 Astra 下一輪裁定;若任一項被判為 Blocker,回到 Fable 回修,不進入擱置。
- T0 尚未執行;需求 b 的收益沒有任何數字。
