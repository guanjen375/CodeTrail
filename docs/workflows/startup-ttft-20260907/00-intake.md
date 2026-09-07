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
