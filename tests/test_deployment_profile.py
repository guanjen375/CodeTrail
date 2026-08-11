from __future__ import annotations

import json
from pathlib import Path

import pytest

from deployment_profile import (
    ProfileError,
    build_server_command,
    load_effective_profile,
    resolve_model_reference,
)
from model_resolution import resolve_main_model_from_env

PROFILE_ENV_KEYS = {
    "AICODE_PROFILE",
    "AICODE_DEPLOYMENT_CONFIG",
    "AICODE_MODEL",
    "AICODE_MODEL_REGISTRY",
    "AICODE_MODEL_REGISTRY_FILE",
    "AICODE_LLAMA_BASE_URL",
    "AICODE_LLAMA_EMBED_BASE_URL",
    "AICODE_LLAMA_RERANK_BASE_URL",
    "AICODE_LLAMA_VL_BASE_URL",
    "AICODE_EMBED_MODEL",
    "AICODE_RERANK_MODEL",
    "AICODE_VL_MODEL",
    "AICODE_VL_MMPROJ",
    "AICODE_N_CTX",
    "EMBED_MODEL",
    "RERANK_MODEL",
    "VL_GGUF",
    "VL_MMPROJ",
    "MAIN_GPU",
    "AUX_GPU",
    "EMBED_GPU",
    "RERANK_GPU",
    "VL_GPU",
    "CUDA_VISIBLE_DEVICES",
    "MAIN_CTX",
    "MAIN_BATCH",
    "MAIN_UBATCH",
}


def _env(tmp_path: Path, **values: str) -> dict[str, str]:
    import os

    env = {key: value for key, value in os.environ.items() if key not in PROFILE_ENV_KEYS}
    env.update({"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), **values})
    return env


def _write_local(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / ".config" / "codetrail" / "deployment.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_profile(tmp_path: Path, name: str, services: dict) -> Path:
    """寫一個繼承內建 safe-defaults 的絕對路徑 profile fixture。"""
    path = tmp_path / f"{name}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": name,
                "extends": "defaults",
                "description": "test profile fixture",
                "verification": "unverified",
                "hardware": "test",
                "services": services,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_absolute_path_profile_inherits_builtin_safe_defaults(tmp_path):
    profile_path = _write_profile(tmp_path, "empty-target", {})
    profile = load_effective_profile(_env(tmp_path, AICODE_PROFILE=str(profile_path)))

    assert profile.selected_profile == "empty-target"
    assert profile.verification == "unverified"
    assert profile.service("main").model is None
    assert profile.service("embedding").parameters["pooling"] == "cls"
    assert profile.service("reranker").parameters["pooling"] == "rank"
    assert profile.service("vl").mmproj == "qwen3.5-9b-mmproj-f16"


def test_named_profile_references_are_rejected(tmp_path):
    with pytest.raises(ProfileError, match="absolute JSON profile path"):
        load_effective_profile(_env(tmp_path, AICODE_PROFILE="some-named-profile"))


def test_main_model_resolution_uses_selected_profile(tmp_path):
    profile_path = _write_profile(
        tmp_path, "pinned-main", {"main": {"model": "profile-main-model"}}
    )
    resolved = resolve_main_model_from_env(
        _env(tmp_path, AICODE_PROFILE=str(profile_path))
    )

    assert resolved.ok
    assert resolved.model == "profile-main-model"
    assert resolved.source.startswith("deployment profile pinned-main")


def test_precedence_cli_env_over_local_over_profile_over_defaults(tmp_path):
    profile_path = _write_profile(tmp_path, "local-selected", {})
    _write_local(
        tmp_path,
        {
            "schema_version": 1,
            "profile": str(profile_path),
            "services": {"main": {"model": "local-main", "ctx": 32768}},
        },
    )
    env = _env(tmp_path, AICODE_MODEL="env-main", MAIN_CTX="49152")

    effective = load_effective_profile(
        env,
        cli_env={"AICODE_MODEL": "cli-main", "MAIN_CTX": "98304"},
    )

    assert effective.selected_profile == "local-selected"
    assert effective.service("main").model == "cli-main"
    assert effective.service("main").ctx == 98304
    assert effective.service("embedding").ctx == 8192  # inherited safe default


def test_canonical_n_ctx_override_wins_over_legacy_main_ctx(tmp_path):
    effective = load_effective_profile(
        _env(tmp_path, AICODE_N_CTX="57344", MAIN_CTX="32768")
    )

    assert effective.service("main").ctx == 57344


def test_explicit_local_override_must_exist(tmp_path):
    missing = tmp_path / "missing-deployment.json"

    with pytest.raises(ProfileError, match="AICODE_DEPLOYMENT_CONFIG.*existing file"):
        load_effective_profile(_env(tmp_path, AICODE_DEPLOYMENT_CONFIG=str(missing)))


def test_absent_default_local_override_still_uses_defaults(tmp_path):
    effective = load_effective_profile(_env(tmp_path))

    assert effective.selected_profile == "defaults"
    assert effective.local_override is None
    assert effective.service("main").ctx == 65536


def test_profile_selector_precedence_cli_then_env_then_local(tmp_path):
    local_choice = _write_profile(tmp_path, "local-choice", {})
    env_choice = _write_profile(tmp_path, "env-choice", {})
    cli_choice = _write_profile(tmp_path, "cli-choice", {})
    _write_local(tmp_path, {"schema_version": 1, "profile": str(local_choice)})
    env = _env(tmp_path, AICODE_PROFILE=str(env_choice))

    assert load_effective_profile(env).selected_profile == "env-choice"
    assert (
        load_effective_profile(env, profile=str(cli_choice)).selected_profile
        == "cli-choice"
    )


@pytest.mark.parametrize(
    "service_patch,needle",
    [
        ({"extra_args": "--host 0.0.0.0; touch /tmp/pwned"}, "unsupported"),
        ({"model": "x; touch /tmp/pwned"}, "safe registry key"),
        ({"model": "~codetrail-user-that-does-not-exist/model.gguf"}, "unresolvable"),
        ({"base_url": "http://user:pass@localhost:8080"}, "credentials"),
        ({"gpu_role": "aux"}, "must remain"),
        ({"parameters": {"shell": "$(id)"}}, "not allowed"),
    ],
)
def test_malicious_or_raw_shell_profile_values_are_rejected(tmp_path, service_patch, needle):
    profile_path = tmp_path / "bad.json"
    profile_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "bad",
                "extends": "defaults",
                "description": "bad test",
                "verification": "unverified",
                "hardware": "test",
                "services": {"main": service_patch},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProfileError, match=needle):
        load_effective_profile(_env(tmp_path), profile=str(profile_path))


@pytest.mark.parametrize(
    ("raw", "needle"),
    [
        ('{"schema_version": 1, "schema_version": 1}', "duplicate JSON key"),
        ('{"schema_version": 1, "services": {"main": {"ctx": NaN}}}', "non-finite"),
    ],
)
def test_noncanonical_json_is_rejected(tmp_path, raw, needle):
    profile_path = tmp_path / "noncanonical.json"
    profile_path.write_text(raw, encoding="utf-8")

    with pytest.raises(ProfileError, match=needle):
        load_effective_profile(_env(tmp_path), profile=str(profile_path))


def test_model_and_mmproj_resolve_from_registry_or_absolute_path(tmp_path):
    main = tmp_path / "main.gguf"
    vl = tmp_path / "vl.gguf"
    mmproj = tmp_path / "mmproj.gguf"
    for path in (main, vl, mmproj):
        path.write_bytes(b"fixture")
    registry = {"main-key": str(main), "vl-key": str(vl), "mm-key": str(mmproj)}
    env = _env(
        tmp_path,
        AICODE_MODEL="main-key",
        AICODE_VL_MODEL="vl-key",
        AICODE_VL_MMPROJ="mm-key",
        AICODE_MODEL_REGISTRY=json.dumps(registry),
    )
    profile = load_effective_profile(env)

    assert resolve_model_reference(profile.service("main").model, env, must_exist=True) == str(main)
    assert resolve_model_reference(profile.service("vl").model, env, must_exist=True) == str(vl)
    assert resolve_model_reference(profile.service("vl").mmproj, env, must_exist=True) == str(mmproj)
    assert resolve_model_reference(str(main), env, must_exist=True) == str(main)


def test_main_and_aux_gpu_split_and_three_aux_share_one_gpu(tmp_path):
    env = _env(tmp_path, MAIN_GPU="GPU-H200", AUX_GPU="GPU-RTX2000ADA")
    profile = load_effective_profile(env)

    assert profile.service("main").gpu == "GPU-H200"
    assert {profile.service(role).gpu for role in ("embedding", "reranker", "vl")} == {
        "GPU-RTX2000ADA"
    }


def test_per_role_gpu_override_wins_over_aux_gpu(tmp_path):
    env = _env(
        tmp_path,
        AUX_GPU="GPU-AUX",
        EMBED_GPU="GPU-EMBED",
        RERANK_GPU="GPU-RERANK",
        VL_GPU="GPU-VL",
    )
    profile = load_effective_profile(env)

    assert profile.service("embedding").gpu == "GPU-EMBED"
    assert profile.service("reranker").gpu == "GPU-RERANK"
    assert profile.service("vl").gpu == "GPU-VL"


def test_command_builder_uses_only_structured_allowlisted_arguments(tmp_path):
    model = tmp_path / "main model.gguf"
    model.write_bytes(b"fixture")
    env = _env(tmp_path, AICODE_MODEL=str(model), MAIN_GPU="GPU-safe")
    service = load_effective_profile(env).service("main")

    command = build_server_command(service, "/opt/llama-server", env, must_exist=True)

    assert command[:3] == ["env", "CUDA_VISIBLE_DEVICES=GPU-safe", "/opt/llama-server"]
    assert command[command.index("-m") + 1] == str(model)
    assert "extra_args" not in command


def test_main_launcher_resolution_fails_loud_without_main_model(tmp_path):
    env = _env(tmp_path)
    service = load_effective_profile(env).service("main")

    with pytest.raises(ProfileError, match="main model is unset"):
        build_server_command(service, "/opt/llama-server", env)


def test_main_auto_fit_parameters_build_expected_command(tmp_path):
    model = tmp_path / "main.gguf"
    model.write_bytes(b"fixture")
    _write_local(
        tmp_path,
        {
            "schema_version": 1,
            "services": {
                "main": {
                    "parameters": {
                        "gpu_layers": "auto",
                        "fit": "on",
                        "fit_target": 5120,
                        "parallel": 1,
                        "jinja": True,
                    }
                }
            },
        },
    )
    env = _env(tmp_path, AICODE_MODEL=str(model))
    service = load_effective_profile(env).service("main")

    command = build_server_command(service, "/opt/llama-server", env, must_exist=True)

    assert command[command.index("-ngl") + 1] == "auto"
    assert command[command.index("--fit") + 1] == "on"
    assert command[command.index("--fit-target") + 1] == "5120"
    assert command[command.index("-np") + 1] == "1"


def test_aux_parallel_and_vl_fit_parameters_build_expected_commands(tmp_path):
    model = tmp_path / "model.gguf"
    mmproj = tmp_path / "mmproj.gguf"
    model.write_bytes(b"fixture")
    mmproj.write_bytes(b"fixture")
    _write_local(
        tmp_path,
        {
            "schema_version": 1,
            "services": {
                "embedding": {"model": str(model), "parameters": {"parallel": 1}},
                "reranker": {"model": str(model), "parameters": {"parallel": 1}},
                "vl": {
                    "model": str(model),
                    "mmproj": str(mmproj),
                    "parameters": {
                        "gpu_layers": "auto",
                        "parallel": 1,
                        "fit": "on",
                        "fit_target": 3072,
                    },
                },
            },
        },
    )
    env = _env(tmp_path)
    profile = load_effective_profile(env)

    for role in ("embedding", "reranker"):
        command = build_server_command(
            profile.service(role), "/opt/llama-server", env, must_exist=True
        )
        assert command[command.index("-np") + 1] == "1"
    vl_command = build_server_command(
        profile.service("vl"), "/opt/llama-server", env, must_exist=True
    )
    assert vl_command[vl_command.index("-ngl") + 1] == "auto"
    assert vl_command[vl_command.index("-np") + 1] == "1"
    assert vl_command[vl_command.index("--fit") + 1] == "on"
    assert vl_command[vl_command.index("--fit-target") + 1] == "3072"


@pytest.mark.parametrize(
    ("role", "parameters", "needle"),
    [
        ("main", {"fit": "maybe"}, "fit must be on or off"),
        ("main", {"fit_target": 0}, "fit_target"),
        ("main", {"parallel": 0}, "parallel"),
        ("main", {"gpu_layers": "autox"}, "gpu_layers"),
        ("embedding", {"fit": "on"}, "not allowed"),
    ],
)
def test_auto_fit_parameter_validation_rejects_bad_values(tmp_path, role, parameters, needle):
    _write_local(
        tmp_path,
        {"schema_version": 1, "services": {role: {"parameters": parameters}}},
    )
    env = _env(tmp_path, AICODE_MODEL="some-model")

    with pytest.raises(ProfileError, match=needle):
        load_effective_profile(env)


def test_bind_defaults_to_loopback_only(tmp_path):
    model = tmp_path / "main.gguf"
    model.write_bytes(b"fixture")
    env = _env(tmp_path, AICODE_MODEL=str(model))
    profile = load_effective_profile(env)

    for role in ("main", "embedding", "reranker", "vl"):
        assert profile.service(role).bind == "local"
    command = build_server_command(profile.service("main"), "/opt/llama-server", env, must_exist=True)
    assert command[command.index("--host") + 1] == "127.0.0.1"


def test_bind_all_interfaces_via_override_and_env(tmp_path):
    model = tmp_path / "main.gguf"
    model.write_bytes(b"fixture")
    _write_local(
        tmp_path,
        {"schema_version": 1, "services": {"main": {"bind": "all-interfaces"}}},
    )
    env = _env(tmp_path, AICODE_MODEL=str(model))
    profile = load_effective_profile(env)
    command = build_server_command(profile.service("main"), "/opt/llama-server", env, must_exist=True)
    assert command[command.index("--host") + 1] == "0.0.0.0"
    # local override 只設了 main;其他 role 仍是安全預設
    assert profile.service("embedding").bind == "local"

    env_all = _env(tmp_path, AICODE_MODEL=str(model), AICODE_BIND="all-interfaces")
    profile_all = load_effective_profile(env_all)
    for role in ("main", "embedding", "reranker", "vl"):
        assert profile_all.service(role).bind == "all-interfaces"


def test_bind_rejects_unknown_value_and_preserves_remote_host(tmp_path):
    _write_local(
        tmp_path,
        {"schema_version": 1, "services": {"main": {"bind": "everywhere"}}},
    )
    with pytest.raises(ProfileError, match="bind must be local or all-interfaces"):
        load_effective_profile(_env(tmp_path, AICODE_MODEL="some-model"))

    # 清掉壞 override,驗證非 loopback base_url(多機部署)不受 bind 預設影響
    (tmp_path / ".config" / "codetrail" / "deployment.json").write_text(
        json.dumps({"schema_version": 1, "services": {}}), encoding="utf-8"
    )
    model = tmp_path / "main.gguf"
    model.write_bytes(b"fixture")
    env = _env(
        tmp_path,
        AICODE_MODEL=str(model),
        AICODE_LLAMA_BASE_URL="http://gpu-host:8080",
    )
    service = load_effective_profile(env).service("main")
    command = build_server_command(service, "/opt/llama-server", env, must_exist=True)
    assert command[command.index("--host") + 1] == "gpu-host"
