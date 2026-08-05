#!/usr/bin/env python3
"""Stop CodeTrail tmux sessions, wait until llama-server fully exits and VRAM is released."""
from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deployment_profile import ProfileError, load_effective_profile  # noqa: E402
from deployment_status import query_gpu_processes  # noqa: E402

# tmux kill-session(SIGHUP)只是「開始停止」:llama-server 退出前要先釋放
# host buffer(--no-mmap / --cpu-moe 的主模型是幾十~上百 GB),這段期間
# process 還掛著、VRAM 也還在 nvidia-smi 上。所以 stop 必須等到真正釋放
# 才能返回,否則使用者 stop 完立刻看 nvidia-smi 會以為「清不乾淨」。
_TERM_AFTER = 10.0  # SIGHUP 後仍存活(非退出中)→ 補 SIGTERM(graceful 路徑)
_KILL_AFTER = 25.0  # 再不退 → SIGKILL(對已在退出路徑的 process 無害)
_PROGRESS_EVERY = 5.0
_DEFAULT_STOP_TIMEOUT = 120  # 大模型 teardown 常見數十秒;AICODE_STOP_TIMEOUT 可覆寫


def _roles(scope: str) -> tuple[str, ...]:
    if scope == "aux":
        return ("embedding", "reranker", "vl")
    return ("main", "embedding", "reranker", "vl")


def _sessions(scope: str) -> tuple[str, ...]:
    main = os.environ.get("MAIN_SESSION") or "codetrail-main"
    aux = os.environ.get("SESSION") or os.environ.get("AUX_SESSION") or "codetrail-rag"
    return (aux,) if scope == "aux" else (main, aux)


def _stop_timeout(environ: Mapping[str, str]) -> int:
    raw = (environ.get("AICODE_STOP_TIMEOUT") or "").strip()
    if not raw:
        return _DEFAULT_STOP_TIMEOUT
    if not raw.isdecimal() or int(raw) < 1:
        print(
            f"[!] AICODE_STOP_TIMEOUT 必須是正整數(拿到 {raw!r});改用預設 {_DEFAULT_STOP_TIMEOUT}s",
            file=sys.stderr,
        )
        return _DEFAULT_STOP_TIMEOUT
    return int(raw)


def _pane_pids(session: str) -> dict[int, str]:
    """kill-session 之前記下 session 內每個 pane 的 process(pid → session:window)。"""
    proc = subprocess.run(
        ["tmux", "list-panes", "-s", "-t", session, "-F", "#{pane_pid} #{window_name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {}
    tracked: dict[int, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split(maxsplit=1)
        if parts and parts[0].isdecimal():
            window = parts[1].strip() if len(parts) > 1 else "?"
            tracked[int(parts[0])] = f"{session}:{window}"
    return tracked


def _proc_state(pid: int) -> str:
    """'' = process 已消失;'Z' = zombie(已死待回收,收不到訊號);其他 = 存活。"""
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
    except OSError:
        return ""
    _, _, tail = stat.rpartition(")")  # comm 可含空白/括號,取最後一個 ')' 之後
    fields = tail.split()
    return fields[0] if fields else ""


def _is_llama_pid(pid: int) -> bool:
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return False
    args = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    return bool(args) and Path(args[0]).name.startswith("llama-server")


def _gpu_compute_pids() -> set[int] | None:
    """nvidia-smi compute list 上的所有 PID(= VRAM 尚未釋放)。None = 查不到。

    刻意不濾 process 名稱:zombie 的 /proc/<pid>/cmdline 讀不到,nvidia-smi
    的 process_name 會變空/不可辨識,按名稱過濾會把「還佔著 VRAM 的殭屍」
    誤判成已釋放(實測 GLM-5.2 的殭屍期長達 ~19s)。判斷歸屬用「pid 是否
    在 tracked 裡」就夠了。"""
    if not shutil.which("nvidia-smi"):
        return None
    proc = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return {int(token) for token in proc.stdout.split() if token.isdecimal()}


def _gpu_memory_line() -> str | None:
    if not shutil.which("nvidia-smi"):
        return None
    proc = subprocess.run(
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
    while True:
        alive = {pid for pid in tracked if _proc_state(pid) not in ("", "Z")}
        gpu = _gpu_compute_pids()
        holding = set(alive)
        if gpu is not None:
            holding |= {pid for pid in tracked if pid in gpu}
        if not holding:
            return []
        now = clock()
        if now >= deadline:
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
    # 設定檔壞掉時(手改壞 JSON、registry 失效)不能連「停止」都做不到:
    # 關 tmux session 不需要 profile,先關;之後的 port 檢查才需要 profile。
    profile = None
    profile_error: ProfileError | None = None
    try:
        profile = load_effective_profile(profile=args.profile)
    except ProfileError as exc:
        profile_error = exc
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "[!] deployment 設定載入失敗 → 退路模式:仍會關閉 tmux session,"
            "但略過 port 檢查。設定可用 ./set_config.sh(或 --restore-last-backup)修復。",
            file=sys.stderr,
        )

    tracked: dict[int, str] = {}
    if shutil.which("tmux"):
        for session in _sessions(args.scope):
            exists = subprocess.run(
                ["tmux", "has-session", "-t", session],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode == 0
            if exists:
                # 先記 pane PID 再 kill:kill 之後就查不到「該等誰退出」了。
                tracked.update(_pane_pids(session))
                kill = subprocess.run(
                    ["tmux", "kill-session", "-t", session],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if kill.returncode == 0:
                    print(f"[+] stopped tmux session {session!r}")
                else:
                    print(
                        f"[!] could not kill tmux session {session!r}: "
                        f"{(kill.stderr or kill.stdout).strip()}",
                        file=sys.stderr,
                    )
            else:
                print(f"[!] tmux session {session!r} does not exist")
    else:
        print("[!] tmux not found; checking profile ports only", file=sys.stderr)

    timeout = _stop_timeout(os.environ)
    stuck: list[int] = []
    if tracked:
        started = time.monotonic()
        stuck = _wait_released(tracked, timeout=timeout)
        if stuck:
            names = "、".join(f"{tracked[pid]}(PID={pid})" for pid in stuck)
            print(
                f"[!] {timeout}s 內仍未結束/未釋放 VRAM:{names} — process 可能卡在"
                "驅動清理;用 nvidia-smi 觀察,久候不退時考慮重開機。"
                "(等待上限可用 AICODE_STOP_TIMEOUT 調整)",
                file=sys.stderr,
            )
        else:
            print(f"[+] llama-server 已全部結束(等待 {time.monotonic() - started:.1f}s)")

    # port 檢查放在等待之後:垂死中的 listener 不會再誤報「rerun with --force」。
    if profile is not None:
        forced: dict[int, str] = {}
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
            stuck_forced = _wait_released(forced, timeout=timeout)
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
    return 1 if profile_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
