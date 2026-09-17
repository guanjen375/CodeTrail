"""A directory added in the TUI must reach the existing MCP immediately."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import client_app
import client_config
import client_mcp

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
