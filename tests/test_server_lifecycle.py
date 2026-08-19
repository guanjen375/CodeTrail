"""server 生命週期腳本:check_status 的 PID 判定,stop_servers 的等待/升級終止。

合併自 tests/test_check_status_script.py 與 tests/test_stop_wait.py(2026-08-20)。
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

from scripts import launch_servers, stop_servers

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_status.py"


def _write_fake_nvidia_smi(tmp_path: Path, output: str, exit_code: int = 0) -> None:
    executable = tmp_path / "nvidia-smi"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' {shlex.quote(output)}\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


FOUR_LLAMA_SERVERS = "\n".join(
    (
        "101, /opt/llama-server, GPU-aaaa, 17000",
        "102, /opt/llama-server, GPU-aaaa, 1100",
        "103, /opt/llama-server, GPU-aaaa, 900",
        "104, /opt/llama-server, GPU-aaaa, 7900",
        "999, /usr/bin/python3, GPU-aaaa, 500",
    )
)

THREE_UNIQUE_LLAMA_SERVERS = "\n".join(
    (
        "101, /opt/llama-server, GPU-aaaa, 12000",
        "101, /opt/llama-server, GPU-bbbb, 5000",
        "102, /opt/llama-server, GPU-aaaa, 1100",
        "103, /opt/llama-server, GPU-aaaa, 900",
    )
)


def test_check_status_passes_with_four_unique_llama_server_pids(tmp_path):
    _write_fake_nvidia_smi(tmp_path, FOUR_LLAMA_SERVERS)

    proc = _run(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("[GPU]") == 4
    assert "PID=101" in proc.stdout
    assert "GPU=GPU-aaaa" in proc.stdout
    assert "VRAM=17000 MiB" in proc.stdout
    assert "偵測到 4 個不同的 llama-server PID" in proc.stdout


def test_check_status_report_only_mode_does_not_fail_the_shell(tmp_path):
    _write_fake_nvidia_smi(tmp_path, THREE_UNIQUE_LLAMA_SERVERS)

    proc = _run(tmp_path)

    assert proc.returncode == 0
    assert "只偵測到 3 個不同的 llama-server PID" in proc.stderr
    assert "report-only mode: exit 0" in proc.stdout


def test_check_status_strict_mode_fails_for_too_few_unique_pids(tmp_path):
    _write_fake_nvidia_smi(tmp_path, THREE_UNIQUE_LLAMA_SERVERS)

    proc = _run(tmp_path, "--strict")

    assert proc.returncode == 1
    assert "只偵測到 3 個不同的 llama-server PID" in proc.stderr
    assert "report-only mode" not in proc.stdout


# --------------------------------------------------------------------------
# 併自 tests/test_stop_wait.py:stop_servers 的等待與強制終止。
# --------------------------------------------------------------------------
class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_wait_released_holds_until_zombie_leaves_gpu(monkeypatch):
    """zombie 還在 nvidia-smi 上 = VRAM 未釋放,必須繼續等;且不對 zombie 送訊號。"""
    clock = _Clock()
    monkeypatch.setattr(stop_servers, "_proc_state", lambda pid: "Z")
    monkeypatch.setattr(
        stop_servers, "_gpu_compute_pids", lambda: {123} if clock.now < 19 else set()
    )
    kills: list[tuple[int, int]] = []

    survivors = stop_servers._wait_released(
        {123: "codetrail-main:main"},
        timeout=120,
        clock=clock,
        sleep=clock.sleep,
        kill=lambda pid, sig: kills.append((pid, sig)),
    )

    assert survivors == []
    assert clock.now >= 19
    assert kills == []


def test_wait_released_returns_immediately_when_clean(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(stop_servers, "_proc_state", lambda pid: "")
    monkeypatch.setattr(stop_servers, "_gpu_compute_pids", lambda: set())

    survivors = stop_servers._wait_released(
        {123: "codetrail-main:main"}, timeout=120, clock=clock, sleep=clock.sleep
    )

    assert survivors == []
    assert clock.now == 0.0  # 一次都不用 sleep


def test_wait_released_escalates_term_then_kill_once_each(monkeypatch):
    """SIGHUP 沒讓它退出 → 10s 補 SIGTERM、25s 補 SIGKILL,各一次,不重複轟炸。"""
    clock = _Clock()
    monkeypatch.setattr(stop_servers, "_proc_state", lambda pid: "S")
    monkeypatch.setattr(stop_servers, "_is_llama_pid", lambda pid: True)
    monkeypatch.setattr(stop_servers, "_gpu_compute_pids", lambda: None)
    kills: list[tuple[int, int]] = []

    survivors = stop_servers._wait_released(
        {7: "codetrail-main:main"},
        timeout=40,
        clock=clock,
        sleep=clock.sleep,
        kill=lambda pid, sig: kills.append((pid, sig)),
    )

    assert survivors == [7]  # 超時仍存活 → 回報,exit code 交由 main() 轉非零
    assert kills.count((7, signal.SIGTERM)) == 1
    assert kills.count((7, signal.SIGKILL)) == 1
    assert kills.index((7, signal.SIGTERM)) < kills.index((7, signal.SIGKILL))


def test_wait_released_never_signals_reused_pid(monkeypatch):
    """PID 已被重用(cmdline 不是 llama-server)→ 絕不送訊號,超時如實回報。"""
    clock = _Clock()
    monkeypatch.setattr(stop_servers, "_proc_state", lambda pid: "S")
    monkeypatch.setattr(stop_servers, "_is_llama_pid", lambda pid: False)
    monkeypatch.setattr(stop_servers, "_gpu_compute_pids", lambda: set())
    kills: list[tuple[int, int]] = []

    survivors = stop_servers._wait_released(
        {7: "codetrail-rag:embed"},
        timeout=30,
        clock=clock,
        sleep=clock.sleep,
        kill=lambda pid, sig: kills.append((pid, sig)),
    )

    assert survivors == [7]
    assert kills == []


def test_wait_released_without_nvidia_smi_waits_on_process_exit(monkeypatch):
    """查不到 GPU(無 nvidia-smi)→ 至少等到 process 消失。"""
    clock = _Clock()
    monkeypatch.setattr(
        stop_servers, "_proc_state", lambda pid: "S" if clock.now < 3 else ""
    )
    monkeypatch.setattr(stop_servers, "_gpu_compute_pids", lambda: None)

    survivors = stop_servers._wait_released(
        {5: "codetrail-rag:rerank"}, timeout=120, clock=clock, sleep=clock.sleep
    )

    assert survivors == []
    assert clock.now >= 3


def test_stop_timeout_env_override_and_fallback(capsys):
    assert stop_servers._stop_timeout({}) == 120
    assert stop_servers._stop_timeout({"AICODE_STOP_TIMEOUT": "30"}) == 30
    assert stop_servers._stop_timeout({"AICODE_STOP_TIMEOUT": "abc"}) == 120
    assert "AICODE_STOP_TIMEOUT" in capsys.readouterr().err


def test_pane_pids_parses_tmux_output(monkeypatch):
    class _Result:
        returncode = 0
        stdout = "481939 main\n482001 embed\nnot-a-pid x\n"
        stderr = ""

    monkeypatch.setattr(
        stop_servers.subprocess, "run", lambda *args, **kwargs: _Result()
    )
    assert stop_servers._pane_pids("codetrail-main") == {
        481939: "codetrail-main:main",
        482001: "codetrail-main:embed",
    }


def test_pane_pids_empty_when_session_missing(monkeypatch):
    class _Result:
        returncode = 1
        stdout = ""
        stderr = "no such session"

    monkeypatch.setattr(
        stop_servers.subprocess, "run", lambda *args, **kwargs: _Result()
    )
    assert stop_servers._pane_pids("codetrail-main") == {}


def test_rollback_waits_for_vram_release(tmp_path, monkeypatch):
    """啟動失敗 rollback 也要等 VRAM 釋放:半載入的主模型不等,立刻重跑會誤判。"""

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        launch_servers.subprocess, "run", lambda *args, **kwargs: _Result()
    )
    monkeypatch.setattr(
        stop_servers, "_pane_pids", lambda session: {9: f"{session}:main"}
    )
    waited: list[tuple[dict[int, str], int]] = []

    def fake_wait(tracked, *, timeout, **_kwargs):
        waited.append((dict(tracked), timeout))
        return []

    monkeypatch.setattr(stop_servers, "_wait_released", fake_wait)

    launch_servers._rollback_started(
        RuntimeError("boom"),
        [],
        ["s-main"],
        {"main": "s-main", "aux": "s-aux"},
        {"HOME": str(tmp_path)},
    )

    assert waited == [({9: "s-main:main"}, 120)]
