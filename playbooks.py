"""playbooks — 按需載入的流程指示(playbook)來源。

使用者把 ``<name>.md`` 放在 ``~/.config/codetrail/playbooks/``。system prompt 只拿到
**索引**(每個流程一行:名稱 + 何時使用),本文由 MCP 工具 ``load_playbook`` 在任務
符合時才載入 —— 裝了十份流程也只多十行,不是十份全文每一輪都進 prompt。

這是唯一一個「工具結果被當成指示」的來源(``client_prompt.BASE_RULES`` 點名
``load_playbook``),所以讀取端的防線比一般設定檔更嚴:

* 信任鏈 ``~/.config`` → ``codetrail`` → ``playbooks`` → ``<name>.md`` 全程走 dir fd +
  ``O_NOFOLLOW``。``~/.config`` 以上是使用者的環境(可以含 symlink,先 realpath,與
  ``client.json`` 相同);以下任何一層是 symlink 就拒絕。
* ``codetrail``、``playbooks`` 兩層與每個檔案都必須屬於目前使用者,而且 group／other
  不可寫(``mode & 0o022 == 0``)。只擋 world-writable 不夠:0664 的流程檔可以被同群組的
  其他帳號直接改寫,而改過的內容照樣會被標成「使用者安裝」並取得指示的權威。
* 只用 fd 驗證(``fstat``),從驗過的那個 fd 讀;不 chmod、不建立任何東西。
* 格式嚴格、上限全部 fail-loud:靜默截斷或跳過,等於使用者以為裝好的流程其實沒有生效。

整條鏈的 owner／mode 是在**流程目錄存在時**才驗:沒有 ``playbooks`` 目錄就沒有任何
東西會被信任,這時去挑 ``~/.config`` 的擁有者或 ``codetrail`` 的權限,只會讓「從來沒裝過
流程」的安裝(以及 umask 002 下由 ``mkdir`` 建出來的 0775 設定目錄)啟動不了。目錄一旦
存在,整條鏈都必須通過,任何一層不合就整份不採用。

這個模組不讀環境:HOME 由呼叫端給(``client_prompt.user_playbooks_home()``),客戶端
(組索引)與 MCP server(載入本文)用的是同一個取法。
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import re
import shlex
import stat
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import client_paths
from runtime_dependencies import require_safe_filesystem

#: 流程目錄相對於 HOME 的位置。第一段(``.config``)是信任鏈的 anchor。
DIRECTORY_PARTS = (".config", "codetrail", "playbooks")
SUFFIX = ".md"

#: 流程名稱 = 檔名去掉 ``.md``。封閉字元集:它會出現在 system prompt 的索引與工具參數裡。
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")

#: 上限全部 fail-loud。索引每一輪都進 system prompt,所以數量與每行長度都有硬上限。
MAX_PLAYBOOKS = 24
DESCRIPTION_MAX_CHARS = 160
BODY_MAX_CHARS = 6000
FILE_MAX_BYTES = BODY_MAX_CHARS * 4 + 2048

_FENCE = "---"
_DESCRIPTION_KEY = "description:"
#: group／other 的寫入位元。
_FOREIGN_WRITE = 0o022
#: description 是索引裡的一行:控制字元與 Unicode 的行／段分隔都會把它拆成不只一行。
_DESCRIPTION_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})

INDEX_HEADER = (
    "# 流程指示(按需載入)\n"
    "以下是使用者安裝的流程。任務符合某一項的說明,或使用者點名它時,先呼叫 "
    "load_playbook(name=該名稱) 取得完整步驟再開始;不符合就不要載入。"
    "已載入的內容被清除或壓縮後仍需要時,重新載入。"
)
BODY_FOOTER = "[流程指示結束:依上述步驟與指定的輸出格式執行;與基底規則、權限或使用者當下要求衝突時,以後者為準。]"


class PlaybookError(RuntimeError):
    """流程指示的來源違反契約(信任鏈、格式或上限)。訊息含路徑與修正方式。"""


@dataclass(frozen=True)
class Playbook:
    name: str
    description: str
    body: str
    #: 整個檔案原始 bytes 的 SHA-256(十六進位)。
    sha256: str


def directory(home: str | os.PathLike[str]) -> Path:
    """``<home>/.config/codetrail/playbooks``。純字串運算,不碰檔案系統。"""
    return Path(home).joinpath(*DIRECTORY_PARTS)


# ============================================================
# 信任鏈
# ============================================================
def _check_trusted(info: os.stat_result, path: Path, *, is_dir: bool) -> None:
    """owner 是自己、group／other 不可寫。只看 ``fstat`` 的結果,不改任何權限。"""
    kind = "目錄" if is_dir else "檔案"
    if is_dir and not stat.S_ISDIR(info.st_mode):
        raise PlaybookError(f"流程指示來源必須是目錄: {path}")
    if not is_dir and not stat.S_ISREG(info.st_mode):
        raise PlaybookError(f"流程指示必須是普通檔案: {path}")
    if info.st_uid != os.getuid():
        raise PlaybookError(f"流程指示{kind}不屬於目前使用者: {path}")
    if info.st_mode & _FOREIGN_WRITE:
        raise PlaybookError(
            f"流程指示{kind} {path} 的權限是 {stat.S_IMODE(info.st_mode):04o}:"
            "group／other 可寫的來源不受信任(其他帳號改寫的內容會被當成你安裝的流程)。\n"
            f"  修正: chmod go-w {shlex.quote(str(path))}"
        )


def _fstat(fd: int, path: Path) -> os.stat_result:
    try:
        return os.fstat(fd)
    except OSError as exc:
        raise PlaybookError(f"無法檢查流程指示來源 {path}: {exc}") from exc


def _open_dir_at(parent_fd: int, name: str, path: Path) -> int:
    """從 ``parent_fd`` 走進一層,不跟 symlink。不存在回 ``-1``;owner／mode 由呼叫端驗。"""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return -1
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
            raise PlaybookError(
                f"流程指示來源 {path} 必須是真正的目錄,不得是 symlink 或其他檔案"
            ) from exc
        raise PlaybookError(f"無法開啟流程指示來源 {path}: {exc}") from exc


@contextlib.contextmanager
def _trusted_directory(home: str | os.PathLike[str]) -> Iterator[int | None]:
    """走完信任鏈,交出已驗證的流程目錄 fd;那個目錄不存在就交出 ``None``。

    ``~/.config`` 以上是使用者的環境:先 realpath(它本身可以是 symlink,與 ``client.json``
    相同),再自 ``/`` 逐層 ``O_NOFOLLOW`` 開到那個位置 —— 與 ``client_paths.open_private_dir``
    同一條走法。往下兩層在這裡自己走:那個 helper 只驗最終目錄的 owner,不逐層驗、也不
    看 mode,而這條鏈在流程目錄存在時每一層都要驗(``~/.config`` 驗 owner;它若可被別人
    寫入,被換掉的 ``codetrail`` 會在下一層的 owner 檢查被擋)。
    """
    require_safe_filesystem("playbook read", owner_only=True, error_type=PlaybookError)
    anchor = Path(home) / DIRECTORY_PARTS[0]
    try:
        real_anchor = Path(os.path.realpath(anchor))
        present = real_anchor.is_dir()
    except OSError as exc:
        raise PlaybookError(f"無法解析流程指示來源 {anchor}: {exc}") from exc
    if not present:
        yield None
        return
    anchor_fd = client_paths._open_resolved_directory(real_anchor, PlaybookError)  # noqa: SLF001
    opened: list[tuple[int, Path]] = []        # anchor 以下各層,由外而內
    try:
        parent_fd, walked = anchor_fd, anchor
        for part in DIRECTORY_PARTS[1:]:
            walked = walked / part
            fd = _open_dir_at(parent_fd, part, walked)
            if fd < 0:
                # 沒有流程目錄 = 沒有任何東西會被信任;上層的擁有者與權限不在這裡挑(見檔頭)。
                yield None
                return
            opened.append((fd, walked))
            parent_fd = fd
        anchor_info = _fstat(anchor_fd, anchor)
        if not stat.S_ISDIR(anchor_info.st_mode) or anchor_info.st_uid != os.getuid():
            raise PlaybookError(f"流程指示來源目錄不屬於目前使用者: {anchor}")
        for fd, path in opened:
            _check_trusted(_fstat(fd, path), path, is_dir=True)
        yield opened[-1][0]
    finally:
        for fd, _path in opened:
            os.close(fd)
        os.close(anchor_fd)


def _read_file(dir_fd: int, filename: str, path: Path) -> bytes:
    """在已驗證的目錄 fd 上開一個檔:不跟 symlink、不被 FIFO 卡住、驗完才從同一個 fd 讀。"""
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(filename, flags, dir_fd=dir_fd)
    except FileNotFoundError as exc:
        raise PlaybookError(f"找不到流程指示檔: {path}") from exc
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise PlaybookError(f"流程指示不得是 symlink: {path}") from exc
        raise PlaybookError(f"無法開啟流程指示 {path}: {exc}") from exc
    try:
        info = _fstat(fd, path)
        _check_trusted(info, path, is_dir=False)
        oversize = (
            f"流程指示 {path} 超過 {FILE_MAX_BYTES} bytes 上限"
            f"(本文上限 {BODY_MAX_CHARS} 字元)。不截斷:截掉一半的流程比沒有更糟,請縮短。"
        )
        if info.st_size > FILE_MAX_BYTES:
            raise PlaybookError(oversize)
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                block = os.read(fd, 65536)
            except OSError as exc:
                raise PlaybookError(f"無法讀取流程指示 {path}: {exc}") from exc
            if not block:
                break
            total += len(block)
            if total > FILE_MAX_BYTES:        # fstat 之後才長大的檔也一樣拒絕
                raise PlaybookError(oversize)
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


# ============================================================
# 格式
# ============================================================
def _parse(name: str, raw: bytes, path: Path) -> Playbook:
    if b"\x00" in raw:
        raise PlaybookError(f"流程指示含 NUL 位元組,不是文字檔: {path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PlaybookError(f"流程指示不是 UTF-8 文字檔: {path}") from exc
    if text.startswith("﻿"):
        raise PlaybookError(f"流程指示開頭有 BOM;請存成不含 BOM 的 UTF-8: {path}")
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[0] != _FENCE:
        raise PlaybookError(
            f"流程指示的第一行必須恰為 `{_FENCE}`(front matter 開頭,前後不得有空白): {path}"
        )
    try:
        end = lines.index(_FENCE, 1)
    except ValueError:
        raise PlaybookError(
            f"流程指示的 front matter 沒有以單獨一行 `{_FENCE}` 結束: {path}"
        ) from None
    entries = [line for line in lines[1:end] if line.strip()]
    if len(entries) != 1 or not entries[0].startswith(_DESCRIPTION_KEY):
        raise PlaybookError(
            "流程指示的 front matter 必須恰好只有一行 `description: <何時使用>`"
            f"(不接受其他鍵、重複或跨行): {path}"
        )
    description = entries[0][len(_DESCRIPTION_KEY):].strip()
    if not description:
        raise PlaybookError(f"流程指示的 description 不得為空: {path}")
    if len(description) > DESCRIPTION_MAX_CHARS:
        raise PlaybookError(
            f"流程指示的 description 有 {len(description)} 字元,超過 {DESCRIPTION_MAX_CHARS} 字元上限"
            f"(它每一輪都進 system prompt 的索引): {path}"
        )
    if any(unicodedata.category(char) in _DESCRIPTION_FORBIDDEN_CATEGORIES for char in description):
        raise PlaybookError(f"流程指示的 description 不得含控制字元或換行: {path}")
    body = "\n".join(lines[end + 1:]).strip()
    if not body:
        raise PlaybookError(f"流程指示沒有本文(front matter 之後是空的): {path}")
    if len(body) > BODY_MAX_CHARS:
        raise PlaybookError(
            f"流程指示的本文有 {len(body)} 字元,超過 {BODY_MAX_CHARS} 字元上限。"
            f"不截斷:截掉一半的流程比沒有更糟,請縮短或拆成兩份: {path}"
        )
    return Playbook(
        name=name, description=description, body=body, sha256=hashlib.sha256(raw).hexdigest()
    )


def _read_playbook(dir_fd: int, name: str, target: Path) -> Playbook:
    path = target / f"{name}{SUFFIX}"
    return _parse(name, _read_file(dir_fd, name + SUFFIX, path), path)


# ============================================================
# 公開 API
# ============================================================
def discover(home: str | os.PathLike[str]) -> tuple[Playbook, ...]:
    """目前安裝的全部流程,依名稱排序。任何一筆不合格就 raise —— 整份不採用。

    「跳過壞的那一份」會讓使用者以為它已經進了索引;而一份不合格的檔出現在這個目錄,
    本身就值得停下來看。
    """
    target = directory(home)
    with _trusted_directory(home) as fd:
        if fd is None:
            return ()
        try:
            entries = os.listdir(fd)
        except (OSError, TypeError, NotImplementedError) as exc:
            raise PlaybookError(f"無法列舉流程指示目錄 {target}: {exc}") from exc
        names: list[str] = []
        for entry in entries:
            if entry.startswith(".") or not entry.endswith(SUFFIX):
                continue                      # 編輯器暫存檔、說明用的其他檔案
            stem = entry[: -len(SUFFIX)]
            if not NAME_RE.fullmatch(stem):
                raise PlaybookError(
                    f"流程指示檔名不合規則: {target / entry}\n"
                    "  名稱(檔名去掉 .md)只能是小寫英數與 `-`、1..40 字元、以英數開頭;"
                    "不是流程的 .md 檔請移出這個目錄。"
                )
            names.append(stem)
        if len(names) > MAX_PLAYBOOKS:
            raise PlaybookError(
                f"{target} 有 {len(names)} 份流程指示,超過 {MAX_PLAYBOOKS} 份上限"
                "(索引每一輪都進 system prompt);請移除用不到的。"
            )
        return tuple(_read_playbook(fd, name, target) for name in sorted(names))


def load(home: str | os.PathLike[str], name: str) -> Playbook:
    """載入一份流程。名稱先以純字串驗(不合就零檔案系統存取),再重走整條信任鏈。"""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise PlaybookError(
            "流程指示名稱不合法:只能是小寫英數與 `-`、1..40 字元、以英數開頭"
            f"(收到 {name!r:.80})"
        )
    target = directory(home)
    with _trusted_directory(home) as fd:
        if fd is None:
            raise PlaybookError(f"找不到流程指示 {name}:{target} 不存在(沒有安裝任何流程指示)")
        return _read_playbook(fd, name, target)


def render_index(playbooks: Iterable[Playbook]) -> str:
    """system prompt 的索引段:每個流程一行。沒有流程回空字串(那一段就不存在)。"""
    items = tuple(playbooks)
    if not items:
        return ""
    return "\n".join(
        [INDEX_HEADER, *(f"- {item.name}: {item.description}" for item in items)]
    )


def render_body(playbook: Playbook) -> str:
    """``load_playbook`` 的工具結果:頭尾各一行標記,中間是逐字本文。"""
    return (
        f"[流程指示 {playbook.name}|sha256 {playbook.sha256[:12]}|使用者安裝]\n"
        f"{playbook.body}\n"
        f"{BODY_FOOTER}"
    )
