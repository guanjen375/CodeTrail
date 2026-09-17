"""A directory added in the TUI must reach the existing MCP immediately."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import agent_tools
import client_app
import client_config
import client_mcp
import config
import container_runner

pytestmark = pytest.mark.smoke


def test_allow_add_directory_reaches_running_mcp_without_restart(tmp_path, monkeypatch):
    """Reported flow: /allow add <bin directory> was rejected as an executable name."""
    config_dir = tmp_path / "settings"
    config_dir.mkdir(mode=0o700)
    path = config_dir / "client.json"
    path.write_text(json.dumps({"schema": 1, "compaction_mode": "manual"}) + "\n")
    path.chmod(0o600)
    monkeypatch.setattr(client_config, "config_path", lambda env=None: path)
    root = tmp_path / "project"
    root.mkdir()
    directory = tmp_path / "toolchain" / "bin"
    directory.mkdir(parents=True)
    executable = directory / "arc-allow-probe"
    executable.write_text("#!/bin/sh\nprintf 'DIRECTORY_ALLOW_LIVE_OK\\n'\n")
    executable.chmod(0o755)
    client = client_mcp.McpClient(
        root, client_config=path, n_ctx=8192, skip_aux_preflight=True,
        env={"XDG_STATE_HOME": str(tmp_path / "state")},
    )
    engine = SimpleNamespace(
        session_id="20260917T000000-abcdef01", messages=[], mcp=client,
        options=SimpleNamespace(policy=SimpleNamespace(name="interactive")),
    )
    app = client_app.CodeTrailApp(engine)
    widgets = []
    monkeypatch.setattr(app, "_append", widgets.append)
    try:
        client.start()
        pid = client.pid
        denied = client.call("run_command", {"cmd": "arc-allow-probe"})
        assert "不允許的命令" in denied.text
        app._cmd_allow(f"add {directory}")
        assert isinstance(widgets[-1], client_app.NoticeLine), widgets[-1].message
        settings = client_config.load_client_settings_from(path)
        assert settings.extra_allowed_command_dirs == [str(directory)]
        accepted = client.call("run_command", {"cmd": "arc-allow-probe"})
        assert not accepted.is_error and "DIRECTORY_ALLOW_LIVE_OK" in accepted.text, accepted.text
        assert client.pid == pid
        app._cmd_allow("list")
        assert isinstance(widgets[-1], client_app.NoticeLine), widgets[-1].message
        assert "pytest" in widgets[-1].message
        assert "arc-allow-probe" in widgets[-1].message
        assert str(directory) in widgets[-1].message
        assert engine.messages == []
        assert client_config.load_client_settings_from(path).compaction_mode == "manual"
    finally:
        client.close()


def test_rejected_tool_path_names_bare_tool_grants_and_shell_limits(tmp_path, monkeypatch):
    """Reported session: after /allow add the model retried `MetaWare/arc/bin/llvm-objdump ... | head`.

    The rejection only listed the first builtin prefixes, so the model reported the tool as still
    not whitelisted. Paths stay rejected without any path or basename fallback, but the reply must
    name the bare tool, list the directory grants and state that shell syntax is unsupported.
    """
    root = tmp_path / "project"
    directory = root / "MetaWare" / "arc" / "bin"
    directory.mkdir(parents=True)
    tool = directory / "llvm-objdump"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    calls = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", True)
    monkeypatch.setattr(container_runner, "CONTAINER_ENABLED", False)
    monkeypatch.setattr(agent_tools.process_env, "run", run)
    executor = agent_tools.ToolExecutor(
        str(root), command_settings_loader=lambda: ([], [str(directory)]),
    )

    reported = executor.run_command("MetaWare/arc/bin/llvm-objdump -d example.elf | head -80")
    absolute = executor.run_command(f"{tool} -d example.elf")
    for message in (reported, absolute):
        lines = message.splitlines()
        assert lines[0] == "錯誤: 不允許的命令。", message
        assert any("裸名稱 llvm-objdump" in line for line in lines), message
        assert any(line.startswith("已授權工具") and "llvm-objdump" in line for line in lines), message
    assert any("不經 shell" in line and "|" in line for line in reported.splitlines()), reported
    assert "不經 shell" not in absolute
    shell = executor.run_command("llvm-objdump -d example.elf | head -80")
    assert "'|'" in shell and "不經 shell" in shell, shell
    assert calls == []
    assert "成功" in executor.run_command("llvm-objdump -d example.elf")
    assert calls == [[str(tool), "-d", "example.elf"]]
