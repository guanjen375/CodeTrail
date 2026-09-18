"""Synthetic reproductions of the reported PDF coverage and empty-row failures."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

import RAG
import figure_candidates as fc
import figure_extract as fx
import figure_quality
import figure_verify as fv
import ingest_checkpoint
import ingest_notify
import table_lookup
from tests.test_figure_verify import candidate, page_evidence


def _deferred():
    return {"page": 1, "bbox": [10.0, 10.0, 300.0, 100.0],
            "channel": "page_boxes:text", "reason": "no_positive_table_or_terminal_signal"}


@pytest.mark.smoke
def test_deferred_native_prose_is_not_reported_missing(monkeypatch, tmp_path, capsys):
    """The same native page survives normal ingest, resume and a forced page redo."""
    import media
    monkeypatch.setattr(media, "_SANDBOX_ROOT", None)
    text = "## Boot sequence\nAfter U-Boot starts, enter npx then npxmain in the console.\n"
    page = {"metadata": {"page_number": 1}, "text": text, "page_boxes": []}
    pdf = tmp_path / "boot.pdf"
    pdf.write_bytes(b"synthetic source identity")
    plan = SimpleNamespace(document_id="boot.pdf::0123456789abcdef", candidates=[],
        over_budget=[], preflight={"pages": 1, "candidates": 0},
        stats={"display_name": "boot.pdf", "absent_regions": [_deferred()]})
    lane = {"active": False, "preflight_only": False, "figures": [],
            "absent": [_deferred()], "replacements": {}, "page_source": {},
            "guard": None, "evidence_ref": {}}
    calls = []
    parser = SimpleNamespace(to_markdown=lambda *a, **k: calls.append(k) or [copy.deepcopy(page)])
    monkeypatch.setattr(RAG, "check_pymupdf4llm", lambda: parser)
    monkeypatch.setattr(RAG, "_source_identity_snapshot", lambda *a: None)
    monkeypatch.setattr(RAG, "_open_pdf_document",
                        lambda *a: SimpleNamespace(page_count=1, close=lambda: None))
    monkeypatch.setattr(RAG, "_run_structured_figure_lane", lambda *a, **k: copy.deepcopy(lane))
    outputs = []
    for options in ({"resume": False}, {"resume": True}, {"resume": True, "redo_pages": [1]}):
        with ingest_checkpoint.IngestJob(tmp_path, pdf, configuration={"test": 1},
                options=ingest_checkpoint.ResumeOptions(**options)):
            document = RAG.extract_pdf_document(str(pdf), root=str(tmp_path))
            assert "npxmain" in "\n".join(chunk["content"] for chunk in document.chunks)
            outputs.append(capsys.readouterr().out)
            summary = ingest_notify.parse_summary_line(
                RAG._ingest_summary_line(document, document.chunks, None))
            assert summary["absent_total"] == 1
            assert summary["absent"][0].get("native_text_status") == "page_chunks_present"
    assert len(calls) == 2, "resume must use the saved page; redo must parse it again"
    assert all("查詢時不會出現" not in out for out in outputs), outputs
    assert all("原生正文" in out and "已有文字 chunk" in out for out in outputs), outputs
    preflight = fc.format_preflight_report(plan)
    assert "不會進 KB 的頁 / 區域" not in preflight, preflight
    assert "預計未收為結構化圖面" in preflight and "尚未入庫" in preflight
    assert "no_positive_table_or_terminal_signal" in preflight


@pytest.mark.smoke
def test_pdf_absence_guidance_uses_exposed_tool():
    """A real text failure stays visible; a figure omission does not imply it."""
    entries = [_deferred(), {"page": 2, "bbox": None, "channel": "text",
                            "reason": "rotated_90_text_unavailable"},
               {"page": 3, "bbox": None, "channel": "page",
                "reason": "structured_lane_inactive"}]
    raw = RAG.format_absent_regions("boot.pdf", entries)
    block = "\n".join(ingest_notify.render_action_block({
        "document": "boot.pdf", "absent": entries, "absent_total": len(entries)}))
    for rendered in (raw, block):
        assert "read_pdf" not in rendered, rendered
        assert "analyze_file(path=" in rendered and "pages=" not in rendered
        assert "原生正文未能讀取" in rendered
        assert "structured_lane_inactive" in rendered
        assert "原生正文是否收錄未知" in rendered


def _table_fixture(*, obstruction=None):
    matrix = [["Register", "Address", "Reset"], ["", "", ""],
              ["CTRL", "0x4000", "0x0001"], ["", "", ""],
              ["STATUS", "0x4004", "0x0000"]]
    bands = [(0, 20), (20, 30), (30, 50), (50, 60), (60, 80)]
    cells = [[(col * 100.0, top, (col + 1) * 100.0, bottom) for col in range(3)]
             for top, bottom in bands]
    words = [(col * 100.0 + 5, top + 3, col * 100.0 + 90, bottom - 3,
              value, 0, row, col)
             for row, ((top, bottom), values) in enumerate(zip(bands, matrix))
             for col, value in enumerate(values) if value]
    markdown = ("| Register | Address | Reset |\n|---|---|---|\n"
                "| CTRL | 0x4000 | 0x0001 |\n| STATUS | 0x4004 | 0x0000 |\n")
    geometry = {"table_bbox": (0.0, 0.0, 300.0, 80.0), "row_count": 5,
                "col_count": 3, "cells": cells, "extract_raw": matrix,
                "extract_unreliable_underscore": True}
    cand = candidate(page=1, bbox=(0.0, 0.0, 300.0, 80.0), native_table={
        "pos": (0, len(markdown)), "markdown": markdown, "strategy": "text",
        "geometry": geometry})
    cand.document_id = "docs/registers.pdf::0123456789abcdef"
    evidence = page_evidence(page=1, raw_markdown=markdown, words=words, tables={"text": [{
        "strategy": "text", "ordinal": 0, "degenerate": False,
        "bbox": cand.bbox, "geometry": geometry}]})
    evidence.overlays = {"drawing_rects": [], "drawing_ink": [], "annots": [], "widgets": []}
    if obstruction == "none":
        matrix[1] = [None, None, None]
    elif obstruction == "text":
        matrix[1][0] = "MISSED"
        words.append((5, 22, 80, 28, "MISSED", 0, 1, 0))
    elif obstruction == "glyph":
        matrix[1][0] = fx.UNREADABLE_GLYPH
    elif obstruction == "raster":
        evidence.image_info = [{"bbox": (110, 22, 118, 28)}]
    elif obstruction == "raster_edge":
        cand.bbox = (0, 0, 350, 80)
        evidence.image_info = [{"bbox": (330, 22, 338, 28)}]
    elif obstruction == "drawing":
        evidence.overlays["drawing_rects"] = [(110, 22, 118, 28)]
        evidence.overlays["drawing_ink"] = [{"bbox": [110, 22, 118, 28], "segments": []}]
    elif obstruction == "unavailable":
        evidence.unavailable = ["drawings:ValueError"]
    return cand, evidence


@pytest.mark.smoke
def test_native_table_blank_anchor_rows_do_not_require_repair(monkeypatch, tmp_path):
    """Reported 4-anchor/2-payload table must verify and support exact lookups."""
    cand, evidence = _table_fixture()
    result = fv.verify_native_table(cand, evidence)
    assert result.verification_status == fx.VERIF_NATIVE, result.reason_details
    assert all(result.evidence["native"]["checks"].values())
    assert [[cell["text"] for cell in row["cells"]] for row in result.payload["rows"]] == [
        ["CTRL", "0x4000", "0x0001"], ["STATUS", "0x4004", "0x0000"]]
    quality = figure_quality.assess_quality(result.payload, "table", reasons=result.reasons,
        evidence=result.evidence, extraction_status="complete", verification_status=result.verification_status)
    assert quality["quality_grade"] == "usable" and quality["auto_disposition"] == "accept"
    chunks = fx.build_figure_chunks([result], source="registers.pdf", doc_type="spec",
        next_chunk_index={}, evidence_ref_by_figure={result.figure_id: ".codetrail/figures/test/manifest.json"})
    assert chunks[0]["verification_status"] == fx.VERIF_NATIVE
    entry = {**chunks[0], **quality, "kind": "table", "payload": result.payload,
             "extraction_status": "complete", "in_kb": True, "revision": 1,
             "document_id": cand.document_id, "evidence": result.evidence, "reasons": result.reasons}
    monkeypatch.setattr(table_lookup.evidence_store, "snapshot", lambda root: {"chunks": chunks})
    monkeypatch.setattr(table_lookup.figure_review, "list_figures", lambda *a, **k: [entry])
    for register, value in (("CTRL", "0x0001"), ("STATUS", "0x0000")):
        found = table_lookup.query_table(tmp_path, figure_id=result.figure_id,
                                        register=register, column="Reset")
        assert found["has_ref"] is True and found["matches"][0]["value"] == value, found
    for name in ("words_geometry", "find_tables:text"):
        alignment = result.evidence["row_alignment"][name]
        assert alignment["anchor_rows"] == 4 and alignment["effective_anchor_rows"] == 2
        assert alignment["blank_rows_dropped"] == [1, 3] and alignment["row_map"] == [2, 4]
        assert alignment["raw_grid"]["rows"] == evidence.tables["text"][0]["geometry"]["extract_raw"][1:]
    assert len(cand.native_table["geometry"]["cells"]) == 5, "original geometry must remain auditable"


@pytest.mark.smoke
@pytest.mark.parametrize("obstruction", ["none", "text", "glyph", "raster", "raster_edge", "drawing", "unavailable"])
def test_nonempty_or_unknown_anchor_rows_still_block_verification(obstruction):
    cand, evidence = _table_fixture(obstruction=obstruction)
    result = fv.verify_native_table(cand, evidence)
    assert result.verification_status == fx.VERIF_NEEDS_REVIEW
    assert "missing_rows" in result.reasons
    if obstruction == "none":
        assert fv._entry_grid(evidence.tables["text"][0])["rows"][0] == [None, None, None]


@pytest.mark.smoke
def test_recovered_cross_page_header_keeps_source_row_mapping():
    cand, evidence = _table_fixture()
    result = fv.verify_native_table(cand, evidence)
    shifted = fv._shift_table_evidence_for_recovered_row(
        result.evidence, [column["label"] for column in result.payload["columns"]], previous=result)
    for name in ("words_geometry", "find_tables:text"):
        rows = shifted["row_alignment"][name]
        assert rows["anchor_rows"] == 5 and rows["effective_anchor_rows"] == 3
        assert rows["row_map"] == [0, 2, 4] and rows["blank_rows_dropped"] == [1, 3]
        grid = shifted["native"]["channel_grids"][name]
        assert grid["rows"] == 3 and grid["raw_rows"] == 5 and grid["row_map"] == [0, 2, 4]


@pytest.mark.smoke
def test_table_grid_rules_are_distinguished_from_vector_content():
    cand, evidence = _table_fixture()
    drawings = []
    for top, bottom in ((0, 30), (30, 60), (60, 80)):
        for col in range(3):
            rect = (col * 100, top, (col + 1) * 100, bottom)
            drawings.append({"rect": rect, "color": (0, 0, 0), "fill": None,
                             "width": 0.7, "items": [("re", rect, 1)]})
    evidence.overlays["drawing_rects"] = [draw["rect"] for draw in drawings]
    evidence.overlays["drawing_ink"] = fc._drawing_ink_evidence(drawings)
    result = fv.verify_native_table(cand, evidence)
    assert result.verification_status == fx.VERIF_NATIVE, result.reason_details
    # A rectangle inside one blank row is content, even though its primitives
    # have exactly the same shape as the table's rectangular border paths.
    icon = (110, 22, 118, 28)
    drawings.append({"rect": icon, "color": (0, 0, 0), "fill": None,
                     "width": 0.7, "items": [("re", icon, 1)]})
    evidence.overlays["drawing_rects"].append(icon)
    evidence.overlays["drawing_ink"] = fc._drawing_ink_evidence(drawings)
    result = fv.verify_native_table(cand, evidence)
    assert result.verification_status == fx.VERIF_NEEDS_REVIEW
    assert "missing_rows" in result.reasons


@pytest.mark.smoke
def test_blank_row_mapping_preserves_rowspan_source_identity():
    cand, evidence = _table_fixture()
    geometry = cand.native_table["geometry"]
    # Remove the second separator, keep a leading separator before two rows
    # whose first column is one merged cell containing a single source word.
    geometry["cells"].pop(3)
    geometry["extract_raw"].pop(3)
    geometry["row_count"] = 4
    span = (0, 30, 100, 80)
    geometry["cells"][2][0] = geometry["cells"][3][0] = span
    geometry["extract_raw"][2][0] = "GROUP"
    geometry["extract_raw"][3][0] = None
    evidence.words = [word for word in evidence.words if word[4] not in {"CTRL", "STATUS"}]
    evidence.words.append((5, 33, 90, 47, "GROUP", 0, 2, 0))
    markdown = evidence.raw_markdown.replace("CTRL", "GROUP").replace("STATUS", "")
    evidence.raw_markdown = markdown
    cand.native_table.update(markdown=markdown, pos=(0, len(markdown)))
    result = fv.verify_native_table(cand, evidence)
    inherited = result.payload["rows"][1]["cells"][0]
    assert inherited["text"] == "GROUP" and inherited["state"] == "inherited"
    assert inherited["inherited_from_row"] == 1
    assert result.evidence["row_alignment"]["words_geometry"]["row_map"] == [2, 3]


@pytest.mark.smoke
def test_table_harvest_keeps_unknown_cells_and_summary_uses_committed_text():
    cand, evidence = _table_fixture(obstruction="none")
    geometry = cand.native_table["geometry"]
    fake_table = SimpleNamespace(bbox=cand.bbox, row_count=5, col_count=3, header=None,
        rows=[SimpleNamespace(cells=row) for row in geometry["cells"]],
        extract=lambda: geometry["extract_raw"])
    captured = fc._table_entry(fake_table, "text", 0)
    assert captured["geometry"]["extract_raw"][1] == [None, None, None]
    assert fv._entry_grid(captured)["rows"][0] == [None, None, None]
    from extracted_document import ExtractedDocument
    document = ExtractedDocument(raw_text="source", source="boot.pdf")
    setattr(document, RAG._ABSENT_ATTR, [{**_deferred(), "native_text_status": "page_chunks_present"}])
    # MinerU may replace native chunks after the native extraction log. The
    # final summary must derive its claim from the chunks actually committed.
    committed = [{"source": "boot.pdf", "page": 1, "content": "OCR text", "text_lane": "mineru"}]
    parsed = ingest_notify.parse_summary_line(RAG._ingest_summary_line(document, committed, None))
    assert "native_text_status" not in parsed["absent"][0]
