"""檔案存取的 sandbox 與型別分流 —— AGENTS.md §3 的第一道閘。

合併自 tests/test_sandbox.py、tests/test_read_file_gating.py、
tests/test_analyze_file_sandbox.py(2026-08-20):三份都在驗同一件事的不同層——
路徑不得逃出 AICODE_ROOT(_safe_path)、內容型別不得被誤讀(read_file sniff)、
以及 analyze_file 這個對外入口不得洩漏外部路徑存不存在。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import media
from agent_tools import ToolExecutor
from tests._harness import import_mcp_module

# smoke:安全層(AGENTS.md §2.1 第 2 款「無聲失敗風險的契約」)
# AGENTS.md §3 安全檢查點:agent_tools.ToolExecutor._safe_path 與 media._safe_path。
# 整份標 smoke —— sandbox 破了是無聲的:路徑逃出去不會有人喊,只會安靜地讀到
# AICODE_ROOT 外的檔。
pytestmark = pytest.mark.smoke


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    (tmp_path / "inside.txt").write_text("hello\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested\n", encoding="utf-8")
    return tmp_path


def test_safe_path_accepts_relative_inside(sandbox: Path):
    ex = ToolExecutor(str(sandbox))
    assert ex._safe_path("inside.txt") is not None
    assert ex._safe_path("sub/nested.txt") is not None
    assert ex._safe_path(".") is not None


def test_safe_path_rejects_dotdot_escape(sandbox: Path):
    ex = ToolExecutor(str(sandbox))
    assert ex._safe_path("../etc/passwd") is None
    assert ex._safe_path("../../tmp") is None
    assert ex._safe_path("sub/../../etc/passwd") is None


def test_safe_path_rejects_absolute_outside(sandbox: Path, tmp_path_factory):
    ex = ToolExecutor(str(sandbox))
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    assert ex._safe_path(str(outside / "secret.txt")) is None


def test_safe_path_rejects_symlink_escape(sandbox: Path, tmp_path_factory):
    """Symlink 指向 sandbox 外應該被拒絕（因為 .resolve() 會解析 symlink）。"""
    ex = ToolExecutor(str(sandbox))
    outside = tmp_path_factory.mktemp("outside_link")
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = sandbox / "evil_link.txt"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink 在這個平台不支援")
    assert ex._safe_path("evil_link.txt") is None


def test_media_safe_path_requires_root(sandbox: Path):
    media._SANDBOX_ROOT = None  # 重置
    media._ALLOW_EXTERNAL = True
    assert media._safe_path("anything.png") is None


def test_media_safe_path_inside_root(sandbox: Path):
    media.set_sandbox_root(str(sandbox), allow_external=False)
    f = sandbox / "img.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    p = media._safe_path("img.png", allow_external=True, allowed_extensions={".png"})
    assert p is not None
    assert p.name == "img.png"


def test_media_safe_path_blocks_external_when_disabled(sandbox: Path, tmp_path_factory):
    media.set_sandbox_root(str(sandbox), allow_external=False)
    out = tmp_path_factory.mktemp("ext")
    f = out / "img.png"
    f.write_bytes(b"\x89PNG")
    # 即使函式呼叫帶 allow_external=True，全域 _ALLOW_EXTERNAL=False 也要擋
    p = media._safe_path(str(f), allow_external=True, allowed_extensions={".png"})
    assert p is None


# --------------------------------------------------------------------------
# 併自 tests/test_read_file_gating.py:read_file 的內容型別分流。
# --------------------------------------------------------------------------
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


# ============================================================
# 2026-08-14 GPT review 第二輪：
# (a) strict UTF-8 decode 成功 ≠ 文字——C0 控制字元 binary 會過關；
#     尾端容錯只看位置會把 b"\xff" 這種短 binary 放行。
# (b) MAX_FILE_READ_CHARS 對「單一超長首行」與「指定 end_line 大範圍」失效。
# ============================================================
def test_sniff_rejects_single_invalid_byte():
    from agent_tools import _sniff_text_encoding

    enc, reason = _sniff_text_encoding(b"\xff")
    assert enc is None, (enc, reason)


def test_sniff_still_tolerates_truncated_multibyte_tail():
    from agent_tools import _sniff_text_encoding

    head = "前面都是正常中文內容。".encode()[:-1]  # 尾字被讀取邊界切斷
    enc, reason = _sniff_text_encoding(head)
    assert enc == "utf-8", (enc, reason)


def test_read_file_rejects_control_byte_binary(tmp_path: Path):
    """全 \\x01 是合法 UTF-8，但不是文字——要用字元層 printable 比例擋。"""
    (tmp_path / "ctl.dump").write_bytes(b"\x01" * 8192)
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("ctl.dump")

    assert out.startswith("錯誤"), out
    assert "控制字元" in out or "二進位" in out, out


def test_read_file_clips_single_oversized_line(tmp_path: Path):
    """單行 150,000 字：以前整行放進輸出（首行豁免），上限形同虛設。"""
    from config import MAX_FILE_READ_CHARS

    (tmp_path / "one.txt").write_text("x" * 150_000, encoding="utf-8")
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("one.txt")

    assert len(out) <= MAX_FILE_READ_CHARS + 500, len(out)  # 500 = header/footer 餘裕
    assert "[CTX]" in out and "過長" in out, out[-200:]


def test_read_file_explicit_end_line_still_budgeted(tmp_path: Path):
    """指定 end_line 的大範圍也要受 MAX_FILE_READ_CHARS 保險。"""
    from config import MAX_FILE_READ_CHARS

    (tmp_path / "many.txt").write_text("0123456789\n" * 30_000, encoding="utf-8")
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("many.txt", 1, 30_000)

    # 原文預算 MAX；行號裝飾每行另計 ~7 字，給 2 倍上限已足以抓「完全沒截」回歸
    assert len(out) < MAX_FILE_READ_CHARS * 2, len(out)
    assert "[CTX]" in out, out[-200:]
    assert "繼續讀取" in out, out[-200:]


# --------------------------------------------------------------------------
# 併自 tests/test_analyze_file_sandbox.py:analyze_file 入口的 containment。
# --------------------------------------------------------------------------
@pytest.fixture
def mcp_module(monkeypatch, tmp_path: Path):
    """以 tmp_path 當 AICODE_ROOT 重新 import mcp_server(細節見 _harness)。"""
    return import_mcp_module(monkeypatch, tmp_path)


def _call_analyze_file(mcp_module, path: str) -> str:
    """從 mcp.tool 包裝後的 analyze_file 取出實際函式並呼叫。"""
    tool = mcp_module.analyze_file
    # FastMCP @mcp.tool() 把原函式包成 callable,但保留 fn 可呼叫
    fn = getattr(tool, "fn", tool)
    if callable(fn):
        return fn(path)
    # 退路:有些 FastMCP 版本暴露不同欄位
    return tool(path)  # type: ignore[misc]


SANDBOX_ERROR = "錯誤: 路徑不在 AICODE_ROOT 內或檔案不存在"


def test_analyze_file_blocks_outside_existing_file(mcp_module, tmp_path: Path):
    """指向 root 外確實存在的檔案 → 回 sandbox 訊息,不洩漏存在性。"""
    outside = tmp_path.parent / "outside_real.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    try:
        out = _call_analyze_file(mcp_module, str(outside))
        assert out == SANDBOX_ERROR, (
            f"外部存在檔案應回統一訊息,實際: {out!r}"
        )
        # 不能 echo 出絕對路徑
        assert str(outside) not in out
    finally:
        outside.unlink(missing_ok=True)


def test_analyze_file_blocks_outside_nonexistent(mcp_module, tmp_path: Path):
    """指向 root 外不存在的檔案 → 跟存在版本回同一句,不洩漏存在性差異。"""
    nope = tmp_path.parent / "outside_nope.png"
    out = _call_analyze_file(mcp_module, str(nope))
    assert out == SANDBOX_ERROR, out


def test_analyze_file_blocks_dotdot_escape(mcp_module, tmp_path: Path):
    """`../outside.bin` 也必須被擋(resolve 後落在 root 外)。"""
    out = _call_analyze_file(mcp_module, "../outside.bin")
    assert out == SANDBOX_ERROR, out


def test_analyze_file_allows_inside_unsupported_ext(mcp_module, tmp_path: Path):
    """root 內檔案 + 不支援的副檔名 → 不應被 sandbox 攔(由 dispatch 層說明)。"""
    inside = tmp_path / "x.unknown_ext"
    inside.write_bytes(b"\x00\x01\x02")
    out = _call_analyze_file(mcp_module, "x.unknown_ext")
    # 該回「不支援的副檔名」,而不是 sandbox 訊息
    assert "不支援" in out or "支援" in out, out
    assert "AICODE_ROOT" not in out, out


# ------------------------------------------------------------------
# 2026-08-14 review 追加:PDF dispatch 與 KB 自動 refresh
# (借用本檔的 mcp_module fixture — 都是 mcp_server 的 tool 層行為)
# ------------------------------------------------------------------

def test_analyze_file_dispatches_pdf_to_read_pdf(mcp_module, tmp_path: Path, monkeypatch):
    """.pdf 應走 read_pdf(一次性檢視),不再回「不支援的副檔名」。"""
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(
        mcp_module, "read_pdf", lambda p: f"PDF_OK:{Path(p).name}"
    )

    out = _call_analyze_file(mcp_module, "doc.pdf")

    assert out == "PDF_OK:doc.pdf", out


def test_ensure_kb_fresh_reloads_only_on_change(mcp_module):
    """KB.source_changed() True → 換新 KnowledgeBase;False → 原物件不動。"""

    class _Stub:
        def __init__(self, changed: bool):
            self._changed = changed

        def source_changed(self) -> bool:
            return self._changed

    stale = _Stub(True)
    mcp_module.KB = stale
    mcp_module._ensure_kb_fresh()
    assert mcp_module.KB is not stale, "檔案變更後應重建 KB singleton"

    fresh = _Stub(False)
    mcp_module.KB = fresh
    mcp_module._ensure_kb_fresh()
    assert mcp_module.KB is fresh, "沒變更就不該動 KB"


def test_query_knowledge_autoloads_kb_created_after_startup(
    mcp_module, tmp_path: Path, monkeypatch
):
    """啟動時沒有 knowledge.json,之後被 ingest 建出來 → 查詢要自動載入。

    這是 P3「reload 依賴人工記得」的 code 層保證:不呼叫
    reload_knowledge_base 也不能查到過期(或不存在)的 singleton。
    """
    import json as _json

    fn = getattr(mcp_module.query_knowledge, "fn", mcp_module.query_knowledge)

    # get_status() 只為顯示 Rerank/LLM 狀態會 probe /health；這條測試驗的是
    # KB 檔案出現後自動重載，不需要也不允許接觸真 reranker。
    monkeypatch.setattr(
        mcp_module.KnowledgeBase,
        "_check_reranker_available",
        lambda _self: False,
    )

    out_before = fn("任何問題")
    assert out_before.get("error") == "knowledge base not loaded"

    (tmp_path / "knowledge.json").write_text(
        _json.dumps({"chunks": [], "metadata": {}}), encoding="utf-8"
    )

    out_after = fn("任何問題")
    assert "error" not in out_after, out_after
