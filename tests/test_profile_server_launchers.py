from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


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


def _run(script: str, tmp_path: Path, env_extra: dict[str, str], *args: str):
    env = _clean_env(tmp_path)
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / script), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_start_all_routes_main_and_all_aux_to_their_shared_gpus(tmp_path):
    main = tmp_path / "main.gguf"
    main.write_bytes(b"fixture")
    proc = _run(
        "start-all.sh",
        tmp_path,
        {
            "AICODE_PROFILE": "maintainer-target",
            "AICODE_MODEL": str(main),
            "MAIN_GPU": "GPU-H200",
            "AUX_GPU": "GPU-RTX2000ADA",
        },
        "--dry-run",
    )

    assert proc.returncode == 0, proc.stderr
    assert "profile=maintainer-target" in proc.stdout
    assert proc.stdout.count("CUDA_VISIBLE_DEVICES=GPU-H200") == 1
    assert proc.stdout.count("CUDA_VISIBLE_DEVICES=GPU-RTX2000ADA") == 3


def test_start_main_fails_loud_when_maintainer_profile_has_no_model(tmp_path):
    proc = _run(
        "start-main-server.sh",
        tmp_path,
        {"AICODE_PROFILE": "maintainer-target"},
        "--dry-run",
    )

    assert proc.returncode != 0
    assert "main model is unset" in proc.stderr


def test_legacy_aux_launcher_env_names_still_override_profile(tmp_path):
    embed = tmp_path / "legacy-embed.gguf"
    rerank = tmp_path / "legacy-rerank.gguf"
    vl = tmp_path / "legacy-vl.gguf"
    mmproj = tmp_path / "legacy-mmproj.gguf"
    proc = _run(
        "start-rag-servers.sh",
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


def test_new_and_legacy_launcher_help_paths_are_offline(tmp_path):
    for script in (
        "start-main-server.sh",
        "start-all.sh",
        "start-rag-servers.sh",
        "quit.sh",
        "stop-rag-servers.sh",
    ):
        proc = _run(script, tmp_path, {}, "--help")
        assert proc.returncode == 0, f"{script}: {proc.stderr}"


def test_quit_still_kills_sessions_when_deployment_config_is_broken(tmp_path):
    """設定檔壞掉時 quit.sh 不能連 tmux session 都拒絕關(復原路徑不能死)。"""
    config_dir = tmp_path / ".config" / "codetrail"
    config_dir.mkdir(parents=True)
    (config_dir / "deployment.json").write_text("{not json", encoding="utf-8")

    proc = _run(
        "quit.sh",
        tmp_path,
        {
            # 不存在的 session 名:只驗證退路流程,不動開發機上真的 codetrail session
            "MAIN_SESSION": "codetrail-test-none-main",
            "SESSION": "codetrail-test-none-rag",
        },
        "--scope", "all",
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

    proc = _run(
        "start-all.sh",
        tmp_path,
        {
            "AICODE_DEPLOYMENT_CONFIG": str(deployment),
            "AICODE_MODEL": str(main),
        },
        "--dry-run",
    )

    assert proc.returncode != 0
    assert "share localhost:8081" in proc.stderr
