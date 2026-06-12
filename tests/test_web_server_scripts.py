"""start-web.sh / stop-web.sh smoke tests。

只走 --dry-run / --help / guard path,不需要 tmux / opencode / llama-server。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
START = REPO_ROOT / "scripts" / "start-web.sh"
STOP = REPO_ROOT / "scripts" / "stop-web.sh"


def _run(script: Path, args, cwd, env_extra=None, timeout=10):
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


def test_start_web_dry_run_resolves_project_port_session(tmp_path):
    proc = _run(
        START,
        ["--dry-run"],
        cwd=tmp_path,
        env_extra={"AICODE_WEB_PORT": "4096", "WEB_SESSION": "codetrail-web"},
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert f"project={os.path.realpath(tmp_path)}" in out
    assert "port=4096" in out
    assert "session=codetrail-web" in out
    assert "health_url=http://127.0.0.1:4096/" in out
    # launch 應該:把 CodeTrail env 明確 export 進 pane、cd 專案、exec aicode web
    assert "export AICODE_WEB_PORT=4096" in out
    assert f"export AICODE_ROOT={os.path.realpath(tmp_path)}" in out
    assert "&& exec " in out
    assert str(REPO_ROOT / "aicode") in out and "web" in out


def test_start_web_dry_run_respects_custom_port_and_session(tmp_path):
    proc = _run(
        START,
        ["--dry-run"],
        cwd=tmp_path,
        env_extra={"AICODE_WEB_PORT": "4097", "WEB_SESSION": "myweb"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "port=4097" in proc.stdout
    assert "session=myweb" in proc.stdout
    assert "health_url=http://127.0.0.1:4097/" in proc.stdout


def test_start_web_dry_run_forwards_extra_args(tmp_path):
    proc = _run(START, ["--dry-run", "--hostname", "0.0.0.0"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    launch = next(l for l in proc.stdout.splitlines() if l.startswith("launch="))
    assert launch.rstrip().endswith("aicode web --hostname 0.0.0.0")


def test_start_web_rejects_invalid_port(tmp_path):
    proc = _run(START, ["--dry-run"], cwd=tmp_path, env_extra={"AICODE_WEB_PORT": "abc"})
    assert proc.returncode != 0
    assert "AICODE_WEB_PORT" in proc.stderr


def test_start_web_refuses_inside_codetrail_repo():
    """從 CodeTrail repo / scripts 目錄跑要被拒(避免把工具 repo 當分析專案)。"""
    proc = _run(START, ["--dry-run"], cwd=REPO_ROOT / "scripts")
    assert proc.returncode != 0
    assert "CodeTrail repo" in proc.stderr


def test_start_web_refuses_repo_root():
    proc = _run(START, ["--dry-run"], cwd=REPO_ROOT)
    assert proc.returncode != 0
    assert "CodeTrail repo" in proc.stderr


def test_start_web_refuses_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    proc = _run(START, ["--dry-run"], cwd=home, env_extra={"HOME": str(os.path.realpath(home))})
    assert proc.returncode != 0
    assert "HOME" in proc.stderr


def test_start_web_help_exits_zero():
    proc = _run(START, ["--help"], cwd=REPO_ROOT)
    assert proc.returncode == 0
    assert "用法" in proc.stdout
    assert "start-web.sh" in proc.stdout


def test_stop_web_help_exits_zero():
    proc = _run(STOP, ["--help"], cwd=REPO_ROOT)
    assert proc.returncode == 0
    assert "用法" in proc.stdout
    assert "stop-web.sh" in proc.stdout


def test_stop_web_rejects_unknown_arg():
    proc = _run(STOP, ["--bogus"], cwd=REPO_ROOT)
    assert proc.returncode != 0
    assert "unknown argument" in proc.stderr


def test_web_scripts_are_executable():
    assert os.access(START, os.X_OK)
    assert os.access(STOP, os.X_OK)
