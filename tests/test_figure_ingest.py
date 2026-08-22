"""PDF 結構化 figure lane 的 ingest 契約（T7）。

守的是「靜默錯配 / 靜默改寫 / KB 半更新」那一類:原 page markdown 表被 structured
chunk 取代之後不得留下第二份、`pos` 不可信時不得亂切正文、抽取失敗與預算超限
一律零寫入、structured chunk 的 content 不得經過通用 normalize 與 splitter。

與 `tests/test_rag_pdf_ingest.py` 的分工:那一份守既有 legacy 圖面路徑
（`class=picture` → 自由文字 VL → `origin="diagram"`）的逐位元組保留;這一份守
新的 structured lane 與兩條 lane 的邊界。
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

import context_generation
import figure_extract
import figure_review
import figure_verify
import RAG


# ============================================================
# 假的 plan / candidate / result（T3/T4 尚未交付時也要能跑）
# ============================================================
# 欄位名＝契約 §6.3 / §6.4 的實質介面。`build_figure_chunks` 對 FigureResult 是
# duck typing（`_figure_view` 用 getattr），所以這些 dataclass 走的是真正的產線路徑。
@dataclass(frozen=True)
class FakeCandidate:
    index: int
    page: int
    bbox: tuple
    page_rect: tuple
    kind_scores: dict
    kind: str
    signals: dict
    reasons: list
    signature: str
    native_table: Optional[dict]
    occurrences: list
    asset_xref: Optional[int]
    asset_digest: str
    figure_id: str
    document_id: str


@dataclass(frozen=True)
class FakePageEvidence:
    page: int
    raw_markdown: str
    page_boxes: list
    words: list = field(default_factory=list)
    image_info: list = field(default_factory=list)
    tables: dict = field(default_factory=dict)
    drawing_clusters: list = field(default_factory=list)
    page_rect: tuple = (0.0, 0.0, 595.0, 842.0)
    rotation: int = 0
    unavailable: list = field(default_factory=list)


@dataclass(frozen=True)
class FakePlan:
    document_id: str
    candidates: list
    page_evidence: dict
    stats: dict
    preflight: dict
    over_budget: list


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"stub-pixels"

# fixture 裡幾乎每個候選都用這個框；`Variant.bbox` 要與候選框相同才算「完整原圖」
#（契約 §21.1），所以 Variant fixture 的預設值也必須對得上，不能是一個無關的框。
TABLE_BBOX = (60.0, 100.0, 520.0, 180.0)


@dataclass(frozen=True)
class FakeVariant:
    """契約 §6.3 的 `Variant`：**每一個欄位都填齊**，`digest` 是真的 sha256。

    以前 `digest` 預設空字串、`bbox` 預設一個與候選框無關的 `(0, 0, 1, 1)`。
    缺欄位／對不上的 fixture 會掩蓋 producer 漂移——「哪一張是完整原圖」這條接縫
    連續四輪沒被抓到，成因就是各 shard 的 Variant fixture 根本沒有把 §6.3 填滿
    （契約 §21.3）。
    """
    figure_id: str
    variant_id: str
    png: bytes = PNG_BYTES
    width: int = 40
    height: int = 20
    bbox: tuple = TABLE_BBOX
    tile_index: int = 0
    tile_total: int = 1
    overlap_px: int = 0
    est_image_tokens: int = 16
    # 空字串＝依 png 自動算真的 sha256（見 `__post_init__`）；要測「digest 與 bytes
    # 不符」時明示傳一個不對的值。
    digest: str = ""
    mime: str = "image/png"

    def __post_init__(self):
        if not self.digest:
            object.__setattr__(self, "digest", hashlib.sha256(self.png).hexdigest())


@dataclass(frozen=True)
class FakeFigureResult:
    figure_id: str
    document_id: str
    page: int
    figure_index: int
    bbox: tuple
    kind: str
    revision: int
    payload: Optional[dict]
    extraction_status: str
    verification_status: str
    reasons: list
    reason_details: list
    evidence: dict
    occurrences: list
    model_input_variant: str
    variants: list
    row_total: Optional[int]
    line_total: Optional[int]


# ============================================================
# fixture 素材
# ============================================================
PAGE_RECT = (0.0, 0.0, 595.0, 842.0)

INTRO = "Register overview paragraph for the control block. \n\n"
TABLE_MD = (
    "|Name|Addr|Bits|Access|Description|\n"
    "|---|---|---|---|---|\n"
    "|CTRL0|0x4000_0100|[7:4]|RW|clock select|\n"
    "|CTRL1|0x4000_0104|[3:0]|RO|status flags|\n"
)
TAIL = "\n\n\nTrailing paragraph after the table. \n\n"
PAGE1 = INTRO + TABLE_MD + TAIL
# 比照實測（pymupdf4llm 1.28.0）:table box 的 pos 會把尾端多餘換行一起含進來，
# 而 raw[end] 是下一段的首字（不是換行）→ 剛好打到 marker 的後綴換行守衛。
POS1 = (len(INTRO), len(INTRO) + len(TABLE_MD) + 3)

BIG_BBOX_FOR_LANE = (72.0, 400.0, 300.0, 630.0)   # legacy picture 框，與表格不重疊
GROUND_TRUTH = [
    ["CTRL0", "0x4000_0100", "[7:4]", "RW", "clock select"],
    ["CTRL1", "0x4000_0104", "[3:0]", "RO", "status flags"],
]


def _table_payload(labels, rows):
    columns = [{"column_id": f"c{i + 1}", "label": label, "role": None}
               for i, label in enumerate(labels)]
    return {
        "kind": figure_extract.KIND_TABLE,
        "columns": columns,
        "rows": [
            {"row_index": index,
             "cells": [{"column_id": columns[i]["column_id"], "text": text,
                        "state": figure_extract.CELL_STATE_OBSERVED,
                        "inherited_from_row": None}
                       for i, text in enumerate(cells)]}
            for index, cells in enumerate(rows, 1)
        ],
        "footnotes": [],
    }


def _terminal_payload(lines):
    return {
        "kind": figure_extract.KIND_TERMINAL,
        "lines": [{"line_index": i, "text": text, "uncertain_spans": []}
                  for i, text in enumerate(lines, 1)],
    }


def _page(number: int, text: str, boxes=None) -> dict:
    return {"metadata": {"page_number": number}, "text": text,
            "page_boxes": list(boxes or [])}


def _table_box(pos, bbox=TABLE_BBOX, index: int = 1) -> dict:
    return {"index": index, "class": "table", "bbox": bbox, "pos": pos}


def _occurrence(page: int, bbox=TABLE_BBOX, index: int = 0) -> dict:
    return {"page": page, "bbox": list(bbox), "index": index}


def _write_pdf(tmp_path: Path, name: str = "reg_spec.pdf") -> Path:
    pdf = tmp_path / name
    pdf.write_bytes(b"%PDF-fake-for-structured-lane")
    return pdf


def _document_id(pdf: Path, root: Path) -> str:
    return figure_extract.document_id_for(pdf, root)


def _figure_id(document_id: str, page: int, bbox=TABLE_BBOX, digest: str = "asset") -> str:
    return figure_extract.figure_id_for(document_id, page, bbox, PAGE_RECT, digest)


def _candidate(document_id, figure_id, *, page=1, bbox=TABLE_BBOX,
               kind=figure_extract.KIND_TABLE, native_table=None, occurrences=None,
               index=1, native_lane=None) -> FakeCandidate:
    # `signals["native_lane"]` 是 lane 判定的唯一真相（契約 §15.1）：preflight 預算、
    # RAG 的 probe 判定、verifier 的 lane 選擇讀的都是它。預設值只是 fixture 的方便
    # 寫法（有 native_table 就走 native lane），測試要驗 word-only terminal 時明示 False。
    lane = bool(native_table) if native_lane is None else bool(native_lane)
    return FakeCandidate(
        index=index, page=page, bbox=bbox, page_rect=PAGE_RECT,
        kind_scores={kind: 1.0}, kind=kind, signals={"native_lane": lane}, reasons=[],
        signature="sig", native_table=native_table,
        occurrences=list(occurrences or [_occurrence(page, bbox)]),
        asset_xref=None, asset_digest="asset", figure_id=figure_id,
        document_id=document_id,
    )


def _evidence(payload=None, kind=None, *channels) -> dict:
    """真 producer（`figure_verify._build_evidence`）的 evidence 形狀。

    契約 §19.4：`native_verified` / `corroborated` 不得配空 evidence，而且要有**格/行級**
    佐證——空的卻宣稱可信，等於把「沒查過」寫成「查過了」，strict query 之後會直接拿它
    回答暫存器數值。fixture 也要誠實，否則等於在測一個現實中產生不出來的 FigureResult。
    """
    evidence = {"channels": list(channels or ("markdown_pos",)), "cells": {}, "lines": {},
                "unlocatable_tokens": [], "anchor_coverage": {}, "row_alignment": {},
                "line_alignment": {}, "stitch": {}}
    if isinstance(payload, dict) and kind == figure_extract.KIND_TABLE:
        for row in payload.get("rows") or []:
            for cell in row["cells"]:
                evidence["cells"][f"r{row['row_index']}{cell['column_id']}"] = {
                    "anchor": "markdown_pos", "matched": True, "raw": cell["text"]}
    elif isinstance(payload, dict) and kind == figure_extract.KIND_TERMINAL:
        for line in payload.get("lines") or []:
            evidence["lines"][str(line["line_index"])] = {
                "anchor": "markdown_pos", "matched": True, "raw": line["text"]}
    return evidence


def _result(document_id, figure_id, *, page=1, bbox=TABLE_BBOX,
            kind=figure_extract.KIND_TABLE, payload=None, occurrences=None,
            status=figure_extract.VERIF_NATIVE, variants=None,
            model_input_variant="native", figure_index=1,
            evidence=None) -> FakeFigureResult:
    payload = payload if payload is not None else _table_payload(
        ["Name", "Addr", "Bits", "Access", "Description"], GROUND_TRUTH)
    row_total = len(payload["rows"]) if kind == figure_extract.KIND_TABLE else None
    line_total = len(payload["lines"]) if kind == figure_extract.KIND_TERMINAL else None
    if variants is None:
        # native lane 沒有任何模型影像輸入 → variants 必須是 []（契約 §15.6）
        variants = [] if model_input_variant == "native" else [model_input_variant]
    return FakeFigureResult(
        figure_id=figure_id, document_id=document_id, page=page, figure_index=figure_index,
        bbox=bbox, kind=kind, revision=1, payload=payload,
        extraction_status=figure_extract.EXTRACTION_COMPLETE,
        verification_status=status,
        # T2 的不變式:flagged 狀態(needs_review/unverified/legacy_unverified)必須
        # 說得出「為什麼還不能信」,reasons 不得為空。fixture 也要誠實,否則等於在
        # 測一個現實中產生不出來的 FigureResult。
        reasons=([] if status in figure_extract.TRUSTED_VERIFICATION
                 else ["no_anchor_evidence"]),
        reason_details=([] if status in figure_extract.TRUSTED_VERIFICATION
                        else ["fixture:無獨立文字層佐證"]),
        evidence=_evidence(payload, kind) if evidence is None else evidence,
        occurrences=list(occurrences or [_occurrence(page, bbox)]),
        model_input_variant=model_input_variant, variants=list(variants),
        row_total=row_total, line_total=line_total,
    )


def _plan(document_id, candidates, page_evidence, *, preflight=None) -> FakePlan:
    return FakePlan(
        document_id=document_id, candidates=list(candidates),
        page_evidence=dict(page_evidence), stats={},
        preflight=dict(preflight or {"candidates": len(candidates), "tiles": 0,
                                     "vl_calls_min": 0, "vl_calls_max": 0,
                                     "image_tokens_est": 0, "pages": 1,
                                     "native_tables": 1}),
        over_budget=[],
    )


class _Spy:
    """記錄呼叫次數與參數的萬用替身。"""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self.result = result
        self.raises = raises

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.result


class _Harness:
    """一次裝好 figure 門面的替身，並保留每個替身供斷言。"""

    def __init__(self, monkeypatch, tmp_path: Path):
        self.monkeypatch = monkeypatch
        self.tmp_path = tmp_path
        self.rendered_for = []
        self.plan_spy = None
        self.check_preflight = _Spy()
        self.ensure_capability = _Spy()
        self.extract = _Spy(result=[])
        self.write_artifacts = _Spy()
        self.prune = _Spy()
        self.report = _Spy(result="[PREFLIGHT] fake report")

    def _set(self, name, value):
        self.monkeypatch.setattr(figure_extract, name, value, raising=False)

    def install(self, plan, results, *, variants=None):
        variants = variants if variants is not None else []
        self.plan_spy = _Spy(result=plan)

        def _render(_doc, candidate):
            self.rendered_for.append(candidate.figure_id)
            mine = [v for v in variants if v.figure_id == candidate.figure_id]
            if mine:
                return mine
            # 每張進 KB 的 figure 都必須留得下覆核用影像（Go/No-Go 5），
            # 所以 fixture 的預設 renderer 一定給得出一張。bbox 取自候選框：
            # 真 renderer 就是照候選框取像，覆核圖也是照這個框判「完整」的。
            return [FakeVariant(figure_id=candidate.figure_id,
                                variant_id="crop@200dpi",
                                bbox=tuple(candidate.bbox))]

        def _write(root, **kwargs):
            # 契約 §15.6：實際模型輸入（variants）與覆核用影像（review_assets）分開
            assert "review_assets" in kwargs, "write_run_artifacts 少了 review_assets"
            self.write_artifacts.calls.append(((root,), kwargs))
            manifest = (Path(root) / ".codetrail" / "figures" / "slug"
                        / kwargs["run_id"] / "manifest.json")
            # locator 要指到真的存在的檔（懸空的 evidence_ref 是覆核死路）
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("{}", encoding="utf-8")
            return manifest

        self._set("plan_document_figures", self.plan_spy)
        self._set("check_preflight", self.check_preflight)
        self._set("format_preflight_report", self.report)
        self._set("ensure_capability", self.ensure_capability)
        self._set("render_candidate_variants", _render)
        self._set("new_run_id", lambda *a, **k: "20260822-120000-abcdef01")
        self._set("evidence_ref_for",
                  lambda document_id, run_id: f".codetrail/figures/slug/{run_id}/manifest.json")
        self._set("write_run_artifacts", _write)
        self._set("prune_old_runs", self.prune)
        if isinstance(results, BaseException):
            self.extract = _Spy(raises=results)
        else:
            self.extract = _Spy(result=list(results))
        outer = self.extract

        def _extract(plan_arg, **kwargs):
            outer.calls.append(((plan_arg,), kwargs))
            if outer.raises is not None:
                raise outer.raises
            # T4 的 VL lane 一定先呼叫 render_variants 才送模型；native lane 零 VL、
            # 完全不 render。替身照做，`rendered`（實際模型輸入）才會是真的。
            render = kwargs.get("render_variants")
            for candidate in plan_arg.candidates:
                if render is not None and not candidate.signals.get("native_lane"):
                    render(kwargs.get("pdf_doc"), candidate)
            return list(outer.result)

        self._set("extract_document_figures", _extract)
        self.monkeypatch.setattr(
            RAG, "_open_pdf_document",
            lambda _path: types.SimpleNamespace(page_count=99, close=lambda: None))
        return self


def _harness(monkeypatch, tmp_path, pages, plan, results, *, variants=None) -> _Harness:
    monkeypatch.setattr(RAG, "check_pymupdf4llm",
                        lambda: types.SimpleNamespace(to_markdown=lambda *a, **k: pages))
    monkeypatch.setenv("AICODE_ROOT", str(tmp_path))
    return _Harness(monkeypatch, tmp_path).install(plan, results, variants=variants)


def _kb_ready(monkeypatch, tmp_path: Path) -> Path:
    """可寫入的 knowledge.json + 打樁的 embedding 端點。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(RAG.llama_client, "embed_one", lambda **_kw: [1.0, 0.0])
    kb_path = tmp_path / "knowledge.json"
    kb_path.write_text(json.dumps({
        "metadata": {"embedding_model": RAG.EMBEDDING_MODEL, "documents": [],
                     "total_documents": 0, "total_chunks": 0},
        "chunks": [],
    }, ensure_ascii=False), encoding="utf-8")
    return kb_path


def _dir_snapshot(path: Path) -> list:
    """整棵目錄的 (相對路徑, 大小)——零寫入要連 NPZ / embedding cache / .codetrail 都不長出來。

    唯一豁免是 KB 的 store lock(``.knowledge.json.lock``):``add_document`` 在**任何 figure
    工作之前**先驗一次 KB(「壞掉的 KB 要在付出抽取成本前就 fail」),那一步經
    ``knowledge_store_lock`` 必然建出 0 byte 的鎖檔。它不是 KB mutation,也不帶任何內容;
    把它算進零寫入斷言只會讓斷言與既有行為打架,而不是抓到真的寫入。
    """
    # 鎖檔名由 knowledge_store._lock_path 決定:`.<json 檔名>.lock`
    lock_names = {f".{item.name}.lock" for item in path.rglob("*.json")} | {".knowledge.json.lock"}
    return sorted((str(item.relative_to(path)), item.stat().st_size if item.is_file() else -1)
                  for item in path.rglob("*")
                  if item.name not in lock_names)


def _kb_chunks(kb_path: Path) -> list:
    return json.loads(kb_path.read_text(encoding="utf-8"))["chunks"]


def _assert_row_identity(contents, ground_truth):
    """逐列驗完整 row identity，並排除**另一列的每一個** critical field。

    只驗「這一列有沒有出現某個值」抓不到交叉錯配:CTRL1 那列同時含自己的位址與
    CTRL0 的位址一樣會過。要驗的是「這一列只含自己的值」。
    """
    body = "\n".join(contents)
    for index, cells in enumerate(ground_truth):
        name = cells[0]
        rows = [line for line in body.split("\n") if name in line]
        assert len(rows) == 1, f"{name} 應該只在一列出現，實際 {rows}"
        row = rows[0]
        for value in cells[1:]:
            assert value in row, f"{name} 那一列少了 {value!r}: {row!r}"
        for other_index, other in enumerate(ground_truth):
            if other_index == index:
                continue
            for value in other[1:]:
                if value in cells[1:]:
                    continue  # 兩列本來就相同的欄位不算交叉
                assert value not in row, (
                    f"{name} 那一列混進了 {other[0]} 的 {value!r}: {row!r}")


def _occurrences_in(chunks, needle: str) -> int:
    return sum(chunk["content"].count(needle) for chunk in chunks)


def _simple_native_case(tmp_path: Path, monkeypatch):
    """一頁 = 前言 + 原生 markdown 表 + 後記，表格由 structured chunk 收錄。"""
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1)
    pages = [_page(1, PAGE1, [_table_box(POS1)])]
    candidate = _candidate(
        document_id, figure_id,
        native_table={"pos": POS1, "markdown": TABLE_MD, "geometry": {}, "strategy": "lines"})
    evidence = {1: FakePageEvidence(page=1, raw_markdown=PAGE1, page_boxes=pages[0]["page_boxes"])}
    plan = _plan(document_id, [candidate], evidence)
    result = _result(document_id, figure_id)
    harness = _harness(monkeypatch, tmp_path, pages, plan, [result])
    return pdf, harness, figure_id


# ============================================================
# 1. 原 page markdown 表被取代，且 KB 內不重複
# ============================================================
@pytest.mark.smoke
def test_native_table_substring_is_replaced_and_kb_has_no_duplicate_row(
    tmp_path: Path, monkeypatch
):
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf, _harness_obj, figure_id = _simple_native_case(tmp_path, monkeypatch)

    RAG.add_document(str(pdf), str(kb_path))

    chunks = _kb_chunks(kb_path)
    text_chunks = [c for c in chunks if not c.get("structured")]
    structured = [c for c in chunks if c.get("structured")]

    assert structured, "原生表格應產出 structured chunk"
    assert all(c["origin"] == "figure_table" for c in structured)
    assert all(c["source"] == "reg_spec.pdf" for c in structured)

    marker = f"[表格已改以結構化 chunk 收錄：figure={figure_id} page=1 rows=2]"
    assert any(marker in c["content"] for c in text_chunks), (
        f"原表格位置要留下單行 marker，實際: {[c['content'] for c in text_chunks]}")

    # 同一列資料在整個 KB 只能出現一次（BM25 不得重複計數）
    for token in ("0x4000_0100", "0x4000_0104", "clock select"):
        assert _occurrences_in(chunks, token) == 1, (
            f"{token} 在 KB 出現 {_occurrences_in(chunks, token)} 次——"
            "原 markdown 表與 structured chunk 兩份並存")
    assert _occurrences_in(text_chunks, "0x4000_0100") == 0, "原 markdown 表沒有被切掉"

    # 表格前後的正文必須完整存活
    joined = "\n".join(c["content"] for c in text_chunks)
    assert "Register overview paragraph for the control block." in joined
    assert "Trailing paragraph after the table." in joined


@pytest.mark.smoke
def test_native_table_row_identity_survives_into_the_kb(tmp_path: Path, monkeypatch):
    """五欄 ground truth:每一欄都要跟同一個 row identity 綁在一起（workflow §5 table ①②）。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf, _h, _fid = _simple_native_case(tmp_path, monkeypatch)

    RAG.add_document(str(pdf), str(kb_path))

    structured = [c for c in _kb_chunks(kb_path) if c.get("structured")]
    _assert_row_identity([c["content"] for c in structured], GROUND_TRUTH)


@pytest.mark.smoke
def test_visual_lane_structured_chunk_lands_in_kb(tmp_path: Path, monkeypatch):
    """VLM lane（`native_table=None`）:沒有 markdown 要取代，chunk 一樣要進 KB。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1, digest="raster")
    text = "Boot log discussion paragraph that stays in the page markdown. "
    pages = [_page(1, text, [{"index": 0, "class": "text", "bbox": (60, 60, 520, 80),
                              "pos": (0, len(text))}])]
    candidate = _candidate(document_id, figure_id, kind=figure_extract.KIND_TERMINAL,
                           native_table=None)
    evidence = {1: FakePageEvidence(page=1, raw_markdown=text, page_boxes=pages[0]["page_boxes"])}
    plan = _plan(document_id, [candidate], evidence,
                 preflight={"candidates": 1, "tiles": 1, "vl_calls_min": 1,
                            "vl_calls_max": 1, "image_tokens_est": 64, "pages": 1,
                            "native_tables": 0})
    payload = _terminal_payload(["", "  $ dmesg | tail -1  ", "[ 0.00] boot: OK", ""])
    result = _result(document_id, figure_id, kind=figure_extract.KIND_TERMINAL,
                     payload=payload, status=figure_extract.VERIF_UNVERIFIED,
                     variants=["crop@200dpi"], model_input_variant="crop@200dpi")
    variants = [FakeVariant(figure_id=figure_id, variant_id="crop@200dpi")]
    harness = _harness(monkeypatch, tmp_path, pages, plan, [result], variants=variants)

    RAG.add_document(str(pdf), str(kb_path))

    chunks = _kb_chunks(kb_path)
    structured = [c for c in chunks if c.get("structured")]
    assert structured and all(c["origin"] == "figure_terminal" for c in structured)
    assert harness.ensure_capability.calls, "visual lane 必須先過 capability probe"
    assert _occurrences_in(chunks, "boot: OK") == 1
    # 原本頁面的文字沒有被動到
    assert any("Boot log discussion paragraph" in c["content"]
               for c in chunks if not c.get("structured"))


# ============================================================
# 2. pos 不可信 → 保留原表、不產 structured chunk、無重複
# ============================================================
def _pos_case(tmp_path, monkeypatch, *, box_pos, native_pos=None, markdown=TABLE_MD,
              raw_markdown=None):
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1)
    boxes = [_table_box(box_pos)] if box_pos is not None else []
    pages = [_page(1, PAGE1, boxes)]
    candidate = _candidate(
        document_id, figure_id,
        native_table={"pos": native_pos if native_pos is not None else box_pos,
                      "markdown": markdown, "geometry": {}, "strategy": "lines"})
    evidence = {1: FakePageEvidence(
        page=1, raw_markdown=PAGE1 if raw_markdown is None else raw_markdown,
        page_boxes=boxes)}
    plan = _plan(document_id, [candidate], evidence)
    harness = _harness(monkeypatch, tmp_path, pages, plan, [_result(document_id, figure_id)])
    return pdf, harness


@pytest.mark.smoke
@pytest.mark.parametrize("label,kwargs,needle", [
    ("缺 pos", {"box_pos": None, "native_pos": None}, "缺 pos"),
    ("越界", {"box_pos": (5, 99999)}, "越界"),
    ("反向區間", {"box_pos": (200, 100)}, "越界"),
    ("float pos", {"box_pos": (1.0, 2.0)}, "缺 pos"),
    ("空白區間", {"box_pos": (len(INTRO) - 2, len(INTRO))}, "空白區間"),
    ("指到別的正文", {"box_pos": (0, len(INTRO))}, "native markdown"),
    ("區間多吃正文", {"box_pos": (len(INTRO), len(PAGE1))}, "多出其他正文"),
    ("plan 與 pages 不一致", {"box_pos": POS1, "raw_markdown": PAGE1 + "drift"},
     "raw text 不一致"),
])
def test_unsafe_pos_keeps_the_original_table(tmp_path: Path, monkeypatch, capsys,
                                             label, kwargs, needle):
    """六種 `pos` 問題一律:退回原 markdown、不產 structured chunk、印 [WARN]、不重複。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf, _h = _pos_case(tmp_path, monkeypatch, **kwargs)

    RAG.add_document(str(pdf), str(kb_path))

    out = capsys.readouterr().out
    chunks = _kb_chunks(kb_path)
    assert not [c for c in chunks if c.get("structured")], f"{label}: 不得產出 structured chunk"
    assert _occurrences_in(chunks, "0x4000_0100") == 1, f"{label}: 原表格必須完整保留一份"
    assert "[WARN]" in out and "保留原表格文字（不重複入庫）" in out, out
    assert needle in out, f"{label}: 訊息少了 {needle!r}\n{out}"
    assert "[表格已改以結構化 chunk 收錄" not in "\n".join(c["content"] for c in chunks)
    # 降級原因要進 manifest（覆核的人才知道那張表為什麼沒有 structured chunk）
    (_args, written) = _h.write_artifacts.calls[-1]
    assert written["failed"] is False
    dropped = written["figures"]
    assert len(dropped) == 1 and "no_pos_cannot_replace" in dropped[0].reasons
    assert any(needle in detail for detail in dropped[0].reason_details), (
        f"{label}: reason_details 沒寫清楚原因 {dropped[0].reason_details}")


# ============================================================
# 3. 多表 offset / 重疊
# ============================================================
@pytest.mark.smoke
def test_two_tables_on_one_page_keep_correct_offsets(tmp_path: Path, monkeypatch):
    """同頁兩張表:第二張的 pos 是相對原始文字的座標，不得被第一張的長度差位移。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)

    head = "SECTION A intro paragraph. \n\n"
    table_a = "|Reg|Addr|Bits|\n|---|---|---|\n|AAA0|0xAAAA_0000|[1:0]|\n"
    middle = "\n\nMIDDLE paragraph between the two tables. \n\n"
    table_b = "|Reg|Addr|Bits|\n|---|---|---|\n|BBB0|0xBBBB_0000|[3:2]|\n"
    foot = "\n\nFOOT paragraph after both tables. \n\n"
    raw = head + table_a + middle + table_b + foot
    pos_a = (len(head), len(head) + len(table_a))
    start_b = len(head) + len(table_a) + len(middle)
    pos_b = (start_b, start_b + len(table_b))

    bbox_a = (60.0, 100.0, 520.0, 150.0)
    bbox_b = (60.0, 300.0, 520.0, 350.0)
    fid_a = _figure_id(document_id, 1, bbox_a, "a")
    fid_b = _figure_id(document_id, 1, bbox_b, "b")
    boxes = [_table_box(pos_a, bbox_a, 1), _table_box(pos_b, bbox_b, 2)]
    pages = [_page(1, raw, boxes)]
    candidates = [
        _candidate(document_id, fid_a, bbox=bbox_a, index=1,
                   native_table={"pos": pos_a, "markdown": table_a, "geometry": {},
                                 "strategy": "lines"},
                   occurrences=[_occurrence(1, bbox_a)]),
        _candidate(document_id, fid_b, bbox=bbox_b, index=2,
                   native_table={"pos": pos_b, "markdown": table_b, "geometry": {},
                                 "strategy": "lines"},
                   occurrences=[_occurrence(1, bbox_b)]),
    ]
    evidence = {1: FakePageEvidence(page=1, raw_markdown=raw, page_boxes=boxes)}
    plan = _plan(document_id, candidates, evidence)
    results = [
        _result(document_id, fid_a, bbox=bbox_a, occurrences=[_occurrence(1, bbox_a)],
                payload=_table_payload(["Reg", "Addr", "Bits"],
                                       [["AAA0", "0xAAAA_0000", "[1:0]"]]), figure_index=1),
        _result(document_id, fid_b, bbox=bbox_b, occurrences=[_occurrence(1, bbox_b)],
                payload=_table_payload(["Reg", "Addr", "Bits"],
                                       [["BBB0", "0xBBBB_0000", "[3:2]"]]), figure_index=2),
    ]
    _harness(monkeypatch, tmp_path, pages, plan, results)

    RAG.add_document(str(pdf), str(kb_path))

    chunks = _kb_chunks(kb_path)
    text = "\n".join(c["content"] for c in chunks if not c.get("structured"))
    assert f"figure={fid_a} page=1 rows=1" in text
    assert f"figure={fid_b} page=1 rows=1" in text
    for token in ("0xAAAA_0000", "0xBBBB_0000"):
        assert _occurrences_in(chunks, token) == 1, f"{token} 重複或消失"
    # 三段正文一個字都不能少（第二張表的 offset 被位移的話會吃掉 MIDDLE 或 FOOT）
    for paragraph in ("SECTION A intro paragraph.", "MIDDLE paragraph between the two tables.",
                      "FOOT paragraph after both tables."):
        assert paragraph in text, f"正文遺失: {paragraph}\n{text}"


@pytest.mark.smoke
def test_nested_overlapping_pos_disqualifies_every_member(tmp_path: Path, monkeypatch, capsys):
    """A=[0,100)、B=[10,20)、C=[30,40):B 與 C 不相鄰，但都被 A 蓋住 → 三張全部失格。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)

    raw = TABLE_MD + "\n\ntail text after the tables. \n"
    outer = (0, len(TABLE_MD))
    inner_a = (0, len("|Name|Addr|Bits|Access|Description|\n"))
    inner_b = (len("|Name|Addr|Bits|Access|Description|\n|---|---|---|---|---|\n"),
               len(TABLE_MD))
    specs = [("outer", outer, (60.0, 100.0, 520.0, 180.0), TABLE_MD),
             ("inner_a", inner_a, (60.0, 100.0, 520.0, 120.0), raw[inner_a[0]:inner_a[1]]),
             ("inner_b", inner_b, (60.0, 150.0, 520.0, 180.0), raw[inner_b[0]:inner_b[1]])]
    boxes, candidates, results = [], [], []
    for i, (tag, pos, bbox, markdown) in enumerate(specs, 1):
        fid = _figure_id(document_id, 1, bbox, tag)
        boxes.append(_table_box(pos, bbox, i))
        candidates.append(_candidate(
            document_id, fid, bbox=bbox, index=i,
            native_table={"pos": pos, "markdown": markdown, "geometry": {}, "strategy": "lines"},
            occurrences=[_occurrence(1, bbox)]))
        results.append(_result(document_id, fid, bbox=bbox,
                               occurrences=[_occurrence(1, bbox)], figure_index=i))
    pages = [_page(1, raw, boxes)]
    evidence = {1: FakePageEvidence(page=1, raw_markdown=raw, page_boxes=boxes)}
    _harness(monkeypatch, tmp_path, pages, _plan(document_id, candidates, evidence), results)

    RAG.add_document(str(pdf), str(kb_path))

    out = capsys.readouterr().out
    chunks = _kb_chunks(kb_path)
    assert not [c for c in chunks if c.get("structured")], "重疊叢集必須整組失格"
    assert out.count("pos 與同頁另一個區塊重疊") == 3, out
    assert _occurrences_in(chunks, "0x4000_0100") == 1, "原 markdown 表要完整留一份"


@pytest.mark.smoke
def test_occurrence_without_its_own_candidate_disqualifies_the_table(
    tmp_path: Path, monkeypatch, capsys
):
    """同一張表出現在兩頁，但只有一頁有候選 → 整張退回原 markdown。

    T3 的候選是 physical 的（每個實體位置一個候選），所以正常情況兩頁各有自己的
    候選。少一個就代表:換掉第 1 頁、第 2 頁的 markdown 卻留著 = 同一張表兩種形式。
    """
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1)
    page2 = "Second page repeats the same table. \n\n" + TABLE_MD
    boxes1 = [_table_box(POS1)]
    pages = [_page(1, PAGE1, boxes1), _page(2, page2, [])]
    occurrences = [_occurrence(1), _occurrence(2, index=1)]  # 第 2 頁沒有自己的候選
    candidate = _candidate(
        document_id, figure_id, occurrences=occurrences,
        native_table={"pos": POS1, "markdown": TABLE_MD, "geometry": {}, "strategy": "lines"})
    evidence = {
        1: FakePageEvidence(page=1, raw_markdown=PAGE1, page_boxes=boxes1),
        2: FakePageEvidence(page=2, raw_markdown=page2, page_boxes=[]),
    }
    _harness(monkeypatch, tmp_path, pages, _plan(document_id, [candidate], evidence),
             [_result(document_id, figure_id, occurrences=occurrences)])

    RAG.add_document(str(pdf), str(kb_path))

    out = capsys.readouterr().out
    chunks = _kb_chunks(kb_path)
    assert not [c for c in chunks if c.get("structured")]
    assert "沒有自己的候選" in out and "保留原表格文字（不重複入庫）" in out, out
    assert _occurrences_in(chunks, "0x4000_0100") == 2, "兩頁的原表都要原樣保留"


def test_repeated_table_replaces_every_physical_occurrence(tmp_path: Path, monkeypatch):
    """同一張表在兩頁各有自己的候選:兩頁的 markdown 都被換掉，各自產 structured chunk。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    head2 = "Second page repeats the same table. \n\n"
    page2 = head2 + TABLE_MD + "\n\nAfter it. \n\n"
    pos2 = (len(head2), len(head2) + len(TABLE_MD) + 2)
    bbox2 = (60.0, 500.0, 520.0, 580.0)
    fid1 = _figure_id(document_id, 1)
    fid2 = _figure_id(document_id, 2, bbox2)
    boxes1 = [_table_box(POS1)]
    boxes2 = [_table_box(pos2, bbox2)]
    pages = [_page(1, PAGE1, boxes1), _page(2, page2, boxes2)]
    # 兩個候選共享同一份 occurrences（T3 依 asset digest 分組）
    shared = [_occurrence(1, TABLE_BBOX), _occurrence(2, bbox2)]
    native = {"markdown": TABLE_MD, "geometry": {}, "strategy": "lines"}
    candidates = [
        _candidate(document_id, fid1, page=1, bbox=TABLE_BBOX, occurrences=shared,
                   native_table={**native, "pos": POS1}, index=1),
        _candidate(document_id, fid2, page=2, bbox=bbox2, occurrences=shared,
                   native_table={**native, "pos": pos2}, index=2),
    ]
    evidence = {1: FakePageEvidence(page=1, raw_markdown=PAGE1, page_boxes=boxes1),
                2: FakePageEvidence(page=2, raw_markdown=page2, page_boxes=boxes2)}
    results = [_result(document_id, fid1, page=1, bbox=TABLE_BBOX, occurrences=shared),
               _result(document_id, fid2, page=2, bbox=bbox2, occurrences=shared)]
    _harness(monkeypatch, tmp_path, pages, _plan(document_id, candidates, evidence), results)

    RAG.add_document(str(pdf), str(kb_path))

    chunks = _kb_chunks(kb_path)
    structured = [c for c in chunks if c.get("structured")]
    text = [c for c in chunks if not c.get("structured")]
    assert {c["page"] for c in structured} == {1, 2}
    assert _occurrences_in(text, "0x4000_0100") == 0, "兩頁的 markdown 表都要被換掉"
    # 表真的在兩頁各出現一次，所以 structured 也是兩份——與換掉之前的份數相同
    assert _occurrences_in(structured, "0x4000_0100") == 2
    assert f"figure={fid1} page=1" in "\n".join(c["content"] for c in text)
    assert f"figure={fid2} page=2" in "\n".join(c["content"] for c in text)


# ============================================================
# 4. structured chunk 不經 normalize / splitter
# ============================================================
@pytest.mark.smoke
def test_structured_chunk_content_bypasses_normalize(tmp_path: Path, monkeypatch):
    """兩欄表不得被改成 `Key: Value`;terminal 的空行與行首行尾空白逐位元組保留。"""
    document_id = "docs/x.pdf::0123456789abcdef"
    fid_table = _figure_id(document_id, 1, TABLE_BBOX, "t")
    fid_term = _figure_id(document_id, 1, (60.0, 300.0, 520.0, 400.0), "term")
    table = _result(document_id, fid_table,
                    payload=_table_payload(["Signal", "Value"], [["ON", "0x1"], ["OFF", "0x0"]]),
                    figure_index=1)
    terminal = _result(document_id, fid_term, bbox=(60.0, 300.0, 520.0, 400.0),
                       kind=figure_extract.KIND_TERMINAL,
                       payload=_terminal_payload(
                           ["", "  indented line  ", "| ERROR | disk full |", ""]),
                       occurrences=[_occurrence(1, (60.0, 300.0, 520.0, 400.0), 1)],
                       status=figure_extract.VERIF_UNVERIFIED, figure_index=2)

    chunks = RAG.build_structured_figure_document(
        [table, terminal], source="x.pdf", doc_type="spec", next_chunk_index={},
        evidence_ref_by_figure={fid_table: ".codetrail/figures/s/r/manifest.json",
                                fid_term: ".codetrail/figures/s/r/manifest.json"})

    table_chunk = next(c for c in chunks if c["figure_kind"] == figure_extract.KIND_TABLE)
    content = table_chunk["content"]
    assert RAG.normalize_table_content(content) != content, (
        "fixture 沒有踩到 normalize 會改寫的形狀，這條測試證明不了 bypass")
    assert "ON: 0x1" not in content and "OFF: 0x0" not in content, content
    assert "| ON |" in content and "| 0x1 |" in content, content

    term_chunk = next(c for c in chunks if c["figure_kind"] == figure_extract.KIND_TERMINAL)
    body = term_chunk["content"]
    assert "\n  indented line  \n" in body, repr(body)
    assert RAG.normalize_table_content(body) != body, (
        "terminal fixture 沒有踩到 normalize 會改寫的形狀，證明不了 bypass")
    assert "ERROR: disk full" not in body, "log 正文被 normalize 改成 Key: Value"
    assert "| ERROR | disk full |" in body, repr(body)


@pytest.mark.smoke
def test_build_structured_figure_document_is_a_pure_passthrough(tmp_path: Path, monkeypatch):
    """薄封裝:輸出與門面逐鍵相同，且完全沒有碰通用 normalize / splitter。"""
    document_id = "docs/x.pdf::0123456789abcdef"
    fid = _figure_id(document_id, 1)
    figure = _result(document_id, fid)
    kwargs = dict(source="x.pdf", doc_type="spec",
                  evidence_ref_by_figure={fid: ".codetrail/figures/s/r/manifest.json"})

    calls = []
    for name in ("normalize_document_text", "normalize_table_content",
                 "split_by_semantic_with_sections", "split_by_semantic",
                 "detect_content_type"):
        original = getattr(RAG, name)

        def _spy(*args, _name=name, _original=original, **kw):
            calls.append(_name)
            return _original(*args, **kw)

        monkeypatch.setattr(RAG, name, _spy)

    index_a, index_b = {2: 5}, {2: 5}
    via = RAG.build_structured_figure_document([figure], next_chunk_index=index_a, **kwargs)
    direct = figure_extract.build_figure_chunks([figure], next_chunk_index=index_b, **kwargs)

    assert via == direct, "build_structured_figure_document 不是純轉發"
    assert index_a == index_b, "next_chunk_index 的就地更新語意不同"
    assert calls == [], f"structured 路徑碰到了通用文字處理: {calls}"


# ============================================================
# 5. 零寫入：抽取失敗 / 預算超限 / preflight_only
# ============================================================
@pytest.mark.smoke
def test_extraction_failure_leaves_knowledge_json_byte_identical(tmp_path: Path, monkeypatch):
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1)
    pages = [_page(1, PAGE1, [_table_box(POS1)])]
    candidate = _candidate(
        document_id, figure_id,
        native_table={"pos": POS1, "markdown": TABLE_MD, "geometry": {}, "strategy": "lines"})
    evidence = {1: FakePageEvidence(page=1, raw_markdown=PAGE1, page_boxes=pages[0]["page_boxes"])}
    boom = figure_extract.FigureExtractionError("row width 不符 (stub)")
    boom.results = []
    # T4 的 `.failed` 是單一 FigureResult（不是 list）——兩種形狀都要吃得下去
    boom.failed = dataclasses.replace(
        _result(document_id, figure_id), payload=None,
        extraction_status=figure_extract.EXTRACTION_FAILED,
        verification_status=figure_extract.VERIF_NEEDS_REVIEW,
        reasons=["extraction_failed"], row_total=None)
    harness = _harness(monkeypatch, tmp_path, pages, _plan(document_id, [candidate], evidence), boom)
    embed_spy = _Spy(result=[])
    monkeypatch.setattr(RAG, "generate_embeddings", embed_spy)

    with pytest.raises(figure_extract.FigureExtractionError, match="row width"):
        RAG.add_document(str(pdf), str(kb_path))

    assert kb_path.read_bytes() == before, "抽取失敗後 knowledge.json 必須零寫入"
    assert not (tmp_path / RAG.KNOWLEDGE_EMB_FILE).exists(), "外掛向量也不得被寫出"
    assert embed_spy.calls == [], "抽取失敗不得先算 embedding"
    assert harness.write_artifacts.calls, "失敗也要留 review artifact"
    (_args, kwargs) = harness.write_artifacts.calls[-1]
    assert kwargs["failed"] is True
    assert [f.figure_id for f in kwargs["figures"]] == [figure_id], (
        "失敗 artifact 要帶 per-figure 資訊（exc.results + exc.failed）")
    assert kwargs["figures"][0].extraction_status == figure_extract.EXTRACTION_FAILED
    assert kwargs["preflight"] and kwargs["stats"] is not None
    assert kwargs["source_signatures"][figure_id]["asset_digest"] == "asset", (
        "沒帶 source_signatures 的話，之後的人工確認永遠沿用不了")


@pytest.mark.smoke
def test_preflight_over_budget_makes_zero_vl_zero_embedding_zero_write(
    tmp_path: Path, monkeypatch, capsys
):
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)
    tree = _dir_snapshot(tmp_path)
    harness.check_preflight = _Spy(
        raises=figure_extract.FigureBudgetError("candidates 300 > 200 (stub)"))
    monkeypatch.setattr(figure_extract, "check_preflight", harness.check_preflight,
                        raising=False)
    embed_spy = _Spy(result=[])
    monkeypatch.setattr(RAG, "generate_embeddings", embed_spy)
    save_spy = _Spy()
    monkeypatch.setattr(RAG, "save_knowledge_base", save_spy)
    vl_spy = _Spy(result="desc")
    monkeypatch.setattr(RAG, "_describe_technical_image_base64", vl_spy)

    with pytest.raises(figure_extract.FigureBudgetError) as exc:
        RAG.add_document(str(pdf), str(kb_path))

    out = capsys.readouterr().out
    assert harness.ensure_capability.calls == [], "超出預算不得做 capability probe"
    assert harness.extract.calls == [], "超出預算不得呼叫抽取"
    assert vl_spy.calls == [] and embed_spy.calls == [] and save_spy.calls == []
    assert kb_path.read_bytes() == before
    assert "python3 RAG.py" in str(exc.value) and "--preflight" in str(exc.value)
    assert "python3 RAG.py" in out and "--preflight" in out
    assert "[PREFLIGHT] fake report" in out, "超出預算也要把完整報告印出來"
    assert "既有圖面路徑" in out, "報告要含 legacy 曝險估算"
    assert _dir_snapshot(tmp_path) == tree, "超出預算不得長出 NPZ / cache / lock 檔"


@pytest.mark.smoke
def test_preflight_only_returns_empty_document_and_writes_nothing(
    tmp_path: Path, monkeypatch, capsys
):
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)
    tree = _dir_snapshot(tmp_path)

    document = RAG.extract_pdf_document(str(pdf), preflight_only=True, root=str(tmp_path))

    out = capsys.readouterr().out
    assert document.chunks == [] and document.raw_text == ""
    assert "[PREFLIGHT] fake report" in out
    assert harness.ensure_capability.calls == [] and harness.extract.calls == []

    assert RAG.main([str(pdf), str(kb_path), "--preflight"]) == 0
    assert kb_path.read_bytes() == before, "--preflight 必須零寫入"
    assert not (tmp_path / RAG.KNOWLEDGE_EMB_FILE).exists()
    assert _dir_snapshot(tmp_path) == tree, (
        "--preflight 不得長出 NPZ / embedding cache / store lock / .codetrail")


@pytest.mark.smoke
def test_preflight_reports_failure_instead_of_faking_success(tmp_path: Path, monkeypatch,
                                                            capsys):
    """PDF 解析失敗 / lane 不啟動時，`--preflight` 必須是 exit 1，不是沒有報告的 exit 0。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf = _write_pdf(tmp_path)
    monkeypatch.setenv("AICODE_ROOT", str(tmp_path))

    def _boom(*_a, **_k):
        raise RuntimeError("broken pdf (stub)")

    monkeypatch.setattr(RAG, "check_pymupdf4llm",
                        lambda: types.SimpleNamespace(to_markdown=_boom))
    assert RAG.main([str(pdf), str(kb_path), "--preflight"]) == 1
    out = capsys.readouterr().out
    assert "未寫入知識庫（零寫入）" not in out, f"產不出報告卻宣告成功:\n{out}"
    assert "[ERROR]" in out and "產不出 figure preflight 報告" in out, out

    # lane 不啟動（檔案不在 root 內）同樣不得假成功
    outside = tmp_path.parent / "outside_root"
    outside.mkdir(exist_ok=True)
    other = _write_pdf(outside, "other.pdf")
    monkeypatch.setattr(RAG, "check_pymupdf4llm",
                        lambda: types.SimpleNamespace(to_markdown=lambda *a, **k: [_page(1, PAGE1)]))
    assert RAG.main([str(other), str(kb_path), "--preflight"]) == 1


# ============================================================
# 6. candidate ↔ result 的一一對應
# ============================================================
@pytest.mark.smoke
@pytest.mark.parametrize("mutation,needle", [
    ("missing", "漏回"),
    ("extra", "多回"),
    ("document", "身分錯配"),
    ("page", "page 與候選"),
    ("bbox", "bbox"),
    # 本輪 occurrence 比對由「頁碼集合」升級成保序保 multiplicity 的完整簽章,
    # 訊息跟著改;斷言字串同步(行為未變,仍是 hard fail)
    ("occurrences", "occurrence 身分與候選不同"),
    ("failed", "extraction_status"),
])
def test_result_set_mismatch_hard_fails(tmp_path: Path, monkeypatch, mutation, needle):
    """extractor 的輸出與候選對不上 → 整份零寫入，**不得**降級成保留原表。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1)
    pages = [_page(1, PAGE1, [_table_box(POS1)])]
    candidate = _candidate(
        document_id, figure_id,
        native_table={"pos": POS1, "markdown": TABLE_MD, "geometry": {}, "strategy": "lines"})
    evidence = {1: FakePageEvidence(page=1, raw_markdown=PAGE1, page_boxes=pages[0]["page_boxes"])}
    good = _result(document_id, figure_id)
    if mutation == "missing":
        results = []
    elif mutation == "extra":
        results = [good, dataclasses.replace(
            good, figure_id=_figure_id(document_id, 1, TABLE_BBOX, "ghost"), figure_index=2)]
    elif mutation == "document":
        results = [dataclasses.replace(good, document_id="other.pdf::ffffffffffffffff")]
    elif mutation == "page":
        results = [dataclasses.replace(good, page=2, occurrences=[_occurrence(2)])]
    elif mutation == "bbox":
        results = [dataclasses.replace(good, bbox=(1.0, 2.0, 3.0, 4.0))]
    elif mutation == "occurrences":
        results = [dataclasses.replace(good, occurrences=[_occurrence(1), _occurrence(9)])]
    else:
        results = [dataclasses.replace(
            good, extraction_status=figure_extract.EXTRACTION_FAILED, payload=None)]
    _harness(monkeypatch, tmp_path, pages, _plan(document_id, [candidate], evidence), results)

    with pytest.raises(figure_extract.FigureExtractionError) as exc:
        RAG.add_document(str(pdf), str(kb_path))

    assert needle in str(exc.value), str(exc.value)
    assert kb_path.read_bytes() == before


@pytest.mark.smoke
def test_root_mismatch_fails_before_any_plan_or_vl(tmp_path: Path, monkeypatch):
    """明示 root 與 AICODE_ROOT 不一致 → 在 plan / probe / VL 之前就停。"""
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)
    other = tmp_path.parent / "another_root"
    other.mkdir(exist_ok=True)

    with pytest.raises(figure_extract.FigureReviewError, match="AICODE_ROOT"):
        RAG.extract_pdf_document(str(pdf), root=str(other))

    assert harness.plan_spy.calls == [], "root 不一致時不得先規劃候選"
    assert harness.ensure_capability.calls == [] and harness.extract.calls == []


@pytest.mark.smoke
def test_claimed_variant_that_was_never_rendered_hard_fails(tmp_path: Path, monkeypatch):
    """FigureResult 宣稱送過的 variant 必須真的被 renderer 產出（artifact 是覆核唯一依據）。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1, digest="raster")
    text = "Some page text only. "
    pages = [_page(1, text, [])]
    candidate = _candidate(document_id, figure_id, kind=figure_extract.KIND_TERMINAL)
    evidence = {1: FakePageEvidence(page=1, raw_markdown=text, page_boxes=[])}
    plan = _plan(document_id, [candidate], evidence,
                 preflight={"candidates": 1, "tiles": 1, "vl_calls_min": 1, "vl_calls_max": 1,
                            "image_tokens_est": 64, "pages": 1, "native_tables": 0})
    result = _result(document_id, figure_id, kind=figure_extract.KIND_TERMINAL,
                     payload=_terminal_payload(["log line"]),
                     status=figure_extract.VERIF_UNVERIFIED,
                     variants=["crop@600dpi"], model_input_variant="crop@600dpi")
    _harness(monkeypatch, tmp_path, pages, plan, [result], variants=[])

    with pytest.raises(figure_extract.FigureExtractionError, match="從未產出"):
        RAG.add_document(str(pdf), str(kb_path))

    assert kb_path.read_bytes() == before


# ============================================================
# 7. lane 邊界與 CLI（非 smoke）
# ============================================================
def test_capability_probe_skipped_when_only_native_tables(tmp_path: Path, monkeypatch):
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)

    RAG.add_document(str(pdf), str(kb_path))

    assert harness.ensure_capability.calls == [], "native lane 永遠不呼叫 VL（契約 §12.1）"


def test_diagram_candidate_with_native_table_stays_in_legacy_lane(tmp_path: Path, monkeypatch):
    """`kind=KIND_DIAGRAM` 一律不進 structured lane，即使它帶著 native_table。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1)
    pages = [_page(1, PAGE1, [_table_box(POS1)])]
    candidate = _candidate(
        document_id, figure_id, kind=figure_extract.KIND_DIAGRAM,
        native_table={"pos": POS1, "markdown": TABLE_MD, "geometry": {}, "strategy": "lines"})
    evidence = {1: FakePageEvidence(page=1, raw_markdown=PAGE1, page_boxes=pages[0]["page_boxes"])}
    harness = _harness(monkeypatch, tmp_path, pages,
                       _plan(document_id, [candidate], evidence), [])

    RAG.add_document(str(pdf), str(kb_path))

    chunks = _kb_chunks(kb_path)
    assert harness.extract.calls == [], "diagram 候選不得進 structured 抽取"
    assert not [c for c in chunks if c.get("structured")]
    assert _occurrences_in(chunks, "0x4000_0100") == 1, "原 markdown 表保持原樣"


@pytest.mark.smoke
def test_retained_raw_table_suppresses_the_overlapping_legacy_crop(tmp_path: Path, monkeypatch):
    """`pos` 不可信而保留原 markdown 時，重疊的 picture 框也要壓掉。

    否則文字層留一份表、legacy VL 再產一份自由文字描述 = 兩個互相競爭的版本。
    """
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1)
    boxes = [_table_box(None), {"class": "picture", "bbox": TABLE_BBOX}]
    pages = [_page(1, PAGE1, boxes)]
    candidate = _candidate(
        document_id, figure_id,
        native_table={"pos": None, "markdown": TABLE_MD, "geometry": {}, "strategy": "lines"})
    evidence = {1: FakePageEvidence(page=1, raw_markdown=PAGE1, page_boxes=boxes)}
    _harness(monkeypatch, tmp_path, pages, _plan(document_id, [candidate], evidence),
             [_result(document_id, figure_id)])
    vl_spy = _Spy(result="# 圖\n\n描述")
    monkeypatch.setattr(RAG, "_describe_technical_image_base64", vl_spy)
    monkeypatch.setattr(RAG, "_render_pdf_figure_png", lambda _d, _j: b"PNG")

    RAG.add_document(str(pdf), str(kb_path))

    assert vl_spy.calls == [], "保留原 markdown 的表，重疊的 legacy crop 必須跳過"
    chunks = _kb_chunks(kb_path)
    assert _occurrences_in(chunks, "0x4000_0100") == 1
    assert not [c for c in chunks if c.get("origin") == "diagram"]


def test_prune_runs_once_after_a_successful_commit(tmp_path: Path, monkeypatch):
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)

    RAG.add_document(str(pdf), str(kb_path))

    assert len(harness.prune.calls) == 1, "成功提交後要清一次舊 run"
    (args, kwargs) = harness.prune.calls[0]
    assert Path(args[0]).resolve() == tmp_path.resolve()
    assert kwargs["document_id"] == _document_id(pdf, tmp_path)
    assert Path(kwargs["kb_path"]) == kb_path


def test_prune_still_runs_when_every_figure_was_dropped(tmp_path: Path, monkeypatch):
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf, harness = _pos_case(tmp_path, monkeypatch, box_pos=None, native_pos=None)

    RAG.add_document(str(pdf), str(kb_path))

    assert len(harness.prune.calls) == 1, "全部退回原 markdown 時仍然寫了 run，要清理"


def test_prune_failure_only_warns_after_a_successful_commit(tmp_path: Path, monkeypatch,
                                                           capsys):
    """清理舊 run 失敗不得回頭影響已提交的 KB。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)
    monkeypatch.setattr(figure_extract, "prune_old_runs",
                        _Spy(raises=figure_extract.FigureReviewError("disk gone (stub)")),
                        raising=False)

    RAG.add_document(str(pdf), str(kb_path))

    out = capsys.readouterr().out
    assert "[WARN]" in out and "KB 已成功寫入" in out, out
    assert [c for c in _kb_chunks(kb_path) if c.get("structured")], "KB 必須已經寫進去"


@pytest.mark.smoke
def test_cli_returncodes_follow_the_frozen_contract(tmp_path: Path, monkeypatch):
    """契約 §11.4:0 = 報告在預算內、2 = 超出預算、1 = 其他錯誤。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)

    before = kb_path.read_bytes()
    assert RAG.main([str(pdf), str(kb_path), "--preflight"]) == 0
    assert RAG.rebuild_cli(["--kb", str(kb_path), str(pdf), "--preflight"]) == 0
    assert kb_path.read_bytes() == before, "--preflight 不得寫 KB"

    monkeypatch.setattr(figure_extract, "check_preflight",
                        _Spy(raises=figure_extract.FigureBudgetError("over (stub)")),
                        raising=False)
    assert RAG.main([str(pdf), str(kb_path), "--preflight"]) == 2
    assert RAG.rebuild_cli(["--kb", str(kb_path), str(pdf), "--preflight"]) == 2
    assert RAG.main([str(pdf), str(kb_path)]) == 2, "一般入庫超出預算同樣是 exit 2"


def test_preflight_rejects_non_pdf_and_vl_modes(tmp_path: Path, monkeypatch):
    kb_path = _kb_ready(monkeypatch, tmp_path)
    note = tmp_path / "note.md"
    note.write_text("# hi\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        RAG.main([str(note), str(kb_path), "--preflight"])
    assert exc.value.code == 1

    assert RAG.main([str(tmp_path / "a.png"), str(kb_path), "--image", "--preflight"]) == 1


def test_no_structured_lane_when_pdf_is_outside_root(tmp_path: Path, monkeypatch, capsys):
    kb_path = _kb_ready(monkeypatch, tmp_path)
    outside = tmp_path.parent / "outside_root_2"
    outside.mkdir(exist_ok=True)
    pdf = _write_pdf(outside, "external.pdf")
    document_id = "external.pdf::0123456789abcdef"
    pages = [_page(1, PAGE1, [_table_box(POS1)])]
    harness = _harness(monkeypatch, tmp_path, pages,
                       _plan(document_id, [], {}), [])

    RAG.add_document(str(pdf), str(kb_path))

    out = capsys.readouterr().out
    assert harness.plan_spy.calls == []
    assert "[WARN]" not in out, out
    assert "結構化 figure lane 不啟動" in out
    assert _occurrences_in(_kb_chunks(kb_path), "0x4000_0100") == 1


# ============================================================
# 8. 追加契約：context_generation 不對 structured chunk 生成脈絡
# ============================================================
@pytest.mark.smoke
def test_context_generation_skips_structured_chunks():
    """兩條判定都要成立:`structured=True` 與三個 figure origin。

    漏掉的症狀是靜默的——多一輪主模型，而且會把生成文字黏在逐格忠實的表格前面。
    """
    assert figure_extract.FIGURE_ORIGINS <= context_generation._GENERATIVE_ORIGINS
    assert context_generation.is_generative_origin({"structured": True, "origin": ""})
    for origin in sorted(figure_extract.FIGURE_ORIGINS):
        assert context_generation.is_generative_origin({"origin": origin}), origin
    for origin in ("image", "screenshot", "diagram"):
        assert context_generation.is_generative_origin({"origin": origin}), origin
    assert not context_generation.is_generative_origin({"origin": "", "structured": False})


# ============================================================
# 9. 假 dataclass 與真 dataclass 的漂移守門
# ============================================================
def test_fake_dataclasses_match_the_real_contract_shapes():
    """本檔的替身欄位＝契約 §6.3/§6.4 的實質介面，漂移了要在這裡紅。"""
    figure_verify = pytest.importorskip("figure_verify", reason="T4 尚未交付")
    real = [f.name for f in dataclasses.fields(figure_verify.FigureResult)]
    fake = [f.name for f in dataclasses.fields(FakeFigureResult)]
    assert fake == real, f"FakeFigureResult 與真 FigureResult 欄位不同:{fake} vs {real}"


def test_marker_newline_guard_keeps_it_on_its_own_line():
    raw = "abc|table|def"          # 表格區間 = [3, 10)
    piece = RAG._marker_piece(raw, 3, 10, "[MARK]")
    assert piece == "\n[MARK]\n"
    assert RAG._apply_page_replacements(raw, [(3, 10, piece)]) == "abc\n[MARK]\ndef"
    # 已經在行首行尾時不多加位元組
    raw2 = "abc\n|table|\ndef"     # 表格區間 = [4, 11)
    assert RAG._marker_piece(raw2, 4, 11, "[MARK]") == "[MARK]"
    assert RAG._apply_page_replacements(
        raw2, [(4, 11, "[MARK]")]) == "abc\n[MARK]\ndef"


# ============================================================
# 10. 總審修正:lane 判定的唯一真相（契約 §15.1）
# ============================================================
def _terminal_case(tmp_path: Path, monkeypatch, *, native_lane: bool, vl_calls_max: int):
    """一頁純文字 + 一個 terminal 候選；lane 由 `signals["native_lane"]` 決定。"""
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1, digest="term")
    text = "Boot log paragraph that stays in the page markdown. "
    pages = [_page(1, text, [])]
    candidate = _candidate(document_id, figure_id, kind=figure_extract.KIND_TERMINAL,
                           native_table=None, native_lane=native_lane)
    evidence = {1: FakePageEvidence(page=1, raw_markdown=text, page_boxes=[])}
    plan = _plan(document_id, [candidate], evidence,
                 preflight={"candidates": 1, "tiles": 1, "vl_calls_min": 0,
                            "vl_calls_max": vl_calls_max, "image_tokens_est": 32,
                            "pages": 1, "native_tables": 0})
    variant_ids = [] if native_lane else ["crop@200dpi"]
    result = _result(document_id, figure_id, kind=figure_extract.KIND_TERMINAL,
                     payload=_terminal_payload(["$ boot", "ok"]),
                     status=figure_extract.VERIF_UNVERIFIED,
                     variants=list(variant_ids),
                     model_input_variant="native" if native_lane else "crop@200dpi")
    variants = ([] if native_lane
                else [FakeVariant(figure_id=figure_id, variant_id="crop@200dpi")])
    harness = _harness(monkeypatch, tmp_path, pages, plan, [result], variants=variants)
    return pdf, harness, figure_id


@pytest.mark.smoke
def test_word_only_terminal_goes_through_the_capability_probe(tmp_path: Path, monkeypatch):
    """word geometry 單獨不足以構成 terminal 的 native lane → 走 VL、要 probe。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf, harness, _fid = _terminal_case(
        tmp_path, monkeypatch, native_lane=False, vl_calls_max=1)

    RAG.add_document(str(pdf), str(kb_path))

    assert harness.ensure_capability.calls, "word-only terminal 必須先過 capability probe"
    kinds = harness.ensure_capability.calls[0][1]["kinds"]
    assert figure_extract.KIND_TERMINAL in kinds, kinds


@pytest.mark.smoke
def test_pos_backed_terminal_never_touches_the_capability_probe(tmp_path: Path, monkeypatch):
    """有 `pos` 支撐的 terminal → native lane、零 VL 預算 → probe 零呼叫（契約 §15.1）。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf, harness, _fid = _terminal_case(
        tmp_path, monkeypatch, native_lane=True, vl_calls_max=0)

    RAG.add_document(str(pdf), str(kb_path))

    assert harness.ensure_capability.calls == [], (
        "native lane 永遠不呼叫 VL，也不該要求 capability probe（契約 §12.1 / §15.1）")
    assert harness.extract.calls, "抽取本身仍要跑"
    assert [c for c in _kb_chunks(kb_path) if c.get("structured")]


@pytest.mark.smoke
def test_missing_native_lane_signal_is_fail_loud(tmp_path: Path, monkeypatch):
    """lane signal 缺失不得由呼叫端自己猜。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1)
    pages = [_page(1, PAGE1, [_table_box(POS1)])]
    candidate = dataclasses.replace(
        _candidate(document_id, figure_id,
                   native_table={"pos": POS1, "markdown": TABLE_MD, "geometry": {},
                                 "strategy": "lines"}),
        signals={})
    evidence = {1: FakePageEvidence(page=1, raw_markdown=PAGE1, page_boxes=pages[0]["page_boxes"])}
    _harness(monkeypatch, tmp_path, pages, _plan(document_id, [candidate], evidence),
             [_result(document_id, figure_id)])

    with pytest.raises(figure_extract.FigureExtractionError) as exc:
        RAG.add_document(str(pdf), str(kb_path))

    # 訊息由 figure_extract.read_native_lane 產生（契約 §17.4 保證帶 page/figure_id）
    assert figure_id in str(exc.value), str(exc.value)
    assert kb_path.read_bytes() == before


# ============================================================
# 11. 總審修正:覆核用影像不得冒充實際模型輸入（契約 §15.6）
# ============================================================
@pytest.mark.smoke
def test_review_only_render_is_not_reported_as_model_input(tmp_path: Path, monkeypatch):
    """native lane 零 VL:補 render 的覆核圖走 `review_assets`，`variants` 必須留空。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1)
    pages = [_page(1, PAGE1, [_table_box(POS1)])]
    candidate = _candidate(
        document_id, figure_id,
        native_table={"pos": POS1, "markdown": TABLE_MD, "geometry": {}, "strategy": "lines"})
    evidence = {1: FakePageEvidence(page=1, raw_markdown=PAGE1, page_boxes=pages[0]["page_boxes"])}
    crop = FakeVariant(figure_id=figure_id, variant_id="crop@200dpi")
    harness = _harness(monkeypatch, tmp_path, pages,
                       _plan(document_id, [candidate], evidence),
                       [_result(document_id, figure_id)], variants=[crop])

    RAG.add_document(str(pdf), str(kb_path))

    (_args, kwargs) = harness.write_artifacts.calls[-1]
    assert kwargs["variants"] == [], (
        "native lane 沒有送任何影像進模型，`variants` 就不得列出東西——"
        "那會讓 review.md 對「模型看過什麼」說謊")
    assert list(kwargs["review_assets"]) == [figure_id]
    assert [v.variant_id for v in kwargs["review_assets"][figure_id]] == ["crop@200dpi"]
    assert kwargs["figures"][0].variants == []
    assert kwargs["figures"][0].model_input_variant == "native"


@pytest.mark.smoke
def test_actual_model_variant_is_not_duplicated_into_review_assets(tmp_path: Path, monkeypatch):
    """真的送過模型的那份就是最好的覆核依據，不再另外 render 一張。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf, harness, figure_id = _terminal_case(
        tmp_path, monkeypatch, native_lane=False, vl_calls_max=1)

    RAG.add_document(str(pdf), str(kb_path))

    (_args, kwargs) = harness.write_artifacts.calls[-1]
    assert [v.variant_id for v in kwargs["variants"]] == ["crop@200dpi"]
    assert kwargs["review_assets"] == {}, "送過模型的 variant 不需要再補一張覆核圖"
    assert harness.rendered_for.count(figure_id) == 1, "不得重複 render"


# ============================================================
# 12. 總審修正:re-ingest 的人工確認沿用（契約 §15.7）
# ============================================================
HUMAN_ROWS = [
    ["CTRL0", "0x4000_0100", "[7:4]", "RW", "人工更正後的時脈選擇"],
    ["CTRL1", "0x4000_0104", "[3:0]", "RO", "人工更正後的狀態旗標"],
]


def _signature_for(bbox=TABLE_BBOX, page: int = 1, asset_digest: str = "asset") -> dict:
    """與 `figure_id_for` / `source_signature` 同一套正規化。"""
    width = PAGE_RECT[2] - PAGE_RECT[0]
    height = PAGE_RECT[3] - PAGE_RECT[1]
    return {"asset_digest": asset_digest, "page": page,
            "nbbox": [round(bbox[0] / width, 4) + 0.0, round(bbox[1] / height, 4) + 0.0,
                      round(bbox[2] / width, 4) + 0.0, round(bbox[3] / height, 4) + 0.0]}


class _FakeArtifactStore:
    """用 `write_run_artifacts` 收到的東西回答 `list_figures`，模擬 T5 的 writer。

    關鍵在 `human_verification`：**只有呼叫端把 `human_verifications=` 傳進去，
    下一輪才讀得到**（寫端刻意不從 `verification_status` 自行合成——那等於捏造
    「使用者看過原圖」的證據）。漏傳的話第二次 re-ingest 的 gate 就會失敗、
    revision 退回 1，而只跑一輪的測試抓不到這個洞。
    """

    def __init__(self, harness: _Harness, seed: dict | None = None):
        self.harness = harness
        self.seed = dict(seed or {})

    def _lookup(self, figure_id: str):
        for (_args, kwargs) in reversed(self.harness.write_artifacts.calls):
            for figure in kwargs.get("figures") or []:
                if figure.figure_id != figure_id:
                    continue
                return {
                    "payload": copy.deepcopy(figure.payload),
                    "kind": figure.kind,
                    "human_verification": copy.deepcopy(
                        (kwargs.get("human_verifications") or {}).get(figure_id)),
                    "source_signature": copy.deepcopy(
                        (kwargs.get("source_signatures") or {}).get(figure_id)),
                }
        return copy.deepcopy(self.seed.get(figure_id))

    def list_figures(self, _root, kb_chunks, **_kwargs):
        rows = []
        for chunk in kb_chunks or []:
            if not chunk.get("structured"):
                continue
            stored = self._lookup(chunk["figure_id"])
            if stored is None:
                continue
            rows.append({
                "document_id": chunk["document_id"], "figure_id": chunk["figure_id"],
                "kind": stored["kind"], "revision": chunk["revision"],
                "verification_status": chunk["verification_status"],
                "payload": stored["payload"], "payload_error": "", "in_kb": True,
                "source_signature": stored["source_signature"],
                "human_verification": stored["human_verification"],
                "evidence_ref": chunk.get("evidence_ref", ""),
            })
        return rows


def _seed_human_verified_kb(monkeypatch, tmp_path: Path, harness: _Harness, *,
                            asset_digest: str, revision: int = 3) -> Path:
    """KB 裡已有一張被人工確認過的 figure；artifacts 由 `_FakeArtifactStore` 代表。"""
    kb_path = tmp_path / "knowledge.json"
    old_document_id = "old_reg_spec.pdf::ffffffffffffffff"
    old_figure_id = figure_extract.figure_id_for(
        old_document_id, 1, TABLE_BBOX, PAGE_RECT, asset_digest)
    evidence_ref = ".codetrail/figures/slug/20260101-000000-deadbeef/manifest.json"
    kb_path.write_text(json.dumps({
        "metadata": {"embedding_model": RAG.EMBEDDING_MODEL, "documents": ["reg_spec.pdf"],
                     "total_documents": 1, "total_chunks": 1},
        "chunks": [{
            "id": "reg_spec.pdf::p1::c1::deadbeef", "source": "reg_spec.pdf", "page": 1,
            "chunk_index": 1, "content": "舊的人工確認內容", "type": "spec", "section": "",
            "structured": True, "origin": "figure_table", "figure_kind": "table",
            "figure_id": old_figure_id, "document_id": old_document_id,
            "revision": revision, "figure_index": 1, "bbox": list(TABLE_BBOX),
            "verification_status": figure_extract.VERIF_HUMAN,
            "evidence_ref": evidence_ref, "embedding": [1.0, 0.0],
        }],
    }, ensure_ascii=False), encoding="utf-8")

    signature = _signature_for(asset_digest=asset_digest)
    store = _FakeArtifactStore(harness, seed={old_figure_id: {
        "payload": _table_payload(
            ["Name", "Addr", "Bits", "Access", "Description"], HUMAN_ROWS),
        "kind": figure_extract.KIND_TABLE,
        "source_signature": signature,
        "human_verification": {"revision": revision, "confirmed_against_image": True,
                               "at": "2026-01-01T00:00:00+08:00",
                               "payload_path": ".codetrail/figures/slug/"
                                               "20260101-000000-deadbeef/revisions/3/payload.json",
                               "source_signature": signature},
    }})
    monkeypatch.setattr(figure_extract, "list_figures", store.list_figures, raising=False)
    return kb_path, store


@pytest.mark.smoke
def test_reingest_keeps_the_human_fix_across_repeated_runs(tmp_path: Path, monkeypatch, capsys):
    """來源像素與框未變 → 沿用人工 payload、`human_verified` 與舊 revision。

    **連跑兩輪**：只驗一次的話，「新 manifest 沒記下 human_verification」這個洞
    抓不到——人工修正會在第二次 re-ingest 才被丟掉。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(RAG.llama_client, "embed_one", lambda **_kw: [1.0, 0.0])
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)
    kb_path, _store = _seed_human_verified_kb(
        monkeypatch, tmp_path, harness, asset_digest="asset", revision=3)

    for attempt in (1, 2):
        RAG.add_document(str(pdf), str(kb_path))

        structured = [c for c in _kb_chunks(kb_path) if c.get("structured")]
        assert structured, f"第 {attempt} 輪之後 structured chunk 要在"
        assert all(c["verification_status"] == figure_extract.VERIF_HUMAN
                   for c in structured), f"第 {attempt} 輪掉了 human_verified"
        assert all(c["revision"] == 3 for c in structured), (
            f"第 {attempt} 輪 revision 倒退了: {[c['revision'] for c in structured]}"
            "——人工確認過的版本不得因為重灌而退回 1")
        body = "\n".join(c["content"] for c in structured)
        assert "人工更正後的時脈選擇" in body, f"第 {attempt} 輪沒沿用人工 canonical payload"
        assert "clock select" not in body, f"第 {attempt} 輪被機器抽取的描述蓋掉了"
        assert "human_verification_carried_over" in structured[0]["reasons"]

        # 新 manifest 必須原樣記下這筆人工確認，否則下一輪就讀不到（§15.7）
        (_args, kwargs) = harness.write_artifacts.calls[-1]
        record = (kwargs.get("human_verifications") or {}).get(structured[0]["figure_id"])
        assert isinstance(record, dict), (
            f"第 {attempt} 輪沒把 human_verification 傳給 write_run_artifacts")
        assert record["confirmed_against_image"] is True
        assert record["revision"] == 3, "沿用的紀錄 revision 必須等於該 figure 的 revision"
        assert record["source_signature"] == _signature_for(), record["source_signature"]

    assert capsys.readouterr().out.count("沿用第 3 版人工確認") == 2


@pytest.mark.smoke
def test_reingest_drops_the_human_fix_when_the_source_pixels_changed(
    tmp_path: Path, monkeypatch
):
    """來源像素改變 → fail-closed 不沿用，revision 從 1 起並記錄原因。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(RAG.llama_client, "embed_one", lambda **_kw: [1.0, 0.0])
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)
    kb_path, _store = _seed_human_verified_kb(
        monkeypatch, tmp_path, harness, asset_digest="OTHER_PIXELS", revision=3)

    RAG.add_document(str(pdf), str(kb_path))

    structured = [c for c in _kb_chunks(kb_path) if c.get("structured")]
    assert structured
    assert all(c["verification_status"] == figure_extract.VERIF_NATIVE for c in structured)
    assert all(c["revision"] == 1 for c in structured)
    body = "\n".join(c["content"] for c in structured)
    assert "clock select" in body and "人工更正後" not in body
    assert "human_verification_not_carried" in structured[0]["reasons"], structured[0]["reasons"]
    (_args, kwargs) = harness.write_artifacts.calls[-1]
    assert kwargs.get("human_verifications") == {}, (
        "沒沿用就不得在新 manifest 掛人工確認紀錄")


@pytest.mark.smoke
def test_stale_human_record_revision_is_not_carried_over(tmp_path: Path, monkeypatch):
    """紀錄自報的 revision 與 KB 不同（artifact 落後）→ 誠實不沿用，不是硬寫下去。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(RAG.llama_client, "embed_one", lambda **_kw: [1.0, 0.0])
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)
    kb_path, store = _seed_human_verified_kb(
        monkeypatch, tmp_path, harness, asset_digest="asset", revision=3)
    # KB 說 revision 3，紀錄自報 2（artifact 落後）
    for entry in store.seed.values():
        entry["human_verification"]["revision"] = 2

    RAG.add_document(str(pdf), str(kb_path))

    structured = [c for c in _kb_chunks(kb_path) if c.get("structured")]
    assert all(c["revision"] == 1 for c in structured)
    assert all(c["verification_status"] == figure_extract.VERIF_NATIVE for c in structured)


def test_first_ingest_does_not_record_a_carry_over_reason(tmp_path: Path, monkeypatch):
    """第一次入庫沒有東西可沿用，不該在每個 chunk 的 reasons 上製造雜訊。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf, _h, _fid = _simple_native_case(tmp_path, monkeypatch)

    RAG.add_document(str(pdf), str(kb_path))

    structured = [c for c in _kb_chunks(kb_path) if c.get("structured")]
    assert structured
    assert all("human_verification_not_carried" not in c["reasons"] for c in structured)
    assert all("human_verification_carried_over" not in c["reasons"] for c in structured)


# ============================================================
# 13. local review 修正:native terminal 的 page partition（§8-4）
# ============================================================
LOG_INTRO = "The boot sequence below is captured verbatim. \n\n"
LOG_MD = ("```\n"
          "  $ dmesg | tail -3  \n"
          "[    0.000000] boot: OK\n"
          "\n"
          "[    0.123456] cpu0: online\n"
          "```\n")
LOG_TAIL = "\n\nEnd of the boot discussion. \n\n"
LOG_PAGE = LOG_INTRO + LOG_MD + LOG_TAIL
LOG_POS = (len(LOG_INTRO), len(LOG_INTRO) + len(LOG_MD) + 2)
LOG_BBOX = (60.0, 200.0, 520.0, 320.0)
LOG_LINES = ["  $ dmesg | tail -3  ", "[    0.000000] boot: OK", "",
             "[    0.123456] cpu0: online"]


def _native_terminal_case(tmp_path: Path, monkeypatch, *, pos=LOG_POS, box_class="code"):
    """一頁 = 前言 + `pos` 支撐的 log 區塊 + 後記；terminal 走 native lane。"""
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1, LOG_BBOX, "log")
    boxes = [{"index": 0, "class": box_class, "bbox": LOG_BBOX, "pos": pos}]
    pages = [_page(1, LOG_PAGE, boxes)]
    candidate = _candidate(document_id, figure_id, bbox=LOG_BBOX,
                           kind=figure_extract.KIND_TERMINAL, native_lane=True,
                           occurrences=[_occurrence(1, LOG_BBOX)])
    candidate = dataclasses.replace(candidate, signals={
        "native_lane": True,
        "native_text": {"pos": pos, "markdown": LOG_MD,
                        "source": f"page_boxes:{box_class}", "box_index": 0},
    })
    evidence = {1: FakePageEvidence(page=1, raw_markdown=LOG_PAGE, page_boxes=boxes)}
    result = _result(document_id, figure_id, bbox=LOG_BBOX,
                     kind=figure_extract.KIND_TERMINAL,
                     payload=_terminal_payload(LOG_LINES),
                     status=figure_extract.VERIF_UNVERIFIED,
                     occurrences=[_occurrence(1, LOG_BBOX)])
    harness = _harness(monkeypatch, tmp_path, pages,
                       _plan(document_id, [candidate], evidence), [result])
    return pdf, harness, figure_id


@pytest.mark.smoke
def test_native_terminal_text_is_replaced_and_never_duplicated(tmp_path: Path, monkeypatch):
    """`pos` 支撐的 log:原文必須被切掉，KB 內每一行只出現一次（§8-4）。

    terminal 的 native 正文來源是 `signals["native_text"]`，不是 `native_table`；
    只看 `native_table` 的話這條路徑會被當成「正文不在 markdown 裡」，原始 log
    與 structured chunk 就會同時留在 KB。
    """
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf, _h, figure_id = _native_terminal_case(tmp_path, monkeypatch)

    RAG.add_document(str(pdf), str(kb_path))

    chunks = _kb_chunks(kb_path)
    text_chunks = [c for c in chunks if not c.get("structured")]
    structured = [c for c in chunks if c.get("structured")]
    assert structured and all(c["origin"] == "figure_terminal" for c in structured)

    marker = f"[表格已改以結構化 chunk 收錄：figure={figure_id} page=1 rows=4]"
    assert any(marker in c["content"] for c in text_chunks), (
        f"原 log 位置要留下 marker，實際: {[c['content'] for c in text_chunks]}")
    for line in ("$ dmesg | tail -3", "[    0.000000] boot: OK", "[    0.123456] cpu0: online"):
        assert _occurrences_in(chunks, line) == 1, (
            f"{line!r} 在 KB 出現 {_occurrences_in(chunks, line)} 次——"
            "原始 log 與 structured chunk 兩份並存")
        assert _occurrences_in(text_chunks, line) == 0, "原始 log 沒有被切掉"
    joined = "\n".join(c["content"] for c in text_chunks)
    assert "The boot sequence below is captured verbatim." in joined
    assert "End of the boot discussion." in joined


@pytest.mark.smoke
def test_native_terminal_without_usable_pos_keeps_the_original_log(
    tmp_path: Path, monkeypatch, capsys
):
    """`pos` 不可信時 terminal 一樣退回原文，且不得產 structured chunk。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf, _h, _fid = _native_terminal_case(tmp_path, monkeypatch, pos=(5, 999999))

    RAG.add_document(str(pdf), str(kb_path))

    out = capsys.readouterr().out
    chunks = _kb_chunks(kb_path)
    assert not [c for c in chunks if c.get("structured")]
    assert "保留原始文字（不重複入庫）" in out, out
    assert _occurrences_in(chunks, "[    0.000000] boot: OK") == 1


# ============================================================
# 14. local review 修正:occurrence 身分、重複候選、整組 all-or-none
# ============================================================
@pytest.mark.smoke
@pytest.mark.parametrize("mutation,needle", [
    ("bbox", "occurrence 身分"),
    ("index", "occurrence 身分"),
    ("multiplicity", "occurrence 身分"),
])
def test_occurrence_identity_mismatch_hard_fails(tmp_path: Path, monkeypatch,
                                                 mutation, needle):
    """occurrence 只比頁碼集合的話，錯 bbox / 錯 index / 重複次數都會靜默通過。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1)
    pages = [_page(1, PAGE1, [_table_box(POS1)])]
    occurrences = [_occurrence(1)]
    candidate = _candidate(
        document_id, figure_id, occurrences=occurrences,
        native_table={"pos": POS1, "markdown": TABLE_MD, "geometry": {}, "strategy": "lines"})
    evidence = {1: FakePageEvidence(page=1, raw_markdown=PAGE1, page_boxes=pages[0]["page_boxes"])}
    if mutation == "bbox":
        bad = [_occurrence(1, (1.0, 2.0, 3.0, 4.0))]
    elif mutation == "index":
        bad = [_occurrence(1, TABLE_BBOX, 7)]
    else:
        bad = [_occurrence(1), _occurrence(1, TABLE_BBOX, 1)]
    result = _result(document_id, figure_id, occurrences=bad)
    _harness(monkeypatch, tmp_path, pages, _plan(document_id, [candidate], evidence), [result])

    with pytest.raises(figure_extract.FigureExtractionError, match=needle):
        RAG.add_document(str(pdf), str(kb_path))

    assert kb_path.read_bytes() == before


@pytest.mark.smoke
def test_duplicate_candidate_figure_id_hard_fails(tmp_path: Path, monkeypatch):
    """planner 撞號時 manifest / crop / chunk 會指向不同實體位置。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1)
    pages = [_page(1, PAGE1, [_table_box(POS1)])]
    native = {"pos": POS1, "markdown": TABLE_MD, "geometry": {}, "strategy": "lines"}
    candidate = _candidate(document_id, figure_id, native_table=native)
    twin = dataclasses.replace(candidate, index=2, bbox=(70.0, 110.0, 530.0, 190.0))
    evidence = {1: FakePageEvidence(page=1, raw_markdown=PAGE1, page_boxes=pages[0]["page_boxes"])}
    _harness(monkeypatch, tmp_path, pages,
             _plan(document_id, [candidate, twin], evidence),
             [_result(document_id, figure_id)])

    with pytest.raises(figure_extract.FigureExtractionError, match="兩個 figure_id"):
        RAG.add_document(str(pdf), str(kb_path))

    assert kb_path.read_bytes() == before


@pytest.mark.smoke
def test_one_bad_occurrence_retains_the_whole_shared_group(tmp_path: Path, monkeypatch, capsys):
    """共享 occurrence 的兩個候選:一個 pos 無效，另一個也不得單獨產 structured。

    只證明「counterpart 候選存在」不夠——它稍後也可能失格，那樣 KB 就會同時留下
    raw 與 structured 兩種表示。
    """
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    head2 = "Second page repeats the same table. \n\n"
    page2 = head2 + TABLE_MD + "\n\nAfter it. \n\n"
    pos2 = (len(head2), len(head2) + len(TABLE_MD) + 2)
    bbox2 = (60.0, 500.0, 520.0, 580.0)
    fid1 = _figure_id(document_id, 1)
    fid2 = _figure_id(document_id, 2, bbox2)
    boxes1 = [_table_box(POS1)]
    boxes2 = [_table_box(pos2, bbox2)]
    pages = [_page(1, PAGE1, boxes1), _page(2, page2, boxes2)]
    shared = [_occurrence(1, TABLE_BBOX), _occurrence(2, bbox2)]
    native = {"markdown": TABLE_MD, "geometry": {}, "strategy": "lines"}
    candidates = [
        _candidate(document_id, fid1, page=1, bbox=TABLE_BBOX, occurrences=shared,
                   native_table={**native, "pos": POS1}, index=1),
        # 第 2 頁的 pos 壞掉
        _candidate(document_id, fid2, page=2, bbox=bbox2, occurrences=shared,
                   native_table={**native, "pos": None}, index=2),
    ]
    evidence = {1: FakePageEvidence(page=1, raw_markdown=PAGE1, page_boxes=boxes1),
                2: FakePageEvidence(page=2, raw_markdown=page2, page_boxes=[])}
    results = [_result(document_id, fid1, page=1, bbox=TABLE_BBOX, occurrences=shared),
               _result(document_id, fid2, page=2, bbox=bbox2, occurrences=shared)]
    _harness(monkeypatch, tmp_path, pages, _plan(document_id, candidates, evidence), results)

    RAG.add_document(str(pdf), str(kb_path))

    out = capsys.readouterr().out
    chunks = _kb_chunks(kb_path)
    assert not [c for c in chunks if c.get("structured")], "整組都要退回原文"
    assert _occurrences_in(chunks, "0x4000_0100") == 2, "兩頁的原表都要原樣保留"
    assert "另一個位置無法安全取代" in out, out


# ============================================================
# 15. local review 修正:artifact 真相（figure_index / 覆核影像 / locator）
# ============================================================
@pytest.mark.smoke
def test_manifest_and_kb_agree_on_figure_index(tmp_path: Path, monkeypatch):
    """legacy offset 要在**寫 artifact 之前**套用，manifest 與 KB 不得用兩套序號。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1)
    boxes = [{"class": "picture", "bbox": BIG_BBOX_FOR_LANE},
             _table_box(POS1)]
    pages = [_page(1, PAGE1, boxes)]
    candidate = _candidate(
        document_id, figure_id,
        native_table={"pos": POS1, "markdown": TABLE_MD, "geometry": {}, "strategy": "lines"})
    evidence = {1: FakePageEvidence(page=1, raw_markdown=PAGE1, page_boxes=boxes)}
    harness = _harness(monkeypatch, tmp_path, pages,
                       _plan(document_id, [candidate], evidence),
                       [_result(document_id, figure_id)])
    monkeypatch.setattr(RAG, "_render_pdf_figure_png", lambda _d, _j: b"PNG")
    monkeypatch.setattr(RAG, "_describe_technical_image_base64", lambda *_a, **_k: "# 圖\n\n描述")

    RAG.add_document(str(pdf), str(kb_path))

    (_args, kwargs) = harness.write_artifacts.calls[-1]
    manifest_index = {f.figure_id: f.figure_index for f in kwargs["figures"]}
    kb_index = {c["figure_id"]: c["figure_index"]
                for c in _kb_chunks(kb_path) if c.get("structured")}
    assert kb_index, "structured chunk 要在"
    assert manifest_index == kb_index, (
        f"manifest 與 KB 的 figure_index 不同:{manifest_index} vs {kb_index}"
        "——覆核與檢索會用到兩套身分")
    assert set(kb_index.values()) == {2}, "同頁有 legacy 圖時 structured 要接在它之後"


@pytest.mark.smoke
@pytest.mark.parametrize("mode", ["raises", "empty"])
def test_missing_review_asset_blocks_the_whole_ingest(tmp_path: Path, monkeypatch, mode):
    """沒有任何影像可供人工覆核的 structured chunk 不得入庫（Go/No-Go 5）。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)
    if mode == "raises":
        def _render(*_a, **_k):
            raise RuntimeError("pixmap allocation failed (stub)")
    else:
        def _render(*_a, **_k):
            return []
    monkeypatch.setattr(figure_extract, "render_candidate_variants", _render, raising=False)

    with pytest.raises(figure_extract.FigureExtractionError, match="覆核"):
        RAG.add_document(str(pdf), str(kb_path))

    assert kb_path.read_bytes() == before
    (_args, kwargs) = harness.write_artifacts.calls[-1]
    assert kwargs["failed"] is True, "post-validation 失敗也要留 per-figure 的 failed artifact"


@pytest.mark.smoke
def test_dangling_manifest_locator_hard_fails(tmp_path: Path, monkeypatch):
    """writer 回一個不存在的路徑 → 不得把懸空的 evidence_ref 寫進 KB。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf, _h, _fid = _simple_native_case(tmp_path, monkeypatch)
    monkeypatch.setattr(
        figure_extract, "write_run_artifacts",
        lambda root, **kw: (Path(root) / ".codetrail" / "figures" / "slug"
                            / kw["run_id"] / "manifest.json"),
        raising=False)

    with pytest.raises(figure_extract.FigureExtractionError, match="manifest"):
        RAG.add_document(str(pdf), str(kb_path))

    assert kb_path.read_bytes() == before


@pytest.mark.smoke
def test_non_bool_native_lane_signal_is_rejected(tmp_path: Path, monkeypatch):
    """`"false"` 這種值被 truthiness 轉型會讓 probe 判定靜默反過來。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1)
    pages = [_page(1, PAGE1, [_table_box(POS1)])]
    candidate = dataclasses.replace(
        _candidate(document_id, figure_id,
                   native_table={"pos": POS1, "markdown": TABLE_MD, "geometry": {},
                                 "strategy": "lines"}),
        signals={"native_lane": "false"})
    evidence = {1: FakePageEvidence(page=1, raw_markdown=PAGE1, page_boxes=pages[0]["page_boxes"])}
    harness = _harness(monkeypatch, tmp_path, pages,
                       _plan(document_id, [candidate], evidence),
                       [_result(document_id, figure_id)])

    with pytest.raises(figure_extract.FigureExtractionError) as exc:
        RAG.add_document(str(pdf), str(kb_path))

    assert figure_id in str(exc.value), str(exc.value)
    assert kb_path.read_bytes() == before
    assert harness.extract.calls == [], "signal 驗證要早於抽取"


# ============================================================
# 16. local review 修正:來源身分的兩段 TOCTOU
# ============================================================
@pytest.mark.smoke
def test_source_changed_between_text_parse_and_planning_blocks_everything(
    tmp_path: Path, monkeypatch
):
    """`to_markdown()` 與 planner 建 document_id 之間換檔 → 文字與 figure 會來自兩個版本。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)

    real_plan = harness.plan_spy.result
    observed = {}

    def _swap_then_plan(*_a, **_k):
        # 原地改寫成**同長度**的內容，再把 atime/mtime 還原到改寫前的奈秒值：
        # `(size, mtime_ns, inode)` 三項因此逐項相同——舊的 stat-tuple guard 完全看不到
        # 這次改寫，只有整檔 sha256 抓得到（契約 §18.3）。
        before = pdf.stat()
        pdf.write_bytes(b"%PDF-fake-for-structured-LANE")
        os.utime(pdf, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = pdf.stat()
        observed["before"] = (before.st_size, before.st_mtime_ns, before.st_ino)
        observed["after"] = (after.st_size, after.st_mtime_ns, after.st_ino)
        # 真 planner 會以新版的 bytes 算 document_id
        return dataclasses.replace(
            real_plan, document_id=figure_extract.document_id_for(pdf, tmp_path))

    monkeypatch.setattr(figure_extract, "plan_document_figures", _swap_then_plan,
                        raising=False)

    with pytest.raises(figure_extract.FigureExtractionError, match="來源檔被換掉"):
        RAG.add_document(str(pdf), str(kb_path))

    # 這條測試的前提:stat tuple 真的沒變（否則它對舊的 stat-tuple 實作是假綠）
    assert observed["after"] == observed["before"], (
        f"stat tuple 變了 {observed['before']} → {observed['after']}，"
        "這條測試就不是在守內容 digest 了")
    assert kb_path.read_bytes() == before
    assert harness.extract.calls == [], "身分對不上就不該再往下抽取"


@pytest.mark.smoke
def test_source_changed_before_commit_blocks_the_write(tmp_path: Path, monkeypatch):
    """抽取完成到 exclusive commit 之間換檔 → 零寫入（鎖內最後一道）。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf, _h, _fid = _simple_native_case(tmp_path, monkeypatch)
    original = RAG.generate_embeddings

    def _swap_then_embed(chunks, cache_dir=None, **kwargs):
        # 模擬「embedding 期間（可能幾十分鐘）來源檔被換掉」
        pdf.write_bytes(b"%PDF-fake-replaced-during-embedding")
        return original(chunks, cache_dir, **kwargs)

    monkeypatch.setattr(RAG, "generate_embeddings", _swap_then_embed)

    with pytest.raises(figure_extract.FigureExtractionError, match="來源檔已變更"):
        RAG.add_document(str(pdf), str(kb_path))

    assert kb_path.read_bytes() == before


# ============================================================
# 17. local review 修正:並行的人工修正不得被蓋掉
# ============================================================
@pytest.mark.smoke
def test_concurrent_human_fix_is_not_overwritten(tmp_path: Path, monkeypatch):
    """carry-over 在鎖外讀 revision 3，別人先提交 revision 4 → 我們必須放棄，不是蓋掉。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(RAG.llama_client, "embed_one", lambda **_kw: [1.0, 0.0])
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)
    kb_path, _store = _seed_human_verified_kb(
        monkeypatch, tmp_path, harness, asset_digest="asset", revision=3)
    original = RAG.generate_embeddings

    def _interleave(chunks, cache_dir=None, **kwargs):
        # 另一個「行程」在我們算 embedding 時完成了 review_figures fix
        data = json.loads(kb_path.read_text(encoding="utf-8"))
        for chunk in data["chunks"]:
            if chunk.get("structured"):
                chunk["revision"] = 4
                chunk["content"] = "更新的人工確認內容"
        kb_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return original(chunks, cache_dir, **kwargs)

    monkeypatch.setattr(RAG, "generate_embeddings", _interleave)

    with pytest.raises(figure_extract.FigureExtractionError, match="併發衝突"):
        RAG.add_document(str(pdf), str(kb_path))

    surviving = _kb_chunks(kb_path)
    assert [c for c in surviving if c.get("revision") == 4], (
        "別人剛完成的 revision 4 必須原封不動地留著")
    assert not [c for c in surviving if c.get("revision") == 1], "不得被 revision 1 蓋掉"


# ============================================================
# 18. local review 修正:證據壞掉不得被當成「第一次 ingest」
# ============================================================
@pytest.mark.smoke
def test_unusable_human_evidence_is_reported_not_silently_dropped(
    tmp_path: Path, monkeypatch, capsys
):
    """KB 說有人工確認、但 artifact 讀不回來 → 要說原因，不是靜默降級。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(RAG.llama_client, "embed_one", lambda **_kw: [1.0, 0.0])
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)
    kb_path, store = _seed_human_verified_kb(
        monkeypatch, tmp_path, harness, asset_digest="asset", revision=3)
    for entry in store.seed.values():
        entry["payload"] = None      # artifact 被回收 / 讀不回來

    RAG.add_document(str(pdf), str(kb_path))

    out = capsys.readouterr().out
    structured = [c for c in _kb_chunks(kb_path) if c.get("structured")]
    assert structured and all(c["revision"] == 1 for c in structured)
    assert "既有的人工確認無法沿用" in out, out
    assert "human_verification_not_carried" in structured[0]["reasons"]
    details = " ".join(structured[0]["reason_details"])
    assert "payload" in details, f"reason_details 沒說明證據為什麼不可用: {details}"


# ============================================================
# 19. local review 修正:VLM lane 的 table row identity（workflow §5 table ⑦）
# ============================================================
@pytest.mark.smoke
def test_visual_lane_table_row_identity_survives_into_the_kb(tmp_path: Path, monkeypatch):
    """`native_table=None` 的 VLM 表：五欄逐列驗，並確認原 markdown 不重複。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1, digest="vlm")
    text = "Figure 3 below shows the interrupt controller register map. "
    pages = [_page(1, text, [{"index": 0, "class": "text", "bbox": (60, 60, 520, 80),
                              "pos": (0, len(text))}])]
    candidate = _candidate(document_id, figure_id, kind=figure_extract.KIND_TABLE,
                           native_table=None, native_lane=False)
    evidence = {1: FakePageEvidence(page=1, raw_markdown=text, page_boxes=pages[0]["page_boxes"])}
    plan = _plan(document_id, [candidate], evidence,
                 preflight={"candidates": 1, "tiles": 1, "vl_calls_min": 1, "vl_calls_max": 2,
                            "image_tokens_est": 512, "pages": 1, "native_tables": 0})
    payload = _table_payload(["Name", "Addr", "Bits", "Access", "Description"], GROUND_TRUTH)
    result = _result(document_id, figure_id, kind=figure_extract.KIND_TABLE, payload=payload,
                     status=figure_extract.VERIF_UNVERIFIED,
                     variants=["crop@200dpi"], model_input_variant="crop@200dpi")
    harness = _harness(monkeypatch, tmp_path, pages, plan, [result],
                       variants=[FakeVariant(figure_id=figure_id, variant_id="crop@200dpi")])

    RAG.add_document(str(pdf), str(kb_path))

    chunks = _kb_chunks(kb_path)
    structured = [c for c in chunks if c.get("structured")]
    assert structured and all(c["origin"] == "figure_table" for c in structured)
    assert harness.ensure_capability.calls, "VLM lane 要先過 capability probe"
    _assert_row_identity([c["content"] for c in structured], GROUND_TRUTH)
    # 衍生文字合計只 render 一次（原 page markdown 裡本來就沒有這張表）
    for value in ("0x4000_0100", "0x4000_0104", "clock select", "status flags"):
        assert _occurrences_in(chunks, value) == 1, value
    assert _occurrences_in([c for c in chunks if not c.get("structured")], "0x4000") == 0


# ============================================================
# 20. local review 修正:carry-over 走真的 artifact 持久化路徑
# ============================================================
def _use_real_artifact_store(monkeypatch):
    """把 artifact 的讀寫換回真貨，只留抽取/預算是替身。

    契約 §15.7 要求的是「端到端」：兩端都用假 store 的話，真 writer 沒寫下人工紀錄、
    真 reader 讀不回 payload，測試照樣全綠。
    """
    import figure_review

    for name in ("write_run_artifacts", "list_figures", "read_manifest",
                 "evidence_ref_for", "new_run_id", "may_carry_over_human_verification",
                 "source_signature"):
        monkeypatch.setattr(figure_extract, name, getattr(figure_review, name),
                            raising=False)
    return figure_review


def _simulate_human_fix(figure_review, tmp_path: Path, kb_path: Path, *, revision: int):
    """在真的 artifact 樹上模擬一次 `review_figures fix` 的結果。

    直接跑 `apply_fix` 需要 rechunk/embed 注入與完整鎖交易；這裡只要「上一輪確實留下
    人工確認」這個前提，所以照 `apply_fix` 的落盤形狀寫進真檔案。
    """
    kb = json.loads(kb_path.read_text(encoding="utf-8"))
    members = [c for c in kb["chunks"] if c.get("structured")]
    assert members, "第一輪要先產出 structured chunk"
    evidence_ref = members[0]["evidence_ref"]
    figure_id = members[0]["figure_id"]
    document_id = members[0]["document_id"]
    manifest_path = tmp_path / evidence_ref
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(e for e in manifest["figures"] if e["figure_id"] == figure_id)

    human_payload = _table_payload(
        ["Name", "Addr", "Bits", "Access", "Description"], HUMAN_ROWS)
    rel_dir = f"{Path(evidence_ref).parent.as_posix()}/revisions/{revision}"
    payload_rel = f"{rel_dir}/payload.json"
    # `figure_review` 拒絕 group/world 可寫的目錄（別人能替換裡面的檔案），
    # 所以這裡要照它的方式建 0700，不能吃 umask 的預設值
    (tmp_path / rel_dir).mkdir(parents=True, exist_ok=True)
    os.chmod(tmp_path / rel_dir, 0o700)
    os.chmod((tmp_path / rel_dir).parent, 0o700)
    (tmp_path / payload_rel).write_text(json.dumps({
        "schema": figure_review.REVISION_SCHEMA,
        "figure_id": figure_id, "document_id": document_id, "kind": entry["kind"],
        "revision": revision, "confirmed_against_image": True, "payload": human_payload,
    }, ensure_ascii=False), encoding="utf-8")

    entry["current_revision"] = revision
    entry["verification_status"] = figure_extract.VERIF_HUMAN
    entry["payload"] = human_payload
    entry["reasons"] = ["human_corrected"]
    entry["reason_details"] = [f"使用者確認並修正 → revision {revision}"]
    entry["human_verification"] = {
        "revision": revision, "confirmed_against_image": True,
        "at": "2026-01-01T00:00:00+08:00", "payload_path": payload_rel,
        "source_signature": entry.get("source_signature"),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    for chunk in members:
        chunk["revision"] = revision
        chunk["verification_status"] = figure_extract.VERIF_HUMAN
    kb_path.write_text(json.dumps(kb, ensure_ascii=False), encoding="utf-8")
    return figure_id


def test_carry_over_survives_the_real_artifact_store(tmp_path: Path, monkeypatch):
    """真 `write_run_artifacts` + 真 `list_figures`：連跑三輪，revision 不得倒退。

    這條走的是契約指定的 persistence code path——manifest 真的被寫出來、真的被讀回去。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(RAG.llama_client, "embed_one", lambda **_kw: [1.0, 0.0])
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)
    figure_review = _use_real_artifact_store(monkeypatch)
    kb_path = tmp_path / "knowledge.json"

    # 第一輪：機器抽取，真 manifest 落盤
    RAG.add_document(str(pdf), str(kb_path))
    first = [c for c in _kb_chunks(kb_path) if c.get("structured")]
    assert first and all(c["revision"] == 1 for c in first)
    manifest_path = tmp_path / first[0]["evidence_ref"]
    assert manifest_path.is_file(), "evidence_ref 必須指到真的存在的 manifest"

    figure_id = _simulate_human_fix(figure_review, tmp_path, kb_path, revision=2)

    # 第二、三輪：來源未變 → 沿用人工 payload 與 revision 2
    for attempt in (2, 3):
        RAG.add_document(str(pdf), str(kb_path))
        structured = [c for c in _kb_chunks(kb_path) if c.get("structured")]
        assert structured, f"第 {attempt} 輪沒有 structured chunk"
        assert all(c["verification_status"] == figure_extract.VERIF_HUMAN
                   for c in structured), f"第 {attempt} 輪掉了 human_verified"
        assert all(c["revision"] == 2 for c in structured), (
            f"第 {attempt} 輪 revision 倒退: {[c['revision'] for c in structured]}")
        body = "\n".join(c["content"] for c in structured)
        assert "人工更正後的時脈選擇" in body, f"第 {attempt} 輪沒沿用人工 payload"

        # 新 manifest 必須自己也記下人工確認，否則下一輪就讀不回來
        new_manifest = json.loads(
            (tmp_path / structured[0]["evidence_ref"]).read_text(encoding="utf-8"))
        entry = next(e for e in new_manifest["figures"] if e["figure_id"] == figure_id)
        assert entry["human_verification"], f"第 {attempt} 輪的 manifest 沒有人工確認紀錄"
        assert entry["human_verification"]["confirmed_against_image"] is True
        assert entry["human_verification"]["revision"] == 2
        assert entry["verification_status"] == figure_extract.VERIF_HUMAN


def test_changed_pixels_are_not_carried_over_through_the_real_store(
    tmp_path: Path, monkeypatch
):
    """真 store 上的另一半：來源像素改變 → fail-closed，不沿用。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(RAG.llama_client, "embed_one", lambda **_kw: [1.0, 0.0])
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)
    figure_review = _use_real_artifact_store(monkeypatch)
    kb_path = tmp_path / "knowledge.json"

    RAG.add_document(str(pdf), str(kb_path))
    _simulate_human_fix(figure_review, tmp_path, kb_path, revision=2)

    # 下一輪的候選 asset_digest 變了＝來源像素變了
    plan = harness.plan_spy.result
    changed = [dataclasses.replace(c, asset_digest="DIFFERENT_PIXELS")
               for c in plan.candidates]
    monkeypatch.setattr(figure_extract, "plan_document_figures",
                        lambda *_a, **_k: dataclasses.replace(plan, candidates=changed),
                        raising=False)

    RAG.add_document(str(pdf), str(kb_path))

    structured = [c for c in _kb_chunks(kb_path) if c.get("structured")]
    assert structured
    assert all(c["verification_status"] == figure_extract.VERIF_NATIVE for c in structured)
    assert all(c["revision"] == 1 for c in structured)
    assert "human_verification_not_carried" in structured[0]["reasons"]


@pytest.mark.smoke
def test_context_generation_sends_zero_requests_for_structured_chunks(tmp_path: Path,
                                                                     monkeypatch):
    """不只驗 predicate:整份都是 structured chunk 時，generator 必須一個請求都不送。

    只測 `is_generative_origin()` 的話，未來 eligibility loop 換寫法（不再呼叫它）
    仍會全綠，而症狀是靜默的——多一輪主模型，還會把生成文字黏在逐格忠實的表格前面。
    """
    from extracted_document import ExtractedDocument

    chunks = [
        {"source": "reg_spec.pdf", "page": 1, "chunk_index": 0, "content": "| A | B |",
         "structured": True, "origin": "figure_table"},
        {"source": "reg_spec.pdf", "page": 1, "chunk_index": 1, "content": "log line",
         "structured": True, "origin": "figure_terminal"},
        {"source": "reg_spec.pdf", "page": 2, "chunk_index": 0, "content": "VL 描述",
         "origin": "diagram"},
    ]
    document = ExtractedDocument(raw_text="doc", chunks=chunks, source="reg_spec.pdf")
    # 離線:不得碰 /props、不得讀本機 model 設定（AGENTS.md §5）
    monkeypatch.setattr(context_generation, "model_identity",
                        lambda *_a, **_k: {"n_ctx": 4096})
    # cache_dir 一定要明示指到 tmp_path：不給的話 `cache_root_for()` 會退回
    # `config.KB_CONTEXT_CACHE_DIR`（預設 `~/.cache/codetrail/ctx`），測試就會寫進
    # 使用者真正的 cache 目錄——在唯讀 home 直接紅，在一般環境是靜默污染。
    generator = context_generation.ContextGenerator(
        kb_path=tmp_path / "knowledge.json", model="stub-model", n_ctx=4096,
        cache_dir=tmp_path / "ctx")
    sent = []

    def _boom(*args, **kwargs):
        sent.append((args, kwargs))
        raise AssertionError("structured chunk 不該送任何主模型請求")

    monkeypatch.setattr(generator.session, "post", _boom)

    report = generator.generate_for_document(document)

    assert sent == []
    assert report.eligible == 0
    assert report.skipped == len(chunks)


# ============================================================
# 21. 終審第三輪修正:來源身分不得 fail-open（契約 §18.2）
# ============================================================
@pytest.mark.smoke
def test_legacy_only_pdf_still_verifies_the_source_before_commit(tmp_path: Path, monkeypatch):
    """零 structured candidate 的 PDF 一樣要帶 guard。

    text-only / legacy-only 也會產生文字 chunk 與 legacy 圖面 chunk；來源在中途被
    換掉時，同一份 KB 就會混進 A 版文字與 B 版圖面。以前 `guard=None` 讓這條路徑
    從 planner 到提交前完全不再核對來源（契約 §18.2）。
    """
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    pages = [_page(1, PAGE1, [_table_box(POS1)])]
    # 零 structured candidate → lane inactive
    harness = _harness(monkeypatch, tmp_path, pages, _plan(document_id, [], {}), [])
    original = RAG.generate_embeddings

    def _swap_then_embed(chunks, cache_dir=None, **kwargs):
        pdf.write_bytes(b"%PDF-fake-replaced-during-embedding")
        return original(chunks, cache_dir, **kwargs)

    monkeypatch.setattr(RAG, "generate_embeddings", _swap_then_embed)

    with pytest.raises(figure_extract.FigureExtractionError, match="來源檔已變更"):
        RAG.add_document(str(pdf), str(kb_path))

    assert harness.extract.calls == [], "零候選時本來就不該抽取"
    assert kb_path.read_bytes() == before, "來源換掉之後不得提交任何 chunk"


@pytest.mark.smoke
def test_unreadable_source_digest_fails_loud(tmp_path: Path, monkeypatch):
    """root 內的實體 PDF 算不出 digest → 零寫入，不得吞成「沒有快照」。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)

    def _boom(*_a, **_k):
        raise figure_extract.FigureError("permission denied (stub)")

    monkeypatch.setattr(figure_extract, "document_id_for", _boom, raising=False)

    with pytest.raises(figure_extract.FigureExtractionError, match="身分 digest"):
        RAG.add_document(str(pdf), str(kb_path))

    assert kb_path.read_bytes() == before
    assert harness.plan_spy.calls == [], "算不出身分就不該開始偵測候選"


@pytest.mark.smoke
def test_source_change_leaves_no_successful_manifest(tmp_path: Path, monkeypatch):
    """身分不符時不得留下 `failed:false` 的 manifest（發布前就要驗）。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)
    real_render = figure_extract.render_candidate_variants

    def _render_then_swap(doc, candidate):
        produced = real_render(doc, candidate)
        # 覆核影像 render 完、發布 manifest 之前，來源被換掉
        pdf.write_bytes(b"%PDF-fake-swapped-before-publishing")
        return produced

    monkeypatch.setattr(figure_extract, "render_candidate_variants", _render_then_swap,
                        raising=False)

    with pytest.raises(figure_extract.FigureExtractionError, match="發布 review artifact 之前"):
        RAG.add_document(str(pdf), str(kb_path))

    successful = [kwargs for (_a, kwargs) in harness.write_artifacts.calls
                  if kwargs.get("failed") is False]
    assert successful == [], "身分不符卻留下了成功的 manifest"


# ============================================================
# 22. 終審第三輪修正:native lane 的 variants 必須是空的（契約 §18.4）
# ============================================================
@pytest.mark.smoke
def test_native_result_with_non_empty_variants_hard_fails(tmp_path: Path, monkeypatch):
    """`model_input_variant="native"` 卻宣告 variants → 與 artifact writer 的契約矛盾。

    以前 RAG 特別放行 `variants=["native"]`，於是這個 writer 不接受的形狀在 RAG 這一側
    合法，跨模組矛盾永遠不會浮出來。
    """
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1)
    pages = [_page(1, PAGE1, [_table_box(POS1)])]
    candidate = _candidate(
        document_id, figure_id,
        native_table={"pos": POS1, "markdown": TABLE_MD, "geometry": {}, "strategy": "lines"})
    evidence = {1: FakePageEvidence(page=1, raw_markdown=PAGE1, page_boxes=pages[0]["page_boxes"])}
    result = _result(document_id, figure_id, variants=["native"],
                     model_input_variant="native")
    _harness(monkeypatch, tmp_path, pages, _plan(document_id, [candidate], evidence), [result])

    with pytest.raises(figure_extract.FigureExtractionError, match="variants 必須是空的"):
        RAG.add_document(str(pdf), str(kb_path))

    assert kb_path.read_bytes() == before


# ============================================================
# 23. 終審第四輪修正:切片候選必須另存完整原圖（契約 §19.2）
# ============================================================
def _tile_bbox(bbox, index: int, total: int) -> tuple:
    """第 `index`/`total` 片的框（橫向等分）。tile 的 bbox 本來就只涵蓋候選框的一段——
    fixture 讓它等於整個候選框的話，「局部 crop 冒充完整原圖」這件事永遠測不出來。"""
    x0, y0, x1, y1 = (float(v) for v in bbox)
    step = (y1 - y0) / total
    return (x0, y0 + step * (index - 1), x1, y0 + step * index)


def _tile_plan(bbox, *, tiles: int):
    """T3 形狀的 tile plan（`render_candidate_variants` 讀的就是這個）。"""
    if tiles <= 1:
        entries = [{"bbox": tuple(bbox), "tile_index": 0, "tile_total": 1, "overlap_px": 0}]
    else:
        entries = [{"bbox": tuple(bbox), "tile_index": i, "tile_total": tiles,
                    "overlap_px": 48} for i in range(1, tiles + 1)]
    return {"mode": "crop", "zoom": 2.7778, "effective_dpi": 200, "tiles": entries}


def _tiled_case(tmp_path: Path, monkeypatch, *, full_render_ok: bool):
    """VL 候選被切成兩片：模型輸入是 tile，覆核用原圖必須另外整框 render 一張。"""
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    figure_id = _figure_id(document_id, 1, digest="tiled")
    text = "Figure 4 is a tall register map rendered in two tiles. "
    pages = [_page(1, text, [{"index": 0, "class": "text", "bbox": (60, 60, 520, 80),
                              "pos": (0, len(text))}])]
    candidate = _candidate(document_id, figure_id, kind=figure_extract.KIND_TABLE,
                           native_table=None, native_lane=False)
    candidate = dataclasses.replace(candidate, signals={
        "native_lane": False, "tile_plan": _tile_plan(TABLE_BBOX, tiles=2)})
    evidence = {1: FakePageEvidence(page=1, raw_markdown=text, page_boxes=pages[0]["page_boxes"])}
    plan = _plan(document_id, [candidate], evidence,
                 preflight={"candidates": 1, "tiles": 2, "vl_calls_min": 2, "vl_calls_max": 2,
                            "image_tokens_est": 900, "pages": 1, "native_tables": 0})
    tile_ids = ["crop@200dpi#tile1of2", "crop@200dpi#tile2of2"]
    result = _result(document_id, figure_id, kind=figure_extract.KIND_TABLE,
                     payload=_table_payload(["Name", "Addr", "Bits", "Access", "Description"],
                                            GROUND_TRUTH),
                     status=figure_extract.VERIF_UNVERIFIED,
                     variants=tile_ids, model_input_variant=tile_ids[0])
    seen_plans = []

    def _tiles_of(cand):
        """真 renderer 產出的 tile：bbox 是**那一片**的框，不是整個候選框。"""
        return [FakeVariant(figure_id=cand.figure_id, variant_id=vid,
                            bbox=_tile_bbox(cand.bbox, i, 2), tile_index=i, tile_total=2,
                            overlap_px=48)
                for i, vid in enumerate(tile_ids, 1)]

    def _render(_doc, cand):
        plan_tiles = (cand.signals.get("tile_plan") or {}).get("tiles") or []
        total = int(plan_tiles[0]["tile_total"]) if plan_tiles else 1
        seen_plans.append(total)
        if total <= 1:
            if not full_render_ok:
                # renderer 給不出完整原圖（例如整框超出 pixmap 上限）→ 只剩切片
                return _tiles_of(cand)
            return [FakeVariant(figure_id=cand.figure_id, variant_id="crop@200dpi",
                                bbox=tuple(cand.bbox), tile_index=0, tile_total=1)]
        return _tiles_of(cand)

    harness = _harness(monkeypatch, tmp_path, pages, plan, [result])
    monkeypatch.setattr(figure_extract, "render_candidate_variants", _render, raising=False)
    return pdf, harness, figure_id, tile_ids, seen_plans


@pytest.mark.smoke
def test_tiled_candidate_also_stores_a_full_review_crop(tmp_path: Path, monkeypatch):
    """模型輸入是切片時，覆核用的完整原圖要另外 render 並走 `review_assets=`。

    只有一堆 tile 的話，覆核的人拿不到那張圖的完整原貌（workflow §8-5）。
    """
    kb_path = _kb_ready(monkeypatch, tmp_path)
    pdf, harness, figure_id, tile_ids, seen_plans = _tiled_case(
        tmp_path, monkeypatch, full_render_ok=True)

    RAG.add_document(str(pdf), str(kb_path))

    (_args, kwargs) = harness.write_artifacts.calls[-1]
    assert [v.variant_id for v in kwargs["variants"]] == tile_ids, (
        "實際模型輸入就是那些 tile，不得被覆核圖污染")
    review = kwargs["review_assets"].get(figure_id) or []
    assert [v.variant_id for v in review] == ["crop@200dpi"], review
    assert all(v.tile_total <= 1 for v in review), "覆核用的必須是未切片的整張圖"
    assert set(tile_ids).isdisjoint({v.variant_id for v in review}), (
        "同一個 variant_id 不得同時出現在 variants/ 與 review_assets/")
    # 覆核圖確實是以「單一整框 tile」重新 render 的（不是把某個 tile 改名）
    assert 1 in seen_plans, f"沒有以未切片的 tile plan 呼叫過 renderer: {seen_plans}"
    assert [c for c in _kb_chunks(kb_path) if c.get("structured")]


@pytest.mark.smoke
def test_tile_only_candidate_blocks_the_whole_ingest(tmp_path: Path, monkeypatch):
    """完整原圖產不出來 → 成功 manifest 與 KB mutation 之前就要 fail-loud。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf, harness, _fid, _tiles, _seen = _tiled_case(
        tmp_path, monkeypatch, full_render_ok=False)

    with pytest.raises(figure_extract.FigureExtractionError, match="只給得出切片"):
        RAG.add_document(str(pdf), str(kb_path))

    assert kb_path.read_bytes() == before
    successful = [kwargs for (_a, kwargs) in harness.write_artifacts.calls
                  if kwargs.get("failed") is False]
    assert successful == [], "沒有完整原圖就不得發布成功的 manifest"


# ============================================================
# 24. 終審第五輪修正:缺 tile metadata 不得被當成完整原圖（契約 §6.3 / §20.2）
# ============================================================
class _NoTileMetaVariant:
    """producer 契約漂移的樣態:`Variant` 少了凍結的 tile 欄位。

    以前 RAG 對缺 `tile_total` 預設 1、writer 那側預設 0 再 `<= 1`，兩邊都判成
    「完整未切片原圖」——於是「成功發布必須有完整原圖」那道閘，只要給一個沒有
    tile metadata 的 Variant 就繞過去了。
    """

    def __init__(self, figure_id: str, *, drop=("tile_total", "tile_index")):
        self.figure_id = figure_id
        self.variant_id = "crop@200dpi"
        self.png = PNG_BYTES
        self.width = 460
        self.height = 80
        self.bbox = TABLE_BBOX
        self.overlap_px = 0
        self.est_image_tokens = 64
        # 契約 §21.3：除了刻意 drop 的那幾個，其餘 §6.3 欄位一律填滿、digest 是真的
        # sha256——否則失敗原因會變成「fixture 沒填 digest」，而不是我們要測的缺欄位。
        self.digest = hashlib.sha256(self.png).hexdigest()
        self.mime = "image/png"
        for name, value in (("tile_index", 0), ("tile_total", 1)):
            if name not in drop:
                setattr(self, name, value)


@pytest.mark.smoke
@pytest.mark.parametrize("drop,needle", [
    (("tile_total",), "tile_total"),
    (("tile_index",), "tile_index"),
    (("tile_total", "tile_index"), "tile_total"),
])
def test_variant_without_tile_metadata_never_publishes(tmp_path: Path, monkeypatch,
                                                       drop, needle):
    """缺 tile metadata → 零寫入、不得留下成功 manifest（不猜「未切片」）。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf, harness, figure_id = _simple_native_case(tmp_path, monkeypatch)
    monkeypatch.setattr(
        figure_extract, "render_candidate_variants",
        lambda _doc, cand: [_NoTileMetaVariant(cand.figure_id, drop=drop)],
        raising=False)

    with pytest.raises(figure_extract.FigureExtractionError, match=needle) as exc:
        RAG.add_document(str(pdf), str(kb_path))

    # 訊息由**共用** validator 出（契約 §21.1）:要指得出「缺的是欄位」與契約條號，
    # 才不會與「欄位在、但值不合法」那條分支混在一起。
    assert "缺少欄位" in str(exc.value) and "§6.3" in str(exc.value), str(exc.value)
    assert kb_path.read_bytes() == before
    successful = [kwargs for (_a, kwargs) in harness.write_artifacts.calls
                  if kwargs.get("failed") is False]
    assert successful == [], "缺 tile metadata 不得發布成功的 manifest"


@pytest.mark.smoke
@pytest.mark.parametrize("index,total,needle", [
    (0, 0, "tile_total"),          # tile_total 必須 >= 1
    (-1, 1, "tile_index"),         # tile_index 必須 >= 0
    (True, 1, "tile_total"),       # bool 不是合法 int（True 會被當成 1）
    (2, 1, "必須是 tile_index=0"),  # 未切片卻宣稱是第 2 片
    (3, 2, r"超出 1\.\.2 的範圍"),   # 切片編號超出總數
])
def test_variant_with_illegal_tile_metadata_never_publishes(tmp_path: Path, monkeypatch,
                                                            index, total, needle):
    """非法範圍／型別的 tile metadata 同樣 fail-loud。"""
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf, _h, _fid = _simple_native_case(tmp_path, monkeypatch)
    if index is True:
        index, total = 0, True     # tile_total=True 才是要測的那個
    monkeypatch.setattr(
        figure_extract, "render_candidate_variants",
        lambda _doc, cand: [FakeVariant(figure_id=cand.figure_id,
                                        variant_id="crop@200dpi",
                                        tile_index=index, tile_total=total)],
        raising=False)

    with pytest.raises(figure_extract.FigureExtractionError, match=needle):
        RAG.add_document(str(pdf), str(kb_path))

    assert kb_path.read_bytes() == before


# ============================================================
# 25. 終審第六輪:三個消費端共用同一份 Variant validator（契約 §21.1 / §21.4）
# ============================================================
# 這條接縫（「哪一張是完整原圖」）已經被打回四輪，每一輪的機制都一樣:同一個凍結
# 介面有三個消費端（`figure_verify` 送 VL 前、`RAG` 的完整原圖閘、`figure_review`
# 寫 manifest），每輪只有被點名的一兩端收緊自己那份檢查，第三端維持寬鬆，繞道就
# 一直在。所以這一節的測試**同時**打三端:任何一端鬆掉都會在這裡紅。
_VARIANT_FIELD_NAMES = (
    "figure_id", "variant_id", "png", "digest", "width", "height",
    "bbox", "tile_index", "tile_total", "overlap_px", "est_image_tokens", "mime",
)

# 候選框的上半:tile flags 完全合法（未切片），但只涵蓋候選框的一部分。
# 這就是本輪 BLOCKER #1 的實測樣態——只驗 tile flags 的閘會把它當成完整原圖。
LOCAL_CROP_BBOX = (TABLE_BBOX[0], TABLE_BBOX[1], TABLE_BBOX[2],
                   TABLE_BBOX[1] + (TABLE_BBOX[3] - TABLE_BBOX[1]) / 2)


def _variant_fields(figure_id: str = "fig_variant_gate", **overrides) -> dict:
    """契約 §6.3 的**每一個**欄位都填齊的合法 Variant（`digest` 是真的 sha256）。

    缺欄位的 fixture 會讓 producer 漂移一直被掩蓋（契約 §21.3）——這正是本接縫連續
    四輪沒被抓到的原因，所以這裡不留任何「反正下游會補」的空位。
    """
    fields = {
        "figure_id": figure_id,
        "variant_id": "crop@200dpi",
        "png": PNG_BYTES,
        "digest": hashlib.sha256(PNG_BYTES).hexdigest(),
        "width": 460,
        "height": 80,
        "bbox": TABLE_BBOX,
        "tile_index": 0,
        "tile_total": 1,
        "overlap_px": 0,
        "est_image_tokens": 64,
        "mime": "image/png",
    }
    assert set(fields) == set(_VARIANT_FIELD_NAMES), "fixture 與 §6.3 的欄位集合不同步"
    fields.update(overrides)
    return fields


# (case_id, 要刪掉的欄位, 要覆寫的欄位)
_MALFORMED_VARIANTS = (
    [(f"missing:{name}", name, {}) for name in _VARIANT_FIELD_NAMES]
    + [
        # bool 是 int 的子類別:不明確擋掉的話 tile_total=True 會被當成 1
        ("tile_total=True", None, {"tile_total": True}),
        ("tile_index=True", None, {"tile_index": True, "tile_total": 2}),
        # str / float 經 int() 會被截成合法的 (1, 0)——所以不准 coercion
        ("tile_total='1'", None, {"tile_total": "1"}),
        ("tile_total=1.9", None, {"tile_total": 1.9}),
        ("tile_index='0'", None, {"tile_index": "0"}),
        ("width=0", None, {"width": 0}),
        ("height='80'", None, {"height": "80"}),
        ("overlap_px=-1", None, {"overlap_px": -1}),
        # 非正的 est_image_tokens 會讓送出前的 image-token 預算複查形同虛設
        ("est_image_tokens=0", None, {"est_image_tokens": 0}),
        ("est_image_tokens=-1", None, {"est_image_tokens": -1}),
        ("est_image_tokens=True", None, {"est_image_tokens": True}),
        # digest 是 manifest 與去重的身分:對不上代表送出的 bytes 不是被記錄的那一份
        ("digest 與 png 不符", None,
         {"digest": hashlib.sha256(PNG_BYTES + b"tampered").hexdigest()}),
        ("digest 空字串", None, {"digest": ""}),
        ("png 空", None, {"png": b"", "digest": hashlib.sha256(b"").hexdigest()}),
        ("mime 空字串", None, {"mime": ""}),
        ("bbox 只有 3 個座標", None, {"bbox": (60.0, 100.0, 520.0)}),
        ("bbox x0>x1", None, {"bbox": (520.0, 100.0, 60.0, 180.0)}),
    ]
)


@pytest.mark.smoke
@pytest.mark.parametrize("drop,overrides", [case[1:] for case in _MALFORMED_VARIANTS],
                         ids=[case[0] for case in _MALFORMED_VARIANTS])
def test_verifier_rag_and_writer_all_reject_the_same_malformed_variant(
    tmp_path: Path, monkeypatch, drop, overrides
):
    """★ 契約 §21.4:同一批 malformed Variant 同時送進 verifier / RAG / writer，三方都要 fail-loud。

    只打其中一端的測試已經連續四輪放過這條接縫了:被點名的那端收緊、另外兩端維持
    寬鬆，於是繞道永遠存在。verifier 那一端尤其要緊——它是**VL 呼叫之前**的檢查，
    等 RAG 或 writer 稍後才拒絕時，VL 的錢已經花掉了。
    """
    monkeypatch.setenv("AICODE_ROOT", str(tmp_path))
    pdf = _write_pdf(tmp_path)
    document_id = _document_id(pdf, tmp_path)
    fig_id = _figure_id(document_id, 1)
    fields = _variant_fields(figure_id=fig_id, **overrides)
    if drop:
        fields.pop(drop)
    variant = types.SimpleNamespace(**fields)
    candidate = types.SimpleNamespace(figure_id=fig_id, bbox=TABLE_BBOX, page=1,
                                      page_rect=PAGE_RECT, document_id=document_id)

    # 1. verifier：送模型之前的那道檢查
    with pytest.raises(figure_extract.FigureError):
        figure_verify._validate_variants(candidate, [variant])

    # 2. RAG：「這張是不是完整原圖」的閘
    with pytest.raises(figure_extract.FigureError):
        RAG._is_full_image(figure_extract, variant, candidate_bbox=candidate.bbox,
                           where=f"cross-module figure={fig_id}")

    # 3. writer：不得寫出任何 run 目錄，更不得發布 manifest
    figure = _result(document_id, fig_id, status=figure_extract.VERIF_UNVERIFIED,
                     variants=["crop@200dpi"], model_input_variant="crop@200dpi")
    with pytest.raises(figure_extract.FigureError):
        figure_review.write_run_artifacts(
            tmp_path, document_id=document_id, run_id=figure_review.new_run_id(),
            figures=[figure], variants=[variant], review_assets={})
    slug_dir = (tmp_path / ".codetrail" / "figures"
                / figure_extract.document_slug(document_id))
    assert list(slug_dir.rglob("manifest.json")) == [], "malformed variant 不得留下 manifest"


@pytest.mark.smoke
def test_a_local_crop_claiming_tile_total_one_is_not_the_full_image():
    """★ 合法 tile flags + 局部 bbox：門面與 RAG 都不得判成完整原圖（契約 §21.1）。

    只看 tile flags 的話，把第一片 tile 的 bytes 配上 `(tile_index=0, tile_total=1)`
    就能冒充完整原圖——覆核的人看到的是只裁到上緣的圖，卻以為那是整張。
    """
    where = "cross-module full-image gate"
    full = types.SimpleNamespace(**_variant_fields())
    local = types.SimpleNamespace(**_variant_fields(bbox=LOCAL_CROP_BBOX))

    assert figure_extract.is_full_image(full, candidate_bbox=TABLE_BBOX, where=where) is True
    assert figure_extract.is_full_image(local, candidate_bbox=TABLE_BBOX, where=where) is False
    # RAG 不得自己另有一套判定：同一批輸入必須與門面逐條相同
    assert RAG._is_full_image(figure_extract, full, candidate_bbox=TABLE_BBOX,
                              where=where) is True
    assert RAG._is_full_image(figure_extract, local, candidate_bbox=TABLE_BBOX,
                              where=where) is False


@pytest.mark.smoke
def test_a_local_crop_review_asset_never_publishes_a_successful_manifest(
    tmp_path: Path, monkeypatch
):
    """★ 契約 §21.4 的整合測試：tile flags 合法但 bbox 只是局部的覆核圖 → 零寫入。

    這是本輪 BLOCKER #1 的實測樣態：`renderer` 交出一張宣稱「未切片」的圖，bbox 卻
    只涵蓋候選框的上半。修正前 RAG 的閘只驗 tile flags，於是它被收成「完整原圖」、
    整份文件照樣發布成功 manifest——`review.md` 因此對「人到底看得到什麼」說謊。
    """
    kb_path = _kb_ready(monkeypatch, tmp_path)
    before = kb_path.read_bytes()
    pdf, harness, _fid = _simple_native_case(tmp_path, monkeypatch)
    monkeypatch.setattr(
        figure_extract, "render_candidate_variants",
        lambda _doc, cand: [types.SimpleNamespace(
            **_variant_fields(figure_id=cand.figure_id, bbox=LOCAL_CROP_BBOX))],
        raising=False)

    with pytest.raises(figure_extract.FigureExtractionError) as exc:
        RAG.add_document(str(pdf), str(kb_path))

    message = str(exc.value)
    # 訊息要指得出「拿到的框」與「候選框」，否則使用者只知道失敗、不知道差在哪
    assert str(tuple(LOCAL_CROP_BBOX)) in message, message
    assert str(tuple(TABLE_BBOX)) in message, message
    assert kb_path.read_bytes() == before
    successful = [kwargs for (_a, kwargs) in harness.write_artifacts.calls
                  if kwargs.get("failed") is False]
    assert successful == [], "局部 crop 不得冒充完整原圖發布成功 manifest"
