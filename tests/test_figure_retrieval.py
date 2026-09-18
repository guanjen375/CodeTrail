"""圖面（figure）lane 的三段契約：payload 組裝、檢索排除/納入、`review_figures` MCP 工具。

合併自 test_figure_retrieval.py（T6 檢索）、test_figure_payload.py（T2 payload → KB dict）
與 test_mcp_figure_tools.py（T7 的 MCP 公開邊界）；區段順序依原檔，每段各自的
「為什麼有這些測試」如下。smoke 逐條標在測試上（原 test_mcp_figure_tools.py 是整檔
smoke，這裡改成那一段每條各標）。

── T6：structured figure chunk 的檢索契約（CONTRACT §6.6 / workflow §4 Step 4）──
守四件事，每一件失效都是**無聲**的：
1. generic 的 noise filter / Jaccard dedup / adjacent merge 不得吞掉 structured chunk
   （被吞了只會表現成「注入了也查不到」或「metadata 不見了」，沒有任何錯誤訊息）。
2. strict query 必須在 **code 層**排除未通過驗證的圖片，且說得出「哪一頁哪張圖待覆核、
   為什麼」——只靠 prompt 提醒模型不算。
3. 一般 query 可以回未驗證內容，但 display 與 machine-readable metadata 都要帶
   status / reasons / range / truncation，不得讓人以為整張表或整份 log 都在 REF 裡。
4. 舊 KB（缺欄位）的圖片 chunk 一律降級成 legacy_unverified，且**不回寫檔案**。

── figure_extract 的 canonical model 契約：payload → validator → render → chunk → KB dict ──
為什麼這些測試存在（AGENTS.md §1.4 的第二類：無聲失敗風險的契約）：structured figure
lane 的整個價值就是「不改寫原文、不錯配欄位、不猜字元」。這三件事壞掉的時候**不會有
任何錯誤訊息**：表格少一欄、log 首行空行被吃掉、`▯` 的位置飄一格、未驗證的內容拿到
trusted status —— 全都會安靜地變成一個看起來正常的 chunk，然後被 strict query 當成可信
數值回答出去。所以這裡驗的是語義，不是形狀：
- terminal 的行序 / 空行 / 可見空白經 JSON round trip 與多 chunk 切分後**逐位元組**相同。
- register 表的每一格與同一列的身分綁定，兩列只差一個 hex 字元也不得交叉配對。
- validator 擋掉的每一條都對應一種真實的靜默錯配（無證據 fill-down、`▯` 不對齊、
  row 寬度不符、trusted 狀態配不確定內容）。
- `build_figure_chunks` 的批次語義：零部分成功、失敗時 `next_chunk_index` 原封不動。
真 PDF / 真模型完全不參與：這一段只有純函式，跑完是毫秒級。

── `review_figures` 的 MCP 公開邊界，以及兩個 query 工具的 `excluded_figures` ──
為什麼不能用 T5 的 `figure_review` 測試代替(AGENTS.md §1.4 第 2 款):
JSON 解析(含重複 key)、kind 的權威來源、document_id/figure_id 配對、人工確認閘、
例外分流(可重試的 conflict vs 必須 fail-loud 的路徑違規)、以及輸出渲染的截斷規則
**全部住在 `mcp_server.py`**。`list_figures` / `apply_fix` 的測試一行都不會經過它們,
而這些每一條失手都是無聲的:模型會拿到一個看似合理、實際被改寫過的結果。
`excluded_figures` 同理:strict gate 在 `knowledge.py` 把未驗證的圖擋掉之後,
若 MCP 的四條回傳路徑漏帶這個欄位,使用者只會看到「拒答」,看不到「有圖可用但
待覆核」——那正是 workflow §4 Step 4 要求要說出來的東西。
"""
from __future__ import annotations

import hashlib
import json
import re
import types
from pathlib import Path

import numpy as np
import pytest

import config
import context_signals
import figure_extract
import figure_extract as fx
import kb_cache
import knowledge
from knowledge import KnowledgeBase
from tests._harness import import_mcp_module, tool_fn

# ── 原 test_figure_retrieval.py：structured figure chunk 的檢索契約（strict gate / 揭露 / 舊 KB backfill） ──
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

    與 tests/test_rag_ingest.py::_kb_with 同一套慣例（刻意複製而不是 import 別人的
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
    payload = dict(
        embeddings=rows / np.where(norms > 0, norms, 1.0),
        embedding_model=config.EMBEDDING_MODEL,
        embedding_dimension=len(chunks[0]["embedding"]),
        chunk_count=len(chunks),
        content_hash=context_signals.chunks_content_hash(plain, schema=schema),
        content_hash_schema=schema,
        store_generation=generation,
    )
    # Persisted caches now require section rows even when all chunks are
    # structured figures (the valid section matrix then has zero rows).
    import RAG

    dimension = len(chunks[0]["embedding"])

    def embed_windows(windows, **_kwargs):
        for window in windows:
            window["embedding"] = [1.0] + [0.0] * (dimension - 1)
        return windows

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(RAG, "generate_embeddings", embed_windows)
        prepared = kb_cache.prepare_sections(plain, cache_dir=tmp_path)
    payload.update(kb_cache.section_fields(plain, prepared, dimension=dimension))
    np.savez(
        tmp_path / config.KNOWLEDGE_EMB_FILE,
        **kb_cache.npz_fields(payload, kb_cache.chunk_row_ids(plain)),
    )
    return json_path


def _loaded_kb(monkeypatch, tmp_path: Path, chunks: list) -> KnowledgeBase:
    """真實 loader + 真實 BM25/hybrid 召回；只擋掉會連線的兩個點。"""
    path = _write_kb(tmp_path, chunks)
    kb = KnowledgeBase(str(path))
    assert kb.loaded and kb.load_error is None, kb.load_error
    monkeypatch.setattr(kb, "_get_embedding", lambda _t: [1.0, 0.0])
    monkeypatch.setattr(kb, "_generate_multi_queries", lambda question: [question])
    # These tests exercise original figure evidence/gates without a model call.
    monkeypatch.setattr(knowledge, "USE_RERANKER", False)
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


@pytest.mark.smoke
def test_prose_scaffolding_is_recognised_like_terminal():
    """★ prose 的衍生文字與 terminal 同一種 scaffolding，截斷計畫必須認得。

    認不出來就回 None：strict 會把它當成「連一行完整資料都沒顯示」而排除，一般查詢
    則可能留下一個沒有關閉的 code fence。prose 是掃描頁最常見的 kind，這條漏掉等於
    整批掃描件在 REF 裡都算不出顯示範圍。
    """
    lines = ["[FIGURE kind=prose id=fig_0123456789abcdef rev=1 page=2 lines=1-2/2 "
             "status=unverified]", "```", "第一行", "第二行", "```"]

    assert knowledge.KnowledgeBase._structured_scaffold_lines(
        lines, {"figure_kind": "prose"}) == 2
    assert knowledge.KnowledgeBase._structured_scaffold_lines(
        lines, {"figure_kind": "terminal"}) == 2, "terminal 的既有行為不得改變"


@pytest.mark.smoke
def test_knowledge_line_family_matches_figure_extract():
    """knowledge.py 的逐行家族副本必須與 figure_extract 相等（漂了是無聲的）。"""
    assert set(knowledge.LINE_FIGURE_KINDS) == set(figure_extract.LINE_KINDS)


# ── 原 test_figure_payload.py：figure_extract 的 canonical model 契約（payload → validator → render → chunk → KB dict） ──
GLYPH = fx.UNREADABLE_GLYPH
FIGURE_ID = "fig_0123456789abcdef"


# ============================================================
# helpers
# ============================================================
def _meta(**overrides) -> dict:
    meta = {
        "figure_id": FIGURE_ID,
        "revision": 1,
        "page": 3,
        "verification_status": fx.VERIF_UNVERIFIED,
    }
    meta.update(overrides)
    return meta


def _model_table(rows, labels=("Name", "Address"), footnotes=(), states=None) -> dict:
    """模型端 table 物件（沒有 column_id / row_index / inherited_from_row）。"""
    states = states or {}
    return {
        "columns": [{"label": label} for label in labels],
        "rows": [
            {"cells": [
                {"text": text, "state": states.get((r, c), fx.CELL_STATE_OBSERVED)}
                for c, text in enumerate(row)
            ]}
            for r, row in enumerate(rows)
        ],
        "footnotes": list(footnotes),
    }


def _table(rows, labels=("Name", "Address"), footnotes=(), states=None) -> dict:
    return fx.canonicalize_table(_model_table(rows, labels, footnotes, states))


def _terminal(texts, spans=None) -> dict:
    spans = spans or {}
    payload = fx.canonicalize_terminal(
        {"lines": [{"text": text, "uncertain_spans": spans.get(i, [])}
                   for i, text in enumerate(texts)]}
    )
    return payload


def _diagram(**overrides) -> dict:
    model = {
        "title": "clock tree",
        "labels": ["PLL", "DIV"],
        "components": [{"name": "PLL", "desc": "phase locked loop"}],
        "relations": [{"src": "PLL", "dst": "DIV", "desc": "feeds"}],
        "values": [{"key": "fout", "value": "100 MHz", "desc": "after divider"}],
    }
    model.update(overrides)
    return fx.canonicalize_diagram(model)


_DEFAULT = object()


def _figure(**overrides):
    """FigureResult 形狀的物件（duck typing；契約 §6.4 的欄位名就是實質介面）。"""
    payload = overrides.pop("payload", _DEFAULT)
    if payload is _DEFAULT:
        payload = _table([["CTRL0", "0x4000_0100"]])
    kind = overrides.get("kind", fx.KIND_TABLE)
    if kind == fx.KIND_TABLE and payload is not None:
        auto_row_total = payload["rows"][-1]["row_index"] if payload["rows"] else 0
        auto_line_total = None
    elif kind == fx.KIND_TERMINAL and payload is not None:
        auto_row_total = None
        auto_line_total = payload["lines"][-1]["line_index"] if payload["lines"] else 0
    else:
        auto_row_total = auto_line_total = None
    fields = {
        "figure_id": FIGURE_ID,
        "document_id": "docs/spec.pdf::0123456789abcdef",
        "page": 3,
        "figure_index": 1,
        "bbox": (10.0, 20.0, 300.0, 400.0),
        "kind": fx.KIND_TABLE,
        "revision": 1,
        "payload": payload,
        "extraction_status": fx.EXTRACTION_COMPLETE,
        "verification_status": fx.VERIF_UNVERIFIED,
        "reasons": ["single_channel_only"],
        "reason_details": ["只有一個原生通道可比對"],
        "occurrences": [{"page": 3, "bbox": [10.0, 20.0, 300.0, 400.0], "index": 0}],
        "model_input_variant": "native",
        "row_total": auto_row_total,
        "line_total": auto_line_total,
    }
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


def _evidence(*figure_ids) -> dict:
    return {fid: ".codetrail/figures/slug/run/manifest.json" for fid in figure_ids}


def _parse_terminal_chunk(content: str) -> tuple[str, list[str]]:
    """位置式解析：第 0 行 header、第 1 行開 fence、最後一行關 fence，中間逐行原文。"""
    body = content.split("\n")
    assert body[0].startswith("[FIGURE kind=terminal "), body[0]
    assert body[1] == body[-1], (body[1], body[-1])
    assert set(body[1]) == {"`"} and len(body[1]) >= 3, body[1]
    return body[0], body[2:-1]


def _unescape_cell(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in ("\\", "|"):
                out.append(nxt)
                i += 2
                continue
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _split_markdown_row(line: str) -> list[str]:
    """`| a | b |` → ['a', 'b']，尊重 `\\|` 轉義。"""
    assert line.startswith("| ") and line.endswith(" |"), line
    body = line[2:-2]
    cells: list[str] = []
    buf: list[str] = []
    backslashes = 0
    i = 0
    while i < len(body):
        char = body[i]
        if char == "\\":
            backslashes += 1
            buf.append(char)
            i += 1
            continue
        if char == "|" and backslashes % 2 == 0:
            cells.append("".join(buf)[:-1])  # 去掉分隔符前的那個空白
            buf = []
            backslashes = 0
            i += 2                            # 跳過 "| "
            continue
        backslashes = 0
        buf.append(char)
        i += 1
    cells.append("".join(buf))
    return [_unescape_cell(cell) for cell in cells]


def _parse_table_chunk(content: str) -> tuple[str, list[str], list[str], list[list[str]], list[str]]:
    """→ (header 行, 欄位 label, 分隔列 cells, 資料列, footnotes)。"""
    body = content.split("\n")
    assert body[0].startswith("[FIGURE kind=table "), body[0]
    labels = _split_markdown_row(body[1])
    separators = _split_markdown_row(body[2])
    rows: list[list[str]] = []
    footnotes: list[str] = []
    for line in body[3:]:
        if line.startswith("[FOOTNOTE "):
            footnotes.append(line)
        else:
            rows.append(_split_markdown_row(line))
    return body[0], labels, separators, rows, footnotes


REGISTER_ROWS = [
    ["CTRL0", "0x4000_0100", "[7:4]", "RW", "clock select"],
    ["CTRL1", "0x4000_0104", "[3:0]", "RO", "reset status"],
    ["CTRL2", "0x4000_0108", "[15:8]", "RW", "divider"],
    ["CTRL3", "0x4000_010B", "[15:8]", "RW", "divider alt"],
]
REGISTER_LABELS = ("Name", "Address", "Bits", "Access", "Description")


# ============================================================
# smoke — terminal 逐位元組保真
# ============================================================
@pytest.mark.smoke
def test_terminal_round_trip_is_byte_exact_across_json_and_chunks():
    """首行/中央/末行空行、行首行尾可見空白、大小寫、反斜線、ANSI、literal ``` 全保留。

    刻意走完整條鏈：canonicalize → **真的 json.dumps/loads** → validate → 小預算切成
    多個 chunk → 依 line_range 重組 → 以 UTF-8 bytes 比較。既有文字路徑會在
    `normalize_document_text()` 的 `.strip()` 與 splitter 的 `.strip()` 各吃掉一次
    首尾空行，所以「有沒有真的繞過去」只能這樣驗。
    """
    original = [
        "",
        "  $ ls -al  ",
        "",
        "\x1b[0;32mOK\x1b[0m  Mixed CASE",
        "back\\slash and C:\\Users\\dev",
        "```",
        "   trailing spaces   ",
        "",
    ]
    payload = _terminal(original)
    payload = json.loads(json.dumps(payload, ensure_ascii=False))
    fx.validate_payload(payload, fx.KIND_TERMINAL)

    parts = fx.chunk_payload(payload, fx.KIND_TERMINAL, meta=_meta(), max_chars=140)
    assert len(parts) > 1, "預算要小到真的切成多個 part，單一 chunk 證明不了跨 chunk 保真"

    rebuilt: list[str] = []
    covered: list[int] = []
    for part in parts:
        header, texts = _parse_terminal_chunk(part["content"])
        start, end = part["line_range"]
        assert f"lines={start}-{end}/{len(original)}" in header
        assert len(texts) == end - start + 1
        covered.extend(range(start, end + 1))
        rebuilt.extend(texts)

    assert covered == list(range(1, len(original) + 1)), "range 必須連續覆蓋、不重疊不缺口"
    assert rebuilt == original
    assert "\n".join(rebuilt).encode("utf-8") == "\n".join(original).encode("utf-8")
    # literal ``` 那一行所在的 part 必須用更長的 fence，否則 code block 會提前結束
    fences = [part["content"].split("\n")[1] for part in parts]
    assert any(len(fence) >= 4 for fence in fences), fences


@pytest.mark.smoke
def test_validator_rejects_newline_inside_a_terminal_line():
    """一個 line 必須恰是一個視覺行；自行拆行等於把行序交給模型自由裁量。"""
    for bad in ("a\nb", "a\rb", "a\r\nb"):
        with pytest.raises(fx.FigureValidationError):
            fx.canonicalize_terminal({"lines": [{"text": bad, "uncertain_spans": []}]})
        payload = {"kind": fx.KIND_TERMINAL,
                   "lines": [{"line_index": 1, "text": bad, "uncertain_spans": []}]}
        with pytest.raises(fx.FigureValidationError):
            fx.validate_payload(payload, fx.KIND_TERMINAL)


# ============================================================
# smoke — table 結構
# ============================================================
@pytest.mark.smoke
def test_validator_rejects_bad_table_shape():
    base = _table([["CTRL0", "0x4000_0100"]])

    wrong_width = json.loads(json.dumps(base))
    wrong_width["rows"][0]["cells"].pop()
    with pytest.raises(fx.FigureValidationError, match="欄"):
        fx.validate_payload(wrong_width, fx.KIND_TABLE)

    duplicate = json.loads(json.dumps(base))
    duplicate["columns"][1]["column_id"] = "c1"
    duplicate["rows"][0]["cells"][1]["column_id"] = "c1"
    with pytest.raises(fx.FigureValidationError, match="重複"):
        fx.validate_payload(duplicate, fx.KIND_TABLE)

    misaligned = json.loads(json.dumps(base))
    misaligned["rows"][0]["cells"][0]["column_id"] = "c2"
    misaligned["rows"][0]["cells"][1]["column_id"] = "c1"
    with pytest.raises(fx.FigureValidationError, match="位置不對齊"):
        fx.validate_payload(misaligned, fx.KIND_TABLE)

    two = _table([["a", "b"], ["c", "d"]])
    for indices in ([2, 3], [1, 1], [2, 1]):
        broken = json.loads(json.dumps(two))
        for row, index in zip(broken["rows"], indices):
            row["row_index"] = index
        with pytest.raises(fx.FigureValidationError):
            fx.validate_payload(broken, fx.KIND_TABLE)

    # canonicalize 端：不補不砍
    with pytest.raises(fx.FigureValidationError, match="不補不砍"):
        fx.canonicalize_table({
            "columns": [{"label": "Name"}, {"label": "Address"}],
            "rows": [{"cells": [{"text": "CTRL0", "state": "observed"}]}],
            "footnotes": [],
        })

    # 全空 header：**結構**沒問題（欄數固定、column_id 唯一、每列對齊），所以
    # validator 收下它。79ef673 起「有沒有可確認的表頭」由抽取端的 `header_missing`
    # blocker 判（→ needs_review，永遠升不到 trusted），見
    # `tests/test_figure_verify.py::test_empty_header_is_flagged_not_rejected`。
    # 這裡硬拒的話，raster 截圖裡本來就沒有表頭列的表會讓**整份 PDF 零寫入**——
    # 那是丟資料，不是 fail-safe；validator 只守結構，不猜字。
    empty_header = json.loads(json.dumps(base))
    for column in empty_header["columns"]:
        column["label"] = ""
    fx.validate_payload(empty_header, fx.KIND_TABLE)


@pytest.mark.smoke
def test_validator_requires_first_index_to_be_one():
    """`>=1` + 嚴格遞增仍會放過「第一列是 2」；契約 §2.3 要的是從 1 起。"""
    table = _table([["a", "b"]])
    table["rows"][0]["row_index"] = 2
    with pytest.raises(fx.FigureValidationError, match="必須是 1"):
        fx.validate_payload(table, fx.KIND_TABLE)

    terminal = _terminal(["only"])
    terminal["lines"][0]["line_index"] = 2
    with pytest.raises(fx.FigureValidationError, match="必須是 1"):
        fx.validate_payload(terminal, fx.KIND_TERMINAL)


@pytest.mark.smoke
def test_inherited_from_row_must_reference_an_existing_earlier_row():
    """驗的是 reference 完整性（T2 能證明的部分）。

    真正的 rowspan geometry 證據由 T4 負責；這裡守的是「沒有 inherited 狀態就不准帶
    來源列」與「來源列必須真的存在且在前面」——無效 reference 會產生看起來合法的
    fill-down 配對，而 intentional blank 被自動改成 inherited 是資料捏造。
    """
    payload = _table([["CTRL0", "0x1000"], ["", "0x1004"]])
    inherited_cell = payload["rows"][1]["cells"][0]

    # 沒有 inherited 狀態卻帶來源列 → 擋
    inherited_cell["inherited_from_row"] = 1
    with pytest.raises(fx.FigureValidationError, match="沒有 rowspan 證據"):
        fx.validate_payload(payload, fx.KIND_TABLE)

    # inherited 卻沒有來源列 → 擋
    inherited_cell["state"] = fx.CELL_STATE_INHERITED
    inherited_cell["inherited_from_row"] = None
    with pytest.raises(fx.FigureValidationError, match="指得出來源列"):
        fx.validate_payload(payload, fx.KIND_TABLE)

    # 來源列不得等於/大於本列
    inherited_cell["inherited_from_row"] = 2
    with pytest.raises(fx.FigureValidationError, match="必須小於本列"):
        fx.validate_payload(payload, fx.KIND_TABLE)

    # bool 不是 int
    inherited_cell["inherited_from_row"] = True
    with pytest.raises(fx.FigureValidationError):
        fx.validate_payload(payload, fx.KIND_TABLE)

    # 來源列必須存在
    sparse = _table([["CTRL0", "0x1000"], ["", "0x1004"], ["", "0x1008"]])
    sparse["rows"][1]["row_index"] = 5
    sparse["rows"][2]["row_index"] = 9
    cell = sparse["rows"][2]["cells"][0]
    cell["state"] = fx.CELL_STATE_INHERITED
    cell["inherited_from_row"] = 4
    with pytest.raises(fx.FigureValidationError, match="不存在的列"):
        fx.validate_payload(sparse, fx.KIND_TABLE)
    cell["inherited_from_row"] = 5
    fx.validate_payload(sparse, fx.KIND_TABLE)

    # 模型不得自稱 inherited
    with pytest.raises(fx.FigureValidationError, match="模型可用範圍"):
        fx.canonicalize_table({
            "columns": [{"label": "Name"}],
            "rows": [{"cells": [{"text": "x", "state": fx.CELL_STATE_INHERITED}]}],
            "footnotes": [],
        })


@pytest.mark.smoke
def test_uncertain_spans_must_align_with_the_unreadable_glyph():
    """span 位置不是 `▯` → 不合格；候選只能進 alternatives，不得混進逐字正文。"""
    good = _terminal([f"addr 0x4000_010{GLYPH}"], spans={0: [{"start": 15, "end": 16,
                                                             "alternatives": ["8", "B"]}]})
    fx.validate_payload(good, fx.KIND_TERMINAL)

    misaligned = json.loads(json.dumps(good, ensure_ascii=False))
    misaligned["lines"][0]["uncertain_spans"][0] = {"start": 0, "end": 4, "alternatives": ["8", "B"]}
    with pytest.raises(fx.FigureValidationError, match="不全是"):
        fx.validate_payload(misaligned, fx.KIND_TERMINAL)

    payload = _terminal([f"{GLYPH}{GLYPH}{GLYPH}"])
    for span, pattern in (
        ({"start": 0, "end": 0, "alternatives": ["a"]}, "範圍不合法"),
        ({"start": 2, "end": 9, "alternatives": ["a"]}, "範圍不合法"),
        ({"start": -1, "end": 2, "alternatives": ["a"]}, "範圍不合法"),
        ({"start": 0, "end": 2, "alternatives": []}, "非空 list"),
    ):
        payload["lines"][0]["uncertain_spans"] = [span]
        with pytest.raises(fx.FigureValidationError, match=pattern):
            fx.validate_payload(payload, fx.KIND_TERMINAL)

    payload["lines"][0]["uncertain_spans"] = [
        {"start": 0, "end": 2, "alternatives": ["a"]},
        {"start": 1, "end": 3, "alternatives": ["b"]},
    ]
    with pytest.raises(fx.FigureValidationError, match="重疊"):
        fx.validate_payload(payload, fx.KIND_TERMINAL)


@pytest.mark.smoke
def test_table_cell_with_glyph_must_declare_unreadable_state():
    """契約 §10-C：cell 放了 `▯` 卻宣稱 observed，等於用 observed 為猜測背書。"""
    payload = _table([["CTRL0", f"0x400{GLYPH}_0100"]],
                     states={(0, 1): fx.CELL_STATE_UNREADABLE})
    fx.validate_payload(payload, fx.KIND_TABLE)
    payload["rows"][0]["cells"][1]["state"] = fx.CELL_STATE_OBSERVED
    with pytest.raises(fx.FigureValidationError, match="卻宣稱 state"):
        fx.validate_payload(payload, fx.KIND_TABLE)
    payload["rows"][0]["cells"][1]["state"] = fx.CELL_STATE_CONFLICT
    fx.validate_payload(payload, fx.KIND_TABLE)
    # canonicalize 端同樣擋得住（模型放了 ▯ 卻自稱 observed）
    with pytest.raises(fx.FigureValidationError, match="卻宣稱 state"):
        fx.canonicalize_table(_model_table([["CTRL0", f"0x400{GLYPH}_0100"]]))


# ============================================================
# smoke — chunk 原子化
# ============================================================
@pytest.mark.smoke
def test_oversized_row_and_line_keep_the_atom_whole():
    long_cell = "clock select " * 40
    table = _table([["CTRL0", "0x4000_0100"], ["CTRL1", long_cell], ["CTRL2", "0x4000_0108"]])
    parts = fx.chunk_payload(table, fx.KIND_TABLE, meta=_meta(), max_chars=200)
    by_range = {tuple(part["row_range"]): part for part in parts}
    assert (2, 2) in by_range, "超長列必須自己一個 chunk"
    oversized = by_range[(2, 2)]
    assert oversized["oversized_row"] is True
    assert oversized["oversized_line"] is False
    assert long_cell in oversized["content"], "整列必須完整保留，絕不拆格拆列"
    assert [part["row_range"] for part in parts] == sorted(part["row_range"] for part in parts)
    assert parts[0]["row_range"][0] == 1 and parts[-1]["row_range"][1] == 3

    long_line = "0xDEADBEEF " * 60
    terminal = _terminal(["short", long_line, "tail"])
    parts = fx.chunk_payload(terminal, fx.KIND_TERMINAL, meta=_meta(), max_chars=200)
    oversized = [part for part in parts if part["oversized_line"]]
    assert len(oversized) == 1
    assert oversized[0]["line_range"] == (2, 2)
    assert oversized[0]["oversized_row"] is False
    _, texts = _parse_terminal_chunk(oversized[0]["content"])
    assert texts == [long_line], "整行保留：不 strip、不 reflow、不切"


@pytest.mark.smoke
def test_non_oversized_parts_never_exceed_max_chars():
    """預算不變式：估算 overhead 只用來挑候選群組，最終一律以實際 render 長度複查。

    沒有這條的話，「oversized 旗標」會與真實長度脫節——下游看到未標 oversized 的
    超長 chunk，就會以為那是正常的、可以安全截斷的內容。
    """
    table = _table([[f"REG{i}", f"0x{i:04X}_0000"] for i in range(30)])
    terminal = _terminal([f"line {i} " + "x" * (i * 3) for i in range(30)])
    for limit in (150, 200, 400, 1200):
        for payload, kind in ((table, fx.KIND_TABLE), (terminal, fx.KIND_TERMINAL)):
            parts = fx.chunk_payload(payload, kind, meta=_meta(), max_chars=limit)
            for part in parts:
                flag = part["oversized_row"] or part["oversized_line"]
                if not flag:
                    assert len(part["content"]) <= limit, (kind, limit, part["part_index"])
            assert [part["part_index"] for part in parts] == list(range(1, len(parts) + 1))
            assert {part["part_total"] for part in parts} == {len(parts)}


@pytest.mark.smoke
def test_chunk_payload_reads_config_at_call_time():
    """契約 §10-A / AGENTS.md §3：預設值不得是 import-time snapshot。"""
    table = _table([[f"REG{i}", f"0x{i:04X}_0000"] for i in range(20)])
    wide = fx.chunk_payload(table, fx.KIND_TABLE, meta=_meta())
    original = config.FIGURE_CHUNK_MAX_CHARS
    try:
        config.FIGURE_CHUNK_MAX_CHARS = 180
        narrow = fx.chunk_payload(table, fx.KIND_TABLE, meta=_meta())
    finally:
        config.FIGURE_CHUNK_MAX_CHARS = original
    assert len(narrow) > len(wide)


@pytest.mark.smoke
def test_zero_row_table_and_zero_line_terminal_still_produce_one_part():
    """空 payload 不得回空 list——那會讓一張 complete 的圖無聲消失。"""
    table = _table([], footnotes=["see note"])
    parts = fx.chunk_payload(table, fx.KIND_TABLE, meta=_meta())
    assert len(parts) == 1
    assert parts[0]["row_range"] is None
    assert parts[0]["part_index"] == 1 and parts[0]["part_total"] == 1
    assert "rows=0-0/0" in parts[0]["content"]
    assert "[FOOTNOTE 1] see note" in parts[0]["content"]

    parts = fx.chunk_payload(_terminal([]), fx.KIND_TERMINAL, meta=_meta())
    assert len(parts) == 1
    assert parts[0]["line_range"] is None
    assert "lines=0-0/0" in parts[0]["content"]
    _, texts = _parse_terminal_chunk(parts[0]["content"])
    assert texts == []


@pytest.mark.smoke
def test_no_synthetic_column_is_added_for_narrow_tables():
    """workflow §4 Step 4 明令刪除「不足 3 欄補 `#` 欄」。

    既有 `extracted_document.normalize_table_content()` 對 `len(cells) < 3` 的表格列
    會改寫成 `key: value`，splitter 的 header 續行也只認 >= 3 欄——structured lane 必須
    完全繞過那套，一欄與兩欄的表照原樣輸出。
    """
    one = _table([["CTRL0"], ["CTRL1"]], labels=("Name",))
    _, labels, separators, rows, _ = _parse_table_chunk(
        fx.render_table_text(one, row_slice=None, meta=_meta())
    )
    assert labels == ["Name"]
    assert separators == ["---"]
    assert rows == [["CTRL0"], ["CTRL1"]]

    two = _table([["CTRL0", "0x4000_0100"]])
    content = fx.render_table_text(two, row_slice=None, meta=_meta())
    _, labels, separators, rows, _ = _parse_table_chunk(content)
    assert labels == ["Name", "Address"]
    assert separators == ["---", "---"]
    assert rows == [["CTRL0", "0x4000_0100"]]
    assert "#" not in content
    assert "CTRL0: 0x4000_0100" not in content, "不得被壓成 key: value"


@pytest.mark.smoke
def test_register_row_identity_survives_render_and_chunk():
    """五欄 register fixture：每欄與同一 row identity 綁定，跨 chunk 也不錯配。

    兩列只差一個 hex 字元（`0x4000_0108` / `0x4000_010B`），名稱與位址不得交叉配對；
    每列在所有 chunk 中恰出現一次（否則 BM25 會重複計數同一列）。
    """
    payload = _table(REGISTER_ROWS, labels=REGISTER_LABELS)
    parts = fx.chunk_payload(payload, fx.KIND_TABLE, meta=_meta(), max_chars=260)
    assert len(parts) > 1, "要真的切成多個 chunk 才驗得到跨 chunk 的 header 與配對"

    seen: list[list[str]] = []
    for part in parts:
        header, labels, separators, rows, _ = _parse_table_chunk(part["content"])
        assert labels == list(REGISTER_LABELS), "每個 chunk 都要帶真實 header"
        assert separators == ["---"] * len(REGISTER_LABELS)
        start, end = part["row_range"]
        assert f"rows={start}-{end}/{len(REGISTER_ROWS)}" in header
        assert len(rows) == end - start + 1
        seen.extend(rows)

    assert seen == REGISTER_ROWS
    by_name = {row[0]: row for row in seen}
    assert by_name["CTRL2"][1] == "0x4000_0108"
    assert by_name["CTRL3"][1] == "0x4000_010B"
    assert by_name["CTRL0"][2:] == ["[7:4]", "RW", "clock select"]
    assert len(seen) == len(REGISTER_ROWS), "每列恰出現一次"


# ============================================================
# smoke — 狀態機
# ============================================================
@pytest.mark.smoke
def test_worst_verification_and_aggregate_status_take_the_worst():
    assert fx.worst_verification([]) == fx.VERIF_LEGACY
    assert fx.worst_verification([fx.VERIF_HUMAN, fx.VERIF_NATIVE]) == fx.VERIF_NATIVE
    assert fx.worst_verification([fx.VERIF_NATIVE, fx.VERIF_NEEDS_REVIEW]) == fx.VERIF_NEEDS_REVIEW
    assert fx.worst_verification([fx.VERIF_CORROBORATED, fx.VERIF_UNVERIFIED]) == fx.VERIF_UNVERIFIED
    assert fx.worst_verification([fx.VERIF_UNVERIFIED, fx.VERIF_LEGACY]) == fx.VERIF_LEGACY
    # 未知狀態一律當最差，且絕不 raise（retrieval 會在舊 KB 資料上呼叫它）
    assert fx.worst_verification([fx.VERIF_HUMAN, "brand_new"]) == fx.VERIF_NEEDS_REVIEW

    members = [
        {"extraction_status": fx.EXTRACTION_COMPLETE, "verification_status": fx.VERIF_NATIVE,
         "reasons": ["single_channel_only"], "reason_details": ["只有一個通道"]},
        {"extraction_status": fx.EXTRACTION_COMPLETE, "verification_status": fx.VERIF_NEEDS_REVIEW,
         "reasons": ["glyph_conflict", "single_channel_only"],
         "reason_details": ["第 3 行第 12 字元 8/B 衝突", "只有一個通道"]},
    ]
    extraction, verification, reasons = fx.aggregate_status(members)
    assert extraction == fx.EXTRACTION_COMPLETE
    assert verification == fx.VERIF_NEEDS_REVIEW
    assert reasons == ["single_channel_only", "glyph_conflict"], "去重且保序"
    assert fx.aggregate_reason_details(members) == ["只有一個通道", "第 3 行第 12 字元 8/B 衝突"]

    members[0]["extraction_status"] = fx.EXTRACTION_FAILED
    assert fx.aggregate_status(members)[0] == fx.EXTRACTION_FAILED
    assert fx.aggregate_status([]) == (fx.EXTRACTION_FAILED, fx.VERIF_LEGACY, [])
    # 缺欄位視同未完成
    assert fx.aggregate_status([{"verification_status": fx.VERIF_NATIVE}])[0] == fx.EXTRACTION_FAILED


@pytest.mark.smoke
def test_read_native_lane_is_exact_bool_and_fail_loud():
    """lane 判定的唯一 reader（契約 §15.1 / §17.4）。

    三個消費端（planner 的 preflight 預算、RAG 的 capability probe 判定、verifier 的
    lane 選擇）原本各寫一份：RAG 對非 `bool` fail-loud，另外兩邊用 truthiness，於是
    `"false"` 在 RAG 是錯誤、在 verifier 卻是 native lane，**那條路徑會跳過 VL
    capability probe**。所以這裡守住「精確 bool、缺值即爆、不猜預設值」。
    """
    def candidate(signals):
        return types.SimpleNamespace(page=7, figure_id=FIGURE_ID, signals=signals)

    assert fx.read_native_lane(candidate({"native_lane": True})) is True
    assert fx.read_native_lane(candidate({"native_lane": False})) is False
    # 其他 signal 共存不影響
    assert fx.read_native_lane(candidate({"anchored": True, "native_lane": False})) is False

    # 缺 key / signals 不是 dict
    for signals in ({}, {"anchored": True}, None, [], "native", 0):
        with pytest.raises(fx.FigureExtractionError):
            fx.read_native_lane(candidate(signals))

    # 非精確 bool：truthiness 會讓 lane 靜默反過來
    for value in ("false", "true", "", 0, 1, 1.0, 0.0, None, [], {}, "False"):
        with pytest.raises(fx.FigureExtractionError) as excinfo:
            fx.read_native_lane(candidate({"native_lane": value}))
        message = str(excinfo.value)
        assert "page=7" in message and FIGURE_ID in message, message

    # 完全沒有 signals 屬性的物件也要 fail-loud，而不是 AttributeError
    with pytest.raises(fx.FigureExtractionError):
        fx.read_native_lane(types.SimpleNamespace(page=1, figure_id=FIGURE_ID))


# ============================================================
# smoke — 共享的 Variant 守門員（契約 §21.1）
# ============================================================
VARIANT_PNG = b"\x89PNG\r\n\x1a\n-fake-payload"
VARIANT_DIGEST = hashlib.sha256(VARIANT_PNG).hexdigest()
CANDIDATE_BBOX = (12.0, 30.0, 112.0, 150.0)


def _variant_fields(**overrides) -> dict:
    """§6.3 的**每一個**欄位都填齊；`digest` 是真的 sha256（契約 §21.3）。"""
    fields = {
        "figure_id": FIGURE_ID,
        "variant_id": "crop@200dpi",
        "png": VARIANT_PNG,
        "digest": VARIANT_DIGEST,
        "width": 800,
        "height": 960,
        "bbox": CANDIDATE_BBOX,
        "tile_index": 0,
        "tile_total": 1,
        "overlap_px": 0,
        "est_image_tokens": 512,
        "mime": "image/png",
    }
    fields.update(overrides)
    return fields


def _variant(**overrides):
    return types.SimpleNamespace(**_variant_fields(**overrides))


@pytest.mark.smoke
def test_validate_variant_accepts_the_frozen_shape():
    fx.validate_variant(_variant(), where="page=3 figure=" + FIGURE_ID)
    # tiled：1-based 編號、有重疊、bbox 只涵蓋自己那一片
    fx.validate_variant(
        _variant(variant_id="crop@200dpi#tile2of3", tile_index=2, tile_total=3,
                 overlap_px=48, bbox=(12.0, 70.0, 112.0, 110.0)),
        where="w")
    # raster：png 欄位裝的是原始 binary，mime 才是真實型別（契約 §13.2）
    raw = b"\xff\xd8\xff-jpeg"
    fx.validate_variant(
        _variant(variant_id="raster", png=raw, digest=hashlib.sha256(raw).hexdigest(),
                 mime="image/jpeg"),
        where="w")
    # dict 形狀也接受：驗的是值，不是容器型別
    fx.validate_variant(_variant_fields(), where="w")


@pytest.mark.smoke
@pytest.mark.parametrize("field", sorted(_variant_fields()))
def test_validate_variant_rejects_missing_field(field):
    """§6.3 的無預設欄位一個都不能少——缺欄位的 fixture 正是這條接縫連續四輪的成因。"""
    fields = _variant_fields()
    del fields[field]
    with pytest.raises(fx.FigureExtractionError, match=field):
        fx.validate_variant(types.SimpleNamespace(**fields), where="page=3")
    with pytest.raises(fx.FigureExtractionError, match=field):
        fx.validate_variant(fields, where="page=3")


@pytest.mark.smoke
@pytest.mark.parametrize("overrides,pattern", [
    # 非空 str
    ({"figure_id": 123}, "figure_id"),
    ({"figure_id": ""}, "figure_id"),
    ({"variant_id": ""}, "variant_id"),
    ({"variant_id": None}, "variant_id"),
    ({"mime": ""}, "mime"),
    ({"mime": b"image/png"}, "mime"),
    # png / digest
    ({"png": b"", "digest": hashlib.sha256(b"").hexdigest()}, "png"),
    ({"png": "not-bytes"}, "png"),
    ({"png": bytearray(VARIANT_PNG)}, "png"),
    ({"digest": ""}, "digest"),
    ({"digest": "0" * 64}, "digest"),
    ({"digest": VARIANT_DIGEST.upper()}, "digest"),
    ({"png": b"other-bytes"}, "digest"),          # png 換了但 digest 沒跟著換
    # 正整數：bool / str / float 都不得被轉型接受
    ({"width": 0}, "width"),
    ({"width": True}, "width"),
    ({"width": "800"}, "width"),
    ({"width": 800.0}, "width"),
    ({"height": -1}, "height"),
    ({"height": None}, "height"),
    ({"est_image_tokens": 0}, "est_image_tokens"),
    ({"est_image_tokens": -5}, "est_image_tokens"),
    ({"est_image_tokens": 1.0}, "est_image_tokens"),
    ({"est_image_tokens": True}, "est_image_tokens"),
    # 非負整數
    ({"overlap_px": -1}, "overlap_px"),
    ({"overlap_px": 48.0}, "overlap_px"),
    ({"overlap_px": False}, "overlap_px"),
    # bbox
    ({"bbox": (0.0, 0.0, 1.0)}, "bbox"),
    ({"bbox": (0.0, 0.0, 1.0, 2.0, 3.0)}, "bbox"),
    ({"bbox": "0,0,1,1"}, "bbox"),
    ({"bbox": (100.0, 0.0, 0.0, 120.0)}, "bbox"),
    ({"bbox": (0.0, 120.0, 100.0, 0.0)}, "bbox"),
    ({"bbox": (float("nan"), 0.0, 1.0, 1.0)}, "bbox"),
    ({"bbox": (0.0, 0.0, float("inf"), 1.0)}, "bbox"),
    ({"bbox": (True, 0.0, 1.0, 1.0)}, "bbox"),
    # tile flags
    ({"tile_total": 0}, "tile_total"),
    ({"tile_total": True}, "tile_total"),
    ({"tile_total": "1"}, "tile_total"),
    ({"tile_total": 1.9}, "tile_total"),
    ({"tile_index": -1}, "tile_index"),
    ({"tile_index": 0.9, "tile_total": 3}, "tile_index"),
    ({"tile_index": 1}, "tile_index"),                        # tile_total=1 只能是 0
    ({"tile_index": 0, "tile_total": 3}, "tile_index"),       # tiled 是 1-based
    ({"tile_index": 4, "tile_total": 3}, "tile_index"),
])
def test_validate_variant_rejects_malformed_field(overrides, pattern):
    """禁止任何 coercion：`int()` 會把 `True` / `"1"` / `1.9` 全截成合法的 `(1, 0)`。

    而那道檢查發生在 **VL 呼叫之前**——等下游拒絕時 VL 的錢已經花掉了（契約 §21.1）。
    """
    where = "page=3 figure=" + FIGURE_ID
    with pytest.raises(fx.FigureExtractionError) as excinfo:
        fx.validate_variant(_variant(**overrides), where=where)
    message = str(excinfo.value)
    assert where in message, message
    assert pattern in message, message


@pytest.mark.smoke
def test_is_full_image_rejects_local_crop_claiming_tile_total_one():
    """★ 局部 crop 即使宣稱 `tile_total=1` 也不得冒充完整原圖。

    只看 flags 的話，把第一片 tile 的 bytes 配上合法 flags 就能通過，於是「完整原圖」的
    下游語義（REF 的 crop 連結、manifest 的原始 asset）會指向一張只有上緣的圖。
    """
    where = "page=3 figure=" + FIGURE_ID
    assert fx.is_full_image(_variant(), candidate_bbox=CANDIDATE_BBOX, where=where) is True

    # flags 完全合法，bbox 只涵蓋候選的上緣三分之一
    partial = _variant(bbox=(12.0, 30.0, 112.0, 70.0))
    fx.validate_variant(partial, where=where)          # 形狀本身合法
    assert fx.is_full_image(partial, candidate_bbox=CANDIDATE_BBOX, where=where) is False

    # 真正的 tile 一樣不是完整原圖
    assert fx.is_full_image(
        _variant(tile_index=1, tile_total=3, bbox=(12.0, 30.0, 112.0, 70.0)),
        candidate_bbox=CANDIDATE_BBOX, where=where) is False
    # tile_total>1 但 bbox 剛好等於整張：仍不是完整原圖（它被切過）
    assert fx.is_full_image(
        _variant(tile_index=1, tile_total=2), candidate_bbox=CANDIDATE_BBOX,
        where=where) is False


@pytest.mark.smoke
def test_is_full_image_validates_first_and_tolerates_only_float_noise():
    where = "page=3 figure=" + FIGURE_ID
    # 不合格的 Variant 一律 raise，不是回 False
    with pytest.raises(fx.FigureExtractionError, match="digest"):
        fx.is_full_image(_variant(digest="0" * 64), candidate_bbox=CANDIDATE_BBOX, where=where)
    with pytest.raises(fx.FigureExtractionError, match="est_image_tokens"):
        fx.is_full_image(_variant(est_image_tokens=0), candidate_bbox=CANDIDATE_BBOX, where=where)
    # candidate_bbox 自己也要合法
    for bad in ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0, 1.0), (float("nan"), 0.0, 1.0, 1.0), None):
        with pytest.raises(fx.FigureExtractionError, match="candidate_bbox"):
            fx.is_full_image(_variant(), candidate_bbox=bad, where=where)

    # float 往返 / rotation 換算的尾差要吸收，肉眼可見的差異不得吸收
    noisy = tuple(v + 1e-9 for v in CANDIDATE_BBOX)
    assert fx.is_full_image(_variant(bbox=noisy), candidate_bbox=CANDIDATE_BBOX,
                            where=where) is True
    shifted = (12.0, 30.0, 112.0, 150.01)
    assert fx.is_full_image(_variant(bbox=shifted), candidate_bbox=CANDIDATE_BBOX,
                            where=where) is False


# ============================================================
# smoke — build_figure_chunks（KB 的唯一產生點）
# ============================================================
@pytest.mark.smoke
def test_build_figure_chunks_shape_and_chunk_index():
    payload = _table(REGISTER_ROWS, labels=REGISTER_LABELS)
    figure = _figure(payload=payload, row_total=len(REGISTER_ROWS))
    next_chunk_index = {3: 5}
    chunks = fx.build_figure_chunks(
        [figure], source="spec.pdf", doc_type="spec",
        next_chunk_index=next_chunk_index, evidence_ref_by_figure=_evidence(FIGURE_ID),
    )
    assert [chunk["chunk_index"] for chunk in chunks] == list(range(5, 5 + len(chunks)))
    assert next_chunk_index == {3: 5 + len(chunks)}, "就地更新，語意同 _pdf_figure_chunks"

    chunk = chunks[0]
    assert chunk["structured"] is True
    assert chunk["origin"] == "figure_table" and chunk["origin"] in fx.FIGURE_ORIGINS
    assert chunk["figure_kind"] == fx.KIND_TABLE
    assert chunk["type"] == "spec", "文件級 doc_type，不經 detect_content_type"
    assert chunk["section"] == "" and chunk["heading_hierarchy"] == ""
    assert chunk["overlap_prefix_chars"] == 0 and chunk["heading_prefix_chars"] == 0
    assert chunk["char_start"] == 0 and chunk["char_end"] == 0
    assert chunk["row_range"] == [1, len(REGISTER_ROWS)] and isinstance(chunk["row_range"], list)
    assert chunk["line_range"] is None
    assert chunk["row_total"] == len(REGISTER_ROWS) and chunk["line_total"] is None
    assert chunk["occurrences"] == [{"page": 3, "bbox": [10.0, 20.0, 300.0, 400.0], "index": 0}]
    assert chunk["evidence_ref"].endswith("manifest.json")
    assert chunk["reasons"] == ["single_channel_only"]
    assert chunk["reason_details"] == ["只有一個原生通道可比對"]
    assert chunk["model_input_variant"] == "native"
    assert chunk["extraction_status"] == fx.EXTRACTION_COMPLETE
    assert "id" not in chunk, "chunk id 由 RAG 統一產生（source::pN::cM::hash）"
    # JSON round trip 後形狀不變（KB 是 JSON）
    assert json.loads(json.dumps(chunk, ensure_ascii=False)) == chunk

    # 缺 evidence_ref → 不可監督，拒絕
    with pytest.raises(fx.FigureValidationError, match="evidence_ref"):
        fx.build_figure_chunks([figure], source="spec.pdf", doc_type="spec",
                               next_chunk_index={}, evidence_ref_by_figure={})


@pytest.mark.smoke
def test_uncertain_payload_must_be_exactly_needs_review():
    """契約 §3：`▯` / conflict / 漏 row/line **就是** needs_review。

    只擋 trusted 不夠——標成 `unverified` 一樣會進 general query 的 REF，而且沒有任何
    地方會告訴使用者「這裡有猜過的字元」，等於讓不確定內容沒有覆核入口。
    """
    table = _table([["CTRL0", f"0x400{GLYPH}_0100"]],
                   states={(0, 1): fx.CELL_STATE_UNREADABLE})
    rejected = sorted(set(fx.VERIFICATION_RANK) - {fx.VERIF_NEEDS_REVIEW})
    for status in rejected:
        with pytest.raises(fx.FigureValidationError) as excinfo:
            fx.build_figure_chunks(
                [_figure(payload=table, verification_status=status)],
                source="spec.pdf", doc_type="spec",
                next_chunk_index={}, evidence_ref_by_figure=_evidence(FIGURE_ID))
        assert "needs_review" in str(excinfo.value) or "legacy" in str(excinfo.value)

    chunks = fx.build_figure_chunks(
        [_figure(payload=table, verification_status=fx.VERIF_NEEDS_REVIEW)],
        source="spec.pdf", doc_type="spec",
        next_chunk_index={}, evidence_ref_by_figure=_evidence(FIGURE_ID))
    assert chunks and chunks[0]["verification_status"] == fx.VERIF_NEEDS_REVIEW

    # terminal 的 uncertain_spans 同樣算不確定
    terminal = _terminal([f"0x400{GLYPH}"], spans={0: [{"start": 5, "end": 6,
                                                        "alternatives": ["8", "B"]}]})
    with pytest.raises(fx.FigureValidationError, match="needs_review"):
        fx.build_figure_chunks(
            [_figure(payload=terminal, kind=fx.KIND_TERMINAL,
                     verification_status=fx.VERIF_CORROBORATED)],
            source="spec.pdf", doc_type="spec",
            next_chunk_index={}, evidence_ref_by_figure=_evidence(FIGURE_ID))

    # 新資料不得宣稱 legacy_unverified（那是舊 KB 缺欄位的補值）
    with pytest.raises(fx.FigureValidationError, match="legacy"):
        fx.build_figure_chunks(
            [_figure(verification_status=fx.VERIF_LEGACY)],
            source="spec.pdf", doc_type="spec",
            next_chunk_index={}, evidence_ref_by_figure=_evidence(FIGURE_ID))


@pytest.mark.smoke
def test_index_gaps_never_reach_the_kb():
    """row_index [1, 3] render 成 `rows=1-3/3` 就是謊報涵蓋了不存在的第 2 列。

    契約 §2.3 只要求「從 1 起、嚴格遞增」（跳號合法），所以 validator 放行；但缺口是
    §3 定義的「漏 row/line」，必須是 needs_review，且 chunk 層直接 fail-closed。
    """
    table = _table([["a", "b"], ["c", "d"]])
    table["rows"][1]["row_index"] = 3
    fx.validate_payload(table, fx.KIND_TABLE)  # validator 依契約放行跳號
    assert "缺口" in "".join(fx._payload_uncertainty(table, fx.KIND_TABLE))
    with pytest.raises(fx.FigureValidationError, match="不連續"):
        fx.chunk_payload(table, fx.KIND_TABLE, meta=_meta())
    with pytest.raises(fx.FigureValidationError, match="needs_review"):
        fx.build_figure_chunks([_figure(payload=table, row_total=3)],
                               source="spec.pdf", doc_type="spec", next_chunk_index={},
                               evidence_ref_by_figure=_evidence(FIGURE_ID))
    # 就算誠實標成 needs_review，chunk 層仍拒絕（不存在能誠實顯示缺口的 range 格式）
    with pytest.raises(fx.FigureValidationError, match="不連續"):
        fx.build_figure_chunks(
            [_figure(payload=table, row_total=3, verification_status=fx.VERIF_NEEDS_REVIEW)],
            source="spec.pdf", doc_type="spec", next_chunk_index={},
            evidence_ref_by_figure=_evidence(FIGURE_ID))

    terminal = _terminal(["one", "two"])
    terminal["lines"][1]["line_index"] = 5
    with pytest.raises(fx.FigureValidationError, match="不連續"):
        fx.chunk_payload(terminal, fx.KIND_TERMINAL, meta=_meta())


@pytest.mark.smoke
def test_build_figure_chunks_is_all_or_nothing():
    """零部分成功 + 失敗原子：批次裡有壞成員時不得輸出任何 chunk，也不得動 next_chunk_index。"""
    good = _figure(row_total=1)
    other_id = "fig_fedcba9876543210"
    failed = _figure(figure_id=other_id, figure_index=2, payload=None,
                     extraction_status=fx.EXTRACTION_FAILED)
    next_chunk_index = {3: 7}
    with pytest.raises(fx.FigureExtractionError, match="部分成功"):
        fx.build_figure_chunks([good, failed], source="spec.pdf", doc_type="spec",
                               next_chunk_index=next_chunk_index,
                               evidence_ref_by_figure=_evidence(FIGURE_ID, other_id))
    assert next_chunk_index == {3: 7}

    # 第二張在 render 迴圈裡才失敗（缺 evidence_ref）→ 第一張已推進的 index 必須回滾
    second = _figure(figure_id=other_id, figure_index=2, row_total=1)
    next_chunk_index = {3: 7}
    with pytest.raises(fx.FigureValidationError):
        fx.build_figure_chunks([good, second], source="spec.pdf", doc_type="spec",
                               next_chunk_index=next_chunk_index,
                               evidence_ref_by_figure=_evidence(FIGURE_ID))
    assert next_chunk_index == {3: 7}, "整批成功才提交，中途失敗時呼叫端的 dict 原封不動"

    # 重複身分
    duplicate = _figure(figure_index=2, row_total=1)
    with pytest.raises(fx.FigureValidationError, match="兩次"):
        fx.build_figure_chunks([good, duplicate], source="spec.pdf", doc_type="spec",
                               next_chunk_index={}, evidence_ref_by_figure=_evidence(FIGURE_ID))


@pytest.mark.smoke
def test_build_figure_chunks_rejects_total_mismatch():
    """total 是 REF 揭露截斷用的完整性宣告；與 payload 不一致就是謊報。"""
    payload = _table(REGISTER_ROWS, labels=REGISTER_LABELS)
    with pytest.raises(fx.FigureValidationError, match="不一致"):
        fx.build_figure_chunks([_figure(payload=payload, row_total=99)],
                               source="spec.pdf", doc_type="spec", next_chunk_index={},
                               evidence_ref_by_figure=_evidence(FIGURE_ID))
    with pytest.raises(fx.FigureValidationError, match="不該有"):
        fx.build_figure_chunks([_figure(payload=payload, row_total=4, line_total=4)],
                               source="spec.pdf", doc_type="spec", next_chunk_index={},
                               evidence_ref_by_figure=_evidence(FIGURE_ID))
    # 缺 total 不得繞過閘門：沒有完整性宣告時 header/REF 會把可能漏列的 payload 顯示成完整
    with pytest.raises(fx.FigureValidationError, match="必須提供 row_total"):
        fx.build_figure_chunks([_figure(payload=payload, row_total=None)],
                               source="spec.pdf", doc_type="spec", next_chunk_index={},
                               evidence_ref_by_figure=_evidence(FIGURE_ID))
    terminal = _terminal(["one", "two"])
    with pytest.raises(fx.FigureValidationError, match="必須提供 line_total"):
        fx.build_figure_chunks(
            [_figure(payload=terminal, kind=fx.KIND_TERMINAL, line_total=None)],
            source="spec.pdf", doc_type="spec", next_chunk_index={},
            evidence_ref_by_figure=_evidence(FIGURE_ID))


@pytest.mark.smoke
def test_build_figure_chunks_rejects_broken_result_metadata():
    """空 evidence_ref / revision 0 / 空 occurrences / 非法 bbox / 佔位 figure_id 都能
    產出外觀正常但無法監督或會被放錯頁的 chunk。"""
    other_bbox = [1.0, 2.0, 3.0, 4.0]
    cases = [
        ({"figure_id": "fig_not_hex"}, "figure_id"),
        ({"revision": 0}, "revision"),
        ({"page": 0}, "page"),
        ({"occurrences": []}, "occurrences"),
        # page 與 bbox 必須是**同一個** occurrence 的組合，不是各自出現過就好
        ({"page": 7,
          "occurrences": [{"page": 3, "bbox": [10.0, 20.0, 300.0, 400.0], "index": 0},
                          {"page": 7, "bbox": other_bbox, "index": 1}]},
         "同一個 occurrence"),
        ({"occurrences": [{"page": 3, "bbox": other_bbox, "index": 0}]},
         "同一個 occurrence"),
        ({"page": 9}, "同一個 occurrence"),
        ({"bbox": (10.0, 20.0, 1.0, 400.0)}, "合法矩形"),
        ({"bbox": (float("nan"), 20.0, 30.0, 400.0)}, "有限數字"),
        ({"model_input_variant": ""}, "model_input_variant"),
        ({"document_id": "no-hash"}, "document_id"),
        ({"verification_status": "made_up"}, "verification_status"),
        # reasons/details 不得是 None（`or []` 會把「忘了填」吞成「沒有原因」）
        ({"reasons": None}, "reasons 必須是 list"),
        ({"reason_details": None}, "reason_details 必須是 list"),
        ({"reasons": "single_channel_only"}, "reasons 必須是 list"),
        # flagged 狀態一定要說得出為什麼還不能信
        ({"reasons": []}, "reasons 不得為空"),
        ({"verification_status": fx.VERIF_NEEDS_REVIEW, "reasons": []}, "reasons 不得為空"),
    ]
    for overrides, pattern in cases:
        figure = _figure(**overrides)
        evidence = _evidence(getattr(figure, "figure_id"))
        with pytest.raises(fx.FigureValidationError, match=pattern):
            fx.build_figure_chunks([figure], source="spec.pdf", doc_type="spec",
                                   next_chunk_index={}, evidence_ref_by_figure=evidence)


@pytest.mark.smoke
def test_build_figure_chunks_requires_one_coherent_document():
    """整批 document_id 必須相同，且 source 必須就是它的 display name。

    混入另一份文件的 figure 會用錯的 source 入庫：`remove_document` 之後留下孤兒
    chunk，REF 與 crop 也會指向錯的檔案。
    """
    other_id = "fig_fedcba9876543210"
    mixed = _figure(figure_id=other_id, figure_index=2,
                    document_id="docs/other.pdf::0123456789abcdef")
    with pytest.raises(fx.FigureValidationError, match="document_id 不一致"):
        fx.build_figure_chunks([_figure(), mixed], source="spec.pdf", doc_type="spec",
                               next_chunk_index={},
                               evidence_ref_by_figure=_evidence(FIGURE_ID, other_id))
    with pytest.raises(fx.FigureValidationError, match="display name"):
        fx.build_figure_chunks([_figure()], source="wrong.pdf", doc_type="spec",
                               next_chunk_index={},
                               evidence_ref_by_figure=_evidence(FIGURE_ID))


@pytest.mark.smoke
def test_context_free_errors_carry_the_locator_sentinel():
    """契約 §5 的定位資訊：拿不到檔名/頁碼/figure_id 的 API 一律以固定 sentinel 開頭。

    sentinel 讓「未知」與「不適用」分得開，也讓有 context 的呼叫端（T4/T5/T7）能用
    `strip_locator()` 換上真值再重拋，而不是做字串猜測。
    """
    bad_table = {"kind": fx.KIND_TABLE, "columns": [], "rows": [], "footnotes": []}
    bad_terminal = {"kind": fx.KIND_TERMINAL,
                    "lines": [{"line_index": 1, "text": "a\nb", "uncertain_spans": []}]}
    good = _table([["a", "b"]])
    calls = [
        lambda: fx.validate_payload(bad_table, fx.KIND_TABLE),
        lambda: fx.validate_payload(bad_terminal, fx.KIND_TERMINAL),
        lambda: fx.canonicalize_table({"columns": [], "rows": [], "footnotes": []}),
        lambda: fx.canonicalize_terminal({"lines": [{"text": "a\nb", "uncertain_spans": []}]}),
        lambda: fx.canonicalize_diagram({"title": "t"}),
        lambda: fx.render_table_text(good, row_slice=(9, 9), meta=_meta()),
        lambda: fx.render_terminal_text(_terminal(["x"]), line_slice=(9, 9), meta=_meta()),
        lambda: fx.render_diagram_text(good, meta=_meta()),
        lambda: fx.chunk_payload(good, fx.KIND_TABLE, meta=_meta(), max_chars=0),
    ]
    for call in calls:
        with pytest.raises(fx.FigureError) as excinfo:
            call()
        message = str(excinfo.value)
        assert message.startswith(fx.LOCATOR_UNKNOWN), message
        assert not fx.strip_locator(excinfo.value).startswith(fx.LOCATOR_UNKNOWN)
        assert fx.strip_locator(excinfo.value), "去掉 sentinel 之後必須還有原因"

    # 巢狀呼叫不得疊兩層 sentinel（canonicalize 內部會再呼叫 validate_payload）
    with pytest.raises(fx.FigureError) as excinfo:
        fx.canonicalize_table(_model_table([["CTRL0", f"0x400{GLYPH}_0100"]]))
    assert str(excinfo.value).count(fx.LOCATOR_UNKNOWN) == 1

    # 有 context 的呼叫點必須把 sentinel 換成真值
    with pytest.raises(fx.FigureValidationError) as excinfo:
        fx.build_figure_chunks([_figure(payload=_table([["a", "b"]]), row_total=99)],
                               source="spec.pdf", doc_type="spec", next_chunk_index={},
                               evidence_ref_by_figure=_evidence(FIGURE_ID))
    assert fx.LOCATOR_UNKNOWN not in str(excinfo.value)


@pytest.mark.smoke
def test_schema_invalid_types_raise_figure_validation_error():
    """llama.cpp 可能忽略 schema：list / dict / bool 是必須由外部 validator 接住的輸入。

    對 frozenset 或 dict 做 membership 需要可 hash 值，原始的 `TypeError` 會繞過以
    `FigureValidationError` 驅動的重試與統一失敗語意（契約 §5）。
    """
    for bad_state in ([], {}, ["observed"], {"v": "observed"}, True, 1, None):
        payload = _table([["CTRL0", "0x1000"]])
        payload["rows"][0]["cells"][0]["state"] = bad_state
        with pytest.raises(fx.FigureValidationError):
            fx.validate_payload(payload, fx.KIND_TABLE)
        with pytest.raises(fx.FigureValidationError):
            fx.canonicalize_table({
                "columns": [{"label": "Name"}],
                "rows": [{"cells": [{"text": "x", "state": bad_state}]}],
                "footnotes": [],
            })

    for bad_status in ([], {}, True, 3, None):
        with pytest.raises(fx.FigureValidationError):
            fx.render_table_text(_table([["a", "b"]]), row_slice=None,
                                 meta=_meta(verification_status=bad_status))
        figure = _figure(verification_status=bad_status)
        with pytest.raises(fx.FigureValidationError):
            fx.build_figure_chunks([figure], source="spec.pdf", doc_type="spec",
                                   next_chunk_index={},
                                   evidence_ref_by_figure=_evidence(FIGURE_ID))
        # 不可 hash 的 status 也絕不能取得信任（且不得拋 TypeError）
        assert fx.worst_verification([fx.VERIF_HUMAN, bad_status]) == fx.VERIF_NEEDS_REVIEW

    for bad_kind in ([], {}, None, 3):
        with pytest.raises(fx.FigureValidationError):
            fx.validate_payload(_table([["a", "b"]]), bad_kind)
        with pytest.raises(fx.FigureValidationError):
            fx.model_json_schema(bad_kind)
        assert fx.critical_tokens("0x1000", bad_kind) == ["0x1000"]


@pytest.mark.smoke
def test_zero_atom_chunk_reports_oversized_against_real_length():
    """零列/零行也要以實際 render 長度判定 oversized，否則下游會無提示地截斷。"""
    long_note = "reserved bits must be written as zero. " * 20
    table = _table([], footnotes=[long_note])
    part = fx.chunk_payload(table, fx.KIND_TABLE, meta=_meta(), max_chars=200)[0]
    assert len(part["content"]) > 200
    assert part["oversized_row"] is True
    assert long_note in part["content"], "整段註腳完整保留"

    # 預算小於 header 本身：terminal 的空 payload 同樣要誠實標記
    part = fx.chunk_payload(_terminal([]), fx.KIND_TERMINAL, meta=_meta(), max_chars=10)[0]
    assert len(part["content"]) > 10
    assert part["oversized_line"] is True

    # 預算夠用時不得誤標
    part = fx.chunk_payload(_table([], footnotes=["short"]), fx.KIND_TABLE, meta=_meta())[0]
    assert part["oversized_row"] is False


@pytest.mark.smoke
def test_build_figure_chunks_error_messages_carry_locators():
    """契約 §5：有 context 的呼叫點必須把檔名 / 頁碼 / figure_id 帶進訊息。"""
    with pytest.raises(fx.FigureValidationError) as excinfo:
        fx.build_figure_chunks([_figure(row_total=1)], source="spec.pdf", doc_type="spec",
                               next_chunk_index={}, evidence_ref_by_figure={})
    message = str(excinfo.value)
    assert "spec.pdf" in message and "page=3" in message and FIGURE_ID in message


@pytest.mark.smoke
def test_facade_declares_every_contract_name():
    """契約 §6.2 + §13.5 的門面清單。漏一個名字，T7 會在 runtime 收 AttributeError。"""
    expected = {
        "plan_document_figures", "check_preflight", "render_candidate_variants",
        "estimate_image_tokens", "format_preflight_report",
        "Candidate", "PageEvidence", "Variant", "FigurePlan",
        "ensure_capability", "extract_document_figures", "FigureResult", "ProbeResult",
        "write_run_artifacts", "read_manifest", "list_figures", "apply_fix", "new_run_id",
        "prune_old_runs", "purge_document_artifacts", "evidence_ref_for", "source_signature",
        "may_carry_over_human_verification",
    }
    assert set(fx._FACADE_SOURCES) == expected
    assert set(fx._FACADE_SOURCES.values()) == {
        "figure_candidates", "figure_verify", "figure_review"}
    # 未宣告的名稱走一般 AttributeError（不會誤觸發子模組 import）
    with pytest.raises(AttributeError):
        fx.definitely_not_a_facade_name
    assert expected <= set(dir(fx))


# ============================================================
# 非 smoke — 大矩陣 / lexer / schema / 身分
# ============================================================
def test_duplicate_column_labels_keep_distinct_identity():
    """重複的 header label 不得讓兩欄變成同一欄：身分靠 column_id，不靠 label 文字。

    這裡只驗 canonical payload → render 的 invariant。真正的 borderless 偵測與
    multi-row header 攤平屬 T3/T4（candidate/parser），由那兩包的測試承接。
    """
    payload = _table(
        [["CTRL0", "[7:4]", "[3:0]"]],
        labels=("Name", "Bits", "Bits"),
    )
    assert [column["column_id"] for column in payload["columns"]] == ["c1", "c2", "c3"]
    _, labels, _, rows, _ = _parse_table_chunk(
        fx.render_table_text(payload, row_slice=None, meta=_meta()))
    assert labels == ["Name", "Bits", "Bits"]
    assert rows == [["CTRL0", "[7:4]", "[3:0]"]]


def test_cell_with_pipe_backslash_and_newline_round_trips():
    payload = _table([["a|b", "c\\d"], ["e\nf", "  spaced  "]])
    content = fx.render_table_text(payload, row_slice=None, meta=_meta())
    assert "\\|" in content and "\\\\" in content and "\\n" in content
    _, _, _, rows, _ = _parse_table_chunk(content)
    assert rows == [["a|b", "c\\d"], ["e\nf", "  spaced  "]]


def test_footnotes_render_only_with_the_first_row():
    payload = _table([["a", "b"], ["c", "d"], ["e", "f"]], footnotes=["note one", "note two"])
    first = fx.render_table_text(payload, row_slice=(1, 1), meta=_meta())
    later = fx.render_table_text(payload, row_slice=(2, 3), meta=_meta())
    assert "[FOOTNOTE 1] note one" in first and "[FOOTNOTE 2] note two" in first
    assert "FOOTNOTE" not in later, "同一段註腳出現兩次 = BM25 重複計數"


def test_render_rejects_bad_meta_and_out_of_range_slice():
    payload = _table([["a", "b"]])
    for meta, pattern in (
        ({"revision": 1, "page": 1, "verification_status": fx.VERIF_UNVERIFIED}, "缺少必要欄位"),
        (_meta(figure_id="placeholder"), "figure_id"),
        (_meta(revision=0), "revision"),
        (_meta(page="3"), "page"),
        (_meta(verification_status="made_up"), "verification_status"),
    ):
        with pytest.raises(fx.FigureValidationError, match=pattern):
            fx.render_table_text(payload, row_slice=None, meta=meta)

    for bad in ((0, 1), (1, 9), (2, 1), (1,), "1-2"):
        with pytest.raises(fx.FigureValidationError):
            fx.render_table_text(payload, row_slice=bad, meta=_meta())

    terminal = _terminal(["one", "two"])
    with pytest.raises(fx.FigureValidationError):
        fx.render_terminal_text(terminal, line_slice=(1, 5), meta=_meta())


def test_large_matrix_covers_every_row_exactly_once():
    rows = [[f"REG{i:03d}", f"0x{i:04X}_0000", f"[{i % 16}:0]", "RW", f"desc {i}"]
            for i in range(200)]
    payload = _table(rows, labels=REGISTER_LABELS)
    parts = fx.chunk_payload(payload, fx.KIND_TABLE, meta=_meta(), max_chars=500)
    assert len(parts) > 5

    seen: list[list[str]] = []
    previous_end = 0
    for part in parts:
        start, end = part["row_range"]
        assert start == previous_end + 1, "range 必須連續"
        previous_end = end
        _, labels, _, chunk_rows, _ = _parse_table_chunk(part["content"])
        assert labels == list(REGISTER_LABELS)
        seen.extend(chunk_rows)
    assert previous_end == 200
    assert seen == rows


def test_terminal_fence_grows_with_backtick_runs():
    payload = _terminal(["plain", "``", "```", "````code````"])
    content = fx.render_terminal_text(payload, line_slice=None, meta=_meta())
    fence = content.split("\n")[1]
    assert fence == "`" * 5
    _, texts = _parse_terminal_chunk(content)
    assert texts == ["plain", "``", "```", "````code````"]


@pytest.mark.parametrize("text,kind,expected", [
    ("CTRL0 0x4000_0100 [7:4] RW clock select", fx.KIND_TABLE,
     ["CTRL0", "0x4000_0100", "[7:4]", "RW", "clock", "select"]),
    ("addr 0x1000-0x1FFF, mask 0FFh, ver 1.2.3", fx.KIND_TABLE,
     ["addr", "0x1000-0x1FFF", ",", "mask", "0FFh", ",", "ver", "1.2.3"]),
    ("ip 192.168.1.10 mac AA:BB:CC:DD:EE:FF uuid 123e4567-e89b-12d3-a456-426614174000",
     fx.KIND_TERMINAL,
     ["ip", "192.168.1.10", "mac", "AA:BB:CC:DD:EE:FF",
      "uuid", "123e4567-e89b-12d3-a456-426614174000"]),
    ("fe80::1 [ 7 : 4 ] [3]", fx.KIND_TABLE, ["fe80::1", "[ 7 : 4 ]", "[3]"]),
    ("clk 100 MHz vdd 3.3V", fx.KIND_TABLE, ["clk", "100 MHz", "vdd", "3.3V"]),
    ("/usr/bin/foo --bar", fx.KIND_TERMINAL, ["/usr/bin/foo", "-", "-", "bar"]),
    ("系統啟動失敗 code=0x5A", fx.KIND_TERMINAL, ["系統啟動失敗", "code", "=", "0x5A"]),
])
def test_critical_tokens_ordered_ground_truth(text, kind, expected):
    """完整有序 token list 當 ground truth。

    只做 membership 斷言會放過「IPv6 被拆成兩段」「MAC 被吃掉一半」「相鄰標點把
    identifier 黏進來」這類錯誤，而那些會直接影響 critical token 一致性 → trusted 判定。
    """
    assert fx.critical_tokens(text, kind) == expected


def test_critical_tokens_treats_ansi_as_one_token():
    tokens = fx.critical_tokens("\x1b[0;32mOK\x1b[0m done", fx.KIND_TERMINAL)
    assert tokens[0] == "\x1b[0;32m"
    assert "OK" in tokens and tokens[-1] == "done"


def test_critical_tokens_keeps_cjk_as_legitimate_content():
    """workflow §4 Step 3 明令刪除「CJK > 30% 疑似翻譯」——中文 log 是合法原文。

    這條同時守住一個更基本的坑：Python 的 `\\w` 涵蓋 CJK，若 lexer 沒有 CJK 專屬
    分支，中文會既不被 identifier 也不被標點吃到，整段**靜默消失**。
    """
    text = "電源開啟後電壓為 3.3V，暫存器 0x4000_0100 未被寫入"
    tokens = fx.critical_tokens(text, fx.KIND_TABLE)
    assert "電源開啟後電壓為" in tokens
    assert "0x4000_0100" in tokens
    assert "3.3V" in tokens
    assert fx.critical_tokens("全中文的一行紀錄", fx.KIND_TERMINAL) == ["全中文的一行紀錄"]


def test_normalize_for_compare_collapses_all_whitespace():
    assert fx.normalize_for_compare("  a\t\tb\n\nc\u3000d\u00a0e  ") == "a b c d e"
    assert fx.normalize_for_compare("") == ""
    assert fx.normalize_for_compare("\n\t ") == ""
    # 只供比對，不是 canonical 文字：tab 與多空白在這個座標系上等價
    assert fx.normalize_for_compare("a\tb") == fx.normalize_for_compare("a    b")


def test_model_schema_is_fully_nested_and_isolated():
    def walk(node, path):
        assert "type" in node, path
        if node["type"] == "object":
            assert node.get("additionalProperties") is False, path
            assert "properties" in node and "required" in node, path
            assert set(node["required"]) == set(node["properties"]), path
            for key, child in node["properties"].items():
                walk(child, f"{path}.{key}")
        elif node["type"] == "array":
            assert "items" in node, path
            walk(node["items"], f"{path}[]")

    for kind in fx.FIGURE_KINDS:
        schema = fx.model_json_schema(kind)
        walk(schema, kind)
        wrapper = fx.response_format_for(kind)
        assert wrapper["type"] == "json_schema"
        assert wrapper["json_schema"]["strict"] is True
        assert wrapper["json_schema"]["name"] == fx.SCHEMA_NAME_BY_KIND[kind]
        assert wrapper["json_schema"]["schema"] == schema
        # 呼叫端就地改動不得污染下一次
        schema["properties"].clear()
        assert fx.model_json_schema(kind)["properties"], kind

    assert fx.model_json_schema(fx.KIND_TABLE)["properties"]["rows"]["items"]["properties"][
        "cells"]["items"]["properties"]["state"]["enum"] == ["observed", "unreadable"]
    for bad in (fx.KIND_UNKNOWN, "picture", ""):
        with pytest.raises(fx.FigureValidationError):
            fx.model_json_schema(bad)


@pytest.mark.parametrize("mutate", [
    lambda m: m.update({"notes": "extra"}),
    lambda m: m["columns"][0].update({"width": 10}),
    lambda m: m["rows"][0].update({"height": 3}),
    lambda m: m["rows"][0]["cells"][0].update({"confidence": 0.9}),
])
def test_canonicalize_table_rejects_nested_unknown_keys(mutate):
    """llama.cpp 可能忽略 `additionalProperties:false`；靜默丟棄多吐的欄位會讓
    canonical payload「看起來驗證成功」。"""
    model = _model_table([["CTRL0", "0x4000_0100"]])
    mutate(model)
    with pytest.raises(fx.FigureValidationError, match="key 不符"):
        fx.canonicalize_table(model)


@pytest.mark.parametrize("mutate", [
    lambda m: m.update({"notes": "extra"}),
    lambda m: m["lines"][0].update({"y": 1.0}),
    lambda m: m["lines"][0]["uncertain_spans"][0].update({"score": 0.5}),
])
def test_canonicalize_terminal_rejects_nested_unknown_keys(mutate):
    model = {"lines": [{"text": GLYPH, "uncertain_spans": [
        {"start": 0, "end": 1, "alternatives": ["8", "B"]}]}]}
    mutate(model)
    with pytest.raises(fx.FigureValidationError, match="key 不符"):
        fx.canonicalize_terminal(model)


@pytest.mark.parametrize("mutate", [
    lambda m: m.update({"notes": "extra"}),
    lambda m: m["components"][0].update({"kind": "block"}),
    lambda m: m["relations"][0].update({"weight": 1}),
    lambda m: m["values"][0].update({"unit": "Hz"}),
])
def test_canonicalize_diagram_rejects_nested_unknown_keys(mutate):
    model = {
        "title": "t", "labels": ["a"],
        "components": [{"name": "n", "desc": "d"}],
        "relations": [{"src": "a", "dst": "b", "desc": "d"}],
        "values": [{"key": "k", "value": "v", "desc": "d"}],
    }
    mutate(model)
    with pytest.raises(fx.FigureValidationError, match="key 不符"):
        fx.canonicalize_diagram(model)


def test_canonicalize_rejects_bad_types():
    with pytest.raises(fx.FigureValidationError):
        fx.canonicalize_table(_model_table([[123, "0x1"]]))
    with pytest.raises(fx.FigureValidationError):
        fx.canonicalize_terminal({"lines": [{"text": 5, "uncertain_spans": []}]})
    with pytest.raises(fx.FigureValidationError):
        fx.canonicalize_table({"columns": [], "rows": [], "footnotes": []})


def test_diagram_is_single_chunk_with_oversized_reason():
    """本輪沒有自動生產者（契約 §13.1），但人工 fix 走這條，所以必須完整可用。"""
    payload = _diagram()
    parts = fx.chunk_payload(payload, fx.KIND_DIAGRAM, meta=_meta())
    assert len(parts) == 1
    assert parts[0]["row_range"] is None and parts[0]["line_range"] is None
    assert parts[0]["oversized_row"] is False and parts[0]["oversized_line"] is False
    assert parts[0]["reasons"] == []
    content = parts[0]["content"]
    assert content.startswith("[FIGURE kind=diagram ")
    assert "rows=" not in content and "lines=" not in content
    assert "[COMPONENT] PLL: phase locked loop" in content
    assert "[RELATION] PLL -> DIV: feeds" in content
    assert "[VALUE] fout = 100 MHz: after divider" in content

    parts = fx.chunk_payload(payload, fx.KIND_DIAGRAM, meta=_meta(), max_chars=50)
    assert parts[0]["reasons"] == ["oversized_diagram"], "不濫用 row/line 的 oversized 旗標"
    assert parts[0]["oversized_row"] is False and parts[0]["oversized_line"] is False

    chunks = fx.build_figure_chunks(
        [_figure(payload=payload, kind=fx.KIND_DIAGRAM)],
        source="spec.pdf", doc_type="spec", next_chunk_index={},
        evidence_ref_by_figure=_evidence(FIGURE_ID))
    assert chunks[0]["origin"] == "figure_diagram"
    assert chunks[0]["row_total"] is None and chunks[0]["line_total"] is None


def test_document_identity(tmp_path):
    pdf = tmp_path / "docs" / "spec sheet.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.7\n")
    document_id = fx.document_id_for(pdf, tmp_path)
    digest = hashlib.sha256(b"%PDF-1.7\n").hexdigest()[:16]
    assert document_id == f"docs/spec sheet.pdf::{digest}"
    assert fx.display_name_for(document_id) == "spec sheet.pdf"

    slug = fx.document_slug(document_id)
    assert slug.startswith("docs_spec_sheet.pdf__")
    assert "/" not in slug and ":" not in slug and " " not in slug
    assert slug.endswith(hashlib.sha256(document_id.encode()).hexdigest()[:10])
    assert slug not in (".", "..")
    long_id = ("a" * 200) + "::" + digest
    assert len(fx.document_slug(long_id)) == 80 + 1 + 10

    outside = tmp_path.parent / "outside.pdf"
    outside.write_bytes(b"x")
    with pytest.raises(fx.FigureError, match="不在 root"):
        fx.document_id_for(outside, tmp_path)

    # 內容變了，身分就要變（re-ingest 不得沿用舊的 human verification）
    pdf.write_bytes(b"%PDF-1.7\nchanged\n")
    assert fx.document_id_for(pdf, tmp_path) != document_id


def test_document_id_fails_loud_when_source_changes_during_hash(tmp_path, monkeypatch):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"payload")
    real_stat = fx.os.stat
    calls = {"n": 0}

    def fake_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 2:  # hash 讀完之後那一次：模擬讀取期間來源被改寫
            return types.SimpleNamespace(
                st_size=result.st_size + 1,
                st_mtime_ns=result.st_mtime_ns,
                st_ino=result.st_ino,
            )
        return result

    # 只換 figure_extract 看到的 os（它只用 os.stat），不動全域 os.stat
    monkeypatch.setattr(fx, "os", types.SimpleNamespace(stat=fake_stat))
    with pytest.raises(fx.FigureError, match="被改寫"):
        fx.document_id_for(pdf, tmp_path)


DIGEST = hashlib.sha256(b"raster-bytes").hexdigest()


def test_figure_id_is_the_frozen_preimage():
    """golden：preimage 格式在這裡獨立寫一次，實作改格式就會紅。"""
    document_id = "docs/spec.pdf::0123456789abcdef"
    preimage = f"{document_id}|3|0.1176,0.1263,0.4902,0.5051|{DIGEST}"
    expected = "fig_" + hashlib.sha256(preimage.encode("utf-8")).hexdigest()[:16]
    actual = fx.figure_id_for(document_id, 3, (72.0, 100.0, 300.0, 400.0),
                              (0.0, 0.0, 612.0, 792.0), DIGEST)
    assert actual == expected
    assert fx.FIGURE_ID_RE.fullmatch(actual)

    # -0.0 必須正規化，否則同一張圖在不同上游版本會拿到不同的永久 ID
    rect = (0.0, 0.0, 100.0, 100.0)
    assert (fx.figure_id_for(document_id, 1, (-0.0, 0.0, 10.0, 10.0), rect, DIGEST)
            == fx.figure_id_for(document_id, 1, (0.0, 0.0, 10.0, 10.0), rect, DIGEST))
    # 位移一格 bbox 就是另一張圖
    assert (fx.figure_id_for(document_id, 1, (0.0, 0.0, 10.0, 10.0), rect, DIGEST)
            != fx.figure_id_for(document_id, 1, (0.0, 0.0, 10.0, 11.0), rect, DIGEST))
    # 來源像素變了就是另一張圖（asset_digest 是唯一綁定來源的欄位）
    other = hashlib.sha256(b"other-bytes").hexdigest()
    assert (fx.figure_id_for(document_id, 1, (0.0, 0.0, 10.0, 10.0), rect, DIGEST)
            != fx.figure_id_for(document_id, 1, (0.0, 0.0, 10.0, 10.0), rect, other))

    for bad_rect in ((0.0, 0.0, 0.0, 100.0), (0.0, 0.0, 100.0, 0.0)):
        with pytest.raises(fx.FigureError, match="退化"):
            fx.figure_id_for(document_id, 1, (0.0, 0.0, 1.0, 1.0), bad_rect, DIGEST)
    with pytest.raises(fx.FigureError):
        fx.figure_id_for(document_id, 1, (0.0, 0.0, float("inf"), 1.0), rect, DIGEST)
    with pytest.raises(fx.FigureError):
        fx.figure_id_for(document_id, 0, (0.0, 0.0, 1.0, 1.0), rect, DIGEST)


def test_asset_digest_must_bind_the_source_pixels():
    """契約 §2.5：asset_digest 是原始 asset / candidate signature 的 sha256。

    漏填（空字串）時 bbox 沒變的圖會算出同一個永久 figure_id，re-ingest 會沿用舊的
    human verification——figure_id 就不再綁定來源像素了。真正提供綁定性的是「內容變
    → digest 變 → figure_id 變」，所以這裡擋的是「空」；`SHA256_HEX_RE` 記錄了期望的
    格式，收緊成強制檢查需要主代理先同步其他 shard 的 fixture（見交付說明）。
    """
    document_id = "docs/spec.pdf::0123456789abcdef"
    rect = (0.0, 0.0, 100.0, 100.0)
    with pytest.raises(fx.FigureError, match="asset_digest"):
        fx.figure_id_for(document_id, 1, (0.0, 0.0, 1.0, 1.0), rect, "")
    with pytest.raises(fx.FigureError, match="asset_digest"):
        fx.figure_id_for(document_id, 1, (0.0, 0.0, 1.0, 1.0), rect, None)
    assert fx.SHA256_HEX_RE.fullmatch(DIGEST), "期望格式仍以常數形式留在模組裡"


def test_state_machine_constants_are_partitioned():
    assert fx.TRUSTED_VERIFICATION | fx.FLAGGED_VERIFICATION == set(fx.VERIFICATION_RANK)
    assert not (fx.TRUSTED_VERIFICATION & fx.FLAGGED_VERIFICATION)
    ranks = [fx.VERIFICATION_RANK[status] for status in (
        fx.VERIF_NEEDS_REVIEW, fx.VERIF_LEGACY, fx.VERIF_UNVERIFIED,
        fx.VERIF_CORROBORATED, fx.VERIF_NATIVE, fx.VERIF_HUMAN)]
    assert ranks == sorted(ranks) == [0, 1, 2, 3, 4, 5]
    assert set(fx.ORIGIN_BY_KIND) == set(fx.FIGURE_KINDS)
    assert set(fx.ORIGIN_BY_KIND.values()) == set(fx.FIGURE_ORIGINS)
    assert fx.VL_ORIGINS == {"image", "screenshot", "diagram"}
    assert not (fx.FIGURE_ORIGINS & fx.VL_ORIGINS)
    assert fx.MODEL_CELL_STATES < fx.CELL_STATES
    assert issubclass(fx.FigureExtractionError, fx.FigureError)
    for name in ("FigureValidationError", "FigureBudgetError", "FigureCapabilityError",
                 "FigureExtractionError", "FigureReviewError"):
        assert issubclass(getattr(fx, name), fx.FigureError)


@pytest.mark.smoke
def test_retrieval_context_rejects_an_explicit_null():
    """★ 明確寫進去的 `null` 是型別錯誤，不是「沒有這個欄位」。

    缺欄位已經由 `.get(name, "")` 給了空字串；把 null 悄悄當成空字串，等於替一份
    壞掉的 metadata 決定它的意思。list / dict / 數字同理（那些會被 `str()` 轉成
    Python repr 寫進 embedding 與 BM25，而看起來完全正常）。
    """
    figure = _figure()
    for bad in (None, ["Table", "3-1"], {"a": 1}, 3):
        with pytest.raises(fx.FigureValidationError, match="必須是 str"):
            fx.build_figure_chunks(
                [figure], source="spec.pdf", doc_type="spec",
                next_chunk_index={3: 0},
                evidence_ref_by_figure={figure.figure_id: "ref/manifest.json"},
                context_by_figure={figure.figure_id: {"caption": bad}})
    # 缺欄位仍然合法（舊 KB 的 chunk 沒有這些欄位）
    chunks = fx.build_figure_chunks(
        [figure], source="spec.pdf", doc_type="spec", next_chunk_index={3: 0},
        evidence_ref_by_figure={figure.figure_id: "ref/manifest.json"},
        context_by_figure={figure.figure_id: {"section": "3.2 Registers"}})
    assert chunks[0]["figure_caption"] == "" and chunks[0]["section"] == "3.2 Registers"


@pytest.mark.smoke
def test_retrieval_context_rejects_a_non_dict_entry():
    """★ 外層的 `None` / `[]` / `""` 不得被當成「這張圖沒有 context」。

    `(context_by_figure or {}).get(fid) or {}` 會把它們一律吞成空 dict，繞過下一行
    的型別檢查——欄位層修得再嚴，外層還是 fail-open。**缺 key** 才是合法的「沒給」。
    """
    figure = _figure()
    common = dict(source="spec.pdf", doc_type="spec",
                  evidence_ref_by_figure={figure.figure_id: "ref/manifest.json"})
    for bad in (None, [], "", 0):
        with pytest.raises(fx.FigureValidationError, match="必須是 dict"):
            fx.build_figure_chunks([figure], next_chunk_index={3: 0},
                                   context_by_figure={figure.figure_id: bad}, **common)
    # 整張圖不在 mapping 裡＝沒給，合法
    chunks = fx.build_figure_chunks([figure], next_chunk_index={3: 0},
                                    context_by_figure={}, **common)
    assert chunks[0]["figure_caption"] == ""


# ── 原 test_mcp_figure_tools.py：review_figures 的 MCP 公開邊界與 query 工具的 excluded_figures（整段皆 smoke） ──
FIG = "fig_0123456789abcdef"
FIG2 = "fig_fedcba9876543210"
DOC = "docs/npu_spec.pdf::0123456789abcdef"


def _entry(**over) -> dict:
    base = {
        "document_id": DOC,
        "display_name": "npu_spec.pdf",
        "source": "npu_spec.pdf",
        "figure_id": FIG,
        "revision": 2,
        "page": 7,
        "bbox": [10.0, 20.0, 300.0, 400.0],
        "kind": figure_extract.KIND_TABLE,
        "extraction_status": figure_extract.EXTRACTION_COMPLETE,
        "verification_status": figure_extract.VERIF_NEEDS_REVIEW,
        "reasons": ["glyph_conflict"],
        "reason_details": ["第 3 列第 2 格 8/B 衝突"],
        "payload": _table_payload(),
        "crop_path": ".codetrail/figures/npu_spec-abc/run1/assets/fig.png",
        "evidence_ref": ".codetrail/figures/npu_spec-abc/run1/manifest.json",
        "row_range": [1, 2],
        "line_range": None,
        "row_total": 2,
        "line_total": None,
        "in_kb": True,
        "fixable": True,
        "payload_error": "",
        "warnings": [],
        "crop_is_model_input": True,
        "variant_paths": {"crop@200dpi": ".codetrail/figures/npu_spec-abc/run1/assets/fig.png"},
        "review_asset_paths": {},
    }
    base.update(over)
    return base


def _table_payload(rows: int = 2) -> dict:
    return {
        "kind": "table",
        "columns": [
            {"column_id": "c1", "label": "Name", "role": None},
            {"column_id": "c2", "label": "Address", "role": None},
        ],
        "rows": [
            {"row_index": i,
             "cells": [
                 {"column_id": "c1", "text": f"CTRL{i}", "state": "observed",
                  "inherited_from_row": None},
                 {"column_id": "c2", "text": "0x4000_0100", "state": "observed",
                  "inherited_from_row": None},
             ]}
            for i in range(1, rows + 1)
        ],
        "footnotes": [],
    }


def _fix_result(**over) -> dict:
    """契約完整的 `apply_fix` 回傳。測試要驗哪一項不合格,就 override 哪一項。"""
    base = {
        "figure_id": FIG,
        "document_id": DOC,
        "kind": figure_extract.KIND_TABLE,
        "previous_revision": 2,
        "revision": 3,
        "verification_status": figure_extract.VERIF_HUMAN,
        "chunks_replaced": 4,
        "chunks_written": 5,
        "payload_path": ".codetrail/figures/x/run1/revisions/3/payload.json",
        "warnings": [],
    }
    base.update(over)
    return base


class _FakeKB:
    loaded = True
    chunks: list = []

    def get_status(self):
        return "[KB] 知識庫: 10 chunks"


def _mcp(monkeypatch, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    mcp = import_mcp_module(monkeypatch, root)
    monkeypatch.setattr(mcp, "_ensure_kb_fresh", lambda: None)
    monkeypatch.setattr(mcp, "KB", _FakeKB())
    return mcp


def _stub_list(monkeypatch, entries):
    # setitem 而非 setattr:門面是 PEP 562 lazy __getattr__,直接 getattr 會在
    # figure_review 尚未載入時去 import 它。寫進 __dict__ 兩種情況都安全。
    monkeypatch.setitem(figure_extract.__dict__, "list_figures",
                        lambda root, chunks, document_id=None: list(entries))


def _stub_apply(monkeypatch, fn):
    monkeypatch.setitem(figure_extract.__dict__, "apply_fix", fn)


class _Spy:
    def __init__(self, result=None, exc=None):
        self.calls: list[dict] = []
        self.result = result
        self.exc = exc

    def __call__(self, root, kb_path, **kwargs):
        self.calls.append(dict(kwargs))
        if self.exc is not None:
            raise self.exc
        return self.result


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_list_shows_every_contract_field(monkeypatch, tmp_path):
    """契約 §6.8 逐項:少了 document_id 下一次 fix 就指不到正確的文件。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])

    out = tool_fn(mcp, "review_figures")(action="list")

    # 斷言**完整欄位行**:只找 "needs_review" 會被表頭那句固定說明
    # 「(needs_review / unverified / legacy_unverified)」假陽性命中,
    # renderer 漏印或印錯 verification_status 時測試仍會綠。
    for needle in (DOC, FIG, "revision: 2", "kind: table", "page: 7",
                   "10.0",
                   f"extraction_status: {figure_extract.EXTRACTION_COMPLETE}",
                   f"verification_status: {figure_extract.VERIF_NEEDS_REVIEW}",
                   "glyph_conflict",
                   "8/B 衝突", "assets/fig.png", "manifest.json", "模型輸入"):
        assert needle in out, f"缺 {needle!r}\n{out}"
    # 多筆模式不附 payload(整份表格/log 會爆 context),但**必須明講**這件事,
    # 否則使用者會以為這張圖根本沒有 canonical payload。
    # 標記用單張模式實際印出的 payload 區塊標題:表頭的用法提示本身就含
    # 「canonical JSON」四個字,直接找那四個字會永遠誤判。
    assert "payload (canonical JSON" not in out, out
    assert "不附 canonical payload" in out, out
    assert "figure_id=" in out, out

    single = tool_fn(mcp, "review_figures")(action="list", figure_id=FIG)
    assert "payload (canonical JSON" in single, single
    assert "0x4000_0100" in single, single
    assert "不附 canonical payload" not in single, single


@pytest.mark.smoke
def test_document_identity_is_matched_byte_for_byte(monkeypatch, tmp_path):
    """文件身分不得被 `.strip()`。

    POSIX basename 可以用空白或換行開頭/結尾,而 KB 存的是原始 basename。
    通知(`ingest_notify`)給的建議命令用的正是逐位元組的身分;這裡若 strip,
    使用者照著貼上去就會「查不到」或「整份列出來」——兩邊對不上,而且無聲。
    """
    mcp = _mcp(monkeypatch, tmp_path)
    odd = " odd name.pdf "
    _stub_list(monkeypatch, [_entry(source=odd, display_name=odd, document_id=odd)])

    hit = tool_fn(mcp, "review_figures")(action="list", document_id=odd)
    assert FIG in hit, hit

    # strip 過的名字**不是**同一個身分,不得命中
    missed = tool_fn(mcp, "review_figures")(action="list", document_id=odd.strip())
    assert FIG not in missed, missed


@pytest.mark.smoke
def test_multi_entry_list_always_says_why_payload_is_missing(monkeypatch, tmp_path):
    """提示不能只在「有待覆核」時出現;每一次多筆列出都要講,包括全部可信的時候。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [
        _entry(figure_id=FIG, verification_status=figure_extract.VERIF_NATIVE),
        _entry(figure_id=FIG2, verification_status=figure_extract.VERIF_HUMAN),
    ])

    out = tool_fn(mcp, "review_figures")(action="list")

    assert "0 張待覆核" in out, out
    assert "不附 canonical payload" in out, out
    assert "figure_id=" in out, out


@pytest.mark.smoke
def test_list_degrades_per_figure_when_artifact_purged(monkeypatch, tmp_path):
    """review artifacts 被清掉的那幾張單獨降級,不能讓整份 list 爆掉。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [
        _entry(figure_id=FIG, payload=None, payload_error="manifest.json 不存在"),
        _entry(figure_id=FIG2, verification_status=figure_extract.VERIF_NATIVE),
    ])

    out = tool_fn(mcp, "review_figures")(action="list")

    assert "manifest.json 不存在" in out, out
    assert "重新 ingest" in out, out
    assert FIG2 in out, out


@pytest.mark.smoke
def test_list_never_cuts_a_canonical_value_in_half(monkeypatch, tmp_path):
    """`0x4000_0100` 被切成 `0x4000_010` 仍像合法值 —— 那就是無聲改寫。

    單一 figure 區塊超過輸出上限時,payload 必須**整份省略**,不得切中段。
    """
    mcp = _mcp(monkeypatch, tmp_path)
    huge = _table_payload(rows=400)
    _stub_list(monkeypatch, [_entry(payload=huge, row_total=400, row_range=[1, 400])])

    out = tool_fn(mcp, "review_figures")(action="list", figure_id=FIG)

    assert "整份省略" in out, out
    idx = 0
    while True:
        hit = out.find("0x4000_010", idx)
        if hit == -1:
            break
        assert out[hit:hit + 11] == "0x4000_0100", out[max(0, hit - 60):hit + 60]
        idx = hit + 1


@pytest.mark.smoke
def test_extraction_failure_is_stated_affirmatively(monkeypatch, tmp_path):
    """零部分成功 = 失敗的圖不進 KB。只掃 KB 的話它們會變成看不見的失敗。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry(
        in_kb=False, fixable=False, payload=None,
        extraction_status=figure_extract.EXTRACTION_FAILED,
        reason_details=["schema 重試後仍不合格"],
        warnings=["artifact_only"],
    )])

    out = tool_fn(mcp, "review_figures")(action="list")

    assert "in_kb: False" in out, out
    assert "fixable: False" in out, out
    # 判準要用**肯定句式**:「抽取失敗」四個字同時出現在反面文案
    # (「不是抽取失敗」)裡,拿裸字串當標記時,renderer 把真失敗誤印成
    # 「已被取代 / 不是抽取失敗」這條測試照樣會綠 —— 而覆核的人會被誤導。
    assert "這張**抽取失敗**" in out, out
    assert "不是抽取失敗" not in out, out
    assert "schema 重試後仍不合格" in out, out


@pytest.mark.smoke
def test_docs_never_claim_a_single_bad_figure_blocks_the_whole_document():
    """文件不得同時宣稱「任一條失敗整份不入庫」與「單張缺席、其餘照常入庫」。

    兩句話並存時,使用者(和模型)會把一次**其餘內容已經提交**的結果誤判成
    KB 沒變 —— 於是不去覆核、也不去 remove,而 KB 裡其實已經有那份文件了。
    """
    repo = Path(__file__).resolve().parent.parent
    # 只禁兩個固定短句是不夠的:同一個錯誤講法有很多寫法。這裡列的是**語義**
    # 等價的說法,而且涵蓋所有會被使用者/模型讀到的文件(含 troubleshooting)。
    forbidden = (
        "任一條失敗都整份不入庫",
        "任一失敗都整份不入庫",
        "任一失敗都是**整份文件不入庫**",
        "任一失敗都是整份文件不入庫",
        "一整份 PDF 會因此零寫入",
        "整份 PDF 因此零寫入",
        # 「圖片分析失敗 ⇒ 整份中止」是同一個錯誤語意的另一種寫法
        "若圖片分析失敗（ingest 會整份中止",
        "圖片分析失敗,ingest 會整份中止",
        "圖片分析失敗，ingest 會整份中止",
    )
    for name in ("README.md", "docs/mcp-tools.md", "docs/rag.md",
                 "docs/troubleshooting.md"):
        text = (repo / name).read_text(encoding="utf-8")
        for phrase in forbidden:
            assert phrase not in text, (name, phrase)
    # 反面:三份主要文件都要**明講**單張缺席的語意,不能只是把錯的句子刪掉。
    # 錨點要是**完整肯定句**:裸「缺席」會被「抽取失敗的圖不會缺席」這種
    # 反面文案命中 —— 那正是這條測試要擋的講法。
    affirmative = ("那一張缺席", "只讓那一張缺席", "那幾張缺席")
    negated = ("不會缺席", "不缺席", "沒有缺席")
    for name in ("docs/mcp-tools.md", "docs/rag.md", "docs/troubleshooting.md"):
        text = (repo / name).read_text(encoding="utf-8")
        assert any(phrase in text for phrase in affirmative), name
        for phrase in negated:
            assert phrase not in text, (name, phrase)


@pytest.mark.smoke
def test_superseded_old_run_is_not_called_an_extraction_failure(monkeypatch, tmp_path):
    """re-ingest 之後舊 run 的 manifest 還在,但它**不是**抽取失敗。

    把「已被取代」講成「抽取失敗」會讓人去追一個不存在的失敗,也會讓人誤以為
    現行 KB 內容有問題。
    """
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry(
        in_kb=False, fixable=False,
        extraction_status=figure_extract.EXTRACTION_COMPLETE,
        warnings=["artifact_only"],
    )])

    out = tool_fn(mcp, "review_figures")(action="list")

    assert "in_kb: False" in out, out
    # 判準要用**肯定句式**:被取代的那條文案本身就含「不是抽取失敗」,
    # 拿裸字串當標記會被自己的否定句命中(同一個坑這輪已經踩過兩次)
    assert "這張**抽取失敗**" not in out, out
    assert "不是抽取失敗" in out, out
    assert "取代" in out, out


@pytest.mark.smoke
def test_crop_that_was_never_sent_to_the_model_says_so(monkeypatch, tmp_path):
    """覆核的人不能拿一張模型從沒看過的圖去「確認」模型的抽取結果。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry(
        crop_is_model_input=False,
        crop_path=".codetrail/figures/npu_spec-abc/run1/review/fig-annotated.png",
        variant_paths={"crop@200dpi": ".codetrail/figures/npu_spec-abc/run1/variants/v.png"},
        review_asset_paths={
            "review": ".codetrail/figures/npu_spec-abc/run1/review/fig-annotated.png"},
    )])

    out = tool_fn(mcp, "review_figures")(action="list")

    assert "未送給模型" in out, out
    assert "模型實際看到的就是這張" not in out, out
    assert "variant_paths" in out, out


@pytest.mark.smoke
def test_missing_crop_is_not_claimed_as_model_input(monkeypatch, tmp_path):
    """舊 manifest 缺欄位 / 原圖已被清除時，一律不宣稱模型看過。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry(crop_path="", crop_is_model_input=False)])

    out = tool_fn(mcp, "review_figures")(action="list")

    assert "crop: (無原圖)" in out, out
    assert "模型輸入" not in out, out


@pytest.mark.smoke
def test_unknown_action_is_rejected(monkeypatch, tmp_path):
    mcp = _mcp(monkeypatch, tmp_path)
    out = tool_fn(mcp, "review_figures")(action="delete")
    assert out.startswith("錯誤"), out
    assert "list" in out and "fix" in out, out


# ---------------------------------------------------------------------------
# fix:零寫入的守門
# ---------------------------------------------------------------------------
@pytest.mark.smoke
@pytest.mark.parametrize("kwargs, needle", [
    ({"action": "fix"}, "figure_id"),
    ({"action": "fix", "figure_id": FIG}, "payload_json"),
    ({"action": "fix", "figure_id": FIG, "payload_json": "{}"}, "expected_revision"),
    ({"action": "fix", "figure_id": "fig_ffffffffffffffff", "payload_json": "{}",
      "expected_revision": 2}, "找不到 figure_id"),
])
def test_fix_missing_arguments_never_reach_backend(monkeypatch, tmp_path, kwargs, needle):
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    spy = _Spy(result=_fix_result())
    _stub_apply(monkeypatch, spy)

    out = tool_fn(mcp, "review_figures")(**kwargs)

    assert out.startswith("錯誤"), out
    assert needle in out, out
    assert spy.calls == [], "被擋下的請求不得碰 KB"


@pytest.mark.smoke
def test_fix_rejects_duplicate_json_keys(monkeypatch, tmp_path):
    """重複 key 的話 Python 只留最後一個 —— 那是升 human_verified 前的無聲改寫。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    spy = _Spy(result=_fix_result())
    _stub_apply(monkeypatch, spy)

    payload = ('{"kind": "table", "columns": [], "rows": [], '
               '"footnotes": [], "rows": [{"row_index": 1, "cells": []}]}')
    out = tool_fn(mcp, "review_figures")(
        action="fix", figure_id=FIG, expected_revision=2,
        payload_json=payload, confirm_against_image=True)

    assert out.startswith("錯誤"), out
    assert "重複" in out, out
    assert spy.calls == []


@pytest.mark.smoke
def test_fix_rejects_free_form_replacement(monkeypatch, tmp_path):
    """自由文字全段替換是明令禁止的入口。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    spy = _Spy(result=_fix_result())
    _stub_apply(monkeypatch, spy)

    out = tool_fn(mcp, "review_figures")(
        action="fix", figure_id=FIG, expected_revision=2,
        payload_json="| CTRL0 | 0x4000_0100 |\n| CTRL1 | 0x4000_0200 |",
        confirm_against_image=True)

    assert out.startswith("錯誤"), out
    assert spy.calls == []


@pytest.mark.smoke
def test_fix_kind_comes_from_kb_not_from_payload(monkeypatch, tmp_path):
    """payload 自報 kind 不能改變一張圖的類別。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry(kind=figure_extract.KIND_TABLE)])
    spy = _Spy(result=_fix_result())
    _stub_apply(monkeypatch, spy)

    payload = json.dumps({"kind": "terminal", "lines": []}, ensure_ascii=False)
    out = tool_fn(mcp, "review_figures")(
        action="fix", figure_id=FIG, expected_revision=2,
        payload_json=payload, confirm_against_image=True)

    assert out.startswith("錯誤"), out
    assert "kind" in out, out
    assert spy.calls == []


@pytest.mark.smoke
def test_fix_requires_explicit_human_confirmation(monkeypatch, tmp_path):
    """只把機器轉寫貼回來不算 human_verified。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    spy = _Spy(result=_fix_result())
    _stub_apply(monkeypatch, spy)

    out = tool_fn(mcp, "review_figures")(
        action="fix", figure_id=FIG, expected_revision=2,
        payload_json=json.dumps(_table_payload(), ensure_ascii=False),
        confirm_against_image=False)

    assert out.startswith("錯誤"), out
    assert "confirm_against_image" in out, out
    assert spy.calls == []


@pytest.mark.smoke
def test_fix_passes_kb_kind_and_canonical_document_id(monkeypatch, tmp_path):
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    spy = _Spy(result=_fix_result())
    _stub_apply(monkeypatch, spy)

    out = tool_fn(mcp, "review_figures")(
        action="fix", figure_id=FIG, expected_revision=2, document_id="npu_spec.pdf",
        payload_json=json.dumps(_table_payload(), ensure_ascii=False),
        confirm_against_image=True)

    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["kind"] == figure_extract.KIND_TABLE
    assert call["document_id"] == DOC, "必須用 KB 的 canonical document_id,不是使用者輸入"
    assert call["expected_revision"] == 2
    assert call["confirm_against_image"] is True
    assert callable(call["rechunk"]) and callable(call["embed"])
    assert "2 → 3" in out, out
    assert "替換 4" in out and "寫入 5" in out, out
    assert figure_extract.VERIF_HUMAN in out, out


@pytest.mark.smoke
def test_fix_surfaces_backend_warnings(monkeypatch, tmp_path):
    """KB 已提交但 artifact mirror 失敗時的 warnings 不得被無聲吞掉。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    _stub_apply(monkeypatch, _Spy(result=_fix_result(
        warnings=["manifest 未能更新 current_revision(KB 已提交)"])))

    out = tool_fn(mcp, "review_figures")(
        action="fix", figure_id=FIG, expected_revision=2,
        payload_json=json.dumps(_table_payload(), ensure_ascii=False),
        confirm_against_image=True)

    assert "warnings" in out, out
    assert "manifest 未能更新" in out, out


@pytest.mark.smoke
@pytest.mark.parametrize("bad, needle", [
    ({}, "figure_id"),                                       # 空 dict
    ({"figure_id": FIG2}, "figure_id"),                      # 身分不符
    ({"document_id": "other.pdf::0000000000000000"}, "document_id"),
    ({"kind": figure_extract.KIND_TERMINAL}, "kind"),
    ({"previous_revision": 7}, "previous_revision"),
    ({"revision": 2}, "revision"),                           # revision 沒動
    ({"revision": 9}, "revision"),                           # 不是 +1
    ({"verification_status": figure_extract.VERIF_UNVERIFIED}, "verification_status"),
    ({"chunks_replaced": 0}, "chunks_replaced"),             # 零 chunk 不是成功
    ({"chunks_written": 0}, "chunks_written"),
    ({"warnings": "boom"}, "warnings"),
])
def test_malformed_apply_fix_result_is_never_reported_as_success(
    monkeypatch, tmp_path, bad, needle
):
    """`human_verified` 是唯一由人背書的狀態。

    只要「是個 dict」就印 `fix ✓` 的話,空 dict、身分不符、revision 沒動、零 chunk
    都會變成一句「已升級為 human_verified」,而使用者之後會拿它當可信數值用。
    """
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    payload = dict(_fix_result())
    payload.update(bad)
    if not bad:
        payload = {}
    _stub_apply(monkeypatch, _Spy(result=payload))

    with pytest.raises(RuntimeError) as excinfo:
        tool_fn(mcp, "review_figures")(
            action="fix", figure_id=FIG, expected_revision=2,
            payload_json=json.dumps(_table_payload(), ensure_ascii=False),
            confirm_against_image=True)

    message = str(excinfo.value)
    assert needle in message, message
    assert "拒絕宣稱修正成功" in message, message


@pytest.mark.smoke
def test_non_dict_apply_fix_result_is_fail_loud(monkeypatch, tmp_path):
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    _stub_apply(monkeypatch, _Spy(result="ok"))

    with pytest.raises(RuntimeError):
        tool_fn(mcp, "review_figures")(
            action="fix", figure_id=FIG, expected_revision=2,
            payload_json=json.dumps(_table_payload(), ensure_ascii=False),
            confirm_against_image=True)


@pytest.mark.smoke
def test_stale_revision_is_a_retryable_conflict(monkeypatch, tmp_path):
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    # figure_review 的真實格式:"<where>: conflict — <detail>"
    _stub_apply(monkeypatch, _Spy(exc=figure_extract.FigureReviewError(
        "apply_fix(npu_spec.pdf/fig_0123456789abcdef): conflict — revision 已由 2 "
        "變成 [5],拒絕覆寫(不做 last-write-wins)")))

    out = tool_fn(mcp, "review_figures")(
        action="fix", figure_id=FIG, expected_revision=2,
        payload_json=json.dumps(_table_payload(), ensure_ascii=False),
        confirm_against_image=True)

    assert out.startswith("錯誤"), out
    assert "conflict" in out and "零寫入" in out, out


@pytest.mark.smoke
def test_path_violation_is_fail_loud_not_a_conflict(monkeypatch, tmp_path):
    """訊息裡剛好出現 conflict 這個字的路徑違規,不得被降級成可重試的衝突。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_list(monkeypatch, [_entry()])
    _stub_apply(monkeypatch, _Spy(exc=figure_extract.FigureReviewError(
        "refusing to follow symlink /tmp/conflict-dir outside .codetrail/figures")))

    with pytest.raises(figure_extract.FigureReviewError):
        tool_fn(mcp, "review_figures")(
            action="fix", figure_id=FIG, expected_revision=2,
            payload_json=json.dumps(_table_payload(), ensure_ascii=False),
            confirm_against_image=True)


# ---------------------------------------------------------------------------
# excluded_figures:四條 strict 回傳路徑 + 一般查詢
# ---------------------------------------------------------------------------
_EXCLUDED = [{
    "source": "npu_spec.pdf", "page": 7, "figure_id": FIG, "figure_index": 1,
    "figure_kind": "table", "verification_status": figure_extract.VERIF_NEEDS_REVIEW,
    "reasons": ["glyph_conflict"],
}]


def _stub_query(monkeypatch, mcp, *, meta, context="ctx"):
    class _KB(_FakeKB):
        def query(self, question, is_strict_mode=False, source=None):
            return (context, "display", meta)
    monkeypatch.setattr(mcp, "KB", _KB())


@pytest.mark.smoke
def test_query_knowledge_carries_excluded_figures(monkeypatch, tmp_path):
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_query(monkeypatch, mcp, meta={"refs": [], "top_score": 0.4,
                                        "excluded_figures": _EXCLUDED})

    # Core payload contract: the transport adapter renders this separately.
    result = mcp.query_knowledge("reset timing")

    assert result["excluded_figures"] == _EXCLUDED
    assert "review_figures" in result["review_hint"]


@pytest.mark.smoke
def test_query_knowledge_not_loaded_still_has_the_key(monkeypatch, tmp_path):
    mcp = _mcp(monkeypatch, tmp_path)

    class _Empty(_FakeKB):
        loaded = False
    monkeypatch.setattr(mcp, "KB", _Empty())

    result = mcp.query_knowledge("reset timing")
    assert result["excluded_figures"] == []
    assert result["review_hint"] == ""


@pytest.mark.smoke
@pytest.mark.parametrize("refuse, grounding, context, expected_reason", [
    (True, True, "ctx", "weak_ref_for_spec_question"),
    (False, False, "", "no_kb_ctx"),
    (False, False, "ctx", "explicit_strict"),
    (False, True, "ctx", "spec_number"),
])
def test_strict_return_paths_all_carry_excluded_figures(
    monkeypatch, tmp_path, refuse, grounding, context, expected_reason
):
    """拒答、缺 context 與顯式 strict 成功都要說得出「哪張圖待覆核」。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_query(monkeypatch, mcp, meta={
        "refs": [], "top_score": 0.4, "top_emb_score": 0.3,
        "excluded_figures": _EXCLUDED,
    }, context=context)
    monkeypatch.setattr(mcp, "should_refuse_answer", lambda q, m, **kw: refuse)
    monkeypatch.setattr(mcp, "needs_grounding", lambda q: (grounding, "spec_number"))
    monkeypatch.setattr(mcp, "answer_with_self_check",
                        lambda q, b, k, binary_ctx="": "答案")

    result = mcp.query_knowledge_strict("reset assert 最小時間")

    assert result["reason"] == expected_reason, result
    assert result["refused"] is (refuse or not context), result
    assert result["strict"] is True, result
    assert result["excluded_figures"] == _EXCLUDED, result
    assert "review_figures" in result["review_hint"], result
    assert "待覆核" in result["review_hint"], result


_LEGACY_EXCLUDED = [{
    "source": "scanned.pdf", "page": 12, "figure_index": 1,
    "figure_kind": "diagram", "verification_status": figure_extract.VERIF_LEGACY,
    "reasons": ["legacy_missing_verification"],
}]


@pytest.mark.smoke
def test_legacy_raster_exclusion_is_not_sent_to_review_figures(monkeypatch, tmp_path):
    """舊 VL / 純 raster chunk 不會出現在 review_figures,叫使用者去那裡找必然撲空。"""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_query(monkeypatch, mcp, meta={"refs": [], "top_score": 0.4,
                                        "excluded_figures": _LEGACY_EXCLUDED})

    hint = mcp.query_knowledge("reset timing")["review_hint"]

    assert "scanned.pdf p.12" in hint, hint
    assert "不可覆核" in hint, hint
    assert "出現在 review_figures" in hint, hint
    assert "原始 PDF" in hint, hint
    # 不得把 legacy 導向 fix 流程
    assert "confirm_against_image" not in hint, hint


@pytest.mark.smoke
def test_structured_and_legacy_exclusions_are_reported_separately(monkeypatch, tmp_path):
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_query(monkeypatch, mcp, meta={
        "refs": [], "top_score": 0.4,
        "excluded_figures": _EXCLUDED + _LEGACY_EXCLUDED,
    })

    hint = mcp.query_knowledge("reset timing")["review_hint"]

    # **不能用「可覆核」當標記**:它是「不可覆核」的子字串。structured 那一段
    # 若被錯寫成 legacy 文案,`"可覆核" in hint` 照樣成立 —— 使用者被導去看原始
    # PDF,而那張圖其實 review_figures 修得動。`review_figures` 同理:它也出現在
    # legacy 那段的否定句「**不會**出現在 review_figures 裡」。
    # 所以兩段各用自己的完整肯定句當錨點。
    assert "可覆核(結構化抽取):" in hint, hint
    assert "不可覆核(舊 KB legacy 視覺辨識):" in hint, hint
    assert 'review_figures(action="fix"' in hint, hint          # 只有 structured 段有
    assert "不會**出現在 review_figures 裡" in hint, hint        # 只有 legacy 段有
    assert hint.index("可覆核(結構化抽取):") < hint.index("不可覆核("), hint
    assert hint.index("npu_spec.pdf") < hint.index("scanned.pdf"), hint


@pytest.mark.smoke
def test_strict_kb_not_loaded_still_has_the_key(monkeypatch, tmp_path):
    mcp = _mcp(monkeypatch, tmp_path)

    class _Empty(_FakeKB):
        loaded = False
    monkeypatch.setattr(mcp, "KB", _Empty())

    result = mcp.query_knowledge_strict("reset assert 最小時間")
    assert result["reason"] == "knowledge_base_not_loaded"
    assert result["excluded_figures"] == []
    assert result["review_hint"] == ""


@pytest.mark.smoke
def test_query_transport_is_compact_and_keeps_exclusion_guidance(monkeypatch, tmp_path):
    """Model-visible text is compact while the UI retains the structured payload."""
    mcp = _mcp(monkeypatch, tmp_path)
    _stub_query(monkeypatch, mcp, meta={
        "refs": [{"source": "npu_spec.pdf", "page": 7}],
        "top_score": 0.4,
        "excluded_figures": _EXCLUDED,
    })
    transport = mcp.mcp._tool_manager.get_tool("query_knowledge").fn

    result = transport(question="reset timing")
    assert len(result.content) == 1
    text = result.content[0].text
    assert text.startswith("status: partial\nnext: "), text
    assert "npu_spec.pdf p.7" in text, text
    assert "review_figures" in text and "待覆核" in text, text
    assert '"display"' not in text and '"refs"' not in text, text
    assert result.structuredContent["excluded_figures"] == _EXCLUDED
