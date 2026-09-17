"""User command extensions must reach execution without bypassing its safety gates."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_tools
import client_config
import config
import container_runner

pytestmark = pytest.mark.smoke


@pytest.fixture
def runtime(monkeypatch):
    # apply_to_config writes several switches; none may leak to another test.
    for name, value in vars(config).copy().items():
        if name.isupper():
            monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, "EXTRA_ALLOWED_COMMANDS", [], raising=False)
    monkeypatch.setattr(config, "EXTRA_ALLOWED_COMMAND_DIRS", [])
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", True)
    monkeypatch.setattr(container_runner, "CONTAINER_ENABLED", False)


def test_user_toolchain_commands_load_and_run_without_repo_edit(tmp_path, monkeypatch, runtime):
    """Reported gap: a personal ARC executable cannot be authorized in client.json."""
    directory = tmp_path / "settings"
    directory.mkdir(mode=0o700)
    path = directory / "client.json"
    path.write_text(json.dumps({
        "schema": 1, "compaction_mode": "manual",
        "extra_allowed_commands": ["nsim", "mdb"],
    }), encoding="utf-8")
    path.chmod(0o600)
    settings = client_config.load_client_settings_from(path)
    client_config.apply_to_config(settings)
    root = tmp_path / "project"
    root.mkdir()
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="ARC toolchain probe", stderr="")

    monkeypatch.setattr(agent_tools.process_env, "run", run)
    executor = agent_tools.ToolExecutor(str(root))
    for name in ("nsim", "mdb"):
        assert "成功" in executor.run_command(f"{name} --version")
    assert [argv for argv, _ in calls] == [["nsim", "--version"], ["mdb", "--version"]]
    assert all(kwargs["shell"] is False and kwargs["cwd"] == str(root)
               for _, kwargs in calls)
    assert "nsim" not in config.ALLOWED_COMMANDS
    assert "mdb" not in config.ALLOWED_COMMANDS


def test_extra_commands_are_replaced_and_readonly_clears_authorization(tmp_path, runtime):
    settings = client_config.ClientSettings(path=tmp_path / "client.json", extra_allowed_commands=["nsim"])
    executor = agent_tools.ToolExecutor(str(tmp_path))
    client_config.apply_to_config(settings)
    assert executor._validate_command("nsim --version")[0]
    client_config.apply_to_config(replace(settings, extra_allowed_commands=["mdb"]))
    assert not executor._validate_command("nsim --version")[0]
    assert executor._validate_command("mdb --version")[0]
    client_config.apply_to_config(settings, readonly=True)
    assert config.EXTRA_ALLOWED_COMMANDS == []
    assert not executor._validate_command("nsim --version")[0]
    client_config.apply_to_config(settings)
    client_config.apply_to_config(replace(settings, extra_allowed_commands=[]))
    assert not executor._validate_command("nsim --version")[0]


def test_extra_commands_keep_exact_names_and_argument_guards(tmp_path, runtime, monkeypatch):
    client_config.apply_to_config(client_config.ClientSettings(
        path=tmp_path / "client.json", extra_allowed_commands=["nsim"],
    ))
    executor = agent_tools.ToolExecutor(str(tmp_path))
    for cmd in (
        "nsim-other --version", "./nsim --version", "/usr/bin/nsim --version",
        "nsim --version; rm -rf .", "nsim $(whoami)", "nsim `whoami`",
        "nsim a | cat", "nsim a > output", "nsim ../outside.elf",
        "nsim --config=/outside.ini", "nsim -f /outside.elf",
    ):
        assert not executor._validate_command(cmd)[0], cmd
    assert executor._validate_command("nsim firmware.elf")[0]
    # The built-in portion must not retain an import snapshot either.
    monkeypatch.setattr(config, "ALLOWED_COMMANDS", ["ctest"])
    assert not executor._validate_command("pytest -h")[0]
    assert executor._validate_command("ctest -h")[0]


def test_extra_commands_do_not_change_tool_permission_or_execution_gates(tmp_path, runtime, monkeypatch):
    import client_policy

    settings = client_config.ClientSettings(path=tmp_path / "client.json", extra_allowed_commands=["nsim"])
    client_config.apply_to_config(settings)
    assert client_config.policy_for(settings).decide(
        "run_command", read_only=False, arguments={"cmd": "nsim"},
    ) is client_policy.Decision.ASK
    assert client_config.policy_for(settings, client_policy.ReadOnlyPolicy()).decide(
        "run_command", read_only=False, arguments={"cmd": "nsim"},
    ) is client_policy.Decision.DENY
    spawned = []
    monkeypatch.setattr(agent_tools.process_env, "run", lambda *a, **k: spawned.append(a))
    executor = agent_tools.ToolExecutor(str(tmp_path))
    for timeout in (True, "60", 0, 601):
        assert "timeout 必須" in executor.run_command("nsim --version", timeout=timeout)
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", False)
    assert "已停用" in executor.run_command("nsim --version")
    assert spawned == []


def test_extra_commands_never_fall_back_from_container_to_host(tmp_path, runtime, monkeypatch):
    client_config.apply_to_config(client_config.ClientSettings(
        path=tmp_path / "client.json", extra_allowed_commands=["nsim"],
    ))
    monkeypatch.setattr(container_runner, "CONTAINER_ENABLED", True)
    calls = []

    def container(**kwargs):
        calls.append(kwargs)
        return {"error": "container unavailable"}

    def host(*args, **kwargs):
        pytest.fail("a container failure must not execute on the host")

    monkeypatch.setattr(container_runner, "run_in_container", container)
    monkeypatch.setattr(agent_tools.process_env, "run", host)
    executor = agent_tools.ToolExecutor(str(tmp_path))
    assert "container unavailable" in executor.run_command("nsim --version")
    assert calls == [{"command": "nsim --version", "folder": str(tmp_path),
                      "timeout": 60, "network": False, "writable": False}]
    assert "不允許" in executor.run_command("nsim --version; echo bad")
    assert len(calls) == 1


@pytest.mark.parametrize("readonly", [False, True])
def test_explicit_client_config_reaches_live_mcp_and_readonly_still_denies(tmp_path, readonly):
    """Real stdio startup must use its selected file, not HOME or client-only state."""
    import client_mcp

    directory = tmp_path / "settings"
    directory.mkdir(mode=0o700)
    path = directory / "client.json"
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir(mode=0o700)
    tool = tool_dir / "arc-readonly-probe"
    tool.write_text("#!/bin/sh\nprintf '%s\\n' directory-authorized\n", encoding="utf-8")
    tool.chmod(0o700)
    path.write_text(json.dumps({
        "schema": 1, "compaction_mode": "manual", "extra_allowed_commands": ["true"],
        "extra_allowed_command_dirs": [str(tool_dir)],
    }), encoding="utf-8")
    path.chmod(0o600)
    root = tmp_path / "project"
    root.mkdir()
    client = client_mcp.McpClient(
        root, readonly=readonly, client_config=path, n_ctx=8192,
        skip_aux_preflight=True, env={"XDG_STATE_HOME": str(tmp_path / "state")},
    )
    try:
        client.start()
        # This is the descriptor actually exposed through tools/list.
        spec = next(item for item in client.tools() if item.name == "run_command")
        assert "extra_allowed_commands" in spec.description
        assert "extra_allowed_command_dirs" in spec.description
        assert spec.command_policy == client.command_policy
        assert client.command_policy["readonly"] is readonly
        assert client.command_policy["run_command_enabled"] is (not readonly)
        assert client.command_policy["build_prefixes"] == []
        for command in ("true", tool.name):
            result = client.call("run_command", {"cmd": command})
            if readonly:
                assert result.is_error or "已停用" in result.text or "readonly" in result.text
                assert "成功" not in result.text
            else:
                assert not result.is_error and "成功" in result.text
        if not readonly:
            rejected = client.call("run_command", {"cmd": "false"})
            assert "不允許的命令" in rejected.text
    finally:
        client.close()
