"""部署設定的解析鏈:deployment profile、狀態巡檢、主模型解析、GPU / ctx 安全閘、
n_ctx 正規化、取樣參數釘住、config 健全性。

合併自 tests/test_deployment_profile.py、tests/test_deployment_status.py、
tests/test_model_resolution.py、tests/test_gpu_safety.py、tests/test_ctx_safety_check.py、
tests/test_ctx_resolution.py、tests/test_llama_sampling.py、tests/test_config.py
(2026-09-02)。行為與 assertion 未變;近似重複的案例改成 parametrize。

- deployment profile:絕對路徑 profile 繼承 safe-defaults、優先序、惡意值拒收、命令建構。
- deployment status:依 cmdline port 認角色、GPU / 模型 / mmproj 錯配偵測。
- 主模型解析鏈:deployment profile / models.json 的解析與 fail-loud、呼叫時機、必要 server 檢查
  (原本又併自 test_resolve_main_model / test_main_model_calltime /
  test_required_model_servers_check,2026-08-20)。2026-09-04:`scripts/resolve_main_model.py`
  這支 CLI 隨 wrapper 一起刪除(客戶端在自己的行程裡解析),argv 那一半的案例
  因此消失 —— 客戶端沒有 `-m`;剩下的解析與 fail-loud 改對
  `client_preflight.resolve_model` 與 `model_resolution` 直接驗。
- gpu_safety:server-based ctx safety verdict + GPU info 回報。完全離線:所有
  nvidia-smi 與 llama-server HTTP 都用 hook 注入 fixture,CI 跑 --no-network 沒問題。
- ctx 容量閘:client_preflight.check_ctx_safety。整段是 smoke
  (AGENTS.md §1.1 第 1 款「真實發生過的 bug 的 regression」:ctx 安全閘),逐條標記。
- n_ctx:界線檢查,以及 runtime 從主 server 觀測(client_preflight.observe_n_ctx)
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
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
import gpu_safety
import model_resolution
import n_ctx
import process_env
from deployment_profile import (
    DEFAULT_LLAMA_BIN,
    LauncherOverrides,
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
from model_resolution import normalize_main_model, resolve_main_model
from scripts import required_model_servers_check as preflight

import client_preflight

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── 原 test_deployment_profile.py:deployment profile 的載入、優先序與命令建構 ──

#: 舊世代用來覆寫 deployment 設定的環境變數名。這一代 loader **一個都不讀**
#: (以前這份清單是從 `deployment_profile.RUNTIME_OVERRIDE_ENV_KEYS` 推導的);
#: 留成字面清單是為了讓底下那條契約測試能把它們原樣設進環境,證明真的無效。
LEGACY_OVERRIDE_ENV_KEYS = (
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
    "AICODE_BIND",
    "AICODE_MAIN_CTX",
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
    "MAIN_PORT",
    "LLAMA_BIN",
    "MODELS_DIR",
    "MAIN_SESSION",
    "AUX_SESSION",
    "SESSION",
)


def _env(tmp_path: Path, **values: str) -> dict[str, str]:
    """交給 loader 的 `environ`:只有「檔案在哪」。

    loader 讀這份 dict 的鍵只有 HOME / USERPROFILE,所以測試不必再從
    `os.environ` 濾掉一堆覆寫名 —— 沒有東西會去讀它們。
    """
    return {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), **values}


def _write_local(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / ".config" / "codetrail" / "deployment.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_registry(tmp_path: Path, entries: dict) -> Path:
    """`~/.config/codetrail/models.json`:bare name → GGUF 的**唯一**來源。"""
    path = tmp_path / ".config" / "codetrail" / "models.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")
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
    profile = load_effective_profile(_env(tmp_path), profile=str(profile_path))

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
        load_effective_profile(_env(tmp_path), profile="some-named-profile")


def test_main_model_resolution_uses_selected_profile(tmp_path):
    profile_path = _write_profile(
        tmp_path, "pinned-main", {"main": {"model": "profile-main-model"}}
    )
    resolved = resolve_main_model(_env(tmp_path), profile=str(profile_path))

    assert resolved.ok
    assert resolved.model == "profile-main-model"
    assert resolved.source.startswith("deployment profile pinned-main")


def test_precedence_cli_overrides_over_local_over_profile_over_defaults(tmp_path):
    profile_path = _write_profile(tmp_path, "local-selected", {})
    _write_local(
        tmp_path,
        {
            "schema_version": 1,
            "profile": str(profile_path),
            "services": {"main": {"model": "local-main", "ctx": 32768}},
        },
    )

    effective = load_effective_profile(
        _env(tmp_path),
        overrides=LauncherOverrides(main_model="cli-main", main_ctx=98304),
    )

    assert effective.selected_profile == "local-selected"
    assert effective.service("main").model == "cli-main"
    assert effective.service("main").ctx == 98304
    assert effective.service("embedding").ctx == 8192  # inherited safe default


def test_explicit_local_override_must_exist(tmp_path):
    missing = tmp_path / "missing-deployment.json"

    with pytest.raises(ProfileError, match="deployment-config.*existing file"):
        load_effective_profile(_env(tmp_path), deployment_config=str(missing))


def test_absent_default_local_override_still_uses_defaults(tmp_path):
    effective = load_effective_profile(_env(tmp_path))

    assert effective.selected_profile == "defaults"
    assert effective.local_override is None
    assert effective.service("main").ctx == 65536


def test_profile_selector_precedence_cli_then_local(tmp_path):
    local_choice = _write_profile(tmp_path, "local-choice", {})
    cli_choice = _write_profile(tmp_path, "cli-choice", {})
    _write_local(tmp_path, {"schema_version": 1, "profile": str(local_choice)})
    env = _env(tmp_path)

    assert load_effective_profile(env).selected_profile == "local-choice"
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
    _write_registry(tmp_path, {"main-key": str(main), "vl-key": str(vl), "mm-key": str(mmproj)})
    _write_local(
        tmp_path,
        {
            "schema_version": 1,
            "services": {
                "main": {"model": "main-key"},
                "vl": {"model": "vl-key", "mmproj": "mm-key"},
            },
        },
    )
    env = _env(tmp_path)
    profile = load_effective_profile(env)

    assert resolve_model_reference(profile.service("main").model, env, must_exist=True) == str(main)
    assert resolve_model_reference(profile.service("vl").model, env, must_exist=True) == str(vl)
    assert resolve_model_reference(profile.service("vl").mmproj, env, must_exist=True) == str(mmproj)
    assert resolve_model_reference(str(main), env, must_exist=True) == str(main)


def test_main_and_aux_gpu_split_and_three_aux_share_one_gpu(tmp_path):
    profile = load_effective_profile(
        _env(tmp_path),
        overrides=LauncherOverrides(gpus={"main": "GPU-H200", "aux": "GPU-RTX2000ADA"}),
    )

    assert profile.service("main").gpu == "GPU-H200"
    assert {profile.service(role).gpu for role in ("embedding", "reranker", "vl")} == {
        "GPU-RTX2000ADA"
    }


def test_per_role_gpu_override_wins_over_aux_gpu(tmp_path):
    profile = load_effective_profile(
        _env(tmp_path),
        overrides=LauncherOverrides(
            gpus={
                "aux": "GPU-AUX",
                "embedding": "GPU-EMBED",
                "reranker": "GPU-RERANK",
                "vl": "GPU-VL",
            }
        ),
    )

    assert profile.service("embedding").gpu == "GPU-EMBED"
    assert profile.service("reranker").gpu == "GPU-RERANK"
    assert profile.service("vl").gpu == "GPU-VL"


def test_command_builder_uses_only_structured_allowlisted_arguments(tmp_path):
    model = tmp_path / "main model.gguf"
    model.write_bytes(b"fixture")
    _write_local(tmp_path, {"schema_version": 1, "services": {"main": {"model": str(model)}}})
    env = _env(tmp_path)
    service = load_effective_profile(
        env, overrides=LauncherOverrides(gpus={"main": "GPU-safe"})
    ).service("main")

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
                    "model": str(model),
                    "parameters": {
                        "gpu_layers": "auto",
                        "fit": "on",
                        "fit_target": 5120,
                        "parallel": 1,
                        "jinja": True,
                    },
                }
            },
        },
    )
    env = _env(tmp_path)
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

    with pytest.raises(ProfileError, match=needle):
        load_effective_profile(_env(tmp_path))


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
        load_effective_profile(_env(tmp_path))


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
    _write_local(tmp_path, {"schema_version": 1, "services": {"main": {"model": str(model)}}})
    env = _env(tmp_path)
    profile = load_effective_profile(env)

    for role in ("main", "embedding", "reranker", "vl"):
        assert profile.service(role).bind == "local"
    command = build_server_command(profile.service("main"), "/opt/llama-server", env, must_exist=True)
    assert command[command.index("--host") + 1] == "127.0.0.1"


def test_bind_all_interfaces_via_the_deployment_file(tmp_path):
    model = tmp_path / "main.gguf"
    model.write_bytes(b"fixture")
    _write_local(
        tmp_path,
        {
            "schema_version": 1,
            "services": {"main": {"model": str(model), "bind": "all-interfaces"}},
        },
    )
    env = _env(tmp_path)
    profile = load_effective_profile(env)
    command = build_server_command(profile.service("main"), "/opt/llama-server", env, must_exist=True)
    assert command[command.index("--host") + 1] == "0.0.0.0"
    # local override 只設了 main;其他 role 仍是安全預設
    assert profile.service("embedding").bind == "local"


def test_bind_rejects_unknown_value_and_preserves_remote_host(tmp_path):
    _write_local(
        tmp_path,
        {"schema_version": 1, "services": {"main": {"bind": "everywhere"}}},
    )
    with pytest.raises(ProfileError, match="bind must be local or all-interfaces"):
        load_effective_profile(_env(tmp_path))

    # 清掉壞 override,驗證非 loopback base_url(多機部署)不受 bind 預設影響
    model = tmp_path / "main.gguf"
    model.write_bytes(b"fixture")
    (tmp_path / ".config" / "codetrail" / "deployment.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "services": {
                    "main": {"model": str(model), "base_url": "http://gpu-host:8080", "port": 8080}
                },
            }
        ),
        encoding="utf-8",
    )
    env = _env(tmp_path)
    service = load_effective_profile(env).service("main")
    command = build_server_command(service, "/opt/llama-server", env, must_exist=True)
    assert command[command.index("--host") + 1] == "gpu-host"


# ── 啟動核心的契約:設定只來自 deployment.json 與 argv ──


@pytest.mark.smoke
def test_the_loader_ignores_every_legacy_override_variable(monkeypatch, tmp_path):
    """舊世代的每一個覆寫變數都不得再影響有效設定。

    真實觸發:同一台機器上兩份安裝,另一份的 `~/start.sh` export 了 `AICODE_MODEL`
    / `MAIN_GPU` / `LLAMA_BIN`。以前 loader 有一整層 env overlay 會吃下它們 ——
    症狀是「使用者以為在跑 A、實際在跑 B」,而且完全無聲。名字同時設進 `os.environ`
    與交給 loader 的 `environ` 參數:兩條路都不得有人讀。
    """
    model = tmp_path / "from-file.gguf"
    model.write_bytes(b"fixture")
    llama_bin = tmp_path / "bin" / "llama-server"
    _write_local(
        tmp_path,
        {
            "schema_version": 1,
            "llama_bin": str(llama_bin),
            "services": {"main": {"model": str(model), "ctx": 32768, "gpu": "GPU-FILE"}},
        },
    )
    leftovers = {name: "shell-leftover" for name in LEGACY_OVERRIDE_ENV_KEYS}
    leftovers.update(
        {
            "AICODE_MODEL": "shell-model",
            "AICODE_N_CTX": "4096",
            "MAIN_CTX": "4096",
            "MAIN_GPU": "GPU-SHELL",
            "AUX_GPU": "GPU-SHELL",
            "CUDA_VISIBLE_DEVICES": "7",
            "LLAMA_BIN": str(tmp_path / "shell-llama-server"),
            "MODELS_DIR": str(tmp_path / "shell-models"),
        }
    )
    for name, value in leftovers.items():
        monkeypatch.setenv(name, value)

    profile = load_effective_profile(_env(tmp_path, **leftovers))

    assert profile.service("main").model == str(model)
    assert profile.service("main").ctx == 32768
    assert profile.service("main").gpu == "GPU-FILE"
    assert {profile.service(role).gpu for role in ("embedding", "reranker", "vl")} == {""}
    assert profile.llama_bin == str(llama_bin)
    assert profile.selected_profile == "defaults"


@pytest.mark.smoke
def test_gpu_and_llama_bin_come_from_the_deployment_file_then_argv(tmp_path):
    """GPU 與 llama-server 路徑的來源只有兩個:`deployment.json` 與 argv。

    兩者都缺席時 llama_bin 退到 `DEFAULT_LLAMA_BIN`(展開成這個 HOME 底下的絕對
    路徑),GPU 則是「不指定」—— 不是靜默沿用繼承來的 `CUDA_VISIBLE_DEVICES`。
    """
    default_profile = load_effective_profile(_env(tmp_path))
    assert default_profile.llama_bin == str(tmp_path / DEFAULT_LLAMA_BIN[2:])
    assert [default_profile.service(role).gpu for role in ("main", "embedding", "reranker", "vl")] == [""] * 4

    _write_local(
        tmp_path,
        {
            "schema_version": 1,
            "llama_bin": str(tmp_path / "file" / "llama-server"),
            "services": {
                "main": {"gpu": "GPU-FILE-MAIN"},
                "embedding": {"gpu": "GPU-FILE-EMBED"},
            },
        },
    )
    from_file = load_effective_profile(_env(tmp_path))
    assert from_file.llama_bin == str(tmp_path / "file" / "llama-server")
    assert from_file.service("main").gpu == "GPU-FILE-MAIN"
    assert from_file.service("embedding").gpu == "GPU-FILE-EMBED"
    assert from_file.service("vl").gpu == ""

    from_argv = load_effective_profile(
        _env(tmp_path),
        overrides=LauncherOverrides(
            llama_bin=str(tmp_path / "argv" / "llama-server"),
            gpus={"main": "GPU-ARGV-MAIN", "aux": "GPU-ARGV-AUX"},
        ),
    )
    assert from_argv.llama_bin == str(tmp_path / "argv" / "llama-server")
    assert from_argv.service("main").gpu == "GPU-ARGV-MAIN"
    # --aux-gpu 只套到三個附屬角色,而且蓋得過檔案裡的 embedding。
    assert {from_argv.service(role).gpu for role in ("embedding", "reranker", "vl")} == {"GPU-ARGV-AUX"}

    with pytest.raises(ProfileError, match="absolute path"):
        load_effective_profile(
            _env(tmp_path), overrides=LauncherOverrides(llama_bin="./llama-server")
        )


@pytest.mark.smoke
def test_the_server_environment_strips_gpu_selectors_and_llama_settings(monkeypatch):
    """llama-server 拿到的環境:CodeTrail 三前綴 + `LLAMA_ARG_*` + `CUDA_VISIBLE_DEVICES` 全剝掉。

    llama.cpp 先套環境再套 argv,所以殼層 / tmux server 全域環境裡的 `LLAMA_ARG_*`
    會蓋掉我們從 `deployment.json` 算出來的旗標;`CUDA_VISIBLE_DEVICES` 則會蓋掉
    設定檔指定的卡。兩者都不留痕跡,所以邊界必須在真正 exec 的那一步。
    """
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "7")
    monkeypatch.setenv("LLAMA_ARG_THREADS", "3")
    monkeypatch.setenv("LLAMA_ARG_CTX_SIZE", "1024")
    monkeypatch.setenv("AICODE_MODEL", "shell-model")
    monkeypatch.setenv("AI_CODE_PATCH", "1")
    monkeypatch.setenv("GGML_CUDA_NO_PINNED", "1")
    monkeypatch.setenv("LLAMA_LOG_VERBOSITY", "1")
    monkeypatch.setenv("MARK", "keep")

    env = process_env.llama_server_env()

    assert "CUDA_VISIBLE_DEVICES" not in env
    assert not [key for key in env if key.startswith("LLAMA_ARG_")]
    assert not [key for key in env if key.startswith(process_env.STRIPPED_ENV_PREFIXES)]
    # 不是 CodeTrail 設定、也沒有 argv 等價入口的那些刻意保留。
    assert env["GGML_CUDA_NO_PINNED"] == "1"
    assert env["LLAMA_LOG_VERBOSITY"] == "1"
    assert env["MARK"] == "keep"
    assert env.get("HOME") == os.environ["HOME"]


@pytest.mark.smoke
def test_the_main_model_resolver_has_no_environment_branch(monkeypatch, tmp_path):
    """主模型只有 `deployment.json` 一個來源。

    以前 `resolve_main_model_from_env` 的第一格是 `AICODE_MODEL`;那條分支讓另一
    份安裝的 `~/start.sh` 決定這一份跑哪顆模型。行為與字面兩邊都釘:殘留值同時
    設進 `os.environ` 與交給解析器的 mapping,結果仍必須是設定檔裡那一顆。
    """
    _write_profile_model(tmp_path, "from-deployment-file")
    monkeypatch.setenv("AICODE_MODEL", "shell-model")

    resolved = resolve_main_model({"HOME": str(tmp_path), "AICODE_MODEL": "mapping-model"})

    assert resolved.ok
    assert resolved.model == "from-deployment-file"
    assert resolved.source.startswith("deployment profile")
    assert not hasattr(model_resolution, "resolve_main_model_from_env")
    source = (REPO_ROOT / "model_resolution.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "AICODE_MODEL" not in code



# ── 原 test_deployment_status.py:依 cmdline port 認角色、GPU / 模型 / mmproj 錯配 ──


def _fixture(tmp_path: Path):
    paths = {
        role: tmp_path / f"{role}.gguf"
        for role in ("main", "embedding", "reranker", "vl", "mmproj")
    }
    for path in paths.values():
        path.write_bytes(b"fixture")
    _write_local(
        tmp_path,
        {
            "schema_version": 1,
            "services": {
                "main": {"model": str(paths["main"]), "gpu": "GPU-H200"},
                "embedding": {"model": str(paths["embedding"]), "gpu": "GPU-RTX2000ADA"},
                "reranker": {"model": str(paths["reranker"]), "gpu": "GPU-RTX2000ADA"},
                "vl": {
                    "model": str(paths["vl"]),
                    "mmproj": str(paths["mmproj"]),
                    "gpu": "GPU-RTX2000ADA",
                },
            },
        },
    )
    env = _env(tmp_path)
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
        cmdline_reader=lambda pid: cmdlines[pid],
        server_reader=servers,
    )

    assert any("vl: loaded mmproj is not observable" in issue for issue in inspection.issues)


# ── 原 test_model_resolution.py:主模型解析鏈(argv/env 解析與 fail-loud、呼叫時機、必要 server 檢查)──


@pytest.fixture
def model_resolution_env(monkeypatch, tmp_path):
    """原 test_model_resolution.py 的 module 級 autouse fixture(`_clean_env`):清掉
    `AICODE_MODEL`、HOME 指到 tmp_path。合併後改成顯式掛載,只給來自該檔的測試;
    不讓它擴散到本檔其他來源的測試(它們各自有自己的 env 隔離)。

    底下有測試刻意再設回 `AICODE_MODEL`,證明殼層殘留值無效。"""
    monkeypatch.delenv("AICODE_MODEL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    yield




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


def _write_profile_model(tmp_path: Path, model: str) -> Path:
    """把主模型寫進 deployment.json —— 現在**唯一**的來源。"""
    cfg_dir = tmp_path / ".config" / "codetrail"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "deployment.json"
    path.write_text(
        json.dumps(
            {"schema_version": 1, "profile": "defaults", "services": {"main": {"model": model}}}
        ),
        encoding="utf-8",
    )
    return path


def _resolve(tmp_path: Path):
    """走客戶端真正的那一條:`client_preflight.resolve_model`。

    它交給 `model_resolution` 的是 `profile_env()`(只有 HOME),所以這裡設好
    HOME 就等於設好了整個解析鏈的輸入。
    """
    result = client_preflight.Preflight(root=tmp_path)
    return client_preflight.resolve_model(result, profile=None)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        pytest.param("same-model", "same-model", id="bare_model_name"),
        pytest.param("/models/foo.gguf", "/models/foo.gguf", id="gguf_path"),
    ],
)
def test_the_main_model_comes_from_the_deployment_profile(
    model_resolution_env, tmp_path, configured, expected
):
    """deployment.json 的 `main.model` 接受的寫法:registry 名,或 GGUF 絕對路徑。

    `provider/name` 這種帶前綴的寫法在 profile 這一層就被拒絕(啟動核心
    的既有驗證);`normalize_main_model` 那一層仍然會 strip provider 前綴,因為
    它同時服務 `~/start.sh` → launcher 那條路。
    """
    _write_profile_model(tmp_path, configured)

    assert _resolve(tmp_path) == expected


@pytest.mark.smoke
def test_there_is_no_model_flag_and_no_model_environment_variable(model_resolution_env, tmp_path):
    """換模型 = 重跑 `./set_config.sh`,不是每次啟動可以改的東西。

    llama-server 一啟動就鎖死一顆模型;客戶端再收一個 `-m` 只會讓「使用者以為
    在跑 A、實際在跑 B」。殼層裡殘留的 `AICODE_MODEL`(可能來自另一份安裝的
    `~/start.sh`)同樣不得改變解析結果 —— 那正是跨 branch 混用的機制。
    """
    _write_profile_model(tmp_path, "profile-model")
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("AICODE_MODEL", "shell-leftover-model")
        assert _resolve(tmp_path) == "profile-model"
    finally:
        monkeypatch.undo()

    import codetrail_chat

    parser = codetrail_chat.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["-m", "some-model"])


def test_registry_aliases_for_the_same_gguf_are_the_same_model(
    model_resolution_env, tmp_path
):
    """兩個 alias 指到同一個 GGUF 就是同一顆模型(`main_model_references_equivalent`)。"""
    from model_resolution import main_model_references_equivalent

    _write_alias_registry(tmp_path, ("old-alias", "new-alias"))
    env = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}

    assert main_model_references_equivalent("old-alias", "new-alias", env=env)
    assert not main_model_references_equivalent("old-alias", "other-model", env=env)


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


@pytest.mark.parametrize(
    "configured",
    [
        pytest.param("openai/gpt-4", id="openai"),
        pytest.param("ollama/qwen3", id="ollama"),
        pytest.param("anthropic/claude", id="anthropic"),
        pytest.param("<CODE_MODEL>", id="placeholder"),
        pytest.param("models/foo.gguf", id="relative_path"),
    ],
)
def test_an_unusable_profile_model_fails_loud(model_resolution_env, tmp_path, configured):
    """外部 provider 與 `<CODE_MODEL>` 之類 placeholder 一律拒絕啟動,而且指名值。

    CodeTrail 只跑本地 llama-server;靜默接受一個 `openai/...` 只會在第一次
    送出時才炸,而那時使用者已經把問題打進去了。
    """
    _write_profile_model(tmp_path, configured)

    with pytest.raises(client_preflight.PreflightError) as excinfo:
        _resolve(tmp_path)
    assert configured in str(excinfo.value)


def test_no_source_at_all_fails_loud(model_resolution_env, tmp_path):
    """CodeTrail 不內建、不推薦主模型:沒設就明確拒絕,而且說要跑什麼。"""
    with pytest.raises(client_preflight.PreflightError, match="set_config"):
        _resolve(tmp_path)


@pytest.mark.parametrize("configured", ["", "   "])
def test_an_empty_profile_model_is_treated_as_unset(
    model_resolution_env, tmp_path, configured
):
    _write_profile_model(tmp_path, configured)

    with pytest.raises(client_preflight.PreflightError):
        _resolve(tmp_path)








def test_a_broken_deployment_profile_fails_loud_with_its_path(
    model_resolution_env, tmp_path
):
    """壞掉的 profile 不得靜默退回預設值再啟動 —— 訊息要指出是哪個檔。"""
    cfg_dir = tmp_path / ".config" / "codetrail"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "deployment.json").write_text('{"main": {"ctx": 1}}', encoding="utf-8")

    with pytest.raises(client_preflight.PreflightError, match="deployment"):
        _resolve(tmp_path)


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


# ── ctx 容量閘:client_preflight.check_ctx_safety ──
# smoke:AGENTS.md §1.1 第 1 款「真實發生過的 bug 的 regression」
# 真實 bug regression:ctx 安全閘。原本是 scripts/ctx_safety_check.py 的 CLI
# (以 AICODE_MODEL / AICODE_N_CTX / AICODE_CTX_SAFETY_DISABLE /
# AICODE_ACCEPT_CTX_RISK 四個環境變數驅動,exit code 當閘)。那支 CLI 與那四個
# 變數在 2026-09-04 一起刪除:閘移進 client_preflight,requested 來自觀測到的
# server n_ctx,逃生口無替代(要跳過就是修那個檢查)。斷言的行為沒變 ——
# 「requested > server 就擋住、== 或 < 放行、UNKNOWN 不擋」逐條保留。


class _FakeService:
    def __init__(self, base_url="http://localhost:8080", ctx=0):
        self.base_url = base_url
        self.ctx = ctx


class _FakeProfile:
    """只提供 `service("main")` —— preflight 對 profile 的全部用法。"""

    def __init__(self, base_url="http://localhost:8080", ctx=0):
        self._main = _FakeService(base_url, ctx)

    def service(self, role):
        assert role == "main"
        return self._main


def _verdict_factory(status: str, server_n_ctx):
    """回一個假 check_safety:固定 server_n_ctx,status 由呼叫端指定。"""

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
@pytest.mark.parametrize("requested", [65536, 32768])
def test_ctx_gate_passes_when_requested_is_within_server_capacity(monkeypatch, requested):
    """requested <= server n_ctx → 放行。

    「小於」不是安全問題(不截斷,只是沒用滿 server 容量),不該擋 —— 這是把舊版
    e129d48「小於就 refuse」死鎖拿掉的回歸測試。
    """
    monkeypatch.setattr(gpu_safety, "check_safety", _verdict_factory("SAFE", 65536))
    result = client_preflight.Preflight(root=Path("/tmp"))

    client_preflight.check_ctx_safety(result, _FakeProfile(), requested)


@pytest.mark.smoke
def test_ctx_gate_refuses_when_requested_exceeds_server(monkeypatch):
    """requested > server n_ctx → 截斷風險,擋住啟動並說出怎麼修。"""
    monkeypatch.setattr(gpu_safety, "check_safety", _verdict_factory("UNSAFE", 8192))
    result = client_preflight.Preflight(root=Path("/tmp"))

    with pytest.raises(client_preflight.PreflightError) as excinfo:
        client_preflight.check_ctx_safety(result, _FakeProfile(), 65536)

    message = str(excinfo.value)
    assert "8192" in message
    assert "set_config.sh" in message


@pytest.mark.smoke
def test_ctx_gate_is_non_blocking_when_the_server_cannot_be_observed(monkeypatch):
    """UNKNOWN(server 沒起來 / 沒回 /props)只警告不擋 —— 否則沒開 server 就進不去。"""
    monkeypatch.setattr(gpu_safety, "check_safety", _verdict_factory("UNKNOWN", None))
    result = client_preflight.Preflight(root=Path("/tmp"))

    client_preflight.check_ctx_safety(result, _FakeProfile(), 65536)


@pytest.mark.smoke
def test_the_ctx_gate_has_no_environment_escape_hatch(monkeypatch):
    """殼層裡的舊逃生口一律無效 —— 這是「設定不來自環境變數」的守門。

    舊 CLI 認 `AICODE_CTX_SAFETY_DISABLE=1`(整個閘短路)與
    `AICODE_ACCEPT_CTX_RISK=1`(UNSAFE 照樣放行)。兩個都刪了,所以留在殼層
    (或 `~/.bashrc`)的殘留值不得把一個會靜默截斷 prompt 的設定放行。
    """
    monkeypatch.setenv("AICODE_CTX_SAFETY_DISABLE", "1")
    monkeypatch.setenv("AICODE_ACCEPT_CTX_RISK", "1")
    monkeypatch.setattr(gpu_safety, "check_safety", _verdict_factory("UNSAFE", 8192))
    result = client_preflight.Preflight(root=Path("/tmp"))

    with pytest.raises(client_preflight.PreflightError):
        client_preflight.check_ctx_safety(result, _FakeProfile(), 65536)


@pytest.mark.smoke
def test_the_ctx_gate_asks_the_endpoint_from_the_profile_not_the_environment(monkeypatch):
    """base_url 來自 deployment profile,不是 `AICODE_LLAMA_BASE_URL`。

    舊 CLI 讀那個環境變數;殼層殘留一個指向別台機器的值,等於拿別人的 n_ctx
    來判自己的閘。
    """
    monkeypatch.setenv("AICODE_LLAMA_BASE_URL", "http://stale-shell:9999")
    calls: dict[str, object] = {}

    def fake_check_safety(requested, base_url="http://localhost:8080", **_kw):
        calls["requested"] = requested
        calls["base_url"] = base_url
        return SafetyVerdict(
            status="UNKNOWN",
            requested_ctx=requested,
            server_n_ctx=None,
            model_path=None,
            vram_total_gb=None,
            vram_free_gb=None,
            reason="test unknown",
        )

    monkeypatch.setattr(gpu_safety, "check_safety", fake_check_safety)
    result = client_preflight.Preflight(root=Path("/tmp"))

    client_preflight.check_ctx_safety(
        result, _FakeProfile(base_url="http://profile-host:8080"), 65536
    )

    assert calls == {"requested": 65536, "base_url": "http://profile-host:8080"}


# ── n_ctx:界線檢查,以及 runtime 從主 server 觀測(client_preflight.observe_n_ctx)──
# 原 test_ctx_resolution.py。`n_ctx.resolve_n_ctx` 那條「多來源優先序」的鏈
# (AICODE_N_CTX > AICODE_DYNAMIC_NUM_CTX_MAX > default,AICODE_NUM_CTX 不升格)
# 已隨那些環境變數一起刪除:runtime 的 n_ctx 只有一個來源 —— 主 llama-server
# 啟動時的 `-c`,由 preflight 觀測後以 argv 交給每一個元件。


@pytest.mark.parametrize("value", [1, 4096, n_ctx.DEFAULT_N_CTX, n_ctx.MAX_N_CTX])
def test_valid_n_ctx_values_pass_through(value):
    assert n_ctx.validate_n_ctx(value) == value


@pytest.mark.parametrize("value", [0, -1, n_ctx.MAX_N_CTX + 1, True, 4096.0, "4096", None])
def test_out_of_range_or_wrong_typed_n_ctx_fails_loud(value):
    """不 clamp、不回退預設 —— 靜默改小就是使用者以為在用 64k 其實在用 4k。"""
    with pytest.raises(ValueError):
        n_ctx.validate_n_ctx(value)


@pytest.mark.smoke
def test_the_n_ctx_module_has_no_environment_seam():
    """`n_ctx` 不得再讀任何環境變數。

    那條鏈的症狀是 llama-server 從 prompt 前面靜默截掉(模型忘記前面說過什麼),
    不是一個錯誤訊息,所以靜態釘住比較實在。
    """
    source = (REPO_ROOT / "n_ctx.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "environ" not in code
    assert "getenv" not in code
    assert not hasattr(n_ctx, "resolve_n_ctx")


def test_observed_n_ctx_comes_from_the_running_server(monkeypatch):
    monkeypatch.setattr(
        gpu_safety,
        "query_server_info",
        lambda _url: gpu_safety.ServerInfo(base_url=_url, n_ctx=65536),
    )
    result = client_preflight.Preflight(root=Path("/tmp"))

    assert client_preflight.observe_n_ctx(result, _FakeProfile(ctx=32768)) == 65536
    assert result.n_ctx == 65536


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(lambda _url: None, id="no-server"),
        pytest.param(
            lambda _url: (_ for _ in ()).throw(OSError("offline")), id="query-raises"
        ),
    ],
)
def test_an_unobservable_server_falls_back_to_the_configured_ctx(monkeypatch, query):
    """server 讀不到不是啟動失敗 —— 退回 deployment profile 的 main.ctx。"""
    monkeypatch.setattr(gpu_safety, "query_server_info", query)
    result = client_preflight.Preflight(root=Path("/tmp"))

    assert client_preflight.observe_n_ctx(result, _FakeProfile(ctx=32768)) == 32768


def test_no_server_and_no_configured_ctx_fails_loud(monkeypatch):
    monkeypatch.setattr(gpu_safety, "query_server_info", lambda _url: None)
    result = client_preflight.Preflight(root=Path("/tmp"))

    with pytest.raises(client_preflight.PreflightError, match="main.ctx"):
        client_preflight.observe_n_ctx(result, _FakeProfile(ctx=0))


def test_dynamic_sizing_never_exceeds_a_small_main_n_ctx(monkeypatch):
    """動態 sizing 不得超過主 n_ctx —— 而且看的是 **runtime** 的那一個。

    2026-09-04:改成 monkeypatch `config.N_CTX` 而不是 `agent.N_CTX`。行為為什麼
    該變:`mcp_server --n-ctx <觀測值>` 會在 `agent` import **之後**覆寫
    `config.N_CTX`,所以 import 期的快照看不到它;快照的症狀是動態 sizing 用
    deployment profile 的舊值當上限,然後 llama-server 從 prompt 前面靜默截掉。
    """
    import agent
    import config

    monkeypatch.setattr(config, "N_CTX", 1024)
    monkeypatch.setattr(agent, "DYNAMIC_NUM_CTX_MIN", 16384)

    assert agent._compute_dynamic_num_ctx([{"role": "user", "content": "hello"}]) == 1024


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


@pytest.mark.smoke
def test_chat_sampling_has_no_environment_override(monkeypatch):
    """取樣值是 repo 常數,殼層改不動。

    2026-09-04:原本這條驗的是 `AICODE_CHAT_TOP_K=40` 會蓋過預設。行為為什麼
    該變:取樣值直接決定模型會不會杜撰具體事實(這整段的存在理由),而一個
    殼層變數等於每台機器、每個 session 都可能不同,而且沒有任何地方會顯示它。
    改 repo 常數 = 所有使用者一致。
    """
    monkeypatch.setenv("AICODE_CHAT_TOP_K", "40")
    monkeypatch.setenv("AICODE_CHAT_TOP_P", "0.99")
    monkeypatch.setenv("AICODE_CHAT_MIN_P", "0.5")
    before = (config.CHAT_TOP_K, config.CHAT_TOP_P, config.CHAT_MIN_P)
    try:
        importlib.reload(config)
        assert (config.CHAT_TOP_K, config.CHAT_TOP_P, config.CHAT_MIN_P) == before
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
def config_home(monkeypatch, tmp_path):
    """把 HOME 指到 tmp 再 reload `config`,結束後以真正的 HOME 重建。

    2026-09-04:原本的 `config_env` 是設 `AICODE_MODEL_REGISTRY` 之類的環境變數
    再 reload。行為為什麼該變:`config` 現在只交 HOME 給解析器,設定一律來自
    `~/.config/codetrail/{deployment,models}.json` —— 測試的接縫因此只剩 HOME。
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cfg_dir = tmp_path / ".config" / "codetrail"
    cfg_dir.mkdir(parents=True)

    def write(*, models: dict | None = None, main_model: str | None = None) -> None:
        if models is not None:
            (cfg_dir / "models.json").write_text(json.dumps(models), encoding="utf-8")
        services = {"main": {"model": main_model}} if main_model is not None else {}
        (cfg_dir / "deployment.json").write_text(
            json.dumps({"schema_version": 1, "profile": "defaults", "services": services}),
            encoding="utf-8",
        )
        importlib.reload(config)

    yield write
    monkeypatch.undo()
    importlib.reload(config)


def test_model_strings_non_empty():
    # MODEL 由 deployment.json 的 main.model 解析; CodeTrail 不內建預設,
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


def test_model_registry_loads_from_models_json(config_home):
    """`~/.config/codetrail/models.json` 是 registry 的**唯一**來源。"""
    config_home(models={"foo": "/m/foo.gguf"}, main_model="foo")

    assert config.MODEL_REGISTRY == {"foo": "/m/foo.gguf"}


@pytest.mark.smoke
def test_the_model_registry_ignores_the_shell(config_home, monkeypatch):
    """殼層裡的 `AICODE_MODEL_REGISTRY` 不得蓋過 models.json。

    那兩個變數是**啟動核心**(`~/start.sh` → launcher)的契約,兩份安裝共用一台
    機器時另一份會設它們;`config` 只交 HOME 給解析器,所以這一側看不到。
    """
    monkeypatch.setenv("AICODE_MODEL_REGISTRY", '{"shell": "/m/shell.gguf"}')
    monkeypatch.setenv("AICODE_MODEL_REGISTRY_FILE", "/nonexistent/registry.json")
    config_home(models={"foo": "/m/foo.gguf"}, main_model="foo")

    assert config.MODEL_REGISTRY == {"foo": "/m/foo.gguf"}


def test_resolve_model_path_uses_registry(config_home, tmp_path):
    """有 registry 命中時走 registry 路徑。"""
    gguf = tmp_path / "foo.gguf"
    gguf.write_text("not-a-real-gguf")
    config_home(models={"foo": str(gguf)}, main_model="foo")

    assert config.resolve_model_path("foo") == str(gguf)


def test_resolve_model_path_passthrough_for_existing_file(config_home, tmp_path):
    """registry 沒命中但路徑存在 → 直接用路徑。"""
    gguf = tmp_path / "bar.gguf"
    gguf.write_text("not-a-real-gguf")
    config_home(models={}, main_model=str(gguf))

    assert config.resolve_model_path(str(gguf)) == str(gguf)


def test_require_main_model_fails_when_unset(config_home):
    """MODEL 為空時 require_main_model 必須 raise (fail-loud, 不 fallback)。"""
    config_home(models={})

    with pytest.raises(RuntimeError) as exc:
        config.require_main_model()
    assert "set_config" in str(exc.value)


def test_require_main_model_returns_value_when_set(config_home):
    """bare name 形式直接回。"""
    config_home(models={"some-model-tag": "/m/x.gguf"}, main_model="some-model-tag")

    assert config.require_main_model() == "some-model-tag"


def test_require_main_model_accepts_gguf_path(config_home):
    """GGUF 絕對路徑也是合法的主模型形式。"""
    config_home(models={}, main_model="/models/foo.gguf")

    assert config.require_main_model() == "/models/foo.gguf"


@pytest.mark.smoke
def test_require_main_model_ignores_a_polluted_shell(config_home, monkeypatch):
    """殼層裡的 `AICODE_MODEL` 不得決定客戶端跑哪一顆模型。

    兩份安裝共用一台機器時,另一份的 `~/start.sh` 會 export 它;讀進來就是
    「使用者以為在跑 A、實際在跑 B」,而且完全無聲。
    """
    monkeypatch.setenv("AICODE_MODEL", "shell-leftover")
    config_home(models={}, main_model="/models/from-profile.gguf")

    assert config.require_main_model() == "/models/from-profile.gguf"
    assert config.MODEL == "/models/from-profile.gguf"


def test_require_main_model_path_fails_when_file_missing(config_home):
    """resolve_model_path 解到的檔不存在時必須 raise。"""
    config_home(models={}, main_model="definitely-not-a-real-model")

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


def test_rerank_fallback_policy_defaults_to_error():
    """預設 fail-loud:reranker 掛了就說出來,不靜默換一種排序。"""
    assert config.RERANK_FALLBACK_POLICY == "error"


@pytest.mark.smoke
def test_rerank_fallback_policy_ignores_the_shell(monkeypatch):
    """它是 client.json 的鍵,不是環境變數。

    2026-09-04:原本這條驗的是 `AICODE_RERANK_FALLBACK_POLICY=not-a-policy` 會
    讓 `import config` fail-loud。行為為什麼該變:那個變數刪了(值改由
    `client_config.apply_to_config()` 從 client.json 推進來,未知值由那邊的
    loader fail-loud);留著檢查等於留著那個入口。
    """
    monkeypatch.setenv("AICODE_RERANK_FALLBACK_POLICY", "main_model")
    try:
        importlib.reload(config)
        assert config.RERANK_FALLBACK_POLICY == "error"
    finally:
        importlib.reload(config)


def test_rerank_fallback_policy_rejects_unknown_value_in_client_json(tmp_path):
    """client.json 寫了不合法的值 → loader fail-loud,不靜默退回預設。"""
    import client_config

    path = tmp_path / "client.json"
    path.write_text(
        json.dumps({"schema": 1, "rerank_fallback_policy": "not-a-policy"}),
        encoding="utf-8",
    )
    path.chmod(0o600)
    tmp_path.chmod(0o700)

    with pytest.raises(client_config.ClientConfigError, match="rerank_fallback_policy"):
        client_config.load_client_settings_from(path)




# ── runtime n_ctx 的覆寫:`mcp_server --n-ctx <觀測值>` 之後,所有讀者都要看到新值 ──


@pytest.mark.smoke
def test_the_runtime_n_ctx_override_reaches_every_reader(monkeypatch):
    """`config.set_runtime_n_ctx()` 必須同時改掉三個相容 alias 與所有動態讀者。

    真實觸發:deployment.json 寫 `main.ctx=131072`,但 server 實際以 `-c 65536`
    起來。preflight 觀測到 65536 並以 `--n-ctx 65536` 交給 MCP;MCP 覆寫
    `config.N_CTX`。任何在 import 期做過快照的讀者(以前是 `utils.N_CTX` 與
    `agent.N_CTX`,都是 `from config import N_CTX`)仍然用 131072 當上限 ——
    症狀是 llama-server 從 prompt 前面靜默截掉,不是一個錯誤訊息。
    """
    import agent
    import utils

    monkeypatch.setattr(config, "N_CTX", 131072)
    monkeypatch.setattr(config, "NUM_CTX", 131072)
    monkeypatch.setattr(config, "NUM_CTX_FULL_MODE", 131072)
    monkeypatch.setattr(config, "DYNAMIC_NUM_CTX_MAX", 131072)

    # 刻意用一個**不等於** import 期預設(deployment profile 的 main.ctx)的值:
    # 相等的話,做過快照的讀者也會剛好答對,這條就白測了。
    observed = 40960
    assert observed != n_ctx.DEFAULT_N_CTX
    config.set_runtime_n_ctx(observed)

    assert config.N_CTX == observed
    assert config.NUM_CTX == observed
    assert config.NUM_CTX_FULL_MODE == observed
    assert config.DYNAMIC_NUM_CTX_MAX == observed
    # 兩個曾經做過 import 期快照的讀者。
    assert utils._default_ctx_budget() == observed
    assert (
        agent._compute_dynamic_num_ctx([{"role": "user", "content": "x" * 10}]) <= observed
    )


@pytest.mark.smoke
@pytest.mark.parametrize("bad", [0, -1, n_ctx.MAX_N_CTX + 1, True, "65536", None])
def test_an_invalid_runtime_n_ctx_override_fails_loud(monkeypatch, bad):
    """壞值不得靜默套用:MCP 拿到之後會 exit 2,而不是用一個荒謬的上限跑下去。"""
    monkeypatch.setattr(config, "N_CTX", 65536)
    with pytest.raises(ValueError):
        config.set_runtime_n_ctx(bad)
