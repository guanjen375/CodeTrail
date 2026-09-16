#!/usr/bin/env python3
"""Stop CodeTrail tmux sessions, wait until llama-server fully exits and VRAM is released."""
from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import process_env  # noqa: E402
from deployment_profile import (  # noqa: E402
    TMUX_SESSIONS,
    ProfileError,
    add_loader_arguments,
    load_effective_profile,
    loader_kwargs,
)
from deployment_status import query_gpu_processes  # noqa: E402
from runtime_dependencies import DependencyError  # noqa: E402

# tmux kill-session(SIGHUP)只是「開始停止」:llama-server 退出前要先釋放
# host buffer(--no-mmap / --cpu-moe 的主模型是幾十~上百 GB),這段期間
# process 還掛著、VRAM 也還在 nvidia-smi 上。所以 stop 必須等到真正釋放
# 才能返回,否則使用者 stop 完立刻看 nvidia-smi 會以為「清不乾淨」。
_TERM_AFTER = 10.0  # SIGHUP 後仍存活(非退出中)→ 補 SIGTERM(graceful 路徑)
_KILL_AFTER = 25.0  # 再不退 → SIGKILL(對已在退出路徑的 process 無害)
_PROGRESS_EVERY = 5.0
#: 大模型 teardown 常見數十秒;`--timeout` 可覆寫(launcher 的 rollback 直接用這個值)。
DEFAULT_STOP_TIMEOUT = 120


def _positive_int(value: str) -> int:
    """argparse 的正整數:`--timeout 0` 要在解析階段就被擋下。"""
    if not value.isdecimal() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return int(value)


def _roles(scope: str) -> tuple[str, ...]:
    if scope == "aux":
        return ("embedding", "reranker", "vl")
    return ("main", "embedding", "reranker", "vl")


def _sessions(scope: str, args: argparse.Namespace) -> tuple[str, ...]:
    """要關掉的 tmux session。名字是 `deployment_profile.TMUX_SESSIONS` 這個 repo 常數。

    以前 aux 那格會吃殼層的泛用 `SESSION`(桌面環境很常見),於是「停止」會去
    關一個完全無關的 session,而真正的 rag session 留著。隱藏旗標只給契約測試用。
    """
    main = getattr(args, "main_session", None) or TMUX_SESSIONS["main"]
    aux = getattr(args, "aux_session", None) or TMUX_SESSIONS["aux"]
    return (aux,) if scope == "aux" else (main, aux)


def _pane_pids(session: str) -> dict[int, str]:
    """kill-session 之前記下 session 內每個 pane 的 process(pid → session:window)。"""
    proc = process_env.run(
        ["tmux", "list-panes", "-s", "-t", session, "-F", "#{pane_pid} #{window_name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise DependencyError(f"tmux list-panes 無法取得待停止的 PID: {(proc.stderr or proc.stdout).strip()}")
    tracked: dict[int, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split(maxsplit=1)
        if not parts or not parts[0].isdecimal() or int(parts[0]) <= 0:
            raise DependencyError("tmux list-panes 的 PID 回應格式無效，無法驗證停止範圍。")
        window = parts[1].strip() if len(parts) > 1 else "?"
        tracked[int(parts[0])] = f"{session}:{window}"
    if not tracked:
        raise DependencyError("tmux list-panes 沒有回報 PID，無法驗證停止範圍。")
    return tracked


def _proc_state(pid: int) -> str:
    """'' = process 已消失;'Z' = zombie(已死待回收,收不到訊號);其他 = 存活。"""
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise DependencyError(f"無法讀取 /proc/{pid}/stat，不能確認 process 已結束: {exc}") from exc
    _, _, tail = stat.rpartition(")")  # comm 可含空白/括號,取最後一個 ')' 之後
    fields = tail.split()
    if not fields or len(fields[0]) != 1 or fields[0] not in "RSDZTtXxKWPI":
        raise DependencyError(f"/proc/{pid}/stat 格式無效，不能確認 process 已結束。")
    return fields[0]


def _is_llama_pid(pid: int) -> bool:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return False
    args = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    return bool(args) and Path(args[0]).name.startswith("llama-server")


def _gpu_compute_pids() -> set[int]:
    """nvidia-smi compute list 上的所有 PID(= VRAM 尚未釋放)；觀測失敗即報錯。

    刻意不濾 process 名稱:zombie 的 /proc/<pid>/cmdline 讀不到,nvidia-smi
    的 process_name 會變空/不可辨識,按名稱過濾會把「還佔著 VRAM 的殭屍」
    誤判成已釋放(實測 GLM-5.2 的殭屍期長達 ~19s)。判斷歸屬用「pid 是否
    在 tracked 裡」就夠了。"""
    if not shutil.which("nvidia-smi"):
        raise DependencyError("需要 nvidia-smi 才能驗證 VRAM 釋放；請安裝或修復 NVIDIA 驅動。")
    try:
        proc = process_env.run(
            ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, process_env.TimeoutExpired) as exc:
        raise DependencyError(f"nvidia-smi 無法驗證 VRAM 釋放: {exc}") from exc
    if proc.returncode != 0:
        raise DependencyError(f"nvidia-smi 無法驗證 VRAM 釋放: {proc.stderr.strip()}")
    if any(not token.isdecimal() for token in proc.stdout.split()):
        raise DependencyError("nvidia-smi PID 回應格式無效，無法驗證 VRAM 釋放。")
    return {int(token) for token in proc.stdout.split() if token.isdecimal()}


def _gpu_memory_line() -> str | None:
    if not shutil.which("nvidia-smi"):
        return None
    proc = process_env.run(
        ["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    parts = []
    for line in proc.stdout.splitlines():
        bits = [bit.strip() for bit in line.split(",")]
        if len(bits) == 2 and bits[0].isdecimal():
            parts.append(f"GPU{bits[0]} {bits[1]}MiB")
    return "、".join(parts) if parts else None


def _wait_released(
    tracked: Mapping[int, str],
    *,
    timeout: int,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    kill: Callable[[int, int], None] = os.kill,
) -> list[int]:
    """等 tracked process 完全結束、且從 nvidia-smi compute list 消失。

    zombie 也可能還佔著 VRAM(驅動清理在 process 收尾期進行),所以「乾淨」
    的判準是兩者同時成立。escalation 只對「仍存活且 cmdline 仍是 llama-server」
    的 PID 送訊號,避開 PID 重用。回傳超時仍未釋放的 PID(空 list = 乾淨)。
    """
    start = clock()
    deadline = start + timeout
    next_report = start + _PROGRESS_EVERY
    signalled: dict[int, set[int]] = {signal.SIGTERM: set(), signal.SIGKILL: set()}
    if not tracked:
        return []
    while True:
        alive = set()
        process_error = None
        for pid in tracked:
            try:
                if _proc_state(pid) not in ("", "Z"):
                    alive.add(pid)
            except DependencyError as exc:
                # 未知狀態不可當退出；保留待處理並繼續其他 PID 的安全清理。
                alive.add(pid)
                process_error = exc
        gpu_error = None
        try:
            gpu = _gpu_compute_pids()
        except DependencyError as exc:
            gpu, gpu_error = None, exc
        holding = set(alive)
        if gpu is not None:
            holding |= {pid for pid in tracked if pid in gpu}
        if not holding:
            if gpu is None:
                raise gpu_error or DependencyError("nvidia-smi 無法觀測，不能確認 VRAM 已釋放。")
            return []
        now = clock()
        if now >= deadline:
            if process_error is not None:
                raise process_error
            return sorted(holding)
        for sig, after in ((signal.SIGTERM, _TERM_AFTER), (signal.SIGKILL, _KILL_AFTER)):
            if now - start < after:
                continue
            for pid in sorted(alive - signalled[sig]):
                signalled[sig].add(pid)
                if not _is_llama_pid(pid):
                    continue
                try:
                    kill(pid, sig)
                except (PermissionError, ProcessLookupError):
                    continue
                print(
                    f"[!] {tracked[pid]}(PID={pid})於 {int(after)}s 內未結束 → {signal.Signals(sig).name}",
                    file=sys.stderr,
                )
        if now >= next_report:
            names = "、".join(f"{tracked[pid]}(PID={pid})" for pid in sorted(holding))
            print(
                f"[…] 等待 llama-server 結束並釋放 VRAM:{names} — 已 {int(now - start)}s"
                f"(上限 {timeout}s;大模型釋放記憶體需要一段時間是正常的)",
                flush=True,
            )
            next_report = now + _PROGRESS_EVERY
        sleep(0.5)


def _listener_pids(port: int) -> set[int]:
    if not shutil.which("ss"):
        raise DependencyError("需要 ss 才能驗證 listener 已關閉；請安裝 iproute2。")
    try:
        proc = process_env.run(
            ["ss", "-H", "-ltnp", f"sport = :{port}"],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, process_env.TimeoutExpired) as exc:
        raise DependencyError(f"ss 無法檢查 port {port}: {exc}；請修復 iproute2。") from exc
    if proc.returncode != 0:
        raise DependencyError(f"ss 無法檢查 port {port}: {proc.stderr.strip()}")
    if any(not re.search(r"pid=([0-9]+)", line) for line in proc.stdout.splitlines() if line.strip()):
        raise DependencyError(f"ss 沒有回報 port {port} 的 listener PID，無法確認歸屬。")
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stop CodeTrail llama-server tmux sessions")
    parser.add_argument("--scope", choices=("aux", "all"), required=True)
    parser.add_argument("--force", action="store_true", help="SIGTERM verified orphan llama-server listeners")
    parser.add_argument(
        "--timeout", type=_positive_int, default=DEFAULT_STOP_TIMEOUT,
        help=f"等 process 結束並釋放 VRAM 的上限秒數(預設 {DEFAULT_STOP_TIMEOUT})",
    )
    # 隱藏旗標:契約測試不能碰開發機上真的在跑的那兩個 session。
    parser.add_argument("--main-session", help=argparse.SUPPRESS)
    parser.add_argument("--aux-session", help=argparse.SUPPRESS)
    # port 檢查要知道 profile 的四個 port,所以 stop 也認同一組 loader 旗標。
    add_loader_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # 設定檔壞掉時(手改壞 JSON、registry 失效)不能連「停止」都做不到:
    # 關 tmux session 不需要 profile,先關;之後的 port 檢查才需要 profile。
    profile = None
    profile_error: ProfileError | None = None
    try:
        profile = load_effective_profile(**loader_kwargs(args))
        if profile.mode == "client":
            print("client mode: no local model processes managed; stop services on A")
            return 0
    except ProfileError as exc:
        profile_error = exc
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "[!] deployment 設定載入失敗 → 退路模式:仍會關閉 tmux session,"
            "但略過 port 檢查。設定可用 ./set_config.sh(或 --restore-last-backup)修復。",
            file=sys.stderr,
        )

    verification_failed = False
    tracked: dict[int, str] = {}
    if shutil.which("tmux"):
        for session in _sessions(args.scope, args):
            try:
                probe = process_env.run(
                    ["tmux", "has-session", "-t", session],
                    stdout=process_env.DEVNULL,
                    stderr=process_env.DEVNULL,
                    check=False,
                )
            except (OSError, process_env.TimeoutExpired) as exc:
                verification_failed = True
                print(f"ERROR: tmux 無法查詢 session {session!r}: {exc}；請修復 tmux。", file=sys.stderr)
                continue
            if probe.returncode not in (0, 1):
                verification_failed = True
                print(f"ERROR: tmux has-session 失敗(exit={probe.returncode})；請修復 tmux。", file=sys.stderr)
                continue
            exists = probe.returncode == 0
            if exists:
                # 先記 pane PID 再 kill:kill 之後就查不到「該等誰退出」了。
                try:
                    tracked.update(_pane_pids(session))
                except (DependencyError, OSError) as exc:
                    verification_failed = True
                    print(f"ERROR: {exc}", file=sys.stderr)
                try:
                    kill = process_env.run(
                        ["tmux", "kill-session", "-t", session],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                except (OSError, process_env.TimeoutExpired) as exc:
                    verification_failed = True
                    print(f"ERROR: tmux 無法停止 session {session!r}: {exc}；請修復 tmux。", file=sys.stderr)
                    continue
                if kill.returncode == 0:
                    print(f"[+] stopped tmux session {session!r}")
                else:
                    verification_failed = True
                    print(
                        f"[!] could not kill tmux session {session!r}: "
                        f"{(kill.stderr or kill.stdout).strip()}",
                        file=sys.stderr,
                    )
            else:
                print(f"[!] tmux session {session!r} does not exist")
    else:
        verification_failed = True
        print("ERROR: tmux 不可用，無法停止指定 session；請安裝或修復 tmux。仍檢查 profile ports。", file=sys.stderr)

    timeout = args.timeout
    stuck: list[int] = []
    if tracked:
        started = time.monotonic()
        release_verified = True
        try:
            stuck = _wait_released(tracked, timeout=timeout)
        except DependencyError as exc:
            verification_failed = True
            release_verified = False
            print(f"ERROR: {exc}", file=sys.stderr)
        if stuck:
            names = "、".join(f"{tracked[pid]}(PID={pid})" for pid in stuck)
            print(
                f"[!] {timeout}s 內仍未結束/未釋放 VRAM:{names} — process 可能卡在"
                "驅動清理;用 nvidia-smi 觀察,久候不退時考慮重開機。"
                "(等待上限可用 --timeout 調整)",
                file=sys.stderr,
            )
        elif release_verified:
            print(f"[+] llama-server 已全部結束(等待 {time.monotonic() - started:.1f}s)")

    # port 檢查放在等待之後:垂死中的 listener 不會再誤報「rerun with --force」。
    if profile is not None:
        forced: dict[int, str] = {}
        for role in _roles(args.scope):
            service = profile.service(role)
            try:
                pids = _listener_pids(service.port)
                if pids is None:
                    raise DependencyError(f"ss 無法驗證 {role} port {service.port}。")
            except DependencyError as exc:
                verification_failed = True
                print(f"ERROR: {exc}", file=sys.stderr)
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
                        forced[pid] = f"{role}(孤兒)"
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
        if forced:
            # --force 的孤兒同樣等到退出+VRAM 釋放,不然一樣「清不乾淨」。
            try:
                stuck_forced = _wait_released(forced, timeout=timeout)
            except DependencyError as exc:
                verification_failed = True
                print(f"ERROR: {exc}", file=sys.stderr)
                stuck_forced = sorted(forced)
            if stuck_forced:
                names = "、".join(f"{forced[pid]}(PID={pid})" for pid in stuck_forced)
                print(f"[!] 孤兒 process {timeout}s 內仍未釋放:{names}", file=sys.stderr)
                stuck.extend(stuck_forced)
            else:
                print("[+] 孤兒 llama-server 已結束(VRAM 已釋放)")

    # 最終盤點:scope all 停完理應沒有任何 llama-server 還佔著 GPU。
    memory_line = _gpu_memory_line()
    if args.scope == "all" and shutil.which("nvidia-smi"):
        rows, gpu_error = query_gpu_processes()
        if gpu_error:
            verification_failed = True
            print(f"[!] 無法盤點 GPU process(nvidia-smi:{gpu_error})", file=sys.stderr)
        elif rows:
            detail = "、".join(f"PID={row.pid}({row.used_gpu_memory}MiB)" for row in rows)
            print(
                f"[!] 仍有 llama-server 佔用 GPU:{detail} — 不是本次 tmux 停止範圍"
                "(孤兒或其他來源);佔用設定 port 的可用 --force 處理。",
                file=sys.stderr,
            )
    if memory_line:
        print(f"[i] GPU VRAM 用量:{memory_line}")

    if stuck:
        return 1
    return 1 if profile_error or verification_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
