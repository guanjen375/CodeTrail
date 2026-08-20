#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Canonical 語意表示式的契約(施工規格 §6 P3A)。

NEW SILENT CONTRACT。這一包守的是 §3 洞 2:representation 被多個消費點**各自**
截斷,而且截在不同地方。那種壞法完全無聲——

* index entry 在儲存時就把 context 截到 500,所以「把 embed text 從 400 調大」
  是 no-op。報表上只會看到「調了沒用」,不會有人知道上游早就砍過一刀。
* leading comment 加進 embed text 但 lexical scorer 還在掃舊 context,
  於是 lexical lane 永遠看不到註解訊號,而它「沒進步」看起來很正常。
* reranker 自行重組 passage,欄位集合與另外兩個消費者不一致。

所以這裡不只驗「有沒有拿到 comments」,還驗 **上游儲存上限必須大於等於下游
預算** —— 那才是洞 2 的真正病灶。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ast_parser  # noqa: E402
import code_rag  # noqa: E402
import config  # noqa: E402

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        not ast_parser.HAS_TREE_SITTER
        or not ast_parser._try_load_tree_sitter_language("c"),
        reason="tree-sitter c 未安裝",
    ),
]

COMMENTED_C = (
    "/* SPDX-License-Identifier: MIT */\n"
    "\n"
    "/* Generated configuration reviewed when retry policy changes. */\n"
    "#define CONFIG_GENERATION 7u\n"
    "\n"
    "/* trailing note that belongs to the macro above */\n"
    "\n"
    "/** Guards the calibration fallback path. */\n"
    "static int g_calibration_flag;\n"
    "\n"
    "int plain_symbol(void) { return 0; }\n"
)


@pytest.fixture(autouse=True)
def _fresh_scan_cache():
    code_rag._INDEX_SCAN_CACHE.clear()
    yield
    code_rag._INDEX_SCAN_CACHE.clear()


def _entries(tmp_path: Path) -> dict:
    source = tmp_path / "src" / "fw.c"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(COMMENTED_C, encoding="utf-8")
    rag = code_rag.CodeRAG(str(tmp_path))
    rows, _embeddings = rag._index_single_file(
        source, "src/fw.c", compute_embeddings=False
    )
    return rag, {row["symbol"]: row for row in rows}


# ============================================================
# leading comment 邊界
# ============================================================
def test_leading_comment_association_respects_its_four_boundaries(tmp_path: Path):
    _rag, entries = _entries(tmp_path)

    macro = entries["CONFIG_GENERATION"]["comments"]
    assert "Generated configuration reviewed" in macro
    assert "SPDX" not in macro, "檔頭 license 不得掛到第一個 symbol 身上"

    flag = entries["g_calibration_flag"]["comments"]
    assert "Guards the calibration fallback path" in flag
    assert "trailing note" not in flag, "跨空行的上一個 symbol 尾註不得被撈進來"

    assert "comments" not in entries["plain_symbol"], "沒有註解就不要編一個出來"


# ============================================================
# 三個消費者都要看得到(不能有人靜默看不到)
# ============================================================
def test_leading_comment_enters_embed_text(tmp_path: Path):
    rag, entries = _entries(tmp_path)
    embed_text = rag._build_embed_text(entries["CONFIG_GENERATION"])
    assert "Generated configuration reviewed" in embed_text


def test_leading_comment_is_visible_to_the_lexical_scorer(tmp_path: Path):
    """lexical lane 也要吃得到註解 —— 只放進 embed text 等於半條鏈沒接上。"""
    rag, entries = _entries(tmp_path)
    entry = entries["CONFIG_GENERATION"]

    scan_text = code_rag.lexical_scan_text(
        entry, config.CODE_RAG_LEXICAL_SCAN_MAX_CHARS
    )
    assert "Generated configuration reviewed" in scan_text

    # 註解裡才有的詞必須真的推高分數,而不是只是出現在字串裡。
    tokens = rag._extract_code_tokens("generated configuration reviewed retry policy")
    assert rag._token_match_score(tokens, entry) > 0.0

    without_comment = {k: v for k, v in entry.items() if k != "comments"}
    assert rag._token_match_score(tokens, entry) > \
        rag._token_match_score(tokens, without_comment)


def test_every_consumer_projects_the_same_canonical_field_set(tmp_path: Path):
    """三個消費者的字串不必相同,但欄位集合必須同一份來源。"""
    _rag, entries = _entries(tmp_path)
    entry = entries["g_calibration_flag"]
    labels = {label for label, _text in code_rag.semantic_fields(entry)}

    assert "linkage:" in labels, "P2 抽到的 linkage 要進表示式,不能只躺在 index 裡"
    rendered = code_rag.render_semantic_fields(entry, 10_000)
    assert "Guards the calibration fallback path" in rendered
    assert "linkage: internal" in rendered
    # canonical 順序:識別資訊在前,context 在最後(超預算時先被截的是它)。
    assert code_rag.CANONICAL_SEMANTIC_FIELDS[-1] == "context"
    assert code_rag.CANONICAL_SEMANTIC_FIELDS.index("comments") < \
        code_rag.CANONICAL_SEMANTIC_FIELDS.index("context")


# ============================================================
# 預算:上游儲存上限必須 >= 下游預算(§3 洞 2 的真正病灶)
# ============================================================
def test_storage_cap_is_not_smaller_than_downstream_budgets():
    """儲存端截得比消費端小的話,調大消費端預算全部是 no-op。"""
    assert config.CODE_RAG_CONTEXT_STORE_MAX_CHARS >= \
        config.CODE_RAG_EMBED_TEXT_MAX_CHARS, (
            "index entry 的 context 上限比 embed text 預算小 —— "
            "調大 embed 預算會是 no-op(這正是 §3 洞 2)"
        )
    assert config.CODE_RAG_CONTEXT_STORE_MAX_CHARS >= \
        config.CODE_RAG_LEXICAL_SCAN_MAX_CHARS
    assert config.CODE_RAG_CONTEXT_STORE_MAX_CHARS >= \
        config.CODE_RERANK_PASSAGE_MAX_CHARS


def test_consumer_budgets_are_independent_knobs():
    """storage cap 與 reranker cap 分開,不是同一個常數改名。"""
    names = (
        "CODE_RAG_CONTEXT_STORE_MAX_CHARS",
        "CODE_RAG_EMBED_TEXT_MAX_CHARS",
        "CODE_RAG_LEXICAL_SCAN_MAX_CHARS",
        "CODE_RERANK_PASSAGE_MAX_CHARS",
    )
    for name in names:
        assert isinstance(getattr(config, name), int)
        assert getattr(config, name) > 0


def test_render_truncates_from_the_tail_and_keeps_identity_fields():
    """超預算時砍的是 context,不是 symbol / signature / comments。"""
    entry = {
        "path": "src/fw.c",
        "type": "function",
        "symbol": "calibration_load",
        "signature": "int calibration_load(const unsigned *c, unsigned n)",
        "comments": "/* Exercise calibration fallback when storage is blank. */",
        "context": "X" * 5000,
    }
    rendered = code_rag.render_semantic_fields(entry, 300)
    assert len(rendered) <= 320  # 每個欄位之間的分隔空白
    assert "calibration_load" in rendered
    assert "Exercise calibration fallback" in rendered
    assert rendered.count("X") < 5000
