"""DSpark launch safety: explicit configuration cannot become silent fallback.

All probes are mocked except the isolated CLI check, which runs only a tiny
temporary fake binary. No model server, GPU, tmux session or download is used.
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import deployment_profile as dp
import deployment_status
import dspark_runtime
import model_identity
import process_env
from scripts import launch_servers

pytestmark = pytest.mark.smoke

HELP = """--spec-type none,draft-simple,draft-dspark
--spec-draft-model, -md, --model-draft FNAME
--spec-draft-n-max N
"""


def _write_json(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _fixture(tmp_path):
    services = {}
    for role in dp.ROLES:
        artifact = tmp_path / f"{role}.gguf"
        artifact.write_bytes(b"fixture")
        services[role] = {"model": str(artifact)}
    projector = tmp_path / "mmproj.gguf"
    projector.write_bytes(b"fixture")
    services["vl"]["mmproj"] = str(projector)
    draft = tmp_path / "draft.gguf"
    draft.write_bytes(b"draft fixture")
    services["main"]["dspark"] = {"draft_model": str(draft), "draft_n_max": 3}
    binary = tmp_path / "llama-server"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    registry = _write_json(tmp_path / "models.json", {})
    config = _write_json(tmp_path / "deployment.json", {
        "schema_version": 1, "profile": "defaults", "llama_bin": str(binary),
        "services": services,
    })
    profile = dp.load_effective_profile(
        {"HOME": str(tmp_path)}, deployment_config=config, model_registry_file=registry,
    )
    return profile, config, draft


def _no_probe(*_args, **_kwargs):
    pytest.fail("this path must not probe a binary, GPU, endpoint or tmux")


@pytest.mark.parametrize("bad", [
    False, {}, {"draft_model": "/model.gguf"},
    {"draft_model": "/model.gguf", "draft_n_max": True},
    {"draft_model": "/model.gguf", "draft_n_max": 0},
    {"draft_model": "/model.gguf", "draft_n_max": 65},
    {"draft_model": "/model.gguf", "draft_n_max": "3"},
    {"draft_model": "/model.gguf", "draft_n_max": None},
    {"draft_model": None, "draft_n_max": 3},
    {"draft_model": "./draft.gguf", "draft_n_max": 3},
    {"draft_model": "draft; echo injected", "draft_n_max": 3},
    {"draft_model": "/model.gguf", "draft_n_max": 3, "gpu": "0"},
])
def test_dspark_rejects_incomplete_or_unsafe_configuration(tmp_path, bad):
    """Do not coerce booleans, inherit half a pair, or accept raw shell options."""
    _, config, _ = _fixture(tmp_path)
    payload = json.loads(config.read_text())
    payload["services"]["main"]["dspark"] = bad
    _write_json(config, payload)
    with pytest.raises(dp.ProfileError):
        dp.load_effective_profile({"HOME": str(tmp_path)}, deployment_config=config)


def test_dspark_is_main_only_and_absent_from_client_manifest(tmp_path):
    """A client cannot inherit local draft paths or accidentally start a draft."""
    profile, config, _ = _fixture(tmp_path)
    payload = json.loads(config.read_text())
    payload["services"]["embedding"]["dspark"] = None
    _write_json(config, payload)
    with pytest.raises(dp.ProfileError, match="only allowed for main"):
        dp.load_effective_profile({"HOME": str(tmp_path)}, deployment_config=config)

    host = replace(profile, mode="model-host", services={
        role: replace(service, identity_alias=f"ct-{role}-v1", deployment_mode="model-host")
        for role, service in profile.services.items()
    })
    manifest = dp.export_client_profile(host, "http://192.168.1.20")
    assert all("dspark" not in raw for raw in manifest["services"].values())
    _write_json(config, manifest)
    client = dp.load_effective_profile({"HOME": str(tmp_path)}, deployment_config=config)
    assert client.service("main").dspark is None
    assert "dspark" not in dp.profile_as_dict(client)["services"]["main"]
    manifest["services"]["main"]["dspark"] = None
    _write_json(config, manifest)
    with pytest.raises(dp.ProfileError, match="client mode has no local dspark"):
        dp.load_effective_profile({"HOME": str(tmp_path)}, deployment_config=config)


def test_dspark_merge_and_model_override_cannot_reuse_stale_pairing(tmp_path):
    """Only an explicit complete replacement can carry a draft to a new target."""
    profile, config, _ = _fixture(tmp_path)
    existing = profile.service("main")
    same = dp.load_effective_profile(
        {"HOME": str(tmp_path)}, deployment_config=config,
        overrides=dp.LauncherOverrides(main_model=existing.model),
    )
    assert same.service("main").dspark == existing.dspark
    changed = dp.load_effective_profile(
        {"HOME": str(tmp_path)}, deployment_config=config,
        overrides=dp.LauncherOverrides(main_model="/other/main.gguf"),
    )
    assert changed.service("main").dspark is None
    base = {"model": "/old.gguf", "dspark": {"draft_model": "/draft.gguf", "draft_n_max": 3}}
    partial = {"draft_n_max": 4}
    merged = dp._merge(base, {"dspark": partial})
    assert merged["dspark"] == partial
    with pytest.raises(dp.ProfileError, match="requires draft_model and draft_n_max"):
        dp._validate_document({"schema_version": 1, "services": {"main": merged}}, "fixture", local=True)
    replacement = {"draft_model": "/new-draft.gguf", "draft_n_max": 4}
    assert dp._merge(base, {"model": "/new.gguf", "dspark": replacement})["dspark"] == replacement


def test_dspark_argv_is_explicit_and_off_never_probes(tmp_path, monkeypatch):
    """Off preserves old binaries and cannot consult a broken former draft."""
    profile, _, draft = _fixture(tmp_path)
    monkeypatch.setattr(process_env, "run", _no_probe)
    main = profile.service("main")
    command = dp.build_server_command(main, profile.llama_bin)
    start = command.index("--spec-type")
    assert command[start:start + 6] == [
        "--spec-type", "draft-dspark", "--spec-draft-model", str(draft),
        "--spec-draft-n-max", "3",
    ]
    assert command.count("--spec-type") == 1
    off = replace(main, dspark=None)
    assert not any(arg.startswith("--spec-") for arg in dp.build_server_command(off, "/old-binary"))
    for role in ("embedding", "reranker", "vl"):
        assert not any(arg.startswith("--spec-") for arg in dp.build_server_command(profile.service(role), "/old-binary"))
    monkeypatch.setattr(dspark_runtime, "resolve_dspark_draft", _no_probe)
    dspark_runtime.validate_dspark_runtime(off, "/missing/llama-server")


def test_dspark_dry_run_stays_offline_with_missing_dependencies(tmp_path, monkeypatch, capsys):
    """A proposed command preview must never execute help or readiness probes."""
    profile, _, draft = _fixture(tmp_path)
    draft.unlink()
    Path(profile.llama_bin).unlink()
    monkeypatch.setattr(process_env, "run", _no_probe)
    monkeypatch.setattr(launch_servers, "require_dspark_active", _no_probe)
    args = launch_servers._parser().parse_args(["--scope", "main", "--dry-run"])
    launch_servers.launch(profile, ["main"], args)
    output = capsys.readouterr().out
    assert "--spec-type draft-dspark" in output
    assert f"--spec-draft-model {draft}" in output
    assert "--spec-draft-n-max 3" in output


def test_dspark_requires_the_complete_first_shard_before_binary_probe(tmp_path, monkeypatch):
    """An existing first shard is not enough to claim the draft is available."""
    profile, _, _ = _fixture(tmp_path)
    first = tmp_path / "draft-00001-of-00002.gguf"
    second = tmp_path / "draft-00002-of-00002.gguf"
    first.write_bytes(b"shard")
    main = replace(profile.service("main"), dspark=dp.DSparkConfig(str(first)))
    monkeypatch.setattr(process_env, "run", _no_probe)
    with pytest.raises(dp.ProfileError, match="draft shard does not exist"):
        dspark_runtime.validate_dspark_runtime(main, profile.llama_bin)
    second.write_bytes(b"shard")
    assert dspark_runtime.resolve_dspark_draft(main, must_exist=True) == str(first)
    with pytest.raises(dp.ProfileError, match="first GGUF shard"):
        dspark_runtime.resolve_dspark_draft(
            replace(main, dspark=dp.DSparkConfig(str(second))), must_exist=True,
        )


def test_require_files_includes_dspark_without_probing_binary(tmp_path, monkeypatch, capsys):
    profile, config, draft = _fixture(tmp_path)
    draft.unlink()
    Path(profile.llama_bin).unlink()
    monkeypatch.setattr(process_env, "run", _no_probe)
    assert dp.main(["validate", "--deployment-config", str(config), "--require-files"]) == 2
    assert str(draft) in capsys.readouterr().err


def test_dspark_help_probe_uses_sanitized_server_environment(tmp_path, monkeypatch):
    """Check the actual spawn handover, not only a mocked helper argument."""
    profile, _, _ = _fixture(tmp_path)
    hostile = {
        "LLAMA_ARG_SPEC_TYPE": "none", "LLAMA_ARG_MODEL": "/wrong.gguf",
        "CUDA_VISIBLE_DEVICES": "99", "AICODE_MODEL": "old",
        "AI_CODE_MODEL": "old", "CODETRAIL_MODEL": "old",
    }
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/expected/libs")
    seen = []

    def spawn(argv, *, env, **kwargs):
        seen.append((argv, env, kwargs))
        # Some builds put help on stderr and use a nonzero help exit code.
        return process_env.CompletedProcess(argv, 1, stdout="", stderr=HELP)

    monkeypatch.setattr(process_env._subprocess, "run", spawn)
    dspark_runtime.validate_dspark_runtime(profile.service("main"), profile.llama_bin)
    argv, environment, kwargs = seen.pop()
    assert argv == [profile.llama_bin, "--help"]
    assert not set(hostile) & set(environment)
    assert environment["LD_LIBRARY_PATH"] == "/expected/libs"
    assert 0 < kwargs["timeout"] <= 10
    assert kwargs["check"] is False


@pytest.mark.parametrize("token", ["--spec-type", "--spec-draft-model", "--spec-draft-n-max", "draft-dspark"])
def test_dspark_help_probe_rejects_substring_capabilities(tmp_path, monkeypatch, token):
    profile, _, _ = _fixture(tmp_path)
    monkeypatch.setattr(process_env, "run", lambda argv, **kwargs: process_env.CompletedProcess(
        argv, 0, stdout=HELP.replace(token, token + "-other"), stderr="",
    ))
    with pytest.raises(dp.ProfileError, match="not advertised"):
        dspark_runtime.validate_dspark_runtime(profile.service("main"), profile.llama_bin)


def test_dspark_server_environment_rejects_override_escape_hatches(monkeypatch):
    monkeypatch.setattr(process_env._subprocess, "run", _no_probe)
    for kwargs in ({"overrides": {}}, {"overrides": {"LLAMA_ARG_MODEL": "wrong"}}, {"env": {}}):
        with pytest.raises(TypeError):
            process_env.run(["never-spawn"], server_env=True, **kwargs)
    with pytest.raises(TypeError, match="must be boolean"):
        process_env.run(["never-spawn"], server_env="false")


def _launch_scaffold(profile, monkeypatch, tmp_path):
    monkeypatch.setattr(launch_servers.shutil, "which", lambda _name: "/fake/tmux")
    monkeypatch.setattr(launch_servers, "_tmux_has_session", lambda _name: False)
    monkeypatch.setattr(launch_servers, "_port_responds", lambda _service: False)
    monkeypatch.setattr(launch_servers, "_state_log_dir", lambda: tmp_path / "logs")
    return launch_servers._parser().parse_args(["--scope", "main"])


@pytest.mark.parametrize("failure", [FileNotFoundError("missing dependency"), process_env.TimeoutExpired("help", 10)])
def test_dspark_runtime_failure_precedes_session_creation(tmp_path, monkeypatch, failure):
    """Dependency/probe failures cannot leave a partially started deployment."""
    profile, _, _ = _fixture(tmp_path)
    args = _launch_scaffold(profile, monkeypatch, tmp_path)
    monkeypatch.setattr(launch_servers, "_start_role", _no_probe)

    def failed_probe(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(process_env, "run", failed_probe)
    with pytest.raises(dp.ProfileError, match="cannot verify DSpark support"):
        launch_servers.launch(profile, ["main"], args)


def test_dspark_inactive_slots_after_health_roll_back_before_ready(tmp_path, monkeypatch, capsys):
    """An upstream silent disable after healthy load is still a failed launch."""
    profile, _, _ = _fixture(tmp_path)
    args = _launch_scaffold(profile, monkeypatch, tmp_path)
    monkeypatch.setattr(process_env, "run", lambda argv, **kwargs: process_env.CompletedProcess(
        argv, 0, stdout=HELP, stderr="",
    ))
    events = []
    monkeypatch.setattr(launch_servers, "_start_role", lambda *_a, **_k: events.append("start"))
    monkeypatch.setattr(launch_servers, "_wait_for_health", lambda *_a, **_k: events.append("health"))

    def slots(_service):
        events.append("slots")
        return [{"speculative": False}]

    def rollback(reason, roles, sessions, mapping, **kwargs):
        events.append("rollback")
        assert [service.role for service in roles] == ["main"]
        assert sessions == [dp.TMUX_SESSIONS["main"]]

    monkeypatch.setattr(deployment_status, "query_slots", slots)
    monkeypatch.setattr(launch_servers, "_rollback_started", rollback)
    with pytest.raises(dp.ProfileError):
        launch_servers.launch(profile, ["main"], args)
    assert events == ["start", "health", "slots", "rollback"]
    assert "servers ready" not in capsys.readouterr().out


@pytest.mark.parametrize("missing_draft", [False, True])
def test_dspark_exec_cli_fails_cleanly_before_server_exec(tmp_path, missing_draft):
    """Exercise __main__ so imported helper exception classes cannot escape."""
    profile, config, draft = _fixture(tmp_path)
    marker = tmp_path / "server-started"
    binary = Path(profile.llama_bin)
    binary.write_text(
        f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
        "if sys.argv[1:] == ['--help']:\n"
        "    print('--spec-type none,draft-simple')\n    raise SystemExit(1)\n"
        f"Path({str(marker)!r}).write_text('started')\n", encoding="utf-8",
    )
    if missing_draft:
        draft.unlink()
    result = process_env.run(
        [sys.executable, str(Path(dp.__file__)), "exec", "main", "--deployment-config", str(config)],
        overrides={"HOME": str(tmp_path)}, capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "[deployment-profile] ERROR:" in result.stderr
    assert "Traceback" not in result.stderr
    assert not marker.exists()


def test_dspark_model_host_launch_rejects_stale_draft_alias(tmp_path):
    """A host must not serve changed draft settings under the previous B alias."""
    profile, _, _ = _fixture(tmp_path)
    main = profile.service("main")
    alias = model_identity.versioned_alias("main", main.model, dspark=main.dspark)
    main = replace(main, identity_alias=alias, deployment_mode="model-host")
    command = dp.build_server_command(main, profile.llama_bin, must_exist=True)
    assert command[command.index("--alias") + 1] == alias
    changed = replace(main, dspark=replace(main.dspark, draft_n_max=4))
    with pytest.raises(dp.ProfileError, match="regenerate model-host"):
        dp.build_server_command(changed, profile.llama_bin, must_exist=True)
