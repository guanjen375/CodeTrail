#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ELF 分析：backend 中立的模型 + 多視角報告（analyze_file / ingest_document 共用）。

設計：
- `ElfModel`：pyelftools（結構化）與 readelf（文字）兩個 backend 都填同一份資料
  結構，報告只從模型渲染，所以兩條路徑的章節與欄位一致。backend 拿不到的能力寫進
  `model.missing`，報告開頭明列——缺 pyelftools 不再是「只標 parser 名字」的靜默降級。
- 視角（view）：summary / headers / sections / memmap / symbols / imports / relocs /
  dynamic / dwarf / disasm / strings。`target` 指定要展開的 symbol / 位址 / section /
  regex / 篩選條件，`limit` 控制筆數；輸出一律套 hard cap，截斷訊息指出該用哪個
  view + target 縮小範圍，而不是默默砍掉。
- 反組譯：objdump（含跨架構變體、`AICODE_OBJDUMP` 覆寫）→ capstone → 失敗時把每個
  嘗試過的工具與原因、以及可行的補救方式明文寫進報告。

這個模組不做 sandbox：路徑安全由 media._safe_path / mcp_server.analyze_file 負責。
"""
from __future__ import annotations

import bisect
import heapq
import itertools
import os
import re
import shutil
import struct
import subprocess
import time
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import (
    BIN_ELF_REPORT_MAX_CHARS, BIN_ELF_MAX_SECTIONS, BIN_ELF_MAX_FUNCS,
    BIN_ELF_MAX_OBJS, BIN_ELF_MAX_STRINGS, BIN_ELF_INGEST_MAX_CHARS,
    BIN_ELF_VIEW_MAX_LIMIT,
)

# pyelftools 為結構化 backend；缺席時退回 readelf 文字解析（報告會明列缺失能力）。
try:
    import elftools as _elftools  # type: ignore
    from elftools.elf.elffile import ELFFile as _PyELFFile  # type: ignore
    from elftools.elf.sections import SymbolTableSection as _PySymTab  # type: ignore
    from elftools.elf.sections import NoteSection as _PyNoteSection  # type: ignore
    from elftools.elf.dynamic import DynamicSection as _PyDynamic  # type: ignore
    from elftools.elf.relocation import RelocationSection as _PyReloc  # type: ignore
    from elftools.elf import descriptions as _pydesc  # type: ignore
    _HAS_PYELFTOOLS = True
    _PYELFTOOLS_VERSION = str(getattr(_elftools, "__version__", "?"))
except ImportError:  # pragma: no cover - 依環境而定
    _PyELFFile = None
    _PySymTab = None
    _PyNoteSection = None
    _PyDynamic = None
    _PyReloc = None
    _pydesc = None
    _HAS_PYELFTOOLS = False
    _PYELFTOOLS_VERSION = ""


VIEWS: Tuple[str, ...] = (
    "summary", "headers", "sections", "memmap", "symbols", "imports",
    "relocs", "dynamic", "dwarf", "disasm", "strings",
)

VIEW_HELP: Dict[str, str] = {
    "summary": "總覽（預設）：Key Facts、header、記憶體配置、sections、symbols、imports、relocs、DWARF、字串分類",
    "headers": "ELF header、全部 program headers、notes、.comment、.modinfo",
    "sections": "全部 section（含 flags / 所屬 segment）；target=section 名或 0x位址 → 該 section 的 hex dump + 字串 + symbols",
    "memmap": "LOAD segment 的 LMA/VMA、section→segment、FLASH/RAM/.bss 估算、Cortex-M 向量表解讀（limit = IRQ 向量數，上限 496）",
    "symbols": "完整 symbol 表（含 LOCAL / UND / size=0）；target=regex、'bind:LOCAL type:FUNC uart' 這類篩選、或 0x位址（查包含該位址的 symbol）",
    "imports": "未定義（外部）symbol 依 API 家族分類（.dynsym UND；.o/.ko 用 .symtab UND）與 relocation 引用次數",
    "relocs": "relocation：各 section 統計、被引用最多的 symbol；target=regex 列出項目與 caller（.o/.ko 的呼叫關係證據）；target=\"*\" 全部逐筆（含沒有 symbol 的 RELATIVE）",
    "dynamic": ".dynamic 全部 tag（NEEDED / SONAME / RPATH / RUNPATH / FLAGS / INIT_ARRAY…）",
    "dwarf": "DWARF：無 target → CU 列表與統計；target=regex → 函式（low/high pc、來源檔:行）與型別（struct/union/enum 成員）；target=0x位址 → 對應來源行與函式",
    "disasm": "反組譯：target=symbol / 0x位址 / 0x起-0x迄（省略 = entry point）；limit=指令數；.o/.ko 可加 'section:.init.text'",
    "strings": "全部可讀字串（ASCII + UTF-16LE，含 offset / section / 分類）；target=regex 或 'cat:diagnostic' / 'section:.rodata' / 'min:12'",
}

# 每個 view 的預設筆數（limit=0 時）與上限；上限統一由 config 控制。
_VIEW_DEFAULT_LIMIT: Dict[str, int] = {
    "sections": 4000, "memmap": 48, "symbols": 200, "imports": 60, "relocs": 200,
    "dynamic": 400, "dwarf": 200, "disasm": 48, "strings": 200,
}
_DISASM_MAX_INSTR = 1000
_SECTION_DUMP_DEFAULT = 512
_SECTION_DUMP_MAX = 4096         # 必須 ≤ BIN_ELF_VIEW_MAX_LIMIT（analyze_file 的 limit schema 上限）
_RELOC_ENTRY_CAP = 20000      # 每個 relocation section 保留的項目數（統計仍算全部）
_STRINGS_CAP = 100000         # 字串掃描上限（每條 ~200 B 記憶體；超過會在報告註明）
_DWARF_FUNC_CAP = 30000
_DWARF_TYPE_CAP = 2000         # dwarf_types 的絕對上限（不管 limit 給多大）
_DWARF_TYPE_MEMBERS_CAP = 256  # 每個型別保留的成員數
_VECTOR_MAX_ENTRIES = 512      # Cortex-M 向量表：16 個核心例外 + 最多 496 個 IRQ
_STRING_PREVIEW_CHARS = 120
_REGEX_MAX_LEN = 200             # target regex 長度上限（更長就當字面）
_REGEX_SUBJECT_MAX = 300         # regex 只看每個字串 / 名稱的前 N 個字元（單次比對的回溯上界）
_REGEX_MAX_BRANCHES = 8          # 最上層 | 的分支數上限
_REGEX_MAX_UNBOUNDED = 1         # * / + 合計上限
_REGEX_MAX_OPTIONAL = 3          # ? 合計上限
_SINK_NOTE_MAX = 200             # 截斷說明的長度上限（截斷時才從尾端回收這段空間）
_FILTER_TIME_BUDGET = 20.0       # 單一 view 的篩選時間預算（秒），超過就截斷並明講
_INGEST_CHARS_PER_LINE = 1       # ingest 筆數保險 = 字元預算：每行至少 1 字元 + 換行，字元預算一定先到（imports 短名稱也是）
_INGEST_MIN_LINES = 200
# 各 view 一行至少幾個字元：筆數上限 = 字元預算 // 最短行長 + 1，永遠不會比字元預算先到，
# 但也不會讓 heap / 候選清單長到遠超過預算能放的量（symbols 的 nsmallest、strings 的 picked）。
_MIN_LINE_CHARS: Dict[str, int] = {
    # 每個值都 ≤ 該 view 真的能印出的最短一行（test_min_line_chars_never_exceed_the_shortest_possible_line）：
    # relocs 最短 `  X+0x0  T  (none)` = 18；dwarf 型別區塊有 `  enum X` = 8 與只有 6 個空白開頭的值列。
    "symbols": 40, "relocs": 16, "strings": 16, "dwarf": 6, "imports": 5,
    "sections": 50, "dynamic": 6, "memmap": 30, "disasm": 12,
}


# ---------------------------------------------------------------------------
# 命令執行
# ---------------------------------------------------------------------------

def cmd_exists(cmd: str) -> bool:
    """命令是否可執行（PATH 內名稱，或含路徑的可執行檔）。"""
    if not cmd:
        return False
    if os.sep in cmd:
        return os.path.isfile(cmd) and os.access(cmd, os.X_OK)
    return shutil.which(cmd) is not None


def _subprocess_env() -> Dict[str, str]:
    """readelf / objdump / c++filt 的輸出是用文字 regex 解析的：固定 C locale，避免翻譯過的欄位名。"""
    env = dict(os.environ)
    env.update({"LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"})
    return env


def run_cmd(cmd: List[str], timeout: int = 30) -> Tuple[Optional[str], Optional[str]]:
    """執行命令並回傳 (stdout, error_msg)；非零 returncode 視為失敗。"""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", env=_subprocess_env(),
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


def _run_capture(cmd: List[str], timeout: int = 30) -> Tuple[Optional[int], str, str]:
    """回傳 (returncode, stdout, stderr)；returncode None 表示根本沒跑起來（原因放 stderr）。

    objdump 遇到不支援的架構會印 `can't disassemble for architecture UNKNOWN!` 但
    returncode 仍是 0，所以反組譯層不能只看 returncode。
    """
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", env=_subprocess_env(),
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except FileNotFoundError:
        return None, "", "command_not_found"
    except subprocess.TimeoutExpired:
        return None, "", f"timeout ({timeout}s)"
    except Exception as e:
        return None, "", f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# 架構名稱正規化（pyelftools enum / readelf 描述 → 統一 key）
# ---------------------------------------------------------------------------

# 順序有意義："aarch64" / "sparc" / "loongarch" 都含 "arc"，要排在 "arc" 前面。
_MACHINE_TABLE: List[Tuple[str, str, str]] = [
    ("x86_64", "x86_64", "x86-64"), ("x86-64", "x86_64", "x86-64"), ("amd64", "x86_64", "x86-64"),
    ("aarch64", "aarch64", "AArch64"),
    ("386", "i386", "x86 (i386)"),
    ("sparc", "sparc", "SPARC"),
    ("loongarch", "loongarch", "LoongArch"),
    ("arc", "arc", "ARC (ARCompact/ARCv2)"),
    ("arm", "arm", "ARM (32-bit)"),
    ("risc", "riscv", "RISC-V"),
    ("mips", "mips", "MIPS"),
    ("ppc64", "ppc64", "PowerPC64"), ("powerpc64", "ppc64", "PowerPC64"),
    ("ppc", "ppc", "PowerPC"), ("powerpc", "ppc", "PowerPC"),
    ("xtensa", "xtensa", "Xtensa"),
    ("68k", "m68k", "m68k"), ("68000", "m68k", "m68k"),
    ("avr", "avr", "AVR"),
    ("msp430", "msp430", "MSP430"),
    ("s390", "s390", "s390"),
    ("microblaze", "microblaze", "MicroBlaze"),
    ("nios", "nios2", "Nios II"), ("altera", "nios2", "Nios II"),
    ("blackfin", "blackfin", "Blackfin"),
    ("csky", "csky", "C-SKY"), ("c-sky", "csky", "C-SKY"),
    ("tricore", "tricore", "TriCore"),
    ("superh", "sh", "SuperH"), ("em_sh", "sh", "SuperH"),
    ("hexagon", "hexagon", "Hexagon"),
    ("bpf", "bpf", "BPF"),
    ("cris", "cris", "CRIS"),
]

# 固定指令寬度（bytes）的架構：反組譯位址範圍用 limit × 寬度估算；其餘用 8。
_FIXED_INSTR_WIDTH: Dict[str, int] = {
    "arm": 4, "aarch64": 4, "riscv": 4, "mips": 4, "ppc": 4, "ppc64": 4,
    "sparc": 4, "arc": 4, "xtensa": 3, "avr": 2, "msp430": 2, "loongarch": 4,
    "microblaze": 4, "nios2": 4, "csky": 4,
}

# 各架構常見的跨編譯 objdump 名稱（Ubuntu binutils-<triplet> / 常見 SDK）。
_OBJDUMP_CANDIDATES: Dict[str, List[str]] = {
    "x86_64": ["x86_64-linux-gnu-objdump"],
    "i386": ["i686-linux-gnu-objdump", "x86_64-linux-gnu-objdump"],
    "arm": ["arm-none-eabi-objdump", "arm-linux-gnueabihf-objdump", "arm-linux-gnueabi-objdump",
            "armv7-linux-gnueabihf-objdump"],
    "aarch64": ["aarch64-linux-gnu-objdump", "aarch64-none-elf-objdump", "aarch64-none-linux-gnu-objdump"],
    "riscv": ["riscv64-unknown-elf-objdump", "riscv32-unknown-elf-objdump", "riscv-none-elf-objdump",
              "riscv64-linux-gnu-objdump", "riscv-none-embed-objdump", "riscv64-unknown-linux-gnu-objdump"],
    "mips": ["mips-linux-gnu-objdump", "mipsel-linux-gnu-objdump", "mips64-linux-gnuabi64-objdump",
             "mips64el-linux-gnuabi64-objdump"],
    "ppc": ["powerpc-linux-gnu-objdump", "powerpc-eabi-objdump"],
    "ppc64": ["powerpc64le-linux-gnu-objdump", "powerpc64-linux-gnu-objdump"],
    "xtensa": ["xtensa-esp32-elf-objdump", "xtensa-esp-elf-objdump", "xtensa-lx106-elf-objdump"],
    "arc": ["arc-elf32-objdump", "arc-linux-gnu-objdump", "arc-snps-linux-gnu-objdump", "arc64-elf-objdump"],
    "m68k": ["m68k-linux-gnu-objdump", "m68k-elf-objdump"],
    "sh": ["sh-elf-objdump", "sh4-linux-gnu-objdump"],
    "sparc": ["sparc64-linux-gnu-objdump", "sparc-elf-objdump"],
    "avr": ["avr-objdump"],
    "msp430": ["msp430-elf-objdump"],
    "loongarch": ["loongarch64-linux-gnu-objdump"],
    "s390": ["s390x-linux-gnu-objdump"],
    "microblaze": ["microblaze-xilinx-elf-objdump", "microblazeel-xilinx-elf-objdump"],
    "nios2": ["nios2-elf-objdump"],
    "csky": ["csky-elf-objdump"],
}

_DISASM_PACKAGE_HINT: Dict[str, str] = {
    "arm": "binutils-arm-none-eabi（arm-none-eabi-objdump）或 binutils-arm-linux-gnueabihf",
    "aarch64": "binutils-aarch64-linux-gnu",
    "riscv": "binutils-riscv64-linux-gnu 或 riscv64-unknown-elf 工具鏈",
    "mips": "binutils-mips-linux-gnu / binutils-mipsel-linux-gnu",
    "ppc": "binutils-powerpc-linux-gnu",
    "ppc64": "binutils-powerpc64le-linux-gnu",
    "xtensa": "ESP-IDF 工具鏈的 xtensa-esp32-elf-objdump",
    "arc": "Synopsys ARC GNU toolchain 的 arc-elf32-objdump（capstone 不支援 ARC）",
    "x86_64": "binutils（objdump）",
    "i386": "binutils（objdump）",
}


def canonical_machine(raw: object) -> Tuple[str, str]:
    """把 e_machine（pyelftools enum 字串 / int / readelf 描述）對到統一 key 與顯示名。"""
    low = str(raw).lower()
    for sub, key, disp in _MACHINE_TABLE:
        if sub in low:
            return key, disp
    return "unknown", str(raw)


def _strip_enum(value: object, prefix: str) -> str:
    """把 pyelftools 的字串 enum（如 'EM_X86_64'）去掉前綴；int / 未知型別就 str()。"""
    s = str(value)
    return s[len(prefix):] if s.startswith(prefix) else s


def _decode(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return str(v)


# ---------------------------------------------------------------------------
# 字串掃描與分類（read_binary 也共用）
# ---------------------------------------------------------------------------

_PRINTABLE_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    " .,_-/:;()[]{}+=@#%$'\"\\|<>!?*&^~`"
)


def scan_ascii_strings(
    filepath: Path,
    min_len: int = 6,
    max_bytes: Optional[int] = None,
) -> List[Tuple[int, str]]:
    """純 Python 掃描 ASCII 可讀字串（含 file offset），不依賴外部 strings 命令。"""
    results: List[Tuple[int, str]] = []

    with open(filepath, "rb") as f:
        file_offset = 0
        current_chars: List[int] = []
        string_start: Optional[int] = None
        bytes_read = 0

        while True:
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
                if 32 <= byte < 127:
                    if string_start is None:
                        string_start = file_offset + i
                    current_chars.append(byte)
                else:
                    if string_start is not None and len(current_chars) >= min_len:
                        try:
                            results.append((string_start, bytes(current_chars).decode("ascii")))
                        except UnicodeDecodeError:
                            pass
                    current_chars = []
                    string_start = None

            file_offset += len(chunk)

        if string_start is not None and len(current_chars) >= min_len:
            try:
                results.append((string_start, bytes(current_chars).decode("ascii")))
            except UnicodeDecodeError:
                pass

    return results


def scan_utf16le_strings(
    filepath: Path,
    min_len: int = 6,
    max_bytes: int = 4 * 1024 * 1024,
) -> List[Tuple[int, str]]:
    """掃描 UTF-16LE 印字字串（韌體 UI / Windows resource 常見格式）。預設只掃前 4MB。"""
    results: List[Tuple[int, str]] = []
    with open(filepath, "rb") as f:
        data = f.read(max_bytes)

    i = 0
    n = len(data)
    while i < n - 1:
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


def is_meaningful_string(s: str) -> bool:
    """判斷字串是否有意義（非純數字/符號雜訊）。"""
    s = s.strip()
    if len(s) < 6:
        return False
    if not any(c.isalpha() for c in s):
        return False
    printable_count = sum(1 for c in s if c in _PRINTABLE_CHARS)
    return printable_count / len(s) >= 0.7


def format_strings_with_offset(
    items: List[Tuple[int, str]],
    limit: int,
    max_str_len: int = 200,
) -> str:
    """格式化字串列表（含 offset）。"""
    lines: List[str] = []
    for offset, s in items[:limit]:
        s = s.strip()
        if len(s) > max_str_len:
            s = s[:max_str_len] + "…"
        lines.append(f"  0x{offset:08x}: {s}")
    if len(items) > limit:
        lines.append(f"  ... (還有 {len(items) - limit} 個)")
    return "\n".join(lines)


STRING_CATEGORIES: Tuple[str, ...] = (
    "version", "diagnostic", "format", "url", "path", "command", "config",
)
_STRING_CATEGORY_LABEL: Dict[str, str] = {
    "version": "version / 編譯資訊",
    "diagnostic": "diagnostic（錯誤 / assert / log）",
    "format": "format（printf 格式字串）",
    "url": "url",
    "path": "path（檔案 / 裝置路徑）",
    "command": "command（shell 命令）",
    "config": "config（設定鍵 / 檔名）",
}
_VERSION_KEYWORDS = ["version", "compiled", "gcc", "clang", "llvm", "build", "built",
                     "u-boot", "linux", "kernel", "firmware"]
_YEAR_RE = re.compile(r"\b20(1[5-9]|2\d)\b")
_CATEGORY_RES: Dict[str, "re.Pattern[str]"] = {
    "diagnostic": re.compile(
        r"\b(error|err|fail|failed|failure|assert|assertion|panic|fatal|warn|warning|invalid|"
        r"timeout|timed out|cannot|can't|unable|not found|denied|overflow|underflow|corrupt|"
        r"corrupted|bad|unexpected|exception|abort|aborted|oops|bug|illegal|unsupported|"
        r"mismatch|refused|unreachable|too (?:large|small|many|long)|out of (?:range|memory|bounds))\b",
        re.I,
    ),
    "format": re.compile(r"%[-+ #0]*(?:\*|\d+)?(?:\.(?:\*|\d+))?(?:hh|h|ll|l|z|j|t|L|q)?[diouxXeEfFgGaAcspn]"),
    "url": re.compile(
        r"\b(?:https?|ftp|ftps|sftp|ssh|tftp|mqtt|mqtts|ws|wss|coap|coaps|rtsp|smb|nfs|ldap|git)://"
        r"|\bwww\.[a-z0-9-]+\.[a-z]{2,}",
        re.I,
    ),
    "path": re.compile(
        r"(?:^|[\s\"'(=:,<])/(?:dev|etc|proc|sys|usr|var|tmp|bin|sbin|lib|lib64|opt|home|mnt|"
        r"media|data|boot|run|root|srv|overlay|www|firmware|lib/modules|lib/firmware)(?:/|\b)"
        r"|(?:^|\s)(?:/[\w.+-]+){2,}(?:\s|$)"
        r"|\b[A-Za-z]:\\[\w\\.-]+",
    ),
    "command": re.compile(
        r"(?:^|[;&|`(]\s*)(?:sh|bash|ash|busybox|mount|umount|insmod|modprobe|rmmod|ifconfig|ip|"
        r"route|iptables|ip6tables|reboot|halt|poweroff|kill|killall|chmod|chown|cp|mv|rm|mkdir|"
        r"cat|echo|wget|curl|tftp|ftpget|ftpput|nc|netcat|telnetd|telnet|dropbear|sshd|ssh|"
        r"udhcpc|udhcpd|dhcpcd|ntpd|ntpdate|hostapd|wpa_supplicant|iwconfig|iwpriv|nvram|"
        r"fw_setenv|fw_printenv|flash_erase|flashcp|nandwrite|dd|tar|gzip|gunzip|unzip|sysctl|"
        r"export|eval|exec|chroot|mkfs\.\w+|mdev|udevd|start-stop-daemon)"
        r"(?:\s+-?[\w./=$\"'-]|\s*$)"
        r"|\bsh -c\b|\b/bin/(?:sh|bash|busybox)\b",
    ),
    "config": re.compile(
        r"^\s*[A-Za-z_][A-Za-z0-9_.-]{1,40}\s*=\s*\S"
        r"|\bCONFIG_[A-Z0-9_]{2,}"
        r"|\.(?:conf|cfg|ini|json|ya?ml|xml|dtb|dts|toml|properties)\b"
        r"|^\s*\[[A-Za-z0-9_ .-]+\]\s*$",
        re.I,
    ),
}


def categorize_string(s: str) -> Tuple[str, ...]:
    """回傳字串命中的分類（可多重命中；空 tuple = other）。"""
    cats: List[str] = []
    low = s.lower()
    if any(kw in low for kw in _VERSION_KEYWORDS) or _YEAR_RE.search(s):
        cats.append("version")
    for name, rx in _CATEGORY_RES.items():
        if rx.search(s):
            cats.append(name)
    return tuple(cats)


def collect_high_priority_strings(
    items: List[Tuple[int, str]],
    seen: set,
) -> List[Tuple[int, str]]:
    """從 (offset, string) 清單裡挑出版本/編譯相關高優先字串（跨呼叫用 seen 去重）。"""
    out: List[Tuple[int, str]] = []
    for offset, s in items:
        s = s.strip()
        if not s or not is_meaningful_string(s):
            continue
        s_lower = s.lower()
        is_high = any(kw in s_lower for kw in _VERSION_KEYWORDS) or bool(_YEAR_RE.search(s))
        if is_high and s not in seen:
            seen.add(s)
            out.append((offset, s))
    return _finish(out)


# ---------------------------------------------------------------------------
# .modinfo / .comment
# ---------------------------------------------------------------------------

def parse_modinfo(data: bytes) -> Dict[str, List[str]]:
    """解析 .modinfo（Linux kernel module 元資料）：多個 null-terminated key=value。"""
    result: Dict[str, List[str]] = {}
    for raw in data.split(b"\x00"):
        if not raw:
            continue
        s = raw.decode("utf-8", errors="replace")
        if "=" not in s:
            continue
        key, _, val = s.partition("=")
        result.setdefault(key.strip(), []).append(val.strip())
    return result


def parse_comment(data: bytes) -> List[str]:
    """.comment 是多個 null-terminated 字串串接（編譯器資訊）。"""
    return [p.decode("utf-8", errors="replace").strip() for p in data.split(b"\x00") if p.strip()]


def format_modinfo(modinfo: Dict[str, List[str]]) -> List[str]:
    """把 .modinfo 字典格式化成報告行；parm/parmtype 很冗，只給計數。"""
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
            tail = f" ... +{len(vals) - 5}" if len(vals) > 5 else ""
            out.append(f"  {key} ({len(vals)}): {preview}{tail}")
    other = [k for k in modinfo if k not in shown and k not in ("parm", "parmtype")]
    if other:
        out.append(f"  其他鍵: {', '.join(other[:10])}" +
                   (f" ... +{len(other) - 10}" if len(other) > 10 else ""))
    if "parm" in modinfo or "parmtype" in modinfo:
        out.append(
            f"  module params: {len(modinfo.get('parm', []))} parm / "
            f"{len(modinfo.get('parmtype', []))} parmtype"
        )
    return _finish(out)


# ---------------------------------------------------------------------------
# Imports 分類（API 家族）
# ---------------------------------------------------------------------------

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
    ("kernel", re.compile(
        r"^(kmalloc|kzalloc|kfree|vmalloc|vfree|printk|_printk|__printk\w*|dev_\w+|"
        r"register_\w+|unregister_\w+|request_irq|free_irq|ioremap\w*|iounmap|"
        r"mutex_\w+|spin_\w+|_raw_spin_\w+|schedule\w*|wake_up\w*|msleep|udelay|"
        r"copy_(to|from)_user|__copy_\w+|kthread_\w+|queue_\w+|init_\w+|module_\w+|"
        r"__module_\w+|param_\w+|kobject_\w+|sysfs_\w+|device_\w+|class_\w+|"
        r"pci_\w+|usb_\w+|i2c_\w+|spi_\w+|gpio\w*|of_\w+|platform_\w+|dma_\w+|"
        r"blk_\w+|bio_\w+|elv_\w+|kmem_cache_\w+|__kmalloc\w*|kstrdup|kstrtoint|"
        r"seq_\w+|proc_\w+|__fentry__|__x86_\w+|__ubsan_\w+)$"
    )),
]


def categorize_imports(imports: List[str]) -> Dict[str, List[str]]:
    """把 imported symbol list 按 API 家族分桶（第一個匹配的家族即歸屬）。"""
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


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------

class ElfModel:
    """兩個 backend 都填這份結構；報告只從它渲染。

    header   : class / endian / osabi / type / machine(key) / machine_desc / entry / flags / phnum / shnum
    segments : idx / type / offset / vaddr / paddr / filesz / memsz / flags("RWE" 三格) / align / sections
    sections : idx / name / type / addr / offset / size / flags / link / info / align / entsize / nobits
    symtabs  : table name → [{value, size, type, bind, vis, ndx, name}]
    dynamic  : tags[(name, value)] + needed / soname / rpath / runpath / bind_now / is_pie / init_array_count / fini_array_count
    relocs   : [{section, applies_to, rela, count, by_type, entries[{offset, type, sym, addend}]}]
    dwarf    : present / debug_info / sections
    missing  : 這次解析拿不到的能力（人話），報告開頭明列
    failed   : 這次讀取 / 解析失敗的資料類別 → 原因；views 據此把「沒有」改講成「讀不到」
    """

    def __init__(self, path: Path):
        self.path = path
        self.size = path.stat().st_size
        self.parser = ""
        self.parser_detail = ""
        self.missing: List[str] = []
        self.warnings: List[str] = []
        self.header: Dict = {}
        self.segments: List[Dict] = []
        self.sections: List[Dict] = []
        self.symtabs: Dict[str, List[Dict]] = {}
        self.dynamic: Optional[Dict] = None
        self.build_id: Optional[str] = None
        self.note_names: List[str] = []
        self.modinfo: Optional[Dict[str, List[str]]] = None
        self.comment: List[str] = []
        self.relocs: List[Dict] = []
        self.reloc_entry_cap_hit = False
        self.dwarf: Dict = {"present": False, "debug_info": False, "sections": []}
        self.caps: Dict[str, bool] = {}
        # 讀取 / 解析失敗的資料類別 → 原因（"symbols" / "relocs" / "dynamic" / "sections" …）。
        # 有失敗記錄的類別，報告不得用「沒有 X」的敘述帶過（那是靜默錯答）。
        self.failed: Dict[str, str] = {}
        self._lazy: Dict[str, object] = {}

    # --- 便利屬性 ---
    @property
    def is_rel(self) -> bool:
        return self.header.get("type") == "REL"

    @property
    def machine(self) -> str:
        return self.header.get("machine", "unknown")

    @property
    def machine_desc(self) -> str:
        return self.header.get("machine_desc") or self.header.get("machine_raw") or "unknown"

    @property
    def addr_width(self) -> int:
        return 16 if self.header.get("class") == 64 else 8

    @property
    def little_endian(self) -> bool:
        return self.header.get("endian") != "big"

    @property
    def entry(self) -> int:
        return int(self.header.get("entry", 0) or 0)

    def fmt_addr(self, v: int) -> str:
        return f"0x{v:0{self.addr_width}x}"

    def section_by_name(self, name: str) -> Optional[Dict]:
        for sec in self.sections:
            if sec["name"] == name:
                return sec
        return None

    def section_by_index(self, idx: int) -> Optional[Dict]:
        if 0 <= idx < len(self.sections) and self.sections[idx]["idx"] == idx:
            return self.sections[idx]
        for sec in self.sections:
            if sec["idx"] == idx:
                return sec
        return None

    def primary_symtab(self) -> Tuple[str, List[Dict]]:
        """stripped binary 只剩 .dynsym 是 ground truth；有 .symtab 就用 .symtab。"""
        for name in (".symtab", ".dynsym"):
            if self.symtabs.get(name):
                return name, self.symtabs[name]
        for name, syms in self.symtabs.items():
            if syms:
                return name, syms
        return "", []

    def has_symbols(self) -> bool:
        return any(self.symtabs.values())

    def ndx_label(self, ndx: str) -> str:
        """symbol 的 st_shndx → 可讀的 section 名（UND/ABS/COM 原樣）。"""
        if ndx in ("UND", "ABS", "COM"):
            return ndx
        try:
            sec = self.section_by_index(int(ndx))
        except ValueError:
            return ndx
        return sec["name"] if sec and sec["name"] else ndx


# ---------------------------------------------------------------------------
# readelf backend（文字解析 fallback）
# ---------------------------------------------------------------------------

_RE_HDR_FIELD = re.compile(r"^([^:]+):\s+(.*)$")
_RE_PH_ROW = re.compile(
    r"^\s*(\S.*?)\s+0x([0-9a-f]+)\s+0x([0-9a-f]+)\s+0x([0-9a-f]+)\s+0x([0-9a-f]+)\s+0x([0-9a-f]+)"
    r"\s+([RWE ]{1,3}?)\s+0x([0-9a-f]+)\s*$",
    re.I,
)
_RE_SEC_ROW = re.compile(r"^\s*\[\s*(\d+)\]\s*(.*)$")
_RE_SYM_ROW = re.compile(
    r"^\s*(\d+):\s*([0-9a-fA-F]+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*(.*)$"
)
_RE_DYN_ROW = re.compile(r"^\s*0x([0-9a-fA-F]+)\s+\((\w+)\)\s*(.*)$")
_RE_RELOC_HDR = re.compile(r"^Relocation section '([^']+)' at offset 0x[0-9a-fA-F]+ contains (\d+) entr")
_RE_RELOC_ROW = re.compile(r"^\s*([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+(\S+)\s*(.*)$")
_RE_STRDUMP_ROW = re.compile(r"^\s*\[\s*[0-9a-fA-F]+\]\s+(.*)$")
_RE_DIE_HDR = re.compile(r"^\s*<(\d+)><([0-9a-fA-F]+)>:\s*Abbrev Number:\s*\d+\s*\((DW_TAG_\w+)\)")
_RE_DIE_ATTR = re.compile(r"^\s*<[0-9a-fA-F]+>\s+(DW_AT_\w+)\s*:\s*(.*)$")
_RE_CU_VERSION = re.compile(r"^\s*Version:\s*(\d+)")
_RE_DECODED_LINE = re.compile(r"^(\S+)\s+(\d+|-)\s+(0x[0-9a-fA-F]+)")
_RE_2HEX = re.compile(r"^[0-9a-f]{2}$")

# DW_LANG 常見代碼（DWARF 2–5）；其餘顯示 lang#N。
_DW_LANG: Dict[int, str] = {
    0x1: "C89", 0x2: "C", 0x4: "C++", 0x8: "Fortran90", 0xb: "Java", 0xc: "C99",
    0x10: "ObjC", 0x11: "ObjC++", 0x13: "D", 0x14: "Python", 0x16: "Go",
    0x19: "C++03", 0x1a: "C++11", 0x1c: "Rust", 0x1d: "C11", 0x1e: "Swift",
    0x21: "C++14", 0x24: "RenderScript", 0x2a: "C++17", 0x2b: "C++20", 0x2c: "C17",
    0x8001: "Mips_Assembler", 0x8002: "Assembler",
}


def _parse_int(s: str) -> int:
    s = (s or "").strip().split(",")[0].strip()
    if not s:
        return 0
    tok = s.split()[0]
    try:
        return int(tok, 0)
    except ValueError:
        try:
            return int(tok, 16)
        except ValueError:
            return 0


def _clean_readelf_attr(v: str) -> str:
    """去掉 readelf 的 `(indirect string, offset: 0x..): ` 這類前綴。"""
    return re.sub(r"^\([^)]*\):\s*", "", (v or "").strip()).strip()


def _lang_from_readelf(v: str) -> str:
    m = re.search(r"\(([^)]+)\)", v)
    if m:
        return m.group(1).strip()
    n = _parse_int(v)
    return _DW_LANG.get(n, f"lang#{n}")


def _readelf_header(model: ElfModel, txt: str) -> None:
    fields: Dict[str, str] = {}
    for line in txt.splitlines():
        m = _RE_HDR_FIELD.match(line.strip())
        if m:
            fields[m.group(1).strip()] = m.group(2).strip()
    cls = 64 if "64" in fields.get("Class", "") else 32
    endian = "big" if "big" in fields.get("Data", "").lower() else "little"
    type_raw = fields.get("Type", "")
    machine_raw = fields.get("Machine", "")
    key, disp = canonical_machine(machine_raw)
    model.header = {
        "class": cls,
        "endian": endian,
        "osabi": fields.get("OS/ABI", ""),
        "type": type_raw.split()[0] if type_raw else "",
        "type_desc": type_raw,
        "machine": key,
        "machine_desc": machine_raw or disp,
        "machine_raw": machine_raw,
        "entry": _parse_int(fields.get("Entry point address", "0")),
        "flags": _parse_int(fields.get("Flags", "0")),
        "phnum": _parse_int(fields.get("Number of program headers", "0")),
        "shnum": _parse_int(fields.get("Number of section headers", "0")),
    }


def _readelf_segments(model: ElfModel, txt: str) -> None:
    segs: List[Dict] = []
    mapping: Dict[int, List[str]] = {}
    in_ph = in_map = False
    for line in txt.splitlines():
        if "Program Headers:" in line:
            in_ph, in_map = True, False
            continue
        if "Section to Segment mapping" in line:
            in_ph, in_map = False, True
            continue
        if in_ph:
            m = _RE_PH_ROW.match(line)
            if not m:
                continue
            fl = m.group(7).upper()
            flags = ("R" if "R" in fl else "-") + ("W" if "W" in fl else "-") + ("E" if "E" in fl else "-")
            segs.append({
                "idx": len(segs), "type": m.group(1).strip(),
                "offset": int(m.group(2), 16), "vaddr": int(m.group(3), 16),
                "paddr": int(m.group(4), 16), "filesz": int(m.group(5), 16),
                "memsz": int(m.group(6), 16), "flags": flags,
                "align": int(m.group(8), 16), "sections": [],
            })
        elif in_map:
            m = re.match(r"^\s*(\d+)\s*(.*)$", line)
            if m:
                mapping[int(m.group(1))] = m.group(2).split()
    for seg in segs:
        seg["sections"] = mapping.get(seg["idx"], [])
    model.segments = segs


def _readelf_sections(model: ElfModel, txt: str) -> None:
    secs: List[Dict] = []
    for line in txt.splitlines():
        m = _RE_SEC_ROW.match(line)
        if not m:
            continue
        idx = int(m.group(1))
        toks = m.group(2).split()
        if len(toks) < 5:
            continue
        try:
            al, inf, lk = int(toks[-1]), int(toks[-2]), int(toks[-3])
        except ValueError:
            continue
        rest = toks[:-3]
        # flags 欄可能是空的；ES 固定兩位小寫 hex，flags 是大寫字母（含 x/o/p 特例）
        if len(rest) >= 5 and _RE_2HEX.match(rest[-1]):
            flags, es, rest = "", int(rest[-1], 16), rest[:-1]
        elif len(rest) >= 6 and _RE_2HEX.match(rest[-2]):
            flags, es, rest = rest[-1], int(rest[-2], 16), rest[:-2]
        else:
            continue
        if len(rest) < 4:
            continue
        try:
            size, off, addr = int(rest[-1], 16), int(rest[-2], 16), int(rest[-3], 16)
        except ValueError:
            continue
        rest = rest[:-3]
        if len(rest) == 1:
            name, typ = "", rest[0]
        else:
            name, typ = rest[0], " ".join(rest[1:])
        secs.append({
            "idx": idx, "name": name, "type": typ, "addr": addr, "offset": off,
            "size": size, "flags": flags, "link": lk, "info": inf, "align": al,
            "entsize": es, "nobits": typ == "NOBITS", "compressed": "C" in flags,
        })
    model.sections = secs


def _readelf_symtabs(model: ElfModel, txt: str) -> None:
    tables: Dict[str, List[Dict]] = {}
    current: Optional[str] = None
    in_data = False
    for line in txt.splitlines():
        s = line.strip()
        hm = re.match(r"Symbol table '([^']+)' contains", s)
        if hm:
            current = hm.group(1)
            tables.setdefault(current, [])
            in_data = False
            continue
        if s.startswith("Num:"):
            in_data = True
            continue
        if not in_data or current is None:
            continue
        m = _RE_SYM_ROW.match(line.rstrip())
        if not m:
            continue
        try:
            value = int(m.group(2), 16)
            size = int(m.group(3), 0)   # readelf 對大 size 會印 0x 開頭的 hex
        except ValueError:
            continue
        name = m.group(8).strip()
        if "@" in name:
            name = name.split("@", 1)[0]
        tables[current].append({
            "value": value, "size": size, "type": m.group(4), "bind": m.group(5),
            "vis": m.group(6), "ndx": m.group(7), "name": name,
        })
    model.symtabs = tables


def _readelf_dynamic(model: ElfModel, txt: str) -> None:
    tags: List[Tuple[str, str]] = []
    needed: List[str] = []
    soname = rpath = runpath = None
    flags_txt = flags1_txt = ""
    init_n = fini_n = 0
    ptr = 8 if model.header.get("class") == 64 else 4
    for line in txt.splitlines():
        m = _RE_DYN_ROW.match(line)
        if not m:
            continue
        tag, val = m.group(2), m.group(3).strip()
        tags.append((tag, val))
        br = re.search(r"\[(.*)\]", val)
        if tag == "NEEDED" and br:
            needed.append(br.group(1))
        elif tag == "SONAME" and br:
            soname = br.group(1)
        elif tag == "RPATH" and br:
            rpath = br.group(1)
        elif tag == "RUNPATH" and br:
            runpath = br.group(1)
        elif tag == "FLAGS":
            flags_txt = val
        elif tag == "FLAGS_1":
            flags1_txt = val.replace("Flags:", "").strip()
        elif tag == "INIT_ARRAYSZ":
            init_n = _parse_int(val) // ptr
        elif tag == "FINI_ARRAYSZ":
            fini_n = _parse_int(val) // ptr
    if not tags:
        model.dynamic = None
        return
    model.dynamic = {
        "tags": tags, "needed": needed, "soname": soname, "rpath": rpath, "runpath": runpath,
        "bind_now": ("BIND_NOW" in flags_txt) or (re.search(r"\bNOW\b", flags1_txt) is not None),
        "is_pie": re.search(r"\bPIE\b", flags1_txt) is not None,
        "init_array_count": init_n, "fini_array_count": fini_n,
        "flags_text": flags_txt, "flags_1_text": flags1_txt,
    }


def _readelf_relocs(model: ElfModel, txt: str) -> None:
    relocs: List[Dict] = []
    cur: Optional[Dict] = None
    for line in txt.splitlines():
        hm = _RE_RELOC_HDR.match(line)
        if hm:
            name, count = hm.group(1), int(hm.group(2))
            sec = model.section_by_name(name)
            applies = ""
            if sec is not None:
                if sec.get("info"):
                    tgt = model.section_by_index(int(sec["info"]))
                    applies = tgt["name"] if tgt else ""
            else:
                # 沒有 section 表可查時，從名稱推（.rela.text → .text）
                applies = re.sub(r"^\.rela?(?=[._]|$)", "", name)
            cur = {
                "section": name, "applies_to": applies,
                "rela": name.startswith(".rela") or (sec is not None and sec["type"] == "RELA"),
                "count": count, "by_type": Counter(), "entries": [],
            }
            relocs.append(cur)
            continue
        if cur is None:
            continue
        m = _RE_RELOC_ROW.match(line)
        if not m:
            continue
        try:
            offset = int(m.group(1), 16)
            int(m.group(2), 16)
        except ValueError:
            continue  # 欄位標題列（Offset / Info …）
        typ = m.group(3)
        rest = m.group(4).strip()
        cur["by_type"][typ] += 1
        if len(cur["entries"]) >= _RELOC_ENTRY_CAP:
            model.reloc_entry_cap_hit = True
            continue
        sym = ""
        addend: Optional[int] = None
        m2 = re.match(r"^([0-9a-fA-F]{8,16})\s+(.*)$", rest)
        if m2:
            tail = m2.group(2).strip()
            m3 = re.match(r"^(.*?)\s*([+-])\s*(?:0x)?([0-9a-fA-F]+)$", tail)
            if m3 and m3.group(1).strip():
                sym = m3.group(1).strip()
                addend = int(m3.group(3), 16) * (-1 if m3.group(2) == "-" else 1)
            else:
                sym = tail
        elif rest:
            if re.fullmatch(r"(?:0x)?[0-9a-fA-F]+", rest):
                addend = int(rest, 16)
            else:
                sym = rest
        if "@" in sym:
            sym = sym.split("@", 1)[0]
        cur["entries"].append({"offset": offset, "type": typ, "sym": sym, "addend": addend})
    model.relocs = relocs


def _readelf_notes(model: ElfModel, txt: str) -> None:
    for line in txt.splitlines():
        m = re.search(r"Displaying notes found in:\s*(\S+)", line)
        if m:
            model.note_names.append(m.group(1))
    m = re.search(r"Build ID:\s*([0-9a-fA-F]+)", txt)
    if m:
        model.build_id = m.group(1)


def _readelf_strdump(path: str, section: str) -> Tuple[List[str], Optional[str]]:
    """readelf -p <section> → (字串列, 失敗原因或 None)。"""
    out, err = run_cmd(["readelf", "-p", section, path], timeout=15)
    if out is None:
        return [], err or "unknown"
    rows: List[str] = []
    for line in out.splitlines():
        m = _RE_STRDUMP_ROW.match(line)
        if m:
            rows.append(m.group(1).strip())
    return rows, None


def _readelf_dwarf_cus(model: ElfModel) -> List[Dict]:
    out, err = run_cmd(
        ["readelf", "--debug-dump=info", "--dwarf-depth=1", str(model.path)], timeout=180,
    )
    if out is None:
        raise RuntimeError(f"readelf --debug-dump=info 失敗: {err}")
    cus: List[Dict] = []
    cur: Optional[Dict] = None
    pending_version = 0
    for line in out.splitlines():
        vm = _RE_CU_VERSION.match(line)
        if vm:
            pending_version = int(vm.group(1))
            continue
        hm = _RE_DIE_HDR.match(line)
        if hm:
            if int(hm.group(1)) == 0 and hm.group(3) in (
                "DW_TAG_compile_unit", "DW_TAG_partial_unit", "DW_TAG_type_unit",
            ):
                cur = {
                    "offset": int(hm.group(2), 16), "version": pending_version, "name": "",
                    "comp_dir": "", "producer": "", "language": "", "low_pc": None, "high_pc": None,
                }
                cus.append(cur)
            else:
                cur = None
            continue
        if cur is None:
            continue
        am = _RE_DIE_ATTR.match(line)
        if not am:
            continue
        key, val = am.group(1), _clean_readelf_attr(am.group(2))
        if key == "DW_AT_name":
            cur["name"] = val
        elif key == "DW_AT_comp_dir":
            cur["comp_dir"] = val
        elif key == "DW_AT_producer":
            cur["producer"] = val
        elif key == "DW_AT_language":
            cur["language"] = _lang_from_readelf(val)
        elif key == "DW_AT_low_pc":
            cur["low_pc"] = _parse_int(val)
        elif key == "DW_AT_high_pc":
            cur["high_pc"] = _parse_int(val)
    for cu in cus:
        # readelf 不顯示 form：high_pc 若小於 low_pc 就是 offset 形式（DWARF4+ 常見）
        if cu["low_pc"] is not None and cu["high_pc"] is not None and cu["high_pc"] < cu["low_pc"]:
            cu["high_pc"] = cu["low_pc"] + cu["high_pc"]
    return cus


def _readelf_dwarf_functions(model: ElfModel) -> List[Dict]:
    out, err = run_cmd(
        ["readelf", "--debug-dump=info", "--dwarf-depth=2", str(model.path)], timeout=300,
    )
    if out is None:
        raise RuntimeError(f"readelf --debug-dump=info 失敗: {err}")
    funcs: List[Dict] = []
    cu_name = ""
    cur: Optional[Dict] = None
    depth0_attrs: Optional[Dict] = None

    def _flush() -> None:
        nonlocal cur
        if cur is None:
            return
        if cur.get("name"):
            low, high = cur.get("low_pc"), cur.get("high_pc")
            if low is not None and high is not None and high < low:
                high = low + high
            funcs.append({
                "name": cur["name"], "low_pc": low, "high_pc": high,
                "file": cu_name if cur.get("decl_file") in (None, 1) else f"file#{cur.get('decl_file')}",
                "line": cur.get("decl_line"), "cu": cu_name,
                "external": cur.get("external", False), "inline": cur.get("inline", False),
                "declaration": cur.get("declaration", False),
            })
        cur = None

    for line in out.splitlines():
        hm = _RE_DIE_HDR.match(line)
        if hm:
            _flush()
            depth, tag = int(hm.group(1)), hm.group(3)
            depth0_attrs = None
            if depth == 0:
                depth0_attrs = {}
                cu_name = ""
            elif depth == 1 and tag == "DW_TAG_subprogram":
                cur = {}
                if len(funcs) >= _DWARF_FUNC_CAP:
                    break
            continue
        am = _RE_DIE_ATTR.match(line)
        if not am:
            continue
        key, val = am.group(1), _clean_readelf_attr(am.group(2))
        if cur is not None:
            if key == "DW_AT_name":
                cur["name"] = val
            elif key == "DW_AT_low_pc":
                cur["low_pc"] = _parse_int(val)
            elif key == "DW_AT_high_pc":
                cur["high_pc"] = _parse_int(val)
            elif key == "DW_AT_decl_file":
                cur["decl_file"] = _parse_int(val)
            elif key == "DW_AT_decl_line":
                cur["decl_line"] = _parse_int(val)
            elif key == "DW_AT_external":
                cur["external"] = True
            elif key == "DW_AT_inline":
                cur["inline"] = _parse_int(val) in (1, 3)
            elif key == "DW_AT_declaration":
                cur["declaration"] = True
        elif depth0_attrs is not None and key == "DW_AT_name":
            cu_name = val
    _flush()
    return funcs


def _readelf_dwarf_lines(model: ElfModel) -> List[Tuple[int, str, int]]:
    """decodedline → 依位址排序的 (addr, file, line)；line=-1 是 end-of-sequence 標記。"""
    out, err = run_cmd(
        ["readelf", "--debug-dump=decodedline", str(model.path)], timeout=300,
    )
    if out is None:
        raise RuntimeError(f"readelf --debug-dump=decodedline 失敗: {err}")
    rows: List[Tuple[int, str, int]] = []
    for line in out.splitlines():
        m = _RE_DECODED_LINE.match(line)
        if not m or m.group(1).lower() in ("file", "cu:"):
            continue
        ln = -1 if m.group(2) == "-" else int(m.group(2))
        rows.append((int(m.group(3), 16), m.group(1), ln))
    rows.sort(key=lambda r: r[0])
    return rows


def _load_readelf(model: ElfModel) -> None:
    model.parser = "readelf"
    if not cmd_exists("readelf"):
        model.parser_detail = "無（系統缺少 binutils readelf）"
        model.missing.append(
            "ELF header / segments / sections / symbols / .dynamic / relocation / DWARF 全部缺失"
            "（系統沒有 readelf，也沒有 pyelftools）；只剩字串掃描。"
            "補救：python3 -m pip install pyelftools，或安裝 binutils"
        )
        model.caps = {k: False for k in ("dwarf_cus", "dwarf_functions", "dwarf_lines", "dwarf_types")}
        return

    model.parser_detail = "readelf 文字解析（fallback）"
    p = str(model.path)

    def _step(args: List[str], timeout: int, cap: str, parser, marker: Optional[str], count) -> None:
        """跑一條 readelf、解析、驗證：失敗與「有輸出但解析不到」都記進 model.failed。"""
        label = "readelf " + " ".join(args)
        out, err = run_cmd(["readelf", *args, p], timeout=timeout)
        if out is None:
            model.failed[cap] = f"{label}: {err}"
            model.warnings.append(
                f"{label} 失敗（{err}）：{cap} 資料缺失——下面凡是「沒有 {cap}」的敘述都不可信"
            )
            return
        try:
            parser(model, out)
        except Exception as e:
            model.failed[cap] = f"{label}: 解析例外 {type(e).__name__}: {e}"
            model.warnings.append(f"{label} 解析失敗：{model.failed[cap]}")
            return
        if marker and marker in out and count() == 0:
            model.failed[cap] = f"{label}: 輸出含「{marker}」但解析不到任何項目（readelf 輸出格式可能改變）"
            model.warnings.append(f"{label} 解析失敗：{model.failed[cap]}")

    _step(["-h"], 10, "header", _readelf_header, "ELF Header", lambda: len(model.header))
    _step(["-lW"], 15, "segments", _readelf_segments, "Program Headers:", lambda: len(model.segments))
    _step(["-SW"], 15, "sections", _readelf_sections, "Section Headers:", lambda: len(model.sections))
    _step(["-sW"], 60, "symbols", _readelf_symtabs, "Symbol table",
          lambda: sum(len(v) for v in model.symtabs.values()))
    _step(["-dW"], 15, "dynamic", _readelf_dynamic, "Dynamic section at offset",
          lambda: len(model.dynamic["tags"]) if model.dynamic else 0)
    _step(["-rW"], 120, "relocs", _readelf_relocs, "Relocation section", lambda: len(model.relocs))
    _step(["-n"], 10, "notes", _readelf_notes, None, lambda: 1)
    if model.section_by_name(".comment"):
        rows, err = _readelf_strdump(p, ".comment")
        if err:
            model.failed["comment"] = f"readelf -p .comment: {err}"
            model.warnings.append(f"readelf -p .comment 失敗（{err}）")
        model.comment = rows
    if model.section_by_name(".modinfo"):
        rows, err = _readelf_strdump(p, ".modinfo")
        if err:
            model.failed["modinfo"] = f"readelf -p .modinfo: {err}"
            model.warnings.append(f"readelf -p .modinfo 失敗（{err}）")
        elif rows:
            model.modinfo = parse_modinfo(b"\x00".join(r.encode("utf-8", "replace") for r in rows))

    dbg = [s["name"] for s in model.sections if s["name"].startswith((".debug", ".zdebug"))]
    has_info = any(n in (".debug_info", ".zdebug_info") for n in dbg)
    model.dwarf = {"present": bool(dbg), "debug_info": has_info, "sections": dbg,
                   "unknown": "sections" in model.failed}
    model.caps = {
        "dwarf_cus": has_info, "dwarf_functions": has_info,
        "dwarf_lines": has_info, "dwarf_types": False,
    }
    model.missing.extend([
        "DWARF 型別資訊（struct / union / enum 成員、typedef）— readelf 文字路徑不解析，需要 pyelftools",
        "DWARF 函式清單只含直接掛在 CU 下、有 DW_AT_name 的 subprogram（C++ / inline / "
        "specification 型的會漏），decl_file 只能對到 CU 主檔；位址→來源行靠 readelf decodedline 文字解析",
        f"relocation 每個 section 只保留前 {_RELOC_ENTRY_CAP:,} 筆項目（統計仍為全量）",
    ])
    if not _HAS_PYELFTOOLS:
        model.missing.append("補救：python3 -m pip install pyelftools（重啟 MCP 後生效）即可恢復完整結構化解析")


# ---------------------------------------------------------------------------
# pyelftools backend（結構化）
# ---------------------------------------------------------------------------

_DF_BITS: List[Tuple[int, str]] = [
    (0x1, "ORIGIN"), (0x2, "SYMBOLIC"), (0x4, "TEXTREL"), (0x8, "BIND_NOW"), (0x10, "STATIC_TLS"),
]
_DF1_BITS: List[Tuple[int, str]] = [
    (0x1, "NOW"), (0x2, "GLOBAL"), (0x4, "GROUP"), (0x8, "NODELETE"), (0x10, "LOADFLTR"),
    (0x20, "INITFIRST"), (0x40, "NOOPEN"), (0x80, "ORIGIN"), (0x100, "DIRECT"),
    (0x400, "INTERPOSE"), (0x800, "NODEFLIB"), (0x1000, "NODUMP"), (0x8000000, "PIE"),
]


def _bits_text(value: int, table: List[Tuple[int, str]]) -> str:
    names = [name for bit, name in table if value & bit]
    return " ".join(names) if names else f"0x{value:x}"


def _py_symbols(sec) -> List[Dict]:
    syms: List[Dict] = []
    for sym in sec.iter_symbols():
        shndx = sym["st_shndx"]
        if isinstance(shndx, int):
            if shndx == 0:
                ndx = "UND"
            elif shndx == 0xfff1:
                ndx = "ABS"
            elif shndx == 0xfff2:
                ndx = "COM"
            else:
                ndx = str(shndx)
        else:
            s = str(shndx)
            ndx = {"SHN_UNDEF": "UND", "SHN_ABS": "ABS", "SHN_COMMON": "COM"}.get(s, s.replace("SHN_", ""))
        try:
            vis = _strip_enum(sym["st_other"]["visibility"], "STV_")
        except Exception:
            vis = ""
        syms.append({
            "value": int(sym["st_value"]),
            "size": int(sym["st_size"]),
            "type": _strip_enum(sym["st_info"]["type"], "STT_"),
            "bind": _strip_enum(sym["st_info"]["bind"], "STB_"),
            "vis": vis,
            "ndx": ndx,
            "name": sym.name or "",
        })
    return syms


def _py_relocs(model: ElfModel, elf, sec, sec_objs: list) -> Dict:
    symtab = None
    try:
        link = int(sec["sh_link"])
        if 0 < link < len(sec_objs) and isinstance(sec_objs[link], _PySymTab):
            symtab = sec_objs[link]
    except Exception:
        symtab = None
    applies = ""
    try:
        info = int(sec["sh_info"])
        if 0 < info < len(sec_objs):
            applies = sec_objs[info].name
    except Exception:
        pass
    is_rela = bool(sec.is_RELA())
    entry: Dict = {
        "section": sec.name, "applies_to": applies, "rela": is_rela,
        "count": 0, "by_type": Counter(), "entries": [],
    }
    sym_cache: Dict[int, str] = {}
    for r in sec.iter_relocations():
        entry["count"] += 1
        try:
            tname = _pydesc.describe_reloc_type(r["r_info_type"], elf)
        except Exception:
            tname = ""
        if not tname or tname.startswith("<") or tname.startswith("_"):
            tname = f"type#{int(r['r_info_type'])}"
        entry["by_type"][tname] += 1
        if len(entry["entries"]) >= _RELOC_ENTRY_CAP:
            model.reloc_entry_cap_hit = True
            continue
        sym_idx = int(r["r_info_sym"])
        sym_name = ""
        if symtab is not None and sym_idx:
            if sym_idx in sym_cache:
                sym_name = sym_cache[sym_idx]
            else:
                try:
                    s = symtab.get_symbol(sym_idx)
                    sym_name = s.name or ""
                    if not sym_name and str(s["st_info"]["type"]) == "STT_SECTION":
                        shndx = s["st_shndx"]
                        if isinstance(shndx, int) and 0 < shndx < len(sec_objs):
                            sym_name = sec_objs[shndx].name
                except Exception:
                    sym_name = ""
                sym_cache[sym_idx] = sym_name
        entry["entries"].append({
            "offset": int(r["r_offset"]), "type": tname, "sym": sym_name,
            "addend": int(r["r_addend"]) if is_rela else None,
        })
    return entry


def _py_dynamic(elf, dyn_sec) -> Dict:
    tags: List[Tuple[str, str]] = []
    needed: List[str] = []
    soname = rpath = runpath = None
    flags_val = flags_1_val = 0
    init_n = fini_n = 0
    ptr = 8 if elf.elfclass == 64 else 4
    for tag in dyn_sec.iter_tags():
        tname = _strip_enum(tag.entry.d_tag, "DT_")
        val = int(tag.entry.d_val) if isinstance(tag.entry.d_val, int) else 0
        display = f"0x{val:x}"
        if tname == "NEEDED":
            display = getattr(tag, "needed", None) or display
            needed.append(display)
        elif tname == "SONAME":
            soname = getattr(tag, "soname", None)
            display = soname or display
        elif tname == "RPATH":
            rpath = getattr(tag, "rpath", None)
            display = rpath or display
        elif tname == "RUNPATH":
            runpath = getattr(tag, "runpath", None)
            display = runpath or display
        elif tname == "FLAGS":
            flags_val = val
            display = _bits_text(val, _DF_BITS)
        elif tname == "FLAGS_1":
            flags_1_val = val
            display = _bits_text(val, _DF1_BITS)
        elif tname.endswith("SZ") or tname in ("RELCOUNT", "RELACOUNT", "VERNEEDNUM", "VERDEFNUM"):
            display = str(val)
            if tname == "INIT_ARRAYSZ":
                init_n = val // ptr
            elif tname == "FINI_ARRAYSZ":
                fini_n = val // ptr
        tags.append((tname, display))
    return {
        "tags": tags, "needed": needed, "soname": soname, "rpath": rpath, "runpath": runpath,
        "bind_now": bool(flags_val & 0x8) or bool(flags_1_val & 0x1),
        "is_pie": bool(flags_1_val & 0x8000000),
        "init_array_count": init_n, "fini_array_count": fini_n,
        "flags_text": _bits_text(flags_val, _DF_BITS) if flags_val else "",
        "flags_1_text": _bits_text(flags_1_val, _DF1_BITS) if flags_1_val else "",
    }


def _load_pyelftools(model: ElfModel) -> None:
    with open(model.path, "rb") as f:
        elf = _PyELFFile(f)
        e_machine = elf["e_machine"]
        key, disp = canonical_machine(e_machine)
        try:
            desc = _pydesc.describe_e_machine(e_machine)
        except Exception:
            desc = ""
        if not desc or desc.startswith("<unknown"):
            desc = disp if key != "unknown" else str(e_machine)
        model.header = {
            "class": elf.elfclass,
            "endian": "little" if elf.little_endian else "big",
            "osabi": _strip_enum(elf["e_ident"]["EI_OSABI"], "ELFOSABI_"),
            "type": _strip_enum(elf["e_type"], "ET_"),
            "type_desc": "",
            "machine": key,
            "machine_desc": desc,
            "machine_raw": str(e_machine),
            "entry": int(elf["e_entry"]),
            "flags": int(elf["e_flags"]),
            "phnum": elf.num_segments(),
            "shnum": elf.num_sections(),
        }

        sec_objs = list(elf.iter_sections())
        for idx, sec in enumerate(sec_objs):
            sh_type = _strip_enum(sec["sh_type"], "SHT_")
            try:
                flags = _pydesc.describe_sh_flags(sec["sh_flags"]).replace(" ", "")
            except Exception:
                flags = ""
            model.sections.append({
                "idx": idx, "name": sec.name, "type": sh_type,
                "addr": int(sec["sh_addr"]), "offset": int(sec["sh_offset"]),
                "size": int(sec["sh_size"]), "flags": flags,
                "link": int(sec["sh_link"]), "info": int(sec["sh_info"]),
                "align": int(sec["sh_addralign"]), "entsize": int(sec["sh_entsize"]),
                "nobits": sh_type == "NOBITS",
                "compressed": bool(getattr(sec, "compressed", False)),
            })
            name = sec.name
            try:
                if name == ".modinfo":
                    model.modinfo = parse_modinfo(sec.data())
                elif name == ".comment":
                    model.comment = parse_comment(sec.data())
            except Exception as e:
                model.warnings.append(f"{name} 讀取失敗: {type(e).__name__}: {e}")
            if isinstance(sec, _PyNoteSection):
                model.note_names.append(name)
                if model.build_id is None:
                    try:
                        for note in sec.iter_notes():
                            if note["n_type"] == "NT_GNU_BUILD_ID":
                                desc_v = note["n_desc"]
                                model.build_id = desc_v if isinstance(desc_v, str) else desc_v.hex()
                                break
                    except Exception:
                        pass
            if isinstance(sec, _PySymTab):
                try:
                    model.symtabs[name] = _py_symbols(sec)
                except Exception as e:
                    model.failed["symbols"] = f"pyelftools {name}: {type(e).__name__}: {e}"
                    model.warnings.append(f"symbol table {name} 解析失敗: {type(e).__name__}: {e}")
            elif isinstance(sec, _PyReloc):
                try:
                    model.relocs.append(_py_relocs(model, elf, sec, sec_objs))
                except Exception as e:
                    model.failed["relocs"] = f"pyelftools {name}: {type(e).__name__}: {e}"
                    model.warnings.append(f"relocation section {name} 解析失敗: {type(e).__name__}: {e}")

        for i, seg in enumerate(elf.iter_segments()):
            pf = int(seg["p_flags"])
            flags = ("R" if pf & 4 else "-") + ("W" if pf & 2 else "-") + ("E" if pf & 1 else "-")
            names: List[str] = []
            for sec in sec_objs:
                if not sec.name:
                    continue
                try:
                    if seg.section_in_segment(sec):
                        names.append(sec.name)
                except Exception:
                    pass
            model.segments.append({
                "idx": i, "type": _strip_enum(seg["p_type"], "PT_"),
                "offset": int(seg["p_offset"]), "vaddr": int(seg["p_vaddr"]),
                "paddr": int(seg["p_paddr"]), "filesz": int(seg["p_filesz"]),
                "memsz": int(seg["p_memsz"]), "flags": flags,
                "align": int(seg["p_align"]), "sections": names,
            })

        dyn_sec = elf.get_section_by_name(".dynamic")
        if dyn_sec is not None and isinstance(dyn_sec, _PyDynamic):
            try:
                model.dynamic = _py_dynamic(elf, dyn_sec)
            except Exception as e:
                model.failed["dynamic"] = f"pyelftools .dynamic: {type(e).__name__}: {e}"
                model.warnings.append(f".dynamic 解析失敗: {type(e).__name__}: {e}")

    dbg = [s["name"] for s in model.sections if s["name"].startswith((".debug", ".zdebug"))]
    has_info = any(n in (".debug_info", ".zdebug_info") for n in dbg)
    model.dwarf = {"present": bool(dbg), "debug_info": has_info, "sections": dbg}
    model.caps = {
        "dwarf_cus": has_info, "dwarf_functions": has_info,
        "dwarf_lines": has_info, "dwarf_types": has_info,
    }
    model.parser = "pyelftools"
    model.parser_detail = f"pyelftools {_PYELFTOOLS_VERSION}"
    if model.reloc_entry_cap_hit:
        model.missing.append(
            f"relocation 每個 section 只保留前 {_RELOC_ENTRY_CAP:,} 筆項目（統計仍為全量）"
        )


# ---------------------------------------------------------------------------
# 載入（含 magic 檢查與快取）
# ---------------------------------------------------------------------------

_MODEL_CACHE: "OrderedDict[Tuple, ElfModel]" = OrderedDict()
_MODEL_CACHE_MAX = 4


def is_elf_file(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def load_model(path) -> ElfModel:
    """解析 ELF 成 ElfModel（同一檔案同一 mtime 快取；pyelftools 失敗退回 readelf）。"""
    path = Path(path)
    with open(path, "rb") as f:
        magic = f.read(4)
    if not magic.startswith(b"\x7fELF"):
        raise ValueError(f"不是有效的 ELF 檔案 (magic: 0x{magic.hex()})")
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        _MODEL_CACHE.move_to_end(key)
        return cached

    model = ElfModel(path)
    if _HAS_PYELFTOOLS:
        try:
            _load_pyelftools(model)
        except Exception as e:
            model = ElfModel(path)
            model.warnings.append(
                f"pyelftools 解析失敗（{type(e).__name__}: {e}），已退回 readelf 文字解析"
            )
            _load_readelf(model)
    else:
        model.warnings.append(
            "未安裝 pyelftools（python3 -m pip install pyelftools），改用 readelf 文字解析"
        )
        _load_readelf(model)

    _MODEL_CACHE[key] = model
    _MODEL_CACHE.move_to_end(key)
    while len(_MODEL_CACHE) > _MODEL_CACHE_MAX:
        _MODEL_CACHE.popitem(last=False)
    return model


# ---------------------------------------------------------------------------
# 位址 / symbol 輔助
# ---------------------------------------------------------------------------

# ARM / AArch64 / RISC-V / ARC 的 mapping symbol（$t / $a / $d / $x…）：不是函式，
# 但 firmware 的 .symtab 裡常常成千上百個，列表要濾掉、統計要單獨算。
_MAPPING_SYM_RE = re.compile(r"^\$[atdxvfbm](\.|$)")


def is_mapping_symbol(name: str) -> bool:
    return bool(_MAPPING_SYM_RE.match(name or ""))


def _iter_load_segments(model: ElfModel):
    """LOAD segment 的 generator：不建 list（大量 segment 的 ELF 不該為了一個 view 先物化全部）。"""
    return (seg for seg in model.segments if seg["type"] == "LOAD")


def _n_load_segments(model: ElfModel) -> int:
    n = model._lazy.get("n_load")
    if n is None:
        n = sum(1 for _ in _iter_load_segments(model))
        model._lazy["n_load"] = n
    return n


def _load_segments(model: ElfModel) -> List[Dict]:
    """需要整份 list 的少數呼叫端（測試 / 小型用途）；渲染路徑一律用 _iter_load_segments。"""
    return list(_iter_load_segments(model))

def segment_for_addr(model: ElfModel, addr: int) -> Optional[Dict]:
    for seg in _iter_load_segments(model):
        if seg["vaddr"] <= addr < seg["vaddr"] + max(seg["memsz"], 1):
            return seg
    return None


def section_for_addr(model: ElfModel, addr: int) -> Optional[Dict]:
    """含該位址的 alloc section（最小者優先）；REL 檔的 addr 全是 0，呼叫端要另外處理。"""
    best: Optional[Dict] = None
    for sec in model.sections:
        if not sec["name"] or sec["size"] <= 0 or "A" not in sec["flags"]:
            continue
        if sec["addr"] <= addr < sec["addr"] + sec["size"]:
            if best is None or sec["size"] < best["size"]:
                best = sec
    return best


def addr_to_file_offset(model: ElfModel, addr: int) -> Optional[Tuple[int, int]]:
    """虛擬位址 → (file offset, 從該處起檔案內還有幾個 bytes)。先看 LOAD segment 再看 section。"""
    for seg in _iter_load_segments(model):
        if seg["vaddr"] <= addr < seg["vaddr"] + seg["filesz"]:
            return seg["offset"] + (addr - seg["vaddr"]), seg["vaddr"] + seg["filesz"] - addr
    sec = section_for_addr(model, addr)
    if sec is not None and not sec["nobits"]:
        return sec["offset"] + (addr - sec["addr"]), sec["addr"] + sec["size"] - addr
    return None


def section_for_offset(model: ElfModel, off: int) -> Optional[Dict]:
    best: Optional[Dict] = None
    for sec in model.sections:
        if not sec["name"] or sec["size"] <= 0 or sec["nobits"]:
            continue
        if sec["offset"] <= off < sec["offset"] + sec["size"]:
            if best is None or sec["size"] < best["size"]:
                best = sec
    return best


def read_file_bytes(model: ElfModel, offset: int, n: int) -> bytes:
    if n <= 0 or offset < 0:
        return b""
    with open(model.path, "rb") as f:
        f.seek(offset)
        return f.read(n)


def _defined_symbols_sorted(model: ElfModel) -> Tuple[List[int], List[Dict]]:
    cached = model._lazy.get("sym_sorted")
    if cached is not None:
        return cached  # type: ignore[return-value]
    _, syms = model.primary_symtab()
    picked = [
        s for s in syms
        if s["ndx"] not in ("UND", "COM") and s["type"] in ("FUNC", "OBJECT", "NOTYPE")
        and s["name"] and not is_mapping_symbol(s["name"])
    ]
    picked.sort(key=lambda s: (s["value"], -s["size"]))
    addrs = [s["value"] for s in picked]
    model._lazy["sym_sorted"] = (addrs, picked)
    return addrs, picked


def symbol_for_addr(model: ElfModel, addr: int) -> Optional[Tuple[Dict, int, bool]]:
    """回傳 (symbol, 位址相對 symbol 的偏移, 是否落在 symbol size 內)。

    Thumb 位址（LSB=1）與 symbol value 都先清掉 bit0 再比。找不到 size 內的就退回同一個
    section 內最近的前一個 symbol（size=0 的組語 label 常見）。
    """
    if model.is_rel:
        return None
    addrs, syms = _defined_symbols_sorted(model)
    if not syms:
        return None
    target = addr & ~1 if model.machine == "arm" else addr
    i = bisect.bisect_right(addrs, target | 1 if model.machine == "arm" else target) - 1
    # 先找 size 內的（往回最多看 8 個，重疊/巢狀 symbol 少見）
    j = i
    while j >= 0 and i - j < 8:
        s = syms[j]
        val = s["value"] & ~1 if model.machine == "arm" else s["value"]
        if s["size"] > 0 and val <= target < val + s["size"]:
            return s, target - val, True
        j -= 1
    if i >= 0:
        s = syms[i]
        val = s["value"] & ~1 if model.machine == "arm" else s["value"]
        sec_a = section_for_addr(model, target)
        if sec_a is not None and s["ndx"] == str(sec_a["idx"]):
            return s, target - val, False
    return None


def find_symbols_by_name(model: ElfModel, name: str) -> List[Tuple[str, Dict]]:
    """依名稱找 symbol（所有表）：先精確，再忽略大小寫，再容忍前導底線差異。"""
    name = name.strip()
    if not name:
        return []
    exact: List[Tuple[str, Dict]] = []
    loose: List[Tuple[str, Dict]] = []
    alt = {name.lower(), name.lstrip("_").lower(), ("_" + name).lower()}
    for tname, syms in model.symtabs.items():
        for s in syms:
            if not s["name"]:
                continue
            if s["name"] == name:
                exact.append((tname, s))
            elif s["name"].lower() in alt:
                loose.append((tname, s))
    found = exact or loose
    # 定義過的、FUNC 的排前面
    found.sort(key=lambda t: (t[1]["ndx"] == "UND", t[1]["type"] != "FUNC", t[1]["size"] == 0))
    return found


def symtab_stats(syms: List[Dict]) -> Dict[str, int]:
    st = {
        "total": len(syms), "func": 0, "func_global": 0, "func_weak": 0, "func_local": 0,
        "object": 0, "und": 0, "zero_size": 0, "mapping": 0, "file": 0, "section": 0,
    }
    for s in syms:
        if not s["name"] and s["type"] == "NOTYPE" and s["value"] == 0 and s["size"] == 0:
            continue  # index 0 的 null symbol：不是 UND 也不是任何東西
        if is_mapping_symbol(s["name"]):
            st["mapping"] += 1
            continue
        if s["ndx"] == "UND":
            st["und"] += 1
            continue
        t = s["type"]
        if t == "FUNC":
            st["func"] += 1
            if s["bind"] == "GLOBAL":
                st["func_global"] += 1
            elif s["bind"] == "WEAK":
                st["func_weak"] += 1
            else:
                st["func_local"] += 1
            if s["size"] == 0:
                st["zero_size"] += 1
        elif t == "OBJECT":
            st["object"] += 1
            if s["size"] == 0:
                st["zero_size"] += 1
        elif t == "FILE":
            st["file"] += 1
        elif t == "SECTION":
            st["section"] += 1
    return st


# ---------------------------------------------------------------------------
# 字串（lazy）
# ---------------------------------------------------------------------------

def model_strings(model: ElfModel) -> List[Dict]:
    """全檔可讀字串（ASCII 全檔 + UTF-16LE 前 4MB），含 offset / section / 分類；快取在 model 上。"""
    cached = model._lazy.get("strings")
    if cached is not None:
        return cached  # type: ignore[return-value]

    ranges = sorted(
        (s["offset"], s["offset"] + s["size"], s["name"])
        for s in model.sections
        if s["name"] and s["size"] > 0 and not s["nobits"]
    )
    starts = [r[0] for r in ranges]

    def _sec_of(off: int) -> str:
        i = bisect.bisect_right(starts, off) - 1
        tries = 0
        while i >= 0 and tries < 4:
            lo, hi, name = ranges[i]
            if lo <= off < hi:
                return name
            i -= 1
            tries += 1
        return ""

    items: List[Dict] = []
    capped = False
    for enc, seq in (
        ("ascii", scan_ascii_strings(model.path, min_len=6, max_bytes=None)),
        ("utf16", scan_utf16le_strings(model.path, min_len=6)),
    ):
        for off, s in seq:
            s = s.strip()
            if not is_meaningful_string(s):
                continue
            if len(items) >= _STRINGS_CAP:
                capped = True
                break
            sec_name = _sec_of(off)
            # 符號名 / section 名表不是「程式內字串」，不分類（仍列在完整清單裡）
            cats = () if sec_name in (".strtab", ".dynstr", ".shstrtab") else categorize_string(s)
            items.append({
                "offset": off, "text": s, "enc": enc,
                "section": sec_name, "cats": cats,
            })
    model._lazy["strings"] = items
    model._lazy["strings_capped"] = capped
    return items


# ---------------------------------------------------------------------------
# DWARF（lazy；pyelftools 結構化 / readelf 文字）
# ---------------------------------------------------------------------------

_CONTAINER_TAGS = {
    "DW_TAG_namespace", "DW_TAG_class_type", "DW_TAG_structure_type",
    "DW_TAG_union_type", "DW_TAG_module",
}
_TYPE_TAGS = {
    "DW_TAG_structure_type": "struct", "DW_TAG_union_type": "union",
    "DW_TAG_class_type": "class", "DW_TAG_enumeration_type": "enum",
    "DW_TAG_typedef": "typedef",
}


def _py_iter_dies(top, max_depth: int = 3):
    """走訪 CU 下的 DIE；只在 namespace / class 這類容器內往下遞迴（不進函式本體）。"""
    stack = [(top, 0)]
    while stack:
        die, depth = stack.pop()
        try:
            children = list(die.iter_children())
        except Exception:
            continue
        for ch in children:
            yield ch, depth + 1
            if depth + 1 < max_depth and ch.tag in _CONTAINER_TAGS:
                stack.append((ch, depth + 1))


def _py_die_name(die, depth: int = 0) -> str:
    a = die.attributes
    for key in ("DW_AT_name", "DW_AT_linkage_name", "DW_AT_MIPS_linkage_name"):
        if key in a:
            return _decode(a[key].value)
    if depth < 4:
        for ref in ("DW_AT_specification", "DW_AT_abstract_origin"):
            if ref in a:
                try:
                    other = die.get_DIE_from_attribute(ref)
                    n = _py_die_name(other, depth + 1)
                    if n:
                        return n
                except Exception:
                    pass
    return ""


def _py_pc_range(die) -> Tuple[Optional[int], Optional[int]]:
    a = die.attributes
    lo = a.get("DW_AT_low_pc")
    if lo is None or not isinstance(lo.value, int):
        return None, None
    low = int(lo.value)
    hi = a.get("DW_AT_high_pc")
    if hi is None or not isinstance(hi.value, int):
        return low, None
    form = str(hi.form)
    if "data" in form or form in ("DW_FORM_implicit_const",):
        return low, low + int(hi.value)
    return low, int(hi.value)


def _py_lp_file(lp, idx: int, comp_dir: str) -> str:
    try:
        version = int(lp.header["version"])
        files = lp["file_entry"]
        dirs = lp["include_directory"]
        i = idx if version >= 5 else idx - 1
        if i < 0 or i >= len(files):
            return f"file#{idx}"
        fe = files[i]
        name = _decode(fe.name)
        di = int(fe.dir_index)
        if version >= 5:
            d = _decode(dirs[di]) if 0 <= di < len(dirs) else ""
        else:
            d = comp_dir if di == 0 else (_decode(dirs[di - 1]) if 0 < di <= len(dirs) else "")
        if name.startswith("/") or not d:
            return name
        return f"{d.rstrip('/')}/{name}"
    except Exception:
        return f"file#{idx}"


def _py_type_name(die, depth: int = 0) -> str:
    if die is None:
        return "void"
    if depth > 12:
        return "…"
    tag = die.tag
    a = die.attributes
    name = _decode(a["DW_AT_name"].value) if "DW_AT_name" in a else ""

    def sub() -> str:
        if "DW_AT_type" not in a:
            return "void"
        try:
            return _py_type_name(die.get_DIE_from_attribute("DW_AT_type"), depth + 1)
        except Exception:
            return "?"

    if tag == "DW_TAG_base_type":
        return name or "?"
    if tag == "DW_TAG_typedef":
        return name or sub()
    if tag == "DW_TAG_structure_type":
        return f"struct {name or '<anon>'}"
    if tag == "DW_TAG_union_type":
        return f"union {name or '<anon>'}"
    if tag == "DW_TAG_class_type":
        return f"class {name or '<anon>'}"
    if tag == "DW_TAG_enumeration_type":
        return f"enum {name or '<anon>'}"
    if tag == "DW_TAG_pointer_type":
        return sub() + " *"
    if tag == "DW_TAG_reference_type":
        return sub() + " &"
    if tag == "DW_TAG_rvalue_reference_type":
        return sub() + " &&"
    if tag == "DW_TAG_const_type":
        return "const " + sub()
    if tag == "DW_TAG_volatile_type":
        return "volatile " + sub()
    if tag == "DW_TAG_restrict_type":
        return sub() + " restrict"
    if tag == "DW_TAG_array_type":
        dims: List[str] = []
        try:
            for ch in die.iter_children():
                if ch.tag != "DW_TAG_subrange_type":
                    continue
                ca = ch.attributes
                if "DW_AT_count" in ca and isinstance(ca["DW_AT_count"].value, int):
                    dims.append(str(int(ca["DW_AT_count"].value)))
                elif "DW_AT_upper_bound" in ca and isinstance(ca["DW_AT_upper_bound"].value, int):
                    dims.append(str(int(ca["DW_AT_upper_bound"].value) + 1))
                else:
                    dims.append("")
        except Exception:
            pass
        return sub() + ("".join(f"[{d}]" for d in dims) if dims else "[]")
    if tag == "DW_TAG_subroutine_type":
        return f"{sub()} (*)(…)"
    if tag == "DW_TAG_unspecified_type":
        return name or "void"
    return name or tag.replace("DW_TAG_", "")


def _uleb128(data: List[int]) -> int:
    result = 0
    shift = 0
    for b in data:
        result |= (b & 0x7f) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result


def _member_offset(attr) -> str:
    if attr is None:
        return ""
    v = attr.value
    if isinstance(v, int):
        return f"+0x{v:x}"
    if isinstance(v, (list, tuple)) and v:
        if v[0] == 0x23:  # DW_OP_plus_uconst
            return f"+0x{_uleb128(list(v[1:])):x}"
        return "+?"
    return ""


def _py_cu_name_dir(top) -> Tuple[str, str]:
    a = top.attributes
    name = _decode(a["DW_AT_name"].value) if "DW_AT_name" in a else ""
    comp_dir = _decode(a["DW_AT_comp_dir"].value) if "DW_AT_comp_dir" in a else ""
    return name, comp_dir


def _dwarf_error(model: ElfModel, e: Exception) -> None:
    model._lazy["dwarf_error"] = f"{type(e).__name__}: {e}"


def dwarf_cus(model: ElfModel) -> List[Dict]:
    cached = model._lazy.get("dwarf_cus")
    if cached is not None:
        return cached  # type: ignore[return-value]
    cus: List[Dict] = []
    if model.caps.get("dwarf_cus"):
        try:
            if model.parser == "pyelftools":
                with open(model.path, "rb") as f:
                    elf = _PyELFFile(f)
                    if elf.has_dwarf_info():
                        d = elf.get_dwarf_info()
                        for cu in d.iter_CUs():
                            top = cu.get_top_DIE()
                            a = top.attributes
                            name, comp_dir = _py_cu_name_dir(top)
                            low, high = _py_pc_range(top)
                            lang = ""
                            if "DW_AT_language" in a and isinstance(a["DW_AT_language"].value, int):
                                lv = int(a["DW_AT_language"].value)
                                lang = _DW_LANG.get(lv, f"lang#{lv}")
                            cus.append({
                                "offset": int(cu.cu_offset), "version": int(cu["version"]),
                                "name": name, "comp_dir": comp_dir,
                                "producer": _decode(a["DW_AT_producer"].value) if "DW_AT_producer" in a else "",
                                "language": lang, "low_pc": low, "high_pc": high,
                            })
            else:
                cus = _readelf_dwarf_cus(model)
        except Exception as e:
            _dwarf_error(model, e)
            cus = []
    model._lazy["dwarf_cus"] = cus
    return cus


def dwarf_functions(model: ElfModel) -> List[Dict]:
    cached = model._lazy.get("dwarf_funcs")
    if cached is not None:
        return cached  # type: ignore[return-value]
    funcs: List[Dict] = []
    if model.caps.get("dwarf_functions"):
        try:
            if model.parser == "pyelftools":
                with open(model.path, "rb") as f:
                    elf = _PyELFFile(f)
                    if elf.has_dwarf_info():
                        d = elf.get_dwarf_info()
                        for cu in d.iter_CUs():
                            top = cu.get_top_DIE()
                            cu_name, comp_dir = _py_cu_name_dir(top)
                            lp = None
                            lp_tried = False
                            for die, _depth in _py_iter_dies(top, 3):
                                if die.tag != "DW_TAG_subprogram":
                                    continue
                                name = _py_die_name(die)
                                if not name:
                                    continue
                                a = die.attributes
                                low, high = _py_pc_range(die)
                                file_name = ""
                                if "DW_AT_decl_file" in a and isinstance(a["DW_AT_decl_file"].value, int):
                                    if not lp_tried:
                                        lp_tried = True
                                        try:
                                            lp = d.line_program_for_CU(cu)
                                        except Exception:
                                            lp = None
                                    fidx = int(a["DW_AT_decl_file"].value)
                                    file_name = _py_lp_file(lp, fidx, comp_dir) if lp is not None else f"file#{fidx}"
                                inl = a.get("DW_AT_inline")
                                funcs.append({
                                    "name": name, "low_pc": low, "high_pc": high, "file": file_name,
                                    "line": int(a["DW_AT_decl_line"].value) if "DW_AT_decl_line" in a else None,
                                    "cu": cu_name, "external": "DW_AT_external" in a,
                                    "inline": bool(inl is not None and isinstance(inl.value, int) and int(inl.value) in (1, 3)),
                                    "declaration": "DW_AT_declaration" in a,
                                })
                                if len(funcs) >= _DWARF_FUNC_CAP:
                                    model._lazy["dwarf_funcs_capped"] = True
                                    break
                            if model._lazy.get("dwarf_funcs_capped"):
                                break
            else:
                funcs = _readelf_dwarf_functions(model)
        except Exception as e:
            _dwarf_error(model, e)
            funcs = []
    model._lazy["dwarf_funcs"] = funcs
    return funcs


def dwarf_types(model: ElfModel, pattern: "re.Pattern[str]", limit: int) -> Tuple[List[Dict], bool]:
    """符合 pattern 的 struct / union / class / enum / typedef（含成員）。回傳 (結果, 是否截斷)。"""
    if not model.caps.get("dwarf_types") or model.parser != "pyelftools":
        return [], False
    limit = max(1, min(int(limit), _DWARF_TYPE_CAP))   # 不管呼叫端給多大，型別數有絕對上限
    results: Dict[Tuple[str, str], Dict] = {}
    truncated = False
    try:
        with open(model.path, "rb") as f:
            elf = _PyELFFile(f)
            if not elf.has_dwarf_info():
                return [], False
            d = elf.get_dwarf_info()
            for cu in d.iter_CUs():
                top = cu.get_top_DIE()
                cu_name, comp_dir = _py_cu_name_dir(top)
                lp = None
                lp_tried = False
                for die, _depth in _py_iter_dies(top, 3):
                    kind = _TYPE_TAGS.get(die.tag)
                    if not kind:
                        continue
                    name = _py_die_name(die)
                    if not name or not _rx(pattern, name):
                        continue
                    a = die.attributes
                    is_decl = "DW_AT_declaration" in a
                    key = (kind, name)
                    if key in results and (is_decl or results[key]["members"] or results[key].get("target")):
                        continue
                    file_name = ""
                    if "DW_AT_decl_file" in a and isinstance(a["DW_AT_decl_file"].value, int):
                        if not lp_tried:
                            lp_tried = True
                            try:
                                lp = d.line_program_for_CU(cu)
                            except Exception:
                                lp = None
                        fidx = int(a["DW_AT_decl_file"].value)
                        file_name = _py_lp_file(lp, fidx, comp_dir) if lp is not None else f"file#{fidx}"
                    entry: Dict = {
                        "kind": kind, "name": name,
                        "size": int(a["DW_AT_byte_size"].value) if "DW_AT_byte_size" in a and isinstance(a["DW_AT_byte_size"].value, int) else None,
                        "file": file_name,
                        "line": int(a["DW_AT_decl_line"].value) if "DW_AT_decl_line" in a else None,
                        "members": [], "declaration": is_decl, "cu": cu_name, "target": "",
                    }
                    if kind == "typedef":
                        try:
                            entry["target"] = _py_type_name(die.get_DIE_from_attribute("DW_AT_type")) if "DW_AT_type" in a else "void"
                        except Exception:
                            entry["target"] = "?"
                    else:
                        try:
                            for ch in die.iter_children():
                                if len(entry["members"]) >= _DWARF_TYPE_MEMBERS_CAP:
                                    entry["members_truncated"] = True
                                    break
                                ca = ch.attributes
                                if kind == "enum" and ch.tag == "DW_TAG_enumerator":
                                    cv = ca.get("DW_AT_const_value")
                                    entry["members"].append({
                                        "name": _decode(ca["DW_AT_name"].value) if "DW_AT_name" in ca else "?",
                                        "value": int(cv.value) if cv is not None and isinstance(cv.value, int) else None,
                                    })
                                elif ch.tag == "DW_TAG_member":
                                    try:
                                        tname = _py_type_name(ch.get_DIE_from_attribute("DW_AT_type")) if "DW_AT_type" in ca else "?"
                                    except Exception:
                                        tname = "?"
                                    bits = ""
                                    if "DW_AT_bit_size" in ca:
                                        bits = f" : {int(ca['DW_AT_bit_size'].value)} bits"
                                        if "DW_AT_data_bit_offset" in ca:
                                            bits += f" @bit {int(ca['DW_AT_data_bit_offset'].value)}"
                                        elif "DW_AT_bit_offset" in ca:
                                            bits += f" (bit_offset {int(ca['DW_AT_bit_offset'].value)})"
                                    entry["members"].append({
                                        "name": _decode(ca["DW_AT_name"].value) if "DW_AT_name" in ca else "<anon>",
                                        "type": tname,
                                        "offset": _member_offset(ca.get("DW_AT_data_member_location")),
                                        "bits": bits,
                                    })
                        except Exception:
                            pass
                    results[key] = entry
                    if len(results) >= limit:
                        truncated = True
                        break
                if truncated:
                    break
    except Exception as e:
        _dwarf_error(model, e)
    return list(results.values()), truncated


def dwarf_addr_to_line(model: ElfModel, addr: int) -> Optional[Dict]:
    """位址 → {file, line, cu, function}；找不到回 None。"""
    if not model.caps.get("dwarf_lines"):
        return None
    func = None
    for fn in dwarf_functions(model):
        if fn["low_pc"] is not None and fn["high_pc"] is not None and fn["low_pc"] <= addr < fn["high_pc"]:
            if func is None or (fn["high_pc"] - fn["low_pc"]) < (func["high_pc"] - func["low_pc"]):
                func = fn
    try:
        if model.parser == "pyelftools":
            return _py_addr_to_line(model, addr, func)
        rows = model._lazy.get("dwarf_lines")
        if rows is None:
            rows = _readelf_dwarf_lines(model)
            model._lazy["dwarf_lines"] = rows
        if not rows:
            return None
        addrs = [r[0] for r in rows]
        i = bisect.bisect_right(addrs, addr) - 1
        if i < 0:
            return None
        row = rows[i]
        if row[2] < 0:
            return None
        return {"file": row[1], "line": row[2], "cu": "", "function": func["name"] if func else ""}
    except Exception as e:
        _dwarf_error(model, e)
        return None


def _py_addr_to_line(model: ElfModel, addr: int, func: Optional[Dict]) -> Optional[Dict]:
    with open(model.path, "rb") as f:
        elf = _PyELFFile(f)
        if not elf.has_dwarf_info():
            return None
        d = elf.get_dwarf_info()
        cu = None
        try:
            ar = d.get_aranges()
            if ar is not None:
                off = ar.cu_offset_at_addr(addr)
                if off is not None:
                    cu = d.get_CU_at(off)
        except Exception:
            cu = None
        candidates = [cu] if cu is not None else []
        if not candidates:
            for c in d.iter_CUs():
                low, high = _py_pc_range(c.get_top_DIE())
                if low is not None and high is not None and low <= addr < high:
                    candidates = [c]
                    break
        if not candidates:
            candidates = list(d.iter_CUs())  # 最後手段：掃全部 line program
        for c in candidates:
            try:
                lp = d.line_program_for_CU(c)
            except Exception:
                lp = None
            if lp is None:
                continue
            cu_name, comp_dir = _py_cu_name_dir(c.get_top_DIE())
            prev = None
            for entry in lp.get_entries():
                st = entry.state
                if st is None:
                    continue
                if prev is not None and not prev.end_sequence and prev.address <= addr < st.address:
                    return {
                        "file": _py_lp_file(lp, int(prev.file), comp_dir),
                        "line": int(prev.line), "cu": cu_name,
                        "function": func["name"] if func else "",
                    }
                prev = st
    return None


# ---------------------------------------------------------------------------
# 反組譯（objdump 變體 → capstone → 明講失敗原因）
# ---------------------------------------------------------------------------

def _objdump_candidates(machine: str) -> List[str]:
    cands: List[str] = []
    env = os.environ.get("AICODE_OBJDUMP", "").strip()
    if env:
        cands.append(env)
    cands.append("objdump")
    cands.extend(_OBJDUMP_CANDIDATES.get(machine, []))
    seen: set = set()
    out: List[str] = []
    for c in cands:
        if c in seen:
            continue
        seen.add(c)
        if cmd_exists(c):
            out.append(c)
    return _finish(out)


def _has_mapping_symbols(model: ElfModel) -> bool:
    for syms in model.symtabs.values():
        for s in syms:
            if is_mapping_symbol(s["name"]):
                return True
    return False


def _parse_objdump_output(out: str, limit: int, start: Optional[int] = None) -> Tuple[List[str], int, Optional[int]]:
    """objdump -d 輸出 → (格式化行, 指令數, 下一條指令位址)。

    objdump 用 tab 分欄（addr:\\tbytes\\tmnemonic）；不能用空白切，因為 `add` 這類助憶碼
    全是 hex 字元，會被誤當成 byte 欄。
    """
    lines: List[str] = []
    n = 0
    last_addr: Optional[int] = None
    last_nbytes = 0
    for raw in out.splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t")
        m = re.match(r"^\s*([0-9a-fA-F]+):\s*$", parts[0]) if parts else None
        if m and len(parts) >= 2:
            addr = int(m.group(1), 16)
            if start is not None and addr < start:
                continue
            byts = parts[1].strip()
            rest = " ".join(p.strip() for p in parts[2:] if p.strip())
            if not rest:
                # 長指令的續行（只有剩餘 bytes，沒有助憶碼）：只更新長度，不另起一行
                last_nbytes += len(byts.split())
                continue
            if n >= limit:
                break
            lines.append(f"  {addr:x}:  {byts:<24} {rest}")
            n += 1
            last_addr = addr
            last_nbytes = len(byts.split())
        else:
            s = raw.strip()
            if re.match(r"^[0-9a-fA-F]+ <[^>]+>:$", s):
                if n < limit:
                    lines.append(s)
            elif s == "...":
                if n < limit:
                    lines.append("  ...")
    nxt = (last_addr + last_nbytes) if last_addr is not None else None
    return lines, n, nxt


def _disasm_plan(model: ElfModel, target: str, limit: int) -> Tuple[Optional[Dict], Optional[str]]:
    """把 target 解析成 (plan, error)。plan: mode / start / stop / symbol / section / thumb / label。"""
    tokens = (target or "").split()
    section_filter: Optional[str] = None
    free: List[str] = []
    for t in tokens:
        if t.lower().startswith("section:"):
            section_filter = t[8:]
        else:
            free.append(t)
    spec = " ".join(free).strip()
    width = _FIXED_INSTR_WIDTH.get(model.machine, 8)
    plan: Dict = {
        "mode": "range", "start": 0, "stop": None, "symbol": "", "section": section_filter,
        "thumb": False, "label": "", "size": 0,
    }

    if not spec:
        if model.is_rel:
            return None, ("REL 檔（.o / .ko）沒有 entry point：請指定 target=<symbol 名>"
                          "（例如 target=\"init_module\"），或 target=\"0x<offset> section:.text\"")
        plan["start"] = model.entry
        hit = symbol_for_addr(model, model.entry)
        plan["label"] = f"entry point {model.fmt_addr(model.entry)}"
        if hit is not None:
            s, off, exact = hit
            plan["label"] += f" → {s['name']}" + (f"+0x{off:x}" if off else "")
            if exact and off == 0 and s["size"] > 0:
                plan["size"] = s["size"]
                plan["stop"] = plan["start"] + s["size"]
    elif re.fullmatch(r"0x[0-9a-fA-F]+\s*-\s*0x[0-9a-fA-F]+", spec):
        a, b = [int(x.strip(), 16) for x in spec.split("-")]
        if b <= a:
            return None, f"位址範圍不合法：{spec}（迄 ≤ 起）"
        plan.update({"start": a, "stop": b, "label": f"{model.fmt_addr(a)}-{model.fmt_addr(b)}"})
    elif re.fullmatch(r"0x[0-9a-fA-F]+(\+(0x[0-9a-fA-F]+|\d+))?", spec):
        base, _, extra = spec.partition("+")
        a = int(base, 16)
        plan["start"] = a
        if extra:
            plan["stop"] = a + int(extra, 0)
        plan["label"] = model.fmt_addr(a)
        if not model.is_rel:
            hit = symbol_for_addr(model, a)
            if hit is not None:
                s, off, _exact = hit
                plan["label"] += f" ({s['name']}" + (f"+0x{off:x}" if off else "") + ")"
    else:
        found = find_symbols_by_name(model, spec)
        found = [(t, s) for t, s in found if s["ndx"] != "UND"]
        if not found:
            hint = "（用 view=\"symbols\" target=\"" + spec + "\" 搜尋）" if model.has_symbols() else "（這個檔沒有 symbol 表，請直接給 0x 位址）"
            return None, f"找不到 symbol {spec!r}{hint}"
        tname, s = found[0]
        plan.update({
            "mode": "symbol", "symbol": s["name"], "start": s["value"], "size": s["size"],
            "label": f"{s['name']} @ {model.fmt_addr(s['value'])}"
                     + (f" size 0x{s['size']:x}" if s["size"] else " (size 未知)")
                     + f" [{tname} {s['type']} {s['bind']} {model.ndx_label(s['ndx'])}]",
        })
        if s["size"] > 0:
            plan["stop"] = s["value"] + s["size"]
        if model.is_rel and not section_filter:
            sec = model.ndx_label(s["ndx"])
            plan["section"] = sec if sec not in ("UND", "ABS", "COM") else ".text"

    if model.machine == "arm" and (plan["start"] & 1):
        plan["thumb"] = True
        plan["start"] &= ~1
        if plan["stop"] is not None:
            plan["stop"] &= ~1
    elif model.machine == "arm":
        # Thumb 也可能沒有 bit0（例如 .symtab 的 FUNC symbol 已清掉）：看 mapping symbol 或 e_flags 無法判定時交給 objdump
        pass
    if plan["stop"] is None:
        plan["stop"] = plan["start"] + max(16, limit * width)
    if model.is_rel and not plan["section"]:
        plan["section"] = ".text"
    return plan, None


def _capstone_disasm(model: ElfModel, plan: Dict, limit: int) -> Tuple[bool, object]:
    try:
        import capstone as cs  # type: ignore
    except ImportError:
        return False, "未安裝（python3 -m pip install capstone）"
    m = model.machine
    cls = model.header.get("class")
    try:
        arch = None
        mode = 0
        if m == "x86_64":
            arch, mode = cs.CS_ARCH_X86, cs.CS_MODE_64
        elif m == "i386":
            arch, mode = cs.CS_ARCH_X86, cs.CS_MODE_32
        elif m == "arm":
            arch, mode = cs.CS_ARCH_ARM, (cs.CS_MODE_THUMB if plan.get("thumb") else cs.CS_MODE_ARM)
        elif m == "aarch64":
            arch = getattr(cs, "CS_ARCH_AARCH64", None) or getattr(cs, "CS_ARCH_ARM64", None)
            mode = cs.CS_MODE_ARM
        elif m == "riscv":
            arch = getattr(cs, "CS_ARCH_RISCV", None)
            mode = (getattr(cs, "CS_MODE_RISCV64", 0) if cls == 64 else getattr(cs, "CS_MODE_RISCV32", 0)) | getattr(cs, "CS_MODE_RISCVC", 0)
        elif m == "mips":
            arch = cs.CS_ARCH_MIPS
            mode = cs.CS_MODE_MIPS64 if cls == 64 else cs.CS_MODE_MIPS32
        elif m in ("ppc", "ppc64"):
            arch = cs.CS_ARCH_PPC
            mode = cs.CS_MODE_64 if cls == 64 else cs.CS_MODE_32
        elif m == "sparc":
            arch, mode = cs.CS_ARCH_SPARC, 0
        elif m == "m68k":
            arch, mode = getattr(cs, "CS_ARCH_M68K", None), 0
        elif m == "xtensa":
            arch, mode = getattr(cs, "CS_ARCH_XTENSA", None), 0
        if arch is None:
            return False, f"capstone 不支援 {model.machine_desc}"
        if not model.little_endian:
            mode |= cs.CS_MODE_BIG_ENDIAN
        md = cs.Cs(arch, mode)
    except Exception as e:
        return False, f"capstone 初始化失敗: {type(e).__name__}: {e}"

    nbytes = max(16, plan["stop"] - plan["start"])
    if model.is_rel:
        sec = model.section_by_name(plan.get("section") or ".text")
        if sec is None or sec["nobits"]:
            return False, f"找不到 section {plan.get('section')!r} 的檔案內容"
        if plan["start"] >= sec["size"]:
            return False, f"offset 0x{plan['start']:x} 超出 {sec['name']} 大小 0x{sec['size']:x}"
        data = read_file_bytes(model, sec["offset"] + plan["start"], min(nbytes, sec["size"] - plan["start"]))
    else:
        loc = addr_to_file_offset(model, plan["start"])
        if loc is None:
            return False, f"位址 {model.fmt_addr(plan['start'])} 不在任何有檔案內容的 LOAD 區段 / section"
        off, avail = loc
        data = read_file_bytes(model, off, min(nbytes, avail))
    if not data:
        return False, "讀不到位元組"
    lines: List[str] = []
    last = None
    try:
        for insn in md.disasm(data, plan["start"]):
            lines.append(f"  {insn.address:x}:  {insn.bytes.hex(' '):<24} {insn.mnemonic} {insn.op_str}".rstrip())
            last = insn.address + insn.size
            if len(lines) >= limit:
                break
    except Exception as e:
        return False, f"capstone 解碼失敗: {type(e).__name__}: {e}"
    if not lines:
        return False, "capstone 解不出指令（該位址可能不是程式碼）"
    return True, (lines, last)


def _disasm_remedies(model: ElfModel) -> List[str]:
    pkg = _DISASM_PACKAGE_HINT.get(model.machine, "對應架構的 binutils（<triplet>-objdump）或 binutils-multiarch")
    out = [
        f"  補救（擇一）：安裝 {pkg}",
        "  　　　　　　或 python3 -m pip install capstone（純 Python 綁定；支援 x86 / ARM / AArch64 / RISC-V / MIPS / PPC / SPARC / m68k；不含 ARC / Xtensa 舊版）",
        "  　　　　　　或設環境變數 AICODE_OBJDUMP=/path/to/<triplet>-objdump（MCP 重啟後生效）",
    ]
    return _finish(out)


def disassemble(model: ElfModel, target: str = "", limit: int = 0) -> Tuple[bool, List[str]]:
    """反組譯。回傳 (成功?, 行)。失敗時的行就是完整的原因說明（給報告直接用）。"""
    limit = max(1, min(int(limit) if limit else _VIEW_DEFAULT_LIMIT["disasm"], _DISASM_MAX_INSTR))
    plan, err = _disasm_plan(model, target, limit)
    if plan is None:
        return False, [f"[反組譯無法進行] {err}"]

    attempts: List[Tuple[str, str]] = []
    header = [f"目標: {plan['label']}" + (" (Thumb)" if plan.get("thumb") else "")]
    if model.is_rel:
        header[0] += f"  section: {plan['section']}（REL 檔位址為 section 內 offset）"

    for tool in _objdump_candidates(model.machine):
        cmd = [tool, "-d"]
        if plan.get("thumb") and not _has_mapping_symbols(model):
            cmd += ["-M", "force-thumb"]
        if model.is_rel and plan.get("section"):
            cmd += ["-j", plan["section"]]
        use_symbol = plan["mode"] == "symbol" and not model.is_rel
        if use_symbol:
            cmd.append(f"--disassemble={plan['symbol']}")
        else:
            cmd += [f"--start-address=0x{plan['start']:x}", f"--stop-address=0x{plan['stop']:x}"]
        cmd.append(str(model.path))
        rc, out, errtxt = _run_capture(cmd, timeout=30)
        if rc is None:
            attempts.append((tool, errtxt))
            continue
        if use_symbol and ("unrecognized option" in errtxt or "invalid option" in errtxt):
            cmd = [c for c in cmd if not c.startswith("--disassemble=")]
            cmd.insert(-1, f"--start-address=0x{plan['start']:x}")
            cmd.insert(-1, f"--stop-address=0x{plan['stop']:x}")
            rc, out, errtxt = _run_capture(cmd, timeout=30)
        lines, n, nxt = _parse_objdump_output(out, limit, start=plan["start"] if not use_symbol else None)
        if n > 0:
            body = [f"工具: {tool}"] + header + lines
            if n >= limit and nxt is not None:
                body.append(f"  … 已達 limit={limit}；續看：target=\"0x{nxt:x}\"" +
                            (f" section:{plan['section']}" if model.is_rel else ""))
            return True, body
        reason = ""
        for src in (errtxt, out):
            for ln in src.splitlines():
                ln = ln.strip()
                if ln and ("can't" in ln or "cannot" in ln or "not recognized" in ln
                           or "error" in ln.lower() or "unknown" in ln.lower()):
                    reason = ln
                    break
            if reason:
                break
        if not reason:
            reason = "沒有輸出任何指令（位址可能不在可執行區段，或 objdump 不支援此架構）"
        attempts.append((tool, reason))

    ok, res = _capstone_disasm(model, plan, limit)
    if ok:
        lines, last = res  # type: ignore[misc]
        body = ["工具: capstone"] + header + list(lines)
        if len(lines) >= limit and last is not None:
            body.append(f"  … 已達 limit={limit}；續看：target=\"0x{last:x}\"" +
                        (f" section:{plan['section']}" if model.is_rel else ""))
        return True, body
    attempts.append(("capstone", str(res)))

    out_lines = [f"[反組譯不可用] 架構 {model.machine_desc}（{plan['label']}）"]
    for tool, reason in attempts:
        out_lines.append(f"  - {tool}: {reason}")
    if not _objdump_candidates(model.machine):
        out_lines.append("  - objdump: 系統沒有任何可用的 objdump")
    out_lines.extend(_disasm_remedies(model))
    return False, out_lines


# ---------------------------------------------------------------------------
# 記憶體配置 / 向量表
# ---------------------------------------------------------------------------

def _segment_kind(seg: Dict) -> str:
    filesz, memsz = seg["filesz"], seg["memsz"]
    writable = "W" in seg["flags"]
    relocated = seg["paddr"] != seg["vaddr"]
    if relocated and filesz > 0:
        return "LMA≠VMA：開機由 LMA（FLASH）複製到 VMA（RAM）的初始化資料"
    if writable and memsz > filesz:
        return "可寫，含 zero-init（.bss 類）"
    if writable:
        return "可寫（RAM）"
    if "E" in seg["flags"]:
        return "唯讀可執行（程式碼，就地執行）"
    return "唯讀（rodata / header）"


def iter_segment_kinds(model: ElfModel):
    """(LOAD segment, 分類說明) 的 generator：memmap 逐筆取用，預算用完就不再往下拉。"""
    for seg in _iter_load_segments(model):
        yield seg, _segment_kind(seg)


def memory_accounting(model: ElfModel) -> Dict:
    """LOAD segment 的 FLASH / RAM / .bss 估算（規則明寫在報告裡）。單趟、只算總量，不建清單。"""
    acc = {"image": 0, "rom_in_place": 0, "ram": 0, "bss": 0, "init_data": 0}
    for seg in _iter_load_segments(model):
        filesz, memsz = seg["filesz"], seg["memsz"]
        writable = "W" in seg["flags"]
        relocated = seg["paddr"] != seg["vaddr"]
        acc["image"] += filesz
        if writable:
            acc["ram"] += memsz
            if relocated:
                acc["init_data"] += filesz
        else:
            acc["rom_in_place"] += memsz
        acc["bss"] += max(0, memsz - filesz)
    return acc

def section_usage(model: ElfModel) -> Dict[str, int]:
    """alloc section 依 flags 歸類：code / rodata / data / bss（bytes）。"""
    usage = {"code": 0, "rodata": 0, "data": 0, "bss": 0}
    for sec in model.sections:
        fl = sec["flags"]
        if "A" not in fl or sec["size"] <= 0 or not sec["name"]:
            continue
        if sec["nobits"]:
            usage["bss"] += sec["size"]
        elif "X" in fl:
            usage["code"] += sec["size"]
        elif "W" in fl:
            usage["data"] += sec["size"]
        else:
            usage["rodata"] += sec["size"]
    return usage


_VECTOR_SECTION_NAMES = (
    ".isr_vector", ".vectors", ".vector_table", ".intvec", ".exceptions",
    ".cs3.interrupt_vector", ".vector", ".isr_vectors", ".exception_vectors",
)
_CM_CORE_VECTORS = [
    "Initial SP", "Reset", "NMI", "HardFault", "MemManage", "BusFault", "UsageFault",
    "SecureFault/Reserved", "Reserved", "Reserved", "Reserved", "SVCall", "DebugMonitor",
    "Reserved", "PendSV", "SysTick",
]


def arm_vector_table(model: ElfModel, limit: int = 48) -> Optional[Dict]:
    """ARM Cortex-M 向量表解讀（word0 = 初始 SP、word1 = Reset，Thumb bit0=1）。

    只在 ARM 32-bit、且 word0/word1 通過合理性檢查時才回傳；否則 None（不硬猜）。
    """
    if model.machine != "arm" or model.is_rel:
        return None
    base: Optional[int] = None
    source = ""
    max_words = _VECTOR_MAX_ENTRIES
    for name in _VECTOR_SECTION_NAMES:
        sec = model.section_by_name(name)
        if sec is not None and sec["size"] >= 8 and not sec["nobits"]:
            base, source = sec["addr"], f"section {name}"
            max_words = min(max_words, sec["size"] // 4)   # 有明確的向量 section 就以它的大小為準
            break
    if base is None:
        first = min((s for s in _iter_load_segments(model) if s["filesz"] >= 8),
                    key=lambda s: s["vaddr"], default=None)
        if first is not None:
            base, source = first["vaddr"], f"最低 LOAD segment {model.fmt_addr(first['vaddr'])}"
    if base is None:
        return None
    loc = addr_to_file_offset(model, base)
    if loc is None:
        return None
    off, avail = loc
    # limit 只是「想看幾個 IRQ」；真正的上界是 section 大小 / Cortex-M 架構上限，
    # 否則 ingest 這類大 limit 會把整個 .text 讀成幾千個假 IRQ。
    count = min(16 + max(0, limit), avail // 4, max_words)
    if count < 2:
        return None
    data = read_file_bytes(model, off, count * 4)
    fmt = ("<" if model.little_endian else ">") + "I" * (len(data) // 4)
    words = list(struct.unpack(fmt, data[: (len(data) // 4) * 4]))
    if len(words) < 2:
        return None
    sp, reset = words[0], words[1]
    if sp == 0 or sp % 4 != 0 or (reset & 1) == 0:
        return None
    if segment_for_addr(model, reset & ~1) is None and section_for_addr(model, reset & ~1) is None:
        return None
    entries: List[Tuple[str, int, str]] = []
    for i, w in enumerate(words):
        name = _CM_CORE_VECTORS[i] if i < 16 else f"IRQ{i - 16}"
        sym = ""
        if i == 0:
            sec = section_for_addr(model, w)
            sym = f"in {sec['name']}" if sec else ""
        elif w:
            hit = symbol_for_addr(model, w)
            if hit is not None:
                s, o, _exact = hit
                sym = s["name"] + (f"+0x{o:x}" if o else "")
        entries.append((name, w, sym))
    return {"base": base, "source": source, "entries": entries, "sp": sp, "reset": reset}


# ---------------------------------------------------------------------------
# 渲染共用
# ---------------------------------------------------------------------------

def _fmt_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n:,} B ({n / 1024 / 1024:.2f} MiB)"
    if n >= 1024:
        return f"{n:,} B ({n / 1024:.1f} KiB)"
    return f"{n:,} B"


def _clip(s: str, n: int = _STRING_PREVIEW_CHARS) -> str:
    return s if len(s) <= n else s[:n] + "…"


def _regex_is_safe(pattern: str) -> Optional[str]:
    """target regex 是否落在安全子集；回傳 None = 安全，否則是拒絕原因（人話）。

    Python 的 re 沒有 timeout、也不釋放 GIL：一個災難性回溯的 target 會把同步的 MCP server
    整個卡住，而任何「看起來危險」的 heuristic 都能被繞過（a?a?a?…a 的可選量詞炸彈沒有
    + * {；30 個連續 (a|aa) 群組一個量詞都沒有）。所以改成正面表列，逐字元 tokenize：
      - 字面、跳脫字面、`.`、字元類別 `[...]`、錨點 `^ $ \\b \\B \\A \\Z`
      - `|` 只能在最上層（分支彼此獨立、不會互相組合），分支數 ≤ _REGEX_MAX_BRANCHES
      - **不收任何群組**：`(...)` / `(?:...)` 一律拒絕——群組串接才會產生指數級的切分方式
      - 單一 atom 的 `* +` 合計 ≤ _REGEX_MAX_UNBOUNDED、`?` 合計 ≤ _REGEX_MAX_OPTIONAL，可加 lazy `?`
      - 不接受 `{n,m}`、backreference、lookaround、inline flag
    搭配比對主體只看前 _REGEX_SUBJECT_MAX 字元，單次 re.search 的成本上界約
    起點數 × 分支數 × 主體長 × 2^可選 = 300 × 8 × 300 × 8 ≈ 6e6 步，不再依賴事後 deadline。
    """
    if len(pattern) > _REGEX_MAX_LEN:
        return f"超過 {_REGEX_MAX_LEN} 字元"
    n = len(pattern)
    i = 0
    unbounded = 0
    optional = 0
    branches = 1
    prev = ""  # atom / anchor / alt / quant / ""
    while i < n:
        c = pattern[i]
        if c == "\\":
            if i + 1 >= n:
                return "結尾的反斜線"
            nxt = pattern[i + 1]
            if nxt.isdigit():
                return "backreference（\\1）"
            prev = "anchor" if nxt in "bBAZz" else "atom"
            i += 2
            continue
        if c == "[":
            j = i + 1
            if j < n and pattern[j] == "^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                if pattern[j] == "\\":
                    j += 1
                j += 1
            if j >= n:
                return "字元類別沒有結尾 ]"
            prev = "atom"
            i = j + 1
            continue
        if c == "(":
            return "不支援群組（alternation 只能放在最上層，例如 uart_init|spi_init；要比對括號請寫 \\(）"
        if c == ")":
            return "多餘的 )（要比對括號請寫 \\)）"
        if c in "*+":
            if prev in ("quant", "alt", "anchor", ""):
                return "量詞前面沒有可重複的東西"
            unbounded += 1
            if unbounded > _REGEX_MAX_UNBOUNDED:
                return f"無界量詞（* / +）超過 {_REGEX_MAX_UNBOUNDED} 個"
            prev = "quant"
            i += 1
            if i < n and pattern[i] == "?":   # lazy
                i += 1
            continue
        if c == "?":
            if prev in ("quant", "alt", "anchor", ""):
                return "量詞前面沒有可重複的東西"
            optional += 1
            if optional > _REGEX_MAX_OPTIONAL:
                return f"可選量詞（?）超過 {_REGEX_MAX_OPTIONAL} 個"
            prev = "quant"
            i += 1
            if i < n and pattern[i] == "?":   # lazy
                i += 1
            continue
        if c == "{":
            return "不支援 {n,m} 量詞"
        if c == "|":
            branches += 1
            if branches > _REGEX_MAX_BRANCHES:
                return f"| 分支超過 {_REGEX_MAX_BRANCHES} 個"
            prev = "alt"
            i += 1
            continue
        if c in "^$":
            prev = "anchor"
            i += 1
            continue
        prev = "atom"
        i += 1
    return None


def safe_regex(text: str) -> Tuple["re.Pattern[str]", Optional[str]]:
    """target → (compiled, note)。不在安全子集或不合法就退回字面比對，note 說明原因。"""
    reason = _regex_is_safe(text)
    if reason:
        return re.compile(re.escape(text), re.I), (
            f"target {text!r} 不在安全 regex 子集內（{reason}），已改用字面比對"
        )
    try:
        return re.compile(text, re.I), None
    except re.error as e:
        return re.compile(re.escape(text), re.I), f"target {text!r} 不是合法 regex（{e}），已改用字面比對"


_MATCH_ALL = re.compile("")


def _rx(rx: "re.Pattern[str]", subject: str) -> bool:
    """regex 只看前 _REGEX_SUBJECT_MAX 個字元（超長字串 / C++ 名稱不該成為回溯的燃料）。"""
    return rx.search(subject[:_REGEX_SUBJECT_MAX]) is not None


class _Sink(list):
    """view 的行容器：累計字元數，達到預算就不再收（truncated=True）——渲染階段就有界。

    以前各 view 先完整產生再事後裁：symbols 幾萬行、strings 幾萬條會先建出幾十 MB 的
    中間資料。現在每個 view 拿一個帶字元預算的容器：
      - 放得下的行照收，**不預扣**任何空間——剛好放得下的報告一個字都不會少；
      - 第一行放不下就 truncated，之後的行只計數不存；
      - generator 來源在截斷後就不再消費（產生階段真的停止，不只是丟掉）；
      - _finish() 補說明時才從尾端回收剛好夠的空間，所以輸出總長 ≤ 預算。
    """

    def __init__(self, budget: int, initial=()):
        super().__init__()
        self.budget = max(0, int(budget))
        self.chars = 0
        self.truncated = False
        self.dropped = 0
        self.dropped_more = False   # generator 截斷後沒再消費：略過的行數只知道下限
        for x in initial:
            self.append(x)

    def _cost(self, s: str) -> int:
        # 最終是 "\n".join(lines)：只有第二行起才多一個換行，第一行不算——否則剛好放得下的
        # 報告會被多算 1 個字元而誤判截斷。
        return len(s) + (1 if len(self) else 0)

    def append(self, s: str) -> None:  # type: ignore[override]
        if self.truncated or self.chars + self._cost(s) > self.budget:
            self.truncated = True
            self.dropped += 1
            return
        self.chars += self._cost(s)
        super().append(s)

    def extend(self, items) -> None:  # type: ignore[override]
        if isinstance(items, (list, tuple)):
            for x in items:          # 已存在的小清單：全數計入 dropped（便宜）
                self.append(x)
            return
        for x in items:              # generator：截斷後不再消費，產生階段停止
            self.append(x)
            if self.truncated:
                self.dropped_more = True
                break

    def pop_last(self) -> None:
        if len(self):
            popped = super().pop()
            self.chars -= len(popped) + (1 if len(self) else 0)
            self.dropped += 1

    def exhausted(self) -> bool:
        """迴圈用：預算用完就回 True，同時記下「之後的行沒再產生」→ 說明改報「至少 N 行」。"""
        if self.truncated:
            self.dropped_more = True
        return self.truncated


def _sink(model: ElfModel, char_budget: Optional[int]) -> "_Sink":
    return _Sink(char_budget or BIN_ELF_REPORT_MAX_CHARS, _hdr_lines(model))


def _finish(out) -> List[str]:
    """截斷時補一行說明；為了讓說明放進預算，從尾端回收剛好夠的行。放得下就原樣。"""
    if isinstance(out, _Sink) and out.truncated:
        while True:
            note = (
                f"… [報告已截斷：此 view 的輸出達到 {out.budget:,} 字元上限，"
                f"已略過{'至少 ' if out.dropped_more else ' '}{out.dropped:,} 行；"
                f"用 target / limit 縮小範圍，或改用其他 view]"
            )[:_SINK_NOTE_MAX]
            if out.chars + out._cost(note) <= out.budget or not len(out):
                break
            out.pop_last()
        list.append(out, note[:out.budget] if len(note) > out.budget else note)
    return out


class _Deadline:
    """篩選迴圈的時間預算：超過就停下來並讓報告明講「結果不完整」。

    這不是硬 timeout（單次 re.search 無法中斷）；單次比對的上界靠 _regex_is_safe +
    _REGEX_SUBJECT_MAX 保證，這裡只負責「很多筆加起來太久」的情況，每一筆都檢查。
    """

    def __init__(self, seconds: Optional[float] = None):
        self.seconds = _FILTER_TIME_BUDGET if seconds is None else seconds
        self.t_end = time.monotonic() + self.seconds
        self.hit = False

    def expired(self) -> bool:
        if not self.hit and time.monotonic() > self.t_end:
            self.hit = True
        return self.hit

    def note(self) -> str:
        return f"  [WARN] 篩選超過 {self.seconds:.0f} 秒時間預算已中止，以上結果不完整；請用更窄的 target"


def _parse_filter(target: str, keys: Tuple[str, ...]) -> Tuple[Dict[str, str], Optional["re.Pattern[str]"], Optional[int], str, Optional[str]]:
    """`bind:LOCAL type:FUNC uart` → ({bind:LOCAL,type:FUNC}, regex(uart), None, 'uart', note)；`0x1234` → addr。

    note 非 None 時代表 regex 被改成字面比對（ReDoS 風險或不合法），呼叫端要印出來。
    """
    fields: Dict[str, str] = {}
    free: List[str] = []
    for tok in (target or "").split():
        k, sep, v = tok.partition(":")
        if sep and k.lower() in keys and v:
            fields[k.lower()] = v
        else:
            free.append(tok)
    text = " ".join(free).strip()
    addr: Optional[int] = None
    rx: Optional["re.Pattern[str]"] = None
    note: Optional[str] = None
    if re.fullmatch(r"0x[0-9a-fA-F]+", text):
        addr = int(text, 16)
    elif text and text != "*":
        rx, note = safe_regex(text)
    return fields, rx, addr, text, note


def _hdr_lines(model: ElfModel) -> List[str]:
    lines = [
        f"檔案: {model.path.name}",
        f"大小: {model.size:,} bytes",
        f"(parser: {model.parser_detail})",
    ]
    for w in model.warnings:
        lines.append(f"[WARN] {w}")
    if model.missing:
        lines.append("[WARN] 本次解析缺少以下能力（不是檔案沒有，是這條解析路徑拿不到）：")
        for m in model.missing:
            lines.append(f"  - {m}")
    return lines


def _section_segment_map(model: ElfModel) -> Dict[str, List[int]]:
    cached = model._lazy.get("sec_seg_map")
    if cached is not None:
        return cached  # type: ignore[return-value]
    m: Dict[str, List[int]] = {}
    for seg in model.segments:
        for name in seg["sections"]:
            m.setdefault(name, []).append(seg["idx"])
    model._lazy["sec_seg_map"] = m
    return m


def _sections_table(model: ElfModel, secs, header: bool = True):
    """Section 表（generator：同 _segments_table，預算用完就停）。"""
    seg_map = _section_segment_map(model)
    w = model.addr_width
    if header:
        yield f"  [idx] {'name':<24} {'type':<12} {'addr':<{w + 2}} {'offset':<10} {'size':<10} flags  seg"
    for sec in secs:
        segs = seg_map.get(sec["name"], [])
        seg_txt = ",".join(str(i) for i in segs[:3]) + ("…" if len(segs) > 3 else "")
        name = sec["name"] or "(null)"
        yield (
            f"  [{sec['idx']:3d}] {name[:24]:<24} {sec['type'][:12]:<12} "
            f"{model.fmt_addr(sec['addr']):<{w + 2}} 0x{sec['offset']:06x}   0x{sec['size']:06x}   "
            f"{sec['flags'] or '-':<6} {seg_txt}"
        )

def _important_sections(model: ElfModel) -> List[Dict]:
    important = {
        ".vectors", ".isr_vector", ".text", ".rodata", ".data", ".bss", ".sdata", ".sbss",
        ".init", ".fini", ".comment", ".symtab", ".dynsym", ".strtab", ".shstrtab",
        ".plt", ".got", ".got.plt", ".eh_frame", ".dynamic", ".interp", ".modinfo",
        ".init_array", ".fini_array", ".ARM.exidx", ".ARM.attributes", ".riscv.attributes",
        ".stack", ".heap", ".noinit", ".ramfunc", ".tbss", ".tdata",
    }
    return [
        sec for sec in model.sections
        if sec["name"] in important
        or sec["name"].startswith((".debug", ".note", ".rela", ".rel.", ".init.", ".exit."))
    ]


def _key_facts(model: ElfModel) -> List[str]:
    h = model.header
    out: List[str] = ["", "【Key Facts】"]
    if not h:
        out.append("  （header 解析失敗：見上方 WARN）")
        return _finish(out)
    raw = h.get("machine_raw", "")
    desc = model.machine_desc
    arch = desc if model.machine != "unknown" or not raw or raw == desc else f"{desc} [{raw}]"
    out.append(f"  Arch     : {arch} (ELF{h.get('class')}, {h.get('endian')}-endian)")

    type_str = h.get("type", "")
    extras: List[str] = []
    dyn = model.dynamic
    if dyn and dyn.get("is_pie"):
        extras.append("PIE")
    elif type_str == "DYN" and dyn and dyn.get("soname"):
        extras.append("shared library")
    elif type_str == "DYN" and not dyn:
        extras.append("DYN without .dynamic")
    if model.modinfo is not None or model.path.suffix.lower() == ".ko":
        extras.append("kernel module")
    elif type_str == "REL":
        extras.append("relocatable object")
    elif type_str == "EXEC" and not dyn:
        extras.append("static / bare-metal image")
    out.append(f"  Type     : {type_str}{' (' + ', '.join(extras) + ')' if extras else ''}")

    entry_txt = model.fmt_addr(model.entry)
    if type_str == "REL":
        entry_txt += " (REL 檔沒有意義)"
    else:
        if model.machine == "arm" and (model.entry & 1):
            entry_txt += " (Thumb)"
        hit = symbol_for_addr(model, model.entry)
        if hit is not None:
            s, off, _ = hit
            entry_txt += f" → {s['name']}" + (f"+0x{off:x}" if off else "")
        sec = section_for_addr(model, model.entry)
        if sec is not None:
            entry_txt += f" in {sec['name']}"
    out.append(f"  Entry    : {entry_txt}")

    has_symtab = bool(model.symtabs.get(".symtab"))
    has_dynsym = bool(model.symtabs.get(".dynsym"))
    if has_symtab:
        stripped = "no (.symtab present)"
    elif has_dynsym:
        stripped = "yes (.symtab absent, .dynsym only)"
    elif "symbols" in model.failed:
        stripped = f"unknown（symbol table 讀取失敗：{model.failed['symbols']}）"
    else:
        stripped = "fully stripped (no symbol tables)"
    out.append(f"  Stripped : {stripped}")

    if not dyn and "dynamic" in model.failed:
        out.append(f"  Linkage  : unknown（.dynamic 讀取失敗：{model.failed['dynamic']}）")
    elif dyn:
        needed = dyn.get("needed", [])
        if needed:
            preview = ", ".join(needed[:3]) + (f" + {len(needed) - 3} more" if len(needed) > 3 else "")
            linkage = f"dynamic, {len(needed)} NEEDED ({preview})"
        else:
            linkage = "dynamic, no DT_NEEDED"
        out.append(f"  Linkage  : {linkage}")
    else:
        out.append("  Linkage  : static (no .dynamic section)")

    tname, syms = model.primary_symtab()
    if syms:
        st = symtab_stats(syms)
        out.append(
            f"  Symbols  : {tname} {st['total']} 個；FUNC {st['func']}"
            f"（GLOBAL {st['func_global']} / WEAK {st['func_weak']} / LOCAL {st['func_local']}）"
            f"、OBJECT {st['object']}、UND {st['und']}、size=0 的 FUNC/OBJECT {st['zero_size']}"
            + (f"、mapping symbol {st['mapping']}" if st["mapping"] else "")
        )
    if has_dynsym:
        dsyms = model.symtabs[".dynsym"]
        n_imp = sum(1 for s in dsyms if s["ndx"] == "UND" and s["name"])
        n_exp = sum(1 for s in dsyms if s["ndx"] not in ("UND", "ABS") and s["bind"] in ("GLOBAL", "WEAK") and s["type"] in ("FUNC", "OBJECT"))
        out.append(f"  Dynsym   : {n_imp} imports (UND) / {n_exp} exports (defined)")

    if model.relocs:
        total = sum(r["count"] for r in model.relocs)
        out.append(f"  Relocs   : {total:,} entries in {len(model.relocs)} sections（view=\"relocs\"）")
    elif "relocs" in model.failed:
        out.append(f"  Relocs   : unknown（讀取失敗：{model.failed['relocs']}）")
    else:
        out.append("  Relocs   : none")

    dw = model.dwarf
    if dw.get("unknown"):
        dtxt = f"unknown（section 表讀取失敗：{model.failed.get('sections', '')}）"
    elif dw.get("debug_info"):
        cus = dwarf_cus(model)
        dtxt = f"present ({len(cus)} CU" + (")" if cus else "; .debug_info 存在但解析不到 CU)")
        if model._lazy.get("dwarf_error"):
            dtxt += f" [解析錯誤: {model._lazy['dwarf_error']}]"
    elif dw.get("present"):
        dtxt = f"只有 {', '.join(dw['sections'][:4])}（無 .debug_info → 無函式 / 行號 / 型別）"
    else:
        dtxt = "absent"
    out.append(f"  DWARF    : {dtxt}")

    if model.build_id:
        out.append(f"  Build-id : {model.build_id}")

    if "segments" in model.failed:
        out.append(f"  Memory   : unknown（program header 讀取失敗：{model.failed['segments']}）")
    elif _n_load_segments(model):
        acc = memory_accounting(model)
        out.append(
            f"  Memory   : image(LOAD filesz) {_fmt_bytes(acc['image'])}；"
            f"RAM(可寫 memsz) {_fmt_bytes(acc['ram'])}；zero-init {_fmt_bytes(acc['bss'])}"
            f"（估算規則見 view=\"memmap\"）"
        )
    return _finish(out)


def _elf_header_block(model: ElfModel) -> List[str]:
    h = model.header
    if not h:
        if "header" in model.failed:
            return ["", f"【ELF Header】讀取失敗：{model.failed['header']}"]
        return []
    out = ["", "【ELF Header】"]
    out.append(f"  Class: ELF{h.get('class')}")
    out.append(f"  Data: {h.get('endian')} endian")
    out.append(f"  OS/ABI: {h.get('osabi', '')}")
    out.append(f"  Type: {h.get('type_desc') or h.get('type', '')}")
    out.append(f"  Machine: {model.machine_desc}" + (f" [{h.get('machine_raw')}]" if model.machine == "unknown" and h.get("machine_raw") and h.get("machine_raw") != model.machine_desc else ""))
    out.append(f"  Entry point address: {model.fmt_addr(model.entry)}")
    out.append(f"  Flags: 0x{int(h.get('flags', 0)):x}")
    out.append(f"  Number of program headers: {h.get('phnum', 0)}")
    out.append(f"  Number of section headers: {h.get('shnum', 0)}")
    if model.build_id:
        out.append(f"  GNU build-id: {model.build_id}")
    if model.note_names:
        out.append(f"  Notes: {', '.join(model.note_names[:8])}")
    return _finish(out)


def _segments_table(model: ElfModel, max_rows: Optional[int] = None):
    """Program header 表（generator：讓 _Sink 在預算用完後停止消費，不先建整份 list）。"""
    if not model.segments:
        if "segments" in model.failed:
            yield ""
            yield f"【Program Headers】讀取失敗：{model.failed['segments']}"
        return
    w = model.addr_width
    yield ""
    yield "【Program Headers】" + (f"（{len(model.segments)} 個）" if max_rows is None else "")
    yield f"  {'#':<3} {'type':<14} {'offset':<10} {'vaddr(VMA)':<{w + 2}} {'paddr(LMA)':<{w + 2}} {'filesz':<10} {'memsz':<10} flg align"
    rows = model.segments if max_rows is None else model.segments[:max_rows]
    for seg in rows:
        yield (
            f"  {seg['idx']:<3} {seg['type'][:14]:<14} 0x{seg['offset']:06x}   "
            f"{model.fmt_addr(seg['vaddr']):<{w + 2}} {model.fmt_addr(seg['paddr']):<{w + 2}} "
            f"0x{seg['filesz']:06x}   0x{seg['memsz']:06x}   {seg['flags']} 0x{seg['align']:x}"
        )
    if max_rows is not None and len(model.segments) > max_rows:
        yield f"  ... (共 {len(model.segments)} 個；view=\"headers\" 看全部)"

def _dynamic_block(model: ElfModel) -> List[str]:
    facts = model.dynamic
    if not facts:
        if "dynamic" in model.failed:
            return ["", f"【.dynamic】讀取失敗：{model.failed['dynamic']}（不是沒有 .dynamic）"]
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
    out.append(f"  （共 {len(facts.get('tags', []))} 個 tag；view=\"dynamic\" 看全部）")
    return _finish(out)


def _comment_block(model: ElfModel) -> List[str]:
    if not model.comment:
        return []
    out = ["", "【.comment（編譯器資訊）】"]
    for line in model.comment[:10]:
        out.append(f"  {line}")
    return _finish(out)


def _entry_block(model: ElfModel, n_instr: int = 16) -> List[str]:
    if model.is_rel:
        return []
    out: List[str] = []
    sec = section_for_addr(model, model.entry)
    loc = addr_to_file_offset(model, model.entry)
    line = f"Entry point {model.fmt_addr(model.entry)}"
    if sec is not None:
        line += f" 位於 section {sec['name']}"
    if loc is not None:
        line += f" (file offset ≈ 0x{loc[0]:x})"
    out.extend(["", line])
    ok, lines = disassemble(model, "", n_instr)
    out.append("")
    out.append("【Entry 反組譯】" + ("" if ok else "（失敗，原因如下）"))
    out.extend(lines)
    if ok:
        out.append(f"  （view=\"disasm\" target=<symbol|0x位址> limit=N 看更多）")
    return _finish(out)


def demangle_names(model: ElfModel, names: List[str]) -> Dict[str, str]:
    """C++ mangled 名稱（_Z…）→ 可讀名稱；用 binutils c++filt 批次處理，結果快取在 model 上。"""
    cache: Dict[str, str] = model._lazy.setdefault("demangle", {})  # type: ignore[assignment]
    todo = sorted({n for n in names if n and n.startswith("_Z") and n not in cache})
    if todo and cmd_exists("c++filt"):
        try:
            res = subprocess.run(
                ["c++filt"], input="\n".join(todo) + "\n", capture_output=True, text=True,
                timeout=20, encoding="utf-8", errors="replace", env=_subprocess_env(),
            )
            outs = (res.stdout or "").splitlines()
            if len(outs) == len(todo):
                for n, d in zip(todo, outs):
                    d = d.strip()
                    cache[n] = d if d and d != n else ""
        except Exception:
            pass
    for n in todo:
        cache.setdefault(n, "")
    return cache


def _symbol_row(model: ElfModel, s: Dict) -> str:
    name = s["name"]
    if len(name) > 70:
        name = name[:70] + "…"
    dem = model._lazy.get("demangle", {}).get(s["name"], "") if s["name"].startswith("_Z") else ""
    if dem:
        name += f"  ⇒ {_clip(dem, 90)}"
    return (f"  {model.fmt_addr(s['value'])} {s['size']:>7}  {s['type'][:7]:<7} {s['bind'][:6]:<6} "
            f"{model.ndx_label(s['ndx'])[:14]:<14} {name}")


def _symbols_summary(model: ElfModel, max_funcs: int, max_objs: int) -> List[str]:
    out: List[str] = []
    if not model.has_symbols():
        if "symbols" in model.failed:
            out.extend(["", f"【Symbols】symbol table 讀取失敗：{model.failed['symbols']}（不能當成 stripped）"])
        else:
            out.extend(["", "【Symbols】沒有 symbol table（fully stripped）。"
                            "可用 view=\"imports\"（若有 .dynsym）、view=\"strings\"、view=\"disasm\" target=0x位址。"])
        return _finish(out)
    order = [k for k in (".symtab", ".dynsym") if model.symtabs.get(k)] + \
            [k for k in model.symtabs if k not in (".symtab", ".dynsym") and model.symtabs[k]]
    for tname in order:
        syms = model.symtabs[tname]
        st = symtab_stats(syms)
        out.append("")
        out.append(
            f"【Symbols / {tname}】總數 {st['total']}；FUNC {st['func']}"
            f"（GLOBAL {st['func_global']} / WEAK {st['func_weak']} / LOCAL {st['func_local']}）"
            f"；OBJECT {st['object']}；UND {st['und']}；size=0 的 FUNC/OBJECT {st['zero_size']}"
            + (f"；mapping symbol {st['mapping']}" if st["mapping"] else "")
        )
        defined = [
            s for s in syms
            if s["ndx"] not in ("UND", "ABS", "COM") and s["name"] and not is_mapping_symbol(s["name"])
        ]
        funcs = [s for s in defined if s["type"] == "FUNC" and s["size"] > 0]
        objs = [s for s in defined if s["type"] == "OBJECT" and s["size"] > 0]
        if funcs:
            top = heapq.nlargest(max_funcs, funcs, key=lambda s: s["size"])
            demangle_names(model, [s["name"] for s in top])
            out.append("")
            out.append(f"Top {len(top)} functions in {tname}（by size；含 LOCAL/static）:")
            out.append(f"  {'addr':<{model.addr_width + 2}} {'size':>7}  {'type':<7} {'bind':<6} {'section':<14} name")
            for s in top:
                out.append(_symbol_row(model, s))
        n_zero = sum(1 for s in defined if s["type"] == "FUNC" and s["size"] == 0)
        if n_zero:
            first = heapq.nsmallest(10, (s for s in defined if s["type"] == "FUNC" and s["size"] == 0),
                                    key=lambda s: s["value"])
            names = ", ".join(s["name"] for s in first)
            out.append("")
            out.append(f"size=0 的 FUNC symbol {n_zero} 個（多為組語 label / 進入點；依位址）: {names}"
                       + (f", … +{n_zero - 10}" if n_zero > 10 else ""))
        if objs:
            top = heapq.nlargest(max_objs, objs, key=lambda s: s["size"])
            out.append("")
            out.append(f"Top {len(top)} objects in {tname}（by size）:")
            out.append(f"  {'addr':<{model.addr_width + 2}} {'size':>7}  {'type':<7} {'bind':<6} {'section':<14} name")
            for s in top:
                out.append(_symbol_row(model, s))
    out.append("")
    out.append("  完整列表 / 篩選：view=\"symbols\"，target 例：\"uart\"、\"bind:LOCAL type:FUNC\"、\"ndx:UND\"、\"0x08001234\"（查位址落在哪個 symbol）")
    return _finish(out)


def _collect_imports(model: ElfModel) -> Tuple[str, List[str]]:
    dsyms = model.symtabs.get(".dynsym")
    if dsyms:
        names = [s["name"] for s in dsyms if s["ndx"] == "UND" and s["name"] and s["type"] in ("FUNC", "OBJECT", "NOTYPE")]
        return ".dynsym", names
    ssyms = model.symtabs.get(".symtab")
    if ssyms:
        names = [s["name"] for s in ssyms if s["ndx"] == "UND" and s["name"] and not is_mapping_symbol(s["name"])]
        return ".symtab", names
    return "", []


def _reloc_ref_counts(model: ElfModel) -> Counter:
    cached = model._lazy.get("reloc_refs")
    if cached is not None:
        return cached  # type: ignore[return-value]
    c: Counter = Counter()
    for r in model.relocs:
        for e in r["entries"]:
            if e["sym"]:
                c[e["sym"]] += 1
    model._lazy["reloc_refs"] = c
    return c


def _imports_block(model: ElfModel, per_cat: int = 8, title: bool = True) -> List[str]:
    table, imports = _collect_imports(model)
    if not imports:
        if "symbols" in model.failed:
            return ["", f"【Imports】symbol table 讀取失敗：{model.failed['symbols']}（無法判斷外部參照）"]
        return []
    refs = _reloc_ref_counts(model)
    by_family = categorize_imports(sorted(set(imports)))
    src = "UND 於 .dynsym" if table == ".dynsym" else "UND 於 .symtab（.o/.ko 的外部參照）"
    out: List[str] = ["", f"【Imports（{src}，按 API 家族分類）】共 {len(set(imports))} 個"]
    family_order = [f for f, _ in _IMPORT_API_CATEGORIES] + ["other"]
    for family in family_order:
        items = by_family.get(family)
        if not items:
            continue
        if refs:
            items = sorted(items, key=lambda n: (-refs.get(n, 0), n))
        shown = items[:per_cat]
        more = len(items) - len(shown)
        sample = ", ".join(f"{n}(×{refs[n]})" if refs.get(n) else n for n in shown)
        if more > 0:
            sample += f", ... +{more}"
        out.append(f"  [{family}] ({len(items)}) {sample}")
    if refs:
        out.append("  （×N = relocation 引用次數）")
    if title:
        out.append("  完整列表：view=\"imports\"；哪個函式用了它：view=\"relocs\" target=<symbol>")
    return _finish(out)


def _reloc_callers_index(model: ElfModel, applies_to: str) -> Tuple[List[int], List[Dict]]:
    key = f"callers:{applies_to}"
    cached = model._lazy.get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    sec = model.section_by_name(applies_to)
    _, syms = model.primary_symtab()
    picked: List[Dict] = []
    if sec is not None:
        idx = str(sec["idx"])
        picked = [s for s in syms if s["ndx"] == idx and s["type"] in ("FUNC", "OBJECT", "NOTYPE")
                  and s["name"] and not is_mapping_symbol(s["name"])]
        picked.sort(key=lambda s: (s["value"], -s["size"]))
    addrs = [s["value"] for s in picked]
    model._lazy[key] = (addrs, picked)
    return addrs, picked


def _reloc_caller(model: ElfModel, applies_to: str, offset: int) -> str:
    addrs, syms = _reloc_callers_index(model, applies_to)
    if not syms:
        return ""
    i = bisect.bisect_right(addrs, offset) - 1
    j = i
    while j >= 0 and i - j < 8:
        s = syms[j]
        if s["size"] > 0 and s["value"] <= offset < s["value"] + s["size"]:
            return s["name"] + (f"+0x{offset - s['value']:x}" if offset != s["value"] else "")
        j -= 1
    if i >= 0:
        s = syms[i]
        return s["name"] + f"+0x{offset - s['value']:x}?"
    return ""


def _relocs_summary(model: ElfModel, top: int = 12) -> List[str]:
    if not model.relocs:
        if "relocs" in model.failed:
            return ["", f"【Relocations】讀取失敗：{model.failed['relocs']}（不能當成沒有 relocation）"]
        return []
    total = sum(r["count"] for r in model.relocs)
    out: List[str] = ["", f"【Relocations】共 {total:,} 筆，{len(model.relocs)} 個 section"]
    for r in model.relocs[:20]:
        types = ", ".join(f"{t} {n}" for t, n in r["by_type"].most_common(4))
        if len(r["by_type"]) > 4:
            types += f", … +{len(r['by_type']) - 4} 種"
        tgt = f" → {r['applies_to']}" if r["applies_to"] else ""
        out.append(f"  {r['section']}{tgt}: {r['count']:,} 筆（{types}）")
    if len(model.relocs) > 20:
        out.append(f"  ... +{len(model.relocs) - 20} 個 section")
    refs = _reloc_ref_counts(model)
    if refs:
        und: set = set()
        for syms in model.symtabs.values():
            for s in syms:
                if s["ndx"] == "UND" and s["name"]:
                    und.add(s["name"])
        out.append(f"  被引用最多的 symbol（前 {min(top, len(refs))}；UND = 外部）:")
        for name, n in refs.most_common(top):
            out.append(f"    {n:>6}  {name}{'  [UND]' if name in und else ''}")
    if model.reloc_entry_cap_hit:
        out.append(f"  （項目超過 {_RELOC_ENTRY_CAP:,} 筆的 section 只保留前 {_RELOC_ENTRY_CAP:,} 筆；統計為全量）")
    out.append("  逐筆 / 呼叫關係：view=\"relocs\" target=<symbol regex>（會標出 caller 函式）")
    return _finish(out)


def _dwarf_summary(model: ElfModel, max_cus: int = 20) -> List[str]:
    dw = model.dwarf
    if dw.get("unknown"):
        return ["", f"【DWARF】無法判斷有沒有 debug section：section 表讀取失敗（{model.failed.get('sections', '')}）"]
    if not dw.get("present"):
        return []
    out: List[str] = [""]
    if not dw.get("debug_info"):
        out.append(f"【DWARF】只有 {', '.join(dw['sections'])}（無 .debug_info）：沒有 CU / 函式 / 行號 / 型別資訊")
        return _finish(out)
    cus = dwarf_cus(model)
    if not cus:
        msg = model._lazy.get("dwarf_error") or "解析不到任何 CU（可能是壓縮 debug section、split DWARF，或格式不支援）"
        out.append(f"【DWARF】.debug_info 存在但 {msg}")
        return _finish(out)
    with_code = sum(1 for c in cus if c["low_pc"] is not None)
    langs = Counter(c["language"] for c in cus if c["language"])
    producers = Counter((c["producer"].split(" -")[0].strip()) for c in cus if c["producer"])
    out.append(f"【DWARF 編譯單元】共 {len(cus)} 個 CU（{with_code} 個含程式碼）"
               + (f"；語言 {', '.join(f'{k}×{v}' for k, v in langs.most_common(3))}" if langs else "")
               + (f"；producer {', '.join(f'{k}' for k, _ in producers.most_common(2))}" if producers else ""))
    for c in cus[:max_cus]:
        name = c["name"]
        if name and not name.startswith("/") and c["comp_dir"]:
            name = f"{c['comp_dir'].rstrip('/')}/{name}"
        rng = f"  [{model.fmt_addr(c['low_pc'])}-{model.fmt_addr(c['high_pc'])}]" if c["low_pc"] is not None and c["high_pc"] is not None else ""
        out.append(f"  {name}{rng}")
    if len(cus) > max_cus:
        out.append(f"  ... (還有 {len(cus) - max_cus} 個)")
    caps: List[str] = []
    if model.caps.get("dwarf_functions"):
        caps.append("target=<函式 regex> 列函式（low/high pc、來源檔:行）")
    if model.caps.get("dwarf_types"):
        caps.append("同一個 target 也會列出符合的 struct/union/enum/typedef 與成員")
    if model.caps.get("dwarf_lines"):
        caps.append("target=0x位址 → 對應來源檔:行與函式")
    out.append("  深入：view=\"dwarf\"，" + "；".join(caps))
    return _finish(out)


def _strings_summary(model: ElfModel, max_strings: int) -> List[str]:
    items = model_strings(model)
    if not items:
        return ["", "【字串】沒有掃到有意義的可讀字串"]
    n_ascii = sum(1 for it in items if it["enc"] == "ascii")
    n_utf16 = len(items) - n_ascii
    counts: Counter = Counter()
    other = 0
    per_cat = max(5, max_strings // 8)
    samples: Dict[str, List[Dict]] = {c: [] for c in STRING_CATEGORIES}
    for it in items:                       # 單趟：計數 + 每類只留前幾筆樣本，不建整份分類清單
        cats = it["cats"]
        if not cats:
            other += 1
            continue
        for c in cats:
            counts[c] += 1
        if "version" in cats:
            if len(samples["version"]) < max_strings:
                samples["version"].append(it)
        else:
            for c in cats:
                if len(samples[c]) < per_cat:
                    samples[c].append(it)
    out: List[str] = [""]
    out.append(
        f"【字串分類】共 {len(items):,} 個可讀字串（ASCII {n_ascii:,} / UTF-16LE {n_utf16:,}"
        + ("，已達掃描上限" if model._lazy.get("strings_capped") else "") + "）；"
        + " / ".join(f"{c} {counts.get(c, 0)}" for c in STRING_CATEGORIES) + f" / other {other}"
    )

    def _fmt(it: Dict) -> str:
        sec = f" [{it['section']}]" if it["section"] else ""
        enc = " (u16)" if it["enc"] == "utf16" else ""
        return f"  0x{it['offset']:08x}{sec}{enc} {_clip(it['text'])}"

    if samples["version"]:
        out.append("")
        out.append(f"[{_STRING_CATEGORY_LABEL['version']}] ({len(samples['version'])}/{counts['version']})")
        for it in samples["version"]:
            out.append(_fmt(it))
    for cat in ("diagnostic", "format", "url", "path", "command", "config"):
        picked = samples[cat]
        if not picked:
            continue
        out.append("")
        out.append(f"[{_STRING_CATEGORY_LABEL[cat]}] ({len(picked)}/{counts[cat]})")
        for it in picked:
            out.append(_fmt(it))
    out.append("")
    out.append("  完整清單：view=\"strings\"，target 例：\"cat:diagnostic\"、\"section:.rodata\"、\"cat:path min:12\"、任意 regex")
    return _finish(out)


def _footer(model: ElfModel) -> List[str]:
    out = ["", "【深入查看（analyze_file 的 view / target / limit 參數）】"]
    for v in VIEWS:
        if v == "summary":
            continue
        out.append(f"  view=\"{v}\"：{VIEW_HELP[v]}")
    return _finish(out)


def _memmap_lines(model: ElfModel, limit: int, compact: bool):
    """記憶體配置（generator：直接串流進 _Sink，預算用完就停，不先建整份 list）。"""
    if "segments" in model.failed:
        yield ("")
        yield (f"【記憶體配置】program header 讀取失敗：{model.failed['segments']}（不能當成沒有 LOAD segment）")
        return
    sec_failed = model.failed.get("sections")
    n_load = _n_load_segments(model)
    if model.is_rel or not n_load:
        yield ("")
        if sec_failed:
            yield (f"【記憶體配置】沒有 LOAD segment，且 section 表讀取失敗：{sec_failed}（section 歸類無法計算）")
            return
        usage = section_usage(model)
        yield ("【記憶體配置】REL 檔（.o / .ko）沒有 LOAD segment；section 依 flags 歸類："
                   f"code {_fmt_bytes(usage['code'])}、rodata {_fmt_bytes(usage['rodata'])}、"
                   f"data {_fmt_bytes(usage['data'])}、bss(NOBITS) {_fmt_bytes(usage['bss'])}")
        if not compact:
            secs = [s for s in model.sections if "A" in s["flags"] and s["size"] > 0 and s["name"]]
            yield ("")
            yield from (_sections_table(model, heapq.nlargest(max(limit, 200), secs, key=lambda s: s["size"])))
        return

    acc = memory_accounting(model)
    usage = section_usage(model)
    w = model.addr_width
    yield ("")
    yield ("【記憶體配置（LOAD segments）】")
    yield (f"  {'#':<3} {'VMA(vaddr)':<{w + 2}} {'LMA(paddr)':<{w + 2}} {'filesz':<10} {'memsz':<10} flg  說明")
    max_segs = 32 if compact else max(limit, 256)
    for si, (seg, kind) in enumerate(iter_segment_kinds(model)):
        if si >= max_segs:
            yield (f"  ... +{n_load - si} 個 LOAD segment（view=\"memmap\" limit=N）")
            break
        yield (
            f"  {seg['idx']:<3} {model.fmt_addr(seg['vaddr']):<{w + 2}} {model.fmt_addr(seg['paddr']):<{w + 2}} "
            f"0x{seg['filesz']:06x}   0x{seg['memsz']:06x}   {seg['flags']}  {kind}"
        )
        if not compact and seg["sections"]:
            names = seg["sections"]
            yield (f"      sections: {' '.join(names[:64])}" + (f" …+{len(names) - 64}" if len(names) > 64 else ""))
    yield (
        f"  估算：image(FLASH/檔案內) = Σ LOAD filesz = {_fmt_bytes(acc['image'])}；"
        f"RAM = Σ 可寫 LOAD memsz = {_fmt_bytes(acc['ram'])}"
        + (f"（其中開機需從 LMA 複製的初始化資料 {_fmt_bytes(acc['init_data'])}）" if acc["init_data"] else "")
        + f"；zero-init(.bss 類) = Σ(memsz−filesz) = {_fmt_bytes(acc['bss'])}；"
        f"唯讀就地執行 = {_fmt_bytes(acc['rom_in_place'])}"
    )
    if sec_failed:
        yield (f"  section 歸類：unknown（section 表讀取失敗：{sec_failed}）")
    else:
        yield (
            f"  section 歸類：code {_fmt_bytes(usage['code'])}、rodata {_fmt_bytes(usage['rodata'])}、"
            f"data {_fmt_bytes(usage['data'])}、bss {_fmt_bytes(usage['bss'])}"
        )
    if not compact:
        yield ("  （Linux 使用者程式 LMA=VMA 是正常的；上面的 FLASH/RAM 說法只對 bare-metal 韌體有意義）")

    vt = arm_vector_table(model, limit if not compact else 16)
    if vt is not None:
        yield ("")
        yield (f"【Cortex-M 向量表】@ {model.fmt_addr(vt['base'])}（{vt['source']}）")
        entries = vt["entries"]
        shown = entries if not compact else entries[:16]
        i = 0
        while i < len(shown):
            name, w_, sym = shown[i]
            # 連續相同 handler 的 IRQ 折疊
            j = i
            if i >= 16:
                while j + 1 < len(shown) and shown[j + 1][1] == w_ and j + 1 >= 16:
                    j += 1
            label = name if j == i else f"{name}..{shown[j][0]}"
            val = model.fmt_addr(w_) if i > 0 else model.fmt_addr(w_)
            yield (f"  {label:<22} {val}  {sym}")
            i = j + 1
        if compact and len(entries) > 16:
            yield (f"  ... IRQ 向量 {len(entries) - 16} 個（view=\"memmap\" limit=N 看更多）")
    elif model.machine == "arm" and not compact:
        yield ("")
        yield ("  （未偵測到 Cortex-M 向量表：word0 不像初始 SP 或 word1 不是 Thumb 位址；A-profile / Linux ELF 屬正常）")
    return


# ---------------------------------------------------------------------------
# 各 view
# ---------------------------------------------------------------------------

def _limit_for(view: str, limit: int, hard_max: Optional[int] = None,
               char_budget: Optional[int] = None) -> int:
    """limit=0 → 該 view 預設；上限 = min(hard_max 或 BIN_ELF_VIEW_MAX_LIMIT, 字元預算 // 最短行長 + 1)。

    字元預算能放的行數就是筆數的天花板：再多的筆數也印不出來，只會讓候選 heap / 清單白白長大。
    """
    default = _VIEW_DEFAULT_LIMIT.get(view, 200)
    ceiling = int(hard_max) if hard_max else BIN_ELF_VIEW_MAX_LIMIT
    budget = int(char_budget) if char_budget else BIN_ELF_REPORT_MAX_CHARS
    ceiling = min(ceiling, budget // _MIN_LINE_CHARS.get(view, 10) + 1)
    if not limit or limit <= 0:
        return max(1, min(default, ceiling))
    return max(1, min(int(limit), ceiling))


def view_summary(model: ElfModel, limit: int = 0, footer: bool = True, hard_max: Optional[int] = None,
                 char_budget: Optional[int] = None) -> List[str]:
    blocks = [
        lambda: _key_facts(model),
        lambda: _elf_header_block(model),
        lambda: _memmap_lines(model, 16, compact=True),
        lambda: _segments_table(model, max_rows=12),
        lambda: _dynamic_block(model),
        lambda: _summary_sections(model, limit or BIN_ELF_MAX_SECTIONS),
        lambda: format_modinfo(model.modinfo) if model.modinfo else [],
        lambda: _entry_block(model, 16),
        lambda: _comment_block(model),
        lambda: _symbols_summary(model, BIN_ELF_MAX_FUNCS, BIN_ELF_MAX_OBJS),
        lambda: _imports_block(model, 8),
        lambda: _relocs_summary(model, 12),
        lambda: _dwarf_summary(model, 20),
        lambda: _strings_summary(model, BIN_ELF_MAX_STRINGS),
    ]
    if footer:
        blocks.append(lambda: _footer(model))
    out = _sink(model, char_budget)
    if footer:
        out.append("（深入：view=symbols / strings / dwarf / relocs / disasm / sections / memmap / imports / dynamic / headers；"
                   "target 指定 symbol、0x位址、regex 或 key:value 篩選；limit 控制筆數）")
    for fn in blocks:
        try:
            out.extend(fn())
        except Exception as e:  # 單段失敗不拖垮整份報告，但要講
            out.append(f"[WARN] 報告段落產生失敗: {type(e).__name__}: {e}")
    return _finish(out)


def _summary_sections(model: ElfModel, max_sections: int) -> List[str]:
    if not model.sections:
        if "sections" in model.failed:
            return ["", f"【Sections】section 表讀取失敗：{model.failed['sections']}（不能當成沒有 section）"]
        return []
    picked = _important_sections(model)[:max_sections]
    out = ["", f"【Sections】({len(picked)}/{len(model.sections)} 個；view=\"sections\" 看全部)"]
    out.extend(_sections_table(model, picked))
    return _finish(out)


def view_headers(model: ElfModel, limit: int = 0, hard_max: Optional[int] = None,
                 char_budget: Optional[int] = None) -> List[str]:
    out = _sink(model, char_budget)
    out.extend(_key_facts(model))
    out.extend(_elf_header_block(model))
    out.extend(_segments_table(model))
    if model.segments:
        out.append("")
        out.append("  Section → Segment:")
        for seg in model.segments:
            if out.exhausted():
                break
            names = seg["sections"]
            out.append(f"    {seg['idx']:02d} {seg['type']:<12} {' '.join(names[:100])}"
                       + (f" …+{len(names) - 100}" if len(names) > 100 else ""))
    out.extend(_comment_block(model))
    if model.modinfo:
        out.extend(format_modinfo(model.modinfo))
    return _finish(out)


def _hexdump(data: bytes, base: int, width: int = 16) -> List[str]:
    lines: List[str] = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {base + i:08x}  {hex_part:<{width * 3}}  |{ascii_part}|")
    return lines


def view_sections(model: ElfModel, target: str = "", limit: int = 0, hard_max: Optional[int] = None,
                  char_budget: Optional[int] = None) -> List[str]:
    out = _sink(model, char_budget)
    if not model.sections:
        if "sections" in model.failed:
            out.append(f"【Sections】section 表讀取失敗：{model.failed['sections']}（不能當成沒有 section）")
        else:
            out.append("沒有 section header（可能被 strip 掉）")
        return _finish(out)
    fields, rx, addr, text, note = _parse_filter(target, ("dump",))
    if note:
        out.append(f"[注意] {note}")
    if not text:
        n = _limit_for("sections", limit, hard_max, char_budget)
        out.append("")
        out.append(f"【Sections（全部 {len(model.sections)} 個）】flags: W=write A=alloc X=exec M=merge S=strings I=info L=link-order G=group T=TLS C=compressed")
        out.extend(_sections_table(model, model.sections[:n]))
        if len(model.sections) > n:
            out.append(f"  ... +{len(model.sections) - n}（limit=N 看更多）")
        out.append("")
        out.append(f"  單一 section：target=<name>（hex dump + 字串 + 內含 symbol；limit=bytes，預設 {_SECTION_DUMP_DEFAULT}、上限 {_SECTION_DUMP_MAX}）；target=0x位址 → 找到所在 section 並從該位址 dump")
        return _finish(out)

    dump_n = _SECTION_DUMP_DEFAULT if not limit else max(16, min(int(limit), _SECTION_DUMP_MAX))
    sec: Optional[Dict] = None
    start_addr: Optional[int] = None
    if addr is not None:
        if model.is_rel:
            out.append(f"REL 檔的 section 位址都是 0；請用 target=<section 名>")
            return _finish(out)
        sec = section_for_addr(model, addr)
        if sec is None:
            seg = segment_for_addr(model, addr)
            out.append(f"位址 {model.fmt_addr(addr)} 不在任何 alloc section 內"
                       + (f"（在 LOAD segment #{seg['idx']} 內，可能是 header/padding）" if seg else "（也不在任何 LOAD segment）"))
            return _finish(out)
        start_addr = addr
    else:
        sec = model.section_by_name(text)
        if sec is None:
            cands = [s["name"] for s in model.sections if s["name"] and (rx is not None and _rx(rx, s["name"]))]
            out.append(f"找不到 section {text!r}" + (f"；名稱相符的有：{', '.join(cands[:20])}" if cands else ""))
            return _finish(out)
        start_addr = sec["addr"]

    seg_map = _section_segment_map(model)
    out.append("")
    out.append(f"【Section {sec['name']}】")
    out.append(f"  idx {sec['idx']}  type {sec['type']}  flags {sec['flags'] or '-'}  addr {model.fmt_addr(sec['addr'])}  "
               f"offset 0x{sec['offset']:x}  size 0x{sec['size']:x} ({sec['size']:,} B)  align {sec['align']}"
               + (f"  link {sec['link']} info {sec['info']}" if sec["link"] or sec["info"] else "")
               + (f"  segments {seg_map.get(sec['name'])}" if seg_map.get(sec["name"]) else ""))
    if sec["nobits"]:
        out.append("  NOBITS（.bss 類）：檔案內沒有內容，只佔記憶體")
    else:
        rel = (start_addr - sec["addr"]) if start_addr is not None else 0
        rel = max(0, min(rel, sec["size"]))
        n = min(dump_n, sec["size"] - rel)
        data = read_file_bytes(model, sec["offset"] + rel, n)
        out.append("")
        base = (sec["addr"] + rel) if not model.is_rel else rel
        out.append(f"  hex dump @ {'offset' if model.is_rel else 'addr'} 0x{base:x}（{len(data)} / {sec['size']:,} bytes；limit=bytes 調整）")
        out.extend(_hexdump(data, base))
        if rel + n < sec["size"]:
            nxt = (sec["addr"] + rel + n) if not model.is_rel else (rel + n)
            out.append(f"  ... 續看：target=\"{'0x%x' % nxt if not model.is_rel else sec['name']}\"")
        strs = [it for it in model_strings(model) if it["section"] == sec["name"]]
        if strs:
            out.append("")
            out.append(f"  字串（{min(60, len(strs))}/{len(strs)}；view=\"strings\" target=\"section:{sec['name']}\" 看全部）:")
            for it in strs[:60]:
                out.append(f"    0x{it['offset']:08x} {_clip(it['text'])}")
    _, syms = model.primary_symtab()
    inside = [s for s in syms if s["ndx"] == str(sec["idx"]) and s["name"] and not is_mapping_symbol(s["name"])]
    if inside:
        inside.sort(key=lambda s: s["value"])
        demangle_names(model, [s["name"] for s in inside[:40]])
        out.append("")
        out.append(f"  symbols in section（{min(40, len(inside))}/{len(inside)}；view=\"symbols\" target=\"section:{sec['name']}\" 看全部）:")
        out.append(f"  {'addr':<{model.addr_width + 2}} {'size':>7}  {'type':<7} {'bind':<6} {'section':<14} name")
        for s in inside[:40]:
            out.append(_symbol_row(model, s))
    return _finish(out)


def view_memmap(model: ElfModel, target: str = "", limit: int = 0, hard_max: Optional[int] = None,
                char_budget: Optional[int] = None) -> List[str]:
    out = _sink(model, char_budget)
    out.extend(_memmap_lines(model, _limit_for("memmap", limit, hard_max, char_budget), compact=False))
    if (_n_load_segments(model) and not model.is_rel and "sections" not in model.failed
            and not out.exhausted()):
        out.append("")
        out.append("【Section → LOAD segment】")
        line_budget = _limit_for("memmap", limit, hard_max, char_budget) * 8 + 64   # 行數上限（sink 另有字元預算）
        emitted = 0
        for seg in _iter_load_segments(model):
            if out.exhausted():
                break
            out.append(f"  LOAD #{seg['idx']} VMA {model.fmt_addr(seg['vaddr'])} LMA {model.fmt_addr(seg['paddr'])} {seg['flags']}:")
            for name in seg["sections"]:
                if out.exhausted():
                    break
                if emitted >= line_budget:
                    out.append("      ... 其餘 section 未列（limit=N）")
                    break
                s = model.section_by_name(name)
                if s is None:
                    continue
                out.append(f"      {name:<24} {model.fmt_addr(s['addr'])}  size 0x{s['size']:06x}  {s['flags'] or '-'}"
                           + ("  (NOBITS)" if s["nobits"] else ""))
                emitted += 1
            if emitted >= line_budget:
                break
        seg_map = _section_segment_map(model)
        unmapped = list(itertools.islice(
            (s for s in model.sections if s["name"] and "A" in s["flags"] and s["size"] > 0 and not seg_map.get(s["name"])),
            21,
        ))
        if unmapped and not out.exhausted():
            out.append("  不在任何 segment 內的 alloc section：" + ", ".join(s["name"] for s in unmapped[:20])
                       + ("，…" if len(unmapped) > 20 else ""))
    return _finish(out)


def view_symbols(model: ElfModel, target: str = "", limit: int = 0, hard_max: Optional[int] = None,
                 char_budget: Optional[int] = None) -> List[str]:
    out = _sink(model, char_budget)
    if not model.has_symbols():
        out.append("")
        if "symbols" in model.failed:
            out.append(f"【Symbols】symbol table 讀取失敗：{model.failed['symbols']}（不能當成 stripped）")
        else:
            out.append("【Symbols】沒有任何 symbol table（fully stripped）。可用 view=\"strings\"、view=\"imports\"（若有 .dynsym）、view=\"disasm\" target=0x位址。")
        return _finish(out)
    fields, rx, addr, text, note = _parse_filter(target, ("bind", "type", "ndx", "section", "table", "vis"))
    if note:
        out.append(f"[注意] {note}")
    n = _limit_for("symbols", limit, hard_max, char_budget)
    deadline = _Deadline()

    if addr is not None:
        out.append("")
        out.append(f"【Symbol lookup】位址 {model.fmt_addr(addr)}")
        if model.is_rel:
            out.append("  REL 檔的 symbol value 是 section 內 offset；下面以各 section 的 offset 比對")
        sec = section_for_addr(model, addr) if not model.is_rel else None
        if sec is not None:
            out.append(f"  section: {sec['name']} (+0x{addr - sec['addr']:x})")
        found_any = False
        for tname, syms in model.symtabs.items():
            cmp = addr & ~1 if model.machine == "arm" else addr
            hits = [s for s in syms if s["ndx"] not in ("UND", "COM") and s["size"] > 0
                    and ((s["value"] & ~1) if model.machine == "arm" else s["value"]) <= cmp
                    < ((s["value"] & ~1) if model.machine == "arm" else s["value"]) + s["size"]
                    and s["name"] and not is_mapping_symbol(s["name"])]
            if hits:
                found_any = True
                out.append(f"  {tname}: 落在下列 symbol 的 size 範圍內")
                for s in hits[:10]:
                    out.append(_symbol_row(model, s) + f"   (+0x{cmp - (s['value'] & ~1 if model.machine == 'arm' else s['value']):x})")
        if not found_any:
            hit = symbol_for_addr(model, addr)
            if hit is not None:
                s, off, _ = hit
                out.append(f"  沒有 symbol 的 size 涵蓋此位址；同 section 內最近的前一個 symbol：{s['name']}+0x{off:x}")
            else:
                out.append("  沒有 symbol 涵蓋此位址（可能在 stripped 區段、資料區或 padding）")
        if model.caps.get("dwarf_lines"):
            loc = dwarf_addr_to_line(model, addr)
            if loc:
                out.append(f"  DWARF: {loc['file']}:{loc['line']}" + (f"  in {loc['function']}" if loc.get("function") else ""))
        return _finish(out)

    tables = [fields["table"]] if fields.get("table") in model.symtabs else \
        [k for k in (".symtab", ".dynsym") if model.symtabs.get(k)] + \
        [k for k in model.symtabs if k not in (".symtab", ".dynsym") and model.symtabs[k]]
    budget = n
    for tname in tables:
        syms = model.symtabs[tname]
        st = symtab_stats(syms)
        if budget <= 0:
            out.append("")
            out.append(f"【Symbols / {tname}】（筆數預算已用完，略；用 target=\"table:{tname}\" 指定）")
            continue
        matched_count = 0

        def _candidates():
            nonlocal matched_count
            for s in syms:
                if deadline.expired():
                    return
                if fields.get("bind") and s["bind"].upper() != fields["bind"].upper():
                    continue
                if fields.get("type") and s["type"].upper() != fields["type"].upper():
                    continue
                if fields.get("vis") and s["vis"].upper() != fields["vis"].upper():
                    continue
                if fields.get("ndx") and s["ndx"].upper() != fields["ndx"].upper() and model.ndx_label(s["ndx"]) != fields["ndx"]:
                    continue
                if fields.get("section") and model.ndx_label(s["ndx"]) != fields["section"]:
                    continue
                if rx is not None and not _rx(rx, s["name"]):
                    continue
                if not s["name"] and (rx is not None or not fields):
                    continue
                matched_count += 1
                yield s

        # 只留最前面 budget 筆（依位址）：O(N log budget)、記憶體 O(budget)，不整批排序
        picked = heapq.nsmallest(budget, _candidates(), key=lambda s: (s["ndx"] == "UND", s["value"], s["name"]))
        out.append("")
        out.append(
            f"【Symbols / {tname}】總數 {st['total']}（FUNC {st['func']}：GLOBAL {st['func_global']} / WEAK {st['func_weak']} / "
            f"LOCAL {st['func_local']}；OBJECT {st['object']}；UND {st['und']}；size=0 {st['zero_size']}"
            + (f"；mapping {st['mapping']}" if st["mapping"] else "") + f"）；符合篩選 {matched_count}，顯示 {len(picked)}"
        )
        if deadline.hit:
            out.append(deadline.note())
            break
        if not picked:
            continue
        demangle_names(model, [s["name"] for s in picked])
        out.append(f"  {'addr':<{model.addr_width + 2}} {'size':>7}  {'type':<7} {'bind':<6} {'section':<14} name")
        for s in picked:
            out.append(_symbol_row(model, s))
        if matched_count > len(picked):
            out.append(f"  ... +{matched_count - len(picked)}（limit=N 或 target 縮小範圍；上限 {BIN_ELF_VIEW_MAX_LIMIT}）")
        budget = max(0, budget - len(picked))
    out.append("")
    out.append("  篩選語法：bind:LOCAL|GLOBAL|WEAK  type:FUNC|OBJECT|NOTYPE  ndx:UND|ABS  section:.text  table:.symtab  其餘文字當 regex；0x位址 = 反查")
    return _finish(out)


def view_imports(model: ElfModel, target: str = "", limit: int = 0, hard_max: Optional[int] = None,
                 char_budget: Optional[int] = None) -> List[str]:
    out = _sink(model, char_budget)
    table, imports = _collect_imports(model)
    if not imports:
        out.append("")
        if "symbols" in model.failed:
            out.append(f"【Imports】symbol table 讀取失敗：{model.failed['symbols']}（無法判斷外部參照）")
        else:
            out.append("【Imports】沒有未定義（UND）symbol：靜態連結 / bare-metal 韌體，或完全 stripped。")
        return _finish(out)
    n = _limit_for("imports", limit, hard_max, char_budget)
    _fields, rx, _addr, _text, note = _parse_filter(target, ())
    if note:
        out.append(f"[注意] {note}")
    names = sorted(set(imports))
    if rx is not None:
        names = [x for x in names if _rx(rx, x)]
    refs = _reloc_ref_counts(model)
    by_family = categorize_imports(names)
    src = "UND 於 .dynsym" if table == ".dynsym" else "UND 於 .symtab（.o/.ko 的外部參照）"
    out.append("")
    out.append(f"【Imports（{src}）】共 {len(names)} 個" + (f"（篩選 {target!r}）" if target else ""))
    family_order = [f for f, _ in _IMPORT_API_CATEGORIES] + ["other"]
    remaining = n   # 整個 view 共用一份筆數預算，不是每個家族各 n 筆
    for family in family_order:
        items = by_family.get(family)
        if not items:
            continue
        if refs:
            items = sorted(items, key=lambda x: (-refs.get(x, 0), x))
        show = items[:remaining] if remaining > 0 else []
        out.append(f"  [{family}] ({len(items)})")
        for x in show:
            out.append(f"    {x}" + (f"  ×{refs[x]}" if refs.get(x) else ""))
        if len(items) > len(show):
            out.append(f"    ... +{len(items) - len(show)}（limit=N；整個 view 共 {n} 筆）")
        remaining -= len(show)
    if refs:
        out.append("  （×N = relocation 引用次數；哪個函式引用：view=\"relocs\" target=<symbol>）")
    return _finish(out)


def view_relocs(model: ElfModel, target: str = "", limit: int = 0, hard_max: Optional[int] = None,
                char_budget: Optional[int] = None) -> List[str]:
    out = _sink(model, char_budget)
    if not model.relocs:
        out.append("")
        if "relocs" in model.failed:
            out.append(f"【Relocations】讀取失敗：{model.failed['relocs']}（不能當成沒有 relocation）")
        else:
            out.append("【Relocations】沒有 relocation section（完全連結的靜態映像屬正常；.o/.ko 沒有的話是解析失敗）")
        return _finish(out)
    fields, rx, addr, text, note = _parse_filter(target, ("section", "type"))
    if note:
        out.append(f"[注意] {note}")
    n = _limit_for("relocs", limit, hard_max, char_budget)
    list_all = (text == "*")
    if not text and not fields:
        out.extend(_relocs_summary(model, top=30))
        out.append("  逐筆列出：target=\"*\"（全部，含沒有 symbol 的 RELATIVE）、target=<symbol/caller/type regex>、"
                   "target=\"section:.rela.text\"、target=\"type:R_ARM_CALL\"、target=0x<offset>")
        return _finish(out)

    out.append("")
    out.append(f"【Relocations 逐筆】篩選 {target!r}；欄位：applies_to+offset  type  symbol±addend  (caller 函式)")
    shown = 0
    matched = 0
    deadline = _Deadline()
    for r in model.relocs:
        if fields.get("section") and fields["section"] not in (r["section"], r["applies_to"]):
            continue
        header_done = False
        for e in r["entries"]:
            if deadline.expired() or out.exhausted():
                break
            if fields.get("type") and e["type"].upper() != fields["type"].upper():
                continue
            caller = _reloc_caller(model, r["applies_to"], e["offset"]) if model.is_rel else ""
            if not list_all:
                if addr is not None and e["offset"] != addr:
                    continue
                if rx is not None and not (_rx(rx, e["sym"] or "") or (caller and _rx(rx, caller)) or _rx(rx, e["type"])):
                    continue
            matched += 1
            if shown >= n:
                continue
            if not header_done:            # 有第一筆才印 section 標題，直接寫進 sink，不先攢 rows
                out.append(f"  -- {r['section']}（{r['count']:,} 筆）")
                header_done = True
            add = ""
            if e["addend"] is not None and e["addend"] != 0:
                add = f"{'+' if e['addend'] >= 0 else '-'}0x{abs(e['addend']):x}"
            out.append(f"  {r['applies_to'] or r['section']}+0x{e['offset']:x}  {e['type']}  {e['sym'] or '(none)'}{add}"
                       + (f"  (in {caller})" if caller else ""))
            shown += 1
        if deadline.hit or out.exhausted():
            break
    if matched == 0:
        out.append("  沒有符合的項目")
    elif matched > shown:
        out.append(f"  ... 符合 {matched} 筆，只顯示 {shown}（limit=N 或縮小 target）")
    if deadline.hit:
        out.append(deadline.note())
    if model.is_rel:
        out.append("  （caller = 該 offset 所在的 FUNC symbol；可當 .o/.ko 呼叫關係證據）")
    if model.reloc_entry_cap_hit:
        out.append(f"  （項目超過 {_RELOC_ENTRY_CAP:,} 筆的 section 只保留前 {_RELOC_ENTRY_CAP:,} 筆；統計為全量）")
    return _finish(out)


def view_dynamic(model: ElfModel, target: str = "", limit: int = 0, hard_max: Optional[int] = None,
                 char_budget: Optional[int] = None) -> List[str]:
    out = _sink(model, char_budget)
    if not model.dynamic:
        out.append("")
        if "dynamic" in model.failed:
            out.append(f"【.dynamic】讀取失敗：{model.failed['dynamic']}（不是沒有 .dynamic）")
        else:
            out.append("【.dynamic】沒有 .dynamic section：靜態連結 / bare-metal 韌體 / REL 檔（不是錯誤）")
        return _finish(out)
    out.extend(_dynamic_block(model)[:-1])
    n = _limit_for("dynamic", limit, hard_max, char_budget)
    tags = model.dynamic.get("tags", [])
    _fields, rx, _addr, _text, note = _parse_filter(target, ())
    if note:
        out.append(f"[注意] {note}")
    if rx is not None:
        tags = [(k, v) for k, v in tags if _rx(rx, k) or _rx(rx, v)]
    out.append("")
    out.append(f"【.dynamic 全部 tag】{len(tags)} 個")
    for k, v in tags[:n]:
        out.append(f"  {k:<18} {v}")
    if len(tags) > n:
        out.append(f"  ... +{len(tags) - n}")
    return _finish(out)


def view_dwarf(model: ElfModel, target: str = "", limit: int = 0, hard_max: Optional[int] = None,
               char_budget: Optional[int] = None) -> List[str]:
    out = _sink(model, char_budget)
    dw = model.dwarf
    out.append("")
    if dw.get("unknown"):
        out.append(f"【DWARF】無法判斷有沒有 debug section：section 表讀取失敗（{model.failed.get('sections', '')}）")
        return _finish(out)
    if not dw.get("present"):
        out.append("【DWARF】沒有 debug section（stripped，或編譯時沒加 -g）。symbol 層資訊請用 view=\"symbols\"。")
        return _finish(out)
    if not dw.get("debug_info"):
        out.append(f"【DWARF】只有 {', '.join(dw['sections'])}（例如 .debug_frame 是 unwind 表），沒有 .debug_info → 無 CU / 函式 / 行號 / 型別")
        return _finish(out)
    fields, rx, addr, text, note = _parse_filter(target, ("kind", "cu"))
    if note:
        out.append(f"[注意] {note}")
    n = _limit_for("dwarf", limit, hard_max, char_budget)
    kind = fields.get("kind", "").lower()
    if kind and kind not in ("func", "function", "functions", "type", "types"):
        out.append(f"【DWARF】不支援的 kind={kind!r}（可用 kind:func / kind:type）")
        return _finish(out)
    if rx is None and addr is None and (kind or fields.get("cu")):
        rx = _MATCH_ALL   # 只給 kind:func / cu:xxx 而沒有 regex → 列全部（受 limit）
        text = text or "*"

    if addr is not None:
        out.append(f"【DWARF 位址對應】{model.fmt_addr(addr)}")
        if not model.caps.get("dwarf_lines"):
            out.append("  這條解析路徑沒有行號能力（見上方缺失能力）")
            return _finish(out)
        loc = dwarf_addr_to_line(model, addr)
        hit = symbol_for_addr(model, addr)
        if hit is not None:
            s, off, _ = hit
            out.append(f"  symbol : {s['name']}" + (f"+0x{off:x}" if off else "") + f"（{model.ndx_label(s['ndx'])}）")
        if loc:
            out.append(f"  source : {loc['file']}:{loc['line']}")
            if loc.get("function"):
                out.append(f"  function: {loc['function']}")
            if loc.get("cu"):
                out.append(f"  CU     : {loc['cu']}")
        else:
            err = model._lazy.get("dwarf_error")
            out.append("  找不到對應的行號（位址不在任何 CU 的 line table 內" + (f"；解析錯誤: {err}" if err else "") + "）")
        return _finish(out)

    if rx is None:
        cus = dwarf_cus(model)
        funcs = dwarf_functions(model) if model.caps.get("dwarf_functions") else []
        with_code = sum(1 for f_ in funcs if f_["low_pc"] is not None)
        out.append(f"【DWARF】{len(cus)} 個 CU；函式 DIE {len(funcs)} 個（{with_code} 個有位址範圍"
                   + ("；已達上限" if model._lazy.get("dwarf_funcs_capped") else "") + "）")
        if model._lazy.get("dwarf_error"):
            out.append(f"  [WARN] DWARF 解析錯誤: {model._lazy['dwarf_error']}")
        out.append(f"  {'#':<4} {'name':<40} {'lang':<6} {'range':<{model.addr_width * 2 + 4}} producer")
        for i, c in enumerate(cus[:n]):
            if out.exhausted():
                break
            name = c["name"]
            if name and not name.startswith("/") and c["comp_dir"]:
                name = f"{c['comp_dir'].rstrip('/')}/{name}"
            rng = f"{model.fmt_addr(c['low_pc'])}-{model.fmt_addr(c['high_pc'])}" if c["low_pc"] is not None and c["high_pc"] is not None else "-"
            prod = (c["producer"] or "")[:60]
            out.append(f"  {i:<4} {name[-40:]:<40} {c['language'][:6]:<6} {rng:<{model.addr_width * 2 + 4}} {prod}")
        if len(cus) > n:
            out.append(f"  ... +{len(cus) - n}（limit=N）")
        out.append("")
        out.append("  深入：target=<函式/型別 regex>（例 \"uart|spi\"、\"^main$\"、\"struct_name\"）；target=0x位址 → 來源行；kind:func / kind:type 只列一種")
        return _finish(out)

    if kind in ("", "func", "function", "functions"):
        matched_funcs = 0

        def _fgen():
            nonlocal matched_funcs
            for f_ in dwarf_functions(model):
                if not _rx(rx, f_["name"]):
                    continue
                if fields.get("cu") and fields["cu"] not in (f_["cu"] or ""):
                    continue
                matched_funcs += 1
                yield f_

        # 只留前 n 筆（依位址）：候選不整批留在記憶體
        funcs = heapq.nsmallest(n, _fgen(), key=lambda f_: (f_["low_pc"] is None, f_["low_pc"] or 0, f_["name"]))
        out.append(f"【DWARF 函式】符合 {text!r} 共 {matched_funcs} 個（顯示 {len(funcs)}）")
        if funcs:
            out.append(f"  {'range':<{model.addr_width * 2 + 4}} {'name':<36} source")
            for f_ in funcs:
                if out.exhausted():
                    break
                rng = (f"{model.fmt_addr(f_['low_pc'])}-{model.fmt_addr(f_['high_pc'])}" if f_["low_pc"] is not None and f_["high_pc"] is not None
                       else (model.fmt_addr(f_["low_pc"]) if f_["low_pc"] is not None else "(no code)"))
                src = f"{f_['file']}:{f_['line']}" if f_["file"] else (f"line {f_['line']}" if f_["line"] else "")
                flags = "".join(x for x in ("E" if f_["external"] else "", "i" if f_["inline"] else "", "d" if f_["declaration"] else ""))
                out.append(f"  {rng:<{model.addr_width * 2 + 4}} {f_['name'][:36]:<36} {src}" + (f"  [{flags}]" if flags else ""))
            if matched_funcs > len(funcs):
                out.append(f"  ... +{matched_funcs - len(funcs)}（limit=N）")
            out.append("  [E]=external [i]=inline [d]=declaration only")
        elif not model.caps.get("dwarf_functions"):
            out.append("  （這條解析路徑沒有函式清單能力）")
    if kind in ("", "type", "types"):
        if model.caps.get("dwarf_types"):
            # 沒指定 limit 時型別最多 50（每個型別帶成員，很長）；指定了就照 limit
            types, truncated = dwarf_types(model, rx, n if limit else min(n, 50))
            out.append("")
            out.append(f"【DWARF 型別】符合 {text!r} 共 {len(types)} 個" + ("（已達上限）" if truncated else ""))
            line_start = len(out)
            for ti, t in enumerate(types):
                if out.exhausted():
                    break
                if len(out) - line_start >= n:          # 型別區塊以 limit 當行預算（每個型別可能幾十行）
                    out.append(f"  ... 其餘 {len(types) - ti} 個型別未列（limit=N 行預算；縮小 target）")
                    break
                head = f"  {t['kind']} {t['name']}"
                if t["size"] is not None:
                    head += f" ({t['size']} bytes)"
                if t["file"]:
                    head += f" — {t['file']}:{t['line']}"
                if t["declaration"] and not t["members"]:
                    head += "  [declaration only]"
                out.append(head)
                if t["kind"] == "typedef":
                    out.append(f"      = {t['target']}")
                elif t["kind"] == "enum":
                    vals = ", ".join(f"{m['name']}={m['value']}" for m in t["members"][:40])
                    if len(t["members"]) > 40:
                        vals += f", … +{len(t['members']) - 40}"
                    out.append(f"      {vals}")
                else:
                    for m in t["members"][:80]:
                        out.append(f"      {m['offset']:<8} {m['name']:<24} : {m['type']}{m['bits']}")
                    if len(t["members"]) > 80 or t.get("members_truncated"):
                        out.append(f"      … +{max(0, len(t['members']) - 80)} 個成員" + ("（成員數超過保留上限）" if t.get("members_truncated") else ""))
        elif kind:
            out.append("")
            out.append("【DWARF 型別】這條解析路徑沒有型別能力（需要 pyelftools）")
    if model._lazy.get("dwarf_error"):
        out.append(f"  [WARN] DWARF 解析錯誤: {model._lazy['dwarf_error']}")
    return _finish(out)


def view_disasm(model: ElfModel, target: str = "", limit: int = 0, hard_max: Optional[int] = None,
                char_budget: Optional[int] = None) -> List[str]:
    out = _sink(model, char_budget)
    out.append("")
    out.append("【反組譯】")
    _ok, lines = disassemble(model, target, _limit_for("disasm", limit, hard_max, char_budget))
    out.extend(lines)
    return _finish(out)


def view_strings(model: ElfModel, target: str = "", limit: int = 0, hard_max: Optional[int] = None,
                 char_budget: Optional[int] = None) -> List[str]:
    out = _sink(model, char_budget)
    items = model_strings(model)
    n = _limit_for("strings", limit, hard_max, char_budget)
    fields, rx, _addr, text, note = _parse_filter(target, ("cat", "section", "min", "enc"))
    if note:
        out.append(f"[注意] {note}")
    deadline = _Deadline()
    cat = fields.get("cat", "").lower()
    section = fields.get("section")
    enc = fields.get("enc", "").lower()
    mn = 0
    if fields.get("min"):
        try:
            mn = int(fields["min"])
        except ValueError:
            mn = 0

    def _matches(it: Dict) -> bool:
        if cat == "other" and it["cats"]:
            return False
        if cat and cat not in ("other", "all") and cat not in it["cats"]:
            return False
        if section and it["section"] != section:
            return False
        if mn and len(it["text"]) < mn:
            return False
        if enc and it["enc"] != enc:
            return False
        if rx is not None and not _rx(rx, it["text"]):
            return False
        return True

    # 單趟篩選：只保存前 n 筆，其餘只計數（不建整份符合清單）
    picked: List[Dict] = []
    matched_count = 0
    if target:
        for i, it in enumerate(items):
            if i % 256 == 0 and deadline.expired():
                break
            if not _matches(it):
                continue
            matched_count += 1
            if len(picked) < n:
                picked.append(it)

    out.append("")
    if not target:
        out.extend(_strings_summary(model, max(BIN_ELF_MAX_STRINGS, n // 4))[1:])
        out.append("")
        out.append(f"【字串（依 offset，前 {min(n, len(items))} / {len(items):,}）】")
        for it in itertools.islice(items, n):
            if out.exhausted():
                break
            sec = f" [{it['section']}]" if it["section"] else ""
            cats = f" ({','.join(it['cats'])})" if it["cats"] else ""
            out.append(f"  0x{it['offset']:08x}{sec}{cats} {_clip(it['text'], 160)}")
        if len(items) > n:
            out.append(f"  ... +{len(items) - n:,}（limit=N 或加 target 篩選）")
        return _finish(out)

    out.append(f"【字串】篩選 {target!r}：符合 {matched_count:,}，顯示 {len(picked)}")
    for it in picked:
        if out.exhausted():
            break
        sec_txt = f" [{it['section']}]" if it["section"] else ""
        enc_txt = " (u16)" if it["enc"] == "utf16" else ""
        cats = f" ({','.join(it['cats'])})" if it["cats"] else ""
        out.append(f"  0x{it['offset']:08x}{sec_txt}{enc_txt}{cats} {_clip(it['text'], 160)}")
    if matched_count > len(picked):
        out.append(f"  ... +{matched_count - len(picked):,}（limit=N，上限 {BIN_ELF_VIEW_MAX_LIMIT}）")
    if deadline.hit:
        out.append(deadline.note())
    out.append("  篩選語法：cat:version|diagnostic|format|url|path|command|config|other  section:.rodata  min:12  enc:ascii|utf16  其餘文字當 regex")
    return _finish(out)


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------

def _cap(text: str, max_chars: int, view: str) -> str:
    if len(text) <= max_chars:
        return text
    note = (
        f"\n\n... [報告已截斷，原長度 {len(text):,} chars，上限 {max_chars:,}。"
        f"這是 view=\"{view}\" 的輸出；用 target 縮小範圍、limit 降低筆數，"
        f"或改用其他 view（symbols / strings / dwarf / relocs / disasm / sections / memmap）。]"
    )
    if max_chars <= len(note) + 40:   # 極小上限：連說明都放不下，只能硬切（上限仍然是上限）
        return text[:max_chars]
    return text[: max_chars - len(note)] + note


def render(model: ElfModel, view: str = "summary", target: str = "", limit: int = 0,
           hard_max: Optional[int] = None, char_budget: Optional[int] = None) -> str:
    """渲染指定 view。hard_max = 筆數上限；char_budget = 渲染階段的字元預算（達到即停，含說明 ≤ 預算）。"""
    view = (view or "summary").strip().lower()
    target = (target or "").strip()
    try:
        limit = int(limit or 0)
    except (TypeError, ValueError):
        limit = 0
    kw = {"hard_max": hard_max, "char_budget": char_budget}
    if view == "summary":
        lines = view_summary(model, limit, **kw)
    elif view == "headers":
        lines = view_headers(model, limit, **kw)
    elif view == "sections":
        lines = view_sections(model, target, limit, **kw)
    elif view == "memmap":
        lines = view_memmap(model, target, limit, **kw)
    elif view == "symbols":
        lines = view_symbols(model, target, limit, **kw)
    elif view == "imports":
        lines = view_imports(model, target, limit, **kw)
    elif view == "relocs":
        lines = view_relocs(model, target, limit, **kw)
    elif view == "dynamic":
        lines = view_dynamic(model, target, limit, **kw)
    elif view == "dwarf":
        lines = view_dwarf(model, target, limit, **kw)
    elif view == "disasm":
        lines = view_disasm(model, target, limit, **kw)
    elif view == "strings":
        lines = view_strings(model, target, limit, **kw)
    else:
        raise ValueError(view)
    return "\n".join(lines)


def unknown_view_message(view: str) -> str:
    lines = [f"[ELF 錯誤] 不支援的 view={view!r}。可用的 view："]
    for v in VIEWS:
        lines.append(f"  {v}: {VIEW_HELP[v]}")
    return "\n".join(lines)


def build_report(path, view: str = "summary", target: str = "", limit: int = 0,
                 max_chars: Optional[int] = None) -> str:
    """analyze_file 用：解析 + 渲染指定 view + hard cap。錯誤以 [ELF 錯誤] 開頭的字串回傳。"""
    v = (view or "summary").strip().lower()
    if v not in VIEWS:
        return unknown_view_message(view)
    try:
        model = load_model(Path(path))
    except ValueError as e:
        return f"[ELF 錯誤] {e}"
    cap = max_chars or BIN_ELF_REPORT_MAX_CHARS
    text = render(model, v, target, limit, char_budget=cap)
    return _cap(text, cap, v)


def _fit_ingest_parts(parts: List[Tuple[str, str, str]], cap: int) -> str:
    """把各段塞進全域上限：總量沒超過就原樣；超過時各段依「等比例 waterfill」截斷並各自註明。

    以前是每段先吃固定配額再套全域上限：大 firmware 的 symbols 段吃光上限，後面的字串 /
    DWARF 段整段消失，KB 查不到卻沒人知道。現在每一段都保證留下開頭與截斷說明。
    parts: (view 名, target 提示, 文字)。
    """
    sep = "\n\n"
    texts = [t for _, _, t in parts]
    total = sum(len(t) for t in texts) + len(sep) * max(0, len(texts) - 1)
    if total <= cap:
        return sep.join(texts)

    remaining = cap - len(sep) * max(0, len(texts) - 1)
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    budgets: Dict[int, int] = {}
    left = len(order)
    for i in order:                      # 短的先滿足，剩餘平均分給長的
        share = max(0, remaining // left)
        take = min(len(texts[i]), share)
        budgets[i] = take
        remaining -= take
        left -= 1

    out_parts: List[str] = []
    for i, (view, hint, text) in enumerate(parts):
        budget = budgets[i]
        if len(text) <= budget:
            out_parts.append(text)
            continue
        note = (
            f"\n… [此段截斷：原 {text.count(chr(10)) + 1:,} 行 / {len(text):,} chars，超過入庫上限 {cap:,}"
            f"（config.BIN_ELF_INGEST_MAX_CHARS）分配給本段的配額；完整內容請用 analyze_file "
            f"view=\"{view}\"{hint} 分批查看]"
        )
        keep_n = max(0, budget - len(note))
        keep = text[:keep_n]
        cut = keep.rfind("\n")
        if cut > 0:
            keep = keep[:cut]
        out_parts.append(keep + note)
    result = sep.join(out_parts)
    if len(result) > cap:                # 理論上不會發生；保險
        result = result[:cap]
    return result


def _ingest_entry_cap(cap: int) -> int:
    """ingest 各 view 的筆數上限：等於字元預算——每行至少 1 字元 + 換行，所以永遠不會比
    字元預算先到（imports 這種 4 空格 + 短名稱的行也一樣）；只是迴圈保險。"""
    return max(_INGEST_MIN_LINES, int(cap) // _INGEST_CHARS_PER_LINE)


def build_ingest_document(path, max_chars: Optional[int] = None) -> str:
    """ingest_document 用：多視角合併的長版報告（RAG 會把 【…】 標題切成章節）。

    analyze_file 的 summary 受 25K hard cap，入庫不該受同一個限制——KB 的價值就是
    能把完整 symbol / 字串 / DWARF / relocation 存下來給之後查。每個 view 以
    BIN_ELF_INGEST_MAX_CHARS 為渲染階段的字元預算（_Sink 達預算即停，中間資料 O(cap)），
    整份再以同一上限做比例分配：沒超過就是真的完整；超過時各段依比例截斷並各自註明
    （見 _fit_ingest_parts），不會有整段消失。
    """
    model = load_model(Path(path))
    cap = max_chars or BIN_ELF_INGEST_MAX_CHARS
    # 每個 view 以 cap 為渲染階段的字元預算（_Sink 達預算即不再收行），中間資料 O(cap)；
    # 筆數上限 = 字元預算（每行至少 1 字元 + 換行），任何 view 都不可能比字元預算先到。
    per_view = _ingest_entry_cap(cap)
    hdr = _hdr_lines(model)
    parts: List[Tuple[str, str, str]] = [
        ("summary", "", "\n".join(view_summary(model, 0, footer=False, char_budget=cap))),
    ]
    # (view, target, target 提示)：順序 = 閱讀順序；比例分配時每一段都有份
    sections: List[Tuple[str, str, str]] = [
        ("symbols", "", " target=<regex>"),
        ("relocs", "*", " target=\"*\""),
        ("dwarf", "", ""),
        ("dwarf", "kind:func", " target=\"kind:func\""),
        ("dwarf", "kind:type", " target=\"kind:type\""),
        ("strings", "cat:all", " target=\"cat:all\""),
        ("memmap", "", ""),
        ("sections", "", ""),
        ("imports", "", ""),
        ("dynamic", "", ""),
    ]
    for view, target, hint in sections:
        if view == "relocs" and not model.relocs:
            continue
        if view == "dwarf" and not model.dwarf.get("debug_info"):
            continue
        if view == "dwarf" and target == "kind:type" and not model.caps.get("dwarf_types"):
            continue
        if view == "dynamic" and not model.dynamic:
            continue
        if view == "imports" and not _collect_imports(model)[1]:
            continue
        try:
            lines = render(model, view, target, per_view, hard_max=per_view, char_budget=cap).split("\n")
        except Exception as e:
            parts.append((view, hint, f"【{view}】產生失敗: {type(e).__name__}: {e}"))
            continue
        # 各 view 開頭都是同一份檔案 header，入庫只留 summary 那一次
        body = [ln for i, ln in enumerate(lines) if not (i < len(hdr) and ln == hdr[i])]
        text = "\n".join(body).strip("\n")
        if text:
            parts.append((view, hint, text))
    return _fit_ingest_parts(parts, cap)
