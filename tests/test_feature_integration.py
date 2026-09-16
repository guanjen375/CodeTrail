"""Model-visible evidence and public tool boundaries for the six-feature delivery."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from tests._harness import import_mcp_module
from tool_result_adapter import ResultBudget, adapt_tool_result

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
