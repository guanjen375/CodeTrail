#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Code inference smoke eval — 舊 16 題 + 20 core + 2 stretch 的離線 harness。

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
  --with-servers      manual real-model lane(真 dense+rerank;server 不在
                      graceful skip;永不入 CI gate)

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
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURE_DIR = Path(__file__).with_name("fixtures") / "code_smoke"
CASES_FILE = FIXTURE_DIR / "cases.json"
BASELINE_FILE = FIXTURE_DIR / "baseline.json"

PSEUDO_EMBED_DIM = 64
LEGACY_CASE_COUNT = 16
BLOCKING_CORE_COUNT = 20
MAX_STRETCH_CASES = 4
CONTEXT_BUDGETS = (12_000, 28_000)
CORE_FAMILIES = (
    "code2test",
    "trace2code",
    "edit2ripple",
    "firmware_semantics",
    "selective_retrieval",
)

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


@contextmanager
def install_offline_stubs():
    """Temporarily install deterministic offline clients and always restore them.

    The eval also runs inside the normal pytest process.  Mutating these module
    globals without a finally block makes later endpoint/reranker tests observe
    poison clients and a disabled reranker when tests run in a single shard.
    """
    import code_rag
    import http_client
    import llama_client

    originals = {
        (llama_client, "get_session"): llama_client.get_session,
        (http_client, "get_session"): http_client.get_session,
        (llama_client, "embed_one"): llama_client.embed_one,
        (llama_client, "embed_batch"): llama_client.embed_batch,
        (code_rag, "USE_RERANKER"): code_rag.USE_RERANKER,
    }

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

    try:
        yield
    finally:
        for (module, name), value in originals.items():
            setattr(module, name, value)


def _with_offline_stubs(func):
    """Keep the CLI runner's large body inside the scoped stub lifecycle."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        with install_offline_stubs():
            return func(*args, **kwargs)

    return wrapped


# ============================================================
# fixture 載入
# ============================================================
def load_cases() -> dict:
    data = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    cases = data["cases"]
    ids = [case.get("id") for case in cases]
    if len(ids) != len(set(ids)):
        raise RuntimeError("code smoke case ids must be unique")
    if any("validation_command" in case for case in cases):
        raise RuntimeError("fixtures must not contain executable validation_command fields")
    legacy = [case for case in cases if str(case.get("id", "")).startswith("smoke_")]
    core = [case for case in cases if case.get("blocking") is True]
    stretch = [case for case in cases if str(case.get("id", "")).startswith("stretch_")]
    if len(legacy) != LEGACY_CASE_COUNT:
        raise RuntimeError(f"expected {LEGACY_CASE_COUNT} legacy cases, got {len(legacy)}")
    if len(core) != BLOCKING_CORE_COUNT:
        raise RuntimeError(f"expected {BLOCKING_CORE_COUNT} blocking core cases, got {len(core)}")
    family_counts = {
        family: sum(case.get("family") == family and case.get("blocking") is True for case in core)
        for family in CORE_FAMILIES
    }
    if any(count != 4 for count in family_counts.values()):
        raise RuntimeError(f"blocking core must contain four cases per family: {family_counts}")
    if not 1 <= len(stretch) <= MAX_STRETCH_CASES:
        raise RuntimeError(f"expected 1..{MAX_STRETCH_CASES} stretch cases, got {len(stretch)}")
    for case in cases:
        if case.get("repo") not in data.get("repos", {}):
            raise RuntimeError(f"case {case.get('id')} references unknown repo")
        if not isinstance(case.get("question"), str) or not case["question"].strip():
            raise RuntimeError(f"case {case.get('id')} has no question")
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


def retrieval_metrics(results: list[dict], gold_files: list[str]) -> dict:
    """純計算的 file Recall@5 / MRR；供 runner 與單元測試共用。"""
    normalized_gold = {path.replace("\\", "/") for path in gold_files}
    ranked_paths = [item.get("path", "").replace("\\", "/") for item in results[:5]]
    hits = normalized_gold.intersection(ranked_paths)
    reciprocal_rank = 0.0
    for rank, path in enumerate(ranked_paths, start=1):
        if path in normalized_gold:
            reciprocal_rank = 1.0 / rank
            break
    return {
        "file_recall_at_5": len(hits) / len(normalized_gold) if normalized_gold else 0.0,
        "mrr": reciprocal_rank,
        "hit_files": sorted(hits),
    }


def _workflow_ranked_items(case: dict, rag) -> list[dict]:
    # CI structural lane:用 CodeRAG 自己的 lexical view 排序 parser 產出的 symbols。
    # pseudo embedding 只在舊 smoke/hybrid plumbing 路徑使用，不能拿隨機向量的
    # threshold hit/miss 冒充真實 semantic 品質。
    tokens = rag._extract_code_tokens(case["question"])
    ranked = sorted(
        (
            (rag._token_match_score(tokens, item), item)
            for item in rag.index
        ),
        key=lambda pair: (-pair[0], pair[1].get("path", ""), pair[1].get("line", 0)),
    )
    return [dict(item, score=float(score)) for score, item in ranked if score > 0][:5]


def eval_workflow_retrieval_case(case: dict, rags: dict) -> dict:
    rag = rags[case["repo"]]
    raw_results = _workflow_ranked_items(case, rag)
    compact = [
        {
            "path": item.get("path", "").replace("\\", "/"),
            "symbol": item.get("symbol", ""),
        }
        for item in raw_results
    ]
    metrics = retrieval_metrics(compact, case.get("seed_files", case.get("gold_files", [])))
    return {
        "id": case["id"],
        "family": case["family"],
        "blocking": bool(case.get("blocking")),
        "lane": "ci_structural_lexical",
        "results": compact,
        **metrics,
        "seed_hit": bool(metrics["hit_files"]),
    }


def summarize_retrieval_families(results: list[dict]) -> dict[str, dict]:
    summary: dict[str, dict] = {}
    for family in sorted({item["family"] for item in results}):
        members = [item for item in results if item["family"] == family]
        summary[family] = {
            "cases": len(members),
            "file_recall_at_5": round(
                sum(item["file_recall_at_5"] for item in members) / len(members), 4
            ),
            "mrr": round(sum(item["mrr"] for item in members) / len(members), 4),
            "seed_hits": sum(item["seed_hit"] for item in members),
        }
    return summary


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


def _source_edges(graph, path: str, symbol: str) -> tuple[list[dict], str | None]:
    nodes = [node for node in graph.find_nodes(symbol) if node["path"] == path]
    if len(nodes) != 1:
        return [], f"source {path}:{symbol} resolved to {len(nodes)} nodes"
    node_id = nodes[0]["id"]
    neighborhood = graph.neighbors(
        node_id, edge_types=("calls",), direction="out", hops=1, limit=100
    )
    return [edge for edge in neighborhood["edges"] if edge["src_id"] == node_id], None


def eval_graph_check(check: dict, graph) -> dict:
    kind = check["kind"]
    if kind == "include":
        got = graph.file_includes(check["source_path"])
        ok = check["target_path"] in got
        return {"kind": kind, "pass": ok, "got": got, "expected": check["target_path"]}
    if kind == "no_path":
        paths = graph.shortest_evidence_paths(
            {check["source_symbol"]}, {check["target_symbol"]}, max_hops=4, limit=3
        )
        return {"kind": kind, "pass": not paths, "paths": paths}

    edges, error = _source_edges(graph, check["source_path"], check["source_symbol"])
    if error:
        return {"kind": kind, "pass": False, "error": error}
    if kind == "no_resolved_calls":
        resolved = [edge for edge in edges if edge["resolved"]]
        return {"kind": kind, "pass": not resolved, "resolved": resolved}
    target = check["target_symbol"]
    if kind == "unresolved":
        matching = [
            edge for edge in edges
            if not edge["resolved"] and edge.get("unresolved_target") == target
        ]
        return {"kind": kind, "pass": bool(matching), "matching": matching}
    if kind == "ambiguous":
        matching = [
            edge for edge in edges
            if not edge["resolved"] and edge.get("ambiguity_group")
            and (edge.get("unresolved_target") == target or edge.get("dst_name") == target)
        ]
        groups = {edge["ambiguity_group"] for edge in matching}
        return {
            "kind": kind,
            "pass": len(matching) >= 2 and len(groups) == 1,
            "matching": matching,
        }
    if kind == "resolved":
        target_nodes = [
            node for node in graph.find_nodes(target)
            if node["path"] == check["target_path"]
        ]
        target_ids = {node["id"] for node in target_nodes}
        matching = [
            edge for edge in edges if edge["resolved"] and edge.get("dst_id") in target_ids
        ]
        return {"kind": kind, "pass": bool(matching), "matching": matching}
    return {"kind": kind, "pass": False, "error": "unknown graph check kind"}


def eval_graph_invariant_case(case: dict, graphs: dict) -> dict:
    graph = graphs[case["repo"]]
    checks = [eval_graph_check(check, graph) for check in case["graph_checks"]]
    return {
        "id": case["id"],
        "family": case["family"],
        "checks": checks,
        "pass": all(check["pass"] for check in checks),
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
    expected = {
        (name, edge)
        for name, info in repos_info.items()
        for edge in info["true_call_edges"]
    }
    extracted_set = set(extracted)
    return {
        "precision": correct / len(extracted),
        "representable_recall": len(extracted_set & expected) / len(expected) if expected else 1.0,
        "extracted": len(extracted),
        "correct": correct,
        "representable": len(expected),
        "false_resolved": sorted(f"{name}:{edge}" for name, edge in extracted_set - expected),
        "missing_representable": sorted(f"{name}:{edge}" for name, edge in expected - extracted_set),
    }


_TRUE_EDGES_BY_REPO: dict[str, set[str]] = {}


def budgeted_context_metrics(bundle: dict, gold_files: list[str]) -> dict:
    """以純字元計算 context coverage/precision；不聲稱 tokenizer token 數。"""
    evidence = bundle.get("evidence", []) if isinstance(bundle, dict) else []
    budget_chars = int(bundle.get("budget_chars", 0)) if isinstance(bundle, dict) else 0
    actual_used = sum(len(str(item.get("text", ""))) for item in evidence)
    declared_used = int(bundle.get("used_chars", actual_used)) if isinstance(bundle, dict) else 0
    evidence_files = {str(item.get("path", "")).replace("\\", "/") for item in evidence}
    gold = {path.replace("\\", "/") for path in gold_files}
    hits = evidence_files & gold
    return {
        "budget_chars": budget_chars,
        "used_chars": actual_used,
        "declared_used_chars": declared_used,
        "within_budget": actual_used <= budget_chars and declared_used == actual_used,
        "gold_file_coverage": len(hits) / len(gold) if gold else 1.0,
        "evidence_precision": len(hits) / len(evidence_files) if evidence_files else 0.0,
        "hit_files": sorted(hits),
        "truncated": bool(bundle.get("truncated")) if isinstance(bundle, dict) else False,
    }


def unavailable_context_report(cases: list[dict]) -> dict:
    return {
        "available": False,
        "reason": "code_context bounded bundle is not implemented yet",
        "budgets": {
            str(budget): {
                "budget_chars": budget,
                "used_chars": 0,
                "gold_file_coverage": 0.0,
                "evidence_precision": 0.0,
                "truncation_cases": 0,
                "cases": len(cases),
            }
            for budget in CONTEXT_BUDGETS
        },
    }


class _CountingExecutor:
    """Eval-only wrapper:count the grep text a multi-round agent would receive."""

    def __init__(self, executor):
        self._executor = executor
        self.root = executor.root
        self.grep_chars = 0

    def _safe_path(self, path):
        return self._executor._safe_path(path)

    def grep(self, *args, **kwargs):
        output = self._executor.grep(*args, **kwargs)
        self.grep_chars += len(output) if isinstance(output, str) else 0
        return output


def evaluate_context_report(cases: list[dict], rags: dict, graphs: dict,
                            roots: dict[str, Path]) -> dict:
    """Run bounded context locally; all source text still goes through ToolExecutor."""
    try:
        import code_context
        from agent_tools import ToolExecutor
    except ImportError as exc:
        report = unavailable_context_report(cases)
        report["reason"] = f"{type(exc).__name__}: {exc}"
        return report

    budgets = {}
    for budget in CONTEXT_BUDGETS:
        case_results = []
        total_gold = total_hits = total_evidence_files = 0
        total_context_chars = total_multi_round_chars = 0
        truncation_cases = 0
        all_within_budget = True
        for case in cases:
            rag = rags[case["repo"]]
            graph = graphs.get(case["repo"])
            allowed_paths = set(rag._scan_code_files())
            executor = ToolExecutor(str(roots[case["repo"]]))
            counting = _CountingExecutor(executor)
            lexical_hits = (
                code_context.collect_safe_lexical_hits(
                    counting, case["question"], allowed_paths
                )
                if graph is not None else []
            )
            bundle = code_context.build_code_context(
                query=case["question"],
                semantic_items=_workflow_ranked_items(case, rag),
                index_items=rag.index,
                allowed_paths=allowed_paths,
                read_window=lambda path, start, end, _executor=executor: _executor.read_file(
                    path, start_line=start, end_line=end
                ),
                max_chars=budget,
                graph=graph,
                graph_status="ok" if graph is not None else "unavailable",
                lexical_hits=lexical_hits,
            )
            metrics = budgeted_context_metrics(bundle, case.get("gold_files", []))
            metrics["id"] = case["id"]
            metrics["multi_round_chars"] = metrics["used_chars"] + counting.grep_chars
            case_results.append(metrics)

            gold = {path.replace("\\", "/") for path in case.get("gold_files", [])}
            total_gold += len(gold)
            total_hits += len(set(metrics["hit_files"]) & gold)
            total_evidence_files += len({
                item.get("path", "") for item in bundle.get("evidence", [])
            })
            total_context_chars += metrics["used_chars"]
            total_multi_round_chars += metrics["multi_round_chars"]
            truncation_cases += int(metrics["truncated"])
            all_within_budget = all_within_budget and metrics["within_budget"]

        budgets[str(budget)] = {
            "budget_chars": budget,
            "used_chars": total_context_chars,
            "max_case_used_chars": max((row["used_chars"] for row in case_results), default=0),
            "within_budget": all_within_budget,
            "gold_file_coverage": total_hits / total_gold if total_gold else 1.0,
            "evidence_precision": total_hits / total_evidence_files
            if total_evidence_files else 0.0,
            "multi_round_chars": total_multi_round_chars,
            "saved_chars": total_multi_round_chars - total_context_chars,
            "truncation_cases": truncation_cases,
            "cases": len(cases),
            "case_results": case_results,
        }
    return {"available": True, "reason": "", "unit": "chars", "budgets": budgets}


# ============================================================
# 主流程
# ============================================================
@_with_offline_stubs
def run_offline(record_baseline: bool) -> int:
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

        loc_cases = [
            c for c in data["cases"]
            if c["task_type"] == "localization" and not c.get("requires", [])
        ]
        ts_cases = [c for c in data["cases"] if c.get("requires") == ["tree_sitter"]]
        inc_cases = [c for c in data["cases"] if c["task_type"] == "includes"]
        path_cases = [
            c for c in data["cases"]
            if c["task_type"] == "call_path" and not c.get("expected_unresolved")
        ]
        fp_cases = [c for c in data["cases"] if c.get("expected_unresolved")]
        abstain_cases = [c for c in data["cases"] if c.get("must_abstain")]
        workflow_cases = [c for c in data["cases"] if c["task_type"] == "workflow_retrieval"]
        graph_invariant_cases = [
            c for c in data["cases"] if c["task_type"] == "graph_invariant"
        ]

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

        # --- task-family retrieval(pseudo embedding 只驗 plumbing,不是語意品質宣稱) ---
        workflow_results = [eval_workflow_retrieval_case(c, rags) for c in workflow_cases]
        family_summary = summarize_retrieval_families(workflow_results)
        context_report = evaluate_context_report(workflow_cases, rags, graphs, roots)

        # --- graph 題 ---
        if graph_available:
            inc_results = [eval_includes_case(c, graphs) for c in inc_cases]
            path_results = [
                eval_call_path_case(c, graphs, _TRUE_EDGES_BY_REPO[c["repo"]])
                for c in path_cases
            ]
            fp_results = [eval_fp_case(c, graphs) for c in fp_cases]
            graph_invariant_results = [
                eval_graph_invariant_case(c, graphs) for c in graph_invariant_cases
            ]
            precision = eval_graph_precision(graphs, data["repos"])
            for g in graphs.values():
                g.close()
        else:
            inc_results = [{"id": c["id"], "pass": False, "unavailable": True} for c in inc_cases]
            path_results = [{"id": c["id"], "pass": False, "unavailable": True} for c in path_cases]
            fp_results = [{"id": c["id"], "pass": False, "unavailable": True} for c in fp_cases]
            graph_invariant_results = [
                {"id": c["id"], "pass": False, "unavailable": True}
                for c in graph_invariant_cases
            ]
            precision = {
                "precision": 0.0,
                "representable_recall": 0.0,
                "extracted": 0,
                "correct": 0,
                "representable": sum(
                    len(info["true_call_edges"]) for info in data["repos"].values()
                ),
                "false_resolved": [],
                "missing_representable": [],
                "unavailable": True,
            }

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
            "retrieval_backend": "deterministic_pseudo_embedding_plumbing_stub",
            "task_families": {"summary": family_summary, "cases": workflow_results},
            "graph_invariants": {"cases": graph_invariant_results},
            "context": context_report,
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
    print("retrieval backend: deterministic pseudo-embedding (plumbing stub; not semantic quality)")
    for family, metrics in family_summary.items():
        print(
            f"family {family}: file_recall@5={metrics['file_recall_at_5']:.3f} "
            f"MRR={metrics['mrr']:.3f} seeds={metrics['seed_hits']}/{metrics['cases']}"
        )
    for result in graph_invariant_results:
        suffix = " (graph unavailable)" if result.get("unavailable") else ""
        print(f"graph invariant {result['id']}: {'PASS' if result['pass'] else 'FAIL'}{suffix}")
    print(
        "graph metrics: resolved_precision="
        f"{precision['precision']:.3f} representable_recall="
        f"{precision.get('representable_recall', 0.0):.3f} "
        f"false_resolved={len(precision.get('false_resolved', []))}"
    )
    print(
        "bounded context: "
        + ("available" if context_report["available"] else f"UNAVAILABLE ({context_report['reason']})")
    )
    if context_report["available"]:
        for budget, metrics in context_report["budgets"].items():
            print(
                f"  budget {budget} chars: coverage={metrics['gold_file_coverage']:.3f} "
                f"used={metrics['used_chars']} multi_round={metrics['multi_round_chars']} "
                f"saved={metrics['saved_chars']} truncated_cases={metrics['truncation_cases']}"
            )

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
    for result in workflow_results:
        if result["blocking"] and not result["seed_hit"]:
            failures.append(f"{result['family']} {result['id']} semantic seed miss")
    for result in graph_invariant_results:
        if not result["pass"]:
            failures.append(f"firmware graph invariant {result['id']} failed")
    if precision.get("unavailable") or precision["precision"] != 1.0:
        failures.append(
            f"resolved-edge precision {precision['precision']:.3f} != 1.0"
            + (f" false={precision.get('false_resolved', [])}" if precision.get("false_resolved") else "")
        )
    if precision.get("representable_recall", 0.0) != 1.0:
        failures.append(
            f"representable-edge recall {precision.get('representable_recall', 0.0):.3f} != 1.0"
        )
    if not context_report["available"]:
        failures.append("bounded context unavailable")
    else:
        for budget, metrics in context_report["budgets"].items():
            if not metrics["within_budget"]:
                failures.append(f"bounded context {budget} exceeded or misreported char budget")
            if metrics["gold_file_coverage"] < 1.0:
                failures.append(
                    f"bounded context {budget} gold coverage "
                    f"{metrics['gold_file_coverage']:.3f} < 1.0"
                )
            if metrics["saved_chars"] <= 0:
                failures.append(
                    f"bounded context {budget} did not reduce equivalent grep/read chars"
                )

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
