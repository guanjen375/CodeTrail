#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""figure_verify — capability probe、structured 抽取、格/行級 verifier。

北極星（workflow §0）：**verified-or-abstain**。本模組的每一條路徑都必須落在
「程式能以獨立證據確認」或「誠實標記待審 / 整份零寫入」兩者之一，中間沒有
「看起來像對的就先入庫」。

── 兩條 lane（契約 §12.1，**推翻**了「native 不夠好就補一次 VL」的舊解讀）──
* **native lane**：候選有可用的原生文字/幾何（markdown `pos` 切片、`find_tables`
  幾何、頁面 word band）。payload 由原生通道組出來，**永遠不呼叫 VL**。
  三通道一致 → `native_verified`；只有一個通道 → `unverified`（合法終態，不是
  「還要再驗」）；通道互相矛盾 → 該格 `▯` + `needs_review`。
* **VL lane**：候選**沒有**任何可用原生文字（純向量繪製的 log、掃描表格）。
  這時沒有任何 anchor，唯一的驗證手段是第二次取樣的 disagreement detection，
  而它最好也只到 `unverified`（契約 §3：無 anchor 的同模型多次取樣即使全等也
  只到這級）。

結果：純文字 + 原生表格的 PDF 是**零 VL 呼叫**，也不需要 capability probe。

── 抽取失敗 vs 驗證等級不足（兩件不同的事）────────────────────────────
* **抽取失敗**（JSON 壞掉 / schema 不合 / 欄寬不對 / 行 contract 違反 /
  `finish_reason` 截斷 / 接合後 payload 失去合法結構）→ 重試一次
  （`config.FIGURE_EXTRACT_RETRIES`）→ 仍失敗即 `FigureExtractionError`，
  **整份 PDF 零寫入**。沒有自由文字 fallback，沒有 legacy 開關。
* **驗證等級不足**（沒有第二個通道、anchor 覆蓋不全）→ `unverified`，正常入庫，
  但 strict query 用不到它。

── import 紀律 ────────────────────────────────────────────────────────
本模組只 `import figure_extract`（門面已是 PEP 562 lazy `__getattr__`），
**不 import `figure_candidates`**：`Candidate` / `PageEvidence` / `Variant` /
`FigurePlan` 一律 duck-typed 屬性存取，絕不 `isinstance`。module import 期間
不取 `figure_extract` 的屬性（只在函式內取），兩個方向的 import 順序都安全。

── 取樣與可重現性 ──────────────────────────────────────────────────────
取樣以單一 `top_k=1` 收斂。llama.cpp 的實際 greedy 行為依 server 版本與 sampler
chain 而定，本模組**不假設**輸出可重現；重複性是每次 runtime 實測後寫進
`evidence["repeatability"]` 的觀測值，不是前提。
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import config
import figure_extract
import llama_client

if TYPE_CHECKING:  # pragma: no cover - 只為型別；執行期不 import（避免循環）
    from figure_candidates import Candidate, FigurePlan, PageEvidence, Variant


# ============================================================
# 1. 模組常數
# ============================================================
# 這些是「實作細節門檻」（排版容差、對齊比例），不是可攜預算旋鈕，所以刻意
# **不進 config.py**（契約 §13.2 對 T3 的同一裁決）。
PROBE_VERSION = 1
PROBE_CACHE_SCHEMA = 1
CANARY_SPEC_VERSION = 1
MAX_PROBE_CACHE_ENTRIES = 32

WORD_BBOX_PAD_PT = 2.0           # 候選框適度外擴（PDF pt）；邊界字不得先被丟掉
WORD_OVERLAP_MIN = 0.5           # word 面積落在候選框內的比例
ROW_BAND_OVERLAP_MIN = 0.5       # 視為同一列的垂直重疊比例
CELL_ASSIGN_PAD_PT = 1.0         # word 指派到 cell bbox 的容差
NATIVE_MATCH_MARGIN = 0.15       # find_tables entry 對候選的 IoU 領先幅度
CORROBORATION_MIN_COVERAGE = 1.0 # 刻意寫死：「逐格/逐行一致」不是百分比門檻
HTTP_ERROR_DETAIL_MAX_CHARS = 500
STITCH_MAX_OVERLAP_ATOMS = 64    # 沒有 stitch 提示時，overlap 搜尋的上限

# native_verified 的 required check：固定集合，缺任何一個 key 一律當 False。
# 用 `all(dict.values())` 會踩到 `all({}) is True`——那等於「沒驗到就升級」。
NATIVE_REQUIRED_CHECKS = (
    "second_channel",
    "header_agreement",
    "row_count_agreement",
    "cell_geometry",
    "critical_token_agreement",
    "cell_text_agreement",
)

PROBE_CHECK_ORDER = (
    "image_content_part",
    "json_schema_accepted",
    "response_not_truncated",
    "json_parsable",
    "required_fields_present",
    "canonicalizable",
    "validator_pass",
    "image_changes_output",
)

# 同一個行程內的 probe pass（避免 RAG 那次與 extract_document_figures 那次在
# 快取不可寫時各跑一輪 live canary）。**不跨行程**，也不取代 server/model
# fingerprint —— 它只是同一次 ingest 內的去重。
_PROCESS_PROBE_PASSES: dict[str, float] = {}
_PROCESS_PROBE_TTL_SECONDS = 900


# ============================================================
# 2. 對外資料類別（契約 §6.4）
# ============================================================
@dataclass(frozen=True)
class FigureResult:
    """一張候選圖的最終結果。欄位名與順序＝跨模組介面，不得改。

    `row_total` / `line_total` 必須等於 payload 實際的最後一個
    `row_index` / `line_index`（`build_figure_chunks` 會 fail-closed 比對）；
    另一個 kind 的欄位一律 `None`。
    """

    figure_id: str
    document_id: str
    page: int
    figure_index: int
    bbox: tuple
    kind: str
    revision: int
    payload: dict | None
    extraction_status: str
    verification_status: str
    reasons: list[str]
    reason_details: list[str]
    evidence: dict
    occurrences: list[dict]
    model_input_variant: str
    variants: list[str]
    row_total: int | None
    line_total: int | None


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    fingerprint: str
    checks: dict[str, bool]
    missing: list[str]
    detail: str


# ============================================================
# 3. 小工具
# ============================================================
def _json_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ordered_unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _as_bbox(value) -> tuple[float, float, float, float] | None:
    try:
        x0, y0, x1, y1 = (float(v) for v in tuple(value)[:4])
    except (TypeError, ValueError):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _expand(bbox, pad: float):
    x0, y0, x1, y1 = bbox
    return (x0 - pad, y0 - pad, x1 + pad, y1 + pad)


def _area(bbox) -> float:
    x0, y0, x1, y1 = bbox
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _intersection(a, b):
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _iou(a, b) -> float:
    inter = _intersection(a, b)
    if inter is None:
        return 0.0
    inter_area = _area(inter)
    union = _area(a) + _area(b) - inter_area
    return inter_area / union if union > 0 else 0.0


def _contains_center(bbox, word_box, pad: float) -> bool:
    cx = (word_box[0] + word_box[2]) / 2.0
    cy = (word_box[1] + word_box[3]) / 2.0
    x0, y0, x1, y1 = _expand(bbox, pad)
    return x0 <= cx <= x1 and y0 <= cy <= y1


def _http_error_detail(exc: BaseException) -> str:
    """把 `requests.HTTPError` 轉成**安全且精確**的一行診斷。

    契約 §12.2：T1 不加工 4xx 語意，診斷完全由本模組負責。llama.cpp 的 400 body
    有可能把送出去的 prompt 片段原樣吐回來，而那裡面是文件內容（NDA），所以規則
    只有一條方向：**除了巢狀 `error.message`，任何原始字串都不進診斷。**

      * 無 `response` → **只報例外類型名**（`str(exc)` 可能已含 body）；
      * body 不是 JSON / 不是物件 → 只報 status code；
      * `error` 不是 dict（例如 `{"error": "<回顯的 prompt>"}`）→ 只報 status code；
      * `error.message` 是 str → 只取它，截到 `HTTP_ERROR_DETAIL_MAX_CHARS`；
      * 讀 body 本身再拋例外時吞掉，不得讓第二個例外遮蔽 status code。
    """
    response = getattr(exc, "response", None)
    if response is None:
        # 刻意不帶 str(exc)：requests 會把 response body 拼進例外訊息。
        return f"{type(exc).__name__}（無 response，內容不回顯）"
    status = getattr(response, "status_code", "?")
    try:
        body = response.json()
    except Exception:
        return f"HTTP {status}（回應非 JSON，內容不回顯）"
    if not isinstance(body, dict):
        return f"HTTP {status}（回應 JSON 非物件，內容不回顯）"
    error = body.get("error")
    if not isinstance(error, dict):
        # 頂層 `error` 是字串時最常見的內容就是被 echo 回來的 prompt。
        return f"HTTP {status}（回應無 error.message 物件，內容不回顯）"
    message = error.get("message")
    if not isinstance(message, str) or not message:
        return f"HTTP {status}（回應無 error.message，內容不回顯）"
    trimmed = message[:HTTP_ERROR_DETAIL_MAX_CHARS]
    suffix = "…（已截斷）" if len(message) > HTTP_ERROR_DETAIL_MAX_CHARS else ""
    return f"HTTP {status}: {trimmed}{suffix}"


_WHITESPACE_CHARS = frozenset(" \t\n\r\v\f　 ")


def _norm_with_map(text: str):
    """`normalize_for_compare()` 的結果 + 每個 normalized 字元回指原字串的 span。

    **為什麼一定要這張表**：normalize 會把連續空白壓成一個空格再 strip，所以
    normalized index ≠ 原字串 index。在 normalized 上找到差異、卻拿那個 index 去
    遮罩原字串，`▯` 會蓋錯字元，terminal 的 `uncertain_spans` 更是明訂以原字串的
    Python index 計算 —— 那是一種安靜的改寫。

    回傳 `(norm, spans)`；`spans[i] == (start, end)` 是 `norm[i]` 對應的原字串區間
    （空白 run 壓成的空格，其 span 會覆蓋整段 run，長度 > 1）。
    上游 normalize 規則若改到與這裡不一致，回 `(None, None)`，呼叫端一律降級成
    「無法定位」而不是照舊遮罩。
    """
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] in _WHITESPACE_CHARS:
            j = i
            while j < n and text[j] in _WHITESPACE_CHARS:
                j += 1
            chars.append(" ")
            spans.append((i, j))
            i = j
        else:
            chars.append(text[i])
            spans.append((i, i + 1))
            i += 1
    # strip：壓縮後首尾的空白最多各一個（任何 " " 都必然來自空白 run）
    start = 1 if chars and chars[0] == " " else 0
    end = len(chars)
    if end > start and chars[end - 1] == " ":
        end -= 1
    chars = chars[start:end]
    spans = spans[start:end]
    norm = "".join(chars)
    if norm != figure_extract.normalize_for_compare(text):
        return None, None
    return norm, spans


def _merge_raw_spans(items: list[tuple[int, int, list[str]]]) -> list[tuple[int, int, list[str]]]:
    """把逐字元的差異 span 合併成連續區間（alternatives 兩側各自串接）。"""
    merged: list[tuple[int, int, list[str]]] = []
    for start, end, alts in sorted(items):
        if merged and start == merged[-1][1]:
            prev_start, _prev_end, prev_alts = merged[-1]
            joined = [prev_alts[k] + alts[k] for k in range(len(alts))]
            merged[-1] = (prev_start, end, joined)
        else:
            merged.append((start, end, list(alts)))
    return merged


def _compare_atom(payload_text: str, anchor_text: str):
    """比對一個原子（cell 或 line）。回傳 `(verdict, spans)`。

    verdict ∈ {`match`, `glyph`, `structural`, `unmappable`}；`glyph` 的 spans 是
    **payload 原字串座標**的 `(start, end, [payload_seg, anchor_seg])`。

    比對走 `normalize_for_compare`：截圖與 PDF word 抽取都證明不了原檔用的是 tab
    還是多個 space，所以空白差異不算衝突。**這也意味著 terminal 的 `corroborated`
    不等於逐位元組一致** —— canonical 文字仍逐位元組保留，只是佐證做不到那麼細。
    """
    payload_norm, payload_spans = _norm_with_map(payload_text)
    anchor_norm, anchor_spans = _norm_with_map(anchor_text)
    if payload_norm is None or anchor_norm is None:
        return "unmappable", []
    if payload_norm == anchor_norm:
        return "match", []
    if len(payload_norm) != len(anchor_norm):
        return "structural", []
    diffs: list[tuple[int, int, list[str]]] = []
    for index, (left, right) in enumerate(zip(payload_norm, anchor_norm)):
        if left == right:
            continue
        p_start, p_end = payload_spans[index]
        if p_end - p_start != 1:
            # 差異落在被壓縮的空白 run 上：定位不到單一字元
            return "structural", []
        a_start, a_end = anchor_spans[index]
        diffs.append((p_start, p_end, [payload_text[p_start:p_end], anchor_text[a_start:a_end]]))
    if not diffs:
        return "structural", []
    return "glyph", _merge_raw_spans(diffs)


def _mask_text(text: str, spans: Sequence[tuple[int, int, list[str]]]) -> str:
    """把 spans 指定的區間換成等長的 `▯`（由後往前，index 不位移）。"""
    glyph = figure_extract.UNREADABLE_GLYPH
    out = text
    for start, end, _alts in sorted(spans, reverse=True):
        out = out[:start] + glyph * (end - start) + out[end:]
    return out


def _all_glyph_placeholder(alternatives: Sequence[str]) -> tuple[str, list[dict]]:
    """結構性衝突時的**安全占位**：正文一個 `▯`，兩份候選只進 alternatives。

    留下其中一份候選文字等於「擇一」，那正是北極星禁止的事。
    """
    glyph = figure_extract.UNREADABLE_GLYPH
    return glyph, [{"start": 0, "end": 1, "alternatives": list(alternatives)}]


# ============================================================
# 4. findings 累加器
# ============================================================
@dataclass
class _Findings:
    """驗證過程的觀察值。blocker → `needs_review`；note 只記錄，不改狀態。"""

    blockers: list[tuple[str, str]] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)
    native_checks: dict[str, bool] | None = None
    channels: list[str] = field(default_factory=list)
    atoms_total: int = 0
    atoms_nonempty: int = 0
    atoms_anchorable: int = 0
    atoms_matched: int = 0

    def block(self, slug: str, detail: str) -> None:
        self.blockers.append((slug, detail))

    def note(self, slug: str, detail: str) -> None:
        self.notes.append((slug, detail))

    def channel(self, name: str) -> None:
        if name not in self.channels:
            self.channels.append(name)

    def slugs(self) -> list[str]:
        return _ordered_unique([s for s, _ in self.blockers] + [s for s, _ in self.notes])

    def details(self) -> list[str]:
        return _ordered_unique([d for _, d in self.blockers] + [d for _, d in self.notes])


# ============================================================
# 5. capability probe（workflow §4 Step 1；契約 §6.4）
# ============================================================
# canary 圖一律在 code 內即時產生：repo 裡不放二進位檔，內容也刻意是通用 ASCII
# （NDA-free）。3 欄與 5 欄兩張表是**全域 image-dependence probe**：同一個 prompt
# 配兩張欄數不同的圖，輸出欄數必須不同，否則無法排除「server 靜默忽略 image
# content part」——`llama_client.vision_completion` 的 docstring 記錄過這個真實
# 歷史 bug（舊 `/completion` 的 image_data 被無聲忽略，模型改用 prompt 幻想圖）。
_CANARY_TABLE_3COL = {
    "kind": "table",
    "header": ["ID", "VALUE", "MODE"],
    "rows": [["A1", "0x10", "RW"], ["A2", "0x20", "RO"]],
}
_CANARY_TABLE_5COL = {
    "kind": "table",
    "header": ["ID", "VALUE", "BITS", "MODE", "NOTE"],
    "rows": [["A1", "0x10", "[3:0]", "RW", "one"],
             ["A2", "0x20", "[7:4]", "RO", "two"]],
}
_CANARY_TERMINAL = {
    "kind": "terminal",
    "lines": ["$ echo hello", "hello", "$ exit"],
}


def _import_pymupdf():
    try:
        import pymupdf  # noqa: PLC0415 - lazy：pymupdf 是 PDF 路徑的相依，不是本模組的
        return pymupdf
    except ImportError:
        pass
    try:
        import fitz  # noqa: PLC0415
        return fitz
    except ImportError as exc:
        raise figure_extract.FigureCapabilityError(
            "capability probe 需要 PyMuPDF 產生 canary 圖（PDF 抽取本來就依賴它）："
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _render_canary_png(spec: dict) -> bytes:
    """把 canary 規格畫成一張極小的 PNG（NDA-free、無外部字型檔）。

    用 base-14 的 Courier（`fontname="cour"`），不需要任何字型檔案；圖很小，
    image token 成本可以忽略。
    """
    pymupdf = _import_pymupdf()
    pad, size, line_h = 10.0, 11.0, 18.0
    if spec["kind"] == "table":
        header, rows = spec["header"], spec["rows"]
        col_w = 78.0
        width = pad * 2 + col_w * len(header)
        height = pad * 2 + line_h * (len(rows) + 1)
        doc = pymupdf.open()
        try:
            page = doc.new_page(width=width, height=height)
            grid = [header] + rows
            for r, row in enumerate(grid):
                for c, cell in enumerate(row):
                    x0 = pad + col_w * c
                    y0 = pad + line_h * r
                    page.draw_rect(
                        pymupdf.Rect(x0, y0, x0 + col_w, y0 + line_h),
                        color=(0, 0, 0), width=0.8,
                    )
                    page.insert_text(
                        (x0 + 4.0, y0 + line_h - 5.0), str(cell),
                        fontname="cour", fontsize=size, color=(0, 0, 0),
                    )
            return page.get_pixmap(dpi=150).tobytes("png")
        finally:
            doc.close()

    lines = spec["lines"]
    width = pad * 2 + 190.0
    height = pad * 2 + line_h * len(lines)
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=width, height=height)
        page.draw_rect(page.rect, color=None, fill=(0.09, 0.09, 0.11))
        for i, text in enumerate(lines):
            page.insert_text(
                (pad, pad + line_h * (i + 1) - 5.0), text,
                fontname="cour", fontsize=size, color=(0.92, 0.92, 0.92),
            )
        return page.get_pixmap(dpi=150).tobytes("png")
    finally:
        doc.close()


# ── prompt：依 backend/model capability 路由，不寫死單一模型 ─────────────
_PROMPT_COMMON = (
    "你是逐字轉錄工具。只根據圖片內容輸出，不推測、不補完、不摘要、不翻譯。\n"
    "大小寫、底線、`0x` 前綴、`[msb:lsb]` 內的空白、反斜線、backtick 一律原樣保留；"
    "圖中的中文是合法原文，不得翻成英文。\n"
    "只輸出 JSON 物件，不要任何說明文字、不要 code fence。\n"
)
_PROMPT_BY_KIND = {
    "table": (
        "把圖中的表格轉成 JSON。\n"
        "- `columns`：依左到右的欄位，`label` 是該欄表頭的原文。\n"
        "- `rows`：每一列一個元素；`cells` 的長度必須**恰好等於** columns 的數量，"
        "順序與 columns 一一對應。看不清的格 `state` 填 \"unreadable\"，其餘填 \"observed\"。\n"
        "- `footnotes`：表格下方的註腳原文，沒有就給空陣列。\n"
        "不要輸出列號、欄位 id 或任何合併儲存格資訊——那些由程式指派。\n"
    ),
    "terminal": (
        "把圖中的終端機/log 畫面轉成 JSON。\n"
        "- `lines`：**一個視覺行一個元素**，不合併、不拆行、不重排；空行也要輸出 "
        "（`text` 給空字串），包括第一行與最後一行。\n"
        "- 看不清的字元在 `text` 放 `▯`，候選寫進 `uncertain_spans` 的 `alternatives`；"
        "**不得**在正文寫成 `[不確定:A|B]` 這種形式。\n"
        "- `uncertain_spans` 的 start/end 是該行 `text` 的字元索引。\n"
        "不要輸出行號——那由程式指派。\n"
    ),
    "diagram": (
        "把圖中的方塊圖/流程圖轉成 JSON：`title`、`labels`、`components`（name/desc）、"
        "`relations`（src/dst/desc）、`values`（key/value/desc）。\n"
        "沒有的欄位給空字串或空陣列，不要省略 key。\n"
    ),
}
_SCHEMA_ECHO_SUFFIX = (
    "\n輸出必須嚴格符合下列 JSON Schema，**不得**出現 schema 以外的鍵：\n{schema}\n"
)


def _resolve_prompt_profile(props: dict | None) -> str:
    """依 server 回報的能力挑 prompt profile。**只看 props，不看 model 名稱。**

    寫死某顆模型的 prompt 會讓別人的 server 拿到不適用的指令，而症狀是「輸出看
    起來正常但欄位對不上」——這正是 workflow §7 禁止的單一模型假設。
    """
    if not isinstance(props, dict) or not props:
        return "schema_echo"
    blob = json.dumps(props, ensure_ascii=False, default=str).lower()
    if "json_schema" in blob or "grammar" in blob or "response_format" in blob:
        return "strict_json"
    return "schema_echo"


def _prompt_for(kind: str, profile: str) -> str:
    body = _PROMPT_COMMON + _PROMPT_BY_KIND[kind]
    if profile == "schema_echo":
        schema = json.dumps(figure_extract.model_json_schema(kind), ensure_ascii=False)
        body += _SCHEMA_ECHO_SUFFIX.format(schema=schema)
    return body


# ── fingerprint / 快取（範本：scripts/tool_call_canary.py）──────────────
_PROPS_FINGERPRINT_KEYS = (
    "model_alias", "model_path", "chat_template", "n_ctx", "n_batch", "n_ubatch",
    "n_parallel", "default_generation_settings", "build_info", "version",
    "total_slots", "modalities", "bos_token", "eos_token",
)


def _model_identity(props: dict | None) -> dict | None:
    """server 回報的**可驗證**模型簽章；驗不出來就回 None（＝一律 live、不快取）。

    llama-server 的 `model` / `model_alias` 只是 informational，換了 GGUF 卻沿用
    同一個 alias 與 port 是最常見的漂移；`model_path` 本身也只是一個字串，
    stat 不到就證明不了背後那顆檔案沒被換掉（同路徑覆蓋是升級模型的標準做法）。
    所以**唯一**算數的是「成功 stat 到的 size + mtime」：

      * 沒有 `model_path`、或 stat 失敗 → None（alias 再明確也不算）；
      * 有 size/mtime → 連同 path hash、alias 與 server build/version 一起回傳。

    回 None 的代價只是每次 ingest 多跑一次 canary；回錯的代價是整條 runtime
    capability gate 被跳過（Go/No-Go 第 8 條）。
    """
    if not isinstance(props, dict) or not props:
        return None
    path = props.get("model_path")
    if not isinstance(path, str) or not path:
        return None
    try:
        info = os.stat(path)
    except OSError:
        return None
    identity: dict[str, Any] = {
        "path_hash": hashlib.sha256(path.encode("utf-8")).hexdigest(),
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
    }
    alias = props.get("model_alias")
    if isinstance(alias, str) and alias:
        identity["alias"] = alias
    for key in ("build_info", "version"):
        value = props.get(key)
        if isinstance(value, (str, int)):
            identity[key] = value
    return identity


def _build_probe_fingerprint(*, base_url: str, model: str, kinds: Sequence[str],
                             props: dict | None, profile: str) -> str:
    """server / model / template / schema / prompt 的合成指紋。

    只有這個 digest 會落地；`base_url`、prompt、schema 原文都只進 hash 的輸入，
    不會被寫進快取檔（見 `_save_probe_pass` 的說明）。
    """
    identity = _model_identity(props)
    if identity is None:
        return ""
    payload = {
        "probe_version": PROBE_VERSION,
        "canary_spec_version": CANARY_SPEC_VERSION,
        "base_url": base_url.rstrip("/"),
        "model": model,
        "kinds": sorted(kinds),
        "profile": profile,
        "model_identity": identity,
        "props": {k: props.get(k) for k in _PROPS_FINGERPRINT_KEYS if k in props},
        "schemas": {k: _json_digest(figure_extract.response_format_for(k)) for k in sorted(kinds)},
        "prompts": {k: _json_digest(_prompt_for(k, profile)) for k in sorted(kinds)},
    }
    return _json_digest(payload)


def _probe_cache_path(cache_path) -> Path:
    if cache_path is not None:
        return Path(cache_path).expanduser()
    return Path(config.FIGURE_PROBE_CACHE_FILE()).expanduser()


def _read_probe_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"schema": PROBE_CACHE_SCHEMA, "passes": {}}
    if not isinstance(data, dict) or data.get("schema") != PROBE_CACHE_SCHEMA:
        return {"schema": PROBE_CACHE_SCHEMA, "passes": {}}
    if not isinstance(data.get("passes"), dict):
        data["passes"] = {}
    return data


def _cached_probe_age(path: Path, fingerprint: str, *, now: float, ttl: int) -> int | None:
    if ttl <= 0 or not fingerprint:
        return None
    entry = _read_probe_cache(path).get("passes", {}).get(fingerprint)
    if not isinstance(entry, dict) or entry.get("status") != "pass":
        return None
    checked_at = entry.get("checked_at")
    if not isinstance(checked_at, (int, float)):
        return None
    age = now - float(checked_at)
    if age < -300 or age > ttl:
        return None
    return max(0, int(age))


def _save_probe_pass(path: Path, fingerprint: str, *, now: float) -> str:
    """寫入一筆 pass。**只存 fingerprint 與時戳**，不存 prompt、模型輸出、專案路徑。

    寫入失敗是 best-effort：能力已經實測通過了，因為快取寫不進去就中止 ingest
    是把便利性問題升級成正確性問題。回傳空字串代表成功，否則回傳原因（呼叫端
    記成 note）。**只快取 pass，不快取 fail** —— 快取失敗會讓一次 server 抖動
    永久擋住入庫。
    """
    data = _read_probe_cache(path)
    passes = data.setdefault("passes", {})
    passes[fingerprint] = {
        "status": "pass",
        "checked_at": now,
        "probe_version": PROBE_VERSION,
    }
    if len(passes) > MAX_PROBE_CACHE_ENTRIES:
        ordered = sorted(
            passes.items(),
            key=lambda item: item[1].get("checked_at", 0) if isinstance(item[1], dict) else 0,
            reverse=True,
        )
        data["passes"] = dict(ordered[:MAX_PROBE_CACHE_ENTRIES])

    tmp_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(stat.S_IRWXU)
        except OSError:
            pass
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(path.parent),
            prefix=f".{path.name}.", delete=False,
        ) as handle:
            tmp_name = handle.name
            json.dump(data, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_name, path)
        tmp_name = None
        return ""
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _canary_call(*, kind: str, png: bytes, base_url: str, model: str, profile: str):
    """跑一張 canary。回傳 `(checks, model_obj, payload, detail)`。

    `image_content_part` 與 `json_schema_accepted` 的區分是**兩步**的：帶
    `response_format` 失敗時，用同一張圖再打一次**不帶** structured output 的
    `vision_completion`。第二次成功 ⇒ 圖片本身是被接受的，問題出在 json_schema；
    第二次也失敗 ⇒ 連 image content part 都不通。單看一個 400 沒辦法分辨這兩件事，
    而它們的處置完全不同（換 server 版本 vs 換模型）。
    """
    checks = {name: False for name in PROBE_CHECK_ORDER if name != "image_changes_output"}
    detail: list[str] = []
    image_b64 = base64.b64encode(png).decode("ascii")
    try:
        result = llama_client.vision_json_completion(
            base_url=base_url,
            prompt=_prompt_for(kind, profile),
            image_base64=image_b64,
            mime_type="image/png",
            model=model,
            max_tokens=int(config.VL_INGEST_MAX_TOKENS),
            response_format=figure_extract.response_format_for(kind),
            temperature=0.0, top_p=1.0, top_k=1,
            timeout=int(config.VL_ANALYZE_TIMEOUT),
            cache_prompt=False,
        )
    except Exception as exc:  # noqa: BLE001 - 探測本來就要吃掉所有失敗並轉成 check
        reason = _http_error_detail(exc)
        try:
            llama_client.vision_completion(
                base_url=base_url, prompt="Reply with the single word OK.",
                image_base64=image_b64, mime_type="image/png", model=model,
                max_tokens=16, timeout=int(config.VL_ANALYZE_TIMEOUT),
            )
        except Exception as plain_exc:  # noqa: BLE001
            detail.append(
                f"帶 schema 失敗（{reason}），不帶 schema 也失敗"
                f"（{_http_error_detail(plain_exc)}）→ 這個 server/模型不吃 image content part"
            )
            return checks, None, None, detail
        checks["image_content_part"] = True
        detail.append(
            f"image content part 可用，但帶 nested json_schema 的請求被拒：{reason}"
        )
        return checks, None, None, detail

    checks["image_content_part"] = True
    checks["json_schema_accepted"] = True
    if result.truncated:
        detail.append(
            f"finish_reason={result.finish_reason!r} → 回應被截斷；"
            "請提高 AICODE_VL_INGEST_MAX_TOKENS"
        )
        return checks, None, None, detail
    checks["response_not_truncated"] = True

    try:
        model_obj = json.loads(result.text.strip())
    except (json.JSONDecodeError, ValueError):
        detail.append("輸出不是可解析的 JSON（不剝 code fence、不抽子字串，也不做救回）")
        return checks, None, None, detail
    if not isinstance(model_obj, dict):
        detail.append(f"輸出 JSON 的頂層不是物件（{type(model_obj).__name__}）")
        return checks, None, None, detail
    checks["json_parsable"] = True

    required = figure_extract.model_json_schema(kind).get("required", [])
    missing_keys = [key for key in required if key not in model_obj]
    if missing_keys:
        detail.append(f"輸出缺少 schema required 欄位：{missing_keys}")
        return checks, model_obj, None, detail
    checks["required_fields_present"] = True

    canonicalize = {
        figure_extract.KIND_TABLE: figure_extract.canonicalize_table,
        figure_extract.KIND_TERMINAL: figure_extract.canonicalize_terminal,
        figure_extract.KIND_DIAGRAM: figure_extract.canonicalize_diagram,
    }[kind]
    try:
        payload = canonicalize(model_obj)
    except figure_extract.FigureValidationError as exc:
        detail.append(f"canonicalize 失敗（欄寬 / 行 contract / 欄位）：{exc}")
        return checks, model_obj, None, detail
    checks["canonicalizable"] = True

    try:
        figure_extract.validate_payload(payload, kind)
    except figure_extract.FigureValidationError as exc:
        detail.append(f"外部 validator 拒絕：{exc}")
        return checks, model_obj, payload, detail
    checks["validator_pass"] = True
    return checks, model_obj, payload, detail


def _probe_kinds(kinds) -> list[str]:
    """把呼叫端要求的 kinds 正規化成需要 canary 的 kind list。"""
    wanted: set[str] = set()
    for kind in kinds or ():
        if kind == figure_extract.KIND_UNKNOWN:
            wanted.update({figure_extract.KIND_TABLE, figure_extract.KIND_TERMINAL})
        elif kind in figure_extract.FIGURE_KINDS:
            wanted.add(kind)
    return sorted(wanted)


def _run_probe(*, base_url: str, model: str, kinds: Sequence[str], profile: str):
    """實際跑 canary。回傳 `(checks, missing, detail_lines)`。

    **一律**先跑 3 欄 / 5 欄的 table pair（全域 image-dependence probe），再依需求
    補該 kind 的 schema canary。因此最多 4 次呼叫（table×2 + terminal + diagram）。

    只驗結構契約（schema / 未截斷 / required / validator / 欄寬 / 行 contract）與
    「圖真的進到模型」。**不驗模型是否答對字**——把準確率當通過條件等於在 CI 之外
    引入一個沒人能保證的假設（workflow §7）。
    """
    checks: dict[str, bool] = {}
    detail: list[str] = []

    pair_payloads = []
    for label, spec in (("3col", _CANARY_TABLE_3COL), ("5col", _CANARY_TABLE_5COL)):
        png = _render_canary_png(spec)
        sub, _obj, payload, lines = _canary_call(
            kind=figure_extract.KIND_TABLE, png=png, base_url=base_url,
            model=model, profile=profile,
        )
        for name, value in sub.items():
            key = f"table.{name}"
            checks[key] = checks.get(key, True) and value
        detail.extend(f"table/{label}: {line}" for line in lines)
        pair_payloads.append(payload)

    if all(p is not None for p in pair_payloads):
        widths = [len(p["columns"]) for p in pair_payloads]
        checks["image_changes_output"] = widths[0] != widths[1]
        if widths[0] == widths[1]:
            detail.append(
                f"兩張欄數不同的 canary（3 欄 / 5 欄）都得到 {widths[0]} 欄："
                "無法排除 server 靜默忽略 image content part（llama.cpp 舊 image_data "
                "路徑就是這樣讓模型改用 prompt 幻想圖片內容）"
            )
    else:
        checks["image_changes_output"] = False
        detail.append("table canary pair 至少一張沒有產出合法 payload，image 依賴性無從證明")

    for kind in kinds:
        if kind == figure_extract.KIND_TABLE:
            continue  # pair 已經涵蓋
        spec = _CANARY_TERMINAL if kind == figure_extract.KIND_TERMINAL else _CANARY_TABLE_3COL
        png = _render_canary_png(spec)
        sub, _obj, _payload, lines = _canary_call(
            kind=kind, png=png, base_url=base_url, model=model, profile=profile,
        )
        for name, value in sub.items():
            checks[f"{kind}.{name}"] = value
        detail.extend(f"{kind}: {line}" for line in lines)

    order = {name: i for i, name in enumerate(PROBE_CHECK_ORDER)}
    missing = sorted(
        (name for name, ok in checks.items() if not ok),
        key=lambda name: (order.get(name.split(".")[-1], 99), name),
    )
    return checks, missing, detail


def ensure_capability(*, base_url: str, model: str, kinds: set[str],
                      cache_path: str | Path | None = None,
                      now: float | None = None) -> ProbeResult:
    """KB mutation 前的 VL capability probe（契約 §6.4）。

    依 server/model/template/schema/prompt 的合成指紋快取與失效；快取檔只存
    fingerprint 與時戳。`kinds` 為空（例如整份 PDF 都走 native lane）→ 直接通過、
    零 VL 呼叫。

    不通過即 raise `FigureCapabilityError`，訊息**逐項**指出缺哪一項能力。
    **絕不**以「OpenAI-compatible」推定品質：回 200 不等於吃了圖，吐 JSON 不等於
    守 schema。
    """
    wanted = _probe_kinds(kinds)
    if not wanted:
        return ProbeResult(
            ok=True, fingerprint="", checks={"skipped": True}, missing=[],
            detail="沒有候選需要 VL（native lane 零呼叫），略過 capability probe",
        )

    moment = time.time() if now is None else float(now)
    props = llama_client.get_props(base_url)
    profile = _resolve_prompt_profile(props)
    fingerprint = _build_probe_fingerprint(
        base_url=base_url, model=model, kinds=wanted, props=props, profile=profile
    )
    path = _probe_cache_path(cache_path)
    ttl = int(config.FIGURE_PROBE_TTL_SECONDS)

    if fingerprint:
        age = _cached_probe_age(path, fingerprint, now=moment, ttl=ttl)
        if age is not None:
            return ProbeResult(
                ok=True, fingerprint=fingerprint, checks={"cached": True}, missing=[],
                detail=f"約 {age // 60} 分鐘前通過，快取命中（profile={profile}）",
            )
        seen = _PROCESS_PROBE_PASSES.get(fingerprint)
        if seen is not None and 0 <= moment - seen <= _PROCESS_PROBE_TTL_SECONDS:
            return ProbeResult(
                ok=True, fingerprint=fingerprint, checks={"process_cached": True}, missing=[],
                detail=f"本次執行稍早已通過（profile={profile}）",
            )

    checks, missing, detail_lines = _run_probe(
        base_url=base_url, model=model, kinds=wanted, profile=profile
    )
    if missing:
        lines = [
            f"VL capability probe 未通過（base_url={base_url}, model={model or '(未指定)'}，"
            f"profile={profile}）："
        ]
        for name in sorted(checks, key=lambda n: (n.split(".")[0], n)):
            lines.append(f"  {'✓' if checks[name] else '✗'} {name}")
        lines.extend(f"  · {line}" for line in detail_lines)
        lines.append(
            "不以「OpenAI-compatible」推定品質：未通過即不進行任何抽取，KB 保持原狀。"
        )
        result = ProbeResult(
            ok=False, fingerprint=fingerprint, checks=checks, missing=missing,
            detail="\n".join(lines),
        )
        error = figure_extract.FigureCapabilityError(result.detail)
        error.probe = result
        raise error

    notes = []
    if fingerprint:
        _PROCESS_PROBE_PASSES[fingerprint] = moment
        failure = _save_probe_pass(path, fingerprint, now=moment)
        if failure:
            notes.append(f"probe 通過但快取寫入失敗（下次會重跑）：{failure}")
    else:
        notes.append(
            "server /props 沒有給出可辨識的模型身分（model_path / model_alias），"
            "本次不快取——換模型卻沿用同一個 alias/port 是最常見的漂移來源"
        )
    return ProbeResult(
        ok=True, fingerprint=fingerprint, checks=checks, missing=[],
        detail="; ".join([f"live probe 通過（profile={profile}）", *notes]),
    )


# ============================================================
# 6. VL lane：structured 抽取
# ============================================================
# 跟著**影像**走的 findings（同一張圖的每個 occurrence 都成立）；其餘都是
# 跟著**這一頁 anchor** 走的 occurrence-level findings，不得跨頁繼承。
_ASSET_LEVEL_SLUGS = frozenset({
    "stitch_uncertain", "stitch_footnote_conflict", "repeat_sample_failed",
    "glyph_conflict", "sample_conflict", "sample_state_conflict", "sample_span_conflict",
    "model_unreadable", "unreadable_content", "two_samples_agree",
})


class _SampleFailure(Exception):
    """單次抽取不合格。`slug` 是穩定的失敗種類，`detail` 給人看。"""

    def __init__(self, slug: str, detail: str):
        super().__init__(detail)
        self.slug = slug
        self.detail = detail


_FAILURE_HINTS = {
    "truncated": "請提高 AICODE_VL_INGEST_MAX_TOKENS，或讓 tile 切小一點",
    "not_json": "server 可能沒有真的套用 grammar 約束；先跑 capability probe 確認",
    "schema": "模型輸出的鍵與 schema 不符",
    "row_width": "每列的 cell 數必須等於欄數——不補不砍",
    "line_contract": "一個 line 必須恰是一個視覺行，不得含 \\n / \\r",
    "empty_payload": "空 payload 不得入庫（那等於宣稱這張圖沒有內容）",
    "canonicalize": "canonicalize 失敗",
    "validator": "外部 validator 拒絕",
}


def _validate_variants(candidate: Candidate, variants) -> list[Variant]:
    """送出前驗 variant 的身分與 tile 完整性——**任何 VL 呼叫之前**的那道閘。

    單一 variant 的欄位合法性一律走門面唯一的 `figure_extract.validate_variant()`
    （契約 §21.1/§21.2）。這裡**不留副本、不做任何 coercion**：本地的 `int()` 正規化
    正是 `tile_total=True` / `"1"` / `1.9` 被截成合法的 `(1, 0)`、字串 `est_image_tokens`
    被轉成正整數而靜默通過的原因。而這道閘在送模之前——等 RAG 或 writer 稍後拒絕時，
    VL 的錢已經花掉了。`Variant` 有四個消費端，判定只留一份，繞道才不會再長回來。

    留在這裡的只有**跨 variant** 的集合性質（共享 validator 看不到單張以外的東西）：
    同一 figure 的 tile_total 必須一致、tile 數不得超過單一候選上限、tile_index 不得
    重號或缺號、每張都得屬於這個候選。缺號 / 重號 / 跨 figure 的 tile 會讓接合安靜
    錯序，而錯序的 log 與錯位的 register 表在外觀上都完全正常。
    """
    figure_id = getattr(candidate, "figure_id", "")
    items = list(variants or [])
    if not items:
        raise figure_extract.FigureExtractionError(
            f"figure={figure_id} 沒有任何可送模型的 variant（render 端沒有產出）"
        )
    candidate_bbox = getattr(candidate, "bbox", None)
    for position, item in enumerate(items, 1):
        where = (f"figure={figure_id} 的第 {position}/{len(items)} 張 variant"
                 "（送 VL 之前）")
        figure_extract.validate_variant(item, where=where)
        # 跨 figure 的 variant：共享 validator 只知道「figure_id 是個非空字串」，
        # 不知道這批應該屬於哪個候選。
        if item.figure_id != figure_id:
            raise figure_extract.FigureExtractionError(
                f"figure={figure_id} 收到屬於 {item.figure_id!r} 的 variant"
            )
        # 契約 §21.10：未切片 ⟹ 這張就是**整個候選框**。`validate_variant()` 依 §21.1
        # 只看單張自身的形狀，看不到候選框；bbox 比對只在 `is_full_image()` 裡。少了
        # 這一步，把某一片 tile 的 bytes 配上合法 flags（tile_index=0 / tile_total=1）
        # 的局部 crop 就會在**送模之前**通關，等 RAG / writer 稍後才拒——錢已經花掉，
        # 而且 REF 的 crop 連結與 manifest 的原始 asset 會指向一張只有一部分的圖。
        # `render_candidate_variants()` 的未切片分支 render 的就是整個候選框，所以
        # 對不上代表 producer 契約漂移，不是可容忍的變形。
        # tiled variant（tile_total >= 2）刻意**不**比對每片的 bbox：tile 不會被任何
        # 一端當成完整原圖（`is_full_image()` 對 tile_total != 1 一律回 False），
        # 冒充路徑不存在。
        if item.tile_total == 1 and not figure_extract.is_full_image(
                item, candidate_bbox=candidate_bbox, where=where):
            raise figure_extract.FigureExtractionError(
                f"{where}: 未 tile（tile_total=1）的 variant "
                f"{item.variant_id!r} bbox={item.bbox!r} 不是完整候選框 "
                f"{candidate_bbox!r}——局部 crop 配上合法 flags 就冒充得了完整原圖，"
                "下游的 REF crop 連結與 manifest 原始 asset 會指向只有一部分的圖"
            )

    # 以下每個欄位都已被共享 validator 保證是合法 int，不需要（也不得）再包 int()。
    totals = {v.tile_total for v in items}
    if len(totals) != 1:
        raise figure_extract.FigureExtractionError(
            f"figure={figure_id} 的 variants 有不一致的 tile_total={sorted(totals)}"
        )
    total = totals.pop()
    max_tiles = int(config.FIGURE_MAX_TILES_PER_CANDIDATE)
    if total > max_tiles:
        # preflight 已經算過一次，但 render 端真的切出幾張才是事實；只靠文件級
        # VL-call 上限擋不住「單一候選被切成幾十張」。
        raise figure_extract.FigureExtractionError(
            f"figure={figure_id} 被切成 {total} 個 tile，超過單一候選上限 {max_tiles}"
            "（送出前複查，第二道；尚未動 KB）"
        )
    indices = [v.tile_index for v in items]
    if len(set(indices)) != len(indices):
        raise figure_extract.FigureExtractionError(
            f"figure={figure_id} 的 tile_index 有重號：{sorted(indices)}"
        )
    if total == 1:
        # 單張的形狀（tile_index 必須是 0）由共享 validator 管；跨 variant 只剩
        # 「未 tile 不得有第二張」——多張同時宣稱 (0, 1) 的話，接合端會以為
        # 只送了一張，其餘的內容靜默消失。
        if len(items) != 1:
            raise figure_extract.FigureExtractionError(
                f"figure={figure_id} 未 tile（tile_total=1）卻有 {len(items)} 張 variant"
                "（未 tile 只能有一張，多出來的會被接合端無聲丟掉）"
            )
    elif sorted(indices) != list(range(1, total + 1)):
        raise figure_extract.FigureExtractionError(
            f"figure={figure_id} 的 tile 不連續：拿到 {sorted(indices)}，"
            f"但 tile_total={total}（缺號的 tile 會讓接合靜默錯序）"
        )
    items.sort(key=lambda v: v.tile_index)
    return items


def _variant_mime(variant) -> str:
    """實際送出的 MIME。

    契約 §13.2：`Variant.png` 在 `variant_id == "raster"` 時裝的是
    `Document.extract_image()` 的原始 binary，**可能不是 PNG**，真實型別看 `mime`。
    預設成 image/png 會讓 server 拿到掛錯副檔名的 JPEG。
    """
    mime = getattr(variant, "mime", "") or ""
    if isinstance(mime, str) and mime.startswith("image/"):
        return mime
    return "image/png"


def _check_send_budget(variant, counters: dict, *, where: str) -> None:
    """真的送出前**再確認一次**預算（preflight 之外的第二道）。

    preflight（T3）算的是計畫值；這裡擋的是「計畫與實際 render 出來的東西不一致」。
    一律在任何 KB mutation 之前。
    """
    # 不做 int()：唯一入口 `_validate_variants()` 已用共享 validator
    # 保證是精確正整數；這裡再轉一次就是消費端又留了一份寬鬆副本（契約 §21.7）。
    tokens = variant.est_image_tokens
    per_call = int(config.FIGURE_MAX_IMAGE_TOKENS_PER_CALL)
    if tokens > per_call:
        raise figure_extract.FigureBudgetError(
            f"{where}: variant {getattr(variant, 'variant_id', '?')!r} 估 {tokens} image tokens，"
            f"超過單次上限 {per_call}（送出前複查，第二道；尚未動 KB）"
        )
    counters["image_tokens"] = counters.get("image_tokens", 0) + tokens
    doc_tokens = int(config.FIGURE_MAX_IMAGE_TOKENS_PER_DOC)
    if counters["image_tokens"] > doc_tokens:
        raise figure_extract.FigureBudgetError(
            f"{where}: 整份文件累計 {counters['image_tokens']} image tokens，"
            f"超過上限 {doc_tokens}（送出前複查，第二道；尚未動 KB）"
        )
    counters["vl_calls"] = counters.get("vl_calls", 0) + 1
    max_calls = int(config.FIGURE_MAX_VL_CALLS_PER_DOC)
    if counters["vl_calls"] > max_calls:
        raise figure_extract.FigureBudgetError(
            f"{where}: 整份文件累計 {counters['vl_calls']} 次 VL 呼叫，"
            f"超過上限 {max_calls}（送出前複查，第二道；尚未動 KB）"
        )


def _parse_sample(kind: str, result) -> dict:
    """把一次 VL 回應轉成 canonical payload；不合格 raise `_SampleFailure`。

    先做逐項 pre-check 再 canonicalize，失敗 slug 才會精準（不會全部糊成 "schema"）。
    """
    if getattr(result, "truncated", True):
        raise _SampleFailure(
            "truncated",
            f"finish_reason={getattr(result, 'finish_reason', '')!r}：回應被截斷"
        )
    text = getattr(result, "text", "")
    try:
        model_obj = json.loads(text.strip())
    except (AttributeError, json.JSONDecodeError, ValueError) as exc:
        raise _SampleFailure("not_json", f"輸出不是可解析的 JSON：{exc}") from exc
    if not isinstance(model_obj, dict):
        raise _SampleFailure("schema", f"輸出 JSON 頂層不是物件（{type(model_obj).__name__}）")

    schema = figure_extract.model_json_schema(kind)
    for key in schema.get("required", []):
        if key not in model_obj:
            raise _SampleFailure("schema", f"輸出缺少 required 欄位 {key!r}")

    if kind == figure_extract.KIND_TABLE:
        columns, rows = model_obj.get("columns"), model_obj.get("rows")
        if not isinstance(columns, list) or not isinstance(rows, list):
            raise _SampleFailure("schema", "columns / rows 必須是 list")
        if not columns or not rows:
            raise _SampleFailure(
                "empty_payload", f"空 payload（columns={len(columns)}, rows={len(rows)}）"
            )
        for position, row in enumerate(rows):
            cells = row.get("cells") if isinstance(row, dict) else None
            if not isinstance(cells, list):
                raise _SampleFailure("schema", f"rows[{position}].cells 必須是 list")
            if len(cells) != len(columns):
                raise _SampleFailure(
                    "row_width",
                    f"rows[{position}] 有 {len(cells)} 格，但表有 {len(columns)} 欄",
                )
    elif kind == figure_extract.KIND_TERMINAL:
        lines = model_obj.get("lines")
        if not isinstance(lines, list):
            raise _SampleFailure("schema", "lines 必須是 list")
        if not lines:
            raise _SampleFailure("empty_payload", "空 payload（lines=0）")
        for position, line in enumerate(lines):
            text_value = line.get("text") if isinstance(line, dict) else None
            if not isinstance(text_value, str):
                raise _SampleFailure("schema", f"lines[{position}].text 必須是 str")
            if "\n" in text_value or "\r" in text_value:
                raise _SampleFailure(
                    "line_contract", f"lines[{position}].text 含 \\n 或 \\r"
                )
    else:
        filled = any(
            model_obj.get(key) for key in ("title", "labels", "components", "relations", "values")
        )
        if not filled:
            raise _SampleFailure("empty_payload", "diagram 的五個欄位全空")

    canonicalize = {
        figure_extract.KIND_TABLE: figure_extract.canonicalize_table,
        figure_extract.KIND_TERMINAL: figure_extract.canonicalize_terminal,
        figure_extract.KIND_DIAGRAM: figure_extract.canonicalize_diagram,
    }[kind]
    try:
        payload = canonicalize(model_obj)
    except figure_extract.FigureValidationError as exc:
        raise _SampleFailure("canonicalize", str(exc)) from exc
    try:
        figure_extract.validate_payload(payload, kind)
    except figure_extract.FigureValidationError as exc:
        raise _SampleFailure("validator", str(exc)) from exc
    return payload


def _call_extractor(*, kind: str, variant, base_url: str, model: str, profile: str,
                    cache_prompt: bool):
    """一次 structured VL 呼叫。

    取樣以單一 `top_k=1` 收斂；**不假設** `temperature=0` 會讓輸出可重現——實際
    greedy 行為依 server 版本與 sampler chain 而定，重複性只由 runtime 的第二次
    取樣實測（見 `evidence["repeatability"]`）。
    """
    return llama_client.vision_json_completion(
        base_url=base_url,
        prompt=_prompt_for(kind, profile),
        image_base64=base64.b64encode(bytes(variant.png)).decode("ascii"),
        mime_type=_variant_mime(variant),
        model=model,
        max_tokens=int(config.VL_INGEST_MAX_TOKENS),
        response_format=figure_extract.response_format_for(kind),
        temperature=0.0, top_p=1.0, top_k=1,
        timeout=int(config.VL_INGEST_TIMEOUT),
        cache_prompt=cache_prompt,
    )


def _extract_variant_payload(*, kind: str, variant, base_url: str, model: str, profile: str,
                             where: str, allow_retry: bool, counters: dict,
                             cache_prompt: bool = True) -> dict:
    """單一 variant 的抽取（含重試）。全部失敗 raise `_SampleFailure`。

    重試一律 `cache_prompt=False`：同一個 prompt 命中 server 的 prompt cache 只會
    把同一份壞輸出重播一次，等於白花一次呼叫。
    """
    attempts = 1 + (max(0, int(config.FIGURE_EXTRACT_RETRIES)) if allow_retry else 0)
    last: _SampleFailure | None = None
    for attempt in range(attempts):
        _check_send_budget(variant, counters, where=where)
        use_cache = cache_prompt and attempt == 0
        try:
            result = _call_extractor(
                kind=kind, variant=variant, base_url=base_url, model=model,
                profile=profile, cache_prompt=use_cache,
            )
        except Exception as exc:  # noqa: BLE001 - 轉成統一的失敗語意
            last = _SampleFailure("transport", _http_error_detail(exc))
            continue
        try:
            return _parse_sample(kind, result)
        except _SampleFailure as exc:
            last = exc
    assert last is not None
    raise last


# ============================================================
# 7. native lane：原生通道
# ============================================================
def _pos_slice(raw_markdown: str, pos) -> tuple[str | None, str]:
    """把 `page_boxes[].pos` 安全地切成 raw substring。

    實測 pymupdf4llm 1.28.0 的 `pos` 是 `(start, stop)`；直接 `raw[pos]` 在 Python
    會 TypeError。這裡明確 unpack 並驗 `0 <= start <= stop <= len(raw)`——越界或
    負值的 offset 切出來的東西會是**另一段內容**，那是最難察覺的錯配。
    """
    if not isinstance(raw_markdown, str):
        return None, "raw_markdown_unavailable"
    if not isinstance(pos, (tuple, list)) or len(pos) != 2:
        return None, "pos_shape"
    start, stop = pos
    if not (isinstance(start, int) and isinstance(stop, int)):
        return None, "pos_not_int"
    if isinstance(start, bool) or isinstance(stop, bool):
        return None, "pos_not_int"
    if not (0 <= start <= stop <= len(raw_markdown)):
        return None, "pos_out_of_range"
    if start == stop:
        return None, "pos_empty"
    return raw_markdown[start:stop], ""


_MD_SEPARATOR_CELL = {"-", ":"}
# 同一張表通常被三個 strategy 都找到；偏好順序＝ T3 的 _TABLE_STRATEGIES。
_TABLE_STRATEGY_RANK = {"lines": 0, "lines_strict": 1, "text": 2}


def _split_markdown_row(line: str) -> list[str]:
    """依未跳脫的 `|` 切一列 markdown，並還原 `\\|` / `\\\\`。"""
    cells: list[str] = []
    buffer: list[str] = []
    escaped = False
    for ch in line:
        if escaped:
            buffer.append(ch if ch in ("|", "\\") else "\\" + ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "|":
            cells.append("".join(buffer))
            buffer = []
        else:
            buffer.append(ch)
    if escaped:
        buffer.append("\\")
    cells.append("".join(buffer))
    if cells and not cells[0].strip():
        cells = cells[1:]
    if cells and not cells[-1].strip():
        cells = cells[:-1]
    return [cell.strip() for cell in cells]


_MD_SPAN_MARKERS = ("```", "``", "`", "***", "**", "*")


def _unwrap_markdown_cell(text: str) -> str:
    """還原 pymupdf4llm 的 markdown 行內標記，取回頁面上真正的字。

    實測（pymupdf4llm 1.28.0）：每個 text span 都被 backtick 包起來，格內換行寫成
    `<br>`——`` `first`<br>`second` ``。把 markup 當成內容會讓這個通道與 word
    geometry 通道**每一格**都衝突，於是整張表被打成 `▯`。
    只剝**整段包裹**的標記；格內出現的單獨 backtick 不動。
    """
    parts = []
    for chunk in text.split("<br>"):
        piece = chunk.strip()
        for marker in _MD_SPAN_MARKERS:
            while (len(piece) > 2 * len(marker) and piece.startswith(marker)
                   and piece.endswith(marker)):
                inner = piece[len(marker):-len(marker)].strip()
                if not inner:
                    break
                piece = inner
        parts.append(piece)
    return "\n".join(parts)


def _parse_markdown_grid(text: str) -> dict | None:
    """markdown 表格 → `{"header": [...], "rows": [[str|None, ...], ...]}`。

    `None` 代表「**這個通道表達不出這一格**」，不是「這一格是空的」。實測
    pymupdf4llm **不會**跳脫 cell 內的 `|`（`a|b` 原樣寫進 markdown），
    所以含 `|` 的列切出來的格數會比表頭多，整列往後位移。那種列一律標成不可用，
    交給 word geometry 通道——只是少一份佐證，絕不會拿位移過的內容當證據。
    """
    lines: list[tuple[list[str], bool]] = []
    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = _split_markdown_row(line)
        if not cells:
            continue
        separator = all(cell and set(cell) <= _MD_SEPARATOR_CELL for cell in cells)
        lines.append(([_unwrap_markdown_cell(cell) for cell in cells], separator))
    if not lines:
        return None

    separators = [i for i, (_cells, is_sep) in enumerate(lines) if is_sep]
    if not separators:
        # 沒有 separator 就分不出 header 與資料。猜「第一列是 header」會在多層
        # 表頭時把第二層表頭寫成第一筆資料（總審 BLOCKER #4），所以這裡 abstain。
        return None
    cut = separators[0]
    header_layers = [cells for cells, is_sep in lines[:cut]]
    if not header_layers:
        return None
    width = len(header_layers[0])
    if width < 1 or any(len(layer) != width for layer in header_layers):
        return None    # 各層表頭寬度不一致 → 無法安全判定，abstain

    # 多層表頭以 `\n` 逐欄攤平；`header_layers` 一併留著，攤平是可逆的。
    header = [
        "\n".join(part for part in (layer[index] for layer in header_layers) if part)
        for index in range(width)
    ]
    rows: list[list] = []
    for cells, is_sep in lines[cut + 1:]:
        if is_sep:
            continue
        rows.append(list(cells) if len(cells) == width else [None] * width)
    return {"header": header, "rows": rows, "header_layers": header_layers}


def _words_for_candidate(words, bbox) -> list[tuple]:
    """把**整頁一次**取好的 words 分派給候選框。

    保留條件：中心點落在適度外擴的框內 **或** 交集面積佔 word 面積 ≥
    `WORD_OVERLAP_MIN`。刻意**不**在上游用 `clip` 截斷——`get_text(clip=...)` 會先
    把邊界字丟掉，事後再怎麼過濾都救不回來。本模組拿到的是 `PageEvidence.words`
    （整頁），結構上就沒有誤用 clip 的可能。
    """
    box = _as_bbox(bbox)
    if box is None:
        return []
    kept: list[tuple] = []
    for word in words or ():
        word_box = _as_bbox(word)
        if word_box is None:
            continue
        if _contains_center(box, word_box, WORD_BBOX_PAD_PT):
            kept.append(tuple(word))
            continue
        inter = _intersection(box, word_box)
        if inter is None:
            continue
        word_area = _area(word_box)
        if word_area <= 0:
            continue
        if _area(inter) / word_area >= WORD_OVERLAP_MIN:
            kept.append(tuple(word))
    return kept


def _group_words_into_rows(words) -> list[list[tuple]]:
    """依垂直重疊把 words 分成視覺列。

    **不用** `(block_no, line_no)` 當分群依據：實測同一個視覺列可能落在不同
    `block_no`（`page.get_text("words")` 的 block 是版面分割，不是「一行」）。
    那兩個欄位只在 evidence 裡當佐證。零高度 / 破損的 word 直接跳過。
    """
    boxes = []
    for word in words:
        box = _as_bbox(word)
        if box is None or box[3] - box[1] <= 0:
            continue
        boxes.append((box, tuple(word)))
    boxes.sort(key=lambda item: ((item[0][1] + item[0][3]) / 2.0, item[0][0]))

    rows: list[list[tuple]] = []
    bands: list[tuple[float, float]] = []
    for box, word in boxes:
        placed = False
        for index, (top, bottom) in enumerate(bands):
            overlap = min(bottom, box[3]) - max(top, box[1])
            smaller = min(bottom - top, box[3] - box[1])
            if smaller > 0 and overlap / smaller >= ROW_BAND_OVERLAP_MIN:
                rows[index].append(word)
                bands[index] = (min(top, box[1]), max(bottom, box[3]))
                placed = True
                break
        if not placed:
            rows.append([word])
            bands.append((box[1], box[3]))
    order = sorted(range(len(rows)), key=lambda i: bands[i][0])
    return [sorted(rows[i], key=lambda w: _as_bbox(w)[0]) for i in order]


def _join_words(words) -> str:
    """把一列 word 串成文字（單一空格）。

    **不會**用來產生 terminal 的 canonical 正文：word 抽取證明不了行首縮排、
    行尾空白與無字空行（契約 §15.1 / 總審 BLOCKER #5），那條路已經改走 VL lane。
    這裡只服務兩種用途：table cell 的文字（格內詞距沒有語義），以及 terminal 的
    **secondary anchor**（比對一律走 `normalize_for_compare`，不當正文）。
    """
    return " ".join(str(word[4]) for word in words if len(word) > 4 and str(word[4]))


def _geometry_cells(candidate) -> list[list] | None:
    native = getattr(candidate, "native_table", None)
    if not isinstance(native, dict):
        return None
    geometry = native.get("geometry")
    if not isinstance(geometry, dict):
        return None
    cells = geometry.get("cells")
    if not isinstance(cells, list) or not cells:
        return None
    if not all(isinstance(row, list) and row for row in cells):
        return None
    return cells


def _grid_from_geometry(cells, words) -> dict | None:
    """用 geometry 的每格 bbox 去撿 word，組成 grid。

    這是實測能還原 `0x4000_0100` 的通道——`find_tables().extract()` 會把它切成
    `'0x4000 0100\\n_'`，所以那條路只能當 evidence，永遠不能當 canonical。
    """
    grid: list[list[str]] = []
    for row in cells:
        # 逐列保留 geometry 的**自然寬度**（不補不砍）：寬度不符會在
        # `_grid_to_table_payload()` fail-loud，而不是靜默錯位。
        line: list[str] = []
        for cell_box in row:
            box = _as_bbox(cell_box)
            if box is None:
                return None
            inside = [
                word for word in words
                if _contains_center(box, _as_bbox(word) or box, CELL_ASSIGN_PAD_PT)
            ]
            inside.sort(key=lambda w: (_as_bbox(w) or box)[0])
            line.append(_join_words(inside))
        grid.append(line)
    if not grid:
        return None
    return {"header": grid[0], "rows": grid[1:]}


def _derive_columns_by_gap(word_rows, n_cols: int):
    """沒有 geometry 時，用 x 投影上最大的 n-1 個間隙推欄界。回傳 boundaries 或 None。"""
    if n_cols < 1:
        return None
    if n_cols == 1:
        return []
    spans: list[tuple[float, float]] = []
    for row in word_rows:
        for word in row:
            box = _as_bbox(word)
            if box is not None:
                spans.append((box[0], box[2]))
    if not spans:
        return None
    spans.sort()
    merged: list[list[float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    if len(merged) < n_cols:
        return None
    gaps = sorted(
        ((merged[i + 1][0] - merged[i][1], i) for i in range(len(merged) - 1)),
        reverse=True,
    )[: n_cols - 1]
    if any(width <= 0 for width, _ in gaps):
        return None
    cuts = sorted(index for _, index in gaps)
    return [(merged[i][1] + merged[i + 1][0]) / 2.0 for i in cuts]


def _grid_from_boundaries(word_rows, boundaries) -> list[list[str]]:
    grid: list[list[str]] = []
    for row in word_rows:
        buckets: list[list[tuple]] = [[] for _ in range(len(boundaries) + 1)]
        for word in row:
            box = _as_bbox(word)
            if box is None:
                continue
            center = (box[0] + box[2]) / 2.0
            index = 0
            while index < len(boundaries) and center > boundaries[index]:
                index += 1
            buckets[index].append(word)
        grid.append([_join_words(sorted(b, key=lambda w: (_as_bbox(w) or (0,))[0])) for b in buckets])
    return grid


def _column_assignment_ok(grid, expected_nonempty) -> bool:
    """欄位切分的**驗證**：不通過就只能做 token-presence，不得宣稱格級佐證。"""
    if not grid or not expected_nonempty:
        return False
    filled = 0
    total = 0
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            if (r, c) in expected_nonempty:
                total += 1
                if cell.strip():
                    filled += 1
    if total == 0:
        return False
    return filled / total >= 0.6


def _match_native_table_entry(evidence, bbox):
    """從 `PageEvidence.tables` 挑出**唯一**對應這個候選的 entry。

    要求最佳 IoU ≥ `config.FIGURE_IOU_MERGE` 且領先次佳 `NATIVE_MATCH_MARGIN`；
    同頁兩張表靠得很近時寧可判為「對不上」，也不要把 B 表的內容當成 A 表的佐證。
    退化（`degenerate`）的 entry 一律不參與。
    """
    box = _as_bbox(bbox)
    tables = getattr(evidence, "tables", None)
    if box is None or not isinstance(tables, dict):
        return None, "no_tables_channel"
    scored: list[tuple[float, str, dict]] = []
    for strategy, entries in tables.items():
        for entry in entries or ():
            if not isinstance(entry, dict) or entry.get("degenerate"):
                continue
            entry_box = _as_bbox(entry.get("bbox"))
            if entry_box is None:
                continue
            scored.append((_iou(box, entry_box), str(strategy), entry))
    if not scored:
        return None, "no_tables_channel"
    scored.sort(key=lambda item: (-item[0], _TABLE_STRATEGY_RANK.get(item[1], 9)))
    best_iou, strategy, entry = scored[0]
    if best_iou < float(config.FIGURE_IOU_MERGE):
        return None, "table_entry_iou_low"
    best_box = _as_bbox(entry.get("bbox"))
    # 三個 strategy 找到**同一張**表不算歧義；真正危險的是同頁另一張表分數接近。
    for rival_iou, _rival_strategy, rival in scored[1:]:
        if best_iou - rival_iou >= NATIVE_MATCH_MARGIN:
            break
        rival_box = _as_bbox(rival.get("bbox"))
        if rival_box is None or best_box is None:
            continue
        if _iou(best_box, rival_box) < float(config.FIGURE_IOU_MERGE):
            return None, "ambiguous_table_match"
    return (strategy, entry), ""


def _table_entry_geometry(entry) -> dict:
    geometry = entry.get("geometry")
    return geometry if isinstance(geometry, dict) else {}


def _entry_grid(entry, *, where: str = "") -> dict | None:
    """`PageEvidence.tables[*]` → grid。

    T3 把 `Table.extract()` 的結果放在 `geometry["extract_raw"]`（不是 entry 頂層），
    而且一律標 `extract_unreliable_underscore`——實測它會把 `0x4000_0100` 讀成
    `'0x4000 0100\n_'`。所以它**只**是 evidence channel，絕不當 canonical。
    頂層的 `rows` / `extract` 是舊形狀，保留讀取以免形狀微調就靜默斷掉。
    """
    geometry = _table_entry_geometry(entry)
    rows = geometry.get("extract_raw")
    if not isinstance(rows, list) or not rows:
        rows = entry.get("rows")
    if not isinstance(rows, list) or not rows:
        rows = entry.get("extract")
    if not isinstance(rows, list) or not rows:
        return None
    cleaned = [
        ["" if cell is None else str(cell) for cell in row]
        for row in rows if isinstance(row, list)
    ]
    if not cleaned:
        return None
    width = len(cleaned[0])
    if width < 1:
        return None
    # 契約 §15.3：**保留原始寬度**，ragged 立即 fail-loud。補空格會憑空造值、
    # 截一格會讓後面所有欄位往前錯位，而把整列改寫成表頭寬度的 `None`（先前的
    # 做法）等於把寬列的內容直接丟掉。
    #
    # 與 `_parse_markdown_grid()` 的 `None` 慣例刻意不同：那邊是**上游已知缺陷**
    # （pymupdf4llm 不跳脫 cell 內的 `|`，整列往後位移），語意是「這個通道表達不出
    # 這一格」；這邊的 ragged 代表 `Table.extract()` 回了非矩形的結果，那是異常，
    # 不是可以退讓的表達能力問題。
    bad = [i for i, row in enumerate(cleaned[1:], 1) if len(row) != width]
    if bad:
        raise figure_extract.FigureExtractionError(
            f"{where}: find_tables 的 extract 結果不是矩形——第 {bad[:5]} 列寬度與表頭的 "
            f"{width} 欄不符。不補不砍，寬度不對就是不合格"
        )
    return {"header": cleaned[0], "rows": [list(row) for row in cleaned[1:]]}


# ============================================================
# 8. 格/行級 verifier
# ============================================================
_MAX_ALIGN_CELLS = 250_000


def _similarity(left: str, right: str) -> float:
    import difflib  # noqa: PLC0415 - 只有對齊時才需要
    a = figure_extract.normalize_for_compare(left)
    b = figure_extract.normalize_for_compare(right)
    if a == b:
        return 1.0
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _align_sequences(left: Sequence[str], right: Sequence[str], *, min_sim: float = 0.5):
    """單調序列對齊（Needleman-Wunsch）。回傳 `(pairs, strategy)`。

    先試「數量相同就直接對」；數量不同才做 DP。DP 規模過大時直接放棄並回報
    `too_large`，不做任何猜測性的截斷對齊。
    """
    if len(left) == len(right):
        return [(i, i) for i in range(len(left))], "count"
    if not left or not right:
        return [], "empty"
    if len(left) * len(right) > _MAX_ALIGN_CELLS:
        return [], "too_large"

    n, m, gap = len(left), len(right), -0.6
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    back = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] + gap
        back[i][0] = 1
    for j in range(1, m + 1):
        dp[0][j] = dp[0][j - 1] + gap
        back[0][j] = 2
    sims = [[0.0] * m for _ in range(n)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            score = _similarity(left[i - 1], right[j - 1])
            sims[i - 1][j - 1] = score
            diag = dp[i - 1][j - 1] + (score if score >= min_sim else -0.4)
            up = dp[i - 1][j] + gap
            side = dp[i][j - 1] + gap
            best = max(diag, up, side)
            dp[i][j] = best
            back[i][j] = 0 if best == diag else (1 if best == up else 2)

    pairs: list[tuple[int, int]] = []
    i, j = n, m
    while i > 0 or j > 0:
        move = back[i][j]
        if i > 0 and j > 0 and move == 0:
            if sims[i - 1][j - 1] >= min_sim:
                pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif i > 0 and (j == 0 or move == 1):
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return pairs, "dp"


def _is_punct_token(token: str) -> bool:
    return len(token) == 1 and not token.isalnum() and not token.isspace()


def _content_tokens(text: str, kind: str) -> list[str]:
    return [t for t in figure_extract.critical_tokens(text, kind) if not _is_punct_token(t)]


def _looks_critical(token: str) -> bool:
    return any(ch.isdigit() for ch in token) or token.lower().startswith("0x") or ":" in token


def _empty_alignment(reason_slug: str, detail: str, *, atoms_total: int, key: str) -> dict:
    return {
        "channels": [],
        key: {},
        "unlocatable_tokens": [],
        "anchor_coverage": {"atoms_total": atoms_total, "atoms_anchorable": 0,
                            "atoms_matched": 0, "ratio": 0.0},
        "blockers": [],
        "notes": [[reason_slug, detail]],
    }


_STRIP_UNDERSCORE_WS = re.compile(r"[\s_\u3000\u00a0]+")


def _underscore_artifact(canonical: str, extracted: str) -> bool:
    """差異是不是**那個已知的** `Table.extract()` 底線缺陷。

    實測（pymupdf 1.28.0）：`0x4000_0100` 會被 `extract()` 讀成 `'0x4000 0100\n_'`
    ——底線被抽成獨立的一段。這個缺陷有明確前提（原文含 `_`）與明確範圍（差異
    只落在底線與空白的位置），所以可以精準辨識，而不是「對不上就放行」。

    只有標了 `extract_unreliable_underscore` 的通道會走這條；而且**只中和它的
    否決權**，那一格仍然要靠其他通道佐證，差異也照樣寫進 evidence。
    值真的不同（`0x4000_0100` vs `0x4000_0101`）時，去掉底線與空白後仍然不等，
    照樣是衝突。
    """
    if "_" not in canonical:
        return False
    if canonical == extracted:
        return False
    return (_STRIP_UNDERSCORE_WS.sub("", canonical)
            == _STRIP_UNDERSCORE_WS.sub("", extracted))


def _merge_verdict(record: dict, channel: str, verdict: str, spans, anchor_text: str,
                   payload_text: str, kind: str, *, unreliable: bool = False) -> None:
    """把一個 channel 對某原子的判定併進該原子的紀錄。

    合併方向只有一個：**任何一個可靠通道說矛盾，就是矛盾**。「A 通道對得上所以
    算過關」會讓 B 通道發現的字元差異被靜默吞掉。
    """
    record.setdefault("anchor", channel)
    record.setdefault("channels", [])
    if channel not in record["channels"]:
        record["channels"].append(channel)
    # 每個 channel 的**獨立判定**：狀態升級只能靠正面的 match，不能靠「沒看到
    # blocker」推論。缺了這一份，unreliable 通道的 artifact 也會讓該格看起來
    # 「被非 canonical 通道佐證過」。
    by_channel = record.setdefault("by_channel", {})
    record["raw"] = anchor_text
    record["payload"] = payload_text
    if verdict == "match":
        by_channel[channel] = {
            "verdict": "match", "reliable": not unreliable,
            "critical_ok": (figure_extract.critical_tokens(payload_text, kind)
                            == figure_extract.critical_tokens(anchor_text, kind)),
        }
        if record.get("matched") is None:
            record["matched"] = True
        record.setdefault("critical_ok", True)
        return
    if unreliable and _underscore_artifact(payload_text, anchor_text):
        by_channel[channel] = {"verdict": "artifact", "reliable": False, "critical_ok": None}
        record.setdefault("artifacts", []).append(
            {"channel": channel, "raw": anchor_text, "reason": "extract_underscore_artifact"}
        )
        if record.get("matched") is None:
            record["matched"] = None
        record.setdefault("critical_ok", None)
        return
    record["matched"] = False
    record["anchor"] = channel
    by_channel[channel] = {"verdict": verdict, "reliable": not unreliable, "critical_ok": False}
    if verdict == "glyph":
        record.setdefault("mask_spans", [])
        existing = {(s, e) for s, e, _ in record["mask_spans"]}
        for start, end, alts in spans:
            if (start, end) not in existing:
                record["mask_spans"].append((start, end, alts))
        record["verdict"] = "glyph"
    elif verdict == "unmappable":
        record["verdict"] = "unmappable"
    else:
        record["verdict"] = "structural"
        record["alternatives"] = [payload_text, anchor_text]
    record["critical_ok"] = (
        figure_extract.critical_tokens(payload_text, kind)
        == figure_extract.critical_tokens(anchor_text, kind)
    )


def _table_channels(n_cols: int, candidate, evidence, findings_notes: list):
    """收集 table 的原生 anchor 通道，primary（markdown pos）在前。

    `n_cols <= 0` 表示「還沒有 payload」（native lane 建 canonical 之前），
    此時欄界只能從 word band 的眾數推。
    """
    channels: list[tuple[str, dict]] = []
    unreliable: set[str] = set()
    where = (f"page={getattr(candidate, 'page', '?')} "
             f"figure={getattr(candidate, 'figure_id', '?')}")
    native = getattr(candidate, "native_table", None)
    raw_markdown = getattr(evidence, "raw_markdown", "")
    if isinstance(native, dict):
        text, problem = _pos_slice(raw_markdown, native.get("pos"))
        if text is not None:
            # plan 與 PageEvidence 必須是同一次 ingest 的產物。offset 合法**不代表**
            # 切到的是同一份內容——混用兩次 ingest 的 plan/evidence 時，`pos` 會切出
            # 另一張表，而 figure 身分（figure_id / bbox / occurrences）仍是舊的，
            # 於是產出一份看起來完全合法的錯配 canonical payload。
            # terminal 路徑早就有這道核對（`signals["native_text"]`），table 補齊。
            declared = native.get("markdown")
            if isinstance(declared, str) and declared != text:
                raise figure_extract.FigureExtractionError(
                    f"page={getattr(candidate, 'page', '?')} "
                    f"figure={getattr(candidate, 'figure_id', '?')}: "
                    f"native_table['markdown'] 與 raw_markdown[{native.get('pos')}] 不一致"
                    "（plan 與 PageEvidence 不同步），不得挑一份當 canonical"
                )
            grid = _parse_markdown_grid(text)
            if grid is not None:
                channels.append(("markdown_pos", grid))
            else:
                findings_notes.append(["markdown_pos_not_a_table", "pos 切片不是 markdown 表格"])
        elif problem:
            findings_notes.append(["markdown_pos_unavailable", f"pos 不可用：{problem}"])

    words = _words_for_candidate(getattr(evidence, "words", None), getattr(candidate, "bbox", None))
    geometry_cells = _geometry_cells(candidate)
    if words and geometry_cells:
        grid = _grid_from_geometry(geometry_cells, words)
        if grid is not None:
            channels.append(("words_geometry", grid))
    elif words:
        rows = _group_words_into_rows(words)
        width = n_cols
        if width <= 0 and rows:
            counts = [len(row) for row in rows]
            width = max(set(counts), key=counts.count)
        boundaries = _derive_columns_by_gap(rows, width)
        if boundaries is not None:
            grid_rows = _grid_from_boundaries(rows, boundaries)
            expected = {
                (r, c)
                for r, row in enumerate(grid_rows)
                for c in range(len(row))
            }
            if _column_assignment_ok(grid_rows, expected):
                channels.append(("words_columns", {"header": grid_rows[0], "rows": grid_rows[1:]}))
            else:
                findings_notes.append(
                    ["column_assignment_unverified", "欄位切分未通過驗證，只能做 token-presence"]
                )
        else:
            findings_notes.append(
                ["column_assignment_unverified", "無 geometry 也推不出欄界，只能做 token-presence"]
            )

    entry_match, problem = _match_native_table_entry(evidence, getattr(candidate, "bbox", None))
    if entry_match is not None:
        strategy, entry = entry_match
        grid = _entry_grid(entry, where=where)
        if grid is not None:
            name = f"find_tables:{strategy}"
            if (entry.get("extract_unreliable_underscore")
                    or _table_entry_geometry(entry).get("extract_unreliable_underscore")):
                unreliable.add(name)
                findings_notes.append(
                    [f"{name}_extract_unreliable",
                     "find_tables().extract() 已知會把 0x4000_0100 切成 '0x4000 0100\\n_'，"
                     "只當 evidence，永不 canonical"]
                )
            channels.append((name, grid))
    elif problem and problem != "no_tables_channel":
        # 「這一頁根本沒有 find_tables 通道」不是異常，不值得污染 reasons[]
        # （reasons 會進 KB 與 REF 顯示）；對不上或分不清才要記。
        findings_notes.append(["native_table_entry_unavailable", problem])
    return channels, words, unreliable


def align_table_cells(payload: dict, candidate: Candidate,
                      evidence: PageEvidence) -> dict:
    """table 的格級對齊（**純函式**，不改 payload）。

    anchoring 是 evidence channel，不是普遍真理：掃描出來的文字層可能亂序、重複，
    甚至本身就是錯的。所以 primary anchor 是 `page_boxes[].pos` 對應的 raw
    substring，secondary 才是 word geometry；兩者矛盾時**兩份都留**，不宣稱哪一邊
    贏。回傳的 dict 帶 `mask_spans` / `blockers` / `notes`，由呼叫端施加。
    """
    notes: list[list[str]] = []
    blockers: list[list[str]] = []
    rows = payload["rows"]
    columns = payload["columns"]
    atoms_total = len(rows) * len(columns)
    channels, words, unreliable = _table_channels(len(columns), candidate, evidence, notes)

    if not channels:
        result = _empty_alignment(
            "no_anchor_evidence", "這個候選沒有任何可用的原生 anchor 通道",
            atoms_total=atoms_total, key="cells",
        )
        result["notes"].extend(notes)
        return result

    payload_rows = [
        " | ".join(cell["text"] for cell in row["cells"]) for row in rows
    ]
    cells: dict[str, dict] = {}
    used_channels: list[str] = []
    paired_payload_rows: set[int] = set()
    row_alignment: dict[str, Any] = {}

    for name, grid in channels:
        anchor_rows = grid["rows"]
        anchor_keys = [" | ".join("" if c is None else c for c in row) for row in anchor_rows]
        pairs, strategy = _align_sequences(payload_rows, anchor_keys)
        row_alignment[name] = {
            "strategy": strategy, "pairs": len(pairs),
            "payload_rows": len(payload_rows), "anchor_rows": len(anchor_keys),
        }
        if not pairs:
            blockers.append([
                "row_alignment_failed",
                f"{name}: {len(payload_rows)} 列 payload 對不上 {len(anchor_keys)} 列 anchor"
                f"（strategy={strategy}）",
            ])
            continue
        coverage = len(pairs) / max(1, len(payload_rows))
        if coverage < 0.6:
            blockers.append([
                "row_alignment_failed",
                f"{name}: 列對齊覆蓋率只有 {coverage:.0%}",
            ])
            continue
        matched_anchor = {j for _, j in pairs}
        if len(matched_anchor) < len(anchor_keys):
            blockers.append([
                "missing_rows",
                f"{name}: anchor 有 {len(anchor_keys)} 列，只有 {len(matched_anchor)} 列在 payload"
                "裡找得到對應——漏列的內容不會出現在任何 chunk",
            ])
        if len(pairs) < len(payload_rows):
            blockers.append([
                "unanchored_rows",
                f"{name}: payload 有 {len(payload_rows)} 列，其中 "
                f"{len(payload_rows) - len(pairs)} 列在 anchor 裡找不到對應",
            ])
        paired_payload_rows.update(pi for pi, _ai in pairs)
        used_channels.append(name)

        for pi, ai in pairs:
            anchor_row = anchor_rows[ai]
            if len(anchor_row) != len(columns):
                blockers.append([
                    "cell_conflict",
                    f"{name}: 第 {rows[pi]['row_index']} 列 anchor 有 {len(anchor_row)} 格，"
                    f"payload 有 {len(columns)} 格",
                ])
                continue
            for ci, column in enumerate(columns):
                cell = rows[pi]["cells"][ci]
                if cell["state"] == figure_extract.CELL_STATE_INHERITED:
                    # fill-down 的格是 geometry 證明出來的，不是「在那一列被看到」的。
                    # markdown 通道把 rowspan 畫成空格，拿它去比一定衝突，那會把
                    # 正確的 fill-down 反過來打成 conflict。
                    continue
                anchor_text = anchor_row[ci]
                if anchor_text is None:
                    continue    # 這個通道表達不出這一格（見 _parse_markdown_grid）
                key = f"r{rows[pi]['row_index']}{column['column_id']}"
                payload_text = cell["text"]
                verdict, spans = _compare_atom(payload_text, anchor_text)
                record = cells.setdefault(key, {"anchor": None, "matched": None})
                _merge_verdict(record, name, verdict, spans, anchor_text, payload_text,
                               figure_extract.KIND_TABLE, unreliable=name in unreliable)

    # 有通道成功對齊、卻**完全沒被配對**的 payload 列＝anchor 說那幾列不在這張表裡。
    # 只加 blocker、正文照留，等於讓一份看起來合理的 register 列繼續被 general
    # query 顯示（local #7）。逐格遮罩並把原文留進 alternatives。
    if used_channels:
        for pi, row in enumerate(rows):
            if pi in paired_payload_rows:
                continue
            for cell in row["cells"]:
                key = f"r{row['row_index']}{cell['column_id']}"
                record = cells.setdefault(key, {"anchor": None, "matched": None})
                record.update({
                    "anchor": "unanchored", "matched": False, "verdict": "structural",
                    "payload": cell["text"], "raw": "",
                    "alternatives": [cell["text"]], "critical_ok": False,
                    "channels": _ordered_unique([*record.get("channels", []), "unanchored"]),
                })

    for key, record in cells.items():
        if record.get("matched") is False:
            verdict = record.get("verdict")
            if verdict == "glyph":
                blockers.append(["glyph_conflict", f"{key}: anchor 與 payload 有字元級差異"])
            elif verdict == "unmappable":
                blockers.append(["cell_conflict", f"{key}: 空白正規化對映失效，無法定位差異"])
            else:
                blockers.append(["cell_conflict", f"{key}: anchor 與 payload 結構性不同"])
        if record.get("critical_ok") is False:
            blockers.append(["critical_token_mismatch", f"{key}: critical token 集合不一致"])

    anchorable = sum(1 for record in cells.values() if record.get("matched") is not None)
    matched = sum(1 for record in cells.values() if record.get("matched") is True)

    unlocatable: list[str] = []
    if not cells and channels:
        # 有 anchor 通道、卻連一格都做不出格級證據（列對齊全滅）→ 只剩 token-presence。
        # 這條路徑**只**寫 evidence sidecar 與 status reason：把對不到的 token 補成
        # 「補遺區段」會再一次拆散名稱／bit range／屬性／說明的配對。
        haystack = " ".join(
            figure_extract.normalize_for_compare(cell["text"])
            for row in rows for cell in row["cells"]
        )
        for name, grid in channels:
            # markdown 通道的 ragged 列是 `None`（表達不出）——join 前一律過濾，
            # 否則這裡會拋未包裝的 TypeError，繞過 `.results` / `.failed` 失敗契約。
            def _flat(cells):
                return " ".join(cell for cell in cells if isinstance(cell, str))

            source = " ".join([_flat(grid["header"]), *(_flat(row) for row in grid["rows"])])
            for token in _content_tokens(source, figure_extract.KIND_TABLE):
                if figure_extract.normalize_for_compare(token) not in haystack:
                    unlocatable.append(token)
        unlocatable = _ordered_unique(unlocatable)
        for token in unlocatable:
            slug = "unlocated_critical_token" if _looks_critical(token) else "unlocated_token"
            blockers.append([
                slug,
                f"文字層有 {token!r} 但對不到任何 cell（只寫 evidence sidecar，"
                "不得補成「補遺區段」）",
            ])

    return {
        "channels": [name for name, _ in channels],
        "cells": cells,
        "unlocatable_tokens": unlocatable,
        "anchor_coverage": {
            "atoms_total": atoms_total, "atoms_anchorable": anchorable,
            "atoms_matched": matched,
            "ratio": (matched / atoms_total) if atoms_total else 0.0,
        },
        "row_alignment": row_alignment,
        "blockers": blockers,
        "notes": notes,
    }


def align_terminal_lines(payload: dict, candidate: Candidate,
                         evidence: PageEvidence) -> dict:
    """terminal 的行級對齊（**純函式**，不改 payload）。

    primary anchor 一樣是 `pos` 對應的 raw substring——它保留空行，所以首行、
    中間、末行的空行才有機會被佐證；word baseline 只能當 secondary（它產生不出
    空行，也還原不了 tab 與多個 space）。**空行照樣計入分母**：沒有佐證的空行
    就是沒有佐證，不能因為「它是空的」而讓整張圖白拿 `corroborated`。
    """
    notes: list[list[str]] = []
    blockers: list[list[str]] = []
    lines = payload["lines"]
    atoms_total = len(lines)

    channels: list[tuple[str, list[str]]] = []
    raw_markdown = getattr(evidence, "raw_markdown", "")
    signal = _native_text_signal(candidate)
    pos = signal["pos"] if signal else None
    if pos is None:
        native = getattr(candidate, "native_table", None)
        if isinstance(native, dict):
            pos = native.get("pos")
    if pos is None:
        pos = _page_box_pos_for(evidence, getattr(candidate, "bbox", None))
    if pos is not None:
        text, problem = _pos_slice(raw_markdown, pos)
        if text is not None:
            channels.append(("markdown_pos", text.split("\n")))
        elif problem:
            notes.append(["markdown_pos_unavailable", f"pos 不可用：{problem}"])
    else:
        notes.append(["markdown_pos_unavailable", "沒有覆蓋這個候選的 page_box pos"])

    words = _words_for_candidate(getattr(evidence, "words", None), getattr(candidate, "bbox", None))
    if words:
        rows = _group_words_into_rows(words)
        channels.append(("words_geometry", [_join_words(row) for row in rows]))
        notes.append([
            "spacing_not_provable",
            "word baseline 還原不了 tab 與多個 space，比對一律走 normalize_for_compare",
        ])

    if not channels:
        result = _empty_alignment(
            "no_anchor_evidence", "這個候選沒有任何可用的原生 anchor 通道",
            atoms_total=atoms_total, key="lines",
        )
        result["notes"].extend(notes)
        return result

    payload_texts = [line["text"] for line in lines]
    records: dict[str, dict] = {}
    used_channels: list[str] = []
    paired_payload_lines: set[int] = set()
    line_alignment: dict[str, Any] = {}

    for name, anchor_texts in channels:
        # word baseline 產生不出空行——那是通道的表達能力限制，不是內容衝突。
        # 所以空行不進**這個通道**的分母（否則每一份含空行的 log 都會因為
        # 次要通道而被打成 needs_review），但它們仍然計入全域 atoms_total，
        # 因此永遠拿不到 anchor，也就永遠擋得住 corroborated（審核 #1）。
        subset = (
            list(range(len(payload_texts))) if name == "markdown_pos"
            else [i for i, text in enumerate(payload_texts) if text.strip()]
        )
        subset_texts = [payload_texts[i] for i in subset]
        pairs, strategy = _align_sequences(subset_texts, anchor_texts)
        line_alignment[name] = {
            "strategy": strategy, "pairs": len(pairs),
            "payload_lines": len(subset_texts), "anchor_lines": len(anchor_texts),
            "empty_lines_excluded": len(payload_texts) - len(subset_texts),
        }
        if not subset_texts:
            notes.append([f"{name}_no_comparable_lines", "這個通道沒有可比對的行"])
            continue
        if not pairs or len(pairs) / len(subset_texts) < 0.6:
            blockers.append([
                "line_alignment_failed",
                f"{name}: {len(subset_texts)} 行 payload 對不上 {len(anchor_texts)} 行 anchor"
                f"（strategy={strategy}）；兩份 raw candidate 都保留在 evidence",
            ])
            line_alignment[name]["anchor_raw"] = anchor_texts[:200]
            line_alignment[name]["payload_raw"] = payload_texts[:200]
            continue
        matched_anchor = {j for _, j in pairs}
        if len(matched_anchor) < len(anchor_texts):
            blockers.append([
                "missing_lines",
                f"{name}: anchor 有 {len(anchor_texts)} 行，只有 {len(matched_anchor)} 行"
                "在 payload 裡找得到對應",
            ])
        if len(pairs) < len(subset_texts):
            blockers.append([
                "unanchored_lines",
                f"{name}: payload 有 {len(subset_texts)} 行可比對，其中 "
                f"{len(subset_texts) - len(pairs)} 行在 anchor 裡找不到對應",
            ])
        paired_payload_lines.update(subset[si] for si, _ai in pairs)
        used_channels.append(name)
        for si, ai in pairs:
            pi = subset[si]
            key = str(lines[pi]["line_index"])
            verdict, spans = _compare_atom(payload_texts[pi], anchor_texts[ai])
            record = records.setdefault(key, {"anchor": None, "matched": None})
            _merge_verdict(record, name, verdict, spans, anchor_texts[ai], payload_texts[pi],
                           figure_extract.KIND_TERMINAL)

    # 同 table：有通道對齊成功、卻沒被配對的**非空**行要遮罩（空行本來就對不到
    # word 通道，已在上面排除在該通道的分母之外，不算被 anchor 否定）。
    if used_channels:
        for pi, line in enumerate(lines):
            if pi in paired_payload_lines or not payload_texts[pi].strip():
                continue
            key = str(line["line_index"])
            record = records.setdefault(key, {"anchor": None, "matched": None})
            record.update({
                "anchor": "unanchored", "matched": False, "verdict": "structural",
                "payload": payload_texts[pi], "raw": "",
                "alternatives": [payload_texts[pi]], "critical_ok": False,
                "channels": _ordered_unique([*record.get("channels", []), "unanchored"]),
            })

    for key, record in records.items():
        if record.get("matched") is False:
            verdict = record.get("verdict")
            if verdict == "glyph":
                blockers.append(["glyph_conflict", f"line {key}: anchor 與 payload 有字元級差異"])
            elif verdict == "unmappable":
                blockers.append(["line_conflict", f"line {key}: 空白正規化對映失效"])
            else:
                blockers.append(["line_conflict", f"line {key}: anchor 與 payload 結構性不同"])
        if record.get("critical_ok") is False:
            blockers.append(["critical_token_mismatch", f"line {key}: critical token 集合不一致"])

    anchorable = sum(1 for record in records.values() if record.get("matched") is not None)
    matched = sum(1 for record in records.values() if record.get("matched") is True)
    return {
        "channels": [name for name, _ in channels],
        "lines": records,
        "unlocatable_tokens": [],
        "anchor_coverage": {
            "atoms_total": atoms_total, "atoms_anchorable": anchorable,
            "atoms_matched": matched,
            "ratio": (matched / atoms_total) if atoms_total else 0.0,
        },
        "line_alignment": line_alignment,
        "blockers": blockers,
        "notes": notes,
    }


def _native_text_signal(candidate) -> dict | None:
    """T3 在 `signals["native_text"]` 放的「`pos` 支撐的 raw markdown」。

    契約 §15.1：terminal 的 native lane 由它決定，所以 canonical 正文也直接取它，
    不再由 verifier 自己反查 page_boxes——planner 用的是校準過的 unrotated bbox，
    verifier 若用上游 display-space bbox 反查，旋轉/裁切頁上兩者會對不起來，
    結果就是「planner 說 native、verifier 找不到 pos」的整份零寫入。
    """
    signals = getattr(candidate, "signals", None)
    if not isinstance(signals, dict):
        return None
    native_text = signals.get("native_text")
    if isinstance(native_text, dict) and native_text.get("pos") is not None:
        return native_text
    return None


def _page_box_pos_for(evidence, bbox):
    """找覆蓋這個候選的 `page_boxes` entry 的 `pos`（terminal 的 secondary 退路）。

    優先讀 T3 校準過的 `_bbox_unrotated` / `_pos`，退回上游原始的 `bbox` / `pos`。
    上游 `bbox` 是 **display 空間**，而候選框是 unrotated 空間——旋轉頁上兩者對不
    起來。T3 目前對旋轉頁一律 abstain（那時上游 markdown 本來就空掉或錯亂），
    所以這條路現在踩不到；讀校準座標是為了讓這個前提哪天放寬時不會再分岔。

    canonical 正文**不走這裡**：它取 `signals["native_text"]`（契約 §15.1）。
    """
    box = _as_bbox(bbox)
    boxes = getattr(evidence, "page_boxes", None)
    if box is None or not isinstance(boxes, list):
        return None
    best = None
    best_iou = 0.0
    for entry in boxes:
        if not isinstance(entry, dict):
            continue
        pos = entry.get("_pos") if entry.get("_pos") is not None else entry.get("pos")
        if pos is None:
            continue
        raw_box = entry.get("_bbox_unrotated")
        entry_box = _as_bbox(raw_box if raw_box is not None else entry.get("bbox"))
        if entry_box is None:
            continue
        score = _iou(box, entry_box)
        if score > best_iou:
            best_iou, best = score, pos
    return best if best_iou >= float(config.FIGURE_IOU_MERGE) else None


# ============================================================
# 9. 遮罩（把對齊/取樣的判定施加到 payload）
# ============================================================
def _merge_uncertain_spans(text: str, existing: list[dict], new_spans) -> list[dict]:
    """把新的不確定區間併進既有的，保證不重疊、且區間內全是 `▯`。

    **相鄰（`start2 == end1`）一律保持分離**：把它們併成一個區間、alternatives 做
    集合聯集，會產生語義錯誤的候選——相鄰兩格分別候選 `A` 與 `B`/`C`，聯集後的
    `["A","B","C"]` 對那個兩字元區間來說沒有一個是完整候選（local #9）。

    **真正重疊**時無法從兩組候選還原該區間的完整候選，所以合併後的 alternatives
    只放等長的 `▯`（誠實表示「沒有可用候選」）；原始的兩組候選仍完整留在
    `evidence[...]["mask_spans"]` 與 `alternatives` 裡。
    """
    items = [(s["start"], s["end"], list(s["alternatives"])) for s in existing]
    items.extend((start, end, list(alts)) for start, end, alts in new_spans)
    items.sort()
    merged: list[tuple[int, int, list[str], bool]] = []
    for start, end, alts in items:
        if merged and start < merged[-1][1]:          # 嚴格重疊才合併；相鄰不合併
            prev_start, prev_end, prev_alts, _lost = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end), prev_alts, True)
        else:
            merged.append((start, end, list(alts), False))
    out = []
    for start, end, alts, lost in merged:
        if not (0 <= start < end <= len(text)):
            continue
        if lost or not alts:
            alts = [figure_extract.UNREADABLE_GLYPH * (end - start)]
        out.append({"start": start, "end": end, "alternatives": alts})
    return out


def _apply_cell_verdict(cell: dict, record: dict) -> None:
    """把一格的判定寫進 canonical payload。

    契約 §10-C：cell 放了 `▯` 就必須是 `unreadable` / `conflict`，validator 會擋。
    結構性衝突時**不留任何一份候選文字**——留下其中一份就是「擇一」，而擇一正是
    北極星禁止的事；正文換成一個 `▯`，兩份原文只進 evidence 的 alternatives。
    """
    verdict = record.get("verdict")
    if verdict == "glyph" and record.get("mask_spans"):
        cell["text"] = _mask_text(cell["text"], record["mask_spans"])
        cell["state"] = figure_extract.CELL_STATE_CONFLICT
        record["alternatives"] = [alts for _s, _e, alts in record["mask_spans"]]
    else:
        alternatives = record.get("alternatives") or [
            record.get("payload", ""), record.get("raw", "")
        ]
        cell["text"] = figure_extract.UNREADABLE_GLYPH
        cell["state"] = figure_extract.CELL_STATE_CONFLICT
        record["alternatives"] = list(alternatives)
    cell["inherited_from_row"] = None


def _apply_line_verdict(line: dict, record: dict) -> None:
    verdict = record.get("verdict")
    if verdict == "glyph" and record.get("mask_spans"):
        line["text"] = _mask_text(line["text"], record["mask_spans"])
        line["uncertain_spans"] = _merge_uncertain_spans(
            line["text"], line["uncertain_spans"], record["mask_spans"]
        )
        record["alternatives"] = [alts for _s, _e, alts in record["mask_spans"]]
    else:
        alternatives = record.get("alternatives") or [
            record.get("payload", ""), record.get("raw", "")
        ]
        text, spans = _all_glyph_placeholder(alternatives)
        line["text"] = text
        line["uncertain_spans"] = spans
        record["alternatives"] = list(alternatives)


def _apply_alignment(payload: dict, kind: str, alignment: dict) -> None:
    if kind == figure_extract.KIND_TABLE:
        records = alignment.get("cells", {})
        for row in payload["rows"]:
            for cell in row["cells"]:
                record = records.get(f"r{row['row_index']}{cell['column_id']}")
                if record is not None and record.get("matched") is False:
                    _apply_cell_verdict(cell, record)
    elif kind == figure_extract.KIND_TERMINAL:
        records = alignment.get("lines", {})
        for line in payload["lines"]:
            record = records.get(str(line["line_index"]))
            if record is not None and record.get("matched") is False:
                _apply_line_verdict(line, record)


# ============================================================
# 10. disagreement detection（無 anchor 時唯一的驗證手段）
# ============================================================
def _raw_diff_spans(left: str, right: str):
    """同長度字串的逐字元差異 → payload 座標的 mask span。長度不同回 None。"""
    if len(left) != len(right):
        return None
    diffs = [
        (i, i + 1, [left[i], right[i]])
        for i, (a, b) in enumerate(zip(left, right)) if a != b
    ]
    if not diffs:
        return []
    return _merge_raw_spans(diffs)


def detect_disagreement(payload_a: dict, payload_b: dict, kind: str) -> tuple[dict, list[str]]:
    """兩次獨立取樣的比對（**純函式**）。回傳 `(evidence, reasons)`。

    比的是**完整 canonical payload**：欄位 label、footnotes、cell state、
    `uncertain_spans` 全都算。只比欄數與 cell 文字會讓「其實不同的兩次輸出」被
    判成一致，而一致正是唯一能拿到 `unverified`（而非 `needs_review`）的理由。

    一律不擇一：可定位的字元差異 → 該字元 `▯` + alternatives；結構/表頭/註腳
    衝突 → 沒有安全的 canonical 結構，交給呼叫端 hard fail。
    """
    reasons: list[str] = []
    evidence: dict[str, Any] = {"agreement": True, "samples": 2}

    if kind == figure_extract.KIND_DIAGRAM:
        if payload_a != payload_b:
            evidence["agreement"] = False
            evidence["structural"] = True
            reasons.append("sample_conflict")
        return evidence, reasons

    if kind == figure_extract.KIND_TABLE:
        labels_a = [c["label"] for c in payload_a["columns"]]
        labels_b = [c["label"] for c in payload_b["columns"]]
        if len(labels_a) != len(labels_b) or len(payload_a["rows"]) != len(payload_b["rows"]):
            evidence.update({
                "agreement": False, "structural": True,
                "shape": {"a": [len(labels_a), len(payload_a["rows"])],
                          "b": [len(labels_b), len(payload_b["rows"])]},
                "candidate_a": copy.deepcopy(payload_a),
                "candidate_b": copy.deepcopy(payload_b),
            })
            return evidence, ["sample_shape_mismatch"]
        if labels_a != labels_b:
            evidence.update({"agreement": False, "structural": True,
                             "header": {"a": labels_a, "b": labels_b},
                             "candidate_a": copy.deepcopy(payload_a),
                             "candidate_b": copy.deepcopy(payload_b)})
            return evidence, ["sample_header_conflict"]
        if payload_a["footnotes"] != payload_b["footnotes"]:
            evidence.update({"agreement": False, "structural": True,
                             "footnotes": {"a": payload_a["footnotes"],
                                           "b": payload_b["footnotes"]},
                             "candidate_a": copy.deepcopy(payload_a),
                             "candidate_b": copy.deepcopy(payload_b)})
            return evidence, ["sample_footnote_conflict"]

        cells: dict[str, dict] = {}
        for row_a, row_b in zip(payload_a["rows"], payload_b["rows"]):
            for cell_a, cell_b in zip(row_a["cells"], row_b["cells"]):
                key = f"r{row_a['row_index']}{cell_a['column_id']}"
                if cell_a["state"] != cell_b["state"]:
                    cells[key] = {"agree": False, "verdict": "structural",
                                  "alternatives": [cell_a["text"], cell_b["text"]],
                                  "states": [cell_a["state"], cell_b["state"]]}
                    reasons.append("sample_state_conflict")
                    continue
                if cell_a["text"] == cell_b["text"]:
                    continue
                spans = _raw_diff_spans(cell_a["text"], cell_b["text"])
                if spans:
                    cells[key] = {"agree": False, "verdict": "glyph", "mask_spans": spans}
                    reasons.append("glyph_conflict")
                else:
                    cells[key] = {"agree": False, "verdict": "structural",
                                  "alternatives": [cell_a["text"], cell_b["text"]]}
                    reasons.append("sample_conflict")
        if cells:
            evidence["agreement"] = False
            evidence["cells"] = cells
        return evidence, _ordered_unique(reasons)

    lines_a, lines_b = payload_a["lines"], payload_b["lines"]
    if len(lines_a) != len(lines_b):
        evidence.update({"agreement": False, "structural": True,
                         "shape": {"a": len(lines_a), "b": len(lines_b)},
                         "candidate_a": copy.deepcopy(payload_a),
                         "candidate_b": copy.deepcopy(payload_b)})
        return evidence, ["sample_shape_mismatch"]
    records: dict[str, dict] = {}
    for line_a, line_b in zip(lines_a, lines_b):
        key = str(line_a["line_index"])
        if line_a["uncertain_spans"] != line_b["uncertain_spans"] and \
                line_a["text"] == line_b["text"]:
            records[key] = {"agree": False, "verdict": "structural",
                            "alternatives": [line_a["text"], line_b["text"]],
                            "spans": [line_a["uncertain_spans"], line_b["uncertain_spans"]]}
            reasons.append("sample_span_conflict")
            continue
        if line_a["text"] == line_b["text"]:
            continue
        spans = _raw_diff_spans(line_a["text"], line_b["text"])
        if spans:
            records[key] = {"agree": False, "verdict": "glyph", "mask_spans": spans}
            reasons.append("glyph_conflict")
        else:
            records[key] = {"agree": False, "verdict": "structural",
                            "alternatives": [line_a["text"], line_b["text"]]}
            reasons.append("sample_conflict")
    if records:
        evidence["agreement"] = False
        evidence["lines"] = records
    return evidence, _ordered_unique(reasons)


def _apply_disagreement(payload: dict, kind: str, evidence: dict) -> None:
    if kind == figure_extract.KIND_TABLE:
        records = evidence.get("cells", {})
        for row in payload["rows"]:
            for cell in row["cells"]:
                record = records.get(f"r{row['row_index']}{cell['column_id']}")
                if record is not None and not record.get("agree", True):
                    _apply_cell_verdict(cell, record)
    elif kind == figure_extract.KIND_TERMINAL:
        records = evidence.get("lines", {})
        for line in payload["lines"]:
            record = records.get(str(line["line_index"]))
            if record is not None and not record.get("agree", True):
                _apply_line_verdict(line, record)


# ============================================================
# 11. native lane：canonical payload 與多策略比對
# ============================================================
GEOMETRY_SPAN_IOU = 0.9      # 兩列同一欄的 cell bbox 幾乎重合 ⇒ 幾何上是同一格
SPAN_COVER_MIN = 0.8         # 一個高 cell 覆蓋某個列帶的比例門檻


def _grid_to_table_payload(grid: dict, *, where: str) -> dict:
    header = list(grid["header"])
    if not header or not any(label.strip() for label in header):
        raise figure_extract.FigureExtractionError(
            f"{where}: 原生表格的表頭整列皆空，組不出合法的 canonical table"
            "（不得憑空造欄名——那會讓每一格的欄位身分變成程式編的）"
        )
    columns = [
        {"column_id": f"c{i + 1}", "label": label, "role": None}
        for i, label in enumerate(header)
    ]
    rows = []
    for position, row in enumerate(grid["rows"], 1):
        # 契約 §15.3：寬度必須**精確**等於表頭欄數。只迭代表頭欄數會把多出來的
        # 格無聲丟掉（三格資料配兩欄表頭 → 第三格消失），補空格則是憑空造值。
        if len(row) != len(columns):
            raise figure_extract.FigureExtractionError(
                f"{where}: 原生表格第 {position} 列有 {len(row)} 格，表頭卻是 "
                f"{len(columns)} 欄——不補不砍，寬度不對就是不合格"
            )
        cells = []
        for j in range(len(columns)):
            value = row[j]
            unrepresentable = value is None
            cells.append({
                "column_id": columns[j]["column_id"],
                "text": figure_extract.UNREADABLE_GLYPH if unrepresentable else value,
                "state": (figure_extract.CELL_STATE_UNREADABLE if unrepresentable
                          else figure_extract.CELL_STATE_OBSERVED),
                "inherited_from_row": None,
            })
        rows.append({"row_index": position, "cells": cells})
    return {"kind": figure_extract.KIND_TABLE, "columns": columns, "rows": rows,
            "footnotes": []}


def _lines_to_terminal_payload(texts: Sequence[str]) -> dict:
    """raw 行 → canonical terminal payload。**原值原樣帶入，不做任何清理。**

    刻意**不** `rstrip("\r")`：契約 §2.3 規定一個 line 不得含 `\r`/`\n`，而清理過
    的字串永遠通不過那條檢查——validator 會變成擺設，非法字元則在驗證之前就被
    無聲改寫（總審 BLOCKER #6）。原值保留，交 `validate_payload` fail-loud。
    """
    return {
        "kind": figure_extract.KIND_TERMINAL,
        "lines": [
            {"line_index": i + 1, "text": text, "uncertain_spans": []}
            for i, text in enumerate(texts)
        ],
    }


# canonical 文字只能來自這三個通道。`find_tables:*` **不在裡面，而且沒有 fallback**：
# `Table.extract()` 已知會把 `0x4000_0100` 讀成 `0x4000 0100` 加一段落單的底線，
# 所以就算它是唯一可用通道，也不得把已被改寫的內容當正文
# （契約 §15.2 / 總審 BLOCKER #2）。
# `markdown_pos` 排第一：它是**唯一**保留 header band（多層表頭）與格內換行的通道。
# word geometry 的 grid 固定把 geometry 第一列當表頭，格內又只按 x 排序後以單一
# 空格串接——多層表頭會被當成資料列、格內換行會被壓成空格（local #6）。
# word 通道仍是最重要的 anchor（它證明得了 `0x4000_0100`），只是不當正文來源。
_CANONICAL_CHANNEL_RANK = {"markdown_pos": 0, "words_geometry": 1, "words_columns": 2}


def _pick_canonical_channel(channels):
    """挑 canonical 通道；沒有**安全**通道就回 `(None, None)`（呼叫端 abstain）。

    順序：`markdown_pos`（保留 header band 與格內換行）→ `words_geometry` →
    `words_columns`。`find_tables:*` 只當 evidence，**永遠**不當 canonical——
    連「它是唯一通道」都不例外，那正是已知會改寫 hex 的那條路。
    """

    def usable_header(grid):
        return grid["header"] and any((label or "").strip() for label in grid["header"])

    def complete(grid):
        return all(cell is not None for row in grid["rows"] for cell in row)

    safe = sorted(
        ((_CANONICAL_CHANNEL_RANK[name], name, grid) for name, grid in channels
         if name in _CANONICAL_CHANNEL_RANK),
        key=lambda item: item[0],
    )
    for require_complete in (True, False):
        for _rank, name, grid in safe:
            if usable_header(grid) and (complete(grid) or not require_complete):
                return name, grid
    return None, None


def _apply_rowspan_filldown(payload: dict, candidate, findings: _Findings) -> dict:
    """依 **cell geometry** 做 fill-down。這是本輪唯一被允許的 rowspan 證據來源。

    契約 §6.4 原本還列了「兩個獨立 extractor 對 span 一致」，但在目前的 schema 與
    呼叫模型下**沒有可實作的證據來源**：模型端 schema 不輸出 rowspan/span 假設，
    同一顆模型的兩次取樣也不是獨立佐證，而且「兩次都輸出空格」根本分不出
    intentional blank 與 merged cell。留一個永遠走不到的分支假裝有兩條證據，比
    只有一條更危險，所以本輪就只有 geometry 這一條。

    **沒有證據的空格保持空**——「保留 / 未實作 / 不適用」本來就該是空。
    """
    conflicts: dict[str, dict] = {}
    cells = _geometry_cells(candidate)
    rows = payload["rows"]
    if not cells or not rows:
        return conflicts
    offset = 1 if len(cells) == len(rows) + 1 else 0
    if len(cells) - offset < len(rows):
        findings.note("rowspan_geometry_unusable", "geometry 的列數與 payload 對不上，不做 fill-down")
        return

    n_cols = len(payload["columns"])
    for col in range(n_cols):
        run_start = 0
        while run_start < len(rows):
            run_end = run_start
            while run_end + 1 < len(rows):
                try:
                    box_a = _as_bbox(cells[run_end + offset][col])
                    box_b = _as_bbox(cells[run_end + 1 + offset][col])
                except IndexError:
                    break
                if box_a is None or box_b is None or _iou(box_a, box_b) < GEOMETRY_SPAN_IOU:
                    break
                run_end += 1
            if run_end > run_start:
                source_row = rows[run_start]
                source_cell = source_row["cells"][col]
                if source_cell["text"].strip():
                    for index in range(run_start + 1, run_end + 1):
                        cell = rows[index]["cells"][col]
                        if cell["text"].strip() and \
                                figure_extract.normalize_for_compare(cell["text"]) != \
                                figure_extract.normalize_for_compare(source_cell["text"]):
                            # geometry 說是同一格、文字卻不同：兩份都是候選，留下
                            # 任何一份都是擇一（local #8）。正文換成 `▯`，兩份原文
                            # 只進 evidence。
                            key = f"r{rows[index]['row_index']}{cell['column_id']}"
                            conflicts[key] = {
                                "anchor": "cell_geometry", "matched": False,
                                "verdict": "structural", "critical_ok": False,
                                "payload": cell["text"], "raw": source_cell["text"],
                                "alternatives": [cell["text"], source_cell["text"]],
                                "channels": ["cell_geometry"],
                                "inherited_from_row": source_row["row_index"],
                            }
                            cell["text"] = figure_extract.UNREADABLE_GLYPH
                            cell["state"] = figure_extract.CELL_STATE_CONFLICT
                            cell["inherited_from_row"] = None
                            findings.block(
                                "span_ambiguous",
                                f"row {rows[index]['row_index']} 的 {cell['column_id']}："
                                f"geometry 說跨列（來源 row {source_row['row_index']}），"
                                "但兩列的文字不同",
                            )
                            continue
                        cell["text"] = source_cell["text"]
                        cell["state"] = figure_extract.CELL_STATE_INHERITED
                        cell["inherited_from_row"] = source_row["row_index"]
                    findings.note(
                        "rowspan_filldown",
                        f"{source_cell['column_id']}: row "
                        f"{source_row['row_index']}–{rows[run_end]['row_index']} 依 geometry 跨列",
                    )
            run_start = run_end + 1
    return conflicts


def _inject_geometry_records(payload: dict, alignment: dict) -> None:
    """把 fill-down 過的格記成「由 cell geometry 佐證」。

    geometry 本來就是一個獨立通道，而且它正是證明 rowspan 的那一個；不記進去
    會讓每一張含合併儲存格的表因為覆蓋率不足而拿不到應得的等級。
    """
    records = alignment.setdefault("cells", {})
    total = alignment.get("anchor_coverage", {}).get("atoms_total", 0)
    for row in payload["rows"]:
        for cell in row["cells"]:
            if cell["state"] != figure_extract.CELL_STATE_INHERITED:
                continue
            records[f"r{row['row_index']}{cell['column_id']}"] = {
                "anchor": "cell_geometry", "matched": True, "raw": cell["text"],
                "payload": cell["text"], "critical_ok": True,
                "channels": ["cell_geometry"],
                "state_source": "geometry_span",
                "by_channel": {"cell_geometry": {"verdict": "match", "reliable": True,
                                                 "critical_ok": True}},
                "inherited_from_row": cell["inherited_from_row"],
            }
    alignment["anchor_coverage"] = _recount_coverage(
        records, total, exclude=alignment.get("anchor_coverage", {}).get("excluded_channel", "")
    )


def _native_checks(payload, channels, candidate, alignment) -> dict:
    """native_verified 的固定 check 集合。缺任何一個 key 一律當 False。"""
    checks = {name: False for name in NATIVE_REQUIRED_CHECKS}
    checks["second_channel"] = len(channels) >= 2

    headers = [
        tuple(figure_extract.normalize_for_compare(label or "") for label in grid["header"])
        for _name, grid in channels
    ]
    checks["header_agreement"] = bool(headers) and len(set(headers)) == 1
    counts = {len(grid["rows"]) for _name, grid in channels}
    checks["row_count_agreement"] = len(counts) == 1 and counts.pop() == len(payload["rows"])

    cells = _geometry_cells(candidate)
    if cells:
        widths = {len(row) for row in cells}
        checks["cell_geometry"] = (
            len(widths) == 1
            and widths.pop() == len(payload["columns"])
            and len(cells) in (len(payload["rows"]), len(payload["rows"]) + 1)
        )

    # 逐格要求「至少一個可靠、非 canonical 的通道明確 match」。原本只看 blocker
    # 是否缺席——沒有比對過的格與被中和的 artifact 都會被當成一致（local #4）。
    canonical = alignment.get("anchor_coverage", {}).get("excluded_channel", "")
    records = alignment.get("cells", {})
    expected_keys = {
        f"r{row['row_index']}{cell['column_id']}"
        for row in payload["rows"] for cell in row["cells"]
    }
    text_ok = bool(expected_keys)
    token_ok = bool(expected_keys)
    for key in expected_keys:
        record = records.get(key) or {}
        if record.get("state_source") == "geometry_span":
            continue    # fill-down 的格由 cell geometry 佐證，見 _inject_geometry_records
        verdicts = [
            item for name, item in (record.get("by_channel") or {}).items()
            if name != canonical and item.get("reliable")
        ]
        matches = [item for item in verdicts if item.get("verdict") == "match"]
        if not matches:
            text_ok = False
            token_ok = False
            break
        if not all(item.get("critical_ok") for item in matches):
            token_ok = False
    checks["critical_token_agreement"] = token_ok
    checks["cell_text_agreement"] = text_ok
    return checks


def verify_native_table(candidate: Candidate, evidence: PageEvidence) -> FigureResult:
    """原生表格的多策略比對（**零 VL**，契約 §12.1）。

    `native_verified` 需要 `NATIVE_REQUIRED_CHECKS` **全部**為真：geometry 加上
    至少另一個原生通道，且表頭、列數、cell geometry、critical token 與逐格文字
    全部一致。單次 `find_tables().extract()` 只有一個通道，永遠到不了。
    「空格率 < 30%」這種指標不存在於本模組——它證明不了任何一格是對的。
    """
    where = (
        f"page={getattr(candidate, 'page', '?')} "
        f"figure={getattr(candidate, 'figure_id', '?')}"
    )
    findings = _Findings()
    notes: list[list[str]] = []
    channels, _words, _unreliable = _table_channels(0, candidate, evidence, notes)
    for slug, detail in notes:
        findings.note(slug, detail)
    if not channels:
        raise figure_extract.FigureExtractionError(
            f"{where}: native lane 找不到任何原生通道（候選不該被升格成 structured）"
        )

    canonical_name, canonical_grid = _pick_canonical_channel(channels)
    if canonical_grid is None:
        raise figure_extract.FigureExtractionError(
            f"{where}: 只剩 {[name for name, _grid in channels]} 可用，沒有安全的 "
            "canonical 通道（words geometry / markdown pos）。"
            "find_tables().extract() 已知會改寫 hex 底線，就算它是唯一通道也不得當正文"
        )
    payload = _grid_to_table_payload(canonical_grid, where=where)
    # fill-down 必須在對齊**之前**：inherited 的格不參與 anchor 比對（見
    # align_table_cells 內的說明），順序反了會把正確的 rowspan 打成 conflict。
    span_conflicts = _apply_rowspan_filldown(payload, candidate, findings)
    alignment = align_table_cells(payload, candidate, evidence)
    # rowspan 的正面矛盾也是 cell 級 evidence，要進 anchor 統計與 manifest。
    alignment.setdefault("cells", {}).update(span_conflicts)
    checks = _native_checks(payload, channels, candidate, alignment)

    for slug, detail in alignment.get("blockers", []):
        findings.block(slug, detail)
    for slug, detail in alignment.get("notes", []):
        findings.note(slug, detail)
    for name in alignment.get("channels", []):
        findings.channel(name)
    if len(channels) < 2:
        findings.note("single_channel_only",
                      f"只有 {channels[0][0]} 一個原生通道，拿不到 native_verified")
    if len(channels) >= 2 and not checks["header_agreement"]:
        # 表頭不一致代表兩個通道對「這張表有哪些欄位」的看法不同——每一格的欄位
        # 身分因此都不可信。只降 check、讓它落到 corroborated 是不夠的（local #4）。
        findings.block(
            "header_conflict",
            "原生通道之間的表頭不一致："
            + " / ".join(
                f"{name}={[label or '' for label in grid['header']]}" for name, grid in channels
            ),
        )

    _apply_alignment(payload, figure_extract.KIND_TABLE, alignment)
    _inject_geometry_records(payload, alignment)

    findings.native_checks = checks
    alignment["anchor_coverage"] = _recount_coverage(
        alignment.get("cells", {}),
        alignment.get("anchor_coverage", {}).get("atoms_total", 0),
        exclude=canonical_name,
    )
    evidence_dict = _build_evidence(alignment, native={
        "canonical_channel": canonical_name,
        "checks": checks,
        "channel_grids": {
            name: {"rows": len(grid["rows"]), "cols": len(grid["header"])}
            for name, grid in channels
        },
    })
    return _build_result(
        candidate, figure_extract.KIND_TABLE, payload, findings, evidence_dict,
        lane="native", model_input_variant="native", variants=[], where=where,
    )


def _verify_native_terminal(candidate, evidence) -> FigureResult:
    """原生文字 log 的行級比對（零 VL）。

    canonical 文字**只**來自 `pos` 的 raw substring——它保留空行與原始空白。
    沒有 `pos` 就不是 native lane（契約 §15.1），因為 word baseline 還原不了行首
    縮排、行尾空白與無字空行；那種候選走 VL lane，words 只當 secondary anchor。
    """
    where = (
        f"page={getattr(candidate, 'page', '?')} "
        f"figure={getattr(candidate, 'figure_id', '?')}"
    )
    findings = _Findings()
    raw_markdown = getattr(evidence, "raw_markdown", "")
    signal = _native_text_signal(candidate)
    pos = signal["pos"] if signal else None
    if pos is None:
        # 直接呼叫本函式的單元測試可能沒有 signal；production 一定有（T3 只有在
        # `native_text` 成立時才會把 terminal 標成 native_lane）。
        pos = _page_box_pos_for(evidence, getattr(candidate, "bbox", None))
        native = getattr(candidate, "native_table", None)
        if pos is None and isinstance(native, dict):
            pos = native.get("pos")
    canonical_channel = "markdown_pos"
    text, problem = _pos_slice(raw_markdown, pos) if pos is not None else (None, "no_pos")
    if text is not None and signal is not None:
        declared = signal.get("markdown")
        if isinstance(declared, str) and declared != text:
            # plan 與這一份 PageEvidence 對不起來（例如 plan 是別次 ingest 產的）。
            # 兩份 raw 不一致時繼續往下走，就是拿不知道是誰的原文當 canonical。
            raise figure_extract.FigureExtractionError(
                f"{where}: signals['native_text'] 的原文與 raw_markdown[{pos}] 不一致"
                "（plan 與 PageEvidence 不同步），不得挑一份當 canonical"
            )
    if text is None:
        # 契約 §15.1：terminal 的 native lane **只有**在有 `pos` 支撐的 raw markdown
        # 時才成立。word baseline 證明不了行首縮排、行尾空白與無字空行，用
        # `" ".join(...)` 合成的正文是猜出來的普通空格（總審 BLOCKER #5），
        # 那種候選要走 VL lane（words 仍當 secondary anchor）。
        raise figure_extract.FigureExtractionError(
            f"{where}: native_lane=True 但取不到 pos 支撐的 raw markdown（{problem}）。"
            "word geometry 單獨不足以構成 terminal 的 native lane——"
            "它還原不了縮排/行尾空白/空行，合成出來的正文是猜的"
        )
    texts = text.split("\n")

    payload = _lines_to_terminal_payload(texts)
    alignment = align_terminal_lines(payload, candidate, evidence)
    for slug, detail in alignment.get("blockers", []):
        findings.block(slug, detail)
    for slug, detail in alignment.get("notes", []):
        findings.note(slug, detail)
    for name in alignment.get("channels", []):
        findings.channel(name)
    if len(alignment.get("channels", [])) < 2:
        findings.note("single_channel_only", "只有一個原生通道，拿不到 corroborated")
    _apply_alignment(payload, figure_extract.KIND_TERMINAL, alignment)
    alignment["anchor_coverage"] = _recount_coverage(
        alignment.get("lines", {}),
        alignment.get("anchor_coverage", {}).get("atoms_total", 0),
        exclude=canonical_channel,
    )
    evidence_dict = _build_evidence(alignment)
    return _build_result(
        candidate, figure_extract.KIND_TERMINAL, payload, findings, evidence_dict,
        lane="native", model_input_variant="native", variants=[], where=where,
    )


# ============================================================
# 12. payload invariant、狀態判定、FigureResult 組裝
# ============================================================
def _payload_sentinels(payload: dict, kind: str) -> list[str]:
    """payload 裡所有「不確定」的痕跡。非空 ⇒ 這份內容不可能是 trusted。"""
    glyph = figure_extract.UNREADABLE_GLYPH
    found: list[str] = []
    if kind == figure_extract.KIND_TABLE:
        for row in payload["rows"]:
            for cell in row["cells"]:
                if glyph in cell["text"]:
                    found.append(f"row {row['row_index']} 的 {cell['column_id']} 含 {glyph}")
                if cell["state"] in (figure_extract.CELL_STATE_UNREADABLE,
                                     figure_extract.CELL_STATE_CONFLICT):
                    found.append(
                        f"row {row['row_index']} 的 {cell['column_id']} state={cell['state']}"
                    )
    elif kind == figure_extract.KIND_TERMINAL:
        for line in payload["lines"]:
            if glyph in line["text"]:
                found.append(f"line {line['line_index']} 含 {glyph}")
            if line["uncertain_spans"]:
                found.append(f"line {line['line_index']} 有 uncertain_spans")
    else:
        blob = json.dumps(payload, ensure_ascii=False)
        if glyph in blob:
            found.append(f"diagram 內容含 {glyph}")
    return found


def _normalize_model_unreadable(payload: dict, kind: str, findings: _Findings) -> None:
    """模型自報看不清的地方，正文一律換成 `▯`，不保留它「猜到一半」的字。"""
    glyph = figure_extract.UNREADABLE_GLYPH
    if kind == figure_extract.KIND_TABLE:
        for row in payload["rows"]:
            for cell in row["cells"]:
                if cell["state"] == figure_extract.CELL_STATE_UNREADABLE:
                    cell["text"] = glyph
                    findings.block(
                        "model_unreadable",
                        f"row {row['row_index']} 的 {cell['column_id']}：模型自報看不清",
                    )
    elif kind == figure_extract.KIND_TERMINAL:
        for line in payload["lines"]:
            if line["uncertain_spans"]:
                findings.block(
                    "model_unreadable", f"line {line['line_index']}：模型標了不確定區間"
                )


def _finalize_payload(payload: dict, kind: str, findings: _Findings, *, where: str) -> None:
    """所有轉換（遮罩 / 接合 / fill-down / span 合併）完成後的最後一道。

    每一種轉換都會再改 payload，所以**只在初次 parse 時驗一次是不夠的**：這裡
    重跑 `validate_payload`，並確認「有 `▯` 就一定有 blocker」這條不變式。
    重驗失敗代表本模組把 payload 改壞了，一律 `FigureExtractionError`（零寫入），
    不得帶著不合法的結構繼續往 KB 走。
    """
    try:
        figure_extract.validate_payload(payload, kind)
    except figure_extract.FigureValidationError as exc:
        raise figure_extract.FigureExtractionError(
            f"{where}: 驗證後的 payload 不再合法（遮罩 / 接合 / fill-down 之後）：{exc}"
        ) from exc
    sentinels = _payload_sentinels(payload, kind)
    if sentinels and not findings.blockers:
        findings.block(
            "unreadable_content",
            "payload 仍有不確定內容：" + "；".join(sentinels[:5]),
        )


def _build_evidence(alignment: dict, *, native: dict | None = None,
                    repeatability: dict | None = None, stitch: dict | None = None,
                    extra: dict | None = None) -> dict:
    """契約 §6.4 的 evidence 形狀（會被 T5 持久化進 manifest）。"""
    evidence: dict[str, Any] = {
        "channels": list(alignment.get("channels", [])),
        "cells": alignment.get("cells", {}),
        "lines": alignment.get("lines", {}),
        "unlocatable_tokens": list(alignment.get("unlocatable_tokens", [])),
        "anchor_coverage": alignment.get("anchor_coverage", {}),
        "row_alignment": alignment.get("row_alignment", {}),
        "line_alignment": alignment.get("line_alignment", {}),
        "stitch": stitch or {},
    }
    if native is not None:
        evidence["native"] = native
    if repeatability is not None:
        evidence["repeatability"] = repeatability
        evidence["channels"].append("vl_sample_2")
    if extra:
        evidence.update(extra)
    return evidence


def _recount_coverage(records: dict, atoms_total: int, *, exclude: str) -> dict:
    """重算 anchor 覆蓋率，**排除 canonical 通道自己**。

    native lane 的 payload 就是從某個通道組出來的，拿它跟自己比一定全中。少了
    這一步，「只有一個原生通道」的表會拿到 `corroborated` —— 那是自我佐證，
    也是最容易在審查中被忽略的一種未驗證內容升級。
    """
    anchorable = matched = 0
    for record in records.values():
        # 只認「可靠、非 canonical 的通道」留下的**獨立判定**。沿用聚合的
        # `matched=True` 會把 canonical 自比、以及 unreliable 通道的 artifact
        # 當成佐證（總審 local #4）。
        verdicts = {
            name: item for name, item in (record.get("by_channel") or {}).items()
            if name != exclude and item.get("reliable")
        }
        if not verdicts:
            continue
        anchorable += 1
        if (record.get("matched") is True
                and any(item.get("verdict") == "match" for item in verdicts.values())):
            matched += 1
    return {
        "atoms_total": atoms_total, "atoms_anchorable": anchorable,
        "atoms_matched": matched,
        "ratio": (matched / atoms_total) if atoms_total else 0.0,
        "excluded_channel": exclude,
    }


def _decide_status(findings: _Findings, lane: str) -> tuple[str, str, list[str], list[str]]:
    """狀態判定決策樹（依序，第一個命中即定案）。

    1. 有任何 blocker                                    → `needs_review`
    2. native lane 且 `NATIVE_REQUIRED_CHECKS` 全過      → `native_verified`
    3. 每個原子都有 anchor 且全部相符（覆蓋率 1.0）      → `corroborated`
    4. 其餘                                              → `unverified`

    `human_verified` 與 `legacy_unverified` 由 T5 / T6 產生，本模組永不產出。
    `unverified` 是**合法終態**（契約 §12.1），不是「還要再驗一次」。
    """
    coverage_note: list[tuple[str, str]] = []
    if findings.blockers:
        status = figure_extract.VERIF_NEEDS_REVIEW
    elif lane == "native" and findings.native_checks is not None and all(
        findings.native_checks.get(name, False) for name in NATIVE_REQUIRED_CHECKS
    ):
        status = figure_extract.VERIF_NATIVE
    elif (
        findings.atoms_total > 0
        and findings.atoms_anchorable == findings.atoms_total
        and findings.atoms_matched == findings.atoms_total
    ):
        status = figure_extract.VERIF_CORROBORATED
    else:
        status = figure_extract.VERIF_UNVERIFIED
        if findings.atoms_anchorable == 0:
            coverage_note.append(("no_anchor_evidence", "沒有任何原子取得獨立佐證"))
        else:
            coverage_note.append((
                "partial_anchor_coverage",
                f"{findings.atoms_matched}/{findings.atoms_total} 個原子有獨立佐證且相符",
            ))
    for slug, detail in coverage_note:
        if slug not in {s for s, _ in findings.notes}:
            findings.note(slug, detail)
    return figure_extract.EXTRACTION_COMPLETE, status, findings.slugs(), findings.details()


def _occurrences_for(candidate, page: int, bbox) -> list[dict]:
    raw = list(getattr(candidate, "occurrences", None) or [])
    cleaned = [
        {"page": int(item["page"]), "bbox": [float(v) for v in item["bbox"]],
         "index": int(item.get("index", 0))}
        for item in raw
        if isinstance(item, dict) and "page" in item and "bbox" in item
    ]
    if not cleaned or page not in {item["page"] for item in cleaned}:
        cleaned.append({"page": int(page), "bbox": [float(v) for v in bbox], "index": 0})
    return cleaned


def _build_result(candidate, kind: str, payload: dict, findings: _Findings, evidence: dict,
                  *, lane: str, model_input_variant: str, variants: list[str],
                  where: str) -> FigureResult:
    _normalize_model_unreadable(payload, kind, findings)
    coverage = evidence.get("anchor_coverage") or {}
    findings.atoms_total = int(coverage.get("atoms_total", 0) or 0)
    findings.atoms_anchorable = int(coverage.get("atoms_anchorable", 0) or 0)
    findings.atoms_matched = int(coverage.get("atoms_matched", 0) or 0)
    _finalize_payload(payload, kind, findings, where=where)
    extraction, verification, reasons, details = _decide_status(findings, lane)

    row_total = line_total = None
    if kind == figure_extract.KIND_TABLE:
        row_total = payload["rows"][-1]["row_index"] if payload["rows"] else 0
    elif kind == figure_extract.KIND_TERMINAL:
        line_total = payload["lines"][-1]["line_index"] if payload["lines"] else 0

    bbox = tuple(getattr(candidate, "bbox", (0.0, 0.0, 0.0, 0.0)))
    page = int(getattr(candidate, "page", 1) or 1)
    evidence = dict(evidence)
    evidence["lane"] = lane
    return FigureResult(
        figure_id=getattr(candidate, "figure_id", ""),
        document_id=getattr(candidate, "document_id", ""),
        page=page,
        figure_index=1,
        bbox=bbox,
        kind=kind,
        revision=1,
        payload=payload,
        extraction_status=extraction,
        verification_status=verification,
        reasons=reasons,
        reason_details=details,
        evidence=evidence,
        occurrences=_occurrences_for(candidate, page, bbox),
        model_input_variant=model_input_variant,
        variants=list(variants),
        row_total=row_total,
        line_total=line_total,
    )


def _failed_result(candidate, kind: str, reason: str) -> FigureResult:
    """抽取失敗時給 T5 寫 review artifact 用（契約 §12.2）。**永不**回傳給呼叫端。"""
    bbox = tuple(getattr(candidate, "bbox", (0.0, 0.0, 0.0, 0.0)))
    page = int(getattr(candidate, "page", 1) or 1)
    return FigureResult(
        figure_id=getattr(candidate, "figure_id", ""),
        document_id=getattr(candidate, "document_id", ""),
        page=page, figure_index=1, bbox=bbox,
        kind=kind if kind in figure_extract.FIGURE_KINDS else figure_extract.KIND_TABLE,
        revision=1, payload=None,
        extraction_status=figure_extract.EXTRACTION_FAILED,
        verification_status=figure_extract.VERIF_NEEDS_REVIEW,
        reasons=["extraction_failed"], reason_details=[reason],
        evidence={"failure": reason},
        occurrences=_occurrences_for(candidate, page, bbox),
        model_input_variant="failed", variants=[],
        row_total=None, line_total=None,
    )


# ============================================================
# 13. 兩份 semantic validator（kind 歧義的解法）
# ============================================================
_SHELL_MARKERS = ("$ ", "# ", "> ", ">>> ", "PS ", "root@", "C:\\", "\x1b[")
_LOG_LEVELS = ("INFO", "WARN", "WARNING", "ERROR", "DEBUG", "TRACE", "FATAL", "NOTICE")
_TIMESTAMP_RE = re.compile(
    r"\[\s*\d+\.\d+\s*\]"                 # [    0.000000] kernel ring buffer
    r"|\d{2}:\d{2}:\d{2}"                   # 12:34:56
    r"|\d{4}-\d{2}-\d{2}"                   # 2026-08-22
)
_PATH_RE = re.compile(r"(?:^|\s)/[\w.+-]+/[\w./+-]*")


def _looks_like_log_line(text: str) -> bool:
    """一行文字有沒有 log 的形狀（prompt / 時間戳 / log level / 路徑 / ANSI）。"""
    stripped = text.strip()
    if not stripped:
        return False
    if stripped[0] in "$#>%" or any(marker in text for marker in _SHELL_MARKERS):
        return True
    if _TIMESTAMP_RE.search(text) or _PATH_RE.search(text):
        return True
    return any(level in text for level in _LOG_LEVELS)


def _weighted(signals: Sequence[tuple[float, float]]) -> float:
    total = sum(weight for _value, weight in signals)
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, sum(value * weight for value, weight in signals) / total))


def _score_as_table(payload: dict) -> float:
    """「這份 payload 像不像表格」的語義分數（0..1）。

    刻意只用結構訊號（欄數、每格的 token 數、表頭品質、log 形狀），不看顏色也
    不看底色：白底 / 淺色主題 / 紙本列印的 log 與無框線的 memory map 都是實際
    目標，「暗底 > 60%」那類必要條件會直接把它們排除掉。

    `multi_col` 與 `log_shape` 權重最重：**一欄的「表」沒有任何欄位配對可言**，
    而每一格都長得像 log 行時，它幾乎一定是被誤判 kind 的終端機截圖。
    """
    columns, rows = payload["columns"], payload["rows"]
    if not columns or not rows:
        return 0.0
    texts = [cell["text"] for row in rows for cell in row["cells"]]
    non_empty = [text for text in texts if text.strip()] or texts
    multi_col = 0.0 if len(columns) < 2 else min(1.0, (len(columns) - 1) / 3.0)
    fieldish = sum(1 for t in non_empty if len(t.split()) <= 4) / len(non_empty)
    short = sum(1 for t in non_empty if len(t) <= 40) / len(non_empty)
    labels = [c["label"].strip() for c in columns]
    header = (sum(1 for label in labels if label) / len(labels)) * (
        len(set(labels)) / len(labels)
    )
    log_shape = sum(1 for t in non_empty if _looks_like_log_line(t)) / len(non_empty)
    return _weighted([
        (multi_col, 3.0), (fieldish, 2.0), (short, 1.0), (header, 1.0),
        (1.0 - log_shape, 3.0),
    ])


def _score_as_terminal(payload: dict) -> float:
    """「這份 payload 像不像終端機輸出」的語義分數（0..1）。

    `log_shape` 權重最重：把表格逐列讀成 log 的時候，行內容會是「欄位串接」，
    既沒有 prompt 也沒有時間戳，分數自然拉不起來。
    """
    lines = payload["lines"]
    if not lines:
        return 0.0
    texts = [line["text"] for line in lines]
    non_empty = [t for t in texts if t.strip()] or texts
    log_shape = sum(1 for t in non_empty if _looks_like_log_line(t)) / len(non_empty)
    lengths = [len(t) for t in texts]
    mean = sum(lengths) / len(lengths)
    spread = 0.0
    if mean > 0:
        variance = sum((n - mean) ** 2 for n in lengths) / len(lengths)
        spread = min(1.0, (variance ** 0.5) / mean)
    leading = sum(1 for t in texts if t[:1].isspace()) / len(texts)
    tableish = sum(1 for t in texts if t.count("|") >= 2) / len(texts)
    return _weighted([
        (log_shape, 4.0), (spread, 2.0), (leading, 1.0), (1.0 - tableish, 3.0),
    ])


def _score_payload(payload: dict, kind: str) -> float:
    if kind == figure_extract.KIND_TABLE:
        return _score_as_table(payload)
    if kind == figure_extract.KIND_TERMINAL:
        return _score_as_terminal(payload)
    return 0.0


# ============================================================
# 14. tile 接合
# ============================================================
def _boundary_overlap(variant) -> tuple[int, bool]:
    """這個 tile 與**前一張** tile 之間，最多可以有幾個原子是合法重複的。

    回傳 `(可去重的原子上限, 是否有可用的幾何證據)`。用的是 T3 實際放進
    `Variant.stitch` 的 band 幾何（`overlap_band_indices_top`）——那是「這張 tile
    的頂端有哪幾個 band 與上一張重疊」的**逐邊界**事實。

    先前用「全體 variants 共用一個上限、且 `overlap_px == 0` 也照樣找 suffix/prefix」
    的做法，會把跨邊界的**合法重複 log 行**靜默刪掉（local #10）。
    """
    stitch = getattr(variant, "stitch", None)
    if not isinstance(stitch, dict):
        return 0, False
    top = stitch.get("overlap_band_indices_top")
    if isinstance(top, list):
        return len(top), True
    return 0, False


def _table_row_key(row) -> tuple:
    return tuple((cell["text"], cell["state"]) for cell in row["cells"])


def _line_key(line) -> tuple:
    return (line["text"], json.dumps(line["uncertain_spans"], sort_keys=True))


def _find_overlap(existing_keys, incoming_keys, cap: int) -> int:
    """回傳「incoming 的前 k 個原子等於 existing 的後 k 個」的最大 k。

    比對是 **byte-exact**（含 state / uncertain_spans）：normalized 相等證明不了
    terminal 原文相同，而 log 本來就會有合法的重複行與空行——用 normalized 去找
    overlap 會把它們當成重疊吞掉。
    """
    limit = min(cap, len(existing_keys), len(incoming_keys))
    for k in range(limit, 0, -1):
        if list(existing_keys[-k:]) == list(incoming_keys[:k]):
            return k
    return 0


def _stitch_payloads(kind: str, payloads: list[dict], variants, findings: _Findings,
                     *, where: str) -> tuple[dict, dict]:
    """把多個 tile 的 payload 接成一份。回傳 `(payload, stitch_evidence)`。"""
    if len(payloads) == 1:
        return payloads[0], {"tiles": 1, "uncertain": False}

    stitch: dict[str, Any] = {"tiles": len(payloads), "uncertain": False,
                              "overlap_matched": [], "boundaries": [], "caps": []}

    def boundary(index: int):
        """第 index 張 tile（1-based）與前一張之間的去重額度。"""
        variant = variants[index - 1] if index - 1 < len(variants) else None
        if variant is None:
            return 0, False, 0
        cap, has_geometry = _boundary_overlap(variant)
        # 同上：已過 `_validate_variants()` 那道閘，保證是精確非負整數（§21.7）。
        overlap_px = variant.overlap_px
        return cap, has_geometry, overlap_px

    if kind == figure_extract.KIND_TABLE:
        labels = [tuple(c["label"] for c in p["columns"]) for p in payloads]
        if len(set(labels)) != 1:
            raise figure_extract.FigureExtractionError(
                f"{where}: 各 tile 的欄位標題不一致（{labels}），接不出安全的 canonical 表"
                "——挑其中一份等於替使用者決定哪個 tile 是對的"
            )
        rows = list(payloads[0]["rows"])
        keys = [_table_row_key(row) for row in rows]
        footnotes = list(payloads[0]["footnotes"])
        for index, payload in enumerate(payloads[1:], start=2):
            incoming = list(payload["rows"])
            incoming_keys = [_table_row_key(row) for row in incoming]
            cap, has_geometry, overlap_px = boundary(index)
            stitch["caps"].append(cap)
            k = _find_overlap(keys, incoming_keys, cap) if cap else 0
            if cap and k == 0:
                stitch["uncertain"] = True
                stitch["boundaries"].append(len(rows))
                findings.block(
                    "stitch_uncertain",
                    f"tile {index - 1}/{index} 的幾何說有 {cap} 列重疊，實際卻對不上；"
                    "不去重直接接，重複列留在原地待審",
                )
            elif not has_geometry and overlap_px > 0:
                stitch["uncertain"] = True
                stitch["boundaries"].append(len(rows))
                findings.block(
                    "stitch_uncertain",
                    f"tile {index - 1}/{index} 有 {overlap_px}px 重疊，但沒有 band 幾何"
                    "說得出哪幾列可以去重；不去重直接接",
                )
            stitch["overlap_matched"].append(k)
            rows.extend(incoming[k:])
            keys.extend(incoming_keys[k:])
            if payload["footnotes"] != payloads[0]["footnotes"]:
                findings.block(
                    "stitch_footnote_conflict",
                    f"tile {index} 的 footnotes 與 tile 1 不同——直接聯集會產生"
                    "一份沒有任何一張 tile 真的看過的註腳集合",
                )
            for note in payload["footnotes"]:
                if note not in footnotes:
                    footnotes.append(note)
        for position, row in enumerate(rows, 1):
            row["row_index"] = position
            for cell in row["cells"]:
                if cell["state"] != figure_extract.CELL_STATE_INHERITED:
                    cell["inherited_from_row"] = None
        merged = {"kind": figure_extract.KIND_TABLE, "columns": payloads[0]["columns"],
                  "rows": rows, "footnotes": footnotes}
        return merged, stitch

    lines = list(payloads[0]["lines"])
    keys = [_line_key(line) for line in lines]
    for index, payload in enumerate(payloads[1:], start=2):
        incoming = list(payload["lines"])
        incoming_keys = [_line_key(line) for line in incoming]
        cap, has_geometry, overlap_px = boundary(index)
        stitch["caps"].append(cap)
        k = _find_overlap(keys, incoming_keys, cap) if cap else 0
        if cap and k == 0:
            stitch["uncertain"] = True
            stitch["boundaries"].append(len(lines))
            findings.block(
                "stitch_uncertain",
                f"tile {index - 1}/{index} 的幾何說有 {cap} 行重疊，實際卻對不上；"
                "不去重直接接，重複行留在原地待審",
            )
        elif not has_geometry and overlap_px > 0:
            stitch["uncertain"] = True
            stitch["boundaries"].append(len(lines))
            findings.block(
                "stitch_uncertain",
                f"tile {index - 1}/{index} 有 {overlap_px}px 重疊，但沒有 band 幾何"
                "說得出哪幾行可以去重；不去重直接接",
            )
        stitch["overlap_matched"].append(k)
        lines.extend(incoming[k:])
        keys.extend(incoming_keys[k:])
    for position, line in enumerate(lines, 1):
        line["line_index"] = position
    return {"kind": figure_extract.KIND_TERMINAL, "lines": lines}, stitch


# ============================================================
# 15. lane 路由與 orchestration
# ============================================================
def _lane_for(candidate) -> str:
    """候選走哪條 lane。唯一真相是 `candidate.signals["native_lane"]`（T3 產生）。

    契約 §17.4：讀取本身也集中在 `figure_extract.read_native_lane()`——
    planner 的 preflight 預算、`RAG.py` 的 probe 判定與這裡**共用同一個 reader**。
    本模組原本自己用 truthiness 判斷，於是 `"false"` 這種字串在 RAG 是錯誤、在
    這裡卻是 native lane，**能跳過 VL capability probe**（終審 BLOCKER）。
    型別與缺值語意不一致，比各自重算更難發現：三邊都「有讀 signal」，只是讀法不同。

    `read_native_lane()` 對「`signals` 非 dict / 缺 key / 值不是精確 bool」一律
    `FigureExtractionError`（訊息帶 page/figure_id）。**verifier 不再有自己的判定**。
    """
    return "native" if figure_extract.read_native_lane(candidate) else "vl"


def _candidate_vl_kinds(candidate) -> set[str]:
    kind = getattr(candidate, "kind", figure_extract.KIND_UNKNOWN)
    if kind == figure_extract.KIND_UNKNOWN:
        return {figure_extract.KIND_TABLE, figure_extract.KIND_TERMINAL}
    return {kind}


DUPLICATE_VARIANT_PREFIX = "duplicate_of:"


def _share_key(candidate, kind: str, *, where: str):
    """重複影像的共享鍵：`(asset_digest, planner requested kind)`（契約 §19.3）。

    優先直接讀 planner 宣告的 `signals["vl_share_key"]`——preflight 就是拿它算
    「這一筆是 0 次 VL」的，verifier 用同一把鍵才不會出現「宣稱 0、實際跑滿」。
    自己另外重建一份看起來一樣的鍵，就是上一輪 4 次 VL 的成因：planner 用
    **原始** kind（可能是 `unknown`）、verifier 卻用**解歧後**的 kind。

    `vl_share_key` 缺席時（直接呼叫公開入口、或舊 plan）退回本地重建；
    宣告的 digest 與候選自己的 `asset_digest` 不一致 → fail-loud（那是 planner bug，
    猜哪一個對就等於把預算宣稱與實際行為悄悄拆開）。
    """
    digest = str(getattr(candidate, "asset_digest", "") or "")
    signals = getattr(candidate, "signals", None)
    declared = signals.get("vl_share_key") if isinstance(signals, dict) else None
    if isinstance(declared, dict):
        declared_digest = str(declared.get("asset_digest") or "")
        requested = str(declared.get("requested_kind") or "")
        if declared_digest and digest and declared_digest != digest:
            raise figure_extract.FigureExtractionError(
                f"{where}: signals['vl_share_key'].asset_digest={declared_digest!r} 與候選的 "
                f"asset_digest={digest!r} 不一致——preflight 與實際抽取會用到兩把不同的鍵"
            )
        if declared_digest and requested:
            return (declared_digest, requested)
    return (digest, kind) if digest else None




def _duplicate_reference(cached: dict, candidate, *, where: str) -> dict:
    """重複影像的 `duplicate_model_input`（契約 §19.1 的凍結表示法）。

    writer（`figure_review`）現在會**明確解析** `model_input_variant` 的
    `duplicate_of:` 前綴，並反查 `evidence["duplicate_model_input"]["figure_id"]`
    是不是同一份 manifest 內、**非 duplicate**、且真的有落盤模型輸入的 figure。
    producer 這側因此要保證那三個欄位填得起來——否則 writer 只會在發布時 fail，
    而那時已經跑完整份 PDF 了。

    三條都 fail-loud（不猜、不降級）：
      * 代表不能是自己（自我參照的 manifest 指不出任何真實模型輸入）；
      * 代表自己不能又是個 duplicate（sentinel 串接，writer 反查會落空）；
      * 代表必須真的有送過模型的 variant（`variants` 非空）。
    """
    representative = str(cached.get("figure_id") or "")
    variant_id = str(cached.get("variant") or "")
    variants = [str(item) for item in (cached.get("variants") or [])]
    own = str(getattr(candidate, "figure_id", "") or "")
    if not representative or representative == own:
        raise figure_extract.FigureExtractionError(
            f"{where}: duplicate 的代表 occurrence 是自己（figure_id={representative!r}），"
            "manifest 會指不出任何真實的模型輸入"
        )
    if variant_id.startswith(DUPLICATE_VARIANT_PREFIX):
        raise figure_extract.FigureExtractionError(
            f"{where}: 代表 occurrence {representative} 自己也是 duplicate"
            f"（model_input_variant={variant_id!r}）——sentinel 不得串接"
        )
    if not variant_id or not variants:
        raise figure_extract.FigureExtractionError(
            f"{where}: 代表 occurrence {representative} 沒有落盤的模型輸入"
            f"（model_input_variant={variant_id!r}, variants={variants}）"
        )
    return {"figure_id": representative, "model_input_variant": variant_id,
            "variants": variants}


def _run_native_lane(candidate, evidence, kind: str, where: str) -> FigureResult:
    if kind == figure_extract.KIND_TABLE:
        return verify_native_table(candidate, evidence)
    if kind == figure_extract.KIND_TERMINAL:
        return _verify_native_terminal(candidate, evidence)
    if kind == figure_extract.KIND_DIAGRAM:
        raise figure_extract.FigureExtractionError(
            f"{where}: diagram 候選不該進 structured lane（契約 §13.1：本輪由 legacy "
            "picture lane 處理），native lane 也組不出 diagram payload"
        )

    # KIND_UNKNOWN：table 與 terminal **各一次**，再由兩份 semantic validator 評分。
    outcomes: dict[str, Any] = {}
    for probe_kind in (figure_extract.KIND_TABLE, figure_extract.KIND_TERMINAL):
        try:
            outcomes[probe_kind] = _run_native_lane(candidate, evidence, probe_kind, where)
        except figure_extract.FigureError as exc:
            outcomes[probe_kind] = exc
    return _resolve_ambiguous(outcomes, where=where)


def _resolve_ambiguous(outcomes: dict, *, where: str) -> FigureResult:
    """dual pass 的解歧：**兩邊都要跑完**，再由兩份 semantic validator 評分。

    「第一份 payload 非空就停」會讓 kind 誤判時的錯誤結構直接入庫——非空從來
    不代表對。單邊失敗**不是**致命錯（那是 kind 的證據）；兩邊都失敗才 hard fail。
    """
    results = {k: v for k, v in outcomes.items() if isinstance(v, FigureResult)}
    errors = {k: f"{type(v).__name__}: {v}" for k, v in outcomes.items()
              if not isinstance(v, FigureResult)}
    if not results:
        raise figure_extract.FigureExtractionError(
            f"{where}: kind 歧義的 dual pass 兩邊都失敗（{errors}）"
        )
    scores = {k: _score_payload(v.payload, k) for k, v in results.items()}
    winner = max(scores, key=lambda k: scores[k])
    chosen = results[winner]
    reasons = list(chosen.reasons)
    details = list(chosen.reason_details)
    evidence = dict(chosen.evidence)
    evidence["ambiguous"] = {
        "scores": scores,
        "errors": errors,
        "payloads": {k: copy.deepcopy(v.payload) for k, v in results.items()},
        "winner": winner,
    }
    status = chosen.verification_status
    if len(scores) > 1:
        ordered = sorted(scores.values(), reverse=True)
        if ordered[0] - ordered[1] < float(config.FIGURE_KIND_MARGIN):
            reasons = _ordered_unique(["kind_ambiguous", *reasons])
            details = _ordered_unique([
                f"table/terminal 的 semantic 分數只差 {ordered[0] - ordered[1]:.3f}"
                f"（< FIGURE_KIND_MARGIN），kind 未能解歧", *details,
            ])
            status = figure_extract.worst_verification(
                [status, figure_extract.VERIF_NEEDS_REVIEW]
            )
            evidence["ambiguous"]["finding"] = ("kind_ambiguous", details[0])
        else:
            reasons = _ordered_unique(["kind_ambiguous_resolved", *reasons])
            details = _ordered_unique([
                f"dual pass 由 semantic validator 解歧為 {winner}"
                f"（分數 {scores}）", *details,
            ])
            evidence["ambiguous"]["finding"] = ("kind_ambiguous_resolved", details[0])
    else:
        reasons = _ordered_unique(["kind_ambiguous_resolved", *reasons])
        details = _ordered_unique([
            f"dual pass 只有 {winner} 產出合法 payload（另一邊：{errors}）", *details,
        ])
        evidence["ambiguous"]["finding"] = ("kind_ambiguous_resolved", details[0])
    return replace(chosen, reasons=reasons, reason_details=details,
                   verification_status=status, evidence=evidence)


def _vl_extract(kind: str, variants, ctx: dict, *, allow_retry: bool,
                cache_prompt: bool) -> list[dict]:
    payloads = []
    for variant in variants:
        payloads.append(_extract_variant_payload(
            kind=kind, variant=variant, base_url=ctx["base_url"], model=ctx["model"],
            profile=ctx["profile"], where=ctx["where"], allow_retry=allow_retry,
            counters=ctx["counters"], cache_prompt=cache_prompt,
        ))
    return payloads


def _vl_result_for_kind(candidate, evidence, kind: str, variants, ctx: dict,
                        *, allow_retry: bool, second_sample: bool) -> FigureResult:
    findings = _Findings()
    where = ctx["where"]
    payloads = _vl_extract(kind, variants, ctx, allow_retry=allow_retry, cache_prompt=True)
    payload, stitch = _stitch_payloads(kind, payloads, variants, findings, where=where)
    _finalize_payload(payload, kind, findings, where=where)
    # asset-level（跟著影像走）與 occurrence-level（跟著這一頁的 anchor 走）必須
    # 分開。先前快取的是「已被第一頁 anchor 遮罩過」的結果，第二頁因此繼承了
    # 別頁的 occurrence conflict，asset-level 的 stitch / repeatability evidence
    # 卻沒有一併帶過去（local #11）。
    pristine = copy.deepcopy(payload)
    asset_blockers = list(findings.blockers)     # 此刻只有 stitch 相關
    asset_notes = list(findings.notes)

    alignment = (
        align_table_cells(payload, candidate, evidence)
        if kind == figure_extract.KIND_TABLE
        else align_terminal_lines(payload, candidate, evidence)
        if kind == figure_extract.KIND_TERMINAL
        else _empty_alignment("no_anchor_evidence", "diagram 沒有格/行級 anchor",
                              atoms_total=0, key="cells")
    )
    for slug, detail in alignment.get("blockers", []):
        findings.block(slug, detail)
    for slug, detail in alignment.get("notes", []):
        findings.note(slug, detail)
    _apply_alignment(payload, kind, alignment)

    repeatability = None
    coverage = alignment.get("anchor_coverage", {})
    if second_sample and int(coverage.get("atoms_anchorable", 0) or 0) == 0:
        # 無 anchor 時唯一的驗證手段。第二次取樣**必須** cache_prompt=False：
        # 同模型同 cache 的重播不是獨立佐證，只是把同一份輸出再唸一次。
        try:
            second = _stitch_payloads(
                kind,
                _vl_extract(kind, variants, ctx, allow_retry=True, cache_prompt=False),
                variants, findings, where=where,
            )[0]
        except _SampleFailure as exc:
            findings.block("repeat_sample_failed",
                           f"第二次取樣失敗（{exc.slug}）：{exc.detail}")
            repeatability = {"samples": 1, "identical": None, "second_cache_prompt": False,
                             "error": exc.slug}
        else:
            disagreement, reasons = detect_disagreement(payload, second, kind)
            if disagreement.get("structural"):
                raise figure_extract.FigureExtractionError(
                    f"{where}: 兩次取樣的結構不同（{reasons}），沒有安全的 canonical 結構；"
                    "沿用其中一份等於替使用者擇一。整份 PDF 零寫入"
                )
            _apply_disagreement(payload, kind, disagreement)
            for slug in reasons:
                findings.block(slug, f"兩次取樣不一致（{slug}）")
            repeatability = {
                "samples": 2,
                "identical": bool(disagreement.get("agreement")),
                "second_cache_prompt": False,
                "detail": disagreement,
            }
            if disagreement.get("agreement"):
                findings.note(
                    "two_samples_agree",
                    "兩次取樣結果相同——這**不是**升級理由（契約 §3：無 anchor 的同模型"
                    "多次取樣即使全等也只到 unverified）",
                )

    # repeatability 是 asset 級的（同一張影像的兩次取樣），alignment 不是。
    asset_blockers += [item for item in findings.blockers
                       if item[0] in _ASSET_LEVEL_SLUGS and item not in asset_blockers]
    asset_notes += [item for item in findings.notes
                    if item[0] in _ASSET_LEVEL_SLUGS and item not in asset_notes]
    ctx["asset_snapshot"] = {
        "kind": kind,
        "payload": pristine,
        "disagreement": (repeatability or {}).get("detail"),
        "repeatability": repeatability,
        "stitch": stitch,
        "blockers": asset_blockers,
        "notes": asset_notes,
    }

    evidence_dict = _build_evidence(alignment, repeatability=repeatability, stitch=stitch)
    return _build_result(
        candidate, kind, payload, findings, evidence_dict, lane="vl",
        model_input_variant=str(getattr(variants[0], "variant_id", "") or "vl"),
        variants=[str(getattr(v, "variant_id", "")) for v in variants], where=where,
    )


def _run_vl_lane(candidate, evidence, kind: str, variants, ctx: dict) -> FigureResult:
    if kind != figure_extract.KIND_UNKNOWN:
        return _vl_result_for_kind(candidate, evidence, kind, variants, ctx,
                                   allow_retry=True, second_sample=True)

    # KIND_UNKNOWN：table、terminal 各**一個** logical pass（不重試、不再取第二次
    # 樣本）。契約 §12.2 明定每個 kind 最多一次 attempt；勝出的 kind 再打一次
    # 就會變成同一個 kind 被呼叫兩次。
    outcomes: dict[str, Any] = {}
    snapshots: dict[str, dict] = {}
    for probe_kind in (figure_extract.KIND_TABLE, figure_extract.KIND_TERMINAL):
        ctx.pop("asset_snapshot", None)
        try:
            outcomes[probe_kind] = _vl_result_for_kind(
                candidate, evidence, probe_kind, variants, ctx,
                allow_retry=False, second_sample=False,
            )
        except (figure_extract.FigureError, _SampleFailure) as exc:
            outcomes[probe_kind] = exc
        snapshot = ctx.pop("asset_snapshot", None)
        if snapshot is not None:
            snapshots[probe_kind] = snapshot

    result = _resolve_ambiguous(outcomes, where=ctx["where"])
    # 兩個 probe kind 各寫過一次 `ctx["asset_snapshot"]`，最後一個會蓋掉前一個——
    # 直接沿用等於把**落敗**那一份 payload 快取起來。改成明確挑勝出者，並把
    # kind 解歧的結論一起帶進 asset-level evidence，讓 duplicate occurrence 不必
    # 重跑 dual pass 也拿得到同一份揭露（契約 §19.3）。
    winner = snapshots.get(result.kind)
    if winner is not None:
        winner = dict(winner)
        ambiguous = result.evidence.get("ambiguous") or {}
        winner["ambiguous"] = ambiguous
        finding = ambiguous.get("finding")
        if finding:
            slug, detail = finding
            bucket = "blockers" if slug == "kind_ambiguous" else "notes"
            winner[bucket] = [*winner.get(bucket, []), (slug, detail)]
        ctx["asset_snapshot"] = winner
    return result


def extract_document_figures(plan: FigurePlan, *, pdf_doc, page_evidence, vl_base_url,
                             vl_model, render_variants,
                             on_progress=None) -> list[FigureResult]:
    """把 `FigurePlan` 的候選變成 `FigureResult` list（契約 §6.4）。

    native lane（有原生文字/幾何）零 VL；VL lane 只用在沒有原生文字的候選。
    任一 table/terminal 候選重試後仍不合格 → `FigureExtractionError`，訊息帶
    `.results`（已完成的）與 `.failed`（失敗那張），供 T5 寫失敗 artifact；
    **整份 PDF 零寫入**，函式不會回傳半套結果。

    `ensure_capability` 在這裡再跑一次（fingerprint 快取讓 `RAG.py` 那次成為
    cache hit）：把「probe 不過 ⇒ 零抽取」變成本模組自己的不變式，而不是依賴
    呼叫端記得先做。
    """
    def progress(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    candidates = sorted(
        list(getattr(plan, "candidates", None) or []),
        key=lambda c: (int(getattr(c, "page", 0) or 0), int(getattr(c, "index", 0) or 0)),
    )
    evidence_by_page = page_evidence or getattr(plan, "page_evidence", {}) or {}

    lanes: dict[int, str] = {}
    vl_kinds: set[str] = set()
    for position, candidate in enumerate(candidates):
        lanes[position] = _lane_for(candidate)
        if lanes[position] == "vl":
            vl_kinds |= _candidate_vl_kinds(candidate)

    if vl_kinds:
        ensure_capability(base_url=vl_base_url, model=vl_model, kinds=vl_kinds)

    results: list[FigureResult] = []
    counters: dict[str, int] = {"vl_calls": 0, "image_tokens": 0, "vl_calls_saved": 0}
    per_page: dict[int, int] = {}
    # key = planner 宣告的 `(asset_digest, requested kind)`（契約 §19.3）
    asset_cache: dict[tuple[str, str], dict] = {}
    total = len(candidates)

    for position, candidate in enumerate(candidates):
        page = int(getattr(candidate, "page", 1) or 1)
        kind = getattr(candidate, "kind", figure_extract.KIND_UNKNOWN)
        where = f"page={page} figure={getattr(candidate, 'figure_id', '?')}"
        evidence = evidence_by_page.get(page)
        if evidence is None:
            error = figure_extract.FigureExtractionError(
                f"{where}: plan 沒有這一頁的 PageEvidence，無法驗證（零寫入）"
            )
            error.results = results
            error.failed = _failed_result(candidate, kind, str(error))
            raise error

        lane = lanes[position]
        progress(f"[figure] {position + 1}/{total} p{page} kind={kind} lane={lane}")
        try:
            if lane == "native":
                result = _run_native_lane(candidate, evidence, kind, where)
            else:
                share_key = _share_key(candidate, kind, where=where)
                cached = asset_cache.get(share_key) if share_key else None
                # 契約 §19.3：命中比的是 planner 宣告的 `(digest, requested kind)`；
                # payload / alignment 則走**解歧後**的 kind。先前只存解歧後的 kind，
                # `unknown` 的重複候選因此永遠命不中——preflight 說第二筆 0 次，
                # 實際卻又跑了一整輪 dual pass（實測 4 次）。
                if cached is not None:
                    resolved = cached["resolved_kind"]
                    counters["vl_calls_saved"] += 1
                    findings = _Findings()
                    findings.note(
                        "duplicate_asset_reused",
                        f"與 {cached['figure_id']} 是同一張影像，沿用該次抽取結果"
                        "（只省 VL 計算，occurrence 一個都不少）",
                    )
                    # 從**未經任何頁面 anchor 遮罩**的原始抽取重新開始，再套回
                    # asset-level 的 evidence（stitch / 兩次取樣的字元衝突）。
                    # occurrence-level 的 alignment 則對這一頁重算。
                    payload = copy.deepcopy(cached["payload"])
                    if cached.get("disagreement"):
                        _apply_disagreement(payload, resolved, cached["disagreement"])
                    for slug, detail in cached.get("blockers", []):
                        findings.block(slug, detail)
                    for slug, detail in cached.get("notes", []):
                        findings.note(slug, detail)
                    alignment = (
                        align_table_cells(payload, candidate, evidence)
                        if resolved == figure_extract.KIND_TABLE
                        else align_terminal_lines(payload, candidate, evidence)
                        if resolved == figure_extract.KIND_TERMINAL
                        else _empty_alignment("no_anchor_evidence", "diagram 無格/行級 anchor",
                                              atoms_total=0, key="cells")
                    )
                    for slug, detail in alignment.get("blockers", []):
                        findings.block(slug, detail)
                    for slug, detail in alignment.get("notes", []):
                        findings.note(slug, detail)
                    _apply_alignment(payload, resolved, alignment)
                    # 契約 §15.6：這個 occurrence **沒有**送過模型。把代表
                    # occurrence 的 variant id 抄過來，會讓 manifest 把「重新
                    # render、從未送模」的 crop 標成實際模型輸入（總審 BLOCKER #7）。
                    # 改以 duplicate_of 交叉引用真正送模的那一張。
                    extra = {
                        "duplicate_of": cached["figure_id"],
                        "duplicate_model_input": _duplicate_reference(
                            cached, candidate, where=where),
                        "requested_kind": kind,
                    }
                    if cached.get("ambiguous"):
                        # kind 歧義的揭露也跟著影像走：duplicate 不重跑 dual pass，
                        # 但兩份 payload、分數與單邊錯誤仍要留得下來。
                        extra["ambiguous"] = cached["ambiguous"]
                    result = _build_result(
                        candidate, resolved, payload, findings,
                        _build_evidence(alignment,
                                        repeatability=cached.get("repeatability"),
                                        stitch=cached.get("stitch"),
                                        extra=extra),
                        lane="vl",
                        model_input_variant=(
                            f"{DUPLICATE_VARIANT_PREFIX}{cached['figure_id']}"),
                        variants=[], where=where,
                    )
                else:
                    variants = _validate_variants(candidate, render_variants(pdf_doc, candidate))
                    ctx = {"base_url": vl_base_url, "model": vl_model,
                           "profile": _resolve_prompt_profile(llama_client.get_props(vl_base_url)),
                           "where": where, "counters": counters}
                    result = _run_vl_lane(candidate, evidence, kind, variants, ctx)
                    snapshot = ctx.get("asset_snapshot")
                    if share_key and snapshot and snapshot["kind"] == result.kind:
                        asset_cache[share_key] = {
                            **snapshot,
                            # requested = planner 宣告共享時用的 kind（可能 unknown）；
                            # resolved = dual pass 之後真正的 kind。命中比 requested，
                            # 重放走 resolved（契約 §19.3）。
                            "requested_kind": kind,
                            "resolved_kind": result.kind,
                            # payload 是**抽取後、任何頁面 anchor 遮罩之前**的深拷貝；
                            # asset-level 的 stitch / 兩次取樣 evidence 一併帶走，
                            # occurrence-level 的 alignment 則每頁重算。
                            "payload": copy.deepcopy(snapshot["payload"]),
                            "figure_id": result.figure_id,
                            "variant": result.model_input_variant,
                            "variants": list(result.variants),
                        }
        except _SampleFailure as exc:
            hint = _FAILURE_HINTS.get(exc.slug, "")
            message = (
                f"{where}: structured 抽取失敗（{exc.slug}）：{exc.detail}"
                + (f"。建議：{hint}" if hint else "")
                + "。重試後仍不合格 → 整份 PDF 零寫入"
            )
            error = figure_extract.FigureExtractionError(message)
            error.results = results
            error.failed = _failed_result(candidate, kind, message)
            raise error from exc
        except figure_extract.FigureExtractionError as exc:
            exc.results = results
            exc.failed = _failed_result(candidate, kind, str(exc))
            raise

        sequence = per_page.get(page, 0) + 1
        per_page[page] = sequence
        results.append(replace(result, figure_index=sequence))

    progress(
        f"[figure] 完成 {len(results)} 張（VL 呼叫 {counters['vl_calls']} 次，"
        f"重複影像省下 {counters['vl_calls_saved']} 次）"
    )
    return results
