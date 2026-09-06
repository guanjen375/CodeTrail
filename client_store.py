#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_store — CodeTrail 客戶端的 session 持久化(JSONL)。

放在 ``${XDG_STATE_HOME:-~/.local/state}/codetrail/sessions/<root 雜湊>/``,
**不放進被分析的 repo**:對話逐字含 NDA 程式碼與文件內容,寫進 repo 等於一次
``git add .`` 就外流,而且會污染 ``code_rag`` 的索引。

檔案規則沿用 ``session_eval`` 的 private writer:目錄 0700、檔案 0600、拒
symlink、以 dir-fd 錨定寫入。差別只在這裡是 **append**(一輪一行)而不是整份
原子替換 —— 一次對話會寫上百行,每行都重寫整份檔案在長 session 上是 O(n²)。

``resume`` 與 ``session_eval`` 的 export 都讀這裡。headless 預設 ephemeral
(不落檔),只有明確旗標才持久化。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import time

import client_paths
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

STATE_DIR_PARTS = ("codetrail", "sessions")
DIR_MODE = 0o700
FILE_MODE = 0o600

#: 單一 session 檔的讀取上限。超過就 fail-loud,不截半份對話當完整的用。
MAX_SESSION_BYTES = 64 * 1024 * 1024
#: 單行上限(一則訊息)。工具結果已在 MCP 端有預算,這裡只擋病態輸入。
MAX_RECORD_BYTES = 8 * 1024 * 1024

_SESSION_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{8}$")

#: 大綱裡一則問題最多顯示幾個字元。選單一列要在 80 欄的 SSH 終端看得完;
#: 超過就加 `…`,**不另存**一份縮寫(session 檔是唯讀的真值)。
OUTLINE_MAX_CHARS = 80


class SessionStoreError(RuntimeError):
    """session 檔的位置、權限或內容不合契約。"""


def root_hash(root: str | os.PathLike[str]) -> str:
    """把 AICODE_ROOT 對應到一個穩定、不可讀回原路徑的目錄名。"""
    resolved = str(Path(root).expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


def state_home(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_STATE_HOME``,否則 ``$HOME/.local/state``。

    **只接受絕對路徑**:相對的 ``XDG_STATE_HOME`` 會被解讀成相對 cwd,而 cwd
    通常正是被分析的專案 —— 對話就這樣落進 repo。XDG 規格本身也要求絕對路徑,
    所以相對值是設定錯誤,fail-loud 而不是靜默改寫位置。
    """
    environ = os.environ if env is None else env
    override = environ.get("XDG_STATE_HOME", "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise SessionStoreError(
                f"XDG_STATE_HOME 必須是絕對路徑,得到 {override!r};"
                "相對路徑會相對 cwd 解讀,而 cwd 通常就是被分析的專案。"
            )
        return path
    home = environ.get("HOME", "").strip()
    if not home:
        raise SessionStoreError("HOME 與 XDG_STATE_HOME 都沒設,無法決定 session 存放位置")
    home_path = Path(home).expanduser()
    if not home_path.is_absolute():
        raise SessionStoreError(f"HOME 必須是絕對路徑,得到 {home!r}")
    return home_path / ".local" / "state"


def sessions_dir(root: str | os.PathLike[str], env: Mapping[str, str] | None = None) -> Path:
    return state_home(env).joinpath(*STATE_DIR_PARTS) / root_hash(root)


def new_session_id(now: float | None = None) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(now if now is not None else time.time()))
    return f"{stamp}-{secrets.token_hex(4)}"


def validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise SessionStoreError(f"session id 不合格式: {session_id!r}")
    return session_id


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    path: Path
    created: float
    updated: float
    title: str
    turns: int
    #: 選單那一列要顯示的東西(見 :func:`session_outline`)。全部有預設值:
    #: 既有呼叫端(headless `sessions`、`--continue`)只用得到上面六欄。
    first_prompt: str = ""
    last_prompt: str = ""
    messages: int = 0
    tool_calls: int = 0
    compactions: int = 0


def _store_error(message: str) -> SessionStoreError:
    return SessionStoreError(message)


def _outline_text(value: Any) -> str:
    """把一則訊息壓成選單看得完的一行。"""
    if not isinstance(value, str):
        return ""
    folded = " ".join(value.split())
    if len(folded) <= OUTLINE_MAX_CHARS:
        return folded
    return folded[: OUTLINE_MAX_CHARS - 1] + "…"


def session_outline(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """從 session 記錄算出「這段對話長什麼樣」。

    **純函式:零 LLM、零寫入。** 選單那一列不得為了好看去跑一次模型(那是一次
    多餘的 NDA 內容出門機會,而且開一次選單要等好幾秒),也不得回頭改寫 session
    檔 —— 那份檔是唯讀的真值。

    大綱只認**使用者自己送的**訊息:壓縮注入的摘要(``synthetic``)與工具結果
    都不是問題。拿它們當大綱的話,選單上會出現「[先前對話摘要]」開頭那一行,或
    一段工具輸出 —— 而使用者是靠自己問過的第一句話認出這段對話的。
    """
    prompts: list[str] = []
    messages = 0
    tool_calls = 0
    compactions = 0
    for record in records:
        kind = record.get("type")
        if kind == "compaction":
            compactions += 1
            continue
        if kind != "message":
            continue
        messages += 1
        role = record.get("role")
        if role == "assistant":
            calls = record.get("tool_calls")
            if isinstance(calls, Sequence) and not isinstance(calls, (str, bytes)):
                tool_calls += len(calls)
            continue
        if role != "user" or record.get("synthetic"):
            continue
        text = _outline_text(record.get("content"))
        if text:
            prompts.append(text)
    return {
        "first_prompt": prompts[0] if prompts else "",
        "last_prompt": prompts[-1] if prompts else "",
        "messages": messages,
        "tool_calls": tool_calls,
        "compactions": compactions,
    }


def _open_private_dir(
    path: Path,
    env: Mapping[str, str] | None = None,
    *,
    create: bool = True,
    guard: Any = None,
) -> int:
    """建立/開啟 session 目錄,並用 dir fd 錨定它。

    走 ``client_paths`` 的共用防線:``$XDG_STATE_HOME`` 以上 realpath 解析,
    以下(``codetrail/sessions/<hash>``)逐層 ``O_NOFOLLOW``。``create=False``
    給純讀取:讀一個不存在的 session 不得把目錄生出來。回 ``-1`` = 目錄不存在。
    """
    return client_paths.open_private_dir(
        Path(path), _store_error, create=create, anchor=state_home(env), guard=guard
    )


def _write_all(fd: int, payload: bytes, name: str) -> None:
    """把整份 payload 寫完。

    ``os.write`` 可以 short write。不檢查回傳長度的話,一次 short write 會
    留下半行 JSON —— 而那一份 session 從此讀不回來,寫入當下卻回報成功。
    """
    view = memoryview(payload)
    written = 0
    while written < len(view):
        try:
            count = os.write(fd, view[written:])
        except OSError as exc:
            raise SessionStoreError(f"cannot write session file {name}") from exc
        if count <= 0:
            raise SessionStoreError(f"cannot write session file {name}")
        written += count


def _encode(record: Mapping[str, Any]) -> bytes:
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    payload = line.encode("utf-8")
    if len(payload) > MAX_RECORD_BYTES:
        raise SessionStoreError("session 記錄超過單行上限")
    return payload


class SessionStore:
    """一個 AICODE_ROOT 底下所有持久化 session 的入口。"""

    def __init__(self, root: str | os.PathLike[str], env: Mapping[str, str] | None = None) -> None:
        self.root = str(Path(root).expanduser().resolve())
        self._env = env
        self.directory = sessions_dir(self.root, env)
        # 對話逐字含 NDA 程式碼與文件內容。落進被分析的 repo 等於一次
        # `git add .` 就外流,而且會被 code_rag 索引進去。
        # 字面路徑與 realpath 都要看:`XDG_STATE_HOME=/tmp/link/state` 而
        # `/tmp/link` 指進 repo,字面上看不出來。建構時判一次,之後**每一次**
        # 開目錄再判一次(祖先 symlink 可以在兩次操作之間被改指)。
        self._refuse_inside_root(
            client_paths.resolved_directory(self.directory, anchor=state_home(env))
        )

    def _refuse_inside_root(self, resolved: Path) -> None:
        root_path = Path(self.root)
        for candidate in (self.directory, resolved):
            if candidate == root_path or root_path in candidate.parents:
                raise SessionStoreError(
                    f"拒絕把 session 檔放進被分析的專案: {candidate} 在 {self.root} 之內。"
                    "請把 XDG_STATE_HOME 指到專案外面。"
                )

    def _dir_fd(self, *, create: bool) -> int:
        return _open_private_dir(
            self.directory, self._env, create=create, guard=self._refuse_inside_root
        )

    # ---- writing ------------------------------------------------------
    def create(self, session_id: str | None = None, *, title: str = "") -> str:
        session_id = validate_session_id(session_id) if session_id else new_session_id()
        header = {
            "schema": SCHEMA_VERSION,
            "type": "header",
            "session": session_id,
            "root": self.root,
            "created": time.time(),
            "title": str(title or ""),
        }
        self._append_raw(session_id, [header], create=True)
        return session_id

    def append(self, session_id: str, record: Mapping[str, Any]) -> None:
        self.append_many(session_id, [record])

    def append_many(self, session_id: str, records: Iterable[Mapping[str, Any]]) -> None:
        self._append_raw(session_id, list(records), create=False)

    def _append_raw(self, session_id: str, records: list[Mapping[str, Any]], *, create: bool) -> None:
        validate_session_id(session_id)
        if not records:
            return
        payload = b"".join(_encode(record) for record in records)
        name = f"{session_id}.jsonl"
        dir_fd = self._dir_fd(create=True)
        try:
            flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            # **只有 create 路徑帶 O_CREAT。** append 也建檔的話,對一個不存在
            # (或剛被清掉)的 session id 呼叫 append 會靜靜生出一份沒有 header
            # 的 JSONL —— 寫入看起來成功,直到之後 resume 才失敗。
            if create:
                flags |= os.O_CREAT | os.O_EXCL
            try:
                fd = os.open(name, flags, FILE_MODE, dir_fd=dir_fd)
            except FileExistsError:
                raise SessionStoreError(f"session 已存在: {session_id}") from None
            except FileNotFoundError:
                raise SessionStoreError(f"session 不存在: {session_id}") from None
            except OSError as exc:
                raise SessionStoreError(f"cannot open session file {name}") from exc
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise SessionStoreError(f"refusing non-regular session file: {name}")
                if st.st_nlink != 1:
                    raise SessionStoreError(f"refusing hard-linked session file: {name}")
                if not create and st.st_size == 0:
                    raise SessionStoreError(f"session 檔是空的(沒有 header): {name}")
                os.fchmod(fd, FILE_MODE)
                _write_all(fd, payload, name)
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)

    # ---- reading ------------------------------------------------------
    def path(self, session_id: str) -> Path:
        return self.directory / f"{validate_session_id(session_id)}.jsonl"

    def exists(self, session_id: str) -> bool:
        try:
            self.read_bytes(session_id)
        except SessionStoreError:
            return False
        return True

    def read_bytes(self, session_id: str) -> bytes:
        """把整份 session 檔讀進來,全程錨在同一個 fd 上。

        讀取端與寫入端用同一套防線:dir-fd + ``O_NOFOLLOW`` 開檔、``fstat``
        驗普通檔與 ``st_nlink == 1``、讀取本身有累積上限。用 path-based
        ``stat()`` 再 ``open()`` 的話,兩次 lookup 之間可以被換掉(symlink 的
        父目錄、hard link、邊讀邊長大的檔),而 64 MiB 的上限只驗到舊的那一次。
        """
        validate_session_id(session_id)
        name = f"{session_id}.jsonl"
        dir_fd = self._dir_fd(create=False)
        if dir_fd < 0:
            # 純讀取不得把目錄生出來(AGENTS §2:讀取端不留痕跡)。
            raise SessionStoreError(f"session 檔不存在: {name}")
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(name, flags, dir_fd=dir_fd)
            except FileNotFoundError:
                raise SessionStoreError(f"session 檔不存在: {name}") from None
            except OSError as exc:
                raise SessionStoreError(f"cannot open session file {name}") from exc
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise SessionStoreError(f"refusing non-regular session file: {name}")
                if info.st_nlink != 1:
                    raise SessionStoreError(f"refusing hard-linked session file: {name}")
                if hasattr(os, "getuid") and info.st_uid != os.getuid():
                    raise SessionStoreError(f"session 檔不屬於目前使用者: {name}")
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(fd, 1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_SESSION_BYTES:
                        raise SessionStoreError(
                            f"session 檔超過 {MAX_SESSION_BYTES} bytes: {name}"
                        )
                    chunks.append(chunk)
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)
        return b"".join(chunks)

    def read(self, session_id: str) -> list[dict[str, Any]]:
        raw = self.read_bytes(session_id)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SessionStoreError(f"session 檔不是 UTF-8: {session_id}") from exc
        records: list[dict[str, Any]] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SessionStoreError(
                    f"session 檔第 {lineno} 行不是合法 JSON: {session_id}"
                ) from exc
            if not isinstance(value, dict):
                raise SessionStoreError(f"session 檔第 {lineno} 行不是物件: {session_id}")
            records.append(value)
        self._validate_header(session_id, records)
        return records

    def _validate_header(self, session_id: str, records: list[dict[str, Any]]) -> None:
        """header 必須是這個 store、這個 session 的。

        只驗「第一行是 header」不夠:把別的專案(或別的 session)的檔案複製過來
        照樣會被 resume 接受,於是使用者以為在續接 A,模型看到的是 B 的對話。
        """
        if not records or records[0].get("type") != "header":
            raise SessionStoreError(f"session 檔缺少 header: {session_id}")
        header = records[0]
        if header.get("schema") != SCHEMA_VERSION:
            raise SessionStoreError(
                f"session 檔的 schema 是 {header.get('schema')!r},本版只認 {SCHEMA_VERSION}"
            )
        if header.get("session") != session_id:
            raise SessionStoreError(
                f"session 檔的 header 記的是 {header.get('session')!r},不是 {session_id}"
            )
        if header.get("root") != self.root:
            raise SessionStoreError(
                f"session 檔屬於另一個專案({header.get('root')!r}),拒絕接續"
            )

    def info(self, session_id: str) -> SessionInfo:
        records = self.read(session_id)
        header = records[0]
        turns = sum(1 for r in records if r.get("type") == "message" and r.get("role") == "user")
        updated = header.get("created", 0.0)
        for record in records:
            stamp = record.get("time")
            if isinstance(stamp, (int, float)):
                updated = max(updated, float(stamp))
        return SessionInfo(
            session_id=session_id,
            path=self.path(session_id),
            created=float(header.get("created", 0.0) or 0.0),
            updated=float(updated or 0.0),
            title=str(header.get("title", "") or ""),
            turns=turns,
            **session_outline(records),
        )

    def list_sessions(self, limit: int | None = None) -> list[SessionInfo]:
        """最近更新的排前面。``limit`` 是**排序之後**才切的(選單只顯示前幾筆)。

        先切再排的話,選單上出現的是目錄順序的前 N 筆 —— 使用者最近在用的那一段
        可能根本不在清單裡。
        """
        if not self.directory.is_dir():
            return []
        out: list[SessionInfo] = []
        for entry in sorted(self.directory.iterdir()):
            if entry.is_symlink() or not entry.is_file() or entry.suffix != ".jsonl":
                continue
            session_id = entry.stem
            if not _SESSION_ID_RE.fullmatch(session_id):
                continue
            try:
                out.append(self.info(session_id))
            except SessionStoreError:
                continue
        out.sort(key=lambda item: item.updated, reverse=True)
        if limit is not None:
            out = out[: max(0, int(limit))]
        return out

    def delete(self, session_id: str) -> bool:
        validate_session_id(session_id)
        dir_fd = self._dir_fd(create=False)
        if dir_fd < 0:
            return False
        try:
            try:
                os.unlink(f"{session_id}.jsonl", dir_fd=dir_fd)
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise SessionStoreError(f"cannot delete session {session_id}") from exc
        finally:
            os.close(dir_fd)
        return True


class EphemeralSessionStore:
    """不落檔的 session store。headless 的預設。

    介面與 ``SessionStore`` 相同,呼叫端不需要分支;``path()`` 一律回 None,讓
    「不落檔」在型別上就看得出來,而不是靠呼叫端記得別寫。
    """

    def __init__(self, root: str | os.PathLike[str], env: Mapping[str, str] | None = None) -> None:
        self.root = str(Path(root).expanduser().resolve())
        self.directory = None
        self._sessions: dict[str, list[dict[str, Any]]] = {}

    def create(self, session_id: str | None = None, *, title: str = "") -> str:
        session_id = validate_session_id(session_id) if session_id else new_session_id()
        self._sessions[session_id] = [
            {
                "schema": SCHEMA_VERSION,
                "type": "header",
                "session": session_id,
                "root": self.root,
                "created": time.time(),
                "title": str(title or ""),
            }
        ]
        return session_id

    def append(self, session_id: str, record: Mapping[str, Any]) -> None:
        self.append_many(session_id, [record])

    def append_many(self, session_id: str, records: Iterable[Mapping[str, Any]]) -> None:
        validate_session_id(session_id)
        # 與持久化版本同一條:append 不建 session。setdefault 會生出一個沒有
        # header 的 bucket,寫入看起來成功,讀回來才失敗。
        try:
            bucket = self._sessions[session_id]
        except KeyError:
            raise SessionStoreError(f"session 不存在: {session_id}") from None
        for record in records:
            _encode(record)  # 同一條大小契約,ephemeral 也不例外
            bucket.append(dict(record))

    def path(self, session_id: str) -> None:
        return None

    def exists(self, session_id: str) -> bool:
        return session_id in self._sessions

    def read(self, session_id: str) -> list[dict[str, Any]]:
        try:
            return [dict(record) for record in self._sessions[session_id]]
        except KeyError:
            raise SessionStoreError(f"session 不存在: {session_id}") from None

    def list_sessions(self, limit: int | None = None) -> list[SessionInfo]:
        return []

    def delete(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None


def iter_messages(records: Iterable[Mapping[str, Any]]) -> Iterator[dict[str, Any]]:
    """從 session 記錄中取出對話訊息(略過 header 與 meta 記錄)。"""
    for record in records:
        if record.get("type") == "message":
            yield dict(record)
