"""Silent-failure gate for the model-visible MCP catalog."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mcp_contract import MCP_INSTRUCTIONS, PUBLIC_TOOL_ORDER
from tests._harness import import_mcp_module


@pytest.mark.smoke
def test_live_catalog_is_bounded_typed_and_ordered(monkeypatch, tmp_path: Path):
    mcp_module = import_mcp_module(monkeypatch, tmp_path)
    tools = asyncio.run(mcp_module.mcp.list_tools())

    assert tuple(tool.name for tool in tools) == PUBLIC_TOOL_ORDER
    assert mcp_module.mcp.instructions == MCP_INSTRUCTIONS
    assert len(mcp_module.mcp.instructions) <= 700

    descriptions = {tool.name: tool.description or "" for tool in tools}
    assert sum(map(len, descriptions.values())) <= 12_000
    for name, description in descriptions.items():
        cap = 2_200 if name == "apply_patch" else 1_600
        assert len(description) <= cap, (name, len(description))

    effective_chars = sum(
        len(tool.description or "")
        + len(json.dumps(tool.inputSchema, ensure_ascii=False, sort_keys=True))
        for tool in tools
    )
    assert effective_chars <= 20_000

    for tool in tools:
        for name, schema in tool.inputSchema.get("properties", {}).items():
            assert schema.get("description"), f"{tool.name}.{name} lacks description"

    by_name = {tool.name: tool for tool in tools}
    assert by_name["code_rag_search"].inputSchema["properties"]["mode"]["enum"] == [
        "semantic", "neighbors", "path", "context"
    ]
    assert by_name["analyze_file"].inputSchema["properties"]["view"]["enum"] == [
        "summary", "headers", "sections", "memmap", "symbols", "imports",
        "relocs", "dynamic", "dwarf", "disasm", "strings",
    ]
    timeout = by_name["run_command"].inputSchema["properties"]["timeout"]
    assert (timeout["minimum"], timeout["maximum"]) == (1, 600)

    evidence = {"code_rag_search", "query_knowledge", "query_knowledge_strict"}
    for tool in tools:
        if tool.name not in evidence:
            assert tool.outputSchema is None, tool.name

