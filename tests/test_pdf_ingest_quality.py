"""Reported PDF ingest losses: exercise the production admission and notification paths."""
from __future__ import annotations

import dataclasses
import json
import types

import pytest

import RAG
import context_generation
import figure_extract as fx
import figure_review
import ingest_notify
from extracted_document import ExtractedDocument
from tests.test_figure_ingest import (
    INTRO, PAGE1, POS1, TABLE_MD, TABLE_BBOX, _candidate, _document_id,
    _figure_id, _harness, _page, _plan, _result, _table_box, _table_payload,
    _write_pdf, _manifests, _use_real_artifact_store, FakePageEvidence,
)


@pytest.mark.smoke
def test_known_damage_uses_repair_in_normal_and_fallback_summary(monkeypatch, tmp_path):
    payload = {"kind": "terminal", "lines": [
        {"line_index": 1, "text": "port=▯", "uncertain_spans": []}]}
    entry = {
        "page": 1, "figure_index": 1, "figure_id": "fig_0123456789abcdef",
        "kind": "terminal", "in_kb": True, "fixable": True,
        "extraction_status": "complete", "verification_status": "needs_review",
        "reasons": ["model_unreadable"], "payload": payload,
        "evidence": {"lane": "native"},
        "quality_grade": "partial", "review_state": "unreviewed",
        "auto_disposition": "repair_required", "quality_issues": ["unreadable_text"],
    }
    chunk = {**entry, "structured": True, "figure_kind": "terminal", "content": "port=▯"}
    document = ExtractedDocument(raw_text="", source="spec.pdf")
    guard = {"root": str(tmp_path), "document_id": "spec.pdf::digest", "run_id": "run"}
    for entries in ([entry], None):
        monkeypatch.setattr(fx, "list_figures", lambda *a, **k: entries)
        summary = ingest_notify.parse_summary_line(
            RAG._ingest_summary_line(document, [chunk], guard))
        assert summary["review_total"] == 0, "Known unreadable text is repair, not a judgment task"
        assert summary["repair_total"] == 1
        assert summary["repair"][0]["disposition"] == "repair_required"
        text = "\n".join(ingest_notify.render_action_block(summary))
        assert "repair" in text.lower() or "修復" in text
        assert "原圖可讀，可人工修正" not in text


@pytest.mark.smoke
def test_pdf_navigation_keeps_source_offsets_but_not_retrieval_noise(monkeypatch, tmp_path):
    raw = ("## Contents\n1. Introduction ........ 3\n2. Setup ........ 8\n"
           "3. Registers ........ 12\n\n## Firmware behavior\n"
           "The READY register is read after initialization.\n")
    lane = {"active": False, "preflight_only": False, "figures": [], "absent": [],
            "replacements": {}, "page_source": {}, "guard": None, "evidence_ref": {}}
    monkeypatch.setattr(RAG, "_figure_root", lambda root: tmp_path)
    monkeypatch.setattr(RAG, "_source_identity_snapshot", lambda *args: None)
    monkeypatch.setattr(RAG, "check_pymupdf4llm", lambda: types.SimpleNamespace(
        to_markdown=lambda *a, **k: [_page(1, raw)]))
    monkeypatch.setattr(RAG, "_run_structured_figure_lane", lambda *a, **k: lane)
    document = RAG.extract_pdf_document(str(tmp_path / "sample.pdf"), root=str(tmp_path))
    assert "Introduction ........ 3" in document.raw_text
    indexed = "\n".join(chunk["content"] for chunk in document.chunks)
    assert "Introduction ........ 3" not in indexed, "Navigation rows currently compete with body evidence"
    assert "READY register" in indexed
    assert all("........" not in section.title for section in document.sections)
    chunk = next(c for c in document.chunks if "READY register" in c["content"])
    assert "READY register" in document.raw_text[chunk["char_start"]:chunk["char_end"]]
    window = context_generation.build_section_window(document, chunk, budget_chars=10000)
    assert "Introduction ........ 3" not in window
    assert "READY register" in window


@pytest.mark.smoke
def test_navigation_rows_cannot_become_figure_captions():
    raw = ("## List of Figures\nFigure 1. Boot flow ........ 3\n"
           "Figure 2. Reset flow ........ 8\nFigure 3. Data flow ........ 12\n"
           "\n## Implementation\nFigure 4. Actual boot sequence\n")
    captions = RAG._page_captions(raw, 100)
    assert [item["text"] for item in captions] == ["Figure 4. Actual boot sequence"]
    assert captions[0]["offset"] == 100 + raw.index("Figure 4.")


@pytest.mark.smoke
def test_navigation_candidate_is_excluded_before_structured_dispatch():
    candidate = types.SimpleNamespace(kind=fx.KIND_TABLE, native_table={"pos": (0, 20)},
        page=1, bbox=TABLE_BBOX, signals={"native_lane": True, "content_role": "navigation"})
    kept, absent = RAG._structured_candidates(fx, types.SimpleNamespace(candidates=[candidate]))
    assert not kept, "A TOC table must not enter the figure lane"
    assert absent[0]["reason"] == "navigation_candidate_excluded"
    assert not ingest_notify.is_actionable_absent(absent[0]["reason"])


@pytest.mark.smoke
def test_terminal_native_span_uses_text_after_code_reclassification():
    candidate = types.SimpleNamespace(kind=fx.KIND_TERMINAL,
        native_table={"pos": (0, 10), "markdown": "lossy table"},
        signals={"native_text": {"pos": (20, 40), "markdown": "    if (a || b) {"}})
    span = RAG._native_span(fx, candidate)
    assert span["kind"] == "native_text"
    assert span["pos"] == (20, 40)


@pytest.mark.smoke
def test_excluded_native_payload_keeps_raw_text_and_auditable_payload(monkeypatch, tmp_path):
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    fid = _figure_id(document_id, 1)
    candidate = _candidate(document_id, fid, native_table={"pos": POS1, "markdown": TABLE_MD})
    broken = _table_payload(["Name", "Addr"], [["▯", "▯"], ["▯", "▯"]])
    for row in broken["rows"]:
        for cell in row["cells"]:
            cell["state"] = "unreadable"
    result = dataclasses.replace(_result(document_id, fid, payload=broken,
        status=fx.VERIF_NEEDS_REVIEW), reasons=["model_unreadable"])
    plan = _plan(document_id, [candidate], {1: FakePageEvidence(
        page=1, raw_markdown=PAGE1, page_boxes=[_table_box(POS1)])})
    _harness(monkeypatch, tmp_path, [_page(1, PAGE1, [_table_box(POS1)])], plan, [result])
    _use_real_artifact_store(monkeypatch)
    document = RAG.extract_pdf_document(str(pdf), root=str(tmp_path))
    assert not any(c.get("structured") for c in document.chunks), "All-unreadable payload currently enters KB"
    assert "CTRL0" in "\n".join(c["content"] for c in document.chunks)
    assert "表格已改以結構化" not in document.raw_text
    manifests = _manifests(tmp_path)
    manifest = json.loads(manifests[0].read_text())
    entry = manifest["figures"][0]
    assert entry["auto_disposition"] == "excluded"
    assert entry["extraction_status"] == "complete"
    listed = figure_review.list_figures(tmp_path, document.chunks, document_id=document_id)
    item = next(e for e in listed if e["figure_id"] == fid)
    assert item["payload"] == broken
    assert "quality_excluded" in item["warnings"]
    assert "artifact_only" not in item["warnings"]


@pytest.mark.smoke
def test_unavailable_native_channels_are_visible_even_without_vl_candidates(monkeypatch, tmp_path):
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    plan = _plan(document_id, [], {1: FakePageEvidence(
        page=1, raw_markdown=INTRO, page_boxes=[], unavailable=["words:unavailable"])})
    plan = dataclasses.replace(plan, stats={"unavailable_channels": {"1": ["words:unavailable"]}})
    _harness(monkeypatch, tmp_path, [_page(1, INTRO)], plan, [])
    document = RAG.extract_pdf_document(str(pdf), root=str(tmp_path))
    absent = getattr(document, RAG._ABSENT_ATTR)
    assert any(item["reason"].startswith("native_channel_unavailable") for item in absent), absent
    assert all(ingest_notify.is_actionable_absent(item["reason"]) for item in absent)


@pytest.mark.smoke
def test_human_carryover_does_not_reuse_superseded_extraction_damage(monkeypatch, tmp_path):
    payload = _table_payload(["Name", "Value"], [["READY", "1"]])
    figure = dataclasses.replace(_result("spec.pdf::digest", "fig_0123456789abcdef",
        payload=payload, status=fx.VERIF_NEEDS_REVIEW), reasons=["glyph_conflict"],
        evidence={"lane": "vl", "transcription": {"attempted_kind": "table", "fallback_kind": "prose",
                  "anchor": None, "coverage": "unknown"}})
    record = {"revision": 2, "confirmed_against_image": True, "carried_over": False}
    old = {"kind": "table", "revision": 2, "payload": payload, "human_verification": record}
    monkeypatch.setattr(RAG, "_existing_human_entries", lambda *a, **k: ([old], [], {figure.figure_id: 2}))
    monkeypatch.setattr(fx, "may_carry_over_human_verification", lambda *a, **k: True)
    carried, records, _ = RAG._carry_over_human_verification(
        fx, "spec.pdf", tmp_path, tmp_path / "knowledge.json", {figure.figure_id: object()}, [figure])
    assert "glyph_conflict" not in carried[0].reasons, "New machine damage does not describe the carried human payload"
    assert "transcription" not in carried[0].evidence
    assert carried[0].payload == payload
    assert records[figure.figure_id] == record


@pytest.mark.smoke
@pytest.mark.parametrize("body", [
    "```c\n    if (ready || pending) {\n        reset();\n    }\n```",
    "firmware/\n|-- drivers/\n|   |-- spi.c\n|   `-- uart.c\n`-- main.c",
])
def test_pdf_normalization_retains_code_and_tree_bytes(monkeypatch, tmp_path, body):
    raw = "## Implementation\n\n" + body + "\n\nThe boot sequence is documented here.\n"
    lane = {"active": False, "preflight_only": False, "figures": [], "absent": [],
            "replacements": {}, "page_source": {}, "guard": None, "evidence_ref": {}}
    monkeypatch.setattr(RAG, "_figure_root", lambda root: tmp_path)
    monkeypatch.setattr(RAG, "_source_identity_snapshot", lambda *a: None)
    monkeypatch.setattr(RAG, "check_pymupdf4llm", lambda: types.SimpleNamespace(
        to_markdown=lambda *a, **k: [_page(1, raw)]))
    monkeypatch.setattr(RAG, "_run_structured_figure_lane", lambda *a, **k: lane)
    document = RAG.extract_pdf_document(str(tmp_path / "sample.pdf"), root=str(tmp_path))
    assert body in document.raw_text, "PDF normalization rewrites code operators or tree edges"
    assert any(body in c["content"] for c in document.chunks)


@pytest.mark.smoke
def test_builder_cannot_confirm_a_glyph_damaged_header():
    figure = dataclasses.replace(_result("spec.pdf::digest", "fig_0123456789abcdef",
        payload=_table_payload(["Nam▯", "Value"], [["READY", "1"]]), status=fx.VERIF_HUMAN),
        reasons=["human_corrected"])
    with pytest.raises(fx.FigureValidationError, match="human_verified|人工確認"):
        fx.build_figure_chunks([figure], source="spec.pdf", doc_type="spec",
            next_chunk_index={}, evidence_ref_by_figure={figure.figure_id: ".codetrail/figures/run/manifest.json"},
            human_verifications_by_figure={figure.figure_id: {
                "revision": figure.revision, "confirmed_against_image": True}})


@pytest.mark.smoke
def test_human_carryover_keeps_duplicate_source_provenance(monkeypatch, tmp_path):
    payload = _table_payload(["Name", "Value"], [["READY", "1"]])
    provenance = {"lane": "vl", "duplicate_of": "fig_abcdef0123456789",
                  "duplicate_model_input": {"figure_id": "fig_abcdef0123456789", "page": 1}}
    figure = dataclasses.replace(_result("spec.pdf::digest", "fig_0123456789abcdef",
        payload=payload, status=fx.VERIF_NEEDS_REVIEW), evidence=provenance)
    record = {"revision": 2, "confirmed_against_image": True, "carried_over": False}
    old = {"kind": "table", "revision": 2, "payload": payload, "human_verification": record}
    monkeypatch.setattr(RAG, "_existing_human_entries", lambda *a, **k: ([old], [], {figure.figure_id: 2}))
    monkeypatch.setattr(fx, "may_carry_over_human_verification", lambda *a, **k: True)
    carried, _, _ = RAG._carry_over_human_verification(
        fx, "spec.pdf", tmp_path, tmp_path / "knowledge.json", {figure.figure_id: object()}, [figure])
    assert {key: carried[0].evidence.get(key) for key in provenance} == provenance


@pytest.mark.smoke
def test_quality_is_separate_from_human_confirmation_in_review_and_query(monkeypatch, tmp_path):
    """The quality fields must survive both product views without bypassing strict trust."""
    import knowledge
    from tests._harness import import_mcp_module

    server = import_mcp_module(monkeypatch, tmp_path)
    quality = {"quality_grade": "partial", "review_state": "unreviewed",
               "auto_disposition": "repair_required", "quality_issues": ["unreadable_text"]}
    chunk = {"structured": True, "figure_id": "fig_0123456789abcdef", "revision": 1,
             "figure_kind": "terminal", "verification_status": "needs_review", **quality}
    kb = knowledge.KnowledgeBase.__new__(knowledge.KnowledgeBase)
    kb.chunks = [chunk]
    review_text = server._render_figure_entry(chunk, with_payload=False)
    ref_text = "\n".join(kb._structured_ref_lines(chunk, "needs_review", {}))
    for field in ("quality_grade", "review_state", "auto_disposition"):
        assert f"{field}: {quality[field]}" in review_text
        assert f"{field}: {quality[field]}" in ref_text
    assert knowledge._figure_quality_metadata(chunk) == quality
    clean_unverified = {**chunk, "quality_grade": "usable", "auto_disposition": "accept",
                        "quality_issues": [], "verification_status": "unverified"}
    assert kb._is_flagged_figure(clean_unverified), "Content quality cannot replace verification"


@pytest.mark.smoke
def test_context_generation_navigation_filter_reaches_shared_windows_and_summaries(monkeypatch):
    """A clean index must not reintroduce navigation through generated retrieval context."""
    import document_structure

    navigation = "## Contents\n1. NOISE_ONLY ........ 3\n2. More ........ 8\n3. End ........ 12\n"
    body = "\n## Runtime\nThe READY register records completed initialization.\n"
    document = ExtractedDocument(raw_text=navigation + body, source="spec.pdf",
        chunks=[{"content": body, "char_start": len(navigation), "char_end": len(navigation + body)}],
        navigation_spans=document_structure.navigation_spans(navigation + body))
    generator = context_generation.ContextGenerator.__new__(context_generation.ContextGenerator)
    generator.report = context_generation.GenerationReport()
    generator.n_ctx = 100000
    generator.identity = {"model": "offline"}
    generator.request_max_tokens = 100
    generator.max_ctx_tokens = 100
    generator.cache = types.SimpleNamespace(get=lambda key: None, put=lambda *a: None)
    sent = []
    monkeypatch.setattr(generator, "_generate_one", lambda messages: (sent.append(messages) or "READY context", None))
    generator.generate_for_document(document)
    assert sent and all("NOISE_ONLY" not in json.dumps(messages) for messages in sent)
    assert "READY register" in json.dumps(sent)
    summary = generator.document_summary(document, 10000)
    assert "NOISE_ONLY" not in summary and "READY register" in summary
    segments = []
    monkeypatch.setattr(generator, "_summarize_segment",
        lambda text, **kwargs: (segments.append(text) or "READY summary"))
    generator.document_summary(document, 8)
    assert segments and all("NOISE_ONLY" not in segment for segment in segments)
    assert document.raw_text == navigation + body


@pytest.mark.smoke
def test_failure_sources_and_detected_region_limits_survive_notification_boundary():
    """Preserve the action source without carrying free-form extraction text in stdout metadata."""
    failed = [RAG._summary_item({"figure_id": f"fig_{n:016x}", "page": n + 1,
        "extraction_status": "failed", "reasons": [reason], "evidence": {"lane": lane}})
        for n, (lane, reason) in enumerate((("native", "native_verify_failed"),
                                            ("vl", "transport"), ("vl", "transcription_empty")))]
    payload = {"schema": 1, "document": "spec.pdf", "failed": failed,
               "coverage": {"scope": "detected_regions", "detected": 3, "failed": 3,
                            "native_processed": 0, "vl_processed": 0, "absent": 2,
                            "excluded": 0, "unknown_channels": 1}}
    parsed = ingest_notify.parse_summary_line(ingest_notify.format_summary_line(payload))
    assert [item["lane"] for item in parsed["failed"]] == ["native", "vl", "vl"]
    assert [item["stage"] for item in parsed["failed"]] == [
        "native_verify_failed", "vl_transport_failed", "vl_sample_failed"]
    assert parsed["coverage"] == payload["coverage"]
    assert "不代表全 PDF OCR 完整" in "\n".join(ingest_notify.render_action_block(parsed))


@pytest.mark.smoke
def test_review_header_retains_manual_count_without_counting_repairs(monkeypatch, tmp_path):
    """I1: retain the existing count label while known damage gets separate handling."""
    from tests import test_figure_retrieval as helpers
    from tests._harness import tool_fn

    server = helpers._mcp(monkeypatch, tmp_path)
    helpers._stub_list(monkeypatch, [
        helpers._entry(figure_id=helpers.FIG, verification_status=fx.VERIF_NATIVE,
                       quality_grade="usable", auto_disposition="accept"),
        helpers._entry(figure_id=helpers.FIG2, verification_status=fx.VERIF_NEEDS_REVIEW,
                       quality_grade="partial", auto_disposition="repair_required"),
    ])
    out = tool_fn(server, "review_figures")(action="list")
    assert "0 張待覆核" in out, "The existing review-count label disappeared"
    assert "1 張需修復" in out and "不附 canonical payload" in out


@pytest.mark.smoke
@pytest.mark.parametrize("candidate_kind", ["unknown", "terminal"])
def test_non_table_candidate_still_replaces_its_native_table_span(monkeypatch, tmp_path, candidate_kind):
    """I2: non-table classification must not lose a proven native replacement anchor."""
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    fid = _figure_id(document_id, 1)
    candidate = _candidate(document_id, fid, kind=candidate_kind, native_lane=False,
                           native_table={"pos": POS1, "markdown": TABLE_MD})
    plan = _plan(document_id, [candidate], {1: FakePageEvidence(
        page=1, raw_markdown=PAGE1, page_boxes=[_table_box(POS1)])})
    result = _result(document_id, fid, status=fx.VERIF_UNVERIFIED,
                     model_input_variant="crop@200dpi")
    _harness(monkeypatch, tmp_path, [_page(1, PAGE1, [_table_box(POS1)])], plan, [result])
    document = RAG.extract_pdf_document(str(pdf), root=str(tmp_path))
    assert "表格已改以結構化" in document.raw_text, "Missing native span leaves duplicate plain/structured text"
    assert not any("CTRL0" in c["content"] for c in document.chunks if not c.get("structured"))
    assert sum(c["content"].count("CTRL0") for c in document.chunks) == 1


@pytest.mark.smoke
@pytest.mark.parametrize("status", ["needs_review", "unverified", "legacy_unverified"])
def test_strict_exclusion_hint_retains_review_label_and_repair_routing(monkeypatch, tmp_path, status):
    """I3: keep the strict review cue while known damage still requires repair."""
    from tests import test_figure_retrieval as helpers

    chunk = helpers._table_chunk(rows=(helpers.ROW_A,), span=(1, 1), status=status,
                                 reasons=("glyph_conflict", "missing_row"))
    chunk.update(quality_grade="partial", review_state="unreviewed",
                 auto_disposition="repair_required", quality_issues=["conflicting_text"])
    kb = helpers._stub_kb(monkeypatch, tmp_path, [chunk])
    model_text, display, meta = kb.query("CTRL0 的位址是多少？", is_strict_mode=True)

    assert "待覆核" in model_text, "The strict model hint lost its existing review cue"
    assert "manual_review（待覆核）才需人工判斷" in model_text
    assert "repair_required/excluded 需要修復或重新 ingest" in model_text
    assert "review_figures" in model_text and "待覆核" in display
    assert "0x4000_0100" not in model_text and meta.get("refs", []) == []
    assert meta["has_ref"] is False and meta.get("has_authoritative_chunk", False) is False
    assert meta["excluded_figures"][0]["auto_disposition"] == "repair_required"
