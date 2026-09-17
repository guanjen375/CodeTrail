"""Validate command authorizations and inspect trusted tool directories safely."""
from __future__ import annotations

import os
import re
import stat
import struct
from dataclasses import dataclass

import config
from runtime_policy import EXTRA_BUILD_COMMANDS


MAX_EXTRA_COMMANDS = 128
MAX_COMMAND_NAME_CHARS = 128
MAX_EXTRA_COMMAND_DIRS = 32
MAX_COMMAND_DIRECTORY_CHARS = 4096
MAX_DIRECTORY_ENTRIES = 4096
MAX_EXECUTABLE_HEADER_BYTES = 64 * 1024
_EXECUTABLE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*")
_PATH_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
_RESERVED_EXECUTABLES = frozenset({
    "rm", "sudo", "curl", "bash", "sh", "dash", "zsh", "fish", "ksh", "csh",
    "tcsh", "env", "xargs", "exec", "command", "eval", "source", "busybox",
    "python3", "node", "perl", "ruby",
})
# Preserve the native functions for capability checks even when instrumented.
_DIRFD_FUNCTIONS = (os.open, os.stat)
_STAT_FUNCTION = os.stat
_SCANDIR_FUNCTION = os.scandir


def _name_rejection(name: str, build_roots: set[str], builtin_roots: set[str]) -> str:
    if len(name) > MAX_COMMAND_NAME_CHARS:
        return f"名稱至多 {MAX_COMMAND_NAME_CHARS} 字元"
    if not _EXECUTABLE_NAME.fullmatch(name):
        return (
            "必須是裸 executable 名稱 ([A-Za-z0-9_][A-Za-z0-9_.+-]*),"
            "不可含路徑、空白或 shell 字元"
        )
    if name == "git":
        return "是保留命令;請使用專用 Git 工具"
    if name in _RESERVED_EXECUTABLES:
        return "是禁止擴大的保留命令或通用執行器"
    if name in build_roots:
        return "是 build 命令;請使用 build_commands 設定"
    if name in builtin_roots:
        return "已由內建白名單管理,不可擴大其參數範圍"
    return ""


def validate_extra_allowed_commands(value: object) -> list[str]:
    """Return a fresh validated list; reject paths, shell syntax and reserved roots.

    Names need not be installed yet. This only validates authorization; execution
    still resolves the exact executable through PATH with the existing safeguards.
    """
    if not isinstance(value, list):
        raise ValueError("extra_allowed_commands 必須是 executable 名稱的字串陣列")
    if len(value) > MAX_EXTRA_COMMANDS:
        raise ValueError(f"extra_allowed_commands 至多 {MAX_EXTRA_COMMANDS} 項")

    build_roots = {command.split()[0] for command in EXTRA_BUILD_COMMANDS}
    builtin_roots = {command.split()[0] for command in config.ALLOWED_COMMANDS}
    result: list[str] = []
    seen: set[str] = set()
    for index, name in enumerate(value):
        label = f"extra_allowed_commands[{index}]={name!r}"
        if not isinstance(name, str):
            raise ValueError(f"{label} 必須是字串")
        reason = _name_rejection(name, build_roots, builtin_roots)
        if reason:
            raise ValueError(f"{label} {reason}")
        if name in seen:
            raise ValueError(f"{label} 重複;每個 executable 名稱只能列一次")
        seen.add(name)
        result.append(name)
    return result


def validate_extra_allowed_command_dirs(value: object) -> list[str]:
    """Normalize absolute paths lexically, without touching the filesystem."""
    if not isinstance(value, list):
        raise ValueError("extra_allowed_command_dirs 必須是絕對目錄路徑的字串陣列")
    if len(value) > MAX_EXTRA_COMMAND_DIRS:
        raise ValueError(f"extra_allowed_command_dirs 至多 {MAX_EXTRA_COMMAND_DIRS} 項")
    result: list[str] = []
    seen: set[str] = set()
    for index, path in enumerate(value):
        label = f"extra_allowed_command_dirs[{index}]={path!r}"
        if not isinstance(path, str) or not path or not path.startswith("/"):
            raise ValueError(f"{label} 必須是非空絕對路徑")
        if len(path) > MAX_COMMAND_DIRECTORY_CHARS:
            raise ValueError(f"{label} 至多 {MAX_COMMAND_DIRECTORY_CHARS} 字元")
        if _PATH_CONTROL.search(path):
            raise ValueError(f"{label} 不可含 NUL、控制字元或無效 Unicode")
        # CodeTrail executes on POSIX; a repeated leading slash names the same
        # root here. Canonicalize it too, so aliases cannot duplicate a grant.
        normalized = os.path.normpath("/" + path.lstrip("/"))
        if normalized in seen:
            raise ValueError(f"{label} 正規化後重複;每個目錄只能列一次")
        seen.add(normalized)
        result.append(normalized)
    return result


@dataclass(frozen=True)
class DirectoryInspection:
    commands: dict[str, str]
    excluded: dict[str, dict[str, int]]
    errors: dict[str, str]


class _DirectoryError(RuntimeError):
    pass


def _require_directory_io() -> None:
    missing = [name for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
               if not getattr(os, name, 0)]
    if any(fn not in getattr(os, "supports_dir_fd", ()) for fn in _DIRFD_FUNCTIONS):
        missing.append("dir_fd/openat")
    if _STAT_FUNCTION not in getattr(os, "supports_follow_symlinks", ()):
        missing.append("stat(follow_symlinks=False)")
    if _SCANDIR_FUNCTION not in getattr(os, "supports_fd", ()):
        missing.append("scandir(fd)")
    if not callable(getattr(os, "getuid", None)):
        missing.append("getuid")
    if missing:
        raise _DirectoryError("工具目錄安全檢查需要 " + ", ".join(missing))


def _directory_identity(info: os.stat_result) -> tuple[int, ...]:
    # Unrelated changes to /tmp must not invalidate this path; ownership,
    # permissions and the actual inode still have to remain the same.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (*_directory_identity(info), info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_nlink)


def _validate_directory(info: os.stat_result, path: str, uid: int, *, final: bool) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise _DirectoryError(f"{path} 不是目錄或是 symlink")
    if info.st_uid not in ({uid} if final else {0, uid}):
        raise _DirectoryError(f"{path} 的 owner 不符合工具目錄信任規則")
    if info.st_mode & stat.S_IWOTH:
        if final or info.st_uid != 0 or not info.st_mode & stat.S_ISVTX:
            raise _DirectoryError(f"{path} 是 world-writable;拒絕授權")


def _open_directory(path: str, uid: int) -> tuple[int, tuple[tuple[int, ...], ...]]:
    """Walk from / without following any link; caller owns the returned fd."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open("/", flags)
    chain: list[tuple[int, ...]] = []
    try:
        parts = [part for part in path.split("/") if part]
        info = os.fstat(fd)
        _validate_directory(info, "/", uid, final=not parts)
        chain.append(_directory_identity(info))
        walked = ""
        for index, part in enumerate(parts):
            walked += "/" + part
            named = os.stat(part, dir_fd=fd, follow_symlinks=False)
            _validate_directory(named, walked, uid, final=index == len(parts) - 1)
            child = os.open(part, flags, dir_fd=fd)
            try:
                opened = os.fstat(child)
                if _directory_identity(opened) != _directory_identity(named):
                    raise _DirectoryError(f"{walked} 在開啟時身分變動")
                _validate_directory(opened, walked, uid, final=index == len(parts) - 1)
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
            chain.append(_directory_identity(opened))
        return fd, tuple(chain)
    except BaseException:
        os.close(fd)
        raise


def _format_rejection(data: bytes, file_size: int) -> str:
    """Classify only the bounded header; never invoke a tool or interpreter."""
    if data.startswith(b"#!"):
        return ""
    if not data.startswith(b"\x7fELF"):
        return "非 ELF executable 或 shebang 腳本"
    invalid = "ELF 格式錯誤、截斷或超出 64 KiB 檔頭範圍"
    if len(data) < 16 or data[4] not in (1, 2) or data[5] not in (1, 2) or data[6] != 1:
        return invalid
    endian = "<" if data[5] == 1 else ">"
    is64 = data[4] == 2
    header_size, ph_size = (64, 56) if is64 else (52, 32)
    if len(data) < header_size:
        return invalid
    fields = struct.unpack_from(endian + ("HHIQQQIHHHHHH" if is64 else "HHIIIIIHHHHHH"), data, 16)
    e_type, _, version, _, phoff, _, _, ehsize, phentsize, phnum, *_ = fields
    if version != 1 or ehsize != header_size:
        return invalid
    if e_type not in (2, 3):
        return "ELF 不是 ET_EXEC 或帶 PT_INTERP 的 ET_DYN"
    if (not phnum or phnum == 0xffff or phoff < header_size or phentsize != ph_size
            or phoff + phnum * phentsize > len(data)):
        return invalid
    has_interp = False
    for index in range(phnum):
        ph = struct.unpack_from(endian + ("IIQQQQQQ" if is64 else "IIIIIIII"),
                                data, phoff + index * phentsize)
        if is64:
            p_type, _, offset, _, _, filesz, memsz, _ = ph
        else:
            p_type, offset, _, _, filesz, memsz, _, _ = ph
        if filesz and offset + filesz > file_size:
            return invalid
        if p_type == 1 and filesz > memsz:
            return invalid
        if p_type == 3:
            if filesz < 2:
                return invalid
            has_interp = True
    if e_type == 3 and not has_interp:
        return "ELF shared object 缺 PT_INTERP"
    return ""


def _inspect_directory(path: str, uid: int) -> tuple[dict[str, str], dict[str, int]]:
    fd, chain = _open_directory(path, uid)
    try:
        before = os.fstat(fd)
        names: list[str] = []
        with os.scandir(fd) as entries:
            for entry in entries:
                if len(names) >= MAX_DIRECTORY_ENTRIES:
                    raise _DirectoryError(f"工具目錄超過 {MAX_DIRECTORY_ENTRIES} 項;未完整檢查")
                names.append(entry.name)
        commands: dict[str, str] = {}
        excluded: dict[str, int] = {}
        identities: dict[str, tuple[int, ...]] = {}
        build_roots = {command.split()[0] for command in EXTRA_BUILD_COMMANDS}
        builtin_roots = {command.split()[0] for command in config.ALLOWED_COMMANDS}
        for name in sorted(names):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            reason = _name_rejection(name, build_roots, builtin_roots)
            if not reason:
                if stat.S_ISLNK(info.st_mode):
                    reason = "symlink"
                elif stat.S_ISDIR(info.st_mode):
                    reason = "子目錄"
                elif not stat.S_ISREG(info.st_mode):
                    reason = "非普通檔"
                elif info.st_uid != uid:
                    reason = "非目前使用者擁有"
                elif not info.st_mode & stat.S_IXUSR:
                    reason = "缺 owner execute 權限"
                elif info.st_mode & stat.S_IWOTH:
                    reason = "world-writable 檔案"
            if not reason:
                # O_NONBLOCK prevents a regular file swapped for a FIFO between
                # stat/open from hanging the synchronous MCP server.
                leaf = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    opened = os.fstat(leaf)
                    if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(info):
                        raise _DirectoryError(f"{name} 在開啟時身分變動")
                    remaining = min(opened.st_size, MAX_EXECUTABLE_HEADER_BYTES)
                    blocks: list[bytes] = []
                    while remaining:
                        block = os.read(leaf, remaining)
                        if not block:
                            raise _DirectoryError(f"{name} 檔頭讀取不完整")
                        blocks.append(block)
                        remaining -= len(block)
                    named = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if (_file_identity(os.fstat(leaf)) != _file_identity(opened)
                            or _file_identity(named) != _file_identity(opened)):
                        raise _DirectoryError(f"{name} 在讀取時身分或內容變動")
                    reason = _format_rejection(b"".join(blocks), opened.st_size)
                finally:
                    os.close(leaf)
            if reason:
                excluded[reason] = excluded.get(reason, 0) + 1
            else:
                commands[name] = os.path.join(path, name)
                identities[name] = _file_identity(info)
        for name, identity in identities.items():
            if _file_identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != identity:
                raise _DirectoryError(f"{name} 在目錄檢查期間變動")
        if _file_identity(os.fstat(fd)) != _file_identity(before):
            raise _DirectoryError("工具目錄在檢查期間變動;未完整檢查")
        # The result is an absolute pathname, so re-open the full chain before
        # publishing it. An anchored fd alone could now name a detached tree.
        fresh, fresh_chain = _open_directory(path, uid)
        try:
            if fresh_chain != chain or _file_identity(os.fstat(fresh)) != _file_identity(before):
                raise _DirectoryError("工具目錄路徑在檢查期間身分變動")
        finally:
            os.close(fresh)
        return commands, dict(sorted(excluded.items()))
    finally:
        os.close(fd)


def inspect_command_directories(
    directories: list[str], *, extra_commands: list[str] | None = None,
) -> DirectoryInspection:
    """Return this inspection only; any error invalidates the complete grant.

    Filesystem failures remain visible to list/add/run instead of preventing
    unrelated settings edits or startup. No subprocess, PATH lookup or write is
    performed, and no result is cached between calls.
    """
    paths = validate_extra_allowed_command_dirs(directories)
    extras = set(validate_extra_allowed_commands([] if extra_commands is None else extra_commands))
    commands: dict[str, str] = {}
    excluded: dict[str, dict[str, int]] = {}
    errors: dict[str, str] = {}
    owners: dict[str, str] = {}
    conflicted: set[str] = set()

    def error(path: str, message: str) -> None:
        errors[path] = errors[path] + "; " + message if path in errors else message

    for path in paths:
        excluded[path] = {}
        try:
            _require_directory_io()
            found, excluded[path] = _inspect_directory(path, os.getuid())
        except (OSError, UnicodeError, NotImplementedError, _DirectoryError) as exc:
            error(path, f"工具目錄不可用: {exc}")
            continue
        if not found:
            error(path, "工具目錄沒有合格的授權工具")
        for name, executable in found.items():
            if name in extras:
                error(path, f"工具 {name!r} 與 legacy extra_allowed_commands 名稱衝突")
                conflicted.add(name)
            if name in owners:
                previous = owners[name]
                message = f"工具 {name!r} 在 {previous} 與 {path} 名稱衝突"
                error(previous, message)
                error(path, message)
                conflicted.add(name)
            else:
                owners[name] = path
            if name not in conflicted:
                commands[name] = executable
            else:
                commands.pop(name, None)
    return DirectoryInspection(dict(sorted(commands.items())), excluded,
                               {path: errors[path] for path in paths if path in errors})
