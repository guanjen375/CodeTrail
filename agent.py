#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - Agent 模式
"""

import os
import re
import json
import fnmatch
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from http_client import get_session

import config  # 用於動態存取 RUN_COMMAND_ENABLED, PATCH_ENABLED 等旗標
import container_runner  # 用於動態存取 CONTAINER_ENABLED
from config import (
    OLLAMA_CHAT_URL, MODEL, NUM_CTX,
    DYNAMIC_NUM_CTX_ENABLED, DYNAMIC_NUM_CTX_MIN, DYNAMIC_NUM_CTX_MAX,
    DYNAMIC_NUM_CTX_BUFFER, CHARS_PER_TOKEN,
    MAX_TOOL_LOOPS, MAX_FILE_READ_CHARS, MAX_GREP_RESULTS, MAX_LIST_DEPTH,
    MAX_MESSAGES_BUDGET, MIN_RECENT_TOOL_OUTPUTS,
    IGNORED_PATTERNS, GREP_DEFAULT_EXTENSIONS, ALLOWED_DOT_DIRS,
    CODE_RAG_ENABLED, CODE_RAG_TOP_K, CODE_RAG_TOP_K_BUG,
    CODE_RAG_PREREAD_TOP_K, CODE_RAG_PREREAD_TOP_K_BUG,
    CODE_RAG_PREREAD_LINES, CODE_RAG_PREREAD_LINES_BUG, CODE_RAG_PREREAD_MAX_LINES,
    RUN_COMMAND_TIMEOUT, RUN_COMMAND_MAX_OUTPUT,
    RUN_COMMAND_TAIL_RATIO, RUN_COMMAND_ERROR_PATTERNS,
    ALLOWED_COMMANDS,
    # 改碼閉環相關設定（注意：PATCH_ENABLED 需用 config.PATCH_ENABLED 讀取）
    PATCH_MAX_FILES, PATCH_MAX_LINES_PER_FILE,
    LINT_COMMANDS
)
from utils import (
    should_ignore_dir, should_ignore_file, call_llm, call_llm_stream,
    should_use_strict_mode, answer_with_self_check
)


# ============================================================
# 智能輸出裁切
# ============================================================
def smart_truncate_output(output: str, max_chars: int, tail_ratio: float = 0.7,
                          error_patterns: list = None) -> str:
    """智能裁切輸出，保留重要的錯誤資訊

    策略：
    1. 測試輸出優先保留尾巴（錯誤訊息通常在尾部）
    2. 優先保留包含 error_patterns 的行
    3. 頭尾比例由 tail_ratio 決定

    Args:
        output: 原始輸出
        max_chars: 最大字元數
        tail_ratio: 尾巴保留比例（預設 0.7 = 保留 70% 尾巴）
        error_patterns: 關鍵錯誤 pattern 列表
    """
    if len(output) <= max_chars:
        return output

    if error_patterns is None:
        error_patterns = RUN_COMMAND_ERROR_PATTERNS

    lines = output.split('\n')
    total_lines = len(lines)

    # 找出包含錯誤 pattern 的行
    important_line_indices = set()
    for i, line in enumerate(lines):
        for pattern in error_patterns:
            if pattern in line:
                # 保留該行及其上下文（前後各 3 行）
                for j in range(max(0, i - 3), min(total_lines, i + 4)):
                    important_line_indices.add(j)
                break

    # 計算頭尾字元數
    head_chars = int(max_chars * (1 - tail_ratio))
    tail_chars = max_chars - head_chars

    # 收集頭部內容
    head_content = []
    head_len = 0
    head_line_end = 0
    for i, line in enumerate(lines):
        if head_len + len(line) + 1 > head_chars:
            break
        head_content.append(line)
        head_len += len(line) + 1
        head_line_end = i + 1

    # 收集尾部內容（從尾巴往前）
    tail_content = []
    tail_len = 0
    tail_line_start = total_lines
    for i in range(total_lines - 1, -1, -1):
        line = lines[i]
        if tail_len + len(line) + 1 > tail_chars:
            break
        tail_content.insert(0, line)
        tail_len += len(line) + 1
        tail_line_start = i

    # 檢查是否有重要行被截斷
    skipped_important = []
    for idx in sorted(important_line_indices):
        if head_line_end <= idx < tail_line_start:
            skipped_important.append((idx, lines[idx][:100]))

    # 組合結果
    skipped_count = tail_line_start - head_line_end
    truncated = len(output) - head_len - tail_len

    result_parts = []
    result_parts.append('\n'.join(head_content))

    if skipped_count > 0:
        # 如果有重要行被截斷，顯示摘要
        if skipped_important:
            important_summary = '\n'.join(
                f"  [{idx+1}] {line}..." for idx, line in skipped_important[:5]
            )
            result_parts.append(
                f"\n\n... [略過 {skipped_count} 行，約 {truncated} 字元] ...\n"
                f"[重要行摘要]:\n{important_summary}\n"
            )
        else:
            result_parts.append(
                f"\n\n... [略過 {skipped_count} 行，約 {truncated} 字元] ...\n\n"
            )

    result_parts.append('\n'.join(tail_content))
    return ''.join(result_parts)


# ============================================================
# Native Tools 定義
# ============================================================
_BASE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出目錄結構",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目錄路徑，預設 '.'"},
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
            "description": "讀取檔案內容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "檔案路徑"},
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
            "description": "搜尋 pattern（支援上下文顯示）",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "搜尋字串"},
                    "path": {"type": "string", "description": "搜尋目錄"},
                    "include": {"type": "string", "description": "檔案過濾"},
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
            "description": "取得檔案資訊",
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

_RUN_COMMAND_TOOL = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": "執行測試命令（白名單：pytest, ctest, npm test, cargo test, go test）",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要執行的命令，如 'pytest test_xxx.py -v' 或 'go test ./...'"},
                "timeout": {"type": "integer", "description": "超時秒數，預設 60"}
            },
            "required": ["command"]
        }
    }
}

# ============================================================
# 改碼閉環工具定義
# ============================================================
_APPLY_PATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "apply_patch",
        "description": "套用 unified diff 格式的程式碼修改。修改會直接寫入檔案。",
        "parameters": {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": "unified diff 格式的修改內容，例如：\n--- a/file.py\n+++ b/file.py\n@@ -10,3 +10,4 @@\n context line\n-old line\n+new line\n+added line"
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "若為 true，只顯示會修改什麼，不實際寫入（預設 false）"
                }
            },
            "required": ["patch"]
        }
    }
}

_GIT_STATUS_TOOL = {
    "type": "function",
    "function": {
        "name": "git_status",
        "description": "顯示 git 工作目錄狀態（已修改、已暫存、未追蹤的檔案）",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
}

_GIT_DIFF_TOOL = {
    "type": "function",
    "function": {
        "name": "git_diff",
        "description": "顯示檔案的 git diff（工作目錄與 HEAD 的差異）",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "檔案路徑（可選，不指定則顯示所有差異）"},
                "staged": {"type": "boolean", "description": "若為 true，顯示已暫存的差異（預設 false）"}
            },
            "required": []
        }
    }
}

_RUN_LINT_TOOL = {
    "type": "function",
    "function": {
        "name": "run_lint",
        "description": "對檔案執行 lint/format 工具（自動根據檔案類型選擇工具）",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要 lint 的檔案路徑"},
                "fix": {"type": "boolean", "description": "若為 true，自動修復問題（預設 true）"}
            },
            "required": ["path"]
        }
    }
}

def _get_native_tools() -> list:
    """動態決定要包含哪些工具

    改進：使用函數而非常量，讓 --run-tests/--patch 可以在 main.py 處理完參數後生效
    """
    tools = list(_BASE_TOOLS)

    if config.RUN_COMMAND_ENABLED:
        tools.append(_RUN_COMMAND_TOOL)

    if config.PATCH_ENABLED:
        tools.extend([_APPLY_PATCH_TOOL, _GIT_STATUS_TOOL, _GIT_DIFF_TOOL, _RUN_LINT_TOOL])

    return tools


def _compute_dynamic_num_ctx(messages: list) -> int:
    """根據 messages 長度動態計算 num_ctx

    策略：
    1. 估算 prompt token 數（chars / CHARS_PER_TOKEN）
    2. 乘以 BUFFER 倍數預留回答空間
    3. 向上取到 2048 的倍數
    4. 限制在 MIN ~ MAX 範圍內
    """
    if not DYNAMIC_NUM_CTX_ENABLED:
        return NUM_CTX

    # 計算所有 messages 的總字元數
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            # multi-modal content
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total_chars += len(part.get("text", ""))

    # 估算 token 數
    estimated_tokens = int(total_chars / CHARS_PER_TOKEN)

    # 乘以 buffer 預留回答空間
    target_ctx = int(estimated_tokens * DYNAMIC_NUM_CTX_BUFFER)

    # 向上取到 2048 的倍數（減少 KV cache 重新分配）
    target_ctx = ((target_ctx + 2047) // 2048) * 2048

    # 限制範圍
    target_ctx = max(DYNAMIC_NUM_CTX_MIN, min(DYNAMIC_NUM_CTX_MAX, target_ctx))

    return target_ctx


def call_llm_with_tools(messages: list, temperature: float = 0.0) -> dict:
    """呼叫 LLM（帶工具）"""
    # 動態計算 num_ctx
    num_ctx = _compute_dynamic_num_ctx(messages)

    try:
        session = get_session()
        resp = session.post(OLLAMA_CHAT_URL, json={
            "model": MODEL,
            "messages": messages,
            "tools": _get_native_tools(),
            "stream": False,
            "options": {"num_ctx": num_ctx, "temperature": temperature},
        }, timeout=600)
        resp.raise_for_status()
        data = resp.json()

        message = data.get("message", {})

        return {
            "content": message.get("content", ""),
            "tool_calls": message.get("tool_calls", []),
            "done_reason": data.get("done_reason", "stop")
        }
    except Exception as e:
        err_type = type(e).__name__
        if "ConnectionError" in err_type:
            return {"content": "[ERROR] 無法連接 Ollama", "tool_calls": [], "done_reason": "error"}
        elif "Timeout" in err_type:
            return {"content": "[ERROR] 請求超時", "tool_calls": [], "done_reason": "error"}
        else:
            return {"content": f"[ERROR] 錯誤: {e}", "tool_calls": [], "done_reason": "error"}


def call_llm_with_tools_stream(messages: list, temperature: float = 0.0) -> str:
    """呼叫 LLM（帶工具，串流輸出，批次顯示）

    改進：批次輸出減少 I/O 開銷，每累積一定字數或遇到換行時才 flush
    """
    import time

    # 動態計算 num_ctx
    num_ctx = _compute_dynamic_num_ctx(messages)

    try:
        session = get_session()
        resp = session.post(OLLAMA_CHAT_URL, json={
            "model": MODEL,
            "messages": messages,
            "tools": _get_native_tools(),
            "stream": True,
            "options": {"num_ctx": num_ctx, "temperature": temperature},
        }, timeout=600, stream=True)
        resp.raise_for_status()

        full_response = []
        buffer = []
        buffer_chars = 0
        last_flush = time.time()
        BATCH_SIZE = 20  # 累積 20 字元或 100ms 後 flush
        FLUSH_INTERVAL = 0.1  # 100ms

        for line in resp.iter_lines():
            if line:
                try:
                    chunk = json.loads(line)
                    message = chunk.get("message", {})
                    token = message.get("content", "")
                    if token:
                        full_response.append(token)
                        buffer.append(token)
                        buffer_chars += len(token)

                        # 遇到換行、累積足夠字數、或超時則 flush
                        now = time.time()
                        should_flush = (
                            '\n' in token or
                            buffer_chars >= BATCH_SIZE or
                            (now - last_flush) >= FLUSH_INTERVAL
                        )

                        if should_flush and buffer:
                            print(''.join(buffer), end="", flush=True)
                            buffer = []
                            buffer_chars = 0
                            last_flush = now

                except json.JSONDecodeError:
                    pass

        # 輸出剩餘的 buffer
        if buffer:
            print(''.join(buffer), end="", flush=True)

        print()  # 換行
        return "".join(full_response)

    except Exception as e:
        err_type = type(e).__name__
        if "ConnectionError" in err_type:
            return "[ERROR] 無法連接 Ollama"
        elif "Timeout" in err_type:
            return "[ERROR] 請求超時"
        else:
            return f"[ERROR] 錯誤: {e}"


# ============================================================
# Tool Executor
# ============================================================
class ToolExecutor:
    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def _safe_path(self, path: str) -> Optional[Path]:
        try:
            full = (self.root / path).resolve()
            full.relative_to(self.root)
            return full
        except ValueError:
            return None

    def list_files(self, path: str = ".", depth: int = 2) -> str:
        depth = min(depth, MAX_LIST_DEPTH)
        target = self._safe_path(path)

        if not target or not target.exists():
            return f"錯誤: 路徑不存在 '{path}'"
        if not target.is_dir():
            return f"錯誤: '{path}' 不是目錄"

        lines = []
        self._tree(target, "", depth, lines)
        return "\n".join(lines) if lines else f"目錄 '{path}' 是空的"

    def _tree(self, dir_path: Path, prefix: str, depth: int, lines: list):
        if depth < 0:
            return

        try:
            items = sorted(dir_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError:
            return

        valid_items = []
        for item in items:
            try:
                if item.is_symlink() and not item.exists():
                    continue
                rel_path = item.relative_to(self.root)
                # 統一使用 should_ignore_dir 判斷（已包含 ALLOWED_DOT_DIRS 邏輯）
                if item.is_dir() and should_ignore_dir(rel_path):
                    continue
                # 檔案：跳過隱藏檔，但允許 ALLOWED_DOT_DIRS 內的檔案
                if item.is_file() and item.name.startswith('.'):
                    # 檢查是否在允許的 dot 目錄內
                    if not any(part.lower() in ALLOWED_DOT_DIRS for part in rel_path.parts[:-1]):
                        continue
                valid_items.append(item)
            except (OSError, ValueError):
                continue

        for i, item in enumerate(valid_items):
            is_last = (i == len(valid_items) - 1)
            conn = "└── " if is_last else "├── "

            try:
                if item.is_dir():
                    lines.append(f"{prefix}{conn}[DIR] {item.name}/")
                    if depth > 0:
                        ext = "    " if is_last else "│   "
                        self._tree(item, prefix + ext, depth - 1, lines)
                else:
                    size = item.stat().st_size
                    sz = f"{size}B" if size < 1024 else f"{size/1024:.1f}KB"
                    lines.append(f"{prefix}{conn}[FILE] {item.name} ({sz})")
            except (OSError, FileNotFoundError):
                continue

    def read_file(self, path: str, start_line: int = 1, end_line: Optional[int] = None) -> str:
        target = self._safe_path(path)

        if not target or not target.exists():
            return f"錯誤: 檔案不存在 '{path}'"
        if not target.is_file():
            return f"錯誤: '{path}' 不是檔案"

        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"錯誤: {e}"

        lines = content.split('\n')
        total = len(lines)

        start_line = max(1, start_line)
        if end_line is None:
            char_count = 0
            end_line = start_line
            for i in range(start_line - 1, total):
                char_count += len(lines[i]) + 1
                if char_count > MAX_FILE_READ_CHARS:
                    break
                end_line = i + 1
        else:
            end_line = min(end_line, total)

        selected = lines[start_line - 1:end_line]
        numbered = [f"{i:4d} | {line}" for i, line in enumerate(selected, start_line)]

        header = f"=== {path} (行 {start_line}-{end_line} / 共 {total} 行) ===\n"
        footer = f"\n... 用 read_file('{path}', {end_line + 1}) 繼續" if end_line < total else ""

        return header + "\n".join(numbered) + footer

    def _is_redos_risk(self, pattern: str) -> bool:
        """檢查 pattern 是否有 ReDoS 風險

        ReDoS 風險模式：
        - 嵌套量詞：(a+)+, (a*)+, (a+)*, (a*)*
        - 交替與量詞組合：(a|b)+, (a|aa)+
        - 重複的重疊模式：.*.*

        保守策略：只檢查明顯的危險模式，允許大部分正常 regex
        """
        # 嵌套量詞：(...)+ 或 (...)* 內部還有 +, *, {n,}
        if re.search(r'\([^)]*[+*][^)]*\)[+*]', pattern):
            return True

        # 多個連續的 .* 或 .+
        if re.search(r'\.\*.*\.\*', pattern) or re.search(r'\.\+.*\.\+', pattern):
            return True

        # 過長的 pattern（可能是惡意構造）
        if len(pattern) > 500:
            return True

        return False

    def grep(self, pattern: str, path: str = ".", include: str = None, context: int = 0) -> str:
        """搜尋 pattern

        Args:
            pattern: 搜尋字串
            path: 搜尋目錄
            include: 檔案過濾，支持逗號分隔的多個 glob（如 "*.py,*.c"）
                     預設只搜尋程式碼檔案，避免掃到圖片/二進位檔
            context: 顯示前後各 N 行上下文（預設 0）

        改進：
        - 先做 case-sensitive，沒結果再 IGNORECASE
        - 支援顯示前後各 N 行，減少後續 read_file 調用
        - ReDoS 保護：檢查危險 pattern 並自動 escape
        """
        target = self._safe_path(path)
        if not target or not target.exists():
            return f"錯誤: 路徑不存在 '{path}'"

        # ReDoS 保護：檢查危險 pattern
        use_literal = self._is_redos_risk(pattern)
        if use_literal:
            # 強制使用 literal 搜尋
            escaped = re.escape(pattern)
            regex_cs = re.compile(escaped)
            regex_ci = re.compile(escaped, re.IGNORECASE)
        else:
            # 先嘗試 case-sensitive，通常更精準
            try:
                regex_cs = re.compile(pattern)
                regex_ci = re.compile(pattern, re.IGNORECASE)
            except re.error:
                escaped = re.escape(pattern)
                regex_cs = re.compile(escaped)
                regex_ci = re.compile(escaped, re.IGNORECASE)

        # 使用預設的程式碼檔案過濾，避免掃到圖片/二進位檔
        if include is None:
            include = GREP_DEFAULT_EXTENSIONS

        # 支持逗號分隔的多個 glob
        include_patterns = [p.strip() for p in include.split(',')]

        files = []
        if target.is_file():
            files = [target]
        else:
            for dirpath, dirnames, filenames in os.walk(target):
                # 使用統一的目錄過濾邏輯
                rel_dir = Path(dirpath).relative_to(target)
                dirnames[:] = [d for d in dirnames if not should_ignore_dir(rel_dir / d)]

                for fname in filenames:
                    # 檢查是否符合任一 include pattern
                    if any(fnmatch.fnmatch(fname, p) for p in include_patterns):
                        fp = Path(dirpath) / fname
                        # 使用相對路徑，讓 should_ignore_file 可匹配 docs/** 等 pattern
                        rel_path = str(fp.relative_to(target))
                        if not should_ignore_file(rel_path):
                            files.append(fp)

        # 先用 case-sensitive 搜尋
        results = self._grep_with_context(files, regex_cs, context)

        # 如果沒結果，用 case-insensitive 重試
        if not results:
            results = self._grep_with_context(files, regex_ci, context)

        if not results:
            return f"沒有找到 '{pattern}'"

        return f"=== grep '{pattern}' ({len(results)} 結果) ===\n" + "\n".join(results)

    def _safe_regex_search(self, regex, text: str, timeout_chars: int = 10000) -> bool:
        """安全的 regex search，對超長行做截斷保護

        ReDoS 攻擊通常依賴超長輸入，截斷可有效防禦
        """
        # 截斷超長行（通常程式碼不會有超過 10000 字元的單行）
        if len(text) > timeout_chars:
            text = text[:timeout_chars]
        return regex.search(text) is not None

    def _grep_with_context(self, files: list, regex, context: int) -> list:
        """搜尋檔案並支援上下文顯示"""
        results = []
        context = min(context, 5)  # 最多顯示前後 5 行

        for fp in files:
            if len(results) >= MAX_GREP_RESULTS:
                break
            try:
                content = fp.read_text(encoding="utf-8", errors="replace")
                lines = content.split('\n')

                for i, line in enumerate(lines):
                    if self._safe_regex_search(regex, line):
                        rel = fp.relative_to(self.root)

                        if context > 0:
                            # 顯示上下文
                            start = max(0, i - context)
                            end = min(len(lines), i + context + 1)
                            ctx_lines = []
                            for j in range(start, end):
                                prefix = ">" if j == i else " "
                                ctx_lines.append(f"{prefix}{j+1:4d}| {lines[j][:120]}")
                            results.append(f"--- {rel}:{i+1} ---\n" + "\n".join(ctx_lines))
                        else:
                            # 只顯示匹配行
                            results.append(f"{rel}:{i+1}: {line.strip()[:100]}")

                        if len(results) >= MAX_GREP_RESULTS:
                            break
            except Exception:
                continue

        return results

    def file_info(self, path: str) -> str:
        target = self._safe_path(path)
        if not target or not target.exists():
            return f"錯誤: 不存在 '{path}'"

        if target.is_file():
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
                lines = content.count('\n') + 1
                chars = len(content)
            except Exception:
                lines, chars = "N/A", target.stat().st_size

            return f"{path}: 檔案, {lines} 行, {chars:,} 字元"
        else:
            count = sum(1 for _ in target.rglob("*") if _.is_file())
            return f"{path}: 目錄, {count} 個檔案"

    def run_command(self, command: str, timeout: int = RUN_COMMAND_TIMEOUT) -> str:
        """執行白名單內的測試/建置命令

        安全改進：
        - 使用 shell=False + shlex.split()，避免 shell injection
        - 可透過環境變數 AI_CODE_RUN_TESTS=1 或 CLI flag --run-tests 啟用
        - 支援容器化執行（AI_CODE_USE_CONTAINER=1 或 --container）
        """
        if not config.RUN_COMMAND_ENABLED:
            return "錯誤: run_command 功能已停用（可用 --run-tests 或設定 AI_CODE_RUN_TESTS=1 啟用）"

        # 容器化執行模式（讀取 module 變數，避免 import 快照問題）
        if container_runner.CONTAINER_ENABLED:
            return self._run_command_in_container(command, timeout)

        command = command.strip()

        # 使用 shlex.split 解析命令（更安全）
        try:
            cmd_parts = shlex.split(command)
        except ValueError as e:
            return f"錯誤: 命令解析失敗 - {e}"

        if not cmd_parts:
            return "錯誤: 空命令"

        # 驗證命令是否在白名單中
        is_allowed = False
        for allowed in ALLOWED_COMMANDS:
            allowed_parts = shlex.split(allowed)
            # 檢查命令前綴是否匹配
            if cmd_parts[:len(allowed_parts)] == allowed_parts:
                is_allowed = True
                break

        if not is_allowed:
            allowed_list = ', '.join(ALLOWED_COMMANDS[:8])
            return f"錯誤: 不允許的命令。\n允許的命令前綴: {allowed_list}..."

        # 額外安全檢查：確保沒有危險字元（即使用 shlex 也要檢查）
        dangerous_patterns = ['$(', '`', '&&', '||', ';', '|', '>', '<']
        for part in cmd_parts:
            for pattern in dangerous_patterns:
                if pattern in part:
                    return f"錯誤: 參數包含不允許的字元 '{pattern}'"

        try:
            print(f"   [RUN] 執行: {command}")
            result = subprocess.run(
                cmd_parts,           # 使用列表形式，更安全
                shell=False,         # 關閉 shell，避免 injection
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}
            )

            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                if output:
                    output += "\n--- stderr ---\n"
                output += result.stderr

            # 智能裁切：保留尾巴（錯誤訊息通常在尾部）
            output = smart_truncate_output(output, RUN_COMMAND_MAX_OUTPUT, RUN_COMMAND_TAIL_RATIO)

            status = "✓ 成功" if result.returncode == 0 else f"✗ 失敗 (exit {result.returncode})"
            return f"=== {status} ===\n{output}" if output else f"=== {status} (無輸出) ==="

        except subprocess.TimeoutExpired:
            return f"錯誤: 命令超時 ({timeout} 秒)"
        except FileNotFoundError:
            return f"錯誤: 找不到命令 '{cmd_parts[0]}'"
        except Exception as e:
            return f"錯誤: {type(e).__name__}: {e}"

    def _run_command_in_container(self, command: str, timeout: int) -> str:
        """在容器中執行命令（更安全）

        特點：
        - 專案目錄唯讀掛載
        - 網路僅在需要時啟用
        - 資源限制（CPU/記憶體）
        """
        command = command.strip()

        # 簡單的命令驗證（容器環境較安全，但仍需基本檢查）
        dangerous_patterns = ['rm -rf', 'mkfs', 'dd if=', ':(){ :|:& };:']
        for pattern in dangerous_patterns:
            if pattern in command:
                return f"錯誤: 命令包含危險操作 '{pattern}'"

        # 判斷是否需要網路（安裝依賴需要）
        needs_network = any(kw in command for kw in ['npm install', 'pip install', 'go get', 'cargo fetch'])

        print(f"   [CONTAINER] 執行: {command}")

        result = container_runner.run_in_container(
            command=command,
            folder=str(self.root),
            timeout=timeout,
            network=needs_network,
            writable=False
        )

        if result['error']:
            return f"錯誤: {result['error']}"

        output = ""
        if result['stdout']:
            output += result['stdout']
        if result['stderr']:
            if output:
                output += "\n--- stderr ---\n"
            output += result['stderr']

        # 智能裁切：保留尾巴（錯誤訊息通常在尾部）
        output = smart_truncate_output(output, RUN_COMMAND_MAX_OUTPUT, RUN_COMMAND_TAIL_RATIO)

        status = "✓ 成功" if result['success'] else f"✗ 失敗 (exit {result['returncode']})"
        return f"=== {status} (容器模式) ===\n{output}" if output else f"=== {status} (容器模式, 無輸出) ==="

    # ============================================================
    # 改碼閉環工具
    # ============================================================
    def apply_patch(self, patch: str, dry_run: bool = False) -> str:
        """套用 unified diff 格式的 patch

        安全措施：
        - 路徑驗證：只能修改專案內的檔案
        - 限制檔案數量和行數
        - 自動備份原始檔案
        - Context 驗證：套用前驗證 context 行匹配

        改進：
        - 成功套用後自動執行 lint

        Args:
            patch: unified diff 格式的 patch 內容
            dry_run: 若為 True，只顯示會修改什麼
        """
        if not config.PATCH_ENABLED:
            return "錯誤: apply_patch 功能已停用（可用 --patch 或設定 AI_CODE_PATCH=1 啟用）"

        try:
            changes = self._parse_unified_diff(patch)
        except ValueError as e:
            return f"錯誤: patch 解析失敗 - {e}"

        if not changes:
            return "錯誤: 無法從 patch 中解析出任何修改"

        # 檢查檔案數量限制
        if len(changes) > PATCH_MAX_FILES:
            return f"錯誤: 修改檔案數量超過限制（{len(changes)} > {PATCH_MAX_FILES}）"

        results = []
        successfully_patched = []  # 記錄成功修改的檔案

        for filepath, hunks in changes.items():
            # 驗證路徑安全性
            target = self._safe_path(filepath)
            if not target:
                results.append(f"✗ {filepath}: 路徑不在專案內或無效")
                continue

            # 檢查行數限制
            total_lines = sum(len(h['add']) + len(h['remove']) for h in hunks)
            if total_lines > PATCH_MAX_LINES_PER_FILE:
                results.append(f"✗ {filepath}: 修改行數超過限制（{total_lines} > {PATCH_MAX_LINES_PER_FILE}）")
                continue

            if dry_run:
                results.append(f"[DRY RUN] {filepath}: 將修改 {len(hunks)} 個區塊, {total_lines} 行")
                for i, hunk in enumerate(hunks):
                    results.append(f"  區塊 {i+1}: 行 {hunk['old_start']}-{hunk['old_start']+hunk['old_count']-1}")
                continue

            # 實際套用修改
            try:
                result = self._apply_hunks_to_file(target, hunks)
                results.append(result)
                # 記錄成功修改的檔案
                if result.startswith("✓"):
                    successfully_patched.append(filepath)
            except Exception as e:
                results.append(f"✗ {filepath}: 套用失敗 - {e}")

        # 自動對成功修改的檔案執行 lint
        if successfully_patched and not dry_run:
            results.append("\n=== 自動 Lint ===")
            for filepath in successfully_patched:
                ext = Path(filepath).suffix.lower()
                if ext in LINT_COMMANDS:
                    try:
                        lint_result = self.run_lint(filepath, fix=True)
                        # 只顯示結果摘要
                        if "✓" in lint_result:
                            results.append(f"  ✓ {filepath}: lint 完成")
                        elif "⚠" in lint_result:
                            results.append(f"  ⚠ {filepath}: lint 有警告")
                        elif "錯誤: 沒有可用的 lint" in lint_result:
                            pass  # 沒有可用工具，跳過
                        else:
                            results.append(f"  {filepath}: {lint_result[:100]}")
                    except Exception as e:
                        results.append(f"  ⚠ {filepath}: lint 失敗 - {e}")

        return "\n".join(results) if results else "沒有修改"

    def _parse_unified_diff(self, patch: str) -> dict:
        """解析 unified diff 格式

        Returns:
            {filepath: [{'old_start': int, 'old_count': int, 'new_start': int, 'new_count': int,
                        'context': [...], 'add': [...], 'remove': [...]}]}
        """
        changes = {}
        lines = patch.split('\n')
        i = 0
        current_file = None

        while i < len(lines):
            line = lines[i]

            # 解析檔案頭
            if line.startswith('--- '):
                # --- a/path/to/file 或 --- path/to/file
                path = line[4:].strip()
                if path.startswith('a/'):
                    path = path[2:]
                # 忽略時間戳記
                path = path.split('\t')[0].strip()
                current_file = path
                i += 1
                continue

            if line.startswith('+++ '):
                # +++ b/path/to/file
                path = line[4:].strip()
                if path.startswith('b/'):
                    path = path[2:]
                path = path.split('\t')[0].strip()
                if current_file != path and current_file:
                    # 如果 --- 和 +++ 的路徑不同，使用 +++ 的路徑
                    pass
                current_file = path
                if current_file not in changes:
                    changes[current_file] = []
                i += 1
                continue

            # 解析 hunk header: @@ -old_start,old_count +new_start,new_count @@
            if line.startswith('@@') and current_file:
                match = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
                if match:
                    old_start = int(match.group(1))
                    old_count = int(match.group(2)) if match.group(2) else 1
                    new_start = int(match.group(3))
                    new_count = int(match.group(4)) if match.group(4) else 1

                    hunk = {
                        'old_start': old_start,
                        'old_count': old_count,
                        'new_start': new_start,
                        'new_count': new_count,
                        'lines': []  # (type, content): type = ' ', '+', '-'
                    }

                    i += 1
                    # 讀取 hunk 內容
                    while i < len(lines):
                        hunk_line = lines[i]
                        if not hunk_line:
                            # 空行可能是 context
                            hunk['lines'].append((' ', ''))
                            i += 1
                        elif hunk_line.startswith(' '):
                            hunk['lines'].append((' ', hunk_line[1:]))
                            i += 1
                        elif hunk_line.startswith('+') and not hunk_line.startswith('+++'):
                            hunk['lines'].append(('+', hunk_line[1:]))
                            i += 1
                        elif hunk_line.startswith('-') and not hunk_line.startswith('---'):
                            hunk['lines'].append(('-', hunk_line[1:]))
                            i += 1
                        elif hunk_line.startswith('@@') or hunk_line.startswith('---'):
                            break
                        else:
                            # 無效行，結束當前 hunk
                            break

                    changes[current_file].append(hunk)
                    continue

            i += 1

        return changes

    def _verify_hunk_context(self, lines: list, hunk: dict) -> tuple[bool, str]:
        """驗證 hunk 的 context 行是否與檔案內容匹配

        Args:
            lines: 檔案內容（每行一個元素）
            hunk: 要驗證的 hunk

        Returns:
            (is_valid, error_message)
        """
        start_idx = hunk['old_start'] - 1  # 轉為 0-based index
        file_line_idx = start_idx

        for line_type, content in hunk['lines']:
            if line_type in (' ', '-'):
                # context 或要刪除的行，必須與檔案內容匹配
                if file_line_idx >= len(lines):
                    return False, f"行 {file_line_idx + 1}: 超出檔案範圍"

                file_line = lines[file_line_idx]
                # 比較時忽略尾部空白
                if file_line.rstrip() != content.rstrip():
                    # 嘗試模糊匹配（只比較非空白部分）
                    if file_line.strip() != content.strip():
                        return False, (
                            f"行 {file_line_idx + 1} context 不匹配:\n"
                            f"  期望: '{content[:60]}...'\n"
                            f"  實際: '{file_line[:60]}...'"
                        )
                file_line_idx += 1

        return True, ""

    def _apply_hunks_to_file(self, filepath: Path, hunks: list) -> str:
        """將 hunks 套用到檔案

        改進：
        - 套用前驗證 context 行是否匹配
        - 從後往前套用，避免行號偏移
        """
        if not filepath.exists():
            # 新檔案：直接寫入所有 '+' 行
            new_lines = []
            for hunk in hunks:
                for line_type, content in hunk['lines']:
                    if line_type in (' ', '+'):
                        new_lines.append(content)
            filepath.write_text('\n'.join(new_lines) + '\n', encoding='utf-8')
            return f"✓ {filepath.relative_to(self.root)}: 新建檔案"

        # 讀取原始檔案
        original = filepath.read_text(encoding='utf-8', errors='replace')
        lines = original.split('\n')

        # 先驗證所有 hunk 的 context
        for i, hunk in enumerate(hunks):
            is_valid, error_msg = self._verify_hunk_context(lines, hunk)
            if not is_valid:
                return f"✗ {filepath.relative_to(self.root)}: 區塊 {i+1} {error_msg}"

        # 備份原始檔案
        backup_path = filepath.with_suffix(filepath.suffix + '.orig')
        backup_path.write_text(original, encoding='utf-8')

        # 從後往前套用（避免行號偏移）
        sorted_hunks = sorted(hunks, key=lambda h: h['old_start'], reverse=True)

        for hunk in sorted_hunks:
            start_idx = hunk['old_start'] - 1  # 轉為 0-based index
            old_count = hunk['old_count']

            # 收集新行
            new_lines = []
            for line_type, content in hunk['lines']:
                if line_type in (' ', '+'):
                    new_lines.append(content)

            # 替換
            lines[start_idx:start_idx + old_count] = new_lines

        # 寫入修改後的檔案
        filepath.write_text('\n'.join(lines), encoding='utf-8')

        # 刪除備份（如果成功）
        try:
            backup_path.unlink()
        except Exception:
            pass

        rel_path = filepath.relative_to(self.root)
        return f"✓ {rel_path}: 已修改 {len(hunks)} 個區塊"

    def git_status(self) -> str:
        """顯示 git 工作目錄狀態"""
        try:
            result = subprocess.run(
                ['git', 'status', '--porcelain', '-uall'],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                # 可能不是 git repo
                return f"錯誤: {result.stderr.strip() or '不是 git 倉庫'}"

            output = result.stdout.strip()
            if not output:
                return "工作目錄乾淨（沒有修改）"

            # 解析狀態碼
            lines = []
            for line in output.split('\n'):
                if len(line) >= 3:
                    status = line[:2]
                    path = line[3:]
                    status_map = {
                        'M ': '已暫存修改',
                        ' M': '未暫存修改',
                        'MM': '已暫存+未暫存修改',
                        'A ': '已暫存新增',
                        ' A': '未暫存新增',
                        'D ': '已暫存刪除',
                        ' D': '未暫存刪除',
                        '??': '未追蹤',
                        'R ': '已重命名',
                        'C ': '已複製',
                    }
                    status_text = status_map.get(status, status)
                    lines.append(f"  {status_text}: {path}")

            return "=== Git 狀態 ===\n" + '\n'.join(lines)

        except FileNotFoundError:
            return "錯誤: 找不到 git 命令"
        except subprocess.TimeoutExpired:
            return "錯誤: git status 超時"
        except Exception as e:
            return f"錯誤: {type(e).__name__}: {e}"

    def git_diff(self, path: str = None, staged: bool = False) -> str:
        """顯示 git diff"""
        try:
            cmd = ['git', 'diff']
            if staged:
                cmd.append('--staged')
            cmd.append('--')

            if path:
                target = self._safe_path(path)
                if not target:
                    return f"錯誤: 路徑不在專案內 '{path}'"
                cmd.append(str(target.relative_to(self.root)))

            result = subprocess.run(
                cmd,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                return f"錯誤: {result.stderr.strip() or '不是 git 倉庫'}"

            output = result.stdout.strip()
            if not output:
                scope = f"'{path}'" if path else "工作目錄"
                staged_text = "已暫存" if staged else ""
                return f"{scope} 沒有{staged_text}差異"

            # 限制輸出長度
            if len(output) > RUN_COMMAND_MAX_OUTPUT:
                half = RUN_COMMAND_MAX_OUTPUT // 2
                output = (
                    output[:half] +
                    f"\n\n... [截斷 {len(output) - RUN_COMMAND_MAX_OUTPUT} 字元] ...\n\n" +
                    output[-half:]
                )

            return f"=== Git Diff {'(staged)' if staged else ''} ===\n{output}"

        except FileNotFoundError:
            return "錯誤: 找不到 git 命令"
        except subprocess.TimeoutExpired:
            return "錯誤: git diff 超時"
        except Exception as e:
            return f"錯誤: {type(e).__name__}: {e}"

    def run_lint(self, path: str, fix: bool = True) -> str:
        """對檔案執行 lint/format 工具"""
        target = self._safe_path(path)
        if not target or not target.exists():
            return f"錯誤: 檔案不存在 '{path}'"
        if not target.is_file():
            return f"錯誤: '{path}' 不是檔案"

        ext = target.suffix.lower()
        lint_cmds = LINT_COMMANDS.get(ext)
        if not lint_cmds:
            return f"錯誤: 不支援的檔案類型 '{ext}'（支援: {', '.join(LINT_COMMANDS.keys())}）"

        results = []
        rel_path = str(target.relative_to(self.root))

        for cmd_template in lint_cmds:
            # 嘗試執行每個 lint 命令
            cmd_parts = shlex.split(cmd_template)
            cmd_parts.append(rel_path)

            try:
                print(f"   [LINT] 執行: {' '.join(cmd_parts)}")
                result = subprocess.run(
                    cmd_parts,
                    cwd=str(self.root),
                    capture_output=True,
                    text=True,
                    timeout=60
                )

                tool_name = cmd_parts[0]
                if result.returncode == 0:
                    output = result.stdout.strip() or result.stderr.strip()
                    if output:
                        results.append(f"✓ {tool_name}: {output[:200]}")
                    else:
                        results.append(f"✓ {tool_name}: 完成")
                    break  # 成功就不嘗試下一個
                else:
                    # 工具不存在或執行失敗，嘗試下一個
                    if "not found" in result.stderr.lower() or "not recognized" in result.stderr.lower():
                        continue
                    # 其他錯誤（如 lint 發現問題）
                    output = result.stderr.strip() or result.stdout.strip()
                    results.append(f"⚠ {tool_name}:\n{output[:500]}")
                    break

            except FileNotFoundError:
                # 工具不存在，嘗試下一個
                continue
            except subprocess.TimeoutExpired:
                results.append(f"✗ {cmd_parts[0]}: 超時")
                break
            except Exception as e:
                results.append(f"✗ {cmd_parts[0]}: {e}")
                break

        if not results:
            return f"錯誤: 沒有可用的 lint 工具（已嘗試: {', '.join(c.split()[0] for c in lint_cmds)}）"

        return f"=== Lint {rel_path} ===\n" + '\n'.join(results)

    def execute(self, tool: str, args: dict) -> Optional[str]:
        if tool == "list_files":
            return self.list_files(args.get("path", "."), args.get("depth", 2))
        elif tool == "read_file":
            return self.read_file(args.get("path", ""), args.get("start_line", 1), args.get("end_line"))
        elif tool == "grep":
            return self.grep(args.get("pattern", ""), args.get("path", "."),
                           args.get("include"), args.get("context", 0))
        elif tool == "file_info":
            return self.file_info(args.get("path", ""))
        elif tool == "run_command":
            return self.run_command(args.get("command", ""), args.get("timeout", RUN_COMMAND_TIMEOUT))
        # 改碼閉環工具
        elif tool == "apply_patch":
            return self.apply_patch(args.get("patch", ""), args.get("dry_run", False))
        elif tool == "git_status":
            return self.git_status()
        elif tool == "git_diff":
            return self.git_diff(args.get("path"), args.get("staged", False))
        elif tool == "run_lint":
            return self.run_lint(args.get("path", ""), args.get("fix", True))
        else:
            return f"錯誤: 未知工具 '{tool}'"


# ============================================================
# Messages Budget 管理
# ============================================================
def _calc_messages_size(messages: list) -> int:
    """計算 messages 總字元數"""
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += len(content)
    return total


def _get_tool_priority(messages: list, tool_idx: int) -> int:
    """取得 tool 輸出的優先級（數字越小優先級越高）

    read_file 和 grep 產生的內容對程式碼分析最重要，應優先保留
    """
    # 往前找對應的 assistant tool_call 來判斷 tool 名稱
    for i in range(tool_idx - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                tool_name = tool_calls[0].get("function", {}).get("name", "")
                # read_file 和 grep 最重要（產生實際程式碼內容）
                if tool_name in ("read_file", "grep"):
                    return 1  # 高優先級
                # run_command 也重要（測試輸出）
                elif tool_name == "run_command":
                    return 2  # 中優先級
                # list_files 和 file_info 優先級較低
                else:
                    return 3  # 低優先級
            break

    return 3  # 預設低優先級


def _trim_messages_to_budget(messages: list, budget: int = MAX_MESSAGES_BUDGET) -> list:
    """裁切 messages 使其總大小不超過預算

    策略：
    1. 保留 system message（第一個）
    2. 保留 user 的原始問題（第二個）
    3. 保留最近 MIN_RECENT_TOOL_OUTPUTS 輪的 tool 輸出
    4. 將較舊的 tool 輸出摘要化（只保留前 200 字）

    改進：
    - 優先保留 read_file 和 grep 的輸出（這些是實際程式碼內容）
    - 優先截斷 list_files 和 file_info 的輸出（這些可以重新查詢）
    """
    if _calc_messages_size(messages) <= budget:
        return messages

    if len(messages) <= 2:
        return messages

    # 找出所有 tool 輸出的位置
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]

    if not tool_indices:
        return messages

    # 計算需要摘要化多少舊的 tool 輸出
    num_to_summarize = max(0, len(tool_indices) - MIN_RECENT_TOOL_OUTPUTS)

    if num_to_summarize == 0:
        # 所有都是最近的，只能硬截斷
        # 但仍依優先級處理：低優先級先截斷
        for i in tool_indices:
            priority = _get_tool_priority(messages, i)
            content = messages[i].get("content", "")
            # 低優先級截短一點，高優先級保留多一點
            if priority == 3 and len(content) > 300:
                messages[i]["content"] = content[:250] + f"\n... [截斷 {len(content) - 250} 字元]"
            elif priority == 2 and len(content) > 500:
                messages[i]["content"] = content[:400] + f"\n... [截斷 {len(content) - 400} 字元]"
            elif priority == 1 and len(content) > 800:
                messages[i]["content"] = content[:700] + f"\n... [截斷 {len(content) - 700} 字元]"
        return messages

    # 按優先級排序要摘要化的 tool 輸出（低優先級先摘要化）
    summarize_candidates = tool_indices[:num_to_summarize]
    summarize_candidates.sort(key=lambda i: -_get_tool_priority(messages, i))  # 低優先級（數字大）排前面

    # 摘要化較舊的 tool 輸出（按優先級）
    for idx in summarize_candidates:
        priority = _get_tool_priority(messages, idx)
        content = messages[idx].get("content", "")
        # 低優先級摘要更激進，高優先級保留更多
        if priority == 3:  # list_files, file_info
            if len(content) > 100:
                messages[idx]["content"] = content[:80] + f"\n... [舊輸出已摘要，原 {len(content)} 字元]"
        elif priority == 2:  # run_command
            if len(content) > 200:
                messages[idx]["content"] = content[:150] + f"\n... [舊輸出已摘要，原 {len(content)} 字元]"
        else:  # read_file, grep
            if len(content) > 400:
                messages[idx]["content"] = content[:350] + f"\n... [舊輸出已摘要，原 {len(content)} 字元]"

    # 如果還是超過，繼續截斷（按優先級）
    while _calc_messages_size(messages) > budget and tool_indices:
        # 找低優先級且最大的 tool 輸出來截斷
        # 先按優先級分組，優先截斷低優先級的
        low_priority = [i for i in tool_indices if _get_tool_priority(messages, i) == 3]
        mid_priority = [i for i in tool_indices if _get_tool_priority(messages, i) == 2]
        high_priority = [i for i in tool_indices if _get_tool_priority(messages, i) == 1]

        candidates = low_priority if low_priority else (mid_priority if mid_priority else high_priority)
        if not candidates:
            break

        max_idx = max(candidates, key=lambda i: len(messages[i].get("content", "")))
        content = messages[max_idx].get("content", "")
        if len(content) > 100:
            messages[max_idx]["content"] = content[:80] + f"\n... [超預算截斷，原 {len(content)} 字元]"
        else:
            # 這個已經很短了，從候選中移除
            tool_indices.remove(max_idx)

    return messages


# ============================================================
# Stack Trace 解析
# ============================================================
STACK_TRACE_PATTERNS = [
    r'File "(.+?)", line (\d+)',
    r'(.+?):(\d+):(?:\d+:)?\s*error',
    r'(.+?):(\d+):(?:\d+:)?\s*warning',
    r'at (.+?):(\d+):',
    r'^\s+at .+?\((.+?):(\d+):\d+\)',
    # 修正：限制路徑字元，避免匹配到中文句子
    # 路徑只能包含 word字元、/、\、.、-、空白前要斷開
    r'(?:^|[\s,;:])([a-zA-Z0-9_./\\-]+\.(?:cpp|c|h|py|rs|go|java|js|ts)):(\d+)',
]


def _normalize_stack_filepath(filepath: str) -> str:
    """正規化 stack trace 中的檔案路徑

    移除前綴雜訊，確保只剩下檔案路徑

    Args:
        filepath: 從 regex 提取的原始路徑字串

    Returns:
        清理後的檔案路徑
    """
    # 移除常見前綴
    filepath = filepath.strip()

    # 如果整個路徑看起來正常，直接返回
    if '/' in filepath or '\\' in filepath:
        # 有路徑分隔符，取最後一個看起來像路徑的部分
        # 例如 "在 code_rag.py" -> "code_rag.py"
        parts = filepath.split()
        for part in reversed(parts):
            if '.' in part and any(part.endswith(ext) for ext in ['.py', '.c', '.cpp', '.h', '.go', '.rs', '.java', '.js', '.ts']):
                return part
        return filepath

    # 沒有路徑分隔符，可能是純檔名
    # 找到最後一個看起來像檔名的部分
    parts = filepath.split()
    for part in reversed(parts):
        if '.' in part and any(part.endswith(ext) for ext in ['.py', '.c', '.cpp', '.h', '.go', '.rs', '.java', '.js', '.ts']):
            return part

    return filepath


def extract_stack_locations(text: str) -> list[tuple[str, int]]:
    """從文字中提取 stack trace 的檔案位置"""
    locations = []
    for pattern in STACK_TRACE_PATTERNS:
        for m in re.finditer(pattern, text, re.MULTILINE):
            try:
                filepath = m.group(1)
                # 正規化路徑，移除前綴雜訊
                filepath = _normalize_stack_filepath(filepath)
                line_num = int(m.group(2))
                if not filepath.startswith('/usr') and not filepath.startswith('C:\\Windows'):
                    locations.append((filepath, line_num))
            except (ValueError, IndexError):
                continue
    return locations


def _build_basename_map(folder: str) -> dict[str, list[str]]:
    """建立 basename -> [relative_paths...] 的對照表

    用於 stack trace filepath 的 suffix matching
    """
    from utils import scan_project_metadata

    basename_map = {}
    for file_info in scan_project_metadata(folder):
        rel_path = file_info["path"]
        basename = Path(rel_path).name.lower()
        if basename not in basename_map:
            basename_map[basename] = []
        basename_map[basename].append(rel_path)

    return basename_map


def _suffix_match_path(filepath: str, basename_map: dict[str, list[str]], folder: str) -> str | None:
    """用 suffix matching 在專案中找最接近的路徑

    策略：
    1. 先查 basename map 找候選
    2. 若只有一個候選，直接採用
    3. 若多個候選，比對最後 2~4 層目錄，找唯一匹配
    4. 無法唯一決定則返回 None

    Args:
        filepath: stack trace 中的路徑（可能是絕對路徑或相對於其他專案）
        basename_map: basename -> [relative_paths...] 對照表
        folder: 專案根目錄

    Returns:
        專案內的相對路徑，或 None（若無法唯一決定）
    """
    # 統一分隔符並取得 basename
    filepath_normalized = filepath.replace('\\', '/')
    basename = Path(filepath_normalized).name.lower()

    candidates = basename_map.get(basename, [])

    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    # 多個候選：用 suffix matching
    # 取 filepath 的最後 2~4 層路徑組件
    filepath_parts = Path(filepath_normalized).parts

    best_matches = []
    best_match_depth = 0

    for candidate in candidates:
        candidate_parts = Path(candidate.replace('\\', '/')).parts

        # 比對從尾端開始有多少層路徑相同
        match_depth = 0
        for i in range(1, min(len(filepath_parts), len(candidate_parts), 5) + 1):
            if filepath_parts[-i].lower() == candidate_parts[-i].lower():
                match_depth = i
            else:
                break

        if match_depth > best_match_depth:
            best_match_depth = match_depth
            best_matches = [candidate]
        elif match_depth == best_match_depth and match_depth > 0:
            best_matches.append(candidate)

    # 只有唯一最佳匹配才採用
    if len(best_matches) == 1:
        return best_matches[0]

    # 多筆匹配，無法唯一決定
    return None


def _resolve_stack_filepath(filepath: str, folder: str, basename_map: dict[str, list[str]] = None) -> tuple[str, str | None]:
    """解析 stack trace 中的 filepath，返回專案內可用的相對路徑

    Args:
        filepath: stack trace 中的原始路徑
        folder: 專案根目錄
        basename_map: basename -> [relative_paths...] 對照表（可選，用於 suffix matching）

    Returns:
        (rel_path, ambiguous_candidates)
        - rel_path: 專案內的相對路徑
        - ambiguous_candidates: 若有歧義，返回候選列表字串；否則為 None
    """
    folder_path = Path(folder).resolve()

    # Case 1: 絕對路徑且在專案內
    if os.path.isabs(filepath):
        try:
            abs_path = Path(filepath).resolve()
            rel_path = str(abs_path.relative_to(folder_path))
            if (folder_path / rel_path).exists():
                return rel_path, None
        except ValueError:
            pass

    # Case 2: 相對路徑，直接在專案內存在
    rel_check = Path(folder) / filepath
    if rel_check.exists():
        return filepath, None

    # Case 3: 用 suffix matching 找最接近的路徑
    if basename_map:
        matched = _suffix_match_path(filepath, basename_map, folder)
        if matched:
            return matched, None

        # 若有多筆候選但無法唯一決定，返回歧義資訊
        basename = Path(filepath.replace('\\', '/')).name.lower()
        candidates = basename_map.get(basename, [])
        if len(candidates) > 1:
            return candidates[0], f"多筆匹配: {', '.join(candidates[:3])}"

    # Case 4: Fallback - 只用 basename（舊行為，但可能讀錯檔案）
    basename = Path(filepath).name
    return basename, f"警告: 無法確定正確檔案，使用 basename: {basename}"


def handle_followup(question: str, prev_qa: list, knowledge_ctx: str = "",
                    code_rag_context: str = "", folder: str = None,
                    use_agent: bool = True, code_rag=None) -> str:
    """處理追問

    改進：追問也走 agent 模式，允許使用工具讀檔/定位
    這樣可以避免「用上次印象 + 推測」回答

    Args:
        question: 追問內容
        prev_qa: 歷史對話列表
        knowledge_ctx: 知識庫上下文（REF 資料）
        code_rag_context: Code RAG 預讀的程式碼上下文
        folder: 專案資料夾（用於 agent 模式）
        use_agent: 是否使用 agent 模式（預設 True）
        code_rag: CodeRAG 實例
    """
    prev_q, prev_a = prev_qa[-1]

    # 如果有 folder 且啟用 agent，走精簡版 agent
    if use_agent and folder:
        # 將追問上下文加入問題
        enhanced_question = f"""【之前的對話】
用戶問：{prev_q[:200]}{'...' if len(prev_q) > 200 else ''}

你的回答摘要：{prev_a[:500]}{'...' if len(prev_a) > 500 else ''}

【用戶現在的追問】
{question}

請根據之前的回答，回答用戶的追問。若需要更多程式碼細節，可使用工具探索。"""

        # 使用 run_agent 但設定較低的 MAX_TOOL_LOOPS
        return run_agent(
            folder=folder,
            question=enhanced_question,
            prev_qa=prev_qa,
            knowledge_ctx=knowledge_ctx,
            code_rag=code_rag,
            max_loops=4  # 追問用較少的工具回合
        )

    # Fallback: 簡單 prompt 模式
    context_parts = []

    if knowledge_ctx:
        context_parts.append(f"【參考資料】\n{knowledge_ctx}")

    if code_rag_context:
        context_parts.append(f"【相關程式碼】\n{code_rag_context}")

    context_block = "\n\n".join(context_parts)
    context_section = f"\n{context_block}\n" if context_block else ""

    prompt = f"""你是程式碼分析助手。
{context_section}
【之前的對話】
用戶問：{prev_q}

你的回答：
{prev_a}

【用戶現在補充】
{question}

請根據之前的回答與上方的參考資料/程式碼，直接給出針對這個補充條件的具體答案。
重要：若有 [REF] 參考資料，必須以 REF 為準；若有相關程式碼，回答需基於程式碼內容。
用繁體中文回答，簡潔明瞭。"""
    return call_llm_stream(prompt)


def run_agent(folder: str, question: str, image_ctx: str = "", prev_qa: list = None,
              knowledge_ctx: str = "", code_rag=None, max_loops: int = None,
              return_metadata: bool = False) -> str | tuple:
    """執行 Agent 模式

    Args:
        max_loops: 最大工具回合數，預設使用 MAX_TOOL_LOOPS
        return_metadata: 若為 True，回傳 (answer, metadata) tuple，
                        metadata 包含 tool_calls 和 files_read

    Returns:
        str: 回答（預設）
        tuple: (answer, {"tool_calls": [...], "files_read": [...]}) 若 return_metadata=True
    """
    executor = ToolExecutor(folder)
    prev_qa = prev_qa or []
    effective_max_loops = max_loops if max_loops is not None else MAX_TOOL_LOOPS

    # 追蹤工具呼叫和讀取的檔案（用於資料飛輪）
    _tool_calls_record = []
    _files_read_record = set()

    def _make_return(answer: str):
        """包裝返回值，支援 metadata"""
        if return_metadata:
            return answer, {
                "tool_calls": _tool_calls_record,
                "files_read": list(_files_read_record)
            }
        return answer

    q_lower = question.lower()
    is_bug_fix = any(kw in q_lower for kw in ['bug', '錯誤', 'error', 'crash', 'fail', '修', 'fix', '問題', 'issue', '不work', '不能'])

    # Stack trace 位置提取
    stack_locations = extract_stack_locations(question)
    stack_preread_context = ""

    if stack_locations:
        print(f"[STACK] 偵測到 {len(stack_locations)} 個 stack trace 位置")
        preread_lines = CODE_RAG_PREREAD_LINES_BUG
        stack_parts = []

        # 建立 basename -> paths 對照表，用於 suffix matching
        basename_map = _build_basename_map(folder)

        for filepath, line_num in stack_locations[:3]:
            # 使用 suffix matching 解析 filepath
            rel_path, ambiguous_info = _resolve_stack_filepath(filepath, folder, basename_map)

            if ambiguous_info:
                print(f"   [STACK_WARN] {filepath} -> {ambiguous_info}")

            half_range = preread_lines // 2
            start = max(1, line_num - half_range)
            end = line_num + half_range

            content = executor.read_file(rel_path, start, end)
            if content and not content.startswith("錯誤"):
                stack_parts.append(f"[Stack trace 位置: {rel_path}:{line_num}]\n{content}")
                print(f"   [STACK_PREREAD] {rel_path}:{line_num} [{preread_lines} 行]")
            elif ambiguous_info and "多筆匹配" in ambiguous_info:
                # 若有歧義且第一個候選讀取失敗，嘗試 preread 所有候選（少量行數）
                basename = Path(filepath.replace('\\', '/')).name.lower()
                candidates = basename_map.get(basename, [])
                for cand in candidates[:3]:
                    cand_content = executor.read_file(cand, start, min(start + 10, end))
                    if cand_content and not cand_content.startswith("錯誤"):
                        stack_parts.append(f"[Stack trace 候選: {cand}:{line_num}]\n{cand_content}")
                        print(f"   [STACK_PREREAD_CAND] {cand}:{line_num} [少量行]")

        if stack_parts:
            stack_preread_context = "\n\n【Stack trace 相關程式碼 - 這些是錯誤發生的位置】:\n" + "\n\n".join(stack_parts)

            # === Stack Trace 快路徑 ===
            # 有明確的 stack trace 時，直接一次 LLM 回答，不進 tool-loop
            # 這大幅加速 bug 類問題的處理
            if is_bug_fix and len(stack_parts) >= 1:
                print(f"[FAST_PATH] 啟用 Stack Trace 快路徑模式")
                fast_prompt = f"""你是除錯助手。以下是錯誤訊息以及對應檔案的相關程式碼：

錯誤訊息：
{question}

{stack_preread_context}

請回答：
1. 造成錯誤的直接原因是什麼？
2. 應該如何修改（描述修法即可，不用貼完整 patch）？

【回答規則】
- 必須根據上面的程式碼分析，不可憑經驗猜測
- 若程式碼不足以判斷根因，請明確說明需要更多資訊
- 所有判斷必須附上 file:line 位置（如 agent.py:123）
- 使用繁體中文回答
"""
                from utils import call_llm_stream
                fast_answer = call_llm_stream(fast_prompt, temperature=0.0)
                print(f"   [OK] 快路徑完成")
                return _make_return(fast_answer)

    # 構建對話歷史（壓縮版）
    # 注意：放在 prompt 前段，因為 LLM 對兩端注意力較強，
    # 低優先級內容放前面，重要的當前任務/規則放後面
    history_context = ""
    if prev_qa:
        history_context = "\n【對話歷史（僅供背景參考，優先級最低）】:\n"
        for i, (q, a) in enumerate(prev_qa[-2:], 1):
            q_short = q[:80] + "..." if len(q) > 80 else q
            a_short = a[:200] + "..." if len(a) > 200 else a
            history_context += f"Q{i}: {q_short}\nA{i}: {a_short}\n"
        history_context += "---\n"

    # Code RAG 自動預讀
    code_rag_context = ""
    preread_files = set()
    for filepath, _ in stack_locations:
        preread_files.add(filepath)
        preread_files.add(Path(filepath).name)

    if code_rag and CODE_RAG_ENABLED:
        # Bug 模式用較小的 top_k，減少噪音
        rag_top_k = CODE_RAG_TOP_K_BUG if is_bug_fix else CODE_RAG_TOP_K
        candidates = code_rag.query(question, top_k=rag_top_k, is_bug_fix=is_bug_fix)

        if candidates:
            print(f"[CODE_RAG] 找到 {len(candidates)} 個可能相關的程式碼位置")

            preread_lines = CODE_RAG_PREREAD_LINES_BUG if is_bug_fix else CODE_RAG_PREREAD_LINES
            preread_top_k = CODE_RAG_PREREAD_TOP_K_BUG if is_bug_fix else CODE_RAG_PREREAD_TOP_K

            preread_parts = []
            for c in candidates[:preread_top_k]:
                if c['path'] in preread_files:
                    continue

                # 改進：優先使用 end_line 讀取完整函式/類別區塊
                start_line = c['line']
                end_line = c.get('end_line')

                if end_line and (end_line - start_line + 1) <= CODE_RAG_PREREAD_MAX_LINES:
                    # 有 end_line 且函式長度在限制內，讀取完整區塊
                    start = start_line
                    end = end_line
                    read_mode = "完整區塊"
                else:
                    # 退回窗口模式：函式太長或沒有 end_line
                    center_line = start_line
                    half_range = preread_lines // 2
                    start = max(1, center_line - half_range)
                    end = center_line + half_range
                    read_mode = f"窗口 {preread_lines} 行"

                content = executor.read_file(c['path'], start, end)
                if content and not content.startswith("錯誤"):
                    preread_parts.append(
                        f"[預讀: {c['path']} - {c['type']} {c['symbol']} (相關度: {c['score']})]\n{content}"
                    )
                    preread_files.add(c['path'])
                    actual_lines = end - start + 1
                    print(f"   [PREREAD] {c['path']}:{start}-{end} ({c['symbol']}) [{read_mode}, {actual_lines} 行]")

            if preread_parts:
                code_rag_context = "\n\n【Code RAG 自動預讀的相關程式碼 - 請優先根據這些內容分析】:\n" + "\n\n".join(preread_parts)

            other_candidates = [c for c in candidates if c['path'] not in preread_files]
            if other_candidates:
                hints = [f"  - {c['path']}:{c['line']} {c['type']} {c['symbol']}" for c in other_candidates[:5]]
                code_rag_context += "\n\n[其他可能相關位置]:\n" + "\n".join(hints)

    # System prompt
    if is_bug_fix and config.RUN_COMMAND_ENABLED:
        # Bug 模式沒有 stack trace 時加警告
        no_evidence_warning = ""
        if not stack_locations and not code_rag_context:
            no_evidence_warning = """
⚠️ 注意：用戶未提供 stack trace 或錯誤 log，以下分析可能不完整。
建議先請用戶提供具體的錯誤訊息，或用 run_command 執行測試重現問題。
"""
        task_hint = f"""{no_evidence_warning}
【Bug 修復模式 - 重要】
請務必嘗試以下步驟：
1. 先用 run_command 執行測試命令來重現問題（如 pytest, ctest, npm test, cargo test, go test）
2. 分析測試輸出，找出具體的錯誤訊息和失敗點
3. 根據錯誤訊息，定位問題程式碼
4. 提出具體的修改建議
5. 如果修改後，建議再次執行測試驗證

若專案中存在測試檔案（test_*.py, *_test.cpp 等），請至少嘗試呼叫一次 run_command"""
    else:
        task_hint = ""

    run_cmd_hint = """
14. 可用 run_command 執行測試（如 pytest, ctest, npm test, cargo test, go test）來驗證想法""" if config.RUN_COMMAND_ENABLED else ""

    is_creative = any(kw in q_lower for kw in ['refactor', '重構', '設計', '架構', 'design', 'architecture', '建議', 'suggest'])

    # 上下文排列：低優先級在前，高優先級在後（LLM 對兩端注意力較強）
    # 順序：對話歷史(最低) -> Code RAG -> Stack trace -> REF知識庫 -> BIN/圖片(最高) -> 規則
    system_prompt = f"""你是程式碼分析 Agent。透過工具探索專案來回答用戶問題。

專案路徑: {folder}
{history_context}
{code_rag_context}
{stack_preread_context}
{knowledge_ctx}
{image_ctx}
{task_hint}

【回答規則 - 嚴格遵守】
1. 若有 [BIN] 二進位檔案，必須優先分析其 Hex dump 和可讀字串，這是使用者最關心的內容
2. 禁止憑常識或經驗猜測，只能根據程式碼與 [REF] 內容回答
3. 若文件/程式碼沒有給出明確資訊，必須說「程式碼/文件中沒有明確說明」
4. 若需要做推測，一定要明確標示「推測：...」，並說明推測依據
5. 凡是來自 [REF] 的描述，句尾必須標註編號，如「...（REF1）」
6. 如果回答中完全沒有 REF 引用，要主動說明「以下為一般經驗，文件未明寫」
7. 若有「Code RAG 預讀程式碼」或「Stack trace 程式碼」，優先基於這些內容分析，不要想像其他檔案內容
8. 若你的常識與 [REF] 內容衝突，一律以 [REF] 為準，不得自行修正
9. 除非預讀程式碼不足以回答，否則不要猜測其他檔案內容；若需要其他檔案，用 read_file 精準讀取
10. 【重要】所有基於程式碼的判斷，必須附上 file:line 位置（如 agent.py:123），讓用戶可以驗證

【工具使用規則】
11. 優先使用預讀內容，不足時再用工具探索
12. 不要重複呼叫相同的工具和參數
13. 需要其他檔案時，用 read_file 精準讀取，不要亂 grep
14. 收集到足夠資訊後，直接用文字回答，答案用繁體中文{run_cmd_hint}"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question}
    ]

    agent_temperature = 0.2 if is_creative else 0.0

    tool_history = []
    read_files_set = set(preread_files)
    has_run_command = False
    bug_fix_reminder_sent = False
    no_evidence_reminder_sent = False  # GPT建議：追蹤是否已發過無證據提醒

    # GPT建議：工具使用次數上限，避免無意義的亂逛
    MAX_READ_FILE_CALLS = 15
    MAX_GREP_CALLS = 10
    read_file_count = 0
    grep_count = 0
    tool_limit_reached = False

    for i in range(effective_max_loops):
        # 若已達工具上限，直接進入總結階段
        if tool_limit_reached:
            print(f"[LOOP] 工具上限已達，跳過剩餘迴圈")
            break

        print(f"[LOOP] Agent 第 {i+1} 輪...")

        # 每輪開始前先裁切 messages，避免 context 超載
        messages = _trim_messages_to_budget(messages)

        response = call_llm_with_tools(messages, temperature=agent_temperature)

        if response["done_reason"] == "error":
            return response["content"]

        tool_calls = response.get("tool_calls", [])

        if not tool_calls:
            content = response.get("content", "")
            if content and len(content) > 50:
                if is_bug_fix and config.RUN_COMMAND_ENABLED and not has_run_command and not bug_fix_reminder_sent:
                    print(f"   [NOTE] Bug 修復模式：尚未執行測試，發送提醒...")
                    bug_fix_reminder_sent = True
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": "在最終回答前，請先用 run_command 執行適當的測試命令（如 pytest、make test）來驗證你的分析是否正確，或重現問題。如果專案沒有測試或你確定不需要測試，請直接給出最終回答。"
                    })
                    continue

                # GPT建議：若沒有讀到任何相關檔案且沒有預讀內容，提醒 LLM 偏向拒答（只提醒一次）
                if not _files_read_record and not stack_preread_context and not code_rag_context and not no_evidence_reminder_sent:
                    print(f"   [WARN] 沒有讀到任何相關檔案，重新提示偏向拒答...")
                    no_evidence_reminder_sent = True
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": "注意：你目前沒有讀到任何程式碼檔案。若上述回答包含對程式碼的推測，請修正為「專案中沒有足夠資訊判斷」。若回答已經基於 [REF] 知識庫內容，則可以保留。請給出最終答案。"
                    })
                    continue

                if should_use_strict_mode(question, knowledge_ctx):
                    print(f"   [STRICT] Agent 啟用嚴格模式自我檢查...")
                    base_ctx = f"專案路徑: {folder}\n{code_rag_context}\n{stack_preread_context}"
                    content = answer_with_self_check(question, base_ctx, knowledge_ctx)

                print(f"   [OK] Agent 完成分析\n")
                # 直接輸出結果（批次輸出，避免逐字 I/O 開銷）
                print(content)
                return _make_return(content)
            else:
                messages.append({"role": "assistant", "content": content or "..."})
                messages.append({"role": "user", "content": "請繼續探索或直接回答問題。"})
                continue

        for tool_call in tool_calls:
            func = tool_call.get("function", {})
            tool_name = func.get("name", "")

            args_raw = func.get("arguments", {})
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {}
            else:
                args = args_raw

            print(f"   [TOOL] {tool_name}({args})")

            if tool_name == "run_command":
                has_run_command = True

            # GPT建議：工具使用次數計數
            if tool_name == "read_file":
                read_file_count += 1
            elif tool_name == "grep":
                grep_count += 1

            # 檢查是否達到工具上限
            if read_file_count > MAX_READ_FILE_CALLS or grep_count > MAX_GREP_CALLS:
                if not tool_limit_reached:
                    tool_limit_reached = True
                    print(f"   [LIMIT] 工具使用達上限 (read_file: {read_file_count}, grep: {grep_count})")
                    # 插入提示讓 LLM 收斂
                    messages.append({
                        "role": "user",
                        "content": "已經讀了足夠多檔案，請根據目前掌握的資訊嘗試給出推論；若仍沒有把握，請明確說「原因不明」而不要亂猜。請直接給出最終回答。"
                    })
                    break  # 跳出 tool_calls 迴圈，讓下一輪不再執行工具
                continue  # 跳過這個工具呼叫

            call_key = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
            if call_key in tool_history:
                print(f"   [WARN] 跳過重複呼叫")
                result = f"已經呼叫過，請用其他工具或直接回答"
            else:
                tool_history.append(call_key)
                result = executor.execute(tool_name, args)

                # 記錄工具呼叫（用於資料飛輪）
                tool_call_summary = f"{tool_name}:{args.get('path', args.get('pattern', args.get('command', '')[:30]))}"
                _tool_calls_record.append(tool_call_summary)

                if tool_name == "read_file" and result:
                    line_match = re.search(r'行 (\d+)-(\d+) / 共 (\d+) 行', result)
                    if line_match:
                        start, end, total = map(int, line_match.groups())
                        if start == 1 and end >= total:
                            read_files_set.add(args.get("path", ""))
                    # 記錄讀取的檔案（用於資料飛輪）
                    _files_read_record.add(args.get("path", ""))

            preview = result[:150] + "..." if result and len(result) > 150 else result
            print(f"   [RESULT] {preview}")

            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [tool_call]
            })
            messages.append({
                "role": "tool",
                "tool_name": tool_name,
                "content": result or "（無結果）"
            })

        # 檢查並裁切 messages 以控制總大小
        old_size = _calc_messages_size(messages)
        if old_size > MAX_MESSAGES_BUDGET:
            messages = _trim_messages_to_budget(messages)
            new_size = _calc_messages_size(messages)
            print(f"   [TRIM] Messages 超預算: {old_size:,} -> {new_size:,} chars")

    print("[WARN] 達到最大探索次數\n")

    # GPT建議：若沒有讀到任何相關檔案，提示偏向拒答
    no_evidence_hint = ""
    if not _files_read_record and not stack_preread_context and not code_rag_context:
        no_evidence_hint = "\n\n注意：目前沒有找到任何與問題強相關的程式碼。若無法確定答案，請直接說明「專案中沒有足夠資訊判斷」，不要想像不存在的函式或配置。"

    summary_prompt = f"""請根據目前收集到的資訊，盡可能回答用戶的問題。
如果資訊不足，請說明你已經知道什麼，還缺少什麼。{no_evidence_hint}"""

    messages.append({"role": "user", "content": summary_prompt})

    # 串流輸出最終回答
    print("[NOTE] 根據已收集資訊回答：\n")
    content = call_llm_with_tools_stream(messages, temperature=agent_temperature)

    if content:
        return _make_return(content)

    return _make_return("[WARN] 達到最大探索次數，請嘗試更具體的問題。")
