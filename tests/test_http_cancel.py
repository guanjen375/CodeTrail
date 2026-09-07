"""預熱專用 transport 的安全契約；只用離線 socket，不建立任何網路連線。"""
import threading

import pytest
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.poolmanager import pool_classes_by_scheme

import http_client
from http_cancel import RequestCancellation, RequestCancelled

pytestmark = pytest.mark.smoke


class Socket:
    def __init__(self):
        self.sent = []
        self.shut = False
        self.closed = False

    def sendall(self, data):
        if self.shut:
            raise OSError("offline socket shut down")
        self.sent.append(bytes(data))

    def shutdown(self, _how):
        self.shut = True

    def close(self):
        self.closed = True


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_cancellation_closes_a_late_connection_before_any_http_bytes(monkeypatch, scheme):
    """取消時 DNS/connect 尚未回：晚回的 socket 先關，永不送出原請求。"""
    request = RequestCancellation()
    session = request.session()
    pool = session.get_adapter(f"{scheme}://").poolmanager.connection_from_url(f"{scheme}://localhost")
    connection = pool._new_conn()
    sock = Socket()
    entered, release = threading.Event(), threading.Event()

    def late_connect(_self):
        entered.set()
        release.wait(2)
        return sock

    monkeypatch.setattr(HTTPConnection, "_new_conn", late_connect)
    results = []

    def send():
        try:
            connection.send(b"POST /v1/chat/completions HTTP/1.1\r\n\r\n")
        except Exception as exc:
            results.append(exc)

    worker = threading.Thread(target=send)
    worker.start()
    try:
        assert entered.wait(1)
        request.cancel()
    finally:
        release.set()
        worker.join(2)
        request.close()
    assert not worker.is_alive()
    assert len(results) == 1 and isinstance(results[0], RequestCancelled)
    assert sock.shut and sock.closed
    assert sock.sent == []


def test_cancellation_owns_the_final_tls_socket_and_preserves_shared_transport(monkeypatch):
    """TLS 換過 socket 仍能關；不改共用 pool、驗證、proxy/netrc 或 redirect 邊界。"""
    shared = http_client.get_session()
    shared_adapters = dict(shared.adapters)
    shared_pools = dict(pool_classes_by_scheme)
    request = RequestCancellation()
    session = request.session()
    assert session is not shared
    assert session.trust_env is False and session.verify is True
    assert session.max_redirects == 0
    assert session.get_adapter("https://").max_retries.total == 0
    pool = session.get_adapter("https://").poolmanager.connection_from_url("https://localhost")
    connection = pool._new_conn()
    final_tls_socket = Socket()
    monkeypatch.setattr(HTTPSConnection, "connect", lambda self: setattr(self, "sock", final_tls_socket))
    try:
        connection.send(b"headers")
        request.cancel()
        with pytest.raises(RequestCancelled):
            connection.send(b"late body")
        assert final_tls_socket.sent == [b"headers"]
        assert final_tls_socket.shut and final_tls_socket.closed
        assert http_client.get_session() is shared
        assert dict(shared.adapters) == shared_adapters
        assert dict(pool_classes_by_scheme) == shared_pools
    finally:
        request.close()


def test_concurrent_cancellation_waits_for_the_same_socket_shutdown():
    """看見取消 Event 不等於 HTTP 已關；第二個清理者也必須等 shutdown 完成。"""
    entered, release = threading.Event(), threading.Event()
    second_started, second_done = threading.Event(), threading.Event()

    class SlowSocket(Socket):
        def shutdown(self, how):
            entered.set()
            release.wait(2)
            super().shutdown(how)

    request = RequestCancellation()
    sock = SlowSocket()
    request.register(sock)
    first = threading.Thread(target=request.cancel)

    def close():
        second_started.set()
        request.close()
        second_done.set()

    second = threading.Thread(target=close)
    first.start()
    try:
        assert entered.wait(1)
        second.start()
        assert second_started.wait(1)
        assert not second_done.wait(0.05)
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert second_done.is_set() and sock.shut and sock.closed
