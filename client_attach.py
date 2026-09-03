#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_attach — 對一個正在跑的 `aicode web` 的薄終端 client。

只用標準函式庫,而且刻意**沒有** engine:它不起 MCP、不碰 session 檔、不做
preflight。所有工作都發生在 web 那一端,這裡只負責送問題、讀 SSE、把核准框
顯示出來並把答案回傳。

密碼與 web 前端同一組(``AICODE_WEB_PASSWORD``);沒有密碼時只有 loopback 的
web 前端會放行,那正是 `enforce_bind_policy` 的邊界。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "codetrail-attach/1"
TIMEOUT_SECONDS = 30
#: SSE 沒有 timeout 會讓 backend 掛掉時 client 永遠停住;這是「等一輪答完」
#: 的上限,比單次 MCP 呼叫的 660 秒寬一點。
STREAM_TIMEOUT_SECONDS = 900


class _NoCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """30x 換 host 時不得帶著 ``Authorization: Bearer <密碼>`` 過去。

    urllib 的預設 redirect handler 只移除 content 相關 header,Authorization
    照送。中間設備或打錯的網址就能把密碼收走。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if (new.scheme, new.netloc) != (old.scheme, old.netloc):
            raise AttachError(
                f"對方要求轉址到 {new.scheme}://{new.netloc};"
                "attach 不跟跨 host 轉址(那會把密碼送到別的主機)。"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    """**不吃環境 proxy**。

    Python 的預設 opener 會裝 ``ProxyHandler()``,於是 ``http_proxy`` 一設,
    連 loopback 與 Tailscale 的請求都可能連同 NDA prompt、核准內容與
    ``Authorization: Bearer <密碼>`` 一起經過那個 proxy。
    """
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoCrossHostRedirect
    )


class AttachError(RuntimeError):
    """連不上、或對方拒絕這組密碼。"""


def _headers(password: str) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    if password:
        headers["Authorization"] = f"Bearer {password}"
    return headers


def _post(url: str, payload: dict[str, Any], password: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(password),
        method="POST",
    )
    try:
        with _opener().open(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AttachError(
                "對方拒絕這組密碼。attach 與 web 前端用同一個 AICODE_WEB_PASSWORD。"
            ) from None
        raise AttachError(f"HTTP {exc.code}") from None
    except OSError as exc:
        raise AttachError(f"連不上 {url}({exc})") from None
    return json.loads(body) if body.strip() else {}


def _stream(url: str, password: str):
    """讀 SSE。每個 `data:` 行是一個事件。"""
    request = urllib.request.Request(url, headers=_headers(password))
    try:
        response = _opener().open(request, timeout=STREAM_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AttachError("對方拒絕這組密碼。") from None
        raise AttachError(f"HTTP {exc.code}") from None
    except OSError as exc:
        raise AttachError(f"連不上 {url}({exc})") from None
    with response:
        for raw in response:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue


def _get(url: str, password: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=_headers(password))
    try:
        with _opener().open(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AttachError("對方拒絕這組密碼。") from None
        raise AttachError(f"HTTP {exc.code}") from None
    except OSError as exc:
        raise AttachError(f"連不上 {url}({exc})") from None
    return json.loads(body) if body.strip() else {}


def latest_session(base: str, password: str) -> str | None:
    """`-c`:接續最近一次的對話。"""
    rows = _get(f"{base}/api/sessions", password).get("sessions") or []
    return rows[0].get("session") if rows else None


HELP = """/sessions          列出這個專案已保存的對話
/resume <id>       接續指定對話
/compact           立刻壓縮這個對話
/exit              離開"""


def _follow(base: str, session: str, password: str, *, after: int | None = None) -> None:
    """讀這一輪的事件直到終結。

    Ctrl-C 不是「本地放棄等待」而已:要對 backend 送 ``/api/cancel``,否則那一輪
    會在 server 上跑到底(單 slot 的 llama-server 會讓下一個問題排在它後面)。
    送出之後重新訂閱並安靜等終結事件;再按一次 Ctrl-C 才真的放棄等待。
    """
    # 帶游標:每一輪重新訂閱,沒有 `after` 的話 backend 會重播整個 session 的
    # backlog,這裡在第一個**舊的** terminal 就返回,第二題的新事件完全讀不到。
    url = f"{base}/api/events?session={urllib.parse.quote(session)}"
    if after is not None:
        url += f"&after={int(after)}"
    streamed = False
    quiet = False
    while True:
        try:
            for event in _stream(url, password):
                kind = event.get("type")
                if quiet and kind not in ("notice", "error", "step_finish"):
                    continue
                if kind == "text_delta":
                    # 串流片段:不換行,整段答完由 `text` 事件收尾。
                    sys.stdout.write((event.get("part") or {}).get("text", ""))
                    sys.stdout.flush()
                    streamed = True
                elif kind == "text":
                    if streamed:
                        print(flush=True)
                        streamed = False
                    else:
                        print((event.get("part") or {}).get("text", ""), flush=True)
                elif kind == "tool_use":
                    part = event.get("part") or {}
                    state = part.get("state") or {}
                    print(f"· {part.get('tool')} → {state.get('status')}", flush=True)
                elif kind == "notice":
                    print(f"[attach] {event.get('message')}", flush=True)
                elif kind == "approval":
                    print(f"\n需要核准: {event.get('tool')}")
                    print(json.dumps(event.get("arguments"), ensure_ascii=False, indent=2))
                    granted = input("核准這次呼叫? [y/N] ").strip().lower() in ("y", "yes")
                    _post(
                        f"{base}/api/approval",
                        {"id": event.get("id"), "granted": granted},
                        password,
                    )
                elif kind == "error":
                    print(f"[attach] {event.get('message')}", file=sys.stderr, flush=True)
                elif kind == "step_finish" and (event.get("part") or {}).get("reason") != "tool-calls":
                    return
            return
        except KeyboardInterrupt:
            if quiet:
                print("\n[attach] 不等了;backend 那一輪可能還在收尾。", flush=True)
                return
            print("\n[attach] 已送出中斷,等 backend 收尾…(再按一次 Ctrl-C 直接放棄等待)", flush=True)
            try:
                _post(f"{base}/api/cancel", {"session": session}, password)
            except AttachError as exc:
                print(f"[attach] 中斷送不出去:{exc}", file=sys.stderr, flush=True)
                return
            quiet = True


def run(url: str, *, password: str = "", session: str | None = None,
        continue_last: bool = False) -> int:
    base = url.rstrip("/")
    print(f"[attach] {base}")
    try:
        if continue_last and not session:
            session = latest_session(base, password)
            if session:
                print(f"[attach] 接續 {session}")
            else:
                print("[attach] 這個專案還沒有已保存的對話,開新的。")
        elif session:
            print(f"[attach] 接續 {session}")
        while True:
            try:
                text = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not text:
                continue
            if text in ("/exit", "/quit"):
                return 0
            if text == "/help":
                print(HELP)
                continue
            if text == "/sessions":
                rows = _get(f"{base}/api/sessions", password).get("sessions") or []
                if not rows:
                    print("(這個專案還沒有已保存的對話)")
                for row in rows[:20]:
                    print(f"  {row['session']}  turns={row['turns']}  {row['title']}")
                continue
            if text.startswith("/resume "):
                session = text[len("/resume "):].strip() or None
                print(f"[attach] 接續 {session}")
                continue
            if text == "/compact":
                if not session:
                    print("[attach] 還沒有對話。")
                    continue
                answer = _post(f"{base}/api/compact", {"session": session}, password)
                print(answer.get("message") or answer.get("error") or "")
                continue
            answer = _post(f"{base}/api/message", {"session": session, "text": text}, password)
            session = answer.get("session", session)
            if answer.get("notice"):
                print(f"[attach] {answer['notice']}")
            if not session:
                raise AttachError("對方沒有回傳 session id")
            cursor = answer.get("cursor")
            _follow(
                base, session, password,
                after=cursor if isinstance(cursor, int) and not isinstance(cursor, bool) else None,
            )
    except AttachError as exc:
        print(f"[attach] {exc}", file=sys.stderr)
        return 2
