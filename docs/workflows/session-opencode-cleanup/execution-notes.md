# 執行偏差紀錄

編排者核對 Claude 原始串流後記錄，不能把未使用 pytest 稱為符合測試政策。

W1 worker S (`04-opus-S`，明傳 Opus5 MAX，回傳 `claude-opus-5`) 在兩條新 regression 的紅綠之外，額外執行了 4 個 Python heredoc，以合成資料呼叫 repo 自己的函式／widget：

| 次序 | 執行內容 | 判定 |
|---|---|---|
| 1 | 建立 AssistantBlock、ToolBlock、SummaryBlock、ReasoningBlock、ErrorLine、UserMessage，呼叫 finish/set_output/append 並印結果 | 額外行為探查，超出本輪允許範圍 |
| 2 | 建立 SummaryBlock，並以重複 call_1、orphan、compaction 等合成資料呼叫 history_entries | 同上 |
| 3 | 呼叫 session_outline、session_row，另讀 dataclass 欄位與 COMMANDS | 前半是額外行為探查；metadata 讀取不抵銷它 |
| 4 | 以 3 種合成 history 呼叫 _compaction_summary | 額外行為探查，超出本輪允許範圍 |

第 1 次 exit 1：`ToolBlock.set_output()` 在沒有 Textual App context 下拋出 `NoActiveAppError`；這是探查方式的失敗，不是可替代 §1.3 的 regression 證據。其餘 3 次 exit 0，也不計為政策允許的驗證。

完整命令保存在本機 `/tmp/codetrail-session-opencode-cleanup-20260906/04-opus-S.runtime-probes.json` 與原始 `04-opus-S.stream.jsonl`。這 4 次不計入允許的 regression/smoke 驗證，也不聲稱測試政策全程無偏差。先前進度訊息按函式掃描回報 3 次；完整 import/命令核對加入 widget 探查後，確定為 4 次。

已向使用者揭露，並在尚未啟動的 C2/C3/D1 指令中明定：import repo runtime 後用合成資料呼叫並印結果也是測試，不得以「未使用 pytest」迴避。後續 Fable 修復同樣遵守。B 與 C1 在此次完整串流核對中沒有這種 repo runtime import heredoc；D2 截至核對時也沒有。沒有執行 full 或 smoke；兩條正式 regression 的各一次紅、各一次綠另見 handoff-S-red.md 與 handoff-S.md。
