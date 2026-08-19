"""RAG 切塊契約:不得遺失內容,表頭要在每個切片重複。

合併自 tests/test_rag_chunk_lossless.py 與 tests/test_rag_table_headers.py(2026-08-20)。
"""
from __future__ import annotations

import pytest

import RAG
from RAG import split_by_semantic_with_sections

# smoke:AGENTS.md §2.1 第 1 款「真實發生過的 bug 的 regression」
# 無聲失敗契約:切塊不得遺失內容、表頭要重複。
pytestmark = pytest.mark.smoke

def _strip_prefixes(content: str) -> str:
    """移除 heading 注入行，只留正文（heading 是我們自己加的前綴）。"""
    lines = content.split("\n")
    body = [ln for ln in lines if not ln.startswith("[HEADING]") and not ln.startswith("[SECTION]")]
    return "\n".join(body)


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


def test_every_original_line_survives_chunking():
    """一般不變量：每一行原文都要能在切出來的 chunks 中找回。"""
    # 造出足夠長、且每行有獨特 token 的文件，逼出多個 chunk + overlap
    lines = [f"LINE{i:03d}_" + ("payload" * 20) for i in range(60)]
    text = "\n".join(lines)

    chunks = split_by_semantic_with_sections(text)  # 用預設 CHUNK_SIZE/OVERLAP/heading
    bodies = "\n".join(_strip_prefixes(c["content"]) for c in chunks)

    missing = [ln[:10] for ln in lines if ln not in bodies]
    assert not missing, f"以下原文行在 chunking 後遺失: {missing}"


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
