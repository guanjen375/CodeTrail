"""Validate user-authorized executable names without widening reserved commands."""
from __future__ import annotations

import re

import config
from runtime_policy import EXTRA_BUILD_COMMANDS


MAX_EXTRA_COMMANDS = 128
MAX_COMMAND_NAME_CHARS = 128
_EXECUTABLE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*")
_RESERVED_EXECUTABLES = frozenset({
    "rm", "sudo", "curl", "bash", "sh", "dash", "zsh", "fish", "ksh", "csh",
    "tcsh", "env", "xargs", "exec", "command", "eval", "source", "busybox",
    "python3", "node", "perl", "ruby",
})


def validate_extra_allowed_commands(value: object) -> list[str]:
    """Return a fresh validated list; reject paths, shell syntax and reserved roots.

    Names need not be installed yet. This only validates authorization; execution
    still resolves the exact executable through PATH with the existing safeguards.
    """
    if not isinstance(value, list):
        raise ValueError("extra_allowed_commands 必須是 executable 名稱的字串陣列")
    if len(value) > MAX_EXTRA_COMMANDS:
        raise ValueError(f"extra_allowed_commands 至多 {MAX_EXTRA_COMMANDS} 項")

    build_roots = {command.split()[0] for command in EXTRA_BUILD_COMMANDS}
    builtin_roots = {command.split()[0] for command in config.ALLOWED_COMMANDS}
    result: list[str] = []
    seen: set[str] = set()
    for index, name in enumerate(value):
        label = f"extra_allowed_commands[{index}]={name!r}"
        if not isinstance(name, str):
            raise ValueError(f"{label} 必須是字串")
        if len(name) > MAX_COMMAND_NAME_CHARS:
            raise ValueError(f"{label} 至多 {MAX_COMMAND_NAME_CHARS} 字元")
        if not _EXECUTABLE_NAME.fullmatch(name):
            raise ValueError(
                f"{label} 必須是裸 executable 名稱 "
                "([A-Za-z0-9_][A-Za-z0-9_.+-]*),不可含路徑、空白或 shell 字元"
            )
        if name == "git":
            raise ValueError(f"{label} 是保留命令;請使用專用 Git 工具")
        if name in _RESERVED_EXECUTABLES:
            raise ValueError(f"{label} 是禁止擴大的保留命令或通用執行器")
        if name in build_roots:
            raise ValueError(f"{label} 是 build 命令;請使用 build_commands 設定")
        if name in builtin_roots:
            raise ValueError(f"{label} 已由內建白名單管理,不可擴大其參數範圍")
        if name in seen:
            raise ValueError(f"{label} 重複;每個 executable 名稱只能列一次")
        seen.add(name)
        result.append(name)
    return result
