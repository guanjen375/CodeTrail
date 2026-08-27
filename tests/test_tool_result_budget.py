"""Result budgets must follow the active context, not a frozen char default."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests._harness import import_mcp_module
from tool_result_adapter import (
    ResultBudget,
    adapt_tool_result,
    estimate_result_tokens,
)


def _text(result) -> str:
    return "".join(getattr(block, "text", "") or "" for block in result.content)


def _run_tool(mcp_module, name: str, arguments: dict):
    tool = mcp_module.mcp._tool_manager.get_tool(name)
    return asyncio.run(tool.run(arguments, convert_result=True))


@pytest.mark.smoke
def test_default_budget_tracks_n_ctx(monkeypatch, tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "large.txt").write_text("0123456789abcdef\n" * 5_000, encoding="utf-8")
    mcp_module = import_mcp_module(monkeypatch, root)

    monkeypatch.setattr(mcp_module.config, "N_CTX", 1_000)
    small = _text(_run_tool(mcp_module, "read_file", {"path": "large.txt"}))
    assert small.startswith("status: partial\nnext:")
    assert estimate_result_tokens(small) <= int(1_000 * 0.12) + 40

    monkeypatch.setattr(mcp_module.config, "N_CTX", 4_000)
    large = _text(_run_tool(mcp_module, "read_file", {"path": "large.txt"}))
    assert len(large) > len(small)
    assert estimate_result_tokens(large) <= int(4_000 * 0.12) + 40


def test_grep_partial_next_names_every_narrowing_control():
    result = adapt_tool_result(
        "grep_code",
        "match\n... [truncated: too many matches]",
        budget=ResultBudget(
            token_limit=1_000,
            char_limit=3_000,
            explicit=False,
            context_risk=False,
        ),
    )
    assert result.content[0].text.startswith(
        "status: partial\nnext: Narrow path/include/pattern"
    )


def test_code_rag_fastmcp_path_wraps_core_and_injects_dynamic_default(
    monkeypatch, tmp_path: Path
):
    mcp_module = import_mcp_module(monkeypatch, tmp_path)
    monkeypatch.setattr(mcp_module.CODE_RAG, "query_ranked", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(mcp_module.CODE_RAG, "_scan_code_files", lambda: [])
    monkeypatch.setattr(
        mcp_module.code_context,
        "collect_safe_lexical_hits",
        lambda *_args, **_kwargs: [],
    )

    def graph_unavailable():
        raise RuntimeError("synthetic graph unavailable")

    monkeypatch.setattr(mcp_module, "_graph_for_query", graph_unavailable)

    monkeypatch.setattr(mcp_module.config, "N_CTX", 10_000)
    small = _run_tool(
        mcp_module,
        "code_rag_search",
        {"query": "locate startup", "mode": "context"},
    )
    small_core = small.structuredContent["result"]
    assert isinstance(small_core, list) and len(small_core) == 1
    small_budget = small_core[0]["budget_chars"]
    assert 2_000 <= small_budget < int(10_000 * 0.12) * 3

    monkeypatch.setattr(mcp_module.config, "N_CTX", 20_000)
    large = _run_tool(
        mcp_module,
        "code_rag_search",
        {"query": "locate startup", "mode": "context"},
    )
    assert large.structuredContent["result"][0]["budget_chars"] > small_budget


def test_success_content_words_do_not_forge_error_or_partial_status():
    budget = ResultBudget(2_000, 6_000, explicit=False, context_risk=False)
    info = adapt_tool_result("file_info", "error.log: 檔案, 1 行, 9 字元", budget=budget)
    read = adapt_tool_result(
        "read_file",
        "=== notes.txt (行 1-1 / 共 1 行) ===\n   1 | the word truncated is data",
        budget=budget,
    )
    grep = adapt_tool_result(
        "grep_code",
        "=== rg 'truncated' (1 matches) ===\nnotes.txt:1: truncated is data",
        budget=budget,
    )
    for result in (info, read, grep):
        assert result.isError is False
        assert result.content[0].text.startswith("status: ok\n")


def test_budgeted_evidence_keeps_metadata_before_bulk_text():
    budget = ResultBudget(220, 660, explicit=False, context_risk=False)
    code_payload = [{
        "query": "startup path",
        "evidence": [{
            "path": "src/main.c",
            "start_line": 10,
            "end_line": 200,
            "symbol": "main",
            "reason": "semantic",
            "text": "\n".join(f"{line:4d} | int value_{line} = {line};" for line in range(10, 201)),
        }],
        "uncertainties": [{
            "target": "call graph",
            "reason": "relationship evidence unavailable: graph degraded",
        }],
        "seeds": [{"path": "src/main.c", "line": 10, "symbol": "main"}],
        "graph_status": "degraded: synthetic",
        "truncated": True,
        "budget_chars": 2_000,
        "used_chars": 1_999,
    }]
    code_text = _text(adapt_tool_result("code_rag_search", code_payload, budget=budget))
    assert "graph_status: degraded: synthetic" in code_text
    assert "truncated: true" in code_text
    assert "relationship evidence unavailable" in code_text

    query_payload = {
        "text": "\n".join(f"evidence line {line}" for line in range(500)),
        "display": "duplicate display",
        "refs": [{"source": "spec.pdf", "page": 7}],
        "has_ref": True,
        "excluded_figures": [{
            "source": "spec.pdf",
            "page": 8,
            "verification_status": "needs_review",
        }],
        "review_hint": "inspect figure 8 before asserting the value",
    }
    query_text = _text(adapt_tool_result("query_knowledge", query_payload, budget=budget))
    assert "spec.pdf p.7" in query_text
    assert "excluded_figures: spec.pdf p.8" in query_text
    assert "review: inspect figure 8" in query_text


def test_review_figure_budget_never_keeps_a_partial_canonical_line():
    budget = ResultBudget(120, 360, explicit=False, context_risk=False)
    canonical_line = '   payload: {"address":"0x4000_0100","cells":"' + "x" * 800 + '"}'
    result = adapt_tool_result(
        "review_figures",
        "── figure_id: fig_1\n" + canonical_line,
        budget=budget,
    )
    text = _text(result)
    assert canonical_line not in text
    assert "payload:" not in text
    assert "[result truncated by context budget]" in text
