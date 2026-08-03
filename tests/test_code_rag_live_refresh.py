from __future__ import annotations

from pathlib import Path

import code_rag


def test_query_refreshes_changed_source_in_same_session(monkeypatch, tmp_path: Path):
    source = tmp_path / "module.py"
    source.write_text("def old_symbol():\n    return 1\n", encoding="utf-8")
    rag = code_rag.CodeRAG(str(tmp_path))
    monkeypatch.setattr(rag, "_get_embedding", lambda _text: [1.0, 0.0])
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", False)
    monkeypatch.setattr(code_rag, "USE_RERANKER", False)

    first = rag.query("old_symbol", top_k=3)
    assert first and first[0]["symbol"] == "old_symbol"

    source.write_text("def new_symbol():\n    return 2\n", encoding="utf-8")
    second = rag.query("new_symbol", top_k=3)

    assert second and second[0]["symbol"] == "new_symbol"
    assert all(row["symbol"] != "old_symbol" for row in second)


def test_lazy_semantic_query_materializes_dense_index_instead_of_arbitrary_slice(monkeypatch, tmp_path: Path):
    rag = code_rag.CodeRAG(str(tmp_path))
    rag.index = [
        {
            "path": f"file_{i}.py",
            "symbol": f"symbol_{i}",
            "type": "class",
            "line": 1,
            "context": "generic plumbing helper",
        }
        for i in range(200)
    ]
    rag.index[-1]["context"] = "coordinates the neural accelerator reset sequence"
    rag._lazy_embed = True
    rag._lazy_embed_top_k = 10
    monkeypatch.setattr(code_rag, "USE_RERANKER", False)

    def fake_embedding(text: str) -> list[float]:
        if text == "如何重新啟動加速器？" or "neural accelerator reset sequence" in text:
            return [1.0, 0.0]
        return [0.0, 1.0]

    monkeypatch.setattr(rag, "_get_embedding", fake_embedding)

    results = rag.query("如何重新啟動加速器？", top_k=5)

    assert any(row["symbol"] == "symbol_199" for row in results)
    assert rag._lazy_embed is False
