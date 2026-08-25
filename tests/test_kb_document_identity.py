"""KB 文件身分:同 basename、不同來源檔一律 fail-loud。

知識庫用 **basename** 當文件識別(`chunk["source"]` 與
`metadata["documents"]` 都是 `Path(x).name`)。這讓「不同目錄下的同名文件」
在入庫時互相覆蓋,而且訊息長得跟正常更新一模一樣:

    [INFO] 更新現有文件: spec.pdf

於是 `a/spec.pdf` 的內容整份消失,查詢照樣回答,只是答的是 `b/spec.pdf`。
沒有例外、沒有 warning、chunk 數還是「合理」的——這是無聲錯答,不是失敗。
`remove_document("spec.pdf")` 同樣會刪到非預期的那一份。

契約:`metadata["document_sources"]` 記下每份文件的來源身分,入庫時比對;
對不上就 raise `DocumentIdentityConflict`,**零寫入**。

舊 KB 沒有這份紀錄,無法回溯判斷,所以只警告並採用這一次的來源——但下一次
再撞就擋得住了。這條界線是刻意的:發明沒有的歷史比沉默更糟。

全部離線:embedding 以 monkeypatch 換成固定向量,不碰任何 server。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config
import RAG
from knowledge_store import DocumentIdentityConflict

pytestmark = pytest.mark.smoke


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
    import knowledge_store

    metadata = {"documents": ["spec.md"]}
    warning = knowledge_store.check_document_identity(
        metadata, "spec.md", "/somewhere/spec.md"
    )
    assert warning and "spec.md" in warning


def test_missing_identity_keeps_legacy_behaviour():
    """呼叫端給不出身分(例如舊的內部呼叫)時不擋,也不寫壞既有紀錄。"""
    import knowledge_store

    metadata = {"documents": ["spec.md"], "document_sources": {"spec.md": "/a/spec.md"}}
    assert knowledge_store.check_document_identity(metadata, "spec.md", "") is None
    knowledge_store.record_document_identity(metadata, "spec.md", "")
    assert metadata["document_sources"] == {"spec.md": "/a/spec.md"}


def test_identity_records_are_pruned_to_the_document_list():
    import knowledge_store

    metadata = {
        "documents": ["kept.md"],
        "document_sources": {"kept.md": "/a/kept.md", "gone.md": "/a/gone.md"},
    }
    knowledge_store.sync_document_identities(metadata)
    assert metadata["document_sources"] == {"kept.md": "/a/kept.md"}


def test_conflict_message_names_both_sides_and_the_way_out():
    import knowledge_store

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
