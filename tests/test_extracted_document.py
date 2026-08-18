"""ExtractedDocument（章節 span / 頁碼 / chunk 定位）的單一真相回歸測試。

背景：切完 chunk 之後「完整文件 + 章節範圍」在舊資料流裡無法無損還原——PDF 逐頁
切、四個入庫入口各自組 chunk dict，章節於是有兩套來源（splitter 的 page-local 追蹤
與呼叫端的 `last_section` 繼承），對不上時沒有仲裁者。實測那個繼承會把「開頭就是
`## 測試結果` 的整頁短 chunk」歸到上一頁的章節，而且錯誤會跨頁累積。

語料全部是合成的（`spec_a.md` / `toolchain_x`），刻意帶重複標題——同一份規格書有
十個「測試結果」節，正是脫離父章節後 chunk 幾乎零鑑別度的動機案例。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import extracted_document
import RAG
from extracted_document import (
    ExtractedDocument,
    PAGE_SEPARATOR,
    extract_sections,
    normalize_document_text,
)


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


class _FakePdfModule:
    """假 pymupdf4llm：to_markdown 直接回預先給定的 page dicts。"""

    def __init__(self, pages):
        self._pages = pages

    def to_markdown(self, *_args, **_kwargs):
        return self._pages


def _fake_pdf(monkeypatch, pages):
    monkeypatch.setattr(RAG, "check_pymupdf4llm", lambda: _FakePdfModule(pages))


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
