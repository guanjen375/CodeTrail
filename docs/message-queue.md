# 訊息排隊與途中補充

[回到 README](../README.md)。

`aicode` 閒置時照常送出問題。回合忙碌時按 Enter，會先讓你選擇：

- **排到下一輪**：目前回合成功收尾後，依加入順序逐則開始新回合。
- **補充目前任務**：等整批工具結果都寫好，在下一次模型請求前加入原文。正在進行的 HTTP、工具寫入和核准保持原有流程。

剛啟動時尚未建立 session。`/queue add` 與 `/queue resume` 會提示先用 `/new` 或直接
輸入第一則問題，不會為了排隊先建立空白對話。接續 `/session` 選定的歷史後也可使用佇列。

Esc 只關閉這個選擇框，原文保留在輸入框。Ctrl-C 仍取消目前整輪；核准框的拒絕仍只拒絕該工具。加入佇列不等於工具核准，唯讀政策仍然生效。

等待工具核准時，可按核准框的「加入訊息」，輸入後選排隊或補充；該工具仍保持待核准。編輯訊息時輸入 y/n 不會回答核准。若核准框因逾時或取消收起，尚未送出的草稿會回到輸入框或輸入歷史。

| 指令 | 作用 |
| --- | --- |
| `/queue` 或 `/queue list` | 查看本對話的待送項目與近期收據，不送模型 |
| `/queue add <文字>` | 明確排到下一輪 |
| `/supplement <文字>` | 明確補充目前任務 |
| `/queue edit <id> <文字>` | 修改尚在 waiting/deferred 的完整原文 |
| `/queue cancel <id>` | 取消尚未接收的項目 |
| `/queue resume` | 本輪中斷或失敗後，明示繼續待送項目 |

`waiting` 表示仍在等待，`delivering` 表示引擎正在接收、已不能修改。`delivered` 只表示原文已進入真實 user 歷史，畫面才會出現 UserMessage；它不代表模型已回答或工具已成功。正常 session 會保存這些 user 訊息，落檔失敗沿用既有明確警告。

本輪已輸出最後答案、正在壓縮、被取消、出錯或用盡模型步數時，尚未加入歷史的補充會標成 `deferred`，保留到下一輪，不會冒稱已送本輪。成功收尾才自動繼續；取消或錯誤會暫停整個待送佇列，必須 `/queue resume`。檢視、修改、取消都不會自動續跑。閒置時直接加入佇列也會等待明示 resume。

每項都綁定加入時的 session 與 turn。選擇框停留到原回合結束後才選「補充」，會改為待下一輪，不會插入另一個正在執行的任務。尚有未送項目時，切換/新建對話、手動壓縮與正常退出會提示先處理佇列；可送完或逐項取消。`/session <id>` 接續指定對話；`/queue resume` 繼續目前對話的待送項目。

佇列只存在目前 TUI 的記憶體內，最多 32 項、單項 64 KiB、合計 256 KiB UTF-8 原文。近期已送/已取消收據會先被淘汰，待送內容不會為了騰出空間被丟棄。程序被強制終止時不能恢復尚未送出的排隊狀態；正常離開會先擋下並列出待處理提示。已送訊息仍按既有 session 保存政策保存。

程式介面在 `TurnCoordinator`：`enqueue(text, mode=..., session_id=..., turn_id=...)`、`queue_snapshot()`、`edit_queued()`、`cancel_queued()`、`resume_queue()`、`assert_session_change_allowed()`。snapshot 是不可修改的 `QueuedMessage`。supplement 由引擎 loop 的安全邊界回呼處理，`record_supplement()` 只接受該 worker 在該邊界呼叫，與取消共用原子准入。它不增加既有步數上限，也不跳過 context gate。
