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
import knowledge
import knowledge_store
import RAG
from knowledge import KnowledgeBase

# smoke:AGENTS.md §2.1 第 1 款「真實發生過的 bug 的 regression」
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


def test_incremental_restore_rejects_embedding_model_mismatch(tmp_path: Path):
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

    with pytest.raises(RuntimeError, match="embedding model"):
        RAG.load_knowledge_base(path)


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
    embeddings_path = tmp_path / config.KNOWLEDGE_EMB_FILE
    RAG.save_knowledge_base(_kb(), path)
    original_json = path.read_bytes()
    original_npz = embeddings_path.read_bytes()

    changed = _kb()
    changed["chunks"][0]["content"] = "replacement content"
    real_replace = knowledge_store.os.replace

    def fail_json_publish(source, destination):
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
    assert not any("rollback" in item.name or ".tmp." in item.name for item in tmp_path.iterdir())


def test_remove_document_rewrites_remaining_npz_and_reload_keeps_dense_search(monkeypatch, tmp_path: Path):
    path = tmp_path / config.KNOWLEDGE_FILE
    kb = _kb()
    RAG.save_knowledge_base(kb, path)
    remove = getattr(RAG, "remove_document_from_knowledge_base", None)
    assert callable(remove), "RAG must expose the shared transactional removal path"

    result = remove(path, "drop.md")
    assert result["removed_chunks"] == 1

    data = np.load(tmp_path / config.KNOWLEDGE_EMB_FILE)
    assert data["embeddings"].shape == (1, 2)
    loaded = KnowledgeBase(str(path))
    assert loaded.loaded
    assert [c["source"] for c in loaded.chunks] == ["keep.md"]
    assert loaded._embeddings is not None
    assert loaded._embeddings.shape == (1, 2)

    monkeypatch.setattr(loaded, "_get_embedding", lambda _text: [1.0, 0.0])
    rows = loaded._hybrid_search("KEEP 0x1000", candidate_k=5)
    assert rows and rows[0].chunk["source"] == "keep.md"


def test_remove_aborts_if_vectors_are_missing_and_leaves_json_unchanged(tmp_path: Path):
    path = tmp_path / config.KNOWLEDGE_FILE
    kb = _kb()
    RAG.save_knowledge_base(kb, path)
    (tmp_path / config.KNOWLEDGE_EMB_FILE).unlink()
    before = path.read_bytes()
    remove = getattr(RAG, "remove_document_from_knowledge_base", None)
    assert callable(remove)

    with pytest.raises(RuntimeError, match="embedding"):
        remove(path, "drop.md")

    assert path.read_bytes() == before
