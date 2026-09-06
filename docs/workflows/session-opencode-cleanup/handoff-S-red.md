# handoff S-red(W0):`§5.1` 兩條 regression 的紅燈證據

只動 `tests/test_client_app.py`(新增兩條 regression + 替身最小擴充)與本檔。**runtime 零改動**;
沒有 commit、沒有 push、沒有跑其他測試。

## 1. 落檔的介面

同 `plan-final.md` §2(I-2 / I-4),但 W0 只在**測試側**複述形狀,runtime 還沒有這些符號:

| 測試側符號(`tests/test_client_app.py`) | 形狀 | 對應 |
|---|---|---|
| `_Snapshot`(frozen dataclass) | `session_id: str`、`messages: tuple[dict, ...] = ()`、`transcript: tuple[dict, ...] = ()`、`compactions: int = 0` | I-2 的 `client_engine.SessionSnapshot` |
| `_Engine.stored: dict[str, _Snapshot]` | 替身的「session 檔」 | — |
| `_Engine.load_session(session_id) -> _Snapshot` | 查 `stored`;查不到丟 `ValueError`;engine 狀態零改動 | I-2 |
| `_Engine.adopt(snapshot) -> None` | 原子換 `session_id` / `messages` / `store_error=None` / `resumed_snapshot` | I-2 |
| `_Engine.resume(session_id) -> _Snapshot` | `adopt(load_session(id))` 並回傳 | I-2 |
| `_Engine.resumed_snapshot: _Snapshot \| None` | `adopt` 設、`new_session` 清 | I-2 |
| `RESUMED_ID` / `RESUMED_TRANSCRIPT` / `_resumable(engine, session_id=RESUMED_ID)` | 模組層 fixture:一段存下來的對話 + 放進替身 | §5.1 的情境欄 |

**`_Snapshot` 是結構替身(duck typing),不是 `client_engine.SessionSnapshot` 的子類。**
W1 落 runtime 時,`_switch_session` / `on_mount` 只能讀 `snapshot.transcript` / `.messages` /
`.session_id` / `.compactions` 這幾個屬性,**不得**對它做 `isinstance(..., SessionSnapshot)` 把關;
要把關就得同時改這兩條 regression,而那會讓紅綠證據失效。

`RESUMED_TRANSCRIPT` 的四筆記錄是照 runtime 真的會寫進 session 檔的形狀寫的
(`client_engine._tool_reply` / `client_engine.py:1182-1191` / `_finalise_tool_calls` 的 `wire`):

* `{"role": "user", "content": …, "time": …}`
* `{"role": "assistant", "content": "在 boot/ 底下。", "time": …}`
* `{"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
  "function": {"name": "list_dir", "arguments": '{"path": "."}'}}], "time": …}`
* `{"role": "tool", "tool_call_id": "call_1", "name": "list_dir", "content": "a\nb\nc",
  "tool_status": "completed", "structured": {…, "truncated_from": 900}, "time": …}`

## 2. test-changes

| 檔 | 測試 / fixture 名 | 動作 | 行為為什麼該變 |
|---|---|---|---|
| `tests/test_client_app.py` | `test_resume_replays_the_stored_history` | 新增(module 層 `pytestmark = pytest.mark.smoke` 已涵蓋) | 真實 bug:`/resume` 只換 engine 的歷史、畫面留在原地,使用者看到空白畫面但模型看得到整段脈絡;工具輸出與壓縮前的原文再也調不出來 |
| `tests/test_client_app.py` | `test_a_session_resumed_at_startup_is_shown_on_mount` | 新增(同上,smoke) | 同一個 bug 的另一條路(`aicode -c` / `aicode --session <id>`,最常走的一條):重播只掛在 `/resume` 上仍然是空白畫面 |
| `tests/test_client_app.py` | `_Engine.resume`(既有替身) | 改行為:從「只設 `session_id`」改成 `adopt(load_session(id))` 並回傳 snapshot | I-2 把一次受信讀取與原子切換拆開;替身要跟 runtime 同形狀,否則這兩條 regression 會紅在「新 API 不存在」而不是「沒有重播」 |
| `tests/test_client_app.py` | `_Engine.new_session`(既有替身) | 加一行 `self.resumed_snapshot = None` | I-2:`adopt` 設、`new_session` 清;不清的話 `/new` 之後啟動重播的判斷會指向上一段對話 |
| `tests/test_client_app.py` | `_Engine.__init__`(既有替身) | 新增 `stored` / `resumed_snapshot` 兩個屬性 | 同上,承載 `load_session` / 啟動接續 |
| `tests/test_client_app.py` | `_Snapshot`、`RESUMED_ID`、`RESUMED_TRANSCRIPT`、`_resumable` | 新增 | §5.1 情境所需的最小 fixture |
| `tests/test_client_app.py` | `from dataclasses import dataclass` | 新增 import | `_Snapshot` 用 |

**沒有動到任何既有測試的斷言、名稱或標記。** 替身的三處改動對既有測試的影響已逐條核對:

* `test_switching_sessions_is_refused_while_a_turn_is_running[/resume …]` 在 `_busy_notice` 就 return
  (`client_app.py:745-750`),`engine.resume` 根本不會被呼叫 → 新的 `ValueError` 路徑碰不到。
* 其他測試都不呼叫 `resume`;新增的屬性是加法,現行 runtime 沒有人讀 `resumed_snapshot`。
* W1 之後 `on_mount` 會讀 `resumed_snapshot`:其餘測試的 `_Engine` 它是 `None`,不會觸發重播。

## 3. 紅燈證據(未修改任何 runtime;`HEAD = 23e8a00`,工作樹 runtime 零 diff)

命令逐字如下,各執行一次。

```
python3 scripts/run_tests.py tests/test_client_app.py::test_resume_replays_the_stored_history
```

exit code **1**(`collected 1 item`,不是 0 collected):

```
platform linux -- Python 3.14.4, pytest-9.1.1, pluggy-1.6.0
rootdir: /home/david/CodeTrail
configfile: pyproject.toml
collected 1 item

tests/test_client_app.py F                                               [100%]

=================================== FAILURES ===================================
____________________ test_resume_replays_the_stored_history ____________________
tests/test_client_app.py:679: in test_resume_replays_the_stored_history
    assert seen["users"] == ["bootloader 在哪一支檔?"]
E   AssertionError: assert [] == ['bootloader 在哪一支檔?']
E     
E     Right contains one more item: 'bootloader 在哪一支檔?'
E     Use -v to get more diff
=========================== short test summary info ============================
FAILED tests/test_client_app.py::test_resume_replays_the_stored_history - Ass...
============================== 1 failed in 0.96s ===============================
[run_tests] PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /usr/bin/python3 -m pytest tests/test_client_app.py::test_resume_replays_the_stored_history
```

```
python3 scripts/run_tests.py tests/test_client_app.py::test_a_session_resumed_at_startup_is_shown_on_mount
```

exit code **1**(`collected 1 item`):

```
platform linux -- Python 3.14.4, pytest-9.1.1, pluggy-1.6.0
rootdir: /home/david/CodeTrail
configfile: pyproject.toml
collected 1 item

tests/test_client_app.py F                                               [100%]

=================================== FAILURES ===================================
_____________ test_a_session_resumed_at_startup_is_shown_on_mount ______________
tests/test_client_app.py:707: in test_a_session_resumed_at_startup_is_shown_on_mount
    assert seen["users"] == ["bootloader 在哪一支檔?"]
E   AssertionError: assert [] == ['bootloader 在哪一支檔?']
E     
E     Right contains one more item: 'bootloader 在哪一支檔?'
E     Use -v to get more diff
=========================== short test summary info ============================
FAILED tests/test_client_app.py::test_a_session_resumed_at_startup_is_shown_on_mount
============================== 1 failed in 0.89s ===============================
[run_tests] PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /usr/bin/python3 -m pytest tests/test_client_app.py::test_a_session_resumed_at_startup_is_shown_on_mount
```

**兩條都紅在「沒有重播」那一行,不是 setup / 替身錯誤**:

* 第一條在斷言之前先過了 `assert engine.session_id == RESUMED_ID` —— 現行 `_cmd_resume` 有呼叫
  `engine.resume()`,替身的 `load_session` / `adopt` 走得通,session 真的換過去了;紅的是畫面。
* 第二條是 `run_test()` 起來之後的 `on_mount`:現行只貼 banner 與 `輸入 /help 看指令。`,
  `#log` 裡一個 `UserMessage` / `AssistantBlock` / `ToolBlock` 都沒有。
* 兩條的 `collected 1 item` 表示 node id 選得到(不是 exit code 5)。

## 4. diff 摘要

本 CLI 只改了一個既有檔(另外新增本檔;`docs/workflows/session-opencode-cleanup/model-log.md`
的改動是編排者寫的,不是本 CLI):

```
tests/test_client_app.py | 137 ++++++++++++++++++++-
```

共 137 行新增、1 行刪除(刪的是替身舊的 `resume` 那一行 `self.session_id = session_id`):

* `+1` import:`from dataclasses import dataclass`
* `+13` 替身區:`_Snapshot` frozen dataclass
* `+3` `_Engine.__init__`:`stored` / `resumed_snapshot` 兩個屬性(含一行註解)
* `+1` `_Engine.new_session`:清 `resumed_snapshot`
* `+16 / -1` `_Engine`:`load_session` / `adopt` / 改寫的 `resume`(= `adopt(load_session(id))`)
* `+103` 新區塊「接續既有對話:畫面要重播那段對話」:`RESUMED_ID` / `RESUMED_TRANSCRIPT` /
  `_resumable()` 與兩條 regression,插在
  `test_switching_sessions_is_refused_while_a_turn_is_running` 之後

## 5. 給其他 owner 的字

**給 W1(同 owner S,綠燈那半)**——這兩條紅燈釘住的斷言,實作時要對得上:

1. 重播用的是**既有 widget 類別**:`UserMessage`(`.message`)、`AssistantBlock`(`.text`)、
   `ToolBlock`(`.title` / `.output`)。測試的 `_snapshot()` helper 按類別抓,換成別的 widget
   就等於這兩條紅燈沒被修好。
2. `ToolBlock` 的標題形狀是 `tool_summary(tool, arguments)`,所以 `HistoryEntry.arguments`
   必須是**解析過**的 dict(從 wire 的 `function.arguments` JSON 字串解出來),
   斷言是 `"list_dir(path=.)" in title and "completed" in title`。
3. 展開區走 I-3 的 `format_tool_output()`:content ＋ 未裁切的 `structuredContent`
   (斷言 `"a\nb\nc"`、`"truncated_from"`、`"900"`)。
4. `content is None` 且帶 `tool_calls` 的那則 assistant **不得**產生 `AssistantBlock`
   (`seen["assistant"]` 斷言只有一則)——I-3 的規則。
5. 兩條路都要貼含 `已接續` 與 session id 的 `NoticeLine`;`on_mount` 的重播接在 banner 與
   `輸入 /help 看指令。` **之後**(第二條斷言 banner 還在)。
6. `/resume` 這條路要保持「先 `load_session` 再 `adopt`」的順序(失敗零改動那條契約是 §5.2 的
   `test_a_failed_switch_keeps_the_session_and_the_screen`,替身的 `load_session` 已經會對未知 id
   丟 `ValueError`,可以直接拿來用)。

**給 D1**——`tests/test_smoke_gate.py` 的 `SAFETY_MODULES` 要登記這兩個 node
(`tests/test_client_app.py` 鍵之下):

* `test_resume_replays_the_stored_history`
* `test_a_session_resumed_at_startup_is_shown_on_mount`

兩條的 smoke 標記來自 `tests/test_client_app.py:35` 的 module 層 `pytestmark`(gate 的
`_test_functions()` 認 module 層 `pytestmark`,不需要單條 decorator)。

## 6. 未完成 / 疑點

* W1 的綠燈證據不在本檔;照 `handoff-execution.md`,同兩個 node 轉綠後貼進 `handoff-S.md`。
* §5.2 的七條新契約 smoke 與 `/session` 參數化擴充都**還沒寫**(W1 的範圍),本輪刻意不碰。
* 沒有跑 full / smoke / collect-only / 任何既有 node;沒有動真服務、tmux、`~/.config` 或真實 session。
* `Tests: smoke only — reviewer owns full execution.`
