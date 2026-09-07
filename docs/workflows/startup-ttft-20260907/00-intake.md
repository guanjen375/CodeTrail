# 啟動畫面與首字延遲：需求及唯讀查核

- 日期：2026-09-07；工作目錄 `/home/david/CodeTrail`。
- 動工 base HEAD：`f200f69`；初始 `git status --short` 無輸出。
- 使用者已確認：自檢成功後進入聊天畫面清掉自檢 LOG；自檢失敗時保留錯誤。
- 流程：Fable 5.1 MAX 初稿 → Astra MAX 審計畫 → Fable 5.1 MAX 定稿 → Opus 5.0 MAX 依 dependency 平行實作 → Astra MAX 審實作／Fable 5.1 MAX 修 Blocker。
- Astra 僅唯讀查核及撰寫審核交接；產品、測試修改由 Claude 側負責。
- 使用者後續指示：方案判斷與規劃直接交給 Fable 5.1 MAX；主代理負責流程交接，不替 Fable 決定方案。下列現況僅為唯讀觀測，最終計畫由 Fable 編寫。
- 使用者指定審核使用 `ROLE=REVIEWER`；先靜態收斂，再由指定 reviewer 執行一次 full。開發者遵守新 regression 單 node red/green 與交付一次 smoke 限制。
- 使用者明確要求 commit plan、review、deferred 交接文件。產品 commit/push 尚無確認；產品 diff 保留待確認。若 HEAD 未含產品變更，驗證記錄須明示 base HEAD 加產品內容 digest，不得冒稱已測 HEAD 內的修正。

## 已查核的現況

1. `command_chat()` 把 `checks.lines` 合併進 `banner`，`CodeTrailApp.on_mount()` 將 banner 各列加入聊天區。這能直接解釋自檢完成後 LOG 仍留在 TUI。
2. `llama_client.chat_completions()` 已設 `cache_prompt=True`。單純新增相同設定不會構成修正。
3. `_iter_sse_lines()` 使用 `resp.iter_lines()`，本機 Requests 2.32.5 的實作以預設 chunk size 呼叫 `iter_content()`。短片段的等待須用真實 Requests 解碼器及受控 byte stream regression 證明，不宜只斷言 mock 收到某個參數。
4. 唯讀 GET `/health` 回 200/ok；`/props` 回報 build `b10276-6ea215d17`、4 slots、每 slot n_ctx 131072。觀測 `/slots` 時有一個 slot 正在處理約 89,848 prompt tokens，其他 slot 閒置。這是單一時間點，不能据此斷言使用者每次延遲都由排程或負載造成。
5. 本機 llama.cpp 來源的 server 說明及實作支持 common-prefix cache，亦可能因 SWA/hybrid cache/checkpoint 不足而重算。伺服器常駐不等於新 prompt 已完成 prefill。
6. 尚未對 live server 送生成請求、重啟服務或更改部署。未讀取私人 session 原文。硬體端 TTFT 改善尚無量測數據。

## 模型身分記錄

- 本機 Claude Code 版本：2.1.263。
- 本機 Claude 設定原預設：`claude-fable-5-1[1m]`、effort `xhigh`；本次以 CLI 明確覆寫 MAX。
- Step 1 argv：`claude -p --model claude-fable-5-1 --effort max --output-format stream-json --verbose --no-session-persistence ...`。
- Step 1 CLI init 回傳 `model=claude-fable-5-1`、`permissionMode=acceptEdits`；最終 modelUsage 待該次執行結束記錄。
- 原始 Claude 執行 log 放在 owner-only `/tmp/codetrail-startup-ttft-20260907/`；不將完整來源／工具輸出 log commit。repo 僅記錄核對過的身分與驗證摘要。

## 補充：修復平行的決策來源

使用者詢問「所以修復平行是你做的不是我指定的，我之前都沒想過修復可平行」。root 已明確說明：將修復階段也平行化，是 root 把使用者對實作的 Dependency 平行規則延伸到修復階段的安排，並非使用者另外明示的要求。root 把修復拆分交給 Fable 5.1 MAX，由 Fable 決定實際 Dependency、介面與檔案 owner，再依其交接啟動 lane；修碼仍全部由 Claude 側執行。這個提問沒有取消正在進行的修復工作。

## 最新角色調整（取代前述相關分工）

使用者最新明示：「你改成 astra max 修復 fable5.1 max 審核」。自此由 Astra MAX 修復、Fable 5.1 MAX 審核，取代原先「Astra 只審不改／改碼一律 Claude」的分工限制。既有 Fable 定稿與驗收仍沿用；原始計畫和歷史審核不回寫。Astra 在修復時按 developer 測試權責執行，Fable 接任 `ROLE=REVIEWER`，先靜態審核收斂，再對同一份凍結產品執行獲准的 full。其餘 Blocker 定義、紅／綠證據、檔案交接、擱置規則與產品未獲准 commit／push 的限制均維持。

## 使用者批准剩餘 447 條補測並合併驗收

root 最後詢問「是否依 Fable 最終建議，只補測這 447 條，並合併既有結果驗收？」並明示這會將原本一次 full 改為兩段結果。使用者最新回覆「好」，已批准此安排；不再等待前一個補跑問題。授權範圍為同一產品 digest 上的剩餘 447 個 node、以前景執行，與既有 3121 個完整通過 node 合併，並非重跑整包或批准產品 commit／push。

root 讀保存的 node 清單發現 R3 報告 §8.6 所列八個整檔也含已完成 shard 的 node（`test_code_rag_search.py`、`test_mcp_ingest.py`、`test_repo_consistency.py` 分跨 shard）；直接跑整檔超出 447 條範圍。交 Fable 改用保存的精確 447 個 node 清單，這不改補測範圍、產品或原驗收。執行交接見 `08-test-recovery.md`。
