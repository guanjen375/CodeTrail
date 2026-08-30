"""KnowledgeBase 的儲存層:staleness 偵測、載入失敗保護、刪除文件後的重寫。

合併自 tests/test_kb_staleness.py 與 tests/test_rag_store_regressions.py(2026-08-20)。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

import config
import kb_cache
import knowledge
import knowledge_store
import RAG
from knowledge import KnowledgeBase

# smoke:AGENTS.md §1.1 第 1 款「真實發生過的 bug 的 regression」
# 真實 bug regression:KB staleness 誤判與刪文件後的 npz 重寫。
pytestmark = pytest.mark.smoke


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
