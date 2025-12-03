#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - Hybrid 版本 (優化版 v2)
優化項目：
1. 專用 Reranker 模型 (bge-reranker) 取代 LLM reranking
2. Query Expansion - 自動擴展搜尋關鍵字
3. 結構化 RAG 輸出 - 便於 LLM 引用
4. 專案級 Code RAG - 動態建立程式碼索引
5. [NEW] Code RAG 自動預讀 - 直接提供相關程式碼上下文
6. [NEW] run_command 工具 - 執行測試/建置命令驗證修正
"""

import sys
import io
import os

os.environ['LANG'] = 'en_US.UTF-8'
os.environ['LC_ALL'] = 'en_US.UTF-8'
os.environ['PYTHONIOENCODING'] = 'utf-8'

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

import re
import json
import base64
import fnmatch
import requests
import hashlib
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# ============================================================
# 設定
# ============================================================
OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3-coder:30b"
VL_MODEL = "qwen3-vl:30b-a3b"

NUM_CTX = 65536
MAX_TOTAL_CHARS = 200000  # 200KB，讓中小型專案使用完整模式

MAX_TOOL_LOOPS = 12
MAX_FILE_READ_CHARS = 50000
MAX_GREP_RESULTS = 30
MAX_LIST_DEPTH = 3

BUDGET_HIGH = 0.55
BUDGET_MID = 0.30
BUDGET_LOW = 0.15
SKELETON_THRESHOLD = 8000
SKELETON_MAX_LINES = 200

CODE_EXTENSIONS = {
    ".cpp", ".c", ".h", ".hpp", ".cc", ".cxx",
    ".py", ".pyx", ".pyi",
    ".json", ".yaml", ".yml", ".toml",
    ".sh", ".bash", ".mk", ".cmake",
    ".tcl", ".cfg", ".ini", ".conf",
    ".rs", ".go", ".java", ".kt",
    ".js", ".ts", ".jsx", ".tsx",
    ".txt", ".md",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

IGNORED_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".idea", ".vscode", "build", "dist", ".cache", ".tox",
    "eggs", "htmlcov", ".pytest_cache", ".mypy_cache",
    "third_party", "3rdparty", "external", "vendor",
}

IGNORED_FILES = {
    "license", "license.txt", "license.md", "copying",
    "changelog", "changelog.md", "changelog.txt",
    "authors", "contributors", "maintainers",
    "news", "history", "todo",
}

IGNORED_PATTERNS = [
    "*_test.cpp", "*_test.c", "*_test.py", "*_test.go",
    "test_*.py", "*_unittest.*", "*_mock.*", "*_stub.*",
    "*.bak", "*.orig", "*.swp", "*.tmp",
    "*.min.js", "*.min.css", "*.map",
]

# 知識庫設定
KNOWLEDGE_FILE = "knowledge.json"
KNOWLEDGE_TOP_K = 5
KNOWLEDGE_CANDIDATE_K = 30          # 增加候選數給 reranker
KNOWLEDGE_THRESHOLD = 0.15          # 絕對最低門檻
DYNAMIC_THRESHOLD_RATIO = 0.5       # 動態門檻 = max(固定, top_score * ratio)
KNOWLEDGE_INCLUDE_CONTENT = True
KNOWLEDGE_CONTENT_MAX_CHARS = 1200
KNOWLEDGE_MERGE_ADJACENT = True     # 合併同頁相鄰 chunk
KNOWLEDGE_MERGE_MAX_CHARS = 2500    # 合併後最大字元數
EMBEDDING_MODEL = "bge-m3"
RERANKER_MODEL = "qllama/bge-reranker-v2-m3"  # 專用 reranker 模型
USE_RERANKER = True
USE_HYBRID_SEARCH = True
USE_QUERY_EXPANSION = True          # 新增：Query Expansion
USE_MMR = True                      # 使用 MMR 去重演算法
MMR_LAMBDA = 0.7                    # MMR 相關性 vs 多樣性權重
KEYWORD_WEIGHT = 0.3

# Code RAG 設定
CODE_RAG_ENABLED = True             # 新增：專案級 Code RAG
CODE_RAG_TOP_K = 8
CODE_RAG_CACHE_FILE = ".code_rag_cache.json"
CODE_RAG_AUTO_PREREAD = True        # 新增：自動預讀 CodeRAG 結果
CODE_RAG_PREREAD_TOP_K = 5          # 預讀前 N 個候選（從 3 增加到 5）
CODE_RAG_PREREAD_LINES = 120        # 每個候選讀取的行數（從 80 增加到 120）
CODE_RAG_PREREAD_LINES_BUG = 160    # Bug 修復時讀取更多行

# 嚴格模式設定（自我檢查）
STRICT_MODE = True                  # 啟用嚴格模式
STRICT_MODE_KEYWORDS = [            # 觸發嚴格模式的關鍵字
    '依文件', '根據文件', '規格', '一定要', '保證正確',
    '根據 manual', '按照手冊', '依照規範', '依據說明',
    'spec', 'manual', 'specification', 'according to'
]

# Run Command 設定（測試/驗證工具）
RUN_COMMAND_ENABLED = True
RUN_COMMAND_TIMEOUT = 60            # 預設超時秒數
RUN_COMMAND_MAX_OUTPUT = 8000       # 最大輸出字元數
ALLOWED_COMMANDS = [
    # Python
    'pytest', 'python -m pytest', 'python -m unittest',
    # C/C++
    'make test', 'make check', 'ctest',
    # Node.js
    'npm test', 'npm run test', 'yarn test',
    # Rust
    'cargo test',
    # Go
    'go test',
    # 通用
    'make', 'cmake',
]


# ============================================================
# GPU 偵測
# ============================================================
def check_ollama_gpu() -> tuple[bool, str]:
    try:
        resp = requests.get("http://localhost:11434/api/tags", timeout=5)
        if resp.status_code != 200:
            return False, "[ERROR] Ollama 服務異常"
        
        resp = requests.get("http://localhost:11434/api/ps", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            models = data.get("models", [])
            
            if not models:
                return True, "[GPU] 待載入模型"
            
            for model in models:
                size_vram = model.get("size_vram", 0)
                size = model.get("size", 0)
                name = model.get("name", "?")
                
                if size_vram > 0:
                    vram_gb = size_vram / (1024**3)
                    gpu_percent = (size_vram / size * 100) if size > 0 else 100
                    if gpu_percent >= 99:
                        return True, f"[GPU] {name} 使用 {vram_gb:.1f}GB VRAM"
                    else:
                        return True, f"[GPU] {name} 使用 {vram_gb:.1f}GB VRAM ({gpu_percent:.0f}% GPU)"
            
            return True, "[GPU] OK"
        
        return True, "[GPU] 狀態查詢失敗"
        
    except requests.exceptions.ConnectionError:
        return False, "[ERROR] 無法連接 Ollama"
    except Exception as e:
        return False, f"[WARN] GPU 檢測失敗: {type(e).__name__}"


# ============================================================
# 知識庫 (RAG) - 優化版
# ============================================================
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
        self._reranker_available = None  # 快取 reranker 可用性

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
                # 檢查是否有 reranker 模型
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

    # ========== 新增：Query Expansion ==========
    def _expand_query(self, question: str) -> list[str]:
        """
        用 LLM 生成額外的搜尋關鍵字
        Returns: 原問題 + 擴展的關鍵字列表
        """
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
            
            # 解析關鍵字
            keywords = [kw.strip() for kw in result.split(',') if kw.strip()]
            keywords = keywords[:5]  # 最多 5 個
            
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

        # Query Expansion
        queries = self._expand_query(question)
        
        # 對每個 query 生成 embedding
        query_embeddings = []
        for q in queries:
            emb = self._get_embedding(q)
            if emb:
                query_embeddings.append(emb)
        
        if not query_embeddings:
            return []

        query_keywords = self._extract_keywords(question) if USE_HYBRID_SEARCH else set()

        # 計算分數（取多個 query embedding 的最高分）
        scores = []
        for chunk in self.chunks:
            emb = chunk.get("embedding", [])
            content = chunk.get("content", "")
            
            # 取最高的 embedding 相似度
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

    # ========== 改進：專用 Reranker 模型 ==========
    def _rerank_with_model(self, question: str, candidates: list, top_k: int) -> list:
        """
        使用專用 reranker 模型 (bge-reranker-v2-m3) 重排
        Fallback 到 LLM reranking
        """
        if not candidates:
            return []
        
        if not USE_RERANKER or len(candidates) <= top_k:
            return [c[3] for c in candidates[:top_k]]

        # 嘗試使用專用 reranker 模型
        if self._check_reranker_available():
            try:
                # bge-reranker 格式：給每個 (query, passage) pair 打分
                scored = []
                for score, _, _, chunk in candidates[:20]:  # 最多處理 20 個
                    content = chunk.get('content', '')[:800]
                    
                    # 使用 reranker 模型打分
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
                    
                    # 嘗試解析分數（reranker 通常輸出數字）
                    result = resp.json().get("response", "").strip()
                    try:
                        rerank_score = float(result)
                    except ValueError:
                        # 如果不是數字，嘗試提取
                        match = re.search(r'[\d.]+', result)
                        rerank_score = float(match.group()) if match else score
                    
                    scored.append((rerank_score, chunk))
                
                scored.sort(reverse=True, key=lambda x: x[0])
                return [c[1] for c in scored[:top_k]]
            
            except Exception:
                pass  # Fallback 到 LLM
        
        # Fallback: 用主模型做 reranking
        return self._rerank_with_llm(question, candidates, top_k)

    def _rerank_with_llm(self, question: str, candidates: list, top_k: int) -> list:
        """LLM Reranking (原有方法，作為 fallback)"""
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

    # ========== 新增：MMR 演算法 ==========
    def _mmr_select(self, chunks: list, question_emb: list, k: int, lambda_: float = MMR_LAMBDA) -> list:
        """
        Max Marginal Relevance 選擇：平衡相關性與多樣性
        避免選到太多重複內容的 chunk
        """
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
                    # 沒有 embedding 的直接用較低分數
                    mmr_score = -1
                else:
                    # 與問題的相關性
                    sim_q = self._cosine_similarity(question_emb, c_emb)

                    # 與已選 chunk 的最大相似度（重複度）
                    sim_rep = 0.0
                    if selected_embs:
                        sim_rep = max(self._cosine_similarity(c_emb, e) for e in selected_embs)

                    # MMR 分數
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

    # ========== 新增：合併相鄰 chunk ==========
    def _merge_adjacent_chunks(self, chunks: list) -> list:
        """
        合併同一頁的相鄰 chunk，避免關鍵資訊被切斷
        """
        if not chunks or not KNOWLEDGE_MERGE_ADJACENT:
            return chunks

        # 按 source, page, chunk_index 排序
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
                # 合併相鄰 chunk
                buffer["content"] += "\n" + c.get("content", "")
                buffer["last_idx"] = chunk_idx
                # 更新 section（取較新的）
                if c.get("section"):
                    buffer["section"] = c.get("section")
            else:
                # 輸出之前的 buffer，開始新的
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

    # ========== 改進：結構化輸出 ==========
    def query(self, question: str, top_k: int = KNOWLEDGE_TOP_K) -> tuple[str, str]:
        """
        查詢相關知識 - 結構化輸出版本
        改進：動態 threshold + MMR 去重 + 合併相鄰 chunk
        """
        if not self.loaded or not self.chunks:
            return "", ""

        candidates = self._hybrid_search(question, KNOWLEDGE_CANDIDATE_K)
        if not candidates:
            return "", ""

        # ========== 動態 threshold ==========
        top_score = candidates[0][0]
        min_score = max(KNOWLEDGE_THRESHOLD, top_score * DYNAMIC_THRESHOLD_RATIO)

        filtered = [(s, e, k, c) for s, e, k, c in candidates if s >= min_score]
        if not filtered:
            return "", ""

        # Rerank
        reranked_chunks = self._rerank_with_model(question, filtered, top_k * 2)  # 多取一些給 MMR
        if not reranked_chunks:
            return "", ""

        # ========== MMR 去重選擇 ==========
        if USE_MMR:
            q_emb = self._get_embedding(question)
            top_chunks = self._mmr_select(reranked_chunks, q_emb, top_k)
        else:
            top_chunks = reranked_chunks[:top_k]

        if not top_chunks:
            return "", ""

        # ========== 合併相鄰 chunk ==========
        merged_chunks = self._merge_adjacent_chunks(top_chunks)

        # 檢查是否有 spec 類型或 warning 類型的文件
        has_spec = any(chunk.get('type') == 'spec' for chunk in merged_chunks)
        has_warning = any(chunk.get('type') == 'warning' for chunk in merged_chunks)

        # ========== 結構化輸出格式 ==========
        model_lines = ["[REF] 相關知識參考（請在回答時引用 REF 編號）:"]

        for i, chunk in enumerate(merged_chunks, 1):
            source = chunk.get('source', '未知')
            page = chunk.get('page', '?')
            doc_type = chunk.get('type', 'doc')
            section = chunk.get('section', '')

            if KNOWLEDGE_INCLUDE_CONTENT:
                content = chunk.get('content', '')
                # 合併後的 chunk 可能較長，適當截斷
                max_chars = KNOWLEDGE_MERGE_MAX_CHARS if KNOWLEDGE_MERGE_ADJACENT else KNOWLEDGE_CONTENT_MAX_CHARS
                if len(content) > max_chars:
                    content = content[:max_chars] + "..."

                # 結構化格式（含 section）
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

        # 加入優先順序規則
        model_lines.append("\n回答規則:")
        model_lines.append("1. 請在回答中標註引用來源，如「根據 REF1...」")
        model_lines.append("2. 若文件未提及則說明「文件中未明確提到」")
        model_lines.append("3. 若你的常識與 [REF] 內容衝突，一律以 [REF] 為準")
        if has_spec:
            model_lines.append("4. 若 spec 與 guide/FAQ 衝突，以 type=spec 的 REF 為準")
        if has_warning:
            model_lines.append("5. type=warning 的內容請優先引用並強調限制條件")

        model_output = "\n".join(model_lines)

        # 終端機顯示
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

        return model_output, display_output

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


# ============================================================
# 專案級 Code RAG（新增功能）
# ============================================================
class CodeRAG:
    """
    專案級程式碼 RAG：
    - 動態建立程式碼索引（函式/類別級別）
    - 用於 Agent 模式的「第一層縮小範圍」
    """
    
    def __init__(self, folder: str):
        self.folder = Path(folder).resolve()
        self.cache_file = self.folder / CODE_RAG_CACHE_FILE
        self.index = []  # [{path, symbol, type, summary, start_line, end_line, embedding}]
        self._folder_hash = None
        
    def _compute_folder_hash(self) -> str:
        """計算資料夾的 hash（用於快取驗證）"""
        files = []
        for dirpath, dirnames, filenames in os.walk(self.folder):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith('.')]
            for f in filenames:
                if Path(f).suffix.lower() in CODE_EXTENSIONS:
                    fp = Path(dirpath) / f
                    try:
                        stat = fp.stat()
                        files.append(f"{fp.relative_to(self.folder)}:{stat.st_size}:{stat.st_mtime}")
                    except OSError:
                        pass
        files.sort()
        return hashlib.md5("\n".join(files).encode()).hexdigest()
    
    def _load_cache(self) -> bool:
        """嘗試載入快取"""
        if not self.cache_file.exists():
            return False
        
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            if data.get("folder_hash") != self._compute_folder_hash():
                return False
            
            self.index = data.get("index", [])
            return len(self.index) > 0
        except Exception:
            return False
    
    def _save_cache(self):
        """儲存快取"""
        try:
            data = {
                "folder_hash": self._compute_folder_hash(),
                "index": self.index
            }
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass
    
    def _extract_symbols(self, filepath: Path, content: str) -> list[dict]:
        """從程式碼中提取符號（函式、類別）"""
        symbols = []
        lines = content.split('\n')
        ext = filepath.suffix.lower()
        rel_path = str(filepath.relative_to(self.folder))
        
        # Python
        if ext in ('.py', '.pyx', '.pyi'):
            pattern = r'^(class|def|async def)\s+(\w+)'
            for i, line in enumerate(lines):
                m = re.match(pattern, line)
                if m:
                    sym_type = 'class' if m.group(1) == 'class' else 'function'
                    symbols.append({
                        'path': rel_path,
                        'symbol': m.group(2),
                        'type': sym_type,
                        'line': i + 1,
                        'context': '\n'.join(lines[max(0, i-2):min(len(lines), i+10)])
                    })
        
        # C/C++
        elif ext in ('.c', '.cpp', '.cc', '.cxx', '.h', '.hpp'):
            # 函式定義
            func_pattern = r'^[\w\s\*\&\<\>\[\]]+\s+(\w+)\s*\([^;]*\)\s*(const)?\s*\{'
            class_pattern = r'^(class|struct)\s+(\w+)'
            
            for i, line in enumerate(lines):
                m = re.match(class_pattern, line)
                if m:
                    symbols.append({
                        'path': rel_path,
                        'symbol': m.group(2),
                        'type': 'class',
                        'line': i + 1,
                        'context': '\n'.join(lines[max(0, i-2):min(len(lines), i+10)])
                    })
                    continue
                
                m = re.match(func_pattern, line)
                if m:
                    symbols.append({
                        'path': rel_path,
                        'symbol': m.group(1),
                        'type': 'function',
                        'line': i + 1,
                        'context': '\n'.join(lines[max(0, i-2):min(len(lines), i+10)])
                    })
        
        # Rust
        elif ext == '.rs':
            pattern = r'^(pub\s+)?(fn|struct|enum|impl)\s+(\w+)'
            for i, line in enumerate(lines):
                m = re.match(pattern, line)
                if m:
                    sym_type = {'fn': 'function', 'struct': 'class', 'enum': 'class', 'impl': 'impl'}[m.group(2)]
                    symbols.append({
                        'path': rel_path,
                        'symbol': m.group(3),
                        'type': sym_type,
                        'line': i + 1,
                        'context': '\n'.join(lines[max(0, i-2):min(len(lines), i+10)])
                    })
        
        # Go
        elif ext == '.go':
            pattern = r'^(func|type)\s+(\w+)'
            for i, line in enumerate(lines):
                m = re.match(pattern, line)
                if m:
                    sym_type = 'function' if m.group(1) == 'func' else 'class'
                    symbols.append({
                        'path': rel_path,
                        'symbol': m.group(2),
                        'type': sym_type,
                        'line': i + 1,
                        'context': '\n'.join(lines[max(0, i-2):min(len(lines), i+10)])
                    })
        
        # JavaScript/TypeScript
        elif ext in ('.js', '.ts', '.jsx', '.tsx'):
            patterns = [
                (r'^(class)\s+(\w+)', 'class'),
                (r'^(function|async function)\s+(\w+)', 'function'),
                (r'^(const|let|var)\s+(\w+)\s*=\s*(async\s+)?\(', 'function'),
                (r'^export\s+(class|function|const|let)\s+(\w+)', 'function'),
            ]
            for i, line in enumerate(lines):
                for pattern, sym_type in patterns:
                    m = re.match(pattern, line)
                    if m:
                        symbols.append({
                            'path': rel_path,
                            'symbol': m.group(2),
                            'type': sym_type,
                            'line': i + 1,
                            'context': '\n'.join(lines[max(0, i-2):min(len(lines), i+10)])
                        })
                        break
        
        return symbols
    
    def _get_embedding(self, text: str) -> list:
        """取得 embedding（複用 KnowledgeBase 的邏輯）"""
        try:
            resp = requests.post(
                "http://localhost:11434/api/embeddings",
                json={"model": EMBEDDING_MODEL, "prompt": text},
                timeout=60
            )
            resp.raise_for_status()
            return resp.json().get("embedding", [])
        except Exception:
            return []
    
    def build_index(self, verbose: bool = True):
        """建立程式碼索引"""
        if not CODE_RAG_ENABLED:
            return
        
        # 嘗試載入快取
        if self._load_cache():
            if verbose:
                print(f"[CODE_RAG] 載入快取: {len(self.index)} 個符號")
            return
        
        if verbose:
            print("[CODE_RAG] 建立程式碼索引...")
        
        self.index = []
        
        for dirpath, dirnames, filenames in os.walk(self.folder):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith('.')]
            
            for filename in filenames:
                if Path(filename).suffix.lower() not in CODE_EXTENSIONS:
                    continue
                if should_ignore_file(filename):
                    continue
                
                filepath = Path(dirpath) / filename
                try:
                    content = filepath.read_text(encoding='utf-8', errors='replace')
                    symbols = self._extract_symbols(filepath, content)
                    
                    for sym in symbols:
                        # 生成 embedding（用符號名 + context）
                        embed_text = f"{sym['symbol']} {sym['context'][:300]}"
                        emb = self._get_embedding(embed_text)
                        
                        self.index.append({
                            'path': sym['path'],
                            'symbol': sym['symbol'],
                            'type': sym['type'],
                            'line': sym['line'],
                            'context': sym['context'][:500],
                            'embedding': emb
                        })
                except Exception:
                    continue
        
        if verbose:
            print(f"[CODE_RAG] 索引完成: {len(self.index)} 個符號")
        
        # 儲存快取
        self._save_cache()
    
    def _extract_code_tokens(self, text: str) -> set:
        """從問題中提取可能是程式碼的 token（函式名、類別名等）"""
        # 匹配像是程式碼的識別符
        tokens = set(re.findall(r'[A-Za-z_][A-Za-z0-9_]{2,}', text))
        # 過濾常見的非程式碼詞
        stopwords = {'the', 'and', 'for', 'this', 'that', 'with', 'from', 'are', 'was',
                     'how', 'why', 'what', 'where', 'when', 'which', 'can', 'could',
                     'should', 'would', 'will', 'have', 'has', 'had', 'does', 'did',
                     'not', 'but', 'use', 'using', 'used', 'function', 'class', 'method',
                     '這個', '那個', '如何', '為什麼', '什麼', '怎麼'}
        return {t for t in tokens if t.lower() not in stopwords}

    def _token_match_score(self, code_tokens: set, item: dict) -> float:
        """計算字面匹配分數"""
        if not code_tokens:
            return 0.0
        text = (item.get("symbol", "") + " " + item.get("path", "")).lower()
        hits = sum(1 for t in code_tokens if t.lower() in text)
        return hits / len(code_tokens)

    def query(self, question: str, top_k: int = CODE_RAG_TOP_K) -> list[dict]:
        """
        查詢相關程式碼位置
        改進：加入字面匹配加權，更準確命中明確點名的 symbol
        """
        if not self.index:
            return []

        q_emb = self._get_embedding(question)
        if not q_emb:
            return []

        # 提取問題中的程式碼 token
        code_tokens = self._extract_code_tokens(question)

        # 計算相似度（embedding + 字面匹配混合）
        scores = []
        for item in self.index:
            emb = item.get('embedding', [])

            # Embedding 相似度
            if emb:
                sim = sum(a * b for a, b in zip(q_emb, emb))
                norm_q = sum(x*x for x in q_emb) ** 0.5
                norm_e = sum(x*x for x in emb) ** 0.5
                if norm_q > 0 and norm_e > 0:
                    emb_score = sim / (norm_q * norm_e)
                else:
                    emb_score = 0
            else:
                emb_score = 0

            # 字面匹配分數
            kw_score = self._token_match_score(code_tokens, item)

            # 混合分數（字面匹配給較高權重，因為明確點名通常很重要）
            combined = 0.6 * emb_score + 0.4 * kw_score

            scores.append((combined, emb_score, kw_score, item))

        scores.sort(reverse=True, key=lambda x: x[0])

        results = []
        for combined, emb_score, kw_score, item in scores[:top_k]:
            if combined > 0.15 or kw_score > 0.5:  # 字面完全匹配可以放寬門檻
                results.append({
                    'path': item['path'],
                    'symbol': item['symbol'],
                    'type': item['type'],
                    'line': item['line'],
                    'score': round(combined, 3)
                })

        return results
    
    def get_candidates_prompt(self, question: str) -> str:
        """生成給 Agent 的候選提示"""
        results = self.query(question)
        if not results:
            return ""
        
        lines = ["\n[CODE_RAG_CANDIDATES] 可能相關的程式碼位置:"]
        for r in results:
            lines.append(f"  - {r['path']}:{r['line']} {r['type']} {r['symbol']} (score: {r['score']})")
        lines.append("[/CODE_RAG_CANDIDATES]\n")
        lines.append("TIP: 可用 read_file 查看上述檔案的具體內容")
        
        return "\n".join(lines)


# ============================================================
# 共用工具函式（保持不變）
# ============================================================
def should_ignore_dir(path: Path) -> bool:
    for part in path.parts:
        if part.lower() in IGNORED_DIRS or part.startswith('.'):
            return True
    return False


def should_ignore_file(filepath: str) -> bool:
    name = Path(filepath).name.lower()
    stem = Path(filepath).stem.lower()
    
    if name in IGNORED_FILES or stem in IGNORED_FILES:
        return True
    
    for pattern in IGNORED_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return True
    
    return False


def get_priority(filepath: str) -> int:
    name = Path(filepath).name.lower()
    path_lower = filepath.lower()
    
    if name in ("main.cpp", "main.c", "main.py", "app.py", "index.py", "index.js"):
        return -10
    if name in ("__init__.py", "__main__.py"):
        return -5
    if "main" in name and any(name.endswith(ext) for ext in [".cpp", ".c", ".py"]):
        return -3
    if name in ("cmakelists.txt", "makefile", "setup.py", "pyproject.toml", "cargo.toml"):
        return -2
    
    if name.endswith((".h", ".hpp")):
        return 0 if any(x in path_lower for x in ["/include/", "/api/"]) else 1
    
    if name.endswith((".cpp", ".c", ".cc", ".py", ".rs", ".go")):
        return 1 if any(x in path_lower for x in ["/src/", "/lib/", "/core/"]) else 2
    
    if name.endswith((".json", ".yaml", ".yml", ".toml")):
        return 2 if "config" in name else 4
    
    if name.endswith((".mk", ".cmake", ".sh", ".tcl")):
        return 3
    
    if "readme" in name or name.endswith(".md"):
        return 8
    if name.endswith(".txt"):
        return 9
    
    return 5


def scan_project_metadata(folder: str) -> list[dict]:
    files = []
    folder_path = Path(folder).resolve()
    self_name = Path(sys.argv[0]).resolve().name
    
    for dirpath, dirnames, filenames in os.walk(folder_path):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith('.')]
        
        for filename in filenames:
            if filename == self_name:
                continue
            if filename.startswith('.'):
                continue
            
            filepath = Path(dirpath) / filename
            
            if filepath.is_symlink() and not filepath.exists():
                continue
            
            try:
                rel_path = filepath.relative_to(folder_path)
            except ValueError:
                continue
            
            rel_str = str(rel_path)
            if should_ignore_file(rel_str):
                continue
            
            if filepath.suffix.lower() not in CODE_EXTENSIONS:
                continue
            
            try:
                stat = filepath.stat()
                files.append({
                    "path": rel_str,
                    "size": stat.st_size,
                })
            except (OSError, FileNotFoundError):
                continue
    
    return files


def scan_project(folder: str) -> dict[str, str]:
    files = {}
    folder_path = Path(folder).resolve()
    self_name = Path(sys.argv[0]).resolve().name
    
    for dirpath, dirnames, filenames in os.walk(folder_path):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith('.')]
        
        for filename in filenames:
            if filename == self_name:
                continue
            if filename.startswith('.'):
                continue
            
            filepath = Path(dirpath) / filename
            
            if filepath.is_symlink() and not filepath.exists():
                continue
            
            try:
                rel_path = filepath.relative_to(folder_path)
            except ValueError:
                continue
            
            rel_str = str(rel_path)
            if should_ignore_file(rel_str):
                continue
            
            if filepath.suffix.lower() not in CODE_EXTENSIONS:
                continue
            
            try:
                content = filepath.read_text(encoding="utf-8", errors="replace")
                files[rel_str] = content
            except (OSError, FileNotFoundError):
                continue
    
    return files


def call_llm(prompt: str, temperature: float = 0.2) -> str:
    try:
        resp = requests.post(OLLAMA_GENERATE_URL, json={
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"num_ctx": NUM_CTX, "temperature": temperature},
        }, timeout=600)
        resp.raise_for_status()
        return resp.json().get("response", "")
    except requests.exceptions.ConnectionError:
        return "[ERROR] 無法連接 Ollama"
    except requests.exceptions.Timeout:
        return "[ERROR] 請求超時"
    except Exception as e:
        return f"[ERROR] 錯誤: {e}"


def should_use_strict_mode(question: str, knowledge_ctx: str) -> bool:
    """判斷是否應該啟用嚴格模式"""
    if not STRICT_MODE:
        return False
    if not knowledge_ctx:
        return False

    q_lower = question.lower()
    return any(kw.lower() in q_lower for kw in STRICT_MODE_KEYWORDS)


def answer_with_self_check(question: str, base_ctx: str, knowledge_ctx: str) -> str:
    """
    嚴格模式：兩階段回答 + 自我檢查
    1. 第一次：正常回答
    2. 第二次：自我檢查，刪除無根據的推測
    """
    print("[STRICT] 啟用嚴格模式 - 兩階段自我檢查")

    # 第一階段：正常回答
    first_prompt = f"""{base_ctx}
{knowledge_ctx}

使用上面的程式碼與 [REF] 參考資料回答問題：
{question}

請直接給出清楚的回答。"""

    print("   [1/2] 生成初稿...")
    draft = call_llm(first_prompt, temperature=0.1)

    if draft.startswith("[ERROR]"):
        return draft

    # 第二階段：自我檢查
    second_prompt = f"""{knowledge_ctx}

上面是你根據文件給出的初稿回答：

[draft]
{draft}
[/draft]

請做兩件事：
1. 逐段檢查 draft 中的敘述是否能在 [REF] 內容裡找到明確根據。
2. 刪除或改寫沒有明確根據的部分，改成「文件中沒寫清楚」或「根據文件無法確定，推測...」。

規則：
- 若某句話在 [REF] 中有明確對應，保留並標註 REF 編號
- 若某句話是合理推論但文件沒明說，改成「推測：...」
- 若某句話完全沒根據，直接刪除
- 不要解釋檢查過程，只輸出修正後的最終回答"""

    print("   [2/2] 自我檢查...")
    final = call_llm(second_prompt, temperature=0.0)

    return final.strip() if not final.startswith("[ERROR]") else draft


NATIVE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出目錄結構",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目錄路徑，預設 '.'"},
                    "depth": {"type": "integer", "description": "遞迴深度，預設 2"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "讀取檔案內容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "檔案路徑"},
                    "start_line": {"type": "integer", "description": "起始行號"},
                    "end_line": {"type": "integer", "description": "結束行號"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "搜尋 pattern",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "搜尋字串"},
                    "path": {"type": "string", "description": "搜尋目錄"},
                    "include": {"type": "string", "description": "檔案過濾"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_info",
            "description": "取得檔案資訊",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "檔案路徑"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "執行測試或建置命令（白名單：pytest, make test, npm test, cargo test, go test 等）",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要執行的命令，如 'pytest test_xxx.py -v'"},
                    "timeout": {"type": "integer", "description": "超時秒數，預設 60"}
                },
                "required": ["command"]
            }
        }
    }
]


def call_llm_with_tools(messages: list, temperature: float = 0.0) -> dict:
    try:
        resp = requests.post(OLLAMA_CHAT_URL, json={
            "model": MODEL,
            "messages": messages,
            "tools": NATIVE_TOOLS,
            "stream": False,
            "options": {"num_ctx": NUM_CTX, "temperature": temperature},
        }, timeout=600)
        resp.raise_for_status()
        data = resp.json()
        
        message = data.get("message", {})
        
        return {
            "content": message.get("content", ""),
            "tool_calls": message.get("tool_calls", []),
            "done_reason": data.get("done_reason", "stop")
        }
    except requests.exceptions.ConnectionError:
        return {"content": "[ERROR] 無法連接 Ollama", "tool_calls": [], "done_reason": "error"}
    except requests.exceptions.Timeout:
        return {"content": "[ERROR] 請求超時", "tool_calls": [], "done_reason": "error"}
    except Exception as e:
        return {"content": f"[ERROR] 錯誤: {e}", "tool_calls": [], "done_reason": "error"}


# ============================================================
# 完整模式 (保持不變)
# ============================================================
@dataclass
class FileEntry:
    path: str
    content: str
    original_lines: int
    included_lines: int
    is_skeleton: bool
    is_skipped: bool
    priority: int


@dataclass
class FullContext:
    files: list[FileEntry]
    context_str: str
    code_chars: int
    stats: dict = field(default_factory=dict)


def extract_skeleton(content: str, max_lines: int = SKELETON_MAX_LINES) -> tuple[str, int]:
    lines = content.split('\n')
    if len(lines) <= max_lines:
        return content, len(lines)
    
    skeleton = []
    skeleton.extend(lines[:25])
    
    for i, line in enumerate(lines[25:], 25):
        stripped = line.strip()
        keep = False
        
        if stripped.startswith(('#include', 'import ', 'from ', 'using ', '#define')):
            keep = True
        if re.match(r'^(class |struct |enum |typedef |namespace |def |async def |fn |func |pub fn |impl )', stripped):
            keep = True
        if re.match(r'^[\w\s\*\&\<\>\[\]]+\s+\w+\s*\([^;]*\)\s*(const)?\s*(\{|;|$)', stripped):
            keep = True
        if stripped.startswith(('/**', '///', '# TODO', '# NOTE')):
            keep = True
        
        if keep:
            skeleton.append(line)
    
    if len(skeleton) > max_lines:
        skeleton = skeleton[:max_lines]
    
    skeleton.append("// ... [skeleton]")
    return '\n'.join(skeleton), len(skeleton)


def build_full_context(files: dict[str, str]) -> FullContext:
    file_list = [(path, content, get_priority(path)) for path, content in files.items()]
    high = [(p, c, pr) for p, c, pr in file_list if pr < 2]
    mid = [(p, c, pr) for p, c, pr in file_list if 2 <= pr <= 5]
    low = [(p, c, pr) for p, c, pr in file_list if pr > 5]
    
    budget_high = int(MAX_TOTAL_CHARS * BUDGET_HIGH)
    budget_mid = int(MAX_TOTAL_CHARS * BUDGET_MID)
    budget_low = int(MAX_TOTAL_CHARS * BUDGET_LOW)
    
    entries = []
    total_chars = 0
    
    def process_group(group, budget, allow_skeleton):
        nonlocal total_chars
        used = 0
        group.sort(key=lambda x: (x[2], len(x[1])))
        
        for path, content, priority in group:
            lines = content.count('\n') + 1
            
            if used + len(content) <= budget:
                entries.append(FileEntry(path, content, lines, lines, False, False, priority))
                used += len(content)
                total_chars += len(content)
            elif allow_skeleton and len(content) > SKELETON_THRESHOLD:
                skeleton, sk_lines = extract_skeleton(content)
                if used + len(skeleton) <= budget:
                    entries.append(FileEntry(path, skeleton, lines, sk_lines, True, False, priority))
                    used += len(skeleton)
                    total_chars += len(skeleton)
                else:
                    entries.append(FileEntry(path, "", lines, 0, False, True, priority))
            else:
                entries.append(FileEntry(path, "", lines, 0, False, True, priority))
        
        return used
    
    used_high = process_group(high, budget_high, False)
    used_mid = process_group(mid, budget_mid, True)
    used_low = process_group(low, budget_low, True)
    
    remaining = MAX_TOTAL_CHARS - total_chars
    if remaining > 1000:
        skipped = [e for e in entries if e.is_skipped]
        skipped.sort(key=lambda e: (e.priority, e.original_lines))
        
        for entry in skipped:
            content = files.get(entry.path, "")
            if len(content) <= remaining:
                entry.content = content
                entry.included_lines = entry.original_lines
                entry.is_skipped = False
                remaining -= len(content)
                total_chars += len(content)
    
    entries.sort(key=lambda e: (e.is_skipped, e.priority, e.path))
    
    parts = []
    for e in entries:
        if not e.is_skipped:
            marker = " [skeleton]" if e.is_skeleton else ""
            parts.append(f"\n\n=== {e.path}{marker} ===\n{e.content}")
    
    included = [e for e in entries if not e.is_skipped]
    skipped = [e for e in entries if e.is_skipped]
    
    stats = {
        "total_files": len(entries),
        "included": len(included),
        "skipped": len(skipped),
        "skeleton": sum(1 for e in included if e.is_skeleton),
        "budget_high": budget_high, "used_high": used_high,
        "budget_mid": budget_mid, "used_mid": used_mid,
        "budget_low": budget_low, "used_low": used_low,
    }
    
    return FullContext(entries, "".join(parts), total_chars, stats)


def analyze_full(ctx: FullContext, question: str, image_ctx: str = "", knowledge_ctx: str = "") -> str:
    # 判斷是否需要創意性回答（重構、設計、架構）
    q_lower = question.lower() if question else ""
    is_creative = any(kw in q_lower for kw in ['refactor', '重構', '設計', '架構', 'design', 'architecture', '建議', 'suggest'])
    temperature = 0.2 if is_creative else 0.0

    # 建立基礎 context
    base_ctx = f"""你是程式碼審查專家。以下是專案的完整程式碼：
{ctx.context_str}
{image_ctx}"""

    if question:
        # 檢查是否啟用嚴格模式
        if should_use_strict_mode(question, knowledge_ctx):
            return answer_with_self_check(question, base_ctx, knowledge_ctx)

        prompt = f"""{base_ctx}
{knowledge_ctx}

用戶問題: {question}

回答規則：
1. 優先根據程式碼與 [REF] 內容回答。
2. 若文件/程式碼沒有給出明確資訊，直接說「程式碼/文件中沒有寫清楚」。
3. 不要憑常識或經驗補完沒有出現的條件。
4. 若需要做推測，一定要標示是推測。
5. 若有 [REF] 參考資料，請在回答中標註引用來源（如「根據 REF1...」）。

請用繁體中文詳細回答。"""
    else:
        prompt = f"""{base_ctx}
{knowledge_ctx}

請分析這個專案：
1. 整體架構和主要功能
2. 重要的類別/函式
3. 潛在問題或改進建議

用繁體中文回答。"""

    return call_llm(prompt, temperature=temperature)


def show_full_stats(ctx: FullContext):
    s = ctx.stats
    tokens = ctx.code_chars // 4
    
    print(f"\n[STAT] 完整模式統計:")
    print(f"   程式碼: {ctx.code_chars:,} / {MAX_TOTAL_CHARS:,} chars (~{tokens:,} tokens)")
    print(f"   檔案: {s['included']} 包含, {s['skipped']} 略過, {s['skeleton']} 骨架")


# ============================================================
# Agent 模式 - 整合 Code RAG
# ============================================================

class ToolExecutor:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
    
    def _safe_path(self, path: str) -> Optional[Path]:
        try:
            full = (self.root / path).resolve()
            full.relative_to(self.root)
            return full
        except ValueError:
            return None
    
    def list_files(self, path: str = ".", depth: int = 2) -> str:
        depth = min(depth, MAX_LIST_DEPTH)
        target = self._safe_path(path)
        
        if not target or not target.exists():
            return f"錯誤: 路徑不存在 '{path}'"
        if not target.is_dir():
            return f"錯誤: '{path}' 不是目錄"
        
        lines = []
        self._tree(target, "", depth, lines)
        return "\n".join(lines) if lines else f"目錄 '{path}' 是空的"
    
    def _tree(self, dir_path: Path, prefix: str, depth: int, lines: list):
        if depth < 0:
            return
        
        try:
            items = sorted(dir_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError:
            return
        
        valid_items = []
        for item in items:
            try:
                if item.is_symlink() and not item.exists():
                    continue
                if should_ignore_dir(item.relative_to(self.root)):
                    continue
                if item.name.startswith('.'):
                    continue
                valid_items.append(item)
            except (OSError, ValueError):
                continue
        
        for i, item in enumerate(valid_items):
            is_last = (i == len(valid_items) - 1)
            conn = "└── " if is_last else "├── "
            
            try:
                if item.is_dir():
                    lines.append(f"{prefix}{conn}[DIR] {item.name}/")
                    if depth > 0:
                        ext = "    " if is_last else "│   "
                        self._tree(item, prefix + ext, depth - 1, lines)
                else:
                    size = item.stat().st_size
                    sz = f"{size}B" if size < 1024 else f"{size/1024:.1f}KB"
                    lines.append(f"{prefix}{conn}[FILE] {item.name} ({sz})")
            except (OSError, FileNotFoundError):
                continue
    
    def read_file(self, path: str, start_line: int = 1, end_line: Optional[int] = None) -> str:
        target = self._safe_path(path)
        
        if not target or not target.exists():
            return f"錯誤: 檔案不存在 '{path}'"
        if not target.is_file():
            return f"錯誤: '{path}' 不是檔案"
        
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"錯誤: {e}"
        
        lines = content.split('\n')
        total = len(lines)
        
        start_line = max(1, start_line)
        if end_line is None:
            char_count = 0
            end_line = start_line
            for i in range(start_line - 1, total):
                char_count += len(lines[i]) + 1
                if char_count > MAX_FILE_READ_CHARS:
                    break
                end_line = i + 1
        else:
            end_line = min(end_line, total)
        
        selected = lines[start_line - 1:end_line]
        numbered = [f"{i:4d} | {line}" for i, line in enumerate(selected, start_line)]
        
        header = f"=== {path} (行 {start_line}-{end_line} / 共 {total} 行) ===\n"
        footer = f"\n... 用 read_file('{path}', {end_line + 1}) 繼續" if end_line < total else ""
        
        return header + "\n".join(numbered) + footer
    
    def grep(self, pattern: str, path: str = ".", include: str = "*") -> str:
        target = self._safe_path(path)
        if not target or not target.exists():
            return f"錯誤: 路徑不存在 '{path}'"
        
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(pattern), re.IGNORECASE)
        
        files = []
        if target.is_file():
            files = [target]
        else:
            for dirpath, dirnames, filenames in os.walk(target):
                dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith('.')]
                
                for fname in filenames:
                    if fnmatch.fnmatch(fname, include):
                        fp = Path(dirpath) / fname
                        if not should_ignore_file(fname):
                            files.append(fp)
        
        results = []
        for fp in files:
            if len(results) >= MAX_GREP_RESULTS:
                break
            try:
                content = fp.read_text(encoding="utf-8", errors="replace")
                for i, line in enumerate(content.split('\n'), 1):
                    if regex.search(line):
                        rel = fp.relative_to(self.root)
                        results.append(f"{rel}:{i}: {line.strip()[:100]}")
                        if len(results) >= MAX_GREP_RESULTS:
                            break
            except Exception:
                continue
        
        if not results:
            return f"沒有找到 '{pattern}'"
        
        return f"=== grep '{pattern}' ({len(results)} 結果) ===\n" + "\n".join(results)
    
    def file_info(self, path: str) -> str:
        target = self._safe_path(path)
        if not target or not target.exists():
            return f"錯誤: 不存在 '{path}'"
        
        if target.is_file():
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
                lines = content.count('\n') + 1
                chars = len(content)
            except Exception:
                lines, chars = "N/A", target.stat().st_size
            
            return f"{path}: 檔案, {lines} 行, {chars:,} 字元"
        else:
            count = sum(1 for _ in target.rglob("*") if _.is_file())
            return f"{path}: 目錄, {count} 個檔案"
    
    def run_command(self, command: str, timeout: int = RUN_COMMAND_TIMEOUT) -> str:
        """
        執行白名單內的測試/建置命令
        安全措施：只允許預定義的命令前綴
        """
        if not RUN_COMMAND_ENABLED:
            return "錯誤: run_command 功能已停用"
        
        command = command.strip()
        
        # 安全檢查：只允許白名單命令
        is_allowed = False
        for allowed in ALLOWED_COMMANDS:
            if command == allowed or command.startswith(allowed + ' '):
                is_allowed = True
                break
        
        if not is_allowed:
            allowed_list = ', '.join(ALLOWED_COMMANDS[:8])
            return f"錯誤: 不允許的命令。\n允許的命令前綴: {allowed_list}..."
        
        # 額外安全檢查：禁止危險字元
        dangerous_chars = [';', '&&', '||', '|', '`', '$(',  '>', '<', '\n']
        for char in dangerous_chars:
            if char in command:
                return f"錯誤: 命令包含不允許的字元 '{char}'"
        
        try:
            print(f"   [RUN] 執行: {command}")
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}
            )
            
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                if output:
                    output += "\n--- stderr ---\n"
                output += result.stderr
            
            # 截斷過長輸出
            if len(output) > RUN_COMMAND_MAX_OUTPUT:
                half = RUN_COMMAND_MAX_OUTPUT // 2
                output = (
                    output[:half] + 
                    f"\n\n... [截斷 {len(output) - RUN_COMMAND_MAX_OUTPUT} 字元] ...\n\n" + 
                    output[-half:]
                )
            
            status = "✓ 成功" if result.returncode == 0 else f"✗ 失敗 (exit {result.returncode})"
            return f"=== {status} ===\n{output}" if output else f"=== {status} (無輸出) ==="
            
        except subprocess.TimeoutExpired:
            return f"錯誤: 命令超時 ({timeout} 秒)"
        except Exception as e:
            return f"錯誤: {type(e).__name__}: {e}"
    
    def execute(self, tool: str, args: dict) -> Optional[str]:
        if tool == "list_files":
            return self.list_files(args.get("path", "."), args.get("depth", 2))
        elif tool == "read_file":
            return self.read_file(args.get("path", ""), args.get("start_line", 1), args.get("end_line"))
        elif tool == "grep":
            return self.grep(args.get("pattern", ""), args.get("path", "."), args.get("include", "*"))
        elif tool == "file_info":
            return self.file_info(args.get("path", ""))
        elif tool == "run_command":
            return self.run_command(args.get("command", ""), args.get("timeout", RUN_COMMAND_TIMEOUT))
        else:
            return f"錯誤: 未知工具 '{tool}'"


def handle_followup(question: str, prev_qa: list) -> str:
    prev_q, prev_a = prev_qa[-1]
    
    prompt = f"""你是程式碼分析助手。

【之前的對話】
用戶問：{prev_q}

你的回答：
{prev_a}

【用戶現在補充】
{question}

請根據之前的回答，直接給出針對這個補充條件的具體答案。
用繁體中文回答，簡潔明瞭。"""
    return call_llm(prompt)


# Stack trace 解析 patterns
STACK_TRACE_PATTERNS = [
    r'File "(.+?)", line (\d+)',           # Python
    r'(.+?):(\d+):(?:\d+:)?\s*error',      # gcc/clang
    r'(.+?):(\d+):(?:\d+:)?\s*warning',    # gcc/clang warning
    r'at (.+?):(\d+):',                    # JS/TS stack
    r'^\s+at .+?\((.+?):(\d+):\d+\)',      # Node.js stack
    r'(.+?)\.(?:cpp|c|h|py|rs|go|java):(\d+)',  # 一般檔案:行號
]


def extract_stack_locations(text: str) -> list[tuple[str, int]]:
    """從文字中提取 stack trace 的檔案位置"""
    locations = []
    for pattern in STACK_TRACE_PATTERNS:
        for m in re.finditer(pattern, text, re.MULTILINE):
            try:
                filepath = m.group(1)
                line_num = int(m.group(2))
                # 只保留看起來像專案內的檔案
                if not filepath.startswith('/usr') and not filepath.startswith('C:\\Windows'):
                    locations.append((filepath, line_num))
            except (ValueError, IndexError):
                continue
    return locations


def run_agent(folder: str, question: str, image_ctx: str = "", prev_qa: list = None,
              knowledge_ctx: str = "", code_rag: CodeRAG = None) -> str:
    """執行 Agent 模式 - 整合 Code RAG + 自動預讀 + run_command + stack trace 解析"""
    executor = ToolExecutor(folder)
    prev_qa = prev_qa or []

    # 提前判斷問題類型（用於後續調整行為）
    q_lower = question.lower()
    is_bug_fix = any(kw in q_lower for kw in ['bug', '錯誤', 'error', 'crash', 'fail', '修', 'fix', '問題', 'issue', '不work', '不能'])

    # ========== 新增：Stack trace 位置提取 ==========
    stack_locations = extract_stack_locations(question)
    stack_preread_context = ""

    if stack_locations:
        print(f"[STACK] 偵測到 {len(stack_locations)} 個 stack trace 位置")
        preread_lines = CODE_RAG_PREREAD_LINES_BUG
        stack_parts = []

        for filepath, line_num in stack_locations[:3]:  # 最多預讀 3 個
            # 嘗試找到相對路徑
            rel_path = filepath
            if os.path.isabs(filepath):
                try:
                    rel_path = str(Path(filepath).relative_to(folder))
                except ValueError:
                    rel_path = Path(filepath).name

            half_range = preread_lines // 2
            start = max(1, line_num - half_range)
            end = line_num + half_range

            content = executor.read_file(rel_path, start, end)
            if content and not content.startswith("錯誤"):
                stack_parts.append(f"[Stack trace 位置: {rel_path}:{line_num}]\n{content}")
                print(f"   [STACK_PREREAD] {rel_path}:{line_num} [{preread_lines} 行]")

        if stack_parts:
            stack_preread_context = "\n\n【Stack trace 相關程式碼 - 這些是錯誤發生的位置】:\n" + "\n\n".join(stack_parts)

    # 構建對話歷史
    history_context = ""
    if prev_qa:
        history_context = "\n\n【之前的對話】:\n"
        for i, (q, a) in enumerate(prev_qa[-2:], 1):
            history_context += f"Q{i}: {q}\n"
            a_short = a[:500] + "..." if len(a) > 500 else a
            history_context += f"A{i}: {a_short}\n\n"

    # ========== 改進：Code RAG 自動預讀 ==========
    code_rag_context = ""
    preread_files = set()
    # 已從 stack trace 預讀的檔案不重複
    for filepath, _ in stack_locations:
        preread_files.add(filepath)
        preread_files.add(Path(filepath).name)

    if code_rag and CODE_RAG_ENABLED:
        candidates = code_rag.query(question, top_k=CODE_RAG_TOP_K)

        if candidates:
            # 顯示候選列表
            print(f"[CODE_RAG] 找到 {len(candidates)} 個可能相關的程式碼位置")

            # 自動預讀 top-k 個候選
            if CODE_RAG_AUTO_PREREAD:
                # Bug 修復時使用更大的預讀範圍
                preread_lines = CODE_RAG_PREREAD_LINES_BUG if is_bug_fix else CODE_RAG_PREREAD_LINES

                preread_parts = []
                for c in candidates[:CODE_RAG_PREREAD_TOP_K]:
                    if c['path'] in preread_files:
                        continue

                    # 讀取符號附近的程式碼
                    center_line = c['line']
                    half_range = preread_lines // 2
                    start = max(1, center_line - half_range)
                    end = center_line + half_range

                    content = executor.read_file(c['path'], start, end)
                    if content and not content.startswith("錯誤"):
                        preread_parts.append(
                            f"[預讀: {c['path']} - {c['type']} {c['symbol']} (相關度: {c['score']})]\n{content}"
                        )
                        preread_files.add(c['path'])
                        print(f"   [PREREAD] {c['path']}:{c['line']} ({c['symbol']}) [{preread_lines} 行]")

                if preread_parts:
                    code_rag_context = "\n\n【Code RAG 自動預讀的相關程式碼 - 請優先根據這些內容分析】:\n" + "\n\n".join(preread_parts)

            # 同時提供其他候選的提示（未預讀的部分）
            other_candidates = [c for c in candidates if c['path'] not in preread_files]
            if other_candidates:
                hints = [f"  - {c['path']}:{c['line']} {c['type']} {c['symbol']}" for c in other_candidates[:5]]
                code_rag_context += "\n\n[其他可能相關位置]:\n" + "\n".join(hints)
    
    # ========== 改進：更新 system prompt ==========
    if is_bug_fix and RUN_COMMAND_ENABLED:
        task_hint = """
【Bug 修復模式 - 重要】
請務必嘗試以下步驟：
1. 先用 run_command 執行測試命令來重現問題（如 pytest, make test, npm test, cargo test, go test）
2. 分析測試輸出，找出具體的錯誤訊息和失敗點
3. 根據錯誤訊息，定位問題程式碼
4. 提出具體的修改建議
5. 如果修改後，建議再次執行測試驗證

若專案中存在測試檔案（test_*.py, *_test.cpp 等），請至少嘗試呼叫一次 run_command"""
    else:
        task_hint = ""
    
    run_cmd_hint = """
8. 可用 run_command 執行測試（如 pytest, make test）來驗證想法""" if RUN_COMMAND_ENABLED else ""

    # 判斷是否需要創意性回答
    is_creative = any(kw in q_lower for kw in ['refactor', '重構', '設計', '架構', 'design', 'architecture', '建議', 'suggest'])

    system_prompt = f"""你是程式碼分析 Agent。透過工具探索專案來回答用戶問題。

專案路徑: {folder}
{history_context}
{image_ctx}
{knowledge_ctx}
{stack_preread_context}
{code_rag_context}
{task_hint}

【回答規則 - 非常重要】
1. 優先根據程式碼與 [REF] 內容回答，不要憑常識或經驗補完沒有出現的條件
2. 若文件/程式碼沒有給出明確資訊，直接說「程式碼/文件中沒有寫清楚」
3. 若需要做推測，一定要明確標示「推測：...」
4. 若有 [REF] 參考資料，請在回答中標註引用來源（如「根據 REF1...」）
5. 若有「Code RAG 自動預讀的相關程式碼」或「Stack trace 相關程式碼」，分析時要優先根據這些預讀內容
6. 若你的常識與 [REF] 內容衝突，一律以 [REF] 為準

【工具使用規則】
7. 使用工具探索專案，找到回答問題所需的資訊
8. 不要重複呼叫相同的工具和參數
9. 收集到足夠資訊後，直接用文字回答，答案用繁體中文{run_cmd_hint}"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question}
    ]

    # 設定溫度：創意性問題用 0.2，其他用 0.0
    agent_temperature = 0.2 if is_creative else 0.0

    tool_history = []
    read_files_set = set(preread_files)  # 已預讀的檔案不重複讀
    has_run_command = False  # 追蹤是否有執行過測試命令
    bug_fix_reminder_sent = False  # 追蹤是否已發送測試提醒

    for i in range(MAX_TOOL_LOOPS):
        print(f"[LOOP] Agent 第 {i+1} 輪...")

        response = call_llm_with_tools(messages, temperature=agent_temperature)
        
        if response["done_reason"] == "error":
            return response["content"]
        
        tool_calls = response.get("tool_calls", [])
        
        if not tool_calls:
            content = response.get("content", "")
            if content and len(content) > 50:
                # Bug 修復模式：檢查是否應該先執行測試
                if is_bug_fix and RUN_COMMAND_ENABLED and not has_run_command and not bug_fix_reminder_sent:
                    print(f"   [NOTE] Bug 修復模式：尚未執行測試，發送提醒...")
                    bug_fix_reminder_sent = True
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": "在最終回答前，請先用 run_command 執行適當的測試命令（如 pytest、make test）來驗證你的分析是否正確，或重現問題。如果專案沒有測試或你確定不需要測試，請直接給出最終回答。"
                    })
                    continue

                # 嚴格模式：對有 knowledge_ctx 且命中關鍵字的回答進行自我檢查
                if should_use_strict_mode(question, knowledge_ctx):
                    print(f"   [STRICT] Agent 啟用嚴格模式自我檢查...")
                    # 構建簡化的 base context（不含工具說明）
                    base_ctx = f"專案路徑: {folder}\n{code_rag_context}\n{stack_preread_context}"
                    content = answer_with_self_check(question, base_ctx, knowledge_ctx)

                print(f"   [OK] Agent 完成分析")
                return content
            else:
                messages.append({"role": "assistant", "content": content or "..."})
                messages.append({"role": "user", "content": "請繼續探索或直接回答問題。"})
                continue
        
        for tool_call in tool_calls:
            func = tool_call.get("function", {})
            tool_name = func.get("name", "")
            
            args_raw = func.get("arguments", {})
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {}
            else:
                args = args_raw
            
            print(f"   [TOOL] {tool_name}({args})")

            # 追蹤是否使用了 run_command
            if tool_name == "run_command":
                has_run_command = True

            call_key = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
            if call_key in tool_history:
                print(f"   [WARN] 跳過重複呼叫")
                result = f"已經呼叫過，請用其他工具或直接回答"
            else:
                tool_history.append(call_key)
                result = executor.execute(tool_name, args)
                
                if tool_name == "read_file" and result:
                    line_match = re.search(r'行 (\d+)-(\d+) / 共 (\d+) 行', result)
                    if line_match:
                        start, end, total = map(int, line_match.groups())
                        if start == 1 and end >= total:
                            read_files_set.add(args.get("path", ""))
            
            preview = result[:150] + "..." if result and len(result) > 150 else result
            print(f"   [RESULT] {preview}")
            
            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [tool_call]
            })
            messages.append({
                "role": "tool",
                "tool_name": tool_name,
                "content": result or "（無結果）"
            })
    
    print("[WARN] 達到最大探索次數")

    summary_prompt = f"""請根據目前收集到的資訊，盡可能回答用戶的問題。
如果資訊不足，請說明你已經知道什麼，還缺少什麼。"""

    messages.append({"role": "user", "content": summary_prompt})
    final_response = call_llm_with_tools(messages, temperature=agent_temperature)
    
    if final_response.get("content"):
        return f"[NOTE] 根據已收集資訊回答：\n\n{final_response['content']}"
    
    return "[WARN] 達到最大探索次數，請嘗試更具體的問題。"


# ============================================================
# OCR
# ============================================================
def ocr_image(path: str) -> str:
    p = Path(path).expanduser().resolve()
    
    if not p.exists():
        return f"[OCR 錯誤] 檔案不存在: {path}"
    
    if p.suffix.lower() not in IMAGE_EXTENSIONS:
        return f"[OCR 錯誤] 不支援的格式: {p.suffix}"
    
    file_size = p.stat().st_size
    if file_size > 20 * 1024 * 1024:
        return f"[OCR 錯誤] 圖片過大: {file_size / 1024 / 1024:.1f}MB"
    
    try:
        with open(p, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        
        resp = requests.post(OLLAMA_GENERATE_URL, json={
            "model": VL_MODEL,
            "prompt": "列出圖片中的所有文字，保持格式。",
            "images": [data],
            "stream": False,
            "options": {"num_ctx": 4096, "temperature": 0.1},
        }, timeout=120)
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        return f"[OCR 錯誤] {type(e).__name__}: {e}"


def process_images(text: str) -> tuple[str, str]:
    pattern = r'img:([^\s]+\.(?:png|jpg|jpeg|gif|webp))'
    matches = re.findall(pattern, text, re.IGNORECASE)
    clean = re.sub(pattern, '', text, flags=re.IGNORECASE).strip()
    
    if not matches:
        return text, ""
    
    ctx = "\n附加圖片:\n"
    for m in matches:
        print(f"[IMG] OCR: {m}")
        ctx += f"\n[{m}]:\n{ocr_image(m)}\n"
    
    return clean, ctx


# ============================================================
# 主程式
# ============================================================
def main():
    args = sys.argv[1:]
    folder = "."
    question = None
    force_mode = None
    kb_path = KNOWLEDGE_FILE
    extra_excludes = []
    include_dirs = []
    
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--agent":
            force_mode = "agent"
        elif arg == "--full":
            force_mode = "full"
        elif arg == "--kb" and i + 1 < len(args):
            kb_path = args[i + 1]
            i += 1
        elif arg.startswith("--kb="):
            kb_path = arg.split("=", 1)[1]
        elif arg == "--exclude" and i + 1 < len(args):
            extra_excludes.append(args[i + 1])
            i += 1
        elif arg.startswith("--exclude="):
            extra_excludes.append(arg.split("=", 1)[1])
        elif arg == "--include-dir" and i + 1 < len(args):
            include_dirs.append(args[i + 1])
            i += 1
        elif arg.startswith("--include-dir="):
            include_dirs.append(arg.split("=", 1)[1])
        elif arg.startswith("-"):
            pass
        elif folder == ".":
            folder = arg
        else:
            question = arg
        i += 1
    
    if include_dirs:
        for d in include_dirs:
            IGNORED_DIRS.discard(d)
            print(f"[CFG] 包含目錄: {d}")
    
    if extra_excludes:
        for p in extra_excludes:
            IGNORED_PATTERNS.append(p)
            print(f"[CFG] 排除: {p}")
    
    if not os.path.isdir(folder):
        print(f"[ERROR] 資料夾不存在: {folder}")
        sys.exit(1)
    
    folder = str(Path(folder).resolve())
    
    print(f"[DIR] 掃描: {folder}")
    file_metadata = scan_project_metadata(folder)
    
    if not file_metadata:
        print("[ERROR] 沒有找到程式碼檔案")
        sys.exit(1)
    
    total_size = sum(f["size"] for f in file_metadata)
    file_count = len(file_metadata)
    
    print(f"[FILE] 找到 {file_count} 個檔案 (~{total_size:,} bytes)")
    
    # 載入知識庫
    kb = KnowledgeBase(kb_path)
    print(kb.get_status())
    
    # 決定模式
    if force_mode == "agent":
        mode = "agent"
    elif force_mode == "full":
        mode = "full"
    elif total_size <= MAX_TOTAL_CHARS:
        mode = "full"
    else:
        mode = "agent"
    
    # 檢查 GPU
    gpu_ok, gpu_status = check_ollama_gpu()
    print(f"\n[AI] 模型: {MODEL}")
    print(f"[CTX] Context: {NUM_CTX:,} tokens")
    print(gpu_status)
    
    # 初始化 Code RAG（Agent 模式）
    code_rag = None
    if mode == "agent" and CODE_RAG_ENABLED:
        code_rag = CodeRAG(folder)
        code_rag.build_index()
    
    # 準備 context
    ctx = None
    if mode == "full":
        print(f"[OK] 使用【完整模式】")
        files = scan_project(folder)
        actual_size = sum(len(c) for c in files.values())
        print(f"   實際大小: {actual_size:,} chars")
        ctx = build_full_context(files)
        show_full_stats(ctx)
    else:
        print(f"🔍 使用【Agent 模式】- 動態探索")
    
    print("-" * 50)
    
    # 單次模式
    if question:
        clean_q, img_ctx = process_images(question)
        print("[KB] 查詢知識庫...")
        knowledge_ctx, knowledge_display = kb.query(clean_q) if kb.loaded else ("", "")
        if knowledge_display:
            print(knowledge_display)
        print(f"\n⏳ 分析中...\n")
        if mode == "full":
            result = analyze_full(ctx, clean_q, img_ctx, knowledge_ctx)
        else:
            result = run_agent(folder, clean_q, img_ctx, knowledge_ctx=knowledge_ctx, code_rag=code_rag)
        
        print("\n" + "=" * 50)
        print("[NOTE] 回答:\n")
        print(result)
        return
    
    # 互動模式
    qa_history = []
    
    while True:
        try:
            print(f"\n💬 輸入問題 (Enter=整體分析, q=離開, clear=清除歷史)")
            q = input(">>> ").strip()
            
            if q.lower() in ('q', 'quit', 'exit'):
                print("[BYE] 再見!")
                break
            
            if q.lower() == 'clear':
                qa_history.clear()
                print("[DEL] 對話歷史已清除")
                continue
            
            if not q:
                q = "請分析這個專案的整體架構和主要功能"
            
            clean_q, img_ctx = process_images(q)

            # 構建 RAG 查詢（帶上一輪問題，幫助追問理解 context）
            if qa_history and kb.loaded:
                last_q, _ = qa_history[-1]
                rag_query = f"前一題：{last_q}\n使用者追問：{clean_q}"
            else:
                rag_query = clean_q

            if kb.loaded:
                print("[KB] 查詢知識庫...")
            knowledge_ctx, knowledge_display = kb.query(rag_query) if kb.loaded else ("", "")
            if knowledge_display:
                print(knowledge_display)
            
            if not qa_history:
                print(f"\n⏳ 分析中...（首次需載入模型）\n")
            else:
                print(f"\n⏳ 分析中...\n")
            
            # 追問偵測
            followup_patterns = ['我是', '我用的是', '我選', '改成', '換成', 
                                '那這樣', '那如果', '所以是', '所以要']
            short_answer_patterns = ['a53', 'a7', 'a55', 'cortex', 'arm']
            
            q_lower = clean_q.lower()
            is_followup = (
                len(clean_q) < 30 and 
                qa_history and 
                (
                    any(kw in q_lower for kw in followup_patterns) or
                    (len(clean_q) < 15 and any(kw in q_lower for kw in short_answer_patterns))
                )
            )
            
            if mode == "full":
                result = analyze_full(ctx, clean_q, img_ctx, knowledge_ctx)
            elif is_followup:
                print("[TIP] 偵測到追問\n")
                result = handle_followup(clean_q, qa_history)
            else:
                result = run_agent(folder, clean_q, img_ctx, prev_qa=qa_history, 
                                  knowledge_ctx=knowledge_ctx, code_rag=code_rag)
            
            qa_history.append((clean_q, result))
            if len(qa_history) > 5:
                qa_history.pop(0)
            
            print("\n" + "=" * 50)
            print("[NOTE] 回答:\n")
            print(result)
        
        except KeyboardInterrupt:
            print("\n[BYE] 再見!")
            break


if __name__ == "__main__":
    main()