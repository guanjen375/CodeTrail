"""Safety contracts for bounded tool-loop progress detection; no live tools."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import client_progress  # noqa: E402
import repeat_guard  # noqa: E402
import tool_result_adapter  # noqa: E402

pytestmark = pytest.mark.smoke


def _call(text="status: ok\nevidence", *, name="read_file", arguments=None, **overrides):
    return dict(
        name=name, arguments={"path": "src/loader.c"} if arguments is None else arguments,
        text=text, **({"status": "completed", "read_only": True, "dispatched": True} | overrides),
    )


def _step(progress, *calls):
    progress.begin_step()
    for call in calls:
        progress.observe(**call)
    return progress.finish_step()


def _adapted(name, arguments, body, *, repeated=0):
    if repeated:
        body = repeat_guard.banner(name, repeated) + body
    result = tool_result_adapter.adapt_tool_result(
        name, body,
        budget=tool_result_adapter.resolve_result_budget(
            n_ctx=131072, requested_max_chars=None, safety_max_chars=200000,
        ),
    )
    return _call(
        result.content[0].text, name=name, arguments=arguments,
        status="error" if result.isError else "completed",
    )


def _grep(pattern, *, body="src/loader.c:18:load_segment(image);", count=1, renderer="rg", **scope):
    suffix = "matches" if renderer == "rg" else "結果"
    return _adapted(
        "grep_code", {"pattern": pattern, **scope},
        f"=== {renderer} '{pattern}' ({count} {suffix}) ===\n{body}",
    )


def test_exact_repeats_ignore_json_key_order_but_use_the_latest_result():
    progress = client_progress.ToolProgress(2, 32)
    first = _call(arguments={"path": "src/loader.c", "start_line": 18})
    reordered = _call(arguments={"start_line": 18, "path": "src/loader.c"})
    assert _step(progress, first) is False
    assert _step(progress, reordered) is True
    changed = reordered | {"text": "status: ok\nchanged evidence"}
    assert _step(progress, changed) is False
    assert _step(progress, first) is False  # An external edit can restore old contents.
    assert _step(progress, first) is True


def test_interleaved_queries_are_tracked_without_crossing_turns():
    progress = client_progress.ToolProgress(2, 32)
    first = _call(arguments={"path": "first.c"})
    second = _call(arguments={"path": "second.c"})
    assert _step(progress, first) is False
    assert _step(progress, second) is False
    assert _step(progress, first) is True
    assert _step(progress, second) is True
    assert _step(client_progress.ToolProgress(2, 32), first) is False


@pytest.mark.parametrize("new_first", [False, True])
def test_new_evidence_anywhere_in_a_batch_prevents_stagnation(new_first):
    progress = client_progress.ToolProgress(2, 32)
    known = _call()
    new = _call("status: ok\nnew source", arguments={"path": "new.c"})
    assert _step(progress, known, known) is False
    calls = (new, known, known) if new_first else (known, known, new)
    assert _step(progress, *calls) is False
    assert _step(progress, known, new) is True


@pytest.mark.parametrize(
    ("renderer", "context", "body"),
    [
        ("rg", 0, "src/loader.c:18:load_segment(image);"),
        ("grep", 0, "src/loader.c:18: load_segment(image);"),
        ("rg", 1, "src/loader.c-17-before\nsrc/loader.c:18:load_segment(image);\nsrc/loader.c-19-after"),
        ("grep", 1, "--- src/loader.c:18 ---\n   17| before\n>  18| load_segment(image);\n   19| after"),
    ],
)
def test_nearby_grep_patterns_require_two_stagnant_complete_batches(renderer, context, body):
    progress = client_progress.ToolProgress(2, 32)
    calls = [_grep(f"load_segment|unused_{index}", renderer=renderer, context=context, body=body)
             for index in range(3)]
    assert [_step(progress, call) for call in calls] == [False, False, True]


def test_same_batch_near_repeats_are_not_new_evidence():
    progress = client_progress.ToolProgress(2, 32)
    assert _step(progress, _grep("load_segment")) is False
    assert _step(progress, _grep("load_segment|x"), _grep("load_segment|y")) is False
    assert _step(progress, _grep("load_segment|z"), _grep("load_segment|w")) is True


def test_new_results_override_near_matches_even_when_a_file_is_restored():
    progress = client_progress.ToolProgress(2, 32)
    old = _grep("load_segment", body="src/loader.c:18:old_value;")
    new = _grep("load_segment", body="src/loader.c:18:new_value;")
    assert _step(progress, old) is False
    assert _step(progress, _grep("load_segment|other", body="src/loader.c:18:new_value;")) is False
    assert _step(progress, new) is False  # This body is known under a different query.
    assert _step(progress, old) is False
    assert _step(progress, old) is True


def test_different_no_hit_queries_are_not_merged():
    progress = client_progress.ToolProgress(2, 32)
    for pattern in ("first", "second", "third", "fourth"):
        call = _adapted("grep_code", {"pattern": pattern}, f"沒有找到 '{pattern}'")
        assert _step(progress, call) is False
    assert _step(progress, call) is True


@pytest.mark.parametrize("scope_key", ["path", "include", "context", "unknown_option"])
def test_changed_scope_or_unknown_arguments_are_not_discarded(scope_key):
    progress = client_progress.ToolProgress(2, 32)
    for index in range(4):
        value = index if scope_key == "context" else f"scope_{index}"
        assert _step(progress, _grep(f"load_segment|{index}", **{scope_key: value})) is False


def test_known_grep_defaults_do_not_resolve_path_spellings():
    progress = client_progress.ToolProgress(2, 32)
    assert _step(progress, _grep("load_segment")) is False
    assert _step(progress, _grep("load_segment|x", path=".", include=None, context=0)) is False
    assert _step(progress, _grep("load_segment|y", path=None, include=None, context=0)) is True
    assert _step(progress, _grep("load_segment|z", path="./")) is False


@pytest.mark.parametrize(
    "body",
    [
        "src/loader.c:18:load_segment(image);\n[CTX] rg 結果不完整",
        "src/loader.c:18:load_segment(image);\n[result truncated by context budget]",
        "src/loader.c:18:load_…[行過長,已截斷 200 字元]",
        "src/loader.c:18:[Omitted long matching line]",
        "18:load_segment(image);",  # No source path in this renderer shape.
        "unknown-renderer load_segment(image);",
    ],
)
def test_incomplete_or_unattributed_grep_results_never_merge_patterns(body):
    progress = client_progress.ToolProgress(2, 32)
    for index in range(3):
        call = _grep(f"load_segment|{index}", body=body)
        assert _step(progress, call) is False
    assert _step(progress, call) is True  # Exact duplicates remain bounded.


def test_match_counts_and_partial_metadata_are_preserved():
    progress = client_progress.ToolProgress(2, 32)
    assert _step(progress, _grep("load_segment")) is False
    for index in range(1, 4):
        # The displayed body cannot prove completeness when its count disagrees.
        assert _step(progress, _grep(f"load_segment|{index}", count=index + 1)) is False
    for index in range(4, 7):
        call = _grep(f"load_segment|{index}")
        call["text"] = call["text"].replace("status: ok\n", "status: partial\nnext: Narrow the search.\n", 1)
        assert _step(progress, call) is False


@pytest.mark.parametrize("context", [0, 1])
def test_python_grep_line_clipping_is_not_treated_as_complete_evidence(context):
    progress = client_progress.ToolProgress(2, 32)
    body = ("--- src/loader.c:18 ---\n>  18| " + "x" * 120
            if context else "src/loader.c:18: " + "x" * 100)
    for index in range(3):
        assert _step(progress, _grep(f"x|{index}", renderer="grep", context=context, body=body)) is False


@pytest.mark.parametrize(
    "body",
    ["=== f.c (行 1-1 / 共 1 行) ===\n   1 | source", "錯誤: source unavailable",
     "=== f.c (行 1-1 / 共 2 行) ===\n   1 | source\n... 用 read_file('f.c', 2) 繼續"],
)
def test_exact_repeat_banner_and_adapter_changes_do_not_wash_the_counter(body):
    progress = client_progress.ToolProgress(2, 32)
    args = {"path": "f.c"}
    assert _step(progress, _adapted("read_file", args, body)) is False
    assert _step(progress, _adapted("read_file", args, body, repeated=2)) is True
    assert _step(progress, _adapted("read_file", args, body, repeated=3)) is True


def test_banner_like_data_and_unrelated_status_changes_remain_evidence():
    progress = client_progress.ToolProgress(2, 32)
    first = _call("status: ok\nfile content\n" + repeat_guard.banner("read_file", 2))
    changed = _call("status: ok\nfile content\n" + repeat_guard.banner("read_file", 3))
    assert _step(progress, first) is False
    assert _step(progress, changed) is False
    assert _step(progress, changed | {"status": "error"}) is False
    assert _step(progress, changed | {"status": "error"}) is True


@pytest.mark.parametrize("read_only", [False, "false", 1, None])
def test_only_literal_readonly_true_avoids_reset_after_a_dispatch(read_only):
    progress = client_progress.ToolProgress(2, 32)
    read = _call()
    mutation = _call("status: error\npatch applied; verification failed", name="apply_patch",
                     arguments={"diff": "private patch"}, read_only=read_only, status="error")
    assert _step(progress, read) is False
    assert _step(progress, mutation) is False
    assert _step(progress, read) is False
    assert _step(progress, mutation) is False
    assert _step(progress, read) is False


@pytest.mark.parametrize("status", ["denied", "error"])
def test_non_dispatched_denials_and_invalid_calls_do_not_clear_the_epoch(status):
    progress = client_progress.ToolProgress(2, 32)
    read = _call()
    refusal = _call("status: error\nnot executed", name="apply_patch", arguments={},
                    status=status, read_only=False, dispatched=False)
    assert _step(progress, read) is False
    assert _step(progress, refusal) is False
    assert _step(progress, refusal) is True
    assert _step(progress, read) is True


def test_progress_resets_the_near_stagnation_run():
    progress = client_progress.ToolProgress(2, 32)
    assert _step(progress, _grep("load_segment")) is False
    assert _step(progress, _grep("load_segment|x")) is False
    assert _step(progress, _call(arguments={"path": "new.c"})) is False
    assert _step(progress, _grep("load_segment|y")) is False
    assert _step(progress, _grep("load_segment|z")) is True


def test_retained_state_is_bounded_and_contains_only_private_content_digests():
    progress = client_progress.ToolProgress(2, 3)
    for index in range(10):
        call = _call(f"status: ok\nPRIVATE_SOURCE_{index}", arguments={"path": f"PRIVATE_PATH_{index}"})
        assert _step(progress, call) is False
        assert len(progress._entries) <= 3
    assert all(isinstance(key, bytes) and len(key) == 32 for key in progress._entries)
    assert "PRIVATE_SOURCE" not in repr(vars(progress))
    assert "PRIVATE_PATH" not in repr(vars(progress))
    evicted = _call("status: ok\nPRIVATE_SOURCE_0", arguments={"path": "PRIVATE_PATH_0"})
    assert _step(progress, evicted) is False
    assert _step(progress, evicted) is True


@pytest.mark.parametrize("invalid", [0, -1, True, 1.0])
def test_invalid_tracking_limits_fail_loud_instead_of_disabling_the_guard(invalid):
    with pytest.raises(ValueError, match="stagnant_steps"):
        client_progress.ToolProgress(invalid, 32)
    with pytest.raises(ValueError, match="max_entries"):
        client_progress.ToolProgress(2, invalid)
