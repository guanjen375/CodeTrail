#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 知識庫 (RAG)
"""

import re
import json
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

import llama_client
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
    USE_MMR, MMR_LAMBDA, KEYWORD_WEIGHT,
    # P0 改進：Source Type Weighting（來源權重）
    SOURCE_TYPE_WEIGHTS, POLLUTION_RISK_TOP_K, POLLUTION_RISK_MIN_SCORE,
    # P0 改進：BM25 + RRF + Reranker 條件式觸發
    BM25_K1, BM25_B, BM25_ENABLED,
    RRF_K, RRF_ENABLED,
    RERANKER_ALWAYS_ON, RERANKER_TOP_N, RERANKER_SKIP_THRESHOLD,
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
        self.path = json_path
        self._reranker_available = None
        # Numpy 加速用的預計算陣列
        self._embeddings = None  # shape: (n_chunks, dim)
        self._embeddings_normalized = False
        # BM25 索引（預計算）
        self._bm25_index = None  # {term: {chunk_idx: tf}}
        self._bm25_doc_lens = None  # [doc_len, ...]
        self._bm25_avg_doc_len = 0.0
        self._bm25_idf = None  # {term: idf}
        # .npz embeddings 路徑（與 json 同目錄）
        json_dir = Path(json_path).parent
        self._emb_path = json_dir / KNOWLEDGE_EMB_FILE

        if Path(json_path).exists():
            self._load(json_path)

    def _load(self, path: str):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self.chunks = data.get("chunks", [])
            metadata = data.get("metadata", {})
            self.documents = metadata.get("documents", [])

            # 驗證 embedding model 一致性
            saved_model = metadata.get("embedding_model", "")
            if saved_model and saved_model != EMBEDDING_MODEL:
                print(f"[WARN] 知識庫 embedding model 不一致！")
                print(f"       知識庫使用: {saved_model}")
                print(f"       目前設定: {EMBEDDING_MODEL}")
                print(f"       請執行 RAG.py 重建知識庫，否則搜尋結果可能不準確")
                # 仍然載入，但發出警告
                self._embedding_mismatch = True
            else:
                self._embedding_mismatch = False

            # 驗證 embedding 維度一致性（抽樣檢查前幾個 chunk）
            if self.chunks:
                sample_dims = set()
                for chunk in self.chunks[:5]:
                    emb = chunk.get("embedding", [])
                    if emb:
                        sample_dims.add(len(emb))

                if len(sample_dims) > 1:
                    print(f"[WARN] 知識庫 embedding 維度不一致: {sample_dims}")
                    print(f"       請執行 RAG.py 重建知識庫")
                    self._embedding_dim_mismatch = True
                else:
                    self._embedding_dim_mismatch = False
                    self._embedding_dim = sample_dims.pop() if sample_dims else None
            else:
                self._embedding_dim_mismatch = False
                self._embedding_dim = None

            self.loaded = True

            # 優先從 .npz 載入 embeddings（加速載入）
            npz_loaded = self._load_embeddings_from_npz()

            if not npz_loaded:
                # Fallback：從 JSON 重建 numpy embeddings
                self._precompute_embeddings()
                # 儲存為 .npz 供下次使用
                self._save_embeddings_to_npz()

            # P0 改進：預計算 BM25 索引
            if BM25_ENABLED:
                self._precompute_bm25_index()

            # 速度優化：記錄載入的 metadata 供快取驗證
            self._cache_metadata = {
                "embedding_model": EMBEDDING_MODEL,
                "chunk_count": len(self.chunks),
                "bm25_enabled": BM25_ENABLED,
            }

        except Exception as e:
            print(f"[WARN] 知識庫載入失敗: {e}")
            self.loaded = False

    def _compute_content_hash(self) -> str:
        """計算所有 chunk 內容的雜湊（用於 .npz 快取驗證）

        改進：使用內容雜湊確保 .npz 與 JSON 內容一致，
        避免 chunk 數量相同但內容變更時讀到舊 embedding
        """
        import hashlib
        hasher = hashlib.md5()
        for chunk in self.chunks:
            content = chunk.get('content', '')
            hasher.update(content.encode('utf-8'))
        return hasher.hexdigest()

    def _load_embeddings_from_npz(self) -> bool:
        """從 .npz 檔案載入 embeddings（加速載入）

        改進：加入內容雜湊驗證，確保 .npz 與 JSON 內容一致

        Returns:
            True 如果成功載入，False 如果需要從 JSON 重建
        """
        if not HAS_NUMPY or not self._emb_path.exists():
            return False

        try:
            data = np.load(self._emb_path, allow_pickle=True)
            embeddings = data['embeddings']
            emb_model = str(data.get('embedding_model', ''))
            chunk_count = int(data.get('chunk_count', 0))
            content_hash = str(data.get('content_hash', ''))

            # 驗證 embedding model 一致
            if emb_model and emb_model != EMBEDDING_MODEL:
                print(f"[WARN] .npz embedding model 不一致，將重建")
                return False

            # 驗證 chunk 數量一致
            if chunk_count != len(self.chunks):
                print(f"[WARN] .npz chunk 數量不一致，將重建")
                return False

            # 驗證內容雜湊一致（避免內容變更但數量相同的情況）
            current_hash = self._compute_content_hash()
            if content_hash and content_hash != current_hash:
                print(f"[WARN] .npz 內容雜湊不一致，將重建")
                return False

            self._embeddings = embeddings
            self._embeddings_normalized = True  # .npz 已預先正規化
            self._embedding_indices = list(range(len(self.chunks)))

            # P0：把 .npz 的向量掛回每個 chunk。
            # RAG 存 knowledge.json 時為了體積「不再 inline embedding」（只留 .npz，
            # 見 RAG.py:_restore_embeddings_from_npz）。若這裡只設 self._embeddings 而
            # 不回填 chunk["embedding"]，下游的 MMR / 污染控制 / 最終信心分數（全都讀
            # chunk.get("embedding")）會一律拿到空向量、把相似度算成 0，最後只回一個
            # 空的 [REF]…[/REF] 殼。chunk_count 已在上面驗證 == len(self.chunks)。
            # 向量已 L2 正規化，且下游 _cosine_similarity / _mmr_select_numpy 都會再
            # 自行正規化；用 .tolist() 是因為那些消費者對 numpy array 做 `if not a` /
            # `if emb:` 會丟 ambiguous-truth 例外，必須是 Python list。
            for i, chunk in enumerate(self.chunks):
                chunk["embedding"] = embeddings[i].tolist()

            return True
        except Exception as e:
            print(f"[WARN] 載入 .npz 失敗: {e}")
            return False

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
        """預計算並正規化 embeddings 到 numpy array"""
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

        # 確保維度一致
        dim = len(embeddings_list[0])
        filtered = [(i, emb) for i, emb in zip(valid_indices, embeddings_list) if len(emb) == dim]

        if not filtered:
            self._embeddings = None
            return

        valid_indices = [x[0] for x in filtered]
        embeddings_list = [x[1] for x in filtered]

        self._embeddings = np.array(embeddings_list, dtype=np.float32)
        self._embedding_indices = valid_indices  # 映射回 self.chunks 的索引

        # L2 正規化（預計算，加速後續 cosine similarity）
        norms = np.linalg.norm(self._embeddings, axis=1, keepdims=True)
        norms = np.where(norms > 0, norms, 1.0)  # 避免除零
        self._embeddings = self._embeddings / norms
        self._embeddings_normalized = True

    def _precompute_bm25_index(self):
        """預計算 BM25 索引（inverted index + IDF）

        BM25 公式：
        score = sum( IDF(t) * (tf * (k1+1)) / (tf + k1 * (1 - b + b * dl/avgdl)) )

        其中：
        - tf: 詞在文件中出現的次數
        - dl: 文件長度（token 數）
        - avgdl: 平均文件長度
        - IDF(t) = log((N - n(t) + 0.5) / (n(t) + 0.5) + 1)
        - N: 文件總數
        - n(t): 包含詞 t 的文件數
        """
        if not self.chunks:
            return

        import math
        from collections import defaultdict

        # 建立 inverted index: {term: {chunk_idx: tf}}
        inverted_index = defaultdict(lambda: defaultdict(int))
        doc_lens = []
        doc_freqs = defaultdict(int)  # 每個 term 出現在多少文件中

        for idx, chunk in enumerate(self.chunks):
            content = chunk.get("content", "")
            # 加入 title、section、source 提升 lexical 命中率
            title = chunk.get("section", "")
            source = chunk.get("source", "")
            full_text = f"{title} {source} {content}"

            # Tokenize（同時支援中英文）
            tokens = self._tokenize_for_bm25(full_text)
            doc_lens.append(len(tokens))

            # 統計 term frequency
            term_set = set()
            for token in tokens:
                inverted_index[token][idx] += 1
                term_set.add(token)

            # 統計 document frequency
            for term in term_set:
                doc_freqs[term] += 1

        # 計算 IDF
        N = len(self.chunks)
        idf = {}
        for term, df in doc_freqs.items():
            # BM25 IDF 公式（加上 +1 避免負值）
            idf[term] = math.log((N - df + 0.5) / (df + 0.5) + 1)

        self._bm25_index = dict(inverted_index)
        self._bm25_doc_lens = doc_lens
        self._bm25_avg_doc_len = sum(doc_lens) / len(doc_lens) if doc_lens else 1.0
        self._bm25_idf = idf

    def _tokenize_for_bm25(self, text: str) -> list:
        """BM25 專用的 tokenizer

        改進：
        - 支援中英文混合
        - 保留程式碼 token（函式名、變數名）
        - 移除 stopwords
        """
        # 先轉小寫
        text = text.lower()

        # 抽取所有 word-like tokens（英文、中文、數字底線）
        # 英文 token
        en_tokens = re.findall(r'\b[a-z_][a-z0-9_]*\b', text)
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

        return [t for t in all_tokens if len(t) > 1 and t not in stopwords]

    def _bm25_score(self, query_tokens: list) -> list:
        """計算所有 chunks 的 BM25 分數

        返回: [(score, chunk_idx), ...] 按分數降序排列
        """
        if not self._bm25_index or not query_tokens:
            return []

        scores = [0.0] * len(self.chunks)
        k1 = BM25_K1
        b = BM25_B
        avgdl = self._bm25_avg_doc_len

        for token in query_tokens:
            if token not in self._bm25_index:
                continue

            idf = self._bm25_idf.get(token, 0.0)
            term_docs = self._bm25_index[token]

            for chunk_idx, tf in term_docs.items():
                dl = self._bm25_doc_lens[chunk_idx]
                # BM25 公式
                numerator = tf * (k1 + 1)
                denominator = tf + k1 * (1 - b + b * dl / avgdl)
                scores[chunk_idx] += idf * numerator / denominator

        # 正規化到 0-1（用 max 正規化）
        max_score = max(scores) if scores else 1.0
        if max_score > 0:
            scores = [s / max_score for s in scores]

        # 返回 (score, chunk_idx) 列表，按分數降序
        scored = [(scores[i], i) for i in range(len(scores)) if scores[i] > 0]
        scored.sort(reverse=True, key=lambda x: x[0])
        return scored

    def _rrf_fusion(self, embedding_ranks: list, bm25_ranks: list, k: int = RRF_K) -> list:
        """RRF (Reciprocal Rank Fusion) 融合兩個排名列表

        RRF 公式：RRF(d) = sum( 1 / (k + rank(d)) )

        Args:
            embedding_ranks: [(emb_score, chunk_idx), ...] 按分數降序
            bm25_ranks: [(bm25_score, chunk_idx), ...] 按分數降序
            k: RRF 常數（預設 60）

        Returns:
            [(rrf_score, emb_score, bm25_score, chunk), ...] 按 RRF 分數降序
        """
        # 建立 chunk_idx -> rank 的映射
        emb_rank_map = {chunk_idx: rank for rank, (_, chunk_idx) in enumerate(embedding_ranks)}
        bm25_rank_map = {chunk_idx: rank for rank, (_, chunk_idx) in enumerate(bm25_ranks)}

        # 建立 chunk_idx -> score 的映射
        emb_score_map = {chunk_idx: score for score, chunk_idx in embedding_ranks}
        bm25_score_map = {chunk_idx: score for score, chunk_idx in bm25_ranks}

        # 取所有候選的 union
        all_chunks = set(emb_rank_map.keys()) | set(bm25_rank_map.keys())

        # 計算 RRF 分數
        rrf_scores = []
        for chunk_idx in all_chunks:
            rrf = 0.0
            # Embedding rank
            if chunk_idx in emb_rank_map:
                rrf += 1.0 / (k + emb_rank_map[chunk_idx])
            # BM25 rank
            if chunk_idx in bm25_rank_map:
                rrf += 1.0 / (k + bm25_rank_map[chunk_idx])

            emb_score = emb_score_map.get(chunk_idx, 0.0)
            bm25_score = bm25_score_map.get(chunk_idx, 0.0)
            chunk = self.chunks[chunk_idx]
            rrf_scores.append((rrf, emb_score, bm25_score, chunk))

        # 按 RRF 分數降序排列
        rrf_scores.sort(reverse=True, key=lambda x: x[0])
        return rrf_scores

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

    def _get_source_weight(self, chunk: dict) -> float:
        """取得 chunk 的來源權重

        權威來源（spec/manual/api）權重較高
        低可靠來源（chat/diagram/web）權重較低
        """
        chunk_type = chunk.get('type', 'default')
        return SOURCE_TYPE_WEIGHTS.get(chunk_type, SOURCE_TYPE_WEIGHTS['default'])

    def _apply_source_weighting(self, candidates: list) -> list:
        """對候選結果應用來源權重

        Args:
            candidates: [(rrf_score, emb_score, bm25_score, chunk), ...]

        Returns:
            [(weighted_score, emb_score, bm25_score, chunk), ...] 按加權分數排序
        """
        weighted = []
        for rrf_score, emb_score, bm25_score, chunk in candidates:
            weight = self._get_source_weight(chunk)
            # 加權分數 = 原始分數 * 來源權重
            weighted_emb = emb_score * weight
            weighted_rrf = rrf_score * weight
            weighted.append((weighted_rrf, weighted_emb, bm25_score, chunk))

        # 按加權 RRF 分數重新排序
        weighted.sort(reverse=True, key=lambda x: x[0])
        return weighted

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
            emb_scores: 對應的 embedding scores

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

        # P0-4: 改用 embedding score，格式是 (rrf, emb, bm25, chunk)
        top_emb_score = candidates[0][1] if candidates else 0

        # 高信心時跳過 expansion
        return top_emb_score < threshold

    def _hybrid_search(self, question: str, candidate_k: int = KNOWLEDGE_CANDIDATE_K) -> list:
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

        # 取得 query embedding
        q_emb = self._get_embedding(question)

        # ===== Embedding 召回 =====
        if HAS_NUMPY and self._embeddings is not None and self._embeddings_normalized:
            embedding_ranks = self._embedding_search_numpy(q_emb, candidate_k * 2)
        else:
            embedding_ranks = self._embedding_search_fallback(q_emb, candidate_k * 2)

        # ===== BM25 召回（P0 改進）=====
        if BM25_ENABLED and self._bm25_index:
            query_tokens = self._tokenize_for_bm25(question)
            bm25_ranks = self._bm25_score(query_tokens)[:candidate_k * 2]
        else:
            # Fallback: 使用舊的 keyword matching
            query_keywords = self._extract_keywords(question) if USE_HYBRID_SEARCH else set()
            bm25_ranks = self._keyword_search_fallback(query_keywords, candidate_k * 2)

        # ===== RRF 融合（P0 改進）=====
        if RRF_ENABLED and embedding_ranks and bm25_ranks:
            scores = self._rrf_fusion(embedding_ranks, bm25_ranks)
        else:
            # Fallback: 只用 embedding
            scores = [(emb_score, emb_score, 0.0, self.chunks[idx])
                      for emb_score, idx in embedding_ranks]

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
                    self._update_scores_with_expansion(scores, mq_emb)

                # 重新排序
                scores.sort(reverse=True, key=lambda x: x[0])

        return scores[:candidate_k]

    def _embedding_search_numpy(self, q_emb: list, top_k: int) -> list:
        """使用 numpy 向量化的 embedding 搜尋

        返回: [(emb_score, chunk_idx), ...] 按分數降序
        """
        if top_k <= 0:
            return []
        # 正規化 query embedding
        q_vec = np.array(q_emb, dtype=np.float32)
        q_norm = np.linalg.norm(q_vec)
        if q_norm > 0:
            q_vec = q_vec / q_norm

        # 批次計算所有 cosine similarity
        emb_scores = np.dot(self._embeddings, q_vec)
        total = emb_scores.shape[0]
        if total == 0:
            return []
        k = min(top_k, total)
        if total <= k:
            idxs = np.arange(total)
        else:
            idxs = np.argpartition(-emb_scores, k - 1)[:k]
        idxs = idxs[np.argsort(-emb_scores[idxs])]
        return [(float(emb_scores[i]), self._embedding_indices[i]) for i in idxs]

    def _embedding_search_fallback(self, q_emb: list, top_k: int) -> list:
        """Fallback：Python 迴圈版 embedding 搜尋

        返回: [(emb_score, chunk_idx), ...] 按分數降序
        """
        if top_k <= 0:
            return []
        import heapq
        results = []
        for idx, chunk in enumerate(self.chunks):
            emb = chunk.get("embedding", [])
            if emb:
                score = self._cosine_similarity(q_emb, emb)
                results.append((score, idx))

        return heapq.nlargest(top_k, results, key=lambda x: x[0])

    def _keyword_search_fallback(self, query_keywords: set, top_k: int) -> list:
        """Fallback：舊的 keyword matching（當 BM25 未啟用時）

        返回: [(kw_score, chunk_idx), ...] 按分數降序
        """
        results = []
        for idx, chunk in enumerate(self.chunks):
            content = chunk.get("content", "")
            score = self._keyword_score(query_keywords, content)
            if score > 0:
                results.append((score, idx))

        results.sort(reverse=True, key=lambda x: x[0])
        return results[:top_k]

    def _hybrid_search_numpy(self, q_emb: list, query_keywords: set, candidate_k: int) -> list:
        """使用 numpy 向量化的混合搜尋"""
        # 正規化 query embedding
        q_vec = np.array(q_emb, dtype=np.float32)
        q_norm = np.linalg.norm(q_vec)
        if q_norm > 0:
            q_vec = q_vec / q_norm

        # 批次計算所有 cosine similarity（因為已預正規化，dot product = cosine similarity）
        emb_scores = np.dot(self._embeddings, q_vec)

        # 建立結果列表
        scores = []
        for arr_idx, chunk_idx in enumerate(self._embedding_indices):
            chunk = self.chunks[chunk_idx]
            emb_score = float(emb_scores[arr_idx])
            content = chunk.get("content", "")

            kw_score = self._keyword_score(query_keywords, content) if USE_HYBRID_SEARCH else 0.0

            if USE_HYBRID_SEARCH:
                combined = emb_score * (1 - KEYWORD_WEIGHT) + kw_score * KEYWORD_WEIGHT
            else:
                combined = emb_score

            scores.append((combined, emb_score, kw_score, chunk))

        # 處理沒有 embedding 的 chunks（給予 0 分）
        indexed_set = set(self._embedding_indices)
        for i, chunk in enumerate(self.chunks):
            if i not in indexed_set:
                content = chunk.get("content", "")
                kw_score = self._keyword_score(query_keywords, content) if USE_HYBRID_SEARCH else 0.0
                combined = kw_score * KEYWORD_WEIGHT if USE_HYBRID_SEARCH else 0.0
                scores.append((combined, 0.0, kw_score, chunk))

        return scores

    def _hybrid_search_fallback(self, q_emb: list, query_keywords: set) -> list:
        """Fallback：Python 迴圈版混合搜尋（無 numpy 時使用）"""
        scores = []
        for chunk in self.chunks:
            emb = chunk.get("embedding", [])
            content = chunk.get("content", "")

            emb_score = 0.0
            if emb:
                emb_score = self._cosine_similarity(q_emb, emb)

            kw_score = self._keyword_score(query_keywords, content) if USE_HYBRID_SEARCH else 0.0

            if USE_HYBRID_SEARCH:
                combined = emb_score * (1 - KEYWORD_WEIGHT) + kw_score * KEYWORD_WEIGHT
            else:
                combined = emb_score

            scores.append((combined, emb_score, kw_score, chunk))

        return scores

    def _update_scores_with_expansion(self, scores: list, eq_emb: list):
        """使用擴展 query 更新分數"""
        if HAS_NUMPY and self._embeddings is not None and self._embeddings_normalized:
            # Numpy 向量化版本
            eq_vec = np.array(eq_emb, dtype=np.float32)
            eq_norm = np.linalg.norm(eq_vec)
            if eq_norm > 0:
                eq_vec = eq_vec / eq_norm
            exp_scores = np.dot(self._embeddings, eq_vec)

            # 建立 chunk -> arr_idx 的映射
            chunk_to_arr = {id(self.chunks[ci]): ai for ai, ci in enumerate(self._embedding_indices)}

            for i, (combined, emb_score, kw_score, chunk) in enumerate(scores):
                arr_idx = chunk_to_arr.get(id(chunk))
                if arr_idx is not None:
                    exp_score = float(exp_scores[arr_idx]) * 0.9  # 擴展分數打 9 折
                    new_emb = max(emb_score, exp_score)
                    if USE_HYBRID_SEARCH:
                        new_combined = new_emb * (1 - KEYWORD_WEIGHT) + kw_score * KEYWORD_WEIGHT
                    else:
                        new_combined = new_emb
                    scores[i] = (new_combined, new_emb, kw_score, chunk)
        else:
            # Fallback：逐一計算
            for i, (combined, emb_score, kw_score, chunk) in enumerate(scores):
                chunk_emb = chunk.get("embedding", [])
                if chunk_emb:
                    exp_score = self._cosine_similarity(eq_emb, chunk_emb) * 0.9
                    new_emb = max(emb_score, exp_score)
                    if USE_HYBRID_SEARCH:
                        new_combined = new_emb * (1 - KEYWORD_WEIGHT) + kw_score * KEYWORD_WEIGHT
                    else:
                        new_combined = new_emb
                    scores[i] = (new_combined, new_emb, kw_score, chunk)

    def _should_rerank(self, candidates: list, top_k: int, is_strict_mode: bool = False) -> bool:
        """判斷是否需要 rerank

        改進：
        - RERANKER_ALWAYS_ON = True 時，有 reranker 就一律使用
        - 嚴格模式下強制 rerank（STRICT_MODE_RERANK_REQUIRED）
        - 高信心時跳過 rerank（top_emb_score > RERANKER_SKIP_THRESHOLD）
        - 否則使用條件觸發
        """
        if len(candidates) <= top_k:
            return False

        # P0-4 修正：使用 embedding score (candidates[i][1]) 而非 RRF score (candidates[i][0])
        # RRF score 範圍約 0.01-0.03，和固定門檻完全不在同一量級
        # 格式是 (rrf_score, emb_score, bm25_score, chunk)
        top_emb_score = candidates[0][1] if candidates else 0

        # 改進：高信心時跳過 rerank（減少不必要的延遲）
        # 但嚴格模式和 ALWAYS_ON 除外
        if not RERANKER_ALWAYS_ON and not is_strict_mode:
            if top_emb_score >= RERANKER_SKIP_THRESHOLD:
                return False

        # P0 改進：強制啟用 reranker
        if RERANKER_ALWAYS_ON:
            return True

        # 嚴格模式強制 rerank
        if is_strict_mode and STRICT_MODE_RERANK_REQUIRED:
            return True

        # Margin-based 判斷（P0 改進）- 改用 embedding score
        if MARGIN_ENABLED and len(candidates) >= 2:
            gap = candidates[0][1] - candidates[1][1]  # P0-4: 用 emb score 差距
            # top1-top2 差距太小 → 不確定，需要 rerank
            if gap < MARGIN_MIN_GAP:
                return True
            # top1 分數太低 → 需要更精確判斷
            if top_emb_score < MARGIN_LOW_SCORE:
                return True

        # 如果最高分很高（>0.6），且與第5名差距明顯（>0.1），不需要 rerank
        if top_emb_score > 0.6:
            fifth_emb_score = candidates[min(4, len(candidates)-1)][1] if len(candidates) > 4 else 0
            if top_emb_score - fifth_emb_score > 0.1:
                return False

        # 如果前幾名分數太接近（差距 < 0.05），需要 rerank 來區分
        if len(candidates) >= 3:
            score_diff = candidates[0][1] - candidates[2][1]  # P0-4: 用 emb score
            if score_diff < 0.05:
                return True

        # 其他情況：top_emb_score 較低時，需要 rerank
        return top_emb_score < 0.5

    def _rerank_fallback(self, question: str, candidates: list, top_k: int, reason: str) -> list:
        """Apply the configured fallback after the dedicated reranker cannot be used."""
        policy = config.RERANK_FALLBACK_POLICY
        if policy == "embedding":
            return [c[3] for c in candidates[:top_k]]
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
        """使用專用 reranker 模型重排

        P0 改進：
        1. RERANKER_ALWAYS_ON = True 時預設啟用
        2. 使用 RERANKER_TOP_N 控制 rerank 後取幾個
        3. 嚴格模式下強制 rerank
        """
        if not candidates:
            return []

        if not USE_RERANKER or len(candidates) <= top_k:
            return [c[3] for c in candidates[:top_k]]

        # 條件觸發：判斷是否真的需要 rerank
        if not self._should_rerank(candidates, top_k, is_strict_mode):
            return [c[3] for c in candidates[:top_k]]

        # P0 改進：使用 RERANKER_TOP_N 控制候選數量
        rerank_count = min(RERANKER_TOP_N, len(candidates))

        if self._check_reranker_available():
            try:
                items = candidates[:rerank_count]
                passages = [chunk.get('content', '')[:800] for _, _, _, chunk in items]
                scores = llama_client.rerank(
                    base_url=LLAMA_RERANK_BASE_URL,
                    query=question,
                    documents=passages,
                    model=RERANKER_MODEL,
                    timeout=60,
                )
                scored = [(scores[i], items[i][3]) for i in range(len(items))]
                scored.sort(reverse=True, key=lambda x: x[0])
                return [c[1] for c in scored[:top_k]]

            except Exception as exc:
                return self._rerank_fallback(
                    question, candidates, top_k, f"dedicated reranker call failed: {exc}"
                )

        return self._rerank_fallback(question, candidates, top_k, "dedicated reranker is not reachable")

    def _rerank_with_llm(self, question: str, candidates: list, top_k: int) -> list:
        """LLM Reranking (fallback)"""
        if not candidates:
            return []

        docs_text = ""
        for i, (score, _, _, chunk) in enumerate(candidates[:15]):
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
                return [candidates[i][3] for i in doc_indices]

        except Exception:
            pass

        return [c[3] for c in candidates[:top_k]]

    def _mmr_select(self, chunks: list, question_emb: list, k: int, lambda_: float = MMR_LAMBDA) -> list:
        """Max Marginal Relevance 選擇：平衡相關性與多樣性

        改進：使用 numpy 加速向量運算（若可用）
        """
        if not chunks or not question_emb:
            return chunks[:k]

        # 嘗試使用 numpy 加速
        if HAS_NUMPY and len(chunks) > 3:
            return self._mmr_select_numpy(chunks, question_emb, k, lambda_)

        # Fallback：原始 Python 實作
        selected = []
        selected_embs = []

        for _ in range(min(k, len(chunks))):
            best, best_score = None, -float('inf')

            for c in chunks:
                if c in selected:
                    continue

                c_emb = c.get("embedding", [])
                if not c_emb:
                    mmr_score = -1
                else:
                    sim_q = self._cosine_similarity(question_emb, c_emb)
                    sim_rep = 0.0
                    if selected_embs:
                        sim_rep = max(self._cosine_similarity(c_emb, e) for e in selected_embs)
                    mmr_score = lambda_ * sim_q - (1 - lambda_) * sim_rep

                if mmr_score > best_score:
                    best, best_score = c, mmr_score

            if best is None:
                break

            selected.append(best)
            best_emb = best.get("embedding", [])
            if best_emb:
                selected_embs.append(best_emb)

        return selected

    def _mmr_select_numpy(self, chunks: list, question_emb: list, k: int, lambda_: float) -> list:
        """使用 numpy 加速的 MMR 選擇"""
        # 收集有效的 embeddings
        valid_chunks = []
        embeddings = []

        for c in chunks:
            emb = c.get("embedding", [])
            if emb:
                valid_chunks.append(c)
                embeddings.append(emb)

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

        # 預計算與 query 的相似度
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
                    "last_idx": chunk_idx,
                    "embedding": c_emb,
                    "_emb_sum": emb_sum,
                    "_emb_count": emb_count,
                }
            elif (buffer["key"] == key and
                  chunk_idx == buffer["last_idx"] + 1 and
                  buffer["section"] == chunk_section and  # 不跨 section 合併
                  len(buffer["content"]) + len(c.get("content", "")) < KNOWLEDGE_MERGE_MAX_CHARS):
                buffer["content"] += "\n" + c.get("content", "")
                buffer["last_idx"] = chunk_idx
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
                    "last_idx": chunk_idx,
                    "embedding": c.get("embedding", []),
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

    def query(self, question: str, top_k: int = KNOWLEDGE_TOP_K,
              is_strict_mode: bool = False) -> tuple[str, str, dict]:
        """
        查詢相關知識 - 結構化輸出版本（P0 改進：Margin-based 動態門檻）

        回傳: (model_output, display_output, metadata)
        metadata 包含: has_ref, top_score, ref_count, is_high_risk
        """
        empty_metadata = {"has_ref": False, "top_score": 0.0, "ref_count": 0, "is_high_risk": False}

        if not self.loaded or not self.chunks:
            return "", "", empty_metadata

        candidates = self._hybrid_search(question, KNOWLEDGE_CANDIDATE_K)
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

        # 改用 embedding score (candidates[i][1]) 作為過濾依據，而非 combined score
        # P0 改進：格式是 (rrf_score, emb_score, bm25_score, chunk)
        top_emb_score = candidates[0][1]  # (rrf, emb, bm25, chunk)
        min_emb_score = max(base_threshold, top_emb_score * DYNAMIC_THRESHOLD_RATIO)

        # P0 改進：Margin-based 風險判斷
        is_high_risk = False
        if MARGIN_ENABLED and len(candidates) >= 2:
            gap = candidates[0][1] - candidates[1][1]  # 用 embedding score 算 gap
            if gap < MARGIN_MIN_GAP:
                is_high_risk = True  # top1-top2 差距太小，不確定
            if top_emb_score < MARGIN_LOW_SCORE:
                is_high_risk = True  # top1 分數太低

        # 過濾：只看 embedding score，避免 keyword 誤打誤撞拉高分數
        filtered = [(s, e, k, c) for s, e, k, c in candidates if e >= min_emb_score]
        if not filtered:
            return "", "", empty_metadata

        # 動態 top_k：高相關度時少給，低相關度時多給
        # P0-4 修正：使用 embedding score 來決定 top_k，避免 RRF score 量級問題
        # top_emb_score 在上面已經取得
        # P0-4: 保留 RRF score 供 metadata 向後相容
        top_score = candidates[0][0]  # RRF score，僅供 metadata 記錄
        if top_emb_score >= DYNAMIC_TOP_K_HIGH_SCORE:
            effective_top_k = DYNAMIC_TOP_K_MIN
        else:
            effective_top_k = min(top_k, DYNAMIC_TOP_K_MAX)

        # P0 改進：傳入 is_strict_mode 給 reranker
        reranked_chunks = self._rerank_with_model(question, filtered, effective_top_k * 2,
                                                   is_strict_mode=is_strict_mode)
        if not reranked_chunks:
            return "", "", empty_metadata

        if USE_MMR:
            q_emb = self._get_embedding(question)
            top_chunks = self._mmr_select(reranked_chunks, q_emb, effective_top_k)
        else:
            top_chunks = reranked_chunks[:effective_top_k]

        if not top_chunks:
            return "", "", empty_metadata

        # P0 改進：計算 embedding scores 供污染風險控制使用
        q_emb_prelim = q_emb if USE_MMR else self._get_embedding(question)
        prelim_emb_scores = []
        for c in top_chunks:
            c_emb = c.get("embedding", [])
            if c_emb and q_emb_prelim:
                prelim_emb_scores.append(self._cosine_similarity(q_emb_prelim, c_emb))
            else:
                prelim_emb_scores.append(0.0)

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

        # 修正：用「最終被選中的 chunks」重新計算 top_emb_score
        # 避免 candidates[0] 被過濾/rerank 後，仍用它的低分來決定信心度
        # 這會導致「有好 REF 卻被誤判為低信心而跳過」
        q_emb_for_score = self._get_embedding(question) if not USE_MMR else q_emb
        used_emb_scores = []
        for c in merged_chunks:
            c_emb = c.get("embedding", [])
            if c_emb and q_emb_for_score:
                used_emb_scores.append(self._cosine_similarity(q_emb_for_score, c_emb))
        top_emb_score_used = max(used_emb_scores) if used_emb_scores else top_emb_score

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

            if KNOWLEDGE_INCLUDE_CONTENT:
                content = chunk.get('content', '')
                original_len = len(content)
                max_chars = KNOWLEDGE_MERGE_MAX_CHARS if KNOWLEDGE_MERGE_ADJACENT else KNOWLEDGE_CONTENT_MAX_CHARS
                if original_len > max_chars:
                    content = content[:max_chars] + f"... [REF{i} 內容已截斷，原長度 {original_len} 字元]"

                model_lines.append(f"\n[REF{i}]")
                model_lines.append(f"  type: {doc_type}")
                model_lines.append(f"  source: {source}")
                model_lines.append(f"  page: {page}")
                if section:
                    model_lines.append(f"  section: {section}")
                model_lines.append(f"  content: {content}")
            else:
                section_hint = f" ({section})" if section else ""
                model_lines.append(f"  - REF{i}: {source} 第 {page} 頁 [{doc_type}]{section_hint}")

        model_lines.append("\n[/REF]")

        # 移除詳細回答規則，避免與 config.get_answer_rules() 重複/打架
        # 只保留輕量提示，主要規則由呼叫端統一注入
        model_lines.append("\n※ 引用 REF 內容時請標註編號（如 REF1）")
        if has_spec:
            model_lines.append("※ spec 類型的 REF 優先級較高")
        if has_warning:
            model_lines.append("※ warning 類型的 REF 請特別注意其限制條件")

        model_output = "\n".join(model_lines)

        doc_pages = {}
        doc_types = {}
        for chunk in merged_chunks:
            src = chunk.get('source', '?')
            chunk_type = chunk.get('type', 'doc')
            if src not in doc_pages:
                doc_pages[src] = []
                doc_types[src] = chunk_type
            else:
                # 改進：同 source 只要出現 warning/spec 就升級 type
                doc_types[src] = self._upgrade_type(doc_types[src], chunk_type)
            page = chunk.get('page')
            if page and page not in doc_pages[src]:
                doc_pages[src].append(page)

        display_parts = []
        for src, pages in doc_pages.items():
            pages_str = ", ".join(str(p) for p in sorted(pages)[:5])
            if len(pages) > 5:
                pages_str += "..."
            dtype = doc_types.get(src, 'doc')
            display_parts.append(f"{src} [{dtype}] p.{pages_str}")

        # display 也顯示信心度，讓用戶知道參考資料的可靠度
        # P0 改進：高風險時加上警告
        risk_warning = " ⚠️" if is_high_risk else ""
        display_output = f"[REF {confidence_label}{risk_warning}] " + " | ".join(display_parts)

        # 回傳 metadata 供上層判斷 REF 強度
        # 改進：分別回傳 embedding score 和 keyword score，讓 spec 題拒答只看 embedding
        # 修正：top_emb_score 改用「最終被選中的 chunks」的最高分，而非 candidates[0]
        # 這避免了「candidates[0] 被過濾掉，但 top_emb_score 仍用它的低分」的問題
        top_kw_score = candidates[0][2] if candidates else 0.0
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
                "section": c.get("section", "")
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
            "top_score": top_score,               # combined score（向後相容）
            "top_emb_score": top_emb_score_used,  # 修正：用最終選中 chunks 的最高 emb score
            "top_kw_score": top_kw_score,         # 純 keyword/BM25 score
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

        feature_str = f" [{'+'.join(features)}]" if features else ""
        return f"[KB] 知識庫: {self.path} ({doc_count} 文件, {chunk_count} 區塊){feature_str}"
