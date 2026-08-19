"""mcp_server 啟動時的 runtime policy:env → 開關決策。

抽成獨立模組的唯一理由是可測性:policy 本身是純函式,但它原本寫在
`mcp_server.py` 的 import 期,要驗它就得整支 server spawn 起來(每條 case
約 0.5s,而且驗的其實只是三個布林值)。閘門沒有改變——mcp_server 仍然在
啟動時套用同一份決策,只是決策本身現在可以單獨呼叫。

預設值不得改:`AI_CODE_PATCH` / `AI_CODE_RUN_TESTS` 預設開但尊重顯式關閉;
build 命令(make/cmake/ninja/meson/bazel)會執行專案內的 build script,
風險面比 pytest/cargo test 大,一律預設不掛白名單。
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

TRUTHY = ("1", "true", "yes")
FALSY = ("0", "false", "no")

# 打開 AI_CODE_ENABLE_BUILD_COMMANDS=1 時才 append 到 config.ALLOWED_COMMANDS。
EXTRA_BUILD_COMMANDS: tuple[str, ...] = (
    "make",
    "cmake",
    "cmake --build",
    "ninja",
    "meson",
    "meson setup",
    "meson compile",
    "bazel build",
)


def env_bool(name: str, default: bool, env: Mapping[str, str] | None = None) -> bool:
    """讀 env 的三態布林:明確 true/false 就照做,其餘(含未設)用 default。"""
    source = os.environ if env is None else env
    raw = (source.get(name) or "").lower()
    if raw in TRUTHY:
        return True
    if raw in FALSY:
        return False
    return default


@dataclass(frozen=True)
class RuntimePolicy:
    patch_enabled: bool
    run_command_enabled: bool
    build_commands_enabled: bool

    @property
    def extra_build_commands(self) -> tuple[str, ...]:
        """實際要 append 的 build 命令;沒打開就是空的。"""
        return EXTRA_BUILD_COMMANDS if self.build_commands_enabled else ()


def resolve_runtime_policy(env: Mapping[str, str] | None = None) -> RuntimePolicy:
    return RuntimePolicy(
        patch_enabled=env_bool("AI_CODE_PATCH", default=True, env=env),
        run_command_enabled=env_bool("AI_CODE_RUN_TESTS", default=True, env=env),
        build_commands_enabled=env_bool(
            "AI_CODE_ENABLE_BUILD_COMMANDS", default=False, env=env
        ),
    )
