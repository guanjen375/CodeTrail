"""Bounded, read-only workspace review sources (HEAD to worktree net changes)."""
from __future__ import annotations

from dataclasses import dataclass, field
import difflib
import hashlib
import os
from pathlib import Path
import re
import stat
import time
from collections import deque
from typing import Callable

import process_env
from runtime_dependencies import require_safe_filesystem


# Repository-owned limits: exceeding any one is incomplete, never a truncated review.
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
# Identity scans retain only digests. They have a separate I/O budget from the
# metadata and selected payloads retained for review.
MAX_SCAN_BYTES = 512 * 1024 * 1024
MAX_SCAN_FILE_BYTES = 64 * 1024 * 1024
MAX_SUBMODULE_DEPTH = 4
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_POLICY_LINKS = 40
MAX_POLICY_PATH_BYTES = 64 * 1024
MAX_POLICY_PATH_PARTS = 4096
MAX_PATHS = 10000
MAX_REVIEW_FILES = 128
MAX_DIFF_LINE_PRODUCT = 4_000_000
MAX_DIFF_LINES = 8000
COLLECTION_TIMEOUT_SECONDS = 60.0
GIT_TIMEOUT_SECONDS = 10.0
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_NON_TEXT_CONTROL = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ATTRIBUTES = ("text", "eol", "filter", "ident", "working-tree-encoding")


class ReviewSourceError(RuntimeError):
    """Source collection is unsafe, incomplete, or stale; never a clean review."""


class ReviewCancelled(ReviewSourceError):
    """The caller cancelled source collection or verification."""


@dataclass(frozen=True)
class ReviewFile:
    path: str
    old_text: str
    new_text: str
    old_hash: str
    new_hash: str
    diff: str
    old_changed_lines: frozenset[int]
    new_changed_lines: frozenset[int]
    status: str
    old_bytes: bytes = field(default=b"", repr=False)
    new_bytes: bytes = field(default=b"", repr=False)


@dataclass(frozen=True)
class ReviewExclusion:
    path: str
    reason: str


@dataclass(frozen=True)
class ReviewSnapshot:
    root: str
    base_oid: str | None
    snapshot_id: str
    files: tuple[ReviewFile, ...]
    excluded: tuple[ReviewExclusion, ...]
    index_only_changes: tuple[str, ...]
    _identity: tuple = field(default=(), repr=False, compare=False)


def collect_workspace(root, cancelled: Callable[[], bool] = lambda: False) -> ReviewSnapshot:
    """Collect a complete bounded source snapshot, or raise ReviewSourceError."""
    return _collect_workspace(root, cancelled)


def verify_snapshot(snapshot: ReviewSnapshot, cancelled: Callable[[], bool] = lambda: False) -> None:
    """Reject stale sources before publishing validated findings."""
    current = collect_workspace(snapshot.root, cancelled)
    if not snapshot._identity or current.snapshot_id != snapshot.snapshot_id:
        raise ReviewSourceError("工作區來源已改變；本次審查結果已過期，請重新 /review。")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _blob_hasher(size: int, algorithm: str):
    digest = hashlib.new(algorithm)
    digest.update(f"blob {size}\0".encode("ascii"))
    return digest


def _blob_oid(data: bytes, algorithm: str) -> str:
    digest = _blob_hasher(len(data), algorithm)
    digest.update(data)
    return digest.hexdigest()


def source_lines(text: str) -> list[str]:
    """Git line numbers count LF, not Unicode paragraph separators; normalize CRLF only."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _path(raw: bytes) -> str:
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ReviewSourceError("Git 路徑不是 UTF-8；無法完整審查。") from exc
    parts = value.split("/")
    if not value or any(part in ("", ".", "..", ".git") for part in parts) or "\0" in value:
        raise ReviewSourceError("Git 回傳不安全的來源路徑；拒絕審查。")
    return value


def _stat_identity(st) -> tuple:
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_nlink)


class _Budget:
    def __init__(self, cancelled):
        self.cancelled = cancelled
        self.deadline = time.monotonic() + COLLECTION_TIMEOUT_SECONDS
        self.bytes_read = 0
        self.scan_bytes = 0

    def check(self):
        if self.cancelled():
            raise ReviewCancelled("已取消工作區來源收集。")
        if time.monotonic() >= self.deadline:
            raise ReviewSourceError("工作區来源收集逾時；未完成審查。")

    def consume(self, count):
        self.check()
        self.bytes_read += count
        if self.bytes_read > MAX_TOTAL_BYTES:
            raise ReviewSourceError("工作區來源超過總讀取上限；未完成審查。")

    def scan(self, count):
        self.check()
        self.scan_bytes += count
        if self.scan_bytes > MAX_SCAN_BYTES:
            raise ReviewSourceError("工作區來源超過串流身分掃描上限；未完成審查。")


@dataclass(frozen=True)
class _Read:
    data: bytes | None
    identity: tuple
    mode: int = 0
    reason: str = ""


@dataclass(frozen=True)
class _Probe:
    identity: tuple
    mode: int = 0
    size: int = 0
    kind: str = "missing"
    raw_oid: str = ""
    lf_oid: str = ""
    has_crlf: bool = False
    has_nul: bool = False
    auto_text: bool = True
    reason: str = ""
    empty_directory: bool = False


class _Tree:
    """Read-only dir-fd tree; every component and the final inode are checked."""

    def __init__(self, root, budget):
        require_safe_filesystem("review source IO", error_type=ReviewSourceError)
        if not getattr(os, "O_NONBLOCK", 0):
            raise ReviewSourceError("review source IO requires O_NONBLOCK")
        self.path = os.path.abspath(os.fspath(root))
        self.budget = budget
        self.fd = self._open_absolute()
        self.identity = _stat_identity(os.fstat(self.fd))[:2]
        # Git inherits only these explicit directory descriptors. This fixes its
        # metadata/worktree even if a parent name changes during a command.
        self.proc_path = f"/proc/self/fd/{self.fd}"
        if not os.path.isdir(self.proc_path):
            self.close()
            raise ReviewSourceError("review requires descriptor-anchored /proc/self/fd paths")

    def _open_absolute(self):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        fd = os.open("/", flags)
        try:
            for part in Path(self.path).parts[1:]:
                self.budget.check()
                new = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = new
            return fd
        except BaseException:
            os.close(fd)
            raise

    def validate(self):
        fd = self._open_absolute()
        try:
            if _stat_identity(os.fstat(fd))[:2] != self.identity:
                raise ReviewSourceError("來源根目錄已換名或被替換；審查快照失效。")
        finally:
            os.close(fd)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _verify_named_inode(self, parts, parents, st):
        check = os.dup(self.fd)
        try:
            for part, expected in parents:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=check)
                os.close(check)
                check = child
                if _stat_identity(os.fstat(check))[:2] != expected:
                    raise ReviewSourceError("來源父目錄已被替換。")
            if _stat_identity(os.stat(parts[-1], dir_fd=check, follow_symlinks=False)) != _stat_identity(st):
                raise ReviewSourceError("來源檔案已被替換。")
        finally:
            os.close(check)

    def probe(self, relative, algorithm):
        """Stream a content identity without retaining a candidate payload.

        Symlinks contribute only their literal link text. Regular hard links,
        FIFOs and devices are never opened; metadata readers retain the same
        strict hard-link rejection as before.
        """
        parts = relative.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ReviewSourceError("來源路徑必須是 root 內的相對路徑。")
        self.budget.check()
        parents = []
        current = os.dup(self.fd)
        fd = None
        try:
            for part in parts[:-1]:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                except FileNotFoundError:
                    return _Probe(("missing-parent", tuple(parents)))
                parents.append((part, _stat_identity(os.fstat(child))[:2]))
                os.close(current)
                current = child
            try:
                st = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
            except FileNotFoundError:
                return _Probe(("missing", tuple(parents)))
            identity = (tuple(parents), _stat_identity(st))
            if stat.S_ISDIR(st.st_mode):
                if os.scandir not in getattr(os, "supports_fd", set()):
                    raise ReviewSourceError("review directory identity requires fd-based scandir")
                fd = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                if _stat_identity(os.fstat(fd)) != _stat_identity(st):
                    raise ReviewSourceError("來源目錄在開啟時被替換；審查快照失效。")
                self.budget.check()
                with os.scandir(fd) as entries:
                    empty = next(entries, None) is None
                self.budget.check()
                if _stat_identity(os.fstat(fd)) != _stat_identity(st):
                    raise ReviewSourceError("來源目錄在檢查期間已改變；審查快照失效。")
                self._verify_named_inode(parts, parents, st)
                return _Probe((*identity, ("directory-empty", empty)), st.st_mode, st.st_size,
                              "directory", empty_directory=empty)
            if stat.S_ISLNK(st.st_mode):
                if os.readlink not in os.supports_dir_fd:
                    raise ReviewSourceError("review symlink identity requires dir-fd readlink")
                target = os.fsencode(os.readlink(parts[-1], dir_fd=current))
                self.budget.scan(len(target))
                self._verify_named_inode(parts, parents, st)
                oid = _blob_oid(target, algorithm)
                return _Probe((*identity, _digest(target)), st.st_mode, len(target), "symlink", oid, oid)
            if not stat.S_ISREG(st.st_mode):
                return _Probe(identity, st.st_mode, st.st_size, "unsafe", reason="非一般檔案（FIFO 或 device）")
            if st.st_nlink != 1:
                return _Probe(identity, st.st_mode, st.st_size, "unsafe", reason="hard link 不可作為審查來源")
            if st.st_size > MAX_SCAN_FILE_BYTES:
                return _Probe(identity, st.st_mode, st.st_size, "regular", reason=f"超過單檔身分掃描上限 {MAX_SCAN_FILE_BYTES} bytes；內容未知")
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
            if _stat_identity(os.fstat(fd)) != _stat_identity(st):
                raise ReviewSourceError("來源檔案在開啟時被替換；審查快照失效。")
            raw = _blob_hasher(st.st_size, algorithm)
            content = hashlib.sha256()
            total = crlfs = crs = 0
            previous_cr = has_nul = controls = False
            while True:
                self.budget.check()
                block = os.read(fd, 65536)
                if not block:
                    break
                total += len(block)
                self.budget.scan(len(block))
                if total > st.st_size:
                    raise ReviewSourceError("來源檔案讀取期間已改變。")
                raw.update(block)
                content.update(block)
                crlfs += block.count(b"\r\n") + int(previous_cr and block.startswith(b"\n"))
                crs += block.count(b"\r")
                previous_cr = block.endswith(b"\r")
                has_nul = has_nul or b"\0" in block
                controls = controls or _NON_TEXT_CONTROL.search(block) is not None
            if total != st.st_size or _stat_identity(os.fstat(fd)) != _stat_identity(st):
                raise ReviewSourceError("來源檔案讀取期間已改變。")
            raw_oid = raw.hexdigest()
            lf_oid = raw_oid
            if crlfs:
                # Git's blob header includes the normalized byte length, so its
                # CRLF identity needs a second bounded pass, not a retained blob.
                os.lseek(fd, 0, os.SEEK_SET)
                normalized = _blob_hasher(st.st_size - crlfs, algorithm)
                second = hashlib.sha256()
                tail = b""
                second_size = 0
                while True:
                    self.budget.check()
                    block = os.read(fd, 65536)
                    if not block:
                        break
                    second_size += len(block)
                    self.budget.scan(len(block))
                    if second_size > st.st_size:
                        raise ReviewSourceError("來源檔案正規化掃描期間已改變。")
                    second.update(block)
                    joined = tail + block
                    tail = b"\r" if joined.endswith(b"\r") else b""
                    if tail:
                        joined = joined[:-1]
                    normalized.update(joined.replace(b"\r\n", b"\n"))
                normalized.update(tail)
                if second_size != total or second.digest() != content.digest():
                    raise ReviewSourceError("來源檔案正規化掃描期間已改變。")
                lf_oid = normalized.hexdigest()
            if _stat_identity(os.fstat(fd)) != _stat_identity(st):
                raise ReviewSourceError("來源檔案掃描期間已改變。")
            self._verify_named_inode(parts, parents, st)
            return _Probe((*identity, content.hexdigest()), st.st_mode, total, "regular",
                          raw_oid, lf_oid, bool(crlfs), has_nul,
                          auto_text=not (has_nul or controls or crs != crlfs))
        finally:
            if fd is not None:
                os.close(fd)
            os.close(current)

    def read(self, relative, limit=MAX_FILE_BYTES):
        parts = relative.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ReviewSourceError("來源路徑必須是 root 內的相對路徑。")
        self.budget.check()
        parents = []
        current = os.dup(self.fd)
        fd = None
        try:
            for part in parts[:-1]:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                except FileNotFoundError:
                    return _Read(None, ("missing-parent", tuple(parents)))
                parents.append((part, _stat_identity(os.fstat(child))[:2]))
                os.close(current)
                current = child
            try:
                st = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
            except FileNotFoundError:
                return _Read(None, ("missing", tuple(parents)))
            identity = (tuple(parents), _stat_identity(st))
            if not stat.S_ISREG(st.st_mode):
                return _Read(None, identity, st.st_mode, "非一般檔案（symlink、FIFO、device 或目錄）")
            if st.st_nlink != 1:
                return _Read(None, identity, st.st_mode, "hard link 不可作為審查來源")
            if st.st_size > limit:
                return _Read(None, identity, st.st_mode, f"超過單檔讀取上限 {limit} bytes；內容未知")
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
            opened = os.fstat(fd)
            if _stat_identity(opened) != _stat_identity(st) or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise ReviewSourceError("來源檔案在開啟時被替換；審查快照失效。")
            chunks = []
            total = 0
            while True:
                self.budget.check()
                block = os.read(fd, min(65536, limit + 1 - total))
                if not block:
                    break
                total += len(block)
                self.budget.consume(len(block))
                if total > limit:
                    raise ReviewSourceError("來源檔案讀取期間超過大小上限。")
                chunks.append(block)
            if _stat_identity(os.fstat(fd)) != _stat_identity(st):
                raise ReviewSourceError("來源檔案讀取期間已改變。")
            data = b"".join(chunks)
            if len(data) != st.st_size:
                raise ReviewSourceError("來源檔案大小與讀取內容不一致。")
            # Rewalk by name. An open parent fd must not hide a replaced parent.
            check = os.dup(self.fd)
            try:
                for part, expected in parents:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=check)
                    os.close(check)
                    check = child
                    if _stat_identity(os.fstat(check))[:2] != expected:
                        raise ReviewSourceError("來源父目錄已被替換。")
                if _stat_identity(os.stat(parts[-1], dir_fd=check, follow_symlinks=False)) != _stat_identity(st):
                    raise ReviewSourceError("來源檔案已被替換。")
            finally:
                os.close(check)
            return _Read(data, (*identity, _digest(data)), st.st_mode)
        finally:
            if fd is not None:
                os.close(fd)
            os.close(current)


def _required(read: _Read, label: str) -> bytes:
    if read.reason or read.data is None:
        raise ReviewSourceError(f"無法安全讀取 {label}：{read.reason or '不存在'}")
    return read.data


def _resolve_policy_path(path, budget):
    """Resolve only an explicitly selected external Git policy source.

    Worktree and repository metadata readers never call this resolver. Every
    link is read as link text, every traversed directory is opened nofollow,
    and the recorded chain is resolved again after the bounded regular read.
    This also supports reviewing a dotfiles repository containing the target.
    """
    if not path or not os.path.isabs(path) or "\0" in path:
        raise ReviewSourceError("Git 回傳非絕對的全域設定路徑。")
    require_safe_filesystem("review external Git policy IO", error_type=ReviewSourceError)
    if os.readlink not in os.supports_dir_fd:
        raise ReviewSourceError("review external Git policy IO requires dir-fd readlink")
    path_bytes = len(os.fsencode(path))
    if path_bytes > MAX_POLICY_PATH_BYTES:
        raise ReviewSourceError("全域 Git 設定路徑超過長度上限。")
    budget.consume(path_bytes)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptors = [os.open("/", flags)]
    resolved, history = [], []
    pending = deque(path.split("/"))
    links = steps = 0
    try:
        history.append(("root", _stat_identity(os.fstat(descriptors[0]))[:3]))
        while pending:
            budget.check()
            steps += 1
            if steps > MAX_POLICY_PATH_PARTS:
                raise ReviewSourceError("全域 Git 設定路徑展開超過上限。")
            part = pending.popleft()
            if part in ("", "."):
                continue
            if part == "..":
                if resolved:
                    resolved.pop()
                    os.close(descriptors.pop())
                history.append(("parent",))
                continue
            try:
                st = os.stat(part, dir_fd=descriptors[-1], follow_symlinks=False)
            except FileNotFoundError:
                history.append(("missing", part, tuple(pending)))
                return None, tuple(history)
            if stat.S_ISLNK(st.st_mode):
                links += 1
                if links > MAX_POLICY_LINKS:
                    raise ReviewSourceError("全域 Git 設定 symlink 循環或連結層數超過上限。")
                target = os.readlink(part, dir_fd=descriptors[-1])
                if _stat_identity(os.stat(part, dir_fd=descriptors[-1], follow_symlinks=False)) != _stat_identity(st):
                    raise ReviewSourceError("全域 Git 設定連結在解析期间已改變。")
                target_bytes = len(os.fsencode(target))
                path_bytes += target_bytes
                if path_bytes > MAX_POLICY_PATH_BYTES:
                    raise ReviewSourceError("全域 Git 設定 symlink 展開超過長度上限。")
                budget.consume(target_bytes)
                history.append(("link", part, _stat_identity(st), target))
                if os.path.isabs(target):
                    while len(descriptors) > 1:
                        os.close(descriptors.pop())
                    resolved.clear()
                    history.append(("root", _stat_identity(os.fstat(descriptors[0]))[:3]))
                pending.extendleft(reversed(target.split("/")))
                continue
            if not pending:
                history.append(("final", part, _stat_identity(st)))
                return "/" + "/".join((*resolved, part)), tuple(history)
            if not stat.S_ISDIR(st.st_mode):
                raise ReviewSourceError("全域 Git 設定來源的父路徑不是目錄。")
            child = os.open(part, flags, dir_fd=descriptors[-1])
            descriptors.append(child)
            if _stat_identity(os.fstat(child))[:3] != _stat_identity(st)[:3]:
                raise ReviewSourceError("全域 Git 設定父目錄在解析期間被替換。")
            resolved.append(part)
            history.append(("directory", part, _stat_identity(st)[:3]))
        # A path ending at a directory (including '/', '.' or '..') is not a
        # regular config file. Preserve that failure rather than treating it as
        # an absent optional config.
        raise ReviewSourceError("全域 Git 設定來源不是一般檔案。")
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


class _Repository:
    def __init__(self, root, budget):
        self.budget = budget
        self.trees = []
        try:
            self.work = self._tree(root)
            marker = self.work.read(".git", MAX_METADATA_BYTES)
            if stat.S_ISDIR(marker.mode):
                self.git = self._tree(Path(self.work.path) / ".git")
                self.common = self.git
            else:
                raw = _required(marker, ".git")
                if not raw.startswith(b"gitdir: ") or b"\0" in raw or raw.count(b"\n") > 1:
                    raise ReviewSourceError("/review 只接受 Git 頂層目錄或合法 linked worktree。")
                location = raw[8:].rstrip(b"\r\n").decode("utf-8", "strict")
                self.git = self._tree(Path(self.work.path) / location)
                common_entry = self.git.read("commondir", 4096)
                if common_entry.reason:
                    raise ReviewSourceError("Git commondir 來源不安全。")
                if common_entry.data is None:
                    # Ordinary submodules / separate-git-dir repositories have
                    # a gitfile but no linked-worktree commondir/backlink pair.
                    # Explicit worktree/git-dir descriptors still fix the scope.
                    self.common = self.git
                else:
                    common = common_entry.data
                    common_path = common.rstrip(b"\r\n").decode("utf-8", "strict")
                    if not common_path or b"\0" in common:
                        raise ReviewSourceError("linked worktree commondir 無效。")
                    self.common = self._tree(Path(self.git.path) / common_path)
                    backlink = _required(self.git.read("gitdir", 4096), "linked worktree gitdir")
                    if os.path.abspath(os.fsdecode(backlink.rstrip(b"\r\n"))) != str(Path(self.work.path) / ".git"):
                        raise ReviewSourceError("linked worktree gitdir 未綁定本 sandbox。")
            self.marker = marker.identity
        except BaseException:
            self.close()
            raise

    def _tree(self, path):
        tree = _Tree(path, self.budget)
        self.trees.append(tree)
        return tree

    def close(self):
        for tree in reversed(self.trees):
            tree.close()

    def git_run(self, *args, input_bytes=b"", allowed=(0,), limit=MAX_METADATA_BYTES):
        self.budget.check()
        for tree in self.trees:
            tree.validate()
        try:
            result = process_env.review_git(
                args, cwd=self.work.proc_path, git_dir=self.git.proc_path,
                work_tree=self.work.proc_path, common_dir=self.common.proc_path,
                pass_fds=tuple(tree.fd for tree in self.trees),
                cancelled=self.budget.cancelled,
                timeout=min(GIT_TIMEOUT_SECONDS, max(0.001, self.budget.deadline - time.monotonic())),
                max_output=limit, input_bytes=input_bytes,
            )
        except process_env.ReviewGitCancelled as exc:
            raise ReviewCancelled(str(exc)) from exc
        except process_env.ReviewGitError as exc:
            raise ReviewSourceError(str(exc)) from exc
        self.budget.consume(len(result.stdout) + len(result.stderr))
        if result.returncode not in allowed:
            detail = result.stderr.decode("utf-8", "replace").strip()[:1000]
            raise ReviewSourceError(f"Git {args[0]} 失敗（{result.returncode}）：{detail}")
        return result

    def metadata(self):
        # Validate files Git may open before invoking it. No command refreshes or
        # writes the index; a nonregular metadata file is a collection failure.
        identities = []
        self.info_attributes = False
        for tree, names in ((self.git, ("HEAD", "index", "config.worktree", "commondir", "gitdir")),
                            (self.common, ("config", "info/attributes", "info/exclude"))):
            for name in names:
                item = tree.read(name, MAX_METADATA_BYTES)
                if item.reason:
                    raise ReviewSourceError(f"Git metadata {name} 不安全：{item.reason}")
                if name == "info/attributes" and item.data:
                    self.info_attributes = True
                identities.append((tree.path, name, item.identity))
        if self.work.read(".git", MAX_METADATA_BYTES).identity != self.marker:
            raise ReviewSourceError("Git metadata 入口已改變。")
        cfg = self.git_run("config", "--null", "--list", "--includes").stdout
        config = _config_values(cfg)
        external, external_identity = self.external_policy()
        config = {**external, **config}
        if any(key == "extensions.partialclone" or (key.startswith("remote.") and key.endswith(".promisor") and value.lower() not in ("false", "no", "off", "0")) for key, value in config.items()):
            raise ReviewSourceError("partial/promisor clone 可能隱含 lazy fetch；本地審查拒絕此來源。")
        head_result = self.git_run("rev-parse", "--verify", "--quiet", "HEAD", allowed=(0, 1))
        base = head_result.stdout.strip().decode("ascii") if head_result.returncode == 0 else None
        if base is not None and not _OID.fullmatch(base):
            raise ReviewSourceError("Git HEAD 不是可驗證的 object id。")
        if base is None:
            symbolic = self.git_run("symbolic-ref", "--quiet", "HEAD").stdout.strip().decode("utf-8", "strict")
            if not symbolic.startswith("refs/heads/") or "\n" in symbolic:
                raise ReviewSourceError("Git HEAD 無效，不能當成新 repo 空基底。")
            if self.git_run("show-ref", "--verify", "--quiet", symbolic, allowed=(0, 1)).returncode != 1:
                raise ReviewSourceError("Git HEAD 無法解析；不能當成新 repo 空基底。")
        head_tree = self.git_run("ls-tree", "-rz", "--full-tree", base).stdout if base else b""
        index = self.git_run("ls-files", "--stage", "-z").stdout
        untracked = self.git_run("ls-files", "--others", "--exclude-standard", "-z").stdout
        return (tuple(identities) + external_identity, cfg, base, head_tree, index, untracked), config

    def external_policy(self):
        """Read declarative normalization only; never enable global Git helpers.

        Known user paths are discovered without letting Git read global config.
        Only this policy reader resolves symlinks, then reads the final regular
        file through nofollow descriptors and revalidates the complete chain.
        Config bytes are parsed on stdin with includes off. Git's compiled
        system-config path query still parses system config before returning;
        that existing subprocess remains bounded and user-global-isolated.
        Global attributes/ignores and includes are deliberately unsupported in
        v1: ignoring a nonempty one would invent the set of review candidates.
        """
        identities = []

        def read(path):
            tree = None
            try:
                resolved, chain = _resolve_policy_path(path, self.budget)
                if resolved is None:
                    if _resolve_policy_path(path, self.budget) != (resolved, chain):
                        raise ReviewSourceError("全域 Git 設定路徑在讀取期間改變；審查快照失效。")
                    identities.append((path, chain, "absent"))
                    return b""
                tree = _Tree(Path(resolved).parent, self.budget)
                item = tree.read(Path(resolved).name, MAX_METADATA_BYTES)
                if item.reason:
                    raise ReviewSourceError(f"全域 Git 來源 {path} 不安全：{item.reason}")
                tree.validate()
                if _resolve_policy_path(path, self.budget) != (resolved, chain):
                    raise ReviewSourceError("全域 Git 設定路徑在讀取期間改變；審查快照失效。")
                identities.append((path, chain, tree.identity, item.identity))
                return item.data or b""
            except OSError as exc:
                raise ReviewSourceError(f"全域 Git 設定來源無法安全讀取：{exc}") from exc
            finally:
                if tree is not None:
                    tree.close()

        def paths(name):
            result = self.git_run("var", name, allowed=(0, 1), limit=65536)
            if result.returncode == 1:
                # Unsupported Git versions must not silently assume there is no
                # system/global configuration. A disabled default has no output
                # only when Git exits successfully.
                raise ReviewSourceError(f"Git 無法提供 {name} 預設路徑，無法確認全域來源語義。")
            return result.stdout.decode("utf-8", "strict").splitlines()

        settings = {}
        # Match Git's actual system -> XDG -> ~/.gitconfig precedence, while
        # global source bytes enter Git only via the no-includes stdin parser.
        config_paths = paths("GIT_CONFIG_SYSTEM") + list(process_env.review_git_global_config_paths())
        for path in config_paths:
            data = read(path)
            if not data:
                continue
            parsed = _config_values(self.git_run("config", "--null", "--file", "-", "--list", "--no-includes", input_bytes=data).stdout)
            if any(key == "include.path" or key.startswith("includeif.") for key in parsed):
                raise ReviewSourceError("全域 Git config include 無法安全重現；審查來源不完整。")
            settings.update(parsed)
        # Unlike the forced command-line value used for worktree plumbing, this
        # scope query exposes the repository's actual declarative excludesFile.
        local = _config_values(self.git_run("config", "--null", "--local", "--list", "--includes").stdout)
        effective = {**settings, **local}
        if effective.get("extensions.worktreeconfig", "").lower() in ("true", "yes", "on", "1"):
            raw = self.git.read("config.worktree", MAX_METADATA_BYTES)
            if raw.data:
                effective.update(_config_values(self.git_run("config", "--null", "--file", "-", "--list", "--no-includes", input_bytes=raw.data).stdout))
        attribute_path = effective.get("core.attributesfile")
        if attribute_path is None:
            attribute_path = process_env.review_git_default_attributes()
        elif attribute_path:
            attribute_path = process_env.review_git_expand_config_path(attribute_path, self.work.path)
        # Git preserves an explicitly configured empty path. It disables the
        # user attributes source instead of falling back to the XDG default.
        for path in paths("GIT_ATTR_SYSTEM") + [attribute_path]:
            if path and path != os.devnull and read(path).strip():
                raise ReviewSourceError("全域/外部 Git attributes 未啟用；無法確定變更正規化，審查來源不完整。")
        ignore = effective.get("core.excludesfile")
        ignore_path = process_env.review_git_expand_config_path(ignore, self.work.path) if ignore else process_env.review_git_default_ignore()
        if ignore_path != os.devnull and read(ignore_path).strip():
            raise ReviewSourceError("全域/外部 Git excludesFile 未啟用；新增檔案候選集合未知，審查來源不完整。")
        return settings, tuple(identities)

    def blob(self, oid):
        if not _OID.fullmatch(oid):
            raise ReviewSourceError("Git blob identity 無效。")
        size = self.git_run("cat-file", "-s", oid, limit=4096).stdout.strip()
        if not size.isdigit():
            raise ReviewSourceError("Git blob 大小無效。")
        if int(size) > MAX_FILE_BYTES:
            return None
        raw = self.git_run("cat-file", "blob", oid, limit=MAX_FILE_BYTES + 4096).stdout
        if len(raw) != int(size):
            raise ReviewSourceError("Git blob 大小與內容不一致。")
        return raw


def _config_values(raw):
    result = {}
    for item in raw.split(b"\0"):
        if item:
            key, _, value = item.partition(b"\n")
            result[key.decode("utf-8", "strict").lower()] = value.decode("utf-8", "strict")
    return result


def _entries(raw, *, index):
    entries = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, tab, name = record.partition(b"\t")
        fields = meta.split()
        if not tab or len(fields) != 3:
            raise ReviewSourceError("Git 來源清單格式不完整。")
        path = _path(name)
        if index:
            mode, oid, stage = fields
            if stage != b"0":
                raise ReviewSourceError("index 含未合併項目；請先處理 merge conflict 再 /review。")
        else:
            mode, kind, oid = fields
            if kind not in (b"blob", b"commit"):
                raise ReviewSourceError("Git tree 含未知來源類型。")
        if path in entries or not _OID.fullmatch(oid.decode("ascii")):
            raise ReviewSourceError("Git 來源清單有重複路徑或無效 identity。")
        entries[path] = (mode.decode("ascii"), oid.decode("ascii"))
    return entries


def _attributes(repo, paths, algorithm):
    if not paths:
        return {}, ()
    attribute_files = {".gitattributes"}
    for path in paths:
        parent = Path(path).parent
        while str(parent) != ".":
            attribute_files.add(parent.as_posix() + "/.gitattributes")
            parent = parent.parent
    identities = []
    for path in sorted(attribute_files):
        item = repo.work.read(path)
        if item.reason:
            raise ReviewSourceError(f"Git attribute 來源 {path} 不安全：{item.reason}")
        identities.append((path, item.identity,
                           _blob_oid(item.data, algorithm) if item.data is not None else None))
    data = b"".join(path.encode("utf-8") + b"\0" for path in paths)
    output = repo.git_run("check-attr", "-z", "--stdin", *_ATTRIBUTES, input_bytes=data).stdout
    values = output.split(b"\0")
    if not values or values.pop() != b"" or len(values) != len(paths) * len(_ATTRIBUTES) * 3:
        raise ReviewSourceError("Git attribute 清單不完整。")
    result = {path: {} for path in paths}
    for i in range(0, len(values), 3):
        path, attr, value = (item.decode("utf-8", "strict") for item in values[i:i + 3])
        if path not in result or attr not in _ATTRIBUTES or attr in result[path]:
            raise ReviewSourceError("Git attribute identity 不一致。")
        result[path][attr] = value
    return result, tuple(identities)


def _attribute_sources_unchanged(path, attribute_oids, head, index, *, info_attributes):
    """Raw equality cannot prove clean if a relevant attribute source changed.

    A missing worktree .gitattributes falls back to the index in check-attr; the
    deletion itself still invalidates the preselection proof, even when that
    fallback happens to contain the previous values.
    """
    if info_attributes:
        return False  # No versioned baseline for repository-local info/attributes.
    relevant = {".gitattributes"}
    parent = Path(path).parent
    while str(parent) != ".":
        relevant.add(parent.as_posix() + "/.gitattributes")
        parent = parent.parent
    for name in relevant:
        work_oid = attribute_oids[name]
        old = head.get(name)
        indexed = index.get(name)
        if old != indexed or work_oid != (old[1] if old else None):
            return False
    return True


def _unsupported(attrs):
    for name in ("filter", "ident", "working-tree-encoding"):
        if attrs[name] not in ("unspecified", "unset"):
            return f"不支援 Git {name}={attrs[name]} 的內容轉換；候選內容未知"
    if attrs["text"] not in ("unspecified", "unset", "set", "auto") or attrs["eol"] not in ("unspecified", "unset", "lf", "crlf"):
        return "不支援的 Git text/eol 屬性；候選內容未知"
    return ""


def _decode(raw):
    if b"\0" in raw or b"\r" in raw.replace(b"\r\n", b"") or any(byte < 32 and byte not in (9, 10, 13) for byte in raw):
        raise ValueError("二進位或不支援的文字控制字元")
    try:
        return raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ValueError("不是可安全映射行號的 UTF-8 文字") from exc


def _review_file(path, old, new, semantic_new, status, budget, *, mode_changed=False):
    old_text, new_text = _decode(old), _decode(new)
    # Do not count Unicode paragraph separators as Git newlines.
    before = old_text.split("\n")
    after = semantic_new.decode("utf-8", "strict").split("\n")
    if before[-1] == "":
        before.pop()
    if after[-1] == "":
        after.pop()
    if len(before) + len(after) > MAX_DIFF_LINES or len(before) * len(after) > MAX_DIFF_LINE_PRODUCT:
        raise ValueError("diff 行數或比較成本超過上限")
    budget.check()
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=True)
    old_changed, new_changed = set(), set()
    for tag, i, j, a, b in matcher.get_opcodes():
        if tag != "equal":
            old_changed.update(range(i + 1, j + 1))
            new_changed.update(range(a + 1, b + 1))
    # A final LF change is still a changed line even though split line content
    # is identical; absent empty files remain listed with no legal line anchor.
    if old.endswith(b"\n") != semantic_new.endswith(b"\n"):
        if before:
            old_changed.add(len(before))
        if after:
            new_changed.add(len(after))
    diff = "\n".join(difflib.unified_diff(before, after, fromfile="a/" + path, tofile="b/" + path, lineterm=""))
    if mode_changed:
        diff = "[executable mode changed]\n" + diff
    if old.endswith(b"\n") != semantic_new.endswith(b"\n"):
        diff += "\n[final LF changed]"
    budget.check()
    return ReviewFile(path, old_text, new_text, _digest(old), _digest(new), diff,
                      frozenset(old_changed), frozenset(new_changed), status, old, new)


def _semantic_oid(probe, attrs, autocrlf, index_entry, read_blob):
    """Choose the raw/CRLF content identity without invoking a clean filter."""
    if attrs["text"] == "unset":
        return probe.raw_oid
    explicit = attrs["text"] == "set" or (attrs["text"] == "unspecified" and attrs["eol"] in ("lf", "crlf"))
    if explicit:
        return probe.lf_oid
    automatic = attrs["text"] == "auto" or (attrs["text"] == "unspecified" and autocrlf != "false")
    if not automatic or not probe.has_crlf or not probe.auto_text:
        return probe.raw_oid
    if index_entry is None or index_entry[1] == probe.lf_oid:
        return probe.lf_oid
    if index_entry[1] == probe.raw_oid:
        return probe.raw_oid  # The unchanged index already contains CRLF.
    indexed = read_blob(index_entry)
    if indexed is None:
        raise ValueError("index 的 EOL 身分超過可驗證上限；內容轉換未知")
    return probe.raw_oid if b"\r\n" in indexed else probe.lf_oid


def _submodule_state(path, budget, depth):
    if depth >= MAX_SUBMODULE_DEPTH:
        return "depth-limit", None, True, "submodule 深度超過來源檢查上限"
    try:
        snapshot, changed = _collect_repository(path, budget, depth + 1, identity_only=True)
        return snapshot.snapshot_id, snapshot.base_oid, changed, ""
    except ReviewCancelled:
        raise
    except ReviewSourceError as exc:
        # Parent files remain reviewable, but an unverified nested repository
        # can never count as a clean gitlink. The reason remains explicit.
        reason = f"submodule 無法驗證：{exc}"
        return _digest(reason.encode("utf-8")), None, True, reason


def _collect_workspace(root, cancelled):
    return _collect_repository(root, _Budget(cancelled), 0)[0]


def _collect_repository(root, budget, depth, *, identity_only=False):
    repo = None
    try:
        repo = _Repository(root, budget)
        meta, config = repo.metadata()
        _, _, base, tree_data, index_data, untracked = meta
        head, index = _entries(tree_data, index=False), _entries(index_data, index=True)
        untracked_paths, nested_paths = set(), set()
        for raw in untracked.split(b"\0"):
            if not raw:
                continue
            if raw.endswith(b"/"):
                # ls-files reports an untracked embedded repository as a single
                # legal directory item. Do not feed that slash to a file parser.
                nested_paths.add(_path(raw[:-1]))
            else:
                untracked_paths.add(_path(raw))
        paths = sorted(set(head) | set(index) | untracked_paths | nested_paths)
        if len(paths) > MAX_PATHS:
            raise ReviewSourceError(f"工作區來源超過 {MAX_PATHS} 路徑上限；未完成審查。")
        algorithm = "sha256" if any(len(entry[1]) == 64 for entry in (*head.values(), *index.values())) else "sha1"
        autocrlf = config.get("core.autocrlf", "false").lower()
        autocrlf = {"yes": "true", "on": "true", "1": "true", "no": "false", "off": "false", "0": "false"}.get(autocrlf, autocrlf)
        if autocrlf not in ("true", "input", "false"):
            raise ReviewSourceError("不支援的 core.autocrlf；無法確認淨變更。")
        filemode = config.get("core.filemode", "true").lower() not in ("false", "no", "off", "0")
        attrs, attribute_identity = _attributes(repo, paths, algorithm)
        attribute_oids = {name: oid for name, _, oid in attribute_identity}
        files, excluded, index_only, sources = [], [], [], []
        changed_paths = []
        blobs = {}

        def read_blob(entry):
            if entry is None:
                return b""
            if entry[1] not in blobs:
                blobs[entry[1]] = repo.blob(entry[1])
            return blobs[entry[1]]

        def exclude(path, reason):
            excluded.append(ReviewExclusion(path, reason))
            if len(changed_paths) + len(excluded) > MAX_REVIEW_FILES:
                raise ReviewSourceError(f"候選來源超過 {MAX_REVIEW_FILES} 檔案上限；未完成審查。")

        for path in paths:
            budget.check()
            work = repo.work.probe(path, algorithm)
            old_entry, index_entry = head.get(path), index.get(path)
            nested_identity = None
            if path in nested_paths:
                sources.append((path, work.identity, None))
                exclude(path, "未追蹤的巢狀 Git repository；不支援審查其內部內容")
                continue
            is_gitlink = any(entry and entry[0] == "160000" for entry in (old_entry, index_entry))
            if is_gitlink and work.kind == "directory":
                if work.empty_directory:
                    sources.append((path, work.identity, None))
                    if old_entry and old_entry[0] == "160000" and old_entry == index_entry:
                        continue
                    # An unpopulated worktree contributes its index gitlink to
                    # HEAD comparison. A changed pointer is not an index-only
                    # cancellation without an actual nested HEAD proving that.
                    exclude(path, "submodule 未 checkout 且 gitlink 有變更；不支援逐檔審查")
                    continue
                nested_identity, nested_head, nested_changed, reason = _submodule_state(
                    Path(repo.work.path) / path, budget, depth)
                sources.append((path, work.identity, nested_identity))
                if not reason and not nested_changed and old_entry and old_entry[0] == "160000" and nested_head == old_entry[1]:
                    if old_entry != index_entry:
                        index_only.append(path)
                    continue
                exclude(path, reason or "submodule HEAD 或內部內容有變更；不支援逐檔審查")
                continue
            sources.append((path, work.identity, nested_identity))
            if work.reason:
                exclude(path, work.reason)
                continue
            exists = work.kind != "missing"
            mode = "120000" if work.kind == "symlink" else (
                ("100755" if work.mode & stat.S_IXUSR else "100644") if filemode
                else (index_entry or old_entry or ("100644",))[0]
            )
            mode_changed = bool(old_entry and exists and old_entry[0] != mode)
            attributes_unchanged = _attribute_sources_unchanged(
                path, attribute_oids, head, index, info_attributes=repo.info_attributes)
            unsupported = _unsupported(attrs[path])
            # Symlink content has no clean conversion. Opaque regular-file
            # transforms require raw equality AND an unchanged index/attribute
            # baseline. Supported text must always go through _semantic_oid:
            # HEAD==raw WT can still differ from an LF-renormalized index.
            if (old_entry and not mode_changed and work.raw_oid == old_entry[1]
                    and (work.kind == "symlink" or (
                        unsupported and attributes_unchanged and old_entry == index_entry))):
                if old_entry != index_entry:
                    index_only.append(path)
                continue
            if unsupported:
                exclude(path, unsupported)
                continue
            if work.kind not in ("regular", "missing"):
                exclude(path, "不支援已變更的 symlink、目錄或未知來源類型")
                continue
            if any(entry and entry[0] not in ("100644", "100755") for entry in (old_entry, index_entry)):
                exclude(path, "不支援已變更的 symlink/submodule 或未知 Git mode")
                continue
            try:
                semantic_oid = _semantic_oid(work, attrs[path], autocrlf, index_entry, read_blob) if exists else ""
            except ValueError as exc:
                exclude(path, str(exc))
                continue
            if bool(old_entry) == exists and not mode_changed and (not exists or semantic_oid == old_entry[1]):
                if old_entry != index_entry:
                    index_only.append(path)
                continue
            status = "added" if old_entry is None else "deleted" if not exists else "modified"
            if identity_only:
                changed_paths.append(path)
                if len(changed_paths) + len(excluded) > MAX_REVIEW_FILES:
                    raise ReviewSourceError(f"候選來源超過 {MAX_REVIEW_FILES} 檔案上限；未完成審查。")
                continue
            if work.size > MAX_FILE_BYTES:
                exclude(path, f"已變更檔案超過單檔 payload 上限 {MAX_FILE_BYTES} bytes")
                continue
            payload = repo.work.read(path)
            if payload.identity != work.identity:
                raise ReviewSourceError(f"來源 {path} 在選取後改變；審查快照失效。")
            old = read_blob(old_entry)
            if old is None:
                exclude(path, "已變更檔案的 HEAD blob 超過單檔 payload 上限")
                continue
            new = payload.data if payload.data is not None else b""
            # Selection already proved which representation Git uses; retaining
            # an unrelated index payload would spend the review budget again.
            semantic = new.replace(b"\r\n", b"\n") if exists and semantic_oid == work.lf_oid and work.lf_oid != work.raw_oid else new
            try:
                files.append(_review_file(path, old, new, semantic, status, budget, mode_changed=mode_changed))
                changed_paths.append(path)
            except ValueError as exc:
                exclude(path, str(exc))
            if len(changed_paths) + len(excluded) > MAX_REVIEW_FILES:
                raise ReviewSourceError(f"候選來源超過 {MAX_REVIEW_FILES} 檔案上限；未完成審查。")
        if len(changed_paths) + len(excluded) > MAX_REVIEW_FILES:
            raise ReviewSourceError(f"候選來源超過 {MAX_REVIEW_FILES} 檔案上限；未完成審查。")
        # A coherent snapshot must survive an end-of-collection re-read. Hashes
        # bind raw bytes, not merely mtime/index stat hints (assume-unchanged and
        # skip-worktree therefore cannot conceal a worktree change).
        if repo.metadata()[0] != meta or _attributes(repo, paths, algorithm)[1] != attribute_identity:
            raise ReviewSourceError("Git HEAD/index/attributes 在收集期間改變；審查快照失效。")
        for path, identity, nested_identity in sources:
            if repo.work.probe(path, algorithm).identity != identity:
                raise ReviewSourceError(f"來源 {path} 在收集期間改變；審查快照失效。")
            if nested_identity is not None:
                current_nested = _submodule_state(Path(repo.work.path) / path, budget, depth)[0]
                if current_nested != nested_identity:
                    raise ReviewSourceError(f"submodule {path} 在收集期間改變；審查快照失效。")
        for tree in repo.trees:
            tree.validate()
        identity = (repo.work.identity, meta, attribute_identity, tuple(sources))
        snapshot_id = _digest(repr(identity).encode("utf-8"))
        budget.check()
        snapshot = ReviewSnapshot(repo.work.path, base, snapshot_id, tuple(files), tuple(excluded), tuple(index_only), identity)
        return snapshot, bool(changed_paths or excluded or index_only)
    except ReviewSourceError:
        raise
    except (OSError, UnicodeError, ValueError, process_env.ReviewGitError) as exc:
        raise ReviewSourceError(f"無法安全收集工作區來源：{exc}") from exc
    finally:
        if repo is not None:
            repo.close()
