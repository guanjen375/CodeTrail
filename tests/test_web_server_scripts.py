"""aicode_web smoke tests(start / stop / --local 全在同一入口)。

只走 --dry-run / --help / guard path,不需要 tmux / opencode / llama-server。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_ENTRY = REPO_ROOT / "aicode_web"


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
        }
    )
    return env, state, calls


def test_local_dry_run_resolves_project_port_session(tmp_path):
    proc = _run(
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
    # launch 應該:把 CodeTrail env 明確 export 進 pane、cd 專案、exec aicode web
    assert "export AICODE_WEB_PORT=4096" in out
    assert f"export AICODE_ROOT={os.path.realpath(tmp_path)}" in out
    assert "&& exec " in out
    assert str(REPO_ROOT / "aicode") in out and "web" in out


def test_local_dry_run_respects_custom_port_and_session(tmp_path):
    proc = _run(
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
    proc = _run(WEB_ENTRY, ["--local", "--dry-run", "--hostname", "0.0.0.0"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    launch = next(line for line in proc.stdout.splitlines() if line.startswith("launch="))
    assert launch.rstrip().endswith("aicode web --hostname 0.0.0.0")


def test_aicode_web_dry_run_binds_only_current_tailscale_ipv4(tmp_path):
    proc = _run(
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
    assert "aicode web --cors https://browser.example --hostname 100.100.10.20" in launch
    assert "0.0.0.0" not in launch


def test_aicode_web_readme_symlink_resolves_codetrail_checkout(tmp_path):
    """README 安裝到 ~/.local/bin 後仍能找到 checkout 內的 scripts / aicode。"""
    fake_home = tmp_path / "home"
    installed_bin = fake_home / ".local" / "bin"
    installed_bin.mkdir(parents=True)
    installed_entry = installed_bin / "aicode_web"
    installed_entry.symlink_to(WEB_ENTRY)
    project = tmp_path / "project"
    project.mkdir()

    proc = _run(
        installed_entry,
        ["--dry-run"],
        cwd=project,
        env_extra={**_tailscale_env(tmp_path), "HOME": str(fake_home)},
    )

    assert proc.returncode == 0, proc.stderr
    assert f"project={project.resolve()}" in proc.stdout
    assert f"aicode={REPO_ROOT / 'aicode'}" in proc.stdout
    assert "bind_host=100.100.10.20" in proc.stdout


def test_aicode_web_ignores_tailscale_version_warning_before_ipv4(tmp_path):
    proc = _run(
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
    proc = _run(
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
        proc = _run(
            WEB_ENTRY,
            ["--dry-run"],
            cwd=tmp_path,
            env_extra=_tailscale_env(tmp_path, output=bad_ip),
        )
        assert proc.returncode != 0, bad_ip
        assert "100.64.0.0/10" in proc.stderr or "無效 IPv4" in proc.stderr


def test_aicode_web_rejects_network_flag_override(tmp_path):
    proc = _run(
        WEB_ENTRY,
        ["--dry-run", "--hostname", "0.0.0.0"],
        cwd=tmp_path,
        env_extra=_tailscale_env(tmp_path),
    )
    assert proc.returncode != 0
    assert "自行鎖定 Tailscale IP" in proc.stderr

    equals_form = _run(
        WEB_ENTRY,
        ["--dry-run", "--mdns=true"],
        cwd=tmp_path,
        env_extra=_tailscale_env(tmp_path),
    )
    assert equals_form.returncode != 0
    assert "自行鎖定 Tailscale IP" in equals_form.stderr


def test_aicode_web_headless_start_is_idempotent_and_stop_uses_session_metadata(tmp_path):
    env, state, calls = _headless_runtime_env(tmp_path)

    first = _run(WEB_ENTRY, [], cwd=tmp_path, env_extra=env)
    assert first.returncode == 0, first.stderr
    assert state.exists()
    assert "B 機瀏覽器請直接開:http://100.100.10.20:4096/" in first.stdout
    assert "A 機不需要 GUI" in first.stdout
    first_calls = calls.read_text(encoding="utf-8")
    assert "new-session -d" in first_calls
    assert "remain-on-exit on" in first_calls
    assert "send-keys" in first_calls

    second = _run(WEB_ENTRY, [], cwd=tmp_path, env_extra=env)
    assert second.returncode == 0, second.stderr
    assert "web backend 已在執行" in second.stdout
    all_calls = calls.read_text(encoding="utf-8")
    assert all_calls.count("new-session -d") == 1

    stopped = _run(WEB_ENTRY, ["stop"], cwd=tmp_path, env_extra=env)
    assert stopped.returncode == 0, stopped.stderr
    assert not state.exists()
    assert "100.100.10.20:4096 已釋放" in stopped.stdout


def test_rejects_invalid_port(tmp_path):
    proc = _run(WEB_ENTRY, ["--local", "--dry-run"], cwd=tmp_path, env_extra={"AICODE_WEB_PORT": "abc"})
    assert proc.returncode != 0
    assert "AICODE_WEB_PORT" in proc.stderr


def test_refuses_inside_codetrail_repo():
    """從 CodeTrail repo / scripts 目錄跑要被拒(避免把工具 repo 當分析專案)。"""
    proc = _run(WEB_ENTRY, ["--local", "--dry-run"], cwd=REPO_ROOT / "scripts")
    assert proc.returncode != 0
    assert "CodeTrail repo" in proc.stderr


def test_refuses_repo_root():
    proc = _run(WEB_ENTRY, ["--local", "--dry-run"], cwd=REPO_ROOT)
    assert proc.returncode != 0
    assert "CodeTrail repo" in proc.stderr


def test_refuses_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    proc = _run(WEB_ENTRY, ["--local", "--dry-run"], cwd=home, env_extra={"HOME": str(os.path.realpath(home))})
    assert proc.returncode != 0
    assert "HOME" in proc.stderr


def test_aicode_web_help_exits_zero():
    proc = _run(WEB_ENTRY, ["--help"], cwd=REPO_ROOT)
    assert proc.returncode == 0
    assert "aicode_web" in proc.stdout
    assert "用法" in proc.stdout
    assert "--tailscale" in proc.stdout
    assert "--local" in proc.stdout
    assert "stop" in proc.stdout


def test_stop_help_exits_zero():
    proc = _run(WEB_ENTRY, ["stop", "--help"], cwd=REPO_ROOT)
    assert proc.returncode == 0
    assert "用法" in proc.stdout


def test_stop_rejects_unknown_arg():
    proc = _run(WEB_ENTRY, ["stop", "--bogus"], cwd=REPO_ROOT)
    assert proc.returncode != 0
    assert "unknown argument" in proc.stderr


def test_rejects_unknown_subcommand():
    proc = _run(WEB_ENTRY, ["bogus"], cwd=REPO_ROOT)
    assert proc.returncode != 0
    assert "unknown subcommand" in proc.stderr


def test_web_entry_is_executable():
    assert os.access(WEB_ENTRY, os.X_OK)
