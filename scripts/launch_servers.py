#!/usr/bin/env python3
"""Launch CodeTrail llama-server roles from the shared deployment profile."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deployment_profile import (  # noqa: E402
    DeploymentProfile,
    ProfileError,
    ServiceProfile,
    build_server_command,
    load_effective_profile,
    resolve_model_reference,
)

WINDOWS = {
    "main": "main",
    "embedding": "embed",
    "reranker": "rerank",
    "vl": "vl",
}


def _positive_int(value: str, name: str) -> int:
    if not value.isdecimal() or int(value) < 1:
        raise ProfileError(f"{name} must be a positive integer")
    return int(value)


def _scope_roles(scope: str) -> tuple[str, ...]:
    if scope == "main":
        return ("main",)
    if scope == "aux":
        return ("embedding", "reranker", "vl")
    return ("main", "embedding", "reranker", "vl")


def _cli_environment(args: argparse.Namespace) -> dict[str, str]:
    mapping = {
        "main_model": "AICODE_MODEL",
        "main_gpu": "MAIN_GPU",
        "aux_gpu": "AUX_GPU",
        "embed_gpu": "EMBED_GPU",
        "rerank_gpu": "RERANK_GPU",
        "vl_gpu": "VL_GPU",
        "main_ctx": "MAIN_CTX",
        "main_batch": "MAIN_BATCH",
        "main_ubatch": "MAIN_UBATCH",
    }
    return {
        env_name: str(value)
        for attr, env_name in mapping.items()
        if (value := getattr(args, attr, None)) is not None
    }


def _sessions(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        "main": environ.get("MAIN_SESSION") or "codetrail-main",
        "aux": environ.get("SESSION") or environ.get("AUX_SESSION") or "codetrail-rag",
    }


def _session_for(role: str, sessions: Mapping[str, str]) -> str:
    return sessions["main" if role == "main" else "aux"]


def _health_timeout(role: str, environ: Mapping[str, str]) -> int:
    name = "MAIN_HEALTH_TIMEOUT" if role == "main" else "RAG_HEALTH_TIMEOUT"
    return _positive_int((environ.get(name) or "120" if role == "main" else environ.get(name) or "60"), name)


def _health_status(service: ServiceProfile) -> str:
    try:
        with urlopen(f"{service.base_url}/health", timeout=2) as response:  # noqa: S310 - validated URL
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError):
        return "unreachable"
    if not isinstance(data, dict):
        return "invalid"
    return str(data.get("status") or "unknown").lower()


def _wait_for_health(service: ServiceProfile, timeout: int, session: str) -> None:
    deadline = time.monotonic() + timeout
    last_status = "unreachable"
    while time.monotonic() < deadline:
        last_status = _health_status(service)
        if last_status == "ok":
            print(f"[+] {service.role} health OK: {service.base_url}/health status=ok")
            return
        time.sleep(1)
    raise ProfileError(
        f"{service.role} server was not ready within {timeout}s: "
        f"{service.base_url}/health last_status={last_status}; inspect tmux session {session!r}"
    )


def _port_responds(service: ServiceProfile) -> bool:
    host = urlsplit(service.base_url).hostname or "localhost"
    try:
        with socket.create_connection((host, service.port), timeout=0.5):
            return True
    except OSError:
        return False


def _check_port_collisions(services: Sequence[ServiceProfile]) -> None:
    seen: dict[tuple[str, int], str] = {}
    for service in services:
        host = urlsplit(service.base_url).hostname or ""
        key = (host, service.port)
        if key in seen:
            raise ProfileError(
                f"services {seen[key]} and {service.role} share {host}:{service.port}"
            )
        seen[key] = service.role


def _command_for(
    service: ServiceProfile,
    llama_bin: str,
    environ: Mapping[str, str],
    *,
    must_exist: bool,
) -> list[str]:
    return build_server_command(service, llama_bin, environ, must_exist=must_exist)


def _print_dry_run(
    profile: DeploymentProfile,
    roles: Sequence[str],
    llama_bin: str,
    environ: Mapping[str, str],
) -> None:
    print(f"profile={profile.selected_profile}")
    print(f"profile_verification={profile.verification}")
    print(f"profile_hardware={profile.hardware}")
    for role in roles:
        service = profile.service(role)
        command = _command_for(service, llama_bin, environ, must_exist=False)
        prefix = {"embedding": "embed", "reranker": "rerank"}.get(role, role)
        print(f"{prefix}_base_url={service.base_url}")
        print(f"{prefix}_host={urlsplit(service.base_url).hostname or ''}")
        print(f"{prefix}_bind_host={command[command.index('--host') + 1]}")
        print(f"{prefix}_port={service.port}")
        if role == "vl":
            print(f"vl_gguf={resolve_model_reference(service.model, environ)}")
            print(f"vl_mmproj={resolve_model_reference(service.mmproj, environ)}")
        else:
            print(f"{prefix}_model={resolve_model_reference(service.model, environ)}")
        print(f"{prefix}_gpu_role={service.gpu_role}")
        print(f"{prefix}_gpu={service.gpu}")
        print(f"{prefix}_command={shlex.join(command)}")
    if any(role != "main" for role in roles):
        policy = (environ.get("AICODE_RERANK_FALLBACK_POLICY") or "error").strip().lower()
        if policy not in {"embedding", "main_model", "error"}:
            raise ProfileError("AICODE_RERANK_FALLBACK_POLICY must be embedding, main_model, or error")
        print(f"rerank_fallback_policy={policy}")
        print(f"health_timeout={_health_timeout('embedding', environ)}")


def _tmux_has_session(session: str) -> bool:
    return subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def _start_role(
    service: ServiceProfile,
    command: Sequence[str],
    session: str,
    *,
    first_in_session: bool,
) -> None:
    window = WINDOWS[service.role]
    command_line = shlex.join(command)
    if first_in_session:
        tmux_args = ["tmux", "new-session", "-d", "-s", session, "-n", window, command_line]
    else:
        tmux_args = ["tmux", "new-window", "-t", session, "-n", window, command_line]
    subprocess.run(tmux_args, check=True)
    print(f"[+] started {service.role} server ({service.base_url}) in tmux {session}:{window}")


def launch(
    profile: DeploymentProfile,
    roles: Sequence[str],
    environ: Mapping[str, str],
    *,
    dry_run: bool,
) -> None:
    llama_bin = environ.get("LLAMA_BIN") or str(Path.home() / "llama.cpp" / "build" / "bin" / "llama-server")
    services = [profile.service(role) for role in roles]
    _check_port_collisions(services)
    if dry_run:
        _print_dry_run(profile, roles, llama_bin, environ)
        return

    if not shutil.which("tmux"):
        raise ProfileError("tmux is required to launch llama-server sessions")
    binary = Path(llama_bin).expanduser()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ProfileError(f"llama-server does not exist or is not executable: {binary}")

    sessions = _sessions(environ)
    used_sessions = {_session_for(role, sessions) for role in roles}
    existing = sorted(session for session in used_sessions if _tmux_has_session(session))
    if existing:
        raise ProfileError(f"tmux session(s) already exist: {', '.join(existing)}; stop them first")
    for service in services:
        resolve_model_reference(service.model, environ, must_exist=True)
        if service.mmproj:
            resolve_model_reference(service.mmproj, environ, must_exist=True)
        if _port_responds(service):
            raise ProfileError(f"{service.role} port {service.port} is already in use ({service.base_url})")

    started_sessions: set[str] = set()
    for service in services:
        session = _session_for(service.role, sessions)
        command = _command_for(service, str(binary), environ, must_exist=True)
        _start_role(service, command, session, first_in_session=session not in started_sessions)
        started_sessions.add(session)
        _wait_for_health(service, _health_timeout(service.role, environ), session)

    print("\nCodeTrail model servers ready.")
    print("  ./scripts/check-status.sh --strict")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch llama-server roles from a CodeTrail profile")
    parser.add_argument("--scope", choices=("main", "aux", "all"), required=True)
    parser.add_argument("--profile", help="profile name or absolute JSON profile path")
    parser.add_argument("--dry-run", action="store_true", help="print resolved commands without launching")
    parser.add_argument("--main-model", help="override the main model registry key/path")
    parser.add_argument("--main-gpu", help="override MAIN_GPU")
    parser.add_argument("--aux-gpu", help="override AUX_GPU")
    parser.add_argument("--embed-gpu", help="override EMBED_GPU")
    parser.add_argument("--rerank-gpu", help="override RERANK_GPU")
    parser.add_argument("--vl-gpu", help="override VL_GPU")
    parser.add_argument("--main-ctx", type=int, help="override main ctx")
    parser.add_argument("--main-batch", type=int, help="override main batch")
    parser.add_argument("--main-ubatch", type=int, help="override main ubatch")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cli_env = _cli_environment(args)
    env = dict(os.environ)
    env.update(cli_env)
    try:
        profile = load_effective_profile(env, profile=args.profile)
        launch(profile, _scope_roles(args.scope), env, dry_run=args.dry_run)
        return 0
    except (ProfileError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
