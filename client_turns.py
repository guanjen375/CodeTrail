#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_turns — 一個對話的回合協調器(不含任何 UI / HTTP)。

`Engine` 自己只認得「進行中的這一輪」。真正要讓使用者按得下「中斷」,還需要
三件 engine 看不到的事:

* **回合鎖**:同一個對話一次只跑一輪。模型鎖只序列化 HTTP 呼叫,保護不到
  session 狀態(``send()`` 先改 messages、payload 也在模型鎖之前組好)。
* **閒置武裝**:送出之後、worker 還沒進到 ``engine.send()`` 之前的那段空窗。
  這時 engine 看不到任何進行中的東西,``cancel()`` 會回 False,那一次點擊
  就整個漏掉;要走 ``request_cancel(arm_when_idle=True)`` 讓 engine 預先武裝。
* **等待核准中的取消**:worker 阻塞在核准上,沒有串流也沒有 MCP 呼叫可以關。
  只能由這裡把 pending 核准**原子地**回成拒絕並喚醒 worker,engine 醒來看
  旗標丟 ``TurnCancelled``(而且不會把它記成一筆 denied)。

這三件事原本只有 web 的協調器做齊。前端只剩一個 TUI 之後它們仍然要成立,
所以搬到這裡:UI 只負責顯示與收鍵,回合語意在這個模組。

**取消的兩層語意**(呼叫端要分清楚):

* 核准框的「拒絕」= :meth:`answer_approval` 帶 ``False``:只拒絕**這一個工具**,
  這一輪繼續(模型會拿到 denied 的工具結果再想別的辦法)。
* 中斷 = :meth:`cancel`:整輪結束,答案不寫進歷史,懸空的 tool_call 由
  ``run_tool_loop`` 補上「已中斷」結果。

**預熱不是一輪**(:meth:`prime_in_background`):它不取回合鎖、不動 ``_turn_done``
與 ``_cancelled``,所以 ``busy`` 不會因為它變成 True(那會擋掉使用者的下一題與
``/new``),``cancel()`` 對它也是 no-op ——閒置時回 True 就是把「已中斷」顯示給一個
根本沒有在跑的回合。真正的准入(policy、模型鎖、server 忙不忙)全在 engine 那端。
每一次真的跑完的預熱都經建構時給的 ``on_prime(reason, outcome)`` 回到 UI,**不分是誰
排的**:壓縮換掉歷史之後那一次是這裡自己排的、沒有每次呼叫的 ``on_done``,少了這條
回呼那一次的結果就直接被丟掉,``/status`` 會停在上一次的 ``sent``。
"""
from __future__ import annotations

import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any

import client_engine
import client_compaction
import context_budget
import client_events

#: 核准等多久沒人回答就當拒絕。UI 掛掉 / 使用者離開時 worker 不能永遠卡著。
APPROVAL_TIMEOUT_SECONDS = 300
MAX_QUEUE_ITEMS = 32
MAX_QUEUE_TEXT_BYTES = 64 * 1024
MAX_QUEUE_BYTES = 256 * 1024
PENDING_QUEUE_STATES = frozenset({"waiting", "deferred", "delivering"})


class QueueError(ValueError):
    """The local input was not accepted; keep its editable draft."""


@dataclass(frozen=True)
class QueuedMessage:
    id: str
    text: str
    mode: str
    session_id: str
    turn_id: str
    status: str = "waiting"
    reason: str = ""
    revision: int = 1
    delivered_turn_id: str = ""


@dataclass
class ApprovalTicket:
    """一次待回答的核准。``approval_id`` 是回答時的鑰匙(見 :meth:`TurnCoordinator.answer_approval`)。"""

    approval_id: str
    request: client_engine.ApprovalRequest
    event: threading.Event = field(default_factory=threading.Event)
    granted: bool = False


class TurnCoordinator:
    """把一輪對話跑在背景執行緒,並提供可中斷的回合邊界。

    ``emit`` 收到的是 :mod:`client_events` 形狀的事件(含串流 ``text_delta``、
    工具事件、notice 與終結的 ``step_finish``)。回合事件在背景執行緒,
    佇列操作的收據也可能來自 UI 執行緒;UI bridge 負責辨識並搬運。
    """

    class Busy(RuntimeError):
        """這個對話已經有一輪在跑。"""

    def __init__(
        self,
        engine: client_engine.Engine,
        *,
        emit: Callable[[dict[str, Any]], None],
        on_approval: Callable[[ApprovalTicket], None] | None = None,
        on_approval_closed: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        compactor: Any = None,
        approval_timeout: float = APPROVAL_TIMEOUT_SECONDS,
        on_prime: Callable[[str, Any], None] | None = None,
    ) -> None:
        self.engine = engine
        self.compactor = compactor
        self._emit = emit
        self._on_approval = on_approval
        self._on_approval_closed = on_approval_closed
        self._on_reasoning = on_reasoning
        self._approval_timeout = approval_timeout
        #: 每一次真的跑完的預熱都回報到這裡:``on_prime(reason, outcome)``,不分是誰排的
        #: (UI 的 mount / new / session,或壓縮換掉歷史之後這裡自己排的那一次)。在預熱
        #: 那條背景執行緒上呼叫;engine raise 時 outcome 是 None。
        self._on_prime = on_prime
        #: 保護 turn_lock 的取得、``_turn_done``、``_cancelled`` 與 pending 核准表。
        #: 慢速動作(MCP 取消要等寬限期,最長 10 秒)一律在鎖外做。
        self._lock = threading.Lock()
        self._turn_lock = threading.Lock()
        #: 這一輪(含尾端的壓縮)已經結束、只是 turn_lock 還沒放:此時的取消是
        #: no-op,不然旗標會留到下一題。
        self._turn_done = True
        #: cancel() 設、begin_turn() 清。``request_approval`` 登記完 pending 之後要
        #: 看它,否則取消落在「ASK 判定後、pending 登記前」的空窗就會等滿逾時。
        self._cancelled = False
        self._approvals: dict[str, ApprovalTicket] = {}
        self._queue: list[QueuedMessage] = []
        self._queue_sequence = 0
        self._queue_paused = False
        self._turn_id = ""
        self._accepting_supplements = False
        self._active_queue_id = ""
        # 摘要不經 send(on_event=...),但手動／自動壓縮都要回報目前階段。
        # 活動共用 UI bridge;不改 send 的 JSONL 事件流,也不進預熱路徑。
        set_activity = getattr(engine, "set_activity_callback", None)
        if callable(set_activity):
            set_activity(self._publish)
        set_supplements = getattr(engine, "set_supplement_callback", None)
        if callable(set_supplements):
            set_supplements(self._deliver_supplements)

    # ---- 狀態 ----------------------------------------------------------
    @property
    def busy(self) -> bool:
        with self._lock:
            return self._turn_lock.locked() and not self._turn_done

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def pending_approvals(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._approvals)

    @property
    def turn_id(self) -> str:
        with self._lock:
            return self._turn_id

    @property
    def queue_paused(self) -> bool:
        with self._lock:
            return self._queue_paused

    def queue_snapshot(self, *, pending_only: bool = False) -> tuple[QueuedMessage, ...]:
        """Immutable local view. Inspection cannot start a turn or contact a model."""
        with self._lock:
            return tuple(item for item in self._queue
                         if item.session_id == self.engine.session_id
                         and (not pending_only or item.status in PENDING_QUEUE_STATES))

    def assert_session_change_allowed(self) -> None:
        with self._lock:
            if self._turn_lock.locked() or self._approvals:
                raise self.Busy(self.engine.session_id)
            if any(item.status in PENDING_QUEUE_STATES for item in self._queue):
                raise QueueError("還有未送訊息;先用 /queue list 查看、/queue resume 繼續或 /queue cancel <id> 取消。")

    @staticmethod
    def _validate_queue_text(text: str) -> None:
        if not isinstance(text, str) or not text.strip():
            raise QueueError("訊息不能為空。")
        if len(text.encode("utf-8")) > MAX_QUEUE_TEXT_BYTES:
            raise QueueError(f"單則排隊訊息不可超過 {MAX_QUEUE_TEXT_BYTES} bytes。")

    def _queue_event(self, item: QueuedMessage) -> None:
        self._publish(client_events.queue_event(item.session_id, asdict(item)))

    def _replace_queued_locked(self, item: QueuedMessage, **changes: Any) -> QueuedMessage:
        updated = replace(item, revision=item.revision + 1, **changes)
        self._queue[self._queue.index(item)] = updated
        return updated

    def enqueue(
        self, text: str, *, mode: str = "queue", session_id: str | None = None,
        turn_id: str | None = None,
    ) -> QueuedMessage:
        """Accept an explicit choice without interrupting HTTP, tools or approval.

        A modal may return after the original turn ended. Its captured turn ID
        prevents a supplement from silently steering a later queued task.
        """
        self._validate_queue_text(text)
        if mode not in ("queue", "supplement"):
            raise QueueError("mode 必須是 queue 或 supplement。")
        with self._lock:
            target = self.engine.session_id
            if session_id is not None and session_id != target:
                raise QueueError("對話已切換,訊息未送出;請在原對話重新確認。")
            pending = [item for item in self._queue if item.status in PENDING_QUEUE_STATES]
            size = sum(len(item.text.encode("utf-8")) for item in pending)
            if len(pending) >= MAX_QUEUE_ITEMS or size + len(text.encode("utf-8")) > MAX_QUEUE_BYTES:
                raise QueueError("待送佇列已滿;先取消或送出其中的訊息。")
            # Retain a bounded local receipt history, evicting only settled items.
            while (len(self._queue) >= MAX_QUEUE_ITEMS or
                   sum(len(item.text.encode("utf-8")) for item in self._queue)
                   + len(text.encode("utf-8")) > MAX_QUEUE_BYTES):
                settled = next(item for item in self._queue if item.status not in PENDING_QUEUE_STATES)
                self._queue.remove(settled)
            active = self._turn_lock.locked() and not self._turn_done
            bound_turn = self._turn_id if turn_id is None else turn_id
            can_supplement = (active and self._accepting_supplements and not self._cancelled
                              and bound_turn == self._turn_id
                              and callable(getattr(self.engine, "record_supplement", None)))
            status = "deferred" if mode == "supplement" and not can_supplement else "waiting"
            reason = "本輪已無安全送入點,保留到下一輪。" if status == "deferred" else ""
            if not active:
                self._queue_paused = True
                reason = "待下一輪;使用 /queue resume 開始。"
            self._queue_sequence += 1
            item = QueuedMessage(f"q{self._queue_sequence:06d}", text, mode, target,
                                 bound_turn, status=status, reason=reason)
            self._queue.append(item)
        self._queue_event(item)
        return item

    def edit_queued(self, message_id: str, text: str) -> QueuedMessage:
        self._validate_queue_text(text)
        with self._lock:
            item = next((item for item in self._queue if item.id == message_id), None)
            if item is None or item.status not in ("waiting", "deferred"):
                raise QueueError("只有 waiting/deferred 訊息可以修改。")
            size = sum(len(entry.text.encode("utf-8")) for entry in self._queue if entry != item)
            if size + len(text.encode("utf-8")) > MAX_QUEUE_BYTES:
                raise QueueError("修改後超過佇列大小上限。")
            item = self._replace_queued_locked(item, text=text)
        self._queue_event(item)
        return item

    def cancel_queued(self, message_id: str) -> QueuedMessage:
        with self._lock:
            item = next((item for item in self._queue if item.id == message_id), None)
            if item is None or item.status not in ("waiting", "deferred"):
                raise QueueError("訊息已送達或正在接收,不能取消;Ctrl-C 可中斷目前回合。")
            item = self._replace_queued_locked(item, status="cancelled", reason="使用者取消待送訊息。")
        self._queue_event(item)
        return item

    def _deliver_supplements(self) -> None:
        # Snapshot this boundary's batch. Inputs arriving while fsync/UI callbacks
        # run wait for another model boundary; an endless producer cannot stall it.
        with self._lock:
            if self._cancelled or not self._accepting_supplements:
                return
            ids = [item.id for item in self._queue
                   if item.mode == "supplement" and item.status == "waiting"
                   and item.session_id == self.engine.session_id and item.turn_id == self._turn_id]
        for message_id in ids:
            with self._lock:
                item = next((entry for entry in self._queue if entry.id == message_id), None)
                if self._cancelled or not self._accepting_supplements:
                    return
                if item is None or item.status != "waiting":
                    continue
                item = self._replace_queued_locked(item, status="delivering")
            accepted = self.engine.record_supplement(item.text, queue_id=item.id)
            with self._lock:
                current = next(entry for entry in self._queue if entry.id == item.id)
                item = self._replace_queued_locked(
                    current, status="delivered" if accepted else "deferred",
                    delivered_turn_id=self._turn_id if accepted else "",
                    reason="已納入本輪歷史。" if accepted else "本輪未接收,保留到下一輪。",
                )
            self._queue_event(item)
            if not accepted:
                return

    def _close_supplements(self) -> None:
        with self._lock:
            self._accepting_supplements = False
            changed = []
            for item in list(self._queue):
                if (item.mode == "supplement" and item.status in ("waiting", "delivering")
                        and item.turn_id == self._turn_id):
                    changed.append(self._replace_queued_locked(
                        item, status="deferred", reason="未納入本輪,保留到下一輪。"))
        for item in changed:
            self._queue_event(item)

    def _pause_queue(self) -> None:
        with self._lock:
            self._queue_paused = True
        if self.queue_snapshot(pending_only=True):
            self._publish(client_events.notice_event(
                self.engine.session_id, "本輪未成功收尾;待送訊息已保留並暫停。用 /queue resume 明示繼續。"))

    def _queue_user_recorded(self, message_id: str) -> None:
        with self._lock:
            item = next(item for item in self._queue if item.id == message_id)
            item = self._replace_queued_locked(item, status="delivered", reason="已納入下一輪歷史。",
                                               delivered_turn_id=self._turn_id)
        self._queue_event(item)

    def resume_queue(self) -> bool:
        """Explicitly resume retained inputs; inspecting/editing never resumes."""
        with self._lock:
            if self._turn_lock.locked():
                raise self.Busy(self.engine.session_id)
            self._queue_paused = False
        return self._start_next_queued(self.engine.session_id)

    def _start_next_queued(self, target: str) -> bool:
        with self._lock:
            if self._queue_paused or self._turn_lock.locked() or self.engine.session_id != target:
                return False
            item = next((item for item in self._queue
                         if item.session_id == target and item.status in ("waiting", "deferred")), None)
            if item is None:
                return False
            self._turn_lock.acquire()
            self._cancelled = False
            self._turn_done = False
            self._turn_id = secrets.token_hex(8)
            self._accepting_supplements = True
            self._active_queue_id = item.id
            item = self._replace_queued_locked(item, status="delivering", reason="正在接收至下一輪。")
        self._queue_event(item)
        try:
            notice = self._stop_notice()
            if notice:
                self._publish(client_events.notice_event(target, notice))
            self._spawn(lambda: self._run_turn(item.text, target), f"codetrail-turn-{target}")
        except Exception as exc:
            self._restore_unsent_queue_item()
            self._pause_queue()
            self.finish_turn()
            self._publish(client_events.error_event(target, f"待送回合無法啟動:{type(exc).__name__}: {exc}"))
            self._publish(client_events.step_finish_event(target, reason=client_events.REASON_ERROR))
            return False
        return True

    def _restore_unsent_queue_item(self) -> None:
        with self._lock:
            item = next((item for item in self._queue if item.id == self._active_queue_id), None)
            if item is None or item.status != "delivering":
                return
            item = self._replace_queued_locked(item, status="deferred", reason="回合未接收,保留待送。")
        self._queue_event(item)

    # ---- 回合邊界 ------------------------------------------------------
    def begin_turn(self) -> None:
        """取 turn_lock 並標「這一輪開始」——兩步在同一個臨界區內。

        兩步之間收到取消的話,:meth:`cancel` 看到的是上一輪留下的
        ``_turn_done=True``,回 False,那一次點擊就整個漏掉。
        """
        with self._lock:
            if self._turn_lock.locked():
                raise self.Busy(self.engine.session_id)
            if any(item.status in PENDING_QUEUE_STATES for item in self._queue):
                raise QueueError("還有待送訊息;使用 /queue resume、/queue add 或 /queue cancel。")
            if not self._turn_lock.acquire(blocking=False):
                raise self.Busy(self.engine.session_id)
            self._cancelled = False
            self._turn_done = False
            self._turn_id = secrets.token_hex(8)
            self._active_queue_id = ""
            self._queue_paused = False
            self._accepting_supplements = False

    def finish_turn(self) -> None:
        """一輪(訊息或手動摘要)結束:標 turn_done、清 engine 旗標、放鎖。

        前兩步在同一個臨界區,跟 :meth:`cancel` 的「判定 + 設旗標」互斥:先標
        「這一輪結束」再清旗標,取消看到 turn_done 就是 no-op,之前來不及消費的
        旗標在這裡清掉——兩邊合起來,取消不會留到下一題。
        """
        with self._lock:
            self._turn_done = True
            self._accepting_supplements = False
            self._active_queue_id = ""
            clear = getattr(self.engine, "clear_cancel", None)
            if callable(clear):
                clear()
        self._turn_lock.release()

    def cancel(self, *, block: bool = True) -> bool:
        """中斷進行中的那一輪。沒有在跑就回 ``False``。

        接不接受由 engine 的 ``request_cancel(arm_when_idle=True)`` **原子**決定:
        進行中且還沒決定寫定 → True;答案 / 摘要已寫定、``send()`` 已退出 → False
        (沒有東西可取消,這一輪照原結果收尾);worker 還沒進 ``engine.send()``
        → engine 預先武裝,這一輪一開始就會被中斷,True。

        鎖裡只做快速部分(旗標 + 關串流 + 收掉 pending 核准);MCP 取消要等寬限期,
        放在鎖裡會把這個對話的所有操作一起卡住。

        ``block=False``:連鎖外的 MCP 取消也丟到背景執行緒。UI 執行緒要用這個
        —— 那條路最長會等寬限期(10 秒)加 SIGTERM 再加重新 spawn,同步跑在
        Textual 的 event loop 上就是整個畫面凍住,而且 worker 的
        ``call_from_thread`` 也跟著卡在後面。
        """
        with self._lock:
            if not self._turn_lock.locked() or self._turn_done:
                return False
            pending_call = None
            slow_cancel = None
            request = getattr(self.engine, "request_cancel", None)
            if callable(request):
                decision = request(arm_when_idle=True)
                if not decision.accepted:
                    return False
                pending_call = decision.call
                slow_cancel = getattr(self.engine, "cancel_pending", None)
            else:  # pragma: no cover - 只有測試替身會少這個方法
                fallback = getattr(self.engine, "cancel", None)
                if callable(fallback):
                    fallback()
            self._cancelled = True
            self._queue_paused = True
            waiting = list(self._approvals.items())
            self._approvals.clear()
            for _approval_id, ticket in waiting:
                ticket.granted = False
        for _approval_id, ticket in waiting:
            ticket.event.set()
        if self._on_approval_closed is not None:
            for approval_id, _ticket in waiting:
                self._on_approval_closed(approval_id)
        if callable(slow_cancel):
            # 鎖外:同一個 pending call 物件只屬於這一輪,晚一點取消也不會誤傷下一輪
            # (下一輪的呼叫是另一個物件;已結束的呼叫取消是 no-op)。
            if block:
                slow_cancel(pending_call)
            else:
                self._spawn(
                    lambda: slow_cancel(pending_call),
                    f"codetrail-cancel-{self.engine.session_id}",
                )
        return True

    # ---- 核准 ----------------------------------------------------------
    def request_approval(self, request: client_engine.ApprovalRequest) -> bool:
        """engine 在 worker 執行緒裡呼叫:阻塞等 UI 回答。

        沒有人回答(逾時)、被取消收掉、或回了非 bool → 都是**拒絕**。
        """
        approval_id = secrets.token_hex(8)
        ticket = ApprovalTicket(approval_id, request)
        with self._lock:
            if self._cancelled:
                # 取消落在 engine 決定要問、與這裡登記 pending 之間:cancel() 沒看到
                # 這筆,所以這裡自己回拒絕(engine 醒來會看旗標,不記成 denied)。
                return False
            self._approvals[approval_id] = ticket
        if self._on_approval is not None:
            self._on_approval(ticket)
        answered = ticket.event.wait(timeout=self._approval_timeout)
        with self._lock:
            still_pending = self._approvals.pop(approval_id, None) is not None
            granted = ticket.granted
        if still_pending and self._on_approval_closed is not None:
            # 逾時:框還開著,要收掉。被 cancel / answer 收走的那些由對方通知。
            self._on_approval_closed(approval_id)
        return bool(answered and granted)

    def answer_approval(self, approval_id: str, granted: Any) -> bool:
        """回答一次核准。**只能回答一次**。

        不在第一個回答就把 pending 原子移除的話,先 deny 再 grant 會讓後者覆蓋
        前者 —— 使用者按了拒絕,工具還是執行了。``granted`` 也必須是真的 bool:
        ``bool("false")`` 是 True。
        """
        if not isinstance(granted, bool):
            return False
        with self._lock:
            ticket = self._approvals.pop(approval_id, None)
            if ticket is None:
                return False
            ticket.granted = granted
        ticket.event.set()
        if self._on_approval_closed is not None:
            self._on_approval_closed(approval_id)
        return True

    # ---- 一輪 ----------------------------------------------------------
    def start_turn(self, text: str) -> str:
        """送一則訊息。回傳送出**之前**要顯示的壓縮停用警告(可能是空字串)。

        警告在啟動 worker 之前就取:先 send 再取的話,模型可能已經撞了 context
        gate,使用者看到的是那個錯誤,卻不知道壓縮早就停了。
        """
        self.begin_turn()
        # begin_turn 之後的任何失敗都必須把回合鎖放掉:漏掉的話這個對話從此
        # 永遠是 busy,使用者連 Ctrl-C 都救不回來(cancel 看到 turn_done=False
        # 但 engine 根本沒有 turn)。
        try:
            with self._lock:
                self._accepting_supplements = True
            target = self.engine.session_id
            notice = self._stop_notice()
            if notice:
                self._publish(client_events.notice_event(target, notice))
            self._spawn(lambda: self._run_turn(text, target), f"codetrail-turn-{target}")
        except BaseException:
            self._close_supplements()
            self._pause_queue()
            self.finish_turn()
            raise
        return notice

    def start_compaction(self) -> None:
        """手動壓縮(``/compact``)。也是一輪:``cancel()`` 才打斷得了長摘要。"""
        self.begin_turn()
        try:
            target = self.engine.session_id
            self._spawn(lambda: self._run_compaction(target), f"codetrail-compact-{target}")
        except BaseException:
            self._close_supplements()
            self._pause_queue()
            self.finish_turn()
            raise

    def prepare_idle(self, reason: str) -> bool:
        """接續歷史先在可取消的回合中壓縮，收尾後才交給零寫入的 prime。"""
        policy = getattr(getattr(self.engine, "options", None), "policy", None)
        if (
            getattr(self.compactor, "mode", None) != client_compaction.MODE_CODETRAIL
            or not self.engine.messages
            or getattr(policy, "name", None) != "interactive"
        ):
            return self.prime_in_background(reason)
        self.begin_turn()
        target = self.engine.session_id
        try:
            self._spawn(
                lambda: self._run_idle_preparation(target, reason),
                f"codetrail-prepare-{target}",
            )
        except BaseException:
            self.finish_turn()
            raise
        return True

    def _run_idle_preparation(self, target: str, reason: str) -> None:
        successful = False
        compacted = False
        try:
            outcome = self.compactor.compact()
            compacted = outcome.status == "compacted"
            successful = outcome.status in ("compacted", "skipped") and not self.cancelled
            if outcome.message:
                self._publish(client_events.notice_event(target, outcome.message))
        except client_events.TurnCancelled:
            self._publish(client_events.notice_event(target, "壓縮已中斷，原始歷史保留。"))
        except Exception as exc:  # noqa: BLE001 - initialization remains usable after a failed count
            self._publish(client_events.error_event(target, f"壓縮準備失敗:{type(exc).__name__}: {exc}"))
        finally:
            if not successful:
                self._pause_queue()
            self._publish(client_events.step_finish_event(
                target, reason=(client_events.REASON_CANCELLED if self.cancelled else
                                client_events.REASON_STOP if successful else client_events.REASON_ERROR),
            ))
            self.finish_turn()
        if successful and not self._start_next_queued(target):
            self.prime_in_background("compaction" if compacted else reason)

    def _spawn(self, body: Callable[[], None], name: str) -> None:
        threading.Thread(target=body, name=name, daemon=True).start()

    def _stop_notice(self) -> str:
        notice = getattr(self.compactor, "pending_stop_notice", None)
        if not callable(notice):
            return ""
        try:
            return notice() or ""
        except Exception:  # noqa: BLE001 - 警告失敗不得擋住送出
            return ""

    def _publish(self, event: Mapping[str, Any]) -> None:
        try:
            self._emit(dict(event))
        except Exception:  # noqa: BLE001 - UI 收不下事件不得帶走這一輪
            pass

    def _run_turn(self, text: str, target: str) -> None:
        # engine 送終結 `step_finish` 的時間點在 send() 回來**之前**,而 ingest 待辦 /
        # 假工具呼叫 / 壓縮結果這些 notice 要等 send() 回來才拿得到。照原順序送,
        # 看終結事件收工的一端會在 notice 之前離開。所以終結先扣住,notice 送完才放行。
        held: list[dict[str, Any]] = []
        compacted = False
        successful = False

        def _emit(event: dict[str, Any]) -> None:
            if client_events.is_terminal_event(event):
                held.append(dict(event))
                return
            self._publish(event)

        try:
            extra: dict[str, Any] = {}
            if self._active_queue_id:
                if self.cancelled:
                    raise client_events.TurnCancelled("回合開始前已中斷")
                message_id = self._active_queue_id
                extra["on_user_recorded"] = lambda: self._queue_user_recorded(message_id)
            result = self.engine.send(
                text,
                on_event=_emit,
                # 沒有 on_text 的話,文字要等整個 model step 跑完才一次送出——長回答
                # 看起來就是「卡住很久然後全部一次冒出來」。串流的那一份用 `text_delta`
                # 標記,終結的 `text` 事件仍由 on_event 負責(事件流的契約沒有改)。
                on_text=lambda chunk: self._publish(
                    client_events.text_delta_event(target, chunk)
                ),
                on_reasoning=self._on_reasoning,
                approve=self.request_approval,
                **extra,
            )
            self._close_supplements()
            for item in result.notices:
                self._publish(client_events.notice_event(target, item))
            outcome = self._auto_compact(target, result)
            compacted = getattr(outcome, "status", None) == "compacted"
            if self.cancelled:
                # 壓縮階段被取消:答案已經給了,但這一輪的結果是「中斷」——cancel 回了
                # True,終結事件就必須是 cancelled,不是 stop。
                self._publish(client_events.notice_event(target, "答案已完成;壓縮已取消。"))
                self._pause_queue()
                self._publish(
                    client_events.step_finish_event(target, reason=client_events.REASON_CANCELLED)
                )
            elif held:
                successful = result.finish == client_events.REASON_STOP
                if not successful:
                    self._pause_queue()
                for event in held:
                    self._publish(event)
            else:
                successful = result.finish == client_events.REASON_STOP
                if not successful:
                    self._pause_queue()
                self._publish(client_events.step_finish_event(target, reason=result.finish))
        except client_events.TurnCancelled:
            self._close_supplements()
            self._restore_unsent_queue_item()
            self._pause_queue()
            self._publish(client_events.notice_event(target, "已中斷這一輪。"))
            self._publish(
                client_events.step_finish_event(target, reason=client_events.REASON_CANCELLED)
            )
        except context_budget.ContextOverflowError as exc:
            self._close_supplements()
            self._restore_unsent_queue_item()
            self._pause_queue()
            self._publish(client_events.error_event(target, str(exc)))
            # The refused request may follow partial tool execution. Recover the
            # older history without retrying this loop or marking its user answered.
            if getattr(self.compactor, "mode", None) == client_compaction.MODE_CODETRAIL:
                outcome = self._auto_compact(target, None, overflow=True)
                compacted = getattr(outcome, "status", None) == "compacted"
                if compacted:
                    self._publish(client_events.notice_event(target, "歷史已壓縮；這次問題尚未完成，請重送。"))
            self._publish(client_events.step_finish_event(
                target, reason=client_events.REASON_CANCELLED if self.cancelled else client_events.REASON_ERROR,
            ))
        except Exception as exc:  # noqa: BLE001 - 一輪失敗不得帶走整個客戶端
            self._close_supplements()
            self._restore_unsent_queue_item()
            self._pause_queue()
            self._publish(client_events.error_event(target, f"{type(exc).__name__}: {exc}"))
            # 終結事件一定要送:只送 error 的話,等終結事件的一端會永遠停在那裡。
            self._publish(
                client_events.step_finish_event(target, reason=client_events.REASON_ERROR)
            )
        finally:
            # Includes late input submitted by the terminal-event UI callback.
            self._close_supplements()
            self.finish_turn()
        # 壓縮換掉了歷史:下一輪要送的 prefix 已經不是剛剛送過的那一份,server 那邊
        # 的 prompt cache 對它是冷的。排在 `finally` 裡的話,預熱會在這一輪的回合鎖
        # **內**等模型鎖,使用者的下一題連 `start_turn` 都排不進來。
        if successful and self._start_next_queued(target):
            return
        if compacted:
            self.prime_in_background("compaction")

    def _run_compaction(self, target: str) -> None:
        compacted = False
        successful = True
        try:
            if self.compactor is None:
                self._publish(
                    client_events.notice_event(target, "這個 session 沒有可用的壓縮(模式為 off)。")
                )
                self._pause_queue()
                return
            try:
                outcome = self.compactor.compact(manual=True)
            except Exception as exc:  # noqa: BLE001
                successful = False
                self._pause_queue()
                self._publish(
                    client_events.notice_event(target, f"壓縮失敗:{type(exc).__name__}: {exc}")
                )
                return
            compacted = getattr(outcome, "status", None) == "compacted"
            if getattr(outcome, "status", None) not in ("compacted", "skipped"):
                successful = False
                self._pause_queue()
            self._publish(
                client_events.notice_event(target, outcome.message or "(沒有可壓縮的內容)")
            )
        finally:
            self._close_supplements()
            reason = (
                client_events.REASON_CANCELLED if self.cancelled else client_events.REASON_STOP
            )
            if self.cancelled:
                successful = False
                self._pause_queue()
            self._publish(client_events.step_finish_event(target, reason=reason))
            self.finish_turn()
        if successful and self._start_next_queued(target):
            return
        if compacted:
            self.prime_in_background("compaction")

    def _auto_compact(self, target: str, result: Any, *, overflow: bool = False) -> Any:
        """自動壓縮。回傳壓縮器給的 outcome(沒壓 / 壓不成回 ``None``)。

        呼叫端要靠它決定壓縮**有沒有真的換掉歷史**:換掉了才需要重新預熱。
        """
        if self.compactor is None:
            return None
        # 只有真的答完(finish=stop)才壓:被截斷(length)、出錯、被中斷的那一輪
        # 沒有可信的切點。
        if not overflow and getattr(result, "finish", None) != client_events.REASON_STOP:
            return None
        try:
            outcome = self.compactor.compact()
        except Exception as exc:  # noqa: BLE001 - 壓縮失敗不得帶走這一輪
            self._pause_queue()
            self._publish(
                client_events.notice_event(target, f"壓縮失敗:{type(exc).__name__}: {exc}")
            )
            return None
        if outcome.status != "skipped" and outcome.message:
            self._publish(client_events.notice_event(target, outcome.message))
        if outcome.status not in ("compacted", "skipped"):
            self._pause_queue()
        return outcome

    # ---- 預熱 ----------------------------------------------------------
    def prime_in_background(
        self, reason: str, *, on_done: Callable[[Any], None] | None = None
    ) -> bool:
        """在背景送一次 prompt cache 預熱。回傳有沒有真的排出去。

        排除兩種情況:engine 沒有這個能力(舊的 engine / 測試替身)、以及**回合
        進行中**——那一輪自己握著模型鎖,預熱只會多一個在鎖上等的執行緒,而且它
        算出來的 prefix 不含這一輪還沒寫定的內容。

        這裡刻意不碰任何回合狀態:不取 ``_turn_lock``、不動 ``_turn_done`` /
        ``_cancelled``。預熱既不是一輪、也不是可以「中斷」的東西(engine 那端由
        ``abort_prime()`` 在換 session 時關掉它)。

        跑完之後,outcome(engine 回的 ``PrimeOutcome`` 形狀;engine raise 時是 None)
        先交給建構時的 ``on_prime(reason, outcome)``,再交給這一次呼叫自己的 ``on_done``
        (可選)。兩邊都在預熱那條執行緒上被呼叫、各自吞例外(UI 正在收尾時搬運會以
        ``CancelledError`` 這種 BaseException 結束),一邊炸了另一邊照樣要到。壓縮換掉
        歷史之後那一次是這裡自己排的、沒有 ``on_done``,只靠 ``on_prime`` 回到 UI。
        """
        prime = getattr(self.engine, "prime_prompt_cache", None)
        if not callable(prime) or self.busy:
            return False

        def _body() -> None:
            outcome = None
            try:
                outcome = prime(reason=reason)
            except Exception:  # noqa: BLE001 - 預熱失敗只是少一次預熱,不得帶走任何東西
                outcome = None
            if self._on_prime is not None:
                try:
                    self._on_prime(reason, outcome)
                except BaseException:  # noqa: BLE001 - 回呼失敗不得帶走 on_done、也不得冒出執行緒
                    pass
            if on_done is None:
                return
            try:
                on_done(outcome)
            except BaseException:  # noqa: BLE001 - UI 正在收尾時搬運會以 CancelledError 結束
                pass

        self._spawn(_body, f"codetrail-prime-{self.engine.session_id}")
        return True

    # ---- session 切換 --------------------------------------------------
    def session_changed(self) -> None:
        """``/new`` / ``/resume`` 之後重綁壓縮器。

        不重綁的話上一段的摘要會進新對話的摘要請求,上一段的停用也會把新的一段
        停掉。這裡**不**取停用警告:那會把「每個 session 只講一次」的那一次在這裡
        消耗掉,真正送出時就不再講了。
        """
        self.assert_session_change_allowed()
        with self._lock:
            self._queue.clear()
            self._turn_id = ""
            self._queue_paused = False
        rebind = getattr(self.compactor, "rebind", None)
        if callable(rebind):
            rebind()
