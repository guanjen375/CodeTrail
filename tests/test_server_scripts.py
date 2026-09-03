"""server 啟停與 web 入口腳本:launch_servers 的啟動路徑與 rollback、check_status /
stop_servers 的生命週期判定、aicode_opencode_web 的 start / stop / --local 守門。

合併自 tests/test_server_launch.py、tests/test_server_lifecycle.py、
tests/test_web_server_scripts.py(2026-09-02)。前兩份本身又是 2026-08-20 併自
test_profile_server_launchers / test_launch_rollback / test_rag_server_scripts 與
test_check_status_script / test_stop_wait。

- 啟動路徑:profile → 啟動命令、失敗時的 rollback、RAG server 腳本契約。
- 生命週期:check_status 的 PID 判定,stop_servers 的等待/升級終止。
- aicode_opencode_web(start / stop / --local 全在同一入口):只走 --dry-run / --help /
  guard path,不需要 tmux / opencode / llama-server。
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from deployment_profile import RUNTIME_OVERRIDE_ENV_KEYS, ServiceProfile
from scripts import launch_servers, stop_servers

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── 原 test_server_launch.py:profile → 啟動命令、rollback、RAG server 腳本契約 ──

# scripts/*.sh 入口已移除;啟停一律直呼共用引擎 + --scope。
START_ALL = ("launch_servers.py", "--scope", "all")
START_MAIN = ("launch_servers.py", "--scope", "main")
START_AUX = ("launch_servers.py", "--scope", "aux")
STOP_ALL = ("stop_servers.py", "--scope", "all")
STOP_AUX = ("stop_servers.py", "--scope", "aux")


def _clean_env(tmp_path: Path) -> dict[str, str]:
    keys = {
        "AICODE_PROFILE",
        "AICODE_DEPLOYMENT_CONFIG",
        "AICODE_MODEL",
        "AICODE_MODEL_REGISTRY",
        "AICODE_MODEL_REGISTRY_FILE",
        "MAIN_GPU",
        "AUX_GPU",
        "EMBED_GPU",
        "RERANK_GPU",
        "VL_GPU",
        "CUDA_VISIBLE_DEVICES",
        "EMBED_MODEL",
        "RERANK_MODEL",
        "VL_GGUF",
        "VL_MMPROJ",
    }
    env = {key: value for key, value in os.environ.items() if key not in keys}
    env.update(
        {
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "MODELS_DIR": str(tmp_path / "models"),
            "LLAMA_BIN": str(tmp_path / "llama-server"),
        }
    )
    return env


def _run_launcher(entry: tuple[str, ...], tmp_path: Path, env_extra: dict[str, str], *args: str):
    env = _clean_env(tmp_path)
    env.update(env_extra)
    script, *base_args = entry
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script), *base_args, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _write_profile_fixture(tmp_path: Path, name: str) -> Path:
    """絕對路徑 profile fixture:繼承內建 safe-defaults,不覆寫任何 service。"""
    path = tmp_path / f"{name}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": name,
                "extends": "defaults",
                "description": "launcher test profile fixture",
                "verification": "unverified",
                "hardware": "test",
                "services": {},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_start_all_routes_main_and_all_aux_to_their_shared_gpus(tmp_path):
    main = tmp_path / "main.gguf"
    main.write_bytes(b"fixture")
    profile_path = _write_profile_fixture(tmp_path, "gpu-split")
    proc = _run_launcher(
        START_ALL,
        tmp_path,
        {
            "AICODE_PROFILE": str(profile_path),
            "AICODE_MODEL": str(main),
            "MAIN_GPU": "GPU-H200",
            "AUX_GPU": "GPU-RTX2000ADA",
        },
        "--dry-run",
    )

    assert proc.returncode == 0, proc.stderr
    assert "profile=gpu-split" in proc.stdout
    assert proc.stdout.count("CUDA_VISIBLE_DEVICES=GPU-H200") == 1
    assert proc.stdout.count("CUDA_VISIBLE_DEVICES=GPU-RTX2000ADA") == 3


def test_start_main_fails_loud_when_no_main_model_is_set(tmp_path):
    proc = _run_launcher(
        START_MAIN,
        tmp_path,
        {},
        "--dry-run",
    )

    assert proc.returncode != 0
    assert "main model is unset" in proc.stderr


def test_legacy_aux_launcher_env_names_still_override_profile(tmp_path):
    embed = tmp_path / "legacy-embed.gguf"
    rerank = tmp_path / "legacy-rerank.gguf"
    vl = tmp_path / "legacy-vl.gguf"
    mmproj = tmp_path / "legacy-mmproj.gguf"
    proc = _run_launcher(
        START_AUX,
        tmp_path,
        {
            "EMBED_MODEL": str(embed),
            "RERANK_MODEL": str(rerank),
            "VL_GGUF": str(vl),
            "VL_MMPROJ": str(mmproj),
            "EMBED_GPU": "0",
            "RERANK_GPU": "1",
            "VL_GPU": "2",
        },
        "--dry-run",
    )

    assert proc.returncode == 0, proc.stderr
    for path in (embed, rerank, vl, mmproj):
        assert str(path) in proc.stdout
    assert "CUDA_VISIBLE_DEVICES=0" in proc.stdout
    assert "CUDA_VISIBLE_DEVICES=1" in proc.stdout
    assert "CUDA_VISIBLE_DEVICES=2" in proc.stdout


def test_launcher_help_paths_are_offline(tmp_path):
    for entry in (START_MAIN, START_ALL, START_AUX, STOP_ALL, STOP_AUX):
        proc = _run_launcher(entry, tmp_path, {}, "--help")
        assert proc.returncode == 0, f"{entry}: {proc.stderr}"


def test_quit_still_kills_sessions_when_deployment_config_is_broken(tmp_path):
    """設定檔壞掉時 stop_servers 不能連 tmux session 都拒絕關(復原路徑不能死)。"""
    config_dir = tmp_path / ".config" / "codetrail"
    config_dir.mkdir(parents=True)
    (config_dir / "deployment.json").write_text("{not json", encoding="utf-8")

    proc = _run_launcher(
        STOP_ALL,
        tmp_path,
        {
            # 不存在的 session 名:只驗證退路流程,不動開發機上真的 codetrail session
            "MAIN_SESSION": "codetrail-test-none-main",
            "SESSION": "codetrail-test-none-rag",
        },
    )

    assert proc.returncode == 1  # 設定仍是壞的 → 非零提醒
    assert "退路模式" in proc.stderr
    assert "does not exist" in proc.stdout  # tmux session 檢查有跑(而非提前 return)


def test_launcher_rejects_duplicate_service_ports(tmp_path):
    deployment = tmp_path / "deployment.json"
    deployment.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "services": {
                    "reranker": {
                        "port": 8081,
                        "base_url": "http://localhost:8081",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    main = tmp_path / "main.gguf"
    main.write_bytes(b"fixture")

    proc = _run_launcher(
        START_ALL,
        tmp_path,
        {
            "AICODE_DEPLOYMENT_CONFIG": str(deployment),
            "AICODE_MODEL": str(main),
        },
        "--dry-run",
    )

    assert proc.returncode != 0
    assert "share localhost:8081" in proc.stderr


def test_legacy_vl_cpu_moe_config_gets_fit_off_and_a_warning(tmp_path):
    """既有 deployment(CPU-MoE + fit on + gpu_layers auto)不必重跑 set_config:

    llama.cpp 的 --fit 會因為 tensor override 而 abort,而它的預設值是 on ——
    launcher 必須明寫 --fit off(否則等同 on)、丟掉不會生效的 --fit-target,
    並且不能靜靜矯正:設定檔與實際行為不一致要講出來。
    """
    config = tmp_path / ".config" / "codetrail" / "deployment.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "defaults",
                "services": {
                    "vl": {
                        "parameters": {
                            "gpu_layers": "auto",
                            "parallel": 1,
                            "fit": "on",
                            "fit_target": 3072,
                            "n_cpu_moe": 35,
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    proc = _run_launcher(START_AUX, tmp_path, {}, "--dry-run")

    assert proc.returncode == 0, proc.stderr
    vl_command = next(
        line for line in proc.stdout.splitlines() if line.startswith("vl_command=")
    ).split(" ")
    assert vl_command[vl_command.index("--fit") + 1] == "off"
    assert vl_command.count("--fit") == 1
    assert "--fit-target" not in vl_command
    assert vl_command[vl_command.index("--n-cpu-moe") + 1] == "35"
    assert "放棄 --fit" in proc.stderr

    # embedding 沒有 CPU-MoE → 完全不受影響,也不該被警告
    assert "services.embedding" not in proc.stderr


def _write_vl_cpu_moe_config(tmp_path: Path, parameters: dict) -> None:
    config = tmp_path / ".config" / "codetrail" / "deployment.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "defaults",
                "services": {"vl": {"parameters": parameters}},
            }
        ),
        encoding="utf-8",
    )


def test_cpu_moe_without_explicit_fit_still_warns(tmp_path):
    """省略 fit 不代表沒事:llama.cpp 的預設就是 on,一樣會 abort。
    只留 fit_target 也會被丟掉,兩者都不能靜默矯正。"""
    _write_vl_cpu_moe_config(
        tmp_path, {"gpu_layers": 99, "parallel": 1, "fit_target": 3072, "n_cpu_moe": 4}
    )

    proc = _run_launcher(START_AUX, tmp_path, {}, "--dry-run")

    assert proc.returncode == 0, proc.stderr
    assert "fit 未設定(llama.cpp 預設即 on)" in proc.stderr
    assert "fit_target 3072(不會被保留)" in proc.stderr
    vl_command = next(
        line for line in proc.stdout.splitlines() if line.startswith("vl_command=")
    ).split(" ")
    assert vl_command[vl_command.index("--fit") + 1] == "off"
    assert "--fit-target" not in vl_command


def test_set_config_shaped_cpu_moe_config_is_not_warned(tmp_path):
    """set_config 產生的形狀(fit off / 明確 gpu_layers / 無 fit_target)沒有衝突,
    不該每次啟動都噴警告。"""
    _write_vl_cpu_moe_config(
        tmp_path, {"gpu_layers": 99, "parallel": 1, "fit": "off", "n_cpu_moe": 4}
    )

    proc = _run_launcher(START_AUX, tmp_path, {}, "--dry-run")

    assert proc.returncode == 0, proc.stderr
    assert "放棄 --fit" not in proc.stderr


def test_systemd_exec_path_also_warns_before_launching(tmp_path):
    """文件支援的 systemd 路徑(deployment_profile.py exec)只呼叫 build_server_command,
    少了警告就等於靜默矯正 —— 這裡釘住它會先印警告再 exec。"""
    _write_vl_cpu_moe_config(
        tmp_path,
        {"gpu_layers": "auto", "parallel": 1, "fit": "on", "fit_target": 3072, "n_cpu_moe": 4},
    )
    env = _clean_env(tmp_path)
    # exec 會 os.execvpe;指到一個一定不存在的 binary,警告仍必須先印出來。
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "deployment_profile.py"), "exec", "vl",
         "--llama-bin", str(tmp_path / "no-such-llama-server")],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=15, check=False,
    )

    assert "[deployment-profile] ⚠" in proc.stderr
    assert 'gpu_layers "auto"' in proc.stderr
    assert "重跑 ./set_config.sh" in proc.stderr


# --------------------------------------------------------------------------
# 併自 tests/test_launch_rollback.py:啟動失敗要收乾淨。
# --------------------------------------------------------------------------
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
    expected = Path(launch_servers.__file__).resolve().parent / "check_status.py"
    assert f"python3 {expected} --strict" in out
    assert "  ./scripts/" not in out


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


# --------------------------------------------------------------------------
# 併自 tests/test_rag_server_scripts.py。
# --------------------------------------------------------------------------
def _isolated_env(tmp_path: Path) -> dict[str, str]:
    """乾淨環境:不吃開發機真實的 ~/.config/codetrail 與 shell 覆寫變數。

    這兩個 dry-run 測試斷言的是 profile 預設值;繼承真實 HOME 會讓
    「本機重跑過 set_config」直接改掉測試結果(環境相依假失敗)。"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in set(RUNTIME_OVERRIDE_ENV_KEYS)
    }
    env.update({"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)})
    return env


def test_start_rag_servers_dry_run_uses_base_url_ports(tmp_path):
    env = {
        **_isolated_env(tmp_path),
        "LLAMA_BIN": str(tmp_path / "llama-server"),
        "MODELS_DIR": str(tmp_path / "models"),
        "AICODE_LLAMA_EMBED_BASE_URL": "http://127.0.0.1:18081",
        "AICODE_LLAMA_RERANK_BASE_URL": "http://localhost:18082",
        "AICODE_LLAMA_VL_BASE_URL": "http://127.0.0.1:18083",
        "EMBED_GPU": "0",
        "RERANK_GPU": "1",
        "VL_GPU": "2",
        "AICODE_RERANK_FALLBACK_POLICY": "error",
    }

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "launch_servers.py"),
         "--scope", "aux", "--dry-run"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "embed_base_url=http://127.0.0.1:18081" in proc.stdout
    assert "embed_host=127.0.0.1" in proc.stdout
    assert "embed_port=18081" in proc.stdout
    assert "rerank_base_url=http://localhost:18082" in proc.stdout
    assert "rerank_host=localhost" in proc.stdout
    assert "rerank_port=18082" in proc.stdout
    assert "vl_base_url=http://127.0.0.1:18083" in proc.stdout
    assert "vl_host=127.0.0.1" in proc.stdout
    assert "vl_port=18083" in proc.stdout
    assert "--port 18081" in proc.stdout
    assert "--port 18082" in proc.stdout
    assert "--port 18083" in proc.stdout
    assert "--mmproj" in proc.stdout
    assert "bge-reranker-v2-m3/bge-reranker-v2-m3-Q8_0.gguf" in proc.stdout
    assert "qwen3.5-9b/Qwen3.5-9B-Q6_K.gguf" in proc.stdout
    assert "qwen3.5-9b/mmproj-F16.gguf" in proc.stdout
    assert "CUDA_VISIBLE_DEVICES=0" in proc.stdout
    assert "CUDA_VISIBLE_DEVICES=1" in proc.stdout
    assert "CUDA_VISIBLE_DEVICES=2" in proc.stdout


def test_start_rag_servers_noncausal_models_use_full_physical_batch(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "launch_servers.py"),
         "--scope", "aux", "--dry-run"],
        cwd=str(REPO_ROOT),
        env={
            **_isolated_env(tmp_path),
            "LLAMA_BIN": str(tmp_path / "llama-server"),
            "MODELS_DIR": str(tmp_path / "models"),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    settings = dict(
        line.split("=", 1)
        for line in proc.stdout.splitlines()
        if "=" in line
    )

    # llama.cpp non-causal embedding/reranking inputs must fit in one physical
    # batch. Its default -ub 512 makes otherwise healthy servers return HTTP
    # 500 as soon as a RAG chunk is longer than 512 tokens.
    assert "-c 8192 -b 8192 -ub 8192" in settings["embed_command"]
    assert "-c 8192 -b 8192 -ub 8192" in settings["rerank_command"]
    assert "-ub 8192" not in settings["vl_command"]


# ── 原 test_server_lifecycle.py:check_status 的 PID 判定,stop_servers 的等待/升級終止 ──

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


def _run_check_status(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
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

    proc = _run_check_status(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("[GPU]") == 4
    assert "PID=101" in proc.stdout
    assert "GPU=GPU-aaaa" in proc.stdout
    assert "VRAM=17000 MiB" in proc.stdout
    assert "偵測到 4 個不同的 llama-server PID" in proc.stdout


def test_check_status_report_only_mode_does_not_fail_the_shell(tmp_path):
    _write_fake_nvidia_smi(tmp_path, THREE_UNIQUE_LLAMA_SERVERS)

    proc = _run_check_status(tmp_path)

    assert proc.returncode == 0
    assert "只偵測到 3 個不同的 llama-server PID" in proc.stderr
    assert "report-only mode: exit 0" in proc.stdout


def test_check_status_strict_mode_fails_for_too_few_unique_pids(tmp_path):
    _write_fake_nvidia_smi(tmp_path, THREE_UNIQUE_LLAMA_SERVERS)

    proc = _run_check_status(tmp_path, "--strict")

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


# ── 原 test_web_server_scripts.py:aicode_opencode_web 的 start / stop / --local 守門 ──

WEB_ENTRY = REPO_ROOT / "aicode_opencode_web"


def _run_aicode_web(script: Path, args, cwd, env_extra=None, timeout=10):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _tailscale_env(
    tmp_path: Path,
    *,
    output: str = "100.100.10.20",
    rc: int = 0,
    warning: str | None = None,
):
    """Return PATH env with an offline tailscale stub for `tailscale ip -4`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "tailscale"
    warning_line = f"  printf '%s\\n' {warning!r} >&2\n" if warning else ""
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"${1:-}\" = ip ] && [ \"${2:-}\" = -4 ]; then\n"
        f"{warning_line}"
        f"  printf '%s\\n' {output!r}\n"
        f"  exit {rc}\n"
        "fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    stub.chmod(0o700)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}


def _headless_runtime_env(tmp_path: Path):
    """Offline tmux/curl/ss stubs that model start → reuse → stop."""
    env = _tailscale_env(tmp_path)
    bin_dir = tmp_path / "bin"
    state = tmp_path / "tmux-session"
    calls = tmp_path / "tmux-calls"
    tmux = bin_dir / "tmux"
    tmux.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_TMUX_CALLS\"\n"
        "case \"${1:-}\" in\n"
        "  has-session) test -f \"$FAKE_TMUX_STATE\" ;;\n"
        "  new-session) : > \"$FAKE_TMUX_STATE\" ;;\n"
        "  show-options)\n"
        "    case \"$*\" in\n"
        "      *@codetrail_project*) printf '%s\\n' \"$FAKE_TMUX_PROJECT\" ;;\n"
        "      *@codetrail_host*) printf '%s\\n' 100.100.10.20 ;;\n"
        "      *@codetrail_port*) printf '%s\\n' 4096 ;;\n"
        "    esac\n"
        "    ;;\n"
        "  display-message) printf '%s\\n' 0 ;;\n"
        "  kill-session) rm -f \"$FAKE_TMUX_STATE\" ;;\n"
        "  set-option|send-keys|capture-pane) : ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    tmux.chmod(0o700)
    for name, body in (
        ("curl", "#!/usr/bin/env bash\nprintf '200'\n"),
        ("ss", "#!/usr/bin/env bash\nexit 0\n"),
    ):
        stub = bin_dir / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o700)
    env.update(
        {
            "FAKE_TMUX_STATE": str(state),
            "FAKE_TMUX_CALLS": str(calls),
            "FAKE_TMUX_PROJECT": str(tmp_path),
            # 這組測試只驗 tmux 包裝行為;前景 preflight 需要完整 aicode_opencode 環境,
            # 由 test_cli.py 的 AICODE_PREFLIGHT_ONLY 測試與下方 blocks 測試涵蓋。
            "AICODE_WEB_SKIP_PREFLIGHT": "1",
        }
    )
    return env, state, calls


def test_local_dry_run_resolves_project_port_session(tmp_path):
    proc = _run_aicode_web(
        WEB_ENTRY,
        ["--local", "--dry-run"],
        cwd=tmp_path,
        env_extra={"AICODE_WEB_PORT": "4096", "WEB_SESSION": "codetrail-web"},
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert f"project={os.path.realpath(tmp_path)}" in out
    assert "port=4096" in out
    assert "session=codetrail-web" in out
    assert "health_url=http://127.0.0.1:4096/" in out
    # launch 應該:把 CodeTrail env 明確 export 進 pane、cd 專案、exec aicode_opencode web
    assert "export AICODE_WEB_PORT=4096" in out
    assert f"export AICODE_ROOT={os.path.realpath(tmp_path)}" in out
    assert "&& exec " in out
    assert str(REPO_ROOT / "aicode_opencode") in out and "web" in out


def test_local_dry_run_respects_custom_port_and_session(tmp_path):
    proc = _run_aicode_web(
        WEB_ENTRY,
        ["--local", "--dry-run"],
        cwd=tmp_path,
        env_extra={"AICODE_WEB_PORT": "4097", "WEB_SESSION": "myweb"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "port=4097" in proc.stdout
    assert "session=myweb" in proc.stdout
    assert "health_url=http://127.0.0.1:4097/" in proc.stdout


def test_local_dry_run_forwards_extra_args(tmp_path):
    proc = _run_aicode_web(WEB_ENTRY, ["--local", "--dry-run", "--hostname", "0.0.0.0"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    launch = next(line for line in proc.stdout.splitlines() if line.startswith("launch="))
    assert launch.rstrip().endswith("aicode_opencode web --hostname 0.0.0.0")


def test_aicode_web_dry_run_binds_only_current_tailscale_ipv4(tmp_path):
    proc = _run_aicode_web(
        WEB_ENTRY,
        ["--dry-run", "--cors", "https://browser.example"],
        cwd=tmp_path,
        env_extra=_tailscale_env(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "exposure=tailscale" in out
    assert "bind_host=100.100.10.20" in out
    assert "browser_url=http://100.100.10.20:4096/" in out
    launch = next(line for line in out.splitlines() if line.startswith("launch="))
    assert "export AICODE_WEB_TAILSCALE_IP=100.100.10.20" in launch
    assert "export BROWSER=/bin/true" in launch
    assert "aicode_opencode web --cors https://browser.example --hostname 100.100.10.20" in launch
    assert "0.0.0.0" not in launch


def test_aicode_web_readme_symlink_resolves_codetrail_checkout(tmp_path):
    """README 安裝到 ~/.local/bin 後仍能找到 checkout 內的 scripts / aicode_opencode。"""
    fake_home = tmp_path / "home"
    installed_bin = fake_home / ".local" / "bin"
    installed_bin.mkdir(parents=True)
    installed_entry = installed_bin / "aicode_opencode_web"
    installed_entry.symlink_to(WEB_ENTRY)
    project = tmp_path / "project"
    project.mkdir()

    proc = _run_aicode_web(
        installed_entry,
        ["--dry-run"],
        cwd=project,
        env_extra={**_tailscale_env(tmp_path), "HOME": str(fake_home)},
    )

    assert proc.returncode == 0, proc.stderr
    assert f"project={project.resolve()}" in proc.stdout
    assert f"aicode_opencode={REPO_ROOT / 'aicode_opencode'}" in proc.stdout
    assert "bind_host=100.100.10.20" in proc.stdout


def test_aicode_web_ignores_tailscale_version_warning_before_ipv4(tmp_path):
    proc = _run_aicode_web(
        WEB_ENTRY,
        ["--dry-run"],
        cwd=tmp_path,
        env_extra=_tailscale_env(
            tmp_path,
            warning='Warning: client version "1.102.2" != tailscaled server version "1.98.4"',
        ),
    )
    assert proc.returncode == 0, proc.stderr
    assert "bind_host=100.100.10.20" in proc.stdout
    assert "browser_url=http://100.100.10.20:4096/" in proc.stdout


def test_aicode_web_reports_tailscale_not_connected(tmp_path):
    proc = _run_aicode_web(
        WEB_ENTRY,
        ["--dry-run"],
        cwd=tmp_path,
        env_extra=_tailscale_env(tmp_path, output="not logged in", rc=1),
    )
    assert proc.returncode != 0
    assert "Tailscale 尚未連線" in proc.stderr
    assert "not logged in" in proc.stderr


def test_aicode_web_rejects_ip_outside_exact_tailscale_cgnat_range(tmp_path):
    for bad_ip in ("192.168.1.50", "100.63.255.255", "100.128.0.1", "100.100.999.1"):
        proc = _run_aicode_web(
            WEB_ENTRY,
            ["--dry-run"],
            cwd=tmp_path,
            env_extra=_tailscale_env(tmp_path, output=bad_ip),
        )
        assert proc.returncode != 0, bad_ip
        assert "100.64.0.0/10" in proc.stderr or "無效 IPv4" in proc.stderr


def test_aicode_web_rejects_network_flag_override(tmp_path):
    proc = _run_aicode_web(
        WEB_ENTRY,
        ["--dry-run", "--hostname", "0.0.0.0"],
        cwd=tmp_path,
        env_extra=_tailscale_env(tmp_path),
    )
    assert proc.returncode != 0
    assert "自行鎖定 Tailscale IP" in proc.stderr

    equals_form = _run_aicode_web(
        WEB_ENTRY,
        ["--dry-run", "--mdns=true"],
        cwd=tmp_path,
        env_extra=_tailscale_env(tmp_path),
    )
    assert equals_form.returncode != 0
    assert "自行鎖定 Tailscale IP" in equals_form.stderr


def test_aicode_web_headless_start_is_idempotent_and_stop_uses_session_metadata(tmp_path):
    env, state, calls = _headless_runtime_env(tmp_path)

    first = _run_aicode_web(WEB_ENTRY, [], cwd=tmp_path, env_extra=env)
    assert first.returncode == 0, first.stderr
    assert state.exists()
    assert "B 機瀏覽器請直接開:http://100.100.10.20:4096/" in first.stdout
    assert "A 機不需要 GUI" in first.stdout
    first_calls = calls.read_text(encoding="utf-8")
    assert "new-session -d" in first_calls
    assert "remain-on-exit on" in first_calls
    assert "send-keys" in first_calls

    second = _run_aicode_web(WEB_ENTRY, [], cwd=tmp_path, env_extra=env)
    assert second.returncode == 0, second.stderr
    assert "web backend 已在執行" in second.stdout
    all_calls = calls.read_text(encoding="utf-8")
    assert all_calls.count("new-session -d") == 1

    stopped = _run_aicode_web(WEB_ENTRY, ["stop"], cwd=tmp_path, env_extra=env)
    assert stopped.returncode == 0, stopped.stderr
    assert not state.exists()
    assert "100.100.10.20:4096 已釋放" in stopped.stdout


def test_foreground_preflight_failure_blocks_backend_start(tmp_path):
    """前景 preflight 失敗必須就地擋下:不建 tmux session、印出指引。"""
    env, state, calls = _headless_runtime_env(tmp_path)
    env.pop("AICODE_WEB_SKIP_PREFLIGHT")
    bin_dir = tmp_path / "bin"
    opencode = bin_dir / "opencode"
    opencode.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    opencode.chmod(0o700)
    home = tmp_path / "home"
    home.mkdir()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            # 指到外部 provider → resolve_main_model 必定 fail-loud(不吃宿主機設定)
            "AICODE_MODEL": "openai/gpt-4",
        }
    )

    proc = _run_aicode_web(WEB_ENTRY, [], cwd=tmp_path, env_extra=env, timeout=60)

    assert proc.returncode != 0
    assert "preflight 未通過" in proc.stderr
    assert not state.exists()
    if calls.exists():
        assert "new-session" not in calls.read_text(encoding="utf-8")


def test_rejects_invalid_port(tmp_path):
    proc = _run_aicode_web(WEB_ENTRY, ["--local", "--dry-run"], cwd=tmp_path, env_extra={"AICODE_WEB_PORT": "abc"})
    assert proc.returncode != 0
    assert "AICODE_WEB_PORT" in proc.stderr


@pytest.mark.parametrize(
    "cwd",
    [
        pytest.param(REPO_ROOT / "scripts", id="scripts_dir"),
        pytest.param(REPO_ROOT, id="repo_root"),
    ],
)
def test_refuses_inside_codetrail_repo(cwd):
    """從 CodeTrail repo(repo_root)/ scripts 目錄(scripts_dir)跑要被拒
    (避免把工具 repo 當分析專案)。"""
    proc = _run_aicode_web(WEB_ENTRY, ["--local", "--dry-run"], cwd=cwd)
    assert proc.returncode != 0
    assert "CodeTrail repo" in proc.stderr


def test_refuses_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    proc = _run_aicode_web(WEB_ENTRY, ["--local", "--dry-run"], cwd=home, env_extra={"HOME": str(os.path.realpath(home))})
    assert proc.returncode != 0
    assert "HOME" in proc.stderr


def test_aicode_web_help_exits_zero():
    proc = _run_aicode_web(WEB_ENTRY, ["--help"], cwd=REPO_ROOT)
    assert proc.returncode == 0
    assert "aicode_opencode_web" in proc.stdout
    assert "用法" in proc.stdout
    assert "--tailscale" in proc.stdout
    assert "--local" in proc.stdout
    assert "stop" in proc.stdout


def test_stop_help_exits_zero():
    proc = _run_aicode_web(WEB_ENTRY, ["stop", "--help"], cwd=REPO_ROOT)
    assert proc.returncode == 0
    assert "用法" in proc.stdout


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        pytest.param(["stop", "--bogus"], "unknown argument", id="stop_unknown_arg"),
        pytest.param(["bogus"], "unknown subcommand", id="unknown_subcommand"),
    ],
)
def test_rejects_unknown_cli_input(args, needle):
    """stop 的未知參數(stop_unknown_arg)與未知子指令(unknown_subcommand)都要拒絕。"""
    proc = _run_aicode_web(WEB_ENTRY, args, cwd=REPO_ROOT)
    assert proc.returncode != 0
    assert needle in proc.stderr


def test_web_entry_is_executable():
    assert os.access(WEB_ENTRY, os.X_OK)
