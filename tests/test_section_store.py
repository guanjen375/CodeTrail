"""Section-store safety contracts: full inputs, row identity and atomic reuse.

Only synthetic chunks and injected embedding functions; no PDF parsing,
private KB, live server or model-quality assertions.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import config
import context_signals
import kb_cache
import knowledge_store
import RAG
import section_index
from knowledge import KnowledgeBase

np = pytest.importorskip("numpy")
pytestmark = pytest.mark.smoke


def _chunk(index, text, *, source="guide.md", title="Control", page=1,
           section_number=0, **extra):
    chunk = {
        "source": source, "section": title, "heading_hierarchy": title,
        "section_index": section_number, "page": page, "chunk_index": index,
        "content": text, "type": "spec", "embedding": [1.0, 0.0], **extra,
    }
    chunk["id"] = knowledge_store.chunk_id(chunk)
    return chunk


def _prepared(chunks):
    return kb_cache.PreparedSections(vectors={
        node.fingerprint: (1.0, 0.0) for node in section_index.build_sections(chunks)
    })


def _save_pair(directory, chunks, *, legacy=False):
    path = directory / "knowledge.json"
    schema = context_signals.CONTENT_INPUT_SCHEMA
    data = {"metadata": {"documents": sorted({c["source"] for c in chunks})},
            "chunks": chunks}
    target = kb_cache.prepare_cache_target(path)
    fields = None if legacy else kb_cache.section_fields(chunks, _prepared(chunks), dimension=2)
    with kb_cache.cache_dir_fd(path, create=True) as fd:
        knowledge_store.save_knowledge_store_atomic(
            data, path, embedding_file=target, embedding_dir_fd=fd,
            embedding_model=config.EMBEDDING_MODEL,
            content_hash=context_signals.chunks_content_hash(chunks, schema=schema),
            content_hash_schema=schema, chunk_ids=kb_cache.chunk_row_ids(chunks),
            section_fields=fields,
        )
    return path


def _offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("embedding work is forbidden in this phase")
    monkeypatch.setattr(RAG, "generate_embeddings", forbidden)
    return forbidden


def test_sections_preserve_source_occurrences_and_complete_body():
    chunks = [
        _chunk(0, "first page", page=1),
        _chunk(1, "  code();\n\tTAIL = 0x91;", page=2),
        _chunk(2, "other section", title="Other", page=2, section_number=1),
        _chunk(3, "repeated title", page=2, section_number=2),
        _chunk(0, "different document", source="other.md", page=1),
    ]
    before = copy.deepcopy(chunks)
    nodes = section_index.build_sections(chunks)
    assert len(nodes) == 4
    assert nodes[0].member_indices == (0, 1)
    assert nodes[0].page_range == (1, 2)
    assert "  code();\n\tTAIL = 0x91;" in nodes[0].text
    assert nodes[0].node_id != nodes[2].node_id
    assert nodes[0].node_id != nodes[3].node_id
    assert all(len({chunks[i]["source"] for i in n.member_indices}) == 1 for n in nodes)
    assert chunks == before, "building nodes must not modify source chunks or their vectors"


def test_section_source_identity_does_not_trim_distinct_document_names():
    chunks = [_chunk(0, "first source", source="guide.md"),
              _chunk(0, "second source", source=" guide.md"),
              _chunk(1, "figure payload", source=" guide.md", structured=True)]
    nodes = section_index.build_sections(chunks)
    assert [node.source for node in nodes] == ["guide.md", " guide.md"]
    assert [node.member_indices for node in nodes] == [(0,), (1, 2)]
    assert nodes[0].node_id != nodes[1].node_id


def test_legacy_section_runs_do_not_merge_repeated_titles_or_guess_prefixes():
    chunks = [_chunk(0, "unknown legacy prefix\nA", section_number=-1),
              _chunk(1, "B", title="Other", section_number=-1),
              _chunk(2, "C", section_number=-1)]
    nodes = section_index.build_sections(chunks)
    assert [n.member_indices for n in nodes] == [(0,), (1,), (2,)]
    assert "unknown legacy prefix\nA" in nodes[0].text
    assert nodes[0].node_id != nodes[2].node_id
    prefixed = _chunk(0, "[HEADING] Control\noverlap\nRAW", ctx="invented claim",
                      heading_prefix_chars=len("[HEADING] Control\n"),
                      overlap_prefix_chars=len("overlap\n"))
    node = section_index.build_sections([prefixed])[0]
    assert node.text == "Control\nRAW"
    assert "invented claim" not in node.text


def test_figure_membership_changes_without_reusing_stale_members_or_reembedding(monkeypatch):
    text = _chunk(0, "authoritative text", page=2)
    figure = _chunk(1, "unverified table V1", page=2, structured=True,
                    figure_id="fig_0123456789abcdef", char_start=0, char_end=0,
                    section_number=-1, verification_status="needs_review")
    chunks = [text, figure]
    old = section_index.build_sections(chunks)[0]
    revised = copy.deepcopy(chunks)
    revised[1]["content"] = "corrected table V2"
    revised[1]["revision"] = 2
    revised[1]["id"] = knowledge_store.chunk_id(revised[1])
    new = section_index.build_sections(revised)[0]
    assert old.member_indices == new.member_indices == (0, 1)
    assert old.text == new.text and "table" not in old.text
    assert old.fingerprint == new.fingerprint and old.node_id == new.node_id
    assert old.member_ids != new.member_ids
    assert section_index.membership_hash([old]) != section_index.membership_hash([new])
    _offline(monkeypatch)
    reused = kb_cache.prepare_sections(revised, cache_dir=Path("unused"),
                                       reusable=_prepared(chunks))
    assert kb_cache.section_fields(revised, reused, dimension=2)["section_count"] == 1


def test_ambiguous_figure_is_never_attached_by_zero_char_offset():
    chunks = [_chunk(0, "first occurrence", page=1, section_number=0),
              _chunk(1, "second occurrence", page=1, section_number=1),
              _chunk(2, "table", page=1, structured=True, char_start=0, char_end=0)]
    nodes = section_index.build_sections(chunks)
    assert len(nodes) == 2
    assert all(2 not in node.member_indices for node in nodes)


def test_title_only_text_anchor_keeps_its_section_and_unique_figure_member():
    chunks = [_chunk(0, "Control", heading_prefix_chars=len("Control")),
              _chunk(1, "figure-only body", structured=True)]
    nodes = section_index.build_sections(chunks)
    assert len(nodes) == 1 and nodes[0].member_indices == (0, 1)
    assert nodes[0].text == "Control\n"
    assert "figure-only body" not in nodes[0].text


def test_section_windows_cover_the_tail_once_and_use_the_ingest_embedding_hook(monkeypatch, tmp_path):
    chunks = [_chunk(0, "BEGIN\n" + "x" * 9000 + "\nTAIL=0xCAFE")]
    node = section_index.build_sections(chunks)[0]
    windows = section_index.embedding_windows(node)
    assert len(windows) >= 3
    assert "".join(w["content"] for w in windows) == node.text
    assert all(len(context_signals.retrieval_embedding_input(w, use_ctx=True))
               <= section_index.WINDOW_MAX_CHARS for w in windows)
    sent = []
    def embed(parts, *, cache_dir, with_gate):
        assert cache_dir == tmp_path and with_gate is False
        sent.extend(copy.deepcopy(parts))
        for part in parts:
            part["embedding"] = [0.0, 1.0] if "TAIL=0xCAFE" in part["content"] else [1.0, 0.0]
        return parts
    monkeypatch.setattr(RAG, "generate_embeddings", embed)
    prepared = kb_cache.prepare_sections(chunks, cache_dir=tmp_path)
    assert "".join(w["content"] for w in sent) == node.text
    vector = prepared.vectors[node.fingerprint]
    assert vector[1] > 0.0, "the whole tail window must contribute to the single section row"
    assert np.linalg.norm(vector) == pytest.approx(1.0)
    _offline(monkeypatch)
    assert kb_cache.prepare_sections(chunks, cache_dir=tmp_path,
                                     reusable=prepared).vectors == prepared.vectors


@pytest.mark.parametrize("failure", ["raise", "short", "zero", "nan", "dimension", "reorder"])
def test_section_window_failure_never_returns_or_changes_partial_vectors(monkeypatch, tmp_path, failure):
    chunks = [_chunk(0, "a" * 9000 + "tail")]
    prior = kb_cache.PreparedSections(vectors={"unrelated": (1.0, 0.0)})
    before = copy.deepcopy(prior)
    def embed(parts, **kwargs):
        for part in parts:
            part["embedding"] = [1.0, 0.0]
        if failure == "raise":
            raise RuntimeError("last window unavailable")
        if failure == "short":
            return parts[:-1]
        if failure == "reorder":
            return list(reversed(parts))
        parts[-1]["embedding"] = {"zero": [0.0, 0.0], "nan": [float("nan"), 0.0],
                                  "dimension": [1.0, 0.0, 0.0]}[failure]
        return parts
    monkeypatch.setattr(RAG, "generate_embeddings", embed)
    with pytest.raises((knowledge_store.KnowledgeStoreError, RuntimeError)):
        kb_cache.prepare_sections(chunks, cache_dir=tmp_path, reusable=prior)
    assert prior == before


def test_section_identity_roundtrips_without_source_parse_or_second_embedding(monkeypatch, tmp_path):
    chunks = [_chunk(0, "full text", char_start=0, char_end=9),
              _chunk(1, "last page", page=2, char_start=10, char_end=19)]
    path = _save_pair(tmp_path, chunks)
    before = kb_cache.cache_file(path).read_bytes()
    _offline(monkeypatch)
    expected = section_index.build_sections(chunks)
    loaded = KnowledgeBase(str(path), allow_rebuild=False)
    assert loaded.loaded
    disk = json.loads(path.read_text(encoding="utf-8"))
    actual = section_index.build_sections(disk["chunks"])
    assert actual == expected
    assert kb_cache.cache_file(path).read_bytes() == before
    assert not (tmp_path / "guide.md").exists(), "there is no source to reparse"


@pytest.mark.parametrize("allow_rebuild", [False, True])
def test_legacy_sectionless_cache_cannot_be_used_when_rebuild_is_unavailable(monkeypatch, tmp_path, allow_rebuild):
    path = _save_pair(tmp_path, [_chunk(0, "old text")], legacy=True)
    before = kb_cache.cache_file(path).read_bytes()
    _offline(monkeypatch)
    with pytest.raises(knowledge_store.KnowledgeStoreError, match="section"):
        KnowledgeBase(str(path), allow_rebuild=allow_rebuild)
    assert kb_cache.cache_file(path).read_bytes() == before


def test_legacy_sectionless_cache_rebuilds_from_json_with_complete_section_rows(monkeypatch, tmp_path):
    path = _save_pair(tmp_path, [_chunk(0, "old text"), _chunk(1, "whole tail", page=2)], legacy=True)
    calls = []
    def embed(parts, **kwargs):
        calls.append([part["content"] for part in parts])
        for part in parts:
            part["embedding"] = [1.0, 0.0]
        return parts
    monkeypatch.setattr(RAG, "generate_embeddings", embed)
    loaded = KnowledgeBase(str(path))
    assert loaded.loaded and len(calls) == 2  # Original chunks, then complete section windows.
    with np.load(kb_cache.cache_file(path), allow_pickle=False) as data:
        assert set(kb_cache.SECTION_NPZ_FIELDS) <= set(data.files)
        assert data["embeddings"].shape == (2, 2)
        assert data["section_embeddings"].shape == (1, 2)
    _offline(monkeypatch)
    assert KnowledgeBase(str(path)).loaded


def test_mineru_heading_without_ctx_requires_gate_during_cache_rebuild(monkeypatch, tmp_path):
    chunks = [_chunk(0, "original body", heading_source="mineru")]
    path = _save_pair(tmp_path, chunks)
    calls = []
    def embed(parts, *, cache_dir, with_gate):
        calls.append(with_gate)
        for part in parts:
            part["embedding"] = [1.0, 0.0]
            if with_gate:
                part["embedding_gate"] = [0.0, 1.0]
        return parts
    monkeypatch.setattr(RAG, "generate_embeddings", embed)
    loaded = KnowledgeBase(str(path))
    assert loaded.loaded and calls == [True, False]
    assert not context_signals.has_any_ctx(loaded.chunks)
    payload = kb_cache._read_cache_npz(path)
    assert payload["content_hash_schema"] == context_signals.CONTEXTUAL_INPUT_SCHEMA
    assert payload["gate_content_hash_schema"] == context_signals.GATE_SCHEMA
    assert payload["embeddings_gate"].tolist() == [[0.0, 1.0]]
    assert payload["section_embeddings"].shape == (1, 2)
    _offline(monkeypatch)
    assert KnowledgeBase(str(path), allow_rebuild=False).loaded
    disk = json.loads(path.read_text(encoding="utf-8"))
    payload["embeddings_gate"] = None
    assert "gate" in kb_cache._verify(
        payload, chunks=disk["chunks"], metadata=disk["metadata"], strict_identity=True)


@pytest.mark.parametrize("field", [
    "section_schema", "section_ids", "section_fingerprints", "section_membership_hash",
    "section_content_hash", "section_count", "section_embedding_dimension",
    "section_embeddings", "section_nonfinite", "section_zero", "section_scaled",
    "section_missing",
])
def test_section_cache_identity_or_vector_corruption_never_loads(monkeypatch, tmp_path, field):
    chunks = [_chunk(0, "A"), _chunk(1, "B", title="Other", section_number=1)]
    path = _save_pair(tmp_path, chunks)
    cache = kb_cache.cache_file(path)
    with np.load(cache, allow_pickle=False) as data:
        payload = {name: data[name].copy() for name in data.files}
    if field in {"section_ids", "section_fingerprints"}:
        payload[field] = payload[field][::-1]
    elif field in {"section_count", "section_embedding_dimension"}:
        payload[field] = 99
    elif field == "section_embeddings":
        payload[field] = np.zeros((1, 2), dtype=np.float32)
    elif field in {"section_nonfinite", "section_zero", "section_scaled"}:
        payload["section_embeddings"][0] = {
            "section_nonfinite": [float("nan"), 0.0],
            "section_zero": [0.0, 0.0],
            "section_scaled": [100.0, 0.0],
        }[field]
    elif field == "section_missing":
        payload.pop("section_schema")
    else:
        payload[field] = "tampered"
    np.savez_compressed(cache, **payload)
    _offline(monkeypatch)
    with pytest.raises(knowledge_store.KnowledgeStoreError, match="section"):
        KnowledgeBase(str(path), allow_rebuild=False)


def test_section_metadata_and_window_policy_changes_require_new_vectors(monkeypatch, tmp_path):
    chunks = [_chunk(0, "same body")]
    prepared = _prepared(chunks)
    for field, value in [("page", 2), ("heading_hierarchy", "Parent > Control"),
                         ("char_end", 200), ("section_index", 5)]:
        changed = copy.deepcopy(chunks)
        changed[0][field] = value
        with pytest.raises(knowledge_store.KnowledgeStoreError, match="fingerprint"):
            kb_cache.section_fields(changed, prepared, dimension=2)
    path = _save_pair(tmp_path, chunks)
    _offline(monkeypatch)
    monkeypatch.setattr(section_index, "WINDOW_MAX_CHARS", section_index.WINDOW_MAX_CHARS + 1)
    with pytest.raises(knowledge_store.KnowledgeStoreError, match="section schema"):
        KnowledgeBase(str(path), allow_rebuild=False)


def test_section_membership_only_save_and_source_removal_need_no_embedding(monkeypatch, tmp_path):
    chunks = [_chunk(0, "body"), _chunk(1, "figure V1", structured=True),
              _chunk(0, "other body", source="remove.md")]
    path = _save_pair(tmp_path, chunks)
    _offline(monkeypatch)
    kb = RAG.load_knowledge_base(path)
    assert isinstance(kb["_section_vectors"], kb_cache.PreparedSections)
    old_vectors = dict(kb["_section_vectors"].vectors)
    kb["chunks"][1]["content"] = "figure V2"
    kb["chunks"][1]["id"] = knowledge_store.chunk_id(kb["chunks"][1])
    RAG.save_knowledge_base(kb, path, _already_locked=False)
    changed = RAG.load_knowledge_base(path)
    assert changed["_section_vectors"].vectors == old_vectors
    result = RAG.remove_document_from_knowledge_base(path, "remove.md")
    assert result["removed_chunks"] == 1
    disk = json.loads(path.read_text(encoding="utf-8"))
    loaded, stale = kb_cache.locate(path, disk["chunks"], disk["metadata"], mutate=False)
    assert not stale and loaded is not None
    assert [node.source for node in loaded.section_nodes] == ["guide.md"]
    assert loaded.section_nodes[0].member_ids[1] == kb["chunks"][1]["id"]


def test_section_store_atomic_failure_restores_every_matrix(monkeypatch, tmp_path):
    chunks = [_chunk(0, "original")]
    path = _save_pair(tmp_path, chunks)
    before_json = path.read_bytes()
    before_npz = kb_cache.cache_file(path).read_bytes()
    original = knowledge_store.os.replace
    def replace(source, destination, **kwargs):
        if not kwargs and Path(destination) == path and ".tmp." in Path(source).name:
            raise OSError("injected section transaction failure")
        return original(source, destination, **kwargs)
    monkeypatch.setattr(knowledge_store.os, "replace", replace)
    with pytest.raises(OSError, match="section transaction"):
        _save_pair(tmp_path, [_chunk(0, "replacement")])
    assert path.read_bytes() == before_json
    assert kb_cache.cache_file(path).read_bytes() == before_npz


def test_section_store_rejects_stale_text_fields_before_replacing_the_pair(monkeypatch, tmp_path):
    original = [_chunk(0, "original text")]
    path = _save_pair(tmp_path, original)
    fields = kb_cache.section_fields(original, _prepared(original), dimension=2)
    replacement = [_chunk(0, "different text")]
    before = (path.read_bytes(), kb_cache.cache_file(path).read_bytes())
    _offline(monkeypatch)
    with kb_cache.cache_dir_fd(path, create=False) as fd:
        with pytest.raises(knowledge_store.KnowledgeStoreError, match="fingerprint"):
            knowledge_store.save_knowledge_store_atomic(
                {"metadata": {"documents": ["guide.md"]}, "chunks": replacement},
                path, embedding_file=kb_cache.cache_file(path), embedding_dir_fd=fd,
                embedding_model=config.EMBEDDING_MODEL,
                content_hash=context_signals.chunks_content_hash(
                    replacement, schema=context_signals.CONTENT_INPUT_SCHEMA),
                content_hash_schema=context_signals.CONTENT_INPUT_SCHEMA,
                chunk_ids=kb_cache.chunk_row_ids(replacement), section_fields=fields,
            )
    assert (path.read_bytes(), kb_cache.cache_file(path).read_bytes()) == before


def test_zero_sections_are_explicit_and_readonly_missing_cache_creates_no_directory(monkeypatch, tmp_path):
    chunks = [_chunk(0, "unsectioned", title="", section_number=-1)]
    _offline(monkeypatch)
    prepared = kb_cache.prepare_sections(chunks, cache_dir=tmp_path)
    fields = kb_cache.section_fields(chunks, prepared, dimension=2)
    assert set(fields) == set(kb_cache.SECTION_NPZ_FIELDS)
    assert fields["section_embeddings"].shape == (0, 2)
    assert fields["section_ids"].tolist() == []
    path = tmp_path / "knowledge.json"
    path.write_text(json.dumps({"chunks": chunks, "metadata": {}}), encoding="utf-8")
    before = sorted(tmp_path.iterdir())
    matrices, reason = kb_cache.locate(path, chunks, {}, mutate=False)
    assert matrices is None and reason
    assert sorted(tmp_path.iterdir()) == before


def test_section_cache_rebuild_does_not_publish_over_newer_generation(monkeypatch, tmp_path):
    old_chunks = [_chunk(0, "old body")]
    path = _save_pair(tmp_path, old_chunks)
    old = json.loads(path.read_text(encoding="utf-8"))
    _save_pair(tmp_path, [_chunk(0, "new body")])
    new_bytes = kb_cache.cache_file(path).read_bytes()
    def embed(parts, **kwargs):
        for part in parts:
            part["embedding"] = [1.0, 0.0]
        return parts
    monkeypatch.setattr(RAG, "generate_embeddings", embed)
    matrices = kb_cache.rebuild(path, old["chunks"], old["metadata"], reason="test")
    assert matrices.path is None and len(matrices.section_nodes) == 1
    assert kb_cache.cache_file(path).read_bytes() == new_bytes
