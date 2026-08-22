"""figure_candidates（T3）的契約防護：候選升格、座標、取像、preflight。

契約：`wf/CONTRACT.md` §6.3 / §12.4 / §13.1–§13.2；workflow §4 Step 2、§5、§8。

這個檔守的是**無聲失敗**（AGENTS.md §2.4 第二類）：

- 沒有結構性證據卻宣稱有表 / 有 log（會把 legacy picture lane 的圖搶走，
  既有 `tests/test_rag_pdf_ingest.py::test_real_pymupdf4llm_contract` 會紅）
- `pos` / bbox 被靜默改值（`int(1.9)`、`int("3")`）→ 切到錯的原文
- 文件身分建不起來時捏一個假 ID → 同名不同路徑的 PDF 互相套用 human verification
- metadata 頁碼與實體頁對不上 → A 頁的 evidence 配到 B 頁的像素
- preflight 少算 → 開始跑之後才爆，而 MCP 端 `capture_output` 不會 streaming

**smoke 的部分完全不需要 pymupdf**（`_FakePage` 是純記憶體的假頁），
真 pymupdf / 真 PDF 的大矩陣一律非 smoke 並 `pytest.importorskip`（契約 §8）。
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
import shlex
import sys
import types
from pathlib import Path

import pytest

import config
import figure_candidates as fc
import figure_extract as fe

# ============================================================
# 純記憶體的假 pymupdf 頁（smoke 用；規劃路徑本來就不需要 pymupdf）
# ============================================================
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _rot90_matrices(width: float, height: float):
    """/Rotate 90 的 rotation / derotation matrix（與 pymupdf 實測值同形）。"""
    return (0.0, 1.0, -1.0, 0.0, height, 0.0), (0.0, -1.0, 1.0, 0.0, 0.0, height)


class _FakeHeader:
    def __init__(self, names, bbox, external=False):
        self.names = names
        self.bbox = bbox
        self.external = external


class _FakeRow:
    def __init__(self, cells):
        self.cells = cells


class _FakeTable:
    """假 `pymupdf.table.Table`；`raises` 內的屬性會像上游一樣丟 ValueError。"""

    def __init__(self, bbox, cells, header_names=None, extract=None, raises=()):
        self._bbox = bbox
        self._cells = cells
        self._header = _FakeHeader(header_names or [], bbox) if header_names else None
        self._extract = extract or []
        self._raises = set(raises)

    def _guard(self, name):
        if name in self._raises:
            raise ValueError(f"{name}() iterable argument is empty")

    @property
    def bbox(self):
        self._guard("bbox")
        return self._bbox

    @property
    def row_count(self):
        self._guard("row_count")
        return len(self._cells)

    @property
    def col_count(self):
        self._guard("col_count")
        return max((len(r) for r in self._cells), default=0)

    @property
    def header(self):
        self._guard("header")
        return self._header

    @property
    def rows(self):
        self._guard("rows")
        return [_FakeRow(row) for row in self._cells]

    def extract(self):
        self._guard("extract")
        return self._extract


class _FakeFinder:
    def __init__(self, tables):
        self.tables = list(tables)


class _FakePage:
    """只實作 `figure_candidates` 會碰到的 API 面。"""

    def __init__(self, *, rect=(0.0, 0.0, 612.0, 792.0), rotation=0, words=(),
                 images=(), drawings=(), tables=None, annots=(), widgets=(),
                 fail=()):
        self.rect = types.SimpleNamespace(x0=rect[0], y0=rect[1], x1=rect[2], y1=rect[3])
        self.rotation = rotation
        if rotation:
            self.rotation_matrix, self.derotation_matrix = _rot90_matrices(
                rect[2] - rect[0], rect[3] - rect[1])
        else:
            self.rotation_matrix = IDENTITY
            self.derotation_matrix = IDENTITY
        self._words = list(words)
        self._images = list(images)
        self._drawings = list(drawings)
        self._tables = tables or {}
        self._annots = list(annots)
        self._widgets = list(widgets)
        self._fail = set(fail)

    def _guard(self, name):
        if name in self._fail:
            raise RuntimeError(f"{name} unavailable (stub)")

    def get_text(self, kind):
        self._guard("words")
        assert kind == "words"
        return list(self._words)

    def get_image_info(self, hashes=False, xrefs=False):
        self._guard("image_info")
        del hashes, xrefs
        return copy.deepcopy(self._images)

    def get_drawings(self):
        self._guard("drawings")
        return [{"rect": types.SimpleNamespace(x0=r[0], y0=r[1], x1=r[2], y1=r[3])}
                for r in self._drawings]

    def cluster_drawings(self, drawings=None):
        del drawings
        if not self._drawings:
            return []
        xs0 = min(r[0] for r in self._drawings)
        ys0 = min(r[1] for r in self._drawings)
        xs1 = max(r[2] for r in self._drawings)
        ys1 = max(r[3] for r in self._drawings)
        return [types.SimpleNamespace(x0=xs0, y0=ys0, x1=xs1, y1=ys1)]

    def find_tables(self, strategy="lines"):
        self._guard("find_tables")
        return _FakeFinder(self._tables.get(strategy, []))

    def annots(self):
        self._guard("annots")
        return [_overlay_item(r) for r in self._annots]

    def widgets(self):
        self._guard("widgets")
        return [_overlay_item(r) for r in self._widgets]


class _BrokenRect:
    """`rect` getter 會丟例外的 annotation（harvest 必須逐 item guard，不得 raise）。"""

    @property
    def rect(self):
        raise RuntimeError("annotation rect unavailable (stub)")


def _overlay_item(rect):
    if rect is _BROKEN:
        return _BrokenRect()
    return types.SimpleNamespace(rect=types.SimpleNamespace(
        x0=rect[0], y0=rect[1], x1=rect[2], y1=rect[3]))


_BROKEN = "broken"


class _FakeDoc:
    def __init__(self, pages, *, images=None):
        self._pages = list(pages)
        self.page_count = len(self._pages)
        self.closed = 0
        self._images = images or {}

    def __getitem__(self, index):
        return self._pages[index]

    def extract_image(self, xref):
        if xref not in self._images:
            raise ValueError(f"no image at xref {xref}")
        return dict(self._images[xref])

    def close(self):
        self.closed += 1


def _word(x0, y0, text, *, size=10.0, char_w=5.0):
    return (x0, y0, x0 + char_w * len(text), y0 + size, text, 0, 0, 0)


def _register_rows(top=100.0, pitch=18.0, x=(72.0, 180.0, 300.0, 380.0)):
    """四欄 register 表的 word list（無框線；欄位嚴格左對齊）。"""
    rows = [
        ("Name", "Address", "Bits", "Access"),
        ("CTRL0", "0x4000_0100", "[7:4]", "RW"),
        ("CTRL1", "0x4000_0104", "[3:0]", "RO"),
        ("CTRL2", "0x4000_0108", "[15:8]", "RW"),
    ]
    words = []
    for row_index, row in enumerate(rows):
        y = top + row_index * pitch
        for col_index, cell in enumerate(row):
            words.append(_word(x[col_index], y, cell))
    return words


def _log_lines(top=400.0, pitch=13.0, count=5):
    lines = [
        "$ dmesg | tail",
        "[    0.000000] Booting kernel",
        "[    0.123456] INFO ready 0xdeadBEEF",
        "[    1.000000] WARN retry",
        "$ echo done",
    ]
    words = []
    for index in range(count):
        text = lines[index % len(lines)]
        y = top + index * pitch
        x = 72.0
        for token in text.split(" "):
            words.append(_word(x, y, token, size=9.0, char_w=5.4))
            x += 5.4 * (len(token) + 1)
    return words


def _page_dict(page_number, text, boxes=()):
    return {"metadata": {"page_number": page_number}, "text": text,
            "page_boxes": [dict(b) for b in boxes]}


def _plan(pages, doc, tmp_path, *, name="spec.pdf"):
    """跑真的 planner。檔案真的存在（`document_id_for` 需要讀 bytes）。"""
    pdf = tmp_path / name
    if not pdf.exists():
        pdf.write_bytes(b"%PDF-1.7 fake bytes for identity only")
    return fc.plan_document_figures(str(pdf), pages, root=tmp_path, pdf_doc=doc)


TABLE_MD = "|Name|Address|Bits|Access|\n|---|---|---|---|\n|CTRL0|0x4000_0100|[7:4]|RW|\n"

# native table geometry fixtures（契約 §12.4：cell matrix 必須與 row/col count 完全一致）
_CELL_GEOMETRY = {
    "row_count": 2, "col_count": 2,
    "cells": [[(0.0, 0.0, 10.0, 5.0), (10.0, 0.0, 20.0, 5.0)],
              [(0.0, 5.0, 10.0, 10.0), (10.0, 5.0, 20.0, 10.0)]],
}
_NONE_CELL_GEOMETRY = {"row_count": 2, "col_count": 2,
                       "cells": [[None, None], [None, None]]}
_RAGGED_GEOMETRY = {"row_count": 2, "col_count": 2,
                    "cells": [[(0.0, 0.0, 10.0, 5.0)],
                              [(0.0, 5.0, 10.0, 10.0), (10.0, 5.0, 20.0, 10.0)]]}


# ============================================================
# smoke：dataclass 形狀（審核 BLOCKER 1）
# ============================================================
@pytest.mark.smoke
def test_mutable_dataclass_defaults_are_per_instance():
    """`fallback` / `overlays` / `stitch` 必須是 `field(default_factory=dict)`。

    寫成 `dict = {}` 的話 dataclass 在 **import 期**就 `ValueError: mutable default`；
    就算繞過去，兩個 instance 也會共用同一份 dict。
    """
    a = fc.PageEvidence(page=1, raw_markdown="", page_boxes=[], words=[], image_info=[],
                        tables={}, drawing_clusters=[], page_rect=(0, 0, 1, 1),
                        rotation=0, unavailable=[])
    b = fc.PageEvidence(page=2, raw_markdown="", page_boxes=[], words=[], image_info=[],
                        tables={}, drawing_clusters=[], page_rect=(0, 0, 1, 1),
                        rotation=0, unavailable=[])
    a.fallback["x"] = 1
    a.overlays["y"] = 1
    assert b.fallback == {} and b.overlays == {}

    def _variant():
        # 契約 §21.3：fixture 必須填齊 §6.3 的每一個欄位，`digest` 是**真的** sha256。
        # 缺欄位 / 假 digest 的 fixture 會掩蓋 producer 漂移（本接縫連續四輪的成因）。
        png = b"x"
        return fc.Variant(figure_id="fig_0123456789abcdef", variant_id="raster", png=png,
                          width=1, height=1, bbox=(0, 0, 1, 1), tile_index=0, tile_total=1,
                          overlap_px=0, est_image_tokens=1,
                          digest=hashlib.sha256(png).hexdigest())

    v1, v2 = _variant(), _variant()
    v1.stitch["z"] = 1
    assert v2.stitch == {}
    assert v2.mime == "image/png"
    # 預設值填出來的 Variant 也必須是**合法**的 Variant（共享 validator 說了算）
    fe.validate_variant(v2, where="dataclass 預設值 fixture")


# ============================================================
# smoke：假 Document → 全 channel unavailable → 零候選（契約 §6.3）
# ============================================================
@pytest.mark.smoke
def test_fake_document_yields_no_structural_candidates(tmp_path: Path):
    """`types.SimpleNamespace(page_count=..., close=...)`：什麼 API 都沒有。

    這正是 `tests/test_rag_pdf_ingest.py` 的既有 stub。所有結構性 channel 都不可用的頁
    **不得**產生任何候選——沒有證據就不宣稱，legacy picture lane 因此完整保留。
    連 `class=table` + 合法 `pos` 都不夠：頁物件拿不到就沒有任何東西能佐證那個框。
    """
    pages = [_page_dict(1, TABLE_MD + "x" * 40, [
        {"class": "picture", "bbox": (72, 110, 300, 340)},
        {"class": "table", "bbox": (72, 110, 300, 340), "pos": (0, len(TABLE_MD))},
    ])]
    stub = types.SimpleNamespace(page_count=9999, close=lambda: None)
    plan = _plan(pages, stub, tmp_path)

    assert plan.candidates == []
    assert plan.preflight["vl_calls_max"] == 0
    assert plan.over_budget == []
    unavailable = plan.stats["unavailable_channels"]["1"]
    for channel in ("page_rect", "words", "image_info", "find_tables", "drawings"):
        assert any(u.startswith(f"{channel}:") for u in unavailable), (
            f"{channel} 必須明確標成不可用，實際: {unavailable}")


# ============================================================
# smoke：preflight 是 VL / embedding / KB 之前的閘（workflow §5 tool ⑤）
# ============================================================
@pytest.mark.smoke
def test_planner_module_cannot_reach_vl_embedding_or_kb():
    """靜態契約：`figure_candidates` **不得**依賴 VL client / KB / RAG。

    這條用 AST 直接看 import，所以是真的守得住：只要有人在 planner 裡加一行
    `import llama_client`，這裡就紅。上一版用「patch 一堆函式再斷言 calls == []」
    的寫法是空的——planner 本來就不會碰到那些函式，`calls == []` 必然成立
    （local review BLOCKER #12）。

    端到端的「ingest orchestration 在 preflight 之前零副作用」由 T7 的
    `tests/test_figure_ingest.py` 守（那裡才真的跑 orchestration）。
    """
    module_path = Path(fc.__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"llama_client", "knowledge", "knowledge_store", "RAG", "requests",
                 "http_client", "urllib", "socket"}
    assert not (imported & forbidden), (
        f"planner 不得依賴 {sorted(imported & forbidden)}——preflight 必須在任何 VL / "
        "embedding / KB 動作之前完成")


@pytest.mark.smoke
def test_preflight_over_budget_raises_and_writes_nothing(tmp_path: Path, monkeypatch):
    """真的跑一次 planner 產生真的超限，再驗 `check_preflight` fail-loud 且**零寫入**。

    不是「合成一個 over_budget 的 FigurePlan 再呼叫一個 if」——那證明不了 planner
    的限額計算會不會漏算。零寫入用**整棵目錄樹的 (path, size, mtime_ns) 快照**驗，
    不是只看一個 KB 檔。
    """
    kb = tmp_path / "knowledge.json"
    kb.write_text(json.dumps({"metadata": {}, "chunks": []}), encoding="utf-8")
    pdf = tmp_path / "spec.pdf"
    pdf.write_bytes(b"%PDF-1.7 fake bytes for identity only")

    def _snapshot():
        return sorted((str(x), x.stat().st_size, x.stat().st_mtime_ns)
                      for x in tmp_path.rglob("*") if x.is_file())

    before = _snapshot()
    monkeypatch.setattr(config, "FIGURE_MAX_VL_CALLS_PER_DOC", 0)
    page = _FakePage(words=_log_lines(count=5))
    plan = _plan([_page_dict(1, "log page")], _FakeDoc([page]), tmp_path)

    assert plan.candidates, "測試前提：必須真的偵測到候選"
    assert plan.preflight["vl_calls_max"] > 0, "測試前提：這個候選要真的需要 VL"
    assert "vl_calls_per_doc" in plan.over_budget, plan.over_budget
    with pytest.raises(fe.FigureBudgetError) as exc:
        fc.check_preflight(plan)

    assert "vl_calls_per_doc" in str(exc.value)
    assert _snapshot() == before, "preflight 階段不得寫任何檔案"


# ============================================================
# smoke：升格閘（審核 BLOCKER 3）
# ============================================================
@pytest.mark.smoke
@pytest.mark.parametrize("label,page,boxes,text", [
    ("散文三行", _FakePage(words=[
        _word(72, 100, "The"), _word(95, 100, "NPU"), _word(130, 100, "has"),
        _word(72, 118, "eight"), _word(115, 118, "compute"), _word(180, 118, "cores"),
        _word(72, 136, "and"), _word(100, 136, "shared"), _word(150, 136, "memory"),
    ]), (), "prose"),
    ("三條裝飾線", _FakePage(drawings=[(72, 100, 400, 101), (72, 200, 400, 201),
                                       (72, 300, 400, 301)]), (), "lines"),
    ("只有 image_info + 非法 pos 的 table box",
     _FakePage(images=[{"bbox": (72, 100, 400, 400), "xref": 7, "width": 80, "height": 60,
                        "digest": b"\x01" * 16, "has-mask": False,
                        "transform": (328.0, 0.0, 0.0, 300.0, 72.0, 100.0)}]),
     ({"class": "table", "bbox": (72, 100, 400, 400), "pos": (0, 9999)},), "short"),
])
def test_regions_without_positive_structural_evidence_do_not_promote(
        label, page, boxes, text, tmp_path: Path):
    """散文、裝飾線、只有像素的區域都**不得**升格成 structured 候選。

    `image_info` 本身不是結構性 channel；`page_boxes:table` 沒有合法 `pos` 也不算。
    這三個負例是 legacy picture lane 不被搶走的最小保證。
    """
    pages = [_page_dict(1, text, boxes)]
    plan = _plan(pages, _FakeDoc([page]), tmp_path, name=f"{label}.pdf")
    assert plan.candidates == [], [
        (c.kind, c.bbox, c.reasons) for c in plan.candidates]


# ============================================================
# smoke：kind 路由（審核 BLOCKER 4 + 契約 §13.1）
# ============================================================
@pytest.mark.smoke
def test_kind_unknown_only_means_table_vs_terminal(monkeypatch):
    """`KIND_UNKNOWN` **只**代表 table 與 terminal 難分。

    舊寫法對三類一起取 top/second：diagram 第一、terminal 第二且差距小時會變成
    `unknown`，等於讓 diagram 繞過過濾進 structured lane。
    """
    monkeypatch.setattr(config, "FIGURE_KIND_MARGIN", 0.15)
    kind, reasons = fc._route_kind({fe.KIND_TABLE: 0.30, fe.KIND_TERMINAL: 0.35,
                                    fe.KIND_DIAGRAM: 0.40})
    assert kind == "" and reasons == ["kind_diagram_legacy_lane"], (kind, reasons)

    kind, _ = fc._route_kind({fe.KIND_TABLE: 0.50, fe.KIND_TERMINAL: 0.45,
                              fe.KIND_DIAGRAM: 0.10})
    assert kind == fe.KIND_UNKNOWN

    kind, _ = fc._route_kind({fe.KIND_TABLE: 0.80, fe.KIND_TERMINAL: 0.20,
                              fe.KIND_DIAGRAM: 0.79})
    assert kind == fe.KIND_TABLE, "diagram 只要沒有贏過 table/terminal 就不影響路由"


@pytest.mark.smoke
def test_diagram_kind_candidates_are_deferred_to_legacy_lane(tmp_path: Path):
    """向量圖（大量非直線圖元、無文字）→ 延後給 legacy picture lane，並記 reason slug。"""
    shapes = [(100 + i * 3, 100 + i * 3, 140 + i * 3, 150 + i * 3) for i in range(12)]
    page = _FakePage(drawings=shapes + [(100, 100, 400, 101), (100, 200, 400, 201),
                                        (100, 300, 400, 301), (100, 100, 101, 300),
                                        (400, 100, 401, 300)])
    plan = _plan([_page_dict(1, "figure page")], _FakeDoc([page]), tmp_path)
    reasons = {entry["reason"] for entry in plan.stats["deferred_to_legacy_lane"]}
    assert plan.candidates == []
    assert reasons & {"kind_diagram_legacy_lane", "raster_no_structural_evidence"}, reasons


# ============================================================
# smoke：pos / bbox 不得靜默改值（審核 BLOCKER 8）
# ============================================================
@pytest.mark.smoke
@pytest.mark.parametrize("pos", [
    (1.9, 20), ("0", 20), (True, 20), (0, 0), (20, 5), (0, 10_000), (-1, 5), None, (0,),
])
def test_invalid_pos_is_treated_as_missing(pos):
    """`int(1.9)` / `int("3")` 會做出看起來合法、實際切錯原文的 offset。

    只接受**真正的 int** 且 `0 <= start < end <= len(raw_markdown)`。
    """
    raw = TABLE_MD
    box = {"class": "table", "bbox": (72, 100, 400, 200)}
    if pos is not None:
        box["pos"] = pos
    evidence = fc.harvest_page_evidence(_page_dict(1, raw, [box]),
                                        pdf_doc=_FakeDoc([_FakePage()]), page_index=0)
    assert evidence.page_boxes[0]["_pos"] is None


@pytest.mark.smoke
@pytest.mark.parametrize("bbox", [
    (float("nan"), 0, 10, 10), (0, 0, float("inf"), 10), (100, 100, 50, 50),
    (10, 10, 10, 20), "not-a-bbox", (1, 2, 3), None, (True, 0, 10, 10),
])
def test_invalid_bbox_is_rejected(bbox):
    """NaN / Inf / 反向 / 退化 / 型別錯的 bbox 一律拒收（**不自動翻正**）。"""
    assert fc._as_bbox(bbox) is None


@pytest.mark.smoke
def test_bbox_outside_page_is_deferred_not_used(tmp_path: Path):
    """框落在 page_rect 之外（旋轉頁的上游幾何常這樣）→ abstain，不產生錯位候選。"""
    page = _FakePage(rect=(0, 0, 200, 200), words=_register_rows(top=1000.0))
    plan = _plan([_page_dict(1, "page")], _FakeDoc([page]), tmp_path)
    assert plan.candidates == []
    assert any(e["reason"] == "bbox_outside_page"
               for e in plan.stats["deferred_to_legacy_lane"]), \
        plan.stats["deferred_to_legacy_lane"]


# ============================================================
# smoke：身分（審核 BLOCKER 9）
# ============================================================
@pytest.mark.smoke
def test_identity_failure_gives_degraded_plan_without_fake_document_id(tmp_path: Path):
    """檔案不存在 / 不在 root 內 → 零候選的降級 plan，`document_id` 為空字串。

    **不得**捏 `basename::unresolved`：不同路徑的同名 PDF 會碰撞，後續 figure ID、
    re-ingest 與人工 verification 都可能錯套（契約 §13.4 也要求此時停用 structured lane）。
    """
    page = _FakePage(words=_register_rows())
    plan = fc.plan_document_figures(str(tmp_path / "missing.pdf"),
                                    [_page_dict(1, TABLE_MD)],
                                    root=tmp_path, pdf_doc=_FakeDoc([page]))
    assert plan.document_id == ""
    assert plan.candidates == []
    assert plan.stats["identity_unavailable"] == "FigureError"
    assert plan.over_budget == []
    assert plan.preflight["vl_calls_max"] == 0

    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF")
    plan2 = fc.plan_document_figures(str(outside), [_page_dict(1, TABLE_MD)],
                                     root=tmp_path / "elsewhere", pdf_doc=_FakeDoc([page]))
    assert plan2.document_id == ""
    assert plan2.candidates == []


# ============================================================
# smoke：頁碼與實體頁（審核 BLOCKER 10）
# ============================================================
@pytest.mark.smoke
def test_page_number_mismatch_abstains_instead_of_mispairing(tmp_path: Path):
    """metadata 頁碼與實體 index 對不上 → 該頁 abstain。

    harvest 用列舉 index、render 用 `candidate.page - 1`：不一致時繼續跑就會把
    A 頁的 evidence 配到 B 頁的像素。寧可不做。
    """
    pages = [_page_dict(1, TABLE_MD), _page_dict(7, TABLE_MD)]
    doc = _FakeDoc([_FakePage(words=_register_rows()), _FakePage(words=_register_rows())])
    plan = _plan(pages, doc, tmp_path)

    assert [c.page for c in plan.candidates] == [1]
    mismatch = plan.stats["page_number_mismatch"]
    assert mismatch and mismatch[0]["metadata_page"] == 7
    assert mismatch[0]["reason"] == "metadata_page_not_physical_index"


@pytest.mark.smoke
def test_page_beyond_document_range_abstains(tmp_path: Path):
    """metadata 說有第 2 頁但 PDF 只有 1 頁（畸形檔）→ 該頁 abstain。"""
    pages = [_page_dict(1, TABLE_MD), _page_dict(2, TABLE_MD)]
    doc = _FakeDoc([_FakePage(words=_register_rows())])
    plan = _plan(pages, doc, tmp_path)
    reasons = {m["reason"] for m in plan.stats["page_number_mismatch"]}
    assert reasons == {"page_out_of_document_range"}
    assert all(c.page == 1 for c in plan.candidates)


# ============================================================
# smoke：VL 呼叫狀態機與六類上限（審核 BLOCKER 16 / 17、契約 §12.1 / §12.4）
# ============================================================
@pytest.mark.smoke
def test_native_lane_costs_zero_vl_calls(tmp_path: Path):
    """契約 §12.1：native lane **永遠不呼叫 VL**。純文字 + 原生表格 = 零 VL。"""
    table = _FakeTable((70, 95, 460, 175),
                       [[(70, 95, 180, 115), (180, 95, 300, 115),
                         (300, 95, 380, 115), (380, 95, 460, 115)],
                        [(70, 115, 180, 135), (180, 115, 300, 135),
                         (300, 115, 380, 135), (380, 115, 460, 135)],
                        [(70, 135, 180, 155), (180, 135, 300, 155),
                         (300, 135, 380, 155), (380, 135, 460, 155)]],
                       header_names=["Name", "Address", "Bits", "Access"])
    page = _FakePage(words=_register_rows(),
                     tables={"lines": [table], "lines_strict": [table]})
    plan = _plan([_page_dict(1, TABLE_MD)], _FakeDoc([page]), tmp_path)
    assert plan.candidates
    assert plan.candidates[0].signals["native_lane"] is True
    assert plan.preflight["vl_calls_min"] == 0
    assert plan.preflight["vl_calls_max"] == 0
    assert plan.preflight["image_tokens_est"] == 0
    assert plan.preflight["native_tables"] >= 1
    assert plan.preflight["native_lane_candidates"] == 1


# ============================================================
# smoke：native lane 的單一真相（契約 §15.1 / 總審 BLOCKER #1）
# ============================================================
@pytest.mark.smoke
@pytest.mark.parametrize("kind,native_table,native_text,expected,slug", [
    (fe.KIND_TABLE, {"pos": (0, 5)}, None, True, "native_lane_table"),
    # 有真正的 cell geometry（每格都有合法 bbox）才算
    (fe.KIND_TABLE, {"pos": None, "geometry": _CELL_GEOMETRY}, None, True, "native_lane_table"),
    # 空 geometry / 全 None cell / ragged matrix 一律不算（local review BLOCKER #7）
    (fe.KIND_TABLE, {"pos": None, "geometry": {}}, None, False, "vl_lane_no_native_table"),
    (fe.KIND_TABLE, {"pos": None, "geometry": _NONE_CELL_GEOMETRY}, None,
     False, "vl_lane_no_native_table"),
    (fe.KIND_TABLE, {"pos": None, "geometry": _RAGGED_GEOMETRY}, None,
     False, "vl_lane_no_native_table"),
    (fe.KIND_TABLE, None, {"pos": (0, 5)}, False, "vl_lane_no_native_table"),
    (fe.KIND_TERMINAL, None, {"pos": (0, 5)}, True, "native_lane_terminal_pos"),
    (fe.KIND_TERMINAL, None, None, False, "vl_lane_word_only_terminal"),
    # word geometry 就算湊出了 native_table，terminal 仍然不算 native lane
    (fe.KIND_TERMINAL, {"pos": (0, 5)}, None, False, "vl_lane_word_only_terminal"),
    (fe.KIND_UNKNOWN, {"pos": (0, 5)}, {"pos": (0, 5)}, False, "vl_lane_kind_unknown"),
])
def test_native_lane_rule_is_the_single_truth(kind, native_table, native_text, expected, slug):
    """`_resolve_native_lane()` 是 planner / RAG probe / verifier 共用的**唯一**判定。

    三處各算各的正是總審 BLOCKER #1：同一個 word-only terminal 候選被 preflight 算成
    要 VL、卻被 verifier 當成零 VL 的 native lane，於是預算誤報、probe 誤擋。
    """
    lane, reasons = fc._resolve_native_lane(kind, native_table, native_text)
    assert lane is expected
    assert reasons == [slug]


@pytest.mark.smoke
def test_word_only_vector_terminal_costs_vl_budget(tmp_path: Path):
    """word-only 的向量 terminal **要**算 VL 預算（契約 §15.1）。

    `page.get_text("words")` 證明不了行首縮排、行尾空白與「完全沒有字的空行」，
    拿它合成 canonical 正文＝把猜出來的普通空格當原文（workflow §8-2 / 總審 BLOCKER #5）。
    """
    page = _FakePage(words=_log_lines(count=5))
    plan = _plan([_page_dict(1, "log page")], _FakeDoc([page]), tmp_path)

    candidate = plan.candidates[0]
    assert candidate.kind == fe.KIND_TERMINAL
    assert candidate.signals["native_lane"] is False
    assert candidate.signals["native_text"] is None
    assert "vl_lane_word_only_terminal" in candidate.reasons
    assert plan.preflight["vl_calls_min"] > 0
    assert plan.preflight["vl_calls_max"] > 0
    assert plan.preflight["native_lane_candidates"] == 0


@pytest.mark.smoke
def test_rotated_page_terminal_never_claims_native_lane(tmp_path: Path):
    """旋轉頁的 terminal 一律走 VL lane。

    本模組用校準過的 unrotated bbox 反查 pos，下游 verifier 用的是上游原始
    display bbox；旋轉頁上兩者對不起來，planner 宣稱 native lane 就會讓 verifier
    fail-loud 成**整份 PDF 零寫入**。實測 rotation != 0 時上游 markdown 本來就
    空掉或錯亂，那份原文也不值得當 canonical。
    """
    words = _log_lines(count=5)
    probe = _plan([_page_dict(1, "log")], _FakeDoc([_FakePage(words=words)]), tmp_path,
                  name="rotprobe.pdf")
    bbox = probe.candidates[0].bbox
    raw = "```\n$ dmesg | tail\n```\n"
    rotated_page = _FakePage(words=words, rotation=90)
    boxes = ({"class": "text", "bbox": bbox, "pos": (0, len(raw))},)
    plan = _plan([_page_dict(1, raw, boxes)], _FakeDoc([rotated_page]), tmp_path,
                 name="rotterm.pdf")
    assert plan.page_evidence[1].rotation == 90, "測試前提：這頁真的是旋轉頁"
    assert plan.candidates, "測試前提：旋轉頁上仍要偵測到候選，否則這條什麼都沒驗到"
    for candidate in plan.candidates:
        assert candidate.signals["native_lane"] is False
        assert candidate.signals["native_text"] is None


@pytest.mark.smoke
def test_pos_backed_terminal_is_native_lane_with_zero_vl(tmp_path: Path):
    """有一塊 `pos` 支撐的 raw markdown 文字 → native lane、零 VL、零 probe。"""
    words = _log_lines(count=5)
    probe = _plan([_page_dict(1, "log")], _FakeDoc([_FakePage(words=words)]), tmp_path,
                  name="probe.pdf")
    bbox = probe.candidates[0].bbox            # 讓 page_box 與候選共延
    raw = "```\n$ dmesg | tail\n[    0.000000] Booting kernel\n```\n"
    boxes = ({"class": "text", "bbox": bbox, "pos": (0, len(raw))},)

    plan = _plan([_page_dict(1, raw, boxes)], _FakeDoc([_FakePage(words=words)]), tmp_path,
                 name="posbacked.pdf")
    candidate = plan.candidates[0]
    assert candidate.kind == fe.KIND_TERMINAL
    assert candidate.signals["native_lane"] is True
    assert "native_lane_terminal_pos" in candidate.reasons
    native_text = candidate.signals["native_text"]
    assert native_text["pos"] == (0, len(raw))
    assert native_text["markdown"] == raw, "pos 必須切在 raw markdown（strip 之前）"
    assert native_text["source"] == "page_boxes:text"
    assert plan.preflight["vl_calls_min"] == 0
    assert plan.preflight["vl_calls_max"] == 0
    assert plan.preflight["image_tokens_est"] == 0
    assert plan.preflight["native_lane_candidates"] == 1


@pytest.mark.smoke
@pytest.mark.parametrize("label,boxes_factory", [
    # 同一塊 log 在 markdown 裡被拆成兩段 → 說不出哪一段是這個候選的正文
    ("兩個重疊的 text box", lambda bb: (
        {"class": "text", "bbox": bb, "pos": (0, 20)},
        {"class": "text", "bbox": (bb[0], bb[1], bb[2], bb[3] - 1.0), "pos": (20, 40)},
    )),
    # 實測 pymupdf4llm 會把每行 log 變成加過裝飾的 list-item（``- `$ dmesg` ``）
    ("list-item", lambda bb: ({"class": "list-item", "bbox": bb, "pos": (0, 40)},)),
    # 框對不上（IoU < FIGURE_IOU_MERGE）
    ("不共延的 text box", lambda bb: (
        {"class": "text", "bbox": (bb[0], bb[3] + 50.0, bb[2], bb[3] + 120.0), "pos": (0, 40)},
    )),
])
def test_ambiguous_or_decorated_pos_boxes_abstain_from_native_lane(
        label, boxes_factory, tmp_path: Path):
    """無法安全判定就 abstain → 走 VL lane（words 仍是 secondary anchor）。"""
    words = _log_lines(count=5)
    raw = "x" * 60
    probe = _plan([_page_dict(1, "log")], _FakeDoc([_FakePage(words=words)]), tmp_path,
                  name=f"probe-{label}.pdf")
    boxes = boxes_factory(probe.candidates[0].bbox)

    plan = _plan([_page_dict(1, raw, boxes)], _FakeDoc([_FakePage(words=words)]), tmp_path,
                 name=f"abstain-{label}.pdf")
    candidate = plan.candidates[0]
    assert candidate.signals["native_lane"] is False, label
    assert candidate.signals["native_text"] is None, label
    assert plan.preflight["vl_calls_max"] > 0, label


@pytest.mark.smoke
@pytest.mark.parametrize("retries", [0, 1, 3])
@pytest.mark.parametrize("kind,anchored,native,expect", [
    # 契約 §20 的凍結狀態表（主代理直接讀 figure_verify 的實際呼叫路徑訂出來）
    ("known", True, False, ("T", "2T(1+R)")),
    ("known", False, False, ("2T", "2T(1+R)")),
    ("unknown", True, False, ("2T", "2T")),
    ("unknown", False, False, ("2T", "2T")),
    ("known", True, True, ("0", "0")),
])
def test_vl_call_state_machine(kind, anchored, native, expect, retries, monkeypatch):
    """逐格驗 lane / anchor / kind / retry 的呼叫數 —— **契約 §20 的凍結狀態表**。

    `T` = tile 數、`R` = `config.FIGURE_EXTRACT_RETRIES`：

    | lane / 情境 | min | max |
    |---|---|---|
    | native lane | 0 | 0 |
    | duplicate（`vl_shared_with` 存在）| 0 | 0 |
    | VL, `KIND_UNKNOWN` | `2T` | `2T` |
    | VL, kind 已定, 有 anchor | `T` | `2T(1+R)` |
    | VL, kind 已定, 無 anchor | `2T` | `2T(1+R)` |

    這張表由主代理直接讀 `figure_verify.py` 的呼叫路徑訂出（`:1197`
    `attempts = 1 + retries`、`:3571-3577` 第二次取樣走 `_vl_extract(..., allow_retry=True)`
    ——**它也會重試**、`:3631` unknown 分支 table/terminal 各一次不重試不取第二樣本）。
    lane 一律讀 `signals["native_lane"]`（契約 §15.1 的單一真相），這裡不重算。
    """
    monkeypatch.setattr(config, "FIGURE_EXTRACT_RETRIES", retries)
    tiles = 3
    signals = {
        "tile_plan": {"tiles": [{"est_image_tokens": 10}] * tiles, "est_tokens": [10] * tiles},
        "anchored": anchored,
        "native_lane": native,
    }
    candidate = fc.Candidate(
        index=1, page=1, bbox=(0, 0, 10, 10), page_rect=(0, 0, 100, 100),
        kind_scores={}, kind=fe.KIND_UNKNOWN if kind == "unknown" else fe.KIND_TABLE,
        signals=signals, reasons=[], signature="", native_table=None, occurrences=[],
        asset_xref=None, asset_digest="d", figure_id="fig_0123456789abcdef",
        document_id="doc::0",
    )
    profile = fc._vl_profile(candidate)
    table = {
        "0": 0, "T": tiles, "2T": tiles * 2, "2T(1+R)": 2 * tiles * (1 + retries),
    }
    assert (profile["min"], profile["max"]) == (table[expect[0]], table[expect[1]])


@pytest.mark.smoke
@pytest.mark.parametrize("signals,label", [
    ({}, "缺 native_lane key"),
    ({"native_lane": None}, "None"),
    ({"native_lane": 0}, "int 0"),
    ({"native_lane": 1}, "int 1"),
    ({"native_lane": "false"}, "字串 false"),
    ({"native_lane": "true"}, "字串 true"),
])
def test_vl_profile_fails_loud_on_non_bool_native_lane(signals, label):
    """`_vl_profile()` 走門面的唯一 exact-bool reader，缺值/型別錯一律 fail-loud。

    lane 有三個消費端（preflight 預算、`RAG` 的 capability probe、verifier 的 lane
    選擇）。之前 `RAG` 對非 `bool` fail-loud、另外兩邊用 truthiness，於是 `"false"`
    在 `RAG` 是錯誤、在別處卻是 native lane —— **跳過 VL capability probe**（契約 §17.4）。

    本模組是 signal 的產生端：自己產出的候選不可能缺這個 key，缺了就是有人繞過
    `Candidate` 的建構路徑。所以這裡**不猜預設值**（舊行為是「缺失當 False」）。
    """
    payload = dict(signals)
    payload["tile_plan"] = {"tiles": [{"est_image_tokens": 10}], "est_tokens": [10]}
    candidate = fc.Candidate(
        index=1, page=7, bbox=(0, 0, 10, 10), page_rect=(0, 0, 100, 100), kind_scores={},
        kind=fe.KIND_TABLE, signals=payload, reasons=[], signature="", native_table=None,
        occurrences=[], asset_xref=None, asset_digest="d",
        figure_id=fe.figure_id_for("doc::0", 7, (0, 0, 10, 10), (0, 0, 100, 100), "d"),
        document_id="doc::0")
    with pytest.raises(fe.FigureExtractionError) as excinfo:
        fc._vl_profile(candidate)
    message = str(excinfo.value)
    assert "native_lane" in message, label
    assert "page=7" in message and candidate.figure_id in message, message


@pytest.mark.smoke
def test_planner_only_emits_exact_bool_native_lane(tmp_path: Path):
    """產生端的不變式：planner 產出的每個候選都帶**精確 bool** 的 `native_lane`。"""
    page = _FakePage(words=_register_rows() + _log_lines())
    plan = _plan([_page_dict(1, TABLE_MD)], _FakeDoc([page]), tmp_path, name="exact.pdf")
    assert plan.candidates, "測試前提：要真的有候選"
    for candidate in plan.candidates:
        value = candidate.signals["native_lane"]
        assert value is True or value is False, (candidate.index, repr(value))
        assert fe.read_native_lane(candidate) is value


@pytest.mark.smoke
@pytest.mark.parametrize("knob,value,expected", [
    ("FIGURE_MAX_CANDIDATES_PER_PAGE", 0, "candidates_per_page:1"),
    ("FIGURE_MAX_CANDIDATES_PER_DOC", 0, "candidates_per_doc"),
    ("FIGURE_MAX_VL_CALLS_PER_DOC", 0, "vl_calls_per_doc"),
    ("FIGURE_MAX_TILES_PER_CANDIDATE", 0, "tiles_per_candidate:1"),
    ("FIGURE_MAX_IMAGE_TOKENS_PER_CALL", 1, "image_tokens_per_call:1"),
    ("FIGURE_MAX_IMAGE_TOKENS_PER_DOC", 0, "image_tokens_per_doc"),
])
def test_every_budget_cap_is_fail_loud(knob, value, expected, tmp_path: Path, monkeypatch):
    """六類上限都要進 `over_budget` 並讓 `check_preflight` raise。"""
    monkeypatch.setattr(config, knob, value)
    page = _FakePage(words=_log_lines(count=5))
    plan = _plan([_page_dict(1, "log")], _FakeDoc([page]), tmp_path)
    assert expected in plan.over_budget, (expected, plan.over_budget, len(plan.candidates))
    with pytest.raises(fe.FigureBudgetError):
        fc.check_preflight(plan)


@pytest.mark.smoke
def test_per_page_cap_lists_every_dropped_candidate(tmp_path: Path, monkeypatch):
    """截斷資訊必須進 `stats`：偵測到幾個、admit 幾個、丟了哪些，逐筆都要看得到。"""
    monkeypatch.setattr(config, "FIGURE_MAX_CANDIDATES_PER_PAGE", 1)
    page = _FakePage(words=_register_rows() + _log_lines())
    plan = _plan([_page_dict(1, "mixed")], _FakeDoc([page]), tmp_path)
    assert plan.preflight["candidates_detected"] == 2
    assert plan.preflight["candidates"] == 1
    dropped = plan.stats["dropped_candidates"]
    assert len(dropped) == 1 and dropped[0]["reason"] == "candidates_per_page"
    assert "page" in dropped[0] and "bbox" in dropped[0] and "kind" in dropped[0]
    assert "candidates_per_page:1" in plan.over_budget


# ============================================================
# smoke：重複影像（審核 BLOCKER 12、workflow §5 evidence ⑤）
# ============================================================
@pytest.mark.smoke
def test_duplicate_figures_keep_own_evidence_and_share_only_vl(tmp_path: Path):
    """同一張表出現在兩頁：**兩個 physical candidate 都保留**，只共享 VL 計算。

    壓成一個 Candidate 的話，另一頁自己的 `pos` 就消失了——T7 無法把那一頁的原
    Markdown 換成 structured 結果，KB 會留下兩份互相競爭的表（workflow §8-4）。
    """
    raw_a = TABLE_MD
    raw_b = "前言\n\n" + TABLE_MD
    boxes_a = ({"class": "table", "bbox": (70, 95, 460, 175), "pos": (0, len(TABLE_MD))},)
    boxes_b = ({"class": "table", "bbox": (70, 95, 460, 175),
                "pos": (len(raw_b) - len(TABLE_MD), len(raw_b))},)
    pages = [_page_dict(1, raw_a, boxes_a), _page_dict(2, raw_b, boxes_b)]
    doc = _FakeDoc([_FakePage(words=_register_rows()), _FakePage(words=_register_rows())])
    plan = _plan(pages, doc, tmp_path)

    assert len(plan.candidates) == 2, [c.page for c in plan.candidates]
    first, second = plan.candidates
    assert first.asset_digest == second.asset_digest, "同內容跨頁必須共用 asset_digest"
    assert first.figure_id != second.figure_id, "figure_id 含頁碼，不同 occurrence 不同 ID"
    # 每個 occurrence 對到**自己那一頁**的原文（不是只比 len(occurrences)==2）
    assert first.native_table["pos"] == (0, len(TABLE_MD))
    assert second.native_table["pos"] == (len(raw_b) - len(TABLE_MD), len(raw_b))
    assert raw_a[slice(*first.native_table["pos"])] == TABLE_MD
    assert raw_b[slice(*second.native_table["pos"])] == TABLE_MD
    assert [(o["page"], o["index"]) for o in first.occurrences] == [(1, 1), (2, 1)]
    assert first.occurrences == second.occurrences
    # 兩筆都是 native lane（有 pos）→ 本來就零 VL，**不得**建立共享關係：
    # 共享的意義是「沿用另一個候選的 VL 結果」，native lane 根本不產生 VL 結果
    # （local review BLOCKER #4）。
    assert first.signals["native_lane"] is True and second.signals["native_lane"] is True
    assert "vl_shared_with" not in second.signals
    assert plan.stats["duplicate_assets_shared"] == []
    assert plan.preflight["vl_calls_max"] == 0


# ============================================================
# smoke：§19.3 duplicate 的共享鍵與「preflight 數字 == 真實呼叫數」
# ============================================================
def _hex_rows_words(top=100.0, pitch=16.0, xs=(72.0, 170.0, 270.0)):
    """table 與 terminal 分數接近的內容 → `KIND_UNKNOWN`（走 dual pass）。"""
    rows = [("0x4000_0100", "RW", "clk"), ("0x4000_0104", "RO", "sts"),
            ("0x4000_0108", "RW", "en")]
    words = []
    for row_index, row in enumerate(rows):
        for column, cell in enumerate(row):
            words.append(_word(xs[column], top + row_index * pitch, cell))
    return words


@pytest.mark.smoke
def test_share_key_declares_planner_requested_kind(tmp_path: Path):
    """共享鍵必須明確帶出 **planner requested kind**（契約 §19.3）。

    verifier 在 dual pass 之後會把 `unknown` 解歧成 table/terminal；cache 若只存
    **解歧後**的 kind，第二筆再拿 `unknown` 去比就永遠命不中——實測兩個 duplicate
    unknown 候選跑出 4 次 VL，preflight 卻宣稱第二筆是 0。
    """
    words = _hex_rows_words()
    doc = _FakeDoc([_FakePage(words=words), _FakePage(words=words)])
    plan = _plan([_page_dict(1, "x" * 40), _page_dict(2, "x" * 40)], doc, tmp_path,
                 name="sharekey.pdf")

    assert len(plan.candidates) == 2, [(c.page, c.kind) for c in plan.candidates]
    assert all(c.kind == fe.KIND_UNKNOWN for c in plan.candidates), (
        "測試前提：這個 fixture 必須落在 table/terminal 難分的 unknown 分支 "
        f"（{[c.kind_scores for c in plan.candidates]}）")
    representative, duplicate = plan.candidates
    for candidate in plan.candidates:
        key = candidate.signals["vl_share_key"]
        assert key["asset_digest"] == candidate.asset_digest
        assert key["requested_kind"] == fe.KIND_UNKNOWN, (
            "共享鍵要存 requested kind，不是 verifier 解歧後的 kind")
    assert representative.signals["vl_share_role"] == "representative"
    assert duplicate.signals["vl_share_role"] == "duplicate"
    assert duplicate.signals["vl_shared_with"] == representative.figure_id
    shared = plan.stats["duplicate_assets_shared"][0]
    assert shared["requested_kind"] == fe.KIND_UNKNOWN
    assert shared["representative"] == representative.figure_id


@pytest.mark.smoke
def test_duplicate_with_representative_at_position_zero_costs_no_vl(tmp_path: Path):
    """代表在**第一個** occurrence（最常見）時，duplicate 的 VL 預算必須是 0。

    舊版把 admitted position 存進 `signals["vl_shared_with"]`，代表在 position 0 時
    值是 `0`；`_vl_profile()` 用 truthiness 讀它 → 讀成「沒有共享」→ duplicate 被算成
    要跑滿 VL（實測 `vl_shared_with=0 → max=3`、`=1 → max=0`）。方向雖是高估，
    但那正是 §17.4 `"false"` 被當 truthy 的同一類坑。
    """
    words = _hex_rows_words()
    doc = _FakeDoc([_FakePage(words=words), _FakePage(words=words)])
    plan = _plan([_page_dict(1, "x" * 40), _page_dict(2, "x" * 40)], doc, tmp_path,
                 name="repzero.pdf")

    assert len(plan.candidates) == 2, [(c.page, c.kind) for c in plan.candidates]
    representative, duplicate = plan.candidates
    assert representative.signals["vl_share_role"] == "representative"
    assert plan.stats["duplicate_assets_shared"][0]["representative"] == \
        representative.figure_id, "測試前提：代表必須是第一個 occurrence（position 0）"
    assert "vl_shared_with" not in representative.signals

    shared = duplicate.signals["vl_shared_with"]
    assert shared == representative.figure_id
    assert isinstance(shared, str) and shared.startswith("fig_"), (
        f"共享關係要存 figure_id，不得存 admitted position（收到 {shared!r}）")

    assert fc._vl_profile(duplicate)["max"] == 0
    assert fc._vl_profile(duplicate)["min"] == 0
    representative_cost = fc._vl_profile(representative)
    assert representative_cost["max"] > 0, "測試前提：代表本身真的要跑 VL"
    assert plan.preflight["vl_calls_max"] == representative_cost["max"]
    assert plan.preflight["vl_calls_min"] == representative_cost["min"]


@pytest.mark.smoke
@pytest.mark.parametrize("shared", [0, "", False, "fig_0123456789abcdef"])
def test_vl_shared_with_is_read_by_presence_not_truthiness(shared):
    """`vl_shared_with` 的判定是 `is not None`，**不是** truthiness。

    `0` / `""` / `False` 都是「有共享關係」的合法表示（舊版的 position 0 就是 `0`），
    只有「不存在」才代表沒有共享。混用 truthiness 會讓預算宣稱與實際行為悄悄拆開。
    """
    candidate = fc.Candidate(
        index=2, page=2, bbox=(0, 0, 10, 10), page_rect=(0, 0, 100, 100), kind_scores={},
        kind=fe.KIND_UNKNOWN,
        signals={"native_lane": False, "anchored": True, "vl_shared_with": shared,
                 "tile_plan": {"tiles": [{"est_image_tokens": 10}] * 3,
                               "est_tokens": [10] * 3}},
        reasons=[], signature="", native_table=None, occurrences=[], asset_xref=None,
        asset_digest="d",
        figure_id=fe.figure_id_for("doc::0", 2, (0, 0, 10, 10), (0, 0, 100, 100), "d"),
        document_id="doc::0")
    profile = fc._vl_profile(candidate)
    assert (profile["min"], profile["max"]) == (0, 0), (shared, profile)
    assert (profile["tokens_min"], profile["tokens_max"]) == (0, 0)


def _vl_stub_script():
    table = json.dumps({
        "columns": [{"label": "Address"}, {"label": "Mode"}, {"label": "Desc"}],
        "rows": [{"cells": [{"text": text, "state": "observed"} for text in row]}
                 for row in (("0x4000_0100", "RW", "clk"), ("0x4000_0104", "RO", "sts"),
                             ("0x4000_0108", "RW", "en"))],
        "footnotes": []})
    terminal = json.dumps({"lines": [
        {"text": "0x4000_0100 RW clk", "uncertain_spans": []},
        {"text": "0x4000_0104 RO sts", "uncertain_spans": []},
        {"text": "0x4000_0108 RW en", "uncertain_spans": []}]})
    return {"figure_table": table, "figure_terminal": terminal}


def _ruled_only_drawings():
    """只有框線、**框內沒有任何字** → VL lane 且 `anchored=False`。"""
    lines = [(65, 90 + i * 30, 430, 91 + i * 30) for i in range(6)]
    lines += [(65, 90, 66, 240), (429, 90, 430, 240), (200, 90, 201, 240),
              (300, 90, 301, 240)]
    return lines


def _install_vl_stub(monkeypatch, figure_verify, *, calls, script=None, flaky=False):
    """把 VL / probe 換成 stub 並記錄每次呼叫的 schema 名稱。

    `flaky=True` 時每一次抽取的**第一個 attempt** 回不合法 JSON，逼出
    `attempts = 1 + retries` 的重試路徑。
    """
    payload = script or _vl_stub_script()
    seen: dict[str, int] = {}

    def _vision(**kwargs):
        name = kwargs["response_format"]["json_schema"]["name"]
        calls.append(name)
        index = seen.get(name, 0)
        seen[name] = index + 1
        text = payload[name]
        if flaky and index % 2 == 0:
            text = "NOT JSON AT ALL"
        return types.SimpleNamespace(text=text, finish_reason="stop", truncated=False,
                                     usage={}, raw={})

    monkeypatch.setattr(figure_verify.llama_client, "vision_json_completion", _vision,
                        raising=False)
    monkeypatch.setattr(
        figure_verify.llama_client, "get_props",
        lambda *a, **k: {"model_path": "/models/vl.gguf", "model_alias": "vl",
                         "chat_template": "supports json_schema", "n_ctx": 8192},
        raising=False)
    monkeypatch.setattr(
        figure_verify, "ensure_capability",
        lambda **k: figure_verify.ProbeResult(True, "fp", {"stub": True}, [], "stub"))


def _stub_variants(candidate):
    """T3 產出的**合法**單張 Variant（契約 §21.3）。

    `digest` 是真的 `sha256(png)`、§6.3 的欄位一個不缺，而且當場過一次共享 validator：
    fixture 只要比 producer 寬鬆，producer 漂移就永遠不會在測試裡現形——這正是這條
    接縫連續四輪被打回的成因。
    """
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 40
    variant = fc.Variant(figure_id=candidate.figure_id, variant_id="crop@200dpi",
                         png=png, width=200, height=120,
                         bbox=candidate.bbox, tile_index=0, tile_total=1, overlap_px=0,
                         est_image_tokens=40, digest=hashlib.sha256(png).hexdigest())
    fe.validate_variant(variant, where=f"測試 stub render figure={candidate.figure_id}")
    return [variant]


def _stub_render(_doc, candidate):
    return _stub_variants(candidate)


_LANE_FIXTURES = {
    # (words, drawings) → 預期的 (kind, anchored)
    "known_anchored": (fe.KIND_TERMINAL, True),
    "known_unanchored": (fe.KIND_TABLE, False),
    "unknown": (fe.KIND_UNKNOWN, True),
}


def _lane_page(label):
    if label == "known_anchored":
        return _FakePage(words=_log_lines(count=6))
    if label == "known_unanchored":
        return _FakePage(drawings=_ruled_only_drawings())
    return _FakePage(words=_hex_rows_words())


@pytest.mark.smoke
@pytest.mark.parametrize("retries", [0, 1])
@pytest.mark.parametrize("label", sorted(_LANE_FIXTURES))
def test_preflight_budget_brackets_real_vl_call_count(label, retries, tmp_path: Path,
                                                      monkeypatch):
    """★ 跨模組：**真 verifier** 的實際呼叫數必須落在 planner 宣稱的 `[min, max]` 內。

    契約 §20 的凍結狀態表（`T` = tiles、`R` = `FIGURE_EXTRACT_RETRIES`）：

    | lane / 情境 | min | max |
    |---|---|---|
    | VL, `KIND_UNKNOWN` | `2T` | `2T` |
    | VL, kind 已定, 有 anchor | `T` | `2T(1+R)` |
    | VL, kind 已定, 無 anchor | `2T` | `2T(1+R)` |

    只驗 planner 自己的公式是不夠的——上一輪就是這樣漏掉「第二次取樣**也會重試**」。
    """
    figure_verify = pytest.importorskip("figure_verify", reason="T4 尚未交付")
    monkeypatch.setattr(config, "FIGURE_EXTRACT_RETRIES", retries)
    expected_kind, expected_anchored = _LANE_FIXTURES[label]

    plan = _plan([_page_dict(1, "x" * 40)], _FakeDoc([_lane_page(label)]), tmp_path,
                 name=f"lane-{label}-{retries}.pdf")
    assert plan.candidates, (
        f"測試前提：{label} 必須產生候選 {plan.stats['deferred_to_legacy_lane']}")
    candidate = plan.candidates[0]
    assert candidate.kind == expected_kind, (label, candidate.kind, candidate.kind_scores)
    assert candidate.signals["anchored"] is expected_anchored, label
    assert candidate.signals["native_lane"] is False, "測試前提：必須走 VL lane"

    tiles = len(candidate.signals["tile_plan"]["tiles"])
    profile = fc._vl_profile(candidate)
    if expected_kind == fe.KIND_UNKNOWN:
        assert (profile["min"], profile["max"]) == (2 * tiles, 2 * tiles)
    else:
        assert profile["min"] == (tiles if expected_anchored else 2 * tiles)
        assert profile["max"] == 2 * tiles * (1 + retries)
    assert plan.preflight["vl_calls_min"] == profile["min"]
    assert plan.preflight["vl_calls_max"] == profile["max"]

    calls: list[str] = []
    _install_vl_stub(monkeypatch, figure_verify, calls=calls)
    figure_verify.extract_document_figures(
        plan, pdf_doc=None, page_evidence=plan.page_evidence,
        vl_base_url="http://127.0.0.1:8083", vl_model="vl", render_variants=_stub_render)

    assert profile["min"] <= len(calls) <= profile["max"], (
        f"{label} R={retries}: 實際 {len(calls)} 次不在 preflight 宣稱的 "
        f"[{profile['min']}, {profile['max']}] 內 {calls}")
    if expected_kind == fe.KIND_UNKNOWN:
        assert len(calls) == profile["max"] == 2 * tiles, (
            f"dual pass 恰好 table/terminal 各一次，不重試也不取第二樣本 {calls}")


@pytest.mark.smoke
def test_retrying_second_sample_stays_inside_preflight_max(tmp_path: Path, monkeypatch):
    """第二次取樣**也會重試** —— `max` 必須是 `2T(1+R)`，不是 `T(1+R)+T`。

    舊公式在 `R=1` 時說 3、實際會跑 4 次：預算閘會在**已經打完 3 次 VL 之後**才於
    runtime 中止，而不是在任何呼叫之前拒絕（違反 workflow §4 Step 2）。
    """
    figure_verify = pytest.importorskip("figure_verify", reason="T4 尚未交付")
    monkeypatch.setattr(config, "FIGURE_EXTRACT_RETRIES", 1)

    plan = _plan([_page_dict(1, "x" * 40)],
                 _FakeDoc([_FakePage(words=_log_lines(count=6))]), tmp_path,
                 name="retrying.pdf")
    candidate = plan.candidates[0]
    tiles = len(candidate.signals["tile_plan"]["tiles"])
    profile = fc._vl_profile(candidate)
    assert profile["max"] == 2 * tiles * 2, profile
    assert profile["max"] > tiles * (1 + 1) + tiles, (
        "舊公式 T(1+R)+T 會低估——這條就是釘住這件事")

    calls: list[str] = []
    _install_vl_stub(monkeypatch, figure_verify, calls=calls, flaky=True)
    figure_verify.extract_document_figures(
        plan, pdf_doc=None, page_evidence=plan.page_evidence,
        vl_base_url="http://127.0.0.1:8083", vl_model="vl", render_variants=_stub_render)

    assert len(calls) == 4 * tiles, (
        f"每次抽取的第一個 attempt 失敗 → 抽取與第二次取樣各跑 2 次，實際 {calls}")
    assert len(calls) <= profile["max"]
    assert len(calls) > tiles * (1 + 1) + tiles, "確認舊公式真的會被這條打到"


@pytest.mark.smoke
def test_duplicate_unknown_preflight_matches_real_vl_call_count(tmp_path: Path, monkeypatch):
    """★ 同時核對 **preflight 數字**與**真實 VL 呼叫數**（契約 §19.3）。

    上一輪之所以沒抓到，就是因為只驗了其中一邊：planner 宣稱 duplicate 是 0 次，
    verifier 卻重跑了一整輪 dual pass（實測 4 次）。這條把兩邊綁在同一個斷言上——
    「一頁」與「兩頁（內容完全相同）」的真實呼叫數必須一模一樣，而且 preflight
    也必須這麼說。
    """
    figure_verify = pytest.importorskip("figure_verify", reason="T4 尚未交付")
    script = _vl_stub_script()
    calls: list[str] = []

    def _vision(**kwargs):
        name = kwargs["response_format"]["json_schema"]["name"]
        calls.append(name)
        return types.SimpleNamespace(text=script[name], finish_reason="stop",
                                     truncated=False, usage={}, raw={})

    monkeypatch.setattr(figure_verify.llama_client, "vision_json_completion", _vision,
                        raising=False)
    monkeypatch.setattr(
        figure_verify.llama_client, "get_props",
        lambda *a, **k: {"model_path": "/models/vl.gguf", "model_alias": "vl",
                         "chat_template": "supports json_schema", "n_ctx": 8192},
        raising=False)
    monkeypatch.setattr(
        figure_verify, "ensure_capability",
        lambda **k: figure_verify.ProbeResult(True, "fp", {"stub": True}, [], "stub"))

    def _render(_doc, candidate):
        return _stub_variants(candidate)

    def _run(n_pages: int):
        calls.clear()
        words = _hex_rows_words()
        doc = _FakeDoc([_FakePage(words=words) for _ in range(n_pages)])
        plan = _plan([_page_dict(i + 1, "x" * 40) for i in range(n_pages)], doc, tmp_path,
                     name=f"dupcalls{n_pages}.pdf")
        assert len(plan.candidates) == n_pages
        assert all(c.kind == fe.KIND_UNKNOWN for c in plan.candidates)
        figure_verify.extract_document_figures(
            plan, pdf_doc=None, page_evidence=plan.page_evidence,
            vl_base_url="http://127.0.0.1:8083", vl_model="vl", render_variants=_render)
        return plan, list(calls)

    single_plan, single_calls = _run(1)
    double_plan, double_calls = _run(2)

    assert single_calls == ["figure_table", "figure_terminal"], single_calls
    assert double_calls == single_calls, (
        f"duplicate 不得增加任何 VL 呼叫：一頁 {len(single_calls)} 次、"
        f"兩頁 {len(double_calls)} 次 {double_calls}")
    assert double_plan.preflight["vl_calls_min"] == single_plan.preflight["vl_calls_min"], (
        "preflight 也必須說 duplicate 是 0 次")
    assert len(double_calls) >= double_plan.preflight["vl_calls_min"]
    assert len(double_calls) <= double_plan.preflight["vl_calls_max"], (
        f"真實呼叫數 {len(double_calls)} 超出 preflight 宣稱的上限 "
        f"{double_plan.preflight['vl_calls_max']}")


@pytest.mark.smoke
def test_duplicate_detection_shares_vl_but_counts_it_once(tmp_path: Path):
    """共享 asset 的第二筆候選不再貢獻 VL 成本（省的是**計算**，不是 evidence）。"""
    words = _log_lines(count=6)
    pages = [_page_dict(1, "log"), _page_dict(2, "log")]
    doc = _FakeDoc([_FakePage(words=words), _FakePage(words=words)])
    plan = _plan(pages, doc, tmp_path)
    assert len(plan.candidates) == 2
    first, second = plan.candidates
    assert first.asset_digest == second.asset_digest
    assert first.signals["native_lane"] is False, "共享只發生在 VL lane"
    assert first.kind == second.kind, "只有同 kind 才可共享 VL 結果"
    assert second.signals["vl_shared_with"] == first.figure_id
    single = fc._vl_profile(first)
    assert plan.preflight["vl_calls_max"] == single["max"]
    assert plan.preflight["vl_calls_max"] > 0
    assert plan.stats["duplicate_assets_shared"][0]["pages"] == [1, 2]
    # `requested_kind` 是 **planner 的** kind（可能是 unknown），不是 verifier 解歧後的
    assert plan.stats["duplicate_assets_shared"][0]["requested_kind"] == first.kind
    assert first.signals["vl_share_key"] == {"asset_digest": first.asset_digest,
                                             "requested_kind": first.kind}


# ============================================================
# smoke：local review 的十二條 BLOCKER
# ============================================================
@pytest.mark.smoke
@pytest.mark.parametrize("broken", ["words", "drawings", "annots", "widgets"])
def test_incomplete_overlay_channel_forces_page_crop(broken, tmp_path: Path):
    """overlay 相關 channel 只要有一個取不到，就**不得**宣稱純 raster。

    words / drawings / annots / widgets 任一失敗時，我們並不知道那張圖上有沒有看得見的
    覆蓋物；送 `extract_image()` 的原始 binary 等於讓模型看到的跟頁面上不一樣
    （local review BLOCKER #1）。
    """
    raster = {"bbox": (100, 100, 400, 325), "xref": 7, "width": 120, "height": 90,
              "digest": b"\x03" * 16, "has-mask": False,
              "transform": (300.0, 0.0, 0.0, 225.0, 100.0, 100.0)}
    raw = "|A|B|\n|---|---|\n|1|2|\n"
    boxes = ({"class": "table", "bbox": (100, 100, 400, 325), "pos": (0, len(raw))},)
    page = _FakePage(images=[raster], fail=(broken,))
    doc = _FakeDoc([page], images={7: {"image": b"PNGDATA", "width": 120, "height": 90,
                                       "ext": "png", "smask": 0}})
    plan = _plan([_page_dict(1, raw, boxes)], doc, tmp_path, name=f"ov-{broken}.pdf")

    assert plan.candidates, f"測試前提：{broken} 壞掉時仍要產生候選才驗得到 purity 分支"
    candidate = plan.candidates[0]
    purity = candidate.signals["raster_purity"]
    assert candidate.asset_xref is None, purity
    assert purity["pure"] is False
    assert purity["reason"] == "overlay_channels_incomplete", purity
    assert broken in purity["missing_channels"], purity


@pytest.mark.smoke
def test_broken_annotation_rect_does_not_raise_and_blocks_pure_raster(tmp_path: Path):
    """單一 annotation 的 `rect` getter 丟例外：harvest 不得 raise，但要標成不完整。"""
    raster = {"bbox": (100, 100, 400, 325), "xref": 7, "width": 120, "height": 90,
              "digest": b"\x03" * 16, "has-mask": False,
              "transform": (300.0, 0.0, 0.0, 225.0, 100.0, 100.0)}
    raw = "|A|B|\n|---|---|\n|1|2|\n"
    boxes = ({"class": "table", "bbox": (100, 100, 400, 325), "pos": (0, len(raw))},)
    page = _FakePage(images=[raster], annots=[_BROKEN])
    doc = _FakeDoc([page], images={7: {"image": b"PNGDATA", "width": 120, "height": 90,
                                       "ext": "png", "smask": 0}})
    plan = _plan([_page_dict(1, raw, boxes)], doc, tmp_path, name="brokenannot.pdf")

    assert "annots:partial" in plan.page_evidence[1].unavailable
    assert plan.candidates
    assert plan.candidates[0].asset_xref is None
    assert plan.candidates[0].signals["raster_purity"]["reason"] == "overlay_channels_incomplete"


@pytest.mark.smoke
def test_tile_bboxes_cover_the_whole_candidate_without_holes(tmp_path: Path, monkeypatch):
    """所有 tile 的 bbox 聯集必須**完整覆蓋**候選，中間不得有洞。

    舊版 core 只取首末 band 的 y 範圍：第一帶之前、末帶之後、以及兩群 band 之間的
    大片空白會完全沒有進到任何 tile —— 內容靜默消失（local review BLOCKER #2）。
    """
    monkeypatch.setattr(config, "FIGURE_MAX_IMAGE_TOKENS_PER_CALL", 300)
    # 框線比文字**上下都多出一截**：候選 bbox 因此比 band 範圍大，首帶之前與末帶之後
    # 各有一段沒有字的區域。舊寫法的 core 只到首末 band，那兩段會掉出所有 tile。
    words = []
    for row in range(12):
        for column, text in enumerate((f"CTRL{row}", f"0x4000_{row:04d}", "[7:4]", "RW")):
            words.append(_word(72.0 + column * 95, 100.0 + row * 18, text))
    lines = [(65, 90, 430, 91), (65, 340, 430, 341)]
    lines += [(65, 90 + i * 25, 430, 91 + i * 25) for i in range(1, 10)]
    lines += [(65, 90, 66, 340), (429, 90, 430, 340), (200, 90, 201, 340)]
    page = _FakePage(words=words, drawings=lines)
    plan = _plan([_page_dict(1, "x" * 40)], _FakeDoc([page]), tmp_path)
    assert plan.candidates, "測試前提：ruled grid + words 要融合成一個候選"
    candidate = plan.candidates[0]
    bands = candidate.signals["bands"]
    assert bands[0]["y0"] > candidate.bbox[1], "測試前提：首帶之前要有沒有字的區域"
    assert bands[-1]["y1"] < candidate.bbox[3], "測試前提：末帶之後要有沒有字的區域"
    tiles = candidate.signals["tile_plan"]["tiles"]
    assert len(tiles) > 1, "測試前提：這個候選要真的被切成多張"

    cores = [t["core_bbox"] for t in tiles]
    assert cores[0][1] == pytest.approx(candidate.bbox[1]), "第一塊 core 的上緣＝候選頂端"
    assert cores[-1][3] == pytest.approx(candidate.bbox[3]), "最後一塊 core 的下緣＝候選底端"
    for previous, current in zip(cores, cores[1:]):
        assert previous[3] == pytest.approx(current[1]), "core 必須完整分割，不得有洞"

    spans = sorted((t["bbox"][1], t["bbox"][3]) for t in tiles)
    merged: list[list[float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    assert len(merged) == 1, f"tile bbox 聯集有洞：{merged}"
    assert merged[0][0] == pytest.approx(candidate.bbox[1])
    assert merged[0][1] == pytest.approx(candidate.bbox[3])


@pytest.mark.smoke
@pytest.mark.parametrize("label,mutate", [
    ("欄位位置不同", lambda w: [(x + (30.0 if i % 4 == 1 else 0.0), y, t)
                                for i, (x, y, t) in enumerate(w)]),
    ("字距不同", lambda w: [(x, y, t + " ") for x, y, t in w]),
])
def test_same_outer_bbox_different_inner_content_gets_different_digest(
        label, mutate, tmp_path: Path):
    """外框相同、內部字位置/內容不同 → `asset_digest` 必須不同。

    簽章只序列化 band 左上角 + 整行文字的話，兩張外框一樣的向量表會碰撞，於是被誤
    去重、把 A 圖的 VL payload 套到 B 圖，甚至誤沿用 A 圖的 human verification
    （local review BLOCKER #3）。
    """
    base = [(w[0], w[1], w[4]) for w in _register_rows()]
    other = mutate(base)
    to_words = [ [_word(x, y, t) for x, y, t in rows] for rows in (base, other) ]
    plans = []
    for index, words in enumerate(to_words):
        plans.append(_plan([_page_dict(1, "x" * 40)], _FakeDoc([_FakePage(words=words)]),
                           tmp_path, name=f"sig-{label}-{index}.pdf"))
    assert all(p.candidates for p in plans), "測試前提：兩份都要有候選"
    a, b = plans[0].candidates[0], plans[1].candidates[0]
    assert a.asset_digest != b.asset_digest, (label, a.signature, b.signature)


@pytest.mark.smoke
def test_drawing_geometry_changes_the_digest(tmp_path: Path):
    """框線不同（有格線 vs 無格線）也要換 digest。"""
    words = _register_rows()
    lines = [(70, 95, 460, 96), (70, 130, 460, 131), (70, 165, 460, 166),
             (70, 95, 71, 165), (459, 95, 460, 165)]
    plain = _plan([_page_dict(1, "x" * 40)], _FakeDoc([_FakePage(words=words)]),
                  tmp_path, name="nolines.pdf")
    ruled = _plan([_page_dict(1, "x" * 40)],
                  _FakeDoc([_FakePage(words=words, drawings=lines)]),
                  tmp_path, name="ruled.pdf")
    assert plain.candidates and ruled.candidates
    assert plain.candidates[0].asset_digest != ruled.candidates[0].asset_digest


@pytest.mark.smoke
def test_native_lane_candidate_never_shares_vl_with_another_candidate(tmp_path: Path):
    """native lane 的候選既不共享、也不當共享的代表。

    第一頁 pos-backed（native lane）、第二頁只有 words（VL lane），內容相同：第二頁
    若被標成共享第一頁，preflight 會算成零次，但第一頁根本不會產生任何 VL 結果可沿用
    （local review BLOCKER #4）。
    """
    words = _log_lines(count=5)
    probe = _plan([_page_dict(1, "log")], _FakeDoc([_FakePage(words=words)]), tmp_path,
                  name="crosslaneprobe.pdf")
    bbox = probe.candidates[0].bbox
    raw = "```\n$ dmesg | tail\n```\n"
    pages = [_page_dict(1, raw, ({"class": "text", "bbox": bbox, "pos": (0, len(raw))},)),
             _page_dict(2, raw, ())]
    doc = _FakeDoc([_FakePage(words=words), _FakePage(words=words)])
    plan = _plan(pages, doc, tmp_path, name="crosslane.pdf")

    assert len(plan.candidates) == 2, [(c.page, c.kind) for c in plan.candidates]
    native, visual = plan.candidates
    assert native.signals["native_lane"] is True
    assert visual.signals["native_lane"] is False
    assert "vl_shared_with" not in visual.signals, "VL lane 不得共享 native lane 的結果"
    assert "vl_shared_with" not in native.signals
    assert plan.preflight["vl_calls_min"] > 0, "第二頁真的需要 VL，預算不得算成 0"
    assert plan.preflight["vl_calls_max"] > 0


@pytest.mark.smoke
def test_cross_lane_duplicate_is_blocked_by_zero_vl_budget(tmp_path: Path, monkeypatch):
    """接續上一條：VL 上限 0 時 preflight 必須擋下來（而不是因為誤判共享而放行）。"""
    monkeypatch.setattr(config, "FIGURE_MAX_VL_CALLS_PER_DOC", 0)
    words = _log_lines(count=5)
    probe = _plan([_page_dict(1, "log")], _FakeDoc([_FakePage(words=words)]), tmp_path,
                  name="clbprobe.pdf")
    raw = "```\n$ dmesg | tail\n```\n"
    pages = [_page_dict(1, raw, ({"class": "text", "bbox": probe.candidates[0].bbox,
                                  "pos": (0, len(raw))},)),
             _page_dict(2, raw, ())]
    doc = _FakeDoc([_FakePage(words=words), _FakePage(words=words)])
    plan = _plan(pages, doc, tmp_path, name="clb.pdf")
    assert "vl_calls_per_doc" in plan.over_budget
    with pytest.raises(fe.FigureBudgetError):
        fc.check_preflight(plan)


@pytest.mark.smoke
def test_multiple_table_pos_in_one_component_disqualifies_the_group(tmp_path: Path):
    """同一群裡有多個不同的 table `pos` → 整組失格，兩段原 Markdown 都完整保留。

    只留第一個 pos 的話，第二段會留在 KB、structured 結果又照常入庫 → 兩份互相競爭的
    表（workflow §8-4 / local review BLOCKER #6）。
    """
    raw = TABLE_MD + TABLE_MD
    boxes = ({"class": "table", "bbox": (70, 95, 460, 175), "pos": (0, len(TABLE_MD))},
             {"class": "table", "bbox": (70, 96, 460, 174),
              "pos": (len(TABLE_MD), len(raw))})
    plan = _plan([_page_dict(1, raw, boxes)],
                 _FakeDoc([_FakePage(words=_register_rows())]), tmp_path, name="ambpos.pdf")
    assert plan.candidates == [], [(c.kind, c.native_table) for c in plan.candidates]
    reasons = [e["reason"] for e in plan.stats["deferred_to_legacy_lane"]]
    assert "ambiguous_table_pos" in reasons, reasons


@pytest.mark.smoke
@pytest.mark.parametrize("cells,label", [
    ([[None, None, None, None]] * 3, "全部 None cell"),
    ([[(70, 95, 180, 115)], [(70, 115, 180, 135), (180, 115, 300, 135)]], "ragged matrix"),
])
def test_table_without_real_cell_geometry_is_not_native(cells, label, tmp_path: Path):
    """cell bbox 拿不出來的 table 不得被當成有效 `native_table`（契約 §12.4）。"""
    table = _FakeTable((70, 95, 460, 175), cells,
                       header_names=["Name", "Address", "Bits", "Access"])
    page = _FakePage(words=_register_rows(), tables={"lines": [table]})
    plan = _plan([_page_dict(1, "x" * 40)], _FakeDoc([page]), tmp_path,
                 name=f"badgeom-{label}.pdf")
    entries = plan.page_evidence[1].tables["lines"]
    assert entries and entries[0]["degenerate"] is True, entries
    assert "cells:incomplete_geometry" in entries[0]["errors"], entries[0]["errors"]
    for candidate in plan.candidates:
        assert candidate.native_table is None, label
        assert candidate.signals["native_lane"] is False, label


@pytest.mark.smoke
@pytest.mark.parametrize("meta", [
    {"page_number": True}, {"page_number": 1.9}, {"page_number": "1"},
    {"page": "0"}, {"page": 0.0}, {"page": True}, {},
])
def test_non_integer_page_metadata_abstains(meta, tmp_path: Path):
    """`True` / `1.9` / `"1"` 都**不是**第 1 頁。

    `int(1.9)` 會做出一個看起來合法、實際錯位的頁碼，而頁碼決定 evidence 配到哪一頁的
    像素與 `figure_id`（local review BLOCKER #8）。
    """
    assert fc._page_number(meta) == fc.INVALID_PAGE
    page = _FakePage(words=_register_rows())
    plan = _plan([{"metadata": meta, "text": TABLE_MD, "page_boxes": []}],
                 _FakeDoc([page]), tmp_path, name="badpage.pdf")
    assert plan.candidates == []
    assert plan.stats["page_number_mismatch"], plan.stats
    # 沒有 physical index 時也不得用 `pdf_doc[-1]` 安靜地拿到最後一頁
    evidence = fc.harvest_page_evidence({"metadata": meta, "text": TABLE_MD},
                                        pdf_doc=_FakeDoc([_FakePage(), _FakePage()]))
    assert "page_object:invalid_page_number" in evidence.unavailable


@pytest.mark.smoke
@pytest.mark.parametrize("target", ["_region_sources", "_plan_tiles"])
def test_unexpected_planning_error_disables_the_whole_structured_lane(
        target, tmp_path: Path, monkeypatch):
    """偵測 / 建構的**非預期**例外 → 零候選 + `planning_error` + 下一個 gate fail-loud。

    只記進 stats 而照樣交付其餘候選的話，structured table 會悄悄消失、PDF 卻繼續
    部分寫入（local review BLOCKER #9）。
    """
    def _boom(*_args, **_kwargs):
        raise RuntimeError("detector exploded (stub)")

    monkeypatch.setattr(fc, target, _boom)
    plan = _plan([_page_dict(1, TABLE_MD)],
                 _FakeDoc([_FakePage(words=_register_rows())]), tmp_path,
                 name=f"boom-{target}.pdf")
    assert plan.candidates == []
    assert plan.over_budget == ["planning_error"], plan.over_budget
    assert plan.stats["candidate_errors"], plan.stats
    assert plan.preflight["vl_calls_max"] == 0
    with pytest.raises(fe.FigureBudgetError, match="planning_error"):
        fc.check_preflight(plan)


@pytest.mark.smoke
def test_pathological_region_count_is_capped_before_quadratic_fusion(tmp_path: Path):
    """fusion 是 O(n²)：raw region 數必須在 clustering **之前**就被擋下。

    釘版 pymupdf4llm 已知會把微小圓點切成大量元素（workflow §4 Step 2）；病態 PDF
    要在 fail-loud 報告產出前先把時間/記憶體吃光（local review BLOCKER #10）。
    """
    words = []
    for block in range(fc.MAX_PROMOTING_REGIONS_PER_PAGE + 30):
        base = 20.0 + block * 80.0
        for row in range(3):
            for column, text in enumerate(("CTRL", "0x40", "[7:4]")):
                words.append(_word(72.0 + column * 60, base + row * 10, text))
    page = _FakePage(rect=(0, 0, 612, 20000), words=words)
    plan = _plan([_page_dict(1, "many")], _FakeDoc([page]), tmp_path, name="many.pdf")

    overflow = plan.stats["raw_region_overflow"]
    assert overflow and overflow[0]["page"] == 1
    assert overflow[0]["promoting"] > fc.MAX_PROMOTING_REGIONS_PER_PAGE
    assert plan.candidates == []
    assert "raw_regions_per_page:1" in plan.over_budget
    assert all(e["reason"] == "raw_regions_per_page"
               for e in plan.stats["deferred_to_legacy_lane"])
    with pytest.raises(fe.FigureBudgetError, match="raw_regions_per_page"):
        fc.check_preflight(plan)


# ============================================================
# 非 smoke（仍然零外部相依）
# ============================================================
def test_signature_is_page_independent_and_content_sensitive(tmp_path: Path):
    """`asset_digest` 的簽章不含頁碼與絕對座標，但**含**原文內容 digest。

    含 rotation / 缺內容的簽章會讓「同內容跨頁」去重失效，或讓兩張只是尺寸與 channel
    相同的表被錯誤合併（審核 BLOCKER 11）。
    """
    same = _log_lines(count=6)
    plan = _plan([_page_dict(1, "log"), _page_dict(2, "log")],
                 _FakeDoc([_FakePage(words=same), _FakePage(words=same)]), tmp_path)
    assert plan.candidates[0].signature == plan.candidates[1].signature
    assert '"page"' not in plan.candidates[0].signature, "簽章不得含頁碼"
    assert "pos_text" in plan.candidates[0].signature

    other = _log_lines(count=6)
    other[-1] = _word(72.0, other[-1][1], "DIFFERENT", size=9.0, char_w=5.4)
    plan2 = _plan([_page_dict(1, "log"), _page_dict(2, "log")],
                  _FakeDoc([_FakePage(words=same), _FakePage(words=other)]), tmp_path,
                  name="other.pdf")
    assert plan2.candidates[0].asset_digest != plan2.candidates[1].asset_digest


def test_fusion_is_order_independent_and_does_not_bridge_two_tables(tmp_path: Path):
    """fusion = 原始 region 的 pairwise IoU connected components。

    重算群 bbox 再迭代會依輸入順序改變結果，也可能把相鄰兩張表橋接成一個候選。
    """
    top = _register_rows(top=100.0)
    bottom = _register_rows(top=400.0)
    page = _FakePage(words=top + bottom)
    plan = _plan([_page_dict(1, "two tables")], _FakeDoc([page]), tmp_path)
    assert len(plan.candidates) == 2, [c.bbox for c in plan.candidates]
    assert plan.candidates[0].bbox[3] < plan.candidates[1].bbox[1], "兩張表不得被橋接"

    shuffled = _FakePage(words=list(reversed(top + bottom)))
    plan2 = _plan([_page_dict(1, "two tables")], _FakeDoc([shuffled]), tmp_path,
                  name="shuffled.pdf")
    assert [c.bbox for c in plan2.candidates] == [c.bbox for c in plan.candidates]


def test_components_are_stable_under_input_permutation():
    """`_components` 對輸入順序不敏感（chain 重疊也要落在同一個 component）。"""
    regions = [
        {"bbox": (0, 0, 100, 100)}, {"bbox": (20, 20, 120, 120)},
        {"bbox": (40, 40, 140, 140)}, {"bbox": (500, 500, 600, 600)},
    ]
    base = fc._components(regions, 0.3)
    assert sorted(len(c) for c in base) == [1, 3]
    for rotation in range(len(regions)):
        rotated = regions[rotation:] + regions[:rotation]
        sizes = sorted(len(c) for c in fc._components(rotated, 0.3))
        assert sizes == [1, 3]


def test_evidence_and_stats_are_json_serializable(tmp_path: Path):
    """manifest 要寫得出去：digest 轉 hex、Rect 轉數字、沒有 dataclass 混進 stats。"""
    page = _FakePage(
        words=_register_rows(),
        images=[{"bbox": (72, 300, 400, 500), "xref": 9, "width": 80, "height": 60,
                 "digest": b"\xab" * 16, "has-mask": False,
                 "transform": (328.0, 0.0, 0.0, 200.0, 72.0, 300.0)}],
        drawings=[(72, 95, 460, 96)])
    plan = _plan([_page_dict(1, TABLE_MD)], _FakeDoc([page]), tmp_path)
    json.dumps(plan.stats, ensure_ascii=False)
    json.dumps(plan.preflight, ensure_ascii=False)
    evidence = plan.page_evidence[1]
    json.dumps({
        "page_boxes": evidence.page_boxes, "image_info": evidence.image_info,
        "tables": evidence.tables, "drawing_clusters": evidence.drawing_clusters,
        "page_rect": evidence.page_rect, "unavailable": evidence.unavailable,
        "fallback": evidence.fallback, "overlays": evidence.overlays,
        "words": [list(w) for w in evidence.words],
    }, ensure_ascii=False)
    assert evidence.image_info[0]["digest_hex"] == "ab" * 16
    assert "digest" not in evidence.image_info[0]
    for candidate in plan.candidates:
        json.dumps(candidate.signals, ensure_ascii=False)


def test_upstream_page_dicts_are_not_mutated(tmp_path: Path):
    """`RAG.extract_pdf_document` 之後還要用同一批 `pages`：不得就地加鍵污染它。"""
    pages = [_page_dict(1, TABLE_MD, ({"class": "table", "bbox": (70, 95, 460, 175),
                                       "pos": (0, len(TABLE_MD))},))]
    snapshot = copy.deepcopy(pages)
    _plan(pages, _FakeDoc([_FakePage(words=_register_rows())]), tmp_path)
    assert pages == snapshot


def test_self_opened_document_is_closed_and_caller_document_is_not(tmp_path: Path, monkeypatch):
    """自己開的檔一定關掉一次；呼叫端傳進來的 document **永遠不由 T3 關閉**。"""
    caller_doc = _FakeDoc([_FakePage(words=_register_rows())])
    _plan([_page_dict(1, TABLE_MD)], caller_doc, tmp_path)
    assert caller_doc.closed == 0

    opened: list[_FakeDoc] = []

    class _FakeModule:
        @staticmethod
        def open(_path):
            doc = _FakeDoc([_FakePage(words=_register_rows())])
            opened.append(doc)
            return doc

    monkeypatch.setitem(__import__("sys").modules, "pymupdf", _FakeModule)
    pdf = tmp_path / "self.pdf"
    pdf.write_bytes(b"%PDF")
    fc.plan_document_figures(str(pdf), [_page_dict(1, TABLE_MD)], root=tmp_path)
    assert len(opened) == 1 and opened[0].closed == 1


def test_estimate_image_tokens_follows_config_patch(monkeypatch):
    """token 估算必須讀 **執行期** 的 config（`import config` 而不是 snapshot import）。"""
    monkeypatch.setattr(config, "FIGURE_IMAGE_TOKEN_PATCH_PX", 28)
    assert fc.estimate_image_tokens(1223, 184) == math.ceil(1223 / 28) * math.ceil(184 / 28)
    monkeypatch.setattr(config, "FIGURE_IMAGE_TOKEN_PATCH_PX", 14)
    assert fc.estimate_image_tokens(1223, 184) == math.ceil(1223 / 14) * math.ceil(184 / 14)
    assert fc.estimate_image_tokens(0, 100) == 0


def test_preflight_report_command_is_copy_pasteable_and_leaks_no_text(tmp_path: Path,
                                                                     monkeypatch):
    """報告要能直接複製貼上，而且**不得**含任何頁面文字（NDA）。"""
    monkeypatch.setattr(config, "FIGURE_MAX_VL_CALLS_PER_DOC", 0)
    directory = tmp_path / "with space"
    directory.mkdir()
    secret = "SECRET_CUSTOMER_TOKEN_0xDEAD"
    words = _log_lines(count=5) + [_word(72.0, 500.0, secret, size=9.0)]
    page = _FakePage(words=words)
    pdf = directory / "my spec.pdf"
    pdf.write_bytes(b"%PDF")
    plan = fc.plan_document_figures(str(pdf), [_page_dict(1, "log")],
                                    root=tmp_path, pdf_doc=_FakeDoc([page]))
    report = fc.format_preflight_report(plan)

    command = (f"python RAG.py {shlex.quote(str(pdf.resolve()))} "
               f"{shlex.quote(config.KNOWLEDGE_FILE)}")
    assert f"{command} --preflight\n" in report + "\n", report
    assert f"{command}\n" in report + "\n", report
    assert "'" in shlex.quote(str(pdf.resolve())), "路徑含空白時必須被 quote"
    assert "vl_calls_per_doc" in report
    assert secret not in report, "報告不得洩漏頁面文字"
    assert "dmesg" not in report


def test_preflight_report_lists_dropped_and_deferred_exactly(tmp_path: Path, monkeypatch):
    """報告要說清楚「丟了什麼、延後了什麼」——不無聲截斷，而且逐筆列出。"""
    monkeypatch.setattr(config, "FIGURE_MAX_CANDIDATES_PER_PAGE", 1)
    page = _FakePage(words=_register_rows() + _log_lines(),
                     images=[{"bbox": (400, 600, 560, 720), "xref": 3, "width": 40,
                              "height": 30, "digest": b"\x02" * 16, "has-mask": False,
                              "transform": (160.0, 0.0, 0.0, 120.0, 400.0, 600.0)}])
    plan = _plan([_page_dict(1, "mixed")], _FakeDoc([page]), tmp_path)
    report = fc.format_preflight_report(plan)

    dropped = plan.stats["dropped_candidates"]
    assert len(dropped) == 1, "測試前提：真的要有被丟掉的候選"
    assert "被丟棄的候選（不無聲截斷）：1" in report
    assert f"第 {dropped[0]['page']} 頁" in report and dropped[0]["reason"] in report

    deferred = plan.stats["deferred_to_legacy_lane"]
    assert deferred, "測試前提：真的要有被延後給 legacy lane 的區域"
    assert "已延後給既有 picture lane" in report
    for reason in {entry["reason"] for entry in deferred}:
        assert reason in report


def test_degraded_plan_report_says_structured_lane_disabled(tmp_path: Path):
    plan = fc.plan_document_figures(str(tmp_path / "missing.pdf"), [_page_dict(1, "x")],
                                    root=tmp_path, pdf_doc=None)
    report = fc.format_preflight_report(plan)
    assert "結構化 lane 已停用" in report


def test_planner_never_raises_on_broken_channels(tmp_path: Path):
    """每個 channel 個別壞掉都只記 slug，整份 ingest 不受影響。"""
    for broken in ("words", "image_info", "find_tables", "drawings"):
        page = _FakePage(words=_register_rows(), fail=(broken,))
        plan = _plan([_page_dict(1, TABLE_MD)], _FakeDoc([page]), tmp_path,
                     name=f"broken-{broken}.pdf")
        assert any(u.startswith(f"{broken}:") for u in plan.page_evidence[1].unavailable)


# ============================================================
# 真 pymupdf / 真 PDF（一律非 smoke；契約 §8）
# ============================================================
def _fz():
    return pytest.importorskip("pymupdf", reason="需要 PyMuPDF（pymupdf4llm 相依）")


def _p4l():
    return pytest.importorskip("pymupdf4llm", reason="PDF ingestion 需要 pymupdf4llm")


def _build_register_pdf(fz, path, *, rotate=0, crop=None, with_lines=True, log=False):
    doc = fz.open()
    page = doc.new_page(width=612, height=792)
    x0, y0 = 72, 90
    cols = [0, 120, 260, 340, 440]
    rows = [0, 22, 44, 66]
    if with_lines:
        for c in cols:
            page.draw_line(fz.Point(x0 + c, y0), fz.Point(x0 + c, y0 + rows[-1]))
        for r in rows:
            page.draw_line(fz.Point(x0, y0 + r), fz.Point(x0 + cols[-1], y0 + r))
    data = [["Name", "Address", "Bits", "Access"],
            ["CTRL0", "0x4000_0100", "[7:4]", "RW"],
            ["CTRL1", "0x4000_0104", "[3:0]", "RO"]]
    for ri, row in enumerate(data):
        for ci, cell in enumerate(row):
            page.insert_text((x0 + cols[ci] + 4, y0 + rows[ri] + 15), cell, fontsize=9)
    if log:
        for i, line in enumerate(["$ dmesg | tail", "[  0.000000] Booting kernel",
                                  "0x4000_0100 = 0xdeadBEEF", "INFO ready"]):
            page.insert_text((72, 560 + i * 13), line, fontsize=9, fontname="cour")
    if crop:
        page.set_cropbox(fz.Rect(*crop))
    if rotate:
        page.set_rotation(rotate)
    doc.save(str(path))
    doc.close()


def _plan_real(path, root, *, keep_open=False):
    fz = _fz()
    p4l = _p4l()
    pages = p4l.to_markdown(str(path), page_chunks=True, write_images=False)
    doc = fz.open(str(path))
    plan = fc.plan_document_figures(str(path), pages, root=root, pdf_doc=doc)
    if keep_open:
        return plan, doc, pages
    doc.close()
    return plan, None, pages


def test_pos_slices_raw_markdown_before_any_normalize(tmp_path: Path):
    """`pos` 的座標系是 **strip / normalize 之前**的 raw page text（workflow §5 evidence ①）。

    `extracted_document.normalize_document_text()` 會先 `.strip()`，開頭有空白的頁
    （圖在文字前面就會這樣）套用同一個 offset 會切到錯的地方。
    """
    fz = _fz()
    _p4l()
    from extracted_document import normalize_document_text

    pdf = tmp_path / "posspec.pdf"
    doc = fz.open()
    page = doc.new_page(width=612, height=792)
    pix = fz.Pixmap(fz.csRGB, fz.IRect(0, 0, 64, 64))
    pix.clear_with(120)
    page.insert_image(fz.Rect(400, 40, 520, 90), pixmap=pix)   # 讓 markdown 以空白開頭
    x0, y0 = 72, 200
    for ri, row in enumerate([["Name", "Address"], ["CTRL0", "0x4000_0100"]]):
        for ci, cell in enumerate(row):
            page.insert_text((x0 + ci * 140, y0 + ri * 22), cell, fontsize=9)
    for c in (0, 140, 280):
        page.draw_line(fz.Point(x0 + c - 4, y0 - 12), fz.Point(x0 + c - 4, y0 + 34))
    for r in (-12, 10, 34):
        page.draw_line(fz.Point(x0 - 4, y0 + r), fz.Point(x0 + 276, y0 + r))
    doc.save(str(pdf))
    doc.close()

    plan, _doc, _pages = _plan_real(pdf, tmp_path)
    evidence = plan.page_evidence[1]
    raw = evidence.raw_markdown
    assert raw != raw.strip(), "raw_markdown 必須是 strip 之前的原文"
    table_boxes = [b for b in evidence.page_boxes if b["class"] == "table" and b["_pos"]]
    assert table_boxes, [b["class"] for b in evidence.page_boxes]
    start, end = table_boxes[0]["_pos"]
    assert "CTRL0" in raw[start:end] and raw[start:end].lstrip().startswith("|")
    normalized = normalize_document_text(raw)
    assert normalized[start:end] != raw[start:end], (
        "normalize 之後 offset 會位移；這條就是釘住『不得對 normalized 文字套 pos』")


def test_ruled_register_table_yields_native_candidate_with_cell_geometry(tmp_path: Path):
    """原生表格候選要帶 `pos`、`markdown` 與**每個 cell 的 bbox**（契約 §12.4）。

    只有整表 bbox 的話，verifier 的 `cell_geometry` 永遠是 False，`native_verified`
    就永遠達不到。
    """
    fz = _fz()
    pdf = tmp_path / "table.pdf"
    _build_register_pdf(fz, pdf)
    plan, _doc, _pages = _plan_real(pdf, tmp_path)

    tables = [c for c in plan.candidates if c.kind == fe.KIND_TABLE and c.native_table]
    assert tables, [(c.kind, c.reasons) for c in plan.candidates]
    candidate = tables[0]
    start, end = candidate.native_table["pos"]
    raw = plan.page_evidence[1].raw_markdown
    assert raw[start:end] == candidate.native_table["markdown"]
    assert "0x4000_0100" in candidate.native_table["markdown"], (
        "原始 markdown 必須保留 hex 底線；`Table.extract()` 會把它讀成 `0x4000 0100\\n_`")
    geometry = candidate.native_table["geometry"]
    assert geometry["row_count"] == 3 and geometry["col_count"] == 4
    assert geometry["header_names"] == ["Name", "Address", "Bits", "Access"]
    cells = geometry["cells"]
    assert len(cells) == 3 and all(len(row) == 4 for row in cells)
    # 每個 cell bbox 必須真的包住對應 ground truth 的字
    words = plan.page_evidence[1].words
    def _word_box(text):
        return next(w[:4] for w in words if w[4] == text)
    for text, (ri, ci) in (("CTRL0", (1, 0)), ("0x4000_0100", (1, 1)),
                           ("[3:0]", (2, 2)), ("RO", (2, 3))):
        cell = cells[ri][ci]
        box = _word_box(text)
        assert cell[0] <= box[0] and cell[2] >= box[2], (text, cell, box)
        assert cell[1] <= box[1] and cell[3] >= box[3], (text, cell, box)
    assert geometry["extract_unreliable_underscore"] is True

    # `PageEvidence.tables` 的 entry 要附 bbox 與**同一份** geometry，verifier 才挑得出
    # 對應候選的那一張（契約 §12.4）。兩處形狀不一致 = `cell_geometry` 永遠 False。
    entry = plan.page_evidence[1].tables[candidate.native_table["strategy"]][0]
    assert entry["bbox"] is not None and entry["degenerate"] is False
    assert entry["geometry"] == geometry
    assert set(geometry) >= {"cells", "rows", "cols", "table_bbox", "row_count", "col_count"}
    assert geometry["rows"] and geometry["cols"]


def test_borderless_dense_memory_map_is_found(tmp_path: Path):
    """無框線、文字密集的 memory map 仍要被候選器找到（workflow §5 table ⑧）。"""
    fz = _fz()
    pdf = tmp_path / "borderless.pdf"
    doc = fz.open()
    page = doc.new_page(width=612, height=792)
    rows = [["Name", "Address", "Bits", "Access"],
            ["CTRL0", "0x4000_0100", "[7:4]", "RW"],
            ["CTRL1", "0x4000_0104", "[3:0]", "RO"],
            ["CTRL2", "0x4000_0108", "[15:8]", "RW"]]
    for ri, row in enumerate(rows):
        for ci, cell in enumerate(row):
            page.insert_text((72 + ci * 110, 120 + ri * 18), cell, fontsize=9)
    doc.save(str(pdf))
    doc.close()
    plan, _doc, _pages = _plan_real(pdf, tmp_path)
    assert any(c.kind == fe.KIND_TABLE for c in plan.candidates), \
        [(c.kind, c.kind_scores, c.reasons) for c in plan.candidates]
    assert not any(c.signals["ruled"]["grid"] for c in plan.candidates), "這份沒有框線"


def test_degenerate_find_tables_is_recorded_and_never_a_candidate_source(tmp_path: Path):
    """實測：`find_tables(strategy="text")` 可能回一個 `t.bbox` 直接 raise 的 Table。

    退化項要留在 evidence（標 `degenerate=True` + 錯誤 slug），但**永不**成為候選來源，
    而且不得讓整條 ingest 掛掉。
    """
    fz = _fz()
    pdf = tmp_path / "degen.pdf"
    _build_register_pdf(fz, pdf, log=True)
    plan, _doc, _pages = _plan_real(pdf, tmp_path)
    entries = plan.page_evidence[1].tables["text"]
    degenerate = [e for e in entries if e["degenerate"]]
    assert degenerate, entries
    assert any("bbox" in err or "col_count" in err or "rows" in err
               for e in degenerate for err in e["errors"]), degenerate
    for candidate in plan.candidates:
        assert "evidence_find_tables_text" not in candidate.reasons


def test_rotated_page_bbox_is_exact_unrotated_and_page_boxes_are_derotated(tmp_path: Path):
    """純旋轉頁：座標一律回到 unrotated 空間，且 `page_boxes` 真的被 derotate。

    「對上就用、對不上就 unaligned」是不夠的——純 rotation 本來就可證明，
    永遠 abstain 也會讓測試過（審核 BLOCKER 23）。這裡斷言**精確**的 unrotated bbox。
    """
    fz = _fz()
    pdf = tmp_path / "rot.pdf"
    _build_register_pdf(fz, pdf, rotate=90)
    plan, _doc, _pages = _plan_real(pdf, tmp_path)
    evidence = plan.page_evidence[1]

    assert evidence.rotation == 90
    assert evidence.page_rect == (0.0, 0.0, 612.0, 792.0), evidence.page_rect
    spaces = {b["_space"] for b in evidence.page_boxes}
    assert spaces == {"derotate"}, spaces
    assert "page_boxes_geometry:unalignable" not in evidence.unavailable

    table_box = next(b for b in evidence.page_boxes if b["class"] == "table")
    raw = table_box["_bbox_raw"]
    derotated = table_box["_bbox_unrotated"]
    # 精確值：/Rotate 90 的 derotation 是 (x, y) -> (y, 792 - x)
    assert derotated == pytest.approx((raw[1], 792.0 - raw[2], raw[3], 792.0 - raw[0])), \
        (raw, derotated)
    for candidate in plan.candidates:
        assert candidate.bbox[2] <= 612.0 and candidate.bbox[3] <= 792.0
        assert "rotated_page_markdown_unreliable" in candidate.reasons or \
            candidate.native_table is None
    # 旋轉頁的 find_tables 幾何無法證明 → 一律標 degenerate，不當候選來源
    for strategy in ("lines", "lines_strict", "text"):
        for entry in evidence.tables[strategy]:
            assert entry["degenerate"]
            assert "rotated_page_geometry_unprovable" in entry["errors"]


def test_crop_plus_rotation_abstains_from_page_box_geometry(tmp_path: Path):
    """rotation + cropbox 併發時上游 `page_boxes` 幾何錯亂（實測）→ abstain。

    `pos` / `class` 仍可用，但 bbox 一律不用；不得因為「大框湊巧 IoU 高」就接受錯的座標系。
    """
    fz = _fz()
    pdf = tmp_path / "croprot.pdf"
    _build_register_pdf(fz, pdf, rotate=90, crop=(50, 60, 580, 760))
    plan, _doc, _pages = _plan_real(pdf, tmp_path)
    evidence = plan.page_evidence[1]
    assert evidence.page_rect == (0.0, 0.0, 530.0, 700.0)
    assert "page_boxes_geometry:unalignable" in evidence.unavailable
    assert all(b["_bbox_unrotated"] is None for b in evidence.page_boxes)
    assert any(b["_pos"] for b in evidence.page_boxes), "pos 仍要保留"
    for candidate in plan.candidates:
        assert candidate.native_table is None or candidate.native_table["geometry"] is None


def test_cropbox_words_table_and_render_bbox_are_cropbox_relative(tmp_path: Path):
    """cropbox：words / table 幾何 / render 範圍全部是 cropbox 相對座標。"""
    fz = _fz()
    pdf = tmp_path / "crop.pdf"
    _build_register_pdf(fz, pdf, crop=(50, 60, 580, 760))
    plan, doc, _pages = _plan_real(pdf, tmp_path, keep_open=True)
    try:
        evidence = plan.page_evidence[1]
        assert evidence.page_rect == (0.0, 0.0, 530.0, 700.0)
        # 原本畫在 (72,90) 的表格框線，扣掉 cropbox 原點 (50,60) → (22,30)
        name_word = next(w for w in evidence.words if w[4] == "Name")
        assert name_word[0] == pytest.approx(26.0, abs=0.5)
        assert name_word[1] == pytest.approx(35.3, abs=0.5)
        candidate = next(c for c in plan.candidates if c.native_table)
        assert candidate.bbox[0] == pytest.approx(22.0, abs=1.0)
        assert candidate.native_table["geometry"]["table_bbox"][0] == pytest.approx(22.0, abs=1.0)
        variants = fc.render_candidate_variants(doc, candidate)
        assert variants[0].bbox[0] == pytest.approx(22.0, abs=1.0)
        assert variants[0].width > 2 and variants[0].height > 2
    finally:
        doc.close()


def test_legacy_contract_pdf_yields_zero_structured_candidates(tmp_path: Path):
    """完整重建 `tests/test_rag_pdf_ingest.py::test_real_pymupdf4llm_contract` 的 PDF。

    那條測試斷言 legacy lane 送出 **3 次** VL、figure 頁 `{2,3,4}`。只要這裡冒出任何
    structured 候選，T7 的「IoU 跳過已被 structured 覆蓋的 picture 框」就會讓它變紅，
    而 Gate 0 的刻意變更清單沒有它（契約 §13.1）。
    """
    fz = _fz()
    _p4l()
    big = (72.0, 110.0, 300.0, 340.0)
    tiny = (500.0, 20.0, 512.0, 32.0)
    text_a = "Chapter 1 Overview. The NPU has 8 compute cores and a shared 4MB SRAM block."
    text_b = "Chapter 2 Limits. Max tensor height and width is 4096 for conv2d inputs."
    pdf = tmp_path / "mixed_spec.pdf"
    doc = fz.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), text_a)
    p2 = doc.new_page()
    pix = fz.Pixmap(fz.csRGB, fz.IRect(0, 0, 64, 64))
    pix.clear_with(120)
    p2.insert_image(fz.Rect(72, 72, 400, 400), pixmap=pix)
    p3 = doc.new_page()
    p3.insert_text((72, 72), text_b)
    p3.insert_image(fz.Rect(*big), pixmap=pix)
    p3.insert_image(fz.Rect(*tiny), pixmap=pix)
    pix2 = fz.Pixmap(fz.csRGB, fz.IRect(0, 0, 64, 64))
    pix2.clear_with(220)
    for _ in range(2):
        page = doc.new_page()
        page.insert_text((72, 72), text_a)
        page.insert_image(fz.Rect(*big), pixmap=pix2)
    doc.save(str(pdf))
    doc.close()

    plan, _doc, _pages = _plan_real(pdf, tmp_path)
    assert plan.candidates == [], [(c.page, c.kind, c.reasons) for c in plan.candidates]
    assert plan.preflight["vl_calls_max"] == 0
    reasons = {e["reason"] for e in plan.stats["deferred_to_legacy_lane"]}
    assert reasons <= {"picture_only", "raster_no_structural_evidence",
                       "page_fallback_no_structural_evidence", "kind_diagram_legacy_lane"}, reasons
    assert any(e["page"] == 2 for e in plan.stats["deferred_to_legacy_lane"])


def test_near_textless_page_records_fallback_but_claims_nothing(tmp_path: Path):
    """近乎無文字頁：`fallback` 要記下來，但**不得**因此宣稱有 table/terminal。"""
    fz = _fz()
    pdf = tmp_path / "scan.pdf"
    doc = fz.open()
    page = doc.new_page()
    pix = fz.Pixmap(fz.csRGB, fz.IRect(0, 0, 64, 64))
    pix.clear_with(120)
    page.insert_image(fz.Rect(72, 72, 500, 700), pixmap=pix)
    doc.save(str(pdf))
    doc.close()
    plan, _doc, _pages = _plan_real(pdf, tmp_path)
    evidence = plan.page_evidence[1]
    assert evidence.fallback["reason"] == "near_zero_text"
    assert evidence.fallback["bbox"] == evidence.page_rect
    assert plan.candidates == []
    assert plan.stats["deferred_to_legacy_lane"]


def test_tiles_cut_on_band_gaps_with_overlap_and_stitch_evidence(tmp_path: Path, monkeypatch):
    """tiling 依 baseline 切，切點落在帶與帶之間，overlap 與 stitch evidence 齊全。

    **不按固定高度盲切**：任何一個切點落進某一行的內部就是把一行剖兩半。
    """
    fz = _fz()
    monkeypatch.setattr(config, "FIGURE_MAX_IMAGE_TOKENS_PER_CALL", 400)
    pdf = tmp_path / "log.pdf"
    doc = fz.open()
    page = doc.new_page(width=612, height=792)
    for i in range(40):
        page.insert_text((72, 100 + i * 15),
                         f"[{i:>5}.000000] line {i} 0x4000_{i:04d} ready",
                         fontsize=9, fontname="cour")
    doc.save(str(pdf))
    doc.close()

    plan, real_doc, _pages = _plan_real(pdf, tmp_path, keep_open=True)
    try:
        candidate = next(c for c in plan.candidates if c.kind == fe.KIND_TERMINAL)
        tile_plan = candidate.signals["tile_plan"]
        tiles = tile_plan["tiles"]
        bands = candidate.signals["bands"]
        assert len(tiles) > 1, tile_plan

        covered = []
        for tile in tiles:
            first, last = tile["band_range"]
            covered.extend(range(first, last + 1))
        assert covered == list(range(1, len(bands) + 1)), "band 不得重複也不得漏掉"

        spans = [(b["y0"], b["y1"]) for b in bands]
        for tile in tiles:
            for cut in (tile["cut_y_top"], tile["cut_y_bottom"]):
                if cut is None:
                    continue
                assert not any(y0 < cut < y1 for y0, y1 in spans), (cut, "切點切進某一行內部")
        assert tiles[0]["tile_index"] == 1 and tiles[-1]["tile_total"] == len(tiles)
        assert tiles[0]["overlap_px"] == 0
        assert all(t["overlap_px"] > 0 for t in tiles[1:])
        assert all(t["est_image_tokens"] <= config.FIGURE_MAX_IMAGE_TOKENS_PER_CALL
                   for t in tiles)
        assert f"image_tokens_per_call:{candidate.index}" not in plan.over_budget

        variants = fc.render_candidate_variants(real_doc, candidate)
        assert len(variants) == len(tiles)
        assert [v.tile_index for v in variants] == [t["tile_index"] for t in tiles]
        assert all(v.tile_total == len(tiles) for v in variants)
        assert all(v.stitch["band_range"] == t["band_range"]
                   for v, t in zip(variants, tiles))
        assert all(v.est_image_tokens <= config.FIGURE_MAX_IMAGE_TOKENS_PER_CALL
                   for v in variants)
        assert variants[1].variant_id.endswith(f"#tile2of{len(tiles)}")
    finally:
        real_doc.close()


def test_single_oversized_band_is_never_split(tmp_path: Path, monkeypatch):
    """一行/一列本身就超限 → 整行一張並標 `oversized_band` + fail-loud，絕不剖半。"""
    fz = _fz()
    monkeypatch.setattr(config, "FIGURE_MAX_IMAGE_TOKENS_PER_CALL", 4)
    pdf = tmp_path / "wide.pdf"
    doc = fz.open()
    page = doc.new_page(width=1200, height=300)
    for i in range(4):
        page.insert_text((20, 60 + i * 30), "0x4000_0100 " * 12, fontsize=9, fontname="cour")
    doc.save(str(pdf))
    doc.close()
    plan, _doc, _pages = _plan_real(pdf, tmp_path)
    candidate = plan.candidates[0]
    tiles = candidate.signals["tile_plan"]["tiles"]
    assert all(t["band_range"][0] == t["band_range"][1] for t in tiles)
    assert any(t["oversized_band"] for t in tiles)
    assert f"image_tokens_per_call:{candidate.index}" in plan.over_budget
    with pytest.raises(fe.FigureBudgetError):
        fc.check_preflight(plan)


def test_real_vector_log_falls_back_to_vl_lane(tmp_path: Path):
    """真 pymupdf4llm 的向量 log：markdown 是**逐行加裝飾**的 `list-item`，不是一塊原文。

    實測輸出是 ``- `$ dmesg | tail` `` 這種 bullet，而且一個候選對到五個 `pos`。
    那不構成「一塊 `pos` 支撐的 raw markdown 文字」→ 必須走 VL lane（契約 §15.1），
    否則 preflight 會算成零 VL、實際卻拿裝飾過的文字當 canonical。
    """
    fz = _fz()
    pdf = tmp_path / "veclog.pdf"
    doc = fz.open()
    page = doc.new_page(width=612, height=792)
    for i, line in enumerate(["$ dmesg | tail", "[    0.000000] Booting kernel",
                              "[    0.123456] INFO ready 0xdeadBEEF",
                              "[    1.000000] WARN retry", "$ echo done"]):
        page.insert_text((72, 100 + i * 15), line, fontsize=9, fontname="cour")
    doc.save(str(pdf))
    doc.close()

    plan, _doc, _pages = _plan_real(pdf, tmp_path)
    candidate = next(c for c in plan.candidates if c.kind == fe.KIND_TERMINAL)
    boxes = plan.page_evidence[1].page_boxes
    assert len(boxes) > 1, "測試前提：上游把 log 拆成多個 box"
    assert candidate.signals["native_lane"] is False
    assert candidate.signals["native_text"] is None
    assert "vl_lane_word_only_terminal" in candidate.reasons
    assert plan.preflight["vl_calls_min"] > 0
    assert plan.preflight["native_lane_candidates"] == 0


def test_terminal_scoring_is_background_independent(tmp_path: Path):
    """白底與暗底的同一段 log 走**完全相同**的路徑（workflow §5 terminal ③）。

    kind 評分刻意零顏色訊號——「暗底 >60%」這類必要條件會讓淺色主題、紙本列印的
    terminal 直接消失。
    """
    fz = _fz()
    lines = ["$ dmesg | tail", "[    0.000000] Booting kernel",
             "[    0.123456] INFO ready 0xdeadBEEF", "[    1.000000] WARN retry",
             "$ echo done"]

    def _build(path, dark):
        doc = fz.open()
        page = doc.new_page(width=612, height=792)
        if dark:
            page.draw_rect(fz.Rect(60, 90, 400, 190), color=(0, 0, 0), fill=(0, 0, 0))
        for i, line in enumerate(lines):
            page.insert_text((72, 100 + i * 15), line, fontsize=9, fontname="cour",
                             color=(1, 1, 1) if dark else (0, 0, 0))
        doc.save(str(path))
        doc.close()

    light = tmp_path / "light.pdf"
    dark = tmp_path / "dark.pdf"
    _build(light, False)
    _build(dark, True)
    plan_light, _d1, _p1 = _plan_real(light, tmp_path)
    plan_dark, _d2, _p2 = _plan_real(dark, tmp_path)

    kinds_light = [c.kind for c in plan_light.candidates]
    kinds_dark = [c.kind for c in plan_dark.candidates]
    assert fe.KIND_TERMINAL in kinds_light, [
        (c.kind, c.kind_scores) for c in plan_light.candidates]
    assert fe.KIND_TERMINAL in kinds_dark, [
        (c.kind, c.kind_scores) for c in plan_dark.candidates]
    light_terminal = next(c for c in plan_light.candidates if c.kind == fe.KIND_TERMINAL)
    dark_terminal = next(c for c in plan_dark.candidates if c.kind == fe.KIND_TERMINAL)
    assert light_terminal.kind_scores[fe.KIND_TERMINAL] == \
        dark_terminal.kind_scores[fe.KIND_TERMINAL]


def test_boundary_word_is_assigned_not_clipped():
    """跨候選 bbox 邊界的字仍要進 band（**不得**先用 `clip` 截斷）。"""
    words = _register_rows()
    bbox = (72.0, 100.0, 420.0, 160.0)
    bands = fc._word_bands(words, bbox)
    texts = " ".join(b["text"] for b in bands)
    assert "Access" in texts, "最右欄跨出 bbox 右緣，仍必須被指派進來"


def test_pure_raster_variant_carries_original_bytes_and_mime(tmp_path: Path):
    """能證明是無 overlay 純 raster → 送 `extract_image()` 的**原始 binary**。

    `Variant.png` 的欄名不變，但 `variant_id == "raster"` 時裝的是原始 bytes，
    真實型別看 `mime`（契約 §12.3 / §13.2；T5 的 `assets/` 就是拿這個）。
    """
    fz = _fz()
    _p4l()
    pdf = tmp_path / "scan_table.pdf"
    doc = fz.open()
    page = doc.new_page(width=612, height=792)
    pix = fz.Pixmap(fz.csRGB, fz.IRect(0, 0, 120, 90))
    pix.clear_with(200)
    page.insert_image(fz.Rect(100, 100, 400, 325), pixmap=pix)
    doc.save(str(pdf))
    doc.close()

    p4l = _p4l()
    pages = p4l.to_markdown(str(pdf), page_chunks=True, write_images=False)
    raw = pages[0].get("text") or ""
    # 模擬上游把掃描表格區域分類成 table：這是唯一能讓純 raster 升格的路徑
    fake_raw = "|A|B|\n|---|---|\n|1|2|\n" + raw
    pages[0] = {"metadata": {"page_number": 1}, "text": fake_raw,
                "page_boxes": [{"class": "table", "bbox": (100, 100, 400, 325),
                                "pos": (0, 18)}]}
    real = fz.open(str(pdf))
    try:
        plan = fc.plan_document_figures(str(pdf), pages, root=tmp_path, pdf_doc=real)
        assert plan.candidates, plan.stats["deferred_to_legacy_lane"]
        candidate = plan.candidates[0]
        assert candidate.asset_xref is not None, candidate.signals["raster_purity"]
        assert candidate.signals["raster_purity"]["pure"] is True
        variants = fc.render_candidate_variants(real, candidate)
        assert len(variants) == 1
        variant = variants[0]
        assert variant.variant_id == "raster"
        assert variant.mime in ("image/png", "image/jpeg")
        assert (variant.width, variant.height) == (120, 90), "原圖尺寸，不是 render 尺寸"
        original = real.extract_image(candidate.asset_xref)["image"]
        assert variant.png == original, "必須是原始 binary，不得重新編碼"
        assert variant.est_image_tokens == fc.estimate_image_tokens(120, 90)
    finally:
        real.close()


@pytest.mark.parametrize("overlay", ["text", "drawing", "annot"])
def test_overlay_forces_page_crop_instead_of_raw_asset(overlay, tmp_path: Path):
    """有文字 / 向量 / annotation 覆蓋 → 一律 page crop（原始 bytes 看不到覆蓋物）。"""
    fz = _fz()
    p4l = _p4l()
    pdf = tmp_path / f"overlay_{overlay}.pdf"
    doc = fz.open()
    page = doc.new_page(width=612, height=792)
    pix = fz.Pixmap(fz.csRGB, fz.IRect(0, 0, 120, 90))
    pix.clear_with(200)
    page.insert_image(fz.Rect(100, 100, 400, 325), pixmap=pix)
    if overlay == "text":
        page.insert_text((120, 200), "OVERLAY 0x4000_0100", fontsize=9)
    elif overlay == "drawing":
        page.draw_line(fz.Point(110, 150), fz.Point(390, 150))
    else:
        page.add_rect_annot(fz.Rect(120, 120, 380, 300))
    doc.save(str(pdf))
    doc.close()

    pages = p4l.to_markdown(str(pdf), page_chunks=True, write_images=False)
    pages[0] = {"metadata": {"page_number": 1}, "text": "|A|B|\n|---|---|\n|1|2|\n",
                "page_boxes": [{"class": "table", "bbox": (100, 100, 400, 325),
                                "pos": (0, 18)}]}
    real = fz.open(str(pdf))
    try:
        plan = fc.plan_document_figures(str(pdf), pages, root=tmp_path, pdf_doc=real)
        assert plan.candidates, (
            "測試前提：這個 fixture 必須產生候選，否則 purity 分支根本沒被執行到 "
            f"（deferred={plan.stats['deferred_to_legacy_lane']}）")
        candidate = plan.candidates[0]
        assert candidate.asset_xref is None, candidate.signals["raster_purity"]
        assert candidate.signals["raster_purity"]["reason"] in (
            "text_overlay", "vector_overlay", "annots_overlay", "widgets_overlay")
        variants = fc.render_candidate_variants(real, candidate)
        assert all(v.variant_id.startswith("crop@") for v in variants)
        assert all(v.mime == "image/png" for v in variants)
    finally:
        real.close()


def test_render_crop_on_rotated_page_targets_the_right_pixels(tmp_path: Path):
    """旋轉頁的 crop 必須用 `bbox * rotation_matrix` 換到 display 空間。

    只驗尺寸不夠——切到頁面另一區也可能剛好同尺寸（審核 BLOCKER 23）。這裡用
    非對稱色塊，直接核對 render 出來的像素顏色。
    """
    fz = _fz()
    pdf = tmp_path / "rotmark.pdf"
    doc = fz.open()
    page = doc.new_page(width=612, height=792)
    page.draw_rect(fz.Rect(100, 100, 300, 200), color=None, fill=(1, 0, 0))
    page.draw_rect(fz.Rect(100, 600, 300, 700), color=None, fill=(0, 0, 1))
    page.set_rotation(90)
    doc.save(str(pdf))
    doc.close()

    real = fz.open(str(pdf))
    try:
        target = (100.0, 100.0, 300.0, 200.0)
        data = {
            "bbox": target,
            "signals": {"bands": [], "raster_purity": {"pure": False, "width": 0, "height": 0}},
        }
        tile_plan = fc._plan_tiles(data)
        candidate = fc.Candidate(
            index=1, page=1, bbox=target, page_rect=(0.0, 0.0, 612.0, 792.0),
            kind_scores={}, kind=fe.KIND_TABLE,
            signals={"tile_plan": tile_plan, "document_id": "x::0"},
            reasons=[], signature="", native_table=None, occurrences=[],
            asset_xref=None, asset_digest="d",
            figure_id=fe.figure_id_for("x::0", 1, target, (0.0, 0.0, 612.0, 792.0), "d"),
            document_id="x::0")
        variant = fc.render_candidate_variants(real, candidate)[0]
        pix = fz.Pixmap(variant.png)
        centre = pix.pixel(pix.width // 2, pix.height // 2)
        assert centre[0] > 200 and centre[1] < 60 and centre[2] < 60, (
            f"render 到錯的區域（取到 {centre}，預期紅色）")
    finally:
        real.close()


def test_glyph_floor_raises_zoom_and_flags_when_unreachable(tmp_path: Path, monkeypatch):
    """render 決策依「有效 glyph pixels」，不是只看 DPI。

    壓不到最小字高時要誠實標記——**提高 DPI 不會創造原檔沒有的資訊**。
    """
    fz = _fz()
    pdf = tmp_path / "tiny.pdf"
    doc = fz.open()
    page = doc.new_page(width=612, height=792)
    for i in range(12):
        page.insert_text((72, 100 + i * 7),
                         f"$ tiny 0x{i:04x} line item {i} value 0x4000_{i:04d}",
                         fontsize=3.5, fontname="cour")
    doc.save(str(pdf))
    doc.close()

    monkeypatch.setattr(config, "FIGURE_MIN_GLYPH_PX", 40)
    plan, _doc, _pages = _plan_real(pdf, tmp_path)
    candidate = plan.candidates[0]
    tile_plan = candidate.signals["tile_plan"]
    assert tile_plan["zoom"] > config.FIGURE_RENDER_TARGET_DPI / 72.0, tile_plan

    monkeypatch.setattr(config, "FIGURE_RENDER_MAX_SIDE_PX", 200)
    plan2, _d, _p = _plan_real(pdf, tmp_path)
    candidate2 = plan2.candidates[0]
    assert candidate2.signals["tile_plan"]["glyph_px_below_min"] is True
    assert "glyph_below_min_px" in candidate2.reasons
    assert "低於最小字高" in fc.format_preflight_report(plan2)


@pytest.mark.parametrize("mode", ["bad_page", "bad_xref", "empty_bytes"])
def test_render_failures_are_wrapped_with_full_locator(mode, tmp_path: Path):
    """所有 render / extract 失敗都包成 `FigureError`，訊息帶身分、頁、候選、figure_id。"""
    figure_id = fe.figure_id_for("doc::0", 3, (0, 0, 10, 10), (0, 0, 100, 100), "d")
    tile_plan = {"mode": "raster" if mode != "bad_page" else "crop", "zoom": 1.0,
                 "effective_dpi": 72,
                 "tiles": [{"tile_index": 0, "tile_total": 1, "bbox": [0, 0, 10, 10],
                            "overlap_px": 0}]}
    candidate = fc.Candidate(
        index=5, page=3, bbox=(0, 0, 10, 10), page_rect=(0, 0, 100, 100), kind_scores={},
        kind=fe.KIND_TABLE, signals={"tile_plan": tile_plan, "document_id": "doc::0"},
        reasons=[], signature="", native_table=None, occurrences=[],
        asset_xref=None if mode == "bad_page" else 42, asset_digest="d",
        figure_id=figure_id, document_id="doc::0")
    images = {42: {"image": b"", "width": 1, "height": 1, "ext": "png"}} \
        if mode == "empty_bytes" else {}
    doc = _FakeDoc([_FakePage()], images=images)

    with pytest.raises(fe.FigureError) as exc:
        fc.render_candidate_variants(doc, candidate)
    message = str(exc.value)
    assert "doc::0" in message and "第 3 頁" in message and "#5" in message
    assert figure_id in message


def test_render_without_tile_plan_is_loud():
    figure_id = fe.figure_id_for("doc::0", 1, (0, 0, 10, 10), (0, 0, 100, 100), "d")
    candidate = fc.Candidate(
        index=1, page=1, bbox=(0, 0, 10, 10), page_rect=(0, 0, 100, 100), kind_scores={},
        kind=fe.KIND_TABLE, signals={}, reasons=[], signature="", native_table=None,
        occurrences=[], asset_xref=None, asset_digest="d", figure_id=figure_id,
        document_id="doc::0")
    with pytest.raises(fe.FigureError, match="tile plan"):
        fc.render_candidate_variants(_FakeDoc([_FakePage()]), candidate)


# ============================================================
# smoke：產生端自檢——每個產出的 Variant 都要過共享 validator（契約 §21.1 / §21.2 T3）
# ============================================================
# 這條接縫連續四輪被打回，共同機制是：**同一個凍結介面有三個消費端，每輪只有被點名的
# 那一兩端收緊，第三端維持寬鬆**。§21 的結構性解法是門面出唯一 validator，
# 而**產生端**（本模組）必須保證自己交出去的東西一開始就合法——下游三端才有東西可以信。
#
# 這裡守的具體洞：plan dict 的 tile metadata 被 `int()` **靜默截斷**成合法值
# （`1.9` → `1`、`True` → `1`、`"1"` → `1`），於是一個型別錯的 plan 會產出一個
# 看起來完全合法的 Variant，四方 validator 全部無話可說。
class _FakePixmap:
    """`page.get_pixmap()` 的最小替身：只有 renderer 真的讀到的三樣東西。"""

    def __init__(self, width, height, png):
        self.width = width
        self.height = height
        self._png = png

    def tobytes(self, fmt):
        assert fmt == "png"
        return self._png


class _CropPage(_FakePage):
    """會 render 的假頁（crop 路徑）。"""

    def __init__(self, *, px=(200, 120), png=b"\x89PNG\r\n\x1a\n" + b"pixels", **kwargs):
        super().__init__(**kwargs)
        self._px = px
        self._png = png

    def get_pixmap(self, *, matrix=None, clip=None, alpha=False):
        del matrix, clip, alpha
        return _FakePixmap(self._px[0], self._px[1], self._png)


class _FakeRect:
    """`pymupdf.Rect`：renderer 只拿它做 `bbox * rotation_matrix` 再餵給 `get_pixmap`。"""

    def __init__(self, *coords):
        self.coords = tuple(float(c) for c in coords)

    def __mul__(self, matrix):
        del matrix
        return self


def _fake_pymupdf(monkeypatch):
    """crop 路徑不必真 PyMuPDF 也能跑（smoke 一律純記憶體；見檔頭）。"""
    module = types.ModuleType("pymupdf")
    module.Rect = _FakeRect
    module.Matrix = lambda zoom_x, zoom_y: (zoom_x, zoom_y)
    monkeypatch.setitem(sys.modules, "pymupdf", module)


_SELF_CHECK_BBOX = (10.0, 20.0, 110.0, 80.0)
_SELF_CHECK_PAGE_RECT = (0.0, 0.0, 612.0, 792.0)
_SELF_CHECK_TILE = {"tile_index": 0, "tile_total": 1, "bbox": list(_SELF_CHECK_BBOX),
                    "overlap_px": 0, "band_range": None, "est_image_tokens": 40}


def _self_check_candidate(tile_overrides=None, *, mode="crop", asset_xref=None):
    tile = dict(_SELF_CHECK_TILE)
    tile.update(tile_overrides or {})
    plan = {"mode": mode, "zoom": 1.0, "effective_dpi": 72, "tiles": [tile]}
    figure_id = fe.figure_id_for("doc::0", 1, _SELF_CHECK_BBOX, _SELF_CHECK_PAGE_RECT, "d")
    return fc.Candidate(
        index=7, page=1, bbox=_SELF_CHECK_BBOX, page_rect=_SELF_CHECK_PAGE_RECT,
        kind_scores={}, kind=fe.KIND_TABLE,
        signals={"tile_plan": plan, "document_id": "doc::0"},
        reasons=[], signature="", native_table=None, occurrences=[],
        asset_xref=asset_xref, asset_digest="d", figure_id=figure_id,
        document_id="doc::0")


def _validator_verdict(variant) -> str:
    """共享 validator 對這個 Variant 的判定（紅燈訊息要印出來的東西）。"""
    try:
        fe.validate_variant(variant, where="紅燈證據")
    except fe.FigureError as exc:
        return f"共享 validator 判定不合法：{exc}"
    return "共享 validator 判定**完全合法**（＝靜默截斷成功，四端都攔不下來）"


def _render_or_message(doc, candidate, *, produced_fields) -> str:
    """跑 renderer：fail-loud 就回訊息；成功就把「產出的欄位」當紅燈證據印出來。"""
    try:
        variants = fc.render_candidate_variants(doc, candidate)
    except fe.FigureError as exc:
        return str(exc)
    produced = variants[0]
    shown = ", ".join(f"{name}={getattr(produced, name)!r}" for name in produced_fields)
    pytest.fail(
        f"產生端沒有 fail-loud：Variant 交出去時是 {shown}；{_validator_verdict(produced)}")
    raise AssertionError("unreachable")


# plan dict 的值 → `int()` 之後的**合法** tile metadata（修正前全部靜默通過）
_SILENTLY_TRUNCATED_TILE = [
    ({"tile_total": 1.9}, "tile_total=1.9", "float_total"),
    ({"tile_total": True}, "tile_total=True", "bool_total"),
    ({"tile_total": "1"}, "tile_total='1'", "str_total"),
    ({"tile_index": 1.9, "tile_total": 3}, "tile_index=1.9", "float_index"),
    ({"tile_index": "2", "tile_total": 3}, "tile_index='2'", "str_index"),
    ({"overlap_px": 1.9}, "overlap_px=1.9", "float_overlap"),
]


@pytest.mark.smoke
@pytest.mark.parametrize(
    "overrides,culprit", [(o, c) for o, c, _ in _SILENTLY_TRUNCATED_TILE],
    ids=[i for _, _, i in _SILENTLY_TRUNCATED_TILE])
def test_render_never_coerces_tile_plan_numbers_into_legal_metadata(
        overrides, culprit, monkeypatch):
    """★ plan dict 的 tile metadata **不得**被 `int()` 靜默截斷（契約 §21.2 T3）。

    `1.9` / `True` / `"1"` 經 `int()` 之後全都變成合法的 `(tile_total=1, tile_index=0)`，
    產出的 Variant 因此通過**每一個**消費端的檢查——四輪審核都沒抓到的正是這一類：
    洞不在 validator，在**產生端先把壞值洗成好值**。

    產生端不猜、不轉型：原值進 `Variant`，再由共享 validator 判生死。
    """
    _fake_pymupdf(monkeypatch)
    candidate = _self_check_candidate(overrides)
    doc = _FakeDoc([_CropPage()])

    message = _render_or_message(
        doc, candidate, produced_fields=("tile_index", "tile_total", "overlap_px"))

    assert culprit in message, message
    # 契約 §5：訊息要帶得出定位（文件、頁、候選序號、figure_id）
    assert "doc::0" in message and "第 1 頁" in message and "#7" in message, message
    assert candidate.figure_id in message, message


# `extract_image()` 的欄位 → `int()` 之後的合法 Variant（同一類洞的 raster 版）
_SILENTLY_TRUNCATED_RASTER = [
    ({"width": "120"}, "width='120'", "str_width"),
    ({"height": 90.5}, "height=90.5", "float_height"),
    ({"width": 0}, "width=0", "zero_width"),
]


@pytest.mark.smoke
@pytest.mark.parametrize(
    "overrides,culprit", [(o, c) for o, c, _ in _SILENTLY_TRUNCATED_RASTER],
    ids=[i for _, _, i in _SILENTLY_TRUNCATED_RASTER])
def test_raster_variant_is_self_checked_before_it_leaves_the_producer(overrides, culprit):
    """raster 分支也要自檢：尺寸不是正整數就 fail-loud，不靠 `int()` 洗成合法值。

    `width=0` 會讓 `est_image_tokens` 一起變成 0，送出前的 image-token 預算複查
    因此形同虛設；`"120"` / `90.5` 則是被 `int()` 洗成看起來完全正常的尺寸，
    而 Variant 宣稱的尺寸與真正的 bytes 不再對得上。
    """
    image = {"image": b"\x89PNG\r\n\x1a\n" + b"raster", "width": 120, "height": 90,
             "ext": "png"}
    image.update(overrides)
    candidate = _self_check_candidate(mode="raster", asset_xref=42)
    doc = _FakeDoc([_FakePage()], images={42: image})

    message = _render_or_message(
        doc, candidate, produced_fields=("width", "height", "est_image_tokens"))

    assert culprit in message, message
    assert candidate.figure_id in message, message


@pytest.mark.smoke
def test_self_check_lets_a_legal_plan_through_untouched(monkeypatch):
    """自檢不得誤殺：合法 plan 照樣產出 Variant，欄位原封不動。

    沒有這條的話，「全部 raise」也能讓上面兩條變綠。
    """
    _fake_pymupdf(monkeypatch)
    png = b"\x89PNG\r\n\x1a\n" + b"pixels"
    candidate = _self_check_candidate({"tile_index": 2, "tile_total": 3, "overlap_px": 48})
    doc = _FakeDoc([_CropPage(px=(200, 120), png=png)])

    variants = fc.render_candidate_variants(doc, candidate)

    assert len(variants) == 1
    variant = variants[0]
    assert (variant.tile_index, variant.tile_total, variant.overlap_px) == (2, 3, 48)
    assert variant.variant_id == "crop@72dpi#tile2of3"
    assert variant.png == png
    assert variant.digest == hashlib.sha256(png).hexdigest()
    fe.validate_variant(variant, where="合法 plan 的產出")
