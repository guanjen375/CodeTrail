# -*- coding: utf-8 -*-
"""T6：structured figure chunk 的檢索契約（CONTRACT §6.6 / workflow §4 Step 4）。

守四件事，每一件失效都是**無聲**的：
1. generic 的 noise filter / Jaccard dedup / adjacent merge 不得吞掉 structured chunk
   （被吞了只會表現成「注入了也查不到」或「metadata 不見了」，沒有任何錯誤訊息）。
2. strict query 必須在 **code 層**排除未通過驗證的圖片，且說得出「哪一頁哪張圖待覆核、
   為什麼」——只靠 prompt 提醒模型不算。
3. 一般 query 可以回未驗證內容，但 display 與 machine-readable metadata 都要帶
   status / reasons / range / truncation，不得讓人以為整張表或整份 log 都在 REF 裡。
4. 舊 KB（缺欄位）的圖片 chunk 一律降級成 legacy_unverified，且**不回寫檔案**。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pytest

import config
import context_signals
import knowledge
from knowledge import KnowledgeBase


# ============================================================
# fixtures：照 figure_extract §2.7 的凍結 render 格式組衍生文字
#
# content 的 `[FIGURE ...]` header 與 chunk metadata 由同一組參數產生，並在
# `_structured_chunk` 裡逐項互相對照（id / rev / page / status / range / 實際列數）。
# 少了這道對照，fixture 可以一邊宣稱 rows=5-12 一邊只放一列，測試就會反過來
# 接受它本來要防的「範圍與內容不符」。
# ============================================================
FIG_TABLE = "fig_1a2b3c4d5e6f7080"
FIG_TERMINAL = "fig_00000000000000ab"

TABLE_HEADER = "| Name | Address | Bits | Access | Description |"
TABLE_SEP = "| --- | --- | --- | --- | --- |"
DESC = "clock select for the primary tile controller domain, write one to enable and zero to disable"
ROW_A = f"| CTRL0 | 0x4000_0100 | [7:4] | RW | {DESC} |"
ROW_B = f"| CTRL1 | 0x4000_0104 | [7:4] | RW | {DESC} |"
FOOTNOTE = "[FOOTNOTE 1] all addresses are relative to the register base address"

# 幾乎全 hex 的 log：實測有意義字元比例 0.207（< 0.3）→ 現行 noise filter 一定丟掉
HEX_DUMP_LINES = [
    f"0x4000_010{i}: 0x0000_000{i} 0x0000_000{i + 1} 0x0000_000{i + 2}" for i in range(6)
]
# 極短的 log
TINY_LOG_LINES = ["0x4000_0100  0x0000_0001"]

_HEADER_RE = re.compile(
    r"^\[FIGURE kind=(?P<kind>\w+) id=(?P<id>\S+) rev=(?P<rev>\d+) page=(?P<page>\d+)"
    r"(?: (?P<range_kw>rows|lines)=(?P<a>\d+)-(?P<b>\d+)/(?P<total>\d+))?"
    r" status=(?P<status>\w+)\]$"
)


def _rows(count: int, start: int = 1) -> list:
    """count 列真實的 register 列（名稱與位址一一對應，供配對斷言使用）。"""
    return [
        f"| CTRL{i} | 0x4000_0{100 + i * 4} | [7:4] | RW | {DESC} |"
        for i in range(start, start + count)
    ]


def _table_content(*, figure_id=FIG_TABLE, rev=1, page=12, rows=(ROW_A,),
                   span=(1, 1), total=8, status="needs_review", footnote=True) -> str:
    lines = [
        f"[FIGURE kind=table id={figure_id} rev={rev} page={page} "
        f"rows={span[0]}-{span[1]}/{total} status={status}]",
        TABLE_HEADER,
        TABLE_SEP,
    ]
    lines.extend(rows)
    if footnote:
        lines.append(FOOTNOTE)
    return "\n".join(lines)


def _terminal_content(*, figure_id=FIG_TERMINAL, rev=1, page=7, lines=(),
                      span=(1, 1), total=6, status="unverified") -> str:
    fence = "`" * max(3, max((len(m) for line in lines
                              for m in re.findall(r"`+", line)), default=0) + 1)
    out = [
        f"[FIGURE kind=terminal id={figure_id} rev={rev} page={page} "
        f"lines={span[0]}-{span[1]}/{total} status={status}]",
        fence,
    ]
    out.extend(lines)
    out.append(fence)
    return "\n".join(out)


def _assert_fixture_consistent(chunk: dict) -> None:
    """content header 與 metadata 必須說同一件事，且實際列數符合宣稱的 range。"""
    lines = str(chunk.get("content", "")).split("\n")
    match = _HEADER_RE.match(lines[0]) if lines else None
    if match is None:
        return
    assert match.group("id") == chunk["figure_id"], "content 的 figure_id 與 metadata 不一致"
    assert int(match.group("rev")) == chunk["revision"]
    assert int(match.group("page")) == chunk["page"]
    assert match.group("status") == chunk["verification_status"]
    assert match.group("kind") == chunk["figure_kind"]
    if not match.group("range_kw"):
        return
    span = (int(match.group("a")), int(match.group("b")))
    total = int(match.group("total"))
    key = "row_range" if match.group("range_kw") == "rows" else "line_range"
    total_key = "row_total" if key == "row_range" else "line_total"
    assert tuple(chunk[key]) == span, f"{key} 與 content header 不一致"
    assert chunk[total_key] == total
    if match.group("range_kw") == "rows":
        body = [ln for ln in lines[3:] if not ln.startswith("[FOOTNOTE ")]
    else:
        body = lines[2:-1]
    assert len(body) == span[1] - span[0] + 1, (
        f"content 只有 {len(body)} 列/行，卻宣稱 {span[0]}-{span[1]}"
    )


def _structured_chunk(*, content, kind="table", origin=None, chunk_index=0, page=12,
                      source="npu_datasheet.pdf", doc_type="spec", figure_id=None,
                      revision=1, figure_index=2, status="needs_review",
                      reasons=("glyph_conflict",), reason_details=("第 3 列第 12 字元 8/B 衝突",),
                      row_range=None, row_total=None, line_range=None, line_total=None,
                      part_index=1, part_total=1, oversized_row=False, oversized_line=False,
                      model_input_variant="crop@200dpi", embedding=(0.0, 1.0)) -> dict:
    """CONTRACT §4 形狀的 structured figure chunk（T2 的 build_figure_chunks 產物）。"""
    if figure_id is None:
        figure_id = FIG_TABLE if kind == "table" else FIG_TERMINAL
    chunk = {
        "id": f"{source}::p{page}::c{chunk_index}",
        "source": source, "page": page, "chunk_index": chunk_index,
        "content": content, "type": doc_type, "section": "",
        "heading_hierarchy": "", "overlap_prefix_chars": 0, "heading_prefix_chars": 0,
        "char_start": 0, "char_end": 0,
        "structured": True,
        "origin": origin or f"figure_{kind}",
        "figure_kind": kind,
        "figure_id": figure_id,
        "document_id": f"{source}::0123456789abcdef",
        "revision": revision,
        "figure_index": figure_index,
        "bbox": [10.0, 20.0, 300.0, 400.0],
        "occurrences": [{"page": page, "bbox": [10.0, 20.0, 300.0, 400.0], "index": 1}],
        "row_range": list(row_range) if row_range else None,
        "line_range": list(line_range) if line_range else None,
        "row_total": row_total, "line_total": line_total,
        "oversized_row": oversized_row, "oversized_line": oversized_line,
        "part_index": part_index, "part_total": part_total,
        "extraction_status": "complete",
        "verification_status": status,
        "reasons": list(reasons), "reason_details": list(reason_details),
        "evidence_ref": ".codetrail/figures/npu_datasheet-1a2b3c4d5e/20260822-101500-ab12cd34/manifest.json",
        "model_input_variant": model_input_variant,
        "embedding": list(embedding),
    }
    _assert_fixture_consistent(chunk)
    return chunk


def _table_chunk(*, rows=(ROW_A,), span=(1, 1), total=8, footnote=True, status="needs_review",
                 figure_id=FIG_TABLE, revision=1, page=12, **over) -> dict:
    """content 與 metadata 由同一組參數產生（identity / status / range 一定一致）。"""
    content = _table_content(figure_id=figure_id, rev=revision, page=page, rows=rows,
                             span=span, total=total, status=status, footnote=footnote)
    return _structured_chunk(content=content, kind="table", figure_id=figure_id,
                             revision=revision, page=page, status=status,
                             row_range=span, row_total=total, **over)


def _terminal_chunk(*, lines=(), span=None, total=None, status="unverified",
                    figure_id=FIG_TERMINAL, revision=1, page=7, doc_type="manual",
                    reasons=("single_channel_only",), reason_details=(), **over) -> dict:
    span = span or (1, len(lines))
    total = total if total is not None else span[1]
    content = _terminal_content(figure_id=figure_id, rev=revision, page=page, lines=lines,
                                span=span, total=total, status=status)
    return _structured_chunk(content=content, kind="terminal", figure_id=figure_id,
                             revision=revision, page=page, doc_type=doc_type, status=status,
                             reasons=reasons, reason_details=reason_details,
                             line_range=span, line_total=total, **over)


def _plain_chunk(content: str, *, chunk_index=0, page=12, source="npu_datasheet.pdf",
                 doc_type="spec", section="", origin="", embedding=(0.0, 1.0),
                 **over) -> dict:
    """對照組 / 純文字 chunk：沒有 structured 旗標（＝現行 generic 路徑）。"""
    chunk = {
        "id": f"{source}::p{page}::c{chunk_index}",
        "source": source, "page": page, "chunk_index": chunk_index,
        "content": content, "type": doc_type, "section": section,
        "embedding": list(embedding),
    }
    if origin:
        chunk["origin"] = origin
    chunk.update(over)
    return chunk


def _stub_kb(monkeypatch, tmp_path: Path, chunks: list, *, recall=None) -> KnowledgeBase:
    """離線 KB：召回/rerank/embedding 全打樁，只驗 query 的後半段契約。

    與 tests/test_rag_pdf_ingest.py::_kb_with 同一套慣例（刻意複製而不是 import 別人的
    測試檔）。真實召回另有 `_loaded_kb`。`recall` 可指定只召回哪幾個 index。
    """
    kb = KnowledgeBase(str(tmp_path / "missing.json"))
    kb.loaded = True
    kb.chunks = list(chunks)
    kb._index_chunks()
    kb.documents = sorted({c["source"] for c in chunks})
    picked = kb.chunks if recall is None else [kb.chunks[i] for i in recall]
    candidates = [
        knowledge.Candidate(chunk_idx=kb.chunks.index(c), chunk=c, rrf_score=0.5 - 0.001 * i,
                            retrieval_score=0.9, gate_score=0.9,
                            retrieval_bm25=0.5, gate_bm25=0.5)
        for i, c in enumerate(picked)
    ]
    monkeypatch.setattr(kb, "_hybrid_search", lambda *_a, **_k: list(candidates))
    monkeypatch.setattr(
        kb, "_rerank_with_model",
        lambda _q, cands, _top_k, **_kw: [(None, c.chunk) for c in cands],
    )
    monkeypatch.setattr(kb, "_get_embedding", lambda _t: [0.0, 1.0])
    monkeypatch.setattr(knowledge, "USE_MMR", False)
    return kb


def _write_kb(tmp_path: Path, chunks: list) -> Path:
    """寫出真的 knowledge.json + .npz（向量只在 NPZ，JSON 不留 inline）。"""
    json_path = tmp_path / config.KNOWLEDGE_FILE
    schema = context_signals.CONTENT_INPUT_SCHEMA
    generation = "gen-t6-test"
    plain = [{k: v for k, v in c.items() if k not in ("embedding", "embedding_gate")}
             for c in chunks]
    json_path.write_text(json.dumps({
        "metadata": {
            "documents": sorted({c["source"] for c in chunks}),
            "embedding_model": config.EMBEDDING_MODEL,
            "store_generation": generation,
            "embedding_content_hash_schema": schema,
        },
        "chunks": plain,
    }, ensure_ascii=False), encoding="utf-8")

    rows = np.array([c["embedding"] for c in chunks], dtype=np.float32)
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    np.savez(
        tmp_path / config.KNOWLEDGE_EMB_FILE,
        embeddings=rows / np.where(norms > 0, norms, 1.0),
        embedding_model=config.EMBEDDING_MODEL,
        embedding_dimension=len(chunks[0]["embedding"]),
        chunk_count=len(chunks),
        content_hash=context_signals.chunks_content_hash(plain, schema=schema),
        content_hash_schema=schema,
        store_generation=generation,
    )
    return json_path


def _loaded_kb(monkeypatch, tmp_path: Path, chunks: list) -> KnowledgeBase:
    """真實 loader + 真實 BM25/hybrid 召回；只擋掉會連線的兩個點。"""
    path = _write_kb(tmp_path, chunks)
    kb = KnowledgeBase(str(path))
    assert kb.loaded and kb.load_error is None, kb.load_error
    monkeypatch.setattr(kb, "_get_embedding", lambda _t: [1.0, 0.0])
    monkeypatch.setattr(kb, "_generate_multi_queries", lambda question: [question])
    monkeypatch.setattr(kb, "_check_reranker_available", lambda: False)
    monkeypatch.setattr(config, "RERANK_FALLBACK_POLICY", "embedding")
    monkeypatch.setattr(knowledge, "USE_MMR", False)
    return kb


# ============================================================
# 1. generic heuristic 不得吞掉 structured chunk
# ============================================================
@pytest.mark.smoke
@pytest.mark.parametrize("lines,label,control_must_drop", [
    (TINY_LOG_LINES, "極短", False),
    (HEX_DUMP_LINES, "幾乎全 hex", True),
])
def test_structured_terminal_survives_noise_filter(monkeypatch, tmp_path: Path, lines, label,
                                                   control_must_drop):
    """workflow §5 terminal ⑥：極短 / 幾乎全 hex 的 log 寫入後仍 query 得到。

    `control_must_drop` 是實測結果，不是猜的：帶著 figure_extract 那行約 88 字元的
    `[FIGURE ...]` header 之後，「內容 < 50 字元」這條規則對極短 log 已經不會觸發；
    真正會吃掉 structured data 的是「字母+中文比例 < 30%」——實測 hex dump 只有 0.207。
    所以只有 hex dump 那組能拿來當「證明 bypass 有作用」的對照組；極短那組仍然要驗
    「查得到」，因為那是驗收條件本身。
    """
    chunk = _terminal_chunk(lines=lines)
    kb = _stub_kb(monkeypatch, tmp_path, [chunk])
    model_text, _display, meta = kb.query("boot log 的暫存器值")
    assert meta["has_ref"] is True, f"{label} structured log 被吞了：{model_text!r}"
    assert lines[0] in model_text

    if control_must_drop:
        control = _plain_chunk(chunk["content"], page=7, doc_type="manual")
        kb2 = _stub_kb(monkeypatch, tmp_path, [control])
        _m2, _d2, meta2 = kb2.query("boot log 的暫存器值")
        assert meta2["has_ref"] is False, (
            f"對照組（{label}，非 structured）竟然通過了 noise filter，"
            "這條測試就證明不了 bypass 的作用了"
        )


@pytest.mark.smoke
def test_similar_register_rows_are_not_jaccard_deduplicated(monkeypatch, tmp_path: Path):
    """兩列只差一個 hex 字元（Jaccard 0.878 ≥ 0.85）→ structured 兩列都要留住。

    同時斷言**語義配對**：名稱與位址不得交叉（CTRL0 只能配 0x4000_0100）。
    """
    # 刻意取兩個**中段** part：footnotes 只在含第 1 列的那份出現（CONTRACT §10-F），
    # 所以中段之間的衍生文字幾乎一模一樣——實測 Jaccard 0.878 ≥ 0.85 的門檻。
    chunk_a = _table_chunk(rows=(ROW_A,), span=(3, 3), footnote=False,
                           chunk_index=0, part_index=3, part_total=8)
    chunk_b = _table_chunk(rows=(ROW_B,), span=(4, 4), footnote=False,
                           chunk_index=7, part_index=4, part_total=8)
    kb = _stub_kb(monkeypatch, tmp_path, [chunk_a, chunk_b])
    model_text, _display, meta = kb.query("CTRL0 與 CTRL1 的位址")

    assert meta["ref_count"] == 2, "相似的 register 列被 Jaccard 吞掉了"
    assert model_text.count(ROW_A) == 1 and model_text.count(ROW_B) == 1
    assert "| CTRL0 | 0x4000_0104 |" not in model_text, "名稱與位址交叉配對"
    assert "| CTRL1 | 0x4000_0100 |" not in model_text, "名稱與位址交叉配對"
    assert [r["row_range"] for r in meta["refs"]] == [[3, 3], [4, 4]]

    control = [
        _plain_chunk(chunk_a["content"], chunk_index=0),
        _plain_chunk(chunk_b["content"], chunk_index=7),
    ]
    kb2 = _stub_kb(monkeypatch, tmp_path, control)
    _m2, _d2, meta2 = kb2.query("CTRL0 與 CTRL1 的位址")
    assert meta2["ref_count"] == 1, (
        "對照組（非 structured）沒有被 Jaccard 去重，這條測試就證明不了 bypass"
    )


@pytest.mark.smoke
def test_merge_never_swallows_structured_metadata(tmp_path: Path):
    """structured chunk 完全不進合併，且一個 metadata 都不掉。

    同時守住「不搬到尾端」：輸出順序仍是 (source, page, chunk_index)，generic 與
    structured 交錯時 REF 編號不會被打亂。
    """
    kb = KnowledgeBase(str(tmp_path / "missing.json"))

    def _text(idx: int) -> dict:
        return _plain_chunk(f"segment {idx} " + "x" * 60, chunk_index=idx, page=3)

    fig_a = _table_chunk(rows=(ROW_A,), span=(1, 1), page=3, chunk_index=2,
                         part_index=1, part_total=8, oversized_row=True)
    fig_b = _table_chunk(rows=(ROW_B,), span=(2, 2), footnote=False, page=3, chunk_index=3,
                         part_index=2, part_total=8)

    merged = kb._merge_adjacent_chunks([_text(0), _text(1), fig_a, fig_b, _text(4), _text(5)])

    kinds = [bool(m.get("structured")) for m in merged]
    assert kinds == [False, True, True, False], (
        f"structured chunk 沒有留在排序位置上：{kinds}"
    )
    assert merged[1] is fig_a and merged[2] is fig_b, "structured chunk 必須是原物件"
    assert "segment 1" in merged[0]["content"] and "segment 5" in merged[3]["content"], (
        "generic chunk 的合併行為必須維持不變"
    )
    for member in (fig_a, fig_b):
        for key in ("figure_id", "revision", "row_range", "row_total", "figure_kind",
                    "verification_status", "reasons", "reason_details", "evidence_ref",
                    "bbox", "occurrences", "part_index", "part_total",
                    "oversized_row", "model_input_variant", "document_id"):
            assert key in member, f"{key} 在合併後不見了"


def test_generic_merge_dedup_and_noise_behaviour_unchanged(tmp_path: Path):
    """交互矩陣：沒有 structured chunk 時，三個 heuristic 維持原行為。"""
    kb = KnowledgeBase(str(tmp_path / "missing.json"))

    text = [_plain_chunk(f"segment {i} " + "x" * 60, chunk_index=i, page=3) for i in (0, 1)]
    vl = [
        _plain_chunk(f"vl {i} " + "y" * 60, chunk_index=i, page=3, doc_type="diagram",
                     origin="diagram", figure_index=1)
        for i in (2, 3)
    ]
    merged = kb._merge_adjacent_chunks(text + vl)
    assert len(merged) == 2, "文字對與 VL 對各自合併"
    assert merged[0].get("verification_status") is None, "純文字合併結果不得被標成 figure"
    assert merged[1]["origin"] == "diagram" and merged[1]["figure_index"] == 1

    noisy = [_plain_chunk("...........", chunk_index=9), _plain_chunk("short", chunk_index=10)]
    assert kb._filter_noisy_chunks(noisy) == []

    same = "identical web paragraph about the accelerator reset behaviour and its limits"
    dupes = [_plain_chunk(same, chunk_index=11), _plain_chunk(same + " x", chunk_index=12)]
    assert len(kb._deduplicate_chunks(dupes)) == 1


@pytest.mark.smoke
def test_legacy_vl_verification_metadata_survives_merge(monkeypatch, tmp_path: Path):
    """舊 VL chunk 被合併之後，載入時補上的 status/reasons 不得蒸發。

    預設就開著 merge，而合併會重建 dict 只留十個 key——一般查詢因此會回一個
    status 空白、reasons 空的 REF，違反 §6.6 的 machine-readable 揭露。
    """
    legacy = [
        _plain_chunk(f"舊 KB 的圖片描述第 {i} 段，長度足夠通過噪音過濾。" * 3,
                     chunk_index=i, page=4, doc_type="diagram", origin="diagram",
                     figure_index=1, embedding=(1.0, 0.0),
                     reasons=["glyph_conflict"] if i == 1 else [])
        for i in (0, 1)
    ]
    kb = _loaded_kb(monkeypatch, tmp_path, legacy)
    assert all(c["verification_status"] == "legacy_unverified" for c in kb.chunks)

    model_text, display, meta = kb.query("圖片描述")

    assert meta["ref_count"] == 1, "兩段舊 VL 描述本來就會合併（既有行為）"
    ref = meta["refs"][0]
    assert ref["verification_status"] == "legacy_unverified", "合併把驗證狀態吃掉了"
    assert "legacy_missing_verification_status" in ref["reasons"]
    assert "glyph_conflict" in ref["reasons"], "另一段的原因也要保留（去重保序聯集）"
    assert "·待覆核" in display
    assert meta["has_authoritative_chunk"] is False
    assert "※ origin 標註 VL" in model_text


# ============================================================
# 2. strict gate（code 層，不是 prompt）
# ============================================================
@pytest.mark.smoke
@pytest.mark.parametrize("status", ["needs_review", "unverified", "legacy_unverified"])
def test_strict_query_cannot_answer_from_flagged_figure(monkeypatch, tmp_path: Path, status):
    """workflow §5 evidence ⑦：strict 不得用未驗證的圖回答 register 數值。

    被排除的內容不得殘留在 model_output / refs / retrieved_chunks，也不得撐起
    has_authoritative_chunk；同時要說得出「哪一頁哪張圖、為什麼」。
    """
    chunk = _table_chunk(rows=(ROW_A,), span=(1, 1), status=status,
                         reasons=("glyph_conflict", "missing_row"))
    kb = _stub_kb(monkeypatch, tmp_path, [chunk])

    model_text, display, meta = kb.query("CTRL0 的位址是多少？", is_strict_mode=True)

    assert meta["has_ref"] is False
    assert "0x4000_0100" not in model_text, "未驗證的 register 值洩漏進 strict 上下文"
    assert meta.get("refs", []) == []
    assert not any("0x4000_0100" in text for text in meta.get("retrieved_chunks", []))
    assert meta.get("has_authoritative_chunk", False) is False

    excluded = meta["excluded_figures"]
    assert len(excluded) == 1
    assert excluded[0]["page"] == 12 and excluded[0]["figure_index"] == 2
    assert excluded[0]["figure_id"] == FIG_TABLE
    assert excluded[0]["verification_status"] == status
    assert "glyph_conflict" in excluded[0]["reasons"]
    assert "待覆核" in model_text and "review_figures" in model_text
    assert "p.12" in model_text and "figure2" in model_text
    assert "待覆核" in display

    # 同一個 KB 的一般查詢仍然回得到（gate 只作用在 strict）
    _m2, _d2, meta2 = kb.query("CTRL0 的位址是多少？")
    assert meta2["has_ref"] is True and meta2["excluded_figures"] == []


@pytest.mark.smoke
@pytest.mark.parametrize("status", ["native_verified", "corroborated", "human_verified"])
def test_strict_query_keeps_trusted_figure(monkeypatch, tmp_path: Path, status):
    """反向：有獨立證據的三種狀態不得被誤擋（gate 不能變成一律封鎖）。"""
    variant = "native" if status == "native_verified" else "crop@200dpi"
    chunk = _table_chunk(rows=(ROW_A,), span=(1, 1), status=status, reasons=(),
                         reason_details=(), model_input_variant=variant)
    kb = _stub_kb(monkeypatch, tmp_path, [chunk])
    model_text, _display, meta = kb.query("CTRL0 的位址是多少？", is_strict_mode=True)

    assert meta["has_ref"] is True and meta["excluded_figures"] == []
    assert "0x4000_0100" in model_text
    assert meta["has_authoritative_chunk"] is True


@pytest.mark.smoke
def test_strict_gate_returns_cleanly_when_every_candidate_is_excluded(monkeypatch, tmp_path: Path):
    """全部候選都被排除時必須立刻返回三個值，不得走到 _decision_order(...)[0]。"""
    chunks = [
        _table_chunk(rows=(ROW_A,), span=(1, 1), chunk_index=i,
                     figure_id=f"fig_00000000000000{i:02x}", figure_index=i + 1)
        for i in range(3)
    ]
    kb = _stub_kb(monkeypatch, tmp_path, chunks)
    result = kb.query("CTRL0 的位址是多少？", is_strict_mode=True)

    assert isinstance(result, tuple) and len(result) == 3
    model_text, display, meta = result
    assert meta["has_ref"] is False
    assert len(meta["excluded_figures"]) == 3
    assert isinstance(model_text, str) and isinstance(display, str)


@pytest.mark.smoke
def test_strict_gate_excludes_legacy_vl_chunks(monkeypatch, tmp_path: Path):
    """舊 VL lane（origin=image/screenshot/diagram）同樣進 strict gate。

    兩條路徑都要守：直接注入的 chunk（沒有 verification_status），以及經過真實
    `_load` backfill 的舊 KB。少了任一條，舊 KB 的 VL 數值就會重新冒充 strict 證據。
    """
    vl = _plain_chunk("架構圖顯示 NPU 共有 8 個運算核心，SRAM 4MB。" * 3,
                      page=5, doc_type="diagram", origin="diagram", figure_index=1)
    kb = _stub_kb(monkeypatch, tmp_path, [vl])
    _model_text, _display, meta = kb.query("NPU 有幾個核心？", is_strict_mode=True)
    assert meta["has_ref"] is False, "舊 VL chunk 仍能當 strict 證據"
    assert meta["excluded_figures"][0]["verification_status"] == "legacy_unverified"

    loaded = _loaded_kb(monkeypatch, tmp_path, [dict(vl, embedding=[1.0, 0.0])])
    assert loaded.chunks[0]["verification_status"] == "legacy_unverified"
    _m, _d, meta2 = loaded.query("NPU 有幾個核心？", is_strict_mode=True)
    assert meta2["has_ref"] is False and meta2["excluded_figures"]


@pytest.mark.smoke
def test_flagged_part_taints_the_whole_figure(monkeypatch, tmp_path: Path):
    """同一張圖的任一 part 待覆核 → 整張圖（含乾淨的 part）都不得進 strict REF。

    CONTRACT §3「聚合一律取最差」。被召回的往往剛好是乾淨那一段，逐 chunk 判定會漏。
    """
    clean = _table_chunk(rows=(ROW_A,), span=(1, 1), chunk_index=0, status="corroborated",
                         reasons=(), reason_details=(), part_index=1, part_total=2)
    dirty = _table_chunk(rows=(ROW_B,), span=(2, 2), footnote=False, chunk_index=1,
                         status="needs_review", part_index=2, part_total=2)
    # 只召回乾淨那一段
    kb = _stub_kb(monkeypatch, tmp_path, [clean, dirty], recall=[0])
    _model_text, _display, meta = kb.query("CTRL0 的位址是多少？", is_strict_mode=True)
    assert meta["has_ref"] is False, "同一張圖有 part 待覆核時，乾淨的 part 也不可信"
    excluded = meta["excluded_figures"][0]
    assert excluded["verification_status"] == "needs_review"
    assert "glyph_conflict" in excluded["reasons"], (
        "被排除的原因來自另一段，只收本 chunk 的空 reasons 等於沒有可監督性"
    )
    assert "figure_part_flagged_elsewhere" in excluded["reasons"]

    _m2, _d2, meta2 = kb.query("CTRL0 的位址是多少？")
    assert meta2["has_ref"] is True
    assert meta2["has_authoritative_chunk"] is False, "figure 層級狀態也要套進權威判定"


@pytest.mark.smoke
def test_clean_part_ref_explains_why_it_was_downgraded(monkeypatch, tmp_path: Path):
    """只召回乾淨 part 的一般查詢：REF 要說得出「為什麼待覆核」，且不得謊稱未知狀態。"""
    clean = _table_chunk(rows=(ROW_A,), span=(1, 1), chunk_index=0, status="corroborated",
                         reasons=(), reason_details=(), part_index=1, part_total=2)
    dirty = _table_chunk(rows=(ROW_B,), span=(2, 2), footnote=False, chunk_index=1,
                         status="needs_review", part_index=2, part_total=2,
                         reasons=("glyph_conflict",), reason_details=("第 2 列第 5 字元 8/B 衝突",))
    kb = _stub_kb(monkeypatch, tmp_path, [clean, dirty], recall=[0])

    model_text, display, meta = kb.query("CTRL0 的位址是多少？")

    assert "不是已知狀態" not in model_text, (
        "corroborated 是合法狀態，被 sibling 降級不等於「未知狀態」"
    )
    assert "status: needs_review" in model_text
    assert "本 chunk 自報 corroborated" in model_text
    assert "reasons: glyph_conflict | figure_part_flagged_elsewhere" in model_text
    assert "第 2 列第 5 字元 8/B 衝突" in model_text
    ref = meta["refs"][0]
    assert ref["verification_status"] == "needs_review"
    assert ref["reasons"] == ["glyph_conflict", "figure_part_flagged_elsewhere"]
    assert "同一張圖的其他 part 未通過驗證" in ref["reason_details"][-1]
    assert "·待覆核" in display
    assert "※ spec 類型的 REF 優先級較高" not in model_text, (
        "待覆核的圖片 chunk 不得觸發 spec 優先提示"
    )


def test_revision_mismatch_between_parts_is_treated_as_needs_review(monkeypatch, tmp_path: Path):
    """KB 裡混著人工修正前後的 revision → 整張圖當待覆核，並說得出原因。"""
    old = _table_chunk(rows=(ROW_A,), span=(1, 1), chunk_index=0, revision=1,
                       status="human_verified", reasons=(), reason_details=())
    new = _table_chunk(rows=(ROW_B,), span=(2, 2), footnote=False, chunk_index=1, revision=2,
                       status="human_verified", reasons=(), reason_details=())
    kb = _stub_kb(monkeypatch, tmp_path, [old, new])
    _model_text, _display, meta = kb.query("CTRL0 的位址是多少？", is_strict_mode=True)
    assert meta["has_ref"] is False
    excluded = meta["excluded_figures"][0]
    assert excluded["verification_status"] == "needs_review"
    assert "figure_revision_conflict" in excluded["reasons"]
    assert any("revision" in detail for detail in excluded["reason_details"])


def test_lexical_only_flagged_figure_is_reported(monkeypatch, tmp_path: Path):
    """dense 分數不夠、但有精確 hex 證據的圖被擋掉時，一樣要列進 excluded_figures。

    register / hex 題主要走 lexical 這條路；只看 gate_score 會讓這類題完全沒有揭露。
    """
    chunk = _table_chunk(rows=(ROW_A,), span=(1, 1))
    kb = _stub_kb(monkeypatch, tmp_path, [chunk])
    monkeypatch.setattr(
        kb, "_hybrid_search",
        lambda *_a, **_k: [knowledge.Candidate(chunk_idx=0, chunk=chunk, rrf_score=0.02,
                                               retrieval_score=0.05, gate_score=0.05,
                                               retrieval_bm25=1.0, gate_bm25=1.0)],
    )
    _model_text, _display, meta = kb.query("0x4000_0100 是哪個 register？", is_strict_mode=True)
    assert meta["has_ref"] is False
    assert meta["excluded_figures"], "lexical-only 的候選被排除卻沒有揭露"


def test_mixed_status_figure_never_gets_the_spec_weight(monkeypatch, tmp_path: Path):
    """髒圖的乾淨 part 不得靠 doc_type=spec 拿到 1.3 權重去擠掉真正的文字證據。"""
    clean = _table_chunk(rows=(ROW_A,), span=(1, 1), chunk_index=0, status="corroborated",
                         reasons=(), reason_details=(), part_index=1, part_total=2)
    dirty = _table_chunk(rows=(ROW_B,), span=(2, 2), footnote=False, chunk_index=1,
                         status="needs_review", part_index=2, part_total=2)
    kb = _stub_kb(monkeypatch, tmp_path, [clean, dirty])
    trust_map = kb._figure_trust_map()

    assert kb._get_source_weight(clean, trust_map) == config.SOURCE_TYPE_WEIGHTS["diagram"]
    assert kb._get_source_weight(clean) == config.SOURCE_TYPE_WEIGHTS["spec"], (
        "沒有 trust_map 時退回逐 chunk 判定（fail-safe 方向：只會少降級）"
    )
    # 加權必須吃 trust_map：weighting 跑在 trust map 前面就是一條後門
    weighted = kb._apply_source_weighting(
        [knowledge.Candidate(chunk_idx=0, chunk=clean, rrf_score=1.0,
                             retrieval_score=1.0, gate_score=1.0)],
        trust_map,
    )
    assert weighted[0].gate_score == pytest.approx(config.SOURCE_TYPE_WEIGHTS["diagram"])


# ============================================================
# 3. 一般 query 的揭露（display + machine-readable）
# ============================================================
@pytest.mark.smoke
def test_general_query_discloses_status_reasons_range_and_evidence(monkeypatch, tmp_path: Path):
    """REF 文字與 metadata["refs"] 都要帶 status / reasons / range / figure 身分。"""
    chunk = _table_chunk(rows=_rows(8, start=5), span=(5, 12), total=40,
                         part_index=1, part_total=4)
    kb = _stub_kb(monkeypatch, tmp_path, [chunk])
    model_text, display, meta = kb.query("CTRL0 的位址是多少？")

    assert "status: needs_review" in model_text
    assert "reasons: glyph_conflict" in model_text
    assert "rows: 5-12/40" in model_text
    assert "（本 REF 是第 1/4 段）" in model_text, "part_index 是 1-based，不得再 +1"
    assert f"figure_id: {FIG_TABLE} rev=1 kind=table" in model_text
    assert "bbox: [10.0, 20.0, 300.0, 400.0]" in model_text
    assert "evidence: .codetrail/figures/" in model_text
    assert "·待覆核" in display

    ref = meta["refs"][0]
    assert ref["figure_id"] == FIG_TABLE
    assert ref["figure_kind"] == "table"
    assert ref["verification_status"] == "needs_review"
    assert ref["reasons"] == ["glyph_conflict"]
    assert ref["row_range"] == [5, 12] and ref["line_range"] is None
    assert ref["truncated"] is False
    assert ref["revision"] == 1 and ref["bbox"] == [10.0, 20.0, 300.0, 400.0]


@pytest.mark.smoke
def test_generic_text_chunk_is_never_marked_as_a_figure(monkeypatch, tmp_path: Path):
    """純文字 chunk 不得被標成待覆核，refs 的 figure 欄位一律空值。"""
    text = _plain_chunk(
        "根據規格書第三章，conv2d 輸入張量的高與寬上限皆為 4096，超過時回傳錯誤碼。" * 2,
        page=3, section="3.2",
    )
    kb = _stub_kb(monkeypatch, tmp_path, [text])
    model_text, display, meta = kb.query("conv2d 張量上限是多少？")

    assert "status:" not in model_text and "figure_id:" not in model_text
    assert "·待覆核" not in display
    ref = meta["refs"][0]
    assert ref["verification_status"] == "" and ref["reasons"] == []
    assert ref["figure_id"] == "" and ref["figure_kind"] == ""
    assert ref["figure_index"] is None and ref["revision"] is None
    assert ref["bbox"] is None and ref["truncated"] is False
    assert meta["has_authoritative_chunk"] is True


@pytest.mark.smoke
def test_truncated_structured_ref_reports_real_range(monkeypatch, tmp_path: Path):
    """截斷時要說出**實際完整顯示**的原子範圍與總數，且不得切在資料行中間。"""
    lines = [f"LINE{i:02d} 0x4000_01{i:02d} 0x0000_00{i:02d} #END" for i in range(1, 21)]
    chunk = _terminal_chunk(lines=lines, span=(1, 20), total=400, part_index=1, part_total=20)
    kb = _stub_kb(monkeypatch, tmp_path, [chunk])
    monkeypatch.setattr(knowledge, "KNOWLEDGE_MERGE_MAX_CHARS", 220)
    model_text, _display, meta = kb.query("boot log")

    assert "truncated:" in model_text and "未完整顯示" in model_text
    assert "lines 1-20/400" in model_text, "要說出本 REF 涵蓋的範圍與整份 log 的總行數"
    assert "只完整顯示 lines 1-" in model_text, "要說出實際顯示到第幾行"
    assert model_text.count("LINE") == model_text.count("#END") >= 1, (
        "截斷切在資料行中間了：半個 hex 值看起來仍像合法值"
    )
    ref = meta["refs"][0]
    assert ref["truncated"] is True
    assert ref["shown_range"] and ref["shown_range"][0] == 1
    assert ref["shown_range"][1] < 20 and ref["line_range"] == [1, 20]


@pytest.mark.smoke
@pytest.mark.parametrize("body,label", [
    ("LINE{i:02d} 0x4000_01{i:02d} #END", "一般 log"),
    ("LINE{i:02d} ``` 0x4000_01{i:02d} #END", "log 內含 triple-backtick"),
])
def test_truncated_terminal_ref_closes_its_fence(monkeypatch, tmp_path: Path, body, label):
    """terminal 截斷必須補回同長度的 closing fence。

    少了它，截斷註記、`[/REF]` 與後面所有信任提示都落在未關閉的 code block 裡，
    模型會把它們讀成 log 正文的一部分。
    """
    lines = [body.format(i=i) for i in range(1, 21)]
    chunk = _terminal_chunk(lines=lines, span=(1, 20), total=20)
    fence = chunk["content"].split("\n")[1]
    assert fence.startswith("```")
    kb = _stub_kb(monkeypatch, tmp_path, [chunk])
    monkeypatch.setattr(knowledge, "KNOWLEDGE_MERGE_MAX_CHARS", 260)
    model_text, _display, _meta = kb.query("boot log")

    ref_body = model_text[model_text.index("content: "):]
    assert ref_body.count(fence) == 2, f"{label}：fence 沒有成對（{ref_body.count(fence)} 個）"
    note_pos = ref_body.index("內容已截斷")
    assert ref_body.rindex(fence) < note_pos, f"{label}：截斷註記落在未關閉的 fence 內"
    assert ref_body.index("[/REF]") > ref_body.rindex(fence)


@pytest.mark.smoke
def test_trusted_oversized_row_cannot_form_an_empty_authoritative_ref(monkeypatch, tmp_path: Path):
    """單一超長 row/line 一列都放不進 REF 時，不得形成「空的權威成功」。

    模型手上沒有任何數值，metadata 卻回 has_ref=True + has_authoritative_chunk=True，
    是最糟的一種無聲失敗——所以 strict 直接 fail-closed，一般查詢也不算權威。
    """
    giant = "0x4000_0100 " * 400
    chunk = _terminal_chunk(lines=[giant], span=(3, 3), total=90, status="corroborated",
                            reasons=(), reason_details=(), oversized_line=True)
    kb = _stub_kb(monkeypatch, tmp_path, [chunk])
    monkeypatch.setattr(knowledge, "KNOWLEDGE_MERGE_MAX_CHARS", 200)

    model_text, _display, meta = kb.query("boot log", is_strict_mode=True)
    assert meta["has_ref"] is False, "一列都顯示不出來的 REF 不能算 strict 證據"
    assert meta.get("has_authoritative_chunk", False) is False
    excluded = meta["excluded_figures"][0]
    assert "ref_truncated_no_complete_row" in excluded["reasons"]
    assert excluded["verification_status"] == "corroborated", "排除原因是截斷，不是驗證狀態"

    general_text, _d2, meta2 = kb.query("boot log")
    assert meta2["has_ref"] is True, "一般查詢仍可回（但要誠實說沒有完整資料）"
    assert meta2["has_authoritative_chunk"] is False
    assert "未能顯示任何完整的資料列/行" in general_text
    assert "lines 3-3/90" in general_text and "oversized: true" in general_text
    assert meta2["refs"][0]["truncated"] is True
    assert meta2["refs"][0]["shown_range"] is None
    assert "※ spec 類型的 REF 優先級較高" not in general_text


@pytest.mark.smoke
def test_generic_chunk_truncation_sets_the_machine_flag(monkeypatch, tmp_path: Path):
    """generic chunk 被截斷時，machine 的 truncated 也要是 True。

    反過來的旗標比沒有旗標更危險：下游會拿 truncated=False 當「內容完整」用。
    """
    chunk = _plain_chunk("暫存器說明段落，內容很長。" * 200, page=3)
    kb = _stub_kb(monkeypatch, tmp_path, [chunk])
    monkeypatch.setattr(knowledge, "KNOWLEDGE_MERGE_MAX_CHARS", 200)
    model_text, _display, meta = kb.query("暫存器說明")

    assert "內容已截斷" in model_text
    assert meta["refs"][0]["truncated"] is True
    assert meta["refs"][0]["verification_status"] == "", "純文字 chunk 仍然不是 figure"


@pytest.mark.smoke
def test_disclosure_survives_include_content_off(monkeypatch, tmp_path: Path):
    """KNOWLEDGE_INCLUDE_CONTENT=False 只關掉 content，不關掉揭露義務。"""
    chunk = _table_chunk(rows=_rows(8, start=5), span=(5, 12), total=40)
    kb = _stub_kb(monkeypatch, tmp_path, [chunk])
    monkeypatch.setattr(knowledge, "KNOWLEDGE_INCLUDE_CONTENT", False)
    model_text, display, meta = kb.query("CTRL0 的位址是多少？")

    assert "0x4000_0100" not in model_text, "關掉 content 後不該印出內容"
    assert "status: needs_review" in model_text
    assert "reasons: glyph_conflict" in model_text
    assert "rows: 5-12/40" in model_text
    assert "（VL 辨識）" in model_text
    assert "※ origin 標註 VL" in model_text
    assert meta["refs"][0]["truncated"] is True, "一個字都沒印 = 一定沒顯示完"
    assert "·待覆核" in display


@pytest.mark.smoke
@pytest.mark.parametrize("kind,origin", [("table", "figure_table"), ("terminal", "figure_terminal")])
def test_native_lane_is_not_labelled_as_visual_model(monkeypatch, tmp_path: Path, kind, origin):
    """native lane 沒有呼叫過 VL，REF 不得宣稱「經視覺模型辨識」，也不得說錯 kind。"""
    if kind == "table":
        chunk = _table_chunk(rows=(ROW_A,), span=(1, 1), status="native_verified",
                             reasons=(), reason_details=(), model_input_variant="native")
    else:
        chunk = _terminal_chunk(lines=HEX_DUMP_LINES, status="native_verified",
                                reasons=(), reason_details=(), model_input_variant="native")
    kb = _stub_kb(monkeypatch, tmp_path, [chunk])
    model_text, display, _meta = kb.query("CTRL0 的位址是多少？")

    assert f"origin: {origin}（PDF 原生結構抽取，非視覺模型）" in model_text
    assert "視覺模型辨識" not in model_text
    assert "表格" not in model_text, "kind 中性文案：不得把 terminal 說成表格"
    assert "·VL" not in display, "native 抽取不是 VL 產物"
    assert "※ origin 標註 VL" not in model_text


@pytest.mark.smoke
@pytest.mark.parametrize("variant", ["crop@200dpi", "native"])
def test_conflict_between_figure_and_text_is_fully_disclosed(monkeypatch, tmp_path: Path, variant):
    """兩份互相衝突的 evidence 必須都在 REF 裡，且提示要求並列、標明未解。

    只驗尾註文案會是假陽性：任一 structured REF 留下來就能讓通用尾註成立，
    文字 REF 或任一數值消失時測試仍會通過。
    """
    figure = _table_chunk(rows=(ROW_A,), span=(1, 1), model_input_variant=variant)
    text = _plain_chunk("規格書第三章寫 CTRL0 的位址是 0x4000_0200。" * 3,
                        chunk_index=9, page=13, section="3.3")
    kb = _stub_kb(monkeypatch, tmp_path, [figure, text])
    model_text, _display, meta = kb.query("CTRL0 的位址是多少？")

    # 兩邊的數值與各自的出處都要在
    assert meta["ref_count"] == 2, "衝突的兩份 evidence 必須同時出現在 REF"
    assert "0x4000_0100" in model_text and "0x4000_0200" in model_text
    figure_block = model_text[model_text.index("[REF1]"):model_text.index("[REF2]")]
    text_block = model_text[model_text.index("[REF2]"):model_text.index("[/REF]")]
    assert "0x4000_0100" in figure_block and f"figure_id: {FIG_TABLE}" in figure_block
    assert "page: 12" in figure_block and "figure: 2" in figure_block
    assert "0x4000_0200" in text_block and "page: 13" in text_block
    assert "status:" not in text_block, "文字 REF 不得被標成 figure"

    sources = {(r["page"], r["figure_id"]) for r in meta["refs"]}
    assert (12, FIG_TABLE) in sources and (13, "") in sources

    # 提示：並列兩邊 + 標明未解 + 不得選邊
    assert "以文字抽取為準" not in model_text, "workflow §4 Step 4 明令不得這樣宣稱"
    assert "衝突未解" in model_text
    assert "同時列出兩邊" in model_text or "並列" in model_text
    assert "不得逕自宣告哪一邊為準" in model_text
    assert "REF 編號" in model_text or "出處" in model_text


def test_vl_hint_prefix_and_display_tag_still_work_for_legacy_vl(monkeypatch, tmp_path: Path):
    """舊 VL lane 的既有揭露逐字不變（既有測試 assert 的就是這個前綴與 ·VL）。"""
    vl = _plain_chunk("視覺模型辨識的架構圖描述。" * 6, page=1, doc_type="diagram",
                      origin="image", source="image_arch.png")
    kb = _stub_kb(monkeypatch, tmp_path, [vl])
    model_text, display, meta = kb.query("NPU 有幾個核心？")
    assert "origin: VL（image 經視覺模型辨識，非原文）" in model_text
    assert "※ origin 標註 VL" in model_text
    assert "·VL" in display
    assert meta["refs"][0]["origin"] == "image"


# ============================================================
# 4. 舊 KB backfill / 真實 loader / 真實召回
# ============================================================
@pytest.mark.smoke
def test_legacy_figure_chunk_backfill_is_memory_only(monkeypatch, tmp_path: Path):
    """舊 KB 缺 verification_status → 記憶體補 legacy_unverified，且不回寫檔案。

    既有的 reasons 不得被覆寫（舊 chunk 可能已經帶著 glyph_conflict 卻剛好缺 status）。
    """
    legacy = _plain_chunk("舊 KB 的圖片描述內容，長度足夠通過噪音過濾。" * 3, page=4,
                          doc_type="diagram", origin="diagram", embedding=(1.0, 0.0),
                          figure_index=1, reasons=["glyph_conflict"],
                          reason_details=["第 2 列不清楚"])
    path = _write_kb(tmp_path, [legacy])
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    kb = KnowledgeBase(str(path))

    assert kb.loaded is True and kb.load_error is None
    assert kb._embeddings is not None, "backfill 不得影響 NPZ 的內容雜湊驗證"
    chunk = kb.chunks[0]
    assert chunk["verification_status"] == "legacy_unverified"
    assert chunk["reasons"] == ["glyph_conflict", "legacy_missing_verification_status"], (
        "既有 reasons 必須保序保留，只補一個 slug"
    )
    assert chunk["reason_details"] == ["第 2 列不清楚"], "reason_details 一個字都不能動"

    after = (path.read_bytes(), path.stat().st_mtime_ns)
    assert before == after, "backfill 把記憶體的修補寫回檔案了"
    on_disk = json.loads(path.read_text(encoding="utf-8"))["chunks"][0]
    assert "verification_status" not in on_disk


@pytest.mark.smoke
def test_structured_metadata_survives_json_npz_reload_and_ref(monkeypatch, tmp_path: Path):
    """workflow §5 evidence ③：status/reasons/bbox/figure_id/revision/ranges 經
    JSON+NPZ save→load→retrieval→REF 之後三處一致（chunk、REF 文字、machine refs）。

    刻意放**兩張不同的圖**（不同 figure_id / 頁 / 範圍 / 向量）：單列 KB 驗不出
    row offset 或 embedding 對錯 chunk 的錯位。
    """
    first = _table_chunk(rows=_rows(8, start=5), span=(5, 12), total=40, part_index=2,
                         part_total=4, chunk_index=0, embedding=(1.0, 0.0))
    second = _terminal_chunk(
        lines=[f"BOOT{i:02d} 0x9000_00{i:02d} ready" for i in range(1, 5)],
        span=(1, 4), total=12, page=21, figure_id="fig_00000000000000cd",
        figure_index=1, chunk_index=1, part_index=1, part_total=3,
        status="corroborated", reasons=(), reason_details=(), embedding=(0.0, 1.0),
    )
    kb = _loaded_kb(monkeypatch, tmp_path, [first, second])

    for original in (first, second):
        loaded = next(c for c in kb.chunks if c["figure_id"] == original["figure_id"])
        for key in ("figure_id", "revision", "bbox", "row_range", "line_range", "row_total",
                    "line_total", "verification_status", "reasons", "reason_details",
                    "evidence_ref", "part_index", "part_total", "figure_kind", "occurrences",
                    "page"):
            assert loaded[key] == original[key], f"{key} 在 JSON round trip 之後變了"
    assert kb._embeddings is not None and kb._embeddings.shape[0] == 2

    # 兩張圖分別查，確認 embedding 列沒有對到另一個 chunk
    monkeypatch.setattr(kb, "_get_embedding", lambda _t: [1.0, 0.0])
    model_text, _display, meta = kb.query("CTRL5 clock select")
    assert meta["has_ref"] is True and meta["refs"][0]["figure_id"] == FIG_TABLE
    assert meta["refs"][0]["row_range"] == [5, 12] and meta["refs"][0]["revision"] == 1
    assert meta["refs"][0]["bbox"] == first["bbox"]
    assert f"figure_id: {FIG_TABLE} rev=1" in model_text
    assert "status: needs_review" in model_text and "rows: 5-12/40" in model_text
    assert "（本 REF 是第 2/4 段）" in model_text

    monkeypatch.setattr(kb, "_get_embedding", lambda _t: [0.0, 1.0])
    model_text2, _d2, meta2 = kb.query("BOOT01 ready")
    assert meta2["refs"][0]["figure_id"] == "fig_00000000000000cd"
    assert meta2["refs"][0]["line_range"] == [1, 4]
    assert meta2["refs"][0]["verification_status"] == "corroborated"
    assert "lines: 1-4/12" in model_text2 and "page: 21" in model_text2


@pytest.mark.smoke
def test_hex_literal_is_retrievable_without_stubbing_recall(monkeypatch, tmp_path: Path):
    """底線分組的 hex（0x4000_0100）必須真的能被 lexical 召回。

    修正前 `_tokenize_for_bm25` / `_exact_literals` 對 `0x4000_0100` 產生**零** token
    （`0x4000` 會被尾隨的 `_` 打掉尾綴斷言後整段回溯失敗），register map 的關鍵值
    因此完全查不到。這條測試不 stub `_hybrid_search`，走真實 BM25 索引。
    """
    chunk = _terminal_chunk(lines=HEX_DUMP_LINES, chunk_index=0, embedding=(0.0, 1.0))
    noise = _plain_chunk("完全無關的一段說明文字，用來讓候選不只一個。" * 3,
                         chunk_index=5, page=1, embedding=(0.0, 1.0))
    kb = _loaded_kb(monkeypatch, tmp_path, [chunk, noise])
    assert "0x4000_0100" in kb._tokenize_for_bm25(chunk["content"])
    # dense 完全關掉：只留 lexical 這條路
    monkeypatch.setattr(kb, "_embedding_search_numpy", lambda *_a, **_k: [])

    results = kb._hybrid_search("0x4000_0100", candidate_k=5)
    assert results, "帶底線的 hex 完全召回不到"
    assert results[0].chunk["figure_id"] == FIG_TERMINAL
    assert results[0].retrieval_bm25 > 0

    _model_text, _display, meta = kb.query("0x4000_0100 這個位址是什麼？")
    assert meta["has_ref"] is True
    assert meta["refs"][0]["figure_id"] == FIG_TERMINAL


# ============================================================
# 5. 常數契約與聚合語義
# ============================================================
@pytest.mark.smoke
def test_verification_constants_match_figure_extract():
    """knowledge.py 的本地副本與 figure_extract 必須逐一相等（漂移是無聲的）。"""
    import figure_extract as fx

    for name in ("VERIF_NATIVE", "VERIF_CORROBORATED", "VERIF_NEEDS_REVIEW",
                 "VERIF_UNVERIFIED", "VERIF_HUMAN", "VERIF_LEGACY"):
        assert getattr(knowledge, name) == getattr(fx, name), name
    assert set(knowledge.TRUSTED_VERIFICATION) == set(fx.TRUSTED_VERIFICATION)
    assert set(knowledge.FLAGGED_VERIFICATION) == set(fx.FLAGGED_VERIFICATION)
    assert knowledge.VERIFICATION_RANK == fx.VERIFICATION_RANK
    assert set(knowledge.FIGURE_ORIGINS) == set(fx.FIGURE_ORIGINS)
    assert set(knowledge.VL_ORIGINS) == set(fx.VL_ORIGINS)
    # 分組不得互相污染
    assert not (set(knowledge.TRUSTED_VERIFICATION) & set(knowledge.FLAGGED_VERIFICATION))
    assert (set(knowledge.TRUSTED_VERIFICATION) | set(knowledge.FLAGGED_VERIFICATION)
            == set(knowledge.VERIFICATION_RANK))


@pytest.mark.smoke
def test_unknown_verification_status_fails_safe():
    """沒見過的狀態字串一律當 needs_review，永遠不得取得信任。"""
    chunk = {"structured": True, "origin": "figure_table", "verification_status": "totally_new"}
    assert knowledge._figure_verification(chunk) == knowledge.VERIF_NEEDS_REVIEW
    assert knowledge._worst_verification(["totally_new"]) == knowledge.VERIF_NEEDS_REVIEW
    assert knowledge._worst_verification([]) == knowledge.VERIF_LEGACY
    assert knowledge._worst_verification(
        ["human_verified", "unverified"]) == knowledge.VERIF_UNVERIFIED
    # 非 figure chunk 沒有狀態，不得被當成待覆核
    assert knowledge._figure_verification({"content": "純文字"}) == ""


def test_aggregate_reason_details_is_ordered_and_deduped():
    """CONTRACT §10-E：去重保序聯集（與 figure_extract.aggregate_reason_details 同語意）。"""
    import figure_extract as fx

    members = [
        {"reason_details": ["a", "b"]},
        {"reason_details": ["b", "c"]},
        {"reason_details": []},
    ]
    assert knowledge._aggregate_reason_details(members) == ["a", "b", "c"]
    assert knowledge._aggregate_reason_details(members) == fx.aggregate_reason_details(members)


def test_excluded_figures_aggregate_worst_status_and_reasons(monkeypatch, tmp_path: Path):
    """同一張圖的多個 part 只列一筆，狀態取最差、reasons 聯集保序。"""
    specs = [
        ("unverified", ("single_channel_only",), ("第一段",)),
        ("needs_review", ("glyph_conflict",), ("第二段",)),
        ("unverified", ("single_channel_only",), ("第一段",)),
    ]
    parts = [
        _table_chunk(rows=(ROW_A,), span=(i + 1, i + 1), footnote=(i == 0), chunk_index=i,
                     part_index=i + 1, part_total=3, status=status, reasons=reasons,
                     reason_details=details)
        for i, (status, reasons, details) in enumerate(specs)
    ]
    kb = _stub_kb(monkeypatch, tmp_path, parts)
    _model_text, _display, meta = kb.query("CTRL0 的位址是多少？", is_strict_mode=True)

    excluded = meta["excluded_figures"]
    assert len(excluded) == 1, "同一個 figure_id 只能列一筆"
    assert excluded[0]["verification_status"] == "needs_review", "狀態要取最差"
    assert excluded[0]["reasons"][:2] == ["single_channel_only", "glyph_conflict"]
    assert "figure_part_flagged_elsewhere" in excluded[0]["reasons"]
    assert excluded[0]["reason_details"][:2] == ["第一段", "第二段"]


def test_flagged_structured_chunk_does_not_inherit_spec_weight(tmp_path: Path):
    """未驗證的視覺抽取不得因為繼承 doc_type=spec 而拿到 1.3 權重。"""
    kb = KnowledgeBase(str(tmp_path / "missing.json"))
    flagged = _table_chunk(rows=(ROW_A,), span=(1, 1))
    trusted = _table_chunk(rows=(ROW_A,), span=(1, 1), status="corroborated",
                           reasons=(), reason_details=())
    assert kb._get_source_weight(flagged) == config.SOURCE_TYPE_WEIGHTS["diagram"]
    assert kb._get_source_weight(trusted) == config.SOURCE_TYPE_WEIGHTS["spec"]
    # 既有 chunk 的權重逐位元組不變
    assert kb._get_source_weight(_plain_chunk("x", doc_type="spec")) == \
        config.SOURCE_TYPE_WEIGHTS["spec"]
    assert kb._get_source_weight(_plain_chunk("x", doc_type="diagram", origin="diagram")) == \
        config.SOURCE_TYPE_WEIGHTS["diagram"]
