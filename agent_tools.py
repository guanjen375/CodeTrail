#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - Agent 工具定義與執行器
"""

import os
import re
import json
import fnmatch
import shlex
import subprocess
import shutil
from pathlib import Path
from typing import Optional

import config
import container_runner
from config import (
    MAX_FILE_READ_CHARS, MAX_GREP_RESULTS, MAX_LIST_DEPTH,
    IGNORED_PATTERNS, GREP_DEFAULT_EXTENSIONS, ALLOWED_DOT_DIRS,
    RUN_COMMAND_TIMEOUT, RUN_COMMAND_MAX_OUTPUT,
    RUN_COMMAND_TAIL_RATIO, RUN_COMMAND_ERROR_PATTERNS,
    ALLOWED_COMMANDS,
    PATCH_MAX_FILES, PATCH_MAX_LINES_PER_FILE,
    LINT_COMMANDS,
)
from utils import should_ignore_dir, should_ignore_file


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


def get_native_tools() -> list:
    """動態決定要包含哪些工具

    改進：使用函數而非常量，讓 --run-tests/--patch 可以在 main.py 處理完參數後生效
    """
    tools = list(_BASE_TOOLS)

    if config.RUN_COMMAND_ENABLED:
        tools.append(_RUN_COMMAND_TOOL)

    if config.PATCH_ENABLED:
        tools.extend([_APPLY_PATCH_TOOL, _GIT_STATUS_TOOL, _GIT_DIFF_TOOL, _RUN_LINT_TOOL])

    return tools


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
        """P0 改進：使用 line-based streaming 避免載入整個檔案"""
        import linecache

        target = self._safe_path(path)

        if not target or not target.exists():
            return f"錯誤: 檔案不存在 '{path}'"
        if not target.is_file():
            return f"錯誤: '{path}' 不是檔案"

        target_str = str(target)
        start_line = max(1, start_line)
        truncated_by_limit = False

        # P0 改進：使用 linecache 進行 line-based 讀取
        # linecache 會快取檔案，對重複讀取同一檔案更有效率
        # 先清除舊的快取（避免檔案變更後讀到舊內容）
        linecache.checkcache(target_str)

        # 計算總行數（使用 generator 避免一次載入整個檔案）
        try:
            with open(target, 'r', encoding='utf-8', errors='replace') as f:
                total = sum(1 for _ in f)
        except Exception as e:
            return f"錯誤: {e}"

        # 計算 end_line
        if end_line is None:
            char_count = 0
            end_line = start_line
            for i in range(start_line, total + 1):
                line = linecache.getline(target_str, i)
                char_count += len(line)
                if char_count > MAX_FILE_READ_CHARS:
                    truncated_by_limit = True
                    break
                end_line = i
        else:
            end_line = min(end_line, total)

        # 讀取指定範圍的行
        selected = []
        for i in range(start_line, end_line + 1):
            line = linecache.getline(target_str, i)
            # getline 返回含 \n 的行，需要 rstrip
            selected.append(line.rstrip('\n\r'))

        numbered = [f"{i:4d} | {line}" for i, line in enumerate(selected, start_line)]

        header = f"=== {path} (行 {start_line}-{end_line} / 共 {total} 行) ===\n"

        if end_line < total:
            if truncated_by_limit:
                footer = f"\n\n⚠️ [CTX] 因 MAX_FILE_READ_CHARS 限制只讀到第 {end_line} 行。用 read_file('{path}', {end_line + 1}) 繼續讀取。"
            else:
                footer = f"\n... 用 read_file('{path}', {end_line + 1}) 繼續"
        else:
            footer = ""

        return header + "\n".join(numbered) + footer

    def _is_redos_risk(self, pattern: str) -> bool:
        """檢查 pattern 是否有 ReDoS 風險"""
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

    def _rg_available(self) -> bool:
        if not hasattr(self, "_has_rg"):
            self._has_rg = shutil.which("rg") is not None
        return self._has_rg

    def _grep_with_rg(self, pattern: str, target: Path, include_patterns: list,
                      context: int, use_literal: bool) -> tuple[list, int, bool] | str:
        def _run(case_insensitive: bool):
            cmd = ["rg", "--no-heading", "--color", "never", "--line-number"]
            if context > 0:
                cmd += ["-C", str(context)]
            for p in include_patterns:
                if p:
                    cmd += ["-g", p]
            if use_literal:
                cmd.append("-F")
            if case_insensitive:
                cmd.append("-i")
            cmd += ["--", pattern, str(target)]
            try:
                result = subprocess.run(
                    cmd,
                    cwd=str(self.root),
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                return result.returncode, result.stdout, result.stderr
            except FileNotFoundError:
                return 2, "", "rg not found"
            except subprocess.TimeoutExpired:
                return 2, "", "rg timeout"

        rc, stdout, stderr = _run(False)
        if rc == 1 and not stdout:
            rc, stdout, stderr = _run(True)

        if rc not in (0, 1):
            return f"錯誤: rg 執行失敗 - {stderr.strip() or 'unknown'}"

        if not stdout.strip():
            return [], 0, False

        lines = stdout.splitlines()
        results = []
        match_count = 0
        truncated = False
        match_line_re = re.compile(r'^.+?:\d+:')

        for line in lines:
            if match_line_re.match(line):
                match_count += 1
            if match_count > MAX_GREP_RESULTS:
                truncated = True
                break
            results.append(line)

        return results, match_count, truncated

    def grep(self, pattern: str, path: str = ".", include: str = None, context: int = 0) -> str:
        """搜尋 pattern"""
        target = self._safe_path(path)
        if not target or not target.exists():
            return f"錯誤: 路徑不存在 '{path}'"

        # ReDoS 保護：檢查危險 pattern
        use_literal = self._is_redos_risk(pattern)
        if use_literal:
            escaped = re.escape(pattern)
            regex_cs = re.compile(escaped)
            regex_ci = re.compile(escaped, re.IGNORECASE)
        else:
            try:
                regex_cs = re.compile(pattern)
                regex_ci = re.compile(pattern, re.IGNORECASE)
            except re.error:
                escaped = re.escape(pattern)
                regex_cs = re.compile(escaped)
                regex_ci = re.compile(escaped, re.IGNORECASE)

        if include is None:
            include = GREP_DEFAULT_EXTENSIONS

        include_patterns = [p.strip() for p in include.split(',')]

        # Fast path: ripgrep
        if self._rg_available():
            rg_result = self._grep_with_rg(pattern, target, include_patterns, context, use_literal)
            if isinstance(rg_result, str):
                return rg_result
            results, match_count, truncated = rg_result
            if not results:
                return f"沒有找到 '{pattern}'"

            header = f"=== rg '{pattern}' ({match_count} matches) ===\n"
            body = "\n".join(results)
            if truncated or match_count >= MAX_GREP_RESULTS:
                body += (
                    f"\n\n[CTX] rg 已達 MAX_GREP_RESULTS={MAX_GREP_RESULTS}，結果可能不完整，"
                    f"建議縮小 path/include 或用更精準的 pattern。"
                )
            return header + body

        files = []
        if target.is_file():
            files = [target]
        else:
            for dirpath, dirnames, filenames in os.walk(target):
                rel_dir = Path(dirpath).relative_to(target)
                dirnames[:] = [d for d in dirnames if not should_ignore_dir(rel_dir / d)]

                for fname in filenames:
                    if any(fnmatch.fnmatch(fname, p) for p in include_patterns):
                        fp = Path(dirpath) / fname
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

        header = f"=== grep '{pattern}' ({len(results)} 結果) ===\n"
        body = "\n".join(results)

        if len(results) >= MAX_GREP_RESULTS:
            body += f"\n\n⚠️ [CTX] grep 已達 MAX_GREP_RESULTS={MAX_GREP_RESULTS}，結果可能不完整。建議縮小 path/include 或用更精準的 pattern。"

        return header + body

    def _safe_regex_search(self, regex, text: str, timeout_chars: int = 10000) -> bool:
        """安全的 regex search，對超長行做截斷保護"""
        if len(text) > timeout_chars:
            text = text[:timeout_chars]
        return regex.search(text) is not None

    def _grep_with_context(self, files: list, regex, context: int) -> list:
        """搜尋檔案並支援上下文顯示"""
        results = []
        context = min(context, 5)

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
                            start = max(0, i - context)
                            end = min(len(lines), i + context + 1)
                            ctx_lines = []
                            for j in range(start, end):
                                prefix = ">" if j == i else " "
                                ctx_lines.append(f"{prefix}{j+1:4d}| {lines[j][:120]}")
                            results.append(f"--- {rel}:{i+1} ---\n" + "\n".join(ctx_lines))
                        else:
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

    def _validate_command(self, command: str) -> tuple[bool, str, list]:
        """驗證命令是否安全且在白名單中

        Returns:
            (is_valid, error_message, cmd_parts)
        """
        command = command.strip()

        try:
            cmd_parts = shlex.split(command)
        except ValueError as e:
            return False, f"錯誤: 命令解析失敗 - {e}", []

        if not cmd_parts:
            return False, "錯誤: 空命令", []

        # 驗證命令是否在白名單中
        is_allowed = False
        for allowed in ALLOWED_COMMANDS:
            allowed_parts = shlex.split(allowed)
            if cmd_parts[:len(allowed_parts)] == allowed_parts:
                is_allowed = True
                break

        if not is_allowed:
            allowed_list = ', '.join(ALLOWED_COMMANDS[:8])
            return False, f"錯誤: 不允許的命令。\n允許的命令前綴: {allowed_list}...", []

        # 額外安全檢查：危險字元
        dangerous_patterns = ['$(', '`', '&&', '||', ';', '|', '>', '<']
        for part in cmd_parts:
            for pattern in dangerous_patterns:
                if pattern in part:
                    return False, f"錯誤: 參數包含不允許的字元 '{pattern}'", []

        return True, "", cmd_parts

    def run_command(self, command: str, timeout: int = RUN_COMMAND_TIMEOUT) -> str:
        """執行白名單內的測試/建置命令"""
        if not config.RUN_COMMAND_ENABLED:
            return "錯誤: run_command 功能已停用（可用 --run-tests 或設定 AI_CODE_RUN_TESTS=1 啟用）"

        # 統一驗證（容器/非容器模式都要過白名單）
        is_valid, error_msg, cmd_parts = self._validate_command(command)
        if not is_valid:
            return error_msg

        # 容器化執行模式
        if container_runner.CONTAINER_ENABLED:
            return self._run_command_in_container(command, timeout)

        try:
            print(f"   [RUN] 執行: {command}")
            result = subprocess.run(
                cmd_parts,
                shell=False,
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
        """在容器中執行命令"""
        command = command.strip()

        dangerous_patterns = ['rm -rf', 'mkfs', 'dd if=', ':(){ :|:& };:']
        for pattern in dangerous_patterns:
            if pattern in command:
                return f"錯誤: 命令包含危險操作 '{pattern}'"

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

        output = smart_truncate_output(output, RUN_COMMAND_MAX_OUTPUT, RUN_COMMAND_TAIL_RATIO)

        status = "✓ 成功" if result['success'] else f"✗ 失敗 (exit {result['returncode']})"
        return f"=== {status} (容器模式) ===\n{output}" if output else f"=== {status} (容器模式, 無輸出) ==="

    # ============================================================
    # 改碼閉環工具
    # ============================================================
    def apply_patch(self, patch: str, dry_run: bool = False) -> str:
        """套用 unified diff 格式的 patch"""
        if not config.PATCH_ENABLED:
            return "錯誤: apply_patch 功能已停用（可用 --patch 或設定 AI_CODE_PATCH=1 啟用）"

        try:
            changes = self._parse_unified_diff(patch)
        except ValueError as e:
            return f"錯誤: patch 解析失敗 - {e}"

        if not changes:
            return "錯誤: 無法從 patch 中解析出任何修改"

        if len(changes) > PATCH_MAX_FILES:
            return f"錯誤: 修改檔案數量超過限制（{len(changes)} > {PATCH_MAX_FILES}）"

        results = []
        successfully_patched = []

        for filepath, hunks in changes.items():
            target = self._safe_path(filepath)
            if not target:
                results.append(f"✗ {filepath}: 路徑不在專案內或無效")
                continue

            total_lines = sum(len(h['add']) + len(h['remove']) for h in hunks)
            if total_lines > PATCH_MAX_LINES_PER_FILE:
                results.append(f"✗ {filepath}: 修改行數超過限制（{total_lines} > {PATCH_MAX_LINES_PER_FILE}）")
                continue

            if dry_run:
                results.append(f"[DRY RUN] {filepath}: 將修改 {len(hunks)} 個區塊, {total_lines} 行")
                for i, hunk in enumerate(hunks):
                    results.append(f"  區塊 {i+1}: 行 {hunk['old_start']}-{hunk['old_start']+hunk['old_count']-1}")
                continue

            try:
                result = self._apply_hunks_to_file(target, hunks)
                results.append(result)
                if result.startswith("✓"):
                    successfully_patched.append(filepath)
            except Exception as e:
                results.append(f"✗ {filepath}: 套用失敗 - {e}")

        # P2 改進：自動驗證流程
        if successfully_patched and not dry_run:
            verify_results = self._verify_patched_files(successfully_patched)
            results.extend(verify_results)

        return "\n".join(results) if results else "沒有修改"

    def _verify_patched_files(self, filepaths: list) -> list:
        """P2 改進：驗證修改後的檔案

        驗證步驟：
        1. Lint/Format
        2. 靜態分析（如 mypy）
        3. 測試（若有）
        """
        results = []
        all_passed = True

        # Step 1: Lint
        if "lint" in getattr(config, 'PATCH_VERIFY_STEPS', []):
            results.append("\n=== [1/3] Lint ===")
            for filepath in filepaths:
                ext = Path(filepath).suffix.lower()
                if ext in LINT_COMMANDS:
                    try:
                        lint_result = self.run_lint(filepath, fix=True)
                        if "✓" in lint_result:
                            results.append(f"  ✓ {filepath}")
                        elif "⚠" in lint_result or "錯誤" in lint_result:
                            results.append(f"  ⚠ {filepath}: {lint_result[:80]}")
                            all_passed = False
                    except Exception as e:
                        results.append(f"  ✗ {filepath}: {e}")
                        all_passed = False

        # Step 2: Typecheck (靜態分析)
        typecheck_cmds = getattr(config, 'TYPECHECK_COMMANDS', {})
        if "typecheck" in getattr(config, 'PATCH_VERIFY_STEPS', []) and typecheck_cmds:
            results.append("\n=== [2/3] 靜態分析 ===")
            for filepath in filepaths:
                ext = Path(filepath).suffix.lower()
                if ext in typecheck_cmds:
                    for cmd_template in typecheck_cmds[ext]:
                        try:
                            cmd = f"{cmd_template} {filepath}"
                            result = self.run_command(cmd, timeout=30)
                            if "error" in result.lower() or "Error" in result:
                                results.append(f"  ⚠ {filepath}: 有型別錯誤")
                                all_passed = False
                            else:
                                results.append(f"  ✓ {filepath}")
                        except Exception as e:
                            results.append(f"  ⚠ {filepath}: 跳過 ({e})")

        # Step 3: 測試 (只執行相關測試，避免跑太久)
        if "test" in getattr(config, 'PATCH_VERIFY_STEPS', []) and config.RUN_COMMAND_ENABLED:
            results.append("\n=== [3/3] 測試 ===")
            # 檢查是否有 pytest
            test_patterns = []
            for filepath in filepaths:
                if filepath.endswith('.py'):
                    # 嘗試找對應的測試檔案
                    base = Path(filepath).stem
                    test_patterns.append(f"test_{base}.py")
                    test_patterns.append(f"{base}_test.py")

            if test_patterns:
                try:
                    # 只執行相關測試（用 -k 過濾）
                    # 注意：不使用 pipe（|）和重導向，因為會被安全檢查擋掉
                    # 輸出截斷改由 Python 處理
                    keywords = " or ".join(p.replace('.py', '') for p in test_patterns[:3])
                    test_cmd = f"pytest -x -q -k \"{keywords}\" --tb=short"
                    test_result = self.run_command(test_cmd, timeout=60)
                    # 截斷過長輸出（原本用 head -20 的功能）
                    test_lines = test_result.split('\n')
                    if len(test_lines) > 25:
                        test_result = '\n'.join(test_lines[:25]) + f"\n... (截斷，共 {len(test_lines)} 行)"
                    if "FAILED" in test_result or "ERROR" in test_result:
                        results.append(f"  ✗ 測試失敗")
                        results.append(f"    {test_result[:200]}")
                        all_passed = False
                    elif "passed" in test_result:
                        results.append(f"  ✓ 測試通過")
                    else:
                        results.append(f"  - 沒有找到相關測試")
                except Exception as e:
                    results.append(f"  ⚠ 測試跳過: {e}")

        # 總結
        if all_passed:
            results.append("\n✓ 所有驗證通過")
        else:
            results.append("\n⚠ 有驗證項目未通過，建議檢查")

        return results

    def _parse_unified_diff(self, patch: str) -> dict:
        """解析 unified diff 格式"""
        changes = {}
        lines = patch.split('\n')
        i = 0
        current_file = None

        while i < len(lines):
            line = lines[i]

            if line.startswith('--- '):
                path = line[4:].strip()
                if path.startswith('a/'):
                    path = path[2:]
                path = path.split('\t')[0].strip()
                current_file = path
                i += 1
                continue

            if line.startswith('+++ '):
                path = line[4:].strip()
                if path.startswith('b/'):
                    path = path[2:]
                path = path.split('\t')[0].strip()
                current_file = path
                if current_file not in changes:
                    changes[current_file] = []
                i += 1
                continue

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
                        'lines': [],
                        'add': [],
                        'remove': []
                    }

                    i += 1
                    while i < len(lines):
                        hunk_line = lines[i]
                        if not hunk_line:
                            hunk['lines'].append((' ', ''))
                            i += 1
                        elif hunk_line.startswith(' '):
                            hunk['lines'].append((' ', hunk_line[1:]))
                            i += 1
                        elif hunk_line.startswith('+') and not hunk_line.startswith('+++'):
                            hunk['lines'].append(('+', hunk_line[1:]))
                            hunk['add'].append(hunk_line[1:])
                            i += 1
                        elif hunk_line.startswith('-') and not hunk_line.startswith('---'):
                            hunk['lines'].append(('-', hunk_line[1:]))
                            hunk['remove'].append(hunk_line[1:])
                            i += 1
                        elif hunk_line.startswith('@@') or hunk_line.startswith('---'):
                            break
                        else:
                            break

                    changes[current_file].append(hunk)
                    continue

            i += 1

        return changes

    def _verify_hunk_context(self, lines: list, hunk: dict) -> tuple:
        """驗證 hunk 的 context 行是否與檔案內容匹配"""
        start_idx = hunk['old_start'] - 1
        file_line_idx = start_idx

        for line_type, content in hunk['lines']:
            if line_type in (' ', '-'):
                if file_line_idx >= len(lines):
                    return False, f"行 {file_line_idx + 1}: 超出檔案範圍"

                file_line = lines[file_line_idx]
                if file_line.rstrip() != content.rstrip():
                    if file_line.strip() != content.strip():
                        return False, (
                            f"行 {file_line_idx + 1} context 不匹配:\n"
                            f"  期望: '{content[:60]}...'\n"
                            f"  實際: '{file_line[:60]}...'"
                        )
                file_line_idx += 1

        return True, ""

    def _apply_hunks_to_file(self, filepath: Path, hunks: list) -> str:
        """將 hunks 套用到檔案"""
        if not filepath.exists():
            new_lines = []
            for hunk in hunks:
                for line_type, content in hunk['lines']:
                    if line_type in (' ', '+'):
                        new_lines.append(content)
            filepath.write_text('\n'.join(new_lines) + '\n', encoding='utf-8')
            return f"✓ {filepath.relative_to(self.root)}: 新建檔案"

        original = filepath.read_text(encoding='utf-8', errors='replace')
        lines = original.split('\n')

        for i, hunk in enumerate(hunks):
            is_valid, error_msg = self._verify_hunk_context(lines, hunk)
            if not is_valid:
                return f"✗ {filepath.relative_to(self.root)}: 區塊 {i+1} {error_msg}"

        backup_path = filepath.with_suffix(filepath.suffix + '.orig')
        backup_path.write_text(original, encoding='utf-8')

        sorted_hunks = sorted(hunks, key=lambda h: h['old_start'], reverse=True)

        for hunk in sorted_hunks:
            start_idx = hunk['old_start'] - 1
            old_count = hunk['old_count']

            new_lines = []
            for line_type, content in hunk['lines']:
                if line_type in (' ', '+'):
                    new_lines.append(content)

            lines[start_idx:start_idx + old_count] = new_lines

        filepath.write_text('\n'.join(lines), encoding='utf-8')

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
                return f"錯誤: {result.stderr.strip() or '不是 git 倉庫'}"

            output = result.stdout.strip()
            if not output:
                return "工作目錄乾淨（沒有修改）"

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
                    break
                else:
                    if "not found" in result.stderr.lower() or "not recognized" in result.stderr.lower():
                        continue
                    output = result.stderr.strip() or result.stdout.strip()
                    results.append(f"⚠ {tool_name}:\n{output[:500]}")
                    break

            except FileNotFoundError:
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
