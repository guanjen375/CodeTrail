#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - Code RAG (程式碼索引)
"""

import os
import re
import json
import hashlib
import requests
from pathlib import Path

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

from config import (
    CODE_EXTENSIONS, IGNORED_DIRS, EMBEDDING_MODEL,
    CODE_RAG_ENABLED, CODE_RAG_TOP_K, CODE_RAG_CACHE_FILE,
    CODE_RAG_THRESHOLD, CODE_RAG_THRESHOLD_BUG,
    OLLAMA_EMBEDDINGS_URL
)
from utils import should_ignore_file


class CodeRAG:
    """
    專案級程式碼 RAG：
    - 動態建立程式碼索引（函式/類別級別）
    - 用於 Agent 模式的「第一層縮小範圍」

    快取優化：
    - 使用 numpy .npz 二進位格式儲存 embedding 向量（壓縮率高）
    - metadata (path, symbol, type, line, context) 單獨存 JSON
    - 快取檔案大小降低約 5-10 倍
    """

    def __init__(self, folder: str):
        self.folder = Path(folder).resolve()
        # 新的快取檔案：metadata 用 JSON，embedding 用 npz
        cache_base = CODE_RAG_CACHE_FILE.replace('.json', '')
        self.cache_meta_file = self.folder / f"{cache_base}_meta.json"
        self.cache_emb_file = self.folder / f"{cache_base}_emb.npz"
        # 舊版快取檔案（用於向後相容）
        self.legacy_cache_file = self.folder / CODE_RAG_CACHE_FILE
        self.index = []
        self.embeddings = None  # numpy array, shape: (N, embedding_dim)

    def _compute_folder_hash(self) -> str:
        """計算資料夾的 hash（用於快取驗證）"""
        # 排除快取檔案本身，避免快取寫入後導致 hash 變化
        cache_base = CODE_RAG_CACHE_FILE.replace('.json', '')
        skip_files = {
            CODE_RAG_CACHE_FILE,
            f"{cache_base}_meta.json",
            f"{cache_base}_emb.npz",
        }

        files = []
        for dirpath, dirnames, filenames in os.walk(self.folder):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith('.')]
            for f in filenames:
                # 跳過 dotfile 和快取檔案
                if f.startswith('.') or f in skip_files:
                    continue
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
        """嘗試載入快取（優先新格式，向後相容舊格式）"""
        folder_hash = self._compute_folder_hash()

        # 嘗試載入新格式快取（metadata JSON + embedding npz）
        if self.cache_meta_file.exists() and self.cache_emb_file.exists() and HAS_NUMPY:
            try:
                with open(self.cache_meta_file, 'r', encoding='utf-8') as f:
                    meta = json.load(f)

                if meta.get("folder_hash") != folder_hash:
                    return False

                # 檢查 embedding model 是否一致，不一致則需重建
                if meta.get("embedding_model") != EMBEDDING_MODEL:
                    return False

                self.index = meta.get("index", [])
                emb_data = np.load(self.cache_emb_file)
                self.embeddings = emb_data['embeddings']

                # 驗證 embedding 維度與快取記錄一致
                cached_dim = meta.get("embedding_dim")
                if cached_dim is not None and self.embeddings.shape[1] != cached_dim:
                    return False

                if len(self.index) > 0 and self.embeddings is not None:
                    return True
            except Exception:
                pass

        # 向後相容：嘗試載入舊格式快取
        if self.legacy_cache_file.exists():
            try:
                with open(self.legacy_cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                if data.get("folder_hash") != folder_hash:
                    return False

                old_index = data.get("index", [])
                if not old_index:
                    return False

                # 從舊格式遷移：分離 embedding 到 numpy array
                self.index = []
                embeddings_list = []
                for item in old_index:
                    emb = item.pop('embedding', [])
                    self.index.append(item)
                    embeddings_list.append(emb if emb else [0.0] * 1024)  # 預設 1024 維

                if HAS_NUMPY:
                    self.embeddings = np.array(embeddings_list, dtype=np.float32)
                    # 遷移後自動保存新格式並刪除舊檔案
                    self._save_cache()
                    try:
                        self.legacy_cache_file.unlink()
                    except Exception:
                        pass
                else:
                    # 沒有 numpy，把 embedding 塞回 index
                    for i, emb in enumerate(embeddings_list):
                        self.index[i]['embedding'] = emb

                return len(self.index) > 0
            except Exception:
                return False

        return False

    def _save_cache(self):
        """儲存快取（新格式：metadata JSON + embedding npz）"""
        try:
            # 準備 metadata（不含 embedding），包含 embedding model 與維度資訊
            emb_dim = self.embeddings.shape[1] if HAS_NUMPY and self.embeddings is not None else None
            meta = {
                "folder_hash": self._compute_folder_hash(),
                "embedding_model": EMBEDDING_MODEL,
                "embedding_dim": emb_dim,
                "index": self.index
            }
            with open(self.cache_meta_file, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False)

            # 儲存 embedding 為 npz（壓縮的 numpy 二進位格式）
            if HAS_NUMPY and self.embeddings is not None:
                np.savez_compressed(self.cache_emb_file, embeddings=self.embeddings)
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
        """取得 embedding"""
        try:
            resp = requests.post(
                OLLAMA_EMBEDDINGS_URL,
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

        if self._load_cache():
            if verbose:
                print(f"[CODE_RAG] 載入快取: {len(self.index)} 個符號")
            return

        if verbose:
            print("[CODE_RAG] 建立程式碼索引...")

        self.index = []
        embeddings_list = []

        for dirpath, dirnames, filenames in os.walk(self.folder):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith('.')]

            for filename in filenames:
                # 跳過 dotfile（包含快取檔案）
                if filename.startswith('.'):
                    continue
                if Path(filename).suffix.lower() not in CODE_EXTENSIONS:
                    continue
                if should_ignore_file(filename):
                    continue

                filepath = Path(dirpath) / filename
                try:
                    content = filepath.read_text(encoding='utf-8', errors='replace')
                    symbols = self._extract_symbols(filepath, content)

                    for sym in symbols:
                        embed_text = f"{sym['symbol']} {sym['context'][:300]}"
                        emb = self._get_embedding(embed_text)

                        # metadata 不含 embedding
                        self.index.append({
                            'path': sym['path'],
                            'symbol': sym['symbol'],
                            'type': sym['type'],
                            'line': sym['line'],
                            'context': sym['context'][:500],
                        })
                        # embedding 單獨收集
                        embeddings_list.append(emb if emb else [])
                except Exception:
                    continue

        # 將 embedding 轉換為 numpy array
        if HAS_NUMPY and embeddings_list:
            # 確保所有 embedding 維度一致
            emb_dim = max(len(e) for e in embeddings_list) if embeddings_list else 1024
            normalized = []
            for emb in embeddings_list:
                if len(emb) == emb_dim:
                    normalized.append(emb)
                elif len(emb) == 0:
                    normalized.append([0.0] * emb_dim)
                else:
                    # 維度不一致，填充或截斷
                    normalized.append((emb + [0.0] * emb_dim)[:emb_dim])
            self.embeddings = np.array(normalized, dtype=np.float32)
        else:
            self.embeddings = None
            # 沒有 numpy 時，退回舊方式（embedding 存在 index 裡）
            for i, emb in enumerate(embeddings_list):
                self.index[i]['embedding'] = emb

        if verbose:
            print(f"[CODE_RAG] 索引完成: {len(self.index)} 個符號")

        self._save_cache()

    def _extract_code_tokens(self, text: str) -> set:
        """從問題中提取可能是程式碼的 token"""
        tokens = set(re.findall(r'[A-Za-z_][A-Za-z0-9_]{2,}', text))
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

    def _get_embedding_at(self, idx: int) -> list:
        """取得指定索引的 embedding（相容新舊格式）"""
        # 新格式：從 numpy array 取得
        if HAS_NUMPY and self.embeddings is not None and idx < len(self.embeddings):
            return self.embeddings[idx].tolist()
        # 舊格式：從 index 取得
        if idx < len(self.index):
            return self.index[idx].get('embedding', [])
        return []

    def query(self, question: str, top_k: int = CODE_RAG_TOP_K, is_bug_fix: bool = False) -> list[dict]:
        """查詢相關程式碼位置（動態門檻）"""
        if not self.index:
            return []

        q_emb = self._get_embedding(question)
        if not q_emb:
            return []

        code_tokens = self._extract_code_tokens(question)

        # 動態門檻：Bug 類問題稍微放寬
        threshold = CODE_RAG_THRESHOLD_BUG if is_bug_fix else CODE_RAG_THRESHOLD

        # 使用 numpy 向量化計算 cosine similarity（如果可用）
        if HAS_NUMPY and self.embeddings is not None and len(self.embeddings) > 0:
            q_vec = np.array(q_emb, dtype=np.float32)
            # 計算所有 cosine similarity
            dot_products = np.dot(self.embeddings, q_vec)
            norms = np.linalg.norm(self.embeddings, axis=1) * np.linalg.norm(q_vec)
            # 避免除零
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

            kw_score = self._token_match_score(code_tokens, item)

            # 明確點名 symbol 時，大幅提高權重
            # 如果 keyword 幾乎完全匹配（>0.8），認定為「明確點名」
            if kw_score >= 0.8:
                # 明確點名：直接給很高分，即使 embedding 不太像
                combined = 0.9 + kw_score * 0.1
            else:
                # 一般情況：function 類型給一點優先權
                type_bonus = 0.05 if item.get('type') == 'function' else 0
                combined = 0.5 * emb_score + 0.5 * kw_score + type_bonus

            scores.append((combined, emb_score, kw_score, item))

        scores.sort(reverse=True, key=lambda x: x[0])

        results = []
        for combined, emb_score, kw_score, item in scores[:top_k]:
            # 使用動態門檻，或字面匹配高可以放寬
            if combined >= threshold or kw_score >= 0.5:
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
