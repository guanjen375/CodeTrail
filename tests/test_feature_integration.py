"""Model-visible evidence and public tool boundaries for the six-feature delivery."""
from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from tests._harness import import_mcp_module
from tool_result_adapter import ResultBudget, adapt_tool_result, estimate_result_tokens

pytestmark = pytest.mark.smoke


def test_text_review_json_error_is_not_reported_as_success():
    budget = ResultBudget(1000, 1000, True, False)
    result = adapt_tool_result("review_text", json.dumps({"status": "error", "action": "confirm",
                               "reason": "TextReviewError: stale revision"}), budget=budget)
    assert result.isError is True
    assert "status: error" in result.content[0].text and "stale revision" in result.content[0].text
    result = adapt_tool_result("review_text", json.dumps({"status": "ok", "action": "show",
                               "units": [{"text_id": "ocr_one", "text_revision": 3,
                                          "text_content_sha256": "a" * 64, "eligible": False,
                                          "original_ocr": "huge" * 1000}]}), budget=budget)
    visible = result.content[0].text
    assert "status: partial" in visible and "ocr_one" in visible
    assert '"text_revision":3' in visible and "original_ocr:" not in visible
    busy = adapt_tool_result("review_text", "稍後重試: ingest 進行中", budget=budget)
    assert "status: ok" not in busy.content[0].text


def test_exact_table_budget_never_exposes_a_shortened_cell_value():
    payload = {"status": "ok", "has_ref": True, "ambiguous": False,
               "matches": [{"value": "1234567890" * 1000, "source": "spec.pdf",
                            "page": 1, "figure_id": "f", "revision": 1}], "excluded": []}
    result = adapt_tool_result("query_table", payload,
                              budget=ResultBudget(1000, 1000, True, False))
    text = result.content[0].text
    assert "status: partial" in text and "cell:" not in text
    assert result.structuredContent == payload


def test_table_scope_and_recovery_reach_model_text_without_widening(tmp_path, monkeypatch):
    """A scoped miss formerly instructed the model to switch to another indexed source."""
    import table_lookup

    document = "qa/docs/spec.pdf::0123456789abcdef"
    figure = "fig_" + "a" * 16
    monkeypatch.setattr(table_lookup.evidence_store, "snapshot", lambda _root: {
        "metadata": {}, "chunks": [{"source": "spec.pdf", "document_id": document}]})
    monkeypatch.setattr(table_lookup.figure_review, "list_figures", lambda *a, **k: [])
    budget = ResultBudget(4000, 16000, False, False)
    for wanted_document, wanted_figure in (("spec.pdf", figure), ("", figure), ("spec.pdf", "")):
        payload = table_lookup.query_table(tmp_path, document_id=wanted_document,
                                           figure_id=wanted_figure, register="CTRL", column="Reset")
        result = adapt_tool_result("query_table", payload, budget=budget)
        text = result.content[0].text
        assert "Use another indexed source" not in text, text
        assert "Keep the requested document_id and figure_id scope" in text
        assert "scope: " in text and '"all_eligible_tables":false' in text
        assert payload["scope"]["requested_document_id"] == wanted_document
        assert payload["scope"]["requested_figure_id"] == wanted_figure
        assert result.structuredContent["scope"] == payload["scope"]
        if wanted_document:
            assert payload["scope"]["resolved_document_ids"] == [document]
            assert document in text

    # Even a successful all-table match must reveal the missing source filters.
    payload = table_lookup.query_table(tmp_path, register="CTRL", column="Reset")
    assert payload["scope"]["all_eligible_tables"] is True
    assert payload["scope"]["resolved_document_ids"] == []
    payload.update(status="ok", has_ref=True, matches=[{"document_id": document, "figure_id": figure,
                                                       "value": "0x0001"}])
    result = adapt_tool_result("query_table", payload, budget=budget)
    assert '"all_eligible_tables":true' in result.content[0].text
    assert '"requested_figure_id":""' in result.content[0].text
    assert result.structuredContent["scope"]["requested_document_id"] == ""
    assert result.content[0].text.index("scope:") < result.content[0].text.index("cell:")


def test_ocr_exclusion_and_build_unknown_reach_the_model_text_lane():
    budget = ResultBudget(4000, 16000, False, False)
    excluded = {"source": "spec.pdf", "page": 3, "text_id": "ocr_a", "reason": "unreviewed"}
    result = adapt_tool_result("query_knowledge_strict",
                              {"refused": True, "reason": "weak_ref", "excluded_text": [excluded]},
                              budget=budget)
    assert "ocr_a" in result.content[0].text and "review_text" in result.content[0].text
    result = adapt_tool_result("code_rag_search", [{"mode": "semantic", "results": [],
                              "build_context": {"target": None, "status": "unknown"}}], budget=budget)
    assert "unknown" in result.content[0].text and "results: 0" in result.content[0].text
    assert "hit:" not in result.content[0].text


@pytest.mark.parametrize("mode", ["semantic", "context", "neighbors", "path"])
def test_every_public_code_mode_uses_the_selected_build_context(monkeypatch, tmp_path, mode):
    module = import_mcp_module(monkeypatch, tmp_path)
    import build_context
    import code_context
    context = SimpleNamespace(restricts_files=True, assert_fresh=lambda: None,
                              summary=lambda: {"target": "board", "status": "unknown",
                                               "unknowns": ["compiler builtins unavailable"]})
    monkeypatch.setattr(build_context, "load_build_context", lambda root, target: context)
    item = {"path": "driver.c", "line": 1, "symbol": "entry", "type": "function",
            "build_state": "unknown"}
    ranked = SimpleNamespace(item=item, final_score=1.0)
    scoped_rag = SimpleNamespace(query_ranked=lambda *a, **k: [ranked],
                                 _scan_code_files=lambda: ["driver.c"], index=[item],
                                 trace_add_files=lambda *a: None)
    def scope_rag(ctx):
        assert ctx is context
        return scoped_rag
    # Deliberately no unscoped query API: falling back would silently mix targets.
    monkeypatch.setattr(module, "CODE_RAG", SimpleNamespace(for_build_context=scope_rag))
    node = {"id": "n", "name": "entry", "qualified_name": "entry", "path": "driver.c",
            "start_line": 1, "backend": "test", "linkage": "external", "condition": "",
            "kind": "function", "build_state": "unknown"}
    graph = SimpleNamespace(ensure_fresh=lambda: None, find_nodes=lambda _: [node],
                            neighbors=lambda *a, **k: {"nodes": [node], "edges": [], "truncated": False},
                            shortest_evidence_paths=lambda *a, **k: [])
    def scope_graph(ctx):
        assert ctx is context
        return graph
    monkeypatch.setattr(module, "_get_code_graph", lambda: SimpleNamespace(for_build_context=scope_graph))
    def bundle(**kwargs):
        assert kwargs["build_context"] is context
        assert kwargs["graph"] is graph
        return {"query": "entry", "evidence": [], "seeds": [], "uncertainties": [],
                "graph_status": "ok", "used_chars": 0, "budget_chars": 12000, "truncated": False}
    monkeypatch.setattr(code_context, "build_code_context", bundle)
    def lexical(*args, **kwargs):
        assert kwargs["build_context"] is context
        return []
    monkeypatch.setattr(code_context, "collect_safe_lexical_hits", lexical)
    monkeypatch.setattr(module.data_flywheel, "collect_enabled", lambda: False)
    result = module.code_rag_search("entry -> exit" if mode == "path" else "entry",
                                    mode=mode, build_target="board")
    assert result[0]["build_context"]["target"] == "board"
    assert result[0]["build_context"]["status"] == "unknown"


def test_new_table_and_review_tools_have_distinct_authority(monkeypatch, tmp_path):
    module = import_mcp_module(monkeypatch, tmp_path)
    import client_policy
    import ingest_runtime
    tools = {tool.name: tool for tool in asyncio.run(module.mcp.list_tools())}
    assert tools["query_table"].annotations.readOnlyHint is True
    assert tools["review_text"].annotations.readOnlyHint is False
    assert "review_text" in client_policy.ASK_TOOLS
    with ingest_runtime.begin("test ingest"):
        with pytest.raises(ingest_runtime.IngestBusyError):
            module.query_table.fn()
        assert module.review_text.fn().startswith("稍後重試:")


def test_ingest_selector_schema_avoids_unsupported_grammar_repetition_and_keeps_limits(monkeypatch, tmp_path):
    module = import_mcp_module(monkeypatch, tmp_path)
    tools = {tool.name: tool for tool in asyncio.run(module.mcp.list_tools())}
    properties = tools["ingest_document"].inputSchema["properties"]
    # llama.cpp rejects finite repetition counts above 2000 before generation.
    # A large API safety bound must remain enforced outside its GBNF grammar.
    for key in ("redo_pages", "redo_figures"):
        for branch in properties[key]["anyOf"]:
            assert branch.get("maxItems", 0) <= 2000
    (tmp_path / "spec.pdf").write_bytes(b"synthetic PDF")
    def forbidden(*args, **kwargs):
        pytest.fail("oversized selectors reached child execution")
    monkeypatch.setattr(module, "_run_rag_subprocess", forbidden)
    for key, values in (("redo_pages", list(range(1, 10002))),
                        ("redo_figures", [f"fig_{index:016x}" for index in range(10001)])):
        result = module.ingest_document.fn("spec.pdf", **{key: values})
        assert "錯誤:" in result and "10000" in result
    assert not (tmp_path / ".codetrail" / "ingest").exists()


# ── list_table_rows / load_playbook / miss guidance: what the model can still see after the adapter ──

_ROOMY = ResultBudget(200_000, 600_000, False, False)
_ACCOUNT_LABELS = ("table_rows_status", "has_ref", "complete", "reason", "counts", "scope_digest",
                   "scope", "where", "columns")
_ACCOUNT_OVERFLOW = (
    "status: error\n"
    "next: The completeness account does not fit this result budget; shorten document_id/where/columns "
    "selectors or raise the context. Nothing was returned."
)


def _row_listing(*, offset=0, returned=6, matched=None, cell_chars=0, excluded_details=0, **counts):
    """A `list_table_rows` payload in the frozen shape, with rows offset+1 .. offset+returned."""
    document = "qa/docs/spec.pdf::0123456789abcdef"
    matched = offset + returned if matched is None else matched
    numbers = range(offset + 1, offset + returned + 1)
    tally = {"tables_scanned": 2, "rows_scanned": 911, "matched": matched, "undetermined": 0,
             "skipped_tables": 0, "excluded_tables": 0, "returned": returned} | counts
    complete = not (tally["excluded_tables"] or tally["skipped_tables"] or tally["undetermined"])
    next_offset = offset + returned if offset + returned < matched else None
    return {
        "has_ref": True, "status": "ok" if complete else "partial",
        "reason": "complete_scan" if complete else "incomplete_scan", "complete": complete,
        "counts": tally, "scope_digest": "0a1b2c3d4e5f",
        "scope": {"requested_document_id": "spec.pdf", "requested_figure_id": "",
                  "resolved_document_ids": [document], "candidate_document_ids": [],
                  "document_resolution": "alias", "all_eligible_tables": False},
        "where": [{"column": "Reset", "op": "!=", "value": "0", "comparison": "numeric"}],
        "columns": ["Register", "Reset"],
        "tables": [{"t": 1, "document_id": document, "source": "spec.pdf", "figure_id": "fig_" + "a" * 16,
                    "page": 12, "revision": 3, "verification_status": "native_verified",
                    "evidence_ref": ".codetrail/figures/synthetic/run/manifest.json",
                    "bbox": [1, 2, 3, 4], "missing_output": []}] if returned else [],
        "rows": [{"n": number, "t": 1, "row_index": number,
                  "cells": {"1 Register": f"REG_{number:04d}" + "x" * cell_chars, "10 Reset": f"0x{number:04X}",
                            "2 Access": "RW"},
                  "unobserved": {}} for number in numbers],
        "offset": offset, "next_offset": next_offset, "truncated": next_offset is not None,
        "scanned": [{"document_id": document, "figure_id": "fig_" + "a" * 16, "page": 12,
                     "rows": 911, "matched": matched, "undetermined": 0}],
        "excluded": [{"document_id": document, "figure_id": f"fig_{index:016x}", "page": 40 + index,
                      "reason": "table_not_verified"} for index in range(excluded_details)],
        "skipped": [], "undetermined": [], "value_samples": [],
    }


def _visible_rows(text):
    return [int(number) for number in re.findall(r'(?m)^row: \{"n":([0-9]+),', text)]


def _next_line(text):
    lines = text.split("\n")
    return lines[1][len("next: "):] if len(lines) > 1 and lines[1].startswith("next: ") else None


@pytest.mark.smoke
def test_row_listing_account_survives_budget_truncation_or_fails_loud():
    """Counts are the answer to "how many"; a cut may drop rows and details, never the account."""
    payload = _row_listing(returned=60, matched=137, cell_chars=40, excluded_details=30, excluded_tables=30)
    whole = adapt_tool_result("list_table_rows", payload, budget=_ROOMY)
    lines = whole.content[0].text.split("\n")
    assert lines[0] == "status: partial" and lines[1].startswith("next: 30 table(s) were excluded")
    account = lines[2:11]
    assert [line.split(":", 1)[0] for line in account] == list(_ACCOUNT_LABELS)
    assert account[4] == ('counts: {"excluded_tables":30,"matched":137,"returned":60,"rows_scanned":911,'
                          '"skipped_tables":0,"tables_scanned":2,"undetermined":0}')
    assert account[5] == "scope_digest: 0a1b2c3d4e5f" and account[2] == "complete: false"
    # A locator precedes the first row that needs it; cells keep column order (10 after 1, before 2).
    assert lines[11].startswith('table: {"t":1,"document_id":"qa/docs/spec.pdf::0123456789abcdef"')
    assert lines[12].startswith('row: {"n":1,"t":1,"row_index":1,"cells":{"1 Register":"REG_0001')
    assert lines[12].index('"10 Reset"') < lines[12].index('"2 Access"')
    assert _visible_rows(whole.content[0].text) == list(range(1, 61))
    assert whole.structuredContent == payload and whole.isError is False

    need = estimate_result_tokens(whole.content[0].text)
    seen = set()
    for limit in range(30, need + 60, 23):
        result = adapt_tool_result("list_table_rows", payload, budget=ResultBudget(limit, limit * 3, False, False))
        text = result.content[0].text
        assert result.structuredContent == payload
        if result.isError:
            # Nothing partial: no stray count, row or locator next to the refusal.
            assert text == _ACCOUNT_OVERFLOW, text
            seen.add("overflow")
            continue
        cut = text.split("\n")
        assert cut[0] == "status: partial" and cut[2:11] == account, (limit, cut[:11])
        assert estimate_result_tokens(text) <= limit, (limit, estimate_result_tokens(text))
        rows = _visible_rows(text)
        assert rows == list(range(1, len(rows) + 1))
        seen.add("all rows" if len(rows) == 60 else "some rows" if rows else "account only")
    assert seen == {"overflow", "account only", "some rows", "all rows"}, seen

    # An error payload with almost no keys still renders a full, explicit account.
    minimal = adapt_tool_result("list_table_rows", {"status": "error", "reason": "ValueError: bad op"}, budget=_ROOMY)
    assert minimal.isError is True
    assert minimal.content[0].text.split("\n")[:5] == [
        "status: error", "next: Correct the reported selector, where or column problem before retrying.",
        "table_rows_status: error", "has_ref: false", "complete: false"]


@pytest.mark.smoke
def test_row_listing_budget_truncation_recomputes_the_resume_offset():
    """The payload's next_offset describes rows a budget cut may have removed; only visible rows count."""
    payload = _row_listing(offset=40, returned=50, matched=200, cell_chars=120)
    assert payload["next_offset"] == 90
    whole = adapt_tool_result("list_table_rows", payload, budget=_ROOMY).content[0].text
    assert _next_line(whole) == ("Use list_table_rows with offset=90 and the same arguments to continue "
                                 "(scope_digest 0a1b2c3d4e5f); counts already cover the whole scope.")
    assert 'rows_shown: {"offset":40,"returned":50,"next_offset":90}' in whole

    limit = estimate_result_tokens(whole) // 2
    cut = adapt_tool_result("list_table_rows", payload, budget=ResultBudget(limit, limit * 3, False, False))
    text = cut.content[0].text
    rows = _visible_rows(text)
    assert rows and rows == list(range(41, rows[-1] + 1)) and rows[-1] < 90
    assert f"Use list_table_rows with offset={rows[-1]} and the same arguments to continue" in _next_line(text)
    assert "offset=90" not in text and '"next_offset":90' not in text
    assert f'"n":{rows[-1] + 1},' not in text
    assert text.endswith("[result truncated by context budget]") and cut.isError is False
    assert estimate_result_tokens(text) <= limit

    # An explicit character budget takes the same path.
    explicit = adapt_tool_result("list_table_rows", payload, budget=ResultBudget(1500, 4500, True, False))
    rows = _visible_rows(explicit.content[0].text)
    assert rows and rows[-1] < 90 and f"offset={rows[-1]} and the same arguments" in explicit.content[0].text
    assert len(explicit.content[0].text) <= 4500

    # Not one row fits: there is no visible row to resume from, so say how to make one fit.
    huge = _row_listing(offset=40, returned=50, matched=200, cell_chars=9000)
    blocked = adapt_tool_result("list_table_rows", huge, budget=ResultBudget(1400, 4200, False, False))
    text = blocked.content[0].text
    assert _visible_rows(text) == [] and blocked.isError is False
    assert ("No row fit this result budget: request fewer columns (projection never changes which rows match) "
            "or read one cell with query_table; continue from offset=40.") in _next_line(text)
    assert "Use list_table_rows with offset=" not in text

    # Every row visible and nothing left to page: only the detail lines were lost, and it says so.
    detailed = _row_listing(returned=3, excluded_details=400, excluded_tables=400)
    limit = estimate_result_tokens(adapt_tool_result("list_table_rows", detailed, budget=_ROOMY).content[0].text) // 2
    trimmed = adapt_tool_result("list_table_rows", detailed, budget=ResultBudget(limit, limit * 3, False, False))
    text = trimmed.content[0].text
    assert _visible_rows(text) == [1, 2, 3]
    assert _next_line(text).endswith("Diagnostic details were cut; call again with limit=0 to list them.")
    assert "400 table(s) were excluded" in _next_line(text) and "Use list_table_rows with offset=" not in text

    # A count-only page returns no row to resume from: "the same arguments" could never advance.
    counted = _row_listing(returned=0, matched=137)
    counted.update(next_offset=0, truncated=True)
    text = adapt_tool_result("list_table_rows", counted, budget=_ROOMY).content[0].text
    assert text.startswith("status: ok\ntable_rows_status: ok\n") and '"matched":137' in text
    assert "Use list_table_rows with offset=" not in text

    # Nothing cut: the payload's own position is the visible one.
    complete = adapt_tool_result("list_table_rows", _row_listing(returned=4), budget=_ROOMY)
    assert complete.content[0].text.startswith("status: ok\ntable_rows_status: ok\n")
    empty = adapt_tool_result("list_table_rows", _row_listing(returned=0, matched=0), budget=_ROOMY)
    assert "Zero rows matched in the scanned verified tables; check value_samples" in _next_line(empty.content[0].text)


@pytest.mark.smoke
def test_miss_results_point_to_the_sibling_index_without_widening_scope():
    """A miss names the sibling index only where that index can keep the caller's scope."""
    figure = "fig_" + "a" * 16
    keep = ("Keep the requested document_id and figure_id scope; verify the current document ID, "
            "figure and review status before retrying. Do not broaden to another source or infer a value.")

    def table_miss(document, wanted_figure, status="not_found", reason="no matching verified table row",
                   excluded=()):
        payload = {"has_ref": False, "status": status, "reason": reason, "matches": [],
                   "ambiguous": status == "ambiguous",
                   "excluded": [{"figure_id": figure, "reason": why} for why in excluded],
                   "scope": {"requested_document_id": document, "requested_figure_id": wanted_figure,
                             "resolved_document_ids": [], "candidate_document_ids": [],
                             "document_resolution": "alias" if document else "all_documents",
                             "all_eligible_tables": not (document or wanted_figure)}}
        result = adapt_tool_result("query_table", payload, budget=_ROOMY)
        assert result.content[0].text.startswith("status: partial\nnext: ") and result.isError is False
        assert "Use another indexed source" not in result.content[0].text or not (document or wanted_figure)
        return _next_line(result.content[0].text)

    # A document keeps its scope in query_knowledge(source=...).
    assert table_miss("spec.pdf", "") == (
        keep + " Before reporting the value absent, confirm once with query_knowledge restricted to the same source.")
    # A figure does not: no confirmation is offered, the scoped answer stays unknown.
    for document in ("spec.pdf", ""):
        scoped = table_miss(document, figure)
        assert scoped == (keep + " query_knowledge cannot filter by figure_id; report the value as unknown "
                          "for this figure instead of searching wider.")
        assert "confirm once" not in scoped
    # The scope itself did not resolve: there is nothing to confirm elsewhere.
    assert table_miss("spec.pdf", figure, reason="figure_scope_not_found") == keep
    assert table_miss("missing.pdf", "", reason="document_scope_not_found") == keep
    assert table_miss("spec.pdf", "", status="ambiguous", reason="document_scope_ambiguous") == keep
    # With several tables in a document a miss is rarely `not_found`: one table without a
    # register column, or one awaiting review, reports the same miss as ambiguous or
    # unverified.  No row was established either way, so the same confirmation applies.
    confirm = keep + " Before reporting the value absent, confirm once with query_knowledge restricted to the same source."
    assert table_miss("spec.pdf", "", status="unverified",
                      reason="no matching row could be established from current verified payloads",
                      excluded=("table_not_verified",)) == confirm
    assert table_miss("spec.pdf", "", status="ambiguous", reason="multiple rows/columns or an unresolved selector",
                      excluded=("selector_column_required",)) == confirm
    # A row did match (several of them, or one whose cell is not directly observed): the
    # value exists and has to be narrowed down, so no other index is suggested.
    assert table_miss("spec.pdf", "", status="ambiguous", reason="multiple rows/columns or an unresolved selector",
                      excluded=("multiple_matching_rows", "multiple_matching_rows")) == keep
    assert table_miss("spec.pdf", "", status="unverified", reason="selected row contains a cell without a direct observed value",
                      excluded=("result_cell_unreadable",)) == keep
    # No scope was requested, so the document index is the same scope.
    unscoped = ("No verified table row matched. Before reporting the value absent, confirm once with query_knowledge; "
                "a value found only outside verified tables stays unverified. Do not infer the missing answer.")
    assert table_miss("", "") == unscoped
    assert table_miss("", "", status="unverified", reason="no matching row could be established from current verified payloads",
                      excluded=("table_not_verified",)) == unscoped
    assert "confirm once" not in table_miss("", "", status="ambiguous", reason="multiple rows",
                                            excluded=("multiple_matching_rows",))

    knowledge = {"text": "", "display": "", "refs": [], "top_score": 0.0, "has_ref": False,
                 "excluded_figures": [], "excluded_text": [], "review_hint": ""}
    text = adapt_tool_result("query_knowledge", knowledge, budget=_ROOMY).content[0].text
    assert text.startswith("status: partial\n") and _next_line(text) == (
        "No reference passed the retrieval gate. If the fact is a table or register value, confirm once with "
        "query_table or list_table_rows before reporting it absent, passing the same source as document_id "
        "when one was given; otherwise report it unknown or ingest the missing evidence. "
        "Do not infer the missing answer.")
    unloaded = adapt_tool_result("query_knowledge", knowledge | {"error": "knowledge base not loaded"}, budget=_ROOMY)
    assert unloaded.isError is True and "confirm once" not in unloaded.content[0].text
    refused = adapt_tool_result("query_knowledge_strict",
                                {"refused": True, "reason": "weak_ref_for_spec_question", "refs": []}, budget=_ROOMY)
    assert _next_line(refused.content[0].text) == (
        "Use another indexed source or ingest evidence; do not infer the missing answer.")

    def code(payload):
        result = adapt_tool_result("code_rag_search", payload, budget=_ROOMY)
        return result.content[0].text.split("\n")[0], _next_line(result.content[0].text)

    untargeted = {"target": None, "status": "unknown"}
    for empty in ([], [{"mode": "semantic", "results": [], "build_context": untargeted}]):
        assert code(empty) == ("status: partial", (
            "No semantic hit in the code index. Before reporting it absent, confirm once with grep_code "
            "for the exact identifier or literal; the index may be scoped or stale."))
    # grep_code has no build_target: a literal hit cannot answer a target-scoped question.
    status, guidance = code([{"mode": "semantic", "results": [],
                              "build_context": {"target": "board", "status": "active"}}])
    assert status == "status: partial" and guidance == (
        "No semantic hit within the selected build_target. grep_code cannot honor a build_target: a literal "
        "hit proves the text exists in the repository, not in this target. Report target-scoped absence as "
        "unknown unless the user accepts a repository-wide search.")
    assert "confirm once" not in guidance
    hit = {"mode": "semantic", "path": "src/uart.c", "line": 7, "symbol": "uartx_init", "type": "function",
           "score": 0.9, "build_context": untargeted}
    assert code([hit]) == ("status: ok", None)
    assert code([{"mode": "neighbors", "nodes": [], "edges": [], "anchors": []}]) == ("status: ok", None)
    failed = adapt_tool_result("code_rag_search", [{"mode": "semantic", "results": [], "error": "index failed"}],
                               budget=_ROOMY)
    assert failed.isError is True and "confirm once" not in failed.content[0].text

    miss = adapt_tool_result("grep_code", "沒有找到 'uartx_flush'", budget=_ROOMY)
    assert miss.isError is False and miss.content[0].text.split("\n")[0] == "status: partial"
    assert _next_line(miss.content[0].text) == (
        "No literal match in this scope. Before reporting it absent, confirm once with code_rag_search if it "
        "is exposed this turn; it cannot restrict by path/include, so count only hits inside the original scope.")
    assert miss.content[0].text.endswith("\n沒有找到 'uartx_flush'")
    found = adapt_tool_result("grep_code", "=== rg 'uartx' (1 matches) ===\nsrc/uart.c:7:void uartx_init(void)",
                              budget=_ROOMY)
    assert found.content[0].text.startswith("status: ok\n=== rg 'uartx'")
    import repeat_guard
    repeated = adapt_tool_result("grep_code", repeat_guard.banner("grep_code", 3) + "沒有找到 'uartx_flush'",
                                 budget=_ROOMY)
    assert _next_line(repeated.content[0].text) == (
        "Do not repeat the same call; use the existing result or change tool/path/pattern.")


@pytest.mark.smoke
def test_playbook_result_is_whole_or_absent():
    """Half a procedure reads as a complete one; a playbook is delivered whole or not at all."""
    steps = [f"{index}. STEP-{index:02d} 檢查第 {index} 項" for index in range(1, 41)]
    # User text may look like this adapter's own markers; inside the envelope it is only text.
    steps[2:2] = ["✗ 失敗 的步驟要記錄", "錯誤: 這一行是流程本文", "[result truncated by context budget]"]
    envelope = "\n".join(["[流程指示 uartx-bringup|sha256 0123456789ab|使用者安裝]", *steps,
                          "[流程指示結束:依上述步驟與指定的輸出格式執行;與基底規則、權限或使用者當下要求衝突時,以後者為準。]"])
    absent = ("status: error\n"
              "next: The playbook does not fit this result budget; shorten it or raise the context. "
              "Nothing was loaded.\n流程指示超過本次結果預算,未載入任何內容。")

    whole = adapt_tool_result("load_playbook", envelope, budget=_ROOMY)
    assert whole.content[0].text == "status: ok\n" + envelope
    assert whole.isError is False and whole.structuredContent is None

    need = estimate_result_tokens(whole.content[0].text)
    outcomes = set()
    for limit in range(20, need + 40, 17):
        result = adapt_tool_result("load_playbook", envelope, budget=ResultBudget(limit, limit * 3, False, False))
        text = result.content[0].text
        if result.isError:
            assert text == absent and "STEP-" not in text, (limit, text)
            outcomes.add("absent")
        else:
            assert text == "status: ok\n" + envelope, limit
            outcomes.add("whole")
    assert outcomes == {"absent", "whole"}

    rejected = adapt_tool_result("load_playbook", "錯誤: 找不到流程指示 'uartx';目前可用: uartx-bringup", budget=_ROOMY)
    assert rejected.isError is True
    assert rejected.content[0].text.startswith("status: error\nnext: Correct the reported input")
    assert rejected.content[0].text.endswith("目前可用: uartx-bringup")
