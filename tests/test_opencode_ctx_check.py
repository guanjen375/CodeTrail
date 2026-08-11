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
    monkeypatch.setenv("AICODE_N_CTX", "65536")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)

    assert occ.main([]) == 0
    assert "SAFE" in capsys.readouterr().out


def test_opencode_ctx_check_fails_when_context_mismatches(monkeypatch, tmp_path, capsys):
    _write_opencode(tmp_path, 32768)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_N_CTX", "65536")
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
    monkeypatch.setenv("AICODE_N_CTX", "65536")
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
    monkeypatch.setenv("AICODE_N_CTX", "65536")
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)

    assert occ.main(["--model", "llamacpp/other-model"]) == 2
    assert "other-model" in capsys.readouterr().out


def test_fix_syncs_only_active_context_and_creates_backup(monkeypatch, tmp_path, capsys):
    path = _write_opencode(tmp_path, 32768)
    original = json.loads(path.read_text(encoding="utf-8"))
    original["theme"] = "custom-theme"
    original["provider"]["llamacpp"]["models"]["other-model"] = {
        "limit": {"context": 8192, "output": 2048}
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_N_CTX", "65536")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)

    assert occ.main(["--fix"]) == 0

    updated = json.loads(path.read_text(encoding="utf-8"))
    models = updated["provider"]["llamacpp"]["models"]
    assert models["main-model"]["limit"] == {"context": 65536, "output": 8192}
    assert models["other-model"] == original["provider"]["llamacpp"]["models"]["other-model"]
    assert updated["theme"] == "custom-theme"
    backup = path.with_name(path.name + occ.BACKUP_SUFFIX)
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    assert "FIXED" in capsys.readouterr().out


def test_fix_adds_missing_context_without_replacing_limit(monkeypatch, tmp_path):
    path = _write_opencode(tmp_path, 32768)
    data = json.loads(path.read_text(encoding="utf-8"))
    limit = data["provider"]["llamacpp"]["models"]["main-model"]["limit"]
    del limit["context"]
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_N_CTX", "49152")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)

    assert occ.main(["--fix"]) == 0
    updated_limit = json.loads(path.read_text(encoding="utf-8"))["provider"]["llamacpp"][
        "models"
    ]["main-model"]["limit"]
    assert updated_limit == {"context": 49152, "output": 8192}


def test_fix_respects_accept_risk_without_writing(monkeypatch, tmp_path):
    path = _write_opencode(tmp_path, 32768)
    before = path.read_bytes()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_N_CTX", "65536")
    monkeypatch.setenv("AICODE_ACCEPT_CTX_RISK", "1")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)

    assert occ.main(["--fix"]) == 0
    assert path.read_bytes() == before
    assert not path.with_name(path.name + occ.BACKUP_SUFFIX).exists()


def test_fix_keeps_opencode_config_symlink(monkeypatch, tmp_path):
    real_home = tmp_path / "real-home"
    target = _write_opencode(real_home, 32768)
    link = tmp_path / "opencode.json"
    link.symlink_to(target)
    monkeypatch.setenv("OPENCODE_CONFIG", str(link))
    monkeypatch.setenv("AICODE_N_CTX", "65536")
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)

    assert occ.main(["--fix"]) == 0
    assert link.is_symlink()
    updated = json.loads(target.read_text(encoding="utf-8"))
    assert updated["provider"]["llamacpp"]["models"]["main-model"]["limit"]["context"] == 65536


def test_fix_refuses_ambiguous_model_entries_without_writing(monkeypatch, tmp_path, capsys):
    path = _write_opencode(tmp_path, 32768, model="main-model")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["provider"]["second"] = {
        "models": {
            "main-model": {
                "name": "main-model",
                "limit": {"context": 32768},
            }
        }
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AICODE_N_CTX", "65536")
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)

    assert occ.main(["--fix"]) == 2
    assert path.read_bytes() == before
    assert "multiple matching" in capsys.readouterr().out
    assert not path.with_name(path.name + occ.BACKUP_SUFFIX).exists()
