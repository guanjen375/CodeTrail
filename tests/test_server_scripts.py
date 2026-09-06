"""server 啟停腳本:launch_servers 的啟動路徑與 rollback、check_status /
stop_servers 的生命週期判定。

合併自 tests/test_server_launch.py、tests/test_server_lifecycle.py(2026-09-02);
兩份本身又是 2026-08-20 併自 test_profile_server_launchers / test_launch_rollback /
test_rag_server_scripts 與 test_check_status_script / test_stop_wait。
`aicode_web` 那一段隨網頁前端一起刪除(介面只剩 `aicode`)。

- 啟動路徑:profile → 啟動命令、失敗時的 rollback、RAG server 腳本契約。
- 生命週期:check_status 的 PID 判定,stop_servers 的等待/升級終止。
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

import pytest

import process_env
from deployment_profile import TMUX_SESSIONS, ServiceProfile
from scripts import launch_servers, stop_servers

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── 原 test_server_launch.py:profile → 啟動命令、rollback、RAG server 腳本契約 ──

# scripts/*.sh 入口已移除;啟停一律直呼共用引擎 + --scope。
START_ALL = ("launch_servers.py", "--scope", "all")
START_MAIN = ("launch_servers.py", "--scope", "main")
START_AUX = ("launch_servers.py", "--scope", "aux")
STOP_ALL = ("stop_servers.py", "--scope", "all")
STOP_AUX = ("stop_servers.py", "--scope", "aux")

#: 這些名字在這一代**沒有任何作用**。測試把它們設進子行程的殼層,是為了證明
#: 「設了也不會生效」—— 同一台機器上另一份安裝的 `~/start.sh` export 的就是這些,
#: 只要有一個還被讀,使用者就會「以為在跑 A、實際在跑 B」而且完全無聲。
LEGACY_SHELL_OVERRIDES = {
    "AICODE_PROFILE": "/shell/profile.json",
    "AICODE_MODEL": "/shell/never-used.gguf",
    "AICODE_DEPLOYMENT_CONFIG": "/shell/deployment.json",
    "AICODE_MODEL_REGISTRY_FILE": "/shell/models.json",
    "AICODE_MAIN_CTX": "111",
    "MAIN_CTX": "222",
    "MAIN_GPU": "SHELL-MAIN-GPU",
    "AUX_GPU": "SHELL-AUX-GPU",
    "EMBED_GPU": "SHELL-EMBED-GPU",
    "RERANK_GPU": "SHELL-RERANK-GPU",
    "VL_GPU": "SHELL-VL-GPU",
    "CUDA_VISIBLE_DEVICES": "9",
    "EMBED_MODEL": "/shell/embed.gguf",
    "RERANK_MODEL": "/shell/rerank.gguf",
    "VL_GGUF": "/shell/vl.gguf",
    "VL_MMPROJ": "/shell/mmproj.gguf",
    "LLAMA_BIN": "/shell/llama-server",
    "MODELS_DIR": "/shell/models",
    "MAIN_SESSION": "shell-main-session",
    "SESSION": "shell-generic-session",
    "AUX_SESSION": "shell-aux-session",
    "MAIN_HEALTH_TIMEOUT": "1",
    "RAG_HEALTH_TIMEOUT": "1",
    "AICODE_NO_ROLLBACK": "1",
    "AICODE_STOP_TIMEOUT": "1",
    "EXPECTED_LLAMA_SERVERS": "99",
    "AICODE_STATUS_PROC_ROOT": "/shell/proc",
    "AICODE_STATUS_SNAPSHOT": "/shell/snapshot.json",
    "AICODE_RERANK_FALLBACK_POLICY": "main_model",
}


def _shell_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """子行程的殼層環境:tmp HOME + 整組已經沒有作用的舊變數。"""
    return {
        **os.environ,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        **LEGACY_SHELL_OVERRIDES,
        **extra,
    }


def _write_deployment(tmp_path: Path, services: dict | None = None, **top) -> Path:
    """tmp HOME 的 `~/.config/codetrail/deployment.json` —— 設定的唯一來源。"""
    path = tmp_path / ".config" / "codetrail" / "deployment.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {"schema_version": 1, "profile": "defaults", **top}
    if services is not None:
        payload["services"] = services
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run_launcher(entry: tuple[str, ...], tmp_path: Path, *args: str):
    env = _shell_env(tmp_path)
    script, *base_args = entry
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script), *base_args, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _launch_args(*argv: str) -> argparse.Namespace:
    """launcher 的解析結果。設定經 argv 進來,測試也照同一條路。"""
    return launch_servers._parser().parse_args(list(argv))


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
    """GPU 分配只來自 argv 與 `deployment.json`;殼層裡同名的 `MAIN_GPU` /
    `AUX_GPU` / `CUDA_VISIBLE_DEVICES` 一個都不算數。"""
    main = tmp_path / "main.gguf"
    main.write_bytes(b"fixture")
    profile_path = _write_profile_fixture(tmp_path, "gpu-split")
    proc = _run_launcher(
        START_ALL,
        tmp_path,
        "--dry-run",
        "--profile", str(profile_path),
        "--main-model", str(main),
        "--main-gpu", "GPU-H200",
        "--aux-gpu", "GPU-RTX2000ADA",
    )

    assert proc.returncode == 0, proc.stderr
    assert "profile=gpu-split" in proc.stdout
    assert proc.stdout.count("CUDA_VISIBLE_DEVICES=GPU-H200") == 1
    assert proc.stdout.count("CUDA_VISIBLE_DEVICES=GPU-RTX2000ADA") == 3
    for dead in ("SHELL-MAIN-GPU", "SHELL-AUX-GPU", "CUDA_VISIBLE_DEVICES=9"):
        assert dead not in proc.stdout


def test_start_main_fails_loud_when_no_main_model_is_set(tmp_path):
    """殼層的 `AICODE_MODEL` 不是來源:沒有檔案也沒有 `--main-model` 就要失敗。"""
    proc = _run_launcher(
        START_MAIN,
        tmp_path,
        "--dry-run",
    )

    assert proc.returncode != 0
    assert "main model is unset" in proc.stderr
    assert "never-used.gguf" not in proc.stdout


def test_aux_models_and_gpus_come_from_the_deployment_file(tmp_path):
    """三顆附屬模型與它們的 GPU 來自 `deployment.json`(以前是四個 `*_MODEL` /
    三個 `*_GPU` 環境變數;那些名字現在只證明自己無效)。"""
    embed = tmp_path / "file-embed.gguf"
    rerank = tmp_path / "file-rerank.gguf"
    vl = tmp_path / "file-vl.gguf"
    mmproj = tmp_path / "file-mmproj.gguf"
    _write_deployment(
        tmp_path,
        {
            "embedding": {"model": str(embed), "gpu": "0"},
            "reranker": {"model": str(rerank), "gpu": "1"},
            "vl": {"model": str(vl), "mmproj": str(mmproj), "gpu": "2"},
        },
    )

    proc = _run_launcher(START_AUX, tmp_path, "--dry-run")

    assert proc.returncode == 0, proc.stderr
    for path in (embed, rerank, vl, mmproj):
        assert str(path) in proc.stdout
    assert "CUDA_VISIBLE_DEVICES=0" in proc.stdout
    assert "CUDA_VISIBLE_DEVICES=1" in proc.stdout
    assert "CUDA_VISIBLE_DEVICES=2" in proc.stdout
    assert "/shell/" not in proc.stdout


def test_launcher_help_paths_are_offline(tmp_path):
    for entry in (START_MAIN, START_ALL, START_AUX, STOP_ALL, STOP_AUX):
        proc = _run_launcher(entry, tmp_path, "--help")
        assert proc.returncode == 0, f"{entry}: {proc.stderr}"


def test_quit_still_kills_sessions_when_deployment_config_is_broken(tmp_path):
    """設定檔壞掉時 stop_servers 不能連 tmux session 都拒絕關(復原路徑不能死)。"""
    config_dir = tmp_path / ".config" / "codetrail"
    config_dir.mkdir(parents=True)
    (config_dir / "deployment.json").write_text("{not json", encoding="utf-8")

    proc = _run_launcher(
        STOP_ALL,
        tmp_path,
        # 不存在的 session 名:只驗證退路流程,不動開發機上真的 codetrail session
        "--main-session", "codetrail-test-none-main",
        "--aux-session", "codetrail-test-none-rag",
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
        "--dry-run",
        "--deployment-config", str(deployment),
        "--main-model", str(main),
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

    proc = _run_launcher(START_AUX, tmp_path, "--dry-run")

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

    proc = _run_launcher(START_AUX, tmp_path, "--dry-run")

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

    proc = _run_launcher(START_AUX, tmp_path, "--dry-run")

    assert proc.returncode == 0, proc.stderr
    assert "放棄 --fit" not in proc.stderr


def test_systemd_exec_path_also_warns_before_launching(tmp_path):
    """文件支援的 systemd 路徑(deployment_profile.py exec)只呼叫 build_server_command,
    少了警告就等於靜默矯正 —— 這裡釘住它會先印警告再 exec。"""
    _write_vl_cpu_moe_config(
        tmp_path,
        {"gpu_layers": "auto", "parallel": 1, "fit": "on", "fit_target": 3072, "n_cpu_moe": 4},
    )
    env = _shell_env(tmp_path)
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
    assert launch_servers._health_timeout("main", None, 0) == 300
    assert launch_servers._health_timeout("main", None, 100 * GIB) == 500
    assert launch_servers._health_timeout("main", None, 1000 * GIB) == 1800  # 上限
    assert launch_servers._health_timeout("main", 42, 10**13) == 42
    assert launch_servers._health_timeout("embedding", None) == 60
    assert launch_servers._health_timeout("vl", 90) == 90
    # 覆寫來自 `--health-timeout`,而且 0 / 負數在解析階段就被擋下。
    assert _launch_args("--scope", "all").health_timeout is None
    assert _launch_args("--scope", "all", "--health-timeout", "42").health_timeout == 42
    with pytest.raises(SystemExit):
        _launch_args("--scope", "all", "--health-timeout", "0")


def _fake_tmux(monkeypatch, calls: list[list[str]], *, stdout: str = "") -> None:
    """tmux 替身:每個 spawn 都經 `process_env.run`,所以換掉它就等於換掉 tmux。"""

    class _Result:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        result = _Result()
        result.stdout = stdout
        return result

    monkeypatch.setattr(launch_servers.process_env, "run", fake_run)


def test_rollback_saves_logs_and_kills_created_sessions(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    _fake_tmux(monkeypatch, calls, stdout="fake server log line\n")
    sessions = {"main": "s-main", "aux": "s-aux"}
    log_dir = launch_servers._state_log_dir({"HOME": str(tmp_path)})

    launch_servers._rollback_started(
        RuntimeError("boom"),
        [_service("main"), _service("embedding")],
        ["s-main", "s-aux"],
        sessions,
        log_dir=log_dir,
    )

    main_log = tmp_path / ".local" / "state" / "codetrail" / "logs" / "main.log"
    embed_log = tmp_path / ".local" / "state" / "codetrail" / "logs" / "embedding.log"
    assert main_log.read_text(encoding="utf-8") == "fake server log line\n"
    assert embed_log.exists()

    captures = [cmd for cmd in calls if cmd[:2] == ["tmux", "capture-pane"]]
    assert any("s-main:main" in cmd for cmd in captures)
    kills = [cmd for cmd in calls if cmd[:2] == ["tmux", "kill-session"]]
    assert [cmd[3] for cmd in kills] == ["s-main", "s-aux"]


def test_rollback_respects_the_keep_on_failure_flag(tmp_path, monkeypatch):
    """保留現場改由 `--keep-on-failure` 決定;殼層變數不再有這個開關。"""
    calls: list[list[str]] = []
    _fake_tmux(monkeypatch, calls)
    launch_servers._rollback_started(
        RuntimeError("boom"),
        [_service("main")],
        ["s-main"],
        {"main": "s-main", "aux": "s-aux"},
        log_dir=tmp_path / "logs",
        keep_on_failure=True,
    )
    assert calls == []  # 保留現場:不 capture、不 kill
    assert not (tmp_path / "logs").exists()
    assert _launch_args("--scope", "all").keep_on_failure is False
    assert _launch_args("--scope", "all", "--keep-on-failure").keep_on_failure is True


def test_start_role_pipes_server_output_to_persistent_log(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    _fake_tmux(monkeypatch, calls)
    log_dir = tmp_path / "logs"
    pane = launch_servers._pane_command("main", _launch_args("--scope", "main"))
    launch_servers._start_role(
        _service("main"), pane, "s-main",
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
    # pane 跑的是 exec choke point,不是 llama-server 本身(B-03)。
    assert respawn[-1] == pane
    assert "deployment_profile.py exec main" in respawn[-1]
    session_cmd = next(cmd for cmd in calls if cmd[:2] == ["tmux", "new-session"])
    assert session_cmd[-1] == "main"  # 先開空 window,不直接帶 llama-server 指令


def test_rollback_noop_when_nothing_created(tmp_path, monkeypatch):
    def fake_run(cmd, **_kwargs):
        raise AssertionError(f"不應呼叫 tmux:{cmd}")

    monkeypatch.setattr(launch_servers.process_env, "run", fake_run)
    launch_servers._rollback_started(
        RuntimeError("boom"), [], [], {"main": "s-main", "aux": "s-aux"},
        log_dir=tmp_path / "logs",
    )


def _fake_profile(llama_bin: Path | str = "/bin/true"):
    # class body 裡 `llama_bin = str(llama_bin)` 會把左值宣告成 class 名稱空間的名字,
    # 右邊的查找不會回到外層函式的參數 → NameError。先在函式層綁成另一個名字。
    binary = str(llama_bin)

    class _Profile:
        llama_bin = binary
        registry_file = None

        def service(self, role):
            return _service(role)

    return _Profile()


def _patch_launch_scaffolding(monkeypatch, tmp_path, *argv: str):
    """launch() 的離線鷹架:tmux/port/模型解析全部 stub,聚焦 session 記帳。

    回傳 `(profile, args)`:設定現在只從 `deployment.json` 與 argv 進來,
    所以 launch() 收的是解析後的 namespace,不再是一份環境。
    """
    binary = tmp_path / "llama-server"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(launch_servers.shutil, "which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr(launch_servers, "_check_port_collisions", lambda _services: None)
    monkeypatch.setattr(launch_servers, "_tmux_has_session", lambda _session: False)
    monkeypatch.setattr(
        launch_servers, "resolve_model_reference",
        lambda value, environ=None, *, must_exist=False, registry_file=None: value,
    )
    monkeypatch.setattr(launch_servers, "_port_responds", lambda _service: False)
    return _fake_profile(binary), _launch_args(*(argv or ("--scope", "main")))


def test_launch_registers_session_before_start_role_failure(monkeypatch, tmp_path):
    """new-session 成功、respawn-window 才失敗的半套狀態:session 必須「先登記
    再建立」,rollback 才會清掉它;否則殘留 session 會卡住下一次啟動。"""
    profile, args = _patch_launch_scaffolding(monkeypatch, tmp_path)

    def boom(*_args, **_kwargs):
        raise process_env.CalledProcessError(1, ["tmux", "respawn-window"])

    monkeypatch.setattr(launch_servers, "_start_role", boom)
    rollbacks: list[list[str]] = []
    monkeypatch.setattr(
        launch_servers, "_rollback_started",
        lambda reason, roles, created, sessions, **_kw: rollbacks.append(list(created)),
    )

    try:
        launch_servers.launch(profile, ["main"], args)
    except process_env.CalledProcessError:
        pass
    else:
        raise AssertionError("expected CalledProcessError to propagate")
    assert rollbacks == [["codetrail-main"]]  # 剛建立(或建立中)的 session 已在清單


def test_launch_rolls_back_on_keyboard_interrupt(monkeypatch, tmp_path):
    """Ctrl-C(最常發生在等 health 的幾分鐘)也要走 rollback,不留殘存 session。"""
    profile, args = _patch_launch_scaffolding(monkeypatch, tmp_path)
    monkeypatch.setattr(launch_servers, "_start_role", lambda *args, **kwargs: None)

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(launch_servers, "_wait_for_health", interrupted)
    rollbacks: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        launch_servers, "_rollback_started",
        lambda reason, roles, created, sessions, **_kw: rollbacks.append((str(reason), list(created))),
    )

    try:
        launch_servers.launch(profile, ["main"], args)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("expected KeyboardInterrupt to propagate")
    assert rollbacks and rollbacks[0][1] == ["codetrail-main"]
    assert "Ctrl-C" in rollbacks[0][0]


def test_main_returns_130_on_keyboard_interrupt(monkeypatch, capsys):
    """CLI 收 Ctrl-C:乾淨訊息 + exit 130,不噴 traceback。"""

    def interrupted(**_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(launch_servers, "load_effective_profile", interrupted)
    rc = launch_servers.main(["--scope", "all"])
    assert rc == 130
    assert "已中斷" in capsys.readouterr().err


def test_ready_message_uses_absolute_status_path(monkeypatch, tmp_path, capsys):
    """啟動成功訊息的 check-status 提示必須是絕對路徑(常從 $HOME 執行)。"""
    profile, args = _patch_launch_scaffolding(monkeypatch, tmp_path)
    monkeypatch.setattr(launch_servers, "_start_role", lambda *args, **kwargs: None)
    monkeypatch.setattr(launch_servers, "_wait_for_health", lambda *args, **kwargs: None)

    launch_servers.launch(profile, ["main"], args)
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

    monkeypatch.setattr(launch_servers.process_env, "run", fake_run)
    launch_servers._start_role(
        _service("main"), "python3 deployment_profile.py exec main", "s-main",
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

    monkeypatch.setattr(launch_servers.process_env, "run", fake_run)
    blocker = tmp_path / "blocked"
    blocker.write_text("file, not dir", encoding="utf-8")
    launch_servers._start_role(
        _service("main"), "python3 deployment_profile.py exec main", "s-main",
        first_in_session=True, log_dir=blocker / "logs",
    )
    err = capsys.readouterr().err
    assert "無法建立 main 的 log 檔" in err


# --------------------------------------------------------------------------
# 併自 tests/test_rag_server_scripts.py。
# --------------------------------------------------------------------------
def test_start_rag_servers_dry_run_uses_base_url_ports(tmp_path):
    """port / host / GPU 全部來自 `deployment.json`(以前是三個 base_url 環境變數
    加三個 `*_GPU`)。"""
    _write_deployment(
        tmp_path,
        {
            "embedding": {"port": 18081, "base_url": "http://127.0.0.1:18081", "gpu": "0"},
            "reranker": {"port": 18082, "base_url": "http://localhost:18082", "gpu": "1"},
            "vl": {"port": 18083, "base_url": "http://127.0.0.1:18083", "gpu": "2"},
        },
    )

    proc = _run_launcher(START_AUX, tmp_path, "--dry-run")

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
    # dry-run 不再印已刪除的 rerank fallback policy(唯一來源是 client.json)。
    assert "rerank_fallback_policy" not in proc.stdout


def test_start_rag_servers_noncausal_models_use_full_physical_batch(tmp_path):
    proc = _run_launcher(START_AUX, tmp_path, "--dry-run")

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


def _write_fake_bin(directory: Path, name: str, body: str) -> Path:
    """PATH 上的假外部命令(tmux / nvidia-smi / ss);測試絕不呼叫真的那幾支。"""
    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / name
    executable.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _write_fake_nvidia_smi(tmp_path: Path, output: str, exit_code: int = 0) -> None:
    _write_fake_bin(
        tmp_path,
        "nvidia-smi",
        f"printf '%s\\n' {shlex.quote(output)}\nexit {exit_code}",
    )


def _run_check_status(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """PATH 前置假 nvidia-smi;`--proc-root` 指向空目錄,不去讀真的 /proc。"""
    proc_root = tmp_path / "proc"
    proc_root.mkdir(exist_ok=True)
    env = {
        **_shell_env(tmp_path),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--proc-root", str(proc_root), *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
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


def test_stop_timeout_comes_from_argv_with_a_fixed_default():
    """等 VRAM 釋放的上限只有兩個來源:`--timeout` 與 repo 常數。壞值在解析階段
    就 fail-loud,而不是靜靜退回預設值(以前殼層給錯字串只印一行警告)。"""
    parser = stop_servers._parser()
    assert stop_servers.DEFAULT_STOP_TIMEOUT == 120
    assert parser.parse_args(["--scope", "all"]).timeout == 120
    assert parser.parse_args(["--scope", "all", "--timeout", "30"]).timeout == 30
    for bad in ("abc", "0", "-5"):
        with pytest.raises(SystemExit):
            parser.parse_args(["--scope", "all", "--timeout", bad])


def test_pane_pids_parses_tmux_output(monkeypatch):
    class _Result:
        returncode = 0
        stdout = "481939 main\n482001 embed\nnot-a-pid x\n"
        stderr = ""

    monkeypatch.setattr(
        stop_servers.process_env, "run", lambda *args, **kwargs: _Result()
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
        stop_servers.process_env, "run", lambda *args, **kwargs: _Result()
    )
    assert stop_servers._pane_pids("codetrail-main") == {}


def test_rollback_waits_for_vram_release(tmp_path, monkeypatch):
    """啟動失敗 rollback 也要等 VRAM 釋放:半載入的主模型不等,立刻重跑會誤判。"""

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        launch_servers.process_env, "run", lambda *args, **kwargs: _Result()
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
        log_dir=tmp_path / "logs",
    )

    assert waited == [({9: "s-main:main"}, stop_servers.DEFAULT_STOP_TIMEOUT)]
    assert waited[0][1] == 120


# --------------------------------------------------------------------------
# B-03 的三條契約:pane 一律經 exec choke point、pane 環境乾淨、
# stop / status 只認 argv 與 repo 常數。全部離線(假 tmux / nvidia-smi / ss /
# llama-server + tmp HOME),不碰真的服務。
# --------------------------------------------------------------------------

@pytest.mark.smoke
def test_the_pane_runs_the_exec_choke_point_with_the_loader_argv(monkeypatch, tmp_path):
    """tmux pane 的環境 = tmux server 的全域環境 + session 環境,launcher 的行程
    環境管不到已經在跑的 daemon。所以 pane 裡最後跑的必須是
    `deployment_profile.py exec <role>`(環境由它在 pane 內算),而且 launcher 收到的
    loader 旗標要原封不動轉過去 —— 讓 pane 自己重讀一次檔案會把 argv 覆寫弄丟。
    """
    profile_path = _write_profile_fixture(tmp_path, "pane-fixture")
    llama = tmp_path / "llama-server"
    profile, args = _patch_launch_scaffolding(
        monkeypatch, tmp_path,
        "--scope", "main",
        "--profile", str(profile_path),
        "--llama-bin", str(llama),
        "--main-gpu", "GPU-7",
    )
    monkeypatch.setattr(launch_servers, "_wait_for_health", lambda *a, **k: None)
    monkeypatch.setattr(launch_servers, "_state_log_dir", lambda *a, **k: tmp_path / "logs")
    calls: list[list[str]] = []
    _fake_tmux(monkeypatch, calls)

    launch_servers.launch(profile, ["main"], args)

    session_cmd = next(cmd for cmd in calls if cmd[:2] == ["tmux", "new-session"])
    assert session_cmd[4] == TMUX_SESSIONS["main"]  # session 名是 repo 常數
    respawn = next(cmd for cmd in calls if cmd[:2] == ["tmux", "respawn-window"])
    assert respawn[-1] == shlex.join([
        sys.executable, str(REPO_ROOT / "deployment_profile.py"), "exec", "main",
        "--profile", str(profile_path), "--llama-bin", str(llama), "--main-gpu", "GPU-7",
    ])
    # pane 命令裡不得直接出現 llama-server 的 argv(那條路繞過 exec 的環境邊界)。
    assert "-ngl" not in respawn[-1]
    assert "CUDA_VISIBLE_DEVICES" not in respawn[-1]


@pytest.mark.smoke
def test_the_exec_path_hands_llama_server_a_clean_environment(tmp_path):
    """exec 是 llama-server 唯一真正被啟動的地方,也是最終環境的唯一決定點:
    CodeTrail 的四個前綴、llama.cpp 自己的 `LLAMA_ARG_*`、繼承來的
    `CUDA_VISIBLE_DEVICES` 全部剝掉;GPU 只由驗證過的 `deployment.json` 值重新輸出。
    留著任何一個,pane 拿到的就不是設定檔說的那個 server。"""
    dump = _write_fake_bin(tmp_path, "fake-llama-server", "env")
    models = {}
    for role in ("main", "embedding"):
        gguf = tmp_path / f"{role}.gguf"
        gguf.write_bytes(b"fixture")
        models[role] = str(gguf)
    _write_deployment(
        tmp_path,
        {
            "main": {"model": models["main"], "gpu": "GPU-FILE"},
            "embedding": {"model": models["embedding"]},
        },
        llama_bin=str(dump),
    )
    env = _shell_env(
        tmp_path,
        CUDA_VISIBLE_DEVICES="7",
        LLAMA_ARG_THREADS="3",
        OPENCODE_API_KEY="leftover-secret",
        AICODE_MODEL="shell-model",
        MARK="keep",
    )

    def _exec(role: str) -> str:
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "deployment_profile.py"), "exec", role],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
            timeout=15, check=False,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    main_env = _exec("main")
    embedding_env = _exec("embedding")

    assert "CUDA_VISIBLE_DEVICES=GPU-FILE" in main_env  # 檔案說的那張卡
    assert "CUDA_VISIBLE_DEVICES=7" not in main_env      # 繼承來的那一份不算數
    assert "CUDA_VISIBLE_DEVICES" not in embedding_env   # 沒設就是不指定
    for leaked in ("LLAMA_ARG_THREADS", "OPENCODE_API_KEY", "AICODE_MODEL",
                   "AICODE_PROFILE", "AICODE_DEPLOYMENT_CONFIG"):
        assert leaked not in main_env, leaked
        assert leaked not in embedding_env, leaked
    # 不是 CodeTrail 設定的變數照樣傳下去(PATH / HOME / 使用者自己的東西);
    # 這條邊界刻意小:剝掉不該剝的會讓 llama-server 突然找不到 CUDA / 動態連結庫。
    assert "MARK=keep" in main_env
    assert f"HOME={tmp_path}" in main_env


@pytest.mark.smoke
def test_stop_and_status_use_argv_and_constants_not_the_shell(tmp_path):
    """停止與狀態的三個舊殼層開關(session 名、預期 server 數)全部失效:
    session 名是 repo 常數,數量是 `--expected`。殼層還設著舊名字時,使用者
    最容易踩的就是「停了但沒停到」與「檢查通過但其實少一個 server」。"""
    bin_dir = tmp_path / "bin"
    tmux_log = tmp_path / "tmux.log"
    _write_fake_bin(bin_dir, "tmux", f'printf "%s\\n" "$*" >> {shlex.quote(str(tmux_log))}\nexit 1')
    _write_fake_bin(bin_dir, "ss", "exit 0")

    def _fake_nvidia_smi(rows: str) -> None:
        """PATH 上的假 nvidia-smi:只回答 compute-apps 查詢,`rows` 就是它看到的
        llama-server 列表;空字串 = GPU 上沒有任何 process(什麼都不印)。其餘查詢
        (VRAM 用量、GPU 盤點)一律沒有輸出。每個階段各寫一次:GPU 上有什麼由
        那個階段自己決定,不是整條測試共用一份。"""
        answer = f"printf '%s\\n' {shlex.quote(rows)}" if rows else ":"
        _write_fake_bin(
            bin_dir, "nvidia-smi",
            "case \"$1\" in\n"
            "  --query-compute-apps=pid,process_name,gpu_uuid,used_gpu_memory)\n"
            f"    {answer} ;;\n"
            "  *) : ;;\n"
            "esac",
        )

    env = {**_shell_env(tmp_path), "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    def _run(script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / script), *args],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
            timeout=15, check=False,
        )

    # stop 階段:GPU 上沒有任何 process。假 tmux 說兩個 session 都不存在、假 ss 說
    # 沒有 listener,GPU 上自然也不該還掛著 llama-server;掛著的話 stop 會走進基準
    # 就有的「孤兒 process」最終盤點分支 —— 那不是這半段(session 名的來源)要驗的。
    _fake_nvidia_smi("")
    stop = _run("stop_servers.py", "--scope", "all")
    assert stop.returncode == 0, stop.stderr
    for session in TMUX_SESSIONS.values():
        assert f"tmux session {session!r} does not exist" in stop.stdout
    for dead in ("shell-main-session", "shell-generic-session", "shell-aux-session"):
        assert dead not in stop.stdout + stop.stderr
        assert dead not in tmux_log.read_text(encoding="utf-8")

    # status 階段:四個 role 都在 GPU 上 —— 「預設 4 / --expected 5」要數的就是這四筆。
    _fake_nvidia_smi(FOUR_LLAMA_SERVERS)
    proc_root = tmp_path / "proc"
    proc_root.mkdir(exist_ok=True)
    status = _run("check_status.py", "--proc-root", str(proc_root))
    assert status.returncode == 0, status.stderr
    # 殼層的 EXPECTED_LLAMA_SERVERS=99 沒有作用;預設仍是四個 role。
    assert "偵測到 4 個不同的 llama-server PID（預期至少 4）" in status.stdout
    assert "預期至少 99" not in status.stdout + status.stderr

    tightened = _run("check_status.py", "--proc-root", str(proc_root), "--expected", "5")
    assert "只偵測到 4 個不同的 llama-server PID（預期至少 5）" in tightened.stderr
