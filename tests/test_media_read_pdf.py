"""media.read_pdf（PDF 一次性檢視通道）測試。

背景（2026-08-14 review，P4）：一次性入口只有 read_file（純文字）與
analyze_file（圖片/binary），.pdf 不在任何一格——「只看一眼」一份 PDF
只能 ingest → remove → reload，汙染 KB 且步驟多。read_pdf 補上這個通道：
抽各頁文字、標註內嵌圖（不做 VL）、不寫入 knowledge.json。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import media


@pytest.fixture
def pdf_sandbox(tmp_path: Path):
    """設 media sandbox root 到 tmp_path，測完還原全域狀態。"""
    old_root = media._SANDBOX_ROOT
    old_ext = media._ALLOW_EXTERNAL
    media.set_sandbox_root(str(tmp_path), allow_external=False)
    yield tmp_path
    media._SANDBOX_ROOT = old_root
    media._ALLOW_EXTERNAL = old_ext


def _build_mixed_pdf(path: Path) -> None:
    fitz = pytest.importorskip("fitz", reason="需要 PyMuPDF（pymupdf4llm 相依）")
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), "Chapter 1: the NPU has 8 cores.")
    p2 = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 64, 64))
    pix.clear_with(120)
    p2.insert_image(fitz.Rect(72, 72, 300, 300), pixmap=pix)
    doc.save(str(path))
    doc.close()


def test_read_pdf_rejects_non_pdf_suffix(pdf_sandbox: Path):
    (pdf_sandbox / "note.md").write_text("hello", encoding="utf-8")
    out = media.read_pdf("note.md")
    assert out.startswith("[PDF 錯誤]"), out


def test_read_pdf_rejects_outside_sandbox(pdf_sandbox: Path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "x.pdf"
    outside.write_bytes(b"%PDF-1.4 fake")
    out = media.read_pdf(str(outside))
    assert out.startswith("[PDF 錯誤]"), out


def test_read_pdf_extracts_text_and_flags_images(pdf_sandbox: Path):
    pytest.importorskip("pymupdf4llm", reason="PDF 檢視需要 pymupdf4llm")
    _build_mixed_pdf(pdf_sandbox / "mixed.pdf")

    out = media.read_pdf("mixed.pdf")

    assert "未寫入 knowledge.json" in out
    assert "第 1 頁" in out and "8 cores" in out
    assert "第 2 頁" in out and "內嵌圖" in out, out
    assert "[注意]" in out and "頁 2" in out, out


def test_read_pdf_truncates_by_max_chars(pdf_sandbox: Path):
    pytest.importorskip("pymupdf4llm", reason="PDF 檢視需要 pymupdf4llm")
    _build_mixed_pdf(pdf_sandbox / "mixed2.pdf")

    out = media.read_pdf("mixed2.pdf", max_chars=10)

    assert "[已截斷]" in out, out
