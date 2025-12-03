#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 知識庫 (RAG)
"""

import re
import json
import requests
from pathlib import Path

from config import (
    OLLAMA_GENERATE_URL, MODEL, KNOWLEDGE_FILE,
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

        if Path(json_path).exists():
            self._load(json_path)

    def _load(self, path: str):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self.chunks = data.get("chunks", [])
            metadata = data.get("metadata", {})
            self.documents = metadata.get("documents", [])
            self.loaded = True
        except Exception as e:
            print(f"[WARN] 知識庫載入失敗: {e}")
            self.loaded = False

    def _check_reranker_available(self) -> bool:
        """檢查 reranker 模型是否可用"""
        if self._reranker_available is not None:
            return self._reranker_available

        try:
            resp = requests.get("http://localhost:11434/api/tags", timeout=5)
            if resp.status_code == 200:
                models = [m.get("name", "") for m in resp.json().get("models", [])]
                self._reranker_available = any(
                    "reranker" in m.lower() or "bge-reranker" in m.lower()
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
        try:
            resp = requests.post(
                "http://localhost:11434/api/embeddings",
                json={"model": EMBEDDING_MODEL, "prompt": text},
                timeout=120
            )
            resp.raise_for_status()
            return resp.json().get("embedding", [])
        except Exception:
            return []

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
        if not query_keywords:
            return 0.0
        chunk_lower = chunk_content.lower()
        matches = sum(1 for kw in query_keywords if kw in chunk_lower)
        return matches / len(query_keywords)

    def _expand_query(self, question: str) -> list[str]:
        """用 LLM 生成額外的搜尋關鍵字"""
        if not USE_QUERY_EXPANSION:
            return [question]

        try:
            prompt = f"""從以下問題中提取 3-5 個適合用於搜尋技術文件的英文關鍵字。
只輸出關鍵字，用逗號分隔，不要解釋。

問題: {question}

關鍵字:"""

            resp = requests.post(
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

            keywords = [kw.strip() for kw in result.split(',') if kw.strip()]
            keywords = keywords[:5]

            if keywords:
                expanded = f"{question} {' '.join(keywords)}"
                return [question, expanded]

        except Exception:
            pass

        return [question]

    def _hybrid_search(self, question: str, candidate_k: int = KNOWLEDGE_CANDIDATE_K) -> list:
        """混合搜尋：Embedding + 關鍵字 + Query Expansion"""
        if not self.loaded or not self.chunks:
            return []

        queries = self._expand_query(question)

        query_embeddings = []
        for q in queries:
            emb = self._get_embedding(q)
            if emb:
                query_embeddings.append(emb)

        if not query_embeddings:
            return []

        query_keywords = self._extract_keywords(question) if USE_HYBRID_SEARCH else set()

        scores = []
        for chunk in self.chunks:
            emb = chunk.get("embedding", [])
            content = chunk.get("content", "")

            emb_score = 0.0
            if emb:
                for q_emb in query_embeddings:
                    s = self._cosine_similarity(q_emb, emb)
                    emb_score = max(emb_score, s)

            kw_score = self._keyword_score(query_keywords, content) if USE_HYBRID_SEARCH else 0.0

            if USE_HYBRID_SEARCH:
                combined = emb_score * (1 - KEYWORD_WEIGHT) + kw_score * KEYWORD_WEIGHT
            else:
                combined = emb_score

            scores.append((combined, emb_score, kw_score, chunk))

        scores.sort(reverse=True, key=lambda x: x[0])
        return scores[:candidate_k]

    def _rerank_with_model(self, question: str, candidates: list, top_k: int) -> list:
        """使用專用 reranker 模型重排"""
        if not candidates:
            return []

        if not USE_RERANKER or len(candidates) <= top_k:
            return [c[3] for c in candidates[:top_k]]

        if self._check_reranker_available():
            try:
                scored = []
                for score, _, _, chunk in candidates[:20]:
                    content = chunk.get('content', '')[:800]

                    resp = requests.post(
                        OLLAMA_GENERATE_URL,
                        json={
                            "model": RERANKER_MODEL,
                            "prompt": f"Query: {question}\n\nPassage: {content}",
                            "stream": False,
                            "options": {"num_ctx": 2048, "temperature": 0}
                        },
                        timeout=30
                    )
                    resp.raise_for_status()

                    result = resp.json().get("response", "").strip()
                    try:
                        rerank_score = float(result)
                    except ValueError:
                        match = re.search(r'[\d.]+', result)
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
            resp = requests.post(
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
        """Max Marginal Relevance 選擇：平衡相關性與多樣性"""
        if not chunks or not question_emb:
            return chunks[:k]

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

    def _merge_adjacent_chunks(self, chunks: list) -> list:
        """合併同一頁的相鄰 chunk"""
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

            if buffer is None:
                buffer = {
                    "key": key,
                    "source": c.get("source", ""),
                    "page": c.get("page", 0),
                    "content": c.get("content", ""),
                    "type": c.get("type", "doc"),
                    "section": c.get("section", ""),
                    "last_idx": chunk_idx,
                    "embedding": c.get("embedding", []),
                }
            elif (buffer["key"] == key and
                  chunk_idx == buffer["last_idx"] + 1 and
                  len(buffer["content"]) + len(c.get("content", "")) < KNOWLEDGE_MERGE_MAX_CHARS):
                buffer["content"] += "\n" + c.get("content", "")
                buffer["last_idx"] = chunk_idx
                if c.get("section"):
                    buffer["section"] = c.get("section")
            else:
                merged.append(buffer)
                buffer = {
                    "key": key,
                    "source": c.get("source", ""),
                    "page": c.get("page", 0),
                    "content": c.get("content", ""),
                    "type": c.get("type", "doc"),
                    "section": c.get("section", ""),
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
        query_tokens = self._estimate_tokens(question)
        base_threshold = KNOWLEDGE_THRESHOLD_SHORT if query_tokens < KNOWLEDGE_SHORT_QUERY_TOKENS else KNOWLEDGE_THRESHOLD

        top_score = candidates[0][0]
        min_score = max(base_threshold, top_score * DYNAMIC_THRESHOLD_RATIO)

        filtered = [(s, e, k, c) for s, e, k, c in candidates if s >= min_score]
        if not filtered:
            return "", "", empty_metadata

        # 動態 top_k：高相關度時少給，低相關度時多給
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

        model_lines = ["[REF] 相關知識參考（請在回答時引用 REF 編號）:"]

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

        model_lines.append("\n回答規則（嚴格遵守）:")
        model_lines.append("1. 凡是來自文件的描述，句尾必須標註 REF 編號，如「...（REF1）」")
        model_lines.append("2. 禁止憑常識或經驗猜測，若 REF 未提及，必須明講「文件中沒有明確說明」")
        model_lines.append("3. 若你的常識與 [REF] 內容衝突，一律以 [REF] 為準，不得自行修正")
        model_lines.append("4. 如果回答中完全沒有 REF 引用，要主動說明「以下為一般經驗，文件未明寫」")
        if has_spec:
            model_lines.append("5. 若 spec 與 guide/FAQ 衝突，以 type=spec 的 REF 為準")
        if has_warning:
            model_lines.append("6. type=warning 的內容請優先引用並強調限制條件")

        model_output = "\n".join(model_lines)

        doc_pages = {}
        doc_types = {}
        for chunk in merged_chunks:
            src = chunk.get('source', '?')
            if src not in doc_pages:
                doc_pages[src] = []
                doc_types[src] = chunk.get('type', 'doc')
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

        display_output = "[REF] " + " | ".join(display_parts)

        # 回傳 metadata 供上層判斷 REF 強度
        metadata = {
            "has_ref": len(merged_chunks) > 0,
            "top_score": top_score,
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
