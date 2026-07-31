from __future__ import annotations

import json

from scripts import opencode_mcp_timeout_check as check


def _write_config(path, timeout):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"mcp": {"codetrail": {"timeout": timeout}}}),
        encoding="utf-8",
    )


def test_mcp_timeout_check_accepts_documented_value(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    _write_config(config_path, check.config.OPENCODE_MCP_TIMEOUT_MIN_MS)
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.delenv(check.SKIP_ENV, raising=False)

    assert check.main() == 0
    assert "SAFE" in capsys.readouterr().out


def test_mcp_timeout_check_rejects_ten_seconds(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    _write_config(config_path, 10_000)
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.delenv(check.SKIP_ENV, raising=False)

    assert check.main() == 2
    out = capsys.readouterr().out
    assert "TOO_SHORT" in out
    assert "660000" in out
    assert "完全退出後重開 aicode" in out


def test_mcp_timeout_check_skips_missing_entry(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    config_path.write_text(json.dumps({"model": "local/model"}), encoding="utf-8")
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.delenv(check.SKIP_ENV, raising=False)

    assert check.main() == 0
    assert "UNKNOWN" in capsys.readouterr().out


def test_mcp_timeout_check_explicit_skip(monkeypatch, capsys):
    monkeypatch.setenv(check.SKIP_ENV, "1")

    assert check.main() == 0
    assert "skipped" in capsys.readouterr().out
