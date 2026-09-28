"""Validate project-local executables for run_command safely.

run_command 的 argv[0] 含 "/" 時視為專案內工具：只接受 AICODE_ROOT 之內、自 `/` 逐層
以 dir-fd／O_NOFOLLOW 驗證過的可執行檔。不需要任何授權指令或設定；每次呼叫都重驗、
不快取。純字串驗證（..、~、控制字元、專案外路徑）一律在任何檔案系統存取之前完成。
"""
from __future__ import annotations

import os
import re
import stat
import struct

import config
from runtime_policy import EXTRA_BUILD_COMMANDS


MAX_COMMAND_NAME_CHARS = 128
MAX_COMMAND_PATH_CHARS = 4096
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


class ProjectCommandError(ValueError):
    """argv[0] 不是可執行的專案內工具；訊息可直接回給模型。"""


class _DirectoryError(RuntimeError):
    pass


def _name_rejection(name: str, build_roots: set[str], builtin_roots: set[str]) -> str:
    if len(name) > MAX_COMMAND_NAME_CHARS:
        return f"檔名至多 {MAX_COMMAND_NAME_CHARS} 字元"
    if not _EXECUTABLE_NAME.fullmatch(name):
        return "檔名必須符合 [A-Za-z0-9_][A-Za-z0-9_.+-]*，不可含空白或 shell 字元"
    if name == "git":
        return "與保留命令 git 同名；請使用 git_status／git_diff"
    if name in _RESERVED_EXECUTABLES:
        return "與保留命令或通用執行器同名，不可執行"
    if name in build_roots:
        return "與 build 命令同名；build 請在 client.json 開 build_commands 後以裸名呼叫"
    if name in builtin_roots:
        return "與內建白名單命令同名；請以裸名呼叫內建命令，不能用專案內同名檔擴大參數範圍"
    return ""


def project_executable_candidate(root: str | os.PathLike[str], argv0: str) -> str:
    """純字串驗證 argv[0]，回傳正規化後的絕對路徑；不做任何檔案系統存取。

    相對路徑以 root 為基準（run_command 的 cwd 也是 root），絕對路徑必須在 root 之下。
    """
    if not isinstance(argv0, str) or "/" not in argv0:
        raise ProjectCommandError("不是路徑形式的專案內工具")
    if len(argv0) > MAX_COMMAND_PATH_CHARS:
        raise ProjectCommandError(f"路徑至多 {MAX_COMMAND_PATH_CHARS} 字元")
    if _PATH_CONTROL.search(argv0):
        raise ProjectCommandError("路徑不可含 NUL、控制字元或無效 Unicode")
    if argv0.startswith("~"):
        raise ProjectCommandError("不展開 ~；請用專案相對路徑")
    if argv0.endswith("/"):
        raise ProjectCommandError("路徑必須指向檔案，不能以 / 結尾")
    if any(part == ".." for part in argv0.split("/")):
        raise ProjectCommandError("路徑不可含 ..")
    base = os.fspath(root)
    if not isinstance(base, str) or not os.path.isabs(base):
        raise ProjectCommandError("專案 root 必須是絕對路徑")
    base = os.path.normpath("/" + base.lstrip("/"))
    joined = argv0 if argv0.startswith("/") else base + "/" + argv0
    # 只做字面正規化：不 resolve、不 realpath，專案外路徑在這裡就拒絕。
    candidate = os.path.normpath("/" + joined.lstrip("/"))
    prefix = base if base.endswith("/") else base + "/"
    if candidate == base or not candidate.startswith(prefix):
        raise ProjectCommandError("不在專案內（只能執行專案 root 內的檔案）")
    return candidate


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
        raise _DirectoryError("專案內工具安全檢查需要 " + ", ".join(missing))


def _directory_identity(info: os.stat_result) -> tuple[int, ...]:
    # Unrelated changes to /tmp must not invalidate this path; ownership,
    # permissions and the actual inode still have to remain the same.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (*_directory_identity(info), info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_nlink)


def _validate_directory(info: os.stat_result, path: str, uid: int, *, final: bool) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise _DirectoryError(f"{path} 不是目錄或是 symlink；不跟隨連結")
    if info.st_uid not in ({uid} if final else {0, uid}):
        raise _DirectoryError(f"{path} 的 owner 不符合專案內工具信任規則")
    if info.st_mode & stat.S_IWOTH:
        if final or info.st_uid != 0 or not info.st_mode & stat.S_ISVTX:
            raise _DirectoryError(f"{path} 是 world-writable；拒絕執行其中的工具")


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


def _inspect_leaf(fd: int, name: str, uid: int) -> tuple[int, ...]:
    """Bounded, no-follow inspection of one entry; returns its verified identity."""
    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode):
        raise _DirectoryError(f"{name} 是 symlink；不跟隨連結")
    if stat.S_ISDIR(info.st_mode):
        raise _DirectoryError(f"{name} 是目錄")
    if not stat.S_ISREG(info.st_mode):
        raise _DirectoryError(f"{name} 不是普通檔")
    if info.st_uid != uid:
        raise _DirectoryError(f"{name} 不是目前使用者擁有")
    if not info.st_mode & stat.S_IXUSR:
        raise _DirectoryError(f"{name} 缺 owner execute 權限")
    if info.st_mode & stat.S_IWOTH:
        raise _DirectoryError(f"{name} 是 world-writable 檔案")
    # O_NONBLOCK prevents a regular file swapped for a FIFO between stat/open
    # from hanging the synchronous MCP server.
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
        if reason:
            raise _DirectoryError(f"{name}：{reason}")
    finally:
        os.close(leaf)
    return _file_identity(info)


def resolve_project_executable(root: str | os.PathLike[str], argv0: str) -> str:
    """回傳已驗證的專案內可執行檔絕對路徑；任何不符都 raise ProjectCommandError。

    純字串驗證與檔名規則先完成；之後自 `/` 逐層 O_DIRECTORY|O_NOFOLLOW 走到檔案所在目錄，
    葉節點不跟 symlink、有界讀檔頭，發布前再走一次整條鏈核對身分。不快取任何結果。
    """
    candidate = project_executable_candidate(root, argv0)
    parent, name = os.path.split(candidate)
    build_roots = {command.split()[0] for command in EXTRA_BUILD_COMMANDS}
    builtin_roots = {command.split()[0] for command in config.ALLOWED_COMMANDS}
    reason = _name_rejection(name, build_roots, builtin_roots)
    if reason:
        raise ProjectCommandError(f"{name} {reason}")
    try:
        _require_directory_io()
        uid = os.getuid()
        fd, chain = _open_directory(parent, uid)
        try:
            identity = _inspect_leaf(fd, name, uid)
        finally:
            os.close(fd)
        # The result is an absolute pathname, so re-open the full chain before
        # publishing it. An anchored fd alone could now name a detached tree.
        fresh, fresh_chain = _open_directory(parent, uid)
        try:
            if fresh_chain != chain:
                raise _DirectoryError("專案內工具路徑在檢查期間身分變動")
            if _file_identity(os.stat(name, dir_fd=fresh, follow_symlinks=False)) != identity:
                raise _DirectoryError(f"{name} 在檢查期間變動")
        finally:
            os.close(fresh)
    except _DirectoryError as exc:
        raise ProjectCommandError(str(exc)) from exc
    except OSError as exc:
        raise ProjectCommandError(f"找不到或無法檢查（{exc.strerror or exc}）") from exc
    except (UnicodeError, NotImplementedError, ValueError) as exc:
        raise ProjectCommandError(f"無法檢查（{exc}）") from exc
    return candidate
