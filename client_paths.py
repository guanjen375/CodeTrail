#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_paths — owner-only 檔案的共用防線(dir-fd + ``O_NOFOLLOW`` + ``fstat``)。

客戶端有三份「被別人改掉就等於繞過安全決策」的檔案:

* ``client.json`` —— 決定寫入工具要不要人工核准
* session JSONL —— 逐字含 NDA 內容
* ``compaction-stopped.jsonl`` —— 跨行程的停用 ledger

它們的防線必須是同一套,所以放在這裡而不是各寫一份:path-based 的
``is_symlink()`` 檢查再 ``read_text()`` / ``open()`` 是 check-then-use,
兩次 lookup 之間換掉那個名字就穿過去了。這裡一律先用 dir fd 錨住父目錄,
再以 ``O_NOFOLLOW`` 開檔、``fstat`` 驗普通檔、owner 與 ``st_nlink == 1``。
"""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Callable

from runtime_dependencies import require_safe_filesystem

DIR_MODE = 0o700
FILE_MODE = 0o600

ErrorFactory = Callable[[str], Exception]


def _raise(make: ErrorFactory, message: str, cause: BaseException | None = None):
    error = make(message)
    if cause is not None:
        raise error from cause
    raise error


def open_private_dir(
    path: Path,
    make_error: ErrorFactory,
    *,
    create: bool = True,
    anchor: Path | None = None,
    guard: Callable[[Path], None] | None = None,
) -> int:
    """建立/開啟一個 owner-only 目錄,並回傳錨住它的 dir fd。

    ``anchor`` 以上是使用者的環境(``$HOME``、``$XDG_STATE_HOME``、被分析的 repo
    root):那一段**可以**含 symlink(``/home`` 指到 ``/usr/home`` 是正常的),
    但一律先 ``realpath`` 解析——呼叫端要拿 :func:`resolved_directory` 的結果做
    containment 判斷,不能拿字面路徑(``XDG_STATE_HOME=/tmp/link/state`` 而
    ``/tmp/link`` 指進 repo,字面上看不出來)。

    ``anchor`` 以下是**我們自己的**目錄(``codetrail/sessions/<hash>``):逐層用
    dir fd + ``O_NOFOLLOW`` 開、缺的用 ``mkdir(dir_fd=...)`` 建、每一層都收成
    0700。任何一層是 symlink 就拒絕——只驗最終目錄擋不住「把中間那層換成
    symlink」。

    ``guard`` 在每一次開啟時拿到解析後的實際位置(anchor 以上 realpath):呼叫端用它做
    containment 判斷(「不得落進被分析的 repo」),而不是只在建構時判一次。

    ``create=False`` 給純讀取用:讀設定不該把 ``~/.config/codetrail`` 生出來
    ——「沒有設定檔 = 沒有接管」的 fail-closed 語意要連目錄都不留痕跡。
    目錄不存在時回 ``-1``,呼叫端當成「檔案不存在」。
    """
    require_safe_filesystem("owner-only files", owner_only=True, error_type=make_error)
    target = Path(path).expanduser()
    base = Path(anchor).expanduser() if anchor is not None else target.parent
    if not target.is_absolute() or not base.is_absolute():
        _raise(make_error, f"private directory must be absolute: {target}")
    try:
        parts = target.relative_to(base).parts
    except ValueError:
        _raise(make_error, f"{target} is not under its anchor {base}")
    real_base = Path(os.path.realpath(base))
    if guard is not None:
        # containment 是**每一次操作**都判,不是建構時判一次:anchor 以上的
        # symlink 可以在兩次操作之間被改指進 repo。
        guard(real_base / Path(*parts) if parts else real_base)
    if not real_base.is_dir() and not create:
        return -1
    # realpath → guard → open 之間 anchor 的某一層可以被改指(check-then-use)。所以
    # 不用 path-based 的 os.open(real_base):realpath 之後這條路徑上**不該**再有任何
    # symlink,於是從 `/` 逐層以 O_NOFOLLOW 的 dir-fd 走下去——哪一層在空窗期被換成
    # symlink,那一層就 ELOOP,fail-closed。這條防線不需要 procfs,任何 Unix 都成立。
    # anchor 還不存在時,缺的那幾層也在同一趟 dir-fd 走訪裡 mkdir(不用 path-based 的
    # mkdir(parents=True):那會在被改指的祖先底下先留一個空目錄)。
    fd = _open_resolved_directory(real_base, make_error, create=create)
    try:
        if guard is not None:
            # 多一道:Linux 上再問 kernel 這個 fd 現在是哪裡(/proc/self/fd),對真正開到
            # 的位置再判一次 containment;拿不到位置時上面的逐層 O_NOFOLLOW 已經保證
            # 沒有跟過任何 symlink,不是 fail-open。
            actual = _opened_directory(fd)
            if actual is not None:
                guard(actual / Path(*parts) if parts else actual)
        for name in parts:
            # _descend 一定會關掉傳進去的父 fd(成功或失敗都關),所以交出去之前
            # 先把自己手上的清掉,except 那裡才不會關第二次(EBADF 會蓋掉真正的
            # 錯誤訊息)。
            parent, fd = fd, -1
            fd = _descend(parent, name, make_error, create=create)
            if fd < 0:
                return -1
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            _raise(make_error, f"not a directory: {target}")
        if info.st_uid != os.getuid():
            _raise(make_error, f"directory is not owned by this user: {target}")
        if create:
            os.fchmod(fd, DIR_MODE)
        return fd
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise


def _open_resolved_directory(resolved: Path, make_error: ErrorFactory, *, create: bool = False) -> int:
    """從 `/` 逐層以 ``O_NOFOLLOW`` 開到 ``resolved``(一條已經 realpath 過的路徑)。

    realpath 之後路徑上不該有 symlink;有的話就是解析之後被人換掉了——拒絕,不跟。
    ``create=True`` 時缺的那幾層用 ``mkdir(dir_fd=)`` 在同一趟走訪裡建(0700),
    然後同樣以 ``O_NOFOLLOW`` 開——建好之後被換成 symlink 也一樣被擋。
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(resolved.anchor or "/", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        _raise(make_error, f"cannot open directory: {resolved.anchor}", exc)
    walked = Path(resolved.anchor or "/")
    for name in resolved.parts[1:] if resolved.anchor else resolved.parts:
        walked = walked / name
        try:
            child = _open_child(fd, name, flags, create=create)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                _raise(
                    make_error,
                    f"{walked} changed underneath us (symlink after resolution): refusing",
                    exc,
                )
            _raise(make_error, f"cannot open directory: {walked}", exc)
        os.close(fd)
        fd = child
    return fd


def _open_child(parent_fd: int, name: str, flags: int, *, create: bool) -> int:
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
    try:
        os.mkdir(name, DIR_MODE, dir_fd=parent_fd)
    except FileExistsError:
        pass                        # 別人剛建好(或剛換成 symlink):下面的 O_NOFOLLOW 會判
    return os.open(name, flags, dir_fd=parent_fd)


def _opened_directory(fd: int) -> Path | None:
    """回這個 dir-fd **實際**指到的目錄(Linux 的 /proc/self/fd);問不到就回 None。"""
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return None


def _descend(parent_fd: int, name: str, make_error: ErrorFactory, *, create: bool) -> int:
    """從 ``parent_fd`` 走進一層,不跟 symlink;回子目錄的 fd(父 fd 一律關掉)。"""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            return os.open(name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            if not create:
                return -1
            try:
                os.mkdir(name, DIR_MODE, dir_fd=parent_fd)
            except FileExistsError:
                pass
            except OSError as exc:
                _raise(make_error, f"cannot create directory component: {name}", exc)
            try:
                return os.open(name, flags, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EMLINK}:
                    _raise(make_error, f"refusing symlink directory component: {name}", exc)
                _raise(make_error, f"cannot open directory component: {name}", exc)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EMLINK}:
                _raise(make_error, f"refusing symlink directory component: {name}", exc)
            _raise(make_error, f"cannot open directory component: {name}", exc)
    finally:
        os.close(parent_fd)


def resolved_directory(path: Path, *, anchor: Path) -> Path:
    """``open_private_dir`` 實際會落在哪裡:anchor 以上 realpath,以下字面接上。

    containment 判斷(「不得落進被分析的 repo」)要用這個,不能用字面路徑。
    """
    target = Path(path).expanduser()
    base = Path(anchor).expanduser()
    return Path(os.path.realpath(base)) / target.relative_to(base)


def _check_regular(fd: int, name: str, make_error: ErrorFactory) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        _raise(make_error, f"not a regular file: {name}")
    if info.st_uid != os.getuid():
        _raise(make_error, f"not owned by this user: {name}")
    if info.st_nlink != 1:
        # hard link:另一個名字指向同一個 inode,權限與位置的判斷全部落空。
        _raise(make_error, f"refusing hard-linked file: {name}")
    return info


def write_all(fd: int, payload: bytes, name: str, make_error: ErrorFactory) -> None:
    """把整份 payload 寫完。``os.write`` 可以 short write。"""
    view = memoryview(payload)
    written = 0
    while written < len(view):
        try:
            count = os.write(fd, view[written:])
        except OSError as exc:
            _raise(make_error, f"cannot write {name}: {exc}", exc)
        if count <= 0:
            _raise(make_error, f"short write on {name}")
        written += count


def read_private_file(
    directory: Path,
    name: str,
    make_error: ErrorFactory,
    *,
    max_bytes: int = 1024 * 1024,
    missing_ok: bool = True,
    anchor: Path | None = None,
) -> bytes | None:
    """讀一份 owner-only 檔案。不存在回 ``None``(``missing_ok``)。"""
    dir_fd = open_private_dir(directory, make_error, create=False, anchor=anchor)
    if dir_fd < 0:
        if missing_ok:
            return None
        _raise(make_error, f"missing file: {Path(directory) / name}")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, dir_fd=dir_fd)
        except FileNotFoundError:
            if missing_ok:
                return None
            _raise(make_error, f"missing file: {directory / name}")
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                _raise(make_error, f"refusing symlink: {directory / name}", exc)
            _raise(make_error, f"cannot open {directory / name}: {exc}", exc)
        try:
            info = _check_regular(fd, name, make_error)
            if info.st_mode & 0o077:
                _raise(
                    make_error,
                    f"{directory / name} is {oct(info.st_mode & 0o777)}; must be 0600",
                )
            if info.st_size > max_bytes:
                _raise(make_error, f"{directory / name} exceeds {max_bytes} bytes")
            chunks: list[bytes] = []
            total = 0
            while True:
                block = os.read(fd, 65536)
                if not block:
                    break
                total += len(block)
                if total > max_bytes:
                    _raise(make_error, f"{directory / name} exceeds {max_bytes} bytes")
                chunks.append(block)
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def replace_private_file(
    directory: Path,
    name: str,
    payload: bytes,
    make_error: ErrorFactory,
    *,
    anchor: Path | None = None,
) -> Path:
    """原子替換一份 owner-only 檔案,全程錨在 dir fd 上。"""
    dir_fd = open_private_dir(directory, make_error, anchor=anchor)
    tmp_name = f".{name}.tmp.{os.getpid()}"
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(tmp_name, flags, FILE_MODE, dir_fd=dir_fd)
        except OSError as exc:
            _raise(make_error, f"cannot create {directory / tmp_name}: {exc}", exc)
        try:
            os.fchmod(fd, FILE_MODE)
            write_all(fd, payload, tmp_name, make_error)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except OSError as exc:
            with _suppress_oserror():
                os.unlink(tmp_name, dir_fd=dir_fd)
            _raise(make_error, f"cannot replace {directory / name}: {exc}", exc)
    finally:
        os.close(dir_fd)
    return Path(directory) / name


def append_private_line(
    directory: Path,
    name: str,
    payload: bytes,
    make_error: ErrorFactory,
    *,
    anchor: Path | None = None,
    guard: Callable[[Path], None] | None = None,
) -> None:
    """對一份 owner-only 檔案 append 一行,建檔與 append 都不跟 symlink。

    ``guard`` 同 :func:`open_private_dir`:每一次 append 都拿解析後的實際位置再判一次
    containment(祖先 symlink 可以在兩次 append 之間被改指)。
    """
    dir_fd = open_private_dir(directory, make_error, anchor=anchor, guard=guard)
    try:
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, dir_fd=dir_fd)
        except FileNotFoundError:
            create = flags | os.O_CREAT | os.O_EXCL
            try:
                fd = os.open(name, create, FILE_MODE, dir_fd=dir_fd)
            except FileExistsError:  # 競態:別人剛建好,重開一次
                fd = os.open(name, flags, dir_fd=dir_fd)
            except OSError as exc:
                _raise(make_error, f"cannot create {directory / name}: {exc}", exc)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                _raise(make_error, f"refusing symlink: {directory / name}", exc)
            _raise(make_error, f"cannot open {directory / name}: {exc}", exc)
        try:
            _check_regular(fd, name, make_error)
            os.fchmod(fd, FILE_MODE)
            write_all(fd, payload, name, make_error)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


class _suppress_oserror:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, OSError)
