#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Real semantic retrieval lanes for the code smoke eval(施工規格 §6 P1A)。

這個模組**永遠不連網**。它只做三件事:

1. 從 CodeRAG 的 index 建 corpus manifest(file manifest + document manifest),
   用 digest 驗收,不硬編碼檔案數。
2. 讀 checked-in 的 float32 vector artifact。任何 cache miss / render 不符 /
   維度不符 → **fail closed**(丟 ``VectorCacheError``),絕不退回合成向量。
3. 跑四條 lane 並把 document ranking 依 ``repo_id:path`` **聚合去重後**才截 k。

錄製向量是另一支程式(``eval/record_semantic_vectors.py``)—— 那是唯一可以碰
loopback llama-server 的顯式 opt-in。

為什麼 lane 不是 RRF:CodeRAG runtime 有 cosine、lexical、explicit-symbol
override、type bonus、threshold 與 candidate cutoff 五個機制,RRF 只是其中一種
可能的融合。``runtime_hybrid`` 直接呼叫 ``code_rag`` 抽出來的 pure scorer,
``rrf_experimental`` 是獨立診斷 lane,不得冒充 production hybrid。
"""
from __future__ import annotations

import array
import hashlib
import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURE_DIR = Path(__file__).with_name("fixtures") / "code_smoke"
VECTOR_MANIFEST_FILE = FIXTURE_DIR / "semantic_vectors.json"
VECTOR_ARTIFACT_FILE = FIXTURE_DIR / "semantic_vectors.f32"
SEMANTIC_BASELINE_FILE = FIXTURE_DIR / "semantic_retrieval_baseline.json"

# corpus / query render 的 schema 版本(embed-text schema 在 code_rag)。
CORPUS_MANIFEST_VERSION = 1
QUERY_RENDER_SCHEMA_VERSION = 1

# recorder 的漂移哨兵文字。開頭與結尾各 embed 一次,cosine 低於容差就拒寫。
SENTINEL_TEXT = "codetrail semantic vector recorder sentinel probe"
SENTINEL_MIN_COSINE = 0.9999

# 主 gate lane 是 per_repo(對齊真實 runtime:每次只有一個 AICODE_ROOT);
# union 只是 cross-repo distractor 的 stress 診斷 lane。
CORPUS_SCOPES = ("per_repo", "union")
LANES = ("lexical", "dense", "runtime_hybrid", "rrf_experimental")
RRF_K = 60

DEFAULT_TOP_K = 5


class VectorCacheError(RuntimeError):
    """向量 cache 缺漏 / 不一致。正常 offline run 一律 fail closed。"""


# ============================================================
# rendering
# ============================================================
def render_document(rag, item: dict) -> str:
    """Document 側的 render = production 的 embed text,不另立一套。"""
    return rag._build_embed_text(item)


def render_query(question: str, instruction_id: str = "none") -> str:
    """Query 側 render 與 document 分開:不假設所有模型共用同一 prefix。

    bge 類不吃 query instruction(``none``);Qwen3-Embedding 這類要 query-side
    instruction,屆時新增 instruction_id 並 bump QUERY_RENDER_SCHEMA_VERSION,
    舊 artifact 自然失效。
    """
    if instruction_id == "none":
        return question
    raise ValueError(f"unknown query instruction_id: {instruction_id!r}")


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ============================================================
# corpus manifest
# ============================================================
def document_id(repo_id: str, path: str, kind: str, qualified_name: str,
                line: int, ordinal: int) -> str:
    """Stable doc id。ordinal 只在同 (path, kind, qname, line) 撞號時區分。"""
    raw = "\0".join(
        [repo_id, path, kind, qualified_name, str(line), str(ordinal)]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_documents(rags: dict) -> list[dict]:
    """把每個 repo 的 CodeRAG index 轉成 corpus documents。

    候選單位是 **symbol document** —— file metric 之後才聚合去重(§6 P1A-1)。
    """
    documents: list[dict] = []
    for repo_id in sorted(rags):
        rag = rags[repo_id]
        seen: dict[tuple, int] = {}
        for item in rag.index:
            path = str(item.get("path", "")).replace("\\", "/")
            kind = str(item.get("type", ""))
            qualified = str(item.get("qualified_name") or item.get("symbol", ""))
            line = int(item.get("line", 0) or 0)
            key = (path, kind, qualified, line)
            ordinal = seen.get(key, 0)
            seen[key] = ordinal + 1
            rendered = render_document(rag, item)
            documents.append({
                "doc_id": document_id(repo_id, path, kind, qualified, line, ordinal),
                "repo_id": repo_id,
                "path": path,
                "kind": kind,
                "qualified_name": qualified,
                "symbol": str(item.get("symbol", "")),
                "line": line,
                # index 只存 definition(P2 起 pure declaration 明確不入索引),
                # declaration / unresolved reference 是 graph 側概念,不在這裡假造。
                "evidence_kind": "definition",
                "rendered_text": rendered,
                "rendered_text_sha256": text_sha256(rendered),
                "file_key": f"{repo_id}:{path}",
                "item": item,
            })
    documents.sort(key=lambda d: (d["repo_id"], d["path"], d["line"], d["doc_id"]))
    return documents


def build_file_manifest(rags: dict) -> list[dict]:
    """**index scope 內**的檔案清單 —— 動態計算,只當診斷資訊,驗收看 digest。

    刻意走 index scope 的 walk 而不是 ``_scan_code_files()``:後者已經先濾掉
    沒有 symbol parser 的檔(``.S`` / ``.ld`` / Makefile / ``.md``),用它當
    manifest 的話,P3B 新增的檔案類型改變了可檢索範圍卻不會讓 digest 動 ——
    corpus 漂移就變成無聲的。
    """
    from index_scope import walk_index_files

    rows: list[dict] = []
    for repo_id in sorted(rags):
        rag = rags[repo_id]
        entries = []
        for filepath, rel_path in walk_index_files(rag.scope):
            entries.append((
                str(rel_path).replace("\\", "/"),
                str(rag._compute_file_hash(filepath)),
            ))
        for path, content_hash in sorted(entries):
            rows.append({
                "repo_id": repo_id,
                "path": path,
                "content_sha256": content_hash,
            })
    return rows


def _digest(rows: list[str]) -> str:
    hasher = hashlib.sha256()
    for row in rows:
        hasher.update(row.encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def file_manifest_digest(file_rows: list[dict]) -> str:
    return _digest([
        f"{row['repo_id']}\0{row['path']}\0{row['content_sha256']}"
        for row in file_rows
    ])


def document_manifest_digest(documents: list[dict]) -> str:
    return _digest([
        f"{d['doc_id']}\0{d['repo_id']}\0{d['path']}\0{d['kind']}"
        f"\0{d['qualified_name']}\0{d['rendered_text_sha256']}"
        for d in documents
    ])


def corpus_manifest(rags: dict, documents: list[dict]) -> dict:
    file_rows = build_file_manifest(rags)
    return {
        "manifest_version": CORPUS_MANIFEST_VERSION,
        "file_count": len(file_rows),
        "document_count": len(documents),
        "file_manifest_digest": file_manifest_digest(file_rows),
        "document_manifest_digest": document_manifest_digest(documents),
    }


def pipeline_identity() -> dict:
    """Manifest 的 pipeline 區塊:任何一項變了,舊 artifact 就不該再被採信。"""
    import ast_parser
    import code_rag

    return {
        "parser_semantics_version": ast_parser.PARSER_SEMANTICS_VERSION,
        "embed_text_schema_version": code_rag.EMBED_TEXT_SCHEMA_VERSION,
        "query_render_schema_version": QUERY_RENDER_SCHEMA_VERSION,
        "retrieval_scorer_version": code_rag.RETRIEVAL_SCORER_VERSION,
        "corpus_manifest_version": CORPUS_MANIFEST_VERSION,
        # 實際的 render 預算(環境變數可覆寫)。document 的 rendered_text_sha256
        # 本來就抓得到差異,但那時的訊息是「這份文件的 render 不一樣」;把預算
        # 列進來,錯誤會直接說是預算改了,不用再去猜。
        "render_budgets": code_rag.cache_identity()["render_budgets"],
    }


# ============================================================
# vector artifact
# ============================================================
def _read_rows(artifact_path: Path, dimension: int) -> list[list[float]]:
    raw = artifact_path.read_bytes()
    values = array.array("f")
    values.frombytes(raw)
    if sys.byteorder != "little":
        values.byteswap()
    if dimension <= 0 or len(values) % dimension != 0:
        raise VectorCacheError(
            f"vector artifact {artifact_path.name} holds {len(values)} floats, "
            f"not a multiple of dimension {dimension}"
        )
    return [
        list(values[i * dimension:(i + 1) * dimension])
        for i in range(len(values) // dimension)
    ]


def pack_rows(rows: list[list[float]]) -> bytes:
    flat = array.array("f")
    for row in rows:
        flat.extend(row)
    if sys.byteorder != "little":
        flat.byteswap()
    return flat.tobytes()


class VectorCache:
    """Checked-in 的 real-vector cache。查不到就 raise,不 fallback。"""

    def __init__(self, manifest: dict, rows: list[list[float]]):
        self.manifest = manifest
        self.rows = rows
        self.dimension = int(manifest["model"]["dimension"])
        self.normalization = str(manifest["pipeline"].get("normalization", "unknown"))
        self._documents = {d["doc_id"]: d for d in manifest["documents"]}
        self._queries = {q["case_id"]: q for q in manifest["queries"]}

    # -- loading -------------------------------------------------
    @classmethod
    def load(cls, manifest_path: Path = VECTOR_MANIFEST_FILE,
             artifact_path: Path = VECTOR_ARTIFACT_FILE) -> "VectorCache":
        if not manifest_path.exists() or not artifact_path.exists():
            raise VectorCacheError(
                f"real-vector cache missing ({manifest_path.name} / "
                f"{artifact_path.name}). Record it with "
                "`python3 eval/record_semantic_vectors.py --record-vectors` "
                "while the embedding server is up; the offline eval never "
                "synthesises vectors."
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = str(manifest["artifact"]["sha256"])
        actual = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if expected != actual:
            raise VectorCacheError(
                f"vector artifact checksum mismatch: manifest={expected[:16]}… "
                f"file={actual[:16]}…"
            )
        rows = _read_rows(artifact_path, int(manifest["model"]["dimension"]))
        declared = int(manifest["artifact"]["shape"][0])
        if declared != len(rows):
            raise VectorCacheError(
                f"vector artifact row count {len(rows)} != declared {declared}"
            )
        return cls(manifest, rows)

    # -- verification --------------------------------------------
    def verify_pipeline(self, pipeline: dict, corpus: dict) -> None:
        recorded = self.manifest["pipeline"]
        for key, value in pipeline.items():
            if recorded.get(key) != value:
                raise VectorCacheError(
                    f"vector cache stale: pipeline.{key} recorded="
                    f"{recorded.get(key)!r} current={value!r}. Re-record vectors."
                )
        recorded_corpus = self.manifest["corpus"]
        for key in ("file_manifest_digest", "document_manifest_digest"):
            if recorded_corpus.get(key) != corpus[key]:
                raise VectorCacheError(
                    f"vector cache stale: corpus.{key} changed "
                    f"(recorded={str(recorded_corpus.get(key))[:16]}… "
                    f"current={corpus[key][:16]}…). Re-record vectors."
                )

    def _row(self, index: int, label: str) -> list[float]:
        if not 0 <= index < len(self.rows):
            raise VectorCacheError(f"{label}: vector_row {index} out of range")
        row = self.rows[index]
        if len(row) != self.dimension:
            raise VectorCacheError(
                f"{label}: vector has {len(row)} dims, expected {self.dimension}"
            )
        if not all(math.isfinite(value) for value in row):
            raise VectorCacheError(f"{label}: vector holds non-finite values")
        norm = math.sqrt(sum(value * value for value in row))
        if norm <= 0.0:
            raise VectorCacheError(f"{label}: vector has zero norm")
        if self.normalization == "l2" and abs(norm - 1.0) > 1e-3:
            raise VectorCacheError(
                f"{label}: manifest claims l2-normalized but norm={norm:.6f}"
            )
        return row

    # -- lookup(fail closed)-------------------------------------
    def document_vector(self, document: dict) -> list[float]:
        record = self._documents.get(document["doc_id"])
        if record is None:
            raise VectorCacheError(
                f"no recorded vector for document {document['doc_id']} "
                f"({document['file_key']} {document['qualified_name']})"
            )
        if record["rendered_text_sha256"] != document["rendered_text_sha256"]:
            raise VectorCacheError(
                f"document {document['doc_id']} ({document['file_key']} "
                f"{document['qualified_name']}) was recorded with a different "
                "rendered text. Re-record vectors."
            )
        return self._row(int(record["vector_row"]), f"document {document['doc_id']}")

    def query_vector(self, case_id: str, rendered_query: str) -> list[float]:
        record = self._queries.get(case_id)
        if record is None:
            raise VectorCacheError(f"no recorded query vector for case {case_id}")
        if record["rendered_query_sha256"] != text_sha256(rendered_query):
            raise VectorCacheError(
                f"case {case_id} question changed since the vectors were "
                "recorded. Re-record vectors."
            )
        return self._row(int(record["vector_row"]), f"query {case_id}")

    def model_summary(self) -> dict:
        return dict(self.manifest["model"])


# ============================================================
# lanes
# ============================================================
def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na * nb)


def _sorted_ranking(scored: list[tuple[float, dict]]) -> list[dict]:
    """穩定排序:分數降冪,同分用 repo_id/path/line/doc_id 打平。"""
    ordered = sorted(
        scored,
        key=lambda pair: (
            -pair[0], pair[1]["repo_id"], pair[1]["path"],
            pair[1]["line"], pair[1]["doc_id"],
        ),
    )
    return [dict(doc, score=float(score)) for score, doc in ordered]


def lane_lexical(rag, question: str, documents: list[dict]) -> list[dict]:
    tokens = rag._extract_code_tokens(question)
    return _sorted_ranking([
        (rag._token_match_score(tokens, doc["item"]), doc) for doc in documents
    ])


def lane_dense(query_vector: list[float], documents: list[dict],
               cache: VectorCache) -> list[dict]:
    return _sorted_ranking([
        (cosine(query_vector, cache.document_vector(doc)), doc) for doc in documents
    ])


def lane_runtime_hybrid(rag, question: str, query_vector: list[float],
                        documents: list[dict], cache: VectorCache) -> list[dict]:
    """跑 production 的 pure scorer + production 的 threshold/cutoff。"""
    import code_rag

    tokens = rag._extract_code_tokens(question)
    tokens_lower = {t.lower() for t in tokens}
    scored: list[tuple[float, float, float, dict]] = []
    for doc in documents:
        emb_score = cosine(query_vector, cache.document_vector(doc))
        kw_score = rag._token_match_score(tokens, doc["item"])
        combined, rule = code_rag.hybrid_symbol_score(
            emb_score=emb_score,
            kw_score=kw_score,
            item_type=doc["kind"],
            is_explicit_mention=doc["symbol"].lower() in tokens_lower,
            code_token_count=len(tokens),
        )
        scored.append((combined, emb_score, kw_score, dict(doc, score_rule=rule)))

    ranking = _sorted_ranking([(row[0], row[3]) for row in scored])
    by_doc = {row[3]["doc_id"]: row for row in scored}
    ordered = [
        (
            item["score"],
            by_doc[item["doc_id"]][1],
            by_doc[item["doc_id"]][2],
            item,
        )
        for item in ranking
    ]
    selected = code_rag.select_scored_candidates(
        ordered,
        threshold=config_threshold(),
        top_k=DEFAULT_TOP_K,
        is_short_query=len(tokens) <= 2,
        code_tokens_lower=tokens_lower,
    )
    return [item for _combined, _emb, _kw, item in selected]


def config_threshold() -> float:
    import config

    return float(config.CODE_RAG_THRESHOLD)


def lane_rrf(lexical: list[dict], dense: list[dict]) -> list[dict]:
    """診斷用的簡單 RRF。**不是** production hybrid,不得拿來冒充。"""
    fused: dict[str, float] = {}
    docs: dict[str, dict] = {}
    for ranking in (lexical, dense):
        for rank, item in enumerate(ranking, start=1):
            fused[item["doc_id"]] = fused.get(item["doc_id"], 0.0) + 1.0 / (RRF_K + rank)
            docs[item["doc_id"]] = item
    return _sorted_ranking([
        (score, docs[doc_id]) for doc_id, score in fused.items()
    ])


# ============================================================
# file aggregation + metrics
# ============================================================
def aggregate_files(ranking: list[dict]) -> list[dict]:
    """每檔取最高 document score,穩定排序,**之後**才輪到截 k。

    不做這步的話同一檔的多個 symbol 會占掉多個名次,那不是 file Recall@k,
    而且 parser symbol 數一變排名就不公平地漂。
    """
    best: dict[str, dict] = {}
    for item in ranking:
        key = item["file_key"]
        current = best.get(key)
        if current is None or item["score"] > current["score"]:
            best[key] = {
                "file_key": key,
                "repo_id": item["repo_id"],
                "path": item["path"],
                "score": float(item["score"]),
                "top_symbol": item["symbol"],
                "top_doc_id": item["doc_id"],
            }
    return sorted(
        best.values(),
        key=lambda row: (-row["score"], row["repo_id"], row["path"]),
    )


def gold_file_keys(case: dict, key: str = "gold_files") -> set[str]:
    """把 case 的 relative gold path 正規化成 repo_id:path。

    union lane 內三個 repo 有同名路徑(src/app.c),不正規化就會互撞。
    """
    repo_id = case["repo"]
    return {
        f"{repo_id}:{str(path).replace(chr(92), '/')}"
        for path in case.get(key, []) or []
    }


def file_metrics(aggregated: list[dict], gold_keys: set[str],
                 top_k: int = DEFAULT_TOP_K) -> dict:
    ranked = [row["file_key"] for row in aggregated[:top_k]]
    hits = gold_keys.intersection(ranked)
    reciprocal_rank = 0.0
    for rank, key in enumerate(ranked, start=1):
        if key in gold_keys:
            reciprocal_rank = 1.0 / rank
            break
    return {
        "file_recall_at_k": len(hits) / len(gold_keys) if gold_keys else 0.0,
        "mrr": reciprocal_rank,
        "ranked_files": ranked,
        "hit_files": sorted(hits),
        "gold_files": sorted(gold_keys),
    }


def symbol_metrics(ranking: list[dict], gold_symbols: list[str],
                   top_k: int = DEFAULT_TOP_K) -> dict:
    """Document-level symbol recall。不把 file 聚合的結果假裝成 symbol ranking。"""
    gold = {str(s) for s in gold_symbols or []}
    top = ranking[:top_k]
    found = {item["symbol"] for item in top if item["symbol"] in gold}
    by_kind: dict[str, int] = {}
    for item in top:
        if item["symbol"] in gold:
            by_kind[item["evidence_kind"]] = by_kind.get(item["evidence_kind"], 0) + 1
    return {
        "symbol_recall_at_k": len(found) / len(gold) if gold else 0.0,
        "found_symbols": sorted(found),
        "missing_symbols": sorted(gold - found),
        "hits_by_evidence_kind": dict(sorted(by_kind.items())),
        # index 只收 definition;declaration 在 P2 語意下**刻意不入索引**,
        # unresolved reference 是 graph 側概念 —— 這裡誠實標明,不假造分桶。
        "declaration_evidence": "not_indexed_by_design",
        "unresolved_reference_evidence": "graph_lane_only",
    }


def evaluate_case(case: dict, rag, documents: list[dict], cache: VectorCache,
                  scope: str, top_k: int = DEFAULT_TOP_K) -> dict:
    question = case["question"]
    rendered_query = render_query(question)
    query_vector = cache.query_vector(case["id"], rendered_query)

    lexical = lane_lexical(rag, question, documents)
    dense = lane_dense(query_vector, documents, cache)
    hybrid = lane_runtime_hybrid(rag, question, query_vector, documents, cache)
    rrf = lane_rrf(lexical, dense)

    gold_keys = gold_file_keys(case, "gold_files")
    seed_keys = gold_file_keys(case, "seed_files") or gold_keys

    lanes: dict[str, dict] = {}
    for lane_name, ranking in (
        ("lexical", lexical), ("dense", dense),
        ("runtime_hybrid", hybrid), ("rrf_experimental", rrf),
    ):
        positive = [item for item in ranking if item["score"] > 0]
        aggregated = aggregate_files(positive)
        metrics = file_metrics(aggregated, gold_keys, top_k)
        # seed_files 只另報 seed_recall,**不取代**完整 gold 的主指標。
        seed = file_metrics(aggregated, seed_keys, top_k)
        lanes[lane_name] = {
            **metrics,
            "seed_recall_at_k": seed["file_recall_at_k"],
            "seed_hit": bool(seed["hit_files"]),
            "symbol": symbol_metrics(positive, case.get("gold_symbols", []), top_k),
            # corpus 全量 vs 這條 lane 真的排進來的:hybrid 有 production 的
            # threshold + candidate cutoff,兩個數字本來就不會一樣。
            "corpus_documents": len(documents),
            "documents_ranked": len(ranking),
            "documents_scored_positive": len(positive),
            "top_documents": [
                {
                    "doc_id": item["doc_id"],
                    "file_key": item["file_key"],
                    "symbol": item["symbol"],
                    "kind": item["kind"],
                    "score": round(float(item["score"]), 6),
                }
                for item in positive[:top_k]
            ],
        }

    return {
        "id": case["id"],
        "family": case.get("family", "unknown"),
        "blocking": bool(case.get("blocking")),
        "repo": case["repo"],
        "scope": scope,
        "top_k": top_k,
        "lanes": lanes,
    }


def summarize_by_family(case_results: list[dict], lane: str) -> dict[str, dict]:
    summary: dict[str, dict] = {}
    families = sorted({row["family"] for row in case_results})
    for family in families:
        members = [row for row in case_results if row["family"] == family]
        lanes = [row["lanes"][lane] for row in members]
        summary[family] = {
            "cases": len(members),
            "file_recall_at_k": round(
                sum(item["file_recall_at_k"] for item in lanes) / len(lanes), 4),
            "mrr": round(sum(item["mrr"] for item in lanes) / len(lanes), 4),
            "seed_recall_at_k": round(
                sum(item["seed_recall_at_k"] for item in lanes) / len(lanes), 4),
            "symbol_recall_at_k": round(
                sum(item["symbol"]["symbol_recall_at_k"] for item in lanes) / len(lanes),
                4),
        }
    return summary
