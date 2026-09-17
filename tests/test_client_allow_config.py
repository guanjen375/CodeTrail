"""Safety contracts for user-authorized executable settings and atomic edits."""
from __future__ import annotations

import copy
import json
import os
from dataclasses import replace

import pytest

import client_config
import config
from command_allowlist import validate_extra_allowed_commands
from runtime_policy import EXTRA_BUILD_COMMANDS


pytestmark = pytest.mark.smoke

_APPLIED_KEYS = (
    "EXTERNAL_IMPORT_ENABLED", "EXTERNAL_IMPORT_ROOTS", "KB_CONTEXT_REMOTE_OK",
    "MODEL_REMOTE_OK", "MODEL_ENDPOINTS", "RERANK_FALLBACK_POLICY",
    "PROJECT_INSTRUCTIONS_ENABLED", "OBJDUMP", "H_LANG", "USE_CONTAINER",
    "EXTRA_ALLOWED_COMMANDS", "COLLECT_DATA", "CTX_METRICS_ENABLED",
)


@pytest.fixture
def isolated_runtime(monkeypatch):
    # apply_to_config changes all of these, even in tests focused on one field.
    for name in _APPLIED_KEYS:
        monkeypatch.setattr(config, name, copy.deepcopy(getattr(config, name)))


def _write_raw(path, **values):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(
        json.dumps({"schema": 1, "compaction_mode": "manual", **values}),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_missing_extra_commands_fail_closed_without_creating_config(tmp_path):
    env = {"HOME": str(tmp_path)}
    settings = client_config.load_client_settings(env)
    assert settings.extra_allowed_commands == []
    assert settings.compaction_mode == "manual"
    assert not settings.present
    unchanged, written = client_config.update_extra_allowed_commands("remove", ["nsim"], env)
    assert unchanged == settings
    assert written is False
    assert not (tmp_path / ".config").exists()


def test_extra_commands_roundtrip_preserves_other_settings(tmp_path):
    env = {"HOME": str(tmp_path)}
    path = client_config.config_path(env)
    settings = client_config.ClientSettings(
        path=path,
        permission={"run_command": "ask", "apply_patch": "deny"},
        compaction_mode="off",
        project_instructions=False,
        keep_historical_reasoning=True,
        build_commands=True,
        external_import=True,
        external_import_roots=["/tmp/imports"],
        extra_allowed_commands=["nsim", "mdb", "toolchain-v1.2+debug"],
    )
    client_config.save_client_settings(settings, env)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    loaded = client_config.load_client_settings(env)
    assert loaded.as_json() == settings.as_json()
    assert client_config.load_client_settings_from(path).as_json() == settings.as_json()

    changed = loaded.with_compaction("manual")
    client_config.save_client_settings(changed, env)
    expected = {**settings.as_json(), "compaction_mode": "manual"}
    assert client_config.load_client_settings(env).as_json() == expected


@pytest.mark.parametrize("value", [
    "nsim", None, True, [1], [""], ["nsim --version"], ["../nsim"], ["/usr/bin/nsim"],
    [r"bin\nsim"], ["nsim*"], ["nsim;id"], ["nsim\n"], ["nsim\x00"], ["模擬器"],
    ["-nsim"], ["nsim", "nsim"], ["a" * 129], [f"tool_{n}" for n in range(129)],
])
def test_extra_commands_loader_rejects_unsafe_or_ambiguous_values(tmp_path, value):
    env = {"HOME": str(tmp_path)}
    path = client_config.config_path(env)
    _write_raw(path, extra_allowed_commands=value)
    with pytest.raises(client_config.ClientConfigError, match="extra_allowed_commands"):
        client_config.load_client_settings(env)


def test_extra_commands_never_widen_reserved_executable_roots():
    forbidden = {
        "rm", "sudo", "curl", "bash", "sh", "dash", "zsh", "fish", "ksh", "csh",
        "tcsh", "env", "xargs", "exec", "command", "eval", "source", "busybox",
        "git", "python3", "node", "perl", "ruby",
    }
    forbidden.update(command.split()[0] for command in config.ALLOWED_COMMANDS)
    forbidden.update(command.split()[0] for command in EXTRA_BUILD_COMMANDS)
    for name in sorted(forbidden):
        with pytest.raises(ValueError) as raised:
            validate_extra_allowed_commands([name])
        assert repr(name) in str(raised.value)
    assert validate_extra_allowed_commands(["nsim", "mdb"]) == ["nsim", "mdb"]


def test_allow_edits_read_latest_settings_and_noops_do_not_write(tmp_path, monkeypatch):
    env = {"HOME": str(tmp_path)}
    path = client_config.config_path(env)

    def no_runtime_change(*_args, **_kwargs):
        pytest.fail("editing stored authorizations must not change the live runtime")

    monkeypatch.setattr(client_config, "apply_to_config", no_runtime_change)
    added, written = client_config.update_extra_allowed_commands("add", ["nsim", "mdb"], env)
    assert written is True
    assert added.present
    assert added.compaction_mode == "manual"
    assert added.extra_allowed_commands == ["nsim", "mdb"]

    # Another settings editor changed a separate field; the next edit must retain it.
    latest = replace(added, permission={"run_command": "deny"}, project_instructions=False)
    client_config.save_client_settings(latest, env)
    changed, written = client_config.update_extra_allowed_commands("add", ["mdb", "probe"], env)
    assert written is True
    assert changed.as_json() == {**latest.as_json(), "extra_allowed_commands": ["nsim", "mdb", "probe"]}

    before = path.read_bytes()
    with monkeypatch.context() as guard:
        def no_save(*_args, **_kwargs):
            pytest.fail("no-op edits must not write")
        guard.setattr(client_config, "save_client_settings", no_save)
        assert client_config.update_extra_allowed_commands("add", ["nsim", "mdb"], env) == (changed, False)
        assert client_config.update_extra_allowed_commands("remove", ["missing"], env) == (changed, False)
    assert path.read_bytes() == before

    removed, written = client_config.update_extra_allowed_commands("remove", ["mdb", "missing"], env)
    assert written is True
    assert removed.as_json() == {**latest.as_json(), "extra_allowed_commands": ["nsim", "probe"]}
    assert client_config.load_client_settings(env).as_json() == removed.as_json()


def test_allow_invalid_batch_is_rejected_before_reading_or_writing(tmp_path, monkeypatch):
    env = {"HOME": str(tmp_path)}

    def no_read(*_args, **_kwargs):
        pytest.fail("the whole incoming batch must be validated before reading settings")

    monkeypatch.setattr(client_config, "load_client_settings", no_read)
    for action, commands in (
        ("replace", ["nsim"]), ("add", []), ("remove", []), ("add", ["nsim", "bash"]),
        ("remove", ["nsim", "../mdb"]), ("add", ["nsim", "nsim"]),
    ):
        with pytest.raises(client_config.ClientConfigError):
            client_config.update_extra_allowed_commands(action, commands, env)
    assert not (tmp_path / ".config").exists()


def test_allow_rejects_oversized_result_without_partial_save(tmp_path):
    env = {"HOME": str(tmp_path)}
    names = [f"tool_{n}" for n in range(128)]
    settings, _ = client_config.update_extra_allowed_commands("add", names, env)
    before = settings.path.read_bytes()
    with pytest.raises(client_config.ClientConfigError, match="128"):
        client_config.update_extra_allowed_commands("add", ["nsim", "mdb"], env)
    assert settings.path.read_bytes() == before
    assert client_config.load_client_settings(env).extra_allowed_commands == names


@pytest.mark.parametrize("invalid", [
    {"extra_allowed_commands": "nsim"},
    {"extra_allowed_commands": ["nsim", "bash"]},
    {"build_commands": "false"},
    {"permission": {"run_command": "sometimes"}},
    {"external_import": True, "external_import_roots": []},
    {"objdump": "測" * (client_config.MAX_BYTES // 2)},
])
def test_save_validates_all_values_and_byte_budget_before_any_write(tmp_path, monkeypatch, invalid):
    env = {"HOME": str(tmp_path)}
    path = client_config.config_path(env)
    settings = client_config.ClientSettings(path=path, **invalid)

    def no_write(*_args, **_kwargs):
        pytest.fail("invalid settings must fail before creating directories or replacing files")

    monkeypatch.setattr(client_config.client_paths, "replace_private_file", no_write)
    with pytest.raises(client_config.ClientConfigError):
        client_config.save_client_settings(settings, env)
    assert not (tmp_path / ".config").exists()


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink", "directory_symlink"])
def test_allow_edits_keep_owner_only_link_defenses(tmp_path, link_kind):
    env = {"HOME": str(tmp_path)}
    path = client_config.config_path(env)
    victim = tmp_path / "elsewhere" / "client.json"
    _write_raw(victim, extra_allowed_commands=["nsim"])
    before = victim.read_bytes()
    path.parent.parent.mkdir(parents=True, mode=0o700)
    if link_kind == "directory_symlink":
        path.parent.symlink_to(victim.parent, target_is_directory=True)
    else:
        path.parent.mkdir(mode=0o700)
        if link_kind == "symlink":
            path.symlink_to(victim)
        else:
            os.link(victim, path)

    with pytest.raises(client_config.ClientConfigError, match="symlink|hard-link"):
        client_config.update_extra_allowed_commands("add", ["mdb"], env)
    assert victim.read_bytes() == before
    assert victim.stat().st_mode & 0o777 == 0o600


def test_allow_refuses_invalid_existing_settings_without_overwriting_them(tmp_path):
    env = {"HOME": str(tmp_path)}
    path = client_config.config_path(env)
    _write_raw(path, extra_allowed_commands=["nsim"], unknown_permission_knob=True)
    before = path.read_bytes()
    with pytest.raises(client_config.ClientConfigError, match="unknown_permission_knob"):
        client_config.update_extra_allowed_commands("add", ["mdb"], env)
    assert path.read_bytes() == before


def test_apply_extra_commands_replaces_copies_and_clears_for_readonly(tmp_path, isolated_runtime):
    settings = client_config.ClientSettings(path=tmp_path / "client.json", extra_allowed_commands=["nsim"])
    client_config.apply_to_config(settings)
    assert config.EXTRA_ALLOWED_COMMANDS == ["nsim"]
    assert config.EXTRA_ALLOWED_COMMANDS is not settings.extra_allowed_commands
    client_config.apply_to_config(replace(settings, extra_allowed_commands=["mdb"]))
    assert config.EXTRA_ALLOWED_COMMANDS == ["mdb"]
    client_config.apply_to_config(replace(settings, extra_allowed_commands=[]))
    assert config.EXTRA_ALLOWED_COMMANDS == []
    client_config.apply_to_config(settings, readonly=True)
    assert config.EXTRA_ALLOWED_COMMANDS == []


@pytest.mark.parametrize("readonly", [False, True])
def test_invalid_extra_commands_do_not_partially_apply_runtime(tmp_path, isolated_runtime, readonly):
    before = {name: copy.deepcopy(getattr(config, name)) for name in _APPLIED_KEYS}
    settings = client_config.ClientSettings(
        path=tmp_path / "client.json", extra_allowed_commands=["nsim", "bash"],
        external_import=not config.EXTERNAL_IMPORT_ENABLED,
        model_remote_ok=not config.MODEL_REMOTE_OK,
        project_instructions=not config.PROJECT_INSTRUCTIONS_ENABLED,
    )
    with pytest.raises(client_config.ClientConfigError, match="bash"):
        client_config.apply_to_config(settings, readonly=readonly)
    assert {name: getattr(config, name) for name in _APPLIED_KEYS} == before
