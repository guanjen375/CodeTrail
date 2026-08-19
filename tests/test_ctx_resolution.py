"""n_ctx 的解析與正規化:config 的多來源優先序,以及 server ctx 自動偵測。

合併自 tests/test_n_ctx.py 與 tests/test_resolve_server_ctx.py(2026-08-20)。
"""
from __future__ import annotations

import pytest

import n_ctx
from scripts import resolve_server_ctx


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


# --------------------------------------------------------------------------
# 併自 tests/test_resolve_server_ctx.py:scripts/resolve_server_ctx.py。
# --------------------------------------------------------------------------
def test_prints_server_n_ctx(monkeypatch, capsys):
    server = resolve_server_ctx.gpu_safety.ServerInfo(
        base_url="http://localhost:8080",
        n_ctx=65536,
    )
    monkeypatch.setattr(
        resolve_server_ctx.gpu_safety,
        "query_server_info",
        lambda _url: server,
    )

    assert resolve_server_ctx.main() == 0
    captured = capsys.readouterr()
    assert captured.out == "65536\n"
    assert captured.err == ""


def test_missing_server_is_non_blocking_and_prints_no_value(monkeypatch, capsys):
    monkeypatch.setattr(
        resolve_server_ctx.gpu_safety,
        "query_server_info",
        lambda _url: None,
    )

    assert resolve_server_ctx.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "無法" in captured.err


def test_query_exception_is_non_blocking_and_prints_no_value(monkeypatch, capsys):
    def fail(_url):
        raise OSError("offline")

    monkeypatch.setattr(resolve_server_ctx.gpu_safety, "query_server_info", fail)

    assert resolve_server_ctx.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "offline" in captured.err
