"""`aicode web` 的存取控制:root safety、非本機 hostname 的密碼閘、Tailscale 例外。

從 test_aicode_web.py 拆出(2026-08-20)。這一半全是「拒絕/放行」判斷,
是 web 模式對外暴露面的守門測試。
"""
from __future__ import annotations

from tests._harness import (
    contains_subsequence,
    read_stub_args,
    run_aicode_subcmd_with_stub,
)


def test_aicode_web_rejects_root_slash(tmp_path):
    """spec E.1:aicode web 從 / 啟動被拒(沿用既有沙箱 root 檢查)。"""
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["web"], env_extra={"AICODE_ROOT": "/"}
    )

    assert result.returncode != 0
    assert "refusing AICODE_ROOT=/" in result.stderr
    assert not args_file.exists()

def test_aicode_web_rejects_home_root(tmp_path):
    """spec E.1:aicode web 從 $HOME 啟動被拒。"""
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["web"], env_extra={"AICODE_ROOT": "__HOME__"}
    )

    assert result.returncode != 0
    assert "refusing AICODE_ROOT=$HOME" in result.stderr
    assert not args_file.exists()

def test_aicode_web_non_local_hostname_without_password_refused(tmp_path):
    """spec E.2:hostname 非 local 且未設密碼 → 拒絕啟動。"""
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["web", "--hostname", "0.0.0.0"]
    )

    assert result.returncode != 0
    assert "OPENCODE_SERVER_PASSWORD" in result.stderr
    assert not args_file.exists()

def test_aicode_web_non_local_hostname_with_password_allowed(tmp_path):
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path,
        ["web", "--hostname", "0.0.0.0"],
        env_extra={"OPENCODE_SERVER_PASSWORD": "s3cret"},
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    args = read_stub_args(args_file)
    assert args[0] == "web"
    assert contains_subsequence(args, ["--hostname", "0.0.0.0"])

def test_aicode_web_verified_tailscale_ip_without_password_allowed(tmp_path):
    """aicode_web 的窄例外:env、hostname、tailscale CLI 三者完全一致才放行。"""
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path,
        ["web", "--hostname", "100.100.10.20"],
        env_extra={"AICODE_WEB_TAILSCALE_IP": "100.100.10.20"},
        tailscale_ip="100.100.10.20",
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert contains_subsequence(
        read_stub_args(args_file), ["--hostname", "100.100.10.20"]
    )
    assert "已驗證並只綁本機 Tailscale IPv4" in result.stdout

def test_aicode_web_tailscale_env_cannot_spoof_current_ip(tmp_path):
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path,
        ["web", "--hostname", "100.100.10.20"],
        env_extra={"AICODE_WEB_TAILSCALE_IP": "100.100.10.20"},
        tailscale_ip="100.100.10.21",
    )

    assert result.returncode != 0
    assert "OPENCODE_SERVER_PASSWORD" in result.stderr
    assert not args_file.exists()

def test_aicode_web_non_cgnat_ip_cannot_use_tailscale_exception(tmp_path):
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path,
        ["web", "--hostname", "192.168.1.50"],
        env_extra={"AICODE_WEB_TAILSCALE_IP": "192.168.1.50"},
        tailscale_ip="192.168.1.50",
    )

    assert result.returncode != 0
    assert "OPENCODE_SERVER_PASSWORD" in result.stderr
    assert not args_file.exists()

def test_aicode_web_mdns_without_password_refused(tmp_path):
    """--mdns 會翻成 0.0.0.0 廣播,視為非 loopback 暴露,未設密碼也要拒絕。"""
    result, args_file = run_aicode_subcmd_with_stub(tmp_path, ["web", "--mdns"])

    assert result.returncode != 0
    assert "OPENCODE_SERVER_PASSWORD" in result.stderr
    assert "mdns" in result.stderr.lower()
    assert not args_file.exists()

def test_aicode_web_mdns_equals_form_without_password_refused(tmp_path):
    """yargs 也接受 --mdns=true；不能靠 spelling 繞過非 loopback 密碼硬規則。"""
    result, args_file = run_aicode_subcmd_with_stub(tmp_path, ["web", "--mdns=true"])

    assert result.returncode != 0
    assert "OPENCODE_SERVER_PASSWORD" in result.stderr
    assert "mdns" in result.stderr.lower()
    assert not args_file.exists()

def test_aicode_web_localhost_hostname_allowed_without_password(tmp_path):
    """明確 --hostname localhost 仍屬 loopback,不需要密碼。"""
    result, args_file = run_aicode_subcmd_with_stub(
        tmp_path, ["web", "--hostname", "localhost"]
    )

    assert result.returncode == 0, f"exit={result.returncode}\nstderr={result.stderr}"
    assert contains_subsequence(read_stub_args(args_file), ["--hostname", "localhost"])
