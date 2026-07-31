#!/usr/bin/env python3
"""Preflight: OpenCode must not cancel CodeTrail MCP tools prematurely."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402

SKIP_ENV = "AICODE_MCP_TIMEOUT_CHECK_SKIP"


def _print(line: str) -> None:
    print(f"[mcp-timeout] {line}", flush=True)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def resolve_config_path(env: dict[str, str]) -> Path | None:
    explicit = (env.get("OPENCODE_CONFIG") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = (env.get("HOME") or env.get("USERPROFILE") or "").strip()
    if not home:
        return None
    return Path(home).expanduser() / ".config" / "opencode" / "opencode.json"


def read_codetrail_timeout(path: Path) -> tuple[bool, int | None, str | None]:
    """Return (codetrail entry present, timeout, error)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, None, None
    except (OSError, json.JSONDecodeError) as exc:
        return False, None, f"{type(exc).__name__}: {exc}"

    mcp = data.get("mcp") if isinstance(data, dict) else None
    entry = mcp.get("codetrail") if isinstance(mcp, dict) else None
    if not isinstance(entry, dict):
        return False, None, None
    timeout = entry.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        return True, None, "mcp.codetrail.timeout 必須是整數毫秒"
    return True, timeout, None


def main() -> int:
    if _truthy(os.environ.get(SKIP_ENV)):
        _print(f"skipped via {SKIP_ENV}=1")
        return 0

    path = resolve_config_path(dict(os.environ))
    if path is None:
        _print("UNKNOWN: 無法定位 opencode.json，跳過檢查")
        return 0

    present, timeout, error = read_codetrail_timeout(path)
    if error:
        _print(f"INVALID: {path}: {error}")
        return 2
    if not present:
        _print(f"UNKNOWN: {path} 沒有 mcp.codetrail 設定，跳過檢查")
        return 0

    minimum = config.OPENCODE_MCP_TIMEOUT_MIN_MS
    if timeout is not None and timeout >= minimum:
        _print(f"SAFE: timeout={timeout} ms >= {minimum} ms ({path})")
        return 0

    _print(f"TOO_SHORT: timeout={timeout} ms < {minimum} ms ({path})")
    _print("           圖片 VL 通常超過 10 秒；ingest_document 最長可到 10 分鐘。")
    _print(f"           請把 mcp.codetrail.timeout 改成 {minimum}，完全退出後重開 aicode。")
    _print(f"           緊急跳過（不建議）: {SKIP_ENV}=1 aicode")
    return 2


if __name__ == "__main__":
    sys.exit(main())
