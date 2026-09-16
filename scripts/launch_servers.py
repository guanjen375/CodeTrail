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
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


class _NoRedirectHandler(HTTPRedirectHandler):
    """loopback /health 探測不跟 3xx:回 None → HTTPError,呼叫端當 unreachable。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# proxy 衛生:health 探測不得被環境 HTTP(S)_PROXY 帶去別的 host。
_LOCAL_PROBE_OPENER = build_opener(ProxyHandler({}), _NoRedirectHandler())

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import process_env  # noqa: E402
from deployment_profile import (  # noqa: E402
    TMUX_SESSIONS,
    DeploymentProfile,
    ProfileError,
    ServiceProfile,
    add_loader_arguments,
    build_server_command,
    load_effective_profile,
    loader_argv,
    loader_kwargs,
    resolve_model_reference,
    warn_cpu_moe_fit_conflicts,
)
from scripts import stop_servers  # noqa: E402
from runtime_dependencies import DependencyError  # noqa: E402

WINDOWS = {
    "main": "main",
    "embedding": "embed",
    "reranker": "rerank",
    "vl": "vl",
}


def _positive_int(value: str) -> int:
    """argparse 的正整數:`--health-timeout 0` 要在解析階段就被擋下。"""
    if not value.isdecimal() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return int(value)


def _scope_roles(scope: str) -> tuple[str, ...]:
    if scope == "main":
        return ("main",)
    if scope == "aux":
        return ("embedding", "reranker", "vl")
    return ("main", "embedding", "reranker", "vl")


def _sessions(args: argparse.Namespace) -> dict[str, str]:
    """tmux session 名。真值是 `deployment_profile.TMUX_SESSIONS` 這個 repo 常數。

    以前是三個環境變數(`MAIN_SESSION` / `SESSION` / `AUX_SESSION`),桌面環境裡
    很常見的泛用 `SESSION` 會讓 stop 去關別人的 session。隱藏旗標只給測試用:
    契約測試不能碰開發機上真的在跑的那兩個 session。
    """
    return {
        "main": getattr(args, "main_session", None) or TMUX_SESSIONS["main"],
        "aux": getattr(args, "aux_session", None) or TMUX_SESSIONS["aux"],
    }


def _pane_command(role: str, args: argparse.Namespace) -> str:
    """pane 裡真正要跑的東西:`deployment_profile.py exec <role> <loader argv>`。

    不直接 respawn llama-server 的理由:tmux pane 的環境 = tmux server 的全域環境
    + session 環境,launcher 的行程環境管不到已經在跑的 daemon。把最後一步放在
    `exec` 裡,最終環境就由 `process_env.llama_server_env()` 一個地方決定
    (`LLAMA_ARG_*` / `CUDA_VISIBLE_DEVICES` 剝掉,GPU 只由驗證過的值重新輸出)。
    `loader_argv` 把 launcher 收到的設定原封不動轉過去 —— 讓 pane 自己重讀一次檔案
    並不等價(argv 覆寫會消失)。
    """
    return shlex.join(
        [sys.executable, str(REPO_ROOT / "deployment_profile.py"), "exec", role, *loader_argv(args)]
    )


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


def _health_timeout(role: str, explicit: int | None = None, artifact_bytes: int = 0) -> int:
    """health 等待上限。main 依模型大小放大:大模型冷載入(尤其 --no-mmap)
    正常就要好幾分鐘,固定 120s 會把「還在載入」誤判成失敗。
    `--health-timeout` 給了就是它(以前是兩個環境變數,兩份安裝會互相蓋)。"""
    if explicit is not None:
        return explicit
    if role != "main":
        return 60
    size_gib = artifact_bytes / (1024**3)
    return max(300, min(1800, int(size_gib * 5)))


def _health_status(service: ServiceProfile) -> str:
    try:
        with _LOCAL_PROBE_OPENER.open(f"{service.base_url}/health", timeout=2) as response:  # noqa: S310 - validated URL
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError):
        return "unreachable"
    if not isinstance(data, dict):
        return "invalid"
    return str(data.get("status") or "unknown").lower()


def _pane_state(session: str, window: str) -> tuple[bool, str]:
    """回傳 (llama-server process 是否已結束, 原因說明)。

    window 開著 remain-on-exit:process 結束時 pane 標記 dead 並保留 exit code
    (畫面上也留著最後輸出可檢視);window 整個不見(被外部 kill)也視為結束。"""
    proc = process_env.run(
        ["tmux", "list-panes", "-t", f"{session}:{window}",
         "-F", "#{pane_dead} #{pane_dead_status}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return True, "tmux window 已關閉"
    for line in proc.stdout.splitlines():
        parts = line.split()
        if parts and parts[0] == "1":
            status = parts[1] if len(parts) > 1 else ""
            return True, f"process 已結束(exit {status or '?'})"
    return False, ""


def _wait_for_health(service: ServiceProfile, timeout: int, session: str) -> None:
    """等 /health=ok。等待期間定時回報進度(載入中 ≠ 當機);
    llama-server process 一結束(pane dead / window 消失)立即失敗,不空等 timeout。"""
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
        dead, reason = _pane_state(session, window)
        if dead:
            raise ProfileError(
                f"{service.role} 的 llama-server {reason};模型載入失敗或參數錯誤。"
                f"完整錯誤:~/start.sh logs {service.role}"
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
    profile: DeploymentProfile,
    *,
    must_exist: bool,
) -> list[str]:
    """dry-run 印的那一份:pane 裡最後真的會被 exec 的 llama-server argv。"""
    return build_server_command(
        service, profile.llama_bin, must_exist=must_exist, registry_file=profile.registry_file
    )


def _print_dry_run(
    profile: DeploymentProfile,
    roles: Sequence[str],
    args: argparse.Namespace,
) -> None:
    print(f"profile={profile.selected_profile}")
    print(f"profile_verification={profile.verification}")
    print(f"profile_hardware={profile.hardware}")
    print(f"llama_bin={profile.llama_bin}")
    registry = profile.registry_file
    for role in roles:
        service = profile.service(role)
        command = _command_for(service, profile, must_exist=False)
        prefix = {"embedding": "embed", "reranker": "rerank"}.get(role, role)
        print(f"{prefix}_base_url={service.base_url}")
        print(f"{prefix}_host={urlsplit(service.base_url).hostname or ''}")
        print(f"{prefix}_bind_host={command[command.index('--host') + 1]}")
        print(f"{prefix}_port={service.port}")
        if role == "vl":
            print(f"vl_gguf={resolve_model_reference(service.model, registry_file=registry)}")
            print(f"vl_mmproj={resolve_model_reference(service.mmproj, registry_file=registry)}")
        else:
            print(f"{prefix}_model={resolve_model_reference(service.model, registry_file=registry)}")
        print(f"{prefix}_gpu_role={service.gpu_role}")
        print(f"{prefix}_gpu={service.gpu}")
        print(f"{prefix}_command={shlex.join(command)}")
    if any(role != "main" for role in roles):
        print(f"health_timeout={_health_timeout('embedding', args.health_timeout)}")


def _tmux_has_session(session: str) -> bool:
    return process_env.run(
        ["tmux", "has-session", "-t", session],
        stdout=process_env.DEVNULL,
        stderr=process_env.DEVNULL,
        check=False,
    ).returncode == 0


def _start_role(
    service: ServiceProfile,
    command_line: str,
    session: str,
    *,
    first_in_session: bool,
    log_dir: Path | None = None,
) -> None:
    window = WINDOWS[service.role]
    target = f"{session}:{window}"
    # 先開「空」window(預設 shell),掛好 remain-on-exit 與 pipe-pane 之後,
    # 才 respawn 成真正的 llama-server。這樣即使 llama-server 秒退:
    #   1. 輸出從第一個 byte 就進 log(pipe-pane 已先接上,沒有 attach race);
    #   2. pane 帶著 exit code 留在原地(remain-on-exit),不會 window 消失後什麼都抓不到。
    if first_in_session:
        process_env.run(["tmux", "new-session", "-d", "-s", session, "-n", window], check=True)
    else:
        process_env.run(["tmux", "new-window", "-t", session, "-n", window], check=True)
    process_env.run(
        ["tmux", "set-option", "-w", "-t", target, "remain-on-exit", "on"], check=False
    )
    if log_dir is not None:
        log_path = log_dir / f"{service.role}.log"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8")  # 每次啟動重寫該 role 的 log
            pipe = process_env.run(
                ["tmux", "pipe-pane", "-o", "-t", target,
                 f"cat >> {shlex.quote(str(log_path))}"],
                check=False,
                capture_output=True,
                text=True,
            )
            if pipe.returncode != 0:
                detail = (pipe.stderr or pipe.stdout).strip() or f"exit {pipe.returncode}"
                print(
                    f"[!] ⚠ 無法為 {service.role} 接上 tmux pipe-pane({detail});"
                    f"啟動照常進行,但 ~/start.sh logs {service.role} 將看不到輸出",
                    file=sys.stderr,
                )
        except OSError as exc:
            # log 寫不進去不該擋啟動,但也不能無聲吞掉,否則使用者以為 logs 可用。
            print(
                f"[!] ⚠ 無法建立 {service.role} 的 log 檔({exc});"
                f"啟動照常進行,但 ~/start.sh logs {service.role} 將看不到輸出",
                file=sys.stderr,
            )
    process_env.run(["tmux", "respawn-window", "-k", "-t", target, command_line], check=True)
    print(f"[+] started {service.role} server ({service.base_url}) in tmux {session}:{window}")


def _state_log_dir(environ: Mapping[str, str] | None = None) -> Path:
    """server log 的位置。這是「檔案在哪」,不是設定,所以仍然讀 XDG / HOME。"""
    env = os.environ if environ is None else environ
    base = (env.get("XDG_STATE_HOME") or "").strip() or str(
        Path(env.get("HOME") or Path.home()) / ".local" / "state"
    )
    return Path(base) / "codetrail" / "logs"


def _capture_window_log(session: str, window: str, dest: Path) -> bool:
    proc = process_env.run(
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
    reason: BaseException | str,
    started_roles: Sequence[ServiceProfile],
    created_sessions: Sequence[str],
    sessions: Mapping[str, str],
    *,
    log_dir: Path,
    keep_on_failure: bool = False,
) -> None:
    """某個 role 啟動失敗:先保存各 role 的 server log,再關掉本次建立的
    tmux sessions,讓使用者修正後可以直接重跑,不會卡在 session already exist。"""
    if not created_sessions:
        return
    if keep_on_failure:
        print(
            f"[!] --keep-on-failure:保留現場不清理(tmux:{', '.join(created_sessions)})",
            file=sys.stderr,
        )
        return
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
    # kill 之前先記 pane PID:半載入的主模型退出時要釋放幾十 GB buffer,
    # VRAM 會多掛數十秒;不等它結束就返回,立刻重跑會撞 port/容量誤判。
    pane_pids: dict[int, str] = {}
    for session in created_sessions:
        try:
            pane_pids.update(stop_servers._pane_pids(session))
        except (DependencyError, OSError) as exc:
            print(f"[rollback] 無法取得待停止的 PID: {exc}", file=sys.stderr)
    for session in created_sessions:
        process_env.run(
            ["tmux", "kill-session", "-t", session],
            stdout=process_env.DEVNULL,
            stderr=process_env.DEVNULL,
            check=False,
        )
    if pane_pids:
        try:
            leftover = stop_servers._wait_released(
                pane_pids, timeout=stop_servers.DEFAULT_STOP_TIMEOUT
            )
        except KeyboardInterrupt:
            leftover = []
            print("[rollback] 略過等待 VRAM 釋放(Ctrl-C)", file=sys.stderr)
        except DependencyError as exc:
            leftover = []
            print(f"[rollback] 無法驗證 VRAM 釋放: {exc}", file=sys.stderr)
        if leftover:
            print(
                f"[rollback] ⚠ 部分 process 尚未釋放 VRAM(PID:"
                f"{', '.join(map(str, sorted(leftover)))});重新啟動前先用 nvidia-smi 確認",
                file=sys.stderr,
            )
    print(f"[rollback] 啟動失敗:{reason}", file=sys.stderr)
    if saved:
        print(f"[rollback] server log 已保存:{log_dir}/({', '.join(saved)}).log", file=sys.stderr)
    print(
        f"[rollback] 已自動停止本次啟動的服務並清理 tmux({', '.join(created_sessions)});"
        "修正後直接重新執行 ~/start.sh 即可。",
        file=sys.stderr,
    )
    print("[rollback] 要保留現場除錯:~/start.sh --keep-on-failure", file=sys.stderr)


def launch(
    profile: DeploymentProfile,
    roles: Sequence[str],
    args: argparse.Namespace,
) -> None:
    """啟動這幾個 role。設定全部來自 `profile`(檔案 + argv 覆寫)與 `args`;
    這個函式不讀任何環境變數 —— pane 的最終環境由 `deployment_profile.py exec`
    在 pane 內決定。"""
    if profile.mode == "client":
        raise ProfileError("client mode does not launch models; run the launcher on A")
    services = [profile.service(role) for role in roles]
    _check_port_collisions(services)
    warn_cpu_moe_fit_conflicts(services)
    if args.dry_run:
        _print_dry_run(profile, roles, args)
        return

    if not shutil.which("tmux"):
        raise ProfileError("tmux is required to launch llama-server sessions")
    binary = Path(profile.llama_bin)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ProfileError(
            f"llama-server does not exist or is not executable: {binary};"
            "改用 --llama-bin,或重跑 ./set_config.sh 寫進 deployment.json 的 llama_bin"
        )

    sessions = _sessions(args)
    registry = profile.registry_file
    used_sessions = {_session_for(role, sessions) for role in roles}
    existing = sorted(session for session in used_sessions if _tmux_has_session(session))
    if existing:
        raise ProfileError(
            f"tmux session(s) already exist: {', '.join(existing)}; "
            "先執行 ~/start.sh stop 再重新啟動"
        )
    for service in services:
        resolve_model_reference(service.model, must_exist=True, registry_file=registry)
        if service.mmproj:
            resolve_model_reference(service.mmproj, must_exist=True, registry_file=registry)
        if _port_responds(service):
            raise ProfileError(f"{service.role} port {service.port} is already in use ({service.base_url})")

    log_dir = _state_log_dir()
    print(f"[i] server log 即時寫入:{log_dir}/<role>.log(~/start.sh logs <role> 可查看)")
    started_sessions: set[str] = set()
    created_sessions: list[str] = []
    started_roles: list[ServiceProfile] = []
    try:
        for service in services:
            session = _session_for(service.role, sessions)
            first_in_session = session not in started_sessions
            if first_in_session:
                # 建 session 之前先登記:new-session 成功、respawn-window 才失敗的
                # 半套狀態也要能 rollback,否則殘留 session 會卡住下一次啟動。
                created_sessions.append(session)
            started_sessions.add(session)
            started_roles.append(service)
            _start_role(
                service, _pane_command(service.role, args), session,
                first_in_session=first_in_session,
                log_dir=log_dir,
            )
            artifact = (
                _artifact_bytes(
                    Path(resolve_model_reference(service.model, registry_file=registry))
                )
                if service.role == "main"
                else 0
            )
            _wait_for_health(
                service,
                _health_timeout(service.role, args.health_timeout, artifact),
                session,
            )
    except (ProfileError, process_env.CalledProcessError) as exc:
        _rollback_started(
            exc, started_roles, created_sessions, sessions,
            log_dir=log_dir, keep_on_failure=args.keep_on_failure,
        )
        raise
    except KeyboardInterrupt:
        # Ctrl-C 最常發生在等待大模型 health 的幾分鐘;同樣要清理,
        # 不留下會卡住下一次啟動的殘存 session(保留現場:--keep-on-failure)。
        _rollback_started(
            "使用者中斷(Ctrl-C)", started_roles, created_sessions, sessions,
            log_dir=log_dir, keep_on_failure=args.keep_on_failure,
        )
        raise

    print("\nCodeTrail model servers ready.")
    # 絕對路徑:這行常被從 $HOME 執行的 ~/start.sh 帶出來,相對路徑會找不到。
    status_py = Path(__file__).resolve().parent / "check_status.py"
    print(f"  python3 {shlex.quote(str(status_py))} --strict")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch llama-server roles from a CodeTrail profile")
    parser.add_argument("--scope", choices=("main", "aux", "all"), required=True)
    parser.add_argument("--dry-run", action="store_true", help="print resolved commands without launching")
    parser.add_argument(
        "--health-timeout", type=_positive_int, default=None,
        help="等 /health=ok 的上限秒數(預設:main 依模型大小 300..1800、附屬 60)",
    )
    parser.add_argument(
        "--keep-on-failure", action="store_true",
        help="啟動失敗時保留 tmux session 供除錯(預設會自動清乾淨)",
    )
    # 隱藏旗標:契約測試不能碰開發機上真的在跑的那兩個 session。
    parser.add_argument("--main-session", help=argparse.SUPPRESS)
    parser.add_argument("--aux-session", help=argparse.SUPPRESS)
    # profile / llama_bin / GPU / main 覆寫全部來自 loader 那一份(launcher 與
    # `deployment_profile.py exec` 必須認同一組旗標,否則 pane 拿到的設定會漂移)。
    add_loader_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        profile = load_effective_profile(**loader_kwargs(args))
        launch(profile, _scope_roles(args.scope), args)
        return 0
    except (ProfileError, process_env.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # launch() 已先做過 rollback(或還沒建任何 session);這裡只收尾訊息。
        print("\n[!] 已中斷(Ctrl-C)。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
