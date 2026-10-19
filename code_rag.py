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
import operator
import uuid
from dataclasses import dataclass
from pathlib import Path
from functools import lru_cache


_NUMPY_IMPORT_ERROR = ""
try:
    import numpy as np
    HAS_NUMPY = True
except Exception as exc:
    HAS_NUMPY = False
    np = None
    _NUMPY_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

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
from runtime_dependencies import DependencyError
from index_scope import load_index_scope, walk_index_files

# 導入 AST 解析器
from ast_parser import (
    PARSER_SEMANTICS_VERSION, PARSER_BACKEND_POLICY,
    parse_file, get_parser_status, require_parsers_for_paths,
)


def _require_numpy() -> None:
    if not HAS_NUMPY or np is None:
        raise DependencyError(
            "Code RAG requires numpy; install the CodeTrail requirements in this Python environment."
            + (f" {_NUMPY_IMPORT_ERROR}" if _NUMPY_IMPORT_ERROR else "")
        )

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
RETRIEVAL_SCORER_VERSION = 3

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


# fresh 掃描的 racy 視窗。stat 指紋沿用上一次的內容 md5(CodeRAG._fingerprinted_file_hash)
# 只收「mtime 與 ctime 都早於這次掃描開始前 2 秒」的檔:檔案系統的時間戳有粒度
# (kernel coarse clock 的 tick),同一個時間戳內再寫一次、size 又沒變的話,stat 完全
# 看不出來,只有讀內容看得出來。時間戳太新的檔下一次 fresh 掃描照舊讀檔(git 的 racy-git
# 規則)。哪些檔案系統的 ctime 可以信任見 _CTIME_TRUSTED_FILESYSTEMS。
_SCAN_RACY_WINDOW_NS = 2_000_000_000

# stat 指紋的前提:st_ctime 是檔案系統如實維護的「最後一次變動時間」(寫入、utime 都會把
# 它推到現在),而且 stat 回的是當下的屬性。不是每個檔案系統都成立:vfat / exFAT 在 Linux
# 上 ctime 跟著 mtime 走(touch -r 會一起改回去);NFS 的屬性快取讓 stat 最長 acregmax 秒
# 都回舊值(讀內容則會經 close-to-open 重新驗證);FUSE / overlay / SMB / 9p 各有語意;
# Windows 的 st_ctime 是建立時間。命中這些地方的舊指紋就是無聲的過期索引,所以只信任下面
# 這些本機檔案系統的裝置(依 /proc/self/mountinfo 判定),其他裝置與讀不到 mountinfo 的
# 平台一律照舊每次讀內容。
_CTIME_TRUSTED_FILESYSTEMS = frozenset({
    "ext2", "ext3", "ext4", "xfs", "btrfs", "tmpfs", "f2fs", "zfs", "bcachefs",
})


def _parse_ctime_trusted_devices(mountinfo: str) -> frozenset[int]:
    """/proc/self/mountinfo 的內容 → 檔案系統類型在白名單裡的 st_dev 集合。

    每行:mount ID、parent ID、major:minor、root、mount point、options、(optional
    fields)、"-"、fstype、source、super options。欄位內的空白會被轉義成 ``\\040``,
    所以 " - " 只會是分隔。格式不符的行略過(= 不信任)。
    """
    devices = set()
    for line in mountinfo.splitlines():
        head, separator, tail = line.partition(" - ")
        if not separator:
            continue
        fields = head.split()
        fstype = tail.split(" ", 1)[0]
        if len(fields) < 5 or fstype not in _CTIME_TRUSTED_FILESYSTEMS:
            continue
        major, colon, minor = fields[2].partition(":")
        if colon and major.isdigit() and minor.isdigit():
            devices.add(os.makedev(int(major), int(minor)))
    return frozenset(devices)


def _ctime_trusted_devices() -> frozenset[int]:
    """這一次 fresh 掃描可以信任 stat 指紋的裝置。讀不到 mountinfo 就是空集合(全部照舊讀內容)。"""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as handle:
            return _parse_ctime_trusted_devices(handle.read())
    except OSError:
        return frozenset()


def _stat_fingerprint(st: os.stat_result) -> tuple[int, int, int, int, int]:
    """(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)。

    ctime 是這一組的關鍵:``touch -r`` / ``rsync -t`` / ``cp -p`` 能把 mtime 改回去,
    ctime 卻一定是改寫當下;編輯器的原子寫入(暫存檔 + rename)則換 inode。
    """
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _has_vector(row) -> bool:
    """這一列是不是真的有向量(None / 長度 0 = 缺)。

    NPZ 還原的列是 float32 ndarray view(_restore_embeddings_from_npz 不再 tolist),
    ndarray 不能拿來做 truthiness —— 超過一個元素直接 ValueError,只有一個元素時則變成
    「值是不是 0」。其他型別維持舊的 ``bool(row)`` 語意(list / None / 舊 cache 的佔位)。
    """
    if row is None:
        return False
    if np is not None and isinstance(row, np.ndarray):
        return len(row) > 0
    return bool(row)


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


# ============================================================
# 檢索路徑(trace):query_ranked(trace=dict) 會把整個候選池(含落選者)、各通道
# 分數(embedding / lexical / 融合 / rerank)、門檻與 reranker 設定填進呼叫端給的
# dict,供 data flywheel 記錄。不帶程式碼文字:路徑 + 行號就能回 repo 取證據,
# 快照另由 data_flywheel 依 trace["files"] 存。
# ============================================================
#: 候選池記到 combined 排序的前幾名。整個 index 可能上千個 symbol,全記沒有意義;
#: 200 已遠超 rerank pool 與 top_k,「正解落在哪一段」一定看得到。
TRACE_MAX_POOL = 200


def _trace_key(item: dict) -> tuple:
    return (item.get("path"), item.get("line"), item.get("symbol", ""))


def _trace_add_files(trace: dict, paths, hashes: dict | None = None, *,
                     hash_algorithm: str = "code_rag_md5_v1") -> None:
    """把要快照的檔加進 trace["files"](去重;帶**索引時**的內容雜湊,快照端拿它核對
    工作樹現在的內容是不是搜尋時那一版)。這裡不設上限:最終結果 + reranker 評過的 +
    context evidence 三者之和有限;快照端的上限有缺席標記(blobs_truncated)。"""
    files = trace.setdefault("files", [])
    seen = {entry["path"] for entry in files if isinstance(entry, dict)}
    hashes = hashes or {}
    for path in paths:
        if not path or path in seen:
            continue
        seen.add(path)
        files.append({"path": path, "index_hash": hashes.get(path),
                      "index_hash_algorithm": hash_algorithm})


def _finish_code_trace(trace: dict, ranked: list, hashes: dict | None = None, *,
                       hash_algorithm: str = "code_rag_md5_v1") -> None:
    """把最終結果標回候選池、列出最終清單與要快照的檔案。"""
    final_keys = {_trace_key(rc.item) for rc in ranked}
    for row in trace.get("pool", []):
        if (row["path"], row["line"], row["symbol"]) in final_keys:
            row["final"] = True
    trace["final"] = [
        {"path": rc.item.get("path"), "line": rc.item.get("line"),
         "symbol": rc.item.get("symbol", ""), "final": round(float(rc.final_score), 4),
         "score_source": rc.score_source}
        for rc in ranked
    ]
    paths = [row.get("path") for row in trace["final"]]
    paths += [r.get("path") for r in trace.get("pool", []) if r.get("rerank") is not None]
    _trace_add_files(trace, paths, hashes, hash_algorithm=hash_algorithm)
    trace["stage"] = "done"


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


# ============================================================
# lexical 通道(字面匹配)的共用零件
#
# 逐筆參考實作 CodeRAG._token_match_score(eval 與測試直接呼叫)與 query_ranked 用的
# 倒排索引(CodeRAG._lexical_scores)共用同一份斷詞(_lexical_profile)與同一個計分
# 運算式(_lexical_score),兩條路的分數因此逐位元相同,不是「大致相同」。
# ============================================================
_LEXICAL_IDENTIFIER_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]{2,}')
_LEXICAL_MISSING = object()


def _lexical_knobs() -> tuple[int, int]:
    """lexical scorer 的兩個預算。查詢當下從 config 讀(行程內可被改),不在 def 時 snapshot。"""
    return (config.CODE_RAG_LEXICAL_SCAN_MAX_CHARS, config.CODE_RAG_LEXICAL_MAX_IDENTIFIERS)


def _lexical_score(hits: int, context_hits: int, token_count: int,
                   exact_symbol_match: bool) -> float:
    """字面匹配分數的唯一運算式(兩條路都走這裡,運算順序因此不可能分岔)。"""
    # 如果完全匹配 symbol，直接給高分
    if exact_symbol_match:
        return 1.0
    # 計算最終分數：target_tokens 匹配優先，context 匹配作為補充
    base_score = hits / token_count
    context_bonus = context_hits / token_count * 0.3  # context 匹配只算 30% 權重
    return min(1.0, base_score + context_bonus)


def _lexical_content_key(item: dict) -> tuple | None:
    """lexical profile 的內容身分:_lexical_profile 讀到的每個欄位原值。

    profile 是「這些欄位 + 兩個預算」的純函式,所以內容相同就能沿用 profile。
    不用 item 物件身分:build_index 每次都從磁碟重載 per-file cache(_load_cache),
    沒變的檔重建後也是新的 dict,物件身分在增量 build 之後一定對不上。
    欄位集合就是 CANONICAL_SEMANTIC_FIELDS;_lexical_profile 讀的欄位不得超出它
    (tests/test_code_rag_search.py 以記錄存取的 dict 守住)。缺鍵與 None 分開記;
    非 str 的值(不該出現)回 None = 不沿用,照常斷詞。
    """
    key = tuple(item.get(field, _LEXICAL_MISSING) for field in CANONICAL_SEMANTIC_FIELDS)
    for value in key:
        if value is not _LEXICAL_MISSING and value is not None and type(value) is not str:
            return None
    return key


@dataclass(frozen=True, eq=False)
class _LexicalIndex:
    """lexical 通道的倒排索引。三組 postings 對同一個 item 互斥,對應逐筆計分的
    if / elif / elif:symbol 完全相同 → target token → context token。"""
    items: tuple            # 建索引時的 index 序列(查詢時逐一比對物件身分)
    knobs: tuple            # 建索引時的 (scan_max_chars, max_identifiers)
    exact: dict             # symbol_lower → [位置]
    target: dict            # symbol / path stem 的 token(不含 symbol_lower)→ [位置]
    context: dict           # canonical 欄位 identifier 的 token(不含 target 已有的)→ [位置]


def index_slice_aligns(index: list, cursor: int, symbols: list) -> bool:
    """``index[cursor:cursor+len(symbols)]`` 是不是就是 ``symbols`` 本身。

    向量在 .npz 裡是以 ``self.index`` 的**列序**存的,而 ``self.index`` 就是各檔
    ``symbols`` 的串接 —— 還原因此是純位置對映,不需要任何鍵。這個函式只是把
    「串接」這個不變式在讀盤時再驗一次:磁碟上的 meta 不可信,錯位比重建糟。
    """
    if cursor + len(symbols) > len(index):
        return False
    for offset, symbol in enumerate(symbols):
        item = index[cursor + offset]
        if (item.get("path"), item.get("symbol"), item.get("line"),
                item.get("type")) != (symbol.get("path"), symbol.get("symbol"),
                                      symbol.get("line"), symbol.get("type")):
            return False
    return True


def cache_identity() -> dict:
    """持久 cache 的身分欄位 —— **單一來源**,寫入端與驗證端都從這裡取。

    刻意包含**實際的預算值**而不是只有 schema 版本:這些預算是 `config.py` 的常數
    (`CODE_RAG_EMBED_TEXT_MAX_CHARS` 等),只鎖 schema version 的話,重啟
    時改一個環境變數就會靜默沿用「用另一組 render 算出來的」embedding —— 沒有
    任何訊息會提醒你現在查的是舊向量。

    兩邊各寫一份的另一個失敗模式同樣無聲:加了欄位而測試 fixture 沒跟上,舊 cache
    被拒 → 那條測試改走 full rebuild,「還是綠的」卻不再驗它本來要驗的東西。
    """
    return {
        "schema_version": CODE_RAG_CACHE_SCHEMA_VERSION,
        "embedding_model": EMBEDDING_MODEL,
        "parser_semantics_version": PARSER_SEMANTICS_VERSION,
        "parser_backend_policy": PARSER_BACKEND_POLICY,
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
                        code_token_count: int,
                        lexical_has_signal: bool) -> tuple[float, str]:
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
    # 一般情況:function 類型給一點優先權 —— 但只在**有 lexical 訊號**時。
    # 純語意 query(kw_score 恆為 0)下,這個平白的 +0.05 是加在一個很窄的
    # cosine 帶上的:真實樹實測全語料 emb 落在 0.35-0.51,+0.05 等於把
    # 每一個 function 都插到 global / macro / typedef 前面。實測
    # 「環境變數怎麼傳給子行程」→ environ.c 的 _environ(global)dense 排名
    # 369,combined 排名卻是 7793;拿掉 bonus 就回到 dense 的名次。
    type_bonus = 0.05 if (item_type == 'function' and lexical_has_signal) else 0.0
    # 整個候選集都沒有 lexical 命中時(純 CJK 問句,或中文夾了語料裡不存在的
    # 英文詞),kw_score 恆為 0。此時仍對半混合等於把 embedding 門檻從 0.35
    # 悄悄抬成 0.70(function 因 bonus 是 0.60)—— 實測 真實樹全語料
    # emb 上限只有 0.5112,於是純中文 query 結構性地永遠回零。
    # 判準是「有沒有實際命中」而非「有沒有抽出 token」:抽得出 token 但零命中
    # 的 query 一樣沒有 lexical 訊號可用。
    kw_weight = 0.5 if lexical_has_signal else 0.0
    return (1.0 - kw_weight) * emb_score + kw_weight * kw_score + type_bonus, "fusion"


def select_scored_candidates(scores: list, *, threshold: float, top_k: int,
                             is_short_query: bool, code_tokens_lower: set,
                             pool_size: int | None = None) -> list:
    """Production 的門檻篩選 + candidate cutoff。純函式,無 self / 無 I/O。

    scores 是已排序的 [(combined, emb_score, kw_score, item), ...]。
    evaluator 的 runtime_hybrid lane 重用它,才會連 cutoff 行為都一致。

    pool_size 是要收給 rerank 的候選上限;None = 取
    config.CODE_RAG_RERANK_CANDIDATE_POOL。舊值是寫死的 ``top_k * 3``。
    """
    if pool_size is None:
        pool_size = config.CODE_RAG_RERANK_CANDIDATE_POOL
    pool_size = max(1, int(pool_size))
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
        if len(selected) >= pool_size:
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
            "Check the embedding llama-server (deployment.json 的 services.embedding)."
        ) from exc

    if not emb:
        raise RuntimeError(
            f"embedding server returned an empty vector at {LLAMA_EMBED_BASE_URL}. "
            "Check the embedding llama-server (deployment.json 的 services.embedding)."
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
    - embedding 使用 numpy .npz 二進位格式（不壓縮：float32 向量壓縮率很低，壓縮只多付 CPU）
    """

    def __init__(self, folder: str, *, build_context=None):
        self.folder = Path(folder).resolve()
        self.build_context = build_context if build_context is not None and build_context.restricts_files else None
        # 索引範圍引擎(Layer C 設定不存在時就是預設行為,不會 fail)
        self.scope = (load_index_scope(self.folder, build_context=self.build_context)
                      if self.build_context is not None else load_index_scope(self.folder))
        # 快取檔案
        cache_base = CODE_RAG_CACHE_FILE.replace('.json', '')
        if self.build_context is not None:
            cache_base += ".build-" + hashlib.sha256(self.build_context.target.encode()).hexdigest()[:20]
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
        # fresh 掃描的 stat 指紋 → 內容 md5:{rel_path: (dev, ino, size, mtime_ns,
        # ctime_ns, hash)}。只有 _scan_code_files 的 fresh 路徑讀寫(見
        # _fingerprinted_file_hash);build_context 路徑不用它。
        self._hash_memo: dict[str, tuple] = {}
        # lexical 通道:profile 依 (詞彙內容, 兩個預算) 沿用 —— (knobs, {content_key:
        # profile});倒排索引綁「index 的 item 序列 + 兩個預算」(見 _lexical_postings)。
        self._lexical_profiles: tuple = (None, {})
        self._lexical_index: _LexicalIndex | None = None

    def for_build_context(self, context):
        """Independent query view; never mutate the process-wide default target."""
        if not context.restricts_files:
            return self
        context.assert_fresh()
        return CodeRAG(str(self.folder), build_context=context)

    # 保留成 class attribute:既有測試與呼叫端都靠它。
    _CONTENT_HASH_MAX_BYTES = CONTENT_HASH_MAX_BYTES

    def _compute_file_hash(self, filepath: Path) -> str:
        """計算單一檔案的 hash（用於增量快取驗證）。見 compute_file_hash。"""
        if self.build_context is not None:
            rel = filepath.relative_to(self.folder).as_posix()
            return self.build_context.dependencies.get(rel) or ""
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

        fresh 掃描對小檔先比 stat 指紋:與上一次 fresh 掃描記下的相同就沿用那次的
        內容 md5,不同才照舊讀檔(_fingerprinted_file_hash;hash 字串不變)。只在 ctime
        語意可信的本機檔案系統上這麼做,其他裝置每次照舊讀內容。
        """
        ttl = int(getattr(config, "CODE_RAG_REFRESH_TTL_SECONDS", 30))
        if self.build_context is not None:
            self.build_context.assert_fresh()
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
        previous_memo = self._hash_memo
        fresh_memo: dict[str, tuple] = {}
        # racy 截止點以「本次掃描開始」為準:掃描途中才被改的檔,時間戳一定晚於它。
        racy_cutoff_ns = _time.time_ns() - _SCAN_RACY_WINDOW_NS
        trusted_devices = (_ctime_trusted_devices() if self.build_context is None
                           else frozenset())
        for filepath, rel_path in walk_index_files(self.scope):
            # §5-5:無 parser 的檔案不入 symbol 掃描(grep/list_dir 不受影響)
            selected_code = (self.build_context is not None and
                             self.build_context.parser_language(rel_path) in ("c", "cpp"))
            if not file_kind_policy.enters_symbol_scan(rel_path) and not selected_code:
                continue
            if self.build_context is not None:
                # build_context 的 hash 來自 import 時綁定的 dependencies,不走 stat 指紋
                file_hash = self._compute_file_hash(filepath)
            else:
                file_hash = self._fingerprinted_file_hash(
                    filepath, rel_path, previous_memo, fresh_memo, racy_cutoff_ns,
                    trusted_devices,
                )
            if file_hash:
                result[rel_path] = {"filepath": filepath, "hash": file_hash}

        if self.build_context is None:
            # 只留這次走訪到、而且可沿用的路徑:刪掉 / 移出範圍 / 時間戳太新的檔都不殘留
            self._hash_memo = fresh_memo
        _INDEX_SCAN_CACHE[cache_key] = {
            "entries": {rel_path: info["hash"] for rel_path, info in result.items()},
            "timestamp": _time.time(),
        }
        return result

    def _fingerprinted_file_hash(self, filepath: Path, rel_path: str, previous: dict,
                                 fresh: dict, racy_cutoff_ns: int,
                                 trusted_devices: frozenset) -> str:
        """fresh 掃描用的 _compute_file_hash:stat 指紋沒變就沿用上一次的內容 md5。

        沒命中時照舊呼叫 _compute_file_hash(同一個 compute_file_hash,hash 字串完全
        相同),前後各 stat 一次:讀檔期間指紋沒變(s1 == s2),而且 mtime / ctime 都早於
        racy 截止點(本次掃描開始前 _SCAN_RACY_WINDOW_NS)才記進 ``fresh``;其他情況
        下次 fresh 掃描照舊讀檔。檔案不在 ``trusted_devices``(ctime 語意可信的本機檔案
        系統,見 _CTIME_TRUSTED_FILESYSTEMS)上就不沿用也不記,每次照舊讀內容。

        用 stat(跟隨 symlink)而不是 lstat:索引收 root 內指向一般檔的 symlink
        (index_scope 以 resolve + is_file 判定),內容是經 link 讀到的目標檔,指紋必須
        是被讀的那個檔 —— lstat 看的是 link 自己,目標被改寫時它不變。
        大檔(> CONTENT_HASH_MAX_BYTES)本來就只 stat、不讀內容,照舊走 _compute_file_hash。
        """
        try:
            before = os.stat(filepath)
        except OSError:
            return self._compute_file_hash(filepath)
        if before.st_dev not in trusted_devices:
            return self._compute_file_hash(filepath)
        fingerprint = _stat_fingerprint(before)
        small = before.st_size <= self._CONTENT_HASH_MAX_BYTES
        if small:
            entry = previous.get(rel_path)
            if entry is not None and entry[:5] == fingerprint:
                fresh[rel_path] = entry
                return entry[5]
        file_hash = self._compute_file_hash(filepath)
        if not small or not file_hash:
            return file_hash
        try:
            after = os.stat(filepath)
        except OSError:
            return file_hash
        if (_stat_fingerprint(after) == fingerprint
                and max(before.st_mtime_ns, before.st_ctime_ns) < racy_cutoff_ns):
            fresh[rel_path] = (*fingerprint, file_hash)
        return file_hash

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
        _require_numpy()
        self._scope_fingerprint_ok = False
        if not self.cache_meta_file.exists():
            return {}

        try:
            with open(self.cache_meta_file, 'r', encoding='utf-8') as f:
                meta = json.load(f)
        except MemoryError:
            # 記憶體不足 != cache 壞掉。當成「損壞」會靜默丟棄一份完全有效的
            # cache,接著整棵樹重建;重建後是 lazy 索引,_save_cache 的 lazy
            # 分支會 unlink 掉既有的 .npz 並以無 embedding 的 meta 覆蓋原檔 ——
            # 一次暫態記憶體不足就永久刪掉全部向量,而訊息還說是檔案損壞。
            # 實測:330270 符號的樹 meta JSON 22.9GB,光 json.load 就要 100GB
            # 以上位址空間,在忙碌的機器上這是真的會發生。一律 fail-loud。
            raise
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

        expected_build = self.build_context.fingerprint if self.build_context is not None else None
        if meta.get("build_context_fingerprint") != expected_build:
            # Header/macro changes can alter symbols even with unchanged source
            # bytes. Per-file cache reuse is unsafe across this boundary.
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

        file_cache = meta.get("file_cache", {})
        require_parsers_for_paths(file_cache, **({"build_context": self.build_context}
                                               if self.build_context is not None else {}))
        # 新格式的 dense cache 不在 JSON 存向量(見 _save_cache),要從 .npz
        # 還原。舊 cache 仍夾帶 embeddings —— 原樣沿用,不必為了換格式重建。
        if npz_md5 is not None and any(
            "embeddings" not in cached for cached in file_cache.values()
        ):
            if not self._restore_embeddings_from_npz(meta.get("index", []), file_cache):
                return {}
        return file_cache

    def _restore_embeddings_from_npz(self, index: list, file_cache: dict) -> bool:
        """把 .npz 的向量還原回 per-file cache。回傳 False 代表不可信 → 重建。

        **位置對映**,不是鍵查表。曾經用 ``(path, symbol, line)`` 當 dict key,
        但那個鍵不唯一:``typedef enum {...} Boolean;`` 一行會產生 typedef 與
        enum 兩個同名同行的符號(真實樹實測 213 組碰撞 / 476 個符號),
        dict 後寫覆蓋前寫 → 263 個符號拿到別人的向量,而且筆數對得上、shape
        檢查過得了,完全無聲。npz 的列序就是 index 的序,位置對映沒有這個問題。
        """
        _require_numpy()
        try:
            with np.load(self.cache_emb_file) as data:
                matrix = data["embeddings"]
        except MemoryError:
            # 與 json.load 同一個理由:記憶體不足不是「壞掉」,靜默重建會把
            # 既有向量覆蓋掉。fail-loud。
            raise
        except Exception as e:
            print(f"[CODE_RAG] cache NPZ 解析失敗({type(e).__name__}: {e}),安全重建",
                  file=sys.stderr)
            return False
        if matrix.ndim != 2 or matrix.shape[0] != len(index):
            print(f"[CODE_RAG] cache NPZ 形狀 {matrix.shape} 與 index {len(index)} 筆"
                  "不符,安全重建", file=sys.stderr)
            return False
        # 每列是 matrix 的 view,不 tolist:dense build 的 np.array(..., float32) 直接拿回
        # 同一份位元(以前 tolist 再轉回來,值相同但大樹上要多配一份 8 倍大的 Python
        # float list)。lazy 分支要進 JSON / 純 Python 計分的列由 build_index 自己 tolist。
        rows = list(matrix)
        cursor = 0
        for rel_path, cached in file_cache.items():
            symbols = cached.get("symbols", [])
            if not index_slice_aligns(index, cursor, symbols):
                print("[CODE_RAG] cache index 與 file_cache 不同序(向量會錯位),"
                      "安全重建", file=sys.stderr)
                return False
            cached["embeddings"] = rows[cursor:cursor + len(symbols)]
            cursor += len(symbols)
        if cursor != len(rows):
            print(f"[CODE_RAG] cache file_cache 共 {cursor} 個符號但 NPZ 有 "
                  f"{len(rows)} 列,安全重建", file=sys.stderr)
            return False
        return True

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
        - NPZ 不壓縮(np.savez):float32 向量的尾數近乎隨機,zlib 壓不太動,壓縮只
          多付寫入與每次載入的 CPU。np.load 兩種都讀,舊的壓縮 NPZ 照常載入
          (md5 / row_count 驗證與格式無關)。
        """
        _require_numpy()
        if self.build_context is not None:
            self.build_context.assert_fresh()
        lock_fd = fs_safety.acquire_file_lock(self.cache_lock_file, self.folder)
        try:
            # 上次 crash 可能留下 tmp 殘留;持鎖下只清自家精確前綴
            for stale in self.folder.glob(f"{self.cache_emb_file.name}.tmp*"):
                stale.unlink(missing_ok=True)
            for stale in self.folder.glob(f"{self.cache_meta_file.name}.tmp*"):
                stale.unlink(missing_ok=True)

            npz_md5 = None
            if self.embeddings is not None:
                fd, tmp_npz = tempfile.mkstemp(
                    dir=self.folder,
                    prefix=f"{self.cache_emb_file.name}.tmp",
                    suffix=".npz",
                )
                os.close(fd)
                try:
                    np.savez(tmp_npz, embeddings=self.embeddings)
                    os.replace(tmp_npz, self.cache_emb_file)
                except BaseException:
                    Path(tmp_npz).unlink(missing_ok=True)
                    raise
                npz_md5 = hashlib.md5(self.cache_emb_file.read_bytes()).hexdigest()
            else:
                # lazy / 無 numpy:沒有 dense 矩陣可存。刪掉舊 NPZ,避免留下
                # 與新 meta 不同世代的殘影。
                self.cache_emb_file.unlink(missing_ok=True)

            emb_dim = self.embeddings.shape[1] if self.embeddings is not None else None
            # dense 模式下向量已經在 .npz 裡,再以 JSON 文字存一份是 18 倍膨脹
            # (實測同一批 330270 個向量:.npz 1.25GB vs meta JSON 22.9GB),
            # 而且載入時 json.load 的暫態峰值會衝到 100GB 以上位址空間。
            # 只有 npz 真的寫成功才剝除 —— lazy 模式沒有 .npz 可還原,那些空
            # placeholder 本來也不佔空間,原樣保留。
            # 淺拷貝:symbols 等大物件仍是同一份參考,不額外吃記憶體。
            if npz_md5 is not None:
                file_cache_payload = {
                    rel: {k: v for k, v in cached.items() if k != "embeddings"}
                    for rel, cached in self._file_cache.items()
                }
            else:
                file_cache_payload = self._file_cache
            meta = {
                **cache_identity(),
                "generation_id": uuid.uuid4().hex,
                "scope_fingerprint": self.scope.fingerprint,
                "build_context_fingerprint": self.build_context.fingerprint if self.build_context is not None else None,
                "embedding_dim": emb_dim,
                "npz_md5": npz_md5,
                "row_count": len(self.index),
                "index": self.index,
                "file_cache": file_cache_payload  # 增量快取
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
            ast_symbols = (parse_file(filepath, content, build_context=self.build_context)
                           if self.build_context is not None else parse_file(filepath, content))
        except DependencyError:
            raise
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
            if self.build_context is not None:
                symbol_dict['build_state'] = self.build_context.state_for(rel_path, sym.start_line)
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
                    "Check the embedding llama-server (deployment.json 的 services.embedding)."
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
        content = (self.build_context.read_source(rel_path) if self.build_context is not None
                   else filepath.read_text(encoding='utf-8', errors='replace'))
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
            for field in ('comments', 'linkage', 'condition', 'storage_class', 'build_state'):
                if sym.get(field):
                    index_entry[field] = sym[field]

            file_symbols.append(index_entry)
            file_embeddings.append(emb)

        return file_symbols, file_embeddings

    def build_index(self, verbose: bool = True, _current_files: dict | None = None):
        """建立程式碼索引（支援增量更新）"""
        if not CODE_RAG_ENABLED:
            return

        _require_numpy()
        current_files = _current_files if _current_files is not None else self._scan_code_files_fresh()
        require_parsers_for_paths(current_files, **({"build_context": self.build_context}
                                                  if self.build_context is not None else {}))

        # 嘗試載入快取
        if self._load_cache():
            self._indexed_file_hashes = {
                rel_path: info["hash"] for rel_path, info in current_files.items()
            }
            if verbose:
                print(f"[CODE_RAG] 載入快取: {len(self.index)} 個符號")
            return

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
            # 已依實際索引語言驗過主要 parser；status 只顯示可用能力。
            parser_status = get_parser_status()
            ts_langs = [k for k, v in parser_status['languages'].items() if v == 'tree-sitter']
            if ts_langs:
                print(f"[CODE_RAG] 使用 tree-sitter: {', '.join(ts_langs)}")

            if is_incremental:
                print(f"[CODE_RAG] 增量更新: {len(files_to_index)} 個檔案變更, "
                      f"{len(files_unchanged)} 個未變, {len(files_deleted)} 個已刪除")
            else:
                print(f"[CODE_RAG] 建立程式碼索引... ({len(current_files)} 個檔案)")

        # 收集所有符號和 embeddings
        # lexical 倒排索引握著上一代 index 的 item;index 換新就放掉它(下一次有程式碼 token
        # 的查詢再依 profile 重建),不讓舊世代撐到那時候 —— 純中文查詢不會重建 postings。
        self._lexical_index = None
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

        # embeddings_list 與 self.index 必須逐位置對應 —— 下面的 dense 矩陣
        # 直接用位置取值,長度不一致就是無聲錯位。fail-loud。
        if len(embeddings_list) != len(self.index):
            self._reset_partial_index()
            raise RuntimeError(
                f"Code RAG cache is inconsistent: {len(embeddings_list)} embeddings "
                f"for {len(self.index)} symbols; refusing a misaligned dense index"
            )
        self._retain_lexical_profiles()

        # lazy 的判準是「**還缺幾個**向量」,不是「總共幾個符號」。
        #
        # 看總數的兩個實測後果(真實樹 330270 符號,2026-08-21):
        #   1. 一份已經完整 embed 的索引每次載入都被判成 lazy → dense 矩陣不建、
        #      查詢時重跑 _materialize_dense_index()、結尾全量回寫。
        #   2. 只要**一個檔案**變動(實測 39 個符號)就留下 39 個
        #      空洞,整份索引因此打回 lazy → _save_cache 的 lazy 分支 unlink 掉
        #      1.25GB 的 .npz,並回寫 22.9GB 的舊格式 meta(實測 578.5s/21.34GB)。
        #      _backfill_cached_embedding_gaps 本來就是要補這種空洞的,但它在
        #      not self._lazy_embed 分支裡,舊判準下永遠走不到。
        #
        # 改看缺口之後:首次建索引(全空)照樣 lazy;增量補幾十個則走 dense
        # backfill —— 補算量本來就有上限(≤ 門檻),不會退化成整棵樹重算。
        missing_embeddings = sum(1 for embedding in embeddings_list if not _has_vector(embedding))
        self._lazy_embed = bool(
            lazy_enabled and missing_embeddings > CODE_RAG_LAZY_EMBED_MAX_SYMBOLS
        )

        # 將 embedding 轉換為 numpy array 並預先 L2 normalize
        if embeddings_list and not self._lazy_embed:
            try:
                self._backfill_cached_embedding_gaps(embeddings_list, verbose=verbose)
            except Exception:
                # 和上面 files_to_index 那個 except 同一個契約:embedding 失敗不能
                # 留下半成品索引。留著的話 query() 會因為 self.index 非空而不再
                # 重建(_refresh_if_stale 也因為 _indexed_file_hashes is None 直接
                # return),整個 MCP process 就一路用缺 embedding 的索引降級下去。
                self._reset_partial_index()
                raise
            dimensions = {len(embedding) for embedding in embeddings_list if _has_vector(embedding)}
            if not dimensions or any(not _has_vector(embedding) for embedding in embeddings_list):
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
            # 列可以混著 NPZ 還原的 float32 view 與 backfill / 舊 cache 的 Python list;
            # np.array 一次 stack 成 float32,結果與舊路徑(全部 tolist 再轉回來)逐位元相同。
            normalized = embeddings_list
            self.embeddings = np.array(normalized, dtype=np.float32)

            norms = np.linalg.norm(self.embeddings, axis=1, keepdims=True)
            norms = np.where(norms > 0, norms, 1.0)
            self.embeddings = self.embeddings / norms
            self._embeddings_normalized = True
        else:
            self.embeddings = None
            # lazy 不建 dense 矩陣:向量逐列以純 Python float64 計分,_save_cache 的 lazy
            # 分支也會把 per-file cache 寫成 JSON。NPZ 還原的 float32 view 因此一律先轉回
            # list —— 值與舊路徑的 matrix.tolist() 逐位元相同;留著 ndarray 的話,Python
            # float 乘 np.float32 會變成 float32 運算(分數無聲改變),寫 JSON 則直接失敗。
            if any(isinstance(emb, np.ndarray) for emb in embeddings_list):
                embeddings_list = [emb.tolist() if isinstance(emb, np.ndarray) else emb
                                   for emb in embeddings_list]
                try:
                    # index 與 per-file cache 共用同一份 list(與舊路徑相同)
                    self._sync_embeddings_to_file_cache(embeddings_list)
                except Exception:
                    self._reset_partial_index()
                    raise
            for i, emb in enumerate(embeddings_list):
                if i < len(self.index) and _has_vector(emb):
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

        # 0 檔變更 + 0 檔刪除 = cache 內容與剛讀進來的完全一致,回寫等於把同一份
        # 位元組再寫一次。大樹上這一次就是 22.9GB / 約 100 分鐘,而且每個新
        # process 都會付。有任何 delta(含首次建索引)才存。
        if is_incremental and not files_to_index and not files_deleted:
            pass
        else:
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
        # 倒排索引握著半成品 index 的 item;profile 表留著,重建成功時照樣依內容沿用。
        self._lexical_index = None

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
        missing = [i for i, embedding in enumerate(embeddings_list) if not _has_vector(embedding)]
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
        """把 dense 列寫回 per-file cache。與 _restore_embeddings_from_npz 同一
        套位置對映 —— 用 (path, symbol, line) 當鍵會在同名同行的 typedef/enum
        上無聲錯配(見該函式 docstring)。

        這裡的不變式是程式碼建構出來的(self.index 就是各檔 symbols 的串接),
        不是磁碟資料,所以不對就是 bug,fail-loud 而不是安全重建。

        沒有 per-file cache 就直接 return:純記憶體索引(index 不是由
        build_index 從 cache 串出來的)沒有東西可寫,也就沒有東西會錯位。
        「少寫」不是這裡要防的失敗模式,「寫錯位置」才是。
        """
        if not self._file_cache:
            return
        cursor = 0
        for cached in self._file_cache.values():
            symbols = cached.get("symbols", [])
            if not index_slice_aligns(self.index, cursor, symbols):
                raise RuntimeError(
                    "Code RAG index and per-file cache diverged; "
                    "refusing to write misaligned embeddings"
                )
            cached["embeddings"] = list(rows[cursor:cursor + len(symbols)])
            cursor += len(symbols)
        if cursor != len(rows):
            raise RuntimeError(
                f"Code RAG per-file cache holds {cursor} symbols but {len(rows)} "
                "embedding rows were produced; refusing a misaligned write"
            )

    def _materialize_dense_index(self) -> None:
        """Embed every lazy symbol when lexical routing has no meaningful signal.

        缺 embedding 的符號統一走 /v1/embeddings 批次(§5-4),不再逐筆
        round trip;已有 embedding 的直接沿用。
        """
        _require_numpy()
        rows: list[list[float] | None] = [None] * len(self.index)
        missing_indices = []
        missing_texts = []
        for index, item in enumerate(self.index):
            embedding = item.get("embedding")
            if _has_vector(embedding):
                rows[index] = [float(value) for value in embedding]
            else:
                missing_indices.append(index)
                missing_texts.append(
                    _normalize_text_for_cache(self._build_embed_text(item))
                )
        vectors = self._embed_texts_batched(missing_texts)
        for index, vec in zip(missing_indices, vectors):
            if not _has_vector(vec):
                raise RuntimeError(f"Code RAG symbol {index} returned an empty embedding")
            rows[index] = vec
        dimensions = {len(row) for row in rows}
        if len(dimensions) != 1:
            raise RuntimeError(
                f"Code RAG embedding dimension mismatch: {sorted(dimensions)}; "
                "refusing a mixed dense index"
            )

        self._sync_embeddings_to_file_cache(rows)
        if rows:
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
        """計算字面匹配分數(逐筆參考實作)

        改進：
        - 使用 token boundary 匹配而非 substring，避免 'log' 命中 'catalog'
        - 加入 context tokens 匹配，提高召回率

        query_ranked 走 _lexical_scores(倒排索引,斷詞只做一次);這裡保留逐筆版本
        給 eval(semantic_retrieval / run_code_smoke_eval)與測試直接呼叫,兩者共用
        _lexical_profile 與 _lexical_score,分數逐位元相同。
        """
        if not code_tokens:
            return 0.0
        return self._score_profile(code_tokens, self._lexical_profile(item, _lexical_knobs()))

    def _lexical_profile(self, item: dict, knobs: tuple[int, int]) -> tuple[str, set, set]:
        """item 的詞彙通道 token:(symbol_lower, target_tokens, context_tokens)。

        knobs = (CODE_RAG_LEXICAL_SCAN_MAX_CHARS, CODE_RAG_LEXICAL_MAX_IDENTIFIERS);
        由呼叫端讀一次再傳進來,同一份 profile 不可能混用兩組預算。讀到的欄位不得
        超出 CANONICAL_SEMANTIC_FIELDS(_lexical_content_key 以它們當沿用身分)。
        """
        scan_max_chars, max_identifiers = knobs
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
        scan_text = lexical_scan_text(item, scan_max_chars)
        context_tokens = set()
        context_identifiers = _LEXICAL_IDENTIFIER_RE.findall(scan_text)
        for ident in context_identifiers[:max_identifiers]:
            context_tokens.update(self._tokenize_identifier(ident))
        return symbol.lower(), target_tokens, context_tokens

    @staticmethod
    def _score_profile(code_tokens, profile: tuple[str, set, set]) -> float:
        """逐 token 計次(大小寫不同算不同 token),運算式在 _lexical_score。"""
        if not code_tokens:
            return 0.0
        symbol_lower, target_tokens, context_tokens = profile

        # 計算 token 級別的精確匹配
        hits = 0
        context_hits = 0
        exact_symbol_match = False

        for t in code_tokens:
            t_lower = t.lower()
            # 完全匹配 symbol（忽略大小寫）
            if t_lower == symbol_lower:
                exact_symbol_match = True
                hits += 2  # 完全匹配加倍分數
            elif t_lower in target_tokens:
                hits += 1
            elif t_lower in context_tokens:
                context_hits += 1  # context 匹配分數較低

        return _lexical_score(hits, context_hits, len(code_tokens), exact_symbol_match)

    def _lexical_entry(self, item: dict, knobs: tuple[int, int], vocab: dict) -> tuple:
        """倒排索引用的 profile:(symbol_lower, target 專屬 token, context 專屬 token)。

        三類互斥,對應 _score_profile 的 if / elif / elif —— 某個小寫 token 對這個
        item 只會落進其中一類(或都不落)。token 字串經 vocab 共用同一個物件,大樹上
        省下每個 item 各存一份 "int" / "return" 的記憶體。
        """
        symbol_lower, target_tokens, context_tokens = self._lexical_profile(item, knobs)
        canonical = vocab.setdefault
        target_only = tuple(canonical(token, token) for token in target_tokens
                            if token != symbol_lower)
        context_only = tuple(canonical(token, token) for token in context_tokens
                             if token != symbol_lower and token not in target_tokens)
        return canonical(symbol_lower, symbol_lower), target_only, context_only

    def _lexical_postings(self) -> _LexicalIndex:
        """目前 index 的 lexical 倒排索引(兩個預算在這裡、查詢當下讀)。

        有效 = 預算相同 + index 的 item 序列逐一是同一個物件(C 速度的 operator.is_);
        連 self.index 被原地換掉元素都會重建。index entry 建好之後不就地改 lexical
        欄位(build_index 一律重建 dict;lazy 只動 "embedding" 鍵),所以物件身分就代表
        內容。重建時 profile 依 (_lexical_content_key, 預算) 沿用 —— 增量 build 後只為
        內容真的變了的 item 斷詞;任一預算改了就全部重新斷詞;已不在 index 的 profile
        隨重建丟掉。
        """
        knobs = _lexical_knobs()
        index = self.index
        current = self._lexical_index
        if (current is not None and current.knobs == knobs
                and len(current.items) == len(index)
                and all(map(operator.is_, current.items, index))):
            return current

        stored_knobs, previous = self._lexical_profiles
        if stored_knobs != knobs:
            previous = {}
        profiles: dict = {}
        vocab: dict[str, str] = {}
        exact: dict[str, list[int]] = {}
        target: dict[str, list[int]] = {}
        context: dict[str, list[int]] = {}
        for position, item in enumerate(index):
            key = _lexical_content_key(item)
            entry = None
            if key is not None:
                entry = profiles.get(key)
                if entry is None:
                    entry = previous.get(key)
            if entry is None:
                entry = self._lexical_entry(item, knobs, vocab)
            if key is not None:
                profiles[key] = entry
            symbol_lower, target_only, context_only = entry
            bucket = exact.get(symbol_lower)
            if bucket is None:
                exact[symbol_lower] = [position]
            else:
                bucket.append(position)
            for token in target_only:
                bucket = target.get(token)
                if bucket is None:
                    target[token] = [position]
                else:
                    bucket.append(position)
            for token in context_only:
                bucket = context.get(token)
                if bucket is None:
                    context[token] = [position]
                else:
                    bucket.append(position)
        self._lexical_profiles = (knobs, profiles)
        self._lexical_index = _LexicalIndex(tuple(index), knobs, exact, target, context)
        return self._lexical_index

    def _retain_lexical_profiles(self) -> None:
        """build_index 換了 index 之後:profile 表只留新 index 用得到的那些,key 改指向新
        item 的字串。

        不斷詞、不建 postings(那是下一次有程式碼 token 的查詢才做的事),只是讓上一代的
        item 字串不必撐到那時才放掉。預算不動:之後查詢時預算若已改變,照樣全部重新斷詞。
        """
        knobs, previous = self._lexical_profiles
        if not previous:
            return
        kept: dict = {}
        for item in self.index:
            key = _lexical_content_key(item)
            if key is not None:
                entry = previous.get(key)
                if entry is not None:
                    kept[key] = entry
        self._lexical_profiles = (knobs, kept)

    def _lexical_scores(self, code_tokens) -> tuple[list[float], list[int]]:
        """整份 index 的字面匹配分數與明確點名的位置。

        回傳 (kw_scores, explicit_indices):kw_scores[i] 與
        ``self._token_match_score(code_tokens, self.index[i])`` 逐位元相同;
        explicit_indices 是 symbol 非空且(小寫)出現在查詢 token 裡的位置,依索引遞增。
        只走查詢 token 的 postings,沒命中的 item 維持 0.0(與逐筆版算出的 0.0 相同)。
        """
        kw_scores = [0.0] * len(self.index)
        if not code_tokens:
            return kw_scores, []
        lexical = self._lexical_postings()
        # 逐 token 計次:大小寫不同的 token(UART / uart)各算一次,與逐筆版迭代 set 相同
        multiplicity: dict[str, int] = {}
        for token in code_tokens:
            lowered = token.lower()
            multiplicity[lowered] = multiplicity.get(lowered, 0) + 1
        exact: set[int] = set()
        explicit: list[int] = []
        hits: dict[int, int] = {}
        context_hits: dict[int, int] = {}
        for lowered, count in multiplicity.items():
            positions = lexical.exact.get(lowered)
            if positions:
                exact.update(positions)
                if lowered:
                    explicit.extend(positions)
            for position in lexical.target.get(lowered, ()):
                hits[position] = hits.get(position, 0) + count
            for position in lexical.context.get(lowered, ()):
                context_hits[position] = context_hits.get(position, 0) + count
        token_count = len(code_tokens)
        for position in exact:
            kw_scores[position] = _lexical_score(0, 0, token_count, True)
        for position in hits.keys() | context_hits.keys():
            if position not in exact:
                kw_scores[position] = _lexical_score(
                    hits.get(position, 0), context_hits.get(position, 0), token_count, False
                )
        explicit.sort()
        return kw_scores, explicit

    def _get_embedding_at(self, idx: int) -> list:
        """取得指定索引的 embedding（相容新舊格式）"""
        _require_numpy()
        # 新格式：從 numpy array 取得
        if self.embeddings is not None and idx < len(self.embeddings):
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

    @staticmethod
    def _reranker_error(reason: str) -> DependencyError:
        return DependencyError(
            f"Code RAG reranker unavailable at {LLAMA_RERANK_BASE_URL}: {reason}. "
            "Check the dedicated reranker llama-server (deployment.json services.reranker)."
        )

    def _rerank_code_candidates(self, question: str, candidates: list, top_k: int,
                                *, trace: dict | None = None) -> list[RankedCandidate]:
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
        def _skip(reason: str) -> None:
            if trace is not None:
                trace["rerank"] = {"applied": False, "input_count": len(candidates), "reason": reason}

        if not candidates:
            _skip("no_candidates")
            return []

        if not USE_RERANKER or len(candidates) <= top_k:
            _skip("reranker_disabled" if not USE_RERANKER else "candidates_within_top_k")
            return self._fusion_candidates(candidates, top_k)

        # 條件觸發：判斷是否真的需要 rerank
        if not self._should_rerank(candidates, top_k):
            _skip("not_needed")
            return self._fusion_candidates(candidates, top_k)

        # 減少 rerank 的 candidates 數量
        rerank_count = max(top_k, config.CODE_RAG_RERANK_CANDIDATE_POOL)
        if trace is not None:
            trace["stage"] = "rerank"
            trace["rerank"] = {"applied": True, "input_count": min(len(candidates), rerank_count),
                               "reason": None}

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
                if trace is not None:
                    # 評過分的每一個都標回候選池(含之後被 top_k 切掉的)。
                    by_key = {(row["path"], row["line"], row["symbol"]): row
                              for row in trace.get("pool", [])}
                    for rc in ranked:
                        row = by_key.get(_trace_key(rc.item))
                        if row is not None:
                            row["rerank"] = round(float(rc.rerank_score), 4)
                ranked.sort(reverse=True, key=lambda rc: rc.final_score)
                return ranked[:top_k]

            except Exception as exc:
                raise self._reranker_error(f"dedicated reranker call failed: {exc}") from exc

        raise self._reranker_error("dedicated reranker is not reachable")

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
            if self.build_context is not None:
                result_item['build_state'] = item.get('build_state', 'unknown')
            results.append(result_item)
        return results

    def query_ranked(self, question: str, top_k: int = CODE_RAG_TOP_K,
                     is_bug_fix: bool = False, *, trace: dict | None = None) -> list[RankedCandidate]:
        """query 的完整結果(RankedCandidate;§8 evidence 模式的資料來源)。

        Lazy build：第一次 query 時才建立索引，避免不需要 CodeRAG 時浪費時間

        `trace`:給一個 dict 就把檢索路徑填進去(候選池、各通道分數、門檻、reranker
        設定與結果;見模組頂端的 TRACE_* 說明)。半途拋例外時已填的階段仍在呼叫端手上。
        """
        _require_numpy()
        if self.build_context is not None:
            self.build_context.assert_fresh()
        if trace is not None:
            trace.update({"kind": "code_rag", "stage": "index", "pool": [], "final": [], "files": []})
        # 已載入的 symbols（含零 symbol 檔案）也不能繞過主要 parser 准入。
        required_paths = set(self._file_cache) | set(self._indexed_file_hashes or {})
        required_paths.update(item.get("path", "") for item in self.index)
        require_parsers_for_paths(required_paths, **({"build_context": self.build_context}
                                                   if self.build_context is not None else {}))
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

        # kw_scores 一律先算一次。同一份結果餵三個地方:lazy 分支用它決定要不要
        # materialize、融合公式用它決定 lexical 半邊有沒有訊號可用、下面的計分
        # 迴圈直接沿用 —— 因為「有沒有 lexical 命中」是全域性質,逐筆算的時候
        # 已經來不及拿來決定權重了。
        # 走倒排索引:符號側斷詞只在 index / 預算變動時做一次,每次查詢只碰查詢 token
        # 的 postings;分數與逐筆 _token_match_score 逐位元相同,explicit_indices 依索引遞增。
        kw_scores, explicit_indices = self._lexical_scores(code_tokens)
        lexical_has_signal = any(score > 0 for score in kw_scores)

        if self._lazy_embed:
            if not lexical_has_signal:
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
                    if not _has_vector(item.get("embedding")):
                        emb = self._get_embedding(self._build_embed_text(item))
                        item["embedding"] = emb

        # 動態門檻：Bug 類問題稍微放寬
        threshold = CODE_RAG_THRESHOLD_BUG if is_bug_fix else CODE_RAG_THRESHOLD

        # Dense 矩陣以 numpy 計分；lazy rows 的逐列計分保留在下面。
        if self.embeddings is not None and len(self.embeddings) > 0 and not self._lazy_embed:
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
                if _has_vector(emb):
                    sim = sum(a * b for a, b in zip(q_emb, emb))
                    norm_q = sum(x*x for x in q_emb) ** 0.5
                    norm_e = sum(x*x for x in emb) ** 0.5
                    emb_score = sim / (norm_q * norm_e) if norm_q > 0 and norm_e > 0 else 0
                else:
                    emb_score = 0

            kw_score = kw_scores[i]

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
                lexical_has_signal=lexical_has_signal,
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

        if trace is not None:
            selected_ids = {id(item) for _c, _e, _k, item in candidates_for_rerank}
            trace["stage"] = "selected"
            trace["settings"] = {
                "embedding_model": EMBEDDING_MODEL,
                "reranker_model": RERANKER_MODEL,
                "use_reranker": bool(USE_RERANKER),
                "reranker_always_on": bool(RERANKER_ALWAYS_ON),
                "threshold": float(threshold),
                "top_k": int(top_k),
                "rerank_pool": int(config.CODE_RAG_RERANK_CANDIDATE_POOL),
                "index_size": len(self.index),
                "lazy_embed": bool(self._lazy_embed),
                "is_short_query": bool(is_short_query),
                "code_tokens": [str(token) for token in code_tokens][:32],
            }
            trace["pool_total"] = len(scores)
            trace["selected_count"] = len(candidates_for_rerank)
            trace["pool"] = [
                {
                    "path": item.get("path"), "line": item.get("line"),
                    "symbol": item.get("symbol", ""), "type": item.get("type", ""),
                    "emb": round(float(emb_score), 4), "lexical": round(float(kw_score), 4),
                    "combined": round(float(combined), 4),
                    "selected": id(item) in selected_ids, "rerank": None, "final": False,
                }
                for combined, emb_score, kw_score, item in scores[:TRACE_MAX_POOL]
            ]

        # 使用 reranker 二次排序（條件觸發)。分數是 query-local 的
        # RankedCandidate(§5-1),絕不寫回 self.index 的持久 item。
        ranked = self._rerank_code_candidates(question, candidates_for_rerank, top_k, trace=trace)
        if self.build_context is not None:
            self.build_context.assert_fresh()
        if trace is not None:
            _finish_code_trace(trace, ranked, self._indexed_file_hashes or {},
                               hash_algorithm="sha256" if self.build_context is not None else "code_rag_md5_v1")
        return ranked

    def trace_add_files(self, trace: dict, paths) -> None:
        """把後續階段(context evidence 等)真的回傳的檔補進 trace["files"],帶索引時的雜湊。"""
        _trace_add_files(trace, paths, self._indexed_file_hashes or {},
                         hash_algorithm="sha256" if self.build_context is not None else "code_rag_md5_v1")

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
