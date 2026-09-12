#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""patch_engine — apply_patch 的格式偵測、SEARCH/REPLACE 解析、byte-level 快照、
journaled 原子寫入與 bounded mismatch 回饋(workflow A/B/E,2026-08-26)。

刻意不 import config / agent_tools:上限值由呼叫端(``ToolExecutor.apply_patch``)當參數
傳入,這個模組只有純函式與檔案 I/O helper。

兩種輸入格式(unified diff / SEARCH/REPLACE)在 ToolExecutor 端正規化成同一種
per-file plan 之後,共用這裡的 snapshot → render → journal 管線;沒有第二份寫檔器。

寫入層的安全模型(B):
  - 既有檔:bytes + UTF-8 strict decode;BOM / homogeneous LF 或 CRLF / final newline /
    mode bits 全部記在 ``FileSnapshot``,寫回時依快照還原;CR-only 與 mixed newline 一律
    fail-loud(不做 majority 猜測)。
  - 真正的 I/O 不走 resolved path:固定一個 root fd,**每次** 讀 / mkdir / temp / publish /
    rollback 都從 root 沿 lexical path 逐層 ``O_DIRECTORY|O_NOFOLLOW`` 重走到 parent,並
    比對 parent 的 dev/ino 與 preflight(或建立目錄時)記錄的一致;缺少 dir_fd 或
    O_NOFOLLOW 等必要安全能力時,在任何讀寫之前拒絕操作。
  - 單檔寫入 = 同目錄唯一 temp(``O_EXCL`` 建立成功後才擁有)+ fsync + 原子發布(既有檔
    ``os.replace``;新檔 ``os.link`` 不覆蓋競態中冒出的同名檔);多檔語意 = 全量 preflight +
    失敗時 best-effort rollback,**不是**跨檔交易。
  - journal 記 prepared/committed、原始 bytes/mode、實際寫入 bytes 與發布 inode(取自自有
    temp fd,publish 成功後立即標 committed);rollback 只還原「同一個 opened fd 上 identity
    與 bytes 都仍是本次 postimage」的檔案,否則保留並回報 conflict。
"""
from __future__ import annotations

import contextlib
import difflib
import errno
import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from runtime_dependencies import DependencyError, require_safe_filesystem

SR_SEARCH = "<<<<<<< SEARCH"
SR_SEP = "======="
SR_REPLACE = ">>>>>>> REPLACE"
SR_MARKERS = frozenset({SR_SEARCH, SR_SEP, SR_REPLACE})
UDIFF_CONTROL_PREFIXES = ("--- ", "+++ ", "@@", "diff ", "Index:")
FENCE_PREFIXES = ("```", "~~~")

UTF8_BOM = b"\xef\xbb\xbf"

# E:整次 tool result 的 mismatch-preview 總上限(不是每 block / 每候選各自;省略標記本身計入)。
MISMATCH_MAX_LINES = 40
MISMATCH_MAX_CHARS = 2000
MISMATCH_MAX_CANDIDATES = 5
MISMATCH_WINDOW_MAX_LINES = 12
# 掃描成本上限:exact 搜尋與 ranking **之前**就檢查(cells = 檔案行數 × SEARCH 行數;
# 字元 = 檔案 + SEARCH),超過就 fail-loud / candidate unavailable,不讓巨型輸入吃 CPU。
SCAN_COST_MAX_CELLS = 20_000_000
RANK_COST_MAX_CELLS = 2_000_000
SCAN_COST_MAX_CHARS = 2_000_000
SCAN_SIMILARITY_MAX_LINES = 20_000
SIMILARITY_SLICE = 200
DISPLAY_LINE_MAX = 120
TEMP_NAME_ATTEMPTS = 8

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

# 所有操作必須以 root fd 錨定(逐層 O_NOFOLLOW)。缺能力直接拒絕。
DIRFD_ANCHORING = bool(
    _O_NOFOLLOW and _O_DIRECTORY and _O_NONBLOCK
    and all(
        fn in os.supports_dir_fd
        for fn in (os.open, os.mkdir, os.stat, os.rename, os.unlink, os.rmdir)
    )
)

SYMLINK_REFUSED = "目標或其路徑上有 symlink / 非目錄 component,拒絕寫入（請改用不經 symlink 的實際路徑）"
NOT_REGULAR = "不是一般檔案（目錄 / device / fifo / socket 不可 patch）"


class PatchFormatError(ValueError):
    """patch 文字本身不合 grammar / path 規則。"""


class SnapshotError(ValueError):
    """目標檔案無法安全地當成 UTF-8 文字快照(decode / newline / symlink / 非一般檔)。"""


class WriteError(RuntimeError):
    """寫入階段的競態或發布失敗(preimage 變更、parent 身分改變、新檔被搶先建立)。"""


# ============================================================
# 文字正規化與格式偵測
# ============================================================
def normalize_patch_text(text: str) -> str:
    """patch transport 的 CRLF 先正規化成 logical LF;剩下孤立 CR 就 fail-loud。"""
    text = str(text).replace("\r\n", "\n")
    if "\r" in text:
        raise PatchFormatError("含孤立 CR（\\r）字元,不接受 CR-only / mixed 換行的 patch 文字")
    return text


def _is_udiff_control(line: str) -> bool:
    return line.startswith(UDIFF_CONTROL_PREFIXES)


def detect_format(text: str) -> str:
    """回 "unified_diff" 或 "search_replace";fence / mixed / 不合 grammar 一律 raise。

    規則(A):
      - 任一行以 ``` 或 ~~~ 起始 → 拒絕(參數已是字串,不要再包 fence)。
      - 第一個非空行是 udiff 控制行 → udiff;此時任何 column-0 **裸** S/R marker
        都是 mixed(hunk body 內帶 ' '/'+'/'-' 前綴的 marker 是內容,不算)。
      - 第一個非空行(path)之後的第一個非空行是 ``<<<<<<< SEARCH`` → S/R。
      - 其餘只要出現裸 marker 就是「像 S/R 但不合 canonical grammar」。
    """
    lines = text.split("\n")
    for lineno, line in enumerate(lines, 1):
        if line.startswith(FENCE_PREFIXES):
            raise PatchFormatError(f"參數已是字串,不要再包 Markdown fence（行 {lineno}）")
    nonblank = [line for line in lines if line.strip()]
    if not nonblank:
        raise PatchFormatError("patch 內容是空的")
    has_bare_marker = any(line in SR_MARKERS for line in lines)
    if _is_udiff_control(nonblank[0]):
        if has_bare_marker:
            raise PatchFormatError("混用 unified diff 與 SEARCH/REPLACE（同一輸入只允許一種格式）")
        return "unified_diff"
    if len(nonblank) >= 2 and nonblank[1] == SR_SEARCH:
        return "search_replace"
    if has_bare_marker:
        raise PatchFormatError(
            "看起來像 SEARCH/REPLACE 但不符 canonical grammar：path 行後必須緊接 "
            "<<<<<<< SEARCH,且不接受孤立 marker 或帶附字的 marker"
        )
    return "unified_diff"


# ============================================================
# SEARCH/REPLACE parser(canonical raw grammar,無 fence)
# ============================================================
@dataclass(frozen=True)
class SRBlock:
    index: int                 # 整份 patch 內的 1-based 順序
    path: str                  # raw path 行(尚未驗證)
    search: tuple[str, ...]
    replace: tuple[str, ...]
    path_line: int


def parse_search_replace(text: str) -> list[SRBlock]:
    """嚴格狀態機:OUTSIDE(只允許空行 / path 行)→ SEARCH → REPLACE → OUTSIDE。

    marker 必須是完整 logical line、逐字相等;mixed、孤立 marker、重複 separator、
    缺 REPLACE、marker 外垃圾全部 fail-loud。search / replace 行保留原文(不 strip)。
    """
    lines = text.split("\n")
    blocks: list[SRBlock] = []
    state = "outside"
    pending: tuple[str, int] | None = None
    search: list[str] = []
    replace: list[str] = []
    index = 0
    for lineno, line in enumerate(lines, 1):
        if state == "outside":
            if not line.strip():
                continue
            if line == SR_SEARCH:
                if pending is None:
                    raise PatchFormatError(f"區塊 {index + 1}: marker 前沒有 path 行（行 {lineno}）")
                state, search, replace = "search", [], []
                index += 1
                continue
            if line in SR_MARKERS:
                raise PatchFormatError(f"孤立 marker '{line}'（行 {lineno}）")
            if _is_udiff_control(line):
                raise PatchFormatError("混用 unified diff 與 SEARCH/REPLACE（同一輸入只允許一種格式）")
            if pending is not None:
                raise PatchFormatError(
                    f"marker 外有非空內容（行 {lineno}）：path 行 '{safe_display(pending[0], 60)}' "
                    "後面必須緊接 <<<<<<< SEARCH"
                )
            pending = (line, lineno)
        elif state == "search":
            if line == SR_SEP:
                state = "replace"
                continue
            if line in (SR_SEARCH, SR_REPLACE):
                raise PatchFormatError(f"區塊 {index}: 缺少 =======（行 {lineno} 遇到 {line}）")
            search.append(line)
        else:
            if line == SR_REPLACE:
                assert pending is not None
                blocks.append(SRBlock(index, pending[0], tuple(search), tuple(replace), pending[1]))
                pending = None
                state = "outside"
                continue
            if line == SR_SEP:
                raise PatchFormatError(f"區塊 {index}: 重複的 =======（行 {lineno}）")
            if line == SR_SEARCH:
                raise PatchFormatError(
                    f"區塊 {index}: 缺少 >>>>>>> REPLACE（行 {lineno} 遇到下一個 <<<<<<< SEARCH）"
                )
            replace.append(line)
    if state != "outside":
        raise PatchFormatError(f"區塊 {index} 未結束（缺 ======= 或 >>>>>>> REPLACE）")
    if pending is not None:
        raise PatchFormatError(f"marker 外有非空內容（行 {pending[1]}）：path 行後沒有 <<<<<<< SEARCH")
    if not blocks:
        raise PatchFormatError("沒有任何 SEARCH/REPLACE 區塊")
    return blocks


# ============================================================
# path 規則與安全顯示
# ============================================================
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_ESCAPED_CATEGORIES = ("Cc", "Cf", "Cs", "Co")


def _has_control_chars(text: str) -> bool:
    return any(unicodedata.category(ch) in ("Cc", "Cf") for ch in text)


def ensure_utf8_encodable(lines, what: str) -> None:
    for line in lines:
        try:
            str(line).encode("utf-8")
        except UnicodeEncodeError as exc:
            raise PatchFormatError(f"{what} 含無法以 UTF-8 編碼的字元（{exc.reason}）") from exc


def validate_sr_path(raw: str) -> str:
    """S/R 的 path:UTF-8、repo-relative POSIX、canonical spelling;其餘全拒(不 strip、不猜)。"""
    if raw != raw.strip():
        raise PatchFormatError("路徑無效（path 行含前後空白）")
    if not raw:
        raise PatchFormatError("路徑無效（空 path）")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PatchFormatError("路徑無效（含無法以 UTF-8 編碼的字元）") from exc
    if "\x00" in raw or _has_control_chars(raw):
        raise PatchFormatError("路徑無效（含 NUL 或控制字元）")
    if "\\" in raw:
        raise PatchFormatError("路徑無效（含反斜線;請用 POSIX 的 /,且不接受 UNC）")
    if raw.startswith("/"):
        raise PatchFormatError("路徑無效（絕對路徑 / UNC;必須是 repo 相對路徑）")
    if _DRIVE_RE.match(raw):
        raise PatchFormatError("路徑無效（Windows drive）")
    if raw in ("/dev/null", "dev/null"):
        raise PatchFormatError("路徑無效（/dev/null）")
    parts = raw.split("/")
    if any(part == "" for part in parts):
        raise PatchFormatError("路徑無效（含空 component:// 或結尾 /）")
    if any(part in (".", "..") for part in parts):
        raise PatchFormatError("路徑無效（含 . 或 .. component）")
    return raw


def clean_udiff_path(raw: str) -> str:
    """udiff 的 path:容忍 ``./`` 與 ``//``(沿用既有 resolve 行為),其餘與 S/R 同樣拒絕。"""
    raw = str(raw).strip()
    if not raw or "\x00" in raw or _has_control_chars(raw):
        raise PatchFormatError("路徑無效")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PatchFormatError("路徑無效") from exc
    if "\\" in raw or raw.startswith("/") or _DRIVE_RE.match(raw):
        raise PatchFormatError("路徑無效")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise PatchFormatError("路徑無效")
    return "/".join(parts)


def has_symlink_component(root: Path, rel: str) -> bool:
    """lexical path 上任一 component(含目標本身)是 symlink → True;不存在的部分忽略。"""
    path = Path(root)
    for comp in rel.split("/"):
        path = path / comp
        try:
            st = os.lstat(path)
        except OSError:
            return False
        if stat.S_ISLNK(st.st_mode):
            return True
    return False


def safe_display(text: str, limit: int = DISPLAY_LINE_MAX) -> str:
    """使用者可控欄位(path / 來源行 / block label)進輸出前統一轉義控制字元並截斷。

    只走到 limit+1 個字元就停,巨型輸入不會被整條展開。
    """
    text = str(text)
    out = []
    for ch in text[:limit + 1]:
        if ch == "\t":
            out.append("\\t")
        elif unicodedata.category(ch) in _ESCAPED_CATEGORIES:
            code = ord(ch)
            out.append(f"\\x{code:02x}" if code < 256 else f"\\u{code:04x}")
        else:
            out.append(ch)
    shown = "".join(out)
    if len(text) > limit:
        shown = "".join(out[:limit]) + "…"
    return shown


# ============================================================
# byte-level 快照與還原
# ============================================================
@dataclass(frozen=True)
class FileSnapshot:
    raw: bytes
    text: str                  # logical LF、無 BOM
    bom: bool
    newline: str               # "\n" 或 "\r\n"
    has_final_newline: bool
    mode: int                  # st_mode & 0o7777
    ino: int
    dev: int


def decode_snapshot(raw: bytes, *, mode: int = 0o644, ino: int = 0, dev: int = 0) -> FileSnapshot:
    bom = raw.startswith(UTF8_BOM)
    body = raw[len(UTF8_BOM):] if bom else raw
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        offset = exc.start + (len(UTF8_BOM) if bom else 0)
        raise SnapshotError(f"不是 UTF-8 文字（strict decode 失敗於 byte {offset}）") from exc
    crlf = text.count("\r\n")
    lone_cr = text.count("\r") - crlf
    lone_lf = text.count("\n") - crlf
    if lone_cr:
        if crlf or lone_lf:
            raise SnapshotError("換行格式不一致（mixed newline: 含孤立 CR）")
        raise SnapshotError("換行格式不支援（CR-only newline）")
    if crlf and lone_lf:
        raise SnapshotError("換行格式不一致（mixed newline: LF 與 CRLF 並存）")
    newline = "\r\n" if crlf else "\n"
    logical = text.replace("\r\n", "\n") if crlf else text
    return FileSnapshot(
        raw=raw, text=logical, bom=bom, newline=newline,
        has_final_newline=logical.endswith("\n"), mode=mode, ino=ino, dev=dev,
    )


def body_lines(snapshot: FileSnapshot) -> list[str]:
    """S/R 定位用的真實 body lines:final-newline sentinel 與 0-byte 檔都不是可匹配的行。"""
    if snapshot.text == "":
        return []
    lines = snapshot.text.split("\n")
    if snapshot.has_final_newline:
        lines.pop()
    return lines


def render_bytes(snapshot: FileSnapshot, text: str) -> bytes:
    """既有檔:logical LF 文字 → 依快照還原 newline / BOM 的 bytes(新檔另走 serialize_new_file)。"""
    if text and snapshot.text and text.endswith("\n") != snapshot.has_final_newline:
        raise WriteError("內部錯誤: final newline 狀態與快照不一致,拒絕寫出")
    out = text.replace("\n", "\r\n") if snapshot.newline == "\r\n" else text
    data = out.encode("utf-8")
    return UTF8_BOM + data if snapshot.bom else data


def serialize_new_file(lines) -> bytes:
    """新檔固定序列化:每一行(含尾端空白行)都以 LF 結尾、無 BOM;不猜 final newline。"""
    return ("\n".join(str(line) for line in lines) + "\n").encode("utf-8")


# ============================================================
# S/R 定位(exact + rstrip;禁止 strip / 相似度代套;多處匹配一律拒絕)
# ============================================================
def _exact_positions_normalized(file_norm: list[str], pat_norm: list[str]) -> list[int]:
    """逐行 ``rstrip()`` 相等的所有起點(0-based);呼叫端先各自正規化一次,這裡不再 rstrip。"""
    n = len(file_norm) - len(pat_norm) + 1
    if not pat_norm:
        return []
    first = pat_norm[0]
    positions = []
    for i in range(max(0, n)):
        if file_norm[i] != first:
            continue
        if all(file_norm[i + k] == expect for k, expect in enumerate(pat_norm)):
            positions.append(i)
    return positions


def scan_cost_exceeded(file_lines: list[str], pattern: list[str], *, max_cells: int,
                       max_chars: int = SCAN_COST_MAX_CHARS) -> str | None:
    """exact 搜尋 / ranking 之前先算成本;超限回原因字串,否則 None。"""
    cells = len(file_lines) * max(1, len(pattern))
    chars = sum(len(line) for line in file_lines) + sum(len(line) for line in pattern)
    if cells > max_cells or chars > max_chars:
        return f"超過掃描成本上限（檔案 {len(file_lines)} 行 × SEARCH {len(pattern)} 行,{chars} 字元）"
    return None


def locate_sr_blocks(body: list[str], blocks: list[SRBlock], *, path: str = "") -> tuple:
    """回 (plan, err, record)。plan entry 形狀與 udiff 的 _locate_hunks 相同。

    所有 block 都對同一份原始 body 定位;全部唯一且互不重疊才回 plan。
    """
    plan: list[dict] = []
    body_norm = [line.rstrip() for line in body]
    for ordinal, block in enumerate(blocks):
        label = f"SEARCH/REPLACE 區塊 {ordinal + 1}"
        pattern = list(block.search)
        over = scan_cost_exceeded(body, pattern, max_cells=SCAN_COST_MAX_CELLS)
        if over is not None:
            return None, f"{label} {over},請縮小 SEARCH 或分段送", None
        pat_norm = [line.rstrip() for line in pattern]
        positions = _exact_positions_normalized(body_norm, pat_norm)
        if not positions:
            record = nearest_region_record(body, pattern, path=path, block_label=label)
            return None, (
                f"{label} 找不到逐字匹配（exact 逐行比對,只容忍行尾空白;"
                "僅縮排相似的候選不會代套）"
            ), record
        if len(positions) > 1:
            listed = ", ".join(str(p + 1) for p in positions[:MISMATCH_MAX_CANDIDATES])
            record = ambiguity_record(body, positions, path=path, block_label=label)
            return None, (
                f"{label} 在檔案中出現 {len(positions)} 處（行 {listed}）,"
                "S/R 沒有行號提示,拒絕套用（請增加 SEARCH 的前後行讓它唯一）"
            ), record
        plan.append({
            "index": ordinal, "status": "apply", "pos": positions[0],
            "replace_len": len(pattern), "new_lines": list(block.replace), "relocated": False,
        })
    ordered = sorted(plan, key=lambda e: (e["pos"], e["index"]))
    for prev, nxt in zip(ordered, ordered[1:]):
        if prev["pos"] + prev["replace_len"] > nxt["pos"]:
            return None, (
                f"SEARCH/REPLACE 區塊 {prev['index'] + 1} 與區塊 {nxt['index'] + 1} "
                f"定位後重疊（行 {nxt['pos'] + 1} 附近）,請合併成一個區塊"
            ), None
    return plan, None, None


# ============================================================
# E:bounded mismatch 回饋(結構化 record → 單一 renderer 套整次總額)
# ============================================================
@dataclass
class MismatchRecord:
    path: str
    block_label: str
    kind: str                                   # "nomatch" | "ambiguous" | "unavailable"
    best_start: int | None = None               # 1-based
    ranking: str = ""                           # 用哪種 deterministic ranking 挑的視窗
    window: list = field(default_factory=list)  # [(lineno, text)]
    first_diff: tuple | None = None             # (lineno, expected, actual)
    candidates: list = field(default_factory=list)  # [(lineno, text)]
    reason: str = ""


def nearest_region_record(file_lines: list[str], pattern: list[str], *,
                          path: str = "", block_label: str = "") -> MismatchRecord:
    """相似度只做 deterministic ranking(挑顯示視窗),永不產生套用位置。

    成本上限在任何比對之前檢查;排不了名就老實回 candidate unavailable。
    """
    if not pattern:
        return MismatchRecord(path, block_label, "unavailable", reason="SEARCH 為空,無可比較區域")
    if not file_lines:
        return MismatchRecord(path, block_label, "unavailable", reason="檔案沒有可比較的內容（空檔）")
    over = scan_cost_exceeded(file_lines, pattern, max_cells=RANK_COST_MAX_CELLS)
    if over is not None:
        return MismatchRecord(path, block_label, "unavailable", reason=over)
    file_norm = [line.strip() for line in file_lines]
    pat_norm = [line.strip() for line in pattern]
    best_pos, best_score = 0, -1
    for i in range(len(file_norm)):
        score = 0
        for k, expect in enumerate(pat_norm):
            if i + k >= len(file_norm):
                break
            if file_norm[i + k] == expect:
                score += 1
        if score > best_score:
            best_pos, best_score = i, score
    ranking = "loose 行比對排名"
    if best_score == 0:
        if len(file_lines) > SCAN_SIMILARITY_MAX_LINES:
            return MismatchRecord(
                path, block_label, "unavailable",
                reason=f"沒有任何一行 loose 命中,且檔案超過相似度排名上限（{SCAN_SIMILARITY_MAX_LINES} 行）",
            )
        anchor = pat_norm[0][:SIMILARITY_SLICE]
        ranked = max(
            ((difflib.SequenceMatcher(None, anchor, line[:SIMILARITY_SLICE]).ratio(), -i)
             for i, line in enumerate(file_norm)),
            default=(0.0, 0),
        )
        best_pos = -ranked[1]
        ranking = "字元相似度排名"
    window_end = min(len(file_lines), best_pos + max(1, min(len(pattern), MISMATCH_WINDOW_MAX_LINES)))
    window = [(i + 1, file_lines[i]) for i in range(best_pos, window_end)]
    first_diff = None
    for k, expect in enumerate(pattern):
        if best_pos + k >= len(file_lines):
            first_diff = (best_pos + k + 1, expect, "<檔案結尾>")
            break
        if file_lines[best_pos + k].rstrip() != expect.rstrip():
            first_diff = (best_pos + k + 1, expect, file_lines[best_pos + k])
            break
    return MismatchRecord(
        path, block_label, "nomatch", best_start=best_pos + 1, ranking=ranking,
        window=window, first_diff=first_diff,
    )


def ambiguity_record(file_lines: list[str], positions: list[int], *,
                     path: str = "", block_label: str = "") -> MismatchRecord:
    candidates = [
        (p + 1, file_lines[p] if p < len(file_lines) else "")
        for p in positions[:MISMATCH_MAX_CANDIDATES]
    ]
    return MismatchRecord(path, block_label, "ambiguous", candidates=candidates)


def _record_preview_lines(record: MismatchRecord) -> list[str]:
    if record.kind == "unavailable":
        return [f"  candidate unavailable（{safe_display(record.reason)}）"]
    if record.kind == "ambiguous":
        lines = [f"  候選位置（前 {MISMATCH_MAX_CANDIDATES} 個,各一行,僅供提示）:"]
        for lineno, text in record.candidates:
            lines.append(f"    行 {lineno}: {safe_display(text, 80)!r}")
        return lines
    lines = [f"  最接近的位置: 行 {record.best_start}（{record.ranking},僅供提示,不會代套）"]
    if record.window:
        lines.append(f"  檔案現況（行 {record.window[0][0]}-{record.window[-1][0]}）:")
        for lineno, text in record.window:
            lines.append(f"    {lineno:4d} | {safe_display(text)}")
    if record.first_diff is not None:
        lineno, expected, actual = record.first_diff
        lines.append(f"  第一個差異: 行 {lineno}")
        lines.append(f"    期望: {safe_display(expected, 80)!r}")
        lines.append(f"    實際: {safe_display(actual, 80)!r}")
    lines.append("  提示: 先 read_file 確認現況,從現況逐字重建 SEARCH/context 後重送。")
    return lines


def render_mismatch_records(records: list, *, max_lines: int = MISMATCH_MAX_LINES,
                            max_chars: int = MISMATCH_MAX_CHARS) -> list[list[str]]:
    """對整次 tool result 套一次總額(省略標記本身計入):只保留完整前綴,超額後不再收任何行。"""
    per_record = [_record_preview_lines(record) for record in records]
    flat = [(idx, line) for idx, lines in enumerate(per_record) for line in lines]
    total_lines = len(flat)
    total_chars = sum(len(line) + 1 for _, line in flat)
    if total_lines <= max_lines and total_chars <= max_chars:
        return [list(lines) for lines in per_record]

    def marker(omitted: int) -> str:
        return f"  （mismatch 預覽已達上限 {max_lines} 行/{max_chars} 字元,省略 {omitted} 行）"

    keep = min(total_lines, max_lines - 1)
    while keep > 0:
        prefix_chars = sum(len(line) + 1 for _, line in flat[:keep])
        if prefix_chars + len(marker(total_lines - keep)) + 1 <= max_chars:
            break
        keep -= 1
    rendered: list[list[str]] = [[] for _ in records]
    for idx, line in flat[:keep]:
        rendered[idx].append(line)
    target = flat[keep - 1][0] if keep > 0 else 0
    if rendered:
        rendered[target].append(marker(total_lines - keep))
    return rendered


# ============================================================
# per-file plan(兩格式共用)
# ============================================================
@dataclass
class FilePlan:
    rel: str
    is_new: bool = False
    new_lines: list = field(default_factory=list)   # 新檔的行向量(serialize_new_file 序列化)
    new_bytes: bytes | None = None                  # 已序列化的新檔內容(udiff 新檔直接給)
    snapshot: FileSnapshot | None = None
    plan: list = field(default_factory=list)
    blocks: int = 0
    budget: int = 0
    parent_identity: tuple | None = None           # preflight 時 parent 目錄的 (dev, ino)


# ============================================================
# 檔案系統層:root-fd 錨定的走訪 / 讀取 / temp / 發布 / journal
# ============================================================
def _read_all(fd: int) -> bytes:
    chunks = []
    while True:
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _identity(st) -> tuple[int, int]:
    return (st.st_dev, st.st_ino)


@dataclass
class _Parent:
    """一次操作內有效的 parent 目錄 fd。"""
    rel_dir: str
    fd: int
    path: Path
    identity: tuple[int, int]


@dataclass
class JournalEntry:
    kind: str                       # "existing" | "new" | "dir"
    rel: str                        # 檔案或目錄的 repo 相對路徑
    parent_identity: tuple          # 操作當時 parent 目錄的 (dev, ino)
    state: str = "prepared"         # "prepared" | "committed"
    tmp: str | None = None          # 仍由本次擁有、尚未清掉的 temp 名稱
    tmp_identity: tuple | None = None
    original_bytes: bytes | None = None
    original_mode: int | None = None
    written_bytes: bytes | None = None
    ino: int | None = None
    dev: int | None = None


class PathOps:
    """從 root 逐層走訪的檔案操作;每次操作都重走 lexical path 並比對 parent 身分。"""

    def __init__(self, root: Path, *, anchored: bool | None = None):
        require_safe_filesystem("apply_patch")
        if not DIRFD_ANCHORING or anchored is False or not _O_NONBLOCK:
            raise DependencyError(
                "apply_patch requires dir_fd, O_NOFOLLOW, O_DIRECTORY and O_NONBLOCK; "
                "use a POSIX Python/filesystem with these safety capabilities."
            )
        self.root = Path(root)
        self.anchored = True
        self.notes: list[str] = []
        self._root_real = self.root.resolve()
        self._root_fd: int | None = os.open(
            str(self.root), os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)

    def close(self) -> None:
        if self._root_fd is not None:
            try:
                os.close(self._root_fd)
            except OSError:
                pass
            self._root_fd = None

    def _sandbox_recheck(self, rel: str) -> None:
        """mkdir 前重驗解析後路徑仍在 root 內(防 symlink 競態把新路徑導出 root)。"""
        candidate = self.root.joinpath(*rel.split("/")) if rel else self.root
        try:
            resolved = candidate.resolve()
            resolved.relative_to(self._root_real)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WriteError(f"{rel}: 解析後路徑不在 sandbox 內,拒絕寫入") from exc

    def _temp_name(self, name: str) -> str:
        return f".{name[:80]}.{os.getpid()}.{os.urandom(4).hex()}.codetrail.tmp"

    @staticmethod
    def _split(rel: str) -> tuple[list[str], str]:
        parts = rel.split("/")
        return parts[:-1], parts[-1]

    # ---- 走訪(每次操作都從 root 重走) ---------------------------------
    @contextlib.contextmanager
    def _parent(self, rel: str, *, create: bool = False, journal: "WriteJournal | None" = None,
                expect: tuple | None = None):
        """走到 rel 的 parent。不存在:create=False → FileNotFoundError;create=True → 逐層
        mkdir 並記 journal。expect 給了就比對 parent (dev, ino),不同即 WriteError。"""
        dir_parts, _ = self._split(rel)
        fd = self._walk_anchored(dir_parts, create=create, journal=journal)
        try:
            st = os.fstat(fd)
            ident = _identity(st)
            if expect is not None and ident != tuple(expect):
                raise WriteError(f"{rel}: parent 目錄身分在 preflight 後改變,中止")
            yield _Parent("/".join(dir_parts), fd, self.root.joinpath(*dir_parts), ident)
        finally:
            os.close(fd)

    def _walk_anchored(self, parts: list[str], *, create: bool, journal) -> int:
        assert self._root_fd is not None
        cur = os.dup(self._root_fd)
        try:
            rel = ""
            for comp in parts:
                rel = f"{rel}/{comp}" if rel else comp
                flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
                try:
                    nfd = os.open(comp, flags, dir_fd=cur)
                except FileNotFoundError:
                    if not create:
                        raise
                    self._sandbox_recheck(rel)
                    parent_ident = _identity(os.fstat(cur))
                    os.mkdir(comp, 0o777, dir_fd=cur)
                    try:
                        st = os.stat(comp, dir_fd=cur, follow_symlinks=False)
                        if not stat.S_ISDIR(st.st_mode):
                            raise WriteError(f"{rel}: 建立目錄後身分不是目錄,中止")
                        if journal is not None:
                            journal.record_dir(rel, parent_ident, st)
                    except BaseException:
                        # identity-safe 清理:rmdir 只會移除空目錄,搶進來的非空 / 非目錄不會被動到
                        try:
                            os.rmdir(comp, dir_fd=cur)
                        except OSError:
                            pass
                        raise
                    nfd = os.open(comp, flags, dir_fd=cur)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise SnapshotError(SYMLINK_REFUSED) from exc
                    raise
                os.close(cur)
                cur = nfd
            return cur
        except BaseException:
            os.close(cur)
            raise

    # ---- 單一 entry 的原子操作(相對於一次 _parent) ----------------------
    def _stat_entry(self, parent: _Parent, name: str):
        try:
            return os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def _open(self, parent: _Parent, name: str, flags: int, mode: int = 0o666) -> int:
        return os.open(name, flags | _O_CLOEXEC, mode, dir_fd=parent.fd)

    def _open_regular(self, parent: _Parent, name: str) -> int | None:
        """A2:先 nofollow stat 必須 S_ISREG,再 O_NONBLOCK|O_NOFOLLOW 開啟並立刻 fstat 再確認。"""
        st0 = self._stat_entry(parent, name)
        if st0 is None:
            return None
        if stat.S_ISLNK(st0.st_mode):
            raise SnapshotError(SYMLINK_REFUSED)
        if not stat.S_ISREG(st0.st_mode):
            raise SnapshotError(NOT_REGULAR)
        try:
            fd = self._open(parent, name, os.O_RDONLY | _O_NONBLOCK | _O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise SnapshotError(SYMLINK_REFUSED) from exc
            raise SnapshotError(f"無法開啟: {exc.strerror}") from exc
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or _identity(st) != _identity(st0):
            os.close(fd)
            raise SnapshotError(NOT_REGULAR)
        return fd

    def _create_temp(self, parent: _Parent, name: str) -> tuple[str, int]:
        """O_EXCL 建立同目錄唯一 temp;碰撞就換名重試,絕不碰別人的檔。回 (tmp, fd)。"""
        for _ in range(TEMP_NAME_ATTEMPTS):
            tmp = self._temp_name(name)
            try:
                fd = self._open(parent, tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o666)
            except FileExistsError:
                continue
            return tmp, fd
        raise WriteError(f"{parent.rel_dir or '.'}: 無法建立唯一的 temp 檔（連續 {TEMP_NAME_ATTEMPTS} 次碰撞）")

    def _replace(self, parent: _Parent, src: str, dst: str) -> None:
        os.replace(src, dst, src_dir_fd=parent.fd, dst_dir_fd=parent.fd)
        try:
            os.fsync(parent.fd)
        except OSError:
            pass

    def _unlink(self, parent: _Parent, name: str) -> None:
        os.unlink(name, dir_fd=parent.fd)

    def _rmdir(self, parent: _Parent, name: str) -> None:
        os.rmdir(name, dir_fd=parent.fd)

    # ---- 公開:preflight 讀取 ---------------------------------------------
    def read_snapshot(self, rel: str) -> tuple:
        """回 (snapshot | None, parent_identity | None)。目標不存在 → (None, identity 或 None)。"""
        _, name = self._split(rel)
        try:
            with self._parent(rel) as parent:
                fd = self._open_regular(parent, name)
                if fd is None:
                    return None, parent.identity
                try:
                    st = os.fstat(fd)
                    raw = _read_all(fd)
                finally:
                    os.close(fd)
                snapshot = decode_snapshot(raw, mode=stat.S_IMODE(st.st_mode), ino=st.st_ino, dev=st.st_dev)
                return snapshot, parent.identity
        except FileNotFoundError:
            return None, None

    # ---- 公開:寫入 -------------------------------------------------------
    def ensure_parents(self, rel: str, journal: "WriteJournal") -> tuple:
        """全量 preflight 通過後才建立父目錄(逐層記 journal);回 parent (dev, ino)。"""
        with self._parent(rel, create=True, journal=journal) as parent:
            return parent.identity

    def _fill_temp(self, parent: _Parent, name: str, rel: str, data: bytes, mode: int | None,
                   journal: "WriteJournal", kind: str, **entry_fields) -> tuple[JournalEntry, os.stat_result]:
        """建立 temp(成功後才登記 journal)、寫滿、fsync、fchmod;回 (entry, temp 的 fstat)。"""
        tmp, tfd = self._create_temp(parent, name)
        try:
            tst = os.fstat(tfd)
            entry = journal.prepare(kind, rel, parent.identity, tmp, _identity(tst), **entry_fields)
        except BaseException:
            os.close(tfd)
            try:
                self._unlink(parent, tmp)
            except OSError:
                pass
            raise
        try:
            _write_all(tfd, data)
            os.fsync(tfd)
            if mode is not None:
                os.fchmod(tfd, mode)
            tst = os.fstat(tfd)
        finally:
            os.close(tfd)
        return entry, tst

    def write_existing(self, rel: str, expect_parent: tuple, snapshot: FileSnapshot,
                       data: bytes, journal: "WriteJournal") -> None:
        """重走 parent(比對身分)→ 驗 preimage → temp → os.replace → 立即 committed → 驗 target。"""
        _, name = self._split(rel)
        with self._parent(rel, expect=expect_parent) as parent:
            fd = self._open_regular(parent, name)
            if fd is None:
                raise WriteError(f"{rel}: preimage 已變更（preflight 後檔案消失）,中止")
            try:
                st = os.fstat(fd)
                raw = _read_all(fd)
            finally:
                os.close(fd)
            if _identity(st) != (snapshot.dev, snapshot.ino) or raw != snapshot.raw:
                raise WriteError(f"{rel}: preimage 已變更（preflight 後被外部修改）,中止")
            entry, tst = self._fill_temp(
                parent, name, rel, data, snapshot.mode, journal, "existing",
                original_bytes=snapshot.raw, original_mode=snapshot.mode, written_bytes=data,
            )
            self._replace(parent, entry.tmp, name)
            journal.commit(entry, tst)          # publish 成功後立即、不可失敗
            after = self._stat_entry(parent, name)
            if after is None or _identity(after) != _identity(tst):
                raise WriteError(f"{rel}: 發布後 target 身分立即被替換,中止")

    def write_new(self, rel: str, expect_parent: tuple, data: bytes, journal: "WriteJournal") -> None:
        """重走 parent → temp → hard link 原子發布,不覆蓋競態同名檔。"""
        _, name = self._split(rel)
        with self._parent(rel, expect=expect_parent) as parent:
            if self._stat_entry(parent, name) is not None:
                raise WriteError(f"{rel}: 新檔在 preflight 後被其他程序建立,中止")
            entry, tst = self._fill_temp(parent, name, rel, data, None, journal, "new", written_bytes=data)
            try:
                os.link(entry.tmp, name, src_dir_fd=parent.fd, dst_dir_fd=parent.fd,
                        follow_symlinks=False)
            except FileExistsError as exc:
                raise WriteError(f"{rel}: 新檔在 preflight 後被其他程序建立,中止") from exc
            except (OSError, NotImplementedError) as exc:
                raise WriteError(
                    f"{rel}: atomic no-clobber hard link 發布失敗;"
                    "請使用支援 hard link 的檔案系統,本次寫入將 rollback"
                ) from exc
            journal.commit(entry, tst)      # link 共用 temp 的 inode
            tmp = entry.tmp
            try:
                self._unlink(parent, tmp)
            except OSError as exc:
                raise WriteError(f"{rel}: 發布後無法清除 temp {tmp}: {exc.strerror}") from exc
            entry.tmp = None
            entry.tmp_identity = None
            after = self._stat_entry(parent, name)
            if after is None or _identity(after) != _identity(tst):
                raise WriteError(f"{rel}: 發布後 target 身分立即被替換,中止")

    # ---- 公開:rollback 原子操作(每個都重走 parent 並比對身分) --------------
    def discard_temp(self, entry: JournalEntry) -> str | None:
        """移除本次擁有的 temp;身分不符就不動並回報。"""
        if not entry.tmp:
            return None
        with self._parent(entry.rel, expect=entry.parent_identity) as parent:
            st = self._stat_entry(parent, entry.tmp)
            if st is None:
                entry.tmp = None
                return None
            if entry.tmp_identity is not None and _identity(st) != tuple(entry.tmp_identity):
                return f"⚠ rollback: temp {entry.tmp} 身分不符,保留"
            self._unlink(parent, entry.tmp)
            entry.tmp = None
            return None

    def restore_existing(self, entry: JournalEntry) -> str | None:
        """A6:同一個 opened fd 的 fstat identity + bytes 同時比對;restore temp 失敗路徑必清。"""
        _, name = self._split(entry.rel)
        with self._parent(entry.rel, expect=entry.parent_identity) as parent:
            try:
                fd = self._open(parent, name, os.O_RDONLY | _O_NONBLOCK | _O_NOFOLLOW)
            except FileNotFoundError:
                return f"⚠ rollback conflict: {entry.rel} 在本次寫入後被移除,保留現況"
            except OSError:
                return f"⚠ rollback conflict: {entry.rel} 在本次寫入後被替換,保留現況"
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or _identity(st) != (entry.dev, entry.ino):
                    return f"⚠ rollback conflict: {entry.rel} 在本次寫入後被替換或移除,保留現況"
                current = _read_all(fd)
            finally:
                os.close(fd)
            if current != entry.written_bytes:
                return f"⚠ rollback conflict: {entry.rel} 在本次寫入後又被外部修改,保留現況"
            tmp, tfd = self._create_temp(parent, name)
            published = False
            try:
                try:
                    _write_all(tfd, entry.original_bytes or b"")
                    os.fsync(tfd)
                    if entry.original_mode is not None:
                        os.fchmod(tfd, entry.original_mode)
                finally:
                    os.close(tfd)
                self._replace(parent, tmp, name)
                published = True
            finally:
                if not published:
                    try:
                        self._unlink(parent, tmp)
                    except OSError:
                        pass
            return None

    def remove_new(self, entry: JournalEntry) -> str | None:
        _, name = self._split(entry.rel)
        with self._parent(entry.rel, expect=entry.parent_identity) as parent:
            st = self._stat_entry(parent, name)
            if st is None:
                return None
            if _identity(st) != (entry.dev, entry.ino):
                return f"⚠ rollback conflict: {entry.rel} 已被其他檔案取代,保留現況"
            self._unlink(parent, name)
            return None

    def remove_dir(self, entry: JournalEntry) -> str | None:
        _, name = self._split(entry.rel)
        with self._parent(entry.rel, expect=entry.parent_identity) as parent:
            st = self._stat_entry(parent, name)
            if st is None:
                return None
            if not stat.S_ISDIR(st.st_mode) or _identity(st) != (entry.dev, entry.ino):
                return f"⚠ rollback conflict: 目錄 {entry.rel} 已被取代,保留現況"
            try:
                self._rmdir(parent, name)
            except OSError as exc:
                if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    return f"⚠ rollback: 目錄 {entry.rel} 非空,保留"
                raise
            return None


class WriteJournal:
    """記錄本次寫入,失敗時反向 best-effort rollback(只還原仍是本次 postimage 的檔案)。"""

    def __init__(self, ops: PathOps):
        self.ops = ops
        self.entries: list[JournalEntry] = []

    def record_dir(self, rel_dir: str, parent_identity: tuple, st) -> None:
        self.entries.append(JournalEntry(
            "dir", rel_dir, tuple(parent_identity), state="committed", ino=st.st_ino, dev=st.st_dev,
        ))

    def prepare(self, kind: str, rel: str, parent_identity: tuple, tmp: str, tmp_identity: tuple, *,
                original_bytes: bytes | None = None, original_mode: int | None = None,
                written_bytes: bytes | None = None) -> JournalEntry:
        """只在 temp 已由本次以 O_EXCL 建立成功之後呼叫(ownership 確定)。"""
        entry = JournalEntry(
            kind, rel, tuple(parent_identity), state="prepared", tmp=tmp, tmp_identity=tuple(tmp_identity),
            original_bytes=original_bytes, original_mode=original_mode, written_bytes=written_bytes,
        )
        self.entries.append(entry)
        return entry

    @staticmethod
    def commit(entry: JournalEntry, st) -> None:
        """publish 成功後立即呼叫;純賦值、不可失敗。identity 取自本次自有 temp 的 fstat。"""
        if st is None:
            raise WriteError(f"{entry.rel}: commit 缺少發布身分,不得回報成功")
        entry.ino, entry.dev = st.st_ino, st.st_dev
        entry.state = "committed"
        if entry.kind == "existing":
            entry.tmp = None
            entry.tmp_identity = None

    def rollback(self) -> list[str]:
        notes: list[str] = []
        for entry in reversed(self.entries):
            try:
                note = self.ops.discard_temp(entry)
                if note:
                    notes.append(note)
                if entry.state != "committed":
                    continue
                if entry.kind == "existing":
                    note = self.ops.restore_existing(entry)
                elif entry.kind == "new":
                    note = self.ops.remove_new(entry)
                else:
                    note = self.ops.remove_dir(entry)
                if note:
                    notes.append(note)
            except Exception as exc:  # noqa: BLE001 - rollback 必須把每一筆都試完
                notes.append(f"⚠ 回滾 {entry.rel} 失敗: {type(exc).__name__}: {safe_display(str(exc))}")
        return notes
