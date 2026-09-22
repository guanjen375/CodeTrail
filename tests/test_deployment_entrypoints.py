"""Public deployment routing and explicit remote authorization contracts."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import client_config
import deployment_profile
import deployment_status
from scripts import deployment_entry as entry
from scripts import launch_servers, set_config

pytestmark = pytest.mark.smoke
ROOT = Path(__file__).resolve().parents[1]


def _services():
    return {role: {"base_url": f"http://10.20.30.40:{8080 + index}",
                   "model": f"ct-{role}-v1", "identity_alias": f"ct-{role}-v1"}
            for index, role in enumerate(deployment_profile.ROLES)}


def _device_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local/state"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    manifest = tmp_path / "endpoints.json"
    manifest.write_text(json.dumps({"schema_version": 1, "mode": "client", "services": _services()}))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("B must not inspect or launch local model tooling")

    for name in ("detect_gpus", "scan_models", "_check_tmux", "check_llama_binary", "_detect_python",
                 "running_codetrail_sessions", "preview_start_commands"):
        monkeypatch.setattr(set_config, name, forbidden)
    return home, project, manifest


def _answers(monkeypatch, values, observe=None):
    pending = iter(values)

    def answer(prompt=""):
        if observe:
            observe(prompt)
        try:
            return next(pending)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", answer)


@pytest.mark.parametrize("wrapper", ["aicode", "set_config.sh", "scripts/configure-advanced.sh",
                                     "scripts/codetrail-host.sh", "scripts/codetrail-device.sh"])
def test_public_entrypoints_reject_argv_before_python_or_writes(tmp_path, wrapper):
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "python-was-started"
    python = bindir / "python3"
    python.write_text(f"#!/usr/bin/env bash\necho bad > {shlex.quote(str(marker))}\n")
    python.chmod(0o700)
    env = dict(os.environ, HOME=str(home), PATH=f"{bindir}:{os.environ.get('PATH', '')}")
    for argv in (["--help"], ["-c"], ["--session", "saved"], ["--yes"], ["stop"], [""]):
        result = subprocess.run(["bash", str(ROOT / wrapper), *argv], env=env, cwd=tmp_path,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 2, (wrapper, argv, result.stderr)
        assert "不接受參數" in result.stderr
    assert not marker.exists()
    assert list(home.iterdir()) == []


@pytest.mark.parametrize("wrapper,role", [("set_config.sh", "configure"),
                                          ("scripts/configure-advanced.sh", "advanced"),
                                          ("scripts/codetrail-host.sh", "host"),
                                          ("scripts/codetrail-device.sh", "device")])
def test_deployment_wrappers_follow_symlinks_and_keep_cwd(tmp_path, wrapper, role):
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    target = checkout / wrapper
    shutil.copy2(ROOT / wrapper, target)
    record = tmp_path / "record.json"
    (checkout / "scripts/deployment_entry.py").write_text(
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(record)!r}).write_text(json.dumps([str(pathlib.Path.cwd()), sys.argv[1:]]))\n"
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    link = bindir / "entry"
    link.symlink_to(Path("..") / "checkout" / wrapper)
    project = tmp_path / "project"
    project.mkdir()
    env = dict(os.environ, AICODE_ROOT="/bogus/project", CODETRAIL_ENTRY="/bogus/entry")
    result = subprocess.run(["bash", str(link)], cwd=project, env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(record.read_text()) == [str(project), [role]]


def test_start_wrapper_only_dispatches_empty_or_exact_stop(tmp_path):
    checkout = tmp_path / "checkout with spaces"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    record = tmp_path / "dispatch.jsonl"
    for script in ("launch_servers.py", "stop_servers.py"):
        (scripts / script).write_text(
            "import json, pathlib, sys\n"
            f"with pathlib.Path({str(record)!r}).open('a') as stream:\n"
            "    stream.write(json.dumps([pathlib.Path(__file__).name, sys.argv[1:]]) + '\\n')\n"
        )
    wrapper = tmp_path / "start.sh"
    source = set_config.render_start_wrapper(checkout)
    wrapper.write_text(source)
    assert not any(line.strip().startswith(("export ", "unset ")) for line in source.splitlines())
    assert '"$@"' not in source
    for argv in ([], ["stop"]):
        result = subprocess.run(["bash", str(wrapper), *argv], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert f"[start.sh] checkout: {checkout}" in result.stdout
        assert "[start.sh] generated: " in result.stdout
        assert "[start.sh] version: unknown" in result.stdout
    assert [json.loads(line) for line in record.read_text().splitlines()] == [
        ["launch_servers.py", ["--scope", "all"]], ["stop_servers.py", ["--scope", "all"]],
    ]
    before = record.read_bytes()
    for argv in (["stop", "--force"], ["quit"], ["--help"], ["--dry-run"], ["status"], ["logs"], [""]):
        result = subprocess.run(["bash", str(wrapper), *argv], capture_output=True, text=True, timeout=10)
        assert result.returncode == 2, (argv, result.stderr)
        assert "[start.sh] checkout:" not in result.stdout
    assert record.read_bytes() == before


def test_start_wrapper_pins_source_identity_and_quotes_checkout_paths(tmp_path):
    checkout = tmp_path / "checkout ' $(touch injected)"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    metadata = checkout / ".git"
    (metadata / "refs/heads").mkdir(parents=True)
    commit = "abcde01234" * 4
    (metadata / "HEAD").write_text("ref: refs/heads/main\n")
    (metadata / "refs/heads/main").write_text(commit + "\n")
    for name in ("launch_servers.py", "stop_servers.py"):
        (scripts / name).write_text("print('fixed core')\n")
    source = set_config.render_start_wrapper(checkout)
    (metadata / "refs/heads/main").write_text("f" * 40 + "\n")
    wrapper = tmp_path / "start.sh"
    wrapper.write_text(source)
    for args in ([], ["stop"]):
        result = subprocess.run(["bash", str(wrapper), *args], cwd=tmp_path,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert f"checkout: {checkout}" in result.stdout
        assert f"version: {commit}" in result.stdout
        assert result.stdout.index("version:") < result.stdout.index("fixed core")
    assert not (tmp_path / "injected").exists()


def test_daily_local_route_warns_conversion_and_keeps_cancelled_settings(tmp_path, monkeypatch, capsys):
    home, _project, manifest = _device_home(tmp_path, monkeypatch)
    args = set_config._parser().parse_args(["--mode", "client", "--yes", "--endpoint-manifest", str(manifest)])
    assert set_config.configure_client(args, home) == 0
    before = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    routed = []
    monkeypatch.setattr(entry, "_configure_role", lambda role, **kwargs:
                        routed.append((role, kwargs)) or entry.SetupResult(0, False))
    monkeypatch.setattr(entry, "configure_menu", lambda *_a: pytest.fail("daily setup asked for a role"))
    assert entry.main(["configure"]) == 0
    assert routed == [("local", {"offer_restart": True, "prompt_compaction": False})]
    assert {path: path.read_bytes() for path in home.rglob("*") if path.is_file()} == before
    output = capsys.readouterr().out
    assert "目前角色是 client" in output
    assert "移除先前 B 的端點授權" in output and "取消不寫入" in output
    assert "configure-advanced.sh" in output


@pytest.mark.parametrize("kb_answer,kb_allowed", [("", False), ("n", False), ("y", True)])
def test_device_displays_four_destinations_and_separate_kb_consent(tmp_path, monkeypatch, capsys, kb_answer, kb_allowed):
    home, project, manifest = _device_home(tmp_path, monkeypatch)
    displayed = []

    def observe(prompt):
        if "KB" in prompt:
            output = capsys.readouterr().out
            for service in _services().values():
                assert service["base_url"] in output
                assert service["identity_alias"] in output
            displayed.append(True)

    _answers(monkeypatch, [str(manifest), kb_answer, "y"], observe)
    calls = []
    monkeypatch.setattr(entry.process_env, "run", lambda argv, **kw: calls.append((argv, kw)) or SimpleNamespace(returncode=0))
    assert entry.device(home) == 0
    assert displayed == [True]
    assert calls == [(["bash", str(ROOT / "aicode")], {"cwd": project, "check": False})]
    settings = client_config.load_client_settings({"HOME": str(home)})
    assert settings.kb_context_remote_ok is kb_allowed
    assert settings.model_remote_ok is False
    assert settings.model_endpoints == {role: service["base_url"] for role, service in _services().items()}
    assert settings.path.stat().st_mode & 0o777 == 0o600
    assert settings.path.parent.stat().st_mode & 0o777 == 0o700
    assert not (home / "start.sh").exists()
    assert not (home / ".config/codetrail/models.json").exists()
    transaction = json.loads(set_config._manifest_path(home).read_text())
    assert set(transaction["targets"]) == {str(home / ".config/codetrail/deployment.json"), str(settings.path)}
    assert len(set_config.main_restore_targets(home)) == 4
    assert "尚未驗證 live" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["", "n", "true", None])
def test_device_without_affirmative_endpoint_consent_never_writes_or_starts(tmp_path, monkeypatch, answer):
    home, _project, manifest = _device_home(tmp_path, monkeypatch)
    values = [str(manifest), "y"] + ([] if answer is None else [answer])
    _answers(monkeypatch, values)
    monkeypatch.setattr(entry.process_env, "run", lambda *_a, **_k: pytest.fail("denied setup started aicode"))
    assert entry.device(home) == 0
    assert not (home / ".config").exists()
    assert not (home / "start.sh").exists()


def test_client_setup_invalid_manifest_and_failed_transaction_leave_no_partial_configuration(tmp_path, monkeypatch):
    home, _project, manifest = _device_home(tmp_path, monkeypatch)
    value = json.loads(manifest.read_text())
    value["services"]["main"]["base_url"] = "http://remote.example:8080"
    manifest.write_text(json.dumps(value))
    _answers(monkeypatch, [str(manifest)])
    monkeypatch.setattr(entry.process_env, "run", lambda *_a, **_k: pytest.fail("invalid setup started aicode"))
    assert entry.main(["device"]) == 2
    assert not (home / ".config").exists()

    value["services"] = _services()
    manifest.write_text(json.dumps(value))
    original_replace = os.replace

    def fail_private_replace(source, target, **kwargs):
        if str(target) == "client.json":
            raise OSError("simulated storage failure")
        return original_replace(source, target, **kwargs)

    monkeypatch.setattr(set_config.os, "replace", fail_private_replace)
    _answers(monkeypatch, [str(manifest), "n", "y"])
    assert entry.main(["device"]) == 2
    assert not (home / ".config/codetrail/deployment.json").exists()
    assert not (home / ".config/codetrail/client.json").exists()
    assert not (home / ".config/codetrail/models.json").exists()
    assert not (home / "start.sh").exists()
    assert set_config._COMMITTED is False


def test_existing_device_uses_aicode_without_setup_or_local_model_probes(tmp_path, monkeypatch):
    home, project, manifest = _device_home(tmp_path, monkeypatch)
    args = set_config._parser().parse_args(["--mode", "client", "--yes", "--endpoint-manifest", str(manifest)])
    assert set_config.configure_client(args, home) == 0
    monkeypatch.setattr(entry, "_configure_role", lambda *_a, **_k: pytest.fail("valid B was reconfigured"))
    calls = []
    monkeypatch.setattr(entry.process_env, "run", lambda argv, **kw: calls.append((argv, kw)) or SimpleNamespace(returncode=7))
    assert entry.device(home) == 7
    assert calls == [(["bash", str(ROOT / "aicode")], {"cwd": project, "check": False})]


def _host_profile(home):
    directory = home / ".config/codetrail"
    directory.mkdir(parents=True, exist_ok=True)
    services = {role: {"model": f"/synthetic/{role}.gguf", "identity_alias": f"ct-{role}-v1",
                       "bind": "all-interfaces"} for role in deployment_profile.ROLES}
    services["main"]["ctx"] = 65536
    (directory / "deployment.json").write_text(json.dumps({"schema_version": 1, "mode": "model-host", "services": services}))
    return entry._load_profile(home)


def _ready(service):
    return {"status": "ok"}, {"model_alias": service.identity_alias, "n_ctx": service.ctx}


def test_existing_host_checks_live_readiness_without_reconfiguration_or_restart(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _host_profile(home)
    monkeypatch.setattr(entry, "_configure_role", lambda *_a, **_k: pytest.fail("valid A was reconfigured"))
    monkeypatch.setattr(launch_servers, "main", lambda *_a: pytest.fail("ready A was restarted"))
    monkeypatch.setattr(deployment_status, "query_server", _ready)
    assert entry.host(home) == 0


@pytest.mark.parametrize("failure", ["alias", "context", "unavailable"])
def test_unready_existing_host_is_fail_loud_and_never_restarted(tmp_path, monkeypatch, failure):
    home = tmp_path / "home"
    _host_profile(home)

    def query(service):
        if failure == "unavailable":
            return None, None
        health, props = _ready(service)
        if failure == "alias":
            props["model_alias"] = "wrong-version"
        elif service.role == "main":
            props.pop("n_ctx")
        return health, props

    monkeypatch.setattr(deployment_status, "query_server", query)
    monkeypatch.setattr(set_config, "running_codetrail_sessions", lambda: ["codetrail-main"])
    monkeypatch.setattr(launch_servers, "main", lambda *_a: pytest.fail("unready A was restarted"))
    with pytest.raises(set_config.SetupError, match="ready"):
        entry.host(home)


def test_absent_host_services_launch_once_then_require_live_readiness(tmp_path, monkeypatch):
    home = tmp_path / "home"
    profile = _host_profile(home)
    calls = []
    monkeypatch.setattr(deployment_status, "query_server", lambda service: _ready(service) if calls else (None, None))
    monkeypatch.setattr(set_config, "running_codetrail_sessions", lambda: [])
    monkeypatch.setattr(launch_servers, "main", lambda argv: calls.append(argv) or 0)
    assert entry._ensure_host_ready(profile) == 0
    assert calls == [["--scope", "all"]]


@pytest.mark.parametrize("consent", ["", "n", "y"])
def test_host_lan_requires_explicit_consent_and_exports_without_extra_transaction_target(tmp_path, monkeypatch, capsys, consent):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    seen = []

    def configure(args):
        assert args.deployment_mode == "model-host"
        assert args.offer_restart is False
        assert args.models_dir == "/custom/models"
        assert args.llama_bin == "/custom/llama-server"
        seen.append(args.allow_remote)
        _host_profile(home)
        set_config._COMMITTED = True
        return 0

    monkeypatch.setattr(set_config, "run", configure)
    monkeypatch.setattr(entry, "_ensure_host_ready", lambda _p: 0)
    _answers(monkeypatch, ["/custom/models", "/custom/llama-server", consent] + (["10.20.30.40"] if consent == "y" else []))
    assert entry.host(home) == 0
    assert seen == [consent == "y"]
    output = capsys.readouterr().out
    assert "0.0.0.0" in output and "沒有認證" in output
    if consent == "y":
        for service in _services().values():
            assert service["base_url"] in output
            assert service["identity_alias"] in output
    else:
        assert "=== Endpoint manifest" not in output
    assert not list(home.rglob("*manifest*"))
    assert len(set_config.main_restore_targets(home)) == 4


def test_role_menu_routes_client_and_restore_without_gpu_setup(tmp_path, monkeypatch):
    home, _project, _manifest = _device_home(tmp_path, monkeypatch)
    routed = []
    monkeypatch.setattr(entry, "_configure_role", lambda role, **kw: routed.append((role, kw)) or entry.SetupResult(0, False))
    _answers(monkeypatch, ["4"])
    assert entry.configure_menu(home) == 0
    assert routed == [("client", {"offer_restart": True})]
    restored = []
    monkeypatch.setattr(set_config, "restore_last_backup", lambda path: restored.append(path) or 0)
    _answers(monkeypatch, ["5", "y"])
    assert entry.configure_menu(home) == 0
    assert restored == [home]


@pytest.mark.parametrize("address", ["fd00::40", "127.0.0.1", "8.8.8.8", "host.example", "http://10.20.30.40"])
def test_host_rejects_unusable_transfer_address_before_setup(tmp_path, monkeypatch, address):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _answers(monkeypatch, ["", "", "y", address])
    monkeypatch.setattr(set_config, "run", lambda *_a: pytest.fail("bad host address reached setup"))
    with pytest.raises(set_config.SetupError, match="IPv4"):
        entry.host(home)
    assert list(home.iterdir()) == []
