"""CLI behavior for scripts/opencode_ctx_check.py."""
from __future__ import annotations

import json
from pathlib import Path

from scripts import opencode_ctx_check as occ


def _write_opencode(tmp_path: Path, ctx: int, model: str = "llamacpp/main-model") -> Path:
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    path = cfg_dir / "opencode.json"
    path.write_text(
        json.dumps(
            {
                "model": model,
                "provider": {
                    "llamacpp": {
                        "models": {
                            "main-model": {
                                "name": "main-model",
                                "limit": {"context": ctx, "output": 8192},
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_opencode_ctx_check_passes_when_context_matches(monkeypatch, tmp_path, capsys):
    _write_opencode(tmp_path, 65536)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_DYNAMIC_NUM_CTX_MAX", "65536")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)

    assert occ.main([]) == 0
    assert "SAFE" in capsys.readouterr().out


def test_opencode_ctx_check_fails_when_context_mismatches(monkeypatch, tmp_path, capsys):
    _write_opencode(tmp_path, 32768)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_DYNAMIC_NUM_CTX_MAX", "65536")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)

    assert occ.main([]) == 2
    out = capsys.readouterr().out
    assert "MISMATCH" in out
    assert "32768" in out
    assert "65536" in out


def test_opencode_ctx_check_accept_risk_allows_mismatch(monkeypatch, tmp_path):
    _write_opencode(tmp_path, 32768)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_DYNAMIC_NUM_CTX_MAX", "65536")
    monkeypatch.setenv("AICODE_ACCEPT_CTX_RISK", "1")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)

    assert occ.main([]) == 0


def test_opencode_ctx_check_uses_cli_model_entry(monkeypatch, tmp_path, capsys):
    _write_opencode(tmp_path, 65536, model="llamacpp/other-model")
    cfg = tmp_path / ".config" / "opencode" / "opencode.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["provider"]["llamacpp"]["models"]["main-model"]["limit"]["context"] = 65536
    data["provider"]["llamacpp"]["models"]["other-model"] = {
        "name": "other-model",
        "limit": {"context": 32768, "output": 8192},
    }
    cfg.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_DYNAMIC_NUM_CTX_MAX", "65536")
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)

    assert occ.main(["--model", "llamacpp/other-model"]) == 2
    assert "other-model" in capsys.readouterr().out
