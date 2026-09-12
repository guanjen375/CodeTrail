"""Offline contracts for source binding, single ownership, and OCR provenance.

These fixtures are synthetic. They neither launch MinerU nor call a model or
read a private PDF. The normal native admission layer is represented by already
admitted structured chunks, so no test grants verification to MinerU text.
"""
from __future__ import annotations

import copy
import hashlib
import json
from functools import partial

import pytest

import mineru_lane as lane
from extracted_document import ExtractedDocument, Section


pytestmark = pytest.mark.smoke
PDF_BYTES = b"%PDF-1.7 synthetic local source for a mocked geometry reader\n"
TABLE_BODY = "<table><tr><td>ORIGINAL_OCR_CELL</td><td>0x20</td></tr></table>"


def _text(text, *, page=0, bbox=(10, 10, 450, 60), level=0, kind="text"):
    block = {"type": kind, "text": text, "page_idx": page, "bbox": list(bbox)}
    if level:
        block["text_level"] = level
    return block


def _table(*, page=0, bbox=(50, 300, 450, 500)):
    return {"type": "table", "table_body": TABLE_BODY, "page_idx": page,
            "bbox": list(bbox), "table_caption": ["Table 1. Control words"],
            "table_footnote": ["The reset value is implementation dependent."],
            "img_path": "/must/not/be/opened.png"}


def _artifact(tmp_path, monkeypatch, blocks, *, page_count=1):
    pdf = tmp_path / "synthetic.pdf"
    pdf.write_bytes(PDF_BYTES)
    content = tmp_path / "content_list.json"
    content.write_bytes(json.dumps(blocks, ensure_ascii=False, indent=2).encode("utf-8"))
    pages = tuple(lane.MineruPageGeometry(page, (0, 0, 1000, 1000), (1, 0, 0, 1, 0, 0),
                                          (0, 0, 1000, 1000))
                  for page in range(1, page_count + 1))
    monkeypatch.setattr(lane, "_pdf_pages", lambda _data: pages)
    return lane.load_artifact(content, pdf, hashlib.sha256(PDF_BYTES).hexdigest(), root=tmp_path)


def _figure(fid="fig_table", *, kind="table", page=1, bbox=(50, 300, 450, 500), **extra):
    return {"source": "synthetic.pdf", "page": page, "chunk_index": 0,
            "content": "Native canonical structured payload", "type": "doc",
            "structured": True, "origin": f"figure_{kind}", "figure_kind": kind,
            "figure_id": fid, "document_id": "synthetic.pdf::native", "bbox": list(bbox),
            "occurrences": [{"page": page, "bbox": list(bbox), "index": 1}],
            "section": "STALE NATIVE HEADING", "heading_hierarchy": "STALE NATIVE HIERARCHY",
            "figure_caption": "STALE NATIVE CAPTION", "char_start": 0, "char_end": 0,
            "row_total": 1 if kind == "table" else None,
            "verification_status": "needs_review", "quality_grade": "partial",
            "review_state": "unreviewed", "auto_disposition": "repair_required",
            "quality_issues": ["uncertain_text"], "revision": 2,
            "evidence_ref": ".codetrail/figures/run/manifest.json", **extra}


def _build(artifact, figures=(), *, splitter=None, native=None):
    from RAG import split_by_semantic_with_sections

    if native is None:
        native = ExtractedDocument("# STALE NATIVE\nBody", [Section("STALE NATIVE", 1, (0, 19))],
                                   list(figures), source="synthetic.pdf")
    return lane.build_document(artifact, native,
                               split_chunks=splitter or split_by_semantic_with_sections)


def _bodies(document):
    return [chunk["content"][chunk.get("heading_prefix_chars", 0):]
            for chunk in document.chunks if chunk.get("origin") == "mineru_text"]


def test_source_digest_is_the_callers_generating_pdf_digest(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path, monkeypatch, [_text("Source words.")])
    with pytest.raises(lane.MineruLaneError, match="generating PDF SHA-256"):
        lane.load_artifact(artifact.content_list_path, artifact.pdf_path, "0" * 64, root=tmp_path)
    assert artifact.pdf_sha256 == hashlib.sha256(PDF_BYTES).hexdigest()
    assert artifact.content_list_sha256 == hashlib.sha256(artifact.content_list_bytes).hexdigest()
    document = _build(artifact)
    assert document.chunks[0]["source_sha256"] == artifact.pdf_sha256
    assert document.chunks[0]["content_list_sha256"] == artifact.content_list_sha256


@pytest.mark.parametrize("target", ["pdf", "json"])
def test_revalidation_rejects_changed_bytes_or_replaced_same_bytes(tmp_path, monkeypatch, target):
    artifact = _artifact(tmp_path, monkeypatch, [_text("Source words.")])
    path = artifact.pdf_path if target == "pdf" else artifact.content_list_path
    original = path.read_bytes()
    path.write_bytes(original + b" ")
    with pytest.raises(lane.MineruError, match="changed after"):
        lane.revalidate_artifact(artifact)
    artifact = _artifact(tmp_path, monkeypatch, [_text("Source words.")])
    path = artifact.pdf_path if target == "pdf" else artifact.content_list_path
    replacement = tmp_path / "replacement"
    replacement.write_bytes(path.read_bytes())
    replacement.replace(path)
    with pytest.raises(lane.MineruError, match="changed after"):
        lane.revalidate_artifact(artifact)


@pytest.mark.parametrize("target", ["file", "parent", "root"])
def test_symlink_components_are_rejected_without_writes(tmp_path, monkeypatch, target):
    real = tmp_path / "real"
    real.mkdir()
    artifact = _artifact(real, monkeypatch, [_text("Source words.")])
    link = tmp_path / "link"
    if target == "file":
        link.symlink_to(artifact.content_list_path)
        path, root = link, tmp_path
    else:
        link.symlink_to(real, target_is_directory=True)
        path = link / "content_list.json"
        root = link if target == "root" else tmp_path
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    with pytest.raises(lane.MineruError, match="safely"):
        lane.load_artifact(path, (link if target == "root" else real) / "synthetic.pdf",
                           artifact.pdf_sha256, root=root)
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before


def test_parent_symlink_swap_after_loading_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    folder = root / "input"
    folder.mkdir()
    artifact = _artifact(folder, monkeypatch, [_text("Source words.")])
    archive = root / "archive"
    folder.rename(archive)
    folder.symlink_to(archive, target_is_directory=True)
    with pytest.raises(lane.MineruError, match="safely"):
        lane.revalidate_artifact(artifact)


def test_bounded_regular_reads_reject_escape_and_do_not_create_directories(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path, monkeypatch, [_text("Source words.")])
    missing = tmp_path / "missing" / "content_list.json"
    with pytest.raises(lane.MineruError, match="safely"):
        lane.load_artifact(missing, artifact.pdf_path, artifact.pdf_sha256, root=tmp_path)
    assert not missing.parent.exists()
    with pytest.raises(lane.MineruError, match="outside"):
        lane.load_artifact(tmp_path.parent / "outside.json", artifact.pdf_path,
                           artifact.pdf_sha256, root=tmp_path)
    with pytest.raises(lane.MineruError, match="regular file"):
        lane.load_artifact(tmp_path, artifact.pdf_path, artifact.pdf_sha256, root=tmp_path)
    monkeypatch.setattr(lane, "CONTENT_LIST_MAX_BYTES", 2)
    with pytest.raises(lane.MineruError, match="byte limit"):
        lane.load_artifact(artifact.content_list_path, artifact.pdf_path, artifact.pdf_sha256, root=tmp_path)


@pytest.mark.parametrize("change", [
    {"page_idx": -1}, {"page_idx": True}, {"page_idx": 1}, {"page_idx": 0.0},
    {"bbox": [0, 0, 1001, 30]}, {"bbox": [0, 0, 0, 0]}, {"bbox": [False, 0, 20, 30]},
    {"text_level": True}, {"text_level": 7}, {"text_level": -1},
    {"type": "future_readable_type"}, {"type": []}, {"table_body": "Unread parallel body"},
])
def test_invalid_page_geometry_heading_or_readable_schema_fails_loud(tmp_path, monkeypatch, change):
    with pytest.raises(lane.MineruError):
        _artifact(tmp_path, monkeypatch, [{**_text("Do not lose this text."), **change}])


@pytest.mark.parametrize("payload", [[], {"pages": []}, [_text(" ")],
                                    [{"type": "image", "page_idx": 0, "bbox": [1, 1, 20, 20],
                                      "img_path": "outside.png"}],
                                    [{"type": "code", "page_idx": 0, "bbox": [1, 1, 20, 20],
                                      "code_body": "  \n"}]])
def test_empty_or_wrong_version_artifacts_never_fall_back_to_native(tmp_path, monkeypatch, payload):
    with pytest.raises(lane.MineruError):
        _artifact(tmp_path, monkeypatch, payload)


def test_page_indices_and_missing_pages_keep_original_pdf_numbers(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path, monkeypatch,
                         [_text("First page.", page=0), _text("Third page.", page=2)], page_count=3)
    document = _build(artifact)
    assert [chunk["page"] for chunk in document.chunks] == [1, 3]
    assert [page for page, _start, _end in document.page_spans] == [1, 3]
    assert artifact.represented_pages == (1, 3)
    assert artifact.missing_pages == (2,)
    assert artifact.page_count == 3 and artifact.readable_page_count == 2
    assert document._codetrail_mineru_summary["missing_pages"] == [2]
    assert document._codetrail_mineru_summary["ocr_verification"] == "unverified"


def test_bbox_display_derotation_matches_native_crop_relative_space(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    pdf = tmp_path / "synthetic.pdf"
    with pymupdf.open() as source:
        page = source.new_page(width=600, height=800)
        page.set_cropbox(pymupdf.Rect(50, 100, 550, 700))
        page.set_rotation(90)
        pdf.write_bytes(source.tobytes())
    payload = [_text("Rotated source.", bbox=(100, 200, 600, 700))]
    content = tmp_path / "content_list.json"
    content.write_text(json.dumps(payload), encoding="utf-8")
    artifact = lane.load_artifact(content, pdf, hashlib.sha256(pdf.read_bytes()).hexdigest(), root=tmp_path)
    with pymupdf.open(pdf) as source:
        page = source[0]
        displayed = pymupdf.Rect(page.rect.width * .1, page.rect.height * .2,
                                 page.rect.width * .6, page.rect.height * .7)
        expected = displayed * page.derotation_matrix
        canonical_page = page.rect * page.derotation_matrix
    assert artifact.blocks[0].bbox == pytest.approx(tuple(expected), abs=1e-6)
    assert artifact.pages[0].unrotated_rect == pytest.approx(tuple(canonical_page), abs=1e-6)
    assert artifact.blocks[0].page == 1


def test_explicit_same_page_headings_are_the_only_section_owner(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path, monkeypatch, [
        _text("First", level=1, bbox=(10, 10, 400, 40)),
        _text("ALL CAPS BODY\n# Body comment\n1.2 NUMBERED BODY", bbox=(10, 60, 400, 120)),
        _text("Second", level=2, bbox=(10, 150, 400, 180)),
        _text("Second body.", bbox=(10, 200, 400, 240)),
    ])
    document = _build(artifact)
    assert [section.title for section in document.sections] == ["First", "Second"]
    assert [chunk["section"] for chunk in document.chunks] == ["First", "Second"]
    assert [chunk["heading_hierarchy"] for chunk in document.chunks] == ["First", "First > Second"]
    assert all("STALE NATIVE" not in chunk["content"] for chunk in document.chunks)
    assert "ALL CAPS BODY\n# Body comment\n1.2 NUMBERED BODY" in _bodies(document)[0]
    boundary = document.raw_text.index("## Second")
    assert document.sections[0].char_span[1] == boundary
    assert document.sections[1].char_span[0] == boundary
    assert document.chunks[0]["char_end"] <= boundary <= document.chunks[1]["char_start"]


def test_code_and_file_tree_characters_survive_chunking_and_locators(tmp_path, monkeypatch):
    from RAG import split_by_semantic_with_sections

    code = "\tif (ready || retry) {  \r\n\t\tsubmit(buffer);\r\n\t}\r\n"
    tree = "firmware/\n├── src/\n│   ├── main.c\n│   └── board.c\n└── include/\n"
    long_line = "    #" + "FLAGS | OPTION " * 50 + "END  \n"
    artifact = _artifact(tmp_path, monkeypatch, [
        _text("Code", level=1),
        {"type": "code", "code_body": code + long_line, "bbox": [10, 80, 900, 500], "page_idx": 0},
        _text(tree, bbox=(10, 600, 400, 900)),
    ])
    document = _build(artifact, splitter=partial(split_by_semantic_with_sections, max_chars=45))
    body = "".join(_bodies(document))
    assert code in body and tree in body and long_line in body
    assert body == document.raw_text
    for chunk in document.chunks:
        assert chunk["content"][chunk["heading_prefix_chars"]:] == document.raw_text[chunk["char_start"]:chunk["char_end"]]
    assert [section.title for section in document.sections] == ["Code"]


def test_navigation_exclusion_preserves_offsets_asides_and_footnotes(tmp_path, monkeypatch):
    navigation = "Contents\n1. Introduction ........ 3\n2. Setup ........ 8\n3. Registers ........ 12\n"
    artifact = _artifact(tmp_path, monkeypatch, [
        _text("Running header", kind="header"),
        _text(navigation, bbox=(10, 100, 900, 220)),
        _text("Operation", level=1, bbox=(10, 250, 500, 290)),
        _text("An aside with an important constraint.", kind="aside_text", bbox=(10, 350, 900, 420)),
        _text("Footnote: preserve the reset exception.", kind="page_footnote", bbox=(10, 700, 900, 770)),
        _text("7", kind="page_number", bbox=(10, 950, 80, 980)),
    ])
    document = _build(artifact)
    joined = "\n".join(_bodies(document))
    assert navigation in document.raw_text
    assert "Running header" in document.raw_text
    assert "........" not in joined and "Running header" not in joined
    assert "important constraint" in joined and "reset exception" in joined
    assert [section.title for section in document.sections if section.title] == ["Operation"]
    assert any(navigation.rstrip() in document.raw_text[start:end]
               for start, end in document.navigation_spans)


def test_first_page_table_uses_one_structured_owner_and_preserves_artifact(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path, monkeypatch, [_text("Registers", level=1), _table()])
    figure = _figure()
    second_part = {**figure, "content": "Canonical second part", "part_index": 1}
    document = _build(artifact, [figure, second_part])
    assert artifact.blocks[1].page == 1
    assert TABLE_BODY in artifact.page_markdown[0][1]
    assert TABLE_BODY.encode("utf-8") in artifact.content_list_bytes
    assert "ORIGINAL_OCR_CELL" not in document.raw_text
    assert "ORIGINAL_OCR_CELL" not in "\n".join(chunk["content"] for chunk in document.chunks)
    assert document.raw_text.count("figure=fig_table page=1 rows=1") == 1
    assert "implementation dependent" in document.raw_text
    figures = [chunk for chunk in document.chunks if chunk.get("structured")]
    assert len(figures) == 2
    assert all(chunk["section"] == "Registers" and chunk["page"] == 1 for chunk in figures)
    assert len({chunk["chunk_index"] for chunk in document.chunks}) == len(document.chunks)


@pytest.mark.parametrize("figures", [[], [_figure("fig_a"), _figure("fig_b")],
                                     [_figure(auto_disposition="excluded")],
                                     [_figure(page=2)]])
def test_table_missing_ambiguous_or_excluded_owner_fails_without_mutation(tmp_path, monkeypatch, figures):
    artifact = _artifact(tmp_path, monkeypatch, [_text("Registers", level=1), _table()], page_count=2)
    original = copy.deepcopy(figures)
    with pytest.raises(lane.MineruError, match="owner"):
        _build(artifact, figures)
    assert figures == original


def test_partial_prose_overlap_is_never_erased_as_a_whole_block(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path, monkeypatch, [
        _text("Registers", level=1),
        _text("Outside prose and an inside table value.", bbox=(10, 250, 600, 550)),
    ])
    figure = _figure()
    before = copy.deepcopy(figure)
    with pytest.raises(lane.MineruError, match="partial or ambiguous"):
        _build(artifact, [figure])
    assert figure == before
    assert "Outside prose" in artifact.page_markdown[0][1]


def test_figure_heading_requires_a_unique_block_on_the_same_page(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path, monkeypatch, [
        _text("First", level=1, bbox=(10, 10, 400, 40)),
        {"type": "image", "page_idx": 0, "bbox": [50, 100, 450, 200],
         "image_caption": ["Figure 1. First diagram"]},
        _text("Second", level=1, bbox=(10, 250, 400, 280)),
        {"type": "image", "page_idx": 0, "bbox": [50, 400, 450, 500],
         "image_caption": ["Figure 2. Second diagram"]},
    ], page_count=2)
    figures = [_figure("fig_first", kind="diagram", bbox=(50, 100, 450, 200)),
               _figure("fig_second", kind="diagram", bbox=(50, 400, 450, 500)),
               _figure("fig_absent", kind="diagram", page=2, bbox=(50, 100, 450, 200))]
    document = _build(artifact, figures)
    mapped = {chunk["figure_id"]: chunk for chunk in document.chunks if chunk.get("structured")}
    assert mapped["fig_first"]["section"] == "First"
    assert mapped["fig_second"]["section"] == "Second"
    assert mapped["fig_second"]["figure_caption"] == "Figure 2. Second diagram"
    assert mapped["fig_absent"]["section"] == ""
    assert mapped["fig_absent"]["heading_hierarchy"] == ""
    assert mapped["fig_absent"]["figure_caption"] == ""
    assert mapped["fig_absent"]["section_index"] == -1


def test_multiple_matching_blocks_leave_figure_heading_empty(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path, monkeypatch, [
        _text("First", level=1),
        _text("Inside diagram label one.", bbox=(70, 320, 200, 380)),
        _text("Inside diagram label two.", bbox=(250, 400, 400, 480)),
    ])
    document = _build(artifact, [_figure(kind="diagram")])
    figure = next(chunk for chunk in document.chunks if chunk.get("structured"))
    assert figure["section"] == figure["heading_hierarchy"] == figure["figure_caption"] == ""
    assert figure["section_index"] == -1


def test_mineru_text_ownership_preserves_native_quality_guards_and_absence(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path, monkeypatch, [
        _text("Current MinerU section", level=1),
        _text("MinerU owns this prose and terminal output.", bbox=(10, 100, 450, 200)),
        _table(),
    ])
    table, prose = _figure(), _figure("fig_prose", kind="prose", bbox=(10, 100, 450, 200))
    terminal = _figure("fig_terminal", kind="terminal", bbox=(500, 100, 900, 200))
    native = ExtractedDocument("Native source must remain auditable", chunks=[table, prose, terminal], source="synthetic.pdf")
    guard = {"wrote_run": True, "repair": [{"figure_id": "fig_table"}]}
    coverage = {"native_pages": [1], "unknown_pages": [2], "ocr_complete": False}
    absent = [{"page": 2, "reason": "native_channel_unavailable"}]
    native._codetrail_figure_prune = guard
    native._codetrail_pdf_coverage = coverage
    native._codetrail_absent_regions = absent
    document = _build(artifact, native=native)
    assert document._codetrail_figure_prune is guard
    assert document._codetrail_pdf_coverage is coverage
    assert document._codetrail_absent_regions is absent
    assert document._codetrail_mineru_native_document is native
    assert document._codetrail_mineru_summary["replaced_figure_ids"] == ["fig_prose", "fig_terminal"]
    assert not any(chunk.get("origin") in {"figure_prose", "figure_terminal"} for chunk in document.chunks)
    stored = next(chunk for chunk in document.chunks if chunk.get("structured"))
    for key in ("verification_status", "quality_grade", "review_state", "auto_disposition",
                "quality_issues", "revision", "content", "evidence_ref"):
        assert stored[key] == table[key]
    assert stored["heading_source"] == "mineru"
    assert all(chunk["verification_status"] == "unverified" and chunk["text_lane"] == "mineru"
               for chunk in document.chunks if not chunk.get("structured"))


def test_native_text_figure_without_mineru_body_fails_loud(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path, monkeypatch, [_text("Only a heading", level=1)])
    with pytest.raises(lane.MineruError, match="no body text"):
        _build(artifact, [_figure("fig_prose", kind="prose")])
