"""Deterministic figure quality, separate from verification trust and human review.

Only current canonical payload and named producer evidence are assessed. Historical
evidence (for example ``previous_revision_evidence``) is audit material, never proof
about the current revision. This module uses only the standard library and does no I/O.
"""
from __future__ import annotations

import math
import re

QUALITY_GRADES = frozenset({
    "usable", "formatting_only", "partial", "structure_error", "unusable", "unknown",
})
REVIEW_STATES = frozenset({"unreviewed", "confirmed"})
AUTO_DISPOSITIONS = frozenset({"accept", "manual_review", "repair_required", "excluded"})
QUALITY_ISSUES = frozenset({
    "extraction_failed", "no_content", "unreadable_text", "excluded_noncontent",
    "structure_mismatch", "conflicting_text", "missing_items", "formatting_difference",
    "unverified_coverage",
})
QUALITY_DEFAULTS = {
    "quality_grade": "unknown", "review_state": "unreviewed",
    "auto_disposition": "manual_review", "quality_issues": [],
}
QUALITY_FIELDS = tuple(QUALITY_DEFAULTS)

# Exact producer slugs: uncertain alignment/headers are not proven structural damage.
# ``*_failed`` are the current verifier spellings of the plan's ``*_mismatch`` aliases.
STRUCTURAL_REASONS = frozenset({
    "span_ambiguous", "stitch_tile_gap", "sample_tile_subset_mismatch",
    "row_alignment_mismatch", "row_alignment_failed", "line_alignment_failed",
    "column_assignment_mismatch", "grid_column_mismatch", "grid_cell_occupancy_mismatch",
    "raster_structure_mismatch",
})
_UNREADABLE_REASONS = frozenset({"model_unreadable", "unreadable_content"})
_CONFLICT_REASONS = frozenset({
    "glyph_conflict", "sample_conflict", "sample_state_conflict", "sample_span_conflict",
    "cell_conflict", "line_conflict", "critical_token_mismatch",
    "stitch_footnote_conflict", "stitch_title_conflict", "sample_footnote_conflict",
})
_MISSING_REASONS = frozenset({
    "missing_rows", "missing_lines", "transcription_source_incomplete",
})
_SEMANTIC_REASONS = frozenset({
    "header_missing", "header_conflict", "sample_header_conflict",
    "header_promoted_from_first_row",
})
_FORMATTING_ARTIFACTS = frozenset({
    "extract_underscore_artifact", "extract_whitespace_artifact",
})
_WHITESPACE = re.compile(r"\s+")
_GLYPH = "▯"


def _result(grade, disposition, issues=(), *, confirmed=False):
    return {"quality_grade": grade,
            "review_state": "confirmed" if confirmed else "unreviewed",
            "auto_disposition": disposition, "quality_issues": list(issues)}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _items(value):
    return value if isinstance(value, (list, tuple)) else ()


def _positive_int(value):
    return type(value) is int and value > 0


def _legal_human_record(record, verification_status):
    record = _mapping(record)
    return (verification_status == "human_verified"
            and record.get("confirmed_against_image") is True
            and _positive_int(record.get("revision"))
            and type(record.get("carried_over", False)) is bool)


def _payload_content(payload, kind):
    """Return body units and secondary strings; table labels cannot rescue empty data."""
    units, secondary = [], []
    if kind == "table":
        for row in _items(payload.get("rows")):
            for cell in _items(_mapping(row).get("cells")):
                cell = _mapping(cell)
                units.append((cell.get("text", ""), cell.get("state"), ()))
        secondary.extend(_mapping(column).get("label", "")
                         for column in _items(payload.get("columns")))
        secondary.extend(_items(payload.get("footnotes")))
    elif kind in ("terminal", "prose"):
        for line in _items(payload.get("lines")):
            line = _mapping(line)
            units.append((line.get("text", ""), None, _items(line.get("uncertain_spans"))))
    elif kind == "diagram":
        secondary = [payload.get("title", ""), *_items(payload.get("labels"))]
        for field, names in (("components", ("name", "desc")),
                             ("relations", ("src", "dst", "desc")),
                             ("values", ("key", "value", "desc"))):
            for item in _items(payload.get(field)):
                secondary.extend(_mapping(item).get(name, "") for name in names)
        units = [(text, None, ()) for text in secondary]
        secondary = []
    return units, [text for text in secondary if isinstance(text, str)]


def _shape_issue(payload, kind):
    if kind != "table":
        return False
    columns = [_mapping(column).get("column_id")
               for column in _items(payload.get("columns"))]
    if len(set(str(column) for column in columns)) != len(columns):
        return True
    return any([_mapping(cell).get("column_id")
                for cell in _items(_mapping(row).get("cells"))] != columns
               for row in _items(payload.get("rows")))


def _missing_indices(payload, kind):
    field, key = (("rows", "row_index") if kind == "table" else ("lines", "line_index"))
    if kind not in ("table", "terminal", "prose"):
        return False
    indices = [_mapping(item).get(key) for item in _items(payload.get(field))]
    return indices != list(range(1, len(indices) + 1))


def _source_coverage(evidence):
    """A numeric transcription score only counts with a source-denominator record."""
    if "transcription" not in evidence:
        return None
    transcript = _mapping(evidence.get("transcription"))
    coverage = transcript.get("coverage")
    anchor = _mapping(transcript.get("anchor"))
    total, matched = anchor.get("source_lines"), anchor.get("matched_source_lines")
    if (type(coverage) not in (int, float) or not math.isfinite(coverage)
            or not 0 <= coverage <= 1 or anchor.get("channel") != "markdown_pos"
            or not _positive_int(total) or type(matched) is not int
            or not 0 <= matched <= total or coverage != matched / total):
        return "unknown"
    return coverage


def _records(evidence, kind):
    field = "cells" if kind == "table" else "lines"
    return list(_mapping(evidence.get(field)).values())


def _independent_evidence(evidence, kind, verification_status):
    if verification_status not in ("native_verified", "corroborated"):
        return False
    channels = [channel for channel in _items(evidence.get("channels"))
                if isinstance(channel, str) and channel and channel != "vl_sample_2"]
    if not channels:
        return False
    if kind == "diagram":
        return True
    for raw_record in _records(evidence, kind):
        record = _mapping(raw_record)
        if record.get("matched") is not True:
            continue
        if record.get("anchor") in channels:
            return True
        if any(channel in channels and _mapping(proof).get("reliable") is True
               and _mapping(proof).get("verdict") == "match"
               for channel, proof in _mapping(record.get("by_channel")).items()):
            return True
    return False


def _formatting_difference(evidence, kind):
    for item in _records(evidence, kind):
        record = _mapping(item)
        if record.get("matched") is not True:
            continue
        for proof in _mapping(record.get("by_channel")).values():
            proof = _mapping(proof)
            raw, text = proof.get("raw"), proof.get("payload")
            if (proof.get("reliable") is True and proof.get("verdict") == "match"
                    and isinstance(raw, str) and isinstance(text, str) and raw != text
                    and _WHITESPACE.sub(" ", raw).strip() == _WHITESPACE.sub(" ", text).strip()):
                return True
        if any(_mapping(artifact).get("reason") in _FORMATTING_ARTIFACTS
               for artifact in _items(record.get("artifacts"))):
            return True
    return False


def _valid_metadata(metadata):
    if not isinstance(metadata, dict):
        return False
    for field, values in (("quality_grade", QUALITY_GRADES), ("review_state", REVIEW_STATES),
                          ("auto_disposition", AUTO_DISPOSITIONS)):
        if not isinstance(metadata.get(field), str) or metadata[field] not in values:
            return False
    issues = metadata.get("quality_issues")
    if not isinstance(issues, list) or any(not isinstance(issue, str) or issue not in QUALITY_ISSUES
                                          for issue in issues):
        return False
    disposition = metadata["auto_disposition"]
    grade = metadata["quality_grade"]
    coherent = {"usable": {"accept"}, "formatting_only": {"accept"},
                "partial": {"repair_required"}, "structure_error": {"excluded"},
                "unusable": {"excluded"}, "unknown": {"manual_review", "excluded"}}
    return (disposition in coherent[grade]
            and (metadata["review_state"] != "confirmed" or disposition == "accept"))


def _metadata_only_quality(evidence, reasons, verification_status, human_verification):
    """A missing artifact is not proof of an empty extraction.

    Notification fallback may use validated persisted KB metadata, without inventing
    a canonical payload from rendered chunks. Visible defects still take precedence;
    stored ``confirmed`` alone cannot manufacture a human confirmation record.
    """
    metadata = evidence.get("quality_metadata")
    valid = _valid_metadata(metadata)
    if reasons & STRUCTURAL_REASONS:
        return _result("structure_error", "excluded", ["structure_mismatch"])
    if valid and metadata["auto_disposition"] == "excluded":
        return _result(metadata["quality_grade"], "excluded", metadata["quality_issues"])
    text = evidence.get("fallback_text")
    unreadable = bool(reasons & _UNREADABLE_REASONS) or (isinstance(text, str) and _GLYPH in text)
    issues = ["unreadable_text"] if unreadable else []
    issues += ["conflicting_text"] if reasons & _CONFLICT_REASONS else []
    issues += ["missing_items"] if reasons & _MISSING_REASONS else []
    if issues:
        if valid and metadata["quality_grade"] == "partial":
            issues = list(dict.fromkeys([*metadata["quality_issues"], *issues]))
        return _result("partial", "repair_required", issues)
    confirmed = _legal_human_record(human_verification, verification_status)
    if valid:
        if metadata["auto_disposition"] == "accept" and not confirmed and (
                verification_status == "needs_review" or reasons & _SEMANTIC_REASONS):
            return _result("unknown", "manual_review", ["unverified_coverage"])
        return _result(metadata["quality_grade"], metadata["auto_disposition"],
                       metadata["quality_issues"],
                       confirmed=confirmed and metadata["auto_disposition"] == "accept")
    return _result("unknown", "manual_review", ["unverified_coverage"])


def assess_quality(payload, kind, *, reasons=(), evidence=None,
                   extraction_status="complete", verification_status="unverified",
                   human_verification=None):
    """Assess known content defects without promoting verification or inventing consent.

    Callers validate payload schemas and bind the human record to the current revision.
    The evaluator accepts no truthy substitutes for actual boolean confirmation and
    never infers human review from ``verification_status`` alone. Missing legacy metadata
    is handled by readers using ``QUALITY_DEFAULTS``, not by regrading old artifacts.
    """
    evidence = _mapping(evidence)
    reason_set = {reason for reason in _items(reasons) if isinstance(reason, str)}
    if extraction_status == "failed":
        return _result("unusable", "excluded", ["extraction_failed"])
    if (extraction_status == "skipped" or evidence.get("content_role") == "navigation"
            or "navigation_candidate_excluded" in reason_set):
        return _result("unknown", "excluded", ["excluded_noncontent"])
    if payload is None and evidence.get("metadata_only") is True:
        return _metadata_only_quality(evidence, reason_set, verification_status, human_verification)
    if not isinstance(payload, dict) or not payload:
        return _result("unusable", "excluded", ["no_content"])

    units, secondary = _payload_content(payload, kind)
    nonempty = [(text, state, spans) for text, state, spans in units
                if isinstance(text, str) and text.strip()]
    if not nonempty:
        return _result("unusable", "excluded", ["no_content"])
    unreadable = bool(reason_set & _UNREADABLE_REASONS)
    conflict = bool(reason_set & _CONFLICT_REASONS)
    readable = False
    for text, state, spans in nonempty:
        unreadable |= _GLYPH in text or state == "unreadable" or bool(spans)
        conflict |= state == "conflict"
        if state not in ("unreadable", "conflict"):
            # Uncertain spans mark text that is not independently readable even when a
            # producer omitted the glyph. Span validation stays with validate_payload.
            chars = list(text)
            for span in spans:
                span = _mapping(span)
                start, end = span.get("start"), span.get("end")
                if type(start) is int and type(end) is int and 0 <= start <= end <= len(chars):
                    chars[start:end] = [_GLYPH] * (end - start)
            readable |= bool("".join(chars).replace(_GLYPH, "").strip())
    unreadable |= any(_GLYPH in text for text in secondary)
    if not readable:
        return _result("unusable", "excluded", ["unreadable_text"])
    if reason_set & STRUCTURAL_REASONS or _shape_issue(payload, kind):
        return _result("structure_error", "excluded", ["structure_mismatch"])

    coverage = _source_coverage(evidence)
    missing = (bool(reason_set & _MISSING_REASONS) or _missing_indices(payload, kind)
               or (type(coverage) in (int, float) and coverage < 1))
    # Read only current atom findings, never recurse into candidate alternatives/history.
    for raw_record in _records(evidence, kind):
        record = _mapping(raw_record)
        if record.get("matched") is False:
            conflict |= record.get("verdict") in ("glyph", "structural", "unmappable")
    issues = (["unreadable_text"] if unreadable else [])
    issues += ["conflicting_text"] if conflict else []
    issues += ["missing_items"] if missing else []
    if issues:
        return _result("partial", "repair_required", issues)

    confirmed = _legal_human_record(human_verification, verification_status)
    if not confirmed and (reason_set & _SEMANTIC_REASONS or coverage == "unknown"
                          or verification_status == "needs_review"):
        return _result("unknown", "manual_review", ["unverified_coverage"])
    verified = _independent_evidence(evidence, kind, verification_status)
    if confirmed or verified:
        if _formatting_difference(evidence, kind):
            return _result("formatting_only", "accept", ["formatting_difference"],
                           confirmed=confirmed)
        return _result("usable", "accept", confirmed=confirmed)
    return _result("unknown", "manual_review", ["unverified_coverage"])
