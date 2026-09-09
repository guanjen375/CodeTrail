"""PDF quality/review regressions and durable safety contracts (offline)."""
from __future__ import annotations

import copy
import json

import pytest

import figure_extract as fx
import figure_review as fr
import RAG
from tests import test_figure_review as helpers

pytestmark = pytest.mark.smoke

QUALITY_DEFAULTS = {
    "quality_grade": "unknown", "review_state": "unreviewed",
    "auto_disposition": "manual_review", "quality_issues": [],
}


@pytest.fixture
def root(tmp_path):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "spec.pdf").write_bytes(b"%PDF-1.4 synthetic quality fixture\n")
    return project


def _quality(entry):
    return {name: entry.get(name) for name in QUALITY_DEFAULTS}


def _write(root, payload, *, status="needs_review", reasons=(), evidence=None,
           human=None, kind="table"):
    doc_id = helpers.document_id(root)
    fig_id = helpers.figure_id(doc_id)
    figure = helpers.make_figure(doc_id, fig_id, payload, status=status, reasons=reasons, kind=kind)
    if evidence is not None:
        figure["evidence"] = evidence
    signature = {"asset_digest": helpers.DIGEST_A, "page": 3,
                 "nbbox": [0.1176, 0.1263, 0.817, 0.404]}
    if human is not None:
        human = {**human, "source_signature": signature}
    run_id = fr.new_run_id()
    path = fr.write_run_artifacts(
        root, document_id=doc_id, run_id=run_id, figures=[figure],
        variants=[helpers.make_variant(fig_id)],
        source_signatures={fig_id: signature},
        human_verifications=({fig_id: human} if human is not None else None))
    return doc_id, fig_id, path


def _unreadable_payload(*, all_cells=False):
    payload = helpers.table_payload()
    cells = [cell for row in payload["rows"] for cell in row["cells"]]
    for cell in cells if all_cells else cells[:1]:
        cell.update(text="▯", state="unreadable")
    return payload


def test_known_damage_is_not_presented_as_manual_judgment(root):
    """Confirmed glyph damage previously appeared as a request to judge correctness."""
    doc_id, fig_id, path = _write(root, _unreadable_payload(), reasons=["model_unreadable"])
    entry = fr.read_manifest(root, evidence_ref=str(path.relative_to(root)))["figures"][0]
    assert _quality(entry) == {
        "quality_grade": "partial", "review_state": "unreviewed",
        "auto_disposition": "repair_required", "quality_issues": ["unreadable_text"],
    }
    review = path.with_name(fr.REVIEW_NAME).read_text("utf-8")
    assert "## 需修復（repair_required）" in review
    assert "## 待人工判斷（manual_review）" not in review
    listed = fr.list_figures(root, [], document_id=doc_id)[0]
    assert listed["figure_id"] == fig_id and _quality(listed) == _quality(entry)


def test_excluded_payload_remains_available_without_old_run_warning(root):
    """All unreadable cells must stay inspectable without posing as an old KB run."""
    payload = _unreadable_payload(all_cells=True)
    doc_id, _fig_id, path = _write(root, payload, reasons=["unreadable_content"])
    listed = fr.list_figures(root, [], document_id=doc_id)[0]
    assert "quality_excluded" in listed["warnings"], listed["warnings"]
    assert "artifact_only" not in listed["warnings"]
    assert listed["in_kb"] is False and listed["fixable"] is False
    assert listed["extraction_status"] == "complete" and listed["payload"] == payload
    assert _quality(listed) == {
        "quality_grade": "unusable", "review_state": "unreviewed",
        "auto_disposition": "excluded", "quality_issues": ["unreadable_text"],
    }
    assert "## 品質排除（excluded，未入庫）" in path.with_name(fr.REVIEW_NAME).read_text("utf-8")


def test_legacy_manifest_quality_defaults_remain_readable(root):
    """Missing additive metadata must not invalidate a published legacy run."""
    doc_id, _fig_id, path = _write(root, helpers.table_payload(), status="unverified")
    raw = json.loads(path.read_text("utf-8"))
    for field in QUALITY_DEFAULTS:
        raw["figures"][0].pop(field, None)
    path.write_text(json.dumps(raw), encoding="utf-8")
    entry = fr.read_manifest(root, evidence_ref=str(path.relative_to(root)))["figures"][0]
    assert _quality(entry) == QUALITY_DEFAULTS
    assert _quality(fr.list_figures(root, [], document_id=doc_id)[0]) == QUALITY_DEFAULTS


def test_fix_refreshes_quality_without_reusing_previous_evidence(root):
    """The mirror retained old machine findings after replacing its canonical payload."""
    doc_id, fig_id, ref, kb_path = helpers.seed(
        root, payload=_unreadable_payload(), status="needs_review")
    path = root / ref
    raw = json.loads(path.read_text("utf-8"))
    previous_evidence = {
        "lane": "vl", "cells": {"r1c1": {"matched": False, "verdict": "glyph"}},
        "transcription": {"attempted_kind": "table", "fallback_kind": "prose",
                          "anchor": None, "coverage": "unknown"},
    }
    raw["figures"][0]["evidence"] = previous_evidence
    path.write_text(json.dumps(raw), encoding="utf-8")
    corrected = helpers.table_payload(rows=helpers.CORRECTED_ROWS)
    result = fr.apply_fix(root, kb_path, document_id=doc_id, figure_id=fig_id,
                          expected_revision=1, payload=corrected, kind="table",
                          confirm_against_image=True, rechunk=helpers.rechunk,
                          embed=helpers.embed)
    entry = fr.read_manifest(root, evidence_ref=ref)["figures"][0]
    assert "transcription" not in entry["evidence"], entry["evidence"]
    assert entry["evidence"]["previous_revision_evidence"] == previous_evidence
    expected = {"quality_grade": "usable", "review_state": "confirmed",
                "auto_disposition": "accept", "quality_issues": []}
    assert _quality(entry) == expected
    assert result["warnings"] == [] and entry["current_revision"] == 2
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    assert all(_quality(chunk) == expected for chunk in chunks if chunk.get("structured"))
    assert _quality(fr.list_figures(root, chunks)[0]) == expected


def test_writer_rejects_human_confirmation_of_damaged_payload(root):
    """The artifact writer must enforce the same human/glyph boundary as the KB writer."""
    before = helpers.snapshot(root)
    with pytest.raises(fx.FigureReviewError, match="human_verified|人工確認"):
        _write(root, _unreadable_payload(), status="human_verified",
               reasons=["human_corrected"],
               human={"revision": 1, "confirmed_against_image": True})
    assert helpers.snapshot(root) == before


def test_list_refuses_conflicting_quality_metadata(root, monkeypatch):
    """A partial metadata update may not expose a seemingly coherent fixable payload."""
    monkeypatch.setattr(helpers.config, "FIGURE_CHUNK_MAX_CHARS", 200)
    doc_id, _fig_id, _ref, kb_path = helpers.seed(root)
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    structured = [chunk for chunk in chunks if chunk.get("structured")]
    assert len(structured) >= 2
    for chunk in structured:
        chunk.update(copy.deepcopy(QUALITY_DEFAULTS))
    structured[-1]["auto_disposition"] = "accept"
    listed = fr.list_figures(root, chunks, document_id=doc_id)[0]
    assert "kb_inconsistent" in listed["warnings"], listed["warnings"]
    assert listed["payload"] is None and listed["fixable"] is False


def test_fix_preserves_duplicate_model_input_provenance(root):
    """Refreshing quality evidence must not erase the duplicate's source-identity link."""
    doc_id = helpers.document_id(root)
    representative = helpers.figure_id(doc_id, page=3)
    duplicate = helpers.figure_id(doc_id, page=4)
    first = helpers.make_figure(doc_id, representative, helpers.table_payload())
    second = helpers._sentinel_duplicate(doc_id, duplicate, representative)
    original_evidence = copy.deepcopy(second["evidence"])
    run_id = fr.new_run_id()
    fr.write_run_artifacts(root, document_id=doc_id, run_id=run_id, figures=[first, second],
                           variants=[helpers.make_variant(representative)])
    ref = fr.evidence_ref_for(doc_id, run_id)
    chunks = fx.build_figure_chunks(
        [first, second], source="spec.pdf", doc_type="spec", next_chunk_index={},
        evidence_ref_by_figure={representative: ref, duplicate: ref})
    for chunk in chunks:
        chunk["embedding"] = [1.0, 0.0]
        chunk["id"] = helpers.knowledge_store.chunk_id(chunk)
    kb_path = root / helpers.config.KNOWLEDGE_FILE
    RAG.save_knowledge_base({"metadata": {"documents": ["spec.pdf"]}, "chunks": chunks}, kb_path)
    error = None
    try:
        result = fr.apply_fix(root, kb_path, document_id=doc_id, figure_id=duplicate,
                              expected_revision=1, payload=helpers.table_payload(), kind="table",
                              confirm_against_image=True, rechunk=helpers.rechunk,
                              embed=helpers.embed)
    except fx.FigureError as exc:
        error = str(exc)
    assert error is None, error
    assert result["warnings"] == []
    entry = next(item for item in fr.read_manifest(root, evidence_ref=ref)["figures"]
                 if item["figure_id"] == duplicate)
    assert entry["evidence"]["duplicate_model_input"] == original_evidence["duplicate_model_input"]
    assert entry["evidence"]["duplicate_of"] == representative
    assert entry["evidence"]["previous_revision_evidence"] == original_evidence
    assert _quality(entry) == {"quality_grade": "usable", "review_state": "confirmed",
                               "auto_disposition": "accept", "quality_issues": []}


def _seed_source_coverage_case(root, *, kind, reason="transcription_source_incomplete"):
    payload = (helpers.table_payload() if kind == "table"
               else {"kind": kind, "lines": [
                   {"line_index": 1, "text": "visible", "uncertain_spans": []}]})
    known_missing = reason == "transcription_source_incomplete"
    # The reliable source is "visible\nsource tail". Production retains its coverage
    # counts, not the complete source text; fix cannot prove any submitted edit is complete.
    evidence = {"lane": "vl", "transcription": {
        "attempted_kind": kind, "fallback_kind": None,
        "anchor": ({"channel": "markdown_pos", "source_lines": 2,
                    "matched_source_lines": 1} if known_missing else None),
        "coverage": 0.5 if known_missing else "unknown",
    }}
    doc_id = helpers.document_id(root)
    fig_id = helpers.figure_id(doc_id)
    figure = helpers.make_figure(
        doc_id, fig_id, payload, kind=kind, reasons=[reason],
        status="unverified" if reason == "no_anchor_evidence" else "needs_review")
    if kind in fx.LINE_KINDS:
        figure["line_total"] = len(payload["lines"])
    figure["evidence"] = evidence
    run_id = fr.new_run_id()
    fr.write_run_artifacts(root, document_id=doc_id, run_id=run_id, figures=[figure],
                           variants=[helpers.make_variant(fig_id)],
                           source_signatures={fig_id: copy.deepcopy(helpers.SIGNATURE)})
    ref = fr.evidence_ref_for(doc_id, run_id)
    chunks = fx.build_figure_chunks(
        [figure], source="spec.pdf", doc_type="spec", next_chunk_index={},
        evidence_ref_by_figure={fig_id: ref})
    for chunk in chunks:
        chunk["embedding"] = [1.0, 0.0]
        chunk["id"] = helpers.knowledge_store.chunk_id(chunk)
    kb_path = root / helpers.config.KNOWLEDGE_FILE
    RAG.save_knowledge_base({"metadata": {"documents": ["spec.pdf"]}, "chunks": chunks}, kb_path)
    return doc_id, fig_id, ref, kb_path, payload


@pytest.mark.parametrize("kind", ["terminal", "prose"])
@pytest.mark.parametrize("edit", ["unchanged", "arbitrary_edit"])
def test_source_incomplete_revision_cannot_be_confirmed_without_reverification(root, kind, edit):
    """Known missing source text cannot disappear behind an unchanged or edited human fix."""
    doc_id, fig_id, ref, kb_path, original = _seed_source_coverage_case(root, kind=kind)
    before = helpers.snapshot(root)
    submitted = copy.deepcopy(original)
    if edit == "arbitrary_edit":
        submitted["lines"][0]["text"] = "arbitrarily edited visible text"
    calls = []

    def rechunk(payload, kind, meta):
        calls.append("rechunk")
        return helpers.rechunk(payload, kind, meta)

    def embed(chunks, *, with_gate=False):
        calls.append("embed")
        return helpers.embed(chunks, with_gate=with_gate)

    error = None
    accepted = None
    try:
        accepted = fr.apply_fix(
            root, kb_path, document_id=doc_id, figure_id=fig_id, expected_revision=1,
            payload=submitted, kind=kind, confirm_against_image=True,
            rechunk=rechunk, embed=embed)
    except fx.FigureReviewError as exc:
        error = exc
    assert error is not None, f"Known missing source was accepted as a human fix: {accepted}"
    assert error.code == "source_reverification_required"
    assert "重新 ingest" in str(error) and "來源" in str(error)
    assert calls == [], "Refuse before rechunk or embedding"
    assert helpers.snapshot(root) == before, "The rejected confirmation must perform zero writes"
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    listed = fr.list_figures(root, chunks, document_id=doc_id)[0]
    assert listed["fixable"] is False
    assert "source_reverification_required" in listed["warnings"]
    assert "重新 ingest" in listed["fixable_reason"]
    assert listed["payload"] == original and listed["revision"] == 1
    assert listed["auto_disposition"] == "repair_required"
    assert fr.read_manifest(root, evidence_ref=ref)["figures"][0]["human_verification"] is None


# New safety contracts below are deliberately not executed by the developer. Root's
# single delivery smoke and the reviewer's full execution own these nodes.
@pytest.mark.parametrize("kind,reason", [
    ("terminal", "no_anchor_evidence"),
    ("prose", "no_anchor_evidence"),
    ("table", "header_conflict"),
])
def test_unknown_coverage_and_semantic_review_allow_same_payload_confirmation(root, kind, reason):
    """Unknown completeness and semantic judgment remain eligible for explicit confirmation."""
    doc_id, fig_id, ref, kb_path, payload = _seed_source_coverage_case(
        root, kind=kind, reason=reason)
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    listed = fr.list_figures(root, chunks, document_id=doc_id)[0]
    assert listed["fixable"] is True
    assert listed["fixable_reason"] == ""
    assert "source_reverification_required" not in listed["warnings"]

    fixed = fr.apply_fix(
        root, kb_path, document_id=doc_id, figure_id=fig_id, expected_revision=1,
        payload=copy.deepcopy(payload), kind=kind, confirm_against_image=True,
        rechunk=helpers.rechunk, embed=helpers.embed)
    expected = {"quality_grade": "usable", "review_state": "confirmed",
                "auto_disposition": "accept", "quality_issues": []}
    assert _quality(fixed) == expected and fixed["revision"] == 2
    current = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    listed = fr.list_figures(root, current, document_id=doc_id)[0]
    entry = fr.read_manifest(root, evidence_ref=ref)["figures"][0]
    assert listed["payload"] == payload and _quality(listed) == expected
    assert _quality(entry) == expected
    assert entry["source_signature"] == helpers.SIGNATURE
    assert entry["human_verification"]["source_signature"] == helpers.SIGNATURE
    assert entry["human_verification"]["revision"] == 2
    assert entry["human_verification"]["confirmed_against_image"] is True


def test_source_reverification_requirement_survives_unavailable_artifact(root):
    """The current KB reason remains authoritative when the review mirror is lost."""
    doc_id, fig_id, ref, kb_path, payload = _seed_source_coverage_case(root, kind="terminal")
    manifest_path = root / ref
    review = (manifest_path.parent / "review.md").read_text("utf-8")
    assert "重新 ingest" in review and "來源完整性核對" in review
    assert 'review_figures(action="fix"' not in review
    manifest_path.unlink()
    before = helpers.snapshot(root)
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    listed = next(item for item in fr.list_figures(root, chunks, document_id=doc_id)
                  if item["figure_id"] == fig_id)
    assert listed["fixable"] is False and listed["payload"] is None
    assert "source_reverification_required" in listed["warnings"]
    assert "artifact_unavailable" in listed["warnings"]
    assert "重新 ingest" in listed["fixable_reason"]
    assert listed["fixable_reason"] in listed["reason_details"]

    def forbidden(*_args, **_kwargs):
        pytest.fail("Source re-verification must be required before callbacks")

    with pytest.raises(fx.FigureReviewError) as exc_info:
        fr.apply_fix(
            root, kb_path, document_id=doc_id, figure_id=fig_id, expected_revision=1,
            payload=payload, kind="terminal", confirm_against_image=True,
            rechunk=forbidden, embed=forbidden)
    assert exc_info.value.code == "source_reverification_required"
    assert helpers.snapshot(root) == before


@pytest.mark.parametrize("reason,grade,disposition,issues", [
    ("row_alignment_failed", "structure_error", "excluded", ["structure_mismatch"]),
    ("line_alignment_failed", "structure_error", "excluded", ["structure_mismatch"]),
    ("span_ambiguous", "structure_error", "excluded", ["structure_mismatch"]),
    ("stitch_tile_gap", "structure_error", "excluded", ["structure_mismatch"]),
    ("sample_tile_subset_mismatch", "structure_error", "excluded", ["structure_mismatch"]),
    ("raster_structure_mismatch", "structure_error", "excluded", ["structure_mismatch"]),
    ("missing_rows", "partial", "repair_required", ["missing_items"]),
    ("missing_lines", "partial", "repair_required", ["missing_items"]),
    ("transcription_source_incomplete", "partial", "repair_required", ["missing_items"]),
    ("cell_conflict", "partial", "repair_required", ["conflicting_text"]),
    ("header_conflict", "unknown", "manual_review", ["unverified_coverage"]),
    ("header_missing", "unknown", "manual_review", ["unverified_coverage"]),
    ("column_assignment_unverified", "unknown", "manual_review", ["unverified_coverage"]),
    ("not_a_real_structural_mismatch", "unknown", "manual_review", ["unverified_coverage"]),
])
def test_quality_uses_only_definite_producer_reason_slugs(reason, grade, disposition, issues):
    from figure_quality import assess_quality

    result = assess_quality(helpers.table_payload(), "table", reasons=[reason],
                            verification_status="needs_review")
    assert result == {"quality_grade": grade, "review_state": "unreviewed",
                      "auto_disposition": disposition, "quality_issues": issues}


@pytest.mark.parametrize("human,status", [
    (None, "human_verified"),
    ({}, "human_verified"),
    (True, "human_verified"),
    ({"revision": 1, "confirmed_against_image": "false"}, "human_verified"),
    ({"revision": 1, "confirmed_against_image": 1}, "human_verified"),
    ({"revision": True, "confirmed_against_image": True}, "human_verified"),
    ({"revision": 1, "confirmed_against_image": True, "carried_over": "false"}, "human_verified"),
    ({"revision": 1, "confirmed_against_image": True}, "unverified"),
])
def test_quality_never_invents_human_confirmation(human, status):
    from figure_quality import assess_quality

    result = assess_quality(helpers.table_payload(), "table", verification_status=status,
                            human_verification=human)
    assert result["review_state"] == "unreviewed"
    assert result["auto_disposition"] == "manual_review"


@pytest.mark.parametrize("damage", ["glyph", "header_glyph", "footnote_glyph", "conflict",
                                    "missing_row", "uncertain_span"])
def test_human_confirmation_cannot_launder_payload_damage(root, damage):
    from figure_quality import assess_quality

    payload, kind = helpers.table_payload(), "table"
    if damage == "glyph":
        payload = _unreadable_payload()
    elif damage == "header_glyph":
        payload["columns"][0]["label"] = "Na▯e"
    elif damage == "footnote_glyph":
        payload["footnotes"] = ["RW = read/▯"]
    elif damage == "conflict":
        payload["rows"][0]["cells"][0]["state"] = "conflict"
    elif damage == "missing_row":
        payload["rows"][-1]["row_index"] = 3
    else:
        payload, kind = helpers.terminal_payload(lines=["a▯c", "known"]), "terminal"
        payload["lines"][0]["uncertain_spans"] = [
            {"start": 1, "end": 2, "alternatives": ["b", "8"]}]
    human = {"revision": 1, "confirmed_against_image": True}
    quality = assess_quality(payload, kind, verification_status="human_verified",
                             human_verification=human)
    assert quality["review_state"] == "unreviewed"
    assert quality["auto_disposition"] == "repair_required"
    before = helpers.snapshot(root)
    with pytest.raises(fx.FigureReviewError, match="human_verified|人工確認"):
        _write(root, payload, kind=kind, status="human_verified", human=human)
    assert helpers.snapshot(root) == before


@pytest.mark.parametrize("mutation", ["missing_record", "wrong_revision", "nonhuman_status",
                                      "truthy_confirmation", "unbacked_review_state"])
def test_manifest_human_record_remains_bidirectional_and_current_revision(root, mutation):
    _doc_id, _fig_id, path = _write(
        root, helpers.table_payload(), status="human_verified",
        human={"revision": 1, "confirmed_against_image": True})
    raw = json.loads(path.read_text("utf-8"))
    entry = raw["figures"][0]
    if mutation == "missing_record":
        entry["human_verification"] = None
    elif mutation == "wrong_revision":
        entry["human_verification"]["revision"] = 2
    elif mutation == "nonhuman_status":
        entry["verification_status"] = "unverified"
    elif mutation == "truthy_confirmation":
        entry["human_verification"]["confirmed_against_image"] = "false"
    else:
        entry["human_verification"] = None
        entry["verification_status"] = "unverified"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(fx.FigureReviewError, match="human_verification|confirmed|人工確認"):
        fr.read_manifest(root, evidence_ref=str(path.relative_to(root)))


def test_quality_requires_independent_evidence_and_proven_formatting():
    from figure_quality import assess_quality

    payload = helpers.table_payload()
    evidence = {"channels": ["words_geometry"], "cells": {
        "r1c1": {"matched": True, "anchor": "words_geometry", "by_channel": {
            "words_geometry": {"reliable": True, "verdict": "match",
                               "raw": "CTRL0   ", "payload": "CTRL0"}}}}}
    before = copy.deepcopy((payload, evidence))
    quality = assess_quality(payload, "table", evidence=evidence,
                             verification_status="corroborated")
    assert quality == {"quality_grade": "formatting_only", "review_state": "unreviewed",
                       "auto_disposition": "accept", "quality_issues": ["formatting_difference"]}
    semantic = assess_quality(payload, "table", evidence=evidence,
                              verification_status="needs_review", reasons=["header_conflict"])
    assert semantic["auto_disposition"] == "manual_review"
    # A repeated VL sample is not a source-coverage proof, even if it agrees exactly.
    repeated = assess_quality(payload, "table", evidence={"channels": ["vl_sample_2"],
                              "repeatability": {"agreement": True, "samples": 2}})
    assert repeated["quality_grade"] == "unknown"
    assert (payload, evidence) == before


@pytest.mark.parametrize("coverage,anchor,grade", [
    ("unknown", None, "unknown"),
    (1.0, None, "unknown"),
    (True, {"channel": "markdown_pos", "source_lines": 2, "matched_source_lines": 2}, "unknown"),
    (0.5, {"channel": "markdown_pos", "source_lines": 2, "matched_source_lines": 1}, "partial"),
    (1.0, {"channel": "markdown_pos", "source_lines": 2, "matched_source_lines": 1}, "unknown"),
])
def test_quality_transcription_coverage_requires_a_source_denominator(coverage, anchor, grade):
    from figure_quality import assess_quality

    payload = helpers.terminal_payload(lines=["known"])
    quality = assess_quality(payload, "terminal", evidence={"transcription": {
        "attempted_kind": "table", "fallback_kind": "prose", "anchor": anchor,
        "coverage": coverage}})
    assert quality["quality_grade"] == grade
    assert quality["quality_issues"] == (["missing_items"] if grade == "partial"
                                         else ["unverified_coverage"])


def test_missing_artifact_does_not_become_known_empty_or_confirmed():
    from figure_quality import assess_quality

    persisted = {"quality_grade": "usable", "review_state": "confirmed",
                 "auto_disposition": "accept", "quality_issues": []}
    evidence = {"metadata_only": True, "quality_metadata": persisted}
    result = assess_quality(None, "table", evidence=evidence, verification_status="human_verified")
    assert result["quality_grade"] == "usable" and result["review_state"] == "unreviewed"
    no_metadata = assess_quality(None, "table", evidence={"metadata_only": True})
    assert no_metadata["quality_issues"] == ["unverified_coverage"]
    evidence["fallback_text"] = "REF\nknown value: ▯"
    assert assess_quality(None, "table", evidence=evidence)["auto_disposition"] == "repair_required"


def test_legacy_chunk_quality_fields_compare_as_conservative_defaults(root, monkeypatch):
    monkeypatch.setattr(helpers.config, "FIGURE_CHUNK_MAX_CHARS", 200)
    doc_id, _fig_id, ref, kb_path = helpers.seed(root)
    chunks = RAG.load_knowledge_base(kb_path, _quiet=True)["chunks"]
    structured = [chunk for chunk in chunks if chunk.get("structured")]
    assert len(structured) >= 2
    for chunk in structured:
        chunk.update(copy.deepcopy(QUALITY_DEFAULTS))
    for field in QUALITY_DEFAULTS:
        structured[0].pop(field)
    path = root / ref
    raw = json.loads(path.read_text("utf-8"))
    for field in QUALITY_DEFAULTS:
        raw["figures"][0].pop(field)
    path.write_text(json.dumps(raw), encoding="utf-8")
    listed = fr.list_figures(root, chunks, document_id=doc_id)[0]
    assert _quality(listed) == QUALITY_DEFAULTS
    assert listed["payload"] == helpers.table_payload() and listed["warnings"] == []
