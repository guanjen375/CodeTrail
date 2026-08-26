#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能程式碼分析器 - 媒體檔案處理
- 圖片通用視覺分析（使用 VL 模型，包含但不限於 OCR）
- 二進位檔案分析（Hex dump + strings 提取，含 offset）
- ELF 檔案解析（多視角：summary / symbols / disasm / dwarf / strings / sections / memmap /
  relocs / imports / dynamic / headers；實作在 elf_analysis.py）
"""

import re
import base64
from collections import OrderedDict
from pathlib import Path
from typing import Optional, List, Tuple, Dict

import llama_client
import elf_analysis
from elf_analysis import (
    scan_ascii_strings, scan_utf16le_strings, is_meaningful_string,
    format_strings_with_offset, collect_high_priority_strings, cmd_exists, run_cmd,
)
from config import (
    LLAMA_VL_BASE_URL, VL_MODEL, IMAGE_EXTENSIONS,
    VL_ANALYZE_MAX_TOKENS, VL_ANALYZE_TIMEOUT,
    BIN_ELF_REPORT_MAX_CHARS, BIN_ELF_INGEST_MAX_CHARS,
    require_pymupdf4llm,
)


# 支援的二進位檔案副檔名
BINARY_EXTENSIONS = {".bin", ".dat", ".raw", ".fw", ".img", ".rom", ".hex"}

# 支援的 ELF 檔案副檔名
ELF_EXTENSIONS = {".elf", ".so", ".o", ".axf", ".out", ".ko"}

# 一次性 PDF 檢視（read_pdf）
PDF_EXTENSIONS = {".pdf"}
MAX_PDF_SIZE = 50 * 1024 * 1024  # 50MB
PDF_ONESHOT_MAX_CHARS = 30000    # read_pdf 輸出上限（避免炸 OpenCode context）

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

# 全域 sandbox root（由 mcp_server.py 設定）
_SANDBOX_ROOT: Optional[Path] = None
_ALLOW_EXTERNAL: bool = True  # 預設允許外部檔案（大部分使用場景都是外部路徑）

# Small LRU caches to avoid repeated VL/BIN/ELF work in a session.
_OCR_CACHE = OrderedDict()
_BIN_CACHE = OrderedDict()
_ELF_CACHE = OrderedDict()
_PDF_CACHE = OrderedDict()
_OCR_CACHE_MAX = 8
_BIN_CACHE_MAX = 6
_ELF_CACHE_MAX = 16   # 同一檔案的多個 view / target 各佔一格（模型本身另在 elf_analysis 快取）
_PDF_CACHE_MAX = 6

_IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


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


# ============================================================================
# ELF 解析：實作在 elf_analysis.py（backend 中立模型 + 多視角報告）
# ============================================================================
# 這裡只留 sandbox 入口、BIN 報告的 hard cap 與相容 alias。舊的 readelf / pyelftools
# 雙實作（_build_elf_report_native / _build_elf_report_readelf / _format_symbol_table …）
# 已整併進 elf_analysis：兩條解析路徑填同一份模型、共用同一套渲染，缺失能力會在報告
# 開頭明列，不再是只標 parser 名字的靜默降級。

_scan_ascii_strings = scan_ascii_strings
_scan_utf16le_strings = scan_utf16le_strings
_is_meaningful_string = is_meaningful_string
_format_strings_with_offset = format_strings_with_offset
_collect_high_priority_strings = collect_high_priority_strings
_cmd_exists = cmd_exists
_run_cmd = run_cmd
ELF_VIEWS = elf_analysis.VIEWS
ELF_VIEW_HELP = elf_analysis.VIEW_HELP


def _truncate_elf_report(full_report: str, max_chars: int = BIN_ELF_REPORT_MAX_CHARS) -> str:
    """BIN 報告硬上限（ELF 各 view 的 cap 在 elf_analysis 內處理）。

    報告本身已按重要度由前往後排（檔名 / 大小 / Magic / Hex dump 在最前），
    所以直接前綴切片就是「優先保留 header」。
    """
    if len(full_report) <= max_chars:
        return full_report
    return (
        full_report[:max_chars - 200] +
        f"\n\n... [報告已截斷，原長度 {len(full_report):,} chars]"
    )


def _build_elf_report(filepath: Path, view: str = "summary", target: str = "",
                      limit: int = 0, max_chars: Optional[int] = None) -> str:
    """相容入口：ELF 報告（指定 view）。錯誤以 [ELF 錯誤] 字串回傳。"""
    return elf_analysis.build_report(filepath, view=view, target=target, limit=limit, max_chars=max_chars)


def _elf_params_note(view: str, target: str, limit: int) -> str:
    """非 ELF 檔案收到 view / target / limit 時，回覆開頭明講已忽略（不要靜默）。"""
    if (view or "summary").strip().lower() != "summary" or (target or "").strip() or (limit or 0):
        return "[注意] view / target / limit 只對 ELF（含 ELF magic 的 .bin）有效，此檔案已忽略這些參數。\n\n"
    return ""

# ============================================================================
# 公開 API
# ============================================================================

def ocr_image(path: str) -> str:
    """對圖片進行通用視覺分析；有文字時同時忠實轉錄。"""
    p = _safe_path(path, allow_external=True, allowed_extensions=IMAGE_EXTENSIONS)

    if p is None:
        if _ALLOW_EXTERNAL:
            return f"[圖片分析錯誤] 檔案不存在或不是支援的圖片格式: {path}"
        else:
            return f"[圖片分析錯誤] 路徑不在允許範圍內或檔案不存在: {path}"

    if not p.exists():
        return f"[圖片分析錯誤] 檔案不存在: {path}"

    if p.suffix.lower() not in IMAGE_EXTENSIONS:
        return f"[圖片分析錯誤] 不支援的格式: {p.suffix}"

    file_size = p.stat().st_size
    if file_size > 20 * 1024 * 1024:
        return f"[圖片分析錯誤] 圖片過大: {file_size / 1024 / 1024:.1f}MB"

    cache_key = _cache_key(
        p,
        extra=("vision-chat-v1", VL_ANALYZE_MAX_TOKENS),
    )
    cached = _cache_get(_OCR_CACHE, cache_key)
    if cached is not None:
        return cached

    try:
        with open(p, "rb") as f:
            data = base64.b64encode(f.read()).decode()

        result = llama_client.vision_completion(
            base_url=LLAMA_VL_BASE_URL,
            prompt=(
                "請忠實分析這張圖片，使用繁體中文，內容完整但避免重複。\n"
                "1. 先判斷圖片類型，例如終端機、UI、聊天、表格、圖表、"
                "架構圖、流程圖、文件頁、照片或其他類型。\n"
                "2. 若有文字、數字、指令、路徑、錯誤碼或標籤，另列「可見文字」，"
                "盡量保持原文與相對位置。\n"
                "3. 整理畫面中的物件、區塊、連接關係、資料流、狀態與關鍵含義；"
                "若是一般照片，也描述人物、物件、背景與重要細節。\n"
                "4. 看不清楚的內容請標註 [模糊] 或 [看不清楚]，不要猜測或補完。"
            ),
            image_base64=data,
            mime_type=_IMAGE_MIME_TYPES[p.suffix.lower()],
            model=VL_MODEL,
            max_tokens=VL_ANALYZE_MAX_TOKENS,
            temperature=0.1,
            timeout=VL_ANALYZE_TIMEOUT,
        )
        _cache_set(_OCR_CACHE, cache_key, result, _OCR_CACHE_MAX)
        return result
    except Exception as e:
        return f"[圖片分析錯誤] {type(e).__name__}: {e}"


_PDF_BATCH_PAGES = 16          # 分批解析大小：達輸出上限即停，晚批不付解析成本
_PDF_ONESHOT_MIN_RESULT = 600  # 最終輸出的絕對下限空間：極小 max_chars 仍能容納導引訊息


def _format_page_ranges(nums: List[int], max_ranges: int = 30) -> str:
    """把頁碼列表壓成範圍摘要：[1,2,3,7,9,10] → "1-3, 7, 9-10"。

    上限 max_ranges 段，超過以「…(共 N 頁)」收尾——逐頁列舉在萬頁 PDF 上
    光頁碼清單就會炸掉輸出上限。
    """
    nums = sorted(nums)
    ranges: List[Tuple[int, int]] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        ranges.append((start, prev))
        start = prev = n
    ranges.append((start, prev))

    shown = [str(a) if a == b else f"{a}-{b}" for a, b in ranges[:max_ranges]]
    text = ", ".join(shown)
    if len(ranges) > max_ranges:
        text += f", …(共 {len(nums)} 頁)"
    return text


def read_pdf(path: str, max_chars: int = PDF_ONESHOT_MAX_CHARS) -> str:
    """一次性抽 PDF 各頁文字（不寫入 knowledge.json）。

    補上「只看一眼 PDF」的通道：以前一次性入口只有 read_file（純文字）與
    analyze_file（圖片/binary），想看 PDF 只能 ingest → remove → reload。
    內嵌圖只標註頁碼與張數、不做 VL 分析——要圖片內容入 KB 就直接
    ingest_document 這份 PDF（內嵌圖會自動經 VL 入庫）；只想看一次就把
    該頁另存 .png 用 ocr_image/analyze_file。

    max_chars 是真 hard cap：最終字串（含 header、截斷訊息、內嵌圖摘要）
    保證 ≤ max(max_chars, 600)；600 是導引訊息的最低可讀空間。解析分批
    進行，達上限即停止——高頁數 PDF 不會為被丟棄的內容卡住 MCP，代價是
    截斷時內嵌圖統計只涵蓋已解析的頁（輸出會註明）。
    """
    p = _safe_path(path, allow_external=True, allowed_extensions=PDF_EXTENSIONS)

    if p is None:
        if _ALLOW_EXTERNAL:
            return f"[PDF 錯誤] 檔案不存在或不是 .pdf: {path}"
        else:
            return f"[PDF 錯誤] 路徑不在允許範圍內或檔案不存在: {path}"

    if not p.exists():
        return f"[PDF 錯誤] 檔案不存在: {path}"

    # sandbox 內路徑 _safe_path 不查副檔名（同 ocr_image 的處理），這裡補查
    if p.suffix.lower() not in PDF_EXTENSIONS:
        return f"[PDF 錯誤] 不是 .pdf: {p.suffix}"

    file_size = p.stat().st_size
    if file_size > MAX_PDF_SIZE:
        return f"[PDF 錯誤] 檔案過大: {file_size / 1024 / 1024:.1f}MB (上限 {MAX_PDF_SIZE // 1024 // 1024}MB)"

    try:
        pymupdf4llm = require_pymupdf4llm()
        import pymupdf
    except (RuntimeError, ImportError) as e:
        return f"[PDF 錯誤] {e}"

    max_chars = max(1, int(max_chars))
    cache_key = _cache_key(p, ("pdf-oneshot-v2", max_chars))
    cached = _cache_get(_PDF_CACHE, cache_key)
    if cached is not None:
        return cached

    # 只為了 page_count / 密碼檢查開一次，批次解析用 pages= 走檔案路徑
    try:
        src = pymupdf.open(str(p))
        try:
            if src.needs_pass:
                return f"[PDF 錯誤] 檔案有密碼保護，無法解析: {p.name}"
            n_total = src.page_count
        finally:
            src.close()
    except Exception as e:
        return f"[PDF 錯誤] 無法解析: {type(e).__name__}: {e}"

    header = f"[PDF] {p.name} 共 {n_total} 頁（一次性檢視，未寫入 knowledge.json）"
    # 正文預算保留一段給尾端訊息（[已截斷]/內嵌圖摘要），避免最終 clamp 削到它們
    budget = max_chars - min(400, max_chars // 4)

    body: List[str] = []
    image_pages: dict = {}      # 頁碼 → 內嵌圖數（只涵蓋已解析的頁）
    used = len(header)
    truncated_at: Optional[int] = None
    parsed_through = 0          # 已解析（含內嵌圖統計）的最後一頁

    for batch_start in range(0, n_total, _PDF_BATCH_PAGES):
        batch_end = min(batch_start + _PDF_BATCH_PAGES, n_total)
        try:
            pages = pymupdf4llm.to_markdown(
                str(p), page_chunks=True, write_images=False,
                pages=list(range(batch_start, batch_end)),  # 0-based；輸出 page_number 是絕對 1-based
            )
        except Exception as e:
            return f"[PDF 錯誤] 無法解析: {type(e).__name__}: {e}"

        for page_info in pages:
            meta = page_info.get("metadata", {}) or {}
            # pymupdf4llm 新版 key 是 page_number（1-based），舊版是 page（0-based）
            page_num = meta.get("page_number")
            if page_num is None:
                page_num = meta.get("page", 0) + 1

            pics = [b for b in page_info.get("page_boxes") or []
                    if b.get("class") == "picture"]
            n_pics = len(pics) or len(page_info.get("images") or [])
            if n_pics:
                image_pages[page_num] = n_pics

            if truncated_at is not None:
                continue  # 本批剩餘頁只補內嵌圖統計

            text = (page_info.get("text") or "").strip()
            page_body = text if text else "（本頁無可抽取文字）"
            if n_pics:
                page_body += f"\n［本頁含 {n_pics} 張內嵌圖，未做 VL 分析］"
            block = f"\n--- 第 {page_num} 頁 ---\n{page_body}"

            if used + len(block) > budget:
                truncated_at = page_num
                continue
            body.append(block)
            used += len(block)

        parsed_through = batch_end
        if truncated_at is not None:
            break  # 之後的批次不再解析：被丟棄的內容不付解析成本

    tails: List[str] = []
    if truncated_at is not None:
        tails.append(
            f"\n[已截斷] 第 {truncated_at} 頁起省略（輸出上限 {max_chars} 字元）。"
            "要完整內容請用 ingest_document 入庫後查詢。"
        )
    if image_pages:
        pages_str = _format_page_ranges(list(image_pages))
        tails.append(
            f"\n[注意] 內嵌圖共 {sum(image_pages.values())} 張（頁 {pages_str}）"
            "未包含在上面文字裡。需要圖片內容時，直接 ingest_document 這份 PDF"
            "（內嵌圖會自動經 VL 入庫），或把該頁另存 .png 用 analyze_file 看一次。"
        )
    if parsed_through < n_total:
        tails.append(
            f"\n[注意] 第 {parsed_through + 1}-{n_total} 頁未解析（達輸出上限即停止），"
            "內嵌圖統計只涵蓋前面已解析的頁。"
        )

    result = "\n".join([header] + body + tails)

    # 真 hard cap：上面 budget 只管正文，這裡對最終字串做絕對保證
    limit = max(max_chars, _PDF_ONESHOT_MIN_RESULT)
    if len(result) > limit:
        tail_note = "\n[硬截斷] 已達輸出絕對上限。完整內容請用 ingest_document 入庫後查詢。"
        result = result[: limit - len(tail_note)] + tail_note

    _cache_set(_PDF_CACHE, cache_key, result, _PDF_CACHE_MAX)
    return result


def read_elf(path: str, view: str = "summary", target: str = "", limit: int = 0) -> str:
    """讀取 ELF 檔案並產生分析報告。

    view / target / limit 的語意見 elf_analysis.VIEW_HELP（summary / headers / sections /
    memmap / symbols / imports / relocs / dynamic / dwarf / disasm / strings）。輸出受
    BIN_ELF_REPORT_MAX_CHARS 硬上限，截斷訊息會指出該用哪個 view + target 縮小範圍。
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

    view_key = (view or "summary").strip().lower()
    target_key = (target or "").strip()
    try:
        limit_key = max(0, int(limit or 0))
    except (TypeError, ValueError):
        limit_key = 0

    cache_key = _cache_key(p, ("elf-v2", view_key, target_key, limit_key))
    cached = _cache_get(_ELF_CACHE, cache_key)
    if cached is not None:
        return cached

    try:
        result = _build_elf_report(p, view=view_key, target=target_key, limit=limit_key)
        _cache_set(_ELF_CACHE, cache_key, result, _ELF_CACHE_MAX)
        return result
    except Exception as e:
        return f"[ELF 錯誤] {type(e).__name__}: {e}"


def read_binary(path: str, max_strings: int = 200, view: str = "summary", target: str = "",
                limit: int = 0, max_chars: int = BIN_ELF_REPORT_MAX_CHARS) -> str:
    """讀取二進位檔案並轉換為可分析格式

    使用純 Python 掃描字串（含 offset），若偵測到 ELF magic 會自動切換到 ELF 解析
    （此時 view / target / limit 同 read_elf；非 ELF 檔收到這些參數會在開頭註明已忽略）。

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

    view_key = (view or "summary").strip().lower()
    target_key = (target or "").strip()
    try:
        limit_key = max(0, int(limit or 0))
    except (TypeError, ValueError):
        limit_key = 0

    cache_key = _cache_key(p, ("bin-v2", max_strings, view_key, target_key, limit_key, max_chars))
    cached = _cache_get(_BIN_CACHE, cache_key)
    if cached is not None:
        return cached

    try:
        # 讀取檔頭
        with open(p, "rb") as f:
            header = f.read(65536)

        # 自動偵測 ELF：若是 ELF 則切換到 ELF 解析（view / target / limit 生效）
        if header.startswith(b"\x7fELF"):
            # 前綴也算在 max_chars 內：ELF 報告的 cap 要扣掉前綴長度，最終值才不會超過上限
            prefix = "[BIN→ELF] 偵測到 ELF magic，自動切換 ELF 解析模式:\n\n"
            cap = max(1, int(max_chars))
            body = _build_elf_report(
                p, view=view_key, target=target_key, limit=limit_key,
                max_chars=max(50, cap - len(prefix)),
            )
            result = prefix + body
            if len(result) > cap:   # 極小的 max_chars 連前綴都放不下：上限仍然是上限，硬切
                result = result[:cap]
            _cache_set(_BIN_CACHE, cache_key, result, _BIN_CACHE_MAX)
            return result

        note = _elf_params_note(view_key, target_key, limit_key)

        # 基本資訊
        report: List[str] = [
            f"檔案: {p.name}",
            f"大小: {file_size:,} bytes",
        ]
        if note:
            report.insert(0, note.rstrip("\n"))
            report.insert(1, "")

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
        full_report = _truncate_elf_report("\n".join(report), max_chars=max_chars)
        _cache_set(_BIN_CACHE, cache_key, full_report, _BIN_CACHE_MAX)
        return full_report

    except Exception as e:
        return f"[BIN 錯誤] {type(e).__name__}: {e}"


def read_binary_for_ingest(path: str) -> str:
    """ingest_document 用的長版報告（RAG.extract_binary_document 呼叫）。

    ELF：elf_analysis.build_ingest_document —— summary 之外再附完整 symbol 表、memmap、
    relocation 逐筆（含 caller）、DWARF 函式 / 型別、全部分類字串，上限
    BIN_ELF_INGEST_MAX_CHARS（比 analyze_file 的 25K 寬得多；KB 的價值就是把這些存下來）。
    非 ELF：read_binary 但字串配額與長度上限放大。沙箱規則與 read_binary 相同。
    """
    allowed_ext = None if _ALLOW_EXTERNAL else (BINARY_EXTENSIONS | ELF_EXTENSIONS)
    p = _safe_path(path, allow_external=True, allowed_extensions=allowed_ext)
    if p is None:
        if _ALLOW_EXTERNAL:
            return f"[BIN 錯誤] 檔案不存在: {path}"
        return f"[BIN 錯誤] 路徑不在允許範圍內或檔案不存在: {path}"
    if not p.exists():
        return f"[BIN 錯誤] 檔案不存在: {path}"
    file_size = p.stat().st_size
    if file_size > MAX_BINARY_SIZE:
        return f"[BIN 錯誤] 檔案過大: {file_size / 1024 / 1024:.1f}MB (上限 {MAX_BINARY_SIZE // 1024 // 1024}MB)"
    if elf_analysis.is_elf_file(p):
        try:
            return elf_analysis.build_ingest_document(p, max_chars=BIN_ELF_INGEST_MAX_CHARS)
        except Exception as e:
            return f"[ELF 錯誤] {type(e).__name__}: {e}"
    return read_binary(str(p), max_strings=2000, max_chars=BIN_ELF_INGEST_MAX_CHARS)


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
        print(f"[IMG] VL 分析: {path}")
        ctx += f"\n[{path}]:\n{ocr_image(path)}\n"

    return clean, ctx


def process_file(text: str, max_images: int = 3) -> tuple[str, str, dict]:
    """統一處理文字中的 file: 檔案引用（自動偵測檔案類型）

    支援：
    - file:/path/to/image.png - 通用 VL 圖片分析（png/jpg/jpeg/gif/webp）
    - file:/path/to/firmware.bin - 二進位分析（bin/dat/raw/fw/img/rom/hex）
    - file:/path/to/app.elf - ELF 解析（elf/so/o/axf/out/ko）
    - file:"/path with spaces/file.bin" - 帶引號的路徑（支援空白）
    - file:'path with spaces/file.png' - 單引號也支援

    自動偵測規則（優先級）：
    1. 副檔名符合圖片格式 → 通用 VL 分析（含文字辨識）
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
        - image_ctx: str - 圖片 VL 分析上下文（獨立，供 strict mode 使用）
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
    file_image_ctx = ""  # 獨立的圖片 VL 分析上下文
    file_binary_ctx = ""  # 獨立的 BIN/ELF 上下文

    # 處理圖片
    if image_files:
        if len(image_files) > max_images:
            others = image_files[max_images:]
            print(f"[WARN] 偵測到 {len(image_files)} 個圖片，為避免超出 context，只處理前 {max_images} 個")
            print(f"       已忽略: {', '.join(others[:3])}" + (f" ... 等 {len(others)} 個" if len(others) > 3 else ""))

        file_image_ctx = "\n附加圖片:\n"
        for path in image_files[:max_images]:
            print(f"[IMG] VL 分析: {path}")
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
        "image_ctx": file_image_ctx,    # 獨立的圖片 VL 分析（供 strict mode 的 base_ctx）
        "binary_ctx": file_binary_ctx   # 獨立的 BIN/ELF（供 strict mode 的 binary_ctx）
    }

    return clean, "\n".join(ctx_parts), metadata
