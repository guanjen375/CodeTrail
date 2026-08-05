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


def test_start_role_pipes_server_output_to_persistent_log(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return _Result()

    monkeypatch.setattr(launch_servers.subprocess, "run", fake_run)
    log_dir = tmp_path / "logs"
    launch_servers._start_role(
        _service("main"), ["llama-server", "-m", "x"], "s-main",
        first_in_session=True, log_dir=log_dir,
    )

    # 從啟動第一刻就 pipe-pane 持續寫 log(llama-server 秒退時 capture-pane 抓不到)
    assert (log_dir / "main.log").exists()
    pipes = [cmd for cmd in calls if cmd[:2] == ["tmux", "pipe-pane"]]
    assert pipes, calls
    assert "-o" in pipes[0]
    assert "s-main:main" in pipes[0]
    assert "main.log" in pipes[0][-1]

    # 零 race 的關鍵順序:先開空 window → remain-on-exit → pipe-pane 接上
    # → 最後才 respawn 成真正的 llama-server(輸出從第一個 byte 就進 log)。
    kinds = [cmd[1] for cmd in calls if cmd[0] == "tmux"]
    assert kinds.index("pipe-pane") < kinds.index("respawn-window")
    assert kinds.index("set-option") < kinds.index("respawn-window")
    respawn = next(cmd for cmd in calls if cmd[:2] == ["tmux", "respawn-window"])
    assert "-k" in respawn
    assert respawn[-1] == "llama-server -m x"
    session_cmd = next(cmd for cmd in calls if cmd[:2] == ["tmux", "new-session"])
    assert session_cmd[-1] == "main"  # 先開空 window,不直接帶 llama-server 指令


def test_rollback_noop_when_nothing_created(tmp_path, monkeypatch):
    def fake_run(cmd, **_kwargs):
        raise AssertionError(f"不應呼叫 tmux:{cmd}")

    monkeypatch.setattr(launch_servers.subprocess, "run", fake_run)
    launch_servers._rollback_started(
        RuntimeError("boom"), [], [], {"main": "s-main", "aux": "s-aux"},
        {"HOME": str(tmp_path)},
    )


def _fake_profile():
    class _Profile:
        def service(self, role):
            return _service(role)

    return _Profile()


def _patch_launch_scaffolding(monkeypatch, tmp_path):
    """launch() 的離線鷹架:tmux/port/模型解析全部 stub,聚焦 session 記帳。"""
    binary = tmp_path / "llama-server"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(launch_servers.shutil, "which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr(launch_servers, "_check_port_collisions", lambda _services: None)
    monkeypatch.setattr(launch_servers, "_tmux_has_session", lambda _session: False)
    monkeypatch.setattr(
        launch_servers, "resolve_model_reference",
        lambda value, _env, must_exist=True: value,
    )
    monkeypatch.setattr(launch_servers, "_port_responds", lambda _service: False)
    monkeypatch.setattr(
        launch_servers, "_command_for",
        lambda service, binary_path, env, must_exist=True: ["llama-server"],
    )
    return {"LLAMA_BIN": str(binary), "HOME": str(tmp_path)}


def test_launch_registers_session_before_start_role_failure(monkeypatch, tmp_path):
    """new-session 成功、respawn-window 才失敗的半套狀態:session 必須「先登記
    再建立」,rollback 才會清掉它;否則殘留 session 會卡住下一次啟動。"""
    import subprocess as sp

    environ = _patch_launch_scaffolding(monkeypatch, tmp_path)

    def boom(*_args, **_kwargs):
        raise sp.CalledProcessError(1, ["tmux", "respawn-window"])

    monkeypatch.setattr(launch_servers, "_start_role", boom)
    rollbacks: list[list[str]] = []
    monkeypatch.setattr(
        launch_servers, "_rollback_started",
        lambda reason, roles, created, sessions, env: rollbacks.append(list(created)),
    )

    try:
        launch_servers.launch(_fake_profile(), ["main"], environ, dry_run=False)
    except sp.CalledProcessError:
        pass
    else:
        raise AssertionError("expected CalledProcessError to propagate")
    assert rollbacks == [["codetrail-main"]]  # 剛建立(或建立中)的 session 已在清單


def test_launch_rolls_back_on_keyboard_interrupt(monkeypatch, tmp_path):
    """Ctrl-C(最常發生在等 health 的幾分鐘)也要走 rollback,不留殘存 session。"""
    environ = _patch_launch_scaffolding(monkeypatch, tmp_path)
    monkeypatch.setattr(launch_servers, "_start_role", lambda *args, **kwargs: None)

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(launch_servers, "_wait_for_health", interrupted)
    rollbacks: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        launch_servers, "_rollback_started",
        lambda reason, roles, created, sessions, env: rollbacks.append((str(reason), list(created))),
    )

    try:
        launch_servers.launch(_fake_profile(), ["main"], environ, dry_run=False)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("expected KeyboardInterrupt to propagate")
    assert rollbacks and rollbacks[0][1] == ["codetrail-main"]
    assert "Ctrl-C" in rollbacks[0][0]


def test_main_returns_130_on_keyboard_interrupt(monkeypatch, capsys):
    """CLI 收 Ctrl-C:乾淨訊息 + exit 130,不噴 traceback。"""

    def interrupted(_env, profile=None):
        raise KeyboardInterrupt()

    monkeypatch.setattr(launch_servers, "load_effective_profile", interrupted)
    rc = launch_servers.main(["--scope", "all"])
    assert rc == 130
    assert "已中斷" in capsys.readouterr().err


def test_ready_message_uses_absolute_status_path(monkeypatch, tmp_path, capsys):
    """啟動成功訊息的 check-status 提示必須是絕對路徑(常從 $HOME 執行)。"""
    environ = _patch_launch_scaffolding(monkeypatch, tmp_path)
    monkeypatch.setattr(launch_servers, "_start_role", lambda *args, **kwargs: None)
    monkeypatch.setattr(launch_servers, "_wait_for_health", lambda *args, **kwargs: None)

    launch_servers.launch(_fake_profile(), ["main"], environ, dry_run=False)
    out = capsys.readouterr().out
    assert "CodeTrail model servers ready." in out
    expected = Path(launch_servers.__file__).resolve().parent / "check-status.sh"
    assert f"{expected} --strict" in out
    assert "  ./scripts/check-status.sh" not in out


def test_start_role_warns_when_pipe_pane_fails(tmp_path, monkeypatch, capsys):
    """pipe-pane 接不上不该無聲吞掉:啟動照常,但要警告 logs 會沒內容。"""

    def fake_run(cmd, **_kwargs):
        class _Result:
            returncode = 1 if cmd[:2] == ["tmux", "pipe-pane"] else 0
            stdout = ""
            stderr = "pipe boom" if cmd[:2] == ["tmux", "pipe-pane"] else ""

        return _Result()

    monkeypatch.setattr(launch_servers.subprocess, "run", fake_run)
    launch_servers._start_role(
        _service("main"), ["llama-server", "-m", "x"], "s-main",
        first_in_session=True, log_dir=tmp_path / "logs",
    )
    err = capsys.readouterr().err
    assert "pipe-pane" in err
    assert "logs main 將看不到輸出" in err


def test_start_role_warns_when_log_dir_unwritable(tmp_path, monkeypatch, capsys):
    """log 檔建不出來也一樣:不擋啟動,但必須告知 logs 不可用,不能佯稱 log 就緒。"""

    def fake_run(cmd, **_kwargs):
        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr(launch_servers.subprocess, "run", fake_run)
    blocker = tmp_path / "blocked"
    blocker.write_text("file, not dir", encoding="utf-8")
    launch_servers._start_role(
        _service("main"), ["llama-server", "-m", "x"], "s-main",
        first_in_session=True, log_dir=blocker / "logs",
    )
    err = capsys.readouterr().err
    assert "無法建立 main 的 log 檔" in err
