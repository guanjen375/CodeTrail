"""The main-model context has one canonical value and safe legacy migration."""
from __future__ import annotations

import pytest

import n_ctx


def test_canonical_n_ctx_wins_over_legacy_values():
    resolved = n_ctx.resolve_n_ctx(
        {
            "AICODE_N_CTX": "49152",
            "AICODE_DYNAMIC_NUM_CTX_MAX": "32768",
            "AICODE_NUM_CTX": "131072",
        }
    )

    assert resolved.value == 49152
    assert resolved.source == "AICODE_N_CTX"
    assert resolved.legacy is False


def test_old_dynamic_max_remains_a_compatibility_alias():
    resolved = n_ctx.resolve_n_ctx({"AICODE_DYNAMIC_NUM_CTX_MAX": "32768"})

    assert resolved.value == 32768
    assert resolved.legacy is True


def test_stale_aicode_num_ctx_is_not_promoted_to_runtime_budget():
    resolved = n_ctx.resolve_n_ctx(
        {"AICODE_NUM_CTX": "131072"},
        default=65536,
        default_source="deployment profile main.ctx",
    )

    assert resolved.value == 65536
    assert resolved.source == "deployment profile main.ctx"


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "1048577"])
def test_invalid_canonical_n_ctx_is_rejected(raw):
    with pytest.raises(ValueError, match="AICODE_N_CTX"):
        n_ctx.resolve_n_ctx({"AICODE_N_CTX": raw})


def test_blank_canonical_env_uses_default():
    assert n_ctx.resolve_n_ctx({"AICODE_N_CTX": " "}).value == n_ctx.DEFAULT_N_CTX


def test_dynamic_sizing_never_exceeds_a_small_main_n_ctx(monkeypatch):
    import agent

    monkeypatch.setattr(agent, "N_CTX", 1024)
    monkeypatch.setattr(agent, "DYNAMIC_NUM_CTX_MIN", 16384)

    assert agent._compute_dynamic_num_ctx([{"role": "user", "content": "hello"}]) == 1024
