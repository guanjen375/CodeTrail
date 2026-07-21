from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "start-rag-servers-mgpu.sh"


def _write_fake_nvidia_smi(tmp_path: Path, output: str, exit_code: int = 0) -> Path:
    executable = tmp_path / "nvidia-smi"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' {shlex.quote(output)}\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _base_env(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "LLAMA_BIN": str(tmp_path / "llama-server"),
        "MODELS_DIR": str(tmp_path / "models"),
    }


def test_mgpu_launcher_scans_prompts_and_pins_all_servers(tmp_path):
    _write_fake_nvidia_smi(
        tmp_path,
        "0, NVIDIA RTX 4090, 24564, 20000, GPU-aaaa\n"
        "1, NVIDIA RTX 5090, 32607, 30000, GPU-bbbb",
    )

    proc = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run"],
        cwd=str(REPO_ROOT),
        env=_base_env(tmp_path),
        input="1\n",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "偵測到 2 顆 NVIDIA GPU" in proc.stdout
    assert "GPU 0: NVIDIA RTX 4090" in proc.stdout
    assert "GPU 1: NVIDIA RTX 5090" in proc.stdout
    assert "已選擇 GPU 1: NVIDIA RTX 5090 (GPU-bbbb)" in proc.stdout
    assert proc.stdout.count("CUDA_VISIBLE_DEVICES=GPU-bbbb") == 3
    assert "--port 8081" in proc.stdout
    assert "--port 8082" in proc.stdout
    assert "--port 8083" in proc.stdout


def test_mgpu_launcher_gpu_flag_is_noninteractive(tmp_path):
    _write_fake_nvidia_smi(
        tmp_path,
        "0, NVIDIA A100, 81920, 70000, GPU-1111\n"
        "3, NVIDIA H100, 81559, 71000, GPU-3333",
    )

    proc = subprocess.run(
        ["bash", str(SCRIPT), "--gpu", "3", "--dry-run"],
        cwd=str(REPO_ROOT),
        env=_base_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "已選擇 GPU 3: NVIDIA H100 (GPU-3333)" in proc.stdout
    assert "請輸入要使用的 GPU index" not in proc.stdout
    assert proc.stdout.count("CUDA_VISIBLE_DEVICES=GPU-3333") == 3


def test_mgpu_launcher_rejects_unknown_gpu_index(tmp_path):
    _write_fake_nvidia_smi(
        tmp_path,
        "0, NVIDIA RTX 4090, 24564, 20000, GPU-aaaa",
    )

    proc = subprocess.run(
        ["bash", str(SCRIPT), "--gpu", "9", "--dry-run"],
        cwd=str(REPO_ROOT),
        env=_base_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 1
    assert "GPU index '9' 不在 nvidia-smi 掃描結果中" in proc.stderr
