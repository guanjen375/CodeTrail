"""ToolExecutor.read_file 的內容型別分流測試。

背景（2026-08-14 review，P4 附帶發現）：read_file 用 errors='replace' 硬開
任何檔案——對 PDF/二進位會「成功地」吐出一堆帶行號的 U+FFFD 亂碼，
燒 context 又容易誘發模型腦補。修正後改回導引訊息（analyze_file /
ingest_document）。
"""
from __future__ import annotations

from pathlib import Path

from agent_tools import ToolExecutor


def test_read_file_rejects_pdf_with_guidance(tmp_path: Path):
    (tmp_path / "spec.pdf").write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n")
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("spec.pdf")

    assert out.startswith("錯誤"), out
    assert "analyze_file" in out and "ingest_document" in out, out
    assert "�" not in out  # 不能吐 replacement 亂碼


def test_read_file_rejects_binary_with_nul(tmp_path: Path):
    (tmp_path / "fw.bin").write_bytes(b"MZ\x00\x01\x02\x03" * 16)
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("fw.bin")

    assert out.startswith("錯誤") and "二進位" in out, out
    assert "analyze_file" in out


def test_read_file_still_reads_plain_text(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("a.txt")

    assert "hello" in out and "world" in out


def test_read_file_allows_utf8_chinese(tmp_path: Path):
    """UTF-8 多位元組字元不含 NUL，不能被誤判成二進位。"""
    (tmp_path / "doc.md").write_text("繁體中文內容，含標點——OK。\n", encoding="utf-8")
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("doc.md")

    assert "繁體中文內容" in out
