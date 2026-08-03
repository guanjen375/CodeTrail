"""Crash-safe persistence primitives for the JSON + NPZ knowledge store.

The JSON document is the metadata/source-of-truth for chunks and the NPZ file
holds row-aligned embeddings.  Every reader and writer in CodeTrail uses the
same lock.  Writers stage both files in the destination directory, fsync them,
then replace the live pair while holding the lock.  A generation id and content
hash make a crash between the two replaces fail loudly instead of mixing data.
"""
from __future__ import annotations

import contextlib
import copy
import json
import math
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Iterator


class KnowledgeStoreError(RuntimeError):
    """The on-disk knowledge store is incomplete or internally inconsistent."""


def _lock_path(json_path: Path) -> Path:
    return json_path.with_name(f".{json_path.name}.lock")


@contextlib.contextmanager
def knowledge_store_lock(json_path: Path, *, exclusive: bool) -> Iterator[None]:
    """Lock a knowledge store across processes.

    Unix uses flock shared/exclusive modes.  Windows' stdlib locking primitive
    has no shared mode, so reads also take the exclusive one-byte lock there.
    """
    json_path = Path(json_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(json_path)
    handle = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(handle.fileno(), mode)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def validate_embeddings(chunks: list[dict]) -> tuple[list[list[float]], int | None]:
    """Return a plain, dimension-consistent matrix without changing ``chunks``."""
    if not chunks:
        return [], None

    rows: list[list[float]] = []
    dimensions: set[int] = set()
    for index, chunk in enumerate(chunks):
        raw = chunk.get("embedding")
        if raw is None or len(raw) == 0:
            raise KnowledgeStoreError(
                f"chunk {index} ({chunk.get('source', '?')}) is missing its embedding; "
                "refusing to write a partial knowledge store"
            )
        try:
            row = [float(value) for value in raw]
        except (TypeError, ValueError) as exc:
            raise KnowledgeStoreError(f"chunk {index} has a non-numeric embedding") from exc
        if not row or not all(math.isfinite(value) for value in row):
            raise KnowledgeStoreError(f"chunk {index} has an empty or non-finite embedding")
        dimensions.add(len(row))
        rows.append(row)

    if len(dimensions) != 1:
        raise KnowledgeStoreError(
            f"embedding dimension mismatch: found {sorted(dimensions)}; "
            "zero-padding/truncation is forbidden, rebuild with one embedding model"
        )
    return rows, dimensions.pop()


def _write_json_temp(directory: Path, basename: str, payload: dict) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix=f".{basename}.tmp.", dir=directory)
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _write_npz_temp(
    directory: Path,
    basename: str,
    rows: list[list[float]],
    *,
    embedding_model: str,
    content_hash: str,
    content_hash_schema: str,
    generation: str,
) -> Path:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - numpy is an optional runtime dependency
        raise KnowledgeStoreError(
            "numpy is required to persist external knowledge embeddings"
        ) from exc

    matrix = np.asarray(rows, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(norms <= 0):
        raise KnowledgeStoreError("zero-norm embedding detected; refusing to persist it")
    matrix = matrix / norms

    fd, raw_path = tempfile.mkstemp(prefix=f".{basename}.tmp.", dir=directory)
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as handle:
            np.savez_compressed(
                handle,
                embeddings=matrix,
                embedding_model=embedding_model,
                embedding_dimension=matrix.shape[1],
                chunk_count=matrix.shape[0],
                content_hash=content_hash,
                content_hash_schema=content_hash_schema,
                store_generation=generation,
            )
            handle.flush()
            os.fsync(handle.fileno())
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _backup_link(path: Path, generation: str) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name(f".{path.name}.rollback.{generation}")
    try:
        os.link(path, backup)
    except OSError:
        shutil.copy2(path, backup)
    return backup


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_knowledge_store_atomic(
    kb: dict,
    json_path: Path,
    *,
    embedding_file: str,
    embedding_model: str,
    content_hash: str,
    content_hash_schema: str,
    already_locked: bool = False,
) -> tuple[Path, Path | None]:
    """Atomically publish a validated JSON/NPZ pair.

    The caller's dict and chunk embeddings are never mutated.  Readers holding
    ``knowledge_store_lock`` cannot observe the pair between replacements.
    """
    json_path = Path(json_path)
    emb_path = json_path.parent / embedding_file
    chunks = list(kb.get("chunks", []))
    rows, dimension = validate_embeddings(chunks)
    generation = uuid.uuid4().hex

    metadata = copy.deepcopy(kb.get("metadata", {}))
    metadata["embedding_model"] = embedding_model
    metadata["embedding_dimension"] = dimension
    metadata["embedding_content_hash"] = content_hash
    metadata["embedding_content_hash_schema"] = content_hash_schema
    metadata["store_generation"] = generation
    metadata["total_chunks"] = len(chunks)
    metadata["total_documents"] = len(metadata.get("documents", []))
    json_chunks = []
    for chunk in chunks:
        clean = copy.deepcopy(chunk)
        clean.pop("embedding", None)
        json_chunks.append(clean)
    payload = {"metadata": metadata, "chunks": json_chunks}

    @contextlib.contextmanager
    def maybe_lock():
        if already_locked:
            yield
        else:
            with knowledge_store_lock(json_path, exclusive=True):
                yield

    json_tmp: Path | None = None
    emb_tmp: Path | None = None
    json_backup: Path | None = None
    emb_backup: Path | None = None
    json_replaced = False
    emb_replaced = False
    try:
        with maybe_lock():
            try:
                json_tmp = _write_json_temp(json_path.parent, json_path.name, payload)
                if rows:
                    emb_tmp = _write_npz_temp(
                        json_path.parent,
                        emb_path.name,
                        rows,
                        embedding_model=embedding_model,
                        content_hash=content_hash,
                        content_hash_schema=content_hash_schema,
                        generation=generation,
                    )

                json_backup = _backup_link(json_path, generation)
                emb_backup = _backup_link(emb_path, generation)

                if emb_tmp is not None:
                    os.replace(emb_tmp, emb_path)
                    emb_tmp = None
                    emb_replaced = True
                elif emb_path.exists():
                    emb_path.unlink()
                    emb_replaced = True

                os.replace(json_tmp, json_path)
                json_tmp = None
                json_replaced = True
                _fsync_directory(json_path.parent)
            except Exception:
                # Roll back before releasing the store lock.  A reader can
                # therefore observe either generation, never a mixed pair.
                if json_replaced:
                    if json_backup and json_backup.exists():
                        os.replace(json_backup, json_path)
                    else:
                        json_path.unlink(missing_ok=True)
                if emb_replaced:
                    if emb_backup and emb_backup.exists():
                        os.replace(emb_backup, emb_path)
                    else:
                        emb_path.unlink(missing_ok=True)
                _fsync_directory(json_path.parent)
                raise

            if json_backup:
                json_backup.unlink(missing_ok=True)
            if emb_backup:
                emb_backup.unlink(missing_ok=True)
            return json_path, emb_path if rows else None
    finally:
        if json_tmp:
            json_tmp.unlink(missing_ok=True)
        if emb_tmp:
            emb_tmp.unlink(missing_ok=True)
        if json_backup:
            json_backup.unlink(missing_ok=True)
        if emb_backup:
            emb_backup.unlink(missing_ok=True)
