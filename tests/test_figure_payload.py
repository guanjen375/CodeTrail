"""figure_extract 的 canonical model 契約：payload → validator → render → chunk → KB dict。

為什麼這些測試存在（AGENTS.md §2.4 的第二類：無聲失敗風險的契約）
------------------------------------------------------------------
structured figure lane 的整個價值就是「不改寫原文、不錯配欄位、不猜字元」。這三件事
壞掉的時候**不會有任何錯誤訊息**：表格少一欄、log 首行空行被吃掉、`▯` 的位置飄一格、
未驗證的內容拿到 trusted status —— 全都會安靜地變成一個看起來正常的 chunk，然後被
strict query 當成可信數值回答出去。

所以這裡驗的是語義，不是形狀：
- terminal 的行序 / 空行 / 可見空白經 JSON round trip 與多 chunk 切分後**逐位元組**相同。
- register 表的每一格與同一列的身分綁定，兩列只差一個 hex 字元也不得交叉配對。
- validator 擋掉的每一條都對應一種真實的靜默錯配（無證據 fill-down、`▯` 不對齊、
  row 寬度不符、trusted 狀態配不確定內容）。
- `build_figure_chunks` 的批次語義：零部分成功、失敗時 `next_chunk_index` 原封不動。

真 PDF / 真模型完全不參與：本檔只有純函式，整檔跑完是毫秒級。
"""
from __future__ import annotations

import hashlib
import json
import types

import pytest

import config
import figure_extract as fx

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

    empty_header = json.loads(json.dumps(base))
    for column in empty_header["columns"]:
        column["label"] = ""
    with pytest.raises(fx.FigureValidationError, match="header 非空"):
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
    """契約 §10-A / AGENTS.md §4：預設值不得是 import-time snapshot。"""
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
