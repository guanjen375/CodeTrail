"""knowledge_store / kb_cache 的儲存層契約:身分驗證、fail-loud 重建、embedding 失敗、npz 相容。

合併自五份整檔 smoke 的測試(2026-09-02),各自的脈絡分段保留如下。

test_kb_store.py —— KnowledgeBase 的儲存層:staleness 偵測、載入失敗保護、原子雙檔
寫入／回滾、刪除文件後的重寫(它本身合併自 test_kb_staleness.py 與
test_rag_store_regressions.py,2026-08-20)。真實 bug regression:KB staleness 誤判與
刪文件後的 npz 重寫。

test_kb_document_identity.py —— KB 文件身分:同 basename、不同來源檔一律 fail-loud。
知識庫用 **basename** 當文件識別(`chunk["source"]` 與 `metadata["documents"]` 都是
`Path(x).name`)。這讓「不同目錄下的同名文件」在入庫時互相覆蓋,而且訊息長得跟正常
更新一模一樣:

    [INFO] 更新現有文件: spec.pdf

於是 `a/spec.pdf` 的內容整份消失,查詢照樣回答,只是答的是 `b/spec.pdf`。沒有例外、
沒有 warning、chunk 數還是「合理」的——這是無聲錯答,不是失敗。
`remove_document("spec.pdf")` 同樣會刪到非預期的那一份。契約:
`metadata["document_sources"]` 記下每份文件的來源身分,入庫時比對;對不上就 raise
`DocumentIdentityConflict`,**零寫入**。舊 KB 沒有這份紀錄,無法回溯判斷,所以只警告
並採用這一次的來源——但下一次再撞就擋得住了。這條界線是刻意的:發明沒有的歷史比
沉默更糟。

test_kb_cache_lifecycle.py —— knowledge.json 是唯一要人管的 KB 檔;embeddings 是程式
自管的 cache。契約(逐條守住):

1. `knowledge.json` 是 chunk 文字 / metadata / generation 身分的唯一真相。
2. 向量放在隱藏的 cache(`.codetrail/cache/embeddings/<kb-id>/`),使用者不需要
   知道它存在:缺了自動重建、身分不符一律丟棄重建、重建不了就 fail-loud,
   **永遠不准拿舊向量湊合**。
3. JSON 不在了 ＝ 空 KB,而且無主 cache(含舊位置的 companion NPZ)要被清掉。
4. `.codetrail/figures/` 不是 cache,fresh ingest 一個位元組都不准動它。

為什麼一定要 smoke(AGENTS.md §1.1 第 2 款「無聲失敗風險的契約」):這裡每一條
的失敗模式都是**靜默錯答**——向量與 chunk 錯位一列,查詢照樣回答,只是答錯,
而且沒有任何一行 log 會說出來。

test_embedding_fail_loud.py —— 無聲失敗契約:embedding 失敗必須 fail loud,不得回空
向量(AGENTS.md §1.1 第 1 款「真實發生過的 bug 的 regression」)。

test_rag_npz.py —— knowledge.json ↔ embeddings cache 的向量持久化契約。2026-08-24 起
向量搬到 `kb_cache` 管理的隱藏 cache;這裡刻意仍用**舊位置**造 fixture,因為那正是
使用者從舊版本升上來時的磁碟狀態(遷移路徑要一直測得到)。它本身合併自
test_rag_incremental.py 與 test_npz_embedding_attach.py(2026-08-20);P0-5 真實 bug
regression:npz 向量沒掛回 chunk → 相似度全 0。

全部離線:embedding 以 monkeypatch 打樁,不碰任何 server。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

import code_rag
import config
import context_signals
import kb_cache
import knowledge
import knowledge_store
import RAG
from knowledge import KnowledgeBase
from knowledge_store import DocumentIdentityConflict, KnowledgeStoreError
from RAG import load_knowledge_base, save_knowledge_base

# numpy 未裝就整份 skip(AGENTS.md §4:離線/缺套件要 graceful skip,不是 collect error)
np = pytest.importorskip("numpy")

# 五個來源檔都是整檔 smoke → 沿用一個 module 層 pytestmark。
pytestmark = pytest.mark.smoke


# ── 原 test_kb_store.py:KnowledgeBase 儲存層——staleness 偵測、strict loader、
#    原子雙檔寫入／回滾、刪除文件後的 npz 重寫 ──
def _write_empty_kb(path: Path) -> None:
    path.write_text(json.dumps({"chunks": [], "metadata": {}}), encoding="utf-8")


def test_source_changed_false_when_still_missing(tmp_path: Path):
    kb = knowledge.KnowledgeBase(str(tmp_path / "knowledge.json"))
    assert kb.loaded is False
    assert kb.source_changed() is False  # 沒檔案 → 沒變化


def test_source_changed_true_when_file_appears(tmp_path: Path):
    """server 啟動時還沒有 knowledge.json，之後第一次 ingest 建檔 → 要偵測到。"""
    p = tmp_path / "knowledge.json"
    kb = knowledge.KnowledgeBase(str(p))
    _write_empty_kb(p)
    assert kb.source_changed() is True


def test_source_changed_after_mtime_bump(tmp_path: Path):
    """已載入的 KB，檔案被 subprocess/CLI 改寫（mtime 變）→ 要偵測到。"""
    p = tmp_path / "knowledge.json"
    _write_empty_kb(p)
    kb = knowledge.KnowledgeBase(str(p))
    assert kb.loaded is True
    assert kb.source_changed() is False

    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert kb.source_changed() is True


def test_source_changed_after_size_change(tmp_path: Path):
    """mtime 解析度不夠時 size 差異也要能抓到。"""
    p = tmp_path / "knowledge.json"
    _write_empty_kb(p)
    kb = knowledge.KnowledgeBase(str(p))
    st = p.stat()

    p.write_text(
        json.dumps({"chunks": [], "metadata": {"documents": []}}),
        encoding="utf-8",
    )
    # 就算把 mtime 改回舊值，size 不同仍算變更
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert kb.source_changed() is True


def test_load_failure_stays_stale_and_strict_loader_protects_old_kb(tmp_path: Path):
    """壞檔回歸（2026-08-14 GPT review #1）：

    以前壞 JSON 會被吞成 loaded=False 的空殼、同時記住壞檔簽章 →
    source_changed() 回 False，自動重載永遠不再重試，且 MCP 端已把
    還能用的舊 KB 換掉。修復後：失敗的 instance 永遠算 stale（哨兵簽章），
    strict loader 對壞檔拋 KnowledgeStoreError，呼叫端得以保留舊 KB。
    """
    p = tmp_path / "knowledge.json"
    _write_empty_kb(p)
    good = knowledge.KnowledgeBase(str(p))
    assert good.loaded is True

    p.write_text("{broken", encoding="utf-8")
    assert good.source_changed() is True  # 舊 KB 看得到檔案變更

    broken = knowledge.KnowledgeBase(str(p))  # 一般例外被吞：loaded=False
    assert broken.loaded is False
    assert broken.load_error
    assert "載入失敗" in broken.get_status()
    # 修復核心：失敗的 instance 不記壞檔簽章 → 下一次查詢一定重試
    assert broken.source_changed() is True

    with pytest.raises(knowledge.KnowledgeStoreError):
        knowledge.load_knowledge_base_strict(str(p))

    # 修好檔案後 strict loader 恢復正常
    _write_empty_kb(p)
    fixed = knowledge.load_knowledge_base_strict(str(p))
    assert fixed.loaded is True
    assert fixed.load_error is None


def test_strict_loader_accepts_missing_file_as_empty(tmp_path: Path):
    """檔案不存在是合法空庫，strict loader 不該拋錯（首次啟動情境）。"""
    kb = knowledge.load_knowledge_base_strict(str(tmp_path / "knowledge.json"))
    assert kb.loaded is False
    assert kb.load_error is None


# --------------------------------------------------------------------------
# 併自 tests/test_rag_store_regressions.py。
# --------------------------------------------------------------------------
def _kb() -> dict:
    return {
        "metadata": {"documents": ["keep.md", "drop.md"]},
        "chunks": [
            {
                "source": "keep.md",
                "page": 1,
                "chunk_index": 0,
                "content": "KEEP register address 0x1000 and reset value 32.",
                "embedding": [1.0, 0.0],
            },
            {
                "source": "drop.md",
                "page": 1,
                "chunk_index": 0,
                "content": "DROP register address 0x2000 and reset value 64.",
                "embedding": [0.0, 1.0],
            },
        ],
    }


def test_incremental_restore_rejects_embedding_model_mismatch(tmp_path: Path, monkeypatch):
    path = tmp_path / config.KNOWLEDGE_FILE
    kb = _kb()
    path.write_text(
        json.dumps({"metadata": kb["metadata"], "chunks": [{k: v for k, v in c.items() if k != "embedding"} for c in kb["chunks"]]}),
        encoding="utf-8",
    )
    np.savez_compressed(
        tmp_path / config.KNOWLEDGE_EMB_FILE,
        embeddings=np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
        embedding_model="different-embedding-model",
        chunk_count=2,
        content_hash=RAG._chunks_content_hash(kb["chunks"]),
    )
    # embedding server 打不通,重建一定失敗 → 唯一可接受的結果是 fail-loud
    monkeypatch.setattr(RAG.llama_client, "embed_one",
                        lambda **_kw: (_ for _ in ()).throw(OSError("no server")))

    with pytest.raises(RuntimeError, match="embedding"):
        RAG.load_knowledge_base(path)

    assert not (tmp_path / config.KNOWLEDGE_EMB_FILE).exists(), (
        "model 對不上的向量是別的模型算的,不得留著"
    )


def test_save_rejects_mixed_dimensions_without_mutating_or_overwriting(tmp_path: Path):
    path = tmp_path / config.KNOWLEDGE_FILE
    path.write_text('{"sentinel": true}', encoding="utf-8")
    before = path.read_bytes()
    kb = _kb()
    kb["chunks"][1]["embedding"] = [0.0, 1.0, 2.0]

    with pytest.raises(RuntimeError, match="dimension"):
        RAG.save_knowledge_base(kb, path)

    assert path.read_bytes() == before
    assert all(chunk.get("embedding") for chunk in kb["chunks"]), "save must not pop caller vectors"


def test_atomic_pair_write_rolls_back_npz_before_unlock_on_json_publish_failure(
    monkeypatch, tmp_path: Path
):
    path = tmp_path / config.KNOWLEDGE_FILE
    embeddings_path = kb_cache.cache_file(path)
    RAG.save_knowledge_base(_kb(), path)
    original_json = path.read_bytes()
    original_npz = embeddings_path.read_bytes()

    changed = _kb()
    changed["chunks"][0]["content"] = "replacement content"
    real_replace = knowledge_store.os.replace

    def fail_json_publish(source, destination, **kwargs):
        # 向量檔現在走 dir_fd 相對操作（src_dir_fd/dst_dir_fd），所以這個替身要吃得下
        # 那兩個關鍵字；只有「用絕對路徑發布 JSON」那一次才注入失敗。
        if kwargs:
            return real_replace(source, destination, **kwargs)
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path == path and ".tmp." in source_path.name:
            raise OSError("injected JSON publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr(knowledge_store.os, "replace", fail_json_publish)

    with pytest.raises(OSError, match="injected JSON publish failure"):
        RAG.save_knowledge_base(changed, path)

    assert path.read_bytes() == original_json
    assert embeddings_path.read_bytes() == original_npz
    leftovers = [item for item in tmp_path.rglob("*")
                 if "rollback" in item.name or ".tmp." in item.name]
    assert leftovers == []


def test_rollback_restores_the_old_cache_when_hardlink_backup_is_unavailable(
    monkeypatch, tmp_path: Path
):
    """跨檔案系統之類的情況 hardlink 會失敗；備份必須改用複製，不能靜默沒有備份。

    以前 dir_fd 版的備份一失敗就回 None，於是「備份失敗」看起來跟「本來就沒有舊檔」
    一樣——之後 JSON 發布失敗要回滾時，新 cache 被刪掉、舊的卻沒有可以還原。
    """
    path = tmp_path / config.KNOWLEDGE_FILE
    RAG.save_knowledge_base(_kb(), path)
    original_json = path.read_bytes()
    original_npz = kb_cache.cache_file(path).read_bytes()

    monkeypatch.setattr(knowledge_store.os, "link",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no hardlinks")))
    real_replace = knowledge_store.os.replace

    def fail_json_publish(source, destination, **kwargs):
        if kwargs:
            return real_replace(source, destination, **kwargs)
        if Path(destination) == path and ".tmp." in Path(source).name:
            raise OSError("injected JSON publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr(knowledge_store.os, "replace", fail_json_publish)
    changed = _kb()
    changed["chunks"][0]["content"] = "replacement content"

    with pytest.raises(OSError, match="injected JSON publish failure"):
        RAG.save_knowledge_base(changed, path)

    assert path.read_bytes() == original_json
    assert kb_cache.cache_file(path).read_bytes() == original_npz, "舊向量必須被還原"


def test_commit_refuses_to_start_when_no_backup_can_be_taken(monkeypatch, tmp_path: Path):
    """備份做不出來就不准開始替換——沒有回滾點的替換等於單向毀損。"""
    path = tmp_path / config.KNOWLEDGE_FILE
    RAG.save_knowledge_base(_kb(), path)
    original_json = path.read_bytes()
    original_npz = kb_cache.cache_file(path).read_bytes()

    monkeypatch.setattr(knowledge_store.os, "link",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no hardlinks")))
    monkeypatch.setattr(knowledge_store.shutil, "copyfileobj",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    changed = _kb()
    changed["chunks"][0]["content"] = "replacement content"

    with pytest.raises(knowledge_store.KnowledgeStoreError, match="回滾點"):
        RAG.save_knowledge_base(changed, path)

    assert path.read_bytes() == original_json
    assert kb_cache.cache_file(path).read_bytes() == original_npz


def test_backup_refuses_a_symlinked_embedding_file(tmp_path: Path):
    """競態版：向量檔在入口檢查之後才被換成指向 sandbox 外的 symlink。

    `os.link()` 預設 `follow_symlinks=True`，所以 hardlink 快路徑會把**外部檔案**
    連進 cache 目錄當成「舊版備份」，而且成功之後根本不會走到有 O_NOFOLLOW 的
    複製 fallback。備份前必須先確認它是普通檔案。
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "secret.bin"
    victim.write_bytes(b"someone else's bytes")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "embeddings.npz").symlink_to(victim)

    fd = os.open(cache_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        slot = knowledge_store._EmbeddingSlot(cache_dir / "embeddings.npz", fd)
        with pytest.raises(knowledge_store.KnowledgeStoreError, match="普通檔案"):
            slot.backup("gen-x")
    finally:
        os.close(fd)

    assert victim.read_bytes() == b"someone else's bytes"
    assert list(cache_dir.glob("*rollback*")) == []


def test_remove_document_rewrites_remaining_npz_and_reload_keeps_dense_search(monkeypatch, tmp_path: Path):
    path = tmp_path / config.KNOWLEDGE_FILE
    kb = _kb()
    RAG.save_knowledge_base(kb, path)
    remove = getattr(RAG, "remove_document_from_knowledge_base", None)
    assert callable(remove), "RAG must expose the shared transactional removal path"

    result = remove(path, "drop.md")
    assert result["removed_chunks"] == 1

    data = np.load(kb_cache.cache_file(path))
    assert data["embeddings"].shape == (1, 2)
    loaded = KnowledgeBase(str(path))
    assert loaded.loaded
    assert [c["source"] for c in loaded.chunks] == ["keep.md"]
    assert loaded._embeddings is not None
    assert loaded._embeddings.shape == (1, 2)

    monkeypatch.setattr(loaded, "_get_embedding", lambda _text: [1.0, 0.0])
    rows = loaded._hybrid_search("KEEP 0x1000", candidate_k=5)
    assert rows and rows[0].chunk["source"] == "keep.md"


def test_remove_aborts_if_vectors_are_missing_and_leaves_json_unchanged(
    tmp_path: Path, monkeypatch
):
    """向量重建不出來時,刪文件必須整批中止,`knowledge.json` 一個位元組都不能動。

    2026-08-24 起 embeddings 是程式自管的 cache:少了它會**先自動重建**(那是
    `knowledge.json` 才是唯一真相的直接結果)。這條測試守的是重建失敗的那一半——
    絕不能因為「反正剩下的列數對得上」就拿舊向量去寫回一份新 KB。
    """
    path = tmp_path / config.KNOWLEDGE_FILE
    kb = _kb()
    RAG.save_knowledge_base(kb, path)
    kb_cache.cache_file(path).unlink()
    (tmp_path / RAG.EMBEDDING_CACHE_FILE).unlink(missing_ok=True)
    monkeypatch.setattr(RAG.llama_client, "embed_one",
                        lambda **_kw: (_ for _ in ()).throw(OSError("no server")))
    before = path.read_bytes()
    remove = getattr(RAG, "remove_document_from_knowledge_base", None)
    assert callable(remove)

    with pytest.raises(RuntimeError, match="embedding"):
        remove(path, "drop.md")

    assert path.read_bytes() == before


# ── 原 test_kb_document_identity.py:KB 文件身分——同 basename、不同來源檔一律 fail-loud ──
@pytest.fixture
def offline_embeddings(monkeypatch):
    """把 embedding 換成固定向量:入庫流程完整跑,只是不連 server。"""
    def fake(chunks, *args, **kwargs):
        for chunk in chunks or []:
            chunk["embedding"] = [1.0, 0.0]
        return chunks

    monkeypatch.setattr(RAG, "generate_embeddings", fake)
    return fake


def _source_file(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def _kb_payload(kb_path: Path) -> dict:
    return json.loads(kb_path.read_text(encoding="utf-8"))


def _kb_text(kb_path: Path) -> str:
    return kb_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 核心契約
# ---------------------------------------------------------------------------

def test_same_basename_from_a_different_directory_is_refused(
    tmp_path: Path, offline_embeddings
):
    """兩個目錄各有一份 spec.md → 第二份必須被擋下,而且第一份原封不動。"""
    kb_path = tmp_path / config.KNOWLEDGE_FILE
    first = _source_file(tmp_path / "a", "spec.md", "第一份文件的內容 alpha")
    second = _source_file(tmp_path / "b", "spec.md", "第二份文件的內容 bravo")

    RAG.add_document(str(first), str(kb_path))
    assert "alpha" in _kb_text(kb_path)

    with pytest.raises(DocumentIdentityConflict) as exc:
        RAG.add_document(str(second), str(kb_path))

    message = str(exc.value)
    assert "spec.md" in message
    assert str(first) in message and str(second) in message

    # 零寫入:被拒絕的那一次不能動到既有內容
    after = _kb_text(kb_path)
    assert "alpha" in after
    assert "bravo" not in after
    assert _kb_payload(kb_path)["metadata"]["documents"] == ["spec.md"]


def test_reingesting_the_same_file_still_replaces_in_place(
    tmp_path: Path, offline_embeddings
):
    """同一個檔改過內容再入庫是正常的更新路徑,不能被身分檢查擋住。"""
    kb_path = tmp_path / config.KNOWLEDGE_FILE
    source = _source_file(tmp_path / "a", "spec.md", "第一版 alpha")

    RAG.add_document(str(source), str(kb_path))
    source.write_text("第二版 bravo", encoding="utf-8")
    RAG.add_document(str(source), str(kb_path))

    payload = _kb_payload(kb_path)
    assert payload["metadata"]["documents"] == ["spec.md"]
    assert "bravo" in _kb_text(kb_path)
    assert "alpha" not in _kb_text(kb_path)


def test_symlinked_source_is_the_same_document(tmp_path: Path, offline_embeddings):
    """身分比對用解析後的實體路徑;經由 symlink 指到同一個檔不算撞名。"""
    kb_path = tmp_path / config.KNOWLEDGE_FILE
    real = _source_file(tmp_path / "a", "spec.md", "唯一的內容 alpha")
    link_dir = tmp_path / "b"
    link_dir.mkdir()
    link = link_dir / "spec.md"
    link.symlink_to(real)

    RAG.add_document(str(real), str(kb_path))
    RAG.add_document(str(link), str(kb_path))  # 不得 raise

    assert _kb_payload(kb_path)["metadata"]["documents"] == ["spec.md"]


def test_removing_the_document_frees_the_name(tmp_path: Path, offline_embeddings):
    """remove_document 之後,同名但不同來源的文件必須能正常入庫。

    身分紀錄留著不清就會變成「刪不掉的墓碑」:使用者照著錯誤訊息刪了,
    再灌一次還是被擋。
    """
    kb_path = tmp_path / config.KNOWLEDGE_FILE
    first = _source_file(tmp_path / "a", "spec.md", "第一份 alpha")
    second = _source_file(tmp_path / "b", "spec.md", "第二份 bravo")

    RAG.add_document(str(first), str(kb_path))
    RAG.remove_document_from_knowledge_base(kb_path, "spec.md")
    RAG.add_document(str(second), str(kb_path))  # 不得 raise

    assert "bravo" in _kb_text(kb_path)
    assert "alpha" not in _kb_text(kb_path)


def test_fresh_ingest_clears_previous_identities(tmp_path: Path, offline_embeddings):
    """`--fresh` 是「這一份就是新 KB 的全部」;舊的身分紀錄要跟著清掉。"""
    kb_path = tmp_path / config.KNOWLEDGE_FILE
    first = _source_file(tmp_path / "a", "spec.md", "第一份 alpha")
    second = _source_file(tmp_path / "b", "spec.md", "第二份 bravo")

    RAG.add_document(str(first), str(kb_path))
    RAG.add_document(str(second), str(kb_path), fresh=True)  # 不得 raise

    payload = _kb_payload(kb_path)
    assert payload["metadata"]["documents"] == ["spec.md"]
    assert payload["metadata"]["document_sources"] == {"spec.md": str(second.resolve())}
    assert "bravo" in _kb_text(kb_path)
    assert "alpha" not in _kb_text(kb_path)


def test_identity_record_is_written_on_ingest(tmp_path: Path, offline_embeddings):
    kb_path = tmp_path / config.KNOWLEDGE_FILE
    source = _source_file(tmp_path / "a", "spec.md", "內容 alpha")

    RAG.add_document(str(source), str(kb_path))

    sources = _kb_payload(kb_path)["metadata"]["document_sources"]
    assert sources == {"spec.md": str(source.resolve())}


# ---------------------------------------------------------------------------
# 純函式層:身分比對本身
# ---------------------------------------------------------------------------

def test_legacy_kb_without_records_warns_instead_of_conflicting():
    """舊 KB 沒有 document_sources。無法回溯判斷 → 警告 + 採用這一次的來源。

    這裡刻意**不** raise:發明沒有的歷史(假設舊那份就是同一個檔、或假設不是)
    兩種猜法都會錯,而錯的那一半是靜默的。下一次入庫就有紀錄可比了。
    """
    metadata = {"documents": ["spec.md"]}
    warning = knowledge_store.check_document_identity(
        metadata, "spec.md", "/somewhere/spec.md"
    )
    assert warning and "spec.md" in warning


def test_missing_identity_keeps_legacy_behaviour():
    """呼叫端給不出身分(例如舊的內部呼叫)時不擋,也不寫壞既有紀錄。"""
    metadata = {"documents": ["spec.md"], "document_sources": {"spec.md": "/a/spec.md"}}
    assert knowledge_store.check_document_identity(metadata, "spec.md", "") is None
    knowledge_store.record_document_identity(metadata, "spec.md", "")
    assert metadata["document_sources"] == {"spec.md": "/a/spec.md"}


def test_identity_records_are_pruned_to_the_document_list():
    metadata = {
        "documents": ["kept.md"],
        "document_sources": {"kept.md": "/a/kept.md", "gone.md": "/a/gone.md"},
    }
    knowledge_store.sync_document_identities(metadata)
    assert metadata["document_sources"] == {"kept.md": "/a/kept.md"}


def test_conflict_message_names_both_sides_and_the_way_out():
    metadata = {
        "documents": ["spec.md"],
        "document_sources": {"spec.md": "/a/spec.md"},
    }
    with pytest.raises(DocumentIdentityConflict) as exc:
        knowledge_store.check_document_identity(metadata, "spec.md", "/b/spec.md")
    message = str(exc.value)
    assert "/a/spec.md" in message and "/b/spec.md" in message
    # 錯誤訊息必須說得出「接下來怎麼辦」,否則使用者只能猜。
    assert "remove_document" in message
    assert "--fresh" in message


# ── 原 test_kb_cache_lifecycle.py:knowledge.json 唯一真相、embeddings 隱藏 cache 的
#    自動重建／身分驗證／fail-loud ──
# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _vec(text: str) -> list[float]:
    """內容決定的 2 維向量：拿到別的 chunk 的向量一定看得出來。"""
    digest = hashlib.md5(text.encode("utf-8")).digest()
    return [1.0 + digest[0] % 7, 1.0 + digest[1] % 7]


def _stub_embed(monkeypatch) -> list[str]:
    """打樁 embedding server，並記錄實際被送出去的字串。"""
    sent: list[str] = []

    def fake_embed_one(*, content, **_kw):
        sent.append(content)
        return _vec(content)

    monkeypatch.setattr(RAG.llama_client, "embed_one", fake_embed_one)
    return sent


def _no_embed_server(monkeypatch, tmp_path: Path) -> None:
    """embedding service 不可用（重建必須 fail-loud，不得沿用舊向量）。

    順手把文字→向量的增量快取也拿掉：留著的話重建會全部快取命中而根本不連線，
    就測不到「連不上時會怎樣」。（那個增量快取本身也是可重建資料。）
    """
    def boom(**_kw):
        raise OSError("connection refused (stub)")

    monkeypatch.setattr(RAG.llama_client, "embed_one", boom)
    (tmp_path / RAG.EMBEDDING_CACHE_FILE).unlink(missing_ok=True)


def _chunks(*bodies: str, source: str = "spec.md") -> list[dict]:
    return [
        {"source": source, "page": 1, "chunk_index": index, "content": body}
        for index, body in enumerate(bodies)
    ]


def _save(tmp_path: Path, chunks: list[dict], *, documents=("spec.md",)) -> Path:
    """走正式寫入路徑造一份 KB（向量由打樁的 embedding server 產生）。"""
    kb_path = tmp_path / config.KNOWLEDGE_FILE
    payload = {
        "metadata": {"documents": list(documents), "embedding_model": config.EMBEDDING_MODEL},
        "chunks": [dict(chunk) for chunk in chunks],
    }
    RAG.save_knowledge_base(payload, kb_path)
    return kb_path


def _cache_files(tmp_path: Path) -> list[Path]:
    """隱藏 cache 目錄裡的 NPZ（使用者不該需要知道確切檔名）。"""
    root = tmp_path / ".codetrail" / "cache"
    return sorted(p for p in root.rglob("*.npz")) if root.exists() else []


def _legacy_npz(tmp_path: Path) -> Path:
    return tmp_path / config.KNOWLEDGE_EMB_FILE


def _read_cache(tmp_path: Path) -> dict:
    files = _cache_files(tmp_path)
    assert files, "找不到隱藏的 embeddings cache"
    with np.load(files[0], allow_pickle=False) as data:
        return {key: data[key] for key in data.files}


def _write_cache(tmp_path: Path, payload: dict) -> None:
    files = _cache_files(tmp_path)
    assert files
    np.savez_compressed(files[0], **payload)


def _vectors(kb_path: Path) -> list[list[float]]:
    kb = KnowledgeBase(str(kb_path))
    assert kb.loaded, kb.load_error
    return [list(chunk["embedding"]) for chunk in kb.chunks]


# ==========================================================================
# 驗收 1 — 空 KB fresh ingest 之後查得到
# ==========================================================================
def test_fresh_ingest_into_an_empty_kb_is_immediately_queryable(tmp_path: Path, monkeypatch):
    _stub_embed(monkeypatch)
    doc = tmp_path / "spec.md"
    doc.write_text("# Reset\n\nCTRL0 register address 0x1000 and reset value 32.\n",
                   encoding="utf-8")
    kb_path = tmp_path / config.KNOWLEDGE_FILE

    RAG.add_document(str(doc), str(kb_path), fresh=True)

    kb = KnowledgeBase(str(kb_path))
    assert kb.loaded, kb.load_error
    assert kb.chunks and all(chunk.get("embedding") for chunk in kb.chunks)
    assert _cache_files(tmp_path), "向量必須落在隱藏 cache 目錄"
    assert not _legacy_npz(tmp_path).exists(), "舊位置的 companion NPZ 不該再出現"


# ==========================================================================
# 驗收 2 — 只刪 knowledge.json：空 KB + 無主 cache 自動清除
# ==========================================================================
def test_deleting_only_the_json_gives_an_empty_kb_and_purges_the_orphan_cache(
    tmp_path: Path, monkeypatch
):
    _stub_embed(monkeypatch)
    kb_path = _save(tmp_path, _chunks("alpha body", "beta body"))
    assert _cache_files(tmp_path)
    # 使用者也可能是從更早的版本升上來的：舊位置那份一樣要被清掉
    _legacy_npz(tmp_path).write_bytes(b"stale legacy npz")

    kb_path.unlink()

    kb = KnowledgeBase(str(kb_path))
    assert kb.loaded is False and kb.chunks == []
    assert kb.load_error is None, "檔案不存在是合法空庫，不是壞庫"
    assert _cache_files(tmp_path) == [], "JSON 不在了，舊向量必須被清掉"
    assert not _legacy_npz(tmp_path).exists()


# ==========================================================================
# 驗收 3 — 只刪 cache：自動重建
# ==========================================================================
def test_deleting_only_the_cache_rebuilds_it_automatically(tmp_path: Path, monkeypatch):
    _stub_embed(monkeypatch)
    kb_path = _save(tmp_path, _chunks("alpha body", "beta body"))
    before = _vectors(kb_path)
    for path in _cache_files(tmp_path):
        path.unlink()

    after = _vectors(kb_path)

    assert after == before
    assert _cache_files(tmp_path), "重建後要把 cache 寫回去，不能每次查詢都重算"


def test_cache_that_cannot_be_rebuilt_fails_loud_instead_of_reusing_old_vectors(
    tmp_path: Path, monkeypatch
):
    _stub_embed(monkeypatch)
    kb_path = _save(tmp_path, _chunks("alpha body", "beta body"))
    for path in _cache_files(tmp_path):
        path.unlink()
    _no_embed_server(monkeypatch, tmp_path)

    with pytest.raises(KnowledgeStoreError) as excinfo:
        KnowledgeBase(str(kb_path))

    assert "cache" in str(excinfo.value)


# ==========================================================================
# 驗收 4 — 另一份「chunk 數相同」的 JSON 覆蓋原檔
# ==========================================================================
def test_overwriting_the_json_with_a_same_sized_kb_rebuilds_instead_of_misaligning(
    tmp_path: Path, monkeypatch
):
    _stub_embed(monkeypatch)
    kb_path = _save(tmp_path, _chunks("alpha body", "beta body"))
    stale_vectors = _vectors(kb_path)

    replacement = _chunks("completely different one", "completely different two")
    kb_path.write_text(
        json.dumps({"metadata": {"documents": ["spec.md"],
                                 "embedding_model": config.EMBEDDING_MODEL},
                    "chunks": replacement}, ensure_ascii=False),
        encoding="utf-8",
    )

    rebuilt = _vectors(kb_path)

    assert rebuilt != stale_vectors, "換了內容卻沿用舊向量 ＝ 靜默錯位"
    for chunk, vector in zip(replacement, rebuilt):
        expected = _vec(RAG.context_signals.retrieval_embedding_input(chunk, use_ctx=True))
        norm = (expected[0] ** 2 + expected[1] ** 2) ** 0.5
        assert vector == pytest.approx([expected[0] / norm, expected[1] / norm], rel=1e-5)


# ==========================================================================
# 驗收 5 — 竄改 generation / hash / chunk IDs / model：不得靜默錯位
# ==========================================================================
@pytest.mark.parametrize("field", ["store_generation", "content_hash",
                                   "chunk_ids", "embedding_model"])
def test_tampered_cache_identity_never_produces_a_silent_query(
    tmp_path: Path, monkeypatch, field: str
):
    _stub_embed(monkeypatch)
    kb_path = _save(tmp_path, _chunks("alpha body", "beta body"))
    payload = _read_cache(tmp_path)
    assert "chunk_ids" in payload, "cache 必須逐列記下 chunk 身分，不能只靠陣列順序"

    if field == "chunk_ids":
        payload["chunk_ids"] = np.array(["bogus-id-1", "bogus-id-2"])
    else:
        payload[field] = np.array("tampered")
    # 向量本身刻意保持可用：唯一的差別就是身分欄位對不上
    _write_cache(tmp_path, payload)
    _no_embed_server(monkeypatch, tmp_path)

    with pytest.raises(KnowledgeStoreError):
        KnowledgeBase(str(kb_path))


def test_row_order_alone_is_not_accepted_as_identity(tmp_path: Path, monkeypatch):
    """列數、內容雜湊、generation 全對，只有列序被掉換 → 必須抓得到。"""
    _stub_embed(monkeypatch)
    kb_path = _save(tmp_path, _chunks("alpha body", "beta body"))
    payload = _read_cache(tmp_path)
    payload["embeddings"] = payload["embeddings"][::-1]
    payload["chunk_ids"] = payload["chunk_ids"][::-1]
    _write_cache(tmp_path, payload)
    _no_embed_server(monkeypatch, tmp_path)

    with pytest.raises(KnowledgeStoreError):
        KnowledgeBase(str(kb_path))


# ==========================================================================
# 驗收 6 — fresh 不疊加、不動 figure artifacts
# ==========================================================================
def _figure_artifact(tmp_path: Path) -> Path:
    run = tmp_path / ".codetrail" / "figures" / "spec.pdf__deadbeef" / "20260101-000000-abcd"
    run.mkdir(parents=True)
    manifest = run / "manifest.json"
    manifest.write_text(json.dumps({"human_verification": {"confirmed_against_image": True}}),
                        encoding="utf-8")
    return manifest


def test_fresh_mode_replaces_chunks_and_never_touches_figure_artifacts(
    tmp_path: Path, monkeypatch
):
    _stub_embed(monkeypatch)
    kb_path = tmp_path / config.KNOWLEDGE_FILE
    first = tmp_path / "first.md"
    first.write_text("first document body about register 0x1000\n", encoding="utf-8")
    second = tmp_path / "second.md"
    second.write_text("second document body about register 0x2000\n", encoding="utf-8")
    RAG.add_document(str(first), str(kb_path))
    manifest = _figure_artifact(tmp_path)
    manifest_bytes = manifest.read_bytes()

    RAG.add_document(str(second), str(kb_path), fresh=True)

    kb = json.loads(kb_path.read_text(encoding="utf-8"))
    assert {chunk["source"] for chunk in kb["chunks"]} == {"second.md"}
    assert kb["metadata"]["documents"] == ["second.md"]
    assert manifest.read_bytes() == manifest_bytes, "figure artifacts 不是 cache，不得被 reset"
    assert KnowledgeBase(str(kb_path)).loaded


# ==========================================================================
# 驗收 7 — fresh 中途失敗不得留下 JSON/cache 錯配
# ==========================================================================
def test_failed_fresh_ingest_leaves_the_previous_kb_and_cache_consistent(
    tmp_path: Path, monkeypatch
):
    _stub_embed(monkeypatch)
    kb_path = tmp_path / config.KNOWLEDGE_FILE
    first = tmp_path / "first.md"
    first.write_text("first document body about register 0x1000\n", encoding="utf-8")
    RAG.add_document(str(first), str(kb_path))
    json_before = kb_path.read_bytes()
    cache_before = _cache_files(tmp_path)[0].read_bytes()

    second = tmp_path / "second.md"
    second.write_text("second document body about register 0x2000\n", encoding="utf-8")
    real_replace = knowledge_store.os.replace

    def fail_json_publish(source, destination, **kwargs):
        # 向量檔走 dir_fd 相對操作；只攔「用絕對路徑發布 JSON」那一次。
        if kwargs:
            return real_replace(source, destination, **kwargs)
        if Path(destination) == kb_path and ".tmp." in Path(source).name:
            raise OSError("injected JSON publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr(knowledge_store.os, "replace", fail_json_publish)

    with pytest.raises(OSError, match="injected JSON publish failure"):
        RAG.add_document(str(second), str(kb_path), fresh=True)

    monkeypatch.undo()
    _stub_embed(monkeypatch)
    assert kb_path.read_bytes() == json_before
    assert _cache_files(tmp_path)[0].read_bytes() == cache_before
    kb = KnowledgeBase(str(kb_path))
    assert kb.loaded and {chunk["source"] for chunk in kb.chunks} == {"first.md"}


# ==========================================================================
# 驗收 8 — 預設 ingest 語意不變（append）
# ==========================================================================
def test_default_ingest_still_appends(tmp_path: Path, monkeypatch):
    _stub_embed(monkeypatch)
    kb_path = tmp_path / config.KNOWLEDGE_FILE
    first = tmp_path / "first.md"
    first.write_text("first document body about register 0x1000\n", encoding="utf-8")
    second = tmp_path / "second.md"
    second.write_text("second document body about register 0x2000\n", encoding="utf-8")

    RAG.add_document(str(first), str(kb_path))
    RAG.add_document(str(second), str(kb_path))

    kb = json.loads(kb_path.read_text(encoding="utf-8"))
    assert {chunk["source"] for chunk in kb["chunks"]} == {"first.md", "second.md"}
    assert sorted(kb["metadata"]["documents"]) == ["first.md", "second.md"]


# ==========================================================================
# 驗收 9 — 舊位置 companion NPZ 的遷移／淘汰
# ==========================================================================
def _write_legacy_pair(tmp_path: Path, chunks: list[dict], *, vectors) -> Path:
    """舊位置／無 chunk_ids；其餘身分完整，包含明示零節點的 section schema。

    本組守位置遷移與核心身分。真正缺 section schema 的舊檔，另由
    test_section_store 守住必須重建／不可重建即失敗的遷移契約。
    """
    kb_path = tmp_path / config.KNOWLEDGE_FILE
    schema = RAG.context_signals.CONTENT_INPUT_SCHEMA
    kb_path.write_text(
        json.dumps({"metadata": {"documents": ["spec.md"],
                                 "embedding_model": config.EMBEDDING_MODEL,
                                 "store_generation": "legacy-gen"},
                    "chunks": chunks}, ensure_ascii=False),
        encoding="utf-8",
    )
    rows = np.array(vectors, dtype=np.float32)
    rows = rows / np.linalg.norm(rows, axis=1, keepdims=True)
    fields = kb_cache.npz_fields({
        "embeddings": rows,
        "embedding_model": config.EMBEDDING_MODEL,
        "content_hash": RAG.context_signals.chunks_content_hash(chunks, schema=schema),
        "content_hash_schema": schema,
        "store_generation": "legacy-gen",
        **kb_cache.section_fields(chunks, None, dimension=rows.shape[1]),
    }, kb_cache.chunk_row_ids(chunks))
    fields.pop("chunk_ids")  # Retain the legacy-location row-identity scenario.
    np.savez_compressed(_legacy_npz(tmp_path), **fields)
    return kb_path


def test_a_fully_verifiable_legacy_npz_is_migrated_not_rebuilt(tmp_path: Path, monkeypatch):
    chunks = _chunks("alpha body", "beta body")
    kb_path = _write_legacy_pair(tmp_path, chunks, vectors=[[1.0, 0.0], [0.0, 1.0]])
    _no_embed_server(monkeypatch, tmp_path)  # 完整驗證過的舊 NPZ 不該需要重算

    kb = KnowledgeBase(str(kb_path))

    assert kb.loaded, kb.load_error
    assert [list(c["embedding"]) for c in kb.chunks] == [[1.0, 0.0], [0.0, 1.0]]
    assert _cache_files(tmp_path), "驗過的舊 NPZ 要遷移到隱藏 cache"
    assert not _legacy_npz(tmp_path).exists(), "遷移完要把使用者看得到的那份收掉"


def test_a_legacy_npz_with_matching_count_but_wrong_content_is_discarded(
    tmp_path: Path, monkeypatch
):
    """筆數剛好相同不等於安全：內容雜湊對不上就必須丟棄重建。"""
    chunks = _chunks("alpha body", "beta body")
    kb_path = _write_legacy_pair(tmp_path, chunks, vectors=[[1.0, 0.0], [0.0, 1.0]])
    kb_path.write_text(
        json.dumps({"metadata": {"documents": ["spec.md"],
                                 "embedding_model": config.EMBEDDING_MODEL,
                                 "store_generation": "legacy-gen"},
                    "chunks": _chunks("totally other one", "totally other two")},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    _no_embed_server(monkeypatch, tmp_path)

    with pytest.raises(KnowledgeStoreError):
        KnowledgeBase(str(kb_path))

    assert not _legacy_npz(tmp_path).exists(), "驗不過的舊 NPZ 是可重建資料，要淘汰掉"


# ==========================================================================
# 兩份 KB 放同一個目錄：cache 不得互相覆蓋
# ==========================================================================
def test_two_kbs_in_one_directory_keep_separate_caches(tmp_path: Path, monkeypatch):
    _stub_embed(monkeypatch)
    first = tmp_path / "knowledge.json"
    second = tmp_path / "other.json"
    RAG.save_knowledge_base(
        {"metadata": {"documents": ["a.md"], "embedding_model": config.EMBEDDING_MODEL},
         "chunks": _chunks("alpha body", source="a.md")}, first)
    RAG.save_knowledge_base(
        {"metadata": {"documents": ["b.md"], "embedding_model": config.EMBEDDING_MODEL},
         "chunks": _chunks("beta body", "gamma body", source="b.md")}, second)

    assert len(_cache_files(tmp_path)) == 2
    _no_embed_server(monkeypatch, tmp_path)
    assert len(KnowledgeBase(str(first)).chunks) == 1
    assert len(KnowledgeBase(str(second)).chunks) == 2


# ==========================================================================
# ingest 路徑也要清無主 cache（不需要 file watcher）
# ==========================================================================
def test_ingest_after_an_external_json_deletion_starts_from_an_empty_kb(
    tmp_path: Path, monkeypatch
):
    _stub_embed(monkeypatch)
    kb_path = tmp_path / config.KNOWLEDGE_FILE
    first = tmp_path / "first.md"
    first.write_text("first document body about register 0x1000\n", encoding="utf-8")
    RAG.add_document(str(first), str(kb_path))
    stale_cache = _cache_files(tmp_path)[0]
    stale_bytes = stale_cache.read_bytes()

    kb_path.unlink()  # 使用者用檔案總管刪掉 knowledge.json
    second = tmp_path / "second.md"
    second.write_text("second document body about register 0x2000\n", encoding="utf-8")
    RAG.add_document(str(second), str(kb_path))

    kb = json.loads(kb_path.read_text(encoding="utf-8"))
    assert {chunk["source"] for chunk in kb["chunks"]} == {"second.md"}
    live = _cache_files(tmp_path)
    assert len(live) == 1 and live[0].read_bytes() != stale_bytes
    assert KnowledgeBase(str(kb_path)).loaded


# ==========================================================================
# ★ 安全檢查點（AGENTS.md §2）：cache 路徑上的 symlink 一律 fail-closed
#
# purge 會 rmtree 整個 <kb-id> 目錄。`.codetrail` 被換成指向 sandbox 外的 symlink
# 時，「刪掉 knowledge.json 之後自動清無主 cache」就變成遞迴刪除外部目錄；寫入端
# 同樣會把 NDA 向量寫到外面。這一組守的是「拒絕並 raise」，不是「跳過檢查繼續做」。
# ==========================================================================
@pytest.mark.parametrize("link_at", [".codetrail", ".codetrail/cache"])
def test_purge_refuses_to_delete_through_a_symlinked_cache_path(
    tmp_path: Path, monkeypatch, link_at: str
):
    _stub_embed(monkeypatch)
    kb_path = _save(tmp_path, _chunks("alpha body", "beta body"))
    outside = tmp_path.parent / f"outside-{link_at.replace('/', '-')}"
    outside.mkdir(exist_ok=True)
    (outside / "precious.txt").write_text("do not delete", encoding="utf-8")

    # 把 cache 路徑上的某一層換成指向 sandbox 外的 symlink
    link = tmp_path / link_at
    shutil.rmtree(tmp_path / ".codetrail")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    kb_path.unlink()

    with pytest.raises(KnowledgeStoreError, match="symlink"):
        KnowledgeBase(str(kb_path))

    assert (outside / "precious.txt").is_file(), "絕不可以刪到 sandbox 外"
    assert link.is_symlink(), "連結本身也不該被動"


def test_writing_the_cache_refuses_a_symlinked_path(tmp_path: Path, monkeypatch):
    _stub_embed(monkeypatch)
    outside = tmp_path.parent / "outside-write"
    outside.mkdir(exist_ok=True)
    (tmp_path / ".codetrail").symlink_to(outside, target_is_directory=True)

    with pytest.raises(KnowledgeStoreError, match="symlink"):
        _save(tmp_path, _chunks("alpha body"))

    assert list(outside.rglob("*.npz")) == [], "向量不得寫到 sandbox 外"


# ==========================================================================
# fresh 不該因為「舊 cache 壞掉」而做不了——舊向量整批要丟了
# ==========================================================================
def test_fresh_ingest_works_even_when_the_old_cache_is_unusable(
    tmp_path: Path, monkeypatch
):
    _stub_embed(monkeypatch)
    kb_path = tmp_path / config.KNOWLEDGE_FILE
    first = tmp_path / "first.md"
    first.write_text("first document body about register 0x1000\n", encoding="utf-8")
    RAG.add_document(str(first), str(kb_path))
    # 舊 cache 壞掉（刪掉 / 內容毀損都算），而且 embedding server 也回不來
    for path in _cache_files(tmp_path):
        path.write_bytes(b"not an npz at all")
    second = tmp_path / "second.md"
    second.write_text("second document body about register 0x2000\n", encoding="utf-8")

    RAG.add_document(str(second), str(kb_path), fresh=True)

    kb = json.loads(kb_path.read_text(encoding="utf-8"))
    assert {chunk["source"] for chunk in kb["chunks"]} == {"second.md"}
    assert KnowledgeBase(str(kb_path)).loaded


# ==========================================================================
# allow_rebuild=False 必須是真的唯讀（離線體檢不能順手把 KB 修好）
# ==========================================================================
def test_read_only_load_neither_discards_nor_migrates(tmp_path: Path, monkeypatch):
    chunks = _chunks("alpha body", "beta body")
    kb_path = _write_legacy_pair(tmp_path, chunks, vectors=[[1.0, 0.0], [0.0, 1.0]])
    legacy_before = _legacy_npz(tmp_path).read_bytes()
    _no_embed_server(monkeypatch, tmp_path)

    kb = KnowledgeBase(str(kb_path), allow_rebuild=False)

    assert kb.loaded, kb.load_error
    assert _legacy_npz(tmp_path).read_bytes() == legacy_before, "唯讀載入不得搬走舊 NPZ"
    assert _cache_files(tmp_path) == [], "唯讀載入不得寫出新 cache"


def test_read_only_load_reports_instead_of_repairing_a_bad_cache(
    tmp_path: Path, monkeypatch
):
    _stub_embed(monkeypatch)
    kb_path = _save(tmp_path, _chunks("alpha body", "beta body"))
    payload = _read_cache(tmp_path)
    payload["content_hash"] = np.array("tampered")
    _write_cache(tmp_path, payload)
    before = _cache_files(tmp_path)[0].read_bytes()

    with pytest.raises(KnowledgeStoreError, match="內容雜湊"):
        KnowledgeBase(str(kb_path), allow_rebuild=False)

    assert _cache_files(tmp_path)[0].read_bytes() == before, "唯讀載入不得淘汰壞 cache"


# ==========================================================================
# 舊 NPZ 缺核心身分：不可以「反正列數對」就認證它
# ==========================================================================
@pytest.mark.parametrize("drop", ["embedding_model", "content_hash"])
def test_legacy_npz_without_core_identity_is_discarded(
    tmp_path: Path, monkeypatch, drop: str
):
    chunks = _chunks("alpha body", "beta body")
    kb_path = _write_legacy_pair(tmp_path, chunks, vectors=[[1.0, 0.0], [0.0, 1.0]])
    with np.load(_legacy_npz(tmp_path), allow_pickle=False) as data:
        payload = {key: data[key] for key in data.files if key != drop}
    np.savez_compressed(_legacy_npz(tmp_path), **payload)
    _no_embed_server(monkeypatch, tmp_path)

    with pytest.raises(KnowledgeStoreError):
        KnowledgeBase(str(kb_path))

    assert not _legacy_npz(tmp_path).exists()


def test_gate_matrix_without_its_own_hash_is_not_trusted(tmp_path: Path, monkeypatch):
    """shape / schema 都對、但來源不明的 gate 矩陣照樣會進拒答判斷。"""
    _stub_embed(monkeypatch)
    chunks = [dict(chunk, ctx="生成脈絡一行") for chunk in _chunks("alpha body", "beta body")]
    kb_path = _save(tmp_path, chunks)
    payload = _read_cache(tmp_path)
    assert "embeddings_gate" in payload
    payload["gate_content_hash"] = np.array("")
    _write_cache(tmp_path, payload)
    _no_embed_server(monkeypatch, tmp_path)

    with pytest.raises(KnowledgeStoreError, match="gate"):
        KnowledgeBase(str(kb_path))


# ==========================================================================
# 鎖外重建不得蓋掉「更新一代」剛寫好的 cache
# ==========================================================================
def test_a_slow_rebuild_never_overwrites_a_newer_generation_cache(
    tmp_path: Path, monkeypatch
):
    """重算是在鎖外做的，所以中途可能有人提交了新的一代。

    身分驗證會擋住舊向量被拿去查新 chunk（不會靜默錯答），但如果讓舊的重算結果
    蓋回固定檔名的 cache，剛提交的**有效** cache 就毀了：下一次查詢必須重算，而
    那一刻 embedding server 連不上的話整個 KB 直接拒載。
    """
    _stub_embed(monkeypatch)
    kb_path = _save(tmp_path, _chunks("alpha body", "beta body"))
    gen_one = json.loads(kb_path.read_text(encoding="utf-8"))["metadata"]["store_generation"]

    # 查詢 A 手上是 gen1 的 chunks（重算是在鎖外做的，可能慢到幾分鐘）
    old_chunks = json.loads(kb_path.read_text(encoding="utf-8"))["chunks"]
    old_meta = {"store_generation": gen_one}

    # ingest B 期間提交了 gen2（JSON 與 cache 一起換掉）
    _save(tmp_path, _chunks("gen two one", "gen two two"))
    gen_two_cache = _cache_files(tmp_path)[0].read_bytes()

    # A 現在才算完並準備發布 —— 發布前會重驗 generation
    matrices = kb_cache.rebuild(kb_path, old_chunks, old_meta, reason="test")

    assert matrices.embeddings is not None, "呼叫端仍拿得到手上這批 chunk 的向量"
    assert matrices.path is None, "但不得發布到 cache（那會蓋掉 gen2 剛寫好的）"
    assert _cache_files(tmp_path)[0].read_bytes() == gen_two_cache, "gen2 的 cache 必須原封不動"

    # 而且 gen2 仍然可以直接載入，不需要再重算一次
    _no_embed_server(monkeypatch, tmp_path)
    kb = KnowledgeBase(str(kb_path))
    assert kb.loaded, kb.load_error
    assert [c["content"] for c in kb.chunks] == ["gen two one", "gen two two"]


# ==========================================================================
# legacy content-v1 的串接雜湊擋不住「重新切分、總文字不變」
# ==========================================================================
def test_legacy_content_v1_without_generation_or_ids_is_rejected(
    tmp_path: Path, monkeypatch
):
    """`["ab","c"]` 與 `["a","bc"]` 的 content-v1 雜湊完全相同、chunk 數也相同。

    這種 NPZ 對「切法變了但總文字沒變」毫無鑑別力，遷移它等於把錯位的向量重新
    認證一次。現行 schema 有長度前綴，不受影響。
    """
    kb_path = tmp_path / config.KNOWLEDGE_FILE
    written = _chunks("ab", "c")
    kb_path.write_text(
        json.dumps({"metadata": {"documents": ["spec.md"],
                                 "embedding_model": config.EMBEDDING_MODEL},
                    "chunks": _chunks("a", "bc")}, ensure_ascii=False),
        encoding="utf-8",
    )
    rows = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    np.savez_compressed(
        _legacy_npz(tmp_path),
        embeddings=rows,
        embedding_model=config.EMBEDDING_MODEL,
        embedding_dimension=2,
        chunk_count=2,
        content_hash=RAG.context_signals.chunks_content_hash(
            written, schema=RAG.context_signals.LEGACY_CONTENT_HASH_SCHEMA),
        content_hash_schema=RAG.context_signals.LEGACY_CONTENT_HASH_SCHEMA,
    )
    _no_embed_server(monkeypatch, tmp_path)

    with pytest.raises(KnowledgeStoreError):
        KnowledgeBase(str(kb_path))

    assert not _legacy_npz(tmp_path).exists()


# ==========================================================================
# 空的 chunk_ids 要走「丟棄重建」，不是在錯誤訊息裡 IndexError
# ==========================================================================
def test_empty_chunk_ids_is_reported_not_crashed(tmp_path: Path, monkeypatch):
    _stub_embed(monkeypatch)
    kb_path = _save(tmp_path, _chunks("alpha body", "beta body"))
    payload = _read_cache(tmp_path)
    payload["chunk_ids"] = np.array([], dtype=payload["chunk_ids"].dtype)
    _write_cache(tmp_path, payload)
    _no_embed_server(monkeypatch, tmp_path)

    with pytest.raises(KnowledgeStoreError) as excinfo:
        KnowledgeBase(str(kb_path))

    assert "chunk id" in str(excinfo.value)


# ==========================================================================
# 舊快照不得刪除 / 覆蓋新世代的 cache（locate 這一條，rebuild 那條在上面）
# ==========================================================================
def test_a_stale_snapshot_never_discards_a_newer_generation_cache(
    tmp_path: Path, monkeypatch
):
    """A 讀到 gen1（讀完就放鎖）→ B 提交 gen2 → A 才拿 gen1 的身分去驗 gen2 的 cache。

    驗不過是必然的（本來就不是同一代），但**不可以**因此把 B 剛寫好的有效 cache
    刪掉：下一次查詢就得重算，而那一刻 embedding server 連不上就整個 KB 拒載。
    """
    _stub_embed(monkeypatch)
    kb_path = _save(tmp_path, _chunks("alpha body", "beta body"))
    old_snapshot = json.loads(kb_path.read_text(encoding="utf-8"))

    _save(tmp_path, _chunks("gen two one", "gen two two"))    # ← B 提交 gen2
    gen_two_cache = _cache_files(tmp_path)[0].read_bytes()

    matrices, stale = kb_cache.locate(
        kb_path, old_snapshot["chunks"], old_snapshot["metadata"])

    assert matrices is None and stale, "gen1 的身分當然驗不過 gen2 的 cache"
    assert _cache_files(tmp_path)[0].read_bytes() == gen_two_cache, "但不得刪掉它"
    _no_embed_server(monkeypatch, tmp_path)
    assert KnowledgeBase(str(kb_path)).loaded, "gen2 仍然可以直接載入，不需要重算"


def test_a_stale_snapshot_never_migrates_over_a_newer_generation_cache(
    tmp_path: Path, monkeypatch
):
    """legacy 遷移更危險：它是直接把舊向量**寫**到新一代的 cache 檔上。"""
    _stub_embed(monkeypatch)
    chunks = _chunks("alpha body", "beta body")
    kb_path = _write_legacy_pair(tmp_path, chunks, vectors=[[1.0, 0.0], [0.0, 1.0]])
    old_snapshot = json.loads(kb_path.read_text(encoding="utf-8"))

    _save(tmp_path, _chunks("gen two one", "gen two two"))    # ← B 提交 gen2
    gen_two_cache = _cache_files(tmp_path)[0].read_bytes()

    kb_cache.locate(kb_path, old_snapshot["chunks"], old_snapshot["metadata"])

    assert _cache_files(tmp_path)[0].read_bytes() == gen_two_cache, "不得被舊向量蓋掉"


def test_purge_does_not_delete_a_cache_that_a_writer_just_committed(
    tmp_path: Path, monkeypatch
):
    """「JSON 不存在」是在鎖外看到的；清除前必須在鎖內重新確認。"""
    _stub_embed(monkeypatch)
    kb_path = tmp_path / config.KNOWLEDGE_FILE
    _save(tmp_path, _chunks("alpha body", "beta body"))
    live_cache = _cache_files(tmp_path)[0].read_bytes()

    # 模擬「A 在 JSON 還不存在時就決定要清」：JSON 此刻是存在的（B 已提交完）
    removed = kb_cache.purge_orphans(kb_path)

    assert removed == []
    assert _cache_files(tmp_path)[0].read_bytes() == live_cache


# ==========================================================================
# 路徑安全的錯誤不得被「壞檔就丟掉」那條路徑吞掉
# ==========================================================================
def test_a_symlinked_cache_dir_is_raised_not_swallowed_into_a_discard(
    tmp_path: Path, monkeypatch
):
    """讀 cache 時撞到 symlink 是安全檢查點的錯，不是「壞檔」。

    吞掉它就會往下走到淘汰邏輯，用普通路徑去 unlink —— 而那條路徑此刻正指向
    sandbox 外的同名檔案。
    """
    _stub_embed(monkeypatch)
    kb_path = _save(tmp_path, _chunks("alpha body", "beta body"))
    snapshot = json.loads(kb_path.read_text(encoding="utf-8"))
    outside = tmp_path.parent / "outside-discard"
    outside.mkdir(exist_ok=True)
    decoy = outside / "embeddings.npz"
    decoy.write_bytes(b"someone else's file")

    inner = tmp_path / ".codetrail" / "cache" / "embeddings"
    shutil.rmtree(inner)
    (inner.parent / "embeddings").symlink_to(outside, target_is_directory=True)

    with pytest.raises(KnowledgeStoreError, match="symlink"):
        kb_cache.locate(kb_path, snapshot["chunks"], snapshot["metadata"])

    assert decoy.is_file(), "絕不可以刪到 sandbox 外的同名檔案"


def test_a_security_error_while_reading_is_never_treated_as_a_bad_cache(
    tmp_path: Path, monkeypatch
):
    """競態版：路徑在入口檢查之後才被換掉，錯誤是在**讀取**時才冒出來。

    那條 `except` 一旦把 KnowledgeStoreError 一起吃掉，就會被當成「壞檔」往下走到
    淘汰邏輯。這裡直接注入那個例外，確認它是往上拋而不是變成一次刪除。
    """
    _stub_embed(monkeypatch)
    kb_path = _save(tmp_path, _chunks("alpha body", "beta body"))
    snapshot = json.loads(kb_path.read_text(encoding="utf-8"))
    cache_before = _cache_files(tmp_path)[0].read_bytes()

    def boom(_json_path):
        raise KnowledgeStoreError("拒絕使用 …：它是 symlink（injected）")

    monkeypatch.setattr(kb_cache, "_read_cache_npz", boom)

    with pytest.raises(KnowledgeStoreError, match="symlink"):
        kb_cache.locate(kb_path, snapshot["chunks"], snapshot["metadata"])

    assert _cache_files(tmp_path)[0].read_bytes() == cache_before, "不得順手刪掉"


def test_same_generation_reader_never_deletes_a_freshly_rebuilt_cache(
    tmp_path: Path, monkeypatch
):
    """同一代的 ABA：A、B 都讀到同一份壞 cache，B 先修好，A 不得把它刪掉。

    世代檢查在這裡看不出差別（generation 一樣），所以答案不是「再多驗一次」，而是
    **根本不預先刪 primary**：它會被下一次成功的重建原子覆蓋。留著一份反正每次載入
    都會被拒絕的舊檔只花一點磁碟；刪掉別人剛修好的那份才是真的痛（下一次查詢又得
    重算，而那一刻 embedding server 連不上就整個 KB 拒載）。
    """
    _stub_embed(monkeypatch)
    kb_path = _save(tmp_path, _chunks("alpha body", "beta body"))
    snapshot = json.loads(kb_path.read_text(encoding="utf-8"))

    # A 讀到一份壞掉的 cache（generation 不變，只是內容雜湊被動過）
    payload = _read_cache(tmp_path)
    payload["content_hash"] = np.array("tampered")
    _write_cache(tmp_path, payload)
    matrices_a, stale_a = kb_cache.locate(
        kb_path, snapshot["chunks"], snapshot["metadata"])
    assert matrices_a is None and stale_a

    # B 在同一代裡把它重建好了
    kb_cache.rebuild(kb_path, snapshot["chunks"], snapshot["metadata"], reason="B")
    healthy = _cache_files(tmp_path)[0].read_bytes()

    # A 現在才走到「淘汰」那一步 —— 不得動到 B 的成果
    kb_cache.locate(kb_path, snapshot["chunks"], snapshot["metadata"])

    assert _cache_files(tmp_path)[0].read_bytes() == healthy
    _no_embed_server(monkeypatch, tmp_path)
    assert KnowledgeBase(str(kb_path)).loaded, "B 修好的 cache 必須還能直接用"


def test_backup_never_hardlinks_through_a_symlinked_cache_file(
    tmp_path: Path, monkeypatch
):
    """`os.link()` 預設 follow_symlinks=True：會把外部檔案連進 cache 當「舊版備份」。"""
    _stub_embed(monkeypatch)
    kb_path = _save(tmp_path, _chunks("alpha body", "beta body"))
    outside = tmp_path.parent / "outside-link"
    outside.mkdir(exist_ok=True)
    victim = outside / "secret.npz"
    victim.write_bytes(b"someone else's bytes")

    cache = _cache_files(tmp_path)[0]
    cache.unlink()
    cache.symlink_to(victim)

    # 事先擺好的連結會被入口的路徑檢查先攔下；競態版（檢查之後才被換掉）由
    # 本檔的 test_backup_refuses_a_symlinked_embedding_file（原 test_kb_store.py）守。
    with pytest.raises(KnowledgeStoreError, match="symlink|普通檔案"):
        RAG.save_knowledge_base(
            {"metadata": {"documents": ["spec.md"],
                          "embedding_model": config.EMBEDDING_MODEL},
             "chunks": _chunks("gamma body")}, kb_path)

    assert victim.read_bytes() == b"someone else's bytes"
    assert not list(outside.glob("*rollback*")), "外部檔案不得被連進 / 複製成備份"


# ==========================================================================
# 使用者只需要理解一個檔：備份／複製 knowledge.json 就夠
# ==========================================================================
def test_copying_only_the_json_to_a_new_directory_still_works(tmp_path: Path, monkeypatch):
    _stub_embed(monkeypatch)
    kb_path = _save(tmp_path, _chunks("alpha body", "beta body"))
    expected = _vectors(kb_path)

    elsewhere = tmp_path / "backup"
    elsewhere.mkdir()
    shutil.copy2(kb_path, elsewhere / config.KNOWLEDGE_FILE)

    assert _vectors(elsewhere / config.KNOWLEDGE_FILE) == expected


# ── 原 test_embedding_fail_loud.py:embedding 失敗必須 fail loud,不得回空向量 ──
# 注意:`clear_embedding_lru_caches` 原本就是 autouse fixture,合併後對整個 module 生效
# (只清 code_rag / knowledge 的 embedding lru_cache,其他區段的測試不經過那個 cache)。
EMBEDDING_MODULES = (code_rag, knowledge)


@pytest.fixture(autouse=True)
def clear_embedding_lru_caches():
    for module in EMBEDDING_MODULES:
        module._cached_get_embedding.cache_clear()
    yield
    for module in EMBEDDING_MODULES:
        module._cached_get_embedding.cache_clear()


def _assert_actionable_error(exc: pytest.ExceptionInfo[RuntimeError], module) -> None:
    message = str(exc.value)
    assert module.LLAMA_EMBED_BASE_URL in message
    assert "embedding llama-server" in message
    assert "deployment.json" in message


def test_code_rag_query_raises_when_embedding_server_is_unreachable(monkeypatch, tmp_path):
    rag = code_rag.CodeRAG(str(tmp_path))
    rag.index = [
        {
            "path": "sample.py",
            "symbol": "target_symbol",
            "type": "function",
            "line": 1,
            "context": "def target_symbol(): pass",
            "embedding": [1.0, 0.0],
        }
    ]
    monkeypatch.setattr(
        code_rag.llama_client,
        "embed_one",
        lambda **kwargs: (_ for _ in ()).throw(ConnectionError("connection refused")),
    )

    with pytest.raises(RuntimeError, match="embedding server unreachable") as exc:
        rag.query("target_symbol")

    _assert_actionable_error(exc, code_rag)


def test_code_rag_lazy_embedding_retries_after_server_recovers(monkeypatch, tmp_path):
    rag = code_rag.CodeRAG(str(tmp_path))
    rag.index = [
        {
            "path": "sample.py",
            "symbol": "target_symbol",
            "type": "function",
            "line": 1,
            "context": "def target_symbol(): pass",
        }
    ]
    rag._lazy_embed = True
    rag._lazy_embed_top_k = 1
    attempts = 0

    def flaky_embed(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise ConnectionError("temporary lazy-embed outage")
        return [1.0, 0.0]

    monkeypatch.setattr(code_rag.llama_client, "embed_one", flaky_embed)

    with pytest.raises(RuntimeError, match="temporary lazy-embed outage"):
        rag.query("target_symbol", top_k=1)

    results = rag.query("target_symbol", top_k=1)
    assert results[0]["symbol"] == "target_symbol"
    assert attempts == 3


def test_code_rag_build_failure_does_not_leave_partial_index(monkeypatch, tmp_path):
    rag = code_rag.CodeRAG(str(tmp_path))
    files = {}
    for name in ("a.py", "b.py"):
        path = tmp_path / name
        path.write_text("def target():\n    pass\n", encoding="utf-8")
        files[name] = {"filepath": path, "hash": name}

    outage = True

    def fake_index(filepath, rel_path, compute_embeddings=True):
        if rel_path == "b.py" and outage:
            raise RuntimeError("embedding server unreachable at test URL")
        return (
            [
                {
                    "path": rel_path,
                    "symbol": f"target_{rel_path[0]}",
                    "type": "function",
                    "line": 1,
                    "context": "def target(): pass",
                }
            ],
            [[1.0, 0.0]],
        )

    monkeypatch.setattr(rag, "_load_cache", lambda: False)
    monkeypatch.setattr(rag, "_scan_code_files", lambda: files)
    monkeypatch.setattr(rag, "_index_single_file", fake_index)
    monkeypatch.setattr(rag, "_save_cache", lambda: None)
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", False)

    with pytest.raises(RuntimeError, match="embedding server unreachable"):
        rag.build_index(verbose=False)

    assert rag.index == []

    outage = False
    rag.build_index(verbose=False)
    assert [item["path"] for item in rag.index] == ["a.py", "b.py"]


def test_knowledge_query_raises_when_embedding_server_is_unreachable(monkeypatch, tmp_path):
    kb = knowledge.KnowledgeBase(str(tmp_path / "missing.json"))
    kb.loaded = True
    kb.chunks = [
        {
            "id": "chunk-1",
            "source": "manual.md",
            "content": "target behavior",
            "embedding": [1.0, 0.0],
        }
    ]
    monkeypatch.setattr(
        knowledge.llama_client,
        "embed_one",
        lambda **kwargs: (_ for _ in ()).throw(ConnectionError("connection refused")),
    )

    with pytest.raises(RuntimeError, match="embedding server unreachable") as exc:
        kb.query("target behavior")

    _assert_actionable_error(exc, knowledge)


@pytest.mark.parametrize("module", EMBEDDING_MODULES)
def test_empty_embedding_vector_raises(module, monkeypatch):
    monkeypatch.setattr(module.llama_client, "embed_one", lambda **kwargs: [])

    with pytest.raises(RuntimeError, match="returned an empty vector") as exc:
        module._cached_get_embedding("empty-vector-probe")

    _assert_actionable_error(exc, module)


@pytest.mark.parametrize("module", EMBEDDING_MODULES)
def test_embedding_exception_is_not_cached_and_retry_succeeds(module, monkeypatch):
    attempts = 0

    def flaky_embed(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("temporary outage")
        return [0.25, 0.5]

    monkeypatch.setattr(module.llama_client, "embed_one", flaky_embed)

    with pytest.raises(RuntimeError, match="temporary outage"):
        module._cached_get_embedding("same-query-after-recovery")

    assert module._cached_get_embedding("same-query-after-recovery") == (0.25, 0.5)
    assert attempts == 2


def test_rag_ingestion_raises_instead_of_writing_empty_embedding(monkeypatch, tmp_path):
    chunks = [{"content": "document chunk"}]
    monkeypatch.setattr(
        RAG.llama_client,
        "embed_one",
        lambda **kwargs: (_ for _ in ()).throw(ConnectionError("connection refused")),
    )

    with pytest.raises(RuntimeError, match="embedding server unreachable") as exc:
        RAG.generate_embeddings(chunks, cache_dir=tmp_path)

    _assert_actionable_error(exc, RAG)
    assert "embedding" not in chunks[0]


def test_rag_ingestion_retries_legacy_empty_disk_cache(monkeypatch, tmp_path):
    content = "document chunk"
    cache_path = tmp_path / RAG.EMBEDDING_CACHE_FILE
    cache_path.write_text(
        json.dumps(
            {
                "model": RAG.EMBEDDING_MODEL,
                "cache": {RAG._content_hash(content): []},
            }
        ),
        encoding="utf-8",
    )
    calls = 0

    def recovered_embed(**kwargs):
        nonlocal calls
        calls += 1
        return [0.25, 0.5]

    monkeypatch.setattr(RAG.llama_client, "embed_one", recovered_embed)
    chunks = RAG.generate_embeddings([{"content": content}], cache_dir=tmp_path)

    assert calls == 1
    assert chunks[0]["embedding"] == [0.25, 0.5]


# ── 原 test_rag_npz.py:knowledge.json ↔ embeddings cache 的向量持久化(舊位置 NPZ 的遷移路徑)──
# fixture 用的 store generation:JSON 與 NPZ 兩邊要一致,才是一組「證明得了身分」
# 的舊檔。少了它 + content-v1 串接雜湊 + 沒有逐列 id,`["ab","c"]` 與 `["a","bc"]`
# 這種重新切分是驗不出來的,載入端因此一律重建(2026-08-24 審核第二輪)。
_GENERATION = "fixture-generation-0001"


def _content_hash(chunks: list[dict]) -> str:
    """與寫入端同一份實作（現行 schema 有長度前綴，不會被串接碰撞騙過）。"""
    return context_signals.chunks_content_hash(
        chunks, schema=context_signals.CONTENT_INPUT_SCHEMA
    )


def test_load_knowledge_base_restores_external_npz_embeddings(tmp_path):
    kb_path = tmp_path / "knowledge.json"
    chunks = [
        {"source": "old.md", "page": 1, "chunk_index": 0, "content": "alpha"},
        {"source": "old.md", "page": 1, "chunk_index": 1, "content": "beta"},
    ]
    kb_path.write_text(
        json.dumps({"metadata": {"documents": ["old.md"],
                                 "store_generation": _GENERATION},
                    "chunks": chunks}, ensure_ascii=False),
        encoding="utf-8",
    )
    np.savez_compressed(
        tmp_path / config.KNOWLEDGE_EMB_FILE,
        embeddings=np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
        embedding_model=config.EMBEDDING_MODEL,
        chunk_count=2,
        content_hash=_content_hash(chunks),
        content_hash_schema=context_signals.CONTENT_INPUT_SCHEMA,
        store_generation=_GENERATION,
        **kb_cache.section_fields(chunks, None, dimension=2),
    )

    kb = load_knowledge_base(kb_path)

    assert kb["chunks"][0]["embedding"] == [1.0, 0.0]
    assert kb["chunks"][1]["embedding"] == [0.0, 1.0]


def test_incremental_save_preserves_old_embeddings_from_npz(tmp_path):
    kb_path = tmp_path / "knowledge.json"
    old_chunks = [
        {"source": "old.md", "page": 1, "chunk_index": 0, "content": "alpha"},
        {"source": "old.md", "page": 1, "chunk_index": 1, "content": "beta"},
    ]
    kb_path.write_text(
        json.dumps(
            {
                "metadata": {
                    "documents": ["old.md"],
                    "total_documents": 1,
                    "total_chunks": 2,
                    "store_generation": _GENERATION,
                },
                "chunks": old_chunks,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    np.savez_compressed(
        tmp_path / config.KNOWLEDGE_EMB_FILE,
        embeddings=np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
        embedding_model=config.EMBEDDING_MODEL,
        chunk_count=2,
        content_hash=_content_hash(old_chunks),
        content_hash_schema=context_signals.CONTENT_INPUT_SCHEMA,
        store_generation=_GENERATION,
        **kb_cache.section_fields(old_chunks, None, dimension=2),
    )
    kb = load_knowledge_base(kb_path)
    kb["chunks"].append(
        {
            "source": "new.md",
            "page": 1,
            "chunk_index": 0,
            "content": "gamma",
            "embedding": [1.0, 1.0],
        }
    )
    kb["metadata"]["documents"].append("new.md")

    save_knowledge_base(kb, kb_path)

    # 向量現在住在程式自管的隱藏 cache,不再與 JSON 同目錄
    data = np.load(kb_cache.cache_file(kb_path))
    embeddings = data["embeddings"]
    assert embeddings.shape == (3, 2)
    assert np.allclose(embeddings[0], [1.0, 0.0])
    assert np.allclose(embeddings[1], [0.0, 1.0])
    assert np.allclose(embeddings[2], [0.70710677, 0.70710677])


def test_save_empty_knowledge_base_removes_stale_npz(tmp_path):
    kb_path = tmp_path / "knowledge.json"
    stale_npz = tmp_path / config.KNOWLEDGE_EMB_FILE
    np.savez_compressed(
        stale_npz,
        embeddings=np.array([[1.0]], dtype=np.float32),
        embedding_model=config.EMBEDDING_MODEL,
        chunk_count=1,
        content_hash="stale",
    )

    save_knowledge_base(
        {
            "metadata": {
                "created_at": "now",
                "embedding_model": config.EMBEDDING_MODEL,
                "chunk_size": 1200,
                "documents": [],
            },
            "chunks": [],
        },
        kb_path,
    )

    assert kb_path.exists()
    assert not stale_npz.exists()


# --------------------------------------------------------------------------
# 併自 tests/test_npz_embedding_attach.py(P0-5):
# 從 .npz 載入 embeddings 後必須把向量掛回每個 chunk,否則下游 MMR /
# 污染控制 / 信心分數都會拿到空向量,相似度全當 0。
# --------------------------------------------------------------------------
# smoke:AGENTS.md §1.1 第 1 款「真實發生過的 bug 的 regression」
# 真實 bug regression(P0-5):npz 向量沒掛回 chunk → 相似度全 0。


def _build_kb_files(tmp_path: Path, n: int = 4, dim: int = 8):
    """造出「knowledge.json（無 inline embedding）+ 相容 .npz」的一組檔案。"""
    chunks = [
        {"content": f"chunk number {i} about spec value {i * 100}",
         "source": "doc.pdf", "type": "text"}
        for i in range(n)
    ]
    json_path = tmp_path / config.KNOWLEDGE_FILE
    json_path.write_text(
        json.dumps({
            "chunks": chunks,
            "metadata": {"embedding_model": knowledge.EMBEDDING_MODEL, "documents": [],
                         "store_generation": _GENERATION},
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    # L2-normalized 隨機向量（正規化與真實 .npz 一致）
    rng = np.arange(1, n * dim + 1, dtype=np.float32).reshape(n, dim)
    rng = rng / np.linalg.norm(rng, axis=1, keepdims=True)

    emb_path = tmp_path / config.KNOWLEDGE_EMB_FILE
    np.savez_compressed(
        emb_path,
        embeddings=rng,
        embedding_model=knowledge.EMBEDDING_MODEL,
        chunk_count=n,
        content_hash=_content_hash(chunks),
        content_hash_schema=context_signals.CONTENT_INPUT_SCHEMA,
        store_generation=_GENERATION,
        **kb_cache.section_fields(chunks, None, dimension=dim),
    )
    return json_path, rng


def test_npz_load_attaches_embeddings_to_chunks(tmp_path: Path):
    json_path, rng = _build_kb_files(tmp_path, n=4, dim=8)

    kb = KnowledgeBase(str(json_path))
    assert kb.loaded

    # self._embeddings 有載到
    assert kb._embeddings is not None
    assert kb._embeddings.shape == (4, 8)

    # 關鍵：每個 chunk 都要拿到非空 embedding，且與 .npz 對應列一致
    for i, chunk in enumerate(kb.chunks):
        emb = chunk.get("embedding")
        assert emb, f"chunk {i} 載入 .npz 後 embedding 仍是空的（P0-5 回歸）"
        assert len(emb) == 8
        assert emb == pytest.approx(rng[i].tolist(), rel=1e-5)


def test_npz_attached_embeddings_drive_nonzero_similarity(tmp_path: Path):
    """回填後，用 chunk["embedding"] 算 cosine 應該拿到非零分數（不再全 0）。"""
    json_path, rng = _build_kb_files(tmp_path, n=3, dim=8)
    kb = KnowledgeBase(str(json_path))

    # 直接用某個 chunk 自己的向量當 query，cosine 應接近 1（而非 0）
    q = kb.chunks[1]["embedding"]
    sims = [kb._cosine_similarity(q, c["embedding"]) for c in kb.chunks]
    assert max(sims) == pytest.approx(1.0, abs=1e-4)
    # 自我相似度必須是最高的那個
    assert sims.index(max(sims)) == 1
