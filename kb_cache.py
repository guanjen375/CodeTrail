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

### 只有一條規則

**cache 有任何一項證明不了 → 禁止使用 → 依 knowledge.json 重建 → 重建失敗才
fatal。** 包含 schema 不在現行白名單、有 ctx 卻沒有 gate 矩陣、gate 的
schema / shape / 雜湊不符——這些以前是「拒載，請你自己重建整個 KB」，但重算
產生的正是**正確的** gate 矩陣與 schema，安全性質（決策向量不得含生成脈絡）
一步都沒讓，只是把手動步驟換成程式自己修好。真正不能自癒的只有一件事：
重建不出來（例如 embedding server 連不上）——那就中止，絕不沿用舊向量。

「JSON 自己宣告的 embedding model 與目前設定不符」不歸這裡管：那不是 cache
的問題，是 KB 對自己身分的宣告，由載入端 fail-loud（切模型通常也要換切法）。

### 路徑安全（AGENTS.md §3）

cache 目錄是這個模組**唯一會遞迴刪除**的東西。`.codetrail` / `cache` /
`embeddings` 任何一層被換成 symlink，purge 就會刪到 sandbox 外、寫入也會把 NDA
向量寫到外面。所以每次讀 / 寫 / 刪之前都走 `_checked_cache_dir()`：逐層 lstat
拒絕 symlink 與非目錄，最後再驗一次 realpath 仍在 KB 目錄底下。違反一律
fail-loud，**絕不「跳過檢查繼續做」**。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
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


def _checked_cache_dir(json_path) -> Path:
    """★ 安全檢查點：驗過的 cache 目錄路徑（AGENTS.md §3）。

    純驗證，**不建立任何目錄**（`--preflight` 的零寫入斷言要看得到這一點）。
    從 KB 目錄逐層往下 lstat：任何一層是 symlink 或不是目錄一律 fail-loud。
    尚未存在的層直接略過——不存在的東西沒有可被替換的目標。

    為什麼一定要有：這是本模組唯一會 `shutil.rmtree` 的路徑。`.codetrail` 被換成
    指向 sandbox 外的 symlink 時，「刪掉 knowledge.json 之後自動清無主 cache」就
    變成遞迴刪除外部目錄；寫入端同樣會把 NDA 向量寫到外面去。

    最後再比一次 realpath：逐層 lstat 擋掉的是「路徑上有連結」，realpath 比對擋
    的是其他把目標搬出 KB 目錄的方式（例如 KB 目錄本身在載入期間被換掉）。
    """
    base = Path(json_path).parent
    current = base
    for part in (*CACHE_RELDIR.parts, kb_id(json_path)):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise KnowledgeStoreError(f"無法 lstat {current}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise KnowledgeStoreError(
                f"拒絕使用 {current}：它是 symlink。這個目錄樹由 CodeTrail 自動產生，"
                "不應該有連結；若不是你自己建的，可能有人想誘導 CodeTrail 把向量寫到 "
                "sandbox 外、或讓自動清除刪到外面的目錄。請檢查後移除它。"
            )
        if not stat.S_ISDIR(info.st_mode):
            raise KnowledgeStoreError(f"拒絕使用 {current}：它存在但不是目錄")
    try:
        base_real = base.resolve()
        target_real = current.resolve()
    except OSError as exc:
        raise KnowledgeStoreError(f"無法解析 embeddings cache 路徑: {exc}") from exc
    if target_real != base_real and base_real not in target_real.parents:
        raise KnowledgeStoreError(
            f"拒絕使用 {current}：它解析後是 {target_real}，已經在 KB 目錄 "
            f"{base_real} 之外"
        )
    return current


def checked_cache_file(json_path) -> Path:
    """★ 驗過的 cache 檔路徑；檔案本身是 symlink 也一律 fail-loud。

    **不建立目錄**。任何要寫入 cache 的路徑都必須經過這裡（或
    `prepare_cache_target`），不能直接用 `cache_file()` —— 後者只是路徑計算。
    """
    target = _checked_cache_dir(json_path) / CACHE_FILENAME
    if target.is_symlink():
        raise KnowledgeStoreError(
            f"拒絕使用 {target}：它是 symlink（cache 檔由 CodeTrail 自動產生）"
        )
    return target


def prepare_cache_target(json_path) -> Path:
    """建好 cache 目錄並回傳驗過的檔案路徑。

    驗證做兩次：`mkdir` 之前擋掉「已經擺好的 symlink」，`mkdir` 之後再驗一次擋掉
    「在這中間才被換掉」。第二次很便宜，漏掉它就等於把 NDA 向量寫到 sandbox 外。
    """
    target = checked_cache_file(json_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    return checked_cache_file(json_path)


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
    directory = _checked_cache_dir(json_path)   # symlink / 逃出 KB 目錄 → 不刪，直接 raise
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

    回傳 ``None`` ＝ 可用；回傳字串 ＝ 不可用的理由。**永遠不 raise**：cache 的
    任何問題都是「禁止使用、重建」，呼叫端只在重建不出來時才 fatal。

    ``strict_identity`` 差別只有一項：新位置的 cache 一定是本程式寫的，所以
    **逐列 chunk_ids 必須存在**；舊位置的 companion NPZ 是舊版本寫的，沒有那個
    欄位。其餘核心身分（embedding model、內容雜湊、generation、列數、維度）
    兩邊一視同仁——缺任何一項就是證明不了，一律重建。

    沒有 chunk_ids 的舊 NPZ 靠的是**內容雜湊本身有序**（``chunks_content_hash``
    逐 chunk 依序 update，非 legacy schema 還加長度前綴），所以雜湊相符即證明
    「這批 chunk 的順序與內容都沒變」，而向量正是照同一個順序寫的。這不等於
    chunk_ids 那種逐列比對，因此遷移時只說「身分驗證通過」，不說「完整驗證」；
    下一次 save 會補上真正的逐列 id。
    """
    embeddings = payload["embeddings"]
    total = len(chunks)
    has_ctx = context_signals.has_any_ctx(chunks)

    if getattr(embeddings, "ndim", 0) != 2:
        return f"矩陣 shape 不是 2 維（{getattr(embeddings, 'shape', None)}）"
    if not payload["embedding_model"]:
        # 內容雜湊不編碼模型：沒有這一欄就證明不了向量是哪個模型算的。
        return "cache 沒有記下 embedding model，證明不了向量出自目前設定的模型"
    if payload["embedding_model"] != EMBEDDING_MODEL:
        return (f"embedding model 不符（cache={payload['embedding_model']}, "
                f"目前設定={EMBEDDING_MODEL}）")
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

    # required-schema 對照：不能只信 cache 自報的 schema 再拿它重算自己，那樣舊
    # schema 永遠自驗通過，組字規則換了也察覺不到。不在白名單 ＝ 這份向量是用
    # 另一套組字算的，重建。
    schema = payload["content_hash_schema"]
    allowed = context_signals.required_retrieval_schemas(has_ctx=has_ctx)
    if schema not in allowed:
        return (f"content hash schema 不在現行白名單（cache={schema!r}, "
                f"需要 {sorted(allowed)} 其一）")

    if not payload["content_hash"]:
        return "cache 沒有內容雜湊，證明不了它屬於這份 knowledge.json"
    current = _content_hash(chunks, schema)
    if payload["content_hash"] != current:
        return (f"內容雜湊不符（cache={payload['content_hash']}, "
                f"knowledge.json={current}）")

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
        # gate（content-only）矩陣是拒答 / 信心判斷的訊號來源。它證明不了自己的
        # 身分時**不准拿來決策**——但它可以從 knowledge.json 重算出來，所以答案是
        # 重建，不是要使用者去找一個他不該知道位置的 cache。
        gate = payload["embeddings_gate"]
        if gate is None:
            return "有 ctx 的 KB，但 cache 沒有 gate（content-only）矩陣"
        if payload["gate_content_hash_schema"] != context_signals.GATE_SCHEMA:
            return (f"gate schema 不符（cache="
                    f"{payload['gate_content_hash_schema']!r}, "
                    f"需要 {context_signals.GATE_SCHEMA!r}）")
        if getattr(gate, "ndim", 0) != 2 or gate.shape != embeddings.shape:
            return (f"gate 矩陣 shape 不符（{getattr(gate, 'shape', None)} vs "
                    f"{embeddings.shape}）")
        gate_hash = payload["gate_content_hash"]
        if not gate_hash:
            # shape / schema 相同但來源不明的 gate 矩陣照樣會進拒答判斷。
            return "cache 沒有 gate 內容雜湊，證明不了 gate 矩陣屬於這份 knowledge.json"
        current_gate = _content_hash(chunks, context_signals.GATE_SCHEMA)
        if gate_hash != current_gate:
            return (f"gate 內容雜湊不符（cache={gate_hash}, "
                    f"knowledge.json={current_gate}）")
    return None


def locate(json_path, chunks: Sequence[Mapping], metadata: Mapping,
           *, mutate: bool = True) -> tuple[Optional[Matrices], str]:
    """找出可用的向量；回傳 ``(matrices, stale_reason)``。

    找得到就 ``(Matrices, "")``；找不到 / 驗不過就 ``(None, 原因)``——原因會原樣
    印給使用者看，讓「為什麼要重算」不是黑箱。

    ``mutate=False`` ＝ 真正唯讀：不淘汰壞 cache、不遷移舊 NPZ、不寫任何檔。
    離線體檢（`kb_ab_compare`）要報告的是**現在磁碟上的狀態**，看一眼就把它修好
    的話，報告講的就不是使用者手上那份 KB 了。
    """
    json_path = Path(json_path)
    if not chunks:
        return None, "knowledge.json 沒有 chunk"

    primary = checked_cache_file(json_path)   # symlink / 逃出 KB 目錄 → fail-loud
    legacy = legacy_companion(json_path)
    stale = "embeddings cache 不存在"

    for path, strict in ((primary, True), (legacy, False)):
        if not path.is_file():
            continue
        try:
            payload = _read_npz(path)
        except Exception as exc:  # noqa: BLE001 — 壞檔是可重建的
            stale = f"{path.name} 讀不回來（{exc}）"
            if mutate:
                _discard(path, stale)
            continue
        reason = _verify(payload, chunks=chunks, metadata=metadata,
                         strict_identity=strict)
        if reason is not None:
            stale = reason if strict else f"舊位置的 {path.name}：{reason}"
            if mutate:
                _discard(path, stale)
            continue
        if path is primary:
            return Matrices(payload["embeddings"], payload["embeddings_gate"],
                            "cache", primary), ""
        # 舊位置的 companion NPZ 身分驗證通過（model / generation / 有序內容雜湊 /
        # 列數 / 維度）→ 遷移進隱藏 cache 再把它收掉。它沒有逐列 chunk_ids，所以
        # 這裡刻意不說「完整驗證」；下一次 save 才會補上真正的逐列 id。
        if not mutate:
            return Matrices(payload["embeddings"], payload["embeddings_gate"],
                            "legacy", path), ""
        print(f"[INFO] 偵測到舊位置的 {path.name}，身分驗證通過；遷移到 {primary.parent}")
        try:
            _write_npz(json_path, payload, chunk_ids=chunk_row_ids(chunks))
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
def _write_npz(json_path, payload: Mapping, *, chunk_ids: Sequence[str]) -> Path:
    """原子寫一份 cache（不需要 store lock：內容自帶身分，重寫同一份是冪等的）。

    路徑驗證做兩次：`mkdir` 之前擋掉「已經擺好的 symlink」，`mkdir` 之後再驗一次
    擋掉「在這中間才被換掉」。第二次很便宜，而漏掉它就等於把 NDA 向量寫到 sandbox 外。
    """
    np = _require_numpy()
    path = prepare_cache_target(json_path)
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
        raise KnowledgeStoreError(
            f"{MSG_FATAL}（要重建的原因：{reason or '未指定'}；"
            f"重建失敗：{type(exc).__name__}: {exc}）"
        ) from exc

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
    target = checked_cache_file(json_path)
    try:
        _write_npz(json_path, payload, chunk_ids=chunk_row_ids(chunks))
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
    """重建被禁止 / 不可行時的統一訊息。

    `reason` 一定要帶進去：使用者看到的是「查詢被中止」，沒有原因就只能去猜是
    哪一項身分對不上（而 cache 的位置本來就不該是他要知道的東西）。
    """
    return KnowledgeStoreError(f"{MSG_FATAL}（原因：{reason}）")
