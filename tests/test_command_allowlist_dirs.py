"""Safety contracts for bounded, fresh directory-based command authorization."""
from __future__ import annotations

import os
import stat
import struct
from pathlib import Path

import pytest

import command_allowlist as allowlist
import config
from runtime_policy import EXTRA_BUILD_COMMANDS


pytestmark = pytest.mark.smoke


def _tool(directory, name="arc-probe", data=b"#!/bin/sh\nexit 0\n", mode=0o755):
    path = directory / name
    path.write_bytes(data)
    path.chmod(mode)
    return path


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


def test_directory_syntax_normalization_never_touches_filesystem(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("authorization schema validation must not access the filesystem")

    values = ["/missing/bin/../tools/", "//other///with spaces/."]
    with monkeypatch.context() as guard:
        for name in ("open", "stat", "scandir", "mkdir"):
            guard.setattr(os, name, forbidden)
        guard.setattr(os.path, "realpath", forbidden)
        guard.setattr(Path, "resolve", forbidden)
        guard.setattr(Path, "expanduser", forbidden)
        normalized = allowlist.validate_extra_allowed_command_dirs(values)
        assert normalized == ["/missing/tools", "/other/with spaces"]
        assert normalized is not values
        for invalid in (
            None, "//tools", ("/tools",), [True], [""], ["relative"], ["~/bin"],
            ["/x\n"], ["/x\x00"], ["/x\x7f"], ["/x\x85"], ["/x\ud800"],
            ["/x", "/x/./"], ["/x", "//x"], ["/" + "x" * 4096],
            [f"/tools/{index}" for index in range(33)],
        ):
            with pytest.raises(ValueError, match="extra_allowed_command_dirs"):
                allowlist.validate_extra_allowed_command_dirs(invalid)


def test_directory_inspection_accepts_tools_and_excludes_untrusted_entries(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir(mode=0o775)
    expected = {"arc-probe", "script.js"}
    _tool(tools)
    _tool(tools, "script.js", b"#!/usr/bin/node\nprint('fixture')\n")
    for bits in (32, 64):
        for endian, label in (("<", "little"), (">", "big")):
            for kind, e_type, interp in (("exec", 2, False), ("pie", 3, True)):
                name = f"elf{bits}-{label}-{kind}"
                _tool(tools, name, _elf(bits, endian, e_type, interp))
                expected.add(name)
    for name, data in (
        ("libarc.so", _elf(e_type=3)), ("data.jar", b"PK\x03\x04fixture"),
        ("page.html", b"<html>fixture</html>"), ("style.css", b"body {}"),
        ("page.js", b"console.log('fixture')"), ("README", b"plain text"),
    ):
        _tool(tools, name, data)
    for name in ("bad name", "bad;name", "-option", ".hidden", "a" * 129):
        _tool(tools, name)
    forbidden = set(allowlist._RESERVED_EXECUTABLES) | {"git"}
    forbidden.update(prefix.split()[0] for prefix in config.ALLOWED_COMMANDS)
    forbidden.update(prefix.split()[0] for prefix in EXTRA_BUILD_COMMANDS)
    for name in forbidden:
        _tool(tools, name)
    _tool(tools, "world-write", mode=0o757)
    _tool(tools, "not-owner-executable", mode=0o654)
    (tools / "nested").mkdir()
    _tool(tools / "nested", "must-not-recurse")
    (tools / "symlink").symlink_to(tools / "arc-probe")
    os.mkfifo(tools / "pipe", mode=0o755)
    inspection = allowlist.inspect_command_directories([str(tools)])
    assert inspection.errors == {}
    assert inspection.commands == {name: str(tools / name) for name in sorted(expected)}
    excluded = inspection.excluded[str(tools)]
    assert excluded["symlink"] == 1
    assert excluded["子目錄"] == 1
    assert excluded["非普通檔"] == 1
    assert excluded["world-writable 檔案"] == 1
    assert excluded["缺 owner execute 權限"] == 1
    assert excluded["ELF shared object 缺 PT_INTERP"] == 1
    assert sum(excluded.values()) == len(list(tools.iterdir())) - len(expected)
    assert list(inspection.commands) == sorted(inspection.commands)
    assert list(excluded) == sorted(excluded)


def test_directory_inspection_rejects_malformed_or_unbounded_elf_headers(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
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
    inspection = allowlist.inspect_command_directories([str(tools)])
    assert inspection.errors == {}
    assert inspection.commands == {"arc-probe": str(tools / "arc-probe")}
    assert sum(inspection.excluded[str(tools)].values()) == len(malformed)


def test_directory_inspection_allows_group_write_and_root_owned_sticky_ancestor(tmp_path, monkeypatch):
    ancestor = tmp_path / "installation"
    ancestor.mkdir(mode=0o775)
    tools = ancestor / "bin"
    tools.mkdir(mode=0o775)
    _tool(tools)
    assert allowlist.inspect_command_directories([str(tools)]).errors == {}
    _masked_metadata(monkeypatch, ancestor, st_uid=0, st_mode=stat.S_IFDIR | 0o1777)
    inspection = allowlist.inspect_command_directories([str(tools)])
    assert inspection.errors == {}
    assert inspection.commands == {"arc-probe": str(tools / "arc-probe")}


@pytest.mark.parametrize("case", ["ancestor_owner", "final_owner", "ancestor_world", "final_world", "nonroot_sticky"])
def test_directory_inspection_enforces_owner_and_world_write_boundaries(tmp_path, monkeypatch, case):
    ancestor = tmp_path / "installation"
    ancestor.mkdir()
    tools = ancestor / "bin"
    tools.mkdir()
    _tool(tools)
    target = tools if case.startswith("final") else ancestor
    changes = {}
    if case.endswith("owner"):
        changes["st_uid"] = os.getuid() + 12345
    else:
        changes["st_mode"] = stat.S_IFDIR | (0o1777 if case in {"final_world", "nonroot_sticky"} else 0o777)
        if case == "nonroot_sticky":
            changes["st_uid"] = os.getuid() or 12345
    _masked_metadata(monkeypatch, target, **changes)
    inspection = allowlist.inspect_command_directories([str(tools)])
    assert inspection.commands == {}
    assert str(tools) in inspection.errors


def test_directory_inspection_excludes_foreign_owned_candidate(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    _tool(tools)
    foreign = _tool(tools, "foreign")
    _masked_metadata(monkeypatch, foreign, st_uid=os.getuid() + 12345)
    inspection = allowlist.inspect_command_directories([str(tools)])
    assert inspection.errors == {}
    assert inspection.commands == {"arc-probe": str(tools / "arc-probe")}
    assert inspection.excluded[str(tools)]["非目前使用者擁有"] == 1


@pytest.mark.parametrize("ancestor_link", [False, True])
def test_directory_inspection_never_follows_directory_symlinks(tmp_path, ancestor_link):
    real = tmp_path / "real"
    real.mkdir()
    tools = real / "bin"
    tools.mkdir()
    _tool(tools)
    link = tmp_path / "linked"
    link.symlink_to(real if ancestor_link else tools, target_is_directory=True)
    requested = str(link / "bin" if ancestor_link else link)
    inspection = allowlist.inspect_command_directories([requested])
    assert inspection.commands == {}
    assert "symlink" in inspection.errors[requested]


def test_directory_inspection_reports_conflicts_without_selecting_a_winner(tmp_path):
    paths = []
    for name in ("first", "second", "third"):
        tools = tmp_path / name
        tools.mkdir()
        _tool(tools)
        _tool(tools, f"unique-{name}")
        paths.append(str(tools))
    inspection = allowlist.inspect_command_directories(paths)
    assert set(inspection.errors) == set(paths)
    assert "arc-probe" not in inspection.commands
    assert all("衝突" in message for message in inspection.errors.values())
    assert list(inspection.errors) == paths
    legacy = allowlist.inspect_command_directories(paths[:1], extra_commands=["arc-probe"])
    assert "arc-probe" not in legacy.commands
    assert "legacy" in legacy.errors[paths[0]]


def test_directory_inspection_never_reuses_mapping_after_installation_becomes_invalid(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    tool = _tool(tools)
    assert allowlist.inspect_command_directories([str(tools)]).commands == {tool.name: str(tool)}
    tool.write_bytes(b"ordinary data now")
    inspection = allowlist.inspect_command_directories([str(tools)])
    assert inspection.commands == {}
    assert "沒有合格" in inspection.errors[str(tools)]
    tool.unlink()
    tools.rmdir()
    missing = allowlist.inspect_command_directories([str(tools)])
    assert missing.commands == {}
    assert str(tools) in missing.errors


def test_directory_inspection_enumeration_is_bounded_and_never_partial_success(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    for index in range(3):
        _tool(tools, f"tool-{index}")
    assert allowlist.MAX_DIRECTORY_ENTRIES == 4096
    monkeypatch.setattr(allowlist, "MAX_DIRECTORY_ENTRIES", 2)
    inspection = allowlist.inspect_command_directories([str(tools)])
    assert inspection.commands == {}
    assert "未完整檢查" in inspection.errors[str(tools)]


def test_directory_inspection_reads_at_most_the_header_budget(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    tool = _tool(tools, data=b"#!/bin/sh\n" + b"x" * (256 * 1024))
    original_read = os.read
    sizes = []

    def counted_read(fd, size):
        assert size <= 64 * 1024
        data = original_read(fd, size)
        sizes.append(len(data))
        return data

    monkeypatch.setattr(os, "read", counted_read)
    inspection = allowlist.inspect_command_directories([str(tools)])
    assert inspection.errors == {}
    assert inspection.commands == {tool.name: str(tool)}
    assert sum(sizes) == 64 * 1024


def test_directory_inspection_fifo_swap_is_nonblocking_and_fails_closed(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    tool = _tool(tools)
    original_open = os.open
    opened = []

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

    monkeypatch.setattr(os, "open", swapped_open)
    inspection = allowlist.inspect_command_directories([str(tools)])
    assert inspection.commands == {}
    assert "身分變動" in inspection.errors[str(tools)]
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.parametrize("change", ["replace_file", "modify_file", "replace_directory", "io_error"])
def test_directory_inspection_revalidates_identity_and_reports_read_failures(tmp_path, monkeypatch, change):
    tools = tmp_path / "tools"
    tools.mkdir()
    tool = _tool(tools)
    original_read = os.read
    changed = False

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

    monkeypatch.setattr(os, "read", changed_read)
    inspection = allowlist.inspect_command_directories([str(tools)])
    assert changed
    assert inspection.commands == {}
    assert str(tools) in inspection.errors
    if change == "io_error":
        assert "fixture read failure" in inspection.errors[str(tools)]


@pytest.mark.parametrize("capability", [
    "O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK", "supports_dir_fd", "supports_fd",
    "supports_follow_symlinks", "getuid",
])
def test_directory_inspection_missing_safety_capability_fails_before_open(monkeypatch, capability):
    monkeypatch.setattr(os, capability, set() if capability.startswith("supports_") else None)

    def forbidden_open(*_args, **_kwargs):
        pytest.fail("missing safety primitives must be rejected before filesystem IO")

    monkeypatch.setattr(os, "open", forbidden_open)
    inspection = allowlist.inspect_command_directories(["/missing/tools"])
    assert inspection.commands == {}
    assert "安全檢查需要" in inspection.errors["/missing/tools"]
