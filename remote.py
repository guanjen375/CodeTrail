#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 遠端檔案同步模組 (--mcp)

透過 SSH 從遠端主機同步檔案到本地暫存目錄，用於分析遠端程式碼。

使用方式：
    python main.py --mcp user@host:/path/to/dir "你的問題"
    python main.py --mcp user@host:/path/to/dir --qa "你的問題"

支援格式：
    user@host:/path          → SSH 預設 port
    user@host:port:/path     → 指定 port
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple, Dict


def parse_ssh_uri(uri: str) -> Optional[Dict[str, str]]:
    """解析 SSH URI 格式

    支援：
        user@host:/path
        user@host:port:/path

    Returns:
        {"user": "kjwang", "host": "140.96.28.10", "port": "22", "path": "/home/kjwang"}
        或 None（解析失敗）
    """
    # 格式: user@host:port:/path 或 user@host:/path
    m = re.match(r'^([^@]+)@([^:]+):(\d+):(.+)$', uri)
    if m:
        return {
            "user": m.group(1),
            "host": m.group(2),
            "port": m.group(3),
            "path": m.group(4),
        }

    # 格式: user@host:/path
    m = re.match(r'^([^@]+)@([^:]+):(.+)$', uri)
    if m:
        return {
            "user": m.group(1),
            "host": m.group(2),
            "port": "22",
            "path": m.group(3),
        }

    return None


def _has_command(cmd: str) -> bool:
    """檢查系統是否有某個指令"""
    try:
        subprocess.run(
            ["which", cmd] if os.name != "nt" else ["where", cmd],
            capture_output=True, timeout=5
        )
        return True
    except Exception:
        return False


def sync_remote_files(ssh_info: Dict[str, str], temp_dir: str) -> bool:
    """透過 rsync 或 scp 同步遠端檔案到本地

    優先使用 rsync（增量同步、可排除目錄），fallback 到 scp。

    Args:
        ssh_info: parse_ssh_uri 的結果
        temp_dir: 本地暫存目錄

    Returns:
        True 表示成功
    """
    user = ssh_info["user"]
    host = ssh_info["host"]
    port = ssh_info["port"]
    remote_path = ssh_info["path"].rstrip("/") + "/"

    # 排除不需要的目錄（與 config.py IGNORED_DIRS 對齊）
    excludes = [
        ".git", "__pycache__", ".venv", "venv", "node_modules",
        ".idea", ".vscode", "build", "dist", ".cache", ".tox",
        "eggs", "htmlcov", ".pytest_cache", ".mypy_cache",
        "third_party", "3rdparty", "external", "vendor",
    ]

    # 嘗試 rsync
    if _has_command("rsync"):
        cmd = [
            "rsync", "-az", "--timeout=30",
            "-e", f"ssh -p {port} -o StrictHostKeyChecking=no -o ConnectTimeout=10",
        ]
        for ex in excludes:
            cmd += ["--exclude", ex]

        cmd += [f"{user}@{host}:{remote_path}", temp_dir + "/"]

        print(f"[MCP] 使用 rsync 同步...")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                return True
            else:
                print(f"[MCP] rsync 失敗: {result.stderr.strip()}")
                # fallback to scp
        except subprocess.TimeoutExpired:
            print("[MCP] rsync 逾時（300s）")
        except Exception as e:
            print(f"[MCP] rsync 錯誤: {e}")

    # Fallback: scp
    if _has_command("scp"):
        cmd = [
            "scp", "-r",
            "-P", port,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            f"{user}@{host}:{remote_path}",
            temp_dir + "/",
        ]

        print(f"[MCP] 使用 scp 同步...")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                return True
            else:
                print(f"[MCP] scp 失敗: {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            print("[MCP] scp 逾時（300s）")
        except Exception as e:
            print(f"[MCP] scp 錯誤: {e}")

    print("[MCP] 無法同步遠端檔案（需要 rsync 或 scp）")
    return False


def fetch_from_remote(uri: str) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    """從 SSH URI 同步遠端檔案

    Args:
        uri: SSH URI，如 "kjwang@140.96.28.10:/home/kjwang"

    Returns:
        (temp_dir, ssh_info) 或 (None, None)
    """
    print(f"[MCP] 解析遠端位址: {uri}")

    ssh_info = parse_ssh_uri(uri)
    if not ssh_info:
        print("[MCP] URI 格式錯誤")
        print("[MCP] 支援格式：")
        print("      user@host:/path")
        print("      user@host:port:/path")
        return None, None

    print(f"[MCP] 主機: {ssh_info['user']}@{ssh_info['host']}:{ssh_info['port']}")
    print(f"[MCP] 路徑: {ssh_info['path']}")

    # 建立暫存目錄
    temp_dir = tempfile.mkdtemp(prefix="ai_code_mcp_")
    print(f"[MCP] 暫存目錄: {temp_dir}")

    if sync_remote_files(ssh_info, temp_dir):
        # 檢查是否有檔案
        file_count = sum(1 for _, _, files in os.walk(temp_dir) for _ in files)
        if file_count == 0:
            print("[MCP] 遠端目錄是空的或無法讀取")
            shutil.rmtree(temp_dir, ignore_errors=True)
            return None, None

        print(f"[MCP] 同步完成，共 {file_count} 個檔案")
        return temp_dir, ssh_info
    else:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None, None


def cleanup_remote_temp(temp_dir: str):
    """清理暫存目錄"""
    if temp_dir and os.path.exists(temp_dir):
        try:
            shutil.rmtree(temp_dir)
            print("[MCP] 已清理暫存目錄")
        except Exception as e:
            print(f"[MCP] 清理暫存目錄失敗: {e}")
