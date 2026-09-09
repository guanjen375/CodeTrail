"""PDF transcription regressions; real verifier paths with offline VL responses."""
from __future__ import annotations

import base64
import json

import pytest

import config
import figure_candidates
import figure_extract
import figure_verify
from tests import test_figure_verify as fixture


_DIAGRAM = json.dumps({
    "title": "summary", "labels": ["visible text"], "components": [],
    "relations": [], "values": [],
})


def _lines(*texts):
    return fixture.prose_json([(text, []) for text in texts])


def _tiles(_doc, candidate):
    count = len(candidate.signals["tile_plan"]["tiles"])
    return [fixture.variant(
        candidate.figure_id, tile_index=i if count > 1 else 0, tile_total=count,
        png=f"PNG-{i}".encode(), bbox=candidate.bbox,
    ) for i in range(1, count + 1)]


@pytest.mark.smoke
@pytest.mark.parametrize("attempted_kind", ["table", "terminal"])
@pytest.mark.parametrize("raster, tiles", [(False, 1), (True, 1), (True, 3)])
def test_empty_schema_uses_bounded_line_transcription(monkeypatch, attempted_kind,
                                                     raster, tiles):
    """An empty inferred schema used to silently replace text with a diagram summary."""
    monkeypatch.setattr(config, "FIGURE_EXTRACT_RETRIES", 1)
    spy = fixture.VLSpy({
        "figure_raster_kind_v1": fixture.raster_kind(attempted_kind),
        "figure_table": fixture.table_json(["Name"], []),
        "figure_terminal": _lines(),
        "figure_prose": lambda kw: _lines(
            "  " + base64.b64decode(kw["image_base64"]).decode()),
        "figure_diagram": _DIAGRAM,
    })
    fixture.install_vl(monkeypatch, spy)
    probed = []
    monkeypatch.setattr(figure_verify, "ensure_capability",
                        lambda **kwargs: probed.append(kwargs["kinds"]))
    candidate = fixture.candidate(
        kind="raster" if raster else attempted_kind,
        signals={"tile_plan": {"tiles": [{}] * tiles, "est_tokens": [120] * tiles}},
    )

    result = fixture.extract([candidate], {4: fixture.page_evidence()}, render=_tiles)[0]

    assert result.kind == "prose", "Line transcription must not become a diagram summary"
    assert [line["text"] for line in result.payload["lines"]] == [
        f"  PNG-{i}" for i in range(1, tiles + 1)]
    assert result.evidence["transcription"] == {
        "attempted_kind": attempted_kind, "fallback_kind": "prose",
        "anchor": None, "coverage": "unknown",
    }
    assert result.evidence.get("repeatability") is None, "Fallback has no second sample"
    assert "prose" in probed[0], "Every sent schema must be capability-probed first"
    assert "figure_diagram" not in spy.schema_names()
    assert spy.schema_names().count("figure_prose") == tiles
    assert result.evidence["extraction_attempts"][0]["failure"] == "empty_payload"
    profile = figure_candidates._vl_profile(candidate)
    assert profile["min"] <= len(spy.calls) <= profile["max"]


@pytest.mark.smoke
@pytest.mark.parametrize("attempted_kind", ["prose", "table", "terminal"])
def test_empty_transcription_fails_with_all_attempts_and_sent_inputs(monkeypatch,
                                                                  attempted_kind):
    """Empty prose is extraction failure; a diagram must not turn it into success."""
    spy = fixture.VLSpy({
        "figure_raster_kind_v1": fixture.raster_kind(attempted_kind),
        "figure_table": fixture.table_json(["Name"], []),
        "figure_terminal": _lines(), "figure_prose": _lines(),
        "figure_diagram": _DIAGRAM,
    })
    fixture.install_vl(monkeypatch, spy)
    fixture.pass_probe(monkeypatch)
    monkeypatch.setattr(figure_verify, "_grid_normalized_variants", lambda variants, ctx: [
        fixture.variant(v.figure_id, variant_id=v.variant_id + "+grid", png=v.png,
                        bbox=v.bbox) for v in variants])

    result = fixture.extract([fixture.candidate(kind="raster")],
                             {4: fixture.page_evidence()})[0]

    assert result.extraction_status == "failed", "An empty transcription is not content"
    assert result.payload is None and result.variants == []
    assert result.kind == "prose"
    assert result.reasons == ["extraction_failed", "transcription_empty"]
    assert "figure_diagram" not in spy.schema_names()
    assert result.evidence["transcription"] == {
        "attempted_kind": attempted_kind,
        "fallback_kind": None if attempted_kind == "prose" else "prose",
        "anchor": None, "coverage": "unknown",
    }
    attempts = result.evidence["extraction_attempts"]
    assert attempts[-1]["kind"] == "prose"
    assert attempts[-1]["failure"] == "transcription_empty"
    assert attempts[-1]["detail"]
    assert result.evidence["sent_variants"] == (
        ["crop@200dpi", "crop@200dpi+grid"] if attempted_kind == "table"
        else ["crop@200dpi"])
    assert result.evidence["raster_classification"]["kind"] == attempted_kind


@pytest.mark.smoke
def test_first_tile_none_transcribes_later_tiles_without_a_second_sample(monkeypatch):
    """A classifier only seeing blank tile one cannot suppress the later source text."""
    spy = fixture.VLSpy({
        "figure_raster_kind_v1": fixture.raster_kind("none"),
        "figure_prose": lambda kw: (
            _lines() if base64.b64decode(kw["image_base64"]) == b"PNG-1"
            else _lines("  child/", "    leaf.c")),
        "figure_diagram": _DIAGRAM,
    })
    fixture.install_vl(monkeypatch, spy)
    fixture.pass_probe(monkeypatch)
    candidate = fixture.candidate(kind="raster", signals={
        "tile_plan": {"tiles": [{}, {}], "est_tokens": [120, 120]},
    })

    result = fixture.extract([candidate], {4: fixture.page_evidence()}, render=_tiles)[0]

    assert result.kind == "prose", "Later tile content must remain a line transcription"
    assert [line["text"] for line in result.payload["lines"]] == ["  child/", "    leaf.c"]
    assert result.verification_status == "needs_review"
    assert result.evidence["transcription"] == {
        "attempted_kind": "none", "fallback_kind": "prose",
        "anchor": None, "coverage": "unknown",
    }
    assert result.evidence.get("repeatability") is None
    assert "blank_tile_dropped" in result.reasons
    profile = figure_candidates._vl_profile(candidate)
    assert profile["min"] <= len(spy.calls) <= profile["max"]
    assert "figure_diagram" not in spy.schema_names()


@pytest.mark.smoke
@pytest.mark.parametrize("source", ["visible\nsource tail", "visible but with a source tail"])
def test_transcription_coverage_measures_source_and_rechecks_duplicate(monkeypatch, source):
    """Output-only atom coverage omits absent source lines/tails and duplicate provenance."""
    spy = fixture.VLSpy({"figure_terminal": _lines("visible")})
    fixture.install_vl(monkeypatch, spy)
    fixture.pass_probe(monkeypatch)
    first = fixture.candidate(kind="terminal", page=4, seed="coverage-first")
    duplicate = fixture.candidate(kind="terminal", page=5, seed="coverage-duplicate")
    evidence = {4: fixture.page_evidence(), 5: fixture.page_evidence(
        page=5, raw_markdown=source,
        page_boxes=[{"bbox": duplicate.bbox, "pos": [0, len(source)]}],
    )}

    results = fixture.extract([first, duplicate], evidence)

    assert "transcription" in results[0].evidence, "Coverage must be explicitly unknown"
    assert results[0].evidence["transcription"]["coverage"] == "unknown"
    transcription = results[1].evidence["transcription"]
    assert transcription["anchor"]["source_lines"] == len(source.split("\n"))
    assert transcription["coverage"] == (0.5 if "\n" in source else 0.0)
    assert "transcription_source_incomplete" in results[1].reasons
    assert results[1].verification_status == "needs_review"
    assert results[1].variants == []
    assert len(spy.calls) == 2, "The duplicate must use the cached extraction, not new VL"


@pytest.mark.smoke
def test_repeated_raster_text_does_not_claim_source_coverage(monkeypatch):
    """Same-model agreement and unrelated native page words cannot prove raster coverage."""
    spy = fixture.VLSpy({"figure_raster_kind_v1": fixture.raster_kind("prose"),
                         "figure_prose": _lines("visible")})
    fixture.install_vl(monkeypatch, spy)
    fixture.pass_probe(monkeypatch)
    candidate = fixture.candidate(kind="raster", signals={"raster_auto": True})
    evidence = fixture.page_evidence(raw_markdown="visible", page_boxes=[
        {"bbox": candidate.bbox, "pos": [0, len("visible")]}])

    result = fixture.extract([candidate], {4: evidence})[0]

    assert "transcription" in result.evidence, "Repeating output is not a source denominator"
    assert result.evidence["transcription"]["coverage"] == "unknown"
    assert result.evidence["transcription"]["anchor"] is None
    assert result.verification_status == "unverified"
    assert result.evidence["repeatability"]["samples"] == 2
    assert result.evidence["repeatability"]["identical"] is True


@pytest.mark.smoke
def test_fallback_transport_still_aborts_the_document_with_provenance(monkeypatch):
    """A new schema fallback must preserve the document-wide transport boundary."""
    spy = fixture.VLSpy({"figure_table": fixture.table_json(["Name"], []),
                         "figure_prose": RuntimeError("connection lost")})
    fixture.install_vl(monkeypatch, spy)
    fixture.pass_probe(monkeypatch)
    with pytest.raises(figure_extract.FigureExtractionError) as error:
        fixture.extract([fixture.candidate()], {4: fixture.page_evidence()})
    assert "整份 PDF 零寫入" in str(error.value)
    failed = error.value.failed
    assert failed.payload is None and failed.kind == "prose"
    assert failed.evidence["lane"] == "vl"
    assert failed.evidence["extraction_attempts"][-1]["failure"] == "transport"
    assert failed.evidence["transcription"]["attempted_kind"] == "table"
    assert failed.evidence["transcription"]["fallback_kind"] == "prose"
    assert failed.evidence["sent_variants"] == ["crop@200dpi"]


@pytest.mark.smoke
def test_transcription_rejects_a_different_source_at_the_declared_pos(monkeypatch):
    """A pos from another PageEvidence cannot become independent source proof."""
    spy = fixture.VLSpy({"figure_terminal": _lines("visible")})
    fixture.install_vl(monkeypatch, spy)
    fixture.pass_probe(monkeypatch)
    candidate = fixture.candidate(kind="terminal", native_lane=False, signals={
        "native_text": {"pos": [0, 7], "markdown": "foreign"},
    })
    with pytest.raises(figure_extract.FigureExtractionError, match="不一致") as error:
        fixture.extract([candidate], {4: fixture.page_evidence(raw_markdown="visible")})
    assert error.value.failed.payload is None
    assert error.value.failed.evidence["extraction_attempts"][-1]["failure"] == "producer_contract"


@pytest.mark.smoke
@pytest.mark.parametrize("role, raw", [
    ("code", "if (enabled || ready) {\n\t  write_value();  \n}\n"),
    ("file_tree", "root/\n  |-- child/\n  |   `-- leaf.c\n  `-- init.c\n"),
])
def test_native_structure_transcription_preserves_bytes_without_vl(monkeypatch, role, raw):
    """Role annotations must not alter raw indentation, blank lines, or native's zero VL."""
    def forbidden(**_kwargs):
        pytest.fail("A native code/tree source must not call the VL model or probe")
    monkeypatch.setattr(figure_verify, "ensure_capability", forbidden)
    monkeypatch.setattr(figure_verify.llama_client, "vision_json_completion", forbidden)
    candidate = fixture.candidate(kind="terminal", native_lane=True, signals={
        "content_role": role, "native_text": {"pos": [0, len(raw)], "markdown": raw},
    })
    result = fixture.extract([candidate], {4: fixture.page_evidence(raw_markdown=raw)})[0]
    assert "\n".join(line["text"] for line in result.payload["lines"]).encode() == raw.encode()
    assert result.evidence["content_role"] == role
    assert result.evidence["lane"] == "native"
    assert result.variants == [] and result.model_input_variant == "native"
    assert result.verification_status == "unverified", "Raw source cannot corroborate itself"


@pytest.mark.smoke
@pytest.mark.parametrize("source", [None, "visible\ufffd"])
def test_incomplete_native_channels_cannot_supply_a_transcription_denominator(monkeypatch, source):
    """Words cannot prove whitespace/blank lines; broken source glyphs cannot prove text."""
    spy = fixture.VLSpy({"figure_terminal": _lines("visible")})
    fixture.install_vl(monkeypatch, spy)
    fixture.pass_probe(monkeypatch)
    candidate = fixture.candidate(kind="terminal")
    evidence = fixture.page_evidence(
        raw_markdown=source or "",
        page_boxes=[{"bbox": candidate.bbox, "pos": [0, len(source)]}] if source else [],
        words=fixture.words_from([(10, [(10, 70, "visible")])]),
    )
    result = fixture.extract([candidate], {4: evidence})[0]
    assert result.evidence["transcription"]["coverage"] == "unknown"
    assert result.evidence["transcription"]["anchor"] is None


@pytest.mark.smoke
def test_known_source_structure_cannot_be_blessed_as_a_raster_table(monkeypatch):
    """A definite source role survives model schema drift; no heuristic on ordinary cells."""
    spy = fixture.VLSpy({"figure_raster_kind_v1": fixture.raster_kind("table"),
                         "figure_table": fixture.REGISTER_TABLE})
    fixture.install_vl(monkeypatch, spy)
    fixture.pass_probe(monkeypatch)
    candidate = fixture.candidate(kind="raster", signals={"content_role": "file_tree"})
    result = fixture.extract([candidate], {4: fixture.page_evidence()})[0]
    assert result.kind == "table" and result.payload
    assert result.evidence["content_role"] == "file_tree"
    assert "raster_structure_mismatch" in result.reasons
    assert result.verification_status == "needs_review"


@pytest.mark.smoke
def test_duplicate_fallback_retains_attempts_and_rechecks_the_source(monkeypatch):
    """Caching the image must preserve the fallback while recomputing occurrence coverage."""
    spy = fixture.VLSpy({"figure_table": fixture.table_json(["Name"], []),
                         "figure_prose": _lines("visible")})
    fixture.install_vl(monkeypatch, spy)
    fixture.pass_probe(monkeypatch)
    first = fixture.candidate(page=4, seed="fallback-first")
    duplicate = fixture.candidate(page=5, seed="fallback-second")
    results = fixture.extract([first, duplicate], {
        4: fixture.page_evidence(),
        5: fixture.page_evidence(page=5, raw_markdown="visible\nsource tail", page_boxes=[
            {"bbox": duplicate.bbox, "pos": [0, len("visible\nsource tail")]}]),
    })
    assert results[0].evidence["transcription"]["coverage"] == "unknown"
    copied = results[1]
    assert copied.evidence["transcription"]["coverage"] == 0.5
    assert copied.evidence["transcription"]["attempted_kind"] == "table"
    assert copied.evidence["transcription"]["fallback_kind"] == "prose"
    assert copied.evidence["extraction_attempts"] == results[0].evidence["extraction_attempts"]
    assert "transcription_source_incomplete" in copied.reasons
    assert "transcription_fallback" in copied.reasons
    assert copied.variants == []
    assert copied.evidence["duplicate_model_input"]["variants"] == results[0].variants
    assert spy.schema_names().count("figure_prose") == 1
