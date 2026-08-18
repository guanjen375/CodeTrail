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
import time as _time
import hashlib
from pathlib import Path
from functools import lru_cache


try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

import config
from config import (
    CODE_EXTENSIONS, EMBEDDING_MODEL,
    CODE_RAG_ENABLED, CODE_RAG_TOP_K, CODE_RAG_CACHE_FILE,
    CODE_RAG_THRESHOLD, CODE_RAG_THRESHOLD_BUG,
    CODE_RAG_LAZY_EMBED, CODE_RAG_LAZY_EMBED_MAX_SYMBOLS, CODE_RAG_LAZY_EMBED_QUERY_TOP_K,
    LLAMA_EMBED_BASE_URL, LLAMA_RERANK_BASE_URL,
    USE_RERANKER, RERANKER_MODEL, RERANKER_ALWAYS_ON
)
import llama_client
from index_scope import load_index_scope, walk_index_files

# 導入 AST 解析器
from ast_parser import parse_file, get_parser_status

# 索引專用的掃描快取。刻意**不**共用 utils 的 _SCAN_CACHE:那份是 agent.py 的
# scan_project_metadata 在用,成員資格規則和索引不同,而且沒有 scope
# fingerprint —— §7 要求 fingerprint 缺失或不一致時禁止 fast path,所以那份
# 快取一律不可信。key 帶 fingerprint,scope 一改就自然失效。
_INDEX_SCAN_CACHE: dict[tuple[str, str], dict] = {}
_INDEX_SCAN_CACHE_TTL = 30  # 秒


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


def _normalize_text_for_cache(text: str) -> str:
    """正規化文字以提高 cache 命中率"""
    return ' '.join(text.split())


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
            "Check the 8081 llama-server or AICODE_LLAMA_EMBED_BASE_URL."
        ) from exc

    if not emb:
        raise RuntimeError(
            f"embedding server returned an empty vector at {LLAMA_EMBED_BASE_URL}. "
            "Check the 8081 llama-server or AICODE_LLAMA_EMBED_BASE_URL."
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
    - embedding 使用 numpy .npz 二進位格式（壓縮率高）
    """

    def __init__(self, folder: str):
        self.folder = Path(folder).resolve()
        # 索引範圍引擎(Layer C 設定不存在時就是預設行為,不會 fail)
        self.scope = load_index_scope(self.folder)
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
        self._indexed_file_hashes = None
        # 快取裡的 scope_fingerprint 是否與現在的規則一致(見 _load_file_cache)
        self._scope_fingerprint_ok = False

    # 保留成 class attribute:既有測試與呼叫端都靠它。
    _CONTENT_HASH_MAX_BYTES = CONTENT_HASH_MAX_BYTES

    def _compute_file_hash(self, filepath: Path) -> str:
        """計算單一檔案的 hash（用於增量快取驗證）。見 compute_file_hash。"""
        return compute_file_hash(filepath, self._CONTENT_HASH_MAX_BYTES)

    def _compute_folder_hash(self) -> str:
        """計算資料夾的 hash（用於舊格式快取驗證，向後相容）

        成員資格與索引本身共用同一個判定（should_index_file），否則「有沒有變」
        和「有沒有進索引」會各講各話。
        """
        files = []
        for filepath, rel_path in walk_index_files(self.scope):
            try:
                stat = filepath.stat()
                files.append(f"{rel_path}:{stat.st_size}:{stat.st_mtime}")
            except OSError:
                pass
        files.sort()
        return hashlib.md5("\n".join(files).encode()).hexdigest()

    def _scan_code_files(self, *, force_refresh: bool = False) -> dict:
        """掃描進索引的程式碼檔案，返回 {rel_path: {"filepath": Path, "hash": str}}

        成員資格由且僅由 self.scope.should_index_file 決定;_filter_dirnames 的
        三態只是剪枝。fast path 的 key 綁 scope fingerprint,而且拿回來的每一條
        cached path 仍然要重過 should_index_file（§7.3:任何來源的 cached path
        都不得直接信任）。
        """
        cache_key = (str(self.folder), self.scope.fingerprint)
        if not force_refresh:
            cached = _INDEX_SCAN_CACHE.get(cache_key)
            if cached is not None and _time.time() - cached["timestamp"] < _INDEX_SCAN_CACHE_TTL:
                result = {}
                for rel_path in cached["paths"]:
                    if not self.scope.should_index_file(rel_path):
                        continue
                    filepath = self.folder / rel_path
                    file_hash = self._compute_file_hash(filepath)
                    if file_hash:
                        result[rel_path] = {"filepath": filepath, "hash": file_hash}
                return result

        result = {}
        for filepath, rel_path in walk_index_files(self.scope):
            file_hash = self._compute_file_hash(filepath)
            if file_hash:
                result[rel_path] = {"filepath": filepath, "hash": file_hash}

        _INDEX_SCAN_CACHE[cache_key] = {
            "paths": list(result.keys()),
            "timestamp": _time.time(),
        }
        return result

    def _scan_code_files_fresh(self) -> dict:
        """Fresh scan helper that keeps simple no-arg test/integration hooks compatible."""
        scanner = self._scan_code_files
        if getattr(scanner, "__func__", None) is CodeRAG._scan_code_files:
            return scanner(force_refresh=True)
        return scanner()

    def _load_file_cache(self) -> dict:
        """載入增量快取（每檔案粒度）

        Returns:
            {rel_path: {"hash": str, "symbols": list, "embeddings": list}}
        """
        self._scope_fingerprint_ok = False
        if not self.cache_meta_file.exists():
            return {}

        try:
            with open(self.cache_meta_file, 'r', encoding='utf-8') as f:
                meta = json.load(f)

            # 檢查 embedding model 是否一致
            if meta.get("embedding_model") != EMBEDDING_MODEL:
                return {}

            # scope 規則變了不代表要重 embed:embedding_model 相同就保留 per-file
            # symbol/embedding cache,只重算 membership delta。這裡只記錄
            # fingerprint 是否相符,由 _load_cache / _scan_code_files 決定要不要
            # 拒絕「整包 fast load」。
            self._scope_fingerprint_ok = (
                meta.get("scope_fingerprint") == self.scope.fingerprint
            )
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

                # 舊格式用 folder_hash 驗證。scope_fingerprint 缺失或不符時一律
                # 拒絕整包 fast load —— 否則會直接沿用「用別套範圍規則建的索引」。
                if (
                    self._scope_fingerprint_ok
                    and meta.get("folder_hash") == folder_hash
                    and meta.get("embedding_model") == EMBEDDING_MODEL
                ):
                    self.index = meta.get("index", [])
                    emb_data = np.load(self.cache_emb_file)
                    self.embeddings = emb_data['embeddings']

                    # §7.3:cached path 一律重過 should_index_file,index 與
                    # embeddings 必須同步裁切,不然 row 會對錯符號。
                    self._filter_loaded_index()

                    if len(self.index) > 0 and self.embeddings is not None:
                        return True
            except Exception:
                pass

        # 向後相容：舊版 JSON 格式。同樣要 scope_fingerprint 相符才准整包載入;
        # 舊版 JSON 從來沒寫過 fingerprint,所以實務上一定走重建那條路。
        if self.legacy_cache_file.exists() and self._scope_fingerprint_ok:
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

    def _filter_loaded_index(self) -> None:
        """把整包載入的 index 重過一次 should_index_file（embeddings 同步裁切）。"""
        if not self.index:
            return
        keep = [
            i for i, item in enumerate(self.index)
            if self.scope.should_index_file(item.get("path", ""))
        ]
        if len(keep) == len(self.index):
            return
        self.index = [self.index[i] for i in keep]
        if HAS_NUMPY and self.embeddings is not None:
            self.embeddings = self.embeddings[keep]

    def _save_cache(self):
        """儲存快取（增量模式：每檔案粒度）"""
        try:
            emb_dim = self.embeddings.shape[1] if HAS_NUMPY and self.embeddings is not None else None
            meta = {
                "embedding_model": EMBEDDING_MODEL,
                "scope_fingerprint": self.scope.fingerprint,
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
        return list(result)

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
            file_embeddings.append(emb)

        return file_symbols, file_embeddings

    def build_index(self, verbose: bool = True, _current_files: dict | None = None):
        """建立程式碼索引（支援增量更新）"""
        if not CODE_RAG_ENABLED:
            return

        # 嘗試載入快取
        if self._load_cache():
            current_files = _current_files or self._scan_code_files_fresh()
            self._indexed_file_hashes = {
                rel_path: info["hash"] for rel_path, info in current_files.items()
            }
            if verbose:
                print(f"[CODE_RAG] 載入快取: {len(self.index)} 個符號")
            return

        # 掃描所有程式碼檔案
        current_files = _current_files or self._scan_code_files_fresh()

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
            except RuntimeError:
                # Embedding failures must not leave a partial index that makes
                # the next query skip build_index after the server recovers.
                self.index = []
                self.embeddings = None
                raise
            except Exception as e:
                print(f"[CODE_RAG] 索引 {rel_path} 時發生錯誤: {e}", file=sys.stderr)
                continue

        self._file_cache = new_file_cache

        # 將 embedding 轉換為 numpy array 並預先 L2 normalize
        if HAS_NUMPY and embeddings_list and not self._lazy_embed:
            self._backfill_cached_embedding_gaps(embeddings_list, verbose=verbose)
            dimensions = {len(embedding) for embedding in embeddings_list if embedding}
            if not dimensions or any(not embedding for embedding in embeddings_list):
                self.index = []
                self.embeddings = None
                raise RuntimeError(
                    "Code RAG full index contains missing embeddings; refusing zero padding"
                )
            if len(dimensions) != 1:
                self.index = []
                self.embeddings = None
                raise RuntimeError(
                    f"Code RAG embedding dimension mismatch: {sorted(dimensions)}; "
                    "zero-padding/truncation is forbidden"
                )
            normalized = embeddings_list
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
        """Incrementally rebuild when files change during the current MCP session."""
        if self._indexed_file_hashes is None:
            return
        current_files = self._scan_code_files_fresh()
        current_hashes = {
            rel_path: info["hash"] for rel_path, info in current_files.items()
        }
        if current_hashes != self._indexed_file_hashes:
            self.build_index(verbose=False, _current_files=current_files)

    def _backfill_cached_embedding_gaps(self, embeddings_list: list, *, verbose: bool) -> None:
        """dense 模式復用 per-file 快取時,把 lazy 模式留下的空 embedding 補回來。

        lazy 模式(符號數 > CODE_RAG_LAZY_EMBED_MAX_SYMBOLS)會把 embedding 存成
        [],延後到查詢時才算。之後索引縮小 —— index scope 變窄、大量檔案被刪 ——
        使符號數掉回門檻以下時,build_index 會走 dense 路徑,那些空洞直接觸發
        「refusing zero padding」fail-loud;而且失敗時不會寫回快取,所以**重啟
        還是失敗**,索引就永久建不起來。索引縮小正是 index scope 的主要場景,
        所以這裡把洞補起來,結果與「刪掉快取重建」一致。

        補算量上限就是新索引的符號數(dense 模式必然 ≤ lazy 門檻),不會比冷啟
        重建更貴。
        """
        missing = [i for i, embedding in enumerate(embeddings_list) if not embedding]
        if not missing:
            return
        if verbose:
            print(f"[CODE_RAG] 補算 {len(missing)} 個 lazy 快取留下的空 embedding")
        for i in missing:
            embeddings_list[i] = self._get_embedding(
                self._build_embed_text(self.index[i])
            )
        # 寫回 per-file 快取,否則 _save_cache 會把洞原樣存回去,下次照樣爆。
        self._sync_embeddings_to_file_cache(embeddings_list)

    def _sync_embeddings_to_file_cache(self, rows: list[list[float]]) -> None:
        by_symbol = {
            (item.get("path"), item.get("symbol"), item.get("line")): row
            for item, row in zip(self.index, rows)
        }
        for cached in self._file_cache.values():
            cached["embeddings"] = [
                by_symbol.get(
                    (symbol.get("path"), symbol.get("symbol"), symbol.get("line")),
                    [],
                )
                for symbol in cached.get("symbols", [])
            ]

    def _materialize_dense_index(self) -> None:
        """Embed every lazy symbol when lexical routing has no meaningful signal."""
        rows = []
        dimensions = set()
        for index, item in enumerate(self.index):
            embedding = item.get("embedding") or self._get_embedding(
                self._build_embed_text(item)
            )
            if not embedding:
                raise RuntimeError(f"Code RAG symbol {index} returned an empty embedding")
            row = [float(value) for value in embedding]
            dimensions.add(len(row))
            rows.append(row)
        if len(dimensions) != 1:
            raise RuntimeError(
                f"Code RAG embedding dimension mismatch: {sorted(dimensions)}; "
                "refusing a mixed dense index"
            )

        self._sync_embeddings_to_file_cache(rows)
        if HAS_NUMPY and rows:
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

    def _rerank_code_fallback(self, candidates: list, top_k: int, reason: str) -> list:
        """Fallback for Code RAG rerank. main_model is intentionally embedding here."""
        if config.RERANK_FALLBACK_POLICY == "error":
            raise RuntimeError(
                "Code RAG reranker unavailable and AICODE_RERANK_FALLBACK_POLICY=error. "
                f"Reason: {reason}"
            )
        return [c[3] for c in candidates[:top_k]]

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
                # 一次把 candidates 全部送進 /reranking 端點,llama-server 端
                # 用 cross-encoder 一次 forward 完所有 (query, passage) pair,
                # 不用 per-item HTTP round trip。
                items = candidates[:rerank_count]
                passages = []
                for combined, emb_score, kw_score, item in items:
                    symbol = item.get('symbol', '')
                    path = item.get('path', '')
                    context = item.get('context', '')[:600]
                    parent_info = f" in {item.get('parent', '')}" if item.get('parent') else ""
                    sym_type = item.get('type', 'function')
                    passages.append(
                        f"{sym_type} {symbol}{parent_info}\nFile: {path}\n{context}"
                    )

                scores = llama_client.rerank(
                    base_url=LLAMA_RERANK_BASE_URL,
                    query=question,
                    documents=passages,
                    model=RERANKER_MODEL,
                    timeout=60,
                )
                scored = [(score, items[i][3]) for i, score in enumerate(scores)]
                scored.sort(reverse=True, key=lambda x: x[0])
                return [c[1] for c in scored[:top_k]]

            except Exception as exc:
                return self._rerank_code_fallback(
                    candidates, top_k, f"dedicated reranker call failed: {exc}"
                )

        # Code RAG 沒有主模型 rerank 路徑;main_model policy 在這裡等同 embedding。
        return self._rerank_code_fallback(candidates, top_k, "dedicated reranker is not reachable")

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
        else:
            self._refresh_if_stale()
            if not self.index:
                return []

        q_emb = self._get_embedding(question)

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

            if not any(score > 0 for score in kw_scores):
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
                    if not item.get("embedding"):
                        emb = self._get_embedding(self._build_embed_text(item))
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
