"""CLI behavior for scripts/resolve_main_model.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import resolve_main_model as rmm  # noqa: E402
from model_resolution import normalize_main_model  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("AICODE_MODEL", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    yield


def _write_home_opencode(tmp_path: Path, model: str = "llamacpp/from-json") -> Path:
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    path = cfg_dir / "opencode.json"
    path.write_text(json.dumps({"model": model}), encoding="utf-8")
    return path


def test_env_and_opencode_json_same_model_allowed(monkeypatch, tmp_path, capsys):
    _write_home_opencode(tmp_path, "llamacpp/from-env")
    monkeypatch.setenv("AICODE_MODEL", "from-env")

    assert rmm.main([]) == 0
    assert capsys.readouterr().out.strip() == "from-env"


def test_env_and_opencode_json_conflict_fails(monkeypatch, tmp_path, capsys):
    _write_home_opencode(tmp_path, "llamacpp/from-json")
    monkeypatch.setenv("AICODE_MODEL", "from-env")

    rc = rmm.main([])

    assert rc == 2
    err = capsys.readouterr().err
    assert "opencode.json" in err
    assert "different models" in err


def test_cli_model_may_override_opencode_json(monkeypatch, tmp_path, capsys):
    _write_home_opencode(tmp_path, "llamacpp/from-json")
    monkeypatch.setenv("AICODE_MODEL", "from-cli")

    assert rmm.main(["--model", "llamacpp/from-cli"]) == 0
    assert capsys.readouterr().out.strip() == "from-cli"


def test_argv_overrides_opencode_json_when_env_missing(tmp_path, capsys):
    _write_home_opencode(tmp_path, "llamacpp/from-json")

    assert rmm.main(["-m", "from-arg"]) == 0
    assert capsys.readouterr().out.strip() == "from-arg"


def test_env_and_argv_same_model_allowed(monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "same-model")

    assert rmm.main(["--model", "same-model"]) == 0
    assert capsys.readouterr().out.strip() == "same-model"


def test_argv_with_custom_provider_prefix_strips_to_bare(monkeypatch, capsys):
    """OpenCode 風格的 myprovider/bare 形式應該 strip 成 bare。"""
    monkeypatch.setenv("AICODE_MODEL", "foo-bar")

    assert rmm.main(["--model", "llamacpp/foo-bar"]) == 0
    assert capsys.readouterr().out.strip() == "foo-bar"


def test_env_and_argv_conflict_fails(monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "env-model")

    rc = rmm.main(["--model", "cli-model"])

    assert rc == 2
    assert "different models" in capsys.readouterr().err


def test_argv_equals_form(capsys):
    assert rmm.main(["--model=llamacpp/foo-bar"]) == 0
    assert capsys.readouterr().out.strip() == "foo-bar"


def test_argv_missing_value_fails_loud(capsys):
    rc = rmm.main(["--model"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "requires a model value" in err


def test_short_argv_missing_value_fails_loud(capsys):
    rc = rmm.main(["-m"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "requires a model value" in err


def test_argv_missing_value_before_other_flag_fails_loud(capsys):
    rc = rmm.main(["--model", "--foo"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "requires a model value" in err


def test_argv_bare_model_name(capsys):
    assert rmm.main(["-m", "foo-bar"]) == 0
    assert capsys.readouterr().out.strip() == "foo-bar"


def test_argv_gguf_path(capsys):
    """GGUF 絕對路徑也是合法的主模型形式。"""
    assert rmm.main(["-m", "/models/foo.gguf"]) == 0
    assert capsys.readouterr().out.strip() == "/models/foo.gguf"


def test_argv_rejects_external_provider(capsys):
    """openai/ ollama/ anthropic/ 等外部 provider prefix 必須拒絕。"""
    for value, hint in [
        ("openai/gpt-4", "openai/"),
        ("ollama/qwen3", "ollama/"),
        ("anthropic/claude", "anthropic/"),
    ]:
        rc = rmm.main(["-m", value])
        assert rc == 2, f"應該拒絕 {value!r}"
        err = capsys.readouterr().err
        assert "外部 provider" in err or "provider prefix" in err


def test_normalize_main_model_strips_custom_provider():
    """custom-provider/bare 形式被 strip 成 bare。"""
    assert normalize_main_model("llamacpp/foo-bar", "test").model == "foo-bar"
    assert normalize_main_model("myprovider/some-model", "test").model == "some-model"


def test_normalize_main_model_accepts_gguf_path():
    assert normalize_main_model("/models/foo.gguf", "test").model == "/models/foo.gguf"
    assert normalize_main_model("~/models/foo.gguf", "test").model == "~/models/foo.gguf"


def test_normalize_main_model_rejects_known_external_providers():
    assert normalize_main_model("openai/gpt-4.1", "test").error
    assert normalize_main_model("anthropic/something", "test").error
    assert normalize_main_model("ollama/qwen3", "test").error


def test_env_rejects_external_provider(monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "anthropic/something")

    rc = rmm.main([])

    assert rc == 2
    err = capsys.readouterr().err
    assert "外部 provider" in err or "provider prefix" in err


def test_opencode_json_fallback_when_neither_env_nor_argv(tmp_path, capsys):
    _write_home_opencode(tmp_path, "llamacpp/from-json")

    assert rmm.main([]) == 0
    assert capsys.readouterr().out.strip() == "from-json"


def test_opencode_config_env_path_is_used(monkeypatch, tmp_path, capsys):
    _write_home_opencode(tmp_path, "llamacpp/home-model")
    custom = tmp_path / "custom-opencode.json"
    custom.write_text(json.dumps({"model": "llamacpp/custom-model"}), encoding="utf-8")
    monkeypatch.setenv("OPENCODE_CONFIG", str(custom))

    assert rmm.main([]) == 0
    assert capsys.readouterr().out.strip() == "custom-model"


def test_opencode_json_rejects_external_provider(tmp_path, capsys):
    _write_home_opencode(tmp_path, "anthropic/something")

    rc = rmm.main([])

    assert rc == 2
    err = capsys.readouterr().err
    assert "外部 provider" in err or "provider prefix" in err


def test_opencode_json_bare_model_also_accepted(tmp_path, capsys):
    """opencode.json 不再強制 require ollama/ 前綴(或任何 prefix);bare 也接受。"""
    _write_home_opencode(tmp_path, "just-bare-name")

    assert rmm.main([]) == 0
    assert capsys.readouterr().out.strip() == "just-bare-name"


def test_placeholder_in_env_fails(monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "<CODE_MODEL>")

    rc = rmm.main([])

    assert rc == 2
    assert "placeholder" in capsys.readouterr().err


def test_placeholder_in_opencode_json_fails(tmp_path, capsys):
    _write_home_opencode(tmp_path, "llamacpp/<CODE_MODEL>")

    rc = rmm.main([])

    assert rc == 2
    assert "placeholder" in capsys.readouterr().err


def test_no_source_at_all_fails_loud(capsys):
    rc = rmm.main([])

    assert rc == 2
    err = capsys.readouterr().err
    assert "AICODE_MODEL" in err
    assert "opencode.json" in err


def test_empty_string_treated_as_unset(monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "   ")

    assert rmm.main([]) == 2


def test_malformed_opencode_json_fails_loud(tmp_path, capsys):
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text("not json", encoding="utf-8")

    assert rmm.main([]) == 2
    assert "opencode.json" in capsys.readouterr().err
