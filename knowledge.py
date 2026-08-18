#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 知識庫 (RAG)
"""

import re
import json
from dataclasses import dataclass, field
from pathlib import Path
from functools import lru_cache


import config

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import jieba
    HAS_JIEBA = True
except ImportError:
    HAS_JIEBA = False
    # 提示：jieba 對中文 BM25 搜尋精準度很重要
    import sys
    print("[WARN] jieba 未安裝，中文 BM25 搜尋精準度可能較低", file=sys.stderr)
    print("       建議執行: pip install jieba", file=sys.stderr)

import context_signals
import llama_client
from knowledge_store import KnowledgeStoreError, knowledge_store_lock

# _load 失敗時塞進 _source_stat 的哨兵:它不等於任何真實 stat 簽章(也不等於
# 「檔案不存在」的 None),因此 source_changed() 恆為 True——失敗的載入永遠算
# stale,查詢端會持續重試,直到檔案修好(或被移除)為止。沒有這個哨兵,失敗
# 的 instance 會記住壞檔的簽章,source_changed() 回 False,自動重載就此卡死。
_LOAD_FAILED_STAT = object()
from config import (
    LLAMA_BASE_URL, LLAMA_EMBED_BASE_URL, LLAMA_RERANK_BASE_URL,
    KNOWLEDGE_FILE, KNOWLEDGE_EMB_FILE,
    KNOWLEDGE_TOP_K, KNOWLEDGE_CANDIDATE_K, KNOWLEDGE_THRESHOLD,
    KNOWLEDGE_THRESHOLD_SHORT, KNOWLEDGE_SHORT_QUERY_TOKENS,
    DYNAMIC_THRESHOLD_RATIO, DYNAMIC_TOP_K_HIGH_SCORE,
    DYNAMIC_TOP_K_MIN, DYNAMIC_TOP_K_MAX,
    KNOWLEDGE_INCLUDE_CONTENT, KNOWLEDGE_CONTENT_MAX_CHARS,
    KNOWLEDGE_MERGE_ADJACENT, KNOWLEDGE_MERGE_MAX_CHARS,
    EMBEDDING_MODEL, RERANKER_MODEL,
    USE_RERANKER, USE_HYBRID_SEARCH, USE_QUERY_EXPANSION,
    USE_MMR, MMR_LAMBDA,
    # P0 改進：Source Type Weighting（來源權重）
    SOURCE_TYPE_WEIGHTS, POLLUTION_RISK_TOP_K, POLLUTION_RISK_MIN_SCORE,
    # P0 改進：BM25 + RRF + Reranker 條件式觸發
    BM25_K1, BM25_B, BM25_ENABLED, BM25_MIN_RELATIVE_SCORE,
    RRF_K, RRF_ENABLED,
    RERANKER_ALWAYS_ON, RERANKER_TOP_N, RERANKER_PASSAGE_MAX_CHARS,
    RERANKER_SKIP_THRESHOLD,
    MARGIN_ENABLED, MARGIN_MIN_GAP, MARGIN_LOW_SCORE,
    STRICT_MODE_THRESHOLD, STRICT_MODE_RERANK_REQUIRED,
    # P1 改進：Multi-Query（條件式啟用）
    MULTI_QUERY_ENABLED, MULTI_QUERY_COUNT, MULTI_QUERY_TYPES,
    MULTI_QUERY_MIN_SCORE_TRIGGER, MULTI_QUERY_SKIP_NUMERIC,
    # P0-3 改進：雙語+符號友善
    QUERY_BILINGUAL_ENABLED, QUERY_SYMBOL_FRIENDLY, QUERY_SYMBOL_PATTERN, QUERY_PRESERVE_SYMBOLS,
)


def _normalize_text_for_cache(text: str) -> str:
    """正規化文字以提高 cache 命中率

    - 移除多餘空白
    - 統一換行符
    """
    return ' '.join(text.split())


@lru_cache(maxsize=512)  # 提高快取大小（速度優化：256->512）
def _cached_get_embedding(text: str) -> tuple:
    """帶 LRU cache 的 embedding 查詢

    改進：追問/重跑時可重用已查詢過的 embedding，提升速度
    注意：回傳 tuple 而非 list，因為 lru_cache 需要 hashable
    """
    try:
        emb = llama_client.embed_one(
            base_url=LLAMA_EMBED_BASE_URL,
            content=text,
            model=EMBEDDING_MODEL,
            timeout=120,
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


def use_generated_context() -> bool:
    """查詢端要不要吃 chunk 的生成脈絡。

    每次呼叫都重讀 config：這同時是緊急 kill switch，關掉之後不必重建 KB、不必
    重啟就退回 content-only 檢索。
    """
    return bool(getattr(config, "KB_CONTEXT_USE", False))


@dataclass
class Candidate:
    """一個候選 chunk 與它的各種分數。

    刻意不是多元素 tuple：這裡有兩套訊號，`retrieval_*` 含 LLM 生成的脈絡、
    `gate_*` 只看原文。哪個分數餵哪個決策必須在型別層看得出來、grep 得到、
    測得到——tuple 的 `c[1]` 做不到這件事，而搞混的後果是生成脈絡抬高的分數
    通過了拒答閘（分數面的循環 grounding）。

    規則：排序 / 召回看 retrieval_*，門檻 / 決策看 gate_*。
    """

    chunk_idx: int
    chunk: dict
    rrf_score: float = 0.0
    retrieval_score: float = 0.0
    gate_score: float = 0.0
    retrieval_bm25: float = 0.0
    gate_bm25: float = 0.0


@dataclass
class Bm25Index:
    """一套 BM25 索引（inverted index + doc len + idf）。"""

    index: dict = field(default_factory=dict)
    doc_lens: list = field(default_factory=list)
    avg_doc_len: float = 1.0
    idf: dict = field(default_factory=dict)


class KnowledgeBase:
    """
    優化版知識庫（P0 改進版）：
    1. 專用 Reranker 模型 (bge-reranker) - 預設啟用
    2. Query Expansion (LLM 生成搜尋關鍵字)
    3. 真正的 BM25 lexical search（取代簡單 keyword matching）
    4. RRF (Reciprocal Rank Fusion) 融合 embedding + BM25
    5. Margin-based 動態門檻判斷
    6. 結構化輸出格式
    """

    def __init__(self, json_path: str = KNOWLEDGE_FILE):
        self.chunks = []
        self.documents = []
        self.loaded = False
        # 載入失敗原因(str);None 表示「沒失敗」——檔案不存在的合法空庫也是 None。
        # 呼叫端用它區分「空庫」與「壞庫」:壞庫不可拿來取代還在服務的舊 KB。
        self.load_error = None
        self.path = json_path
        self._reranker_available = None
        # Numpy 加速用的預計算陣列
        self._embeddings = None  # shape: (n_chunks, dim)
        self._embeddings_normalized = False
        # 決策訊號：content-only 的向量矩陣。沒有任何 ctx 的 KB 直接別名到
        # retrieval 矩陣（兩者組字本來就相同），不多佔一份記憶體。
        self._gate_embeddings = None
        self._has_ctx = False
        # BM25 索引（預計算）：retrieval 一套、gate（content-only）一套。
        # 本專案規模下多一套 idf/doc-len 的記憶體與載入成本可忽略，換到的是
        # 「lexical 決策永遠看不到生成文字」這條硬保證。
        self._bm25 = None
        self._bm25_gate = None
        # .npz embeddings 路徑（與 json 同目錄）
        json_dir = Path(json_path).parent
        self._emb_path = json_dir / KNOWLEDGE_EMB_FILE

        # staleness 偵測：記下載入當下的檔案簽章，之後用 source_changed() 比對。
        # 刻意在 _load 之前取——若載入期間檔案又被改，下次比對會看到差異再重載一次。
        self._source_stat = self._stat_signature(json_path)

        if Path(json_path).exists():
            self._load(json_path)

    @staticmethod
    def _stat_signature(path: str):
        """檔案簽章 (mtime_ns, size)；檔案不存在時回 None。"""
        try:
            st = Path(path).stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def source_changed(self) -> bool:
        """knowledge.json 在本 instance 載入後是否又被改過。

        查詢端（MCP query_knowledge*）靠這個做自動重載，取代
        「ingest 後必須人工記得呼叫 reload」的文件層約定。
        """
        return self._stat_signature(self.path) != self._source_stat

    def _load(self, path: str):
        try:
            with knowledge_store_lock(Path(path), exclusive=False):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                self.chunks = data.get("chunks", [])
                self._index_chunks()
                metadata = data.get("metadata", {})
                self._loaded_metadata = metadata
                self.documents = metadata.get("documents", [])

                # 驗證 embedding model 一致性
                saved_model = metadata.get("embedding_model", "")
                if saved_model and saved_model != EMBEDDING_MODEL:
                    raise KnowledgeStoreError(
                        "knowledge JSON embedding model mismatch: "
                        f"saved={saved_model}, configured={EMBEDDING_MODEL}. "
                        "Rebuild the knowledge base before querying."
                    )
                self._embedding_mismatch = False

                # 驗證 embedding 維度一致性（抽樣檢查前幾個 chunk）
                if self.chunks:
                    sample_dims = set()
                    for chunk in self.chunks[:5]:
                        emb = chunk.get("embedding", [])
                        if emb:
                            sample_dims.add(len(emb))

                    if len(sample_dims) > 1:
                        raise KnowledgeStoreError(
                            f"knowledge JSON embedding dimension mismatch: {sorted(sample_dims)}"
                        )
                    self._embedding_dim_mismatch = False
                    self._embedding_dim = sample_dims.pop() if sample_dims else None
                else:
                    self._embedding_dim_mismatch = False
                    self._embedding_dim = None

                self.loaded = True

                # 優先從 .npz 載入 embeddings（加速載入）
                npz_loaded = self._load_embeddings_from_npz()

                if not npz_loaded:
                    # 有 ctx 的 KB 一律不准走 legacy inline 回退。那條路徑會把
                    # retrieval 矩陣直接別名成 gate，於是 KB_CONTEXT_USE=0、拒答
                    # 門檻、信心判斷全都吃到含生成脈絡的向量——正是雙訊號要擋的
                    # 循環 grounding。gate 向量只能來自驗過的 NPZ 第二組矩陣。
                    if self._has_ctx:
                        raise KnowledgeStoreError(
                            "this knowledge base carries generated chunk context but its "
                            f"embeddings could not be loaded from {self._emb_path}; refusing "
                            "to fall back to inline vectors (that would alias the retrieval "
                            "matrix as the decision gate). Rebuild the knowledge base."
                        )
                    # Legacy JSON may still contain inline embeddings.  Missing
                    # vectors are not a lexical-only fallback: fail loudly.
                    self._precompute_embeddings()
                    if self.chunks and self._embeddings is None:
                        raise KnowledgeStoreError(
                            f"knowledge chunks have no usable embeddings and {self._emb_path} "
                            "could not be loaded; rebuild the knowledge base"
                        )

                # P0 改進：預計算 BM25 索引
                if BM25_ENABLED:
                    self._precompute_bm25_index()

                # 速度優化：記錄載入的 metadata 供快取驗證
                self._cache_metadata = {
                    "embedding_model": EMBEDDING_MODEL,
                    "chunk_count": len(self.chunks),
                    "bm25_enabled": BM25_ENABLED,
                }

        except KnowledgeStoreError as e:
            self.loaded = False
            self.load_error = str(e)
            self._source_stat = _LOAD_FAILED_STAT
            raise
        except Exception as e:
            print(f"[WARN] 知識庫載入失敗: {e}")
            self.loaded = False
            self.load_error = f"{type(e).__name__}: {e}"
            self._source_stat = _LOAD_FAILED_STAT

    def _index_chunks(self) -> None:
        """標上 KB 內的列索引（≠ chunk 自己的 `chunk_index`，那是文件內序號）。

        決策點靠這個索引回去 gate 矩陣讀對應的列，而不是把 gate 向量掛回 chunk
        （n×dim 的 Python float list 記憶體會爆）。冪等，重複呼叫無害。
        """
        for row, chunk in enumerate(self.chunks):
            chunk["chunk_idx"] = row

    def _compute_content_hash(self, schema: str = context_signals.LEGACY_CONTENT_HASH_SCHEMA) -> str:
        """chunks 內容雜湊（用於 .npz 快取驗證）。

        組字與雜湊的唯一定義在 context_signals，寫入端（RAG.py）用的是同一份。
        以前兩邊各寫一套同樣的字串，差一個字就會變成「內容雜湊不一致」這種看起來
        像資料壞掉的訊息。
        """
        return context_signals.chunks_content_hash(self.chunks, schema=schema)

    def _load_embeddings_from_npz(self) -> bool:
        """從 .npz 載入 embeddings（retrieval + gate 兩套）。

        schema 走 required 對照，不是只拿 NPZ 自報的 schema 重算一次自己：那樣
        legacy NPZ 永遠自驗通過，程式換了組字規則也察覺不到。

        KB 只要有任何 chunk 帶 ctx，gate 矩陣就必須同時存在，缺了直接拒載——
        絕不 fallback 到 contextual 向量當 gate 用。

        Returns:
            True 如果成功載入，False 如果需要從 JSON 重建
        """
        self._has_ctx = context_signals.has_any_ctx(self.chunks)

        if not HAS_NUMPY:
            if self._has_ctx:
                raise KnowledgeStoreError(
                    "this knowledge base carries generated chunk context, which needs "
                    "numpy to load its two embedding matrices; install numpy"
                )
            return False
        if not self._emb_path.exists():
            # 有 ctx 的情況由 _load 統一拒載（訊息在那邊，涵蓋所有 return False）
            return False

        try:
            with np.load(self._emb_path, allow_pickle=False) as data:
                available = set(getattr(data, "files", []))
                embeddings = data['embeddings'].copy()
                emb_model = str(data.get('embedding_model', ''))
                chunk_count = int(data.get('chunk_count', 0))
                content_hash = str(data.get('content_hash', ''))
                hash_schema = str(data.get(
                    'content_hash_schema', context_signals.LEGACY_CONTENT_HASH_SCHEMA
                ))
                npz_generation = str(data.get('store_generation', ''))
                stored_dimension = int(data.get('embedding_dimension', 0))
                gate_embeddings = (
                    data['embeddings_gate'].copy() if 'embeddings_gate' in available else None
                )
                gate_hash = str(data.get('gate_content_hash', ''))
                gate_schema = str(data.get('gate_content_hash_schema', ''))
        except KnowledgeStoreError:
            raise
        except Exception as e:
            print(f"[WARN] 載入 .npz 失敗: {e}")
            return False

        try:
            # 驗證 embedding model 一致
            if emb_model and emb_model != EMBEDDING_MODEL:
                print(f"[WARN] .npz embedding model 不一致，將重建")
                return False

            # 驗證 chunk 數量一致
            if chunk_count != len(self.chunks):
                print(f"[WARN] .npz chunk 數量不一致，將重建")
                return False

            if embeddings.ndim != 2 or embeddings.shape[0] != len(self.chunks):
                print(f"[WARN] .npz embedding matrix shape 不一致，將重建")
                return False
            if stored_dimension and embeddings.shape[1] != stored_dimension:
                print(f"[WARN] .npz embedding dimension metadata 不一致，將重建")
                return False

            json_generation = str(getattr(self, "_loaded_metadata", {}).get("store_generation", ""))
            if json_generation and npz_generation != json_generation:
                print(f"[WARN] .npz store generation 不一致，拒絕載入")
                return False

            # required-schema 對照：這是 fail-loud，不是「重建就好」——schema 不對
            # 代表向量是用另一套組字算的，拿來查會靜默地錯。
            allowed = context_signals.required_retrieval_schemas(has_ctx=self._has_ctx)
            if hash_schema not in allowed:
                raise KnowledgeStoreError(
                    f"knowledge embedding schema mismatch: NPZ={hash_schema!r}, "
                    f"required one of {sorted(allowed)}. Rebuild the knowledge base "
                    f"(python RAG.py rebuild --kb {self.path} <docs>)."
                )

            # 驗證內容雜湊一致（避免內容變更但數量相同的情況）
            current_hash = self._compute_content_hash(schema=hash_schema)
            if content_hash and content_hash != current_hash:
                print(f"[WARN] .npz 內容雜湊不一致，將重建")
                return False

            if self._has_ctx:
                if gate_embeddings is None:
                    raise KnowledgeStoreError(
                        "this knowledge base carries generated chunk context but "
                        f"{self._emb_path} has no gate (content-only) matrix; refusing to "
                        "use contextual vectors for decisions. Rebuild the knowledge base."
                    )
                if gate_schema != context_signals.GATE_SCHEMA:
                    raise KnowledgeStoreError(
                        f"gate embedding schema mismatch: NPZ={gate_schema!r}, "
                        f"required {context_signals.GATE_SCHEMA!r}. Rebuild the knowledge base."
                    )
                if gate_embeddings.ndim != 2 or gate_embeddings.shape != embeddings.shape:
                    raise KnowledgeStoreError(
                        "gate embedding matrix shape mismatch: "
                        f"{getattr(gate_embeddings, 'shape', None)} vs {embeddings.shape}"
                    )
                current_gate_hash = self._compute_content_hash(
                    schema=context_signals.GATE_SCHEMA
                )
                if gate_hash and gate_hash != current_gate_hash:
                    raise KnowledgeStoreError(
                        f"gate embedding content hash mismatch: NPZ={gate_hash}, "
                        f"JSON={current_gate_hash}. Rebuild the knowledge base."
                    )
        except KnowledgeStoreError:
            raise

        self._embeddings = embeddings
        self._embeddings_normalized = True  # .npz 已預先正規化
        self._embedding_indices = list(range(len(self.chunks)))
        # 沒有 ctx 的 KB：retrieval 與 gate 是同一組字算出來的，直接別名。
        self._gate_embeddings = gate_embeddings if gate_embeddings is not None else embeddings

        # P0：把 .npz 的 retrieval 向量掛回每個 chunk。
        # RAG 存 knowledge.json 時為了體積「不再 inline embedding」（只留 .npz），
        # 若這裡只設 self._embeddings 而不回填 chunk["embedding"]，下游的 MMR /
        # 污染控制（都讀 chunk.get("embedding")）會一律拿到空向量、把相似度算成 0。
        # gate 向量刻意**不**掛回 chunk：那是 Python float list，n×dim 一份就夠痛，
        # 決策點一律用 chunk_idx 讀矩陣列。
        for i, chunk in enumerate(self.chunks):
            chunk["embedding"] = embeddings[i].tolist()

        return True

    def _decision_order(self, candidates: list) -> list:
        """決策用的候選排序。

        決策讀的是「top1 / top2 / top5 的分數」這種位置性的東西，而候選本身是照
        RRF 排的——生成脈絡可以改動那個順序，於是即使每個位置讀的都是 gate 分數，
        ctx 仍然能透過「誰站在第一位」間接推動門檻與 margin。真的在用 ctx 時，
        決策改看 gate 分數自己的排序。

        沒有 ctx（或旗標關著）時原樣回傳：那條路徑必須與加入本功能前逐位元組
        相同，而 RRF 順序本來就不等於 dense 分數順序。
        """
        if not (self._has_ctx and use_generated_context()):
            return candidates
        return sorted(candidates, key=lambda c: c.gate_score, reverse=True)

    def _ranking_bm25_index(self):
        """排序用的 BM25 索引；USE 關掉時就是 gate（content-only）那一套。"""
        return self._bm25 if use_generated_context() else self._bm25_gate

    def _retrieval_matrix(self):
        """排序用的矩陣。

        `KB_CONTEXT_USE` 關掉時退回 gate 矩陣——旗標的契約是「關掉之後檢索與
        content-only 完全等價」,只換分數來源、不必重建 KB。
        """
        if self._has_ctx and not use_generated_context():
            return self._gate_matrix()
        return self._embeddings

    def _selection_vector(self, chunk: dict) -> list:
        """MMR/多樣性用的向量；USE 關掉時同樣退回 gate 列。"""
        if self._has_ctx and not use_generated_context() and HAS_NUMPY:
            matrix = self._gate_matrix()
            index = chunk.get("chunk_idx")
            if matrix is not None and isinstance(index, int) and 0 <= index < matrix.shape[0]:
                return matrix[index].tolist()
        return chunk.get("embedding", [])

    def _gate_matrix(self):
        """決策用的 content-only 矩陣；沒有就退回 retrieval（代表 KB 無 ctx）。"""
        if self._gate_embeddings is not None:
            return self._gate_embeddings
        return self._embeddings

    def _gate_score_for(self, chunk_idx: int, q_emb: list) -> float:
        """單一 chunk 的 gate 相似度（以列索引讀矩陣，不掛回 chunk）。"""
        matrix = self._gate_matrix()
        if matrix is None or not q_emb:
            return 0.0
        if not (0 <= chunk_idx < matrix.shape[0]):
            return 0.0
        q_vec = np.asarray(q_emb, dtype=np.float32)
        norm = np.linalg.norm(q_vec)
        if norm <= 0:
            return 0.0
        return float(np.dot(matrix[chunk_idx], q_vec / norm))

    @staticmethod
    def _member_indices(chunk: dict) -> list:
        """chunk（可能是合併過的）對應的 KB 列索引。"""
        members = chunk.get("member_chunk_idx")
        if not members:
            index = chunk.get("chunk_idx")
            members = [index] if index is not None else []
        indices = []
        for value in members:
            try:
                indices.append(int(value))
            except (TypeError, ValueError):
                continue
        return indices

    def _chunk_matrix_score(self, chunk: dict, q_emb: list, matrix) -> float:
        """chunk 對某個矩陣的相似度；合併 chunk 取成員的 max。

        聚合刻意用成員的 max，**不**拿合併後的平均向量重算——決策面那樣做等於
        讓生成脈絡回到路徑上。
        """
        indices = self._member_indices(chunk)
        if indices and HAS_NUMPY and matrix is not None:
            scores = self._matrix_scores_for(indices, q_emb, matrix)
            return max(scores.values()) if scores else 0.0
        # 沒有 numpy 的 legacy 路徑：那種 KB 一定沒有 ctx，inline 向量就是
        # content-only 的訊號本身。
        embedding = chunk.get("embedding", [])
        if embedding and q_emb:
            return self._cosine_similarity(q_emb, embedding)
        return 0.0

    def _chunk_gate_score(self, chunk: dict, q_emb: list) -> float:
        """決策用的 content-only 相似度。"""
        return self._chunk_matrix_score(chunk, q_emb, self._gate_matrix())

    def _chunk_retrieval_score(self, chunk: dict, q_emb: list) -> float:
        """觀測用的檢索相似度（USE 開著時含生成脈絡）。"""
        return self._chunk_matrix_score(chunk, q_emb, self._retrieval_matrix())

    def _gate_scores_for(self, chunk_indices: list, q_emb: list) -> dict:
        """一次算好一批 chunk 的 gate 相似度。"""
        return self._matrix_scores_for(chunk_indices, q_emb, self._gate_matrix())

    def _matrix_scores_for(self, chunk_indices: list, q_emb: list, matrix) -> dict:
        """一次算好一批 chunk 對某個矩陣的相似度。"""
        if matrix is None or not q_emb or not chunk_indices:
            return {}
        q_vec = np.asarray(q_emb, dtype=np.float32)
        norm = np.linalg.norm(q_vec)
        if norm <= 0:
            return {}
        q_vec = q_vec / norm
        valid = [i for i in chunk_indices if 0 <= i < matrix.shape[0]]
        if not valid:
            return {}
        scores = np.dot(matrix[valid], q_vec)
        return {index: float(score) for index, score in zip(valid, scores)}

    def _save_embeddings_to_npz(self):
        """將 embeddings 儲存為 .npz（加速下次載入）

        改進：儲存內容雜湊用於驗證
        """
        if not HAS_NUMPY or self._embeddings is None:
            return

        try:
            content_hash = self._compute_content_hash()
            np.savez_compressed(
                self._emb_path,
                embeddings=self._embeddings,
                embedding_model=EMBEDDING_MODEL,
                chunk_count=len(self.chunks),
                content_hash=content_hash
            )
        except Exception as e:
            print(f"[WARN] 儲存 .npz 失敗: {e}")

    def _precompute_embeddings(self):
        """預計算並正規化 embeddings 到 numpy array（legacy：JSON inline 向量）

        這條路徑會把 retrieval 矩陣別名成 gate，所以只准用在確定沒有 ctx 的 KB。
        呼叫端（_load）已經擋過一次；這裡是第二道，避免以後有人繞過去。
        """
        self._index_chunks()
        if context_signals.has_any_ctx(self.chunks):
            raise KnowledgeStoreError(
                "refusing to build a gate matrix from inline vectors for a knowledge base "
                "that carries generated chunk context; rebuild it so the NPZ has both "
                "matrices"
            )
        if not HAS_NUMPY or not self.chunks:
            self._embeddings = None
            return

        # 收集所有 embeddings
        embeddings_list = []
        valid_indices = []

        for i, chunk in enumerate(self.chunks):
            emb = chunk.get("embedding", [])
            if emb and isinstance(emb, list) and len(emb) > 0:
                embeddings_list.append(emb)
                valid_indices.append(i)

        if not embeddings_list:
            self._embeddings = None
            return

        # 維度不一致是模型/快取混用，不能靜默丟掉異常列。
        dimensions = {len(embedding) for embedding in embeddings_list}
        if len(dimensions) != 1:
            raise KnowledgeStoreError(
                f"knowledge embedding dimension mismatch: {sorted(dimensions)}"
            )

        self._embeddings = np.array(embeddings_list, dtype=np.float32)
        self._embedding_indices = valid_indices  # 映射回 self.chunks 的索引

        # L2 正規化（預計算，加速後續 cosine similarity）
        norms = np.linalg.norm(self._embeddings, axis=1, keepdims=True)
        norms = np.where(norms > 0, norms, 1.0)  # 避免除零
        self._embeddings = self._embeddings / norms
        self._embeddings_normalized = True
        # JSON inline 向量的 KB 一定沒有 ctx（ctx 是新格式），別名即可。
        self._gate_embeddings = self._embeddings

    @property
    def _bm25_index(self):
        """retrieval BM25 的 inverted index（相容舊呼叫端）。"""
        return self._bm25.index if self._bm25 else None

    @property
    def _bm25_doc_lens(self):
        return self._bm25.doc_lens if self._bm25 else None

    @property
    def _bm25_avg_doc_len(self):
        return self._bm25.avg_doc_len if self._bm25 else 0.0

    @property
    def _bm25_idf(self):
        return self._bm25.idf if self._bm25 else None

    def _build_bm25_index(self, *, use_ctx: bool) -> Bm25Index:
        """建一套 BM25 索引。

        BM25 公式：
        score = sum( IDF(t) * (tf * (k1+1)) / (tf + k1 * (1 - b + b * dl/avgdl)) )
        """
        import math
        from collections import defaultdict

        inverted_index = defaultdict(lambda: defaultdict(int))
        doc_lens = []
        doc_freqs = defaultdict(int)

        for idx, chunk in enumerate(self.chunks):
            # 章節 + 來源 +（ctx）+ 去合成前綴的本文；組法的唯一定義在 context_signals
            full_text = context_signals.bm25_document_text(chunk, use_ctx=use_ctx)
            tokens = self._tokenize_for_bm25(full_text)
            doc_lens.append(len(tokens))

            term_set = set()
            for token in tokens:
                inverted_index[token][idx] += 1
                term_set.add(token)
            for term in term_set:
                doc_freqs[term] += 1

        n_docs = len(self.chunks)
        idf = {}
        for term, df in doc_freqs.items():
            # BM25 IDF 公式（加上 +1 避免負值）
            idf[term] = math.log((n_docs - df + 0.5) / (df + 0.5) + 1)

        return Bm25Index(
            index=dict(inverted_index),
            doc_lens=doc_lens,
            avg_doc_len=sum(doc_lens) / len(doc_lens) if doc_lens else 1.0,
            idf=idf,
        )

    def _precompute_bm25_index(self):
        """預計算 BM25 索引：retrieval 一套，gate（content-only）一套。

        沒有任何 ctx 時兩套的來源文本逐位元組相同，直接別名，不重算也不多佔記憶體。
        """
        if not self.chunks:
            return
        self._bm25 = self._build_bm25_index(use_ctx=True)
        self._bm25_gate = (
            self._build_bm25_index(use_ctx=False) if self._has_ctx else self._bm25
        )

    def _content_for_bm25(self, chunk: dict) -> str:
        """去掉合成前綴（overlap + heading）的本文；定義在 context_signals。"""
        return context_signals.chunk_body(chunk)

    def _tokenize_for_bm25(self, text: str) -> list:
        """BM25 專用的 tokenizer

        改進：
        - 支援中英文混合
        - 保留程式碼 token（函式名、變數名）
        - 移除 stopwords
        """
        # 先轉小寫
        text = text.lower()

        # 技術 token：identifier、十六進位與純數字都必須保留。數字/hex
        # 是 register map、offset、threshold、版本題的主要 lexical evidence。
        en_tokens = re.findall(
            r'(?<![a-z0-9_])(?:0x[0-9a-f]+|v?\d+(?:\.\d+)+(?:[a-z]+)?|\d+(?:[a-z]+)?|[a-z_][a-z0-9_]*)(?![a-z0-9_])',
            text,
        )
        # 中文 token（單字或雙字詞）
        if HAS_JIEBA:
            zh_tokens = [t for t in jieba.cut(text, cut_all=False)
                         if re.search(r'[\u4e00-\u9fff]', t)]
        else:
            zh_tokens = re.findall(r'[\u4e00-\u9fff]{1,2}', text)

        all_tokens = en_tokens + zh_tokens

        # 移除 stopwords
        stopwords = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been',
                     'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
                     'would', 'could', 'should', 'may', 'might', 'must', 'shall',
                     'can', 'need', 'to', 'of', 'in', 'for', 'on', 'with', 'at',
                     'by', 'from', 'as', 'into', 'through', 'during', 'before',
                     'after', 'above', 'below', 'between', 'under', 'again',
                     'then', 'once', 'here', 'there', 'when', 'where', 'why',
                     'how', 'all', 'each', 'few', 'more', 'most', 'other',
                     'some', 'such', 'no', 'nor', 'not', 'only', 'own', 'same',
                     'so', 'than', 'too', 'very', 'just', 'and', 'but', 'if',
                     'or', 'because', 'until', 'while', 'this', 'that', 'these',
                     'those', 'what', '的', '是', '在', '有', '和', '與', '了',
                     '我', '你', '他', '她', '它', '們', '這', '那', '要', '會',
                     '能', '可以', '一個', '什麼', '怎麼', '如何'}

        return [
            t for t in all_tokens
            if (len(t) > 1 or t.isdigit()) and t not in stopwords
        ]

    def _bm25_score(
        self,
        query_tokens: list,
        allowed_indices: set[int] | None = None,
        *,
        index: Bm25Index | None = None,
    ) -> list:
        """計算所有 chunks 的 BM25 分數

        `index` 不給就用 retrieval 那一套（含 ctx）。決策路徑要傳
        `self._bm25_gate`——lexical 決策不能看見生成文字。

        返回: [(score, chunk_idx), ...] 按分數降序排列
        """
        bm25 = index or self._bm25
        if not bm25 or not bm25.index or not query_tokens:
            return []

        scores = [0.0] * len(self.chunks)
        k1 = BM25_K1
        b = BM25_B
        avgdl = bm25.avg_doc_len or 1.0

        for token in query_tokens:
            if token not in bm25.index:
                continue

            idf = bm25.idf.get(token, 0.0)
            term_docs = bm25.index[token]

            for chunk_idx, tf in term_docs.items():
                if allowed_indices is not None and chunk_idx not in allowed_indices:
                    continue
                dl = bm25.doc_lens[chunk_idx]
                # BM25 公式
                numerator = tf * (k1 + 1)
                denominator = tf + k1 * (1 - b + b * dl / avgdl)
                scores[chunk_idx] += idf * numerator / denominator

        # 正規化到 0-1（用 max 正規化）
        max_score = max(scores) if scores else 1.0
        if max_score > 0:
            scores = [s / max_score for s in scores]

        # 返回 (score, chunk_idx) 列表，按分數降序
        scored = [
            (scores[i], i) for i in range(len(scores))
            if scores[i] >= BM25_MIN_RELATIVE_SCORE
            and (allowed_indices is None or i in allowed_indices)
        ]
        scored.sort(reverse=True, key=lambda x: x[0])
        return scored

    def _gate_bm25_map(
        self, query_tokens: list, chunk_indices: list, allowed_indices: set[int] | None = None
    ) -> dict:
        """候選 chunk 的 content-only BM25 分數（決策訊號）。

        沒有 ctx 時 gate 索引就是 retrieval 索引的別名，這一趟等於免費。
        """
        if self._bm25_gate is None or not query_tokens:
            return {}
        wanted = set(chunk_indices)
        return {
            chunk_idx: score
            for score, chunk_idx in self._bm25_score(
                query_tokens, allowed_indices=allowed_indices, index=self._bm25_gate
            )
            if chunk_idx in wanted
        }

    def _rrf_fusion(self, embedding_ranks: list, bm25_ranks: list, k: int = RRF_K) -> list:
        """RRF (Reciprocal Rank Fusion) 融合兩個排名列表

        RRF 公式：RRF(d) = sum( 1 / (k + rank(d)) )

        Args:
            embedding_ranks: [(emb_score, chunk_idx), ...] 按分數降序
            bm25_ranks: [(bm25_score, chunk_idx), ...] 按分數降序
            k: RRF 常數（預設 60）

        Returns:
            [Candidate, ...] 按 RRF 分數降序（gate 分數由呼叫端補）
        """
        # 建立 chunk_idx -> rank 的映射
        emb_rank_map = {chunk_idx: rank for rank, (_, chunk_idx) in enumerate(embedding_ranks)}
        bm25_rank_map = {chunk_idx: rank for rank, (_, chunk_idx) in enumerate(bm25_ranks)}

        # 建立 chunk_idx -> score 的映射
        emb_score_map = {chunk_idx: score for score, chunk_idx in embedding_ranks}
        bm25_score_map = {chunk_idx: score for score, chunk_idx in bm25_ranks}

        # 取所有候選的 union
        all_chunks = set(emb_rank_map.keys()) | set(bm25_rank_map.keys())

        candidates = []
        for chunk_idx in all_chunks:
            rrf = 0.0
            if chunk_idx in emb_rank_map:
                rrf += 1.0 / (k + emb_rank_map[chunk_idx])
            if chunk_idx in bm25_rank_map:
                rrf += 1.0 / (k + bm25_rank_map[chunk_idx])

            candidates.append(Candidate(
                chunk_idx=chunk_idx,
                chunk=self.chunks[chunk_idx],
                rrf_score=rrf,
                retrieval_score=emb_score_map.get(chunk_idx, 0.0),
                retrieval_bm25=bm25_score_map.get(chunk_idx, 0.0),
            ))

        candidates.sort(reverse=True, key=lambda c: c.rrf_score)
        return candidates

    def _check_reranker_available(self) -> bool:
        """檢查 reranker 模型是否可用

        改進：檢查 RERANKER_MODEL 是否已安裝，而非只要有任意 reranker 就視為可用
        避免設定了 A 模型但機器上只有 B 模型，導致每次都先嘗試 A → 失敗 → fallback
        """
        if self._reranker_available is not None:
            return self._reranker_available

        try:
            self._reranker_available = llama_client.is_ready(LLAMA_RERANK_BASE_URL)
        except Exception:
            self._reranker_available = False

        return self._reranker_available

    def _cosine_similarity(self, a: list, b: list) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def _get_embedding(self, text: str) -> list:
        """取得 embedding（使用 LRU cache 加速重複查詢）"""
        # 正規化文字以提高 cache 命中率
        normalized = _normalize_text_for_cache(text)
        # 使用 cached function（回傳 tuple，需轉 list）
        result = _cached_get_embedding(normalized)
        return list(result)

    def _extract_keywords(self, text: str) -> set:
        text = re.sub(r'[^\w\s\-_]', ' ', text.lower())
        words = text.split()
        stopwords = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been',
                     'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
                     'would', 'could', 'should', 'may', 'might', 'must', 'shall',
                     'can', 'need', 'to', 'of', 'in', 'for', 'on', 'with', 'at',
                     'by', 'from', 'as', 'into', 'through', 'during', 'before',
                     'after', 'above', 'below', 'between', 'under', 'again',
                     'then', 'once', 'here', 'there', 'when', 'where', 'why',
                     'how', 'all', 'each', 'few', 'more', 'most', 'other',
                     'some', 'such', 'no', 'nor', 'not', 'only', 'own', 'same',
                     'so', 'than', 'too', 'very', 'just', 'and', 'but', 'if',
                     'or', 'because', 'until', 'while', 'this', 'that', 'these',
                     'those', 'what', '的', '是', '在', '有', '和', '與', '了',
                     '我', '你', '他', '她', '它', '們', '這', '那', '要', '會',
                     '能', '可以'}
        keywords = {w for w in words if len(w) > 2 and w not in stopwords}
        if HAS_JIEBA:
            for token in jieba.cut(text, cut_all=False):
                if len(token) > 1 and re.search(r'[\u4e00-\u9fff]', token) and token not in stopwords:
                    keywords.add(token)
        else:
            for token in re.findall(r'[\u4e00-\u9fff]{2,}', text):
                if token not in stopwords:
                    keywords.add(token)
        return keywords

    def _keyword_score(self, query_keywords: set, chunk_content: str) -> float:
        """計算關鍵字匹配分數

        改進：使用 word boundary 匹配而非 substring，避免 'log' 命中 'catalog'
        """
        if not query_keywords:
            return 0.0

        # 將 chunk 分解為 word tokens（以非字母數字字元分割）
        chunk_tokens = set(re.findall(r'\b[A-Za-z_][A-Za-z0-9_]*\b', chunk_content.lower()))

        matches = 0
        for kw in query_keywords:
            kw_lower = kw.lower()
            # 精確 token 匹配
            if kw_lower in chunk_tokens:
                matches += 1
            # 或使用 word boundary regex
            elif re.search(r'\b' + re.escape(kw_lower) + r'\b', chunk_content.lower()):
                matches += 1

        return matches / len(query_keywords)

    def _expand_query(self, question: str, force: bool = False) -> list[str]:
        """用 LLM 生成額外的搜尋關鍵字（P0-3 升級版：雙語+符號友善）

        P0-3 改進：
        - 保留原始符號（如 NUM_CTX, CODE_RAG_THRESHOLD）
        - 支援中文和英文混合關鍵字
        - 不再過濾掉非純英文 token
        """
        if not USE_QUERY_EXPANSION and not force:
            return [question]

        # P0-3: 先提取問題中的符號（大寫+底線）
        preserved_symbols = []
        if QUERY_PRESERVE_SYMBOLS:
            preserved_symbols = re.findall(QUERY_SYMBOL_PATTERN, question)

        try:
            # P0-3: 改進 prompt，允許中英文混合關鍵字
            prompt = f"""從以下問題中提取 3-5 個適合用於搜尋技術文件的關鍵字。
可以是中文或英文，保留原始的技術術語和符號名稱（如 NUM_CTX, THRESHOLD 等）。
只輸出關鍵字，用逗號分隔，不要解釋。

問題: {question}

關鍵字:"""

            model = config.require_main_model()
            data = llama_client.native_completion(
                base_url=LLAMA_BASE_URL,
                prompt=prompt,
                temperature=0,
                stream=False,
                timeout=30,
            )
            result = (data.get("content") or data.get("response") or "").strip()

            # 同時支援半形和全形逗號
            raw_keywords = re.split(r'[,，]', result)
            keywords = []
            for kw in raw_keywords:
                kw = kw.strip()
                # P0-3: 放寬過濾條件，允許中文和符號
                # 只過濾過長或空的 token
                if kw and len(kw) <= 40:
                    # 避免整句被當作關鍵字（超過 4 個空格分隔的詞）
                    if len(kw.split()) <= 4:
                        keywords.append(kw)

            keywords = keywords[:5]

            # P0-3: 確保原始符號被保留
            for sym in preserved_symbols:
                if sym not in keywords:
                    keywords.append(sym)

            if keywords:
                expanded = f"{question} {' '.join(keywords)}"
                return [question, expanded]

        except Exception:
            pass

        return [question]

    def _generate_multi_queries(self, question: str) -> list[str]:
        """P1 改進：生成多個 query 變體以提高召回率（P0-3 升級：雙語+符號友善）

        策略：
        1. key_terms: 抽取關鍵術語（保留原始符號）
        2. translate: 雙語互譯（中→英 或 英→中）
        3. code_hint: 猜測可能的函式名/旗標名

        P0-3 改進：
        - 雙向翻譯：中文問題加英文版，英文問題加中文版
        - 符號友善：保留 NUM_CTX 等大寫符號
        - 放寬術語過濾，允許中英文混合

        返回: [原始 query, 變體1, 變體2, ...]
        """
        if not MULTI_QUERY_ENABLED:
            return [question]

        queries = [question]

        # 判斷問題語言
        has_chinese = bool(re.search(r'[\u4e00-\u9fff]', question))
        has_english = bool(re.search(r'[a-zA-Z]{3,}', question))  # 至少 3 個連續英文字母

        # P0-3: 提取問題中的符號（供後續保留）
        preserved_symbols = []
        if QUERY_PRESERVE_SYMBOLS:
            preserved_symbols = re.findall(QUERY_SYMBOL_PATTERN, question)

        try:
            model = config.require_main_model()

            # 根據啟用的類型生成變體
            for query_type in MULTI_QUERY_TYPES[:MULTI_QUERY_COUNT]:
                if query_type == "key_terms":
                    # P0-3: 改進 prompt，保留符號和允許中英文混合
                    prompt = f"""從以下問題中提取 3-5 個最重要的技術術語，用於搜尋技術文件。
保留原始的符號名稱（如 NUM_CTX, THRESHOLD）和技術術語。
可以是中文或英文，只輸出術語，用逗號分隔，不要解釋。

問題: {question}

術語:"""

                elif query_type == "translate":
                    # P0-3: 雙語互譯（不只是中→英）
                    if QUERY_BILINGUAL_ENABLED:
                        if has_chinese and not has_english:
                            # 純中文問題 → 翻譯成英文
                            prompt = f"""把以下中文問題翻譯成簡潔的英文搜尋查詢，保留技術術語和符號名稱。
只輸出英文查詢，不要解釋。

中文: {question}

English:"""
                        elif has_english and not has_chinese:
                            # 純英文問題 → 翻譯成中文（增加中文文件召回）
                            prompt = f"""把以下英文問題翻譯成簡潔的中文搜尋查詢，保留技術術語和符號名稱。
只輸出中文查詢，不要解釋。

English: {question}

中文:"""
                        elif has_chinese and has_english:
                            # 中英混合 → 生成純英文版本
                            prompt = f"""把以下問題轉換成純英文的搜尋查詢，保留所有技術術語和符號名稱。
只輸出英文查詢，不要解釋。

問題: {question}

English:"""
                        else:
                            continue
                    else:
                        # 原有邏輯：只有中文才翻譯
                        if has_chinese:
                            prompt = f"""把以下中文問題翻譯成簡潔的英文搜尋查詢，保留技術術語。
只輸出英文查詢，不要解釋。

中文: {question}

English:"""
                        else:
                            continue

                elif query_type == "code_hint":
                    # 猜測可能的函式名/旗標名
                    prompt = f"""根據以下問題，猜測可能相關的程式碼元素（函式名、變數名、旗標、常數名等）。
只輸出 3-5 個可能的程式碼元素名稱，用逗號分隔。

問題: {question}

程式碼元素:"""

                else:
                    continue

                try:
                    data = llama_client.native_completion(
                        base_url=LLAMA_BASE_URL,
                        prompt=prompt,
                        temperature=0.3,
                        stream=False,
                        timeout=20,
                    )
                except Exception:
                    continue

                result = (data.get("content") or data.get("response") or "").strip()
                if result and len(result) < 200:
                    if query_type == "translate":
                        # P0-3: 翻譯結果直接加入，並附上原始符號
                        translated = result
                        for sym in preserved_symbols:
                            if sym not in translated:
                                translated = f"{translated} {sym}"
                        queries.append(translated)
                    else:
                        # 組合原始問題和術語
                        terms = [t.strip() for t in re.split(r'[,，]', result)]
                        # P0-3: 放寬過濾，允許中英文混合和符號
                        terms = [t for t in terms if t and len(t) <= 40 and len(t.split()) <= 3]
                        # 確保符號被保留
                        for sym in preserved_symbols:
                            if sym not in terms:
                                terms.append(sym)
                        if terms:
                            queries.append(f"{question} {' '.join(terms[:6])}")

        except Exception:
            pass

        return queries[:MULTI_QUERY_COUNT + 1]  # 原始 + N 個變體

    def _is_numeric_query(self, question: str) -> bool:
        """判斷是否為數值查詢（含數字/最大/預設等）

        這類查詢通常有精確答案，不適合 query expansion 避免 drift
        """
        numeric_patterns = [
            r'\d+',           # 任何數字
            r'最[大小]',       # 最大/最小
            r'[上下]限',       # 上限/下限
            r'預設',          # 預設值
            r'default',       # default
            r'多少',          # 多少
            r'幾[個條筆次]',   # 幾個/幾條
        ]
        for pattern in numeric_patterns:
            if re.search(pattern, question, re.IGNORECASE):
                return True
        return False

    def _exact_literals(self, text: str) -> set[str]:
        """Extract values whose exact spelling is evidence (hex, decimal, version-like)."""
        return {
            match.group(0).lower()
            for match in re.finditer(
                r'(?<![A-Za-z0-9_])(?:0x[0-9A-Fa-f]+|v?\d+(?:\.\d+)+|\d+)(?![A-Za-z0-9_])',
                text,
                re.IGNORECASE,
            )
        }

    def _has_lexical_numeric_evidence(
        self, question: str, bm25_score: float, chunk: dict
    ) -> bool:
        """數值題的 lexical 放行判定。**只吃 gate BM25**。

        呼叫端必須傳 content-only 的 BM25 分數：「最大值是多少」這種沒有明示
        數字的問題走的是 `bm25_score >= 0.75` 這條 BM25-only 的路，如果分數裡
        含 LLM 生成的脈絡，生成文字就能冒充數值證據。haystack 這邊同理，只看
        source / section / 去前綴本文，不看 ctx。
        """
        if bm25_score <= 0 or not self._is_numeric_query(question):
            return False
        literals = self._exact_literals(question)
        if not literals:
            # Questions such as "最大值是多少" still need a lexical route.  A
            # normalized BM25 leader is admitted to the reranker, not trusted
            # as the final answer by itself.
            return bm25_score >= 0.75
        haystack = " ".join([
            str(chunk.get("source", "")),
            str(chunk.get("section", "")),
            self._content_for_bm25(chunk),
        ]).lower()
        return bool(literals & self._exact_literals(haystack))

    def _get_source_weight(self, chunk: dict) -> float:
        """取得 chunk 的來源權重

        權威來源（spec/manual/api）權重較高
        低可靠來源（chat/diagram/web）權重較低
        """
        chunk_type = chunk.get('type', 'default')
        return SOURCE_TYPE_WEIGHTS.get(chunk_type, SOURCE_TYPE_WEIGHTS['default'])

    def _apply_source_weighting(self, candidates: list) -> list:
        """對候選結果應用來源權重（retrieval 與 gate 一視同仁）。

        兩套 dense 分數都乘同一個權重：來源權重是確定性的 metadata，不是生成物，
        對 gate 訊號同樣適用；只加權其中一邊會讓兩套分數不可比。
        """
        for candidate in candidates:
            weight = self._get_source_weight(candidate.chunk)
            candidate.rrf_score *= weight
            candidate.retrieval_score *= weight
            candidate.gate_score *= weight

        candidates.sort(reverse=True, key=lambda c: c.rrf_score)
        return candidates

    def _select_with_pollution_control(self, chunks: list, pollution_risk: str,
                                        emb_scores: list) -> list:
        """根據污染風險選擇 REF，寧缺勿濫

        高污染風險時：
        1. 減少 REF 數量
        2. 提高最低分數門檻
        3. 優先選擇權威來源

        Args:
            chunks: 候選 chunk 列表
            pollution_risk: "low" / "medium" / "high"
            emb_scores: 對應的 **gate** embedding scores（這裡有 min_score 門檻，
                是決策；用含生成脈絡的分數會讓弱原文被放行）

        Returns:
            篩選後的 chunk 列表
        """
        if not chunks:
            return []

        # 根據污染風險決定最大數量
        max_count = POLLUTION_RISK_TOP_K.get(pollution_risk, POLLUTION_RISK_TOP_K['low'])

        # 高污染風險時，提高最低分數門檻
        min_score = 0.0
        if pollution_risk in ('medium', 'high'):
            min_score = POLLUTION_RISK_MIN_SCORE

        # 篩選：只保留分數足夠高的
        selected = []
        for chunk, score in zip(chunks, emb_scores):
            if score >= min_score:
                selected.append((chunk, score))

        # 按（來源權重 * 分數）重新排序
        selected.sort(key=lambda x: self._get_source_weight(x[0]) * x[1], reverse=True)

        # 截取前 max_count 個
        return [c for c, _ in selected[:max_count]]

    def _deduplicate_chunks(self, chunks: list, similarity_threshold: float = 0.85) -> list:
        """P0 改進：Chunk 去重（尤其 web/OCR 來源容易重複）

        使用 jaccard similarity 判斷兩個 chunk 是否重複

        Args:
            chunks: chunk 列表
            similarity_threshold: 相似度門檻，超過則視為重複

        Returns:
            去重後的 chunk 列表
        """
        if not chunks or len(chunks) <= 1:
            return chunks

        def get_tokens(text: str) -> set:
            """將文字轉換為 token set"""
            words = re.findall(r'\b[a-zA-Z0-9_]+\b', text.lower())
            return set(words)

        def jaccard_similarity(set1: set, set2: set) -> float:
            """計算 Jaccard 相似度"""
            if not set1 or not set2:
                return 0.0
            intersection = len(set1 & set2)
            union = len(set1 | set2)
            return intersection / union if union > 0 else 0.0

        # 預計算所有 chunk 的 token set
        chunk_tokens = []
        for chunk in chunks:
            content = chunk.get('content', '')
            tokens = get_tokens(content)
            chunk_tokens.append(tokens)

        # 去重：保留每組相似 chunks 中的第一個
        keep_indices = []
        for i in range(len(chunks)):
            is_duplicate = False
            for kept_idx in keep_indices:
                sim = jaccard_similarity(chunk_tokens[i], chunk_tokens[kept_idx])
                if sim >= similarity_threshold:
                    is_duplicate = True
                    break
            if not is_duplicate:
                keep_indices.append(i)

        return [chunks[i] for i in keep_indices]

    def _filter_noisy_chunks(self, chunks: list) -> list:
        """P0 改進：過濾噪音 chunk（web/OCR 來源的常見問題）

        過濾條件：
        1. 內容太短（< 50 字元）
        2. 重複字元比例過高（OCR 錯誤特徵）
        3. 幾乎全是標點或數字（無意義內容）
        """
        if not chunks:
            return chunks

        MIN_CONTENT_LEN = 50
        MAX_REPEAT_RATIO = 0.5  # 重複字元比例上限
        MIN_TEXT_RATIO = 0.3    # 有意義文字比例下限

        filtered = []
        for chunk in chunks:
            content = chunk.get('content', '')

            # 檢查 1：內容長度
            if len(content) < MIN_CONTENT_LEN:
                continue

            # 檢查 2：重複字元比例（偵測 OCR 錯誤如 "......." 或 "======"）
            char_counts = {}
            for c in content:
                char_counts[c] = char_counts.get(c, 0) + 1
            if char_counts:
                max_char_count = max(char_counts.values())
                repeat_ratio = max_char_count / len(content)
                if repeat_ratio > MAX_REPEAT_RATIO:
                    continue

            # 檢查 3：有意義文字比例（字母+中文）
            meaningful_chars = sum(1 for c in content if c.isalpha() or '\u4e00' <= c <= '\u9fff')
            text_ratio = meaningful_chars / len(content)
            if text_ratio < MIN_TEXT_RATIO:
                continue

            filtered.append(chunk)

        return filtered

    def _should_expand_query(self, candidates: list, question: str = "",
                             threshold: float = None) -> bool:
        """判斷是否需要 Query Expansion

        改進：
        1. 使用 MULTI_QUERY_MIN_SCORE_TRIGGER 作為門檻（條件式啟用）
        2. 數值查詢跳過 expansion（避免 query drift）
        3. 高信心時跳過（top_emb_score > 門檻）

        條件：候選數量不足 或 最高 embedding 分數偏低
        """
        # 使用 config 中的門檻
        if threshold is None:
            threshold = MULTI_QUERY_MIN_SCORE_TRIGGER

        # 數值查詢跳過 expansion（避免 drift）
        if MULTI_QUERY_SKIP_NUMERIC and question and self._is_numeric_query(question):
            return False

        if not candidates:
            return True
        if len(candidates) < 3:
            return True

        # 「信心夠高就不擴寫」是決策，看 gate 分數：content-only 的信心低才擴寫，
        # 不能讓生成脈絡把信心撐高而少召回。
        ranked = self._decision_order(candidates)
        top_gate_score = ranked[0].gate_score if ranked else 0

        # 高信心時跳過 expansion
        return top_gate_score < threshold

    def _matching_chunk_indices(self, metadata_filter: dict | None) -> set[int] | None:
        """Resolve exact metadata filters before either retrieval branch applies top-k."""
        if not metadata_filter:
            return None

        allowed = set(range(len(self.chunks)))
        for key in ("source", "type", "section"):
            if key not in metadata_filter or metadata_filter[key] in (None, ""):
                continue
            raw = metadata_filter[key]
            values = raw if isinstance(raw, (list, tuple, set)) else [raw]
            expected = {str(value).casefold() for value in values}
            matched = set()
            for index, chunk in enumerate(self.chunks):
                actual = str(chunk.get(key, ""))
                candidates = {actual.casefold()}
                if key == "source":
                    candidates.add(Path(actual).name.casefold())
                if candidates & expected:
                    matched.add(index)
            allowed &= matched
        return allowed

    def _search_once(
        self,
        question: str,
        q_emb: list,
        candidate_k: int,
        allowed_indices: set[int] | None,
    ) -> list:
        """Run one dense+lexical recall pass and keep scores in RRF units."""
        recall_k = max(1, candidate_k * 2)
        if HAS_NUMPY and self._retrieval_matrix() is not None and self._embeddings_normalized:
            embedding_ranks = self._embedding_search_numpy(
                q_emb, recall_k, allowed_indices=allowed_indices
            )
        else:
            embedding_ranks = self._embedding_search_fallback(
                q_emb, recall_k, allowed_indices=allowed_indices
            )

        query_tokens = []
        if BM25_ENABLED and self._bm25_index:
            query_tokens = self._tokenize_for_bm25(question)
            # USE 關掉時連 lexical 排序都走 content-only 索引
            bm25_ranks = self._bm25_score(
                query_tokens,
                allowed_indices=allowed_indices,
                index=self._ranking_bm25_index(),
            )[:recall_k]
        else:
            query_keywords = self._extract_keywords(question) if USE_HYBRID_SEARCH else set()
            bm25_ranks = self._keyword_search_fallback(
                query_keywords, recall_k, allowed_indices=allowed_indices
            )

        if RRF_ENABLED and (embedding_ranks or bm25_ranks):
            candidates = self._rrf_fusion(embedding_ranks, bm25_ranks)
        elif embedding_ranks:
            candidates = [
                Candidate(chunk_idx=idx, chunk=self.chunks[idx], rrf_score=emb_score,
                          retrieval_score=emb_score)
                for emb_score, idx in embedding_ranks
            ]
        else:
            candidates = [
                Candidate(chunk_idx=idx, chunk=self.chunks[idx], rrf_score=bm25_score,
                          retrieval_bm25=bm25_score)
                for bm25_score, idx in bm25_ranks
            ]

        self._fill_gate_scores(candidates, q_emb, query_tokens, allowed_indices)
        return candidates

    def _fill_gate_scores(
        self,
        candidates: list,
        q_emb: list,
        query_tokens: list,
        allowed_indices: set[int] | None,
    ) -> None:
        """替候選補上 content-only 的 dense / lexical 分數（決策訊號）。

        沒有 ctx 的 KB 兩套訊號同源，gate 分數會等於 retrieval 分數；有 ctx 時
        才真的分開。決策點一律只讀 gate_*。
        """
        if not candidates:
            return

        # 兩套訊號同源時直接沿用，不重跑一次 dense/BM25。沒有 ctx 的 KB（也就是
        # 功能關掉的所有既有部署）會走這條，查詢成本與加入本功能前完全相同。
        same_dense = self._gate_matrix() is self._retrieval_matrix()
        same_lexical = self._ranking_bm25_index() is self._bm25_gate
        if same_dense and same_lexical:
            for candidate in candidates:
                candidate.gate_score = candidate.retrieval_score
                candidate.gate_bm25 = candidate.retrieval_bm25
            return

        indices = [c.chunk_idx for c in candidates]
        dense = (
            {} if same_dense
            else (self._gate_scores_for(indices, q_emb) if HAS_NUMPY else {})
        )
        lexical = (
            {} if same_lexical
            else self._gate_bm25_map(query_tokens, indices, allowed_indices)
        )
        for candidate in candidates:
            if same_dense:
                candidate.gate_score = candidate.retrieval_score
            elif dense:
                candidate.gate_score = dense.get(candidate.chunk_idx, 0.0)
            else:
                # 沒有 numpy 的 legacy 路徑：chunk 上有 inline 向量，且這種 KB
                # 一定沒有 ctx（ctx 是新格式），retrieval 分數就是 content-only。
                candidate.gate_score = candidate.retrieval_score
            candidate.gate_bm25 = (
                candidate.retrieval_bm25 if same_lexical
                else lexical.get(candidate.chunk_idx, 0.0)
            )

    def _merge_expansion_scores(
        self, base_scores: list, expansion_scores: list, weight: float = 0.9
    ) -> list:
        """Union a variant's recall with the base list without mixing score scales.

        兩套分數各自合併：gate 分數同樣取 max，才不會因為只有變體召回到某個
        chunk 就讓它的決策分數變成 0。
        """
        merged: dict[int, Candidate] = {c.chunk_idx: c for c in base_scores}
        for candidate in expansion_scores:
            existing = merged.get(candidate.chunk_idx)
            if existing is None:
                merged[candidate.chunk_idx] = Candidate(
                    chunk_idx=candidate.chunk_idx,
                    chunk=candidate.chunk,
                    rrf_score=candidate.rrf_score * weight,
                    retrieval_score=candidate.retrieval_score * weight,
                    gate_score=candidate.gate_score * weight,
                    retrieval_bm25=candidate.retrieval_bm25,
                    gate_bm25=candidate.gate_bm25,
                )
                continue
            existing.rrf_score += candidate.rrf_score * weight
            existing.retrieval_score = max(
                existing.retrieval_score, candidate.retrieval_score * weight
            )
            existing.gate_score = max(existing.gate_score, candidate.gate_score * weight)
            existing.retrieval_bm25 = max(existing.retrieval_bm25, candidate.retrieval_bm25)
            existing.gate_bm25 = max(existing.gate_bm25, candidate.gate_bm25)

        result = list(merged.values())
        result.sort(reverse=True, key=lambda c: c.rrf_score)
        return result

    def _hybrid_search(
        self,
        question: str,
        candidate_k: int = KNOWLEDGE_CANDIDATE_K,
        metadata_filter: dict | None = None,
    ) -> list:
        """混合搜尋：Embedding + BM25 + RRF 融合

        P0 改進：
        1. 使用真正的 BM25（取代簡單 keyword matching）
        2. 使用 RRF（取代線性加權）融合 embedding 和 BM25 排名
        3. 支援 numpy 向量化加速
        4. 條件式 Query Expansion

        返回格式：[(rrf_score, emb_score, bm25_score, chunk), ...]
        """
        if not self.loaded or not self.chunks:
            return []

        allowed_indices = self._matching_chunk_indices(metadata_filter)
        if allowed_indices is not None and not allowed_indices:
            return []

        # 取得 query embedding
        q_emb = self._get_embedding(question)
        scores = self._search_once(question, q_emb, candidate_k, allowed_indices)

        first_round = scores[:candidate_k]

        # P1 改進：Multi-Query - 條件式啟用（候選不足/分數偏低/非數值查詢）
        if self._should_expand_query(first_round, question=question):
            if MULTI_QUERY_ENABLED:
                # 使用完整的 multi-query
                multi_queries = self._generate_multi_queries(question)
            elif USE_QUERY_EXPANSION:
                # Fallback: 使用簡單的 query expansion
                multi_queries = self._expand_query(question, force=True)
            else:
                multi_queries = [question]

            if len(multi_queries) > 1:
                # 用額外的 queries 增強 embedding 召回
                for mq in multi_queries[1:]:
                    mq_emb = self._get_embedding(mq)
                    scores = self._update_scores_with_expansion(
                        scores,
                        mq_emb,
                        expanded_query=mq,
                        candidate_k=candidate_k,
                        allowed_indices=allowed_indices,
                    )

        return scores[:candidate_k]

    def _embedding_search_numpy(
        self, q_emb: list, top_k: int, allowed_indices: set[int] | None = None
    ) -> list:
        """使用 numpy 向量化的 embedding 搜尋

        返回: [(emb_score, chunk_idx), ...] 按分數降序
        """
        if top_k <= 0:
            return []
        # 正規化 query embedding
        q_vec = np.array(q_emb, dtype=np.float32)
        if self._embeddings.shape[1] != q_vec.shape[0]:
            raise RuntimeError(
                "query embedding dimension mismatch: "
                f"query={q_vec.shape[0]}, knowledge={self._embeddings.shape[1]}"
            )
        q_norm = np.linalg.norm(q_vec)
        if q_norm > 0:
            q_vec = q_vec / q_norm

        # 批次計算所有 cosine similarity
        emb_scores = np.dot(self._retrieval_matrix(), q_vec)
        eligible = np.array([
            arr_idx for arr_idx, chunk_idx in enumerate(self._embedding_indices)
            if allowed_indices is None or chunk_idx in allowed_indices
        ], dtype=int)
        total = eligible.shape[0]
        if total == 0:
            return []
        k = min(top_k, total)
        if total <= k:
            idxs = eligible
        else:
            local = np.argpartition(-emb_scores[eligible], k - 1)[:k]
            idxs = eligible[local]
        idxs = idxs[np.argsort(-emb_scores[idxs])]
        return [(float(emb_scores[i]), self._embedding_indices[i]) for i in idxs]

    def _embedding_search_fallback(
        self, q_emb: list, top_k: int, allowed_indices: set[int] | None = None
    ) -> list:
        """Fallback：Python 迴圈版 embedding 搜尋

        返回: [(emb_score, chunk_idx), ...] 按分數降序
        """
        if top_k <= 0:
            return []
        import heapq
        results = []
        for idx, chunk in enumerate(self.chunks):
            if allowed_indices is not None and idx not in allowed_indices:
                continue
            emb = chunk.get("embedding", [])
            if emb:
                score = self._cosine_similarity(q_emb, emb)
                results.append((score, idx))

        return heapq.nlargest(top_k, results, key=lambda x: x[0])

    def _keyword_search_fallback(
        self, query_keywords: set, top_k: int, allowed_indices: set[int] | None = None
    ) -> list:
        """Fallback：舊的 keyword matching（當 BM25 未啟用時）

        返回: [(kw_score, chunk_idx), ...] 按分數降序
        """
        results = []
        for idx, chunk in enumerate(self.chunks):
            if allowed_indices is not None and idx not in allowed_indices:
                continue
            content = chunk.get("content", "")
            score = self._keyword_score(query_keywords, content)
            if score > 0:
                results.append((score, idx))

        results.sort(reverse=True, key=lambda x: x[0])
        return results[:top_k]

    def _update_scores_with_expansion(
        self,
        scores: list,
        eq_emb: list,
        *,
        expanded_query: str,
        candidate_k: int,
        allowed_indices: set[int] | None = None,
    ) -> list:
        """Return an RRF-scale union including chunks recalled only by a variant."""
        expansion_scores = self._search_once(
            expanded_query, eq_emb, candidate_k, allowed_indices
        )
        return self._merge_expansion_scores(scores, expansion_scores)

    def _should_rerank(self, candidates: list, top_k: int, is_strict_mode: bool = False) -> bool:
        """判斷是否需要 rerank

        改進：
        - RERANKER_ALWAYS_ON = True 時，有 reranker 就一律使用
        - 嚴格模式下強制 rerank（STRICT_MODE_RERANK_REQUIRED）
        - 高信心時跳過 rerank（top_emb_score > RERANKER_SKIP_THRESHOLD）
        - 否則使用條件觸發
        """
        # One item cannot be reordered.  Candidate count relative to requested
        # output is not a reason to skip: strict/low-confidence queries often
        # have <= output_k candidates and are exactly where reranking matters.
        if len(candidates) <= 1:
            return False

        # 分數用 gate（content-only）而不是 RRF：RRF 範圍約 0.01-0.03，和固定門檻
        # 不在同一量級；而「跳過 rerank」是決策，不能讓生成脈絡抬高的分數觸發跳過。
        candidates = self._decision_order(candidates)
        top_gate_score = candidates[0].gate_score if candidates else 0

        # 改進：高信心時跳過 rerank（減少不必要的延遲）
        # 但嚴格模式和 ALWAYS_ON 除外
        if not RERANKER_ALWAYS_ON and not is_strict_mode:
            if top_gate_score >= RERANKER_SKIP_THRESHOLD:
                return False

        # P0 改進：強制啟用 reranker
        if RERANKER_ALWAYS_ON:
            return True

        # 嚴格模式強制 rerank
        if is_strict_mode and STRICT_MODE_RERANK_REQUIRED:
            return True

        # Margin-based 判斷（P0 改進）- 全部走 gate 分數
        if MARGIN_ENABLED and len(candidates) >= 2:
            gap = candidates[0].gate_score - candidates[1].gate_score
            # top1-top2 差距太小 → 不確定，需要 rerank
            if gap < MARGIN_MIN_GAP:
                return True
            # top1 分數太低 → 需要更精確判斷
            if top_gate_score < MARGIN_LOW_SCORE:
                return True

        # 如果最高分很高（>0.6），且與第5名差距明顯（>0.1），不需要 rerank
        if top_gate_score > 0.6:
            fifth_gate_score = (
                candidates[min(4, len(candidates) - 1)].gate_score
                if len(candidates) > 4 else 0
            )
            if top_gate_score - fifth_gate_score > 0.1:
                return False

        # 如果前幾名分數太接近（差距 < 0.05），需要 rerank 來區分
        if len(candidates) >= 3:
            score_diff = candidates[0].gate_score - candidates[2].gate_score
            if score_diff < 0.05:
                return True

        # 其他情況：gate 分數較低時，需要 rerank
        return top_gate_score < 0.5

    def _rerank_fallback(self, question: str, candidates: list, top_k: int, reason: str) -> list:
        """Apply the configured fallback after the dedicated reranker cannot be used."""
        policy = config.RERANK_FALLBACK_POLICY
        if policy == "embedding":
            return [c.chunk for c in candidates[:top_k]]
        if policy == "main_model":
            return self._rerank_with_llm(question, candidates, top_k)
        if policy == "error":
            raise RuntimeError(
                "RAG reranker unavailable and AICODE_RERANK_FALLBACK_POLICY=error. "
                f"Reason: {reason}"
            )
        raise RuntimeError(f"Unknown RERANK_FALLBACK_POLICY: {policy!r}")

    def _rerank_with_model(self, question: str, candidates: list, top_k: int,
                           is_strict_mode: bool = False) -> list:
        """重排並保留 cross-encoder 分數，回 [(score, chunk), ...]。

        分數會一路傳到 MMR 當 relevance。以前 rerank 完就把分數丟掉，MMR 再拿
        `cosine(query, chunk_embedding)` 重算相關度——等於把 cross-encoder 的排序
        整份蓋掉，reranker 實際上只剩「篩候選」的作用。實測（真實 spec）：
        reranker 排第一的 chunk 被 MMR 直接降到第二、甚至剔除。

        沒有走到 cross-encoder 的路徑（跳過 rerank、fallback）score 是 None，
        MMR 會退回原本的 embedding 相關度。
        """
        if not candidates:
            return []

        if not USE_RERANKER or len(candidates) <= 1:
            return [(None, c.chunk) for c in candidates[:top_k]]

        # 條件觸發：判斷是否真的需要 rerank
        if not self._should_rerank(candidates, top_k, is_strict_mode):
            return [(None, c.chunk) for c in candidates[:top_k]]

        # Input pool and output count are separate.  RERANKER_TOP_N is the
        # final query cap; the cross-encoder must see a much wider pool so a
        # rank-7..30 item can be rescued and MMR still has choices.
        rerank_count = min(len(candidates), max(15, top_k * 3))

        if self._check_reranker_available():
            try:
                items = candidates[:rerank_count]
                # reranker 的 document 側是檢索訊號（排序），可以帶 ctx；
                # 組法的唯一定義在 context_signals，USE 關掉時逐位元組等同舊版。
                use_ctx = use_generated_context()
                passages = [
                    context_signals.reranker_passage(
                        item.chunk, use_ctx=use_ctx, max_chars=RERANKER_PASSAGE_MAX_CHARS
                    )
                    for item in items
                ]
                scores = llama_client.rerank(
                    base_url=LLAMA_RERANK_BASE_URL,
                    query=question,
                    documents=passages,
                    model=RERANKER_MODEL,
                    timeout=60,
                )
                if len(scores) != len(items):
                    raise RuntimeError(
                        f"reranker returned {len(scores)} scores for {len(items)} passages"
                    )
                scored = [(float(scores[i]), items[i].chunk) for i in range(len(items))]
                scored.sort(reverse=True, key=lambda x: x[0])
                return scored[:top_k]

            except Exception as exc:
                return [(None, chunk) for chunk in self._rerank_fallback(
                    question, candidates, top_k, f"dedicated reranker call failed: {exc}"
                )]

        return [(None, chunk) for chunk in self._rerank_fallback(
            question, candidates, top_k, "dedicated reranker is not reachable"
        )]

    def _rerank_with_llm(self, question: str, candidates: list, top_k: int) -> list:
        """LLM Reranking (fallback)"""
        if not candidates:
            return []

        docs_text = ""
        for i, candidate in enumerate(candidates[:15]):
            chunk = candidate.chunk
            content = chunk.get('content', '')[:500]
            source = chunk.get('source', '?')
            page = chunk.get('page', '?')
            docs_text += f"\n[DOC_{i}] ({source} p.{page}):\n{content}\n"

        rerank_prompt = f"""你是文件相關性評估專家。

用戶問題: {question}

請根據相關性排序，返回最相關的 {top_k} 個文件編號。
格式: DOC_0, DOC_2, DOC_5（逗號分隔，最相關在前）

候選文件:
{docs_text}

排序結果:"""

        try:
            model = config.require_main_model()
            data = llama_client.native_completion(
                base_url=LLAMA_BASE_URL,
                prompt=rerank_prompt,
                temperature=0,
                stream=False,
                timeout=60,
            )
            result = data.get("content") or data.get("response") or ""

            doc_indices = []
            for match in re.finditer(r'DOC_(\d+)', result):
                idx = int(match.group(1))
                if idx < len(candidates) and idx not in doc_indices:
                    doc_indices.append(idx)
                if len(doc_indices) >= top_k:
                    break

            if doc_indices:
                return [candidates[i].chunk for i in doc_indices]

        except Exception:
            pass

        return [c.chunk for c in candidates[:top_k]]

    @staticmethod
    def _normalized_relevance(relevance: list | None) -> list | None:
        """把 cross-encoder 分數壓到 [0, 1]，好跟餘弦的多樣性懲罰同量級。

        reranker 回的是 logit，範圍不固定（可能是負的），直接跟 [0,1] 的餘弦
        相減會讓 λ 失去意義。用候選池自己的 min-max：全部同分時一律給 1.0
        （此時 MMR 退化成純多樣性，正是我們要的）。任何一項缺分數就整批放棄，
        退回 embedding 相關度——半套的 relevance 比沒有更糟。
        """
        if not relevance or any(score is None for score in relevance):
            return None
        values = [float(score) for score in relevance]
        low, high = min(values), max(values)
        if high - low <= 1e-9:
            return [1.0] * len(values)
        span = high - low
        return [(value - low) / span for value in values]

    def _mmr_select(self, chunks: list, question_emb: list, k: int,
                    lambda_: float = MMR_LAMBDA, relevance: list | None = None) -> list:
        """Max Marginal Relevance 選擇：平衡相關性與多樣性

        `relevance` 是 cross-encoder 分數（跟 chunks 等長）。給了就用它當相關度，
        embedding 只負責算多樣性懲罰——否則 rerank 的結果會被這一步整份蓋掉。
        沒給就退回原本的 `cosine(query, chunk)`。
        """
        if not chunks:
            return chunks[:k]

        rel = self._normalized_relevance(relevance)
        if rel is not None and len(rel) != len(chunks):
            rel = None
        if rel is None and not question_emb:
            return chunks[:k]

        # 嘗試使用 numpy 加速
        if HAS_NUMPY and len(chunks) > 3:
            return self._mmr_select_numpy(chunks, question_emb, k, lambda_, rel)

        # Fallback：原始 Python 實作
        selected = []
        selected_embs = []
        relevance_by_id = {id(c): rel[i] for i, c in enumerate(chunks)} if rel else {}

        for _ in range(min(k, len(chunks))):
            best, best_score = None, -float('inf')

            for c in chunks:
                if c in selected:
                    continue

                c_emb = self._selection_vector(c)
                if rel:
                    sim_q = relevance_by_id.get(id(c), 0.0)
                elif c_emb:
                    sim_q = self._cosine_similarity(question_emb, c_emb)
                else:
                    sim_q = None

                if sim_q is None:
                    mmr_score = -1
                else:
                    sim_rep = 0.0
                    if selected_embs and c_emb:
                        sim_rep = max(self._cosine_similarity(c_emb, e) for e in selected_embs)
                    mmr_score = lambda_ * sim_q - (1 - lambda_) * sim_rep

                if mmr_score > best_score:
                    best, best_score = c, mmr_score

            if best is None:
                break

            selected.append(best)
            best_emb = self._selection_vector(best)
            if best_emb:
                selected_embs.append(best_emb)

        return selected

    def _mmr_select_numpy(self, chunks: list, question_emb: list, k: int, lambda_: float,
                          relevance: list | None = None) -> list:
        """使用 numpy 加速的 MMR 選擇"""
        # 收集有效的 embeddings
        valid_chunks = []
        embeddings = []
        valid_relevance = []

        for index, c in enumerate(chunks):
            emb = self._selection_vector(c)
            if emb:
                valid_chunks.append(c)
                embeddings.append(emb)
                if relevance is not None:
                    valid_relevance.append(relevance[index])

        if not valid_chunks:
            return chunks[:k]

        # 轉換為 numpy array 並正規化
        emb_matrix = np.array(embeddings, dtype=np.float32)
        norms = np.linalg.norm(emb_matrix, axis=1, keepdims=True)
        norms = np.where(norms > 0, norms, 1.0)
        emb_matrix = emb_matrix / norms

        q_vec = np.array(question_emb, dtype=np.float32)
        q_norm = np.linalg.norm(q_vec)
        if q_norm > 0:
            q_vec = q_vec / q_norm

        # 相關度：有 cross-encoder 分數就用它，否則退回與 query 的餘弦。
        if relevance is not None and len(valid_relevance) == len(valid_chunks):
            sim_to_query = np.asarray(valid_relevance, dtype=np.float32)
        else:
            sim_to_query = np.dot(emb_matrix, q_vec)

        n = len(valid_chunks)
        selected_indices = []
        selected_mask = np.zeros(n, dtype=bool)

        for _ in range(min(k, n)):
            # 計算每個候選的 MMR 分數
            mmr_scores = np.full(n, -np.inf)

            for i in range(n):
                if selected_mask[i]:
                    continue

                sim_q = sim_to_query[i]

                if selected_indices:
                    # 計算與已選取項目的最大相似度
                    selected_embs = emb_matrix[selected_indices]
                    sim_rep = np.max(np.dot(selected_embs, emb_matrix[i]))
                else:
                    sim_rep = 0.0

                mmr_scores[i] = lambda_ * sim_q - (1 - lambda_) * sim_rep

            # 選擇最高 MMR 分數的項目
            best_idx = np.argmax(mmr_scores)
            if mmr_scores[best_idx] == -np.inf:
                break

            selected_indices.append(best_idx)
            selected_mask[best_idx] = True

        return [valid_chunks[i] for i in selected_indices]

    def _upgrade_type(self, current_type: str, new_type: str) -> str:
        """升級 chunk type（warning > spec > doc）

        合併 chunk 時，只要任一 chunk 是 warning/spec，合併後的 type 應該升級
        """
        type_priority = {"warning": 3, "spec": 2, "doc": 1}
        current_prio = type_priority.get(current_type, 1)
        new_prio = type_priority.get(new_type, 1)
        if new_prio > current_prio:
            return new_type
        return current_type

    def _average_embeddings(self, emb_sum: list, emb_count: int) -> list:
        """Average embeddings from sum/count."""
        if not emb_sum or emb_count <= 0:
            return []
        return [v / emb_count for v in emb_sum]

    def _merge_adjacent_chunks(self, chunks: list) -> list:
        """合併同一頁的相鄰 chunk

        改進：
        1. 合併時 propagate type（warning > spec > doc）
        2. 不跨 section 合併（section 不同時視為不相鄰）
        """
        if not chunks or not KNOWLEDGE_MERGE_ADJACENT:
            return chunks

        sorted_chunks = sorted(
            chunks,
            key=lambda c: (c.get("source", ""), c.get("page", 0), c.get("chunk_index", 0))
        )

        merged = []
        buffer = None

        def _member_indices(chunk: dict) -> list:
            members = chunk.get("member_chunk_idx")
            if members:
                return list(members)
            index = chunk.get("chunk_idx")
            return [index] if index is not None else []

        def _init_emb(emb):
            if not emb:
                return [], 0
            return emb[:], 1

        def _add_emb(emb_sum, emb_count, emb):
            if not emb:
                return emb_sum, emb_count
            if not emb_sum:
                return emb[:], 1
            if len(emb) != len(emb_sum):
                return emb_sum, emb_count
            for i, v in enumerate(emb):
                emb_sum[i] += v
            return emb_sum, emb_count + 1

        for c in sorted_chunks:
            key = (c.get("source", ""), c.get("page", 0))
            chunk_idx = c.get("chunk_index", 0)
            chunk_type = c.get("type", "doc")
            chunk_section = c.get("section", "")
            c_emb = c.get("embedding", [])

            if buffer is None:
                emb_sum, emb_count = _init_emb(c_emb)
                buffer = {
                    "key": key,
                    "source": c.get("source", ""),
                    "page": c.get("page", 0),
                    "content": c.get("content", ""),
                    "type": chunk_type,
                    "section": chunk_section,
                    # 同一 source 的 origin 一致，直接沿用（丟掉會讓 VL 出身標記在 REF 消失）
                    "origin": c.get("origin", ""),
                    "last_idx": chunk_idx,
                    "embedding": c_emb,
                    # 成員的 KB 列索引：決策要靠它回去 gate 矩陣聚合，
                    # 不能用合併後的平均向量重算。
                    "member_chunk_idx": _member_indices(c),
                    "_emb_sum": emb_sum,
                    "_emb_count": emb_count,
                }
            elif (buffer["key"] == key and
                  chunk_idx == buffer["last_idx"] + 1 and
                  buffer["section"] == chunk_section and  # 不跨 section 合併
                  len(buffer["content"]) + len(c.get("content", "")) < KNOWLEDGE_MERGE_MAX_CHARS):
                buffer["content"] += "\n" + c.get("content", "")
                buffer["last_idx"] = chunk_idx
                buffer["member_chunk_idx"] = buffer.get("member_chunk_idx", []) + _member_indices(c)
                # 升級 type（warning > spec > doc）
                buffer["type"] = self._upgrade_type(buffer["type"], chunk_type)
                emb_sum, emb_count = _add_emb(buffer.get("_emb_sum", []),
                                              buffer.get("_emb_count", 0), c_emb)
                buffer["_emb_sum"] = emb_sum
                buffer["_emb_count"] = emb_count
            else:
                if buffer.get("_emb_count", 0) > 1:
                    buffer["embedding"] = self._average_embeddings(
                        buffer.get("_emb_sum", []), buffer.get("_emb_count", 0)
                    )
                buffer.pop("_emb_sum", None)
                buffer.pop("_emb_count", None)
                merged.append(buffer)
                emb_sum, emb_count = _init_emb(c_emb)
                buffer = {
                    "key": key,
                    "source": c.get("source", ""),
                    "page": c.get("page", 0),
                    "content": c.get("content", ""),
                    "type": chunk_type,
                    "section": chunk_section,
                    "origin": c.get("origin", ""),
                    "last_idx": chunk_idx,
                    "embedding": c.get("embedding", []),
                    "member_chunk_idx": _member_indices(c),
                    "_emb_sum": emb_sum,
                    "_emb_count": emb_count,
                }

        if buffer:
            if buffer.get("_emb_count", 0) > 1:
                buffer["embedding"] = self._average_embeddings(
                    buffer.get("_emb_sum", []), buffer.get("_emb_count", 0)
                )
            buffer.pop("_emb_sum", None)
            buffer.pop("_emb_count", None)
            merged.append(buffer)

        return merged

    def _estimate_tokens(self, text: str) -> int:
        """簡單估算 token 數（中文約 1.5 字/token，英文約 4 字元/token）"""
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        other_chars = len(text) - chinese_chars
        return int(chinese_chars / 1.5 + other_chars / 4)

    def query(
        self,
        question: str,
        top_k: int = KNOWLEDGE_TOP_K,
        is_strict_mode: bool = False,
        metadata_filter: dict | None = None,
        source: str | None = None,
    ) -> tuple[str, str, dict]:
        """
        查詢相關知識 - 結構化輸出版本（P0 改進：Margin-based 動態門檻）

        回傳: (model_output, display_output, metadata)
        metadata 包含: has_ref, top_score, ref_count, is_high_risk
        """
        empty_metadata = {"has_ref": False, "top_score": 0.0, "ref_count": 0, "is_high_risk": False}

        if not self.loaded or not self.chunks:
            return "", "", empty_metadata

        if source:
            metadata_filter = dict(metadata_filter or {})
            metadata_filter["source"] = source
        candidates = self._hybrid_search(
            question, KNOWLEDGE_CANDIDATE_K, metadata_filter=metadata_filter
        )
        if not candidates:
            return "", "", empty_metadata

        # P0 改進：應用來源權重（spec/manual/api 優先）
        candidates = self._apply_source_weighting(candidates)

        # 動態門檻：短問題用較低門檻，嚴格模式用較高門檻
        query_tokens = self._estimate_tokens(question)
        if is_strict_mode:
            base_threshold = STRICT_MODE_THRESHOLD
        elif query_tokens < KNOWLEDGE_SHORT_QUERY_TOKENS:
            base_threshold = KNOWLEDGE_THRESHOLD_SHORT
        else:
            base_threshold = KNOWLEDGE_THRESHOLD

        # 門檻一律吃 gate（content-only）分數。排序可以被生成脈絡影響，
        # 「夠不夠格當證據」不行——那正是規格 §2 說的分數面循環 grounding：
        # 錯誤脈絡把弱原文推過 strict 門檻。
        decision_ranked = self._decision_order(candidates)
        top_gate_score = decision_ranked[0].gate_score
        min_gate_score = max(base_threshold, top_gate_score * DYNAMIC_THRESHOLD_RATIO)

        # P0 改進：Margin-based 風險判斷（決策 → gate）
        is_high_risk = False
        if MARGIN_ENABLED and len(decision_ranked) >= 2:
            gap = decision_ranked[0].gate_score - decision_ranked[1].gate_score
            if gap < MARGIN_MIN_GAP:
                is_high_risk = True  # top1-top2 差距太小，不確定
            if top_gate_score < MARGIN_LOW_SCORE:
                is_high_risk = True  # top1 分數太低

        # Dense 是一般語意 gate；數字/hex 題另保留有精確 lexical evidence
        # 的候選交給 reranker。否則 dense 對數字偏弱時會把 BM25 的正解先丟掉。
        # lexical 判定吃 gate BM25：生成文字不得冒充數值證據。
        filtered = [
            candidate for candidate in candidates
            if candidate.gate_score >= min_gate_score
            or self._has_lexical_numeric_evidence(
                question, candidate.gate_bm25, candidate.chunk
            )
        ]
        if not filtered:
            return "", "", empty_metadata

        # 動態 top_k：高相關度時少給，低相關度時多給（同樣看 gate）
        top_score = candidates[0].rrf_score  # RRF score，僅供 metadata 記錄
        if top_gate_score >= DYNAMIC_TOP_K_HIGH_SCORE:
            effective_top_k = DYNAMIC_TOP_K_MIN
        else:
            effective_top_k = min(top_k, DYNAMIC_TOP_K_MAX)

        # RERANKER_TOP_N 是最終上限；reranker output 先保留較寬的 MMR pool。
        effective_top_k = min(effective_top_k, RERANKER_TOP_N)
        rerank_output_k = effective_top_k
        if USE_MMR:
            rerank_output_k = min(
                len(filtered), max(effective_top_k * 3, RERANKER_TOP_N * 2)
            )
        reranked = self._rerank_with_model(
            question,
            filtered,
            rerank_output_k,
            is_strict_mode=is_strict_mode,
        )
        reranked_chunks = [chunk for _score, chunk in reranked]
        if not reranked_chunks:
            return "", "", empty_metadata

        if USE_MMR:
            q_emb = self._get_embedding(question)
            # cross-encoder 的分數一路帶到這裡當相關度；MMR 只負責多樣性懲罰。
            top_chunks = self._mmr_select(
                reranked_chunks, q_emb, effective_top_k,
                relevance=[score for score, _chunk in reranked],
            )
        else:
            top_chunks = reranked_chunks[:effective_top_k]

        if not top_chunks:
            return "", "", empty_metadata

        # 污染風險控制有 min_score 門檻 → 決策 → 讀 gate 矩陣。
        # 以 chunk_idx 讀矩陣列，不把 gate 向量掛回 chunk（那是 n×dim 的 float list）。
        q_emb_prelim = q_emb if USE_MMR else self._get_embedding(question)
        prelim_emb_scores = [
            self._chunk_gate_score(c, q_emb_prelim) for c in top_chunks
        ]

        # P0 改進：預估污染風險
        prelim_unique_sources = set(c.get("source", "") for c in top_chunks)
        prelim_variance = 0.0
        if len(prelim_emb_scores) >= 2:
            prelim_mean = sum(prelim_emb_scores) / len(prelim_emb_scores)
            prelim_variance = sum((s - prelim_mean) ** 2 for s in prelim_emb_scores) / len(prelim_emb_scores)

        prelim_pollution_risk = "low"
        if len(prelim_unique_sources) > 3 and prelim_variance < 0.02:
            prelim_pollution_risk = "high"
        elif len(prelim_unique_sources) > 2 and is_high_risk:
            prelim_pollution_risk = "medium"

        # P0 改進：根據污染風險控制 REF 數量，寧缺勿濫
        if prelim_pollution_risk in ('medium', 'high'):
            top_chunks = self._select_with_pollution_control(
                top_chunks, prelim_pollution_risk, prelim_emb_scores
            )

        merged_chunks = self._merge_adjacent_chunks(top_chunks)

        # P0 改進：去重和噪音過濾（尤其 web/OCR 來源）
        merged_chunks = self._filter_noisy_chunks(merged_chunks)
        merged_chunks = self._deduplicate_chunks(merged_chunks)

        has_spec = any(chunk.get('type') == 'spec' for chunk in merged_chunks)
        has_warning = any(chunk.get('type') == 'warning' for chunk in merged_chunks)
        # VL 產物（圖片/截圖經視覺模型抽述）要在 REF 層揭露出身：
        # 它的內容是機率性描述，不能與原文抽取的 chunk 當同級證據
        vl_origins = {'image', 'screenshot'}
        has_vl_ref = any(chunk.get('origin') in vl_origins for chunk in merged_chunks)

        # 修正：用「最終被選中的 chunks」重新計算信心分數
        # 避免 candidates[0] 被過濾/rerank 後，仍用它的低分來決定信心度
        # 這會導致「有好 REF 卻被誤判為低信心而跳過」
        # 信心是決策（拒答閘讀的就是這個數字）→ gate 矩陣；合併過的 chunk 取
        # 成員的最大 gate 分數，絕不回退到 contextual 向量重算。
        q_emb_for_score = self._get_embedding(question) if not USE_MMR else q_emb
        used_emb_scores = [
            self._chunk_gate_score(c, q_emb_for_score) for c in merged_chunks
        ]
        used_emb_scores = [s for s in used_emb_scores if s]
        top_emb_score_used = max(used_emb_scores) if used_emb_scores else top_gate_score
        # 觀測用的檢索分數必須算在**同一組 chunk** 上，否則兩個數字不可比
        # （一個是最終選中的、一個是過濾前的候選首位）。
        used_retrieval_scores = [
            self._chunk_retrieval_score(c, q_emb_for_score) for c in merged_chunks
        ]
        used_retrieval_scores = [s for s in used_retrieval_scores if s]
        top_retrieval_score = (
            max(used_retrieval_scores) if used_retrieval_scores else top_emb_score_used
        )

        # 在 REF header 加入信心分數提示，讓 LLM 了解參考資料的可靠度
        # 使用修正後的 top_emb_score_used
        confidence_label = ""
        if top_emb_score_used >= 0.6:
            confidence_label = "高信心"
        elif top_emb_score_used >= 0.4:
            confidence_label = "中信心"
        else:
            confidence_label = "低信心"

        model_lines = [f"[REF] 相關知識參考（信心度: {confidence_label}, score={top_emb_score_used:.2f}）:"]
        model_lines.append(f"※ 信心度說明：高信心(≥0.6)資料可直接引用，中信心(0.4-0.6)請謹慎使用，低信心(<0.4)僅供參考")

        for i, chunk in enumerate(merged_chunks, 1):
            source = chunk.get('source', '未知')
            page = chunk.get('page', '?')
            doc_type = chunk.get('type', 'doc')
            section = chunk.get('section', '')
            origin = chunk.get('origin', '')

            if KNOWLEDGE_INCLUDE_CONTENT:
                content = chunk.get('content', '')
                original_len = len(content)
                max_chars = KNOWLEDGE_MERGE_MAX_CHARS if KNOWLEDGE_MERGE_ADJACENT else KNOWLEDGE_CONTENT_MAX_CHARS
                if original_len > max_chars:
                    content = content[:max_chars] + f"... [REF{i} 內容已截斷，原長度 {original_len} 字元]"

                model_lines.append(f"\n[REF{i}]")
                model_lines.append(f"  type: {doc_type}")
                if origin:
                    origin_label = (f"VL（{origin} 經視覺模型辨識，非原文）"
                                    if origin in vl_origins else origin)
                    model_lines.append(f"  origin: {origin_label}")
                model_lines.append(f"  source: {source}")
                model_lines.append(f"  page: {page}")
                if section:
                    model_lines.append(f"  section: {section}")
                model_lines.append(f"  content: {content}")
            else:
                section_hint = f" ({section})" if section else ""
                vl_hint = "（VL 辨識）" if origin in vl_origins else ""
                model_lines.append(f"  - REF{i}: {source} 第 {page} 頁 [{doc_type}]{vl_hint}{section_hint}")

        model_lines.append("\n[/REF]")

        # 移除詳細回答規則，避免與 config.get_answer_rules() 重複/打架
        # 只保留輕量提示，主要規則由呼叫端統一注入
        model_lines.append("\n※ 引用 REF 內容時請標註編號（如 REF1）")
        if has_spec:
            model_lines.append("※ spec 類型的 REF 優先級較高")
        if has_warning:
            model_lines.append("※ warning 類型的 REF 請特別注意其限制條件")
        if has_vl_ref:
            model_lines.append(
                "※ origin 標註 VL 的 REF 是視覺模型對圖片/截圖的辨識結果"
                "（機率性描述，非原始文件）：引用其數值/規格時請註明「(VL 辨識)」，"
                "與文字抽取的 REF 衝突時以文字抽取為準"
            )

        model_output = "\n".join(model_lines)

        doc_pages = {}
        doc_types = {}
        vl_sources = set()
        for chunk in merged_chunks:
            src = chunk.get('source', '?')
            chunk_type = chunk.get('type', 'doc')
            if src not in doc_pages:
                doc_pages[src] = []
                doc_types[src] = chunk_type
            else:
                # 改進：同 source 只要出現 warning/spec 就升級 type
                doc_types[src] = self._upgrade_type(doc_types[src], chunk_type)
            if chunk.get('origin') in vl_origins:
                vl_sources.add(src)
            page = chunk.get('page')
            if page and page not in doc_pages[src]:
                doc_pages[src].append(page)

        display_parts = []
        for src, pages in doc_pages.items():
            pages_str = ", ".join(str(p) for p in sorted(pages)[:5])
            if len(pages) > 5:
                pages_str += "..."
            dtype = doc_types.get(src, 'doc')
            vl_tag = "·VL" if src in vl_sources else ""
            display_parts.append(f"{src} [{dtype}{vl_tag}] p.{pages_str}")

        # display 也顯示信心度，讓用戶知道參考資料的可靠度
        # P0 改進：高風險時加上警告
        risk_warning = " ⚠️" if is_high_risk else ""
        display_output = f"[REF {confidence_label}{risk_warning}] " + " | ".join(display_parts)

        # 回傳 metadata 供上層判斷 REF 強度
        # 改進：分別回傳 embedding score 和 keyword score，讓 spec 題拒答只看 embedding
        # 修正：top_emb_score 改用「最終被選中的 chunks」的最高分，而非 candidates[0]
        # 這避免了「candidates[0] 被過濾掉，但 top_emb_score 仍用它的低分」的問題
        top_kw_score = decision_ranked[0].gate_bm25 if decision_ranked else 0.0
        # 將 has_spec_chunk 改為 has_authoritative_chunk
        # 權威類型：spec、manual、api（chat/diagram 不算權威）
        authoritative_types = {'spec', 'manual', 'api'}
        has_authoritative_chunk = any(
            chunk.get('type') in authoritative_types for chunk in merged_chunks
        )
        # 保留舊名以向後相容
        has_spec_chunk = has_authoritative_chunk

        # 新增：回傳 refs 清單供 data_flywheel / eval 使用
        # 這讓匯出的資料能記錄「用了哪些 REF」，方便訓練和回歸比較
        refs = [
            {
                "source": c.get("source", ""),
                "page": c.get("page", 0),
                "type": c.get("type", "doc"),
                "section": c.get("section", ""),
                # 出身揭露：VL 產物（image/screenshot）在下游要能與原文區分
                "origin": c.get("origin", ""),
            }
            for c in merged_chunks
        ]

        # P1 改進：計算污染指標
        unique_sources = set(c.get("source", "") for c in merged_chunks)
        score_variance = 0.0
        if len(used_emb_scores) >= 2:
            mean_score = sum(used_emb_scores) / len(used_emb_scores)
            score_variance = sum((s - mean_score) ** 2 for s in used_emb_scores) / len(used_emb_scores)

        # 污染風險判斷
        # - 來源太多（>3）且分數差距小 → 可能混入不相關內容
        # - 分數變異太小（<0.01）→ 難以區分，可能都不太相關
        context_pollution_risk = "low"
        if len(unique_sources) > 3 and score_variance < 0.02:
            context_pollution_risk = "high"
        elif len(unique_sources) > 2 and is_high_risk:
            context_pollution_risk = "medium"

        # P0-Eval: 提取 retrieved_chunks 內容供 Layer 1 Retrieval Recall 計算
        retrieved_chunks = [c.get("content", "") for c in merged_chunks]

        metadata = {
            "has_ref": len(merged_chunks) > 0,
            "top_score": top_score,               # RRF score（向後相容）
            # 決策訊號：content-only（gate）的最高分。拒答閘、信心標記讀的都是
            # 這個數字，所以它**必須**不含 LLM 生成的脈絡。
            "top_emb_score": top_emb_score_used,
            "top_kw_score": top_kw_score,         # gate BM25 score
            # 觀測用：含生成脈絡的檢索分數。只給 telemetry / A-B 看，不做決策。
            "top_retrieval_score": top_retrieval_score,
            "context_in_use": use_generated_context() and self._has_ctx,
            "has_spec_chunk": has_spec_chunk,     # 向後相容（等同 has_authoritative_chunk）
            "has_authoritative_chunk": has_authoritative_chunk,  # 是否命中權威類型（spec/manual/api）
            "ref_count": len(merged_chunks),
            "refs": refs,                         # 實際引用的 REF 清單
            # P0-Eval: 供 eval 用的 retrieved_chunks 內容
            "retrieved_chunks": retrieved_chunks, # chunk 內容列表，用於 Layer 1 Recall 評估
            # P0 改進：Margin-based 風險判斷
            "is_high_risk": is_high_risk,         # True = top1-top2 差距太小或分數太低
            "confidence_label": confidence_label, # 高信心/中信心/低信心
            # P1 改進：Context 污染指標
            "unique_sources": len(unique_sources),     # 引用了幾個不同來源
            "score_variance": score_variance,          # 分數變異（越大越好）
            "context_pollution_risk": context_pollution_risk  # low/medium/high
        }

        return model_output, display_output, metadata

    def get_status(self) -> str:
        if not self.loaded:
            if self.load_error:
                return f"[KB] 知識庫: 載入失敗({self.load_error})"
            return "[KB] 知識庫: (空)"

        chunk_count = len(self.chunks)
        doc_count = len(self.documents)
        features = []

        # P0 改進：顯示 BM25 + RRF 狀態
        if BM25_ENABLED and self._bm25_index:
            features.append("BM25")
        elif USE_HYBRID_SEARCH:
            features.append("Hybrid")

        if RRF_ENABLED:
            features.append("RRF")

        if USE_RERANKER:
            reranker_type = "Model" if self._check_reranker_available() else "LLM"
            always_on = "+" if RERANKER_ALWAYS_ON else ""
            features.append(f"Rerank{always_on}({reranker_type})")

        if USE_QUERY_EXPANSION:
            features.append("QExp")

        if USE_MMR:
            features.append("MMR")

        if self._has_ctx:
            features.append("Ctx(on)" if use_generated_context() else "Ctx(off)")

        feature_str = f" [{'+'.join(features)}]" if features else ""
        line = f"[KB] 知識庫: {self.path} ({doc_count} 文件, {chunk_count} 區塊){feature_str}"
        ctx_line = self.context_status()
        return f"{line}\n{ctx_line}" if ctx_line else line

    def context_status(self) -> str:
        """chunk 脈絡的世代分布。

        沒有單一 KB-level fingerprint 能代表混存狀態（同一個 KB 可能混著不同
        prompt 版本、不同模型生成的 ctx），所以聚合每個 chunk 的 ctx_meta 就是
        真相。只出計數，**不出 ctx 文本**——ctx 內容視同 KB 內容（NDA）。
        """
        if not self._has_ctx:
            return ""
        generations: dict[str, int] = {}
        absent = 0
        absent_reasons: dict[str, int] = {}
        for chunk in self.chunks:
            meta = chunk.get("ctx_meta") or {}
            if context_signals.chunk_ctx(chunk):
                key = f"prompt-v{meta.get('prompt_version', '?')}"
                generations[key] = generations.get(key, 0) + 1
            else:
                absent += 1
                reason = str(meta.get("absent_reason") or "unknown")
                absent_reasons[reason] = absent_reasons.get(reason, 0) + 1
        total = len(self.chunks) or 1
        covered = total - absent
        parts = ", ".join(f"{k}: {v}" for k, v in sorted(generations.items()))
        line = f"[KB] ctx coverage {covered * 100 // total}% ({parts or 'none'}, absent: {absent})"
        if absent_reasons:
            detail = ", ".join(f"{k}×{v}" for k, v in sorted(absent_reasons.items()))
            line += f" [{detail}]"
        return line


def load_knowledge_base_strict(json_path: str) -> KnowledgeBase:
    """建立新 KnowledgeBase;任何載入失敗都拋 KnowledgeStoreError,不回傳半殘物件。

    給「自動重載 / 手動 reload」的呼叫端用:呼叫端把回傳值當 candidate,
    例外時保留手上還能用的舊 KB(不要先覆蓋再發現壞掉)。
    「檔案不存在」是合法空庫,正常回傳(loaded=False、load_error=None)。
    """
    kb = KnowledgeBase(json_path)   # KnowledgeStoreError 由 _load 直接傳播
    if kb.load_error is not None:
        raise KnowledgeStoreError(
            f"knowledge.json 載入失敗: {kb.load_error}(path={json_path})"
        )
    return kb
