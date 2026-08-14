"""extract_pdf 的頁碼與內嵌圖 fail-loud 回歸測試。

背景（2026-08-14 review）：
1. pymupdf4llm 新版（>=1.x）page metadata key 從 `page`（0-based）改名
   `page_number`（1-based）。舊碼硬讀 `page` → 所有 chunk 都變第 1 頁，
   REF 引用「第 X 頁」全錯。
2. 混合 PDF（文字+內嵌圖）走純文字抽取，圖片無聲消失且回報成功——
   與 fail-loud 原則不一致。新版要印 [WARN]（經 ingest_document 轉回給使用者）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import RAG


class _FakePdfModule:
    """假 pymupdf4llm：to_markdown 直接回預先給定的 page dicts。"""

    def __init__(self, pages):
        self._pages = pages

    def to_markdown(self, *_args, **_kwargs):
        return self._pages


def _fake_pdf(monkeypatch, pages):
    monkeypatch.setattr(RAG, "check_pymupdf4llm", lambda: _FakePdfModule(pages))


LONG_TEXT_A = "Chapter 1 Overview. The NPU has 8 compute cores and a shared 4MB SRAM block."
LONG_TEXT_B = "Chapter 2 Limits. Max tensor height and width is 4096 for conv2d inputs."


def test_new_metadata_page_number_key(monkeypatch, capsys):
    """新版 key page_number（1-based）：頁碼必須正確，不可全部歸 1。"""
    _fake_pdf(monkeypatch, [
        {"metadata": {"page_number": 1}, "text": LONG_TEXT_A, "page_boxes": []},
        {"metadata": {"page_number": 2}, "text": "", "page_boxes": [{"class": "picture"}]},
        {"metadata": {"page_number": 3}, "text": LONG_TEXT_B,
         "page_boxes": [{"class": "text"}, {"class": "picture"}]},
    ])

    chunks = RAG.extract_pdf("fake_spec.pdf")

    assert chunks, "文字頁應產出 chunk"
    assert {c["page"] for c in chunks} == {1, 3}, (
        f"頁碼應反映實體頁，實際: {sorted({c['page'] for c in chunks})}"
    )


def test_legacy_metadata_page_key_still_works(monkeypatch):
    """舊版 key page（0-based）：fallback 路徑 +1 後仍正確。"""
    _fake_pdf(monkeypatch, [
        {"metadata": {"page": 0}, "text": LONG_TEXT_A},
        {"metadata": {"page": 2}, "text": LONG_TEXT_B},
    ])

    chunks = RAG.extract_pdf("fake_spec.pdf")

    assert {c["page"] for c in chunks} == {1, 3}


def test_mixed_pdf_warns_about_embedded_images(monkeypatch, capsys):
    """混合 PDF：內嵌圖必須有 [WARN]（張數+頁碼），純圖頁另有整頁跳過警告。"""
    _fake_pdf(monkeypatch, [
        {"metadata": {"page_number": 1}, "text": LONG_TEXT_A, "page_boxes": []},
        {"metadata": {"page_number": 2}, "text": "", "page_boxes": [{"class": "picture"}]},
        {"metadata": {"page_number": 3}, "text": LONG_TEXT_B,
         "page_boxes": [{"class": "picture"}]},
    ])

    RAG.extract_pdf("fake_spec.pdf")

    out = capsys.readouterr().out
    assert "[WARN]" in out and "內嵌圖" in out, f"缺內嵌圖警告: {out!r}"
    assert "共 2 張" in out and "頁 2, 3" in out, f"張數/頁碼不對: {out!r}"
    assert "第 2 頁" in out and "整頁已跳過" in out, f"缺純圖頁警告: {out!r}"


def test_legacy_images_key_also_detected(monkeypatch, capsys):
    """舊版把內嵌圖放在 images list：同樣要觸發警告。"""
    _fake_pdf(monkeypatch, [
        {"metadata": {"page": 0}, "text": LONG_TEXT_A},
        {"metadata": {"page": 1}, "text": "", "images": [{"bbox": [0, 0, 1, 1]}]},
    ])

    RAG.extract_pdf("fake.pdf")

    out = capsys.readouterr().out
    assert "內嵌圖" in out and "頁 2" in out


def test_text_only_pdf_has_no_image_warning(monkeypatch, capsys):
    """純文字 PDF 不該出現內嵌圖警告（避免狼來了）。"""
    _fake_pdf(monkeypatch, [
        {"metadata": {"page_number": 1}, "text": LONG_TEXT_A, "page_boxes": [{"class": "text"}]},
    ])

    chunks = RAG.extract_pdf("fake.pdf")

    out = capsys.readouterr().out
    assert chunks
    assert "內嵌圖" not in out


def test_real_pymupdf4llm_contract(tmp_path: Path, capsys):
    """整合測試：真 pymupdf4llm 的 page dict 必須仍給得出正確頁碼與圖片框。

    這是抓「上游又改 schema」的活網——當年就是 page → page_number 改名
    讓所有 chunk 歸 1 頁。
    """
    fitz = pytest.importorskip("fitz", reason="需要 PyMuPDF（pymupdf4llm 相依）")
    pytest.importorskip("pymupdf4llm", reason="PDF ingestion 需要 pymupdf4llm")

    pdf = tmp_path / "mixed_spec.pdf"
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), LONG_TEXT_A)
    p2 = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 64, 64))
    pix.clear_with(120)
    p2.insert_image(fitz.Rect(72, 72, 300, 300), pixmap=pix)
    p3 = doc.new_page()
    p3.insert_text((72, 72), LONG_TEXT_B)
    p3.insert_image(fitz.Rect(72, 110, 300, 340), pixmap=pix)
    doc.save(str(pdf))
    doc.close()

    chunks = RAG.extract_pdf(str(pdf))

    out = capsys.readouterr().out
    assert {c["page"] for c in chunks} == {1, 3}, (
        "頁碼歸 1 → pymupdf4llm 的 metadata key 又變了，去修 extract_pdf 的讀法"
    )
    assert "內嵌圖" in out, (
        "偵測不到內嵌圖 → pymupdf4llm 的 page_boxes/images schema 又變了"
    )
