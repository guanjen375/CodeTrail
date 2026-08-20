#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - Code RAG (程式碼索引)

改進：
- 使用 AST/tree-sitter 解析程式碼符號（比 regex 更精準）
- 符號包含完整範圍（start_line, end_line）
- 支援讀取完整函式/類別區塊
"""

import os
import re
import sys
import json
import tempfile
import time as _time
import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from functools import lru_cache


try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

import config
from config import (
    CODE_EXTENSIONS, EMBEDDING_MODEL,
    CODE_RAG_ENABLED, CODE_RAG_TOP_K, CODE_RAG_CACHE_FILE,
    CODE_RAG_THRESHOLD, CODE_RAG_THRESHOLD_BUG,
    CODE_RAG_LAZY_EMBED, CODE_RAG_LAZY_EMBED_MAX_SYMBOLS, CODE_RAG_LAZY_EMBED_QUERY_TOP_K,
    LLAMA_EMBED_BASE_URL, LLAMA_RERANK_BASE_URL,
    USE_RERANKER, RERANKER_MODEL, RERANKER_ALWAYS_ON
)
import file_kind_policy
import fs_safety
import llama_client
from index_scope import load_index_scope, walk_index_files

# 導入 AST 解析器
from ast_parser import PARSER_SEMANTICS_VERSION, parse_file, get_parser_status

# Cache schema 版本。v2(2026-08-19):generation 欄位(generation_id/npz_md5/
# row_count)+ index entry 的 qualified_name/backend(§5-2 與 §6.2-6 合併一次
# bump,只重建一次)。schema 不符 → stderr 說明 + 安全重建。
# v3(P2):index entry 多了 linkage / condition / storage_class,而且 symbol
# 集合本身因 parser semantics v2 而改變(macro/typedef/enum/global 進索引、
# 假 struct definition 消失)。舊 cache 會變混血索引,一律重建一次。
CODE_RAG_CACHE_SCHEMA_VERSION = 3

# Hybrid scorer 的語意版本。改 hybrid_symbol_score / select_scored_candidates
# 的任何權重、bonus、override 或 cutoff 規則就要 bump。
#
# 版本消費矩陣(施工規格 §6 P2-5):
#   - CodeRAG cache:**否**。scorer 是 query-local 的,不寫進 cache。
#   - CodeGraph fingerprint:**否**。
#   - eval vector manifest / baseline report:**是**。lane 分數換演算法後
#     舊 baseline 不可比。
RETRIEVAL_SCORER_VERSION = 1

# Embed text 的 schema 版本。增量重建只比 file_hash,改了 render 卻沒有任何
# 東西會自動失效 —— 這個版本常數與 cache_identity() 是唯二的防線。
#
# 版本消費矩陣(施工規格 §6 P2-5):
#   - CodeRAG cache:**是**(舊向量是用舊 render 算的)
#   - CodeGraph fingerprint:**否**(graph 不吃 embed text)
#   - eval vector manifest:**是**
#
# **什麼時候要 bump**(權威定義,其他地方一律指向這裡):
#
#   免 bump 的只有一種東西 —— **已經明確列在 cache_identity() 的 render_budgets
#   裡的預算數值**(含 AICODE_* 環境變數覆寫)。那些值本身進 cache identity,
#   改了就會自己讓 cache 失效,不需要人工記得 bump。
#
#   其他任何會改變 render **輸出**的修改,一律「bump,或先把它納入 identity」。
#   包含但不限於:欄位集合、欄位順序、label 文字("linkage:" 這種)、分隔方式、
#   截斷演算法,以及**任何還沒進 render_budgets 的截斷數字**。
#
# 為什麼要寫成「白名單」而不是「預算免 bump」:沒進 identity 的數字改了不會讓
# 任何東西失效,而「預算不必 bump」這句話會被讀成「這個數字也不必 bump」——
# 兩邊都不動,舊向量就被靜默沿用。曾經踩到的就是 docstring 的 `[:300]`:它是
# 預算沒錯,但沒有名字、沒進 identity。現在它是
# CODE_RAG_DOCSTRING_MAX_CHARS,規則因此變成機械可判定:**要嘛在
# render_budgets 裡,要嘛就得 bump**,不用靠人分類「這算不算預算」。
#
# 上游那一刀不歸這裡管:ast_parser 建 Symbol 時就先截過 docstring / signature /
# condition,也決定 leading comment 取幾行 —— 那些屬 PARSER_SEMANTICS_VERSION
# (它同樣在 cache_identity() 裡)。
#
# v1:path / type / symbol / parent / signature / docstring / type_hints /
#     context,context 吃 400 - used_len 的剩餘預算。
# v2(P3A):canonical field ordering(含 leading comment、linkage、condition),
#          三個消費者共用同一組欄位、各自獨立預算。
EMBED_TEXT_SCHEMA_VERSION = 2

# 無 parser 的檔案不入 symbol 掃描(§5-5):.txt/.md 以及 P3B 新增的
# .S/.ld/.dts/Makefile/Kconfig 都沒有 symbol parser,進索引只產生零 symbol 的
# 掃描 / hash 成本。刻意不動 CODE_EXTENSIONS —— grep_code / list_dir 的可見
# 範圍不變(Level 1 承諾的正是「全文搜得到」而不是「進 dense retrieval」)。
# 判定改由 file_kind_policy 決定,清單不再各寫一份。
CODE_RAG_SKIP_EXTENSIONS = file_kind_policy.SYMBOL_SCAN_SKIP_SUFFIXES

# 索引專用的掃描快取。刻意**不**共用 utils 的 _SCAN_CACHE:那份是 agent.py 的
# scan_project_metadata 在用,成員資格規則和索引不同,而且沒有 scope
# fingerprint —— §7 要求 fingerprint 缺失或不一致時禁止 fast path,所以那份
# 快取一律不可信。key 帶 fingerprint,scope 一改就自然失效。
# 快照存 {rel_path: hash}(§5-3):TTL 內直接回快照,零 walk、零
# compute_file_hash。TTL 由 config.CODE_RAG_REFRESH_TTL_SECONDS 控制(0=關閉);
# MCP 的寫入工具(apply_patch / run_command / run_lint fix)在 finally 呼叫
# invalidate_scan_cache() 主動失效。
_INDEX_SCAN_CACHE: dict[tuple[str, str], dict] = {}


def invalidate_scan_cache(root: str | Path | None = None) -> None:
    """清掉掃描快照。root=None 清全部;否則只清該 root(不分 fingerprint)。

    寫入類工具(patch / command / lint fix)完成後呼叫 —— 不依 exit code 或
    回傳文字判斷:patch 可能寫入後才驗證失敗,失敗的 formatter/build 也可能
    已改檔,所以一律 finally 失效。
    """
    if root is None:
        _INDEX_SCAN_CACHE.clear()
        return
    resolved = str(Path(root).resolve())
    for key in [k for k in _INDEX_SCAN_CACHE if k[0] == resolved]:
        del _INDEX_SCAN_CACHE[key]


# 小檔走 content hash 的門檻 — 256 KiB 以下直接 hash 內容,
# 大於這個值 fallback 到 size + mtime_ns 的快路徑。
CONTENT_HASH_MAX_BYTES = 256 * 1024


def compute_file_hash(filepath: Path, max_bytes: int = CONTENT_HASH_MAX_BYTES) -> str:
    """單一檔案的快取 hash。空字串 = 讀不到(呼叫端一律當作「不進索引」)。

    小檔(≤256 KiB)直接 hash content,避開「同秒多次寫入 / preserve-timestamp
    同步工具」造成的 mis-hit;大檔走 size + mtime_ns 快路徑,平衡 I/O 與正確性。
    mtime_ns 比 mtime(秒解析度)更穩,在快速 edit-save 場景下不會誤判 cache hit。

    module-level 是為了讓 scripts/index_stats.py 能用同一套判定驗證快取新鮮度,
    不必複製一份會漂移的實作。
    """
    try:
        stat = filepath.stat()
        if stat.st_size <= max_bytes:
            return hashlib.md5(filepath.read_bytes()).hexdigest()
        return hashlib.md5(f"{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()
    except OSError:
        return ""


def _normalize_text_for_cache(text: str) -> str:
    """正規化文字以提高 cache 命中率"""
    return ' '.join(text.split())


def plan_embed_batches(texts: list[str], max_items: int, max_chars: int) -> list[list[int]]:
    """把 texts 依雙預算(筆數 ≤ max_items 且總 chars ≤ max_chars)切成 index 批。

    保序;單筆超過 max_chars 時自成一批(不丟棄、不截斷 —— server 端自己有
    context 上限,超長由它 fail-loud)。module-level 讓測試能直接鎖切分結果。
    """
    batches: list[list[int]] = []
    current: list[int] = []
    current_chars = 0
    for i, text in enumerate(texts):
        n = len(text)
        if current and (len(current) >= max_items or current_chars + n > max_chars):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(i)
        current_chars += n
    if current:
        batches.append(current)
    return batches


@dataclass
class RankedCandidate:
    """單一 query 的排序結果(query-local,絕不寫回 self.index 的持久 item)。

    rerank_score 是 None ⇔ 本次沒有實際執行 rerank;0.0 是有效的 cross-encoder
    低分,不得當「沒有分數」。final_score = rerank_score if score_source=="rerank"
    else combined_score;對外 score = round(final_score, 3)。
    """
    item: dict
    rerank_score: float | None
    combined_score: float
    final_score: float
    score_source: str  # "rerank" | "fusion"


# Canonical 語意欄位順序(施工規格 §6 P3A-1)。**單一來源**:dense embed text、
# lexical scorer、reranker passage 三個消費者都從這裡投影出自己的視角。
#
# 不共用一份的後果是無聲的:改了 embed text 而 lexical 還在掃舊 context,
# 報表上只會看到「某條 lane 沒進步」,不會有人知道是它根本沒拿到那個欄位。
# 三者**不必字串完全相同**,但不能有人看得到 comments、有人看不到。
CANONICAL_SEMANTIC_FIELDS = (
    "path", "type", "symbol", "parent", "signature",
    "linkage", "condition", "comments", "docstring", "type_hints", "context",
)


def semantic_fields(item: dict) -> list[tuple[str, str]]:
    """把 index entry 投影成 (label, text) 的 canonical 有序欄位。

    label 空字串代表「直接放值、不加前綴」(path / kind / symbol 這類本身就是
    識別資訊的欄位)。
    """
    fields: list[tuple[str, str]] = []

    def push(label: str, value) -> None:
        text = str(value or "").strip()
        if text:
            fields.append((label, text))

    push("", item.get("path"))
    push("", item.get("type"))
    push("", item.get("symbol"))
    if item.get("parent"):
        push("in", item["parent"])
    push("", item.get("signature"))
    push("linkage:", item.get("linkage"))
    push("condition:", item.get("condition"))
    push("", item.get("comments"))
    push("", item.get("docstring"))
    push("types:", item.get("type_hints"))
    push("", item.get("context"))
    return fields


def render_semantic_fields(item: dict, max_chars: int) -> str:
    """依 canonical 順序組字串,超出預算就從尾端欄位開始截。

    尾端是 context —— 最長也最可再生的欄位;symbol / signature / comments 這些
    高密度的識別資訊排在前面,先被保住。
    """
    parts: list[str] = []
    used = 0
    for label, text in semantic_fields(item):
        chunk = f"{label} {text}" if label else text
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
        parts.append(chunk)
        used += len(chunk) + 1
    return " ".join(parts)


def lexical_scan_text(item: dict, max_chars: int) -> str:
    """lexical scorer 掃描的文字:同一組 canonical 欄位,自己的預算。"""
    return render_semantic_fields(item, max_chars)


def cache_identity() -> dict:
    """持久 cache 的身分欄位 —— **單一來源**,寫入端與驗證端都從這裡取。

    刻意包含**實際的預算值**而不是只有 schema 版本:這些預算是環境變數可覆寫的
    (`AICODE_CODE_RAG_EMBED_TEXT_MAX_CHARS` 等),只鎖 schema version 的話,重啟
    時改一個環境變數就會靜默沿用「用另一組 render 算出來的」embedding —— 沒有
    任何訊息會提醒你現在查的是舊向量。

    兩邊各寫一份的另一個失敗模式同樣無聲:加了欄位而測試 fixture 沒跟上,舊 cache
    被拒 → 那條測試改走 full rebuild,「還是綠的」卻不再驗它本來要驗的東西。
    """
    return {
        "schema_version": CODE_RAG_CACHE_SCHEMA_VERSION,
        "embedding_model": EMBEDDING_MODEL,
        "parser_semantics_version": PARSER_SEMANTICS_VERSION,
        "embed_text_schema_version": EMBED_TEXT_SCHEMA_VERSION,
        # 只列**會改變已儲存內容**的預算。lexical scan 與 rerank passage 是
        # query-time 才用的,不影響任何 cache 住的東西。
        "render_budgets": {
            "context_store": config.CODE_RAG_CONTEXT_STORE_MAX_CHARS,
            "comment": config.CODE_RAG_COMMENT_MAX_CHARS,
            "docstring": config.CODE_RAG_DOCSTRING_MAX_CHARS,
            "embed_text": config.CODE_RAG_EMBED_TEXT_MAX_CHARS,
        },
    }


def hybrid_symbol_score(*, emb_score: float, kw_score: float, item_type: str,
                        is_explicit_mention: bool,
                        code_token_count: int) -> tuple[float, str]:
    """Production 的 hybrid 融合分數。純函式,無 self / 無 I/O。

    抽出來的理由(施工規格 §6 P1A-2):evaluator 的 ``runtime_hybrid`` lane
    必須跑**同一份** production scoring,否則量到的是另一條演算法。這裡不是
    RRF —— cosine + lexical + explicit-symbol override + type bonus 是四個
    不同機制,RRF 只能當獨立的診斷 lane。

    回傳 (combined_score, score_rule);score_rule 供 eval lane metadata 揭露
    是哪條規則決定名次。
    """
    if is_explicit_mention:
        # 明確點名:直接給很高分,即使 embedding 不太像
        return 0.95, "explicit_symbol"
    if kw_score >= 0.8 and code_token_count >= 2:
        # 高 kw_score 但需要至少 2 個 code_tokens,避免短 query 誤判
        return 0.9 + kw_score * 0.1, "lexical_dominant"
    # 一般情況:function 類型給一點優先權
    type_bonus = 0.05 if item_type == 'function' else 0.0
    return 0.5 * emb_score + 0.5 * kw_score + type_bonus, "fusion"


def select_scored_candidates(scores: list, *, threshold: float, top_k: int,
                             is_short_query: bool, code_tokens_lower: set) -> list:
    """Production 的門檻篩選 + candidate cutoff。純函式,無 self / 無 I/O。

    scores 是已排序的 [(combined, emb_score, kw_score, item), ...]。
    evaluator 的 runtime_hybrid lane 重用它,才會連 cutoff 行為都一致。
    """
    selected = []
    for combined, emb_score, kw_score, item in scores:
        if is_short_query:
            symbol_lower = item.get("symbol", "").lower()
            is_explicit = symbol_lower in code_tokens_lower
            if combined >= threshold or is_explicit:
                selected.append((combined, emb_score, kw_score, item))
        else:
            if combined >= threshold or kw_score >= 0.85:
                selected.append((combined, emb_score, kw_score, item))

        # 收集足夠的候選後停止（rerank 用）
        if len(selected) >= top_k * 3:
            break
    return selected


@lru_cache(maxsize=256)
def _cached_get_embedding(text: str) -> tuple:
    """帶 LRU cache 的 embedding 查詢(CodeRAG 用,走 llama-server /embedding)。"""
    try:
        emb = llama_client.embed_one(
            base_url=LLAMA_EMBED_BASE_URL,
            content=text,
            model=EMBEDDING_MODEL,
            timeout=60,
        )
    except Exception as exc:
        raise RuntimeError(
            f"embedding server unreachable at {LLAMA_EMBED_BASE_URL}: {exc}. "
            "Check the 8081 llama-server or AICODE_LLAMA_EMBED_BASE_URL."
        ) from exc

    if not emb:
        raise RuntimeError(
            f"embedding server returned an empty vector at {LLAMA_EMBED_BASE_URL}. "
            "Check the 8081 llama-server or AICODE_LLAMA_EMBED_BASE_URL."
        )
    return tuple(emb)


class CodeRAG:
    """
    專案級程式碼 RAG：
    - 動態建立程式碼索引（函式/類別級別）
    - 用於 Agent 模式的「第一層縮小範圍」

    快取優化 v3（增量更新）：
    - 每檔案獨立快取：file_hash -> symbols + embeddings
    - 只重建變更的檔案，大幅提升大型 repo 的速度
    - embedding 使用 numpy .npz 二進位格式（壓縮率高）
    """

    def __init__(self, folder: str):
        self.folder = Path(folder).resolve()
        # 索引範圍引擎(Layer C 設定不存在時就是預設行為,不會 fail)
        self.scope = load_index_scope(self.folder)
        # 快取檔案
        cache_base = CODE_RAG_CACHE_FILE.replace('.json', '')
        self.cache_meta_file = self.folder / f"{cache_base}_meta.json"
        self.cache_emb_file = self.folder / f"{cache_base}_emb.npz"
        # 單一 writer 鎖(§5-2);建立走 fs_safety 的 symlink 防線
        self.cache_lock_file = self.folder / f"{cache_base}.lock"
        self.index = []
        self.embeddings = None  # numpy array, shape: (N, embedding_dim)
        # 增量快取：{file_rel_path: {"hash": str, "symbols": list, "embeddings": list}}
        self._file_cache = {}
        self._lazy_embed = False
        self._lazy_embed_top_k = CODE_RAG_LAZY_EMBED_QUERY_TOP_K
        self._indexed_file_hashes = None
        # 快取裡的 scope_fingerprint 是否與現在的規則一致(見 _load_file_cache)
        self._scope_fingerprint_ok = False

    # 保留成 class attribute:既有測試與呼叫端都靠它。
    _CONTENT_HASH_MAX_BYTES = CONTENT_HASH_MAX_BYTES

    def _compute_file_hash(self, filepath: Path) -> str:
        """計算單一檔案的 hash（用於增量快取驗證）。見 compute_file_hash。"""
        return compute_file_hash(filepath, self._CONTENT_HASH_MAX_BYTES)

    def _scan_code_files(self, *, force_refresh: bool = False) -> dict:
        """掃描進索引的程式碼檔案，返回 {rel_path: {"filepath": Path, "hash": str}}

        成員資格由且僅由 self.scope.should_index_file 決定;_filter_dirnames 的
        三態只是剪枝。fast path 的 key 綁 scope fingerprint,而且拿回來的每一條
        cached path 仍然要重過 should_index_file（§7.3:任何來源的 cached path
        都不得直接信任）。

        TTL 快照(§5-3):TTL 內直接回快照的 {rel: {filepath, hash}} shallow
        copy —— 零 os.walk、零 compute_file_hash。快照過期 / TTL=0 /
        invalidate_scan_cache() 之後才 fresh 掃描。
        """
        ttl = int(getattr(config, "CODE_RAG_REFRESH_TTL_SECONDS", 30))
        cache_key = (str(self.folder), self.scope.fingerprint)
        if not force_refresh and ttl > 0:
            cached = _INDEX_SCAN_CACHE.get(cache_key)
            if cached is not None and _time.time() - cached["timestamp"] < ttl:
                result = {}
                for rel_path, file_hash in cached["entries"].items():
                    # 既有不變式:cached path 一律重過 should_index_file(純記憶體)
                    if not self.scope.should_index_file(rel_path):
                        continue
                    result[rel_path] = {"filepath": self.folder / rel_path, "hash": file_hash}
                return result

        result = {}
        for filepath, rel_path in walk_index_files(self.scope):
            # §5-5:無 parser 的檔案不入 symbol 掃描(grep/list_dir 不受影響)
            if not file_kind_policy.enters_symbol_scan(rel_path):
                continue
            file_hash = self._compute_file_hash(filepath)
            if file_hash:
                result[rel_path] = {"filepath": filepath, "hash": file_hash}

        _INDEX_SCAN_CACHE[cache_key] = {
            "entries": {rel_path: info["hash"] for rel_path, info in result.items()},
            "timestamp": _time.time(),
        }
        return result

    def _scan_code_files_fresh(self) -> dict:
        """Fresh scan helper that keeps simple no-arg test/integration hooks compatible."""
        scanner = self._scan_code_files
        if getattr(scanner, "__func__", None) is CodeRAG._scan_code_files:
            return scanner(force_refresh=True)
        return scanner()

    def _load_file_cache(self) -> dict:
        """載入增量快取（每檔案粒度）

        載入驗證(§5-2):schema 版本、embedding model、NPZ md5 世代一致性、
        row_count。任一不符 → stderr 印明確原因 + 回空(安全重建),絕不讀
        撕裂 / 半成品世代,也絕不 silent swallow。

        Returns:
            {rel_path: {"hash": str, "symbols": list, "embeddings": list}}
        """
        self._scope_fingerprint_ok = False
        if not self.cache_meta_file.exists():
            return {}

        try:
            with open(self.cache_meta_file, 'r', encoding='utf-8') as f:
                meta = json.load(f)
        except Exception as e:
            print(f"[CODE_RAG] cache meta 損壞({type(e).__name__}: {e}),安全重建",
                  file=sys.stderr)
            return {}

        schema = meta.get("schema_version")
        if schema != CODE_RAG_CACHE_SCHEMA_VERSION:
            print(f"[CODE_RAG] cache schema {schema!r} != {CODE_RAG_CACHE_SCHEMA_VERSION},"
                  "安全重建", file=sys.stderr)
            return {}

        if meta.get("embedding_model") != EMBEDDING_MODEL:
            print("[CODE_RAG] cache embedding_model 與現行設定不符,安全重建",
                  file=sys.stderr)
            return {}

        # 版本消費矩陣(§6 P2-5):parser semantics 決定 symbol 集合、embed-text
        # schema 與 render 預算決定向量內容。增量重建只比 file_hash,這些一動舊
        # cache 就是錯的,而且錯得無聲 —— 必須在這裡擋掉。
        #
        # **遍歷整份 identity,不列舉 key**:寫死清單的話,以後在
        # cache_identity() 加欄位,寫入端會存、loader 卻視而不見 —— 又是一個
        # 只有「查出來的結果怪怪的」才會發現的無聲差異。
        for key, expected in cache_identity().items():
            if meta.get(key) != expected:
                print(f"[CODE_RAG] cache {key} {meta.get(key)!r} != "
                      f"{expected!r},安全重建", file=sys.stderr)
                return {}

        # 世代一致性:meta 記錄的 npz_md5 必須與磁碟上的 NPZ 相符。
        # kill 在「NPZ 已替換、meta 未替換」之間會在這裡被抓到。
        npz_md5 = meta.get("npz_md5")
        if npz_md5 is not None:
            if not self.cache_emb_file.exists():
                print("[CODE_RAG] cache NPZ 缺失(meta 記錄應存在),安全重建",
                      file=sys.stderr)
                return {}
            try:
                actual_md5 = hashlib.md5(self.cache_emb_file.read_bytes()).hexdigest()
            except OSError as e:
                print(f"[CODE_RAG] cache NPZ 讀取失敗({e}),安全重建", file=sys.stderr)
                return {}
            if actual_md5 != npz_md5:
                print("[CODE_RAG] cache NPZ md5 不符(世代不一致),安全重建",
                      file=sys.stderr)
                return {}

        row_count = meta.get("row_count")
        if row_count != len(meta.get("index", [])):
            print(f"[CODE_RAG] cache row_count={row_count!r} 與 index 筆數不符,安全重建",
                  file=sys.stderr)
            return {}

        # scope 規則變了不代表要重 embed:embedding_model 相同就保留 per-file
        # symbol/embedding cache,只重算 membership delta。這裡只記錄
        # fingerprint 是否相符,由 _load_cache / _scan_code_files 決定要不要
        # 拒絕「整包 fast load」。
        self._scope_fingerprint_ok = (
            meta.get("scope_fingerprint") == self.scope.fingerprint
        )
        return meta.get("file_cache", {})

    def _load_cache(self) -> bool:
        """嘗試載入快取(增量模式)。

        schema v2 起只有增量路徑:pre-v2 的整包 fast load(folder_hash)與更早
        的單檔 legacy JSON 都在 schema bump 時淘汰 —— 舊快取缺 qualified_name /
        backend / generation 欄位,留著會變混血索引,一律重建一次(§5-2)。
        _load_file_cache 已對不合格 meta 印 stderr 原因。
        """
        self._file_cache = self._load_file_cache()

        # 如果有增量快取，使用增量模式
        if self._file_cache:
            return False  # 返回 False 讓 build_index 進行增量更新

        return False

    def _save_cache(self):
        """儲存快取（增量模式：每檔案粒度）

        §5-2 契約:
        - flock(.code_rag_cache.lock)單一 writer;lock 檔建立走 fs_safety 的
          symlink 防線(O_NOFOLLOW + fstat S_ISREG + 父目錄 realpath 在 root 內)。
        - 寫序:NPZ temp → os.replace → 算 md5 → meta JSON(generation_id /
          npz_md5 / row_count)temp → os.replace。kill 在任一點,讀端都能用
          md5 / row_count 驗出撕裂世代並安全重建。
        - 寫入失敗 raise(fail-loud),不得無聲吞掉。
        """
        lock_fd = fs_safety.acquire_file_lock(self.cache_lock_file, self.folder)
        try:
            # 上次 crash 可能留下 tmp 殘留;持鎖下只清自家精確前綴
            for stale in self.folder.glob(f"{self.cache_emb_file.name}.tmp*"):
                stale.unlink(missing_ok=True)
            for stale in self.folder.glob(f"{self.cache_meta_file.name}.tmp*"):
                stale.unlink(missing_ok=True)

            npz_md5 = None
            if HAS_NUMPY and self.embeddings is not None:
                fd, tmp_npz = tempfile.mkstemp(
                    dir=self.folder,
                    prefix=f"{self.cache_emb_file.name}.tmp",
                    suffix=".npz",
                )
                os.close(fd)
                try:
                    np.savez_compressed(tmp_npz, embeddings=self.embeddings)
                    os.replace(tmp_npz, self.cache_emb_file)
                except BaseException:
                    Path(tmp_npz).unlink(missing_ok=True)
                    raise
                npz_md5 = hashlib.md5(self.cache_emb_file.read_bytes()).hexdigest()
            else:
                # lazy / 無 numpy:沒有 dense 矩陣可存。刪掉舊 NPZ,避免留下
                # 與新 meta 不同世代的殘影。
                self.cache_emb_file.unlink(missing_ok=True)

            emb_dim = self.embeddings.shape[1] if HAS_NUMPY and self.embeddings is not None else None
            meta = {
                **cache_identity(),
                "generation_id": uuid.uuid4().hex,
                "scope_fingerprint": self.scope.fingerprint,
                "embedding_dim": emb_dim,
                "npz_md5": npz_md5,
                "row_count": len(self.index),
                "index": self.index,
                "file_cache": self._file_cache  # 增量快取
            }
            fd, tmp_meta = tempfile.mkstemp(
                dir=self.folder,
                prefix=f"{self.cache_meta_file.name}.tmp",
                suffix=".json",
            )
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(meta, f, ensure_ascii=False)
                os.replace(tmp_meta, self.cache_meta_file)
            except BaseException:
                Path(tmp_meta).unlink(missing_ok=True)
                raise
        finally:
            fs_safety.release_file_lock(lock_fd)

    def _extract_symbols(self, filepath: Path, content: str) -> list[dict]:
        """從程式碼中提取符號（函式、類別）

        改進（v2）：
        - 使用 AST/tree-sitter 解析，比 regex 更精準
        - 符號包含完整範圍（start_line, end_line）
        - 支援 method 的 parent class 資訊

        P0 改進（v3）：
        - 支援 signature, docstring, type_hints
        """
        rel_path = str(filepath.relative_to(self.folder))

        # 使用 AST 解析器
        try:
            ast_symbols = parse_file(filepath, content)
        except Exception as e:
            print(f"[CODE_RAG] AST 解析 {rel_path} 失敗: {e}", file=sys.stderr)
            ast_symbols = []

        # 轉換為內部格式
        symbols = []
        for sym in ast_symbols:
            symbol_dict = {
                'path': rel_path,
                'symbol': sym.name,
                'type': sym.type,
                'line': sym.start_line,
                'end_line': sym.end_line,  # 新增：符號結束行
                # 儲存端上限。這是最上游的截斷:比下游任何預算小的話,
                # 下游放大都是 no-op(§3 洞 2)。
                'context': sym.context[:config.CODE_RAG_CONTEXT_STORE_MAX_CHARS],
                # graph 前置(§6.2-6):stable ID 與 evidence 揭露依賴這兩欄。
                # 只進 index entry / cache;預設回傳 shape 不出現(§8.1)。
                'qualified_name': sym.qualified_name or sym.name,
                'backend': sym.backend or 'unknown',
            }
            # 如果有 parent（method 屬於某個 class），記錄下來
            if sym.parent:
                symbol_dict['parent'] = sym.parent
            # P0 改進：擴充欄位
            if sym.signature:
                symbol_dict['signature'] = sym.signature
            if sym.docstring:
                symbol_dict['docstring'] = sym.docstring[:config.CODE_RAG_DOCSTRING_MAX_CHARS]
            if sym.type_hints:
                symbol_dict['type_hints'] = sym.type_hints
            # C/C++ definition metadata(§6 P2-4)。parser 算出來卻沒往下傳的話,
            # retrieval 與 graph 都看不到,「parser 支援」等於沒發生。
            if sym.comments:
                symbol_dict['comments'] = sym.comments[:config.CODE_RAG_COMMENT_MAX_CHARS]
            for field in ('linkage', 'condition', 'storage_class'):
                value = getattr(sym, field, None)
                if value:
                    symbol_dict[field] = value

            symbols.append(symbol_dict)

        return symbols

    def _get_embedding(self, text: str) -> list:
        """取得 embedding（使用 LRU cache 加速重複查詢）"""
        normalized = _normalize_text_for_cache(text)
        result = _cached_get_embedding(normalized)
        return list(result)

    def _embed_texts_batched(self, texts: list[str]) -> list[list[float]]:
        """批次 embedding(§5-4):依雙預算切批走 /v1/embeddings。

        任一批失敗直接拋:/v1/embeddings 的契約錯誤(cardinality / index /
        維度)保留原訊息;連線層例外包成既有的 unreachable 訊息。
        不部分接受 —— 呼叫端(build/backfill/materialize)自己負責 reset。
        """
        if not texts:
            return []
        batches = plan_embed_batches(
            texts, config.EMBED_BATCH_SIZE, config.EMBED_BATCH_MAX_CHARS
        )
        out: list[list[float] | None] = [None] * len(texts)
        for batch_indices in batches:
            contents = [texts[i] for i in batch_indices]
            try:
                vectors = llama_client.embed_batch(
                    base_url=LLAMA_EMBED_BASE_URL,
                    contents=contents,
                    model=EMBEDDING_MODEL,
                    timeout=300,
                )
            except llama_client.EmbeddingContractError:
                raise  # /v1/embeddings 嚴格契約違規:訊息已明確,不重包
            except Exception as exc:
                raise RuntimeError(
                    f"embedding server unreachable at {LLAMA_EMBED_BASE_URL}: {exc}. "
                    "Check the 8081 llama-server or AICODE_LLAMA_EMBED_BASE_URL."
                ) from exc
            for i, vec in zip(batch_indices, vectors):
                out[i] = vec
        return out  # plan_embed_batches 覆蓋每個 index,不會留 None

    def _build_embed_text(self, item: dict) -> str:
        """Dense embedding 的 document text(消費者之一,§6 P3A-1)。

        欄位集合與順序來自 CANONICAL_SEMANTIC_FIELDS —— 與 lexical scorer、
        reranker passage 同一份;預算是自己的 CODE_RAG_EMBED_TEXT_MAX_CHARS。
        改動 render **輸出**(欄位、順序、label、分隔、截斷演算法)要 bump
        EMBED_TEXT_SCHEMA_VERSION;只改預算數值不必 —— 規則與理由見該常數的
        宣告處。
        """
        return render_semantic_fields(item, config.CODE_RAG_EMBED_TEXT_MAX_CHARS)

    def _index_single_file(self, filepath: Path, rel_path: str,
                           compute_embeddings: bool = True) -> tuple:
        """索引單一檔案，返回 (symbols, embeddings)"""
        content = filepath.read_text(encoding='utf-8', errors='replace')
        symbols = self._extract_symbols(filepath, content)

        file_symbols = []
        file_embeddings = []

        for sym in symbols:
            embed_text = self._build_embed_text(sym)
            emb = self._get_embedding(embed_text) if compute_embeddings else []

            index_entry = {
                'path': sym['path'],
                'symbol': sym['symbol'],
                'type': sym['type'],
                'line': sym['line'],
                'context': sym['context'][:config.CODE_RAG_CONTEXT_STORE_MAX_CHARS],
                'qualified_name': sym.get('qualified_name', sym['symbol']),
                'backend': sym.get('backend', 'unknown'),
            }
            if 'end_line' in sym:
                index_entry['end_line'] = sym['end_line']
            if 'parent' in sym:
                index_entry['parent'] = sym['parent']
            # P0 改進：儲存擴充欄位
            if 'signature' in sym and sym['signature']:
                index_entry['signature'] = sym['signature']
            if 'docstring' in sym and sym['docstring']:
                index_entry['docstring'] = sym['docstring'][:config.CODE_RAG_DOCSTRING_MAX_CHARS]
            if 'type_hints' in sym and sym['type_hints']:
                index_entry['type_hints'] = sym['type_hints']
            for field in ('comments', 'linkage', 'condition', 'storage_class'):
                if sym.get(field):
                    index_entry[field] = sym[field]

            file_symbols.append(index_entry)
            file_embeddings.append(emb)

        return file_symbols, file_embeddings

    def build_index(self, verbose: bool = True, _current_files: dict | None = None):
        """建立程式碼索引（支援增量更新）"""
        if not CODE_RAG_ENABLED:
            return

        # 嘗試載入快取
        if self._load_cache():
            current_files = _current_files or self._scan_code_files_fresh()
            self._indexed_file_hashes = {
                rel_path: info["hash"] for rel_path, info in current_files.items()
            }
            if verbose:
                print(f"[CODE_RAG] 載入快取: {len(self.index)} 個符號")
            return

        # 掃描所有程式碼檔案
        current_files = _current_files or self._scan_code_files_fresh()

        # 計算需要更新的檔案
        files_to_index = []
        files_unchanged = []
        files_deleted = set(self._file_cache.keys()) - set(current_files.keys())

        for rel_path, info in current_files.items():
            cached = self._file_cache.get(rel_path)
            if cached and cached.get("hash") == info["hash"]:
                files_unchanged.append(rel_path)
            else:
                files_to_index.append((rel_path, info["filepath"], info["hash"]))

        # 判斷是增量還是全量
        is_incremental = len(self._file_cache) > 0 and len(files_to_index) < len(current_files)

        if verbose:
            # §6.2-5 能力揭露(反轉舊的「tree-sitter 在場才印」邏輯):主要語言
            # degraded 時必須 WARN,而不是安靜地少報符號。counts/語言名 only,
            # 永不印 NDA path。
            parser_status = get_parser_status()
            degraded_main = sorted(
                lang for lang in ('c', 'cpp')
                if parser_status['languages'].get(lang) == 'regex-degraded'
            )
            if degraded_main:
                print(
                    f"[CODE_RAG] WARN: parser degraded to regex for "
                    f"{', '.join(degraded_main)} — 多行 signature 函式會漏抽。"
                    "安裝: pip install tree-sitter tree-sitter-c tree-sitter-cpp",
                    file=sys.stderr,
                )
            ts_langs = [k for k, v in parser_status['languages'].items() if v == 'tree-sitter']
            if ts_langs:
                print(f"[CODE_RAG] 使用 tree-sitter: {', '.join(ts_langs)}")

            if is_incremental:
                print(f"[CODE_RAG] 增量更新: {len(files_to_index)} 個檔案變更, "
                      f"{len(files_unchanged)} 個未變, {len(files_deleted)} 個已刪除")
            else:
                print(f"[CODE_RAG] 建立程式碼索引... ({len(current_files)} 個檔案)")

        # 收集所有符號和 embeddings
        self.index = []
        embeddings_list = []
        new_file_cache = {}
        total_symbols = 0
        lazy_enabled = CODE_RAG_LAZY_EMBED
        self._lazy_embed = False

        # 先加入未變更的檔案（從快取讀取）
        for rel_path in files_unchanged:
            cached = self._file_cache[rel_path]
            for sym in cached.get("symbols", []):
                self.index.append(sym)
            embeddings_list.extend(cached.get("embeddings", []))
            total_symbols += len(cached.get("symbols", []))
            new_file_cache[rel_path] = cached

        if lazy_enabled and total_symbols > CODE_RAG_LAZY_EMBED_MAX_SYMBOLS:
            self._lazy_embed = True

        # 索引變更的檔案。§5-4:parse 階段一律不逐筆 embed
        # (compute_embeddings=False → 空 embedding 佔位);dense 模式下由
        # _backfill_cached_embedding_gaps 統一走 /v1/embeddings 批次補齊,
        # lazy 模式維持空 embedding 延後到查詢。
        indexed_count = 0
        for rel_path, filepath, file_hash in files_to_index:
            try:
                symbols, embeddings = self._index_single_file(
                    filepath, rel_path, compute_embeddings=False
                )
                self.index.extend(symbols)
                embeddings_list.extend(embeddings)
                total_symbols += len(symbols)

                if lazy_enabled and not self._lazy_embed:
                    if total_symbols > CODE_RAG_LAZY_EMBED_MAX_SYMBOLS:
                        self._lazy_embed = True

                # 更新快取
                new_file_cache[rel_path] = {
                    "hash": file_hash,
                    "symbols": symbols,
                    "embeddings": embeddings
                }
                indexed_count += 1

                if verbose and is_incremental:
                    print(f"   [REINDEX] {rel_path} ({len(symbols)} 個符號)")
            except RuntimeError:
                # Embedding failures must not leave a partial index that makes
                # the next query skip build_index after the server recovers.
                self._reset_partial_index()
                raise
            except Exception as e:
                print(f"[CODE_RAG] 索引 {rel_path} 時發生錯誤: {e}", file=sys.stderr)
                continue

        self._file_cache = new_file_cache

        # 將 embedding 轉換為 numpy array 並預先 L2 normalize
        if HAS_NUMPY and embeddings_list and not self._lazy_embed:
            try:
                self._backfill_cached_embedding_gaps(embeddings_list, verbose=verbose)
            except Exception:
                # 和上面 files_to_index 那個 except 同一個契約:embedding 失敗不能
                # 留下半成品索引。留著的話 query() 會因為 self.index 非空而不再
                # 重建(_refresh_if_stale 也因為 _indexed_file_hashes is None 直接
                # return),整個 MCP process 就一路用缺 embedding 的索引降級下去。
                self._reset_partial_index()
                raise
            dimensions = {len(embedding) for embedding in embeddings_list if embedding}
            if not dimensions or any(not embedding for embedding in embeddings_list):
                self._reset_partial_index()
                raise RuntimeError(
                    "Code RAG full index contains missing embeddings; refusing zero padding"
                )
            if len(dimensions) != 1:
                self._reset_partial_index()
                raise RuntimeError(
                    f"Code RAG embedding dimension mismatch: {sorted(dimensions)}; "
                    "zero-padding/truncation is forbidden"
                )
            normalized = embeddings_list
            self.embeddings = np.array(normalized, dtype=np.float32)

            norms = np.linalg.norm(self.embeddings, axis=1, keepdims=True)
            norms = np.where(norms > 0, norms, 1.0)
            self.embeddings = self.embeddings / norms
            self._embeddings_normalized = True
        else:
            self.embeddings = None
            for i, emb in enumerate(embeddings_list):
                if i < len(self.index) and emb:
                    self.index[i]['embedding'] = emb

        self._indexed_file_hashes = {
            rel_path: info["hash"] for rel_path, info in current_files.items()
        }

        if verbose:
            if is_incremental:
                print(f"[CODE_RAG] 增量更新完成: 共 {len(self.index)} 個符號")
            else:
                print(f"[CODE_RAG] 索引完成: {len(self.index)} 個符號")
            if self._lazy_embed:
                print(f"[CODE_RAG] lazy embed on: >{CODE_RAG_LAZY_EMBED_MAX_SYMBOLS} symbols")
            self._print_scope_summary()

        self._save_cache()

    def _print_scope_summary(self) -> None:
        """索引範圍摘要 —— **只有計數,永遠不印路徑或 pattern 內容**。

        這行會進 MCP log,而 log 會被貼進 issue;路徑本身就是 NDA 內容。
        預設部署(沒有任何規則命中)完全不印,避免噪音。
        """
        lines = self.scope.stats_lines(include_zero=False)
        if lines:
            print("[CODE_RAG] index scope — " + "; ".join(lines))

    def _refresh_if_stale(self) -> None:
        """Incrementally rebuild when files change during the current MCP session.

        §5-3:走 TTL 快照(_scan_code_files 的 fast path)。TTL 內重複查詢
        零 walk、零 compute_file_hash;TTL 過期或被 invalidate_scan_cache()
        主動失效後才 fresh 掃描。TTL=0 時每次都 fresh(行為同舊版)。
        """
        if self._indexed_file_hashes is None:
            return
        current_files = self._scan_code_files()
        current_hashes = {
            rel_path: info["hash"] for rel_path, info in current_files.items()
        }
        if current_hashes != self._indexed_file_hashes:
            self.build_index(verbose=False, _current_files=current_files)

    def _reset_partial_index(self) -> None:
        """把半成品索引清乾淨,保證下一次 query 一定會重建。

        三個欄位缺一不可:index 非空 → query() 不重建;_indexed_file_hashes 非 None
        → _refresh_if_stale 會拿舊 hash 比對。留任何一個都會讓 server 恢復之後,
        同一個 process 繼續用壞掉的索引。
        """
        self.index = []
        self.embeddings = None
        self._indexed_file_hashes = None

    def _backfill_cached_embedding_gaps(self, embeddings_list: list, *, verbose: bool) -> None:
        """dense 模式下把空 embedding 一次批次補齊(/v1/embeddings,§5-4)。

        空洞的兩個來源,處理方式相同:
        - build_index 的 parse 階段一律不逐筆 embed(空 embedding 佔位);
        - lazy 模式(符號數 > CODE_RAG_LAZY_EMBED_MAX_SYMBOLS)存下的 [],之後
          索引縮小掉回門檻以下、build 轉走 dense 路徑時仍留在 per-file cache。
          不補的話會觸發「refusing zero padding」fail-loud,而且失敗不寫快取,
          重啟也一樣失敗 —— 索引就永久建不起來。

        補算量上限就是 dense 索引的符號數(必然 ≤ lazy 門檻),批次切分依
        EMBED_BATCH_SIZE / EMBED_BATCH_MAX_CHARS 雙預算。
        """
        missing = [i for i, embedding in enumerate(embeddings_list) if not embedding]
        if not missing:
            return
        if verbose:
            print(f"[CODE_RAG] 批次計算 {len(missing)} 個 embedding(/v1/embeddings)")
        texts = [
            _normalize_text_for_cache(self._build_embed_text(self.index[i]))
            for i in missing
        ]
        vectors = self._embed_texts_batched(texts)
        for i, vec in zip(missing, vectors):
            embeddings_list[i] = vec
        # 寫回 per-file 快取,否則 _save_cache 會把洞原樣存回去,下次照樣爆。
        self._sync_embeddings_to_file_cache(embeddings_list)

    def _sync_embeddings_to_file_cache(self, rows: list[list[float]]) -> None:
        by_symbol = {
            (item.get("path"), item.get("symbol"), item.get("line")): row
            for item, row in zip(self.index, rows)
        }
        for cached in self._file_cache.values():
            cached["embeddings"] = [
                by_symbol.get(
                    (symbol.get("path"), symbol.get("symbol"), symbol.get("line")),
                    [],
                )
                for symbol in cached.get("symbols", [])
            ]

    def _materialize_dense_index(self) -> None:
        """Embed every lazy symbol when lexical routing has no meaningful signal.

        缺 embedding 的符號統一走 /v1/embeddings 批次(§5-4),不再逐筆
        round trip;已有 embedding 的直接沿用。
        """
        rows: list[list[float] | None] = [None] * len(self.index)
        missing_indices = []
        missing_texts = []
        for index, item in enumerate(self.index):
            embedding = item.get("embedding")
            if embedding:
                rows[index] = [float(value) for value in embedding]
            else:
                missing_indices.append(index)
                missing_texts.append(
                    _normalize_text_for_cache(self._build_embed_text(item))
                )
        vectors = self._embed_texts_batched(missing_texts)
        for index, vec in zip(missing_indices, vectors):
            if not vec:
                raise RuntimeError(f"Code RAG symbol {index} returned an empty embedding")
            rows[index] = vec
        dimensions = {len(row) for row in rows}
        if len(dimensions) != 1:
            raise RuntimeError(
                f"Code RAG embedding dimension mismatch: {sorted(dimensions)}; "
                "refusing a mixed dense index"
            )

        self._sync_embeddings_to_file_cache(rows)
        if HAS_NUMPY and rows:
            matrix = np.asarray(rows, dtype=np.float32)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            if np.any(norms <= 0):
                raise RuntimeError("Code RAG dense index contains a zero-norm embedding")
            self.embeddings = matrix / norms
            self._embeddings_normalized = True
            for item in self.index:
                item.pop("embedding", None)
        else:
            self.embeddings = None
            for item, row in zip(self.index, rows):
                item["embedding"] = row
        self._lazy_embed = False
        self._save_cache()

    def _extract_code_tokens(self, text: str) -> set:
        """從問題中提取可能是程式碼的 token

        改進：tokenize snake_case 和 camelCase
        """
        # 先提取完整的 identifier
        raw_tokens = set(re.findall(r'[A-Za-z_][A-Za-z0-9_]{2,}', text))
        stopwords = {'the', 'and', 'for', 'this', 'that', 'with', 'from', 'are', 'was',
                     'how', 'why', 'what', 'where', 'when', 'which', 'can', 'could',
                     'should', 'would', 'will', 'have', 'has', 'had', 'does', 'did',
                     'not', 'but', 'use', 'using', 'used', 'function', 'class', 'method',
                     '這個', '那個', '如何', '為什麼', '什麼', '怎麼'}

        result = set()
        for token in raw_tokens:
            if token.lower() in stopwords:
                continue
            result.add(token)
            # 分解 snake_case
            if '_' in token:
                for part in token.split('_'):
                    if len(part) >= 3 and part.lower() not in stopwords:
                        result.add(part)
            # 分解 camelCase/PascalCase
            camel_parts = re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)', token)
            for part in camel_parts:
                if len(part) >= 3 and part.lower() not in stopwords:
                    result.add(part)

        return result

    def _tokenize_identifier(self, identifier: str) -> set:
        """將 identifier 分解為 token（snake_case、camelCase、數字切分）"""
        tokens = {identifier.lower()}
        # snake_case
        if '_' in identifier:
            for part in identifier.split('_'):
                if len(part) >= 2:
                    tokens.add(part.lower())
        # camelCase/PascalCase
        camel_parts = re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|[0-9]+', identifier)
        for part in camel_parts:
            if len(part) >= 2:
                tokens.add(part.lower())
        return tokens

    def _token_match_score(self, code_tokens: set, item: dict) -> float:
        """計算字面匹配分數

        改進：
        - 使用 token boundary 匹配而非 substring，避免 'log' 命中 'catalog'
        - 加入 context tokens 匹配，提高召回率
        """
        if not code_tokens:
            return 0.0

        symbol = item.get("symbol", "")
        path = item.get("path", "")

        # 將 symbol 和 path 分解為 tokens
        target_tokens = self._tokenize_identifier(symbol)
        # path 中提取檔名部分
        path_name = Path(path).stem if path else ""
        target_tokens.update(self._tokenize_identifier(path_name))

        # 掃 canonical 欄位而不是只掃 context(§6 P3A-1):leading comment 只放在
        # 獨立欄位的話,lexical lane 會完全看不到註解訊號 —— 而那條 lane 沒進步
        # 的原因不會出現在任何報表上。
        scan_text = lexical_scan_text(item, config.CODE_RAG_LEXICAL_SCAN_MAX_CHARS)
        context_tokens = set()
        context_identifiers = re.findall(r'[A-Za-z_][A-Za-z0-9_]{2,}', scan_text)
        for ident in context_identifiers[:config.CODE_RAG_LEXICAL_MAX_IDENTIFIERS]:
            context_tokens.update(self._tokenize_identifier(ident))

        # 計算 token 級別的精確匹配
        hits = 0
        context_hits = 0
        exact_symbol_match = False

        for t in code_tokens:
            t_lower = t.lower()
            # 完全匹配 symbol（忽略大小寫）
            if t_lower == symbol.lower():
                exact_symbol_match = True
                hits += 2  # 完全匹配加倍分數
            elif t_lower in target_tokens:
                hits += 1
            elif t_lower in context_tokens:
                context_hits += 1  # context 匹配分數較低

        # 如果完全匹配 symbol，直接給高分
        if exact_symbol_match:
            return 1.0

        # 計算最終分數：target_tokens 匹配優先，context 匹配作為補充
        base_score = hits / len(code_tokens)
        context_bonus = context_hits / len(code_tokens) * 0.3  # context 匹配只算 30% 權重

        return min(1.0, base_score + context_bonus)

    def _get_embedding_at(self, idx: int) -> list:
        """取得指定索引的 embedding（相容新舊格式）"""
        # 新格式：從 numpy array 取得
        if HAS_NUMPY and self.embeddings is not None and idx < len(self.embeddings):
            return self.embeddings[idx].tolist()
        # 舊格式：從 index 取得
        if idx < len(self.index):
            return self.index[idx].get('embedding', [])
        return []

    def _check_reranker_available(self) -> bool:
        """檢查 reranker server 是否 ready(/health 200 + status=ok)。"""
        if not hasattr(self, '_reranker_available'):
            try:
                self._reranker_available = llama_client.is_ready(LLAMA_RERANK_BASE_URL)
            except Exception:
                self._reranker_available = False
        return self._reranker_available

    def _should_rerank(self, candidates: list, top_k: int) -> bool:
        """判斷是否需要 rerank（避免不必要的 API 呼叫）

        觸發條件：
        1. top_score < 0.6（不夠確信）
        2. 前幾名分數太接近（差距 < 0.05）
        """
        if len(candidates) <= top_k:
            return False

        if RERANKER_ALWAYS_ON:
            return True

        top_score = candidates[0][0] if candidates else 0
        if top_score >= 0.85:
            # 已經很有信心，不需要 rerank
            return False

        # 前幾名分數太接近需要 rerank
        if len(candidates) >= 3:
            score_diff = candidates[0][0] - candidates[2][0]
            if score_diff < 0.05:
                return True

        return top_score < 0.6

    @staticmethod
    def _fusion_candidates(candidates: list, top_k: int) -> list[RankedCandidate]:
        """未執行 rerank 的路徑:score_source="fusion",rerank_score=None。"""
        return [
            RankedCandidate(
                item=item,
                rerank_score=None,
                combined_score=combined,
                final_score=combined,
                score_source="fusion",
            )
            for combined, _emb, _kw, item in candidates[:top_k]
        ]

    def _rerank_code_fallback(self, candidates: list, top_k: int, reason: str) -> list[RankedCandidate]:
        """Fallback for Code RAG rerank. main_model is intentionally embedding here."""
        if config.RERANK_FALLBACK_POLICY == "error":
            raise RuntimeError(
                "Code RAG reranker unavailable and AICODE_RERANK_FALLBACK_POLICY=error. "
                f"Reason: {reason}"
            )
        return self._fusion_candidates(candidates, top_k)

    def _rerank_code_candidates(self, question: str, candidates: list, top_k: int) -> list[RankedCandidate]:
        """使用 reranker 模型對程式碼候選進行二次排序

        Args:
            question: 使用者問題
            candidates: [(combined_score, emb_score, kw_score, item), ...]
            top_k: 返回數量

        Returns:
            list[RankedCandidate](§5-1):分數 query-local,絕不寫回 self.index
            的持久 item;cache 檔因此不可能出現 rerank 欄位。實際 rerank 過的
            score_source="rerank"、final_score=rerank_score(0.0 是有效低分);
            否則 "fusion"、rerank_score=None。
        """
        if not candidates:
            return []

        if not USE_RERANKER or len(candidates) <= top_k:
            return self._fusion_candidates(candidates, top_k)

        # 條件觸發：判斷是否真的需要 rerank
        if not self._should_rerank(candidates, top_k):
            return self._fusion_candidates(candidates, top_k)

        # 減少 rerank 的 candidates 數量
        rerank_count = min(15, top_k * 3)

        if self._check_reranker_available():
            try:
                # 一次把 candidates 全部送進 /reranking 端點,llama-server 端
                # 用 cross-encoder 一次 forward 完所有 (query, passage) pair,
                # 不用 per-item HTTP round trip。
                items = candidates[:rerank_count]
                passages = []
                for combined, emb_score, kw_score, item in items:
                    symbol = item.get('symbol', '')
                    path = item.get('path', '')
                    parent_info = f" in {item.get('parent', '')}" if item.get('parent') else ""
                    sym_type = item.get('type', 'function')
                    # 同一組 canonical 欄位,但 cross-encoder 有自己的預算 ——
                    # header 已經帶了 symbol/path,body 只補其餘欄位。
                    body = render_semantic_fields(
                        {k: v for k, v in item.items()
                         if k not in ('path', 'type', 'symbol', 'parent')},
                        config.CODE_RERANK_PASSAGE_MAX_CHARS,
                    )
                    passages.append(
                        f"{sym_type} {symbol}{parent_info}\nFile: {path}\n{body}"
                    )

                scores = llama_client.rerank(
                    base_url=LLAMA_RERANK_BASE_URL,
                    query=question,
                    documents=passages,
                    model=RERANKER_MODEL,
                    timeout=60,
                )
                ranked = [
                    RankedCandidate(
                        item=items[i][3],
                        rerank_score=float(score),
                        combined_score=items[i][0],
                        final_score=float(score),
                        score_source="rerank",
                    )
                    for i, score in enumerate(scores)
                ]
                ranked.sort(reverse=True, key=lambda rc: rc.final_score)
                return ranked[:top_k]

            except Exception as exc:
                return self._rerank_code_fallback(
                    candidates, top_k, f"dedicated reranker call failed: {exc}"
                )

        # Code RAG 沒有主模型 rerank 路徑;main_model policy 在這裡等同 embedding。
        return self._rerank_code_fallback(candidates, top_k, "dedicated reranker is not reachable")

    def query(self, question: str, top_k: int = CODE_RAG_TOP_K, is_bug_fix: bool = False) -> list[dict]:
        """查詢相關程式碼位置（動態門檻 + reranker 二次排序）

        回傳預設 shape(§8.1):每筆恰為 {path, symbol, type, line, score},
        條件式附 end_line / parent;score = round(final_score, 3)。
        要拿 score_components / backend 等 evidence,呼叫 query_ranked。
        """
        ranked = self.query_ranked(question, top_k=top_k, is_bug_fix=is_bug_fix)
        results = []
        for rc in ranked:
            item = rc.item
            result_item = {
                'path': item['path'],
                'symbol': item['symbol'],
                'type': item['type'],
                'line': item['line'],
                'score': round(rc.final_score, 3),
            }
            # 新增 end_line 和 parent（如果有）
            if 'end_line' in item:
                result_item['end_line'] = item['end_line']
            if 'parent' in item:
                result_item['parent'] = item['parent']
            results.append(result_item)
        return results

    def query_ranked(self, question: str, top_k: int = CODE_RAG_TOP_K,
                     is_bug_fix: bool = False) -> list[RankedCandidate]:
        """query 的完整結果(RankedCandidate;§8 evidence 模式的資料來源)。

        Lazy build：第一次 query 時才建立索引，避免不需要 CodeRAG 時浪費時間
        """
        # Lazy build：第一次 query 時才建立索引
        if not self.index:
            self.build_index(verbose=True)
            # build 後若仍無索引（空專案），返回空
            if not self.index:
                return []
        else:
            self._refresh_if_stale()
            if not self.index:
                return []

        q_emb = self._get_embedding(question)

        code_tokens = self._extract_code_tokens(question)
        code_tokens_lower = {t.lower() for t in code_tokens}
        kw_scores = None

        if self._lazy_embed:
            kw_scores = []
            explicit_indices = []
            for i, item in enumerate(self.index):
                kw_score = self._token_match_score(code_tokens, item)
                kw_scores.append(kw_score)
                symbol_lower = item.get("symbol", "").lower()
                if symbol_lower and symbol_lower in code_tokens_lower:
                    explicit_indices.append(i)

            if not any(score > 0 for score in kw_scores):
                # All lexical scores tied at zero: slicing the first N symbols
                # is arbitrary and makes dense recall effectively random.
                self._materialize_dense_index()
            else:
                if self._lazy_embed_top_k > 0 and self.index:
                    import heapq
                    lazy_top_k = min(self._lazy_embed_top_k, len(self.index))
                    cand_indices = heapq.nlargest(
                        lazy_top_k, range(len(self.index)), key=lambda idx: kw_scores[idx]
                    )
                else:
                    cand_indices = []

                cand_set = {i for i in cand_indices if kw_scores[i] > 0}
                cand_set.update(explicit_indices)
                for idx in cand_set:
                    item = self.index[idx]
                    if not item.get("embedding"):
                        emb = self._get_embedding(self._build_embed_text(item))
                        item["embedding"] = emb

        # 動態門檻：Bug 類問題稍微放寬
        threshold = CODE_RAG_THRESHOLD_BUG if is_bug_fix else CODE_RAG_THRESHOLD

        # 使用 numpy 向量化計算 cosine similarity（如果可用）
        if HAS_NUMPY and self.embeddings is not None and len(self.embeddings) > 0 and not self._lazy_embed:
            q_vec = np.array(q_emb, dtype=np.float32)

            # 如果 embeddings 已經預先 L2 normalize，只需要 normalize query 然後做 dot product
            if getattr(self, '_embeddings_normalized', False):
                q_norm = np.linalg.norm(q_vec)
                if q_norm > 0:
                    q_vec = q_vec / q_norm
                emb_scores = np.dot(self.embeddings, q_vec)
            else:
                # 舊的方式：計算完整的 cosine similarity
                dot_products = np.dot(self.embeddings, q_vec)
                norms = np.linalg.norm(self.embeddings, axis=1) * np.linalg.norm(q_vec)
                norms = np.where(norms > 0, norms, 1.0)
                emb_scores = dot_products / norms
        else:
            emb_scores = None

        scores = []
        for i, item in enumerate(self.index):
            # 取得 embedding score
            if emb_scores is not None:
                emb_score = float(emb_scores[i])
            else:
                emb = self._get_embedding_at(i)
                if emb:
                    sim = sum(a * b for a, b in zip(q_emb, emb))
                    norm_q = sum(x*x for x in q_emb) ** 0.5
                    norm_e = sum(x*x for x in emb) ** 0.5
                    emb_score = sim / (norm_q * norm_e) if norm_q > 0 and norm_e > 0 else 0
                else:
                    emb_score = 0

            kw_score = kw_scores[i] if kw_scores is not None else self._token_match_score(code_tokens, item)

            # 明確點名 symbol 時，大幅提高權重
            # 改進：用 symbol 精確匹配（正則邊界）而非單純 kw_score 門檻
            # 避免短 query 或中文問題下 kw_score 門檻誤收噪音
            symbol = item.get("symbol", "")
            symbol_lower = symbol.lower()
            is_explicit_mention = symbol_lower in code_tokens_lower

            combined, _rule = hybrid_symbol_score(
                emb_score=emb_score,
                kw_score=kw_score,
                item_type=item.get('type', ''),
                is_explicit_mention=is_explicit_mention,
                code_token_count=len(code_tokens),
            )

            scores.append((combined, emb_score, kw_score, item))

        scores.sort(reverse=True, key=lambda x: x[0])

        # 改進：短 query 判定使用 code_tokens 數量而非 split()
        # 中文問題用 split() 會被判成 1 個字串，導致誤判
        is_short_query = len(code_tokens) <= 2

        # 先做初步過濾（門檻篩選）
        candidates_for_rerank = select_scored_candidates(
            scores,
            threshold=threshold,
            top_k=top_k,
            is_short_query=is_short_query,
            code_tokens_lower=code_tokens_lower,
        )

        # 使用 reranker 二次排序（條件觸發)。分數是 query-local 的
        # RankedCandidate(§5-1),絕不寫回 self.index 的持久 item。
        return self._rerank_code_candidates(question, candidates_for_rerank, top_k)

    def get_candidates_prompt(self, question: str) -> str:
        """生成給 Agent 的候選提示"""
        results = self.query(question)
        if not results:
            return ""

        lines = ["\n[CODE_RAG_CANDIDATES] 可能相關的程式碼位置:"]
        for r in results:
            # 顯示行號範圍（如果有 end_line）
            line_info = f"{r['line']}"
            if 'end_line' in r and r['end_line'] != r['line']:
                line_info = f"{r['line']}-{r['end_line']}"

            # 顯示 parent（如果有）
            parent_info = f" in {r['parent']}" if r.get('parent') else ""

            lines.append(f"  - {r['path']}:{line_info} {r['type']} {r['symbol']}{parent_info} (score: {r['score']})")
        lines.append("[/CODE_RAG_CANDIDATES]\n")
        lines.append("TIP: 可用 read_file 查看上述檔案的具體內容（行號範圍已標示）")

        return "\n".join(lines)
