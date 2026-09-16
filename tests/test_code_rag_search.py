#!/usr/bin/env python3
"""CodeRAG 檢索層:code_rag_search 的回傳契約、CJK / 無 lexical 命中的檢索、bounded code
context、RepeatGuard(鬼打牆打斷)、canonical 語意表示式。全部離線。

合併自(2026-09-02):

- tests/test_code_rag_search_contract.py —— code_rag_search 的回傳契約(§8):預設 shape 完全
  不變(key-set 鎖死)、evidence 模式欄位、neighbors/path 模式、graph 缺席行為;GPT 審核修正
  (2026-08-19 二輪)的回歸;workflow F:graph 缺席時 mcp_server 不得預先清空 lexical_hits。
- tests/test_code_rag_cjk_retrieval.py —— 純 CJK / 無 lexical 命中的 query 回零結果,真實 bug
  regression(2026-08-21)。真實樹實測:「環境變數怎麼傳給子行程」回 0 筆,而語料裡有 environ.c、
  system.c 的 fork/execvp 這些該命中的東西。機制是算術的,不是偶發:``_extract_code_tokens``
  只抽 ``[A-Za-z_][A-Za-z0-9_]{2,}``,純中文問句抽出空集合 → 全語料 ``kw_score`` 皆 0 →
  fusion 的 ``0.5*emb + 0.5*kw`` 只剩一半 → 固定門檻 0.35 等於被悄悄抬成 emb ≥ 0.70
  (function 有 +0.05 bonus 則 ≥ 0.60)。實測全語料 emb 最大值 0.5112,combined 最大值 0.3027,
  結構上不可能有任何一筆通過。判準是「候選集有沒有實際 kw 命中」,不是「有沒有抽出 token」:
  中文夾英文技術詞(例:「VPX 的 DMA descriptor 怎麼設」)會抽出 token,但那些 token 在語料裡
  可能一個都沒命中,kw_score 照樣全 0,分數照樣被腰斬。
- tests/test_code_context.py —— bounded code evidence 的挑選、合併、安全與 char-budget;
  workflow F:lexical / index evidence 不依賴 graph,graph 缺席時必有 relationship uncertainty
  (唯一產生點常數,警語永不截斷)。
- tests/test_repeat_guard.py —— RepeatGuard(鬼打牆打斷)。觀察到的失敗模式(repeat.png,
  2026-08-19):本機小模型對同一組 codetrail_grep_code 參數連續呼叫六次,每次 thinking 40–70 秒,
  結果一模一樣卻停不下來。MCP 層對唯讀查詢工具掛 RepeatGuard:同工具+同參數+同結果連續出現時,
  在結果前面加打斷文字。結果每次重算重比:檔案變了 → 計數歸零、不加文字(絕不遮蔽新資訊)。
- tests/test_semantic_representation.py —— canonical 語意表示式的契約(施工規格 §6 P3A)。
  NEW SILENT CONTRACT,守的是 §3 洞 2:representation 被多個消費點各自截斷,而且截在不同地方,
  那種壞法完全無聲 —— index entry 在儲存時就把 context 截到 500,所以「把 embed text 從 400
  調大」是 no-op;leading comment 加進 embed text 但 lexical scorer 還在掃舊 context,於是
  lexical lane 永遠看不到註解訊號;reranker 自行重組 passage,欄位集合與另外兩個消費者不一致。
  所以這裡不只驗「有沒有拿到 comments」,還驗上游儲存上限必須大於等於下游預算 —— 那才是
  洞 2 的真正病灶。

smoke:本檔不用 module 層 pytestmark,逐條標記。原 cjk_retrieval / repeat_guard /
semantic_representation 是整檔 smoke,折進來後每一條各自帶 @pytest.mark.smoke;
semantic_representation 原本 module 層的 tree-sitter skipif 也逐條保留
(``_REQUIRES_TREE_SITTER_C``);其餘照原本的逐條標記。

各來源檔原本各自一份 autouse 的 ``_clean_scan_cache`` / ``_fresh_scan_cache``(清
``code_rag._INDEX_SCAN_CACHE``),已由 tests/conftest.py 的全域 autouse
``_isolate_code_rag_scan_cache`` 接手,本檔不再重複。
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest

from tests._harness import import_mcp_module

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ast_parser  # noqa: E402
import code_context  # noqa: E402
import code_rag  # noqa: E402
import config  # noqa: E402
from agent_tools import ToolExecutor  # noqa: E402
from code_context import EvidenceCandidate  # noqa: E402
from code_rag import hybrid_symbol_score  # noqa: E402
from config import CODE_RAG_THRESHOLD  # noqa: E402
from repeat_guard import BANNER_THRESHOLD, RepeatGuard, args_key, banner  # noqa: E402

# ── 原 test_code_rag_search_contract.py:code_rag_search 的回傳契約(§8)與 graph 缺席行為 ──

# 預設結果保留位置／分數並明示 build applicability；未選target不得假稱正在編譯。
REQUIRED_KEYS = {"path", "symbol", "type", "line", "score", "build_context", "build_state"}
OPTIONAL_KEYS = {"end_line", "parent"}
EVIDENCE_KEYS = {"score_components", "backend", "confidence", "relations", "graph_status"}
CONTEXT_KEYS = {
    "query", "evidence", "uncertainties", "seeds", "graph_status", "truncated",
    "budget_chars", "used_chars", "build_context",
}


@pytest.fixture
def mcp_module(monkeypatch, tmp_path: Path):
    """先在 root 放一組有明確 import 邊的來源,再以它當 AICODE_ROOT import mcp_server。"""
    (tmp_path / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    # 直接 name call:path mode 只走確定解析的邊,`util.helper()` 這種
    # attribute call 只有 heuristic confidence,本來就不該進呼叫鏈
    # (該行為的回歸測試在 tests/test_code_graph.py)。
    (tmp_path / "app.py").write_text(
        "from util import helper\n\n\ndef entry():\n    return helper()\n",
        encoding="utf-8")

    code_rag._INDEX_SCAN_CACHE.clear()
    mcp_server = import_mcp_module(monkeypatch, tmp_path)

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
    # 收集永久開啟(readonly 才關);這裡明確打開,不依賴同行程先前有沒有套過 readonly。
    monkeypatch.setattr(mcp_module.data_flywheel, "collect_enabled", lambda: True)
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
    # 檢索路徑跟著 telemetry 走,但一樣只有身分與分數,沒有程式碼文字(上面那條斷言也蓋到它)。
    trace = payload["extra_meta"]["trace"]
    assert trace["mode"] == "context" and trace["pool"]
    assert set(trace["pool"][0]) == {
        "path", "line", "symbol", "type", "emb", "lexical", "combined",
        "selected", "rerank", "final",
    }


@pytest.mark.smoke
def test_code_rag_trace_keeps_the_eliminated_pool_and_channel_scores(mcp_module, monkeypatch):
    """審核 BLOCKER 1:只存最後 top_k 的勝出者,看不出正解是沒被召回、沒過門檻,
    還是被 reranker 淘汰。trace 必須帶整個候選池(含落選者)、各通道分數
    (embedding / lexical / 融合 / rerank)與 embedding / reranker 設定。"""
    recorded = []
    monkeypatch.setattr(mcp_module.data_flywheel, "collect_enabled", lambda: True)
    monkeypatch.setattr(
        mcp_module, "_record_kb_interaction", lambda **kwargs: recorded.append(kwargs)
    )

    results = mcp_module.code_rag_search("entry helper", top_k=1)

    assert len(results) == 1
    trace = recorded[-1]["extra_meta"]["trace"]
    assert trace["stage"] == "done"
    pool = trace["pool"]
    assert len(pool) >= 2, "落選的候選也要在池裡"
    assert sum(1 for row in pool if row["final"]) == 1
    for row in pool:
        assert {"emb", "lexical", "combined", "selected", "rerank", "final"} <= set(row)
    assert trace["pool_total"] >= len(pool)
    assert trace["rerank"]["applied"] is False and trace["rerank"]["reason"]
    assert "embedding_model" in trace["settings"] and "reranker_model" in trace["settings"]
    assert trace["settings"]["threshold"] == CODE_RAG_THRESHOLD
    assert trace["files"], "最終結果的檔案要列出來給快照用"


@pytest.mark.smoke
def test_code_rag_search_records_a_failure_sample_when_retrieval_raises(mcp_module, monkeypatch):
    """審核 BLOCKER 3:reranker timeout 這種服務失敗以前完全不留紀錄。
    失敗也是樣本:要記問題、走到哪一步與失敗原因,而且例外照樣往外傳。"""
    recorded = []
    monkeypatch.setattr(mcp_module.data_flywheel, "collect_enabled", lambda: True)
    monkeypatch.setattr(
        mcp_module, "_record_kb_interaction", lambda **kwargs: recorded.append(kwargs)
    )

    def boom(*_args, **kwargs):
        trace = kwargs.get("trace")
        if trace is not None:
            trace["stage"] = "rerank"
        raise RuntimeError("reranker timeout")

    monkeypatch.setattr(mcp_module.CODE_RAG, "query_ranked", boom)

    # 直接呼叫工具函式時例外照樣往外傳(transport 那層才轉成 error result);
    # 紀錄要在它傳出去**之前**寫好。
    with pytest.raises(RuntimeError, match="reranker timeout"):
        mcp_module.code_rag_search("entry helper")

    assert recorded, "失敗也要留一筆"
    failure = recorded[-1]
    assert failure["extra_meta"]["failed"] is True
    assert failure["extra_meta"]["error_type"] == "RuntimeError"
    assert "reranker timeout" in failure["extra_meta"]["error"]
    assert failure["trace"]["stage"] == "rerank", "走到哪一步要保留"


@pytest.mark.smoke
def test_query_knowledge_records_a_failure_sample_with_the_partial_trace(mcp_module, monkeypatch):
    """審核 BLOCKER 3(KB 端):KB.query() 半途炸掉(例如 reranker 不可用)時,
    要把 KB 手上那份走到一半的 trace 一起記下來。"""
    recorded = []
    monkeypatch.setattr(mcp_module.data_flywheel, "collect_enabled", lambda: True)
    monkeypatch.setattr(
        mcp_module, "_record_kb_interaction", lambda **kwargs: recorded.append(kwargs)
    )
    monkeypatch.setattr(mcp_module, "_ensure_kb_fresh", lambda: None)
    monkeypatch.setattr(mcp_module.KB, "loaded", True)
    partial = {"schema": 1, "stage": "gate", "candidates": [{"id": "x"}]}

    def boom(*_args, **_kwargs):
        mcp_module.KB.last_trace = partial
        raise RuntimeError("RAG reranker unavailable")

    monkeypatch.setattr(mcp_module.KB, "query", boom)

    with pytest.raises(RuntimeError, match="reranker unavailable"):
        mcp_module.query_knowledge("哪裡設定 reset 值")

    failure = recorded[-1]
    assert failure["mode"] == "mcp_query_knowledge"
    assert failure["extra_meta"]["failed"] is True
    assert failure["extra_meta"]["error_type"] == "RuntimeError"
    assert failure["trace"] is partial


@pytest.mark.smoke
def test_context_mode_records_every_returned_evidence_file_for_snapshots(mcp_module, monkeypatch):
    """審核第三輪 B6:context 模式的 evidence 可以經 lexical / graph 補進不在 semantic 候選裡
    的檔;trace.files 在 semantic 結束時就定案,那些真的回傳的證據就沒有快照。"""
    recorded = []
    monkeypatch.setattr(mcp_module.data_flywheel, "collect_enabled", lambda: True)
    monkeypatch.setattr(
        mcp_module, "_record_kb_interaction", lambda **kwargs: recorded.append(kwargs)
    )

    [bundle] = mcp_module.code_rag_search("entry helper", mode="context", top_k=1)

    trace = recorded[-1]["extra_meta"]["trace"]
    evidence_paths = [item["path"] for item in bundle["evidence"]]
    assert evidence_paths, "fixture 一定有 evidence"
    assert [entry["path"] for entry in trace["context_evidence"]] == evidence_paths
    assert set(evidence_paths) <= {entry["path"] for entry in trace["files"]}


@pytest.mark.smoke
def test_a_failure_before_the_query_is_recorded_without_a_stale_trace(mcp_module, monkeypatch):
    """審核第三輪 B7:KB 重載失敗發生在 KB.query() 之前,try/except 沒包到,一筆都不留;
    而且 KB.last_trace 還是上一題的,不能拿來充數。"""
    recorded = []
    monkeypatch.setattr(mcp_module.data_flywheel, "collect_enabled", lambda: True)
    monkeypatch.setattr(
        mcp_module, "_record_kb_interaction", lambda **kwargs: recorded.append(kwargs)
    )
    mcp_module.KB.last_trace = {"stage": "done", "query": {"question": "上一題"}}

    def reload_fails():
        raise RuntimeError("knowledge.json reload failed")

    monkeypatch.setattr(mcp_module, "_ensure_kb_fresh", reload_fails)

    with pytest.raises(RuntimeError, match="reload failed"):
        mcp_module.query_knowledge("這一題")

    failure = recorded[-1]
    assert failure["extra_meta"]["failed"] is True
    assert failure["trace"]["stage"] == "load"
    assert failure["trace"]["query"]["question"] == "這一題"


@pytest.mark.smoke
def test_a_context_stage_failure_after_semantic_search_is_recorded(mcp_module, monkeypatch):
    """審核第三輪 B7:semantic 搜完之後 lexical 缺 rg 拋 DependencyError,新的 try/except
    只包了 query_ranked;失敗與已完成的 semantic 資訊一起消失。"""
    recorded = []
    monkeypatch.setattr(mcp_module.data_flywheel, "collect_enabled", lambda: True)
    monkeypatch.setattr(
        mcp_module, "_record_kb_interaction", lambda **kwargs: recorded.append(kwargs)
    )

    def no_rg(*_args, **_kwargs):
        raise mcp_module.DependencyError("rg is required")

    monkeypatch.setattr(mcp_module.code_context, "collect_safe_lexical_hits", no_rg)

    with pytest.raises(mcp_module.DependencyError, match="rg"):
        mcp_module.code_rag_search("entry helper", mode="context")

    failure = recorded[-1]
    assert failure["extra_meta"]["failed"] is True
    assert failure["trace"]["stage"] == "context"
    assert failure["trace"]["pool"], "semantic 已完成的候選池要留著"


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

    # context 降級:semantic + lexical evidence 仍在,只有呼叫關係證據缺席,
    # 不因 graph 壞掉整體失敗。(workflow F 唯一核准的既有斷言變更:原本要求
    # 全部 reason 皆為 semantic,現在允許另含 lexical,但 semantic 不得消失,
    # 且 relationship uncertainty 必須恰一條、文案精確。)
    [bundle] = mcp_module.code_rag_search("entry", mode="context", max_chars=2000)
    assert set(bundle) == CONTEXT_KEYS
    assert bundle["graph_status"].startswith("unavailable")
    assert bundle["evidence"]
    allowed_reasons = {
        "semantic", "lexical match", "lexical test candidate",
        "lexical header candidate", "lexical config candidate",
    }
    for item in bundle["evidence"]:
        assert set(item["reason"].split("; ")) <= allowed_reasons, item["reason"]
    seed_paths = {seed["path"] for seed in bundle["seeds"]}
    assert any(
        item["path"] in seed_paths and "semantic" in item["reason"].split("; ")
        for item in bundle["evidence"]
    ), bundle["evidence"]
    relationship = [
        row for row in bundle["uncertainties"]
        if row["target"] == code_context.RELATIONSHIP_UNAVAILABLE_TARGET
    ]
    assert len(relationship) == 1, bundle["uncertainties"]
    assert relationship[0]["reason"] == (
        code_context.RELATIONSHIP_UNAVAILABLE_REASON.format(category="unavailable")
    )


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


# ============================================================
# workflow F:graph 缺席時 mcp_server 不得預先清空 lexical_hits
# ============================================================
@pytest.fixture
def mcp_module_isolated(monkeypatch, tmp_path: Path):
    """同 `mcp_module`,但 HOME / USERPROFILE / XDG_CONFIG_HOME 全指向 tmp_path
    (SEAMS S-E:新測試不得讀真實 ~/.config);root 另開 `repo/`,因為
    mcp_server 會拒絕 AICODE_ROOT == $HOME。既有 fixture 不動。"""
    from tests._harness import seed_home

    # HOME 指到 tmp 的同時要放一份 deployment.json:設定只來自檔案,空的 HOME
    # 會讓 mcp_server 在 require_main_model() 掛掉。
    home = seed_home(tmp_path / "home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))

    root = tmp_path / "repo"
    root.mkdir()
    (root / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (root / "app.py").write_text(
        "from util import helper\n\n\ndef entry():\n    return helper()\n",
        encoding="utf-8")

    code_rag._INDEX_SCAN_CACHE.clear()
    mcp_server = import_mcp_module(monkeypatch, root)

    monkeypatch.setattr(code_rag, "USE_RERANKER", False)
    monkeypatch.setattr(mcp_server.CODE_RAG, "_get_embedding", lambda _t: [1.0, 0.0])
    monkeypatch.setattr(mcp_server.CODE_RAG, "_embed_texts_batched",
                        lambda texts: [[1.0, 0.0]] * len(texts))

    import code_graph as _code_graph

    _code_graph.CodeGraph(str(root)).build()

    yield mcp_server
    sys.modules.pop("mcp_server", None)
    code_rag._INDEX_SCAN_CACHE.clear()


@pytest.mark.smoke
def test_context_mode_keeps_lexical_hits_when_graph_missing(mcp_module_isolated):
    """graph 尚未建立時,只有 grep 才找得到的 config 檔仍要以 lexical evidence
    入選(mcp_server 以前在 graph None 時直接把 lexical_hits 設成 []),而且
    relationship uncertainty 恰一條;連跑兩輪確認沒有偷偷重建 graph。"""
    mcp_server = mcp_module_isolated
    root = Path(mcp_server.AICODE_ROOT)
    (root / "config").mkdir()
    (root / "config" / "app.cfg").write_text(
        "ZQXV_LEXICAL_ONLY_TOKEN=1\nentry_window=fast\n", encoding="utf-8"
    )
    code_rag.invalidate_scan_cache(root)

    for suffix in ("", "-wal", "-shm"):
        p = root / f".code_rag_graph.sqlite3{suffix}"
        if p.exists():
            p.unlink()
    mcp_server._CODE_GRAPH = None

    for _round in range(2):
        [bundle] = mcp_server.code_rag_search(
            "ZQXV_LEXICAL_ONLY_TOKEN entry", mode="context", max_chars=4000
        )
        # 只有 grep 找得到:config 檔沒有 symbol,不在 index 裡
        assert "config/app.cfg" not in {
            item["path"] for item in mcp_server.CODE_RAG.index
        }
        assert bundle["graph_status"].startswith("unavailable")
        cfg_rows = [item for item in bundle["evidence"] if item["path"] == "config/app.cfg"]
        assert len(cfg_rows) == 1, bundle["evidence"]
        assert "lexical config candidate" in set(cfg_rows[0]["reason"].split("; "))
        relationship = [
            row for row in bundle["uncertainties"]
            if row["target"] == code_context.RELATIONSHIP_UNAVAILABLE_TARGET
        ]
        assert len(relationship) == 1, bundle["uncertainties"]
        assert relationship[0]["reason"] == (
            code_context.RELATIONSHIP_UNAVAILABLE_REASON.format(category="unavailable")
        )
        assert not (root / ".code_rag_graph.sqlite3").exists()


# ── 原 test_code_rag_cjk_retrieval.py:純 CJK / 無 lexical 命中的 query 回零結果(2026-08-21 真實 bug regression) ──

# 落在「舊公式必定被濾掉、新公式必定通過」的區間:
# 舊 = 0.5*0.51 + 0.05 = 0.305 < 0.35;新 = 0.51 + 0.05 = 0.56 >= 0.35。
BAND_EMB = 0.51


# ============================================================
# 單元:融合公式
# ============================================================
@pytest.mark.smoke
def test_no_lexical_signal_is_dense_only():
    """純中文:沒有任何 lexical 訊號時,dense 分數不該被對半稀釋。"""
    combined, _rule = hybrid_symbol_score(
        emb_score=BAND_EMB, kw_score=0.0, item_type="function",
        is_explicit_mention=False, code_token_count=0,
        lexical_has_signal=False,
    )
    assert combined >= CODE_RAG_THRESHOLD, (
        f"combined={combined:.4f} 仍低於門檻 {CODE_RAG_THRESHOLD};"
        "純 CJK query 會結構性地回零結果"
    )


@pytest.mark.smoke
def test_tokens_extracted_but_nothing_matched_is_also_dense_only():
    """中文夾英文技術詞:抽得出 token,但語料裡一個都沒命中 —— 同樣不該稀釋。

    這是 code_token_count 判準修不到的情況(count>=1 卻無命中)。
    """
    combined, _rule = hybrid_symbol_score(
        emb_score=BAND_EMB, kw_score=0.0, item_type="function",
        is_explicit_mention=False, code_token_count=3,
        lexical_has_signal=False,
    )
    assert combined >= CODE_RAG_THRESHOLD, (
        f"combined={combined:.4f};有 token 但零命中時仍被腰斬"
    )


@pytest.mark.smoke
@pytest.mark.parametrize("emb", [0.0, 0.31, 0.62, 1.0])
@pytest.mark.parametrize("kw", [0.0, 0.4, 0.79])
@pytest.mark.parametrize("item_type", ["function", "class", "variable"])
@pytest.mark.parametrize("count", [1, 2, 5])
def test_with_lexical_signal_is_identical_to_the_old_formula(emb, kw, item_type, count):
    """恆等性:只要有 lexical 命中,分數必須與舊公式逐位元相同。

    這條專門守住「不影響現有含英文字的 query」—— core / target 那些題的
    名次不得因為這次修改而漂移。
    """
    combined, rule = hybrid_symbol_score(
        emb_score=emb, kw_score=kw, item_type=item_type,
        is_explicit_mention=False, code_token_count=count,
        lexical_has_signal=True,
    )
    type_bonus = 0.05 if item_type == "function" else 0.0
    assert rule == "fusion"
    assert combined == pytest.approx(0.5 * emb + 0.5 * kw + type_bonus, abs=0.0)


@pytest.mark.smoke
def test_explicit_and_lexical_dominant_rules_are_untouched():
    """兩條捷徑規則不得因為這次修改而改變。"""
    combined, rule = hybrid_symbol_score(
        emb_score=0.0, kw_score=0.0, item_type="class",
        is_explicit_mention=True, code_token_count=1,
        lexical_has_signal=True,
    )
    assert (combined, rule) == (0.95, "explicit_symbol")

    combined, rule = hybrid_symbol_score(
        emb_score=0.0, kw_score=0.9, item_type="class",
        is_explicit_mention=False, code_token_count=2,
        lexical_has_signal=True,
    )
    assert rule == "lexical_dominant"
    assert combined == pytest.approx(0.9 + 0.9 * 0.1)


# ============================================================
# 整合:端對端 query
# ============================================================
def _band_rag(monkeypatch, root: Path, question: str) -> code_rag.CodeRAG:
    """離線 CodeRAG,且讓 query↔symbol 的 cosine 剛好落在 BAND_EMB。"""
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", False)
    monkeypatch.setattr(code_rag, "USE_RERANKER", False)
    q_vec = [1.0, 0.0]
    item_vec = [BAND_EMB, math.sqrt(1.0 - BAND_EMB ** 2)]

    rag = code_rag.CodeRAG(str(root))
    monkeypatch.setattr(
        rag, "_get_embedding",
        lambda text: q_vec if text == question else item_vec,
    )
    monkeypatch.setattr(
        rag, "_embed_texts_batched", lambda texts: [list(item_vec)] * len(texts)
    )
    return rag


@pytest.mark.smoke
def test_pure_cjk_query_returns_hits(monkeypatch, tmp_path):
    """端對端:純中文問句必須撈得到東西。"""
    (tmp_path / "mod.py").write_text(
        "def pass_environment_to_child():\n    return 1\n", encoding="utf-8"
    )
    question = "環境變數怎麼傳給子行程"
    rag = _band_rag(monkeypatch, tmp_path, question)

    hits = rag.query(question, top_k=5)
    assert hits, "純中文 query 回零結果"


@pytest.mark.smoke
def test_cjk_with_unmatched_ascii_token_returns_hits(monkeypatch, tmp_path):
    """端對端:中文夾一個語料裡沒有的英文詞,同樣必須撈得到。"""
    (tmp_path / "mod.py").write_text(
        "def pass_environment_to_child():\n    return 1\n", encoding="utf-8"
    )
    question = "環境變數怎麼傳給 zzzznotpresent"
    rag = _band_rag(monkeypatch, tmp_path, question)

    assert rag._extract_code_tokens(question), "前提:這題必須抽得出 ASCII token"
    hits = rag.query(question, top_k=5)
    assert hits, "有 token 但零命中的 query 回零結果"


@pytest.mark.smoke
def test_type_bonus_only_applies_when_there_is_lexical_signal():
    """純語意 query 不得給 function 平白的 +0.05 —— 那個 bonus 會壓掉 global。

    真實樹實測(2026-08-21):「環境變數怎麼傳給子行程」的答案
    ``environ.c::_environ`` 是 global,dense 排名 369,但 combined 排名 7793 ——
    中間那 7400 名幾乎都是靠 +0.05 插隊的 function。全語料 emb 只落在
    0.35-0.51 這條很窄的帶上,0.05 在這裡不是「一點優先權」而是決定性的。
    有 lexical 訊號時維持原樣(上面 108 組參數的恆等性測試守住)。
    """
    func, _rule = hybrid_symbol_score(
        emb_score=BAND_EMB, kw_score=0.0, item_type="function",
        is_explicit_mention=False, code_token_count=0, lexical_has_signal=False,
    )
    glob, _rule = hybrid_symbol_score(
        emb_score=BAND_EMB, kw_score=0.0, item_type="global",
        is_explicit_mention=False, code_token_count=0, lexical_has_signal=False,
    )
    assert func == pytest.approx(glob), (
        f"function={func:.4f} global={glob:.4f};同樣的 cosine 下 function 仍被加分"
    )
    assert func == pytest.approx(BAND_EMB), "無 lexical 訊號時 combined 就該等於 emb"


# ── 原 test_code_context.py:bounded code evidence 的挑選、合併、安全與 char-budget ──

@pytest.mark.parametrize("value", [1999, 30001, True, False, 12000.0, "12000"])
def test_max_chars_rejects_out_of_range_and_non_integer(value):
    with pytest.raises(ValueError, match="2000..30000"):
        code_context.validate_max_chars(value)


@pytest.mark.parametrize("value", [2000, 12000, 28000, 30000])
def test_max_chars_accepts_documented_range(value):
    assert code_context.validate_max_chars(value) == value


def test_two_character_chinese_query_term_is_kept_but_stop_words_are_filtered():
    assert code_context.query_terms("中斷") == ["中斷"]
    assert code_context.query_terms("哪個") == []


def test_overlapping_ranges_merge_and_preserve_reasons():
    merged = code_context.merge_candidate_ranges([
        EvidenceCandidate("src/a.c", 10, 30, "seed", ("semantic",), 100, 12),
        EvidenceCandidate("src/a.c", 25, 45, "callee", ("confirmed callee",), 88, 27),
        EvidenceCandidate("src/a.c", 70, 80, "other", ("lexical match",), 70, 72),
    ])
    assert len(merged) == 2
    assert (merged[0].start_line, merged[0].end_line) == (10, 45)
    assert merged[0].symbol == "seed"
    assert merged[0].reasons == ("semantic", "confirmed callee")


def _fake_numbered_read(path: str, start: int, end: int, width: int = 96) -> str:
    lines = [f"{line:4d} | {path}:{'x' * width}" for line in range(start, end + 1)]
    return (
        f"=== {path} (行 {start}-{end} / 共 500 行) ===\n"
        + "\n".join(lines)
        + f"\n... 用 read_file('{path}', {end + 1}) 繼續"
    )


def test_pack_respects_actual_text_budget_and_reports_omissions():
    calls = []

    def read_window(path, start, end):
        calls.append((path, start, end))
        return _fake_numbered_read(path, start, end)

    bundle = code_context.build_code_context(
        query="large evidence",
        semantic_items=[
            {"path": "src/a.c", "symbol": "a", "line": 1, "end_line": 40},
            {"path": "src/b.c", "symbol": "b", "line": 1, "end_line": 40},
        ],
        index_items=[],
        allowed_paths={"src/a.c", "src/b.c"},
        read_window=read_window,
        max_chars=2000,
        graph=None,
        graph_status="unavailable",
    )

    actual = sum(len(item["text"]) for item in bundle["evidence"])
    assert bundle["used_chars"] == actual <= 2000
    assert bundle["truncated"] is True
    assert len(calls) > len(bundle["evidence"]), "oversized windows should be safely re-read smaller"
    assert any("budget omitted" in item["reason"] for item in bundle["uncertainties"])


def test_identical_source_content_is_deduplicated():
    def same_content(path, start, end):
        return (
            f"=== {path} (行 1-2 / 共 2 行) ===\n"
            "   1 | int same(void) {\n"
            "   2 |     return 1;\n"
        )

    bundle = code_context.build_code_context(
        query="same",
        semantic_items=[
            {"path": "a.c", "symbol": "same", "line": 1, "end_line": 2},
            {"path": "b.c", "symbol": "same", "line": 1, "end_line": 2},
        ],
        index_items=[],
        allowed_paths={"a.c", "b.c"},
        read_window=same_content,
        max_chars=2000,
        graph=None,
        graph_status="unavailable",
    )
    assert len(bundle["seeds"]) == 2
    assert len(bundle["evidence"]) == 1


def test_safe_lexical_hits_support_config_and_filter_unscoped_paths(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "config").mkdir()
    (root / "ignored").mkdir()
    (root / "config/layout.cfg").write_text(
        "HANDOFF_REGION=BOOT_FAST\n", encoding="utf-8"
    )
    (root / "ignored/secret.cfg").write_text(
        "HANDOFF_REGION=SECRET\n", encoding="utf-8"
    )
    executor = ToolExecutor(str(root))

    hits = code_context.collect_safe_lexical_hits(
        executor,
        "latency critical interrupt region",
        allowed_paths={"config/layout.cfg"},
    )
    assert {hit["path"] for hit in hits} == {"config/layout.cfg"}
    assert all("text" not in hit for hit in hits), "grep source text must not leak into metadata"


def test_lexical_hit_parser_does_not_treat_colon_digits_in_match_text_as_line_number(
    tmp_path,
):
    root = tmp_path / "repo"
    source = root / "src/value.c"
    source.parent.mkdir(parents=True)
    source.write_text("int target = 123;\n", encoding="utf-8")
    executor = ToolExecutor(str(root))
    executor.grep = lambda *args, **kwargs: (
        f"=== rg 'target' (1 matches) ===\n{source}:1:value:123: target"
    )

    assert code_context.collect_safe_lexical_hits(
        executor, "target", {"src/value.c"}
    ) == [{"path": "src/value.c", "line": 1, "terms": ["target"]}]


@pytest.mark.skipif(os.name == "nt", reason="Windows filenames cannot contain colon")
def test_lexical_hit_parser_accepts_safe_path_containing_colon_digits(tmp_path):
    root = tmp_path / "repo"
    source = root / "src/part:123:value.c"
    source.parent.mkdir(parents=True)
    source.write_text("int target = 1;\n", encoding="utf-8")
    executor = ToolExecutor(str(root))
    executor.grep = lambda *args, **kwargs: f"{source}:1:int target = 1;"

    assert code_context.collect_safe_lexical_hits(
        executor, "target", {"src/part:123:value.c"}
    ) == [{"path": "src/part:123:value.c", "line": 1, "terms": ["target"]}]


@pytest.mark.skipif(os.name == "nt", reason="Windows filenames cannot contain colon")
def test_lexical_hit_parser_prefers_longest_scoped_colon_digit_path(tmp_path):
    root = tmp_path / "repo"
    prefix = root / "src/prefix"
    source = root / "src/prefix:123:value.c"
    source.parent.mkdir(parents=True)
    prefix.write_text("prefix decoy\n", encoding="utf-8")
    source.write_text("int target = 1;\n", encoding="utf-8")
    executor = ToolExecutor(str(root))
    executor.grep = lambda *args, **kwargs: f"{source}:1:int target = 1;"

    assert code_context.collect_safe_lexical_hits(
        executor,
        "target",
        {"src/prefix", "src/prefix:123:value.c"},
    ) == [{"path": "src/prefix:123:value.c", "line": 1, "terms": ["target"]}]


def test_candidate_limit_is_not_reported_as_character_budget_exhaustion():
    semantic = [
        {"path": f"src/f{i}.c", "symbol": f"f{i}", "line": 1, "end_line": 1}
        for i in range(code_context._MAX_CANDIDATES + 5)
    ]

    def read_window(path, start, end):
        return f"=== {path} (行 1-1 / 共 1 行) ===\n   1 | int {Path(path).stem}(void);\n"

    bundle = code_context.build_code_context(
        query="functions",
        semantic_items=semantic,
        index_items=[],
        allowed_paths={item["path"] for item in semantic},
        read_window=read_window,
        max_chars=30000,
        graph=None,
        graph_status="unavailable",
    )
    reasons = [row["reason"] for row in bundle["uncertainties"]]
    assert bundle["truncated"] is True
    assert any("candidate limit" in reason for reason in reasons)
    assert not any("character budget" in reason for reason in reasons)


def test_uncertainties_are_deduplicated_and_bounded_with_explicit_marker():
    rows = [
        {"target": f"target_{i}", "reason": "unresolved"}
        for i in range(code_context._MAX_UNCERTAINTIES + 20)
    ]
    bounded = code_context._dedupe_uncertainties(rows)
    assert len(bounded) == code_context._MAX_UNCERTAINTIES
    assert "uncertainty limit" in bounded[-1]["reason"]


def test_graph_traversal_truncation_is_reported_as_uncertainty():
    class TruncatedGraph:
        def find_nodes(self, name, limit=20):
            return [{"id": "seed", "path": "src/a.c", "start_line": 1}]

        def neighbors(self, *args, **kwargs):
            return {"nodes": [], "edges": [], "truncated": True}

        def file_neighbors(self, *args, **kwargs):
            return {"files": ["src/a.c"], "edges": [], "truncated": True}

    bundle = code_context.build_code_context(
        query="seed",
        semantic_items=[{"path": "src/a.c", "symbol": "seed", "line": 1}],
        index_items=[],
        allowed_paths={"src/a.c"},
        read_window=lambda path, start, end: (
            f"=== {path} (行 1-1 / 共 1 行) ===\n   1 | int seed(void);\n"
        ),
        max_chars=2000,
        graph=TruncatedGraph(),
    )
    assert any("graph traversal limit" in row["reason"]
               for row in bundle["uncertainties"])


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink containment")
def test_source_windows_cannot_follow_symlink_outside_sandbox(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.c"
    outside.write_text("int secret(void) { return 7; }\n", encoding="utf-8")
    os.symlink(outside, root / "linked.c")
    executor = ToolExecutor(str(root))
    calls = []

    def safe_read(path, start, end):
        calls.append((path, start, end))
        return executor.read_file(path, start_line=start, end_line=end)

    bundle = code_context.build_code_context(
        query="secret",
        semantic_items=[
            {"path": "linked.c", "symbol": "secret", "line": 1, "end_line": 1}
        ],
        index_items=[],
        # Even a compromised candidate catalog cannot bypass the final safe read.
        allowed_paths={"linked.c"},
        read_window=safe_read,
        max_chars=2000,
        graph=None,
        graph_status="unavailable",
    )
    assert calls == [("linked.c", 1, 5)]
    assert bundle["evidence"] == []
    assert bundle["used_chars"] == 0
    assert any("safe source read unavailable" in row["reason"]
               for row in bundle["uncertainties"])


def test_binary_disguised_as_source_is_rejected_by_safe_reader(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "bad.c").write_bytes(b"\x00\x01\x02not-source")
    executor = ToolExecutor(str(root))
    bundle = code_context.build_code_context(
        query="bad",
        semantic_items=[{"path": "bad.c", "symbol": "bad", "line": 1}],
        index_items=[],
        allowed_paths={"bad.c"},
        read_window=lambda path, start, end: executor.read_file(
            path, start_line=start, end_line=end
        ),
        max_chars=2000,
        graph=None,
        graph_status="unavailable",
    )
    assert bundle["evidence"] == []
    assert bundle["used_chars"] == 0
    assert bundle["uncertainties"]


def test_failed_budget_shrink_read_is_not_reported_as_budget_omission():
    calls = 0

    def read_window(path, start, end):
        nonlocal calls
        calls += 1
        if calls == 1:
            return (
                f"=== {path} (行 1-100 / 共 100 行) ===\n"
                + "".join(f"{line:4d} | int value_{line};\n" for line in range(1, 101))
            )
        return "錯誤:縮小後讀取失敗"

    bundle = code_context.build_code_context(
        query="value",
        semantic_items=[{
            "path": "src/large.c", "symbol": "value", "line": 1, "end_line": 100,
        }],
        index_items=[],
        allowed_paths={"src/large.c"},
        read_window=read_window,
        max_chars=2000,
        graph=None,
        graph_status="unavailable",
    )

    reasons = [row["reason"] for row in bundle["uncertainties"]]
    assert any("safe source read unavailable" in reason for reason in reasons)
    assert not any("character budget omitted" in reason for reason in reasons)


def test_unscoped_index_items_never_reach_read_callback():
    calls = []
    bundle = code_context.build_code_context(
        query="secret",
        semantic_items=[
            {"path": "node_modules/secret.c", "symbol": "secret", "line": 1}
        ],
        index_items=[
            {"path": "node_modules/secret.c", "symbol": "secret", "line": 1,
             "context": "secret"}
        ],
        allowed_paths={"src/good.c"},
        read_window=lambda *args: calls.append(args) or "should not be read",
        max_chars=2000,
        graph=None,
        graph_status="unavailable",
    )
    assert bundle["seeds"] == []
    assert bundle["evidence"] == []
    assert calls == []


def _stub_graph(edge: dict, related: dict):
    class StubGraph:
        def find_nodes(self, name, limit=20):
            return [{"id": "seed", "path": "src/a.py", "start_line": 1,
                     "end_line": 3, "name": "seed"}]

        def neighbors(self, *args, **kwargs):
            return {
                "nodes": [
                    {"id": "seed", "path": "src/a.py", "start_line": 1,
                     "end_line": 3, "name": "seed"},
                    related,
                ],
                "edges": [edge],
                "truncated": False,
            }

        def file_neighbors(self, *args, **kwargs):
            return {"files": ["src/a.py"], "edges": [], "truncated": False}

    return StubGraph()


def _context_with_edge(edge: dict):
    related = {"id": "callee", "path": "src/b.py", "start_line": 1,
               "end_line": 3, "name": "target"}
    return code_context.build_code_context(
        query="seed",
        semantic_items=[{"path": "src/a.py", "symbol": "seed", "line": 1}],
        index_items=[],
        allowed_paths={"src/a.py", "src/b.py"},
        read_window=lambda path, start, end: (
            f"=== {path} (行 1-1 / 共 1 行) ===\n   1 | def {Path(path).stem}(): ...\n"
        ),
        max_chars=4000,
        graph=_stub_graph(edge, related),
    )


def test_heuristic_call_edge_is_not_confirmed_evidence():
    """resolved 但 confidence=heuristic(Python attribute call 猜同名)。"""
    bundle = _context_with_edge({
        "src_id": "seed", "dst_id": "callee", "dst_name": "target",
        "resolved": True, "confidence": "heuristic",
        "ambiguity_group": None, "type": "calls",
    })
    reasons = [row["reason"] for row in bundle["evidence"]]
    assert any("heuristic callee candidate" in reason for reason in reasons), reasons
    assert not any("confirmed callee" in reason for reason in reasons), reasons
    assert any("not confirmed evidence" in row["reason"]
               for row in bundle["uncertainties"]), bundle["uncertainties"]


def test_exact_call_edge_stays_confirmed_evidence():
    bundle = _context_with_edge({
        "src_id": "seed", "dst_id": "callee", "dst_name": "target",
        "resolved": True, "confidence": "exact",
        "ambiguity_group": None, "type": "calls",
    })
    reasons = [row["reason"] for row in bundle["evidence"]]
    assert any("confirmed callee" in reason for reason in reasons), reasons
    assert not any("not confirmed evidence" in row["reason"]
                   for row in bundle["uncertainties"]), bundle["uncertainties"]


# ---------------------------------------------------------------------------
# workflow F:lexical / index evidence 不依賴 graph;graph 缺席時必有
# relationship uncertainty(唯一產生點常數,警語永不截斷)。
# ---------------------------------------------------------------------------
def _numbered_window(path: str, start: int, end: int) -> str:
    lines = [f"{line:4d} | {path}: line {line}" for line in range(start, end + 1)]
    return f"=== {path} (行 {start}-{end} / 共 400 行) ===\n" + "\n".join(lines)


_F_SEMANTIC = [{"path": "src/boot.c", "symbol": "boot_init", "line": 10, "end_line": 20}]
_F_INDEX = [
    {"path": "src/boot.c", "symbol": "boot_init", "line": 10, "end_line": 20,
     "context": "void boot_init(void)"},
    {"path": "tests/test_boot.c", "symbol": "test_boot_init_handoff", "line": 5,
     "end_line": 12, "context": "boot_init handoff region check"},
]
_F_GREP_HITS = [{"path": "config/board.cfg", "line": 3, "terms": ["boot_init", "handoff"]}]
_F_ALLOWED = {"src/boot.c", "tests/test_boot.c", "config/board.cfg"}


def _f_bundle(**overrides):
    kwargs = dict(
        query="boot_init handoff region",
        semantic_items=_F_SEMANTIC,
        index_items=_F_INDEX,
        allowed_paths=_F_ALLOWED,
        read_window=_numbered_window,
        max_chars=8000,
        lexical_hits=_F_GREP_HITS,
    )
    kwargs.update(overrides)
    return code_context.build_code_context(**kwargs)


def _reason_tokens_by_path(bundle: dict) -> dict[str, set[str]]:
    return {item["path"]: set(item["reason"].split("; ")) for item in bundle["evidence"]}


def _relationship_rows(bundle: dict) -> list[dict]:
    return [
        row for row in bundle["uncertainties"]
        if row["target"] == code_context.RELATIONSHIP_UNAVAILABLE_TARGET
    ]


@pytest.mark.smoke
def test_graph_none_keeps_index_lexical_and_grep_evidence_and_reports_relationship_uncertainty():
    bundle = _f_bundle(graph=None, graph_status="unavailable")

    tokens = _reason_tokens_by_path(bundle)
    assert "semantic" in tokens["src/boot.c"]
    assert "lexical test candidate" in tokens["tests/test_boot.c"], tokens
    assert "lexical config candidate" in tokens["config/board.cfg"], tokens
    assert bundle["graph_status"] == "unavailable"

    rows = _relationship_rows(bundle)
    assert len(rows) == 1, bundle["uncertainties"]
    assert rows[0]["reason"] == code_context.RELATIONSHIP_UNAVAILABLE_REASON.format(
        category="unavailable"
    )

    # graph 正常(不拋例外)時:同一組輸入不加 relationship uncertainty,
    # lexical evidence 也一樣在。
    ok_graph = _stub_graph(
        {"src_id": "seed", "dst_id": "callee", "dst_name": "target",
         "resolved": True, "confidence": "exact",
         "ambiguity_group": None, "type": "calls"},
        {"id": "callee", "path": "src/b.py", "start_line": 1, "end_line": 3,
         "name": "target"},
    )
    ok_bundle = _f_bundle(graph=ok_graph, graph_status="ok")
    assert ok_bundle["graph_status"] == "ok"
    assert _relationship_rows(ok_bundle) == []
    assert {item["path"] for item in ok_bundle["evidence"]} == {
        item["path"] for item in bundle["evidence"]
    }


@pytest.mark.smoke
def test_graph_exception_degrades_but_lexical_evidence_survives_with_bounded_uncertainty():
    class ExplodingGraph:
        def find_nodes(self, name, limit=20):
            raise RuntimeError("graph lookup exploded " + "x" * 400)

        def neighbors(self, *args, **kwargs):  # pragma: no cover - find_nodes 先炸
            raise AssertionError("neighbors must not be reached")

        def file_neighbors(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("file_neighbors must not be reached")

    bundle = _f_bundle(graph=ExplodingGraph(), graph_status="ok")

    assert bundle["graph_status"].startswith("degraded: RuntimeError")
    assert len(bundle["graph_status"]) <= 200

    tokens = _reason_tokens_by_path(bundle)
    assert "semantic" in tokens["src/boot.c"]
    assert "lexical test candidate" in tokens["tests/test_boot.c"], tokens
    assert "lexical config candidate" in tokens["config/board.cfg"], tokens

    rows = _relationship_rows(bundle)
    assert len(rows) == 1, bundle["uncertainties"]
    expected = code_context.RELATIONSHIP_UNAVAILABLE_REASON.format(category="degraded")
    assert rows[0]["reason"] == expected
    assert rows[0]["reason"].endswith("未看到 caller/callee 不代表不存在")


@pytest.mark.smoke
def test_graph_none_with_default_status_is_reported_as_unavailable():
    bundle = _f_bundle(
        index_items=[], lexical_hits=(), allowed_paths={"src/boot.c"}, graph=None,
    )
    assert bundle["graph_status"] == "unavailable"
    rows = _relationship_rows(bundle)
    assert len(rows) == 1, bundle["uncertainties"]
    assert rows[0]["reason"] == code_context.RELATIONSHIP_UNAVAILABLE_REASON.format(
        category="unavailable"
    )


# ── 原 test_repeat_guard.py:RepeatGuard(鬼打牆打斷) ──
# smoke:AGENTS.md §1.1 第 1 款「真實發生過的 bug 的 regression」
# 真實 bug regression(repeat.png 2026-08-19):鬼打牆打斷。原檔整檔 smoke,這裡逐條標。

# ---------------------------------------------------------------------------
# 單元:計數 / 歸零 / LRU
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_observe_counts_identical_results():
    g = RepeatGuard()
    assert g.observe("grep_code", "k1", "same") == 1
    assert g.observe("grep_code", "k1", "same") == 2
    assert g.observe("grep_code", "k1", "same") == 3


@pytest.mark.smoke
def test_observe_resets_when_result_changes():
    g = RepeatGuard()
    assert g.observe("grep_code", "k1", "old") == 1
    assert g.observe("grep_code", "k1", "old") == 2
    # 檔案被改了 → 結果不同 → 歸零重計,不會被標成重複
    assert g.observe("grep_code", "k1", "new") == 1
    assert g.observe("grep_code", "k1", "new") == 2


@pytest.mark.smoke
def test_interleaved_keys_tracked_independently():
    """repeat.png 的實際形狀:A、B 兩組參數交替連打,各自都要被抓到。"""
    g = RepeatGuard()
    assert g.observe("grep_code", "A", "ra") == 1
    assert g.observe("grep_code", "B", "rb") == 1
    assert g.observe("grep_code", "A", "ra") == 2
    assert g.observe("grep_code", "B", "rb") == 2
    assert g.observe("grep_code", "A", "ra") == 3


@pytest.mark.smoke
def test_lru_cap_evicts_oldest():
    g = RepeatGuard(max_keys=2)
    g.observe("t", "k1", "r")
    g.observe("t", "k2", "r")
    g.observe("t", "k3", "r")  # 擠掉 k1
    assert g.observe("t", "k1", "r") == 1  # k1 已被淘汰 → 重新從 1 起算


@pytest.mark.smoke
def test_args_key_is_order_insensitive():
    assert args_key((), {"a": 1, "b": 2}) == args_key((), {"b": 2, "a": 1})
    assert args_key((), {"a": 1}) != args_key((), {"a": 2})


@pytest.mark.smoke
def test_banner_mentions_tool_and_count():
    text = banner("grep_code", 3)
    assert "grep_code" in text and "3" in text
    assert "重複" in text


# ---------------------------------------------------------------------------
# 整合:MCP tool 層(fresh import mcp_server,同 test_analyze_file_sandbox)
# ---------------------------------------------------------------------------
@pytest.fixture
def mcp_module_plain(monkeypatch, tmp_path: Path):
    """以 tmp_path 當 AICODE_ROOT 重新 import mcp_server(細節見 _harness)。

    與本檔的 `mcp_module` 不同:root 不預放檔案、不建 code graph,收尾也不從
    sys.modules 拔 module —— 這是原 test_repeat_guard.py 的定義,原樣保留。
    """
    return import_mcp_module(monkeypatch, tmp_path)


def _tool_fn(mcp_module_plain, name: str):
    tool = getattr(mcp_module_plain, name)
    return getattr(tool, "fn", tool)


@pytest.mark.smoke
def test_grep_code_repeat_gets_banner(mcp_module_plain, tmp_path: Path):
    (tmp_path / "a.py").write_text("needle = 1\n", encoding="utf-8")
    grep = _tool_fn(mcp_module_plain, "grep_code")

    out1 = grep(pattern="needle", path=".", include="*.py", context=0)
    assert "重複呼叫" not in out1

    out2 = grep(pattern="needle", path=".", include="*.py", context=0)
    assert "重複呼叫" in out2, out2
    # 打斷文字加在最前面,原結果仍完整保留在後
    assert out2.endswith(out1), "banner 必須前置,不能改動原結果"


@pytest.mark.smoke
def test_grep_code_banner_clears_when_file_changes(mcp_module_plain, tmp_path: Path):
    (tmp_path / "a.py").write_text("needle = 1\n", encoding="utf-8")
    grep = _tool_fn(mcp_module_plain, "grep_code")

    grep(pattern="needle", path=".", include="*.py", context=0)
    out2 = grep(pattern="needle", path=".", include="*.py", context=0)
    assert "重複呼叫" in out2

    # 檔案變了 → 同參數的下一次呼叫結果不同 → 不能再標重複
    (tmp_path / "b.py").write_text("needle = 2\n", encoding="utf-8")
    out3 = grep(pattern="needle", path=".", include="*.py", context=0)
    assert "重複呼叫" not in out3, out3


@pytest.mark.smoke
def test_different_args_do_not_trigger_banner(mcp_module_plain, tmp_path: Path):
    (tmp_path / "a.py").write_text("needle = 1\nhay = 2\n", encoding="utf-8")
    grep = _tool_fn(mcp_module_plain, "grep_code")
    assert "重複呼叫" not in grep(pattern="needle", path=".", include="*.py", context=0)
    assert "重複呼叫" not in grep(pattern="hay", path=".", include="*.py", context=0)


@pytest.mark.smoke
def test_read_file_repeat_gets_banner(mcp_module_plain, tmp_path: Path):
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    read_file = _tool_fn(mcp_module_plain, "read_file")
    read_file(path="f.txt")
    out2 = read_file(path="f.txt")
    assert "重複呼叫" in out2, out2


@pytest.mark.smoke
def test_threshold_is_two():
    """第一次重複(第 2 次呼叫)就要打斷 — 六次才打斷等於沒修。"""
    assert BANNER_THRESHOLD == 2


# ── 原 test_semantic_representation.py:canonical 語意表示式的契約(§6 P3A) ──
# 原檔 module 層 pytestmark = [smoke, skipif(tree-sitter c 未安裝)];折進來後兩者都逐條掛。
_REQUIRES_TREE_SITTER_C = pytest.mark.skipif(
    not ast_parser.HAS_TREE_SITTER
    or not ast_parser._try_load_tree_sitter_language("c"),
    reason="tree-sitter c 未安裝",
)

COMMENTED_C = (
    "/* SPDX-License-Identifier: MIT */\n"
    "\n"
    "/* Generated configuration reviewed when retry policy changes. */\n"
    "#define CONFIG_GENERATION 7u\n"
    "\n"
    "/* trailing note that belongs to the macro above */\n"
    "\n"
    "/** Guards the calibration fallback path. */\n"
    "static int g_calibration_flag;\n"
    "\n"
    "int plain_symbol(void) { return 0; }\n"
)


def _entries(tmp_path: Path) -> dict:
    source = tmp_path / "src" / "fw.c"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(COMMENTED_C, encoding="utf-8")
    rag = code_rag.CodeRAG(str(tmp_path))
    rows, _embeddings = rag._index_single_file(
        source, "src/fw.c", compute_embeddings=False
    )
    return rag, {row["symbol"]: row for row in rows}


# ============================================================
# leading comment 邊界
# ============================================================
@pytest.mark.smoke
@_REQUIRES_TREE_SITTER_C
def test_leading_comment_association_respects_its_four_boundaries(tmp_path: Path):
    _rag, entries = _entries(tmp_path)

    macro = entries["CONFIG_GENERATION"]["comments"]
    assert "Generated configuration reviewed" in macro
    assert "SPDX" not in macro, "檔頭 license 不得掛到第一個 symbol 身上"

    flag = entries["g_calibration_flag"]["comments"]
    assert "Guards the calibration fallback path" in flag
    assert "trailing note" not in flag, "跨空行的上一個 symbol 尾註不得被撈進來"

    assert "comments" not in entries["plain_symbol"], "沒有註解就不要編一個出來"


# ============================================================
# 三個消費者都要看得到(不能有人靜默看不到)
# ============================================================
@pytest.mark.smoke
@_REQUIRES_TREE_SITTER_C
def test_leading_comment_enters_embed_text(tmp_path: Path):
    rag, entries = _entries(tmp_path)
    embed_text = rag._build_embed_text(entries["CONFIG_GENERATION"])
    assert "Generated configuration reviewed" in embed_text


@pytest.mark.smoke
@_REQUIRES_TREE_SITTER_C
def test_leading_comment_is_visible_to_the_lexical_scorer(tmp_path: Path):
    """lexical lane 也要吃得到註解 —— 只放進 embed text 等於半條鏈沒接上。"""
    rag, entries = _entries(tmp_path)
    entry = entries["CONFIG_GENERATION"]

    scan_text = code_rag.lexical_scan_text(
        entry, config.CODE_RAG_LEXICAL_SCAN_MAX_CHARS
    )
    assert "Generated configuration reviewed" in scan_text

    # 註解裡才有的詞必須真的推高分數,而不是只是出現在字串裡。
    tokens = rag._extract_code_tokens("generated configuration reviewed retry policy")
    assert rag._token_match_score(tokens, entry) > 0.0

    without_comment = {k: v for k, v in entry.items() if k != "comments"}
    assert rag._token_match_score(tokens, entry) > \
        rag._token_match_score(tokens, without_comment)


@pytest.mark.smoke
@_REQUIRES_TREE_SITTER_C
def test_every_consumer_projects_the_same_canonical_field_set(tmp_path: Path):
    """三個消費者的字串不必相同,但欄位集合必須同一份來源。"""
    _rag, entries = _entries(tmp_path)
    entry = entries["g_calibration_flag"]
    labels = {label for label, _text in code_rag.semantic_fields(entry)}

    assert "linkage:" in labels, "P2 抽到的 linkage 要進表示式,不能只躺在 index 裡"
    rendered = code_rag.render_semantic_fields(entry, 10_000)
    assert "Guards the calibration fallback path" in rendered
    assert "linkage: internal" in rendered
    # canonical 順序:識別資訊在前,context 在最後(超預算時先被截的是它)。
    assert code_rag.CANONICAL_SEMANTIC_FIELDS[-1] == "context"
    assert code_rag.CANONICAL_SEMANTIC_FIELDS.index("comments") < \
        code_rag.CANONICAL_SEMANTIC_FIELDS.index("context")


# ============================================================
# 預算:上游儲存上限必須 >= 下游預算(§3 洞 2 的真正病灶)
# ============================================================
@pytest.mark.smoke
@_REQUIRES_TREE_SITTER_C
def test_storage_cap_is_not_smaller_than_downstream_budgets():
    """儲存端截得比消費端小的話,調大消費端預算全部是 no-op。"""
    assert config.CODE_RAG_CONTEXT_STORE_MAX_CHARS >= \
        config.CODE_RAG_EMBED_TEXT_MAX_CHARS, (
            "index entry 的 context 上限比 embed text 預算小 —— "
            "調大 embed 預算會是 no-op(這正是 §3 洞 2)"
        )
    assert config.CODE_RAG_CONTEXT_STORE_MAX_CHARS >= \
        config.CODE_RAG_LEXICAL_SCAN_MAX_CHARS
    assert config.CODE_RAG_CONTEXT_STORE_MAX_CHARS >= \
        config.CODE_RERANK_PASSAGE_MAX_CHARS


@pytest.mark.smoke
@_REQUIRES_TREE_SITTER_C
def test_consumer_budgets_are_independent_knobs():
    """storage cap 與 reranker cap 分開,不是同一個常數改名。"""
    names = (
        "CODE_RAG_CONTEXT_STORE_MAX_CHARS",
        "CODE_RAG_EMBED_TEXT_MAX_CHARS",
        "CODE_RAG_LEXICAL_SCAN_MAX_CHARS",
        "CODE_RERANK_PASSAGE_MAX_CHARS",
    )
    for name in names:
        assert isinstance(getattr(config, name), int)
        assert getattr(config, name) > 0


@pytest.mark.smoke
@_REQUIRES_TREE_SITTER_C
def test_render_truncates_from_the_tail_and_keeps_identity_fields():
    """超預算時砍的是 context,不是 symbol / signature / comments。"""
    entry = {
        "path": "src/fw.c",
        "type": "function",
        "symbol": "calibration_load",
        "signature": "int calibration_load(const unsigned *c, unsigned n)",
        "comments": "/* Exercise calibration fallback when storage is blank. */",
        "context": "X" * 5000,
    }
    rendered = code_rag.render_semantic_fields(entry, 300)
    assert len(rendered) <= 320  # 每個欄位之間的分隔空白
    assert "calibration_load" in rendered
    assert "Exercise calibration fallback" in rendered
    assert rendered.count("X") < 5000
