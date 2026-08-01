#!/usr/bin/env python3
"""Stop CodeTrail tmux sessions and inspect only profile-owned service ports."""
from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deployment_profile import ProfileError, load_effective_profile  # noqa: E402


def _roles(scope: str) -> tuple[str, ...]:
    if scope == "aux":
        return ("embedding", "reranker", "vl")
    return ("main", "embedding", "reranker", "vl")


def _sessions(scope: str) -> tuple[str, ...]:
    main = os.environ.get("MAIN_SESSION") or "codetrail-main"
    aux = os.environ.get("SESSION") or os.environ.get("AUX_SESSION") or "codetrail-rag"
    return (aux,) if scope == "aux" else (main, aux)


def _listener_pids(port: int) -> set[int] | None:
    if not shutil.which("ss"):
        return None
    proc = subprocess.run(
        ["ss", "-H", "-ltnp", f"sport = :{port}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {int(value) for value in re.findall(r"pid=([0-9]+)", proc.stdout)}


def _is_expected_llama(pid: int, port: int) -> bool:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return False
    args = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    if not args or not any(Path(arg).name.startswith("llama-server") for arg in args[:1]):
        return False
    for index, arg in enumerate(args):
        if arg == "--port" and index + 1 < len(args) and args[index + 1] == str(port):
            return True
        if arg == f"--port={port}":
            return True
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stop CodeTrail llama-server tmux sessions")
    parser.add_argument("--scope", choices=("aux", "all"), required=True)
    parser.add_argument("--profile", help="profile name or absolute JSON profile path")
    parser.add_argument("--force", action="store_true", help="SIGTERM verified orphan llama-server listeners")
    args = parser.parse_args(argv)
    try:
        profile = load_effective_profile(profile=args.profile)
    except ProfileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if shutil.which("tmux"):
        for session in _sessions(args.scope):
            exists = subprocess.run(
                ["tmux", "has-session", "-t", session],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode == 0
            if exists:
                subprocess.run(["tmux", "kill-session", "-t", session], check=True)
                print(f"[+] stopped tmux session {session!r}")
            else:
                print(f"[!] tmux session {session!r} does not exist")
    else:
        print("[!] tmux not found; checking profile ports only", file=sys.stderr)

    for role in _roles(args.scope):
        service = profile.service(role)
        pids = _listener_pids(service.port)
        if pids is None:
            print(f"[!] cannot inspect {role} port {service.port}: ss is not available", file=sys.stderr)
            continue
        if not pids:
            print(f"[+] {role} port {service.port} is free ({service.base_url})")
            continue
        verified = sorted(pid for pid in pids if _is_expected_llama(pid, service.port))
        unverified = sorted(pids - set(verified))
        if args.force:
            for pid in verified:
                # Re-check immediately before signalling to narrow the PID-reuse race.
                if not _is_expected_llama(pid, service.port):
                    print(
                        f"[!] {role} PID={pid} changed after inspection; refusing to signal it",
                        file=sys.stderr,
                    )
                    continue
                try:
                    os.kill(pid, signal.SIGTERM)
                except (PermissionError, ProcessLookupError) as exc:
                    print(f"[!] could not signal {role} PID={pid}: {exc}", file=sys.stderr)
                else:
                    print(f"[!] sent SIGTERM to orphan {role} llama-server PID={pid}")
        elif verified:
            print(
                f"[!] {role} port {service.port} still has llama-server PID(s) "
                f"{','.join(map(str, verified))}; rerun with --force",
                file=sys.stderr,
            )
        if unverified:
            print(
                f"[!] {role} port {service.port} is owned by unverified PID(s) "
                f"{','.join(map(str, unverified))}; refusing to signal them",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
