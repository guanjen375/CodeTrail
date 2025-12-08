#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 知識庫 (RAG)
"""

import re
import json
from pathlib import Path
from functools import lru_cache

from http_client import get_session

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

from config import (
    OLLAMA_GENERATE_URL, OLLAMA_EMBEDDINGS_URL, OLLAMA_TAGS_URL,
    MODEL, KNOWLEDGE_FILE,
    KNOWLEDGE_TOP_K, KNOWLEDGE_CANDIDATE_K, KNOWLEDGE_THRESHOLD,
    KNOWLEDGE_THRESHOLD_SHORT, KNOWLEDGE_SHORT_QUERY_TOKENS,
    DYNAMIC_THRESHOLD_RATIO, DYNAMIC_TOP_K_HIGH_SCORE,
    DYNAMIC_TOP_K_MIN, DYNAMIC_TOP_K_MAX,
    KNOWLEDGE_INCLUDE_CONTENT, KNOWLEDGE_CONTENT_MAX_CHARS,
    KNOWLEDGE_MERGE_ADJACENT, KNOWLEDGE_MERGE_MAX_CHARS,
    EMBEDDING_MODEL, RERANKER_MODEL,
    USE_RERANKER, USE_HYBRID_SEARCH, USE_QUERY_EXPANSION,
    USE_MMR, MMR_LAMBDA, KEYWORD_WEIGHT
)


def _normalize_text_for_cache(text: str) -> str:
    """正規化文字以提高 cache 命中率

    - 移除多餘空白
    - 統一換行符
    """
    return ' '.join(text.split())


@lru_cache(maxsize=256)
def _cached_get_embedding(text: str) -> tuple:
    """帶 LRU cache 的 embedding 查詢

    改進：追問/重跑時可重用已查詢過的 embedding，提升速度
    注意：回傳 tuple 而非 list，因為 lru_cache 需要 hashable
    """
    try:
        session = get_session()
        resp = session.post(
            OLLAMA_EMBEDDINGS_URL,
            json={"model": EMBEDDING_MODEL, "prompt": text},
            timeout=120
        )
        resp.raise_for_status()
        emb = resp.json().get("embedding", [])
        return tuple(emb) if emb else ()
    except Exception:
        return ()


class KnowledgeBase:
    """
    優化版知識庫：
    1. 專用 Reranker 模型 (bge-reranker)
    2. Query Expansion (LLM 生成搜尋關鍵字)
    3. 結構化輸出格式
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

            # 預計算 numpy embeddings（用於加速向量運算）
            self._precompute_embeddings()

        except Exception as e:
            print(f"[WARN] 知識庫載入失敗: {e}")
            self.loaded = False

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

    def _check_reranker_available(self) -> bool:
        """檢查 reranker 模型是否可用

        改進：檢查 RERANKER_MODEL 是否已安裝，而非只要有任意 reranker 就視為可用
        避免設定了 A 模型但機器上只有 B 模型，導致每次都先嘗試 A → 失敗 → fallback
        """
        if self._reranker_available is not None:
            return self._reranker_available

        try:
            session = get_session()
            resp = session.get(OLLAMA_TAGS_URL, timeout=5)
            if resp.status_code == 200:
                models = [m.get("name", "") for m in resp.json().get("models", [])]
                # 精確檢查 RERANKER_MODEL 是否已安裝
                # Ollama model name 格式可能是 "model:tag" 或 "model"
                reranker_base = RERANKER_MODEL.split(":")[0].lower()
                self._reranker_available = any(
                    m.lower() == RERANKER_MODEL.lower() or
                    m.lower().startswith(reranker_base + ":")
                    for m in models
                )
            else:
                self._reranker_available = False
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
        return list(result) if result else []

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
        return {w for w in words if len(w) > 2 and w not in stopwords}

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
        """用 LLM 生成額外的搜尋關鍵字

        改進：
        - 預設不啟動，只有 force=True 或候選不足時才啟用
        - 解析時同時支援 , 和 ，（全形逗號）
        - 過濾非字母數字的 token
        """
        if not USE_QUERY_EXPANSION and not force:
            return [question]

        try:
            prompt = f"""從以下問題中提取 3-5 個適合用於搜尋技術文件的英文關鍵字。
只輸出關鍵字，用逗號分隔，不要解釋。

問題: {question}

關鍵字:"""

            session = get_session()
            resp = session.post(
                OLLAMA_GENERATE_URL,
                json={
                    "model": MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"num_ctx": 2048, "temperature": 0}
                },
                timeout=30
            )
            resp.raise_for_status()
            result = resp.json().get("response", "").strip()

            # 同時支援半形和全形逗號
            raw_keywords = re.split(r'[,，]', result)
            keywords = []
            for kw in raw_keywords:
                kw = kw.strip()
                # 只保留字母數字 token，過濾句子或雜訊
                if kw and re.match(r'^[A-Za-z0-9_\-]+$', kw) and len(kw) <= 30:
                    keywords.append(kw)

            keywords = keywords[:5]

            if keywords:
                expanded = f"{question} {' '.join(keywords)}"
                return [question, expanded]

        except Exception:
            pass

        return [question]

    def _should_expand_query(self, candidates: list, threshold: float = 0.35) -> bool:
        """判斷是否需要 Query Expansion

        條件：候選數量不足 或 最高分數偏低
        """
        if not candidates:
            return True
        if len(candidates) < 3:
            return True
        top_score = candidates[0][0] if candidates else 0
        return top_score < threshold

    def _hybrid_search(self, question: str, candidate_k: int = KNOWLEDGE_CANDIDATE_K) -> list:
        """混合搜尋：Embedding + 關鍵字 + 條件式 Query Expansion

        改進：
        1. 先用原始 query 搜一次，若候選不足/分數偏低，再啟用 expansion
        2. 使用 numpy 向量化加速（若可用）
        """
        if not self.loaded or not self.chunks:
            return []

        # 取得 query embedding
        q_emb = self._get_embedding(question)
        if not q_emb:
            return []

        query_keywords = self._extract_keywords(question) if USE_HYBRID_SEARCH else set()

        # 使用 numpy 向量化計算（若可用且已預計算）
        if HAS_NUMPY and self._embeddings is not None and self._embeddings_normalized:
            scores = self._hybrid_search_numpy(q_emb, query_keywords, candidate_k)
        else:
            scores = self._hybrid_search_fallback(q_emb, query_keywords)

        scores.sort(reverse=True, key=lambda x: x[0])
        first_round = scores[:candidate_k]

        # 條件式 Query Expansion：只有候選不足或分數偏低時才啟用
        if USE_QUERY_EXPANSION and self._should_expand_query(first_round):
            expanded_queries = self._expand_query(question, force=True)
            if len(expanded_queries) > 1:  # 有擴展的 query
                # 用擴展的 query 重新搜尋
                for eq in expanded_queries[1:]:  # 跳過原始 query
                    eq_emb = self._get_embedding(eq)
                    if eq_emb:
                        self._update_scores_with_expansion(scores, eq_emb)

                # 重新排序
                scores.sort(reverse=True, key=lambda x: x[0])

        return scores[:candidate_k]

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

    def _should_rerank(self, candidates: list, top_k: int) -> bool:
        """判斷是否需要 rerank

        條件觸發：只有在 top_score 不高 或 前幾名分數差距很小時才 rerank
        這可以大幅減少 reranker 呼叫次數，提升速度
        """
        if len(candidates) <= top_k:
            return False

        top_score = candidates[0][0] if candidates else 0
        # 如果最高分很高（>0.6），且與第5名差距明顯（>0.1），不需要 rerank
        if top_score > 0.6:
            fifth_score = candidates[min(4, len(candidates)-1)][0] if len(candidates) > 4 else 0
            if top_score - fifth_score > 0.1:
                return False

        # 如果前幾名分數太接近（差距 < 0.05），需要 rerank 來區分
        if len(candidates) >= 3:
            score_diff = candidates[0][0] - candidates[2][0]
            if score_diff < 0.05:
                return True

        # 其他情況：top_score 較低時，需要 rerank
        return top_score < 0.5

    def _rerank_with_model(self, question: str, candidates: list, top_k: int) -> list:
        """使用專用 reranker 模型重排

        改進：
        1. 條件觸發：只有必要時才 rerank（提升速度）
        2. 減少 candidates 數量：min(10, top_k*2) 而非固定 20
        """
        if not candidates:
            return []

        if not USE_RERANKER or len(candidates) <= top_k:
            return [c[3] for c in candidates[:top_k]]

        # 條件觸發：判斷是否真的需要 rerank
        if not self._should_rerank(candidates, top_k):
            return [c[3] for c in candidates[:top_k]]

        # 減少 rerank 的 candidates 數量
        rerank_count = min(10, top_k * 2)

        if self._check_reranker_available():
            try:
                session = get_session()
                scored = []
                for score, _, _, chunk in candidates[:rerank_count]:
                    content = chunk.get('content', '')[:800]

                    # 改進：使用 stop 和 num_predict 強制 numeric output
                    # - num_predict: 限制最多輸出 10 個 token（足夠 0.xxxxx 格式）
                    # - stop: 遇到換行或空格就停止
                    resp = session.post(
                        OLLAMA_GENERATE_URL,
                        json={
                            "model": RERANKER_MODEL,
                            "prompt": f"Query: {question}\n\nPassage: {content}",
                            "stream": False,
                            "options": {
                                "num_ctx": 2048,
                                "temperature": 0,
                                "num_predict": 10,
                                "stop": ["\n", " ", ",", "。", "，"]
                            }
                        },
                        timeout=30
                    )
                    resp.raise_for_status()

                    result = resp.json().get("response", "").strip()
                    try:
                        rerank_score = float(result)
                    except ValueError:
                        # Fallback: 嘗試從 result 中提取數字
                        match = re.search(r'-?[\d.]+', result)
                        rerank_score = float(match.group()) if match else score

                    scored.append((rerank_score, chunk))

                scored.sort(reverse=True, key=lambda x: x[0])
                return [c[1] for c in scored[:top_k]]

            except Exception:
                pass

        return self._rerank_with_llm(question, candidates, top_k)

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
            session = get_session()
            resp = session.post(
                OLLAMA_GENERATE_URL,
                json={
                    "model": MODEL,
                    "prompt": rerank_prompt,
                    "stream": False,
                    "options": {"num_ctx": 8192, "temperature": 0}
                },
                timeout=60
            )
            resp.raise_for_status()
            result = resp.json().get("response", "")

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

        for c in sorted_chunks:
            key = (c.get("source", ""), c.get("page", 0))
            chunk_idx = c.get("chunk_index", 0)
            chunk_type = c.get("type", "doc")
            chunk_section = c.get("section", "")

            if buffer is None:
                buffer = {
                    "key": key,
                    "source": c.get("source", ""),
                    "page": c.get("page", 0),
                    "content": c.get("content", ""),
                    "type": chunk_type,
                    "section": chunk_section,
                    "last_idx": chunk_idx,
                    "embedding": c.get("embedding", []),
                }
            elif (buffer["key"] == key and
                  chunk_idx == buffer["last_idx"] + 1 and
                  buffer["section"] == chunk_section and  # 不跨 section 合併
                  len(buffer["content"]) + len(c.get("content", "")) < KNOWLEDGE_MERGE_MAX_CHARS):
                buffer["content"] += "\n" + c.get("content", "")
                buffer["last_idx"] = chunk_idx
                # 升級 type（warning > spec > doc）
                buffer["type"] = self._upgrade_type(buffer["type"], chunk_type)
            else:
                merged.append(buffer)
                buffer = {
                    "key": key,
                    "source": c.get("source", ""),
                    "page": c.get("page", 0),
                    "content": c.get("content", ""),
                    "type": chunk_type,
                    "section": chunk_section,
                    "last_idx": chunk_idx,
                    "embedding": c.get("embedding", []),
                }

        if buffer:
            merged.append(buffer)

        return merged

    def _estimate_tokens(self, text: str) -> int:
        """簡單估算 token 數（中文約 1.5 字/token，英文約 4 字元/token）"""
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        other_chars = len(text) - chinese_chars
        return int(chinese_chars / 1.5 + other_chars / 4)

    def query(self, question: str, top_k: int = KNOWLEDGE_TOP_K) -> tuple[str, str, dict]:
        """
        查詢相關知識 - 結構化輸出版本（動態門檻 + 動態 top_k）
        回傳: (model_output, display_output, metadata)
        metadata 包含: has_ref, top_score, ref_count
        """
        empty_metadata = {"has_ref": False, "top_score": 0.0, "ref_count": 0}

        if not self.loaded or not self.chunks:
            return "", "", empty_metadata

        candidates = self._hybrid_search(question, KNOWLEDGE_CANDIDATE_K)
        if not candidates:
            return "", "", empty_metadata

        # 動態門檻：短問題用較低門檻
        # GPT建議：過濾時只看 embedding score，keyword 只用來排序
        query_tokens = self._estimate_tokens(question)
        base_threshold = KNOWLEDGE_THRESHOLD_SHORT if query_tokens < KNOWLEDGE_SHORT_QUERY_TOKENS else KNOWLEDGE_THRESHOLD

        # 改用 embedding score (candidates[i][1]) 作為過濾依據，而非 combined score
        top_emb_score = candidates[0][1]  # (combined, emb, kw, chunk)
        min_emb_score = max(base_threshold, top_emb_score * DYNAMIC_THRESHOLD_RATIO)

        # 過濾：只看 embedding score，避免 keyword 誤打誤撞拉高分數
        filtered = [(s, e, k, c) for s, e, k, c in candidates if e >= min_emb_score]
        if not filtered:
            return "", "", empty_metadata

        # 動態 top_k：高相關度時少給，低相關度時多給
        # 使用 combined score 來決定 top_k（排序仍用 combined）
        top_score = candidates[0][0]
        if top_score >= DYNAMIC_TOP_K_HIGH_SCORE:
            effective_top_k = DYNAMIC_TOP_K_MIN
        else:
            effective_top_k = min(top_k, DYNAMIC_TOP_K_MAX)

        reranked_chunks = self._rerank_with_model(question, filtered, effective_top_k * 2)
        if not reranked_chunks:
            return "", "", empty_metadata

        if USE_MMR:
            q_emb = self._get_embedding(question)
            top_chunks = self._mmr_select(reranked_chunks, q_emb, effective_top_k)
        else:
            top_chunks = reranked_chunks[:effective_top_k]

        if not top_chunks:
            return "", "", empty_metadata

        merged_chunks = self._merge_adjacent_chunks(top_chunks)

        has_spec = any(chunk.get('type') == 'spec' for chunk in merged_chunks)
        has_warning = any(chunk.get('type') == 'warning' for chunk in merged_chunks)

        # GPT建議：在 REF header 加入信心分數提示，讓 LLM 了解參考資料的可靠度
        confidence_label = ""
        if top_emb_score >= 0.6:
            confidence_label = "高信心"
        elif top_emb_score >= 0.4:
            confidence_label = "中信心"
        else:
            confidence_label = "低信心"

        model_lines = [f"[REF] 相關知識參考（信心度: {confidence_label}, score={top_emb_score:.2f}）:"]
        model_lines.append(f"※ 信心度說明：高信心(≥0.6)資料可直接引用，中信心(0.4-0.6)請謹慎使用，低信心(<0.4)僅供參考")

        for i, chunk in enumerate(merged_chunks, 1):
            source = chunk.get('source', '未知')
            page = chunk.get('page', '?')
            doc_type = chunk.get('type', 'doc')
            section = chunk.get('section', '')

            if KNOWLEDGE_INCLUDE_CONTENT:
                content = chunk.get('content', '')
                max_chars = KNOWLEDGE_MERGE_MAX_CHARS if KNOWLEDGE_MERGE_ADJACENT else KNOWLEDGE_CONTENT_MAX_CHARS
                if len(content) > max_chars:
                    content = content[:max_chars] + "..."

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

        # GPT建議：移除詳細回答規則，避免與 config.get_answer_rules() 重複/打架
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

        # GPT建議：display 也顯示信心度，讓用戶知道參考資料的可靠度
        display_output = f"[REF {confidence_label}] " + " | ".join(display_parts)

        # 回傳 metadata 供上層判斷 REF 強度
        # 改進：分別回傳 embedding score 和 keyword score，讓 spec 題拒答只看 embedding
        top_emb_score = candidates[0][1] if candidates else 0.0  # (combined, emb, kw, chunk)
        top_kw_score = candidates[0][2] if candidates else 0.0
        has_spec_chunk = any(chunk.get('type') == 'spec' for chunk in merged_chunks)

        metadata = {
            "has_ref": len(merged_chunks) > 0,
            "top_score": top_score,           # combined score（向後相容）
            "top_emb_score": top_emb_score,   # 純 embedding score
            "top_kw_score": top_kw_score,     # 純 keyword score
            "has_spec_chunk": has_spec_chunk, # 是否命中 spec 類型的 chunk
            "ref_count": len(merged_chunks)
        }

        return model_output, display_output, metadata

    def get_status(self) -> str:
        if not self.loaded:
            return "[KB] 知識庫: (空)"

        chunk_count = len(self.chunks)
        doc_count = len(self.documents)
        features = []
        if USE_HYBRID_SEARCH:
            features.append("Hybrid")
        if USE_RERANKER:
            reranker_type = "Model" if self._check_reranker_available() else "LLM"
            features.append(f"Rerank({reranker_type})")
        if USE_QUERY_EXPANSION:
            features.append("QExp")
        feature_str = f" [{'+'.join(features)}]" if features else ""
        return f"[KB] 知識庫: {self.path} ({doc_count} 文件, {chunk_count} 區塊){feature_str}"
