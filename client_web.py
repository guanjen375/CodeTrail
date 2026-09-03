#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_web — HTTP + SSE 薄前端(與終端 REPL 共用同一個 engine)。

只用標準函式庫。存取邊界與舊的 OpenCode web backend 完全相同:

* 預設只綁 loopback。
* 非 loopback 或 mDNS 廣播 **必須** 設 ``AICODE_WEB_PASSWORD``;
  **server 自己**在沒有密碼時拒絕綁非 loopback —— 不是只靠 wrapper 擋。
  wrapper 那一層仍然保留(兩道獨立的閘),但即使有人繞過 wrapper 直接叫
  ``codetrail_chat.py web --hostname 0.0.0.0``,這裡照樣拒絕。
* 經驗證的 Tailscale-only 例外照舊:hostname 必須等於 wrapper 傳入的
  ``AICODE_WEB_TAILSCALE_IP``、落在 100.64.0.0/10,而且 ``tailscale ip -4``
  當下真的回報同一個位址。存取邊界是 tailnet ACL。
* 密碼比對是常數時間;密碼不進 log、不進事件流。HTTP log 只留方法與路徑,
  query string 一律丟掉(它可能帶 token)。
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import queue
import secrets
import shutil
import socket
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import client_engine
import client_events
import client_store
import endpoint_policy

PASSWORD_ENV = "AICODE_WEB_PASSWORD"
TAILSCALE_IP_ENV = "AICODE_WEB_TAILSCALE_IP"
COOKIE_NAME = "codetrail_token"
DEFAULT_PORT = 4096

#: 核准框等使用者回答的上限。逾時視為拒絕(不是靜默放行)。
APPROVAL_TIMEOUT_SECONDS = 300

MAX_BODY_BYTES = 1024 * 1024


#: 「找不到這個對話」的例外集合:真實 store 丟 SessionStoreError,替身丟 FileNotFoundError。
_NOT_FOUND = (KeyError, FileNotFoundError, ValueError, client_store.SessionStoreError)


class WebSecurityError(RuntimeError):
    """繫結位址與密碼設定不符合存取邊界。"""


def is_loopback(host: str) -> bool:
    return endpoint_policy.is_loopback_host(host)


def _is_tailscale_ipv4(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10")


def tailscale_verified(host: str, env: Mapping[str, str] | None = None) -> bool:
    """hostname 是不是**當下真的**是本機的 Tailscale IPv4。

    三方一致才算數:wrapper 傳進來的 env、CIDR、以及 ``tailscale ip -4`` 現在
    回報的位址。少一方就等於「env 可以偽造一個位址繞過密碼硬規則」。
    """
    environ = os.environ if env is None else env
    expected = str(environ.get(TAILSCALE_IP_ENV, "")).strip()
    if not expected or expected != host or not _is_tailscale_ipv4(host):
        return False
    binary = shutil.which("tailscale")
    if not binary:
        return False
    try:
        result = subprocess.run(
            [binary, "ip", "-4"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return host in {line.strip() for line in result.stdout.splitlines() if line.strip()}


def enforce_bind_policy(
    host: str, *, password: str, mdns: bool = False, env: Mapping[str, str] | None = None
) -> None:
    """沒有密碼就不准離開 loopback。server 端的第二道閘。"""
    exposed = mdns or not is_loopback(host)
    if not exposed or password:
        return
    if not mdns and tailscale_verified(host, env):
        return
    why = f"hostname={host}" + ("(且開啟 mDNS 廣播)" if mdns else "")
    raise WebSecurityError(
        f"拒絕啟動:{why} 會把 web 介面暴露到 loopback 以外,但沒有設 {PASSWORD_ENV}。\n"
        "  沒有密碼時任何能連到這個位址的人都能讀你的專案、核准寫入工具。\n"
        "  擇一處理:\n"
        "    1) 只在本機用:預設的 127.0.0.1\n"
        "    2) A/B 機在同一 tailnet:用 aicode_web(只綁 Tailscale IP,邊界是 tailnet ACL)\n"
        f"    3) 要對可信內網暴露:先 export {PASSWORD_ENV}=<強密碼> 再啟動"
    )


@dataclass
class _Pending:
    request: client_engine.ApprovalRequest
    event: threading.Event = field(default_factory=threading.Event)
    granted: bool = False


@dataclass
class _Session:
    """一個對話在 server 這端的全部狀態。"""

    engine: client_engine.Engine
    compactor: Any = None
    #: 同一個 session 一次只能有一輪在跑(見 WebApp.send)。
    turn_lock: threading.Lock = field(default_factory=threading.Lock)
    busy: bool = False
    listeners: list[queue.Queue] = field(default_factory=list)
    #: 已送出的事件。SSE 訂閱者一定晚於 POST(新 session 要先 POST 才有 id),
    #: 沒有 backlog 的話那段空窗裡的事件就永遠消失了。
    backlog: list[dict[str, Any]] = field(default_factory=list)
    #: 單調遞增的事件序號。attach 每一輪重新訂閱,沒有游標的話會把整個 session
    #: 的 backlog 重播一遍、在第一個**舊的** terminal 就返回,第二題的新事件
    #: 完全讀不到。
    seq: int = 0
    #: cancel() 設、start_turn() 清:request_approval 登記完 pending 之後要看它,
    #: 否則取消落在「ASK 判定後、pending 登記前」的空窗就會等滿 300 秒。
    cancelled: bool = False
    #: 這一輪(含尾端的壓縮)已經結束、只是 turn_lock 還沒放:此時的取消是
    #: no-op,不然旗標會留到下一題。
    turn_done: bool = True


@dataclass(frozen=True)
class TurnStart:
    """`start_turn()` 的回覆:接下來訂閱要從哪個序號之後開始。"""

    session: str
    cursor: int
    notice: str = ""


class WebApp:
    """一個 root、一個 MCP client、多個對話。"""

    #: 每個 session 保留多少事件給晚到的訂閱者重播。
    BACKLOG_LIMIT = 500

    def __init__(
        self,
        engine_factory: Callable[[], client_engine.Engine],
        *,
        password: str = "",
        compactor_factory: Callable[[client_engine.Engine], Any] | None = None,
        store: Any = None,
    ) -> None:
        self._factory = engine_factory
        self._factory_takes_session = _accepts_session_id(engine_factory)
        self._compactor_factory = compactor_factory
        self._store = store
        self.password = password
        #: serve() 綁定之後填入:這個 server 願意回應的 Host 值。DNS rebinding 的
        #: 頁面送來的 Origin 與 Host 都是攻擊者的網域,兩者相等擋不住它——
        #: 要對照的是「我到底綁在哪」。空集合(沒經 serve 起的測試)不檢查。
        self.allowed_hosts: frozenset[str] = frozenset()
        #: `web --cors <origin>`:除了同源之外,還允許哪些瀏覽器來源呼叫 API。
        #: 給「前端頁面放在別的 host」這種部署用;預設空集合 = 只有同源。
        self.allowed_origins: frozenset[str] = frozenset()
        self._token = secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._approvals: dict[str, _Pending] = {}

    # ---- auth ----------------------------------------------------------
    @property
    def token(self) -> str:
        return self._token

    def check_password(self, candidate: str) -> bool:
        """常數時間比對。**先轉 bytes**:``compare_digest`` 對含非 ASCII 的
        str 會丟 ``TypeError``,那等於非 ASCII 密碼永遠登入失敗(而且是
        500,不是 401)。"""
        if not self.password:
            return False
        return hmac.compare_digest(
            str(candidate).encode("utf-8"), str(self.password).encode("utf-8")
        )

    def check_token(self, candidate: str) -> bool:
        return hmac.compare_digest(
            str(candidate).encode("utf-8"), str(self._token).encode("utf-8")
        )

    def authorised(self, headers: Mapping[str, str]) -> bool:
        if not self.password:
            # 只有 loopback 才走得到這裡(見 enforce_bind_policy)。
            return True
        auth = str(headers.get("Authorization", ""))
        if auth.startswith("Bearer ") and self.check_password(auth[7:].strip()):
            return True
        cookie = str(headers.get("Cookie", ""))
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME and self.check_token(value):
                return True
        return False

    # ---- sessions ------------------------------------------------------
    def _session(self, session_id: str | None) -> _Session:
        """取得(必要時建立)一個對話。

        resume 一個還沒載入的 id 時,**不得**先讓 factory 建一個新的持久
        session 再 resume 過去 —— 那會在使用者的 session 目錄留下一個永遠
        空白的孤兒檔(刪除失敗或中途 crash 就留在那裡)。factory 要能直接以
        既有 id 建 engine(`Engine(session_id=...)` 不會 create)。
        """
        if not session_id:
            # `codetrail_chat.py web --session X`:沒指定 session 的請求接續 X,
            # 而不是每次開一個空白對話(以前 args.session 被靜默忽略)。
            session_id = getattr(self, "default_session", None) or None
        with self._lock:
            if session_id and session_id in self._sessions:
                return self._sessions[session_id]
        if session_id and self._store is not None:
            # 先驗這個 id 真的存在,再花力氣建 engine。
            self._store.read(session_id)
        if session_id and self._factory_takes_session:
            engine = self._factory(session_id)
            engine.resume(session_id)
        elif session_id:
            # 不吃 session_id 的 factory(測試替身):退回「建、resume、收掉」。
            engine = self._factory()
            created = engine.session_id
            try:
                engine.resume(session_id)
            except Exception:
                self._discard(engine, created)
                raise
            if created != engine.session_id:
                self._discard(engine, created)
        else:
            engine = self._factory()
        compactor = self._compactor_factory(engine) if self._compactor_factory else None
        session = _Session(engine=engine, compactor=compactor)
        with self._lock:
            existing = self._sessions.get(engine.session_id)
            if existing is not None:
                return existing
            self._sessions[engine.session_id] = session
        return session

    @staticmethod
    def _discard(engine: client_engine.Engine, session_id: str) -> None:
        """把 factory 剛建出來、其實用不到的那個空 session 收掉。"""
        delete = getattr(engine.store, "delete", None)
        if callable(delete):
            try:
                delete(session_id)
            except Exception:  # noqa: BLE001 - 收不掉就算了,不得擋住 resume
                pass

    def engine_for(self, session_id: str | None) -> client_engine.Engine:
        return self._session(session_id).engine

    def resume_session(self, session_id: str) -> dict[str, Any]:
        """瀏覽器的 `/resume`:先把對話載進來,回游標與最近幾則(畫面要能接上)。

        JS 之前是直接對一個還沒載入的 id 開 EventSource:`subscribe()` 對未知
        session 回一個沒掛進任何 listener 的 queue,下一次 POST 才載入、id 又相同,
        畫面從此收不到任何事件。
        """
        session = self._session(session_id)
        with self._lock:
            cursor = session.seq
        recent = []
        for message in session.engine.messages[-40:]:
            role = message.get("role")
            content = message.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                if role == "user" and message.get("synthetic"):
                    continue
                recent.append({"role": role, "text": content})
        return {"session": session.engine.session_id, "cursor": cursor, "messages": recent}

    def sessions(self) -> list[dict[str, Any]]:
        """這個專案已保存的對話(給前端與 attach 選)。"""
        if self._store is None:
            return []
        return [
            {
                "session": info.session_id,
                "title": info.title,
                "turns": info.turns,
                "updated": info.updated,
            }
            for info in self._store.list_sessions()
        ]

    def subscribe(
        self, session_id: str, *, after: int | None = None
    ) -> tuple[queue.Queue, list[dict[str, Any]]]:
        """訂閱事件,並拿回這個 session 的 backlog(``after`` 之後的那些)。"""
        channel: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return channel, []
            session.listeners.append(channel)
            backlog = [
                event for event in session.backlog
                if after is None or int(event.get("seq", 0)) > after
            ]
            return channel, backlog

    def unsubscribe(self, session_id: str, channel: queue.Queue) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            if channel in session.listeners:
                session.listeners.remove(channel)

    def publish(self, session_id: str, event: Mapping[str, Any]) -> None:
        item = dict(event)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            session.seq += 1
            item["seq"] = session.seq
            session.backlog.append(item)
            if len(session.backlog) > self.BACKLOG_LIMIT:
                del session.backlog[: len(session.backlog) - self.BACKLOG_LIMIT]
            listeners = list(session.listeners)
        for channel in listeners:
            try:
                channel.put_nowait(dict(item))
            except queue.Full:
                pass

    # ---- approvals -----------------------------------------------------
    def request_approval(self, request: client_engine.ApprovalRequest) -> bool:
        approval_id = secrets.token_hex(8)
        pending = _Pending(request)
        with self._lock:
            self._approvals[approval_id] = pending
            session = self._sessions.get(request.session_id)
            if session is not None and session.cancelled:
                # 取消落在 engine 決定要問、與這裡登記 pending 之間:cancel() 沒看到
                # 這筆,所以這裡自己回拒絕(engine 醒來會看旗標,不記成 denied)。
                self._approvals.pop(approval_id, None)
                return False
        self.publish(
            request.session_id,
            {
                "type": "approval",
                "sessionID": request.session_id,
                "id": approval_id,
                "tool": request.tool,
                "arguments": request.arguments,
            },
        )
        answered = pending.event.wait(timeout=APPROVAL_TIMEOUT_SECONDS)
        with self._lock:
            self._approvals.pop(approval_id, None)
            granted = pending.granted
        return bool(answered and granted)

    def answer_approval(self, approval_id: str, granted: Any) -> bool:
        """回答一次核准。**只能回答一次**。

        不在第一個回答就把 pending 原子移除的話,兩個分頁(或重送的請求)
        可以先 deny 再 grant,而後者會覆蓋前者 —— 使用者按了拒絕,工具還是
        執行了。`granted` 也必須是真的 bool:``bool("false")`` 是 True。
        """
        if not isinstance(granted, bool):
            return False
        with self._lock:
            pending = self._approvals.pop(approval_id, None)
            if pending is None:
                return False
            pending.granted = granted
        pending.event.set()
        return True

    # ---- turns ---------------------------------------------------------
    class Busy(RuntimeError):
        """這個 session 已經有一輪在跑。"""

    def send(self, session_id: str | None, text: str) -> str:
        return self.start_turn(session_id, text).session

    def start_turn(self, session_id: str | None, text: str) -> TurnStart:
        session = self._session(session_id)
        engine = session.engine
        target = engine.session_id

        # 同一個 session 同時跑兩輪會把歷史交錯:`send()` 先改 messages,
        # payload 也在模型鎖之前組好,於是模型看得到另一輪還沒完成的 tool
        # call。模型鎖只序列化 HTTP 呼叫,保護不到 session 狀態。
        # 取 turn_lock 與「這一輪開始(turn_done=False)」必須在同一個 app lock 臨界區:
        # 兩步之間收到 /api/cancel 的話,cancel() 看到的是上一輪留下的 turn_done=True,
        # 回 False,這一次點擊就整個漏掉。
        self._begin_turn(session, target)

        # 停用警告在**啟動 worker 之前**就發:先 send 再取 notice 的話,模型可能
        # 已經撞了 context gate,使用者看到的是那個錯誤,卻不知道壓縮早就停了。
        notice = ""
        pending_notice = getattr(session.compactor, "pending_stop_notice", None)
        if callable(pending_notice):
            try:
                notice = pending_notice() or ""
            except Exception:  # noqa: BLE001 - 警告失敗不得擋住送出
                notice = ""
        with self._lock:
            cursor = session.seq
        if notice:
            self.publish(target, client_events.notice_event(target, notice))

        def _run() -> None:
            # engine 送 terminal `step_finish` 的時間點在 send() 回來**之前**,
            # 而 ingest 待辦 / 假工具呼叫 / 壓縮結果這些 notice 要等 send() 回來
            # 才拿得到。照原順序送,等 terminal 的 attach 會在 notice 之前離開。
            # 所以 terminal 先扣住,notice 送完才放行。
            held: list[dict[str, Any]] = []

            def _emit(event: dict[str, Any]) -> None:
                if client_events.is_terminal_event(event):
                    held.append(dict(event))
                    return
                self.publish(target, event)

            try:
                result = engine.send(
                    text,
                    on_event=_emit,
                    # 沒有 on_text 的話,文字要等整個 model step 跑完才一次送出
                    # ——長回答在瀏覽器與 attach 看起來就是「卡住很久然後全部
                    # 一次冒出來」。串流的那一份用 `delta` 標記,終端事件仍由
                    # on_event 的 text 事件負責(事件流的契約沒有改)。
                    on_text=lambda chunk: self.publish(
                        target, client_events.text_delta_event(target, chunk)
                    ),
                    approve=self.request_approval,
                )
                for item in result.notices:
                    self.publish(target, client_events.notice_event(target, item))
                self._auto_compact(session, target, result)
                with self._lock:
                    cancelled = session.cancelled
                if cancelled:
                    # 壓縮階段被取消:答案已經給了,但這一輪的結果是「中斷」——cancel 回了
                    # True,terminal 就必須是 cancelled,不是 stop。
                    self.publish(target, client_events.notice_event(target, "答案已完成;壓縮已取消。"))
                    self.publish(
                        target,
                        client_events.step_finish_event(target, reason=client_events.REASON_CANCELLED),
                    )
                elif held:
                    for event in held:
                        self.publish(target, event)
                else:
                    self.publish(
                        target,
                        client_events.step_finish_event(target, reason=result.finish),
                    )
            except client_engine.TurnCancelled:
                self.publish(target, client_events.notice_event(target, "已中斷這一輪。"))
                self.publish(
                    target,
                    client_events.step_finish_event(
                        target, reason=client_events.REASON_CANCELLED
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - 一輪失敗不得帶走 server
                self.publish(
                    target,
                    client_events.error_event(target, f"{type(exc).__name__}: {exc}"),
                )
                # 終結事件一定要送:只送 error 的話,等 terminal 的 attach
                # 會永遠停在那裡(它的 SSE 沒有 timeout)。
                self.publish(
                    target,
                    client_events.step_finish_event(target, reason=client_events.REASON_ERROR),
                )
            finally:
                self._finish_turn(session)

        threading.Thread(target=_run, name=f"codetrail-turn-{target}", daemon=True).start()
        return TurnStart(session=target, cursor=cursor, notice=notice)

    def _auto_compact(self, session: _Session, target: str, result: Any) -> None:
        if session.compactor is None:
            return
        # 只有真的答完(finish=stop)才壓:被截斷(length)、出錯、被中斷的那一輪
        # 沒有可信的切點。
        if getattr(result, "finish", None) != client_events.REASON_STOP:
            return
        try:
            outcome = session.compactor.compact()
        except Exception as exc:  # noqa: BLE001 - 壓縮失敗不得帶走這一輪
            self.publish(
                target,
                client_events.notice_event(target, f"壓縮失敗:{type(exc).__name__}: {exc}"),
            )
            return
        if outcome.status != "skipped" and outcome.message:
            self.publish(target, client_events.notice_event(target, outcome.message))

    def _begin_turn(self, session: _Session, target: str) -> None:
        """取 turn_lock 並標「這一輪開始」——兩步在同一個 app lock 臨界區內。"""
        with self._lock:
            if not session.turn_lock.acquire(blocking=False):
                raise self.Busy(target)
            session.cancelled = False
            session.turn_done = False

    def _finish_turn(self, session: _Session) -> None:
        """一輪(訊息或手動摘要)結束:標 turn_done、清旗標、放鎖。

        前兩步在 app lock 內一起做,跟 `cancel()` 的「判定 + 設旗標」互斥:先標
        「這一輪結束」再清旗標,cancel() 看到 turn_done 就是 no-op,之前來不及消費的
        旗標在這裡清掉——兩邊合起來,取消不會留到下一題。
        """
        with self._lock:
            session.turn_done = True
            clear = getattr(session.engine, "clear_cancel", None)
            if callable(clear):
                clear()
        session.turn_lock.release()

    def cancel(self, session_id: str) -> bool:
        """中斷這個 session 進行中的那一輪。沒有在跑就回 ``False``。

        瀏覽器關掉分頁、attach 按 Ctrl-C 都不會讓 backend 停下來;沒有這條路徑,
        那一輪會跑到底,而 llama-server 是單 slot。

        接不接受由 engine 的 `request_cancel(arm_when_idle=True)` 原子決定:進行中且還沒
        決定寫定 → True;答案 / 摘要已寫定、send() 已退出、壓縮的結果已決定 → False(沒有
        東西可取消,這一輪照原結果收尾);worker 還沒進到 `engine.send()` → engine 預先武裝,
        這一輪一開始就會被中斷,True。等待核准的那一輪則由這裡把 pending 核准回成拒絕來
        喚醒——engine 醒來先看旗標,不會把它記成一筆 denied。
        """
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        pending_call = None
        slow_cancel = None
        with self._lock:
            # 判定與設旗標必須跟 worker 的「turn_done=True → clear_cancel」互斥:
            # 否則 worker 清完旗標之後這裡才補設,下一輪一開始就被誤殺。
            # 鎖內只做**快速**部分(旗標 + 關串流);MCP 取消要等寬限期(最長 10 秒),
            # 放在鎖裡會把其他 session 的所有 app 狀態操作一起卡住。
            if not session.turn_lock.locked() or session.turn_done:
                return False
            # 接不接受由 engine **原子地**決定(它自己的 turn-state 鎖):答案 / 摘要已決定寫定
            # → 拒絕(回 False,什麼都不動);還沒 → 接受(旗標、關串流);worker 還沒進 send()
            # → 預先武裝,下一個 turn 一開始就中斷。web 不再自己 snapshot engine 狀態。
            request = getattr(session.engine, "request_cancel", None)
            if callable(request):
                decision = request(arm_when_idle=True)
                if not decision.accepted:
                    return False
                pending_call = decision.call
                slow_cancel = getattr(session.engine, "cancel_pending", None)
            else:
                cancel = getattr(session.engine, "cancel", None)
                if callable(cancel):
                    cancel()
            session.cancelled = True
            waiting = [
                (approval_id, pending)
                for approval_id, pending in self._approvals.items()
                if pending.request.session_id == session_id
            ]
            for approval_id, pending in waiting:
                self._approvals.pop(approval_id, None)
                pending.granted = False
        for _approval_id, pending in waiting:
            pending.event.set()
        if callable(slow_cancel):
            # 鎖外:同一個 pending call 物件只屬於這一輪,晚一點取消也不會誤傷下一輪
            # (下一輪的呼叫是另一個物件;已結束的呼叫取消是 no-op)。
            slow_cancel(pending_call)
        return True

    def compact(self, session_id: str) -> str:
        """手動壓縮(前端的 /compact)。"""
        session = self._session(session_id)
        if session.compactor is None:
            return "這個 session 沒有可用的壓縮(模式為 off)。"
        # 手動摘要也是一輪:turn_done=False 才讓 /api/cancel 能打斷長摘要。
        self._begin_turn(session, session_id)
        try:
            return session.compactor.compact(manual=True).message or "(沒有可壓縮的內容)"
        finally:
            self._finish_turn(session)

    def stop_notice(self, session_id: str) -> str:
        """送出當下要講的壓縮停用警告(每個 session 只講一次)。"""
        session = self._session(session_id)
        notice = getattr(session.compactor, "pending_stop_notice", None)
        return notice() if callable(notice) else ""


LOGIN_HTML = """<!doctype html>
<meta charset="utf-8"><title>CodeTrail</title>
<style>body{font-family:system-ui,sans-serif;display:grid;place-items:center;height:100vh}</style>
<form method="post" action="/api/login">
  <input type="password" name="password" autofocus placeholder="密碼"><button>登入</button>
</form>
"""

INDEX_HTML = """<!doctype html>
<meta charset="utf-8"><title>CodeTrail</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;display:flex;flex-direction:column;height:100vh}
#log{flex:1;overflow:auto;padding:1rem;white-space:pre-wrap}
form{display:flex;gap:.5rem;padding:.75rem;border-top:1px solid #ccc}
input[type=text]{flex:1;padding:.5rem}
.tool{color:#666}.err{color:#b00}.note{color:#a60}
</style>
<div id="log"></div>
<form id="f"><input type="text" id="q" autofocus autocomplete="off"><button>送出</button><button type="button" id="stop">中斷</button></form>
<script>
const log=document.getElementById('log');
let session=null,es=null;
function add(text,cls){const d=document.createElement('div');if(cls)d.className=cls;d.textContent=text;log.appendChild(d);log.scrollTop=log.scrollHeight;}
let cur=null;
function stream(t){if(!cur){cur=document.createElement('div');log.appendChild(cur);}cur.textContent+=t;log.scrollTop=log.scrollHeight;}
function flush(){cur=null;}
function post(path,body){return fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}
function listen(id,after){
  if(es)es.close();
  const q=after==null?'':'&after='+encodeURIComponent(after);
  es=new EventSource('/api/events?session='+encodeURIComponent(id)+q);
  es.onmessage=e=>{const ev=JSON.parse(e.data);
    if(ev.type==='text_delta'){stream(ev.part.text);}
    else if(ev.type==='text'){flush();}
    else if(ev.type==='tool_use')add('· '+ev.part.tool+' → '+ev.part.state.status,'tool');
    else if(ev.type==='notice')add('⚠ '+ev.message,'note');
    else if(ev.type==='error')add(ev.message,'err');
    else if(ev.type==='approval'){
      const ok=confirm('核准 '+ev.tool+'?\\n\\n'+JSON.stringify(ev.arguments,null,2));
      post('/api/approval',{id:ev.id,granted:ok});
    }};
}
async function sessions(){
  const r=await fetch('/api/sessions');const d=await r.json();
  if(!d.sessions||!d.sessions.length){add('(這個專案還沒有已保存的對話)','note');return;}
  for(const s of d.sessions)add('  '+s.session+'  turns='+s.turns+'  '+s.title,'note');
  add('用 /resume <id> 接續。','note');
}
document.getElementById('stop').onclick=async()=>{if(!session)return;
  const r=await post('/api/cancel',{session:session});const d=await r.json();
  if(!d.ok)add('(這個對話目前沒有在跑的回合)','note');};
document.getElementById('f').onsubmit=async e=>{e.preventDefault();
  const q=document.getElementById('q');const text=q.value.trim();if(!text)return;q.value='';
  if(text==='/sessions'){add('> '+text);sessions();return;}
  if(text.startsWith('/resume ')){const id=text.slice(8).trim();add('> '+text);
    const r=await post('/api/resume',{session:id});const d=await r.json();
    if(d.error){add(d.error,'err');return;}
    session=d.session;for(const m of d.messages)add((m.role==='user'?'> ':'')+m.text);
    listen(session,d.cursor);add('已接續 '+session,'note');return;}
  if(text==='/compact'){add('> '+text);
    if(!session){add('還沒有對話。','note');return;}
    const r=await post('/api/compact',{session:session});const d=await r.json();
    add(d.message||d.error||'','note');return;}
  add('> '+text);
  const r=await post('/api/message',{session:session,text:text});
  const data=await r.json();
  if(data.error){add(data.error,'err');return;}
  if(session!==data.session){session=data.session;listen(session,data.cursor);}
};
</script>
"""


def make_handler(app: WebApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CodeTrail"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            # query string 可能帶 token;log 只留方法與路徑。
            path = urlparse(self.path).path
            print(f"[web] {self.command} {path}", flush=True)

        # -- helpers --
        def _json(
            self,
            status: HTTPStatus,
            payload: Mapping[str, Any],
            *,
            extra_headers: tuple[tuple[str, str], ...] = (),
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._cors_headers()
            for name, value in extra_headers:
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> bytes:
            raw = self.headers.get("Content-Length")
            try:
                length = int(raw) if raw is not None else 0
            except ValueError:
                return b""
            if length <= 0 or length > MAX_BODY_BYTES:
                return b""
            try:
                return self.rfile.read(length)
            except OSError:
                return b""

        def _body(self) -> dict[str, Any]:
            """JSON body。非 object(list / 純量)一律回 {},不丟 traceback。"""
            try:
                value = json.loads(self._read_body().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}
            return value if isinstance(value, dict) else {}

        def _form_body(self) -> dict[str, Any]:
            """``application/x-www-form-urlencoded``。登入頁送的是這個。"""
            try:
                text = self._read_body().decode("utf-8")
            except UnicodeDecodeError:
                return {}
            return {key: values[0] for key, values in parse_qs(text).items() if values}

        def _same_origin(self) -> bool:
            """跨站頁面不得驅動這個 server。

            瀏覽器的 simple POST(``text/plain``、``form``)不觸發 preflight,
            所以 CORS 標頭擋不住它:惡意網頁可以對 loopback 或無密碼的
            tailnet backend 直接送出訊息、開一堆 session、誘導不需核准的
            寫入工具。cookie 是 ``SameSite=Strict``,但無密碼模式根本不用
            cookie —— 所以這一層必須在 server 自己做。
            """
            origin = str(self.headers.get("Origin", "")).strip()
            if not origin:
                # 沒有 Origin 的是非瀏覽器客戶端(attach / curl);它們不會
                # 帶著別人的憑證被誘導。Referer 有值時仍要比對。
                referer = str(self.headers.get("Referer", "")).strip()
                if not referer:
                    return True
                origin = referer
            host = str(self.headers.get("Host", "")).strip()
            try:
                parsed = urlparse(origin)
            except ValueError:
                return False
            if parsed.scheme not in ("http", "https"):
                return False
            if f"{parsed.scheme}://{parsed.netloc}".lower() in app.allowed_origins:
                return True
            return parsed.netloc == host and bool(host)

        def _cors_headers(self) -> None:
            """只對 `--cors` 明列的來源回 ACAO;沒列的來源什麼都不回。"""
            origin = str(self.headers.get("Origin", "")).strip()
            if origin and origin.lower() in app.allowed_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                # 明列的來源可以帶 credentials(cookie / withCredentials 的 EventSource)。
                self.send_header("Access-Control-Allow-Credentials", "true")
                self.send_header("Vary", "Origin")

        def do_OPTIONS(self) -> None:  # noqa: N802
            # CORS preflight:只有 `--cors` 明列的來源會拿到 ACAO,其餘 204 但沒有
            # 任何 Access-Control-* 標頭,瀏覽器自己會擋。
            self.send_response(HTTPStatus.NO_CONTENT)
            self._cors_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _json_request(self) -> bool:
            """API 端點只收 ``application/json``。

            ``text/plain`` 的 simple POST 是跨站頁面唯一送得出來的形狀;
            要求 JSON content type 會讓它必須先過 preflight。
            """
            kind = str(self.headers.get("Content-Type", "")).split(";")[0].strip().lower()
            return kind == "application/json"

        def _host_ok(self) -> bool:
            """Host 必須是這個 server 真的綁的位址。

            DNS rebinding:攻擊頁的網域先指向攻擊者、TTL 後重綁到 127.0.0.1,
            瀏覽器送來的 Origin 與 Host **都是**那個網域——same-origin 比對
            過得了,對照 bind identity 才擋得住。
            """
            if not app.allowed_hosts:
                return True
            host = str(self.headers.get("Host", "")).strip().lower()
            return host in app.allowed_hosts

        def _cors_origin(self) -> str:
            """請求的 Origin 若在 `--cors` 允許清單內就回它,否則回空字串。"""
            origin = str(self.headers.get("Origin", "")).strip()
            return origin if origin and origin.lower() in app.allowed_origins else ""

        def _guard(self, *, query_token: str = "") -> bool:
            if not self._host_ok():
                self._json(HTTPStatus.FORBIDDEN, {"error": "unexpected Host header"})
                return False
            if not self._same_origin():
                self._json(HTTPStatus.FORBIDDEN, {"error": "cross-origin request refused"})
                return False
            if app.authorised(self.headers):
                return True
            if query_token and self._cors_origin() and app.check_token(query_token):
                return True
            self._unauthorised()
            return False

        # -- routes --
        def _html(self, body: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/login":
                self._html(LOGIN_HTML.encode("utf-8"))
                return
            # SSE 是唯一收 query token 的路徑(見 _events 的說明);守門在這裡一次做完。
            query_token = ""
            if path == "/api/events":
                query_token = parse_qs(urlparse(self.path).query).get("token", [""])[0]
            if not self._guard(query_token=query_token):
                return
            if path == "/":
                self._html(INDEX_HTML.encode("utf-8"))
                return
            if path == "/api/sessions":
                self._json(HTTPStatus.OK, {"sessions": app.sessions()})
                return
            if path == "/api/events":
                query = parse_qs(urlparse(self.path).query)
                raw_after = query.get("after", [""])[0]
                after = int(raw_after) if raw_after.isdigit() else None
                self._events(
                    query.get("session", [""])[0],
                    after=after,
                    query_token=query.get("token", [""])[0],
                )
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _unauthorised(self) -> None:
            """瀏覽器來的 GET 導到登入頁;程式化客戶端拿 401 JSON。"""
            accept = str(self.headers.get("Accept", ""))
            if self.command == "GET" and "text/html" in accept:
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorised"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/login":
                if not self._host_ok():
                    self._json(HTTPStatus.FORBIDDEN, {"error": "unexpected Host header"})
                    return
                if not self._same_origin():
                    self._json(
                        HTTPStatus.FORBIDDEN, {"error": "cross-origin request refused"}
                    )
                    return
                # 登入頁送的是 form,程式化客戶端送 JSON。只認 JSON 的話,
                # 正確密碼會被當成空 payload 而回 401 —— 瀏覽器永遠登不進來。
                is_json = self._json_request()
                payload = self._body() if is_json else self._form_body()
                if app.check_password(str(payload.get("password", ""))):
                    cookie = f"{COOKIE_NAME}={app.token}; HttpOnly; SameSite=Strict; Path=/"
                    if is_json:
                        # 程式化 / 跨來源客戶端:token 也回在 body 裡,SSE 可用
                        # `/api/events?token=` 帶(原生 EventSource 不能自訂 header);
                        # 回應帶 CORS 標頭,不然 --cors 明列的前端連登入結果都讀不到。
                        self._json(HTTPStatus.OK, {"ok": True, "token": app.token},
                                   extra_headers=(("Set-Cookie", cookie),))
                        return
                    # 登入頁的 form:回到首頁,cookie 已經帶上。
                    self.send_response(HTTPStatus.SEE_OTHER)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie", cookie)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorised"})
                return
            if not self._guard():
                return
            if not self._json_request():
                self._json(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    {"error": "expected application/json"},
                )
                return
            payload = self._body()
            session = payload.get("session")
            session = session if isinstance(session, str) and session else None
            if path == "/api/message":
                text = str(payload.get("text") or "")
                if not text.strip():
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "empty message"})
                    return
                try:
                    started = app.start_turn(session, text)
                except WebApp.Busy:
                    self._json(
                        HTTPStatus.CONFLICT,
                        {"error": "這個對話已經有一輪在跑,等它結束再送下一則。"},
                    )
                    return
                except _NOT_FOUND as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                # notice 只走事件流(它已經 publish 成 seq 最小的那則),回應裡不重複帶,
                # 否則 attach / 瀏覽器會顯示兩次。
                self._json(HTTPStatus.ACCEPTED, {"session": started.session, "cursor": started.cursor})
                return
            if path == "/api/resume":
                if session is None:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "missing session"})
                    return
                try:
                    loaded = app.resume_session(session)
                except _NOT_FOUND as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                self._json(HTTPStatus.OK, loaded)
                return
            if path == "/api/compact":
                if session is None:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "missing session"})
                    return
                try:
                    message = app.compact(session)
                except WebApp.Busy:
                    self._json(
                        HTTPStatus.CONFLICT, {"error": "這個對話已經有一輪在跑。"}
                    )
                    return
                except _NOT_FOUND as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                self._json(HTTPStatus.OK, {"message": message})
                return
            if path == "/api/cancel":
                if session is None:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "missing session"})
                    return
                try:
                    ok = app.cancel(session)
                except KeyError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "unknown session"})
                    return
                self._json(HTTPStatus.OK, {"ok": ok})
                return
            if path == "/api/approval":
                ok = app.answer_approval(
                    str(payload.get("id") or ""), payload.get("granted")
                )
                self._json(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND, {"ok": ok})
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _events(
            self, session_id: str, *, after: int | None = None, query_token: str = ""
        ) -> None:
            if not session_id:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "missing session"})
                return
            # 原生 EventSource 不能自訂 Authorization header,跨來源前端(--cors 明列)
            # 只能把 /api/login 回的 token 放在 query 裡。只有 SSE 這一條路收 query
            # token、而且只對明列的來源收(_guard);同來源一律走 cookie。log 不寫 query。
            del query_token
            # EventSource 自動重連時會帶 Last-Event-ID:它一定比 URL 裡固定的 after 新
            # (瀏覽器重連用的是同一個 URL),所以有它就以它為準,不重播已顯示的事件。
            last = str(self.headers.get("Last-Event-ID", "")).strip()
            if last.isdigit():
                after = int(last)
            channel, backlog = app.subscribe(session_id, after=after)
            self.send_response(HTTPStatus.OK)
            self._cors_headers()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            def _write(event: Mapping[str, Any]) -> None:
                seq = event.get("seq")
                head = f"id: {seq}\n".encode("utf-8") if isinstance(seq, int) else b""
                self.wfile.write(
                    head + b"data: " + json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n\n"
                )

            try:
                # 新 session 一定是「先 POST 拿到 id、才訂閱」,所以快答、
                # 即時的 context error 或早到的 approval 都會落在訂閱之前。
                # 先重播 backlog(`after` 之後的那些),那段空窗裡的事件才不會消失。
                for event in backlog:
                    _write(event)
                self.wfile.flush()
                while True:
                    try:
                        event = channel.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    _write(event)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app.unsubscribe(session_id, channel)

    return Handler


def serve(
    app: WebApp,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    mdns: bool = False,
    env: Mapping[str, str] | None = None,
) -> ThreadingHTTPServer:
    enforce_bind_policy(host, password=app.password, mdns=mdns, env=env)

    class _Server(ThreadingHTTPServer):
        # `is_loopback()` 接受 `::1`,但 `ThreadingHTTPServer` 的 address_family
        # 預設是 AF_INET —— 不跟著換就是「策略說可以、bind 直接失敗」。
        address_family = (
            socket.AF_INET6 if ":" in host else socket.AF_INET
        )
        daemon_threads = True

    server = _Server((host, port), make_handler(app))
    app.allowed_hosts = host_aliases(host, server.server_address[1], mdns=mdns)
    return server


def host_aliases(host: str, port: int, *, mdns: bool = False) -> frozenset[str]:
    """瀏覽器可能送來的、對應這次 bind 的 Host 值。

    綁 wildcard(`0.0.0.0` / `::`)時**不限制** Host:遠端瀏覽器送來的是實際的
    LAN IP 或主機名,列舉不完;而 wildcard 本來就必須設密碼(`enforce_bind_policy`),
    密碼才是那條邊界。回空集合 = `_host_ok()` 不檢查。
    """
    if host in ("0.0.0.0", "::", ""):
        return frozenset()
    names = {host}
    if is_loopback(host):
        names.update({"127.0.0.1", "localhost", "::1"})
    short = socket.gethostname().split(".")[0]
    if short:
        names.update({short, socket.gethostname()})
    if mdns:
        names.add(f"{short}.local")
    out: set[str] = set()
    for name in names:
        shown = f"[{name}]" if ":" in name else name
        out.add(shown.lower())
        out.add(f"{shown}:{port}".lower())
    return frozenset(out)


def _accepts_session_id(factory: Callable[..., Any]) -> bool:
    """engine factory 能不能直接以既有 session id 建 engine。"""
    import inspect

    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        return False
    return any(
        p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
        for p in parameters.values()
    )
