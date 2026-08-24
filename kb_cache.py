"""embeddings cache 的唯一擁有者：路徑、身分驗證、重建、清除、舊格式遷移。

設計前提只有一句話：

    ``knowledge.json`` 是使用者唯一需要理解、備份、複製、刪除的 KB 檔案；
    向量是**程式自管的可重建 cache**，使用者不需要知道它在哪裡。

以前 JSON 與 ``knowledge_emb.npz`` 是一對必須手動配對的檔案：使用者刪掉 JSON
之後 NPZ 還躺在同一個目錄裡，看起來像「知識庫還在」；把別份 JSON 複製過來覆蓋，
兩邊的 chunk 數剛好一樣時甚至查得動——查得動而且答錯，沒有任何一行 log 會講。

現在：

* 向量放 ``<kb 目錄>/.codetrail/cache/embeddings/<kb-id>/embeddings.npz``，
  跟 ``.codetrail/figures`` 同一個既有慣例（整個 ``.codetrail/`` 已在 .gitignore）。
* ``<kb-id>`` 取自 **JSON 檔名**（不是絕對路徑）：同一個目錄放兩份 KB 不會互相
  覆蓋，整個目錄搬家時 cache 仍跟著有效。
* cache 自帶完整身分：embedding model、store generation、內容雜湊（含 schema）、
  chunk 數、矩陣 shape/維度，以及**逐列的 chunk id**。少任何一項對不上就丟棄重建，
  絕不「反正列數一樣」湊合著用。
* 重建失敗（例如 embedding server 連不上）→ fail-loud，**不准沿用舊向量**。

### 硬違規 vs 可重建

不是所有不一致都能靠重建救回來，兩類要分清楚：

* **可重建（stale）**：cache 不存在、model / generation / 內容雜湊 / chunk id /
  列數 / 維度對不上。這些都只是「這份 cache 不是這份 JSON 的」，重算即可。
* **硬違規（raise）**：schema 不在現行白名單、有 ctx 的 KB 卻沒有 gate 矩陣、
  gate 的 schema / shape / 雜湊不符。這些是 KB 契約本身被破壞（雙訊號那條
  「決策向量不得含生成脈絡」的線），不能靠重算掩蓋過去，必須讓人看到。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

import context_signals
from knowledge_store import KnowledgeStoreError

try:
    from config import EMBEDDING_MODEL, KNOWLEDGE_EMB_FILE
except ImportError:  # pragma: no cover - 獨立執行時的預設值，與 RAG.py 一致
    EMBEDDING_MODEL = "bge-m3"
    KNOWLEDGE_EMB_FILE = "knowledge_emb.npz"

# `.codetrail/` 已經是這個 repo 放衍生資料的地方（figures 在隔壁），而且整棵
# 都在 .gitignore 裡——NDA 內容不會因為換位置而外洩。
CACHE_RELDIR = Path(".codetrail") / "cache" / "embeddings"
CACHE_FILENAME = "embeddings.npz"

MSG_REBUILD = "[INFO] embeddings cache 不存在或已過期，正在依 knowledge.json 重建。"
MSG_PURGED = "[INFO] knowledge.json 不存在，KB 視為空；已清除無主 embeddings cache。"
MSG_FATAL = "[FATAL] embeddings cache 無法重建，未使用舊向量；查詢已中止。"


# ==========================================================================
# 路徑
# ==========================================================================
def kb_id(json_path) -> str:
    """KB 在 cache 目錄裡的身分。

    只吃**檔名**，不吃絕對路徑：使用者把整個專案目錄搬走 / 改名時，cache 仍然
    是對的（身分驗證本來就綁在檔案內容上，不綁位置）；而同一個目錄放兩份 KB
    時檔名不同，也就不會互相覆蓋。
    """
    name = Path(json_path).name
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).stem).strip("-.") or "kb"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]
    return f"{slug[:48]}-{digest}"


def cache_dir(json_path) -> Path:
    return Path(json_path).parent / CACHE_RELDIR / kb_id(json_path)


def cache_file(json_path) -> Path:
    return cache_dir(json_path) / CACHE_FILENAME


def legacy_companion(json_path) -> Path:
    """舊版本放在 JSON 旁邊的 companion NPZ（使用者看得到的那份）。"""
    return Path(json_path).parent / KNOWLEDGE_EMB_FILE


# ==========================================================================
# 逐列身分
# ==========================================================================
def chunk_row_ids(chunks: Sequence[Mapping]) -> list[str]:
    """每一列向量對應的穩定 chunk 識別。

    優先用 chunk 自己的 ``id``（``knowledge_store.chunk_id`` 的產物）。舊 KB /
    手寫 KB 可能沒有 id，甚至缺 page / chunk_index，所以 fallback **不呼叫**
    ``chunk_id()``（那會 raise）——改用身分欄位的摘要。它只要滿足「同一個 chunk
    永遠算出同一個值、不同 chunk 幾乎不可能撞」就夠了。
    """
    ids: list[str] = []
    for chunk in chunks:
        raw = chunk.get("id")
        if isinstance(raw, str) and raw:
            ids.append(raw)
            continue
        payload = json.dumps(
            [
                str(chunk.get("source") or ""),
                chunk.get("page"),
                chunk.get("chunk_index"),
                chunk.get("content") or "",
            ],
            ensure_ascii=False,
            default=str,
        )
        ids.append("sha1:" + hashlib.sha1(payload.encode("utf-8")).hexdigest())
    return ids


# ==========================================================================
# 清除（cache 是可重建資料，figure artifacts 不是——這裡永遠不碰 figures）
# ==========================================================================
def purge(json_path, *, announce: bool = False) -> list[str]:
    """刪掉這份 KB 的 cache 與舊位置 companion NPZ；回傳實際刪掉的路徑。

    刻意**不建立任何目錄**：`--preflight` 之類的零寫入路徑也會走到這裡，
    長出一個空的 `.codetrail/` 就會讓「零寫入」的斷言變成謊話。
    """
    removed: list[str] = []
    directory = cache_dir(json_path)
    if directory.is_dir():
        shutil.rmtree(directory, ignore_errors=True)
        if not directory.exists():
            removed.append(str(directory))
    legacy = legacy_companion(json_path)
    if legacy.is_file():
        with contextlib.suppress(OSError):
            legacy.unlink()
            removed.append(str(legacy))
    # 空掉的 embeddings/ 與 cache/ 一併收乾淨，但 `.codetrail/` 本身留著
    # （figures 住在那裡）。
    for parent in (directory.parent, directory.parent.parent):
        with contextlib.suppress(OSError):
            parent.rmdir()
    if removed and announce:
        print(MSG_PURGED)
    return removed


# ==========================================================================
# 讀取與驗證
# ==========================================================================
@dataclass
class Matrices:
    """一次載入拿到的兩套矩陣（沒有 ctx 的 KB 只有 retrieval 那一套）。"""

    embeddings: object
    gate_embeddings: object | None
    source: str          # "cache" | "legacy" | "rebuilt"
    path: Optional[Path]


def _require_numpy():
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - numpy 是可選 runtime 相依
        raise KnowledgeStoreError(
            "載入 embeddings cache 需要 numpy；請安裝 numpy 後重試"
        ) from exc
    return np


def _content_hash(chunks, schema: str) -> str:
    return context_signals.chunks_content_hash(chunks, schema=schema)


def _read_npz(path: Path) -> dict:
    np = _require_numpy()
    with np.load(path, allow_pickle=False) as data:
        available = set(getattr(data, "files", []))
        return {
            "embeddings": data["embeddings"].copy(),
            "embedding_model": str(data.get("embedding_model", "")),
            "chunk_count": int(data.get("chunk_count", 0)),
            "content_hash": str(data.get("content_hash", "")),
            "content_hash_schema": str(
                data.get("content_hash_schema", context_signals.LEGACY_CONTENT_HASH_SCHEMA)
            ),
            "store_generation": str(data.get("store_generation", "")),
            "embedding_dimension": int(data.get("embedding_dimension", 0)),
            "chunk_ids": (
                [str(value) for value in data["chunk_ids"].tolist()]
                if "chunk_ids" in available else None
            ),
            "embeddings_gate": (
                data["embeddings_gate"].copy() if "embeddings_gate" in available else None
            ),
            "gate_content_hash": str(data.get("gate_content_hash", "")),
            "gate_content_hash_schema": str(data.get("gate_content_hash_schema", "")),
        }


def _verify(
    payload: dict,
    *,
    chunks: Sequence[Mapping],
    metadata: Mapping,
    strict_identity: bool,
) -> Optional[str]:
    """驗一份 cache 是不是這份 JSON 的。

    回傳 ``None`` ＝ 可用；回傳字串 ＝ **可重建**的不一致（呼叫端會重算）。
    KB 契約本身被破壞的情況直接 raise（見模組 docstring 的兩類劃分）。

    ``strict_identity``：新位置的 cache 一定是本程式寫的，`content_hash` 與
    `chunk_ids` 必須齊全；舊位置的 companion NPZ 是別的版本寫的，缺欄位算
    「證明不了」→ 當成可重建，而不是靜默放行。
    """
    embeddings = payload["embeddings"]
    total = len(chunks)
    has_ctx = context_signals.has_any_ctx(chunks)

    if getattr(embeddings, "ndim", 0) != 2:
        return f"矩陣 shape 不是 2 維（{getattr(embeddings, 'shape', None)}）"
    if payload["embedding_model"]:
        if payload["embedding_model"] != EMBEDDING_MODEL:
            return (f"embedding model 不符（cache={payload['embedding_model']}, "
                    f"目前設定={EMBEDDING_MODEL}）")
    elif strict_identity:
        # 內容雜湊不編碼模型：沒有這一欄就證明不了向量是哪個模型算的。
        return "cache 沒有記下 embedding model，證明不了向量出自目前設定的模型"
    if payload["chunk_count"] != total or embeddings.shape[0] != total:
        return (f"chunk 數不符（cache={payload['chunk_count']}/"
                f"{embeddings.shape[0]}, knowledge.json={total}）")
    stored_dimension = payload["embedding_dimension"]
    if stored_dimension and embeddings.shape[1] != stored_dimension:
        return (f"向量維度與 cache 自報的 metadata 不符"
                f"（{embeddings.shape[1]} vs {stored_dimension}）")

    json_generation = str((metadata or {}).get("store_generation", ""))
    if json_generation and payload["store_generation"] != json_generation:
        return (f"store generation 不符（cache={payload['store_generation'] or '(缺)'}, "
                f"knowledge.json={json_generation}）")

    # required-schema 對照是**硬違規**：不能只信 cache 自報的 schema 再拿它重算
    # 自己，那樣舊 schema 永遠自驗通過，組字規則換了也察覺不到。
    schema = payload["content_hash_schema"]
    allowed = context_signals.required_retrieval_schemas(has_ctx=has_ctx)
    if schema not in allowed:
        raise KnowledgeStoreError(
            f"knowledge embedding schema mismatch: cache={schema!r}, "
            f"required one of {sorted(allowed)}. Rebuild the knowledge base."
        )

    if payload["content_hash"]:
        current = _content_hash(chunks, schema)
        if payload["content_hash"] != current:
            return (f"內容雜湊不符（cache={payload['content_hash']}, "
                    f"knowledge.json={current}）")
    elif strict_identity:
        return "cache 沒有內容雜湊，證明不了它屬於這份 knowledge.json"

    ids = payload["chunk_ids"]
    if ids is not None:
        expected = chunk_row_ids(chunks)
        if ids != expected:
            first = next((i for i, (a, b) in enumerate(zip(ids, expected)) if a != b), 0)
            return (f"逐列 chunk id 不符（第 {first} 列 cache={ids[first]!r}, "
                    f"knowledge.json={expected[first]!r}）")
    elif strict_identity:
        return "cache 沒有逐列 chunk id，只靠陣列順序配對是不安全的"

    if has_ctx:
        gate = payload["embeddings_gate"]
        if gate is None:
            raise KnowledgeStoreError(
                "this knowledge base carries generated chunk context but its embeddings "
                "cache has no gate (content-only) matrix; refusing to use contextual "
                "vectors for decisions. Rebuild the knowledge base."
            )
        if payload["gate_content_hash_schema"] != context_signals.GATE_SCHEMA:
            raise KnowledgeStoreError(
                f"gate embedding schema mismatch: cache="
                f"{payload['gate_content_hash_schema']!r}, "
                f"required {context_signals.GATE_SCHEMA!r}. Rebuild the knowledge base."
            )
        if getattr(gate, "ndim", 0) != 2 or gate.shape != embeddings.shape:
            raise KnowledgeStoreError(
                "gate embedding matrix shape mismatch: "
                f"{getattr(gate, 'shape', None)} vs {embeddings.shape}"
            )
        gate_hash = payload["gate_content_hash"]
        if gate_hash:
            current_gate = _content_hash(chunks, context_signals.GATE_SCHEMA)
            if gate_hash != current_gate:
                raise KnowledgeStoreError(
                    f"gate embedding content hash mismatch: cache={gate_hash}, "
                    f"knowledge.json={current_gate}. Rebuild the knowledge base."
                )
    return None


def locate(json_path, chunks: Sequence[Mapping], metadata: Mapping) -> tuple[Optional[Matrices], str]:
    """找出可用的向量；回傳 ``(matrices, stale_reason)``。

    找得到就 ``(Matrices, "")``；找不到 / 驗不過就 ``(None, 原因)``——原因會原樣
    印給使用者看，讓「為什麼要重算」不是黑箱。硬違規直接往上拋。

    舊位置的 companion NPZ 驗過就**遷移**到隱藏 cache 再把它收掉；驗不過就直接
    淘汰（它是可重建資料，留著只會讓使用者以為知識庫還在）。
    """
    json_path = Path(json_path)
    if not chunks:
        return None, "knowledge.json 沒有 chunk"

    primary = cache_file(json_path)
    legacy = legacy_companion(json_path)
    stale = "embeddings cache 不存在"

    for path, strict in ((primary, True), (legacy, False)):
        if not path.is_file():
            continue
        try:
            payload = _read_npz(path)
        except KnowledgeStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 — 壞檔是可重建的，不是契約違規
            stale = f"{path.name} 讀不回來（{exc}）"
            _discard(path, stale)
            continue
        reason = _verify(payload, chunks=chunks, metadata=metadata,
                         strict_identity=strict)
        if reason is not None:
            stale = reason if strict else f"舊位置的 {path.name} {reason}"
            _discard(path, stale)
            continue
        if path is primary:
            return Matrices(payload["embeddings"], payload["embeddings_gate"],
                            "cache", primary), ""
        # 舊位置的 companion NPZ 完整驗證通過 → 遷移進隱藏 cache 再把它收掉。
        print(f"[INFO] 偵測到舊位置的 {path.name}，完整驗證通過；遷移到 {primary.parent}")
        try:
            _write_npz(primary, payload, chunk_ids=chunk_row_ids(chunks))
        except Exception as exc:  # noqa: BLE001 — 遷移失敗不該讓查詢死掉
            print(f"[WARN] embeddings cache 遷移失敗（這次仍用已驗證的舊向量）: {exc}")
        else:
            with contextlib.suppress(OSError):
                path.unlink()
        return Matrices(payload["embeddings"], payload["embeddings_gate"],
                        "legacy", primary), ""

    return None, stale


def _discard(path: Path, reason: str) -> None:
    print(f"[INFO] 丟棄不可用的 embeddings cache（{reason}）: {path}")
    with contextlib.suppress(OSError):
        path.unlink()


# ==========================================================================
# 寫入
# ==========================================================================
def _write_npz(path: Path, payload: Mapping, *, chunk_ids: Sequence[str]) -> Path:
    """原子寫一份 cache（不需要 store lock：內容自帶身分，重寫同一份是冪等的）。"""
    np = _require_numpy()
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = {
        "embeddings": payload["embeddings"],
        "embedding_model": payload["embedding_model"] or EMBEDDING_MODEL,
        "embedding_dimension": payload["embeddings"].shape[1],
        "chunk_count": payload["embeddings"].shape[0],
        "content_hash": payload["content_hash"],
        "content_hash_schema": payload["content_hash_schema"],
        "store_generation": payload["store_generation"],
        "chunk_ids": np.array(list(chunk_ids)),
    }
    if payload.get("embeddings_gate") is not None:
        fields.update(
            embeddings_gate=payload["embeddings_gate"],
            gate_embedding_dimension=payload["embeddings_gate"].shape[1],
            gate_chunk_count=payload["embeddings_gate"].shape[0],
            gate_content_hash=payload["gate_content_hash"],
            gate_content_hash_schema=payload["gate_content_hash_schema"],
        )
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=path.parent)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "wb") as handle:
            np.savez_compressed(handle, **fields)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return path
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def rebuild(json_path, chunks, metadata, *, reason: str = "") -> Matrices:
    """依 knowledge.json 重算向量並寫回 cache。失敗一律 fail-loud。

    重算走 ``RAG.generate_embeddings``（late import 避開 import 期循環），所以
    文字→向量的增量快取、進度輸出、fail-loud 規則都與入庫路徑同一份實作。
    """
    np = _require_numpy()
    json_path = Path(json_path)
    print(MSG_REBUILD + (f"（原因：{reason}）" if reason else ""))

    import RAG  # late import：knowledge.py → kb_cache → RAG 不得在 import 期成環

    has_ctx = context_signals.has_any_ctx(chunks)
    try:
        RAG.generate_embeddings(list(chunks), cache_dir=json_path.parent, with_gate=has_ctx)
    except KnowledgeStoreError:
        raise
    except Exception as exc:  # noqa: BLE001 — 統一成「不得沿用舊向量」的訊息
        raise KnowledgeStoreError(f"{MSG_FATAL}（{type(exc).__name__}: {exc}）") from exc

    from knowledge_store import validate_embeddings

    rows, _ = validate_embeddings(list(chunks))
    matrix = _normalized(np, rows, "embedding")
    gate_matrix = None
    if has_ctx:
        gate_rows, _ = validate_embeddings(list(chunks), key="embedding_gate")
        gate_matrix = _normalized(np, gate_rows, "gate embedding")

    schema = (context_signals.CONTEXTUAL_INPUT_SCHEMA if has_ctx
              else context_signals.CONTENT_INPUT_SCHEMA)
    payload = {
        "embeddings": matrix,
        "embeddings_gate": gate_matrix,
        "embedding_model": EMBEDDING_MODEL,
        "content_hash": _content_hash(chunks, schema),
        "content_hash_schema": schema,
        "store_generation": str((metadata or {}).get("store_generation", "")),
        "gate_content_hash": (
            _content_hash(chunks, context_signals.GATE_SCHEMA) if has_ctx else ""
        ),
        "gate_content_hash_schema": context_signals.GATE_SCHEMA if has_ctx else "",
    }
    target = cache_file(json_path)
    try:
        _write_npz(target, payload, chunk_ids=chunk_row_ids(chunks))
    except Exception as exc:  # noqa: BLE001 — 寫不進去只是下次還要重算，不影響正確性
        print(f"[WARN] embeddings cache 寫入失敗（這次的向量仍然是重算出來的）: {exc}")
        target = None
    # 舊位置那份已經沒有身分可言了，順手收掉，使用者才不會以為它還有用。
    with contextlib.suppress(OSError):
        legacy_companion(json_path).unlink(missing_ok=True)
    return Matrices(matrix, gate_matrix, "rebuilt", target)


def _normalized(np, rows: Iterable[Iterable[float]], label: str):
    matrix = np.asarray(list(rows), dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(norms <= 0):
        raise KnowledgeStoreError(f"zero-norm {label} detected; refusing to persist it")
    return matrix / norms


def fatal(reason: str) -> KnowledgeStoreError:
    """重建被禁止 / 不可行時的統一訊息。"""
    return KnowledgeStoreError(f"{MSG_FATAL}（原因：{reason}）")
