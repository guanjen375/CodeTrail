"""scripts/ctx_safety_check.py 的 CLI 行為測試。"""
from __future__ import annotations

import config as cfg
from gpu_safety import SafetyVerdict
from scripts import ctx_safety_check as ctx


def _unknown_verdict(requested: int) -> SafetyVerdict:
    return SafetyVerdict(
        status="UNKNOWN",
        requested_ctx=requested,
        computed_max_ctx=None,
        weights_gb=None,
        kv_per_token_kb=None,
        vram_needed_gb=None,
        vram_total_gb=None,
        headroom_gb=2.0,
        reason="test unknown",
    )


def test_ctx_safety_default_model_matches_config():
    assert ctx.DEFAULT_MODEL == cfg.DEFAULT_MODEL


def test_ctx_safety_uses_default_model_when_env_missing(monkeypatch, capsys):
    monkeypatch.delenv("AICODE_MODEL", raising=False)
    monkeypatch.delenv("AICODE_DYNAMIC_NUM_CTX_MAX", raising=False)
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)

    calls: dict[str, object] = {}

    def fake_check_safety(model: str, requested: int, *, base_url: str):
        calls["model"] = model
        calls["requested"] = requested
        calls["base_url"] = base_url
        return _unknown_verdict(requested)

    monkeypatch.setattr(ctx.gpu_safety, "check_safety", fake_check_safety)

    assert ctx.main() == 0
    assert calls == {
        "model": ctx.DEFAULT_MODEL,
        "requested": ctx.DEFAULT_CTX_MAX,
        "base_url": "http://localhost:11434",
    }
    out = capsys.readouterr().out
    assert "AICODE_MODEL 未設,使用預設模型" in out
    assert "跳過 ctx 安全檢查" not in out


def test_ctx_safety_env_model_still_overrides_default(monkeypatch):
    monkeypatch.setenv("AICODE_MODEL", "custom:latest")
    monkeypatch.delenv("AICODE_DYNAMIC_NUM_CTX_MAX", raising=False)
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)

    calls: dict[str, object] = {}

    def fake_check_safety(model: str, requested: int, *, base_url: str):
        calls["model"] = model
        calls["requested"] = requested
        return _unknown_verdict(requested)

    monkeypatch.setattr(ctx.gpu_safety, "check_safety", fake_check_safety)

    assert ctx.main() == 0
    assert calls["model"] == "custom:latest"
