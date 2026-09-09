"""PDF code/tree preservation and conservative navigation exclusion contracts.

Fixtures are synthetic source evidence; no PDF reader, model, or private document
is needed. Existing planner regressions run directly against harvested-evidence
shapes so a missing new helper cannot masquerade as the red-before-green result.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import config
import extracted_document as ed
import figure_candidates as fc
import figure_extract as fe


CODE = "if (ready || retry) {\n    submit(buffer);\n    return 0;\n}\n"
ASCII_TREE = "firmware/\n|-- src/\n|   |-- main.c\n|   `-- board.c\n`-- include/\n"
UNICODE_TREE = "firmware/\n├── src/\n│   ├── main.c\n│   └── board.c\n└── include/\n"
NAVIGATION = "Contents\n1. Introduction ........ 3\n2. Setup ............... 8\n3. Runtime ............ 12\n"
BBOX = (60.0, 90.0, 460.0, 210.0)


def _evidence(text, *, pos=True, geometry=True, ruled=False):
    boxes = []
    if pos:
        boxes.append({"class": "table" if geometry else "text", "_ordinal": 0,
                      "_pos": (0, len(text)), "_bbox_unrotated": BBOX,
                      "bbox": BBOX, "pos": (0, len(text)), "_space": "identity"})
    cells = [[(60.0, 90.0, 260.0, 150.0), (260.0, 90.0, 460.0, 150.0)],
             [(60.0, 150.0, 260.0, 210.0), (260.0, 150.0, 460.0, 210.0)]]
    tables = {"lines": [{"bbox": BBOX, "ordinal": 0, "degenerate": False,
                         "geometry": {"row_count": 2, "col_count": 2,
                                      "cells": cells}}]} if geometry else {}
    words = [(70.0, 100.0 + i * 15, 70.0 + len(line) * 5, 110.0 + i * 15,
              line, 0, i, 0) for i, line in enumerate(text.splitlines()) if line]
    drawings = ([(60, y, 460, y + 1) for y in (90, 150, 209)]
                + [(x, 90, x + 1, 210) for x in (60, 459)]) if ruled else []
    return fc.PageEvidence(
        page=1, raw_markdown=text, page_boxes=boxes, words=words, image_info=[],
        tables=tables, drawing_clusters=[BBOX] if ruled else [],
        page_rect=(0.0, 0.0, 612.0, 792.0), rotation=0, unavailable=[],
        overlays={"drawing_rects": drawings},
    )


def _plan(evidence, tmp_path, monkeypatch):
    source = tmp_path / "synthetic-structure.pdf"
    source.write_bytes(b"%PDF-1.7 synthetic identity only")
    monkeypatch.setattr(fc, "harvest_page_evidence", lambda *args, **kwargs: evidence)
    return fc.plan_document_figures(
        str(source), [{"metadata": {"page_number": 1}, "text": evidence.raw_markdown}],
        root=tmp_path, pdf_doc=SimpleNamespace(page_count=1),
    )


@pytest.mark.smoke
@pytest.mark.parametrize("text,role", [(CODE, "code"), (ASCII_TREE, "file_tree"),
                                      (UNICODE_TREE, "file_tree")])
def test_code_and_file_tree_override_native_table_geometry(text, role, tmp_path, monkeypatch):
    """find_tables geometry must not flatten source code or parent-child lines."""
    plan = _plan(_evidence(text), tmp_path, monkeypatch)
    assert len(plan.candidates) == 1, plan.stats
    candidate = plan.candidates[0]
    assert candidate.kind == fe.KIND_TERMINAL, candidate.reasons
    assert candidate.native_table is None
    assert candidate.signals["content_role"] == role
    assert candidate.signals["native_lane"] is True
    assert candidate.signals["native_text"]["pos"] == (0, len(text))
    assert candidate.signals["native_text"]["markdown"] == text
    assert plan.preflight["vl_calls_min"] == plan.preflight["vl_calls_max"] == 0


@pytest.mark.smoke
@pytest.mark.parametrize("text", [CODE, ASCII_TREE, UNICODE_TREE])
def test_code_without_pos_is_deferred_instead_of_vl(text, tmp_path, monkeypatch):
    """Word boxes cannot establish canonical indentation, even with a fake grid."""
    plan = _plan(_evidence(text, pos=False), tmp_path, monkeypatch)
    assert plan.candidates == [], [(c.kind, c.reasons) for c in plan.candidates]
    assert {item["reason"] for item in plan.stats["absent_regions"]} == {
        "code_block_without_pos"}
    assert plan.preflight["vl_calls_max"] == 0


@pytest.mark.smoke
def test_navigation_candidates_do_not_reserve_ocr_budget(tmp_path, monkeypatch):
    """A TOC with drawn rows used to consume VL budget as an unanchored table."""
    monkeypatch.setattr(config, "FIGURE_MAX_VL_CALLS_PER_DOC", 0)
    plan = _plan(_evidence(NAVIGATION, geometry=False, ruled=True), tmp_path, monkeypatch)
    assert len(plan.candidates) == 1, plan.stats
    assert plan.preflight["vl_calls_min"] == plan.preflight["vl_calls_max"] == 0
    assert plan.preflight["image_tokens_est"] == 0
    assert plan.over_budget == []
    assert plan.candidates[0].signals["content_role"] == "navigation"


@pytest.mark.smoke
def test_fenced_heading_lines_do_not_create_sections():
    """A source comment inside a fence used to become a retrieval heading."""
    source = "# Setup\nInstructions.\n```c\n# BUILD OPTIONS\n1.2 PARSER FLAGS\n```\n## Runtime\nBody.\n"
    sections = ed.extract_sections(source)
    assert [section.title for section in sections] == ["Setup", "Runtime"]
    assert sections[0].char_span == (0, source.index("## Runtime"))
    assert sections[1].char_span == (source.index("## Runtime"), len(source))


@pytest.mark.smoke
def test_navigation_title_does_not_swallow_prose_with_trailing_numbers():
    """A TOC run must stop before measured values or a new Markdown heading."""
    from document_structure import navigation_spans

    source = NAVIGATION + "\nMeasured duration 12\n## Configuration 3\nBody.\n"
    assert navigation_spans(source) == [(0, len(NAVIGATION))]


# New silent-loss contracts. Written for root's smoke / reviewer execution; they
# are not a reason for developers to run the whole file during implementation.
@pytest.mark.smoke
def test_structure_preserving_normalization_keeps_original_code_and_tree_bytes():
    from document_structure import normalize_preserving_structure

    code = "```c\r\n\tif (ready || retry) {  \r\n\t\tsubmit(buffer);\r\n\t}\r\n```\r\n"
    source = "  Intro.\n\n" + code + "\n" + ASCII_TREE + "\n| Key | Value |\n| --- | --- |\n| MODE | 1 |\n"
    normalized = normalize_preserving_structure(source, ed.normalize_document_text)
    assert code.encode("utf-8") in normalized.encode("utf-8")
    assert ASCII_TREE.encode("utf-8") in normalized.encode("utf-8")
    assert "Key: Value" in normalized and "MODE: 1" in normalized
    assert "```\r\n\nfirmware/" in normalized
    assert ed.normalize_document_text("| Key | Value |") == "Key: Value"
    # The direct legacy utility remains deliberately lossy; protection is opt-in.
    assert ed.normalize_document_text("if (ready || retry) {") == "if (ready: retry) {"


@pytest.mark.smoke
def test_navigation_spans_only_exclude_confirmed_runs_and_keep_source_offsets():
    from document_structure import classify_text_role, navigation_spans

    source = NAVIGATION + "\n## Runtime Details\nUse the directory listed below.\n" + ASCII_TREE
    spans = navigation_spans(source)
    assert spans == [(0, len(NAVIGATION))]
    assert source[slice(*spans[0])] == NAVIGATION
    assert classify_text_role(source) == "content"
    assert classify_text_role(NAVIGATION) == "navigation"
    assert classify_text_role("目錄結構\n" + ASCII_TREE) == "file_tree"
    assert navigation_spans("目錄結構\n" + ASCII_TREE) == []
    continued = "3. Runtime ............ 12\n4. Errors ............. 18\n5. Appendix ........... 21\n"
    assert navigation_spans(continued) == [(0, len(continued))]
    assert navigation_spans("Directory\n/src 12\n/include 18\n") == []
    assert navigation_spans("## Runtime\nThe latency is 12\nThe limit is 18\n") == []
    sections = ed.extract_sections(source, exclude_spans=spans)
    assert [section.title for section in sections if section.title] == ["Runtime Details"]
    assert sections[-1].char_span[0] == source.index("## Runtime Details")
    document = ed.ExtractedDocument(source, navigation_spans=spans)
    assert document.raw_text == source and document.navigation_spans == spans
    assert ed.ExtractedDocument("").navigation_spans == []


@pytest.mark.smoke
def test_real_table_with_one_code_or_path_cell_remains_a_table(tmp_path, monkeypatch):
    from document_structure import classify_text_role, protected_text_spans

    source = "| Key | Value |\n| --- | --- |\n| PATH | /src/main.c |\n| CONDITION | if (a || b) { |\n"
    assert classify_text_role(source) == "content"
    assert protected_text_spans(source) == []
    plan = _plan(_evidence(source), tmp_path, monkeypatch)
    assert len(plan.candidates) == 1
    assert plan.candidates[0].kind == fe.KIND_TABLE
    assert plan.candidates[0].native_table is not None
    assert plan.candidates[0].signals["native_lane"] is True


@pytest.mark.smoke
def test_navigation_rows_survive_legacy_two_column_normalization():
    from document_structure import navigation_spans, normalize_preserving_structure

    source = "## List of Tables\n| Title | Page |\n| --- | --- |\n| Table 1. Registers | 5 |\n| Table 2. Timing | 9 |\n"
    normalized = normalize_preserving_structure(source, ed.normalize_document_text)
    assert "Table 1. Registers: 5" in normalized
    assert navigation_spans(normalized) == [(0, len(normalized))]


@pytest.mark.smoke
def test_code_boundary_adjacent_to_table_is_protected_without_swallowing_table():
    from document_structure import normalize_preserving_structure, protected_text_spans

    table = "| Key | Value |\n| --- | --- |\n| MODE | 1 |\n"
    source = table + CODE
    assert protected_text_spans(source) == [(len(table), len(source))]
    normalized = normalize_preserving_structure(source, ed.normalize_document_text)
    assert "MODE: 1\nif (ready || retry) {" in normalized
    assert normalized.endswith(CODE)


@pytest.mark.smoke
def test_navigation_budget_exclusion_still_checks_native_lane_bool():
    candidate = SimpleNamespace(kind=fe.KIND_TABLE, signals={
        "native_lane": "false", "content_role": "navigation"})
    with pytest.raises(fe.FigureExtractionError):
        fc._vl_profile(candidate)


@pytest.mark.smoke
def test_code_without_pos_cannot_reenter_as_attached_raster(tmp_path, monkeypatch):
    from dataclasses import replace

    evidence = _evidence(CODE, pos=False)
    evidence = replace(evidence, image_info=[{
        "bbox": BBOX, "ordinal": 0, "xref": 1, "digest_hex": "a" * 32,
        "width": 800, "height": 240,
    }])
    plan = _plan(evidence, tmp_path, monkeypatch)
    assert plan.candidates == []
    assert plan.preflight["vl_calls_max"] == 0
    assert "code_block_without_pos" in {
        item["reason"] for item in plan.stats["absent_regions"]}
