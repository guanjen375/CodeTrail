"""client_turns 的回合協調契約。

這些是「engine 自己看不到」的取消狀態:送出之後 worker 還沒進 ``send()``、
worker 阻塞在核准上、取消與回合收尾互相搶跑。三者都真的發生過(原本由 web
的協調器守著),介面只剩 TUI 之後仍然成立,所以搬到這裡。

核准的三條也在這裡:沒回答 = 拒絕、只能回答一次、非 bool 不算核准。

prompt cache 預熱的那三條也在這裡:它**不是**一輪(不進 busy、不被 cancel 認得),
只在壓縮真的換掉歷史之後、放掉回合鎖才排,而且每一次跑完都經 ``on_prime`` 回報。
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import client_engine  # noqa: E402
import client_events  # noqa: E402
import client_turns  # noqa: E402
import context_budget  # noqa: E402

pytestmark = pytest.mark.smoke


# ============================================================
# 替身
# ============================================================

def test_idle_preparation_is_cancellable_and_does_not_widen_automatic_modes(monkeypatch):
    import client_compaction

    for mode, policy in [("manual", "interactive"), ("off", "interactive"), ("codetrail", "readonly")]:
        engine = _Engine()
        engine.messages = [{"role": "user", "content": "old"}]
        engine.options = types.SimpleNamespace(policy=types.SimpleNamespace(name=policy))
        compactor = types.SimpleNamespace(mode=mode, compact=lambda: pytest.fail("unexpected automatic summary"))
        coordinator, _ = _coordinator(engine, compactor=compactor)
        queued = []
        monkeypatch.setattr(coordinator, "_spawn", lambda body, name: queued.append((body, name)))
        coordinator.prepare_idle("mount")
        assert not coordinator.busy
        assert all("prime" in name for _, name in queued)

    engine = _Engine()
    engine.messages = [{"role": "user", "content": "keep verbatim"}]
    engine.options = types.SimpleNamespace(policy=types.SimpleNamespace(name="interactive"))

    def compact():
        assert engine.cancelled, "Cancellation must be armed before the worker enters compaction"
        raise client_events.TurnCancelled("cancelled")

    compactor = types.SimpleNamespace(mode=client_compaction.MODE_CODETRAIL, compact=compact)
    coordinator, recorder = _coordinator(engine, compactor=compactor)
    queued = []
    monkeypatch.setattr(coordinator, "_spawn", lambda body, name: queued.append(body))
    assert coordinator.prepare_idle("mount")
    assert coordinator.busy and coordinator.cancel()
    queued.pop(0)()
    assert not coordinator.busy and not queued and not engine.primes
    assert engine.messages == [{"role": "user", "content": "keep verbatim"}]
    assert client_events.event_part(recorder.events[-1])["reason"] == "cancelled"


class _Result:
    def __init__(self, notices=(), finish=client_events.REASON_STOP):
        self.notices = tuple(notices)
        self.finish = finish


class _Engine:
    """最小的 engine 替身:記下取消旗標,``send()`` 由子類決定。"""

    _next = 0

    def __init__(self, notices=()):
        _Engine._next += 1
        self.session_id = f"20260101T00000{_Engine._next}-abcdef01"
        self.messages: list[dict] = []
        self.sent: list[str] = []
        self.cancelled = False
        self.cleared = 0
        #: 這一輪的答案 / 摘要已經決定寫定。真 engine 之後一律拒絕取消
        #: (``_turn_completed``);替身不模擬這一段的話,「協調器忽略
        #: CancelDecision(False)」這種錯誤永遠測不出來。
        self.committed = False
        #: 預熱的呼叫記錄:每一筆是 ``(reason, 當下的 coordinator.busy)``。預熱不是
        #: 一輪,所以協調器叫它的時候 busy 必須已經是 False(測試自己指派
        #: ``engine.coordinator``;沒指派就記 None)。
        self.coordinator = None
        self.primes: list[tuple[str, bool | None]] = []
        self.priming = False
        self._notices = tuple(notices)

    # 真 engine 的取消介面
    def request_cancel(self, *, arm_when_idle: bool = False):
        if self.committed:
            return client_engine.CancelDecision(False, None)
        self.cancelled = True
        return client_engine.CancelDecision(True, None)

    @staticmethod
    def cancel_pending(call):
        if call is None:
            return False
        call.cancel("cancelled by user")
        return True

    def clear_cancel(self):
        self.cleared += 1
        self.cancelled = False
        self.committed = False

    # §4.2 的預熱入口。真 engine 零寫入、不 raise、不發事件,回一個
    # `PrimeOutcome(sent, reason, processed_tokens)`;協調器只把它原樣交給 on_done,
    # 不看裡面,所以這裡只複述形狀。
    def prime_prompt_cache(self, *, reason: str = ""):
        busy = self.coordinator.busy if self.coordinator is not None else None
        self.primes.append((reason, busy))
        return types.SimpleNamespace(sent=True, reason="", processed_tokens=7)

    def send(self, text, *, on_event=None, on_text=None, on_reasoning=None, approve=None):
        self.sent.append(text)
        if on_event:
            on_event(client_events.text_event(self.session_id, "ok"))
        return _Result(self._notices)


class _Recorder:
    """收集協調器送出的事件。"""

    def __init__(self):
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, event):
        with self._lock:
            self.events.append(dict(event))

    def types(self):
        with self._lock:
            return [event["type"] for event in self.events]

    def wait(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                snapshot = list(self.events)
            if predicate(snapshot):
                return snapshot
            time.sleep(0.005)
        with self._lock:
            raise AssertionError(f"事件沒有出現:{[e['type'] for e in self.events]}")

    def terminal(self, count=1, timeout=5.0):
        return self.wait(
            lambda events: sum(
                1 for e in events if e["type"] == client_events.TYPE_STEP_FINISH
            )
            >= count,
            timeout,
        )


def _coordinator(engine, **kwargs):
    recorder = kwargs.pop("recorder", None) or _Recorder()
    return client_turns.TurnCoordinator(engine, emit=recorder, **kwargs), recorder


def _eventually(predicate, timeout=5.0):
    """等背景執行緒把事情做完。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _joined(name, timeout=5.0):
    """等某條 worker 執行緒真的結束。

    協調器的 ``_spawn`` 不回傳 handle,只能認名字。等它結束才問「有沒有預熱」是
    唯一不靠 sleep 的問法:``_run_turn`` 回來的時候,該排的預熱已經 spawn 出去了。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(t.name == name and t.is_alive() for t in threading.enumerate()):
            return True
        time.sleep(0.005)
    return False


# ============================================================
# 取消:engine 自己看不到的三個狀態
# ============================================================
def test_cancel_wakes_a_turn_waiting_for_approval():
    """等核准的那一輪也要能被中斷:pending 核准原子回成拒絕來喚醒,engine 那端看旗標。"""
    asked = threading.Event()

    class _WaitsForApproval(_Engine):
        def send(self, text, *, on_event=None, on_text=None, on_reasoning=None, approve=None):
            granted = approve(client_engine.ApprovalRequest(self.session_id, "apply_patch", {}))
            if self.cancelled:
                raise client_events.TurnCancelled("user")
            return _Result([f"granted={granted}"])

    engine = _WaitsForApproval()
    tickets: list[client_turns.ApprovalTicket] = []

    def _on_approval(ticket):
        tickets.append(ticket)
        asked.set()

    coordinator, recorder = _coordinator(engine, on_approval=_on_approval)
    coordinator.start_turn("hi")
    assert asked.wait(5)
    assert coordinator.cancel() is True
    events = recorder.terminal()
    assert events[-1]["part"]["reason"] == client_events.REASON_CANCELLED
    # 已經被收掉:事後再回答不能把它翻成核准。
    assert coordinator.answer_approval(tickets[0].approval_id, True) is False


def test_cancel_counts_even_before_the_worker_enters_send():
    """送出之後立刻中斷:worker 還沒進 send(),engine 看不到進行中的東西,但旗標已設。"""
    gate = threading.Event()

    class _Prestart(_Engine):
        def request_cancel(self, *, arm_when_idle=False):
            # engine 那端「沒有進行中的串流或呼叫」,只能靠 arm_when_idle 預先武裝。
            assert arm_when_idle is True
            self.cancelled = True
            gate.set()
            return client_engine.CancelDecision(True, None)

        def send(self, text, *, on_event=None, on_text=None, on_reasoning=None, approve=None):
            gate.wait(5)
            raise client_events.TurnCancelled("armed")

    coordinator, recorder = _coordinator(_Prestart())
    coordinator.start_turn("hi")
    assert coordinator.cancel() is True
    events = recorder.terminal()
    assert events[-1]["part"]["reason"] == client_events.REASON_CANCELLED


def test_a_cancel_arriving_while_the_turn_is_being_started_is_not_lost():
    """取鎖與「這一輪開始」之間的空窗:以前 cancel() 看到上一輪留下的 turn_done=True
    回 False,那一次點擊就漏掉。現在兩步在同一個臨界區。"""
    engine = _Engine()
    coordinator, recorder = _coordinator(engine)
    coordinator.start_turn("warm-up")
    recorder.terminal()

    real_lock = coordinator._lock
    entered = threading.Event()
    proceed = threading.Event()

    class _Probe:
        def __enter__(self):
            real_lock.acquire()
            if any(f.name == "begin_turn" for f in traceback.extract_stack()) and not entered.is_set():
                entered.set()
                proceed.wait(5)          # 讓 cancel() 排在「開始」臨界區後面
            return self

        def __exit__(self, *_exc):
            real_lock.release()
            return False

    coordinator._lock = _Probe()
    results: list[bool] = []
    starter = threading.Thread(target=lambda: coordinator.start_turn("second"))
    starter.start()
    assert entered.wait(5)
    canceller = threading.Thread(target=lambda: results.append(coordinator.cancel()))
    canceller.start()
    time.sleep(0.05)
    proceed.set()
    canceller.join(5)
    starter.join(5)
    assert results == [True]              # 取消命中這一輪,沒有漏掉
    recorder.terminal(count=2)


def test_the_slow_mcp_cancel_does_not_hold_the_coordinator_lock():
    """MCP 取消要等寬限期(最長 10 秒):它必須在協調器的鎖外進行,不然這個對話的
    每一個操作(核准、下一輪、狀態查詢)都跟著卡住。"""
    blocked = threading.Event()
    reached = threading.Event()

    class _PendingCall:
        def cancel(self, _reason):
            reached.set()
            blocked.wait(5)

    class _Split(_Engine):
        def __init__(self):
            super().__init__()
            self.pending = _PendingCall()

        def request_cancel(self, *, arm_when_idle=False):
            self.cancelled = True
            return client_engine.CancelDecision(True, self.pending)

        def send(self, text, *, on_event=None, on_text=None, on_reasoning=None, approve=None):
            blocked.wait(5)
            return _Result()

    coordinator, recorder = _coordinator(_Split())
    coordinator.start_turn("hi")
    canceller = threading.Thread(target=coordinator.cancel)
    canceller.start()
    assert reached.wait(5)
    # 慢速取消正在跑;此時仍拿得到協調器的狀態(鎖沒有被扣住)。
    assert coordinator.busy is True
    assert coordinator.pending_approvals() == ()
    blocked.set()
    canceller.join(5)
    recorder.terminal()


def test_a_cancel_that_races_the_end_of_the_turn_never_poisons_the_next_one():
    """收尾的兩步必須在**同一個**臨界區:先標 turn_done 再清旗標。

    測法是把 worker 停在 `clear_cancel()` 裡,再讓另一條執行緒呼叫 `cancel()`:
    * 正確的收尾握著協調器的鎖,取消排在後面 → 看到「這一輪已結束」→ no-op。
    * 把 `clear_cancel()` 搬到臨界區外(或先清旗標再標 turn_done),取消就擠得進
      那個空窗:它看到 turn_done 還是 False,於是接受並設下 engine 旗標,而清旗標
      的那一步已經跑過了 —— 旗標留到下一題,下一輪一開始就被中斷。
    """
    entered = threading.Event()
    proceed = threading.Event()

    class _PausesOnClear(_Engine):
        def clear_cancel(self):
            entered.set()
            proceed.wait(5)
            super().clear_cancel()

    engine = _PausesOnClear()
    coordinator, recorder = _coordinator(engine)
    coordinator.start_turn("first")
    assert entered.wait(5)
    results: list[bool] = []
    canceller = threading.Thread(target=lambda: results.append(coordinator.cancel()))
    canceller.start()
    time.sleep(0.05)                      # 讓 cancel() 有機會擠進空窗(如果有的話)
    proceed.set()
    canceller.join(5)
    recorder.terminal()
    assert results == [False]             # 這一輪已經結束,取消是 no-op
    assert engine.cancelled is False      # 沒有旗標留給下一題

    coordinator.start_turn("second")
    events = recorder.terminal(count=2)
    assert events[-1]["part"]["reason"] == client_events.REASON_STOP
    assert engine.sent == ["first", "second"]


def test_a_cancel_after_the_answer_is_committed_is_refused():
    """答案已經決定寫定(engine 回 `CancelDecision(False)`):協調器必須照原結果收尾,
    不得回 True、也不得把終結事件翻成 cancelled——那等於「說中斷了但答案還在」。"""
    compacting = threading.Event()
    release = threading.Event()

    class _Committed(_Engine):
        def send(self, text, *, on_event=None, on_text=None, on_reasoning=None, approve=None):
            self.sent.append(text)
            self.committed = True          # 答案寫進歷史
            return _Result()

    class _SlowCompactor(_Compactor):
        def compact(self, *, manual: bool = False):
            self.calls.append(manual)
            compacting.set()
            release.wait(5)
            return _Outcome("skipped", "")

    engine = _Committed()
    coordinator, recorder = _coordinator(engine, compactor=_SlowCompactor())
    coordinator.start_turn("hi")
    assert compacting.wait(5)
    assert coordinator.cancel() is False
    release.set()
    events = recorder.terminal()
    assert events[-1]["part"]["reason"] == client_events.REASON_STOP
    assert coordinator.cancelled is False


def test_a_cancel_during_the_manual_compaction_preflight_is_consumed_not_lost():
    """取消落在「這一輪已開始、worker 還沒進到 compact()」的空窗:engine 預先武裝,
    這次壓縮一開始就中斷。旗標必須被**這一輪**消費掉,不得留給下一題。"""
    armed = threading.Event()
    entered = threading.Event()

    class _PreflightCompactor(_Compactor):
        def __init__(self, engine):
            super().__init__(engine)
            self.engine = engine

        def compact(self, *, manual: bool = False):
            self.calls.append(manual)
            entered.set()
            armed.wait(5)
            if self.engine.cancelled:
                # 真 Compactor 的 early-return 會先過 commit_point,
                # 取消先到就以 TurnCancelled 結束、一個 byte 都不動。
                raise client_events.TurnCancelled("user")
            return _Outcome("skipped", "")

    engine = _Engine()
    compactor = _PreflightCompactor(engine)
    coordinator, recorder = _coordinator(engine, compactor=compactor)
    coordinator.start_compaction()
    assert entered.wait(5)
    assert coordinator.cancel() is True
    armed.set()
    events = recorder.terminal()
    assert events[-1]["part"]["reason"] == client_events.REASON_CANCELLED

    # 下一題必須乾淨:旗標已經在 finish_turn 清掉。
    coordinator.start_turn("next")
    events = recorder.terminal(count=2)
    assert events[-1]["part"]["reason"] == client_events.REASON_STOP
    assert engine.sent == ["next"]


def test_a_cancel_while_idle_is_refused():
    """閒置時的 Ctrl-C 不得顯示成「已中斷」:協調器回 False,UI 才知道要當離開處理。"""
    coordinator, _recorder = _coordinator(_Engine())
    assert coordinator.cancel() is False
    assert coordinator.busy is False


# ============================================================
# 核准
# ============================================================
def test_an_unanswered_approval_is_a_refusal():
    coordinator, _recorder = _coordinator(_Engine(), approval_timeout=0.05)
    request = client_engine.ApprovalRequest("s", "apply_patch", {})
    assert coordinator.request_approval(request) is False


def test_an_unknown_approval_id_is_rejected():
    coordinator, _recorder = _coordinator(_Engine())
    assert coordinator.answer_approval("nope", True) is False


def test_an_approval_can_only_be_answered_once():
    """先 deny 再 grant 不得翻成核准:第一個回答就原子移除。"""
    answers: list[bool] = []
    seen: list[client_turns.ApprovalTicket] = []
    coordinator, _recorder = _coordinator(_Engine(), on_approval=seen.append)
    request = client_engine.ApprovalRequest("s", "apply_patch", {})
    worker = threading.Thread(target=lambda: answers.append(coordinator.request_approval(request)))
    worker.start()
    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        time.sleep(0.005)
    assert seen, "核准框沒有被叫出來"
    approval_id = seen[0].approval_id
    assert coordinator.answer_approval(approval_id, False) is True
    assert coordinator.answer_approval(approval_id, True) is False
    worker.join(5)
    assert answers == [False]


@pytest.mark.parametrize("granted", ["true", "false", 1, 0, None, "yes"])
def test_a_non_boolean_granted_is_never_an_approval(granted):
    """``bool("false")`` 是 True。只認真的 bool。"""
    seen: list[client_turns.ApprovalTicket] = []
    coordinator, _recorder = _coordinator(_Engine(), on_approval=seen.append)
    request = client_engine.ApprovalRequest("s", "run_command", {})
    answers: list[bool] = []
    worker = threading.Thread(target=lambda: answers.append(coordinator.request_approval(request)))
    worker.start()
    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        time.sleep(0.005)
    assert seen
    assert coordinator.answer_approval(seen[0].approval_id, granted) is False
    assert coordinator.answer_approval(seen[0].approval_id, False) is True
    worker.join(5)
    assert answers == [False]


def test_an_approval_registered_after_the_cancel_is_refused_immediately():
    """取消落在「engine 決定要問」與「登記 pending」之間:cancel() 沒看到這筆,
    登記端要自己回拒絕,不能等滿逾時。"""
    started = threading.Event()
    release = threading.Event()
    asked: list[bool] = []

    class _AsksLate(_Engine):
        def send(self, text, *, on_event=None, on_text=None, on_reasoning=None, approve=None):
            started.set()
            release.wait(5)
            asked.append(approve(client_engine.ApprovalRequest(self.session_id, "apply_patch", {})))
            raise client_events.TurnCancelled("user")

    coordinator, recorder = _coordinator(_AsksLate(), approval_timeout=30)
    coordinator.start_turn("hi")
    assert started.wait(5)
    assert coordinator.cancel() is True
    release.set()
    recorder.terminal()
    assert asked == [False]


# ============================================================
# notice / 終結事件 / 壓縮
# ============================================================
class _Compactor:
    def __init__(self, engine=None, outcome=None, notice=""):
        self.engine = engine
        self.mode = "codetrail"
        self.calls: list[bool] = []
        self.rebinds = 0
        self.notices = 0
        self._outcome = outcome
        self._notice = notice

    def pending_stop_notice(self):
        self.notices += 1
        return self._notice

    def rebind(self):
        self.rebinds += 1

    def compact(self, *, manual: bool = False):
        self.calls.append(manual)
        return self._outcome or _Outcome("skipped", "")


class _Outcome:
    def __init__(self, status, message):
        self.status = status
        self.message = message


def test_notices_are_delivered_before_the_terminal_event():
    """ingest 待辦 / 假工具呼叫的提示要在終結事件之前;反過來的話收工的一端會先離開。"""
    coordinator, recorder = _coordinator(_Engine(notices=("待辦事項",)))
    coordinator.start_turn("hi")
    events = recorder.terminal()
    kinds = [event["type"] for event in events]
    assert kinds.index(client_events.TYPE_NOTICE) < kinds.index(client_events.TYPE_STEP_FINISH)


def test_a_turn_failure_still_sends_a_terminal_event():
    """只送 error 的話,等終結事件的一端會永遠停在那裡。"""
    class _Boom(_Engine):
        def send(self, *_a, **_k):
            raise RuntimeError("boom")

    coordinator, recorder = _coordinator(_Boom())
    coordinator.start_turn("hi")
    events = recorder.terminal()
    assert events[-2]["type"] == client_events.TYPE_ERROR
    assert events[-1]["part"]["reason"] == client_events.REASON_ERROR


def test_two_concurrent_turns_are_refused():
    """同一個對話同時跑兩輪會把歷史交錯:模型鎖只序列化 HTTP 呼叫,保護不到 session 狀態。"""
    gate = threading.Event()

    class _Blocks(_Engine):
        def send(self, *_a, **_k):
            gate.wait(5)
            return _Result()

    coordinator, recorder = _coordinator(_Blocks())
    coordinator.start_turn("first")
    with pytest.raises(client_turns.TurnCoordinator.Busy):
        coordinator.start_turn("second")
    gate.set()
    recorder.terminal()


def test_the_durable_stop_notice_is_published_before_the_turn_starts():
    """先 send 再取警告的話,模型可能已經撞了 context gate,使用者不知道壓縮早就停了。"""
    compactor = _Compactor(notice="這個 session 的自動壓縮已停用")
    coordinator, recorder = _coordinator(_Engine(), compactor=compactor)
    assert coordinator.start_turn("hi") == "這個 session 的自動壓縮已停用"
    events = recorder.terminal()
    assert events[0]["type"] == client_events.TYPE_NOTICE
    assert events[0]["message"] == "這個 session 的自動壓縮已停用"


def test_auto_compaction_only_runs_after_a_completed_answer():
    """截斷(length)/ 出錯 / 中斷的那一輪沒有可信的切點。"""
    class _Truncated(_Engine):
        def send(self, *_a, **_k):
            return _Result(finish="length")

    compactor = _Compactor()
    coordinator, recorder = _coordinator(_Truncated(), compactor=compactor)
    coordinator.start_turn("hi")
    recorder.terminal()
    assert compactor.calls == []


def test_a_context_gate_refusal_can_recover_old_history_without_retrying_tools():
    class Overflows(_Engine):
        def send(self, text, **_kwargs):
            self.sent.append(text)
            raise context_budget.ContextOverflowError(context_budget.ContextUsage(
                effective_num_ctx=131072, estimated_input_tokens=123345,
                reserved_output_tokens=8192, hard_overflow=True,
            ))

    engine = Overflows()
    compactor = _Compactor(outcome=_Outcome("compacted", "歷史已壓縮"))
    compactor.mode = "codetrail"
    coordinator, recorder = _coordinator(engine, compactor=compactor)
    coordinator.start_turn("pending question")
    events = recorder.terminal()
    assert compactor.calls == [False], "Gate refusal must not make automatic recovery unreachable"
    assert engine.sent == ["pending question"], "Do not silently rerun a partially executed tool loop"
    assert any("歷史已壓縮" in e.get("message", "") for e in events)
    assert client_events.event_part(events[-1])["reason"] == client_events.REASON_ERROR


def test_auto_compaction_runs_after_a_completed_answer():
    compactor = _Compactor(outcome=_Outcome("compacted", "已壓縮"))
    coordinator, recorder = _coordinator(_Engine(), compactor=compactor)
    coordinator.start_turn("hi")
    events = recorder.terminal()
    assert compactor.calls == [False]
    assert any(e.get("message") == "已壓縮" for e in events)


@pytest.mark.parametrize("manual", [False, True])
def test_compaction_activity_reaches_the_ui_before_the_terminal_event(manual):
    """手動與答案後的摘要進度都必須到 UI,且不能冒充答案或提早結束回合。"""
    class _ActivityEngine(_Engine):
        activity_callback = None

        def set_activity_callback(self, callback):
            self.activity_callback = callback

    engine = _ActivityEngine()
    activity = {
        "type": "activity",
        "sessionID": engine.session_id,
        "part": {
            "operation": "compact", "phase": "prompt_processing", "percent": 92,
        },
    }

    class _ProgressCompactor(_Compactor):
        def compact(self, *, manual=False):
            self.calls.append(manual)
            if engine.activity_callback is not None:
                engine.activity_callback(activity)
            return _Outcome("skipped", "沒有更動歷史")

    compactor = _ProgressCompactor()
    coordinator, recorder = _coordinator(engine, compactor=compactor)
    if manual:
        coordinator.start_compaction()
    else:
        coordinator.start_turn("hi")
    events = recorder.terminal()
    assert activity in events, "摘要的 92% 進度沒有從 engine 到達 UI"
    assert compactor.calls == [manual]
    assert events.index(activity) < len(events) - 1
    assert not client_events.is_terminal_event(activity)
    assert client_events.event_generated_text(activity) == ""
    assert client_events.completed_tool_call(activity) is None
    assert [client_events.event_generated_text(event) for event in events
            if client_events.event_generated_text(event)] == ([] if manual else ["ok"])


def test_a_cancel_accepted_during_compaction_ends_with_a_cancelled_terminal():
    """答案已經給了,但這一輪的結果是「中斷」——cancel 回了 True,終結事件就必須是 cancelled。"""
    compacting = threading.Event()
    release = threading.Event()

    class _SlowCompactor(_Compactor):
        def compact(self, *, manual: bool = False):
            self.calls.append(manual)
            compacting.set()
            release.wait(5)
            return _Outcome("skipped", "")

    compactor = _SlowCompactor()
    coordinator, recorder = _coordinator(_Engine(), compactor=compactor)
    coordinator.start_turn("hi")
    assert compacting.wait(5)
    assert coordinator.cancel() is True
    release.set()
    events = recorder.terminal()
    assert events[-1]["part"]["reason"] == client_events.REASON_CANCELLED


def test_a_manual_compaction_is_a_turn_and_can_be_cancelled():
    """`/compact` 也要能中斷:長摘要不是「按了沒反應」。"""
    compacting = threading.Event()
    release = threading.Event()

    class _SlowCompactor(_Compactor):
        def compact(self, *, manual: bool = False):
            self.calls.append(manual)
            compacting.set()
            release.wait(5)
            return _Outcome("skipped", "壓縮被中斷,對話維持原狀。")

    compactor = _SlowCompactor()
    coordinator, recorder = _coordinator(_Engine(), compactor=compactor)
    coordinator.start_compaction()
    assert compacting.wait(5)
    assert coordinator.cancel() is True
    release.set()
    events = recorder.terminal()
    assert compactor.calls == [True]
    assert events[-1]["part"]["reason"] == client_events.REASON_CANCELLED


def test_a_session_change_rebinds_the_compactor_without_consuming_the_notice():
    """`/new` / `/resume` 只重綁;「每個 session 只講一次」的警告留給送出當下。"""
    compactor = _Compactor(notice="已停用")
    coordinator, _recorder = _coordinator(_Engine(), compactor=compactor)
    coordinator.session_changed()
    assert compactor.rebinds == 1 and compactor.notices == 0


# ============================================================
# prompt cache 預熱
# ============================================================
def test_a_compaction_that_replaced_the_history_primes_after_the_turn_lock_is_released():
    """壓縮換掉歷史之後要重新預熱,而且只能在**放掉回合鎖之後**排。

    壓縮之後,下一輪要送的 prefix 已經不是剛剛送過的那一份 —— 那正是 server 端的
    prompt cache 會落空的時刻。反過來,`skipped` / `stopped` / `failed` 沒有換掉
    任何東西,預熱只是白打一次主模型(而且會佔著模型鎖)。

    排在 `finally` 裡(回合鎖還握著)的話,預熱會在這一輪的臨界區內等模型鎖:
    使用者的下一題連 `start_turn` 都排不進來,畫面上只看得到「這一輪還在跑」。
    記下的 busy 就是這件事的證據。
    """

    def _primes_after(status, *, manual):
        # 四種只差在 status(訊息刻意一樣):判準是「歷史有沒有被換掉」,不是
        # 壓縮器講了什麼。
        engine = _Engine()
        coordinator, recorder = _coordinator(
            engine, compactor=_Compactor(outcome=_Outcome(status, "已壓縮"))
        )
        engine.coordinator = coordinator
        if manual:
            coordinator.start_compaction()
            worker = f"codetrail-compact-{engine.session_id}"
        else:
            coordinator.start_turn("hi")
            worker = f"codetrail-turn-{engine.session_id}"
        recorder.terminal()
        assert _joined(worker), f"{worker} 沒有結束"
        if status == "compacted":
            assert _eventually(lambda: len(engine.primes) == 1), engine.primes
        return list(engine.primes)

    for manual in (False, True):
        assert _primes_after("compacted", manual=manual) == [("compaction", False)]
        for status in ("skipped", "stopped", "failed"):
            assert _primes_after(status, manual=manual) == [], (status, manual)


def test_priming_is_invisible_to_busy_and_cancel_and_refused_while_a_turn_runs():
    """預熱不是一輪。

    讓它算進 `busy` 的話,使用者的下一題與 `/new` 會被自己的預熱擋住;讓 `cancel()`
    認得它的話,閒置時按 Ctrl-C 會顯示成「已中斷這一輪」——那是謊報,而且真的
    engine 那端根本沒有回合可以中斷(預熱的中止是 `abort_prime()`,不是這裡)。
    反過來,回合進行中不得再排一個:那一輪握著模型鎖,預熱只會多一條在鎖上等的
    執行緒,而且它算出來的 prefix 不含這一輪還沒寫定的內容。
    """
    engine = _Engine()
    coordinator, _recorder = _coordinator(engine)
    engine.coordinator = coordinator

    assert coordinator.prime_in_background("mount") is True
    assert _eventually(lambda: engine.primes == [("mount", False)]), engine.primes
    assert coordinator.busy is False
    assert coordinator.cancelled is False
    assert coordinator.cancel() is False          # 沒有回合可中斷
    assert engine.cancelled is False              # engine 的取消旗標也沒有被動到

    entered = threading.Event()
    release = threading.Event()

    class _Blocks(_Engine):
        def send(self, *_a, **_k):
            entered.set()
            release.wait(5)
            return _Result()

    busy_engine = _Blocks()
    busy_coordinator, busy_recorder = _coordinator(busy_engine)
    busy_engine.coordinator = busy_coordinator
    busy_coordinator.start_turn("hi")
    assert entered.wait(5)
    assert busy_coordinator.prime_in_background("mount") is False
    assert busy_engine.primes == []               # 連 spawn 都沒有
    release.set()
    busy_recorder.terminal()

    class _Older:
        """還沒有預熱能力的 engine。少了那個方法是**跳過**,不是例外。"""

        session_id = "20260101T000000-00000000"

    older, _older_recorder = _coordinator(_Older())
    assert older.prime_in_background("mount") is False


def test_every_prime_the_coordinator_runs_reports_through_on_prime(monkeypatch):
    """協調器**每一次**真的跑完的預熱都要經建構時給的 `on_prime(reason, outcome)` 回報。

    壓縮換掉歷史之後那一次是協調器自己排的,沒有每次呼叫的 `on_done`;少了這條回呼,
    那一次的 outcome 直接被丟掉,`/status` 停在 mount 那一次的 `sent`(Astra R1-B04)。
    順序是先 `on_prime` 再 `on_done`;engine 破了「不 raise」的契約時 outcome 是 None
    (不是不叫);`on_prime` 自己炸掉不得帶走 `on_done`、也不得從預熱執行緒冒出來
    (UI 收尾時搬運會以 CancelledError 這種 BaseException 結束)。
    """
    seen: list[tuple] = []
    engine = _Engine()
    coordinator, recorder = _coordinator(
        engine,
        compactor=_Compactor(outcome=_Outcome("compacted", "已壓縮")),
        on_prime=lambda reason, outcome: seen.append(("prime", reason, outcome, coordinator.busy)),
    )
    engine.coordinator = coordinator

    # 閒置時由 UI 排的那一次:on_prime 與 on_done 都到、on_prime 在前、拿到同一個 outcome。
    assert coordinator.prime_in_background(
        "mount", on_done=lambda outcome: seen.append(("done", outcome))
    ) is True
    assert _eventually(lambda: len(seen) == 2), seen
    assert seen[0][:2] == ("prime", "mount") and seen[0][2].sent is True and seen[0][3] is False
    assert seen[1] == ("done", seen[0][2])

    # 協調器自己排的那一次(壓縮換掉歷史之後):沒有 on_done,照樣回到 on_prime,而且回合鎖已放。
    seen.clear()
    coordinator.start_compaction()
    recorder.terminal()
    assert _joined(f"codetrail-compact-{engine.session_id}")
    assert _eventually(lambda: len(seen) == 1), seen
    assert seen[0][:2] == ("prime", "compaction") and seen[0][2].sent is True
    assert seen[0][3] is False and coordinator.busy is False
    assert engine.primes == [("mount", False), ("compaction", False)]

    # engine 破了「不 raise」的契約:outcome 是 None,不是不叫。
    class _Raises(_Engine):
        def prime_prompt_cache(self, *, reason: str = ""):
            raise RuntimeError("boom")

    reported: list[tuple] = []
    raising, _raising_recorder = _coordinator(
        _Raises(), on_prime=lambda reason, outcome: reported.append((reason, outcome))
    )
    assert raising.prime_in_background("mount") is True
    assert _eventually(lambda: reported == [("mount", None)]), reported

    # on_prime 自己炸掉(連 BaseException 都算):執行緒正常結束、on_done 照樣被叫、不冒泡。
    class _Boom(BaseException):
        pass

    def _explodes(reason, _outcome):
        raise _Boom(reason)

    escaped: list = []
    monkeypatch.setattr(threading, "excepthook", lambda args: escaped.append(args))
    done: list = []
    exploding_engine = _Engine()
    exploding, _exploding_recorder = _coordinator(exploding_engine, on_prime=_explodes)
    assert exploding.prime_in_background("mount", on_done=done.append) is True
    assert _joined(f"codetrail-prime-{exploding_engine.session_id}")
    assert len(done) == 1 and done[0].sent is True
    assert escaped == []
