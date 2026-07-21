from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-status.sh"


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
        ["bash", str(SCRIPT), *args],
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
