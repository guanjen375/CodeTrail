#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""code_rag_search 的回傳契約(§8):預設 shape 完全不變(key-set 鎖死)、
evidence 模式欄位、neighbors/path 模式、graph 缺席行為。全部離線。"""
from __future__ import annotations

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
CONTEXT_KEYS = {
    "query", "evidence", "uncertainties", "seeds", "graph_status", "truncated",
    "budget_chars", "used_chars",
}


@pytest.fixture
def mcp_module(monkeypatch, tmp_path: Path):
    pytest.importorskip("mcp", reason="mcp 套件未安裝")
    (tmp_path / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    # 直接 name call:path mode 只走確定解析的邊,`util.helper()` 這種
    # attribute call 只有 heuristic confidence,本來就不該進呼叫鏈
    # (該行為的回歸測試在 tests/test_code_graph.py)。
    (tmp_path / "app.py").write_text(
        "from util import helper\n\n\ndef entry():\n    return helper()\n",
        encoding="utf-8")

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

    # 離線 stub:embedding 與 reranker 都不打 server
    monkeypatch.setattr(code_rag, "USE_RERANKER", False)
    monkeypatch.setattr(mcp_server.CODE_RAG, "_get_embedding", lambda _t: [1.0, 0.0])
    monkeypatch.setattr(mcp_server.CODE_RAG, "_embed_texts_batched",
                        lambda texts: [[1.0, 0.0]] * len(texts))

    # graph 首次建置是顯式動作(§2:未建置時 graph 模式 fail-loud);
    # 測試模擬使用者已跑過 `python code_graph.py --root <root>`。
    import code_graph as _code_graph

    _code_graph.CodeGraph(str(tmp_path)).build()

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


def test_context_mode_has_exact_top_level_contract_and_char_accounting(mcp_module):
    [bundle] = mcp_module.code_rag_search(
        "entry calls helper", mode="context", top_k=2, max_chars=12000
    )
    assert set(bundle) == CONTEXT_KEYS
    assert bundle["graph_status"] == "ok"
    assert bundle["budget_chars"] == 12000
    assert bundle["evidence"]
    assert bundle["seeds"]
    actual = sum(len(item["text"]) for item in bundle["evidence"])
    assert bundle["used_chars"] == actual <= bundle["budget_chars"]
    for item in bundle["evidence"]:
        assert item["path"] in {"app.py", "util.py"}
        assert 1 <= item["start_line"] <= item["end_line"]
        assert item["text"].startswith(f"=== {item['path']} (行 ")


@pytest.mark.parametrize("bad", [1999, 30001, True, "12000"])
def test_context_mode_rejects_invalid_max_chars(mcp_module, bad):
    with pytest.raises(ValueError, match="2000..30000"):
        mcp_module.code_rag_search("entry", mode="context", max_chars=bad)


def test_context_mode_is_not_silently_capped_at_graph_8000(mcp_module, tmp_path):
    import json

    body = ["def big_context():", "    value = 0"]
    body.extend(f"    # evidence-{i:02d}-" + "x" * 120 for i in range(68))
    body.append("    return value")
    (tmp_path / "big.py").write_text("\n".join(body) + "\n", encoding="utf-8")
    code_rag.invalidate_scan_cache(tmp_path)

    [bundle] = mcp_module.code_rag_search(
        "big_context", mode="context", top_k=1, max_chars=12000
    )
    assert bundle["used_chars"] <= 12000
    assert len(json.dumps(bundle, ensure_ascii=False)) > 8000
    assert "truncation" not in bundle, "context must not pass through graph response cap"


def test_context_telemetry_records_metadata_but_not_evidence_text(mcp_module, monkeypatch):
    recorded = []
    monkeypatch.setattr(mcp_module.data_flywheel, "DATA_COLLECT_ENABLED", True)
    monkeypatch.setattr(
        mcp_module, "_record_kb_interaction", lambda **kwargs: recorded.append(kwargs)
    )

    [bundle] = mcp_module.code_rag_search("entry helper", mode="context")
    assert recorded and recorded[0]["extra_meta"]["mode"] == "context"
    payload = recorded[0]
    assert all(set(item) == {"path", "line", "symbol"}
               for item in payload["code_snippets"])
    evidence_text = bundle["evidence"][0]["text"]
    assert evidence_text not in repr(payload)


def test_unknown_mode_is_rejected(mcp_module):
    with pytest.raises(ValueError, match="context"):
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

    # context 降級成 semantic-only evidence，不因 graph 壞掉整體失敗。
    [bundle] = mcp_module.code_rag_search("entry", mode="context", max_chars=2000)
    assert set(bundle) == CONTEXT_KEYS
    assert bundle["graph_status"].startswith("unavailable")
    assert bundle["evidence"]
    assert all(item["reason"] == "semantic" for item in bundle["evidence"])


# ============================================================
# GPT 審核修正的回歸測試(2026-08-19 二輪)
# ============================================================
def test_neighbors_accepts_file_path_anchor(mcp_module):
    """審核 #5:「這個檔 include 誰」—— query 放 repo 相對路徑要能走 includes。"""
    [resp] = mcp_module.code_rag_search("app.py", mode="neighbors")
    assert resp["mode"] == "neighbors"
    assert resp["anchors"] == [{"file": "app.py"}]
    assert "util.py" in resp["files"]
    import_edges = [e for e in resp["edges"] if e["type"] == "imports"]
    assert any(e["src"] == "app.py" and e["dst"] == "util.py" for e in import_edges)
    for e in resp["edges"]:
        path, _, line = e["evidence"].rpartition(":")
        assert path and line.isdigit()


def test_slim_edge_preserves_ambiguity_group(mcp_module):
    """審核 #3(MCP 端):歧義資訊不得在精簡輸出被丟掉。"""
    edge = mcp_module._slim_edge({
        "src_name": "a", "dst_name": "b", "unresolved_target": "b",
        "ambiguity_group": "grp123", "type": "calls",
        "evidence_path": "x.c", "evidence_line": 3,
        "backend": "tree-sitter", "confidence": "syntactic",
        "resolution_basis": "ambiguous_condition", "condition": "#if BOARD_A",
        "resolved": False,
    })
    assert edge["ambiguity_group"] == "grp123"
    assert edge["resolved"] is False
    assert edge["resolution_basis"] == "ambiguous_condition"
    assert edge["condition"] == "#if BOARD_A"


def test_cap_holds_for_path_mode_and_after_metadata(mcp_module):
    """審核 #9:8000 上限對 paths 也成立,且 metadata 加入後仍 ≤ 上限。"""
    import json

    big_edge = {
        "src": "a" * 50, "dst": "b" * 50, "unresolved_target": None,
        "ambiguity_group": None, "type": "calls",
        "evidence": "some/deep/path/file.c:123", "backend": "tree-sitter",
        "confidence": "resolved", "resolved": True,
    }
    resp = {
        "mode": "path", "src": "a", "dst": "b",
        "paths": [[dict(big_edge) for _ in range(30)] for _ in range(20)],
        "graph_status": "ok",
    }
    capped = mcp_module._cap_graph_response(resp)
    size = len(json.dumps(capped, ensure_ascii=False))
    assert size <= mcp_module._GRAPH_RESPONSE_MAX_CHARS, (
        f"含 truncation metadata 的最終回應 {size} chars 仍超上限")
    assert capped["truncated"] is True
    assert capped["truncation"]["kept"]["paths"] < capped["truncation"]["total"]["paths"]


def test_cap_holds_for_neighbors_lists(mcp_module):
    import json

    resp = {
        "mode": "neighbors", "query": "x",
        "anchors": [{"id": "i", "name": "x"}],
        "nodes": [{"name": f"n{i}", "path": "p.py" * 30, "line": i} for i in range(400)],
        "edges": [{"src": "a" * 40, "dst": "b" * 40, "evidence": "p.py:1"}
                  for _ in range(400)],
        "graph_status": "ok", "truncated": False,
    }
    capped = mcp_module._cap_graph_response(resp)
    assert len(json.dumps(capped, ensure_ascii=False)) <= mcp_module._GRAPH_RESPONSE_MAX_CHARS


def test_missing_graph_fails_loud_with_build_command(mcp_module, tmp_path):
    """審核二輪 #3:graph 缺席時 neighbors/path 依施工單 §2 明確報錯
    (不做隱式 lazy build),錯誤訊息含建立命令;semantic 不受影響。"""
    import code_graph

    for suffix in ("", "-wal", "-shm"):
        p = tmp_path / f".code_rag_graph.sqlite3{suffix}"
        if p.exists():
            p.unlink()
    mcp_module._CODE_GRAPH = None

    with pytest.raises(code_graph.CodeGraphError, match="code_graph.py --root"):
        mcp_module.code_rag_search("entry", mode="neighbors")
    with pytest.raises(code_graph.CodeGraphError, match="尚未建立"):
        mcp_module.code_rag_search("entry -> helper", mode="path")

    # 缺席期間不得偷偷建出 graph 檔
    assert not (tmp_path / ".code_rag_graph.sqlite3").exists()

    # semantic 完全不受影響(預設 shape)
    results = mcp_module.code_rag_search("entry")
    assert results and all(not (set(r) & EVIDENCE_KEYS) for r in results)

    # evidence 模式:graph_status 揭露 unavailable,不 raise
    results = mcp_module.code_rag_search("entry", include_evidence=True)
    assert all(r["graph_status"].startswith("unavailable") for r in results)


def test_cap_is_hard_even_with_huge_echo_fields(mcp_module):
    """審核二輪 #4:超長 query(echo 欄位)也不能撐爆 8000 cap。"""
    import json

    huge = "q" * 100_000
    resp = {
        "mode": "path", "src": mcp_module._echo(huge), "dst": mcp_module._echo(huge),
        "paths": [], "graph_status": "ok",
    }
    capped = mcp_module._cap_graph_response(resp)
    assert len(json.dumps(capped, ensure_ascii=False)) <= mcp_module._GRAPH_RESPONSE_MAX_CHARS
    # echo 欄位在組 resp 時就截斷
    assert len(capped.get("src", "")) <= mcp_module._GRAPH_ECHO_MAX_CHARS + 1


def test_cap_falls_back_to_minimal_when_lists_exhausted(mcp_module):
    """裁光清單仍超限(病態固定欄位)→ 回 minimal 物件,絕不回傳超限內容。"""
    import json

    resp = {
        "mode": "neighbors",
        "pathological_fixed_field": "x" * 20_000,  # 不在可裁清單內
        "edges": [{"e": 1}],
        "graph_status": "ok",
    }
    capped = mcp_module._cap_graph_response(resp)
    assert len(json.dumps(capped, ensure_ascii=False)) <= mcp_module._GRAPH_RESPONSE_MAX_CHARS
    assert "error" in capped and capped["truncated"] is True
