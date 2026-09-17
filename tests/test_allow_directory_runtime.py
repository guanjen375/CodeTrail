"""Directory authorization must remain fresh without widening execution or MCP policy."""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_tools
import client_config
import client_mcp
import client_policy
import config
import container_runner
from mcp_contract import PUBLIC_TOOL_ORDER

pytestmark = pytest.mark.smoke


@pytest.fixture
def runtime(monkeypatch):
    for name, value in vars(config).copy().items():
        if name.isupper():
            monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, "ALLOWED_COMMANDS", list(config.ALLOWED_COMMANDS))
    monkeypatch.setattr(config, "EXTRA_ALLOWED_COMMANDS", [])
    monkeypatch.setattr(config, "EXTRA_ALLOWED_COMMAND_DIRS", [])
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", True)
    monkeypatch.setattr(container_runner, "CONTAINER_ENABLED", False)


def _tool(directory: Path, name: str = "arc-allow-probe", marker: str = "trusted") -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{marker}'\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _write_settings(path: Path, **values) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(json.dumps({"schema": 1, "compaction_mode": "manual", **values}), encoding="utf-8")
    path.chmod(0o600)


def test_directory_executor_uses_fresh_settings_and_absolute_argv_without_path_fallback(
    tmp_path, monkeypatch, runtime,
):
    root = tmp_path / "project"
    root.mkdir()
    first = _tool(tmp_path / "first tools", marker="first-trusted")
    second = _tool(tmp_path / "second tools", marker="second-trusted")
    shadow = _tool(tmp_path / "shadow", marker="PATH-shadow")
    monkeypatch.setenv("PATH", str(shadow.parent))
    settings = ([], [])
    loads, calls = [], []
    original_run = agent_tools.process_env.run

    def load():
        loads.append(True)
        return settings

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return original_run(argv, **kwargs)

    monkeypatch.setattr(agent_tools.process_env, "run", run)
    executor = agent_tools.ToolExecutor(str(root), command_settings_loader=load)
    assert "不允許的命令" in executor.run_command("arc-allow-probe --version")
    settings = ([], [str(first.parent)])
    assert "first-trusted" in executor.run_command("arc-allow-probe --version")
    settings = ([], [str(second.parent)])
    assert "second-trusted" in executor.run_command("arc-allow-probe --version")
    settings = ([], [])
    assert "不允許的命令" in executor.run_command("arc-allow-probe --version")
    assert len(loads) == 4
    assert [argv for argv, _ in calls] == [
        [str(first), "--version"], [str(second), "--version"],
    ]
    assert all(kwargs["shell"] is False and kwargs["cwd"] == str(root)
               and kwargs["overrides"] == {"PYTHONIOENCODING": "utf-8"}
               for _, kwargs in calls)
    assert config.EXTRA_ALLOWED_COMMANDS == [] and config.EXTRA_ALLOWED_COMMAND_DIRS == []


def test_directory_executor_revalidates_and_fails_closed_without_cached_mapping(
    tmp_path, monkeypatch, runtime,
):
    tool = _tool(tmp_path / "tools")
    current = ([], [str(tool.parent)])
    failure = None
    calls = []

    def load():
        if failure is not None:
            raise failure
        return current

    def run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="trusted", stderr="")

    monkeypatch.setattr(agent_tools.process_env, "run", run)
    executor = agent_tools.ToolExecutor(str(tmp_path), command_settings_loader=load)
    assert "成功" in executor.run_command(tool.name)
    assert len(calls) == 1
    tool.chmod(0o600)
    assert "授權解析失敗" in executor.run_command(tool.name)
    assert len(calls) == 1
    tool.chmod(0o700)
    assert "成功" in executor.run_command(tool.name)
    assert len(calls) == 2
    current = ([], [str(tool.parent), str(tmp_path / "gone")])
    assert "授權解析失敗" in executor.run_command(tool.name)
    assert "授權解析失敗" in executor.run_command("pytest -h")
    current = ([tool.name], [str(tool.parent)])
    assert "授權解析失敗" in executor.run_command(tool.name)
    failure = client_config.ClientConfigError("private settings cannot be read")
    assert "private settings cannot be read" in executor.run_command(tool.name)
    assert len(calls) == 2


def test_directory_executor_keeps_bare_names_dangerous_patterns_and_path_containment(
    tmp_path, monkeypatch, runtime,
):
    root = tmp_path / "project"
    root.mkdir()
    tool = _tool(tmp_path / "tools")
    _tool(tool.parent, name="pytest")
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(agent_tools.process_env, "run", run)
    monkeypatch.setattr(config, "ALLOWED_COMMANDS", ["pytest --version"])
    executor = agent_tools.ToolExecutor(
        str(root), command_settings_loader=lambda: ([], [str(tool.parent)]),
    )
    rejected = (
        str(tool), "./arc-allow-probe", "arc-allow-probe-other", "pytest -h",
        "arc-allow-probe x; echo bad", "arc-allow-probe $(whoami)",
        "arc-allow-probe `whoami`", "arc-allow-probe x | cat",
        "arc-allow-probe x > output", "arc-allow-probe ../outside.elf",
        "arc-allow-probe --config=/outside.ini", "arc-allow-probe -f /outside.elf",
        f"arc-allow-probe {tool}",
    )
    for command in rejected:
        assert "錯誤" in executor.run_command(command), command
    assert calls == []
    assert "成功" in executor.run_command("arc-allow-probe firmware.elf")
    assert calls == [[str(tool), "firmware.elf"]]
    assert executor._validate_command("pytest --version")[2] == ["pytest", "--version"]


def test_directory_executor_does_not_load_before_disabled_or_timeout_gates(
    tmp_path, monkeypatch, runtime,
):
    def forbidden():
        pytest.fail("disabled/invalid timeout must not even read command settings")

    executor = agent_tools.ToolExecutor(str(tmp_path), command_settings_loader=forbidden)
    monkeypatch.setattr(agent_tools.process_env, "run", lambda *a, **k: pytest.fail("unexpected spawn"))
    for timeout in (True, "60", 1.0, 0, 601):
        assert "timeout 必須" in executor.run_command("arc-allow-probe", timeout=timeout)
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", False)
    assert "已停用" in executor.run_command("arc-allow-probe")


def test_direct_directory_executor_uses_only_config_and_keeps_permission_and_readonly(
    tmp_path, monkeypatch, runtime,
):
    tool = _tool(tmp_path / "tools")
    settings = client_config.ClientSettings(
        path=tmp_path / "client.json", extra_allowed_command_dirs=[str(tool.parent)],
    )
    client_config.apply_to_config(settings)
    for name in ("load_client_settings", "load_client_settings_from"):
        monkeypatch.setattr(client_config, name, lambda *a, **k: pytest.fail("direct executor read HOME"))
    executor = agent_tools.ToolExecutor(str(tmp_path))
    assert executor._validate_command(tool.name)[2] == [str(tool)]
    assert client_config.policy_for(settings).decide(
        "run_command", read_only=False, arguments={"cmd": tool.name},
    ) is client_policy.Decision.ASK
    denied = replace(settings, permission={"run_command": "deny"})
    assert client_config.policy_for(denied).decide(
        "run_command", read_only=False, arguments={"cmd": tool.name},
    ) is client_policy.Decision.DENY
    client_config.apply_to_config(settings, readonly=True)
    assert config.EXTRA_ALLOWED_COMMANDS == [] and config.EXTRA_ALLOWED_COMMAND_DIRS == []
    assert not executor._validate_command(tool.name)[0]
    assert client_config.policy_for(settings, client_policy.ReadOnlyPolicy()).decide(
        "run_command", read_only=False, arguments={"cmd": tool.name},
    ) is client_policy.Decision.DENY


def test_directory_executor_refuses_container_without_host_or_legacy_semantic_changes(
    tmp_path, monkeypatch, runtime,
):
    tool = _tool(tmp_path / "tools")
    monkeypatch.setattr(container_runner, "CONTAINER_ENABLED", True)
    calls = []

    def container(**kwargs):
        calls.append(kwargs)
        return {"error": "offline container"}

    monkeypatch.setattr(container_runner, "run_in_container", container)
    monkeypatch.setattr(agent_tools.process_env, "run", lambda *a, **k: pytest.fail("host fallback"))
    executor = agent_tools.ToolExecutor(
        str(tmp_path), command_settings_loader=lambda: (["legacy-probe"], [str(tool.parent)]),
    )
    assert "目錄授權工具無法在容器" in executor.run_command(tool.name)
    assert calls == []
    command = 'legacy-probe "$HOME"'
    assert "offline container" in executor.run_command(command)
    assert calls == [{"command": command, "folder": str(tmp_path), "timeout": 60,
                      "network": False, "writable": False}]


@pytest.mark.parametrize("explicit", [False, True])
def test_live_mcp_reloads_only_allow_from_selected_source_and_keeps_runtime_policy(
    tmp_path, explicit,
):
    from tests._harness import seed_home

    home = seed_home(tmp_path / "home")
    root = tmp_path / "project"
    root.mkdir()
    default_path = home / ".config" / "codetrail" / "client.json"
    selected = tmp_path / "explicit" / "client.json" if explicit else default_path
    if explicit:
        _write_settings(default_path, extra_allowed_commands=["false"])
    _write_settings(selected, extra_allowed_commands=["true"])
    tool = _tool(tmp_path / "tools", marker="same-instance-directory")
    client = client_mcp.McpClient(
        root, client_config=selected if explicit else None, n_ctx=8192,
        skip_aux_preflight=True,
        env={"HOME": str(home), "XDG_STATE_HOME": str(tmp_path / "state")},
    )
    try:
        client.start()
        pid = client.pid
        initial_policy = client.command_policy
        assert initial_policy["schema"] == 1
        assert "pytest" in initial_policy["builtin_prefixes"]
        assert initial_policy["build_prefixes"] == []
        assert initial_policy["run_command_enabled"] is True
        assert initial_policy["readonly"] is False
        assert initial_policy["use_container"] is False
        assert "成功" in client.call("run_command", {"cmd": "true"}).text
        assert "不允許的命令" in client.call("run_command", {"cmd": tool.name}).text
        _write_settings(
            selected, extra_allowed_commands=["echo"],
            extra_allowed_command_dirs=[str(tool.parent)],
            build_commands=True, use_container=True, permission={"run_command": "deny"},
            show_reasoning=True,
        )
        assert "same-instance-directory" in client.call("run_command", {"cmd": tool.name}).text
        assert "legacy-live" in client.call("run_command", {"cmd": "echo legacy-live"}).text
        assert "不允許的命令" in client.call("run_command", {"cmd": "true"}).text
        assert "不允許的命令" in client.call("run_command", {"cmd": "make --version"}).text
        assert client.pid == pid and client.command_policy == initial_policy
        selected.chmod(0o644)
        assert "無法讀取或驗證 run_command 授權" in client.call("run_command", {"cmd": tool.name}).text
        selected.unlink()
        removed = client.call("run_command", {"cmd": tool.name}).text
        if explicit:
            assert "找不到指定的 client.json" in removed
        else:
            assert "不允許的命令" in removed
        assert client.pid == pid and client.command_policy == initial_policy
    finally:
        client.close()


def test_mcp_default_allow_loader_pins_startup_path_without_mutating_runtime(
    tmp_path, monkeypatch, runtime,
):
    from tests._harness import import_mcp_module, seed_home

    home = seed_home(tmp_path / "home")
    monkeypatch.setenv("HOME", str(home))
    selected = home / ".config" / "codetrail" / "client.json"
    _write_settings(selected, extra_allowed_commands=["legacy-probe"])
    root = tmp_path / "project"
    root.mkdir()
    module = import_mcp_module(monkeypatch, root)
    other_home = tmp_path / "other-home"
    _write_settings(other_home / ".config" / "codetrail" / "client.json",
                    extra_allowed_commands=["other-probe"])
    monkeypatch.setenv("HOME", str(other_home))
    monkeypatch.setattr(client_config, "apply_to_config", lambda *a, **k: pytest.fail("hot config mutation"))
    names, dirs = module._load_command_settings()
    assert names == ["legacy-probe"] and dirs == []
    names.append("locally-mutated")
    assert module._load_command_settings() == (["legacy-probe"], [])
    selected.unlink()
    assert module._load_command_settings() == ([], [])


def _policy(**changes):
    return {"schema": 1, "builtin_prefixes": ["pytest", "cargo test"],
            "build_prefixes": [], "run_command_enabled": True,
            "readonly": False, "use_container": False, **changes}


def _catalog(policy):
    return {"tools": [
        {"name": name, "description": name, "inputSchema": {"type": "object"},
         "annotations": {"readOnlyHint": name != "run_command",
                         "codetrailCommandPolicy": policy}}
        for name in PUBLIC_TOOL_ORDER
    ]}


def test_command_policy_metadata_is_optional_strict_and_never_changes_model_or_readonly():
    good = _policy()
    spec = next(item for item in client_mcp.tool_specs(_catalog(good)) if item.name == "run_command")
    good["builtin_prefixes"].append("after-parse")
    assert spec.command_policy == _policy() and spec.read_only is False
    assert "command_policy" not in spec.as_openai_tool()
    assert "codetrailCommandPolicy" not in json.dumps(spec.as_openai_tool())
    assert spec == replace(spec, command_policy={})
    assert all(item.command_policy == {} for item in client_mcp.tool_specs(_catalog(_policy()))
               if item.name != "run_command")
    malformed = [None, [], {}, _policy(schema=True), _policy(schema=2),
                 _policy(builtin_prefixes="pytest"), _policy(build_prefixes=[1]),
                 _policy(run_command_enabled="true"), _policy(readonly=1),
                 _policy(use_container=None)]
    for value in malformed:
        specs = client_mcp.tool_specs(_catalog(value))
        client_mcp.assert_public_catalog(specs)
        assert all(item.command_policy == {} for item in specs)
    for hint in (True, False, "true", "false", 1, None):
        listed = _catalog(_policy(readonly=True))
        tool = next(item for item in listed["tools"] if item["name"] == "run_command")
        tool["annotations"]["readOnlyHint"] = hint
        parsed = next(item for item in client_mcp.tool_specs(listed) if item.name == "run_command")
        assert parsed.read_only is (hint is True)


class _PolicyProcess:
    stdin = stdout = stderr = None

    def __init__(self, pid):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout):
        return self.returncode


def test_command_policy_cache_does_not_start_request_write_or_wait_on_lifecycle_lock(
    tmp_path, monkeypatch,
):
    client = client_mcp.McpClient(tmp_path)
    for name in ("tools", "start", "restart", "_request"):
        monkeypatch.setattr(client, name, lambda *a, **k: pytest.fail("cache property performed MCP work"))
    assert client.command_policy == {}
    proc = _PolicyProcess(1)
    client._proc = proc
    client._command_policy_snapshot = (proc, _policy())
    assert client.command_policy == {}, "unfinished handshake exposed a snapshot"
    client._startup_complete = True
    before = set(tmp_path.rglob("*"))
    copied = client.command_policy
    copied["builtin_prefixes"].append("mutation")
    assert client.command_policy == _policy()
    locked, release = threading.Event(), threading.Event()

    def hold_lifecycle():
        with client._lock:
            locked.set()
            release.wait(2)

    lock_worker = threading.Thread(target=hold_lifecycle)
    lock_worker.start()
    assert locked.wait(1)
    read_done = threading.Event()
    observed = []

    def read_cache():
        observed.append(client.command_policy)
        read_done.set()

    reader = threading.Thread(target=read_cache)
    reader.start()
    try:
        assert read_done.wait(0.5), "list waited for MCP spawn/handshake lifecycle lock"
        assert observed == [_policy()]
    finally:
        release.set()
        reader.join(2)
        lock_worker.join(2)
    proc.returncode = 1
    assert client.command_policy == {}, "dead instance retained live metadata"
    proc.returncode = None
    client._proc = _PolicyProcess(2)
    assert client.command_policy == {}, "old process snapshot leaked into a new instance"
    client._proc = proc
    client.close()
    assert client.command_policy == {}
    assert set(tmp_path.rglob("*")) == before


def test_command_policy_snapshot_updates_on_handshake_and_normal_respawn(tmp_path, monkeypatch):
    client = client_mcp.McpClient(tmp_path)
    processes, requests = [], []
    current = _policy()

    def popen(*args, **kwargs):
        proc = _PolicyProcess(len(processes) + 1)
        processes.append(proc)
        return proc

    def request(method, params, **kwargs):
        requests.append(method)
        assert client.command_policy == {}, "unfinished handshake reused old metadata"
        return _catalog(current) if method == "tools/list" else {}

    monkeypatch.setattr(client_mcp.process_env, "popen", popen)
    monkeypatch.setattr(client, "_read_stdout", lambda proc: None)
    monkeypatch.setattr(client, "_read_stderr", lambda proc: None)
    monkeypatch.setattr(client, "_send", lambda message: None)
    monkeypatch.setattr(client, "_request", request)
    try:
        client.start()
        assert client.command_policy == current and client.pid == 1
        # tools() consumers must not mutate the independent runtime snapshot.
        spec = next(item for item in client.tools() if item.name == "run_command")
        spec.command_policy["builtin_prefixes"].append("mutated-tools-cache")
        assert client.command_policy == current
        current = _policy(build_prefixes=["make"], use_container=True)
        client.restart()
        assert client.pid == 2 and processes[0].poll() is not None
        assert client.command_policy == current
        current = None  # older stub servers need no new metadata to handshake.
        client.restart()
        assert client.pid == 3 and client.command_policy == {}
        assert requests == ["initialize", "tools/list"] * 3
    finally:
        client.close()
    assert client.command_policy == {}
