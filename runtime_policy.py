"""mcp_server 啟動時的 runtime policy:啟動參數 → 開關決策。

抽成獨立模組的唯一理由是可測性:policy 本身是純函式,但它原本寫在
`mcp_server.py` 的 import 期,要驗它就得整支 server spawn 起來(每條 case
約 0.5s,而且驗的其實只是三個布林值)。閘門沒有改變——mcp_server 仍然在
啟動時套用同一份決策,只是決策本身現在可以單獨呼叫。

預設值不得改:patch 與 run_command 預設開;build 命令(make/cmake/ninja/meson/
bazel)會執行專案內的 build script,風險面比 pytest/cargo test 大,一律預設不掛白名單。

**`--readonly` 是一道硬閘,不是偏好**:評測 / 抽查 / canary 以它啟動 server,
三個開關一律關到底。以前這是四個環境變數(`AI_CODE_PATCH=0` 等),而環境變數的
問題是「殼層裡殘留的同名變數可以把它翻回來」;現在它是 argv,而且子行程的環境在
交出去之前已經被剝乾淨。`client.json` 也翻不回來 —— 它根本不參與這個決定。
"""
from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class RuntimePolicy:
    patch_enabled: bool
    run_command_enabled: bool
    build_commands_enabled: bool

    @property
    def extra_build_commands(self) -> tuple[str, ...]:
        """實際要 append 的 build 命令;沒打開就是空的。"""
        return EXTRA_BUILD_COMMANDS if self.build_commands_enabled else ()


#: readonly 模式下三個開關的值。全部關到底 —— 沒有「只關寫檔、留著執行命令」這種
#: 中間狀態:評測的契約是「前後 project state 不變」,而一條 `pytest` 就能改檔。
READONLY_POLICY = RuntimePolicy(
    patch_enabled=False, run_command_enabled=False, build_commands_enabled=False
)


def resolve_runtime_policy(
    *, readonly: bool = False, build_commands: bool = False
) -> RuntimePolicy:
    """決定這個 server 行程的三個開關。

    `readonly=True` 一律回 :data:`READONLY_POLICY`,而且**不看**任何其他輸入:
    它是評測邊界,不是可以被別的設定調和的偏好。
    """
    if readonly:
        return READONLY_POLICY
    return RuntimePolicy(
        patch_enabled=True,
        run_command_enabled=True,
        build_commands_enabled=bool(build_commands),
    )
