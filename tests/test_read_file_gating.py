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


# ============================================================
# 2026-08-14 GPT review #3：只看前 4096 bytes 有無 NUL 會同時
# 誤放行（全 0xFF firmware 讀成空白）與誤拒絕（UTF-16 純文字 log）。
# ============================================================
def test_read_file_rejects_nul_free_binary(tmp_path: Path):
    """全 0xFF firmware 不含 NUL：以前被放行、內容整片讀成空白。"""
    (tmp_path / "fw.dat").write_bytes(b"\xff" * 8192)
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("fw.dat")

    assert out.startswith("錯誤"), out
    assert "analyze_file" in out, out


def test_read_file_rejects_known_binary_extension_without_sniff(tmp_path: Path):
    """已知 binary 副檔名（就算內容不含 NUL）不必開檔 sniff 就要擋。"""
    (tmp_path / "model.gguf").write_bytes(b"GGUF" + b"ab" * 16)
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("model.gguf")

    assert out.startswith("錯誤") and "非文字" in out, out
    assert "analyze_file" in out, out


def test_read_file_reads_utf16_bom_log(tmp_path: Path):
    """UTF-16（大量 NUL）純文字 log：以前被 NUL heuristic 誤判成二進位。"""
    (tmp_path / "w.log").write_text("hello 世界\nline2 ok\n", encoding="utf-16")  # 帶 BOM
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("w.log")

    assert "hello 世界" in out and "line2 ok" in out, out
    assert "�" not in out, out


def test_read_file_reads_bomless_utf16le_ascii_log(tmp_path: Path):
    (tmp_path / "b.log").write_bytes("event=ok code=200\nnext line\n".encode("utf-16-le"))
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("b.log")

    assert "event=ok code=200" in out and "next line" in out, out


def test_read_file_survives_stray_binary_byte(tmp_path: Path):
    """幾乎全 ASCII、夾一個壞 byte 的 log：不能整檔靜默變空白。

    舊實作用 linecache（strict decode）讀內容：檔案任何位置一個非 UTF-8
    byte 就會讓所有行都回空字串，輸出變成 N 行帶行號的空白。
    """
    (tmp_path / "s.log").write_bytes(b"line one ok\nbad:\x80 spew\nline three ok\n")
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("s.log")

    assert "line one ok" in out and "line three ok" in out, out
