"""client_app(Textual TUI)的介面契約。

守四件事:

* **核准框**完整顯示參數(含整份 patch)、只認真的 ``bool``;
* **Ctrl-C 與 Esc 是兩層語意**:框裡的 Esc 只拒絕那個工具(回合繼續),
  Ctrl-C 中斷整輪(框開著時也一樣);閒置時的 Ctrl-C 不得顯示成「已中斷」;
* **無 tty 明確拒絕**,不靜默降級成另一種介面;
* **輸入歷史檔**逐字含使用者問過的問題,所以讀寫兩端都走 owner-only 防線。
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import client_app  # noqa: E402
import client_engine  # noqa: E402
import client_events  # noqa: E402
import client_store  # noqa: E402
import client_turns  # noqa: E402
import codetrail_chat  # noqa: E402

pytestmark = pytest.mark.smoke


# ============================================================
# 替身
# ============================================================
class _Store:
    def __init__(self):
        self.created = 0
        self.sessions: list = []

    def create(self):
        self.created += 1
        return f"20260101T00000{self.created}-abcdef01"

    def append(self, *_a, **_k):
        return None

    def path(self, _session_id):
        return None

    def list_sessions(self):
        return list(self.sessions)


class _Spec:
    def __init__(self, name, read_only=True):
        self.name = name
        self.read_only = read_only


class _Engine:
    def __init__(self, store=None):
        self.store = store or _Store()
        self.session_id = "20260101T000000-abcdef01"
        self.messages: list[dict] = []
        self.store_error = None
        self.sent: list[str] = []
        self.cancelled = False
        self.options = types.SimpleNamespace(
            model="test-model",
            n_ctx=8192,
            max_output_tokens=4096,
            policy=types.SimpleNamespace(name="interactive"),
        )
        self.system_prompt = types.SimpleNamespace(sections=())
        self.tool_specs = {"list_dir": _Spec("list_dir"), "apply_patch": _Spec("apply_patch", False)}

    def request_cancel(self, *, arm_when_idle: bool = False):
        self.cancelled = True
        return client_engine.CancelDecision(True, None)

    @staticmethod
    def cancel_pending(_call):
        return False

    def clear_cancel(self):
        self.cancelled = False

    def new_session(self):
        self.session_id = self.store.create()
        self.messages = []
        return self.session_id

    def resume(self, session_id):
        self.session_id = session_id

    def send(self, text, *, on_event=None, on_text=None, on_reasoning=None, approve=None):
        self.sent.append(text)
        if on_text:
            on_text("hello")
        if on_event:
            on_event(client_events.text_event(self.session_id, "hello"))
        return client_engine.TurnResult(text="hello", finish="stop", steps=1, tool_calls=0)


def _run(body):
    """在同步測試裡跑一段 Textual pilot。不引進 async 測試外掛。"""
    return asyncio.run(body())


async def _settle(pilot, times=25):
    for _ in range(times):
        await pilot.pause()


def _snapshot(app):
    """在 pilot 還開著的時候把畫面抄下來。離開 ``run_test`` 之後 widget 已經卸載。"""
    log = app.query_one("#log")
    kids = list(log.children)
    return {
        "notices": [w.message for w in kids if isinstance(w, client_app.NoticeLine)],
        "errors": [w.message for w in kids if isinstance(w, client_app.ErrorLine)],
        "users": [w.message for w in kids if isinstance(w, client_app.UserMessage)],
        "assistant": [w.text for w in kids if isinstance(w, client_app.AssistantBlock)],
        "tools": [(w.title, w.output) for w in kids if isinstance(w, client_app.ToolBlock)],
        "status": app.status_text,
        "completions": app.completion_text,
        "return_value": app.return_value,
    }


# ============================================================
# 核准框
# ============================================================
PATCH = "--- a/x.py\n+++ b/x.py\n@@\n-old\n+new\n" * 40


class _AsksApproval(_Engine):
    def __init__(self, store=None):
        super().__init__(store)
        self.granted: list[bool] = []
        self.asked = threading.Event()
        self.finished = threading.Event()

    def send(self, text, *, on_event=None, on_text=None, on_reasoning=None, approve=None):
        self.sent.append(text)
        self.asked.set()
        self.granted.append(approve(client_engine.ApprovalRequest(self.session_id, "apply_patch", {"diff": PATCH})))
        if self.cancelled:
            raise client_events.TurnCancelled("user")
        self.finished.set()
        return client_engine.TurnResult(text="", finish="stop", steps=1, tool_calls=1)


async def _wait_for_approval(app, pilot):
    for _ in range(400):
        if isinstance(app.screen, client_app.ApprovalScreen):
            return app.screen
        await pilot.pause()
    raise AssertionError("核准框沒有出現")


def test_the_approval_box_shows_the_whole_patch():
    """截斷過的核准等於沒有核准:整份 patch 必須逐字在框裡,而且框是可捲動的。"""
    engine = _AsksApproval()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app.submit("改一下")
            screen = await _wait_for_approval(app, pilot)
            detail = screen.query_one("#approval-detail")
            assert PATCH in str(detail.content)
            # 可捲動:內容放在 VerticalScroll 裡,不是被截掉的一行。
            from textual.containers import VerticalScroll

            assert isinstance(detail.parent, VerticalScroll)
            await pilot.press("escape")
            await _settle(pilot)
        return app

    _run(body)
    assert engine.granted == [False]


@pytest.mark.parametrize(
    ("key", "expected"), [("y", True), ("n", False), ("escape", False)]
)
def test_the_approval_keys_only_answer_this_one_tool(key, expected):
    """Esc / n 只拒絕**這個工具**,回合繼續跑完(不是中斷整輪)。"""
    engine = _AsksApproval()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app.submit("改一下")
            await _wait_for_approval(app, pilot)
            await pilot.press(key)
            await _settle(pilot)
        return app

    _run(body)
    assert engine.granted == [expected]
    assert engine.finished.wait(5)              # 這一輪照常結束,不是被中斷


def test_a_dismissed_approval_is_a_refusal():
    """畫面被收掉(dismiss 沒帶值)一律當拒絕:UI 交給協調器的只能是真的 ``bool``,
    ``bool("false")`` 是 True 這種形狀進不來。"""
    engine = _Engine()
    answers: list[bool] = []
    kinds: list[type] = []

    async def body():
        app = client_app.CodeTrailApp(engine)
        request = client_engine.ApprovalRequest("s", "run_command", {})
        worker = threading.Thread(
            target=lambda: answers.append(app.coordinator.request_approval(request))
        )
        async with app.run_test() as pilot:
            worker.start()
            for _ in range(400):
                if isinstance(app.screen, client_app.ApprovalScreen):
                    break
                await pilot.pause()
            kinds.append(type(app.screen))
            app.screen.dismiss(None)
            await _settle(pilot)
        worker.join(5)

    _run(body)
    assert kinds == [client_app.ApprovalScreen]
    assert answers == [False]


# ============================================================
# Ctrl-C 的兩層語意
# ============================================================
class _Blocks(_Engine):
    def __init__(self, store=None):
        super().__init__(store)
        self.entered = threading.Event()
        self.release = threading.Event()

    def send(self, text, *, on_event=None, on_text=None, on_reasoning=None, approve=None):
        self.sent.append(text)
        self.entered.set()
        self.release.wait(5)
        raise client_events.TurnCancelled("user")


def test_ctrl_c_during_a_turn_cancels_the_whole_turn():
    engine = _Blocks()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app.submit("hi")
            assert engine.entered.wait(5)
            await pilot.press("ctrl+c")
            engine.release.set()
            await _settle(pilot, 60)
            return _snapshot(app)

    seen = _run(body)
    assert engine.cancelled is False              # finish_turn 之後旗標已清
    assert "已中斷這一輪。" in seen["notices"]
    # 中斷不是離開:app 沒有因為 Ctrl-C 就結束。
    assert seen["return_value"] is None


def test_ctrl_c_with_the_approval_box_open_cancels_the_whole_turn():
    """核准框開著時的 Ctrl-C 是中斷整輪,不是拒絕這個工具。"""
    engine = _AsksApproval()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app.submit("改一下")
            await _wait_for_approval(app, pilot)
            await pilot.press("ctrl+c")
            await _settle(pilot, 60)
            return _snapshot(app)

    seen = _run(body)
    assert engine.granted == [False]
    assert not engine.finished.is_set()          # 這一輪被中斷,沒有跑完
    assert "已中斷這一輪。" in seen["notices"]


def test_a_cancel_right_after_submitting_still_lands():
    """送出之後立刻 Ctrl-C:worker 還沒進 send(),engine 看不到進行中的東西。"""
    gate = threading.Event()

    class _Prestart(_Engine):
        def request_cancel(self, *, arm_when_idle=False):
            assert arm_when_idle is True
            self.cancelled = True
            gate.set()
            return client_engine.CancelDecision(True, None)

        def send(self, *_a, **_k):
            gate.wait(5)
            raise client_events.TurnCancelled("armed")

    engine = _Prestart()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app.submit("hi")
            await pilot.press("ctrl+c")
            await _settle(pilot, 60)
            return _snapshot(app)

    seen = _run(body)
    assert "已中斷這一輪。" in seen["notices"]


def test_ctrl_c_while_idle_needs_two_presses_and_never_claims_a_cancel():
    engine = _Engine()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            await _settle(pilot)
            first = _snapshot(app)["notices"]
            assert app.return_value is None
            await pilot.press("ctrl+c")
            await _settle(pilot)
            return app.return_value, first

    returned, first = _run(body)
    assert any("再按一次" in note for note in first)
    assert not any("已中斷" in note for note in first)
    assert returned == 0


def test_ctrl_d_with_the_approval_box_open_only_refuses_that_tool():
    """框內 EOF = 拒絕**這一個工具**,回合繼續(沿用舊 REPL 的核准提示語意)。
    直接拆掉畫面會留下一個卡在核准上的 worker,而呼叫端隨即關掉共用的 MCP。"""
    engine = _AsksApproval()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app.submit("改一下")
            await _wait_for_approval(app, pilot)
            await pilot.press("ctrl+d")
            await _settle(pilot, 60)
            return app.return_value

    assert _run(body) is None                    # 沒有離開
    assert engine.granted == [False]             # 只拒絕了這個工具
    assert engine.finished.wait(5)               # 這一輪照常跑完


@pytest.mark.parametrize("how", ["ctrl+d", "/exit"])
def test_leaving_is_refused_while_a_turn_is_running(how):
    """回合進行中直接退出會留下卡住的 worker;要停就先 Ctrl-C。"""
    engine = _Blocks()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app.submit("hi")
            assert engine.entered.wait(5)
            if how == "ctrl+d":
                await pilot.press("ctrl+d")
            else:
                app._command("/exit")
            await _settle(pilot)
            returned = app.return_value
            notes = _snapshot(app)["notices"]
            engine.release.set()
            await _settle(pilot, 60)
            return returned, notes

    returned, notes = _run(body)
    assert returned is None
    assert any("這一輪還在跑" in note for note in notes)


def test_ctrl_c_does_not_block_the_ui_on_a_slow_mcp_cancel():
    """MCP 取消要等寬限期(10 秒)+ SIGTERM + 重新 spawn。同步跑在 UI 執行緒上
    就是整個畫面凍住,worker 送事件的 call_from_thread 也跟著排在後面。"""
    released = threading.Event()

    class _SlowCancel(_Blocks):
        def request_cancel(self, *, arm_when_idle=False):
            self.cancelled = True
            return client_engine.CancelDecision(True, object())

        @staticmethod
        def cancel_pending(_call):
            released.wait(5)
            return True

    engine = _SlowCancel()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app.submit("hi")
            assert engine.entered.wait(5)
            started = time.monotonic()
            await pilot.press("ctrl+c")
            elapsed = time.monotonic() - started
            released.set()
            engine.release.set()
            await _settle(pilot, 60)
            return elapsed

    assert _run(body) < 1.0


def test_ctrl_d_leaves():
    engine = _Engine()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            await pilot.press("ctrl+d")
            await _settle(pilot)
            return app.return_value

    assert _run(body) == 0


# ============================================================
# 無 tty
# ============================================================
def test_a_pipe_is_refused_and_points_at_the_headless_entry(monkeypatch, capsys):
    """TUI 會接管整個畫面;pipe 過去只會得到一團控制碼。明確拒絕,不靜默降級。"""
    monkeypatch.setattr(codetrail_chat, "_has_tty", lambda: False)
    assert codetrail_chat.main([]) == 2
    err = capsys.readouterr().err
    assert "tty" in err and "run" in err


# ============================================================
# 對話流
# ============================================================
def test_a_turn_streams_text_and_lists_tools_without_calling_the_model():
    engine = _Engine()

    async def body():
        app = client_app.CodeTrailApp(engine, banner=("root=/tmp",))
        async with app.run_test() as pilot:
            await pilot.press(*"hi")
            await pilot.press("enter")
            await _settle(pilot, 60)
            app._command("/tools")
            await _settle(pilot)
            return _snapshot(app)

    seen = _run(body)
    assert engine.sent == ["hi"]
    assert seen["users"] == ["hi"]
    assert seen["assistant"] == ["hello"]
    assert any("list_dir" in note for note in seen["notices"])


def test_a_tool_call_shows_a_summary_and_can_be_expanded():
    """工具輸出**不進事件流**(那份 JSONL 是 canary / eval 的輸入);畫面從 engine 的
    tool 訊息取全文——含 `structuredContent`(未裁切的核心,刻意只給畫面與 eval)。
    只取 text 的話,展開看到的是套過 budget 的節錄。"""
    engine = _Engine()
    engine.messages = [
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "list_dir",
            "content": "a\nb\nc",
            "structured": {"entries": ["a", "b", "c"], "truncated_from": 900},
        }
    ]

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app.handle_event(
                client_events.tool_event(
                    engine.session_id,
                    tool="list_dir",
                    call_id="call-1",
                    status=client_events.STATUS_COMPLETED,
                    arguments={"path": "."},
                )
            )
            await _settle(pilot)
            return _snapshot(app)

    seen = _run(body)
    assert len(seen["tools"]) == 1
    title, output = seen["tools"][0]
    assert "list_dir(path=.)" in title and "completed" in title
    assert "a\nb\nc" in output
    assert "truncated_from" in output and "900" in output


def test_a_new_session_rebinds_the_compactor():
    """`/new` 之後不重綁的話,上一段的摘要會進新對話的摘要請求、上一段的停用會把新的
    一段停掉(engine 那半由 test_client_engine 守)。"""
    engine = _Engine()
    rebinds: list[int] = []

    async def body():
        compactor = types.SimpleNamespace(
            mode="manual", pending_stop_notice=lambda: "", rebind=lambda: rebinds.append(1)
        )
        app = client_app.CodeTrailApp(engine, compactor=compactor)
        async with app.run_test() as pilot:
            app._command("/new")
            await _settle(pilot)
            return _snapshot(app)

    seen = _run(body)
    assert rebinds == [1]
    assert engine.session_id != "20260101T000000-abcdef01"
    assert any("新對話" in note for note in seen["notices"])


def test_a_failed_new_keeps_the_app_and_the_current_session():
    """建不了新 session 就留在原地:反過來的話記憶體歷史已經清空,使用者還停在舊 session。"""
    class _Flaky(_Store):
        def create(self):
            if self.created:
                self.created += 1
                raise OSError("disk full")
            return super().create()

    engine = _Engine(store=_Flaky())
    engine.session_id = engine.store.create()
    engine.messages = [{"role": "user", "content": "q"}]
    session = engine.session_id

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app._command("/new")
            await _settle(pilot)
            return _snapshot(app)

    seen = _run(body)
    assert engine.session_id == session
    assert engine.messages == [{"role": "user", "content": "q"}]
    assert any("無法開新對話" in message for message in seen["errors"])


@pytest.mark.parametrize("command", ["/new", "/resume 20260101T000000-bbbbbbbb"])
def test_switching_sessions_is_refused_while_a_turn_is_running(command):
    """`/new` / `/resume` 直接換掉 engine 的 session_id 與 messages:在回合中做
    等於把還沒寫完的答案與自動壓縮落到**另一段**對話。"""
    engine = _Blocks()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app.submit("hi")
            assert engine.entered.wait(5)
            app._command(command)
            await _settle(pilot)
            session = engine.session_id
            notes = _snapshot(app)["notices"]
            engine.release.set()
            await _settle(pilot, 60)
            return session, notes

    session, notes = _run(body)
    assert session == "20260101T000000-abcdef01"       # 沒有被換掉
    assert any("這一輪還在跑" in note for note in notes)


def test_a_second_question_during_a_turn_is_not_shown_and_keeps_the_input():
    """被 Busy 拒絕的那一則不得先貼進對話區(畫面上會多一則模型沒看過的問題),
    輸入框也要留著原文。"""
    engine = _Blocks()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app.submit("first")
            assert engine.entered.wait(5)
            await pilot.press(*"second")
            await pilot.press("enter")
            await _settle(pilot)
            seen = _snapshot(app)
            kept = app.query_one("#prompt").text
            engine.release.set()
            await _settle(pilot, 60)
            return seen, kept

    seen, kept = _run(body)
    assert seen["users"] == ["first"]
    assert kept == "second"
    assert engine.sent == ["first"]


def test_slash_commands_never_reach_the_model():
    engine = _Engine()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            for line in ("/help", "/status", "/tools", "/nope"):
                await pilot.press(*line)
                await pilot.press("enter")
                await _settle(pilot)
            return _snapshot(app)

    seen = _run(body)
    assert engine.sent == []
    notes = "\n".join(seen["notices"])
    assert "/compact" in notes and "未知指令" in notes


def test_thinking_only_toggles_the_display():
    """`/thinking` 只切換畫面。送模 payload 與摘要輸入是另一個設定,狀態列的值不變。"""
    engine = _Engine()

    async def body():
        app = client_app.CodeTrailApp(engine, keep_historical_reasoning=True)
        async with app.run_test() as pilot:
            app._on_reasoning("想一下")
            await _settle(pilot)
            before = app.status_text
            hidden = [w.display for w in app.query(client_app.ReasoningBlock)]
            app._command("/thinking")
            await _settle(pilot)
            shown = [w.display for w in app.query(client_app.ReasoningBlock)]
            return app.status_text, before, hidden, shown

    after, before, hidden, shown = _run(body)
    assert hidden == [False] and shown == [True]
    assert "舊 reasoning=送模" in before and "舊 reasoning=送模" in after


def test_typing_a_slash_offers_completions():
    engine = _Engine()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            await pilot.press("slash")
            await _settle(pilot)
            visible = app.query_one("#completions").display
            text = app.completion_text
            await pilot.press("tab")
            await _settle(pilot)
            completed = app.query_one("#prompt").text
        return visible, text, completed

    visible, text, completed = _run(body)
    assert visible is True
    assert "/compact" in text and "/thinking" in text
    assert completed == "/help"


def test_the_status_bar_carries_the_session_context_and_modes():
    engine = _Engine()
    engine.system_prompt = types.SimpleNamespace(
        sections=(types.SimpleNamespace(name="project_agents", source="AGENTS.md", chars=10),)
    )

    async def body():
        compactor = types.SimpleNamespace(mode="codetrail", pending_stop_notice=lambda: "", rebind=lambda: None)
        app = client_app.CodeTrailApp(engine, compactor=compactor)
        async with app.run_test() as pilot:
            app.handle_event(
                client_events.step_finish_event(engine.session_id, reason="stop")
            )
            await _settle(pilot)
            return app.status_text

    status = _run(body)
    for needle in (
        "test-model",
        "n_ctx=8192",
        "ctx≈",
        engine.session_id,
        "權限=interactive",
        "壓縮=codetrail",
        "專案指示=on",
        "舊 reasoning=不送",
    ):
        assert needle in status, (needle, status)


# ============================================================
# 輸入歷史檔:與 session store 同一組防線
# ============================================================
@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """真的 state tree:`$XDG_STATE_HOME/codetrail/sessions/<root hash>`。

    測試不能自己捏一個 `tmp/sessions/hash`:那個位置根本不在 state home 底下,
    dir-fd 防線會直接拒絕,測試就變成「什麼都沒寫也算過」。
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    directory = client_store.sessions_dir(tmp_path / "project")
    directory.mkdir(parents=True)
    return directory


def _run_app(state, body):
    engine = _Engine()

    async def wrapper():
        app = client_app.CodeTrailApp(engine, state_dir=state)
        async with app.run_test() as pilot:
            await _settle(pilot)
            return await body(app, pilot)

    return _run(wrapper)


def _save_history(state, lines):
    async def body(app, pilot):
        app.query_one("#prompt", client_app.PromptInput).input_history = list(lines)
        app._save_history()
        await pilot.pause()

    _run_app(state, body)


def test_saving_history_never_writes_through_a_hard_link(state_dir, tmp_path):
    """先 O_TRUNC 開檔再驗 nlink 的話,被指向的別人的檔案在拒絕之前就先被清空了。"""
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    os.link(victim, state_dir / client_app.HISTORY_FILENAME)
    _save_history(state_dir, ["問過的 NDA 問題"])
    assert victim.read_text(encoding="utf-8") == "keep me\n"
    written = state_dir / client_app.HISTORY_FILENAME
    assert client_app.decode_history(written.read_bytes()) == ["問過的 NDA 問題"]
    assert written.stat().st_nlink == 1 and oct(written.stat().st_mode & 0o777) == "0o600"


def test_saving_history_never_follows_a_symlink(state_dir, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    (state_dir / client_app.HISTORY_FILENAME).symlink_to(victim)
    _save_history(state_dir, ["問過的 NDA 問題"])
    assert victim.read_text(encoding="utf-8") == "keep me\n"
    assert not (state_dir / client_app.HISTORY_FILENAME).is_symlink()


def test_a_symlinked_middle_component_is_refused(tmp_path, monkeypatch):
    """anchor 要放在 state home:放在 `sessions/` 的話,把 `codetrail/` 或
    `sessions/` 換成 symlink 就能把這份逐字含 NDA 問題的檔寫到受管 tree 之外。"""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    directory = client_store.sessions_dir(tmp_path / "project")
    outside = tmp_path / "outside"
    (outside / "sessions" / directory.name).mkdir(parents=True)
    (tmp_path / "state" / "codetrail").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "codetrail").symlink_to(outside)
    _save_history(directory, ["問過的 NDA 問題"])
    assert not (outside / "sessions" / directory.name / client_app.HISTORY_FILENAME).exists()


def test_loading_history_never_reads_through_a_symlink(state_dir, tmp_path):
    """path-based 讀會跟著 symlink 走,把別人的檔讀進歷史。"""
    victim = tmp_path / "victim.txt"
    victim.write_text("SOMEONE ELSES SECRET LINE\n", encoding="utf-8")
    (state_dir / client_app.HISTORY_FILENAME).symlink_to(victim)

    async def body(app, _pilot):
        return list(app.query_one("#prompt", client_app.PromptInput).input_history)

    assert "SOMEONE ELSES SECRET LINE" not in _run_app(state_dir, body)


def test_history_is_saved_on_a_normal_exit_and_round_trips_multiline(state_dir):
    """`App.on_unmount` 靠不住(拆畫面時子 widget 已經被移除),所以正常退出的
    路徑必須自己存;而輸入框是多行的,一則含換行的問題不得被拆成兩筆歷史。"""
    engine = _Engine()
    multiline = "第一行\n第二行"

    async def body():
        app = client_app.CodeTrailApp(engine, state_dir=state_dir)
        async with app.run_test() as pilot:
            await _settle(pilot)
            prompt = app.query_one("#prompt", client_app.PromptInput)
            prompt.input_history = ["第一題", multiline]
            await pilot.press("ctrl+d")
            await _settle(pilot)
            return app.return_value

    assert _run(body) == 0
    written = state_dir / client_app.HISTORY_FILENAME
    assert written.exists(), "正常退出必須落檔"
    assert client_app.decode_history(written.read_bytes()) == ["第一題", multiline]

    async def reopen(app, _pilot):
        return list(app.query_one("#prompt", client_app.PromptInput).input_history)

    assert _run_app(state_dir, reopen) == ["第一題", multiline]


def test_the_arrows_recall_the_input_history(state_dir):
    _save_history(state_dir, ["第一題", "第二題"])

    async def body(app, pilot):
        await pilot.press("up")
        await _settle(pilot)
        first = app.query_one("#prompt").text
        await pilot.press("up")
        await _settle(pilot)
        return first, app.query_one("#prompt").text

    first, second = _run_app(state_dir, body)
    assert first == "第二題" and second == "第一題"


# ── 總審 F1-8:換 session 之後不得把舊 block 當成新呼叫的 block ──


def test_a_new_session_forgets_the_old_tool_blocks():
    engine = _Engine()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app.handle_event(
                client_events.tool_event(
                    engine.session_id, tool="list_dir", call_id="call_0",
                    status=client_events.STATUS_COMPLETED, arguments={"path": "."},
                )
            )
            await _settle(pilot)
            app._command("/new")
            await _settle(pilot)
            app.handle_event(
                client_events.tool_event(
                    engine.session_id, tool="read_file", call_id="call_0",
                    status=client_events.STATUS_COMPLETED, arguments={"path": "a.py"},
                )
            )
            await _settle(pilot)
            return _snapshot(app)

    seen = _run(body)
    names = [t for t in seen["tools"]]
    assert any("read_file" in str(t) for t in names), seen["tools"]
