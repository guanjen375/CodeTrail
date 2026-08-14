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
