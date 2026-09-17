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
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import client_app  # noqa: E402
import client_compaction  # noqa: E402
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

    def list_sessions(self, limit=None):
        sessions = list(self.sessions)
        return sessions if limit is None else sessions[:limit]


class _Spec:
    def __init__(self, name, read_only=True):
        self.name = name
        self.read_only = read_only


@dataclass(frozen=True)
class _Snapshot:
    """`Engine.load_session()` 回的東西:模型歷史與畫面歷史來自**同一次**讀取。

    這裡只複述欄位形狀(engine 那半由 test_client_engine 守),app 只讀屬性。
    """

    session_id: str
    messages: tuple[dict, ...] = ()
    transcript: tuple[dict, ...] = ()
    compactions: int = 0


class _Engine:
    def __init__(self, store=None):
        self.store = store or _Store()
        self.session_id = "20260101T000000-abcdef01"
        self.messages: list[dict] = []
        self.store_error = None
        #: session id → 存下來的快照(替身的「session 檔」)。
        self.stored: dict[str, _Snapshot] = {}
        self.resumed_snapshot: _Snapshot | None = None
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
        #: 預熱:每一次 `prime_prompt_cache(reason=…)` 的 reason(engine 那半的准入與
        #: 零寫入由 test_client_engine 守,這裡只記錄)。`primed` 讓測試等那條背景
        #: 執行緒,不必靠 sleep。
        self.primes: list[str] = []
        self.primed = threading.Event()
        self.priming = False

    def request_cancel(self, *, arm_when_idle: bool = False):
        self.cancelled = True
        return client_engine.CancelDecision(True, None)

    @staticmethod
    def cancel_pending(_call):
        return False

    def clear_cancel(self):
        self.cancelled = False

    def prime_prompt_cache(self, *, reason: str = ""):
        self.primes.append(reason)
        self.primed.set()
        # 形狀同 §4.2 的 `PrimeOutcome(sent, reason, processed_tokens)`。
        return types.SimpleNamespace(sent=True, reason="", processed_tokens=7)

    def new_session(self):
        self.session_id = self.store.create()
        self.messages = []
        self.resumed_snapshot = None
        return self.session_id

    def load_session(self, session_id):
        """只讀,不動 engine 任何狀態(換過去是 ``adopt`` 的事)。"""
        snapshot = self.stored.get(session_id)
        if snapshot is None:
            raise ValueError(f"沒有這段對話:{session_id}")
        return snapshot

    def adopt(self, snapshot):
        self.session_id = snapshot.session_id
        self.messages = [dict(item) for item in snapshot.messages]
        self.store_error = None
        self.resumed_snapshot = snapshot

    def resume(self, session_id):
        snapshot = self.load_session(session_id)
        self.adopt(snapshot)
        return snapshot

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


def test_review_modal_blocks_chat_session_changes_and_closes_through_cancel(monkeypatch):
    import client_review

    engine = _Engine()
    entered = threading.Event()
    held = []
    real_job = client_review.ReviewJob

    class Job(real_job):
        def run(self, progress):
            entered.set()
            progress("收集變更中")
            assert self._cancelled.wait(3)
            return client_review.ReviewOutcome("cancelled", "cancelled", detail="cancelled")

    monkeypatch.setattr(client_review, "ReviewJob", Job)

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app._cmd_review("")
            for _ in range(30):
                await pilot.pause()
                if entered.is_set():
                    break
            assert entered.is_set() and isinstance(app.screen, client_app.ReviewScreen)
            review = app.screen
            assert app.coordinator.reviewing and app.coordinator.busy
            app._cmd_new("")
            app._cmd_compact("")
            app._cmd_session("another-session")
            assert engine.store.created == 0 and engine.sent == [] and engine.messages == []
            assert not app.submit("retain my draft") and engine.sent == []
            assert not isinstance(app.screen, client_app.QueueChoiceScreen)
            assert not list(app.query(client_app.AssistantBlock)), "review drafts entered the chat"
            assert review.query_one("#review-body", client_app.VerticalScroll)
            await pilot.press("escape")
            for _ in range(30):
                await pilot.pause()
                if not app.coordinator.busy and not isinstance(app.screen, client_app.ReviewScreen):
                    break
            assert not app.coordinator.busy and not app.coordinator.reviewing
            assert not isinstance(app.screen, client_app.ReviewScreen)
            assert engine.messages == [] and not engine.cancelled
            assert engine.primes == ["mount"], "review scheduled another prime"
            assert app.submit("normal chat after review")
            for _ in range(30):
                await pilot.pause()
                if not app.coordinator.busy:
                    break
            assert engine.sent == ["normal chat after review"]
            held.append(review.report)

    _run(body)
    assert held and "未寫入聊天歷史" in held[0]


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
        "reasoning": [(w.text, w.display) for w in kids if isinstance(w, client_app.ReasoningBlock)],
        "summaries": [(w.title, w.summary) for w in kids if isinstance(w, client_app.SummaryBlock)],
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


@pytest.mark.parametrize(
    "command",
    [
        "/new",
        "/resume 20260101T000000-bbbbbbbb",
        "/session",
        "/session 20260101T000000-bbbbbbbb",
    ],
)
def test_switching_sessions_is_refused_while_a_turn_is_running(command):
    """`/new` / `/resume` / `/session` 直接換掉 engine 的 session_id 與 messages:在回合中做
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


# ============================================================
# 接續既有對話:畫面要重播那段對話
# ============================================================
#: 一段存下來的對話:一則問題、一則回答、一則宣告工具呼叫的 assistant,以及
#: 那次呼叫的結果(含只給畫面與 eval 的 `structured`)。形狀就是 session 檔裡
#: `message` 記錄去掉 `type` 之後的樣子。
RESUMED_ID = "20260101T000000-cccccccc"
RESUMED_TRANSCRIPT: tuple[dict, ...] = (
    {"role": "user", "content": "bootloader 在哪一支檔?", "time": 1.0},
    {"role": "assistant", "content": "在 boot/ 底下。", "time": 2.0},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "list_dir", "arguments": '{"path": "."}'},
            }
        ],
        "time": 3.0,
    },
    {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "list_dir",
        "content": "a\nb\nc",
        "tool_status": client_events.STATUS_COMPLETED,
        "structured": {"entries": ["a", "b", "c"], "truncated_from": 900},
        "time": 4.0,
    },
)


def _resumable(engine, session_id=RESUMED_ID):
    """把上面那段對話放進替身的「session 檔」,回傳它的快照。"""
    snapshot = _Snapshot(
        session_id=session_id,
        messages=RESUMED_TRANSCRIPT,
        transcript=RESUMED_TRANSCRIPT,
        compactions=0,
    )
    engine.stored[session_id] = snapshot
    return snapshot


def test_resume_replays_the_stored_history():
    """接續一段既有對話之後,畫面上要有那段對話。

    只換掉 engine 的歷史、畫面留在原地的話,使用者面對的是一個空白畫面,
    但模型看得到整段脈絡:接下來每一則回答都在回應畫面上不存在的東西
    (「照你剛剛說的」指的是使用者看不到的那一段),而且工具輸出、
    reasoning 與壓縮過的部分再也沒有辦法在這個介面裡看到。
    """
    engine = _Engine()
    _resumable(engine)

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app._command(f"/resume {RESUMED_ID}")
            await _settle(pilot)
            return _snapshot(app)

    seen = _run(body)
    assert engine.session_id == RESUMED_ID
    assert seen["users"] == ["bootloader 在哪一支檔?"]
    assert seen["assistant"] == ["在 boot/ 底下。"]
    assert len(seen["tools"]) == 1, seen["tools"]
    title, output = seen["tools"][0]
    assert "list_dir(path=.)" in title and "completed" in title
    # 展開區跟 live 那條路一樣是完整輸出:content + 未裁切的 structuredContent。
    assert "a\nb\nc" in output
    assert "truncated_from" in output and "900" in output
    assert any("已接續" in note and RESUMED_ID in note for note in seen["notices"])


def test_a_session_resumed_at_startup_is_shown_on_mount():
    """`aicode -c` / `aicode --session <id>` 在建 app 之前就接續好了。

    重播只掛在 `/resume` 上的話,啟動時接續的那條路(最常用的一條)照樣是
    空白畫面。engine 帶著 `resumed_snapshot` 進來,畫面就要把它貼出來。
    """
    engine = _Engine()
    snapshot = _resumable(engine)
    engine.adopt(snapshot)                     # `_build` 已經 resume 過

    async def body():
        app = client_app.CodeTrailApp(engine, banner=("root=/tmp",))
        async with app.run_test() as pilot:
            await _settle(pilot)
            return _snapshot(app)

    seen = _run(body)
    assert seen["users"] == ["bootloader 在哪一支檔?"]
    assert seen["assistant"] == ["在 boot/ 底下。"]
    assert len(seen["tools"]) == 1, seen["tools"]
    assert any("已接續" in note and RESUMED_ID in note for note in seen["notices"])
    # banner 與 /help 提示還在:重播是加在它們之後,不是取代啟動畫面。
    assert "root=/tmp" in seen["notices"]


# ============================================================
# 重播的內容契約:配對、壓縮標記、pending / orphan、與即時事件的界線
# ============================================================
def _info(session_id, *, updated, turns, first_prompt):
    return client_store.SessionInfo(
        session_id=session_id,
        path=Path(f"/nonexistent/{session_id}.jsonl"),
        created=updated - 60.0,
        updated=updated,
        title="",
        turns=turns,
        first_prompt=first_prompt,
    )


OTHER_ID = "20260101T000000-dddddddd"


def _pickable(engine):
    """兩段可選的對話:最近更新的那一段排前面(就是 RESUMED_TRANSCRIPT 那一段)。"""
    _resumable(engine)
    engine.stored[OTHER_ID] = _Snapshot(session_id=OTHER_ID)
    engine.store.sessions = [
        _info(RESUMED_ID, updated=1_800_000_000.0, turns=2, first_prompt="bootloader 在哪一支檔?"),
        _info(OTHER_ID, updated=1_700_000_000.0, turns=1, first_prompt="另一段對話問過的事"),
    ]


def test_the_session_picker_lists_outlines_and_switches():
    """`/session` 的選單要看得出「哪一段是哪一段」:時間、輪數、問過的第一句話。

    只列 session id 的話(`20260101T000000-abcdef01`),使用者唯一能做的就是逐個
    試接續 —— 而每試一次都會換掉 engine 的歷史。大綱是本地算的(零 LLM、零寫入)。
    """
    engine = _Engine()
    _pickable(engine)

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            await _settle(pilot)
            app._command("/session")
            await _settle(pilot)
            opened = isinstance(app.screen, client_app.SessionPickerScreen)
            listing = app.screen.query_one("#picker-list")
            rows = [
                str(listing.get_option_at_index(index).prompt)
                for index in range(listing.option_count)
            ]
            await pilot.press("enter")           # 選最上面那一段
            await _settle(pilot)
            return opened, rows, _snapshot(app)

    opened, rows, seen = _run(body)
    assert opened
    assert len(rows) == 2
    assert "bootloader 在哪一支檔?" in rows[0] and "2 輪" in rows[0]
    assert "另一段對話問過的事" in rows[1]
    # 選定 = 換過去而且畫面重播那一段。
    assert engine.session_id == RESUMED_ID
    assert seen["users"] == ["bootloader 在哪一支檔?"]
    assert any("已接續" in note and RESUMED_ID in note for note in seen["notices"])


def test_escape_and_ctrl_c_only_close_the_picker():
    """選單裡的 Esc / Ctrl-C 是「不選了」,不是中斷一輪、也不是離開。

    把它算成中斷會顯示成「已中斷這一輪」(閒置時根本沒有東西可中斷,那是謊報);
    算成離開的話,Ctrl-C 收掉選單之後再按一次就直接退出程式。
    """
    engine = _Engine()
    _pickable(engine)

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            await _settle(pilot)
            app._command("/session")
            await _settle(pilot)
            await pilot.press("escape")
            await _settle(pilot)
            after_escape = isinstance(app.screen, client_app.SessionPickerScreen)
            app._command("/session")
            await _settle(pilot)
            await pilot.press("ctrl+c")
            await _settle(pilot)
            after_ctrl_c = isinstance(app.screen, client_app.SessionPickerScreen)
            return after_escape, after_ctrl_c, _snapshot(app)

    after_escape, after_ctrl_c, seen = _run(body)
    assert after_escape is False and after_ctrl_c is False
    assert engine.session_id == "20260101T000000-abcdef01"      # 兩次都沒有換
    assert seen["users"] == [] and seen["tools"] == []
    assert not any("已中斷" in note for note in seen["notices"])
    assert not any("再按一次" in note for note in seen["notices"])
    assert seen["return_value"] is None                          # 也沒有離開


def test_a_failed_switch_keeps_the_session_and_the_screen():
    """讀不到那段對話 = 什麼都沒發生:engine、畫面、工具表、壓縮器一個都不准動。

    先換 engine 再重播的話,失敗會留下「模型在新對話、畫面是舊那段」的狀態:
    使用者對著舊畫面問下一題,那一題被寫進另一段對話,而畫面上看不出來。
    """
    engine = _Engine()
    engine.messages = [{"role": "user", "content": "原本這一段"}]
    rebinds: list[int] = []

    async def body():
        compactor = types.SimpleNamespace(
            mode="manual", pending_stop_notice=lambda: "", rebind=lambda: rebinds.append(1)
        )
        app = client_app.CodeTrailApp(engine, compactor=compactor, banner=("root=/tmp",))
        async with app.run_test() as pilot:
            app.handle_event(
                client_events.tool_event(
                    engine.session_id, tool="list_dir", call_id="call-9",
                    status=client_events.STATUS_COMPLETED, arguments={"path": "."},
                )
            )
            await _settle(pilot)
            app._command("/resume 20260101T000000-eeeeeeee")
            await _settle(pilot)
            return _snapshot(app), list(app._tools)

    seen, tools = _run(body)
    assert engine.session_id == "20260101T000000-abcdef01"
    assert engine.messages == [{"role": "user", "content": "原本這一段"}]
    assert engine.resumed_snapshot is None
    assert rebinds == []                              # 壓縮器沒有被重綁
    assert tools == ["call-9"]                        # 即時事件的那張表原封不動
    assert "root=/tmp" in seen["notices"]             # 畫面沒有被清掉
    assert len(seen["tools"]) == 1
    assert any("無法接續" in message for message in seen["errors"])


#: 同一個 `call_1` 出現兩次:fallback id 每個行程從 1 起算,所以同一段對話裡
#: 重複是**正常**的。第一次的呼叫沒有結果(crash / 中斷),第二次才有。
GROUPED_TRANSCRIPT: tuple[dict, ...] = (
    {"role": "user", "content": "看一下目錄", "time": 1.0},
    {
        "role": "assistant",
        "content": None,
        "reasoning_content": "先列目錄",
        "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "list_dir", "arguments": '{"path": "."}'}}
        ],
        "time": 2.0,
    },
    {"role": "user", "content": "再試一次", "time": 3.0},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "boot.c"}'}}
        ],
        "time": 4.0,
    },
    {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "read_file",
        "content": "int main(void)",
        "tool_status": client_events.STATUS_COMPLETED,
        "structured": {"lines": 1},
        "time": 5.0,
    },
    {
        "role": "tool",
        "tool_call_id": "call_ghost",
        "name": "grep_code",
        "content": "沒有人宣告過我",
        "tool_status": client_events.STATUS_ERROR,
        "time": 6.0,
    },
    {"role": "assistant", "content": "半截的答案", "tool_status": client_events.STATUS_ERROR,
     "time": 7.0},
)


def test_replay_pairs_tool_results_by_declaration_group_not_by_id():
    """結果只配**宣告它的那一組**呼叫。

    fallback call id 每個行程從 `call_1` 起算,同一段對話裡必然重複。以 id 反查
    整段歷史的話,第二次呼叫的結果會貼到幾十輪之前那個 block 上 —— 使用者看到的
    是「上一輪的工具突然自己更新了」,而真正這一次的呼叫顯示成沒有結果。舊群組
    沒被回答的那次是真的沒有結果(pending),配不到任何群組的則要標出來。
    """
    engine = _Engine()
    engine.stored[RESUMED_ID] = _Snapshot(
        session_id=RESUMED_ID, messages=GROUPED_TRANSCRIPT, transcript=GROUPED_TRANSCRIPT
    )

    async def body():
        app = client_app.CodeTrailApp(engine, show_reasoning=True)
        async with app.run_test() as pilot:
            app._command(f"/resume {RESUMED_ID}")
            await _settle(pilot)
            return _snapshot(app)

    seen = _run(body)
    assert [title for title, _output in seen["tools"]] == [
        f"· list_dir(path=.) → {client_app.PENDING_TOOL_STATUS}",
        "✓ read_file(path=boot.c) → completed",
        f"✗ grep_code() → error {client_app.ORPHAN_TOOL_NOTE}",
    ], seen["tools"]
    outputs = [output for _title, output in seen["tools"]]
    assert outputs[0] == client_app.PENDING_TOOL_OUTPUT
    assert "int main(void)" in outputs[1] and "structuredContent" in outputs[1]
    assert "沒有人宣告過我" in outputs[2]
    # reasoning 也重播(`/thinking` 管它顯不顯示),被標成 error 的那一則要看得出來。
    assert seen["reasoning"] == [("先列目錄", True)]
    assert seen["assistant"] == ["半截的答案"]
    assert client_app.INCOMPLETE_ANSWER_NOTE in seen["errors"]


#: 兩次壓縮:壓縮前的原文全部留著,壓縮本身只是一個標記(tail 不重畫)。
COMPACTED_TRANSCRIPT: tuple[dict, ...] = (
    {"role": "user", "content": "第一題", "time": 1.0},
    {"role": "assistant", "content": "第一答", "time": 2.0},
    {"type": "compaction", "time": 3.0, "summary": "第一次的摘要", "dropped": 2, "kept": 1},
    {"role": "user", "content": "第二題", "time": 4.0},
    {"role": "assistant", "content": "第二答", "time": 5.0},
    {"type": "compaction", "time": 6.0, "summary": "第二次的摘要", "dropped": 3, "kept": 1},
    {"role": "user", "content": "第三題", "time": 7.0},
)


def test_replay_shows_pre_compaction_originals_and_a_summary_marker():
    """壓縮過的對話:畫面留原文,標記只講「模型從這裡之後只看得到摘要」。

    畫面跟著模型歷史走的話,壓縮過的那一段在畫面上就永久消失了 —— 檔案裡明明
    還在,而使用者是靠捲回去看自己問過什麼。反過來把 tail 隨標記再畫一次,同一段
    問答會出現兩次,使用者分不出哪一次真的發生過。
    """
    engine = _Engine()
    engine.stored[RESUMED_ID] = _Snapshot(
        session_id=RESUMED_ID,
        messages=({"role": "user", "content": "第三題"},),
        transcript=COMPACTED_TRANSCRIPT,
        compactions=2,
    )

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app._command(f"/resume {RESUMED_ID}")
            await _settle(pilot)
            return _snapshot(app)

    seen = _run(body)
    assert seen["users"] == ["第一題", "第二題", "第三題"]
    assert seen["assistant"] == ["第一答", "第二答"]
    titles = [title for title, _summary in seen["summaries"]]
    assert [summary for _title, summary in seen["summaries"]] == ["第一次的摘要", "第二次的摘要"]
    assert "2 則已壓縮" in titles[0] and "1 則逐字保留" in titles[0]
    assert "3 則已壓縮" in titles[1]
    # notice 要講清楚兩份歷史不一樣長(模型只看得到 1 則)。
    assert any("畫面 7 則" in note and "模型歷史 1 則" in note and "壓縮 2 次" in note
               for note in seen["notices"]), seen["notices"]


def test_replayed_tool_blocks_are_not_registered_for_live_events():
    """重播出來的 block **不進** `_tools`。

    那張表以 call id 當 key 給即時事件用;塞進重播的 block 之後,新的一次
    `call_1` 會更新到上一段對話那個 block 上 —— 畫面上看起來是舊區塊自己動了,
    而這一次真正的呼叫從頭到尾沒有出現。
    """
    engine = _Engine()
    _resumable(engine)

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app._command(f"/resume {RESUMED_ID}")
            await _settle(pilot)
            registered = list(app._tools)
            app.handle_event(
                client_events.tool_event(
                    engine.session_id, tool="grep_code", call_id="call_1",
                    status=client_events.STATUS_COMPLETED, arguments={"pattern": "boot"},
                )
            )
            await _settle(pilot)
            return registered, _snapshot(app)

    registered, seen = _run(body)
    assert registered == []
    titles = [title for title, _output in seen["tools"]]
    assert len(titles) == 2, titles
    assert "list_dir" in titles[0] and "grep_code" in titles[1]


def test_new_clears_the_screen():
    """`/new` 之後畫面上不得留著上一段對話。

    留著的話,新對話的第一個回答會接在另一段對話下面,而模型完全看不到那一段 ——
    畫面顯示的脈絡與模型手上的從此不同。
    """
    engine = _Engine()
    _resumable(engine)

    async def body():
        app = client_app.CodeTrailApp(engine, banner=("root=/tmp",))
        async with app.run_test() as pilot:
            app._command(f"/resume {RESUMED_ID}")
            await _settle(pilot)
            app._command("/new")
            await _settle(pilot)
            return _snapshot(app)

    seen = _run(body)
    assert seen["users"] == [] and seen["assistant"] == [] and seen["tools"] == []
    assert seen["notices"] == [f"新對話:{engine.session_id}"], seen["notices"]


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
        "ctx=",
        engine.session_id,
        "權限=interactive",
        "壓縮=codetrail",
        "專案指示=on",
        "舊 reasoning=不送",
    ):
        assert needle in status, (needle, status)


@pytest.fixture()
def progress_clock(monkeypatch):
    """Only the prefill display clock advances; Textual's event loop stays real."""
    now = [0.0]
    monkeypatch.setattr(client_app, "_progress_clock", lambda: now[0])
    return now


@pytest.mark.smoke
def test_prompt_progress_replaces_waiting_and_previous_answer_phase(progress_clock):
    """真實 SSE 的 92% 要出現在等待列,下一步與答案後壓縮也不能卡在「回答中」。"""
    engine = _Engine()

    async def body():
        app = client_app.CodeTrailApp(engine, show_reasoning=True)
        async with app.run_test() as pilot:
            app.coordinator.begin_turn()
            app._turn_started = time.monotonic()
            try:
                statuses = []
                for operation in ("response", "response", "compact"):
                    app.handle_event({
                        "type": "activity", "sessionID": engine.session_id,
                        "part": {"operation": operation, "phase": "preparing"},
                    })
                    app.handle_event({
                        "type": "activity", "sessionID": engine.session_id,
                        "part": {
                            "operation": operation, "phase": "prompt_processing", "percent": 92,
                        },
                    })
                    assert "prompt processing" not in app._turn_phase()
                    progress_clock[0] += 10
                    app._refresh_status()
                    statuses.append(app.status_text)
                    if operation == "response":
                        app._on_reasoning("先讀工具結果")
                        app.handle_event(client_events.text_event(engine.session_id, "已讀到結果"))
                await pilot.pause()
                return statuses, _snapshot(app)
            finally:
                app.coordinator.finish_turn()

    statuses, seen = _run(body)
    assert all("prompt processing(92%)" in status for status in statuses), statuses
    assert "compact · prompt processing(92%)" in statuses[-1], statuses[-1]
    assert all("回答中" not in status and "thinking" not in status for status in statuses)
    assert seen["assistant"] == ["已讀到結果", "已讀到結果"]
    assert seen["reasoning"] == [("先讀工具結果", True), ("先讀工具結果", True)]
    assert seen["summaries"] == []


@pytest.mark.smoke
def test_activity_ignores_late_progress_and_resets_each_model_request(progress_clock):
    """晚到 prefill 不蓋掉生成;每步重新等候,工具與核准也不沿用舊答案相位。"""
    engine = _Engine()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app.coordinator.begin_turn()
            app._turn_started = time.monotonic()

            def activity(phase, **fields):
                app.handle_event(client_events.activity_event(
                    engine.session_id, operation="response", phase=phase, **fields,
                ))
                if phase == "prompt_processing":
                    progress_clock[0] += 10
                return app._turn_phase()

            try:
                assert activity("preparing") == "準備請求"
                assert activity("waiting_model") == "等待模型"
                assert activity("waiting_response") == "等待回應"
                assert activity("prompt_processing", percent=92) == "prompt processing(92%)"
                assert activity("generating") == "產生回應中"
                assert activity("prompt_processing", percent=100) == "產生回應中"
                app._on_reasoning("只在原 reasoning 區塊")
                assert activity("prompt_processing", percent=100) == "thinking 1 段"
                app.handle_event(client_events.text_delta_event(engine.session_id, "原回答"))
                assert activity("prompt_processing", percent=100) == "回答中"
                assert activity("approval", tool="apply_patch") == "等待核准 apply_patch"
                assert activity("tool", tool="list_dir") == "執行工具 list_dir"
                assert activity("prompt_processing", percent=100) == "執行工具 list_dir"
                assert activity("preparing") == "準備請求"
                assert app._reasoning_chunks == 0 and app._answer_started is False
                assert activity("waiting_response") == "等待回應"
                for percent in (None, True, -1, 101, 92.5, float("nan"), float("inf"), "92"):
                    assert activity("prompt_processing", percent=percent) == "prompt processing"
                assert activity("prompt_processing", percent=0) == "prompt processing(0%)"
                assert activity("prompt_processing", percent=100) == "prompt processing(100%)"
                app.handle_event({"type": "activity", "sessionID": engine.session_id, "part": []})
                assert app._turn_phase() == "prompt processing(100%)"
                # 文字回呼本身也要移除舊百分比,即使 generating 的 UI 通知沒有送達。
                app.handle_event(client_events.text_delta_event(engine.session_id, "續答"))
                assert app._turn_phase() == "回答中"
                assert app._activity == {"operation": "response", "phase": "generating"}
                await pilot.pause()
                seen = _snapshot(app)
                assert seen["assistant"] == ["原回答續答"]
                assert seen["reasoning"] == [("只在原 reasoning 區塊", False)]
                assert seen["tools"] == []  # activity 不自行新增或更新工具內容。
            finally:
                app.coordinator.finish_turn()

    _run(body)


@pytest.mark.smoke
def test_activity_is_cleared_when_a_turn_stops(progress_clock):
    """終結、錯誤與接受 Ctrl-C 立即清暫態,錯 session/已結束回合不得再灌進度。"""
    engine = _Engine()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            for ending in ("stop", "cancelled", "error", "error_event", "interrupt"):
                app.coordinator.begin_turn()
                app._turn_started = time.monotonic()
                try:
                    for phase, percent in (("preparing", None), ("prompt_processing", 92)):
                        app.handle_event(client_events.activity_event(
                            engine.session_id, operation="compact", phase=phase, percent=percent,
                        ))
                    app.handle_event(client_events.activity_event(
                        "another-session", operation="response", phase="preparing",
                    ))
                    progress_clock[0] += 10
                    app._refresh_status()
                    assert app._turn_phase() == "compact · prompt processing(92%)"
                    app.handle_event(client_events.step_finish_event(
                        engine.session_id, reason=client_events.REASON_TOOL_CALLS,
                    ))
                    progress_clock[0] += 10
                    app._refresh_status()
                    assert app._turn_phase() == "compact · prompt processing(92%)"
                    if ending == "interrupt":
                        app.action_interrupt()
                        assert app.coordinator.cancelled
                    elif ending == "error_event":
                        app.handle_event(client_events.error_event(engine.session_id, "request failed"))
                    else:
                        app.handle_event(client_events.step_finish_event(engine.session_id, reason=ending))
                    assert app._activity is None
                    assert app._prompt_started is None and app._prompt_display is None
                    if ending == "interrupt":
                        assert app._turn_started is not None
                        assert app._turn_phase() == "中斷中"
                        assert "中斷中" in app.status_text
                    else:
                        assert app._turn_started is None
                    assert app._compacting is False
                    # 協調器尚未放回合鎖時,UI 已收到終結也不准重新開始顯示。
                    app.handle_event(client_events.activity_event(
                        engine.session_id, operation="compact", phase="preparing",
                    ))
                    app.handle_event(client_events.activity_event(
                        engine.session_id, operation="compact", phase="prompt_processing", percent=100,
                    ))
                    assert app._activity is None
                    assert app._prompt_started is None and app._prompt_display is None
                    assert "prompt processing" not in app.status_text
                    if ending == "interrupt":
                        assert app._turn_phase() == "中斷中"
                        app.handle_event(client_events.step_finish_event(
                            engine.session_id, reason=client_events.REASON_CANCELLED,
                        ))
                        assert app._turn_started is None
                        assert "中斷中" not in app.status_text
                finally:
                    app.coordinator.finish_turn()
            # 只有時間戳也不算 active turn,預熱或遲到事件不得復活狀態列。
            app._turn_started = time.monotonic()
            app.handle_event(client_events.activity_event(
                engine.session_id, operation="response", phase="prompt_processing", percent=92,
            ))
            assert app._activity is None
            await pilot.pause()

    _run(body)


@pytest.mark.smoke
def test_activity_is_cleared_on_new_and_resumed_sessions(progress_clock):
    """session 切換清掉殘留活動,舊 session 的延遲通知不能覆蓋新回合。"""
    engine = _Engine()
    engine.stored[RESUMED_ID] = _Snapshot(RESUMED_ID)

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            for command in ("/new", f"/resume {RESUMED_ID}"):
                old_session = engine.session_id
                app.coordinator.begin_turn()
                app._turn_started = time.monotonic()
                try:
                    for phase, percent in (("preparing", None), ("prompt_processing", 92)):
                        app.handle_event(client_events.activity_event(
                            old_session, operation="compact", phase=phase, percent=percent,
                        ))
                    progress_clock[0] += 10
                    app._refresh_status()
                    assert app._turn_phase() == "compact · prompt processing(92%)"
                finally:
                    app.coordinator.finish_turn()
                app._command(command)
                assert app._activity is None
                assert app._prompt_started is None and app._prompt_display is None
                assert app._compacting is False
                assert app._activity_generating is False
                assert app._turn_started is None
                assert "prompt processing" not in app.status_text
                app.coordinator.begin_turn()
                app._turn_started = time.monotonic()
                try:
                    app.handle_event(client_events.activity_event(
                        engine.session_id, operation="response", phase="preparing",
                    ))
                    app.handle_event(client_events.activity_event(
                        old_session, operation="compact", phase="prompt_processing", percent=92,
                    ))
                    assert app._turn_phase() == "準備請求"
                finally:
                    app.coordinator.finish_turn()
            await pilot.pause()

    _run(body)


@pytest.mark.smoke
def test_manual_compaction_activity_uses_the_worker_bridge_without_answer_output(progress_clock):
    """/compact 的進度只在 UI 執行緒更新狀態,摘要生成不冒到回答或 reasoning 區。"""
    engine = _Engine()
    progressed, generate, generated, finish = (threading.Event() for _ in range(4))
    ui_threads = []

    async def body():
        def compact(*, manual=False):
            assert manual is True
            for phase, percent in (("preparing", None), ("waiting_response", None), ("prompt_processing", 92)):
                app._emit_from_worker(client_events.activity_event(
                    engine.session_id, operation="compact", phase=phase, percent=percent,
                ))
            progressed.set()
            assert generate.wait(5)
            app._emit_from_worker(client_events.activity_event(
                engine.session_id, operation="compact", phase="generating",
            ))
            # 摘要生成後即使還來 prefill 數字,也不能倒退回 100%。
            app._emit_from_worker(client_events.activity_event(
                engine.session_id, operation="compact", phase="prompt_processing", percent=100,
            ))
            generated.set()
            assert finish.wait(5)
            return types.SimpleNamespace(status="compacted", message="已壓縮")

        compactor = types.SimpleNamespace(mode="codetrail", compact=compact)
        app = client_app.CodeTrailApp(engine, compactor=compactor)
        original_handle = app.handle_event

        def handle(event):
            if event.get("type") == "activity":
                ui_threads.append(threading.get_ident())
            original_handle(event)

        app.handle_event = handle
        async with app.run_test() as pilot:
            app._command("/compact")
            try:
                assert await _until(pilot, progressed.is_set)
                assert app._turn_phase() == "compact · 等待回應"
                progress_clock[0] += 10
                app._refresh_status()
                assert app._turn_phase() == "compact · prompt processing(92%)"
                generate.set()
                assert await _until(pilot, generated.is_set)
                assert app._turn_phase() == "compact · 產生摘要中"
                finish.set()
                assert await _until(pilot, lambda: not app.coordinator.busy)
                assert app._activity is None and app._turn_started is None
                assert app._prompt_started is None and app._prompt_display is None
                assert "compact ·" not in app.status_text
                seen = _snapshot(app)
                assert seen["assistant"] == [] and seen["reasoning"] == []
                assert seen["summaries"] == [] and engine.messages == []
                assert ui_threads and set(ui_threads) == {threading.get_ident()}
            finally:
                generate.set()
                finish.set()

    _run(body)


# ============================================================
# Prefill details are sampled; all other phases remain immediate.
# ============================================================
@pytest.mark.parametrize("operation", ["response", "compact"])
def test_fast_prefill_never_delays_generation_or_leaks_into_the_next_request(progress_clock, operation):
    engine = _Engine()
    app = client_app.CodeTrailApp(engine)
    app.coordinator.begin_turn()
    app._turn_started = time.monotonic()

    def activity(phase, **fields):
        app.handle_event(client_events.activity_event(
            engine.session_id, operation=operation, phase=phase, **fields,
        ))

    try:
        activity("preparing")
        activity("prompt_processing", progress={"processed": 500, "total": 1000, "cache": 0, "time_ms": 500})
        progress_clock[0] = 9.999
        assert app._turn_phase() == ("compact · " if operation == "compact" else "") + "等待回應"
        activity("generating")
        expected = "compact · 產生摘要中" if operation == "compact" else "產生回應中"
        assert app._turn_phase() == expected
        assert app._prompt_started is None and app._prompt_display is None
        progress_clock[0] = 30
        activity("prompt_processing", percent=100)
        assert app._turn_phase() == expected
        activity("preparing")
        activity("prompt_processing", progress={"processed": 10, "total": 200, "cache": 0, "time_ms": 100})
        assert "等待回應" in app._turn_phase()
        progress_clock[0] = 40
        assert "10/200 tok" in app._turn_phase() and "500" not in app._turn_phase()
    finally:
        app.coordinator.finish_turn()


def test_prompt_snapshots_use_ten_second_samples_without_inventing_initial_speed(progress_clock):
    engine = _Engine()
    app = client_app.CodeTrailApp(engine)
    app.coordinator.begin_turn()
    app._turn_started = time.monotonic()

    def progress(processed, milliseconds):
        app.handle_event(client_events.activity_event(
            engine.session_id, operation="response", phase="prompt_processing",
            progress={"processed": processed, "total": 1000, "cache": 200, "time_ms": milliseconds},
        ))

    try:
        progress(200, 25)  # Server has not finished its first decode batch.
        progress_clock[0] = 9.999
        assert app._turn_phase() == "等待回應"
        progress_clock[0] = 10
        initial = app._turn_phase()
        assert initial == "prompt processing(20%) · 200/1,000 tok · cache 200"
        progress_clock[0] = 20
        assert app._turn_phase() == initial  # No false zero rate/time or stall claim.
        progress_clock[0] = 22
        progress(600, 2000)
        progress_clock[0] = 29.999
        assert app._turn_phase() == initial
        progress_clock[0] = 30
        measured = app._turn_phase()
        assert "600/1,000 tok" in measured and "200.0 tok/s · prefill 2s" in measured
        assert "距更新" not in measured
        progress_clock[0] = 41
        assert "距更新 19s" in app._turn_phase()
        progress_clock[0] = 42
        progress(610, 2050)
        assert "600/1,000 tok" in app._turn_phase() and "距更新" not in app._turn_phase()
        progress_clock[0] = 51
        assert "610/1,000 tok" in app._turn_phase()
        # Corrupt telemetry cannot keep the previous good numbers on screen.
        app.handle_event({"type": "activity", "sessionID": engine.session_id, "part": {
            "operation": "response", "phase": "prompt_processing", "percent": 99,
            "progress": {"processed": 610, "total": 1000, "cache": 611},
        }})
        assert app._turn_phase() == "prompt processing"
    finally:
        app.coordinator.finish_turn()


def test_compaction_commit_phases_clear_prefill_and_reject_late_snapshots(progress_clock):
    engine = _Engine()
    app = client_app.CodeTrailApp(engine)
    app.coordinator.begin_turn()
    app._turn_started = time.monotonic()
    try:
        for phase, expected in (("validating", "驗證摘要中"), ("persisting", "儲存摘要中")):
            app.handle_event(client_events.activity_event(engine.session_id, operation="compact", phase="preparing"))
            app.handle_event(client_events.activity_event(engine.session_id, operation="compact", phase="prompt_processing", percent=80))
            progress_clock[0] += 10
            assert "prompt processing" in app._turn_phase()
            app.handle_event(client_events.activity_event(engine.session_id, operation="compact", phase=phase))
            assert app._turn_phase() == f"compact · {expected}"
            assert app._prompt_display is None and app._prompt_started is None
            app.handle_event(client_events.activity_event(engine.session_id, operation="compact", phase="prompt_processing", percent=100))
            assert app._turn_phase() == f"compact · {expected}"
    finally:
        app.coordinator.finish_turn()


def test_slow_prompt_metrics_remain_visible_in_a_narrow_terminal(progress_clock):
    engine = _Engine()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test(size=(80, 30)) as pilot:
            app.coordinator.begin_turn()
            app._turn_started = time.monotonic()
            try:
                app.handle_event(client_events.activity_event(
                    engine.session_id, operation="compact", phase="prompt_processing",
                    progress={"processed": 8400, "total": 20000, "cache": 2000, "time_ms": 30000},
                ))
                assert "prompt processing" not in app.status_text
                progress_clock[0] = 10
                app._refresh_status()
                await pilot.pause()
                bar = app.query_one("#status")
                assert 2 <= bar.size.height <= 3
                assert "8,400/20,000 tok" in app.status_text
                assert "213.3 tok/s · prefill 30s" in app.status_text
                app.handle_event(client_events.activity_event(engine.session_id, operation="compact", phase="generating"))
                await pilot.pause()
                assert bar.size.height == 1
                assert "tok/s" not in app.status_text and "產生摘要中" in app.status_text
            finally:
                app.coordinator.finish_turn()

    _run(body)


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


# ============================================================
# prompt cache 預熱:歷史剛換過的那幾個時刻
# ============================================================
class _SlowPrime(_Engine):
    """預熱期間 `priming` 為 True(真 engine 只由持著模型鎖在送的那一次設 / 清)。"""

    def __init__(self, store=None):
        super().__init__(store)
        self.entered = threading.Event()
        self.release = threading.Event()

    def prime_prompt_cache(self, *, reason: str = ""):
        self.priming = True
        self.entered.set()
        self.release.wait(5)
        self.priming = False                     # 先落地才通知,狀態列讀得到確定的值
        return super().prime_prompt_cache(reason=reason)


def test_the_tui_primes_on_mount_new_and_session_switch_through_the_coordinator():
    """三個「歷史剛換過」的時刻要預熱:啟動、`/new`、換 session。

    這幾個時刻之後,下一輪要送的 prefix 跟 server 上快取的那一份對不起來,而使用者
    通常正在打第一題 —— 那是唯一不必等使用者的機會。預熱一律走協調器(只有它知道
    回合有沒有在跑),而且**不進對話區**:它不是一則對話內容,貼出來只會讓使用者
    以為自己問了什麼。回合進行中的 `/new` 本來就被擋下,那時候也不得偷排一次。
    """
    engine = _Engine()
    _resumable(engine)

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            assert engine.primed.wait(5)
            mount = list(engine.primes)
            for _ in range(400):                 # on_done 要搬回 UI 執行緒才記得下來
                if app._last_prime is not None:
                    break
                await pilot.pause()
            noted = app._last_prime
            engine.primed.clear()
            app._command("/new")
            assert engine.primed.wait(5)
            await _settle(pilot)
            after_new = list(engine.primes)
            engine.primed.clear()
            app._command(f"/session {RESUMED_ID}")
            assert engine.primed.wait(5)
            await _settle(pilot)
            return mount, after_new, list(engine.primes), noted, _snapshot(app)

    mount, after_new, after_switch, noted, seen = _run(body)
    assert mount == ["mount"]
    assert after_new == ["mount", "new"]
    assert after_switch == ["mount", "new", "session"]
    assert engine.session_id == RESUMED_ID
    # outcome 經協調器的 on_prime 搬回 UI 執行緒(`/status` 才有東西可講)、記錄帶觸發點,
    # 而對話區一個字都不多。
    assert noted is not None and noted[1] == "mount" and noted[2].sent is True
    assert not any("預熱" in note for note in seen["notices"])

    blocked = _Blocks()

    async def during_a_turn():
        app = client_app.CodeTrailApp(blocked)
        async with app.run_test() as pilot:
            assert blocked.primed.wait(5)
            app.submit("hi")
            assert blocked.entered.wait(5)
            app._command("/new")
            await _settle(pilot)
            reasons = list(blocked.primes)
            blocked.release.set()
            await _settle(pilot, 60)
            return reasons

    assert _run(during_a_turn) == ["mount"]

    slow = _SlowPrime()

    async def while_priming():
        app = client_app.CodeTrailApp(slow)
        async with app.run_test() as pilot:
            assert slow.entered.wait(5)
            app._refresh_status()
            during = app.status_text
            slow.release.set()
            assert slow.primed.wait(5)
            await _settle(pilot)
            app._refresh_status()
            return during, app.status_text

    during, after = _run(while_priming)
    # 預熱握著模型鎖:這時候送出的下一題會在鎖上等它,狀態列要講得出來。
    assert "預熱" in during, during
    assert "預熱" not in after, after


async def _until(pilot, predicate, timeout=5.0):
    """讓 UI 執行緒一邊跑、一邊等背景執行緒把事情做完。

    worker 的每一則事件都經 ``call_from_thread`` 搬進 UI 執行緒**而且會等它跑完**;
    在 UI 執行緒上 ``Event.wait()`` 的話 worker 永遠搬不過來。到期回 False、不 raise:
    要不要當成失敗由呼叫端決定(有些等待在未修的產品上本來就不會發生)。
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        await pilot.pause(0.01)
    return True


class _PrimeOutcomes(_Engine):
    """每一次預熱回**可區分**的 outcome:mount 送出,壓縮後那兩次各自跳過、原因不同。

    `/status` 要講得出「最近一次是哪一次」,三個 outcome 得先分得開。形狀同 §4.2 的
    `PrimeOutcome(sent, reason, processed_tokens)`,不 import 真型別。
    """

    def __init__(self, store=None):
        super().__init__(store)
        self.outcomes: list = []

    def prime_prompt_cache(self, *, reason: str = ""):
        if reason == "compaction":
            nth = sum(1 for item in self.primes if item == "compaction")
            outcome = types.SimpleNamespace(
                sent=False,
                reason="server_busy" if nth == 0 else "model_busy",
                processed_tokens=None,
            )
        else:
            outcome = types.SimpleNamespace(sent=True, reason="", processed_tokens=7)
        self.outcomes.append(outcome)
        self.primes.append(reason)
        self.primed.set()                        # 先落地才通知,等的那一端讀得到確定的值
        return outcome


def test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction():
    """`/status` 的「prompt cache 預熱」要反映**協調器跑的每一次**預熱,含壓縮後那一次。

    自動壓縮與 `/compact` 換掉歷史之後,協調器自己排一次預熱;那一次的 outcome 沒有回到
    TUI 的話,`/status` 仍顯示 mount 那一次的 `sent` 與舊時間 —— 使用者對著「sent」以為
    cache 是熱的,實際上這一次是 skipped,而且拿不到原因。三個 outcome 刻意可區分
    (sent / server_busy / model_busy),`觸發=` 講得出這一次是哪一種入口。預熱仍然不進
    對話區(只有 `/status` 那幾則含它)、仍在回合鎖外(`busy` 為 False)、不動取消旗標。
    """
    engine = _PrimeOutcomes()
    compactor = types.SimpleNamespace(
        mode="codetrail",
        pending_stop_notice=lambda: "",
        rebind=lambda: None,
        compact=lambda manual=False: types.SimpleNamespace(status="compacted", message="已壓縮"),
    )

    def _landed(app):
        # TUI 手上的 outcome 就是 engine 最近一次回的那一個(只看最後一欄,不綁記錄的形狀)。
        noted = app._last_prime
        return noted is not None and noted[-1] is engine.outcomes[-1]

    async def _status_line(app, pilot):
        app._command("/status")
        await _settle(pilot)
        notice = _snapshot(app)["notices"][-1]
        line = next(l for l in notice.splitlines() if l.startswith("prompt cache 預熱="))
        return notice, line

    async def body():
        app = client_app.CodeTrailApp(engine, compactor=compactor)
        async with app.run_test() as pilot:
            assert await _until(pilot, engine.primed.is_set), "mount 沒有預熱"
            assert await _until(pilot, lambda: _landed(app)), "mount 的 outcome 沒有搬回 UI 執行緒"
            at_mount = await _status_line(app, pilot)

            engine.primed.clear()
            app._command("/compact")
            assert await _until(pilot, engine.primed.is_set), "/compact 之後沒有預熱"
            busy_while_priming = app.coordinator.busy
            await _until(pilot, lambda: _landed(app))     # 未修的產品上不會發生;由 /status 字串判
            after_compact = await _status_line(app, pilot)

            engine.primed.clear()
            assert app.submit("hi") is True
            assert await _until(pilot, engine.primed.is_set), "自動壓縮之後沒有預熱"
            await _until(pilot, lambda: _landed(app))
            after_auto = await _status_line(app, pilot)
            return at_mount, after_compact, after_auto, busy_while_priming, _snapshot(app)

    at_mount, after_compact, after_auto, busy_while_priming, seen = _run(body)
    assert engine.primes == ["mount", "compaction", "compaction"]
    # B04 本體先判:壓縮後那一次的 outcome 必須回到 /status(未修的產品停在 mount 的 sent)。
    assert "skipped(server_busy)" in after_compact[1], after_compact[1]
    assert "skipped(model_busy)" in after_auto[1], after_auto[1]
    # 再判每一次的觸發點與 mount 那一次。
    assert "sent" in at_mount[1] and "觸發=mount" in at_mount[1], at_mount[1]
    assert "觸發=compaction" in after_compact[1], after_compact[1]
    assert "觸發=compaction" in after_auto[1], after_auto[1]
    # 三次 /status 是對話區裡**僅有**含「預熱」的 notice:預熱本身不進對話區。
    assert [note for note in seen["notices"] if "預熱" in note] == [
        at_mount[0], after_compact[0], after_auto[0]
    ]
    assert busy_while_priming is False           # 預熱在回合鎖外
    assert engine.cancelled is False             # 也沒有動到取消旗標


def test_a_resumed_history_is_compacted_before_startup_prefill():
    """A resumed long conversation must not start a nine-minute prime first."""
    engine = _Engine()
    engine.messages = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": "latest question"},
        {"role": "assistant", "content": "latest answer"},
    ]
    order = []
    original_prime = engine.prime_prompt_cache

    def prime(*, reason):
        order.append(("prime", len(engine.messages)))
        return original_prime(reason=reason)

    engine.prime_prompt_cache = prime

    class Compactor:
        mode = "codetrail"

        def compact(self, *, manual=False, **_kwargs):
            assert manual is False
            assert app.coordinator.busy
            order.append(("compact", len(engine.messages)))
            engine.messages = [{"role": "user", "content": "summary", "synthetic": True}]
            return client_compaction.CompactionOutcome("compacted", message="history compacted")

    app = client_app.CodeTrailApp(engine, compactor=Compactor())

    async def body():
        async with app.run_test() as pilot:
            assert await _until(pilot, engine.primed.is_set)
            assert order == [("compact", 4), ("prime", 1)]

    _run(body)


def test_context_display_counts_full_tokens_off_the_ui_thread_and_discards_stale_results():
    engine = _Engine()
    entered = threading.Event()
    release = threading.Event()
    first_done = threading.Event()
    worker_threads = []
    calls = []

    def count(history=None):
        calls.append(engine.session_id)
        worker_threads.append(threading.get_ident())
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
            first_done.set()
            return 123345
        return 4567

    engine.context_tokens = count
    # The old heuristic sees a tiny payload despite a large true token count.
    engine.payload_messages = lambda: ([{"role": "user", "content": "字元估算低估"}], {})
    engine.openai_tools = lambda: []
    app = client_app.CodeTrailApp(engine)

    async def body():
        try:
            async with app.run_test() as pilot:
                assert await _until(pilot, entered.is_set, timeout=1)
                assert worker_threads[0] != threading.get_ident()
                engine.session_id = "20260916T130000-deadbeef"
                engine.messages = [{"role": "user", "content": "different history"}]
                app._recount_context()
                assert await _until(pilot, lambda: app._context_tokens == 4567)
                release.set()
                assert await _until(pilot, first_done.is_set)
                await pilot.pause()
                assert app._context_tokens == 4567, "Late count from the previous history must be discarded"
        finally:
            release.set()

    _run(body)
