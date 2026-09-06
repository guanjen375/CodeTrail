"""檔案存取的 sandbox 與型別分流 —— AGENTS.md §2 的第一道閘;grep 輸出預算;PDF 一次性檢視。

合併自 tests/test_sandbox.py、tests/test_read_file_gating.py、
tests/test_analyze_file_sandbox.py(2026-08-20):三份都在驗同一件事的不同層——
路徑不得逃出 AICODE_ROOT(_safe_path)、內容型別不得被誤讀(read_file sniff)、
以及 analyze_file 這個對外入口不得洩漏外部路徑存不存在。

2026-09-02 再併入 tests/test_grep_output_budget.py 與 tests/test_media_read_pdf.py
(同樣是 ToolExecutor / media 這層的檔案存取邊界)。各區段前的分隔註解標了原檔名。

smoke 成員資格與合併前逐條相同:sandbox / read_file 分流 / analyze_file 入口
(AGENTS.md §2 安全檢查點)與 grep 預算(真實 bug regression)原本整檔 smoke,
合併後逐條標 `@pytest.mark.smoke`;read_pdf 那段原本就不在 smoke 裡。

── grep_code 的輸出硬預算(原 test_grep_output_budget.py,實機事故回歸)──
2026-08-17 實機:對 28 GB / 145,825 檔的專案不帶 path 做 grep_code,
`MAX_GREP_RESULTS = 30` 只擋 match 筆數、不擋位元組,25 個 match 就回傳
1,315,124,516 字元(1.32 GB)。那份字串經 MCP stdio 送給前端,造成
worker thread 99% CPU 空轉 30 分鐘以上,工具呼叫永遠停在 status=running。

兩層防線都要釘:
  1. rg 的 --max-columns:讓 rg 自己就不吐超長行。少了它,
     subprocess.run(capture_output=True) 會先把整份 stdout 讀進記憶體
     (實測峰值 14.73 GB),之後再截斷已經來不及。
  2. 收集時的整體預算 MAX_GREP_OUTPUT_CHARS + 單行 MAX_GREP_LINE_CHARS,
     涵蓋沒有 rg 的 Python fallback 路徑。

── media.read_pdf(原 test_media_read_pdf.py,PDF 一次性檢視通道)──
背景(2026-08-14 review,P4):一次性入口只有 read_file(純文字)與
analyze_file(圖片/binary),.pdf 不在任何一格——「只看一眼」一份 PDF
只能 ingest → remove → reload,汙染 KB 且步驟多。read_pdf 補上這個通道:
抽各頁文字、標註內嵌圖(不做 VL)、不寫入 knowledge.json。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
import media
from agent_tools import ToolExecutor, _clip_grep_line, _collect_within_budget
from tests._harness import import_mcp_module

# ═══════════════════════════════════════════════════════════════════════════
# ── 原 test_fs_sandbox.py:sandbox(_safe_path)、read_file 型別分流、analyze_file 入口 ──
# smoke:安全層(AGENTS.md §1.1 第 2 款「無聲失敗風險的契約」)
# AGENTS.md §2 安全檢查點:agent_tools.ToolExecutor._safe_path 與 media._safe_path。
# 這段每一條都標 smoke(原本整檔 `pytestmark`)—— sandbox 破了是無聲的:路徑逃出去
# 不會有人喊,只會安靜地讀到 AICODE_ROOT 外的檔。
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    (tmp_path / "inside.txt").write_text("hello\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested\n", encoding="utf-8")
    return tmp_path


@pytest.mark.smoke
def test_safe_path_accepts_relative_inside(sandbox: Path):
    ex = ToolExecutor(str(sandbox))
    assert ex._safe_path("inside.txt") is not None
    assert ex._safe_path("sub/nested.txt") is not None
    assert ex._safe_path(".") is not None


@pytest.mark.smoke
def test_safe_path_rejects_dotdot_escape(sandbox: Path):
    ex = ToolExecutor(str(sandbox))
    assert ex._safe_path("../etc/passwd") is None
    assert ex._safe_path("../../tmp") is None
    assert ex._safe_path("sub/../../etc/passwd") is None


@pytest.mark.smoke
def test_safe_path_rejects_absolute_outside(sandbox: Path, tmp_path_factory):
    ex = ToolExecutor(str(sandbox))
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    assert ex._safe_path(str(outside / "secret.txt")) is None


@pytest.mark.smoke
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


@pytest.mark.smoke
def test_media_safe_path_requires_root(sandbox: Path):
    media._SANDBOX_ROOT = None  # 重置
    media._ALLOW_EXTERNAL = True
    assert media._safe_path("anything.png") is None


@pytest.mark.smoke
def test_media_safe_path_inside_root(sandbox: Path):
    media.set_sandbox_root(str(sandbox), allow_external=False)
    f = sandbox / "img.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    p = media._safe_path("img.png", allow_external=True, allowed_extensions={".png"})
    assert p is not None
    assert p.name == "img.png"


@pytest.mark.smoke
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
@pytest.mark.smoke
def test_read_file_rejects_pdf_with_guidance(tmp_path: Path):
    (tmp_path / "spec.pdf").write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n")
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("spec.pdf")

    assert out.startswith("錯誤"), out
    assert "analyze_file" in out and "ingest_document" in out, out
    assert "�" not in out  # 不能吐 replacement 亂碼


@pytest.mark.smoke
def test_read_file_rejects_binary_with_nul(tmp_path: Path):
    (tmp_path / "fw.bin").write_bytes(b"MZ\x00\x01\x02\x03" * 16)
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("fw.bin")

    assert out.startswith("錯誤") and "二進位" in out, out
    assert "analyze_file" in out


@pytest.mark.smoke
def test_read_file_still_reads_plain_text(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("a.txt")

    assert "hello" in out and "world" in out


@pytest.mark.smoke
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
@pytest.mark.smoke
def test_read_file_rejects_nul_free_binary(tmp_path: Path):
    """全 0xFF firmware 不含 NUL：以前被放行、內容整片讀成空白。"""
    (tmp_path / "fw.dat").write_bytes(b"\xff" * 8192)
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("fw.dat")

    assert out.startswith("錯誤"), out
    assert "analyze_file" in out, out


@pytest.mark.smoke
def test_read_file_rejects_known_binary_extension_without_sniff(tmp_path: Path):
    """已知 binary 副檔名（就算內容不含 NUL）不必開檔 sniff 就要擋。"""
    (tmp_path / "model.gguf").write_bytes(b"GGUF" + b"ab" * 16)
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("model.gguf")

    assert out.startswith("錯誤") and "非文字" in out, out
    assert "analyze_file" in out, out


@pytest.mark.smoke
def test_read_file_reads_utf16_bom_log(tmp_path: Path):
    """UTF-16（大量 NUL）純文字 log：以前被 NUL heuristic 誤判成二進位。"""
    (tmp_path / "w.log").write_text("hello 世界\nline2 ok\n", encoding="utf-16")  # 帶 BOM
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("w.log")

    assert "hello 世界" in out and "line2 ok" in out, out
    assert "�" not in out, out


@pytest.mark.smoke
def test_read_file_reads_bomless_utf16le_ascii_log(tmp_path: Path):
    (tmp_path / "b.log").write_bytes("event=ok code=200\nnext line\n".encode("utf-16-le"))
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("b.log")

    assert "event=ok code=200" in out and "next line" in out, out


@pytest.mark.smoke
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
@pytest.mark.smoke
def test_sniff_rejects_single_invalid_byte():
    from agent_tools import _sniff_text_encoding

    enc, reason = _sniff_text_encoding(b"\xff")
    assert enc is None, (enc, reason)


@pytest.mark.smoke
def test_sniff_still_tolerates_truncated_multibyte_tail():
    from agent_tools import _sniff_text_encoding

    head = "前面都是正常中文內容。".encode()[:-1]  # 尾字被讀取邊界切斷
    enc, reason = _sniff_text_encoding(head)
    assert enc == "utf-8", (enc, reason)


@pytest.mark.smoke
def test_read_file_rejects_control_byte_binary(tmp_path: Path):
    """全 \\x01 是合法 UTF-8，但不是文字——要用字元層 printable 比例擋。"""
    (tmp_path / "ctl.dump").write_bytes(b"\x01" * 8192)
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("ctl.dump")

    assert out.startswith("錯誤"), out
    assert "控制字元" in out or "二進位" in out, out


@pytest.mark.smoke
def test_read_file_clips_single_oversized_line(tmp_path: Path):
    """單行 150,000 字：以前整行放進輸出（首行豁免），上限形同虛設。"""
    from config import MAX_FILE_READ_CHARS

    (tmp_path / "one.txt").write_text("x" * 150_000, encoding="utf-8")
    ex = ToolExecutor(str(tmp_path))

    out = ex.read_file("one.txt")

    assert len(out) <= MAX_FILE_READ_CHARS + 500, len(out)  # 500 = header/footer 餘裕
    assert "[CTX]" in out and "過長" in out, out[-200:]


@pytest.mark.smoke
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


@pytest.mark.smoke
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


@pytest.mark.smoke
def test_analyze_file_blocks_outside_nonexistent(mcp_module, tmp_path: Path):
    """指向 root 外不存在的檔案 → 跟存在版本回同一句,不洩漏存在性差異。"""
    nope = tmp_path.parent / "outside_nope.png"
    out = _call_analyze_file(mcp_module, str(nope))
    assert out == SANDBOX_ERROR, out


@pytest.mark.smoke
def test_analyze_file_blocks_dotdot_escape(mcp_module, tmp_path: Path):
    """`../outside.bin` 也必須被擋(resolve 後落在 root 外)。"""
    out = _call_analyze_file(mcp_module, "../outside.bin")
    assert out == SANDBOX_ERROR, out


@pytest.mark.smoke
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

@pytest.mark.smoke
def test_analyze_file_dispatches_pdf_to_read_pdf(mcp_module, tmp_path: Path, monkeypatch):
    """.pdf 應走 read_pdf(一次性檢視),不再回「不支援的副檔名」。"""
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(
        mcp_module, "read_pdf", lambda p: f"PDF_OK:{Path(p).name}"
    )

    out = _call_analyze_file(mcp_module, "doc.pdf")

    assert out == "PDF_OK:doc.pdf", out


@pytest.mark.smoke
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


@pytest.mark.smoke
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


# ═══════════════════════════════════════════════════════════════════════════
# ── 原 test_grep_output_budget.py:grep_code 的輸出硬預算(實機事故回歸)──
# smoke:AGENTS.md §1.1 第 1 款「真實發生過的 bug 的 regression」
# 真實 bug regression:grep 輸出預算。這段每一條都標 smoke(原本整檔 `pytestmark`)。
# ═══════════════════════════════════════════════════════════════════════════


def _write_tree(root, *, long_line_chars: int) -> None:
    """一個有超長行的專案:真實世界的生成檔 / 壓縮 JSON 就長這樣。"""
    (root / "src").mkdir(parents=True)
    (root / "src" / "normal.c").write_text(
        "int main(void) {\n    return vec_mem_sys_base;\n}\n", encoding="utf-8"
    )
    # 單行內含 match,長度遠超上限
    blob = "x" * long_line_chars
    (root / "src" / "generated.json").write_text(
        f'{{"pad":"{blob}","sym":"vec_mem_sys_base","pad2":"{blob}"}}\n', encoding="utf-8"
    )


@pytest.mark.smoke
def test_single_long_line_cannot_blow_up_grep_output(tmp_path):
    """單一超長行不得撐爆輸出:整體字元數必須落在預算內。"""
    _write_tree(tmp_path, long_line_chars=2_000_000)   # 兩行各 2 MB
    result = ToolExecutor(str(tmp_path)).grep(pattern="vec_mem_sys_base", context=3)

    assert "vec_mem_sys_base" in result                       # 仍然找得到
    assert len(result) <= config.MAX_GREP_OUTPUT_CHARS * 1.1  # 沒有 GB 級輸出
    longest = max(len(line) for line in result.splitlines())
    # 單行上限 + 截斷註記的長度;絕不能是 2 MB
    assert longest < config.MAX_GREP_LINE_CHARS * 3


@pytest.mark.smoke
def test_rg_is_told_to_cap_columns_itself(tmp_path, monkeypatch):
    """--max-columns 必須真的傳給 rg —— 這是唯一能避免把 GB 級 stdout
    先讀進記憶體的防線,只在事後截斷是不夠的。"""
    if not shutil.which("rg"):
        pytest.skip("這台沒有 rg;此防線只適用 rg 快速路徑")
    seen: list[list[str]] = []
    real_run = __import__("process_env").run

    def spy(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd and cmd[0] == "rg":
            seen.append(cmd)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr("process_env.run", spy)
    _write_tree(tmp_path, long_line_chars=50_000)
    ToolExecutor(str(tmp_path)).grep(pattern="vec_mem_sys_base", context=3)

    assert seen, "沒有呼叫到 rg"
    cmd = seen[0]
    assert "--max-columns" in cmd
    assert cmd[cmd.index("--max-columns") + 1] == str(config.MAX_GREP_LINE_CHARS)


@pytest.mark.smoke
def test_budget_helpers_clip_line_and_stop_at_total():
    """沒有 rg 的 Python fallback 也要受同一組預算保護。"""
    long_line = "y" * (config.MAX_GREP_LINE_CHARS * 4)
    clipped = _clip_grep_line(long_line)
    assert len(clipped) < len(long_line)
    assert "已截斷" in clipped
    # 未超過上限的行原樣保留
    assert _clip_grep_line("short") == "short"

    kept, over = _collect_within_budget([long_line] * 10_000)
    assert over is True
    assert sum(len(k) + 1 for k in kept) <= config.MAX_GREP_OUTPUT_CHARS


@pytest.mark.smoke
def test_scoped_grep_still_returns_full_results(tmp_path):
    """預算不能傷到正常用法:小範圍搜尋照樣拿到完整內容。"""
    _write_tree(tmp_path, long_line_chars=100)
    result = ToolExecutor(str(tmp_path)).grep(
        pattern="vec_mem_sys_base", path="src", context=1
    )
    assert "normal.c" in result
    assert "截斷" not in result


# ═══════════════════════════════════════════════════════════════════════════
# ── 原 test_media_read_pdf.py:media.read_pdf(PDF 一次性檢視通道)—— 不在 smoke 裡 ──
# ═══════════════════════════════════════════════════════════════════════════


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


def _build_blank_pdf(path: Path, n_pages: int) -> None:
    """只提供 read_pdf 所需的真 page_count；內容由受控 converter stub 給。"""
    fitz = pytest.importorskip("fitz", reason="需要 PyMuPDF")
    doc = fitz.open()
    for _ in range(n_pages):
        doc.new_page()
    doc.save(str(path))
    doc.close()


def _stub_markdown(monkeypatch, page_factory):
    """替換昂貴的 pymupdf4llm layout pipeline，保留 pages= 分批契約。"""
    calls: list[list[int]] = []

    def to_markdown(_path, *, pages, **_kwargs):
        calls.append(list(pages))
        return [page_factory(page_idx) for page_idx in pages]

    fake = SimpleNamespace(to_markdown=to_markdown)
    monkeypatch.setattr(media, "require_pymupdf4llm", lambda: fake)
    return calls


def _fake_text_page(page_idx: int) -> dict:
    return {
        "metadata": {"page_number": page_idx + 1},
        "text": (f"page {page_idx + 1} lorem ipsum dolor sit amet " * 30).strip(),
        "page_boxes": [],
        "images": [],
    }


def _fake_image_page(page_idx: int) -> dict:
    return {
        "metadata": {"page_number": page_idx + 1},
        "text": "",
        "page_boxes": [{"class": "picture"}],
        "images": [],
    }


# ============================================================
# 2026-08-14 GPT review #2：max_chars 不是 hard cap——
# header/截斷訊息/逐頁圖片列表都不計入 budget（10,000 圖片頁 +
# max_chars=100 實測輸出 59,230 字），且解析工作不受 cap 限制。
# ============================================================
def test_read_pdf_image_pages_are_range_summarized(pdf_sandbox: Path, monkeypatch):
    _build_blank_pdf(pdf_sandbox / "imgs.pdf", 40)
    _stub_markdown(monkeypatch, _fake_image_page)

    out = media.read_pdf("imgs.pdf")

    assert "頁 1-40" in out, out          # 範圍摘要
    assert "頁 1, 2, 3" not in out, out   # 不逐頁列舉


def test_read_pdf_output_is_hard_capped(pdf_sandbox: Path, monkeypatch):
    _build_blank_pdf(pdf_sandbox / "imgs2.pdf", 40)
    _stub_markdown(monkeypatch, _fake_image_page)

    out = media.read_pdf("imgs2.pdf", max_chars=700)

    assert len(out) <= 700, (len(out), out)
    assert "[已截斷]" in out, out


def test_read_pdf_stops_parsing_after_budget(pdf_sandbox: Path, monkeypatch):
    """達 budget 後的批次不再解析：高頁數 PDF 不會為被丟棄的內容卡住 MCP。"""
    _build_blank_pdf(pdf_sandbox / "long.pdf", 60)
    calls = _stub_markdown(monkeypatch, _fake_text_page)

    out = media.read_pdf("long.pdf", max_chars=800)

    assert len(calls) == 1, calls  # 60 頁 / 批 16 → 只解析第一批
    assert calls[0] == list(range(16)), calls
    assert "[已截斷]" in out, out
    assert "未解析" in out, out
