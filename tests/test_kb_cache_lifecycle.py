"""knowledge.json 是唯一要人管的 KB 檔；embeddings 是程式自管的 cache。

契約（本檔逐條守住）：

1. `knowledge.json` 是 chunk 文字 / metadata / generation 身分的唯一真相。
2. 向量放在隱藏的 cache（`.codetrail/cache/embeddings/<kb-id>/`），使用者不需要
   知道它存在：缺了自動重建、身分不符一律丟棄重建、重建不了就 fail-loud，
   **永遠不准拿舊向量湊合**。
3. JSON 不在了 ＝ 空 KB，而且無主 cache（含舊位置的 companion NPZ）要被清掉。
4. `.codetrail/figures/` 不是 cache，fresh ingest 一個位元組都不准動它。

為什麼一定要 smoke（AGENTS.md §2.1 第 2 款「無聲失敗風險的契約」）：這裡每一條
的失敗模式都是**靜默錯答**——向量與 chunk 錯位一列，查詢照樣回答，只是答錯，
而且沒有任何一行 log 會說出來。
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

import config
import knowledge_store
import RAG
from knowledge import KnowledgeBase
from knowledge_store import KnowledgeStoreError

np = pytest.importorskip("numpy")

pytestmark = pytest.mark.smoke


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

    def fail_json_publish(source, destination):
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
    """舊格式：knowledge.json ＋ 同目錄的 knowledge_emb.npz（沒有 chunk_ids）。"""
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
    np.savez_compressed(
        _legacy_npz(tmp_path),
        embeddings=rows,
        embedding_model=config.EMBEDDING_MODEL,
        embedding_dimension=rows.shape[1],
        chunk_count=rows.shape[0],
        content_hash=RAG.context_signals.chunks_content_hash(chunks, schema=schema),
        content_hash_schema=schema,
        store_generation="legacy-gen",
    )
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
