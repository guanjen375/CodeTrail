#!/usr/bin/env python3
"""Report or strictly verify all CodeTrail llama-server deployment roles."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deployment_profile import ProfileError, ServiceProfile, load_effective_profile  # noqa: E402
from deployment_status import (  # noqa: E402
    Inspection,
    inspect_deployment,
    query_gpu_inventory,
    query_gpu_processes,
    read_proc_cmdline,
)


def _snapshot_reader(path: Path) -> Callable[[ServiceProfile], tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"invalid AICODE_STATUS_SNAPSHOT {path}: {exc}") from exc
    if not isinstance(data, dict) or set(data) - {"main", "embedding", "reranker", "vl"}:
        raise ProfileError("AICODE_STATUS_SNAPSHOT must contain only main/embedding/reranker/vl objects")

    def read(service: ServiceProfile):
        item = data.get(service.role) or {}
        if not isinstance(item, dict) or set(item) - {"health", "props"}:
            raise ProfileError(f"status snapshot role {service.role} has invalid fields")
        health = item.get("health")
        props = item.get("props")
        if health is not None and not isinstance(health, dict):
            raise ProfileError(f"status snapshot {service.role}.health must be an object or null")
        if props is not None and not isinstance(props, dict):
            raise ProfileError(f"status snapshot {service.role}.props must be an object or null")
        return health, props

    return read


def _render(inspection: Inspection, expected_count: int) -> None:
    print("CodeTrail llama-server GPU processes (all GPUs):")
    seen: set[int] = set()
    for row in inspection.gpu_processes:
        if row.pid in seen:
            continue
        seen.add(row.pid)
        print(
            f"[GPU] PID={row.pid:<7} GPU={row.gpu_uuid} "
            f"VRAM={row.used_memory_mib} MiB process={row.process_name}"
        )

    recognized = sum(1 for observation in inspection.observations.values() if observation.pid is not None)
    for role in ("main", "embedding", "reranker", "vl"):
        observation = inspection.observations[role]
        gpu = ",".join(observation.gpu_uuids) or "unknown"
        model = Path(observation.model).name if observation.model else "unknown"
        print(
            f"[ROLE] role={role:<9} PID={observation.pid or '-':<7} GPU={gpu} "
            f"model={model} n_ctx={observation.n_ctx or 'unknown'} "
            f"health={observation.health} port={observation.port}"
        )

    unique_count = len({row.pid for row in inspection.gpu_processes})
    if unique_count >= expected_count:
        print(f"[PASS] 偵測到 {unique_count} 個不同的 llama-server PID（預期至少 {expected_count}）")
    else:
        print(
            f"[WARN] 只偵測到 {unique_count} 個不同的 llama-server PID（預期至少 {expected_count}）",
            file=sys.stderr,
        )
    if recognized == 0 and unique_count:
        print("[INFO] 無法讀取這些 PID 的 cmdline；保留舊版 PID-count 報告，但 strict 會視為缺少 role")
    for warning in inspection.warnings:
        print(f"[WARN] {warning}", file=sys.stderr)
    for issue in inspection.issues:
        print(f"[FAIL] {issue}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Identify and verify main/embedding/reranker/VL llama-server processes"
    )
    parser.add_argument("--strict", action="store_true", help="fail on missing, unhealthy, wrong-GPU, or wrong-model roles")
    parser.add_argument("--profile", help="profile name or absolute JSON profile path")
    parser.add_argument("--no-network", action="store_true", help="skip /health and /props requests")
    args = parser.parse_args(argv)
    try:
        expected_raw = os.environ.get("EXPECTED_LLAMA_SERVERS") or "4"
        if not expected_raw.isdecimal() or int(expected_raw) < 1:
            raise ProfileError("EXPECTED_LLAMA_SERVERS must be a positive integer")
        expected = int(expected_raw)
        profile = load_effective_profile(profile=args.profile)
        processes, gpu_error = query_gpu_processes()
        proc_root = Path(os.environ.get("AICODE_STATUS_PROC_ROOT") or "/proc")
        snapshot = (os.environ.get("AICODE_STATUS_SNAPSHOT") or "").strip()
        server_reader = None
        if not args.no_network:
            server_reader = _snapshot_reader(Path(snapshot)) if snapshot else None
            if not snapshot:
                from deployment_status import query_server

                server_reader = query_server
        inventory = query_gpu_inventory() if any(service.gpu for service in profile.services.values()) else {}
        inspection = inspect_deployment(
            profile,
            processes,
            environ=os.environ,
            cmdline_reader=lambda pid: read_proc_cmdline(pid, proc_root),
            server_reader=server_reader,
            gpu_inventory=inventory,
        )
        if gpu_error:
            print(f"[FAIL] nvidia-smi: {gpu_error}", file=sys.stderr)
        print(
            f"[PROFILE] name={profile.selected_profile} verification={profile.verification} "
            f"hardware={profile.hardware}"
        )
        _render(inspection, expected)
        failed = bool(gpu_error or inspection.issues or len({row.pid for row in processes}) < expected)
        if args.strict:
            return 1 if failed else 0
        print("[INFO] report-only mode: exit 0；自動化檢查請使用 --strict")
        return 0
    except ProfileError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        if args.strict:
            return 2
        print("[INFO] report-only mode: exit 0；自動化檢查請使用 --strict")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
