#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""figure_extract — PDF 圖片結構化抽取的 canonical model 與對外門面。

為什麼要有這個模組
------------------
PDF 裡的 register map / memory map / 終端機 log 一旦走既有的文字入庫路徑，就會被
無聲改寫。實際發生在既有程式碼裡的三處（都不是 bug，是對「散文」正確、對「表格與
log」致命的設計）：

1. `extracted_document.normalize_document_text()` 先 `text.strip()` —— terminal 的
   首行 / 末行空行與外側可見空白當場消失；接著 `normalize_table_content()` 把
   `len(cells) < 3` 的表格列改寫成 `"key: value"`，兩欄表的欄位身分被壓成一個字串。
2. `RAG.split_by_semantic_with_sections()` 對每個 chunk 再 `.strip()` 一次、在空行處
   丟切點、把全空白 chunk 整塊濾掉，單行超長時走 `split_long_paragraph()` 依句號切、
   仍超長就 `sent[i:i+max_chars]` 硬切 —— 長 register 列與長 log 行被攔腰切在 token 中間。
3. overlap 前綴會把前一段尾端複製進下一段（同一列被 BM25 重複計數），`[HEADING]` /
   `[SECTION]` 前綴會把 log 裡以 `#` 開頭的行誤判成標題。

所以 structured lane 不走那條路：**JSON payload 是唯一真相**，Markdown / fence 只是
為了 embedding 與 BM25 的衍生表示，由本模組自己 render、自己切 chunk、自己組 KB
chunk dict。`build_figure_chunks()` 是 structured figure chunk 進 KB 的**唯一產生點**，
狀態機不變式（不確定內容不得取得 trusted status）也在那裡強制。

北極星（workflow §0）：**verified-or-abstain**。看不清就放 `▯` 並標原因，不猜字元；
形狀不合就整份失敗，不做「補一欄」「砍一格」這種讓資料看起來合法的修補。

錯誤訊息的定位資訊（契約 §5）
-----------------------------
`validate_payload()` / `canonicalize_*()` / `render_*()` 這些 API **沒有**檔名與頁碼
參數（它們是 context-free 的純函式），所以它們的訊息只帶 kind 與結構位置
（第幾列、哪個 column_id、第幾行）。完整的「檔名 + 頁碼 + figure_id」由**有 context
的呼叫點**負責：本模組的 `build_figure_chunks()` 自己帶 source / page / figure_id；
`figure_verify` / `figure_review` / `RAG` 需要在自己的錯誤路徑上包一層。

匯入紀律
--------
只依賴標準庫 + `import config`（AGENTS.md §4：動態值一律 `import config`，不得
`from config import X` 取 import-time snapshot）。**不** import pymupdf / numpy /
requests / RAG / knowledge —— 這個檔是整條鏈的地基，smoke 要能在毫秒級跑完。

子模組（`figure_candidates` / `figure_verify` / `figure_review`）的公開名稱在檔尾以
PEP 562 模組 `__getattr__` 延遲 re-export：延到第一次屬性存取才 import，兩個方向的
import 順序都不會產生循環，而且三個子模組還沒交付時 `import figure_extract` 依然可用。
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib
import math
import os
import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

import config

# ============================================================
# 1. 常數（契約 §2.1 / §2.2 / §3 / §4 / §12.2）
# ============================================================
KIND_TABLE = "table"
KIND_TERMINAL = "terminal"
KIND_DIAGRAM = "diagram"
KIND_UNKNOWN = "unknown"
# tuple 而非 set：契約 §2.1 逐字如此，順序也是 schema / 報告的列舉順序。
FIGURE_KINDS = (KIND_TABLE, KIND_TERMINAL, KIND_DIAGRAM)

CELL_STATE_OBSERVED = "observed"
CELL_STATE_INHERITED = "inherited"
CELL_STATE_UNREADABLE = "unreadable"
CELL_STATE_CONFLICT = "conflict"
CELL_STATES = frozenset({
    CELL_STATE_OBSERVED, CELL_STATE_INHERITED,
    CELL_STATE_UNREADABLE, CELL_STATE_CONFLICT,
})
# 模型只准輸出這兩種：`inherited` 需要 geometry 證據、`conflict` 需要兩個通道比對，
# 兩者都不是模型單獨看得出來的（契約 §2.4）。
MODEL_CELL_STATES = frozenset({CELL_STATE_OBSERVED, CELL_STATE_UNREADABLE})

# 看不清的字元佔位符（U+25AF WHITE VERTICAL RECTANGLE）。契約 §12.2 的名稱。
UNREADABLE_GLYPH = "\u25af"

EXTRACTION_COMPLETE = "complete"
EXTRACTION_FAILED = "failed"

VERIF_NATIVE = "native_verified"
VERIF_CORROBORATED = "corroborated"
VERIF_NEEDS_REVIEW = "needs_review"
VERIF_UNVERIFIED = "unverified"
VERIF_HUMAN = "human_verified"
VERIF_LEGACY = "legacy_unverified"

TRUSTED_VERIFICATION = frozenset({VERIF_NATIVE, VERIF_CORROBORATED, VERIF_HUMAN})
FLAGGED_VERIFICATION = frozenset({VERIF_NEEDS_REVIEW, VERIF_UNVERIFIED, VERIF_LEGACY})
# 「最差」排序：aggregate 一律取 min rank，不得由第一個成員覆蓋其他成員。
VERIFICATION_RANK = {
    VERIF_NEEDS_REVIEW: 0,
    VERIF_LEGACY: 1,
    VERIF_UNVERIFIED: 2,
    VERIF_CORROBORATED: 3,
    VERIF_NATIVE: 4,
    VERIF_HUMAN: 5,
}

ORIGIN_BY_KIND = {
    KIND_TABLE: "figure_table",
    KIND_TERMINAL: "figure_terminal",
    KIND_DIAGRAM: "figure_diagram",
}
FIGURE_ORIGINS = frozenset(ORIGIN_BY_KIND.values())

# 舊 VL origin（既有揭露邏輯）。契約 §4 逐字寫成 set literal，`knowledge.py` 另有一份
# 本地副本並以 smoke 比對兩邊相等（契約 §13.3），所以這裡的名稱與內容都不能動。
VL_ORIGINS = {"image", "screenshot", "diagram"}

# 送模型時 `json_schema.name` 用的名稱。目前字串與 ORIGIN_BY_KIND 相同但語意不同
# （一個是 llama.cpp 的 schema 名，一個是 KB 的 origin），**刻意不共用常數**：
# 共用的話其中一邊改名會把另一邊連帶改掉，而那是無聲的。
SCHEMA_NAME_BY_KIND = {
    KIND_TABLE: "figure_table",
    KIND_TERMINAL: "figure_terminal",
    KIND_DIAGRAM: "figure_diagram",
}

# figure_id 的凍結格式（契約 §2.5）。`build_figure_chunks` 用它擋掉佔位字串進 KB。
FIGURE_ID_RE = re.compile(r"fig_[0-9a-f]{16}")
# asset_digest 的凍結格式（契約 §2.5：原始 asset bytes 或 candidate signature 的 sha256）。
SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")


# ============================================================
# 2. 例外階層（契約 §5）
# ============================================================
class FigureError(RuntimeError):
    """figure 全鏈路的基底例外。

    訊息一律帶得出來的定位資訊；context-free 的純函式（validator / renderer）只帶
    kind 與結構位置，檔名 / 頁碼 / figure_id 由有 context 的呼叫點補（見檔頭）。
    """


class FigureValidationError(FigureError):
    """schema / validator / canonicalize / metadata 不合格。零寫入。"""


class FigureBudgetError(FigureError):
    """preflight 或送出前複查超過上限。此時尚未呼 VL、未算 embedding、未動 KB。"""


class FigureCapabilityError(FigureError):
    """capability probe 未通過。KB mutation 前 fail-loud，不以「OpenAI 相容」推定品質。"""


class FigureExtractionError(FigureError):
    """重試後仍失敗 → 整份 PDF 零寫入（不得以自由文字冒充成功入庫）。

    `.results` / `.failed` 由 `figure_verify` 掛上（契約 §12.2），供
    `figure_review.write_run_artifacts(failed=True)` 取用。
    """

    def __init__(self, message: str, *, results=None, failed=None):
        super().__init__(message)
        self.results = list(results or [])
        self.failed = list(failed or [])


class FigureReviewError(FigureError):
    """review artifact / 人工 fix 交易（路徑、revision、schema）。失敗時舊資料保持可用。"""


# 契約 §5 要求錯誤訊息帶「檔名、頁碼、figure_id、原因、底層原始錯誤」。
# `validate_payload()` / `canonicalize_*()` / `render_*()` / `chunk_payload()` 是
# context-free 純函式，**簽章上就拿不到**前三項（Gate 0 凍結了它們的 signature），所以
# 它們的訊息一律以這個**固定 sentinel** 開頭。sentinel 的用途有三個：
#   1. 誠實：明說這三欄「未知」，而不是讓人以為「不適用」。
#   2. 可機器判斷：下游 (`figure_verify` / `figure_review` / `RAG`) 只要看訊息開頭就知道
#      要不要補 context，不必做字串猜測。
#   3. 可替換：`strip_locator()` 把 sentinel 去掉，呼叫端換上真值再重拋。
# `build_figure_chunks()` 有 context，會自己把 sentinel 換成實際的 source/page/figure_id。
LOCATOR_UNKNOWN = "[figure file=? page=? figure_id=?]"


def strip_locator(exc: BaseException) -> str:
    """去掉 `LOCATOR_UNKNOWN` 前綴，讓有 context 的呼叫端換上真值後重拋。"""
    message = str(exc)
    if message.startswith(LOCATOR_UNKNOWN):
        return message[len(LOCATOR_UNKNOWN):].lstrip()
    return message


@contextlib.contextmanager
def _locator_sentinel():
    """context-free 公開 API 的訊息前綴（冪等：巢狀呼叫不會疊兩層）。"""
    try:
        yield
    except FigureError as exc:
        message = str(exc)
        if message.startswith(LOCATOR_UNKNOWN):
            raise
        raise type(exc)(f"{LOCATOR_UNKNOWN} {message}") from exc


# ============================================================
# 3. 內部小工具
# ============================================================
def _is_int(value) -> bool:
    """嚴格 int：`bool` 是 `int` 的子類別，被當成 index / revision 會是無聲錯誤。"""
    return type(value) is int


def _ordered_unique(items: Iterable[str]) -> list[str]:
    """去重且保序（dict.fromkeys 語意）；非 str 元素一律拒絕。"""
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            raise FigureValidationError(f"reasons/details 元素必須是 str，收到 {type(item).__name__}: {item!r}")
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _require_exact_keys(obj, keys: Iterable[str], what: str) -> None:
    """dict 的 key 集合必須恰好等於 keys。

    多出來的 key 一律拒絕而不是靜默丟棄：llama.cpp 的 grammar 可能忽略
    `additionalProperties:false`，模型多吐一欄時若我們默默丟掉，重建出來的 canonical
    payload 會「看起來驗證成功」，而那正是本輪要消滅的無聲失敗。
    """
    if not isinstance(obj, dict):
        raise FigureValidationError(f"{what} 必須是 dict，收到 {type(obj).__name__}")
    expected = set(keys)
    actual = set(obj)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise FigureValidationError(
            f"{what} 的 key 不符：缺 {missing}、多 {extra}（期望恰好 {sorted(expected)}）"
        )


def _require_str(value, what: str) -> str:
    if not isinstance(value, str):
        raise FigureValidationError(f"{what} 必須是 str，收到 {type(value).__name__}: {value!r}")
    return value


def _require_str_list(value, what: str) -> list[str]:
    if not isinstance(value, list):
        raise FigureValidationError(f"{what} 必須是 list，收到 {type(value).__name__}")
    for i, item in enumerate(value):
        _require_str(item, f"{what}[{i}]")
    return list(value)


def _bbox_close(a, b) -> bool:
    """兩個 bbox 是否實質相同（float 不做 `==`：上游經過縮放/旋轉換算會有尾差）。"""
    return all(math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-6) for x, y in zip(a, b))


def _require_bbox(value, what: str) -> list[float]:
    """4 個有限數字，且 x0 <= x1、y0 <= y1。

    NaN / Inf 或反向矩形會產生「看起來穩定」但無法重現的 figure_id 與無意義的
    crop 範圍，一律 fail-loud。
    """
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise FigureValidationError(f"{what} 必須是 4 個座標，收到 {value!r}")
    coords = []
    for i, raw in enumerate(value):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise FigureValidationError(f"{what}[{i}] 必須是數字，收到 {raw!r}")
        number = float(raw)
        if not math.isfinite(number):
            raise FigureValidationError(f"{what}[{i}] 必須是有限數字，收到 {raw!r}")
        coords.append(number)
    if coords[0] > coords[2] or coords[1] > coords[3]:
        raise FigureValidationError(f"{what} 不是合法矩形（x0>x1 或 y0>y1）：{value!r}")
    return coords


# ============================================================
# 4. 送模型的 JSON Schema（契約 §2.4）
# ============================================================
# 模型**不**產生 row_index / line_index / column_id / inherited_from_row：那些由 code
# 指派與驗證（行序不能變成模型的自由裁量；fill-down 需要 geometry 證據）。
_MODEL_SCHEMAS = {
    KIND_TABLE: {
        "type": "object", "additionalProperties": False,
        "required": ["columns", "rows", "footnotes"],
        "properties": {
            "columns": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["label"],
                "properties": {"label": {"type": "string"}}}},
            "rows": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["cells"],
                "properties": {"cells": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["text", "state"],
                    "properties": {
                        "text": {"type": "string"},
                        "state": {"type": "string", "enum": ["observed", "unreadable"]}}}}}}},
            "footnotes": {"type": "array", "items": {"type": "string"}},
        },
    },
    KIND_TERMINAL: {
        "type": "object", "additionalProperties": False,
        "required": ["lines"],
        "properties": {
            "lines": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["text", "uncertain_spans"],
                "properties": {
                    "text": {"type": "string"},
                    "uncertain_spans": {"type": "array", "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["start", "end", "alternatives"],
                        "properties": {
                            "start": {"type": "integer"},
                            "end": {"type": "integer"},
                            "alternatives": {"type": "array", "items": {"type": "string"}}}}}}}},
        },
    },
    KIND_DIAGRAM: {
        "type": "object", "additionalProperties": False,
        "required": ["title", "labels", "components", "relations", "values"],
        "properties": {
            "title": {"type": "string"},
            "labels": {"type": "array", "items": {"type": "string"}},
            "components": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["name", "desc"],
                "properties": {"name": {"type": "string"}, "desc": {"type": "string"}}}},
            "relations": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["src", "dst", "desc"],
                "properties": {"src": {"type": "string"}, "dst": {"type": "string"},
                               "desc": {"type": "string"}}}},
            "values": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["key", "value", "desc"],
                "properties": {"key": {"type": "string"}, "value": {"type": "string"},
                               "desc": {"type": "string"}}}},
        },
    },
}


def _require_kind(kind: str, *, allow: Iterable[str] = FIGURE_KINDS) -> str:
    # 先確認是 str：kind 可能來自外部 JSON，若是 list/dict，`in` 對 set/dict 會拋
    # TypeError 而不是契約 §5 指定的 FigureValidationError。
    if not isinstance(kind, str):
        raise FigureValidationError(f"kind 必須是 str，收到 {type(kind).__name__}: {kind!r}")
    if kind not in allow:
        raise FigureValidationError(
            f"kind={kind!r} 不是可用的 figure kind（可用：{sorted(allow)}；"
            f"{KIND_UNKNOWN!r} 只是候選階段的 table/terminal 分數接近，不可入庫）"
        )
    return kind


def model_json_schema(kind: str) -> dict:
    """送模型的 JSON Schema（每層 type/properties/items/required/additionalProperties:False）。

    每次回傳**全新的 deep copy**：呼叫端（llama client / extractor）常會把它塞進
    payload 再就地改，共用同一個 dict 會讓下一次呼叫拿到被污染的 schema。

    ⚠️ llama.cpp 的 grammar 只支援 JSON Schema 的子集，`enum` /
    `additionalProperties:false` 等 constraint 可能被略過。**「JSON 可解析」不等於正確**，
    拿到模型輸出後一律要跑 `canonicalize_*()` + `validate_payload()`。
    """
    _require_kind(kind)
    return copy.deepcopy(_MODEL_SCHEMAS[kind])


def response_format_for(kind: str) -> dict:
    """完整 nested wrapper：`{"type":"json_schema","json_schema":{name,strict,schema}}`。"""
    _require_kind(kind)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": SCHEMA_NAME_BY_KIND[kind],
            "strict": True,
            "schema": model_json_schema(kind),
        },
    }


def _model_keys(node: dict) -> list[str]:
    """從 schema 節點推出模型端物件的合法 key 集合。

    刻意從 schema 推、不另外寫一份常數：兩份會漂移，而漂移的症狀是「schema 改了但
    canonicalize 還在接受舊欄位」這種靜默通過。
    """
    return list(node["properties"])


# ============================================================
# 5. validator（契約 §2.3 + §10-C）
# ============================================================
def _validate_table(payload: dict) -> None:
    _require_exact_keys(payload, ("kind", "columns", "rows", "footnotes"), "table payload")

    columns = payload["columns"]
    if not isinstance(columns, list) or not columns:
        raise FigureValidationError("table.columns 必須是非空 list（沒有欄位就沒有欄位身分可言）")
    seen_ids: set[str] = set()
    for i, column in enumerate(columns):
        _require_exact_keys(column, ("column_id", "label", "role"), f"table.columns[{i}]")
        column_id = _require_str(column["column_id"], f"table.columns[{i}].column_id")
        if not column_id:
            raise FigureValidationError(f"table.columns[{i}].column_id 不得為空字串")
        if column_id in seen_ids:
            raise FigureValidationError(
                f"table.columns[{i}].column_id={column_id!r} 重複——欄位身分必須唯一，"
                "否則同一列的兩格會指向同一欄，配對就失去意義"
            )
        seen_ids.add(column_id)
        _require_str(column["label"], f"table.columns[{i}].label")
        role = column["role"]
        if role is not None:
            _require_str(role, f"table.columns[{i}].role")
    if not any(column["label"] for column in columns):
        raise FigureValidationError("table.columns 的 label 全為空字串（契約 §2.3：header 非空）")

    rows = payload["rows"]
    if not isinstance(rows, list):
        raise FigureValidationError(f"table.rows 必須是 list，收到 {type(rows).__name__}")
    previous = 0
    seen_rows: set[int] = set()
    for position, row in enumerate(rows):
        _require_exact_keys(row, ("row_index", "cells"), f"table.rows[{position}]")
        row_index = row["row_index"]
        if not _is_int(row_index):
            raise FigureValidationError(
                f"table.rows[{position}].row_index 必須是 int（bool 不算），收到 {row_index!r}"
            )
        if position == 0 and row_index != 1:
            raise FigureValidationError(
                f"table.rows[0].row_index 必須是 1，收到 {row_index}（契約 §2.3：從 1 起）"
            )
        if row_index <= previous:
            raise FigureValidationError(
                f"table.rows[{position}].row_index={row_index} 沒有嚴格遞增（前一個是 {previous}）"
            )
        previous = row_index

        cells = row["cells"]
        if not isinstance(cells, list):
            raise FigureValidationError(f"table.rows[{position}].cells 必須是 list")
        if len(cells) != len(columns):
            raise FigureValidationError(
                f"table.rows[{position}](row_index={row_index}) 有 {len(cells)} 格，"
                f"但表有 {len(columns)} 欄——不補不砍，寬度不對就是不合格"
            )
        for j, cell in enumerate(cells):
            where = f"table.rows[{position}](row_index={row_index}).cells[{j}]"
            _require_exact_keys(cell, ("column_id", "text", "state", "inherited_from_row"), where)
            if cell["column_id"] != columns[j]["column_id"]:
                raise FigureValidationError(
                    f"{where}.column_id={cell['column_id']!r} 與 columns[{j}]="
                    f"{columns[j]['column_id']!r} 位置不對齊"
                )
            text = _require_str(cell["text"], f"{where}.text")
            # 先驗型別再做 membership：llama.cpp 可能忽略 schema 而吐出 list/dict，
            # 直接對 frozenset 做 `in` 會拋 TypeError（不可 hash），繞過契約 §5 的例外語意。
            state = _require_str(cell["state"], f"{where}.state")
            if state not in CELL_STATES:
                raise FigureValidationError(f"{where}.state={state!r} 不是合法 state（{sorted(CELL_STATES)}）")
            # 契約 §10-C：cell 放了 ▯ 就代表那格看不清，state 必須誠實反映。
            if UNREADABLE_GLYPH in text and state not in (CELL_STATE_UNREADABLE, CELL_STATE_CONFLICT):
                raise FigureValidationError(
                    f"{where} 的 text 含 {UNREADABLE_GLYPH!r} 卻宣稱 state={state!r}；"
                    f"必須是 {CELL_STATE_UNREADABLE!r} 或 {CELL_STATE_CONFLICT!r}"
                )
            inherited = cell["inherited_from_row"]
            if state == CELL_STATE_INHERITED:
                if not _is_int(inherited):
                    raise FigureValidationError(
                        f"{where}.state=inherited 但 inherited_from_row={inherited!r} 不是 int；"
                        "fill-down 必須指得出來源列"
                    )
                if inherited >= row_index:
                    raise FigureValidationError(
                        f"{where}.inherited_from_row={inherited} 必須小於本列 row_index={row_index}"
                    )
                if inherited not in seen_rows:
                    raise FigureValidationError(
                        f"{where}.inherited_from_row={inherited} 指向不存在的列——"
                        "無效的 evidence reference 會產生看似合法的 fill-down 配對"
                    )
            elif inherited is not None:
                raise FigureValidationError(
                    f"{where}.state={state!r} 卻帶 inherited_from_row={inherited!r}；"
                    "沒有 rowspan 證據的空格必須保持空（保留/未實作/不適用本來就該是空）"
                )
        seen_rows.add(row_index)

    _require_str_list(payload["footnotes"], "table.footnotes")


def _validate_terminal(payload: dict) -> None:
    _require_exact_keys(payload, ("kind", "lines"), "terminal payload")
    lines = payload["lines"]
    if not isinstance(lines, list):
        raise FigureValidationError(f"terminal.lines 必須是 list，收到 {type(lines).__name__}")
    previous = 0
    for position, line in enumerate(lines):
        where = f"terminal.lines[{position}]"
        _require_exact_keys(line, ("line_index", "text", "uncertain_spans"), where)
        line_index = line["line_index"]
        if not _is_int(line_index):
            raise FigureValidationError(f"{where}.line_index 必須是 int（bool 不算），收到 {line_index!r}")
        if position == 0 and line_index != 1:
            raise FigureValidationError(
                f"terminal.lines[0].line_index 必須是 1，收到 {line_index}（契約 §2.3：從 1 起）"
            )
        if line_index <= previous:
            raise FigureValidationError(
                f"{where}.line_index={line_index} 沒有嚴格遞增（前一個是 {previous}）"
            )
        previous = line_index

        text = _require_str(line["text"], f"{where}.text")
        if "\n" in text or "\r" in text:
            raise FigureValidationError(
                f"{where}(line_index={line_index}).text 含 \\n 或 \\r；"
                "一個 line 必須恰是一個視覺行，換行由 line_index 表示"
            )

        spans = line["uncertain_spans"]
        if not isinstance(spans, list):
            raise FigureValidationError(f"{where}.uncertain_spans 必須是 list")
        bounds: list[tuple[int, int]] = []
        for k, span in enumerate(spans):
            span_where = f"{where}(line_index={line_index}).uncertain_spans[{k}]"
            _require_exact_keys(span, ("start", "end", "alternatives"), span_where)
            start, end = span["start"], span["end"]
            if not _is_int(start) or not _is_int(end):
                raise FigureValidationError(f"{span_where} 的 start/end 必須是 int，收到 {start!r}/{end!r}")
            if not (0 <= start < end <= len(text)):
                raise FigureValidationError(
                    f"{span_where} 範圍不合法：需要 0 <= start < end <= len(text)={len(text)}，"
                    f"收到 start={start}, end={end}"
                )
            alternatives = span["alternatives"]
            if not isinstance(alternatives, list) or not alternatives:
                raise FigureValidationError(f"{span_where}.alternatives 必須是非空 list")
            _require_str_list(alternatives, f"{span_where}.alternatives")
            segment = text[start:end]
            if any(ch != UNREADABLE_GLYPH for ch in segment):
                raise FigureValidationError(
                    f"{span_where} 標了不確定，但 text[{start}:{end}]={segment!r} 不全是 "
                    f"{UNREADABLE_GLYPH!r}——候選只能進 alternatives，不得混進逐字正文"
                )
            bounds.append((start, end))
        bounds.sort()
        for (s1, e1), (s2, _e2) in zip(bounds, bounds[1:]):
            if s2 < e1:
                raise FigureValidationError(
                    f"{where}(line_index={line_index}) 的 uncertain_spans 重疊："
                    f"({s1},{e1}) 與 ({s2},{_e2})"
                )


def _validate_diagram(payload: dict) -> None:
    _require_exact_keys(
        payload, ("kind", "title", "labels", "components", "relations", "values"), "diagram payload"
    )
    _require_str(payload["title"], "diagram.title")
    _require_str_list(payload["labels"], "diagram.labels")
    for field, keys in (("components", ("name", "desc")),
                        ("relations", ("src", "dst", "desc")),
                        ("values", ("key", "value", "desc"))):
        items = payload[field]
        if not isinstance(items, list):
            raise FigureValidationError(f"diagram.{field} 必須是 list，收到 {type(items).__name__}")
        for i, item in enumerate(items):
            where = f"diagram.{field}[{i}]"
            _require_exact_keys(item, keys, where)
            for key in keys:
                _require_str(item[key], f"{where}.{key}")


_VALIDATORS = {
    KIND_TABLE: _validate_table,
    KIND_TERMINAL: _validate_terminal,
    KIND_DIAGRAM: _validate_diagram,
}


def validate_payload(payload: dict, kind: str) -> None:
    """canonical payload 的**外部** validator；不合格 raise `FigureValidationError`。

    這裡刻意不依賴 llama.cpp 的 grammar：grammar 只支援 JSON Schema 子集，
    `enum` / `additionalProperties` 之類的 constraint 可能被整個略過，所以
    「模型回了合法 JSON」不代表這一份 payload 可以入庫。

    驗的是契約 §2.3 的每一條，外加 §10-C（cell 放了 `▯` 就必須是 unreadable/conflict）。
    context-free：訊息只帶 kind 與結構位置，檔名/頁碼由呼叫端補（見檔頭）。
    """
    with _locator_sentinel():
        return _validate_payload_impl(payload, kind)


def _validate_payload_impl(payload: dict, kind: str) -> None:
    """實作；context-free 訊息的 `LOCATOR_UNKNOWN` 前綴由 `validate_payload()` 統一補上。"""
    _require_kind(kind)
    if not isinstance(payload, dict):
        raise FigureValidationError(f"payload 必須是 dict，收到 {type(payload).__name__}")
    actual_kind = payload.get("kind")
    if actual_kind != kind:
        raise FigureValidationError(
            f"payload['kind']={actual_kind!r} 與要求的 kind={kind!r} 不符——"
            "kind 歧義是靜默錯配的入口，不接受推定"
        )
    _VALIDATORS[kind](payload)


# ============================================================
# 6. canonicalize（model 物件 → canonical payload；契約 §2.4）
# ============================================================
def _require_model_object(obj, schema_node: dict, what: str) -> dict:
    """模型端物件必須恰好帶 schema 宣告的 key（巢狀每一層都驗）。"""
    _require_exact_keys(obj, _model_keys(schema_node), what)
    return obj


def canonicalize_table(model_obj: dict) -> dict:
    """model 物件 → canonical table payload。

    `column_id` 由 code 指派成 `c1..cN`、`row_index` 由 1 遞增、`inherited_from_row`
    一律 `None`（fill-down 需要 geometry 證據，不是模型說了算）、`role` 一律 `None`
    （semantic role 另存，不得覆蓋 label）。

    **不補不砍**：row 的 cells 長度不等於欄數就是不合格。補一格會憑空造出一個值，
    砍一格會讓後面所有欄位往前錯位——兩種都是靜默錯配。
    """
    with _locator_sentinel():
        return _canonicalize_table_impl(model_obj)


def _canonicalize_table_impl(model_obj: dict) -> dict:
    """實作；context-free 訊息的 `LOCATOR_UNKNOWN` 前綴由 `canonicalize_table()` 統一補上。"""
    schema = _MODEL_SCHEMAS[KIND_TABLE]["properties"]
    _require_model_object(model_obj, _MODEL_SCHEMAS[KIND_TABLE], "model table")

    raw_columns = model_obj["columns"]
    if not isinstance(raw_columns, list) or not raw_columns:
        raise FigureValidationError("model table.columns 必須是非空 list")
    columns = []
    for i, column in enumerate(raw_columns):
        _require_model_object(column, schema["columns"]["items"], f"model table.columns[{i}]")
        columns.append({
            "column_id": f"c{i + 1}",
            "label": _require_str(column["label"], f"model table.columns[{i}].label"),
            "role": None,
        })

    raw_rows = model_obj["rows"]
    if not isinstance(raw_rows, list):
        raise FigureValidationError("model table.rows 必須是 list")
    rows = []
    for position, row in enumerate(raw_rows):
        _require_model_object(row, schema["rows"]["items"], f"model table.rows[{position}]")
        raw_cells = row["cells"]
        if not isinstance(raw_cells, list):
            raise FigureValidationError(f"model table.rows[{position}].cells 必須是 list")
        if len(raw_cells) != len(columns):
            raise FigureValidationError(
                f"model table.rows[{position}] 有 {len(raw_cells)} 格，但表有 {len(columns)} 欄"
                "——不補不砍"
            )
        cells = []
        for j, cell in enumerate(raw_cells):
            where = f"model table.rows[{position}].cells[{j}]"
            _require_model_object(cell, schema["rows"]["items"]["properties"]["cells"]["items"], where)
            state = _require_str(cell["state"], f"{where}.state")
            if state not in MODEL_CELL_STATES:
                raise FigureValidationError(
                    f"{where}.state={state!r} 不在模型可用範圍 {sorted(MODEL_CELL_STATES)}；"
                    "inherited 需要 geometry 證據、conflict 需要兩個通道比對，都不是模型能自稱的"
                )
            cells.append({
                "column_id": columns[j]["column_id"],
                "text": _require_str(cell["text"], f"{where}.text"),
                "state": state,
                "inherited_from_row": None,
            })
        rows.append({"row_index": position + 1, "cells": cells})

    payload = {
        "kind": KIND_TABLE,
        "columns": columns,
        "rows": rows,
        "footnotes": _require_str_list(model_obj["footnotes"], "model table.footnotes"),
    }
    validate_payload(payload, KIND_TABLE)
    return payload


def canonicalize_terminal(model_obj: dict) -> dict:
    """model 物件 → canonical terminal payload。

    `line_index` 由 code 從 1 遞增指派。`text` 含 `\\n` / `\\r` 一律不合格：**不得**
    自行拆行——拆了之後行序就變成模型的自由裁量，而行序正是 log 的語義。
    """
    with _locator_sentinel():
        return _canonicalize_terminal_impl(model_obj)


def _canonicalize_terminal_impl(model_obj: dict) -> dict:
    """實作；context-free 訊息的 `LOCATOR_UNKNOWN` 前綴由 `canonicalize_terminal()` 統一補上。"""
    schema = _MODEL_SCHEMAS[KIND_TERMINAL]["properties"]
    _require_model_object(model_obj, _MODEL_SCHEMAS[KIND_TERMINAL], "model terminal")

    raw_lines = model_obj["lines"]
    if not isinstance(raw_lines, list):
        raise FigureValidationError("model terminal.lines 必須是 list")
    lines = []
    for position, line in enumerate(raw_lines):
        where = f"model terminal.lines[{position}]"
        _require_model_object(line, schema["lines"]["items"], where)
        text = _require_str(line["text"], f"{where}.text")
        if "\n" in text or "\r" in text:
            raise FigureValidationError(
                f"{where}.text 含 \\n 或 \\r；一個 line 必須恰是一個視覺行，不得自行拆行"
            )
        raw_spans = line["uncertain_spans"]
        if not isinstance(raw_spans, list):
            raise FigureValidationError(f"{where}.uncertain_spans 必須是 list")
        spans = []
        for k, span in enumerate(raw_spans):
            span_where = f"{where}.uncertain_spans[{k}]"
            _require_model_object(
                span, schema["lines"]["items"]["properties"]["uncertain_spans"]["items"], span_where
            )
            spans.append({
                "start": span["start"],
                "end": span["end"],
                "alternatives": span["alternatives"],
            })
        lines.append({"line_index": position + 1, "text": text, "uncertain_spans": spans})

    payload = {"kind": KIND_TERMINAL, "lines": lines}
    validate_payload(payload, KIND_TERMINAL)
    return payload


def canonicalize_diagram(model_obj: dict) -> dict:
    """model 物件 → canonical diagram payload（補 `kind`，其餘原樣）。

    本輪沒有自動生產者（契約 §13.1：raster / picture 候選維持既有 legacy VL lane），
    但 `review_figures(action="fix")` 的人工修正吃這個 kind，所以 schema / validator /
    renderer 一律完整實作。
    """
    with _locator_sentinel():
        return _canonicalize_diagram_impl(model_obj)


def _canonicalize_diagram_impl(model_obj: dict) -> dict:
    """實作；context-free 訊息的 `LOCATOR_UNKNOWN` 前綴由 `canonicalize_diagram()` 統一補上。"""
    node = _MODEL_SCHEMAS[KIND_DIAGRAM]
    _require_model_object(model_obj, node, "model diagram")
    schema = node["properties"]
    payload = {"kind": KIND_DIAGRAM, "title": model_obj["title"], "labels": model_obj["labels"]}
    for field, keys in (("components", ("name", "desc")),
                        ("relations", ("src", "dst", "desc")),
                        ("values", ("key", "value", "desc"))):
        items = model_obj[field]
        if not isinstance(items, list):
            raise FigureValidationError(f"model diagram.{field} 必須是 list")
        collected = []
        for i, item in enumerate(items):
            _require_model_object(item, schema[field]["items"], f"model diagram.{field}[{i}]")
            collected.append({key: item[key] for key in keys})
        payload[field] = collected
    validate_payload(payload, KIND_DIAGRAM)
    return payload


# ============================================================
# 7. 狀態機（契約 §3 / §10-E）
# ============================================================
def worst_verification(statuses: Iterable[str]) -> str:
    """一組 verification_status 取**最差**（min rank）；空 → `legacy_unverified`。

    未知字串一律當成比 `needs_review` 更差並回 `needs_review`，**不 raise**：這個函式
    會被 retrieval / merge 在舊 KB 資料上呼叫，raise 會打斷查詢；但絕不能讓沒見過的
    字串取得信任（fail-safe 方向只有一個）。
    """
    best_rank = None
    result = VERIF_LEGACY
    empty = True
    for status in statuses:
        empty = False
        # 非 str（含 list/dict 這種不可 hash 的值）不能拿去查 dict，也絕不能取得信任
        if not isinstance(status, str):
            return VERIF_NEEDS_REVIEW
        rank = VERIFICATION_RANK.get(status)
        if rank is None:
            return VERIF_NEEDS_REVIEW
        if best_rank is None or rank < best_rank:
            best_rank = rank
            result = status
    if empty:
        return VERIF_LEGACY
    return result


def aggregate_status(members: list[dict]) -> tuple[str, str, list[str]]:
    """合併一群 chunk/figure 的狀態 → `(extraction_status, verification_status, reasons)`。

    任一成員不是 `complete`（含缺欄位）→ `failed`；verification 取最差；reasons 是所有
    成員 reasons 的去重保序聯集。空 members → `(failed, legacy_unverified, [])`：
    「什麼都沒有」不能宣稱 complete。

    `reason_details` 因為凍結的三值 signature 不在回傳裡，改用
    `aggregate_reason_details()`（契約 §10-E）。
    """
    if not members:
        return EXTRACTION_FAILED, VERIF_LEGACY, []
    extraction = EXTRACTION_COMPLETE
    for member in members:
        if member.get("extraction_status") != EXTRACTION_COMPLETE:
            extraction = EXTRACTION_FAILED
            break
    verification = worst_verification(member.get("verification_status") for member in members)
    reasons: list[str] = []
    for member in members:
        reasons.extend(member.get("reasons") or [])
    return extraction, verification, _ordered_unique(reasons)


def aggregate_reason_details(members: list[dict]) -> list[str]:
    """所有成員 `reason_details` 的去重保序聯集（契約 §10-E；T5/T6 統一呼叫這個）。"""
    details: list[str] = []
    for member in members or []:
        details.extend(member.get("reason_details") or [])
    return _ordered_unique(details)


def read_native_lane(candidate) -> bool:
    """`candidate.signals["native_lane"]` 的**唯一** reader（契約 §15.1 / §17.4）。

    lane 決定「這個候選要不要呼叫 VL」，而它有三個消費端：planner 的 preflight 預算
    （`figure_candidates._vl_profile`）、`RAG.py` 的 capability probe 判定，以及
    verifier 的 lane 選擇（`figure_verify._lane_for`）。三邊各寫一份判定的結果是**型別
    語義不一致**：`RAG` 對非 `bool` fail-loud，另外兩邊卻用 truthiness，於是
    `"false"` 在 `RAG` 是錯誤、在 verifier 卻是 native lane —— 那條路徑會**跳過 VL
    capability probe**，而公開的 `extract_document_figures` 被直接呼叫時走得到。

    所以判定只留這一份，而且是**精確 bool**：
    - `signals` 不是 dict、缺 `native_lane` key，或值不是 `isinstance(v, bool)`
      （`0` / `1` / `1.0` / `"true"` / `"false"` / `None` 全部不算）→ `FigureExtractionError`。
    - 不猜預設值：猜一個等於把「三處不一致」換成「一處無聲不一致」。

    訊息帶 page 與 figure_id（契約 §5 的定位要求；candidate 身上就有這兩項，所以這裡
    不需要 `LOCATOR_UNKNOWN` sentinel）。
    """
    where = (f"page={getattr(candidate, 'page', '?')} "
             f"figure={getattr(candidate, 'figure_id', '?')}")
    signals = getattr(candidate, "signals", None)
    if not isinstance(signals, dict):
        raise FigureExtractionError(
            f"{where}: candidate.signals 不是 dict（收到 {type(signals).__name__}）——"
            "lane 判定只有一個真相，讀不到就不得由呼叫端自己猜（契約 §15.1）"
        )
    if "native_lane" not in signals:
        raise FigureExtractionError(
            f"{where}: candidate.signals 缺少 'native_lane'——lane 判定只有一個真相，"
            "缺了不得由呼叫端自己猜（契約 §15.1）"
        )
    value = signals["native_lane"]
    if not isinstance(value, bool):
        raise FigureExtractionError(
            f"{where}: signals['native_lane']={value!r} 不是 bool"
            f"（型別 {type(value).__name__}）。truthiness 轉型會讓 lane 判定靜默反過來——"
            "例如 \"false\" 會被當成 native lane 而跳過 VL capability probe"
        )
    return value


# ============================================================
# 7b. 共享的 Variant 守門員（契約 §21.1）
# ============================================================
# 為什麼放在門面：`Variant` 這個凍結介面有**三個消費端**——`figure_verify`（送 VL 前）、
# `RAG`（決定「這張是不是完整原圖」）、`figure_review`（寫 manifest / 發布 artifact），
# 外加 `figure_candidates` 這個產生端。前四輪終審每次都只有被點名的一兩端收緊自己那份
# 檢查，第三端維持寬鬆，於是繞道永遠存在（契約 §21.5）。所以判定只留這一份，四方都呼叫它。
#
# 這裡**禁止任何 coercion**：不得 `int(...)`、不得 `getattr(x, name, 預設值)`。
# 原因是實測過的靜默通過——`tile_total=True`、`"1"`/`"0"`、`1.9`/`0.9` 經 `int()` 之後
# 全都被截成合法的 `(1, 0)`，而那道檢查發生在 VL 呼叫**之前**：等 RAG 或 writer 稍後
# 拒絕時，VL 的錢已經花掉了。
_VARIANT_FIELDS = (
    "figure_id", "variant_id", "png", "digest", "width", "height",
    "bbox", "tile_index", "tile_total", "overlap_px", "est_image_tokens", "mime",
)

_MISSING = object()

# `is_full_image` 的 bbox 容忍值：與 `_bbox_close` 同一組（rel_tol=1e-9, abs_tol=1e-6）。
# 絕對容忍取 1e-6 pt —— PDF 座標以 point 為單位，1e-6 pt 遠小於任何可見差異，
# 但足以吸收 rotation / cropbox 換算與 float 往返的尾差。
VARIANT_BBOX_REL_TOL = 1e-9
VARIANT_BBOX_ABS_TOL = 1e-6


def _variant_field(variant, name: str, where: str):
    """取一個 Variant 欄位；缺就 fail-loud（**不給預設值**）。"""
    if isinstance(variant, dict):
        value = variant.get(name, _MISSING)
    else:
        value = getattr(variant, name, _MISSING)
    if value is _MISSING:
        raise FigureExtractionError(
            f"{where}: Variant 缺少欄位 {name!r}（契約 §6.3 的無預設欄位一個都不能少；"
            "缺欄位的 fixture 正是這條接縫連續四輪沒被抓到的原因）"
        )
    return value


def _variant_int(value, name: str, where: str, *, minimum: int) -> int:
    """精確整數 + 下界。`bool` 是 `int` 的子類別，必須明確排除。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise FigureExtractionError(
            f"{where}: Variant.{name}={value!r} 不是整數（型別 {type(value).__name__}）。"
            "這裡不做 int() 轉型——`True` / `\"1\"` / `1.9` 被截成合法值就是靜默通過"
        )
    if value < minimum:
        raise FigureExtractionError(
            f"{where}: Variant.{name}={value} 必須 >= {minimum}"
        )
    return value


def _variant_nonempty_str(value, name: str, where: str) -> str:
    if not isinstance(value, str):
        raise FigureExtractionError(
            f"{where}: Variant.{name}={value!r} 必須是 str（型別 {type(value).__name__}）"
        )
    if not value:
        raise FigureExtractionError(f"{where}: Variant.{name} 不得為空字串")
    return value


def _variant_bbox(value, name: str, where: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise FigureExtractionError(
            f"{where}: Variant.{name}={value!r} 必須是 4 個座標"
        )
    coords: list[float] = []
    for i, raw in enumerate(value):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise FigureExtractionError(
                f"{where}: Variant.{name}[{i}]={raw!r} 必須是數字"
            )
        number = float(raw)
        if not math.isfinite(number):
            raise FigureExtractionError(
                f"{where}: Variant.{name}[{i}]={raw!r} 必須是有限數字"
            )
        coords.append(number)
    if coords[0] > coords[2] or coords[1] > coords[3]:
        raise FigureExtractionError(
            f"{where}: Variant.{name}={value!r} 不是合法矩形（x0>x1 或 y0>y1）"
        )
    return coords


def validate_variant(variant, *, where: str) -> None:
    """`Variant`（契約 §6.3）的**唯一** validator；不合格一律 `FigureExtractionError`。

    四個模組共用這一份：`figure_candidates` 在產生端自檢、`figure_verify` 在**任何 VL
    呼叫之前**檢查、`RAG` 與 `figure_review` 在消費端檢查。訊息一律帶 `where` 與出錯的
    欄位名（呼叫端自己拼 `where`，因為只有它知道是哪個 figure / 哪一頁）。

    驗的每一條（全部**不做轉型**）：

    - `figure_id` / `variant_id` / `mime`：非空 `str`
    - `png`：非空 `bytes`
    - `digest`：必須**等於** `sha256(png).hexdigest()`（缺失、空字串、不符一律 raise）
    - `width` / `height` / `est_image_tokens`：正整數（`est_image_tokens <= 0` 會讓送出前
      的 image-token 預算複查形同虛設）
    - `overlap_px`：非負整數
    - `bbox`：長度 4、四個有限數、`x0 <= x1`、`y0 <= y1`
    - `tile_total >= 1`、`tile_index >= 0`；未 tile 是 `(tile_total=1, tile_index=0)`，
      tiled 是 `tile_total >= 2` 且 `1 <= tile_index <= tile_total`

    `bool` 在每一處都被明確排除（它是 `int` 的子類別，`tile_total=True` 會被當成 1）。
    接受 dataclass / `SimpleNamespace` / dict 三種形狀——驗的是**值**，不是容器型別。
    """
    _variant_nonempty_str(_variant_field(variant, "figure_id", where), "figure_id", where)
    _variant_nonempty_str(_variant_field(variant, "variant_id", where), "variant_id", where)
    _variant_nonempty_str(_variant_field(variant, "mime", where), "mime", where)

    png = _variant_field(variant, "png", where)
    if not isinstance(png, bytes):
        raise FigureExtractionError(
            f"{where}: Variant.png 必須是 bytes（型別 {type(png).__name__}）"
        )
    if not png:
        raise FigureExtractionError(f"{where}: Variant.png 是空的——沒有影像可送模型")

    digest = _variant_field(variant, "digest", where)
    if not isinstance(digest, str) or not digest:
        raise FigureExtractionError(
            f"{where}: Variant.digest={digest!r} 必須是非空 str"
        )
    expected = hashlib.sha256(png).hexdigest()
    if digest != expected:
        raise FigureExtractionError(
            f"{where}: Variant.digest 與 png 內容不符（digest={digest!r}，"
            f"實際 sha256={expected!r}）——digest 是 manifest 與去重的身分，"
            "對不上代表送出的 bytes 不是被記錄的那一份"
        )

    _variant_int(_variant_field(variant, "width", where), "width", where, minimum=1)
    _variant_int(_variant_field(variant, "height", where), "height", where, minimum=1)
    _variant_int(_variant_field(variant, "est_image_tokens", where),
                 "est_image_tokens", where, minimum=1)
    _variant_int(_variant_field(variant, "overlap_px", where), "overlap_px", where, minimum=0)
    _variant_bbox(_variant_field(variant, "bbox", where), "bbox", where)

    tile_total = _variant_int(_variant_field(variant, "tile_total", where),
                              "tile_total", where, minimum=1)
    tile_index = _variant_int(_variant_field(variant, "tile_index", where),
                              "tile_index", where, minimum=0)
    if tile_total == 1:
        if tile_index != 0:
            raise FigureExtractionError(
                f"{where}: 未 tile（tile_total=1）的編號必須是 tile_index=0，實得 {tile_index}"
                "——放行 tile_index=1 之類的變形，接合端會以為它是「多張中的第一張」"
            )
    elif not (1 <= tile_index <= tile_total):
        raise FigureExtractionError(
            f"{where}: tile_index={tile_index} 超出 1..{tile_total} 的範圍"
            "（tiled 的編號是 1-based，缺號或越界會讓接合靜默錯序）"
        )


def is_full_image(variant, *, candidate_bbox, where: str) -> bool:
    """這個 variant 是不是**候選的完整原圖**（而不是其中一片 tile）。

    先跑 `validate_variant()`（不合格即 `FigureExtractionError`），再要求兩件事同時成立：

    1. `tile_total == 1`（沒有被切片）
    2. `variant.bbox` 與 `candidate_bbox` 在容忍值內相等

    第 2 條是重點：**局部 crop 即使宣稱 `tile_total=1` 也不得冒充完整原圖**。只看 flags
    的話，把第一片 tile 的 bytes 配上合法 flags 就能通過，於是「完整原圖」的下游語義
    （REF 的 crop 連結、manifest 的原始 asset）會指向一張只有上緣的圖。

    容忍值：`math.isclose(rel_tol=1e-9, abs_tol=1e-6)`。PDF 座標以 point 為單位，
    1e-6 pt 遠小於任何可見差異，但足以吸收 rotation / cropbox 換算與 float 往返的尾差。

    回傳 `bool`：不是完整原圖時回 `False`（**不 raise**）——那是合法狀態，
    raise 的只有「Variant 本身不合格」。
    """
    validate_variant(variant, where=where)
    box = _variant_bbox(candidate_bbox, "candidate_bbox", where)
    if _variant_field(variant, "tile_total", where) != 1:
        return False
    variant_box = _variant_bbox(_variant_field(variant, "bbox", where), "bbox", where)
    return all(
        math.isclose(a, b, rel_tol=VARIANT_BBOX_REL_TOL, abs_tol=VARIANT_BBOX_ABS_TOL)
        for a, b in zip(variant_box, box)
    )


# ============================================================
# 8. 身分（契約 §2.5）
# ============================================================
def document_id_for(path: Path | str, root: Path | str) -> str:
    """`f"{posix_relpath}::{sha256(file_bytes)[:16]}"`；path 不在 root 內 → `FigureError`。

    relpath 讓同一份檔改名後身分改變（本來就是不同文件），內容 hash 讓同路徑改內容後
    身分也改變（re-ingest 不得沿用舊的 human verification）。

    走 `resolve()` + `relative_to()`（比照 `agent_tools._safe_path` 的慣例）：指向 root
    外的 symlink 會被判為不在 root 內，訊息會把兩條實際路徑印出來。hash 前後各 stat
    一次，讀取期間來源被改寫就 fail-loud——否則會產生一個看起來穩定、實際無法重現的 ID。
    """
    root_path = Path(root).resolve()
    full = Path(path).resolve()
    try:
        relative = full.relative_to(root_path)
    except ValueError as exc:
        raise FigureError(
            f"無法產生 document_id：{full} 不在 root {root_path} 內"
            "（symlink 指向外部時 resolve() 之後也算在外）"
        ) from exc

    try:
        before = os.stat(full)
        digest = hashlib.sha256()
        with open(full, "rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
        after = os.stat(full)
    except OSError as exc:
        raise FigureError(f"無法讀取 {full} 以計算 document_id: {exc}") from exc
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino):
        raise FigureError(
            f"計算 document_id 期間 {full} 被改寫（size/mtime/inode 不一致）；"
            "此時算出來的 hash 無法重現，拒絕使用"
        )
    return f"{PurePosixPath(relative).as_posix()}::{digest.hexdigest()[:16]}"


def document_slug(document_id: str) -> str:
    """檔名安全的 slug：非 `[A-Za-z0-9._-]` → `_`，截到 80 字元，後綴 `-sha256[:10]`。

    hash 後綴有三個作用：截斷後不碰撞、slug 不可能剛好等於 `.` 或 `..`（否則會變成
    路徑穿越的素材）、以及可回頭對照是哪一份 document_id。**不取代** `figure_review`
    的 `safe_figure_path()`——那才是安全檢查點。
    """
    _require_str(document_id, "document_id")
    if not document_id:
        raise FigureError("document_id 不得為空")
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", document_id)[:80]
    return f"{sanitized}-{hashlib.sha256(document_id.encode('utf-8')).hexdigest()[:10]}"


def display_name_for(document_id: str) -> str:
    """給人看的名字：basename（不含 `::hash`）。"""
    _require_str(document_id, "document_id")
    # rsplit：相對路徑本身若含 "::"，切最後一段才不會切錯（hash 一定在最後）
    return PurePosixPath(document_id.rsplit("::", 1)[0]).name


def figure_id_for(document_id: str, page: int, bbox, page_rect, asset_digest: str) -> str:
    """`"fig_" + sha256(f"{document_id}|{page}|{nx0},{ny0},{nx1},{ny1}|{asset_digest}")[:16]`。

    normalized bbox = 每個座標除以頁寬 / 頁高後 `round(..., 4)`，直接以 f-string 代入
    （契約 §2.5 的凍結 preimage 格式，不得改成固定小數位——那會產生另一組永久 ID）。
    `round()` 後加 `0.0` 把 `-0.0` 正規化成 `0.0`：兩者在 f-string 下是 `-0.0` 與 `0.0`，
    不處理的話同一張圖在不同上游版本會拿到不同的永久 ID。
    """
    _require_str(document_id, "document_id")
    if not _is_int(page) or page < 1:
        raise FigureError(f"figure_id_for 的 page 必須是 >= 1 的 int，收到 {page!r}")
    box = _require_bbox(bbox, "figure_id_for 的 bbox")
    rect = _require_bbox(page_rect, "figure_id_for 的 page_rect")
    _require_str(asset_digest, "asset_digest")
    # 契約 §2.5：asset_digest 是「原始 asset bytes」或「candidate signature」的 sha256
    # （`SHA256_HEX_RE` 就是那個格式）。**空字串一律拒絕**：上游漏填時 bbox 沒變的圖會
    # 算出同一個永久 figure_id，re-ingest 會沿用舊的 human verification，等於 figure_id
    # 不再綁定來源像素——這正是 §2.5 要防的事。
    #
    # 這裡刻意只擋「空」而不強制 64 位 hex：真正的綁定性來自「內容變 → digest 變」，
    # 任何非空且隨內容改變的值都成立。強制格式屬於格式衛生，且會讓多個既有測試
    # fixture（"d" / "asset" / "asset-digest-1"）轉紅，那些檔案不屬本任務所有。
    # 若要收緊成 `SHA256_HEX_RE.fullmatch(...)`，需先由主代理同步更新那些 fixture。
    if not asset_digest:
        raise FigureError(
            "figure_id_for 的 asset_digest 不得為空（契約 §2.5：原始 asset bytes 或 "
            "candidate signature 的 sha256）——漏填會讓 figure_id 不再綁定來源像素"
        )
    width = rect[2] - rect[0]
    height = rect[3] - rect[1]
    if width <= 0 or height <= 0:
        raise FigureError(f"figure_id_for 的 page_rect 退化（寬 {width}、高 {height}）：{page_rect!r}")
    nx0 = round(box[0] / width, 4) + 0.0
    ny0 = round(box[1] / height, 4) + 0.0
    nx1 = round(box[2] / width, 4) + 0.0
    ny1 = round(box[3] / height, 4) + 0.0
    preimage = f"{document_id}|{page}|{nx0},{ny0},{nx1},{ny1}|{asset_digest}"
    return "fig_" + hashlib.sha256(preimage.encode("utf-8")).hexdigest()[:16]


# ============================================================
# 9. critical token lexer（契約 §2.6）
# ============================================================
# 回傳的是 **token 字串 list**（不含分類標籤），所以「分類過寬」無害；真正重要的是
# 不把該完整的 token 切開、也不把兩個 token 黏成一個。
#
# ⚠️ regex 只是訊號，不是完整語義驗證：它認得 `0x4000_0100` 是一個 token，不代表
# 它知道那是不是正確的位址。**本 lexer 刻意沒有、也不得加入**任何「CJK 比例 >30%
# 疑似翻譯」之類的判定——中文 log 是合法原文，不是翻譯痕跡。
_TOKEN_PARTS_BASE = (
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",  # UUID
    r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}",                                      # MAC
    r"\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?",                                         # IPv4(:port)
    r"(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:%[0-9A-Za-z]+)?",                 # IPv6
    r"0[xX][0-9A-Fa-f][0-9A-Fa-f_]*\s*[-\u2013\u2014]\s*0[xX][0-9A-Fa-f][0-9A-Fa-f_]*",  # 位址範圍
    r"0[xX][0-9A-Fa-f][0-9A-Fa-f_]*",                                                # 0x 前綴（含底線）
    r"(?<![\w$])[0-9][0-9A-Fa-f_]*[hH](?!\w)",                                       # 尾綴 h/H
    r"\[\s*\d+\s*:\s*\d+\s*\]",                                                      # [msb:lsb]
    r"\[\s*\d+\s*\]",                                                                # [bit]
    r"\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.]+)?",                                      # 版本號
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]+",                      # CJK / kana
    r"[A-Za-z_][A-Za-z0-9_]*",                                                       # identifier
    r"[^\W\d_]+",                                                                    # 其他語系字母
    r"\d+",                                                                          # 純數字
    r"[^\w\s]",                                                                      # 標點（單字元）
)
# terminal 專屬：ANSI CSI 與路徑段。放最前面，免得被拆成 `[`+`0`+`m` 或一堆斜線標點。
_TOKEN_PARTS_TERMINAL = (
    r"\x1b\[[0-9;]*[A-Za-z]",
    r"(?:[A-Za-z]:)?(?:[/\\][\w.+@-]+)+[/\\]?",
)
# table / diagram 專屬：帶單位的數值（3.3V / 100 MHz），避免拆成版本號 + identifier。
_TOKEN_PARTS_UNIT = (
    r"\d+(?:\.\d+)?\s*[munpkKMGT]?(?:Hz|bps|ms|us|ns|[VAWBs])(?![A-Za-z0-9_])",
)

CRITICAL_TOKEN_RE = re.compile("|".join(_TOKEN_PARTS_BASE))
_TOKEN_RE_BY_KIND = {
    KIND_TABLE: re.compile("|".join(_TOKEN_PARTS_UNIT + _TOKEN_PARTS_BASE)),
    KIND_DIAGRAM: re.compile("|".join(_TOKEN_PARTS_UNIT + _TOKEN_PARTS_BASE)),
    KIND_TERMINAL: re.compile("|".join(_TOKEN_PARTS_TERMINAL + _TOKEN_PARTS_BASE)),
}


def critical_tokens(text: str, kind: str) -> list[str]:
    """把 text 切成 critical token（依序回傳，可重複）。

    kind 只決定要不要加上該類型的擴充 pattern（terminal 的 ANSI/path、table 與
    diagram 的帶單位數值）；不認得的 kind 一律退回 base pattern，**不 raise**——這是
    比對訊號函式，會被餵各種來源的文字。

    ⚠️ regex 只是訊號，不是完整語義驗證（見上方註解）。
    """
    _require_str(text, "critical_tokens 的 text")
    # 非 str 的 kind 不可 hash，查 dict 會 TypeError；比對訊號函式一律退回 base pattern
    pattern = _TOKEN_RE_BY_KIND.get(kind, CRITICAL_TOKEN_RE) if isinstance(kind, str) \
        else CRITICAL_TOKEN_RE
    return pattern.findall(text)


_WHITESPACE_RE = re.compile(r"[\s\u3000\u00a0]+")


def normalize_for_compare(text: str) -> str:
    """所有空白（含 `\\n` `\\r` `\\t` 全形空白 / NBSP）壓成單一空格再 strip。

    **只供比對**：截圖證明不了原檔用的是 tab 還是多個 space，所以 anchor 比對必須
    在這個座標系上做。**永遠不要**用它產生 canonical 文字——那等於改寫原文。
    """
    _require_str(text, "normalize_for_compare 的 text")
    return _WHITESPACE_RE.sub(" ", text).strip()


# ============================================================
# 10. 衍生顯示文字（契約 §2.7；每列/每行只 render 一次）
# ============================================================
_META_KEYS = ("figure_id", "revision", "page", "verification_status")


def _validate_meta(meta) -> None:
    """render / chunk 用的 meta。多帶的 key 允許（T5 會傳更多），少或錯就 fail-loud。"""
    if not isinstance(meta, dict):
        raise FigureValidationError(f"meta 必須是 dict，收到 {type(meta).__name__}")
    missing = [key for key in _META_KEYS if key not in meta]
    if missing:
        raise FigureValidationError(f"meta 缺少必要欄位 {missing}（需要 {list(_META_KEYS)}）")
    figure_id = meta["figure_id"]
    if not isinstance(figure_id, str) or not FIGURE_ID_RE.fullmatch(figure_id):
        raise FigureValidationError(
            f"meta['figure_id']={figure_id!r} 不是合法 figure_id（格式 fig_ + 16 位小寫 hex）；"
            "KB 內容裡出現佔位字串等於失去可監督性"
        )
    revision = meta["revision"]
    if not _is_int(revision) or revision < 1:
        raise FigureValidationError(f"meta['revision'] 必須是 >= 1 的 int，收到 {revision!r}")
    page = meta["page"]
    if not _is_int(page) or page < 1:
        raise FigureValidationError(f"meta['page'] 必須是 >= 1 的 int，收到 {page!r}")
    status = _require_str(meta["verification_status"], "meta['verification_status']")
    if status not in VERIFICATION_RANK:
        raise FigureValidationError(
            f"meta['verification_status']={status!r} 不是已知狀態（{sorted(VERIFICATION_RANK)}）"
        )


def _header_line(kind: str, meta: dict, *, range_kw: str = "", span=(0, 0), total: int = 0) -> str:
    """衍生文字的第一行（契約 §2.7）。diagram 沒有 range 欄。"""
    parts = [
        f"[FIGURE kind={kind}",
        f"id={meta['figure_id']}",
        f"rev={meta['revision']}",
        f"page={meta['page']}",
    ]
    if range_kw:
        parts.append(f"{range_kw}={span[0]}-{span[1]}/{total}")
    parts.append(f"status={meta['verification_status']}]")
    return " ".join(parts)


def _escape_inline(text: str) -> str:
    """反斜線與換行的字面化（JSON 才是真相，衍生文字只為 embedding/BM25）。"""
    out = text.replace("\\", "\\\\")
    return out.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _escape_cell(text: str) -> str:
    """markdown 表格 cell：`\\` → `\\\\`、`|` → `\\|`、換行 → 字面 `\\n`（順序不可換）。"""
    out = text.replace("\\", "\\\\")
    out = out.replace("|", "\\|")
    return out.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _select_items(payload: dict, kind: str, index_slice):
    """依 slice 取出要 render 的 row / line。

    slice 座標系（本模組凍結）：`(a, b)` 是 **1-based 閉區間**，比對的是 payload 裡的
    `row_index` / `line_index` **值**（不是 list 位置），且 a 與 b 都必須真的存在——
    越界的 slice 會讓 header 的 range 與實際內容錯位，那是無聲的謊報。
    `None` = 整份；整份且沒有任何 row/line 時 span 為 `(0, 0)`、total 為 0。
    """
    field, index_key = ("rows", "row_index") if kind == KIND_TABLE else ("lines", "line_index")
    items = payload[field]
    indices = [item[index_key] for item in items]
    total = indices[-1] if indices else 0
    if index_slice is None:
        if not items:
            return [], (0, 0), 0
        return list(items), (indices[0], indices[-1]), total
    if not isinstance(index_slice, (tuple, list)) or len(index_slice) != 2:
        raise FigureValidationError(f"slice 必須是 (a, b) 兩元素，收到 {index_slice!r}")
    start, end = index_slice
    if not _is_int(start) or not _is_int(end):
        raise FigureValidationError(f"slice 的兩端必須是 int，收到 {index_slice!r}")
    if start > end:
        raise FigureValidationError(f"slice {index_slice!r} 的起點大於終點")
    known = set(indices)
    if start not in known or end not in known:
        raise FigureValidationError(
            f"slice {index_slice!r} 指到不存在的 {index_key}（現有 {indices[:5]}…共 {len(indices)} 筆）"
        )
    selected = [item for item in items if start <= item[index_key] <= end]
    return selected, (start, end), total


def render_table_text(payload: dict, *, row_slice, meta: dict) -> str:
    """table 的衍生顯示文字：header 行 + 真實表頭 + 分隔列 + 本段各列。

    **每個 chunk 都重出真實 header 與分隔列**（欄數就是真實欄數）。
    **不得**「不足 3 欄補 `#` 欄」——為了迎合既有 splitter 而改變資料，是資料毀損。
    footnotes 只在含第 1 列的那份出現（契約 §10-F），否則同一段註腳會被 BM25 重複計數。
    """
    with _locator_sentinel():
        return _render_table_text_impl(payload, row_slice=row_slice, meta=meta)


def _render_table_text_impl(payload: dict, *, row_slice, meta: dict) -> str:
    """實作；context-free 訊息的 `LOCATOR_UNKNOWN` 前綴由 `render_table_text()` 統一補上。"""
    validate_payload(payload, KIND_TABLE)
    _validate_meta(meta)
    rows, span, total = _select_items(payload, KIND_TABLE, row_slice)
    columns = payload["columns"]

    lines = [_header_line(KIND_TABLE, meta, range_kw="rows", span=span, total=total)]
    lines.append("| " + " | ".join(_escape_cell(column["label"]) for column in columns) + " |")
    lines.append("| " + " | ".join("---" for _ in columns) + " |")
    for row in rows:
        lines.append("| " + " | ".join(_escape_cell(cell["text"]) for cell in row["cells"]) + " |")

    all_rows = payload["rows"]
    includes_first = (not all_rows) or (bool(rows) and rows[0]["row_index"] == all_rows[0]["row_index"])
    if includes_first:
        for i, note in enumerate(payload["footnotes"], 1):
            lines.append(f"[FOOTNOTE {i}] {_escape_inline(note)}")
    return "\n".join(lines)


def _fence_for(texts: Iterable[str]) -> str:
    """動態 fence：長度 = max(3, 內容中最長連續 backtick 數 + 1)。

    log 本身可能含 ``` fence，固定三個 backtick 會讓內容提前結束 code block。
    """
    longest = 0
    for text in texts:
        for run in re.findall(r"`+", text):
            longest = max(longest, len(run))
    return "`" * max(3, longest + 1)


def render_terminal_text(payload: dict, *, line_slice, meta: dict) -> str:
    """terminal 的衍生顯示文字：header 行 + 動態 fence 包住的逐行原文。

    行內容**逐位元組保留**：不 strip、不 reflow、不做 overlap。首行 / 中央 / 末行的
    空行都在 fence 之間原樣保留。解析回來的方式是位置式的（第 0 行 header、第 1 行
    開 fence、最後一行關 fence），中間的行以 `\\n` 切開即為原始行——因為 validator
    保證單行不含 `\\n`/`\\r`，而 fence 保證比內容裡任何 backtick run 都長。
    """
    with _locator_sentinel():
        return _render_terminal_text_impl(payload, line_slice=line_slice, meta=meta)


def _render_terminal_text_impl(payload: dict, *, line_slice, meta: dict) -> str:
    """實作；context-free 訊息的 `LOCATOR_UNKNOWN` 前綴由 `render_terminal_text()` 統一補上。"""
    validate_payload(payload, KIND_TERMINAL)
    _validate_meta(meta)
    lines, span, total = _select_items(payload, KIND_TERMINAL, line_slice)
    texts = [line["text"] for line in lines]
    fence = _fence_for(texts)
    out = [_header_line(KIND_TERMINAL, meta, range_kw="lines", span=span, total=total), fence]
    out.extend(texts)
    out.append(fence)
    return "\n".join(out)


def render_diagram_text(payload: dict, *, meta: dict) -> str:
    """diagram 的衍生顯示文字（無 range 欄）。"""
    with _locator_sentinel():
        return _render_diagram_text_impl(payload, meta=meta)


def _render_diagram_text_impl(payload: dict, *, meta: dict) -> str:
    """實作；context-free 訊息的 `LOCATOR_UNKNOWN` 前綴由 `render_diagram_text()` 統一補上。"""
    validate_payload(payload, KIND_DIAGRAM)
    _validate_meta(meta)
    out = [_header_line(KIND_DIAGRAM, meta)]
    if payload["title"]:
        out.append(f"[TITLE] {_escape_inline(payload['title'])}")
    if payload["labels"]:
        out.append("[LABELS] " + ", ".join(_escape_inline(label) for label in payload["labels"]))
    for item in payload["components"]:
        out.append(f"[COMPONENT] {_escape_inline(item['name'])}: {_escape_inline(item['desc'])}")
    for item in payload["relations"]:
        out.append(
            f"[RELATION] {_escape_inline(item['src'])} -> {_escape_inline(item['dst'])}"
            f": {_escape_inline(item['desc'])}"
        )
    for item in payload["values"]:
        out.append(
            f"[VALUE] {_escape_inline(item['key'])} = {_escape_inline(item['value'])}"
            f": {_escape_inline(item['desc'])}"
        )
    return "\n".join(out)


# ============================================================
# 11. chunk 原子化（契約 §2.8 / §10-A / §10-B / §10-G）
# ============================================================
def _empty_part(content: str, *, reasons: list[str]) -> dict:
    return {
        "content": content,
        "row_range": None,
        "line_range": None,
        "oversized_row": False,
        "oversized_line": False,
        "part_index": 1,
        "part_total": 1,
        "reasons": reasons,
    }


def chunk_payload(payload: dict, kind: str, *, meta: dict, max_chars: int | None = None) -> list[dict]:
    """canonical payload → 不可分割原子的 chunk 清單。

    `meta` 是 **keyword-only**（契約 §2.8 的凍結形狀）。§11.2 凍結的是 `apply_fix`
    注入的 **callback** 形狀 `rechunk(payload, kind, meta)`，而它自己就明寫
    `= figure_extract.chunk_payload(payload, kind, meta=meta)`——**adapter 就是那座橋**，
    `chunk_payload` 從來不需要接受 positional `meta`，兩條契約沒有衝突（契約 §17.1
    已正式推翻先前「取交集」的口頭裁決）。
    `max_chars=None` → 呼叫時才讀 `config.FIGURE_CHUNK_MAX_CHARS`（契約 §10-A；
    import-time snapshot 會讓測試 monkeypatch 失效，也違反 AGENTS.md §4）。

    切法：
    - table：row 是不可分割原子，依 char 預算聚合多列；縮到只剩一列仍超過預算 → 該列
      自己一個 chunk 並標 `oversized_row=True`（**絕不拆格拆列**）。
    - terminal：line 同理，標 `oversized_line=True`（不 strip、不 reflow、不 overlap）。

    `oversized_row` / `oversized_line` 的精確語義是「這個 part 已經縮到只剩一個原子，
    render 出來仍超過 max_chars」——成因可能是該列/行本身超長，也可能是必要的表頭、
    分隔列或註腳開銷把它推過去。兩者都代表同一件事：**不拆原子是刻意的**，下游揭露
    時不要解讀成「這一列一定很長」。
    - diagram：整份一個 chunk；超限時在 `reasons` 加 `oversized_diagram`（契約 §10-B：
      不濫用 row/line 的 oversized 旗標，那會讓下游以為有列被撐大）。
    - 零列 / 零行：仍產出唯一一個 part（range 為 None、header 顯示 `0-0/0`），
      **不得回空 list**——那會讓一張 complete 的圖無聲消失。

    不變式：**非 oversized 的 part 一定 `len(content) <= max_chars`**。預算先用估算挑
    候選群組，再以**實際 render 的長度**複查並在超過時縮小群組，所以 oversized 標記
    永遠與真實長度一致（估算偏差不會變成錯誤的 metadata）。
    `part_index` 從 **1** 起（契約 §10-G，與 row_range/line_range 同座標系）。
    """
    with _locator_sentinel():
        return _chunk_payload_impl(payload, kind, meta=meta, max_chars=max_chars)


def _chunk_payload_impl(payload: dict, kind: str, *, meta: dict, max_chars: int | None = None) -> list[dict]:
    """實作；context-free 訊息的 `LOCATOR_UNKNOWN` 前綴由 `chunk_payload()` 統一補上。"""
    _require_kind(kind)
    validate_payload(payload, kind)
    _validate_meta(meta)
    limit = config.FIGURE_CHUNK_MAX_CHARS if max_chars is None else max_chars
    if not _is_int(limit) or limit <= 0:
        raise FigureValidationError(f"max_chars 必須是 > 0 的 int，收到 {max_chars!r}")

    if kind == KIND_DIAGRAM:
        content = render_diagram_text(payload, meta=meta)
        reasons = ["oversized_diagram"] if len(content) > limit else []
        return [_empty_part(content, reasons=reasons)]

    if kind == KIND_TABLE:
        field, index_key, range_key, flag_key = "rows", "row_index", "row_range", "oversized_row"

        def _render(span):
            return render_table_text(payload, row_slice=span, meta=meta)

        def _item_cost(item):
            return len("| " + " | ".join(_escape_cell(c["text"]) for c in item["cells"]) + " |") + 1
    else:
        field, index_key, range_key, flag_key = "lines", "line_index", "line_range", "oversized_line"

        def _render(span):
            return render_terminal_text(payload, line_slice=span, meta=meta)

        def _item_cost(item):
            return len(item["text"]) + 1

    items = payload[field]
    if not items:
        # 零列 / 零行也要以**實際 render 長度**決定 oversized：長 footnote 的空表、或
        # 小於 header 長度的預算，都會產出超過 max_chars 的 chunk。沒有可切的原子時
        # 仍沿用該 kind 的 oversized 旗標（語義同下：已縮到無法再小卻仍超限），
        # 而不是新增未經 Gate 0 的 reason slug。
        content = _render(None)
        part = _empty_part(content, reasons=[])
        part[flag_key] = len(content) > limit
        return [part]

    indices = [item[index_key] for item in items]
    # index 必須連續：`rows=1-3/3` 在 row_index 為 [1, 3] 時會謊報涵蓋第 2 列。
    # validator 依契約 §2.3 只要求「從 1 起、嚴格遞增」（允許跳號），但**渲染成
    # 連續範圍就是說謊**，所以缺口在這一層 fail-closed，不進 KB。
    if indices != list(range(1, len(indices) + 1)):
        raise FigureValidationError(
            f"{field} 的 index 不連續（{indices[:8]}…共 {len(indices)} 筆）："
            f"衍生文字的 range 是連續閉區間，跳號會讓 header 謊報涵蓋到不存在的列/行。"
            "漏列請重新編號並在 reasons 記錄，不要用缺口表示"
        )
    total = indices[-1]
    costs = [_item_cost(item) for item in items]
    # 保守（偏大）的固定額外開銷：header 用最壞位數，terminal 的 fence 取整份 payload
    # 的上界，table 含表頭 / 分隔列 / 註腳。估太大只會多切一個 chunk（無正確性問題），
    # 估太小則由下面的實際 render 複查補救。
    overhead = len(_header_line(kind, meta, range_kw=("rows" if kind == KIND_TABLE else "lines"),
                                span=(total, total), total=total)) + 1
    if kind == KIND_TABLE:
        columns = payload["columns"]
        overhead += len("| " + " | ".join(_escape_cell(c["label"]) for c in columns) + " |") + 1
        overhead += len("| " + " | ".join("---" for _ in columns) + " |") + 1
        footnote_cost = sum(
            len(f"[FOOTNOTE {i}] {_escape_inline(note)}") + 1
            for i, note in enumerate(payload["footnotes"], 1)
        )
    else:
        fence = _fence_for(item["text"] for item in items)
        overhead += 2 * (len(fence) + 1)
        footnote_cost = 0

    parts: list[dict] = []
    position = 0
    count = len(items)
    while position < count:
        budget = limit - overhead - (footnote_cost if position == 0 else 0)
        take = 1
        used = costs[position]
        while position + take < count and used + costs[position + take] <= budget:
            used += costs[position + take]
            take += 1
        while True:
            span = (indices[position], indices[position + take - 1])
            content = _render(span)
            if len(content) <= limit or take == 1:
                break
            take -= 1
        oversized = len(content) > limit
        part = {
            "content": content,
            "row_range": None,
            "line_range": None,
            "oversized_row": False,
            "oversized_line": False,
            "part_index": len(parts) + 1,
            "part_total": 0,
            "reasons": [],
        }
        part[range_key] = span
        part[flag_key] = oversized
        parts.append(part)
        position += take

    for part in parts:
        part["part_total"] = len(parts)
    return parts


# ============================================================
# 12. KB chunk 產生點（契約 §4 / §6.2）
# ============================================================
_FIGURE_FIELDS = (
    "figure_id", "document_id", "page", "figure_index", "bbox", "kind", "revision",
    "payload", "extraction_status", "verification_status", "reasons", "reason_details",
    "occurrences", "model_input_variant", "row_total", "line_total",
)


def _figure_field(figure, name: str, where: str):
    if isinstance(figure, dict):
        if name not in figure:
            raise FigureValidationError(f"{where} 缺少欄位 {name!r}")
        return figure[name]
    try:
        return getattr(figure, name)
    except AttributeError as exc:
        raise FigureValidationError(f"{where} 缺少欄位 {name!r}") from exc


def _figure_view(figure, position: int) -> dict:
    """把 FigureResult(dataclass / dict / 任意物件) 攤成一個 dict。

    刻意用 duck typing 而不 import `figure_verify`：那會製造 import 循環，也會讓本
    模組的測試被迫等 T4 交付。代價是欄位名等於實質介面——`_FIGURE_FIELDS` 的名稱
    與契約 §6.4 的 `FigureResult` 必須一字不差。
    """
    where = f"figures[{position}]"
    return {name: _figure_field(figure, name, where) for name in _FIGURE_FIELDS}


def _payload_uncertainty(payload: dict, kind: str) -> list[str]:
    """payload 裡「不確定」的證據；非空代表這份內容只能是 `needs_review`。

    契約 §3 的 `needs_review` 定義涵蓋「存在 `▯`、conflict、**漏 row/line**」，所以
    index 缺口（`row_index` 為 [1, 3]）也算——那正是「漏了第 2 列」的表示法。
    """
    found: list[str] = []
    field, index_key = (("rows", "row_index") if kind == KIND_TABLE
                        else ("lines", "line_index") if kind == KIND_TERMINAL else (None, None))
    if field is not None:
        indices = [item[index_key] for item in payload[field]]
        if indices != list(range(1, len(indices) + 1)):
            found.append(f"{field} 的 {index_key} 有缺口（漏 row/line）：{indices[:8]}")
    if kind == KIND_TABLE:
        for row in payload["rows"]:
            for cell in row["cells"]:
                if UNREADABLE_GLYPH in cell["text"]:
                    found.append(f"row {row['row_index']} 的 {cell['column_id']} 含 {UNREADABLE_GLYPH}")
                if cell["state"] in (CELL_STATE_UNREADABLE, CELL_STATE_CONFLICT):
                    found.append(f"row {row['row_index']} 的 {cell['column_id']} state={cell['state']}")
    elif kind == KIND_TERMINAL:
        for line in payload["lines"]:
            if UNREADABLE_GLYPH in line["text"]:
                found.append(f"line {line['line_index']} 含 {UNREADABLE_GLYPH}")
            if line["uncertain_spans"]:
                found.append(f"line {line['line_index']} 有 uncertain_spans")
    else:
        texts = [payload["title"], *payload["labels"]]
        for item in payload["components"]:
            texts.extend((item["name"], item["desc"]))
        for item in payload["relations"]:
            texts.extend((item["src"], item["dst"], item["desc"]))
        for item in payload["values"]:
            texts.extend((item["key"], item["value"], item["desc"]))
        if any(UNREADABLE_GLYPH in text for text in texts):
            found.append(f"diagram 內容含 {UNREADABLE_GLYPH}")
    return found[:5]


def _validate_figure_view(view: dict, *, source: str, position: int) -> None:
    """FigureResult 的 metadata 驗證。

    這裡是 structured chunk 進 KB 的唯一入口，所以「空 evidence_ref / revision 0 /
    未知 status / 空 occurrences / 非法 bbox / 重複 identity」全部要在這裡擋下來——
    它們都能產出外觀正常但破壞監督與去重的 chunk。訊息一律帶 source / page / figure_id。
    """
    where = f"{source} figures[{position}]"
    figure_id = view["figure_id"]
    if not isinstance(figure_id, str) or not FIGURE_ID_RE.fullmatch(figure_id):
        raise FigureValidationError(
            f"{where}: figure_id={figure_id!r} 不是合法格式（fig_ + 16 位小寫 hex，"
            "由 figure_id_for() 產生）"
        )
    where = f"{source} figure={figure_id}"

    document_id = view["document_id"]
    if not isinstance(document_id, str) or "::" not in document_id:
        raise FigureValidationError(f"{where}: document_id={document_id!r} 不是合法 document_id")

    for name in ("page", "figure_index", "revision"):
        value = view[name]
        if not _is_int(value) or value < 1:
            raise FigureValidationError(f"{where}: {name} 必須是 >= 1 的 int，收到 {value!r}")

    bbox = _require_bbox(view["bbox"], f"{where} 的 bbox")
    kind = view["kind"]
    if not isinstance(kind, str) or kind not in FIGURE_KINDS:
        raise FigureValidationError(f"{where}: kind={kind!r} 不是可入庫的 figure kind {list(FIGURE_KINDS)}")

    occurrences = view["occurrences"]
    if not isinstance(occurrences, list) or not occurrences:
        raise FigureValidationError(
            f"{where}: occurrences 必須是非空 list——重複影像只省 VL 計算，"
            "所有 occurrence 都要留得下來（契約 §2.5）"
        )
    for i, occurrence in enumerate(occurrences):
        if not isinstance(occurrence, dict):
            raise FigureValidationError(f"{where}: occurrences[{i}] 必須是 dict")
        for key in ("page", "bbox", "index"):
            if key not in occurrence:
                raise FigureValidationError(f"{where}: occurrences[{i}] 缺少 {key!r}")
        occurrence_page = occurrence["page"]
        if not _is_int(occurrence_page) or occurrence_page < 1:
            raise FigureValidationError(f"{where}: occurrences[{i}].page={occurrence_page!r} 不合法")
        if not _is_int(occurrence["index"]) or occurrence["index"] < 0:
            raise FigureValidationError(f"{where}: occurrences[{i}].index={occurrence['index']!r} 不合法")
        _require_bbox(occurrence["bbox"], f"{where} 的 occurrences[{i}].bbox")
    # page 與 bbox 必須來自**同一個** occurrence。只各自檢查「page 出現在某個
    # occurrence」「bbox 是合法矩形」的話，page 可以來自第 2 頁的 occurrence、bbox
    # 來自第 7 頁的那個，chunk 就會標著 page=2 卻讓 crop / REF 指到第 7 頁的框上。
    #
    # 契約 §4 寫的是「首個 occurrence 的頁碼」。這裡刻意驗「配對存在」而不是「必須是
    # occurrences[0]」：occurrence 的排序由 T3/T4 決定（`figure_verify._occurrences_for`
    # 目前是把候選自己的 occurrence **append** 而非 prepend），強制第一項會讓重複影像
    # 的既有測試轉紅，而那些檔案不屬本任務所有。配對檢查已經擋掉真正的錯配；要收緊成
    # 「必須是 occurrences[0]」需先由主代理讓上游把候選自己的 occurrence 排到最前面。
    if not any(occurrence["page"] == view["page"]
               and _bbox_close(bbox, [float(v) for v in occurrence["bbox"]])
               for occurrence in occurrences):
        raise FigureValidationError(
            f"{where}: page={view['page']} 與 bbox={bbox} 不是同一個 occurrence 的組合"
            f"（occurrences 的頁碼 {[o['page'] for o in occurrences]}）"
            "——crop 與 REF 會指到圖面上完全不同的位置"
        )

    variant = view["model_input_variant"]
    if not isinstance(variant, str) or not variant:
        raise FigureValidationError(
            f"{where}: model_input_variant 必須是非空 str（實際送模型的 variant 是可監督性的一部分）"
        )

    status = view["verification_status"]
    if not isinstance(status, str) or status not in VERIFICATION_RANK:
        raise FigureValidationError(f"{where}: verification_status={status!r} 不是已知狀態")
    if status == VERIF_LEGACY:
        raise FigureValidationError(
            f"{where}: 新資料不得宣稱 {VERIF_LEGACY!r}——那個狀態只給「舊 KB 缺欄位」的 chunk"
        )

    for name in ("reasons", "reason_details"):
        value = view[name]
        # 不接受 None：`or []` 會把「忘了填」與「真的沒有原因」混成同一件事，
        # 待覆核的 chunk 就只剩一個狀態、沒有任何解釋。
        if not isinstance(value, list):
            raise FigureValidationError(
                f"{where}: {name} 必須是 list[str]（不接受 None），收到 {type(value).__name__}"
            )
        _ordered_unique(value)
    if status in FLAGGED_VERIFICATION and not view["reasons"]:
        raise FigureValidationError(
            f"{where}: verification_status={status!r} 屬 flagged，reasons 不得為空——"
            "可監督性要求說得出「為什麼還不能信」"
        )


def build_figure_chunks(figures, *, source: str, doc_type: str,
                        next_chunk_index: dict, evidence_ref_by_figure: dict) -> list[dict]:
    """`FigureResult` list → KB chunk dict list（契約 §4 的形狀）。

    這是 structured figure chunk 的**唯一產生點**，所以三件事在這裡強制：

    1. **零部分成功**：先掃過整批；任一 figure 不是 `complete`（或 payload 是 None）
       就丟 `FigureExtractionError`，不產生任何 chunk。跳過失敗成員再回傳其餘 chunk
       等於部分成功，違反 workflow §8-10。
    2. **狀態機不變式**：payload 裡有 `▯` / `unreadable` / `conflict` / uncertain span /
       index 缺口（漏 row/line）時，`verification_status` 必須**恰為** `needs_review`
       （契約 §3 的定義）。trusted 會直接進 strict query；`unverified` 則讓漏列與猜過的
       字元沒有覆核入口。另外 table 必須帶 `row_total`、terminal 必須帶 `line_total`，
       與 payload 推導值不符或缺漏一律拒絕——那是 REF 揭露截斷用的完整性宣告。
    3. **失敗原子**：`next_chunk_index` 先在 shadow copy 上推進，整批全部成功才一次
       提交。中途失敗時呼叫端拿到的 dict 與呼叫前完全一致。

    另外驗批次一致性：整批 `document_id` 必須相同、`source` 必須等於它的 display name、
    每張圖的 `page`/`bbox` 必須對得上**首個** occurrence（契約 §4）。

    `next_chunk_index`（頁碼 → 下一個可用 chunk_index）就地更新，語意與既有
    `RAG._pdf_figure_chunks` 相同：figure chunk 的 index 接在該頁「文字 chunk +
    legacy diagram chunk」之後，chunk id 才不會撞。

    **不經** `normalize_document_text()` / `normalize_table_content()` / 任何 splitter /
    `detect_content_type()` / overlap / heading 前綴——那些正是本模組存在的理由（見檔頭）。
    chunk 的 `id` 不在這裡產生：`RAG` 統一以 `source::pN::cM::md5(content)[:8]` 補上。
    """
    if not isinstance(source, str) or not source:
        raise FigureValidationError("build_figure_chunks 的 source 必須是非空 str")
    if not isinstance(doc_type, str) or not doc_type:
        raise FigureValidationError("build_figure_chunks 的 doc_type 必須是非空 str")
    if not isinstance(next_chunk_index, dict):
        raise FigureValidationError("next_chunk_index 必須是 dict（頁碼 → 下一個可用 chunk_index）")
    if not isinstance(evidence_ref_by_figure, dict):
        raise FigureValidationError("evidence_ref_by_figure 必須是 dict（figure_id → manifest 路徑）")

    views = [_figure_view(figure, position) for position, figure in enumerate(figures)]

    # (1) 零部分成功：整批先掃一次
    broken = [
        view for view in views
        if view["extraction_status"] != EXTRACTION_COMPLETE or view["payload"] is None
    ]
    if broken:
        detail = "、".join(
            f"page={view['page']} figure={view['figure_id']!r} "
            f"status={view['extraction_status']!r} payload={'有' if view['payload'] else '無'}"
            for view in broken[:5]
        )
        raise FigureExtractionError(
            f"{source}: {len(broken)}/{len(views)} 張 figure 抽取未完成（{detail}），"
            "整批不產生任何 chunk——跳過失敗成員再入庫其餘的，等於部分成功"
        )

    # 同一次呼叫來自同一份 PDF：document_id 必須一致，且 source 必須就是它的 display name。
    # 不驗的話，混入另一份文件的 figure 會用錯的 source 入庫，`remove_document` 之後
    # 留下孤兒 chunk，REF 也會指向錯的檔案。
    document_ids = {view["document_id"] for view in views
                    if isinstance(view["document_id"], str)}
    if len(document_ids) > 1:
        raise FigureValidationError(
            f"{source}: 同一批 figure 的 document_id 不一致 {sorted(document_ids)}"
            "——一次呼叫只能處理一份文件"
        )
    if document_ids:
        expected_source = display_name_for(next(iter(document_ids)))
        if expected_source != source:
            raise FigureValidationError(
                f"source={source!r} 與 document_id 的 display name {expected_source!r} 不符"
                "——KB 的文件身分是 basename，兩者不一致會讓 chunk 掛在錯的文件下"
            )

    seen_ids: set[str] = set()
    seen_slots: set[tuple[int, int]] = set()
    for position, view in enumerate(views):
        _validate_figure_view(view, source=source, position=position)
        figure_id = view["figure_id"]
        if figure_id in seen_ids:
            raise FigureValidationError(f"{source}: figure_id={figure_id!r} 在同一批出現兩次")
        seen_ids.add(figure_id)
        slot = (view["page"], view["figure_index"])
        if slot in seen_slots:
            raise FigureValidationError(
                f"{source}: page={slot[0]} 的 figure_index={slot[1]} 在同一批出現兩次"
                "——頁內序號要能唯一指認一張圖"
            )
        seen_slots.add(slot)

    views.sort(key=lambda view: (view["page"], view["figure_index"]))

    shadow = dict(next_chunk_index)
    chunks: list[dict] = []
    for view in views:
        figure_id = view["figure_id"]
        page = view["page"]
        kind = view["kind"]
        payload = view["payload"]
        status = view["verification_status"]
        where = f"{source} page={page} figure={figure_id}"

        try:
            validate_payload(payload, kind)
        except FigureError as exc:
            raise FigureValidationError(f"{where}: {strip_locator(exc)}") from exc

        # (2) 狀態機不變式：契約 §3 明訂「存在 ▯、conflict、漏 row/line」**就是**
        # needs_review。只擋 trusted 不夠——標成 unverified 一樣會進 general query 的
        # REF，而且 `_get_source_weight` 只降權、不揭露「這裡有猜過的字元」。
        uncertainty = _payload_uncertainty(payload, kind)
        if uncertainty and status != VERIF_NEEDS_REVIEW:
            raise FigureValidationError(
                f"{where}: payload 有不確定內容（{'; '.join(uncertainty)}），"
                f"verification_status 必須恰為 {VERIF_NEEDS_REVIEW!r}，收到 {status!r}。"
                "trusted 會直接進 strict query；unverified 則讓漏列/猜測沒有覆核入口"
            )

        evidence_ref = evidence_ref_by_figure.get(figure_id)
        if not isinstance(evidence_ref, str) or not evidence_ref:
            raise FigureValidationError(
                f"{where}: 缺少 evidence_ref（manifest 路徑）。沒有 locator 的 chunk 無法被覆核"
            )

        # row_total / line_total：payload 是唯一真相，與 FigureResult 不一致即 fail-closed。
        # 「表面上完整、實際漏列」如果只降級成 reason，header 與 REF 仍會顯示成完整。
        row_total = line_total = None
        if kind == KIND_TABLE:
            row_total = payload["rows"][-1]["row_index"] if payload["rows"] else 0
        elif kind == KIND_TERMINAL:
            line_total = payload["lines"][-1]["line_index"] if payload["lines"] else 0
        for name, derived in (("row_total", row_total), ("line_total", line_total)):
            declared = view[name]
            if derived is None:
                # 不適用的 kind 才允許 None
                if declared is not None:
                    raise FigureValidationError(
                        f"{where}: kind={kind} 不該有 {name}，但 FigureResult 給了 {declared!r}"
                    )
                continue
            if declared is None:
                raise FigureValidationError(
                    f"{where}: kind={kind} 必須提供 {name}（int）。缺完整性宣告時，"
                    "header 與 REF 會把可能已截斷的 payload 顯示成完整"
                )
            if not _is_int(declared) or declared != derived:
                raise FigureValidationError(
                    f"{where}: {name}={declared!r} 與 payload 實際的 {derived} 不一致。"
                    "total 是 REF 揭露截斷用的完整性宣告，不一致就是謊報，拒絕入庫"
                )

        meta = {
            "figure_id": figure_id,
            "revision": view["revision"],
            "page": page,
            "verification_status": status,
        }
        try:
            parts = chunk_payload(payload, kind, meta=meta)
        except FigureError as exc:
            raise FigureValidationError(f"{where}: {strip_locator(exc)}") from exc

        base = shadow.get(page, 0)
        if not _is_int(base) or base < 0:
            raise FigureValidationError(f"{where}: next_chunk_index[{page}]={base!r} 不是合法起始索引")
        figure_reasons = _ordered_unique(view["reasons"])
        figure_details = _ordered_unique(view["reason_details"])
        bbox = _require_bbox(view["bbox"], f"{where} 的 bbox")
        occurrences = [
            {"page": occurrence["page"],
             "bbox": _require_bbox(occurrence["bbox"], f"{where} 的 occurrence bbox"),
             "index": occurrence["index"]}
            for occurrence in view["occurrences"]
        ]
        for offset, part in enumerate(parts):
            row_range = part["row_range"]
            line_range = part["line_range"]
            chunks.append({
                # 與文字 chunk 同形狀的既有欄位
                "source": source,
                "page": page,
                "chunk_index": base + offset,
                "content": part["content"],
                "type": doc_type,
                "section": "",
                "heading_hierarchy": "",
                "overlap_prefix_chars": 0,
                "heading_prefix_chars": 0,
                "char_start": 0,
                "char_end": 0,
                # figure 專屬
                "structured": True,
                "origin": ORIGIN_BY_KIND[kind],
                "figure_kind": kind,
                "figure_id": figure_id,
                "document_id": view["document_id"],
                "revision": view["revision"],
                "figure_index": view["figure_index"],
                "bbox": list(bbox),
                "occurrences": copy.deepcopy(occurrences),
                "row_range": list(row_range) if row_range else None,
                "line_range": list(line_range) if line_range else None,
                "row_total": row_total,
                "line_total": line_total,
                "oversized_row": part["oversized_row"],
                "oversized_line": part["oversized_line"],
                "part_index": part["part_index"],
                "part_total": part["part_total"],
                "extraction_status": EXTRACTION_COMPLETE,
                "verification_status": status,
                "reasons": _ordered_unique(figure_reasons + list(part["reasons"])),
                "reason_details": list(figure_details),  # 已在上面 ordered-unique 正規化
                "evidence_ref": evidence_ref,
                "model_input_variant": view["model_input_variant"],
            })
        shadow[page] = base + len(parts)

    # (3) 全批成功才提交 next_chunk_index
    next_chunk_index.update(shadow)
    return chunks


# ============================================================
# 13. 對外門面（契約 §6.2 + §13.5）
# ============================================================
# PEP 562 模組 __getattr__：延遲到**第一次屬性存取**才 import 子模組。
#
# 為什麼不用檔尾 `from figure_candidates import ...`：那樣一來，只要有人先
# `import figure_candidates`（它會 `import figure_extract`），figure_extract 就會在
# 自己還沒跑完檔尾之前反過來要求 figure_candidates 的名稱，而後者此刻尚未初始化完成
# → ImportError。lazy 版本兩個方向都安全，而且三個子模組還沒交付時
# `import figure_extract` 依然可用（本模組是整條鏈的地基）。
_FACADE_SOURCES = {
    # figure_candidates（T3）
    "plan_document_figures": "figure_candidates",
    "check_preflight": "figure_candidates",
    "render_candidate_variants": "figure_candidates",
    "estimate_image_tokens": "figure_candidates",
    "format_preflight_report": "figure_candidates",
    "Candidate": "figure_candidates",
    "PageEvidence": "figure_candidates",
    "Variant": "figure_candidates",
    "FigurePlan": "figure_candidates",
    # figure_verify（T4）
    "ensure_capability": "figure_verify",
    "extract_document_figures": "figure_verify",
    "FigureResult": "figure_verify",
    "ProbeResult": "figure_verify",
    # figure_review（T5）
    "write_run_artifacts": "figure_review",
    "read_manifest": "figure_review",
    "list_figures": "figure_review",
    "apply_fix": "figure_review",
    "new_run_id": "figure_review",
    "prune_old_runs": "figure_review",
    "purge_document_artifacts": "figure_review",
    "evidence_ref_for": "figure_review",
    "source_signature": "figure_review",
    "may_carry_over_human_verification": "figure_review",
}


def __getattr__(name: str):
    """子模組公開名稱的延遲 re-export（見上方註解）。"""
    module_name = _FACADE_SOURCES.get(name)
    if module_name is None:
        raise AttributeError(f"module 'figure_extract' has no attribute {name!r}")
    module = importlib.import_module(module_name)
    try:
        value = getattr(module, name)
    except AttributeError as exc:
        raise AttributeError(
            f"'{module_name}' 沒有提供門面宣告的 {name!r}（契約 §6.2 / §13.5）"
        ) from exc
    globals()[name] = value  # 快取：之後走一般屬性查找
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_FACADE_SOURCES))
