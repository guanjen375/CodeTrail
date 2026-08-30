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

### 路徑安全（AGENTS.md §2）

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
    """★ 安全檢查點：驗過的 cache 目錄路徑（AGENTS.md §2）。

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


_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
# `os.replace` 與 `os.rename` 在 POSIX 是同一個 syscall，但只有 rename 會被列進
# supports_dir_fd（figure_review 也用同一個代理判斷）；實際呼叫 replace，因為覆寫
# 語意才是我們要的。
_HAS_OPENAT = bool(
    _O_DIRECTORY and _O_NOFOLLOW
    and {os.open, os.unlink, os.rmdir, os.rename, os.stat, os.mkdir,
         os.link}.issubset(os.supports_dir_fd)
    and os.scandir in os.supports_fd
)


def _link_error(where: Path) -> KnowledgeStoreError:
    return KnowledgeStoreError(
        f"拒絕使用 {where}：它是 symlink。這個目錄樹由 CodeTrail 自動產生，"
        "不應該有連結；若不是你自己建的，可能有人想誘導 CodeTrail 把向量寫到 "
        "sandbox 外、或讓自動清除刪到外面的目錄。請檢查後移除它。"
    )


def _step_dir(parent_fd: int, name: str, where: Path, *, create: bool) -> Optional[int]:
    """往下走一層目錄。`O_NOFOLLOW`：被換成 symlink 就開不起來，不會跟過去。"""
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise KnowledgeStoreError(f"無法建立 {where}: {exc}") from exc
    try:
        info = os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise KnowledgeStoreError(f"無法 lstat {where}: {exc}") from exc
    # 先 lstat 只是為了給出精確訊息（O_NOFOLLOW|O_DIRECTORY 對 symlink 回的是
    # ENOTDIR，讀起來像「不是目錄」）；真正的防線是 O_NOFOLLOW 本身。
    if stat.S_ISLNK(info.st_mode):
        raise _link_error(where)
    if not stat.S_ISDIR(info.st_mode):
        raise KnowledgeStoreError(f"拒絕使用 {where}：它存在但不是目錄")
    try:
        return os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise KnowledgeStoreError(
            f"無法安全開啟 {where}（symlink 或型別不對？）: {exc}"
        ) from exc


@contextlib.contextmanager
def _dir_fd(json_path, names: Sequence[str], *, create: bool):
    """★ 安全檢查點：yield 走到 ``names`` 最後一層的目錄 fd（不存在就 yield None）。

    從 KB 目錄出發逐層 `openat(O_DIRECTORY|O_NOFOLLOW)`，**整段 with 期間持有 fd**。
    之後所有的讀 / 寫 / 刪都用 `dir_fd=` 進行 —— 檢查完才用路徑再 open 一次是
    check-then-use，中途被換成 symlink 就越界了；持有 fd 等於釘住驗過的那個 inode，
    路徑之後怎麼換都動不到我們。

    平台缺 `openat` 家族（Windows）時退回 `_checked_cache_dir()` 的路徑檢查——那擋得住
    「事先擺好的 symlink」，擋不住競態；本 repo 的 sandbox 模型以 POSIX 為準。
    """
    base = Path(json_path).parent
    try:
        base_fd = os.open(base, os.O_RDONLY | _O_DIRECTORY)
    except OSError as exc:
        raise KnowledgeStoreError(f"無法開啟 KB 目錄 {base}: {exc}") from exc
    fd = base_fd
    where = base
    try:
        for name in names:
            where = where / name
            nxt = _step_dir(fd, name, where, create=create)
            os.close(fd)
            fd = nxt
            if fd is None:
                break
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


def _cache_dir_names(json_path) -> tuple[str, ...]:
    return (*CACHE_RELDIR.parts, kb_id(json_path))


@contextlib.contextmanager
def cache_dir_fd(json_path, *, create: bool):
    """cache 目錄（`<kb-id>`）的 fd。缺 openat 支援時 yield None 讓呼叫端走路徑版。"""
    if not _HAS_OPENAT:
        if create:
            prepare_cache_target(json_path)
        else:
            checked_cache_dir_exists = _checked_cache_dir(json_path)
            del checked_cache_dir_exists
        yield None
        return
    with _dir_fd(json_path, _cache_dir_names(json_path), create=create) as fd:
        yield fd


def _rmtree_at(parent_fd: int, name: str, where: Path) -> None:
    """用 dir_fd 遞迴刪掉一層；樹裡出現任何 symlink 一律 fail-loud（不跟過去、也不猜）。"""
    try:
        info = os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise KnowledgeStoreError(f"無法 lstat {where}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise _link_error(where)
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    fd = os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=parent_fd)
    try:
        with os.scandir(fd) as entries:
            children = [entry.name for entry in entries]
        for child in children:
            _rmtree_at(fd, child, where / child)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


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
    if _HAS_OPENAT:
        # 持有 `embeddings/` 的 fd 再刪 `<kb-id>`：檢查完才用路徑刪是 check-then-use，
        # 中途 `.codetrail` 被換成外部 symlink 就會遞迴刪到 sandbox 外。
        with _dir_fd(json_path, CACHE_RELDIR.parts, create=False) as parent_fd:
            if parent_fd is not None:
                name = kb_id(json_path)
                try:
                    os.lstat(name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                else:
                    _rmtree_at(parent_fd, name, directory)
                    removed.append(str(directory))
    elif directory.is_dir():   # pragma: no cover - 非 POSIX 的退路
        shutil.rmtree(directory, ignore_errors=True)
        if not directory.exists():
            removed.append(str(directory))
    legacy = legacy_companion(json_path)
    if legacy.is_file():
        with contextlib.suppress(OSError):
            legacy.unlink()
            removed.append(str(legacy))
    prune_empty_dirs(json_path)
    if removed and announce:
        print(MSG_PURGED)
    return removed


def prune_empty_dirs(json_path) -> None:
    """把空掉的 `<kb-id>` / `embeddings` / `cache` 收乾淨（由深到淺）。

    只 rmdir 空目錄（非空一定失敗）。**一律用 parent 的 dir fd 來 rmdir**：普通
    路徑版是 check-then-use，中間那層在檢查之後被換成 symlink 的話，刪掉的會是
    sandbox 外的空目錄。

    呼叫端必須持有 store lock：Linux 允許 rmdir 一個「已被別人開著但還是空的」目錄，
    所以在鎖外做這件事會讓正在提交的 writer 收到 ENOENT。目前只有 `purge()` 呼叫它，
    而 `purge()` 只從鎖內的 `purge_orphans()` 進來。
    """
    if not _HAS_OPENAT:   # pragma: no cover - 非 POSIX 的退路
        directory = _checked_cache_dir(json_path)
        for parent in (directory, directory.parent, directory.parent.parent):
            with contextlib.suppress(OSError):
                parent.rmdir()
        return
    names = _cache_dir_names(json_path)
    for depth in range(len(names), 0, -1):
        with _dir_fd(json_path, names[:depth - 1], create=False) as parent_fd:
            if parent_fd is None:
                return
            with contextlib.suppress(OSError):
                os.rmdir(names[depth - 1], dir_fd=parent_fd)


def purge_orphans(json_path, *, announce: bool = False) -> list[str]:
    """`knowledge.json` 不在了才清無主 cache——整段在 exclusive lock 內。

    沒有這把鎖會這樣壞掉：查詢 A 看到 JSON 還不存在 → ingest B 開始提交（先把向量
    換上去，再原子換 JSON）→ A 把 B 剛寫好的 cache 刪掉，最後留下「有 JSON、沒有
    向量」的 KB。所以鎖內要**重新確認**檔案真的還是不存在。

    完全沒有東西要清時連鎖都不拿：`knowledge_store_lock` 會建出 `.<name>.lock`，
    為了「確認沒東西可清」而在乾淨的專案裡長出一個檔案是沒有道理的。
    """
    json_path = Path(json_path)
    # 路徑安全先驗（不建任何目錄）：被動手腳的話要在什麼都還沒做之前就 fail-loud，
    # 不能因為「反正短路了沒事」就靜默跳過——使用者不會知道專案裡有那條連結。
    directory = _checked_cache_dir(json_path)
    if not directory.exists() and not legacy_companion(json_path).exists():
        return []

    from knowledge_store import knowledge_store_lock

    with knowledge_store_lock(json_path, exclusive=True):
        if json_path.exists():
            # 有人剛提交完；這不是無主 cache。
            return []
        return purge(json_path, announce=announce)


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


def _read_cache_npz(json_path) -> dict:
    """讀 cache 檔：整段持有目錄 fd，檔案本身以 `O_NOFOLLOW` 開。"""
    if not _HAS_OPENAT:   # pragma: no cover - 非 POSIX 的退路
        return _read_npz(checked_cache_file(json_path))
    with cache_dir_fd(json_path, create=False) as dfd:
        if dfd is None:
            raise FileNotFoundError(str(cache_dir(json_path)))
        fd = os.open(CACHE_FILENAME, os.O_RDONLY | _O_NOFOLLOW, dir_fd=dfd)
        with os.fdopen(fd, "rb") as handle:
            return _read_npz(handle)


def _read_legacy_npz(path: Path) -> dict:
    """舊位置的 companion NPZ：以 `O_NOFOLLOW` 開，不跟著連結走。"""
    if not _HAS_OPENAT:   # pragma: no cover - 非 POSIX 的退路
        return _read_npz(path)
    fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        return _read_npz(handle)


def _read_npz(path) -> dict:
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

    # legacy 的 `content-v1` 只是把每個 chunk 的 content **依序串接**後取 md5，
    # 沒有分隔也沒有長度前綴：`["ab", "c"]` 與 `["a", "bc"]` 的雜湊完全相同、
    # chunk 數也相同，但每一列向量對應到的 chunk 已經換人了。所以
    # 「content-v1 ＋ 沒有 store generation ＋ 沒有逐列 chunk id」這個組合對
    # 「切法改變但總文字不變」完全沒有鑑別力，一律重建。現行 schema 有長度前綴，
    # 不受影響（見 context_signals.chunks_content_hash）。
    if (schema == context_signals.LEGACY_CONTENT_HASH_SCHEMA
            and not json_generation
            and payload["chunk_ids"] is None):
        return ("cache 用的是 content-v1 串接雜湊，又沒有 store generation 與逐列 "
                "chunk id：重新切分（總文字不變）時證明不了每一列的對應")

    ids = payload["chunk_ids"]
    if ids is not None:
        expected = chunk_row_ids(chunks)
        # 長度先單獨看：不然下面用第一個不同的位置去索引，空 list 會 IndexError，
        # 變成「本來承諾丟棄重建，實際上炸在錯誤訊息裡」。
        if len(ids) != len(expected):
            return f"逐列 chunk id 數量不符（cache={len(ids)}, knowledge.json={len(expected)}）"
        if ids != expected:
            first = next(i for i, (a, b) in enumerate(zip(ids, expected)) if a != b)
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

    **判斷本身完全唯讀**。真正動檔案（淘汰壞 cache、遷移舊 NPZ）一律另外走
    `_unchanged_generation()`：取一次短的 exclusive lock 並重驗 KB 還是我們讀到的
    那一代。少了這一步就會這樣壞掉：

        1. 查詢 A 讀到 gen1（讀完就放鎖，因為重算不能持鎖）
        2. ingest B 提交 gen2 的 JSON 與 cache
        3. A 拿 gen1 的身分去驗 gen2 的 cache → 當然不符 → 把 B 剛寫好的**有效**
           cache 刪掉（legacy 遷移更糟：直接用 gen1 的向量蓋過去）

    身分驗證擋得住錯向量被查（不會靜默錯答），但下一次查詢就必須重算，而那一刻
    embedding server 連不上的話整個 KB 拒載。

    ``mutate=False`` ＝ 連那一步都不做。離線體檢（`kb_ab_compare`）要報告的是
    **現在磁碟上的狀態**，看一眼就把它修好的話，報告講的就不是使用者手上那份 KB。
    """
    json_path = Path(json_path)
    if not chunks:
        return None, "knowledge.json 沒有 chunk"

    primary = checked_cache_file(json_path)   # symlink / 逃出 KB 目錄 → fail-loud
    legacy = legacy_companion(json_path)
    stale = "embeddings cache 不存在"
    unusable: list[tuple[Path, str]] = []

    for path, strict in ((primary, True), (legacy, False)):
        if not path.is_file():
            continue
        try:
            payload = (_read_cache_npz(json_path) if path is primary
                       else _read_legacy_npz(path))
        except KnowledgeStoreError:
            # 路徑安全檢查點的錯誤**絕不吞**：吞掉之後下面會用普通路徑去 unlink，
            # 而那條路徑此刻可能正指向 sandbox 外的同名檔案。
            raise
        except Exception as exc:  # noqa: BLE001 — 壞檔是可重建的
            stale = f"{path.name} 讀不回來（{exc}）"
            unusable.append((path, stale))
            continue
        reason = _verify(payload, chunks=chunks, metadata=metadata,
                         strict_identity=strict)
        if reason is not None:
            stale = reason if strict else f"舊位置的 {path.name}：{reason}"
            unusable.append((path, stale))
            continue

        if path is primary:
            return Matrices(payload["embeddings"], payload["embeddings_gate"],
                            "cache", primary), ""
        # 舊位置的 companion NPZ 身分驗證通過（model / generation / 有序內容雜湊 /
        # 列數 / 維度）→ 遷移進隱藏 cache 再把它收掉。它沒有逐列 chunk_ids，所以
        # 這裡刻意不說「完整驗證」；下一次 save 才會補上真正的逐列 id。
        target = path
        if mutate:
            target = _migrate_legacy(json_path, payload, chunks, metadata) or path
        return Matrices(payload["embeddings"], payload["embeddings_gate"],
                        "legacy", target), ""

    if mutate:
        # **不刪 primary**：它會被下一次成功的重建原子覆蓋掉。預先刪它會這樣壞掉——
        # A、B 同時讀到**同一代**的壞 cache，B 先重建出有效的那份，A 隨後仍然照著
        # 檔名把 B 的成果刪掉（generation 一樣，世代檢查看不出差別）。留著一份反正
        # 每次載入都會被拒絕的舊檔，代價只有一點磁碟；刪掉別人剛修好的才是真的痛。
        legacy_stale = [(path, why) for path, why in unusable
                        if path == legacy_companion(json_path)]
        if legacy_stale:
            _discard_legacy(json_path, metadata, legacy_stale)
    return None, stale


@contextlib.contextmanager
def _unchanged_generation(json_path: Path, metadata: Mapping):
    """取 exclusive lock 並 yield「KB 還是我們讀到的那一代嗎」。

    呼叫端**必須**在沒有持有 store lock 的情況下進來：flock 綁在 open file
    description 上，同一個行程持著 shared lock 再要 exclusive 會擋住自己。
    `locate(mutate=True)` 與 `rebuild()` 都只從鎖外的載入路徑呼叫（見
    `RAG.load_knowledge_base` 與 `KnowledgeBase._load`）。
    """
    from knowledge_store import knowledge_store_lock

    expected = str((metadata or {}).get("store_generation", ""))
    with knowledge_store_lock(json_path, exclusive=True):
        current = _json_generation(json_path)
        yield current is not None and current == expected


def _discard_legacy(json_path: Path, metadata: Mapping,
                    unusable: Sequence[tuple[Path, str]]) -> None:
    """淘汰驗不過的**舊位置** companion NPZ——只在 KB 還是同一代的時候。

    只有舊位置那份需要主動刪：它不是我們寫的，沒有「下一次原子覆蓋」可以指望，
    而留著會讓使用者以為「刪了 knowledge.json 知識庫還在」。它也不會被別的行程
    重新產生（本程式只會刪它、不會寫它），所以沒有 primary 那種 ABA 問題。
    """
    with _unchanged_generation(json_path, metadata) as same:
        if not same:
            print("[INFO] KB 在這期間換代了；不動舊位置的 embeddings 檔"
                  "（新一代的判斷不歸這次管）")
            return
        for path, reason in unusable:
            print(f"[INFO] 丟棄不可用的 embeddings cache（{reason}）: {path}")
            # 舊位置那份就在 KB 目錄裡；`unlink` 對 symlink 是刪連結本身，不會穿出去。
            with contextlib.suppress(OSError):
                path.unlink()


def _migrate_legacy(json_path: Path, payload: dict, chunks, metadata) -> Optional[Path]:
    """把驗過的舊 companion NPZ 搬進隱藏 cache——同樣只在 KB 還是同一代的時候。"""
    with _unchanged_generation(json_path, metadata) as same:
        if not same:
            print("[WARN] 準備遷移舊 NPZ 時發現 KB 已換代；本次不搬，"
                  "以免用舊向量蓋掉新一代已經寫好的 cache")
            return None
        legacy = legacy_companion(json_path)
        print(f"[INFO] 偵測到舊位置的 {legacy.name}，身分驗證通過；"
              f"遷移到 {cache_dir(json_path)}")
        try:
            target = _write_npz(json_path, payload, chunk_ids=chunk_row_ids(chunks))
        except KnowledgeStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 — 遷移失敗不該讓查詢死掉
            print(f"[WARN] embeddings cache 遷移失敗（這次仍用已驗證的舊向量）: {exc}")
            return None
        with contextlib.suppress(OSError):
            legacy.unlink()
        return target


# ==========================================================================
# 寫入
# ==========================================================================
def npz_fields(payload: Mapping, chunk_ids: Sequence[str]) -> dict:
    """cache NPZ 的完整欄位（含逐列身分）。寫入端只有這一份定義。"""
    np = _require_numpy()
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
    return fields


def write_npz_at(dir_fd: int, fields: Mapping, *, name: str = CACHE_FILENAME) -> None:
    """在**已持有**的目錄 fd 裡原子寫一份 NPZ：temp(O_EXCL|O_NOFOLLOW) → fsync → replace。

    整段只用 `dir_fd` 相對操作，所以路徑之後被換成 symlink 也影響不到我們——動到的
    永遠是開 fd 當下驗過的那個 inode。
    """
    np = _require_numpy()
    tmp = f".{name}.tmp.{os.getpid()}.{os.urandom(6).hex()}"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_NOFOLLOW, 0o600,
                 dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            np.savez_compressed(handle, **dict(fields))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise


def _write_npz(json_path, payload: Mapping, *, chunk_ids: Sequence[str]) -> Path:
    """原子寫一份 cache（不需要 store lock：內容自帶身分，重寫同一份是冪等的）。"""
    np = _require_numpy()
    fields = npz_fields(payload, chunk_ids)
    path = prepare_cache_target(json_path)
    if _HAS_OPENAT:
        with cache_dir_fd(json_path, create=True) as dfd:
            if dfd is None:   # pragma: no cover - create=True 之後不該是 None
                raise KnowledgeStoreError(f"無法開啟 embeddings cache 目錄: {path.parent}")
            write_npz_at(dfd, fields)
        return path
    # pragma: no cover - 非 POSIX 的退路（只擋得住事先擺好的 symlink）
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
    target = _publish_rebuilt(json_path, payload, chunks, metadata)
    return Matrices(matrix, gate_matrix, "rebuilt", target)


def _json_generation(json_path: Path) -> Optional[str]:
    """讀 knowledge.json 目前的 store_generation；讀不到回 None（代表狀態不明）。"""
    try:
        with open(json_path, "r", encoding="utf-8") as handle:
            return str(json.load(handle).get("metadata", {}).get("store_generation", ""))
    except (OSError, ValueError):
        return None


def _publish_rebuilt(json_path: Path, payload: dict, chunks, metadata) -> Optional[Path]:
    """把重算出來的向量寫進 cache——但只在 KB 還是同一代的時候。

    重算是在**鎖外**做的（不然會持著 shared lock 打幾分鐘網路），所以中途可能有
    人提交了新的一代：

        1. 查詢 A 讀到 gen1，開始慢慢重算
        2. ingest B 提交 gen2 的 JSON 與 cache
        3. A 把 gen1 的向量蓋回固定檔名的 cache

    身分驗證會擋住 gen1 向量被拿去查 gen2 的 chunk，所以**不會**靜默錯答；但 B
    剛寫好的有效 cache 被毀了，下一次查詢得重算，而那一刻 embedding server 如果
    連不上，整個 KB 就拒載。所以發布前取一次短的 exclusive lock 並重驗 generation：
    不同代就不寫（這次的向量仍然對應手上這批 chunk，照常回傳給呼叫端用）。

    這也是為什麼「重算絕不能在持有 store lock 時發生」：flock 綁在 open file
    description 上，同一個行程持著 shared lock 再要 exclusive 會擋住自己。呼叫端
    因此一律先放鎖再重算（`RAG.load_knowledge_base` 與 `KnowledgeBase._load` 都是）。
    """
    try:
        with _unchanged_generation(json_path, metadata) as same:
            if not same:
                print("[WARN] 重算期間 knowledge.json 已換代；本次不寫入 cache，"
                      "以免蓋掉新一代已經寫好的向量")
                return None
            target = _write_npz(json_path, payload, chunk_ids=chunk_row_ids(chunks))
            # 舊位置那份已經沒有身分可言了，順手收掉，使用者才不會以為它還有用。
            with contextlib.suppress(OSError):
                legacy_companion(json_path).unlink(missing_ok=True)
            return target
    except KnowledgeStoreError:
        raise
    except Exception as exc:  # noqa: BLE001 — 寫不進去只是下次還要重算，不影響正確性
        print(f"[WARN] embeddings cache 寫入失敗（這次的向量仍然是重算出來的）: {exc}")
        return None


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
