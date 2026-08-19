#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""code_rag_search 的回傳契約(§8):預設 shape 完全不變(key-set 鎖死)、
evidence 模式欄位、neighbors/path 模式、graph 缺席行為。全部離線。"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import code_rag  # noqa: E402

# 預設 shape 的 key 契約(§8.1):必含 5 鍵;end_line/parent 僅在 item 具備時
# 出現;不得多任何新 key。
REQUIRED_KEYS = {"path", "symbol", "type", "line", "score"}
OPTIONAL_KEYS = {"end_line", "parent"}
EVIDENCE_KEYS = {"score_components", "backend", "confidence", "relations", "graph_status"}


@pytest.fixture
def mcp_module(monkeypatch, tmp_path: Path):
    pytest.importorskip("mcp", reason="mcp 套件未安裝")
    (tmp_path / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "import util\n\n\ndef entry():\n    return util.helper()\n", encoding="utf-8")

    monkeypatch.setenv("AICODE_ROOT", str(tmp_path))
    monkeypatch.setenv("AICODE_MODEL", "example-code-model:30b")
    monkeypatch.setenv("AICODE_LLAMA_BASE_URL", "http://127.0.0.1:65535")
    monkeypatch.setenv("AICODE_REQUIRED_MODELS_CHECK_SKIP", "1")
    monkeypatch.setenv("AI_CODE_PATCH", "")
    monkeypatch.setenv("AI_CODE_RUN_TESTS", "")
    monkeypatch.setenv("AI_CODE_ENABLE_BUILD_COMMANDS", "")

    import config as _config
    monkeypatch.setattr(_config, "PATCH_ENABLED", _config.PATCH_ENABLED)
    monkeypatch.setattr(_config, "RUN_COMMAND_ENABLED", _config.RUN_COMMAND_ENABLED)
    monkeypatch.setattr(_config, "ALLOWED_COMMANDS", list(_config.ALLOWED_COMMANDS))

    code_rag._INDEX_SCAN_CACHE.clear()
    sys.modules.pop("mcp_server", None)
    import mcp_server  # type: ignore

    importlib.reload(mcp_server)

    # 離線 stub:embedding 與 reranker 都不打 server
    monkeypatch.setattr(code_rag, "USE_RERANKER", False)
    monkeypatch.setattr(mcp_server.CODE_RAG, "_get_embedding", lambda _t: [1.0, 0.0])
    monkeypatch.setattr(mcp_server.CODE_RAG, "_embed_texts_batched",
                        lambda texts: [[1.0, 0.0]] * len(texts))
    yield mcp_server
    sys.modules.pop("mcp_server", None)
    code_rag._INDEX_SCAN_CACHE.clear()


def test_default_shape_key_set_is_locked(mcp_module):
    results = mcp_module.code_rag_search("entry helper")
    assert results, "fixture repo 必須有結果"
    for row in results:
        keys = set(row)
        assert REQUIRED_KEYS <= keys, f"缺必要鍵: {REQUIRED_KEYS - keys}"
        extras = keys - REQUIRED_KEYS - OPTIONAL_KEYS
        assert not extras, (
            f"預設回傳 shape 出現新 key {sorted(extras)} —— §8.1 禁止;"
            "evidence 欄位只能在 include_evidence=True 出現"
        )
        assert isinstance(row["score"], float)


def test_default_shape_has_no_graph_or_evidence_fields(mcp_module):
    for row in mcp_module.code_rag_search("entry"):
        for banned in EVIDENCE_KEYS:
            assert banned not in row


def test_include_evidence_adds_exactly_the_documented_fields(mcp_module):
    results = mcp_module.code_rag_search("entry helper", include_evidence=True)
    assert results
    for row in results:
        keys = set(row)
        assert EVIDENCE_KEYS <= keys
        extras = keys - REQUIRED_KEYS - OPTIONAL_KEYS - EVIDENCE_KEYS
        assert not extras, f"evidence 模式出現未文件化欄位: {sorted(extras)}"
        sc = row["score_components"]
        assert set(sc) == {"rerank_score", "combined_score", "score_source"}
        # 離線(reranker off)→ fusion;rerank_score 必須是 None 不是 0.0
        assert sc["score_source"] == "fusion"
        assert sc["rerank_score"] is None
        assert row["score"] == round(sc["combined_score"], 3)
        assert row["backend"] == "python-ast"
        assert isinstance(row["relations"], list)
        assert len(row["relations"]) <= 5
        assert row["graph_status"] == "ok"


def test_evidence_relations_carry_stepwise_evidence(mcp_module):
    results = mcp_module.code_rag_search("entry", include_evidence=True)
    entry_row = next(r for r in results if r["symbol"] == "entry")
    rels = entry_row["relations"]
    assert any(r["dst"] == "helper" for r in rels), "entry→helper 的 1-hop 關係要出現"
    for rel in rels:
        path, _, line = rel["evidence"].rpartition(":")
        assert path and line.isdigit(), f"relation 證據必須是 file:line: {rel['evidence']}"


def test_neighbors_mode_returns_graph_structure(mcp_module):
    [resp] = mcp_module.code_rag_search("entry", mode="neighbors")
    assert resp["mode"] == "neighbors"
    assert resp["graph_status"] == "ok"
    assert resp["anchors"] and resp["anchors"][0]["name"] == "entry"
    assert any(e["dst"] == "helper" for e in resp["edges"])
    for e in resp["edges"]:
        path, _, line = e["evidence"].rpartition(":")
        assert path and line.isdigit()


def test_neighbors_without_anchor_errors_with_suggestions(mcp_module):
    with pytest.raises(RuntimeError) as exc:
        mcp_module.code_rag_search("helpe", mode="neighbors")
    message = str(exc.value)
    assert "找不到 symbol" in message
    assert "helper" in message, "要附近似候選"


def test_path_mode_returns_stepwise_paths(mcp_module):
    [resp] = mcp_module.code_rag_search("entry -> helper", mode="path")
    assert resp["mode"] == "path"
    assert resp["paths"], "entry→helper 必須有路徑"
    path = resp["paths"][0]
    assert path[0]["src"] == "entry" and path[-1]["dst"] == "helper"
    assert len(resp["paths"]) <= 3


def test_path_mode_rejects_bad_format_and_unknown_symbols(mcp_module):
    with pytest.raises(ValueError, match="SRC -> DST"):
        mcp_module.code_rag_search("entry", mode="path")
    import code_graph

    with pytest.raises(code_graph.CodeGraphError, match="resolve 失敗"):
        mcp_module.code_rag_search("entry -> no_such_symbol", mode="path")


def test_unknown_mode_is_rejected(mcp_module):
    with pytest.raises(ValueError, match="semantic|neighbors|path"):
        mcp_module.code_rag_search("x", mode="bogus")


def test_corrupt_graph_fails_graph_modes_but_not_semantic(mcp_module, tmp_path):
    # 先讓 graph 建起來,再弄壞
    mcp_module.code_rag_search("entry", mode="neighbors")
    (tmp_path / ".code_rag_graph.sqlite3").write_bytes(b"garbage")
    # 換一個 fresh singleton 模擬新 process 撞上壞檔
    mcp_module._CODE_GRAPH = None

    import code_graph

    with pytest.raises(code_graph.CodeGraphError):
        mcp_module.code_rag_search("entry", mode="neighbors")

    # semantic 完全不受影響:預設 shape、無 graph 欄位
    results = mcp_module.code_rag_search("entry")
    assert results
    for row in results:
        assert not (set(row) & EVIDENCE_KEYS)

    # evidence 模式:graph unavailable 但 semantic 結果照常
    results = mcp_module.code_rag_search("entry", include_evidence=True)
    assert results
    assert all(r["graph_status"].startswith("unavailable") for r in results)
    assert all(r["relations"] == [] for r in results)
