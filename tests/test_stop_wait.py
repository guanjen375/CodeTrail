"""stop_servers 的「等到 process 退出 + VRAM 釋放」邏輯(全部離線,fake clock)。

實測背景(RTX 5090 + GLM-5.2 --no-mmap --cpu-moe):tmux kill-session 後
llama-server 60ms 內變 zombie,但以 zombie 身分繼續佔著 25.7GB VRAM ~19 秒
(釋放 300GB host buffer 的退出路徑),nvidia-smi 的 compute list 才是
「乾淨」的權威判準 — 這組測試鎖住該行為契約。
"""
from __future__ import annotations

import signal

from scripts import launch_servers, stop_servers


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
