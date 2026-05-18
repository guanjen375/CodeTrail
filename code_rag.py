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
    CODE_RAG_LAZY_EMBED, CODE_RAG_LAZY_EMBED_MAX_SYMBOLS, CODE_RAG_LAZY_EMBED_QUERY_TOP_K,
    OLLAMA_EMBEDDINGS_URL, OLLAMA_GENERATE_URL,
    USE_RERANKER, RERANKER_MODEL
)
from utils import should_ignore_file, should_ignore_dir, get_cached_scan_result, set_scan_cache

# 導入 AST 解析器
from ast_parser import parse_file, get_parser_status


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

    快取優化 v3（增量更新）：
    - 每檔案獨立快取：file_hash -> symbols + embeddings
    - 只重建變更的檔案，大幅提升大型 repo 的速度
    - embedding 使用 numpy .npz 二進位格式（壓縮率高）
    """

    def __init__(self, folder: str):
        self.folder = Path(folder).resolve()
        # 快取檔案
        cache_base = CODE_RAG_CACHE_FILE.replace('.json', '')
        self.cache_meta_file = self.folder / f"{cache_base}_meta.json"
        self.cache_emb_file = self.folder / f"{cache_base}_emb.npz"
        # 舊版快取檔案（用於向後相容）
        self.legacy_cache_file = self.folder / CODE_RAG_CACHE_FILE
        self.index = []
        self.embeddings = None  # numpy array, shape: (N, embedding_dim)
        # 增量快取：{file_rel_path: {"hash": str, "symbols": list, "embeddings": list}}
        self._file_cache = {}
        self._lazy_embed = False
        self._lazy_embed_top_k = CODE_RAG_LAZY_EMBED_QUERY_TOP_K

    # 小檔走 content hash 的門檻 — 256 KiB 以下直接 hash 內容,
    # 大於這個值 fallback 到 size + mtime_ns 的快路徑。
    _CONTENT_HASH_MAX_BYTES = 256 * 1024

    def _compute_file_hash(self, filepath: Path) -> str:
        """計算單一檔案的 hash（用於增量快取驗證）。

        小檔(≤256 KiB)直接 hash content,避開「同秒多次寫入 / preserve-timestamp
        同步工具」造成的 mis-hit;大檔走 size + mtime_ns 快路徑,平衡 I/O 與
        正確性。mtime_ns 比 mtime(秒解析度)更穩,在快速 edit-save 場景下不會
        誤判 cache hit。
        """
        try:
            stat = filepath.stat()
            if stat.st_size <= self._CONTENT_HASH_MAX_BYTES:
                return hashlib.md5(filepath.read_bytes()).hexdigest()
            return hashlib.md5(
                f"{stat.st_size}:{stat.st_mtime_ns}".encode()
            ).hexdigest()
        except OSError:
            return ""

    def _compute_folder_hash(self) -> str:
        """計算資料夾的 hash（用於快取驗證，向後相容）"""
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

    def _scan_code_files(self) -> dict:
        """掃描所有程式碼檔案，返回 {rel_path: {"filepath": Path, "hash": str}}

        P0 改進：使用共用快取減少重複掃描
        """
        cache_base = CODE_RAG_CACHE_FILE.replace('.json', '')
        skip_files = {
            CODE_RAG_CACHE_FILE,
            f"{cache_base}_meta.json",
            f"{cache_base}_emb.npz",
        }

        # P0 改進：檢查共用快取
        cached = get_cached_scan_result(str(self.folder))
        if cached is not None:
            # 快取格式是 [{"path": str, "size": int}, ...]
            # 轉換為我們需要的格式並計算 hash
            result = {}
            for item in cached:
                rel_path = item["path"]
                if Path(rel_path).name in skip_files:
                    continue
                filepath = self.folder / rel_path
                file_hash = self._compute_file_hash(filepath)
                if file_hash:
                    result[rel_path] = {"filepath": filepath, "hash": file_hash}
            return result

        result = {}
        folder_path = Path(self.folder)
        files_for_cache = []

        for dirpath, dirnames, filenames in os.walk(self.folder):
            rel_dir = Path(dirpath).relative_to(folder_path)
            dirnames[:] = [d for d in dirnames if not should_ignore_dir(rel_dir / d)]

            for filename in filenames:
                if filename.startswith('.') or filename in skip_files:
                    continue
                if Path(filename).suffix.lower() not in CODE_EXTENSIONS:
                    continue

                filepath = Path(dirpath) / filename
                rel_path = str(filepath.relative_to(self.folder))
                if should_ignore_file(rel_path):
                    continue

                file_hash = self._compute_file_hash(filepath)
                if file_hash:
                    result[rel_path] = {"filepath": filepath, "hash": file_hash}
                    # 加入快取格式
                    try:
                        stat = filepath.stat()
                        files_for_cache.append({"path": rel_path, "size": stat.st_size})
                    except OSError:
                        pass

        # P0 改進：設定共用快取
        if files_for_cache:
            set_scan_cache(str(self.folder), files_for_cache)

        return result

    def _load_file_cache(self) -> dict:
        """載入增量快取（每檔案粒度）

        Returns:
            {rel_path: {"hash": str, "symbols": list, "embeddings": list}}
        """
        if not self.cache_meta_file.exists():
            return {}

        try:
            with open(self.cache_meta_file, 'r', encoding='utf-8') as f:
                meta = json.load(f)

            # 檢查 embedding model 是否一致
            if meta.get("embedding_model") != EMBEDDING_MODEL:
                return {}

            return meta.get("file_cache", {})
        except Exception:
            return {}

    def _load_cache(self) -> bool:
        """嘗試載入快取（優先增量模式，向後相容舊格式）"""
        # 載入增量快取
        self._file_cache = self._load_file_cache()

        # 如果有增量快取，使用增量模式
        if self._file_cache:
            return False  # 返回 False 讓 build_index 進行增量更新

        # 向後相容：嘗試載入舊格式快取（folder_hash 模式）
        folder_hash = self._compute_folder_hash()

        if self.cache_meta_file.exists() and self.cache_emb_file.exists() and HAS_NUMPY:
            try:
                with open(self.cache_meta_file, 'r', encoding='utf-8') as f:
                    meta = json.load(f)

                # 舊格式用 folder_hash 驗證
                if meta.get("folder_hash") == folder_hash and meta.get("embedding_model") == EMBEDDING_MODEL:
                    self.index = meta.get("index", [])
                    emb_data = np.load(self.cache_emb_file)
                    self.embeddings = emb_data['embeddings']

                    if len(self.index) > 0 and self.embeddings is not None:
                        return True
            except Exception:
                pass

        # 向後相容：舊版 JSON 格式
        if self.legacy_cache_file.exists():
            try:
                with open(self.legacy_cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                if data.get("folder_hash") != folder_hash:
                    return False

                old_index = data.get("index", [])
                if not old_index:
                    return False

                self.index = []
                embeddings_list = []
                for item in old_index:
                    emb = item.pop('embedding', [])
                    self.index.append(item)
                    embeddings_list.append(emb if emb else [0.0] * 1024)

                if HAS_NUMPY:
                    self.embeddings = np.array(embeddings_list, dtype=np.float32)
                    self._save_cache()
                    try:
                        self.legacy_cache_file.unlink()
                    except Exception:
                        pass
                else:
                    for i, emb in enumerate(embeddings_list):
                        self.index[i]['embedding'] = emb

                return len(self.index) > 0
            except Exception:
                return False

        return False

    def _save_cache(self):
        """儲存快取（增量模式：每檔案粒度）"""
        try:
            emb_dim = self.embeddings.shape[1] if HAS_NUMPY and self.embeddings is not None else None
            meta = {
                "embedding_model": EMBEDDING_MODEL,
                "embedding_dim": emb_dim,
                "index": self.index,
                "file_cache": self._file_cache  # 增量快取
            }
            with open(self.cache_meta_file, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False)

            if HAS_NUMPY and self.embeddings is not None:
                np.savez_compressed(self.cache_emb_file, embeddings=self.embeddings)
        except Exception:
            pass

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
                'context': sym.context[:500],
            }
            # 如果有 parent（method 屬於某個 class），記錄下來
            if sym.parent:
                symbol_dict['parent'] = sym.parent
            # P0 改進：擴充欄位
            if sym.signature:
                symbol_dict['signature'] = sym.signature
            if sym.docstring:
                symbol_dict['docstring'] = sym.docstring[:300]
            if sym.type_hints:
                symbol_dict['type_hints'] = sym.type_hints

            symbols.append(symbol_dict)

        return symbols

    def _get_embedding(self, text: str) -> list:
        """取得 embedding（使用 LRU cache 加速重複查詢）"""
        normalized = _normalize_text_for_cache(text)
        result = _cached_get_embedding(normalized)
        return list(result) if result else []

    def _build_embed_text(self, item: dict) -> str:
        """Build embed text from a symbol/item dict.

        P0 改進：擴充 embedding 內容（signature, docstring, type_hints, parent）
        """
        parts = []

        # 基本資訊
        parts.append(item.get('path', ''))
        parts.append(item.get('type', ''))
        parts.append(item.get('symbol', ''))

        # P0 改進：加入 parent（類別/繼承）
        if item.get('parent'):
            parts.append(f"in {item['parent']}")

        # P0 改進：加入 signature（函式簽名，優先於 context）
        if item.get('signature'):
            parts.append(item['signature'])

        # P0 改進：加入 docstring（截取前 200 字元）
        if item.get('docstring'):
            parts.append(item['docstring'][:200])

        # P0 改進：加入 type hints
        if item.get('type_hints'):
            parts.append(f"types: {item['type_hints']}")

        # Context（補充剩餘空間）
        context = item.get('context', '')
        # 計算已用長度，調整 context 截取
        used_len = sum(len(p) for p in parts)
        max_context = max(100, 400 - used_len)
        if context:
            parts.append(context[:max_context])

        return ' '.join(parts)

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
                'context': sym['context'][:500],
            }
            if 'end_line' in sym:
                index_entry['end_line'] = sym['end_line']
            if 'parent' in sym:
                index_entry['parent'] = sym['parent']
            # P0 改進：儲存擴充欄位
            if 'signature' in sym and sym['signature']:
                index_entry['signature'] = sym['signature']
            if 'docstring' in sym and sym['docstring']:
                index_entry['docstring'] = sym['docstring'][:300]
            if 'type_hints' in sym and sym['type_hints']:
                index_entry['type_hints'] = sym['type_hints']

            file_symbols.append(index_entry)
            file_embeddings.append(emb if emb else [])

        return file_symbols, file_embeddings

    def build_index(self, verbose: bool = True):
        """建立程式碼索引（支援增量更新）"""
        if not CODE_RAG_ENABLED:
            return

        # 嘗試載入快取
        if self._load_cache():
            if verbose:
                print(f"[CODE_RAG] 載入快取: {len(self.index)} 個符號")
            return

        # 掃描所有程式碼檔案
        current_files = self._scan_code_files()

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
            parser_status = get_parser_status()
            if parser_status['has_tree_sitter']:
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

        # 索引變更的檔案
        indexed_count = 0
        for rel_path, filepath, file_hash in files_to_index:
            try:
                compute_embeddings = not (lazy_enabled and self._lazy_embed)
                symbols, embeddings = self._index_single_file(
                    filepath, rel_path, compute_embeddings=compute_embeddings
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
            except Exception as e:
                print(f"[CODE_RAG] 索引 {rel_path} 時發生錯誤: {e}", file=sys.stderr)
                continue

        self._file_cache = new_file_cache

        # 將 embedding 轉換為 numpy array 並預先 L2 normalize
        if HAS_NUMPY and embeddings_list and not self._lazy_embed:
            emb_dim = max((len(e) for e in embeddings_list if e), default=1024)
            normalized = []
            for emb in embeddings_list:
                if len(emb) == emb_dim:
                    normalized.append(emb)
                elif len(emb) == 0:
                    normalized.append([0.0] * emb_dim)
                else:
                    normalized.append((emb + [0.0] * emb_dim)[:emb_dim])
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

        if verbose:
            if is_incremental:
                print(f"[CODE_RAG] 增量更新完成: 共 {len(self.index)} 個符號")
            else:
                print(f"[CODE_RAG] 索引完成: {len(self.index)} 個符號")
            if self._lazy_embed:
                print(f"[CODE_RAG] lazy embed on: >{CODE_RAG_LAZY_EMBED_MAX_SYMBOLS} symbols")

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

    def _check_reranker_available(self) -> bool:
        """檢查 reranker 模型是否可用"""
        if not hasattr(self, '_reranker_available'):
            try:
                from config import OLLAMA_TAGS_URL
                session = get_session()
                resp = session.get(OLLAMA_TAGS_URL, timeout=5)
                if resp.status_code == 200:
                    models = resp.json().get("models", [])
                    model_names = [m.get("name", "") for m in models]
                    self._reranker_available = any(RERANKER_MODEL in n for n in model_names)
                else:
                    self._reranker_available = False
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

    def _rerank_code_candidates(self, question: str, candidates: list, top_k: int) -> list:
        """使用 reranker 模型對程式碼候選進行二次排序

        Args:
            question: 使用者問題
            candidates: [(combined_score, emb_score, kw_score, item), ...]
            top_k: 返回數量

        Returns:
            重排後的 item list
        """
        if not candidates:
            return []

        if not USE_RERANKER or len(candidates) <= top_k:
            return [c[3] for c in candidates[:top_k]]

        # 條件觸發：判斷是否真的需要 rerank
        if not self._should_rerank(candidates, top_k):
            return [c[3] for c in candidates[:top_k]]

        # 減少 rerank 的 candidates 數量
        rerank_count = min(15, top_k * 3)

        if self._check_reranker_available():
            try:
                session = get_session()
                scored = []

                for combined, emb_score, kw_score, item in candidates[:rerank_count]:
                    # 建構 reranker 輸入：symbol + path + context
                    symbol = item.get('symbol', '')
                    path = item.get('path', '')
                    context = item.get('context', '')[:600]
                    parent_info = f" in {item.get('parent', '')}" if item.get('parent') else ""
                    sym_type = item.get('type', 'function')

                    passage = f"{sym_type} {symbol}{parent_info}\nFile: {path}\n{context}"

                    resp = session.post(
                        OLLAMA_GENERATE_URL,
                        json={
                            "model": RERANKER_MODEL,
                            "prompt": f"Query: {question}\n\nPassage: {passage}",
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
                        # Fallback: 嘗試提取數字
                        match = re.search(r'-?[\d.]+', result)
                        rerank_score = float(match.group()) if match else combined

                    scored.append((rerank_score, item))

                scored.sort(reverse=True, key=lambda x: x[0])
                return [c[1] for c in scored[:top_k]]

            except Exception:
                pass

        # Fallback: 不用 reranker，直接返回原始排序
        return [c[3] for c in candidates[:top_k]]

    def query(self, question: str, top_k: int = CODE_RAG_TOP_K, is_bug_fix: bool = False) -> list[dict]:
        """查詢相關程式碼位置（動態門檻 + reranker 二次排序）

        Lazy build：第一次 query 時才建立索引，避免不需要 CodeRAG 時浪費時間
        """
        # Lazy build：第一次 query 時才建立索引
        if not self.index:
            self.build_index(verbose=True)
            # build 後若仍無索引（空專案），返回空
            if not self.index:
                return []

        q_emb = self._get_embedding(question)
        if not q_emb:
            return []

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

            if self._lazy_embed_top_k > 0 and self.index:
                import heapq
                lazy_top_k = min(self._lazy_embed_top_k, len(self.index))
                cand_indices = heapq.nlargest(
                    lazy_top_k, range(len(self.index)), key=lambda idx: kw_scores[idx]
                )
            else:
                cand_indices = []

            cand_set = {i for i in cand_indices if kw_scores[i] > 0}
            if not cand_set:
                cand_set = set(cand_indices)
            cand_set.update(explicit_indices)
            for idx in cand_set:
                item = self.index[idx]
                if not item.get("embedding"):
                    emb = self._get_embedding(self._build_embed_text(item))
                    if emb:
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

        # 先做初步過濾（門檻篩選）
        candidates_for_rerank = []
        for combined, emb_score, kw_score, item in scores:
            if is_short_query:
                symbol_lower = item.get("symbol", "").lower()
                is_explicit = any(t.lower() == symbol_lower for t in code_tokens)
                if combined >= threshold or is_explicit:
                    candidates_for_rerank.append((combined, emb_score, kw_score, item))
            else:
                if combined >= threshold or kw_score >= 0.85:
                    candidates_for_rerank.append((combined, emb_score, kw_score, item))

            # 收集足夠的候選後停止（rerank 用）
            if len(candidates_for_rerank) >= top_k * 3:
                break

        # 使用 reranker 二次排序（條件觸發）
        reranked_items = self._rerank_code_candidates(question, candidates_for_rerank, top_k)

        # 轉換為結果格式
        results = []
        for item in reranked_items:
            result_item = {
                'path': item['path'],
                'symbol': item['symbol'],
                'type': item['type'],
                'line': item['line'],
                'score': round(item.get('_rerank_score', 0), 3) if '_rerank_score' in item else 0.0
            }
            # 從原始 candidates 取得原始 combined score（如果沒有 rerank score）
            if result_item['score'] == 0.0:
                for combined, _, _, orig_item in candidates_for_rerank:
                    if orig_item is item:
                        result_item['score'] = round(combined, 3)
                        break
            # 新增 end_line 和 parent（如果有）
            if 'end_line' in item:
                result_item['end_line'] = item['end_line']
            if 'parent' in item:
                result_item['parent'] = item['parent']
            results.append(result_item)

        return results

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
