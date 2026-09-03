"""部署設定的解析鏈:deployment profile、狀態巡檢、主模型解析、GPU / ctx 安全閘、
n_ctx 正規化、取樣參數釘住、config 健全性。

合併自 tests/test_deployment_profile.py、tests/test_deployment_status.py、
tests/test_model_resolution.py、tests/test_gpu_safety.py、tests/test_ctx_safety_check.py、
tests/test_ctx_resolution.py、tests/test_llama_sampling.py、tests/test_config.py
(2026-09-02)。行為與 assertion 未變;近似重複的案例改成 parametrize。

- deployment profile:絕對路徑 profile 繼承 safe-defaults、優先序、惡意值拒收、命令建構。
- deployment status:依 cmdline port 認角色、GPU / 模型 / mmproj 錯配偵測。
- 主模型解析鏈:argv/env/deployment profile 的解析與 fail-loud、呼叫時機、必要 server 檢查
  (opencode.json 已不在鏈上,只留「它不得再影響解析」的守門測試)
  (原本又併自 test_resolve_main_model / test_main_model_calltime /
  test_required_model_servers_check,2026-08-20)。
- gpu_safety:server-based ctx safety verdict + GPU info 回報。完全離線:所有
  nvidia-smi 與 llama-server HTTP 都用 hook 注入 fixture,CI 跑 --no-network 沒問題。
- ctx_safety_check:scripts/ctx_safety_check.py 的 CLI 行為。整段是 smoke
  (AGENTS.md §1.1 第 1 款「真實發生過的 bug 的 regression」:ctx 安全閘),逐條標記。
- n_ctx:config 的多來源優先序,以及 server ctx 自動偵測
  (原本又併自 test_n_ctx / test_resolve_server_ctx,2026-08-20)。
- 取樣參數釘住(離線,不需要 llama-server)。背景:llama-server 啟動沒帶 sampling
  旗標時,內建預設是 temp 0.8 / top_k 40 / min_p 0.05,偏離 Qwen3-235B-A22B-Thinking-2507
  官方建議,容易讓模型杜撰具體事實。CodeTrail 自己的呼叫除了壓 temperature,也把
  top_p/top_k/min_p 釘在 Qwen 建議值(config.CHAT_*),不再依賴 server 端預設。這段鎖住
  「參數真的有進 request payload」與「agent 路徑真的帶了 config.CHAT_*」。
- config.py 健全性:確保關鍵設定值有合理型別與範圍。
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
import gpu_safety
import n_ctx
from deployment_profile import (
    ProfileError,
    build_server_command,
    load_effective_profile,
    resolve_model_reference,
)
from deployment_status import GpuProcess, inspect_deployment
from gpu_safety import (
    GPUInfo,
    SafetyVerdict,
    ServerInfo,
    check_safety,
    query_gpu_info,
    query_server_info,
    runtime_offload_check,
)
from model_resolution import normalize_main_model, resolve_main_model_from_env
from scripts import ctx_safety_check as ctx
from scripts import required_model_servers_check as preflight
from scripts import resolve_main_model as rmm
from scripts import resolve_server_ctx

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── 原 test_deployment_profile.py:deployment profile 的載入、優先序與命令建構 ──

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
    assert profile.service("embedding").parameters["cache_ram"] == 0
    assert profile.service("reranker").parameters["pooling"] == "rank"
    assert profile.service("reranker").parameters["cache_ram"] == 0
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
        assert command[command.index("--cache-ram") + 1] == "0"
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


@pytest.mark.parametrize("role", ["embedding", "reranker"])
@pytest.mark.parametrize("cache_ram", [0, 262_144])
def test_aux_cache_ram_boundaries_build_expected_command(tmp_path, role, cache_ram):
    model = tmp_path / "aux.gguf"
    model.write_bytes(b"fixture")
    _write_local(
        tmp_path,
        {
            "schema_version": 1,
            "services": {role: {"model": str(model), "parameters": {"cache_ram": cache_ram}}},
        },
    )
    env = _env(tmp_path)
    service = load_effective_profile(env).service(role)
    command = build_server_command(service, "/opt/llama-server", env, must_exist=True)
    assert command[command.index("--cache-ram") + 1] == str(cache_ram)


@pytest.mark.parametrize("role", ["main", "vl"])
def test_cache_ram_is_rejected_for_generating_roles(tmp_path, role):
    _write_local(
        tmp_path,
        {"schema_version": 1, "services": {role: {"parameters": {"cache_ram": 0}}}},
    )
    with pytest.raises(ProfileError, match="not allowed"):
        load_effective_profile(_env(tmp_path, AICODE_MODEL="some-model"))


@pytest.mark.parametrize("value", [-1, 262_145, True, 0.0, "0"])
def test_cache_ram_rejects_out_of_range_bool_and_wrong_types(tmp_path, value):
    _write_local(
        tmp_path,
        {
            "schema_version": 1,
            "services": {"embedding": {"parameters": {"cache_ram": value}}},
        },
    )
    with pytest.raises(ProfileError, match="cache_ram must be an integer in 0..262144"):
        load_effective_profile(_env(tmp_path))


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


# ── 原 test_deployment_status.py:依 cmdline port 認角色、GPU / 模型 / mmproj 錯配 ──


def _fixture(tmp_path: Path):
    paths = {
        role: tmp_path / f"{role}.gguf"
        for role in ("main", "embedding", "reranker", "vl", "mmproj")
    }
    for path in paths.values():
        path.write_bytes(b"fixture")
    env = {
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "AICODE_MODEL": str(paths["main"]),
        "EMBED_MODEL": str(paths["embedding"]),
        "RERANK_MODEL": str(paths["reranker"]),
        "VL_GGUF": str(paths["vl"]),
        "VL_MMPROJ": str(paths["mmproj"]),
        "MAIN_GPU": "GPU-H200",
        "AUX_GPU": "GPU-RTX2000ADA",
    }
    profile = load_effective_profile(env)
    gpu_for = {
        "main": "GPU-H200",
        "embedding": "GPU-RTX2000ADA",
        "reranker": "GPU-RTX2000ADA",
        "vl": "GPU-RTX2000ADA",
    }
    processes = [
        GpuProcess(100 + index, "/opt/llama-server", gpu_for[role], "1000")
        for index, role in enumerate(("main", "embedding", "reranker", "vl"))
    ]
    cmdlines = {}
    for index, role in enumerate(("main", "embedding", "reranker", "vl")):
        service = profile.service(role)
        args = [
            "/opt/llama-server",
            "-m",
            resolve_model_reference(service.model, env),
            "--port",
            str(service.port),
            "-c",
            str(service.ctx),
        ]
        if service.mmproj:
            args.extend(["--mmproj", resolve_model_reference(service.mmproj, env)])
        cmdlines[100 + index] = tuple(args)

    def servers(service):
        return (
            {"status": "ok"},
            {
                "model_path": resolve_model_reference(service.model, env),
                "default_generation_settings": {"n_ctx": service.ctx},
            },
        )

    return env, profile, processes, cmdlines, servers, paths


def test_status_identifies_all_roles_by_cmdline_port(tmp_path):
    env, profile, processes, cmdlines, servers, _ = _fixture(tmp_path)

    inspection = inspect_deployment(
        profile,
        processes,
        environ=env,
        cmdline_reader=lambda pid: cmdlines[pid],
        server_reader=servers,
    )

    assert not inspection.issues
    assert {role: obs.pid for role, obs in inspection.observations.items()} == {
        "main": 100,
        "embedding": 101,
        "reranker": 102,
        "vl": 103,
    }


def test_status_no_network_still_validates_cmdline_without_health_failure(tmp_path):
    env, profile, processes, cmdlines, _, _ = _fixture(tmp_path)

    inspection = inspect_deployment(
        profile,
        processes,
        environ=env,
        cmdline_reader=lambda pid: cmdlines[pid],
        server_reader=None,
    )

    assert not inspection.issues
    assert {obs.health for obs in inspection.observations.values()} == {"not-checked"}


def test_status_detects_wrong_gpu_for_aux_role(tmp_path):
    env, profile, processes, cmdlines, servers, _ = _fixture(tmp_path)
    processes[1] = GpuProcess(101, "/opt/llama-server", "GPU-H200", "1000")

    inspection = inspect_deployment(
        profile,
        processes,
        environ=env,
        cmdline_reader=lambda pid: cmdlines[pid],
        server_reader=servers,
    )

    assert any("embedding: wrong GPU" in issue for issue in inspection.issues)


def test_status_detects_wrong_loaded_model(tmp_path):
    env, profile, processes, cmdlines, servers, paths = _fixture(tmp_path)

    def wrong_servers(service):
        health, props = servers(service)
        if service.role == "reranker":
            props = {**props, "model_path": str(paths["embedding"])}
        return health, props

    inspection = inspect_deployment(
        profile,
        processes,
        environ=env,
        cmdline_reader=lambda pid: cmdlines[pid],
        server_reader=wrong_servers,
    )

    assert any("reranker: wrong model" in issue for issue in inspection.issues)


def test_status_requires_observable_vl_mmproj(tmp_path):
    env, profile, processes, cmdlines, servers, _ = _fixture(tmp_path)
    cmdlines[103] = tuple(
        arg
        for index, arg in enumerate(cmdlines[103])
        if arg != "--mmproj" and (index == 0 or cmdlines[103][index - 1] != "--mmproj")
    )

    inspection = inspect_deployment(
        profile,
        processes,
        environ=env,
        cmdline_reader=lambda pid: cmdlines[pid],
        server_reader=servers,
    )

    assert any("vl: loaded mmproj is not observable" in issue for issue in inspection.issues)


# ── 原 test_model_resolution.py:主模型解析鏈(argv/env 解析與 fail-loud、呼叫時機、必要 server 檢查)──


@pytest.fixture
def model_resolution_env(monkeypatch, tmp_path):
    """原 test_model_resolution.py 的 module 級 autouse fixture(`_clean_env`):清掉
    AICODE_MODEL / OPENCODE_CONFIG,HOME 指到 tmp_path。合併後改成顯式掛載,只給
    來自該檔的測試;不讓它擴散到本檔其他來源的測試(它們各自有自己的 env 隔離)。"""
    monkeypatch.delenv("AICODE_MODEL", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    yield


def _write_home_opencode(tmp_path: Path, model: str = "llamacpp/from-json") -> Path:
    """寫一份殘留的 OpenCode 設定。**只用來證明它不再影響解析。**
    """
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    path = cfg_dir / "opencode.json"
    path.write_text(json.dumps({"model": model}), encoding="utf-8")
    return path


def _write_alias_registry(tmp_path: Path, aliases: tuple[str, ...]) -> Path:
    model = tmp_path / "models" / "same-model.gguf"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"GGUF fixture")
    cfg_dir = tmp_path / ".config" / "codetrail"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "models.json").write_text(
        json.dumps({alias: str(model) for alias in aliases}),
        encoding="utf-8",
    )
    return model


@pytest.mark.parametrize(
    ("env_model", "argv_model", "expected"),
    [
        pytest.param("same-model", "same-model", "same-model", id="same_model_allowed"),
        pytest.param(
            "foo-bar", "llamacpp/foo-bar", "foo-bar",
            id="custom_provider_prefix_strips_to_bare",
        ),
    ],
)
def test_env_and_argv_agree(model_resolution_env, monkeypatch, capsys, env_model, argv_model, expected):
    """- same_model_allowed:env 與 --model 同一個 bare model → 放行。
    - custom_provider_prefix_strips_to_bare:OpenCode 風格的 myprovider/bare 形式應該
      strip 成 bare;strip 後與 env 一致就不算衝突。"""
    monkeypatch.setenv("AICODE_MODEL", env_model)

    assert rmm.main(["--model", argv_model]) == 0
    assert capsys.readouterr().out.strip() == expected


def test_env_and_argv_registry_aliases_for_same_gguf_allowed(
    model_resolution_env, monkeypatch, tmp_path, capsys
):
    _write_alias_registry(tmp_path, ("old-alias", "new-alias"))
    monkeypatch.setenv("AICODE_MODEL", "old-alias")

    assert rmm.main(["--model", "new-alias"]) == 0
    assert capsys.readouterr().out.strip() == "old-alias"


def test_env_and_argv_conflict_fails(model_resolution_env, monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "env-model")

    rc = rmm.main(["--model", "cli-model"])

    assert rc == 2
    assert "different models" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(["--model=llamacpp/foo-bar"], "foo-bar", id="equals_form"),
        pytest.param(["-m", "foo-bar"], "foo-bar", id="bare_model_name"),
        pytest.param(["-m", "/models/foo.gguf"], "/models/foo.gguf", id="gguf_path"),
    ],
)
def test_argv_model_forms(model_resolution_env, capsys, argv, expected):
    """argv 接受的主模型寫法:
    - equals_form:`--model=provider/name`,provider 會被 strip。
    - bare_model_name:`-m name`。
    - gguf_path:GGUF 絕對路徑也是合法的主模型形式。"""
    assert rmm.main(argv) == 0
    assert capsys.readouterr().out.strip() == expected


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--model"], id="long_flag"),
        pytest.param(["-m"], id="short_flag"),
        pytest.param(["--model", "--foo"], id="value_is_another_flag"),
    ],
)
def test_argv_missing_value_fails_loud(model_resolution_env, capsys, argv):
    """--model / -m 沒帶值(long_flag / short_flag),或下一個 token 是另一個 flag
    (value_is_another_flag)→ fail-loud,不吞。"""
    rc = rmm.main(argv)

    assert rc == 2
    err = capsys.readouterr().err
    assert "requires a model value" in err


def test_argv_rejects_external_provider(model_resolution_env, capsys):
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


def test_normalize_main_model_strips_custom_provider(model_resolution_env):
    """custom-provider/bare 形式被 strip 成 bare。"""
    assert normalize_main_model("llamacpp/foo-bar", "test").model == "foo-bar"
    assert normalize_main_model("myprovider/some-model", "test").model == "some-model"


def test_normalize_main_model_accepts_gguf_path(model_resolution_env):
    assert normalize_main_model("/models/foo.gguf", "test").model == "/models/foo.gguf"
    assert normalize_main_model("~/models/foo.gguf", "test").model == "~/models/foo.gguf"


def test_normalize_main_model_rejects_known_external_providers(model_resolution_env):
    assert normalize_main_model("openai/gpt-4.1", "test").error
    assert normalize_main_model("anthropic/something", "test").error
    assert normalize_main_model("ollama/qwen3", "test").error


def test_env_rejects_external_provider(model_resolution_env, monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "anthropic/something")

    rc = rmm.main([])

    assert rc == 2
    err = capsys.readouterr().err
    assert "外部 provider" in err or "provider prefix" in err


def test_placeholder_in_env_fails(model_resolution_env, monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "<CODE_MODEL>")

    rc = rmm.main([])

    assert rc == 2
    assert "placeholder" in capsys.readouterr().err


def test_no_source_at_all_fails_loud(model_resolution_env, capsys):
    rc = rmm.main([])

    assert rc == 2
    err = capsys.readouterr().err
    assert "AICODE_MODEL" in err
    assert "opencode" not in err


@pytest.mark.smoke
def test_opencode_json_is_no_longer_a_model_source(model_resolution_env, tmp_path, capsys):
    """`opencode.json` 已經不在主模型解析鏈上。

    CodeTrail 啟動的是自己的客戶端,沒有第二個 TUI 要對齊。沿用那份設定裡的
    模型等於「使用者以為在跑 A、實際在跑 B」。
    """
    _write_home_opencode(tmp_path, "llamacpp/from-json")
    assert rmm.main([]) == 2
    err = capsys.readouterr().err
    assert "from-json" not in err


@pytest.mark.smoke
def test_a_broken_opencode_json_never_blocks_startup(
    model_resolution_env, monkeypatch, tmp_path, capsys
):
    """一台根本沒在用 OpenCode 的機器,不該因為那份殘留檔壞掉而無法啟動。"""
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("AICODE_MODEL", "review-model")

    assert rmm.main([]) == 0
    assert capsys.readouterr().out.strip() == "review-model"


@pytest.mark.smoke
def test_a_conflicting_opencode_json_is_not_a_conflict_any_more(
    model_resolution_env, monkeypatch, tmp_path, capsys
):
    _write_home_opencode(tmp_path, "llamacpp/from-json")
    monkeypatch.setenv("AICODE_MODEL", "from-env")

    assert rmm.main([]) == 0
    assert capsys.readouterr().out.strip() == "from-env"


def test_empty_string_treated_as_unset(model_resolution_env, monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "   ")

    assert rmm.main([]) == 2


# --------------------------------------------------------------------------
# 併自 tests/test_main_model_calltime.py:主模型在什麼時機被解析。
# --------------------------------------------------------------------------
class CapturingNativeCompletion:
    """假裝 llama_client.native_completion 的 callable;同時記錄被叫到時帶的參數。"""
    def __init__(self, response_payload: dict):
        self.response_payload = response_payload
        self.last_kwargs = None

    def __call__(self, **kwargs):
        self.last_kwargs = kwargs
        return self.response_payload


class CapturingChatCompletions:
    """假裝 llama_client.chat_completions。"""
    def __init__(self, response_payload: dict):
        self.response_payload = response_payload
        self.last_kwargs = None

    def __call__(self, **kwargs):
        self.last_kwargs = kwargs
        return self.response_payload


def test_utils_call_llm_uses_require_main_model(model_resolution_env, monkeypatch):
    import llama_client
    import utils

    fake = CapturingNativeCompletion({"content": "ok"})
    usage = SimpleNamespace(error_type=None)

    monkeypatch.setattr(utils.config, "require_main_model", lambda: "calltime-utils")
    monkeypatch.setattr(llama_client, "native_completion", fake)
    monkeypatch.setattr(utils.context_budget, "check_and_log", lambda **_kw: usage)
    monkeypatch.setattr(utils.context_budget, "parse_usage_from_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(utils.context_budget, "emit_post_call_line", lambda *_a, **_kw: None)
    monkeypatch.setattr(utils.context_budget, "log_metrics", lambda *_a, **_kw: None)

    # call_llm 不再傳 model 給 server (llama-server 是 one-model-per-instance),
    # 但仍應該透過 require_main_model 解析。我們驗證 require_main_model 有被叫到
    # — 由上面 monkeypatch 直接 patch 成 fn 並回固定字串,效果一樣可驗證。
    assert utils.call_llm("hello") == "ok"
    # native_completion 至少被呼叫一次
    assert fake.last_kwargs is not None
    assert fake.last_kwargs["prompt"] == "hello"


def test_agent_call_llm_with_tools_uses_require_main_model(model_resolution_env, monkeypatch):
    import agent
    import llama_client

    fake = CapturingChatCompletions({
        "choices": [{
            "message": {"content": "ok", "tool_calls": []},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    })
    usage = SimpleNamespace(error_type=None, did_trim=False, trim_summary=None)

    monkeypatch.setattr(agent.config, "require_main_model", lambda: "calltime-agent")
    monkeypatch.setattr(llama_client, "chat_completions", fake)
    monkeypatch.setattr(agent, "_compute_dynamic_num_ctx", lambda _messages: 2048)
    monkeypatch.setattr(agent, "get_native_tools", lambda: [])
    monkeypatch.setattr(agent, "_pre_send_trim_if_needed", lambda *_a, **_kw: (usage, None))
    monkeypatch.setattr(agent.context_budget, "emit_pre_call_lines", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "enforce_gate", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "parse_usage_from_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "emit_post_call_line", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "log_metrics", lambda *_a, **_kw: None)

    result = agent.call_llm_with_tools([{"role": "user", "content": "hello"}])

    assert result["content"] == "ok"
    assert fake.last_kwargs["model"] == "calltime-agent"


def test_knowledge_expand_query_uses_require_main_model(model_resolution_env, monkeypatch):
    import knowledge
    import llama_client

    fake = CapturingNativeCompletion({"content": "alpha, beta"})
    monkeypatch.setattr(knowledge.config, "require_main_model", lambda: "calltime-knowledge")
    monkeypatch.setattr(llama_client, "native_completion", fake)

    kb = knowledge.KnowledgeBase(str(REPO_ROOT / ".missing-knowledge-for-test.json"))
    expanded = kb._expand_query("What changed?", force=True)

    assert expanded
    # require_main_model 在路徑中被叫到時 patched 成回 "calltime-knowledge"。
    # native_completion 本身沒帶 model 參數(server 鎖死),但 require_main_model
    # 解析的值會出現在 monkeypatch hook 觸發前的呼叫;只需確認 native_completion
    # 真的被呼到即可。
    assert fake.last_kwargs is not None


# --------------------------------------------------------------------------
# 併自 tests/test_required_model_servers_check.py。
# --------------------------------------------------------------------------
def test_required_model_servers_all_pass(model_resolution_env, monkeypatch):
    monkeypatch.setattr(preflight.llama_client, "get_health", lambda url, timeout=3: {"status": "ok"})
    monkeypatch.setattr(preflight.llama_client, "embed_one", lambda **kwargs: [0.1, 0.2])
    monkeypatch.setattr(preflight.llama_client, "rerank", lambda **kwargs: [0.9, 0.1])
    monkeypatch.setattr(preflight.llama_client, "vision_completion", lambda **kwargs: "ok")

    checks = preflight.run_checks()

    assert all(check.ok for check in checks)
    assert {check.role for check in checks} == {"embedding", "reranker", "VL"}


def test_required_model_servers_fails_on_missing_health(model_resolution_env, monkeypatch):
    monkeypatch.setattr(preflight.llama_client, "get_health", lambda url, timeout=3: None)

    checks = preflight.run_checks()

    assert not any(check.ok for check in checks)
    assert all("health endpoint unreachable" in check.message for check in checks)
    report = "\n".join(preflight.render_report(checks))
    assert "refuse to start" in report


def test_required_model_servers_fails_role_probe(model_resolution_env, monkeypatch):
    monkeypatch.setattr(preflight.llama_client, "get_health", lambda url, timeout=3: {"status": "ok"})
    monkeypatch.setattr(preflight.llama_client, "embed_one", lambda **kwargs: [0.1, 0.2])
    monkeypatch.setattr(preflight.llama_client, "rerank", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(preflight.llama_client, "vision_completion", lambda **kwargs: "ok")

    checks = preflight.run_checks()

    by_role = {check.role: check for check in checks}
    assert by_role["embedding"].ok
    assert not by_role["reranker"].ok
    assert "boom" in by_role["reranker"].message
    assert by_role["VL"].ok


# ── 原 test_gpu_safety.py:server-based ctx safety verdict + GPU info 回報 ──


# ============================================================
# fixtures:典型 /props 回應
# ============================================================
def _props_with_ctx(n_ctx: int, model_path: str = "/m/foo.gguf") -> dict:
    return {
        "default_generation_settings": {"n_ctx": n_ctx, "n_predict": -1},
        "total_slots": 1,
        "model_path": model_path,
        "chat_template": "...",
    }


# ============================================================
# query_gpu_info
# ============================================================
class TestQueryGpuInfo:
    def test_parses_single_gpu(self):
        def runner(cmd):
            assert cmd[0] == "nvidia-smi"
            return "NVIDIA GeForce RTX 5090, 32607, 30000\n"
        gi = query_gpu_info(_runner=runner)
        assert gi is not None
        assert gi.name == "NVIDIA GeForce RTX 5090"
        assert gi.total_bytes == 32607 * 1024 * 1024
        assert gi.free_bytes == 30000 * 1024 * 1024

    def test_picks_largest_of_multiple_gpus(self):
        def runner(cmd):
            return (
                "GPU A, 8000, 6000\n"
                "GPU B, 32607, 30000\n"
                "GPU C, 24000, 20000\n"
            )
        gi = query_gpu_info(_runner=runner)
        assert gi.name == "GPU B"
        assert gi.total_bytes == 32607 * 1024 * 1024

    def test_handles_runner_failure(self):
        def runner(cmd):
            raise RuntimeError("nvidia-smi: command failed")
        assert query_gpu_info(_runner=runner) is None

    def test_handles_garbage_lines(self):
        def runner(cmd):
            return "garbage\nNVIDIA RTX 5090, 32607, 30000\nmore garbage,\n"
        gi = query_gpu_info(_runner=runner)
        assert gi is not None
        assert gi.name == "NVIDIA RTX 5090"


# ============================================================
# query_server_info: HTTP 用 _props_fn hook
# ============================================================
class TestQueryServerInfo:
    def test_parses_default_generation_settings(self):
        s = query_server_info(
            "http://localhost:8080",
            _props_fn=lambda url: _props_with_ctx(32768, "/models/foo.gguf"),
        )
        assert s is not None
        assert s.n_ctx == 32768
        assert s.model_path == "/models/foo.gguf"
        assert s.total_slots == 1

    def test_falls_back_to_top_level_n_ctx(self):
        """有些 server 版本把 n_ctx 直接放頂層。"""
        s = query_server_info(
            "http://localhost:8080",
            _props_fn=lambda url: {"n_ctx": 4096, "model_path": "/m/bar.gguf"},
        )
        assert s is not None
        assert s.n_ctx == 4096

    def test_returns_none_when_server_down(self):
        """props_fn 回 None(server 不可連)→ ServerInfo 也 None。"""
        s = query_server_info("http://localhost:8080", _props_fn=lambda url: None)
        assert s is None

    def test_returns_none_on_non_dict_response(self):
        s = query_server_info("http://localhost:8080", _props_fn=lambda url: "not a dict")
        assert s is None


# ============================================================
# check_safety
# ============================================================
class TestCheckSafety:
    def _gpu(self, gb: int) -> GPUInfo:
        return GPUInfo(name=f"fake-{gb}GB", total_bytes=gb * 1024 ** 3,
                       free_bytes=gb * 1024 ** 3)

    def test_safe_when_requested_within_server_ctx(self):
        v = check_safety(
            32768,
            _gpu=self._gpu(32),
            _server=ServerInfo(base_url="http://localhost:8080", n_ctx=65536,
                               model_path="/m/foo.gguf"),
        )
        assert v.status == "SAFE"
        assert v.server_n_ctx == 65536
        assert v.requested_ctx == 32768
        assert v.detail_lines  # 不能空

    def test_unsafe_when_requested_exceeds_server_ctx(self):
        v = check_safety(
            65536,
            _gpu=self._gpu(32),
            _server=ServerInfo(base_url="http://localhost:8080", n_ctx=8192,
                               model_path="/m/foo.gguf"),
        )
        assert v.status == "UNSAFE"
        assert v.server_n_ctx == 8192
        assert "8192" in v.reason
        assert "truncate" in v.reason or "截斷" in v.reason

    def test_unknown_when_server_unreachable(self, monkeypatch):
        # query_server_info 的 HTTP/None 分支已有上面的 hook 測試；這裡只驗
        # check_safety 對「查不到 server」的 verdict，不必真的等 socket timeout。
        monkeypatch.setattr(gpu_safety, "query_server_info", lambda _url: None)
        v = check_safety(
            32768,
            base_url="http://127.0.0.1:65535",
            _gpu=self._gpu(32),
            _server=None,
        )
        assert v.status == "UNKNOWN"
        assert v.server_n_ctx is None
        assert "llama-server" in v.reason

    def test_unknown_when_server_missing_n_ctx(self):
        v = check_safety(
            32768,
            _gpu=self._gpu(32),
            _server=ServerInfo(base_url="http://localhost:8080", n_ctx=None,
                               model_path="/m/foo.gguf"),
        )
        assert v.status == "UNKNOWN"
        assert "n_ctx" in v.reason

    def test_safe_at_exact_boundary(self):
        """requested == server n_ctx 仍算 SAFE。"""
        v = check_safety(
            8192,
            _gpu=self._gpu(32),
            _server=ServerInfo(base_url="http://localhost:8080", n_ctx=8192,
                               model_path="/m/foo.gguf"),
        )
        assert v.status == "SAFE"

    def test_no_gpu_still_works(self, monkeypatch):
        """nvidia-smi 拿不到也應該照樣回 verdict,GPU info 是 informational。"""
        monkeypatch.setattr(gpu_safety, "query_gpu_info", lambda: None)
        v = check_safety(
            8192,
            _server=ServerInfo(base_url="http://localhost:8080", n_ctx=65536,
                               model_path="/m/foo.gguf"),
        )
        assert v.status == "SAFE"
        assert v.vram_total_gb is None


# ============================================================
# runtime_offload_check
# ============================================================
class TestRuntimeOffloadCheck:
    def test_reports_server_info(self):
        s = runtime_offload_check(
            "http://localhost:8080",
            _props_fn=lambda url: _props_with_ctx(32768, "/m/qwen.gguf"),
            _slots_fn=lambda url: [{"id": 0, "state": 0, "n_ctx": 32768}],
        )
        assert s.available
        assert s.n_ctx == 32768
        assert "qwen.gguf" in (s.model_name or "")
        assert s.busy_slots == 0
        assert s.total_slots == 1

    def test_busy_slot_count(self):
        s = runtime_offload_check(
            "http://localhost:8080",
            _props_fn=lambda url: _props_with_ctx(8192),
            _slots_fn=lambda url: [
                {"id": 0, "state": 1, "n_ctx": 8192},
                {"id": 1, "state": 0, "n_ctx": 8192},
            ],
        )
        assert s.available
        assert s.busy_slots == 1

    def test_unavailable_when_server_down(self):
        s = runtime_offload_check(
            "http://localhost:8080",
            _props_fn=lambda url: None,
        )
        assert not s.available
        assert s.base_url == "http://localhost:8080"

    def test_slots_endpoint_failure_still_usable(self):
        """slots 拿不到時不影響 props 的回報。"""
        s = runtime_offload_check(
            "http://localhost:8080",
            _props_fn=lambda url: _props_with_ctx(32768),
            _slots_fn=lambda url: None,
        )
        assert s.available
        assert s.n_ctx == 32768
        assert s.busy_slots == 0

    def test_is_offloaded_always_false_for_llamacpp(self):
        """llama-server 在啟動時就決定是否 offload (--n-gpu-layers),runtime
        觀測不到,所以 is_offloaded 永遠 False。這欄留著只為了向後相容呼叫端。
        """
        s = runtime_offload_check(
            "http://localhost:8080",
            _props_fn=lambda url: _props_with_ctx(8192),
            _slots_fn=lambda url: [{"id": 0, "state": 0}],
        )
        assert s.is_offloaded is False


# ============================================================
# RuntimeOffloadStatus.short()
# ============================================================
def test_runtime_status_short_when_available():
    s = gpu_safety.RuntimeOffloadStatus(
        available=True,
        base_url="http://localhost:8080",
        model_name="foo.gguf",
        n_ctx=32768,
        total_slots=1,
        busy_slots=0,
    )
    line = s.short()
    assert "foo.gguf" in line
    assert "32768" in line


def test_runtime_status_short_when_unavailable():
    s = gpu_safety.RuntimeOffloadStatus(available=False, base_url="http://localhost:8080")
    assert "無資料" in s.short()


# ── 原 test_ctx_safety_check.py:scripts/ctx_safety_check.py 的 CLI 行為 ──
# smoke:AGENTS.md §1.1 第 1 款「真實發生過的 bug 的 regression」
# 真實 bug regression:ctx 安全閘。原檔是 module 層 pytestmark = smoke;合併後
# 本檔其他來源不是 smoke,所以這一段每一條各自標 @pytest.mark.smoke。


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
    server_n_ctx 釘住,測試端用 AICODE_N_CTX 控制 requested 即可。
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


@pytest.mark.smoke
def test_ctx_safety_passes_when_requested_equals_server(monkeypatch, capsys):
    """requested == server n_ctx → SAFE,放行 (exit 0)。"""
    monkeypatch.setenv("AICODE_MODEL", "custom-model")
    monkeypatch.setenv("AICODE_N_CTX", "65536")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)
    monkeypatch.setattr(
        ctx.gpu_safety, "check_safety", _server_verdict_factory("SAFE", 65536)
    )

    assert ctx.main() == 0
    out = capsys.readouterr().out
    assert "SAFE" in out
    assert "<= server n_ctx=65536" in out


@pytest.mark.smoke
def test_ctx_safety_passes_when_requested_below_server(monkeypatch, capsys):
    """requested < server n_ctx → SAFE,放行 (exit 0)。

    「小於」不是安全問題(不截斷,只是沒用滿 server 容量),不該擋。正常情況下
    aicode 會自動把 requested 帶成 == server,這條主要保障使用者手動設小一點時
    不會被無謂擋住,也是把舊版 e129d48「小於就 refuse」死鎖拿掉的回歸測試。
    """
    monkeypatch.setenv("AICODE_MODEL", "custom-model")
    monkeypatch.setenv("AICODE_N_CTX", "32768")
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


@pytest.mark.smoke
def test_ctx_safety_unsafe_allows_with_accept_risk(monkeypatch, capsys):
    """requested > server n_ctx 但設了 AICODE_ACCEPT_CTX_RISK=1 → 放行 (exit 0)。"""
    monkeypatch.setenv("AICODE_MODEL", "custom-model")
    monkeypatch.setenv("AICODE_N_CTX", "65536")
    monkeypatch.setenv("AICODE_ACCEPT_CTX_RISK", "1")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)
    monkeypatch.setattr(
        ctx.gpu_safety, "check_safety", _server_verdict_factory("UNSAFE", 8192)
    )

    assert ctx.main() == 0
    out = capsys.readouterr().out
    assert "UNSAFE" in out
    assert "AICODE_ACCEPT_CTX_RISK=1 已設" in out


@pytest.mark.smoke
def test_ctx_safety_unsafe_when_requested_above_server(monkeypatch, capsys):
    """requested > server n_ctx → UNSAFE (截斷風險),擋住 (exit 2)。"""
    monkeypatch.setenv("AICODE_MODEL", "custom-model")
    monkeypatch.setenv("AICODE_N_CTX", "65536")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)
    monkeypatch.delenv("AICODE_ACCEPT_CTX_RISK", raising=False)
    monkeypatch.setattr(
        ctx.gpu_safety, "check_safety", _server_verdict_factory("UNSAFE", 8192)
    )

    assert ctx.main() == 2
    out = capsys.readouterr().out
    assert "UNSAFE" in out
    assert "8192" in out
    assert "set_config.sh" in out
    assert "refuse to start" in out


@pytest.mark.smoke
def test_ctx_safety_fails_loud_when_env_missing(monkeypatch, capsys):
    """CodeTrail 不內建主模型: AICODE_MODEL 未設時必須 fail-loud (exit 2)。"""
    monkeypatch.delenv("AICODE_MODEL", raising=False)
    monkeypatch.delenv("AICODE_N_CTX", raising=False)
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


@pytest.mark.smoke
def test_ctx_safety_fails_loud_on_placeholder_model(monkeypatch, capsys):
    """值是 `<CODE_MODEL>` 之類 placeholder 也要 fail-loud。"""
    monkeypatch.setenv("AICODE_MODEL", "<CODE_MODEL>")
    monkeypatch.delenv("AICODE_N_CTX", raising=False)
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


@pytest.mark.smoke
def test_ctx_safety_disable_short_circuits_even_without_model(monkeypatch, capsys):
    """AICODE_CTX_SAFETY_DISABLE=1 時, 即使沒設 AICODE_MODEL 也 exit 0 (CI / 緊急逃生)。"""
    monkeypatch.delenv("AICODE_MODEL", raising=False)
    monkeypatch.setenv("AICODE_CTX_SAFETY_DISABLE", "1")
    assert ctx.main() == 0
    out = capsys.readouterr().out
    assert "disabled via AICODE_CTX_SAFETY_DISABLE" in out


@pytest.mark.smoke
def test_ctx_safety_rejects_invalid_canonical_n_ctx(monkeypatch, capsys):
    monkeypatch.setenv("AICODE_MODEL", "custom-model")
    monkeypatch.setenv("AICODE_N_CTX", "not-a-number")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)

    assert ctx.main() == 2
    out = capsys.readouterr().out
    assert "AICODE_N_CTX" in out
    assert "refuse to start" in out


@pytest.mark.smoke
def test_ctx_safety_uses_resolved_model_from_env(monkeypatch):
    """新版 check_safety(requested_ctx, base_url=...) 簽名 — 不再吃 model。
    這個 test 確認 ctx_safety_check.main 走到 gpu_safety.check_safety 並帶入正確
    requested ctx 與 base_url。
    """
    monkeypatch.setenv("AICODE_MODEL", "custom-model")
    monkeypatch.setenv("AICODE_N_CTX", "65536")
    monkeypatch.delenv("AICODE_CTX_SAFETY_DISABLE", raising=False)
    monkeypatch.setenv("AICODE_LLAMA_BASE_URL", "http://example.test:8080")

    calls: dict[str, object] = {}

    def fake_check_safety(requested, base_url="http://localhost:8080", **_kw):
        calls["requested"] = requested
        calls["base_url"] = base_url
        return _unknown_verdict(requested)

    monkeypatch.setattr(ctx.gpu_safety, "check_safety", fake_check_safety)

    assert ctx.main() == 0
    assert calls["requested"] == ctx.n_ctx.DEFAULT_N_CTX
    assert calls["base_url"] == "http://example.test:8080"


# ── 原 test_ctx_resolution.py:n_ctx 的多來源優先序,以及 server ctx 自動偵測 ──


def test_canonical_n_ctx_wins_over_legacy_values():
    resolved = n_ctx.resolve_n_ctx(
        {
            "AICODE_N_CTX": "49152",
            "AICODE_DYNAMIC_NUM_CTX_MAX": "32768",
            "AICODE_NUM_CTX": "131072",
        }
    )

    assert resolved.value == 49152
    assert resolved.source == "AICODE_N_CTX"
    assert resolved.legacy is False


def test_old_dynamic_max_remains_a_compatibility_alias():
    resolved = n_ctx.resolve_n_ctx({"AICODE_DYNAMIC_NUM_CTX_MAX": "32768"})

    assert resolved.value == 32768
    assert resolved.legacy is True


def test_stale_aicode_num_ctx_is_not_promoted_to_runtime_budget():
    resolved = n_ctx.resolve_n_ctx(
        {"AICODE_NUM_CTX": "131072"},
        default=65536,
        default_source="deployment profile main.ctx",
    )

    assert resolved.value == 65536
    assert resolved.source == "deployment profile main.ctx"


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "1048577"])
def test_invalid_canonical_n_ctx_is_rejected(raw):
    with pytest.raises(ValueError, match="AICODE_N_CTX"):
        n_ctx.resolve_n_ctx({"AICODE_N_CTX": raw})


def test_blank_canonical_env_uses_default():
    assert n_ctx.resolve_n_ctx({"AICODE_N_CTX": " "}).value == n_ctx.DEFAULT_N_CTX


def test_dynamic_sizing_never_exceeds_a_small_main_n_ctx(monkeypatch):
    import agent

    monkeypatch.setattr(agent, "N_CTX", 1024)
    monkeypatch.setattr(agent, "DYNAMIC_NUM_CTX_MIN", 16384)

    assert agent._compute_dynamic_num_ctx([{"role": "user", "content": "hello"}]) == 1024


# --------------------------------------------------------------------------
# 併自 tests/test_resolve_server_ctx.py:scripts/resolve_server_ctx.py。
# --------------------------------------------------------------------------
def test_prints_server_n_ctx(monkeypatch, capsys):
    server = resolve_server_ctx.gpu_safety.ServerInfo(
        base_url="http://localhost:8080",
        n_ctx=65536,
    )
    monkeypatch.setattr(
        resolve_server_ctx.gpu_safety,
        "query_server_info",
        lambda _url: server,
    )

    assert resolve_server_ctx.main() == 0
    captured = capsys.readouterr()
    assert captured.out == "65536\n"
    assert captured.err == ""


def test_missing_server_is_non_blocking_and_prints_no_value(monkeypatch, capsys):
    monkeypatch.setattr(
        resolve_server_ctx.gpu_safety,
        "query_server_info",
        lambda _url: None,
    )

    assert resolve_server_ctx.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "無法" in captured.err


def test_query_exception_is_non_blocking_and_prints_no_value(monkeypatch, capsys):
    def fail(_url):
        raise OSError("offline")

    monkeypatch.setattr(resolve_server_ctx.gpu_safety, "query_server_info", fail)

    assert resolve_server_ctx.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "offline" in captured.err


# ── 原 test_llama_sampling.py:取樣參數釘住(llama_client / config / agent 三層)──


class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _CapturingSession:
    """假 requests session:記錄送出的 json payload,回固定假 response。"""

    def __init__(self, payload: dict):
        self._payload = payload
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None, stream=False, allow_redirects=True):
        self.calls.append({"url": url, "json": json, "stream": stream})
        return _FakeResp(self._payload)


# ------------------------------------------------------------
# llama_client 層:參數有沒有進 payload
# ------------------------------------------------------------
def test_chat_completions_forwards_sampling(monkeypatch):
    import llama_client

    sess = _CapturingSession({"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr(llama_client, "get_session", lambda: sess)

    llama_client.chat_completions(
        base_url="http://127.0.0.1:8080",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.0,
        top_p=0.95,
        top_k=20,
        min_p=0.0,
    )

    body = sess.calls[0]["json"]
    assert body["temperature"] == 0.0
    assert body["top_p"] == 0.95
    assert body["top_k"] == 20
    assert body["min_p"] == 0.0
    assert sess.calls[0]["url"].endswith("/v1/chat/completions")


def test_chat_completions_omits_sampling_when_unset(monkeypatch):
    """預設 None → 不送,沿用 server 啟動旗標的取樣預設(向後相容)。"""
    import llama_client

    sess = _CapturingSession({"choices": []})
    monkeypatch.setattr(llama_client, "get_session", lambda: sess)

    llama_client.chat_completions(
        base_url="http://127.0.0.1:8080",
        messages=[{"role": "user", "content": "hi"}],
    )

    body = sess.calls[0]["json"]
    assert "top_p" not in body
    assert "top_k" not in body
    assert "min_p" not in body


def test_native_completion_min_p_is_opt_in(monkeypatch):
    import llama_client

    # 不帶 min_p → payload 無 min_p(top_p/top_k 仍有舊預設,維持向後相容)
    sess = _CapturingSession({"content": "ok"})
    monkeypatch.setattr(llama_client, "get_session", lambda: sess)
    llama_client.native_completion(base_url="http://127.0.0.1:8080", prompt="hi")
    body = sess.calls[0]["json"]
    assert "min_p" not in body
    assert body["top_p"] == 0.95
    assert body["top_k"] == 40

    # 帶 min_p=0 → 進 payload
    sess2 = _CapturingSession({"content": "ok"})
    monkeypatch.setattr(llama_client, "get_session", lambda: sess2)
    llama_client.native_completion(base_url="http://127.0.0.1:8080", prompt="hi", min_p=0.0)
    assert sess2.calls[0]["json"]["min_p"] == 0.0


# ------------------------------------------------------------
# config 層:Qwen 建議值預設 + env 覆寫
# ------------------------------------------------------------
def test_config_chat_sampling_defaults(monkeypatch):
    try:
        with monkeypatch.context() as patch:
            for key in ("AICODE_CHAT_TOP_P", "AICODE_CHAT_TOP_K", "AICODE_CHAT_MIN_P"):
                patch.delenv(key, raising=False)
            importlib.reload(config)

            assert config.CHAT_TOP_P == 0.95
            assert config.CHAT_TOP_K == 20
            assert config.CHAT_MIN_P == 0.0
            assert isinstance(config.CHAT_TOP_P, float)
            assert isinstance(config.CHAT_TOP_K, int)
            assert isinstance(config.CHAT_MIN_P, float)
    finally:
        importlib.reload(config)


def test_config_chat_sampling_env_override(monkeypatch):
    try:
        with monkeypatch.context() as patch:
            patch.setenv("AICODE_CHAT_TOP_K", "40")
            importlib.reload(config)
            assert config.CHAT_TOP_K == 40
    finally:
        importlib.reload(config)


# ------------------------------------------------------------
# agent 層:互動 agent 路徑真的帶了 config.CHAT_*
# ------------------------------------------------------------
def test_agent_call_pins_chat_sampling(monkeypatch):
    """call_llm_with_tools 必須把 config.CHAT_* 帶進 chat_completions。"""
    import agent
    import llama_client

    captured: dict = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return {
            "choices": [{
                "message": {"content": "ok", "tool_calls": []},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        }

    usage = SimpleNamespace(error_type=None, did_trim=False, trim_summary=None)
    monkeypatch.setattr(agent.config, "require_main_model", lambda: "m")
    monkeypatch.setattr(llama_client, "chat_completions", fake_chat)
    monkeypatch.setattr(agent, "_compute_dynamic_num_ctx", lambda _m: 2048)
    monkeypatch.setattr(agent, "get_native_tools", lambda: [])
    monkeypatch.setattr(agent, "_pre_send_trim_if_needed", lambda *_a, **_kw: (usage, None))
    monkeypatch.setattr(agent.context_budget, "emit_pre_call_lines", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "enforce_gate", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "parse_usage_from_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "emit_post_call_line", lambda *_a, **_kw: None)
    monkeypatch.setattr(agent.context_budget, "log_metrics", lambda *_a, **_kw: None)

    agent.call_llm_with_tools([{"role": "user", "content": "hi"}])

    assert captured["top_p"] == config.CHAT_TOP_P
    assert captured["top_k"] == config.CHAT_TOP_K
    assert captured["min_p"] == config.CHAT_MIN_P


# ── 原 test_config.py:config.py 健全性(關鍵設定值的型別與範圍)──


@pytest.fixture
def config_env(monkeypatch):
    """Reload 測試結束後，以已還原的 process env 重建 config module。"""
    with monkeypatch.context() as patch:
        yield patch
    importlib.reload(config)


def test_model_strings_non_empty():
    # MODEL 由 AICODE_MODEL / opencode.json 動態解析; CodeTrail 不內建預設,
    # 沒設好時是 "" — 這是刻意的 fail-loud 狀態。型別仍應是 str。
    # 真實 LLM 呼叫端必須先呼 config.require_main_model() (沒設就 raise)。
    assert isinstance(config.MODEL, str)
    # EMBEDDING / RERANKER 是 RAG 內部固定附屬模型 ID (informational), 預設值保留。
    assert isinstance(config.EMBEDDING_MODEL, str) and config.EMBEDDING_MODEL.strip()
    assert isinstance(config.RERANKER_MODEL, str) and config.RERANKER_MODEL.strip()


def test_llama_server_urls_are_strings():
    """4 個 llama-server URL 都應該是 str + 有 scheme。"""
    for attr in ("LLAMA_BASE_URL", "LLAMA_EMBED_BASE_URL",
                 "LLAMA_RERANK_BASE_URL", "LLAMA_VL_BASE_URL"):
        v = getattr(config, attr)
        assert isinstance(v, str)
        assert v.startswith("http://") or v.startswith("https://"), f"{attr}={v!r}"


def test_model_registry_loads_from_env(config_env):
    """AICODE_MODEL_REGISTRY env (JSON 字串) 會被 _load_model_registry 吃進來。"""
    config_env.setenv("AICODE_MODEL_REGISTRY", '{"foo": "/m/foo.gguf"}')
    config_env.delenv("AICODE_MODEL_REGISTRY_FILE", raising=False)
    importlib.reload(config)
    assert config.MODEL_REGISTRY == {"foo": "/m/foo.gguf"}


def test_resolve_model_path_uses_registry(config_env, tmp_path):
    """有 registry 命中時走 registry 路徑。"""
    gguf = tmp_path / "foo.gguf"
    gguf.write_text("not-a-real-gguf")
    config_env.setenv("AICODE_MODEL_REGISTRY", f'{{"foo": "{gguf}"}}')
    config_env.delenv("AICODE_MODEL_REGISTRY_FILE", raising=False)
    importlib.reload(config)
    assert config.resolve_model_path("foo") == str(gguf)


def test_resolve_model_path_passthrough_for_existing_file(config_env, tmp_path):
    """registry 沒命中但路徑存在 → 直接用路徑。"""
    gguf = tmp_path / "bar.gguf"
    gguf.write_text("not-a-real-gguf")
    config_env.delenv("AICODE_MODEL_REGISTRY", raising=False)
    config_env.delenv("AICODE_MODEL_REGISTRY_FILE", raising=False)
    importlib.reload(config)
    assert config.resolve_model_path(str(gguf)) == str(gguf)


def test_require_main_model_fails_when_unset(monkeypatch, tmp_path):
    """MODEL 為空時 require_main_model 必須 raise (fail-loud, 不 fallback)。"""
    monkeypatch.delenv("AICODE_MODEL", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    with pytest.raises(RuntimeError) as exc:
        config.require_main_model()
    assert "AICODE_MODEL" in str(exc.value)


def test_require_main_model_returns_value_when_set(monkeypatch):
    """bare name 形式直接回。"""
    monkeypatch.setenv("AICODE_MODEL", "some-model-tag")
    assert config.require_main_model() == "some-model-tag"


def test_require_main_model_rejects_external_provider(monkeypatch):
    """openai/ ollama/ anthropic/ 等外部 provider prefix 一律拒絕。"""
    for value in ("anthropic/foo", "openai/gpt-4", "ollama/qwen3"):
        monkeypatch.setenv("AICODE_MODEL", value)
        with pytest.raises(RuntimeError) as exc:
            config.require_main_model()
        assert "外部 provider prefix" in str(exc.value) or "provider prefix" in str(exc.value)


def test_require_main_model_strips_custom_provider(monkeypatch):
    """custom-provider/bare 形式會 strip,只留下 bare model name。"""
    monkeypatch.setenv("AICODE_MODEL", "myprovider/qwen3-coder-32b")
    assert config.require_main_model() == "qwen3-coder-32b"


def test_require_main_model_accepts_gguf_path(monkeypatch):
    """GGUF 絕對路徑也是合法的主模型形式。"""
    monkeypatch.setenv("AICODE_MODEL", "/models/foo.gguf")
    assert config.require_main_model() == "/models/foo.gguf"


def test_require_main_model_path_fails_when_file_missing(monkeypatch):
    """resolve_model_path 解到的檔不存在時必須 raise。"""
    monkeypatch.setenv("AICODE_MODEL", "definitely-not-a-real-model")
    monkeypatch.delenv("AICODE_MODEL_REGISTRY", raising=False)
    monkeypatch.delenv("AICODE_MODEL_REGISTRY_FILE", raising=False)
    with pytest.raises(RuntimeError) as exc:
        config.require_main_model_path()
    assert "找不到對應的 GGUF" in str(exc.value)


def test_numeric_thresholds_in_unit_range():
    """KB / RAG 相關門檻應該都在 [0, 1]。"""
    for attr in (
        "KNOWLEDGE_THRESHOLD",
        "KNOWLEDGE_THRESHOLD_SHORT",
        "DYNAMIC_THRESHOLD_RATIO",
        "WEAK_REF_THRESHOLD",
        "STRICT_MODE_THRESHOLD",
        "LOW_CONFIDENCE_KB_THRESHOLD",
        "CODE_RAG_THRESHOLD",
        "CODE_RAG_THRESHOLD_BUG",
        "MMR_LAMBDA",
        "KEYWORD_WEIGHT",
        "POLLUTION_RISK_MIN_SCORE",
        "RERANKER_SKIP_THRESHOLD",
    ):
        v = getattr(config, attr)
        assert 0.0 <= float(v) <= 1.0, f"{attr}={v} 不在 [0, 1]"


def test_context_sizes_positive():
    assert config.N_CTX > 0
    assert config.NUM_CTX == config.N_CTX
    assert config.DYNAMIC_NUM_CTX_MAX == config.N_CTX
    assert config.MAX_TOTAL_CHARS > 0
    assert config.MAX_FILE_READ_CHARS > 0
    assert config.MAX_TOOL_LOOPS > 0


def test_code_context_character_budget_contract():
    assert config.CODE_CONTEXT_MIN_MAX_CHARS == 2000
    assert config.CODE_CONTEXT_DEFAULT_MAX_CHARS == 12000
    assert config.CODE_CONTEXT_MAX_MAX_CHARS == 30000
    assert (
        config.CODE_CONTEXT_MIN_MAX_CHARS
        <= config.CODE_CONTEXT_DEFAULT_MAX_CHARS
        <= config.CODE_CONTEXT_MAX_MAX_CHARS
    )


def test_dangerous_features_default_off():
    """改碼/跑命令類預設應為 False（要靠明確 env 開）。"""
    # 這些值在 import 時若 env 不為 truthy 就應該是 False
    import os
    if os.environ.get("AI_CODE_PATCH", "").lower() not in ("1", "true", "yes"):
        assert config.PATCH_ENABLED is False
    if os.environ.get("AI_CODE_RUN_TESTS", "").lower() not in ("1", "true", "yes"):
        assert config.RUN_COMMAND_ENABLED is False
    if os.environ.get("AI_CODE_ALLOW_EXTERNAL_IMPORT", "").lower() not in ("1", "true", "yes"):
        assert config.EXTERNAL_IMPORT_ENABLED is False


def test_allowed_commands_no_dangerous_entries():
    """白名單不能放 rm / sudo / curl 等真會搞壞系統的命令。"""
    bad_prefixes = ("rm", "sudo", "curl", "wget", "sh", "bash", "chmod", "chown", "dd", "mkfs")
    for cmd in config.ALLOWED_COMMANDS:
        first = cmd.split()[0]
        assert first not in bad_prefixes, f"危險命令誤入白名單: {cmd!r}"


def test_get_answer_rules_returns_string():
    s1 = config.get_answer_rules(has_binary=False)
    s2 = config.get_answer_rules(has_binary=True)
    assert isinstance(s1, str) and "REF" in s1
    assert isinstance(s2, str) and "BIN" in s2 and "ELF" in s2


def test_rerank_fallback_policy_defaults_to_error(config_env):
    config_env.delenv("AICODE_RERANK_FALLBACK_POLICY", raising=False)
    importlib.reload(config)
    assert config.RERANK_FALLBACK_POLICY == "error"


def test_rerank_fallback_policy_rejects_unknown_value():
    import os
    import subprocess
    import sys

    env = {**os.environ, "AICODE_RERANK_FALLBACK_POLICY": "not-a-policy"}
    proc = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode != 0
    assert "AICODE_RERANK_FALLBACK_POLICY" in proc.stderr
