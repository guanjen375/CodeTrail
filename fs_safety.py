#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fs_safety — index artifact(cache/graph/lock/tmp)建立前的 symlink 防線 + 檔案鎖。

安全範本來自 context_generation 的 writer lock(O_NOFOLLOW + fstat S_ISREG):
把 lock / cache 路徑換成 symlink 指向任意可寫檔案時,open+truncate 會把那個
檔案洗掉。這裡的防線(§7.6):

  1. 父目錄 realpath 必須落在 root 內(防 `.code_rag_cache.lock` 的父層被換掉)
  2. lstat 既有路徑非 symlink
  3. 以 O_NOFOLLOW 開啟(TOCTOU 防線:就算 1-2 之後被換,open 也會失敗)
  4. os.fstat 驗證拿到的是 regular file(FIFO / device 同樣拒絕)

違反任何一條 → FsSafetyError(fail-loud),絕不靜默跟過去。

鎖語意:blocking exclusive(寫端互斥、願意等);與 context_generation 的
non-blocking writer lock(搶不到就報錯)是不同契約,所以分開實作。
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

if os.name == "nt":  # pragma: no cover - 本輪不在 Windows 上驗證
    import msvcrt
else:
    import fcntl


class FsSafetyError(RuntimeError):
    """symlink / 非常規檔案 / 路徑逃逸 root。一律 fail-loud。"""


def ensure_parent_within_root(path: Path, root: Path) -> None:
    """path 的父目錄 realpath 必須等於 root 或在 root 之下。"""
    parent_real = Path(path).parent.resolve()
    root_real = Path(root).resolve()
    if parent_real != root_real and root_real not in parent_real.parents:
        raise FsSafetyError(
            f"refusing to create {path}: parent {parent_real} escapes root {root_real}"
        )


def open_regular_file_nofollow(path: Path, *, mode: int = 0o600) -> int:
    """以 O_NOFOLLOW 開啟(必要時建立)path,驗證是 regular file,回傳 fd。"""
    path = Path(path)
    try:
        if path.is_symlink():
            raise FsSafetyError(f"refusing {path}: existing path is a symlink")
    except OSError as exc:
        raise FsSafetyError(f"cannot lstat {path}: {exc}") from exc

    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, mode)
    except OSError as exc:
        # O_NOFOLLOW 對 symlink 會 ELOOP:訊息講明,不留「打不開」謎題
        raise FsSafetyError(
            f"cannot open {path} safely (symlink or wrong type?): {exc}"
        ) from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise FsSafetyError(f"refusing {path}: not a regular file")
    return fd


def _lock_fd(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover
        # msvcrt LK_LOCK: blocking(內部每秒重試,約 10 秒後 OSError)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


def acquire_file_lock(path: Path, root: Path) -> int:
    """symlink 防線 + blocking exclusive lock。回傳持鎖 fd(用 release_file_lock 釋放)。"""
    path = Path(path)
    ensure_parent_within_root(path, root)
    fd = open_regular_file_nofollow(path)
    try:
        _lock_fd(fd)
    except OSError:
        os.close(fd)
        raise
    return fd


def try_acquire_file_lock(path: Path, root: Path) -> int | None:
    """非阻塞版:拿不到鎖回 None(fd 已關),拿到回持鎖 fd。"""
    path = Path(path)
    ensure_parent_within_root(path, root)
    fd = open_regular_file_nofollow(path)
    try:
        if os.name == "nt":  # pragma: no cover
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def release_file_lock(fd: int) -> None:
    try:
        _unlock_fd(fd)
    finally:
        os.close(fd)
