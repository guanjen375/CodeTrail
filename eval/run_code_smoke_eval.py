#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Code smoke eval — 16 題,離線、零 server 接觸的 Code-RAG/graph 回歸 harness。

離線保證(三道,缺一不可):
  1. embedding 取得路徑被 monkeypatch 成 SHA-256(normalized text) 展開的
     決定性 pseudo-vector(跨 process 穩定;絕不用 Python hash())。
  2. reranker 以 ``code_rag.USE_RERANKER = False`` 關閉 —— 注意 code_rag 頂部是
     snapshot import,改 ``config.USE_RERANKER`` 無效。
  3. Poison session:``llama_client.get_session`` 與 ``http_client.get_session``
     都被換成「任何操作都 raise」的物件。任何漏網 HTTP request 都會讓 runner
     立刻失敗 —— 這本身就是「零 server 接觸」的斷言。

模式:
  --record-baseline   把現況數字寫進 eval/fixtures/code_smoke/baseline.json
                      (不 gate;Step 0 用)
  (預設)             依 §3.5 判準 gate,任一組不過 exit 1
  --with-servers      手動 warm-latency 冒煙(真 dense+rerank;server 不在
                      graceful skip;永不入 gate)

fixture 不攜帶 validation_command;評分全部在本檔內建決定性計算。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURE_DIR = Path(__file__).with_name("fixtures") / "code_smoke"
CASES_FILE = FIXTURE_DIR / "cases.json"
BASELINE_FILE = FIXTURE_DIR / "baseline.json"

PSEUDO_EMBED_DIM = 64

# --with-servers 手動模式的固定問題(字面值常數,不從 fixture 讀)。
WARM_LATENCY_QUERIES = [
    "push an event into the event queue",
    "where is sm_transition implemented",
    "register a callback for an event id",
]


# ============================================================
# 離線 stubs
# ============================================================
def pseudo_embedding(text: str, dim: int = PSEUDO_EMBED_DIM) -> list[float]:
    """SHA-256(normalized text) 展開成固定維度向量。決定性、跨 process 穩定。"""
    normalized = " ".join(text.split())
    seed = hashlib.sha256(normalized.encode("utf-8")).digest()
    values: list[float] = []
    counter = 0
    while len(values) < dim:
        block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        for i in range(0, len(block) - 3, 4):
            if len(values) >= dim:
                break
            word = int.from_bytes(block[i : i + 4], "big")
            values.append((word / 2**32) * 2.0 - 1.0)
        counter += 1
    return values


class _PoisonSession:
    """任何屬性存取都 raise:離線 runner 絕不允許真的發 HTTP。"""

    def __getattr__(self, name):
        raise RuntimeError(
            f"offline smoke eval attempted HTTP (session.{name}); "
            "this run must never touch a server"
        )


def _poison_get_session():
    return _PoisonSession()


def install_offline_stubs() -> None:
    import code_rag
    import http_client
    import llama_client

    # Poison 兩個入口:llama_client 已用 from-import 存了自己的 reference,
    # 只 patch http_client.get_session 無效;兩個都 patch 防未來新 call site。
    llama_client.get_session = _poison_get_session
    http_client.get_session = _poison_get_session

    code_rag.USE_RERANKER = False  # snapshot import,必須 patch code_rag 這份

    def _stub_embed_one(*, base_url: str, content: str, model: str = "", timeout: int = 60):
        return pseudo_embedding(content)

    def _stub_embed_batch(*, base_url: str, contents: list[str], model: str = "",
                          timeout: int = 300):
        return [pseudo_embedding(c) for c in contents]

    llama_client.embed_one = _stub_embed_one
    llama_client.embed_batch = _stub_embed_batch


# ============================================================
# fixture 載入
# ============================================================
def load_cases() -> dict:
    data = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    cases = data["cases"]
    if len(cases) != 16:
        raise RuntimeError(f"expected 16 smoke cases, got {len(cases)}")
    return data


def copy_fixture_repos(data: dict, workdir: Path) -> dict[str, Path]:
    """把 fixture repo 複製到 tmpdir 執行,避免 cache/graph 檔污染 checked-in fixture。"""
    roots: dict[str, Path] = {}
    for name, info in data["repos"].items():
        src = FIXTURE_DIR / info["root"]
        if not src.is_dir():
            raise RuntimeError(f"fixture repo missing: {src}")
        dst = workdir / name
        shutil.copytree(src, dst)
        roots[name] = dst
    return roots


# ============================================================
# 檢索題(localization / tree_sitter / abstain)
# ============================================================
def build_rags(roots: dict[str, Path]) -> dict:
    from code_rag import CodeRAG

    rags = {}
    for name, root in roots.items():
        rag = CodeRAG(str(root))
        rag.build_index(verbose=False)
        rags[name] = rag
    return rags


def eval_retrieval_case(case: dict, rags: dict) -> dict:
    rag = rags[case["repo"]]
    results = rag.query(case["question"], top_k=5)
    paths = [r.get("path", "").replace("\\", "/") for r in results]
    symbols = [r.get("symbol", "") for r in results]
    gold_files = [p.replace("\\", "/") for p in case.get("gold_files", [])]
    gold_symbols = case.get("gold_symbols", [])
    return {
        "id": case["id"],
        "results": [
            {"path": p, "symbol": s} for p, s in zip(paths, symbols)
        ],
        "file_hit": bool(gold_files) and any(p in gold_files for p in paths),
        "symbol_hit": bool(gold_symbols) and any(s in gold_symbols for s in symbols),
        "empty": len(results) == 0,
    }


# ============================================================
# graph 題
# ============================================================
def load_graph_backend():
    """code_graph 缺席(Step 4 之前)回 None;評分端把 graph 題標 unavailable。"""
    try:
        import code_graph
    except ImportError:
        return None
    return code_graph


def build_graphs(code_graph_mod, roots: dict[str, Path]) -> dict:
    graphs = {}
    for name, root in roots.items():
        g = code_graph_mod.CodeGraph(str(root))
        g.build(verbose=False)
        graphs[name] = g
    return graphs


def eval_includes_case(case: dict, graphs: dict) -> dict:
    g = graphs[case["repo"]]
    got = set(
        f"{case['target_file']}->{dst}" for dst in g.file_includes(case["target_file"])
    )
    gold = set(case["gold_edges"])
    return {"id": case["id"], "got": sorted(got), "gold": sorted(gold), "pass": got == gold}


def eval_call_path_case(case: dict, graphs: dict, true_edges: set[str]) -> dict:
    g = graphs[case["repo"]]
    src, _, dst = case["question"].partition("->")
    src, dst = src.strip(), dst.strip()
    paths = g.shortest_evidence_paths({src}, {dst}, max_hops=4, limit=3)
    name_paths = []
    invalid = 0
    for path in paths:
        names = [path[0]["src_name"]] + [e["dst_name"] for e in path]
        name_paths.append(names)
        for edge in path:
            if f"{edge['src_name']}->{edge['dst_name']}" not in true_edges:
                invalid += 1

    resolved_edges = {
        f"{e['src_name']}->{e['dst_name']}" for e in g.iter_call_edges() if e["resolved"]
    }
    missing_gold_edges = [e for e in case["gold_edges"] if e not in resolved_edges]
    gold_path_hit = any(list(gp) in name_paths for gp in case["gold_paths"])
    return {
        "id": case["id"],
        "paths": name_paths,
        "missing_gold_edges": missing_gold_edges,
        "gold_path_hit": gold_path_hit,
        "invalid_path_steps": invalid,
        "pass": not missing_gold_edges and gold_path_hit and invalid == 0,
    }


def eval_fp_case(case: dict, graphs: dict) -> dict:
    g = graphs[case["repo"]]
    src = case["source_symbol"]
    callees = g.callees(src)
    unresolved = [e for e in callees if not e["resolved"]]
    resolved_names = sorted({e["dst_name"] for e in callees if e["resolved"]})
    gold_unresolved_targets = {e.split("->", 1)[1] for e in case["gold_edges"]}
    has_unresolved = any(
        (e.get("unresolved_target") or "") in gold_unresolved_targets for e in unresolved
    )
    # 「不得錯誤 resolve」:src 的 resolved callee 只能是 fixture 真實的邊,
    # 不得把 function-pointer 目標掛到某個真函式上。
    wrongly_resolved = [
        n for n in resolved_names if f"{src}->{n}" not in _TRUE_EDGES_BY_REPO[case["repo"]]
    ]
    return {
        "id": case["id"],
        "unresolved_found": has_unresolved,
        "wrongly_resolved": wrongly_resolved,
        "pass": has_unresolved and not wrongly_resolved,
    }


def eval_graph_precision(graphs: dict, repos_info: dict) -> dict:
    extracted = []
    for name, g in graphs.items():
        for e in g.iter_call_edges():
            if e["resolved"]:
                extracted.append((name, f"{e['src_name']}->{e['dst_name']}"))
    if not extracted:
        return {"precision": 0.0, "extracted": 0, "correct": 0}
    correct = sum(
        1 for name, edge in extracted if edge in set(repos_info[name]["true_call_edges"])
    )
    return {
        "precision": correct / len(extracted),
        "extracted": len(extracted),
        "correct": correct,
    }


_TRUE_EDGES_BY_REPO: dict[str, set[str]] = {}


# ============================================================
# 主流程
# ============================================================
def run_offline(record_baseline: bool) -> int:
    install_offline_stubs()

    data = load_cases()
    for name, info in data["repos"].items():
        _TRUE_EDGES_BY_REPO[name] = set(info["true_call_edges"])

    from ast_parser import get_parser_status

    parser_status = get_parser_status()

    summary: dict = {"cases": {}}
    with tempfile.TemporaryDirectory(prefix="code_smoke_") as tmp:
        workdir = Path(tmp)
        roots = copy_fixture_repos(data, workdir)
        rags = build_rags(roots)

        code_graph_mod = load_graph_backend()
        graphs = {}
        graph_available = code_graph_mod is not None
        graph_error = ""
        if graph_available:
            try:
                graphs = build_graphs(code_graph_mod, roots)
            except Exception as exc:  # graph build 失敗要如實呈現,不是 crash
                graph_available = False
                graph_error = f"{type(exc).__name__}: {exc}"

        loc_cases = [c for c in data["cases"] if c["task_type"] == "localization" and not c["requires"]]
        ts_cases = [c for c in data["cases"] if c["requires"] == ["tree_sitter"]]
        inc_cases = [c for c in data["cases"] if c["task_type"] == "includes"]
        path_cases = [
            c for c in data["cases"]
            if c["task_type"] == "call_path" and not c.get("expected_unresolved")
        ]
        fp_cases = [c for c in data["cases"] if c.get("expected_unresolved")]
        abstain_cases = [c for c in data["cases"] if c.get("must_abstain")]

        # --- localization ---
        loc_results = [eval_retrieval_case(c, rags) for c in loc_cases]
        file_recall = sum(r["file_hit"] for r in loc_results) / len(loc_results)
        symbol_recall = sum(r["symbol_hit"] for r in loc_results) / len(loc_results)

        # --- tree_sitter(exact symbol 命中)---
        ts_results = [eval_retrieval_case(c, rags) for c in ts_cases]
        ts_found = sum(r["symbol_hit"] for r in ts_results)

        # --- abstain ---
        abstain_results = [eval_retrieval_case(c, rags) for c in abstain_cases]
        abstain_ok = all(r["empty"] for r in abstain_results)

        # --- graph 題 ---
        if graph_available:
            inc_results = [eval_includes_case(c, graphs) for c in inc_cases]
            path_results = [
                eval_call_path_case(c, graphs, _TRUE_EDGES_BY_REPO[c["repo"]])
                for c in path_cases
            ]
            fp_results = [eval_fp_case(c, graphs) for c in fp_cases]
            precision = eval_graph_precision(graphs, data["repos"])
            for g in graphs.values():
                g.close()
        else:
            inc_results = [{"id": c["id"], "pass": False, "unavailable": True} for c in inc_cases]
            path_results = [{"id": c["id"], "pass": False, "unavailable": True} for c in path_cases]
            fp_results = [{"id": c["id"], "pass": False, "unavailable": True} for c in fp_cases]
            precision = {"precision": 0.0, "extracted": 0, "correct": 0, "unavailable": True}

    summary.update(
        {
            "parser_status": parser_status,
            "graph_available": graph_available,
            "graph_error": graph_error,
            "localization": {
                "file_recall_at_5": round(file_recall, 4),
                "symbol_recall_at_5": round(symbol_recall, 4),
                "cases": loc_results,
            },
            "tree_sitter": {"found": ts_found, "total": len(ts_cases), "cases": ts_results},
            "includes": {"cases": inc_results},
            "call_path": {"cases": path_results, "precision": precision},
            "function_pointer": {"cases": fp_results},
            "abstain": {"ok": abstain_ok, "cases": abstain_results},
        }
    )

    print("=== code smoke eval (offline) ===")
    print(f"parser: tree_sitter={parser_status.get('has_tree_sitter')}  graph={graph_available}")
    print(
        f"localization: file_recall@5={file_recall:.3f} symbol_recall@5={symbol_recall:.3f} "
        f"({len(loc_results)} cases)"
    )
    print(f"tree_sitter: {ts_found}/{len(ts_cases)} exact symbol found")
    for r in inc_results:
        print(f"includes {r['id']}: {'PASS' if r['pass'] else 'FAIL' + (' (graph unavailable)' if r.get('unavailable') else '')}")
    for r in path_results:
        print(f"call_path {r['id']}: {'PASS' if r['pass'] else 'FAIL' + (' (graph unavailable)' if r.get('unavailable') else '')}")
    print(
        "calls precision: "
        f"{precision['precision']:.3f} ({precision['correct']}/{precision['extracted']})"
    )
    for r in fp_results:
        print(f"function_pointer {r['id']}: {'PASS' if r['pass'] else 'FAIL'}")
    print(f"abstain: {'PASS' if abstain_ok else 'FAIL'}")

    if record_baseline:
        baseline = {
            "recorded_with": {
                "tree_sitter": bool(parser_status.get("has_tree_sitter")),
                "graph_available": graph_available,
            },
            "localization": {
                "file_recall_at_5": round(file_recall, 4),
                "symbol_recall_at_5": round(symbol_recall, 4),
            },
            "tree_sitter_found": ts_found,
            "abstain_ok": abstain_ok,
        }
        BASELINE_FILE.write_text(
            json.dumps(baseline, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"baseline written to {BASELINE_FILE}")
        return 0

    # ---- gate 模式(§3.5) ----
    if not BASELINE_FILE.exists():
        print("FAIL: baseline.json 不存在;先跑 --record-baseline", file=sys.stderr)
        return 1
    baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    failures: list[str] = []

    base_loc = baseline["localization"]
    if file_recall < base_loc["file_recall_at_5"]:
        failures.append(
            f"localization file_recall@5 {file_recall:.3f} < baseline {base_loc['file_recall_at_5']}"
        )
    if symbol_recall < base_loc["symbol_recall_at_5"]:
        failures.append(
            f"localization symbol_recall@5 {symbol_recall:.3f} < baseline {base_loc['symbol_recall_at_5']}"
        )
    if ts_found != len(ts_cases):
        failures.append(f"tree_sitter {ts_found}/{len(ts_cases)} (要求全數命中)")
    for r in inc_results:
        if not r["pass"]:
            failures.append(f"includes {r['id']} 集合不相等或 graph 缺席")
    for r in path_results:
        if not r["pass"]:
            failures.append(f"call_path {r['id']} 未達判準")
    if precision.get("unavailable") or precision["precision"] < 0.8:
        failures.append(f"calls precision {precision['precision']:.3f} < 0.8")
    for r in fp_results:
        if not r["pass"]:
            failures.append(f"function_pointer {r['id']} 未回報 unresolved 或誤 resolve")
    if not abstain_ok:
        failures.append("abstain 題誤答")

    if failures:
        print("\n=== GATE FAIL ===")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("\n=== GATE PASS ===")
    return 0


def run_with_servers() -> int:
    """手動 warm-latency 冒煙:真 dense+rerank,3 次取中位數。永不入 gate。"""
    import llama_client
    from config import LLAMA_EMBED_BASE_URL

    if not llama_client.is_ready(LLAMA_EMBED_BASE_URL):
        print(f"SKIP: embedding server not ready at {LLAMA_EMBED_BASE_URL}")
        return 0

    data = load_cases()
    with tempfile.TemporaryDirectory(prefix="code_smoke_srv_") as tmp:
        workdir = Path(tmp)
        roots = copy_fixture_repos(data, workdir)
        from code_rag import CodeRAG

        rag = CodeRAG(str(roots["ism_mini"]))
        rag.build_index(verbose=False)  # cache warm(build 完成後才量)
        for query in WARM_LATENCY_QUERIES:
            timings = []
            for _ in range(3):
                start = time.perf_counter()
                rag.query(query, top_k=5)
                timings.append(time.perf_counter() - start)
            print(f"warm latency p50={statistics.median(timings) * 1000:.1f}ms  {query!r}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-baseline", action="store_true",
                        help="記錄現況到 baseline.json(不 gate)")
    parser.add_argument("--with-servers", action="store_true",
                        help="手動 warm-latency 模式(真 server;不入 gate)")
    args = parser.parse_args(argv)

    if args.with_servers:
        return run_with_servers()
    return run_offline(record_baseline=args.record_baseline)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
