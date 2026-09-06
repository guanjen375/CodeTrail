# handoff S(W1):session 選單、重播與原子切換

只動 `plan-final.md` §3 的 S 列七個檔:`client_engine.py`、`client_app.py`、`client_store.py`、
`codetrail_chat.py`、`tests/test_client_app.py`、`tests/test_client_engine.py`、
`tests/test_client_store.py`(加本檔)。沒有 commit、沒有 push、沒有 stash / checkout。
`client_store` 的讀寫防線(`read_bytes` / `_append_raw` / `_open_private_dir` / `_validate_header` /
`delete` / `path`)**一行未改**。

`Tests: smoke only — reviewer owns full execution.`

## 1. 落檔的介面

### 與 §2 相同

* **I-1** `client_store`:`OUTLINE_MAX_CHARS = 80`;`session_outline(records) -> dict`
  (鍵 = `first_prompt` / `last_prompt` / `messages` / `tool_calls` / `compactions`,純函式、零 LLM、零寫入);
  `SessionInfo` 既有六欄不動 + 上面五個有預設值的欄位;`SessionStore.list_sessions(limit=None)`
  (排序後才切);`EphemeralSessionStore.list_sessions(limit=None)` 仍回 `[]`。
* **I-2** `client_engine`:`SessionSnapshot(session_id, messages=(), transcript=(), compactions=0)`
  (frozen);`Engine.load_session(id) -> SessionSnapshot`(唯一一次 `store.read()`、零狀態改動、
  壞 compaction 記錄丟 `ValueError`);`Engine.adopt(snapshot) -> None`;
  `Engine.resume(id) -> SessionSnapshot`;`Engine.resumed_snapshot`(`adopt` 設、`new_session` 清)。
  `summary` / `kept` / `dropped` 的算法逐字照 §2。
* **I-3** `client_app`:`HistoryEntry`、`history_entries(transcript) -> list[HistoryEntry]`、
  `format_tool_output(message) -> str`(live 的 `_tool_output(call_id)` 找到訊息後呼叫它)。
  配對、pending 字串、`SummaryBlock` 標題文字都照 §2。
* **I-4** `client_app`:`_switch_session`、`SessionPickerScreen(ModalScreen[str | None])`、
  `/session` 指令(`COMMANDS` 在 `/sessions` 之後)、`/sessions` 每列 `<id>  <時間>  <turns> 輪  <first_prompt>`、
  `action_interrupt` / `action_leave` 先收選單、重播的 `ToolBlock` 不進 `_tools`、
  `reasoning` 條目 `display = show_reasoning`、`/new` 清畫面、
  CLI `sessions` 輸出 `<id>\tturns=<n>\tupdated=<ISO 本地>\t<first_prompt>`。
  `codetrail_chat._build` / `command_chat` 形狀不變。

### 介面偏差(下游以本節為準)

| 舊(§2) | 新(落檔) | 理由 |
|---|---|---|
| `HistoryEntry` 無 `kept` | 加 `kept: int = 0` | 同一節指定的摘要標題文字要 `{kept}`;沒有這個欄位就算不出「{kept} 則逐字保留」 |
| `_replay_history(clear=False)` | `_replay_history(transcript, *, clear)`,內部拆成 `_entry_widgets(entries)` 與 `_mount_history(widgets, *, clear)` | §4 要求「先建 widget、再 `adopt`、最後換畫面」;建與掛必須是兩個可分開呼叫的步驟(啟動那條路仍是單一 `_replay_history(..., clear=False)`) |
| `_switch_session(session_id)` | `_switch_session(session_id, *, what="/session")` | busy 提示要講使用者剛打的那個指令(`/resume` 與 `/session` 共用同一條路);預設值讓選單回呼不必傳 |
| — | 新增模組層 `local_time(stamp)`、`session_row(info, *, with_id=False)` | 選單 / `/sessions` / CLI 三處的列格式只有一份 |
| — | 新增常數 `SESSION_PICKER_LIMIT=50`、`SESSION_LIST_LIMIT=20`、`PENDING_TOOL_STATUS="pending"`、`PENDING_TOOL_OUTPUT`、`ORPHAN_TOOL_NOTE`、`INCOMPLETE_ANSWER_NOTE` | 測試與畫面共用字面值,不各寫一份 |
| — | 新增 `SummaryBlock(Collapsible)`、`ReasoningBlock.text` property | 重播需要一個可展開的摘要標記與一個可讀回文字的 reasoning block |
| — | `SessionPickerScreen.AUTO_FOCUS = "#picker-list"` + `BINDINGS = [escape → action_cancel]` | Enter 要落在 OptionList 上;Esc 要收選單 |
| — | 私有 `client_engine._compaction_summary(history)`、`client_store._outline_text(value)` | 兩個小的純函式;`_compaction_summary` **在函式內** `import client_compaction`(與 `Engine.complete()` 同一個做法),摘要前綴的真值仍只有 `client_compaction.SUMMARY_PREFIX` 一份 |
| — | `assistant_error` 條目 = `AssistantBlock`(原文)+ 緊接一則 `ErrorLine(INCOMPLETE_ANSWER_NOTE)` | §2 只說要分 kind、沒說怎麼畫。原文放在 assistant 那一欄(不改字),「它不是答案」另起一行 —— 只畫成錯誤行會讓被截斷的半篇回答變成紅字,只畫成 assistant 又看不出它沒完成 |

## 2. test-changes

全部三個檔都有 module 層 `pytestmark = pytest.mark.smoke`(`test_client_app.py:35`、
`test_client_engine.py:38`、`test_client_store.py:20`),所以下面每一條新增測試都是 smoke,
不需要單條 decorator。**沒有刪除任何測試、沒有改名、沒有放寬任何既有斷言。**

| 檔 | 測試 / fixture 名 | 動作 | 行為為什麼該變 |
|---|---|---|---|
| `tests/test_client_app.py` | `from dataclasses import dataclass` | 新增 import(W0) | `_Snapshot` 用 |
| `tests/test_client_app.py` | `_Snapshot` | 新增替身 dataclass(W0) | 複述 I-2 的 `SessionSnapshot` 欄位形狀;app 只讀屬性,runtime **不得**對它 `isinstance` |
| `tests/test_client_app.py` | `_Engine.__init__`(`stored` / `resumed_snapshot`) | 新增屬性(W0) | 承載替身的「session 檔」與啟動接續 |
| `tests/test_client_app.py` | `_Engine.load_session` / `_Engine.adopt` | 新增(W0) | I-2 把一次受信讀取與原子切換拆開,替身要同形狀 |
| `tests/test_client_app.py` | `_Engine.resume` | 改行為(W0):`adopt(load_session(id))` 並回傳 | 同上;不改的話兩條 regression 會紅在「新 API 不存在」而不是「沒有重播」 |
| `tests/test_client_app.py` | `_Engine.new_session` | 加清 `resumed_snapshot`(W0) | `/new` 之後啟動重播的判斷不得指向上一段對話 |
| `tests/test_client_app.py` | `RESUMED_ID` / `RESUMED_TRANSCRIPT` / `_resumable` | 新增 fixture(W0) | §5.1 情境所需 |
| `tests/test_client_app.py` | `test_resume_replays_the_stored_history` | 新增(W0 紅 / W1 綠) | 真實 bug:`/resume` 只換 engine 歷史、畫面留白,使用者面對空白畫面而模型看得到整段脈絡 |
| `tests/test_client_app.py` | `test_a_session_resumed_at_startup_is_shown_on_mount` | 新增(W0 紅 / W1 綠) | 同一個 bug 的另一條路(`aicode -c` / `--session`,最常走的一條) |
| `tests/test_client_app.py` | `_Store.list_sessions` | 改簽名 `(self, limit=None)` + 切片 | runtime 現在要的是**有上限**的清單(選單 50、`/sessions` 20);替身不接就會在 `TypeError` 上紅,而不是在被測行為上 |
| `tests/test_client_app.py` | `_snapshot()` | 加 `reasoning`、`summaries` 兩個鍵 | 重播要驗的是 reasoning(含顯示旗標)與壓縮標記;既有鍵與斷言一個都沒動 |
| `tests/test_client_app.py` | `test_switching_sessions_is_refused_while_a_turn_is_running` | 參數化加 `/session`、`/session <id>`(docstring 同步) | `/session` 是第三條會換掉 `session_id` 與 `messages` 的路,必須走同一條 busy 拒絕 |
| `tests/test_client_app.py` | `_info` / `OTHER_ID` / `_pickable` / `GROUPED_TRANSCRIPT` / `COMPACTED_TRANSCRIPT` | 新增 fixture | 選單與兩條重播契約的資料 |
| `tests/test_client_app.py` | `test_the_session_picker_lists_outlines_and_switches` | 新增契約 | 選單只列 id 的話,使用者唯一能做的是逐個試接續,而每試一次都換掉 engine 歷史 |
| `tests/test_client_app.py` | `test_escape_and_ctrl_c_only_close_the_picker` | 新增契約 | 把收選單算成「已中斷這一輪」是謊報;算成離開的話 Ctrl-C 收完選單再按一次就退出程式 |
| `tests/test_client_app.py` | `test_a_failed_switch_keeps_the_session_and_the_screen` | 新增契約 | 先換 engine 再重播會留下「模型在新對話、畫面是舊那段」,下一題被寫進另一段對話而畫面上看不出來 |
| `tests/test_client_app.py` | `test_replay_pairs_tool_results_by_declaration_group_not_by_id` | 新增契約 | fallback call id 每個行程從 `call_1` 起算、同一段對話必然重複;以 id 反查會把結果貼到幾十輪前那個 block 上(順帶釘住 pending / orphan / reasoning / error 的顯示) |
| `tests/test_client_app.py` | `test_replay_shows_pre_compaction_originals_and_a_summary_marker` | 新增契約 | 畫面跟著模型歷史走 = 壓縮過的那段在畫面上永久消失(檔案裡明明還在);tail 重畫 = 同一段問答出現兩次 |
| `tests/test_client_app.py` | `test_replayed_tool_blocks_are_not_registered_for_live_events` | 新增契約 | `_tools` 以 call id 當 key;塞進重播的 block 之後,新的一次呼叫會更新到舊 block 上 |
| `tests/test_client_app.py` | `test_new_clears_the_screen` | 新增契約 | 舊對話留在畫面上的話,新對話第一個回答會接在模型看不到的一段下面 |
| `tests/test_client_engine.py` | `test_load_session_leaves_the_engine_untouched_and_adopt_switches_atomically` | 新增契約 | 讀取當場就換 engine 的話,畫面建不出來時會留下半換狀態;順帶釘住 `adopt` 換的是複本、`store_error` 只屬於這個 session |
| `tests/test_client_engine.py` | `test_the_snapshot_model_history_is_compacted_while_the_transcript_keeps_the_originals` | 新增契約(內含 `import client_compaction`) | 兩份歷史必須同源且語意相反:模型看壓縮後的、畫面看壓縮前的原文,壓縮只是一個標記 |
| `tests/test_client_store.py` | `test_the_outline_is_the_first_real_question_never_the_summary_or_tool_output` | 新增契約 | 拿摘要 / 工具輸出當大綱是無聲的:每段對話在選單上長得一樣,使用者認不出哪一段是自己的;而且大綱不得跑模型、不得回寫 session 檔 |

## 3. 紅綠證據(§5.1 兩條 regression)

紅燈在 `handoff-S-red.md`(HEAD `23e8a00`,runtime 零 diff,兩條各 `collected 1 item`、exit 1)。
本輪實作完成後同兩個 node 各單跑一次,命令逐字如下。

```
python3 scripts/run_tests.py tests/test_client_app.py::test_resume_replays_the_stored_history
```

exit code **0**:

```
platform linux -- Python 3.14.4, pytest-9.1.1, pluggy-1.6.0
rootdir: /home/david/CodeTrail
configfile: pyproject.toml
collected 1 item

tests/test_client_app.py .                                               [100%]

============================== 1 passed in 0.91s ===============================
[run_tests] PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /usr/bin/python3 -m pytest tests/test_client_app.py::test_resume_replays_the_stored_history
```

```
python3 scripts/run_tests.py tests/test_client_app.py::test_a_session_resumed_at_startup_is_shown_on_mount
```

exit code **0**:

```
platform linux -- Python 3.14.4, pytest-9.1.1, pluggy-1.6.0
rootdir: /home/david/CodeTrail
configfile: pyproject.toml
collected 1 item

tests/test_client_app.py .                                               [100%]

============================== 1 passed in 0.88s ===============================
[run_tests] PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /usr/bin/python3 -m pytest tests/test_client_app.py::test_a_session_resumed_at_startup_is_shown_on_mount
```

兩條的斷言與 node 名與紅燈那一輪逐字相同(替身、fixture、斷言都沒有為了轉綠而放寬);
兩次都是 `collected 1 item`,不是 exit code 5。

**綠燈之後又落了兩處編輯,兩處都不在這兩個 node 的路徑上,所以沒有再跑一次**:
`client_app.SessionPickerScreen.AUTO_FOCUS = "#picker-list"`(選單專用,這兩條不開選單),
以及 `test_the_session_picker_lists_outlines_and_switches` /
`test_escape_and_ctrl_c_only_close_the_picker` 兩條**新**契約各多一行前置 `await _settle(pilot)`。

## 4. 給其他 owner 的字

**給 D1**(`tests/test_smoke_gate.py` 的 `SAFETY_MODULES`)——三個鍵之下新增的 node 全名:

* `tests/test_client_app.py`:
  * `test_resume_replays_the_stored_history`
  * `test_a_session_resumed_at_startup_is_shown_on_mount`
  * `test_the_session_picker_lists_outlines_and_switches`
  * `test_escape_and_ctrl_c_only_close_the_picker`
  * `test_a_failed_switch_keeps_the_session_and_the_screen`
  * `test_replay_pairs_tool_results_by_declaration_group_not_by_id`
  * `test_replay_shows_pre_compaction_originals_and_a_summary_marker`
  * `test_replayed_tool_blocks_are_not_registered_for_live_events`
  * `test_new_clears_the_screen`
* `tests/test_client_engine.py`:
  * `test_load_session_leaves_the_engine_untouched_and_adopt_switches_atomically`
  * `test_the_snapshot_model_history_is_compacted_while_the_transcript_keeps_the_originals`
* `tests/test_client_store.py`:
  * `test_the_outline_is_the_first_real_question_never_the_summary_or_tool_output`

名稱與 `plan-final.md` §5.1 / §5.2 逐字相同。三個檔的 smoke 標記來自 module 層 `pytestmark`。
說明欄若要補字,可用:`client_app` 加「接續 / 啟動重播必須貼出原始記錄(文字、reasoning、
工具含未裁切 structuredContent、壓縮標記),工具結果按宣告群組配對,重播 block 不登記給即時
事件,busy 一律拒絕換,失敗保持 session 與畫面,`/new` 清畫面,選單的 Esc / Ctrl-C 只收選單」;
`client_engine` 加「`load_session` 是唯一一次受信讀取、`adopt` 之前零改動、transcript 只以標記
呈現 compaction」;`client_store` 加「大綱只取本地真實 user 訊息,零 LLM、零寫入」。

**給 D1**(AGENTS.md §2,`plan-final.md` §6.3 已定稿)——實作的符號名與那段文字一致:
`load_session` / `adopt` / `resumed_snapshot` / 宣告群組配對 / 重播 block 不進 `_tools` /
大綱零寫入。§6.3 不需要因為本輪的介面偏差改字(`kept` 欄位與兩個 helper 名稱都在 §2 的層級之下)。

**給 B**(`client_compaction.py`)——`SUMMARY_PREFIX` 這個**名字**現在多一個 runtime 消費者:
`client_engine._compaction_summary()` 用它從 compaction 記錄裡取回摘要正文。改名的話
`load_session` 不會炸,而是**靜默**把摘要標記的內容變成空字串(標記還在、正文不見)。
本輪核對過 B 的 diff:`client_compaction.py:70` 未改名。

**給 D2**(文件,非必要但建議)——使用者可見的兩處變動:
1. TUI 多一個 `/session` 指令(`/session <id>` 直接換、`/session` 開選單);`/help` 的清單是
   `client_app.COMMANDS`,文件裡沒有第二份清單,所以沒有必改的地方。
2. headless `python3 codetrail_chat.py sessions` 每列多一欄:
   `<id>\tturns=<n>\tupdated=<ISO 本地>\t<第一句問題>`(原本是 `<id>\tturns=<n>\t<title>`,
   而 `title` 永遠是空的)。`docs/session-model-eval.md:26` 只說「可由 `/sessions` 或
   `codetrail_chat.py sessions` 取得 session id」,仍然成立,不需要改。

## 5. 未完成 / 疑點

* **§5.2 的十條新契約沒有執行過**(政策:本輪只准跑 §5.1 那兩條 regression)。它們與兩條
  regression 共用同一組替身與 pilot 慣例,但「綠」由編排者交付前那唯一一次 smoke 決定;
  本檔不宣稱它們通過。
* 途中只跑過:`python3 -m compileall -q`(七個檔皆過)與幾次不含 pytest 的純函式 / Textual API
  探查(`history_entries`、`session_outline`、`_compaction_summary`、`OptionList` 的
  `option_count` / `get_option_at_index`)。沒有跑 full / smoke / collect-only / `--lf`,
  沒有碰真服務、tmux、`~/.config`、真實 session 或 NDA repo。
* 已知 UX 小瑕疵(照 §2 的指定順序,不自行更動):`COMMANDS` 裡 `/sessions` 排在 `/session`
  之前,所以輸入 `/session` 再按 Tab 會補成 `/sessions`。指令本身是逐字比對,直接按 Enter
  仍然走 `/session`。要改的話是 `plan-final.md` §2 I-4 的順序決定,不在本輪授權內。
* `_Engine` 替身沒有 `payload_messages()`,所以 `_recount_context()` 在這些測試裡會吃到
  `AttributeError` 並被既有的 broad except 吞掉 —— 與本輪之前的行為相同,不是新引入的。
