"""set_config.sh / scripts/set_config.py 的離線測試。

nvidia-smi、llama-server 都用 PATH / LLAMA_BIN 上的 stub、模型目錄用 tmp
fixture、HOME 指到 tmp,完全不需要 GPU / 真 llama-server / 真模型。
tmux session 名稱指到不存在的名字,避免碰到開發機上真的 CodeTrail session。
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import struct
import subprocess
from pathlib import Path

from deployment_profile import RUNTIME_OVERRIDE_ENV_KEYS

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "set_config.sh"

TWO_GPUS = (
    "0, NVIDIA GeForce RTX 5090, 32607, 30000, GPU-aaaa-5090\n"
    "1, NVIDIA RTX 2000 Ada Generation, 16380, 15000, GPU-bbbb-2000"
)

GIB = 1024**3

# 會影響有效設定的 env 一律引用 deployment_profile 的單一來源,只補測試自身用的鍵。
_PROFILE_ENV_KEYS = set(RUNTIME_OVERRIDE_ENV_KEYS) | {
    "MODELS_DIR", "LLAMA_BIN",
    "MAIN_SESSION", "AUX_SESSION", "SESSION",
    "MAIN_HEALTH_TIMEOUT", "RAG_HEALTH_TIMEOUT",
    "AICODE_SET_CONFIG_MEMINFO",
    "OPENCODE_CONFIG",  # set_config 會尊重它;開發機殼層若有設定不得洩漏進測試
}


def _sparse(path: Path, size: int) -> None:
    """建立指定 st_size 的稀疏檔:容量規劃只看大小,不需要真的占磁碟。"""
    with open(path, "wb") as handle:
        handle.truncate(size)


def _sparse_dense_gguf(path: Path, size: int) -> None:
    """建立有合法 tensor table 的 sparse dense GGUF，供 offload 分流測試。"""
    name = b"blk.0.attn_q.weight"
    header = struct.pack("<4sIQQ", b"GGUF", 3, 1, 0)
    tensor = (
        struct.pack("<Q", len(name)) + name
        + struct.pack("<I", 1) + struct.pack("<Q", 1)
        + struct.pack("<I", 0) + struct.pack("<Q", 0)
    )
    metadata_end = len(header) + len(tensor)
    data_start = (metadata_end + 31) // 32 * 32
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(tensor)
        handle.write(b"\0" * (data_start - metadata_end))
        handle.truncate(size)


def _sparse_moe_gguf(path: Path, size: int, expert_bytes: int) -> None:
    """建立 expert + dense tensor 的 sparse MoE GGUF。"""
    tensors = (
        (b"blk.0.ffn_up_exps.weight", 0),
        (b"blk.0.attn_q.weight", expert_bytes),
    )
    header = struct.pack("<4sIQQ", b"GGUF", 3, len(tensors), 0)
    table = bytearray()
    for name, offset in tensors:
        table.extend(struct.pack("<Q", len(name)) + name)
        table.extend(struct.pack("<I", 1) + struct.pack("<Q", 1))
        table.extend(struct.pack("<I", 0) + struct.pack("<Q", offset))
    metadata_end = len(header) + len(table)
    data_start = (metadata_end + 31) // 32 * 32
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(table)
        handle.write(b"\0" * (data_start - metadata_end))
        handle.truncate(size)


def _write_fake_nvidia_smi(bin_dir: Path, output: str, exit_code: int = 0) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    executable = bin_dir / "nvidia-smi"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' {shlex.quote(output)}\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)


def _write_fake_llama(
    tmp_path: Path, help_flags: str = "--fit --cpu-moe --reranking --mmproj"
) -> Path:
    executable = tmp_path / "llama-server"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        f"if [[ \"${{1:-}}\" == \"--help\" ]]; then printf '%s\\n' {shlex.quote(help_flags)}; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _make_models(root: Path, *, with_reranker: bool = True) -> Path:
    models = root / "models"
    (models / "big-chat").mkdir(parents=True)
    (models / "big-chat" / "big-chat-ud-q4_k_xl-00001-of-00002.gguf").write_bytes(b"x" * 2048)
    (models / "big-chat" / "big-chat-ud-q4_k_xl-00002-of-00002.gguf").write_bytes(b"x" * 2048)
    (models / "bge-m3").mkdir()
    (models / "bge-m3" / "bge-m3-f16.gguf").write_bytes(b"x" * 512)
    if with_reranker:
        (models / "bge-reranker-v2-m3").mkdir()
        (models / "bge-reranker-v2-m3" / "bge-reranker-v2-m3-Q8_0.gguf").write_bytes(
            b"x" * 512
        )
        # 舊模型同時存在時，--yes 仍必須優先選已驗證的 BGE 預設。
        (models / "qwen3-reranker-0.6b").mkdir()
        (models / "qwen3-reranker-0.6b" / "qwen3-reranker-0.6b-q8_0.gguf").write_bytes(b"x" * 512)
    (models / "vl").mkdir()
    (models / "vl" / "vl-model-q6.gguf").write_bytes(b"x" * 512)
    (models / "vl" / "mmproj-F16.gguf").write_bytes(b"x" * 256)
    return models


def _env(tmp_path: Path, *, with_llama: bool = True) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key not in _PROFILE_ENV_KEYS}
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       536870912 kB\nMemAvailable:   524288000 kB\n",
        encoding="utf-8",
    )
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PATH": f"{tmp_path / 'bin'}:{env.get('PATH', '')}",
            "LLAMA_BIN": str(tmp_path / "llama-server"),
            # 不存在的 session 名稱:避免「偵測到 server 運行中」誤觸開發機上真的 tmux。
            "MAIN_SESSION": "codetrail-test-none-main",
            "SESSION": "codetrail-test-none-rag",
            "AICODE_SET_CONFIG_MEMINFO": str(meminfo),
        }
    )
    if with_llama and not (tmp_path / "llama-server").exists():
        _write_fake_llama(tmp_path)
    return env


def _run(tmp_path: Path, *args: str, stdin: str | None = None,
         with_llama: bool = True,
         env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = _env(tmp_path, with_llama=with_llama)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(SCRIPT), "--skip-deps-check", *args],
        cwd=REPO_ROOT,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_help_is_offline_and_exits_zero(tmp_path):
    proc = _run(tmp_path, "--help")
    assert proc.returncode == 0, proc.stderr
    assert "--models-dir" in proc.stdout
    assert "--advanced" in proc.stdout
    assert "--cpu-moe" in proc.stdout
    assert "--allow-remote" in proc.stdout
    assert "--rerank-ctx" in proc.stdout


def test_yes_run_generates_all_artifacts(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    proc = _run(tmp_path, "--yes", "--models-dir", str(models))

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "初步判定 OK" in proc.stdout
    home = tmp_path / "home"

    registry = json.loads((home / ".config/codetrail/models.json").read_text(encoding="utf-8"))
    main_path = str(models / "big-chat" / "big-chat-ud-q4_k_xl-00001-of-00002.gguf")
    assert registry["big-chat-ud-q4-k-xl"] == main_path

    deployment = json.loads((home / ".config/codetrail/deployment.json").read_text(encoding="utf-8"))
    services = deployment["services"]
    assert services["main"]["model"] == "big-chat-ud-q4-k-xl"
    assert services["main"]["ctx"] == 65536
    assert services["main"]["parameters"]["jinja"] is True
    assert "threads" in services["main"]["parameters"]
    assert services["embedding"]["model"] == str(models / "bge-m3" / "bge-m3-f16.gguf")
    assert services["reranker"]["model"].endswith("bge-reranker-v2-m3-Q8_0.gguf")
    assert services["vl"]["model"] == str(models / "vl" / "vl-model-q6.gguf")
    assert services["vl"]["mmproj"] == str(models / "vl" / "mmproj-F16.gguf")
    assert services["embedding"]["parameters"] == {"parallel": 1}
    assert services["reranker"]["ctx"] == 8192
    assert services["reranker"]["batch"] == 8192
    assert services["reranker"]["ubatch"] == 8192
    assert services["reranker"]["parameters"] == {"parallel": 1}
    assert services["vl"]["parameters"] == {
        "gpu_layers": "auto",
        "parallel": 1,
        "fit": "on",
        "fit_target": 3072,
    }
    # 預設不開放遠端 → 不寫 bind(profile 預設 local)
    assert "bind" not in services["main"]

    opencode = json.loads((home / ".config/opencode/opencode.json").read_text(encoding="utf-8"))
    assert opencode["model"] == "llamacpp/big-chat-ud-q4-k-xl"
    limit = opencode["provider"]["llamacpp"]["models"]["big-chat-ud-q4-k-xl"]["limit"]
    assert limit["context"] == 65536
    mcp = opencode["mcp"]["codetrail"]
    assert mcp["timeout"] == 660000
    assert "mcp_server.py" in mcp["command"][2]
    assert opencode["permission"]["bash"] == "deny"

    start = home / "start.sh"
    content = start.read_text(encoding="utf-8")
    assert "generated by CodeTrail set_config.sh" in content
    assert "export MAIN_GPU=GPU-aaaa-5090" in content
    assert "export AUX_GPU=GPU-bbbb-2000" in content
    assert content.rstrip().endswith('start-all.sh "$@"')
    assert start.stat().st_mode & stat.S_IXUSR

    # 結尾預覽 = start-all --dry-run 的完整指令(推薦啟動參數)
    assert "main_command=" in proc.stdout
    assert proc.stdout.count("CUDA_VISIBLE_DEVICES=GPU-bbbb-2000") == 3
    # 三層狀態:設定完成 ≠ 已可使用
    assert "第 1 層" in proc.stdout
    assert "待執行" in proc.stdout


def test_yes_honors_vl_hint_order_before_model_size(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    preferred = models / "qwen3.5-9b"
    preferred.mkdir()
    (preferred / "Qwen3.5-9B-Q6_K.gguf").write_bytes(b"x" * 2048)
    (preferred / "mmproj-F16.gguf").write_bytes(b"x" * 256)
    older = models / "qwen3-vl"
    older.mkdir()
    (older / "Qwen3VL-8B-Instruct-Q4_K_M.gguf").write_bytes(b"x" * 1024)
    (older / "mmproj-Qwen3VL-8B-Instruct-F16.gguf").write_bytes(b"x" * 256)

    proc = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))

    assert proc.returncode == 0, proc.stderr + proc.stdout
    deployment = json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )
    assert deployment["services"]["vl"]["model"].endswith("Qwen3.5-9B-Q6_K.gguf")


def test_generated_start_sh_dry_run_pins_gpus_and_binds_loopback(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    assert _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models)).returncode == 0

    proc = subprocess.run(
        ["bash", str(tmp_path / "home" / "start.sh"), "--dry-run"],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    for port in ("8080", "8081", "8082", "8083"):
        assert f"_port={port}" in proc.stdout
    assert proc.stdout.count("CUDA_VISIBLE_DEVICES=GPU-aaaa-5090") == 1
    assert proc.stdout.count("CUDA_VISIBLE_DEVICES=GPU-bbbb-2000") == 3
    # 安全預設:只綁 127.0.0.1,不暴露 0.0.0.0
    assert "main_bind_host=127.0.0.1" in proc.stdout
    assert "0.0.0.0" not in proc.stdout


def test_allow_remote_binds_all_interfaces_with_warning(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    proc = _run(tmp_path, "--yes", "--no-preview", "--allow-remote", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr
    assert "0.0.0.0" in proc.stdout  # 警告文字

    deployment = json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )
    for role in ("main", "embedding", "reranker", "vl"):
        assert deployment["services"][role]["bind"] == "all-interfaces"

    dry = subprocess.run(
        ["bash", str(tmp_path / "home" / "start.sh"), "--dry-run"],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert dry.returncode == 0, dry.stderr
    assert "main_bind_host=0.0.0.0" in dry.stdout


def test_generated_start_sh_clears_legacy_env_overrides(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    assert _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models)).returncode == 0

    env = _env(tmp_path)
    env["EMBED_MODEL"] = "/bogus/does-not-exist.gguf"   # 模擬 .bashrc 殘留的舊 override
    env["MAIN_CTX"] = "1234"
    proc = subprocess.run(
        ["bash", str(tmp_path / "home" / "start.sh"), "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "/bogus/does-not-exist.gguf" not in proc.stdout
    assert "-c 65536" in proc.stdout  # 不被 MAIN_CTX=1234 蓋掉


def test_summary_confirm_enter_writes_and_q_aborts(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)

    # main、CPU-MoE、reranker、reranker ctx、摘要確認各按一次 Enter。
    accepted = _run(
        tmp_path, "--no-preview", "--models-dir", str(models), stdin="\n\n\n\n\n"
    )
    assert accepted.returncode == 0, accepted.stderr + accepted.stdout
    assert "【主聊天模型】 — 偵測到的候選" in accepted.stdout
    assert "目前主模型: big-chat-ud-q4_k_xl-00001-of-00002.gguf" in accepted.stdout
    assert "Qwen3 = 上游 benchmark 較強的 accuracy-first 候選" in accepted.stdout
    assert "【reranker 模型】 — 偵測到的候選" in accepted.stdout
    assert "【reranker ctx】" in accepted.stdout
    assert "ctx 設更大不會讓排序更準" in accepted.stdout
    assert "建議配置" in accepted.stdout
    assert (tmp_path / "home" / "start.sh").exists()

    home2 = tmp_path / "home2"
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--skip-deps-check", "--no-preview", "--models-dir", str(models)],
        cwd=REPO_ROOT,
        env={**_env(tmp_path), "HOME": str(home2), "USERPROFILE": str(home2)},
        input="\n\n\n\nq\n",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "未寫入任何檔案" in proc.stdout
    assert not (home2 / ".config").exists()
    assert not (home2 / "start.sh").exists()


def test_flags_override_model_and_gpu(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    proc = _run(
        tmp_path,
        "--yes", "--no-preview", "--models-dir", str(models),
        "--main-gpu", "1", "--embed-gpu", "0",
    )
    assert proc.returncode == 0, proc.stderr
    content = (tmp_path / "home" / "start.sh").read_text(encoding="utf-8")
    assert "export MAIN_GPU=GPU-bbbb-2000" in content
    # aux 三顆不同卡(embed=0、rerank/vl=預設)→ 逐 role export
    assert "export EMBED_GPU=GPU-aaaa-5090" in content


def test_no_gpu_notifies_and_fails(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", "", exit_code=1)
    models = _make_models(tmp_path)
    proc = _run(tmp_path, "--yes", "--models-dir", str(models))
    assert proc.returncode == 2
    assert "偵測失敗" in proc.stderr


def test_missing_model_category_fails_precheck(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path, with_reranker=False)
    proc = _run(tmp_path, "--yes", "--models-dir", str(models))
    assert proc.returncode == 2
    assert "reranker" in proc.stderr
    assert "初步判定不通過" in proc.stderr


def test_missing_llama_binary_fails_with_build_hint(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    proc = _run(tmp_path, "--yes", "--models-dir", str(models), with_llama=False)
    assert proc.returncode == 2
    assert "llama-server" in proc.stderr
    assert "README §1.5" in proc.stderr
    assert "LLAMA_BIN" in proc.stderr


def test_llama_without_reranking_support_fails(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    _write_fake_llama(tmp_path, help_flags="--fit --mmproj")  # 沒有 --reranking
    proc = _run(tmp_path, "--yes", "--models-dir", str(models))
    assert proc.returncode == 2
    assert "--reranking" in proc.stderr
    assert "更新並重新 build" in proc.stderr


def test_cpu_moe_mode_requires_llama_cpu_moe_flag(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    _write_fake_llama(tmp_path, help_flags="--fit --reranking --mmproj")

    proc = _run(
        tmp_path, "--yes", "--cpu-moe", "--no-preview", "--models-dir", str(models)
    )

    assert proc.returncode == 2
    assert "需要 llama-server 的 --cpu-moe" in proc.stderr
    assert "重新 build" in proc.stderr


def test_generated_vl_safety_requires_llama_fit_flag(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    _write_fake_llama(tmp_path, help_flags="--cpu-moe --reranking --mmproj")

    proc = _run(
        tmp_path, "--yes", "--no-cpu-moe", "--no-preview", "--models-dir", str(models)
    )

    assert proc.returncode == 2
    assert "安全的 VL placement 需要 llama-server --fit" in proc.stderr
    assert "重新 build" in proc.stderr


def test_incomplete_shards_are_reported_with_missing_names(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    (models / "big-chat" / "big-chat-ud-q4_k_xl-00002-of-00002.gguf").unlink()

    # 還有別的 main 候選(VL 模型也可當 main)→ 軟剔除:警告列出缺哪片,改用替代模型
    proc = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr
    assert "模型不完整已剔除" in proc.stdout
    assert "big-chat-ud-q4_k_xl-00002-of-00002.gguf" in proc.stdout
    registry = json.loads(
        (tmp_path / "home" / ".config/codetrail/models.json").read_text(encoding="utf-8")
    )
    assert "big-chat-ud-q4-k-xl" not in registry  # 壞模型不會被選成 main

    # 唯一的 main 候選也不見了 → 硬失敗,錯誤訊息直接寫缺哪個 shard 檔
    shutil.rmtree(models / "vl")
    proc2 = _run(tmp_path, "--yes", "--models-dir", str(models))
    assert proc2.returncode == 2
    assert "初步判定不通過" in proc2.stderr
    assert "缺少 shard" in proc2.stderr
    assert "big-chat-ud-q4_k_xl-00002-of-00002.gguf" in proc2.stderr


def test_multiple_mmproj_requires_explicit_choice(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    (models / "vl" / "mmproj-other-F16.gguf").write_bytes(b"x" * 256)

    ambiguous = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert ambiguous.returncode == 2
    assert "--vl-mmproj" in ambiguous.stderr

    explicit = _run(
        tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
        "--vl-mmproj", str(models / "vl" / "mmproj-F16.gguf"),
    )
    assert explicit.returncode == 0, explicit.stderr
    deployment = json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )
    assert deployment["services"]["vl"]["mmproj"] == str(models / "vl" / "mmproj-F16.gguf")


def test_single_gpu_warns_and_shares_one_card(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", "0, NVIDIA GeForce RTX 5090, 32607, 30000, GPU-solo")
    models = _make_models(tmp_path)
    proc = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr
    assert "只偵測到 1 顆 GPU" in proc.stdout
    content = (tmp_path / "home" / "start.sh").read_text(encoding="utf-8")
    assert "export MAIN_GPU=GPU-solo" in content
    assert "export AUX_GPU=GPU-solo" in content


def test_dry_run_writes_nothing(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    proc = _run(tmp_path, "--yes", "--dry-run", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr
    assert "[dry-run]" in proc.stdout
    home = tmp_path / "home"
    assert not (home / ".config").exists()
    assert not (home / "start.sh").exists()


def test_existing_configs_are_backed_up_and_registry_merged(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    home = tmp_path / "home"
    (home / ".config/codetrail").mkdir(parents=True)
    (home / ".config/codetrail/models.json").write_text(
        json.dumps({"old-key": "/somewhere/old.gguf"}), encoding="utf-8"
    )
    (home / ".config/opencode").mkdir(parents=True)
    (home / ".config/opencode/opencode.json").write_text("{}", encoding="utf-8")

    proc = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr

    registry = json.loads((home / ".config/codetrail/models.json").read_text(encoding="utf-8"))
    assert registry["old-key"] == "/somewhere/old.gguf"
    assert "big-chat-ud-q4-k-xl" in registry
    assert list((home / ".config/codetrail").glob("models.json.bak-setconfig-*"))
    assert list((home / ".config/opencode").glob("opencode.json.bak-setconfig-*"))


def test_opencode_merge_preserves_user_config_and_respects_permission(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    home = tmp_path / "home"
    (home / ".config/opencode").mkdir(parents=True)
    existing = {
        "theme": "dark",
        "model": "openrouter/some-cloud-model",
        "enabled_providers": ["openrouter"],
        "provider": {
            "openrouter": {"npm": "@ai-sdk/openai", "options": {"apiKey": "sk-keep"}},
            "llamacpp": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": "http://localhost:8080/v1", "apiKey": "dummy"},
                "models": {"my-old-local": {"name": "my-old-local"}},
            },
        },
        "mcp": {"other-server": {"type": "local", "command": ["echo"], "enabled": True}},
        "permission": {"*": "deny", "bash": "allow"},
    }
    (home / ".config/opencode/opencode.json").write_text(json.dumps(existing), encoding="utf-8")

    proc = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr

    merged = json.loads((home / ".config/opencode/opencode.json").read_text(encoding="utf-8"))
    assert merged["theme"] == "dark"                                   # 使用者設定保留
    assert merged["model"] == "llamacpp/big-chat-ud-q4-k-xl"           # CodeTrail 欄位覆蓋
    assert "openrouter" in merged["provider"]                          # 其他 provider 保留
    assert merged["provider"]["openrouter"]["options"]["apiKey"] == "sk-keep"
    assert "my-old-local" in merged["provider"]["llamacpp"]["models"]  # 舊本機模型項保留
    assert "big-chat-ud-q4-k-xl" in merged["provider"]["llamacpp"]["models"]
    assert "other-server" in merged["mcp"]                             # 其他 MCP 保留
    assert "codetrail" in merged["mcp"]
    assert merged["permission"]["bash"] == "allow"                     # 尊重使用者顯式設定…
    assert "已尊重你的設定" in proc.stdout                              # …但要警告
    assert "llamacpp" in merged["enabled_providers"]
    assert "openrouter" in merged["enabled_providers"]


def test_noninteractive_without_flags_fails_with_hint(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    proc = _run(tmp_path, "--models-dir", str(models), stdin="")
    assert proc.returncode == 2
    assert "無互動輸入環境" in proc.stderr


def test_restore_last_backup_round_trips_whole_transaction(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    home = tmp_path / "home"
    (home / ".config/codetrail").mkdir(parents=True)
    (home / ".config/codetrail/models.json").write_text('{"marker": "/old.gguf"}', encoding="utf-8")

    assert _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models)).returncode == 0
    registry = json.loads((home / ".config/codetrail/models.json").read_text(encoding="utf-8"))
    assert "big-chat-ud-q4-k-xl" in registry
    assert (home / ".config/codetrail/setconfig-last-transaction.json").is_file()

    proc = _run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 0, proc.stderr
    # manifest 整批還原:當時存在的檔案回到備份內容…
    restored = json.loads((home / ".config/codetrail/models.json").read_text(encoding="utf-8"))
    assert restored == {"marker": "/old.gguf"}
    # …當時不存在的檔案被移除,不會殘留半套設定
    assert not (home / ".config/codetrail/deployment.json").exists()
    assert not (home / ".config/opencode/opencode.json").exists()
    assert not (home / "start.sh").exists()


def test_vl_model_is_not_auto_selected_as_main(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    # VL 模型比一般聊天模型大很多:舊排序(越大越優先)會誤選它當 main
    _sparse(models / "vl" / "vl-model-q6.gguf", 8 * GIB)

    proc = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr
    deployment = json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )
    assert deployment["services"]["main"]["model"] == "big-chat-ud-q4-k-xl"
    assert deployment["services"]["vl"]["model"].endswith("vl-model-q6.gguf")


def test_only_vl_main_candidate_proceeds_with_warning(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    shutil.rmtree(models / "big-chat")

    proc = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr
    assert "同時當 main" in proc.stdout
    deployment = json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )
    assert deployment["services"]["main"]["model"] == "vl-model-q6"


def test_missing_fit_stops_at_preflight_before_any_questions(tmp_path):
    """--fit 是硬需求(VL placement):缺少時要在前置檢查就擋下,
    不能讓使用者答完所有互動題才發現白忙一場。"""
    _write_fake_nvidia_smi(tmp_path / "bin", "0, Small GPU, 24576, 20000, GPU-small")
    models = _make_models(tmp_path)
    _write_fake_llama(tmp_path, help_flags="--reranking --mmproj")  # 沒有 --fit

    proc = _run(tmp_path, "--yes", "--models-dir", str(models))
    assert proc.returncode == 2
    assert "安全的 VL placement 需要 llama-server --fit" in proc.stderr
    assert "重新 build" in proc.stderr
    # 前置(preflight)就失敗:還沒開始掃描模型/互動
    assert "[3/5]" not in proc.stdout


def test_llama_help_loader_failure_is_not_misdiagnosed_as_missing_flags(tmp_path):
    """--help 因動態庫問題跑不起來時,要指向執行環境,不能誤診成缺 --reranking。"""
    _write_fake_nvidia_smi(tmp_path / "bin", "0, Small GPU, 24576, 20000, GPU-small")
    models = _make_models(tmp_path)
    executable = tmp_path / "llama-server"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'error while loading shared libraries: libcudart.so.13: cannot open' >&2\n"
        "exit 127\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    proc = _run(tmp_path, "--yes", "--models-dir", str(models))
    assert proc.returncode == 2
    assert "無法執行" in proc.stderr
    assert "LD_LIBRARY_PATH" in proc.stderr
    assert "libcudart.so.13" in proc.stderr          # 原始錯誤要轉述給使用者
    assert "不支援 --reranking" not in proc.stderr    # 不得誤診成旗標問題


def test_capacity_main_too_big_for_ram_hard_stops(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", "0, Small GPU, 24576, 20000, GPU-small")
    models = _make_models(tmp_path)
    _sparse_dense_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00001-of-00002.gguf", 13 * GIB
    )
    _sparse_dense_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00002-of-00002.gguf", 12 * GIB
    )
    # dense 25GiB > 24GiB VRAM → 走 --fit CPU offload;RAM 只有 16GiB → 容量硬停。
    low_meminfo = tmp_path / "low-meminfo"
    low_meminfo.write_text(
        "MemTotal:       16777216 kB\nMemAvailable:   8388608 kB\n",
        encoding="utf-8",
    )
    proc = _run(
        tmp_path, "--yes", "--models-dir", str(models),
        env_overrides={"AICODE_SET_CONFIG_MEMINFO": str(low_meminfo)},
    )
    assert proc.returncode == 2
    assert "容量判定不通過" in proc.stderr
    assert "RAM" in proc.stderr
    assert "--ignore-capacity" in proc.stderr

    # --ignore-capacity escape hatch:同樣條件降為警告並完成設定。
    forced = _run(
        tmp_path,
        "--yes", "--no-preview", "--ignore-capacity", "--models-dir", str(models),
        env_overrides={"AICODE_SET_CONFIG_MEMINFO": str(low_meminfo)},
    )
    assert forced.returncode == 0, forced.stderr
    assert "--ignore-capacity" in forced.stdout


def test_capacity_shared_gpu_raises_fit_target(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", "0, Small GPU, 24576, 20000, GPU-small")
    models = _make_models(tmp_path)
    _sparse_dense_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00001-of-00002.gguf", 13 * GIB
    )
    _sparse_dense_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00002-of-00002.gguf", 12 * GIB
    )
    _sparse(models / "vl" / "vl-model-q6.gguf", 4 * GIB)  # 共卡的附屬模型夠大才看得出抬高

    proc = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr
    deployment = json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )
    parameters = deployment["services"]["main"]["parameters"]
    assert parameters["fit"] == "on"
    assert parameters["gpu_layers"] == "auto"
    # 預設 5120 → 抬高到「同卡附屬 + 保留」量級
    assert 9000 < parameters["fit_target"] < 11000


def test_yes_auto_selects_cpu_moe_for_oversized_moe_main_only(tmp_path):
    _write_fake_nvidia_smi(
        tmp_path / "bin", "0, NVIDIA GeForce RTX 5090, 32607, 30000, GPU-solo"
    )
    models = _make_models(tmp_path)
    _sparse_moe_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00001-of-00002.gguf",
        14 * GIB,
        21 * GIB // 2,
    )
    _sparse_moe_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00002-of-00002.gguf",
        12 * GIB,
        9 * GIB,
    )

    proc = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "自動選擇主模型模式:CPU-MoE" in proc.stdout
    deployment = json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )
    assert deployment["services"]["main"]["parameters"]["cpu_moe"] is True
    assert deployment["services"]["main"]["parameters"]["fit"] == "off"
    for role in ("embedding", "reranker", "vl"):
        assert "cpu_moe" not in deployment["services"][role].get("parameters", {})


def test_capacity_aux_alone_exceeding_budget_fails(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", "0, Tiny GPU, 4096, 3500, GPU-tiny")
    models = _make_models(tmp_path)
    _sparse(models / "vl" / "vl-model-q6.gguf", 8 * GIB)

    proc = _run(tmp_path, "--yes", "--models-dir", str(models))
    assert proc.returncode == 2
    assert "容量判定不通過" in proc.stderr
    assert "附屬模型" in proc.stderr


def test_start_sh_pins_validated_llama_bin(tmp_path):
    """set_config 用 LLAMA_BIN 驗證旗標 → 產生的 start.sh 必須寫死同一顆 binary,
    否則新 shell 啟動的是另一顆(可能沒 --fit 的)llama-server。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    assert _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models)).returncode == 0
    content = (tmp_path / "home" / "start.sh").read_text(encoding="utf-8")
    assert f"export LLAMA_BIN={tmp_path / 'llama-server'}" in content


def test_rerun_keeps_current_main_model_instead_of_reference_hint(tmp_path):
    """重跑 --yes 不得把使用者現用的主模型靜默換成 hint 排序第一的 reference 模型。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    assert _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models)).returncode == 0
    deployment_path = tmp_path / "home" / ".config/codetrail/deployment.json"
    first = json.loads(deployment_path.read_text(encoding="utf-8"))
    assert first["services"]["main"]["model"] == "big-chat-ud-q4-k-xl"

    # 之後才下載了 hint 首選的 reference 模型(排序會排最前)…
    hinted = models / "Qwen3-235B-A22B-Thinking-2507-GGUF"
    hinted.mkdir()
    (hinted / "Qwen3-235B-A22B-Thinking-2507-UD-Q4_K_XL.gguf").write_bytes(b"x" * 4096)

    rerun = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert rerun.returncode == 0, rerun.stderr
    assert "沿用目前設定" in rerun.stdout
    second = json.loads(deployment_path.read_text(encoding="utf-8"))
    # 主模型仍是使用者現用的 big-chat,不被 hint 蓋掉
    assert second["services"]["main"]["model"] == "big-chat-ud-q4-k-xl"

    # 明確給旗標仍可換模型(旗標優先於沿用)
    switched = _run(
        tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
        "--main-model", str(hinted / "Qwen3-235B-A22B-Thinking-2507-UD-Q4_K_XL.gguf"),
    )
    assert switched.returncode == 0, switched.stderr
    third = json.loads(deployment_path.read_text(encoding="utf-8"))
    assert third["services"]["main"]["model"].startswith("qwen3-235b")


def test_rerun_keeps_ctx_threads_and_reranker_ctx_values(tmp_path):
    """重跑 --yes 也要沿用數值(ctx/threads/reranker ctx),不悄悄重設回預設。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    first = _run(
        tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
        "--ctx", "32768", "--threads", "12", "--rerank-ctx", "4096",
    )
    assert first.returncode == 0, first.stderr

    rerun = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert rerun.returncode == 0, rerun.stderr
    deployment = json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )
    assert deployment["services"]["main"]["ctx"] == 32768
    assert deployment["services"]["main"]["parameters"]["threads"] == 12
    assert deployment["services"]["reranker"]["ctx"] == 4096

    opencode = json.loads(
        (tmp_path / "home" / ".config/opencode/opencode.json").read_text(encoding="utf-8")
    )
    limit = opencode["provider"]["llamacpp"]["models"]["big-chat-ud-q4-k-xl"]["limit"]
    assert limit["context"] == 32768  # OpenCode limit.context 跟著沿用的 ctx


def test_symlinked_config_files_are_written_through(tmp_path):
    """dotfiles 使用者的 opencode.json 是 symlink → 寫穿到目標,保留連結。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    home = tmp_path / "home"
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "opencode.json"
    real.write_text("{}", encoding="utf-8")
    (home / ".config/opencode").mkdir(parents=True)
    (home / ".config/opencode/opencode.json").symlink_to(real)

    proc = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr
    link = home / ".config/opencode/opencode.json"
    assert link.is_symlink()  # 連結還在,沒被換成一般檔
    merged = json.loads(real.read_text(encoding="utf-8"))
    assert merged["model"] == "llamacpp/big-chat-ud-q4-k-xl"  # 內容寫到目標
    assert "symlink" in proc.stdout


def test_invalid_registry_entries_are_dropped_with_warning(tmp_path):
    """models.json 有格式非法的手寫項目時要剔除並警告,
    否則啟動時整份 registry 會被 loader 拒絕。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    home = tmp_path / "home"
    (home / ".config/codetrail").mkdir(parents=True)
    (home / ".config/codetrail/models.json").write_text(
        json.dumps({
            "good-key": "/somewhere/model.gguf",
            "bad key with spaces": "/somewhere/other.gguf",
            "relative-path": "not/absolute.gguf",
        }),
        encoding="utf-8",
    )

    proc = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr
    assert "格式不合法的項目已剔除" in proc.stdout
    registry = json.loads((home / ".config/codetrail/models.json").read_text(encoding="utf-8"))
    assert "good-key" in registry
    assert "bad key with spaces" not in registry
    assert "relative-path" not in registry


def test_rerun_preserves_hand_added_sampling_params_and_warns_on_dropped(tmp_path):
    """工具自己教使用者把取樣參數加進 deployment.json → 重跑必須保留;
    其他未涵蓋鍵(如 n_cpu_moe)要警告已捨棄,不能靜默消失。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    assert _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models)).returncode == 0
    deployment_path = tmp_path / "home" / ".config/codetrail/deployment.json"
    config = json.loads(deployment_path.read_text(encoding="utf-8"))
    config["services"]["main"]["parameters"]["temperature"] = 0.6
    config["services"]["main"]["parameters"]["top_p"] = 0.95
    config["services"]["main"]["parameters"]["n_cpu_moe"] = 90
    deployment_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    rerun = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert rerun.returncode == 0, rerun.stderr + rerun.stdout
    assert "保留你手動加在 deployment.json 的 main 取樣參數" in rerun.stdout
    assert "temperature=0.6" in rerun.stdout
    assert "已捨棄:n_cpu_moe=90" in rerun.stdout

    merged = json.loads(deployment_path.read_text(encoding="utf-8"))
    parameters = merged["services"]["main"]["parameters"]
    assert parameters["temperature"] == 0.6
    assert parameters["top_p"] == 0.95
    assert "n_cpu_moe" not in parameters


def test_summary_advanced_reentry_carries_prior_answers(tmp_path):
    """摘要頁按 a:進階逐項重問,但要帶上一輪選擇當預設;再次確認後正常寫入。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    stdin = "\n\n\n\n" + "a\n" + "\n" * 20
    proc = _run(tmp_path, "--no-preview", "--models-dir", str(models), stdin=stdin)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "進階逐項設定:以上一輪選擇為預設" in proc.stdout
    assert "上輪選擇,預設" in proc.stdout
    assert (tmp_path / "home" / "start.sh").exists()


def test_summary_invalid_input_reprompts_instead_of_aborting(tmp_path):
    """摘要頁打錯字要重新詢問,不能直接 exit 2 丟掉使用者剛答完的所有選擇。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    proc = _run(
        tmp_path, "--no-preview", "--models-dir", str(models), stdin="\n\n\n\nzz\nq\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert "無效輸入 'zz'" in proc.stdout
    assert "未寫入任何檔案" in proc.stdout
    assert not (tmp_path / "home" / "start.sh").exists()


def test_capacity_moe_offers_inplace_cpu_moe_switch(tmp_path):
    """互動時 MoE 在一般模式塞不下 → 就地詢問改用 CPU-MoE,不逼使用者整段重來。"""
    _write_fake_nvidia_smi(tmp_path / "bin", "0, Small GPU, 24576, 20000, GPU-small")
    models = _make_models(tmp_path)
    _sparse_moe_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00001-of-00002.gguf",
        14 * GIB, 11 * GIB,
    )
    _sparse_moe_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00002-of-00002.gguf",
        12 * GIB, 10 * GIB,
    )

    # main=Enter、CPU-MoE 強制答 n(一般模式)、reranker、rerank ctx、
    # 容量判定的就地切換提問=Enter(Y)、摘要確認=Enter。
    proc = _run(
        tmp_path, "--no-preview", "--models-dir", str(models), stdin="\nn\n\n\n\n\n"
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "直接改用 CPU-MoE 模式重新規劃?" in proc.stdout
    assert "已就地改用 CPU-MoE" in proc.stdout
    deployment = json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )
    assert deployment["services"]["main"]["parameters"]["cpu_moe"] is True


def test_model_path_flag_rescues_missing_category(tmp_path):
    """模型不在 models-dir 時,--rerank-model <路徑> 必須能救援「缺類別」硬停。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path, with_reranker=False)
    external = tmp_path / "elsewhere"
    external.mkdir()
    (external / "my-reranker.gguf").write_bytes(b"x" * 512)

    proc = _run(
        tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
        "--rerank-model", str(external / "my-reranker.gguf"),
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    deployment = json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )
    assert deployment["services"]["reranker"]["model"] == str(external / "my-reranker.gguf")

    # 數字編號沒有候選可對應 → 仍要硬停,且訊息指向用路徑
    numbered = _run(
        tmp_path, "--yes", "--models-dir", str(models), "--rerank-model", "1",
    )
    assert numbered.returncode == 2
    assert "初步判定不通過" in numbered.stderr


def test_generated_start_sh_subcommand_guard_and_logs_validation(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    assert _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models)).returncode == 0
    start = tmp_path / "home" / "start.sh"

    def run_start(*args: str):
        return subprocess.run(
            ["bash", str(start), *args],
            env=_env(tmp_path),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    helped = run_start("help")
    assert helped.returncode == 0, helped.stderr
    assert "用法" in helped.stdout
    assert "logs [role]" in helped.stdout

    typo = run_start("stauts")  # 拼錯不得直接進入啟動流程
    assert typo.returncode == 2
    assert "未知子命令" in typo.stderr

    bad_role = run_start("logs", "gpu")
    assert bad_role.returncode == 2
    assert "未知 role" in bad_role.stderr

    never_started = run_start("logs", "main")
    assert never_started.returncode == 1
    assert "尚未啟動過" in never_started.stderr


def test_rtx2000_aux_capacity_prefers_bge_and_accepts_measured_model_sizes(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    _sparse(models / "bge-m3" / "bge-m3-f16.gguf", 1100 * 1024**2)
    _sparse(
        models / "bge-reranker-v2-m3" / "bge-reranker-v2-m3-Q8_0.gguf",
        610 * 1024**2,
    )
    _sparse(models / "qwen3-reranker-0.6b" / "qwen3-reranker-0.6b-q8_0.gguf", 610 * 1024**2)
    _sparse(models / "vl" / "vl-model-q6.gguf", 7 * GIB)
    _sparse(models / "vl" / "mmproj-F16.gguf", 876 * 1024**2)

    proc = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))

    assert proc.returncode == 0, proc.stderr + proc.stdout
    deployment = json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )
    assert deployment["services"]["reranker"]["model"].endswith(
        "bge-reranker-v2-m3-Q8_0.gguf"
    )


def test_rtx2000_aux_capacity_uses_selected_qwen3_reranker_ctx(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    shutil.rmtree(models / "bge-reranker-v2-m3")
    _sparse(models / "bge-m3" / "bge-m3-f16.gguf", 1100 * 1024**2)
    _sparse(models / "qwen3-reranker-0.6b" / "qwen3-reranker-0.6b-q8_0.gguf", 610 * 1024**2)
    _sparse(models / "vl" / "vl-model-q6.gguf", 7 * GIB)
    _sparse(models / "vl" / "mmproj-F16.gguf", 876 * 1024**2)

    recommended = _run(
        tmp_path,
        "--yes",
        "--no-preview",
        "--models-dir",
        str(models),
    )

    assert recommended.returncode == 0, recommended.stderr + recommended.stdout
    deployment = json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(
            encoding="utf-8"
        )
    )
    assert deployment["services"]["reranker"]["ctx"] == 2048
    assert deployment["services"]["reranker"]["batch"] == 2048
    assert deployment["services"]["reranker"]["ubatch"] == 2048

    forced_long = _run(
        tmp_path,
        "--yes",
        "--no-preview",
        "--rerank-ctx",
        "8192",
        "--models-dir",
        str(models),
    )

    assert forced_long.returncode == 2
    assert "Qwen3-Reranker" in forced_long.stderr
    assert "ctx=8192" in forced_long.stderr
    assert "6144 MiB" in forced_long.stderr
    assert "bge-reranker-v2-m3" in forced_long.stderr


# ---------------------------------------------------------------------------
# 2026-08-05 入口第三方 review 修復的行為契約
# ---------------------------------------------------------------------------

def test_generated_start_sh_exports_before_subcommand_dispatch(tmp_path):
    """GPU/模型 exports 必須在 case dispatch 之前:status --strict 的 wrong-GPU
    檢查唯一來源是這些環境變數,放在 case 之後 status 路徑會拿不到期望值。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    assert _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models)).returncode == 0
    content = (tmp_path / "home" / "start.sh").read_text(encoding="utf-8")
    dispatch = content.index('case "${1:-}"')
    assert content.index("unset ") < content.index("export AICODE_MODEL=") < dispatch
    assert content.index("export MAIN_GPU=") < dispatch
    assert content.index("export LLAMA_BIN=") < dispatch


def test_opencode_json_written_owner_only_and_dry_run_redacts_api_keys(tmp_path):
    """opencode.json 合併後帶著使用者 provider 的 apiKey:檔案必須 0600
    (不得把原本的 0600 重跑成 0644),--dry-run 印出的內容也不得出現金鑰原文。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    home = tmp_path / "home"
    (home / ".config/opencode").mkdir(parents=True)
    secret = "sk-live-super-secret-123"
    opencode_path = home / ".config/opencode/opencode.json"
    opencode_path.write_text(
        json.dumps({"provider": {"openrouter": {"options": {"apiKey": secret}}}}),
        encoding="utf-8",
    )
    opencode_path.chmod(0o600)

    dry = _run(tmp_path, "--yes", "--dry-run", "--models-dir", str(models))
    assert dry.returncode == 0, dry.stderr + dry.stdout
    assert secret not in dry.stdout
    assert "***redacted***" in dry.stdout
    assert "憑證類欄位值已遮罩" in dry.stdout
    assert stat.S_IMODE(opencode_path.stat().st_mode) == 0o600  # dry-run 不動檔案

    real = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert real.returncode == 0, real.stderr + real.stdout
    assert stat.S_IMODE(opencode_path.stat().st_mode) == 0o600
    merged = json.loads(opencode_path.read_text(encoding="utf-8"))
    # 遮罩只影響顯示,實際寫入的金鑰原樣保留
    assert merged["provider"]["openrouter"]["options"]["apiKey"] == secret


def test_restart_subprocess_env_is_sanitized(monkeypatch):
    """[R] 自動重啟的 quit/start 子程序不得繼承泛用 SESSION/override env:
    桌面環境的 SESSION 會讓 stop 殺錯無關 session、漏掉真正的 codetrail-rag。"""
    from scripts import set_config as sc

    monkeypatch.setenv("SESSION", "unrelated-desktop-session")
    monkeypatch.setenv("MAIN_SESSION", "custom-main")
    monkeypatch.setenv("AUX_SESSION", "custom-aux")
    monkeypatch.setenv("MAIN_GPU", "GPU-x")
    monkeypatch.setenv("KEEP_ME", "1")
    env = sc._sanitized_subprocess_env()
    assert "SESSION" not in env
    assert "MAIN_SESSION" not in env
    assert "AUX_SESSION" not in env
    assert "MAIN_GPU" not in env
    assert env.get("KEEP_ME") == "1"

    calls = []

    class _Result:
        returncode = 0

    def fake_run(cmd, check=False, env=None, **_kwargs):
        calls.append((list(cmd), env))
        return _Result()

    monkeypatch.setattr(sc.subprocess, "run", fake_run)
    rc = sc._restart_servers(Path("/fake/home/start.sh"))
    assert rc == 0
    assert len(calls) == 2
    assert calls[0][0][1].endswith("quit.sh")
    assert calls[1][0][1] == "/fake/home/start.sh"
    for _cmd, env in calls:
        assert env is not None
        assert "SESSION" not in env and "MAIN_GPU" not in env


def test_existing_gpu_selectors_parse_generated_start_sh(tmp_path):
    """GPU 分卡的唯一持久紀錄是產生的 start.sh export;重跑沿用要能讀回。"""
    from scripts import set_config as sc

    start = tmp_path / "start.sh"
    start.write_text(
        "#!/usr/bin/env bash\n"
        f"# {sc.GENERATED_MARKER} — test\n"
        "export MAIN_GPU=GPU-aaaa\n"
        "export AUX_GPU='GPU-bbbb'\n",
        encoding="utf-8",
    )
    assert sc._existing_gpu_selectors(start) == {
        "main": "GPU-aaaa",
        "embedding": "GPU-bbbb",
        "reranker": "GPU-bbbb",
        "vl": "GPU-bbbb",
    }
    # 逐 role export 的形式
    start.write_text(
        f"# {sc.GENERATED_MARKER}\n"
        "export MAIN_GPU=GPU-1\nexport EMBED_GPU=GPU-2\n"
        "export RERANK_GPU=GPU-3\nexport VL_GPU=GPU-4\n",
        encoding="utf-8",
    )
    assert sc._existing_gpu_selectors(start)["reranker"] == "GPU-3"
    # 非本工具產生 → 不解析;檔案不存在 → 空
    start.write_text("#!/usr/bin/env bash\nexport MAIN_GPU=GPU-x\n", encoding="utf-8")
    assert sc._existing_gpu_selectors(start) == {}
    assert sc._existing_gpu_selectors(tmp_path / "missing.sh") == {}


def test_rerun_keeps_custom_gpu_split(tmp_path):
    """重跑 --yes 不得把使用者自訂的 GPU 分卡靜默重設回 VRAM 建議預設。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    first = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
                 "--main-gpu", "1", "--embed-gpu", "0")
    assert first.returncode == 0, first.stderr
    content = (tmp_path / "home" / "start.sh").read_text(encoding="utf-8")
    assert "export MAIN_GPU=GPU-bbbb-2000" in content
    assert "export EMBED_GPU=GPU-aaaa-5090" in content

    rerun = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert rerun.returncode == 0, rerun.stderr + rerun.stdout
    assert "沿用目前 GPU 分卡" in rerun.stdout
    content = (tmp_path / "home" / "start.sh").read_text(encoding="utf-8")
    assert "export MAIN_GPU=GPU-bbbb-2000" in content   # 自訂 main GPU 保留
    assert "export EMBED_GPU=GPU-aaaa-5090" in content  # 自訂 embed GPU 保留

    # 旗標仍優先於沿用
    switched = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
                    "--main-gpu", "0")
    assert switched.returncode == 0, switched.stderr
    content = (tmp_path / "home" / "start.sh").read_text(encoding="utf-8")
    assert "export MAIN_GPU=GPU-aaaa-5090" in content


def test_rerun_keeps_forced_cpu_moe_mode(tmp_path):
    """--cpu-moe 覆寫過容量建議之後,重跑 --yes 要沿用該模式,不悄悄退回推薦值。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    moe_dir = models / "moe-chat"
    moe_dir.mkdir()
    # 小 MoE:容量上一般模式塞得下(推薦=一般),使用者仍強制 CPU-MoE。
    _sparse_moe_gguf(moe_dir / "moe-chat-q4.gguf", 4096, 2048)
    first = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
                 "--main-model", str(moe_dir / "moe-chat-q4.gguf"), "--cpu-moe")
    assert first.returncode == 0, first.stderr + first.stdout
    deployment_path = tmp_path / "home" / ".config/codetrail/deployment.json"
    deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
    assert deployment["services"]["main"]["parameters"]["cpu_moe"] is True

    rerun = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert rerun.returncode == 0, rerun.stderr + rerun.stdout
    assert "沿用既有主模型模式" in rerun.stdout
    deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
    assert deployment["services"]["main"]["parameters"]["cpu_moe"] is True
    # 明確旗標仍可改回一般模式
    switched = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
                    "--no-cpu-moe")
    assert switched.returncode == 0, switched.stderr
    deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
    assert "cpu_moe" not in deployment["services"]["main"]["parameters"]


def test_rerun_keeps_hand_edited_port_and_base_url(tmp_path):
    """port/base_url 屬使用者領域(本工具從不寫)→ 手改過的值重跑要原樣保留。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    assert _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models)).returncode == 0
    deployment_path = tmp_path / "home" / ".config/codetrail/deployment.json"
    config = json.loads(deployment_path.read_text(encoding="utf-8"))
    config["services"]["main"]["port"] = 18080
    config["services"]["main"]["base_url"] = "http://127.0.0.1:18080"
    deployment_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    rerun = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert rerun.returncode == 0, rerun.stderr + rerun.stdout
    assert "保留你手動設定的 services.main.port=18080" in rerun.stdout
    merged = json.loads(deployment_path.read_text(encoding="utf-8"))
    assert merged["services"]["main"]["port"] == 18080
    assert merged["services"]["main"]["base_url"] == "http://127.0.0.1:18080"
    # OpenCode 的 baseURL 必須跟著保留下來的 main endpoint,不能仍寫死 8080
    opencode = json.loads(
        (tmp_path / "home" / ".config/opencode/opencode.json").read_text(encoding="utf-8")
    )
    options = opencode["provider"]["llamacpp"]["options"]
    assert options["baseURL"] == "http://127.0.0.1:18080/v1"
    assert "對齊 deployment 的 main endpoint" in rerun.stdout


def test_rerun_keeps_out_of_tree_main_model(tmp_path):
    """現用主模型在 models-dir 之外:重跑 --yes 不得靜默換成掃描到的模型。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    _sparse_dense_gguf(outside / "external-chat-q4.gguf", 4096)
    first = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
                 "--main-model", str(outside / "external-chat-q4.gguf"))
    assert first.returncode == 0, first.stderr + first.stdout
    deployment_path = tmp_path / "home" / ".config/codetrail/deployment.json"
    deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
    assert deployment["services"]["main"]["model"] == "external-chat-q4"

    rerun = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert rerun.returncode == 0, rerun.stderr + rerun.stdout
    assert "之外" in rerun.stdout
    deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
    assert deployment["services"]["main"]["model"] == "external-chat-q4"


def test_opencode_config_env_var_is_honored(tmp_path):
    """OpenCode 與 config.py/aicode 都先讀 OPENCODE_CONFIG;set_config 寫死預設
    路徑會做出「顯示 PASS 但完全沒生效」的設定。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    custom = tmp_path / "custom" / "oc.json"
    proc = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
                env_overrides={"OPENCODE_CONFIG": str(custom)})
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OPENCODE_CONFIG 已設定" in proc.stdout
    written = json.loads(custom.read_text(encoding="utf-8"))
    assert written["model"] == "llamacpp/big-chat-ud-q4-k-xl"
    assert not (tmp_path / "home" / ".config/opencode/opencode.json").exists()


def test_relative_models_dir_and_llama_bin_are_stored_absolute(tmp_path):
    """相對路徑立刻轉絕對:--models-dir ./models 不得走到最後 schema 驗證才爆;
    相對 LLAMA_BIN 不得原樣寫進 ~/start.sh(換目錄執行就找不到)。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    env = _env(tmp_path)
    env["LLAMA_BIN"] = "./llama-server"
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--skip-deps-check", "--yes", "--no-preview",
         "--models-dir", "./models"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    registry = json.loads(
        (tmp_path / "home" / ".config/codetrail/models.json").read_text(encoding="utf-8")
    )
    expected = str(models / "big-chat" / "big-chat-ud-q4_k_xl-00001-of-00002.gguf")
    assert registry["big-chat-ud-q4-k-xl"] == expected
    content = (tmp_path / "home" / "start.sh").read_text(encoding="utf-8")
    assert f"export LLAMA_BIN={tmp_path / 'llama-server'}" in content
    assert "export LLAMA_BIN=./llama-server" not in content


def test_interactive_reranker_ctx_rejects_below_minimum(tmp_path):
    """互動輸入與 CLI 用同一個下限:reranker ctx=1 不得被接受寫入。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    # main、CPU-MoE、reranker 各 Enter;ctx 先給 1(拒絕)再給 256;摘要 Enter。
    proc = _run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin="\n\n\n1\n256\n\n")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "請輸入 ≥ 128 的整數" in proc.stdout
    deployment = json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )
    assert deployment["services"]["reranker"]["ctx"] == 256

    cli = _run(tmp_path, "--yes", "--rerank-ctx", "1", "--models-dir", str(models))
    assert cli.returncode == 2
    assert "128" in cli.stderr


def test_small_main_ctx_clamps_batch_instead_of_failing_validation(tmp_path):
    """--ctx 1024(CLI 允許的最小值)+ 全 GPU 模式:batch 要夾到 ctx,
    不得產生 batch>ctx 再被自己的 schema 驗證打死。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    proc = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
                "--ctx", "1024")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    deployment = json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )
    main = deployment["services"]["main"]
    assert main["ctx"] == 1024
    assert main["batch"] <= 1024
    assert main["ubatch"] <= main["batch"]


def test_opencode_merge_rebuilds_wrong_typed_sections_without_traceback(tmp_path):
    """合法 JSON 但 provider/mcp/models 型別錯誤:降級重建+變更說明,不得 traceback。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    home = tmp_path / "home"
    (home / ".config/opencode").mkdir(parents=True)
    opencode_path = home / ".config/opencode/opencode.json"
    opencode_path.write_text(
        json.dumps({"provider": [], "mcp": "not-an-object", "theme": "dark"}),
        encoding="utf-8",
    )
    proc = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "Traceback" not in proc.stderr
    assert "不是 JSON object" in proc.stdout
    merged = json.loads(opencode_path.read_text(encoding="utf-8"))
    assert merged["theme"] == "dark"
    assert isinstance(merged["provider"], dict) and "llamacpp" in merged["provider"]
    assert isinstance(merged["mcp"], dict) and "codetrail" in merged["mcp"]

    # provider.llamacpp.models 是 list 的變體同樣不得當機
    opencode_path.write_text(
        json.dumps({"provider": {"llamacpp": {"models": []}}}), encoding="utf-8"
    )
    proc = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr + proc.stdout
    merged = json.loads(opencode_path.read_text(encoding="utf-8"))
    assert "big-chat-ud-q4-k-xl" in merged["provider"]["llamacpp"]["models"]


def test_restore_last_backup_dry_run_previews_without_touching_files(tmp_path):
    """--restore-last-backup --dry-run 只預覽會做什麼,絕不動檔案。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    home = tmp_path / "home"
    (home / ".config/codetrail").mkdir(parents=True)
    (home / ".config/codetrail/models.json").write_text('{"marker": "/old.gguf"}', encoding="utf-8")
    assert _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models)).returncode == 0

    after_setup = (home / ".config/codetrail/models.json").read_text(encoding="utf-8")
    proc = _run(tmp_path, "--restore-last-backup", "--dry-run")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "[dry-run]" in proc.stdout
    assert "會還原" in proc.stdout
    assert "會移除" in proc.stdout
    # 檔案完全沒動:還原目標仍是設定後內容,產物一個都沒消失
    assert (home / ".config/codetrail/models.json").read_text(encoding="utf-8") == after_setup
    assert (home / ".config/codetrail/deployment.json").exists()
    assert (home / ".config/opencode/opencode.json").exists()
    assert (home / "start.sh").exists()


def test_pip_fix_hint_matches_python_environment(monkeypatch):
    """venv 的 pip 會拒絕 --user:建議指令必須分環境給。"""
    from scripts import set_config as sc

    monkeypatch.setattr(sc.sys, "prefix", "/venv")
    monkeypatch.setattr(sc.sys, "base_prefix", "/usr")
    hint = sc._pip_fix_hint("/venv/bin/python")
    assert "pip install --user" not in hint  # venv 內的建議指令不得帶 --user
    assert "-m pip install -r" in hint

    monkeypatch.setattr(sc.sys, "prefix", "/usr")
    monkeypatch.setattr(sc.sys, "base_prefix", "/usr")
    hint = sc._pip_fix_hint("/usr/bin/python3")
    assert "--user --break-system-packages" in hint


# ---------------------------------------------------------------------------
# 2026-08-05 第六輪 review 修復的行為契約
# ---------------------------------------------------------------------------

def _make_flat_vl_models(root: Path) -> Path:
    """混放目錄 fixture:flat/ 內兩顆聊天模型 + 一顆 mmproj(歸屬不明)。"""
    models = root / "models"
    (models / "big-chat").mkdir(parents=True)
    _sparse_dense_gguf(models / "big-chat" / "big-chat-q4.gguf", 4096)
    (models / "bge-m3").mkdir()
    (models / "bge-m3" / "bge-m3-f16.gguf").write_bytes(b"x" * 512)
    (models / "bge-reranker-v2-m3").mkdir()
    (models / "bge-reranker-v2-m3" / "bge-reranker-v2-m3-Q8_0.gguf").write_bytes(b"x" * 512)
    flat = models / "flat"
    flat.mkdir()
    (flat / "chat-small-q4.gguf").write_bytes(b"x" * 512)
    (flat / "media-large-q4.gguf").write_bytes(b"x" * 1024)
    (flat / "mmproj-F16.gguf").write_bytes(b"x" * 256)
    return models


def test_flat_dir_vl_pairing_fails_loud_on_yes(tmp_path):
    """混放目錄無法判斷 mmproj 歸屬:--yes 不得自動抓一顆配對,要 fail-loud 指路。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_flat_vl_models(tmp_path)
    proc = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "混放" in proc.stderr
    assert "--vl-model" in proc.stderr
    assert not (tmp_path / "home" / "start.sh").exists()  # 未寫入任何設定

    # 明確 --vl-model 之後可過:唯一 mmproj 與明確指定的模型配對
    explicit = _run(
        tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
        "--vl-model", str(models / "flat" / "media-large-q4.gguf"),
    )
    assert explicit.returncode == 0, explicit.stderr + explicit.stdout
    deployment = json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )
    assert deployment["services"]["vl"]["model"].endswith("media-large-q4.gguf")
    assert deployment["services"]["vl"]["mmproj"].endswith("mmproj-F16.gguf")

    # 之後的重跑沿用既有 VL+mmproj,不再被混放目錄卡住
    rerun = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert rerun.returncode == 0, rerun.stderr + rerun.stdout
    deployment = json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )
    assert deployment["services"]["vl"]["model"].endswith("media-large-q4.gguf")


def test_flat_dir_vl_pairing_asks_explicitly_in_interactive(tmp_path):
    """互動模式遇到混放目錄:就地要求明確選 VL 模型,不悄悄用排序第一名。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_flat_vl_models(tmp_path)
    # main、CPU-MoE、reranker、reranker ctx 各 Enter;VL 明確選 [2] media-large;摘要 Enter。
    proc = _run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin="\n\n\n\n2\n\n")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "無法自動判斷哪顆是 VL 模型" in proc.stdout
    deployment = json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )
    assert deployment["services"]["vl"]["model"].endswith("media-large-q4.gguf")
    assert deployment["services"]["vl"]["mmproj"].endswith("mmproj-F16.gguf")


def test_rerun_rescues_external_model_before_precheck(tmp_path):
    """現用外部模型必須在 precheck 之前 rescue:models-dir 缺該類別時,
    重跑 --yes 不得因「初步判定不通過」直接失敗。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = tmp_path / "models"
    (models / "big-chat").mkdir(parents=True)
    _sparse_dense_gguf(models / "big-chat" / "big-chat-q4.gguf", 4096)
    (models / "bge-reranker-v2-m3").mkdir()
    (models / "bge-reranker-v2-m3" / "bge-reranker-v2-m3-Q8_0.gguf").write_bytes(b"x" * 512)
    (models / "vl").mkdir()
    (models / "vl" / "vl-model-q6.gguf").write_bytes(b"x" * 512)
    (models / "vl" / "mmproj-F16.gguf").write_bytes(b"x" * 256)
    outside = tmp_path / "external-embed"
    outside.mkdir()
    (outside / "bge-m3-f16.gguf").write_bytes(b"x" * 512)

    first = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
                 "--embed-model", str(outside / "bge-m3-f16.gguf"))
    assert first.returncode == 0, first.stderr + first.stdout

    rerun = _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models))
    assert rerun.returncode == 0, rerun.stderr + rerun.stdout
    assert "初步判定 OK" in rerun.stdout
    assert "之外" in rerun.stdout
    deployment = json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )
    assert deployment["services"]["embedding"]["model"] == str(outside / "bge-m3-f16.gguf")


def test_llama_help_exec_failure_hard_stops_without_skip(tmp_path):
    """llama-server --help 連跑都跑不動(OSError):非 --skip-binary-check 必須硬停,
    不得假定支援全部旗標然後顯示 PASS。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    broken = tmp_path / "llama-server"
    broken.write_text("#!/nonexistent-interpreter\n", encoding="utf-8")
    broken.chmod(0o755)

    proc = _run(tmp_path, "--yes", "--models-dir", str(models))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "--help 無法執行" in proc.stderr

    skipped = _run(tmp_path, "--yes", "--no-preview", "--skip-binary-check",
                   "--models-dir", str(models))
    assert skipped.returncode == 0, skipped.stderr + skipped.stdout
    assert "跳過 llama-server 執行檢查" in skipped.stdout


def test_deployment_env_override_split_brain_warns(tmp_path):
    """AICODE_DEPLOYMENT_CONFIG 等 override 有設時要警告:aicode 會讀自訂檔、
    ~/start.sh 卻刻意 unset,兩邊將各用一份設定。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    proc = _run(
        tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
        env_overrides={"AICODE_DEPLOYMENT_CONFIG": str(tmp_path / "custom-deploy.json")},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "偵測到環境變數 AICODE_DEPLOYMENT_CONFIG" in proc.stdout
    assert "各用一份設定" in proc.stdout


def test_logs_accepts_count_and_follow_shorthand(tmp_path):
    """logs 依說明允許省略 role:logs 3 / logs -f 都要能用;多餘參數不得靜默忽略。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    assert _run(tmp_path, "--yes", "--no-preview", "--models-dir", str(models)).returncode == 0
    home = tmp_path / "home"
    start = home / "start.sh"
    state = home / ".local" / "state"
    log_dir = state / "codetrail" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "main.log").write_text("line1\nline2\n", encoding="utf-8")
    env = _env(tmp_path)
    env["XDG_STATE_HOME"] = str(state)

    def run_start(*args: str, timeout: float = 30):
        return subprocess.run(
            ["bash", str(start), *args],
            env=env, capture_output=True, text=True, timeout=timeout, check=False,
        )

    shorthand = run_start("logs", "3")   # 省略 role,數字當行數
    assert shorthand.returncode == 0, shorthand.stderr
    assert "line2" in shorthand.stdout

    extra = run_start("logs", "main", "5", "x")
    assert extra.returncode == 2
    assert "參數過多" in extra.stderr

    still_bad = run_start("logs", "gpu")  # 未知 role 仍要拒絕
    assert still_bad.returncode == 2
    assert "未知 role" in still_bad.stderr

    try:
        run_start("logs", "-f", timeout=2)
        raise AssertionError("logs -f 應進入 tail -f 追蹤模式,不得被當成未知 role")
    except subprocess.TimeoutExpired:
        pass  # tail -f 持續追蹤 → timeout = 解析成功
