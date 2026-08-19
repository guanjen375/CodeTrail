#!/usr/bin/env python3
"""Offline-testable deployment inspection for CodeTrail llama-server roles."""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from deployment_profile import DeploymentProfile, ProfileError, ServiceProfile, resolve_model_reference


@dataclass(frozen=True)
class GpuProcess:
    pid: int
    process_name: str
    gpu_uuid: str
    used_memory_mib: str


@dataclass(frozen=True)
class ServiceObservation:
    role: str
    pid: int | None
    gpu_uuids: tuple[str, ...]
    model: str
    mmproj: str
    n_ctx: int | None
    health: str
    port: int
    cmdline: tuple[str, ...]


@dataclass(frozen=True)
class Inspection:
    observations: dict[str, ServiceObservation]
    gpu_processes: tuple[GpuProcess, ...]
    unassigned_pids: tuple[int, ...]
    issues: tuple[str, ...]
    warnings: tuple[str, ...]


def parse_gpu_process_csv(output: str) -> list[GpuProcess]:
    rows: list[GpuProcess] = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",", 3)]
        if len(parts) != 4 or not parts[0].isdecimal():
            continue
        process_name = parts[1]
        if not Path(process_name).name.startswith("llama-server"):
            continue
        rows.append(GpuProcess(int(parts[0]), process_name, parts[2] or "unknown", parts[3] or "unknown"))
    return rows


def query_gpu_processes(
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[list[GpuProcess], str]:
    try:
        proc = run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,gpu_uuid,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return [], str(exc)
    if proc.returncode != 0:
        return [], (proc.stderr or proc.stdout or "nvidia-smi failed").strip()
    return parse_gpu_process_csv(proc.stdout), ""


def query_gpu_inventory(
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, str]:
    try:
        proc = run(
            ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return {}
    if proc.returncode != 0:
        return {}
    result: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = [part.strip() for part in line.split(",", 1)]
        if len(parts) == 2 and parts[0].isdecimal() and parts[1]:
            result[parts[0]] = parts[1]
    return result


def read_proc_cmdline(pid: int, proc_root: Path = Path("/proc")) -> tuple[str, ...]:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ()
    return tuple(part.decode("utf-8", "replace") for part in raw.split(b"\0") if part)


def _option(args: Sequence[str], *names: str) -> str:
    for index, arg in enumerate(args):
        for name in names:
            if arg == name and index + 1 < len(args):
                return args[index + 1]
            if arg.startswith(f"{name}="):
                return arg.split("=", 1)[1]
    return ""


def role_from_cmdline(args: Sequence[str], profile: DeploymentProfile) -> str | None:
    raw_port = _option(args, "--port")
    if not raw_port.isdecimal():
        return None
    matches = [role for role, service in profile.services.items() if service.port == int(raw_port)]
    return matches[0] if len(matches) == 1 else None


def _props_n_ctx(props: Mapping[str, Any]) -> int | None:
    settings = props.get("default_generation_settings")
    raw = settings.get("n_ctx") if isinstance(settings, dict) else None
    raw = raw or props.get("n_ctx")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


class _NoRedirectHandler(HTTPRedirectHandler):
    """3xx 一律不跟隨:redirect_request 回 None → urllib 拋 HTTPError(URLError
    子類),被呼叫端的既有 except 當 unreachable。loopback llama-server 不會回
    3xx;會回的就不是我們要探測的東西。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# proxy 衛生:/health /props 是 loopback 探測,不得被環境 HTTP(S)_PROXY 帶去
# 別的 host。ProxyHandler({}) = 無視環境 proxy。
_LOCAL_PROBE_OPENER = build_opener(ProxyHandler({}), _NoRedirectHandler())


def query_server(service: ServiceProfile) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    def get(path: str) -> dict[str, Any] | None:
        try:
            with _LOCAL_PROBE_OPENER.open(f"{service.base_url}{path}", timeout=0.75) as response:  # noqa: S310
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    return get("/health"), get("/props")


def _expected_gpu_set(selector: str, inventory: Mapping[str, str]) -> set[str]:
    expected: set[str] = set()
    for item in (part.strip() for part in selector.split(",")):
        if not item:
            continue
        expected.add(inventory.get(item, item))
    return expected


def _basename(value: str) -> str:
    return Path(value).name.lower() if value else ""


def inspect_deployment(
    profile: DeploymentProfile,
    gpu_processes: Iterable[GpuProcess],
    *,
    environ: Mapping[str, str] | None = None,
    cmdline_reader: Callable[[int], Sequence[str]] = read_proc_cmdline,
    server_reader: Callable[[ServiceProfile], tuple[dict[str, Any] | None, dict[str, Any] | None]] | None = query_server,
    gpu_inventory: Mapping[str, str] | None = None,
) -> Inspection:
    env = os.environ if environ is None else environ
    inventory = {} if gpu_inventory is None else dict(gpu_inventory)
    rows = tuple(gpu_processes)
    by_pid: dict[int, list[GpuProcess]] = {}
    for row in rows:
        by_pid.setdefault(row.pid, []).append(row)

    candidates: dict[str, list[tuple[int, tuple[str, ...], tuple[str, ...]]]] = {}
    unassigned: list[int] = []
    for pid, process_rows in by_pid.items():
        args = tuple(cmdline_reader(pid))
        role = role_from_cmdline(args, profile) if args else None
        if role is None:
            unassigned.append(pid)
            continue
        gpus = tuple(sorted({row.gpu_uuid for row in process_rows if row.gpu_uuid}))
        candidates.setdefault(role, []).append((pid, args, gpus))

    observations: dict[str, ServiceObservation] = {}
    issues: list[str] = []
    warnings: list[str] = []
    for role, service in profile.services.items():
        role_candidates = candidates.get(role, [])
        if len(role_candidates) > 1:
            issues.append(f"{role}: multiple llama-server PIDs match port {service.port}")
        pid: int | None = None
        args: tuple[str, ...] = ()
        gpu_uuids: tuple[str, ...] = ()
        if role_candidates:
            pid, args, gpu_uuids = role_candidates[0]

        health_data: dict[str, Any] | None = None
        props: dict[str, Any] | None = None
        if server_reader is not None and role_candidates:
            health_data, props = server_reader(service)
        health = (
            str((health_data or {}).get("status") or "unreachable").lower()
            if server_reader is not None
            else "not-checked"
        )
        model = str((props or {}).get("model_path") or _option(args, "-m", "--model"))
        mmproj = _option(args, "--mmproj")
        n_ctx = _props_n_ctx(props or {})
        if n_ctx is None:
            raw_ctx = _option(args, "-c", "--ctx-size")
            n_ctx = int(raw_ctx) if raw_ctx.isdecimal() else None

        observation = ServiceObservation(
            role=role,
            pid=pid,
            gpu_uuids=gpu_uuids,
            model=model,
            mmproj=mmproj,
            n_ctx=n_ctx,
            health=health,
            port=service.port,
            cmdline=args,
        )
        observations[role] = observation

        if pid is None:
            issues.append(f"{role}: missing llama-server process for port {service.port}")
            continue
        if server_reader is not None and health != "ok":
            issues.append(f"{role}: health={health}")
        if service.gpu:
            expected_gpus = _expected_gpu_set(service.gpu, inventory)
            actual_gpus = set(gpu_uuids)
            if expected_gpus != actual_gpus:
                issues.append(
                    f"{role}: wrong GPU expected={','.join(sorted(expected_gpus))} "
                    f"actual={','.join(sorted(actual_gpus)) or 'none'}"
                )
        try:
            expected_model = resolve_model_reference(service.model, env)
        except ProfileError as exc:
            issues.append(f"{role}: expected model cannot be resolved: {exc}")
        else:
            if not model:
                issues.append(f"{role}: loaded model is not observable")
            elif _basename(expected_model) != _basename(model):
                issues.append(
                    f"{role}: wrong model expected={Path(expected_model).name} "
                    f"actual={Path(model).name}"
                )
        if service.mmproj:
            try:
                expected_mmproj = resolve_model_reference(service.mmproj, env)
            except ProfileError as exc:
                issues.append(f"{role}: expected mmproj cannot be resolved: {exc}")
            else:
                if not mmproj:
                    issues.append(f"{role}: loaded mmproj is not observable")
                elif _basename(expected_mmproj) != _basename(mmproj):
                    issues.append(
                        f"{role}: wrong mmproj expected={Path(expected_mmproj).name} "
                        f"actual={Path(mmproj).name}"
                    )
        if service.ctx is not None:
            if n_ctx is None:
                issues.append(f"{role}: n_ctx is not observable")
            elif service.ctx != n_ctx:
                issues.append(f"{role}: n_ctx mismatch expected={service.ctx} actual={n_ctx}")

    return Inspection(
        observations=observations,
        gpu_processes=rows,
        unassigned_pids=tuple(sorted(unassigned)),
        issues=tuple(issues),
        warnings=tuple(warnings),
    )
