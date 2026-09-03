"""client_web 的存取邊界。

web 介面能讀整個專案、能核准寫入工具。所以「沒有密碼就不准離開 loopback」是
安全層,而且必須由 **server 自己**擋 —— 只靠 wrapper 的話,任何人直接叫
`codetrail_chat.py web --hostname 0.0.0.0` 就繞過去了。
"""
from __future__ import annotations

import json
import sys
import threading
import traceback
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import client_engine  # noqa: E402
import client_events  # noqa: E402
import client_mdns  # noqa: E402
import client_store  # noqa: E402
import client_web  # noqa: E402

pytestmark = pytest.mark.smoke


# ============================================================
# 繫結 policy
# ============================================================
def test_loopback_without_a_password_is_allowed():
    for host in ("127.0.0.1", "::1", "localhost"):
        client_web.enforce_bind_policy(host, password="", mdns=False, env={})


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "10.0.0.5", "::"])
def test_a_non_loopback_bind_without_a_password_is_refused(host):
    with pytest.raises(client_web.WebSecurityError, match=client_web.PASSWORD_ENV):
        client_web.enforce_bind_policy(host, password="", mdns=False, env={})


def test_mdns_on_loopback_still_needs_a_password():
    """mDNS 會對區網廣播服務;綁在 loopback 也算暴露。"""
    with pytest.raises(client_web.WebSecurityError):
        client_web.enforce_bind_policy("127.0.0.1", password="", mdns=True, env={})


def test_a_password_allows_a_non_loopback_bind():
    client_web.enforce_bind_policy("192.168.1.10", password="s3cret", mdns=False, env={})


def test_env_alone_cannot_fake_the_tailscale_exception(monkeypatch):
    """三方一致才算數:env、CIDR、以及 `tailscale ip -4` 當下回報的位址。"""
    monkeypatch.setattr(client_web.shutil, "which", lambda _name: None)
    with pytest.raises(client_web.WebSecurityError):
        client_web.enforce_bind_policy(
            "100.101.102.103",
            password="",
            mdns=False,
            env={client_web.TAILSCALE_IP_ENV: "100.101.102.103"},
        )


def test_a_lan_address_is_never_accepted_as_tailscale(monkeypatch):
    monkeypatch.setattr(client_web.shutil, "which", lambda _name: "/usr/bin/tailscale")
    monkeypatch.setattr(
        client_web.subprocess,
        "run",
        lambda *_a, **_k: type("R", (), {"stdout": "192.168.1.10\n"})(),
    )
    with pytest.raises(client_web.WebSecurityError):
        client_web.enforce_bind_policy(
            "192.168.1.10",
            password="",
            mdns=False,
            env={client_web.TAILSCALE_IP_ENV: "192.168.1.10"},
        )


def test_a_verified_tailscale_address_is_the_documented_exception(monkeypatch):
    monkeypatch.setattr(client_web.shutil, "which", lambda _name: "/usr/bin/tailscale")
    monkeypatch.setattr(
        client_web.subprocess,
        "run",
        lambda *_a, **_k: type("R", (), {"stdout": "100.101.102.103\n"})(),
    )
    client_web.enforce_bind_policy(
        "100.101.102.103",
        password="",
        mdns=False,
        env={client_web.TAILSCALE_IP_ENV: "100.101.102.103"},
    )


def test_mdns_has_no_tailscale_exception(monkeypatch):
    monkeypatch.setattr(client_web.shutil, "which", lambda _name: "/usr/bin/tailscale")
    monkeypatch.setattr(
        client_web.subprocess,
        "run",
        lambda *_a, **_k: type("R", (), {"stdout": "100.101.102.103\n"})(),
    )
    with pytest.raises(client_web.WebSecurityError):
        client_web.enforce_bind_policy(
            "100.101.102.103",
            password="",
            mdns=True,
            env={client_web.TAILSCALE_IP_ENV: "100.101.102.103"},
        )


# ============================================================
# 每個端點都要驗
# ============================================================
class _Result:
    def __init__(self, notices=()):
        self.notices = list(notices)
        self.finish = client_events.REASON_STOP


class _StubEngine:
    _next = 0

    def __init__(self, notices=(), block=None):
        _StubEngine._next += 1
        self.session_id = f"20260101T00000{_StubEngine._next}-abcdef01"
        self.messages: list[dict] = []
        self.sent: list[str] = []
        self.store = _StubStore()
        self._notices = list(notices)
        self._block = block

    def resume(self, session_id):
        self.session_id = session_id

    def send(self, text, *, on_event=None, approve=None, **_kwargs):
        self.sent.append(text)
        if self._block is not None:
            self._block.wait(timeout=10)
        if on_event:
            on_event({"type": "text", "sessionID": self.session_id,
                      "part": {"type": "text", "text": "ok"}})
        return _Result(self._notices)


class _StubStore:
    def __init__(self):
        self.deleted: list[str] = []

    def delete(self, session_id):
        self.deleted.append(session_id)


@pytest.fixture()
def served():
    app = client_web.WebApp(lambda: _StubEngine(), password="s3cret")
    server = client_web.serve(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield app, base
    finally:
        server.shutdown()
        server.server_close()


def _get(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(request, timeout=10)


def _post(url, payload, headers=None):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=10)


@pytest.mark.parametrize(
    "path,method",
    [
        ("/", "GET"),
        ("/api/events?session=20260101T000000-abcdef01", "GET"),
        ("/api/message", "POST"),
        ("/api/approval", "POST"),
    ],
)
def test_every_endpoint_requires_the_password(served, path, method):
    _app, base = served
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        if method == "GET":
            _get(base + path)
        else:
            _post(base + path, {})
    assert excinfo.value.code == 401


def test_the_password_unlocks_every_endpoint(served):
    _app, base = served
    headers = {"Authorization": "Bearer s3cret"}
    assert _get(base + "/", headers=headers).status == 200
    response = _post(base + "/api/message", {"text": "hi"}, headers=headers)
    assert response.status == 202


def test_a_wrong_password_is_rejected(served):
    _app, base = served
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(base + "/", headers={"Authorization": "Bearer wrong"})
    assert excinfo.value.code == 401


def test_the_login_cookie_is_httponly_and_not_the_password(served):
    _app, base = served
    response = _post(base + "/api/login", {"password": "s3cret"})
    cookie = response.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert "s3cret" not in cookie


def test_the_password_never_reaches_the_log(served, capsys):
    _app, base = served
    with pytest.raises(urllib.error.HTTPError):
        _get(base + "/api/events?session=x&token=s3cret", headers={})
    time.sleep(0.1)
    captured = capsys.readouterr()
    assert "s3cret" not in captured.out + captured.err


def test_password_comparison_is_constant_time():
    app = client_web.WebApp(lambda: _StubEngine(), password="s3cret")
    assert app.check_password("s3cret") is True
    assert app.check_password("wrong") is False
    assert app.check_password("") is False


def test_a_passwordless_app_still_serves_loopback():
    app = client_web.WebApp(lambda: _StubEngine(), password="")
    assert app.authorised({}) is True


# ============================================================
# 核准
# ============================================================
def test_an_unanswered_approval_is_a_refusal(monkeypatch):
    monkeypatch.setattr(client_web, "APPROVAL_TIMEOUT_SECONDS", 0.2)
    app = client_web.WebApp(lambda: _StubEngine(), password="")
    request = client_engine.ApprovalRequest("s", "apply_patch", {"diff": "x"})
    assert app.request_approval(request) is False


def test_an_approval_answer_reaches_the_waiting_engine(monkeypatch):
    monkeypatch.setattr(client_web, "APPROVAL_TIMEOUT_SECONDS", 5)
    app = client_web.WebApp(lambda: _StubEngine(), password="")
    target = app.engine_for(None).session_id
    channel, _backlog = app.subscribe(target)
    request = client_engine.ApprovalRequest(target, "apply_patch", {"diff": "x"})
    result: list[bool] = []
    worker = threading.Thread(
        target=lambda: result.append(app.request_approval(request)), daemon=True
    )
    worker.start()
    event = channel.get(timeout=5)
    assert event["type"] == "approval" and event["arguments"] == {"diff": "x"}
    assert app.answer_approval(event["id"], True) is True
    worker.join(timeout=5)
    assert result == [True]


def test_an_approval_can_only_be_answered_once(monkeypatch):
    """先 deny 再 grant 不得把拒絕翻成核准。

    兩個分頁 / 重送的請求都拿得到同一個 approval id。pending 不在第一個
    回答時就原子移除的話,後到的那個會覆蓋前一個 —— 使用者按了拒絕,寫入
    工具還是執行了。
    """
    monkeypatch.setattr(client_web, "APPROVAL_TIMEOUT_SECONDS", 5)
    app = client_web.WebApp(lambda: _StubEngine(), password="")
    target = app.engine_for(None).session_id
    channel, _backlog = app.subscribe(target)
    request = client_engine.ApprovalRequest(target, "apply_patch", {"diff": "x"})
    result: list[bool] = []
    worker = threading.Thread(
        target=lambda: result.append(app.request_approval(request)), daemon=True
    )
    worker.start()
    event = channel.get(timeout=5)
    assert app.answer_approval(event["id"], False) is True
    assert app.answer_approval(event["id"], True) is False
    worker.join(timeout=5)
    assert result == [False]


def test_a_non_boolean_granted_is_never_an_approval(monkeypatch):
    """`bool("false")` 是 True。字串一律不算核准。"""
    monkeypatch.setattr(client_web, "APPROVAL_TIMEOUT_SECONDS", 5)
    app = client_web.WebApp(lambda: _StubEngine(), password="")
    target = app.engine_for(None).session_id
    channel, _backlog = app.subscribe(target)
    request = client_engine.ApprovalRequest(target, "apply_patch", {"diff": "x"})
    result: list[bool] = []
    worker = threading.Thread(
        target=lambda: result.append(app.request_approval(request)), daemon=True
    )
    worker.start()
    event = channel.get(timeout=5)
    for value in ("false", "true", 1, None):
        assert app.answer_approval(event["id"], value) is False
    assert app.answer_approval(event["id"], False) is True
    worker.join(timeout=5)
    assert result == [False]


def test_an_unknown_approval_id_is_rejected():
    app = client_web.WebApp(lambda: _StubEngine(), password="")
    assert app.answer_approval("deadbeef", True) is False


# ============================================================
# 瀏覽器來源邊界(S5 審核回修)
# ============================================================
def _raw_post(base, path, body, headers):
    request = urllib.request.Request(
        base + path, data=body, headers=headers, method="POST"
    )
    return urllib.request.urlopen(request, timeout=10)


@pytest.fixture()
def open_served():
    """無密碼的 loopback server:CSRF 的判準不能靠密碼。"""
    app = client_web.WebApp(lambda: _StubEngine(), password="")
    server = client_web.serve(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield app, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_a_cross_origin_page_cannot_drive_the_server(open_served):
    """惡意網頁對 loopback 送 simple POST:server 自己要擋。

    無密碼模式根本不用 cookie,所以 SameSite 擋不到它;沒有這一層,
    任何開著的網頁都能開對話、誘導不需核准的寫入工具。
    """
    app, base = open_served
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(base + "/api/message", {"text": "hi"},
              headers={"Origin": "http://evil.example"})
    assert excinfo.value.code == 403
    assert app.sessions() == []


def test_a_text_plain_simple_post_is_refused(open_served):
    """`text/plain` 是跨站頁面唯一免 preflight 的形狀。"""
    _app, base = open_served
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _raw_post(base, "/api/message", json.dumps({"text": "hi"}).encode(),
                  {"Content-Type": "text/plain"})
    assert excinfo.value.code == 415


def test_a_same_origin_page_still_works(open_served):
    _app, base = open_served
    host = base.split("//", 1)[1]
    response = _post(base + "/api/message", {"text": "hi"},
                     headers={"Origin": base, "Host": host})
    assert response.status == 202


def test_a_non_ascii_password_can_log_in():
    """`compare_digest` 對含非 ASCII 的 str 會丟 TypeError。"""
    app = client_web.WebApp(lambda: _StubEngine(), password="中文密碼")
    assert app.check_password("中文密碼") is True
    assert app.check_password("別的") is False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def test_a_browser_get_without_the_password_goes_to_the_login_page(served):
    """瀏覽器直接開首頁時只回一份 401 JSON,使用者根本看不到登入表單。"""
    _app, base = served
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(base + "/", headers={"Accept": "text/html"})
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        opener.open(request, timeout=10)
    assert excinfo.value.code == 303
    assert excinfo.value.headers.get("Location") == "/login"
    assert b"<form" in _get(base + "/login").read()


def test_the_login_form_body_is_accepted(served):
    """登入頁送的是 urlencoded;只解析 JSON 的話正確密碼也會被當成空 payload。"""
    _app, base = served
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(
        base + "/api/login", data=b"password=s3cret", method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    # 表單登入成功 → 帶著 cookie 回首頁(303),不是留在一個空白的 204。
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        opener.open(request, timeout=10)
    assert excinfo.value.code == 303
    assert excinfo.value.headers.get("Location") == "/"
    assert client_web.COOKIE_NAME in excinfo.value.headers.get("Set-Cookie", "")


# ============================================================
# 事件不得在訂閱之前消失 / 同一 session 不得並行
# ============================================================
def test_events_published_before_the_subscription_are_replayed():
    """新 session 一定是「先 POST 拿到 id、才訂閱」。

    沒有 backlog 的話,快答、即時的 context error 與早到的 approval 全部
    落在那段空窗裡,前端與 attach 永遠等不到。
    """
    app = client_web.WebApp(lambda: _StubEngine(), password="")
    target = app.send(None, "hi")
    for _ in range(200):
        channel, backlog = app.subscribe(target)
        app.unsubscribe(target, channel)
        if backlog:
            break
        time.sleep(0.01)
    # 終結事件一定在(engine 沒送的話 web 自己補一個),而且排在 notice 之後。
    assert [event["type"] for event in backlog] == ["text", "step_finish"]


def test_a_turn_failure_still_sends_a_terminal_event():
    """只送 error 的話,等 terminal 的 attach 會永遠停在那裡。"""
    class _Boom(_StubEngine):
        def send(self, *_a, **_k):
            raise RuntimeError("boom")

    app = client_web.WebApp(lambda: _Boom(), password="")
    target = app.send(None, "hi")
    for _ in range(200):
        channel, backlog = app.subscribe(target)
        app.unsubscribe(target, channel)
        if any(e["type"] == "step_finish" for e in backlog):
            break
        time.sleep(0.01)
    kinds = [event["type"] for event in backlog]
    assert kinds == ["error", "step_finish"]
    assert backlog[-1]["part"]["reason"] == client_events.REASON_ERROR


def test_two_concurrent_turns_on_one_session_are_refused():
    """同時跑兩輪會把歷史交錯:模型看得到另一輪還沒完成的 tool call。"""
    gate = threading.Event()
    app = client_web.WebApp(lambda: _StubEngine(block=gate), password="")
    target = app.send(None, "first")
    with pytest.raises(client_web.WebApp.Busy):
        app.send(target, "second")
    gate.set()


def test_the_turn_notices_reach_the_client():
    """ingest 待辦、假工具呼叫、對話未落檔都靠這條路徑。"""
    app = client_web.WebApp(
        lambda: _StubEngine(notices=["⚠ 有待辦"]), password=""
    )
    target = app.send(None, "hi")
    for _ in range(200):
        channel, backlog = app.subscribe(target)
        app.unsubscribe(target, channel)
        if any(e["type"] == "notice" for e in backlog):
            break
        time.sleep(0.01)
    assert any(e["type"] == "notice" and e["message"] == "⚠ 有待辦" for e in backlog)


# ============================================================
# session 接續
# ============================================================
class _Store:
    def __init__(self):
        self.rows = {"20260101T000000-aaaaaaaa": ["turn"]}
        self.deleted: list[str] = []

    def read(self, session_id):
        if session_id not in self.rows:
            raise FileNotFoundError(session_id)
        return self.rows[session_id]

    def delete(self, session_id):
        self.deleted.append(session_id)

    def list_sessions(self):
        class _Info:
            session_id = "20260101T000000-aaaaaaaa"
            title = "舊對話"
            turns = 1
            updated = 0.0

        return [_Info()]


def test_resuming_never_leaves_an_orphan_session_behind():
    """factory 先建一個新的持久 session、再 resume 過去,會留下空白孤兒檔。"""
    store = _Store()
    engines: list[_StubEngine] = []

    def _factory():
        engine = _StubEngine()
        engine.store = store
        engines.append(engine)
        return engine

    app = client_web.WebApp(_factory, password="", store=store)
    created: list[str] = []
    original_factory = _factory

    def _recording_factory():
        engine = original_factory()
        created.append(engine.session_id)
        return engine

    app._factory = _recording_factory
    engine = app.engine_for("20260101T000000-aaaaaaaa")
    assert engine.session_id == "20260101T000000-aaaaaaaa"
    assert store.deleted == created          # factory 建的那個空 session 被收掉了


def test_an_unknown_session_id_creates_nothing():
    store = _Store()

    def _factory():
        engine = _StubEngine()
        engine.store = store
        return engine

    app = client_web.WebApp(_factory, password="", store=store)
    with pytest.raises(FileNotFoundError):
        app.engine_for("20260101T000000-ffffffff")
    assert store.deleted == []


def test_the_session_list_is_available_to_the_client(served):
    _app, base = served
    app = client_web.WebApp(lambda: _StubEngine(), password="", store=_Store())
    assert app.sessions()[0]["session"] == "20260101T000000-aaaaaaaa"


# ============================================================
# mDNS 廣播(S5 審核回修:原本只有安全檢查、沒有實作)
# ============================================================
def test_the_advertisement_carries_the_service_and_address():
    """廣播的內容:PTR / SRV / TXT / A 四筆,而且**不含**密碼或專案路徑。"""
    payload = client_mdns.build_response(
        "CodeTrail on 192.168.1.9-4096", "boxy", "192.168.1.9", 4096
    )
    assert payload[6:8] == b"\x00\x04"                      # ANCOUNT = 4
    assert b"_http" in payload and b"_tcp" in payload
    assert bytes([192, 168, 1, 9]) in payload
    assert b"boxy" in payload


def test_a_query_for_the_service_is_recognised():
    import struct

    question = client_mdns.encode_name(client_mdns.SERVICE)
    query = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + question + struct.pack("!HH", 12, 1)
    questions = client_mdns.parse_questions(query)
    assert questions == [(client_mdns.SERVICE, client_mdns.TYPE_PTR)]
    assert client_mdns.wants_us(questions, "CodeTrail on 192.168.1.9-4096", "boxy") is True
    assert client_mdns.wants_us(questions, "other", "boxy") is True   # PTR 是服務層級


def test_a_query_for_someone_else_is_ignored():
    import struct

    question = client_mdns.encode_name("_ipp._tcp.local")
    query = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + question + struct.pack("!HH", 12, 1)
    assert client_mdns.wants_us(
        client_mdns.parse_questions(query), "CodeTrail on 1.2.3.4-4096", "boxy"
    ) is False


def test_a_response_is_never_treated_as_a_query():
    """QR=1 的封包是別人的答案,不是要我們回答的問題。"""
    payload = client_mdns.build_response("x", "boxy", "192.168.1.9", 4096)
    assert client_mdns.parse_questions(payload) == []


def test_broadcasting_a_loopback_address_is_refused():
    """廣播一個別人連不到的位址只會製造困惑。"""
    with pytest.raises(client_mdns.MdnsError):
        client_mdns.Advertiser("127.0.0.1", 4096)


# ============================================================
# 密碼不得外流到子行程 / tmux(S5 審核回修)
# ============================================================
@pytest.mark.smoke
def test_the_web_password_never_reaches_the_mcp_child(monkeypatch):
    """MCP server 用不到 web 密碼,而核准後的 run_command 會繼承整份環境。

    專案自己的測試腳本只要印一次 env,那個密碼就進了工具結果與 session 檔。
    """
    import client_mcp

    monkeypatch.setenv("AICODE_WEB_PASSWORD", "sentinel-password")
    child = client_mcp.McpClient("/tmp")._build_env()
    assert "AICODE_WEB_PASSWORD" not in child
    assert "sentinel-password" not in "".join(child.values())


@pytest.mark.smoke
def test_the_streamed_deltas_never_duplicate_the_turn_text():
    """串流片段只給 UI:共用解析器只認 `text`,不然同一段文字會被算兩次。"""
    from scripts.eval_tool_routing import parse_event_stream

    session = "20260101T000000-abcdef01"
    stream = "\n".join(
        client_events.dumps(event)
        for event in (
            client_events.text_delta_event(session, "半"),
            client_events.text_delta_event(session, "句"),
            client_events.text_event(session, "半句話"),
            client_events.step_finish_event(session, reason=client_events.REASON_STOP),
        )
    )
    trace = parse_event_stream(stream)
    assert trace.assistant_text == "半句話"


# ============================================================
# web 的壓縮接線與 attach 的網路邊界(總審前補的守門測試)
# ============================================================
class _Outcome:
    def __init__(self, status, message):
        self.status = status
        self.message = message


class _StubCompactor:
    def __init__(self, engine):
        self.engine = engine
        self.calls: list[bool] = []

    def compact(self, *, manual=False):
        self.calls.append(manual)
        return _Outcome("compacted", "已壓縮:stub")

    def pending_stop_notice(self):
        return ""


@pytest.mark.smoke
def test_manual_compaction_is_reachable_from_the_web_api():
    """web 沒接壓縮的話,`/compact` 只會變成一則普通訊息送給模型。"""
    made: list[_StubCompactor] = []

    def _factory(engine):
        made.append(_StubCompactor(engine))
        return made[-1]

    app = client_web.WebApp(lambda: _StubEngine(), password="", compactor_factory=_factory)
    target = app.engine_for(None).session_id
    assert app.compact(target) == "已壓縮:stub"
    assert made[0].calls == [True]


@pytest.mark.smoke
def test_auto_compaction_runs_after_a_web_turn():
    made: list[_StubCompactor] = []

    def _factory(engine):
        made.append(_StubCompactor(engine))
        return made[-1]

    app = client_web.WebApp(lambda: _StubEngine(), password="", compactor_factory=_factory)
    target = app.send(None, "hi")
    for _ in range(200):
        if made and made[0].calls:
            break
        time.sleep(0.01)
    assert made[0].calls == [False]
    channel, backlog = app.subscribe(target)
    app.unsubscribe(target, channel)
    assert any(e["type"] == "notice" and "已壓縮" in e["message"] for e in backlog)


@pytest.mark.smoke
def test_attach_never_uses_an_environment_proxy(monkeypatch):
    """`http_proxy` 一設,連 loopback / Tailscale 的請求都會連同 NDA prompt 與
    `Authorization: Bearer <密碼>` 一起經過那個 proxy。"""
    import client_attach

    monkeypatch.setenv("http_proxy", "http://proxy.example:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    opener = client_attach._opener()
    proxied = [
        h for h in opener.handlers
        if isinstance(h, urllib.request.ProxyHandler) and h.proxies
    ]
    assert proxied == []
    # 對照組:預設 opener 真的會吃到這個 env(證明上面那條不是空測)。
    default = urllib.request.build_opener()
    assert any(
        isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in default.handlers
    )


@pytest.mark.smoke
def test_attach_refuses_a_cross_host_redirect():
    """urllib 的預設 redirect handler 會把 Authorization 帶到 Location 的 host。"""
    import client_attach

    handler = client_attach._NoCrossHostRedirect()
    request = urllib.request.Request(
        "http://127.0.0.1:4096/api/message", headers={"Authorization": "Bearer s"}
    )
    with pytest.raises(client_attach.AttachError, match="轉址"):
        handler.redirect_request(
            request, None, 302, "Found", {}, "http://other.example/capture"
        )
    same = handler.redirect_request(
        request, None, 302, "Found", {}, "http://127.0.0.1:4096/api/other"
    )
    assert same is not None


# ============================================================
# /api/cancel
# ============================================================
@pytest.mark.smoke
def test_a_running_turn_can_be_cancelled_from_the_api():
    """瀏覽器關掉分頁、attach 按 Ctrl-C 都不會讓 backend 停下來;要有一條路徑。"""
    gate = threading.Event()

    class _Cancellable(_StubEngine):
        def __init__(self):
            super().__init__()
            self.cancelled = False

        def cancel(self):
            self.cancelled = True
            gate.set()
            return True

        def send(self, text, *, on_event=None, approve=None, **_kwargs):
            self.sent.append(text)
            gate.wait(10)
            if self.cancelled:
                raise client_engine.TurnCancelled("user")
            return _Result()

    app = client_web.WebApp(lambda: _Cancellable(), password="")
    target = app.send(None, "hi")
    assert app.cancel(target) is True
    for _ in range(300):
        channel, backlog = app.subscribe(target)
        app.unsubscribe(target, channel)
        if any(e["type"] == "step_finish" for e in backlog):
            break
        time.sleep(0.01)
    kinds = [(e["type"], e.get("message") or (e.get("part") or {}).get("reason")) for e in backlog]
    assert ("notice", "已中斷這一輪。") in kinds
    assert ("step_finish", client_events.REASON_CANCELLED) in kinds
    assert client_events.is_terminal_event(backlog[-1])
    assert app.cancel(target) is False          # 已經沒有在跑的一輪


def test_cancelling_an_unknown_session_is_a_404(served):
    _app, base = served
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(base + "/api/cancel", {"session": "20260101T000000-ffffffff"},
              headers={"Authorization": "Bearer s3cret"})
    assert excinfo.value.code == 404


# ============================================================
# 總審第 1 輪回修:游標、notice 順序、取消接縫、Host、resume 不建孤兒
# ============================================================
class _TerminalStub(_StubEngine):
    """會自己送 terminal step_finish 的替身(真 engine 的形狀)。"""

    def send(self, text, *, on_event=None, approve=None, **_kwargs):
        self.sent.append(text)
        if on_event:
            on_event(client_events.text_event(self.session_id, "ok"))
            on_event(client_events.step_finish_event(self.session_id, reason=client_events.REASON_STOP))
        return _Result(self._notices)


def _wait_for(app, target, predicate, tries=300):
    for _ in range(tries):
        channel, backlog = app.subscribe(target)
        app.unsubscribe(target, channel)
        if predicate(backlog):
            return backlog
        time.sleep(0.01)
    raise AssertionError("condition never met")


@pytest.mark.smoke
def test_notices_are_delivered_before_the_terminal_event():
    """terminal 先扣住、notice 送完才放:等 terminal 的 attach 才看得到 ingest 待辦。"""
    app = client_web.WebApp(lambda: _TerminalStub(notices=["⚠ 有待辦"]), password="")
    target = app.send(None, "hi")
    backlog = _wait_for(app, target, lambda b: any(e["type"] == "step_finish" for e in b))
    assert [e["type"] for e in backlog] == ["text", "notice", "step_finish"]


@pytest.mark.smoke
def test_a_turn_cursor_lets_the_next_subscription_skip_old_events():
    """attach 每一輪重新訂閱:沒有游標會在舊的 terminal 就返回,新事件讀不到。"""
    app = client_web.WebApp(lambda: _TerminalStub(), password="")
    first = app.start_turn(None, "first")
    _wait_for(app, first.session, lambda b: any(e["type"] == "step_finish" for e in b))
    second = app.start_turn(first.session, "second")
    assert second.cursor >= 2
    backlog = _wait_for(
        app, first.session,
        lambda b: sum(1 for e in b if e["type"] == "step_finish") == 2,
    )
    _channel, after = app.subscribe(first.session, after=second.cursor)
    assert all(e["seq"] > second.cursor for e in after)
    assert [e["type"] for e in after] == ["text", "step_finish"]
    assert len(backlog) == 4


def test_the_message_api_returns_the_cursor(open_served):
    _app, base = open_served
    payload = json.loads(_post(base + "/api/message", {"text": "hi"}).read())
    assert isinstance(payload["cursor"], int)


@pytest.mark.smoke
def test_cancel_wakes_a_turn_waiting_for_approval():
    """等核准的那一輪也要能被中斷:pending 核准回成拒絕來喚醒,engine 那端看旗標。"""
    class _WaitsForApproval(_StubEngine):
        def __init__(self):
            super().__init__()
            self.cancelled = False

        def cancel(self):
            self.cancelled = True
            return True

        def send(self, text, *, on_event=None, approve=None, **_kwargs):
            granted = approve(client_engine.ApprovalRequest(self.session_id, "apply_patch", {}))
            if self.cancelled:
                raise client_engine.TurnCancelled("user")
            return _Result([f"granted={granted}"])

    app = client_web.WebApp(lambda: _WaitsForApproval(), password="")
    target = app.send(None, "hi")
    backlog = _wait_for(app, target, lambda b: any(e["type"] == "approval" for e in b))
    approval_id = next(e["id"] for e in backlog if e["type"] == "approval")
    assert app.cancel(target) is True
    backlog = _wait_for(app, target, lambda b: any(e["type"] == "step_finish" for e in b))
    assert backlog[-1]["part"]["reason"] == client_events.REASON_CANCELLED
    assert app.answer_approval(approval_id, True) is False      # 已經被收掉,不能再翻成核准


@pytest.mark.smoke
def test_cancel_counts_even_before_the_worker_enters_send():
    """POST 之後立刻 cancel:worker 還沒進 send(),engine 看不到進行中的東西,但旗標已設。"""
    gate = threading.Event()

    class _Prestart(_StubEngine):
        def cancel(self):
            gate.set()
            return False          # engine 那端「沒有進行中的串流或呼叫」

        def send(self, text, *, on_event=None, approve=None, **_kwargs):
            gate.wait(5)
            raise client_engine.TurnCancelled("flag")

    app = client_web.WebApp(lambda: _Prestart(), password="")
    target = app.send(None, "hi")
    assert app.cancel(target) is True
    backlog = _wait_for(app, target, lambda b: any(e["type"] == "step_finish" for e in b))
    assert backlog[-1]["part"]["reason"] == client_events.REASON_CANCELLED


@pytest.mark.smoke
def test_auto_compaction_only_runs_after_a_completed_answer():
    """截斷(length)/ 出錯 / 中斷的那一輪沒有可信的切點。"""
    made: list[_StubCompactor] = []

    def _factory(engine):
        made.append(_StubCompactor(engine))
        return made[-1]

    class _Truncated(_StubEngine):
        def send(self, text, *, on_event=None, approve=None, **_kwargs):
            result = _Result()
            result.finish = "length"
            return result

    app = client_web.WebApp(lambda: _Truncated(), password="", compactor_factory=_factory)
    target = app.send(None, "hi")
    _wait_for(app, target, lambda b: any(e["type"] == "step_finish" for e in b))
    assert made[0].calls == []


@pytest.mark.smoke
def test_the_durable_stop_notice_is_published_before_the_turn_starts():
    """先 send 再取 notice 的話,模型可能已經撞了 context gate,使用者不知道壓縮早就停了。"""
    class _Stopped(_StubCompactor):
        def pending_stop_notice(self):
            return "這個 session 的自動壓縮已停用"

    app = client_web.WebApp(
        lambda: _TerminalStub(), password="", compactor_factory=lambda e: _Stopped(e)
    )
    started = app.start_turn(None, "hi")
    assert started.notice == "這個 session 的自動壓縮已停用"
    backlog = _wait_for(app, started.session, lambda b: any(e["type"] == "step_finish" for e in b))
    assert backlog[0]["type"] == "notice" and backlog[0]["seq"] == 1
    assert started.cursor == 0


@pytest.mark.smoke
def test_resuming_never_creates_a_session_file_first():
    """factory 能直接以既有 id 建 engine:resume 不得先 create 再刪。"""
    created: list[str] = []

    class _CountingStore(_Store):
        def create(self, *_a, **_k):
            created.append("create")
            return "should-not-happen"

    store = _CountingStore()

    def _factory(session_id=None):
        engine = _StubEngine()
        engine.store = store
        if session_id:
            engine.session_id = session_id
        return engine

    app = client_web.WebApp(_factory, password="", store=store)
    engine = app.engine_for("20260101T000000-aaaaaaaa")
    assert engine.session_id == "20260101T000000-aaaaaaaa"
    assert created == []
    assert store.deleted == []


@pytest.mark.smoke
def test_a_rebound_host_header_is_refused_even_when_origin_matches(served):
    """DNS rebinding:Origin 與 Host 都是攻擊者的網域,相等擋不住;要對照 bind identity。"""
    _app, base = served
    headers = {
        "Authorization": "Bearer s3cret",
        "Host": "evil.example",
        "Origin": "http://evil.example",
    }
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(base + "/api/message", {"text": "hi"}, headers=headers)
    assert excinfo.value.code == 403
    ok = _post(base + "/api/message", {"text": "hi"}, headers={"Authorization": "Bearer s3cret"})
    assert ok.status == 202


def test_host_aliases_cover_the_loopback_spellings():
    aliases = client_web.host_aliases("127.0.0.1", 4096)
    assert {"127.0.0.1:4096", "localhost:4096", "[::1]:4096", "localhost"} <= aliases
    assert "evil.example:4096" not in aliases


@pytest.mark.smoke
def test_attach_follows_a_turn_from_its_cursor(monkeypatch):
    """attach 訂閱時要帶 `after=<cursor>`,否則第二題會讀到第一題的舊 terminal。"""
    import client_attach

    seen: list[str] = []

    def _fake_stream(url, password):
        seen.append(url)
        yield client_events.step_finish_event("s", reason=client_events.REASON_STOP)

    monkeypatch.setattr(client_attach, "_stream", _fake_stream)
    client_attach._follow("http://127.0.0.1:1", "20260101T000000-abcdef01", "", after=7)
    assert seen and "after=7" in seen[0]


@pytest.mark.smoke
def test_cors_origins_are_an_explicit_allowlist(open_served):
    """`--cors` 沒列的來源:同源檢查照樣擋、也不回任何 Access-Control 標頭。"""
    app, base = open_served
    app.allowed_origins = frozenset({"https://browser.example"})
    ok = _post(base + "/api/message", {"text": "hi"}, headers={"Origin": "https://browser.example"})
    assert ok.status == 202
    assert ok.headers.get("Access-Control-Allow-Origin") == "https://browser.example"
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(base + "/api/message", {"text": "hi"}, headers={"Origin": "https://other.example"})
    assert excinfo.value.code == 403
    assert excinfo.value.headers.get("Access-Control-Allow-Origin") is None


# ============================================================
# 總審第 2 輪回修:取消競態、wildcard Host、SSE 游標 / 重連、瀏覽器 resume
# ============================================================
@pytest.mark.smoke
def test_a_cancel_during_the_compaction_phase_reaches_the_engine_and_ends_with_the_turn():
    """send() 已返回、壓縮還在跑:取消要送到 engine(壓縮串流會看旗標),
    而這一輪結束後的取消是 no-op(不得留旗標給下一題)。"""
    gate = threading.Event()
    reached = threading.Event()

    class _Cancellable(_TerminalStub):
        def __init__(self):
            super().__init__()
            self.cancels = 0

        def cancel(self):
            self.cancels += 1
            return True

    class _SlowCompactor(_StubCompactor):
        def compact(self, *, manual=False):
            reached.set()
            gate.wait(5)
            return _Outcome("skipped", "")

    engines: list[_Cancellable] = []

    def _factory():
        engines.append(_Cancellable())
        return engines[-1]

    app = client_web.WebApp(_factory, password="", compactor_factory=lambda e: _SlowCompactor(e))
    target = app.send(None, "hi")
    assert reached.wait(5)
    assert app.cancel(target) is True          # 壓縮進行中:取消要送到 engine
    gate.set()
    _wait_for(app, target, lambda b: any(e["type"] == "step_finish" for e in b))
    assert engines[0].cancels == 1
    assert app.cancel(target) is False         # 這一輪已結束:no-op


@pytest.mark.smoke
def test_an_approval_registered_after_the_cancel_is_refused_immediately():
    """取消落在「engine 決定要問」與「pending 登記」之間的空窗:登記完要再看一次。"""
    app = client_web.WebApp(lambda: _StubEngine(), password="")
    target = app.engine_for(None).session_id
    app._sessions[target].cancelled = True      # cancel() 已經跑過、但 pending 還沒登記
    request = client_engine.ApprovalRequest(target, "apply_patch", {"diff": "x"})
    assert app.request_approval(request) is False
    assert app._approvals == {}


def test_wildcard_binds_do_not_restrict_the_host_header():
    """綁 0.0.0.0 時遠端瀏覽器送來的是 LAN IP / 主機名,列舉不完;密碼才是那條邊界。"""
    assert client_web.host_aliases("0.0.0.0", 4096) == frozenset()
    assert client_web.host_aliases("::", 4096) == frozenset()
    assert "127.0.0.1:4096" in client_web.host_aliases("127.0.0.1", 4096)


@pytest.mark.smoke
def test_a_password_protected_wildcard_bind_accepts_the_real_lan_host():
    app = client_web.WebApp(lambda: _TerminalStub(), password="s3cret")
    server = client_web.serve(app, host="0.0.0.0", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        response = _post(base + "/api/message", {"text": "hi"},
                         headers={"Authorization": "Bearer s3cret", "Host": "lan-box.example:4096"})
        assert response.status == 202
    finally:
        server.shutdown()
        server.server_close()


def _read_sse(base, path, headers=None, lines=6, timeout=5):
    request = urllib.request.Request(base + path, headers=headers or {})
    response = urllib.request.urlopen(request, timeout=timeout)
    out = []
    try:
        for _ in range(lines):
            line = response.readline()
            if not line:
                break
            out.append(line.decode("utf-8"))
    finally:
        response.close()
    return response.headers, out


@pytest.mark.smoke
def test_sse_events_carry_ids_and_reconnects_resume_from_last_event_id(open_served):
    """EventSource 重連會帶 Last-Event-ID:從那之後接,不重播整份 backlog。"""
    _app, base = open_served
    payload = json.loads(_post(base + "/api/message", {"text": "hi"}).read())
    session = payload["session"]
    for _ in range(300):
        _channel, backlog = _app.subscribe(session)
        _app.unsubscribe(session, _channel)
        if any(e["type"] == "step_finish" for e in backlog):
            break
        time.sleep(0.01)
    _headers, lines = _read_sse(base, f"/api/events?session={session}", lines=4)
    assert lines[0].startswith("id: 1")
    assert lines[1].startswith("data: ")
    # 重連時帶「倒數第二則」的 id:只該重播最後一則,而不是整份 backlog。
    _headers, again = _read_sse(base, f"/api/events?session={session}",
                                headers={"Last-Event-ID": str(len(backlog) - 1)}, lines=1)
    assert again[0].strip() == f"id: {len(backlog)}"


def test_sse_carries_cors_headers_for_an_allowed_origin(open_served):
    app, base = open_served
    app.allowed_origins = frozenset({"https://browser.example"})
    session = json.loads(_post(base + "/api/message", {"text": "hi"}).read())["session"]
    headers, _lines = _read_sse(base, f"/api/events?session={session}",
                                headers={"Origin": "https://browser.example"}, lines=1)
    assert headers.get("Access-Control-Allow-Origin") == "https://browser.example"


@pytest.mark.smoke
def test_the_browser_resume_api_loads_the_session_and_returns_a_cursor():
    """cold /resume:先載入、回游標與最近幾則,JS 才有東西可以接;未載入就開
    EventSource 會拿到一個沒掛進 listener 的 queue,畫面從此收不到事件。"""
    store = _Store()

    def _factory(session_id=None):
        engine = _StubEngine()
        engine.store = store
        if session_id:
            engine.session_id = session_id
            engine.messages = [
                {"role": "user", "content": "舊問題"},
                {"role": "assistant", "content": "舊回答"},
                {"role": "user", "content": "摘要", "synthetic": True},
            ]
        return engine

    app = client_web.WebApp(_factory, password="", store=store)
    loaded = app.resume_session("20260101T000000-aaaaaaaa")
    assert loaded["session"] == "20260101T000000-aaaaaaaa" and loaded["cursor"] == 0
    assert loaded["messages"] == [{"role": "user", "text": "舊問題"}, {"role": "assistant", "text": "舊回答"}]
    channel, _backlog = app.subscribe("20260101T000000-aaaaaaaa")
    assert channel in app._sessions["20260101T000000-aaaaaaaa"].listeners


def test_a_store_error_on_resume_is_a_404_not_a_traceback(open_served):
    app, base = open_served

    class _Broken:
        def read(self, session_id):
            raise client_store.SessionStoreError("session 檔不存在")

        def list_sessions(self):
            return []

    app._store = _Broken()
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(base + "/api/resume", {"session": "20260101T000000-ffffffff"})
    assert excinfo.value.code == 404


def test_the_stop_notice_is_not_duplicated_in_the_post_body(open_served):
    app, base = open_served

    class _Stopped(_StubCompactor):
        def pending_stop_notice(self):
            return "這個 session 的自動壓縮已停用"

    app._compactor_factory = lambda e: _Stopped(e)
    body = json.loads(_post(base + "/api/message", {"text": "hi"}).read())
    assert "notice" not in body
    _channel, backlog = app.subscribe(body["session"])
    assert backlog[0]["type"] == "notice"


# ── 總審第 3 輪回修(F3-1 / F3-3 / F3-4)──

@pytest.mark.smoke
def test_a_manual_compaction_can_be_cancelled_from_the_api():
    """`/compact` 也是一輪:長摘要按取消要送到 engine,不是回 ok:false;旗標隨這一輪一起清。"""
    gate = threading.Event()
    reached = threading.Event()

    class _Cancellable(_StubEngine):
        def __init__(self):
            super().__init__()
            self.cancels = 0
            self.cleared = 0

        def cancel(self):
            self.cancels += 1
            return True

        def clear_cancel(self):
            self.cleared += 1

    class _SlowCompactor(_StubCompactor):
        def compact(self, *, manual=False):
            reached.set()
            gate.wait(5)
            return _Outcome("skipped", "已中斷")

    engines: list[_Cancellable] = []

    def _factory():
        engines.append(_Cancellable())
        return engines[-1]

    app = client_web.WebApp(_factory, password="", compactor_factory=lambda e: _SlowCompactor(e))
    target = app.engine_for(None).session_id
    outcome: dict = {}
    thread = threading.Thread(target=lambda: outcome.setdefault("message", app.compact(target)))
    thread.start()
    assert reached.wait(5)
    assert app.cancel(target) is True          # 摘要進行中:取消要送到 engine
    gate.set()
    thread.join(5)
    assert outcome["message"] == "已中斷"
    assert engines[0].cancels == 1
    assert engines[0].cleared == 1             # 旗標隨這一輪清掉,不留給下一題
    assert app.cancel(target) is False


@pytest.mark.smoke
def test_a_cancel_that_races_the_end_of_the_turn_never_poisons_the_next_one():
    """worker 正在收尾(turn_done=True → 清旗標)時 cancel() 進來:兩邊必須互斥——
    否則 cancel() 判定「還沒結束」、等 worker 清完旗標才補設,下一題一開始就被誤殺。"""

    class _Recording(_StubEngine):
        def __init__(self):
            super().__init__()
            self.flag = False

        def cancel(self):
            self.flag = True
            return True

        def clear_cancel(self):
            self.flag = False

    engines: list[_Recording] = []

    def _factory():
        engines.append(_Recording())
        return engines[-1]

    app = client_web.WebApp(_factory, password="")
    real_lock = app._lock
    entered = threading.Event()
    proceed = threading.Event()

    class _Probe:
        """worker 一進 _finish_turn 的臨界區就停住,讓 cancel() 排在後面。"""

        def __enter__(self):
            real_lock.acquire()
            if threading.current_thread().name.startswith("codetrail-turn-") and any(
                frame.name == "_finish_turn" for frame in traceback.extract_stack()
            ):
                entered.set()
                proceed.wait(5)
            return self

        def __exit__(self, *_exc):
            real_lock.release()
            return False

    app._lock = _Probe()
    target = app.send(None, "hi")
    assert entered.wait(5)
    results: list[bool] = []
    canceller = threading.Thread(target=lambda: results.append(app.cancel(target)))
    canceller.start()
    time.sleep(0.05)                           # cancel() 現在卡在 app lock 上
    proceed.set()
    canceller.join(5)
    assert results == [False]                  # 收尾已在進行:取消是 no-op
    assert engines[0].flag is False            # 旗標沒有留給下一題


@pytest.mark.smoke
def test_a_browser_reconnect_prefers_last_event_id_over_the_url_cursor(open_served):
    """瀏覽器重連用的是同一個 URL(帶初始 after=N),另外送 Last-Event-ID=M:要從 M 接,
    不然回答文字、notice、tool event 全部重播一次。"""
    _app, base = open_served
    payload = json.loads(_post(base + "/api/message", {"text": "hi"}).read())
    session = payload["session"]
    for _ in range(300):
        _channel, backlog = _app.subscribe(session)
        _app.unsubscribe(session, _channel)
        if any(e["type"] == "step_finish" for e in backlog):
            break
        time.sleep(0.01)
    _headers, again = _read_sse(
        base, f"/api/events?session={session}&after=0",
        headers={"Last-Event-ID": str(len(backlog) - 1)}, lines=1,
    )
    assert again[0].strip() == f"id: {len(backlog)}"


@pytest.mark.smoke
def test_a_cors_front_end_can_log_in_and_stream_with_the_returned_token(served):
    """--cors 明列的來源 + 密碼:原生 EventSource 不能帶 Authorization,登入回的 token
    要能放在 /api/events?token= 上;登入與 SSE 回應都要帶 credentialed CORS 標頭。
    query token 只對明列來源有效——沒有 Origin 的直接請求一律走 cookie / Bearer。"""
    app, base = served
    app.allowed_origins = {"https://browser.example"}
    origin = {"Origin": "https://browser.example"}
    response = _post(base + "/api/login", {"password": "s3cret"}, headers=origin)
    assert response.status == 200
    assert response.headers.get("Access-Control-Allow-Origin") == "https://browser.example"
    assert response.headers.get("Access-Control-Allow-Credentials") == "true"
    token = json.loads(response.read())["token"]
    assert token and token != "s3cret"
    payload = json.loads(_post(base + "/api/message", {"text": "hi"},
                               headers={"Authorization": "Bearer s3cret"}).read())
    session = payload["session"]
    for _ in range(300):
        _channel, backlog = app.subscribe(session)
        app.unsubscribe(session, _channel)
        if any(e["type"] == "step_finish" for e in backlog):
            break
        time.sleep(0.01)
    headers, lines = _read_sse(base, f"/api/events?session={session}&token={token}",
                               headers=origin, lines=1)
    assert headers.get("Access-Control-Allow-Origin") == "https://browser.example"
    assert lines[0].startswith("id: 1")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _read_sse(base, f"/api/events?session={session}&token={token}", lines=1)
    assert excinfo.value.code == 401


# ── 總審第 4 輪回修(F4-1 / F4-3)──

@pytest.mark.smoke
def test_a_cancel_arriving_while_the_turn_is_being_started_is_not_lost():
    """POST 已拿到 turn_lock、還沒標 turn_done=False 的空窗:以前 cancel() 看到上一輪留下的
    turn_done=True 回 False,這一次點擊就漏掉。現在取鎖與「開始」在同一個 app lock 臨界區。"""

    class _Recording(_StubEngine):
        def __init__(self):
            super().__init__()
            self.flag = False

        def cancel(self):
            self.flag = True
            return True

        def clear_cancel(self):
            self.flag = False

    engines: list[_Recording] = []

    def _factory():
        engines.append(_Recording())
        return engines[-1]

    app = client_web.WebApp(_factory, password="")
    first = app.send(None, "warm-up")                 # 留下 turn_done=True 的上一輪
    _wait_for(app, first, lambda b: any(e["type"] == "step_finish" for e in b))
    real_lock = app._lock
    entered = threading.Event()
    proceed = threading.Event()

    class _Probe:
        def __enter__(self):
            real_lock.acquire()
            if any(f.name == "_begin_turn" for f in traceback.extract_stack()) and not entered.is_set():
                entered.set()
                proceed.wait(5)                     # 讓 cancel() 排在「開始」臨界區後面
            return self

        def __exit__(self, *_exc):
            real_lock.release()
            return False

    app._lock = _Probe()
    results: list[bool] = []
    starter = threading.Thread(target=lambda: app.send(first, "second"))
    starter.start()
    assert entered.wait(5)
    canceller = threading.Thread(target=lambda: results.append(app.cancel(first)))
    canceller.start()
    time.sleep(0.05)
    proceed.set()
    canceller.join(5)
    starter.join(5)
    assert results == [True]                        # 取消命中這一輪,沒有漏掉
    _wait_for(app, first, lambda b: sum(1 for e in b if e["type"] == "step_finish") >= 2)


@pytest.mark.smoke
def test_the_slow_mcp_cancel_does_not_hold_the_app_lock():
    """MCP 取消要等寬限期(最長 10 秒):它必須在 app lock 外進行,不然其他 session 的
    所有操作(subscribe / start_turn / sessions)都跟著卡住。"""
    blocked = threading.Event()
    reached = threading.Event()

    class _Pending:
        def cancel(self, reason):
            reached.set()
            blocked.wait(5)

    class _SplitEngine(_StubEngine):
        def __init__(self):
            super().__init__()
            self.pending = _Pending()

        def request_cancel(self, *, arm_when_idle=False):
            return client_engine.CancelDecision(True, self.pending)

        @staticmethod
        def cancel_pending(call):
            call.cancel("cancelled by user")
            return True

        def send(self, text, *, on_event=None, approve=None, **_kwargs):
            self.sent.append(text)
            blocked.wait(5)
            raise client_engine.TurnCancelled("user")

    app = client_web.WebApp(lambda: _SplitEngine(), password="")
    target = app.send(None, "hi")
    canceller = threading.Thread(target=lambda: app.cancel(target))
    canceller.start()
    assert reached.wait(5)                          # 慢速取消正在等寬限期
    assert app._lock.acquire(timeout=1)             # app lock 沒被它扣住
    app._lock.release()
    blocked.set()
    canceller.join(5)
    _wait_for(app, target, lambda b: any(e["type"] == "step_finish" for e in b))


@pytest.mark.smoke
def test_a_web_started_with_a_session_resumes_it_by_default():
    """`codetrail_chat.py web --session X`:第一個沒指定 session 的請求要接續 X,
    以前 args.session 被靜默忽略、每次都開一個空白對話。"""
    made: list[str | None] = []

    def _factory(session_id=None):
        made.append(session_id)
        engine = _StubEngine()
        if session_id:
            engine.session_id = session_id
        return engine

    app = client_web.WebApp(_factory, password="")
    app.default_session = "20260101T000000-abcdef01"
    assert app.engine_for(None).session_id == "20260101T000000-abcdef01"
    assert made == ["20260101T000000-abcdef01"]
    assert app.send(None, "hi") == "20260101T000000-abcdef01"


# ── 總審第 9 輪回修(F9-1 的 web 面):答案寫定之後的取消不算數;壓縮階段的取消 → terminal cancelled ──

@pytest.mark.smoke
def test_a_cancel_after_the_answer_is_committed_but_before_compaction_is_refused():
    """engine 已把答案決定寫定、send() 還沒返回:/api/cancel 要回 False、不動 engine
    (接不接受由 engine 的 request_cancel 原子決定);以前 web 自己 snapshot、回 True 卻保留答案。"""
    gate = threading.Event()

    class _Committed(_StubEngine):
        def __init__(self):
            super().__init__()
            self.turn_completed = False
            self.cancels = 0

        def request_cancel(self, *, arm_when_idle=False):
            if self.turn_completed:
                return client_engine.CancelDecision(False, None)
            self.cancels += 1
            return client_engine.CancelDecision(True, None)

        def send(self, text, *, on_event=None, approve=None, **_kwargs):
            self.sent.append(text)
            self.turn_completed = True          # 答案已寫進歷史
            gate.wait(5)                        # 還沒返回
            return _Result()

    engines: list[_Committed] = []

    def _factory():
        engines.append(_Committed())
        return engines[-1]

    app = client_web.WebApp(_factory, password="")
    target = app.send(None, "hi")
    for _ in range(200):
        if engines and engines[0].turn_completed:
            break
        time.sleep(0.01)
    assert app.cancel(target) is False
    assert engines[0].cancels == 0
    gate.set()
    _wait_for(app, target, lambda b: any(e["type"] == "step_finish" for e in b))
    _channel, backlog = app.subscribe(target)
    app.unsubscribe(target, _channel)
    assert [e["part"]["reason"] for e in backlog if e["type"] == "step_finish"] == [client_events.REASON_STOP]


@pytest.mark.smoke
def test_a_cancel_accepted_during_compaction_ends_with_a_cancelled_terminal():
    """壓縮進行中的取消算數(回 True、送到 engine):那這一輪的 terminal 就必須是
    cancelled——不能回 True 之後還送 step_finish(stop)。"""
    gate = threading.Event()
    reached = threading.Event()

    class _Cancellable(_TerminalStub):
        def __init__(self):
            super().__init__()
            self.cancels = 0

        def request_cancel(self, *, arm_when_idle=False):
            self.cancels += 1
            return client_engine.CancelDecision(True, None)

    class _SlowCompactor(_StubCompactor):
        def compact(self, *, manual=False):
            reached.set()
            gate.wait(5)
            return _Outcome("skipped", "cancelled")

    engines: list[_Cancellable] = []

    def _factory():
        engines.append(_Cancellable())
        return engines[-1]

    app = client_web.WebApp(_factory, password="", compactor_factory=lambda e: _SlowCompactor(e))
    target = app.send(None, "hi")
    assert reached.wait(5)
    assert app.cancel(target) is True
    gate.set()
    _wait_for(app, target, lambda b: any(e["type"] == "step_finish" for e in b))
    _channel, backlog = app.subscribe(target)
    app.unsubscribe(target, _channel)
    reasons = [e["part"]["reason"] for e in backlog if e["type"] == "step_finish"]
    assert reasons == [client_events.REASON_CANCELLED]
    assert any(e.get("message") == "答案已完成;壓縮已取消。" for e in backlog if e["type"] == "notice")
    assert engines[0].cancels == 1
    assert app.cancel(target) is False


# ── 總審第 12 輪回修(F12-1):手動 /compact 的 preflight 期間取消,web 回 True 就必須真的中斷 ──

@pytest.mark.smoke
def test_a_cancel_during_the_manual_compaction_preflight_is_consumed_not_lost(tmp_path, monkeypatch):
    """真 Engine + 真 Compactor:/compact 對一個還沒有完整回答的 session,preflight 期間按取消
    → /api/cancel 回 True,而且 /compact 的回應是「壓縮被中斷」——不是 ok:true 之後照回
    「還沒有已完成的回合」。"""
    import client_compaction as cc
    import client_policy
    import client_prompt
    import client_store

    class _NoMcp:
        def tools(self):
            return ()

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "project"
    root.mkdir()
    options = client_engine.EngineOptions(
        root=root, model="test-model", base_url="http://127.0.0.1:65535", n_ctx=131072,
        policy=client_policy.InteractivePolicy(),
    )
    engine = client_engine.Engine(
        options, mcp=_NoMcp(), store=client_store.EphemeralSessionStore(root),
        system_prompt=client_prompt.SystemPrompt(text="SYSTEM"),
    )
    engine.messages = [{"role": "user", "content": "q"}]
    reached = threading.Event()
    proceed = threading.Event()

    def _factory(session_id=None):
        return engine

    def _compactor(_engine):
        compactor = cc.Compactor(_engine, cc.MODE_MANUAL, n_ctx=131072)
        real_anchor = compactor.anchor

        def _slow_anchor():
            reached.set()
            proceed.wait(5)                  # 放大 preflight 的窗口
            return real_anchor()

        compactor.anchor = _slow_anchor
        return compactor

    app = client_web.WebApp(_factory, password="", compactor_factory=_compactor)
    target = app.engine_for(None).session_id
    outcome: dict = {}
    worker = threading.Thread(target=lambda: outcome.setdefault("message", app.compact(target)))
    worker.start()
    assert reached.wait(5)
    assert app.cancel(target) is True        # preflight 期間:進行中的 turn,接受
    proceed.set()
    worker.join(5)
    assert outcome["message"] == "壓縮被中斷,對話維持原狀。"
    assert app.cancel(target) is False
