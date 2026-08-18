#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AICODE_ROOT 安全檢查 —— 單一實作,給 mcp_server.py 與維護 CLI 共用。

刻意不 import config / utils / mcp:任何工具(包含 scripts/index_stats.py 這種
完全離線的唯讀 CLI)都要能在不拉起 MCP server、不碰模型設定的情況下驗證 root。
純函式,方便測試。
"""
from __future__ import annotations

from pathlib import Path


def validate_aicode_root(root_env: str | None, home: str | None,
                         allow_home_override: bool) -> tuple[str | None, str | None]:
    """純函式：判斷 AICODE_ROOT 是否安全。回傳 (resolved_root, error_msg)。

    拒絕:未設定 / 無法解析 / 不是目錄 / filesystem root(含磁碟根) / $HOME。
    """
    if not root_env:
        return None, (
            "[FATAL] 未設定 AICODE_ROOT 環境變數。\n"
            "        為避免誤掃 cwd 或洩漏 NDA 內容, server 拒絕啟動。\n"
            "        範例:  AICODE_ROOT=/path/to/project python mcp_server.py"
        )
    try:
        resolved_path = Path(root_env).resolve()
        resolved = str(resolved_path)
    except (OSError, ValueError) as e:
        return None, f"[FATAL] AICODE_ROOT 無法解析: {e}"

    if not resolved_path.is_dir():
        return None, f"[FATAL] AICODE_ROOT 不是目錄: {resolved}"

    if resolved_path.parent == resolved_path:
        return None, (
            "[FATAL] 拒絕 AICODE_ROOT=/ — 會把整個檔案系統暴露給 MCP sandbox。\n"
            "        cd 到具體 project 目錄再啟動 mcp_server.py。"
        )
    if home:
        try:
            home_resolved = str(Path(home).resolve())
        except (OSError, ValueError):
            home_resolved = home
        if resolved == home_resolved and not allow_home_override:
            return None, (
                f"[FATAL] 拒絕 AICODE_ROOT=$HOME ({home_resolved})。\n"
                "        $HOME 範圍太大且很容易意外洩漏個人資料。\n"
                "        cd 到具體 project 目錄再啟動。\n"
                "        若真的有需要 (高風險，自行承擔), 設定:\n"
                "        AI_CODE_ALLOW_HOME_ROOT=1"
            )
    return resolved, None
