#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bounded code evidence selection, merge, safety, and char-budget tests."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import code_context  # noqa: E402
from agent_tools import ToolExecutor  # noqa: E402
from code_context import EvidenceCandidate  # noqa: E402


@pytest.mark.parametrize("value", [1999, 30001, True, False, 12000.0, "12000"])
def test_max_chars_rejects_out_of_range_and_non_integer(value):
    with pytest.raises(ValueError, match="2000..30000"):
        code_context.validate_max_chars(value)


@pytest.mark.parametrize("value", [2000, 12000, 28000, 30000])
def test_max_chars_accepts_documented_range(value):
    assert code_context.validate_max_chars(value) == value


def test_overlapping_ranges_merge_and_preserve_reasons():
    merged = code_context.merge_candidate_ranges([
        EvidenceCandidate("src/a.c", 10, 30, "seed", ("semantic",), 100, 12),
        EvidenceCandidate("src/a.c", 25, 45, "callee", ("confirmed callee",), 88, 27),
        EvidenceCandidate("src/a.c", 70, 80, "other", ("lexical match",), 70, 72),
    ])
    assert len(merged) == 2
    assert (merged[0].start_line, merged[0].end_line) == (10, 45)
    assert merged[0].symbol == "seed"
    assert merged[0].reasons == ("semantic", "confirmed callee")


def _fake_numbered_read(path: str, start: int, end: int, width: int = 96) -> str:
    lines = [f"{line:4d} | {path}:{'x' * width}" for line in range(start, end + 1)]
    return (
        f"=== {path} (行 {start}-{end} / 共 500 行) ===\n"
        + "\n".join(lines)
        + f"\n... 用 read_file('{path}', {end + 1}) 繼續"
    )


def test_pack_respects_actual_text_budget_and_reports_omissions():
    calls = []

    def read_window(path, start, end):
        calls.append((path, start, end))
        return _fake_numbered_read(path, start, end)

    bundle = code_context.build_code_context(
        query="large evidence",
        semantic_items=[
            {"path": "src/a.c", "symbol": "a", "line": 1, "end_line": 40},
            {"path": "src/b.c", "symbol": "b", "line": 1, "end_line": 40},
        ],
        index_items=[],
        allowed_paths={"src/a.c", "src/b.c"},
        read_window=read_window,
        max_chars=2000,
        graph=None,
        graph_status="unavailable",
    )

    actual = sum(len(item["text"]) for item in bundle["evidence"])
    assert bundle["used_chars"] == actual <= 2000
    assert bundle["truncated"] is True
    assert len(calls) > len(bundle["evidence"]), "oversized windows should be safely re-read smaller"
    assert any("budget omitted" in item["reason"] for item in bundle["uncertainties"])


def test_identical_source_content_is_deduplicated():
    def same_content(path, start, end):
        return (
            f"=== {path} (行 1-2 / 共 2 行) ===\n"
            "   1 | int same(void) {\n"
            "   2 |     return 1;\n"
        )

    bundle = code_context.build_code_context(
        query="same",
        semantic_items=[
            {"path": "a.c", "symbol": "same", "line": 1, "end_line": 2},
            {"path": "b.c", "symbol": "same", "line": 1, "end_line": 2},
        ],
        index_items=[],
        allowed_paths={"a.c", "b.c"},
        read_window=same_content,
        max_chars=2000,
        graph=None,
        graph_status="unavailable",
    )
    assert len(bundle["seeds"]) == 2
    assert len(bundle["evidence"]) == 1


def test_safe_lexical_hits_support_config_and_filter_unscoped_paths(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "config").mkdir()
    (root / "ignored").mkdir()
    (root / "config/layout.cfg").write_text(
        "HANDOFF_REGION=BOOT_FAST\n", encoding="utf-8"
    )
    (root / "ignored/secret.cfg").write_text(
        "HANDOFF_REGION=SECRET\n", encoding="utf-8"
    )
    executor = ToolExecutor(str(root))

    hits = code_context.collect_safe_lexical_hits(
        executor,
        "latency critical interrupt region",
        allowed_paths={"config/layout.cfg"},
    )
    assert {hit["path"] for hit in hits} == {"config/layout.cfg"}
    assert all("text" not in hit for hit in hits), "grep source text must not leak into metadata"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink containment")
def test_source_windows_cannot_follow_symlink_outside_sandbox(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.c"
    outside.write_text("int secret(void) { return 7; }\n", encoding="utf-8")
    os.symlink(outside, root / "linked.c")
    executor = ToolExecutor(str(root))
    calls = []

    def safe_read(path, start, end):
        calls.append((path, start, end))
        return executor.read_file(path, start_line=start, end_line=end)

    bundle = code_context.build_code_context(
        query="secret",
        semantic_items=[
            {"path": "linked.c", "symbol": "secret", "line": 1, "end_line": 1}
        ],
        index_items=[],
        # Even a compromised candidate catalog cannot bypass the final safe read.
        allowed_paths={"linked.c"},
        read_window=safe_read,
        max_chars=2000,
        graph=None,
        graph_status="unavailable",
    )
    assert calls == [("linked.c", 1, 5)]
    assert bundle["evidence"] == []
    assert bundle["used_chars"] == 0
    assert any("safe source read unavailable" in row["reason"]
               for row in bundle["uncertainties"])


def test_binary_disguised_as_source_is_rejected_by_safe_reader(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "bad.c").write_bytes(b"\x00\x01\x02not-source")
    executor = ToolExecutor(str(root))
    bundle = code_context.build_code_context(
        query="bad",
        semantic_items=[{"path": "bad.c", "symbol": "bad", "line": 1}],
        index_items=[],
        allowed_paths={"bad.c"},
        read_window=lambda path, start, end: executor.read_file(
            path, start_line=start, end_line=end
        ),
        max_chars=2000,
        graph=None,
        graph_status="unavailable",
    )
    assert bundle["evidence"] == []
    assert bundle["used_chars"] == 0
    assert bundle["uncertainties"]


def test_unscoped_index_items_never_reach_read_callback():
    calls = []
    bundle = code_context.build_code_context(
        query="secret",
        semantic_items=[
            {"path": "node_modules/secret.c", "symbol": "secret", "line": 1}
        ],
        index_items=[
            {"path": "node_modules/secret.c", "symbol": "secret", "line": 1,
             "context": "secret"}
        ],
        allowed_paths={"src/good.c"},
        read_window=lambda *args: calls.append(args) or "should not be read",
        max_chars=2000,
        graph=None,
        graph_status="unavailable",
    )
    assert bundle["seeds"] == []
    assert bundle["evidence"] == []
    assert calls == []
