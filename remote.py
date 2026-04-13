#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - MCP 遠端模式（完全獨立模組）

透過 SSH 按需存取遠端主機上的檔案，模型自行決定要讀取哪些資料。
此模組獨立運作，不依賴 agent.py / agent_tools.py 的本地工具系統。

使用方式：
    python main.py --mcp user@host
    python main.py --mcp user@host:port
    python main.py --mcp user@host --kb docs.json
"""

import os
import re
import json
import atexit
import shlex
import subprocess
import tempfile
from typing import Optional, Dict

from config import (
    OLLAMA_CHAT_URL, MODEL, NUM_CTX,
    DYNAMIC_NUM_CTX_ENABLED, DYNAMIC_NUM_CTX_MIN, DYNAMIC_NUM_CTX_MAX,
    DYNAMIC_NUM_CTX_BUFFER, CHARS_PER_TOKEN,
    MAX_FILE_READ_CHARS, MAX_GREP_RESULTS, MAX_TOOL_LOOPS,
)
from http_client import get_session
from utils import print_ctx_usage


# ============================================================
# URI 解析
# ============================================================
def parse_mcp_uri(uri: str) -> Optional[Dict[str, str]]:
    """解析 MCP URI 格式

    支援：
        user@host          → SSH 預設 port，home 目錄
        user@host:port     → 指定 port，home 目錄

    Returns:
        {"user": ..., "host": ..., "port": ...} 或 None
    """
    # user@host:port
    m = re.match(r'^([^@]+)@([^:]+):(\d+)$', uri)
    if m:
        return {"user": m.group(1), "host": m.group(2), "port": m.group(3)}

    # user@host
    m = re.match(r'^([^@]+)@([^:]+)$', uri)
    if m:
        return {"user": m.group(1), "host": m.group(2), "port": "22"}

    return None


# ============================================================
# SSH 遠端工具執行器
# ============================================================
class RemoteToolExecutor:
    """透過 SSH 在遠端執行工具，提供 list_files / read_file / grep / file_info

    使用 SSH ControlMaster 機制：
    - 第一次連線（test_connection）為互動式，可輸入密碼
    - 建立 ControlMaster socket 後，後續所有工具呼叫都複用同一條連線
    - 預設探索範圍是遠端根目錄 `/`，可用 `~` 回到使用者 home
    - 程式結束時自動關閉 socket
    """

    def __init__(self, ssh_info: Dict[str, str]):
        self.user = ssh_info["user"]
        self.host = ssh_info["host"]
        self.port = ssh_info["port"]
        self._root_dir = "/"
        self._home_dir = None
        # ControlMaster socket 路徑
        self._socket_path = os.path.join(
            tempfile.gettempdir(),
            f"mcp_ssh_{self.user}_{self.host}_{self.port}"
        )
        # 程式結束時自動清理 socket
        atexit.register(self._cleanup_socket)

    def _cleanup_socket(self):
        """關閉 ControlMaster 連線"""
        try:
            subprocess.run(
                ["ssh", "-O", "exit",
                 "-o", f"ControlPath={self._socket_path}",
                 f"{self.user}@{self.host}"],
                capture_output=True, timeout=5
            )
        except Exception:
            pass

    def _ssh_cmd_base(self) -> list:
        """SSH 命令前綴（複用 ControlMaster socket）"""
        return [
            "ssh", "-p", self.port,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-o", f"ControlPath={self._socket_path}",
            f"{self.user}@{self.host}",
        ]

    def _run_ssh(self, remote_cmd: str, timeout: int = 30) -> tuple:
        """透過已建立的 ControlMaster 執行遠端命令"""
        cmd = self._ssh_cmd_base() + [remote_cmd]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"SSH 命令逾時（{timeout}s）"
        except Exception as e:
            return -1, "", str(e)

    def test_connection(self) -> tuple:
        """建立 SSH 連線（互動式，支援密碼輸入）

        此方法會建立 ControlMaster socket，後續所有工具呼叫都複用這條連線。
        如果需要密碼，會在終端機顯示密碼提示。
        """
        # 建立 ControlMaster 連線（互動式，stdin/stderr 不攔截，讓密碼提示顯示在終端）
        master_cmd = [
            "ssh", "-p", self.port,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=15",
            "-o", "ControlMaster=yes",
            "-o", f"ControlPath={self._socket_path}",
            "-o", "ControlPersist=600",
            f"{self.user}@{self.host}",
            "echo ok && whoami && pwd",
        ]
        try:
            # 不攔截 stdin/stderr → 密碼提示會顯示在終端
            # 只攔截 stdout → 取得 whoami/pwd 結果
            result = subprocess.run(
                master_cmd,
                stdout=subprocess.PIPE,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                self._home_dir = lines[2] if len(lines) >= 3 else "~"
                return True, f"連線成功（home: {self._home_dir}）"
            return False, "連線失敗（密碼錯誤或主機無法連線）"
        except subprocess.TimeoutExpired:
            return False, "連線逾時（60s）"
        except Exception as e:
            return False, f"連線失敗: {e}"

    @property
    def home_dir(self) -> str:
        if not self._home_dir:
            rc, stdout, _ = self._run_ssh("pwd")
            self._home_dir = stdout.strip() if rc == 0 else "~"
        return self._home_dir

    @property
    def server_root(self) -> str:
        return self._root_dir

    def _resolve_path(self, path: str) -> str:
        if not path or path == ".":
            return self.server_root
        if path == "~":
            return self.home_dir
        if path.startswith("~/"):
            suffix = path[2:].lstrip("/")
            return self.home_dir if not suffix else f"{self.home_dir}/{suffix}"
        if not path.startswith("/"):
            return f"{self.server_root.rstrip('/')}/{path.lstrip('/')}"
        return path

    def list_files(self, path: str = ".", depth: int = 2) -> str:
        depth = min(depth, 4)
        remote_path = self._resolve_path(path)
        quoted_remote_path = shlex.quote(remote_path)

        excludes = (
            r" -name .git -o -name __pycache__ -o -name node_modules"
            r" -o -name .venv -o -name venv -o -name build -o -name dist"
            r" -o -name .cache -o -name .tox"
        )
        cmd = (
            f"find {quoted_remote_path} -maxdepth {depth}"
            f" \\( {excludes} \\) -prune -o -print"
            f" 2>/dev/null | head -500 | sort"
        )
        rc, stdout, stderr = self._run_ssh(cmd)
        if rc != 0 and not stdout:
            return f"錯誤: 無法列出目錄 '{path}' - {stderr.strip()}"
        if not stdout.strip():
            return f"目錄 '{path}' 是空的或不存在"

        lines = stdout.strip().split('\n')
        base = remote_path.rstrip('/')
        result = []
        for line in lines:
            line = line.strip()
            if not line or line == base:
                continue
            rel = line[len(base) + 1:] if line.startswith(base + '/') else line
            indent = "  " * rel.count('/')
            name = rel.split('/')[-1]
            result.append(f"{indent}{name}")

        if not result:
            return f"目錄 '{path}' 是空的"
        return f"=== {remote_path} ===\n" + "\n".join(result[:300])

    def read_file(self, path: str, start_line: int = 1, end_line: Optional[int] = None) -> str:
        remote_path = self._resolve_path(path)
        quoted_remote_path = shlex.quote(remote_path)

        rc, stdout, _ = self._run_ssh(f"wc -l < {quoted_remote_path} 2>/dev/null")
        if rc != 0:
            return f"錯誤: 檔案不存在或無法讀取 '{path}'"
        total = int(stdout.strip()) if stdout.strip().isdigit() else 0

        start_line = max(1, start_line)
        if end_line is None:
            max_lines = MAX_FILE_READ_CHARS // 80
            end_line = min(start_line + max_lines, total)
        else:
            end_line = min(end_line, total)

        cmd = f"sed -n '{start_line},{end_line}p' {quoted_remote_path} 2>/dev/null"
        rc, stdout, stderr = self._run_ssh(cmd, timeout=30)
        if rc != 0:
            return f"錯誤: 無法讀取 '{path}' - {stderr.strip()}"

        if len(stdout) > MAX_FILE_READ_CHARS:
            stdout = stdout[:MAX_FILE_READ_CHARS]
            last_nl = stdout.rfind('\n')
            if last_nl > 0:
                stdout = stdout[:last_nl]
            end_line = start_line + stdout.count('\n')

        lines = stdout.split('\n')
        if lines and lines[-1] == '':
            lines = lines[:-1]
        numbered = [f"{i:4d} | {line}" for i, line in enumerate(lines, start_line)]

        header = f"=== {path} (行 {start_line}-{end_line} / 共 {total} 行) ===\n"
        footer = f"\n... 用 read_file('{path}', {end_line + 1}) 繼續" if end_line < total else ""
        return header + "\n".join(numbered) + footer

    def grep(self, pattern: str, path: str = ".", include: str = None, context: int = 0) -> str:
        remote_path = self._resolve_path(path)
        quoted_remote_path = shlex.quote(remote_path)
        quoted_pattern = shlex.quote(pattern)
        context = min(context, 5)

        rc, _, _ = self._run_ssh("which rg 2>/dev/null")
        has_rg = (rc == 0)

        if has_rg:
            parts = ["rg", "--no-heading", "--color", "never", "--line-number",
                     f"--max-count={MAX_GREP_RESULTS}"]
            if context > 0:
                parts.extend(["-C", str(context)])
            if include:
                for p in include.split(','):
                    p = p.strip()
                    if p:
                        parts.extend(["-g", shlex.quote(p)])
            for exclude in ("!.git/", "!node_modules/", "!__pycache__/", "!.venv/", "!build/", "!dist/"):
                parts.extend(["-g", shlex.quote(exclude)])
            cmd = " ".join(parts) + f" -- {quoted_pattern} {quoted_remote_path} 2>/dev/null | head -200"
        else:
            parts = ["grep", "-rn", "--color=never"]
            if context > 0:
                parts.extend(["-C", str(context)])
            if include:
                for p in include.split(','):
                    p = p.strip()
                    if p:
                        parts.append(f"--include={shlex.quote(p)}")
            parts += ["--exclude-dir=.git", "--exclude-dir=node_modules",
                       "--exclude-dir=__pycache__", "--exclude-dir=.venv",
                       "--exclude-dir=build", "--exclude-dir=dist"]
            cmd = " ".join(parts) + f" -- {quoted_pattern} {quoted_remote_path} 2>/dev/null | head -200"

        rc, stdout, _ = self._run_ssh(cmd, timeout=30)
        if not stdout.strip():
            return f"沒有找到 '{pattern}'"

        lines = stdout.strip().split('\n')
        match_count = len([l for l in lines if re.match(r'^.+?:\d+:', l)])
        return f"=== grep '{pattern}' ({match_count} matches) ===\n" + "\n".join(lines)

    def file_info(self, path: str) -> str:
        remote_path = self._resolve_path(path)
        quoted_remote_path = shlex.quote(remote_path)
        cmd = (
            f"if [ -f {quoted_remote_path} ]; then"
            f"  wc -lc < {quoted_remote_path} | awk '{{print \"file\", $1, $2}}';"
            f"elif [ -d {quoted_remote_path} ]; then"
            f"  find {quoted_remote_path} -type f 2>/dev/null | wc -l | awk '{{print \"dir\", $1}}';"
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
        dispatch = {
            "list_files": lambda: self.list_files(args.get("path", "."), args.get("depth", 2)),
            "read_file": lambda: self.read_file(args.get("path", ""), args.get("start_line", 1), args.get("end_line")),
            "grep": lambda: self.grep(args.get("pattern", ""), args.get("path", "."),
                                      args.get("include"), args.get("context", 0)),
            "file_info": lambda: self.file_info(args.get("path", "")),
        }
        fn = dispatch.get(tool)
        if fn:
            return fn()
        return f"錯誤: MCP 模式不支援工具 '{tool}'（僅支援 list_files, read_file, grep, file_info）"


# ============================================================
# MCP 工具定義（只有基本的 4 個工具，獨立於 agent_tools.py）
# ============================================================
_MCP_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出遠端目錄結構",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目錄路徑，預設 '.'（伺服器根目錄 /）；可用 '~' 表示 home"},
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
            "description": "讀取遠端檔案內容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "檔案路徑（相對於伺服器根目錄 /、絕對路徑，或 ~/...）"},
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
            "description": "在遠端搜尋 pattern",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "搜尋字串"},
                    "path": {"type": "string", "description": "搜尋目錄（預設從伺服器根目錄 / 開始）"},
                    "include": {"type": "string", "description": "檔案過濾，如 '*.py,*.c'"},
                    "context": {"type": "integer", "description": "顯示前後各 N 行上下文（預設 0）"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_info",
            "description": "取得遠端檔案資訊（行數、大小）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "檔案路徑"}
                },
                "required": ["path"]
            }
        }
    },
]


# ============================================================
# MCP 專用 LLM 呼叫
# ============================================================
def _compute_dynamic_num_ctx(messages: list) -> int:
    if not DYNAMIC_NUM_CTX_ENABLED:
        return NUM_CTX
    total_chars = sum(len(m.get("content", "")) for m in messages if isinstance(m.get("content"), str))
    target = int((total_chars / CHARS_PER_TOKEN) * DYNAMIC_NUM_CTX_BUFFER)
    target = ((target + 2047) // 2048) * 2048
    return max(DYNAMIC_NUM_CTX_MIN, min(DYNAMIC_NUM_CTX_MAX, target))


def _call_llm_with_tools(messages: list) -> dict:
    num_ctx = _compute_dynamic_num_ctx(messages)
    try:
        session = get_session()
        resp = session.post(OLLAMA_CHAT_URL, json={
            "model": MODEL,
            "messages": messages,
            "tools": _MCP_TOOLS,
            "stream": False,
            "options": {"num_ctx": num_ctx, "temperature": 0.0},
        }, timeout=600)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            return {"content": f"[ERROR] {data['error']}", "tool_calls": []}
        msg = data.get("message", {})
        return {"content": msg.get("content", ""), "tool_calls": msg.get("tool_calls", [])}
    except Exception as e:
        return {"content": f"[ERROR] {e}", "tool_calls": []}


def _call_llm_stream(messages: list) -> str:
    """最後一輪串流輸出"""
    import time
    num_ctx = _compute_dynamic_num_ctx(messages)
    try:
        session = get_session()
        resp = session.post(OLLAMA_CHAT_URL, json={
            "model": MODEL,
            "messages": messages,
            "tools": _MCP_TOOLS,
            "stream": True,
            "options": {"num_ctx": num_ctx, "temperature": 0.0},
        }, timeout=600, stream=True)
        resp.raise_for_status()

        full = []
        buf = []
        last_flush = time.time()
        for line in resp.iter_lines():
            if line:
                try:
                    chunk = json.loads(line)
                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        full.append(token)
                        buf.append(token)
                        now = time.time()
                        if '\n' in token or len(''.join(buf)) >= 20 or (now - last_flush) >= 0.1:
                            print(''.join(buf), end="", flush=True)
                            buf = []
                            last_flush = now
                except json.JSONDecodeError:
                    pass
        if buf:
            print(''.join(buf), end="", flush=True)
        print()
        return "".join(full)
    except Exception as e:
        return f"[ERROR] {e}"


# ============================================================
# MCP Agent 主迴圈（完全獨立）
# ============================================================
def run_mcp_agent(executor: RemoteToolExecutor, question: str,
                  dir_listing: str = "", knowledge_ctx: str = ""):
    """MCP 專用 Agent — 獨立於本地 agent.py

    Args:
        executor: RemoteToolExecutor 實例
        question: 使用者問題
        dir_listing: 預掃描的目錄結構（注入 system prompt）
        knowledge_ctx: 知識庫上下文（可選，來自 --kb）
    """
    host_label = f"{executor.user}@{executor.host}"
    root = executor.server_root
    home = executor.home_dir

    kb_section = f"\n【知識庫參考】\n{knowledge_ctx}\n" if knowledge_ctx else ""

    system_prompt = f"""你是遠端主機分析 Agent。透過 SSH 工具探索 {host_label} 上你有權限讀取的檔案來回答用戶問題。

遠端主機: {host_label}
伺服器根目錄: {root}
使用者 home 目錄: {home}

【初始目錄摘要（從伺服器根目錄開始）】
{dir_listing}
{kb_section}
【路徑規則】
1. 相對路徑一律視為相對於伺服器根目錄 `/`
2. `~` 或 `~/...` 代表使用者 home 目錄
3. grep/read_file 若回傳絕對路徑，後續直接沿用原路徑

【回答規則】
1. 優先根據工具取得的實際檔案內容回答，不要猜測
2. 若有 [REF] 知識庫參考，必須標註引用來源
3. 若資訊不足，明確說明還需要查看哪些檔案
4. 使用繁體中文回答

【工具使用規則】
1. 你已經有伺服器根目錄的初始摘要，不要重複列出 `/`
2. 需要縮小範圍時，用 list_files 探索特定子目錄
3. 需要看某個檔案時，用 read_file 精準讀取
4. 需要搜尋時，用 grep 並盡量加上適當路徑範圍
5. 收集到足夠資訊後，直接用文字回答"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]

    tool_history = []

    for i in range(MAX_TOOL_LOOPS):
        print(f"[MCP] Agent 第 {i+1} 輪...")
        print_ctx_usage(sum(len(m.get("content", "")) for m in messages if isinstance(m.get("content"), str)))

        response = _call_llm_with_tools(messages)
        tool_calls = response.get("tool_calls", [])

        # 沒有工具呼叫 = 模型準備回答了
        if not tool_calls:
            content = response.get("content", "")
            if content and len(content) > 50:
                print(f"[MCP] Agent 完成分析\n")
                print(content)
                return content
            # 回答太短，催一下
            messages.append({"role": "assistant", "content": content or "..."})
            messages.append({"role": "user", "content": "請繼續探索或直接回答問題。"})
            continue

        # 執行工具
        for tc in tool_calls:
            func = tc.get("function", {})
            tool_name = func.get("name", "")
            args_raw = func.get("arguments", {})
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw

            print(f"   [TOOL] {tool_name}({args})")

            # 重複檢查
            call_key = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
            if call_key in tool_history:
                print(f"   [WARN] 跳過重複呼叫")
                result = "已經呼叫過，請用其他工具或直接回答"
            else:
                tool_history.append(call_key)
                result = executor.execute(tool_name, args)

            preview = result[:150] + "..." if result and len(result) > 150 else result
            print(f"   [RESULT] {preview}")

            messages.append({"role": "assistant", "content": "", "tool_calls": [tc]})
            messages.append({"role": "tool", "tool_name": tool_name, "content": result or "（無結果）"})

    # 達到上限，強制總結
    print("[MCP] 達到最大探索次數，請求總結\n")
    messages.append({"role": "user", "content": "請根據目前收集到的資訊回答問題。若資訊不足請說明。"})
    return _call_llm_stream(messages)
