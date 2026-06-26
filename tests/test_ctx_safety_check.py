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


def _server_verdict_factory(status: str, server_n_ctx: int):
    """回一個假 check_safety:固定 server_n_ctx,status 由呼叫端指定。

    gate 是拿 env 推出的 requested 跟 verdict.server_n_ctx 比,所以這裡只要把
    server_n_ctx 釘住,測試端用 AICODE_DYNAMIC_NUM_CTX_MAX 控制 requested 即可。
    """

    def fake_check_safety(requested, base_url="http://localhost:8080", **_kw):
        return SafetyVerdict(
            status=status,
            requested_ctx=requested,
            server_n_ctx=server_n_ctx,
            model_path="/models/x.gguf",
            vram_total_gb=None,
            vram_free_gb=None,
            reason="test reason",
            detail_lines=[f"Server n_ctx (啟動時 -c): {server_n_ctx}"],
        )

    return fake_check_safety


def test_ctx_safety_passes_when_requested_equals_server(monkeypatch, capsys):
    """requested == server n_ctx → SAFE,放行 (exit 0)。"""
    monkeypatch.setenv("AICODE_MODEL", "custom-model")
    monkeypatch.setenv("AICODE_DYNAMIC_NUM_CTX_MAX", "65536")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)
    monkeypatch.setattr(
        ctx.gpu_safety, "check_safety", _server_verdict_factory("SAFE", 65536)
    )

    assert ctx.main() == 0
    out = capsys.readouterr().out
    assert "SAFE" in out
    assert "<= server n_ctx=65536" in out


def test_ctx_safety_passes_when_requested_below_server(monkeypatch, capsys):
    """requested < server n_ctx → SAFE,放行 (exit 0)。

    「小於」不是安全問題(不截斷,只是沒用滿 server 容量),不該擋。正常情況下
    aicode 會自動把 requested 帶成 == server,這條主要保障使用者手動設小一點時
    不會被無謂擋住,也是把舊版 e129d48「小於就 refuse」死鎖拿掉的回歸測試。
    """
    monkeypatch.setenv("AICODE_MODEL", "custom-model")
    monkeypatch.setenv("AICODE_DYNAMIC_NUM_CTX_MAX", "32768")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)
    monkeypatch.setattr(
        ctx.gpu_safety, "check_safety", _server_verdict_factory("SAFE", 65536)
    )

    assert ctx.main() == 0
    out = capsys.readouterr().out
    assert "SAFE" in out
    assert "<= server n_ctx=65536" in out
    assert "refuse to start" not in out


def test_ctx_safety_unsafe_allows_with_accept_risk(monkeypatch, capsys):
    """requested > server n_ctx 但設了 AICODE_ACCEPT_CTX_RISK=1 → 放行 (exit 0)。"""
    monkeypatch.setenv("AICODE_MODEL", "custom-model")
    monkeypatch.setenv("AICODE_DYNAMIC_NUM_CTX_MAX", "65536")
    monkeypatch.setenv("AICODE_ACCEPT_CTX_RISK", "1")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)
    monkeypatch.setattr(
        ctx.gpu_safety, "check_safety", _server_verdict_factory("UNSAFE", 8192)
    )

    assert ctx.main() == 0
    out = capsys.readouterr().out
    assert "UNSAFE" in out
    assert "AICODE_ACCEPT_CTX_RISK=1 已設" in out


def test_ctx_safety_unsafe_when_requested_above_server(monkeypatch, capsys):
    """requested > server n_ctx → UNSAFE (截斷風險),擋住 (exit 2)。"""
    monkeypatch.setenv("AICODE_MODEL", "custom-model")
    monkeypatch.setenv("AICODE_DYNAMIC_NUM_CTX_MAX", "65536")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)
    monkeypatch.setattr(
        ctx.gpu_safety, "check_safety", _server_verdict_factory("UNSAFE", 8192)
    )

    assert ctx.main() == 2
    out = capsys.readouterr().out
    assert "UNSAFE" in out
    assert "8192" in out
    # 主要修法:拿掉手動的 AICODE_DYNAMIC_NUM_CTX_MAX 讓 CodeTrail 自動跟隨 server
    assert "自動跟隨 server" in out
    assert "refuse to start" in out


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
