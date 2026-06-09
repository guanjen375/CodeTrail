from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_start_rag_servers_dry_run_uses_base_url_ports(tmp_path):
    env = {
        **os.environ,
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
        ["bash", str(REPO_ROOT / "scripts" / "start-rag-servers.sh"), "--dry-run"],
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
    assert "CUDA_VISIBLE_DEVICES=0" in proc.stdout
    assert "CUDA_VISIBLE_DEVICES=1" in proc.stdout
    assert "CUDA_VISIBLE_DEVICES=2" in proc.stdout
