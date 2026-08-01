"""launch_servers 的啟動失敗 rollback、動態 health timeout 與 shard 大小聚合。

全部離線:tmux / 檔案系統呼叫用 monkeypatch 或 tmp fixture。
"""
from __future__ import annotations

from pathlib import Path

from deployment_profile import ServiceProfile
from scripts import launch_servers

GIB = 1024**3


def _service(role: str = "main") -> ServiceProfile:
    return ServiceProfile(
        role=role,
        model="/tmp/model.gguf",
        port=8080,
        base_url="http://localhost:8080",
        gpu_role="main" if role == "main" else "aux",
        gpu="",
        ctx=65536,
        batch=None,
        ubatch=None,
        parameters={},
    )


def test_artifact_bytes_sums_all_shards(tmp_path):
    (tmp_path / "m-00001-of-00003.gguf").write_bytes(b"a" * 1000)
    (tmp_path / "m-00002-of-00003.gguf").write_bytes(b"b" * 1000)
    (tmp_path / "m-00003-of-00003.gguf").write_bytes(b"c" * 1000)
    (tmp_path / "unrelated.gguf").write_bytes(b"z" * 5000)

    assert launch_servers._artifact_bytes(tmp_path / "m-00001-of-00003.gguf") == 3000
    assert launch_servers._artifact_bytes(tmp_path / "unrelated.gguf") == 5000
    assert launch_servers._artifact_bytes(tmp_path / "missing.gguf") == 0


def test_health_timeout_scales_with_model_size():
    assert launch_servers._health_timeout("main", {}, 0) == 300
    assert launch_servers._health_timeout("main", {}, 100 * GIB) == 500
    assert launch_servers._health_timeout("main", {}, 1000 * GIB) == 1800  # 上限
    assert launch_servers._health_timeout("main", {"MAIN_HEALTH_TIMEOUT": "42"}, 10**13) == 42
    assert launch_servers._health_timeout("embedding", {}) == 60
    assert launch_servers._health_timeout("vl", {"RAG_HEALTH_TIMEOUT": "90"}) == 90


def test_rollback_saves_logs_and_kills_created_sessions(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = "fake server log line\n"
        stderr = ""

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return _Result()

    monkeypatch.setattr(launch_servers.subprocess, "run", fake_run)
    sessions = {"main": "s-main", "aux": "s-aux"}
    environ = {"HOME": str(tmp_path)}

    launch_servers._rollback_started(
        RuntimeError("boom"),
        [_service("main"), _service("embedding")],
        ["s-main", "s-aux"],
        sessions,
        environ,
    )

    main_log = tmp_path / ".local" / "state" / "codetrail" / "logs" / "main.log"
    embed_log = tmp_path / ".local" / "state" / "codetrail" / "logs" / "embedding.log"
    assert main_log.read_text(encoding="utf-8") == "fake server log line\n"
    assert embed_log.exists()

    captures = [cmd for cmd in calls if cmd[:2] == ["tmux", "capture-pane"]]
    assert any("s-main:main" in cmd for cmd in captures)
    kills = [cmd for cmd in calls if cmd[:2] == ["tmux", "kill-session"]]
    assert [cmd[3] for cmd in kills] == ["s-main", "s-aux"]


def test_rollback_respects_no_rollback_env(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr(launch_servers.subprocess, "run", fake_run)
    launch_servers._rollback_started(
        RuntimeError("boom"),
        [_service("main")],
        ["s-main"],
        {"main": "s-main", "aux": "s-aux"},
        {"HOME": str(tmp_path), "AICODE_NO_ROLLBACK": "1"},
    )
    assert calls == []  # 保留現場:不 capture、不 kill


def test_rollback_noop_when_nothing_created(tmp_path, monkeypatch):
    def fake_run(cmd, **_kwargs):
        raise AssertionError(f"不應呼叫 tmux:{cmd}")

    monkeypatch.setattr(launch_servers.subprocess, "run", fake_run)
    launch_servers._rollback_started(
        RuntimeError("boom"), [], [], {"main": "s-main", "aux": "s-aux"},
        {"HOME": str(tmp_path)},
    )
