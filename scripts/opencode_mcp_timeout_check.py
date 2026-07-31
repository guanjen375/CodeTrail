#!/usr/bin/env python3
"""Check or repair the OpenCode timeout used for CodeTrail MCP calls.

``aicode`` invokes this script with ``--fix`` before starting OpenCode.  The
repair is deliberately narrow: it only changes ``mcp.codetrail.timeout`` in an
existing CodeTrail MCP entry, preserves every other JSON setting, writes the
replacement atomically, and keeps a backup of the previous file.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402

SKIP_ENV = "AICODE_MCP_TIMEOUT_CHECK_SKIP"
BACKUP_SUFFIX = ".codetrail.bak"


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


def _read_config(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return None, "OpenCode config root 必須是 JSON object"
    return data, None


def _codetrail_entry(data: dict[str, Any]) -> dict[str, Any] | None:
    mcp = data.get("mcp")
    entry = mcp.get("codetrail") if isinstance(mcp, dict) else None
    return entry if isinstance(entry, dict) else None


def read_codetrail_timeout(path: Path) -> tuple[bool, int | None, str | None]:
    """Return (codetrail entry present, timeout, error)."""
    data, error = _read_config(path)
    if error:
        return False, None, error
    if data is None:
        return False, None, None
    entry = _codetrail_entry(data)
    if entry is None:
        return False, None, None
    timeout = entry.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        return True, None, "mcp.codetrail.timeout 必須是整數毫秒"
    return True, timeout, None


def _next_backup_path(path: Path) -> Path:
    first = path.with_name(path.name + BACKUP_SUFFIX)
    if not first.exists():
        return first
    index = 1
    while True:
        candidate = path.with_name(path.name + BACKUP_SUFFIX + f".{index}")
        if not candidate.exists():
            return candidate
        index += 1


def write_codetrail_timeout(
    path: Path,
    data: dict[str, Any],
    timeout: int,
) -> tuple[Path, Path]:
    """Atomically update one field and return ``(target, backup)``.

    Resolve symlinks before replacing so an ``OPENCODE_CONFIG`` symlink remains
    a symlink instead of being replaced by a regular file.
    """
    target = path.resolve(strict=True)
    entry = _codetrail_entry(data)
    if entry is None:
        raise ValueError("找不到既有的 mcp.codetrail object")
    entry["timeout"] = timeout

    backup = _next_backup_path(target)
    shutil.copy2(target, backup)

    original_mode = stat.S_IMODE(target.stat().st_mode)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.codetrail-",
        suffix=".tmp",
        dir=target.parent,
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, original_mode)
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return target, backup


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check or repair mcp.codetrail.timeout in OpenCode config."
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="atomically raise a missing, invalid, or too-short timeout",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args([] if argv is None else argv)
    if _truthy(os.environ.get(SKIP_ENV)):
        _print(f"skipped via {SKIP_ENV}=1")
        return 0

    path = resolve_config_path(dict(os.environ))
    if path is None:
        _print("UNKNOWN: 無法定位 opencode.json，跳過檢查")
        return 0

    data, error = _read_config(path)
    if error:
        _print(f"INVALID: {path}: {error}")
        return 2
    if data is None:
        _print(f"UNKNOWN: {path} 不存在，跳過檢查")
        return 0

    entry = _codetrail_entry(data)
    if entry is None:
        _print(f"UNKNOWN: {path} 沒有 mcp.codetrail 設定，跳過檢查")
        return 0

    timeout = entry.get("timeout")
    valid_timeout = not isinstance(timeout, bool) and isinstance(timeout, int)
    minimum = config.OPENCODE_MCP_TIMEOUT_MIN_MS
    if valid_timeout and timeout >= minimum:
        _print(f"SAFE: timeout={timeout} ms >= {minimum} ms ({path})")
        return 0

    if args.fix:
        previous = repr(timeout) if "timeout" in entry else "<missing>"
        try:
            target, backup = write_codetrail_timeout(path, data, minimum)
        except (OSError, ValueError) as exc:
            _print(f"FIX_FAILED: {path}: {type(exc).__name__}: {exc}")
            return 2
        _print(f"FIXED: timeout={previous} -> {minimum} ms ({target})")
        _print(f"       原設定備份: {backup}")
        return 0

    if valid_timeout:
        _print(f"TOO_SHORT: timeout={timeout} ms < {minimum} ms ({path})")
    else:
        _print(f"INVALID: timeout={timeout!r}，必須是 >= {minimum} 的整數毫秒 ({path})")
    _print("           圖片 VL 通常超過 10 秒；ingest_document 最長可到 10 分鐘。")
    _print(f"           請執行此腳本的 --fix，或把 mcp.codetrail.timeout 改成 {minimum}。")
    _print(f"           緊急跳過（不建議）: {SKIP_ENV}=1 aicode")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
