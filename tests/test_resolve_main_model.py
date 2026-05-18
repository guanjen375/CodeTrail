"""CLI behavior for scripts/resolve_main_model.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import resolve_main_model as rmm  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("AICODE_MODEL", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    yield


def _write_home_opencode(tmp_path: Path, model: str = "ollama/from-json:tag") -> Path:
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    path = cfg_dir / "opencode.json"
    path.write_text(json.dumps({"model": model}), encoding="utf-8")
    return path


def test_env_takes_priority_over_opencode_json(monkeypatch, tmp_path, capsys):
    _write_home_opencode(tmp_path, "ollama/from-json:tag")
    monkeypatch.setenv("AICODE_MODEL", "from-env:tag")

    assert rmm.main([]) == 0
    assert capsys.readouterr().out.strip() == "from-env:tag"


def test_argv_overrides_opencode_json_when_env_missing(tmp_path, capsys):
    _write_home_opencode(tmp_path, "ollama/from-json:tag")

    assert rmm.main(["-m", "ollama/from-arg:tag"]) == 0
    assert capsys.readouterr().out.strip() == "from-arg:tag"


def test_env_and_argv_same_model_allowed(monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "same-model:tag")

    assert rmm.main(["--model", "ollama/same-model:tag"]) == 0
    assert capsys.readouterr().out.strip() == "same-model:tag"


def test_env_and_argv_conflict_fails(monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "env-model:tag")

    rc = rmm.main(["--model", "ollama/cli-model:tag"])

    assert rc == 2
    assert "different models" in capsys.readouterr().err


def test_argv_equals_form(capsys):
    assert rmm.main(["--model=ollama/foo:bar"]) == 0
    assert capsys.readouterr().out.strip() == "foo:bar"


def test_argv_bare_ollama_name(capsys):
    assert rmm.main(["-m", "foo:bar"]) == 0
    assert capsys.readouterr().out.strip() == "foo:bar"


def test_argv_namespaced_ollama_name_kept(capsys):
    assert rmm.main(["-m", "some-org/model:tag"]) == 0
    assert capsys.readouterr().out.strip() == "some-org/model:tag"


def test_argv_rejects_non_ollama_provider(capsys):
    rc = rmm.main(["-m", "openai/gpt-4"])

    assert rc == 2
    assert "non-Ollama provider" in capsys.readouterr().err


def test_env_rejects_non_ollama_provider(monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "anthropic/claude-sonnet-4")

    rc = rmm.main([])

    assert rc == 2
    assert "non-Ollama provider" in capsys.readouterr().err


def test_opencode_json_fallback_when_neither_env_nor_argv(tmp_path, capsys):
    _write_home_opencode(tmp_path, "ollama/from-json:tag")

    assert rmm.main([]) == 0
    assert capsys.readouterr().out.strip() == "from-json:tag"


def test_opencode_config_env_path_is_used(monkeypatch, tmp_path, capsys):
    _write_home_opencode(tmp_path, "ollama/home-model:tag")
    custom = tmp_path / "custom-opencode.json"
    custom.write_text(json.dumps({"model": "ollama/custom-model:tag"}), encoding="utf-8")
    monkeypatch.setenv("OPENCODE_CONFIG", str(custom))

    assert rmm.main([]) == 0
    assert capsys.readouterr().out.strip() == "custom-model:tag"


def test_opencode_json_rejects_non_ollama_provider(tmp_path, capsys):
    _write_home_opencode(tmp_path, "anthropic/claude-sonnet-4")

    rc = rmm.main([])

    assert rc == 2
    assert 'must be "ollama/<MODEL>"' in capsys.readouterr().err


def test_opencode_json_requires_ollama_prefix_for_namespaced_model(tmp_path, capsys):
    _write_home_opencode(tmp_path, "some-org/model:tag")

    rc = rmm.main([])

    assert rc == 2
    assert 'must be "ollama/<MODEL>"' in capsys.readouterr().err


def test_placeholder_in_env_fails(monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "<CODE_MODEL>")

    rc = rmm.main([])

    assert rc == 2
    assert "placeholder" in capsys.readouterr().err


def test_placeholder_in_opencode_json_fails(tmp_path, capsys):
    _write_home_opencode(tmp_path, "ollama/<CODE_MODEL>")

    rc = rmm.main([])

    assert rc == 2
    assert "placeholder" in capsys.readouterr().err


def test_no_source_at_all_fails_loud(capsys):
    rc = rmm.main([])

    assert rc == 2
    err = capsys.readouterr().err
    assert "AICODE_MODEL" in err
    assert "opencode.json" in err
    assert "<CODE_MODEL>" in err


def test_empty_string_treated_as_unset(monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "   ")

    assert rmm.main([]) == 2


def test_strip_ollama_prefix_only_strips_leading(monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "ollama/qllama/bge-reranker-v2-m3")

    assert rmm.main([]) == 0
    assert capsys.readouterr().out.strip() == "qllama/bge-reranker-v2-m3"


def test_malformed_opencode_json_fails_loud(tmp_path, capsys):
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text("not json", encoding="utf-8")

    assert rmm.main([]) == 2
    assert "opencode.json" in capsys.readouterr().err
