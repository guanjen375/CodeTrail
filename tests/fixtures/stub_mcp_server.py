#!/usr/bin/env python3
"""手寫的 JSON-RPC stdio stub,用來釘住 client_mcp 的取消契約。

刻意**不**用 mcp SDK 的 server:契約測試要驗的其中一件事就是「收到
notifications/cancelled 之後行為是什麼」,而 stub 必須能演出「server 完全不理
取消」這個 client 必須自己收拾的情境。

行為由 env 控制:
  STUB_SLOW_SECONDS      slow_tool 每次呼叫要跑多久(預設 60)
  STUB_IGNORE_CANCEL=1   收到 notifications/cancelled 完全不理(不回應)
  STUB_CANCEL_LOG=<path> 每收到一則 notification 就 append 一行 JSON
  STUB_IGNORE_SIGTERM=1  忽略 SIGTERM(驗 SIGKILL fallback)
  STUB_SPAWN_CHILD=1     slow_tool 期間開一個自成 process group 的子行程,
                         收到 cancel 時整組收掉(對應 mcp_server 的 ingest 回收)
  STUB_CHILD_PGID=<path> 把子行程的 pgid 寫進這個檔
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

SLOW_SECONDS = float(os.environ.get("STUB_SLOW_SECONDS", "60"))
IGNORE_CANCEL = os.environ.get("STUB_IGNORE_CANCEL", "") == "1"
CANCEL_LOG = os.environ.get("STUB_CANCEL_LOG", "")
IGNORE_SIGTERM = os.environ.get("STUB_IGNORE_SIGTERM", "") == "1"
SPAWN_CHILD = os.environ.get("STUB_SPAWN_CHILD", "") == "1"
CHILD_PGID_FILE = os.environ.get("STUB_CHILD_PGID", "")

_write_lock = threading.Lock()
_cancelled: set[object] = set()


def _send(payload: dict) -> None:
    line = json.dumps(payload, ensure_ascii=False)
    with _write_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _log_notification(message: dict) -> None:
    if not CANCEL_LOG:
        return
    with open(CANCEL_LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(message, ensure_ascii=False) + "\n")


# 工具目錄必須逐字等於真的 server 的 19 個名稱與順序 —— client 在 start()
# 就驗這件事,stub 自己編一組名字的話,整批取消契約測試就繞過了那道驗證。
# 慢工具挑 ingest_document:取消契約本來就是為它存在的。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from mcp_contract import PUBLIC_TOOL_ORDER  # noqa: E402

SLOW_TOOL = "ingest_document"
FAST_TOOL = "list_dir"
READ_ONLY_TOOLS = frozenset({
    "list_dir", "read_file", "grep_code", "code_rag_search", "file_info",
    "query_knowledge", "query_knowledge_strict", "git_status", "git_diff",
    "analyze_file",
})

TOOLS = [
    {
        "name": name,
        "description": f"stub {name}",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": True},
        "annotations": {"readOnlyHint": name in READ_ONLY_TOOLS},
    }
    for name in PUBLIC_TOOL_ORDER
]


def _spawn_child():
    """自成 process group 的長命子行程(模擬 ingest 的 RAG.py 子行程)。"""
    import subprocess

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        start_new_session=True,
    )
    pgid = os.getpgid(proc.pid)
    if CHILD_PGID_FILE:
        with open(CHILD_PGID_FILE, "w", encoding="utf-8") as handle:
            handle.write(str(pgid))
    return proc, pgid


def _reap(proc, pgid) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _handle_call(request_id: object, name: str) -> None:
    if name == SLOW_TOOL:
        child = _spawn_child() if SPAWN_CHILD else None
        deadline = time.monotonic() + SLOW_SECONDS
        while time.monotonic() < deadline:
            if request_id in _cancelled:
                if child is not None:
                    _reap(*child)
                _send({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": 0, "message": "Request cancelled"},
                })
                return
            time.sleep(0.02)
        if child is not None:
            _reap(*child)
    _send({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": f"{name} done"}],
            "isError": False,
        },
    })


def main() -> int:
    if IGNORE_SIGTERM:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        if "id" not in message:
            _log_notification(message)
            if method == "notifications/cancelled" and not IGNORE_CANCEL:
                params = message.get("params") or {}
                _cancelled.add(params.get("requestId"))
            continue
        request_id = message.get("id")
        if method == "initialize":
            _send({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": message.get("params", {}).get(
                        "protocolVersion", "2025-06-18"
                    ),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "stub", "version": "0"},
                },
            })
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = (message.get("params") or {}).get("name", "")
            threading.Thread(
                target=_handle_call, args=(request_id, name), daemon=True
            ).start()
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": request_id, "result": {}})
        else:
            _send({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"unknown method {method}"},
            })
    return 0


if __name__ == "__main__":
    sys.exit(main())
