#!/usr/bin/env python3
"""Fail loudly when OpenCode is not using CodeTrail's direct-tool contract.

CodeTrail currently supports OpenCode 1.x native MCP tools named
``codetrail_*``.  OpenCode V2 and Code Mode have materially different config,
tool exposure, disablement, and timeout semantics, so guessing across those
contracts would make the launcher canary validate the wrong protocol.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import compaction_mode  # noqa: E402
from scripts.opencode_mcp_timeout_check import resolve_config_path  # noqa: E402

MIN_OPENCODE_VERSION = (1, 17, 0)
MAX_OPENCODE_MAJOR = 2
# 壓縮功能自己的下限:1.18.15 起摘要請求的歷史從 model messages 改成序列化
# 文字,1.18.17 起 tail_turns 預設消失、保留額上限由 8k 改 15k。同一份規則在
# 那之前拿到的是**不同語意** —— 那正是計畫禁止的「靜默退回」,所以與
# direct-tool 契約分開判,不影響其餘功能的 1.17.0 下限。
MIN_COMPACTION_VERSION = compaction_mode.MIN_COMPACTION_OPENCODE_VERSION
#: aicode 把 preflight 量到的版本用這個變數傳給 OpenCode 行程;壓縮 plugin
#: 讀它來決定要不要停用。公開的 plugin / SDK API 沒有「目前執行中版本」這個
#: 欄位(`Session.version` 是 session 建立時的),所以只能由這一端遞下去。
COMPACTION_VERSION_ENV = "AICODE_OPENCODE_VERSION"
DEFAULT_COMMAND_TIMEOUT_SECONDS = 30

_VERSION_RE = re.compile(
    r"(?<![0-9.])[vV]?(\d+)\.(\d+)\.(\d+)(?![0-9.])"
)


class DirectToolContractError(RuntimeError):
    """The active OpenCode client cannot safely expose direct CodeTrail tools."""


def parse_opencode_version(raw: str) -> tuple[int, int, int]:
    """Parse one semantic OpenCode version from CLI output.

    ``opencode --version`` has used both a bare version and a labelled form.
    Pre-release/build suffixes are harmless for the contract boundary, but
    ambiguous output containing multiple versions is rejected rather than
    selecting one silently.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise DirectToolContractError("OpenCode 版本輸出為空或不是字串")
    matches = _VERSION_RE.findall(raw.strip())
    if len(matches) != 1:
        raise DirectToolContractError(
            f"OpenCode 版本無法唯一解析：{raw.strip()!r}"
        )
    return tuple(int(part) for part in matches[0])  # type: ignore[return-value]


def _contract_guidance(reason: str) -> str:
    return (
        f"{reason}。CodeTrail 目前只支援 OpenCode >=1.17.0,<2.0.0 的 direct "
        "codetrail_* native MCP tools。OpenCode V2 改用 "
        "mcp.servers.codetrail，並且 codemode:false、disabled 與 execution "
        "timeout 的語意都不同；Code Mode 不在本規格，不能用 V1 canary "
        "靜默驗證 V2/Code Mode。請改用相容的 OpenCode 1.x 設定後重試"
    )


def _contains_config_key(value: Any, target: str) -> bool:
    """Find a lifecycle key anywhere in a JSON-like config without recursion."""
    pending = [value]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(current, Mapping):
            if target in current:
                return True
            pending.extend(current.values())
        elif isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            pending.extend(current)
    return False


def require_direct_tool_contract(
    version_raw: str,
    effective_config: Mapping[str, Any],
) -> None:
    """Validate the supported OpenCode version/config closed world.

    Presence is what matters for V2-only keys.  Treating ``false`` as safe
    would still accept a config whose shape and merge semantics belong to a
    different lifecycle contract.
    """
    if not isinstance(effective_config, Mapping):
        raise DirectToolContractError(
            _contract_guidance("OpenCode effective config 根節點不是 object")
        )
    try:
        version = parse_opencode_version(version_raw)
    except DirectToolContractError as exc:
        raise DirectToolContractError(_contract_guidance(str(exc))) from exc

    if version[0] >= MAX_OPENCODE_MAJOR:
        raise DirectToolContractError(
            _contract_guidance(
                f"OpenCode {'.'.join(map(str, version))} 是未支援的 major"
            )
        )
    if version < MIN_OPENCODE_VERSION:
        raise DirectToolContractError(
            _contract_guidance(
                f"OpenCode {'.'.join(map(str, version))} 太舊"
            )
        )

    mcp = effective_config.get("mcp")
    has_mcp_servers = "mcp.servers" in effective_config or (
        isinstance(mcp, Mapping) and "servers" in mcp
    )
    incompatible_keys: list[str] = []
    if has_mcp_servers:
        incompatible_keys.append("mcp.servers")
    if _contains_config_key(effective_config, "codemode"):
        incompatible_keys.append("codemode")
    if incompatible_keys:
        raise DirectToolContractError(
            _contract_guidance(
                "OpenCode effective config 出現不相容欄位 "
                + ", ".join(incompatible_keys)
            )
        )


def compaction_version_warning(
    version: tuple[int, int, int],
    state: Mapping[str, Any] | None,
    config_path: Path | None = None,
) -> str | None:
    """壓縮模式需要的最低版本沒到時,回一句可照做的提醒;否則 None。

    刻意**不是** FAIL:CodeTrail 其餘功能在 1.17.0 以上都正常,而且 plugin
    自己在 runtime 也會拒絕觸發。這裡只負責讓它在啟動時就看得見 —— 不然
    使用者會以為壓縮已經照新規則在跑。
    """
    if not state or state.get("mode") not in compaction_mode.PLUGIN_MODES:
        return None
    if config_path is not None and not compaction_mode.state_matches_config(
        state, config_path
    ):
        # 那份狀態描述的是另一份 opencode.json;plugin 也會因為身分不符而完全
        # 不接管,這時警告版本(還寫一筆 incident)是報一個不存在的問題。
        return None
    if tuple(version) >= tuple(MIN_COMPACTION_VERSION):
        return None
    minimum = ".".join(map(str, MIN_COMPACTION_VERSION))
    current = ".".join(map(str, version))
    return (
        f"壓縮模式 {state['mode']} 需要 OpenCode >= {minimum},目前是 {current}:"
        "壓縮語意在那之前不同,plugin 不會觸發任何壓縮。請升級 OpenCode,或"
        "重跑 ./set_config.sh --compaction-mode native"
    )


def _record_version_incident() -> None:
    """記一筆零內容的 `compaction_stopped/version_unsupported`。fail-open。"""
    try:
        import mcp_lease

        mcp_lease._record_incident(
            "compaction_stopped", detail="version_unsupported", source="server"
        )
    except Exception:  # noqa: BLE001 — preflight 不得因為診斷紀錄而失敗
        pass


def load_live_contract_inputs(
    *,
    root: Path,
    env: Mapping[str, str],
    timeout: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
) -> tuple[str, dict[str, Any]]:
    """Read OpenCode's version and merged config without starting MCP/model work."""

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                args,
                cwd=str(root),
                env=dict(env),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DirectToolContractError("找不到 opencode") from exc
        except OSError as exc:
            raise DirectToolContractError(
                f"{' '.join(args)} 無法啟動（{type(exc).__name__}）"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DirectToolContractError(
                f"{' '.join(args)} 超過 {timeout} 秒"
            ) from exc

    version_result = run(["opencode", "--version"])
    if version_result.returncode != 0:
        raise DirectToolContractError(
            f"opencode --version 失敗（exit {version_result.returncode}）"
        )
    version_raw = version_result.stdout.strip()

    config_result = run(["opencode", "debug", "config"])
    if config_result.returncode != 0:
        raise DirectToolContractError(
            f"opencode debug config 失敗（exit {config_result.returncode}）"
        )
    try:
        config = json.loads(config_result.stdout)
    except json.JSONDecodeError as exc:
        raise DirectToolContractError("opencode debug config 沒有回傳合法 JSON") from exc
    if not isinstance(config, dict):
        raise DirectToolContractError("opencode debug config 根節點不是 object")
    return version_raw, config


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="OpenCode project root; defaults to AICODE_ROOT/cwd")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_COMMAND_TIMEOUT_SECONDS,
        help="seconds allowed for each read-only OpenCode inspection",
    )
    parser.add_argument(
        "--print-version-env",
        action="store_true",
        help=(
            f"print one `{COMPACTION_VERSION_ENV}=<version>` line on stdout so the "
            "launcher can export the running OpenCode version to the compaction plugin"
        ),
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.timeout < 1 or args.timeout > 300:
        print("[direct-contract] FAIL — --timeout 必須介於 1..300", file=sys.stderr)
        return 2
    raw_root = args.root or os.environ.get("AICODE_ROOT") or os.getcwd()
    try:
        root = Path(raw_root).expanduser().resolve(strict=True)
    except (OSError, ValueError) as exc:
        print(
            f"[direct-contract] FAIL — root 無法解析：{type(exc).__name__}",
            file=sys.stderr,
        )
        return 2
    if not root.is_dir():
        print("[direct-contract] FAIL — root 不是目錄", file=sys.stderr)
        return 2

    try:
        version_raw, config = load_live_contract_inputs(
            root=root,
            env=os.environ,
            timeout=args.timeout,
        )
        require_direct_tool_contract(version_raw, config)
        version = parse_opencode_version(version_raw)
    except DirectToolContractError as exc:
        message = str(exc)
        if "direct codetrail_*" not in message:
            message = _contract_guidance(message)
        print(f"[direct-contract] FAIL — {message}", file=sys.stderr)
        return 2
    warning = compaction_version_warning(
        version, compaction_mode.load_state(), resolve_config_path(dict(os.environ))
    )
    if warning:
        print(f"[direct-contract] ⚠ WARN — {warning}", file=sys.stderr)
        # plugin 端讀不到「目前正在跑的版本」(`Session.version` 是 session
        # 建立時的版本),所以這道閘只有 preflight 做得到,incident 也由這裡寫:
        # headless 沒有 toast,doctor 的統計是唯一看得到它的地方。
        _record_version_incident()
    if args.print_version_env:
        # aicode 匯入這一行並 export,plugin 才有辦法知道**目前執行中**的版本。
        # 沒有這個變數時 plugin 不做版本判斷(直接跑 `opencode` 的 session)。
        print(f"{COMPACTION_VERSION_ENV}={'.'.join(map(str, version))}")
    print(
        "[direct-contract] PASS — OpenCode "
        f"{'.'.join(map(str, version))} direct codetrail_* contract"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
