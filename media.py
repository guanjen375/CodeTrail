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

import llama_client
from config import (
    LLAMA_VL_BASE_URL, VL_MODEL, IMAGE_EXTENSIONS,
    BIN_ELF_REPORT_MAX_CHARS,
    BIN_ELF_MAX_SECTIONS, BIN_ELF_MAX_FUNCS, BIN_ELF_MAX_OBJS, BIN_ELF_MAX_STRINGS
)

# pyelftools 為可選依賴：若安裝則優先用結構化解析（避開 readelf 文字 regex 的脆弱性），
# 同時也能拿到 DWARF / relocation / dynamic table。沒裝就走 readelf fallback。
try:
    from elftools.elf.elffile import ELFFile as _PyELFFile  # type: ignore
    from elftools.elf.sections import SymbolTableSection as _PySymTab  # type: ignore
    from elftools.elf.sections import NoteSection as _PyNoteSection  # type: ignore
    from elftools.elf.dynamic import DynamicSection as _PyDynamic  # type: ignore
    _HAS_PYELFTOOLS = True
except ImportError:
    _PyELFFile = None
    _PySymTab = None
    _PyNoteSection = None
    _PyDynamic = None
    _HAS_PYELFTOOLS = False


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

# 全域 sandbox root（由 mcp_server.py 設定）
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

    路徑處理改進：
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
    """解析 readelf -sW 輸出（合併所有 symbol table，向後相容）"""
    tables = _parse_elf_symbols_split(txt)
    merged: List[Dict] = []
    for syms in tables.values():
        merged.extend(syms)
    return merged


def _parse_elf_symbols_split(txt: str) -> Dict[str, List[Dict]]:
    """解析 readelf -sW，依 symbol table 名稱（.symtab/.dynsym/...）拆開回傳。

    readelf 在同一次呼叫會輸出多個 symbol table，每張表前面有：
        Symbol table '<name>' contains N entries:
    對 stripped binary 而言只剩 .dynsym 是 ground truth，合在一起會誤導模型。
    """
    tables: Dict[str, List[Dict]] = {}
    current_name: Optional[str] = None
    in_data = False

    for line in txt.splitlines():
        s = line.strip()
        header_match = re.match(r"Symbol table '([^']+)' contains", s)
        if header_match:
            current_name = header_match.group(1)
            tables.setdefault(current_name, [])
            in_data = False
            continue
        if s.startswith("Num:"):
            in_data = True
            continue
        if not in_data or current_name is None:
            continue
        row_match = _SYMBOL_RE.match(line.rstrip())
        if not row_match:
            continue
        tables[current_name].append({
            "value": int(row_match.group(2), 16),
            "size": int(row_match.group(3)),
            "type": row_match.group(4),
            "bind": row_match.group(5),
            "ndx": row_match.group(7),
            "name": row_match.group(8).strip(),
        })
    return tables


def _get_build_id(filepath: Path) -> Optional[str]:
    """讀 .note.gnu.build-id（GNU build-id 是 binary 的唯一指紋，160-bit SHA1）。

    用 readelf -n；找不到（如非 GNU toolchain 編的）會回 None。
    """
    if not _cmd_exists("readelf"):
        return None
    out, _ = _run_cmd(["readelf", "-n", str(filepath)], timeout=10)
    if not out:
        return None
    m = re.search(r"Build ID:\s+([0-9a-fA-F]+)", out)
    return m.group(1) if m else None


def _disasm_entry(
    filepath: Path,
    entry_addr: int,
    machine: str = "",
    num_bytes: int = 96,
    max_instr: int = 24,
) -> Optional[str]:
    """反組譯 entry point 附近的指令（給模型看實際啟動序列在做什麼）。

    ARM Cortex-M 等 Thumb 架構：entry address 的 LSB=1 是 Thumb 模式旗標，
    傳給 objdump --start-address 前要把 LSB 清掉，否則 objdump 會找不到指令。

    Returns: 格式化後的指令列表（最多 max_instr 條），或 None（命令不存在/無輸出）。
    """
    if not _cmd_exists("objdump"):
        return None

    is_arm = "ARM" in machine.upper() or "AARCH" in machine.upper()
    actual_addr = entry_addr & ~1 if (is_arm and entry_addr & 1) else entry_addr

    out, _err = _run_cmd([
        "objdump", "-d",
        f"--start-address=0x{actual_addr:x}",
        f"--stop-address=0x{actual_addr + num_bytes:x}",
        str(filepath),
    ], timeout=15)
    if not out:
        return None

    # 抽出指令行（格式: "  <hex addr>:  <bytes>   <mnemonic>"）
    instr_lines = [
        line for line in out.splitlines()
        if re.match(r"^\s*[0-9a-f]+:\s+[0-9a-f]", line)
    ]
    if not instr_lines:
        return None
    return "\n".join(f"  {line.strip()}" for line in instr_lines[:max_instr])


def _scan_utf16le_strings(
    filepath: Path,
    min_len: int = 6,
    max_bytes: int = 4 * 1024 * 1024,
) -> List[Tuple[int, str]]:
    """掃描 UTF-16LE 印字字串（韌體 UI / Windows resource 常見格式）。

    UTF-16LE 的 ASCII 字元編碼為 [ascii_byte, 0x00]。預設只掃前 4MB，
    避免大檔過慢；對韌體分析夠用（資源字串通常集中在前段）。
    """
    results: List[Tuple[int, str]] = []
    with open(filepath, "rb") as f:
        data = f.read(max_bytes)

    i = 0
    n = len(data)
    while i < n - 1:
        # 偵測 ASCII 印字 byte 接 0x00 起始
        if 32 <= data[i] < 127 and data[i + 1] == 0:
            start = i
            chars: List[int] = []
            while i < n - 1 and 32 <= data[i] < 127 and data[i + 1] == 0:
                chars.append(data[i])
                i += 2
            if len(chars) >= min_len:
                try:
                    results.append((start, bytes(chars).decode("ascii")))
                except UnicodeDecodeError:
                    pass
        else:
            i += 1
    return results


def _truncate_elf_report(full_report: str) -> str:
    """ELF 報告硬上限（hard cap）。

    報告本身已按重要度由前往後排（Header → Program Headers → Sections →
    Entry/反組譯 → Symbols → Strings），所以直接前綴切片就是「優先保留 header」。
    過去的 critical_end 計算實際等價於前綴切片（兩段相加等於一段），是死碼，已移除。
    """
    if len(full_report) <= BIN_ELF_REPORT_MAX_CHARS:
        return full_report
    return (
        full_report[:BIN_ELF_REPORT_MAX_CHARS - 200] +
        f"\n\n... [報告已截斷，原長度 {len(full_report):,} chars]"
    )


def _collect_high_priority_strings(
    items: List[Tuple[int, str]],
    seen: set,
) -> List[Tuple[int, str]]:
    """從 (offset, string) 清單裡挑出版本/編譯相關高優先字串。

    seen 用於跨多次呼叫去重（例如 ASCII 跟 UTF-16 結果合併時）。
    """
    year_pattern = re.compile(r"\b20(1[5-9]|2\d)\b")
    keywords = ["version", "compiled", "gcc", "clang", "llvm", "build", "built",
                "u-boot", "linux", "kernel", "firmware"]

    out: List[Tuple[int, str]] = []
    for offset, s in items:
        s = s.strip()
        if not s or not _is_meaningful_string(s):
            continue
        s_lower = s.lower()
        is_high = (any(kw in s_lower for kw in keywords) or year_pattern.search(s))
        if is_high and s not in seen:
            seen.add(s)
            out.append((offset, s))
    return out


def _build_elf_report(
    filepath: Path,
    max_sections: int = None,
    max_funcs: int = None,
    max_objs: int = None,
    max_strings: int = None,
) -> str:
    """建立 ELF 檔案的完整分析報告（dispatcher）。

    優先用 pyelftools（結構化、可拿 DWARF/notes），失敗或未安裝就退回 readelf 文字解析。
    最後統一套 hard cap 截斷。
    """
    max_sections = max_sections or BIN_ELF_MAX_SECTIONS
    max_funcs = max_funcs or BIN_ELF_MAX_FUNCS
    max_objs = max_objs or BIN_ELF_MAX_OBJS
    max_strings = max_strings or BIN_ELF_MAX_STRINGS

    # 先確認 ELF magic（兩個 backend 共用的快速失敗）
    with open(filepath, "rb") as f:
        magic = f.read(4)
    if not magic.startswith(b"\x7fELF"):
        return f"[ELF 錯誤] 不是有效的 ELF 檔案 (magic: 0x{magic.hex()})"

    # 「fail loud」：pyelftools 失敗或未安裝時，把原因明文寫進 report，
    # 不要只 print()——MCP 回傳裡 stdout 看不到，否則模型無法判斷自己看到的是
    # 結構化解析還是 readelf 文字 fallback。
    pyelf_failure: Optional[str] = None
    if _HAS_PYELFTOOLS:
        try:
            report = _build_elf_report_native(
                filepath, max_sections, max_funcs, max_objs, max_strings
            )
            return _truncate_elf_report(report)
        except Exception as e:
            pyelf_failure = (
                f"[WARN] pyelftools 解析失敗 ({type(e).__name__}: {e})，已退回 readelf 文字解析"
            )
            print(pyelf_failure)

    report = _build_elf_report_readelf(
        filepath, max_sections, max_funcs, max_objs, max_strings,
        pyelf_failure=pyelf_failure,
    )
    return _truncate_elf_report(report)


def _format_symbol_table(
    syms: List[Dict],
    table_label: str,
    max_funcs: int,
    max_objs: int,
) -> List[str]:
    """把單張 symbol table（如 .symtab 或 .dynsym）格式化成報告行。

    篩 GLOBAL/WEAK FUNC/OBJECT 且 size>0、ndx 非 UND/ABS。
    """
    out: List[str] = []
    funcs = [s for s in syms
             if s["type"] == "FUNC"
             and s["bind"] in ("GLOBAL", "WEAK")
             and s["size"] > 0
             and s["ndx"] not in ("UND", "ABS")]
    objs = [s for s in syms
            if s["type"] == "OBJECT"
            and s["bind"] in ("GLOBAL", "WEAK")
            and s["size"] > 0
            and s["ndx"] not in ("UND", "ABS")]

    out.append("")
    out.append(
        f"【Symbols / {table_label}】總數: {len(syms)}, "
        f"Global/Weak Funcs: {len(funcs)}, Objects: {len(objs)}"
    )

    if funcs:
        top_funcs = sorted(funcs, key=lambda s: s["size"], reverse=True)[:max_funcs]
        out.append("")
        out.append(f"Top {len(top_funcs)} Functions in {table_label} (by size):")
        out.append("  addr       size   name")
        for sym in top_funcs:
            name = sym["name"]
            if len(name) > 60:
                name = name[:60] + "…"
            out.append(f"  0x{sym['value']:08x} {sym['size']:6d}  {name}")

    if objs:
        top_objs = sorted(objs, key=lambda s: s["size"], reverse=True)[:max_objs]
        out.append("")
        out.append(f"Top {len(top_objs)} Objects in {table_label} (by size):")
        out.append("  addr       size   name")
        for sym in top_objs:
            name = sym["name"]
            if len(name) > 60:
                name = name[:60] + "…"
            out.append(f"  0x{sym['value']:08x} {sym['size']:6d}  {name}")
    return out


def _build_elf_report_readelf(
    filepath: Path,
    max_sections: int,
    max_funcs: int,
    max_objs: int,
    max_strings: int,
    pyelf_failure: Optional[str] = None,
) -> str:
    """ELF 報告：readelf / objdump 文字解析路徑（fallback）。"""
    file_size = filepath.stat().st_size

    report: List[str] = [
        f"檔案: {filepath.name}",
        f"大小: {file_size:,} bytes",
        "(parser: readelf 文字解析)",
    ]
    if pyelf_failure:
        report.append(pyelf_failure)

    elf_fields: Dict[str, str] = {}
    sections: List[Dict] = []
    has_readelf = _cmd_exists("readelf")

    if not has_readelf:
        report.append("")
        report.append("[WARN] 系統缺少 readelf，將只提供 strings 分析")
    else:
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

        # GNU build-id（binary 指紋；非 GNU toolchain 可能沒有）
        build_id = _get_build_id(filepath)
        if build_id:
            report.append(f"  GNU build-id: {build_id}")

        # Program Headers
        ph_out, _ = _run_cmd(["readelf", "-lW", str(filepath)], timeout=15)
        if ph_out:
            lines = ph_out.splitlines()
            ph_start = None
            for i, line in enumerate(lines):
                if "Program Headers:" in line:
                    ph_start = i + 1
                    break

            if ph_start is not None:
                ph_lines: List[str] = []
                for line in lines[ph_start:]:
                    if "Section to Segment" in line or not line.strip():
                        if ph_lines:
                            break
                        continue
                    ph_lines.append(line)

                if ph_lines:
                    report.append("")
                    report.append("【Program Headers】")
                    for line in ph_lines[:10]:
                        report.append(f"  {line.strip()}")
                    if len(ph_lines) > 10:
                        report.append(f"  ... (共 {len(ph_lines)} 個)")

        # Sections
        sec_out, _ = _run_cmd(["readelf", "-SW", str(filepath)], timeout=15)
        if sec_out:
            sections = _parse_elf_sections(sec_out)

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

        # Entry point 對應的 section + 反組譯
        entry_addr: Optional[int] = None
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
                entry_addr = None

        if entry_addr is not None:
            disasm = _disasm_entry(filepath, entry_addr, machine=elf_fields.get("Machine", ""))
            if disasm:
                report.append("")
                report.append("【Entry 反組譯（前幾條指令）】")
                report.append(disasm)

        # .comment section（編譯器資訊）
        com_out, _ = _run_cmd(["readelf", "-p", ".comment", str(filepath)], timeout=10)
        if com_out:
            com_lines = [l.strip() for l in com_out.splitlines() if l.strip()]
            com_lines = [l for l in com_lines if not l.startswith("String dump")]
            if com_lines:
                report.append("")
                report.append("【.comment（編譯器資訊）】")
                for line in com_lines[:10]:
                    report.append(f"  {line}")

        # Symbols：分 .symtab 與 .dynsym 顯示（stripped binary 只剩 .dynsym 是 ground truth）
        sym_out, _ = _run_cmd(["readelf", "-sW", str(filepath)], timeout=30)
        if sym_out:
            tables = _parse_elf_symbols_split(sym_out)
            # 顯示順序：.symtab 先（資訊較多），.dynsym 後，其他最後
            preferred_order = [".symtab", ".dynsym"]
            ordered_keys = (
                [k for k in preferred_order if k in tables] +
                [k for k in tables.keys() if k not in preferred_order]
            )
            for tname in ordered_keys:
                report.extend(_format_symbol_table(
                    tables[tname], tname, max_funcs=max_funcs, max_objs=max_objs
                ))

    # 高優先字串：ASCII + UTF-16LE 合併（含 offset，去重）
    seen_strings: set = set()
    ascii_strings = _scan_ascii_strings(filepath, min_len=6, max_bytes=None)
    high_ascii = _collect_high_priority_strings(ascii_strings, seen_strings)

    utf16_strings = _scan_utf16le_strings(filepath, min_len=6)
    high_utf16 = _collect_high_priority_strings(utf16_strings, seen_strings)

    if high_ascii:
        high_ascii.sort(key=lambda x: x[0])
        report.append("")
        report.append(f"【高優先字串 ASCII】({min(max_strings, len(high_ascii))}/{len(high_ascii)} 個)")
        report.append(_format_strings_with_offset(high_ascii, limit=max_strings))

    if high_utf16:
        high_utf16.sort(key=lambda x: x[0])
        # UTF-16 通常較少，給 1/3 配額避免擠掉 ASCII
        utf16_limit = max(10, max_strings // 3)
        report.append("")
        report.append(f"【高優先字串 UTF-16LE】({min(utf16_limit, len(high_utf16))}/{len(high_utf16)} 個)")
        report.append(_format_strings_with_offset(high_utf16, limit=utf16_limit))

    return "\n".join(report)


# ----- pyelftools backend ----------------------------------------------------

def _py_strip_enum(value: object, prefix: str) -> str:
    """把 pyelftools 的字串 enum（如 'EM_X86_64'）去掉前綴。

    對 int 或其他型別（罕見的未知 enum）就 str() 起來。
    """
    s = str(value)
    return s[len(prefix):] if s.startswith(prefix) else s


# Imported-symbol（.dynsym UND）按 API 家族分桶；對 stripped binary 而言，
# 看不到自己定義的 function 名稱，但能從 imports 推斷做了什麼（網路？crypto？exec？）。
# 順序就是顯示優先順序：威脅模型上更重要的（exec/dynamic_link/crypto/network）放前面。
_IMPORT_API_CATEGORIES: List[Tuple[str, "re.Pattern[str]"]] = [
    ("exec/process", re.compile(
        r"^(fork|vfork|exec[lv][ep]?e?|system|popen|wait[a-z]*|kill|posix_spawn\w*|"
        r"clone\d?|setuid|setgid|seteuid|setegid|setresuid|setresgid|chroot|"
        r"daemon|setsid)$"
    )),
    ("dynamic_link", re.compile(r"^(dlopen|dlmopen|dlsym|dlvsym|dlclose|dlerror|dladdr|dlinfo)$")),
    ("crypto", re.compile(
        r"(^MD[245]|^SHA\d+|^AES_|^DES_|^RSA_|^EVP_|^HMAC_|^RAND_|^BN_|^EC_|^X509_|"
        r"^PEM_|^SSL_|^TLS_|^ENGINE_|^BIO_|^crypto_|^gcry_|^nettle_|^openssl_|"
        r"^mbedtls_|^wolfSSL_|PKCS[71]|PBKDF)"
    )),
    ("network", re.compile(
        r"^(socket|bind|connect|listen|accept4?|send(to|msg)?|recv(from|msg)?|"
        r"select|p?poll|epoll_\w+|getaddrinfo|freeaddrinfo|gethostby\w+|inet_\w+|"
        r"htons|htonl|ntohs|ntohl|getsockopt|setsockopt|shutdown|getpeername|getsockname)$"
    )),
    ("io/fs", re.compile(
        r"^(open(at)?|close|read[v]?|write[v]?|pread\d*|pwrite\d*|lseek\d*|"
        r"f?stat\d*|lstat\d*|access|faccessat|mkdir(at)?|rmdir|unlink(at)?|"
        r"rename(at)?|chmod|fchmod(at)?|chown|fchown(at)?|lchown|ioctl|fcntl\d*|"
        r"dup[23]?|mmap\d*|munmap|msync|mprotect|sync|f?datasync|fsync|"
        r"opendir|fdopendir|readdir\d*|closedir|truncate\d*|ftruncate\d*|"
        r"symlink(at)?|readlink(at)?|getcwd|chdir|fchdir|umask|mknod(at)?|"
        r"flock|fallocate)$"
    )),
    ("threading", re.compile(
        r"^(pthread_|sem_(open|close|wait|post|init|destroy|trywait|timedwait|getvalue|unlink)|"
        r"mtx_|thrd_|tss_|cnd_|atomic_|__atomic_|__sync_)"
    )),
    ("memory", re.compile(
        r"^(malloc|free|calloc|realloc(array)?|mem(cpy|move|set|cmp|chr|rchr|mem)|"
        r"brk|sbrk|mremap|posix_memalign|aligned_alloc|valloc|memalign|pvalloc)$"
    )),
    ("env/sig", re.compile(
        r"^(getenv|setenv|unsetenv|putenv|clearenv|signal|sigaction|sigprocmask|"
        r"sigpending|sigsuspend|sigwait\w*|raise|abort|atexit|on_exit|alarm|"
        r"setitimer|getitimer)$"
    )),
    ("printf/str", re.compile(
        r"^(printf|fprintf|sprintf|snprintf|vprintf|vsprintf|vsnprintf|asprintf|"
        r"vasprintf|dprintf|vdprintf|__\w*printf\w*_chk|"
        r"str(cpy|ncpy|cat|ncat|cmp|ncmp|len|nlen|chr|rchr|str|tok|dup|ndup|casecmp|ncasecmp)|"
        r"__str\w+_chk)$"
    )),
]


def _categorize_imports(imports: List[str]) -> Dict[str, List[str]]:
    """把 imported symbol list 按 API 家族分桶。

    分類順序固定（見 _IMPORT_API_CATEGORIES），第一個匹配的家族即歸屬，
    所以 e.g. pthread_create 會落在 threading（而不是 exec/process 的 fork 同類）。
    """
    by_family: Dict[str, List[str]] = {}
    for name in imports:
        matched = False
        for family, pat in _IMPORT_API_CATEGORIES:
            if pat.search(name):
                by_family.setdefault(family, []).append(name)
                matched = True
                break
        if not matched:
            by_family.setdefault("other", []).append(name)
    return by_family


def _format_imports(imports: List[str], max_per_cat: int = 8) -> List[str]:
    """格式化 imports 為分類列表。

    每類最多顯示 max_per_cat 個；超過就標 '+N more'。對 stripped binary，
    這通常比看 .dynsym 已定義 symbols 更能告訴你「這檔案做什麼」。
    """
    if not imports:
        return []
    by_family = _categorize_imports(imports)

    out: List[str] = ["", f"【Imports（.dynsym UND，按 API 家族分類）】共 {len(imports)} 個"]
    family_order = [f for f, _ in _IMPORT_API_CATEGORIES] + ["other"]
    for family in family_order:
        items = by_family.get(family)
        if not items:
            continue
        shown = items[:max_per_cat]
        more = len(items) - len(shown)
        sample = ", ".join(shown)
        if more > 0:
            sample += f", ... +{more}"
        out.append(f"  [{family}] ({len(items)}) {sample}")
    return out


def _parse_modinfo(data: bytes) -> Dict[str, List[str]]:
    """解析 .modinfo（Linux kernel module 元資料）。

    格式：多個 null-terminated 的 key=value 字串串接。
    同一個 key 可以重複（如 alias=、depends=），所以 value 是 list。
    """
    result: Dict[str, List[str]] = {}
    for raw in data.split(b"\x00"):
        if not raw:
            continue
        try:
            s = raw.decode("utf-8", errors="replace")
        except Exception:
            continue
        if "=" not in s:
            continue
        key, _, val = s.partition("=")
        result.setdefault(key.strip(), []).append(val.strip())
    return result


def _format_modinfo(modinfo: Dict[str, List[str]]) -> List[str]:
    """把 .modinfo 字典格式化成報告行。

    parm/parmtype 很冗（大型 module 可能上百個），只給計數不展開。
    """
    out: List[str] = ["", "【.modinfo（kernel module 元資料）】"]
    priority = [
        "name", "license", "version", "vermagic", "srcversion", "author",
        "description", "depends", "import_ns", "alias", "firmware",
        "intree", "retpoline", "scmversion",
    ]
    shown: set = set()
    for key in priority:
        vals = modinfo.get(key)
        if not vals:
            continue
        shown.add(key)
        if len(vals) == 1:
            out.append(f"  {key}: {vals[0]}")
        else:
            preview = ", ".join(vals[:5])
            tail = f" ... +{len(vals)-5}" if len(vals) > 5 else ""
            out.append(f"  {key} ({len(vals)}): {preview}{tail}")
    other = [k for k in modinfo if k not in shown and k not in ("parm", "parmtype")]
    if other:
        out.append(f"  其他鍵: {', '.join(other[:10])}" +
                   (f" ... +{len(other)-10}" if len(other) > 10 else ""))
    if "parm" in modinfo or "parmtype" in modinfo:
        out.append(
            f"  module params: {len(modinfo.get('parm', []))} parm / "
            f"{len(modinfo.get('parmtype', []))} parmtype"
        )
    return out


def _format_dynamic_section(facts: Dict) -> List[str]:
    """把 .dynamic 摘要格式化成報告行（NEEDED / SONAME / RPATH/RUNPATH 等）。"""
    if not facts.get("has_dynamic"):
        return []
    out: List[str] = ["", "【.dynamic】"]
    if facts.get("soname"):
        out.append(f"  SONAME: {facts['soname']}")
    needed = facts.get("needed", [])
    if needed:
        out.append(f"  NEEDED ({len(needed)}):")
        for lib in needed[:12]:
            out.append(f"    {lib}")
        if len(needed) > 12:
            out.append(f"    ... +{len(needed) - 12}")
    # RPATH/RUNPATH 是 LD_LIBRARY_PATH 風險評估的關鍵資訊
    if facts.get("rpath"):
        out.append(f"  RPATH: {facts['rpath']}")
    if facts.get("runpath"):
        out.append(f"  RUNPATH: {facts['runpath']}")
    flag_bits: List[str] = []
    if facts.get("bind_now"):
        flag_bits.append("BIND_NOW")
    if facts.get("is_pie"):
        flag_bits.append("PIE")
    if flag_bits:
        out.append(f"  Flags: {', '.join(flag_bits)}")
    init_n = facts.get("init_array_count", 0)
    fini_n = facts.get("fini_array_count", 0)
    if init_n or fini_n:
        out.append(f"  INIT_ARRAY: {init_n} entries, FINI_ARRAY: {fini_n} entries")
    return out


def _format_key_facts(facts: Dict) -> List[str]:
    """TL;DR 摘要放在報告最前面。

    設計意圖：就算 hard cap 截斷掉中間段（symbols/strings/DWARF），
    模型還能從這段抓到 arch / type / stripped / linkage / imports 等高價值資訊。
    每行控制在一個 key+value，便於模型快速 index。
    """
    out: List[str] = ["", "【Key Facts】"]
    out.append(f"  Arch     : {facts['machine']} (ELF{facts['class']}, {facts['endian']}-endian)")

    type_str = facts["type"]
    type_extra: List[str] = []
    if facts.get("is_pie"):
        type_extra.append("PIE")
    elif type_str == "DYN" and facts.get("soname"):
        type_extra.append("shared library")
    if facts.get("is_ko"):
        type_extra.append("kernel module")
    type_suffix = f" ({', '.join(type_extra)})" if type_extra else ""
    out.append(f"  Type     : {type_str}{type_suffix}")

    out.append(f"  Entry    : 0x{facts['entry']:08x}")

    has_symtab = facts.get("has_symtab", False)
    has_dynsym = facts.get("has_dynsym", False)
    if has_symtab:
        stripped = "no (.symtab present)"
    elif has_dynsym:
        stripped = "yes (.symtab absent, .dynsym only)"
    else:
        stripped = "fully stripped (no symbol tables)"
    out.append(f"  Stripped : {stripped}")

    if facts.get("has_dynamic"):
        n_needed = len(facts.get("needed", []))
        if n_needed:
            preview = ", ".join(facts["needed"][:3])
            if n_needed > 3:
                preview += f" + {n_needed - 3} more"
            linkage = f"dynamic, {n_needed} NEEDED ({preview})"
        else:
            linkage = "dynamic, no DT_NEEDED"
    else:
        linkage = "static (no .dynamic section)"
    out.append(f"  Linkage  : {linkage}")

    n_imports = facts.get("dynsym_imports_count", 0)
    n_exports = facts.get("dynsym_exports_count", 0)
    out.append(f"  Dynsym   : {n_imports} imports (UND) / {n_exports} exports (defined)")

    out.append(f"  DWARF    : {'present' if facts.get('has_dwarf') else 'absent'}")

    if facts.get("build_id"):
        out.append(f"  Build-id : {facts['build_id']}")

    return out


# DT_FLAGS_1 中代表 PIE 的位元，DF_1_PIE = 0x08000000；BIND_NOW = 0x01。
# 這些常數定義在 glibc / elf.h；pyelftools 不會直接給 enum。
_DF_1_PIE = 0x08000000
_DF_1_NOW = 0x00000001
_DF_BIND_NOW = 0x00000008  # 舊版 DT_FLAGS 裡的 DF_BIND_NOW


def _build_elf_report_native(
    filepath: Path,
    max_sections: int,
    max_funcs: int,
    max_objs: int,
    max_strings: int,
) -> str:
    """ELF 報告：pyelftools 路徑（結構化、不依賴 readelf 文字格式）。

    額外提供 readelf 路徑沒有的：Key Facts 摘要、.dynamic 表（NEEDED/SONAME/
    RPATH/RUNPATH）、imports（.dynsym UND，按 API 家族分類）、.modinfo（kernel
    module）、DWARF compilation unit 來源檔案列表。
    """
    file_size = filepath.stat().st_size
    header_lines: List[str] = [
        f"檔案: {filepath.name}",
        f"大小: {file_size:,} bytes",
        "(parser: pyelftools)",
    ]
    body: List[str] = []  # 詳細資料區段（會在 facts 收集後 append 進來）
    facts: Dict = {}     # Key Facts header 依賴的事實集合

    with open(filepath, "rb") as f:
        elf = _PyELFFile(f)

        # ===== 基本 header 欄位（facts + ELF Header 區塊都用） =====
        machine_str = _py_strip_enum(elf["e_machine"], "EM_")
        type_str = _py_strip_enum(elf["e_type"], "ET_")
        entry_addr = int(elf["e_entry"])
        osabi_str = _py_strip_enum(elf["e_ident"]["EI_OSABI"], "ELFOSABI_")
        facts.update({
            "class": elf.elfclass,
            "endian": "little" if elf.little_endian else "big",
            "machine": machine_str,
            "type": type_str,
            "entry": entry_addr,
            "osabi": osabi_str,
            "has_dwarf": elf.has_dwarf_info(),
        })

        # ===== Sections（一次走訪 + 順便收集 build-id / 旗標 / .modinfo） =====
        sections: List[Dict] = []
        build_id: Optional[str] = None
        modinfo: Optional[Dict[str, List[str]]] = None
        for idx, sec in enumerate(elf.iter_sections()):
            sections.append({
                "idx": idx,
                "name": sec.name,
                "type": _py_strip_enum(sec["sh_type"], "SHT_"),
                "addr": int(sec["sh_addr"]),
                "offset": int(sec["sh_offset"]),
                "size": int(sec["sh_size"]),
            })
            if sec.name == ".symtab":
                facts["has_symtab"] = True
            elif sec.name == ".dynsym":
                facts["has_dynsym"] = True
            elif sec.name == ".dynamic":
                facts["has_dynamic"] = True
            elif sec.name == ".modinfo":
                try:
                    modinfo = _parse_modinfo(sec.data())
                except Exception:
                    modinfo = None
            # GNU build-id 藏在 .note.gnu.build-id 裡，順手抓
            if build_id is None and isinstance(sec, _PyNoteSection):
                try:
                    for note in sec.iter_notes():
                        if note["n_type"] == "NT_GNU_BUILD_ID":
                            desc = note["n_desc"]
                            build_id = desc if isinstance(desc, str) else desc.hex()
                            break
                except Exception:
                    pass
        facts.setdefault("has_symtab", False)
        facts.setdefault("has_dynsym", False)
        facts.setdefault("has_dynamic", False)
        facts["build_id"] = build_id
        facts["is_ko"] = (modinfo is not None) or filepath.suffix.lower() == ".ko"

        # ===== .dynamic 表（NEEDED / SONAME / RPATH / RUNPATH / FLAGS） =====
        # 沒 .dynamic（靜態連結）就略過；存在的話這段資訊對 stripped binary 特別關鍵。
        dyn_sec = elf.get_section_by_name(".dynamic")
        if dyn_sec is not None and isinstance(dyn_sec, _PyDynamic):
            needed: List[str] = []
            soname: Optional[str] = None
            rpath: Optional[str] = None
            runpath: Optional[str] = None
            flags_val = 0
            flags_1_val = 0
            init_arr = 0
            fini_arr = 0
            try:
                for tag in dyn_sec.iter_tags():
                    tname = str(tag.entry.d_tag)
                    if tname == "DT_NEEDED":
                        needed.append(getattr(tag, "needed", str(tag.entry.d_val)))
                    elif tname == "DT_SONAME":
                        soname = getattr(tag, "soname", None)
                    elif tname == "DT_RPATH":
                        rpath = getattr(tag, "rpath", None)
                    elif tname == "DT_RUNPATH":
                        runpath = getattr(tag, "runpath", None)
                    elif tname == "DT_FLAGS":
                        flags_val = int(tag.entry.d_val)
                    elif tname == "DT_FLAGS_1":
                        flags_1_val = int(tag.entry.d_val)
                    elif tname == "DT_INIT_ARRAYSZ":
                        # pointer size = 4 (ELF32) or 8 (ELF64)
                        init_arr = int(tag.entry.d_val) // (8 if elf.elfclass == 64 else 4)
                    elif tname == "DT_FINI_ARRAYSZ":
                        fini_arr = int(tag.entry.d_val) // (8 if elf.elfclass == 64 else 4)
            except Exception:
                pass
            facts.update({
                "needed": needed,
                "soname": soname,
                "rpath": rpath,
                "runpath": runpath,
                "bind_now": bool(flags_val & _DF_BIND_NOW) or bool(flags_1_val & _DF_1_NOW),
                # PIE 判斷：DT_FLAGS_1 的 DF_1_PIE 是最可靠來源（ET_DYN 也可能是 .so）
                "is_pie": bool(flags_1_val & _DF_1_PIE),
                "init_array_count": init_arr,
                "fini_array_count": fini_arr,
            })

        # ===== Symbols（分 .symtab / .dynsym） + 計算 imports/exports =====
        sym_tables: Dict[str, List[Dict]] = {}
        for sec in elf.iter_sections():
            if not isinstance(sec, _PySymTab):
                continue
            syms: List[Dict] = []
            for sym in sec.iter_symbols():
                shndx = sym["st_shndx"]
                # pyelftools 對 shndx 有時回 str（'SHN_UNDEF'）有時回 int，要兩種都接
                if isinstance(shndx, int):
                    if shndx == 0:
                        ndx_str = "UND"
                    elif shndx == 0xfff1:
                        ndx_str = "ABS"
                    elif shndx == 0xfff2:
                        ndx_str = "COM"
                    else:
                        ndx_str = str(shndx)
                elif shndx == "SHN_UNDEF":
                    ndx_str = "UND"
                elif shndx == "SHN_ABS":
                    ndx_str = "ABS"
                elif shndx == "SHN_COMMON":
                    ndx_str = "COM"
                else:
                    ndx_str = str(shndx)
                syms.append({
                    "value": int(sym["st_value"]),
                    "size": int(sym["st_size"]),
                    "type": _py_strip_enum(sym["st_info"]["type"], "STT_"),
                    "bind": _py_strip_enum(sym["st_info"]["bind"], "STB_"),
                    "ndx": ndx_str,
                    "name": sym.name or "",
                })
            sym_tables[sec.name] = syms

        # imports = .dynsym 裡 UND 的 FUNC/OBJECT；exports = 已定義的 GLOBAL/WEAK
        dynsym_syms = sym_tables.get(".dynsym", [])
        imports = [s["name"] for s in dynsym_syms
                   if s["ndx"] == "UND" and s["type"] in ("FUNC", "OBJECT", "NOTYPE")
                   and s["name"]]
        exports = [s for s in dynsym_syms
                   if s["ndx"] not in ("UND", "ABS")
                   and s["bind"] in ("GLOBAL", "WEAK")
                   and s["type"] in ("FUNC", "OBJECT")]
        facts["dynsym_imports_count"] = len(imports)
        facts["dynsym_exports_count"] = len(exports)

        # ===== Key Facts header（必須先 emit，hard cap 截斷時最先保留） =====
        body.extend(_format_key_facts(facts))

        # ===== ELF Header（完整版） =====
        body.append("")
        body.append("【ELF Header】")
        body.append(f"  Class: ELF{elf.elfclass}")
        body.append(f"  Data: {'little endian' if elf.little_endian else 'big endian'}")
        body.append(f"  OS/ABI: {osabi_str}")
        body.append(f"  Type: {type_str}")
        body.append(f"  Machine: {machine_str}")
        body.append(f"  Entry point address: 0x{entry_addr:08x}")
        body.append(f"  Flags: 0x{int(elf['e_flags']):x}")
        body.append(f"  Number of program headers: {elf.num_segments()}")
        body.append(f"  Number of section headers: {elf.num_sections()}")
        if build_id:
            body.append(f"  GNU build-id: {build_id}")

        # ===== Program Headers =====
        if elf.num_segments() > 0:
            body.append("")
            body.append("【Program Headers】")
            for i, seg in enumerate(elf.iter_segments()):
                if i >= 10:
                    body.append(f"  ... (共 {elf.num_segments()} 個)")
                    break
                ptype = _py_strip_enum(seg["p_type"], "PT_")
                body.append(
                    f"  {ptype:<14} offset=0x{int(seg['p_offset']):06x} "
                    f"vaddr=0x{int(seg['p_vaddr']):08x} "
                    f"filesz=0x{int(seg['p_filesz']):x} memsz=0x{int(seg['p_memsz']):x} "
                    f"flags=0x{int(seg['p_flags']):x}"
                )

        # ===== .dynamic 詳細區塊（NEEDED / SONAME / RPATH / RUNPATH） =====
        body.extend(_format_dynamic_section(facts))

        # ===== Sections =====
        important_names = {
            ".vectors", ".text", ".rodata", ".data", ".bss",
            ".init", ".fini", ".comment", ".symtab", ".dynsym",
            ".strtab", ".shstrtab", ".plt", ".got", ".got.plt",
            ".eh_frame", ".dynamic", ".interp", ".modinfo",
        }
        picked = [
            sec for sec in sections
            if sec["name"] in important_names
            or sec["name"].startswith(".debug")
            or sec["name"].startswith(".note")
        ]
        picked = sorted(picked, key=lambda s: s["idx"])[:max_sections]

        if picked:
            body.append("")
            body.append(f"【Sections】({len(picked)}/{len(sections)} 個)")
            body.append("  [idx] name              type       addr       offset   size")
            for sec in picked:
                body.append(
                    f"  [{sec['idx']:2d}] {sec['name']:<17} {sec['type']:<10} "
                    f"0x{sec['addr']:08x} 0x{sec['offset']:06x} 0x{sec['size']:06x}"
                )

        # ===== .modinfo（kernel module 才有） =====
        if modinfo:
            body.extend(_format_modinfo(modinfo))

        # ===== Entry point 對應的 section + 反組譯 =====
        # REL（.o/.ko）的 e_entry 通常是 0、由 kernel/linker 後續決定，不要硬報。
        # 同時 sections[0] 是 SHN_UNDEF（addr=0 size=0 名字空），用 max(1,size) 會誤命中——
        # 過濾空名 section 規避。
        if type_str != "REL":
            for sec in sections:
                if not sec["name"]:
                    continue
                sec_end = sec["addr"] + max(1, sec["size"])
                if sec["addr"] <= entry_addr < sec_end:
                    file_off = sec["offset"] + (entry_addr - sec["addr"])
                    body.append("")
                    body.append(
                        f"Entry point 0x{entry_addr:08x} 位於 section {sec['name']} "
                        f"(file offset ≈ 0x{file_off:x})"
                    )
                    break

            disasm = _disasm_entry(filepath, entry_addr, machine=machine_str)
            if disasm:
                body.append("")
                body.append("【Entry 反組譯（前幾條指令）】")
                body.append(disasm)

        # ===== .comment（編譯器資訊） =====
        comment_sec = elf.get_section_by_name(".comment")
        if comment_sec is not None:
            try:
                raw = comment_sec.data()
                # .comment 是多個 null-terminated 字串串接
                parts = [p.decode("utf-8", errors="replace").strip()
                         for p in raw.split(b"\x00") if p.strip()]
                if parts:
                    body.append("")
                    body.append("【.comment（編譯器資訊）】")
                    for line in parts[:10]:
                        body.append(f"  {line}")
            except Exception:
                pass

        # ===== Symbols（已定義 FUNC/OBJECT，分 .symtab / .dynsym） =====
        preferred_order = [".symtab", ".dynsym"]
        ordered_keys = (
            [k for k in preferred_order if k in sym_tables] +
            [k for k in sym_tables.keys() if k not in preferred_order]
        )
        for tname in ordered_keys:
            body.extend(_format_symbol_table(
                sym_tables[tname], tname, max_funcs=max_funcs, max_objs=max_objs
            ))

        # ===== Imports（.dynsym UND，按 API 家族分類） =====
        # 對 stripped binary 而言：自己定義的函式名字看不到，imports 反而是
        # 推斷「這檔在做什麼」的最佳線索（呼叫了 socket？exec？crypto？）。
        body.extend(_format_imports(imports))

        # ===== DWARF compilation units（pyelftools 才有，readelf 路徑沒這個） =====
        if facts["has_dwarf"]:
            try:
                dwarf = elf.get_dwarf_info()
                cu_files: List[Tuple[str, str]] = []  # (comp_dir, file_name)
                for cu in dwarf.iter_CUs():
                    top = cu.get_top_DIE()
                    name_attr = top.attributes.get("DW_AT_name")
                    dir_attr = top.attributes.get("DW_AT_comp_dir")

                    def _decode(attr):
                        if attr is None:
                            return ""
                        v = attr.value
                        if isinstance(v, bytes):
                            return v.decode("utf-8", errors="replace")
                        return str(v)

                    fname = _decode(name_attr)
                    fdir = _decode(dir_attr)
                    if fname:
                        cu_files.append((fdir, fname))

                if cu_files:
                    body.append("")
                    body.append(f"【DWARF 編譯單元】({min(20, len(cu_files))}/{len(cu_files)} 個來源檔)")
                    for fdir, fname in cu_files[:20]:
                        # DWARF DW_AT_name 可能是絕對或相對；只有相對時才接 comp_dir
                        if fname.startswith("/") or not fdir:
                            body.append(f"  {fname}")
                        else:
                            body.append(f"  {fdir.rstrip('/')}/{fname}")
                    if len(cu_files) > 20:
                        body.append(f"  ... (還有 {len(cu_files) - 20} 個)")
            except Exception as e:
                body.append(f"[WARN] DWARF 解析失敗: {type(e).__name__}: {e}")

        report = header_lines + body

    # ===== 字串掃描（ASCII + UTF-16LE，與 readelf 路徑共用 helper） =====
    seen_strings: set = set()
    ascii_strings = _scan_ascii_strings(filepath, min_len=6, max_bytes=None)
    high_ascii = _collect_high_priority_strings(ascii_strings, seen_strings)

    utf16_strings = _scan_utf16le_strings(filepath, min_len=6)
    high_utf16 = _collect_high_priority_strings(utf16_strings, seen_strings)

    if high_ascii:
        high_ascii.sort(key=lambda x: x[0])
        report.append("")
        report.append(f"【高優先字串 ASCII】({min(max_strings, len(high_ascii))}/{len(high_ascii)} 個)")
        report.append(_format_strings_with_offset(high_ascii, limit=max_strings))

    if high_utf16:
        high_utf16.sort(key=lambda x: x[0])
        utf16_limit = max(10, max_strings // 3)
        report.append("")
        report.append(f"【高優先字串 UTF-16LE】({min(utf16_limit, len(high_utf16))}/{len(high_utf16)} 個)")
        report.append(_format_strings_with_offset(high_utf16, limit=utf16_limit))

    return "\n".join(report)


# ============================================================================
# 公開 API
# ============================================================================

def ocr_image(path: str) -> str:
    """對圖片進行視覺分析與 OCR。"""
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

    cache_key = _cache_key(p, extra=("vision-v2",))
    cached = _cache_get(_OCR_CACHE, cache_key)
    if cached is not None:
        return cached

    try:
        with open(p, "rb") as f:
            data = base64.b64encode(f.read()).decode()

        resp_data = llama_client.native_completion(
            base_url=LLAMA_VL_BASE_URL,
            prompt=(
                "請完整分析這張圖片，使用繁體中文輸出。\n"
                "1. 先用 3-6 句描述主要畫面、物件、人物/角色、背景與風格。\n"
                "2. 若圖片中有文字或數字，另列「可見文字」並保持原文。\n"
                "3. 若是 UI、終端機、錯誤截圖、表格或圖表，請整理關鍵資訊與可能含義。\n"
                "4. 若幾乎沒有文字，也不要只回答空白；仍要描述視覺內容。"
            ),
            temperature=0.1,
            stream=False,
            image_data=[{"id": 10, "data": data}],
            timeout=120,
        )
        result = (resp_data.get("content") or resp_data.get("response") or "").strip()
        _cache_set(_OCR_CACHE, cache_key, result, _OCR_CACHE_MAX)
        return result
    except Exception as e:
        return f"[OCR 錯誤] {type(e).__name__}: {e}"


def read_elf(path: str, max_sections: Optional[int] = None,
              max_funcs: Optional[int] = None, max_objs: Optional[int] = None,
              max_strings: Optional[int] = None) -> str:
    """讀取 ELF 檔案並產生分析報告

    Args:
        path: ELF 檔案路徑
        max_sections: 最多顯示的 section 數量（None → BIN_ELF_MAX_SECTIONS）
        max_funcs: 最多顯示的 function 數量（None → BIN_ELF_MAX_FUNCS）
        max_objs: 最多顯示的 object 數量（None → BIN_ELF_MAX_OBJS）
        max_strings: 最多顯示的高優先字串數量（None → BIN_ELF_MAX_STRINGS）

    None 預設值會 fall through 到 config 設定，確保 `elf:` 與 `bin:` 走同一個 ELF
    時報告大小一致（_build_elf_report 在 bin: 路徑會以 config 為預設）。
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

    # 先把 None 換成 config 預設，再算 cache key——這樣 read_elf(p) 跟
    # _build_elf_report(p)（bin: 路徑用）會打到同一個 cache entry。
    max_sections = max_sections or BIN_ELF_MAX_SECTIONS
    max_funcs = max_funcs or BIN_ELF_MAX_FUNCS
    max_objs = max_objs or BIN_ELF_MAX_OBJS
    max_strings = max_strings or BIN_ELF_MAX_STRINGS

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

        # Hard cap：報告已按重要度由前往後排（檔名/大小/Magic/Hex dump 在最前），
        # 直接前綴切片即可保護關鍵資訊。
        full_report = _truncate_elf_report("\n".join(report))
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
