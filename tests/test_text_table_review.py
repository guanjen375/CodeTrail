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


# ---- list_table_rows: the whole matching set and its count, or an explicit account of what is missing ----

def _grid(number, labels, rows, **overrides):
    """A trusted table entry shaped like `_table()` with an arbitrary grid."""
    entry = _table()
    columns = [{"column_id": f"c{index}", "label": label, "role": None} for index, label in enumerate(labels, 1)]
    entry["payload"] = {"kind": "table", "columns": columns, "footnotes": [], "rows": [
        {"row_index": row, "cells": [
            {"column_id": column["column_id"], "text": text, "state": "observed", "inherited_from_row": None}
            for column, text in zip(columns, texts)]}
        for row, texts in enumerate(rows, 1)]}
    entry["figure_id"] = f"fig_{number:016x}"
    entry.update(overrides)
    return entry


def _serve(tmp_path, monkeypatch, entries):
    """Index the in-KB entries and hand all of them out the way list_figures scopes a document."""
    _kb(tmp_path, [{"source": entry["source"], "document_id": entry["document_id"], "structured": True,
                    "figure_id": entry["figure_id"]} for entry in entries if entry.get("in_kb") is True])
    monkeypatch.setattr(table_lookup.figure_review, "list_figures", lambda root, chunks, *, document_id=None: [
        entry for entry in entries if document_id is None or entry["document_id"] in (document_id, "")])
    return lambda **arguments: table_lookup.list_table_rows(tmp_path, **arguments)


def test_row_listing_reports_every_match_with_exact_counts(tmp_path, monkeypatch):
    import model_identity
    import RAG
    labels = ["Register", "Address", "Access", "Reset"]
    first = _grid(1, labels, [("CTRL", "0x4000", "RW", "0x0001"), ("STATUS", "0x4004", "RO", "0x0000"),
                              ("BAUD", "0x4008", "RW", "0x0683"), ("TX", "0x400C", "WO", "0x0000_0000"),
                              ("RX", "0x4010", "RO", "0")], page=2)
    second = _grid(2, labels, [("CFG", "0x5000", "RW", "0x0044"), ("ID", "0x5004", "RO", "0x0000")], page=5)
    listing = _serve(tmp_path, monkeypatch, [first, second])
    monkeypatch.setattr(RAG, "load_knowledge_base", lambda *a, **k: pytest.fail("row listing loaded vector KB"))
    monkeypatch.setattr(model_identity, "capture_model_identity", lambda *a, **k: pytest.fail("row listing probed model"))
    where = [{"column": "Reset", "op": "!=", "value": 0}]

    result = listing(document_id="spec.pdf", where=where)
    assert (result["status"], result["reason"], result["complete"], result["has_ref"]) == (
        "ok", "complete_scan", True, True), result
    assert result["counts"] == {"tables_scanned": 2, "rows_scanned": 7, "matched": 3, "undetermined": 0,
                                "skipped_tables": 0, "excluded_tables": 0, "returned": 3}
    # 0x0000_0000 and 0 are zero too; the integer condition is echoed with the mode it was read in.
    assert result["where"] == [{"column": "Reset", "op": "!=", "value": "0", "comparison": "numeric"}]
    assert [(row["n"], row["row_index"], row["cells"]["1 Register"], row["cells"]["4 Reset"])
            for row in result["rows"]] == [(1, 1, "CTRL", "0x0001"), (2, 3, "BAUD", "0x0683"), (3, 1, "CFG", "0x0044")]
    located = [result["tables"][row["t"] - 1] for row in result["rows"]]
    provenance = ("document_id", "source", "figure_id", "page", "revision", "evidence_ref", "bbox", "verification_status")
    assert [{key: item[key] for key in provenance} for item in located] == [
        {key: entry[key] for key in provenance} for entry in (first, first, second)]
    assert result["scanned"] == [
        {"document_id": first["document_id"], "figure_id": first["figure_id"], "page": 2,
         "rows": 5, "matched": 2, "undetermined": 0},
        {"document_id": second["document_id"], "figure_id": second["figure_id"], "page": 5,
         "rows": 2, "matched": 1, "undetermined": 0}]
    assert (result["next_offset"], result["truncated"], result["value_samples"]) == (None, False, [])
    assert result["scope"]["resolved_document_ids"] == [first["document_id"]]

    # A count needs no rows: the totals cover the whole scope on their own.
    counted = listing(document_id="spec.pdf", where=where, limit=0)
    assert counted["counts"] == {**result["counts"], "returned": 0}
    assert (counted["rows"], counted["tables"], counted["next_offset"], counted["status"]) == ([], [], None, "ok")

    everything = listing()
    assert everything["counts"]["matched"] == everything["counts"]["rows_scanned"] == 7
    assert everything["complete"] is True and everything["scope"]["all_eligible_tables"] is True
    assert [row["cells"]["1 Register"] for row in everything["rows"]] == ["CTRL", "STATUS", "BAUD", "TX", "RX", "CFG", "ID"]
    assert not (tmp_path / ".codetrail").exists()
    assert not (tmp_path / ".knowledge.json.lock").exists()


def test_row_listing_never_claims_completeness_past_unverified_unreadable_or_skipped_tables(tmp_path, monkeypatch):
    labels = ["Register", "Address", "Access", "Reset"]
    where = [{"column": "Reset", "op": "!=", "value": 0}]
    good = _grid(1, labels, [("CTRL", "0x4000", "RW", "0x0001"), ("STATUS", "0x4004", "RO", "0x0000")], page=2)
    pending = _grid(2, labels, [("HIDDEN", "0x6000", "RW", "0xBAD0")], page=3,
                    verification_status="needs_review", human_verification=None)
    # What list_figures returns when a review artifact cannot be scanned at all.
    scan_error = {**_table(), "document_id": "", "figure_id": "", "kind": "", "source": "", "in_kb": False,
                  "payload": None, "evidence_ref": "", "warnings": ["artifact_unreadable"],
                  "payload_error": "無法掃描 review artifacts: " + "x" * 400}
    renamed = _grid(3, ["Register", "Address", "Access", "Reset Value"], [("ALT", "0x7000", "RW", "0x0002")], page=4)
    torn = {**_grid(4, labels, [("TORN", "0x9000", "RW", "0x0003")], page=7), "kind": "weird", "payload": None,
            "payload_error": "KB 內這個 figure 的 chunk 不一致"}
    retained = _grid(5, labels, [("OLD", "0x8000", "RW", "0x0009")], page=1, in_kb=False,
                     document_id="old.pdf::ffffffffffffffff", source="old.pdf")
    log = {**_grid(6, ["line"], [("boot ok",)], page=6), "kind": "terminal"}

    assert _serve(tmp_path, monkeypatch, [good])(where=where)["complete"] is True
    # A retained artifact of a removed document and a non-table figure are not part of the scope.
    assert _serve(tmp_path, monkeypatch, [good, retained, log])(where=where)["complete"] is True
    # Each of these, alone, must turn the same scan into an explicit lower bound.
    for defect, counter, reason in ((pending, "excluded_tables", "table_not_verified"),
                                    (scan_error, "excluded_tables", "artifact_unreadable"),
                                    (torn, "excluded_tables", "artifact_unreadable"),
                                    (renamed, "skipped_tables", "filter_column_not_found")):
        result = _serve(tmp_path, monkeypatch, [good, defect])(where=where)
        assert (result["status"], result["reason"], result["complete"], result["has_ref"]) == (
            "partial", "incomplete_scan", False, True), reason
        assert result["counts"][counter] == 1 and result["counts"]["matched"] == 1
        assert [item["reason"] for item in result["excluded"] + result["skipped"]] == [reason]
        assert [row["cells"]["1 Register"] for row in result["rows"]] == ["CTRL"]

    everything = [scan_error, good, pending, renamed, torn, retained, log]
    for scope in ({}, {"document_id": "spec.pdf"}):
        result = _serve(tmp_path, monkeypatch, everything)(where=where, **scope)
        assert (result["status"], result["complete"]) == ("partial", False), scope
        assert result["counts"] == {"tables_scanned": 1, "rows_scanned": 2, "matched": 1, "undetermined": 0,
                                    "skipped_tables": 1, "excluded_tables": 3, "returned": 1}
        assert sorted(item["reason"] for item in result["excluded"]) == [
            "artifact_unreadable", "artifact_unreadable", "table_not_verified"]
        assert result["skipped"] == [{"document_id": renamed["document_id"], "figure_id": renamed["figure_id"],
                                      "page": 4, "reason": "filter_column_not_found", "missing": ["Reset"],
                                      "columns": ["Register", "Address", "Access", "Reset Value"]}]
        unscanned = next(item for item in result["excluded"] if not item["figure_id"])
        assert unscanned["detail"].startswith("無法掃描 review artifacts") and len(unscanned["detail"]) <= 200
        dumped = json.dumps(result, ensure_ascii=False)
        assert "0xBAD0" not in dumped and "HIDDEN" not in dumped        # an unverified value never surfaces
        assert retained["figure_id"] not in dumped and "0x0009" not in dumped and log["figure_id"] not in dumped

    listing = _serve(tmp_path, monkeypatch, everything)
    # A scoped figure that was found is not affected by a failed scan of other artifacts...
    scoped = listing(figure_id=good["figure_id"], where=where)
    assert (scoped["status"], scoped["complete"], scoped["counts"]["excluded_tables"], scoped["excluded"]) == (
        "ok", True, 0, [])
    assert scoped["scope"]["resolved_document_ids"] == [good["document_id"]]
    # ...but a figure that cannot be found keeps that failure visible.
    absent = listing(figure_id="fig_" + "9" * 16, where=where)
    assert (absent["status"], absent["reason"], absent["has_ref"], absent["rows"]) == (
        "not_found", "figure_scope_not_found", False, [])
    assert [item["reason"] for item in absent["excluded"]] == ["artifact_unreadable", "artifact_unreadable"]
    assert absent["counts"]["excluded_tables"] == 2
    for figure, status, reason in ((pending, "unverified", "no_verified_table_in_scope"),
                                   (torn, "unverified", "no_verified_table_in_scope"),
                                   (log, "not_found", "figure_is_not_a_table"),
                                   (renamed, "not_found", "no_verified_table_with_filter_columns")):
        result = listing(figure_id=figure["figure_id"], where=where)
        assert (result["status"], result["reason"], result["complete"], result["has_ref"], result["rows"]) == (
            status, reason, False, False, []), reason
    assert "0xBAD0" not in json.dumps(listing(figure_id=pending["figure_id"]))


def test_row_listing_projection_never_changes_matches_counts_or_paging(tmp_path, monkeypatch):
    wide = _grid(1, ["Register", "Reset", "Description"],
                 [(f"R{index}", f"0x{index % 3:04x}", f"note {index}") for index in range(1, 8)], page=2)
    narrow = _grid(2, ["Register", "Reset"], [(f"S{index}", f"0x{index % 2:04x}") for index in range(1, 5)], page=3)
    listing = _serve(tmp_path, monkeypatch, [wide, narrow])
    where = [{"column": "Reset", "op": "!=", "value": "0x0"}]

    def identity(result):
        return [(row["n"], result["tables"][row["t"] - 1]["figure_id"], row["row_index"]) for row in result["rows"]]

    expected = [(1, wide["figure_id"], 1), (2, wide["figure_id"], 2), (3, wide["figure_id"], 4),
                (4, wide["figure_id"], 5), (5, wide["figure_id"], 7), (6, narrow["figure_id"], 1),
                (7, narrow["figure_id"], 3)]
    full = listing(where=where)
    assert identity(full) == expected and full["counts"]["matched"] == 7 and full["complete"] is True

    # A table without a requested *output* column still contributes every matching row.
    projected = listing(where=where, columns=["Reset", "Description"])
    unknown = listing(where=where, columns=["Nope"])
    for result in (projected, unknown):
        assert identity(result) == expected
        assert result["counts"] == full["counts"] and result["scope_digest"] == full["scope_digest"]
        assert (result["status"], result["complete"]) == ("ok", True)
    assert [table["missing_output"] for table in projected["tables"]] == [[], ["Description"]]
    assert projected["rows"][0]["cells"] == {"2 Reset": "0x0001", "3 Description": "note 1"}
    assert projected["rows"][-1]["cells"] == {"2 Reset": "0x0001"}
    assert [table["missing_output"] for table in unknown["tables"]] == [["Nope"], ["Nope"]]
    assert all(row["cells"] == {} for row in unknown["rows"])

    pages, offset = [], 0
    while offset is not None:
        pages.append(listing(where=where, columns=["Register"], limit=3, offset=offset))
        offset = pages[-1]["next_offset"]
    assert [identity(page) for page in pages] == [expected[:3], expected[3:6], expected[6:]]
    assert [(page["offset"], page["next_offset"], page["truncated"], page["counts"]["returned"]) for page in pages] == [
        (0, 3, True, 3), (3, 6, True, 3), (6, None, False, 1)]
    # Totals describe the scope, not the page, and every page names the same scan.
    assert all(page["counts"]["matched"] == 7 and page["scope_digest"] == full["scope_digest"] for page in pages)
    assert [row["cells"]["1 Register"] for page in pages for row in page["rows"]] == [
        "R1", "R2", "R4", "R5", "R7", "S1", "S3"]
    beyond = listing(where=where, offset=7)
    assert (beyond["rows"], beyond["tables"], beyond["next_offset"], beyond["counts"]["matched"]) == ([], [], None, 7)
    # Another condition is another scan; another spelling of the same number is not.
    assert listing(where=[{"column": "Reset", "op": "=", "value": 0}])["scope_digest"] != full["scope_digest"]
    assert listing(where=[{"column": "Reset", "op": "!=", "value": 0}])["scope_digest"] == full["scope_digest"]


def test_row_listing_filter_semantics_are_closed_and_explicit(tmp_path, monkeypatch):
    table = _grid(1, ["Name", "Value", "Mode"], [
        ("A", "0x0000_0010", "Read  Write"), ("B", "16", "read write"), ("C", "0x11", "RO"),
        ("D", "n/a", "RO"), ("E", "", "RW"), ("F", "0x11", "RO")], page=2)
    table["payload"]["rows"][5]["cells"][1].update(state="inherited", inherited_from_row=3)
    listing = _serve(tmp_path, monkeypatch, [table])

    def names(*conditions):
        result = listing(where=[{"column": column, "op": op, "value": value} for column, op, value in conditions])
        return [row["cells"]["1 Name"] for row in result["rows"]], result

    matched, result = names(("Value", "=", 16))
    assert matched == ["A", "B"]                                    # 0x0000_0010 and 16 are one number
    assert result["where"] == [{"column": "Value", "op": "=", "value": "16", "comparison": "numeric"}]
    assert (result["status"], result["complete"], result["counts"]["matched"], result["counts"]["undetermined"]) == (
        "partial", False, 2, 3)
    # A cell that cannot be compared is neither a match nor a silent non-match.
    assert [(item["row_index"], item["column"], item["reason"], item.get("inherited_from_row"))
            for item in result["undetermined"]] == [
        (4, "Value", "cell_not_numeric", None), (5, "Value", "selector_cell_empty", None),
        (6, "Value", "selector_cell_inherited", 3)]
    assert names(("Value", "!=", 16))[0] == ["C"]
    assert names(("Value", ">", "0x10"))[0] == ["C"]
    assert names(("Value", ">=", "16"))[0] == ["A", "B", "C"]
    assert names(("Value", "<", "0x11"))[0] == ["A", "B"]
    assert names(("Value", "<=", "0x10"))[0] == ["A", "B"]

    matched, result = names(("Mode", "=", "READ WRITE"))            # whitespace and case are folded
    assert matched == ["A", "B"] and result["where"][0]["comparison"] == "text"
    assert (result["status"], result["complete"]) == ("ok", True)
    assert names(("Mode", "!=", "ro"))[0] == ["A", "B", "E"]
    assert names(("Mode", "contains", "ead w"))[0] == ["A", "B"]
    assert names(("3", "=", "rw"))[0] == ["E"]                      # a one-based column index is a selector
    # `contains` stays textual even when the value looks like a number.
    matched, result = names(("Value", "contains", 16))
    assert matched == ["B"] and result["where"][0]["comparison"] == "text"

    # AND: row E is a decided non-match (Mode is RW) although its Value is empty.
    matched, result = names(("Value", ">=", 16), ("Mode", "=", "ro"))
    assert matched == ["C"]
    assert [(item["row_index"], item["reason"]) for item in result["undetermined"]] == [
        (4, "cell_not_numeric"), (6, "selector_cell_inherited")]

    # Zero matches come with what the column actually holds, so a misspelt value is visible.
    matched, result = names(("Mode", "=", "wo"))
    assert matched == [] and (result["status"], result["has_ref"]) == ("ok", True)
    assert result["value_samples"] == [{"column": "Mode", "distinct": 4, "values": [
        ["RO", 3], ["Read Write", 1], ["read write", 1], ["RW", 1]]}]

    # A table holding an unreadable cell is not trusted at all today...
    damaged = copy.deepcopy(table)
    damaged["payload"]["rows"][0]["cells"][1]["state"] = "unreadable"
    result = _serve(tmp_path, monkeypatch, [damaged])(where=[{"column": "Value", "op": "=", "value": 16}])
    assert (result["status"], result["counts"]["matched"], [item["reason"] for item in result["excluded"]]) == (
        "unverified", 0, ["table_quality_not_accepted"])
    # ...and were it ever trusted, that cell would still decide nothing.
    for state in ("unreadable", "conflict"):
        damaged["payload"]["rows"][0]["cells"][1]["state"] = state
        with monkeypatch.context() as patch:
            patch.setattr(table_lookup, "_trust_reason", lambda entry: "")
            result = table_lookup.list_table_rows(tmp_path, where=[{"column": "Value", "op": "=", "value": 16}])
        assert [row["cells"]["1 Name"] for row in result["rows"]] == ["B"], state
        assert (result["undetermined"][0]["row_index"], result["undetermined"][0]["reason"]) == (
            1, "selector_cell_" + state)

    # Anything outside the closed grammar is refused before the store or any artifact is read.
    monkeypatch.setattr(table_lookup.evidence_store, "snapshot",
                        lambda root: pytest.fail("an invalid request reached the store"))
    monkeypatch.setattr(table_lookup.figure_review, "list_figures",
                        lambda *a, **k: pytest.fail("an invalid request scanned figures"))
    condition = {"column": "Value", "op": "=", "value": "16"}
    for arguments in (
            {"where": condition}, {"where": [condition] * 5}, {"where": [{"column": "Value", "op": "="}]},
            {"where": [{**condition, "extra": 1}]}, {"where": [{**condition, "op": "~"}]},
            {"where": [{**condition, "op": "<", "value": "ten"}]}, {"where": [{**condition, "value": True}]},
            {"where": [{**condition, "value": 1.5}]}, {"where": [{**condition, "value": "  "}]},
            {"where": [{**condition, "column": " "}]}, {"where": [{**condition, "column": "c" * 201}]},
            {"where": [{**condition, "value": "v" * 201}]}, {"columns": ["Name", "Name"]},
            {"columns": [str(index) for index in range(1, 18)]}, {"columns": "Name"},
            {"limit": 201}, {"limit": -1}, {"limit": True}, {"offset": -1}, {"offset": "0"},
            {"figure_id": "fig_nothex"}, {"document_id": "d" * 513}):
        result = table_lookup.list_table_rows(tmp_path, **arguments)
        assert (result["status"], result["has_ref"], result["complete"], result["rows"]) == (
            "error", False, False, []), arguments
        assert result["reason"].startswith("ValueError: "), arguments


def test_row_listing_reports_what_a_substring_condition_matched(tmp_path, monkeypatch):
    """`contains` also matches longer values: "core1" is inside "core10".

    Seen live: asked how many interrupts go to core1, the model filtered with `contains`
    instead of `=`. On a table that also has core10 that silently counts both. The tool
    cannot know which was meant, so it lists what the substring actually matched and the
    adapter says to rerun with `=`; an exact condition, or a substring that hit one value
    only, carries no such report.
    """
    from tool_result_adapter import ResultBudget, adapt_tool_result

    table = _grid(1, ["IRQ", "Core"], [("0", "core0"), ("1", "core1"), ("2", "core10"), ("3", "Core1"),
                                        ("4", "core10"), ("5", "core 1")], page=2)
    listing = _serve(tmp_path, monkeypatch, [table])
    budget = ResultBudget(4000, 16000, False, False)

    loose = listing(where=[{"column": "Core", "op": "contains", "value": "core1"}])
    assert (loose["status"], loose["complete"], loose["counts"]["matched"]) == ("ok", True, 4)
    assert loose["contains_matches"] == [
        {"column": "Core", "value": "core1", "distinct": 3, "values": [["core10", 2], ["core1", 1], ["Core1", 1]]}]
    text = adapt_tool_result("list_table_rows", loose, budget=budget).content[0].text
    assert text.startswith("status: partial\nnext: A contains condition matched more than one distinct value")
    assert "rerun with = and report that count instead" in text.split("\n")[1]
    assert 'contains_matches: {"column":"Core","distinct":3,' in text

    exact = listing(where=[{"column": "Core", "op": "=", "value": "core1"}])
    assert [row["cells"]["1 IRQ"] for row in exact["rows"]] == ["1", "3"]      # casefold: core1 and Core1
    assert exact["contains_matches"] == [] and exact["counts"]["matched"] == 2
    text = adapt_tool_result("list_table_rows", exact, budget=budget).content[0].text
    assert text.startswith("status: ok\n") and "contains_matches:" not in text

    # One distinct value: the substring was not ambiguous, so nothing is reported.
    single = listing(where=[{"column": "Core", "op": "contains", "value": "core0"}])
    assert single["counts"]["matched"] == 1 and single["contains_matches"] == []
    assert adapt_tool_result("list_table_rows", single, budget=budget).content[0].text.startswith("status: ok\n")


def test_row_listing_rows_carry_unique_cell_keys_and_a_source_locator(tmp_path, monkeypatch):
    odd = _grid(1, ["A", "A", "#1 A", ""], [("x", "y", "z", "w"), ("p", "q", "FILLED-DOWN", "s")], page=2)
    odd["payload"]["rows"][1]["cells"][2].update(state="inherited", inherited_from_row=1)
    listing = _serve(tmp_path, monkeypatch, [odd])
    result = listing()
    # The one-based column index keeps every key distinct, whatever the labels are.
    assert result["rows"][0]["cells"] == {"1 A": "x", "2 A": "y", "3 #1 A": "z", "4 ": "w"}
    assert result["rows"][0]["unobserved"] == {}
    # A cell that was not directly observed is named, never given a value.
    assert result["rows"][1]["cells"] == {"1 A": "p", "2 A": "q", "4 ": "s"}
    assert result["rows"][1]["unobserved"] == {"3 #1 A": {"state": "inherited", "inherited_from_row": 1}}
    assert "FILLED-DOWN" not in json.dumps(result)
    assert listing(columns=["A"])["rows"][0]["cells"] == {"1 A": "x", "2 A": "y"}
    assert listing(columns=["3"])["rows"][0]["cells"] == {"3 #1 A": "z"}
    # A duplicated label cannot *select* rows: that would be a guess between two columns.
    guess = listing(where=[{"column": "A", "op": "=", "value": "x"}])
    assert (guess["status"], guess["counts"]["matched"], guess["excluded"][0]["reason"]) == (
        "unverified", 0, "ambiguous_selector_column")
    assert listing(where=[{"column": "2", "op": "=", "value": "y"}])["counts"]["matched"] == 1

    # More tables than any diagnostic cap: every returned row still resolves to its own source.
    many = [_grid(100 + index, ["Register", "Reset"], [(f"M{index}", "0x0001")], page=index + 1) for index in range(60)]
    listing = _serve(tmp_path, monkeypatch, many)
    where = [{"column": "Reset", "op": "!=", "value": 0}]
    result = listing(where=where, limit=200)
    assert result["counts"]["tables_scanned"] == 60 and len(result["scanned"]) == 50
    assert len(result["rows"]) == len(result["tables"]) == 60
    for index, row in enumerate(result["rows"]):
        table = result["tables"][row["t"] - 1]
        assert (table["t"], table["figure_id"], table["page"], table["document_id"], table["source"],
                table["revision"], table["evidence_ref"]) == (
            row["t"], many[index]["figure_id"], index + 1, many[index]["document_id"], "spec.pdf",
            2, many[index]["evidence_ref"])
        assert row["cells"]["1 Register"] == f"M{index}"
    tail = listing(where=where, limit=5, offset=55)
    assert [row["n"] for row in tail["rows"]] == [56, 57, 58, 59, 60]
    assert [(table["t"], table["figure_id"]) for table in tail["tables"]] == [
        (position, entry["figure_id"]) for position, entry in enumerate(many[55:], 1)]
    untrusted = [dict(entry, verification_status="unverified", human_verification=None) for entry in many]
    result = _serve(tmp_path, monkeypatch, untrusted)(where=where)
    assert (result["status"], result["counts"]["excluded_tables"], len(result["excluded"])) == ("unverified", 60, 50)


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
