"""Observed undercount, cache-heavy histories and recoverable compaction failures."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

import client_compaction as cc
import client_events
import context_budget


pytestmark = pytest.mark.smoke


def _summary():
    return "\n".join(f"## {heading}\n- 已保留的事實" for heading in cc.rule_headings())


def _history(turns=4):
    return [message for index in range(turns) for message in (
        {"role": "user", "content": f"FULL_BLOCK_{index}: 使用者原始問題"},
        {"role": "assistant", "content": f"ANSWER_{index}: 完整答案", "time": index},
    )]


class CountedEngine:
    """A token service can report far more tokens than character heuristics."""

    def __init__(self, history=None, *, n_ctx=131072, input_tokens=123345, unit_tokens=45000):
        self.session_id = "20260916T120000-aabbccdd"
        self.messages = copy.deepcopy(history if history is not None else _history())
        self.options = SimpleNamespace(n_ctx=n_ctx, max_output_tokens=8192)
        self.input_tokens = input_tokens
        self.unit_tokens = unit_tokens
        self.replacement_tokens = 3000
        self.requests = []
        self.replaced = None
        self.failure = None
        self.fail_on_call = 0
        self.prompt_tokens_processed = 7  # A warm cache says nothing about capacity.

    def payload_messages(self):
        return copy.deepcopy(self.messages), {}

    def openai_tools(self):
        return []

    def context_tokens(self, history=None):
        return self.input_tokens if history is None else self.replacement_tokens

    def count_input_tokens(self, messages, **_kwargs):
        transcript = messages[-1]["content"]
        # Each marker represents a dense token block from a different real turn.
        return 512 + transcript.count("FULL_BLOCK_") * self.unit_tokens

    def prune_for_summary(self, messages):
        assert messages == self.messages[:len(messages)]
        return copy.deepcopy(messages)

    def complete(self, messages, *, source):
        assert source == "compaction"
        measured = self.count_input_tokens(messages)
        if measured + self.options.max_output_tokens >= self.options.n_ctx * 0.9:
            usage = context_budget.ContextUsage(
                source=source, effective_num_ctx=self.options.n_ctx,
                estimated_input_tokens=measured,
                reserved_output_tokens=self.options.max_output_tokens,
                hard_overflow=True,
            )
            raise context_budget.ContextOverflowError(usage)
        self.requests.append(copy.deepcopy(messages))
        if self.failure is not None and (
            not self.fail_on_call or len(self.requests) == self.fail_on_call
        ):
            raise self.failure
        return cc.Completion(_summary())

    def replace_history(self, messages):
        self.replaced = copy.deepcopy(messages)
        self.messages = copy.deepcopy(messages)


@pytest.mark.parametrize("n_ctx", [32768, 65536, 98304, 131072, 262144, 524288, 1024000, 1048576])
def test_plain_conversation_compacts_by_full_tokens_across_live_context_sizes(n_ctx):
    engine = CountedEngine(n_ctx=n_ctx)
    compactor = cc.Compactor(engine, cc.MODE_CODETRAIL, n_ctx=n_ctx, max_output_tokens=8192, env={})
    threshold = compactor.derived.idle_threshold
    for count, expected in ((threshold - 1, False), (threshold, False), (threshold + 1, True)):
        engine.input_tokens = count
        assert compactor.should_compact()[0] is expected, (
            "Full prompt count must drive ordinary idle compaction, even with only 7 tokens processed"
        )


def test_an_already_oversized_summary_is_batched_without_losing_turns(tmp_path):
    engine = CountedEngine()
    original = copy.deepcopy(engine.messages)
    compactor = cc.Compactor(engine, cc.MODE_CODETRAIL, n_ctx=131072, env={"HOME": str(tmp_path)})
    outcome = compactor.compact(manual=True)
    assert outcome.status == "compacted", outcome
    assert len(engine.requests) >= 2
    transcripts = [request[-1]["content"] for request in engine.requests]
    for index in range(3):
        assert sum(text.count(f"FULL_BLOCK_{index}:") for text in transcripts) == 1
        assert sum(text.count(f"ANSWER_{index}:") for text in transcripts) == 1
    assert all("FULL_BLOCK_3:" not in text for text in transcripts)
    assert all(_summary() in request[0]["content"] for request in engine.requests[1:])
    assert engine.replaced[-2:] == original[-2:]
    assert cc.read_stopped({"HOME": str(tmp_path)}) == {}


def test_overflow_recovery_preserves_the_unanswered_turn_verbatim(tmp_path):
    pending = {"role": "user", "content": "尚未回答的問題", "time": 99}
    engine = CountedEngine(_history(3) + [pending], unit_tokens=10000)
    compactor = cc.Compactor(engine, cc.MODE_CODETRAIL, n_ctx=131072, env={"HOME": str(tmp_path)})
    outcome = compactor.compact(manual=True)
    assert outcome.status == "compacted", outcome
    assert engine.replaced[-1] == pending
    assert all(pending["content"] not in str(request) for request in engine.requests)
    assert not cc.last_user_answered(engine.messages)


def test_summary_request_errors_are_retryable_without_a_durable_stop(tmp_path):
    engine = CountedEngine(unit_tokens=1000)
    engine.failure = RuntimeError("transport failed before a summary was received")
    original = copy.deepcopy(engine.messages)
    env = {"HOME": str(tmp_path)}
    compactor = cc.Compactor(engine, cc.MODE_CODETRAIL, n_ctx=131072, env=env)
    outcome = compactor.compact(manual=True)
    assert outcome.status == "failed", outcome
    assert engine.messages == original and engine.replaced is None
    assert compactor.stopped_detail is None and compactor.last_anchor is None
    assert cc.read_stopped(env) == {}


@pytest.mark.parametrize("failure", [RuntimeError("second batch failed"), client_events.TurnCancelled("cancel")])
def test_a_later_batch_failure_never_installs_a_partial_summary(tmp_path, failure):
    engine = CountedEngine()
    engine.failure = failure
    engine.fail_on_call = 2
    original = copy.deepcopy(engine.messages)
    env = {"HOME": str(tmp_path)}
    compactor = cc.Compactor(engine, cc.MODE_CODETRAIL, n_ctx=131072, env=env)
    outcome = compactor.compact(manual=True)
    assert len(engine.requests) == 2
    assert outcome.status == ("skipped" if isinstance(failure, client_events.TurnCancelled) else "failed")
    assert engine.messages == original and engine.replaced is None
    assert compactor.previous_summary == "" and compactor.last_anchor is None
    assert cc.read_stopped(env) == {}


def test_a_replacement_that_still_overflows_never_changes_history(tmp_path):
    engine = CountedEngine(unit_tokens=1000)
    engine.replacement_tokens = 123345
    original = copy.deepcopy(engine.messages)
    env = {"HOME": str(tmp_path)}
    compactor = cc.Compactor(engine, cc.MODE_CODETRAIL, n_ctx=131072, env=env)
    outcome = compactor.compact(manual=True)
    assert outcome.status == "failed", outcome
    assert engine.messages == original and engine.replaced is None
    assert compactor.last_anchor is None and cc.read_stopped(env) == {}
