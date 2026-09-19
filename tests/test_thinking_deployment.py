"""Thinking capability contracts: artifact evidence must survive setup and handoff."""
from __future__ import annotations

import builtins
import json
import struct
from dataclasses import replace
from pathlib import Path

import pytest

import deployment_profile as deployment
import template_capabilities
from scripts import set_config

pytestmark = pytest.mark.smoke

_ENABLE = "{% if enable_thinking is defined and enable_thinking is false %}off{% else %}on{% endif %}"
_THINKING = "{% if thinking %}on{% else %}off{% endif %}"
_DEEPSEEK = """
{%- if not thinking is defined -%}
  {%- if enable_thinking is defined -%}
    {%- set thinking = enable_thinking -%}
  {%- else -%}
    {%- set thinking = true -%}
  {%- endif -%}
{%- endif -%}
{%- if thinking -%}<think>{%- else -%}</think>{%- endif -%}
"""


def _string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack("<Q", len(encoded)) + encoded


def _write_templates(path: Path, default: str | None, tool_use: str | None = None) -> None:
    # Metadata-only GGUF is enough: detecting capability must not load weights.
    fields = [("tokenizer.chat_template", default),
              ("tokenizer.chat_template.tool_use", tool_use)]
    metadata = [_string(name) + struct.pack("<I", 8) + _string(value)
                for name, value in fields if value is not None]
    path.write_bytes(struct.pack("<4sIQQ", b"GGUF", 3, 0, len(metadata)) + b"".join(metadata))


def _plan(path: Path) -> set_config.Plan:
    gpu = set_config.Gpu(0, "synthetic", 8192, 8192, "GPU-synthetic")
    candidate = set_config.ModelCandidate(path, path.stat().st_size, 1)
    return set_config.Plan(
        gpus=[gpu],
        main=set_config.Selection("main", candidate, gpu),
        embedding=set_config.Selection("embedding", candidate, gpu),
        reranker=set_config.Selection("reranker", candidate, gpu),
        vl=set_config.Selection("vl", candidate, gpu, mmproj=path),
        main_key="synthetic-main", ctx=4096, batch=512, ubatch=128,
        reranker_ctx=8192, llama_bin="/synthetic/llama-server",
    )


@pytest.fixture(autouse=True)
def _never_execute_the_template(monkeypatch):
    from jinja2 import Environment

    def forbidden(*_args, **_kwargs):
        raise AssertionError("setup must only parse the template, never compile or render it")

    monkeypatch.setattr(Environment, "compile", forbidden)
    monkeypatch.setattr(Environment, "from_string", forbidden)


@pytest.mark.parametrize(("source", "expected"), [
    (_ENABLE, "enable_thinking"),
    (_THINKING, "thinking"),
    (_DEEPSEEK, "thinking"),
    ("{% set enable_thinking = enable_thinking|default(true) %}" + _ENABLE, "enable_thinking"),
    ("{{ unavailable_function() }}" + _ENABLE, "enable_thinking"),
    ("enable_thinking thinking", None),
    ("{# {% if enable_thinking %}on{% endif %} #}{{ 'thinking' }}", None),
    ("{% set enable_thinking = true %}" + _ENABLE, None),
    ("{% for thinking in messages %}{% if thinking %}on{% endif %}{% endfor %}", None),
    ("{% if enable_thinking is defined %}same for true and false{% endif %}", None),
    ("{% if enable_thinking|default(true, true) %}always on{% endif %}", None),
    ("{% if false and enable_thinking %}never{% endif %}", None),
    ("{% if false %}" + _ENABLE + "{% endif %}", None),
    ("{% if enable_thinking == 'yes' %}not a boolean switch{% endif %}", None),
    ("{% if enable_thinking %}", None),
    (None, None),
])
def test_setup_detects_only_external_template_switches(tmp_path, source, expected):
    # A misleading model name must not confer capability when the template does not.
    path = tmp_path / "DeepSeek-thinking-Qwen-enable_thinking.gguf"
    _write_templates(path, source)
    plan = _plan(path)
    config = set_config.build_deployment_config(plan)
    assert config["services"]["main"]["thinking_kwarg"] == expected
    assert any("thinking_kwarg=" + str(expected or "null") in note for note in plan.notes)
    if expected is None:
        assert any("/think on 不可用" in note for note in plan.notes)


@pytest.mark.parametrize(("default", "tool_use", "expected"), [
    (_DEEPSEEK, None, "thinking"),
    (_ENABLE, _DEEPSEEK, "thinking"),
    (_DEEPSEEK, _ENABLE, "enable_thinking"),
    (None, _DEEPSEEK, "thinking"),
    ("chatml", _DEEPSEEK, "thinking"),
    (_ENABLE, "{% if messages %}no switch{% endif %}", None),
    ("no switch", _ENABLE, None),
    (_ENABLE, "{% broken %}", None),
])
def test_setup_accepts_deepseek_alias_and_checks_both_selected_variants(
    tmp_path, default, tool_use, expected,
):
    path = tmp_path / "synthetic.gguf"
    _write_templates(path, default, tool_use)
    plan = _plan(path)
    assert set_config.build_deployment_config(plan)["services"]["main"]["thinking_kwarg"] == expected


def test_setup_bounds_template_metadata_and_requires_the_real_parser(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.gguf"
    key = _string("tokenizer.chat_template") + struct.pack("<I", 8)
    # Reject the length before allocating/reading it, even if an old profile claimed support.
    path.write_bytes(struct.pack("<4sIQQ", b"GGUF", 3, 0, 1) + key
                     + struct.pack("<Q", template_capabilities.MAX_TEMPLATE_BYTES + 1))
    plan = _plan(path)
    assert set_config.build_deployment_config(plan)["services"]["main"]["thinking_kwarg"] is None
    assert any("長度異常" in note for note in plan.notes)
    # A seek over a truncated metadata array must not reach a purported later template.
    path.write_bytes(struct.pack("<4sIQQ", b"GGUF", 3, 0, 2)
                     + _string("tokenizer.ggml.scores") + struct.pack("<IIQ", 9, 6, 100_000_000))
    plan = _plan(path)
    assert set_config.build_deployment_config(plan)["services"]["main"]["thinking_kwarg"] is None
    assert any("有界讀取上限" in note for note in plan.notes)
    _write_templates(path, _ENABLE)
    original_import = builtins.__import__

    def missing_parser(name, *args, **kwargs):
        if name == "jinja2":
            raise ImportError("synthetic missing parser")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_parser)
    with pytest.raises(set_config.SetupError, match="Jinja2"):
        set_config.build_deployment_config(_plan(path))


def test_reconfigure_replaces_stale_thinking_capability(tmp_path):
    path = tmp_path / "selected.gguf"
    _write_templates(path, _DEEPSEEK)
    existing = tmp_path / "deployment.json"
    previous = set_config.build_deployment_config(_plan(path))
    assert previous["services"]["main"]["thinking_kwarg"] == "thinking"
    existing.write_text(json.dumps(previous), encoding="utf-8")
    for source, expected in ((_ENABLE, "enable_thinking"), (None, None)):
        _write_templates(path, source)
        plan = _plan(path)
        updated = set_config.build_deployment_config(plan)
        set_config.merge_existing_deployment(updated, existing, plan.notes)
        assert updated["services"]["main"]["thinking_kwarg"] == expected
        existing.write_text(json.dumps(updated), encoding="utf-8")
        loaded = deployment.load_effective_profile({"HOME": str(tmp_path)}, deployment_config=existing)
        assert loaded.service("main").thinking_kwarg == expected


def test_thinking_capability_survives_split_export_and_import(tmp_path, monkeypatch):
    artifact = tmp_path / "synthetic.gguf"
    _write_templates(artifact, _DEEPSEEK)
    generated = set_config.build_deployment_config(_plan(artifact))
    host_path = tmp_path / "host.json"
    host_path.write_text(json.dumps(generated), encoding="utf-8")
    profile = deployment.load_effective_profile({"HOME": str(tmp_path)}, deployment_config=host_path)
    host = replace(profile, mode="model-host", services={
        role: replace(service, identity_alias=f"ct-{role}-v1", deployment_mode="model-host")
        for role, service in profile.services.items()
    })
    manifest = deployment.export_client_profile(host, "http://10.20.30.40")
    assert manifest["services"]["main"]["thinking_kwarg"] == "thinking"
    assert all("thinking_kwarg" not in value for role, value in manifest["services"].items() if role != "main")
    manifest_path = tmp_path / "client-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    home = tmp_path / "client-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("client import must not inspect host artifacts or run local inference tooling")

    for name in ("detect_gpus", "scan_models", "_check_tmux", "check_llama_binary",
                 "_detect_python", "inspect_main_thinking_kwarg"):
        monkeypatch.setattr(set_config, name, forbidden)
    args = set_config._parser().parse_args([
        "--mode", "client", "--yes", "--endpoint-manifest", str(manifest_path),
    ])
    assert set_config.run(args) == 0
    imported = deployment.load_effective_profile({"HOME": str(home)})
    assert imported.service("main").thinking_kwarg == "thinking"
    assert deployment.profile_as_dict(imported)["services"]["main"]["thinking_kwarg"] == "thinking"
    assert not (home / ".config" / "codetrail" / "models.json").exists()


def test_legacy_and_changed_model_profiles_do_not_inherit_capability(tmp_path):
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps({"schema_version": 1, "services": {"main": {"model": "old-model"}}}))
    assert deployment.load_effective_profile(deployment_config=path).service("main").thinking_kwarg is None
    path.write_text(json.dumps({"schema_version": 1, "services": {
        "main": {"model": "old-model", "thinking_kwarg": "thinking"},
    }}))
    current = deployment.load_effective_profile(deployment_config=path)
    assert current.service("main").thinking_kwarg == "thinking"
    assert deployment.profile_as_dict(current)["services"]["main"]["thinking_kwarg"] == "thinking"
    overridden = deployment.load_effective_profile(
        deployment_config=path, overrides=deployment.LauncherOverrides(main_model="different-model"),
    )
    assert overridden.service("main").thinking_kwarg is None
    # A legacy client manifest likewise cannot inherit a local/base capability.
    services = {role: {"model": f"ct-{role}-v1", "identity_alias": f"ct-{role}-v1",
                       "base_url": f"http://10.20.30.40:{8080 + i}"}
                for i, role in enumerate(deployment.ROLES)}
    path.write_text(json.dumps({"schema_version": 1, "mode": "client", "services": services}))
    assert deployment.load_effective_profile(deployment_config=path).service("main").thinking_kwarg is None


@pytest.mark.parametrize("value", [True, False, 1, "true", "reasoning", [], {}])
def test_thinking_capability_schema_rejects_noncanonical_values(tmp_path, value):
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps({"schema_version": 1, "services": {"main": {"thinking_kwarg": value}}}))
    with pytest.raises(deployment.ProfileError, match="thinking_kwarg"):
        deployment.load_effective_profile(deployment_config=path)
    path.write_text(json.dumps({"schema_version": 1, "services": {"vl": {"thinking_kwarg": "thinking"}}}))
    with pytest.raises(deployment.ProfileError, match="only allowed for main"):
        deployment.load_effective_profile(deployment_config=path)
