"""KnowledgeBase staleness 偵測（source_changed）回歸測試。

背景（2026-08-14 review，P3）：ingest_document 後不 reload 就查不到，
「必須 reload」只活在文件與工具回傳字串裡（prompt 層），沒有 code 層保證。
修正後：KnowledgeBase 記錄載入當下的檔案簽章（mtime_ns, size），
MCP query_knowledge / query_knowledge_strict 每次查詢前用 source_changed()
自動偵測並重載。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import knowledge


def _write_empty_kb(path: Path) -> None:
    path.write_text(json.dumps({"chunks": [], "metadata": {}}), encoding="utf-8")


def test_source_changed_false_when_still_missing(tmp_path: Path):
    kb = knowledge.KnowledgeBase(str(tmp_path / "knowledge.json"))
    assert kb.loaded is False
    assert kb.source_changed() is False  # 沒檔案 → 沒變化


def test_source_changed_true_when_file_appears(tmp_path: Path):
    """server 啟動時還沒有 knowledge.json，之後第一次 ingest 建檔 → 要偵測到。"""
    p = tmp_path / "knowledge.json"
    kb = knowledge.KnowledgeBase(str(p))
    _write_empty_kb(p)
    assert kb.source_changed() is True


def test_source_changed_after_mtime_bump(tmp_path: Path):
    """已載入的 KB，檔案被 subprocess/CLI 改寫（mtime 變）→ 要偵測到。"""
    p = tmp_path / "knowledge.json"
    _write_empty_kb(p)
    kb = knowledge.KnowledgeBase(str(p))
    assert kb.loaded is True
    assert kb.source_changed() is False

    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert kb.source_changed() is True


def test_source_changed_after_size_change(tmp_path: Path):
    """mtime 解析度不夠時 size 差異也要能抓到。"""
    p = tmp_path / "knowledge.json"
    _write_empty_kb(p)
    kb = knowledge.KnowledgeBase(str(p))
    st = p.stat()

    p.write_text(
        json.dumps({"chunks": [], "metadata": {"documents": []}}),
        encoding="utf-8",
    )
    # 就算把 mtime 改回舊值，size 不同仍算變更
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert kb.source_changed() is True


def test_load_failure_stays_stale_and_strict_loader_protects_old_kb(tmp_path: Path):
    """壞檔回歸（2026-08-14 GPT review #1）：

    以前壞 JSON 會被吞成 loaded=False 的空殼、同時記住壞檔簽章 →
    source_changed() 回 False，自動重載永遠不再重試，且 MCP 端已把
    還能用的舊 KB 換掉。修復後：失敗的 instance 永遠算 stale（哨兵簽章），
    strict loader 對壞檔拋 KnowledgeStoreError，呼叫端得以保留舊 KB。
    """
    p = tmp_path / "knowledge.json"
    _write_empty_kb(p)
    good = knowledge.KnowledgeBase(str(p))
    assert good.loaded is True

    p.write_text("{broken", encoding="utf-8")
    assert good.source_changed() is True  # 舊 KB 看得到檔案變更

    broken = knowledge.KnowledgeBase(str(p))  # 一般例外被吞：loaded=False
    assert broken.loaded is False
    assert broken.load_error
    assert "載入失敗" in broken.get_status()
    # 修復核心：失敗的 instance 不記壞檔簽章 → 下一次查詢一定重試
    assert broken.source_changed() is True

    with pytest.raises(knowledge.KnowledgeStoreError):
        knowledge.load_knowledge_base_strict(str(p))

    # 修好檔案後 strict loader 恢復正常
    _write_empty_kb(p)
    fixed = knowledge.load_knowledge_base_strict(str(p))
    assert fixed.loaded is True
    assert fixed.load_error is None


def test_strict_loader_accepts_missing_file_as_empty(tmp_path: Path):
    """檔案不存在是合法空庫，strict loader 不該拋錯（首次啟動情境）。"""
    kb = knowledge.load_knowledge_base_strict(str(tmp_path / "knowledge.json"))
    assert kb.loaded is False
    assert kb.load_error is None
