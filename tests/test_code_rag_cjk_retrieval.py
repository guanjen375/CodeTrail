#!/usr/bin/env python3
"""純 CJK / 無 lexical 命中的 query 回零結果 —— 真實 bug regression(2026-08-21)。

MetaWare 樹實測:「環境變數怎麼傳給子行程」回 0 筆,而語料裡有 environ.c、
system.c 的 fork/execvp、run-lldbac.py 的 os.environ→subprocess。

機制是算術的,不是偶發:``_extract_code_tokens`` 只抽 ``[A-Za-z_][A-Za-z0-9_]{2,}``,
純中文問句抽出空集合 → 全語料 ``kw_score`` 皆 0 → fusion 的
``0.5*emb + 0.5*kw`` 只剩一半 → 固定門檻 0.35 等於被悄悄抬成 emb ≥ 0.70
(function 有 +0.05 bonus 則 ≥ 0.60)。實測全語料 emb 最大值 0.5112,
combined 最大值 0.3027,**結構上不可能有任何一筆通過**。

判準是「候選集有沒有實際 kw 命中」,不是「有沒有抽出 token」:中文夾英文
技術詞(例:「VPX 的 DMA descriptor 怎麼設」)會抽出 token,但那些 token
在語料裡可能一個都沒命中,kw_score 照樣全 0,分數照樣被腰斬。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import code_rag  # noqa: E402
from code_rag import hybrid_symbol_score  # noqa: E402
from config import CODE_RAG_THRESHOLD  # noqa: E402

pytestmark = pytest.mark.smoke

# 落在「舊公式必定被濾掉、新公式必定通過」的區間:
# 舊 = 0.5*0.51 + 0.05 = 0.305 < 0.35;新 = 0.51 + 0.05 = 0.56 >= 0.35。
BAND_EMB = 0.51


@pytest.fixture(autouse=True)
def _clean_scan_cache():
    code_rag._INDEX_SCAN_CACHE.clear()
    yield
    code_rag._INDEX_SCAN_CACHE.clear()


# ============================================================
# 單元:融合公式
# ============================================================
def test_no_lexical_signal_is_dense_only():
    """純中文:沒有任何 lexical 訊號時,dense 分數不該被對半稀釋。"""
    combined, _rule = hybrid_symbol_score(
        emb_score=BAND_EMB, kw_score=0.0, item_type="function",
        is_explicit_mention=False, code_token_count=0,
        lexical_has_signal=False,
    )
    assert combined >= CODE_RAG_THRESHOLD, (
        f"combined={combined:.4f} 仍低於門檻 {CODE_RAG_THRESHOLD};"
        "純 CJK query 會結構性地回零結果"
    )


def test_tokens_extracted_but_nothing_matched_is_also_dense_only():
    """中文夾英文技術詞:抽得出 token,但語料裡一個都沒命中 —— 同樣不該稀釋。

    這是 code_token_count 判準修不到的情況(count>=1 卻無命中)。
    """
    combined, _rule = hybrid_symbol_score(
        emb_score=BAND_EMB, kw_score=0.0, item_type="function",
        is_explicit_mention=False, code_token_count=3,
        lexical_has_signal=False,
    )
    assert combined >= CODE_RAG_THRESHOLD, (
        f"combined={combined:.4f};有 token 但零命中時仍被腰斬"
    )


@pytest.mark.parametrize("emb", [0.0, 0.31, 0.62, 1.0])
@pytest.mark.parametrize("kw", [0.0, 0.4, 0.79])
@pytest.mark.parametrize("item_type", ["function", "class", "variable"])
@pytest.mark.parametrize("count", [1, 2, 5])
def test_with_lexical_signal_is_identical_to_the_old_formula(emb, kw, item_type, count):
    """恆等性:只要有 lexical 命中,分數必須與舊公式逐位元相同。

    這條專門守住「不影響現有含英文字的 query」—— core / target 那些題的
    名次不得因為這次修改而漂移。
    """
    combined, rule = hybrid_symbol_score(
        emb_score=emb, kw_score=kw, item_type=item_type,
        is_explicit_mention=False, code_token_count=count,
        lexical_has_signal=True,
    )
    type_bonus = 0.05 if item_type == "function" else 0.0
    assert rule == "fusion"
    assert combined == pytest.approx(0.5 * emb + 0.5 * kw + type_bonus, abs=0.0)


def test_explicit_and_lexical_dominant_rules_are_untouched():
    """兩條捷徑規則不得因為這次修改而改變。"""
    combined, rule = hybrid_symbol_score(
        emb_score=0.0, kw_score=0.0, item_type="class",
        is_explicit_mention=True, code_token_count=1,
        lexical_has_signal=True,
    )
    assert (combined, rule) == (0.95, "explicit_symbol")

    combined, rule = hybrid_symbol_score(
        emb_score=0.0, kw_score=0.9, item_type="class",
        is_explicit_mention=False, code_token_count=2,
        lexical_has_signal=True,
    )
    assert rule == "lexical_dominant"
    assert combined == pytest.approx(0.9 + 0.9 * 0.1)


# ============================================================
# 整合:端對端 query
# ============================================================
def _band_rag(monkeypatch, root: Path, question: str) -> code_rag.CodeRAG:
    """離線 CodeRAG,且讓 query↔symbol 的 cosine 剛好落在 BAND_EMB。"""
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", False)
    monkeypatch.setattr(code_rag, "USE_RERANKER", False)
    q_vec = [1.0, 0.0]
    item_vec = [BAND_EMB, math.sqrt(1.0 - BAND_EMB ** 2)]

    rag = code_rag.CodeRAG(str(root))
    monkeypatch.setattr(
        rag, "_get_embedding",
        lambda text: q_vec if text == question else item_vec,
    )
    monkeypatch.setattr(
        rag, "_embed_texts_batched", lambda texts: [list(item_vec)] * len(texts)
    )
    return rag


def test_pure_cjk_query_returns_hits(monkeypatch, tmp_path):
    """端對端:純中文問句必須撈得到東西。"""
    (tmp_path / "mod.py").write_text(
        "def pass_environment_to_child():\n    return 1\n", encoding="utf-8"
    )
    question = "環境變數怎麼傳給子行程"
    rag = _band_rag(monkeypatch, tmp_path, question)

    hits = rag.query(question, top_k=5)
    assert hits, "純中文 query 回零結果"


def test_cjk_with_unmatched_ascii_token_returns_hits(monkeypatch, tmp_path):
    """端對端:中文夾一個語料裡沒有的英文詞,同樣必須撈得到。"""
    (tmp_path / "mod.py").write_text(
        "def pass_environment_to_child():\n    return 1\n", encoding="utf-8"
    )
    question = "環境變數怎麼傳給 zzzznotpresent"
    rag = _band_rag(monkeypatch, tmp_path, question)

    assert rag._extract_code_tokens(question), "前提:這題必須抽得出 ASCII token"
    hits = rag.query(question, top_k=5)
    assert hits, "有 token 但零命中的 query 回零結果"
