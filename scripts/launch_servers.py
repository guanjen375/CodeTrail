#!/usr/bin/env python3
"""Launch CodeTrail llama-server roles from the shared deployment profile."""
from __future__ import annotations

import argparse
import json
import os
import re
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


_SHARD_RE = re.compile(r"^(?P<stem>.+)-(?P<idx>\d{5})-of-(?P<total>\d{5})\.gguf$", re.IGNORECASE)


def _artifact_bytes(path: Path) -> int:
    """模型總大小;多 shard GGUF 會把同組 shard 全部加總。"""
    try:
        match = _SHARD_RE.match(path.name)
        if not match:
            return path.stat().st_size
        stem, total = match.group("stem"), match.group("total")
        return sum(
            sib.stat().st_size
            for sib in path.parent.glob("*.gguf")
            if (m := _SHARD_RE.match(sib.name)) and m.group("stem") == stem and m.group("total") == total
        )
    except OSError:
        return 0


def _health_timeout(role: str, environ: Mapping[str, str], artifact_bytes: int = 0) -> int:
    """health 等待上限。main 依模型大小放大:大模型冷載入(尤其 --no-mmap)
    正常就要好幾分鐘,固定 120s 會把「還在載入」誤判成失敗。"""
    name = "MAIN_HEALTH_TIMEOUT" if role == "main" else "RAG_HEALTH_TIMEOUT"
    explicit = (environ.get(name) or "").strip()
    if explicit:
        return _positive_int(explicit, name)
    if role != "main":
        return 60
    size_gib = artifact_bytes / (1024**3)
    return max(300, min(1800, int(size_gib * 5)))


def _health_status(service: ServiceProfile) -> str:
    try:
        with urlopen(f"{service.base_url}/health", timeout=2) as response:  # noqa: S310 - validated URL
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError):
        return "unreachable"
    if not isinstance(data, dict):
        return "invalid"
    return str(data.get("status") or "unknown").lower()


def _window_alive(session: str, window: str) -> bool:
    proc = subprocess.run(
        ["tmux", "list-windows", "-t", session, "-F", "#{window_name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and window in proc.stdout.split()


def _wait_for_health(service: ServiceProfile, timeout: int, session: str) -> None:
    """等 /health=ok。等待期間定時回報進度(載入中 ≠ 當機);
    llama-server process 一結束(tmux window 消失)立即失敗,不空等 timeout。"""
    window = WINDOWS[service.role]
    start = time.monotonic()
    deadline = start + timeout
    next_report = start + 15
    last_status = "unreachable"
    while time.monotonic() < deadline:
        last_status = _health_status(service)
        if last_status == "ok":
            print(f"[+] {service.role} health OK: {service.base_url}/health status=ok")
            return
        if not _window_alive(session, window):
            raise ProfileError(
                f"{service.role} 的 llama-server process 已結束(模型載入失敗或參數錯誤);"
                f"tmux window {session}:{window} 已關閉"
            )
        now = time.monotonic()
        if now >= next_report:
            elapsed = int(now - start)
            print(
                f"[…] {service.role} 載入中,已等待 {elapsed}s(health={last_status},"
                f"上限 {timeout}s)— process 存活,大模型載入需要幾分鐘是正常的",
                flush=True,
            )
            next_report = now + 15
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
    log_dir: Path | None = None,
) -> None:
    window = WINDOWS[service.role]
    command_line = shlex.join(command)
    if first_in_session:
        tmux_args = ["tmux", "new-session", "-d", "-s", session, "-n", window, command_line]
    else:
        tmux_args = ["tmux", "new-window", "-t", session, "-n", window, command_line]
    subprocess.run(tmux_args, check=True)
    if log_dir is not None:
        # 從啟動第一刻就把 server 輸出持續寫進一般檔案:llama-server 若因參數/模型
        # 錯誤立即退出,tmux window 會消失、事後 capture-pane 抓不到 —— pipe-pane
        # 保證失敗當下的 log 已經在磁碟上。
        log_path = log_dir / f"{service.role}.log"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8")  # 每次啟動重寫該 role 的 log
            subprocess.run(
                ["tmux", "pipe-pane", "-o", "-t", f"{session}:{window}",
                 f"cat >> {shlex.quote(str(log_path))}"],
                check=False,
            )
        except OSError:
            pass
    print(f"[+] started {service.role} server ({service.base_url}) in tmux {session}:{window}")


def _state_log_dir(environ: Mapping[str, str]) -> Path:
    base = (environ.get("XDG_STATE_HOME") or "").strip() or str(
        Path(environ.get("HOME") or Path.home()) / ".local" / "state"
    )
    return Path(base) / "codetrail" / "logs"


def _capture_window_log(session: str, window: str, dest: Path) -> bool:
    proc = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", f"{session}:{window}", "-S", "-300"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(proc.stdout, encoding="utf-8")
    except OSError:
        return False
    return True


def _rollback_started(
    exc: Exception,
    started_roles: Sequence[ServiceProfile],
    created_sessions: Sequence[str],
    sessions: Mapping[str, str],
    environ: Mapping[str, str],
) -> None:
    """某個 role 啟動失敗:先保存各 role 的 server log,再關掉本次建立的
    tmux sessions,讓使用者修正後可以直接重跑,不會卡在 session already exist。"""
    if not created_sessions:
        return
    if (environ.get("AICODE_NO_ROLLBACK") or "").strip():
        print(
            f"[!] AICODE_NO_ROLLBACK=1:保留現場不清理(tmux:{', '.join(created_sessions)})",
            file=sys.stderr,
        )
        return
    log_dir = _state_log_dir(environ)
    saved: list[str] = []
    for service in started_roles:
        dest = log_dir / f"{service.role}.log"
        try:
            piped = dest.is_file() and dest.stat().st_size > 0
        except OSError:
            piped = False
        # pipe-pane 已持續寫入的 log 不覆蓋(capture-pane 只是 window 還活著時的補充)。
        session = _session_for(service.role, sessions)
        if piped or _capture_window_log(session, WINDOWS[service.role], dest):
            saved.append(service.role)
    for session in created_sessions:
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    print(f"[rollback] 啟動失敗:{exc}", file=sys.stderr)
    if saved:
        print(f"[rollback] server log 已保存:{log_dir}/({', '.join(saved)}).log", file=sys.stderr)
    print(
        f"[rollback] 已自動停止本次啟動的服務並清理 tmux({', '.join(created_sessions)});"
        "修正後直接重新執行 ~/start.sh 即可。",
        file=sys.stderr,
    )
    print("[rollback] 要保留現場除錯:AICODE_NO_ROLLBACK=1 ~/start.sh", file=sys.stderr)


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
        raise ProfileError(
            f"tmux session(s) already exist: {', '.join(existing)}; "
            "先執行 ./scripts/quit.sh(或 ~/start.sh stop)再重新啟動"
        )
    for service in services:
        resolve_model_reference(service.model, environ, must_exist=True)
        if service.mmproj:
            resolve_model_reference(service.mmproj, environ, must_exist=True)
        if _port_responds(service):
            raise ProfileError(f"{service.role} port {service.port} is already in use ({service.base_url})")

    log_dir = _state_log_dir(environ)
    print(f"[i] server log 即時寫入:{log_dir}/<role>.log(~/start.sh logs <role> 可查看)")
    started_sessions: set[str] = set()
    created_sessions: list[str] = []
    started_roles: list[ServiceProfile] = []
    try:
        for service in services:
            session = _session_for(service.role, sessions)
            command = _command_for(service, str(binary), environ, must_exist=True)
            _start_role(
                service, command, session,
                first_in_session=session not in started_sessions,
                log_dir=log_dir,
            )
            if session not in started_sessions:
                created_sessions.append(session)
            started_sessions.add(session)
            started_roles.append(service)
            artifact = (
                _artifact_bytes(Path(resolve_model_reference(service.model, environ)))
                if service.role == "main"
                else 0
            )
            _wait_for_health(service, _health_timeout(service.role, environ, artifact), session)
    except (ProfileError, subprocess.CalledProcessError) as exc:
        _rollback_started(exc, started_roles, created_sessions, sessions, environ)
        raise

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
