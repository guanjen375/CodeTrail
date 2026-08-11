#!/usr/bin/env python3
"""Check or safely sync OpenCode limit.context to the main server n_ctx.

``aicode`` invokes this script with ``--fix``.  The repair changes only the
active local model's ``limit.context``, preserves every other JSON setting,
writes atomically, and leaves a backup.  Check-only mode remains available to
doctor/tests and never mutates the config.
"""
from __future__ import annotations

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

import opencode_context  # noqa: E402

BACKUP_SUFFIX = ".codetrail.bak"


def _print(line: str) -> None:
    print(f"[ctx-align] {line}", flush=True)


def _truthy(value: str) -> bool:
    return value.lower() in ("1", "true", "yes")


def _split_args(argv: list[str]) -> tuple[bool, list[str]]:
    """Consume our private --fix flag and preserve all OpenCode arguments."""
    if argv and argv[0] == "--fix":
        return True, argv[1:]
    return False, list(argv)


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


def _read_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("OpenCode config root 必須是 JSON object")
    return data


def write_active_context_limit(
    resolved: opencode_context.OpenCodeContextLimit,
    requested: int,
) -> tuple[Path, Path]:
    """Atomically update the already-resolved active model and return target/backup."""
    if resolved.path is None or not resolved.provider_key or not resolved.model_id:
        raise ValueError("無法唯一定位 active model 的 provider/model entry")

    # Preserve an OPENCODE_CONFIG symlink rather than replacing the link itself.
    target = resolved.path.resolve(strict=True)
    data = _read_config(target)
    providers = data.get("provider")
    provider = providers.get(resolved.provider_key) if isinstance(providers, dict) else None
    models = provider.get("models") if isinstance(provider, dict) else None
    spec = models.get(resolved.model_id) if isinstance(models, dict) else None
    if not isinstance(spec, dict):
        raise ValueError("active model entry 在寫入前消失或型別不正確")
    limit = spec.get("limit")
    if limit is None:
        limit = {}
        spec["limit"] = limit
    if not isinstance(limit, dict):
        raise ValueError("active model 的 limit 必須是 JSON object")
    limit["context"] = requested

    backup = _next_backup_path(target)
    shutil.copy2(target, backup)
    original_mode = stat.S_IMODE(target.stat().st_mode)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.codetrail-ctx-",
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


def main(argv: list[str] | None = None) -> int:
    raw_args = list(argv if argv is not None else sys.argv[1:])
    fix, args = _split_args(raw_args)

    if _truthy(os.environ.get("AICODE_CTX_SAFETY_DISABLE", "")):
        _print("disabled via AICODE_CTX_SAFETY_DISABLE")
        return 0

    try:
        requested = opencode_context.n_ctx_from_env(os.environ)
    except ValueError as exc:
        _print(f"INVALID: {exc}")
        return 2 if fix else 0

    limit = opencode_context.resolve_active_opencode_context_limit(os.environ, args)
    if limit.error:
        where = f" ({limit.path})" if limit.path else ""
        _print(f"UNKNOWN: OpenCode context limit 讀取失敗{where}: {limit.error}")
        return 2 if fix else 0
    if not limit.present:
        _print("UNKNOWN: 找不到 opencode.json,跳過 OpenCode ctx 對齊檢查")
        return 0

    label = limit.raw_model or limit.model
    if limit.context == requested:
        _print(f"SAFE: OpenCode model={label} limit.context=n_ctx={requested}")
        return 0

    if _truthy(os.environ.get("AICODE_ACCEPT_CTX_RISK", "")):
        _print(
            f"MISMATCH accepted: OpenCode model={label} limit.context={limit.context!r}, "
            f"n_ctx={requested}"
        )
        return 0

    if fix:
        try:
            target, backup = write_active_context_limit(limit, requested)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            _print(f"FIX_FAILED: {limit.path}: {type(exc).__name__}: {exc}")
            return 2
        previous = "<missing>" if limit.context is None else str(limit.context)
        _print(
            f"FIXED: OpenCode model={label} limit.context={previous} -> n_ctx={requested} "
            f"({target})"
        )
        _print(f"       原設定備份: {backup}")
        return 0

    if limit.context is None:
        where = f" ({limit.path})" if limit.path else ""
        model = label or "<unknown>"
        _print(f"MISSING: OpenCode model={model}{where} 沒有 limit.context")
    else:
        _print(f"MISMATCH: OpenCode model={label} limit.context={limit.context}")

    _print(f"          主模型 n_ctx = {requested}")
    _print("          OpenCode TUI 直接打 llama-server、不經過 CodeTrail;limit.context 跟")
    _print("          n_ctx 不一致時,TUI 會在跟 CodeTrail 不同的 ctx 預算下工作")
    _print("          (太小會提早 compact、太大會被 server 截斷)。")
    _print("")
    _print("          執行本腳本 --fix 可安全同步；aicode 正常啟動時會自動執行。")
    _print("          緊急略過: AICODE_ACCEPT_CTX_RISK=1 或 AICODE_CTX_SAFETY_DISABLE=1")
    _print("")

    _print("refuse to start.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
