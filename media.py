#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 媒體檔案處理
- 圖片 OCR（使用 VL 模型）
- 二進位檔案分析（Hex dump + strings 提取，含 offset）
- ELF 檔案解析（header/sections/symbols）
"""

import re
import base64
import shutil
import subprocess
from collections import OrderedDict
from pathlib import Path
from typing import Optional, List, Tuple, Dict

from http_client import get_session
from config import (
    OLLAMA_GENERATE_URL, VL_MODEL, IMAGE_EXTENSIONS,
    BIN_ELF_REPORT_MAX_CHARS, BIN_ELF_HEADER_RESERVED,
    BIN_ELF_MAX_SECTIONS, BIN_ELF_MAX_FUNCS, BIN_ELF_MAX_OBJS, BIN_ELF_MAX_STRINGS
)


# 支援的二進位檔案副檔名
BINARY_EXTENSIONS = {".bin", ".dat", ".raw", ".fw", ".img", ".rom", ".hex"}

# 支援的 ELF 檔案副檔名
ELF_EXTENSIONS = {".elf", ".so", ".o", ".axf", ".out", ".ko"}

# 檔案大小限制
MAX_BINARY_SIZE = 50 * 1024 * 1024  # 50MB

# Magic signatures 用於識別檔案格式
MAGIC_SIGNATURES: List[Tuple[str, bytes]] = [
    ("ELF", b"\x7fELF"),
    ("uImage", b"\x27\x05\x19\x56"),
    ("FDT/DTB", b"\xd0\x0d\xfe\xed"),
    ("gzip", b"\x1f\x8b\x08"),
    ("bzip2", b"BZh"),
    ("xz", b"\xfd7zXZ\x00"),
    ("zstd", b"\x28\xb5\x2f\xfd"),
    ("lz4", b"\x04\x22\x4d\x18"),
    ("ZIP/PK", b"PK\x03\x04"),
    ("squashfs", b"hsqs"),
    ("UBI", b"UBI#"),
    ("JFFS2", b"\x85\x19"),
    ("CPIO", b"070701"),
]

# 用於判斷字串是否有意義的字元集
_PRINTABLE_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    " .,_-/:;()[]{}+=@#%$'\"\\|<>!?*&^~`"
)

# 全域 sandbox root（由 main.py 設定）
_SANDBOX_ROOT: Optional[Path] = None
_ALLOW_EXTERNAL: bool = True  # 預設允許外部檔案（大部分使用場景都是外部路徑）

# Small LRU caches to avoid repeated OCR/BIN/ELF work in a session.
_OCR_CACHE = OrderedDict()
_BIN_CACHE = OrderedDict()
_ELF_CACHE = OrderedDict()
_OCR_CACHE_MAX = 8
_BIN_CACHE_MAX = 6
_ELF_CACHE_MAX = 6


def set_sandbox_root(root: str, allow_external: bool = True) -> None:
    """設定 sandbox 根目錄，只允許讀取此目錄內的檔案

    Args:
        root: sandbox 根目錄
        allow_external: 是否允許讀取外部的圖片和 bin 檔案（預設 True）
    """
    global _SANDBOX_ROOT, _ALLOW_EXTERNAL
    _SANDBOX_ROOT = Path(root).resolve()
    _ALLOW_EXTERNAL = allow_external


def _safe_path(path: str, allow_external: bool = False, allowed_extensions: set = None) -> Optional[Path]:
    """驗證路徑是否在 sandbox 內，防止讀取任意本機檔案

    相對路徑會以 _SANDBOX_ROOT 為基準解析，而非當前工作目錄

    GPT建議改進：
    - 處理路徑空白（strip + 引號移除）
    - 使用 is_file() 而非 exists()（避免目錄誤判）
    - 更好的錯誤訊息

    Args:
        path: 檔案路徑
        allow_external: 是否允許外部路徑
        allowed_extensions: 允許的外部檔案副檔名（None 表示不限制）
    """
    if _SANDBOX_ROOT is None:
        # 未設定 sandbox 時，拒絕所有請求
        return None

    # 路徑預處理：去除空白和引號
    path = path.strip().strip('"').strip("'")
    if not path:
        return None

    try:
        p = Path(path).expanduser()
        # 相對路徑以 sandbox root 為基準，絕對路徑直接使用
        if not p.is_absolute():
            full = (_SANDBOX_ROOT / p).resolve()
        else:
            full = p.resolve()

        # 檢查是否在 sandbox 內
        try:
            full.relative_to(_SANDBOX_ROOT)
            # 在 sandbox 內：只需要確認是檔案（不是目錄）
            if full.is_file():
                return full
            # 檔案可能不存在但路徑有效（讓呼叫者處理）
            if not full.exists():
                return full
            return None  # 是目錄，不是檔案
        except ValueError:
            # 路徑在 sandbox 外
            if allow_external and _ALLOW_EXTERNAL:
                # 外部檔案必須存在且是檔案
                if full.is_file():
                    # 檢查副檔名（如果有限制）
                    if allowed_extensions is None or full.suffix.lower() in allowed_extensions:
                        return full
            return None
    except (OSError, ValueError) as e:
        # 路徑格式錯誤（如 Windows 上的非法字元）
        return None
    except Exception:
        return None


# ============================================================================
# Cache helpers
# ============================================================================

def _cache_key(path: Path, extra: tuple = ()) -> tuple:
    try:
        stat = path.stat()
        base = (str(path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        base = (str(path), None, None)
    return base + extra


def _cache_get(cache: OrderedDict, key: tuple):
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    return None


def _cache_set(cache: OrderedDict, key: tuple, value: str, max_items: int):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > max_items:
        cache.popitem(last=False)


# ============================================================================
# 輔助函式
# ============================================================================

def _cmd_exists(cmd: str) -> bool:
    """檢查命令是否存在"""
    return shutil.which(cmd) is not None


def _run_cmd(cmd: List[str], timeout: int = 30) -> Tuple[Optional[str], Optional[str]]:
    """執行命令並回傳 (stdout, error_msg)"""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding='utf-8', errors='replace'
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            return None, err or f"returncode={result.returncode}"
        return result.stdout, None
    except FileNotFoundError:
        return None, "command_not_found"
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as e:
        return None, str(e)


def _detect_magics(data: bytes, scan_range: int = 65536) -> List[Tuple[str, int]]:
    """在資料中搜尋已知的 magic signatures"""
    found: List[Tuple[str, int]] = []
    search_data = data[:scan_range]
    for name, sig in MAGIC_SIGNATURES:
        idx = search_data.find(sig)
        if idx != -1:
            found.append((name, idx))
    return sorted(found, key=lambda x: x[1])


def _hex_dump(data: bytes, base_offset: int = 0, width: int = 16) -> str:
    """產生 hex dump 格式的輸出"""
    lines: List[str] = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base_offset + i:08x}  {hex_part:<{width * 3}}  |{ascii_part}|")
    return "\n".join(lines)


def _scan_ascii_strings(
    filepath: Path,
    min_len: int = 6,
    max_bytes: Optional[int] = None
) -> List[Tuple[int, str]]:
    """
    純 Python 掃描 ASCII 可讀字串（含 file offset）
    不依賴外部 strings 命令
    """
    results: List[Tuple[int, str]] = []

    with open(filepath, "rb") as f:
        file_offset = 0
        current_chars: List[int] = []
        string_start: Optional[int] = None
        bytes_read = 0

        while True:
            # 每次讀取 1MB
            read_size = 1024 * 1024
            if max_bytes is not None:
                remaining = max_bytes - bytes_read
                if remaining <= 0:
                    break
                read_size = min(read_size, remaining)

            chunk = f.read(read_size)
            if not chunk:
                break

            bytes_read += len(chunk)

            for i, byte in enumerate(chunk):
                # ASCII 可印字元 (32-126)
                if 32 <= byte < 127:
                    if string_start is None:
                        string_start = file_offset + i
                    current_chars.append(byte)
                else:
                    # 字串結束
                    if string_start is not None and len(current_chars) >= min_len:
                        try:
                            s = bytes(current_chars).decode("ascii")
                            results.append((string_start, s))
                        except UnicodeDecodeError:
                            pass
                    current_chars = []
                    string_start = None

            file_offset += len(chunk)

        # 處理檔案結尾的字串
        if string_start is not None and len(current_chars) >= min_len:
            try:
                s = bytes(current_chars).decode("ascii")
                results.append((string_start, s))
            except UnicodeDecodeError:
                pass

    return results


def _is_meaningful_string(s: str) -> bool:
    """判斷字串是否有意義（非純數字/符號雜訊）"""
    s = s.strip()
    if len(s) < 6:
        return False
    # 至少要有一個字母
    if not any(c.isalpha() for c in s):
        return False
    # 可印字元比例要夠高
    printable_count = sum(1 for c in s if c in _PRINTABLE_CHARS)
    return printable_count / len(s) >= 0.7


def _format_strings_with_offset(
    items: List[Tuple[int, str]],
    limit: int,
    max_str_len: int = 200
) -> str:
    """格式化字串列表（含 offset）"""
    lines: List[str] = []
    for offset, s in items[:limit]:
        s = s.strip()
        if len(s) > max_str_len:
            s = s[:max_str_len] + "…"
        lines.append(f"  0x{offset:08x}: {s}")
    if len(items) > limit:
        lines.append(f"  ... (還有 {len(items) - limit} 個)")
    return "\n".join(lines)


# ============================================================================
# ELF 解析相關函式
# ============================================================================

# readelf 輸出解析用的正規表達式
_SECTION_RE = re.compile(
    r"\s*\[\s*(\d+)\]\s+(\S+)\s+(\S+)\s+([0-9a-fA-F]+)\s+"
    r"([0-9a-fA-F]+)\s+([0-9a-fA-F]+)"
)

_SYMBOL_RE = re.compile(
    r"\s*(\d+):\s*([0-9a-fA-F]+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*(.*)"
)


def _parse_elf_header(txt: str) -> Dict[str, str]:
    """解析 readelf -h 輸出"""
    wanted_fields = {
        "Class", "Data", "OS/ABI", "Type", "Machine",
        "Entry point address", "Flags",
        "Start of program headers", "Start of section headers",
        "Number of program headers", "Number of section headers",
    }
    result: Dict[str, str] = {}
    for line in txt.splitlines():
        line = line.strip()
        match = re.match(r"^([^:]+):\s+(.*)$", line)
        if match and match.group(1).strip() in wanted_fields:
            result[match.group(1).strip()] = match.group(2).strip()
    return result


def _parse_elf_sections(txt: str) -> List[Dict]:
    """解析 readelf -SW 輸出"""
    sections: List[Dict] = []
    for line in txt.splitlines():
        match = _SECTION_RE.match(line)
        if not match:
            continue
        sections.append({
            "idx": int(match.group(1)),
            "name": match.group(2),
            "type": match.group(3),
            "addr": int(match.group(4), 16),
            "offset": int(match.group(5), 16),
            "size": int(match.group(6), 16),
        })
    return sections


def _parse_elf_symbols(txt: str) -> List[Dict]:
    """解析 readelf -sW 輸出"""
    lines = txt.splitlines()
    # 找到表頭位置
    start_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("Num:"):
            start_idx = i + 1
            break
    if start_idx is None:
        return []

    symbols: List[Dict] = []
    for line in lines[start_idx:]:
        line = line.rstrip()
        if not line:
            continue
        match = _SYMBOL_RE.match(line)
        if not match:
            continue
        symbols.append({
            "value": int(match.group(2), 16),
            "size": int(match.group(3)),
            "type": match.group(4),
            "bind": match.group(5),
            "ndx": match.group(7),
            "name": match.group(8).strip(),
        })
    return symbols


def _build_elf_report(
    filepath: Path,
    max_sections: int = None,
    max_funcs: int = None,
    max_objs: int = None,
    max_strings: int = None
) -> str:
    """建立 ELF 檔案的完整分析報告

    使用 config 中的 hard cap 設定，優先保護重要資訊（Header、Entry point）
    """
    # 使用 config 的 hard cap 設定
    max_sections = max_sections or BIN_ELF_MAX_SECTIONS
    max_funcs = max_funcs or BIN_ELF_MAX_FUNCS
    max_objs = max_objs or BIN_ELF_MAX_OBJS
    max_strings = max_strings or BIN_ELF_MAX_STRINGS
    file_size = filepath.stat().st_size

    # 讀取檔頭確認是 ELF
    with open(filepath, "rb") as f:
        header = f.read(65536)

    if not header.startswith(b"\x7fELF"):
        return f"[ELF 錯誤] 不是有效的 ELF 檔案 (magic: 0x{header[:4].hex()})"

    report: List[str] = [
        f"檔案: {filepath.name}",
        f"大小: {file_size:,} bytes",
    ]

    elf_fields: Dict[str, str] = {}
    sections: List[Dict] = []
    has_readelf = _cmd_exists("readelf")

    if has_readelf:
        # ELF Header
        hdr_out, hdr_err = _run_cmd(["readelf", "-h", str(filepath)], timeout=10)
        if hdr_out:
            elf_fields = _parse_elf_header(hdr_out)
            report.append("")
            report.append("【ELF Header】")
            for key in ["Class", "Data", "OS/ABI", "Type", "Machine",
                        "Entry point address", "Flags",
                        "Number of program headers", "Number of section headers"]:
                if key in elf_fields:
                    report.append(f"  {key}: {elf_fields[key]}")
        elif hdr_err:
            report.append(f"[WARN] readelf -h 失敗: {hdr_err}")

        # Program Headers
        ph_out, _ = _run_cmd(["readelf", "-lW", str(filepath)], timeout=15)
        if ph_out:
            lines = ph_out.splitlines()
            # 找 Program Headers 區段
            ph_start = None
            for i, line in enumerate(lines):
                if "Program Headers:" in line:
                    ph_start = i + 1
                    break

            if ph_start is not None:
                ph_lines: List[str] = []
                for line in lines[ph_start:]:
                    if "Section to Segment" in line or not line.strip():
                        if ph_lines:  # 已經收集了一些行
                            break
                        continue
                    ph_lines.append(line)

                if ph_lines:
                    report.append("")
                    report.append("【Program Headers】")
                    # 只顯示前 10 行
                    for line in ph_lines[:10]:
                        report.append(f"  {line.strip()}")
                    if len(ph_lines) > 10:
                        report.append(f"  ... (共 {len(ph_lines)} 個)")

        # Sections
        sec_out, _ = _run_cmd(["readelf", "-SW", str(filepath)], timeout=15)
        if sec_out:
            sections = _parse_elf_sections(sec_out)

            # 選擇重要的 sections
            important_names = {
                ".vectors", ".text", ".rodata", ".data", ".bss",
                ".init", ".fini", ".comment", ".symtab", ".dynsym",
                ".strtab", ".shstrtab", ".plt", ".got", ".got.plt",
                ".eh_frame", ".dynamic", ".interp",
            }

            picked = []
            for sec in sections:
                name = sec["name"]
                if (name in important_names or
                    name.startswith(".debug") or
                    name.startswith(".note")):
                    picked.append(sec)

            # 按 idx 排序，限制數量
            picked = sorted(picked, key=lambda s: s["idx"])[:max_sections]

            if picked:
                report.append("")
                report.append(f"【Sections】({len(picked)}/{len(sections)} 個)")
                report.append("  [idx] name              type       addr       offset   size")
                for sec in picked:
                    report.append(
                        f"  [{sec['idx']:2d}] {sec['name']:<17} {sec['type']:<10} "
                        f"0x{sec['addr']:08x} 0x{sec['offset']:06x} 0x{sec['size']:06x}"
                    )

        # Entry point 對應的 section
        if "Entry point address" in elf_fields and sections:
            try:
                entry_str = elf_fields["Entry point address"].split()[0]
                entry_addr = int(entry_str, 16)
                for sec in sections:
                    sec_end = sec["addr"] + max(1, sec["size"])
                    if sec["addr"] <= entry_addr < sec_end:
                        file_off = sec["offset"] + (entry_addr - sec["addr"])
                        report.append("")
                        report.append(
                            f"Entry point 0x{entry_addr:08x} 位於 section {sec['name']} "
                            f"(file offset ≈ 0x{file_off:x})"
                        )
                        break
            except (ValueError, KeyError):
                pass

        # .comment section（編譯器資訊）
        com_out, _ = _run_cmd(["readelf", "-p", ".comment", str(filepath)], timeout=10)
        if com_out:
            com_lines = [l.strip() for l in com_out.splitlines() if l.strip()]
            # 過濾掉標題行
            com_lines = [l for l in com_lines if not l.startswith("String dump")]
            if com_lines:
                report.append("")
                report.append("【.comment（編譯器資訊）】")
                for line in com_lines[:10]:
                    report.append(f"  {line}")

        # Symbols
        sym_out, _ = _run_cmd(["readelf", "-sW", str(filepath)], timeout=30)
        if sym_out:
            symbols = _parse_elf_symbols(sym_out)

            # 篩選 global/weak functions 和 objects
            funcs = [s for s in symbols
                     if s["type"] == "FUNC"
                     and s["bind"] in ("GLOBAL", "WEAK")
                     and s["size"] > 0
                     and s["ndx"] not in ("UND", "ABS")]

            objs = [s for s in symbols
                    if s["type"] == "OBJECT"
                    and s["bind"] in ("GLOBAL", "WEAK")
                    and s["size"] > 0
                    and s["ndx"] not in ("UND", "ABS")]

            report.append("")
            report.append(
                f"【Symbols】總數: {len(symbols)}, "
                f"Global/Weak Funcs: {len(funcs)}, Objects: {len(objs)}"
            )

            if funcs:
                # 按 size 排序
                top_funcs = sorted(funcs, key=lambda s: s["size"], reverse=True)[:max_funcs]
                report.append("")
                report.append(f"Top {len(top_funcs)} Functions (by size):")
                report.append("  addr       size   name")
                for sym in top_funcs:
                    name = sym["name"]
                    if len(name) > 60:
                        name = name[:60] + "…"
                    report.append(f"  0x{sym['value']:08x} {sym['size']:6d}  {name}")

            if objs:
                top_objs = sorted(objs, key=lambda s: s["size"], reverse=True)[:max_objs]
                report.append("")
                report.append(f"Top {len(top_objs)} Objects (by size):")
                report.append("  addr       size   name")
                for sym in top_objs:
                    name = sym["name"]
                    if len(name) > 60:
                        name = name[:60] + "…"
                    report.append(f"  0x{sym['value']:08x} {sym['size']:6d}  {name}")
    else:
        report.append("")
        report.append("[WARN] 系統缺少 readelf，將只提供 strings 分析")

    # 高優先字串（含 offset）
    raw_strings = _scan_ascii_strings(filepath, min_len=6, max_bytes=None)

    # 篩選高優先字串（版本/編譯資訊）
    year_pattern = re.compile(r"\b20(1[5-9]|2\d)\b")
    keywords = ["version", "compiled", "gcc", "clang", "llvm", "build", "built",
                "u-boot", "linux", "kernel", "firmware"]

    high_priority: List[Tuple[int, str]] = []
    seen_strings: set = set()

    for offset, s in raw_strings:
        s = s.strip()
        if not s or not _is_meaningful_string(s):
            continue

        s_lower = s.lower()
        is_high = (
            any(kw in s_lower for kw in keywords) or
            year_pattern.search(s)
        )

        if is_high and s not in seen_strings:
            seen_strings.add(s)
            high_priority.append((offset, s))

    if high_priority:
        # 按 offset 排序
        high_priority.sort(key=lambda x: x[0])
        report.append("")
        report.append(f"【高優先字串】({min(max_strings, len(high_priority))}/{len(high_priority)} 個)")
        report.append(_format_strings_with_offset(high_priority, limit=max_strings))

    # Hard cap 保護：確保報告不超過上限，且保護重要資訊
    full_report = "\n".join(report)

    if len(full_report) > BIN_ELF_REPORT_MAX_CHARS:
        # 找出重要段落的結束位置（Header + Entry point）
        # 這些資訊在報告開頭，必須保留
        critical_markers = ["Entry point 0x", "【Sections】", "【Program Headers】"]
        critical_end = 0

        for marker in critical_markers:
            pos = full_report.find(marker)
            if pos != -1:
                # 找到該段落的結尾（下一個空行或下一個【】）
                next_section = full_report.find("\n【", pos + len(marker))
                if next_section != -1:
                    critical_end = max(critical_end, next_section)
                else:
                    critical_end = max(critical_end, pos + 500)

        # 確保至少保留 header 區域
        critical_end = max(critical_end, min(BIN_ELF_HEADER_RESERVED, len(full_report)))

        # 計算可用於後續內容的空間
        remaining_budget = BIN_ELF_REPORT_MAX_CHARS - critical_end - 200  # 200 for truncation notice

        if remaining_budget > 0:
            truncated = (
                full_report[:critical_end] +
                full_report[critical_end:critical_end + remaining_budget] +
                f"\n\n... [報告已截斷，原長度 {len(full_report):,} chars，保留關鍵 Header/Entry point 資訊]"
            )
        else:
            truncated = (
                full_report[:BIN_ELF_REPORT_MAX_CHARS - 100] +
                f"\n\n... [報告已截斷，原長度 {len(full_report):,} chars]"
            )

        return truncated

    return full_report


# ============================================================================
# 公開 API
# ============================================================================

def ocr_image(path: str) -> str:
    """對圖片進行 OCR"""
    p = _safe_path(path, allow_external=True, allowed_extensions=IMAGE_EXTENSIONS)

    if p is None:
        if _ALLOW_EXTERNAL:
            return f"[OCR 錯誤] 檔案不存在或不是支援的圖片格式: {path}"
        else:
            return f"[OCR 錯誤] 路徑不在允許範圍內或檔案不存在: {path}"

    if not p.exists():
        return f"[OCR 錯誤] 檔案不存在: {path}"

    if p.suffix.lower() not in IMAGE_EXTENSIONS:
        return f"[OCR 錯誤] 不支援的格式: {p.suffix}"

    file_size = p.stat().st_size
    if file_size > 20 * 1024 * 1024:
        return f"[OCR 錯誤] 圖片過大: {file_size / 1024 / 1024:.1f}MB"

    cache_key = _cache_key(p)
    cached = _cache_get(_OCR_CACHE, cache_key)
    if cached is not None:
        return cached

    try:
        with open(p, "rb") as f:
            data = base64.b64encode(f.read()).decode()

        session = get_session()
        resp = session.post(OLLAMA_GENERATE_URL, json={
            "model": VL_MODEL,
            "prompt": "列出圖片中的所有文字，保持格式。",
            "images": [data],
            "stream": False,
            "options": {"num_ctx": 4096, "temperature": 0.1},
        }, timeout=120)
        resp.raise_for_status()
        result = resp.json().get("response", "").strip()
        _cache_set(_OCR_CACHE, cache_key, result, _OCR_CACHE_MAX)
        return result
    except Exception as e:
        return f"[OCR 錯誤] {type(e).__name__}: {e}"


def read_elf(path: str, max_sections: int = 40, max_funcs: int = 30,
              max_objs: int = 15, max_strings: int = 30) -> str:
    """讀取 ELF 檔案並產生分析報告

    Args:
        path: ELF 檔案路徑
        max_sections: 最多顯示的 section 數量
        max_funcs: 最多顯示的 function 數量
        max_objs: 最多顯示的 object 數量
        max_strings: 最多顯示的高優先字串數量
    """
    # 外部檔案不限制副檔名（process_file 已做 header sniffing）
    # sandbox 內檔案才檢查白名單（ELF + BIN，因為 .bin 可能是 ELF）
    allowed_ext = None if _ALLOW_EXTERNAL else (ELF_EXTENSIONS | BINARY_EXTENSIONS)
    p = _safe_path(path, allow_external=True, allowed_extensions=allowed_ext)

    if p is None:
        if _ALLOW_EXTERNAL:
            return f"[ELF 錯誤] 檔案不存在或不是支援的格式: {path}"
        else:
            return f"[ELF 錯誤] 路徑不在允許範圍內或檔案不存在: {path}"

    if not p.exists():
        return f"[ELF 錯誤] 檔案不存在: {path}"

    file_size = p.stat().st_size
    if file_size > MAX_BINARY_SIZE:
        return f"[ELF 錯誤] 檔案過大: {file_size / 1024 / 1024:.1f}MB (上限 {MAX_BINARY_SIZE // 1024 // 1024}MB)"

    cache_key = _cache_key(p, (max_sections, max_funcs, max_objs, max_strings))
    cached = _cache_get(_ELF_CACHE, cache_key)
    if cached is not None:
        return cached

    try:
        result = _build_elf_report(
            p,
            max_sections=max_sections,
            max_funcs=max_funcs,
            max_objs=max_objs,
            max_strings=max_strings
        )
        _cache_set(_ELF_CACHE, cache_key, result, _ELF_CACHE_MAX)
        return result
    except Exception as e:
        return f"[ELF 錯誤] {type(e).__name__}: {e}"


def read_binary(path: str, max_strings: int = 200) -> str:
    """讀取二進位檔案並轉換為可分析格式

    使用純 Python 掃描字串（含 offset），若偵測到 ELF magic 會自動切換到 ELF 解析

    注意：外部檔案（_ALLOW_EXTERNAL=True）不檢查副檔名，因為 process_file() 已經
    用 magic header 做了類型判斷。這讓未知副檔名的檔案（如 firmware 無副檔名）也能分析。
    """
    # 外部檔案不限制副檔名（process_file 已做 header sniffing）
    # sandbox 內檔案才檢查白名單
    allowed_ext = None if _ALLOW_EXTERNAL else BINARY_EXTENSIONS
    p = _safe_path(path, allow_external=True, allowed_extensions=allowed_ext)

    if p is None:
        if _ALLOW_EXTERNAL:
            return f"[BIN 錯誤] 檔案不存在: {path}"
        else:
            return f"[BIN 錯誤] 路徑不在允許範圍內或檔案不存在: {path}"

    if not p.exists():
        return f"[BIN 錯誤] 檔案不存在: {path}"

    file_size = p.stat().st_size
    if file_size > MAX_BINARY_SIZE:
        return f"[BIN 錯誤] 檔案過大: {file_size / 1024 / 1024:.1f}MB (上限 {MAX_BINARY_SIZE // 1024 // 1024}MB)"

    cache_key = _cache_key(p, (max_strings,))
    cached = _cache_get(_BIN_CACHE, cache_key)
    if cached is not None:
        return cached

    try:
        # 讀取檔頭
        with open(p, "rb") as f:
            header = f.read(65536)

        # 自動偵測 ELF：若是 ELF 則切換到 ELF 解析
        if header.startswith(b"\x7fELF"):
            result = "[BIN→ELF] 偵測到 ELF magic，自動切換 ELF 解析模式:\n\n" + _build_elf_report(p)
            _cache_set(_BIN_CACHE, cache_key, result, _BIN_CACHE_MAX)
            return result

        # 基本資訊
        report: List[str] = [
            f"檔案: {p.name}",
            f"大小: {file_size:,} bytes",
        ]

        # Magic signatures 偵測
        magics = _detect_magics(header)
        if magics:
            report.append("")
            report.append("【Magic/格式偵測】")
            for name, offset in magics:
                report.append(f"  {name} @ 0x{offset:x}")

        # Hex dump（前 1KB）
        hex_data = header[:1024]
        report.append("")
        report.append("【Hex Dump（前 1KB）】")
        report.append(_hex_dump(hex_data))

        # 使用純 Python 掃描字串（含 offset）
        raw_strings = _scan_ascii_strings(p, min_len=6, max_bytes=None)

        # 過濾有意義的字串
        meaningful: List[Tuple[int, str]] = []
        for offset, s in raw_strings:
            if _is_meaningful_string(s):
                meaningful.append((offset, s.strip()))

        if meaningful:
            # 分類：高優先（版本/編譯）、中優先（boot）、其他
            year_pattern = re.compile(r"\b20(1[5-9]|2\d)\b")
            high_keywords = ["version", "compiled", "gcc", "clang", "llvm",
                             "build", "built", "u-boot"]

            high_priority: List[Tuple[int, str]] = []
            medium_priority: List[Tuple[int, str]] = []
            normal: List[Tuple[int, str]] = []

            for offset, s in meaningful:
                s_lower = s.lower()
                if (any(kw in s_lower for kw in high_keywords) or
                    year_pattern.search(s)):
                    high_priority.append((offset, s))
                elif "boot" in s_lower:
                    medium_priority.append((offset, s))
                else:
                    normal.append((offset, s))

            report.append("")
            report.append(f"【可讀字串（含 offset）】共 {len(meaningful)} 個")

            if high_priority:
                report.append("")
                report.append("[最重要 - 版本/編譯資訊]:")
                report.append(_format_strings_with_offset(
                    sorted(high_priority, key=lambda x: x[0]),
                    limit=min(50, max_strings)
                ))

            if medium_priority:
                report.append("")
                report.append("[Boot 相關]:")
                report.append(_format_strings_with_offset(
                    sorted(medium_priority, key=lambda x: x[0]),
                    limit=min(20, max_strings)
                ))

            # 計算剩餘配額
            remaining = max_strings - min(len(high_priority), 50) - min(len(medium_priority), 20)
            if remaining > 0 and normal:
                report.append("")
                report.append("[其他字串]:")
                report.append(_format_strings_with_offset(
                    sorted(normal, key=lambda x: x[0]),
                    limit=remaining
                ))

        # Hard cap 保護：確保報告不超過上限，保護 Magic/Hex dump 等重要資訊
        full_report = "\n".join(report)

        if len(full_report) > BIN_ELF_REPORT_MAX_CHARS:
            # BIN 報告的重要資訊在開頭：檔名、大小、Magic、Hex dump
            # 找到 Hex dump 結束位置
            hex_end = full_report.find("【可讀字串")
            if hex_end == -1:
                hex_end = min(BIN_ELF_HEADER_RESERVED, len(full_report))

            remaining_budget = BIN_ELF_REPORT_MAX_CHARS - hex_end - 200

            if remaining_budget > 0:
                truncated = (
                    full_report[:hex_end] +
                    full_report[hex_end:hex_end + remaining_budget] +
                    f"\n\n... [報告已截斷，原長度 {len(full_report):,} chars，保留 Magic/Hex dump 資訊]"
                )
            else:
                truncated = (
                    full_report[:BIN_ELF_REPORT_MAX_CHARS - 100] +
                    f"\n\n... [報告已截斷，原長度 {len(full_report):,} chars]"
                )
            _cache_set(_BIN_CACHE, cache_key, truncated, _BIN_CACHE_MAX)
            return truncated

        _cache_set(_BIN_CACHE, cache_key, full_report, _BIN_CACHE_MAX)
        return full_report

    except Exception as e:
        return f"[BIN 錯誤] {type(e).__name__}: {e}"


def _build_binary_context(tag: str, content: str, warn_msg: str = "") -> str:
    """建立 BIN/ELF 分析結果的上下文字串（共用模板）

    Args:
        tag: 'BIN' 或 'ELF'
        content: 分析結果內容
        warn_msg: 警告訊息（可選）

    Returns:
        格式化的上下文字串
    """
    return f"""
╔══════════════════════════════════════════════════════════════╗
║ ⚠️ [{tag}] 本輪回答的最高優先依據：下方 {tag} 分析結果（含 offset/addr） ║
╚══════════════════════════════════════════════════════════════╝

【強制規則 - 違反將導致回答錯誤】
1. 必須先使用下方 [{tag}] 內容判斷與推導
2. 回答必須明確說明「在 {tag} 中找到…」或「在 {tag} 中沒有找到…」
3. 重要性排序：{tag} > knowledge.json([REF]) > 程式碼 > 一般文件
4. 若 {tag} 與程式碼/文件衝突，以 {tag} 為準
5. 本輪只分析一個檔案（見下方 warning）
{warn_msg}
---------------- [{tag}] 解析結果開始 ----------------
{content}
---------------- [{tag}] 解析結果結束 ----------------
"""


def process_binary(text: str) -> tuple[str, str]:
    """處理文字中的二進位/ELF 檔案引用

    支援：
    - elf:/path/to/file.elf - ELF 解析（舊語法，向後相容）
    - bin:/path/to/file.bin - 二進位解析（自動偵測 ELF）
    - elf:"/path with spaces/file.elf" - 帶引號的路徑（支援空白）
    - bin:'path with spaces/file.bin' - 單引號也支援

    注意：建議使用新的 file: 統一語法，見 process_file()

    規則：每輪只分析第一個，避免 context 爆掉
    """
    # 匹配 elf:/bin: 後面跟著：
    # 1. 雙引號包圍的路徑 "..."
    # 2. 單引號包圍的路徑 '...'
    # 3. 無空白的路徑
    pattern = re.compile(
        r'(elf|bin):(?:"([^"]+)"|\'([^\']+)\'|([^\s]+))',
        flags=re.IGNORECASE
    )
    matches = list(pattern.finditer(text))

    if not matches:
        return text, ""

    # 清除所有 elf:/bin: 標記
    clean = pattern.sub("", text).strip()

    def extract_path(m) -> str:
        """從 match 中提取路徑（處理引號和非引號格式）"""
        # group(2) = 雙引號, group(3) = 單引號, group(4) = 無引號
        return m.group(2) or m.group(3) or m.group(4) or ""

    # 只取第一個（單檔規則）
    first_match = matches[0]
    kind = first_match.group(1).lower()
    target = extract_path(first_match)

    # 多檔警告
    warn_msg = ""
    if len(matches) > 1:
        others = [f"{m.group(1)}:{extract_path(m)}" for m in matches[1:]]
        warn_msg = f"\n[WARN] 偵測到 {len(matches)} 個 bin/elf 檔案，為避免超出 context，只分析第一個。\n"
        warn_msg += f"       已忽略: {', '.join(others[:3])}"
        if len(others) > 3:
            warn_msg += f" ... 等 {len(others)} 個"
        warn_msg += "\n"
        print(warn_msg.strip())

    # 根據類型呼叫對應函式
    if kind == "elf":
        print(f"[ELF] 讀取: {target}")
        content = read_elf(target)
        tag = "ELF"
    else:
        print(f"[BIN] 讀取: {target}")
        content = read_binary(target)
        tag = "BIN"

    ctx = _build_binary_context(tag, content, warn_msg)
    return clean, ctx


def process_images(text: str, max_images: int = 3) -> tuple[str, str]:
    """處理文字中的圖片引用

    支援：
    - img:/path/to/image.png - 標準路徑（舊語法，向後相容）
    - img:"/path with spaces/image.png" - 帶雙引號的路徑（支援空白）
    - img:'path with spaces/image.png' - 帶單引號的路徑

    注意：建議使用新的 file: 統一語法，見 process_file()

    Args:
        text: 輸入文字
        max_images: 每輪最多處理的圖片數量（控制 context 大小）
    """
    # 匹配 img: 後面跟著：
    # 1. 雙引號包圍的路徑 "..."
    # 2. 單引號包圍的路徑 '...'
    # 3. 無空白的路徑（以圖片副檔名結尾）
    pattern = re.compile(
        r'img:(?:"([^"]+\.(?:png|jpg|jpeg|gif|webp))"|'
        r"'([^']+\.(?:png|jpg|jpeg|gif|webp))'|"
        r'([^\s]+\.(?:png|jpg|jpeg|gif|webp)))',
        flags=re.IGNORECASE
    )
    matches = list(pattern.finditer(text))

    if not matches:
        return text, ""

    # 清除所有 img: 標記
    clean = pattern.sub("", text).strip()

    def extract_path(m) -> str:
        """從 match 中提取路徑（處理引號和非引號格式）"""
        return m.group(1) or m.group(2) or m.group(3) or ""

    # 多圖警告
    if len(matches) > max_images:
        others = [extract_path(m) for m in matches[max_images:]]
        print(f"[WARN] 偵測到 {len(matches)} 個圖片，為避免超出 context，只處理前 {max_images} 個")
        print(f"       已忽略: {', '.join(others[:3])}" + (f" ... 等 {len(others)} 個" if len(others) > 3 else ""))

    ctx = "\n附加圖片:\n"
    for m in matches[:max_images]:
        path = extract_path(m)
        print(f"[IMG] OCR: {path}")
        ctx += f"\n[{path}]:\n{ocr_image(path)}\n"

    return clean, ctx


def process_file(text: str, max_images: int = 3) -> tuple[str, str, dict]:
    """統一處理文字中的 file: 檔案引用（自動偵測檔案類型）

    支援：
    - file:/path/to/image.png - 圖片 OCR（png/jpg/jpeg/gif/webp）
    - file:/path/to/firmware.bin - 二進位分析（bin/dat/raw/fw/img/rom/hex）
    - file:/path/to/app.elf - ELF 解析（elf/so/o/axf/out/ko）
    - file:"/path with spaces/file.bin" - 帶引號的路徑（支援空白）
    - file:'path with spaces/file.png' - 單引號也支援

    自動偵測規則（優先級）：
    1. 副檔名符合圖片格式 → OCR
    2. 副檔名符合 ELF 格式 或 檔案開頭是 ELF magic → ELF 解析
    3. 其他 → 二進位分析

    Args:
        text: 輸入文字
        max_images: 每輪最多處理的圖片數量（二進位檔只處理第一個）

    Returns:
        (清理後的文字, 合併上下文字串, metadata)
        metadata 包含：
        - has_binary: bool - 是否有處理 BIN/ELF 檔案
        - has_image: bool - 是否有處理圖片
        - binary_type: str|None - 'bin' 或 'elf' 或 None
        - image_ctx: str - 圖片 OCR 上下文（獨立，供 strict mode 使用）
        - binary_ctx: str - BIN/ELF 上下文（獨立，供 strict mode 使用）
    """
    # 匹配 file: 後面跟著：
    # 1. 雙引號包圍的路徑 "..."
    # 2. 單引號包圍的路徑 '...'
    # 3. 無空白的路徑
    pattern = re.compile(
        r'file:(?:"([^"]+)"|\'([^\']+)\'|([^\s]+))',
        flags=re.IGNORECASE
    )
    matches = list(pattern.finditer(text))

    empty_metadata = {
        "has_binary": False, "has_image": False, "binary_type": None,
        "image_ctx": "", "binary_ctx": ""
    }
    if not matches:
        return text, "", empty_metadata

    # 清除所有 file: 標記
    clean = pattern.sub("", text).strip()

    def extract_path(m) -> str:
        """從 match 中提取路徑"""
        return m.group(1) or m.group(2) or m.group(3) or ""

    # 分類檔案
    image_files: List[str] = []
    binary_files: List[Tuple[str, str]] = []  # (path, type: 'bin'|'elf')

    for m in matches:
        path = extract_path(m)
        if not path:
            continue

        suffix = Path(path).suffix.lower()

        if suffix in IMAGE_EXTENSIONS:
            image_files.append(path)
        elif suffix in ELF_EXTENSIONS:
            binary_files.append((path, 'elf'))
        elif suffix in BINARY_EXTENSIONS:
            binary_files.append((path, 'bin'))
        else:
            # 未知副檔名：先走 _safe_path 驗證，再嘗試讀取檔頭判斷
            # 這確保「判斷檔案類型」與「實際讀取檔案」使用相同的路徑解析規則
            safe_p = _safe_path(path, allow_external=True)
            if safe_p and safe_p.is_file():
                try:
                    with open(safe_p, "rb") as f:
                        header = f.read(4)
                    if header.startswith(b"\x7fELF"):
                        binary_files.append((path, 'elf'))
                    else:
                        binary_files.append((path, 'bin'))
                except Exception:
                    binary_files.append((path, 'bin'))
            else:
                # 檔案不存在或不在允許範圍內，當作 bin 處理（讓錯誤訊息顯示）
                binary_files.append((path, 'bin'))

    ctx_parts: List[str] = []
    processed_binary_type: Optional[str] = None  # 記錄處理的 binary 類型
    file_image_ctx = ""  # 獨立的圖片 OCR 上下文
    file_binary_ctx = ""  # 獨立的 BIN/ELF 上下文

    # 處理圖片
    if image_files:
        if len(image_files) > max_images:
            others = image_files[max_images:]
            print(f"[WARN] 偵測到 {len(image_files)} 個圖片，為避免超出 context，只處理前 {max_images} 個")
            print(f"       已忽略: {', '.join(others[:3])}" + (f" ... 等 {len(others)} 個" if len(others) > 3 else ""))

        file_image_ctx = "\n附加圖片:\n"
        for path in image_files[:max_images]:
            print(f"[IMG] OCR: {path}")
            file_image_ctx += f"\n[{path}]:\n{ocr_image(path)}\n"
        ctx_parts.append(file_image_ctx)

    # 處理二進位/ELF（只取第一個）
    if binary_files:
        if len(binary_files) > 1:
            others = [f"{p}" for p, _ in binary_files[1:]]
            print(f"[WARN] 偵測到 {len(binary_files)} 個 bin/elf 檔案，為避免超出 context，只分析第一個")
            print(f"       已忽略: {', '.join(others[:3])}" + (f" ... 等 {len(others)} 個" if len(others) > 3 else ""))

        target, kind = binary_files[0]
        processed_binary_type = kind  # 記錄類型供 metadata

        if kind == 'elf':
            print(f"[ELF] 讀取: {target}")
            content = read_elf(target)
            tag = "ELF"
        else:
            print(f"[BIN] 讀取: {target}")
            content = read_binary(target)
            tag = "BIN"

        warn_msg = ""
        if len(binary_files) > 1:
            warn_msg = f"\n[WARN] 本輪只分析第一個檔案，已忽略其他 {len(binary_files) - 1} 個\n"

        file_binary_ctx = _build_binary_context(tag, content, warn_msg)
        ctx_parts.append(file_binary_ctx)

    # 建立 metadata（包含獨立的 image_ctx 和 binary_ctx 供 strict mode 使用）
    metadata = {
        "has_binary": processed_binary_type is not None,
        "has_image": len(image_files) > 0,
        "binary_type": processed_binary_type,
        "image_ctx": file_image_ctx,    # 獨立的圖片 OCR（供 strict mode 的 base_ctx）
        "binary_ctx": file_binary_ctx   # 獨立的 BIN/ELF（供 strict mode 的 binary_ctx）
    }

    return clean, "\n".join(ctx_parts), metadata
