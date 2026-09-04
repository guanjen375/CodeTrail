"""RAG ingest 鏈:切塊、PDF ingest、ExtractedDocument 章節／頁碼座標、chunk 脈絡生成端。

合併自四份測試(2026-09-02),各自的脈絡分段保留如下。smoke 成員資格逐條保留:
test_rag_chunking.py 原本整檔 smoke → 那一區段每條各自標 `@pytest.mark.smoke`;
test_rag_pdf_ingest.py 原本就是逐條標;另外兩份沒有 smoke → 本檔不用 module 層 pytestmark。

test_rag_chunking.py —— RAG 切塊契約:不得遺失內容,表頭要在每個切片重複(它本身合併自
test_rag_chunk_lossless.py 與 test_rag_table_headers.py,2026-08-20)。無聲失敗契約:
切塊不得遺失內容、表頭要重複(AGENTS.md §1.1 第 1 款「真實發生過的 bug 的 regression」)。

test_rag_pdf_ingest.py —— PDF ingest 全鏈:pymupdf4llm 契約、頁碼、內嵌圖轉 VL、以及
origin 溯源揭露。併入 tests/test_rag_vl_provenance.py(2026-08-20):VL 來源標記是 ingest
的產物,分兩個檔會讓「哪些 chunk 該有 origin」的契約被拆散。

test_extracted_document.py —— ExtractedDocument(章節 span / 頁碼 / chunk 定位)的單一真相
回歸測試。背景:切完 chunk 之後「完整文件 + 章節範圍」在舊資料流裡無法無損還原——PDF
逐頁切、四個入庫入口各自組 chunk dict,章節於是有兩套來源(splitter 的 page-local 追蹤與
呼叫端的 `last_section` 繼承),對不上時沒有仲裁者。實測那個繼承會把「開頭就是
`## 測試結果` 的整頁短 chunk」歸到上一頁的章節,而且錯誤會跨頁累積。語料全部是合成的
(`spec_a.md` / `toolchain_x`),刻意帶重複標題——同一份規格書有十個「測試結果」節,正是
脫離父章節後 chunk 幾乎零鑑別度的動機案例。

test_context_generation.py —— Chunk 脈絡生成端的回歸測試:窗預算、快取、鎖、輸出衛生、
端點安全。全部離線:HTTP 層用假的 `_request_json` / 假 session 注入,沒有任何測試會碰到
真的 llama-server。語料合成(`spec_a.md` / `toolchain_x`)。雙訊號那一半(組字、schema、
儲存、六個決策點)在 tests/test_rag_retrieval.py(原 tests/test_contextual_signals.py)。
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import types
from pathlib import Path

import pytest

import config
import context_budget
import context_generation as cg
import extracted_document
import knowledge
import RAG
from extracted_document import (
    PAGE_SEPARATOR,
    ExtractedDocument,
    Section,
    extract_sections,
    normalize_document_text,
)
from RAG import split_by_semantic_with_sections


# ── 原 test_rag_chunking.py:切塊不得遺失內容、表頭要在每個切片重複 ──
# 來源檔原本是整檔 smoke(AGENTS.md §1.1 第 1 款「真實發生過的 bug 的 regression」),
# 合併後改成每條各自標 @pytest.mark.smoke。
def _strip_prefixes(content: str) -> str:
    """移除 heading 注入行，只留正文（heading 是我們自己加的前綴）。"""
    lines = content.split("\n")
    body = [ln for ln in lines if not ln.startswith("[HEADING]") and not ln.startswith("[SECTION]")]
    return "\n".join(body)


@pytest.mark.smoke
def test_review_example_no_char_loss():
    """review 的三段例子：HIJ 與 hij 不能消失。"""
    text = "0123456789\nABCDEFGHIJ\nabcdefghij"
    chunks = split_by_semantic_with_sections(
        text, max_chars=10, overlap_chars=3, include_heading=False
    )
    joined = "\n".join(c["content"] for c in chunks)
    # 每一段原文都必須完整出現在某個 chunk 內
    assert "ABCDEFGHIJ" in joined, joined
    assert "abcdefghij" in joined, joined
    assert "HIJ" in joined and "hij" in joined, joined


@pytest.mark.smoke
def test_every_original_line_survives_chunking():
    """一般不變量：每一行原文都要能在切出來的 chunks 中找回。"""
    # 造出足夠長、且每行有獨特 token 的文件，逼出多個 chunk + overlap
    lines = [f"LINE{i:03d}_" + ("payload" * 20) for i in range(60)]
    text = "\n".join(lines)

    chunks = split_by_semantic_with_sections(text)  # 用預設 CHUNK_SIZE/OVERLAP/heading
    bodies = "\n".join(_strip_prefixes(c["content"]) for c in chunks)

    missing = [ln[:10] for ln in lines if ln not in bodies]
    assert not missing, f"以下原文行在 chunking 後遺失: {missing}"


@pytest.mark.smoke
def test_overlap_does_not_truncate_current_chunk_tail():
    """當 overlap+正文超過 max_chars 時，正文尾端不能被截掉。

    兩段都要**在** max_chars 之內，超出是 overlap 造成的——這才是要測的那條路。
    （舊 fixture 的 seg2 本身就超過 max_chars，會先被「單行超長 → 按句切」硬切；
    它之所以沒被切，是因為當時 `"A"*40` 這種整行大寫被誤判成標題而跳過長度檢查。
    標題偵測收緊之後那個巧合消失，fixture 才露出來。）
    """
    seg1 = "A" * 40
    seg2 = "B" * 23 + "TAIL_MUST_SURVIVE"   # 40 字元，未超過 max_chars
    text = seg1 + "\n" + seg2
    chunks = split_by_semantic_with_sections(
        text, max_chars=45, overlap_chars=10, include_heading=False
    )
    joined = "\n".join(c["content"] for c in chunks)
    assert "TAIL_MUST_SURVIVE" in joined, joined


# --------------------------------------------------------------------------
# 併自 tests/test_rag_table_headers.py。
# --------------------------------------------------------------------------
@pytest.mark.smoke
def test_register_table_header_is_repeated_after_chunk_split():
    rows = [
        "| Register | Offset | Reset | Description |",
        "| --- | --- | --- | --- |",
    ]
    rows.extend(
        f"| REG_{i:02d} | 0x{i * 4:04X} | {i} | control field number {i} |"
        for i in range(20)
    )
    chunks = RAG.split_by_semantic_with_sections(
        "\n".join(rows), max_chars=180, overlap_chars=0, include_heading=False
    )

    assert len(chunks) > 1
    for chunk in chunks:
        if "| REG_" in chunk["content"]:
            assert "| Register | Offset | Reset | Description |" in chunk["content"]
            assert "| --- | --- | --- | --- |" in chunk["content"]


# ── 原 test_rag_pdf_ingest.py:pymupdf4llm 契約、頁碼、內嵌圖／structured lane、origin 揭露 ──
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
    assert context["section"] == "", (
        "沒有文字層就證明不出這一頁屬於哪一節：掃描頁完全可能在影像裡開新的一節。"
        f"退回 offset 0 會掛到第一節、沿用前一頁會掛到前一節，實際 {context['section']!r}")
    assert context["heading_hierarchy"] == ""


# ── 原 test_extracted_document.py:ExtractedDocument 的章節 span / 頁碼 / chunk 定位 ──
# ============================================================
# 合成語料
# ============================================================
def _filler(tag: str, count: int) -> str:
    """夠長、可辨識、不含標題語法的內文。"""
    return " ".join(f"{tag}{i:03d}" for i in range(count))


def _repeated_heading_doc() -> str:
    """三個同名「測試結果」節，各自掛在不同的父章節下。"""
    blocks = []
    for chapter in (1, 2, 3):
        blocks.append(f"# Chapter {chapter} Toolchain X")
        blocks.append(_filler(f"c{chapter}intro", 120))
        blocks.append("")
        blocks.append("## 測試結果")
        blocks.append(_filler(f"c{chapter}result", 200))
        blocks.append("")
    return "\n".join(blocks)


def _chunked_doc(path: Path, text: str, name: str = "spec_a.md") -> ExtractedDocument:
    target = path / name
    target.write_text(text, encoding="utf-8")
    return RAG.extract_text_file_document(str(target))


# `_FakePdfModule` / `_fake_pdf` 與上面 test_rag_pdf_ingest.py 區段的定義完全相同,共用那一份。


# ============================================================
# 章節 span
# ============================================================
def test_sections_tile_the_document_without_gaps():
    raw = normalize_document_text(_repeated_heading_doc())
    sections = extract_sections(raw)

    assert sections, "有標題的文件必須切得出 section"
    assert sections[0].char_span[0] == 0
    assert sections[-1].char_span[1] == len(raw)
    for previous, current in zip(sections, sections[1:]):
        assert previous.char_span[1] == current.char_span[0], (
            f"section span 不連續: {previous} → {current}"
        )


def test_repeated_headings_get_distinct_spans_and_parents():
    raw = normalize_document_text(_repeated_heading_doc())
    sections = extract_sections(raw)

    results = [s for s in sections if s.title == "測試結果"]
    assert len(results) == 3, "三個同名節必須各自成節，不能被合併"
    assert len({s.char_span for s in results}) == 3, "同名節的 span 必須互異"

    # 每個「測試結果」節都落在自己那個 Chapter 之後
    chapters = [s for s in sections if s.title.startswith("Chapter ")]
    assert len(chapters) == 3
    for chapter, result in zip(chapters, results):
        assert chapter.char_span[1] <= result.char_span[0]
        assert raw[result.char_span[0]:result.char_span[1]].startswith("## 測試結果")


def test_text_before_first_heading_becomes_a_preamble_section():
    raw = normalize_document_text("intro line without heading\n\n# Chapter 1\nbody")
    sections = extract_sections(raw)

    assert sections[0].title == ""
    assert sections[0].level == 0
    assert sections[0].char_span[0] == 0
    assert sections[1].title == "Chapter 1"


def test_document_without_headings_has_no_sections_but_stays_usable(tmp_path):
    document = _chunked_doc(tmp_path, _filler("plain", 400), name="notes.md")

    assert all(s.title == "" for s in document.sections)
    assert all(chunk["section"] == "" for chunk in document.chunks)
    assert document.chunks, "沒有標題不代表沒有內容"


def test_empty_extraction_returns_a_safe_empty_document(tmp_path):
    missing = RAG.process_file_document(str(tmp_path / "nope.md"))

    assert missing.raw_text == ""
    assert missing.chunks == []
    assert missing.sections == []
    assert missing.section_index_for_offset(0) == -1
    assert missing.section_text(0) == ""
    missing.assign_section_indices()  # 不得炸


# ============================================================
# chunk ↔ 文件座標
# ============================================================
def test_chunk_span_locates_its_body_inside_raw_text(tmp_path):
    document = _chunked_doc(tmp_path, _repeated_heading_doc())

    assert len(document.chunks) > 1, "語料要真的被切開才有意義"
    for chunk in document.chunks:
        start, end = chunk["char_start"], chunk["char_end"]
        assert 0 <= start <= end <= len(document.raw_text)
        skip = chunk["overlap_prefix_chars"] + chunk["heading_prefix_chars"]
        body = chunk["content"][skip:]
        window = document.raw_text[start:end]
        # span 是「來源行範圍」：正文落在窗內，或窗是正文的一段（表格續行會補表頭）
        assert body in window or window in body


def test_chunk_spans_are_monotonic(tmp_path):
    document = _chunked_doc(tmp_path, _repeated_heading_doc())

    starts = [chunk["char_start"] for chunk in document.chunks]
    assert starts == sorted(starts)


def test_chunk_section_is_the_document_level_truth(tmp_path):
    document = _chunked_doc(tmp_path, _repeated_heading_doc())

    for chunk in document.chunks:
        index = chunk["section_index"]
        assert 0 <= index < len(document.sections)
        assert chunk["section"] == document.sections[index].title
        # chunk 起點必須真的落在它宣稱的那一節裡
        start, end = document.sections[index].char_span
        assert start <= chunk["char_start"] < end


def test_chunks_of_repeated_sections_point_at_different_occurrences(tmp_path):
    document = _chunked_doc(tmp_path, _repeated_heading_doc())

    indices = {
        chunk["section_index"]
        for chunk in document.chunks
        if chunk["section"] == "測試結果"
    }
    assert len(indices) == 3, (
        "三個同名節的 chunk 必須指向三個不同的 section index，"
        f"實際只有 {sorted(indices)}"
    )


# ============================================================
# PDF：頁 span 與跨頁章節
# ============================================================
def test_pdf_page_spans_reconstruct_raw_text(monkeypatch):
    pages = [
        {"metadata": {"page_number": 1}, "text": f"# Chapter 1\n{_filler('p1', 150)}"},
        {"metadata": {"page_number": 2}, "text": f"## 測試結果\n{_filler('p2', 150)}"},
        {"metadata": {"page_number": 3}, "text": _filler("p3", 150)},
    ]
    _fake_pdf(monkeypatch, pages)

    document = RAG.extract_pdf_document("spec_a.pdf")

    assert [page for page, _, _ in document.page_spans] == [1, 2, 3]
    rebuilt = PAGE_SEPARATOR.join(
        document.raw_text[start:end] for _, start, end in document.page_spans
    )
    assert rebuilt == document.raw_text


def test_pdf_chunk_span_lands_on_its_own_page(monkeypatch):
    pages = [
        {"metadata": {"page_number": n}, "text": f"## 測試結果\n{_filler(f'p{n}', 300)}"}
        for n in range(1, 6)
    ]
    _fake_pdf(monkeypatch, pages)

    document = RAG.extract_pdf_document("spec_a.pdf")

    assert document.chunks
    for chunk in document.chunks:
        assert document.page_for_offset(chunk["char_start"]) == chunk["page"]


def test_pdf_section_page_range_covers_every_page_it_spans(monkeypatch):
    pages = [
        {"metadata": {"page_number": 1}, "text": f"# Chapter 1\n{_filler('p1', 150)}"},
        {"metadata": {"page_number": 2}, "text": _filler("p2", 150)},
        {"metadata": {"page_number": 3}, "text": f"# Chapter 2\n{_filler('p3', 150)}"},
    ]
    _fake_pdf(monkeypatch, pages)

    document = RAG.extract_pdf_document("spec_a.pdf")

    by_title = {section.title: section for section in document.sections}
    assert by_title["Chapter 1"].page_range == (1, 2), "跨頁章節要涵蓋到第 2 頁"
    assert by_title["Chapter 2"].page_range == (3, 3)


def test_short_page_is_not_filed_under_the_previous_pages_section(monkeypatch):
    """回歸：整頁塞得進一個 chunk 時，舊碼會把它歸到上一頁的章節。

    splitter 對「整份文字 <= chunk 上限」的輸入直接回一個 section 為空的 chunk，
    舊 PDF 路徑就用 last_section 補；而 last_section 只在 chunk 帶了非空 section
    時才更新，於是連續短頁之後它會卡在過期章節上。這裡第 3 頁開頭就是自己的
    `## 測試結果`，不該被記成 `Register Map`。
    """
    pages = [
        {"metadata": {"page_number": 1}, "text": f"# Chapter 1\n{_filler('p1', 200)}"},
        {"metadata": {"page_number": 2}, "text": "## Register Map\nshort body"},
        {"metadata": {"page_number": 3}, "text": "## 測試結果\nall cases passed"},
    ]
    _fake_pdf(monkeypatch, pages)

    document = RAG.extract_pdf_document("spec_a.pdf")

    page3 = [chunk for chunk in document.chunks if chunk["page"] == 3]
    assert page3, "第 3 頁必須有 chunk"
    assert all(chunk["section"] == "測試結果" for chunk in page3), (
        f"第 3 頁被歸到 {sorted({c['section'] for c in page3})}"
    )
    page2 = [chunk for chunk in document.chunks if chunk["page"] == 2]
    assert all(chunk["section"] == "Register Map" for chunk in page2)


# ============================================================
# 入口一致性
# ============================================================
_REQUIRED_CHUNK_KEYS = {
    "source", "page", "chunk_index", "content", "type", "section",
    "heading_hierarchy", "overlap_prefix_chars", "heading_prefix_chars",
    "char_start", "char_end", "section_index",
}


def test_every_entry_produces_the_same_chunk_shape(tmp_path, monkeypatch):
    text = _repeated_heading_doc()

    documents = {
        "text": _chunked_doc(tmp_path, text),
        "chat": RAG.build_chat_document("session_a.png", text),
        "image": RAG.build_image_document("diagram_a.png", text),
        "url": RAG.build_url_document(
            "https://example.invalid/toolchain_x", text, "Toolchain X", "2026-01-01T00:00:00"
        ),
    }
    _fake_pdf(monkeypatch, [{"metadata": {"page_number": 1}, "text": text}])
    documents["pdf"] = RAG.extract_pdf_document("spec_a.pdf")

    for label, document in documents.items():
        assert document.chunks, f"{label} 沒有產出 chunk"
        for chunk in document.chunks:
            missing = _REQUIRED_CHUNK_KEYS - set(chunk)
            assert not missing, f"{label} chunk 缺欄位: {sorted(missing)}"


def test_vl_and_url_entries_keep_their_origin_markers(tmp_path):
    text = _repeated_heading_doc()

    assert {c["origin"] for c in RAG.build_chat_document("a.png", text).chunks} == {"screenshot"}
    assert {c["origin"] for c in RAG.build_image_document("a.png", text).chunks} == {"image"}
    url_chunks = RAG.build_url_document(
        "https://example.invalid/x", text, "T", "2026-01-01T00:00:00"
    ).chunks
    assert {c["origin"] for c in url_chunks} == {"url"}
    assert {c["url"] for c in url_chunks} == {"https://example.invalid/x"}


def test_compat_wrappers_return_the_documents_chunks(tmp_path, monkeypatch):
    target = tmp_path / "spec_a.md"
    target.write_text(_repeated_heading_doc(), encoding="utf-8")

    assert RAG.extract_text_file(str(target)) == RAG.extract_text_file_document(str(target)).chunks
    assert RAG.process_file(str(target)) == RAG.process_file_document(str(target)).chunks

    _fake_pdf(monkeypatch, [{"metadata": {"page_number": 1}, "text": _filler("p", 300)}])
    assert RAG.extract_pdf("spec_a.pdf") == RAG.extract_pdf_document("spec_a.pdf").chunks


def test_process_url_wrapper_still_returns_chunks_and_url_name(monkeypatch):
    monkeypatch.setattr(
        RAG, "fetch_url_content", lambda url: (_repeated_heading_doc(), "Toolchain X")
    )

    result = RAG.process_url("https://example.invalid/toolchain_x")

    assert result is not None
    chunks, url_name = result
    assert chunks and url_name
    assert all(chunk["source"] == f"url_{url_name}" for chunk in chunks)


def test_failed_url_fetch_returns_none(monkeypatch):
    monkeypatch.setattr(RAG, "fetch_url_content", lambda url: ("", ""))

    assert RAG.process_url_document("https://example.invalid/x") is None
    assert RAG.process_url("https://example.invalid/x") is None


# ============================================================
# 座標系契約
# ============================================================
@pytest.mark.parametrize("chunk_size", [300, 800, 1200])
def test_pre_normalized_input_yields_the_same_chunks(chunk_size):
    text = _repeated_heading_doc()
    raw = normalize_document_text(text)

    direct = RAG.split_by_semantic_with_sections(text, max_chars=chunk_size)
    prepared = RAG.split_by_semantic_with_sections(
        raw, max_chars=chunk_size, pre_normalized=True
    )

    assert direct == prepared, "pre_normalized 只是省掉重複正規化，結果必須完全一致"


def test_raw_text_is_the_text_the_splitter_actually_saw(tmp_path):
    # 表格會被 normalize 改寫；raw_text 必須是改寫後的版本，offset 才對得上
    text = "# Chapter 1\n\n| Field | Value |\n" + _filler("body", 300)
    document = _chunked_doc(tmp_path, text)

    assert document.raw_text == normalize_document_text(text)
    assert "Field: Value" in document.raw_text


# ============================================================
# 標題偵測（G2）：條列項不是標題
# ============================================================
@pytest.mark.parametrize(
    "line",
    [
        "2. Power on the HAPS system.",
        "1. L2 CPU selftest: DM, CSM, XM",
        "3. STU copy test: XM => VCCM, CSM => VCCM",
        "1. For the fastest response, enter a case through SolvNetPlus: https://example.invalid",
        "2. Insert the adapter into the J22 socket on the GPIO card.",
        "1. Go to https://example.invalid.",
        "2. Set the environment variables for enabling the bus with the debugger:",
    ],
)
def test_numbered_list_items_are_not_headings(line):
    """實測三份真實 spec：舊規則命中 73 次，沒有一次是真標題，全是這種條列項。

    它們變成 section 之後會污染 [SECTION_METADATA] 與 [HEADING]，還多切一堆
    chunk 邊界。
    """
    assert extracted_document.is_heading(line) is False
    assert extracted_document.extract_section_title(line) == ""


@pytest.mark.parametrize(
    "line",
    [
        "2.3 Methods",
        "1.1.4 Results",
        "5.8 MEM access path",
        "1. Introduction",          # 純文字文件的單層章節：夠短、無冒號、無句尾標點
        "3. Configuration",
    ],
)
def test_real_numbered_headings_still_detected(line):
    """規則不能刪：沒有 markdown 結構的純文字文件要靠它。"""
    assert extracted_document.is_heading(line) is True
    assert extracted_document.extract_section_title(line) == line


@pytest.mark.parametrize(
    "line,expected",
    [
        ("SYSTEM CONTROL REGISTER", True),
        ("MEMORY MAP", True),
        ("PASS", False),                              # 單一個詞是結論，不是標題
        ("PASS 畫面截圖如下：", False),                 # CJK 沒有大小寫，不該算 ALL CAPS
        ("以下針對 NPX/VPX 測試項目逐一說明：", False),
        ("OK", False),                                # 太短
    ],
)
def test_allcaps_rule_is_not_fooled_by_mixed_scripts(line, expected):
    """`str.isupper()` 對中英混排太寬：拉丁部分大寫就整行算 ALL CAPS。"""
    assert extracted_document.is_heading(line) is expected


def test_heading_detection_and_title_extraction_never_disagree():
    """is_heading 為真卻抽不出標題名 = 靜默的空 section。兩者共用同一組 helper。"""
    lines = [
        "# Chapter 1", "## 1.2 Core control", "#### 流程：",
        "SYSTEM CONTROL REGISTER", "PASS", "2.3 Methods", "1. Introduction",
        "2. Power on the HAPS system.", "1. L2 CPU selftest: DM, CSM, XM",
        "plain body text", "", "   ",
    ]
    for line in lines:
        detected = extracted_document.is_heading(line)
        title = extracted_document.extract_section_title(line)
        assert detected == bool(title), f"{line!r}: is_heading={detected} title={title!r}"


def test_list_items_no_longer_fragment_a_section(tmp_path):
    """條列項不再切斷章節：整個步驟清單留在它所屬的節裡。"""
    text = "\n".join([
        "## 5.2 Running a Test Application",
        _filler("intro", 60),
        "",
        "1. Set up the environment by launching the setup script.",
        "2. Compile the example or model.",
        "3. Invoke the build system command to run the test application.",
        "",
        _filler("tail", 60),
    ])
    document = _chunked_doc(tmp_path, text)

    titles = [s.title for s in document.sections if s.title]
    assert titles == ["5.2 Running a Test Application"]
    assert all(c["section"] == "5.2 Running a Test Application" for c in document.chunks)


# ── 原 test_context_generation.py:chunk 脈絡生成端——窗預算、快取、鎖、輸出衛生、端點安全 ──
# ============================================================
# 假的 HTTP 層
# ============================================================
class _FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeSession:
    """記錄呼叫參數的假 session。"""

    def __init__(self, response=None):
        self.trust_env = True
        self.max_redirects = 30
        self.response = response or _FakeResponse()
        self.calls: list[dict] = []

    def request(self, method, url, *, json=None, timeout=None, allow_redirects=None):
        self.calls.append({
            "method": method, "url": url, "json": json,
            "timeout": timeout, "allow_redirects": allow_redirects,
        })
        return self.response

    def close(self):
        pass


def _completion(content: str, finish_reason: str = "stop", cached: int = 0) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        "timings": {"cache_n": cached, "prompt_n": 100},
    }


def _install_fake_transport(monkeypatch, model_file: Path, responses, *, n_ctx: int = 32768):
    """把 `_request_json` 換掉：/props 回身份，/chat 依序回 responses。"""
    state = {"index": 0, "chat_calls": 0}
    props = {
        "model_path": str(model_file),
        "model_alias": "test-model",
        "build_info": "b-test",
        "default_generation_settings": {"n_ctx": n_ctx},
    }

    def fake(session, method, url, *, timeout, json_body=None):
        cg.ensure_endpoint_allowed(url)
        if url.endswith("/props"):
            return props
        state["chat_calls"] += 1
        index = min(state["index"], len(responses) - 1)
        state["index"] += 1
        return responses[index]

    monkeypatch.setattr(cg, "_request_json", fake)
    monkeypatch.setattr(cg, "_restricted_session", lambda: _FakeSession())
    return state


def _document(chunks: int = 3, *, body: str = "原文") -> ExtractedDocument:
    raw = "\n".join(f"# 第 {i} 節\n{body}{i}" for i in range(chunks))
    sections = [
        Section(f"第 {i} 節", 1, (i * 20, (i + 1) * 20), (1, 1)) for i in range(chunks)
    ]
    return ExtractedDocument(
        raw_text=raw,
        sections=sections,
        chunks=[
            {"source": "spec_a.md", "page": 1, "chunk_index": i, "content": f"{body}{i}",
             "section": f"第 {i} 節", "section_index": i,
             "char_start": i * 20, "char_end": (i + 1) * 20}
            for i in range(chunks)
        ],
        source="spec_a.md",
        page_spans=[(1, 0, len(raw))],
    )


@pytest.fixture
def model_file(tmp_path: Path) -> Path:
    path = tmp_path / "model.gguf"
    path.write_bytes(b"gguf" * 16)
    return path


# ============================================================
# 端點安全（§13）
# ============================================================
@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True), ("127.5.5.5", True), ("::1", True), ("[::1]", True),
        ("localhost", True), ("0:0:0:0:0:0:0:1", True),
        ("10.0.0.1", False), ("example.com", False), ("", False),
    ],
)
def test_loopback_detection_is_dual_stack(host, expected):
    assert cg.is_loopback_host(host) is expected


def test_remote_endpoint_needs_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(config, "KB_CONTEXT_REMOTE_OK", False)

    with pytest.raises(cg.ContextGenerationError) as exc:
        cg.ensure_endpoint_allowed("http://10.0.0.5:8080")

    message = str(exc.value)
    assert "kb_context_remote_ok" in message
    assert "整份文件" in message, "錯誤訊息必須講明會外送什麼"


def test_remote_endpoint_allowed_with_opt_in(monkeypatch):
    monkeypatch.setattr(config, "KB_CONTEXT_REMOTE_OK", True)

    cg.ensure_endpoint_allowed("http://10.0.0.5:8080")  # 不得 raise


def test_restricted_session_ignores_environment_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://evil.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://evil.invalid:3128")

    session = cg._restricted_session()

    assert session.trust_env is False, "context client 必須不讀環境 proxy"
    assert session.max_redirects == 0


def test_redirect_is_treated_as_an_error():
    session = _FakeSession(_FakeResponse(307, headers={"Location": "http://evil.invalid/"}))

    with pytest.raises(cg.ContextGenerationError, match="redirect"):
        cg._request_json(session, "POST", "http://127.0.0.1:8080/x", timeout=5, json_body={})

    assert session.calls[0]["allow_redirects"] is False


def test_http_error_is_transport_failure():
    session = _FakeSession(_FakeResponse(500))

    with pytest.raises(cg.ContextGenerationError, match="HTTP 500"):
        cg._request_json(session, "POST", "http://127.0.0.1:8080/x", timeout=5, json_body={})


# ============================================================
# 快取（§11、§13）
# ============================================================
def test_cache_files_are_hashed_and_private(tmp_path: Path):
    root = cg.cache_root_for(tmp_path / "knowledge.json", str(tmp_path / "cachebase"))
    cache = cg.ContextCache(root)

    cache.put("a" * 64, "some ctx", {"kind": "chunk"})

    entries = list(root.iterdir())
    assert len(entries) == 1
    assert "knowledge" not in entries[0].name and "spec_a" not in entries[0].name
    assert stat.S_IMODE(entries[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_cache_root_name_does_not_contain_the_source_path(tmp_path: Path):
    root = cg.cache_root_for(tmp_path / "secret_project" / "knowledge.json", str(tmp_path / "c"))

    assert "secret_project" not in str(root)


def test_symlink_escape_is_refused(tmp_path: Path):
    root = tmp_path / "cache"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    cache = cg.ContextCache(root)
    fingerprint = "b" * 64
    (root / f"{fingerprint}.json").symlink_to(outside / "escaped.json")

    with pytest.raises(cg.ContextGenerationError, match="escapes"):
        cache.put(fingerprint, "ctx")

    assert not (outside / "escaped.json").exists()


def test_write_through_checkpoint_persists_each_entry(tmp_path: Path, monkeypatch, model_file):
    _install_fake_transport(monkeypatch, model_file, [_completion("脈絡一"), _completion("脈絡二")])
    document = _document(2)

    cg.generate_document_context(
        document, kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "cache")
    )

    root = cg.cache_root_for(tmp_path / "knowledge.json", str(tmp_path / "cache"))
    entries = [p for p in root.iterdir() if p.suffix == ".json"]
    assert len(entries) == 2, "每個 chunk 成功就該落盤，不是整批結束才寫"


def test_unchanged_document_costs_zero_llm_calls(tmp_path: Path, monkeypatch, model_file):
    state = _install_fake_transport(monkeypatch, model_file, [_completion("脈絡")])
    kb_path = tmp_path / "knowledge.json"

    cg.generate_document_context(_document(3), kb_path=kb_path, cache_dir=str(tmp_path / "c"))
    first_calls = state["chat_calls"]
    report = cg.generate_document_context(
        _document(3), kb_path=kb_path, cache_dir=str(tmp_path / "c")
    )

    assert first_calls == 3
    assert state["chat_calls"] == first_calls, "重跑同一份文件不得再呼叫模型"
    assert report.llm_calls == 0 and report.cache_hits == 3


def test_fingerprint_changes_when_the_model_file_changes(tmp_path: Path, model_file):
    messages = [{"role": "user", "content": "x"}]
    params = {"temperature": 0}
    before = cg.generation_fingerprint(
        messages=messages, params=params, kind="chunk",
        identity={"model_path": str(model_file),
                  "model_file": {"size": 64, "mtime_ns": 111}},
    )
    after = cg.generation_fingerprint(
        messages=messages, params=params, kind="chunk",
        identity={"model_path": str(model_file),
                  "model_file": {"size": 65, "mtime_ns": 222}},
    )

    assert before != after, "同路徑換掉模型檔必須讓指紋失效"


def test_model_identity_records_file_size_and_mtime(tmp_path: Path, monkeypatch, model_file):
    _install_fake_transport(monkeypatch, model_file, [])

    identity = cg.model_identity("http://127.0.0.1:8080")

    assert identity["model_path"] == str(model_file)
    assert identity["model_file"]["size"] == model_file.stat().st_size
    assert identity["model_file"]["mtime_ns"] == model_file.stat().st_mtime_ns


def test_summary_fingerprint_depends_on_child_summaries():
    messages = [{"role": "user", "content": "same"}]
    identity = {"model_path": "m", "model_file": None}
    first = cg.generation_fingerprint(
        messages=messages, params={}, identity=identity, kind="summary",
        extra={"level": 1, "order": 0, "span": [0, 10], "children": ["aaa"]},
    )
    second = cg.generation_fingerprint(
        messages=messages, params={}, identity=identity, kind="summary",
        extra={"level": 1, "order": 0, "span": [0, 10], "children": ["bbb"]},
    )

    assert first != second, "下層摘要變了，上層必須失效"


# ============================================================
# single-writer 鎖
# ============================================================
def test_second_writer_fails_loudly(tmp_path: Path):
    root = tmp_path / "cache"
    first = cg.SingleWriterLock(root)
    first.acquire()

    with pytest.raises(cg.ContextLockError, match="rebuild"):
        cg.SingleWriterLock(root).acquire()

    first.release()
    cg.SingleWriterLock(root).acquire()  # 釋放後可以取得


def test_lock_left_behind_by_a_dead_process_is_reusable(tmp_path: Path):
    """行程死掉之後 kernel 就把 flock 釋放了，殘留的鎖檔不該卡住任何人。"""
    root = tmp_path / "cache"
    root.mkdir(parents=True)
    lock_path = root / ".writer.lock"
    lock_path.write_text(json.dumps({"pid": 2 ** 22, "started_at": 0}), encoding="utf-8")

    lock = cg.SingleWriterLock(root)
    lock.acquire()

    assert json.loads(lock_path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    lock.release()


def test_empty_or_garbage_lock_file_does_not_confuse_acquire(tmp_path: Path):
    """鎖檔內容壞掉／空的只影響診斷訊息，不影響互斥。"""
    root = tmp_path / "cache"
    root.mkdir(parents=True)
    (root / ".writer.lock").write_text("", encoding="utf-8")

    first = cg.SingleWriterLock(root)
    first.acquire()
    try:
        with pytest.raises(cg.ContextLockError):
            cg.SingleWriterLock(root).acquire()
    finally:
        first.release()


# ============================================================
# 窗預算（§10）
# ============================================================
def test_window_budget_subtracts_reserved_output_tokens(monkeypatch):
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 4096)
    monkeypatch.setattr(config, "KB_CONTEXT_WINDOW_SAFETY", 1.0)

    budget = cg.window_budget_tokens(10000, template_tokens=100, chunk_tokens=400)

    assert budget == 10000 - 100 - 400 - 4096


def test_smaller_n_ctx_shrinks_the_window(monkeypatch):
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 1000)
    monkeypatch.setattr(config, "KB_CONTEXT_WINDOW_SAFETY", 0.8)

    big = cg.window_budget_tokens(100000, template_tokens=10, chunk_tokens=10)
    small = cg.window_budget_tokens(8192, template_tokens=10, chunk_tokens=10)

    assert small < big
    assert small == int(8192 * 0.8) - 10 - 10 - 1000


def test_section_window_expands_to_neighbours_until_the_budget_is_full():
    raw = "".join(f"# 第 {i} 節\n" + ("內容" * 10) + "\n" for i in range(5))
    spans, cursor = [], 0
    for i in range(5):
        block = f"# 第 {i} 節\n" + ("內容" * 10) + "\n"
        spans.append(Section(f"第 {i} 節", 1, (cursor, cursor + len(block))))
        cursor += len(block)
    document = ExtractedDocument(raw_text=raw, sections=spans)
    chunk = {"section_index": 2, "char_start": spans[2].char_span[0], "content": "內容"}

    narrow = cg.build_section_window(document, chunk, 40)
    wide = cg.build_section_window(document, chunk, len(raw))

    assert "第 2 節" in narrow
    assert len(wide) > len(narrow)
    assert "第 1 節" in wide and "第 3 節" in wide


def test_oversized_single_section_falls_back_to_a_chunk_centred_window():
    heading = "# 超大節\n"
    body = "".join(f"行{i:04d}\n" for i in range(500))
    raw = heading + body
    document = ExtractedDocument(
        raw_text=raw, sections=[Section("超大節", 1, (0, len(raw)))]
    )
    target = raw.index("行0250")
    chunk = {"section_index": 0, "char_start": target, "content": "行0250"}

    window = cg.build_section_window(document, chunk, 200)

    assert len(window) <= 200 + len(heading)
    assert window.startswith("# 超大節"), "截窗仍要保留該節標題，否則定位任務沒有依據"
    assert "行0250" in window


def test_long_document_takes_the_map_reduce_path(tmp_path: Path, monkeypatch, model_file):
    # 小 n_ctx + 長文件：整份塞不進窗 → 必須先做階層式文件摘要
    state = _install_fake_transport(
        monkeypatch, model_file, [_completion("摘要"), _completion("脈絡")], n_ctx=2048
    )
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 0)
    document = _document(60, body="內容片段" * 30)

    report = cg.generate_document_context(
        document, kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "c")
    )

    assert state["chat_calls"] > len(document.chunks), "長文件必須先做文件級摘要"
    assert report.generated == len(document.chunks)


def test_every_call_passes_through_the_context_budget_gate(
    tmp_path: Path, monkeypatch, model_file
):
    _install_fake_transport(monkeypatch, model_file, [_completion("脈絡")])
    seen: list[str] = []
    real = context_budget.check_and_log

    def spy(**kwargs):
        seen.append(kwargs["source"])
        return real(**kwargs)

    monkeypatch.setattr(context_budget, "check_and_log", spy)

    cg.generate_document_context(
        _document(2), kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "c")
    )

    assert seen == ["kb_context", "kb_context"]


# ============================================================
# 生成與降級
# ============================================================
def test_empty_response_retries_once_then_records_absent(
    tmp_path: Path, monkeypatch, model_file
):
    state = _install_fake_transport(
        monkeypatch, model_file,
        [_completion(""), _completion(""), _completion("第二個 chunk 的脈絡")],
    )
    monkeypatch.setattr(config, "KB_CONTEXT_MAX_ABSENT_RATIO", 0.9)
    document = _document(2)

    report = cg.generate_document_context(
        document, kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "c")
    )

    assert state["chat_calls"] == 3, "空回應要重試一次，成功的不重試"
    assert report.absent == 1
    assert report.absent_reasons == {"empty_response": 1}
    assert document.chunks[0]["ctx"] == ""
    assert document.chunks[0]["ctx_meta"]["absent_reason"] == "empty_response"


def test_length_exhausted_is_recorded_separately(tmp_path: Path, monkeypatch, model_file):
    """推理模型把額度用在 reasoning 上而沒吐出 content 的樣子要分開記。"""
    _install_fake_transport(monkeypatch, model_file, [_completion("", finish_reason="length")])
    monkeypatch.setattr(config, "KB_CONTEXT_MAX_ABSENT_RATIO", 1.0)

    report = cg.generate_document_context(
        _document(1), kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "c")
    )

    assert report.absent_reasons == {"length_exhausted": 1}


def test_low_coverage_aborts_the_publish(tmp_path: Path, monkeypatch, model_file):
    _install_fake_transport(monkeypatch, model_file, [_completion("")])
    monkeypatch.setattr(config, "KB_CONTEXT_MAX_ABSENT_RATIO", 0.2)

    with pytest.raises(cg.ContextCoverageError, match="覆蓋率"):
        cg.generate_document_context(
            _document(3), kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "c")
        )


def test_transport_failure_stops_the_whole_batch(tmp_path: Path, monkeypatch, model_file):
    props = {"model_path": str(model_file), "default_generation_settings": {"n_ctx": 32768}}

    def fake(session, method, url, *, timeout, json_body=None):
        if url.endswith("/props"):
            return props
        raise cg.ContextGenerationError("server unreachable")

    monkeypatch.setattr(cg, "_request_json", fake)
    monkeypatch.setattr(cg, "_restricted_session", lambda: _FakeSession())

    with pytest.raises(cg.ContextGenerationError, match="unreachable"):
        cg.generate_document_context(
            _document(3), kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "c")
        )


def test_vl_chunks_are_skipped(tmp_path: Path, monkeypatch, model_file):
    state = _install_fake_transport(monkeypatch, model_file, [_completion("脈絡")])
    document = _document(2)
    document.chunks[0]["origin"] = "screenshot"

    report = cg.generate_document_context(
        document, kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "c")
    )

    assert report.skipped == 1
    assert state["chat_calls"] == 1
    assert "ctx" not in document.chunks[0], "VL 產物不 contextualize（生成疊生成）"


def test_generated_ctx_never_touches_content(tmp_path: Path, monkeypatch, model_file):
    _install_fake_transport(monkeypatch, model_file, [_completion("這是生成的脈絡")])
    document = _document(1)
    original = document.chunks[0]["content"]

    cg.generate_document_context(
        document, kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "c")
    )

    assert document.chunks[0]["content"] == original
    assert document.chunks[0]["ctx"] == "這是生成的脈絡"


# ============================================================
# 輸出衛生
# ============================================================
def test_sanitize_strips_control_chars_and_folds_newlines():
    assert cg.sanitize_ctx("第一行\x00\n第二行\x07", max_chars=100) == "第一行 第二行"


def test_sanitize_truncates_at_a_sentence_boundary():
    text = "第一句話結束。第二句話也結束。第三句話會被截掉因為超過長度限制了"

    out = cg.sanitize_ctx(text, max_chars=16)

    assert out.endswith("。")
    assert len(out) <= 16


def test_sanitize_falls_back_to_hard_cut_without_a_boundary():
    out = cg.sanitize_ctx("一" * 50, max_chars=10)

    assert len(out) == 10


# ============================================================
# GPT review 回歸（2026-08-18）
# ============================================================
def test_live_writer_is_never_stolen_no_matter_how_long_it_runs(tmp_path: Path):
    """活著的 writer 不得被奪鎖——不管它跑多久、多久沒有動靜。

    rebuild 本來就會有長停頓（單次大窗呼叫、機器負載、SIGSTOP），停頓不等於死亡。
    這裡刻意**不**做任何續期，只把鎖檔的 mtime 推到兩小時前：互斥由 flock 認定，
    跟時間無關。
    """
    root = tmp_path / "cache"
    holder = cg.SingleWriterLock(root)
    holder.acquire()
    two_hours_ago = time.time() - 7200
    os.utime(holder.path, (two_hours_ago, two_hours_ago))

    try:
        with pytest.raises(cg.ContextLockError):
            cg.SingleWriterLock(root).acquire()
        assert holder.held
    finally:
        holder.release()


def test_release_does_not_unlink_the_lock_file(tmp_path: Path):
    """釋放不刪檔：刪掉會讓正在 open 但還沒 flock 的人抓到孤兒 inode。"""
    root = tmp_path / "cache"
    first = cg.SingleWriterLock(root)
    first.acquire()
    first.release()

    assert first.path.exists()
    second = cg.SingleWriterLock(root)
    second.acquire()   # 釋放之後別人拿得到
    second.release()


def test_two_locks_are_never_held_at_the_same_time(tmp_path: Path):
    root = tmp_path / "cache"
    first = cg.SingleWriterLock(root)
    second = cg.SingleWriterLock(root)
    first.acquire()
    try:
        with pytest.raises(cg.ContextLockError):
            second.acquire()
        assert first.held and not second.held
    finally:
        first.release()


def test_dead_holder_is_still_taken_over(tmp_path: Path):
    root = tmp_path / "cache"
    root.mkdir(parents=True)
    (root / ".writer.lock").write_text(
        json.dumps({"pid": 2 ** 22, "started_at": 0, "token": "old"}), encoding="utf-8"
    )

    cg.SingleWriterLock(root).acquire()  # 持有者已死 → 接手，不得卡住


def test_summary_fingerprint_tracks_the_real_request_budget(
    tmp_path: Path, monkeypatch, model_file
):
    """摘要指紋要記實際送出的 max_tokens，不是窗預算。

    請求端另外加了 reasoning 額度；指紋若只記 budget_tokens，調大 reasoning
    budget 之後仍會命中舊摘要，等於改了生成參數卻沒失效。
    """
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 0)
    monkeypatch.setattr(config, "KB_CONTEXT_REASONING_TOKENS", 512)
    state = _install_fake_transport(
        monkeypatch, model_file, [_completion("摘要"), _completion("脈絡")], n_ctx=2048
    )
    document = _document(60, body="內容片段" * 30)
    kb_path = tmp_path / "knowledge.json"
    cache_dir = str(tmp_path / "c")

    def _summary_entries() -> int:
        root = cg.cache_root_for(kb_path, cache_dir)
        return sum(
            1 for path in root.iterdir()
            if path.suffix == ".json"
            and json.loads(path.read_text(encoding="utf-8")).get("meta", {}).get("kind")
            == "summary"
        )

    cg.generate_document_context(document, kb_path=kb_path, cache_dir=cache_dir)
    before = _summary_entries()
    assert before, "第一次 rebuild 應該留下摘要快取"

    # 只改 reasoning 額度：實際送出的 max_tokens 變了，摘要指紋必須跟著失效
    monkeypatch.setattr(config, "KB_CONTEXT_REASONING_TOKENS", 2048)
    cg.generate_document_context(_document(60, body="內容片段" * 30),
                                 kb_path=kb_path, cache_dir=cache_dir)

    assert _summary_entries() > before, (
        "調整生成參數之後摘要仍命中舊快取（指紋沒涵蓋實際送出的 max_tokens）"
    )
    assert state["chat_calls"] > 0


def test_empty_summary_is_retried_and_not_cached(tmp_path: Path, monkeypatch, model_file):
    """空摘要不進快取：記下來等於毒化之後每一次 rebuild。"""
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 0)
    state = _install_fake_transport(
        monkeypatch, model_file, [_completion(""), _completion(""), _completion("脈絡")],
        n_ctx=2048,
    )
    document = _document(60, body="內容片段" * 30)

    cg.generate_document_context(
        document, kb_path=tmp_path / "knowledge.json", cache_dir=str(tmp_path / "c")
    )

    root = cg.cache_root_for(tmp_path / "knowledge.json", str(tmp_path / "c"))
    cached = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in root.iterdir() if p.suffix == ".json"
    ]
    assert cached, "chunk 的 ctx 還是要落盤"
    empty_summaries = [
        entry for entry in cached
        if entry.get("meta", {}).get("kind") == "summary" and not entry.get("value")
    ]
    assert not empty_summaries, "空摘要被寫進快取了（會毒化之後每一次 rebuild）"
    assert state["chat_calls"] >= 2, "空摘要至少要重試一次"


def test_lock_refuses_a_symlinked_lock_file(tmp_path: Path):
    """鎖檔是 symlink 時不得跟過去。

    沒有 O_NOFOLLOW 的話，把 .writer.lock 指向任何可寫檔案，取得鎖之後的
    ftruncate + 寫 PID JSON 就會把那個檔案洗掉。
    """
    root = tmp_path / "cache"
    root.mkdir()
    victim = tmp_path / "victim.txt"
    original = "不該被動到的內容\n第二行"
    victim.write_text(original, encoding="utf-8")
    (root / ".writer.lock").symlink_to(victim)

    with pytest.raises(cg.ContextLockError):
        cg.SingleWriterLock(root).acquire()

    assert victim.read_text(encoding="utf-8") == original, "symlink 目標被改寫了"


def test_lock_refuses_a_non_regular_lock_file(tmp_path: Path):
    """FIFO / device 之類同樣不能當鎖檔。"""
    root = tmp_path / "cache"
    root.mkdir()
    os.mkfifo(root / ".writer.lock")

    with pytest.raises(cg.ContextLockError):
        cg.SingleWriterLock(root).acquire()


# ── 總審 F1-14:預設 cache 位置是函式,呼叫端要真的呼叫它 ──


@pytest.mark.smoke
def test_the_default_context_cache_root_resolves_without_an_explicit_dir(tmp_path, monkeypatch):
    """`python3 RAG.py rebuild … --context` 不傳 cache_dir 就走預設。

    `config.KB_CONTEXT_CACHE_DIR` 改成函式之後,`Path(base_dir or config.KB_CONTEXT_CACHE_DIR)`
    收到的是 function object → 在任何模型呼叫前就 `TypeError`。測試全部顯式傳
    `cache_dir`,所以沒有人踩到預設路徑。
    """
    import context_generation

    monkeypatch.setenv("HOME", str(tmp_path))
    root = context_generation.cache_root_for(tmp_path / "knowledge.json")
    assert isinstance(root, Path)
    assert str(root).startswith(str(tmp_path)), root


# ── 總審 F1-15:RAG.py 自己套用 client.json(預設 HOME;可用 --client-config 指定)──


@pytest.mark.smoke
def test_rag_cli_applies_the_client_config_it_is_given(tmp_path, monkeypatch):
    import RAG
    import config

    cfg = tmp_path / "client.json"
    cfg.write_text(json.dumps({"schema": 1, "compaction_mode": "manual", "objdump": "/opt/bin/objdump"}), encoding="utf-8")
    cfg.chmod(0o600)
    tmp_path.chmod(0o700)
    rest, path = RAG._split_client_config(["--client-config", str(cfg), "doc.pdf", "kb.json"])
    assert rest == ["doc.pdf", "kb.json"] and path == str(cfg)
    monkeypatch.setattr(config, "OBJDUMP", "")
    RAG._apply_client_settings(path)
    assert config.OBJDUMP == "/opt/bin/objdump"
    # 壞掉的檔 fail-loud(與客戶端、MCP 同一個判準)。
    cfg.write_text("{ not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        RAG._apply_client_settings(str(cfg))


# ── 總審 NON-BLOCKER 4:RAG 的 --client-config 與 MCP parser 同等嚴格 ──


@pytest.mark.smoke
def test_rag_client_config_flag_is_as_strict_as_the_mcp_parser(monkeypatch):
    """重複旗標 last-wins、把下一個旗標吃成路徑、空值當沒給 —— 都是「使用者以為
    套了那一份設定、實際套了另一份 / 沒套」的無聲漂移。與 `mcp_server.py` 一樣 exit 2。"""
    import RAG

    for argv in (
        ["spec.pdf", "--client-config"],
        ["spec.pdf", "--client-config", "--fresh"],
        ["spec.pdf", "--client-config="],
        ["spec.pdf", "--client-config", "a.json", "--client-config", "b.json"],
    ):
        with pytest.raises(SystemExit) as exc:
            RAG._split_client_config(list(argv))
        assert exc.value.code == 2, argv
    rest, path = RAG._split_client_config(["spec.pdf", "--client-config", "/tmp/c.json", "--fresh"])
    assert (rest, path) == (["spec.pdf", "--fresh"], "/tmp/c.json")
    # `rebuild` 走 argparse:同判準(重複 / 空值 exit 2),不是 last-wins。必要參數都給齊,
    # 讓 parser 唯一可能的錯就是那個旗標;parse 之後第一步是套 client 設定,用哨兵證明
    # 「合法的單一旗標會走到那裡、重複的不會」。
    class Reached(Exception):
        pass

    def reached(*_a, **_k):
        raise Reached()

    monkeypatch.setattr(RAG, "_apply_client_settings", reached)
    for argv in (
        ["--kb", "k.json", "x.pdf", "--client-config", "a.json", "--client-config", "b.json"],
        ["--kb", "k.json", "x.pdf", "--client-config="],
    ):
        with pytest.raises(SystemExit) as exc:
            RAG.rebuild_cli(list(argv))
        assert exc.value.code == 2, argv
    with pytest.raises(Reached):
        RAG.rebuild_cli(["--kb", "k.json", "x.pdf", "--client-config", "a.json"])
    # 縮寫(`--client-conf`)也不收:MCP 的 parser 是精確比對,這裡不該比較寬。
    with pytest.raises(SystemExit) as exc:
        RAG.rebuild_cli(["--kb", "k.json", "x.pdf", "--client-conf", "a.json"])
    assert exc.value.code == 2
