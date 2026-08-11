"""resolve_server_ctx 的 deterministic 單元測試。

Wrapper integration tests 會直接給定 AICODE_N_CTX，避免每一案都對
離線 port 重複等待；server /props → ctx 的契約集中在這裡完整驗證。
"""
from __future__ import annotations

from scripts import resolve_server_ctx


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
