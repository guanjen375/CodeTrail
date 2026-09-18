"""Safety contracts for literal table evidence and version-bound OCR review."""
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import evidence_store
import table_lookup
import text_review

pytestmark = pytest.mark.smoke


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _kb(tmp_path, chunks=()):
    path = tmp_path / "knowledge.json"
    payload = {"metadata": {"documents": sorted({chunk["source"] for chunk in chunks}),
                            "store_generation": "initial"}, "chunks": list(chunks)}
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return payload


def _table():
    columns = [{"column_id": "c1", "label": "Register", "role": None},
               {"column_id": "c2", "label": "Address", "role": None},
               {"column_id": "c3", "label": "Reset", "role": None}]
    payload = {"kind": "table", "columns": columns,
               "rows": [{"row_index": 1, "cells": [
                   {"column_id": column["column_id"], "text": text, "state": "observed", "inherited_from_row": None}
                   for column, text in zip(columns, ("CTRL", "0x004000", "0x0001"))]}], "footnotes": []}
    return {"document_id": "spec.pdf::0123456789abcdef", "figure_id": "fig_" + "a" * 16,
            "source": "spec.pdf", "page": 2, "bbox": [1, 2, 3, 4], "revision": 2,
            "kind": "table", "extraction_status": "complete", "verification_status": "human_verified",
            "human_verification": {"confirmed_against_image": True, "revision": 2, "carried_over": False},
            "auto_disposition": "accept", "evidence": {}, "reasons": [], "payload_error": "",
            "in_kb": True, "evidence_ref": ".codetrail/figures/synthetic/run/manifest.json", "payload": payload}


def _text(tmp_path, *, body="Register CTRL is enabled.", index=0, prefix=""):
    pdf = tmp_path / "spec.pdf"
    artifact = tmp_path / "content_list.json"
    if not pdf.exists():
        pdf.write_text("synthetic PDF bytes")
        artifact.write_text("synthetic local OCR bytes")
        # Existing inputs belong to the user's project, not private state.
        # Explicit umask-002 modes keep this compatibility independent of CI.
        pdf.chmod(0o664)
        artifact.chmod(0o664)
    chunk = {"source": "spec.pdf", "page": 1, "chunk_index": index, "content": prefix + body,
             "type": "spec", "section": "", "section_index": 0,
             "origin": "mineru_text", "text_lane": "mineru", "heading_source": "mineru",
             "heading_prefix_chars": len(prefix), "overlap_prefix_chars": 0,
             "char_start": index * 100, "char_end": index * 100 + len(body),
             "bbox": [0, 0, 10, 10], "mineru_blocks": [{"index": index, "bbox": [0, 0, 10, 10],
                                                        "source_char_start": 0, "source_char_end": len(body)}],
             "source_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
             "content_list_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
    text_review.initialize_chunk(chunk, source_path=pdf.name, artifact_path=artifact.name)
    return chunk


def _confirmed(chunk):
    return text_review._mutate(chunk, action="confirm", text="", confirm_against_source=True)


def test_exact_table_lookup_never_loads_models_and_preserves_literal_provenance(tmp_path, monkeypatch):
    import RAG
    import model_identity
    _kb(tmp_path)
    table = _table()
    monkeypatch.setattr(table_lookup.figure_review, "list_figures", lambda *a, **k: [table])
    monkeypatch.setattr(RAG, "load_knowledge_base", lambda *a, **k: pytest.fail("exact lookup loaded vector KB"))
    monkeypatch.setattr(model_identity, "capture_model_identity", lambda *a, **k: pytest.fail("exact lookup probed model"))
    result = table_lookup.query_table(tmp_path, register="CTRL", address="16384", column="Reset")
    assert result["status"] == "ok" and result["has_ref"] is True
    cell = result["matches"][0]
    assert cell["value"] == "0x0001"
    assert cell["row_index"] == 1 and cell["column_id"] == "c3"
    assert {key: cell[key] for key in ("source", "document_id", "page", "bbox", "figure_id", "revision", "evidence_ref")} == {
        key: table[key] for key in ("source", "document_id", "page", "bbox", "figure_id", "revision", "evidence_ref")}
    assert not (tmp_path / ".codetrail").exists()
    assert not (tmp_path / ".knowledge.json.lock").exists()


def test_table_document_alias_resolves_current_source_without_widening(tmp_path, monkeypatch):
    """The public basename contract must resolve a current ID, never widen a failed scope."""
    table = _table()
    table["document_id"] = "qa/docs/spec.pdf::0123456789abcdef"
    stale = copy.deepcopy(table)
    stale.update(document_id="qa/docs/spec.pdf::fedcba9876543210", in_kb=False)
    other = copy.deepcopy(table)
    other.update(document_id="qa/other.pdf::1111111111111111", source="other.pdf",
                 figure_id="fig_" + "b" * 16)
    entries = [table, stale, other]
    calls = []

    def current(*tables):
        _kb(tmp_path, [{"source": item["source"], "document_id": item["document_id"],
                        "structured": True, "figure_id": item["figure_id"]} for item in tables])

    def figures(root, chunks, *, document_id=None):
        calls.append(document_id)
        return [item for item in entries if document_id is None or item["document_id"] == document_id]

    current(table, other)
    monkeypatch.setattr(table_lookup.figure_review, "list_figures", figures)
    for selector in ("spec.pdf", "qa/docs/spec.pdf", table["document_id"]):
        result = table_lookup.query_table(tmp_path, document_id=selector, figure_id=table["figure_id"],
                                          register="CTRL", column="Reset")
        assert result["status"] == "ok" and result["matches"][0]["value"] == "0x0001", result
        assert result["matches"][0]["document_id"] == table["document_id"]
        assert calls[-1] == table["document_id"]

    # A valid figure elsewhere cannot rescue an unknown/stale document selector.
    for selector in ("missing.pdf", stale["document_id"], " spec.pdf"):
        before = len(calls)
        result = table_lookup.query_table(tmp_path, document_id=selector, figure_id=table["figure_id"],
                                          register="CTRL", column="Reset")
        assert result["status"] == "not_found" and result["reason"] == "document_scope_not_found", result
        assert not result["has_ref"] and result["matches"] == []
        assert len(calls) == before, "an unresolved scope must not scan all figure artifacts"

    result = table_lookup.query_table(tmp_path, document_id="spec.pdf", figure_id=other["figure_id"],
                                      register="CTRL", column="Reset")
    assert result["reason"] == "figure_scope_not_found" and result["matches"] == []
    assert calls[-1] == table["document_id"]

    # Two currently indexed IDs sharing a basename require an explicit selection.
    collision = copy.deepcopy(table)
    collision.update(document_id="another/spec.pdf::2222222222222222", figure_id="fig_" + "c" * 16)
    entries.append(collision)
    current(table, other, collision)
    before = len(calls)
    result = table_lookup.query_table(tmp_path, document_id="spec.pdf", register="CTRL", column="Reset")
    assert result["status"] == "ambiguous" and result["reason"] == "document_scope_ambiguous", result
    assert result["matches"] == [] and result["ambiguous"] is True
    assert result["scope"]["candidate_document_ids"] == sorted((table["document_id"], collision["document_id"]))
    assert len(calls) == before
    exact = table_lookup.query_table(tmp_path, document_id=table["document_id"], register="CTRL", column="Reset")
    assert exact["status"] == "ok", exact

    # Names are literal metadata, including legitimate leading/trailing whitespace.
    spaced = copy.deepcopy(table)
    spaced.update(source=" spec\n.pdf ", document_id="qa/docs/ spec\n.pdf ::3333333333333333")
    entries[:] = [spaced]
    current(spaced)
    result = table_lookup.query_table(tmp_path, document_id=spaced["source"], register="CTRL", column="Reset")
    assert result["status"] == "ok" and result["matches"][0]["source"] == spaced["source"]


@pytest.mark.parametrize("defect", ["duplicate_row", "duplicate_column", "inherited", "unreadable", "conflict", "revision", "unverified"])
def test_table_lookup_never_returns_ambiguous_or_unverified_values(tmp_path, monkeypatch, defect):
    _kb(tmp_path)
    entry = _table()
    rows = entry["payload"]["rows"]
    if defect == "duplicate_row":
        extra = copy.deepcopy(rows[0])
        extra["row_index"] = 2
        rows.append(extra)
    elif defect == "duplicate_column":
        entry["payload"]["columns"][1]["label"] = "Reset"
    elif defect == "inherited":
        first = copy.deepcopy(rows[0])
        first["cells"][0]["text"] = "OTHER"
        rows[0]["row_index"] = 2
        rows[0]["cells"][2].update(state="inherited", inherited_from_row=1)
        rows.insert(0, first)
    elif defect in {"unreadable", "conflict"}:
        rows[0]["cells"][2]["state"] = defect
    elif defect == "revision":
        entry["payload_error"] = "canonical revision unavailable"
    else:
        entry["verification_status"] = "unverified"
        entry["human_verification"] = None
    monkeypatch.setattr(table_lookup.figure_review, "list_figures", lambda *a, **k: [entry])
    result = table_lookup.query_table(tmp_path, register="CTRL", column="Reset")
    assert result["status"] in {"ambiguous", "unverified"}
    assert result["matches"] == [] and result["has_ref"] is False
    assert "0x0001" not in json.dumps(result)


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "duplicate_json", "root_symlink"])
def test_evidence_reader_rejects_unsafe_metadata(tmp_path, attack):
    _kb(tmp_path)
    path = tmp_path / "knowledge.json"
    root = tmp_path
    if attack == "symlink":
        path.rename(tmp_path / "other.json")
        path.symlink_to(tmp_path / "other.json")
    elif attack == "hardlink":
        os.link(path, tmp_path / "other.json")
    elif attack == "root_symlink":
        root = tmp_path / "alias"
        root.symlink_to(tmp_path, target_is_directory=True)
    else:
        path.write_text('{"metadata":{},"chunks":[],"chunks":[]}')
    with pytest.raises((evidence_store.EvidenceStoreError, OSError)):
        evidence_store.snapshot(root)


@pytest.mark.parametrize("change", ["body", "source", "artifact", "prefix", "quality"])
def test_text_confirmation_is_bound_to_content_source_and_quality(tmp_path, change):
    chunk = _confirmed(_text(tmp_path))
    assert text_review.eligibility(chunk, root=tmp_path)["eligible"]
    if change == "body":
        chunk["content"] += " Different content."
    elif change == "source":
        (tmp_path / "spec.pdf").write_text("changed source")
    elif change == "artifact":
        (tmp_path / "content_list.json").write_text("changed artifact")
    elif change == "prefix":
        chunk["text_prefix"] = "changed heading"
        chunk["heading_prefix_chars"] = len(chunk["text_prefix"])
        chunk["content"] = chunk["text_prefix"] + text_review.effective_text(chunk)
    else:
        chunk["text_quality_issues"] = ["missing_lines"]
    assert not text_review.eligibility(chunk, root=tmp_path)["eligible"]


def test_reingest_preserves_only_exact_review_binding_and_advances_invalidated_revision(tmp_path):
    original = _text(tmp_path)
    corrected = text_review._mutate(original, action="correct", text="CTRL address is 0x4000.", confirm_against_source=False)
    confirmed = _confirmed(corrected)
    document = SimpleNamespace(source="spec.pdf", chunks=[copy.deepcopy(original)])
    text_review.carry_over(document, [confirmed])
    assert document.chunks[0]["content"] == "CTRL address is 0x4000."
    assert text_review.eligibility(document.chunks[0], root=tmp_path)["eligible"]
    changed = copy.deepcopy(original)
    changed["content_list_sha256"] = "a" * 64
    document.chunks = [changed]
    text_review.carry_over(document, [confirmed])
    assert changed["text_corrected"] is None and changed["text_confirmation"] is None
    assert changed["text_revision"] > confirmed["text_revision"]
    assert changed["text_id"] in document._codetrail_text_review_summary["invalidated"]


def test_strict_text_eligibility_checks_every_section_and_merge_member(tmp_path, monkeypatch):
    import knowledge
    from knowledge import KnowledgeBase
    verified = _confirmed(_text(tmp_path, index=0))
    pending = _text(tmp_path, index=1)
    verified["chunk_idx"], pending["chunk_idx"] = 0, 1
    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb.path, kb.chunks = tmp_path / "knowledge.json", [verified, pending]
    kb._text_source_cache = {}
    assert kb._text_eligibility(verified)["eligible"]
    merged = {"content": verified["content"] + "\n" + pending["content"],
              "member_chunk_idx": [0, 1], "origin": "mineru_text"}
    assert not kb._text_eligibility(merged)["eligible"]
    candidates = []
    kb._section_nodes = [SimpleNamespace(source="spec.pdf", member_indices=(0, 1))]
    kb._expand_section_members(candidates, [0], None)
    assert [candidate.chunk_idx for candidate in candidates if kb._text_eligibility(candidate.chunk)["eligible"]] == [0]
    monkeypatch.setattr(knowledge, "KNOWLEDGE_MERGE_ADJACENT", True)
    assert kb._merge_adjacent_chunks([verified, pending]) == [verified, pending]


def test_ocr_heading_prefix_cannot_raise_gate_evidence(tmp_path):
    import context_signals
    chunk = _text(tmp_path, body="Literal reviewed body.", prefix="[HEADING] invented 0xDEAD\n")
    assert "0xDEAD" in context_signals.retrieval_embedding_input(chunk, use_ctx=True)
    assert "0xDEAD" not in context_signals.gate_embedding_input(chunk)


def _offline_writes(monkeypatch):
    import RAG
    import model_identity
    def restore(kb, *args, **kwargs):
        for chunk in kb["chunks"]:
            chunk["embedding"] = [1.0, 0.0]
            chunk["embedding_gate"] = [1.0, 0.0]
        return True
    def embed(chunks, *args, **kwargs):
        for chunk in chunks:
            chunk["embedding"] = [0.0, 1.0]
            if kwargs.get("with_gate"):
                chunk["embedding_gate"] = [0.0, 1.0]
        return chunks
    monkeypatch.setattr(RAG, "_restore_embeddings_from_npz", restore)
    monkeypatch.setattr(RAG, "generate_embeddings", embed)
    monkeypatch.setattr(model_identity, "capture_model_identity", lambda *a, **k: {"fingerprint": "a" * 64})
    return RAG, embed


def test_text_correction_confirmation_and_revocation_publish_new_revisions(tmp_path, monkeypatch):
    RAG, _embed = _offline_writes(monkeypatch)
    original = _text(tmp_path)
    _kb(tmp_path, [original])
    corrected = json.loads(text_review.review_text(
        tmp_path, action="correct", text_id=original["text_id"], expected_revision=1,
        expected_sha256=original["text_content_sha256"], text="CTRL is disabled."))
    assert corrected["status"] == "ok" and corrected["eligible"] is False
    assert corrected["text_revision"] == 2 and corrected["original_ocr"] == original["text_original_ocr"]
    current = evidence_store.snapshot(tmp_path)["chunks"][0]
    assert current["content"] == "CTRL is disabled." and "embedding" not in current
    confirmed = json.loads(text_review.review_text(
        tmp_path, action="confirm", text_id=current["text_id"], expected_revision=2,
        expected_sha256=current["text_content_sha256"], confirm_against_source=True))
    assert confirmed["status"] == "ok" and confirmed["eligible"] is True
    revoked = json.loads(text_review.review_text(
        tmp_path, action="revoke", text_id=current["text_id"], expected_revision=3,
        expected_sha256=current["text_content_sha256"]))
    assert revoked["status"] == "ok" and revoked["eligible"] is False and revoked["text_revision"] == 4


def test_text_review_cas_rejects_a_concurrent_writer(tmp_path, monkeypatch):
    RAG, embed = _offline_writes(monkeypatch)
    original = _text(tmp_path)
    _kb(tmp_path, [original])
    wrote = []
    def racing_embed(chunks, *args, **kwargs):
        path = tmp_path / "knowledge.json"
        kb = json.loads(path.read_text())
        kb["metadata"]["store_generation"] = "another-writer"
        path.write_text(json.dumps(kb))
        return embed(chunks, *args, **kwargs)
    monkeypatch.setattr(RAG, "generate_embeddings", racing_embed)
    monkeypatch.setattr(RAG, "save_knowledge_base", lambda *a, **k: wrote.append(True))
    result = json.loads(text_review.review_text(
        tmp_path, action="correct", text_id=original["text_id"], expected_revision=1,
        expected_sha256=original["text_content_sha256"], text="new correction"))
    assert result["status"] == "error" and "conflict" in result["reason"]
    assert not wrote
    assert evidence_store.snapshot(tmp_path)["chunks"][0]["content"] == original["content"]


def test_text_review_readonly_paths_have_zero_model_calls_or_writes(tmp_path, monkeypatch):
    import model_identity
    original = _text(tmp_path)
    _kb(tmp_path, [original])
    monkeypatch.setattr(model_identity, "capture_model_identity", lambda *a, **k: pytest.fail("read-only review probed model"))
    for action in ("list", "show"):
        result = json.loads(text_review.review_text(tmp_path, action=action, text_id=original["text_id"]))
        assert result["status"] == "ok" and result["units"][0]["text_id"] == original["text_id"]
    assert not (tmp_path / ".codetrail").exists()
    assert not (tmp_path / ".knowledge.json.lock").exists()


def test_ocr_review_rejects_source_replacement_by_symlink(tmp_path, monkeypatch):
    _offline_writes(monkeypatch)
    original = _text(tmp_path)
    # A user-owned source may be group-writable, but a replacement link is not
    # an admissible source, even when it still points at identical bytes.
    assert text_review.eligibility(_confirmed(original), root=tmp_path)["eligible"]
    _kb(tmp_path, [original])
    corrected = json.loads(text_review.review_text(
        tmp_path, action="correct", text_id=original["text_id"], expected_revision=1,
        expected_sha256=original["text_content_sha256"], text="CTRL has reset value 0x01."))
    assert corrected["status"] == "ok", corrected
    confirmed = json.loads(text_review.review_text(
        tmp_path, action="confirm", text_id=original["text_id"], expected_revision=2,
        expected_sha256=corrected["text_content_sha256"], confirm_against_source=True))
    assert confirmed["status"] == "ok" and confirmed["eligible"], confirmed
    source = tmp_path / "spec.pdf"
    source.rename(tmp_path / "source-original.pdf")
    source.symlink_to("source-original.pdf")
    current = evidence_store.snapshot(tmp_path)["chunks"][0]
    verdict = text_review.eligibility(current, root=tmp_path)
    assert not verdict["eligible"] and verdict["reason"] == "source_unavailable"


def test_strict_ocr_notice_discloses_unverified_status_and_revision_locator():
    from knowledge import KnowledgeBase
    rendered = KnowledgeBase._excluded_text_line([
        {"source": "synthetic.pdf", "page": 3, "text_id": "text_" + "a" * 24,
         "text_revision": 7, "reason": "mineru_text_not_independently_verified"}])
    assert "未經獨立驗證" in rendered
    assert "synthetic.pdf p.3" in rendered and "text_" + "a" * 24 in rendered
    assert "rev=7" in rendered and "review_text" in rendered


def test_existing_group_writable_project_and_sources_keep_review_safety(tmp_path, monkeypatch):
    project = tmp_path / "existing-project"
    project.mkdir()
    project.chmod(0o775)
    _offline_writes(monkeypatch)
    original = _text(project)
    for name in ("spec.pdf", "content_list.json"):
        (project / name).chmod(0o664)
    _kb(project, [original])
    (project / "knowledge.json").chmod(0o664)
    lock = project / ".knowledge.json.lock"
    lock.write_bytes(b"")
    lock.chmod(0o664)  # Existing knowledge_store_lock uses normal open/umask.
    monkeypatch.setattr(table_lookup.figure_review, "list_figures", lambda *a, **k: [_table()])

    table = table_lookup.query_table(project, register="CTRL", column="Reset")
    assert table["status"] == "ok", table
    shown = json.loads(text_review.review_text(project, action="show", text_id=original["text_id"]))
    assert shown["status"] == "ok", shown
    assert shown["units"][0]["reason"] == "mineru_text_not_independently_verified"
    confirmed = json.loads(text_review.review_text(
        project, action="confirm", text_id=original["text_id"], expected_revision=1,
        expected_sha256=original["text_content_sha256"], confirm_against_source=True))
    assert confirmed["status"] == "ok" and confirmed["eligible"], confirmed
    assert project.stat().st_mode & 0o777 == 0o775
    assert (project / "spec.pdf").stat().st_mode & 0o777 == 0o664
    assert (project / "knowledge.json").stat().st_mode & 0o777 == 0o600
    assert lock.stat().st_mode & 0o777 == 0o664

    # New private lock creation remains 0600, independent of the project mode.
    lock.unlink()
    revoked = json.loads(text_review.review_text(
        project, action="revoke", text_id=original["text_id"], expected_revision=2,
        expected_sha256=original["text_content_sha256"]))
    assert revoked["status"] == "ok", revoked
    assert lock.stat().st_mode & 0o777 == 0o600

    source = project / "spec.pdf"
    backup = project / "source-original.pdf"
    source.rename(backup)
    source.symlink_to(backup.name)
    with pytest.raises(OSError):
        evidence_store.source_digest(project, source.name)
    source.unlink()
    backup.rename(source)
    link = project / "source-second-link.pdf"
    os.link(source, link)
    with pytest.raises(evidence_store.EvidenceStoreError):
        evidence_store.source_digest(project, source.name)
    link.unlink()
    alias = tmp_path / "project-alias"
    alias.symlink_to(project, target_is_directory=True)
    with pytest.raises(OSError):
        evidence_store.snapshot(alias)

    # Simulate foreign ownership without chown or touching any real project.
    real_fstat = os.fstat
    for target, read in ((project, lambda: evidence_store.snapshot(project)),
                         (source, lambda: evidence_store.source_digest(project, source.name)),
                         (project / "knowledge.json", lambda: evidence_store.snapshot(project))):
        target_identity = (target.stat().st_dev, target.stat().st_ino)
        def foreign_owner(fd):
            info = real_fstat(fd)
            if (info.st_dev, info.st_ino) == target_identity:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1,
                                       st_nlink=info.st_nlink)
            return info
        with monkeypatch.context() as patch:
            patch.setattr(evidence_store.os, "fstat", foreign_owner)
            with pytest.raises(evidence_store.EvidenceStoreError):
                read()
