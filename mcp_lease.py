"""MCP server 的 per-instance lease 與 incident 記錄(plan.txt §D / SEAMS §6)。

**為什麼不是一個心跳檔**:canary、TUI、web、`opencode run` 各自起一個 MCP
子行程。四個行程寫同一個檔會互相覆寫,於是「server 還活著嗎」永遠只看得到
最後一個寫入者,而那正是最沒有診斷價值的一份。改成一行程一份 lease
(檔名 = 本行程一次性的 `boot_id`,不是 pid),plugin 用 `ppid == 自己的 pid`
認領當前 instance。

**fail-open 是這個模組的第一原則**:lease 寫不了、目錄建不了、磁碟滿了,
19 個工具都必須照常運作。所有 public 函式自己吞例外;診斷用的
`read_*` / `classify_*` / `incident_stats` 回空值或 `"unknown"`,不 raise。

**零內容零路徑**:lease 與 incident 只放工具名、狀態 slug、時間與計數。
不放工具參數、結果、檔名、絕對路徑,session id 一律 `sha256(...)[:16]`。
`detail` / `exit_reason` 只收固定 slug 集合,收到自由文字換成固定 fallback
(`detail` → `"unknown"`,其餘 → `"other"`)。

**SIGKILL / OOM 不可推論成正常退出**:`exited` 只有 `close_lease()` 會寫。
檔案停在最後一次寫入時,`classify_lease` 只能回 `stale` 或 `unknown`。
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import json
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# ---- 凍結常數(SEAMS §6;T3 的 JS 逐字沿用同一組字面字串)------------------
LEASE_SCHEMA = 1
INCIDENT_SCHEMA = 1
INCIDENT_KINDS = ("promise_without_call", "client_mcp_failed", "structured_call_failed")
INCIDENT_MAX_BYTES = 1_048_576  # 超過就轉存 incidents.jsonl.1(只留 1 份)

STATE_DIR_NAME = "codetrail"
LEASE_DIR_NAME = "mcp"
INCIDENTS_FILENAME = "incidents.jsonl"
INCIDENTS_ROTATED_FILENAME = "incidents.jsonl.1"

# 不在集合裡的值一律換成這個,絕不原樣寫入自由文字。
OTHER_SLUG = "other"

# incident 的 `detail`:SEAMS 附錄 A.1 凍結的 11 個 slug,順序逐字照抄
# (T3 的 JS 寫入端用同一份)。不在集合裡的值一律寫成 `unknown`。
# 這是跨語言契約:任何一端自己增刪一個 slug,另一端就會把對方寫的合法狀態
# 正規化掉,那些事件在事後統計裡等於憑空消失。
UNKNOWN_DETAIL = "unknown"
INCIDENT_DETAILS = (
    "mcp_status_failed",
    "mcp_status_missing",
    "mcp_disabled",
    "mcp_needs_auth",
    "lease_live",
    "lease_stale",
    "lease_exited",
    "lease_unknown",
    "no_tool_part",
    "tool_error",
    UNKNOWN_DETAIL,
)
INCIDENT_SOURCES = ("plugin", "server", OTHER_SLUG)

# 工具結果第一行的 `status:` 三態(tool_result_adapter 的既有形狀)。
TOOL_STATUSES = ("ok", "partial", "error")

# 節流:狀態沒變時最多每秒寫一次(工具呼叫不該每次都 fsync 一個檔)。
# 狀態有變(換了工具 / 換了狀態 / tools/list 次數 +1)一律立刻寫——那才是
# 事後歸因要看的那一格,壓在記憶體裡等於 SIGKILL 之後什麼都沒有。
LEASE_WRITE_MIN_INTERVAL = 1.0

# 正常退出且超過這個時間的 lease 在下次 open_lease() 時回收。
LEASE_RETENTION_SECONDS = 7 * 24 * 3600

_SLUG_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

_LOCK = threading.Lock()
_STATE: dict[str, Any] | None = None
_LEASE_PATH: Path | None = None
_LAST_WRITE = 0.0
_LAST_WRITTEN_KEY: tuple[Any, ...] | None = None


# ============================================================
# 路徑
# ============================================================
def state_dir() -> Path:
    """`$XDG_STATE_HOME/codetrail`,預設 `~/.local/state/codetrail`。

    每次呼叫都重新讀環境變數:測試(與 `aicode` 的 per-session 隔離)靠
    monkeypatch `XDG_STATE_HOME`,快取住就會寫到使用者真的 state 目錄。

    `.strip()` 是**兩端共同的契約**,不是順手加的:T3 的 JS 端對同一個環境
    變數做一樣的 trim,所以兩邊算出來的目錄逐字相同。相對路徑照原樣 join
    (SEAMS 附錄 A.2),空白也照原樣兩端一起去掉——單邊改掉這一行,
    Python 與 JS 就會寫到不同目錄,lease 與 incident 互相看不見。
    """
    base = (os.environ.get("XDG_STATE_HOME") or "").strip()
    if not base:
        home = (os.environ.get("HOME") or "").strip()
        if not home:
            try:
                home = str(Path.home())
            except Exception:
                home = tempfile.gettempdir()
        base = str(Path(home) / ".local" / "state")
    return Path(base) / STATE_DIR_NAME


def lease_dir() -> Path:
    return state_dir() / LEASE_DIR_NAME


def incidents_path() -> Path:
    return state_dir() / INCIDENTS_FILENAME


def _rotated_incidents_path() -> Path:
    return state_dir() / INCIDENTS_ROTATED_FILENAME


# ============================================================
# 小工具
# ============================================================
def _slug(value: object, allowed: tuple[str, ...] | None = None, *, fallback: str = OTHER_SLUG) -> str:
    """把值收斂成安全 slug;不合格或不在正面表列裡一律回 `fallback`。

    `fallback` 因欄位而異:`detail` 的值域由 SEAMS 附錄 A.1 凍結,界外值是
    `unknown`;`last_tool` / `exit_reason` / `source` 這種本模組自己定的欄位
    才用 `other`。
    """
    if not isinstance(value, str):
        return fallback
    text = value.strip()
    if allowed is not None:
        return text if text in allowed else fallback
    return text if _SLUG_RE.match(text) else fallback


def _hash_session(session_id: object) -> str:
    """`sha256(sessionID)[:16]`。空值回空字串,絕不回原始 session id。"""
    if not isinstance(session_id, str) or not session_id:
        return ""
    try:
        return hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest()[:16]
    except Exception:
        return ""


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass  # 目錄已存在且權限不是我們的:不是致命問題


def _atomic_write(path: Path, text: str) -> None:
    """tmp + os.replace 原子寫,mode 0600。"""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _pid_exists(pid: int) -> bool:
    proc = Path(f"/proc/{pid}")
    try:
        if proc.parent.is_dir():
            return proc.is_dir()
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _proc_starttime_ticks(pid: int) -> int | None:
    """`/proc/<pid>/stat` 第 22 欄(starttime,單位 clock tick);拿不到回 None。

    直接存**原始 tick 值**、比對時要求逐值相等,不換算成 epoch 秒:換算要靠
    `/proc/stat` 的 btime,而 btime 會隨 NTP 調整漂移,拿漂過的秒數比對就只能
    留一個容忍窗——那個窗正是 pid 重用鑽得過去的洞。tick 值在同一次開機內是
    固定的,`(pid, starttime)` 就是 Linux 上的行程身分。

    **不用** `/proc/<pid>` 目錄的 mtime:實測那個時間戳是 dentry 被實體化的
    時間,不是行程啟動時間,拿它比對會把老行程判成剛起來的。
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 2:].split()
    # comm 可能含空白與 ')',所以從右括號之後才切。切完 fields[0] 是第 3 欄
    # (state),因此 starttime(第 22 欄)是 fields[19]。
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


# ============================================================
# lease
# ============================================================
def _write_lease_locked() -> None:
    """呼叫前必須持有 `_LOCK`。例外由呼叫端吞掉。"""
    global _LAST_WRITE, _LAST_WRITTEN_KEY
    if _STATE is None or _LEASE_PATH is None:
        return
    _STATE["updated"] = time.time()
    _atomic_write(_LEASE_PATH, json.dumps(_STATE, ensure_ascii=False, sort_keys=True))
    _LAST_WRITE = _STATE["updated"]
    _LAST_WRITTEN_KEY = _state_key()


def _state_key() -> tuple[Any, ...]:
    if _STATE is None:
        return ()
    return (
        _STATE.get("tools_list_count"),
        _STATE.get("last_tool"),
        _STATE.get("last_tool_status"),
        _STATE.get("exited"),
    )


def _flush_locked() -> None:
    """狀態有變就立刻寫;沒變就最多每秒一次。"""
    if _STATE is None:
        return
    if _state_key() != _LAST_WRITTEN_KEY:
        _write_lease_locked()
        return
    if time.time() - _LAST_WRITE >= LEASE_WRITE_MIN_INTERVAL:
        _write_lease_locked()


def open_lease() -> None:
    """本行程開一份新 lease。任何失敗都靜默(工具照常運作)。"""
    global _STATE, _LEASE_PATH, _LAST_WRITE, _LAST_WRITTEN_KEY
    try:
        with _LOCK:
            directory = lease_dir()
            _ensure_dir(directory)
            now = time.time()
            boot_id = secrets.token_hex(8)
            _STATE = {
                "schema": LEASE_SCHEMA,
                "boot_id": boot_id,
                "pid": os.getpid(),
                "ppid": os.getppid(),
                # 行程身分:pid 會被重用,(pid, starttime) 不會。讀不到就寫
                # None,classify_lease 看到 None 只會回 unknown,絕不回 live。
                "proc_started": _proc_starttime_ticks(os.getpid()),
                "started": now,
                "updated": now,
                "tools_list_count": 0,
                "last_tool": None,
                "last_tool_time": None,
                "last_tool_status": None,
                "exited": None,
                "exit_reason": None,
            }
            _LEASE_PATH = directory / f"{boot_id}.json"
            _LAST_WRITE = 0.0
            _LAST_WRITTEN_KEY = None
            _write_lease_locked()
    except Exception:
        return
    _sweep_old_leases()


def close_lease(reason: str = "exit") -> None:
    """正常關閉:寫下 `exited` / `exit_reason`。只有這裡會寫這兩格。"""
    try:
        with _LOCK:
            if _STATE is None:
                return
            _STATE["exited"] = time.time()
            _STATE["exit_reason"] = _slug(reason)
            _write_lease_locked()
    except Exception:
        return


def close_lease_from_signal(reason: str = "signal") -> None:
    """**signal handler 專用**:寫下 `exited` 但**不取 `_LOCK`**。

    signal handler 跑在主執行緒、而且是在**任意一個 bytecode 邊界**插進來的 ——
    如果主執行緒當下正好持有 `_LOCK`(例如正在 `note_tool_call` 裡寫 lease),
    handler 再去 `with _LOCK` 就是自己鎖自己:行程從此不動,supervisor 最後
    只能 SIGKILL,而那條路連 lease 都不會被標記。

    不取鎖的代價是**可能與正在進行的寫入交錯**。這裡刻意接受:lease 是原子
    `os.replace` 寫入的,最壞情況是這一筆退出標記被另一筆蓋掉(doctor 顯示
    `stale` 而不是 `exited`)——那只是診斷少一格,而死鎖是整個關不掉。
    """
    try:
        if _STATE is None:
            return
        _STATE["exited"] = time.time()
        _STATE["exit_reason"] = _slug(reason)
        _write_lease_locked()
    except Exception:
        return


def note_tools_list(count: int) -> None:
    """記一次 tools/list 請求。

    `tools_list_count` 記的是**請求次數**(plan.txt §D「tools/list 次數」),
    不是工具數量;`count` 是這次回應裡的工具數,只用來確認回應成形
    (拿不到就當 0,請求照樣計次——client 確實問過一次)。
    """
    try:
        with _LOCK:
            if _STATE is None:
                return
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                count = 0
            _STATE["tools_list_count"] = int(_STATE.get("tools_list_count") or 0) + 1
            _flush_locked()
    except Exception:
        return


def note_tool_call(name: str, status: str) -> None:
    """記最後一次工具呼叫的名稱 / 時間 / 狀態(不記參數與結果)。"""
    try:
        with _LOCK:
            if _STATE is None:
                return
            _STATE["last_tool"] = _slug(name)
            _STATE["last_tool_time"] = time.time()
            _STATE["last_tool_status"] = _slug(status, TOOL_STATUSES)
            _flush_locked()
    except Exception:
        return


def _status_from_text(text: str) -> str:
    """工具結果第一行永遠是 `status: ok|partial|error`(SEAMS §1 凍結)。"""
    head = text.split("\n", 1)[0].strip().lower()
    if head.startswith("status:"):
        token = head[len("status:"):].strip()
        if token in TOOL_STATUSES:
            return token
    return "ok"


def _result_status(result: object) -> str:
    """從工具結果取狀態;取不到當 ok。

    真正的 transport wrapper 回的是 `mcp.types.CallToolResult`,**不是**字串:
    `isError` 為真就是 error,否則第一段 content 的第一行才是那個凍結的
    `status:` 行。只認 `str` 的話每一次 partial / error 都會被記成 ok,
    doctor 就會指著一份全綠的 lease 說「工具都正常」——那正是這包要修的
    錯誤歸因。`str` 分支保留:單元測試與未包裝的 callable 仍會直接回字串。
    """
    try:
        if isinstance(result, str):
            return _status_from_text(result)
        if getattr(result, "isError", False):
            return "error"
        content = getattr(result, "content", None)
        if content:
            text = getattr(content[0], "text", None)
            if isinstance(text, str):
                return _status_from_text(text)
    except Exception:
        return "ok"
    return "ok"


async def _await_and_note(awaitable: Any, tool_name: str) -> Any:
    try:
        result = await awaitable
    except BaseException:
        note_tool_call(tool_name, "error")
        raise
    note_tool_call(tool_name, _result_status(result))
    return result


def record_tool_calls(name: str, wrapper: Any) -> Any:
    """包 MCP tool wrapper,呼叫後記一筆 lease。回傳同型 callable。

    FastMCP 靠 `inspect.signature` / `get_type_hints` 產 schema,所以一律用
    `functools.wraps`(會設 `__wrapped__`,`inspect.signature` 預設會跟著
    unwrap 到原函式,連 annotation 的求值 namespace 都是原函式的)。
    sync / async 分流:T2 會把 `ingest_document` 改成 async endpoint,
    把 coroutine function 包成 sync 會讓 FastMCP 把 coroutine 當結果送出去。

    記錄失敗絕不能影響工具本身,所以 lease 相關的呼叫各自 fail-open;
    包不起來(取不到 `__name__` 之類)就原樣回傳未包裝的 wrapper。
    """
    tool_name = _slug(name)
    try:
        if inspect.iscoroutinefunction(wrapper):

            @functools.wraps(wrapper)
            async def async_recorder(*args: Any, **kwargs: Any) -> Any:
                try:
                    result = await wrapper(*args, **kwargs)
                except BaseException:
                    note_tool_call(tool_name, "error")
                    raise
                note_tool_call(tool_name, _result_status(result))
                return result

            return async_recorder

        @functools.wraps(wrapper)
        def sync_recorder(*args: Any, **kwargs: Any) -> Any:
            try:
                result = wrapper(*args, **kwargs)
            except BaseException:
                note_tool_call(tool_name, "error")
                raise
            if inspect.isawaitable(result):
                # 同步函式回 awaitable(例如被別的 decorator 包過):結果還沒
                # 發生,現在記 ok 就是說謊——改成等它結束再記。
                return _await_and_note(result, tool_name)
            note_tool_call(tool_name, _result_status(result))
            return result

        return sync_recorder
    except Exception:
        return wrapper


def instrument_tools_list(mcp: Any) -> None:
    """包 `mcp._mcp_server.request_handlers[ListToolsRequest]`。包不到就安靜跳過。

    FastMCP 在 `_setup_handlers()` 就把 bound method 存進 `request_handlers`,
    所以改 `mcp.list_tools` 屬性沒有用,一定要換掉 dict 裡那一格。
    """
    try:
        from mcp import types as mcp_types  # 延後 import:模組本身不依賴 mcp

        server = getattr(mcp, "_mcp_server", None)
        handlers = getattr(server, "request_handlers", None)
        if not isinstance(handlers, dict):
            return
        key = mcp_types.ListToolsRequest
        handler = handlers.get(key)
        if handler is None or getattr(handler, "_codetrail_lease_wrapped", False):
            return

        @functools.wraps(handler)
        async def lease_recording_handler(*args: Any, **kwargs: Any) -> Any:
            result = await handler(*args, **kwargs)
            try:
                root = getattr(result, "root", result)
                tools = getattr(root, "tools", None)
                note_tools_list(len(tools) if tools is not None else 0)
            except Exception:
                pass
            return result

        lease_recording_handler._codetrail_lease_wrapped = True  # type: ignore[attr-defined]
        handlers[key] = lease_recording_handler
    except Exception:
        return


def _sweep_old_leases() -> None:
    """回收正常退出且超過保留期的 lease。**只刪 `exited` 有值的**——

    停在最後一次寫入的檔可能屬於還活著的行程(schema 不同 / 我們讀不懂),
    刪掉就等於把還在跑的 instance 從診斷裡抹掉。
    """
    try:
        directory = lease_dir()
        cutoff = time.time() - LEASE_RETENTION_SECONDS
        for path in directory.glob("*.json"):
            try:
                if path == _LEASE_PATH:
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("exited") is None:
                    continue
                updated = data.get("updated")
                if isinstance(updated, (int, float)) and not isinstance(updated, bool):
                    if float(updated) < cutoff:
                        path.unlink()
            except Exception:
                continue
    except Exception:
        return


def read_leases() -> list[dict]:
    """讀所有 lease(壞檔跳過,不 raise)。目錄不存在回 `[]`,**不建目錄**。"""
    leases: list[dict] = []
    try:
        directory = lease_dir()
        if not directory.is_dir():
            return []
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and isinstance(data.get("boot_id"), str):
                leases.append(data)
    except Exception:
        return leases
    leases.sort(key=lambda item: item.get("started") if isinstance(item.get("started"), (int, float)) else 0.0)
    return leases


def classify_lease(lease: dict, now: float) -> str:
    """`live` / `exited` / `stale` / `unknown`。

    SIGKILL / OOM 的 lease 停在最後一次寫入,這裡只會回 `stale`(pid 不見了)
    或 `unknown`(pid 在但身分對不上),**絕不**推論成 `exited`。

    `live` 的判準是**精確身分**,不是時間窗:lease 裡的 `proc_started`
    (開 lease 當下 `/proc/self/stat` 第 22 欄)必須與現在佔著這個 pid 的行程
    逐值相等。時間窗擋不住快速的 pid 重用——重用的行程本來就會落在窗內,
    然後 doctor 會理直氣壯地說 server 還活著。沒有 `proc_started` 的舊 lease
    也一律 `unknown`:驗不了身分就不宣稱。

    `now` 保留在簽名裡(SEAMS §6 凍結),身分比對本身不需要它。
    """
    try:
        if not isinstance(lease, dict):
            return "unknown"
        if lease.get("exited") is not None:
            return "exited"
        pid = lease.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return "unknown"
        if not _pid_exists(pid):
            return "stale"
        recorded = lease.get("proc_started")
        if not isinstance(recorded, int) or isinstance(recorded, bool):
            return "unknown"  # 舊 lease / 讀不到 /proc:不宣稱 live
        actual = _proc_starttime_ticks(pid)
        if actual is None or actual != recorded:
            return "unknown"  # pid 被重用,或這台機器驗不了身分
        return "live"
    except Exception:
        return "unknown"


# ============================================================
# incident
# ============================================================
def _record_incident(
    kind: str,
    *,
    session: str | None = None,
    detail: str | None = None,
    source: str = "server",
    ts: float | None = None,
) -> None:
    """寫一筆 incident(JSONL,0600,零內容零路徑)。失敗靜默。

    **私有,而且刻意不是 production 寫入端**:正式寫入端是 T3 的 OpenCode
    plugin(JS)。這個函式的角色是把「一行 incident 長什麼樣」用可執行的形式
    定死——欄位、slug 正規化、rotation 門檻、0600——好讓 §7 的跨語言一致性
    測試有東西可以對。取名底線開頭是為了不讓它看起來像一條公開 API:
    誰在 Python 這邊呼叫它,誰就是在寫一條 plugin 不會產生的紀錄。
    """
    try:
        record = {
            "schema": INCIDENT_SCHEMA,
            "ts": float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else time.time(),
            "kind": _slug(kind, INCIDENT_KINDS),
            "session": _hash_session(session),
            "detail": (
                _slug(detail, INCIDENT_DETAILS, fallback=UNKNOWN_DETAIL)
                if detail is not None
                else UNKNOWN_DETAIL
            ),
            "source": _slug(source, INCIDENT_SOURCES),
        }
        directory = state_dir()
        _ensure_dir(directory)
        path = incidents_path()
        _rotate_incidents(path)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        return


def _rotate_incidents(path: Path) -> None:
    """寫入前檔案 ≥ INCIDENT_MAX_BYTES 就轉存 `.jsonl.1`(覆蓋舊的,只留一份)。"""
    try:
        if path.stat().st_size < INCIDENT_MAX_BYTES:
            return
    except OSError:
        return
    try:
        os.replace(path, _rotated_incidents_path())
    except OSError:
        pass


def _iter_incidents() -> Iterator[dict]:
    """`.jsonl.1` + `.jsonl` 的每一行(舊到新);壞行跳過不 raise。"""
    for path in (_rotated_incidents_path(), incidents_path()):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and isinstance(item.get("kind"), str):
                yield item


def read_incidents(limit: int = 500) -> list[dict]:
    """最後 `limit` 筆(舊到新)。**這是顯示用的樣本,不是總數**——要總數請用
    `incident_stats()` / `recent_incident_count()`,它們掃完整個檔。
    """
    records: list[dict] = []
    try:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            limit = 500
        records = list(_iter_incidents())
    except Exception:
        return records[-limit:] if records else []
    return records[-limit:]


def incident_stats() -> dict:
    """`{kind: count}`,**兩個檔的每一行都算**,不截斷。

    三種已知 kind 一定在(沒有就是 0),其餘歸 `other`。分組 key 一律是
    `kind`(SEAMS 附錄 A.1):`detail` 是跨語言的自由度較高的欄位,拿它分組
    會讓別版寫進來的值變成新類別,統計就再也對不起來。

    這裡刻意不吃 `limit`:doctor 把回傳值加總後當「總數」印出去,一個
    帶上限的統計只要事件一多就會靜默低報,而低報的方向剛好是「看起來沒事」。
    """
    stats: dict[str, int] = {kind: 0 for kind in INCIDENT_KINDS}
    stats[OTHER_SLUG] = 0
    try:
        for item in _iter_incidents():
            kind = item.get("kind")
            key = kind if isinstance(kind, str) and kind in INCIDENT_KINDS else OTHER_SLUG
            stats[key] = stats.get(key, 0) + 1
    except Exception:
        return stats
    return stats


def recent_incident_count(within_seconds: float, now: float | None = None) -> int:
    """`ts` 落在最近 `within_seconds` 內的筆數,同樣掃完整個檔不截斷。

    doctor 要印「最近 7 天 N 筆」,而那個 N 一旦是從截斷過的樣本算出來的,
    incident 一 burst 就會顯示成比實際少——使用者會以為問題比較小。
    """
    try:
        reference = float(now) if isinstance(now, (int, float)) and not isinstance(now, bool) else time.time()
        cutoff = reference - float(within_seconds)
        count = 0
        for item in _iter_incidents():
            ts = item.get("ts")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool) and float(ts) >= cutoff:
                count += 1
        return count
    except Exception:
        return 0
