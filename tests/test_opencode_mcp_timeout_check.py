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
    assert "--fix" in out


def test_mcp_timeout_fix_preserves_other_settings_and_creates_backup(
    monkeypatch, tmp_path, capsys
):
    config_path = tmp_path / "opencode.json"
    original = {
        "model": "llamacpp/local-model",
        "mcp": {
            "codetrail": {
                "type": "local",
                "enabled": True,
                "timeout": 10_000,
            },
            "another-server": {"type": "remote", "url": "http://localhost:9999"},
        },
        "permission": {"*": "deny"},
    }
    config_path.write_text(
        json.dumps(original, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.delenv(check.SKIP_ENV, raising=False)

    assert check.main(["--fix"]) == 0

    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["mcp"]["codetrail"]["timeout"] == check.config.OPENCODE_MCP_TIMEOUT_MIN_MS
    assert updated["model"] == original["model"]
    assert updated["mcp"]["another-server"] == original["mcp"]["another-server"]
    assert updated["permission"] == original["permission"]

    backup = config_path.with_name(config_path.name + check.BACKUP_SUFFIX)
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    assert "FIXED" in capsys.readouterr().out


def test_mcp_timeout_fix_repairs_invalid_value(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    config_path.write_text(
        json.dumps({"mcp": {"codetrail": {"timeout": "10 seconds"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.delenv(check.SKIP_ENV, raising=False)

    assert check.main(["--fix"]) == 0
    assert (
        json.loads(config_path.read_text(encoding="utf-8"))["mcp"]["codetrail"][
            "timeout"
        ]
        == check.config.OPENCODE_MCP_TIMEOUT_MIN_MS
    )
    assert "FIXED" in capsys.readouterr().out


def test_mcp_timeout_fix_adds_missing_value(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    config_path.write_text(
        json.dumps({"mcp": {"codetrail": {"type": "local"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.delenv(check.SKIP_ENV, raising=False)

    assert check.main(["--fix"]) == 0
    assert (
        json.loads(config_path.read_text(encoding="utf-8"))["mcp"]["codetrail"][
            "timeout"
        ]
        == check.config.OPENCODE_MCP_TIMEOUT_MIN_MS
    )
    assert "FIXED" in capsys.readouterr().out


def test_mcp_timeout_fix_does_not_rewrite_safe_config(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "opencode.json"
    _write_config(config_path, check.config.OPENCODE_MCP_TIMEOUT_MIN_MS)
    before = config_path.read_bytes()
    monkeypatch.setenv("OPENCODE_CONFIG", str(config_path))
    monkeypatch.delenv(check.SKIP_ENV, raising=False)

    assert check.main(["--fix"]) == 0
    assert config_path.read_bytes() == before
    assert not config_path.with_name(config_path.name + check.BACKUP_SUFFIX).exists()
    assert "SAFE" in capsys.readouterr().out


def test_mcp_timeout_fix_keeps_config_symlink(monkeypatch, tmp_path):
    target = tmp_path / "real-opencode.json"
    link = tmp_path / "opencode.json"
    _write_config(target, 10_000)
    link.symlink_to(target)
    monkeypatch.setenv("OPENCODE_CONFIG", str(link))
    monkeypatch.delenv(check.SKIP_ENV, raising=False)

    assert check.main(["--fix"]) == 0
    assert link.is_symlink()
    assert (
        json.loads(target.read_text(encoding="utf-8"))["mcp"]["codetrail"]["timeout"]
        == check.config.OPENCODE_MCP_TIMEOUT_MIN_MS
    )


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
