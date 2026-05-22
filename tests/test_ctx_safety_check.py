"""scripts/ctx_safety_check.py 的 CLI 行為測試。"""
from __future__ import annotations

from gpu_safety import SafetyVerdict
from scripts import ctx_safety_check as ctx


def _unknown_verdict(requested: int) -> SafetyVerdict:
    return SafetyVerdict(
        status="UNKNOWN",
        requested_ctx=requested,
        server_n_ctx=None,
        model_path=None,
        vram_total_gb=None,
        vram_free_gb=None,
        reason="test unknown",
    )


def test_ctx_safety_fails_loud_when_env_missing(monkeypatch, capsys):
    """CodeTrail 不內建主模型: AICODE_MODEL 未設時必須 fail-loud (exit 2)。"""
    monkeypatch.delenv("AICODE_MODEL", raising=False)
    monkeypatch.delenv("AICODE_DYNAMIC_NUM_CTX_MAX", raising=False)
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)

    called = {"hit": False}

    def fake_check_safety(*args, **kwargs):
        called["hit"] = True
        return _unknown_verdict(0)

    monkeypatch.setattr(ctx.gpu_safety, "check_safety", fake_check_safety)

    assert ctx.main() == 2
    assert called["hit"] is False, "AICODE_MODEL 未設時不該呼叫 check_safety"
    out = capsys.readouterr().out
    assert "AICODE_MODEL 未設" in out
    assert "refuse to start" in out


def test_ctx_safety_fails_loud_on_placeholder_model(monkeypatch, capsys):
    """值是 `<CODE_MODEL>` 之類 placeholder 也要 fail-loud。"""
    monkeypatch.setenv("AICODE_MODEL", "<CODE_MODEL>")
    monkeypatch.delenv("AICODE_DYNAMIC_NUM_CTX_MAX", raising=False)
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)

    called = {"hit": False}

    def fake_check_safety(*args, **kwargs):
        called["hit"] = True
        return _unknown_verdict(0)

    monkeypatch.setattr(ctx.gpu_safety, "check_safety", fake_check_safety)

    assert ctx.main() == 2
    assert called["hit"] is False
    out = capsys.readouterr().out
    assert "placeholder" in out


def test_ctx_safety_disable_short_circuits_even_without_model(monkeypatch, capsys):
    """AICODE_CTX_SAFETY_DISABLE=1 時, 即使沒設 AICODE_MODEL 也 exit 0 (CI / 緊急逃生)。"""
    monkeypatch.delenv("AICODE_MODEL", raising=False)
    monkeypatch.setenv("AICODE_CTX_SAFETY_DISABLE", "1")
    assert ctx.main() == 0
    out = capsys.readouterr().out
    assert "disabled via AICODE_CTX_SAFETY_DISABLE" in out


def test_ctx_safety_uses_resolved_model_from_env(monkeypatch):
    """新版 check_safety(requested_ctx, base_url=...) 簽名 — 不再吃 model。
    這個 test 確認 ctx_safety_check.main 走到 gpu_safety.check_safety 並帶入正確
    requested ctx 與 base_url。
    """
    monkeypatch.setenv("AICODE_MODEL", "custom-model")
    monkeypatch.delenv("AICODE_DYNAMIC_NUM_CTX_MAX", raising=False)
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)
    monkeypatch.setenv("AICODE_LLAMA_BASE_URL", "http://example.test:8080")

    calls: dict[str, object] = {}

    def fake_check_safety(requested, base_url="http://localhost:8080", **_kw):
        calls["requested"] = requested
        calls["base_url"] = base_url
        return _unknown_verdict(requested)

    monkeypatch.setattr(ctx.gpu_safety, "check_safety", fake_check_safety)

    assert ctx.main() == 0
    assert calls["requested"] == ctx.DEFAULT_CTX_MAX
    assert calls["base_url"] == "http://example.test:8080"
