#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Select and pack bounded source evidence for ``code_rag_search(mode="context")``.

This module owns deterministic code-evidence selection, overlapping-range merge,
content de-duplication, and a *character* packing budget.  It is deliberately not
``context_budget.py`` (the LLM request hard gate), and it does not replace
``context_signals.py`` or ``opencode_context.py``.

Filesystem access is injected.  Production callers must use the existing
``ToolExecutor.grep`` / ``ToolExecutor.read_file`` paths; this module never opens
source files directly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

import config


_MAX_QUERY_TERMS = 12
_MAX_LEXICAL_HITS = 160
_MAX_CANDIDATES = 48
_SYMBOL_WINDOW_LINES = 80
_INCLUDE_WINDOW_LINES = 60
_LEXICAL_RADIUS = 8
_PATH_DIVERSITY_PENALTY = 12.0

_STOP_WORDS = frozenset({
    "about", "after", "also", "and", "because", "before", "change", "changing",
    "does", "find", "from", "have", "into", "locate", "need", "never",
    "for", "only", "reach", "reaches", "review", "show", "that", "the", "their",
    "this", "through", "what", "when", "where", "which", "with", "would",
    "如何", "哪個", "哪裡", "為什麼", "這個", "什麼", "請問",
})


@dataclass(frozen=True)
class EvidenceCandidate:
    path: str
    start_line: int
    end_line: int
    symbol: str
    reasons: tuple[str, ...]
    priority: float
    anchor_line: int


def validate_max_chars(value: int) -> int:
    """Validate the public evidence-character budget (bool is not an int here)."""
    minimum = int(config.CODE_CONTEXT_MIN_MAX_CHARS)
    maximum = int(config.CODE_CONTEXT_MAX_MAX_CHARS)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"max_chars 必須是 {minimum}..{maximum} 的整數,收到 {value!r}")
    return value


def query_terms(query: str) -> list[str]:
    """Return a small, stable set of grep-safe lexical terms."""
    raw_terms = re.findall(
        r"[A-Za-z_][A-Za-z0-9_]*|[\u3400-\u9fff]{2,}", str(query)
    )
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_terms:
        normalized = raw.lower()
        if len(normalized) < 3 or normalized in _STOP_WORDS or normalized in seen:
            continue
        seen.add(normalized)
        out.append(raw)
        if len(out) >= _MAX_QUERY_TERMS:
            break
    return out


def collect_safe_lexical_hits(executor, query: str,
                              allowed_paths: Iterable[str]) -> list[dict]:
    """Collect structured grep hits without bypassing ``ToolExecutor`` safety.

    ``allowed_paths`` must come from CodeRAG's scoped scanner.  A grep result is
    accepted only after it passes ``ToolExecutor._safe_path`` again and resolves
    to one of those scoped paths.  No match text is returned or persisted.
    """
    allowed = {str(path).replace("\\", "/") for path in allowed_paths}
    suffixes = sorted({Path(path).suffix.lower() for path in allowed if Path(path).suffix})
    if not allowed or not suffixes:
        return []
    include = ",".join(f"*{suffix}" for suffix in suffixes)
    aggregated: dict[tuple[str, int], set[str]] = {}

    for term in query_terms(query):
        pattern = "(?i)" + re.escape(term)
        output = executor.grep(pattern, path=".", include=include, context=0)
        if not isinstance(output, str) or output.startswith("錯誤:"):
            continue
        for line in output.splitlines():
            match = re.match(r"^(.*):(\d+):(.*)$", line)
            if match is None:
                continue
            raw_path, raw_line = match.group(1), match.group(2)
            try:
                target = executor._safe_path(raw_path)
                if target is None or not target.is_file():
                    continue
                rel_path = target.relative_to(executor.root).as_posix()
            except (OSError, ValueError):
                continue
            if rel_path not in allowed:
                continue
            key = (rel_path, max(1, int(raw_line)))
            aggregated.setdefault(key, set()).add(term)
            if len(aggregated) >= _MAX_LEXICAL_HITS:
                break
        if len(aggregated) >= _MAX_LEXICAL_HITS:
            break

    return [
        {"path": path, "line": line, "terms": sorted(terms, key=str.lower)}
        for (path, line), terms in sorted(aggregated.items())
    ]


def _normalize_item(item: dict) -> dict:
    path = str(item.get("path", "")).replace("\\", "/")
    line = max(1, int(item.get("line", 1) or 1))
    end_line = max(line, int(item.get("end_line", line) or line))
    return {
        **item,
        "path": path,
        "line": line,
        "end_line": end_line,
        "symbol": str(item.get("symbol", "") or ""),
    }


def _symbol_candidate(item: dict, reason: str, priority: float) -> EvidenceCandidate:
    item = _normalize_item(item)
    start = max(1, item["line"] - 4)
    natural_end = item["end_line"] + 4
    end = max(start, min(natural_end, start + _SYMBOL_WINDOW_LINES - 1))
    return EvidenceCandidate(
        path=item["path"],
        start_line=start,
        end_line=end,
        symbol=item["symbol"],
        reasons=(reason,),
        priority=priority,
        anchor_line=item["line"],
    )


def _path_reason(path: str) -> str:
    lower = path.lower()
    name = Path(lower).name
    suffix = Path(lower).suffix
    if ({"test", "tests"} & set(Path(lower).parts)) \
            or name.startswith("test") or name.endswith("_test.c"):
        return "lexical test candidate"
    if suffix in {".h", ".hpp", ".hh", ".hxx"}:
        return "lexical header candidate"
    if suffix in {".cfg", ".ini", ".conf", ".toml", ".yaml", ".yml", ".json"}:
        return "lexical config candidate"
    return "lexical match"


def _lexical_score(terms: list[str], item: dict, query: str) -> tuple[int, bool]:
    searchable = "\n".join(
        str(item.get(key, ""))
        for key in (
            "path", "symbol", "qualified_name", "signature", "context",
            "docstring", "type_hints",
        )
    ).lower()
    hits = sum(1 for term in terms if term.lower() in searchable)
    phrase = " ".join(str(query).lower().split())
    return hits, bool(phrase and phrase in " ".join(searchable.split()))


def _index_lexical_candidates(query: str, index_items: Iterable[dict],
                              allowed: set[str]) -> list[EvidenceCandidate]:
    terms = query_terms(query)
    scored = []
    for raw in index_items:
        item = _normalize_item(raw)
        if item["path"] not in allowed:
            continue
        hits, phrase = _lexical_score(terms, item, query)
        if hits == 0:
            continue
        reason = _path_reason(item["path"])
        priority = 64.0 + min(21.0, hits * 3.0) + (6.0 if phrase else 0.0)
        scored.append((priority, item["path"], item["line"], item, reason))
    scored.sort(key=lambda row: (-row[0], row[1], row[2], row[3]["symbol"]))
    return [
        _symbol_candidate(item, reason, priority)
        for priority, _path, _line, item, reason in scored[:24]
    ]


def _grep_lexical_candidates(hits: Iterable[dict], allowed: set[str]) -> list[EvidenceCandidate]:
    out = []
    for hit in hits:
        path = str(hit.get("path", "")).replace("\\", "/")
        if path not in allowed:
            continue
        line = max(1, int(hit.get("line", 1) or 1))
        terms = tuple(str(term) for term in hit.get("terms", []) if str(term))
        reason = _path_reason(path)
        priority = 68.0 + min(18.0, len(set(terms)) * 3.0)
        out.append(EvidenceCandidate(
            path=path,
            start_line=max(1, line - _LEXICAL_RADIUS),
            end_line=line + _LEXICAL_RADIUS,
            symbol="",
            reasons=(reason,),
            priority=priority,
            anchor_line=line,
        ))
    return out


def _candidate_for_related_node(node: dict, reason: str,
                                priority: float) -> EvidenceCandidate:
    return _symbol_candidate({
        "path": node.get("path", ""),
        "line": node.get("start_line", 1),
        "end_line": node.get("end_line", node.get("start_line", 1)),
        "symbol": node.get("name", ""),
    }, reason, priority)


def _file_candidate(path: str, reason: str, priority: float,
                    index_by_path: dict[str, list[dict]], terms: list[str],
                    query: str) -> EvidenceCandidate:
    items = index_by_path.get(path, [])
    if items:
        ranked = sorted(
            items,
            key=lambda item: (
                -_lexical_score(terms, item, query)[0],
                int(item.get("line", 1) or 1),
                str(item.get("symbol", "")),
            ),
        )
        return _symbol_candidate(ranked[0], reason, priority)
    return EvidenceCandidate(
        path=path,
        start_line=1,
        end_line=_INCLUDE_WINDOW_LINES,
        symbol="",
        reasons=(reason,),
        priority=priority,
        anchor_line=1,
    )


def _graph_candidates(graph, seeds: list[dict], allowed: set[str],
                      index_items: list[dict], query: str) -> tuple[list[EvidenceCandidate], list[dict]]:
    candidates: list[EvidenceCandidate] = []
    uncertainties: list[dict] = []
    terms = query_terms(query)
    index_by_path: dict[str, list[dict]] = {}
    for raw in index_items:
        item = _normalize_item(raw)
        if item["path"] in allowed:
            index_by_path.setdefault(item["path"], []).append(item)

    for seed in seeds:
        matches = [
            node for node in graph.find_nodes(seed["symbol"], limit=20)
            if node.get("path") == seed["path"]
        ]
        if matches:
            anchor = min(
                matches,
                key=lambda node: (abs(int(node.get("start_line", 1)) - seed["line"]),
                                  str(node.get("id", ""))),
            )
            neighborhood = graph.neighbors(
                anchor["id"], edge_types=("calls",), direction="both", hops=1, limit=100
            )
            nodes = {node["id"]: node for node in neighborhood.get("nodes", [])}
            for edge in neighborhood.get("edges", []):
                if not edge.get("resolved"):
                    target = edge.get("unresolved_target") or edge.get("dst_name") or "?"
                    reason = (
                        "ambiguous call target; excluded from confirmed evidence"
                        if edge.get("ambiguity_group")
                        else "call target unresolved (possible function pointer, macro, or missing declaration)"
                    )
                    uncertainties.append({"target": target, "reason": reason})
                    continue
                if edge.get("src_id") == anchor["id"]:
                    related = nodes.get(edge.get("dst_id"))
                    reason, priority = "confirmed callee", 88.0
                elif edge.get("dst_id") == anchor["id"]:
                    related = nodes.get(edge.get("src_id"))
                    reason, priority = "confirmed caller", 86.0
                else:
                    related = None
                if related is not None and related.get("path") in allowed:
                    candidates.append(_candidate_for_related_node(related, reason, priority))

        file_graph = graph.file_neighbors(seed["path"], hops=1, limit=100)
        for edge in file_graph.get("edges", []):
            if not edge.get("resolved"):
                uncertainties.append({
                    "target": edge.get("unresolved_target") or edge.get("dst_id") or "?",
                    "reason": "include/import target ambiguous or unresolved; excluded",
                })
                continue
            if edge.get("src_id") == seed["path"]:
                related_path = edge.get("dst_id")
                reason, priority = "confirmed include", 73.0
            elif edge.get("dst_id") == seed["path"]:
                related_path = edge.get("src_id")
                reason, priority = "confirmed includer", 70.0
            else:
                continue
            if related_path in allowed:
                candidates.append(_file_candidate(
                    related_path, reason, priority, index_by_path, terms, query
                ))

    return candidates, uncertainties


def _merge_reason_tuples(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*left, *right)))


def merge_candidate_ranges(candidates: Iterable[EvidenceCandidate]) -> list[EvidenceCandidate]:
    """Merge overlapping/adjacent ranges in the same file deterministically."""
    ordered = sorted(
        candidates,
        key=lambda c: (c.path, c.start_line, c.end_line, -c.priority, c.symbol),
    )
    merged: list[EvidenceCandidate] = []
    for candidate in ordered:
        candidate = replace(
            candidate,
            path=candidate.path.replace("\\", "/"),
            start_line=max(1, int(candidate.start_line)),
            end_line=max(int(candidate.start_line), int(candidate.end_line)),
            anchor_line=max(1, int(candidate.anchor_line)),
        )
        if not merged or merged[-1].path != candidate.path \
                or candidate.start_line > merged[-1].end_line + 1:
            merged.append(candidate)
            continue
        previous = merged.pop()
        primary = candidate if candidate.priority > previous.priority else previous
        merged.append(EvidenceCandidate(
            path=previous.path,
            start_line=min(previous.start_line, candidate.start_line),
            end_line=max(previous.end_line, candidate.end_line),
            symbol=primary.symbol,
            reasons=_merge_reason_tuples(previous.reasons, candidate.reasons),
            priority=max(previous.priority, candidate.priority),
            anchor_line=primary.anchor_line,
        ))
    return merged


def rank_with_file_diversity(candidates: Iterable[EvidenceCandidate]) -> list[EvidenceCandidate]:
    """Greedy stable ranking with a small penalty for repeated ranges from one file."""
    remaining = list(candidates)
    ranked: list[EvidenceCandidate] = []
    used_per_path: dict[str, int] = {}
    while remaining:
        best = min(
            remaining,
            key=lambda c: (
                -(c.priority - _PATH_DIVERSITY_PENALTY * used_per_path.get(c.path, 0)),
                c.path,
                c.start_line,
                c.end_line,
            ),
        )
        remaining.remove(best)
        ranked.append(best)
        used_per_path[best.path] = used_per_path.get(best.path, 0) + 1
    return ranked


_READ_HEADER_RE = re.compile(r"^=== .* \(行 (\d+)-(\d+) / 共 (\d+) 行\) ===$")
_NUMBERED_LINE_RE = re.compile(r"^\s*\d+\s+\|\s?(.*)$")


def _actual_range(text: str, candidate: EvidenceCandidate) -> tuple[int, int]:
    first = text.splitlines()[0] if text else ""
    match = _READ_HEADER_RE.match(first)
    if match is None:
        return candidate.start_line, candidate.end_line
    return int(match.group(1)), int(match.group(2))


def _content_key(text: str) -> str:
    source_lines = []
    for line in text.splitlines():
        match = _NUMBERED_LINE_RE.match(line)
        if match is not None:
            source_lines.append(match.group(1))
    return "\n".join(source_lines) if source_lines else text


def _has_numbered_source(text: str) -> bool:
    return any(_NUMBERED_LINE_RE.match(line) for line in text.splitlines())


def _shrink_candidate(candidate: EvidenceCandidate, target_lines: int) -> EvidenceCandidate:
    target_lines = max(1, target_lines)
    half = target_lines // 2
    start = max(candidate.start_line, candidate.anchor_line - half)
    end = min(candidate.end_line, start + target_lines - 1)
    start = max(candidate.start_line, end - target_lines + 1)
    return replace(candidate, start_line=start, end_line=end)


def _dedupe_uncertainties(items: Iterable[dict]) -> list[dict]:
    seen = set()
    out = []
    for item in items:
        normalized = {
            "target": str(item.get("target", "?")),
            "reason": str(item.get("reason", "uncertain")),
        }
        key = (normalized["target"], normalized["reason"])
        if key not in seen:
            seen.add(key)
            out.append(normalized)
    return out


def build_code_context(
    *,
    query: str,
    semantic_items: Iterable[dict],
    index_items: Iterable[dict],
    allowed_paths: Iterable[str],
    read_window: Callable[[str, int, int], str],
    max_chars: int,
    graph=None,
    graph_status: str = "ok",
    lexical_hits: Iterable[dict] = (),
) -> dict:
    """Build the exact public context response object.

    ``read_window`` must be ``ToolExecutor.read_file`` (or a test double with the
    same contract).  ``used_chars`` counts only the actual ``evidence[].text``.
    """
    budget = validate_max_chars(max_chars)
    allowed = {str(path).replace("\\", "/") for path in allowed_paths}
    normalized_items = [_normalize_item(item) for item in index_items]

    seeds: list[dict] = []
    seed_candidates: list[EvidenceCandidate] = []
    seen_seeds = set()
    for rank, raw in enumerate(semantic_items):
        item = _normalize_item(raw)
        key = (item["path"], item["line"], item["symbol"])
        if item["path"] not in allowed or key in seen_seeds:
            continue
        seen_seeds.add(key)
        seeds.append({
            "path": item["path"],
            "line": item["line"],
            "symbol": item["symbol"],
            "reason": "semantic",
        })
        seed_candidates.append(_symbol_candidate(item, "semantic", 100.0 - rank * 2.0))

    candidates = list(seed_candidates)
    uncertainties: list[dict] = []
    if graph is not None:
        try:
            graph_candidates, graph_uncertainties = _graph_candidates(
                graph, seeds, allowed, normalized_items, str(query)
            )
            candidates.extend(graph_candidates)
            uncertainties.extend(graph_uncertainties)
        except Exception as exc:
            graph_status = f"degraded: {type(exc).__name__}: {exc}"[:200]

        # Graph is available: add local lexical/test/trace/config diversity.
        candidates.extend(_index_lexical_candidates(str(query), normalized_items, allowed))
        candidates.extend(_grep_lexical_candidates(lexical_hits, allowed))

    ranked = rank_with_file_diversity(merge_candidate_ranges(candidates))
    discarded = ranked[_MAX_CANDIDATES:]
    ranked = ranked[:_MAX_CANDIDATES]
    omitted_reasons: dict[str, int] = {}
    for candidate in discarded:
        for reason in candidate.reasons:
            omitted_reasons[reason] = omitted_reasons.get(reason, 0) + 1

    evidence: list[dict] = []
    seen_content: set[str] = set()
    used_chars = 0
    truncated = bool(discarded)

    for candidate in ranked:
        text = read_window(candidate.path, candidate.start_line, candidate.end_line)
        if not isinstance(text, str) or text.startswith("錯誤:"):
            uncertainties.append({
                "target": f"{candidate.path}:{candidate.start_line}",
                "reason": "safe source read unavailable; evidence excluded",
            })
            continue

        remaining = budget - used_chars
        selected = candidate
        if len(text) > remaining and remaining > 0:
            span = candidate.end_line - candidate.start_line + 1
            target_lines = max(1, int(span * remaining / max(len(text), 1)) - 1)
            if target_lines < span:
                selected = _shrink_candidate(candidate, target_lines)
                text = read_window(selected.path, selected.start_line, selected.end_line)
                truncated = True
        if not isinstance(text, str) or text.startswith("錯誤:") or len(text) > remaining:
            truncated = True
            for reason in candidate.reasons:
                omitted_reasons[reason] = omitted_reasons.get(reason, 0) + 1
            continue

        if not _has_numbered_source(text):
            uncertainties.append({
                "target": f"{candidate.path}:{candidate.start_line}",
                "reason": "safe source window contained no readable source lines",
            })
            continue

        content_key = _content_key(text)
        if content_key in seen_content:
            continue
        seen_content.add(content_key)
        start_line, end_line = _actual_range(text, selected)
        evidence.append({
            "path": selected.path,
            "start_line": start_line,
            "end_line": end_line,
            "symbol": selected.symbol,
            "reason": "; ".join(selected.reasons),
            "text": text,
        })
        used_chars += len(text)

    if omitted_reasons:
        summary = ", ".join(
            f"{reason}={count}" for reason, count in sorted(omitted_reasons.items())
        )
        uncertainties.append({
            "target": f"{sum(omitted_reasons.values())} candidate ranges",
            "reason": f"character budget omitted evidence types: {summary}",
        })

    return {
        "query": str(query),
        "evidence": evidence,
        "uncertainties": _dedupe_uncertainties(uncertainties),
        "seeds": seeds,
        "graph_status": str(graph_status),
        "truncated": truncated,
        "budget_chars": budget,
        "used_chars": used_chars,
    }
