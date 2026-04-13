#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - MCP 遠端模式

透過 SSH 按需存取遠端主機上的檔案，模型自行決定要讀取哪些資料。
不搬運檔案到本地，所有操作都在遠端執行。

使用方式：
    python main.py --mcp user@host
    python main.py --mcp user@host:port

概念：
    類似 MCP（Model Context Protocol），提供工具讓 LLM 按需存取遠端資料。
    LLM 自行決定要讀什麼檔案、搜尋什麼內容，而非一次全部搬運。
"""

import os
import re
import subprocess
from typing import Optional, Dict

from config import MAX_FILE_READ_CHARS, MAX_GREP_RESULTS


def parse_mcp_uri(uri: str) -> Optional[Dict[str, str]]:
    """解析 MCP URI 格式

    支援：
        user@host          → SSH 預設 port，home 目錄
        user@host:port     → 指定 port，home 目錄

    Returns:
        {"user": "kjwang", "host": "140.96.28.10", "port": "22"}
        或 None（解析失敗）
    """
    # 格式: user@host:port
    m = re.match(r'^([^@]+)@([^:]+):(\d+)$', uri)
    if m:
        return {
            "user": m.group(1),
            "host": m.group(2),
            "port": m.group(3),
        }

    # 格式: user@host
    m = re.match(r'^([^@]+)@([^:]+)$', uri)
    if m:
        return {
            "user": m.group(1),
            "host": m.group(2),
            "port": "22",
        }

    return None


class RemoteToolExecutor:
    """透過 SSH 在遠端執行工具命令

    提供與本地 ToolExecutor 相同的介面（list_files, read_file, grep, file_info），
    但所有操作都透過 SSH 在遠端主機上執行。
    """

    def __init__(self, ssh_info: Dict[str, str]):
        self.user = ssh_info["user"]
        self.host = ssh_info["host"]
        self.port = ssh_info["port"]
        self._home_dir = None  # lazy init

    def _ssh_cmd_base(self) -> list:
        """SSH 命令前綴"""
        return [
            "ssh",
            "-p", self.port,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes",
            f"{self.user}@{self.host}",
        ]

    def _run_ssh(self, remote_cmd: str, timeout: int = 30) -> tuple:
        """執行遠端命令

        Returns:
            (returncode, stdout, stderr)
        """
        cmd = self._ssh_cmd_base() + [remote_cmd]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"SSH 命令逾時（{timeout}s）"
        except Exception as e:
            return -1, "", str(e)

    def test_connection(self) -> tuple:
        """測試 SSH 連線

        Returns:
            (success: bool, message: str)
        """
        rc, stdout, stderr = self._run_ssh("echo ok && whoami && pwd", timeout=15)
        if rc == 0:
            lines = stdout.strip().split('\n')
            home = lines[2] if len(lines) >= 3 else "~"
            self._home_dir = home
            return True, f"連線成功（home: {home}）"
        else:
            return False, f"連線失敗: {stderr.strip()}"

    @property
    def home_dir(self) -> str:
        if not self._home_dir:
            rc, stdout, _ = self._run_ssh("pwd")
            self._home_dir = stdout.strip() if rc == 0 else "~"
        return self._home_dir

    def list_files(self, path: str = ".", depth: int = 2) -> str:
        """列出遠端目錄結構"""
        depth = min(depth, 4)
        # 將相對路徑轉為基於 home 的絕對路徑
        if path == "." or not path:
            remote_path = self.home_dir
        elif not path.startswith("/"):
            remote_path = f"{self.home_dir}/{path}"
        else:
            remote_path = path

        # 使用 find 列出目錄結構，排除常見垃圾目錄
        excludes = (
            r" -name .git -o -name __pycache__ -o -name node_modules"
            r" -o -name .venv -o -name venv -o -name build -o -name dist"
            r" -o -name .cache -o -name .tox"
        )
        cmd = (
            f"find {remote_path} -maxdepth {depth}"
            f" \\( {excludes} \\) -prune -o -print"
            f" 2>/dev/null | head -500 | sort"
        )

        rc, stdout, stderr = self._run_ssh(cmd)
        if rc != 0 and not stdout:
            return f"錯誤: 無法列出目錄 '{path}' - {stderr.strip()}"

        if not stdout.strip():
            return f"目錄 '{path}' 是空的或不存在"

        # 格式化輸出：將 find 結果轉為樹狀結構
        lines = stdout.strip().split('\n')
        result = []
        base = remote_path.rstrip('/')
        for line in lines:
            line = line.strip()
            if not line or line == base:
                continue
            # 計算相對路徑
            if line.startswith(base + '/'):
                rel = line[len(base) + 1:]
            else:
                rel = line
            indent_level = rel.count('/')
            name = rel.split('/')[-1]
            indent = "  " * indent_level
            result.append(f"{indent}{name}")

        if not result:
            return f"目錄 '{path}' 是空的"

        return f"=== {remote_path} ===\n" + "\n".join(result[:300])

    def read_file(self, path: str, start_line: int = 1, end_line: Optional[int] = None) -> str:
        """讀取遠端檔案內容"""
        if not path.startswith("/"):
            remote_path = f"{self.home_dir}/{path}"
        else:
            remote_path = path

        # 先取得總行數
        rc, stdout, _ = self._run_ssh(f"wc -l < {remote_path} 2>/dev/null")
        if rc != 0:
            return f"錯誤: 檔案不存在或無法讀取 '{path}'"

        total = int(stdout.strip()) if stdout.strip().isdigit() else 0

        start_line = max(1, start_line)

        # 計算 end_line
        if end_line is None:
            # 用字元數限制推算行數（粗估每行 80 字元）
            max_lines = MAX_FILE_READ_CHARS // 80
            end_line = min(start_line + max_lines, total)
        else:
            end_line = min(end_line, total)

        # 使用 sed 讀取指定行範圍
        cmd = f"sed -n '{start_line},{end_line}p' {remote_path} 2>/dev/null"
        rc, stdout, stderr = self._run_ssh(cmd, timeout=30)

        if rc != 0:
            return f"錯誤: 無法讀取 '{path}' - {stderr.strip()}"

        # 截斷過大的輸出
        if len(stdout) > MAX_FILE_READ_CHARS:
            stdout = stdout[:MAX_FILE_READ_CHARS]
            # 找到最後一個完整行
            last_nl = stdout.rfind('\n')
            if last_nl > 0:
                stdout = stdout[:last_nl]
            actual_lines = stdout.count('\n')
            end_line = start_line + actual_lines

        # 加行號
        lines = stdout.split('\n')
        # 移除最後一個空行（sed 輸出結尾）
        if lines and lines[-1] == '':
            lines = lines[:-1]
        numbered = [f"{i:4d} | {line}" for i, line in enumerate(lines, start_line)]

        header = f"=== {path} (行 {start_line}-{end_line} / 共 {total} 行) ===\n"

        footer = ""
        if end_line < total:
            footer = f"\n... 用 read_file('{path}', {end_line + 1}) 繼續"

        return header + "\n".join(numbered) + footer

    def grep(self, pattern: str, path: str = ".", include: str = None, context: int = 0) -> str:
        """在遠端搜尋 pattern"""
        if path == "." or not path:
            remote_path = self.home_dir
        elif not path.startswith("/"):
            remote_path = f"{self.home_dir}/{path}"
        else:
            remote_path = path

        context = min(context, 5)

        # 構建 grep 命令（優先用 rg，fallback 到 grep）
        # 先檢查遠端是否有 rg
        rc, _, _ = self._run_ssh("which rg 2>/dev/null")
        has_rg = (rc == 0)

        if has_rg:
            cmd_parts = [
                "rg", "--no-heading", "--color", "never", "--line-number",
                f"--max-count={MAX_GREP_RESULTS}",
            ]
            if context > 0:
                cmd_parts += [f"-C {context}"]
            if include:
                for p in include.split(','):
                    p = p.strip()
                    if p:
                        cmd_parts.append(f"-g '{p}'")
            # 排除目錄
            cmd_parts += [
                "-g '!.git/'", "-g '!node_modules/'", "-g '!__pycache__/'",
                "-g '!.venv/'", "-g '!build/'", "-g '!dist/'",
            ]
            # 用 -- 分隔 pattern 和 path，避免 pattern 被誤解為 flag
            cmd = " ".join(cmd_parts) + f" -- '{pattern}' {remote_path} 2>/dev/null | head -200"
        else:
            # fallback grep
            cmd_parts = ["grep", "-rn", "--color=never"]
            if context > 0:
                cmd_parts.append(f"-C {context}")
            if include:
                for p in include.split(','):
                    p = p.strip()
                    if p:
                        cmd_parts.append(f"--include='{p}'")
            # 排除目錄
            cmd_parts += [
                "--exclude-dir=.git", "--exclude-dir=node_modules",
                "--exclude-dir=__pycache__", "--exclude-dir=.venv",
                "--exclude-dir=build", "--exclude-dir=dist",
            ]
            cmd = " ".join(cmd_parts) + f" '{pattern}' {remote_path} 2>/dev/null | head -200"

        rc, stdout, stderr = self._run_ssh(cmd, timeout=30)

        if not stdout.strip():
            return f"沒有找到 '{pattern}'"

        # 將絕對路徑替換為相對路徑（更容易閱讀）
        base = self.home_dir.rstrip('/') + '/'
        stdout = stdout.replace(base, '')

        lines = stdout.strip().split('\n')
        match_count = len([l for l in lines if re.match(r'^.+?:\d+:', l)])

        header = f"=== grep '{pattern}' ({match_count} matches) ===\n"
        return header + "\n".join(lines)

    def file_info(self, path: str) -> str:
        """取得遠端檔案資訊"""
        if not path.startswith("/"):
            remote_path = f"{self.home_dir}/{path}"
        else:
            remote_path = path

        cmd = (
            f"if [ -f {remote_path} ]; then"
            f"  wc -lc < {remote_path} | awk '{{print \"file\", $1, $2}}';"
            f"elif [ -d {remote_path} ]; then"
            f"  find {remote_path} -type f 2>/dev/null | wc -l | awk '{{print \"dir\", $1}}';"
            f"else"
            f"  echo 'notfound';"
            f"fi"
        )

        rc, stdout, _ = self._run_ssh(cmd)
        output = stdout.strip()

        if output == 'notfound' or rc != 0:
            return f"錯誤: 不存在 '{path}'"

        parts = output.split()
        if parts[0] == 'file' and len(parts) >= 3:
            return f"{path}: 檔案, {parts[1]} 行, {parts[2]} 字元"
        elif parts[0] == 'dir' and len(parts) >= 2:
            return f"{path}: 目錄, {parts[1]} 個檔案"

        return f"{path}: {output}"

    def execute(self, tool: str, args: dict) -> Optional[str]:
        """統一工具執行介面（與 ToolExecutor 相同簽名）"""
        if tool == "list_files":
            return self.list_files(args.get("path", "."), args.get("depth", 2))
        elif tool == "read_file":
            return self.read_file(args.get("path", ""), args.get("start_line", 1), args.get("end_line"))
        elif tool == "grep":
            return self.grep(args.get("pattern", ""), args.get("path", "."),
                           args.get("include"), args.get("context", 0))
        elif tool == "file_info":
            return self.file_info(args.get("path", ""))
        else:
            return f"錯誤: MCP 模式不支援工具 '{tool}'（僅支援 list_files, read_file, grep, file_info）"
