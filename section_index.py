"""Deterministic section recall nodes derived only from persisted KB chunks.

Sections are recall aids, never evidence rows.  Structured figures can belong
to a section but their payload is deliberately absent from its embedding text
and fingerprint: a figure review must not need a model call under the KB lock.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Mapping, Sequence

import context_signals
from knowledge_store import KnowledgeStoreError


SECTION_SCHEMA = "section-title-whole-body-v1"
WINDOW_MAX_CHARS = 4000


def schema_identity() -> str:
    """The render/window/pooling policy is part of the required cache schema."""
    return f"{SECTION_SCHEMA};window={WINDOW_MAX_CHARS};overlap=0;pool=mean-l2"


def _digest(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                         sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _integer(value, default: int = -1) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _string(chunk: Mapping, name: str) -> str:
    value = chunk.get(name, "")
    return value.strip() if isinstance(value, str) else ""


def _source(chunk: Mapping) -> str:
    # Source is a document identity, not a heading label.  Trimming it would
    # combine distinct stored basenames and break the member source check.
    value = chunk.get("source", "")
    return value if isinstance(value, str) and value.strip() else ""


@dataclass(frozen=True)
class SectionNode:
    node_id: str
    source: str
    title: str
    hierarchy: str
    member_ids: tuple[str, ...]
    member_indices: tuple[int, ...]
    text: str
    fingerprint: str
    page_range: tuple[int, int]


def _reading_key(index: int, chunks: Sequence[Mapping]) -> tuple:
    chunk = chunks[index]
    page = _integer(chunk.get("page"), 0)
    start = _integer(chunk.get("char_start"))
    end = _integer(chunk.get("char_end"))
    # char_start/end are source-line locators, not inverse content coordinates.
    # In particular, several long-line chunks can legitimately share a span.
    located = start >= 0 and end > start
    return (page, start if located else _integer(chunk.get("chunk_index"), index),
            _integer(chunk.get("chunk_index"), index), index)


def build_sections(chunks: Sequence[Mapping], *, chunk_ids=None) -> tuple[SectionNode, ...]:
    """Build one node per identifiable section, without reparsing source text.

    Explicit section_index wins; legacy metadata uses consecutive title/path
    runs in document reading order.  Repeated titles separated by another run
    remain separate.  Missing historical boundaries cannot be reconstructed.
    """
    if chunk_ids is None:
        # Late import only for the optional convenience argument.  Both cache
        # callers and the writer can pass their existing row IDs explicitly.
        from kb_cache import chunk_row_ids
        chunk_ids = chunk_row_ids(chunks)
    if len(chunk_ids) != len(chunks):
        raise KnowledgeStoreError("section builder chunk ID count mismatch")
    if any(not isinstance(value, str) or not value for value in chunk_ids):
        raise KnowledgeStoreError("section builder requires nonempty chunk row IDs")

    sources: dict[str, list[int]] = {}
    figures = []
    for index, chunk in enumerate(chunks):
        source = _source(chunk)
        if not source:
            continue
        if chunk.get("structured"):
            figures.append(index)
        else:
            sources.setdefault(source, []).append(index)

    nodes = []
    for source, indices in sources.items():
        groups: dict[tuple, list[int]] = {}
        previous_fallback = None
        run = 0
        for index in sorted(indices, key=lambda i: _reading_key(i, chunks)):
            chunk = chunks[index]
            title = _string(chunk, "section")
            hierarchy = _string(chunk, "heading_hierarchy")
            if not title and not hierarchy:
                previous_fallback = None
                continue
            title = title or hierarchy
            section = _integer(chunk.get("section_index"))
            if section >= 0:
                # Conflicting title/path metadata must not silently combine
                # unrelated sections just because a legacy ordinal repeats.
                key = ("index", section, title, hierarchy)
                previous_fallback = None
            else:
                label = (title, hierarchy)
                if previous_fallback != label:
                    run += 1
                previous_fallback = label
                key = ("run", run, title, hierarchy)
            groups.setdefault(key, []).append(index)

        for key, members in groups.items():
            title, hierarchy = key[-2:]
            bodies = [context_signals.chunk_body(chunks[i]) for i in members]
            # A title-only text anchor still identifies a real section and can
            # recall a uniquely matching figure.  Its title is the full input.
            text = title + "\n" + "\n".join(bodies)
            pages = [_integer(chunks[i].get("page"), 0) for i in members]
            page_range = (min(pages), max(pages))
            # No runtime vectors, chunk_idx, ExtractedDocument or figure
            # payload participates in the vector identity.
            identities = [
                [chunk_ids[i], chunks[i].get("section_index"),
                 _string(chunks[i], "section"), _string(chunks[i], "heading_hierarchy"),
                 chunks[i].get("page"), chunks[i].get("char_start"),
                 chunks[i].get("char_end"), bodies[offset]]
                for offset, i in enumerate(members)
            ]
            node_id = "section:" + _digest([source, key])
            fingerprint = _digest([schema_identity(), node_id, identities, text])
            nodes.append(SectionNode(
                node_id, source, title, hierarchy,
                tuple(chunk_ids[i] for i in members), tuple(members), text,
                fingerprint, page_range,
            ))

    attachments: dict[int, list[int]] = {}
    for index in figures:
        chunk = chunks[index]
        source = _source(chunk)
        title = _string(chunk, "section")
        hierarchy = _string(chunk, "heading_hierarchy")
        page = _integer(chunk.get("page"))
        if page <= 0 or not (title or hierarchy):
            continue
        matched = [
            row for row, node in enumerate(nodes)
            if node.source == source and node.page_range[0] <= page <= node.page_range[1]
            and (not title or node.title == title)
            and (not hierarchy or node.hierarchy == hierarchy)
        ]
        if len(matched) == 1:
            attachments.setdefault(matched[0], []).append(index)

    for row, attached in attachments.items():
        node = nodes[row]
        # Figures have no usable text char span.  Preserve the stored order on
        # each page, rather than treating their conventional zero as offset 0.
        members = sorted((*node.member_indices, *attached),
                         key=lambda i: (_integer(chunks[i].get("page"), 0), i))
        nodes[row] = replace(node, member_indices=tuple(members),
                             member_ids=tuple(chunk_ids[i] for i in members))
    return tuple(nodes)


def embedding_windows(node: SectionNode) -> list[dict]:
    """Bounded pseudo-chunks whose contents cover the entire node text once.

    RAG.generate_embeddings owns HTTP/cache behavior; this function only
    renders its inputs.  No summary, generated ctx, overlap or tail loss.
    """
    skeleton = {"source": node.source, "section": node.hierarchy or node.title,
                "content": ""}
    budget = WINDOW_MAX_CHARS - len(context_signals.gate_embedding_input(skeleton))
    if budget <= 0:
        raise KnowledgeStoreError("section heading exceeds the embedding window budget")
    return [dict(skeleton, content=node.text[start:start + budget])
            for start in range(0, len(node.text), budget)]


def content_hash(nodes: Sequence[SectionNode]) -> str:
    return _digest([[node.node_id, node.fingerprint, node.text] for node in nodes])


def membership_hash(nodes: Sequence[SectionNode]) -> str:
    """Separate identity: figure revisions affect membership, never vectors."""
    return _digest([[node.node_id, node.member_ids, node.page_range] for node in nodes])
