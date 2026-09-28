#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_attachments — 輸入框 ``@路徑`` 附件的解析、路由與補全(TUI 與 headless 共用)。

使用者在訊息裡寫 ``@docs/spec.pdf`` 或 ``@"my docs/spec.pdf"``,送出時由 engine 以
**既有的 MCP 工具**讀它:圖片／PDF／ELF／firmware binary 走 ``analyze_file``(圖片就是
既有的 VL 路徑),其他檔案走 ``read_file``。這個模組只負責「哪些 @ 是附件、交給哪個
工具」;真正讀內容的是 server,而 server 的 sandbox 仍然會重驗路徑。

邊界(AGENTS.md §2「TUI 的 @ 附件」):

* **只附加專案 root 內的普通檔。** 字串層先拒 ``~``、``..`` 與專案外路徑 —— 那些
  連一次 stat 都不做。其餘自 ``/`` 沿 root 與父目錄**逐層** ``O_NOFOLLOW`` 開啟,
  葉節點以 ``stat(dir_fd, follow_symlinks=False)`` 判斷;任何一層或葉節點是 symlink
  就不附加。只驗最終路徑擋不住「中間一層被換成指向專案外的 symlink」。
* **補全只列已驗證目錄 fd 裡的項目。** 目錄以同一條逐層 nofollow 開到 fd 再
  ``scandir(fd)``:檢查之後才被換成 symlink 的路徑名稱,不會讓清單變成專案外的內容。
* **有上限、零寫入、不讀環境、不 print。** 單則最多檢查 ``MAX_MENTIONS`` 個 @、
  附加 ``MAX_ATTACHMENTS`` 個;補全最多掃 ``MAX_COMPLETION_SCAN`` 個目錄項。
  會碰檔案系統的函式(``resolve``、``completions``)不在 UI 執行緒呼叫;
  ``find_mentions``／``path_mentions``／``route`` 是純字串。

路由只看**字面**副檔名:symlink 一律不附加,所以字面路徑就是實體檔,與 server
依 resolve 後副檔名分流的結果相同。
"""
from __future__ import annotations

import errno
import json
import os
import posixpath
import re
import secrets
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import config

#: 單則訊息最多附加幾個檔案。
MAX_ATTACHMENTS = 5
#: 單則訊息最多檢查幾個 @(含不是附件的)。超過的只合成一筆說明,不再碰檔案系統。
MAX_MENTIONS = 16
#: 補全最多掃幾個目錄項(不是符合的項目數)。
MAX_COMPLETION_SCAN = 2000
#: 補全最多列幾項。
MAX_COMPLETIONS = 8

# analyze_file 的 dispatch(mcp_server.analyze_file):圖片 → VL、.pdf → 一次性抽文字、
# ELF、binary。刻意不 import media / agent_tools(那兩個是 server 端模組);三處是否一致
# 由 tests/test_client_attachments.py 的 smoke 契約釘住。
_PDF_EXTENSIONS = frozenset({".pdf"})
_ELF_EXTENSIONS = frozenset({".elf", ".so", ".o", ".axf", ".out", ".ko"})
_BINARY_EXTENSIONS = frozenset({".bin", ".dat", ".raw", ".fw", ".img", ".rom", ".hex"})
ANALYZE_FILE_EXTENSIONS: frozenset[str] = frozenset(
    set(config.IMAGE_EXTENSIONS) | _PDF_EXTENSIONS | _ELF_EXTENSIONS | _BINARY_EXTENSIONS
)

_KIND_LABELS = {"image": "圖片", "pdf": "PDF", "elf": "ELF", "binary": "二進位檔", "text": "文字"}

# `@` 前面不能是 ASCII 英數、`_ . @ / \ -`:email(user@host)、decorator 接在識別字後面
# 都不算;中文字後面可以直接接 @(「請看@docs/a.png」)。
_MENTION = re.compile(r'(?<![A-Za-z0-9_.@/\\-])@(?:"([^"\n]+)"|([^\s"`]+))')
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_TRAILING_PUNCTUATION = ",.;:!?)]}>'，。；：！？）】」』、"
# 中文句子常把全形標點直接接在路徑後面(「@docs/a.txt，謝謝」)。
_CJK_PUNCTUATION = re.compile("[，。；：！？、）】」』]")
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_UNLISTABLE = re.compile(r'[\x00-\x1f\x7f-\x9f"`]')

_OUTSIDE = "專案外檔案不會附加（可先複製進專案，或請模型用 import_external_file）"
_HOME = "只支援專案內路徑"
_LINK = "符號連結不附加"
_LINKED_PARENT = "路徑含符號連結或非目錄，不附加"
_DIRECTORY = "目錄不附加"
_SPECIAL = "不是一般檔案"
_MISSING = "找不到或無法讀取"
_CONTROL_CHARS = "路徑含控制字元"
_INCAPABLE = "此平台缺 dir-fd／nofollow，附件停用"
_OVER_LIMIT = f"超過單則 {MAX_ATTACHMENTS} 個附件上限"


@dataclass(frozen=True)
class Mention:
    """訊息裡的一個 ``@``。``path`` 是去掉 ``@`` 與引號的字面路徑。"""

    raw: str
    path: str
    start: int
    end: int
    quoted: bool


@dataclass(frozen=True)
class Attachment:
    """一個要交給工具的附件。``path`` 是 root 相對、正規化後的 POSIX 字面路徑。"""

    path: str
    tool: str
    kind: str


@dataclass(frozen=True)
class Skipped:
    raw: str
    reason: str


@dataclass(frozen=True)
class Resolution:
    attachments: tuple[Attachment, ...]
    skipped: tuple[Skipped, ...]


@dataclass(frozen=True)
class Completion:
    """``text[start:end]`` 要被 ``items`` 其中一項的第一格取代;第二格是說明。"""

    start: int
    end: int
    items: tuple[tuple[str, str], ...]


_EMPTY = Resolution((), ())


class _Reject(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ============================================================
# 純字串
# ============================================================
def _code_spans(text: str) -> list[tuple[int, int]]:
    spans = [match.span() for match in _FENCE.finditer(text)]

    def inside(position: int) -> bool:
        return any(start <= position < end for start, end in spans)

    for match in _INLINE_CODE.finditer(text):
        if not inside(match.start()):
            spans.append(match.span())
    return spans


def find_mentions(text: str) -> tuple[Mention, ...]:
    """依出現順序列出訊息裡的 @(``` 與 `inline` code 內的不算)。不碰檔案系統。"""
    if not isinstance(text, str) or "@" not in text:
        return ()
    spans = _code_spans(text)
    found: list[Mention] = []
    for match in _MENTION.finditer(text):
        start = match.start()
        if any(low <= start < high for low, high in spans):
            continue
        quoted = match.group(1) is not None
        path = match.group(1) if quoted else match.group(2)
        found.append(Mention(match.group(0), path, start, match.end(), quoted))
    return tuple(found)


def _candidates(mention: Mention) -> tuple[str, ...]:
    """先試原樣;無引號時再試去掉尾端標點(「@docs/a.png，」),以及截在第一個全形標點
    之前(「@docs/a.txt，謝謝」)。原樣永遠第一個試,檔名本身含這些字元時仍指向它。"""
    if mention.quoted:
        return (mention.path,)
    options = [mention.path]
    stripped = mention.path.rstrip(_TRAILING_PUNCTUATION)
    cut = _CJK_PUNCTUATION.split(mention.path, maxsplit=1)[0].rstrip(_TRAILING_PUNCTUATION)
    for option in (stripped, cut):
        if option and option not in options:
            options.append(option)
    return tuple(options)


def path_like(mention: Mention) -> bool:
    """看起來像路徑:含 ``/`` 或以 .副檔名結尾。``@property``、``@user`` 不是。"""
    return any("/" in candidate or _EXTENSION.search(candidate)
               for candidate in _candidates(mention))


def path_mentions(text: str) -> tuple[Mention, ...]:
    """看起來像路徑的 @(補充訊息防呆用;純字串,零 I/O)。"""
    return tuple(mention for mention in find_mentions(text) if path_like(mention))


def route(path: str) -> tuple[str, str]:
    """(工具, 類型):只看小寫字面副檔名,與 mcp_server.analyze_file 的分流一致。"""
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in config.IMAGE_EXTENSIONS:
        return "analyze_file", "image"
    if suffix in _PDF_EXTENSIONS:
        return "analyze_file", "pdf"
    if suffix in _ELF_EXTENSIONS:
        return "analyze_file", "elf"
    if suffix in _BINARY_EXTENSIONS:
        return "analyze_file", "binary"
    return "read_file", "text"


def _lexical(candidate: str, root: str) -> str:
    """字串層:回 root 相對的正規化路徑,或 raise _Reject。**不碰檔案系統**。"""
    if not candidate:
        raise _Reject(_MISSING)
    if candidate.startswith("~"):
        raise _Reject(_HOME)
    if _CONTROL.search(candidate):
        raise _Reject(_CONTROL_CHARS)
    if ".." in candidate.split("/"):
        raise _Reject(_OUTSIDE)
    if candidate.startswith("/"):
        normalized = posixpath.normpath(candidate)
        prefix = root.rstrip("/") + "/"
        if normalized == root.rstrip("/"):
            raise _Reject(_DIRECTORY)
        if not normalized.startswith(prefix):
            raise _Reject(_OUTSIDE)
        relative = normalized[len(prefix):]
    else:
        relative = posixpath.normpath(candidate)
    if relative in ("", "."):
        raise _Reject(_DIRECTORY)
    if relative.startswith("/") or ".." in relative.split("/"):
        raise _Reject(_OUTSIDE)
    return relative


# ============================================================
# 檔案系統(逐層 nofollow)
# ============================================================
# 能力判斷用 import 當下的原生函式:測試或除錯工具包裝 os.open／os.stat 時,
# 不能讓「有沒有 dir-fd 能力」跟著變成否(同 command_allowlist 的做法)。
_NATIVE_OPEN, _NATIVE_STAT, _NATIVE_SCANDIR = os.open, os.stat, os.scandir


def _capable() -> bool:
    return bool(
        getattr(os, "O_NOFOLLOW", 0)
        and getattr(os, "O_DIRECTORY", 0)
        and _NATIVE_OPEN in getattr(os, "supports_dir_fd", ())
        and _NATIVE_STAT in getattr(os, "supports_dir_fd", ())
        and _NATIVE_STAT in getattr(os, "supports_follow_symlinks", ())
        and _NATIVE_SCANDIR in getattr(os, "supports_fd", ())
    )


def _root_parts(root: str) -> list[str]:
    return [part for part in root.split("/") if part]


def _open_dir_chain(parts: Sequence[str], *, readable: bool) -> int:
    """自 ``/`` 逐層 ``O_DIRECTORY|O_NOFOLLOW`` 開到最後一段,回傳它的 fd(呼叫端關)。

    任何一段是 symlink 或不是目錄 → ENOTDIR／ELOOP(OSError)。中間層用 ``O_PATH``
    (只需要 execute 權限就能往下走);``readable`` 時最後一段用 ``O_RDONLY``,
    ``scandir(fd)`` 才能用。
    """
    cloexec = getattr(os, "O_CLOEXEC", 0)
    walk = getattr(os, "O_PATH", 0) or os.O_RDONLY
    step = walk | os.O_DIRECTORY | os.O_NOFOLLOW | cloexec
    final = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | cloexec) if readable else step
    fd = os.open("/", (final if readable and not parts else walk | os.O_DIRECTORY) | cloexec)
    try:
        for index, part in enumerate(parts):
            flags = final if index == len(parts) - 1 else step
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _inspect(relative: str, root: str) -> None:
    """FS 層:確認 ``root/relative`` 是普通檔,否則 raise _Reject。"""
    parts = relative.split("/")
    try:
        fd = _open_dir_chain(_root_parts(root) + parts[:-1], readable=False)
    except OSError as exc:
        linked = exc.errno in (errno.ENOTDIR, errno.ELOOP)
        raise _Reject(_LINKED_PARENT if linked else _MISSING) from None
    try:
        info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
    except OSError:
        raise _Reject(_MISSING) from None
    finally:
        os.close(fd)
    if stat.S_ISLNK(info.st_mode):
        raise _Reject(_LINK)
    if stat.S_ISDIR(info.st_mode):
        raise _Reject(_DIRECTORY)
    if not stat.S_ISREG(info.st_mode):
        raise _Reject(_SPECIAL)


def _root_text(root: Path | str) -> str | None:
    """root 必須是絕對路徑(engine 啟動時已 resolve);否則附件停用。"""
    text = os.fspath(root)
    return text if isinstance(text, str) and text.startswith("/") else None


def resolve(text: str, root: Path | str | None) -> Resolution:
    """判定哪些 @ 是附件。root 為 None 或文字沒有 @ → 空結果,零 I/O。"""
    if root is None or not isinstance(text, str) or "@" not in text:
        return _EMPTY
    mentions = find_mentions(text)
    if not mentions:
        return _EMPTY
    root_text = _root_text(root)
    if root_text is None:
        return _EMPTY
    capable = _capable()
    attachments: list[Attachment] = []
    skipped: list[Skipped] = []
    seen: set[str] = set()
    for mention in mentions[:MAX_MENTIONS]:
        relative: str | None = None
        reason = ""
        for candidate in _candidates(mention):
            try:
                lexical = _lexical(candidate, root_text)
            except _Reject as exc:
                reason = reason or exc.reason
                continue
            if lexical in seen:
                relative = lexical
                break
            if not capable:
                reason = reason or _INCAPABLE
                continue
            try:
                _inspect(lexical, root_text)
            except _Reject as exc:
                reason = reason or exc.reason
                continue
            relative = lexical
            break
        if relative is None:
            entry = Skipped(mention.raw, reason or _MISSING)
            if path_like(mention) and entry not in skipped:
                skipped.append(entry)
            continue
        if relative in seen:
            continue
        if len(attachments) >= MAX_ATTACHMENTS:
            skipped.append(Skipped(mention.raw, _OVER_LIMIT))
            continue
        seen.add(relative)
        tool, kind = route(relative)
        attachments.append(Attachment(relative, tool, kind))
    rest = len(mentions) - MAX_MENTIONS
    if rest > 0:
        skipped.append(Skipped("…", f"單則最多檢查 {MAX_MENTIONS} 個 @，其餘 {rest} 個未處理"))
    return Resolution(tuple(attachments), tuple(skipped))


def describe(resolution: Resolution) -> tuple[str, ...]:
    """附件摘要(TUI 預覽與 notice 共用)。"""
    lines = [
        f"附件 {item.path} → {item.tool}（{_KIND_LABELS.get(item.kind, item.kind)}）"
        for item in resolution.attachments
    ]
    lines.extend(f"未附加 {item.raw}：{item.reason}" for item in resolution.skipped)
    return tuple(lines)


def skipped_notice(resolution: Resolution) -> str:
    if not resolution.skipped:
        return ""
    return "以下 @ 路徑未附加，已當成文字送出：" + "；".join(
        f"{item.raw}（{item.reason}）" for item in resolution.skipped
    )


def tool_calls(attachments: Sequence[Attachment]) -> list[dict[str, Any]]:
    """附件 → 與 client_engine._finalise_tool_calls 同形狀的呼叫(id 前綴 ``attach_``)。"""
    token = secrets.token_hex(6)
    calls: list[dict[str, Any]] = []
    for index, attachment in enumerate(attachments):
        call_id = f"attach_{token}_{index}"
        arguments = {"path": attachment.path}
        calls.append({
            "id": call_id,
            "name": attachment.tool,
            "arguments": arguments,
            "arguments_error": False,
            "wire": {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": attachment.tool,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            },
        })
    return calls


# ============================================================
# 補全
# ============================================================
def _token_at(text: str, cursor: int) -> tuple[int, str, bool] | None:
    """游標所在 @ token:(``@`` 的位置, @ 後到游標的字, 是否未閉合引號)。"""
    if cursor < len(text) and not text[cursor].isspace():
        return None
    line_start = text.rfind("\n", 0, cursor) + 1
    line = text[line_start:cursor]
    quote = line.rfind('"')
    if quote > 0 and line[quote - 1] == "@":
        at = line_start + quote - 1
        if at == 0 or not re.match(r"[A-Za-z0-9_.@/\\-]", text[at - 1]):
            return at, line[quote + 1:], True
    boundary = max(line.rfind(" "), line.rfind("\t"), line.rfind("　"))
    segment_start = line_start + boundary + 1
    segment = text[segment_start:cursor]
    for offset, char in enumerate(segment):
        if char != "@":
            continue
        at = segment_start + offset
        if at > 0 and re.match(r"[A-Za-z0-9_.@/\\-]", text[at - 1]):
            continue
        prefix = text[at + 1:cursor]
        if '"' in prefix or "`" in prefix or any(ch.isspace() for ch in prefix):
            return None
        return at, prefix, False
    return None


def completions(
    text: str, cursor: int, root: Path | str | None, *, limit: int = MAX_COMPLETIONS,
) -> Completion | None:
    """游標位於 @ token 尾端時列出專案內候選。其他情況、任何 OSError → None。零寫入。"""
    if root is None or not isinstance(text, str) or not 0 <= cursor <= len(text):
        return None
    if "@" not in text[:cursor] or not _capable():
        return None
    token = _token_at(text, cursor)
    if token is None:
        return None
    at, prefix, quoted = token
    if prefix.startswith(("/", "~")) or _CONTROL.search(prefix) or ".." in prefix.split("/"):
        return None
    directory, _, name_part = prefix.rpartition("/")
    parts = [part for part in directory.split("/") if part not in ("", ".")]
    root_text = _root_text(root)
    if root_text is None:
        return None
    entries: list[tuple[bool, str]] = []
    try:
        fd = _open_dir_chain(_root_parts(root_text) + parts, readable=True)
        try:
            with os.scandir(fd) as iterator:
                for scanned, entry in enumerate(iterator):
                    if scanned >= MAX_COMPLETION_SCAN:
                        break
                    name = entry.name
                    if not name.startswith(name_part) or _UNLISTABLE.search(name):
                        continue
                    if name.startswith(".") and not name_part.startswith("."):
                        continue
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        entries.append((True, name))
                    elif entry.is_file(follow_symlinks=False):
                        entries.append((False, name))
        finally:
            os.close(fd)
    except OSError:
        return None
    if not entries:
        return None
    entries.sort(key=lambda item: (not item[0], item[1].lower(), item[1]))
    items: list[tuple[str, str]] = []
    base = directory + "/" if directory else ""
    for is_dir, name in entries[:max(0, limit)]:
        relative = base + name + ("/" if is_dir else "")
        needs_quotes = quoted or any(ch.isspace() for ch in relative)
        if needs_quotes:
            inserted = f'@"{relative}' if is_dir else f'@"{relative}"'
        else:
            inserted = f"@{relative}"
        if is_dir:
            description = "目錄"
        else:
            tool, kind = route(relative)
            description = f"{tool} · {_KIND_LABELS.get(kind, kind)}"
        items.append((inserted, description))
    return Completion(at, cursor, tuple(items)) if items else None
