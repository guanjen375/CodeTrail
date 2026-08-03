from __future__ import annotations

import RAG


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
