"""Primary runtime dependencies must never become successful weaker operations."""
from __future__ import annotations

import builtins
import os
import shutil
import subprocess
import types
from collections import OrderedDict
from pathlib import Path

import pytest

from runtime_dependencies import DependencyError, DEPENDENCY_ERROR_PREFIX

pytestmark = pytest.mark.smoke


@pytest.mark.parametrize("failure", ["missing", "disappeared", "timeout", "nonzero"])
def test_grep_requires_ripgrep_and_exposes_dependency_failure(tmp_path, monkeypatch, failure):
    import agent_tools

    (tmp_path / "source.c").write_text("int needle;\n")
    executor = agent_tools.ToolExecutor(str(tmp_path))
    monkeypatch.setattr(executor, "_rg_available", lambda: failure != "missing")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if failure == "disappeared":
            raise FileNotFoundError("rg")
        if failure == "timeout":
            raise agent_tools.process_env.TimeoutExpired(command, 30)
        return types.SimpleNamespace(returncode=2, stdout="", stderr="rg failed")

    monkeypatch.setattr(agent_tools.process_env, "run", run)
    result = executor.grep("needle", include="*.c")
    assert result.startswith(DEPENDENCY_ERROR_PREFIX), result
    assert "ripgrep" in result
    assert all(command[0] == "rg" for command in calls)


@pytest.mark.parametrize("failure", ["missing", "nonzero", "timeout"])
def test_lint_does_not_try_a_second_tool(tmp_path, monkeypatch, failure):
    import agent_tools

    (tmp_path / "source.py").write_text("x=1\n")
    monkeypatch.setitem(agent_tools.LINT_COMMANDS, ".py", {"check": ["ruff check", "black --check"]})
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "ruff":
            if failure == "missing":
                raise FileNotFoundError("ruff")
            if failure == "timeout":
                raise agent_tools.process_env.TimeoutExpired(command, 60)
            return types.SimpleNamespace(returncode=127, stdout="", stderr="ruff: not found")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(agent_tools.process_env, "run", run)
    result = agent_tools.ToolExecutor(str(tmp_path)).run_lint("source.py", fix=False)
    assert result.startswith("錯誤:"), result
    assert not any(command[0] == "black" for command in calls)


@pytest.mark.parametrize("failure", ["missing", "parse", "warm-cache"])
def test_elf_requires_pyelftools_before_loading_or_using_cache(tmp_path, monkeypatch, failure):
    import elf_analysis

    path = tmp_path / "image.elf"
    path.write_bytes(b"\x7fELFfake")
    monkeypatch.setattr(elf_analysis, "_MODEL_CACHE", OrderedDict())
    calls = []
    monkeypatch.setattr(elf_analysis, "_load_readelf", lambda model: calls.append("readelf"), raising=False)
    monkeypatch.setattr(elf_analysis, "_HAS_PYELFTOOLS", failure == "parse")
    if failure == "parse":
        monkeypatch.setattr(elf_analysis, "_load_pyelftools", lambda model: (_ for _ in ()).throw(ValueError("broken parser")))
    if failure == "warm-cache":
        stat = path.stat()
        elf_analysis._MODEL_CACHE[(str(path), stat.st_size, stat.st_mtime_ns)] = elf_analysis.ElfModel(path)
    with pytest.raises(DependencyError, match="pyelftools"):
        elf_analysis.load_model(path)
    assert not calls


def test_media_elf_cache_cannot_hide_missing_parser(tmp_path, monkeypatch):
    import elf_analysis
    import media

    path = tmp_path / "image.elf"
    path.write_bytes(b"\x7fELFfake")
    monkeypatch.setattr(media, "_safe_path", lambda *args, **kwargs: path)
    monkeypatch.setattr(media, "_cache_get", lambda *args: "STALE ELF REPORT")
    monkeypatch.setattr(elf_analysis, "_HAS_PYELFTOOLS", False)
    result = media.read_elf(str(path), view="headers")
    assert result.startswith("[ELF 錯誤]"), result
    assert "pyelftools" in result


@pytest.mark.parametrize("configured", ["", "/missing/cross-objdump"])
@pytest.mark.parametrize("failure", ["missing", "nonzero", "empty"])
def test_disassembly_uses_only_the_selected_objdump(tmp_path, monkeypatch, configured, failure):
    import config
    import elf_analysis

    path = tmp_path / "image.elf"
    path.write_bytes(b"\x7fELFfake")
    model = elf_analysis.ElfModel(path)
    model.header["machine"] = "EM_ARM"
    monkeypatch.setattr(config, "OBJDUMP", configured)
    monkeypatch.setattr(elf_analysis, "cmd_exists", lambda command: failure != "missing")
    monkeypatch.setattr(elf_analysis, "_disasm_plan", lambda *args: (
        {"label": "entry", "mode": "address", "start": 0, "stop": 16}, None))
    calls = []

    def capture(command, **kwargs):
        calls.append(command[0])
        return (1 if failure == "nonzero" else 0), "", "cannot disassemble architecture"

    monkeypatch.setattr(elf_analysis, "_run_capture", capture)
    monkeypatch.setattr(elf_analysis, "_capstone_disasm", lambda *args: (True, (["fake instruction"], 4)), raising=False)
    with pytest.raises(DependencyError, match="objdump"):
        elf_analysis.disassemble(model)
    assert set(calls) <= {configured or "objdump"}


@pytest.mark.parametrize("failure", ["missing", "nonzero", "timeout", "cardinality", "warm-cache"])
def test_demangling_requires_working_cppfilt_without_poisoning_cache(tmp_path, monkeypatch, failure):
    import elf_analysis

    path = tmp_path / "image.elf"
    path.write_bytes(b"\x7fELFfake")
    model = elf_analysis.ElfModel(path)
    if failure == "warm-cache":
        model._lazy["demangle"] = {"_Z3foov": "foo()"}
    before = dict(model._lazy.get("demangle", {}))
    monkeypatch.setattr(elf_analysis, "cmd_exists", lambda command: failure not in {"missing", "warm-cache"})

    def run(command, **kwargs):
        if failure == "timeout":
            raise elf_analysis.process_env.TimeoutExpired(command, 20)
        return types.SimpleNamespace(returncode=1 if failure == "nonzero" else 0,
                                     stdout="" if failure == "cardinality" else "foo()\n", stderr="broken")

    monkeypatch.setattr(elf_analysis.process_env, "run", run)
    with pytest.raises(DependencyError, match=r"c\+\+filt"):
        elf_analysis.demangle_names(model, ["_Z3foov"])
    assert model._lazy.get("demangle", {}) == before


@pytest.mark.parametrize("failure", ["missing", "nonzero", "timeout"])
def test_container_auto_never_substitutes_docker(tmp_path, monkeypatch, failure):
    import container_runner

    monkeypatch.setattr(container_runner, "CONTAINER_ENGINE", "auto")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "podman":
            if failure == "missing":
                raise FileNotFoundError("podman")
            if failure == "timeout":
                raise container_runner.process_env.TimeoutExpired(command, 5)
            return types.SimpleNamespace(returncode=1, stdout="", stderr="broken")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(container_runner.process_env, "run", run)
    result = container_runner.run_in_container("echo hello", str(tmp_path))
    assert not result["success"]
    assert "podman" in result["error"].lower()
    assert not any(command[0] == "docker" for command in calls)


def test_docker_cannot_omit_the_user_identity(tmp_path, monkeypatch):
    import container_runner

    monkeypatch.setattr(container_runner, "get_container_engine", lambda: "docker")
    calls = []
    monkeypatch.setattr(container_runner.process_env, "run", lambda command, **kwargs:
                        calls.append(command) or types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    with monkeypatch.context() as platform:
        platform.delattr(container_runner.os, "getuid")
        result = container_runner.run_in_container("echo hello", str(tmp_path))
    assert not result["success"]
    assert not calls


def test_container_python_test_command_has_no_second_interpreter(tmp_path, monkeypatch):
    import container_runner

    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    commands = []
    monkeypatch.setattr(container_runner, "run_in_container", lambda command, *args, **kwargs:
                        commands.append(command) or {"success": True})
    container_runner.run_tests_in_container(str(tmp_path))
    assert commands == ["python -m pytest -v 2>&1"]


def test_eval_requested_container_import_failure_never_runs_agent(tmp_path, monkeypatch):
    import config
    from eval import run_eval

    original_import = builtins.__import__
    original_flags = (config.RUN_COMMAND_ENABLED, config.PATCH_ENABLED)
    calls = []

    def import_module(name, *args, **kwargs):
        if name == "container_runner":
            raise ImportError("container runner unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_module)
    monkeypatch.setattr(run_eval, "run_agent", lambda *args, **kwargs:
                        calls.append(args) or (_ for _ in ()).throw(RuntimeError("HOST_CALLED")))
    case = types.SimpleNamespace(question="q", expected={})
    with pytest.raises(DependencyError, match="container"):
        run_eval.eval_bug_question(case, str(tmp_path), None, run_tests=True, use_container=True)
    assert not calls
    assert (config.RUN_COMMAND_ENABLED, config.PATCH_ENABLED) == original_flags


@pytest.mark.parametrize("failure", ["missing", "invalid", "exception", "unknown-safety"])
def test_startup_requires_observed_context_and_safe_verdict(tmp_path, monkeypatch, failure):
    import client_preflight
    import gpu_safety

    result = client_preflight.Preflight(root=tmp_path)
    profile = types.SimpleNamespace(service=lambda role:
                                    types.SimpleNamespace(base_url="http://127.0.0.1:65535", ctx=65536))

    def query(url):
        if failure == "exception":
            raise OSError("offline")
        return None if failure == "missing" else types.SimpleNamespace(n_ctx=0)

    monkeypatch.setattr(gpu_safety, "query_server_info", query)
    monkeypatch.setattr(gpu_safety, "check_safety", lambda *args, **kwargs:
                        types.SimpleNamespace(status="UNKNOWN", reason="unobservable", detail_lines=[]))
    with pytest.raises(client_preflight.PreflightError):
        if failure == "unknown-safety":
            client_preflight.check_ctx_safety(result, profile, 65536)
        else:
            client_preflight.observe_n_ctx(result, profile)
    assert result.n_ctx == 0


@pytest.mark.parametrize("available", [False, True])
def test_headless_observes_live_context_before_mcp_and_forwards_it(tmp_path, monkeypatch, available):
    import client_config
    import client_preflight
    import client_prompt
    import codetrail_chat
    import config
    import gpu_safety

    args = types.SimpleNamespace(policy="readonly", model="m", session=None)
    settings = client_config.ClientSettings(path=tmp_path / "client.json")
    monkeypatch.setattr(codetrail_chat, "_settings", lambda *args: settings)
    monkeypatch.setattr(client_config, "apply_to_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(config, "N_CTX", 16384)
    monkeypatch.setattr(gpu_safety, "query_server_info", lambda *args:
                        types.SimpleNamespace(n_ctx=65536) if available else None)
    calls = []

    def client(root, **kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(readonly=True, start=lambda: None)

    monkeypatch.setattr(codetrail_chat.client_mcp, "shared_client", client)
    monkeypatch.setattr(codetrail_chat.client_engine, "Engine", lambda options, **kwargs:
                        types.SimpleNamespace(options=options, load_tools=lambda: None))
    monkeypatch.setattr(client_prompt, "build_system_prompt", lambda *args:
                        client_prompt.SystemPrompt(text="SYSTEM"))
    if available:
        _, engine = codetrail_chat._build(tmp_path, args, persist=False)
        assert engine.options.n_ctx == 65536
        assert calls[0]["n_ctx"] == 65536
        assert calls[0]["readonly"] is True
    else:
        with pytest.raises(client_preflight.PreflightError):
            codetrail_chat._build(tmp_path, args, persist=False)
        assert not calls


def test_stop_cannot_claim_release_without_gpu_evidence(monkeypatch):
    from scripts import stop_servers

    monkeypatch.setattr(stop_servers, "_proc_state", lambda pid: "")
    monkeypatch.setattr(stop_servers, "_gpu_compute_pids", lambda: None)
    with pytest.raises(DependencyError, match="nvidia-smi"):
        stop_servers._wait_released({123: "main"}, timeout=1)


@pytest.mark.parametrize("failure", ["missing", "nonzero"])
def test_stop_listener_probe_cannot_report_unknown_port_as_free(monkeypatch, failure):
    from scripts import stop_servers

    monkeypatch.setattr(stop_servers.shutil, "which", lambda command: None if failure == "missing" else command)
    monkeypatch.setattr(stop_servers.process_env, "run", lambda *args, **kwargs:
                        types.SimpleNamespace(returncode=1, stdout="", stderr="ss failed"))
    with pytest.raises(DependencyError, match="ss"):
        stop_servers._listener_pids(8080)


def test_aicode_requires_python3_even_when_python_is_available(tmp_path):
    from tests._harness import REPO_ROOT, require_working_bash

    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    for command in ("dirname", "readlink", "cat"):
        (binary_dir / command).symlink_to(shutil.which(command))
    (binary_dir / "python").write_text("#!/bin/sh\nexit 0\n")
    (binary_dir / "python").chmod(0o755)
    result = subprocess.run([require_working_bash(), str(REPO_ROOT / "aicode"), "--help"],
                            env={**os.environ, "PATH": str(binary_dir)},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2, result.stderr
    assert "python3" in result.stderr


@pytest.mark.parametrize("reply", ["", "not-a-pid main\n"])
def test_stop_requires_valid_pane_pid_evidence(monkeypatch, reply):
    from scripts import stop_servers

    monkeypatch.setattr(stop_servers.process_env, "run", lambda *args, **kwargs:
                        types.SimpleNamespace(returncode=0, stdout=reply, stderr=""))
    with pytest.raises(DependencyError, match="tmux"):
        stop_servers._pane_pids("codetrail-main")


def test_stop_proc_permission_failure_is_not_process_exit(monkeypatch):
    from scripts import stop_servers

    def denied(path, *args, **kwargs):
        raise PermissionError("proc access denied")

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(DependencyError, match="/proc"):
        stop_servers._proc_state(123)


@pytest.mark.parametrize("failed_command", ["has-session", "kill-session"])
def test_stop_tmux_execution_failure_keeps_port_cleanup_and_returns_nonzero(monkeypatch, failed_command):
    from scripts import stop_servers

    profile = types.SimpleNamespace(mode="local", service=lambda role:
                                    types.SimpleNamespace(port=8080, base_url="http://localhost:8080"))
    monkeypatch.setattr(stop_servers, "load_effective_profile", lambda **kwargs: profile)
    monkeypatch.setattr(stop_servers.shutil, "which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    monkeypatch.setattr(stop_servers, "_pane_pids", lambda session: {11: session})
    monkeypatch.setattr(stop_servers, "_wait_released", lambda *args, **kwargs: [])
    ports = []
    monkeypatch.setattr(stop_servers, "_listener_pids", lambda port: ports.append(port) or set())

    def run(command, **kwargs):
        if command[:2] == ["tmux", failed_command]:
            raise FileNotFoundError("tmux disappeared")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(stop_servers.process_env, "run", run)
    assert stop_servers.main(["--scope", "aux"]) == 1
    assert ports == [8080, 8080, 8080]
