#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""檢索訊號的唯一定義：組字、hash schema、BM25/reranker 文本。

為什麼要有這個模組
------------------
「chunk 怎麼變成 embedding 輸入」以前有兩份實作:`RAG.py._embedding_input`
(寫入端)與 `knowledge.py.KnowledgeBase._compute_content_hash`(載入端各自重打
一次同樣的字串)。兩邊只要有一個字不一樣,hash 就對不上,而症狀是「.npz 內容
雜湊不一致,將重建」這種看起來像資料壞掉的訊息。

Contextual retrieval 之後訊號變成兩套(retrieval 含 ctx / gate 純內容),各自
還要有 schema 名稱與 hash——再讓兩端各寫一份必爆。所以:**寫入端與載入端一律
import 這裡**,全 repo 只有這一份定義。

雙訊號的分工(規格 §2)
----------------------
`ctx` 是 LLM 生成物,只准影響「哪些 chunk 被撈上來、排第幾」。任何**決策**
(拒答、信心標記、rerank/expansion 的 skip、數值證據判定)都必須讀 gate 訊號
= 不含 ctx 的那一套。所以這裡的每個函式都要求呼叫端明講 `use_ctx`,沒有
「看設定自己決定」的預設值——那正是 ctx 會悄悄洩進決策路徑的方式。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Iterable, List

# ============================================================
# schema 名稱
# ============================================================
# 最舊的 KB:hash 只涵蓋 content。
LEGACY_CONTENT_HASH_SCHEMA = "content-v1"
# 現行 content-only 組字（同時是 contextual KB 的 gate 訊號）。
CONTENT_INPUT_SCHEMA = "source-section-content-v2"
# 含 LLM 生成脈絡的 retrieval 組字。
CONTEXTUAL_INPUT_SCHEMA = "source-section-ctx-content-v1"

# 沒有任何 ctx 的 KB，retrieval 矩陣允許的 schema。
LEGACY_RETRIEVAL_SCHEMAS = frozenset({LEGACY_CONTENT_HASH_SCHEMA, CONTENT_INPUT_SCHEMA})
# gate 矩陣只有一種合法 schema：它的定義就是「不含 ctx」。
GATE_SCHEMA = CONTENT_INPUT_SCHEMA

SOURCE_TAG = "[SOURCE]"
SECTION_TAG = "[SECTION_METADATA]"
CTX_TAG = "[CTX]"


# ============================================================
# ctx 欄位
# ============================================================
def chunk_ctx(chunk: Dict) -> str:
    """chunk 的生成脈絡；沒有就是空字串。

    ctx 永遠是獨立欄位,絕不烘進 content——heading 那種確定性資訊烘進 content
    並用 prefix_chars 追蹤是合理的,ctx 是生成性資訊,烘進去之後「證據 =
    content」這條界線就不再機械可判。
    """
    value = chunk.get("ctx", "")
    return str(value).strip() if value else ""


def has_any_ctx(chunks: Iterable[Dict]) -> bool:
    """這批 chunk 裡有沒有任何一個帶 ctx（決定 KB 走 legacy 還是 contextual 規則）。"""
    return any(chunk_ctx(chunk) for chunk in chunks)


def required_retrieval_schemas(*, has_ctx: bool) -> frozenset:
    """目前程式接受的 retrieval schema 集合。

    載入端必須拿 NPZ 自報的 schema 跟這個集合比對。舊碼只拿 NPZ 自報的 schema
    去重算 hash 再比對自己——legacy NPZ 因此永遠自驗通過,程式換了組字規則也
    察覺不到。
    """
    if has_ctx:
        return frozenset({CONTEXTUAL_INPUT_SCHEMA})
    return LEGACY_RETRIEVAL_SCHEMAS


# ============================================================
# embedding 組字
# ============================================================
def _prefix_lines(chunk: Dict) -> List[str]:
    source = Path(str(chunk.get("source", ""))).name
    section = str(chunk.get("section", "")).strip()
    lines = []
    if source:
        lines.append(f"{SOURCE_TAG} {source}")
    if section:
        lines.append(f"{SECTION_TAG} {section}")
    return lines


def gate_embedding_input(chunk: Dict) -> str:
    """content-only 組字：決策一律看這一套算出來的分數。

    來源身分是檢索證據而不是事後 metadata,所以 [SOURCE]/[SECTION_METADATA]
    留在 gate 訊號裡——它們是確定性的,不是生成物。
    """
    parts = _prefix_lines(chunk)
    parts.append(str(chunk.get("content", "")))
    return "\n".join(parts)


def retrieval_embedding_input(chunk: Dict, *, use_ctx: bool) -> str:
    """retrieval 組字：`use_ctx` 為真且該 chunk 有 ctx 時，多插一段 [CTX]。

    ctx 是插槽不是替代:既有的 [SOURCE]/[SECTION_METADATA] 與烘進 content 的
    heading 前綴全部保留。
    """
    if not use_ctx:
        return gate_embedding_input(chunk)
    ctx = chunk_ctx(chunk)
    if not ctx:
        return gate_embedding_input(chunk)
    parts = _prefix_lines(chunk)
    parts.append(f"{CTX_TAG} {ctx}")
    parts.append(str(chunk.get("content", "")))
    return "\n".join(parts)


def embedding_input(chunk: Dict, *, schema: str) -> str:
    """依 schema 名稱組字（供 hash 使用）。"""
    if schema == CONTEXTUAL_INPUT_SCHEMA:
        return retrieval_embedding_input(chunk, use_ctx=True)
    return gate_embedding_input(chunk)


def chunks_content_hash(chunks: Iterable[Dict], *, schema: str) -> str:
    """整批 chunk 的內容雜湊，用來確認 NPZ 與 JSON 是同一份資料。

    非 legacy schema 都加長度前綴,避免欄位/相鄰 chunk 的邊界碰撞。未知 schema
    退回 legacy 的 content-only 行為(與舊碼一致);schema 本身合不合法由
    `required_retrieval_schemas()` 的對照負責,不在這裡判。
    """
    hasher = hashlib.md5()
    known = schema in (CONTENT_INPUT_SCHEMA, CONTEXTUAL_INPUT_SCHEMA)
    for chunk in chunks:
        if not known:
            hasher.update(str(chunk.get("content", "")).encode("utf-8"))
            continue
        encoded = embedding_input(chunk, schema=schema).encode("utf-8")
        hasher.update(len(encoded).to_bytes(8, "big"))
        hasher.update(encoded)
    return hasher.hexdigest()


# ============================================================
# lexical / reranker 文本
# ============================================================
def chunk_body(chunk: Dict) -> str:
    """去掉合成前綴（overlap + heading）之後的本文。

    新 chunk 在切分時就記下前綴長度;舊 KB 沒有那個 metadata,維持原本的索引
    行為直到重建——猜前綴邊界有可能砍掉真正的內文。
    """
    content = str(chunk.get("content", ""))
    try:
        heading_chars = int(chunk.get("heading_prefix_chars", 0) or 0)
        overlap_chars = int(chunk.get("overlap_prefix_chars", 0) or 0)
    except (TypeError, ValueError):
        return content
    total_prefix = heading_chars + overlap_chars
    if total_prefix < 0 or total_prefix > len(content):
        return content
    return content[total_prefix:]


def bm25_document_text(chunk: Dict, *, use_ctx: bool) -> str:
    """BM25 索引的來源文本：章節 + 來源 +（ctx）+ 去前綴本文。

    `use_ctx=False` 的輸出與加入 contextual retrieval 之前逐位元組相同——
    gate BM25 索引就是靠這一點維持「決策看到的還是原本那套 lexical 分數」。
    """
    title = str(chunk.get("section", ""))
    source = str(chunk.get("source", ""))
    body = chunk_body(chunk)
    ctx = chunk_ctx(chunk) if use_ctx else ""
    if ctx:
        return f"{title} {source} {ctx} {body}"
    return f"{title} {source} {body}"


def reranker_passage(chunk: Dict, *, use_ctx: bool, max_chars: int) -> str:
    """cross-encoder 的 document 側文本。"""
    source = chunk.get("source", "")
    section = chunk.get("section", "")
    content = str(chunk.get("content", ""))[:max_chars]
    ctx = chunk_ctx(chunk) if use_ctx else ""
    if ctx:
        return f"Source: {source}\nSection: {section}\nContext: {ctx}\n{content}"
    return f"Source: {source}\nSection: {section}\n{content}"
