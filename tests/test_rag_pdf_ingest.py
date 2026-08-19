"""PDF ingest 全鏈:pymupdf4llm 契約、頁碼、內嵌圖轉 VL、以及 origin 溯源揭露。

併入 tests/test_rag_vl_provenance.py(2026-08-20):VL 來源標記是 ingest 的產物,
分兩個檔會讓「哪些 chunk 該有 origin」的契約被拆散。
"""
from __future__ import annotations

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


def _stub_render(monkeypatch):
    """替換 pymupdf 開檔與 render：bytes 由 job 決定，dedup 邏輯吃真 hash。

    crop 的 bytes 只看 bbox（同一框 → 同一張圖 → 相同 bytes，跨頁去重可測）；
    整頁 render 的 bytes 帶頁碼（不同頁預設視為不同內容）。
    """
    monkeypatch.setattr(
        RAG, "_open_pdf_document",
        lambda _path: types.SimpleNamespace(page_count=9999, close=lambda: None),
    )

    def _render(_doc, job):
        if job["mode"] == "page":
            return f"PNG:page:{job['page']}".encode()
        return f"PNG:crop:{job['bbox']}".encode()

    monkeypatch.setattr(RAG, "_render_pdf_figure_png", _render)


VL_DESCRIPTION = (
    "# 架構圖\n\n## 概述\nNPU 方塊圖，含 8 個運算核心與 4MB 共享 SRAM，"
    "經 AXI 匯流排連接 DMA 引擎。"
)


class _VLSpy:
    """假 VL：記錄每次呼叫；fail=True 時模擬 VL 端失敗。"""

    def __init__(self, description: str = VL_DESCRIPTION, fail: bool = False):
        self.calls = []
        self.description = description
        self.fail = fail

    def __call__(self, image_base64: str, mime_type: str) -> str:
        self.calls.append((image_base64, mime_type))
        if self.fail:
            raise RuntimeError("VL server unreachable (stub)")
        return self.description


def _stub_vl(monkeypatch, **kwargs) -> _VLSpy:
    spy = _VLSpy(**kwargs)
    monkeypatch.setattr(RAG, "_describe_technical_image_base64", spy)
    return spy


def _text_chunks(chunks):
    return [c for c in chunks if c.get("origin") != "diagram"]


def _diagram_chunks(chunks):
    return [c for c in chunks if c.get("origin") == "diagram"]


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
    _stub_render(monkeypatch)
    _stub_vl(monkeypatch)

    chunks = RAG.extract_pdf("fake_spec.pdf")

    text = _text_chunks(chunks)
    assert text, "文字頁應產出 chunk"
    assert {c["page"] for c in text} == {1, 3}, (
        f"頁碼應反映實體頁，實際: {sorted({c['page'] for c in text})}"
    )
    # 內嵌圖也要落在正確頁（沿用同一個 page_number 相容 helper）
    assert {c["page"] for c in _diagram_chunks(chunks)} == {2, 3}


def test_legacy_metadata_page_key_still_works(monkeypatch):
    """舊版 key page（0-based）：fallback 路徑 +1 後仍正確。"""
    _fake_pdf(monkeypatch, [
        {"metadata": {"page": 0}, "text": LONG_TEXT_A},
        {"metadata": {"page": 2}, "text": LONG_TEXT_B},
    ])

    chunks = RAG.extract_pdf("fake_spec.pdf")

    assert {c["page"] for c in chunks} == {1, 3}


# ============================================================
# 內嵌圖自動 VL：分流
# ============================================================
def test_text_only_pdf_zero_vl_calls(monkeypatch, capsys):
    """純文字 PDF：零 VL 呼叫、零 diagram chunk、文字 chunk 照舊。"""
    _fake_pdf(monkeypatch, [
        {"metadata": {"page_number": 1}, "text": LONG_TEXT_A,
         "page_boxes": [{"class": "text"}]},
        {"metadata": {"page_number": 2}, "text": LONG_TEXT_B, "page_boxes": []},
    ])
    spy = _stub_vl(monkeypatch)

    chunks = RAG.extract_pdf("fake.pdf")

    out = capsys.readouterr().out
    assert spy.calls == [], "純文字 PDF 不得觸發任何 VL 呼叫"
    assert chunks and not _diagram_chunks(chunks)
    assert "內嵌圖" not in out


def test_mixed_pdf_text_chunks_unchanged_plus_diagram(monkeypatch):
    """混合 PDF：文字 chunk 與純文字路徑逐位元組相同，另產 diagram chunk。"""
    def _pages(with_pics: bool):
        boxes = [{"class": "picture", "bbox": BIG_BBOX}] if with_pics else []
        return [
            {"metadata": {"page_number": 1}, "text": LONG_TEXT_A, "page_boxes": []},
            {"metadata": {"page_number": 2}, "text": LONG_TEXT_B, "page_boxes": boxes},
        ]

    _fake_pdf(monkeypatch, _pages(False))
    baseline = RAG.extract_pdf("fake_spec.pdf")

    _fake_pdf(monkeypatch, _pages(True))
    _stub_render(monkeypatch)
    spy = _stub_vl(monkeypatch)
    chunks = RAG.extract_pdf("fake_spec.pdf")

    text = _text_chunks(chunks)
    assert [c["content"] for c in text] == [c["content"] for c in baseline]
    assert [c["page"] for c in text] == [c["page"] for c in baseline]

    figs = _diagram_chunks(chunks)
    assert len(spy.calls) == 1
    assert figs, "混合 PDF 應另產 diagram chunk"
    assert all(c["page"] == 2 for c in figs)
    assert all(c["type"] == "diagram" for c in figs)
    assert all(c["figure_index"] == 1 for c in figs)


def test_image_only_page_produces_chunks(monkeypatch):
    """無文字頁（掃描頁）：整頁 render 經 VL，chunk 數 > 0（現行為 0）。"""
    _fake_pdf(monkeypatch, [
        {"metadata": {"page_number": 1}, "text": "",
         "page_boxes": [{"class": "picture", "bbox": BIG_BBOX}]},
    ])
    _stub_render(monkeypatch)
    spy = _stub_vl(monkeypatch)

    chunks = RAG.extract_pdf("scan.pdf")

    assert len(chunks) > 0
    assert all(c.get("origin") == "diagram" for c in chunks)
    assert all(c["page"] == 1 for c in chunks)
    assert len(spy.calls) == 1


def test_multiple_figures_same_page_distinguishable(monkeypatch):
    """同頁多張影像：figure 索引可區分，chunk_index 不互相覆蓋。"""
    _fake_pdf(monkeypatch, [
        {"metadata": {"page_number": 1}, "text": LONG_TEXT_A,
         "page_boxes": [{"class": "picture", "bbox": BIG_BBOX},
                        {"class": "picture", "bbox": BIG_BBOX_2}]},
    ])
    _stub_render(monkeypatch)
    spy = _stub_vl(monkeypatch)

    chunks = RAG.extract_pdf("fake_spec.pdf")

    figs = _diagram_chunks(chunks)
    assert len(spy.calls) == 2
    assert {c["figure_index"] for c in figs} == {1, 2}
    # 同頁所有 chunk（文字+圖）index 不得重複，否則 chunk id 空間互踩
    indices = [c["chunk_index"] for c in chunks if c["page"] == 1]
    assert len(indices) == len(set(indices)), f"chunk_index 重複: {indices}"


def test_duplicate_images_dedup(monkeypatch, capsys):
    """重複影像（頁首 logo 類）：只產一個 chunk，記錄首次出現頁碼。"""
    _fake_pdf(monkeypatch, [
        {"metadata": {"page_number": 1}, "text": LONG_TEXT_A,
         "page_boxes": [{"class": "picture", "bbox": BIG_BBOX}]},
        {"metadata": {"page_number": 2}, "text": LONG_TEXT_B,
         "page_boxes": [{"class": "picture", "bbox": BIG_BBOX}]},
    ])
    _stub_render(monkeypatch)  # 同 bbox → 同 bytes → 同 hash
    spy = _stub_vl(monkeypatch)

    chunks = RAG.extract_pdf("fake_spec.pdf")

    figs = _diagram_chunks(chunks)
    assert len(spy.calls) == 1, "同一張圖只送一次 VL"
    assert figs and {c["page"] for c in figs} == {1}, "chunk 應記錄首次出現的頁碼"
    assert "去重" in capsys.readouterr().out


def test_tiny_images_not_sent_to_vl(monkeypatch, capsys):
    """過小影像（圖示/項目符號/分隔線）：不送 VL、不產 chunk。"""
    _fake_pdf(monkeypatch, [
        {"metadata": {"page_number": 1}, "text": LONG_TEXT_A,
         "page_boxes": [{"class": "picture", "bbox": TINY_BBOX}]},
    ])
    spy = _stub_vl(monkeypatch)

    chunks = RAG.extract_pdf("fake_spec.pdf")

    assert spy.calls == []
    assert not _diagram_chunks(chunks)
    assert "過小" in capsys.readouterr().out


def test_tiny_only_image_page_is_skipped(monkeypatch, capsys):
    """幾乎沒文字且圖全過小的頁：不整頁 render（避免 VL 看空白頁）。"""
    _fake_pdf(monkeypatch, [
        {"metadata": {"page_number": 1}, "text": LONG_TEXT_A, "page_boxes": []},
        {"metadata": {"page_number": 2}, "text": "",
         "page_boxes": [{"class": "picture", "bbox": TINY_BBOX}]},
    ])
    spy = _stub_vl(monkeypatch)

    chunks = RAG.extract_pdf("fake_spec.pdf")

    assert spy.calls == []
    assert not _diagram_chunks(chunks)
    assert "只有過小影像" in capsys.readouterr().out


def test_legacy_images_key_with_bbox_processed(monkeypatch):
    """舊版把內嵌圖放在 images list：一樣分流、一樣送 VL。"""
    _fake_pdf(monkeypatch, [
        {"metadata": {"page": 0}, "text": LONG_TEXT_A},
        {"metadata": {"page": 1}, "text": "", "images": [{"bbox": list(BIG_BBOX)}]},
        {"metadata": {"page": 2}, "text": LONG_TEXT_B,
         "images": [{"bbox": list(BIG_BBOX_2)}]},
    ])
    _stub_render(monkeypatch)
    spy = _stub_vl(monkeypatch)

    chunks = RAG.extract_pdf("fake.pdf")

    figs = _diagram_chunks(chunks)
    assert len(spy.calls) == 2
    assert {c["page"] for c in figs} == {2, 3}


def test_missing_bbox_degrades_to_page_render(monkeypatch, capsys):
    """偵測到圖但 bbox 解析不出（舊 schema）：降級整頁 render，不無聲丟圖。"""
    _fake_pdf(monkeypatch, [
        {"metadata": {"page_number": 1}, "text": LONG_TEXT_A,
         "page_boxes": [{"class": "picture"}]},  # 沒有 bbox
    ])
    _stub_render(monkeypatch)
    spy = _stub_vl(monkeypatch)

    chunks = RAG.extract_pdf("fake_spec.pdf")

    figs = _diagram_chunks(chunks)
    assert len(spy.calls) == 1
    assert figs and all(c["page"] == 1 for c in figs)
    assert "缺 bbox" in capsys.readouterr().out


# ============================================================
# hard fail 與 per-doc 原子性
# ============================================================
MIXED_PAGES_FOR_FAIL = [
    {"metadata": {"page_number": 1}, "text": LONG_TEXT_A, "page_boxes": []},
    {"metadata": {"page_number": 2}, "text": "",
     "page_boxes": [{"class": "picture", "bbox": BIG_BBOX}]},
]


def test_vl_failure_hard_fails_and_kb_bytes_unchanged(monkeypatch, tmp_path: Path):
    """VL 呼叫失敗：raise（帶檔案/頁碼/圖索引/原始錯誤），knowledge.json 位元組不變。"""
    pdf = tmp_path / "mixed_spec.pdf"
    pdf.write_bytes(b"%PDF-fake")  # 內容無關：to_markdown/render 都被替換
    kb_path = tmp_path / "knowledge.json"
    kb_path.write_text(json.dumps({
        "metadata": {
            "embedding_model": RAG.EMBEDDING_MODEL,
            "documents": ["old_doc.md"],
            "total_documents": 1,
            "total_chunks": 0,
        },
        "chunks": [],
    }, ensure_ascii=False), encoding="utf-8")
    before = kb_path.read_bytes()

    _fake_pdf(monkeypatch, MIXED_PAGES_FOR_FAIL)
    _stub_render(monkeypatch)
    _stub_vl(monkeypatch, fail=True)

    with pytest.raises(RAG.PdfFigureError) as exc:
        RAG.add_document(str(pdf), str(kb_path))

    msg = str(exc.value)
    assert "mixed_spec.pdf" in msg, msg
    assert "第 2 頁" in msg and "圖 1" in msg, msg
    assert "VL server unreachable" in msg, msg
    assert kb_path.read_bytes() == before, "失敗後 knowledge.json 必須零寫入"


def test_render_failure_hard_fails(monkeypatch):
    """render 失敗同樣 hard fail：圖絕不無聲消失。"""
    _fake_pdf(monkeypatch, MIXED_PAGES_FOR_FAIL)
    monkeypatch.setattr(
        RAG, "_open_pdf_document",
        lambda _path: types.SimpleNamespace(page_count=9999, close=lambda: None),
    )

    def _boom(_doc, _job):
        raise ValueError("pixmap allocation failed (stub)")

    monkeypatch.setattr(RAG, "_render_pdf_figure_png", _boom)
    spy = _stub_vl(monkeypatch)

    with pytest.raises(RAG.PdfFigureError) as exc:
        RAG.extract_pdf("fake_spec.pdf")

    assert "render 失敗" in str(exc.value)
    assert spy.calls == []


def test_render_precondition_failure_keeps_full_location(monkeypatch, tmp_path: Path):
    """render helper 自己的前置檢查（頁碼超界）也必須帶檔案/頁/圖/第 N 張。

    舊版讓 helper 自拋 PdfFigureError 再被裸 re-raise，訊息只剩「第 2 頁」，
    缺檔名與 figure 索引——與 fail-loud「可直接定位」的承諾不符。

    用真 pymupdf render：PDF 實際只有 1 頁，但 metadata 報 2 頁（模擬 schema
    漂移／畸形檔），第 1 張正常 render、第 2 張撞前置檢查。
    """
    fitz = pytest.importorskip("fitz", reason="需要 PyMuPDF（pymupdf4llm 相依）")

    pdf = tmp_path / "spec.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), LONG_TEXT_A)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 64, 64))
    pix.clear_with(120)
    page.insert_image(fitz.Rect(*BIG_BBOX), pixmap=pix)
    doc.save(str(pdf))
    doc.close()

    _fake_pdf(monkeypatch, [
        {"metadata": {"page_number": 1}, "text": LONG_TEXT_A,
         "page_boxes": [{"class": "picture", "bbox": BIG_BBOX}]},
        {"metadata": {"page_number": 2}, "text": LONG_TEXT_B,
         "page_boxes": [{"class": "picture", "bbox": BIG_BBOX_2}]},
    ])
    monkeypatch.setattr(RAG, "_describe_technical_image_base64",
                        lambda *_a, **_k: VL_DESCRIPTION)

    with pytest.raises(RAG.PdfFigureError) as exc:
        RAG.extract_pdf(str(pdf))

    msg = str(exc.value)
    assert "spec.pdf" in msg, msg
    assert "第 2 頁" in msg and "圖 1" in msg, msg
    assert "第 2/2 張" in msg, msg
    assert "頁碼超出範圍" in msg and "PDF 共 1 頁" in msg, msg


def test_empty_vl_description_is_hard_fail(monkeypatch):
    """VL 回空/純空白：嚴格核心要 raise，不得產生空 chunk。"""
    monkeypatch.setattr(
        RAG.llama_client, "vision_completion", lambda **_kw: "   \n  "
    )
    with pytest.raises(RuntimeError, match="空內容"):
        RAG._describe_technical_image_base64("aGk=", "image/png")


# ============================================================
# 整合：真 pymupdf4llm + 真 render（VL 打樁）
# ============================================================
def test_real_pymupdf4llm_contract(tmp_path: Path, capsys, monkeypatch):
    """整合測試：真 pymupdf4llm 的 page dict + 真 pymupdf render 必須撐起整條分流。

    這是抓「上游又改 schema」的活網——當年 page → page_number 改名讓所有
    chunk 歸 1 頁；現在 page_boxes 的 class/bbox 再變動，會直接讓內嵌圖
    偵測或 crop 破掉。
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

    spy = _stub_vl(monkeypatch)

    chunks = RAG.extract_pdf(str(pdf))

    out = capsys.readouterr().out
    text = _text_chunks(chunks)
    figs = _diagram_chunks(chunks)
    assert {c["page"] for c in text} == {1, 3, 4, 5}, (
        "頁碼歸 1 → pymupdf4llm 的 metadata key 又變了，去修 _pdf_page_number"
    )
    assert {c["page"] for c in figs} == {2, 3, 4}, (
        "內嵌圖分流結果不對（p2 整頁、p3 crop、p4 首見、p5 應去重）→ "
        f"實際: {sorted({c['page'] for c in figs})}；"
        "偵測不到圖表示 page_boxes schema 又變了"
    )
    assert len(spy.calls) == 3, "p5 與 p4 同圖應去重，只剩 3 次 VL 呼叫"
    # 送 VL 的必須是真 PNG（render 產物）
    import base64
    for image_b64, mime in spy.calls:
        assert mime == "image/png"
        assert base64.b64decode(image_b64)[:8] == b"\x89PNG\r\n\x1a\n"
    assert "去重" in out
    assert "過小" in out  # p3 的 12x12 圖示要被門檻擋下
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
