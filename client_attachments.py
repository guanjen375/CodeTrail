#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_attachments — 輸入框 ``@路徑`` 附件的解析、路由與補全(TUI 與 headless 共用)。

使用者在訊息裡寫 ``@docs/spec.pdf`` 或 ``@"my docs/spec.pdf"``,送出時由 engine 以
**既有的 MCP 工具**讀它:圖片／PDF／ELF／firmware binary 走 ``analyze_file``(圖片就是
既有的 VL 路徑),其他檔案走 ``read_file``。專案外的檔案(``@~/Downloads/截圖.png``)
送出時先以 ``import_external_file`` 複製進專案(逐次核准),成功才讀複製出來的那一份。
這個模組只負責「哪些 @ 是附件、交給哪個工具」;真正讀內容的是 server,而 server 的
sandbox 與匯入檢查仍然會重驗路徑。

邊界(AGENTS.md §2「TUI 的 @ 附件」):

* **專案內只附加普通檔。** 字串層先拒 ``..``,以及沒有外部範圍時的 ``~`` 與專案外路徑 ——
  那些連一次 stat 都不做。其餘自 ``/`` 沿 root 與父目錄**逐層** ``O_NOFOLLOW`` 開啟,
  葉節點以 ``stat(dir_fd, follow_symlinks=False)`` 判斷;任何一層或葉節點是 symlink
  就不附加。只驗最終路徑擋不住「中間一層被換成指向專案外的 symlink」。
* **專案外只在外部匯入開啟、而且字面上落在來源根內時才碰檔案系統。** 範圍
  (:class:`ExternalScope`)由啟動時的設定產生、以參數交進來;關閉、根外、``~user``、
  Windows 路徑、不支援的副檔名一律在字串層拒絕(零 FS)。落在根內的,根以
  ``realpath`` 解析(與 server 的 resolve 同義),再自 ``/`` 逐層 ``O_NOFOLLOW`` 驗到葉節點,
  只 stat 不讀內容;實體其實在專案 root 內就改成專案內附件,不發匯入。
* **補全只列已驗證目錄 fd 裡的項目。** 目錄以同一條逐層 nofollow 開到 fd 再
  ``scandir(fd)``:檢查之後才被換成 symlink 的路徑名稱,不會讓清單變成專案外的內容。
  檔名搜尋(``@uart``)從持有的 root fd 逐層 nofollow 重開每個子目錄做 BFS,有界、跳過
  symlink、特殊檔與隱藏項目;外部來源只列來源根內的目錄與可匯入的檔,依修改時間新到舊。
* **貼上改寫是純字串。** :func:`paste_mentions` 只把「整段都是路徑」的貼上(終端機拖放)
  改寫成 @ 語法,不碰檔案系統。
* **有上限、零寫入、不讀環境、不 print。** 單則最多檢查 ``MAX_MENTIONS`` 個 @、
  附加 ``MAX_ATTACHMENTS`` 個;補全最多掃 ``MAX_COMPLETION_SCAN`` 個目錄項、檔名搜尋最多
  ``MAX_SEARCH_SCAN`` 個。HOME 只經 :class:`ExternalScope` 交進來。會碰檔案系統的函式
  (``resolve``、``completions``)不在 UI 執行緒呼叫;``find_mentions``／``path_mentions``／
  ``route``／``paste_mentions`` 是純字串。

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
import shlex
import stat
import time
import urllib.parse
from collections import deque
from collections.abc import Iterable, Sequence
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
#: 專案外附件交給的工具:送出時先匯入進專案(逐次核准),成功才讀複製出來的那一份。
IMPORT_TOOL = "import_external_file"
#: 檔名搜尋:@ 後面的名稱至少幾個字才搜整個專案。
MIN_SEARCH_CHARS = 2
#: 檔名搜尋最多掃幾個目錄項(含目錄本身;BFS,淺的先)。
MAX_SEARCH_SCAN = 20000
#: 檔名搜尋最多往下走幾層目錄。
MAX_SEARCH_DEPTH = 12
#: 貼上改寫只看這麼長以內的貼上(路徑不會更長;長的貼上多半是程式碼或 log)。
MAX_PASTE_CHARS = 4096

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
# Windows 路徑(`C:\…`、`C:/…`、`\\server\share`):SSH 進來的使用者把本機檔案拖進終端機得到的就是它。
_WINDOWS_PATH = re.compile(r"[A-Za-z]:[\\/]|\\\\")
_WINDOWS_TOKEN = re.compile(r'"((?:[A-Za-z]:[\\/]|\\\\)[^"\r\n]*)"|((?:[A-Za-z]:[\\/]|\\\\)\S*)')
# 貼上改寫:插入點前一字元是這些時先補空白(@ 前面緊接它們就不算 mention,見 _MENTION)。
_GLUED = re.compile(r"[A-Za-z0-9_.@/\\-]")
#: 終端機拖放檔案時會貼上的路徑開頭。
_PASTE_HEADS = ("/", "~/", "file://")

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
_WINDOWS = "Windows 路徑：aicode 讀的是這台主機上的檔案，請先傳到這台主機（例如 ~/Downloads）"
_TILDE_USER = "只支援 ~/ 開頭"
_EXTERNAL_OFF = "專案外檔案：外部匯入未開啟（/import on 後重開 aicode；每次匯入仍需核准）"
_EXTERNAL_EXTENSION = "外部匯入不支援此副檔名"


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
    """一個要交給工具的附件。

    專案內:``path`` 是 root 相對、正規化後的 POSIX 字面路徑,``tool`` 是 analyze_file／
    read_file。專案外:``tool`` 是 :data:`IMPORT_TOOL`、``path`` 是展開後的字面絕對來源路徑
    (= import_external_file 的 path 參數)、``kind`` 依來源字面副檔名、``display`` 是使用者寫法。
    """

    path: str
    tool: str
    kind: str
    display: str = ""

    @property
    def external(self) -> bool:
        return self.tool == IMPORT_TOOL


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


@dataclass(frozen=True)
class ExternalScope:
    """專案外 @ 附件的範圍:啟動時由 runtime 設定產生(這個模組不讀環境)。

    ``enabled``／``roots`` 是本次執行的外部匯入開關與來源根(client.json 原字串);
    ``home`` 是展開 ``~/`` 用的絕對路徑(None = 不展開);``max_bytes`` > 0 時預檢大小;
    ``extensions`` 非空時只收這些(小寫)副檔名 —— 與 server 的匯入檢查同一份值。
    """

    enabled: bool
    home: str | None
    roots: tuple[str, ...]
    max_bytes: int
    extensions: frozenset[str]


@dataclass(frozen=True)
class _Target:
    """字串層判定的結果。``key`` 是去重鍵:("p", root 相對) 或 ("x", 字面絕對路徑)。"""

    key: tuple[str, str]
    relative: str = ""
    absolute: str = ""
    root: str = ""
    inside: str = ""
    display: str = ""


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


def _normabs(path: str) -> str:
    """絕對路徑的字面正規化(不碰 FS):折疊 ``//``、``./`` 與結尾 ``/``。``..`` 由呼叫端先拒。"""
    return "/" + posixpath.normpath(path).lstrip("/")


def _home_join(home: str, rest: str) -> str:
    """``~`` 之後的部分(``""`` 或 ``/…``)接到 home 上。"""
    return (home.rstrip("/") + rest) or "/"


def _expand_root(configured: str, home: str | None) -> str | None:
    """來源根的字面展開(``~/`` 以 home 展開、normpath);認不得的根回 None(永遠不比對到)。

    ``~user``、相對路徑、含 ``..`` 或控制字元的根 client 端一律不比對 —— 寧可說「不在來源」,
    也不猜 server 的 resolve 會把它變成哪裡。
    """
    text = configured.strip() if isinstance(configured, str) else ""
    if not text or _CONTROL.search(text):
        return None
    if text.startswith("~"):
        if home is None or (text != "~" and not text.startswith("~/")):
            return None
        text = _home_join(home, text[1:])
    if not text.startswith("/") or ".." in text.split("/"):
        return None
    return _normabs(text)


def _scope_roots(scope: ExternalScope) -> list[str]:
    roots: list[str] = []
    for configured in scope.roots:
        expanded = _expand_root(configured, scope.home)
        if expanded is not None and expanded not in roots:
            roots.append(expanded)
    return roots


def _tilde_form(root: str, home: str | None) -> str | None:
    """來源根以 ``~`` 開頭的寫法(不在 home 內 → None)。"""
    if home is None:
        return None
    if home == "/":
        return "~" + root
    if root == home:
        return "~"
    if root.startswith(home + "/"):
        return "~" + root[len(home):]
    return None


def _not_in_roots(scope: ExternalScope) -> str:
    listed = "、".join(scope.roots) or "未設定"
    return f"不在外部匯入來源（{listed}）；若是你本機的檔案，請先傳到這台主機的來源目錄"


def _size_text(size: int) -> str:
    mib = 1024 * 1024
    if size >= mib and size % mib == 0:
        return f"{size // mib} MB"
    if size >= mib:
        return f"{size / mib:.1f} MB"
    return f"{size:,} bytes"


def external_scope(
    *,
    enabled: bool,
    roots: Sequence[str],
    home: str | None,
    max_bytes: int,
    extensions: Iterable[str],
) -> ExternalScope:
    """由啟動時的 runtime 設定建立 :class:`ExternalScope`。不讀環境、不碰檔案系統。

    home 不是乾淨的絕對路徑 → None(``~/`` 不展開);roots 去頭尾空白、丟空字串、
    保留順序去重;extensions 轉小寫。
    """
    home_path = None
    if (isinstance(home, str) and home.startswith("/") and not _CONTROL.search(home)
            and ".." not in home.split("/")):
        home_path = _normabs(home)
    cleaned: list[str] = []
    for item in roots or ():
        text = item.strip() if isinstance(item, str) else ""
        if text and text not in cleaned:
            cleaned.append(text)
    return ExternalScope(
        enabled=bool(enabled),
        home=home_path,
        roots=tuple(cleaned),
        max_bytes=int(max_bytes),
        extensions=frozenset(str(item).lower() for item in (extensions or ())),
    )


def _classify(candidate: str, root: str, scope: ExternalScope | None) -> _Target:
    """字串層:這個 candidate 是專案內的哪個檔,或哪個來源根裡的專案外檔。**不碰檔案系統**。

    scope 為 None(替身、沒有啟動設定的呼叫端)時完全沿用 :func:`_lexical`,輸出與加入
    專案外附件之前逐字相同。
    """
    if scope is None:
        relative = _lexical(candidate, root)
        return _Target(("p", relative), relative=relative)
    if not candidate:
        raise _Reject(_MISSING)
    if _CONTROL.search(candidate):
        raise _Reject(_CONTROL_CHARS)
    if _WINDOWS_PATH.match(candidate):
        raise _Reject(_WINDOWS)
    path = candidate
    if path.startswith("~"):
        if scope.home is None:
            raise _Reject(_HOME)
        if path != "~" and not path.startswith("~/"):
            raise _Reject(_TILDE_USER)
        path = _home_join(scope.home, path[1:])
    if ".." in path.split("/"):
        raise _Reject(_OUTSIDE)
    if not path.startswith("/"):
        relative = posixpath.normpath(path)
        if relative in ("", "."):
            raise _Reject(_DIRECTORY)
        if relative.startswith("/") or ".." in relative.split("/"):
            raise _Reject(_OUTSIDE)
        return _Target(("p", relative), relative=relative)
    absolute = _normabs(path)
    base = root.rstrip("/") or "/"
    if absolute == base:
        raise _Reject(_DIRECTORY)
    prefix = base.rstrip("/") + "/"
    if absolute.startswith(prefix):
        relative = absolute[len(prefix):]
        return _Target(("p", relative), relative=relative)
    if not scope.enabled:
        raise _Reject(_EXTERNAL_OFF)
    for source_root in _scope_roots(scope):
        if absolute == source_root:
            raise _Reject(_DIRECTORY)
        head = source_root.rstrip("/") + "/"
        if not absolute.startswith(head):
            continue
        inside = absolute[len(head):]
        if scope.extensions and PurePosixPath(inside).suffix.lower() not in scope.extensions:
            raise _Reject(_EXTERNAL_EXTENSION)
        return _Target(("x", absolute), absolute=absolute, root=source_root,
                       inside=inside, display=candidate)
    raise _Reject(_not_in_roots(scope))


# ============================================================
# 貼上(純字串)
# ============================================================
def _decoded_paste(token: str) -> str | None:
    """``file://`` 轉成路徑(主機空或 localhost、UTF-8 strict);含引號／反引號／控制字元 → None。"""
    if token.startswith("file://"):
        try:
            # 畸形 URI(`file://[bad/…`)會讓 urlsplit 丟 ValueError:那不是路徑,原樣貼上。
            split = urllib.parse.urlsplit(token)
        except ValueError:
            return None
        if split.netloc not in ("", "localhost") or split.query or split.fragment:
            return None
        try:
            token = urllib.parse.unquote(split.path, encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
        if not token.startswith("/"):
            return None
    if not token or '"' in token or "`" in token or _CONTROL.search(token):
        return None
    return token


def _posix_paste(body: str) -> list[str] | None:
    """整段是一到多個 POSIX 路徑(shell 引號／跳脫、file://)時回路徑清單,否則 None。"""
    try:
        parts = shlex.split(body)
    except ValueError:
        parts = []
    if parts and all(part.startswith(_PASTE_HEADS) for part in parts):
        decoded = [_decoded_paste(part) for part in parts]
        return None if any(item is None for item in decoded) else [item for item in decoded if item]
    # 不加引號、含空白的單一路徑(Alacritty 之類)。要以副檔名結尾,「/tmp 滿了怎麼辦」
    # 這種以路徑開頭的句子才不會被當成一個路徑。
    if ("\n" in body or "\r" in body or not body.startswith(_PASTE_HEADS)
            or not _EXTENSION.search(body)):
        return None
    single = _decoded_paste(body)
    return [single] if single is not None else None


def _windows_paste(body: str) -> list[str] | None:
    """整段是一到多個 Windows 路徑(可被雙引號包住)時回清單,否則 None。"""
    if not _WINDOWS_PATH.match(body.lstrip('"')):
        return None
    paths: list[str] = []
    position = 0
    while position < len(body):
        match = _WINDOWS_TOKEN.match(body, position)
        if match is None:
            return None
        paths.append(match.group(1) if match.group(1) is not None else match.group(2))
        position = match.end()
        gap = position
        while position < len(body) and body[position].isspace():
            position += 1
        if position < len(body) and position == gap:
            return None
    return paths


def _shortened(path: str, root: Path | str | None, home: str | None) -> str:
    """在 root 內 → root 相對;在 home 內 → ``~/…``;其餘(含有 ``..`` 的)原樣。"""
    if not path.startswith("/") or ".." in path.split("/"):
        return path
    absolute = _normabs(path)
    base = _root_text(root) if root is not None else None
    if base is not None and ".." not in base.split("/"):
        prefix = _normabs(base).rstrip("/") + "/"
        if absolute.startswith(prefix):
            relative = absolute[len(prefix):]
            if relative and not relative.startswith("~"):
                return relative
    if (isinstance(home, str) and home.startswith("/") and not _CONTROL.search(home)
            and ".." not in home.split("/")):
        home_path = _normabs(home)
        if home_path != "/" and absolute.startswith(home_path + "/"):
            return "~" + absolute[len(home_path):]
    return path


def paste_mentions(
    text: str, *, before: str = "", root: Path | str | None = None, home: str | None = None,
) -> str | None:
    """貼上的是「整段都是路徑」(終端機拖放檔案)時,改寫成 @ 附件語法;否則 None(原樣貼上)。

    純字串、零 I/O。每段路徑成為一個 ``@路徑``(含空白或 Windows 路徑用 ``@"…"``),在 root 內
    的寫成 root 相對、home 內的寫成 ``~/…``;以空白分隔、結尾補一個空白(游標不停在 @ token 上)。
    ``before`` 是插入點前一個字元:``"`` → 不改寫;``@`` → 第一段不再加 @;英數或 ``_.@/\\-``
    → 前面補空白。段數 1..MAX_ATTACHMENTS;含引號、反引號、控制字元或混有其他文字 → None。
    斜線指令的判斷不在這裡(那需要知道有哪些指令),由呼叫端先擋。
    """
    if not isinstance(text, str) or len(text) > MAX_PASTE_CHARS:
        return None
    body = text.strip()
    previous = before[-1:] if isinstance(before, str) else ""
    if not body or previous == '"':
        return None
    paths = _windows_paste(body)
    windows = paths is not None
    if paths is None:
        paths = _posix_paste(body)
    if not paths or len(paths) > MAX_ATTACHMENTS:
        return None
    tokens: list[str] = []
    for path in paths:
        if not path or '"' in path or "`" in path or _CONTROL.search(path):
            return None
        shown = path if windows else _shortened(path, root, home)
        if windows or any(char.isspace() for char in shown):
            tokens.append(f'@"{shown}"')
        else:
            tokens.append(f"@{shown}")
    joined = " ".join(tokens)
    if previous == "@":
        joined = joined[1:]
    elif previous and _GLUED.match(previous):
        joined = " " + joined
    return joined + " "


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


def _open_below(base_fd: int, parts: Sequence[str]) -> int:
    """自一個已驗證的目錄 fd 往下逐層 ``O_NOFOLLOW|O_DIRECTORY`` 開到 ``parts`` 的最後一段。

    中間層 ``O_PATH``、最後一段 ``O_RDONLY``(``scandir(fd)`` 才能用);任何一段是 symlink
    或不是目錄 → OSError。``parts`` 不得為空;回傳新的 fd(呼叫端關),``base_fd`` 不動。
    """
    cloexec = getattr(os, "O_CLOEXEC", 0)
    walk = getattr(os, "O_PATH", 0) or os.O_RDONLY
    step = walk | os.O_DIRECTORY | os.O_NOFOLLOW | cloexec
    final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | cloexec
    fd = base_fd
    try:
        for index, part in enumerate(parts):
            child = os.open(part, final if index == len(parts) - 1 else step, dir_fd=fd)
            if fd != base_fd:
                os.close(fd)
            fd = child
    except BaseException:
        if fd != base_fd:
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


def _resolved_root(source_root: str) -> str | None:
    """來源根的實體路徑(realpath,與 server 的 Path.resolve 同義);不存在或不是絕對路徑 → None。"""
    try:
        resolved = os.path.realpath(source_root, strict=True)
    except (OSError, ValueError):
        return None
    return resolved if isinstance(resolved, str) and resolved.startswith("/") else None


def _inspect_external(target: _Target, root: str, scope: ExternalScope) -> str | None:
    """FS 層(專案外):確認來源根內的那個檔是普通檔。

    實體路徑(解析後的根＋根內相對路徑)其實落在專案 root 內 → 以既有的 :func:`_inspect`
    驗證並回傳 root 相對路徑(改成專案內附件,不發匯入);否則驗證通過回 None。
    不合格 raise _Reject。只 stat,不讀內容。
    """
    resolved = _resolved_root(target.root)
    if resolved is None:
        raise _Reject(_MISSING)
    real = resolved.rstrip("/") + "/" + target.inside
    base = root.rstrip("/") or "/"
    if real == base:
        raise _Reject(_DIRECTORY)
    prefix = base.rstrip("/") + "/"
    if real.startswith(prefix):
        relative = real[len(prefix):]
        _inspect(relative, root)
        return relative
    parts = _root_parts(resolved) + target.inside.split("/")
    try:
        fd = _open_dir_chain(parts[:-1], readable=False)
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
    if scope.max_bytes > 0 and info.st_size > scope.max_bytes:
        raise _Reject(f"超過匯入上限 {_size_text(scope.max_bytes)}")
    return None


def _verify(
    target: _Target, root: str, scope: ExternalScope | None,
) -> tuple[tuple[str, str], Attachment]:
    """FS 層:驗證字串層的判定,回 (去重鍵, 附件) 或 raise _Reject。"""
    if target.key[0] == "p":
        _inspect(target.relative, root)
        tool, kind = route(target.relative)
        return target.key, Attachment(target.relative, tool, kind)
    assert scope is not None
    landed = _inspect_external(target, root, scope)
    if landed is not None:
        tool, kind = route(landed)
        return ("p", landed), Attachment(landed, tool, kind)
    _tool, kind = route(target.absolute)
    return target.key, Attachment(target.absolute, IMPORT_TOOL, kind, display=target.display)


def _root_text(root: Path | str) -> str | None:
    """root 必須是絕對路徑(engine 啟動時已 resolve);否則附件停用。"""
    text = os.fspath(root)
    return text if isinstance(text, str) and text.startswith("/") else None


def resolve(
    text: str, root: Path | str | None, *, external: ExternalScope | None = None,
) -> Resolution:
    """判定哪些 @ 是附件。root 為 None 或文字沒有 @ → 空結果,零 I/O。

    ``external`` 是啟動時的外部匯入範圍;None 時專案外一律在字串層拒絕(與加入專案外
    附件之前逐字相同)。
    """
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
    seen: set[tuple[str, str]] = set()
    for mention in mentions[:MAX_MENTIONS]:
        chosen: Attachment | None = None
        chosen_key: tuple[str, str] | None = None
        reason = ""
        for candidate in _candidates(mention):
            try:
                target = _classify(candidate, root_text, external)
            except _Reject as exc:
                reason = reason or exc.reason
                continue
            if target.key in seen:
                chosen_key = target.key
                break
            if not capable:
                reason = reason or _INCAPABLE
                continue
            try:
                chosen_key, chosen = _verify(target, root_text, external)
            except _Reject as exc:
                reason = reason or exc.reason
                continue
            break
        if chosen_key is None:
            entry = Skipped(mention.raw, reason or _MISSING)
            if path_like(mention) and entry not in skipped:
                skipped.append(entry)
            continue
        if chosen_key in seen or chosen is None:
            continue
        if len(attachments) >= MAX_ATTACHMENTS:
            skipped.append(Skipped(mention.raw, _OVER_LIMIT))
            continue
        seen.add(chosen_key)
        attachments.append(chosen)
    rest = len(mentions) - MAX_MENTIONS
    if rest > 0:
        skipped.append(Skipped("…", f"單則最多檢查 {MAX_MENTIONS} 個 @，其餘 {rest} 個未處理"))
    return Resolution(tuple(attachments), tuple(skipped))


def describe(resolution: Resolution) -> tuple[str, ...]:
    """附件摘要(TUI 預覽與 notice 共用)。"""
    lines: list[str] = []
    for item in resolution.attachments:
        label = _KIND_LABELS.get(item.kind, item.kind)
        if item.external:
            lines.append(
                f"附件 {item.display or item.path} → 匯入後 {route(item.path)[0]}（{label}；匯入需核准）"
            )
        else:
            lines.append(f"附件 {item.path} → {item.tool}（{label}）")
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


def follow_up(attachment: Attachment, landed: str) -> Attachment:
    """專案外附件匯入成功後的讀取:``landed`` 是 root 相對落點,依它的副檔名路由。"""
    tool, kind = route(landed)
    return Attachment(landed, tool, kind, display=attachment.display or attachment.path)


# ============================================================
# 補全
# ============================================================
#: 「多久前」用的時鐘(測試可換)。
_now = time.time


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


def _inserted(path: str, *, is_dir: bool, quoted: bool) -> str:
    """補全插入的文字:含空白(或已開引號)就用 ``@"…"``;目錄不閉合引號,好繼續補全。"""
    if quoted or any(ch.isspace() for ch in path):
        return f'@"{path}' if is_dir else f'@"{path}"'
    return f"@{path}"


def _file_detail(path: str) -> str:
    tool, kind = route(path)
    return f"{tool} · {_KIND_LABELS.get(kind, kind)}"


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "剛剛"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分鐘前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小時前"
    return f"{hours // 24} 天前"


def _search(root: str, query: str, *, skip: set[str], limit: int) -> list[str]:
    """整個專案裡 basename 含 ``query``(casefold)的普通檔,root 相對、排好序。

    root fd 以 :func:`_open_dir_chain` 開一次、全程持有;每個子目錄都從它逐層
    ``O_NOFOLLOW|O_DIRECTORY`` 重開(:func:`_open_below`)再 ``scandir(fd)``、掃完即關,
    所以走訪中途被換成 symlink 的目錄只會開不進去,不會列出專案外的名稱。BFS(淺的先),
    跳過 symlink、特殊檔、列不出來的名稱與隱藏目錄;隱藏檔只在查詢字以 ``.`` 開頭時列。
    最多掃 MAX_SEARCH_SCAN 個目錄項、往下 MAX_SEARCH_DEPTH 層;單一子目錄開不了只略過它。
    排序:basename 前綴相符優先 → 深度 → 路徑長度 → 字母。
    """
    if limit <= 0 or not query:
        return []
    folded = query.casefold()
    hidden_files = query.startswith(".")
    try:
        root_fd = _open_dir_chain(_root_parts(root), readable=True)
    except OSError:
        return []
    found: list[tuple[tuple[int, int, int, str, str], str]] = []
    scanned = 0
    queue: deque[tuple[str, ...]] = deque([()])
    try:
        while queue and scanned < MAX_SEARCH_SCAN:
            parts = queue.popleft()
            try:
                fd = _open_below(root_fd, parts) if parts else root_fd
            except OSError:
                continue
            try:
                with os.scandir(fd) as iterator:
                    for entry in iterator:
                        if scanned >= MAX_SEARCH_SCAN:
                            break
                        scanned += 1
                        name = entry.name
                        if _UNLISTABLE.search(name):
                            continue
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if not name.startswith(".") and len(parts) < MAX_SEARCH_DEPTH:
                                    queue.append((*parts, name))
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        if name.startswith(".") and not hidden_files:
                            continue
                        lowered = name.casefold()
                        if folded not in lowered:
                            continue
                        relative = "/".join((*parts, name))
                        if relative in skip:
                            continue
                        rank = (0 if lowered.startswith(folded) else 1, len(parts),
                                len(relative), relative.casefold(), relative)
                        found.append((rank, relative))
            except OSError:
                pass
            finally:
                if fd != root_fd:
                    os.close(fd)
    finally:
        os.close(root_fd)
    found.sort()
    return [relative for _rank, relative in found[:limit]]


def _external_completions(
    at: int, cursor: int, prefix: str, quoted: bool, scope: ExternalScope, limit: int,
) -> Completion | None:
    """``~``／``/`` 開頭的 token(外部匯入已開啟):還沒進入來源根 → 列出相符的根本身;
    已在根內 → 列根內的目錄與可匯入的檔,新到舊。其他情況 → None。"""
    if _CONTROL.search(prefix) or ".." in prefix.split("/"):
        return None
    tilde = prefix.startswith("~")
    if tilde:
        if scope.home is None or (prefix != "~" and not prefix.startswith("~/")):
            return None
        expanded = _home_join(scope.home, prefix[1:])
    else:
        expanded = prefix
    roots = _scope_roots(scope)
    for source_root in roots:
        head = source_root.rstrip("/") + "/"
        if expanded.startswith(head):
            return _list_external(
                at, cursor, prefix, quoted, scope, source_root, expanded[len(head):], limit,
            )
    items: list[tuple[str, str]] = []
    for source_root in roots:
        spelled = _tilde_form(source_root, scope.home) if tilde else source_root
        if spelled is None or not spelled.startswith(prefix):
            continue
        items.append((_inserted(spelled.rstrip("/") + "/", is_dir=True, quoted=quoted), "外部匯入來源"))
    items = items[:max(0, limit)]
    return Completion(at, cursor, tuple(items)) if items else None


def _list_external(
    at: int, cursor: int, prefix: str, quoted: bool, scope: ExternalScope,
    source_root: str, relative: str, limit: int,
) -> Completion | None:
    directory, _, name_part = relative.rpartition("/")
    parts = [part for part in directory.split("/") if part not in ("", ".")]
    # 插入文字保留使用者打的根與目錄寫法(`~/Downloads/…`),只換掉最後的名稱部分。
    base = prefix[:len(prefix) - len(name_part)]
    resolved = _resolved_root(source_root)
    if resolved is None:
        return None
    entries: list[tuple[float, bool, str]] = []
    try:
        fd = _open_dir_chain(_root_parts(resolved) + parts, readable=True)
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
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            is_dir = True
                        elif entry.is_file(follow_symlinks=False):
                            suffix = PurePosixPath(name).suffix.lower()
                            if scope.extensions and suffix not in scope.extensions:
                                continue
                            is_dir = False
                        else:
                            continue
                        # DirEntry 的 stat 以 scandir 的 dir fd 做 fstatat:fd 關掉之前取。
                        modified = entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        continue
                    entries.append((modified, is_dir, name))
        finally:
            os.close(fd)
    except OSError:
        return None
    entries.sort(key=lambda item: (-item[0], item[2].lower(), item[2]))
    now = _now()
    items: list[tuple[str, str]] = []
    for modified, is_dir, name in entries[:max(0, limit)]:
        path = base + name + ("/" if is_dir else "")
        if is_dir:
            detail = "目錄"
        else:
            tool, kind = route(name)
            detail = f"匯入後 {tool} · {_KIND_LABELS.get(kind, kind)} · {_age(now - modified)}"
        items.append((_inserted(path, is_dir=is_dir, quoted=quoted), detail))
    return Completion(at, cursor, tuple(items)) if items else None


def completions(
    text: str,
    cursor: int,
    root: Path | str | None,
    *,
    limit: int = MAX_COMPLETIONS,
    external: ExternalScope | None = None,
) -> Completion | None:
    """游標位於 @ token 尾端時列出候選。其他情況、任何 OSError → None。零寫入。

    專案內:游標那一層目錄裡前綴符合的項目;名稱部分 ≥ MIN_SEARCH_CHARS 且 token 沒有
    ``/`` 時,再追加整個專案裡檔名含它的普通檔(:func:`_search`)。``~``／``/`` 開頭只在
    外部匯入開啟(``external.enabled``)時列出來源根與根內可匯入的檔,否則 None(零 FS)。
    """
    if root is None or not isinstance(text, str) or not 0 <= cursor <= len(text):
        return None
    if "@" not in text[:cursor] or not _capable():
        return None
    token = _token_at(text, cursor)
    if token is None:
        return None
    at, prefix, quoted = token
    if prefix.startswith(("/", "~")):
        if external is None or not external.enabled:
            return None
        return _external_completions(at, cursor, prefix, quoted, external, limit)
    if _CONTROL.search(prefix) or ".." in prefix.split("/"):
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
    entries.sort(key=lambda item: (not item[0], item[1].lower(), item[1]))
    items: list[tuple[str, str]] = []
    base = directory + "/" if directory else ""
    for is_dir, name in entries[:max(0, limit)]:
        relative = base + name + ("/" if is_dir else "")
        items.append((
            _inserted(relative, is_dir=is_dir, quoted=quoted),
            "目錄" if is_dir else _file_detail(relative),
        ))
    if not directory and len(name_part) >= MIN_SEARCH_CHARS and len(items) < limit:
        listed = {name for is_dir, name in entries if not is_dir}
        for relative in _search(root_text, name_part, skip=listed, limit=limit - len(items)):
            items.append((_inserted(relative, is_dir=False, quoted=quoted), _file_detail(relative)))
    return Completion(at, cursor, tuple(items)) if items else None
