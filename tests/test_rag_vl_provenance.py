"""VL 產物（圖片/截圖經視覺模型抽述）的出身鏈回歸測試。

背景（2026-08-14 review，P2）：
- `origin` 欄位入庫後從來沒被讀過（write-only），REF 驗證鏈把 VL 的
  機率性描述當成與原文同級的證據。
- detect_content_type 的 warning 關鍵字升級會把 diagram/chat chunk 改成
  warning（權重 1.15 > diagram 0.8），抹掉降權與出身標記——「注意/必須/限制」
  在中文 VL 描述裡幾乎必然出現。

修正後：REF 區塊揭露 origin、refs 帶 origin、display 加 ·VL 標記、
VL REF 出現時附信任等級提示；diagram/chat 不做 warning 升級。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import knowledge
import RAG


# ------------------------------------------------------------------
# detect_content_type：VL 產物不升級 warning
# ------------------------------------------------------------------

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
    monkeypatch.setattr(kb, "_hybrid_search", lambda *_a, **_k: [(0.5, 0.9, 0.0, chunk)])
    monkeypatch.setattr(
        kb, "_rerank_with_model",
        lambda _q, candidates, _top_k, **_kw: [c[3] for c in candidates],
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
