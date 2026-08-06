"""set_config.sh / scripts/set_config.py 的離線測試。

nvidia-smi、llama-server 都用 PATH / LLAMA_BIN 上的 stub、模型目錄用 tmp
fixture、HOME 指到 tmp,完全不需要 GPU / 真 llama-server / 真模型。
tmux session 名稱指到不存在的名字,避免碰到開發機上真的 CodeTrail session。

新契約(2026-08-06):set_config 不做容量估算、不推薦數值、互動題沒有預設值;
只驗證輸入在合理範圍。--yes = 純旗標非互動,缺哪個值就報錯。
VRAM 塞不塞得下由使用者以啟動後 nvidia-smi 實測(start.sh 結尾提醒)。
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
    "OPENCODE_CONFIG",  # set_config 會尊重它;開發機殼層若有設定不得洩漏進測試
}

# --yes 的數值旗標(互動題沒有預設值 → 非互動一律得給)。
NUM_FLAGS = ("--ctx", "65536", "--threads", "8", "--rerank-ctx", "8192")
# 標準 fixture(TWO_GPUS + _make_models):main 有 2 個候選(big-chat + VL)、
# reranker 有 2 個(bge + qwen3)、兩顆 GPU → 這些都要旗標;
# embedding / VL 只有一個候選會自動選用。
YES_TWO_GPU = (
    "--yes", "--main-model", "1", "--rerank-model", "1",
    "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1", "--vl-gpu", "1",
    *NUM_FLAGS,
)
# 單 GPU fixture:GPU 自動選用,只剩模型與數值。
YES_ONE_GPU = ("--yes", "--main-model", "1", "--rerank-model", "1", *NUM_FLAGS)

# 標準 fixture 的互動作答順序:
# main 編號、main GPU、embed GPU、reranker 編號、reranker GPU、reranker ctx、
# VL GPU、主模型 ctx、threads、摘要確認。
# (big-chat 是非 GGUF 假檔 → 無法解析 layout → 不會問 CPU-MoE;
#  embedding / VL 單一候選自動選用。)
STDIN_STANDARD = "1\n0\n1\n1\n1\n8192\n1\n65536\n8\n\n"


def _sparse(path: Path, size: int) -> None:
    """建立指定 st_size 的稀疏檔:掃描只看大小,不需要真的占磁碟。"""
    with open(path, "wb") as handle:
        handle.truncate(size)


def _sparse_dense_gguf(path: Path, size: int) -> None:
    """建立有合法 tensor table 的 sparse dense GGUF,供模式分流測試。"""
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


def _sparse_layered_moe_gguf(
    path: Path, *, layer_expert_bytes: dict[int, int], dense_bytes: int
) -> None:
    """多層 expert 的 sparse MoE GGUF:n_cpu_moe 提問需要 per-layer 編號。"""
    names: list[tuple[bytes, int]] = []
    offset = 0
    for layer, size in sorted(layer_expert_bytes.items()):
        names.append((f"blk.{layer}.ffn_up_exps.weight".encode(), offset))
        offset += size
    names.append((b"blk.0.attn_q.weight", offset))
    offset += dense_bytes
    header = struct.pack("<4sIQQ", b"GGUF", 3, len(names), 0)
    table = bytearray()
    for name, tensor_offset in names:
        table.extend(struct.pack("<Q", len(name)) + name)
        table.extend(struct.pack("<I", 1) + struct.pack("<Q", 1))
        table.extend(struct.pack("<I", 0) + struct.pack("<Q", tensor_offset))
    metadata_end = len(header) + len(table)
    data_start = (metadata_end + 31) // 32 * 32
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(table)
        handle.write(b"\0" * (data_start - metadata_end))
        handle.truncate(data_start + offset)


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
    tmp_path: Path, help_flags: str = "--fit --cpu-moe --n-cpu-moe --reranking --mmproj"
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
        # 兩顆 reranker 並存:清單排序 BGE 在前(維護者驗證 hint),選哪顆由使用者輸入。
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
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PATH": f"{tmp_path / 'bin'}:{env.get('PATH', '')}",
            "LLAMA_BIN": str(tmp_path / "llama-server"),
            # 不存在的 session 名稱:避免「偵測到 server 運行中」誤觸開發機上真的 tmux。
            "MAIN_SESSION": "codetrail-test-none-main",
            "SESSION": "codetrail-test-none-rag",
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


def _read_deployment(tmp_path: Path) -> dict:
    return json.loads(
        (tmp_path / "home" / ".config/codetrail/deployment.json").read_text(encoding="utf-8")
    )


def test_help_is_offline_and_exits_zero(tmp_path):
    proc = _run(tmp_path, "--help")
    assert proc.returncode == 0, proc.stderr
    assert "--models-dir" in proc.stdout
    assert "--cpu-moe" in proc.stdout
    assert "--n-cpu-moe" in proc.stdout
    assert "--allow-remote" in proc.stdout
    assert "--rerank-ctx" in proc.stdout
    # 容量/建議機制已移除:相關旗標不得再出現
    assert "--advanced" not in proc.stdout
    assert "--ignore-capacity" not in proc.stdout
    assert "--fit-target" not in proc.stdout


def test_removed_flags_are_rejected(tmp_path):
    for flag in (("--ignore-capacity",), ("--advanced",), ("--fit-target", "5120")):
        proc = _run(tmp_path, *flag)
        assert proc.returncode == 2, flag
        assert "unrecognized arguments" in proc.stderr


def test_yes_run_generates_all_artifacts(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    proc = _run(tmp_path, *YES_TWO_GPU, "--models-dir", str(models))

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
    assert services["main"]["parameters"]["threads"] == 8
    assert services["main"]["parameters"]["gpu_layers"] == 99
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
    # 未給 --allow-remote → 不寫 bind(profile 預設 local)
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
    assert 'start-all.sh "$@" || rc=$?' in content
    assert content.rstrip().endswith('exit "$rc"')
    assert start.stat().st_mode & stat.S_IXUSR

    # 結尾預覽 = start-all --dry-run 的完整指令
    assert "main_command=" in proc.stdout
    assert proc.stdout.count("CUDA_VISIBLE_DEVICES=GPU-bbbb-2000") == 3
    # 三層狀態:設定完成 ≠ 已可使用
    assert "第 1 層" in proc.stdout
    assert "待執行" in proc.stdout
    # 不再有容量預估字樣
    assert "容量預估" not in proc.stdout
    assert "建議配置" not in proc.stdout


def test_yes_missing_value_errors_name_the_flag(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)

    # main 有 2 個候選 → 一開始就要 --main-model
    no_main = _run(tmp_path, "--yes", "--models-dir", str(models))
    assert no_main.returncode == 2
    assert "--main-model" in no_main.stderr

    # 兩顆 GPU → 每個 role 都要 GPU 旗標(缺 --vl-gpu 驗證)
    no_vl_gpu = _run(
        tmp_path, "--yes", "--main-model", "1", "--rerank-model", "1",
        "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1",
        *NUM_FLAGS, "--models-dir", str(models),
    )
    assert no_vl_gpu.returncode == 2
    assert "--vl-gpu" in no_vl_gpu.stderr

    # 數值也沒有預設:缺 --ctx 就報錯
    no_ctx = _run(
        tmp_path, "--yes", "--main-model", "1", "--rerank-model", "1",
        "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1", "--vl-gpu", "1",
        "--threads", "8", "--rerank-ctx", "8192", "--models-dir", str(models),
    )
    assert no_ctx.returncode == 2
    assert "--ctx" in no_ctx.stderr


def test_vl_hint_order_sorts_candidates_first(tmp_path):
    """hint 只影響候選清單排序(維護者驗證的排前面),不再自動選用。"""
    from scripts import set_config as sc

    models = _make_models(tmp_path)
    preferred = models / "qwen3.5-9b"
    preferred.mkdir()
    (preferred / "Qwen3.5-9B-Q6_K.gguf").write_bytes(b"x" * 2048)
    (preferred / "mmproj-F16.gguf").write_bytes(b"x" * 256)
    older = models / "qwen3-vl"
    older.mkdir()
    (older / "Qwen3VL-8B-Instruct-Q4_K_M.gguf").write_bytes(b"x" * 1024)
    (older / "mmproj-Qwen3VL-8B-Instruct-F16.gguf").write_bytes(b"x" * 256)

    candidates, broken = sc.scan_models(models)
    assert not broken
    assert candidates["vl"][0].path.name == "Qwen3.5-9B-Q6_K.gguf"


def test_generated_start_sh_dry_run_pins_gpus_and_binds_loopback(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    assert _run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0

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
    # --dry-run 沒有真的啟動 → 不印 nvidia-smi 監控提醒
    assert "稍微監控" not in proc.stdout


def test_generated_start_sh_ends_with_nvidia_smi_reminder(tmp_path):
    """啟動成功後的最後輸出 = 提醒使用者用 nvidia-smi 稍微監控(set_config 不做容量估算)。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    assert _run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0
    content = (tmp_path / "home" / "start.sh").read_text(encoding="utf-8")
    assert "watch -n 1 nvidia-smi" in content
    assert "稍微監控" in content
    # 提醒在啟動流程之後、只在成功(rc=0)且非 --dry-run 時印出
    assert 'start-all.sh "$@" || rc=$?' in content
    assert '*" --dry-run "*' in content
    assert content.index('start-all.sh "$@"') < content.index("稍微監控")


def test_allow_remote_binds_all_interfaces_with_warning(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    proc = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--allow-remote",
                "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr
    assert "0.0.0.0" in proc.stdout  # 警告文字

    deployment = _read_deployment(tmp_path)
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
    assert _run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0

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


def test_interactive_flow_answers_everything_and_validates_ranges(tmp_path):
    """互動 = 純問答:沒有預設值(Enter 不可過關)、選項外的輸入會重問。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)

    # main 先按 Enter(無效)再輸入 3(超出 1..2,無效)才輸入 1;
    # main GPU 先輸入 5(不存在)再輸入 0;其餘照標準作答。
    stdin = "\n3\n1\n5\n0\n1\n1\n1\n8192\n1\n65536\n8\n\n"
    proc = _run(tmp_path, "--no-preview", "--models-dir", str(models), stdin=stdin)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "【主聊天模型】 — 偵測到的候選" in proc.stdout
    assert "編號只有 1..2" in proc.stdout          # 選項 1/2 輸入 3 → 重問
    assert "無效的 GPU index" in proc.stdout       # GPU 0/1 輸入 5 → 重問
    assert "只有一個候選,自動選用" in proc.stdout  # embedding / VL 唯一候選
    assert "設定摘要" in proc.stdout
    assert "(預設)" not in proc.stdout             # 不再有任何預設標記
    assert "建議配置" not in proc.stdout           # 不再有建議配置頁
    assert (tmp_path / "home" / "start.sh").exists()
    deployment = _read_deployment(tmp_path)
    assert deployment["services"]["main"]["ctx"] == 65536
    assert deployment["services"]["main"]["parameters"]["threads"] == 8


def test_summary_confirm_enter_writes_and_q_aborts(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)

    accepted = _run(
        tmp_path, "--no-preview", "--models-dir", str(models), stdin=STDIN_STANDARD
    )
    assert accepted.returncode == 0, accepted.stderr + accepted.stdout
    assert "設定摘要" in accepted.stdout
    assert (tmp_path / "home" / "start.sh").exists()

    home2 = tmp_path / "home2"
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--skip-deps-check", "--no-preview", "--models-dir", str(models)],
        cwd=REPO_ROOT,
        env={**_env(tmp_path), "HOME": str(home2), "USERPROFILE": str(home2)},
        # 全部答完,摘要頁按 q → 不寫入
        input="1\n0\n1\n1\n1\n8192\n1\n65536\n8\nq\n",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "未寫入任何檔案" in proc.stdout
    assert not (home2 / ".config").exists()
    assert not (home2 / "start.sh").exists()


def test_summary_invalid_input_reprompts_instead_of_aborting(tmp_path):
    """摘要頁打錯字要重新詢問,不能直接 exit 2 丟掉使用者剛答完的所有選擇。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    proc = _run(
        tmp_path, "--no-preview", "--models-dir", str(models),
        stdin="1\n0\n1\n1\n1\n8192\n1\n65536\n8\nzz\nq\n",
    )
    assert proc.returncode == 0, proc.stderr
    assert "無效輸入 'zz'" in proc.stdout
    assert "未寫入任何檔案" in proc.stdout
    assert not (tmp_path / "home" / "start.sh").exists()


def test_flags_override_model_and_gpu(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    proc = _run(
        tmp_path,
        "--yes", "--no-preview", "--models-dir", str(models),
        "--main-model", "1", "--rerank-model", "1",
        "--main-gpu", "1", "--embed-gpu", "0", "--rerank-gpu", "1", "--vl-gpu", "1",
        *NUM_FLAGS,
    )
    assert proc.returncode == 0, proc.stderr
    content = (tmp_path / "home" / "start.sh").read_text(encoding="utf-8")
    assert "export MAIN_GPU=GPU-bbbb-2000" in content
    # aux 三顆不同卡(embed=0、rerank/vl=1)→ 逐 role export
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

    # big-chat 假檔不是 GGUF → layout 無法解析;--cpu-moe 仍尊重旗標,
    # 但 build 不支援就要硬停。
    proc = _run(
        tmp_path, "--yes", "--cpu-moe", "--no-preview", "--models-dir", str(models),
        "--main-model", "1", "--main-gpu", "0",
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
    proc = _run(
        tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
        "--rerank-model", "1",
        "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1", "--vl-gpu", "1",
        *NUM_FLAGS,
    )
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

    ambiguous = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert ambiguous.returncode == 2
    assert "--vl-mmproj" in ambiguous.stderr

    explicit = _run(
        tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
        "--vl-mmproj", str(models / "vl" / "mmproj-F16.gguf"),
    )
    assert explicit.returncode == 0, explicit.stderr
    deployment = _read_deployment(tmp_path)
    assert deployment["services"]["vl"]["mmproj"] == str(models / "vl" / "mmproj-F16.gguf")


def test_single_gpu_warns_and_shares_one_card(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", "0, NVIDIA GeForce RTX 5090, 32607, 30000, GPU-solo")
    models = _make_models(tmp_path)
    proc = _run(tmp_path, *YES_ONE_GPU, "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr
    assert "只偵測到 1 顆 GPU" in proc.stdout
    content = (tmp_path / "home" / "start.sh").read_text(encoding="utf-8")
    assert "export MAIN_GPU=GPU-solo" in content
    assert "export AUX_GPU=GPU-solo" in content


def test_no_capacity_estimation_oversized_configs_pass_through(tmp_path):
    """set_config 完全不做容量估算:遠超 VRAM/RAM 的組合照樣產生設定
    (塞不塞得下由使用者以啟動後 nvidia-smi 實測;start.sh 結尾提醒)。"""
    _write_fake_nvidia_smi(tmp_path / "bin", "0, Tiny GPU, 4096, 3500, GPU-tiny")
    models = _make_models(tmp_path)
    # 25 GiB dense 主模型 + 8 GiB VL,全部指到 4 GiB 的 GPU。
    _sparse_dense_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00001-of-00002.gguf", 13 * GIB
    )
    _sparse_dense_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00002-of-00002.gguf", 12 * GIB
    )
    _sparse(models / "vl" / "vl-model-q6.gguf", 8 * GIB)

    proc = _run(tmp_path, *YES_ONE_GPU, "--no-preview", "--models-dir", str(models))

    assert proc.returncode == 0, proc.stderr + proc.stdout
    combined = proc.stdout + proc.stderr
    assert "容量判定" not in combined
    assert "容量預估" not in combined
    assert "不做容量估算" in proc.stdout
    parameters = _read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert parameters["gpu_layers"] == 99      # 不再退 --fit 自動配置
    assert "fit_target" not in parameters
    assert parameters["fit"] == "off"


def test_yes_moe_main_requires_explicit_mode_flag(tmp_path):
    """MoE 主模型的運行模式沒有預設:--yes 必須用旗標指定,不再自動選。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    _sparse_moe_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00001-of-00002.gguf",
        14 * GIB, 11 * GIB,
    )
    _sparse_moe_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00002-of-00002.gguf",
        12 * GIB, 10 * GIB,
    )

    proc = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 2
    assert "--cpu-moe / --no-cpu-moe / --n-cpu-moe" in proc.stderr

    forced = _run(tmp_path, *YES_TWO_GPU, "--cpu-moe", "--no-preview",
                  "--models-dir", str(models))
    assert forced.returncode == 0, forced.stderr + forced.stdout
    deployment = _read_deployment(tmp_path)
    assert deployment["services"]["main"]["parameters"]["cpu_moe"] is True
    assert deployment["services"]["main"]["parameters"]["fit"] == "off"
    for role in ("embedding", "reranker", "vl"):
        assert "cpu_moe" not in deployment["services"][role].get("parameters", {})


def test_cpu_moe_flag_on_dense_main_is_rejected(tmp_path):
    """輸入合理性驗證:dense 主模型給 --cpu-moe / --n-cpu-moe 都要報錯。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    _sparse_dense_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00001-of-00002.gguf", 4096
    )
    _sparse_dense_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00002-of-00002.gguf", 4096
    )

    cpu_moe = _run(tmp_path, *YES_TWO_GPU, "--cpu-moe", "--no-preview",
                   "--models-dir", str(models))
    assert cpu_moe.returncode == 2
    assert "只對 MoE 主模型有意義" in cpu_moe.stderr

    n_cpu_moe = _run(tmp_path, *YES_TWO_GPU, "--n-cpu-moe", "3", "--no-preview",
                     "--models-dir", str(models))
    assert n_cpu_moe.returncode == 2
    assert "只對 MoE 主模型有意義" in n_cpu_moe.stderr


def test_dry_run_writes_nothing(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    proc = _run(tmp_path, *YES_TWO_GPU, "--dry-run", "--models-dir", str(models))
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

    proc = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
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

    proc = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
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
    assert "--yes" in proc.stderr


def test_restore_last_backup_round_trips_whole_transaction(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    home = tmp_path / "home"
    (home / ".config/codetrail").mkdir(parents=True)
    (home / ".config/codetrail/models.json").write_text('{"marker": "/old.gguf"}', encoding="utf-8")

    assert _run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0
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
    # VL 模型比一般聊天模型大很多:排序仍把 vl_paired 放最後,[1] 是 big-chat
    _sparse(models / "vl" / "vl-model-q6.gguf", 8 * GIB)

    proc = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr
    deployment = _read_deployment(tmp_path)
    assert deployment["services"]["main"]["model"] == "big-chat-ud-q4-k-xl"
    assert deployment["services"]["vl"]["model"].endswith("vl-model-q6.gguf")


def test_only_vl_main_candidate_proceeds_with_warning(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    shutil.rmtree(models / "big-chat")

    proc = _run(
        tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
        "--rerank-model", "1",
        "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1", "--vl-gpu", "1",
        *NUM_FLAGS,
    )
    assert proc.returncode == 0, proc.stderr
    assert "同時當 main" in proc.stdout
    deployment = _read_deployment(tmp_path)
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


def test_start_sh_pins_validated_llama_bin(tmp_path):
    """set_config 用 LLAMA_BIN 驗證旗標 → 產生的 start.sh 必須寫死同一顆 binary,
    否則新 shell 啟動的是另一顆(可能沒 --fit 的)llama-server。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    assert _run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0
    content = (tmp_path / "home" / "start.sh").read_text(encoding="utf-8")
    assert f"export LLAMA_BIN={tmp_path / 'llama-server'}" in content


def test_rerun_has_no_carryover_current_answers_win(tmp_path):
    """新契約:重跑不沿用任何舊值 —— 每次的設定完全來自本次作答/旗標。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    first = _run(
        tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
        "--main-model", "1", "--rerank-model", "1",
        "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1", "--vl-gpu", "1",
        "--ctx", "32768", "--threads", "12", "--rerank-ctx", "4096",
    )
    assert first.returncode == 0, first.stderr
    assert _read_deployment(tmp_path)["services"]["main"]["ctx"] == 32768

    rerun = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert rerun.returncode == 0, rerun.stderr + rerun.stdout
    assert "沿用" not in rerun.stdout
    deployment = _read_deployment(tmp_path)
    assert deployment["services"]["main"]["ctx"] == 65536      # 本次旗標值,不是舊值
    assert deployment["services"]["main"]["parameters"]["threads"] == 8
    assert deployment["services"]["reranker"]["ctx"] == 8192

    opencode = json.loads(
        (tmp_path / "home" / ".config/opencode/opencode.json").read_text(encoding="utf-8")
    )
    limit = opencode["provider"]["llamacpp"]["models"]["big-chat-ud-q4-k-xl"]["limit"]
    assert limit["context"] == 65536


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

    proc = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
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

    proc = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr
    assert "格式不合法的項目已剔除" in proc.stdout
    registry = json.loads((home / ".config/codetrail/models.json").read_text(encoding="utf-8"))
    assert "good-key" in registry
    assert "bad key with spaces" not in registry
    assert "relative-path" not in registry


def test_rerun_preserves_hand_added_sampling_params_and_warns_on_dropped(tmp_path):
    """工具自己教使用者把取樣參數加進 deployment.json → 重跑必須保留;
    no_mmap 已不由工具管理,手動設定同樣保留;工具管理鍵(n_cpu_moe 等)每次
    依作答重寫、安靜淘汰;其他未涵蓋鍵要警告已捨棄,不能靜默消失。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    assert _run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0
    deployment_path = tmp_path / "home" / ".config/codetrail/deployment.json"
    config = json.loads(deployment_path.read_text(encoding="utf-8"))
    config["services"]["main"]["parameters"]["temperature"] = 0.6
    config["services"]["main"]["parameters"]["top_p"] = 0.95
    config["services"]["main"]["parameters"]["no_mmap"] = True
    config["services"]["main"]["parameters"]["n_cpu_moe"] = 90
    config["services"]["main"]["parameters"]["custom_flag"] = 123
    deployment_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    rerun = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert rerun.returncode == 0, rerun.stderr + rerun.stdout
    assert "保留你手動加在 deployment.json 的 main 取樣參數" in rerun.stdout
    assert "temperature=0.6" in rerun.stdout
    assert "已捨棄:custom_flag=123" in rerun.stdout
    assert "n_cpu_moe=90" not in rerun.stdout  # 工具管理鍵:安靜淘汰,不當成使用者鍵警告

    merged = json.loads(deployment_path.read_text(encoding="utf-8"))
    parameters = merged["services"]["main"]["parameters"]
    assert parameters["temperature"] == 0.6
    assert parameters["top_p"] == 0.95
    assert parameters["no_mmap"] is True
    assert "n_cpu_moe" not in parameters
    assert "custom_flag" not in parameters


def _moe_models_needing_cpu_moe(tmp_path: Path) -> Path:
    """主模型 = 10 層 experts(各 2 GiB)+ dense 6 GiB 的 sparse MoE fixture。"""
    models = _make_models(tmp_path)
    main_dir = models / "big-chat"
    (main_dir / "big-chat-ud-q4_k_xl-00001-of-00002.gguf").unlink()
    (main_dir / "big-chat-ud-q4_k_xl-00002-of-00002.gguf").unlink()
    _sparse_layered_moe_gguf(
        main_dir / "big-moe-ud-q4_k_xl.gguf",
        layer_expert_bytes={index: 2 * GIB for index in range(10)},
        dense_bytes=6 * GIB,
    )
    return models


def test_n_cpu_moe_flag_sets_value_and_implies_cpu_moe_mode(tmp_path):
    """--n-cpu-moe N 非互動指定:蘊含 CPU-MoE 模式,值照寫;與 --no-cpu-moe 互斥。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _moe_models_needing_cpu_moe(tmp_path)

    proc = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--n-cpu-moe", "3",
                "--models-dir", str(models))

    assert proc.returncode == 0, proc.stderr + proc.stdout
    parameters = _read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert parameters["n_cpu_moe"] == 3
    assert "cpu_moe" not in parameters
    start_sh = (tmp_path / "home" / "start.sh").read_text(encoding="utf-8")
    assert "--n-cpu-moe 3" in start_sh

    conflict = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--n-cpu-moe", "3",
                    "--no-cpu-moe", "--models-dir", str(models))
    assert conflict.returncode == 2
    assert "not allowed" in conflict.stderr or "互斥" in conflict.stderr


def test_n_cpu_moe_flag_over_max_index_means_full_cpu_moe(tmp_path):
    """--n-cpu-moe 超過最大 blk 編號 = 全部 experts 留 RAM(寫成 cpu_moe 布林鍵)。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _moe_models_needing_cpu_moe(tmp_path)

    proc = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--n-cpu-moe", "42",
                "--models-dir", str(models))

    assert proc.returncode == 0, proc.stderr + proc.stdout
    parameters = _read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert parameters["cpu_moe"] is True
    assert "n_cpu_moe" not in parameters


def test_interactive_prompt_accepts_typed_n_cpu_moe(tmp_path):
    """互動流程:CPU-MoE 答 y 後,n-cpu-moe 由使用者輸入(無建議值,驗證範圍)。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _moe_models_needing_cpu_moe(tmp_path)

    # main、main GPU、CPU-MoE=y、n-cpu-moe 先 abc(無效)再 3、embed GPU、
    # reranker、reranker GPU、reranker ctx、VL GPU、ctx、threads、摘要確認。
    proc = _run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin="1\n0\ny\nabc\n3\n1\n1\n1\n8192\n1\n65536\n8\n\n")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "【主模型 n-cpu-moe(部分 CPU-MoE)】" in proc.stdout
    assert "共 10 層 experts(blk.0..blk.9)" in proc.stdout
    assert "建議值" not in proc.stdout
    assert "無效輸入:請輸入 0..1024 的整數" in proc.stdout
    parameters = _read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert parameters["n_cpu_moe"] == 3
    assert "cpu_moe" not in parameters


def test_interactive_n_cpu_moe_over_max_index_means_full_cpu_moe(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _moe_models_needing_cpu_moe(tmp_path)

    proc = _run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin="1\n0\ny\n42\n1\n1\n1\n8192\n1\n65536\n8\n\n")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "輸入 ≥ 10 = 全部 experts 留 RAM" in proc.stdout
    parameters = _read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert parameters["cpu_moe"] is True
    assert "n_cpu_moe" not in parameters


def test_interactive_mode_question_requires_explicit_y_or_n(tmp_path):
    """CPU-MoE 模式問題沒有預設答案:Enter / 亂打都要重問,必須明確 y 或 n。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _moe_models_needing_cpu_moe(tmp_path)

    proc = _run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin="1\n0\n\nx\nn\n1\n1\n1\n8192\n1\n65536\n8\n\n")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "請輸入 y 或 n" in proc.stdout
    parameters = _read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert "cpu_moe" not in parameters
    assert "n_cpu_moe" not in parameters
    assert parameters["gpu_layers"] == 99


def test_explicit_cpu_moe_flag_means_full_ram_without_question(tmp_path):
    """--cpu-moe 明確代表全部 experts 放 RAM:不再詢問 n-cpu-moe 檔位。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _moe_models_needing_cpu_moe(tmp_path)

    proc = _run(tmp_path, *YES_TWO_GPU, "--cpu-moe", "--no-preview",
                "--models-dir", str(models))

    assert proc.returncode == 0, proc.stderr + proc.stdout
    parameters = _read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert parameters["cpu_moe"] is True
    assert "n_cpu_moe" not in parameters


def test_build_without_n_cpu_moe_support_degrades_to_full_cpu_moe(tmp_path):
    """llama-server 沒有 --n-cpu-moe(舊 build):互動答 y 直接用全 --cpu-moe 並提示;
    --n-cpu-moe 旗標則直接報錯。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    _write_fake_llama(tmp_path, help_flags="--fit --cpu-moe --reranking --mmproj")
    models = _moe_models_needing_cpu_moe(tmp_path)

    proc = _run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin="1\n0\ny\n1\n1\n1\n8192\n1\n65536\n8\n\n")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "不支援 --n-cpu-moe" in proc.stdout
    parameters = _read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert parameters["cpu_moe"] is True
    assert "n_cpu_moe" not in parameters

    flagged = _run(tmp_path, *YES_TWO_GPU, "--n-cpu-moe", "3", "--no-preview",
                   "--models-dir", str(models))
    assert flagged.returncode == 2
    assert "--n-cpu-moe 需要 llama-server 支援" in flagged.stderr


def test_model_path_flag_rescues_missing_category(tmp_path):
    """模型不在 models-dir 時,--rerank-model <路徑> 必須能救援「缺類別」硬停。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path, with_reranker=False)
    external = tmp_path / "elsewhere"
    external.mkdir()
    (external / "my-reranker.gguf").write_bytes(b"x" * 512)

    proc = _run(
        tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
        "--main-model", "1",
        "--rerank-model", str(external / "my-reranker.gguf"),
        "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1", "--vl-gpu", "1",
        *NUM_FLAGS,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    deployment = _read_deployment(tmp_path)
    assert deployment["services"]["reranker"]["model"] == str(external / "my-reranker.gguf")

    # 數字編號沒有候選可對應 → 仍要硬停,且訊息指向用路徑
    numbered = _run(
        tmp_path, "--yes", "--models-dir", str(models), "--rerank-model", "1",
    )
    assert numbered.returncode == 2
    assert "初步判定不通過" in numbered.stderr

    # 新契約:重跑不沿用 → 沒帶旗標的重跑同樣在 precheck 硬停
    rerun = _run(tmp_path, "--yes", "--models-dir", str(models))
    assert rerun.returncode == 2
    assert "初步判定不通過" in rerun.stderr


def test_generated_start_sh_subcommand_guard_and_logs_validation(tmp_path):
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    assert _run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0
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


def test_generated_start_sh_exports_before_subcommand_dispatch(tmp_path):
    """GPU/模型 exports 必須在 case dispatch 之前:status --strict 的 wrong-GPU
    檢查唯一來源是這些環境變數,放在 case 之後 status 路徑會拿不到期望值。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    assert _run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0
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

    dry = _run(tmp_path, *YES_TWO_GPU, "--dry-run", "--models-dir", str(models))
    assert dry.returncode == 0, dry.stderr + dry.stdout
    assert secret not in dry.stdout
    assert "***redacted***" in dry.stdout
    assert "憑證類欄位值已遮罩" in dry.stdout
    assert stat.S_IMODE(opencode_path.stat().st_mode) == 0o600  # dry-run 不動檔案

    real = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
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


def test_rerun_keeps_hand_edited_port_and_base_url(tmp_path):
    """port/base_url 屬使用者領域(本工具從不寫)→ 手改過的值重跑要原樣保留。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    assert _run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0
    deployment_path = tmp_path / "home" / ".config/codetrail/deployment.json"
    config = json.loads(deployment_path.read_text(encoding="utf-8"))
    config["services"]["main"]["port"] = 18080
    config["services"]["main"]["base_url"] = "http://127.0.0.1:18080"
    deployment_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    rerun = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
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


def test_opencode_config_env_var_is_honored(tmp_path):
    """OpenCode 與 config.py/aicode 都先讀 OPENCODE_CONFIG;set_config 寫死預設
    路徑會做出「顯示 PASS 但完全沒生效」的設定。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    custom = tmp_path / "custom" / "oc.json"
    proc = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
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
        ["bash", str(SCRIPT), "--skip-deps-check", *YES_TWO_GPU, "--no-preview",
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


def test_interactive_reranker_ctx_validates_range(tmp_path):
    """互動輸入與 CLI 用同一組上下限:reranker ctx=1 不得被接受寫入。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    # 標準作答,但 reranker ctx 先給 1(拒絕)再給 256。
    proc = _run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin="1\n0\n1\n1\n1\n1\n256\n1\n65536\n8\n\n")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "無效輸入:請輸入 128..1048576 的整數" in proc.stdout
    deployment = _read_deployment(tmp_path)
    assert deployment["services"]["reranker"]["ctx"] == 256

    cli = _run(tmp_path, "--yes", "--rerank-ctx", "1", "--models-dir", str(models))
    assert cli.returncode == 2
    assert "128" in cli.stderr


def test_interactive_main_ctx_rejects_above_maximum(tmp_path):
    """互動主模型 ctx 超過 schema 上限(1048576)也要重問,不得寫入後才被驗證打死。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    proc = _run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin="1\n0\n1\n1\n1\n8192\n1\n9999999\n65536\n8\n\n")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "無效輸入:請輸入 1024..1048576 的整數" in proc.stdout
    assert _read_deployment(tmp_path)["services"]["main"]["ctx"] == 65536

    cli = _run(tmp_path, "--yes", "--ctx", "9999999", "--models-dir", str(models))
    assert cli.returncode == 2
    assert "1048576" in cli.stderr


def test_small_main_ctx_clamps_batch_instead_of_failing_validation(tmp_path):
    """--ctx 1024(CLI 允許的最小值):batch 要夾到 ctx,
    不得產生 batch>ctx 再被自己的 schema 驗證打死。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    proc = _run(
        tmp_path,
        "--yes", "--no-preview", "--models-dir", str(models),
        "--main-model", "1", "--rerank-model", "1",
        "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1", "--vl-gpu", "1",
        "--ctx", "1024", "--threads", "8", "--rerank-ctx", "8192",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    deployment = _read_deployment(tmp_path)
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
    proc = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
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
    proc = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
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
    assert _run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0

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
    """混放目錄無法判斷 mmproj 歸屬 → VL 有多顆候選:--yes 必須用 --vl-model
    明確指定,不得自動抓一顆配對。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_flat_vl_models(tmp_path)
    proc = _run(
        tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
        "--main-model", "1",
        "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1", "--vl-gpu", "1",
        *NUM_FLAGS,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "--vl-model" in proc.stderr
    assert not (tmp_path / "home" / "start.sh").exists()  # 未寫入任何設定

    # 明確 --vl-model 之後可過:唯一 mmproj 與明確指定的模型配對
    explicit = _run(
        tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
        "--main-model", "1",
        "--vl-model", str(models / "flat" / "media-large-q4.gguf"),
        "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1", "--vl-gpu", "1",
        *NUM_FLAGS,
    )
    assert explicit.returncode == 0, explicit.stderr + explicit.stdout
    deployment = _read_deployment(tmp_path)
    assert deployment["services"]["vl"]["model"].endswith("media-large-q4.gguf")
    assert deployment["services"]["vl"]["mmproj"].endswith("mmproj-F16.gguf")


def test_flat_dir_vl_pairing_asks_explicitly_in_interactive(tmp_path):
    """互動模式遇到混放目錄:VL 是多候選 → 必答題,選定後唯一 mmproj 自動配對。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_flat_vl_models(tmp_path)
    # main(3 候選選 1)、main GPU、embed GPU、reranker 唯一自動、reranker GPU、
    # reranker ctx、VL 明確選 [2] media-large、VL GPU、ctx、threads、摘要確認。
    proc = _run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin="1\n0\n1\n1\n8192\n2\n1\n65536\n8\n\n")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "【VL 模型】 — 偵測到的候選" in proc.stdout
    deployment = _read_deployment(tmp_path)
    assert deployment["services"]["vl"]["model"].endswith("media-large-q4.gguf")
    assert deployment["services"]["vl"]["mmproj"].endswith("mmproj-F16.gguf")


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

    skipped = _run(tmp_path, *YES_TWO_GPU, "--no-preview", "--skip-binary-check",
                   "--models-dir", str(models))
    assert skipped.returncode == 0, skipped.stderr + skipped.stdout
    assert "跳過 llama-server 執行檢查" in skipped.stdout


def test_deployment_env_override_split_brain_warns(tmp_path):
    """AICODE_DEPLOYMENT_CONFIG 等 override 有設時要警告:aicode 會讀自訂檔、
    ~/start.sh 卻刻意 unset,兩邊將各用一份設定。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    proc = _run(
        tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
        env_overrides={"AICODE_DEPLOYMENT_CONFIG": str(tmp_path / "custom-deploy.json")},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "偵測到環境變數 AICODE_DEPLOYMENT_CONFIG" in proc.stdout
    assert "各用一份設定" in proc.stdout


def test_logs_accepts_count_and_follow_shorthand(tmp_path):
    """logs 依說明允許省略 role:logs 3 / logs -f 都要能用;多餘參數不得靜默忽略。"""
    _write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = _make_models(tmp_path)
    assert _run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0
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
