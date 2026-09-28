"""run_command 的專案內執行檔：不需要授權，但每次呼叫都 fail-closed 地重新驗證。

argv[0] 含 "/" 時視為專案內工具（command_allowlist.resolve_project_executable）：
純字串拒絕（..、~、控制字元、專案外）先於任何檔案系統存取；之後自 `/` 逐層
dir-fd／O_NOFOLLOW，owner／權限／檔頭／身分全驗，只把 argv[0] 換成驗證過的絕對路徑。
取代舊的 /allow 目錄授權；仍適用的檢查點（symlink、owner、ELF 檔頭、FIFO、身分漂移、
缺安全能力、shell 字元、參數 containment、timeout／停用閘、容器不退回 host、live MCP）
從已刪除的 test_command_allowlist_dirs.py／test_allow_directory_runtime.py／
test_allow_directory_regression.py／test_extra_commands.py 移植到這裡。
"""
from __future__ import annotations

import ast
import json
import os
import stat
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_tools
import client_policy
import command_allowlist
import config
import container_runner
from runtime_policy import EXTRA_BUILD_COMMANDS

pytestmark = pytest.mark.smoke

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def runtime(monkeypatch):
    # run_command 讀的幾個開關不得漏到別的測試。
    for name, value in vars(config).copy().items():
        if name.isupper():
            monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, "ALLOWED_COMMANDS", list(config.ALLOWED_COMMANDS))
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", True)
    monkeypatch.setattr(container_runner, "CONTAINER_ENABLED", False)


def _tool(directory: Path, name: str = "probe", data: bytes = b"#!/bin/sh\nexit 0\n",
          mode: int = 0o755) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(data)
    path.chmod(mode)
    return path


def _script(directory: Path, name: str = "probe", marker: str = "project-probe") -> Path:
    return _tool(directory, name, f"#!/bin/sh\nprintf '%s\\n' '{marker}:'\"$1\"\n".encode())


def _elf(bits=64, endian="<", e_type=2, interp=False):
    """Minimal verifiable ELF headers; the fixtures are never executed."""
    is64 = bits == 64
    header_size, ph_size = (64, 56) if is64 else (52, 32)
    interp_bytes = b"/lib/fixture-loader\x00" if interp else b""
    phnum = 2 if interp else 1
    total = header_size + ph_size * phnum + len(interp_bytes)
    ident = b"\x7fELF" + bytes((2 if is64 else 1, 1 if endian == "<" else 2, 1)) + bytes(9)
    header = struct.pack(
        endian + ("HHIQQQIHHHHHH" if is64 else "HHIIIIIHHHHHH"),
        e_type, 62 if is64 else 3, 1, 0, header_size, 0, 0, header_size,
        ph_size, phnum, 0, 0, 0,
    )
    if is64:
        load = struct.pack(endian + "IIQQQQQQ", 1, 5, 0, 0, 0, total, total, 4096)
        interpreter = struct.pack(endian + "IIQQQQQQ", 3, 4, header_size + ph_size * phnum,
                                  0, 0, len(interp_bytes), len(interp_bytes), 1)
    else:
        load = struct.pack(endian + "IIIIIIII", 1, 0, 0, 0, total, total, 5, 4096)
        interpreter = struct.pack(endian + "IIIIIIII", 3, header_size + ph_size * phnum,
                                  0, 0, len(interp_bytes), len(interp_bytes), 4, 1)
    return ident + header + load + (interpreter if interp else b"") + interp_bytes


def _masked_metadata(monkeypatch, path, **changes):
    """Model foreign ownership without needing root or changing the real owner."""
    target = path.stat()
    original_stat, original_fstat = os.stat, os.fstat

    class ChangedStat:
        def __init__(self, info):
            self.info = info

        def __getattr__(self, name):
            return changes[name] if name in changes else getattr(self.info, name)

    def alter(info):
        return ChangedStat(info) if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino) else info

    def masked_stat(*args, **kwargs):
        return alter(original_stat(*args, **kwargs))

    def masked_fstat(*args, **kwargs):
        return alter(original_fstat(*args, **kwargs))

    monkeypatch.setattr(os, "stat", masked_stat)
    monkeypatch.setattr(os, "fstat", masked_fstat)


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def _recording_run(monkeypatch, *, real: bool = False):
    calls: list[tuple[list[str], dict]] = []
    original = agent_tools.process_env.run

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        if real:
            return original(argv, **kwargs)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(agent_tools.process_env, "run", run)
    return calls


def _no_spawn(monkeypatch):
    monkeypatch.setattr(agent_tools.process_env, "run",
                        lambda *a, **k: pytest.fail("rejected command must never spawn"))


def test_project_executable_resolves_inside_root_and_runs_by_validated_path(
    tmp_path, monkeypatch, runtime,
):
    root = _project(tmp_path)
    (root / "tools").mkdir(mode=0o775)  # group-write 不影響信任(同今天的目錄規則)
    tool = _script(root / "tools")
    calls = _recording_run(monkeypatch, real=True)
    executor = agent_tools.ToolExecutor(str(root))
    expected = str(executor.root / "tools" / "probe")
    assert tool.resolve() == Path(expected)
    for command in (
        "tools/probe --version", "./tools/probe --version", f"{expected} --version",
        "tools/./probe --version", "tools//probe --version",
    ):
        out = executor.run_command(command)
        assert out.startswith("=== ✓ 成功") and "project-probe:--version" in out, (command, out)
    assert [argv for argv, _ in calls] == [[expected, "--version"]] * 5
    assert all(kwargs["shell"] is False and kwargs["cwd"] == str(executor.root)
               and kwargs["overrides"] == {"PYTHONIOENCODING": "utf-8"} for _, kwargs in calls)
    # 驗證器只看檔頭分類,ELF fixtures 不會被執行。
    accepted = []
    for bits in (32, 64):
        for endian, label in (("<", "little"), (">", "big")):
            for kind, e_type, interp in (("exec", 2, False), ("pie", 3, True)):
                name = f"elf{bits}-{label}-{kind}"
                _tool(root / "bin", name, _elf(bits, endian, e_type, interp))
                accepted.append(name)
    _tool(root / "bin", "script.js", b"#!/usr/bin/node\nprint('fixture')\n")
    for name in (*accepted, "script.js"):
        ok, message, parts = executor._validate_command(f"bin/{name} -x")
        assert ok, (name, message)
        assert parts == [str(executor.root / "bin" / name), "-x"]
    # 內建白名單的裸名照舊,不經專案驗證器、不改 argv。
    assert executor._validate_command("pytest --version")[2] == ["pytest", "--version"]
    assert not hasattr(config, "EXTRA_ALLOWED_COMMANDS")
    assert not hasattr(config, "EXTRA_ALLOWED_COMMAND_DIRS")


def test_rejected_project_paths_never_touch_the_filesystem(tmp_path, monkeypatch, runtime):
    """P1:純字串就能判定的拒絕,一律在任何 stat／open／resolve 之前完成。"""
    root = _project(tmp_path)
    _script(root / "tools")
    executor = agent_tools.ToolExecutor(str(root))
    base = str(executor.root)
    assert command_allowlist.project_executable_candidate(base, "tools//./probe") == f"{base}/tools/probe"
    assert command_allowlist.project_executable_candidate(base, f"{base}/tools/probe") == f"{base}/tools/probe"

    touched: list[str] = []

    def forbidden(name):
        def _forbidden(*_args, **_kwargs):
            touched.append(name)
            raise AssertionError(f"filesystem access via {name}")
        return _forbidden

    rejected = {
        "/outside/tool --x": "不在專案內",
        "../outside/tool": "..",
        "tools/../../outside/tool": "..",
        "tools/../probe": "..",
        "~/bin/tool": "~",
        "~root/bin/tool": "~",
        "tools/\x01probe": "控制字元",
        "tools/sub/": "/ 結尾",
        f"{tmp_path}/elsewhere/tool": "不在專案內",
        f"{base}/": "/ 結尾",
        f"{base}/.": "不在專案內",
        "tools/" + "p" * 5000: "至多",
        "tools/probe x | head": "不允許的字元",
        "tools/probe $(whoami)": "不允許的字元",
    }
    results = {}
    with monkeypatch.context() as guard:
        for name in ("stat", "lstat", "open", "scandir", "listdir", "readlink", "access"):
            guard.setattr(os, name, forbidden(f"os.{name}"))
        guard.setattr(os.path, "realpath", forbidden("os.path.realpath"))
        guard.setattr(os.path, "exists", forbidden("os.path.exists"))
        for name in ("resolve", "expanduser", "exists", "is_dir", "is_file", "stat", "lstat"):
            guard.setattr(Path, name, forbidden(f"Path.{name}"))
        guard.setattr(agent_tools.process_env, "run", forbidden("process_env.run"))
        for command in rejected:
            results[command] = executor.run_command(command)
    assert touched == []
    for command, fragment in rejected.items():
        assert results[command].startswith("錯誤"), (command, results[command])
        assert fragment in results[command], (command, results[command])
    assert agent_tools.PROJECT_TOOL_HINT in results["/outside/tool --x"]
    assert "不經 shell" in results["tools/probe x | head"]


@pytest.mark.parametrize("case", ["directory_component", "leaf", "leaf_outside"])
def test_project_executable_never_follows_symlinks(tmp_path, monkeypatch, runtime, case):
    root = _project(tmp_path)
    tool = _script(root / "real" / "bin")
    if case == "directory_component":
        (root / "linked").symlink_to(root / "real", target_is_directory=True)
        command = "linked/bin/probe"
    elif case == "leaf":
        (root / "real" / "bin" / "alias").symlink_to(tool)
        command = "real/bin/alias"
    else:
        outside = _script(tmp_path / "outside-tools", marker="outside")
        (root / "real" / "bin" / "outside").symlink_to(outside)
        command = "real/bin/outside"
    _no_spawn(monkeypatch)
    executor = agent_tools.ToolExecutor(str(root))
    out = executor.run_command(command)
    assert out.startswith("錯誤: 專案內工具不可執行"), out
    assert "symlink" in out, out
    with pytest.raises(command_allowlist.ProjectCommandError, match="symlink"):
        command_allowlist.resolve_project_executable(executor.root, command)


def test_project_executable_enforces_owner_mode_and_format(tmp_path, monkeypatch, runtime):
    root = _project(tmp_path)
    tools = root / "tools"
    _tool(tools)
    malformed = [b"\x7fELF", _elf()[:63], _elf()[:-1], _elf(e_type=1)]
    for offset, value in ((4, 0), (5, 0), (6, 0)):
        data = bytearray(_elf())
        data[offset] = value
        malformed.append(bytes(data))
    for offset, fmt, value in (
        (20, "I", 0), (32, "Q", 65536), (52, "H", 63),
        (54, "H", 1), (56, "H", 0), (56, "H", 65535),
    ):
        data = bytearray(_elf())
        struct.pack_into("<" + fmt, data, offset, value)
        malformed.append(bytes(data))
    oversized_table = bytearray(_elf())
    struct.pack_into("<Q", oversized_table, 32, 65536)
    oversized_table.extend(bytes(65536))
    malformed.append(bytes(oversized_table))
    for index, data in enumerate(malformed):
        _tool(tools, f"malformed-{index}", data)
    cases = {
        "not-executable": ("#!/bin/sh\nexit 0\n", 0o644, "缺 owner execute"),
        "world-write": ("#!/bin/sh\nexit 0\n", 0o757, "world-writable"),
        "data-file": ("plain text\n", 0o755, "非 ELF executable 或 shebang"),
    }
    for name, (content, mode, _) in cases.items():
        _tool(tools, name, content.encode(), mode)
    _tool(tools, "libarc.so", _elf(e_type=3))
    (tools / "nested").mkdir()
    os.mkfifo(tools / "pipe", mode=0o755)
    _no_spawn(monkeypatch)
    executor = agent_tools.ToolExecutor(str(root))
    assert executor._validate_command("tools/probe")[0]
    for name, (_content, _mode, reason) in cases.items():
        ok, message, _ = executor._validate_command(f"tools/{name}")
        assert not ok and reason in message, (name, message)
    for name, reason in (("libarc.so", "PT_INTERP"), ("nested", "是目錄"), ("pipe", "不是普通檔")):
        ok, message, _ = executor._validate_command(f"tools/{name}")
        assert not ok and reason in message, (name, message)
    for index in range(len(malformed)):
        ok, message, _ = executor._validate_command(f"tools/malformed-{index}")
        assert not ok and "ELF" in message, (index, message)

    # owner／world-writable 邊界:同今天的目錄規則(中間層 root 或本人、root-owned sticky 例外;
    # 最終目錄與檔案必須是本人、不得 world-writable)。
    with monkeypatch.context() as masked:
        _masked_metadata(masked, tools / "probe", st_uid=os.getuid() + 12345)
        ok, message, _ = executor._validate_command("tools/probe")
        assert not ok and "不是目前使用者擁有" in message, message
    for target, changes in (
        (tools, {"st_uid": os.getuid() + 12345}),
        (tools, {"st_mode": stat.S_IFDIR | 0o1777}),
        (root, {"st_uid": os.getuid() + 12345}),
        (root, {"st_mode": stat.S_IFDIR | 0o777}),
        (root, {"st_mode": stat.S_IFDIR | 0o1777, "st_uid": os.getuid() or 12345}),
    ):
        with monkeypatch.context() as masked:
            _masked_metadata(masked, target, **changes)
            ok, message, _ = executor._validate_command("tools/probe")
            assert not ok and "專案內工具不可執行" in message, (target, changes, message)
    with monkeypatch.context() as masked:
        _masked_metadata(masked, root, st_uid=0, st_mode=stat.S_IFDIR | 0o1777)
        assert executor._validate_command("tools/probe")[0]


def test_project_executable_rejects_reserved_builtin_and_build_names(tmp_path, monkeypatch, runtime):
    root = _project(tmp_path)
    forbidden = set(command_allowlist._RESERVED_EXECUTABLES) | {"git"}
    forbidden.update(prefix.split()[0] for prefix in config.ALLOWED_COMMANDS)
    forbidden.update(prefix.split()[0] for prefix in EXTRA_BUILD_COMMANDS)
    assert {"bash", "sh", "env", "python3", "python", "pytest", "make", "npm", "git"} <= forbidden
    for name in forbidden:
        _tool(root / "tools", name)
    for name in ("bad name", "-option", ".hidden"):
        _tool(root / "tools", name)
    _no_spawn(monkeypatch)
    executor = agent_tools.ToolExecutor(str(root))
    for name in sorted(forbidden):
        ok, message, _ = executor._validate_command(f"tools/{name} --version")
        assert not ok and "同名" in message, (name, message)
    for name in ("bad name", "-option", ".hidden"):
        ok, message, _ = executor._validate_command(f"'tools/{name}'")
        assert not ok and "檔名" in message, (name, message)


@pytest.mark.parametrize("change", [
    "header_budget", "fifo_swap", "replace_file", "modify_file", "replace_directory", "io_error",
])
def test_project_executable_header_read_is_bounded_and_fifo_or_drift_fails_closed(
    tmp_path, monkeypatch, runtime, change,
):
    root = _project(tmp_path)
    tools = root / "tools"
    if change == "header_budget":
        tool = _tool(tools, data=b"#!/bin/sh\n" + b"x" * (256 * 1024))
    else:
        tool = _tool(tools)
    executor = agent_tools.ToolExecutor(str(root))
    _no_spawn(monkeypatch)
    original_read, original_open = os.read, os.open
    sizes, opened = [], []
    changed = False

    def counted_read(fd, size):
        assert size <= 64 * 1024
        data = original_read(fd, size)
        sizes.append(len(data))
        return data

    def changed_read(fd, size):
        nonlocal changed
        if not changed:
            changed = True
            if change == "replace_file":
                replacement = _tool(tmp_path, "replacement")
                replacement.replace(tool)
            elif change == "modify_file":
                tool.write_bytes(b"#!/bin/sh\nchanged content\n")
            elif change == "replace_directory":
                tools.rename(tmp_path / "detached")
                tools.mkdir()
                _tool(tools)
            else:
                raise OSError("fixture read failure")
        return original_read(fd, size)

    def swapped_open(path, flags, *args, **kwargs):
        if path == tool.name and "dir_fd" in kwargs:
            assert flags & os.O_NOFOLLOW
            assert flags & os.O_NONBLOCK
            tool.unlink()
            os.mkfifo(tool, mode=0o755)
            result = original_open(path, flags, *args, **kwargs)
            opened.append(result)
            return result
        return original_open(path, flags, *args, **kwargs)

    if change == "header_budget":
        monkeypatch.setattr(os, "read", counted_read)
        ok, message, parts = executor._validate_command("tools/probe")
        assert ok, message
        assert parts == [str(executor.root / "tools" / "probe")]
        assert sum(sizes) == 64 * 1024
        return
    if change == "fifo_swap":
        monkeypatch.setattr(os, "open", swapped_open)
    else:
        monkeypatch.setattr(os, "read", changed_read)
    out = executor.run_command("tools/probe")
    assert out.startswith("錯誤: 專案內工具不可執行"), out
    if change == "fifo_swap":
        assert "身分變動" in out
        assert len(opened) == 1
        with pytest.raises(OSError):
            os.fstat(opened[0])
    else:
        assert changed
        if change == "io_error":
            assert "fixture read failure" in out


@pytest.mark.parametrize("capability", [
    "O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK", "supports_dir_fd", "supports_fd",
    "supports_follow_symlinks", "getuid",
])
def test_project_executable_missing_safety_capability_fails_before_open(
    tmp_path, monkeypatch, runtime, capability,
):
    root = _project(tmp_path)
    _tool(root / "tools")
    executor = agent_tools.ToolExecutor(str(root))
    _no_spawn(monkeypatch)
    monkeypatch.setattr(os, capability, set() if capability.startswith("supports_") else None)

    def forbidden_open(*_args, **_kwargs):
        pytest.fail("missing safety primitives must be rejected before filesystem IO")

    monkeypatch.setattr(os, "open", forbidden_open)
    out = executor.run_command("tools/probe")
    assert out.startswith("錯誤: 專案內工具不可執行"), out
    assert "安全檢查需要" in out


def test_project_command_keeps_shell_containment_timeout_and_disabled_gates(
    tmp_path, monkeypatch, runtime,
):
    root = _project(tmp_path)
    tool = _tool(root / "tools")
    executor = agent_tools.ToolExecutor(str(root))
    calls = _recording_run(monkeypatch)
    for command in (
        "tools/probe x; echo bad", "tools/probe $(whoami)", "tools/probe `whoami`",
        "tools/probe x | cat", "tools/probe x > output", "tools/probe x && y",
        "tools/probe x || y", "tools/probe < input",
    ):
        out = executor.run_command(command)
        assert "不允許的字元" in out and "不經 shell" in out, (command, out)
    for command in (
        "tools/probe ../outside.elf", "tools/probe --config=/outside.ini",
        "tools/probe -f /outside.elf", "tools/probe /etc/passwd",
    ):
        out = executor.run_command(command)
        assert "路徑超出 sandbox" in out, (command, out)
    assert calls == []
    assert "成功" in executor.run_command("tools/probe firmware.elf")
    assert [argv for argv, _ in calls] == [[str(executor.root / "tools" / tool.name), "firmware.elf"]]
    # 內建白名單不得持有 import 快照:每次都讀 config.ALLOWED_COMMANDS。
    monkeypatch.setattr(config, "ALLOWED_COMMANDS", ["ctest"])
    assert not executor._validate_command("pytest -h")[0]
    assert executor._validate_command("ctest -h")[0]

    def forbidden(*_args, **_kwargs):
        pytest.fail("disabled/invalid timeout must not even validate the tool")

    monkeypatch.setattr(command_allowlist, "resolve_project_executable", forbidden)
    monkeypatch.setattr(command_allowlist, "project_executable_candidate", forbidden)
    for timeout in (True, "60", 1.0, 0, 601):
        assert "timeout 必須" in executor.run_command("tools/probe", timeout=timeout)
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", False)
    assert "已停用" in executor.run_command("tools/probe")
    assert len(calls) == 1
    # 核准與 readonly 邊界不因專案內工具改變:互動要問、唯讀一律拒絕。
    arguments = {"cmd": "tools/probe"}
    assert client_policy.InteractivePolicy().decide(
        "run_command", read_only=False, arguments=arguments,
    ) is client_policy.Decision.ASK
    assert client_policy.ReadOnlyPolicy().decide(
        "run_command", read_only=False, arguments=arguments,
    ) is client_policy.Decision.DENY


def test_project_command_revalidates_every_call_without_cache(tmp_path, monkeypatch, runtime):
    root = _project(tmp_path)
    tool = _tool(root / "tools")
    executor = agent_tools.ToolExecutor(str(root))
    calls = _recording_run(monkeypatch)
    walks = []
    original_walk = command_allowlist._open_directory

    def counted_walk(path, uid):
        walks.append(path)
        return original_walk(path, uid)

    monkeypatch.setattr(command_allowlist, "_open_directory", counted_walk)
    assert "成功" in executor.run_command("tools/probe")
    first = len(walks)
    assert first >= 2  # 驗證一次＋發布前重走整條鏈
    tool.chmod(0o644)
    assert "缺 owner execute" in executor.run_command("tools/probe")
    tool.chmod(0o755)
    assert "成功" in executor.run_command("tools/probe")
    tool.unlink()
    (root / "tools" / "real").write_bytes(b"#!/bin/sh\nexit 0\n")
    (root / "tools" / "real").chmod(0o755)
    tool.symlink_to(root / "tools" / "real")
    assert "symlink" in executor.run_command("tools/probe")
    tool.unlink()
    tool.write_bytes(b"ordinary data now")
    tool.chmod(0o755)
    assert "非 ELF" in executor.run_command("tools/probe")
    tool.unlink()
    assert "找不到或無法檢查" in executor.run_command("tools/probe")
    assert len(calls) == 2
    assert len(walks) > 2 * first  # 每一次呼叫都重新走,沒有快取


def test_project_command_in_container_is_refused_without_host_fallback(tmp_path, monkeypatch, runtime):
    root = _project(tmp_path)
    _tool(root / "tools")
    monkeypatch.setattr(container_runner, "CONTAINER_ENABLED", True)
    calls = []

    def container(**kwargs):
        calls.append(kwargs)
        return {"error": "offline container"}

    monkeypatch.setattr(container_runner, "run_in_container", container)
    _no_spawn(monkeypatch)
    executor = agent_tools.ToolExecutor(str(root))
    out = executor.run_command("tools/probe")
    assert out == "錯誤: 容器模式不執行專案內工具；不會改到本機執行。"
    assert calls == []
    # 內建白名單命令在容器模式照舊交給容器;失敗不得改到 host。
    assert "offline container" in executor.run_command("pytest -q")
    assert calls == [{"command": "pytest -q", "folder": str(executor.root), "timeout": 60,
                      "network": False, "writable": False}]


def test_regression_model_path_with_pipe_gets_no_shell_hint_and_never_spawns(
    tmp_path, monkeypatch, runtime,
):
    """Reported session: the model sent `MetaWare/arc/bin/llvm-objdump ... | head` for a tool
    inside the analysed project. The path form is now the supported one; only the shell pipe is
    refused (before any filesystem access) and the reply says why."""
    root = _project(tmp_path)
    tool = _tool(root / "MetaWare" / "arc" / "bin", "llvm-objdump")
    executor = agent_tools.ToolExecutor(str(root))
    calls = _recording_run(monkeypatch)
    reported = executor.run_command("MetaWare/arc/bin/llvm-objdump -d example.elf | head -80")
    assert "'|'" in reported and "不經 shell" in reported, reported
    bare = executor.run_command("llvm-objdump -d example.elf")
    assert bare.splitlines()[0] == "錯誤: 不允許的命令。", bare
    assert "專案相對路徑" in bare
    assert "/allow" not in reported + bare and "授權" not in reported + bare
    assert calls == []
    resolved = str(executor.root / "MetaWare" / "arc" / "bin" / "llvm-objdump")
    assert tool.resolve() == Path(resolved)
    assert "成功" in executor.run_command("MetaWare/arc/bin/llvm-objdump -d example.elf")
    assert "成功" in executor.run_command(f"{resolved} -d example.elf")
    assert [argv for argv, _ in calls] == [[resolved, "-d", "example.elf"]] * 2


@pytest.mark.parametrize("readonly", [False, True])
def test_live_mcp_runs_project_tool_without_settings_and_readonly_denies(tmp_path, readonly):
    """Real stdio startup: no client.json key or command is needed; readonly still refuses."""
    import client_mcp

    settings = tmp_path / "settings"
    settings.mkdir(mode=0o700)
    path = settings / "client.json"
    path.write_text(json.dumps({"schema": 1, "compaction_mode": "manual"}), encoding="utf-8")
    path.chmod(0o600)
    root = _project(tmp_path)
    _script(root / "tools", "live-probe", marker="project-live")
    client = client_mcp.McpClient(
        root, readonly=readonly, client_config=path, n_ctx=8192,
        skip_aux_preflight=True, env={"XDG_STATE_HOME": str(tmp_path / "state")},
    )
    try:
        client.start()
        spec = next(item for item in client.tools() if item.name == "run_command")
        assert "project-relative path" in spec.description
        assert "extra_allowed" not in spec.description and "/allow" not in spec.description
        result = client.call("run_command", {"cmd": "tools/live-probe --ok"})
        if readonly:
            assert result.is_error or "已停用" in result.text or "readonly" in result.text
            assert "project-live" not in result.text
        else:
            assert not result.is_error and "project-live:--ok" in result.text, result.text
            rejected = client.call("run_command", {"cmd": "live-probe"})
            assert "不允許的命令" in rejected.text
    finally:
        client.close()


def test_run_command_descriptions_teach_project_paths_not_allow():
    import mcp_contract

    tree = ast.parse((REPO_ROOT / "mcp_server.py").read_text(encoding="utf-8"))
    fn = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_command"
    )
    surfaces = {
        "native": agent_tools._RUN_COMMAND_TOOL["function"]["description"],
        "executor": agent_tools.ToolExecutor.run_command.__doc__ or "",
        "mcp_docstring": ast.get_docstring(fn) or "",
        "model": mcp_contract.MODEL_TOOL_DESCRIPTIONS["run_command"],
        "hint": agent_tools.PROJECT_TOOL_HINT,
    }
    for name, text in surfaces.items():
        assert "/allow" not in text and "extra_allowed" not in text, name
    normalized = {name: " ".join(text.split()) for name, text in surfaces.items()}
    for name in ("native", "executor", "mcp_docstring", "hint"):
        assert "專案相對路徑" in normalized[name], name
    for name in ("native", "mcp_docstring"):
        assert "容器模式不執行專案內工具" in normalized[name], name
    assert "project-relative path" in normalized["model"]
    assert "containers never run project tools" in normalized["model"]
