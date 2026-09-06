# 執行偏差紀錄

本檔記錄截至 W1 交接時的執行。編排者核對 Claude 原始串流後記錄，不能把未使用 pytest 稱為符合測試政策。

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

第 5 階段 Fable 修復第 1 輪另有 **1 次已執行的純 Python 語意探查**。`05-fable-fix-01` 的兩個靜態 heredoc 末尾都附帶自行定義 `f` / `g`、建立 class 並實際呼叫的 B-02 名稱遮蔽對照。第一個 heredoc 在較前方的 manifest AST 擷取 assertion 就停止，沒有執行到這段；第二個執行到該段，印出舊形狀的 `NameError` 與新形狀 `OK`。它沒有 import repo runtime，但仍超出本輪明定「只准原始碼 / AST / 靜態比對與指定新 regression」的限制；不計為正式紅綠、smoke 或 fixture 驗收證據。命令完整保存在本機 `05-fable-fix-01.language-probes.json` 與原始串流。編排者已向使用者揭露，後續指令會明禁 toy function / class 的執行探查，不能以「沒有 import repo」作為許可。

目前已知額外探查共 **5 次：S 的 4 次 repo runtime 探查 + Fable 的 1 次純 Python 語意探查**。Fable 兩條正式新 regression 的各一次紅、各一次綠另有完整記錄，兩條測試從紅到綠的原文 SHA-256 未變；上述額外探查不抵銷也不擴張正式驗證範圍。

`fix-01.md` §4 標題稱「全部是允許的命令」不成立：其表內的純 Python class 語意探查屬上述額外執行。保留作者原交接文字供稽核，以本檔的編排者核對為準，不把那一列當成正式驗收證據。

修復第 2 輪另有**範圍外記憶寫入**：Claude 在 `05-fable-fix-02` 透過兩個 Edit 更新本機 `.claude/projects/-home-david-CodeTrail/memory/` 的 `MEMORY.md` 與 `codetrail-test-role-split.md`，加入本輪測試限制說明。這不在本輪只准 troubleshooting / fix-02.md 的修改範圍。編排者已向使用者揭露，依原始串流的 old_string / new_string 精確反向替換兩次（替換前要求完整新片段恰好出現一次，拒絕 symlink／非 owner／多連結檔案），保留其他既有記憶。還原證據只含路徑與前後內容雜湊，保存在本機 `claude-memory-restoration.json`。這不是另一次測試／runtime 探查，與前述 5 次額外探查分開記錄；不宣稱 file-only 模式阻止了範圍外寫入。`fix-02b` 未發現額外記憶寫入。

後續修復 CLI 除 file-only 工具限定外，另以呼叫當次 `--settings` JSON 設 `autoMemoryEnabled: false`，不修改使用者永久設定；開關語意已核對 [Claude 官方記憶文件](https://code.claude.com/docs/en/memory)。這是針對已觀察記憶寫入的預防，不能抹除先前偏差。

修復第 3 輪完整串流已核對：05-fable-fix-03 明傳單次 autoMemoryEnabled=false，init 工具只有 Read/Glob/Grep/Write/Edit；實際 4 次 Edit + 1 次 Write 僅觸及三份指定測試與 fix-03.md，沒有命令或記憶寫入。先前 5 次額外探查與 2 個已精確撤回的記憶 Edit 仍照實保留。第一次 smoke 後另核對原先觀察的 46 個舊設定路徑，lstat metadata 與直接目錄項目均無變動，未讀取其設定內容；證據保存在本機 external-settings-after-smoke-01-comparison.json。
