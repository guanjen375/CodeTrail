#!/usr/bin/env python3
"""index_stats —— 看「這棵樹到底有多少東西進了 Code RAG 索引」。

完全唯讀、完全離線:不寫任何 cache、不呼叫任何 server、不碰模型設定。
預設輸出**只有計數**,不含任何路徑 —— 這個工具會被貼進 issue / 聊天視窗,
路徑本身就是 NDA 內容。要看路徑樣本請顯式加 ``--show-paths``(只印終端)。

用法:
    AICODE_ROOT=/path/to/project python scripts/index_stats.py
    python scripts/index_stats.py --root /path/to/project
    python scripts/index_stats.py --root /path/to/project --deep

符號數預設讀既有的 .code_rag_cache_meta.json;沒有(或涵蓋不全)就印 unknown。
``--deep`` 才真的跑 AST 解析,並且有檔數 / 時間預算,超過會標示 truncated。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import CODE_RAG_CACHE_FILE  # noqa: E402
from index_scope import (  # noqa: E402
    IndexScopeError,
    load_index_scope,
    validate_aicode_root,
    walk_index_files,
)

DEEP_MAX_FILES = 2000
DEEP_TIMEOUT_SECONDS = 30.0
DEEP_MAX_BYTES = 2 * 1024 * 1024


class RootError(Exception):
    """root 未給 / 不安全。退出碼 2,比照 mcp_server.py。"""


def _resolve_root(cli_root: str | None, env: dict) -> str:
    """root 只能來自 --root 或 AICODE_ROOT;都沒有就報錯,不猜 cwd。"""
    raw = cli_root or env.get("AICODE_ROOT")
    if not raw:
        raise RootError(
            "[FATAL] 沒有 root:請給 --root <path> 或設 AICODE_ROOT。\n"
            "        這個工具不猜 cwd —— 猜錯會掃到不該掃的樹。"
        )
    resolved, err = validate_aicode_root(
        raw,
        env.get("HOME") or env.get("USERPROFILE"),
        allow_home_override=(env.get("AI_CODE_ALLOW_HOME_ROOT", "").lower()
                             in ("1", "true", "yes")),
    )
    if err:
        raise RootError(err)
    assert resolved is not None
    return resolved


def _cached_symbol_count(root: Path, files: list[tuple[Path, str]]) -> int | None:
    """從既有快取算符號數。涵蓋不全或過期就回 None(印 unknown,不瞎猜)。

    只確認「每個檔案都在快取裡」是不夠的:檔案改過之後快取仍會回報舊的符號數。
    這個數字的用途就是判斷索引範圍對不對,報一個過期的數字比報 unknown 更糟,
    所以 hash 也要對得上 —— 用 code_rag 自己那份 hash 實作,避免兩邊漂移。
    """
    cache_base = CODE_RAG_CACHE_FILE.replace(".json", "")
    meta_file = root / f"{cache_base}_meta.json"
    if not meta_file.is_file():
        return None
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    file_cache = meta.get("file_cache")
    if not isinstance(file_cache, dict):
        return None

    from code_rag import compute_file_hash  # 只有真的有快取檔才付這個 import 成本

    total = 0
    for filepath, rel in files:
        entry = file_cache.get(rel)
        if not isinstance(entry, dict):
            return None                      # 有檔案沒進快取 → 數字不可信
        if entry.get("hash") != compute_file_hash(filepath):
            return None                      # 快取過期 → 數字不可信
        total += len(entry.get("symbols") or ())
    return total


def _deep_symbol_count(scope, files: list[tuple[Path, str]], max_files: int,
                       timeout: float) -> tuple[int, int, bool]:
    """AST-only 計算。回傳 (symbols, parsed_files, truncated)。"""
    from ast_parser import parse_file  # 只有 --deep 才付這個 import 成本

    started = time.monotonic()
    symbols = 0
    parsed = 0
    truncated = False
    for filepath, _rel in files:
        if parsed >= max_files or time.monotonic() - started > timeout:
            truncated = True
            break
        if not scope.contained(filepath):      # 讀檔前再驗一次(縮窄 TOCTOU)
            scope.note_symlink_escape()
            continue
        try:
            if not filepath.is_file():         # FIFO/socket 讀下去會永久阻塞
                truncated = True
                continue
            if filepath.stat().st_size > DEEP_MAX_BYTES:
                truncated = True
                continue
            content = filepath.read_text(encoding="utf-8", errors="replace")
            symbols += len(parse_file(filepath, content))
        except (OSError, ValueError, RuntimeError):
            truncated = True
            continue
        parsed += 1
    return symbols, parsed, truncated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="index_stats.py",
        description="Code RAG 索引範圍的計數摘要(唯讀、離線、預設不印路徑)",
    )
    parser.add_argument("--root", help="要統計的樹;不給就用 AICODE_ROOT")
    parser.add_argument("--deep", action="store_true",
                        help="真的跑 AST 算符號數(有檔數/時間預算)")
    parser.add_argument("--deep-max-files", type=int, default=DEEP_MAX_FILES,
                        help=f"--deep 的檔數預算(預設 {DEEP_MAX_FILES})")
    parser.add_argument("--deep-timeout", type=float, default=DEEP_TIMEOUT_SECONDS,
                        help=f"--deep 的時間預算秒數(預設 {DEEP_TIMEOUT_SECONDS})")
    parser.add_argument("--show-paths", action="store_true",
                        help="額外印出路徑樣本(只印終端;預設關閉)")
    parser.add_argument("--max-paths", type=int, default=50,
                        help="--show-paths 印幾條(預設 50)")
    args = parser.parse_args(argv)

    try:
        root = Path(_resolve_root(args.root, os.environ))
        scope = load_index_scope(root)
    except RootError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except IndexScopeError as exc:
        print(f"[FATAL] index-scope.json 不合法:{exc}", file=sys.stderr)
        return 2

    files = list(walk_index_files(scope))
    rel_paths = [rel for _fp, rel in files]

    if args.deep:
        symbols, parsed, truncated = _deep_symbol_count(
            scope, files, args.deep_max_files, args.deep_timeout
        )
        suffix = f" (deep scan truncated: {parsed}/{len(files)} files)" if truncated else ""
        symbol_text = f"{symbols}{suffix}"
    else:
        cached = _cached_symbol_count(root, files)
        symbol_text = "unknown" if cached is None else f"{cached} (cached)"

    print(f"indexed: {len(files)} files / {symbol_text} symbols")
    for line in scope.stats_lines():
        print(line)

    if args.show_paths:
        print(f"--- paths sample (max {args.max_paths}) ---")
        for rel in rel_paths[: args.max_paths]:
            print(rel)
        if len(rel_paths) > args.max_paths:
            print(f"... 還有 {len(rel_paths) - args.max_paths} 條未列出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
