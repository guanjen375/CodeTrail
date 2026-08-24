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
import kb_cache
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
# ★ 安全檢查點（AGENTS.md §3）：cache 路徑上的 symlink 一律 fail-closed
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
