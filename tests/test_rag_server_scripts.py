from __future__ import annotations

import os
import subprocess
from pathlib import Path

from deployment_profile import RUNTIME_OVERRIDE_ENV_KEYS

REPO_ROOT = Path(__file__).resolve().parent.parent


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
    assert "bge-reranker-v2-m3/bge-reranker-v2-m3-Q8_0.gguf" in proc.stdout
    assert "qwen3.5-9b/Qwen3.5-9B-Q6_K.gguf" in proc.stdout
    assert "qwen3.5-9b/mmproj-F16.gguf" in proc.stdout
    assert "CUDA_VISIBLE_DEVICES=0" in proc.stdout
    assert "CUDA_VISIBLE_DEVICES=1" in proc.stdout
    assert "CUDA_VISIBLE_DEVICES=2" in proc.stdout


def test_start_rag_servers_noncausal_models_use_full_physical_batch(tmp_path):
    proc = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "start-rag-servers.sh"), "--dry-run"],
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
