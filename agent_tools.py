#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - Agent 工具定義與執行器
"""

import os
import process_env
import re
import sys
import json
import codecs
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional

import config
import command_allowlist
import container_runner
import patch_engine
from runtime_dependencies import DependencyError, DEPENDENCY_ERROR_PREFIX
from media import BINARY_EXTENSIONS, ELF_EXTENSIONS
from config import (
    IMAGE_EXTENSIONS,
    MAX_FILE_READ_CHARS, MAX_GREP_RESULTS,
    MAX_GREP_LINE_CHARS,
    MAX_GREP_OUTPUT_CHARS, MAX_LIST_DEPTH,
    IGNORED_PATTERNS, GREP_DEFAULT_EXTENSIONS, ALLOWED_DOT_DIRS,
    RUN_COMMAND_TIMEOUT, RUN_COMMAND_MAX_OUTPUT,
    RUN_COMMAND_TIMEOUT_MIN, RUN_COMMAND_TIMEOUT_MAX,
    RUN_COMMAND_TAIL_RATIO, RUN_COMMAND_ERROR_PATTERNS,
    PATCH_MAX_FILES, PATCH_MAX_LINES_PER_FILE,
    LINT_COMMANDS,
)
from utils import should_ignore_dir, should_ignore_file
import patch_verify


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
        "description": (
            "執行白名單命令。預設白名單=測試/靜態命令(pytest, ctest, npm test, cargo test, "
            "go test; mypy, tsc, ruff, black, isort, eslint, clang-format);"
            "build 命令(make/cmake/ninja/meson/bazel build)只在 client.json 的 "
            "build_commands 打開時加入;git 不在白名單(用 git_status / git_diff)。"
            "client.json 的 extra_allowed_commands 可另授權 PATH 上的裸命令名稱。"
            "/allow add <絕對目錄> 授權 extra_allowed_command_dirs；後續命令立即重驗，"
            "以裸工具名呼叫已驗證絕對路徑，不改 PATH；目錄工具在容器模式拒絕。"
            "授權不改既有人工核准。"
            "timeout 1..600 秒(server 端上限;client 可能更早截止)。"
            "apply_patch 不會自動呼叫這裡:lint / test 要另行呼叫 run_lint(fix=False) / run_command。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要執行的命令，如 'pytest test_xxx.py -v' 或 'go test ./...'"},
                "timeout": {
                    "type": "integer",
                    "minimum": RUN_COMMAND_TIMEOUT_MIN,
                    "maximum": RUN_COMMAND_TIMEOUT_MAX,
                    "default": RUN_COMMAND_TIMEOUT,
                    "description": (
                        f"超時秒數,{RUN_COMMAND_TIMEOUT_MIN}..{RUN_COMMAND_TIMEOUT_MAX},"
                        f"預設 {RUN_COMMAND_TIMEOUT}(server 端上限;client 可能更早截止)"
                    ),
                },
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
        "description": (
            "套用程式碼修改,兩種格式擇一:SEARCH/REPLACE(建議)或 unified diff。"
            "參數已是字串,不要包 Markdown fence。修改會直接寫入檔案;"
            "最多 5 個檔案、單檔 200 行(udiff 算 added+removed;S/R 算 SEARCH+REPLACE 行數)。"
            "S/R 的 SEARCH 逐行 exact 比對(只容忍行尾空白),多處匹配或縮排不同都會拒絕,不會代套。"
            "套用後只做唯讀 syntax check(advisory,不回滾);lint / test 請另外呼叫 run_lint(fix=False) / run_command。"
            "dry_run=true 時只做 preflight,逐檔回報 format / 檔案清單 / blocks / payload budget / "
            "locations(定位行) / new_file(是否新建),全部通過才顯示 would apply。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": (
                        "SEARCH/REPLACE 格式:第一行是 repo 相對路徑,接著三個 marker 各自獨佔一行。例如:\n"
                        "src/led.c\n<<<<<<< SEARCH\n    gpio_write(LED_PIN, 1);\n=======\n"
                        "    gpio_toggle(LED_PIN);\n>>>>>>> REPLACE\n"
                        "空 SEARCH = 建新檔(目標不存在、該檔恰一個區塊、REPLACE 非空)。"
                        "unified diff 格式:--- a/file / +++ b/file / @@(行號選填,靠 context 定位),"
                        "修改行前後帶 2-3 行 context。"
                    )
                },
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "若為 true,只做 preflight 並逐檔回報 format、檔案清單、blocks、payload budget、"
                        "locations(定位行)、new_file(是否新建),全部通過才顯示 would apply;"
                        "不寫檔、零副作用（預設 false）"
                    )
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


# run_command 以 shell=False 執行；拒絕提示與危險字元檢查共用同一份清單。
_SHELL_PATTERNS = ('$(', '`', '&&', '||', ';', '|', '>', '<')
_NO_SHELL_HINT = (
    "run_command 不經 shell：不支援 " + "、".join(_SHELL_PATTERNS)
    + " 等語法；請用工具自己的參數限制輸出。"
)
MAX_GRANTED_LISTING_CHARS = 2000


def _command_rejection(cmd_parts: list, extra_names: list, directory_commands: dict) -> str:
    """拒絕訊息只給修正方向：帶路徑的授權工具指回裸名稱，但不以路徑或 basename 放行。

    實際 session:/allow add 之後模型仍送 `MetaWare/arc/bin/llvm-objdump ... | head`,
    舊訊息只列前 8 個內建前綴，模型便回報「白名單仍不允許」。
    """
    builtins = list(config.ALLOWED_COMMANDS)
    granted = sorted({*extra_names, *directory_commands})
    lines = ["錯誤: 不允許的命令。"]
    base = cmd_parts[0].rsplit("/", 1)[-1]
    known = {*granted, *(entry.split()[0] for entry in builtins if entry.split())}
    if "/" in cmd_parts[0] and base in known:
        lines.append(f"命令不可帶路徑：請改用裸名稱 {base} 呼叫（server 只執行已驗證的授權路徑）。")
    if any(pattern in part for part in cmd_parts for pattern in _SHELL_PATTERNS):
        lines.append(_NO_SHELL_HINT)
    if granted:
        shown, used = [], 0
        for name in granted:
            if used + len(name) + 2 > MAX_GRANTED_LISTING_CHARS:
                break
            shown.append(name)
            used += len(name) + 2
        hidden = len(granted) - len(shown)
        lines.append(
            "已授權工具（以裸名稱呼叫）: " + ", ".join(shown)
            + (f" …（共 {len(granted)} 個，{hidden} 個未列出）" if hidden else "")
        )
    lines.append(
        "允許的命令前綴: " + ", ".join(builtins[:8]) + ("..." if len(builtins) > 8 else "")
    )
    lines.append(
        "自訂工具可由使用者以 /allow add <絕對目錄> 授權；"
        "既有 client.json 的 extra_allowed_commands 裸名稱仍相容。"
    )
    return "\n".join(lines)


class ToolExecutor:
    def __init__(
        self, root: str, *,
        command_settings_loader: Callable[[], tuple[list[str], list[str]]] | None = None,
    ):
        self.root = Path(root).resolve()
        self._command_settings_loader = command_settings_loader

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
        return shutil.which("rg") is not None

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
                result = process_env.run(
                    cmd,
                    cwd=str(self.root),
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                return result.returncode, result.stdout, result.stderr
            except OSError as exc:
                return 2, "", str(exc)
            except process_env.TimeoutExpired:
                return 2, "", "rg timeout"

        rc, stdout, stderr = _run(False)
        if rc == 1 and not stdout:
            rc, stdout, stderr = _run(True)

        if rc not in (0, 1):
            return (f"{DEPENDENCY_ERROR_PREFIX}ripgrep (rg) 執行失敗: "
                    f"{stderr.strip() or 'unknown'}；請安裝或修復 ripgrep。")

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

        # 危險或不合法 regex 仍改字面比對；搜尋實作只有 ripgrep。
        use_literal = self._is_redos_risk(pattern)
        if not use_literal:
            try:
                re.compile(pattern)
            except re.error:
                use_literal = True
        if include is None:
            include = GREP_DEFAULT_EXTENSIONS
        include_patterns = [p.strip() for p in include.split(',')]
        if not self._rg_available():
            return f"{DEPENDENCY_ERROR_PREFIX}需要 ripgrep (rg)；請安裝 ripgrep 並確認 rg 在 PATH。"
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

    def file_info(self, path: str) -> str:
        target = self._safe_path(path)
        if not target or not target.exists():
            return f"錯誤: 不存在 '{path}'"

        if target.is_file():
            try:
                size = target.stat().st_size
                ext = target.suffix.lower()
                encoding = None
                if ext not in _NONTEXT_EXTENSIONS and ext != ".pdf":
                    with target.open("rb") as source:
                        encoding, _ = _sniff_text_encoding(source.read(8192))
                if encoding is None:
                    guidance = (
                        "請用 analyze_file 解析。" if ext in _ANALYZABLE_EXTENSIONS
                        else "請先轉成支援的文字、PDF 或圖片格式再分析。"
                    )
                    return f"{path}: 檔案, 二進位/非文字, {size:,} bytes；{guidance}"

                # Match read_file's encoding decision. Binary bytes must never
                # become fictitious text counts that misroute the next call.
                lines, chars = 1, 0
                with target.open("r", encoding=encoding, errors="replace") as source:
                    for chunk in iter(lambda: source.read(65536), ""):
                        lines += chunk.count('\n')
                        chars += len(chunk)
            except Exception as exc:
                return f"錯誤: 無法讀取 '{path}' 的檔案資訊: {exc}"

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
        # MCP 注入的 loader 每次重讀同一份設定，只取兩個 allow 欄位。
        # direct executor 則仍只看 config；不偷讀 HOME，也不保存舊授權 mapping。
        try:
            if self._command_settings_loader is None:
                extra_names, directories = (
                    config.EXTRA_ALLOWED_COMMANDS, config.EXTRA_ALLOWED_COMMAND_DIRS,
                )
            else:
                extra_names, directories = self._command_settings_loader()
            extra_names = command_allowlist.validate_extra_allowed_commands(extra_names)
            directories = command_allowlist.validate_extra_allowed_command_dirs(directories)
            inspection = command_allowlist.inspect_command_directories(
                directories, extra_commands=extra_names,
            )
        except Exception as exc:
            return False, f"錯誤: 無法讀取或驗證 run_command 授權: {exc}", []
        if inspection.errors:
            errors = "；".join(f"{path}: {reason}" for path, reason in inspection.errors.items())
            return False, f"錯誤: run_command 目錄授權解析失敗: {errors}", []

        command = command.strip()

        try:
            cmd_parts = shlex.split(command)
        except ValueError as e:
            return False, f"錯誤: 命令解析失敗 - {e}", []

        if not cmd_parts:
            return False, "錯誤: 空命令", []

        # 動態授權不可持有 from-import 快照；名稱與目錄 mapping 都來自本次驗證。
        # 同一套逐 token 比對：單 token 項只放行完全相同的 argv[0]。
        allowed_commands = [*config.ALLOWED_COMMANDS, *extra_names, *inspection.commands]
        is_allowed = False
        for allowed in allowed_commands:
            allowed_parts = shlex.split(allowed)
            if cmd_parts[:len(allowed_parts)] == allowed_parts:
                is_allowed = True
                break

        if not is_allowed:
            return False, _command_rejection(cmd_parts, extra_names, inspection.commands), []

        # 額外安全檢查：危險字元
        for part in cmd_parts:
            for pattern in _SHELL_PATTERNS:
                if pattern in part:
                    return False, f"錯誤: 參數包含不允許的字元 '{pattern}'\n{_NO_SHELL_HINT}", []

        # Path containment：白名單命令的參數不能逃出 AICODE_ROOT。
        # 阻擋 `pytest /tmp/x.py`、`make -C /tmp`、`cmake --build /abs/build` 之類。
        ok, why = self._check_path_containment(cmd_parts)
        if not ok:
            return False, f"錯誤: {why}（命令參數必須指向 AICODE_ROOT 內的路徑）", []

        # 只替換通過所有裸 argv 檢查的 argv[0]，其餘參數仍限 sandbox。
        # 不改 PATH、不以 basename 重試，也不改成 /proc/self/fd 的執行路徑。
        if cmd_parts[0] in inspection.commands:
            cmd_parts[0] = inspection.commands[cmd_parts[0]]
        return True, "", cmd_parts

    def run_command(self, command: str, timeout: int = RUN_COMMAND_TIMEOUT) -> str:
        """執行白名單內的測試 / 靜態分析命令(build 命令需 opt-in)。

        白名單(內建前綴、legacy extra_allowed_commands 與授權目錄工具):
          - 測試 / 靜態命令是預設白名單(pytest / ctest / npm test / cargo test / go test;
            mypy / tsc / ruff / black / isort / eslint / clang-format 等)。
          - build 命令(make / cmake / ninja / meson / bazel build)只在
            client.json 的 build_commands 打開時加入(server 收 --enable-build-commands)。
          - git 不在白名單(用 git_status / git_diff)。
          - client.json 的 extra_allowed_commands 額外放行 PATH 上的裸命令名稱。
          - /allow add <絕對目錄> 的 extra_allowed_command_dirs 每次重驗，
            同一 MCP 後續命令立即生效；以裸工具名呼叫，執行已驗證絕對路徑。
            目錄授權不改人工核准，host 目錄工具在容器模式明確拒絕。
        apply_patch 不再自動呼叫這裡:lint / test 由模型另行、顯式呼叫,讓各自的核准閘生效。

        Args:
            command: 完整命令列(shell=False,shlex 切詞後逐 token 驗證白名單、危險字元、路徑範圍)。
            timeout: 秒,1..600(RUN_COMMAND_TIMEOUT_MIN..MAX),預設 60。這是 server 端上限;
                     MCP client 可能更早截止,不保證 600 秒必在 client timeout 內。
                     非整數(含 bool)或超出範圍在 spawn 之前拒絕,容器模式同樣受檢。
        """
        if not config.RUN_COMMAND_ENABLED:
            return "錯誤: run_command 功能已停用（這是 readonly session;互動 session 才會啟用）"

        # timeout 三層契約的 executor 層:int 且 1..600(bool 不算),在 spawn 之前拒絕;
        # 回顯輸入時截到 80 字元,不把任意長字串整段回送。
        if type(timeout) is not int or not (
            RUN_COMMAND_TIMEOUT_MIN <= timeout <= RUN_COMMAND_TIMEOUT_MAX
        ):
            shown = repr(timeout)
            if len(shown) > 80:
                shown = shown[:80] + "…(截斷)"
            return (
                f"錯誤: timeout 必須是 {RUN_COMMAND_TIMEOUT_MIN}..{RUN_COMMAND_TIMEOUT_MAX} 的整數,"
                f"收到 {type(timeout).__name__}: {shown}"
            )

        # 統一驗證（容器/非容器模式都要過白名單）
        is_valid, error_msg, cmd_parts = self._validate_command(command)
        if not is_valid:
            return error_msg

        # 容器化執行模式
        if container_runner.CONTAINER_ENABLED:
            if os.path.isabs(cmd_parts[0]):
                return (
                    "錯誤: 目錄授權工具無法在容器模式執行；host 工具目錄未掛入容器，"
                    "不會改到 host 執行。"
                )
            return self._run_command_in_container(command, timeout)

        try:
            print(f"   [RUN] 執行: {command}", file=sys.stderr)
            result = process_env.run(
                cmd_parts,
                shell=False,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
                overrides={'PYTHONIOENCODING': 'utf-8'}
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

        except process_env.TimeoutExpired:
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
    # ------------------------------------------------------------------
    # per-file plan(兩格式共用的形狀:_locate_hunks 的 plan entry)
    # ------------------------------------------------------------------
    def _plan_search_replace(self, rel: str, blocks: list, snapshot, budget_used: int,
                             parent_identity) -> tuple:
        """S/R 的 per-file preflight。回 (FilePlan | None, err | None, MismatchRecord | None)。"""
        empty = [block for block in blocks if not block.search]
        if empty:
            if snapshot is not None:
                return None, "空 SEARCH 只能建立不存在的新檔（檔案已存在）", None
            if len(blocks) != 1:
                return None, f"空 SEARCH 必須是該檔唯一的區塊（共 {len(blocks)} 個區塊）", None
            if not blocks[0].replace:
                return None, "空 SEARCH 且空 REPLACE", None
            plan = patch_engine.FilePlan(
                rel, is_new=True, new_lines=list(blocks[0].replace), blocks=1, budget=budget_used,
            )
            return plan, None, None
        if snapshot is None:
            return None, "檔案不存在（非空 SEARCH 無法套用到不存在的檔案;要建新檔請用空 SEARCH）", None
        entries, err, record = patch_engine.locate_sr_blocks(
            patch_engine.body_lines(snapshot), blocks, path=rel,
        )
        if err is not None:
            return None, err, record
        plan = patch_engine.FilePlan(
            rel, snapshot=snapshot, plan=entries, blocks=len(blocks), budget=budget_used,
            parent_identity=parent_identity,
        )
        return plan, None, None

    def _plan_unified_diff(self, rel: str, hunks: list, snapshot, budget_used: int,
                           parent_identity) -> tuple:
        """udiff 的 per-file preflight(定位邏輯沿用 _locate_hunks,行為不變)。"""
        if snapshot is None:
            plan = patch_engine.FilePlan(
                rel, is_new=True,
                new_bytes=self._compute_new_file_content(hunks).encode("utf-8"),
                blocks=len(hunks), budget=budget_used,
            )
            return plan, None, None
        records: list = []
        entries, hunk_err = self._locate_hunks(
            snapshot.text.split('\n'), hunks, path=rel, records=records,
        )
        if hunk_err:
            return None, hunk_err, (records[0] if records else None)
        plan = patch_engine.FilePlan(
            rel, snapshot=snapshot, plan=entries, blocks=len(hunks), budget=budget_used,
            parent_identity=parent_identity,
        )
        return plan, None, None

    @staticmethod
    def _render_preflight_errors(errors: list) -> list:
        """✗ 行 + 其 mismatch 預覽;預覽由單一 renderer 對整次結果套總額(E)。"""
        records = [record for _, record in errors if record is not None]
        rendered = patch_engine.render_mismatch_records(records)
        out = []
        idx = 0
        for text, record in errors:
            out.append(text)
            if record is not None:
                out.extend(rendered[idx])
                idx += 1
        return out

    @staticmethod
    def _plan_locations(file_plan) -> str:
        if file_plan.is_new:
            return "new file"
        pending = [e for e in file_plan.plan if e['status'] == 'apply']
        return "; ".join(
            f"行 {e['pos'] + 1}-{e['pos'] + max(e['replace_len'], 1)}" for e in pending
        ) or "已全部套用過"

    def apply_patch(self, patch: str, dry_run: bool = False) -> str:
        """套用 patch:unified diff 或 canonical SEARCH/REPLACE,兩格式共用同一條管線。

        sandbox → 上限 → byte-level preflight(UTF-8 strict / newline / symlink /
        定位)→ dry_run 回報或全量拒絕 → journaled 原子寫入(失敗 best-effort
        rollback)→ 同 process 的 syntax 驗證(由 _verify_patched_files 決定)。
        """
        if not config.PATCH_ENABLED:
            return "✗ apply_patch 已停用（這是 readonly session;互動 session 才會啟用）"
        max_files = PATCH_MAX_FILES
        max_lines = PATCH_MAX_LINES_PER_FILE
        safe = patch_engine.safe_display

        try:
            text = patch_engine.normalize_patch_text(patch)
            fmt = patch_engine.detect_format(text)
        except patch_engine.PatchFormatError as e:
            return f"✗ patch 格式錯誤: {e}"

        # 每個 parsed file 一筆 preflight record(成功或失敗都保留固定欄位,dry_run 逐筆輸出)
        records = []     # dict(shown, blocks, budget, new_file, locations, error=(✗ 行, MismatchRecord|None)|None)
        specs = []       # [(raw_path, payload)]
        if fmt == "search_replace":
            try:
                blocks = patch_engine.parse_search_replace(text)
            except patch_engine.PatchFormatError as e:
                return f"✗ patch 格式錯誤: {e}"
            grouped = {}
            invalid = {}
            for block in blocks:
                try:
                    patch_engine.ensure_utf8_encodable(
                        block.search + block.replace, what=f"區塊 {block.index}",
                    )
                    rel = patch_engine.validate_sr_path(block.path)
                except patch_engine.PatchFormatError as e:
                    rec = invalid.get(block.path)
                    if rec is None:
                        rec = {
                            "shown": safe(block.path), "blocks": 0, "budget": 0, "new_file": "?",
                            "locations": "preflight failed",
                            "error": (f"✗ {safe(block.path)}: {e}", None),
                        }
                        invalid[block.path] = rec
                        records.append(rec)
                    rec["blocks"] += 1
                    rec["budget"] += len(block.search) + len(block.replace)
                    continue
                grouped.setdefault(rel, []).append(block)
            specs = list(grouped.items())
            file_count = len(specs) + len(invalid)
        else:
            try:
                changes = self._parse_unified_diff(text)
            except ValueError as e:
                return f"✗ patch 解析失敗: {e}"
            if not changes:
                return "✗ 無法從 patch 中解析出任何修改"
            specs = list(changes.items())
            file_count = len(specs)

        if file_count > max_files:
            return f"✗ 修改檔案數量超過限制（{file_count} > {max_files}）"

        # ============================================================
        # Phase 1: 全量 preflight(零寫入、零 mkdir、零 temp)
        #   path 規則 → sandbox → 同檔多寫法 → 上限 → byte snapshot → 定位
        #   任何預期的 OS / resolve 失敗都轉成 per-file ✗,不逸出 MCP。
        # ============================================================
        plans = []
        identity = {}
        try:
            ops = patch_engine.PathOps(self.root)
        except DependencyError as e:
            return f"{DEPENDENCY_ERROR_PREFIX}✗ {safe(str(e))}"
        except OSError as e:
            return f"✗ 無法開啟 sandbox root: {safe(str(e))}"
        try:
            for raw_path, payload in specs:
                if fmt == "search_replace":
                    rel = raw_path
                    shown = rel
                    blocks_n = len(payload)
                    budget_used = sum(len(b.search) + len(b.replace) for b in payload)
                    budget_label = "S/R payload budget 超過限制"
                else:
                    shown = str(raw_path)
                    blocks_n = len(payload)
                    budget_used = sum(len(h['add']) + len(h['remove']) for h in payload)
                    budget_label = "修改行數超過限制"
                rec = {
                    "shown": safe(shown), "blocks": blocks_n, "budget": budget_used,
                    "new_file": "?", "locations": "preflight failed", "error": None,
                }
                records.append(rec)
                try:
                    if fmt == "unified_diff":
                        try:
                            rel = patch_engine.clean_udiff_path(raw_path)
                            patch_engine.ensure_utf8_encodable(
                                (content for hunk in payload for _, content in hunk['lines']),
                                what="hunk 內容",
                            )
                        except patch_engine.PatchFormatError as e:
                            reason = "路徑不在專案內或無效" if "路徑" in str(e) else str(e)
                            rec["error"] = (f"✗ {safe(shown)}: {reason}", None)
                            continue

                    target = self._safe_path(rel)
                    if target is None:
                        reason = (
                            patch_engine.SYMLINK_REFUSED
                            if patch_engine.has_symlink_component(self.root, rel)
                            else "路徑不在專案內或無效"
                        )
                        rec["error"] = (f"✗ {safe(shown)}: {reason}", None)
                        continue
                    key = str(target)
                    if key in identity:
                        rec["error"] = (
                            f"✗ 同一檔案以多個 path 寫法出現: {safe(identity[key])}, {safe(shown)}", None,
                        )
                        continue
                    identity[key] = shown

                    if budget_used > max_lines:
                        rec["error"] = (f"✗ {safe(rel)}: {budget_label}（{budget_used} > {max_lines}）", None)
                        continue

                    try:
                        snapshot, parent_identity = ops.read_snapshot(rel)
                    except patch_engine.SnapshotError as e:
                        rec["error"] = (f"✗ {safe(rel)}: {e}", None)
                        continue

                    if fmt == "search_replace":
                        file_plan, err, record = self._plan_search_replace(
                            rel, payload, snapshot, budget_used, parent_identity,
                        )
                    else:
                        file_plan, err, record = self._plan_unified_diff(
                            rel, payload, snapshot, budget_used, parent_identity,
                        )
                    if err is not None:
                        rec["new_file"] = "no" if snapshot is not None else "yes"
                        rec["error"] = (f"✗ {safe(rel)}: {err}", record)
                        continue
                    rec["new_file"] = "yes" if file_plan.is_new else "no"
                    rec["locations"] = self._plan_locations(file_plan)
                    rec["plan"] = file_plan
                    plans.append(file_plan)
                except (OSError, RuntimeError, ValueError) as e:
                    rec["error"] = (
                        f"✗ {safe(shown)}: preflight 失敗（{type(e).__name__}: {safe(str(e))}）", None,
                    )
        finally:
            preflight_notes = list(ops.notes)
            ops.close()

        errors = [rec["error"] for rec in records if rec["error"] is not None]

        # ---- dry_run: 每個 parsed file 固定欄位一行(成功或失敗),再附錯誤;零副作用 ----
        if dry_run:
            lines = [f"[DRY RUN] 格式: {fmt}"]
            error_records = [rec["error"][1] for rec in records if rec["error"] is not None]
            rendered = patch_engine.render_mismatch_records(
                [record for record in error_records if record is not None]
            )
            rendered_idx = 0
            for rec in records:
                lines.append(
                    f"[DRY RUN] {rec['shown']}: format={fmt} blocks={rec['blocks']} "
                    f"budget={rec['budget']}/{max_lines} new_file={rec['new_file']} "
                    f"locations={rec['locations']}"
                )
                file_plan = rec.get("plan")
                if file_plan is not None and not file_plan.is_new:
                    for e in file_plan.plan:
                        if e['status'] == 'already':
                            lines.append(
                                f"  區塊 {e['index'] + 1}: 已套用過(修改後內容見行 "
                                f"{e['pos'] + 1}),將跳過"
                            )
                        elif e.get('relocated'):
                            end = e['pos'] + max(e['replace_len'], 1)
                            lines.append(f"  區塊 {e['index'] + 1}: 行 {e['pos'] + 1}-{end}（依 context 定位）")
                if rec["error"] is not None:
                    text_line, record = rec["error"]
                    lines.append(text_line)
                    if record is not None:
                        lines.extend(rendered[rendered_idx])
                        rendered_idx += 1
            if errors:
                lines.append(
                    "⚠ [DRY RUN] 上述 ✗ 檔案未通過 preflight；實際套用時整份 patch 會被拒絕,"
                    "不會寫入任何檔案（全量 preflight）"
                )
            else:
                lines.append(f"[DRY RUN] would apply: {len(plans)} 個檔案")
            return "\n".join(preflight_notes + lines)

        # ---- 全量 preflight:任一檔失敗 → 整份拒絕、零寫入 ----
        if errors:
            out = preflight_notes + self._render_preflight_errors(errors)
            out.append("⚠ 因有檔案未通過 preflight,整份 patch 已被拒絕,未寫入任何檔案（全量 preflight）")
            return "\n".join(out)

        # ============================================================
        # Phase 2: journaled 寫入(單檔原子;多檔 best-effort rollback)
        #   父目錄只在全量 preflight 通過後建立並逐層記錄;每次 mkdir / 讀 /
        #   publish / rollback 都從 root 重走 lexical path 並比對 parent 身分,
        #   寫入前重驗 preimage。
        # ============================================================
        tag = " [search_replace]" if fmt == "search_replace" else ""
        try:
            ops = patch_engine.PathOps(self.root)
        except DependencyError as e:
            return f"{DEPENDENCY_ERROR_PREFIX}✗ {safe(str(e))}"
        except OSError as e:
            return f"✗ 無法開啟 sandbox root: {safe(str(e))}"
        journal = patch_engine.WriteJournal(ops)
        results = []
        written = []    # [(rel,)]:實際寫入 / 新建的檔(no-op 不算),供 verifier 使用
        try:
            for fp in plans:
                if fp.is_new:
                    fp.parent_identity = ops.ensure_parents(fp.rel, journal)
            for fp in plans:
                if fp.is_new:
                    data = (
                        fp.new_bytes if fp.new_bytes is not None
                        else patch_engine.serialize_new_file(fp.new_lines)
                    )
                    ops.write_new(fp.rel, fp.parent_identity, data, journal)
                    results.append(f"✓ {fp.rel}: 新建檔案{tag}")
                    written.append((fp.rel,))
                    continue
                pending = [e for e in fp.plan if e['status'] == 'apply']
                already = [e for e in fp.plan if e['status'] == 'already']
                if not pending:
                    # 冪等:所有區塊都已套用過 → 不碰檔案、不進 journal、不進 verifier。
                    where = ", ".join(f"區塊{e['index'] + 1}→行 {e['pos'] + 1}" for e in already)
                    results.append(
                        f"✓ {fp.rel}: 所有區塊({len(already)})都已套用過,檔案未變更（{where}）"
                    )
                    continue
                content = self._compute_patched_content(fp.snapshot.text, fp.plan)
                data = patch_engine.render_bytes(fp.snapshot, content)
                ops.write_existing(fp.rel, fp.parent_identity, fp.snapshot, data, journal)
                msg = f"✓ {fp.rel}: 已修改 {len(pending)} 個區塊{tag}"
                relocated = [e for e in pending if e.get('relocated')]
                if relocated:
                    msg += (
                        "（"
                        + ", ".join(
                            f"區塊{e['index'] + 1}依 context 定位於行 {e['pos'] + 1}" for e in relocated
                        )
                        + "）"
                    )
                if already:
                    msg += (
                        "（另 "
                        + ", ".join(f"區塊{e['index'] + 1}已套用過於行 {e['pos'] + 1}" for e in already)
                        + ",跳過）"
                    )
                results.append(msg)
                written.append((fp.rel,))
        except Exception as e:
            rollback_notes = journal.rollback()
            notes = list(dict.fromkeys(preflight_notes + ops.notes))
            ops.close()
            msg = notes + [
                "✗ 套用失敗；已執行 best-effort rollback（回滾本次已寫入的檔案;"
                f"全量 preflight＋best-effort rollback,不是跨檔交易）: {safe(str(e), 400)}"
            ]
            msg.extend(rollback_notes)
            return "\n".join(msg)
        ops.close()
        results = list(dict.fromkeys(preflight_notes + ops.notes)) + results
        plans = written

        # P2 改進：自動驗證流程
        successfully_patched = [p[0] for p in plans]
        if successfully_patched:
            verify_results = self._verify_patched_files(successfully_patched)
            results.extend(verify_results)

        return "\n".join(results) if results else "沒有修改"

    def _verify_patched_files(self, filepaths: list) -> list:
        """P2 改進：驗證修改後的檔案

        現在只做「同 process、無 subprocess、唯讀」的 syntax check(patch_verify):
          .py/.pyi 用 ast.parse;.pyx 明示 skipped;C/C++ 只在釘版 tree-sitter grammar
          載入成功時檢查(ERROR + 零寬 MISSING 都算 failed);其他 suffix skipped。
        三態 passed / failed / skipped:有任何 skipped 就是「驗證不完整」;syntax 是寫入
        後的 advisory gate,失敗不回滾(第一行明講「patch 已套用、未回滾」)。
        lint / typecheck / test 不再由這裡執行——那會把使用者對 apply_patch 的核准暗中
        擴張成命令執行核准。要 lint / test 請另行呼叫 run_lint(fix=False) /
        run_command,各自經過核准閘。這裡永不呼叫 run_lint / run_command /
        subprocess;整個函式體包在最外層 try/except,永不把例外拋回 apply_patch
        (寫入已完成,拋錯只會讓使用者看不到結果)。
        唯一的讀檔路徑是這裡的區域函式 read_bytes:_safe_path → lstat(非 symlink、
        regular)→ O_NOFOLLOW 開檔 → fstat → 讀到 EOF → 再 fstat,三次身分
        (st_ino/st_dev)都一致才採用;patch_verify 自己永不開檔。

        Args:
            filepaths: 本次實際寫入 / 新建的 repo 相對路徑。

        Returns:
            要附加到 apply_patch 結果尾端的文字行;第一行固定是三態標題。
        """
        fallback_next_steps = (
            "建議下一步: 對改過的檔案呼叫 run_lint(fix=False) 做 lint 檢查；"
            "run_command(\"pytest ...\") 跑相關測試（各需獨立核准；apply_patch 不代跑）"
        )
        try:
            import stat as _stat

            def same_identity(before, after) -> bool:
                return (before.st_ino, before.st_dev) == (after.st_ino, after.st_dev)

            def read_bytes(rel_path: str) -> bytes:
                target = self._safe_path(rel_path)
                if target is None:
                    raise patch_verify.ReadRefused("path outside sandbox")
                try:
                    before = os.lstat(target)
                except OSError as exc:
                    raise patch_verify.ReadRefused(
                        f"unreadable ({type(exc).__name__}: {exc.strerror or exc})"
                    ) from exc
                if _stat.S_ISLNK(before.st_mode) or not _stat.S_ISREG(before.st_mode):
                    raise patch_verify.ReadRefused("target is not a regular file")
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
                try:
                    fd = os.open(str(target), flags)
                except OSError as exc:
                    raise patch_verify.ReadRefused(
                        f"unreadable ({type(exc).__name__}: {exc.strerror or exc})"
                    ) from exc
                try:
                    opened = os.fstat(fd)
                    if not _stat.S_ISREG(opened.st_mode) or not same_identity(before, opened):
                        raise patch_verify.ReadRefused("file identity changed during read")
                    chunks = []
                    while True:
                        chunk = os.read(fd, 1 << 16)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    after = os.fstat(fd)
                    if not same_identity(before, after) or not same_identity(opened, after):
                        raise patch_verify.ReadRefused("file identity changed during read")
                finally:
                    os.close(fd)
                return b"".join(chunks)

            auto_verify = bool(getattr(config, "PATCH_AUTO_VERIFY", True))
            requested = [str(step) for step in (getattr(config, "PATCH_VERIFY_STEPS", None) or [])]
            rel_paths = [str(path).replace("\\", "/") for path in filepaths]
            if not auto_verify:
                return patch_verify.render_report([], requested=requested, auto_verify=False)
            try:
                results = patch_verify.verify_files(rel_paths, requested, read_bytes=read_bytes)
            except Exception as exc:
                reason = f"verifier error ({type(exc).__name__}: {str(exc)[:120]})"
                results = [
                    patch_verify.StepResult(step, rel_path, "skipped", reason=reason)
                    for rel_path in rel_paths
                    for step in requested
                ]
            return patch_verify.render_report(results, requested=requested, auto_verify=True)
        except Exception as exc:
            # 最後一道:連 renderer / 常數都不信任,硬編安全文字。
            return [
                f"⚠ 驗證不完整（skipped: verifier error {type(exc).__name__}）——patch 已套用、未回滾",
                fallback_next_steps,
            ]

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
                        # tests/test_apply_patch.py)。
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
    #   - 零匹配 → 只有當「修改後內容」正好落在 @@ 宣告的新檔起始行(`+c`)
    #     時才視為已套用過(no-op)。沒寫 `+c`、`+c` 越界或對不上位置一律
    #     拒絕,並在訊息裡指出修改後內容在第幾行 —— 純內容比對分不出
    #     「已套用」與「目標漂移、別處剛好相同」,猜錯就是靜默漏改。
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

    def _best_mismatch_report(self, file_lines: list, pattern: list, *,
                              path: str = "", block_label: str = ""):
        """零匹配時的診斷 record(E):相似度只做 deterministic ranking 挑顯示視窗,
        永不產生套用位置;渲染與整次總額由 patch_engine.render_mismatch_records 負責。"""
        return patch_engine.nearest_region_record(
            file_lines, pattern, path=path, block_label=block_label,
        )

    def _resolve_already_applied(self, idx: int, positions: list,
                                 new_start: int | None, line_count: int) -> tuple:
        """零匹配時判斷這個 hunk 是不是真的已經套用過。

        只認一種硬證據:「修改後內容」出現的位置,正好是 hunk header 宣告的
        **新檔起始行**(`@@ -a,b +c,d @@` 的 c)。post-image 本來就活在新檔
        座標上,所以同一份 patch 重送時 c 一定對得上(多 hunk 也對得上,因為
        c 已經含了前面 hunk 造成的位移)。

        其餘情況一律 fail loud。純內容比對無法區分「已套用過」與「目標區塊
        漂移、檔案別處剛好有相同內容」——猜錯就是靜默跳過真正該改的地方。
        訊息會講清楚「修改後內容在行 N」,模型據此判斷要不要重送。

        Returns:
            (pos, err):err 非 None 時視為定位失敗(不當成 no-op)。
        """
        listed = ', '.join(str(p + 1) for p in positions[:5])
        if new_start is None or not 1 <= new_start <= line_count:
            missing = "沒有寫新檔行號" if new_start is None else \
                f"新檔行號 {new_start} 超出檔案範圍(共 {line_count} 行)"
            return None, (
                f"區塊 {idx + 1} 的 context 對不上,但檔案行 {listed} 有相同的"
                f"「修改後內容」。@@ {missing},無法確認那就是這個區塊的位置"
                "(目標區塊漂移時,別處的相同內容看起來一模一樣),"
                "所以不宣稱已套用過。\n"
                f"→ 若你確認這個區塊已經改過(行 {listed}),就不要再送它;"
                "否則請先 read_file 確認現況,並在 @@ 補上 `+<新檔起始行>` "
                "或增加 context 行數後重送。"
            )
        if (new_start - 1) not in positions:
            return None, (
                f"區塊 {idx + 1} 的 context 對不上;「修改後內容」在檔案行 "
                f"{listed},但 @@ 宣告的新檔起始行是 {new_start},兩者不一致,"
                "不能認定是同一處。請先 read_file 確認現況再重送。"
            )
        return new_start - 1, None

    def _locate_hunks(self, file_lines: list, hunks: list, *,
                      path: str = "", records: list | None = None) -> tuple:
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
                        if records is not None:
                            records.append(patch_engine.ambiguity_record(
                                file_lines, positions, path=path, block_label=f"區塊 {idx + 1}",
                            ))
                        return None, (
                            f"區塊 {idx + 1} 的 context 在檔案中出現 "
                            f"{len(positions)} 處(行 "
                            f"{', '.join(str(p + 1) for p in positions[:5])}),"
                            f"行號提示 {hint} 距離打平無法消歧。"
                            "請增加 context 行數。"
                        )
                    pos = best
                else:
                    if records is not None:
                        records.append(patch_engine.ambiguity_record(
                            file_lines, positions, path=path, block_label=f"區塊 {idx + 1}",
                        ))
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
                            idx, done, hunk.get('new_start'), line_count,
                        )
                        if done_err:
                            if records is not None:
                                records.append(self._best_mismatch_report(
                                    file_lines, pattern, path=path, block_label=f"區塊 {idx + 1}",
                                ))
                            return None, done_err
                        plan.append({
                            'index': idx, 'status': 'already',
                            'pos': done_pos, 'replace_len': 0,
                            'new_lines': [], 'relocated': False,
                        })
                        continue
                record = self._best_mismatch_report(
                    file_lines, pattern, path=path, block_label=f"區塊 {idx + 1}",
                )
                if records is not None:
                    records.append(record)
                return None, (
                    f"區塊 {idx + 1} context 不匹配(在檔案中找不到對應內容)。"
                    "\n提示: context 行必須與檔案現況一致;"
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

    # 非 git 專案:回固定的跳過通知,不是「錯誤:」。tool_result_adapter 把「錯誤:」
    # 開頭一律包成 status error + 「修正後重試一次」,可是「不是 git 倉庫」沒有東西
    # 可修;MCP_INSTRUCTIONS 要模型改檔前先看 git,非 git 專案每次都會撞到這裡,
    # 回錯誤只是讓模型多繞一圈(2026-09-02 實機:一個沒有 .git 的 firmware 資料夾)。
    GIT_NOT_A_REPO_NOTICE = (
        "此專案不是 git 倉庫(AICODE_ROOT 與上層目錄都沒有 .git)。"
        "不需要 git 檢查,直接 apply_patch;不要重試 git_status / git_diff。"
    )

    def _run_git(self, args: list, timeout: int) -> process_env.CompletedProcess:
        # LC_ALL=C:下面靠 stderr 的英文 "not a git repository" 判斷非 git 專案,
        # 使用者 LANG 是中文時 git 會翻譯訊息,判斷就落空。只影響訊息語言,
        # porcelain 的路徑引號規則(core.quotePath)與 locale 無關。
        return process_env.run(
            ['git', *args],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=timeout,
            overrides={"LC_ALL": "C"},
        )

    # git 對兩件不同的事說同一句 "not a git repository":
    #   * **discovery 失敗**(真的不在倉庫裡)——訊息一定帶括號:
    #     "(or any of the parent directories): .git" 或
    #     "(or any parent up to mount point /)"。
    #   * **git 環境/metadata 壞了**——"not a git repository: '<path>'",
    #     `GIT_DIR` 指到不存在的路徑、`.git` 檔指向壞掉的 gitdir 都是這一種。
    #     那個專案其實在版控裡,只是 git 用不了。
    # 後者被當成前者的話,模型會拿到「不需要 git 檢查,直接 apply_patch」的
    # **成功**通知:改檔前的 git 檢查被跳過,現場既有的修改完全不會被看到。
    _GIT_DISCOVERY_FAILURE = "not a git repository (or any "

    def _git_failure_message(self, result: process_env.CompletedProcess, label: str) -> str:
        """把失敗的 git 命令翻成使用者訊息:跳過通知 vs 錯誤。

        不能只看失敗那條命令自己的 stderr —— `git diff` 對兩種情況印的是
        **一模一樣**的 "warning: Not a git repository. Use --no-index ..."。
        所以失敗時另外問一次 `git rev-parse`:它的訊息才分得出來,而且只在
        失敗路徑上多跑一次。
        """
        try:
            probe = self._run_git(['rev-parse', '--is-inside-work-tree'], timeout=10)
        except Exception:  # noqa: BLE001 — 探測失敗就退回原本那條命令的錯誤
            probe = None
        if probe is not None and probe.returncode != 0:
            if self._GIT_DISCOVERY_FAILURE in probe.stderr.lower():
                return self.GIT_NOT_A_REPO_NOTICE
            broken = probe.stderr.strip() or f"git rev-parse 失敗(exit {probe.returncode})"
            return (
                f"錯誤: git 環境異常(不是「沒有 git 倉庫」):{broken}。"
                "檢查 GIT_DIR / GIT_WORK_TREE 或損壞的 .git"
            )
        detail = result.stderr.strip()
        return f"錯誤: {detail or f'{label} 失敗(exit {result.returncode})'}"

    def git_status(self) -> str:
        """顯示 git 工作目錄狀態;非 git 專案回固定跳過通知(不是錯誤)"""
        try:
            result = self._run_git(['status', '--porcelain', '-uall'], timeout=10)

            if result.returncode != 0:
                return self._git_failure_message(result, "git status")

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
        except process_env.TimeoutExpired:
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

            result = self._run_git(cmd[1:], timeout=30)

            if result.returncode != 0:
                return self._git_failure_message(result, "git diff")

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
        except process_env.TimeoutExpired:
            return "錯誤: git diff 超時"
        except Exception as e:
            return f"錯誤: {type(e).__name__}: {e}"

    def run_lint(self, path: str, fix: bool = True) -> str:
        """對檔案執行 lint/format 工具。

        fix=True  → 走 LINT_COMMANDS[ext]['fix']（會就地改檔）
        fix=False → 走 LINT_COMMANDS[ext]['check']（只回報、不改檔）；
                    若該副檔名沒提供 check 命令，回錯誤而不是回頭跑 fix。

        readonly session(PATCH_ENABLED=False): fix=True 會改檔,必須一起擋下;
        check-only(fix=False) 仍允許,只回報不寫檔。
        """
        if fix and not config.PATCH_ENABLED:
            return (
                "錯誤: run_lint(fix=True) 已停用(readonly session)。"
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

        # 每個副檔名 / mode 只有一個主命令，拒絕舊的替代工具清單。
        if len(lint_cmds) != 1:
            return f"錯誤: config.LINT_COMMANDS[{ext!r}][{mode!r}] 必須只設定一個主命令。"
        rel_path = str(target.relative_to(self.root))
        cmd_parts = shlex.split(lint_cmds[0])
        if not cmd_parts:
            return f"錯誤: {ext} 的 {mode} 主命令是空的。"
        tool_name = cmd_parts[0]
        cmd_parts.append(rel_path)
        try:
            print(f"   [LINT] 執行: {' '.join(cmd_parts)}", file=sys.stderr)
            result = process_env.run(
                cmd_parts, cwd=str(self.root), capture_output=True, text=True, timeout=60
            )
        except (OSError, process_env.TimeoutExpired) as exc:
            return (f"{DEPENDENCY_ERROR_PREFIX}{tool_name} 無法執行: {exc}；"
                    f"請安裝或修復 {tool_name}。")
        if result.returncode != 0:
            output = result.stderr.strip() or result.stdout.strip()
            return f"錯誤: {tool_name} 執行失敗(exit {result.returncode}):\n{output[:500]}"
        output = result.stdout.strip() or result.stderr.strip()
        return f"=== Lint {rel_path} ===\n✓ {tool_name}: {output[:200] if output else '完成'}"

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
