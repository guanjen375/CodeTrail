#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""definition metadata 的全鏈路保存(施工規格 §6 P2-4)。

NEW SILENT CONTRACT。這條鏈的每一段斷掉都是無聲的:

* parser 算出 linkage / condition / storage_class,但 ``_extract_symbols``
  沒複製 → symbol dict 就掉了。
* symbol dict 有,但 ``_index_single_file`` 沒寫進 index entry → 持久 cache 掉了。
* index entry 有,但 renderer 不吃 → 檢索看不到。
* parser 都算好了,但 ``graph_linkage()`` 對非 callable 一律回
  ``not_applicable`` → graph node 又丟一次。

「parser 支援了」不等於「retrieval / graph 保留了」——這些測試守的就是那個落差。
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
from code_graph import CodeGraph  # noqa: E402

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        not ast_parser.HAS_TREE_SITTER
        or not ast_parser._try_load_tree_sitter_language("c"),
        reason="tree-sitter c 未安裝",
    ),
]

FIRMWARE_C = (
    "#define WDT_TIMEOUT_MS 500\n"
    "typedef enum { LINK_IDLE, LINK_BUSY } link_state_t;\n"
    "static int internal_counter;\n"
    "int exported_counter;\n"
    "#if defined(BOARD_ALPHA)\n"
    "static int variant_flag = 1;\n"
    "#endif\n"
    "int firmware_entry(void) { return 0; }\n"
)


@pytest.fixture(autouse=True)
def _fresh_scan_cache():
    code_rag._INDEX_SCAN_CACHE.clear()
    yield
    code_rag._INDEX_SCAN_CACHE.clear()


def _write(root: Path, rel_path: str, content: str) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_symbol_dict_keeps_linkage_condition_and_storage_class(tmp_path: Path):
    _write(tmp_path, "src/fw.c", FIRMWARE_C)
    rag = code_rag.CodeRAG(str(tmp_path))
    symbols = {
        sym["symbol"]: sym
        for sym in rag._extract_symbols(tmp_path / "src/fw.c", FIRMWARE_C)
    }

    assert symbols["internal_counter"]["linkage"] == "internal"
    assert symbols["internal_counter"]["storage_class"] == "static"
    assert symbols["exported_counter"]["linkage"] == "external"
    assert symbols["variant_flag"]["condition"] == "#if defined(BOARD_ALPHA)"
    # linkage 沒有意義的 kind 不得硬掰一個值。
    assert "linkage" not in symbols["WDT_TIMEOUT_MS"]
    assert "linkage" not in symbols["link_state_t"]


def test_index_entry_persists_definition_metadata(tmp_path: Path):
    _write(tmp_path, "src/fw.c", FIRMWARE_C)
    rag = code_rag.CodeRAG(str(tmp_path))
    entries, _embeddings = rag._index_single_file(
        tmp_path / "src/fw.c", "src/fw.c", compute_embeddings=False
    )
    by_symbol = {entry["symbol"]: entry for entry in entries}

    assert by_symbol["internal_counter"]["linkage"] == "internal"
    assert by_symbol["internal_counter"]["storage_class"] == "static"
    assert by_symbol["variant_flag"]["condition"] == "#if defined(BOARD_ALPHA)"
    assert {"macro", "typedef", "enum", "enum_constant", "global", "function"} <= {
        entry["type"] for entry in entries
    }


def test_parser_semantics_version_invalidates_the_coderag_cache(tmp_path: Path,
                                                                monkeypatch):
    """改 parser 語意後不得沿用舊 cache —— 舊 cache 的 symbol 集合已經是錯的。"""
    _write(tmp_path, "src/fw.c", FIRMWARE_C)
    rag = code_rag.CodeRAG(str(tmp_path))
    rag.index, _ = rag._index_single_file(
        tmp_path / "src/fw.c", "src/fw.c", compute_embeddings=False
    )
    rag._file_cache = {"src/fw.c": {"hash": "x", "symbols": rag.index,
                                    "embeddings": []}}
    rag._save_cache()

    assert code_rag.CodeRAG(str(tmp_path))._load_file_cache(), "同版本應載入得到"

    monkeypatch.setattr(code_rag, "PARSER_SEMANTICS_VERSION",
                        ast_parser.PARSER_SEMANTICS_VERSION + 1)
    assert code_rag.CodeRAG(str(tmp_path))._load_file_cache() == {}, (
        "parser semantics 一動,舊 cache 必須被判定為不可用"
    )


def test_embed_text_schema_version_invalidates_the_coderag_cache(tmp_path: Path,
                                                                 monkeypatch):
    """改 embed text 的 render 也要讓舊向量失效 —— 增量重建只比 file_hash。"""
    _write(tmp_path, "src/fw.c", FIRMWARE_C)
    rag = code_rag.CodeRAG(str(tmp_path))
    rag.index, _ = rag._index_single_file(
        tmp_path / "src/fw.c", "src/fw.c", compute_embeddings=False
    )
    rag._file_cache = {"src/fw.c": {"hash": "x", "symbols": rag.index,
                                    "embeddings": []}}
    rag._save_cache()

    monkeypatch.setattr(code_rag, "EMBED_TEXT_SCHEMA_VERSION",
                        code_rag.EMBED_TEXT_SCHEMA_VERSION + 1)
    assert code_rag.CodeRAG(str(tmp_path))._load_file_cache() == {}


def test_graph_keeps_parser_linkage_for_global_objects(tmp_path: Path):
    """graph node 也要留住 linkage;舊版對非 callable 一律 not_applicable。"""
    _write(tmp_path, "src/fw.c", FIRMWARE_C)
    graph = CodeGraph(str(tmp_path))
    graph.build(verbose=False)
    try:
        internal = [n for n in graph.find_nodes("internal_counter")
                    if n["path"] == "src/fw.c"]
        exported = [n for n in graph.find_nodes("exported_counter")
                    if n["path"] == "src/fw.c"]
        macro = [n for n in graph.find_nodes("WDT_TIMEOUT_MS")
                 if n["path"] == "src/fw.c"]
        variant = [n for n in graph.find_nodes("variant_flag")
                   if n["path"] == "src/fw.c"]

        assert internal and internal[0]["linkage"] == "internal"
        assert exported and exported[0]["linkage"] == "external"
        assert variant and variant[0]["condition"] == "#if defined(BOARD_ALPHA)"
        # macro 在 C/C++ 語意上沒有 linkage,誠實標 not_applicable。
        assert macro and macro[0]["linkage"] == "not_applicable"
    finally:
        graph.close()


def test_parser_semantics_version_is_in_the_graph_fingerprint(tmp_path: Path):
    """語意版本要進 graph 指紋,但**不得**濫 bump GRAPH_SCHEMA_VERSION。"""
    graph = CodeGraph(str(tmp_path))
    try:
        fingerprint = graph._parser_versions()
    finally:
        graph.close()
    assert f"parser-semantics:{ast_parser.PARSER_SEMANTICS_VERSION}" in fingerprint


def test_render_budget_change_invalidates_the_coderag_cache(tmp_path: Path,
                                                            monkeypatch):
    """BUG REGRESSION:改 render 預算的**環境變數**不會讓舊 embedding 失效。

    `CODE_RAG_EMBED_TEXT_MAX_CHARS` 等三個預算是 `AICODE_*` 環境變數可覆寫的,
    但 cache meta 只存固定的 schema 版本。重啟時把預算調大/調小,render 出來的
    embed text 不同了,增量重建卻只比 file_hash —— 舊向量被靜默沿用,而且是
    無聲的:沒有任何訊息說「你現在查的是用舊 render 算的向量」。
    """
    _write(tmp_path, "src/fw.c", FIRMWARE_C)
    rag = code_rag.CodeRAG(str(tmp_path))
    rag.index, _ = rag._index_single_file(
        tmp_path / "src/fw.c", "src/fw.c", compute_embeddings=False
    )
    rag._file_cache = {"src/fw.c": {"hash": "x", "symbols": rag.index,
                                    "embeddings": []}}
    rag._save_cache()

    assert code_rag.CodeRAG(str(tmp_path))._load_file_cache(), "同預算應載入得到"

    monkeypatch.setattr(config, "CODE_RAG_EMBED_TEXT_MAX_CHARS",
                        config.CODE_RAG_EMBED_TEXT_MAX_CHARS + 200)
    assert code_rag.CodeRAG(str(tmp_path))._load_file_cache() == {}, (
        "embed text 預算變了,舊向量是用別的 render 算的,不得沿用"
    )


def test_storage_budget_change_invalidates_the_coderag_cache(tmp_path: Path,
                                                             monkeypatch):
    """儲存端上限同理:它決定 index entry 存了多少 context。"""
    _write(tmp_path, "src/fw.c", FIRMWARE_C)
    rag = code_rag.CodeRAG(str(tmp_path))
    rag.index, _ = rag._index_single_file(
        tmp_path / "src/fw.c", "src/fw.c", compute_embeddings=False
    )
    rag._file_cache = {"src/fw.c": {"hash": "x", "symbols": rag.index,
                                    "embeddings": []}}
    rag._save_cache()

    monkeypatch.setattr(config, "CODE_RAG_CONTEXT_STORE_MAX_CHARS",
                        config.CODE_RAG_CONTEXT_STORE_MAX_CHARS + 200)
    assert code_rag.CodeRAG(str(tmp_path))._load_file_cache() == {}


def test_cache_identity_is_the_single_source_for_meta_and_validation():
    """身分欄位只有一份定義,測試 fixture 與 production 都從這裡取。

    兩邊各寫一份的話,加欄位時 fixture 會靜默落後 —— 舊 cache 被拒、測試改走
    full rebuild,於是那條測試「還是綠的」卻不再驗它本來要驗的東西。
    """
    identity = code_rag.cache_identity()
    assert identity["schema_version"] == code_rag.CODE_RAG_CACHE_SCHEMA_VERSION
    assert identity["parser_semantics_version"] == \
        ast_parser.PARSER_SEMANTICS_VERSION
    assert identity["embed_text_schema_version"] == \
        code_rag.EMBED_TEXT_SCHEMA_VERSION
    assert identity["render_budgets"] == {
        "context_store": config.CODE_RAG_CONTEXT_STORE_MAX_CHARS,
        "comment": config.CODE_RAG_COMMENT_MAX_CHARS,
        "embed_text": config.CODE_RAG_EMBED_TEXT_MAX_CHARS,
    }
