#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""figure_candidates — PDF 圖面候選偵測、原始 evidence 採集、取像與 preflight（T3）。

契約：`wf/CONTRACT.md` §6.3 / §12.4 / §13.1–§13.2；workflow §4 Step 2、§5、§8。

北極星是 **verified-or-abstain**，落在本模組就是三句話：

1. **沒有證據就不宣稱。** 任一 evidence channel 取不到只記進
   `PageEvidence.unavailable`，**不 raise、不印任何東西**（`RAG.extract_pdf` 的既有測試
   斷言 `"[WARN]" not in out`）。整頁的結構性 channel 全滅時，那一頁不得產生任何
   table/terminal 候選——這正是既有 `tests/test_rag_pdf_ingest.py` 用
   `types.SimpleNamespace(page_count=..., close=...)` 假裝 pymupdf Document 仍能通過的依據。
2. **不搶 legacy picture lane 的東西**（契約 §13.1，使用者已拍板）。structured lane 只收
   「有結構性原生證據」的候選：原生 markdown 表格（`class=table` + 合法 `pos`）、
   `find_tables` 幾何、ruled-line grid、對齊的 word band（無框線 memory map）、向量文字 log。
   `kind == KIND_DIAGRAM` 與純 raster 一律延後給既有 `_plan_pdf_figure_jobs` lane，並記進
   `stats["deferred_to_legacy_lane"]`。**已知範圍限制**：PDF 內的純 raster 終端機截圖與
   掃描頁表格本輪仍走自由文字 VL lane（`origin="diagram"`），拿不到 `▯` / 逐格證據 / strict gate。
3. **不無聲截斷。** 超過任何上限都進 `over_budget` 並由 `check_preflight()` fail-loud；
   被丟棄的候選逐筆列進 `stats["dropped_candidates"]`。

座標系（實測 pymupdf 1.28.0 / pymupdf4llm 1.28.0，全部有測試釘住）：

| channel | 座標系 |
|---|---|
| `page.get_text("words")` | **unrotated**、cropbox 相對 |
| `page.get_image_info()` | **unrotated**、cropbox 相對 |
| `page.cluster_drawings()` / `get_drawings()` | **unrotated**、cropbox 相對 |
| `page.find_tables()` | **unrotated**（且 rotation != 0 時上游恆回 0 個表） |
| `page.rect` | **rotated / display** |
| pymupdf4llm `page_boxes[].bbox` | **rotated / display**（rotation + cropbox 併發時上游疑似錯亂） |

因此 canonical 座標一律是 **unrotated、cropbox 相對** 空間：
`page_rect = Rect(page.rect) * page.derotation_matrix`。`page_boxes` 的幾何要先經
`_calibrate_page_box_space()` 用 `image_info` 做一對一驗證才敢用；對不上就標
`page_boxes_geometry:unalignable`，**只用 `pos` / `class`，不用它的 bbox**。
render 時再把 unrotated bbox 乘回 `page.rotation_matrix` 得到 `get_pixmap(clip=)` 要的 display rect。

已知上游限制（釘版 1.28.0，不盲升版；升版時這些 slug 就是漂移偵測點）：

- `rotation != 0` → `find_tables()` 三策略恆回 0，pymupdf4llm 的 markdown `text` 恆為空。
- `find_tables(strategy="text")` 可能回退化 Table：`t.bbox` 與 `t.col_count` **會 raise
  `ValueError`**。每個屬性都各自 try/except，退化項標 `degenerate=True` 且永不成為候選來源。
- `Table.extract()` 會把 `0x4000_0100` 讀成 `'0x4000 0100\n_'`。它只存成
  `extract_unreliable_underscore=True` 的 evidence channel，**絕不當 canonical**
  （workflow §3「單次 `find_tables().extract()` 不算」的實證）。
- rotation + cropbox 併發時 `page_boxes[].bbox` 與任何可證明的 transform 都對不上。

本模組**完全不寫檔、不呼叫 VL、不算 embedding、不碰 KB**（純讀 + 記憶體），
因此不是 AGENTS.md §3 的安全模組，不需登記 `tests/test_smoke_gate.py::SAFETY_MODULES`。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config

# ── figure_extract 一律「呼叫時」才取用 ────────────────────────────────
# 門面（figure_extract）用 PEP 562 lazy `__getattr__` re-export 本模組的名稱。即使如此，
# 本模組**不在 module 層 import figure_extract**：`import figure_candidates` 若在
# `import figure_extract` 之前發生，module 層 import 會在 figure_extract 尚未定義完常數時
# 取值。函式內 import 走的是已建好的 sys.modules 快取，兩個方向都安全。
# **禁止** `from figure_extract import X`（會在 import 期綁值）。


def _fx():
    """取得 `figure_extract`（呼叫時 import，避免 import 期循環）。"""
    import figure_extract

    return figure_extract


# ============================================================
# 1. 偵測門檻（模組級；契約 §13.2：config.FIGURE_* 是預算旋鈕，偵測門檻是實作細節）
# ============================================================
# 全部可被測試 monkeypatch。**沒有任何一項與顏色/亮度/暗底比例有關**——白底、淺色主題、
# 紙本列印的 terminal 與無框線表都是實際目標（workflow §4 Step 2 明令禁用
# 「暗底 >60%」「框內文字稀疏」這類必要條件）。
MIN_CANDIDATE_SIDE_PT = 24.0        # 候選短邊下限（pt）
MIN_CANDIDATE_AREA_PT2 = 2000.0     # 候選面積下限（pt²）
MIN_GRID_ROWS = 3                   # word band 群的最少帶數
MIN_GRID_COLS = 2                   # 一帶內對齊欄的最少數量
GRID_COL_TOL_PT = 4.0               # 欄左緣對齊容忍（pt）
COL_SUPPORT_MIN = 0.6               # 「有對齊欄」的 band 比例下限（升格用）
BAND_TOL_RATIO = 0.6                # band 合併容忍 = 比例 × 字高
BLOCK_GAP_RATIO = 1.8               # 垂直間隙 > 比例 × 中位帶高 → 切成不同 block
RULED_LINE_MIN_LEN_PT = 40.0        # 視為表格框線的最短長度
RULED_LINE_MAX_THICK_PT = 2.5       # 視為框線的最大厚度
MIN_RULED_H_LINES = 3               # grid 的水平線下限
MIN_RULED_V_LINES = 2               # grid 的垂直線下限
NEAR_ZERO_TEXT_CHARS = 20           # 與 RAG.PDF_PAGE_TEXT_NEAR_ZERO_CHARS 同義（不 import RAG）
# fusion 是 O(n²) pairwise IoU；這兩個是**資源邊界**（在 clustering 之前就檢查），
# 不是 config.FIGURE_MAX_CANDIDATES_* 那種候選上限。超限 → 該頁整頁 abstain + fail-loud。
MAX_RAW_REGIONS_PER_PAGE = 400
MAX_PROMOTING_REGIONS_PER_PAGE = 120
MAX_RAW_DRAWINGS_PER_PAGE = 20000   # `cluster_drawings()` 之前的原始圖元上限
PAGE_BOX_ALIGN_MIN_IOU = 0.60       # page_box ↔ image_info 一對一配對的最小 IoU
PAGE_BOX_ALIGN_MIN_MATCHED = 0.75   # 必須配對成功的 page_box 比例
PAGE_BOX_ALIGN_AMBIGUOUS = 0.05     # 兩個 transform 分數差距小於此 → abstain
WORD_ASSIGN_DILATE_PT = 2.0         # bbox 邊界字外擴（**不用 clip 截斷**，邊界字丟了救不回）
MONO_CV_SCALE = 0.35                # char-advance 變異係數 → mono 分數的尺度
TERMINAL_SIGNAL_LINES = 2           # 升格為 terminal 訊號所需的 prompt/timestamp 行數
DEFAULT_GLYPH_PT = 9.0              # 沒有 word band 時的字高預設（估算用）
PATCH_ROUNDING_MARGIN = 1           # token 估算的保守 patch 邊界（pixmap rounding）

# terminal 版面訊號（**正向**證據，不是排除條件）
_PROMPT_LINE_RE = re.compile(r"^\s*(?:[$#>]|PS[ >]|[A-Za-z0-9._-]+[@:][^\s]*[$#>])\s")
_TIMESTAMP_LINE_RE = re.compile(r"^\s*[\[(]\s*\d+[.:]\d+")
_LOGLEVEL_LINE_RE = re.compile(
    r"\b(?:DEBUG|INFO|WARN|WARNING|ERROR|FATAL|TRACE|CRITICAL)\b")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_HEXISH_RE = re.compile(r"0[xX][0-9A-Fa-f][0-9A-Fa-f_]*|\[\s*\d+\s*:\s*\d+\s*\]")

INVALID_PAGE = 0                     # `_page_number()` 的 invalid sentinel
_TABLE_STRATEGIES = ("lines", "lines_strict", "text")
# terminal 的 native lane 只認得這些「整塊原文」的 page_box class（契約 §15.1）。
# **刻意排除 `list-item`**：實測 pymupdf4llm 會把每一行 log 變成一個 `list-item`，
# markdown 是 ``- `$ dmesg | tail` `` 這種**加過裝飾**的逐行輸出，而且一個候選會對到
# 五個 pos —— 那不是「一塊 pos 支撐的 raw markdown 文字」。
_TEXT_BEARING_CLASSES = frozenset({"text", "code", "table", "section-header"})
# fusion 的來源優先序（也決定 stats / signals 內 channel 的穩定排序）
_CHANNEL_ORDER = (
    "find_tables:lines",
    "find_tables:lines_strict",
    "find_tables:text",
    "page_boxes:table",
    "drawings:ruled",
    "words:block",
    "image_info:raster",
    "page_boxes:picture",
    "page:fallback",
)
# 「結構性原生證據」——只有這些 channel 能讓一個區域升格成 structured 候選（契約 §13.1）
_PROMOTING_CHANNELS = frozenset({
    "find_tables:lines", "find_tables:lines_strict", "find_tables:text",
    "page_boxes:table", "drawings:ruled", "words:block",
})


# ============================================================
# 2. 凍結資料結構（契約 §6.3 + §12.4 + §13.2）
# ============================================================
@dataclass(frozen=True)
class PageEvidence:
    """一頁的原始 evidence（**任何 strip / normalize 之前**）。

    `unavailable` 是「這個 channel 取不到」的唯一表示法，元素形如 `"words:no_page"`，
    冒號後是**穩定 slug**（例外型別名或原因），不含任何頁面文字（NDA）。
    `words == []` 且 `"words:..." not in unavailable` 才代表「這頁真的沒有字」。

    尾端帶預設值的加欄（向後相容，位置建構不受影響）：

    - `fallback` —— 契約 §13.2 已核准（近乎無文字頁的整頁 fallback）。
    - `overlays` —— 契約 **§16.1 已正式納入**（第四個尾端加欄）。它存 annotation /
      widget 幾何與原始 drawing rect，是計畫審核 BLOCKER 13（「純 raster 判定必須排除
      annotation/widget」）與 local review BLOCKER #1（「overlay channel 失敗時不得宣稱
      純 raster」）唯一可行的落點：這些資料必須在**同一次開檔**採到並帶到候選建構階段。
      屬 `figure_candidates` 內部使用（T4/T5/T7 皆未讀取）。
    """

    page: int
    raw_markdown: str
    page_boxes: list[dict]
    words: list[tuple]
    image_info: list[dict]
    tables: dict[str, list]
    drawing_clusters: list[tuple]
    page_rect: tuple
    rotation: int
    unavailable: list[str]
    fallback: dict = field(default_factory=dict)
    overlays: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Candidate:
    """一個 structured 候選（**physical**：重複影像不合併，只共享 VL 計算）。

    `bbox` / `page_rect` 一律是 **unrotated、cropbox 相對**空間。
    `kind` 只可能是 `table` / `terminal` / `unknown`——`unknown` **只**表示
    「table 與 terminal 分數接近」（契約 §6.4 的 dual pass），不表示「不知道是不是圖」；
    `diagram` 與純 raster 在輸出前就已延後給 legacy lane（契約 §13.1）。
    """

    index: int
    page: int
    bbox: tuple
    page_rect: tuple
    kind_scores: dict
    kind: str
    signals: dict
    reasons: list[str]
    signature: str
    native_table: dict | None
    occurrences: list[dict]
    asset_xref: int | None
    asset_digest: str
    figure_id: str
    document_id: str


@dataclass(frozen=True)
class Variant:
    """實際會送模型的一張影像。

    ⚠️ `png` 的欄名維持契約 §6.3 不變，但 **`variant_id == "raster"` 時裝的是
    `Document.extract_image(xref)` 的原始 binary，可能不是 PNG**；真實型別看 `mime`
    （契約 §12.3 / §13.2）。其餘 variant 一律是 `Pixmap.tobytes("png")`。

    `overlap_px` = 與**前一張 tile** 重疊的像素高度；第一張與未 tile 一律 0。
    `tile_index` 0 = 未 tile（此時 `tile_total == 1`）。
    """

    figure_id: str
    variant_id: str
    png: bytes
    width: int
    height: int
    bbox: tuple
    tile_index: int
    tile_total: int
    overlap_px: int
    est_image_tokens: int
    digest: str
    stitch: dict = field(default_factory=dict)
    mime: str = "image/png"


@dataclass(frozen=True)
class FigurePlan:
    """整份文件的候選計畫 + preflight 數字。

    `preflight` 的七個鍵是凍結契約（`candidates` / `tiles` / `vl_calls_min` /
    `vl_calls_max` / `image_tokens_est` / `pages` / `native_tables`）；附加鍵可加。
    `over_budget` 非空 → `check_preflight()` 會 raise `FigureBudgetError`。
    """

    document_id: str
    candidates: list[Candidate]
    page_evidence: dict
    stats: dict
    preflight: dict
    over_budget: list[str]


# ============================================================
# 3. 幾何與型別 helper（全部 JSON-safe，且拒絕靜默改值）
# ============================================================
def _is_real_int(value) -> bool:
    """真正的 int（`bool` 不算、`"3"` 不算、`1.9` 不算）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _finite(value) -> float | None:
    """`value` 是有限的實數才回 float，否則 None（NaN / Inf / str / bool 全擋）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _as_bbox(value) -> tuple[float, float, float, float] | None:
    """把任意 bbox-like 轉成有限、正向的 `(x0, y0, x1, y1)`；不合格回 None。

    **不做**自動 normalize（反向框不翻正）：反向框代表上游給了壞資料，
    翻正等於默默接受一個我們無法解釋的區域。
    """
    if value is None:
        return None
    try:
        items = tuple(value)
    except TypeError:
        return None
    if len(items) != 4:
        return None
    coords = [_finite(v) for v in items]
    if any(c is None for c in coords):
        return None
    x0, y0, x1, y1 = coords  # type: ignore[misc]
    if not (x1 > x0 and y1 > y0):
        return None
    return (x0, y0, x1, y1)


def _as_thin_rect(value) -> tuple[float, float, float, float] | None:
    """允許**零寬 / 零高**的矩形（框線就是這種：`(72,90,72,156)`）。

    `_as_bbox()` 要求正向且非退化，用在候選 bbox 是對的；但拿它讀 `get_drawings()`
    的 rect 會把所有直線整批丟掉——framed table 的 grid 訊號與 raster overlay 偵測
    就會**無聲失效**。
    """
    if value is None:
        return None
    try:
        items = tuple(value)
    except TypeError:
        return None
    if len(items) != 4:
        return None
    coords = [_finite(v) for v in items]
    if any(c is None for c in coords):
        return None
    x0, y0, x1, y1 = coords  # type: ignore[misc]
    if x1 < x0 or y1 < y0:
        return None
    return (x0, y0, x1, y1)


def _rect_of(obj, *, thin: bool = False):
    """pymupdf `Rect` / 4-tuple → JSON-safe tuple（`thin=True` 允許零寬/零高）。"""
    convert = _as_thin_rect if thin else _as_bbox
    if obj is None:
        return None
    for attr in ("x0", "y0", "x1", "y1"):
        if not hasattr(obj, attr):
            return convert(obj)
    return convert((obj.x0, obj.y0, obj.x1, obj.y1))


def _overlaps(a, b) -> bool:
    """相交判定（**含**零寬/零高的線：`_intersection()` 對線一律回 None）。"""
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _rect_within(rect, box, tol: float = 1.0) -> bool:
    """`rect` 是否落在 `box` 內（容忍 `tol`）；對線也成立。"""
    return (rect[0] >= box[0] - tol and rect[1] >= box[1] - tol
            and rect[2] <= box[2] + tol and rect[3] <= box[3] + tol)


def _area(box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


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
    if union <= 0:
        return 0.0
    return inter_area / union


def _coverage(inner, outer) -> float:
    """`inner` 有多少比例落在 `outer` 內（0..1）。"""
    inter = _intersection(inner, outer)
    if inter is None or _area(inner) <= 0:
        return 0.0
    return _area(inter) / _area(inner)


def _union_box(boxes):
    xs0 = min(b[0] for b in boxes)
    ys0 = min(b[1] for b in boxes)
    xs1 = max(b[2] for b in boxes)
    ys1 = max(b[3] for b in boxes)
    return (xs0, ys0, xs1, ys1)


def _round_box(box, digits: int = 2):
    return tuple(round(float(v), digits) + 0.0 for v in box)


def _slug(exc: BaseException) -> str:
    """例外 → 穩定 slug（**只取型別名**：例外訊息可能含 NDA 內容）。"""
    return type(exc).__name__


def _median(values: Sequence[float], default: float = 0.0) -> float:
    data = sorted(float(v) for v in values)
    if not data:
        return default
    mid = len(data) // 2
    if len(data) % 2:
        return data[mid]
    return (data[mid - 1] + data[mid]) / 2.0


def _channel_rank(channel: str) -> int:
    try:
        return _CHANNEL_ORDER.index(channel)
    except ValueError:
        return len(_CHANNEL_ORDER)


# ============================================================
# 4. 座標系：rotation / cropbox / page_box 校準
# ============================================================
def _page_geometry(page) -> dict:
    """讀出一頁的座標系資訊；任何一項讀不到就整包放棄（呼叫端記 `page_rect:<slug>`）。

    **刻意不 import pymupdf**：規劃／preflight 這條路徑因此零外部相依，
    只有 `render_candidate_variants()` 真的取像時才需要 pymupdf。

    `page_rect` = `Rect(page.rect) * page.derotation_matrix`：這是 **unrotated、
    cropbox 相對**的頁矩形，也是 words / image_info / drawings / find_tables 共用的空間。
    """
    rect = _rect_of(page.rect)
    if rect is None:
        raise ValueError("page.rect 退化")
    derot = tuple(float(v) for v in page.derotation_matrix)
    rot = tuple(float(v) for v in page.rotation_matrix)
    if len(derot) != 6 or len(rot) != 6:
        raise ValueError("rotation matrix 形狀不對")
    unrotated = _as_bbox(_apply_matrix(rect, derot))
    if unrotated is None:
        raise ValueError("derotation 後的 page rect 退化")
    return {
        "rotation": int(page.rotation or 0),
        "page_rect": _round_box(unrotated),
        "display_rect": _round_box(rect),
        "rotation_matrix": rot,
        "derotation_matrix": derot,
    }


def _apply_matrix(box, matrix):
    """4-tuple bbox × pymupdf Matrix 6-tuple → 正規化後的 4-tuple。"""
    a, b, c, d, e, f = matrix
    xs = []
    ys = []
    for x, y in ((box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3])):
        xs.append(a * x + c * y + e)
        ys.append(b * x + d * y + f)
    return (min(xs), min(ys), max(xs), max(ys))


def _greedy_one_to_one(boxes_a, boxes_b, min_iou: float) -> tuple[int, float]:
    """一對一貪婪配對，回 `(配對數, 配對 IoU 平均)`。

    **不是**「IoU 總和」：總和會讓一個大框在錯誤 transform 下也拿到高分
    （審核 BLOCKER 7）。一對一 + 平均值可以擋掉這種偽對齊。
    """
    pairs = []
    for i, a in enumerate(boxes_a):
        for j, b in enumerate(boxes_b):
            value = _iou(a, b)
            if value >= min_iou:
                pairs.append((value, i, j))
    pairs.sort(key=lambda item: (-item[0], item[1], item[2]))
    used_a: set[int] = set()
    used_b: set[int] = set()
    matched = []
    for value, i, j in pairs:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        matched.append(value)
    if not matched:
        return 0, 0.0
    return len(matched), sum(matched) / len(matched)


def _calibrate_page_box_space(page_boxes, image_info, word_blocks, geom) -> str:
    """判定 pymupdf4llm `page_boxes[].bbox` 屬於哪個座標系。

    回 `"identity"` / `"derotate"` / `"unaligned"`。

    - `rotation == 0`：identity 與 derotate 是同一個矩陣，identity **可證明**成立，直接回。
    - `rotation != 0`：拿 `class=picture` 的 page_box 對 `image_info`（同一批物件的
      ground truth）做一對一配對；沒有 picture 就退而用 `class=text` 對 word block。
      兩個 transform 都通過且分數接近 → **abstain**（`unaligned`），寧可只用 `pos`/`class`。
    - 實測：純 rotation 時 derotate 完全對得上；rotation + cropbox 併發時**兩者都對不上**
      （上游疑似 bug），這條路徑就會回 `unaligned`。
    """
    rotation = int(geom.get("rotation") or 0)
    if rotation == 0:
        return "identity"

    anchors = [b for b in (image_info or []) if b is not None]
    sources = [box for box in page_boxes if box.get("class") == "picture" and box.get("_bbox_raw")]
    if not anchors or not sources:
        # 沒有 picture（例如旋轉的**純表格頁**）→ 改用文字類 page_box 對 word block。
        # 只認 picture 的話，這種頁會被無條件標成 unaligned，原生表格就白白丟掉。
        anchors = [b for b in (word_blocks or []) if b is not None] + \
                  [b for b in (image_info or []) if b is not None]
        sources = [box for box in page_boxes
                   if box.get("class") in ("text", "table", "section-header")
                   and box.get("_bbox_raw")]
    if not anchors or not sources:
        # rotation != 0 卻沒有任何可驗證的 anchor：不可證明 → abstain
        return "unaligned"

    raw = [box["_bbox_raw"] for box in sources]
    scored: dict[str, tuple[int, float]] = {}
    for mode, matrix in (("identity", None), ("derotate", geom.get("derotation_matrix"))):
        if mode == "derotate" and not matrix:
            continue
        transformed = [b if matrix is None else _apply_matrix(b, matrix) for b in raw]
        matched, mean_iou = _greedy_one_to_one(transformed, anchors, PAGE_BOX_ALIGN_MIN_IOU)
        if matched / max(1, len(raw)) >= PAGE_BOX_ALIGN_MIN_MATCHED:
            scored[mode] = (matched, mean_iou)
    if not scored:
        return "unaligned"
    if len(scored) == 2:
        best, second = sorted(scored.values(), key=lambda v: -v[1])
        if abs(best[1] - second[1]) < PAGE_BOX_ALIGN_AMBIGUOUS:
            return "unaligned"
    return max(scored.items(), key=lambda item: (item[1][1], item[0]))[0]


# ============================================================
# 5. evidence 採集
# ============================================================
def _safe(channel: str, unavailable: list[str], fn: Callable[[], Any], default):
    """跑一個 channel；失敗只記 slug，**永不 raise**。"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - channel 降級是設計目標
        unavailable.append(f"{channel}:{_slug(exc)}")
        return default


def _normalize_page_boxes(page_info: dict, raw_len: int, unavailable: list[str]) -> list[dict]:
    """複製並正規化 `page_boxes`。

    **複製**上游 dict（`RAG.extract_pdf_document` 之後還要用同一批 `pages`，就地加鍵會污染它）。
    原有 key 逐字保留；衍生 key 一律 `_` 前綴：

    - `_bbox_raw`：上游 bbox（display 空間），不合法 → None
    - `_bbox_unrotated`：校準後的 unrotated bbox（`_space == "unaligned"` 時為 None）
    - `_pos`：**只接受真正的 int** 且 `0 <= start < end <= len(raw_markdown)`；
      `1.9` / `"3"` / `True` 一律視為缺 `pos`（審核 BLOCKER 8：靜默改值會做出看似合法的錯 offset）
    - `_space`：`identity` / `derotate` / `unaligned`
    """
    boxes: list[dict] = []
    raw_boxes = page_info.get("page_boxes")
    if raw_boxes is None:
        return boxes
    if not isinstance(raw_boxes, (list, tuple)):
        unavailable.append("page_boxes:TypeError")
        return boxes
    for ordinal, entry in enumerate(raw_boxes):
        if not isinstance(entry, dict):
            continue
        box = dict(entry)
        box["_ordinal"] = ordinal
        box["_bbox_raw"] = _as_bbox(entry.get("bbox"))
        box["_bbox_unrotated"] = None
        box["_space"] = "unaligned"
        pos = entry.get("pos")
        parsed = None
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            start, end = pos
            if _is_real_int(start) and _is_real_int(end) and 0 <= start < end <= raw_len:
                parsed = (int(start), int(end))
        box["_pos"] = parsed
        boxes.append(box)
    return boxes


def _harvest_tables(page, unavailable: list[str], degenerate: list[dict]) -> dict[str, list]:
    """三策略 `find_tables()`。**每個屬性各自 try/except**。

    退化項（`bbox` / `col_count` raise，或列數欄數不足）留在 evidence 裡但標
    `degenerate=True`，**永遠不會成為候選來源**——實測 `strategy="text"` 在一般
    段落頁就會回一個 `t.bbox` 直接 raise `ValueError` 的 Table。

    每個 entry 都附 `bbox` 與 `geometry`（含**每個 cell 的 bbox**），
    verifier 才挑得出對應候選的那一張（契約 §12.4）。
    """
    result: dict[str, list] = {name: [] for name in _TABLE_STRATEGIES}
    for strategy in _TABLE_STRATEGIES:
        try:
            finder = page.find_tables(strategy=strategy)
            tables = list(finder.tables)
        except Exception as exc:  # noqa: BLE001
            unavailable.append(f"find_tables:{strategy}:{_slug(exc)}")
            continue
        for ordinal, table in enumerate(tables):
            entry = _table_entry(table, strategy, ordinal)
            result[strategy].append(entry)
            if entry["degenerate"]:
                degenerate.append({
                    "strategy": strategy,
                    "ordinal": ordinal,
                    "errors": list(entry["errors"]),
                })
    return result


def _table_entry(table, strategy: str, ordinal: int) -> dict:
    """單一 `Table` → JSON-safe evidence entry（每個屬性各自 guard）。"""
    errors: list[str] = []

    def _attr(name: str, default=None):
        try:
            value = getattr(table, name)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}:{_slug(exc)}")
            return default
        return value

    bbox = _as_bbox(_attr("bbox"))
    if bbox is None and "bbox" not in " ".join(errors):
        errors.append("bbox:invalid")
    row_count = _attr("row_count")
    col_count = _attr("col_count")
    row_count = row_count if _is_real_int(row_count) else None
    col_count = col_count if _is_real_int(col_count) else None

    header_names = None
    header_bbox = None
    header_external = None
    header = _attr("header")
    if header is not None:
        try:
            header_names = [str(v) if v is not None else "" for v in (header.names or [])]
        except Exception as exc:  # noqa: BLE001
            errors.append(f"header.names:{_slug(exc)}")
        header_bbox = _as_bbox(getattr(header, "bbox", None))
        raw_external = getattr(header, "external", None)
        header_external = bool(raw_external) if raw_external is not None else None

    cells: list[list] = []
    rows_y: list[float] = []
    cols_x: list[float] = []
    try:
        for row in table.rows:
            row_cells = []
            for cell in row.cells:
                cell_box = _as_bbox(cell)
                row_cells.append(_round_box(cell_box) if cell_box else None)
                if cell_box:
                    cols_x.extend([cell_box[0], cell_box[2]])
                    rows_y.extend([cell_box[1], cell_box[3]])
            cells.append(row_cells)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"rows:{_slug(exc)}")

    extract_raw = None
    try:
        extract_raw = [[("" if v is None else str(v)) for v in row] for row in table.extract()]
    except Exception as exc:  # noqa: BLE001
        errors.append(f"extract:{_slug(exc)}")

    # cell matrix 必須與 row/col count **完全一致**且每格都有合法 bbox。
    # `cells=[[None, None], ...]` 或 ragged geometry 過關的話，`native_verified` 的
    # `cell_geometry` 會建立在不存在的幾何上（local review BLOCKER #7）。
    cells_ok = bool(cells) and _is_real_int(row_count) and _is_real_int(col_count)
    if cells_ok:
        cells_ok = len(cells) == row_count and all(
            len(row) == col_count and all(c is not None for c in row) for row in cells)
    if not cells_ok and "cells" not in " ".join(errors):
        errors.append("cells:incomplete_geometry")
    degenerate = (
        bbox is None
        or not _is_real_int(row_count) or row_count < 2
        or not _is_real_int(col_count) or col_count < 2
        or not cells_ok
    )
    geometry = {
        "table_bbox": _round_box(bbox) if bbox else None,
        "row_count": row_count,
        "col_count": col_count,
        "header_names": header_names,
        "header_bbox": _round_box(header_bbox) if header_bbox else None,
        "header_external": header_external,
        "rows": sorted({round(v, 2) + 0.0 for v in rows_y}),
        "cols": sorted({round(v, 2) + 0.0 for v in cols_x}),
        "cells": cells,
        "extract_raw": extract_raw,
        # ⚠️ 實測 `Table.extract()` 會把 `0x4000_0100` 讀成 `'0x4000 0100\n_'`。
        # 它只是 evidence channel 之一，**絕不當 canonical**（workflow §3）。
        "extract_unreliable_underscore": True,
    }
    return {
        "strategy": strategy,
        "ordinal": ordinal,
        "bbox": _round_box(bbox) if bbox else None,
        "geometry": geometry,
        "degenerate": bool(degenerate),
        "errors": errors,
    }


def _harvest_image_info(page) -> list[dict]:
    """`get_image_info(hashes=True, xrefs=True)`；**每個顯示 occurrence 都保留**。

    `digest` 是 bytes（不可 JSON 序列化）→ 轉成 `digest_hex` 並移除原欄位（審核 BLOCKER 19）。
    """
    entries = []
    for ordinal, info in enumerate(page.get_image_info(hashes=True, xrefs=True) or []):
        if not isinstance(info, dict):
            continue
        item = {k: v for k, v in info.items() if k != "digest"}
        digest = info.get("digest")
        item["digest_hex"] = digest.hex() if isinstance(digest, (bytes, bytearray)) else ""
        item["bbox"] = _round_box(_as_bbox(info.get("bbox")) or (0.0, 0.0, 0.0, 0.0))
        transform = info.get("transform")
        item["transform"] = tuple(float(v) for v in transform) if transform else None
        item["ordinal"] = ordinal
        item["xref"] = info.get("xref") if _is_real_int(info.get("xref")) else None
        item["has_mask"] = bool(info.get("has-mask"))
        entries.append(item)
    return entries


def _harvest_overlays(page, unavailable: list[str]) -> dict:
    """annotation / widget 幾何——純 raster 判定必須排除它們。

    **逐 item guard**：單一 annotation 的 `rect` getter 丟例外也不得讓 harvest raise
    （契約 §6.3「任一 channel 取不到 → 記 unavailable，不 raise」）。抓不到幾何的
    item 會讓整個 channel 標成不完整（`<key>:partial`），因為「我們知道有覆蓋物、
    但不知道它在哪」比「沒有覆蓋物」危險得多——純 raster 判定會據此強制走 crop。
    """
    overlays: dict = {"annots": [], "widgets": []}
    for key in ("annots", "widgets"):
        try:
            items = list(getattr(page, key)() or [])
        except Exception as exc:  # noqa: BLE001
            unavailable.append(f"{key}:{_slug(exc)}")
            continue
        boxes = []
        partial = False
        for item in items:
            try:
                box = _rect_of(getattr(item, "rect", None), thin=True)
            except Exception:  # noqa: BLE001 - 單一 item 壞掉不得拖垮整頁
                partial = True
                continue
            if box:
                boxes.append(_round_box(box))
            else:
                partial = True
        overlays[key] = boxes
        if partial:
            unavailable.append(f"{key}:partial")
    return overlays


def harvest_page_evidence(page_info: dict, *, pdf_doc=None, page_index: int | None = None,
                          page_number: int | None = None) -> PageEvidence:
    """採集一頁的全部原始 evidence。

    `page_info` 是 `pymupdf4llm.to_markdown(page_chunks=True)` 的 page dict；
    `pdf_doc` 是 pymupdf `Document`（可為 None，或任何「什麼 API 都沒有」的假物件）。

    **一定不 raise。** 任一 channel 取不到 → `unavailable` 多一筆 `"<channel>:<slug>"`。
    `raw_markdown` 保留 **strip / normalize 之前**的原文（`pos` 的座標系就是它）。
    """
    unavailable: list[str] = []
    raw_markdown = page_info.get("text")
    if not isinstance(raw_markdown, str):
        unavailable.append("raw_markdown:missing")
        raw_markdown = ""

    page_boxes = _normalize_page_boxes(page_info, len(raw_markdown), unavailable)
    page_num = int(page_number if page_number is not None else _page_number(page_info.get("metadata")))

    words: list[tuple] = []
    image_info: list[dict] = []
    tables: dict[str, list] = {name: [] for name in _TABLE_STRATEGIES}
    clusters: list[tuple] = []
    drawing_rects: list[tuple] = []
    overlays: dict = {"annots": [], "widgets": []}
    page_rect: tuple = (0.0, 0.0, 0.0, 0.0)
    rotation = 0
    degenerate_tables: list[dict] = []

    native_channels = ("page_rect", "words", "image_info", "find_tables", "drawings")
    page = None
    geom: dict = {}
    if pdf_doc is None:
        for channel in native_channels:
            unavailable.append(f"{channel}:no_document")
    else:
        idx = page_index if page_index is not None else page_num - 1
        if idx < 0:
            # 頁碼無效（`_page_number()` 回 INVALID_PAGE）而呼叫端又沒給 physical index：
            # `pdf_doc[-1]` 會安靜地拿到**最後一頁**，等於把 A 頁 evidence 配到 B 頁像素。
            unavailable.append("page_object:invalid_page_number")
            page = None
        else:
            page = _safe("page_object", unavailable, lambda: pdf_doc[idx], None)
        if page is None:
            # 取不到 page 物件（例如測試用的假 Document）→ **每一個** native channel 都要
            # 明確記成不可用，呼叫端才看得出「這頁沒有結構性證據」而不是「這頁沒有表」。
            reason = unavailable[-1].split(":", 1)[1] if unavailable else "no_page"
            for channel in native_channels:
                unavailable.append(f"{channel}:{reason}")

    if page is not None:
        geom = _safe("page_rect", unavailable, lambda: _page_geometry(page), None) or {}
        if geom:
            page_rect = geom["page_rect"]
            rotation = geom["rotation"]
        words = _safe("words", unavailable,
                      lambda: [tuple(w) for w in (page.get_text("words") or [])], [])
        image_info = _safe("image_info", unavailable, lambda: _harvest_image_info(page), [])
        tables = _safe("find_tables", unavailable,
                       lambda: _harvest_tables(page, unavailable, degenerate_tables),
                       {name: [] for name in _TABLE_STRATEGIES})
        raw_drawings = _safe("drawings", unavailable, lambda: list(page.get_drawings() or []), None)
        if raw_drawings is not None and len(raw_drawings) > MAX_RAW_DRAWINGS_PER_PAGE:
            # 微小圓點被切成數萬個元素的病態頁：cluster_drawings 之前就停，
            # 不讓 vector detector 無界擴張（workflow §4 Step 2）。
            unavailable.append("drawings:too_many_items")
            raw_drawings = None
        if raw_drawings is not None:
            drawing_rects = [
                box for box in (_rect_of(d.get("rect"), thin=True)
                                for d in raw_drawings if isinstance(d, dict))
                if box is not None
            ]
            clusters = _safe(
                "drawing_clusters", unavailable,
                lambda: [_round_box(_rect_of(r)) for r in (page.cluster_drawings(drawings=raw_drawings) or [])
                         if _rect_of(r) is not None],
                [])
        overlays = _harvest_overlays(page, unavailable)
        if rotation != 0:
            # 實測上游限制：rotation != 0 時 `find_tables()` 要嘛回 0 個表，要嘛回一組
            # **座標系無法證明**（落在 page_rect 之外）的 bbox。兩種情況都不可當候選來源：
            # evidence 留著，但一律標 degenerate，理由寫死成可搜尋的 slug。
            for name in _TABLE_STRATEGIES:
                for entry in tables.get(name) or []:
                    entry["degenerate"] = True
                    entry["errors"].append("rotated_page_geometry_unprovable")
            if not any(tables[name] for name in _TABLE_STRATEGIES):
                unavailable.append("find_tables:empty_on_rotated_page")

    overlays["drawing_rects"] = drawing_rects

    fallback: dict = {}
    text_chars = len(raw_markdown.strip())
    words_missing = any(u.startswith("words:") for u in unavailable)
    if text_chars < NEAR_ZERO_TEXT_CHARS and (words_missing or not words):
        fallback = {
            "reason": "near_zero_text",
            "bbox": page_rect,
            "text_chars": text_chars,
            "words": len(words),
        }

    if page_rect == (0.0, 0.0, 0.0, 0.0) and not any(u.startswith("page_rect:") for u in unavailable):
        unavailable.append("page_rect:degenerate")

    space = "unaligned"
    if page_boxes:
        word_blocks = [b["bbox"] for b in _word_blocks(words)]
        space = _calibrate_page_box_space(
            page_boxes, [i["bbox"] for i in image_info], word_blocks,
            {"rotation": rotation, "derotation_matrix": geom.get("derotation_matrix")},
        )
        if space == "unaligned":
            unavailable.append("page_boxes_geometry:unalignable")
        for box in page_boxes:
            box["_space"] = space
            raw_box = box.get("_bbox_raw")
            if raw_box is None or space == "unaligned":
                box["_bbox_unrotated"] = None
            elif space == "identity":
                box["_bbox_unrotated"] = _round_box(raw_box)
            else:
                box["_bbox_unrotated"] = _round_box(
                    _apply_matrix(raw_box, geom.get("derotation_matrix") or (1, 0, 0, 1, 0, 0)))

    return PageEvidence(
        page=page_num,
        raw_markdown=raw_markdown,
        page_boxes=page_boxes,
        words=words,
        image_info=image_info,
        tables=tables,
        drawing_clusters=clusters,
        page_rect=page_rect,
        rotation=rotation,
        unavailable=unavailable,
        fallback=fallback,
        overlays=overlays,
    )


def _page_number(meta) -> int:
    """pymupdf4llm 頁碼相容（與 `RAG._pdf_page_number` 同語意）。

    >= 1.x 的 key 是 `page_number`（1-based）；舊版是 `page`（0-based）。

    **只接受非 bool 的原生 int**：`True` / `1.9` / `"1"` 全部回 `INVALID_PAGE`（0）。
    `int(1.9)` 會做出一個看起來合法、實際錯位的頁碼，而頁碼決定 evidence 配到哪一頁的
    像素與 `figure_id`——那是靜默錯配（local review BLOCKER #8）。呼叫端收到
    `INVALID_PAGE` 要把該實體頁記成 mismatch 並 abstain。
    **刻意不 import RAG**（`RAG` → `figure_extract` → 本模組會形成 import 循環）；
    上游頁碼 key 若再變，兩處都要改。
    """
    meta = meta if isinstance(meta, dict) else {}
    if "page_number" in meta:
        value = meta.get("page_number")
        return int(value) if _is_real_int(value) else INVALID_PAGE
    if "page" in meta:
        value = meta.get("page")
        return int(value) + 1 if _is_real_int(value) else INVALID_PAGE
    return INVALID_PAGE


# ============================================================
# 6. 訊號：word band / 欄位對齊 / 框線 / terminal 版面
# ============================================================
def _word_box(word) -> tuple | None:
    return _as_bbox(word[:4]) if len(word) >= 5 else None


def _word_in(word, bbox, dilate: float) -> bool:
    """字是否屬於 bbox。

    中心點在框內 **或** 與外擴框相交都算——**不用 `page.get_text(clip=...)`**：
    `clip` 會先把邊界字整個丟掉，事後過濾救不回（workflow §4 Step 3）。
    """
    box = _word_box(word)
    if box is None:
        return False
    if bbox is None:
        return True
    cx = (box[0] + box[2]) / 2.0
    cy = (box[1] + box[3]) / 2.0
    if bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]:
        return True
    grown = (bbox[0] - dilate, bbox[1] - dilate, bbox[2] + dilate, bbox[3] + dilate)
    return _intersection(box, grown) is not None


def _word_bands(words, bbox=None, dilate: float = WORD_ASSIGN_DILATE_PT) -> list[dict]:
    """把字聚成 baseline 帶（row / line）。

    帶的容忍度用**該帶目前的字高**（`BAND_TOL_RATIO × 高`），不是固定 pt：
    9pt 表格與 18pt 標題不該共用同一個門檻。
    """
    selected = []
    for word in words or []:
        if not _word_in(word, bbox, dilate):
            continue
        box = _word_box(word)
        if box is None:
            continue
        selected.append((box, str(word[4])))
    selected.sort(key=lambda item: ((item[0][1] + item[0][3]) / 2.0, item[0][0]))

    bands: list[dict] = []
    for box, text in selected:
        centre = (box[1] + box[3]) / 2.0
        height = box[3] - box[1]
        if bands:
            current = bands[-1]
            tol = BAND_TOL_RATIO * max(height, current["h"], 1.0)
            if abs(centre - current["yc"]) <= tol:
                current["words"].append((box, text))
                current["y0"] = min(current["y0"], box[1])
                current["y1"] = max(current["y1"], box[3])
                current["x0"] = min(current["x0"], box[0])
                current["x1"] = max(current["x1"], box[2])
                current["h"] = current["y1"] - current["y0"]
                current["yc"] = (current["y0"] + current["y1"]) / 2.0
                continue
        bands.append({
            "y0": box[1], "y1": box[3], "x0": box[0], "x1": box[2],
            "yc": centre, "h": max(height, 1.0), "words": [(box, text)],
        })

    result = []
    for index, band in enumerate(bands, 1):
        band["words"].sort(key=lambda item: item[0][0])
        result.append({
            "index": index,
            "y0": round(band["y0"], 2) + 0.0,
            "y1": round(band["y1"], 2) + 0.0,
            "x0": round(band["x0"], 2) + 0.0,
            "x1": round(band["x1"], 2) + 0.0,
            "yc": round(band["yc"], 2) + 0.0,
            "h": round(band["h"], 2) + 0.0,
            "text": " ".join(text for _box, text in band["words"]),
            "word_x0": [round(box[0], 2) + 0.0 for box, _t in band["words"]],
            "n_words": len(band["words"]),
        })
    return result


def _word_blocks(words) -> list[dict]:
    """把整頁的 band 依垂直間隙切成 block（連續多行的文字區塊）。"""
    bands = _word_bands(words, None)
    if not bands:
        return []
    heights = [b["h"] for b in bands]
    gap_limit = BLOCK_GAP_RATIO * max(_median(heights, DEFAULT_GLYPH_PT), 1.0)
    blocks: list[list[dict]] = [[bands[0]]]
    for band in bands[1:]:
        if band["y0"] - blocks[-1][-1]["y1"] > gap_limit:
            blocks.append([band])
        else:
            blocks[-1].append(band)
    result = []
    for group in blocks:
        result.append({
            "bbox": _round_box(_union_box([(b["x0"], b["y0"], b["x1"], b["y1"]) for b in group])),
            "n_bands": len(group),
        })
    return result


def _column_signal(bands) -> dict:
    """欄位對齊訊號：抓**無框線**的 memory map / register table。

    對所有 band 的字左緣做 1-D 單連結聚類；被 >= 2 個 band 支持的群才算「欄」。
    `col_support` = 至少落在 `MIN_GRID_COLS` 個欄上的 band 比例。
    """
    if not bands:
        return {"columns": [], "col_support": 0.0, "n_columns": 0}
    values = sorted(x for band in bands for x in band["word_x0"])
    clusters: list[list[float]] = []
    for value in values:
        if clusters and value - clusters[-1][-1] <= GRID_COL_TOL_PT:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    columns = []
    for cluster in clusters:
        support = sum(1 for band in bands
                      if any(abs(x - cluster[0]) <= GRID_COL_TOL_PT
                             or (cluster[0] <= x <= cluster[-1]) for x in band["word_x0"]))
        if support >= 2:
            columns.append({"x": round(cluster[0], 2) + 0.0, "support": support})
    hits = 0
    for band in bands:
        matched = sum(1 for col in columns
                      if any(abs(x - col["x"]) <= GRID_COL_TOL_PT for x in band["word_x0"]))
        if matched >= MIN_GRID_COLS:
            hits += 1
    return {
        "columns": columns,
        "n_columns": len(columns),
        "col_support": round(hits / len(bands), 4) + 0.0,
    }


def _ruled_signal(rects, bbox) -> dict:
    """框線訊號：長且薄的水平 / 垂直圖元。`grid` 需要 h>=3 **且** v>=2。

    三條裝飾線（例如頁首分隔線）不會構成 grid——這是審核 BLOCKER 3 要求的正面條件。
    """
    horizontal = 0
    vertical = 0
    other = 0
    for rect in rects or []:
        if bbox is not None and not _rect_within(rect, bbox, tol=2.0):
            continue
        width = rect[2] - rect[0]
        height = rect[3] - rect[1]
        if height <= RULED_LINE_MAX_THICK_PT and width >= RULED_LINE_MIN_LEN_PT:
            horizontal += 1
        elif width <= RULED_LINE_MAX_THICK_PT and height >= RULED_LINE_MIN_LEN_PT:
            vertical += 1
        else:
            other += 1
    total = horizontal + vertical + other
    return {
        "h_lines": horizontal,
        "v_lines": vertical,
        "other_shapes": other,
        "grid": bool(horizontal >= MIN_RULED_H_LINES and vertical >= MIN_RULED_V_LINES),
        "shape_density": round(other / total, 4) + 0.0 if total else 0.0,
    }


def _mono_score(bands) -> float:
    """等寬字形訊號：每個字的「平均字元寬度」變異係數越小越像 monospace。

    只用 `get_text("words")` 就算得出來（不必再抽一次 span/font channel）。
    這是**訊號**不是判準：比例字型的 log 一樣可以靠 prompt / timestamp 得分。
    """
    # 用每個 band 的整體寬度 / 字元數估算平均字元寬（不必再抽一次 span/font channel）
    per_band = []
    for band in bands:
        chars = len(band["text"])
        if chars >= 4 and band["x1"] > band["x0"]:
            per_band.append((band["x1"] - band["x0"]) / chars)
    if len(per_band) < 2:
        return 0.0
    mean = sum(per_band) / len(per_band)
    if mean <= 0:
        return 0.0
    variance = sum((v - mean) ** 2 for v in per_band) / len(per_band)
    cv = math.sqrt(variance) / mean
    return max(0.0, min(1.0, 1.0 - cv / MONO_CV_SCALE))


def _terminal_layout_signal(bands) -> dict:
    """terminal 版面訊號：全部是**正向**證據，沒有任何顏色 / 稀疏度條件。"""
    prompt = 0
    timestamp = 0
    loglevel = 0
    ansi = 0
    ragged = 0
    if not bands:
        return {"prompt_lines": 0, "timestamp_lines": 0, "loglevel_lines": 0,
                "ansi_lines": 0, "right_ragged": 0.0}
    right_max = max(band["x1"] for band in bands)
    span = max(1.0, right_max - min(band["x0"] for band in bands))
    for band in bands:
        text = band["text"]
        if _PROMPT_LINE_RE.match(text):
            prompt += 1
        if _TIMESTAMP_LINE_RE.match(text):
            timestamp += 1
        if _LOGLEVEL_LINE_RE.search(text):
            loglevel += 1
        if _ANSI_RE.search(text):
            ansi += 1
        if (right_max - band["x1"]) / span > 0.05:
            ragged += 1
    return {
        "prompt_lines": prompt,
        "timestamp_lines": timestamp,
        "loglevel_lines": loglevel,
        "ansi_lines": ansi,
        "right_ragged": round(ragged / len(bands), 4) + 0.0,
    }


def _token_signal(bands) -> dict:
    """critical token 訊號（單一定義來自 `figure_extract.critical_tokens`）。"""
    text = "\n".join(band["text"] for band in bands)
    hex_rows = sum(1 for band in bands if _HEXISH_RE.search(band["text"]))
    tokens_table: list[str] = []
    tokens_terminal: list[str] = []
    if text:
        fx = _fx()
        try:
            tokens_table = fx.critical_tokens(text, fx.KIND_TABLE)
            tokens_terminal = fx.critical_tokens(text, fx.KIND_TERMINAL)
        except Exception:  # noqa: BLE001 - 訊號函式不得讓整條 ingest 掛掉
            tokens_table = text.split()
            tokens_terminal = tokens_table
    header_like = bool(
        len(bands) >= 2
        and not _HEXISH_RE.search(bands[0]["text"])
        and any(_HEXISH_RE.search(b["text"]) for b in bands[1:])
    )
    return {
        "hex_rows": hex_rows,
        "n_tokens_table": len(tokens_table),
        "n_tokens_terminal": len(tokens_terminal),
        "header_like": header_like,
    }


# ============================================================
# 7. 候選：來源 → IoU fusion → 升格閘 → kind 評分
# ============================================================
def _region_sources(evidence: PageEvidence) -> list[dict]:
    """把每個 channel 的原始區域攤平成 region list（unrotated 空間）。

    `promoting` 標記就是契約 §13.1 的「結構性原生證據」，且每一條都要求**正面**條件：

    - `find_tables:*`：非退化（>=2 列 >=2 欄、bbox 可讀）
    - `page_boxes:table`：bbox 校準成功 **且** `pos` 合法（審核 BLOCKER 3）
    - `drawings:ruled`：h>=3 **且** v>=2（三條裝飾線不算）
    - `words:block`：>=3 帶，**且**（欄位對齊達 `COL_SUPPORT_MIN` 或有 terminal 版面訊號）
      → 一般多行散文不會升格
    """
    regions: list[dict] = []
    page_rect = evidence.page_rect
    valid_page = _area(page_rect) > 0

    for strategy in _TABLE_STRATEGIES:
        for entry in evidence.tables.get(strategy) or []:
            box = _as_bbox(entry.get("bbox"))
            if box is None or entry.get("degenerate"):
                continue
            regions.append({
                "channel": f"find_tables:{strategy}",
                "bbox": box,
                "promoting": True,
                "detail": {"ordinal": entry["ordinal"], "geometry": entry["geometry"]},
            })

    for box_entry in evidence.page_boxes:
        cls = box_entry.get("class")
        box = _as_bbox(box_entry.get("_bbox_unrotated"))
        if cls == "table":
            if box is None or box_entry.get("_pos") is None:
                continue
            regions.append({
                "channel": "page_boxes:table",
                "bbox": box,
                "promoting": True,
                "detail": {"ordinal": box_entry.get("_ordinal"), "pos": list(box_entry["_pos"])},
            })
        elif cls == "picture" and box is not None:
            regions.append({
                "channel": "page_boxes:picture",
                "bbox": box,
                "promoting": False,
                "detail": {"ordinal": box_entry.get("_ordinal")},
            })

    drawing_rects = (evidence.overlays or {}).get("drawing_rects") or []
    for ordinal, cluster in enumerate(evidence.drawing_clusters or []):
        box = _as_bbox(cluster)
        if box is None:
            continue
        ruled = _ruled_signal(drawing_rects, box)
        if not ruled["grid"]:
            continue
        regions.append({
            "channel": "drawings:ruled",
            "bbox": box,
            "promoting": True,
            "detail": {"ordinal": ordinal, "ruled": ruled},
        })

    for block in _word_blocks(evidence.words):
        box = _as_bbox(block["bbox"])
        if box is None or block["n_bands"] < MIN_GRID_ROWS:
            continue
        bands = _word_bands(evidence.words, box)
        columns = _column_signal(bands)
        layout = _terminal_layout_signal(bands)
        terminal_signal = (
            layout["prompt_lines"] + layout["timestamp_lines"] >= TERMINAL_SIGNAL_LINES
            or layout["ansi_lines"] >= 1
            or (_mono_score(bands) >= 0.6 and layout["loglevel_lines"] >= 1)
        )
        if columns["col_support"] < COL_SUPPORT_MIN and not terminal_signal:
            continue
        regions.append({
            "channel": "words:block",
            "bbox": box,
            "promoting": True,
            "detail": {"n_bands": block["n_bands"],
                       "col_support": columns["col_support"],
                       "terminal_signal": bool(terminal_signal)},
        })

    for entry in evidence.image_info or []:
        box = _as_bbox(entry.get("bbox"))
        if box is None:
            continue
        regions.append({
            "channel": "image_info:raster",
            "bbox": box,
            "promoting": False,
            "detail": {"ordinal": entry["ordinal"], "xref": entry.get("xref"),
                       "digest_hex": entry.get("digest_hex", "")},
        })

    if evidence.fallback and valid_page:
        regions.append({
            "channel": "page:fallback",
            "bbox": page_rect,
            "promoting": False,
            "detail": dict(evidence.fallback),
        })

    regions.sort(key=lambda r: (_channel_rank(r["channel"]), r["bbox"][1], r["bbox"][0]))
    for ordinal, region in enumerate(regions):
        region["region_index"] = ordinal
    return regions


def _components(regions, threshold: float) -> list[list[int]]:
    """promoting region 的 pairwise IoU 圖 → connected components。

    **只用原始 region 的 bbox 兩兩比對**（不重算群 bbox、不迭代）：重算 union 會讓
    相鄰兩張表被橋接，而且結果會依輸入順序改變（審核 BLOCKER 6）。
    """
    parent = list(range(len(regions)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for i in range(len(regions)):
        for j in range(i + 1, len(regions)):
            if _iou(regions[i]["bbox"], regions[j]["bbox"]) >= threshold:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(len(regions)):
        groups.setdefault(find(i), []).append(i)
    return [sorted(v) for _k, v in sorted(groups.items())]


def _attach_non_promoting(components, promoting, others, threshold: float):
    """非 promoting source 只**附掛**到最合適的一個群，不得單獨升格、也不得橋接兩群。"""
    attachments: list[list[dict]] = [[] for _ in components]
    orphans: list[dict] = []
    boxes = [_union_box([promoting[i]["bbox"] for i in comp]) for comp in components]
    for region in others:
        best = -1
        best_score = 0.0
        for index, box in enumerate(boxes):
            score = max(_iou(region["bbox"], box), _coverage(region["bbox"], box))
            if score > best_score:
                best_score = score
                best = index
        if best >= 0 and best_score >= threshold:
            attachments[best].append(region)
        else:
            orphans.append(region)
    return attachments, orphans


def _deferred_entry(page: int, bbox, channels, reason: str) -> dict:
    return {
        "page": int(page),
        "bbox": list(_round_box(bbox)),
        "channels": sorted(set(channels), key=_channel_rank),
        "reason": reason,
    }


def _score_kind(bands, columns, ruled, layout, tokens, native_hit: bool, raster_only: bool) -> dict:
    """kind 分數（0..1，**全部正向證據**）。

    刻意**沒有**任何顏色 / 背景 / 「框內文字稀疏」項：白底與暗底的同一段 log 會走
    完全相同的路徑（workflow §5 terminal ③、§4 Step 2 的禁令）。
    """
    n_bands = len(bands)
    grid = 1.0 if ruled["grid"] else (0.5 if ruled["h_lines"] >= MIN_RULED_H_LINES else 0.0)
    table = (
        0.45 * (1.0 if native_hit else 0.0)
        + 0.20 * grid
        + 0.20 * columns["col_support"]
        + 0.10 * min(1.0, tokens["hex_rows"] / 3.0)
        + 0.05 * (1.0 if tokens["header_like"] else 0.0)
    )
    terminal = (
        0.30 * _mono_score(bands)
        + 0.30 * min(1.0, (layout["prompt_lines"] + layout["timestamp_lines"]) / 3.0)
        + 0.20 * layout["right_ragged"]
        + 0.20 * min(1.0, n_bands / 8.0)
    )
    diagram = (
        0.60 * (1.0 if raster_only else 0.0)
        + 0.40 * ruled["shape_density"]
    )
    fx = _fx()
    return {
        fx.KIND_TABLE: round(min(1.0, table), 4) + 0.0,
        fx.KIND_TERMINAL: round(min(1.0, terminal), 4) + 0.0,
        fx.KIND_DIAGRAM: round(min(1.0, diagram), 4) + 0.0,
    }


def _route_kind(scores) -> tuple[str, list[str]]:
    """kind 路由（契約 §13.1 / 審核 BLOCKER 4）。

    **先**獨立判斷是否延後給 legacy lane，**再**只在 table 與 terminal 之間比大小。
    `KIND_UNKNOWN` 因此只可能是「table 與 terminal 分數接近」，不可能是
    「diagram 第一、terminal 第二且差距小」繞進來的（那是舊版三選一 argmax 的漏洞）。
    """
    fx = _fx()
    table = scores[fx.KIND_TABLE]
    terminal = scores[fx.KIND_TERMINAL]
    diagram = scores[fx.KIND_DIAGRAM]
    if diagram > max(table, terminal):
        return "", ["kind_diagram_legacy_lane"]
    margin = float(config.FIGURE_KIND_MARGIN)
    if abs(table - terminal) < margin:
        return fx.KIND_UNKNOWN, ["kind_margin_below_threshold"]
    return (fx.KIND_TABLE, ["kind_table"]) if table > terminal else (fx.KIND_TERMINAL, ["kind_terminal"])


# 純 raster 判定必須「確定看過」的 channel：任何一個不可用/不完整都強制 page crop。
_OVERLAY_CHANNELS = ("words", "drawings", "annots", "widgets")


def _overlay_channels_complete(evidence: PageEvidence) -> tuple[bool, list[str]]:
    """overlay 相關 channel 是否**全部**明確成功。

    `words` / `drawings` / `annots` / `widgets` 任何一個失敗或只抓到一半，我們就
    不知道這張圖上有沒有看得見的覆蓋物。此時宣稱「純 raster」＝把可能漏掉覆蓋內容的
    原始 binary 送給模型，模型看到的跟頁面上不一樣（local review BLOCKER #1）。
    """
    missing = sorted({
        u.split(":", 1)[0] for u in evidence.unavailable
        if u.split(":", 1)[0] in _OVERLAY_CHANNELS
    })
    return (not missing), missing


def _raster_purity(group_box, rasters, bands, drawing_rects, overlays, pdf_doc, *,
                   channels_complete: bool = True, missing_channels=()) -> dict:
    """能否確定「這個候選就是一張沒有任何覆蓋物的原始 raster」。

    保守到底（審核 BLOCKER 13）：xref 可用、`extract_image` 真的抽得出非空 bytes、
    尺寸與 `image_info` 一致、placement matrix 是正向且軸對齊（無旋轉/鏡射/剪切）、
    無 mask/smask、bbox 與 transform 推得的矩形一致（沒有被裁切）、
    且框內**沒有**任何字、圖元、annotation 或 widget。任何一項不確定 → page crop。
    """
    verdict = {"pure": False, "xref": None, "reason": "", "digest_hex": "", "ext": "",
               "width": 0, "height": 0, "missing_channels": list(missing_channels)}
    if not channels_complete:
        verdict["reason"] = "overlay_channels_incomplete"
        return verdict
    if len(rasters) != 1:
        verdict["reason"] = "not_single_raster" if rasters else "no_raster"
        return verdict
    raster = rasters[0]
    box = _as_bbox(raster["bbox"])
    if box is None or _iou(box, group_box) < 0.95:
        verdict["reason"] = "raster_not_coextensive"
        return verdict
    xref = raster.get("xref")
    if not _is_real_int(xref) or xref <= 0:
        verdict["reason"] = "no_xref"
        return verdict
    if raster.get("has_mask"):
        verdict["reason"] = "has_mask"
        return verdict
    transform = raster.get("transform")
    if not transform or len(transform) != 6:
        verdict["reason"] = "no_transform"
        return verdict
    a, b, c, d, e, f = transform
    if abs(b) > 1e-6 or abs(c) > 1e-6 or a <= 0 or d <= 0:
        verdict["reason"] = "transform_not_axis_aligned"
        return verdict
    placed = (e, f, e + a, f + d)
    if _iou(placed, box) < 0.99:
        verdict["reason"] = "clipped_placement"
        return verdict
    if bands:
        verdict["reason"] = "text_overlay"
        return verdict
    for rect in drawing_rects or []:
        if _overlaps(rect, group_box):
            verdict["reason"] = "vector_overlay"
            return verdict
    for key in ("annots", "widgets"):
        for rect in (overlays or {}).get(key) or []:
            if _overlaps(rect, group_box):
                verdict["reason"] = f"{key}_overlay"
                return verdict
    if pdf_doc is None:
        verdict["reason"] = "no_document"
        return verdict
    try:
        extracted = pdf_doc.extract_image(int(xref))
        payload = extracted.get("image")
        ext = str(extracted.get("ext") or "")
        width = int(extracted.get("width") or 0)
        height = int(extracted.get("height") or 0)
        smask = extracted.get("smask")
    except Exception as exc:  # noqa: BLE001
        verdict["reason"] = f"extract_image:{_slug(exc)}"
        return verdict
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        verdict["reason"] = "empty_image_bytes"
        return verdict
    if smask:
        verdict["reason"] = "has_smask"
        return verdict
    if width <= 0 or height <= 0:
        verdict["reason"] = "unknown_intrinsic_size"
        return verdict
    if (raster.get("width"), raster.get("height")) != (width, height):
        verdict["reason"] = "intrinsic_size_mismatch"
        return verdict
    verdict.update({
        "pure": True, "xref": int(xref), "reason": "pure_raster", "ext": ext,
        "width": width, "height": height,
        "digest_hex": hashlib.sha256(bytes(payload)).hexdigest(),
    })
    return verdict


_MIME_BY_EXT = {
    "png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "jpx": "image/jp2",
    "gif": "image/gif", "tiff": "image/tiff", "tif": "image/tiff", "bmp": "image/bmp",
    "webp": "image/webp",
}


def _rel_box(box, bbox) -> list[float]:
    """相對於候選左上角的 bbox（讓簽章與絕對頁面座標無關）。"""
    return [round(box[0] - bbox[0], 1) + 0.0, round(box[1] - bbox[1], 1) + 0.0,
            round(box[2] - bbox[0], 1) + 0.0, round(box[3] - bbox[1], 1) + 0.0]


def _content_signature(bbox, channels, kind, words_in, columns, raster_digest,
                       pos_texts, drawing_rects, native_table) -> str:
    """**頁面無關**的內容簽章（契約 §13.2）。

    不含頁碼、不含絕對座標——這是「同一張圖出現在多頁只算一次 VL」對非純 raster
    也成立的前提。

    必須含**逐字的相對 bbox 與原字串、框內 drawing 幾何、native cell geometry、
    以及所有 content-bearing `pos` 原文的 digest**（local review BLOCKER #3）。
    只序列化 band 左上角 + 人工插空格的整行文字是不夠的：兩張外框相同、但內部格線、
    欄位位置或可見空白不同的向量圖會拿到同一個 `asset_digest`，於是被誤去重、
    把 A 圖的 VL payload 套到 B 圖，甚至誤沿用 A 圖的 human verification。
    """
    words = sorted(
        [_rel_box(box, bbox) + [text]
         for box, text in ((_word_box(w), str(w[4])) for w in words_in)
         if box is not None],
        key=lambda item: (item[1], item[0], item[4]),
    )
    draws = sorted(_rel_box(rect, bbox) for rect in drawing_rects or []
                   if _overlaps(rect, bbox))
    geometry = (native_table or {}).get("geometry") or {}
    cells = [[_rel_box(cell, bbox) if _as_bbox(cell) else None for cell in row]
             for row in (geometry.get("cells") or [])]
    payload = {
        "w": round(bbox[2] - bbox[0], 2) + 0.0,
        "h": round(bbox[3] - bbox[1], 2) + 0.0,
        "channels": sorted(set(channels), key=_channel_rank),
        "kind": kind,
        "cols": columns["n_columns"],
        "raster": raster_digest,
        "words": words,
        "drawings": draws,
        "cells": cells,
        "pos_text": [hashlib.sha256((t or "").encode("utf-8")).hexdigest()
                     for t in sorted(pos_texts or [])],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _valid_cell_geometry(geometry) -> bool:
    """geometry 是否真的帶得出**每一格**的 bbox（契約 §12.4）。

    空 dict、`cells=[[None, ...]]`、ragged matrix 一律不算——沒有真正的 cell 幾何時
    宣稱 native lane，等於讓 verifier 拿不存在的座標去對格。
    """
    if not isinstance(geometry, dict):
        return False
    cells = geometry.get("cells")
    rows = geometry.get("row_count")
    cols = geometry.get("col_count")
    if not cells or not _is_real_int(rows) or not _is_real_int(cols):
        return False
    if rows < 1 or cols < 1 or len(cells) != rows:
        return False
    for row in cells:
        if not isinstance(row, (list, tuple)) or len(row) != cols:
            return False
        if any(_as_bbox(cell) is None for cell in row):
            return False
    return True


def _valid_native_table(native_table) -> bool:
    """`native_table` 是否足以支撐 native lane：合法 `pos` **或**真正的 cell geometry。"""
    if not isinstance(native_table, dict):
        return False
    pos = native_table.get("pos")
    if isinstance(pos, (list, tuple)) and len(pos) == 2 \
            and _is_real_int(pos[0]) and _is_real_int(pos[1]) and 0 <= pos[0] < pos[1]:
        return True
    return _valid_cell_geometry(native_table.get("geometry"))


def _native_table_from(regions, evidence) -> tuple[dict | None, list[str]]:
    """群內的原生表格 evidence：`pos` / 原始 markdown / **含每個 cell bbox 的** geometry。

    `geometry` 形狀與 `PageEvidence.tables[*]["geometry"]` 完全一致（審核 BLOCKER 2），
    否則 verifier 的 `cell_geometry` 永遠是 False（契約 §12.4）。

    **同一個 component 內出現多個不同 `pos` → 整組失格**（回 `(None, ["ambiguous_table_pos"])`）。
    只留第一個 pos 的話，第二段原 Markdown 會留在 KB，structured 結果又照常入庫 →
    兩份互相競爭的表（workflow §8-4 / local review BLOCKER #6）。
    """
    reasons: list[str] = []
    positions = []
    for region in regions:
        if region["channel"] == "page_boxes:table" and region["detail"].get("pos"):
            start, end = region["detail"]["pos"]
            positions.append((int(start), int(end)))
    distinct = sorted(set(positions))
    pos = None
    markdown = ""
    if len(distinct) > 1:
        return None, ["ambiguous_table_pos"]
    if distinct:
        pos = distinct[0]
        markdown = evidence.raw_markdown[pos[0]:pos[1]]

    geometry = None
    strategy = ""
    for name in _TABLE_STRATEGIES:
        for region in regions:
            if region["channel"] == f"find_tables:{name}" \
                    and _valid_cell_geometry(region["detail"].get("geometry")):
                geometry = region["detail"]["geometry"]
                strategy = name
                break
        if geometry is not None:
            break
    if pos is None and geometry is None:
        return None, reasons
    native = {"pos": pos, "markdown": markdown, "geometry": geometry, "strategy": strategy}
    if not _valid_native_table(native):
        return None, ["native_table_geometry_unusable"]
    return native, reasons


def _native_text_span(evidence: PageEvidence, bbox) -> dict | None:
    """候選是否有「一塊 `pos` 支撐的 raw markdown 文字」（契約 §15.1）。

    這是 terminal 走 native lane 的**唯一**條件：word geometry 單獨不算。
    `page.get_text("words")` 證明不了行首縮排、行尾空白與「完全沒有字的空行」，
    拿它合成 canonical 正文就是把猜出來的普通空格當成原文（workflow §8-2）。

    判定：文字類 page_box 中，**恰好一個**與候選共延（IoU >= `FIGURE_IOU_MERGE`）
    且帶合法 `pos`。零個或多個一律 abstain —— 多個代表這塊 log 在 markdown 裡被拆成
    好幾段，說不出哪一段才是這個候選的正文。

    **旋轉頁一律 abstain**：本模組拿的是校準過的 `_bbox_unrotated`，但下游 verifier
    的 pos 反查用的是**上游原始（display 空間）** bbox；旋轉頁上兩者對不起來，
    planner 說 `native_lane=True`、verifier 卻找不到 pos，就會 fail-loud 成整份 PDF
    零寫入。何況實測 rotation != 0 時 pymupdf4llm 的 markdown 本來就空掉或錯亂
    （見模組 docstring 的上游限制表），那份原文不值得當 canonical。
    """
    if int(getattr(evidence, "rotation", 0) or 0) != 0:
        return None
    matches = []
    for box in evidence.page_boxes:
        if box.get("class") not in _TEXT_BEARING_CLASSES:
            continue
        pos = box.get("_pos")
        box_bbox = _as_bbox(box.get("_bbox_unrotated"))
        if pos is None or box_bbox is None:
            continue
        if _iou(box_bbox, bbox) < float(config.FIGURE_IOU_MERGE):
            continue
        matches.append(box)
    if len(matches) != 1:
        return None
    box = matches[0]
    start, end = box["_pos"]
    return {
        "pos": (int(start), int(end)),
        "markdown": evidence.raw_markdown[start:end],
        "source": f"page_boxes:{box.get('class')}",
        "box_index": box.get("_ordinal"),
    }


def _resolve_native_lane(kind: str, native_table, native_text) -> tuple[bool, list[str]]:
    """★ native lane 的**單一真相**（契約 §15.1）。

    | kind | native_lane |
    |---|---|
    | `table` | `bool(native_table)`（markdown `pos` 或 cell geometry） |
    | `terminal` | 只有在有 `pos` 支撐的 raw markdown 文字時才 True |
    | 其餘（含 `unknown`） | False —— 走 VL lane，words 仍是 secondary anchor |

    產出寫進 `candidate.signals["native_lane"]`。`_vl_profile()`（preflight）、
    `RAG.py` 的 probe 判定與 `figure_verify` 的 lane 選擇**全部讀這個值**，
    不得各自重算——三處各算各的正是總審 BLOCKER #1。
    """
    fx = _fx()
    if kind == fx.KIND_TABLE:
        if _valid_native_table(native_table):
            return True, ["native_lane_table"]
        return False, ["vl_lane_no_native_table"]
    if kind == fx.KIND_TERMINAL:
        if native_text:
            return True, ["native_lane_terminal_pos"]
        return False, ["vl_lane_word_only_terminal"]
    return False, ["vl_lane_kind_unknown"]


def _page_candidates(evidence: PageEvidence, *, document_id: str, pdf_doc,
                     stats: dict) -> list[dict]:
    """一頁的候選（尚未指派文件級 index / figure_id）。"""
    deferred = stats["deferred_to_legacy_lane"]
    page_rect = evidence.page_rect
    if _area(page_rect) <= 0:
        return []

    # 「channel 整條不可用」只看頂層 slug（`find_tables:lines:ValueError` 是單一策略失敗，
    # `find_tables:empty_on_rotated_page` 是上游限制，兩者都不代表整個 channel 死掉）。
    dead = {u.split(":", 1)[0] for u in evidence.unavailable
            if u.count(":") == 1 and not u.endswith(":empty_on_rotated_page")}
    native_alive = [name for name in ("words", "image_info", "find_tables", "drawings")
                    if name not in dead]
    if not native_alive:
        # 契約 §6.3：所有結構性 channel 都不可用的頁不得產生候選。
        for region in _region_sources(evidence):
            deferred.append(_deferred_entry(evidence.page, region["bbox"],
                                            [region["channel"]], "no_native_channel"))
        return []

    regions = _region_sources(evidence)
    promoting = [r for r in regions if r["promoting"]]
    # ★ 在 O(n²) fusion **之前**就擋住：釘版 pymupdf4llm 已知會把微小圓點切成大量
    # 元素（workflow §4 Step 2），病態 PDF 可以在 fail-loud 報告產出前先把時間/記憶體
    # 吃光。這是資源邊界，不是候選上限（local review BLOCKER #10）。
    if len(regions) > MAX_RAW_REGIONS_PER_PAGE or len(promoting) > MAX_PROMOTING_REGIONS_PER_PAGE:
        stats.setdefault("raw_region_overflow", []).append({
            "page": evidence.page,
            "regions": len(regions),
            "promoting": len(promoting),
            "limit_regions": MAX_RAW_REGIONS_PER_PAGE,
            "limit_promoting": MAX_PROMOTING_REGIONS_PER_PAGE,
        })
        for region in regions:
            deferred.append(_deferred_entry(evidence.page, region["bbox"],
                                            [region["channel"]], "raw_regions_per_page"))
        return []
    others = [r for r in regions if not r["promoting"]]
    if not promoting:
        for region in others:
            reason = ("picture_only" if region["channel"].startswith("page_boxes")
                      else "page_fallback_no_structural_evidence"
                      if region["channel"] == "page:fallback"
                      else "raster_no_structural_evidence")
            deferred.append(_deferred_entry(evidence.page, region["bbox"],
                                            [region["channel"]], reason))
        return []

    threshold = float(config.FIGURE_IOU_MERGE)
    components = _components(promoting, threshold)
    attachments, orphans = _attach_non_promoting(components, promoting, others, threshold)
    stats["fusion_components"] += len(components)
    for region in orphans:
        reason = ("picture_only" if region["channel"].startswith("page_boxes")
                  else "page_fallback_no_structural_evidence"
                  if region["channel"] == "page:fallback"
                  else "raster_no_structural_evidence")
        deferred.append(_deferred_entry(evidence.page, region["bbox"],
                                        [region["channel"]], reason))

    drawing_rects = (evidence.overlays or {}).get("drawing_rects") or []
    channels_complete, missing_channels = _overlay_channels_complete(evidence)
    fx_kind_terminal = _fx().KIND_TERMINAL
    results: list[dict] = []
    for comp, attached in zip(components, attachments):
        members = [promoting[i] for i in comp] + list(attached)
        bbox = _round_box(_union_box([m["bbox"] for m in members]))
        clipped = _intersection(bbox, page_rect)
        channels = [m["channel"] for m in members]
        if clipped is None:
            deferred.append(_deferred_entry(evidence.page, bbox, channels, "bbox_outside_page"))
            continue
        bbox = _round_box(clipped)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        if min(width, height) < MIN_CANDIDATE_SIDE_PT or width * height < MIN_CANDIDATE_AREA_PT2:
            deferred.append(_deferred_entry(evidence.page, bbox, channels, "below_min_size"))
            continue

        words_in = [w for w in evidence.words if _word_in(w, bbox, WORD_ASSIGN_DILATE_PT)]
        bands = _word_bands(evidence.words, bbox)
        columns = _column_signal(bands)
        ruled = _ruled_signal(drawing_rects, bbox)
        layout = _terminal_layout_signal(bands)
        tokens = _token_signal(bands)
        rasters = [
            entry for entry in evidence.image_info or []
            if _as_bbox(entry.get("bbox")) and _coverage(_as_bbox(entry["bbox"]), bbox) >= 0.9
        ]
        # 「純 raster」= 文字層完全看不見這塊內容。有原生表格 evidence（`pos` 或
        # find_tables 幾何）就不算——掃描頁被上游分類成 table 的區域要走 table lane，
        # 不能因為它是像素就被 diagram 分數蓋過去。
        native_table, native_reasons = _native_table_from(members, evidence)
        if "ambiguous_table_pos" in native_reasons:
            # 同一群裡有多個不同的 table `pos`：說不出哪一段原 Markdown 該被取代。
            # 只留第一個會讓第二段留在 KB、structured 結果又照常入庫 → 兩份互相
            # 競爭的表（workflow §8-4）。整組失格，兩段原文都完整保留。
            deferred.append(_deferred_entry(evidence.page, bbox, channels,
                                            "ambiguous_table_pos"))
            continue
        native_hit = bool(native_table)
        raster_only = bool(rasters and not bands and not native_table)

        scores = _score_kind(bands, columns, ruled, layout, tokens, native_hit, raster_only)
        kind, kind_reasons = _route_kind(scores)
        native_text = _native_text_span(evidence, bbox) if kind == fx_kind_terminal else None
        native_lane, lane_reasons = _resolve_native_lane(kind, native_table, native_text)
        if not kind:
            deferred.append(_deferred_entry(evidence.page, bbox, channels, kind_reasons[0]))
            continue
        if raster_only:
            # 契約 §13.1：純 raster（零 word band、無結構性原生證據）一律不升格，
            # 交給既有 legacy picture lane（第二道防線；孤兒 raster 在 fusion 前就擋掉了）
            deferred.append(_deferred_entry(evidence.page, bbox, channels,
                                            "raster_no_structural_evidence"))
            continue

        purity = _raster_purity(
            bbox, rasters, bands, drawing_rects, evidence.overlays, pdf_doc,
            channels_complete=channels_complete, missing_channels=missing_channels)
        raster_digest = purity["digest_hex"] or (rasters[0].get("digest_hex", "") if rasters else "")
        # 所有 content-bearing pos（不只 native table 那一段）都要進簽章
        pos_texts = []
        for box_entry in evidence.page_boxes:
            span = box_entry.get("_pos")
            if not span:
                continue
            entry_box = _as_bbox(box_entry.get("_bbox_unrotated"))
            if entry_box is None or not _overlaps(entry_box, bbox):
                continue
            pos_texts.append(evidence.raw_markdown[span[0]:span[1]])
        if native_text:
            pos_texts.append(native_text["markdown"])
        signature = _content_signature(
            bbox, channels, kind, words_in, columns, raster_digest,
            pos_texts, drawing_rects, native_table)
        asset_digest = (purity["digest_hex"] if purity["pure"]
                        else hashlib.sha256(signature.encode("utf-8")).hexdigest())

        reasons = list(kind_reasons) + lane_reasons + native_reasons
        for channel in sorted(set(channels), key=_channel_rank):
            reasons.append("evidence_" + channel.replace(":", "_"))
        if any(u == "page_boxes_geometry:unalignable" for u in evidence.unavailable):
            reasons.append("page_boxes_geometry_unaligned")
        if ruled["grid"]:
            reasons.append("ruled_grid")
        if columns["col_support"] >= COL_SUPPORT_MIN:
            reasons.append("column_aligned_bands")
        if layout["prompt_lines"] or layout["timestamp_lines"]:
            reasons.append("prompt_or_timestamp_lines")
        if evidence.rotation and (native_table or {}).get("pos"):
            # 實測：rotation != 0 時 pymupdf4llm 的 markdown 會空掉或錯亂（上游限制）。
            # `pos` 對 raw markdown 仍然精確，但那份 markdown 本身不可信 → 誠實標記，
            # 讓 verifier 不會把它當成可信的 native channel。
            reasons.append("rotated_page_markdown_unreliable")

        signals = {
            "document_id": document_id,
            "channels": sorted(set(channels), key=_channel_rank),
            "bands": bands,
            "columns": columns,
            "ruled": ruled,
            "layout": layout,
            "tokens": tokens,
            "raster_purity": purity,
            "raster_only": raster_only,
            "anchored": bool(bands),
            # ★ 契約 §15.1 的單一真相；preflight / RAG probe / verifier 都讀這個
            "native_lane": native_lane,
            "native_text": native_text,
            "page_rect": list(page_rect),
            "rotation": evidence.rotation,
        }
        results.append({
            "page": evidence.page,
            "bbox": bbox,
            "page_rect": page_rect,
            "kind": kind,
            "kind_scores": scores,
            "signals": signals,
            "reasons": reasons,
            "signature": signature,
            "native_table": native_table,
            "asset_xref": purity["xref"] if purity["pure"] else None,
            "asset_digest": asset_digest,
            "score": max(scores.values()),
        })

    results.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
    return results


# ============================================================
# 8. 取像：DPI / glyph 下限 / baseline tiling
# ============================================================
def estimate_image_tokens(width: int, height: int) -> int:
    """以 `config.FIGURE_IMAGE_TOKEN_PATCH_PX` 的 patch 估算影像 token 數。

    ⚠️ **這是估算，不是保證**：實際 tokenizer 依 server / model / projector 而異。
    只用於 preflight 預算與 fail-loud，不作為正確性依據。
    """
    patch = max(1, int(config.FIGURE_IMAGE_TOKEN_PATCH_PX))
    w = max(0, int(width))
    h = max(0, int(height))
    if w == 0 or h == 0:
        return 0
    return math.ceil(w / patch) * math.ceil(h / patch)


def _plan_tiles(candidate_data: dict) -> dict:
    """算出這個候選要怎麼取像（純函式；plan 期就決定，render 期只照著做）。

    - **DPI 由「有效 glyph pixels」決定，不是只看 DPI**：先取
      `max(目標 DPI, FIGURE_MIN_GLYPH_PX / 中位字高)`，再受 `FIGURE_RENDER_MAX_SIDE_PX` 壓。
      壓完仍低於 `FIGURE_MIN_GLYPH_PX` → `glyph_px_below_min=True` + reason。
      ⚠️ **提高 DPI 不會創造不存在的資訊**：raster 上原本就被遮住或低於解析度的字，
      放大只會得到更大的模糊塊。這個旗標是給 verifier / 人工覆核的誠實訊號，
      不是「調高 DPI 就能解決」的效能問題。
    - **tiling 依 row/line baseline 或偵測區帶切，絕不按固定高度盲切**。切點一律落在
      相鄰兩帶之間的空白；單一帶自己超限就整帶一張並標 `oversized_band`（不切開一行/一列）。
    - 沒有 band 可切（純像素）時**不盲切**：先降 zoom 到 glyph 下限，仍超限就記
      `image_tokens_per_call` 讓 preflight fail-loud。
    - token 估算用**外擴 overlap 之後**的保守像素界，並多算 `PATCH_ROUNDING_MARGIN`
      個 patch（pixmap rounding），避免 preflight 過關但實際 Variant 超限（審核 BLOCKER 15）。
    """
    bbox = candidate_data["bbox"]
    signals = candidate_data["signals"]
    bands = signals["bands"]
    purity = signals["raster_purity"]
    per_call = max(1, int(config.FIGURE_MAX_IMAGE_TOKENS_PER_CALL))
    local_over: list[str] = []

    if purity["pure"]:
        # raster 送的是 intrinsic 原始 binary，**不存在 zoom 這回事**（審核 BLOCKER 14）：
        # token 一律用原圖 width/height 估，超限就 fail-loud。
        width, height = purity["width"], purity["height"]
        tokens = estimate_image_tokens(width, height)
        if tokens > per_call:
            local_over.append("image_tokens_per_call")
        tile = {
            "tile_index": 0, "tile_total": 1,
            "core_bbox": list(bbox), "bbox": list(bbox),
            "band_range": None, "overlap_top_pt": 0.0, "overlap_bottom_pt": 0.0,
            "overlap_px": 0, "cut_y_top": None, "cut_y_bottom": None,
            "band_texts": [], "oversized_band": False,
            "px_width": width, "px_height": height, "est_image_tokens": tokens,
        }
        return {"mode": "raster", "zoom": 1.0, "effective_dpi": 0,
                "median_glyph_pt": 0.0, "glyph_px": 0.0, "glyph_px_below_min": False,
                "tiles": [tile], "est_tokens": [tokens], "est_tokens_total": tokens,
                "over_budget": local_over}

    width_pt = bbox[2] - bbox[0]
    height_pt = bbox[3] - bbox[1]
    median_glyph = _median([b["h"] for b in bands], DEFAULT_GLYPH_PT) or DEFAULT_GLYPH_PT
    target_zoom = float(config.FIGURE_RENDER_TARGET_DPI) / 72.0
    glyph_zoom = float(config.FIGURE_MIN_GLYPH_PX) / max(median_glyph, 0.1)
    zoom = max(target_zoom, glyph_zoom)
    max_side = float(config.FIGURE_RENDER_MAX_SIDE_PX)
    longest = max(width_pt, height_pt, 0.1)
    zoom = min(zoom, max_side / longest)
    zoom = max(zoom, 0.05)
    glyph_px = median_glyph * zoom
    below_min = glyph_px < float(config.FIGURE_MIN_GLYPH_PX)

    overlap_pt = float(config.FIGURE_TILE_OVERLAP_PX) / zoom

    def _tokens_for(box) -> int:
        px_w = math.ceil((box[2] - box[0]) * zoom)
        px_h = math.ceil((box[3] - box[1]) * zoom)
        patch = max(1, int(config.FIGURE_IMAGE_TOKEN_PATCH_PX))
        return ((math.ceil(px_w / patch) + PATCH_ROUNDING_MARGIN)
                * (math.ceil(px_h / patch) + PATCH_ROUNDING_MARGIN))

    if _tokens_for(bbox) <= per_call:
        px_w = math.ceil(width_pt * zoom)
        px_h = math.ceil(height_pt * zoom)
        tokens = _tokens_for(bbox)
        tile = {
            "tile_index": 0, "tile_total": 1,
            "core_bbox": list(bbox), "bbox": list(bbox),
            "band_range": [bands[0]["index"], bands[-1]["index"]] if bands else None,
            "overlap_top_pt": 0.0, "overlap_bottom_pt": 0.0, "overlap_px": 0,
            "cut_y_top": None, "cut_y_bottom": None,
            "band_texts": [b["text"] for b in bands],
            "oversized_band": False,
            "px_width": px_w, "px_height": px_h, "est_image_tokens": tokens,
        }
        return {"mode": "crop", "zoom": zoom,
                "effective_dpi": int(round(zoom * 72)),
                "median_glyph_pt": round(median_glyph, 3) + 0.0,
                "glyph_px": round(glyph_px, 2) + 0.0, "glyph_px_below_min": below_min,
                "tiles": [tile], "est_tokens": [tokens], "est_tokens_total": tokens,
                "over_budget": local_over}

    if not bands:
        # 沒有 baseline 可切 → **不盲切**。降到 glyph 下限仍超限就 fail-loud。
        floor_zoom = max(0.05, min(zoom, float(config.FIGURE_MIN_GLYPH_PX) / max(median_glyph, 0.1)))
        zoom = min(zoom, floor_zoom)
        tokens = _tokens_for(bbox)
        if tokens > per_call:
            local_over.append("image_tokens_per_call")
        px_w = math.ceil(width_pt * zoom)
        px_h = math.ceil(height_pt * zoom)
        tile = {
            "tile_index": 0, "tile_total": 1,
            "core_bbox": list(bbox), "bbox": list(bbox), "band_range": None,
            "overlap_top_pt": 0.0, "overlap_bottom_pt": 0.0, "overlap_px": 0,
            "cut_y_top": None, "cut_y_bottom": None, "band_texts": [],
            "oversized_band": False,
            "px_width": px_w, "px_height": px_h, "est_image_tokens": tokens,
        }
        return {"mode": "crop", "zoom": zoom, "effective_dpi": int(round(zoom * 72)),
                "median_glyph_pt": round(median_glyph, 3) + 0.0,
                "glyph_px": round(median_glyph * zoom, 2) + 0.0,
                "glyph_px_below_min": median_glyph * zoom < float(config.FIGURE_MIN_GLYPH_PX),
                "tiles": [tile], "est_tokens": [tokens], "est_tokens_total": tokens,
                "over_budget": local_over}

    # ── baseline tiling ────────────────────────────────────────────
    # ★ core 必須**完整分割**整個候選：第一塊的上緣＝候選頂端、最後一塊的下緣＝候選底端、
    # 中間邊界＝相鄰兩帶之間的中點。只用「首末 band 的 y 範圍」當 core 的話，第一帶
    # 之前、末帶之後、以及兩群 band 之間的大片空白會**完全沒有進到任何 tile**——
    # 內容靜默消失，直接違反北極星（local review BLOCKER #2）。
    def _core_bounds(first_index: int, last_index: int) -> tuple[float, float]:
        top = bbox[1] if first_index == 0 else (
            bands[first_index - 1]["y1"] + bands[first_index]["y0"]) / 2.0
        bottom = bbox[3] if last_index >= len(bands) - 1 else (
            bands[last_index]["y1"] + bands[last_index + 1]["y0"]) / 2.0
        return max(bbox[1], top), min(bbox[3], bottom)

    def _expand(first_index: int, last_index: int) -> tuple[float, float]:
        """core 範圍 → 含 overlap 的實際 render 範圍。

        overlap 至多吃到相鄰 core 的另一端（外加 `FIGURE_TILE_OVERLAP_PX` 上限）：
        行距小於 overlap 設定時不加限制會讓 tile 之間不再單調遞增，stitch evidence
        就沒有意義了。
        """
        core_top, core_bottom = _core_bounds(first_index, last_index)
        prev_top = _core_bounds(0, first_index - 1)[0] if first_index > 0 else bbox[1]
        next_bottom = (_core_bounds(last_index + 1, len(bands) - 1)[1]
                       if last_index + 1 < len(bands) else bbox[3])
        top = core_top - min(overlap_pt, max(0.0, core_top - prev_top))
        bottom = core_bottom + min(overlap_pt, max(0.0, next_bottom - core_bottom))
        return max(bbox[1], top), min(bbox[3], bottom)

    groups: list[tuple[int, int]] = []
    start = 0
    for position in range(len(bands)):
        top, bottom = _expand(start, position)
        probe = (bbox[0], top, bbox[2], bottom)
        if position > start and _tokens_for(probe) > per_call:
            groups.append((start, position - 1))
            start = position
    groups.append((start, len(bands) - 1))

    total = len(groups)
    tiles = []
    est = []
    previous_bottom = None
    for order, (first_index, last_index) in enumerate(groups, 1):
        group = bands[first_index:last_index + 1]
        core_top, core_bottom = _core_bounds(first_index, last_index)
        top, bottom = _expand(first_index, last_index)
        # 縫合切點＝core 的邊界，一律落在**相鄰兩帶之間的空白**（不是固定高度切）；
        # 首尾則是候選本身的邊界，所以整個候選被 core 完整覆蓋、沒有洞。
        cut_top = core_top if first_index > 0 else None
        cut_bottom = core_bottom if last_index < len(bands) - 1 else None
        box = (bbox[0], round(top, 2) + 0.0, bbox[2], round(bottom, 2) + 0.0)
        tokens = _tokens_for(box)
        if tokens > per_call:
            local_over.append("image_tokens_per_call")
        overlap_px = 0
        if previous_bottom is not None and previous_bottom > box[1]:
            overlap_px = int(round((previous_bottom - box[1]) * zoom))
        overlap_top_bands = [b["index"] for b in bands if b["y1"] <= core_top and b["y0"] >= box[1]]
        overlap_bottom_bands = [b["index"] for b in bands if b["y0"] >= core_bottom and b["y1"] <= box[3]]
        tiles.append({
            "tile_index": order, "tile_total": total,
            "core_bbox": [bbox[0], round(core_top, 2) + 0.0, bbox[2], round(core_bottom, 2) + 0.0],
            "bbox": list(box),
            "band_range": [group[0]["index"], group[-1]["index"]],
            "overlap_top_pt": round(max(0.0, core_top - box[1]), 2) + 0.0,
            "overlap_bottom_pt": round(max(0.0, box[3] - core_bottom), 2) + 0.0,
            "overlap_px": overlap_px,
            "cut_y_top": round(cut_top, 2) + 0.0 if cut_top is not None else None,
            "cut_y_bottom": round(cut_bottom, 2) + 0.0 if cut_bottom is not None else None,
            "overlap_band_indices_top": overlap_top_bands,
            "overlap_band_indices_bottom": overlap_bottom_bands,
            "band_texts": [b["text"] for b in group],
            "oversized_band": bool(len(group) == 1 and tokens > per_call),
            "px_width": math.ceil((box[2] - box[0]) * zoom),
            "px_height": math.ceil((box[3] - box[1]) * zoom),
            "est_image_tokens": tokens,
        })
        est.append(tokens)
        previous_bottom = box[3]

    return {"mode": "crop", "zoom": zoom, "effective_dpi": int(round(zoom * 72)),
            "median_glyph_pt": round(median_glyph, 3) + 0.0,
            "glyph_px": round(glyph_px, 2) + 0.0, "glyph_px_below_min": below_min,
            "tiles": tiles, "est_tokens": est, "est_tokens_total": sum(est),
            "over_budget": sorted(set(local_over))}


def _candidate_locator(candidate: Candidate) -> str:
    """契約 §5 要求的定位字串：文件身分、頁、候選序號、figure_id。"""
    document = candidate.document_id or candidate.signals.get("document_id") or "<unknown>"
    return (f"{document} 第 {candidate.page} 頁 候選 #{candidate.index} "
            f"figure={candidate.figure_id}")


def _render_error(candidate: Candidate, where: str, exc: BaseException):
    fx = _fx()
    return fx.FigureError(
        f"圖面取像失敗: {_candidate_locator(candidate)} {where}: "
        f"{type(exc).__name__}: {exc}"
    )


def _self_checked(candidate: Candidate, variant: Variant) -> Variant:
    """產生端自檢（契約 §21.2 T3）：交出去之前先過**共享** validator。

    `Variant` 這個凍結介面有三個消費端（verifier / RAG / writer），前四輪終審每次都只有
    被點名的那一兩端各自收緊，第三端維持寬鬆，繞道因此永遠存在（契約 §21.5）。結構性
    解法是「門面出唯一 validator，產生端保證輸出合法」——所以這裡呼叫的必須是
    `figure_extract.validate_variant`，**不得**在本模組另寫一份欄位規則。

    不合格一律 fail-loud（`FigureExtractionError`，`FigureError` 的子類別，訊息帶完整
    定位）：本模組是產生端，自己產出不合格的 Variant 代表 plan 或上游取像結果壞了，
    交出去只會讓下游拿著一張「宣稱與內容對不上」的圖去花 VL 的錢。
    """
    _fx().validate_variant(
        variant, where=f"圖面取像自檢: {_candidate_locator(candidate)} "
                       f"variant={variant.variant_id}")
    return variant


def render_candidate_variants(pdf_doc, candidate: Candidate) -> list[Variant]:
    """把一個候選變成實際要送模型的影像。

    - 能確定是**無 overlay 的純 raster** → `Document.extract_image(xref)` 的**原始 binary**
      （`variant_id="raster"`，真實型別看 `Variant.mime`）。
    - 其餘一律 page crop：`clip` 用 `bbox * page.rotation_matrix` 換到 display 空間
      （`get_pixmap` 的 clip 吃 display 空間，實測）。
    - **所有**失敗路徑都包成 `figure_extract.FigureError`（自檢失敗是它的子類別
      `FigureExtractionError`），訊息帶檔案身分、頁、候選序號、figure_id、tile 與底層
      原始錯誤，並保留 exception chaining（審核 BLOCKER 18）。

    **產生端不做任何 coercion**（契約 §21.2 T3）：tile plan 的 `tile_index` /
    `tile_total` / `overlap_px` 與 `extract_image()` 的 `width` / `height` 一律原值進
    `Variant`，再由 `_self_checked()` 交給共享 validator 判生死。以前這裡寫
    `int(tile["tile_total"])`，於是 `1.9` / `True` / `"1"` 全都被洗成合法的
    `(tile_total=1, tile_index=0)`——產出的 Variant 因此通過**每一個**消費端的檢查，
    洞不在 validator 而在產生端先把壞值洗成好值（本接縫連續四輪的成因）。
    """
    plan = candidate.signals.get("tile_plan") or {}
    tiles = plan.get("tiles") or []
    if not tiles:
        raise _fx().FigureError(
            f"候選 #{candidate.index}（figure={candidate.figure_id}）沒有 tile plan，無法取像")

    if plan.get("mode") == "raster" and candidate.asset_xref is not None:
        try:
            extracted = pdf_doc.extract_image(int(candidate.asset_xref))
            payload = bytes(extracted["image"])
            # 原值，**不 int()**：上游給的尺寸與實際 bytes 對不上時要看得見，
            # 不是被截成一個看起來正常的數字（自檢在下面）。
            width = extracted["width"]
            height = extracted["height"]
            ext = str(extracted.get("ext") or "").lower()
        except Exception as exc:  # noqa: BLE001
            raise _render_error(candidate, f"extract_image(xref={candidate.asset_xref})", exc) from exc
        if not payload:
            raise _fx().FigureError(
                f"圖面取像失敗: {_candidate_locator(candidate)} "
                f"extract_image(xref={candidate.asset_xref}) 回傳空 bytes")
        tile = tiles[0]
        try:
            variant = Variant(
                figure_id=candidate.figure_id, variant_id="raster", png=payload,
                width=width, height=height, bbox=candidate.bbox,
                tile_index=0, tile_total=1, overlap_px=0,
                est_image_tokens=estimate_image_tokens(width, height),
                digest=hashlib.sha256(payload).hexdigest(),
                stitch=dict(tile), mime=_MIME_BY_EXT.get(ext, "application/octet-stream"),
            )
        except Exception as exc:  # noqa: BLE001
            raise _render_error(candidate, "raster 建構 Variant", exc) from exc
        return [_self_checked(candidate, variant)]

    try:
        import pymupdf
    except Exception as exc:  # noqa: BLE001
        raise _render_error(candidate, "import pymupdf", exc) from exc
    try:
        page = pdf_doc[candidate.page - 1]
        rot_matrix = page.rotation_matrix
    except Exception as exc:  # noqa: BLE001
        raise _render_error(candidate, f"載入第 {candidate.page} 頁", exc) from exc

    try:
        # `zoom` / `dpi` 是 render 參數（不是 `Variant` 欄位，`dpi` 只進 variant_id 的標籤），
        # 這裡照舊轉型；但壞值不得以裸 ValueError 逃出去——本函式的失敗一律是 FigureError。
        zoom = float(plan.get("zoom") or 1.0)
        dpi = int(plan.get("effective_dpi") or round(zoom * 72))
    except Exception as exc:  # noqa: BLE001
        raise _render_error(candidate, "讀 tile plan 的 zoom / effective_dpi", exc) from exc
    variants: list[Variant] = []
    for tile in tiles:
        # 原值，**不 int()**（契約 §21.2 T3）。`== 1` 而不是 `<= 1`：合法定義域上兩者
        # 等價（`tile_total >= 1`），但 `<=` 碰到 `"1"` 這種型別會自己丟 TypeError，
        # 那條路徑就不再是「包成 FigureError 的 fail-loud」了。
        try:
            total = tile["tile_total"]
            index = tile["tile_index"]
        except Exception as exc:  # noqa: BLE001
            raise _render_error(candidate, "讀 tile metadata", exc) from exc
        variant_id = f"crop@{dpi}dpi" if total == 1 else f"crop@{dpi}dpi#tile{index}of{total}"
        try:
            clip = pymupdf.Rect(*tile["bbox"]) * rot_matrix
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
        except Exception as exc:  # noqa: BLE001
            raise _render_error(candidate, f"render {variant_id}", exc) from exc
        if pix.width < 2 or pix.height < 2:
            raise _fx().FigureError(
                f"圖面取像失敗: {_candidate_locator(candidate)} {variant_id}: "
                f"render 結果過小（{pix.width}x{pix.height}px）")
        try:
            png = pix.tobytes("png")
        except Exception as exc:  # noqa: BLE001
            raise _render_error(candidate, f"{variant_id} tobytes(png)", exc) from exc
        try:
            variant = Variant(
                figure_id=candidate.figure_id, variant_id=variant_id, png=png,
                width=pix.width, height=pix.height, bbox=tuple(tile["bbox"]),
                tile_index=index, tile_total=total, overlap_px=tile["overlap_px"],
                est_image_tokens=estimate_image_tokens(pix.width, pix.height),
                digest=hashlib.sha256(png).hexdigest(), stitch=dict(tile), mime="image/png",
            )
        except Exception as exc:  # noqa: BLE001
            raise _render_error(candidate, f"{variant_id} 建構 Variant", exc) from exc
        variants.append(_self_checked(candidate, variant))
    return variants


# ============================================================
# 9. plan / preflight
# ============================================================
def _vl_profile(candidate: Candidate) -> dict:
    """一個候選的 VL 呼叫狀態機（契約 §12.1 / §12.4、審核 BLOCKER 16）。

    | lane | 條件 | min | max |
    |---|---|---|---|
    | native | `figure_extract.read_native_lane(candidate)` 為 True | 0 | 0 |
    | 共享（重複影像） | `signals["vl_shared_with"] is not None` | 0 | 0 |

    **duplicate 為什麼可以是 0**（契約 §19.3，兩邊必須同一個定義）：verifier 以
    `signals["vl_share_key"]`＝`(asset_digest, planner requested kind)` 命中代表候選的
    抽取結果，沿用之後**只重算 occurrence-level alignment**（純 code，不呼叫 VL）。
    requested kind 是 `unknown` 時也一樣——cache 必須用 requested kind 而不是 dual pass
    解歧後的 kind 去比對，否則第二筆永遠命不中，實際會跑滿 4 次而 preflight 宣稱 0。
    | VL / kind 已定 / 有 anchor | — | `T` | `2T(1+R)` |
    | VL / kind 已定 / 無 anchor | 需 disagreement detection | `2T` | `2T(1+R)` |
    | VL / KIND_UNKNOWN | dual pass 每 kind 一次、不重試、不取第二樣本 | `2T` | `2T` |

    `T` = tile 數、`R` = `config.FIGURE_EXTRACT_RETRIES`。

    ★ 這張表是**契約 §20 的凍結狀態表**，由主代理直接讀 `figure_verify.py` 的實際呼叫
    路徑訂出來（`:1197` `attempts = 1 + retries`、`:3571-3577` 第二次取樣走
    `_vl_extract(..., allow_retry=True)`——**它也會重試**、`:3631` unknown 分支是
    table/terminal 各一次、不重試、不取第二樣本）。**不得再自行推導**：

    - 已知 kind 的 `max` 是 `2T(1+R)` 而不是 `T(1+R)+T`——第二次取樣**也會重試**。
      少算的話（`R=1` 時說 3、實際 4）預算閘會在**跑完 3 次 VL 之後**才於 runtime 中止，
      而不是在任何呼叫之前拒絕，違反 workflow §4 Step 2。
    - `KIND_UNKNOWN` 的 `max` 恰好是 `2T`，不再加第二次取樣——高估會**錯擋**本來在
      預算內的文件。
    - `anchored` **只影響 `min`**（樂觀下界）；`max` 不看它。

    lane 一律走 **`figure_extract.read_native_lane()`**（契約 §17.4 的唯一 exact-bool
    reader；signal 由本模組的 `_resolve_native_lane()` 產生），**這裡不得自己另算、
    也不得用 truthiness**——三處各算各的正是總審第一輪 BLOCKER #1，而型別語義不一致
    （`"false"` 在 RAG 是錯誤、在別處卻 truthy → 跳過 VL capability probe）是第二輪的
    BLOCKER。實務差別：word-only 的向量 terminal `native_lane=False`，所以它
    **要**算 VL 預算；有 `pos` 支撐的 terminal 與有 `native_table` 的 table 才是 0。

    **native lane 永遠不呼叫 VL**（契約 §12.1）：純文字 + 原生表格的 PDF 是零 VL 呼叫，
    `vl_calls_max` 也**不含**「native 未達 native_verified 時的視覺 corroboration」。

    **signal 缺失或型別不對一律 `FigureExtractionError`（不再當 False）**：本模組是
    signal 的產生端，自己產出的候選不可能缺這個 key；缺了代表有人繞過 `Candidate`
    的建構路徑，那是 bug，不是「保守估高一點」的情境。猜一個預設值等於把「三處不
    一致」換成「一處無聲不一致」（契約 §17.4）。
    """
    read_native_lane = _fx().read_native_lane
    plan = candidate.signals.get("tile_plan") or {}
    tiles = len(plan.get("tiles") or [])
    tokens = list(plan.get("est_tokens") or [])
    base_tokens = sum(tokens)
    if read_native_lane(candidate):
        return {"tiles": tiles, "min": 0, "max": 0, "tokens_min": 0, "tokens_max": 0}
    # `is not None`，**不是** truthiness：舊版存的是 admitted position，代表在
    # position 0 時值是 `0` → falsy → duplicate 被算成要跑滿 VL（方向雖是高估，
    # 但那正是 §17.4 `"false"` 被當 truthy 的同一類坑）。現在存的是 figure_id，
    # 這裡仍用 `is not None` 讓「值存在」與「值真假」永遠分開。
    if candidate.signals.get("vl_shared_with") is not None:
        return {"tiles": tiles, "min": 0, "max": 0, "tokens_min": 0, "tokens_max": 0}
    retries = max(0, int(config.FIGURE_EXTRACT_RETRIES))
    anchored = bool(candidate.signals.get("anchored"))
    unknown = candidate.kind == _fx().KIND_UNKNOWN
    if unknown:
        # dual pass：table、terminal 各一次 attempt，**不重試、不取第二樣本**
        min_mult = max_mult = 2
    else:
        # 樂觀下界：有 anchor 就假設對得上（不必第二次取樣）
        min_mult = 1 if anchored else 2
        # 保守上界：第一次抽取與第二次取樣**都各自會重試**（`attempts = 1 + retries`）
        max_mult = 2 * (1 + retries)
    return {
        "tiles": tiles,
        "min": tiles * min_mult,
        "max": tiles * max_mult,
        "tokens_min": base_tokens * min_mult,
        "tokens_max": base_tokens * max_mult,
    }


def _dataclass_replace(plan: FigurePlan, **changes) -> FigurePlan:
    import dataclasses

    return dataclasses.replace(plan, **changes)


def _degraded_plan(document_id: str, stats: dict, page_evidence: dict) -> FigurePlan:
    preflight = {
        "candidates": 0, "tiles": 0, "vl_calls_min": 0, "vl_calls_max": 0,
        "image_tokens_est": 0, "pages": len(page_evidence), "native_tables": 0,
        "image_tokens_min": 0, "candidates_detected": 0, "dropped_candidates": 0,
        "deferred_to_legacy_lane": len(stats.get("deferred_to_legacy_lane") or []),
    }
    return FigurePlan(document_id=document_id, candidates=[], page_evidence=page_evidence,
                      stats=stats, preflight=preflight, over_budget=[])


def plan_document_figures(file_path: str, pages: list[dict], *, root: str | Path,
                          pdf_doc=None) -> FigurePlan:
    """採集全文件 evidence、產生候選、算出 preflight 數字。

    **絕不 raise、絕不 print**（`RAG.extract_pdf` 的既有測試斷言 `"[WARN]" not in out`）。
    **絕不呼叫 VL、不算 embedding、不碰 KB。**

    `pdf_doc` 為 None 時本函式會自己開檔，並在回傳前**關掉自己開的那個**（呼叫端傳進來的
    永遠不由本模組關閉）。之後還要 `render_candidate_variants()` 的呼叫端請自己開檔並傳
    `pdf_doc`（`RAG.extract_pdf_document` 本來就是這樣做）。

    身分無法建立時（檔案不存在、不在 root 內、讀取期間被改寫）→ 回**零候選的降級 plan**，
    `document_id=""`。**不製造 `basename::unresolved` 之類的假 ID**：不同路徑的同名 PDF
    會碰撞，後續 figure ID / re-ingest / 人工 verification 都會錯套（審核 BLOCKER 9）。
    """
    fx = _fx()
    stats: dict = {
        "source_path": str(Path(file_path)),
        "root": str(Path(root)) if root is not None else "",
        "display_name": Path(file_path).name,
        "pages_seen": len(pages or []),
        "pages_harvested": 0,
        "page_number_mismatch": [],
        "unavailable_channels": {},
        "page_box_space": {},
        "degenerate_tables": [],
        "deferred_to_legacy_lane": [],
        "dropped_candidates": [],
        "duplicate_assets_shared": [],
        "candidates_detected": 0,
        "candidates_admitted": 0,
        "fusion_components": 0,
    }

    document_id = ""
    try:
        document_id = fx.document_id_for(file_path, root)
        stats["display_name"] = fx.display_name_for(document_id)
    except Exception as exc:  # noqa: BLE001
        stats["identity_unavailable"] = _slug(exc)

    owned = False
    document = pdf_doc
    if document is None:
        try:
            import pymupdf

            document = pymupdf.open(file_path)
            owned = True
        except Exception as exc:  # noqa: BLE001
            document = None
            stats["document_unavailable"] = _slug(exc)

    try:
        page_evidence: dict = {}
        page_count = None
        if document is not None:
            try:
                page_count = int(document.page_count)
            except Exception:  # noqa: BLE001
                page_count = None

        detected: list[dict] = []
        for physical_index, page_info in enumerate(pages or []):
            if not isinstance(page_info, dict):
                continue
            page_num = _page_number(page_info.get("metadata"))
            mismatch = ""
            if page_num != physical_index + 1:
                mismatch = "metadata_page_not_physical_index"
            elif page_count is not None and not (1 <= page_num <= page_count):
                mismatch = "page_out_of_document_range"
            evidence = harvest_page_evidence(
                page_info, pdf_doc=document, page_index=physical_index, page_number=page_num)
            stats["pages_harvested"] += 1
            if evidence.page in page_evidence:
                mismatch = mismatch or "duplicate_page_number"
            else:
                page_evidence[evidence.page] = evidence
            if evidence.unavailable:
                stats["unavailable_channels"][str(evidence.page)] = list(evidence.unavailable)
            if evidence.page_boxes:
                stats["page_box_space"][str(evidence.page)] = evidence.page_boxes[0].get("_space", "")
            for strategy in _TABLE_STRATEGIES:
                for entry in evidence.tables.get(strategy) or []:
                    if entry.get("degenerate"):
                        stats["degenerate_tables"].append({
                            "page": evidence.page, "strategy": strategy,
                            "ordinal": entry["ordinal"], "errors": list(entry["errors"]),
                        })
            if mismatch:
                # 頁碼對不上 physical index → 該頁 abstain（evidence 還是留著給人看）
                stats["page_number_mismatch"].append({
                    "physical_index": physical_index,
                    "metadata_page": page_num,
                    "reason": mismatch,
                })
                continue
            if not document_id:
                continue
            # 契約 §13.2：本函式絕不 raise。偵測器自己出意外時只記 slug 並讓那一頁 abstain，
            # 整份 ingest 不會因為單頁畸形資料而掛掉（legacy lane 仍照舊處理該頁）。
            try:
                detected.extend(_page_candidates(evidence, document_id=document_id,
                                                 pdf_doc=document, stats=stats))
            except Exception as exc:  # noqa: BLE001
                stats.setdefault("candidate_errors", []).append(
                    {"page": evidence.page, "error": _slug(exc), "stage": "detect"})

        stats["deferred_to_legacy_lane"].sort(key=lambda e: (e["page"], e["bbox"][1], e["bbox"][0]))
        if not document_id:
            return _degraded_plan("", stats, page_evidence)

        stats["candidates_detected"] = len(detected)
        over_budget: list[str] = []

        # 每頁上限：保留分數最高的，其餘**逐筆列出**（不無聲截斷）
        per_page_cap = int(config.FIGURE_MAX_CANDIDATES_PER_PAGE)
        by_page: dict[int, list[dict]] = {}
        for item in detected:
            by_page.setdefault(item["page"], []).append(item)
        admitted: list[dict] = []
        for page_num in sorted(by_page):
            group = by_page[page_num]
            if len(group) > per_page_cap:
                over_budget.append(f"candidates_per_page:{page_num}")
                ranked = sorted(group, key=lambda i: (-i["score"], i["bbox"][1], i["bbox"][0]))
                keep = ranked[:max(0, per_page_cap)]
                for dropped in ranked[max(0, per_page_cap):]:
                    stats["dropped_candidates"].append({
                        "page": dropped["page"], "bbox": list(dropped["bbox"]),
                        "kind": dropped["kind"], "score": dropped["score"],
                        "reason": "candidates_per_page",
                    })
                group = sorted(keep, key=lambda i: (i["bbox"][1], i["bbox"][0]))
            admitted.extend(group)
        if len(detected) > int(config.FIGURE_MAX_CANDIDATES_PER_DOC):
            over_budget.append("candidates_per_doc")
        for entry in stats.get("raw_region_overflow") or []:
            over_budget.append(f"raw_regions_per_page:{entry['page']}")
        stats["dropped_candidates"].sort(key=lambda e: (e["page"], e["bbox"][1], e["bbox"][0]))
        stats["candidates_admitted"] = len(admitted)

        admitted.sort(key=lambda i: (i["page"], i["bbox"][1], i["bbox"][0]))

        # 重複 asset：**保留每一個 physical candidate**（每頁有自己的 pos / bbox / evidence），
        # 只共享 VL 計算（審核 BLOCKER 12）。
        #
        # ★ 共享的前提是「真的有一個 VL producer 會產生結果可以沿用」：
        # 只有 **同 requested kind 且雙方都走 VL lane** 的候選才能共享。native lane 的
        # 候選根本不會產生 VL 結果，把 VL 候選標成共享它會讓 preflight 算成零次、實際
        # 卻沒有任何結果可沿用（低報預算 + 繞過 probe 時機；local review BLOCKER #4）。
        #
        # ★★ 共享鍵一律是 **(asset_digest, planner requested kind)**（契約 §19.3）。
        # `requested kind` 就是 `Candidate.kind`，**可能是 `unknown`**；verifier 在
        # dual pass 之後會把它解歧成 table/terminal，若 cache 只存**解歧後**的 kind，
        # 第二筆再拿 `unknown` 去比就永遠命不中——實測兩個 duplicate unknown 候選跑出
        # 4 次 VL，preflight 卻宣稱第二筆是 0。所以 planner 把 requested kind 明確寫進
        # `signals["vl_share_key"]`，verifier 端據此同時保留 requested 與 resolved kind。
        occurrences_by_digest: dict[str, list[dict]] = {}
        share_representative: dict[tuple, int] = {}
        for position, item in enumerate(admitted):
            digest = item["asset_digest"]
            occurrences_by_digest.setdefault(digest, []).append({
                "page": item["page"], "bbox": list(item["bbox"]),
                "index": sum(1 for o in occurrences_by_digest.get(digest, [])
                             if o["page"] == item["page"]) + 1,
            })
            if not item["signals"].get("native_lane"):
                share_representative.setdefault((digest, item["kind"]), position)

        candidates: list[Candidate] = []
        position_to_candidate: dict[int, int] = {}   # admitted 位置 → candidates 索引
        for position, item in enumerate(admitted):
            digest = item["asset_digest"]
            signals = dict(item["signals"])
            try:
                # `native_lane` 的 exact-bool 不變式在**產生端**就釘住：下游三個消費端
                # 都走 `figure_extract.read_native_lane()`（契約 §17.4），它對非 bool
                # fail-loud，所以這裡壞掉要走既有的 `planning_error` 致命路徑，
                # 不能讓一個型別錯的 signal 流出去。
                if not isinstance(signals.get("native_lane"), bool):
                    raise TypeError(
                        f"native_lane 必須是精確 bool，收到 {signals.get('native_lane')!r}")
                signals["tile_plan"] = _plan_tiles(item)
                figure_id = fx.figure_id_for(document_id, item["page"], item["bbox"],
                                             item["page_rect"], digest)
            except Exception as exc:  # noqa: BLE001
                stats.setdefault("candidate_errors", []).append(
                    {"page": item["page"], "error": _slug(exc), "stage": "build"})
                stats["dropped_candidates"].append({
                    "page": item["page"], "bbox": list(item["bbox"]),
                    "kind": item["kind"], "score": item["score"],
                    "reason": "candidate_build_error",
                })
                continue
            share_key = (digest, item["kind"])
            representative = share_representative.get(share_key)
            if not signals.get("native_lane"):
                # 每個 VL lane 候選都帶同一把鍵（代表與 duplicate 都有），verifier 的
                # cache 才對得起來；`requested_kind` 就是這裡的 `Candidate.kind`。
                signals["vl_share_key"] = {"asset_digest": digest,
                                           "requested_kind": item["kind"]}
                signals["vl_share_role"] = (
                    "duplicate" if (representative is not None and representative != position)
                    else "representative")
            if (not signals.get("native_lane") and representative is not None
                    and representative != position):
                # ★ 直接存**代表的 figure_id**，不存 admitted position。
                # 存索引的話代表在 position 0（最常見：第一個 occurrence 就是代表）
                # 會得到 `0`，任何 truthiness 判斷都會把它讀成「沒有共享」→ preflight
                # 把 duplicate 算成要跑滿 VL。這與 §17.4 的 `native_lane` 是同一類坑，
                # 所以連「先存索引、事後轉換」的中間狀態都不留。
                # 代表在同一個迴圈的更前面就建好了（`share_representative` 取的是最小
                # position）；若它建構失敗被丟掉，就不建立共享關係，這筆自己跑一次 VL。
                target = position_to_candidate.get(representative)
                if target is not None:
                    signals["vl_shared_with"] = candidates[target].figure_id
            position_to_candidate[position] = len(candidates)
            candidates.append(Candidate(
                index=position + 1, page=item["page"], bbox=item["bbox"],
                page_rect=item["page_rect"], kind_scores=item["kind_scores"],
                kind=item["kind"], signals=signals, reasons=item["reasons"],
                signature=item["signature"], native_table=item["native_table"],
                occurrences=list(occurrences_by_digest[digest]),
                asset_xref=item["asset_xref"], asset_digest=digest,
                figure_id=figure_id, document_id=document_id,
            ))
        by_share: dict[tuple, list[int]] = {}
        for position, item in enumerate(admitted):
            target = position_to_candidate.get(position)
            if target is None or item["signals"].get("native_lane"):
                continue
            by_share.setdefault((item["asset_digest"], item["kind"]), []).append(target)
        for (digest, requested_kind), indices in sorted(by_share.items()):
            if len(indices) > 1:
                stats["duplicate_assets_shared"].append({
                    "asset_digest": digest,
                    # **planner requested kind**（可能是 `unknown`）——共享鍵的一半，
                    # 不是 verifier 解歧後的 kind（契約 §19.3）
                    "requested_kind": requested_kind,
                    "representative": candidates[indices[0]].figure_id,
                    "figure_ids": [candidates[i].figure_id for i in indices],
                    "pages": [candidates[i].page for i in indices],
                })

        tiles_total = 0
        vl_min = 0
        vl_max = 0
        tokens_min = 0
        tokens_max = 0
        native_tables = 0
        native_lane_count = 0
        per_call = int(config.FIGURE_MAX_IMAGE_TOKENS_PER_CALL)
        tile_cap = int(config.FIGURE_MAX_TILES_PER_CANDIDATE)
        for candidate in candidates:
            plan = candidate.signals["tile_plan"]
            n_tiles = len(plan["tiles"])
            tiles_total += n_tiles
            if candidate.native_table:
                native_tables += 1
            if fx.read_native_lane(candidate):
                native_lane_count += 1
            if n_tiles > tile_cap:
                over_budget.append(f"tiles_per_candidate:{candidate.index}")
            if plan.get("over_budget") or any(t["est_image_tokens"] > per_call for t in plan["tiles"]):
                over_budget.append(f"image_tokens_per_call:{candidate.index}")
            if plan.get("glyph_px_below_min") and "glyph_below_min_px" not in candidate.reasons:
                candidate.reasons.append("glyph_below_min_px")
            profile = _vl_profile(candidate)
            vl_min += profile["min"]
            vl_max += profile["max"]
            tokens_min += profile["tokens_min"]
            tokens_max += profile["tokens_max"]

        if vl_max > int(config.FIGURE_MAX_VL_CALLS_PER_DOC):
            over_budget.append("vl_calls_per_doc")
        if tokens_max > int(config.FIGURE_MAX_IMAGE_TOKENS_PER_DOC):
            over_budget.append("image_tokens_per_doc")

        if stats.get("candidate_errors"):
            # ★ 偵測 / 建構的**非預期**例外（不是 channel 降級）代表我們不知道漏掉了什麼。
            # 交付部分候選 = structured table 悄悄消失、PDF 卻照樣部分寫入，違反北極星。
            # 維持「plan 不 raise」，但回**零候選的降級 plan** 並讓下一個 gate
            # (`check_preflight`) 明確 fail-loud（local review BLOCKER #9）。
            plan = _degraded_plan(document_id, stats, page_evidence)
            return _dataclass_replace(plan, over_budget=["planning_error"])

        preflight = {
            # ── 契約 §6.3 的七個凍結鍵 ──
            "candidates": len(candidates),
            "tiles": tiles_total,
            "vl_calls_min": vl_min,
            "vl_calls_max": vl_max,
            "image_tokens_est": tokens_max,
            "pages": len(page_evidence),
            "native_tables": native_tables,
            # ── 附加鍵（契約 §13.2 允許）──
            "image_tokens_min": tokens_min,
            # `native_tables` 是「有原生表格 evidence 的候選數」，`native_lane_candidates`
            # 是「零 VL 的候選數」。兩者不必相等：kind 是 UNKNOWN 的候選就算帶
            # native_table 也走 VL dual pass（契約 §15.1）。
            "native_lane_candidates": native_lane_count,
            "candidates_detected": len(detected),
            "dropped_candidates": len(stats["dropped_candidates"]),
            "deferred_to_legacy_lane": len(stats["deferred_to_legacy_lane"]),
        }
        return FigurePlan(document_id=document_id, candidates=candidates,
                          page_evidence=page_evidence, stats=stats,
                          preflight=preflight, over_budget=sorted(set(over_budget)))
    finally:
        if owned and document is not None:
            try:
                document.close()
            except Exception:  # noqa: BLE001
                pass


def check_preflight(plan: FigurePlan) -> None:
    """超出任何上限就 `FigureBudgetError`。

    **這一關在任何 VL 呼叫、embedding 計算與 KB mutation 之前**（契約 §5 / workflow §5
    tool-runtime ⑤）。本模組到此為止沒有做過上述任何一件事。
    """
    if plan.over_budget:
        raise _fx().FigureBudgetError(format_preflight_report(plan))


def _limit_for(item: str) -> tuple[str, str]:
    name = item.split(":", 1)[0]
    table = {
        "candidates_per_page": ("每頁候選", str(config.FIGURE_MAX_CANDIDATES_PER_PAGE)),
        "candidates_per_doc": ("每份文件候選", str(config.FIGURE_MAX_CANDIDATES_PER_DOC)),
        "vl_calls_per_doc": ("每份文件 VL 呼叫", str(config.FIGURE_MAX_VL_CALLS_PER_DOC)),
        "tiles_per_candidate": ("每個候選的 tile", str(config.FIGURE_MAX_TILES_PER_CANDIDATE)),
        "image_tokens_per_call": ("單次影像 token", str(config.FIGURE_MAX_IMAGE_TOKENS_PER_CALL)),
        "image_tokens_per_doc": ("每份文件影像 token", str(config.FIGURE_MAX_IMAGE_TOKENS_PER_DOC)),
        "raw_regions_per_page": ("fusion 前的原始 region",
                                 f"{MAX_RAW_REGIONS_PER_PAGE} / promoting {MAX_PROMOTING_REGIONS_PER_PAGE}"),
        "planning_error": ("偵測/建構發生非預期例外，structured lane 已整份停用", "0"),
    }
    return table.get(name, (name, "?"))


def format_preflight_report(plan: FigurePlan) -> str:
    """給人看的 preflight 報告（`RAG.py --preflight` 與 `FigureBudgetError` 共用）。

    **只含計數、頁碼、bbox 與穩定 slug，絕不含任何頁面文字 / cell 內容**（NDA，
    AGENTS.md §6）。含可直接複製貼上的 CLI 命令——MCP 端 `capture_output` 不會 streaming，
    開始後才逾時等於完全沒有提示（workflow §1 / §4 Step 2）。
    """
    pre = plan.preflight
    stats = plan.stats
    source = stats.get("source_path") or ""
    try:
        source_abs = str(Path(source).resolve()) if source else ""
    except OSError:
        source_abs = source
    kb_path = config.KNOWLEDGE_FILE
    lines = [f"[PREFLIGHT] {stats.get('display_name') or Path(source).name or '<unknown>'}"]
    if not plan.document_id:
        lines.append(f"  結構化 lane 已停用：無法建立文件身分（{stats.get('identity_unavailable', 'unknown')}）")
    lines.append(
        f"  頁數 {pre.get('pages', 0)} · 候選 {pre.get('candidates', 0)}"
        f"（偵測 {pre.get('candidates_detected', 0)}）· 原生表格 {pre.get('native_tables', 0)}"
        f" · tile {pre.get('tiles', 0)}")
    lines.append(
        f"  VL 呼叫 最少 {pre.get('vl_calls_min', 0)} / 最多 {pre.get('vl_calls_max', 0)}"
        f"（上限 {config.FIGURE_MAX_VL_CALLS_PER_DOC}）"
        f"；零 VL 的 native lane 候選 {pre.get('native_lane_candidates', 0)}")
    lines.append(
        f"  影像 token 估計 {pre.get('image_tokens_est', 0)}"
        f"（上限 {config.FIGURE_MAX_IMAGE_TOKENS_PER_DOC}；單次上限 "
        f"{config.FIGURE_MAX_IMAGE_TOKENS_PER_CALL}）——估算值，實際依 server/model 而異")
    for item in plan.over_budget:
        label, limit = _limit_for(item)
        suffix = item.split(":", 1)[1] if ":" in item else ""
        where = f"（{suffix}）" if suffix else ""
        lines.append(f"  超出上限：{item}{where} — {label} 上限 {limit}")
    deferred = stats.get("deferred_to_legacy_lane") or []
    if deferred:
        counts: dict[str, int] = {}
        for entry in deferred:
            counts[entry["reason"]] = counts.get(entry["reason"], 0) + 1
        detail = "、".join(f"{k} {v}" for k, v in sorted(counts.items()))
        lines.append(f"  已延後給既有 picture lane：{len(deferred)}（{detail}）")
    dropped = stats.get("dropped_candidates") or []
    if dropped:
        lines.append(f"  被丟棄的候選（不無聲截斷）：{len(dropped)}")
        for entry in dropped:
            lines.append(f"    第 {entry['page']} 頁 bbox={entry['bbox']} kind={entry['kind']}"
                         f" reason={entry['reason']}")
    low_glyph = [c for c in plan.candidates
                 if (c.signals.get("tile_plan") or {}).get("glyph_px_below_min")]
    if low_glyph:
        lines.append("  低於最小字高的候選（提高 DPI 不會創造原檔沒有的資訊）：")
        for candidate in low_glyph:
            tile_plan = candidate.signals["tile_plan"]
            lines.append(
                f"    #{candidate.index} 第 {candidate.page} 頁 估 {tile_plan['glyph_px']}px"
                f" < {config.FIGURE_MIN_GLYPH_PX}px")
    mismatch = stats.get("page_number_mismatch") or []
    if mismatch:
        lines.append(f"  頁碼與實體頁對不上而略過的頁：{len(mismatch)}"
                     f"（{'、'.join(sorted({m['reason'] for m in mismatch}))}）")
    quoted_source = shlex.quote(source_abs or source or "<pdf>")
    quoted_kb = shlex.quote(kb_path)
    lines.append("  建議改用 CLI（MCP 端不會 streaming，開始後才逾時等於沒有提示）：")
    lines.append(f"    python3 RAG.py {quoted_source} {quoted_kb} --preflight")
    lines.append(f"    python3 RAG.py {quoted_source} {quoted_kb}")
    return "\n".join(lines)
