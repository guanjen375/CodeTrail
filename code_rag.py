#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - Code RAG (程式碼索引)
"""

import os
import re
import sys
import json
import hashlib
from pathlib import Path
from functools import lru_cache

from http_client import get_session

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

from config import (
    CODE_EXTENSIONS, EMBEDDING_MODEL,
    CODE_RAG_ENABLED, CODE_RAG_TOP_K, CODE_RAG_CACHE_FILE,
    CODE_RAG_THRESHOLD, CODE_RAG_THRESHOLD_BUG,
    OLLAMA_EMBEDDINGS_URL
)
from utils import should_ignore_file, should_ignore_dir


def _normalize_text_for_cache(text: str) -> str:
    """正規化文字以提高 cache 命中率"""
    return ' '.join(text.split())


@lru_cache(maxsize=256)
def _cached_get_embedding(text: str) -> tuple:
    """帶 LRU cache 的 embedding 查詢（CodeRAG 用）"""
    try:
        session = get_session()
        resp = session.post(
            OLLAMA_EMBEDDINGS_URL,
            json={"model": EMBEDDING_MODEL, "prompt": text},
            timeout=60
        )
        resp.raise_for_status()
        emb = resp.json().get("embedding", [])
        return tuple(emb) if emb else ()
    except Exception:
        return ()


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
        folder_path = Path(self.folder)
        for dirpath, dirnames, filenames in os.walk(self.folder):
            # 使用統一的目錄過濾邏輯
            rel_dir = Path(dirpath).relative_to(folder_path)
            dirnames[:] = [d for d in dirnames if not should_ignore_dir(rel_dir / d)]
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
        """從程式碼中提取符號（函式、類別）

        改進：
        - Python：支援縮排的 class method、decorator
        - JS/TS：支援 arrow function、object method
        - C/C++：支援 template、namespace、複雜返回型別
        """
        symbols = []
        lines = content.split('\n')
        ext = filepath.suffix.lower()
        rel_path = str(filepath.relative_to(self.folder))

        def add_symbol(name: str, sym_type: str, line_num: int):
            symbols.append({
                'path': rel_path,
                'symbol': name,
                'type': sym_type,
                'line': line_num,
                'context': '\n'.join(lines[max(0, line_num-3):min(len(lines), line_num+10)])
            })

        # Python：支援縮排的 method 和 decorator
        if ext in ('.py', '.pyx', '.pyi'):
            # 支援縮排（前導空白）
            pattern = r'^(\s*)(class|def|async\s+def)\s+(\w+)'
            pending_decorator = None
            for i, line in enumerate(lines):
                # 記住 decorator，下一個 def/class 會使用它
                if re.match(r'^\s*@\w+', line):
                    pending_decorator = i
                    continue

                m = re.match(pattern, line)
                if m:
                    sym_type = 'class' if m.group(2) == 'class' else 'function'
                    # 如果有 decorator，從 decorator 開始
                    start_line = pending_decorator if pending_decorator is not None else i
                    add_symbol(m.group(3), sym_type, start_line + 1)
                    pending_decorator = None

        # C/C++：支援 template、namespace、複雜返回型別（含 ::, <, >, ,）
        elif ext in ('.c', '.cpp', '.cc', '.cxx', '.h', '.hpp'):
            # class/struct（含 template）
            class_pattern = r'^(?:template\s*<[^>]*>\s*)?(class|struct)\s+(\w+)'
            # namespace
            namespace_pattern = r'^namespace\s+(\w+)'
            # 函式：放寬型別字元集（加入 :, ,，容忍 template）
            # 支援：std::vector<int> ns::func(...) { 或 void func(...) const {
            func_pattern = r'^(?:template\s*<[^>]*>\s*)?[\w\s\*\&\<\>\[\]:,]+\s+(?:(\w+)::)?(\w+)\s*\([^;]*\)\s*(?:const|override|noexcept|final|\s)*\{'

            for i, line in enumerate(lines):
                # namespace
                m = re.match(namespace_pattern, line)
                if m:
                    add_symbol(m.group(1), 'namespace', i + 1)
                    continue

                # class/struct
                m = re.match(class_pattern, line)
                if m:
                    add_symbol(m.group(2), 'class', i + 1)
                    continue

                # 函式
                m = re.match(func_pattern, line)
                if m:
                    # group(1) = namespace/class prefix（可選）
                    # group(2) = 函式名
                    func_name = m.group(2)
                    if m.group(1):  # 有 namespace::prefix
                        func_name = f"{m.group(1)}::{func_name}"
                    add_symbol(func_name, 'function', i + 1)

        # Rust
        elif ext == '.rs':
            pattern = r'^(\s*)(pub\s+)?(fn|struct|enum|impl|trait|mod)\s+(\w+)'
            for i, line in enumerate(lines):
                m = re.match(pattern, line)
                if m:
                    keyword = m.group(3)
                    sym_type = {
                        'fn': 'function', 'struct': 'class', 'enum': 'class',
                        'impl': 'impl', 'trait': 'trait', 'mod': 'module'
                    }.get(keyword, 'function')
                    add_symbol(m.group(4), sym_type, i + 1)

        # Go
        elif ext == '.go':
            # 支援 method receiver：func (r *Receiver) Name()
            pattern = r'^func\s+(?:\([^)]+\)\s+)?(\w+)'
            type_pattern = r'^type\s+(\w+)'
            for i, line in enumerate(lines):
                m = re.match(pattern, line)
                if m:
                    add_symbol(m.group(1), 'function', i + 1)
                    continue
                m = re.match(type_pattern, line)
                if m:
                    add_symbol(m.group(1), 'class', i + 1)

        # JavaScript/TypeScript：支援 arrow function、object method、export
        elif ext in ('.js', '.ts', '.jsx', '.tsx'):
            patterns = [
                # class Foo / export class Foo
                (r'^(?:export\s+)?(?:default\s+)?(class)\s+(\w+)', 'class'),
                # function foo / async function foo / export function foo
                (r'^(?:export\s+)?(?:default\s+)?(async\s+)?function\s+(\w+)', 'function'),
                # const foo = (...) => / const foo = async (...) =>
                (r'^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>', 'function'),
                # const foo = function / const foo = async function
                (r'^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?function', 'function'),
                # object method in object literal: foo: (...) => / foo: function
                (r'^\s+(\w+)\s*:\s*(?:async\s+)?(?:function|\([^)]*\)\s*=>)', 'function'),
                # class method (with indentation): async foo() { / foo() {
                (r'^\s+(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{', 'function'),
                # interface/type
                (r'^(?:export\s+)?(?:interface|type)\s+(\w+)', 'class'),
            ]
            for i, line in enumerate(lines):
                for pattern, sym_type in patterns:
                    m = re.match(pattern, line)
                    if m:
                        # 取最後一個 group 作為 symbol name
                        symbol_name = m.group(m.lastindex)
                        # 跳過常見的控制流關鍵字
                        if symbol_name in ('if', 'else', 'for', 'while', 'switch', 'catch', 'try', 'finally'):
                            continue
                        add_symbol(symbol_name, sym_type, i + 1)
                        break

        return symbols

    def _get_embedding(self, text: str) -> list:
        """取得 embedding（使用 LRU cache 加速重複查詢）"""
        normalized = _normalize_text_for_cache(text)
        result = _cached_get_embedding(normalized)
        return list(result) if result else []

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
        folder_path = Path(self.folder)

        for dirpath, dirnames, filenames in os.walk(self.folder):
            # 使用統一的目錄過濾邏輯
            rel_dir = Path(dirpath).relative_to(folder_path)
            dirnames[:] = [d for d in dirnames if not should_ignore_dir(rel_dir / d)]

            for filename in filenames:
                # 跳過 dotfile（包含快取檔案）
                if filename.startswith('.'):
                    continue
                if Path(filename).suffix.lower() not in CODE_EXTENSIONS:
                    continue

                filepath = Path(dirpath) / filename
                # 使用相對路徑，讓 should_ignore_file 可匹配 docs/** 等 pattern
                rel_path = str(filepath.relative_to(self.folder))
                if should_ignore_file(rel_path):
                    continue
                try:
                    content = filepath.read_text(encoding='utf-8', errors='replace')
                    symbols = self._extract_symbols(filepath, content)

                    for sym in symbols:
                        # 改進：embedding text 加入 path + type，提升搜尋精準度
                        # 格式："{path} {type} {symbol} {context}"
                        embed_text = f"{sym['path']} {sym['type']} {sym['symbol']} {sym['context'][:300]}"
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
                except Exception as e:
                    # 記錄錯誤但不中斷索引建立
                    print(f"[CODE_RAG] 索引 {rel_path} 時發生錯誤: {e}", file=sys.stderr)
                    continue

        # 將 embedding 轉換為 numpy array 並預先 L2 normalize
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

            # 預先 L2 normalize，query 時可省掉 norm 計算
            norms = np.linalg.norm(self.embeddings, axis=1, keepdims=True)
            norms = np.where(norms > 0, norms, 1.0)  # 避免除零
            self.embeddings = self.embeddings / norms
            self._embeddings_normalized = True
        else:
            self.embeddings = None
            # 沒有 numpy 時，退回舊方式（embedding 存在 index 裡）
            for i, emb in enumerate(embeddings_list):
                self.index[i]['embedding'] = emb

        if verbose:
            print(f"[CODE_RAG] 索引完成: {len(self.index)} 個符號")

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
        context = item.get("context", "")

        # 將 symbol 和 path 分解為 tokens
        target_tokens = self._tokenize_identifier(symbol)
        # path 中提取檔名部分
        path_name = Path(path).stem if path else ""
        target_tokens.update(self._tokenize_identifier(path_name))

        # 從 context 中提取 identifier tokens（限制數量避免太多雜訊）
        context_tokens = set()
        context_identifiers = re.findall(r'[A-Za-z_][A-Za-z0-9_]{2,}', context[:500])
        for ident in context_identifiers[:30]:  # 最多取 30 個 identifier
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

            kw_score = self._token_match_score(code_tokens, item)

            # 明確點名 symbol 時，大幅提高權重
            # 改進：用 symbol 精確匹配（正則邊界）而非單純 kw_score 門檻
            # 避免短 query 或中文問題下 kw_score 門檻誤收噪音
            symbol = item.get("symbol", "")
            symbol_lower = symbol.lower()
            is_explicit_mention = any(
                t.lower() == symbol_lower for t in code_tokens
            )

            if is_explicit_mention:
                # 明確點名：直接給很高分，即使 embedding 不太像
                combined = 0.95
            elif kw_score >= 0.8 and len(code_tokens) >= 2:
                # 高 kw_score 但需要至少 2 個 code_tokens，避免短 query 誤判
                combined = 0.9 + kw_score * 0.1
            else:
                # 一般情況：function 類型給一點優先權
                type_bonus = 0.05 if item.get('type') == 'function' else 0
                combined = 0.5 * emb_score + 0.5 * kw_score + type_bonus

            scores.append((combined, emb_score, kw_score, item))

        scores.sort(reverse=True, key=lambda x: x[0])

        # 改進：短 query 判定使用 code_tokens 數量而非 split()
        # 中文問題用 split() 會被判成 1 個字串，導致誤判
        is_short_query = len(code_tokens) <= 2

        results = []
        for combined, emb_score, kw_score, item in scores[:top_k]:
            # 使用動態門檻
            # 改進：短 query 時更嚴格，避免混入噪音
            if is_short_query:
                # 短 query：只有 combined 夠高或 explicit symbol match 才加入
                symbol_lower = item.get("symbol", "").lower()
                is_explicit = any(t.lower() == symbol_lower for t in code_tokens)
                if combined >= threshold or is_explicit:
                    results.append({
                        'path': item['path'],
                        'symbol': item['symbol'],
                        'type': item['type'],
                        'line': item['line'],
                        'score': round(combined, 3)
                    })
            else:
                # 一般 query：kw_score 高分(>=0.85)也額外加入
                if combined >= threshold or kw_score >= 0.85:
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
