#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_mcp — CodeTrail 客戶端用的 MCP stdio client(同步、無 event loop)。

為什麼不 in-process import ``mcp_server``:那份模組在 import 期就會驗 root、
載入 KnowledgeBase、跑 model preflight,失敗時直接 ``sys.exit``;而且
``ingest_document`` 的 worker offload 與 progress 綁在 FastMCP 的 ``Context``
上。所以客戶端一律以子行程啟動既有的 ``mcp_server.py``,協定與 OpenCode 走的
是同一條 stdio JSON-RPC。

為什麼**不用 SDK 的 ``ClientSession``**(這是對施工計畫方向 1 字面的偏離,
理由寫在這裡):

  1. **取消契約做不出來。** ``BaseSession.send_request`` 在 read timeout 或
     呼叫端 task 被取消時都不送 ``notifications/cancelled``
     (``tests/test_client_mcp.py`` 用契約測試釘住這件事),而 server 端的
     ingest 回收只在 handler 真的被取消時才觸發。SDK 也沒有任何公開途徑可以
     問「這一次呼叫的 request id 是多少」。用它就必須攔送出端去猜 id,或讀
     ``_response_streams`` 這類私有欄位 —— 兩者都會在 ``mcp>=1.28,<2`` 的
     允許範圍內無聲漂移。
  2. **它逼出一個跨執行緒的 event loop。** ClientSession 是 async 的,而終端
     REPL 是同步的,於是要在背景執行緒跑一個 loop 並靠
     ``run_coroutine_threadsafe`` 喚醒它。審核在受限 sandbox 裡實測到那個喚醒
     不會發生:主執行緒停在 ``future.result()``、loop 執行緒停在
     ``selectors.select``,initialize 一個 byte 都沒送出。

這裡的作法:自己送那六種訊息(``initialize`` / ``notifications/initialized`` /
``tools/list`` / ``tools/call`` / ``notifications/cancelled`` / 收
``notifications/progress``),**request id 由我們自己配發**,所以「送出前就知道
id」是構造上的保證,不是靠攔截。協定常數仍取自 ``mcp.types``,不另外抄一份。

**一個 engine 行程(一個 AICODE_ROOT)只起一個 instance**,該行程內所有對話
共用它。``ingest`` 的 busy 閘是 server 行程內的狀態,只有共用同一個 instance
才維持得住「ingest 期間其他 KB 工具立刻回 busy」的既有語意。

取消契約
--------------------------------------------------------------------
1. Ctrl-C 與 timeout 兩種情況都送 ``notifications/cancelled``,並在寬限期內
   等 server 回覆(SDK server 收到取消會回一則 ``Request cancelled`` 錯誤)。
2. 寬限期過仍無回應 → SIGTERM 該 instance(``mcp_server`` 自己的 SIGTERM
   handler 會收 ingest 子行程與 lease),把該 instance **所有**進行中的呼叫
   回成 error,再重新 spawn。

stderr 政策
--------------------------------------------------------------------
MCP server 的 stderr 含 strict 查詢的串流回答、截圖抽取內容、命令與絕對路徑。
預設**只保留有上限的記憶體尾端**,不落任何檔案;失敗時印給本機使用者看。
需要原始 log 時用 ``CODETRAIL_MCP_STDERR_LOG`` 明確指定檔案,owner-only 建檔
並印出「含 NDA 內容」警告。
"""
from __future__ import annotations

import contextlib
import errno
import itertools
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from mcp.types import LATEST_PROTOCOL_VERSION

import config
from mcp_contract import PUBLIC_TOOL_ORDER

REPO_ROOT = Path(__file__).resolve().parent
SERVER_SCRIPT = REPO_ROOT / "mcp_server.py"

#: 送出 ``notifications/cancelled`` 之後等 server 回覆的寬限秒數。
CANCEL_GRACE_SECONDS = 10.0

#: SIGTERM 之後等行程結束、再對 process group SIGKILL 的寬限秒數。
TERMINATE_GRACE_SECONDS = 5.0

#: 記憶體裡保留的 stderr 尾端上限(bytes)。只在失敗時印給本機使用者。
STDERR_TAIL_BYTES = 16_384

#: 明確指定 MCP stderr 落檔位置的 env。設了才落檔,且會印出 NDA 警告。
STDERR_LOG_ENV = "CODETRAIL_MCP_STDERR_LOG"

#: OpenCode 會替 MCP 工具加 ``codetrail_`` 前綴;CodeTrail 自己的客戶端不加。
FORBIDDEN_TOOL_PREFIX = "codetrail_"

CLIENT_INFO = {"name": "codetrail-client", "version": "1"}

_HEARTBEAT_SECONDS = 15.0


def call_timeout_seconds() -> float:
    """每一次 ``tools/call`` 的固定 read timeout。

    **沒有 per-call override。** 動態讀 ``config`` 是刻意的(AGENTS.md §3):
    唯一的來源是 ``config.MCP_CALL_TIMEOUT_SECONDS``,測試要縮短就
    monkeypatch 那一個常數,而不是在呼叫端開一個可以放寬的參數 —— 一個能被
    呼叫端調小的 timeout 等於沒有「≥ 660 秒」這條契約:ingest 還在跑就被
    client 端放棄,而 server 那邊照樣寫完 knowledge.json。
    """
    return float(config.MCP_CALL_TIMEOUT_SECONDS)


class McpClientError(RuntimeError):
    """MCP client 層的錯誤基底。"""


class McpUnavailableError(McpClientError):
    """instance 不在了(啟動失敗、被 SIGTERM 收掉、或 stdio 已關閉)。"""


class McpCallCancelledError(McpClientError):
    """呼叫被使用者取消(Ctrl-C)。"""


class McpCallTimeoutError(McpClientError):
    """呼叫超過固定 read timeout。"""


class McpToolError(McpClientError):
    """server 以 JSON-RPC error 回覆這一次呼叫。"""


@dataclass(frozen=True)
class ToolSpec:
    """一個 MCP 工具的模型可見契約。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool

    def as_openai_tool(self) -> dict[str, Any]:
        """轉成 llama-server ``/v1/chat/completions`` 吃的 function tool。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.input_schema),
            },
        }


@dataclass(frozen=True)
class ToolCallResult:
    """一次 ``tools/call`` 的結果。

    ``text`` 是唯一會餵進模型的東西(所有 text block 串起來)。
    ``structured`` 只給 UI / eval —— evidence 工具的 ``structuredContent`` 是
    另一份完整資料,重複餵給模型等於把同一份證據算兩次 context。
    """

    name: str
    text: str
    structured: Any | None
    is_error: bool


def _text_blocks(content: Iterable[Any]) -> str:
    parts: list[str] = []
    for block in content or ():
        if not isinstance(block, Mapping):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


class _Call:
    """一次進行中的請求。id 在送出**之前**就配好,所以永遠有得取消。"""

    __slots__ = ("request_id", "name", "done", "result", "error", "cancel_sent", "on_progress")

    def __init__(self, request_id: int, name: str,
                 on_progress: Callable[[float, float | None, str | None], None] | None) -> None:
        self.request_id = request_id
        self.name = name
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None
        self.cancel_sent = False
        self.on_progress = on_progress


class PendingCall:
    """一次進行中的 ``tools/call``。可從任何執行緒等待或取消。"""

    def __init__(self, client: "McpClient", call: _Call) -> None:
        self._client = client
        self._call = call
        self.name = call.name

    @property
    def request_id(self) -> int:
        return self._call.request_id

    def done(self) -> bool:
        return self._call.done.is_set()

    def result(self) -> ToolCallResult:
        """等結果。timeout 到期會先走完整取消契約再 raise。"""
        return self._client._await_call(self._call)

    def cancel(self, reason: str = "cancelled by user") -> None:
        """送 ``notifications/cancelled``,寬限期內等不到就 SIGTERM 該 instance。"""
        self._client._cancel(self._call, reason)


class McpClient:
    """一個 MCP server 子行程 + 我們自己的 JSON-RPC 對話。全同步,無 event loop。"""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        argv: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
        cancel_grace: float = CANCEL_GRACE_SECONDS,
        terminate_grace: float = TERMINATE_GRACE_SECONDS,
        start_timeout: float | None = None,
        stderr_log: str | os.PathLike[str] | None = None,
        on_start_progress: Callable[[float], None] | None = None,
    ) -> None:
        self.root = str(Path(root).resolve())
        self._argv = list(argv) if argv else [sys.executable, str(SERVER_SCRIPT)]
        self._env_overrides = dict(env or {})
        self.cancel_grace = float(cancel_grace)
        self.terminate_grace = float(terminate_grace)
        self.start_timeout = float(start_timeout) if start_timeout is not None else None
        self._stderr_log = (
            stderr_log if stderr_log is not None else os.environ.get(STDERR_LOG_ENV) or None
        )
        self._on_start_progress = on_start_progress

        # 三把獨立的鎖,刻意不共用一把:
        #   _lock         生命週期(_proc / _closed / _generation / _tools)
        #   _pending_lock 進行中的請求表與 id 配發
        #   _stderr_lock  stderr 尾端環形緩衝
        # 共用一把的話,`start()` 持著鎖等 initialize 回應,而回應正是由 reader
        # 執行緒送進來的 —— 它一取同一把鎖就死鎖,整個啟動停在那裡。
        self._lock = threading.RLock()
        self._pending_lock = threading.Lock()
        self._stderr_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._stderr_tail: deque[bytes] = deque()
        self._stderr_bytes = 0
        self._stderr_handle = None
        self._pending: dict[int, _Call] = {}
        self._tools: tuple[ToolSpec, ...] = ()
        self._closed = False
        self._generation = 0

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> None:
        """啟動 instance 並完成 initialize / tools/list。已啟動時是 no-op。"""
        with self._lock:
            if self._closed:
                raise McpUnavailableError("MCP client already closed")
            if self._proc is not None and self._proc.poll() is None:
                return
            self._spawn()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._teardown("client closed")

    def __enter__(self) -> "McpClient":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def restart(self) -> None:
        """收掉目前的 instance 並重新 spawn。進行中的呼叫全部回成 error。"""
        with self._lock:
            if self._closed:
                raise McpUnavailableError("MCP client already closed")
            self._teardown("restart requested")
            self._spawn()

    # ---- introspection -----------------------------------------------
    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None else None

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def closed(self) -> bool:
        return self._closed

    def tools(self) -> tuple[ToolSpec, ...]:
        self.start()
        return self._tools

    def openai_tools(self) -> list[dict[str, Any]]:
        return [spec.as_openai_tool() for spec in self.tools()]

    def stderr_tail(self, limit: int = STDERR_TAIL_BYTES) -> str:
        with self._stderr_lock:
            data = b"".join(self._stderr_tail)
        return data[-limit:].decode("utf-8", errors="replace")

    # ---- calls --------------------------------------------------------
    def begin_call(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        on_progress: Callable[[float, float | None, str | None], None] | None = None,
    ) -> PendingCall:
        self.start()
        if self._proc is None or self._proc.poll() is not None:
            raise McpUnavailableError("MCP instance is not running")
        with self._pending_lock:
            request_id = next(self._ids)
            call = _Call(request_id, name, on_progress)
            self._pending[request_id] = call
        params: dict[str, Any] = {"name": name, "arguments": dict(arguments or {})}
        if on_progress is not None:
            params["_meta"] = {"progressToken": request_id}
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params})
        except McpClientError:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise
        return PendingCall(self, call)

    def call(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        on_progress: Callable[[float, float | None, str | None], None] | None = None,
    ) -> ToolCallResult:
        """同步呼叫。Ctrl-C 與 timeout 都會走完整取消契約。"""
        pending = self.begin_call(name, arguments, on_progress=on_progress)
        try:
            return pending.result()
        except KeyboardInterrupt:
            pending.cancel(reason="user interrupt")
            raise McpCallCancelledError(f"MCP 呼叫 {name} 已取消") from None

    # ---- internals ----------------------------------------------------
    #: 不得傳給 MCP 子行程的變數。
    #:
    #: web 密碼是給**前端**驗身用的,MCP server 完全用不到它。傳過去的話,
    #: 核准後執行的 `run_command` / `run_lint` 子行程會繼承整份環境,專案自己的
    #: 測試腳本只要印一次 env 就把它寫進工具結果與 session 檔。
    SECRET_ENV_KEYS = (
        "AICODE_WEB_PASSWORD",
        # 升級機器上可能還留著的舊 OpenCode 變數:密碼與整份設定內容。
        "OPENCODE_SERVER_PASSWORD",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_API_KEY",
    )

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for key in self.SECRET_ENV_KEYS:
            env.pop(key, None)
        env.update(self._env_overrides)
        env["AICODE_ROOT"] = self.root
        return env

    def _spawn(self) -> None:
        with self._stderr_lock:
            self._stderr_tail = deque()
            self._stderr_bytes = 0
        self._stderr_handle = _open_stderr_log(self._stderr_log)
        try:
            proc = subprocess.Popen(
                self._argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.root,
                env=self._build_env(),
                # 自成 session:終端機的 Ctrl-C(SIGINT 給前景 process group)不會
                # 直接打到 MCP server。中斷要走 notifications/cancelled,server
                # 才有機會收乾淨 ingest 子行程,而不是被 SIGINT 當場砍掉。
                start_new_session=True,
            )
        except OSError as exc:
            self._close_stderr_handle()
            raise McpUnavailableError(f"無法啟動 MCP server: {exc}") from exc
        self._proc = proc
        self._reader = threading.Thread(
            target=self._read_stdout, args=(proc,), name="codetrail-mcp-stdout", daemon=True
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._read_stderr, args=(proc,), name="codetrail-mcp-stderr", daemon=True
        )
        self._stderr_reader.start()
        try:
            self._handshake()
        except BaseException:
            tail = self.stderr_tail()
            self._teardown("startup failed")
            detail = f"\n--- MCP server stderr (tail) ---\n{tail}" if tail else ""
            raise McpUnavailableError(f"MCP server 啟動失敗{detail}") from None
        self._generation += 1

    def _handshake(self) -> None:
        deadline_seconds = self.start_timeout if self.start_timeout is not None else call_timeout_seconds()
        self._request(
            "initialize",
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
            timeout=deadline_seconds,
            label="initialize",
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        listed = self._request("tools/list", {}, timeout=deadline_seconds, label="tools/list")
        specs = tool_specs(listed)
        assert_public_catalog(specs)
        self._tools = specs

    def _request(self, method: str, params: Mapping[str, Any], *, timeout: float, label: str) -> Any:
        with self._pending_lock:
            request_id = next(self._ids)
            call = _Call(request_id, label, None)
            self._pending[request_id] = call
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
        started = time.monotonic()
        reported = 0.0
        while not call.done.wait(timeout=1.0):
            elapsed = time.monotonic() - started
            if elapsed >= timeout:
                with self._pending_lock:
                    self._pending.pop(request_id, None)
                raise McpUnavailableError(f"{label} 超過 {timeout:g} 秒未回應")
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                with self._pending_lock:
                    self._pending.pop(request_id, None)
                raise McpUnavailableError(f"MCP server 在 {label} 期間結束(exit {proc.returncode})")
            if self._on_start_progress is not None and elapsed - reported >= _HEARTBEAT_SECONDS:
                reported = elapsed
                with contextlib.suppress(Exception):
                    self._on_start_progress(elapsed)
        with self._pending_lock:
            self._pending.pop(request_id, None)
        if call.error is not None:
            raise call.error
        return call.result

    def _send(self, message: Mapping[str, Any]) -> None:
        payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise McpUnavailableError("MCP instance is not running")
        with self._write_lock:
            try:
                proc.stdin.write(payload)
                proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                raise McpUnavailableError(f"MCP stdio 已中斷: {exc}") from exc

    def _read_stdout(self, proc: subprocess.Popen) -> None:
        stream = proc.stdout
        assert stream is not None
        try:
            for raw in stream:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        except (ValueError, OSError):
            pass
        finally:
            self._fail_pending(McpUnavailableError("MCP server 的 stdio 已關閉"))

    def _dispatch(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("id")
        if isinstance(request_id, int) and ("result" in message or "error" in message):
            with self._pending_lock:
                call = self._pending.get(request_id)
            if call is None:
                return
            if "error" in message:
                error = message.get("error") or {}
                text = error.get("message") if isinstance(error, Mapping) else str(error)
                if call.cancel_sent:
                    call.error = McpCallCancelledError(f"MCP 呼叫 {call.name} 已取消")
                else:
                    call.error = McpToolError(f"MCP 呼叫 {call.name} 失敗: {text}")
            else:
                call.result = message.get("result")
            call.done.set()
            return
        if message.get("method") == "notifications/progress":
            params = message.get("params") or {}
            token = params.get("progressToken")
            with self._pending_lock:
                call = self._pending.get(token) if isinstance(token, int) else None
            if call is not None and call.on_progress is not None:
                with contextlib.suppress(Exception):
                    call.on_progress(
                        params.get("progress"), params.get("total"), params.get("message")
                    )

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        stream = proc.stderr
        assert stream is not None
        handle = self._stderr_handle
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                if handle is not None:
                    with contextlib.suppress(OSError):
                        handle.write(chunk)
                        handle.flush()
                    continue
                with self._stderr_lock:
                    self._stderr_tail.append(chunk)
                    self._stderr_bytes += len(chunk)
                    while self._stderr_bytes > STDERR_TAIL_BYTES and len(self._stderr_tail) > 1:
                        self._stderr_bytes -= len(self._stderr_tail.popleft())
        except (ValueError, OSError):
            pass

    def _await_call(self, call: _Call) -> ToolCallResult:
        timeout = call_timeout_seconds()
        deadline = time.monotonic() + timeout
        while not call.done.wait(timeout=0.2):
            if time.monotonic() >= deadline:
                self._cancel(call, "client read timeout")
                raise McpCallTimeoutError(
                    f"MCP 呼叫 {call.name} 超過 {timeout:g} 秒未回應;已送出取消通知。"
                )
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                break
        with self._pending_lock:
            self._pending.pop(call.request_id, None)
        if not call.done.is_set():
            raise McpUnavailableError(f"MCP server 在 {call.name} 期間結束")
        if call.error is not None:
            raise call.error
        result = call.result if isinstance(call.result, Mapping) else {}
        return ToolCallResult(
            name=call.name,
            text=_text_blocks(result.get("content") or ()),
            structured=result.get("structuredContent"),
            is_error=bool(result.get("isError")),
        )

    def _cancel(self, call: _Call, reason: str) -> None:
        if call.done.is_set():
            return
        call.cancel_sent = True
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": call.request_id, "reason": reason},
                }
            )
        except McpClientError:
            self._escalate("cancellation could not be delivered")
            return
        if call.done.wait(timeout=self.cancel_grace):
            return
        # 寬限期過了 server 還沒回應這一次取消 —— 整個 instance 都不能再信任。
        # SIGTERM 會走 mcp_server 自己的 handler(收 ingest 子行程與 lease)。
        self._escalate("cancellation grace expired")

    def _escalate(self, reason: str) -> None:
        with self._lock:
            if self._proc is None:
                return
            self._teardown(reason)
            if not self._closed:
                with contextlib.suppress(McpClientError):
                    self._spawn()

    def _teardown(self, reason: str) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            _terminate(proc, self.terminate_grace)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                with contextlib.suppress(Exception):
                    if pipe is not None:
                        pipe.close()
        self._close_stderr_handle()
        self._fail_pending(McpUnavailableError(f"MCP instance 已收掉({reason})"))

    def _fail_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            calls = list(self._pending.values())
            self._pending.clear()
        for call in calls:
            if call.done.is_set():
                continue
            call.error = McpCallCancelledError(f"MCP 呼叫 {call.name} 已取消") if call.cancel_sent else error
            call.done.set()

    def _close_stderr_handle(self) -> None:
        handle = self._stderr_handle
        self._stderr_handle = None
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.close()


def _terminate(proc: subprocess.Popen, grace: float) -> None:
    if proc.poll() is not None:
        with contextlib.suppress(Exception):
            proc.wait(timeout=grace)
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.terminate()
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=grace)


def tool_specs(listed: Any) -> tuple[ToolSpec, ...]:
    tools = listed.get("tools") if isinstance(listed, Mapping) else None
    if not isinstance(tools, list):
        raise McpClientError("MCP tools/list 沒有回傳工具清單")
    specs: list[ToolSpec] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            raise McpClientError("MCP tools/list 回傳了非物件的工具項")
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise McpClientError("MCP tools/list returned a tool without a name")
        if name.startswith(FORBIDDEN_TOOL_PREFIX):
            raise McpClientError(
                f"MCP tool name unexpectedly carries the OpenCode prefix: {name!r}"
            )
        schema = tool.get("inputSchema")
        if not isinstance(schema, Mapping):
            raise McpClientError(f"MCP tool {name!r} has no object inputSchema")
        annotations = tool.get("annotations")
        hint = annotations.get("readOnlyHint") if isinstance(annotations, Mapping) else None
        # **只有 JSON true 才算唯讀。** `bool("false")` 是 True —— catalog 的型別
        # 一漂移,readonly policy 就會把一個寫入工具當成唯讀直接放行。
        read_only = hint is True
        description = tool.get("description")
        specs.append(
            ToolSpec(
                name=name,
                description=description if isinstance(description, str) else "",
                input_schema=dict(schema),
                read_only=read_only,
            )
        )
    return tuple(specs)


def assert_public_catalog(specs: Sequence[ToolSpec]) -> None:
    """工具集合與順序必須逐字等於 ``PUBLIC_TOOL_ORDER``。

    在 ``start()`` 就驗,不是只在測試裡驗:server 少一個工具或順序漂移時,
    模型看到的工具集合就跟所有 eval baseline 與文件講的不是同一份。
    """
    names = tuple(spec.name for spec in specs)
    if names != PUBLIC_TOOL_ORDER:
        raise McpClientError(
            "MCP tools/list 與 PUBLIC_TOOL_ORDER 不一致:\n"
            f"  got      = {names}\n"
            f"  expected = {PUBLIC_TOOL_ORDER}"
        )


def _open_stderr_log(stderr_log: str | os.PathLike[str] | None):
    """設了 ``CODETRAIL_MCP_STDERR_LOG`` 才落檔;owner-only 並印一行警告。"""
    if not stderr_log:
        return None
    path = Path(stderr_log).expanduser()
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise McpClientError(f"{STDERR_LOG_ENV} 不得指向 symlink: {path}") from exc
        raise McpClientError(f"無法開啟 {STDERR_LOG_ENV} 指定的檔案 {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise McpClientError(f"{STDERR_LOG_ENV} 必須指向普通檔案: {path}")
        os.fchmod(fd, 0o600)
    except Exception:
        os.close(fd)
        raise
    handle = os.fdopen(fd, "ab", closefd=True)
    print(
        f"[mcp] ⚠️ MCP server stderr 會寫進 {path}(0600)。"
        "它含查詢原文、截圖抽取內容、命令與絕對路徑 —— 等同 NDA 內容,請自行保管與清理。",
        file=sys.stderr,
        flush=True,
    )
    return handle


_SHARED_LOCK = threading.Lock()
_SHARED: dict[str, McpClient] = {}


def shared_client(root: str | os.PathLike[str], **kwargs: Any) -> McpClient:
    """取得這個行程內、對應這個 root 的唯一 MCP client。

    「一個 engine 行程只起一個 MCP instance」是 ingest busy 閘的前提:那個閘
    是 server 行程內的狀態,兩個 instance 等於兩份互看不到的狀態,第二個對話
    在第一個 ingest 期間就會拿到一份「剛好抓到的」KB 而不是 busy。

    **關閉與替換在同一把鎖裡完成**:``close()`` 回來時舊子行程已經走了,所以
    不會出現「舊的還在收屍、新的已經起來」的重疊窗口。
    """
    key = str(Path(root).resolve())
    with _SHARED_LOCK:
        client = _SHARED.get(key)
        if client is not None and not client.closed:
            return client
        if client is not None:
            client.close()
        client = McpClient(key, **kwargs)
        _SHARED[key] = client
        return client


def close_shared_client(root: str | os.PathLike[str]) -> None:
    """關掉並移除某個 root 的共用 client(關閉完成後才移除登記)。"""
    key = str(Path(root).resolve())
    with _SHARED_LOCK:
        client = _SHARED.get(key)
        if client is None:
            return
        client.close()
        _SHARED.pop(key, None)


def reset_shared_clients() -> None:
    """測試用:關閉並忘掉所有共用 client。"""
    with _SHARED_LOCK:
        clients = list(_SHARED.values())
        _SHARED.clear()
    for client in clients:
        with contextlib.suppress(Exception):
            client.close()
