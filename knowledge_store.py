"""Crash-safe persistence primitives for the JSON + NPZ knowledge store.

The JSON document is the metadata/source-of-truth for chunks and the NPZ file
holds row-aligned embeddings.  Every reader and writer in CodeTrail uses the
same lock.  Writers stage both files in their own destination directory, fsync
them, then replace the live pair while holding the lock.  A generation id, a
content hash and per-row chunk ids make a crash between the two replaces fail
loudly instead of mixing data.

NPZ 住在哪裡、怎麼被驗、什麼時候該重建，全部由 ``kb_cache`` 決定；這一層只負責
「把驗好的一對東西原子地換上去」。使用者只需要管 ``knowledge.json``。
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import math
import os
import shutil
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Iterator, Mapping


class KnowledgeStoreError(RuntimeError):
    """The on-disk knowledge store is incomplete or internally inconsistent."""


class DocumentIdentityConflict(KnowledgeStoreError):
    """兩個不同來源的檔案在 KB 內撞到同一個文件名稱。"""


# 文件在 KB 內的識別字串是 **basename**（`chunk["source"]` 與
# `metadata["documents"]` 都是 `Path(x).name`）。這個選擇讓 `remove_document("spec.pdf")`
# 之類的操作對使用者是直覺的，但它同時代表 `a/spec.pdf` 與 `b/spec.pdf` 在 KB 裡
# 是同一個身分——後灌的那份會把前一份整份換掉，而訊息與正常更新一字不差。
#
# `metadata["document_sources"]` 記下每份文件的來源身分（解析後的絕對路徑，或 URL），
# 入庫前比對。對不上就 fail-loud：靜默覆蓋的後果是查詢照樣回答，只是答的是別份文件。
DOCUMENT_SOURCES_KEY = "document_sources"


def _document_sources(metadata: Mapping) -> dict:
    raw = metadata.get(DOCUMENT_SOURCES_KEY)
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(k, str) and v}


def check_document_identity(
    metadata: Mapping, doc_name: str, identity: str
) -> str | None:
    """入庫前比對文件身分。撞名且來源不同 → `DocumentIdentityConflict`。

    回傳非 None 時是給呼叫端印的警告字串（不是錯誤）。

    三種情形：
      1. `identity` 是空的（呼叫端給不出來源）→ 不擋、不記，維持既有語意。
      2. KB 沒有這個名字的紀錄 → 放行（新文件，或舊 KB 還沒有 `document_sources`）。
         後者只能警告：舊 KB 沒有留下來源，這時候猜「是同一個檔」或猜「不是」
         都會錯一半，而錯的那一半是靜默的。下一次入庫就有紀錄可比。
      3. 有紀錄且不同 → raise。
    """
    if not identity:
        return None
    recorded = _document_sources(metadata).get(doc_name)
    if recorded is None:
        documents = metadata.get("documents") or []
        if doc_name in documents:
            return (
                f"知識庫裡已經有一份 '{doc_name}'，但舊版沒有記錄它的來源檔，"
                f"無法確認這次是不是撞名（同名不同來源）。"
                f"這一次採用 {identity}；如果它們其實是兩份不同的文件，"
                f"請先把其中一份改名再重新入庫。"
            )
        return None
    if recorded == identity:
        return None
    raise DocumentIdentityConflict(
        f"知識庫裡已經有一份叫 '{doc_name}' 的文件，但它來自別的位置：\n"
        f"  已在 KB：{recorded}\n"
        f"  這一次：  {identity}\n"
        f"KB 用檔名（basename）當文件識別，直接寫下去會把前一份整份換掉，"
        f"而且訊息跟正常更新一模一樣——查詢仍然會回答，只是答的是另一份文件。\n"
        f"三種處理方式：\n"
        f"  1. 這兩個路徑其實是同一份文件（檔案搬過位置）："
        f"先 remove_document(\"{doc_name}\") 再重新 ingest；\n"
        f"  2. 兩份都要留：先把其中一份改名，再 ingest；\n"
        f"  3. 要用這一份重建整個 KB：加 --fresh。"
    )


def record_document_identity(metadata: dict, doc_name: str, identity: str) -> None:
    """記下這份文件的來源身分。`identity` 是空的就什麼都不做。"""
    if not identity:
        return
    sources = _document_sources(metadata)
    sources[doc_name] = identity
    metadata[DOCUMENT_SOURCES_KEY] = sources


def forget_document_identity(metadata: dict, doc_name: str) -> None:
    """移除一份文件的身分紀錄（`remove_document` 用）。

    不清的話身分紀錄會變成刪不掉的墓碑：使用者照著錯誤訊息刪了文件，
    再灌同名的另一份還是被擋。
    """
    sources = _document_sources(metadata)
    if sources.pop(doc_name, None) is not None:
        metadata[DOCUMENT_SOURCES_KEY] = sources


def sync_document_identities(metadata: dict) -> None:
    """把身分紀錄對齊 `documents`：不在文件清單裡的一律移除。

    放在提交路徑上，讓每一個 writer（ingest / remove / fresh）都自動收斂，
    不必各自記得清。
    """
    sources = _document_sources(metadata)
    if not sources:
        metadata.pop(DOCUMENT_SOURCES_KEY, None)
        return
    documents = {str(name) for name in (metadata.get("documents") or [])}
    kept = {name: value for name, value in sources.items() if name in documents}
    if kept:
        metadata[DOCUMENT_SOURCES_KEY] = kept
    else:
        metadata.pop(DOCUMENT_SOURCES_KEY, None)


def chunk_id(chunk: Mapping) -> str:
    """KB chunk 的 id 組法（**單一實作**）。

    格式凍結為 ``f"{source}::p{page}::c{chunk_index}::{md5(content)[:8]}"``——與
    ``RAG._commit_document_to_kb`` 原本的行內字面值逐字相同。人工修正 figure 時要重算
    id，兩處各寫一份公式必然漂移（而漂移是靜默的：id 只是識別字串，錯了不會有人喊），
    所以組法放在 store 層由兩邊共用。

    **缺任一身分欄位或型別不對一律 fail-loud**（``KnowledgeStoreError``）。id 是
    chunk 在 KB 內的身分：把缺 ``source`` 的 chunk 補成空字串，會讓兩個不同來源的
    chunk 拿到同一個 id，後續的去重與人工修正就會覆寫到錯的東西。舊碼直接
    ``chunk['source']`` / ``chunk['content'].encode()``，缺欄位本來就會 KeyError /
    AttributeError——這裡維持同一條界線，只是換成訊息說得清楚的例外。
    """
    for name, types in (("source", str), ("page", int),
                        ("chunk_index", int), ("content", str)):
        if name not in chunk:
            raise KnowledgeStoreError(
                f"chunk 缺少身分欄位 {name!r}，無法產生 id"
                f"（現有欄位：{sorted(chunk)[:10]}）；補預設值會讓不同 chunk 撞 id"
            )
        value = chunk[name]
        if isinstance(value, bool) or not isinstance(value, types):
            raise KnowledgeStoreError(
                f"chunk 的 {name!r} 必須是 {types.__name__}，收到 "
                f"{type(value).__name__}({value!r})"
            )
    source = chunk["source"]
    if not source:
        raise KnowledgeStoreError("chunk 的 'source' 不得是空字串——id 會失去來源身分")
    page = chunk["page"]
    chunk_index = chunk["chunk_index"]
    if page < 0 or chunk_index < 0:
        raise KnowledgeStoreError(
            f"chunk 的 page={page} / chunk_index={chunk_index} 不得為負"
        )
    digest = hashlib.md5(chunk["content"].encode("utf-8")).hexdigest()[:8]
    return f"{source}::p{page}::c{chunk_index}::{digest}"


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


def validate_embeddings(
    chunks: list[dict], *, key: str = "embedding"
) -> tuple[list[list[float]], int | None]:
    """Return a plain, dimension-consistent matrix without changing ``chunks``.

    ``key`` 選要驗哪一套向量:``embedding`` 是 retrieval(可能含生成脈絡),
    ``embedding_gate`` 是 content-only 的決策訊號。兩套各自驗維度一致性——
    混維度代表模型/快取混用,補零或截斷都是靜默毀損。
    """
    if not chunks:
        return [], None

    rows: list[list[float]] = []
    dimensions: set[int] = set()
    for index, chunk in enumerate(chunks):
        raw = chunk.get(key)
        if raw is None or len(raw) == 0:
            raise KnowledgeStoreError(
                f"chunk {index} ({chunk.get('source', '?')}) is missing its {key}; "
                "refusing to write a partial knowledge store"
            )
        try:
            row = [float(value) for value in raw]
        except (TypeError, ValueError) as exc:
            raise KnowledgeStoreError(f"chunk {index} has a non-numeric {key}") from exc
        if not row or not all(math.isfinite(value) for value in row):
            raise KnowledgeStoreError(f"chunk {index} has an empty or non-finite {key}")
        dimensions.add(len(row))
        rows.append(row)

    if len(dimensions) != 1:
        raise KnowledgeStoreError(
            f"{key} dimension mismatch: found {sorted(dimensions)}; "
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


def _normalized_matrix(rows: list[list[float]], label: str):
    import numpy as np

    matrix = np.asarray(rows, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(norms <= 0):
        raise KnowledgeStoreError(f"zero-norm {label} detected; refusing to persist it")
    return matrix / norms


def _write_npz_temp(
    directory: Path,
    basename: str,
    rows: list[list[float]],
    *,
    embedding_model: str,
    content_hash: str,
    content_hash_schema: str,
    generation: str,
    chunk_ids: list[str],
    dir_fd: int | None = None,
    gate_rows: list[list[float]] | None = None,
    gate_content_hash: str = "",
    gate_content_hash_schema: str = "",
    section_fields: Mapping | None = None,
):
    """Stage all row spaces in one NPZ, so publishing is one rename.

    `embeddings` 是 retrieval 訊號(可能含生成脈絡),`embeddings_gate` 是
    content-only 的決策訊號。兩組各帶自己的 schema / hash / 維度 / 列數,
    同一個 store_generation 下一次提交——分兩個檔就會有「只換到一半」的視窗。

    `chunk_ids` 是**逐列**的 chunk 身分。內容雜湊只證明「這批 chunk 的集合沒變」,
    證明不了「第 i 列就是第 i 個 chunk 的向量」;少了它,載入端就只能靠陣列順序
    配對,而錯位一列的後果是查詢照常回答、只是答錯。
    """
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - numpy is an optional runtime dependency
        raise KnowledgeStoreError(
            "numpy is required to persist external knowledge embeddings"
        ) from exc

    matrix = _normalized_matrix(rows, "embedding")
    if len(chunk_ids) != matrix.shape[0]:
        raise KnowledgeStoreError(
            f"chunk id 數量與向量列數不符: {len(chunk_ids)} vs {matrix.shape[0]}"
        )
    payload = {
        "embeddings": matrix,
        "embedding_model": embedding_model,
        "embedding_dimension": matrix.shape[1],
        "chunk_count": matrix.shape[0],
        "content_hash": content_hash,
        "content_hash_schema": content_hash_schema,
        "store_generation": generation,
        "chunk_ids": np.array(list(chunk_ids)),
    }

    if gate_rows is not None:
        gate_matrix = _normalized_matrix(gate_rows, "gate embedding")
        if gate_matrix.shape[0] != matrix.shape[0]:
            raise KnowledgeStoreError(
                "gate matrix row count does not match the retrieval matrix: "
                f"{gate_matrix.shape[0]} vs {matrix.shape[0]}"
            )
        payload.update(
            embeddings_gate=gate_matrix,
            gate_embedding_dimension=gate_matrix.shape[1],
            gate_chunk_count=gate_matrix.shape[0],
            gate_content_hash=gate_content_hash,
            gate_content_hash_schema=gate_content_hash_schema,
        )
    if section_fields is not None:
        # Already validated against the exact JSON chunks by the public
        # writer.  All three row spaces share this one generation/rename.
        payload.update(section_fields)

    if dir_fd is not None:
        # 持有已驗證目錄 fd 的版本：staging 也一律相對操作，不留任何用路徑
        # 再開一次的機會（那正是 TOCTOU 的來源）。回傳的是相對檔名。
        name = f".{basename}.tmp.{os.getpid()}.{os.urandom(6).hex()}"
        handle_fd = os.open(
            name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600, dir_fd=dir_fd,
        )
        try:
            with os.fdopen(handle_fd, "wb") as handle:
                np.savez_compressed(handle, **payload)
                handle.flush()
                os.fsync(handle.fileno())
            return name
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=dir_fd)
            raise

    fd, raw_path = tempfile.mkstemp(prefix=f".{basename}.tmp.", dir=directory)
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as handle:
            np.savez_compressed(handle, **payload)
            handle.flush()
            os.fsync(handle.fileno())
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


class _EmbeddingSlot:
    """向量檔的四個動作（存在？備份、替換、刪除）——有 dir_fd 就一律走相對操作。

    路徑版留著給沒有傳 fd 的呼叫端；行為逐字相同，差別只在「動到的是路徑還是
    開 fd 當下驗過的那個 inode」。
    """

    def __init__(self, path: Path, dir_fd: int | None):
        self.path = path
        self.name = path.name
        self.fd = dir_fd

    _NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

    def exists(self) -> bool:
        if self.fd is None:
            return self.path.exists()
        try:
            os.lstat(self.name, dir_fd=self.fd)
            return True
        except FileNotFoundError:
            return False

    def _assert_regular(self) -> None:
        """既有的向量檔必須是普通檔案。

        `os.link()` 預設 `follow_symlinks=True`:向量檔在前置檢查之後被換成指向
        sandbox 外的 symlink 時,hardlink 快路徑會把**外部檔案**連進 cache 目錄
        (後面的 `_copy_at` 雖然有 O_NOFOLLOW,但 hardlink 成功就不會走到它)。
        這個目錄樹由 CodeTrail 自動產生,不該有連結——看到就 fail-loud。
        """
        info = (os.lstat(self.name, dir_fd=self.fd) if self.fd is not None
                else self.path.lstat())
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise KnowledgeStoreError(
                f"拒絕備份 {self.path}：它不是普通檔案（symlink 或裝置檔？）。"
                "這個目錄樹由 CodeTrail 自動產生，不應該有連結。"
            )

    def backup(self, generation: str) -> str | None:
        """既有向量檔的回滾點。檔案不存在回 None；**做不出備份一律 raise**。

        路徑版的 `_backup_link` 在 hardlink 失敗時會 fallback 去複製；dir_fd 版
        以前直接回 None，於是「備份失敗」看起來跟「本來就沒有舊檔」一樣——之後
        JSON 發布失敗要回滾時，會把新 cache 刪掉卻沒有舊的可以還原，舊向量就這樣
        無聲消失。沒有回滾點就不准開始替換。
        """
        if self.fd is None:
            if not self.path.exists():
                return None
            self._assert_regular()
            backup = _backup_link(self.path, generation)
            return str(backup) if backup else None
        if not self.exists():
            return None
        self._assert_regular()
        name = f".{self.name}.rollback.{generation}"
        try:
            # `follow_symlinks=False` 是第二道（`_assert_regular` 之後才被換掉的
            # 競態）：linkat 不帶 AT_SYMLINK_FOLLOW 時，對 symlink 會直接 EPERM。
            os.link(self.name, name, src_dir_fd=self.fd, dst_dir_fd=self.fd,
                    follow_symlinks=False)
            return name
        except (OSError, NotImplementedError):
            pass   # 跨檔案系統 / 不支援 hardlink → 改用 fd 安全複製
        try:
            self._copy_at(self.name, name)
        except OSError as exc:
            raise KnowledgeStoreError(
                f"無法備份既有的向量檔 {self.name}: {exc}；"
                "拒絕在沒有回滾點的情況下替換它"
            ) from exc
        return name

    def _copy_at(self, src: str, dst: str) -> None:
        """以 dir_fd + O_NOFOLLOW 複製一份備份（失敗時不留半個檔）。"""
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(src, os.O_RDONLY | nofollow, dir_fd=self.fd)
        try:
            target_fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY | nofollow,
                                0o600, dir_fd=self.fd)
        except OSError:
            os.close(source_fd)
            raise
        try:
            with os.fdopen(source_fd, "rb") as source, os.fdopen(target_fd, "wb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(dst, dir_fd=self.fd)
            raise

    def restore(self, backup) -> None:
        if self.fd is None:
            if backup and Path(backup).exists():
                os.replace(Path(backup), self.path)
            else:
                self.path.unlink(missing_ok=True)
            return
        if backup and self.exists_name(backup):
            os.replace(backup, self.name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        else:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.name, dir_fd=self.fd)

    def exists_name(self, name: str) -> bool:
        try:
            os.lstat(name, dir_fd=self.fd)
            return True
        except (FileNotFoundError, TypeError):
            return False

    def drop_backup(self, backup) -> None:
        if not backup:
            return
        if self.fd is None:
            Path(backup).unlink(missing_ok=True)
        else:
            with contextlib.suppress(OSError):
                os.unlink(backup, dir_fd=self.fd)

    def remove(self) -> None:
        if self.fd is None:
            self.path.unlink(missing_ok=True)
        else:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.name, dir_fd=self.fd)


def _default_chunk_ids(chunks: list[dict]) -> list[str]:
    """呼叫端沒給逐列身分時的預設來源。

    late import 避開 import 期循環（``kb_cache`` 是 import 這個模組的那一邊）。
    刻意**不**用 ``chunk_id()``：那是 fail-loud 的身分產生器，對缺 page /
    chunk_index 的舊 chunk 會 raise，而「寫不出 cache」不該是缺欄位的懲罰。
    """
    from kb_cache import chunk_row_ids

    return chunk_row_ids(chunks)


def _backup_link(path: Path, generation: str) -> Path | None:
    """回滾點（路徑版，給 JSON 與沒有 dir fd 的呼叫端用）。

    `follow_symlinks=False` 是刻意的：`os.link` 預設會跟著 symlink 走，把連結指到
    的**別的檔案**連進來當成「舊版備份」。linkat 不帶 AT_SYMLINK_FOLLOW 對 symlink
    會 EPERM，於是退回 `copy2`；`copy2` 也失敗就往上拋（沒有回滾點就不該開始替換）。
    """
    if not path.exists():
        return None
    backup = path.with_name(f".{path.name}.rollback.{generation}")
    try:
        os.link(path, backup, follow_symlinks=False)
    except (OSError, NotImplementedError):
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
    embedding_file: str | Path,
    embedding_model: str,
    content_hash: str,
    content_hash_schema: str,
    chunk_ids: list[str] | None = None,
    embedding_dir_fd: int | None = None,
    legacy_companion: Path | None = None,
    gate_content_hash: str | None = None,
    gate_content_hash_schema: str | None = None,
    section_fields: Mapping | None = None,
    already_locked: bool = False,
) -> tuple[Path, Path | None]:
    """Atomically publish a validated JSON/NPZ pair.

    The caller's dict and chunk embeddings are never mutated.  Readers holding
    ``knowledge_store_lock`` cannot observe the pair between replacements.

    給了 ``gate_content_hash_schema`` 就同時寫出 content-only 的 gate 矩陣
    (取自每個 chunk 的 ``embedding_gate``)。沒給就只有單一矩陣——沒有任何
    ctx 的 KB 用不到第二套向量,retrieval 與 gate 本來就是同一組字。

    ``embedding_file`` 可以是絕對路徑(現在的向量放在隱藏 cache 目錄,不再與
    JSON 同目錄)。暫存檔一律 staging 在**向量檔自己的目錄**裡,`os.replace`
    才保證是同一個檔案系統上的原子替換。

    ``legacy_companion`` 給的話,提交成功後會把舊版本留在 JSON 旁邊的
    companion NPZ 刪掉——它是可重建資料,留著只會讓使用者以為「刪了 JSON
    知識庫還在」。

    ``embedding_dir_fd`` 給的話,向量檔的 staging / 備份 / 替換 / 刪除全部改用
    `dir_fd` 相對操作(呼叫端負責持有那個已驗證的 fd)。路徑先檢查再用是
    check-then-use:`.codetrail` 在檢查之後被換成指向 sandbox 外的 symlink,
    NDA 向量就寫到外面去了。持有 fd 等於釘住驗過的 inode,路徑之後怎麼換都
    影響不到這裡。沒給就走舊的 path-based 版本(其他呼叫端與測試不受影響)。

    ``section_fields`` 是鎖外已準備好的章節矩陣與身分；本層只驗證、從不計算
    embedding。None 保留直接低階 caller 明示寫舊格式的能力，載入端仍會把
    缺 section schema 的 cache 判 stale，不會因這個 optional 參數放寬驗證。
    """
    json_path = Path(json_path)
    emb_path = json_path.parent / Path(embedding_file)
    chunks = list(kb.get("chunks", []))
    rows, dimension = validate_embeddings(chunks)
    with_gate = gate_content_hash_schema is not None
    gate_rows: list[list[float]] | None = None
    gate_dimension: int | None = None
    if with_gate:
        gate_rows, gate_dimension = validate_embeddings(chunks, key="embedding_gate")
        if gate_dimension is not None and dimension is not None and gate_dimension != dimension:
            raise KnowledgeStoreError(
                "gate embedding dimension does not match the retrieval matrix: "
                f"{gate_dimension} vs {dimension}"
            )
    if section_fields is not None:
        import kb_cache

        section_fields = copy.deepcopy(dict(section_fields))
        if set(section_fields) != set(kb_cache.SECTION_NPZ_FIELDS):
            raise KnowledgeStoreError("section fields must contain exactly the section cache schema")
        reason = kb_cache._verify_section_fields(
            section_fields, chunks=chunks, dimension=dimension or 0)
        if reason:
            raise KnowledgeStoreError(reason)
    generation = uuid.uuid4().hex

    metadata = copy.deepcopy(kb.get("metadata", {}))
    metadata["embedding_model"] = embedding_model
    metadata["embedding_dimension"] = dimension
    metadata["embedding_content_hash"] = content_hash
    metadata["embedding_content_hash_schema"] = content_hash_schema
    metadata["store_generation"] = generation
    metadata["total_chunks"] = len(chunks)
    metadata["total_documents"] = len(metadata.get("documents", []))
    # 每一個 writer 都經過這裡：身分紀錄在提交當下對齊文件清單，
    # 不必指望 ingest / remove / fresh 各自記得清。
    sync_document_identities(metadata)
    if with_gate:
        metadata["gate_embedding_dimension"] = gate_dimension
        metadata["gate_embedding_content_hash"] = gate_content_hash or ""
        metadata["gate_embedding_content_hash_schema"] = gate_content_hash_schema
    else:
        # 上一代 KB 有 gate 而這次沒有時，殘留的 metadata 會讓載入端誤以為
        # NPZ 裡還有第二套矩陣。
        for stale in (
            "gate_embedding_dimension",
            "gate_embedding_content_hash",
            "gate_embedding_content_hash_schema",
        ):
            metadata.pop(stale, None)
    json_chunks = []
    for chunk in chunks:
        clean = copy.deepcopy(chunk)
        # 任何向量都不 inline 進 JSON：體積之外，兩份會各自漂移。
        clean.pop("embedding", None)
        clean.pop("embedding_gate", None)
        json_chunks.append(clean)
    payload = {"metadata": metadata, "chunks": json_chunks}

    @contextlib.contextmanager
    def maybe_lock():
        if already_locked:
            yield
        else:
            with knowledge_store_lock(json_path, exclusive=True):
                yield

    slot = _EmbeddingSlot(emb_path, embedding_dir_fd)
    json_tmp: Path | None = None
    emb_tmp = None            # path-based 是 Path，dir_fd 版是相對檔名 str
    json_backup: Path | None = None
    emb_backup = None
    json_replaced = False
    emb_replaced = False
    try:
        with maybe_lock():
            try:
                json_tmp = _write_json_temp(json_path.parent, json_path.name, payload)
                if rows:
                    if embedding_dir_fd is None:
                        emb_path.parent.mkdir(parents=True, exist_ok=True)
                    emb_tmp = _write_npz_temp(
                        emb_path.parent,
                        emb_path.name,
                        rows,
                        dir_fd=embedding_dir_fd,
                        embedding_model=embedding_model,
                        content_hash=content_hash,
                        content_hash_schema=content_hash_schema,
                        generation=generation,
                        chunk_ids=list(chunk_ids if chunk_ids is not None
                                       else _default_chunk_ids(chunks)),
                        gate_rows=gate_rows,
                        gate_content_hash=gate_content_hash or "",
                        gate_content_hash_schema=gate_content_hash_schema or "",
                        section_fields=section_fields,
                    )

                json_backup = _backup_link(json_path, generation)
                emb_backup = slot.backup(generation)

                if emb_tmp is not None:
                    if embedding_dir_fd is None:
                        os.replace(emb_tmp, emb_path)
                    else:
                        os.replace(emb_tmp, slot.name,
                                   src_dir_fd=embedding_dir_fd,
                                   dst_dir_fd=embedding_dir_fd)
                        os.fsync(embedding_dir_fd)
                    emb_tmp = None
                    emb_replaced = True
                elif slot.exists():
                    slot.remove()
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
                    slot.restore(emb_backup)
                _fsync_directory(json_path.parent)
                raise

            if json_backup:
                json_backup.unlink(missing_ok=True)
            slot.drop_backup(emb_backup)
            # 提交成功之後才動舊位置那份:回滾路徑已經走完,不會出現「舊 JSON
            # 還在、它的 companion 卻被刪掉」的狀態。
            if legacy_companion is not None:
                with contextlib.suppress(OSError):
                    Path(legacy_companion).unlink(missing_ok=True)
            return json_path, emb_path if rows else None
    finally:
        if json_tmp:
            json_tmp.unlink(missing_ok=True)
        if emb_tmp is not None:
            if embedding_dir_fd is None:
                Path(emb_tmp).unlink(missing_ok=True)
            else:
                with contextlib.suppress(OSError):
                    os.unlink(emb_tmp, dir_fd=embedding_dir_fd)
        if json_backup:
            json_backup.unlink(missing_ok=True)
        slot.drop_backup(emb_backup)
