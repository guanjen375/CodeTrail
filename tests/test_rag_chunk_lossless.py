"""P0-2：RAG chunk overlap / heading 注入不得毀損原文。

review 重現的資料毀損：
    原文：0123456789 / ABCDEFGHIJ / abcdefghij
    舊輸出：0123456789 / 789+ABCDEFG / EFG+abcdefg   （遺失 HIJ、hij）
overlap 應為純前綴，不得截斷當前 chunk 的正文。
"""
from __future__ import annotations

from RAG import split_by_semantic_with_sections


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
