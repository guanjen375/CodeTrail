"""Bounded, anchored metadata reads for evidence tools; no model/cache loading."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat

MAX_JSON_BYTES = 128 * 1024 * 1024
MAX_SOURCE_BYTES = 1024 * 1024 * 1024


class EvidenceStoreError(RuntimeError):
    pass


def _require():
    from runtime_dependencies import require_safe_filesystem
    require_safe_filesystem("evidence store", error_type=EvidenceStoreError)


def _regular(fd):
    # These are user-owned sources, knowledge.json, or its existing store lock;
    # none lives in an evidence_store-private directory. Existing projects and
    # the legacy lock writer legitimately inherit umask 002 (0664). Ownership,
    # file kind and link identity remain mandatory; chmod is not a read action.
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1):
        raise EvidenceStoreError("evidence must be a current-user-owned, single-link regular file")
    return info


def _parts(path):
    path = Path(path).expanduser()
    if ".." in path.parts:
        raise EvidenceStoreError("evidence paths must not contain '..'")
    return path if path.is_absolute() else Path.cwd() / path


@contextlib.contextmanager
def root_fd(root):
    """Walk from / without resolving links, including every root ancestor."""
    _require()
    root = _parts(root)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in root.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        # The user's project is a shared ancestor, not a CodeTrail-private
        # state directory. Group-writable 0775 is an established project mode.
        if info.st_uid != os.geteuid():
            raise EvidenceStoreError("evidence project root must be owned by the current user")
        yield root, fd
    finally:
        os.close(fd)


def _read_at(fd, name, limit):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        before = _regular(handle)
        if before.st_size > limit:
            raise EvidenceStoreError("evidence exceeds bounded read limit")
        data = bytearray()
        while True:
            block = os.read(handle, min(1024 * 1024, limit + 1 - len(data)))
            if not block:
                break
            data.extend(block)
            if len(data) > limit:
                raise EvidenceStoreError("evidence grew beyond bounded read limit")
        after = os.fstat(handle)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise EvidenceStoreError("evidence changed during read")
        return bytes(data)
    finally:
        os.close(handle)


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise EvidenceStoreError("duplicate JSON key in evidence store")
        value[key] = item
    return value


def read_at(fd):
    try:
        data = _read_at(fd, "knowledge.json", MAX_JSON_BYTES)
    except FileNotFoundError:
        return {"metadata": {}, "chunks": []}
    try:
        kb = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(EvidenceStoreError("non-finite JSON")))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise EvidenceStoreError(f"invalid evidence store: {exc}") from exc
    if (not isinstance(kb, dict) or not isinstance(kb.get("chunks"), list)
            or not isinstance(kb.get("metadata"), dict)
            or any(not isinstance(chunk, dict) for chunk in kb["chunks"])):
        raise EvidenceStoreError("invalid knowledge store metadata/chunks")
    return kb


@contextlib.contextmanager
def locked_store(root, *, exclusive=False):
    """Use the existing KB flock inode. Read-only calls never create anything."""
    with root_fd(root) as (root_path, fd):
        flags = os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_RDWR | os.O_CREAT if exclusive else os.O_RDONLY)
        try:
            lock = os.open(".knowledge.json.lock", flags, 0o600, dir_fd=fd)
        except FileNotFoundError:
            lock = None
        try:
            if lock is not None:
                _regular(lock)
                fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            # Atomic JSON replacement is safe to read even before the first
            # writer creates a lock; no NPZ is consumed by these readers.
            yield root_path, fd
        finally:
            if lock is not None:
                fcntl.flock(lock, fcntl.LOCK_UN)
                os.close(lock)


def snapshot(root):
    with locked_store(root) as (_root, fd):
        return read_at(fd)


def fingerprint(kb):
    frozen = {"metadata": kb.get("metadata", {}), "chunks": [
        {key: value for key, value in chunk.items() if key not in {"embedding", "embedding_gate"}}
        for chunk in kb.get("chunks", [])]}
    return hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def source_digest(root, relative):
    """Hash a source through nofollow descriptors; never trust paths from JSON."""
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise EvidenceStoreError("missing or invalid relative source locator")
    path = Path(relative)
    if path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise EvidenceStoreError("source locator must be project-relative")
    with root_fd(root) as (_root, root_handle):
        fd = os.dup(root_handle)
        try:
            for part in path.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            handle = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                before = _regular(handle)
                if before.st_size > MAX_SOURCE_BYTES:
                    raise EvidenceStoreError("source exceeds bounded hashing limit")
                hasher = hashlib.sha256()
                count = 0
                while True:
                    block = os.read(handle, 1024 * 1024)
                    if not block:
                        break
                    count += len(block)
                    if count > MAX_SOURCE_BYTES:
                        raise EvidenceStoreError("source grew beyond bounded hashing limit")
                    hasher.update(block)
                after = os.fstat(handle)
                named = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
                identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
                if identity(before) != identity(after) or identity(after) != identity(named):
                    raise EvidenceStoreError("source changed during hashing")
                return hasher.hexdigest()
            finally:
                os.close(handle)
        finally:
            os.close(fd)


def anchored_json_path(fd):
    """Keep the JSON writer on the locked directory inode (Linux runtime)."""
    path = Path(f"/proc/self/fd/{fd}/knowledge.json")
    if not path.parent.is_dir():
        raise EvidenceStoreError("anchored KB writes require /proc/self/fd")
    return path
