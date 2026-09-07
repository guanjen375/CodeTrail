"""Scoped, cancellable requests transport for background prompt-cache priming.

The shared HTTP session is untouched. Each socket is registered before HTTP bytes
can be sent, including sockets whose connect finishes after cancellation. Closing
a response alone cannot interrupt a read waiting for response headers; shutdown
of the registered socket does. DNS/connect/TLS may finish later, but cannot send
an HTTP request after cancellation.
"""
from __future__ import annotations

import contextlib
import socket
import threading

from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from http_client import create_session


class RequestCancelled(Exception):
    """The owner cancelled this request; no further HTTP bytes may be sent."""


def _shutdown(sock) -> None:
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()


class RequestCancellation:
    """One prime's GET/POST cancellation and dedicated session ownership.

    ``cancel()`` returns only after all registered sockets have been shut down.
    Concurrent callers wait for that same shutdown; observing the event alone is
    insufficient to release the model lease. Registration after cancellation
    closes the late socket and raises, so a delayed connect cannot become a POST.
    """

    def __init__(self) -> None:
        self.event = threading.Event()
        self._guard = threading.Lock()
        self._sockets: set = set()
        self._session = None

    def check(self) -> None:
        if self.event.is_set():
            raise RequestCancelled("background request cancelled")

    def register(self, sock) -> None:
        with self._guard:
            if self.event.is_set():
                _shutdown(sock)
                raise RequestCancelled("background request cancelled")
            self._sockets.add(sock)

    def cancel(self) -> None:
        with self._guard:
            self.event.set()
            for sock in self._sockets:
                _shutdown(sock)
            self._sockets.clear()

    def session(self):
        with self._guard:
            self.check()
            if self._session is None:
                session = create_session()
                # Preserve trust_env=False, TLS verification and no redirects.
                # This optional one-shot warmup never retries a cancelled POST.
                for adapter in set(session.adapters.values()):
                    adapter.close()
                adapter = _CancellationAdapter(self, max_retries=0)
                session.mount("http://", adapter)
                session.mount("https://", adapter)
                self._session = session
            return self._session

    def close(self) -> None:
        self.cancel()
        with self._guard:
            session, self._session = self._session, None
        if session is not None:
            session.close()


class _CancellationAdapter(HTTPAdapter):
    def __init__(self, cancellation: RequestCancellation, **kwargs) -> None:
        self._cancellation = cancellation
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        super().init_poolmanager(connections, maxsize, block=block, **kwargs)
        cancellation = self._cancellation

        class CancellableConnection:
            def _new_conn(self):
                cancellation.check()
                sock = super()._new_conn()
                cancellation.register(sock)
                return sock

            def send(self, data):
                cancellation.check()
                if self.sock is None:
                    self.connect()
                # HTTPS wraps/replaces the TCP socket during connect. Register
                # the final SSLSocket as well, before headers or body are sent.
                cancellation.register(self.sock)
                return super().send(data)

        class CancelHTTPConnection(CancellableConnection, HTTPConnection):
            pass

        class CancelHTTPSConnection(CancellableConnection, HTTPSConnection):
            pass

        class CancelHTTPPool(HTTPConnectionPool):
            ConnectionCls = CancelHTTPConnection

        class CancelHTTPSPool(HTTPSConnectionPool):
            ConnectionCls = CancelHTTPSConnection

        # PoolManager's default mapping is shared across instances. Never mutate
        # it in place: normal rounds and other engines keep their own transport.
        self.poolmanager.pool_classes_by_scheme = {
            "http": CancelHTTPPool,
            "https": CancelHTTPSPool,
        }
