"""PDF ingest 全鏈:pymupdf4llm 契約、頁碼、內嵌圖轉 VL、以及 origin 溯源揭露。

併入 tests/test_rag_vl_provenance.py(2026-08-20):VL 來源標記是 ingest 的產物,
分兩個檔會讓「哪些 chunk 該有 origin」的契約被拆散。
"""
from __future__ import annotations

import hashlib
import json
import types
from pathlib import Path

import pytest

import knowledge
import RAG


class _FakePdfModule:
    """假 pymupdf4llm：to_markdown 直接回預先給定的 page dicts。"""

    def __init__(self, pages):
        self._pages = pages

    def to_markdown(self, *_args, **_kwargs):
        return self._pages


def _fake_pdf(monkeypatch, pages):
    monkeypatch.setattr(RAG, "check_pymupdf4llm", lambda: _FakePdfModule(pages))


# 覆核用影像 fixture 的 bytes；`digest` 一律由它算，不留空字串（契約 §21.3）。
_STUB_PNG = b"\x89PNG\r\n\x1a\nstub"


def _stub_open(monkeypatch):
    """替換 pymupdf 開檔：假 PDF bytes 也能走完 structured lane 的開檔前置。

    2026-08-30：legacy 圖面路徑（`_render_pdf_figure_png` / `_pdf_figure_chunks`）
    已移除，所以這裡不再需要假 renderer——沒被 structured lane 收錄的框現在是
    **缺席**，不會有人去 render 它。
    """
    monkeypatch.setattr(
        RAG, "_open_pdf_document",
        lambda _path: types.SimpleNamespace(page_count=9999, close=lambda: None),
    )


def _text_chunks(chunks):
    return [c for c in chunks if not c.get("origin")]


def _figure_chunks(chunks):
    return [c for c in chunks if c.get("origin")]


LONG_TEXT_A = "Chapter 1 Overview. The NPU has 8 compute cores and a shared 4MB SRAM block."
LONG_TEXT_B = "Chapter 2 Limits. Max tensor height and width is 4096 for conv2d inputs."

BIG_BBOX = (72.0, 110.0, 300.0, 340.0)     # 228x230pt：遠超門檻
BIG_BBOX_2 = (320.0, 110.0, 540.0, 400.0)  # 同頁第二張
TINY_BBOX = (500.0, 20.0, 512.0, 32.0)     # 12x12pt 圖示：低於門檻


# ============================================================
# 頁碼相容（原有回歸）
# ============================================================
def test_new_metadata_page_number_key(monkeypatch):
    """新版 key page_number（1-based）：頁碼必須正確，不可全部歸 1。"""
    _fake_pdf(monkeypatch, [
        {"metadata": {"page_number": 1}, "text": LONG_TEXT_A, "page_boxes": []},
        {"metadata": {"page_number": 2}, "text": "",
         "page_boxes": [{"class": "picture", "bbox": BIG_BBOX}]},
        {"metadata": {"page_number": 3}, "text": LONG_TEXT_B,
         "page_boxes": [{"class": "text"}, {"class": "picture", "bbox": BIG_BBOX}]},
    ])
    _stub_open(monkeypatch)

    chunks = RAG.extract_pdf("fake_spec.pdf")

    text = _text_chunks(chunks)
    assert text, "文字頁應產出 chunk"
    assert {c["page"] for c in text} == {1, 3}, (
        f"頁碼應反映實體頁，實際: {sorted({c['page'] for c in text})}"
    )
    # 2026-08-30：沒被 structured lane 收錄的 picture 框是**缺席**，不再產生 chunk。
    assert not _figure_chunks(chunks)


def test_legacy_metadata_page_key_still_works(monkeypatch):
    """舊版 key page（0-based）：fallback 路徑 +1 後仍正確。"""
    _fake_pdf(monkeypatch, [
        {"metadata": {"page": 0}, "text": LONG_TEXT_A},
        {"metadata": {"page": 2}, "text": LONG_TEXT_B},
    ])

    chunks = RAG.extract_pdf("fake_spec.pdf")

    assert {c["page"] for c in chunks} == {1, 3}


# ============================================================
# 內嵌圖：structured lane 沒收就是缺席
# ============================================================
def test_text_only_pdf_stays_silent(monkeypatch, capsys):
    """純文字 PDF：零 figure chunk、零缺席提示、文字 chunk 照舊。"""
    _fake_pdf(monkeypatch, [
        {"metadata": {"page_number": 1}, "text": LONG_TEXT_A,
         "page_boxes": [{"class": "text"}]},
        {"metadata": {"page_number": 2}, "text": LONG_TEXT_B, "page_boxes": []},
    ])

    chunks = RAG.extract_pdf("fake.pdf")

    out = capsys.readouterr().out
    assert chunks and not _figure_chunks(chunks)
    assert "內嵌圖" not in out
    assert "沒有進知識庫" not in out, "沒有圖的 PDF 不得講缺席（罐頭提示）"


def test_mixed_pdf_text_chunks_unchanged_and_pictures_are_absent(monkeypatch, capsys):
    """混合 PDF：文字 chunk 與純文字路徑逐位元組相同；picture 框改成列帳缺席。"""
    def _pages(with_pics: bool):
        boxes = [{"class": "picture", "bbox": BIG_BBOX}] if with_pics else []
        return [
            {"metadata": {"page_number": 1}, "text": LONG_TEXT_A, "page_boxes": []},
            {"metadata": {"page_number": 2}, "text": LONG_TEXT_B, "page_boxes": boxes},
        ]

    _fake_pdf(monkeypatch, _pages(False))
    baseline = RAG.extract_pdf("fake_spec.pdf")

    _fake_pdf(monkeypatch, _pages(True))
    _stub_open(monkeypatch)
    document = RAG.extract_pdf_document("fake_spec.pdf")

    text = _text_chunks(document.chunks)
    assert [c["content"] for c in text] == [c["content"] for c in baseline]
    assert [c["page"] for c in text] == [c["page"] for c in baseline]
    assert not _figure_chunks(document.chunks), "沒有自由文字 lane 了"
    absent = getattr(document, RAG._ABSENT_ATTR, None)
    assert any(item["page"] == 2 for item in absent or []), absent
    assert "沒有進知識庫" in capsys.readouterr().out


# ============================================================
# hard fail 與 per-doc 原子性
# ============================================================
def test_empty_vl_description_is_hard_fail(monkeypatch):
    """VL 回空/純空白：嚴格核心要 raise，不得產生空 chunk。"""
    monkeypatch.setattr(
        RAG.llama_client, "vision_completion", lambda **_kw: "   \n  "
    )
    with pytest.raises(RuntimeError, match="空內容"):
        RAG._describe_technical_image_base64("aGk=", "image/png")


# ============================================================
# 整合：真 pymupdf4llm 的 page dict
# ============================================================
def test_real_pymupdf4llm_contract(tmp_path: Path, capsys, monkeypatch):
    """整合測試：真 pymupdf4llm 的 page dict 必須撐起頁碼與內嵌圖偵測。

    這是抓「上游又改 schema」的活網——當年 page → page_number 改名讓所有
    chunk 歸 1 頁；現在 page_boxes 的 class/bbox 再變動，會直接讓內嵌圖
    偵測破掉（症狀從「多一張圖」變成「缺席帳少一筆」，一樣是無聲的）。
    """
    fitz = pytest.importorskip("fitz", reason="需要 PyMuPDF（pymupdf4llm 相依）")
    pytest.importorskip("pymupdf4llm", reason="PDF ingestion 需要 pymupdf4llm")

    pdf = tmp_path / "mixed_spec.pdf"
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), LONG_TEXT_A)
    p2 = doc.new_page()  # 純圖頁（掃描頁）：整頁 render
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 64, 64))
    pix.clear_with(120)
    p2.insert_image(fitz.Rect(72, 72, 400, 400), pixmap=pix)
    p3 = doc.new_page()  # 文字 + 大圖 + 過小圖示
    p3.insert_text((72, 72), LONG_TEXT_B)
    p3.insert_image(fitz.Rect(*BIG_BBOX), pixmap=pix)
    p3.insert_image(fitz.Rect(*TINY_BBOX), pixmap=pix)
    pix2 = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 64, 64))
    pix2.clear_with(220)  # 與 p3 的圖不同內容，才不會被 p3 先去重掉
    for _ in range(2):  # p4/p5：同一張圖同位置 → 內容 hash 去重
        page = doc.new_page()
        page.insert_text((72, 72), LONG_TEXT_A)
        page.insert_image(fitz.Rect(*BIG_BBOX), pixmap=pix2)
    doc.save(str(pdf))
    doc.close()

    document = RAG.extract_pdf_document(str(pdf))

    out = capsys.readouterr().out
    text = _text_chunks(document.chunks)
    assert {c["page"] for c in text} == {1, 3, 4, 5}, (
        "頁碼歸 1 → pymupdf4llm 的 metadata key 又變了，去修 _pdf_page_number"
    )
    assert not _figure_chunks(document.chunks), (
        "tmp_path 不在專案根內 → structured lane 不啟動 → 一張圖都不該入庫")
    # 圖仍要被**看見**：偵測不到 picture box 的話缺席帳會少一筆，而那是無聲的。
    absent = getattr(document, RAG._ABSENT_ATTR, None) or []
    assert {item["page"] for item in absent} == {2, 3, 4, 5}, (
        f"內嵌圖偵測結果不對（實際列帳頁碼 {sorted({i['page'] for i in absent})}）；"
        "偵測不到圖表示 page_boxes schema 又變了")
    assert all(str(item["reason"]).startswith("structured_lane_inactive")
               for item in absent), absent
    assert "沒有進知識庫" in out
    assert "[WARN]" not in out


# ============================================================
# 2026-08-14 GPT review 第二輪：check_pymupdf4llm 的 config fallback
# 曾退回「只驗 import」——假 999.0.0 module 也會被放行。修正後
# fail closed：config 缺失直接退出；版本不符也退出。
# 這兩個測試不依賴真的 pymupdf4llm 套件（乾淨環境也會執行）。
# ============================================================
def _fake_pymupdf4llm(monkeypatch, version: str):
    import importlib.metadata as _md
    import sys as _sys
    import types

    fake = types.ModuleType("pymupdf4llm")
    fake.__version__ = version
    monkeypatch.setitem(_sys.modules, "pymupdf4llm", fake)

    def _no_dist(_name):
        raise _md.PackageNotFoundError(_name)

    # 逼 require_pymupdf4llm 走 __version__ fallback，脫離本機真實安裝狀態
    monkeypatch.setattr(_md, "version", _no_dist)
    return fake


def test_check_pymupdf4llm_rejects_wrong_version(monkeypatch, capsys):
    _fake_pymupdf4llm(monkeypatch, "999.0.0")

    with pytest.raises(SystemExit) as exc:
        RAG.check_pymupdf4llm()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "版本不符" in out and "999.0.0" in out, out


def test_check_pymupdf4llm_accepts_pinned_version(monkeypatch):
    import config

    fake = _fake_pymupdf4llm(monkeypatch, config.PYMUPDF4LLM_PIN)

    assert RAG.check_pymupdf4llm() is fake


# --------------------------------------------------------------------------
# 併自 tests/test_rag_vl_provenance.py:VL/diagram chunk 的 origin 揭露。
# --------------------------------------------------------------------------
def test_detect_content_type_keeps_diagram_despite_warning_keywords():
    content = "注意：必須遵守記憶體對齊限制，禁止跨 4KB 邊界存取。"
    assert RAG.detect_content_type(content, "diagram") == "diagram"


def test_detect_content_type_keeps_chat_despite_warning_keywords():
    assert RAG.detect_content_type("WARNING: do not do this", "chat") == "chat"


def test_detect_content_type_still_upgrades_text_sources():
    """文字抽取來源（doc/spec...）維持原本的 warning 升級行為。"""
    assert RAG.detect_content_type("CAUTION: 必須先斷電", "doc") == "warning"
    assert RAG.detect_content_type("一般說明文字，介紹模組用途。", "doc") == "doc"


# ------------------------------------------------------------------
# KB.query：origin 揭露
# ------------------------------------------------------------------

VL_CONTENT = (
    "NPU 共有 8 個運算核心，最大張量高寬為 4096，內部共享 SRAM 容量為 4MB，"
    "本段描述由視覺模型辨識架構圖產生，用於出身鏈揭露測試。"
)
TEXT_CONTENT = (
    "根據規格書第三章，conv2d 輸入張量的高與寬上限皆為 4096，"
    "超過時驅動程式會回傳 E_TENSOR_TOO_LARGE 錯誤碼。"
)


def _kb_with(monkeypatch, tmp_path: Path, chunk: dict) -> knowledge.KnowledgeBase:
    kb = knowledge.KnowledgeBase(str(tmp_path / "missing.json"))
    kb.loaded = True
    kb.chunks = [chunk]
    kb.documents = [chunk["source"]]
    monkeypatch.setattr(
        kb, "_hybrid_search",
        lambda *_a, **_k: [knowledge.Candidate(
            chunk_idx=0, chunk=chunk, rrf_score=0.5,
            retrieval_score=0.9, gate_score=0.9,
        )],
    )
    monkeypatch.setattr(
        kb, "_rerank_with_model",
        lambda _q, candidates, _top_k, **_kw: [(None, c.chunk) for c in candidates],
    )
    monkeypatch.setattr(kb, "_get_embedding", lambda _t: [0.0, 1.0])
    monkeypatch.setattr(knowledge, "USE_MMR", False)
    return kb


def test_query_exposes_vl_origin(monkeypatch, tmp_path: Path):
    vl_chunk = {
        "id": "img1",
        "source": "image_arch.png",
        "page": 1,
        "chunk_index": 0,
        "type": "diagram",
        "section": "",
        "content": VL_CONTENT,
        "embedding": [0.0, 1.0],
        "origin": "image",
    }
    kb = _kb_with(monkeypatch, tmp_path, vl_chunk)

    model_text, display, meta = kb.query("NPU 有幾個核心？")

    assert meta["has_ref"] is True
    # REF 區塊要標 origin（VL）
    assert "origin:" in model_text and "VL" in model_text, model_text
    # 要附 VL 信任等級提示
    assert "※ origin 標註 VL" in model_text, model_text
    # refs 清單帶 origin（下游 data_flywheel / eval 用）
    assert meta["refs"][0]["origin"] == "image"
    # display 給人看的摘要要有 ·VL 標記
    assert "·VL" in display, display


def test_query_text_chunk_has_no_vl_markers(monkeypatch, tmp_path: Path):
    text_chunk = {
        "id": "txt1",
        "source": "npu_spec.pdf",
        "page": 3,
        "chunk_index": 0,
        "type": "spec",
        "section": "3.2",
        "content": TEXT_CONTENT,
        "embedding": [0.0, 1.0],
    }
    kb = _kb_with(monkeypatch, tmp_path, text_chunk)

    model_text, display, meta = kb.query("conv2d 張量上限是多少？")

    assert meta["has_ref"] is True
    assert "origin:" not in model_text, model_text
    assert "※ origin 標註 VL" not in model_text
    assert meta["refs"][0]["origin"] == ""
    assert "·VL" not in display


def test_query_exposes_pdf_figure_index(monkeypatch, tmp_path: Path):
    """PDF 內嵌圖（origin=diagram）：REF 文字與 structured refs 都要帶 figure。

    同頁多張圖若 VL 抽出的標題相同，少了 figure_index 下游（MCP / strict /
    data_flywheel / eval）就分不出引用的是哪一張。
    """
    figure_chunk = {
        "id": "fig1",
        "source": "npu_spec.pdf",
        "page": 7,
        "chunk_index": 3,
        "type": "diagram",
        "section": "架構圖",
        "content": VL_CONTENT,
        "embedding": [0.0, 1.0],
        "origin": "diagram",
        "figure_index": 2,
    }
    kb = _kb_with(monkeypatch, tmp_path, figure_chunk)

    model_text, display, meta = kb.query("NPU 有幾個核心？")

    assert "figure: 2" in model_text, model_text
    # diagram 也算 VL 產物：出身揭露與 image/screenshot 同級
    assert "origin:" in model_text and "VL" in model_text, model_text
    assert "※ origin 標註 VL" in model_text, model_text
    assert "·VL" in display, display
    assert meta["refs"][0]["origin"] == "diagram"
    assert meta["refs"][0]["figure_index"] == 2


def test_text_refs_carry_null_figure_index(monkeypatch, tmp_path: Path):
    """純文字 chunk 的 figure_index 是 None，且 REF 文字不得出現 figure 行。"""
    text_chunk = {
        "id": "txt1",
        "source": "npu_spec.pdf",
        "page": 3,
        "chunk_index": 0,
        "type": "spec",
        "section": "3.2",
        "content": TEXT_CONTENT,
        "embedding": [0.0, 1.0],
    }
    kb = _kb_with(monkeypatch, tmp_path, text_chunk)

    model_text, _display, meta = kb.query("conv2d 張量上限是多少？")

    assert "figure:" not in model_text, model_text
    assert meta["refs"][0]["figure_index"] is None


def test_merge_never_mixes_text_and_figures(tmp_path: Path):
    """同一份 PDF 的文字 chunk、figure 1、figure 2 三者互不合併。

    PDF 內嵌圖入庫後，同一 source+page 底下同時有原文與多張 VL 描述，
    chunk_index 又是連號——只用 (source, page) 當合併 key 會把 VL 描述
    黏進原文（origin 被首個成員蓋掉，出身揭露整個消失），或把兩張不同
    的圖混成一段。
    """
    kb = knowledge.KnowledgeBase(str(tmp_path / "missing.json"))

    def _c(idx: int, origin: str = "", figure: int | None = None) -> dict:
        chunk = {
            "source": "npu_spec.pdf",
            "page": 3,
            "chunk_index": idx,
            "type": "diagram" if origin else "spec",
            "section": "",
            "content": f"segment {idx} " + "x" * 60,
            "embedding": [0.0, 1.0],
        }
        if origin:
            chunk["origin"] = origin
            chunk["figure_index"] = figure
        return chunk

    merged = kb._merge_adjacent_chunks([
        _c(0), _c(1),                              # 原文（可互相合併）
        _c(2, "diagram", 1), _c(3, "diagram", 1),  # 第 1 張圖的兩段描述
        _c(4, "diagram", 2),                       # 第 2 張圖
    ])

    assert len(merged) == 3, [
        (m.get("origin"), m.get("figure_index")) for m in merged
    ]
    text_group, fig1, fig2 = merged
    assert text_group.get("origin", "") == "" and "segment 1" in text_group["content"]
    assert "segment 2" not in text_group["content"], "VL 描述不得併進原文"
    assert fig1["origin"] == "diagram" and fig1["figure_index"] == 1
    assert "segment 3" in fig1["content"], "同一張圖的相鄰描述仍應合併"
    assert fig2["figure_index"] == 2 and "segment 4" in fig2["content"]
    assert "segment 3" not in fig2["content"], "不同 figure 不得混成一段"


def test_merge_adjacent_chunks_preserves_origin(tmp_path: Path):
    """_merge_adjacent_chunks 重建 dict 時不可弄丟 origin（否則 REF 標記消失）。"""
    kb = knowledge.KnowledgeBase(str(tmp_path / "missing.json"))

    def _c(idx: int) -> dict:
        return {
            "source": "image_arch.png",
            "page": 1,
            "chunk_index": idx,
            "type": "diagram",
            "section": "",
            "content": f"segment {idx} " + "x" * 60,
            "embedding": [0.0, 1.0],
            "origin": "image",
        }

    merged = kb._merge_adjacent_chunks([_c(0), _c(1)])

    assert len(merged) == 1, "相鄰 chunk 應合併"
    assert merged[0]["origin"] == "image"


# ==========================================================================
# 2026-08-22:legacy 圖面路徑與新的 structured lane 共存（T7）。
#
# 兩條 lane 的邊界只有兩種壞法，而且都是靜默的:
#   (a) 同頁的 chunk_index / figure_index 互踩 → chunk id 撞、REF 指錯圖;
#   (b) 同一個框被兩條 lane 各收一次 → KB 內同一張圖兩份互相競爭的版本。
# 這兩條就是守它們。structured lane 自己的契約在 tests/test_figure_ingest.py。
# ==========================================================================
def _structured_lane_stub(monkeypatch, tmp_path: Path, *, page: int, bbox, native_pos,
                          page_text: str, table_md: str):
    """把 figure 門面換成固定的一張 native table 結果（不碰 T3/T4 的真實實作）。"""
    import dataclasses

    figure_extract = pytest.importorskip("figure_extract")

    @dataclasses.dataclass(frozen=True)
    class _Cand:
        index: int
        page: int
        bbox: tuple
        page_rect: tuple
        kind_scores: dict
        kind: str
        signals: dict
        reasons: list
        signature: str
        native_table: dict
        occurrences: list
        asset_xref: object
        asset_digest: str
        figure_id: str
        document_id: str

    @dataclasses.dataclass(frozen=True)
    class _Evidence:
        page: int
        raw_markdown: str
        page_boxes: list

    @dataclasses.dataclass(frozen=True)
    class _Plan:
        document_id: str
        candidates: list
        page_evidence: dict
        stats: dict
        preflight: dict
        over_budget: list

    @dataclasses.dataclass(frozen=True)
    class _Result:
        figure_id: str
        document_id: str
        page: int
        figure_index: int
        bbox: tuple
        kind: str
        revision: int
        payload: dict
        extraction_status: str
        verification_status: str
        reasons: list
        reason_details: list
        evidence: dict
        occurrences: list
        model_input_variant: str
        variants: list
        row_total: int
        line_total: object

    pdf = tmp_path / "mixed_lane_spec.pdf"
    pdf.write_bytes(b"%PDF-fake-two-lanes")
    monkeypatch.setenv("AICODE_ROOT", str(tmp_path))
    page_rect = (0.0, 0.0, 595.0, 842.0)
    document_id = figure_extract.document_id_for(pdf, tmp_path)
    figure_id = figure_extract.figure_id_for(document_id, page, bbox, page_rect, "asset")
    occurrences = [{"page": page, "bbox": list(bbox), "index": 0}]
    boxes = [{"index": 0, "class": "table", "bbox": bbox, "pos": native_pos}]
    columns = [{"column_id": "c1", "label": "Reg", "role": None},
               {"column_id": "c2", "label": "Addr", "role": None}]
    payload = {
        "kind": figure_extract.KIND_TABLE,
        "columns": columns,
        "rows": [{"row_index": 1, "cells": [
            {"column_id": "c1", "text": "CTRL9", "state": "observed", "inherited_from_row": None},
            {"column_id": "c2", "text": "0x9000_0000", "state": "observed",
             "inherited_from_row": None}]}],
        "footnotes": [],
    }
    candidate = _Cand(
        index=1, page=page, bbox=bbox, page_rect=page_rect,
        kind_scores={"table": 1.0}, kind=figure_extract.KIND_TABLE,
        # native_lane 是 lane 判定的唯一真相(契約 §15.1):有 native_table 的 table
        # 候選走零 VL 的原生路徑。缺這個 key 會被 RAG._candidate_native_lane fail-loud。
        signals={"native_lane": True}, reasons=[],
        signature="sig",
        native_table={"pos": native_pos, "markdown": table_md, "geometry": {},
                      "strategy": "lines"},
        occurrences=occurrences, asset_xref=None, asset_digest="asset",
        figure_id=figure_id, document_id=document_id)
    plan = _Plan(document_id=document_id, candidates=[candidate],
                 page_evidence={page: _Evidence(page=page, raw_markdown=page_text,
                                                page_boxes=boxes)},
                 stats={}, preflight={"candidates": 1, "tiles": 0, "vl_calls_min": 0,
                                      "vl_calls_max": 0, "image_tokens_est": 0,
                                      "pages": 1, "native_tables": 1},
                 over_budget=[])
    result = _Result(
        figure_id=figure_id, document_id=document_id, page=page, figure_index=1, bbox=bbox,
        kind=figure_extract.KIND_TABLE, revision=1, payload=payload,
        extraction_status=figure_extract.EXTRACTION_COMPLETE,
        verification_status=figure_extract.VERIF_NATIVE, reasons=[], reason_details=[],
        # native lane 零 VL、沒有模型影像輸入 → variants 必須是空的（契約 §6.4 / §15.6）
        # 契約 §19.4：可信狀態不得配空 evidence（真 producer 會填 channels）
        evidence={"channels": ["markdown_pos"],
                  "cells": {"r1c1": {"anchor": "markdown_pos", "matched": True,
                                     "raw": "CTRL9"},
                            "r1c2": {"anchor": "markdown_pos", "matched": True,
                                     "raw": "0x9000_0000"}},
                  "lines": {},
                  "unlocatable_tokens": [], "anchor_coverage": {}, "row_alignment": {},
                  "line_alignment": {}, "stitch": {}},
        occurrences=occurrences, model_input_variant="native",
        variants=[], row_total=1, line_total=None)

    def _set(name, value):
        monkeypatch.setattr(figure_extract, name, value, raising=False)

    _set("plan_document_figures", lambda *a, **k: plan)
    _set("check_preflight", lambda *a, **k: None)
    _set("format_preflight_report", lambda *a, **k: "[PREFLIGHT] stub")
    _set("ensure_capability", lambda *a, **k: None)
    # 每張進 KB 的 figure 都要留得下**完整未切片**的覆核用影像（Go/No-Go 5）。
    # 這裡刻意填齊契約 §6.3 凍結的每一個 `Variant` 欄位:少填 tile metadata 的話，
    # 「未切片」會變成兩邊各自猜出來的預設值，而不是這個 fixture 真的宣告的事實。
    # `digest` 同理，要是**真的** sha256（契約 §21.3）:共用 validator 會拿它與 png
    # 的實際內容核對，留空等於讓 fixture 宣稱一個產線上根本產不出來的 Variant。
    _set("render_candidate_variants",
         lambda _doc, cand: [types.SimpleNamespace(
             figure_id=cand.figure_id, variant_id="crop@200dpi",
             png=_STUB_PNG, width=460, height=80, bbox=tuple(cand.bbox),
             tile_index=0, tile_total=1, overlap_px=0, est_image_tokens=64,
             digest=hashlib.sha256(_STUB_PNG).hexdigest(), mime="image/png")])
    _set("extract_document_figures", lambda *a, **k: [result])
    # artifact 這一段刻意**不 stub**:native lane 的 `variants=[]`／`model_input_variant`
    # 與 review_assets 的分離都是跨模組契約，writer 被換掉的話矛盾永遠不會浮出來
    # （契約 §18.4）。`new_run_id` / `evidence_ref_for` / `write_run_artifacts` /
    # `list_figures` 全部走真貨，只有偵測與抽取是替身。
    _set("prune_old_runs", lambda *a, **k: None)
    return pdf, figure_id


TABLE_MD_LANE = "|Reg|Addr|\n|---|---|\n|CTRL9|0x9000_0000|\n"
INTRO_LANE = "Lane coexistence page intro paragraph. \n\n"
PAGE_LANE = INTRO_LANE + TABLE_MD_LANE + "\n\nTail paragraph. \n\n"
POS_LANE = (len(INTRO_LANE), len(INTRO_LANE) + len(TABLE_MD_LANE) + 2)
TABLE_BBOX_LANE = (60.0, 400.0, 520.0, 470.0)


# ============================================================
# 2026-08-30 旋轉頁：pymupdf4llm 的 markdown 恆為空，正文不得無聲消失
# ============================================================
ROTATED_BODY = "CTRL0 register controls the clock gate for the NPU compute cores."


def _rotated_two_page_pdf(path: Path) -> None:
    """兩頁、同一段正文，第二頁 /Rotate 90（釘版 1.28.0 的 markdown 恆為空）。"""
    fitz = pytest.importorskip("fitz", reason="需要 PyMuPDF（pymupdf4llm 相依）")
    doc = fitz.open()
    for _ in range(2):
        page = doc.new_page()
        page.insert_text((72, 100), ROTATED_BODY, fontsize=11)
    doc[1].set_rotation(90)
    doc.save(str(path))
    doc.close()


@pytest.mark.smoke
def test_rotated_page_body_text_is_not_silently_dropped(tmp_path: Path):
    """★ 旋轉頁的正文必須入庫。

    釘版 pymupdf4llm 1.28.0 對 `rotation != 0` 的頁 `to_markdown(page_chunks=True)`
    回空字串（實測；同頁 `page.get_text()` 有全文）。舊碼在 `if not content: continue`
    直接跳過那一頁——零 chunk、零 WARN、摘要零提示，而橫放的 register map / 大表頁
    正是 datasheet 最常旋轉的頁。
    """
    pytest.importorskip("pymupdf4llm", reason="PDF ingestion 需要 pymupdf4llm")
    pdf = tmp_path / "rotated_spec.pdf"
    _rotated_two_page_pdf(pdf)

    chunks = RAG.extract_pdf(str(pdf))

    pages = {c["page"] for c in chunks}
    assert pages == {1, 2}, f"旋轉頁的正文整頁消失了，實際頁碼: {sorted(pages)}"
    page2 = [c for c in chunks if c["page"] == 2]
    assert any("CTRL0" in c["content"] for c in page2), (
        f"第 2 頁有 chunk 但抽不到正文: {[c['content'][:40] for c in page2]}")


# ============================================================
# 2026-08-30 structured lane 是唯一的圖面 lane：沒收就是缺席，且要列帳
# ============================================================
@pytest.mark.smoke
def test_structured_lane_absence_is_reported_not_described(tmp_path: Path, capsys):
    """★ structured lane 收不到的圖 = 缺席，不再退回自由文字描述。

    舊碼在這裡會走 legacy lane：`class=picture` 的框直接送 VL 產生
    `origin="diagram"` 的自由文字 chunk——沒有 ▯、沒有 review artifact、沒有
    evidence_ref，事後從 KB 與摘要都看不出那段是「看圖說故事」。現在那一頁必須
    (1) 零 figure chunk，(2) 在 ingest 的輸出與摘要 payload 裡列出頁碼與原因。
    """
    fitz = pytest.importorskip("fitz", reason="需要 PyMuPDF（pymupdf4llm 相依）")
    pytest.importorskip("pymupdf4llm", reason="PDF ingestion 需要 pymupdf4llm")
    pdf = tmp_path / "picture_spec.pdf"            # tmp_path 不在專案根內 → lane 不啟動
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), LONG_TEXT_A)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 64, 64))
    pix.clear_with(120)
    page.insert_image(fitz.Rect(*BIG_BBOX), pixmap=pix)
    doc.save(str(pdf))
    doc.close()

    document = RAG.extract_pdf_document(str(pdf))

    out = capsys.readouterr().out
    assert not [c for c in document.chunks if c.get("origin") == "diagram"], (
        "legacy 自由文字 lane 已移除，不得再產生 origin=diagram 的 chunk")
    absent = getattr(document, RAG._ABSENT_ATTR, None)
    assert absent, "圖沒進 KB 卻沒有任何缺席紀錄＝無聲漏圖"
    assert any(item["page"] == 1 and
               str(item["reason"]).startswith("structured_lane_inactive")
               for item in absent), absent
    assert "沒有進知識庫" in out, out


@pytest.mark.smoke
def test_text_only_pdf_reports_no_absence(tmp_path: Path, capsys):
    """純文字 PDF 一句缺席都不准講——罐頭提示會讓使用者學會跳過整段。"""
    fitz = pytest.importorskip("fitz", reason="需要 PyMuPDF（pymupdf4llm 相依）")
    pytest.importorskip("pymupdf4llm", reason="PDF ingestion 需要 pymupdf4llm")
    pdf = tmp_path / "text_only.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), LONG_TEXT_A)
    doc.save(str(pdf))
    doc.close()

    document = RAG.extract_pdf_document(str(pdf))

    assert getattr(document, RAG._ABSENT_ATTR, None) == []
    assert "沒有進知識庫" not in capsys.readouterr().out


# ============================================================
# 2026-08-30 figure chunk 的 caption 與章節；marker 在查詢期解得回來
# ============================================================
CAPTION_LANE = "Table 3-1 Register map for the NPU control block\n\n"
SECTION_LANE = "## 3.2 Registers\n\n"
PAGE_CAPTIONED = (SECTION_LANE + INTRO_LANE + CAPTION_LANE + TABLE_MD_LANE
                  + "\n\nTail paragraph. \n\n")
POS_CAPTIONED = (len(SECTION_LANE) + len(INTRO_LANE) + len(CAPTION_LANE),
                 len(SECTION_LANE) + len(INTRO_LANE) + len(CAPTION_LANE)
                 + len(TABLE_MD_LANE) + 2)


@pytest.mark.smoke
def test_structured_figure_chunk_carries_caption_and_section(monkeypatch, tmp_path: Path):
    """★ figure chunk 要帶 caption 與所在章節，而且只當檢索訊號。

    caption（`Table 3-1 …`）留在鄰近的文字 chunk、figure chunk 的 section 固定空字串
    時，以表名提問命中的是那個只剩 marker 的文字 chunk，不是 figure chunk——圖的內容
    與圖的名字在檢索面是斷開的。
    """
    import context_signals

    pdf, figure_id = _structured_lane_stub(
        monkeypatch, tmp_path, page=1, bbox=TABLE_BBOX_LANE, native_pos=POS_CAPTIONED,
        page_text=PAGE_CAPTIONED, table_md=TABLE_MD_LANE)
    _fake_pdf(monkeypatch, [{"metadata": {"page_number": 1}, "text": PAGE_CAPTIONED,
                             "page_boxes": []}])
    _stub_open(monkeypatch)

    chunks = RAG.extract_pdf_document(str(pdf), root=str(tmp_path)).chunks

    figure_chunks = [c for c in chunks if c.get("figure_id") == figure_id]
    assert figure_chunks, [c.get("origin") for c in chunks]
    chunk = figure_chunks[0]
    assert chunk["figure_caption"] == "Table 3-1 Register map for the NPU control block", chunk
    assert chunk["section"] == "3.2 Registers", chunk["section"]
    # 只進檢索訊號：payload 的衍生文字（evidence）一個字都不含 caption
    assert "Register map" not in chunk["content"], chunk["content"]
    assert "Table 3-1" in context_signals.bm25_document_text(chunk, use_ctx=False)
    assert "Table 3-1" in context_signals.gate_embedding_input(chunk)


@pytest.mark.smoke
def test_replaced_table_marker_resolves_to_the_figure_chunk(monkeypatch, tmp_path: Path):
    """★ 文字 chunk 裡的取代 marker，查詢期要跟得回去那張 figure chunk。

    `PDF_TABLE_REPLACED_MARKER` 以前只寫不讀：marker 指名的 figure_id 在查詢期沒有
    任何 consumer，所以命中帶 marker 的文字 chunk 時，使用者只拿得到一個頁碼。
    """
    figure_id = "fig_0123456789abcdef"
    text_chunk = {
        "id": "t1", "source": "spec.pdf", "page": 3, "chunk_index": 0, "type": "spec",
        "section": "3.2 Registers", "embedding": [0.0, 1.0],
        "content": "控制暫存器的定義見下表。" + RAG.PDF_TABLE_REPLACED_MARKER.format(
            figure_id=figure_id, page=3, rows=2),
    }
    figure_chunk = {
        "id": "f1", "source": "spec.pdf", "page": 3, "chunk_index": 1, "type": "spec",
        "section": "3.2 Registers", "embedding": [0.0, 1.0],
        "content": f"[FIGURE kind=table id={figure_id} rev=1 page=3 rows=1-2/2 "
                   "status=native_verified]\n| Reg | Addr |\n| --- | --- |\n"
                   "| CTRL9 | 0x9000_0000 |",
        "structured": True, "origin": "figure_table", "figure_kind": "table",
        "figure_id": figure_id, "revision": 1, "figure_index": 1,
        "verification_status": "native_verified", "extraction_status": "complete",
        "evidence_ref": ".codetrail/figures/x/run/manifest.json",
    }
    kb = knowledge.KnowledgeBase(str(tmp_path / "missing.json"))
    kb.loaded = True
    kb.chunks = [text_chunk, figure_chunk]
    kb.documents = ["spec.pdf"]
    kb._index_chunks()
    monkeypatch.setattr(
        kb, "_hybrid_search",
        lambda *_a, **_k: [knowledge.Candidate(
            chunk_idx=0, chunk=text_chunk, rrf_score=0.5,
            retrieval_score=0.9, gate_score=0.9)],
    )
    monkeypatch.setattr(
        kb, "_rerank_with_model",
        lambda _q, candidates, _top_k, **_kw: [(None, c.chunk) for c in candidates],
    )
    monkeypatch.setattr(kb, "_get_embedding", lambda _t: [0.0, 1.0])
    monkeypatch.setattr(knowledge, "USE_MMR", False)

    model_text, _display, meta = kb.query("CTRL9 的位址是多少？")

    assert "0x9000_0000" in model_text, model_text
    assert any(ref.get("figure_id") == figure_id for ref in meta["refs"]), meta["refs"]


@pytest.mark.smoke
def test_marker_pattern_matches_the_real_marker_format():
    """knowledge.py 的 marker pattern 與 RAG 的 marker 字面必須同步。

    兩邊刻意不共用常數（knowledge.py 在 MCP 啟動熱路徑上，不能 import RAG），
    所以漂移是無聲的：marker 照樣寫進 KB，查詢期卻再也解不回那張 figure chunk。
    """
    marker = RAG.PDF_TABLE_REPLACED_MARKER.format(
        figure_id="fig_0123456789abcdef", page=7, rows=42)
    match = knowledge._REPLACED_FIGURE_MARKER_RE.search(marker)

    assert match is not None, marker
    assert match.group(1) == "fig_0123456789abcdef"
    assert match.group(2) == "7" and match.group(3) == "42"


@pytest.mark.smoke
def test_figure_on_a_textless_page_does_not_inherit_the_first_section():
    """★ 沒有文字 span 的頁（純圖片頁）不得把 figure 掛到文件開頭那一節。

    `page_spans` 只收「產出過文字」的頁，所以純圖片頁查不到 span。退回 offset 0 的話，
    只要第一節從 0 開始，那一頁所有 figure 都會被標成第一節——一個看起來完全正常、
    但指錯章節的檢索訊號。
    """
    import types

    from extracted_document import ExtractedDocument, Section

    raw = "# 1 Overview\n\nintro text\n\n# 7 Appendix\n\ntail text"
    document = ExtractedDocument(
        raw_text=raw,
        sections=[Section(title="1 Overview", level=1, char_span=(0, 25)),
                  Section(title="7 Appendix", level=1, char_span=(25, len(raw)))],
        chunks=[],
        source="spec.pdf",
        # 第 1、2 頁有文字（分屬兩節），第 3 頁是純圖片頁（沒有 span）
        page_spans=[(1, 0, 25), (2, 25, len(raw))],
    )
    figure = types.SimpleNamespace(page=3, bbox=(10.0, 20.0, 300.0, 400.0),
                                   kind="diagram", figure_id="fig_00000000000000ff")

    context = RAG._figure_retrieval_context(document, [figure])["fig_00000000000000ff"]

    assert context["caption"] == ""
    assert context["section"] != "1 Overview", (
        "退回 offset 0 會讓純圖片頁的 figure 全部掛到文件開頭那一節")
    assert context["section"] in ("", "7 Appendix"), (
        f"沒有 span 的頁只能留空或沿用前一頁所在章節，實際 {context['section']!r}")
