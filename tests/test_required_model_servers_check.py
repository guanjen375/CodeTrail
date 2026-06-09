from __future__ import annotations

from scripts import required_model_servers_check as preflight


def test_required_model_servers_all_pass(monkeypatch):
    monkeypatch.setattr(preflight.llama_client, "get_health", lambda url, timeout=3: {"status": "ok"})
    monkeypatch.setattr(preflight.llama_client, "embed_one", lambda **kwargs: [0.1, 0.2])
    monkeypatch.setattr(preflight.llama_client, "rerank", lambda **kwargs: [0.9, 0.1])
    monkeypatch.setattr(preflight.llama_client, "native_completion", lambda **kwargs: {"content": "ok"})

    checks = preflight.run_checks()

    assert all(check.ok for check in checks)
    assert {check.role for check in checks} == {"embedding", "reranker", "VL"}


def test_required_model_servers_fails_on_missing_health(monkeypatch):
    monkeypatch.setattr(preflight.llama_client, "get_health", lambda url, timeout=3: None)

    checks = preflight.run_checks()

    assert not any(check.ok for check in checks)
    assert all("health endpoint unreachable" in check.message for check in checks)
    report = "\n".join(preflight.render_report(checks))
    assert "refuse to start" in report


def test_required_model_servers_fails_role_probe(monkeypatch):
    monkeypatch.setattr(preflight.llama_client, "get_health", lambda url, timeout=3: {"status": "ok"})
    monkeypatch.setattr(preflight.llama_client, "embed_one", lambda **kwargs: [0.1, 0.2])
    monkeypatch.setattr(preflight.llama_client, "rerank", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(preflight.llama_client, "native_completion", lambda **kwargs: {"content": "ok"})

    checks = preflight.run_checks()

    by_role = {check.role: check for check in checks}
    assert by_role["embedding"].ok
    assert not by_role["reranker"].ok
    assert "boom" in by_role["reranker"].message
    assert by_role["VL"].ok
