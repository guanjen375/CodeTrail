"""figure_verify 的契約防護：capability probe、structured 抽取、格/行級 verifier。

全部離線：VL 一律用 stub，`ensure_capability` 的 canary 圖也可以被換掉，所以
CI 不需要 llama-server，也不需要 PyMuPDF（除了明確標注的那一條）。

smoke 的挑選標準（AGENTS.md §2.1 第 2 款「無聲失敗風險的契約」）：只收
「會讓錯誤內容安靜入庫」的最小案例——截斷／非 schema JSON／欄寬錯／空 payload
被當成成功、字元衝突被擇一、遮罩偏移算錯、probe 沒過還是抽了、第二次取樣重播
prompt cache 冒充獨立佐證、自我佐證升級成 corroborated、以及失敗時回傳半套結果。
其餘（native 多策略、rowspan、dual pass、tile 接合、probe 快取失效矩陣）保持
離線但不塞進十秒 smoke。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

import config
import figure_extract
import figure_verify

REPO_ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# 共用 stub（duck-typed：刻意不 import figure_candidates，避免 import 循環）
# ============================================================
def fig_id(seed: str) -> str:
    """真格式的 figure_id：`fig_` + 16 位小寫 hex（build_figure_chunks 會驗）。"""
    return "fig_" + hashlib.sha256(seed.encode()).hexdigest()[:16]


DOC_ID = "docs/spec.pdf::0123456789abcdef"

_FAKE_MODEL_PATH: str | None = None


def fake_model_path(payload: bytes = b"GGUF-stub") -> str:
    """真的存在、stat 得到的假模型檔。

    `_model_identity()` 只認「成功 stat 到的 size + mtime」——`model_path` 只是
    字串，stat 不到就證明不了背後那顆 GGUF 沒被換掉（同路徑覆蓋是升級模型的標準
    做法）。所以 probe 快取的測試必須指向一個真的檔案。
    """
    global _FAKE_MODEL_PATH
    if _FAKE_MODEL_PATH is None:
        handle, path = tempfile.mkstemp(prefix="codetrail-fake-vl-", suffix=".gguf")
        os.close(handle)
        _FAKE_MODEL_PATH = path
    target = Path(_FAKE_MODEL_PATH)
    # 只有內容真的變了才重寫：無條件寫入會動到 mtime，而 mtime 正是 fingerprint
    # 的一部分，快取命中測試會因此永遠 miss。
    if not target.exists() or target.read_bytes() != payload:
        target.write_bytes(payload)
    return _FAKE_MODEL_PATH


def candidate(*, kind=figure_extract.KIND_TABLE, page=4, seed="c1", bbox=(0.0, 0.0, 200.0, 80.0),
              native_table=None, asset_digest="digest-1", index=1, occurrences=None,
              native_lane=None, signals=None):
    """duck-typed Candidate。

    `signals["native_lane"]` 是 lane 的**唯一真相**（契約 §15.1）；預設值只是
    fixture 的方便寫法（有 native_table ⇒ True），測試要驗 lane 分流時一律明寫。
    """
    resolved = dict(signals or {})
    resolved.setdefault(
        "native_lane", bool(native_table) if native_lane is None else bool(native_lane)
    )
    if native_lane is not None:
        resolved["native_lane"] = bool(native_lane)
    return types.SimpleNamespace(
        index=index, page=page, bbox=bbox, page_rect=(0.0, 0.0, 595.0, 842.0),
        kind=kind, kind_scores={}, signals=resolved, reasons=[], signature="sig",
        native_table=native_table,
        occurrences=occurrences if occurrences is not None
        else [{"page": page, "bbox": list(bbox), "index": 0}],
        asset_xref=None, asset_digest=asset_digest,
        figure_id=fig_id(seed), document_id=DOC_ID,
    )


def page_evidence(*, page=4, raw_markdown="", page_boxes=None, words=None, tables=None):
    return types.SimpleNamespace(
        page=page, raw_markdown=raw_markdown, page_boxes=page_boxes or [],
        words=words or [], image_info=[], tables=tables or {}, drawing_clusters=[],
        page_rect=(0.0, 0.0, 595.0, 842.0), rotation=0, unavailable=[],
    )


def variant(figure_id, *, variant_id="crop@200dpi", tile_index=0, tile_total=1,
            overlap_px=0, est_image_tokens=120, mime="image/png", png=b"\x89PNG-stub",
            width=400, height=200, bbox=(0.0, 0.0, 200.0, 80.0), digest=None,
            stitch=None):
    """duck-typed Variant，契約 §6.3 的欄位**一個都不缺**。

    `digest` 預設是 `png` 的真 sha256（不是佔位字串）：門面的
    `figure_extract.validate_variant()` 會逐條驗身分，而**缺欄位或填假值的 fixture
    正是這條接縫連續四輪沒被抓到的成因**——producer 漂移時測試照樣綠。要驗
    「digest 與 bytes 對不上」這種情境時才明寫 `digest=`。
    """
    if digest is None:
        # 型別壞掉的 png 也要能組出 fixture（那正是要被 validator 擋下的案例）。
        digest = hashlib.sha256(png).hexdigest() if isinstance(png, bytes) else ""
    return types.SimpleNamespace(
        figure_id=figure_id, variant_id=variant_id, png=png, width=width, height=height,
        bbox=bbox, tile_index=tile_index, tile_total=tile_total,
        overlap_px=overlap_px, est_image_tokens=est_image_tokens, digest=digest,
        mime=mime, stitch=stitch or {},
    )


def words_from(rows):
    """rows: [(y, [(x0, x1, text, block_no?), ...]), ...] → get_text("words") 的 8-tuple。"""
    out = []
    for default_block, (y, items) in enumerate(rows):
        for word_no, item in enumerate(items):
            x0, x1, text = item[0], item[1], item[2]
            block = item[3] if len(item) > 3 else default_block
            out.append((float(x0), float(y), float(x1), float(y) + 10.0, text, block, 0, word_no))
    return out


class VLSpy:
    """假 vision_json_completion：依 schema 名稱與呼叫序回應，並記錄每次 kwargs。"""

    def __init__(self, script, finish_reason="stop"):
        self.script = script
        self.finish_reason = finish_reason
        self.calls: list[dict] = []
        self._per_schema: dict[str, int] = {}

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        name = kwargs["response_format"]["json_schema"]["name"]
        seq = self._per_schema.get(name, 0)
        self._per_schema[name] = seq + 1
        entry = self.script[name]
        text = entry[min(seq, len(entry) - 1)] if isinstance(entry, list) else entry
        if isinstance(text, Exception):
            raise text
        finish = self.finish_reason
        return types.SimpleNamespace(
            text=text, finish_reason=finish, truncated=finish not in ("stop", "eos"),
            usage={}, raw={},
        )

    def schema_names(self) -> list[str]:
        return [c["response_format"]["json_schema"]["name"] for c in self.calls]


def table_json(columns, rows, footnotes=()):
    return json.dumps({
        "columns": [{"label": label} for label in columns],
        "rows": [{"cells": [{"text": text, "state": state} for text, state in row]}
                 for row in rows],
        "footnotes": list(footnotes),
    })


def terminal_json(lines):
    return json.dumps({
        "lines": [{"text": text, "uncertain_spans": list(spans)} for text, spans in lines]
    })


REGISTER_TABLE = table_json(
    ["Name", "Address", "Bits", "Mode"],
    [[("CTRL0", "observed"), ("0x8000_0100", "observed"),
      ("[7:4]", "observed"), ("RW", "observed")]],
)


DEFAULT_PROPS = object()


def install_vl(monkeypatch, spy, *, props=DEFAULT_PROPS):
    resolved = ({"model_path": fake_model_path(), "model_alias": "vl",
                 "chat_template": "supports json_schema", "n_ctx": 8192}
                if props is DEFAULT_PROPS else props)
    monkeypatch.setattr(figure_verify.llama_client, "vision_json_completion", spy, raising=False)
    monkeypatch.setattr(
        figure_verify.llama_client, "get_props", lambda *a, **k: resolved, raising=False,
    )
    monkeypatch.setattr(
        figure_verify.llama_client, "vision_completion", lambda **k: "OK", raising=False
    )


def pass_probe(monkeypatch):
    monkeypatch.setattr(
        figure_verify, "ensure_capability",
        lambda **kwargs: figure_verify.ProbeResult(True, "fp", {"stub": True}, [], "stub"),
    )


def extract(candidates, evidences, *, render=None, on_progress=None):
    plan = types.SimpleNamespace(
        document_id=DOC_ID, candidates=list(candidates), page_evidence=evidences,
        stats={}, preflight={}, over_budget=[],
    )
    return figure_verify.extract_document_figures(
        plan, pdf_doc=None, page_evidence=evidences,
        vl_base_url="http://127.0.0.1:8083", vl_model="vl",
        # 預設 renderer 必須跟真 producer 一樣「未切片就是整個候選框」：
        # `render_candidate_variants()` 的未切片分支 render 的就是 `candidate.bbox`，
        # 而送出前那道閘會比對（契約 §21.10）。寫死一個框的 fixture 只要遇到
        # 非預設 bbox 的候選就會漂掉——這正是本接縫連續四輪的成因。
        render_variants=render or (
            lambda _doc, cand: [variant(cand.figure_id, bbox=cand.bbox)]),
        on_progress=on_progress,
    )


# ============================================================
# smoke —— 會讓錯誤內容安靜入庫的最小案例
# ============================================================
@pytest.mark.smoke
def test_truncated_response_is_fail_loud(monkeypatch):
    """`finish_reason="length"` 的輸出即使湊巧能 parse 也不得採用。"""
    spy = VLSpy({"figure_table": REGISTER_TABLE}, finish_reason="length")
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        extract([candidate()], {4: page_evidence()})

    message = str(excinfo.value)
    assert "truncated" in message
    assert "AICODE_VL_INGEST_MAX_TOKENS" in message, "截斷要給得出可行動的建議"
    # 重試一次（config.FIGURE_EXTRACT_RETRIES）之後才放棄
    assert len(spy.calls) == 1 + config.FIGURE_EXTRACT_RETRIES


@pytest.mark.smoke
@pytest.mark.parametrize(
    "text, slug",
    [
        ("這是一張暫存器表格，共有四欄。", "not_json"),
        (json.dumps({"table": []}), "schema"),
        (json.dumps(["columns", "rows"]), "schema"),
    ],
)
def test_http_200_but_not_schema_json_is_fail_loud(monkeypatch, text, slug):
    """HTTP 200 不等於守約：llama.cpp 的 grammar 只支援 JSON Schema 的子集。"""
    spy = VLSpy({"figure_table": text})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        extract([candidate()], {4: page_evidence()})
    assert slug in str(excinfo.value)


@pytest.mark.smoke
def test_row_width_mismatch_is_fail_loud(monkeypatch):
    """欄寬不對就是不合格——補一格會憑空造值，砍一格會讓後面全部錯位。"""
    bad = table_json(
        ["Name", "Address", "Mode"],
        [[("CTRL0", "observed"), ("0x8000_0100", "observed")]],
    )
    spy = VLSpy({"figure_table": bad})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        extract([candidate()], {4: page_evidence()})
    message = str(excinfo.value)
    assert "row_width" in message
    assert "2 格" in message and "3 欄" in message, "訊息要指得出是哪一列、差多少"


@pytest.mark.smoke
@pytest.mark.parametrize(
    "kind, schema_name, text",
    [
        (figure_extract.KIND_TABLE, "figure_table", table_json(["Name"], [])),
        (figure_extract.KIND_TERMINAL, "figure_terminal", terminal_json([])),
    ],
)
def test_empty_payload_is_fail_loud(monkeypatch, kind, schema_name, text):
    """空 payload 等於宣稱「這張圖沒有內容」，不得入庫。"""
    spy = VLSpy({schema_name: text})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        extract([candidate(kind=kind)], {4: page_evidence()})
    assert "empty_payload" in str(excinfo.value)


@pytest.mark.smoke
def test_hex_glyph_disagreement_masks_the_cell_and_keeps_neither(monkeypatch):
    """兩次取樣只差 8/B → 該格必須 `▯`，候選只進 evidence，**不得擇一**。"""
    sample_a = REGISTER_TABLE
    sample_b = REGISTER_TABLE.replace("0x8000_0100", "0xB000_0100")
    spy = VLSpy({"figure_table": [sample_a, sample_b]})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    result = extract([candidate()], {4: page_evidence()})[0]

    cell = result.payload["rows"][0]["cells"][1]
    assert cell["text"] == "0x▯000_0100"
    assert cell["text"] not in ("0x8000_0100", "0xB000_0100")
    assert cell["state"] == figure_extract.CELL_STATE_CONFLICT
    assert result.verification_status == figure_extract.VERIF_NEEDS_REVIEW
    assert "glyph_conflict" in result.reasons
    spans = result.evidence["repeatability"]["detail"]["cells"]["r1c2"]["mask_spans"]
    assert [list(alts) for _s, _e, alts in spans] == [["8", "B"]]


@pytest.mark.smoke
def test_terminal_glyph_disagreement_uses_uncertain_spans(monkeypatch):
    """terminal 的候選只能進 `uncertain_spans`，不得混進逐字正文。"""
    line_a = terminal_json([("addr 0xF0", []), ("", [])])
    line_b = terminal_json([("addr 0xFO", []), ("", [])])
    spy = VLSpy({"figure_terminal": [line_a, line_b]})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    result = extract(
        [candidate(kind=figure_extract.KIND_TERMINAL)], {4: page_evidence()}
    )[0]

    line = result.payload["lines"][0]
    assert line["text"] == "addr 0xF▯"
    assert "[不確定" not in line["text"]
    assert line["uncertain_spans"] == [{"start": 8, "end": 9, "alternatives": ["0", "O"]}]
    assert result.verification_status == figure_extract.VERIF_NEEDS_REVIEW
    # 空行是合法 canonical line，不得被吞掉
    assert [ln["text"] for ln in result.payload["lines"]] == ["addr 0xF▯", ""]
    assert result.line_total == 2


@pytest.mark.smoke
def test_normalized_diff_masks_the_right_raw_offset(monkeypatch):
    """在 normalize 過的字串上找差異、卻遮罩原字串 → `▯` 會蓋錯字元。

    `normalize_for_compare` 會把連續空白壓成一個空格再 strip，所以 normalized
    index ≠ 原字串 index。這條守的是那張 offset 對照表。
    """
    verdict, spans = figure_verify._compare_atom("  A  8", " A   B ")
    assert verdict == "glyph"
    assert spans == [(5, 6, ["8", "B"])], "差異必須映回原字串的第 5 個字元"
    masked = figure_verify._mask_text("  A  8", spans)
    assert masked == "  A  ▯"
    assert len(masked) == len("  A  8"), "遮罩必須等長，否則 span index 全部位移"

    # tab / 全形空白 / CJK 混排都要走同一張表
    for text in ("\tA \t8\t", "A　8", "  0x8000_0100  ", "中文 8 結尾"):
        norm, offsets = figure_verify._norm_with_map(text)
        assert norm == figure_extract.normalize_for_compare(text)
        assert len(norm) == len(offsets)
        for index, char in enumerate(norm):
            start, end = offsets[index]
            assert 0 <= start < end <= len(text)
            if char != " ":
                assert text[start:end] == char


@pytest.mark.smoke
def test_structural_conflict_keeps_neither_candidate(monkeypatch):
    """長度/結構不同 → 定位不到字元，正文換成安全占位，兩份原文只進 evidence。"""
    sample_a = REGISTER_TABLE
    sample_b = REGISTER_TABLE.replace("0x8000_0100", "0x8000_010")
    spy = VLSpy({"figure_table": [sample_a, sample_b]})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    result = extract([candidate()], {4: page_evidence()})[0]

    cell = result.payload["rows"][0]["cells"][1]
    assert cell["text"] == figure_extract.UNREADABLE_GLYPH
    assert cell["state"] == figure_extract.CELL_STATE_CONFLICT
    assert result.verification_status == figure_extract.VERIF_NEEDS_REVIEW
    record = result.evidence["repeatability"]["detail"]["cells"]["r1c2"]
    assert sorted(record["alternatives"]) == ["0x8000_010", "0x8000_0100"]
    # 正文不得留下任何一份候選，也不得出現「補遺」式的旁註
    blob = json.dumps(result.payload, ensure_ascii=False)
    assert "0x8000_0100" not in blob and "0x8000_010" not in blob
    assert "未經文字層佐證" not in blob and "補遺" not in blob


@pytest.mark.smoke
def test_capability_probe_failure_blocks_every_extraction(monkeypatch, tmp_path):
    """probe 沒過 ⇒ 零抽取、零 render、零 KB mutation，而且失敗不進快取。"""
    monkeypatch.setenv("AICODE_FIGURE_PROBE_FILE", str(tmp_path / "probe.json"))
    spy = VLSpy({"figure_table": REGISTER_TABLE}, finish_reason="length")
    install_vl(monkeypatch, spy)
    monkeypatch.setattr(figure_verify, "_render_canary_png", lambda spec: b"\x89PNG-canary")

    rendered: list = []
    with pytest.raises(figure_extract.FigureCapabilityError) as excinfo:
        extract([candidate()], {4: page_evidence()},
                render=lambda _doc, cand: rendered.append(cand) or [variant(cand.figure_id)])

    message = str(excinfo.value)
    assert "table.response_not_truncated" in message
    assert "OpenAI-compatible" in message, "不得以「相容」推定品質"
    assert rendered == [], "probe 沒過就不該 render 任何 variant"
    assert not (tmp_path / "probe.json").exists(), "失敗一律不快取"
    # probe 之外沒有任何抽取呼叫：所有呼叫都帶 canary，不含真實候選的 variant
    assert all(call["cache_prompt"] is False for call in spy.calls)


@pytest.mark.smoke
def test_second_sample_disables_prompt_cache(monkeypatch):
    """同模型同 cache 的重播不是獨立佐證：第二次取樣必須 `cache_prompt=False`。"""
    spy = VLSpy({"figure_table": REGISTER_TABLE})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    extract([candidate()], {4: page_evidence()})

    assert [call["cache_prompt"] for call in spy.calls] == [True, False]


@pytest.mark.smoke
def test_retry_after_failure_also_disables_prompt_cache(monkeypatch):
    """重試也要關 cache，否則只是把同一份壞輸出再唸一次。"""
    good = REGISTER_TABLE
    spy = VLSpy({"figure_table": ["{oops", good, good]})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    extract([candidate()], {4: page_evidence()})

    assert spy.calls[0]["cache_prompt"] is True
    assert spy.calls[1]["cache_prompt"] is False


@pytest.mark.smoke
def test_probe_cache_stores_only_fingerprint_and_timestamp(monkeypatch, tmp_path):
    """快取檔只能有 fingerprint 與時戳：不存 prompt、模型輸出、base_url、專案路徑。"""
    cache = tmp_path / "probe.json"
    monkeypatch.setenv("AICODE_FIGURE_PROBE_FILE", str(cache))
    monkeypatch.setattr(figure_verify, "_render_canary_png", lambda spec: b"\x89PNG-canary")
    figure_verify._PROCESS_PROBE_PASSES.clear()

    widths = iter([3, 5])
    def respond(**kwargs):
        name = kwargs["response_format"]["json_schema"]["name"]
        if name == "figure_terminal":
            text = terminal_json([("$ echo hello", [])])
        else:
            n = next(widths)
            text = table_json([f"C{i}" for i in range(n)],
                              [[("x", "observed")] * n])
        return types.SimpleNamespace(text=text, finish_reason="stop", truncated=False,
                                     usage={}, raw={})

    install_vl(monkeypatch, respond)
    result = figure_verify.ensure_capability(
        base_url="http://127.0.0.1:8083", model="vl",
        kinds={figure_extract.KIND_TABLE, figure_extract.KIND_TERMINAL}, now=1000.0,
    )
    assert result.ok

    body = cache.read_text(encoding="utf-8")
    data = json.loads(body)
    entry = next(iter(data["passes"].values()))
    assert set(entry) == {"status", "checked_at", "probe_version"}
    for leak in ("逐字轉錄", "http://", "/models/", "figure_table", "columns", "echo hello",
                 str(REPO_ROOT)):
        assert leak not in body, f"快取洩漏了 {leak!r}"
    assert cache.stat().st_mode & 0o777 == 0o600


@pytest.mark.smoke
def test_failure_never_returns_partial_results(monkeypatch):
    """先成功一張、下一張失敗 → raise，且成功那張只出現在 `.results`。"""
    spy = VLSpy({"figure_table": [REGISTER_TABLE, REGISTER_TABLE, "{broken", "{broken"]})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    first = candidate(seed="ok", page=2, asset_digest="a")
    second = candidate(seed="bad", page=3, asset_digest="b", index=2)

    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        extract([first, second], {2: page_evidence(page=2), 3: page_evidence(page=3)})

    error = excinfo.value
    assert [r.figure_id for r in error.results] == [first.figure_id]
    assert error.failed.figure_id == second.figure_id
    assert error.failed.payload is None
    assert error.failed.extraction_status == figure_extract.EXTRACTION_FAILED
    assert error.failed.verification_status == figure_extract.VERIF_NEEDS_REVIEW
    assert error.failed.occurrences, "失敗 artifact 也要指得出頁碼與框"


@pytest.mark.smoke
def test_single_native_channel_is_not_self_corroborated():
    """canonical 通道不得當自己的 anchor：一個通道只能到 `unverified`。"""
    markdown = "| Name | Address |\n|---|---|\n| CTRL0 | 0x4000_0100 |\n"
    cand = candidate(native_table={
        "pos": (0, len(markdown)), "markdown": markdown, "strategy": "lines",
        "geometry": {"cells": [[(5, 5, 55, 25), (55, 5, 145, 25)],
                               [(5, 25, 55, 45), (55, 25, 145, 45)]],
                     "rows": [10.0, 30.0], "cols": [5, 55, 145]},
    })
    result = figure_verify.verify_native_table(
        cand, page_evidence(raw_markdown=markdown, words=[])
    )
    assert result.verification_status == figure_extract.VERIF_UNVERIFIED
    assert "single_channel_only" in result.reasons
    assert result.evidence["native"]["checks"]["second_channel"] is False
    assert result.evidence["anchor_coverage"]["atoms_anchorable"] == 0


# ============================================================
# native lane（零 VL）
# ============================================================
REGISTER_MD = (
    "| Name | Address | Bits | Mode | Description |\n"
    "|---|---|---|---|---|\n"
    "| CTRL0 | 0x4000_0100 | [7:4] | RW | clock select |\n"
    "| CTRL1 | 0x4000_0101 | [3:0] | RO | clock status |\n"
)
REGISTER_WORDS = words_from([
    (10.0, [(10, 60, "Name"), (70, 170, "Address"), (180, 220, "Bits"),
            (230, 270, "Mode"), (280, 380, "Description")]),
    (30.0, [(10, 60, "CTRL0"), (70, 170, "0x4000_0100"), (180, 220, "[7:4]"),
            (230, 270, "RW"), (280, 340, "clock"), (345, 380, "select")]),
    (50.0, [(10, 60, "CTRL1"), (70, 170, "0x4000_0101"), (180, 220, "[3:0]"),
            (230, 270, "RO"), (280, 340, "clock"), (345, 380, "status")]),
])
REGISTER_GEOMETRY = {
    "cells": [
        [(5, y - 5, 65, y + 15), (65, y - 5, 175, y + 15), (175, y - 5, 225, y + 15),
         (225, y - 5, 275, y + 15), (275, y - 5, 385, y + 15)]
        for y in (10.0, 30.0, 50.0)
    ],
    "rows": [10.0, 30.0, 50.0],
    "cols": [5, 65, 175, 225, 275, 385],
}


def register_candidate(**kwargs):
    native = {"pos": (0, len(REGISTER_MD)), "markdown": REGISTER_MD, "strategy": "lines",
              "geometry": REGISTER_GEOMETRY}
    native.update(kwargs.pop("native_extra", {}))
    kwargs.setdefault("bbox", (0.0, 0.0, 400.0, 80.0))
    return candidate(native_table=native, **kwargs)


def test_five_column_register_row_identity_is_preserved():
    """workflow §5 table ①：每一欄都必須綁在**同一列的身分**上，不是只有欄數對。"""
    result = figure_verify.verify_native_table(
        register_candidate(), page_evidence(raw_markdown=REGISTER_MD, words=REGISTER_WORDS)
    )
    assert result.verification_status == figure_extract.VERIF_NATIVE
    assert [c["label"] for c in result.payload["columns"]] == [
        "Name", "Address", "Bits", "Mode", "Description"
    ]
    grid = [[cell["text"] for cell in row["cells"]] for row in result.payload["rows"]]
    assert grid == [
        ["CTRL0", "0x4000_0100", "[7:4]", "RW", "clock select"],
        ["CTRL1", "0x4000_0101", "[3:0]", "RO", "clock status"],
    ]
    assert result.row_total == 2
    assert result.model_input_variant == "native"
    assert result.variants == []


def test_rows_differing_by_one_hex_char_never_cross_pair():
    """workflow §5 table ②：兩列只差一個 hex 字元，名稱與位址不得交叉配對。"""
    result = figure_verify.verify_native_table(
        register_candidate(), page_evidence(raw_markdown=REGISTER_MD, words=REGISTER_WORDS)
    )
    by_name = {
        row["cells"][0]["text"]: row["cells"][1]["text"] for row in result.payload["rows"]
    }
    assert by_name == {"CTRL0": "0x4000_0100", "CTRL1": "0x4000_0101"}


def test_native_multi_strategy_reaches_native_verified_with_zero_vl(monkeypatch):
    """三通道一致 → `native_verified`，而且完全沒有 VL 呼叫（契約 §12.1）。"""
    calls: list = []
    monkeypatch.setattr(
        figure_verify.llama_client, "vision_json_completion",
        lambda **kw: calls.append(kw), raising=False,
    )
    monkeypatch.setattr(
        figure_verify, "ensure_capability",
        lambda **kw: pytest.fail("native lane 不該做 capability probe"),
    )
    results = extract(
        [register_candidate()],
        {4: page_evidence(raw_markdown=REGISTER_MD, words=REGISTER_WORDS)},
        render=lambda _doc, _cand: pytest.fail("native lane 不該 render variant"),
    )
    assert calls == []
    assert results[0].verification_status == figure_extract.VERIF_NATIVE
    assert set(results[0].evidence["native"]["checks"]) == set(
        figure_verify.NATIVE_REQUIRED_CHECKS
    )
    assert all(results[0].evidence["native"]["checks"].values())


UNRELIABLE_MD = "| Name | Address |\n|---|---|\n| CTRL0 | 0x4000_0100 |\n"
UNRELIABLE_WORDS = words_from([(10.0, [(10, 60, "Name"), (70, 170, "Address")]),
                               (30.0, [(10, 60, "CTRL0"), (70, 170, "0x4000_0100")])])
UNRELIABLE_GEOMETRY = {"cells": [[(5, 5, 65, 25), (65, 5, 175, 25)],
                                 [(5, 25, 65, 45), (65, 25, 175, 45)]],
                       "rows": [10.0, 30.0], "cols": [5, 65, 175]}


def unreliable_tables(second_cell):
    """T3 的真實形狀：`extract()` 結果在 `geometry["extract_raw"]` 並標不可靠。"""
    return {"lines": [{
        "strategy": "lines", "ordinal": 0, "degenerate": False, "errors": [],
        "bbox": (0.0, 0.0, 200.0, 80.0),
        "geometry": {"table_bbox": (0.0, 0.0, 200.0, 80.0), "row_count": 2, "col_count": 2,
                     "cells": [], "rows": [], "cols": [],
                     "header_names": ["Name", "Address"],
                     "extract_raw": [["Name", "Address"], ["CTRL0", second_cell]],
                     "extract_unreliable_underscore": True},
    }]}


def unreliable_candidate():
    return candidate(native_table={"pos": (0, len(UNRELIABLE_MD)), "markdown": UNRELIABLE_MD,
                                   "strategy": "lines", "geometry": UNRELIABLE_GEOMETRY})


def test_known_extract_underscore_artifact_does_not_veto_the_cell():
    """實測：`Table.extract()` 把 `0x4000_0100` 讀成 `'0x4000 0100\n_'`。

    那是**有明確前提與範圍的已知缺陷**，不是內容不確定。讓它否決每一格 hex，
    等於把每一張暫存器表都打成待覆核，卻換不到任何安全性。差異照樣進 evidence。
    """
    result = figure_verify.verify_native_table(
        unreliable_candidate(),
        page_evidence(raw_markdown=UNRELIABLE_MD, words=UNRELIABLE_WORDS,
                      tables=unreliable_tables("0x4000 0100\n_")),
    )
    cell = result.payload["rows"][0]["cells"][1]
    assert cell["text"] == "0x4000_0100", "canonical 必須是頁面上真正的字"
    assert cell["state"] == figure_extract.CELL_STATE_OBSERVED
    assert result.verification_status == figure_extract.VERIF_NATIVE
    artifacts = result.evidence["cells"]["r1c2"]["artifacts"]
    assert artifacts[0]["reason"] == "extract_underscore_artifact"
    assert artifacts[0]["raw"] == "0x4000 0100\n_", "差異仍然完整揭露"
    assert "find_tables:lines_extract_unreliable" in result.reasons


def test_unreliable_channel_still_catches_a_real_value_difference():
    """中和的只有底線缺陷本身：值真的不同就照樣是衝突，正文放 `▯`。"""
    result = figure_verify.verify_native_table(
        unreliable_candidate(),
        page_evidence(raw_markdown=UNRELIABLE_MD, words=UNRELIABLE_WORDS,
                      tables=unreliable_tables("0x4000_0999")),
    )
    cell = result.payload["rows"][0]["cells"][1]
    assert figure_extract.UNREADABLE_GLYPH in cell["text"]
    assert cell["state"] == figure_extract.CELL_STATE_CONFLICT
    assert result.verification_status == figure_extract.VERIF_NEEDS_REVIEW


@pytest.mark.parametrize(
    "canonical, extracted, expected",
    [
        ("0x4000_0100", "0x4000 0100\n_", True),
        ("0x4000_0100", "0x40000100", True),
        ("0x4000_0100", "0x4000_0101", False),   # 值真的不同
        ("0x4000_0100", "0x4000_0100", False),   # 相同就不是 artifact
        ("CTRL0", "CTRL 0", False),              # 原文沒有底線 ⇒ 前提不成立
    ],
)
def test_underscore_artifact_detection_is_tightly_scoped(canonical, extracted, expected):
    assert figure_verify._underscore_artifact(canonical, extracted) is expected


def test_markdown_channel_markup_is_unwrapped():
    """pymupdf4llm 會把每個 text span 包在 backtick 裡、格內換行寫成 `<br>`。

    把 markup 當成內容，會讓 markdown 通道與 word geometry 通道**每一格**都衝突。
    """
    grid = figure_verify._parse_markdown_grid(
        "|`Name`|`Note`|\n|---|---|\n|`CTRL0`|`first`<br>`second`|\n"
    )
    assert grid["header"] == ["Name", "Note"]
    assert grid["rows"] == [["CTRL0", "first\nsecond"]]


def test_markdown_row_with_an_unescaped_pipe_is_marked_unrepresentable():
    """實測 pymupdf4llm **不會**跳脫 cell 內的 `|`，整列會往後位移。

    那種列一律標成「這個通道表達不出」，**不是**內容衝突——少一份佐證可以接受，
    拿位移過的內容當證據不行。
    """
    grid = figure_verify._parse_markdown_grid(
        "|`Name`|`Note`|\n|---|---|\n|`CTRL0`|`a|b`|\n|`CTRL1`|`ok`|\n"
    )
    assert grid["rows"][0] == [None, None], "位移的列不得被當成內容"
    assert grid["rows"][1] == ["CTRL1", "ok"]


def test_same_table_found_by_several_strategies_is_not_ambiguous():
    """三個 strategy 找到**同一張**表不算歧義；同頁的另一張表才算。"""
    entry = {"bbox": (0.0, 0.0, 200.0, 80.0), "geometry": {"extract_raw": [["A"], ["1"]]}}
    tables = {"lines": [entry],
              "lines_strict": [{**entry, "bbox": (0.5, 0.5, 200.5, 80.5)}],
              "text": [{**entry, "bbox": (0.0, 0.0, 200.0, 80.0)}]}
    match, problem = figure_verify._match_native_table_entry(
        page_evidence(tables=tables), (0.0, 0.0, 200.0, 80.0)
    )
    assert problem == "" and match is not None
    assert match[0] == "lines", "偏好順序＝ T3 的 _TABLE_STRATEGIES"


def test_geometry_proven_rowspan_fills_down_with_inherited_from_row():
    markdown = "| Group | Name |\n|---|---|\n| CLK | CTRL0 |\n|  | CTRL1 |\n"
    words = words_from([(10.0, [(10, 60, "Group"), (70, 170, "Name")]),
                        (30.0, [(10, 60, "CLK"), (70, 170, "CTRL0")]),
                        (50.0, [(70, 170, "CTRL1")])])
    span = (5, 25, 65, 65)
    geometry = {"cells": [[(5, 5, 65, 25), (65, 5, 175, 25)],
                          [span, (65, 25, 175, 45)],
                          [span, (65, 45, 175, 65)]],
                "rows": [10.0, 30.0, 50.0], "cols": [5, 65, 175]}
    cand = candidate(native_table={"pos": (0, len(markdown)), "markdown": markdown,
                                   "strategy": "lines", "geometry": geometry})
    result = figure_verify.verify_native_table(
        cand, page_evidence(raw_markdown=markdown, words=words)
    )
    second = result.payload["rows"][1]["cells"][0]
    assert second["text"] == "CLK"
    assert second["state"] == figure_extract.CELL_STATE_INHERITED
    assert second["inherited_from_row"] == 1
    assert "rowspan_filldown" in result.reasons


def test_intentional_blank_is_never_filled_down():
    """「保留 / 未實作 / 不適用」本來就該是空——沒有 geometry 證據就不准補。"""
    markdown = "| Group | Name |\n|---|---|\n| CLK | CTRL0 |\n|  | CTRL1 |\n"
    words = words_from([(10.0, [(10, 60, "Group"), (70, 170, "Name")]),
                        (30.0, [(10, 60, "CLK"), (70, 170, "CTRL0")]),
                        (50.0, [(70, 170, "CTRL1")])])
    geometry = {"cells": [[(5, 5, 65, 25), (65, 5, 175, 25)],
                          [(5, 25, 65, 45), (65, 25, 175, 45)],
                          [(5, 45, 65, 65), (65, 45, 175, 65)]],
                "rows": [10.0, 30.0, 50.0], "cols": [5, 65, 175]}
    cand = candidate(native_table={"pos": (0, len(markdown)), "markdown": markdown,
                                   "strategy": "lines", "geometry": geometry})
    result = figure_verify.verify_native_table(
        cand, page_evidence(raw_markdown=markdown, words=words)
    )
    second = result.payload["rows"][1]["cells"][0]
    assert second["text"] == ""
    assert second["state"] == figure_extract.CELL_STATE_OBSERVED
    assert second["inherited_from_row"] is None
    assert "rowspan_filldown" not in result.reasons


def test_identical_samples_alone_never_justify_filldown(monkeypatch):
    """審核 #37：兩次取樣一致**不是** rowspan 證據，本輪只認 geometry。"""
    blank_table = table_json(
        ["Group", "Name"],
        [[("CLK", "observed"), ("CTRL0", "observed")],
         [("", "observed"), ("CTRL1", "observed")]],
    )
    spy = VLSpy({"figure_table": blank_table})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    result = extract([candidate()], {4: page_evidence()})[0]

    second = result.payload["rows"][1]["cells"][0]
    assert second["text"] == ""
    assert second["state"] == figure_extract.CELL_STATE_OBSERVED
    assert second["inherited_from_row"] is None
    assert not any(cell["state"] == figure_extract.CELL_STATE_INHERITED
                   for row in result.payload["rows"] for cell in row["cells"])


def test_two_agreeing_channels_without_geometry_are_only_corroborated():
    markdown = "| Name | Address |\n|---|---|\n| CTRL0 | 0x4000_0100 |\n"
    words = words_from([(10.0, [(10, 60, "Name"), (70, 170, "Address")]),
                        (30.0, [(10, 60, "CTRL0"), (70, 170, "0x4000_0100")])])
    cand = candidate(native_table={"pos": (0, len(markdown)), "markdown": markdown,
                                   "strategy": "lines"})
    result = figure_verify.verify_native_table(
        cand, page_evidence(raw_markdown=markdown, words=words)
    )
    assert result.verification_status == figure_extract.VERIF_CORROBORATED
    assert result.evidence["native"]["checks"]["cell_geometry"] is False


def test_native_checks_key_set_is_fixed():
    """`all({})` 是 True——check 集合必須固定，缺 key 一律當 False。"""
    checks = figure_verify._native_checks(
        {"columns": [], "rows": []}, [], candidate(), {"blockers": []}
    )
    assert set(checks) == set(figure_verify.NATIVE_REQUIRED_CHECKS)
    assert checks["second_channel"] is False
    assert checks["cell_geometry"] is False


# ============================================================
# anchoring 的幾何前提
# ============================================================
def test_boundary_word_is_kept_not_clipped():
    """`get_text(clip=...)` 會先丟掉邊界字，事後救不回——所以整頁取一次再分派。"""
    box = (0.0, 0.0, 100.0, 50.0)
    inside = (10.0, 10.0, 40.0, 20.0, "INSIDE", 0, 0, 0)
    straddling = (90.0, 10.0, 130.0, 20.0, "EDGE", 0, 0, 1)   # 中心點 110 在框外
    centered_edge = (80.0, 10.0, 110.0, 20.0, "KEPT", 0, 0, 2)  # 中心點 95 在框內
    kept = figure_verify._words_for_candidate([inside, straddling, centered_edge], box)
    texts = [w[4] for w in kept]
    assert "INSIDE" in texts
    assert "KEPT" in texts, "中心點在框內的邊界字不得被丟掉"
    assert "EDGE" not in texts


def test_row_grouping_ignores_block_numbers():
    """實測：同一個視覺列可能落在不同 `block_no`，分群只能靠 y 幾何重疊。"""
    same_row = words_from([(10.0, [(10, 40, "A", 0), (50, 80, "B", 7)])])
    rows = figure_verify._group_words_into_rows(same_row)
    assert len(rows) == 1 and [w[4] for w in rows[0]] == ["A", "B"]

    same_block_two_rows = words_from([(10.0, [(10, 40, "A", 3)]), (40.0, [(10, 40, "B", 3)])])
    rows = figure_verify._group_words_into_rows(same_block_two_rows)
    assert len(rows) == 2


def test_zero_height_word_is_skipped():
    broken = (10.0, 20.0, 40.0, 20.0, "GHOST", 0, 0, 0)
    good = (10.0, 30.0, 40.0, 40.0, "REAL", 0, 0, 1)
    rows = figure_verify._group_words_into_rows([broken, good])
    assert [w[4] for row in rows for w in row] == ["REAL"]


@pytest.mark.parametrize(
    "pos, expected",
    [
        ((0, 4), ""),                 # 合法
        ((4, 0), "pos_out_of_range"),
        ((-1, 4), "pos_out_of_range"),
        ((0, 999), "pos_out_of_range"),
        ((2, 2), "pos_empty"),
        (5, "pos_shape"),
        ((0, 1, 2), "pos_shape"),
        (("0", "4"), "pos_not_int"),
        ((True, 4), "pos_not_int"),
    ],
)
def test_pos_slice_is_validated(pos, expected):
    """`raw[pos]` 在 Python 直接 TypeError；越界的 offset 會切到**另一段內容**。"""
    text, problem = figure_verify._pos_slice("abcd", pos)
    assert problem == expected
    assert (text is not None) == (expected == "")


def test_ambiguous_table_entry_on_the_same_page_is_refused():
    """同頁兩張表靠得很近時，寧可判為對不上，也不要拿 B 表當 A 表的佐證。"""
    # 兩張**互不重疊**的表，對候選框的 IoU 又差不多 → 分不出是哪一張
    tables = {"lines": [
        {"bbox": (0.0, 0.0, 200.0, 80.0), "geometry": {"extract_raw": [["A"], ["1"]]}},
        {"bbox": (0.0, 70.0, 200.0, 150.0), "geometry": {"extract_raw": [["B"], ["2"]]}},
    ]}
    match, problem = figure_verify._match_native_table_entry(
        page_evidence(tables=tables), (0.0, 0.0, 200.0, 150.0)
    )
    assert match is None
    assert problem == "ambiguous_table_match"


def test_degenerate_table_entry_never_becomes_a_channel():
    tables = {"lines": [{"bbox": (0.0, 0.0, 200.0, 80.0), "degenerate": True,
                         "geometry": {"extract_raw": [["A"], ["1"]]}}]}
    match, problem = figure_verify._match_native_table_entry(
        page_evidence(tables=tables), (0.0, 0.0, 200.0, 80.0)
    )
    assert match is None and problem == "no_tables_channel"


# ============================================================
# terminal / log
# ============================================================
TERMINAL_RAW = "$ dmesg | tail\n\n[    0.000000] Booting\n\n"


def terminal_candidate(*, native_text=None, **kwargs):
    """terminal 候選。`signals["native_text"]` 是 T3 給的「pos 支撐的 raw markdown」。"""
    signals = dict(kwargs.pop("signals", None) or {})
    if native_text is not None:
        signals["native_text"] = native_text
    return candidate(kind=figure_extract.KIND_TERMINAL, signals=signals, **kwargs)


def terminal_evidence(words=None):
    return page_evidence(
        raw_markdown=TERMINAL_RAW,
        page_boxes=[{"index": 0, "class": "text", "bbox": (0.0, 0.0, 200.0, 80.0),
                     "pos": (0, len(TERMINAL_RAW))}],
        words=words if words is not None else words_from([
            (10.0, [(10, 30, "$"), (35, 90, "dmesg"), (95, 105, "|"), (110, 150, "tail")]),
            (50.0, [(10, 90, "[    0.000000]"), (95, 160, "Booting")]),
        ]),
    )


def test_terminal_primary_anchor_preserves_empty_lines():
    """空行（首/中/末）是 canonical line；word baseline 產生不出它們。"""
    result = figure_verify._verify_native_terminal(terminal_candidate(), terminal_evidence())
    assert [line["text"] for line in result.payload["lines"]] == [
        "$ dmesg | tail", "", "[    0.000000] Booting", "", ""
    ]
    assert result.line_total == 5
    # 空行沒有佐證 ⇒ 擋得住 corroborated，但不該被打成 needs_review
    assert result.verification_status == figure_extract.VERIF_UNVERIFIED
    assert "partial_anchor_coverage" in result.reasons
    coverage = result.evidence["anchor_coverage"]
    assert coverage["atoms_total"] == 5
    assert coverage["atoms_anchorable"] < coverage["atoms_total"]


def test_native_terminal_requires_pos_backed_markdown():
    """word geometry 單獨**不足以**構成 terminal 的 native lane（契約 §15.1）。

    `" ".join(...)` 合成的正文會把行首縮排、行尾空白與無字空行全部抹平，卻仍被
    存成 canonical——那是猜出來的普通空格（總審 BLOCKER #5）。這種候選走 VL lane。
    """
    cand = terminal_candidate()
    evidence = page_evidence(
        raw_markdown="", page_boxes=[],
        words=words_from([(10.0, [(10, 30, "$"), (35, 90, "ls")])]),
    )
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        figure_verify._verify_native_terminal(cand, evidence)
    message = str(excinfo.value)
    assert "word geometry 單獨不足以構成 terminal 的 native lane" in message
    assert "縮排" in message


def test_terminal_line_alignment_failure_keeps_both_raw_candidates():
    words = words_from([(10.0, [(10, 60, "totally")]), (30.0, [(10, 60, "different")]),
                        (50.0, [(10, 60, "content")]), (70.0, [(10, 60, "again")])])
    result = figure_verify._verify_native_terminal(
        terminal_candidate(), terminal_evidence(words=words)
    )
    assert result.verification_status == figure_extract.VERIF_NEEDS_REVIEW
    assert "line_alignment_failed" in result.reasons
    detail = result.evidence["line_alignment"]["words_geometry"]
    assert detail["anchor_raw"] and detail["payload_raw"], "兩份 raw candidate 都要留著"


def test_terminal_line_contract_is_enforced_on_the_model(monkeypatch):
    """一個 line 必須恰是一個視覺行——自己拆行等於把行序交給模型自由裁量。"""
    bad = json.dumps({"lines": [{"text": "first\nsecond", "uncertain_spans": []}]})
    spy = VLSpy({"figure_terminal": bad})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        extract([terminal_candidate()], {4: page_evidence()})
    assert "line_contract" in str(excinfo.value)


def test_unlocatable_tokens_only_reach_the_sidecar():
    """對不到 cell 的 token 只寫 evidence 與 reason，**禁止**補成「補遺區段」。"""
    markdown = "| Name | Addr |\n|---|---|\n| GPIO7 | 0xDEAD_BEEF |\n"
    cand = candidate(native_table={"pos": (0, len(markdown)), "markdown": markdown,
                                   "strategy": "lines"})
    payload = {
        "kind": figure_extract.KIND_TABLE,
        "columns": [{"column_id": "c1", "label": "X", "role": None}],
        "rows": [{"row_index": 1, "cells": [
            {"column_id": "c1", "text": "完全無關", "state": "observed",
             "inherited_from_row": None}]}],
        "footnotes": [],
    }
    alignment = figure_verify.align_table_cells(
        payload, cand, page_evidence(raw_markdown=markdown)
    )
    assert "0xDEAD_BEEF" in alignment["unlocatable_tokens"]
    slugs = {slug for slug, _detail in alignment["blockers"]}
    assert "unlocated_critical_token" in slugs
    # payload 完全沒被動過：沒有補遺、沒有旁註
    blob = json.dumps(payload, ensure_ascii=False)
    assert "0xDEAD_BEEF" not in blob
    assert "補遺" not in blob and "未經文字層佐證" not in blob


# ============================================================
# VL lane：dual pass、重複影像、預算、tile
# ============================================================
LOG_LINES = [
    ("$ dmesg | tail -3", []),
    ("[    0.000000] Linux version 6.1", []),
    ("[    0.120000] Booting the kernel", []),
    ("", []),
]
GOOD_TERMINAL = terminal_json(LOG_LINES)
# kind 誤判時 table extractor 仍會吐出**非空**的 payload——非空從來不代表對
WRONG_KIND_TABLE = table_json(
    ["output"],
    [[(text, "observed")] for text, _ in LOG_LINES if text],
)


def test_kind_unknown_runs_each_extractor_exactly_once(monkeypatch):
    """契約 §12.2：ambiguous dual pass 每個 kind **恰好一次**，勝出的也不再打第二次。"""
    spy = VLSpy({"figure_table": WRONG_KIND_TABLE, "figure_terminal": GOOD_TERMINAL})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    result = extract([candidate(kind=figure_extract.KIND_UNKNOWN)], {4: page_evidence()})[0]

    names = spy.schema_names()
    assert names.count("figure_table") == 1
    assert names.count("figure_terminal") == 1
    assert len(names) == 2, "勝出的 kind 不得再被取樣一次"
    assert result.kind == figure_extract.KIND_TERMINAL
    assert "kind_ambiguous_resolved" in result.reasons
    ambiguous = result.evidence["ambiguous"]
    assert set(ambiguous["payloads"]) == {figure_extract.KIND_TABLE,
                                          figure_extract.KIND_TERMINAL}
    assert ambiguous["scores"][figure_extract.KIND_TERMINAL] > \
        ambiguous["scores"][figure_extract.KIND_TABLE]


def test_kind_unknown_does_not_stop_at_the_first_non_empty_payload(monkeypatch):
    """「第一份 payload 非空就停」會讓誤判的結構直接入庫。"""
    spy = VLSpy({"figure_table": WRONG_KIND_TABLE, "figure_terminal": GOOD_TERMINAL})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)
    extract([candidate(kind=figure_extract.KIND_UNKNOWN)], {4: page_evidence()})
    assert set(spy.schema_names()) == {"figure_table", "figure_terminal"}


def test_kind_unknown_single_sided_failure_is_evidence_not_fatal(monkeypatch):
    spy = VLSpy({"figure_table": "{broken", "figure_terminal": GOOD_TERMINAL})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)
    result = extract([candidate(kind=figure_extract.KIND_UNKNOWN)], {4: page_evidence()})[0]
    assert result.kind == figure_extract.KIND_TERMINAL
    assert result.evidence["ambiguous"]["errors"], "單邊失敗要留在 evidence 裡"


def test_kind_unknown_both_sides_failing_is_fatal(monkeypatch):
    spy = VLSpy({"figure_table": "{broken", "figure_terminal": "{broken"})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        extract([candidate(kind=figure_extract.KIND_UNKNOWN)], {4: page_evidence()})
    assert "dual pass 兩邊都失敗" in str(excinfo.value)


def test_kind_unknown_close_scores_become_needs_review(monkeypatch):
    """分數差 < FIGURE_KIND_MARGIN → kind 未解歧 → needs_review。"""
    monkeypatch.setattr(config, "FIGURE_KIND_MARGIN", 0.99)
    spy = VLSpy({"figure_table": WRONG_KIND_TABLE, "figure_terminal": GOOD_TERMINAL})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)
    result = extract([candidate(kind=figure_extract.KIND_UNKNOWN)], {4: page_evidence()})[0]
    assert "kind_ambiguous" in result.reasons
    assert result.verification_status == figure_extract.VERIF_NEEDS_REVIEW


def test_two_identical_samples_without_anchor_stay_unverified(monkeypatch):
    """契約 §3：無 anchor 的同模型多次取樣即使全等也只到 `unverified`。"""
    spy = VLSpy({"figure_table": REGISTER_TABLE})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    result = extract([candidate()], {4: page_evidence()})[0]

    assert result.verification_status == figure_extract.VERIF_UNVERIFIED
    assert result.verification_status != figure_extract.VERIF_CORROBORATED
    assert "two_samples_agree" in result.reasons
    assert "no_anchor_evidence" in result.reasons
    assert result.evidence["repeatability"]["identical"] is True
    assert result.evidence["repeatability"]["second_cache_prompt"] is False


def test_sample_shape_mismatch_is_fatal(monkeypatch):
    """兩次取樣的欄數不同 → 沒有安全的 canonical 結構，沿用 A 就是替使用者擇一。"""
    other = table_json(["Name", "Address"], [[("CTRL0", "observed"), ("0x1", "observed")]])
    spy = VLSpy({"figure_table": [REGISTER_TABLE, other]})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        extract([candidate()], {4: page_evidence()})
    assert "sample_shape_mismatch" in str(excinfo.value)


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda p: p["columns"][0].update({"label": "Renamed"}), "sample_header_conflict"),
        (lambda p: p["footnotes"].append("note"), "sample_footnote_conflict"),
        (lambda p: p["rows"][0]["cells"][0].update({"state": "unreadable"}),
         "sample_state_conflict"),
    ],
)
def test_detect_disagreement_compares_the_whole_canonical_payload(mutate, reason):
    """只比欄數與 cell 文字，會把「其實不同的兩次輸出」判成一致。"""
    base = figure_extract.canonicalize_table(json.loads(REGISTER_TABLE))
    other = figure_extract.canonicalize_table(json.loads(REGISTER_TABLE))
    mutate(other)
    evidence, reasons = figure_verify.detect_disagreement(
        base, other, figure_extract.KIND_TABLE
    )
    assert evidence["agreement"] is False
    assert reason in reasons


def test_detect_disagreement_notices_terminal_span_differences():
    a = figure_extract.canonicalize_terminal(
        json.loads(terminal_json([("ab▯", [{"start": 2, "end": 3, "alternatives": ["c"]}])]))
    )
    b = figure_extract.canonicalize_terminal(
        json.loads(terminal_json([("ab▯", [{"start": 2, "end": 3, "alternatives": ["d"]}])]))
    )
    evidence, reasons = figure_verify.detect_disagreement(a, b, figure_extract.KIND_TERMINAL)
    assert evidence["agreement"] is False
    assert "sample_span_conflict" in reasons


def test_duplicate_asset_reuses_the_payload_and_keeps_all_occurrences(monkeypatch):
    """重複影像只省 VL 計算：兩個 occurrence 的身分、頁碼、框一個都不能少。"""
    spy = VLSpy({"figure_table": REGISTER_TABLE})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    first = candidate(seed="dup-a", page=2, asset_digest="same", index=1)
    second = candidate(seed="dup-b", page=7, asset_digest="same", index=2)
    results = extract([first, second], {2: page_evidence(page=2), 7: page_evidence(page=7)})

    assert len(spy.calls) == 2, "第一張抽一次 + 一次重複性取樣；第二張沿用"
    assert [r.figure_id for r in results] == [first.figure_id, second.figure_id]
    assert [r.page for r in results] == [2, 7]
    assert "duplicate_asset_reused" in results[1].reasons
    assert results[1].evidence["duplicate_of"] == first.figure_id
    assert results[0].payload["rows"][0]["cells"][1]["text"] == "0x8000_0100"
    assert results[0].payload is not results[1].payload


def test_different_assets_are_not_deduplicated(monkeypatch):
    spy = VLSpy({"figure_table": REGISTER_TABLE})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)
    first = candidate(seed="a", page=2, asset_digest="a1", index=1)
    second = candidate(seed="b", page=7, asset_digest="b1", index=2)
    extract([first, second], {2: page_evidence(page=2), 7: page_evidence(page=7)})
    assert len(spy.calls) == 4


def test_duplicate_occurrence_does_not_mutate_the_frozen_first_result(monkeypatch):
    """asset cache 存的必須是 pristine 深拷貝，否則第二頁的遮罩會回頭改第一張。"""
    spy = VLSpy({"figure_table": REGISTER_TABLE})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)
    first = candidate(seed="dup-a", page=2, asset_digest="same", index=1)
    second = candidate(seed="dup-b", page=7, asset_digest="same", index=2)
    results = extract([first, second], {2: page_evidence(page=2), 7: page_evidence(page=7)})
    results[1].payload["rows"][0]["cells"][0]["text"] = "TAMPERED"
    assert results[0].payload["rows"][0]["cells"][0]["text"] == "CTRL0"


@pytest.mark.parametrize(
    "tokens_per_call, expect",
    [(config.FIGURE_MAX_IMAGE_TOKENS_PER_CALL + 1, True), (10, False)],
)
def test_image_token_budget_is_rechecked_before_send(monkeypatch, tokens_per_call, expect):
    """preflight 算的是計畫值；送出前這一道擋的是「計畫與實際 render 不一致」。"""
    spy = VLSpy({"figure_table": REGISTER_TABLE})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)
    def render(_doc, cand):
        return [variant(cand.figure_id, est_image_tokens=tokens_per_call)]

    if expect:
        with pytest.raises(figure_extract.FigureBudgetError) as excinfo:
            extract([candidate()], {4: page_evidence()}, render=render)
        assert "送出前複查" in str(excinfo.value)
        assert spy.calls == [], "超過預算就不該送出去"
    else:
        extract([candidate()], {4: page_evidence()}, render=render)


@pytest.mark.parametrize(
    "variants_for, fragment",
    [
        (lambda fid: [variant(fid, tile_index=1, tile_total=3),
                      variant(fid, tile_index=3, tile_total=3)], "tile 不連續"),
        (lambda fid: [variant(fid, tile_index=1, tile_total=2),
                      variant(fid, tile_index=1, tile_total=2)], "重號"),
        (lambda fid: [variant(fid, tile_index=1, tile_total=2),
                      variant(fid, tile_index=2, tile_total=3)], "tile_total"),
        (lambda fid: [variant("fig_" + "0" * 16)], "收到屬於"),
        (lambda fid: [variant(fid, png=b"")], "Variant.png 是空的"),
        (lambda fid: [], "沒有任何可送模型的 variant"),
    ],
)
def test_variant_identity_is_validated_before_send(monkeypatch, variants_for, fragment):
    spy = VLSpy({"figure_table": REGISTER_TABLE})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        extract([candidate()], {4: page_evidence()},
                render=lambda _doc, cand: variants_for(cand.figure_id))
    assert fragment in str(excinfo.value)
    assert spy.calls == []


# `_validate_variants()` 是**任何 VL 呼叫之前**的那道閘。它本地的 `int()` 正規化
# 把 `tile_total=True` / `"1"` / `1.9` 截成合法的 `(1, 0)`、把字串 `est_image_tokens`
# 轉成正整數，於是這些 variant 一路送進模型；等 RAG / writer 稍後拒絕時，VL 的錢
# 已經花掉了。修正＝改呼叫門面唯一的 `figure_extract.validate_variant()`。
_COERCIBLE_VARIANT_FIELDS = [
    ({"tile_total": True}, "Variant.tile_total=True"),
    ({"tile_total": "1"}, "Variant.tile_total='1'"),
    ({"tile_index": "0"}, "Variant.tile_index='0'"),
    ({"tile_total": 1.9, "tile_index": 0.9}, "Variant.tile_total=1.9"),
    ({"est_image_tokens": "120"}, "Variant.est_image_tokens='120'"),
    ({"est_image_tokens": 120.5}, "Variant.est_image_tokens=120.5"),
    ({"overlap_px": True}, "Variant.overlap_px=True"),
    ({"width": "400"}, "Variant.width='400'"),
    ({"height": 200.0}, "Variant.height=200.0"),
    ({"digest": "d"}, "Variant.digest 與 png 內容不符"),
    ({"bbox": (0.0, 0.0, "200", 80.0)}, "Variant.bbox[2]='200'"),
    ({"mime": ""}, "Variant.mime 不得為空字串"),
]


@pytest.mark.smoke
@pytest.mark.parametrize(
    "overrides, marker",
    _COERCIBLE_VARIANT_FIELDS,
    ids=[next(iter(o)) + "=" + repr(next(iter(o.values())))
         for o, _ in _COERCIBLE_VARIANT_FIELDS],
)
def test_validate_variants_refuses_every_coercible_field(overrides, marker):
    """`_validate_variants()` 必須用門面的共享 validator，不得自己 `int()`。

    每一條都是實測會靜默通過的形狀：`int(True or 0)` → 1、`int("1")` → 1、
    `int(1.9)` → 1、`int("120")` → 120，而 `digest` / `bbox` / `mime` / `width`
    在這道閘裡根本沒被看過。訊息斷言用 `Variant.<欄位>=<值>` 這種結構化 token，
    不是裸關鍵字——肯定句式的拒絕訊息才綁得住哪個欄位真的被驗了。
    """
    cand = candidate()
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        figure_verify._validate_variants(cand, [variant(cand.figure_id, **overrides)])
    assert marker in str(excinfo.value)


@pytest.mark.smoke
@pytest.mark.parametrize(
    "overrides",
    [{"tile_total": True}, {"tile_total": "1", "tile_index": "0"},
     {"tile_total": 1.9, "tile_index": 0.9}, {"est_image_tokens": "120"},
     {"digest": "d"}],
    ids=["tile_total_bool", "tile_total_str", "tile_total_float",
         "est_image_tokens_str", "digest_mismatch"],
)
def test_coercible_variant_never_reaches_the_vl_call(monkeypatch, overrides):
    """被轉型救回來的 variant 不得抵達模型——錢在拒絕之前就花掉了。"""
    spy = VLSpy({"figure_table": REGISTER_TABLE})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)
    with pytest.raises(figure_extract.FigureExtractionError):
        extract([candidate()], {4: page_evidence()},
                render=lambda _doc, cand: [variant(cand.figure_id, **overrides)])
    assert spy.calls == [], "VL 已經被呼叫過了：這道閘必須在送出前就擋下來"


# ★ 契約 §21.10：`tile_total == 1` 的 variant 必須**真的是整個候選框**。
# `validate_variant()` 依 §21.1 只看單一 variant 自身的形狀，它不知道候選框長怎樣；
# bbox 比對只存在於 `is_full_image()`，而 `_validate_variants()` 從來沒呼叫過它。
# 於是「把第一片 tile 的 bytes 配上合法 flags（tile_index=0 / tile_total=1）」的局部
# crop 會在**任何 VL 呼叫之前**通關，等 RAG / writer 之後才拒——錢已經花掉了。
# `render_candidate_variants()` 的未切片分支 render 的就是整個候選框，所以
# `tile_total == 1` ⟹ `bbox == candidate.bbox` 是產生端的既有事實，違反即 producer 漂移。
_LOCAL_CROP_BBOXES = [
    ((0.0, 0.0, 200.0, 40.0), "top_half"),
    ((0.0, 40.0, 200.0, 80.0), "bottom_half"),
    ((0.0, 0.0, 100.0, 80.0), "left_half"),
    ((10.0, 10.0, 190.0, 70.0), "inset"),
    ((0.0, 0.0, 200.0, 80.001), "one_thousandth_taller"),
]


@pytest.mark.smoke
@pytest.mark.parametrize("crop_bbox, label", _LOCAL_CROP_BBOXES,
                         ids=[label for _, label in _LOCAL_CROP_BBOXES])
def test_local_crop_posing_as_the_full_image_is_refused_before_send(crop_bbox, label):
    """局部 crop 宣稱 `tile_total=1` 不得通過送出前那道閘（契約 §21.10）。

    斷言用「不是完整候選框」這個肯定句式的拒絕理由 ＋ 兩個 bbox 的實際數值，
    不用裸關鍵字——`bbox` / `tile_total` 這種字在正反兩種文案都會出現。
    """
    cand = candidate()
    assert tuple(cand.bbox) != tuple(crop_bbox), "測試前提：crop 必須真的小於候選框"
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        figure_verify._validate_variants(
            cand, [variant(cand.figure_id, bbox=crop_bbox)])
    message = str(excinfo.value)
    assert "不是完整候選框" in message
    assert repr(list(crop_bbox)) in message or repr(tuple(crop_bbox)) in message


@pytest.mark.smoke
def test_local_crop_posing_as_the_full_image_never_reaches_the_vl_call(monkeypatch):
    """冒充者必須在送模之前就被擋下——之後才拒的話，VL 的錢已經花掉了。"""
    spy = VLSpy({"figure_table": REGISTER_TABLE})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)
    with pytest.raises(figure_extract.FigureExtractionError):
        extract([candidate()], {4: page_evidence()},
                render=lambda _doc, cand: [
                    variant(cand.figure_id, bbox=(0.0, 0.0, 200.0, 40.0))])
    assert spy.calls == [], "VL 已經被呼叫過了：這道閘必須在送出前就擋下來"


@pytest.mark.smoke
def test_tiled_variant_bboxes_are_deliberately_not_checked_against_the_candidate():
    """契約 §21.10 的**範圍界線**：tiled variant 的每片 bbox 刻意不比對候選框。

    tile 不會被任何一端當成完整原圖（`is_full_image()` 對 `tile_total != 1` 一律回
    `False`），冒充路徑不存在。這條把「刻意不收」釘住：日後有人順手把 bbox 檢查
    擴到 tile 上，這裡會轉紅，逼他先回去看契約而不是直接改。
    """
    cand = candidate()
    tiles = [variant(cand.figure_id, tile_index=1, tile_total=2,
                     bbox=(0.0, 0.0, 200.0, 40.0)),
             variant(cand.figure_id, tile_index=2, tile_total=2, overlap_px=8,
                     bbox=(0.0, 40.0, 200.0, 80.0))]
    accepted = figure_verify._validate_variants(cand, tiles)
    assert [v.tile_index for v in accepted] == [1, 2]


def test_raster_variant_mime_is_forwarded(monkeypatch):
    """契約 §13.2：`variant_id == "raster"` 的 bytes 可能不是 PNG，真實型別看 `mime`。"""
    spy = VLSpy({"figure_table": REGISTER_TABLE})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)
    extract([candidate()], {4: page_evidence()},
            render=lambda _doc, cand: [variant(cand.figure_id, variant_id="raster",
                                               mime="image/jpeg", png=b"\xff\xd8jpeg")])
    assert all(call["mime_type"] == "image/jpeg" for call in spy.calls)


# ============================================================
# tile 接合
# ============================================================
def tile_pair(rows_a, rows_b, *, columns=("Name", "Address")):
    return [table_json(list(columns), rows_a), table_json(list(columns), rows_b)]


def tiled_render(figure_id, *, overlap_bands=(0,), overlap_px=48, stitch_second=None):
    """兩張 tile。`stitch` 用 T3 實際產出的形狀（band 幾何），不是虛構的鍵。

    `overlap_band_indices_top` = 這張 tile 頂端有哪幾個 band 與上一張重疊——
    那才是「這個邊界最多可以去重幾個原子」的事實。
    """
    second = stitch_second if stitch_second is not None else {
        "tile_index": 2, "tile_total": 2, "band_range": [1, 3],
        "overlap_band_indices_top": list(overlap_bands),
        "overlap_band_indices_bottom": [], "overlap_px": overlap_px,
        "overlap_top_pt": 12.0, "overlap_bottom_pt": 0.0,
        "cut_y_top": 40.0, "cut_y_bottom": None,
    }
    return [variant(figure_id, tile_index=1, tile_total=2, overlap_px=0,
                    stitch={"tile_index": 1, "tile_total": 2, "band_range": [0, 1],
                            "overlap_band_indices_top": [],
                            "overlap_band_indices_bottom": list(overlap_bands),
                            "overlap_px": 0, "cut_y_top": None, "cut_y_bottom": 40.0}),
            variant(figure_id, tile_index=2, tile_total=2, overlap_px=overlap_px,
                    stitch=second)]


def test_tile_overlap_is_deduplicated_and_reindexed(monkeypatch):
    shared = [("R2", "observed"), ("0x2", "observed")]
    first = [[("R1", "observed"), ("0x1", "observed")], shared]
    second = [shared, [("R3", "observed"), ("0x3", "observed")]]
    texts = tile_pair(first, second)
    spy = VLSpy({"figure_table": texts + texts})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    result = extract([candidate()], {4: page_evidence()},
                     render=lambda _doc, cand: tiled_render(cand.figure_id))[0]

    names = [[cell["text"] for cell in row["cells"]] for row in result.payload["rows"]]
    assert names == [["R1", "0x1"], ["R2", "0x2"], ["R3", "0x3"]]
    assert [row["row_index"] for row in result.payload["rows"]] == [1, 2, 3]
    assert result.row_total == 3
    assert result.evidence["stitch"]["uncertain"] is False
    assert result.evidence["stitch"]["overlap_matched"] == [1]


def test_tile_without_overlap_is_marked_uncertain_not_silently_merged(monkeypatch):
    first = [[("R1", "observed"), ("0x1", "observed")]]
    second = [[("R9", "observed"), ("0x9", "observed")]]
    texts = tile_pair(first, second)
    spy = VLSpy({"figure_table": texts + texts})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    result = extract([candidate()], {4: page_evidence()},
                     render=lambda _doc, cand: tiled_render(cand.figure_id))[0]

    assert result.evidence["stitch"]["uncertain"] is True
    assert "stitch_uncertain" in result.reasons
    assert result.verification_status == figure_extract.VERIF_NEEDS_REVIEW
    assert len(result.payload["rows"]) == 2, "不去重直接接，重複留在原地待審"


def test_legitimately_repeated_log_lines_are_not_eaten_by_stitching(monkeypatch):
    """log 本來就會有合法的重複行與空行——overlap 比對必須 byte-exact。"""
    first = terminal_json([("retry", []), ("retry", []), ("", [])])
    second = terminal_json([("done", [])])
    texts = [first, second]
    spy = VLSpy({"figure_terminal": texts + texts})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    result = extract([candidate(kind=figure_extract.KIND_TERMINAL)], {4: page_evidence()},
                     render=lambda _doc, cand: tiled_render(cand.figure_id))[0]

    assert [line["text"] for line in result.payload["lines"]] == [
        "retry", "retry", "", "done"
    ]
    assert [line["line_index"] for line in result.payload["lines"]] == [1, 2, 3, 4]


def test_tile_column_mismatch_is_fatal(monkeypatch):
    texts = [table_json(["Name", "Address"], [[("R1", "observed"), ("0x1", "observed")]]),
             table_json(["Name", "Value"], [[("R2", "observed"), ("0x2", "observed")]])]
    spy = VLSpy({"figure_table": texts + texts})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        extract([candidate()], {4: page_evidence()},
                render=lambda _doc, cand: tiled_render(cand.figure_id))
    assert "欄位標題不一致" in str(excinfo.value)


# ============================================================
# capability probe
# ============================================================
def probe_responder(table_widths, *, terminal=None, truncate=(), fail=()):
    widths = list(table_widths)
    state = {"n": 0}

    def respond(**kwargs):
        name = kwargs["response_format"]["json_schema"]["name"]
        if name in fail:
            raise RuntimeError("boom")
        if name == "figure_terminal":
            text = terminal if terminal is not None else terminal_json([("$ echo hello", [])])
        else:
            index = min(state["n"], len(widths) - 1)
            state["n"] += 1
            n = widths[index]
            text = table_json([f"C{i}" for i in range(n)], [[("x", "observed")] * n])
        finish = "length" if name in truncate else "stop"
        return types.SimpleNamespace(text=text, finish_reason=finish,
                                     truncated=finish not in ("stop", "eos"),
                                     usage={}, raw={})
    return respond


@pytest.fixture
def probe_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AICODE_FIGURE_PROBE_FILE", str(tmp_path / "probe.json"))
    monkeypatch.setattr(figure_verify, "_render_canary_png", lambda spec: b"\x89PNG-canary")
    figure_verify._PROCESS_PROBE_PASSES.clear()
    return tmp_path / "probe.json"


def test_probe_uses_the_column_pair_to_prove_the_image_reached_the_model(probe_env, monkeypatch):
    """同一個 prompt 配兩張欄數不同的圖，輸出欄數必須不同。"""
    calls: list = []

    def respond(**kwargs):
        calls.append(kwargs)
        return probe_responder([3, 3])(**kwargs)

    install_vl(monkeypatch, respond)
    with pytest.raises(figure_extract.FigureCapabilityError) as excinfo:
        figure_verify.ensure_capability(base_url="http://x", model="vl",
                                        kinds={figure_extract.KIND_TABLE}, now=1.0)
    message = str(excinfo.value)
    assert "image_changes_output" in message
    assert "靜默忽略 image content part" in message
    assert len(calls) == 2, "image-dependence probe 就是那兩張 3 欄 / 5 欄的表"
    assert not probe_env.exists()


def test_probe_passes_when_the_column_pair_differs(probe_env, monkeypatch):
    install_vl(monkeypatch, probe_responder([3, 5]))
    result = figure_verify.ensure_capability(
        base_url="http://x", model="vl", kinds={figure_extract.KIND_TABLE}, now=1.0
    )
    assert result.ok and result.checks["image_changes_output"] is True


def test_probe_lists_every_missing_capability(probe_env, monkeypatch):
    install_vl(monkeypatch, probe_responder([3, 5], truncate={"figure_terminal"}))
    with pytest.raises(figure_extract.FigureCapabilityError) as excinfo:
        figure_verify.ensure_capability(
            base_url="http://x", model="vl",
            kinds={figure_extract.KIND_TABLE, figure_extract.KIND_TERMINAL}, now=1.0,
        )
    error = excinfo.value
    assert error.probe.missing == [
        "terminal.response_not_truncated", "terminal.json_parsable",
        "terminal.required_fields_present", "terminal.canonicalizable",
        "terminal.validator_pass",
    ]
    assert "AICODE_VL_INGEST_MAX_TOKENS" in str(error)


def test_probe_separates_image_rejection_from_schema_rejection(probe_env, monkeypatch):
    """一個 400 分不出「不吃圖」與「不吃 json_schema」，處置卻完全不同。"""
    install_vl(monkeypatch, probe_responder([3, 5], fail={"figure_table"}))
    # 不帶 schema 的重試成功 ⇒ image 是被接受的，問題出在 json_schema
    with pytest.raises(figure_extract.FigureCapabilityError) as excinfo:
        figure_verify.ensure_capability(base_url="http://x", model="vl",
                                        kinds={figure_extract.KIND_TABLE}, now=1.0)
    checks = excinfo.value.probe.checks
    assert checks["table.image_content_part"] is True
    assert checks["table.json_schema_accepted"] is False

    monkeypatch.setattr(figure_verify.llama_client, "vision_completion",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("no image")),
                        raising=False)
    figure_verify._PROCESS_PROBE_PASSES.clear()
    with pytest.raises(figure_extract.FigureCapabilityError) as excinfo:
        figure_verify.ensure_capability(base_url="http://y", model="vl",
                                        kinds={figure_extract.KIND_TABLE}, now=1.0)
    checks = excinfo.value.probe.checks
    assert checks["table.image_content_part"] is False


def test_probe_cache_hit_skips_live_calls_and_expires(probe_env, monkeypatch):
    install_vl(monkeypatch, probe_responder([3, 5]))
    figure_verify.ensure_capability(base_url="http://x", model="vl",
                                    kinds={figure_extract.KIND_TABLE}, now=1000.0)
    calls: list = []
    responder = probe_responder([3, 5])

    def counting(**kwargs):
        calls.append(kwargs)
        return responder(**kwargs)

    install_vl(monkeypatch, counting)
    result = figure_verify.ensure_capability(base_url="http://x", model="vl",
                                             kinds={figure_extract.KIND_TABLE}, now=1100.0)
    assert result.checks == {"cached": True} and calls == []

    figure_verify._PROCESS_PROBE_PASSES.clear()
    figure_verify.ensure_capability(
        base_url="http://x", model="vl", kinds={figure_extract.KIND_TABLE},
        now=1000.0 + config.FIGURE_PROBE_TTL_SECONDS + 1,
    )
    assert calls, "TTL 過期就要重跑"


@pytest.mark.parametrize(
    "props",
    [None, {}, {"n_ctx": 4096}, {"model_path": "", "model_alias": ""}],
)
def test_probe_refuses_to_cache_without_a_real_model_identity(probe_env, monkeypatch, props):
    """換了 GGUF 但沿用同一個 alias/port 是最常見的漂移——身分不明就不准快取。"""
    install_vl(monkeypatch, probe_responder([3, 5]), props=props)
    result = figure_verify.ensure_capability(base_url="http://x", model="vl",
                                             kinds={figure_extract.KIND_TABLE}, now=1.0)
    assert result.ok and result.fingerprint == ""
    assert "不快取" in result.detail
    assert not probe_env.exists()


def test_probe_fingerprint_changes_with_model_template_schema_and_prompt(monkeypatch, tmp_path):
    base = dict(base_url="http://x", model="vl",
                kinds=[figure_extract.KIND_TABLE], profile="strict_json")
    props = {"model_path": fake_model_path(), "model_alias": "vl",
             "chat_template": "tpl-a", "n_ctx": 4096}
    original = figure_verify._build_probe_fingerprint(props=props, **base)
    assert original, "有真實可 stat 的模型檔才會有 fingerprint"

    other = tmp_path / "other.gguf"
    other.write_bytes(b"GGUF-other")
    assert figure_verify._build_probe_fingerprint(
        props={**props, "model_path": str(other)}, **base) != original
    assert figure_verify._build_probe_fingerprint(
        props={**props, "chat_template": "tpl-b"}, **base) != original
    assert figure_verify._build_probe_fingerprint(
        props=props, **{**base, "model": "other"}) != original
    assert figure_verify._build_probe_fingerprint(
        props=props, **{**base, "profile": "schema_echo"}) != original

    monkeypatch.setattr(figure_verify, "PROBE_VERSION", figure_verify.PROBE_VERSION + 1)
    assert figure_verify._build_probe_fingerprint(props=props, **base) != original


def test_prompt_profile_comes_from_props_not_the_model_name():
    """extractor prompt 依 backend/model capability 路由，不寫死單一模型。"""
    grammar_props = {"chat_template": "…json_schema…", "model_alias": "anything"}
    plain_props = {"chat_template": "plain jinja", "model_alias": "anything"}
    assert figure_verify._resolve_prompt_profile(grammar_props) == "strict_json"
    assert figure_verify._resolve_prompt_profile(plain_props) == "schema_echo"
    assert figure_verify._resolve_prompt_profile(None) == "schema_echo"
    # schema_echo 會把 schema 一起帶進 prompt；strict_json 不會
    echo = figure_verify._prompt_for(figure_extract.KIND_TABLE, "schema_echo")
    strict = figure_verify._prompt_for(figure_extract.KIND_TABLE, "strict_json")
    assert "additionalProperties" in echo and "additionalProperties" not in strict
    for prompt in (echo, strict):
        assert "不翻譯" in prompt and "不得翻成英文" in prompt
        assert "state" in prompt, "table prompt 要講清楚看不清的格填 unreadable"
    terminal_prompt = figure_verify._prompt_for(figure_extract.KIND_TERMINAL, "strict_json")
    assert "uncertain_spans" in terminal_prompt
    assert "[不確定:A|B]" in terminal_prompt, "prompt 要明文禁止把候選寫進逐字正文"


def test_probe_is_skipped_entirely_when_nothing_needs_vl():
    result = figure_verify.ensure_capability(base_url="http://x", model="vl", kinds=set())
    assert result.ok and result.checks == {"skipped": True}


def test_probe_cache_write_failure_is_best_effort(probe_env, monkeypatch):
    """能力已經實測通過了，因為快取寫不進去就中止 ingest 是把便利性問題升級成正確性問題。"""
    install_vl(monkeypatch, probe_responder([3, 5]))
    monkeypatch.setattr(figure_verify, "_save_probe_pass",
                        lambda *a, **k: "OSError: read-only file system")
    result = figure_verify.ensure_capability(base_url="http://x", model="vl",
                                             kinds={figure_extract.KIND_TABLE}, now=1.0)
    assert result.ok and "快取寫入失敗" in result.detail


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("pymupdf") is None
    and __import__("importlib").util.find_spec("fitz") is None,
    reason="canary 圖需要 PyMuPDF（PDF 抽取本來就依賴它）",
)
def test_canary_images_are_generated_in_code():
    """repo 裡不放二進位檔；canary 內容也刻意是通用 ASCII（NDA-free）。"""
    for spec in (figure_verify._CANARY_TABLE_3COL, figure_verify._CANARY_TABLE_5COL,
                 figure_verify._CANARY_TERMINAL):
        png = figure_verify._render_canary_png(spec)
        assert png[:4] == b"\x89PNG" and len(png) > 500
    assert len(figure_verify._CANARY_TABLE_3COL["header"]) == 3
    assert len(figure_verify._CANARY_TABLE_5COL["header"]) == 5


# ============================================================
# HTTP 4xx 診斷（契約 §12.2：T1 不加工，診斷由本模組負責）
# ============================================================
class _FakeResponse:
    def __init__(self, status_code, payload=None, raises=False):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._payload


class _FakeHTTPError(Exception):
    def __init__(self, response=None):
        super().__init__("http error")
        self.response = response


def test_http_error_detail_keeps_the_message_and_nothing_else():
    detail = figure_verify._http_error_detail(
        _FakeHTTPError(_FakeResponse(400, {"error": {"message": "image input not supported"}}))
    )
    assert detail == "HTTP 400: image input not supported"


def test_http_error_detail_truncates_long_messages():
    detail = figure_verify._http_error_detail(
        _FakeHTTPError(_FakeResponse(400, {"error": {"message": "X" * 900}}))
    )
    assert len(detail) < 600 and detail.endswith("（已截斷）")


@pytest.mark.parametrize(
    "response",
    [
        _FakeResponse(500, raises=True),
        _FakeResponse(400, {"detail": "prompt echoed back: 0x4000_0100 CTRL0"}),
        _FakeResponse(400, ["not", "an", "object"]),
    ],
)
def test_http_error_detail_never_echoes_the_body(response):
    """body 可能把 prompt（含 NDA 內容）原樣回顯，只能報 status code。"""
    detail = figure_verify._http_error_detail(_FakeHTTPError(response))
    assert "0x4000_0100" not in detail and "CTRL0" not in detail
    assert "內容不回顯" in detail
    assert str(response.status_code) in detail


def test_http_error_detail_survives_a_missing_response():
    detail = figure_verify._http_error_detail(_FakeHTTPError(None))
    assert "_FakeHTTPError" in detail


# ============================================================
# 文件契約
# ============================================================
def test_module_never_claims_temperature_zero_determinism():
    """契約 §6.4：不得宣稱 `temperature=0` 必然 greedy/deterministic。

    只禁**正面宣稱**；誠實的否定句（「不假設可重現」）本來就該留著。
    """
    source = (REPO_ROOT / "figure_verify.py").read_text(encoding="utf-8")
    for claim in ("必然可重現", "保證可重現", "一定可重現", "必然 greedy",
                  "保證 greedy", "guaranteed deterministic", "always deterministic",
                  "temperature=0 保證", "temperature=0 必然"):
        assert claim not in source, f"不得出現正面宣稱 {claim!r}"
    assert "不假設" in source, "取樣說明必須明講不假設可重現"
    assert "top_k=1" in source


def test_facade_reexports_work_in_both_import_orders():
    """PEP 562 lazy 門面 + 本模組的 module binding 要在兩個方向都不炸。"""
    for script in (
        "import figure_verify, figure_extract; assert figure_extract.FigureResult",
        "import figure_extract; assert figure_extract.extract_document_figures",
        "import figure_verify; assert figure_verify.ProbeResult",
    ):
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=120,
        )
        assert completed.returncode == 0, f"{script!r} 失敗：{completed.stderr[-500:]}"


# ============================================================
# 真 PDF 整合（非 smoke：需要 PyMuPDF / pymupdf4llm / T3 的 planner）
# ============================================================
def test_real_pdf_register_table_is_native_verified_with_zero_vl(monkeypatch, tmp_path):
    """T3 planner → T4 native lane → T2 chunk 的真檔全鏈。

    這條非 smoke，但它是唯一會踩到上游真實行為的測試：pymupdf4llm 的 backtick
    包裹、`Table.extract()` 的底線缺陷、三個 strategy 找到同一張表——三個都是
    stub 測不出來、卻會讓真檔整張表變 `▯` 的問題。
    """
    pymupdf = pytest.importorskip("pymupdf", reason="需要 PyMuPDF")
    pytest.importorskip("pymupdf4llm", reason="PDF ingestion 需要 pymupdf4llm")
    pymupdf4llm = __import__("pymupdf4llm")
    figure_candidates = pytest.importorskip("figure_candidates", reason="T3 尚未交付")
    monkeypatch.delenv("AICODE_ROOT", raising=False)

    pdf_path = tmp_path / "register.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=460, height=300)
    page.insert_text((40, 40), "Register Map", fontname="helv", fontsize=13)
    columns = [40, 120, 250, 310, 380, 440]
    ground_truth = [
        ["Name", "Address", "Bits", "Mode", "Desc"],
        ["CTRL0", "0x4000_0100", "[7:4]", "RW", "clk sel"],
        ["CTRL1", "0x4000_0104", "[3:0]", "RO", "clk sts"],
    ]
    for r, y in enumerate((70, 100, 130)):
        for c in range(5):
            page.draw_rect(pymupdf.Rect(columns[c], y, columns[c + 1], y + 30),
                           color=(0, 0, 0), width=0.7)
            page.insert_text((columns[c] + 3, y + 20), ground_truth[r][c],
                             fontname="cour", fontsize=8)
    doc.save(str(pdf_path))
    doc.close()

    pages = pymupdf4llm.to_markdown(str(pdf_path), page_chunks=True)
    pdf_doc = pymupdf.open(str(pdf_path))
    try:
        plan = figure_candidates.plan_document_figures(
            str(pdf_path), pages, root=tmp_path, pdf_doc=pdf_doc
        )
        assert plan.preflight["vl_calls_max"] == 0, "原生表格不該規劃任何 VL 呼叫"

        monkeypatch.setattr(
            figure_verify.llama_client, "vision_json_completion",
            lambda **kw: pytest.fail("native lane 不得呼叫 VL"), raising=False,
        )
        monkeypatch.setattr(
            figure_verify, "ensure_capability",
            lambda **kw: pytest.fail("零 VL 的文件不該做 capability probe"),
        )
        results = figure_verify.extract_document_figures(
            plan, pdf_doc=pdf_doc, page_evidence=plan.page_evidence,
            vl_base_url="http://127.0.0.1:8083", vl_model="vl",
            render_variants=lambda _doc, _cand: pytest.fail("native lane 不該 render"),
        )
    finally:
        pdf_doc.close()

    assert len(results) == 1
    result = results[0]
    assert result.verification_status == figure_extract.VERIF_NATIVE
    assert [c["label"] for c in result.payload["columns"]] == ground_truth[0]
    assert [[cell["text"] for cell in row["cells"]]
            for row in result.payload["rows"]] == ground_truth[1:]

    chunks = figure_extract.build_figure_chunks(
        [result], source=pdf_path.name, doc_type="spec", next_chunk_index={},
        evidence_ref_by_figure={result.figure_id: ".codetrail/figures/x/manifest.json"},
    )
    assert len(chunks) == 1
    assert chunks[0]["verification_status"] == figure_extract.VERIF_NATIVE
    assert "0x4000_0100" in chunks[0]["content"]
    assert figure_extract.UNREADABLE_GLYPH not in chunks[0]["content"]


# ============================================================
# 總審第一輪修正（契約 §15）
# ============================================================
@pytest.mark.smoke
def test_lane_comes_only_from_the_candidate_signal(monkeypatch):
    """lane 的唯一真相是 `candidate.signals["native_lane"]`（契約 §15.1）。

    verifier 自己重算 lane 正是總審 BLOCKER #1：planner 把 word-only terminal 算成
    要 1~2 次 VL，verifier 卻走零 VL 的 native lane，於是預算誤報、probe 誤擋。
    這條用「有 native_table 但 signal 說 False」的候選證明 verifier 不再自行判斷。
    """
    spy = VLSpy({"figure_table": REGISTER_TABLE})
    install_vl(monkeypatch, spy)
    probes: list = []
    monkeypatch.setattr(
        figure_verify, "ensure_capability",
        lambda **kw: probes.append(kw) or figure_verify.ProbeResult(
            True, "fp", {"stub": True}, [], "stub"),
    )

    forced_vl = register_candidate(native_lane=False)
    results = extract([forced_vl], {4: page_evidence(raw_markdown=REGISTER_MD,
                                                    words=REGISTER_WORDS)})

    assert spy.calls, "signal 說 VL 就要走 VL，不得因為看得到 native_table 就改判"
    assert probes, "走 VL lane 就必須先過 capability probe"
    assert results[0].evidence["lane"] == "vl"


@pytest.mark.smoke
def test_missing_native_lane_signal_is_fail_loud(monkeypatch):
    """signal 缺失時猜一個預設值，等於把三處不一致換成一處無聲不一致。

    候選同樣用「native lane 會成功」的那種：`candidate()` 沒有 `native_table`，
    走到哪條 lane 都會因為別的理由 raise，測試就守不住這條契約。
    """
    monkeypatch.setattr(
        figure_verify, "ensure_capability",
        lambda **kw: pytest.fail("lane 判定失敗就不該走到 probe"),
    )
    monkeypatch.setattr(
        figure_verify.llama_client, "vision_json_completion",
        lambda **kw: pytest.fail("lane 判定失敗就不該呼叫 VL"), raising=False,
    )
    broken = register_candidate()
    broken.signals = {key: value for key, value in broken.signals.items()
                      if key != "native_lane"}
    evidences = {4: page_evidence(raw_markdown=REGISTER_MD, words=REGISTER_WORDS)}
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        extract([broken], evidences)
    assert broken.figure_id in str(excinfo.value), "訊息要帶 figure_id（契約 §17.4）"


def test_native_lane_signal_true_means_zero_vl_and_zero_probe(monkeypatch):
    monkeypatch.setattr(
        figure_verify.llama_client, "vision_json_completion",
        lambda **kw: pytest.fail("native lane 不得呼叫 VL"), raising=False,
    )
    monkeypatch.setattr(
        figure_verify, "ensure_capability",
        lambda **kw: pytest.fail("零 VL 的文件不該做 capability probe"),
    )
    results = extract([register_candidate(native_lane=True)],
                      {4: page_evidence(raw_markdown=REGISTER_MD, words=REGISTER_WORDS)})
    assert results[0].verification_status == figure_extract.VERIF_NATIVE
    assert results[0].variants == []
    assert results[0].model_input_variant == "native"


def test_word_only_terminal_runs_through_the_vl_lane(monkeypatch):
    """word-only vector terminal：signal 說 VL → 真的走 VL，probe 有被呼叫。"""
    spy = VLSpy({"figure_terminal": terminal_json([("  indented", []), ("", []),
                                                   ("trailing  ", [])])})
    install_vl(monkeypatch, spy)
    probes: list = []
    monkeypatch.setattr(
        figure_verify, "ensure_capability",
        lambda **kw: probes.append(kw) or figure_verify.ProbeResult(
            True, "fp", {"stub": True}, [], "stub"),
    )
    cand = terminal_candidate(native_lane=False)
    evidence = page_evidence(words=words_from([(10.0, [(10, 60, "indented")])]))
    result = extract([cand], {4: evidence})[0]

    assert probes and spy.calls
    # 行首縮排與行尾空白逐位元組保留（不是 word baseline 合成的）
    assert [line["text"] for line in result.payload["lines"]] == [
        "  indented", "", "trailing  "
    ]


def test_find_tables_is_never_canonical_even_as_the_only_channel():
    """契約 §15.2：已知會改寫 hex 的通道，就算是唯一通道也不得當正文。"""
    cand = candidate(native_table={"pos": None, "markdown": "", "strategy": "lines",
                                   "geometry": {"cells": [], "rows": [], "cols": []}})
    evidence = page_evidence(tables=unreliable_tables("0x4000 0100\n_"))
    # 只有 find_tables 通道 → 沒有安全的 canonical 來源
    channels, _words, _unreliable = figure_verify._table_channels(0, cand, evidence, [])
    assert [name for name, _grid in channels] == ["find_tables:lines"]
    assert figure_verify._pick_canonical_channel(channels) == (None, None)

    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        figure_verify.verify_native_table(cand, evidence)
    message = str(excinfo.value)
    assert "沒有安全的" in message and "find_tables" in message


@pytest.mark.smoke
@pytest.mark.parametrize(
    "extra_cells, label",
    [([("40", "70", "X")], "wide"), ([], "short")],
)
def test_native_grid_row_width_is_fail_loud(extra_cells, label):
    """契約 §15.3：native 路徑的短列/寬列一律 fail-loud，不補空格也不截斷。

    原本 `_grid_to_table_payload()` 只迭代表頭欄數，三格資料配兩欄表頭會**無聲
    丟掉第三格**；`_entry_grid()` 又會先把短列補到最大寬度。
    """
    header = ["Name", "Address"]
    row = ["CTRL0"] + (["0x1"] if label == "wide" else []) + [c[2] for c in extra_cells]
    grid = {"header": header, "rows": [row]}
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        figure_verify._grid_to_table_payload(grid, where="page=1 figure=fig_x")
    assert "不補不砍" in str(excinfo.value)


@pytest.mark.parametrize(
    "extract_raw, label",
    [
        ([["A", "B"], ["1"], ["2", "3"]], "short"),
        ([["A", "B"], ["1", "2", "3"]], "wide"),
    ],
)
def test_entry_grid_ragged_row_is_fail_loud(extract_raw, label):
    """契約 §15.3：保留原始寬度並立即 fail-loud。

    補空格會憑空造值、截一格會錯位，而把整列改寫成表頭寬度的 `None`（先前的做法）
    等於把寬列的內容直接丟掉。
    """
    entry = {"geometry": {"extract_raw": extract_raw}}
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        figure_verify._entry_grid(entry, where="page=1 figure=fig_x")
    assert "不補不砍" in str(excinfo.value)


def test_multi_row_header_is_not_turned_into_a_data_row():
    """契約 §15.4：separator 之前的每一列都是表頭，逐欄可逆攤平。"""
    grid = figure_verify._parse_markdown_grid(
        "| Top | Address |\n| Name | Base |\n|---|---|\n| CTRL0 | 0x100 |\n"
    )
    assert grid["header"] == ["Top\nName", "Address\nBase"]
    assert grid["header_layers"] == [["Top", "Address"], ["Name", "Base"]]
    assert grid["rows"] == [["CTRL0", "0x100"]], "第二層表頭不得變成資料列"
    # 攤平是可逆的
    assert [label.split("\n") for label in grid["header"]] == [["Top", "Name"],
                                                              ["Address", "Base"]]


@pytest.mark.parametrize(
    "text",
    [
        "| Top | Address |\n| CTRL0 | 0x100 |\n",              # 完全沒有 separator
        "| Top | Address |\n| Name |\n|---|---|\n| A | B |\n",  # 表頭各層寬度不一致
    ],
)
def test_markdown_grid_abstains_when_the_header_band_is_unsafe(text):
    """無法安全判定 header band 時 abstain——絕不把表頭當資料列。"""
    assert figure_verify._parse_markdown_grid(text) is None


@pytest.mark.smoke
def test_native_terminal_carriage_return_reaches_the_validator():
    """契約 §15.5：非法字元原值保留，validator 才擋得住（總審 BLOCKER #6）。

    先前 `rstrip("\\r")` 讓 `"A\\r"` 變成 canonical `"A"`——validator 依契約 §2.3
    永遠拒絕不了它，因為它在驗證之前就被改寫了。
    """
    payload = figure_verify._lines_to_terminal_payload(["A\r", "B"])
    assert payload["lines"][0]["text"] == "A\r", "原值必須原樣保留"
    with pytest.raises(figure_extract.FigureValidationError):
        figure_extract.validate_payload(payload, figure_extract.KIND_TERMINAL)

    raw = "A\r\nB\n"
    cand = terminal_candidate()
    evidence = page_evidence(
        raw_markdown=raw,
        page_boxes=[{"index": 0, "class": "text", "bbox": (0.0, 0.0, 200.0, 80.0),
                     "pos": (0, len(raw))}],
        words=words_from([(10.0, [(10, 30, "A")]), (30.0, [(10, 30, "B")])]),
    )
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        figure_verify._verify_native_terminal(cand, evidence)
    assert "不再合法" in str(excinfo.value)


def test_duplicate_occurrence_declares_no_model_input_of_its_own(monkeypatch):
    """契約 §15.6：第二個 occurrence 從未送模，不得把代表的 variant id 抄過來。"""
    spy = VLSpy({"figure_table": REGISTER_TABLE})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    first = candidate(seed="dup-a", page=2, asset_digest="same", index=1)
    second = candidate(seed="dup-b", page=7, asset_digest="same", index=2)
    results = extract([first, second], {2: page_evidence(page=2), 7: page_evidence(page=7)})

    representative, duplicate = results
    assert representative.variants == ["crop@200dpi"]
    assert duplicate.variants == [], "從未送模的 render 不得被列成實際模型輸入"
    assert duplicate.model_input_variant == f"duplicate_of:{representative.figure_id}"
    cross = duplicate.evidence["duplicate_model_input"]
    assert cross["figure_id"] == representative.figure_id
    assert cross["variants"] == ["crop@200dpi"], "真正送模的那一張仍然指得出來"


def test_terminal_canonical_text_comes_from_the_planner_signal():
    """契約 §15.1：canonical 正文取 `signals["native_text"]`，不由 verifier 反查。

    planner 用的是校準過的 unrotated bbox；verifier 若自己用上游 display-space
    bbox 反查 page_boxes，旋轉/裁切頁上兩者會對不起來，結果是「planner 說
    native、verifier 找不到 pos」的整份零寫入。
    """
    raw = "$ ls\n\nfile.txt\n"
    cand = terminal_candidate(
        native_text={"pos": (0, len(raw)), "markdown": raw,
                     "source": "page_boxes:text", "box_index": 0},
    )
    # page_boxes 刻意留空：verifier 反查不到，但 signal 指得出來
    result = figure_verify._verify_native_terminal(
        cand, page_evidence(raw_markdown=raw, page_boxes=[],
                            words=words_from([(10.0, [(10, 30, "$"), (35, 60, "ls")]),
                                              (40.0, [(10, 80, "file.txt")])]))
    )
    assert [line["text"] for line in result.payload["lines"]] == ["$ ls", "", "file.txt", ""]


def test_stale_plan_signal_versus_page_evidence_is_fail_loud():
    """signal 的原文與 `raw_markdown[pos]` 不一致 → 不得挑一份當 canonical。"""
    raw = "$ ls\n"
    cand = terminal_candidate(
        native_text={"pos": (0, len(raw)), "markdown": "完全不同的內容\n",
                     "source": "page_boxes:text", "box_index": 0},
    )
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        figure_verify._verify_native_terminal(cand, page_evidence(raw_markdown=raw))
    assert "不同步" in str(excinfo.value)


def test_pos_lookup_prefers_the_calibrated_coordinates():
    """T3 的 `_bbox_unrotated` / `_pos` 是校準過的；上游 `bbox` 是 display 空間。

    兩者在旋轉頁上對不起來——planner 說 native、verifier 反查不到 pos，就是整份
    零寫入。T3 目前對旋轉頁一律 abstain，這條守的是「哪天放寬時不會再分岔」。
    """
    evidence = page_evidence(
        raw_markdown="x" * 20,
        page_boxes=[{"class": "text", "bbox": (500.0, 500.0, 600.0, 600.0), "pos": (0, 5),
                     "_bbox_unrotated": (0.0, 0.0, 200.0, 80.0), "_pos": (3, 9)}],
    )
    assert figure_verify._page_box_pos_for(evidence, (0.0, 0.0, 200.0, 80.0)) == (3, 9)

    legacy = page_evidence(
        raw_markdown="x" * 20,
        page_boxes=[{"class": "text", "bbox": (0.0, 0.0, 200.0, 80.0), "pos": (0, 5)}],
    )
    assert figure_verify._page_box_pos_for(legacy, (0.0, 0.0, 200.0, 80.0)) == (0, 5)


# ============================================================
# local code review 修正（NDA 防漏、身分、正面證據、接合、快取）
# ============================================================
SENSITIVE = "0x4000_0100 CTRL0 客戶機密規格"


@pytest.mark.smoke
@pytest.mark.parametrize(
    "response",
    [
        _FakeResponse(400, {"error": SENSITIVE}),                    # 頂層 error 是字串
        _FakeResponse(400, {"error": {"detail": SENSITIVE}}),        # 有 error 但沒有 message
        _FakeResponse(400, {"error": {"message": None}, "echo": SENSITIVE}),
        _FakeResponse(500, {"prompt": SENSITIVE}),
    ],
)
def test_http_error_detail_only_accepts_nested_error_message(response):
    """llama.cpp 的 400 body 可能把送出去的 prompt 原樣吐回來——那是文件內容。

    契約只允許擷取巢狀 `error.message`；其餘形狀一律只報 status code。
    """
    detail = figure_verify._http_error_detail(_FakeHTTPError(response))
    assert SENSITIVE not in detail and "0x4000_0100" not in detail
    assert "內容不回顯" in detail
    assert str(response.status_code) in detail


@pytest.mark.smoke
def test_http_error_detail_without_response_only_reports_the_type():
    """`str(exc)` 本身就可能含 response body（requests 會把它拼進訊息）。"""
    exc = _FakeHTTPError(None)
    exc.args = (f"500 Server Error: {SENSITIVE}",)
    detail = figure_verify._http_error_detail(exc)
    assert SENSITIVE not in detail
    assert detail.startswith("_FakeHTTPError")


@pytest.mark.smoke
@pytest.mark.parametrize(
    "props, label",
    [
        ({"model_alias": "vl", "chat_template": "t"}, "alias-only"),
        ({"model_path": "/nonexistent/never-here.gguf", "model_alias": "vl"}, "unstatable"),
        ({"model_path": "", "model_alias": "vl"}, "empty-path"),
    ],
)
def test_model_identity_needs_a_verifiable_signature(props, label):
    """alias 或 stat 不到的 path 都不算模型身分——同路徑覆蓋是升級模型的標準做法。"""
    assert figure_verify._model_identity(props) is None
    assert figure_verify._build_probe_fingerprint(
        base_url="http://x", model="vl", kinds=[figure_extract.KIND_TABLE],
        props=props, profile="strict_json") == ""


def test_probe_fingerprint_changes_when_the_same_path_gets_a_new_model():
    """換 GGUF 但沿用同一個路徑/alias/port —— 最常見的漂移，必須讓快取失效。"""
    base = dict(base_url="http://x", model="vl",
                kinds=[figure_extract.KIND_TABLE], profile="strict_json")
    props = {"model_path": fake_model_path(b"GGUF-v1"), "model_alias": "vl"}
    first = figure_verify._build_probe_fingerprint(props=props, **base)
    assert first
    fake_model_path(b"GGUF-v2-a-completely-different-model")
    assert figure_verify._build_probe_fingerprint(props=props, **base) != first
    fake_model_path()   # 還原，避免影響其他測試


@pytest.mark.smoke
def test_stale_plan_markdown_versus_page_evidence_is_fail_loud_for_tables():
    """table 的 `pos` 切片也要逐位元組核對 plan 宣告的 markdown。

    offset 合法**不代表**切到同一份內容：混用兩次 ingest 的 plan/evidence 時，
    figure 身分是舊的、內容卻是別張表的，產出一份看起來完全合法的錯配 payload。
    """
    declared = "| Name | Address |\n|---|---|\n| CTRL0 | 0x4000_0100 |\n"
    on_page = "| Name | Address |\n|---|---|\n| GPIO9 | 0xDEAD_BEEF |\n"
    cand = candidate(native_table={"pos": (0, len(on_page)), "markdown": declared,
                                   "strategy": "lines"})
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        figure_verify.verify_native_table(cand, page_evidence(raw_markdown=on_page))
    assert "不同步" in str(excinfo.value)


def test_two_tables_on_one_page_keep_their_own_pos_slices():
    """同頁兩表：各自的 `pos` 切到各自的內容，宣告的 markdown 都對得上。"""
    first = "| Name | Address |\n|---|---|\n| CTRL0 | 0x4000_0100 |\n"
    second = "| Name | Address |\n|---|---|\n| CTRL9 | 0x4000_0900 |\n"
    raw = first + "\n" + second
    for offset, block, expected in ((0, first, "0x4000_0100"),
                                    (len(first) + 1, second, "0x4000_0900")):
        cand = candidate(native_table={"pos": (offset, offset + len(block)),
                                       "markdown": block, "strategy": "lines"})
        result = figure_verify.verify_native_table(cand, page_evidence(raw_markdown=raw))
        assert result.payload["rows"][0]["cells"][1]["text"] == expected


def test_header_conflict_between_channels_is_needs_review():
    """表頭差一個字 → 每一格的欄位身分都不可信，不得只降 check 落到 corroborated。"""
    markdown = "| Name | Addres |\n|---|---|\n| CTRL0 | 0x4000_0100 |\n"
    words = words_from([(10.0, [(10, 60, "Name"), (70, 170, "Address")]),
                        (30.0, [(10, 60, "CTRL0"), (70, 170, "0x4000_0100")])])
    cand = candidate(native_table={"pos": (0, len(markdown)), "markdown": markdown,
                                   "strategy": "lines"})
    result = figure_verify.verify_native_table(
        cand, page_evidence(raw_markdown=markdown, words=words))
    assert "header_conflict" in result.reasons
    assert result.verification_status == figure_extract.VERIF_NEEDS_REVIEW


def test_only_unreliable_channel_cannot_reach_trusted_status():
    """唯一的通道是已知會改寫 hex 的 extract → 連 canonical 都不給，更不用說 trusted。"""
    cand = candidate(native_table={"pos": None, "markdown": "", "strategy": "lines",
                                   "geometry": {"cells": [], "rows": [], "cols": []}})
    with pytest.raises(figure_extract.FigureExtractionError):
        figure_verify.verify_native_table(
            cand, page_evidence(tables=unreliable_tables("0x4000 0100\n_")))


@pytest.mark.parametrize("raw_row", [["CTRL0"], ["CTRL0", "0x1", "extra"]])
def test_ragged_native_row_fails_through_the_full_verifier(raw_row):
    """short / wide row 要走完整 `verify_native_table`，不是只直接測 grid helper。"""
    cand = candidate(native_table={
        "pos": None, "markdown": "", "strategy": "lines",
        "geometry": {"cells": [], "rows": [], "cols": []}})
    tables = {"lines": [{
        "strategy": "lines", "degenerate": False, "bbox": (0.0, 0.0, 200.0, 80.0),
        "geometry": {"extract_raw": [["Name", "Address"], raw_row]},
    }]}
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        figure_verify.verify_native_table(cand, page_evidence(tables=tables))
    assert "不補不砍" in str(excinfo.value)


def test_multi_row_header_survives_the_full_canonical_selection():
    """canonical 必須是保留 header band 的 markdown 通道，不是壓平的 word grid。"""
    markdown = ("| Top | Address |\n| Name | Base |\n|---|---|\n"
                "| CTRL0 | 0x4000_0100 |\n")
    words = words_from([(10.0, [(10, 60, "Top"), (70, 170, "Address")]),
                        (30.0, [(10, 60, "Name"), (70, 170, "Base")]),
                        (50.0, [(10, 60, "CTRL0"), (70, 170, "0x4000_0100")])])
    cand = candidate(native_table={"pos": (0, len(markdown)), "markdown": markdown,
                                   "strategy": "lines"})
    result = figure_verify.verify_native_table(
        cand, page_evidence(raw_markdown=markdown, words=words))
    assert [c["label"] for c in result.payload["columns"]] == ["Top\nName", "Address\nBase"]
    assert result.evidence["native"]["canonical_channel"] == "markdown_pos"
    # 第二層表頭不得變成資料列
    assert [[c["text"] for c in row["cells"]] for row in result.payload["rows"]] == [
        ["CTRL0", "0x4000_0100"]
    ]
    # word grid 把第一列當表頭 → 表頭不一致 → needs_review，不得升成 native_verified
    assert result.verification_status == figure_extract.VERIF_NEEDS_REVIEW


def test_cell_newline_from_markdown_is_preserved_as_canonical():
    """格內換行（`<br>`）只有 markdown 通道保得住；word 通道會壓成空格。"""
    markdown = "| Name | Note |\n|---|---|\n| CTRL0 | `first`<br>`second` |\n"
    cand = candidate(native_table={"pos": (0, len(markdown)), "markdown": markdown,
                                   "strategy": "lines"})
    result = figure_verify.verify_native_table(cand, page_evidence(raw_markdown=markdown))
    assert result.payload["rows"][0]["cells"][1]["text"] == "first\nsecond"


def test_unpaired_payload_rows_are_masked_not_left_as_text(monkeypatch):
    """有通道對齊成功、卻沒被配對的列＝anchor 說它不在這張表裡，正文不得留著。"""
    ghost = "0xBADD_C0DE"
    payload_json = table_json(
        ["Name", "Address"],
        [[("CTRL0", "observed"), ("0x4000_0100", "observed")],
         [("CTRL1", "observed"), ("0x4000_0104", "observed")],
         [("GHOST", "observed"), (ghost, "observed")]],
    )
    spy = VLSpy({"figure_table": payload_json})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)
    # anchor（words）只有前兩列 → 覆蓋率 2/3 ≥ 門檻，通道對齊成功，
    # 第三列則是「anchor 說它不在這張表裡」。
    words = words_from([(10.0, [(10, 60, "Name"), (70, 170, "Address")]),
                        (30.0, [(10, 60, "CTRL0"), (70, 170, "0x4000_0100")]),
                        (50.0, [(10, 60, "CTRL1"), (70, 170, "0x4000_0104")])])
    evidence = page_evidence(words=words)
    cand = candidate(native_table={
        "pos": None, "markdown": "", "strategy": "lines",
        "geometry": {"cells": [[(5, 5, 65, 25), (65, 5, 175, 25)],
                               [(5, 25, 65, 45), (65, 25, 175, 45)],
                               [(5, 45, 65, 65), (65, 45, 175, 65)]],
                     "rows": [10.0, 30.0, 50.0], "cols": [5, 65, 175]}},
        native_lane=False)
    result = extract([cand], {4: evidence})[0]

    blob = json.dumps(result.payload, ensure_ascii=False)
    assert ghost not in blob, "未配對的候選文字不得留在 canonical 正文"
    assert "GHOST" not in blob
    assert result.verification_status == figure_extract.VERIF_NEEDS_REVIEW
    assert ghost in json.dumps(result.evidence, ensure_ascii=False), "原文要留在 evidence"


def test_rowspan_geometry_conflicting_with_text_is_masked():
    """geometry 說跨列、兩列文字卻不同 → 兩份都是候選，留任何一份都是擇一。"""
    markdown = "| Group | Name |\n|---|---|\n| CLK | CTRL0 |\n| PLL | CTRL1 |\n"
    words = words_from([(10.0, [(10, 60, "Group"), (70, 170, "Name")]),
                        (30.0, [(10, 60, "CLK"), (70, 170, "CTRL0")]),
                        (50.0, [(10, 60, "PLL"), (70, 170, "CTRL1")])])
    span = (5, 25, 65, 65)
    geometry = {"cells": [[(5, 5, 65, 25), (65, 5, 175, 25)],
                          [span, (65, 25, 175, 45)],
                          [span, (65, 45, 175, 65)]],
                "rows": [10.0, 30.0, 50.0], "cols": [5, 65, 175]}
    cand = candidate(native_table={"pos": (0, len(markdown)), "markdown": markdown,
                                   "strategy": "lines", "geometry": geometry})
    result = figure_verify.verify_native_table(
        cand, page_evidence(raw_markdown=markdown, words=words))
    conflicted = result.payload["rows"][1]["cells"][0]
    assert conflicted["text"] == figure_extract.UNREADABLE_GLYPH
    assert conflicted["state"] == figure_extract.CELL_STATE_CONFLICT
    assert conflicted["inherited_from_row"] is None
    assert "span_ambiguous" in result.reasons
    alternatives = result.evidence["cells"]["r2c1"]["alternatives"]
    assert sorted(alternatives) == ["CLK", "PLL"], "兩份候選都要留在 evidence"


def test_adjacent_uncertain_spans_are_not_unioned():
    """相鄰兩格的候選集合直接聯集，會產生對那個區間毫無意義的 alternatives。"""
    glyph = figure_extract.UNREADABLE_GLYPH
    merged = figure_verify._merge_uncertain_spans(
        f"a{glyph}{glyph}b", [], [(1, 2, ["A"]), (2, 3, ["B", "C"])]
    )
    assert merged == [
        {"start": 1, "end": 2, "alternatives": ["A"]},
        {"start": 2, "end": 3, "alternatives": ["B", "C"]},
    ]


def test_overlapping_uncertain_spans_do_not_invent_candidates():
    """真正重疊時無法還原完整候選 → alternatives 只放等長 `▯`，原文留在 evidence。"""
    glyph = figure_extract.UNREADABLE_GLYPH
    merged = figure_verify._merge_uncertain_spans(
        f"a{glyph}{glyph}b", [{"start": 1, "end": 3, "alternatives": ["XY"]}],
        [(2, 3, ["Z"])],
    )
    assert merged == [{"start": 1, "end": 3, "alternatives": [glyph * 2]}]


def test_zero_overlap_boundary_never_deduplicates(monkeypatch):
    """`overlap_px == 0` 的邊界沒有任何東西該被去重——相同字串是合法的重複。"""
    same = [("done", "observed"), ("0x1", "observed")]
    texts = [table_json(["Name", "Address"], [same]),
             table_json(["Name", "Address"], [same])]
    spy = VLSpy({"figure_table": texts + texts})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    def render(_doc, cand):
        return tiled_render(cand.figure_id, overlap_bands=(), overlap_px=0)

    result = extract([candidate()], {4: page_evidence()}, render=render)[0]
    assert len(result.payload["rows"]) == 2, "零 overlap 的邊界不得去重"
    assert result.evidence["stitch"]["overlap_matched"] == [0]
    assert "stitch_uncertain" not in result.reasons


def test_repeated_log_line_across_a_real_overlap_boundary_survives(monkeypatch):
    """跨邊界的合法重複行：幾何說只有 1 個 band 重疊，就只能去重 1 行。"""
    first = terminal_json([("retry", []), ("retry", [])])
    second = terminal_json([("retry", []), ("retry", []), ("done", [])])
    texts = [first, second]
    spy = VLSpy({"figure_terminal": texts + texts})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    def render(_doc, cand):
        return tiled_render(cand.figure_id, overlap_bands=(3,), overlap_px=48)

    result = extract([candidate(kind=figure_extract.KIND_TERMINAL)], {4: page_evidence()},
                     render=render)[0]
    # 幾何額度是 1 → 只去掉一行，第三個 retry 是真的存在的
    assert [line["text"] for line in result.payload["lines"]] == [
        "retry", "retry", "retry", "done"
    ]
    assert result.evidence["stitch"]["overlap_matched"] == [1]


def test_overlap_without_band_geometry_is_stitch_uncertain(monkeypatch):
    """有重疊像素卻沒有 band 幾何說得出哪幾行可去重 → 保留兩份並標不確定。"""
    texts = [terminal_json([("a", []), ("b", [])]), terminal_json([("b", []), ("c", [])])]
    spy = VLSpy({"figure_terminal": texts + texts})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    def render(_doc, cand):
        return tiled_render(cand.figure_id, overlap_px=48,
                            stitch_second={"tile_index": 2, "tile_total": 2})

    result = extract([candidate(kind=figure_extract.KIND_TERMINAL)], {4: page_evidence()},
                     render=render)[0]
    assert result.evidence["stitch"]["uncertain"] is True
    assert "stitch_uncertain" in result.reasons
    assert [line["text"] for line in result.payload["lines"]] == ["a", "b", "b", "c"]


def test_tile_footnote_conflict_is_not_silently_unioned(monkeypatch):
    texts = [table_json(["Name"], [[("A", "observed")]], footnotes=["note-1"]),
             table_json(["Name"], [[("B", "observed")]], footnotes=["note-2"])]
    spy = VLSpy({"figure_table": texts + texts})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)
    result = extract([candidate()], {4: page_evidence()},
                     render=lambda _doc, cand: tiled_render(cand.figure_id))[0]
    assert "stitch_footnote_conflict" in result.reasons
    assert result.verification_status == figure_extract.VERIF_NEEDS_REVIEW


@pytest.mark.parametrize(
    "variants_for, fragment",
    [
        (lambda fid: [variant(fid, tile_index=i, tile_total=99)
                      for i in range(1, 100)], "超過單一候選上限"),
        (lambda fid: [variant(fid, tile_index=1, tile_total=1)], "tile_index=0"),
        (lambda fid: [variant(fid, tile_index=0, tile_total=0)], "Variant.tile_total=0 必須 >= 1"),
        (lambda fid: [variant(fid, est_image_tokens=0)], "est_image_tokens"),
        (lambda fid: [variant(fid, est_image_tokens=-5)], "est_image_tokens"),
    ],
)
def test_variant_shape_and_tile_budget_are_enforced(monkeypatch, variants_for, fragment):
    """未 tile 嚴格是 (0, 1)；tiled 嚴格是 1..total 且 total <= 上限；token 必須為正。"""
    spy = VLSpy({"figure_table": REGISTER_TABLE})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        extract([candidate()], {4: page_evidence()},
                render=lambda _doc, cand: variants_for(cand.figure_id))
    assert fragment in str(excinfo.value)
    assert spy.calls == []


def test_duplicate_inherits_asset_level_evidence_but_not_page_conflicts(monkeypatch):
    """asset-level（stitch / 兩次取樣）跟著影像走；occurrence-level（anchor）每頁重算。"""
    sample_a = REGISTER_TABLE
    sample_b = REGISTER_TABLE.replace("0x8000_0100", "0xB000_0100")
    spy = VLSpy({"figure_table": [sample_a, sample_b]})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    first = candidate(seed="dup-a", page=2, asset_digest="same", index=1)
    second = candidate(seed="dup-b", page=7, asset_digest="same", index=2)
    representative, duplicate = extract(
        [first, second], {2: page_evidence(page=2), 7: page_evidence(page=7)})

    assert len(spy.calls) == 2, "第二個 occurrence 不得再打 VL"
    # asset-level 的字元衝突（兩次取樣 8/B）必須跟著影像帶到第二個 occurrence
    for result in (representative, duplicate):
        assert result.payload["rows"][0]["cells"][1]["text"] == "0x▯000_0100"
        assert "glyph_conflict" in result.reasons
        assert result.verification_status == figure_extract.VERIF_NEEDS_REVIEW
    assert duplicate.evidence["repeatability"]["identical"] is False


def test_duplicate_starts_from_the_pristine_extraction(monkeypatch):
    """快取存的必須是**任何頁面 anchor 遮罩之前**的抽取，不是第一頁的成品。"""
    spy = VLSpy({"figure_table": REGISTER_TABLE})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    # 第一頁有 anchor 且與 payload 衝突 → 第一張被遮罩；第二頁沒有 anchor
    words = words_from([(10.0, [(10, 60, "Name"), (70, 170, "Address"),
                                (180, 220, "Bits"), (230, 270, "Mode")]),
                        (30.0, [(10, 60, "CTRL0"), (70, 170, "0x9999_9999"),
                                (180, 220, "[7:4]"), (230, 270, "RW")])])
    geometry = {"cells": [[(5, 5, 65, 25), (65, 5, 175, 25), (175, 5, 225, 25),
                           (225, 5, 275, 25)],
                          [(5, 25, 65, 45), (65, 25, 175, 45), (175, 25, 225, 45),
                           (225, 25, 275, 45)]],
                "rows": [10.0, 30.0], "cols": [5, 65, 175, 225, 275]}
    first = candidate(seed="p-a", page=2, asset_digest="same", index=1,
                      bbox=(0.0, 0.0, 300.0, 80.0), native_lane=False,
                      native_table={"pos": None, "markdown": "", "strategy": "lines",
                                    "geometry": geometry})
    second = candidate(seed="p-b", page=7, asset_digest="same", index=2, native_lane=False)
    representative, duplicate = extract(
        [first, second], {2: page_evidence(page=2, words=words), 7: page_evidence(page=7)})

    assert figure_extract.UNREADABLE_GLYPH in \
        representative.payload["rows"][0]["cells"][1]["text"], "第一頁的 anchor 衝突要被遮罩"
    # 第二頁沒有 anchor，不得繼承第一頁的 occurrence 級衝突
    assert duplicate.payload["rows"][0]["cells"][1]["text"] == "0x8000_0100"
    assert figure_extract.UNREADABLE_GLYPH not in \
        json.dumps(duplicate.payload, ensure_ascii=False)


@pytest.mark.smoke
@pytest.mark.parametrize(
    "value, label",
    [
        ("false", "字串 false"),
        ("true", "字串 true"),
        (1, "int 1"),
        (0, "int 0"),
        (None, "None"),
        ([], "空 list"),
        ("", "空字串"),
    ],
)
def test_non_bool_native_lane_is_fail_loud_from_the_public_entry(monkeypatch, value, label):
    """契約 §17.4：`native_lane` 只接受**精確 bool**，且缺值/型別錯的行為三方一致。

    這條刻意走**公開的** `extract_document_figures` 入口——不一致正是從這裡進來
    才走得到：verifier 原本用 truthiness，`"false"` 在 `RAG.py` 是錯誤、在這裡卻
    是 native lane，於是**跳過 VL capability probe**。只測內部的 `_lane_for`
    守不住這條路徑。

    同時斷言 fail-loud 發生在任何 probe / render / VL 之前。
    """
    probes: list = []
    renders: list = []
    monkeypatch.setattr(
        figure_verify.llama_client, "vision_json_completion",
        lambda **kw: pytest.fail("lane 判定失敗就不該呼叫 VL"), raising=False,
    )
    monkeypatch.setattr(
        figure_verify, "ensure_capability",
        lambda **kw: probes.append(kw) or figure_verify.ProbeResult(
            True, "fp", {"stub": True}, [], "stub"),
    )

    # 候選刻意是「native lane 會成功、VL lane 也走得下去」的那種：否則不管走哪條
    # 都會因為別的理由 raise，測試就變成空的——舊的 truthiness 實作照樣會綠。
    #   truthy 非 bool（"false" / "true" / 1）→ 舊實作走 native lane 並**成功**，
    #                                            完全不 raise → 這條測試就會紅。
    #   falsy 非 bool（0 / None / [] / ""）    → 舊實作走 VL lane → 呼叫 probe
    #                                            → `probes == []` 斷言會紅。
    bad = register_candidate()
    bad.signals = dict(bad.signals, native_lane=value)
    evidences = {4: page_evidence(raw_markdown=REGISTER_MD, words=REGISTER_WORDS)}
    plan = types.SimpleNamespace(
        document_id=DOC_ID, candidates=[bad], page_evidence=evidences,
        stats={}, preflight={}, over_budget=[],
    )
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        figure_verify.extract_document_figures(
            plan, pdf_doc=None, page_evidence=evidences,
            vl_base_url="http://127.0.0.1:8083", vl_model="vl",
            render_variants=lambda _doc, cand: renders.append(cand) or [variant(cand.figure_id)],
        )

    # 契約 §17.4 只保證「訊息帶 page/figure_id」，措辭由 `figure_extract` 決定，
    # 這裡就不去釘別人擁有的字串。
    assert bad.figure_id in str(excinfo.value), f"{label}: 訊息要帶 figure_id"
    assert probes == [], f"{label}: 不得走到 capability probe"
    assert renders == [], f"{label}: 不得 render 任何 variant"


# ============================================================
# 終審第三輪修正（契約 §19.1 / §19.3）
# ============================================================
AMBIGUOUS_LOG = terminal_json([
    ("$ dmesg | tail -3", []),
    ("[    0.000000] Linux version 6.1", []),
    ("[    0.120000] Booting the kernel", []),
])
AMBIGUOUS_TABLE = table_json(
    ["output"],
    [[("$ dmesg | tail -3", "observed")],
     [("[    0.000000] Linux version 6.1", "observed")]],
)


def shared_unknown_candidate(*, seed, page, index, digest="shared-asset", shared_with=None):
    """帶 planner 真實共享訊號的 `KIND_UNKNOWN` 候選。

    `vl_share_key` 的形狀直接照 `figure_candidates` 產出的
    `{"asset_digest", "requested_kind"}`——requested kind 就是候選的 `kind`
    （這裡是 `unknown`），不是 dual pass 解歧後的 kind。
    """
    cand = candidate(kind=figure_extract.KIND_UNKNOWN, seed=seed, page=page,
                     asset_digest=digest, index=index, native_lane=False)
    signals = dict(cand.signals)
    signals["vl_share_key"] = {"asset_digest": digest,
                               "requested_kind": figure_extract.KIND_UNKNOWN}
    signals["vl_share_role"] = "duplicate" if shared_with is not None else "representative"
    signals["tile_plan"] = {"tiles": [{}], "est_tokens": [100]}
    if shared_with is not None:
        signals["vl_shared_with"] = shared_with
    cand.signals = signals
    return cand


@pytest.mark.smoke
def test_duplicate_unknown_candidate_reuses_the_representative_extraction(monkeypatch):
    """契約 §19.3：`unknown` 的重複候選不得重跑 dual pass。

    planner 用**原始** kind（`unknown`）宣告共享，verifier 先前卻把**解歧後**的
    `table`/`terminal` 當 cache key，第二筆永遠命不中——審核實測**兩個 duplicate
    unknown 候選跑了 4 次 VL**，preflight 卻宣稱第二筆是 0。
    """
    spy = VLSpy({"figure_table": AMBIGUOUS_TABLE, "figure_terminal": AMBIGUOUS_LOG})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    representative = shared_unknown_candidate(seed="u-rep", page=2, index=1)
    duplicate = shared_unknown_candidate(seed="u-dup", page=7, index=2, shared_with=0)
    first, second = extract(
        [representative, duplicate],
        {2: page_evidence(page=2), 7: page_evidence(page=7)},
    )

    # 代表跑一次 dual pass（table + terminal 各一次）；duplicate 一次都不跑。
    assert len(spy.calls) == 2, f"duplicate 不得重跑 dual pass（實得 {spy.schema_names()}）"
    assert sorted(spy.schema_names()) == ["figure_table", "figure_terminal"]

    # 解歧結果跟著影像走：duplicate 直接用 resolved kind，不是 planner 的 unknown。
    assert first.kind == second.kind == figure_extract.KIND_TERMINAL
    assert second.evidence["requested_kind"] == figure_extract.KIND_UNKNOWN
    assert "duplicate_asset_reused" in second.reasons
    # kind 歧義的揭露也要跟著過去，否則 duplicate 會看起來比代表更確定
    assert "kind_ambiguous_resolved" in second.reasons
    assert second.evidence["ambiguous"]["winner"] == figure_extract.KIND_TERMINAL
    assert set(second.evidence["ambiguous"]["payloads"]) == {
        figure_extract.KIND_TABLE, figure_extract.KIND_TERMINAL
    }


def test_duplicate_unknown_matches_the_preflight_budget_claim(monkeypatch):
    """**同時**核對 planner 宣稱的預算與真實呼叫數——只驗一邊正是上一輪沒抓到的原因。"""
    figure_candidates = pytest.importorskip("figure_candidates", reason="T3 尚未交付")
    spy = VLSpy({"figure_table": AMBIGUOUS_TABLE, "figure_terminal": AMBIGUOUS_LOG})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    representative = shared_unknown_candidate(seed="b-rep", page=2, index=1)
    duplicate = shared_unknown_candidate(seed="b-dup", page=7, index=2, shared_with=1)

    rep_profile = figure_candidates._vl_profile(representative)
    dup_profile = figure_candidates._vl_profile(duplicate)
    # planner 這一側的宣稱：代表要跑 dual pass，duplicate 是 0
    assert dup_profile["max"] == 0, "planner 宣告共享的候選，預算就是 0"
    assert rep_profile["max"] > 0

    # 只跑代表 → 基準呼叫數
    extract([representative], {2: page_evidence(page=2)})
    baseline = len(spy.calls)
    spy.calls.clear()
    spy._per_schema.clear()

    extract([representative, duplicate],
            {2: page_evidence(page=2), 7: page_evidence(page=7)})
    actual = len(spy.calls)

    # 真實側：duplicate **一次都沒有**多打；預算側：planner 也宣稱 0。兩邊對得上。
    assert actual == baseline == 2, f"duplicate 多打了 {actual - baseline} 次"
    assert actual <= rep_profile["max"] + dup_profile["max"], "實際呼叫不得超出預算"


@pytest.mark.smoke
def test_duplicate_model_input_points_at_a_real_representative(monkeypatch):
    """契約 §19.1：writer 會反查這三個欄位，producer 這側必須填得齊。"""
    spy = VLSpy({"figure_table": REGISTER_TABLE})
    install_vl(monkeypatch, spy)
    pass_probe(monkeypatch)

    first = candidate(seed="ref-a", page=2, asset_digest="same", index=1)
    second = candidate(seed="ref-b", page=7, asset_digest="same", index=2)
    representative, duplicate = extract(
        [first, second], {2: page_evidence(page=2), 7: page_evidence(page=7)})

    assert duplicate.model_input_variant == (
        f"{figure_verify.DUPLICATE_VARIANT_PREFIX}{representative.figure_id}")
    assert duplicate.variants == []
    reference = duplicate.evidence["duplicate_model_input"]
    assert set(reference) == {"figure_id", "model_input_variant", "variants"}
    # 指向代表、不是自己；代表本身不是 duplicate；代表真的有落盤的模型輸入
    assert reference["figure_id"] == representative.figure_id
    assert reference["figure_id"] != duplicate.figure_id
    assert not reference["model_input_variant"].startswith(
        figure_verify.DUPLICATE_VARIANT_PREFIX)
    assert reference["variants"] == representative.variants != []
    assert duplicate.evidence["duplicate_of"] == representative.figure_id


@pytest.mark.parametrize(
    "cached, fragment",
    [
        ({"figure_id": "", "variant": "crop@200dpi", "variants": ["crop@200dpi"]},
         "代表 occurrence 是自己"),
        ({"figure_id": fig_id("self"), "variant": "crop@200dpi",
          "variants": ["crop@200dpi"]}, "代表 occurrence 是自己"),
        ({"figure_id": fig_id("rep"), "variant": "duplicate_of:fig_" + "a" * 16,
          "variants": ["x"]}, "sentinel 不得串接"),
        ({"figure_id": fig_id("rep"), "variant": "crop@200dpi", "variants": []},
         "沒有落盤的模型輸入"),
    ],
)
def test_duplicate_reference_is_fail_loud_when_unusable(cached, fragment):
    """writer 反查不到只會在發布時炸，而那時整份 PDF 已經跑完了。"""
    own = candidate(seed="self")
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        figure_verify._duplicate_reference(cached, own, where="page=1 figure=fig_x")
    assert fragment in str(excinfo.value)


def test_share_key_prefers_the_planner_declaration():
    """兩側必須用**同一把**鍵；自己重建一份看起來一樣的鍵就是上一輪 4 次 VL 的成因。"""
    cand = shared_unknown_candidate(seed="k", page=1, index=1, digest="dig")
    assert figure_verify._share_key(cand, figure_extract.KIND_TERMINAL, where="w") == (
        "dig", figure_extract.KIND_UNKNOWN
    ), "要用 planner 的 requested kind，不是呼叫端當下的 kind"

    plain = candidate(seed="k2", asset_digest="dig2")
    plain.signals = {"native_lane": False}
    assert figure_verify._share_key(plain, figure_extract.KIND_TABLE, where="w") == (
        "dig2", figure_extract.KIND_TABLE)

    drifted = shared_unknown_candidate(seed="k3", page=1, index=1, digest="dig")
    drifted.signals = dict(drifted.signals,
                           vl_share_key={"asset_digest": "other", "requested_kind": "table"})
    with pytest.raises(figure_extract.FigureExtractionError) as excinfo:
        figure_verify._share_key(drifted, figure_extract.KIND_TABLE, where="w")
    assert "不一致" in str(excinfo.value)
