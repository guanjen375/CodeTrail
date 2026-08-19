#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - Agent 工具定義與執行器
"""

import os
import re
import sys
import json
import codecs
import fnmatch
import shlex
import subprocess
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import config
import container_runner
from media import BINARY_EXTENSIONS, ELF_EXTENSIONS
from config import (
    IMAGE_EXTENSIONS,
    MAX_FILE_READ_CHARS, MAX_GREP_RESULTS,
    MAX_GREP_LINE_CHARS,
    MAX_GREP_OUTPUT_CHARS, MAX_LIST_DEPTH,
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
        "description": "套用 unified diff 格式的程式碼修改。修改會直接寫入檔案。定位靠 context 行內容,@@ 行號可省略、不必計算行數。",
        "parameters": {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": "unified diff 格式的修改內容。hunk header 寫 @@ 即可(行號選填,只當多處匹配時的提示);修改行前後帶 2-3 行 context,context 必須與檔案現況一致。例如：\n--- a/file.py\n+++ b/file.py\n@@\n context line\n-old line\n+new line\n+added line"
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

    使用函數而非常量，讓 env/MCP runtime 對 RUN_COMMAND_ENABLED/PATCH_ENABLED
    的明確設定能在組工具清單時生效。
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
# read_file 的「已知非文字」副檔名黑名單:這些格式硬讀只會吐亂碼或空白,
# 不必開檔就直接導向 analyze_file / ingest_document。刻意不含 .dat/.raw/.hex/
# .out 等可能是文字的模糊副檔名——那些交給 _sniff_text_encoding 的內容判斷。
_NONTEXT_EXTENSIONS = {
    # 圖片
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tif", ".tiff",
    # 壓縮 / 封裝
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".7z", ".rar", ".tar", ".whl",
    # 編譯產物 / 可執行
    ".so", ".o", ".a", ".elf", ".ko", ".axf", ".pyc", ".pyo", ".wasm",
    ".class", ".jar", ".dex", ".dll", ".exe", ".dylib", ".bin",
    # 資料庫 / 序列化 / 模型
    ".sqlite", ".sqlite3", ".db", ".npz", ".npy", ".pkl", ".pickle",
    ".pt", ".pth", ".onnx", ".gguf", ".safetensors",
    # 文件容器
    ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".doc", ".xls", ".ppt", ".epub",
    # 影音 / 字型
    ".mp3", ".mp4", ".m4a", ".avi", ".mkv", ".mov", ".flac", ".ogg", ".wav", ".webm",
    ".ttf", ".otf", ".woff", ".woff2",
}

# analyze_file 實際吃得下的格式(media dispatch 四類)。黑名單提示分流用:
# 在這集合內才導向 analyze_file,其餘(.docx/.zip/.mp4/.sqlite...)老實說
# 沒有工具能解析,要先轉檔——導向不支援的工具只會讓模型空轉。
_ANALYZABLE_EXTENSIONS = IMAGE_EXTENSIONS | ELF_EXTENSIONS | BINARY_EXTENSIONS | {".pdf"}


def _sniff_text_encoding(head: bytes):
    """判斷檔案開頭 bytes 是否為文字,回 (encoding, None) 或 (None, 拒絕原因)。

    取代單看 NUL 的 heuristic——那會放行不含 NUL 的 binary(例如全 0xFF 的
    firmware,讀出來全是亂碼或空白),又誤殺含 NUL 的 UTF-16 純文字 log。
    判斷順序:
      1. BOM(UTF-8/16/32):確定性,直接採信
      2. 無 BOM 但含 NUL:ASCII 主體的 UTF-16 有「一半 byte 幾乎全 NUL、
         另一半幾乎全可印字元」的交錯 pattern;不符就視為二進位
      3. 無 BOM 無 NUL:strict UTF-8 decode;失敗時只有「幾乎全 ASCII、
         零星壞 byte」(log 夾到 binary 噴濺)才放行,其餘拒絕
    """
    if not head:
        return "utf-8", None  # 空檔案當文字
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig", None
    # UTF-32-LE BOM(FF FE 00 00)是 UTF-16-LE BOM(FF FE)的前綴,必須先查 32
    if head.startswith(codecs.BOM_UTF32_LE) or head.startswith(codecs.BOM_UTF32_BE):
        return "utf-32", None
    if head.startswith(codecs.BOM_UTF16_LE) or head.startswith(codecs.BOM_UTF16_BE):
        return "utf-16", None

    def _text_ratio(bs: bytes) -> float:
        if not bs:
            return 0.0
        ok = sum(1 for b in bs if 0x20 <= b < 0x7F or b in (0x09, 0x0A, 0x0D))
        return ok / len(bs)

    if b"\x00" in head:
        even, odd = head[0::2], head[1::2]
        if odd and odd.count(0) / len(odd) > 0.7 and _text_ratio(even) > 0.7:
            return "utf-16-le", None
        if even and even.count(0) / len(even) > 0.7 and _text_ratio(odd) > 0.7:
            return "utf-16-be", None
        return None, "二進位檔(內含 NUL byte)"

    try:
        decoded = head.decode("utf-8")
    except UnicodeDecodeError as e:
        # 尾端容錯只認「多位元組字元被讀取邊界切斷」:位置在結尾**且** reason
        # 是 unexpected end of data。單看位置會把 b"\xff" 這種單 byte binary
        # 放行(檔案夠短時任何錯誤位置都算「在結尾」)。
        if e.start >= len(head) - 3 and "unexpected end of data" in (e.reason or ""):
            decoded = head[:e.start].decode("utf-8")
        elif _text_ratio(head) >= 0.90:
            decoded = head.decode("utf-8", errors="replace")  # 零星壞 byte 以 U+FFFD 呈現
        else:
            return None, "非 UTF-8 文字或二進位內容"
    # decode 成功不代表是文字:C0 控制字元(\x01...)是合法 UTF-8,
    # 全控制字元的 binary 會 strict decode 過關。再用字元層 printable 比例擋。
    if decoded:
        printable = sum(1 for ch in decoded if ch.isprintable() or ch in "\t\n\r")
        if printable / len(decoded) < 0.90:
            return None, "二進位內容(控制字元比例過高)"
    return "utf-8", None



def _clip_grep_line(line: str) -> str:
    """單行硬上限。grep 的爆量幾乎都來自生成檔的超長行,不是 match 太多。"""
    if len(line) <= MAX_GREP_LINE_CHARS:
        return line
    return line[:MAX_GREP_LINE_CHARS] + f"…[行過長,已截斷 {len(line) - MAX_GREP_LINE_CHARS} 字元]"


def _collect_within_budget(lines) -> tuple[list, bool]:
    """逐行套用行上限並累計總長度,超過整體預算就停手。"""
    out, total = [], 0
    for line in lines:
        clipped = _clip_grep_line(line)
        if total + len(clipped) + 1 > MAX_GREP_OUTPUT_CHARS:
            return out, True
        out.append(clipped)
        total += len(clipped) + 1
    return out, False


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
        """P0 改進：line-based streaming 單趟讀取，避免載入整個檔案"""
        target = self._safe_path(path)

        if not target or not target.exists():
            return f"錯誤: 檔案不存在 '{path}'"
        if not target.is_file():
            return f"錯誤: '{path}' 不是檔案"

        # 純文字通道分流：PDF/二進位硬讀只會吐 U+FFFD 亂碼——燒 context 又
        # 容易誘發模型腦補。回導引訊息比「成功地讀出垃圾」誠實。
        if target.suffix.lower() == ".pdf":
            return (f"錯誤: '{path}' 是 PDF，read_file 只處理純文字。"
                    "想這一輪看一眼用 analyze_file(path)；"
                    "要之後隨時可查用 ingest_document(path)。")
        ext = target.suffix.lower()
        if ext in _NONTEXT_EXTENSIONS:
            if ext in _ANALYZABLE_EXTENSIONS:
                return (f"錯誤: '{path}' 是二進位/非文字格式（{ext}），"
                        "read_file 只處理純文字。請改用 analyze_file"
                        "（圖片/ELF/binary/PDF）解析。")
            return (f"錯誤: '{path}' 是二進位/非文字格式（{ext}），read_file 只處理"
                    "純文字，且 analyze_file/ingest_document 也不支援此格式；"
                    "請先轉成純文字、PDF 或圖片再處理。")

        try:
            with open(target, 'rb') as fb:
                head = fb.read(8192)
        except Exception as e:
            return f"錯誤: {e}"
        encoding, reject_reason = _sniff_text_encoding(head)
        if encoding is None:
            return (f"錯誤: '{path}' 判定為{reject_reason}，"
                    "read_file 只處理純文字。請改用 analyze_file"
                    "（圖片/ELF/binary/PDF）或 ingest_document 入庫；"
                    "若確定是其他編碼的純文字，請先轉成 UTF-8。")

        start_line = max(1, start_line)
        truncated_by_limit = False
        line_clipped = False

        # 單趟 streaming：邊數總行數邊收集目標範圍。用 sniff 出的編碼 +
        # errors='replace' 讀——以前 linecache 走 strict decode，檔案中段
        # 一個壞 byte 會讓「整個檔案」的每一行都靜默變成空字串。
        # MAX_FILE_READ_CHARS 是硬預算：指定 end_line 的大範圍照樣受限，
        # 首行本身超限也只放行預算內的前段（不然單行巨檔會整行進 context）。
        selected: list = []
        total = 0
        char_count = 0
        try:
            with open(target, 'r', encoding=encoding, errors='replace') as f:
                for i, line in enumerate(f, 1):
                    total = i
                    if i < start_line:
                        continue
                    if end_line is not None and i > end_line:
                        continue  # 之後只數總行數
                    if truncated_by_limit:
                        continue
                    if char_count + len(line) > MAX_FILE_READ_CHARS:
                        if not selected:
                            selected.append(line[:MAX_FILE_READ_CHARS].rstrip('\n\r'))
                            line_clipped = True
                        truncated_by_limit = True
                        continue
                    char_count += len(line)
                    selected.append(line.rstrip('\n\r'))
        except Exception as e:
            return f"錯誤: {e}"

        if selected:
            end_line = start_line + len(selected) - 1
        else:
            end_line = min(end_line, total) if end_line is not None else start_line

        numbered = [f"{i:4d} | {line}" for i, line in enumerate(selected, start_line)]

        header = f"=== {path} (行 {start_line}-{end_line} / 共 {total} 行) ===\n"

        if truncated_by_limit:
            clip_note = (f"(第 {end_line} 行過長,僅顯示前 {MAX_FILE_READ_CHARS} 字元)"
                         if line_clipped else "")
            cont = (f"用 read_file('{path}', {end_line + 1}) 繼續讀取。"
                    if end_line < total else "")
            footer = f"\n\n⚠️ [CTX] 因 MAX_FILE_READ_CHARS 限制只讀到第 {end_line} 行{clip_note}。{cont}"
        elif end_line < total:
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
            # --max-columns 讓 rg 自己就不吐超長行:否則 capture_output 會先把
            # 整份 stdout(實測可達 1.3 GB)讀進記憶體,之後再截斷已經來不及。
            cmd = ["rg", "--no-heading", "--color", "never", "--line-number",
                   "--max-columns", str(MAX_GREP_LINE_CHARS)]
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
        total = 0
        match_line_re = re.compile(r'^.+?:\d+:')

        for line in lines:
            if match_line_re.match(line):
                match_count += 1
            if match_count > MAX_GREP_RESULTS:
                truncated = True
                break
            # 位元組預算與筆數預算是兩回事:超長行(生成檔/壓縮 JSON)能讓
            # 25 個 match 撐出 GB 級字串,經 MCP 送出去會把前端打死。
            clipped = _clip_grep_line(line)
            if total + len(clipped) + 1 > MAX_GREP_OUTPUT_CHARS:
                truncated = True
                break
            results.append(clipped)
            total += len(clipped) + 1

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
                    f"\n\n[CTX] rg 結果不完整(上限 MAX_GREP_RESULTS={MAX_GREP_RESULTS}、"
                    f"MAX_GREP_OUTPUT_CHARS={MAX_GREP_OUTPUT_CHARS}、"
                    f"單行 MAX_GREP_LINE_CHARS={MAX_GREP_LINE_CHARS})，"
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
        kept, over_budget = _collect_within_budget(results)
        body = "\n".join(kept)
        if over_budget:
            body += (
                f"\n\n⚠️ [CTX] grep 輸出已達 MAX_GREP_OUTPUT_CHARS={MAX_GREP_OUTPUT_CHARS}，"
                "結果已截斷。請縮小 path/include 或用更精準的 pattern。"
            )

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

    # ============================================================
    # Path containment for run_command
    # ============================================================
    # 帶路徑的常見 flag(下一個 token 是 path)
    _PATH_FLAGS_NEXT = {
        "-C", "--directory",
        "-f", "--file",
        "--build",
        "--project", "--project-dir",
        "--config", "-c",
        "-S",  # cmake source
        "-B",  # cmake build
    }
    # 形如 --foo=path 的 flag
    _PATH_FLAGS_INLINE = {
        "--directory", "--build", "--project", "--project-dir",
        "--config", "-S", "-B", "--file",
    }
    # 看起來像 path 的 token(用來判斷哪些 free arg 要做 containment 檢查)
    @staticmethod
    def _looks_like_path(s: str) -> bool:
        if not s or s.startswith("-"):
            return False
        # 絕對路徑、~ 開頭、含 /、含 .. — 都當 path 處理
        if s.startswith(("/", "~", ".")) or "/" in s or "\\" in s:
            return True
        return False

    def _path_arg_in_root(self, raw: str) -> tuple[bool, str]:
        """判斷一個 path-like 參數是否在 sandbox root 內。

        回傳 (ok, resolved_str)。
        """
        try:
            p = Path(raw).expanduser()
            if not p.is_absolute():
                resolved = (self.root / p).resolve()
            else:
                resolved = p.resolve()
            resolved.relative_to(self.root)
            return True, str(resolved)
        except (ValueError, OSError):
            return False, raw

    def _check_path_containment(self, cmd_parts: list) -> tuple[bool, str]:
        """檢查白名單命令的所有 path-like 參數都在 root 內。"""
        i = 0
        while i < len(cmd_parts):
            tok = cmd_parts[i]
            # --foo=path 形式
            if "=" in tok and tok.startswith("-"):
                flag, _, val = tok.partition("=")
                if flag in self._PATH_FLAGS_INLINE and self._looks_like_path(val):
                    ok, _ = self._path_arg_in_root(val)
                    if not ok:
                        return False, f"路徑超出 sandbox: {flag}={val}"
            # 帶下一個 token 的 path flag
            elif tok in self._PATH_FLAGS_NEXT and i + 1 < len(cmd_parts):
                nxt = cmd_parts[i + 1]
                if self._looks_like_path(nxt):
                    ok, _ = self._path_arg_in_root(nxt)
                    if not ok:
                        return False, f"路徑超出 sandbox: {tok} {nxt}"
                i += 1  # consume value
            # 自由 arg(不是 flag),看起來像 path 就檢查
            elif not tok.startswith("-") and self._looks_like_path(tok):
                ok, _ = self._path_arg_in_root(tok)
                if not ok:
                    return False, f"路徑超出 sandbox: {tok}"
            i += 1
        return True, ""

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

        # Path containment：白名單命令的參數不能逃出 AICODE_ROOT。
        # 阻擋 `pytest /tmp/x.py`、`make -C /tmp`、`cmake --build /abs/build` 之類。
        ok, why = self._check_path_containment(cmd_parts)
        if not ok:
            return False, f"錯誤: {why}（命令參數必須指向 AICODE_ROOT 內的路徑）", []

        return True, "", cmd_parts

    def run_command(self, command: str, timeout: int = RUN_COMMAND_TIMEOUT) -> str:
        """執行白名單內的測試/建置命令"""
        if not config.RUN_COMMAND_ENABLED:
            return "錯誤: run_command 功能已停用（設定 AI_CODE_RUN_TESTS=1 才會啟用）"

        # 統一驗證（容器/非容器模式都要過白名單）
        is_valid, error_msg, cmd_parts = self._validate_command(command)
        if not is_valid:
            return error_msg

        # 容器化執行模式
        if container_runner.CONTAINER_ENABLED:
            return self._run_command_in_container(command, timeout)

        try:
            print(f"   [RUN] 執行: {command}", file=sys.stderr)
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

        print(f"   [CONTAINER] 執行: {command}", file=sys.stderr)

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
            return "錯誤: apply_patch 功能已停用（設定 AI_CODE_PATCH=1 才會啟用）"

        try:
            changes = self._parse_unified_diff(patch)
        except ValueError as e:
            return f"錯誤: patch 解析失敗 - {e}"

        if not changes:
            return "錯誤: 無法從 patch 中解析出任何修改"

        if len(changes) > PATCH_MAX_FILES:
            return f"錯誤: 修改檔案數量超過限制（{len(changes)} > {PATCH_MAX_FILES}）"

        # ============================================================
        # Phase 1: preflight 全部檔案（不寫入任何東西）
        #   - 路徑必須在 sandbox 內
        #   - 行數限制
        #   - 既有檔案的每個 hunk 必須能靠 context 內容定位
        #     （dry_run 也要驗，與工具說明一致）
        # ============================================================
        plans = []      # [(filepath, target, hunks, plan_or_None, original_or_None)]
        errors = []
        for filepath, hunks in changes.items():
            target = self._safe_path(filepath)
            if not target:
                errors.append(f"✗ {filepath}: 路徑不在專案內或無效")
                continue

            total_lines = sum(len(h['add']) + len(h['remove']) for h in hunks)
            if total_lines > PATCH_MAX_LINES_PER_FILE:
                errors.append(f"✗ {filepath}: 修改行數超過限制（{total_lines} > {PATCH_MAX_LINES_PER_FILE}）")
                continue

            if target.exists():
                try:
                    original = target.read_text(encoding='utf-8', errors='replace')
                except Exception as e:
                    errors.append(f"✗ {filepath}: 讀取失敗 - {e}")
                    continue
                plan, hunk_err = self._locate_hunks(original.split('\n'), hunks)
                if hunk_err:
                    errors.append(f"✗ {filepath}: {hunk_err}")
                    continue
                plans.append((filepath, target, hunks, plan, original))
            else:
                plans.append((filepath, target, hunks, None, None))

        # ---- dry_run: 只報告 preflight 結果，不寫入 ----
        if dry_run:
            results = []
            for filepath, target, hunks, plan, original in plans:
                total_lines = sum(len(h['add']) + len(h['remove']) for h in hunks)
                if original is None:
                    results.append(
                        f"[DRY RUN] {filepath}: 將新建檔案 {len(hunks)} 個區塊, "
                        f"{total_lines} 行"
                    )
                    continue
                pending = [e for e in plan if e['status'] == 'apply']
                results.append(
                    f"[DRY RUN] {filepath}: 將修改 {len(pending)} 個區塊, "
                    f"{total_lines} 行（context 已依內容定位）"
                )
                for e in plan:
                    if e['status'] == 'already':
                        results.append(
                            f"  區塊 {e['index'] + 1}: 已套用過(修改後內容見行 "
                            f"{e['pos'] + 1}),將跳過"
                        )
                    else:
                        end = e['pos'] + max(e['replace_len'], 1)
                        results.append(
                            f"  區塊 {e['index'] + 1}: 行 {e['pos'] + 1}-{end}"
                            + ("（依 context 定位）" if e['relocated'] else "")
                        )
            results.extend(errors)
            if errors:
                results.append(
                    "⚠ [DRY RUN] 上述 ✗ 檔案未通過 preflight；實際套用時整個 patch 會被拒絕，"
                    "不會寫入任何檔案（atomic）。"
                )
            return "\n".join(results) if results else "沒有修改"

        # ---- 原子性：任一檔 preflight 失敗 → 全部不套用 ----
        if errors:
            results = list(errors)
            results.append("⚠ 因有檔案未通過 preflight，整個 patch 已被拒絕，未寫入任何檔案（atomic）。")
            return "\n".join(results)

        # ============================================================
        # Phase 2: 實際套用（含失敗回滾）
        #   每個既有檔案先寫一份唯一命名的備份（不覆蓋使用者既有 .orig）；
        #   任一檔寫入拋錯 → 用備份把已寫入的檔案全部回滾。
        # ============================================================
        written = []    # [(target, backup_path_or_None)]  None = 本次新建的檔案
        results = []
        try:
            for filepath, target, hunks, plan, original in plans:
                if original is None:
                    # 先登記（backup=None 代表新建）再寫，確保寫到一半失敗時
                    # rollback 也涵蓋這一檔（把它刪掉還原成「不存在」）。
                    written.append((target, None))
                    content = self._compute_new_file_content(hunks)
                    target.write_text(content, encoding='utf-8')
                    results.append(f"✓ {filepath}: 新建檔案")
                else:
                    pending = [e for e in plan if e['status'] == 'apply']
                    already = [e for e in plan if e['status'] == 'already']
                    if not pending:
                        # 冪等:所有區塊都已套用過 → 不碰檔案、不留備份。
                        # 位置一定要報出來:no-op 若判錯,這是唯一的破綻。
                        where = ", ".join(
                            f"區塊{e['index'] + 1}→行 {e['pos'] + 1}" for e in already
                        )
                        results.append(
                            f"✓ {filepath}: 所有區塊({len(already)})都已套用過,"
                            f"檔案未變更（{where}）"
                        )
                        continue
                    fd, backup_name = tempfile.mkstemp(
                        dir=str(target.parent), prefix=target.name + '.', suffix='.orig'
                    )
                    os.close(fd)
                    backup_path = Path(backup_name)
                    backup_path.write_text(original, encoding='utf-8')
                    # backup 就緒後、寫入前先登記 → 若 write_text 中途失敗，
                    # 這一檔也能從備份還原（否則會留下半寫入的檔案 + 孤兒備份）。
                    written.append((target, backup_path))
                    content = self._compute_patched_content(original, plan)
                    target.write_text(content, encoding='utf-8')
                    msg = f"✓ {filepath}: 已修改 {len(pending)} 個區塊"
                    relocated = [e for e in pending if e['relocated']]
                    if relocated:
                        msg += (
                            "（"
                            + ", ".join(
                                f"區塊{e['index'] + 1}依 context 定位於行 {e['pos'] + 1}"
                                for e in relocated
                            )
                            + "）"
                        )
                    if already:
                        msg += (
                            "（另 "
                            + ", ".join(
                                f"區塊{e['index'] + 1}已套用過於行 {e['pos'] + 1}"
                                for e in already
                            )
                            + ",跳過）"
                        )
                    results.append(msg)
        except Exception as e:
            rollback_notes = []
            for tgt, backup_path in reversed(written):
                try:
                    if backup_path is None:
                        tgt.unlink(missing_ok=True)  # 移除本次新建的檔案
                    else:
                        tgt.write_text(backup_path.read_text(encoding='utf-8'), encoding='utf-8')
                except Exception as rexc:
                    rollback_notes.append(f"⚠ 回滾 {tgt} 失敗: {rexc}")
            for _, backup_path in written:
                if backup_path is not None:
                    try:
                        backup_path.unlink(missing_ok=True)
                    except Exception:
                        pass
            msg = [f"✗ 套用失敗，已回滾所有變更（atomic）: {e}"]
            msg.extend(rollback_notes)
            return "\n".join(msg)

        # ---- 成功：只刪除本次自己建立的備份（絕不動使用者既有 .orig）----
        for _, backup_path in written:
            if backup_path is not None:
                try:
                    backup_path.unlink()
                except Exception:
                    pass

        # P2 改進：自動驗證流程
        successfully_patched = [p[0] for p in plans]
        if successfully_patched:
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

    # hunk header:行號/行數全部**選填**。`@@` / `@@ -26 +26 @@` /
    # `@@ -26,6 +26,13 @@ void f()` 都合法。實測(2026-08-19)本機小模型
    # 幾乎每次都把行數算錯,舊版 strict 核對讓它陷入「改 header → 再拒絕」
    # 的重試迴圈;現在定位靠 context 內容(_locate_hunks),行號只當多處
    # 匹配時的提示,行數完全不使用 — 資料安全由「splice 長度 = body 實際
    # 行數 + 內容必須匹配」結構性保證,不再需要 header 自我一致。
    _HUNK_HEADER_RE = re.compile(
        r'^@@(?:\s+-(\d+)(?:,(\d+))?(?:\s+\+(\d+)(?:,(\d+))?)?)?\s*(?:@@.*)?$'
    )

    @staticmethod
    def _is_hunk_body_line(line: str) -> bool:
        """這行是否屬於 hunk body(context / 新增 / 移除)。"""
        if line.startswith(' '):
            return True
        if line.startswith('+') and not line.startswith('+++'):
            return True
        if line.startswith('-') and not line.startswith('---'):
            return True
        return line.startswith('\\')  # "\ No newline at end of file"

    def _parse_unified_diff(self, patch: str) -> dict:
        """解析 unified diff 格式(容錯版:行號選填、空白 context 行容錯)"""
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
                match = self._HUNK_HEADER_RE.match(line)
                if match:
                    # 行號/行數是選填 hint:缺省為 None,絕不參與行數核對。
                    old_start = int(match.group(1)) if match.group(1) else None
                    old_count = int(match.group(2)) if match.group(2) else None
                    new_start = int(match.group(3)) if match.group(3) else None
                    new_count = int(match.group(4)) if match.group(4) else None

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

                        # 合法 unified-diff context blank line 是 " "(單一空格),
                        # 但模型常把行尾空白 strip 掉、送出 `""`。`""` 也可能是
                        # split('\n') 對結尾 newline 產生的 sentinel,或 hunk 結束
                        # 後的空白分隔。用 lookahead 區分:跳過連續空行後,若下一
                        # 個非空行仍是 hunk body(' '/'+'/'-'),這些空行就是被
                        # strip 的 context blank line;否則是分隔/EOF sentinel,
                        # 結束 hunk(EOF sentinel 誤算的舊 bug 見
                        # tests/test_patch_parser_edge.py)。
                        if hunk_line == "":
                            j = i
                            while j < len(lines) and lines[j] == "":
                                j += 1
                            if j < len(lines) and self._is_hunk_body_line(lines[j]):
                                for _ in range(i, j):
                                    hunk['lines'].append((' ', ''))
                                i = j
                                continue
                            break

                        # `\ No newline at end of file` — git / unified diff 對沒尾
                        # 換行檔案的標記,跳過不影響 hunk 內容。
                        if hunk_line.startswith('\\'):
                            i += 1
                            continue

                        if hunk_line.startswith(' '):
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

    # ------------------------------------------------------------------
    # hunk 定位:靠 context 內容,不靠行號
    # ------------------------------------------------------------------
    # 舊版拿 old_start 直接當套用位置、context 對不上就拒絕 — 小模型行號
    # 幾乎必錯,導致反覆重試。新版把 hunk 的 (context+移除) 行當搜尋樣板,
    # 在檔案裡找匹配位置:
    #   - 唯一匹配 → 套用(行號錯誤/缺省都無所謂)。
    #   - 多處匹配 → 有行號 hint 挑最近的一處;沒有 hint 或距離打平 →
    #     拒絕並列出候選行號(fail loud,絕不猜位置)。
    #   - 零匹配 → 若 hunk 的「修改後內容」在檔案中唯一存在、與行號 hint 相符,
    #     且 hint 那一帶沒有留著「像修改前」的反證,才視為已套用過(no-op,
    #     重試安全)。多處相同、與 hint 明顯衝突、或 hint 附近仍是 pre-image
    #     一律拒絕——否則會靜默跳過真正該改的地方。
    #     其餘情況拒絕,附最接近位置的期望/實際對照。
    # 逐行比對容忍度沿用舊版:行尾空白差異(rstrip)一律容忍;整檔 strict
    # 掃不到時退一步做縮排不敏感(strip)掃描。

    @staticmethod
    def _lines_match(actual: str, expect: str, *, loose: bool) -> bool:
        if actual.rstrip() == expect.rstrip():
            return True
        return loose and actual.strip() == expect.strip()

    def _find_pattern_positions(self, file_lines: list, pattern: list,
                                *, loose: bool) -> list:
        """回傳 pattern(逐行)在 file_lines 中所有匹配起點(0-based)。"""
        n = len(file_lines) - len(pattern) + 1
        positions = []
        for i in range(max(0, n)):
            if all(
                self._lines_match(file_lines[i + k], expect, loose=loose)
                for k, expect in enumerate(pattern)
            ):
                positions.append(i)
        return positions

    def _best_mismatch_report(self, file_lines: list, pattern: list) -> str:
        """零匹配時的診斷:找匹配行數最多的位置,回報第一個不符的行。"""
        # 掃描成本上限:超大檔 × 長 pattern 就不做逐位置評分(訊息仍完整)。
        if not pattern or len(file_lines) * len(pattern) > 2_000_000:
            return ""
        best_pos, best_score = 0, -1
        for i in range(len(file_lines)):
            score = 0
            for k, expect in enumerate(pattern):
                if i + k >= len(file_lines):
                    break
                if self._lines_match(file_lines[i + k], expect, loose=True):
                    score += 1
            if score > best_score:
                best_pos, best_score = i, score
        for k, expect in enumerate(pattern):
            actual = (
                file_lines[best_pos + k]
                if best_pos + k < len(file_lines) else "<檔案結尾>"
            )
            if not self._lines_match(actual, expect, loose=True):
                return (
                    f"最接近的位置是行 {best_pos + 1}(匹配 {best_score}/"
                    f"{len(pattern)} 行),第一個不符在行 {best_pos + k + 1}:\n"
                    f"  期望: {expect[:80]!r}\n"
                    f"  實際: {actual[:80]!r}"
                )
        return ""

    # 「已套用過」的判定窗:hint 落在檔案範圍內(不是模型隨手亂寫)時,
    # post-image 必須就在提示附近才算同一處。純內容比對會把「檔案別處剛好
    # 有一段相同的修改後內容」誤判成已套用,靜默跳過真正該改的地方。
    ALREADY_APPLIED_HINT_WINDOW = 100

    def _pattern_match_score(self, file_lines: list, pattern: list,
                             pos: int) -> int:
        """pattern 在 pos 的逐行相符數(loose):衡量「這裡還像不像修改前」。"""
        score = 0
        for offset, expect in enumerate(pattern):
            index = pos + offset
            if index >= len(file_lines):
                break
            if self._lines_match(file_lines[index], expect, loose=True):
                score += 1
        return score

    def _resolve_already_applied(self, idx: int, positions: list,
                                 hint: int | None, line_count: int,
                                 file_lines: list, pattern: list,
                                 new_lines: list) -> tuple:
        """零匹配時判斷「修改後內容」是否真的就是這個 hunk 的目標位置。

        Returns:
            (pos, err):err 非 None 時視為定位失敗(fail loud,不當成 no-op)。
        """
        # 超出檔案範圍的行號沒有消歧資格:拿它挑「最近的一處」等於亂猜。
        if hint is not None and not 1 <= hint <= line_count:
            hint = None
        listed = ', '.join(str(p + 1) for p in positions[:5])
        if len(positions) > 1:
            if hint is None:
                return None, (
                    f"區塊 {idx + 1} 的 context 對不上,而「修改後內容」在檔案中"
                    f"出現 {len(positions)} 處(行 {listed}),"
                    "無法判斷是已套用過還是該改別處。"
                    "請增加 context 行數,或在 @@ 標大約行號以消歧。"
                )
            best = min(positions, key=lambda p: abs(p - (hint - 1)))
            ties = [
                p for p in positions
                if abs(p - (hint - 1)) == abs(best - (hint - 1))
            ]
            if len(ties) > 1:
                return None, (
                    f"區塊 {idx + 1} 的 context 對不上,而「修改後內容」在檔案中"
                    f"出現 {len(positions)} 處(行 {listed}),"
                    f"行號提示 {hint} 距離打平無法消歧。請增加 context 行數。"
                )
            chosen = best
        else:
            chosen = positions[0]

        if hint is None:
            # 沒有可用提示(缺省或整組亂寫)= 唯一性是唯一依據;
            # 這條也是「重送同一份 patch 要冪等」的保命符。
            return chosen, None

        distance = abs(chosen - (hint - 1))
        if distance > self.ALREADY_APPLIED_HINT_WINDOW:
            return None, (
                f"區塊 {idx + 1} 的 context 對不上;行 {chosen + 1} 雖有相同的"
                f"「修改後內容」,但距行號提示 {hint} 有 {distance} 行,"
                "不能認定是同一處(很可能是別處剛好內容相同)。"
                "請先 read_file 確認現況,再用該處的實際 context 重送。"
            )

        # 唯一性 + 100 行窗仍擋不住「目標區塊漂移、別處剛好有相同修改後內容」:
        # 那種情況 hint 指的那一帶會留著「像修改前」的內容。找出這個反證。
        # 排除與 post-image 重疊的位置——同一區段的高分是套用成功的正常現象
        # (context 行本來就不變),不是反證。
        span = max(len(pattern), len(new_lines), 1)
        chosen_score = self._pattern_match_score(file_lines, pattern, chosen)
        rival_pos, rival_score = None, 0
        low = max(0, hint - 1 - self.ALREADY_APPLIED_HINT_WINDOW)
        high = min(len(file_lines) - 1, hint - 1 + self.ALREADY_APPLIED_HINT_WINDOW)
        for pos in range(low, high + 1):
            if abs(pos - chosen) < span:
                continue
            score = self._pattern_match_score(file_lines, pattern, pos)
            if score > rival_score or (
                score == rival_score and rival_pos is not None
                and abs(pos - (hint - 1)) < abs(rival_pos - (hint - 1))
            ):
                rival_pos, rival_score = pos, score
        # 過半數才算「像修改前」:單一空行/括號的巧合不足以推翻已套用。
        if rival_pos is not None and rival_score >= chosen_score \
                and rival_score * 2 >= len(pattern):
            return None, (
                f"區塊 {idx + 1} 的 context 對不上。行 {chosen + 1} 雖有相同的"
                f"「修改後內容」,但行號提示 {hint} 附近(行 {rival_pos + 1})還留著"
                f"像「修改前」的內容({rival_score}/{len(pattern)} 行相符),"
                "不能認定已套用過——比較可能是目標區塊已漂移、別處剛好內容相同。"
                "請先 read_file 確認現況,再用該處的實際 context 重送。"
            )
        return chosen, None

    def _locate_hunks(self, file_lines: list, hunks: list) -> tuple:
        """把每個 hunk 定位到檔案位置。

        Returns:
            (plan, err):err 非 None 時 plan 無效。plan 是 per-hunk dict:
            {'index', 'status'('apply'|'already'), 'pos'(0-based),
             'replace_len', 'new_lines', 'relocated'(header 行號缺省或不準)}
        """
        # split('\n') 在「檔尾有換行」時會多出一個空字串 sentinel,它不是一行。
        # 真實行數是純新增定位與 hint 合理性判斷的唯一基準。
        line_count = (
            len(file_lines) - 1
            if file_lines and file_lines[-1] == '' else len(file_lines)
        )
        plan = []
        for idx, hunk in enumerate(hunks):
            pattern = [c for t, c in hunk['lines'] if t in (' ', '-')]
            new_lines = [c for t, c in hunk['lines'] if t in (' ', '+')]
            hint = hunk.get('old_start')  # 1-based 或 None

            if not pattern:
                # 純新增且完全沒 context:只能靠行號提示。
                if hint is None:
                    return None, (
                        f"區塊 {idx + 1} 是純新增且沒有 context 行,無法定位。"
                        "請在修改行前後帶 2-3 行 context(建議),"
                        "或在 @@ 提供行號提示。"
                    )
                # unified diff 慣例:old_count == 0 表示插在第 old_start 行之後。
                insert_at = hint if hunk.get('old_count') == 0 else hint - 1
                # 越界行號一律 fail loud。舊版把它 clamp 到檔尾,
                # `@@ -999,0 +1000,1 @@` 會「成功」寫到 EOF 之後(還會多一個
                # 空行、吃掉尾端換行),模型完全看不出定位錯了。
                if not 0 <= insert_at <= line_count:
                    return None, (
                        f"區塊 {idx + 1} 是純新增且沒有 context 行,只能靠行號定位,"
                        f"但 @@ 的行號提示 {hint} 超出檔案範圍"
                        f"(檔案共 {line_count} 行)。"
                        "請改帶 2-3 行 context(定位就不必依賴行號),"
                        f"或把行號改成 0..{line_count} 之間。"
                    )
                pos = insert_at
                plan.append({
                    'index': idx, 'status': 'apply', 'pos': pos,
                    'replace_len': 0, 'new_lines': new_lines, 'relocated': False,
                })
                continue

            positions = self._find_pattern_positions(file_lines, pattern, loose=False)
            if not positions:
                positions = self._find_pattern_positions(file_lines, pattern, loose=True)

            if len(positions) == 1:
                pos = positions[0]
            elif len(positions) > 1:
                if hint is not None:
                    best = min(positions, key=lambda p: abs(p - (hint - 1)))
                    ties = [
                        p for p in positions
                        if abs(p - (hint - 1)) == abs(best - (hint - 1))
                    ]
                    if len(ties) > 1:
                        return None, (
                            f"區塊 {idx + 1} 的 context 在檔案中出現 "
                            f"{len(positions)} 處(行 "
                            f"{', '.join(str(p + 1) for p in positions[:5])}),"
                            f"行號提示 {hint} 距離打平無法消歧。"
                            "請增加 context 行數。"
                        )
                    pos = best
                else:
                    return None, (
                        f"區塊 {idx + 1} 的 context 在檔案中出現 "
                        f"{len(positions)} 處(行 "
                        f"{', '.join(str(p + 1) for p in positions[:5])})。"
                        "請增加 context 行數,或在 @@ 標大約行號以消歧。"
                    )
            else:
                # 零匹配:先檢查是否已套用過(修改後內容已在檔案裡)。
                # 只對「有新增行」的 hunk 做,純刪除的 context-only 樣板太弱,
                # 誤判成已套用會靜默漏刪 — 那種情況走 fail-loud。
                if hunk['add'] and new_lines:
                    done = self._find_pattern_positions(
                        file_lines, new_lines, loose=False
                    ) or self._find_pattern_positions(
                        file_lines, new_lines, loose=True
                    )
                    if done:
                        done_pos, done_err = self._resolve_already_applied(
                            idx, done, hint, line_count,
                            file_lines, pattern, new_lines,
                        )
                        if done_err:
                            return None, done_err
                        plan.append({
                            'index': idx, 'status': 'already',
                            'pos': done_pos, 'replace_len': 0,
                            'new_lines': [], 'relocated': False,
                        })
                        continue
                detail = self._best_mismatch_report(file_lines, pattern)
                return None, (
                    f"區塊 {idx + 1} context 不匹配(在檔案中找不到對應內容)。"
                    + (f"\n{detail}" if detail else "")
                    + "\n提示: context 行必須與檔案現況一致;"
                    "先 read_file 確認現況再重送。行號不需要準確,定位靠 context。"
                )

            plan.append({
                'index': idx, 'status': 'apply', 'pos': pos,
                'replace_len': len(pattern), 'new_lines': new_lines,
                'relocated': hint is None or (hint - 1) != pos,
            })

        # 重疊檢查:兩個 hunk 套到同一段行 → 順序/語意不明,拒絕。
        applied = sorted(
            (e for e in plan if e['status'] == 'apply'),
            key=lambda e: (e['pos'], e['index']),
        )
        for prev, nxt in zip(applied, applied[1:]):
            if prev['pos'] + prev['replace_len'] > nxt['pos']:
                return None, (
                    f"區塊 {prev['index'] + 1} 與區塊 {nxt['index'] + 1} "
                    f"定位後重疊(行 {nxt['pos'] + 1} 附近)。"
                    "請合併成一個 hunk,或增加 context 讓兩者分開。"
                )
        return plan, None

    def _compute_new_file_content(self, hunks: list) -> str:
        """從 hunks 組出新建檔案的完整內容（context + 新增行）。"""
        new_lines = []
        for hunk in hunks:
            for line_type, content in hunk['lines']:
                if line_type in (' ', '+'):
                    new_lines.append(content)
        return '\n'.join(new_lines) + '\n'

    def _compute_patched_content(self, original: str, plan: list) -> str:
        """把 _locate_hunks 產出的 plan 套到既有內容，回傳新內容字串（不寫檔）。

        splice 位置來自 content 定位(不是 header 行號),splice 長度 =
        pattern 實際行數 —— 結構上保證永遠不會刪到沒列在 patch body 裡的行,
        也因此 header 行數宣稱錯誤完全無害(直接忽略)。
        由後往前套,前面的 splice 不會位移後面的定位。
        """
        lines = original.split('\n')
        pending = sorted(
            (e for e in plan if e['status'] == 'apply'),
            key=lambda e: (e['pos'], e['index']),
            reverse=True,
        )
        for entry in pending:
            lines[entry['pos']:entry['pos'] + entry['replace_len']] = entry['new_lines']
        return '\n'.join(lines)

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
        """對檔案執行 lint/format 工具。

        fix=True  → 走 LINT_COMMANDS[ext]['fix']（會就地改檔）
        fix=False → 走 LINT_COMMANDS[ext]['check']（只回報、不改檔）；
                    若該副檔名沒提供 check 命令，回錯誤而不是回頭跑 fix。

        AI_CODE_PATCH=0 完全唯讀模式: fix=True 會改檔,必須一起擋下;
        check-only(fix=False) 仍允許,只回報不寫檔。
        """
        if fix and not config.PATCH_ENABLED:
            return (
                "錯誤: run_lint(fix=True) 已停用 (AI_CODE_PATCH=0,唯讀模式)。"
                "若只要檢查,改用 fix=False 跑 check-only。"
            )

        target = self._safe_path(path)
        if not target or not target.exists():
            return f"錯誤: 檔案不存在 '{path}'"
        if not target.is_file():
            return f"錯誤: '{path}' 不是檔案"

        ext = target.suffix.lower()
        lint_spec = LINT_COMMANDS.get(ext)
        if not lint_spec:
            return f"錯誤: 不支援的檔案類型 '{ext}'（支援: {', '.join(LINT_COMMANDS.keys())}）"

        mode = 'fix' if fix else 'check'
        lint_cmds = lint_spec.get(mode)
        if not lint_cmds:
            return (
                f"錯誤: '{ext}' 沒有 {mode} 模式的命令設定。"
                f"要 check-only 但目前工具鏈不支援，請改用 fix=True，或在 config.LINT_COMMANDS 補上 '{mode}' key。"
            )

        results = []
        rel_path = str(target.relative_to(self.root))

        for cmd_template in lint_cmds:
            cmd_parts = shlex.split(cmd_template)
            cmd_parts.append(rel_path)

            try:
                print(f"   [LINT] 執行: {' '.join(cmd_parts)}", file=sys.stderr)
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
