"""審核模型(auditor)角色的部署契約:升級相容、fail-loud 的位置與 A/B 授權。

舊安裝升級後 deployment.json / client.json 只有四個角色。它們必須**載入得了**
(config 在 import 期就讀 profile;載入失敗等於 aicode、MCP、doctor、連 set_config
自己都起不來,修法也印不出來),但審核模型在**使用前** fail-loud,而且修法只有一個
來源(`deployment_profile.auditor_unconfigured_reason`)。反過來,舊 B 端的四角色授權
不得順手放行任何審核模型端點。全部離線、合成檔案,不碰 GPU / tmux / 網路。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import client_config
import config
import deployment_profile as deployment
import endpoint_policy
import model_identity
from scripts import check_status, doctor, launch_servers, set_config, stop_servers
from scripts import deployment_entry as entry
from tests._set_config_harness import (
    NUM_FLAGS,
    TWO_GPUS,
    make_models,
    read_deployment,
    run,
    write_fake_nvidia_smi,
)

pytestmark = pytest.mark.smoke
REPO_ROOT = Path(__file__).resolve().parents[1]

#: config 模組上每個角色的端點屬性(endpoint_policy 的 configured map 讀的就是這些)。
_URL_ATTRIBUTES = (
    ("main", "LLAMA_BASE_URL"), ("embedding", "LLAMA_EMBED_BASE_URL"),
    ("reranker", "LLAMA_RERANK_BASE_URL"), ("vl", "LLAMA_VL_BASE_URL"),
    ("auditor", "LLAMA_AUDITOR_BASE_URL"),
)


def _client_services(roles) -> dict:
    """A 匯出的 manifest 形狀;port 依 ROLES 順序 8080+i(auditor 是 8084)。"""
    ports = {role: 8080 + index for index, role in enumerate(deployment.ROLES)}
    return {role: {"base_url": f"http://10.20.30.40:{ports[role]}",
                   "model": f"ct-{role}-v1", "identity_alias": f"ct-{role}-v1"}
            for role in roles}


def _write_deployment(home: Path, document: dict) -> Path:
    directory = home / ".config" / "codetrail"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "deployment.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _legacy_local(home: Path) -> Path:
    """升級前的本機 deployment.json:四個角色都寫了模型,沒有 auditor。"""
    services = {role: {"model": f"/synthetic/{role}.gguf"} for role in deployment.LEGACY_ROLES}
    services["vl"]["mmproj"] = "/synthetic/mmproj.gguf"
    return _write_deployment(home, {"schema_version": 1, "mode": "local", "services": services})


def _legacy_client_home(tmp_path: Path) -> Path:
    """升級前的 B:四角色 client deployment.json + 四角色 owner-only client.json 授權。"""
    home = tmp_path / "home"
    services = _client_services(deployment.LEGACY_ROLES)
    _write_deployment(home, {"schema_version": 1, "mode": "client", "services": services})
    env = {"HOME": str(home)}
    client_config.save_client_settings(
        client_config.ClientSettings(
            path=client_config.config_path(env), present=True,
            model_endpoints={role: service["base_url"] for role, service in services.items()},
        ),
        env,
    )
    return home


def _authorize(monkeypatch, grants: dict[str, str], endpoints: dict[str, str]) -> None:
    """client.json 推進 config 之後的狀態(只改這條測試的 config 屬性)。"""
    monkeypatch.setattr(config, "DEPLOYMENT_MODE", "client")
    monkeypatch.setattr(config, "MODEL_ENDPOINTS", dict(grants))
    for role, attribute in _URL_ATTRIBUTES:
        monkeypatch.setattr(config, attribute, endpoints.get(role, ""))


def _snapshot(home: Path) -> dict:
    return {str(path.relative_to(home)): path.read_bytes()
            for path in home.rglob("*") if path.is_file()}


def _import_config(home: Path) -> list[str]:
    """另起一個行程 import config(import 期就讀 profile 的那一條真路徑)。"""
    code = (
        "import client_config, config\n"
        "settings = client_config.load_client_settings()\n"
        "print(config.DEPLOYMENT_MODE)\n"
        "print(repr(config.AUDITOR_MODEL))\n"
        "print(repr(config.LLAMA_AUDITOR_BASE_URL))\n"
        "print(','.join(sorted(settings.model_endpoints)))\n"
    )
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT), env=env,
                          capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()[-4:]


# ── 角色清單 ──


def test_auditor_is_appended_and_both_role_lists_agree():
    """auditor 加在尾端:既有 `enumerate(ROLES)` + 8080+i 的 fixture 前四個 port 不變。"""
    assert deployment.ROLES == (*deployment.LEGACY_ROLES, "auditor")
    assert endpoint_policy.MODEL_ROLES == deployment.ROLES
    assert endpoint_policy.LEGACY_MODEL_ROLES == deployment.LEGACY_ROLES


def test_the_session_fixture_points_the_auditor_at_a_dead_port():
    """conftest 的 HOME 把審核模型指向必定沒人聽的 port;內建預設 8084 可能真的有人在跑。"""
    profile = deployment.load_effective_profile()
    auditor = profile.service("auditor")
    assert (auditor.port, auditor.base_url) == (65531, "http://127.0.0.1:65531")
    assert auditor.model == "example-auditor-model"
    assert deployment.auditor_unconfigured_reason(profile) is None


def test_auditor_starts_and_stops_before_vl():
    """VL 用 --fit 依「已啟動服務的實際占用」決定層數:auditor 晚於 VL 就沒有 VRAM 可用。"""
    for scope in ("aux", "all"):
        roles = launch_servers._scope_roles(scope)
        assert roles.index("auditor") < roles.index("vl") == len(roles) - 1
        assert stop_servers._roles(scope) == roles
    assert launch_servers._scope_roles("main") == ("main",)
    assert launch_servers.WINDOWS["auditor"] == "audit"
    big = 40 * 1024**3
    assert launch_servers._health_timeout("auditor", None, big) == launch_servers._health_timeout("main", None, big)


# ── P1:舊本機設定 ──


@pytest.mark.parametrize("role", deployment.ROLES)
def test_only_main_and_auditor_models_may_be_null(tmp_path, role):
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps({"schema_version": 1, "services": {role: {"model": None}}}))
    if role in ("main", "auditor"):
        profile = deployment.load_effective_profile({"HOME": str(tmp_path)}, deployment_config=path)
        assert profile.service(role).model is None
    else:
        with pytest.raises(deployment.ProfileError):
            deployment.load_effective_profile({"HOME": str(tmp_path)}, deployment_config=path)


def test_legacy_local_deployment_loads_and_config_imports(tmp_path):
    _legacy_local(tmp_path)
    profile = deployment.load_effective_profile({"HOME": str(tmp_path)})
    auditor = profile.service("auditor")
    assert auditor.model is None and auditor.port == 8084 and auditor.gpu_role == "aux"
    assert deployment.service_unconfigured(auditor)
    assert deployment.auditor_unconfigured_reason(profile) == deployment.AUDITOR_LOCAL_MISSING
    assert "./set_config.sh" in deployment.AUDITOR_LOCAL_MISSING
    assert profile.service("main").model == "/synthetic/main.gguf"
    assert _import_config(tmp_path) == ["local", "None", "'http://localhost:8084'", ""]


def test_daily_setup_reaches_the_wizard_from_a_legacy_local_deployment(tmp_path, monkeypatch):
    """舊設定沒有審核模型 = 需要重跑精靈;精靈本身不得因此被擋在門外。"""
    home = tmp_path / "home"
    _legacy_local(home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local/state"))
    monkeypatch.setattr(set_config, "_COMMITTED", False)
    monkeypatch.setattr(set_config, "_detect_python", lambda *_a: sys.executable)
    monkeypatch.setattr(set_config, "_check_tmux", lambda *_a: None)
    monkeypatch.setattr(set_config, "check_llama_binary", lambda *_a: {"fit": True, "cache_ram": True})
    monkeypatch.setattr(set_config, "detect_gpus", lambda: [
        set_config.Gpu(0, "synthetic", 16384, 16384, "GPU-synthetic")])
    monkeypatch.setattr(set_config, "commit_files", lambda *_a, **_k:
                        pytest.fail("a cancelled setup attempted a transaction"))

    def scan(directory, *, notes):
        candidate = set_config.ModelCandidate(directory / "synthetic.gguf", 1 << 30, 1)
        return {role: [candidate, candidate] if role == "main" else [candidate]
                for role in ("main", "embedding", "reranker", "vl")}, []

    prompts = []

    def cancel(prompt):
        prompts.append(prompt)
        raise KeyboardInterrupt

    monkeypatch.setattr(set_config, "scan_models", scan)
    monkeypatch.setattr(set_config, "_input", cancel)
    before = _snapshot(home)
    assert entry.main(["configure"]) == 130
    assert len(prompts) == 1 and "請輸入編號" in prompts[0]
    assert _snapshot(home) == before


@pytest.mark.parametrize("scope", ["all", "aux"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_launcher_refuses_an_unconfigured_auditor_before_starting_anything(
    tmp_path, monkeypatch, capsys, scope, dry_run,
):
    """半套啟動(main 載入幾分鐘才發現審核模型沒設)只會留下要 rollback 的現場。"""
    _legacy_local(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("nothing may be checked or started before the auditor gate")

    for name in ("_check_port_collisions", "_print_dry_run", "_tmux_has_session",
                 "_start_role", "_wait_for_health"):
        monkeypatch.setattr(launch_servers, name, forbidden)
    monkeypatch.setattr(launch_servers.shutil, "which", forbidden)
    monkeypatch.setattr(launch_servers.process_env, "run", forbidden)
    assert launch_servers.main(["--scope", scope, *(["--dry-run"] if dry_run else [])]) == 1
    assert deployment.AUDITOR_LOCAL_MISSING in capsys.readouterr().err


def test_main_only_launch_is_not_blocked_by_the_auditor(tmp_path, monkeypatch, capsys):
    _legacy_local(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert launch_servers.main(["--scope", "main", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "main_command=" in out and "auditor_" not in out


def test_exec_refuses_an_unconfigured_auditor_without_exec(tmp_path, monkeypatch, capsys):
    """systemd 之類的 supervisor 只走 `deployment_profile.py exec`:同一句修法,不 exec。"""
    _legacy_local(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(deployment.os, "execvpe", lambda *_a: pytest.fail("unconfigured auditor reached exec"))
    assert deployment.main(["exec", "auditor"]) == 2
    assert deployment.AUDITOR_LOCAL_MISSING in capsys.readouterr().err


def test_stop_skips_the_unconfigured_auditor_and_still_succeeds(tmp_path, monkeypatch, capsys):
    """升級後 `~/start.sh stop` 不得因為還沒選審核模型而變紅。"""
    _legacy_local(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(stop_servers.shutil, "which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    monkeypatch.setattr(stop_servers.process_env, "run",
                        lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr=""))
    ports = []
    monkeypatch.setattr(stop_servers, "_listener_pids", lambda port: ports.append(port) or set())
    assert stop_servers.main(["--scope", "aux"]) == 0
    assert ports == [8081, 8082, 8083]
    assert "auditor（審核模型）尚未設定" in capsys.readouterr().out


def test_status_and_doctor_name_the_local_fix(tmp_path, monkeypatch, capsys):
    _legacy_local(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(check_status, "query_gpu_processes", lambda: ([], ""))
    monkeypatch.setattr(check_status, "query_gpu_inventory", lambda: {})
    assert check_status.main(["--strict", "--no-network", "--proc-root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert f"[FAIL] auditor: {deployment.AUDITOR_LOCAL_MISSING}" in captured.err
    assert "role=auditor" in captured.out and "health=not-configured" in captured.out

    result = doctor.Result()
    doctor.check_deployment_profile(result, no_network=True, server_status={})
    assert deployment.AUDITOR_LOCAL_MISSING in result.fails


# ── P2:舊 A/B 分離部署 ──


def test_client_mode_accepts_exactly_five_or_the_legacy_four_roles(tmp_path):
    def load(services):
        path = tmp_path / "deployment.json"
        path.write_text(json.dumps({"schema_version": 1, "mode": "client", "services": services}))
        return deployment.load_effective_profile({"HOME": str(tmp_path)}, deployment_config=path)

    five = _client_services(deployment.ROLES)
    current = load(five)
    assert deployment.auditor_unconfigured_reason(current) is None
    assert current.service("auditor").base_url == "http://10.20.30.40:8084"
    legacy = load(_client_services(deployment.LEGACY_ROLES))
    assert legacy.service("auditor").model is None and legacy.service("auditor").base_url == ""
    assert deployment.auditor_unconfigured_reason(legacy) == deployment.AUDITOR_CLIENT_MISSING
    with pytest.raises(deployment.ProfileError, match=r"missing service role\(s\): vl") as caught:
        load({role: service for role, service in five.items() if role != "vl"})
    assert deployment.AUDITOR_CLIENT_MISSING not in str(caught.value)
    with pytest.raises(deployment.ProfileError) as caught:
        load({role: five[role] for role in ("main", "embedding", "reranker")})
    assert "auditor, vl" in str(caught.value) and deployment.AUDITOR_CLIENT_MISSING in str(caught.value)
    with pytest.raises(deployment.ProfileError):
        load({**five, "auditor": {**five["auditor"], "model": None}})


@pytest.mark.parametrize("roles", [(), deployment.LEGACY_ROLES, deployment.ROLES])
def test_model_endpoint_grants_accept_empty_legacy_four_or_all_five(roles):
    grants = {role: service["base_url"] for role, service in _client_services(roles).items()}
    assert endpoint_policy.validate_model_endpoints(grants) == grants


@pytest.mark.parametrize("grants", [
    {"main": "http://10.20.30.40:8080"},
    {role: f"http://10.20.30.40:{8080 + i}" for i, role in enumerate(("main", "embedding", "reranker", "auditor"))},
    {**{role: f"http://10.20.30.40:{8080 + i}" for i, role in enumerate(deployment.ROLES)},
     "extra": "http://10.20.30.40:8090"},
    {role: "http://10.20.30.40:8080" for role in deployment.ROLES},
    ["http://10.20.30.40:8080"],
    None,
])
def test_model_endpoint_grants_reject_other_shapes(grants):
    with pytest.raises(endpoint_policy.EndpointPolicyError):
        endpoint_policy.validate_model_endpoints(grants)


def test_partial_grants_name_the_missing_roles_and_the_fix():
    with pytest.raises(endpoint_policy.EndpointPolicyError) as caught:
        endpoint_policy.validate_model_endpoints({"main": "http://10.20.30.40:8080"})
    message = str(caught.value)
    assert "missing: auditor, embedding, reranker, vl" in message
    assert "configure-advanced.sh" in message


def test_the_auditor_grant_authorizes_only_its_own_chat_and_count_paths(monkeypatch):
    grants = {role: service["base_url"] for role, service in _client_services(deployment.ROLES).items()}
    _authorize(monkeypatch, grants, grants)
    auditor = grants["auditor"]
    for path in ("/v1/chat/completions", "/v1/chat/completions/input_tokens", "/tokenize",
                 "/props", "/health", "/slots"):
        endpoint_policy.ensure_allowed(auditor + path, "auditor")
    # 審核引擎經 llama_client 送出時用的是泛用的 "model":一樣只認 auditor 自己的授權。
    endpoint_policy.ensure_allowed(auditor + "/v1/chat/completions", "model")
    for url, role in ((auditor + "/completion", "auditor"), (auditor + "/embedding", "auditor"),
                      (auditor + "/v1/chat/completions", "main"),
                      (grants["main"] + "/v1/chat/completions", "auditor"),
                      (grants["vl"] + "/v1/chat/completions/input_tokens", "model")):
        with pytest.raises(endpoint_policy.EndpointPolicyError):
            endpoint_policy.ensure_allowed(url, role)


def test_legacy_split_client_loads_but_never_authorizes_the_auditor(tmp_path, monkeypatch):
    home = _legacy_client_home(tmp_path)
    env = {"HOME": str(home)}
    profile = deployment.load_effective_profile(env)
    assert profile.mode == "client"
    assert deployment.auditor_unconfigured_reason(profile) == deployment.AUDITOR_CLIENT_MISSING
    assert "configure-advanced.sh" in deployment.AUDITOR_CLIENT_MISSING
    assert "set_config.sh" not in deployment.AUDITOR_CLIENT_MISSING
    settings = client_config.load_client_settings(env)
    assert set(settings.model_endpoints) == set(deployment.LEGACY_ROLES)
    assert _import_config(home) == ["client", "None", "''", "embedding,main,reranker,vl"]

    _authorize(monkeypatch, settings.model_endpoints,
               {role: service.base_url for role, service in profile.services.items()})
    endpoint_policy.ensure_allowed(profile.service("main").base_url + "/v1/chat/completions", "main")
    for url in ("", "http://10.20.30.40:8084", "http://10.20.30.40:8084/v1/chat/completions"):
        for role in ("auditor", "model"):
            with pytest.raises(endpoint_policy.EndpointPolicyError):
                endpoint_policy.ensure_allowed(url, role)
    # aicode / doctor 的 live 身分核對走這裡:給 A/B 修法,不去探測空端點。
    with pytest.raises(model_identity.ModelIdentityError) as caught:
        model_identity.capture_model_identity("auditor", profile=profile)
    assert deployment.AUDITOR_CLIENT_MISSING in str(caught.value)


def test_status_doctor_and_device_name_the_split_fix(tmp_path, monkeypatch, capsys):
    home = _legacy_client_home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    settings = client_config.load_client_settings({"HOME": str(home)})
    profile = deployment.load_effective_profile({"HOME": str(home)})
    _authorize(monkeypatch, settings.model_endpoints,
               {role: service.base_url for role, service in profile.services.items()})
    monkeypatch.setattr(client_config, "apply_to_config", lambda *_a, **_k: None)
    assert check_status.main(["--strict", "--no-network"]) == 1
    assert f"[FAIL] auditor: {deployment.AUDITOR_CLIENT_MISSING}" in capsys.readouterr().err

    result = doctor.Result()
    doctor.check_deployment_profile(result, no_network=True, server_status={})
    assert deployment.AUDITOR_CLIENT_MISSING in result.fails

    # codetrail-device.sh 不得把缺審核模型的舊 B 當成「既有設定可用」直接進 aicode。
    capsys.readouterr()
    assert entry._existing_for_role(home, "client") is None
    assert deployment.AUDITOR_CLIENT_MISSING in capsys.readouterr().out


def test_split_client_setup_requires_a_five_role_manifest(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local/state"))

    def configure(services):
        manifest = tmp_path / "endpoints.json"
        manifest.write_text(json.dumps({"schema_version": 1, "mode": "client", "services": services}))
        args = set_config._parser().parse_args(
            ["--mode", "client", "--yes", "--endpoint-manifest", str(manifest)])
        return set_config.configure_client(args, home)

    with pytest.raises(set_config.SetupError, match="審核模型"):
        configure(_client_services(deployment.LEGACY_ROLES))
    assert not (home / ".config").exists()

    five = _client_services(deployment.ROLES)
    assert configure(five) == 0
    settings = client_config.load_client_settings({"HOME": str(home)})
    assert settings.model_endpoints == {role: service["base_url"] for role, service in five.items()}
    profile = deployment.load_effective_profile({"HOME": str(home)})
    assert deployment.auditor_unconfigured_reason(profile) is None
    assert profile.service("auditor").identity_alias == "ct-auditor-v1"


def test_model_host_without_auditor_cannot_export_or_skip_setup(tmp_path, capsys):
    """A 還沒選審核模型:不得匯出缺角的 manifest,修法指向附加入口(日常精靈會把 A 改成本機)。"""
    services = {role: {"model": f"/synthetic/{role}.gguf", "identity_alias": f"ct-{role}-v1",
                       "bind": "all-interfaces"} for role in deployment.LEGACY_ROLES}
    _write_deployment(tmp_path, {"schema_version": 1, "mode": "model-host", "services": services})
    profile = entry._load_profile(tmp_path)
    assert deployment.auditor_unconfigured_reason(profile) == deployment.AUDITOR_HOST_MISSING
    assert "configure-advanced.sh" in deployment.AUDITOR_HOST_MISSING
    assert "set_config.sh" not in deployment.AUDITOR_HOST_MISSING
    with pytest.raises(deployment.ProfileError, match="identity_alias"):
        deployment.export_client_profile(profile, "http://10.20.30.40")
    with pytest.raises(set_config.SetupError) as caught:
        entry._print_manifest(profile, "http://10.20.30.40")
    assert str(caught.value) == deployment.AUDITOR_HOST_MISSING
    capsys.readouterr()
    assert entry._existing_for_role(tmp_path, "model-host") is None
    assert deployment.AUDITOR_HOST_MISSING in capsys.readouterr().out


# ── set_config:--yes 不得替使用者選審核模型 ──


def test_yes_requires_explicit_auditor_answers_and_writes_them(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    base = ("--yes", "--main-model", "1", "--rerank-model", "1", "--main-gpu", "1",
            "--embed-gpu", "2", "--rerank-gpu", "2", "--vl-gpu", "2", "--models-dir", str(models))
    numbers = ("--ctx", "65536", "--rerank-ctx", "8192")
    for flags, missing in (
        (("--auditor-gpu", "2", "--auditor-ctx", "32768"), "--auditor-model"),
        (("--auditor-model", "1", "--auditor-ctx", "32768"), "--auditor-gpu"),
        (("--auditor-model", "1", "--auditor-gpu", "2"), "--auditor-ctx"),
        (("--auditor-model", "1", "--auditor-gpu", "2", "--auditor-ctx", "4096"), "--auditor-ctx"),
    ):
        proc = run(tmp_path, *base, *numbers, *flags)
        assert proc.returncode == 2, proc.stdout
        assert missing in proc.stderr
    assert not (tmp_path / "home" / ".config" / "codetrail" / "deployment.json").exists()

    done = run(tmp_path, *base, "--auditor-model", "1", "--auditor-gpu", "2", *NUM_FLAGS, "--no-preview")
    assert done.returncode == 0, done.stderr + done.stdout
    auditor = read_deployment(tmp_path)["services"]["auditor"]
    # 聊天模型候選由小到大:1 = VL 主檔(同一個 GGUF,不載 mmproj)。
    assert Path(auditor["model"]).name == "vl-model-q6.gguf" and "mmproj" not in auditor
    assert (auditor["ctx"], auditor["gpu"]) == (32768, "GPU-bbbb-2000")
    assert auditor["parameters"]["parallel"] == 1 and auditor["parameters"]["gpu_layers"] == 99
