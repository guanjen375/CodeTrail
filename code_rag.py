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

from config import (
    CODE_EXTENSIONS, IGNORED_DIRS, EMBEDDING_MODEL,
    CODE_RAG_ENABLED, CODE_RAG_TOP_K, CODE_RAG_CACHE_FILE,
    CODE_RAG_THRESHOLD, CODE_RAG_THRESHOLD_BUG
)
from utils import should_ignore_file


class CodeRAG:
    """
    專案級程式碼 RAG：
    - 動態建立程式碼索引（函式/類別級別）
    - 用於 Agent 模式的「第一層縮小範圍」
    """

    def __init__(self, folder: str):
        self.folder = Path(folder).resolve()
        self.cache_file = self.folder / CODE_RAG_CACHE_FILE
        self.index = []

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

        scores = []
        for item in self.index:
            emb = item.get('embedding', [])

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
