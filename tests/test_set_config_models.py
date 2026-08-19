"""set_config 的模型探索與 CPU-MoE 決策:shard 齊全性、mmproj 配對、VL、n_cpu_moe。

從 tests/test_set_config.py 拆出(2026-08-20),並併入原 test_set_config_cpu_moe.py 與
test_set_config_reranker_ctx.py。
"""

from __future__ import annotations

import json
import shutil
import struct
from pathlib import Path

import pytest

from deployment_profile import (
    ProfileError,
    build_server_command,
    cpu_moe_fit_conflict,
    load_effective_profile,
)
from scripts import set_config as sc
from tests._set_config_harness import (
    GIB,
    NUM_FLAGS,
    STDIN_STANDARD,
    TWO_GPUS,
    YES_TWO_GPU,
    make_models,
    read_deployment,
    run,
    sparse,
    sparse_dense_gguf,
    sparse_layered_moe_gguf,
    sparse_moe_gguf,
    write_fake_llama,
    write_fake_nvidia_smi,
)


def moe_models_needing_cpu_moe(tmp_path: Path) -> Path:
    """主模型 = 10 層 experts(各 2 GiB)+ dense 6 GiB 的 sparse MoE fixture。"""
    models = make_models(tmp_path)
    main_dir = models / "big-chat"
    (main_dir / "big-chat-ud-q4_k_xl-00001-of-00002.gguf").unlink()
    (main_dir / "big-chat-ud-q4_k_xl-00002-of-00002.gguf").unlink()
    sparse_layered_moe_gguf(
        main_dir / "big-moe-ud-q4_k_xl.gguf",
        layer_expert_bytes={index: 2 * GIB for index in range(10)},
        dense_bytes=6 * GIB,
    )
    return models


def make_flat_vl_models(root: Path) -> Path:
    """混放目錄 fixture:flat/ 內兩顆聊天模型 + 一顆 mmproj(歸屬不明)。"""
    models = root / "models"
    (models / "big-chat").mkdir(parents=True)
    sparse_dense_gguf(models / "big-chat" / "big-chat-q4.gguf", 4096)
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


def test_vl_hint_order_sorts_candidates_first(tmp_path):
    """hint 只影響候選清單排序(維護者驗證的排前面),不再自動選用。"""
    from scripts import set_config as sc

    models = make_models(tmp_path)
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

def test_incomplete_shards_are_reported_with_missing_names(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    (models / "big-chat" / "big-chat-ud-q4_k_xl-00002-of-00002.gguf").unlink()

    # 還有別的 main 候選(VL 模型也可當 main)→ 軟剔除:警告列出缺哪片,改用替代模型
    proc = run(
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
    proc2 = run(tmp_path, "--yes", "--models-dir", str(models))
    assert proc2.returncode == 2
    assert "初步判定不通過" in proc2.stderr
    assert "缺少 shard" in proc2.stderr
    assert "big-chat-ud-q4_k_xl-00002-of-00002.gguf" in proc2.stderr

def test_multiple_mmproj_requires_explicit_choice(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    (models / "vl" / "mmproj-other-F16.gguf").write_bytes(b"x" * 256)

    ambiguous = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert ambiguous.returncode == 2
    assert "--vl-mmproj" in ambiguous.stderr

    explicit = run(
        tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
        "--vl-mmproj", str(models / "vl" / "mmproj-F16.gguf"),
    )
    assert explicit.returncode == 0, explicit.stderr
    deployment = read_deployment(tmp_path)
    assert deployment["services"]["vl"]["mmproj"] == str(models / "vl" / "mmproj-F16.gguf")

def test_yes_moe_main_requires_explicit_mode_flag(tmp_path):
    """MoE 主模型的 CPU-MoE 層數沒有預設:--yes 必須用旗標指定,不再自動選。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    sparse_moe_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00001-of-00002.gguf",
        14 * GIB, 11 * GIB,
    )
    sparse_moe_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00002-of-00002.gguf",
        12 * GIB, 10 * GIB,
    )

    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 2
    assert "--n-cpu-moe" in proc.stderr
    assert "--cpu-moe / --no-cpu-moe" in proc.stderr

    forced = run(tmp_path, *YES_TWO_GPU, "--cpu-moe", "--no-preview",
                  "--models-dir", str(models))
    assert forced.returncode == 0, forced.stderr + forced.stdout
    deployment = read_deployment(tmp_path)
    assert deployment["services"]["main"]["parameters"]["cpu_moe"] is True
    assert deployment["services"]["main"]["parameters"]["fit"] == "off"
    for role in ("embedding", "reranker", "vl"):
        assert "cpu_moe" not in deployment["services"][role].get("parameters", {})

    # 0 = 不 offload:與 --no-cpu-moe 同義,不寫任何 CPU-MoE 鍵。
    zero = run(tmp_path, *YES_TWO_GPU, "--n-cpu-moe", "0", "--no-preview",
                "--models-dir", str(models))
    assert zero.returncode == 0, zero.stderr + zero.stdout
    main_params = read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert "cpu_moe" not in main_params
    assert "n_cpu_moe" not in main_params

def test_cpu_moe_flag_on_dense_main_is_rejected(tmp_path):
    """輸入合理性驗證:dense 主模型給 --cpu-moe / --n-cpu-moe 都要報錯。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    sparse_dense_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00001-of-00002.gguf", 4096
    )
    sparse_dense_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00002-of-00002.gguf", 4096
    )

    cpu_moe = run(tmp_path, *YES_TWO_GPU, "--cpu-moe", "--no-preview",
                   "--models-dir", str(models))
    assert cpu_moe.returncode == 2
    assert "只對 MoE 模型有意義" in cpu_moe.stderr

    n_cpu_moe = run(tmp_path, *YES_TWO_GPU, "--n-cpu-moe", "3", "--no-preview",
                     "--models-dir", str(models))
    assert n_cpu_moe.returncode == 2
    assert "只對 MoE 模型有意義" in n_cpu_moe.stderr

    # 0 = 關閉:dense 模型也接受(等同沒開 CPU-MoE),不該報錯
    zero = run(tmp_path, *YES_TWO_GPU, "--n-cpu-moe", "0", "--no-preview",
                "--models-dir", str(models))
    assert zero.returncode == 0, zero.stderr + zero.stdout

def test_vl_cpu_moe_warns_about_mmap_and_preserves_manual_no_mmap(tmp_path):
    """VL 開了 CPU-MoE 卻沒 no_mmap:llama-server 會警告首次推論從 SSD 逐頁載入,
    set_config 要講清楚;使用者手動加的 services.vl.no_mmap 重跑不得被丟掉。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    sparse_layered_moe_gguf(
        models / "vl" / "vl-model-q6.gguf",
        layer_expert_bytes={index: GIB for index in range(6)},
        dense_bytes=GIB,
    )

    first = run(tmp_path, *YES_TWO_GPU, "--vl-n-cpu-moe", "3", "--no-preview",
                 "--models-dir", str(models))
    assert first.returncode == 0, first.stderr + first.stdout
    assert "VL 模型 開了 CPU-MoE 但未設 no_mmap" in first.stdout
    assert "tensor overrides to CPU are used with mmap enabled" in first.stdout

    # 使用者照建議手動加上 → 重跑保留,警告消失
    path = tmp_path / "home" / ".config/codetrail/deployment.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["services"]["vl"]["parameters"]["no_mmap"] = True
    path.write_text(json.dumps(config), encoding="utf-8")

    rerun = run(tmp_path, *YES_TWO_GPU, "--vl-n-cpu-moe", "3", "--no-preview",
                 "--models-dir", str(models))
    assert rerun.returncode == 0, rerun.stderr + rerun.stdout
    vl_params = read_deployment(tmp_path)["services"]["vl"]["parameters"]
    assert vl_params["no_mmap"] is True          # 沒被當成「未涵蓋鍵」丟掉
    assert vl_params["n_cpu_moe"] == 3
    assert "已捨棄:no_mmap" not in rerun.stdout
    assert "VL 模型 開了 CPU-MoE 但未設 no_mmap" not in rerun.stdout

    # 沒開 CPU-MoE 就不該有這個警告
    off = run(tmp_path, *YES_TWO_GPU, "--vl-n-cpu-moe", "0", "--no-preview",
               "--models-dir", str(models))
    assert off.returncode == 0, off.stderr + off.stdout
    assert "VL 模型 開了 CPU-MoE 但未設 no_mmap" not in off.stdout

def test_vl_cpu_moe_is_asked_and_written_for_moe_vl(tmp_path):
    """VL 也有 CPU-MoE 題:MoE VL --yes 必須給旗標,寫入後與 --fit on 並存。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    sparse_layered_moe_gguf(
        models / "vl" / "vl-model-q6.gguf",
        layer_expert_bytes={index: GIB for index in range(6)},
        dense_bytes=GIB,
    )

    missing = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert missing.returncode == 2
    assert "--vl-n-cpu-moe" in missing.stderr

    partial = run(tmp_path, *YES_TWO_GPU, "--vl-n-cpu-moe", "3", "--no-preview",
                   "--models-dir", str(models))
    assert partial.returncode == 0, partial.stderr + partial.stdout
    vl_params = read_deployment(tmp_path)["services"]["vl"]["parameters"]
    assert vl_params["n_cpu_moe"] == 3
    # CPU-MoE 之下 llama.cpp 的 --fit 一定會 abort(tensor override 已被設定),
    # 所以不能再寫一組不會生效的 --fit on/--fit-target,改成明寫 -ngl 99 --fit off。
    assert vl_params["fit"] == "off"
    assert vl_params["gpu_layers"] == 99
    assert "fit_target" not in vl_params
    assert "cpu_moe" not in vl_params
    assert "--fit 會因為 tensor override" in partial.stdout   # 安全網消失要講明

    full = run(tmp_path, *YES_TWO_GPU, "--vl-cpu-moe", "--no-preview",
                "--models-dir", str(models))
    assert full.returncode == 0, full.stderr + full.stdout
    vl_params = read_deployment(tmp_path)["services"]["vl"]["parameters"]
    assert vl_params["cpu_moe"] is True
    assert "n_cpu_moe" not in vl_params

    off = run(tmp_path, *YES_TWO_GPU, "--vl-n-cpu-moe", "0", "--no-preview",
               "--models-dir", str(models))
    assert off.returncode == 0, off.stderr + off.stdout
    vl_params = read_deployment(tmp_path)["services"]["vl"]["parameters"]
    assert "cpu_moe" not in vl_params and "n_cpu_moe" not in vl_params
    # 沒有 CPU-MoE → fit 真的能用,維持原本的自動配置
    assert (vl_params["fit"], vl_params["gpu_layers"]) == ("on", "auto")
    assert vl_params["fit_target"] == 3072

def test_dense_vl_skips_cpu_moe_question_with_reason(tmp_path):
    """dense VL(本專案預設的 Qwen3.5-9B 就是)不問 CPU-MoE,但要說明為什麼。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    sparse_dense_gguf(models / "vl" / "vl-model-q6.gguf", 4096)
    sparse_dense_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00001-of-00002.gguf", 4096
    )
    sparse_dense_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00002-of-00002.gguf", 4096
    )

    proc = run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin=STDIN_STANDARD)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert proc.stdout.count("略過 CPU-MoE 提問") == 2   # main 與 VL 各一次
    assert "dense 模型" in proc.stdout
    vl_params = read_deployment(tmp_path)["services"]["vl"]["parameters"]
    assert "cpu_moe" not in vl_params and "n_cpu_moe" not in vl_params

def test_vl_model_is_not_auto_selected_as_main(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    # VL 模型比一般聊天模型大很多:排序仍把 vl_paired 放最後,[1] 是 big-chat
    sparse(models / "vl" / "vl-model-q6.gguf", 8 * GIB)

    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr
    deployment = read_deployment(tmp_path)
    assert deployment["services"]["main"]["model"] == "big-chat-ud-q4-k-xl"
    assert deployment["services"]["vl"]["model"].endswith("vl-model-q6.gguf")

def test_only_vl_main_candidate_proceeds_with_warning(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    shutil.rmtree(models / "big-chat")

    proc = run(
        tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
        "--rerank-model", "1",
        "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1", "--vl-gpu", "1",
        *NUM_FLAGS,
    )
    assert proc.returncode == 0, proc.stderr
    assert "同時當 main" in proc.stdout
    deployment = read_deployment(tmp_path)
    assert deployment["services"]["main"]["model"] == "vl-model-q6"

def test_n_cpu_moe_flag_sets_value_and_implies_cpu_moe_mode(tmp_path):
    """--n-cpu-moe N 非互動指定:蘊含 CPU-MoE 模式,值照寫;與 --no-cpu-moe 互斥。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = moe_models_needing_cpu_moe(tmp_path)

    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--n-cpu-moe", "3",
                "--models-dir", str(models))

    assert proc.returncode == 0, proc.stderr + proc.stdout
    parameters = read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert parameters["n_cpu_moe"] == 3
    assert "cpu_moe" not in parameters
    start_sh = (tmp_path / "home" / "start.sh").read_text(encoding="utf-8")
    assert "--n-cpu-moe 3" in start_sh

    conflict = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--n-cpu-moe", "3",
                    "--no-cpu-moe", "--models-dir", str(models))
    assert conflict.returncode == 2
    assert "not allowed" in conflict.stderr or "互斥" in conflict.stderr

def test_n_cpu_moe_flag_over_max_index_means_full_cpu_moe(tmp_path):
    """--n-cpu-moe 超過最大 blk 編號 = 全部 experts 留 RAM(寫成 cpu_moe 布林鍵)。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = moe_models_needing_cpu_moe(tmp_path)

    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--n-cpu-moe", "42",
                "--models-dir", str(models))

    assert proc.returncode == 0, proc.stderr + proc.stdout
    parameters = read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert parameters["cpu_moe"] is True
    assert "n_cpu_moe" not in parameters

def test_interactive_prompt_accepts_typed_n_cpu_moe(tmp_path):
    """互動流程:CPU-MoE 只有「幾層」一題(沒有 y/n),由使用者輸入、只驗證範圍。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = moe_models_needing_cpu_moe(tmp_path)

    # main、main GPU(選 1 = 15000 MiB free)、ctx、CPU-MoE 層數先 abc(無效)再 3、
    # embed GPU、reranker、reranker GPU、reranker ctx、VL GPU、摘要確認。
    proc = run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin="1\n1\n65536\nabc\n3\n1\n1\n1\n8192\n1\n\n")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "主聊天模型 CPU-MoE 留在 RAM 的層數(0-1024)" in proc.stdout
    # 提示只留兩件事:(1) 方向 (2) 依 GGUF 權重 + nvidia-smi free VRAM 算的推薦區間。
    # 權重 26 GiB(10 層 × 2 GiB experts + 6 GiB dense)、GPU 1 free 15000 MiB
    # → 要移走 6 層才放得進,上界是全部移到 RAM 的 10。
    assert "數值越大 → GPU 負載越低(0 = 不 offload)。" in proc.stdout
    assert "推薦數值:6-10" in proc.stdout
    assert "GiB" not in proc.stdout               # 不再對使用者丟權重容量細節
    assert "[y/n]" not in proc.stdout             # 不再有模式分流題
    assert "無效輸入:請輸入 0-1024 的整數" in proc.stdout
    parameters = read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert parameters["n_cpu_moe"] == 3
    assert "cpu_moe" not in parameters

def test_interactive_n_cpu_moe_over_max_index_means_full_cpu_moe(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = moe_models_needing_cpu_moe(tmp_path)

    proc = run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin="1\n0\n65536\n42\n1\n1\n1\n8192\n1\n\n")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "推薦數值:" in proc.stdout
    parameters = read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert parameters["cpu_moe"] is True
    assert "n_cpu_moe" not in parameters

def test_interactive_cpu_moe_zero_means_no_offload(tmp_path):
    """CPU-MoE 預設就是開著問層數:不想 offload 的人輸入 0,不寫任何 CPU-MoE 鍵。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = moe_models_needing_cpu_moe(tmp_path)

    proc = run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin="1\n0\n65536\n0\n1\n1\n1\n8192\n1\n\n")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "0 = 不 offload" in proc.stdout
    parameters = read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert "cpu_moe" not in parameters
    assert "n_cpu_moe" not in parameters
    assert parameters["gpu_layers"] == 99

def test_explicit_cpu_moe_flag_means_full_ram_without_question(tmp_path):
    """--cpu-moe 明確代表全部 experts 放 RAM:不再詢問 n-cpu-moe 檔位。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = moe_models_needing_cpu_moe(tmp_path)

    proc = run(tmp_path, *YES_TWO_GPU, "--cpu-moe", "--no-preview",
                "--models-dir", str(models))

    assert proc.returncode == 0, proc.stderr + proc.stdout
    parameters = read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert parameters["cpu_moe"] is True
    assert "n_cpu_moe" not in parameters

def test_build_without_n_cpu_moe_support_degrades_to_full_cpu_moe(tmp_path):
    """llama-server 沒有 --n-cpu-moe(舊 build):互動輸入的層數改用全 --cpu-moe
    並提示;--n-cpu-moe 旗標則直接報錯。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    write_fake_llama(tmp_path, help_flags="--fit --cpu-moe --reranking --mmproj --cache-ram")
    models = moe_models_needing_cpu_moe(tmp_path)

    proc = run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin="1\n0\n65536\n3\n1\n1\n1\n8192\n1\n\n")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "不支援 --n-cpu-moe" in proc.stdout
    parameters = read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert parameters["cpu_moe"] is True
    assert "n_cpu_moe" not in parameters

    flagged = run(tmp_path, *YES_TWO_GPU, "--n-cpu-moe", "3", "--no-preview",
                   "--models-dir", str(models))
    assert flagged.returncode == 2
    assert "--n-cpu-moe 需要 llama-server 支援" in flagged.stderr

def test_model_path_flag_rescues_missing_category(tmp_path):
    """模型不在 models-dir 時,--rerank-model <路徑> 必須能救援「缺類別」硬停。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path, with_reranker=False)
    external = tmp_path / "elsewhere"
    external.mkdir()
    (external / "my-reranker.gguf").write_bytes(b"x" * 512)

    proc = run(
        tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
        "--main-model", "1",
        "--rerank-model", str(external / "my-reranker.gguf"),
        "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1", "--vl-gpu", "1",
        *NUM_FLAGS,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    deployment = read_deployment(tmp_path)
    assert deployment["services"]["reranker"]["model"] == str(external / "my-reranker.gguf")

    # 數字編號沒有候選可對應 → 仍要硬停,且訊息指向用路徑
    numbered = run(
        tmp_path, "--yes", "--models-dir", str(models), "--rerank-model", "1",
    )
    assert numbered.returncode == 2
    assert "初步判定不通過" in numbered.stderr

    # 新契約:重跑不沿用 → 沒帶旗標的重跑同樣在 precheck 硬停
    rerun = run(tmp_path, "--yes", "--models-dir", str(models))
    assert rerun.returncode == 2
    assert "初步判定不通過" in rerun.stderr

def test_flat_dir_vl_pairing_fails_loud_on_yes(tmp_path):
    """混放目錄無法判斷 mmproj 歸屬 → VL 有多顆候選:--yes 必須用 --vl-model
    明確指定,不得自動抓一顆配對。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_flat_vl_models(tmp_path)
    proc = run(
        tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
        "--main-model", "1",
        "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1", "--vl-gpu", "1",
        *NUM_FLAGS,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "--vl-model" in proc.stderr
    assert not (tmp_path / "home" / "start.sh").exists()  # 未寫入任何設定

    # 明確 --vl-model 之後可過:唯一 mmproj 與明確指定的模型配對
    explicit = run(
        tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
        "--main-model", "1",
        "--vl-model", str(models / "flat" / "media-large-q4.gguf"),
        "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1", "--vl-gpu", "1",
        *NUM_FLAGS,
    )
    assert explicit.returncode == 0, explicit.stderr + explicit.stdout
    deployment = read_deployment(tmp_path)
    assert deployment["services"]["vl"]["model"].endswith("media-large-q4.gguf")
    assert deployment["services"]["vl"]["mmproj"].endswith("mmproj-F16.gguf")

def test_flat_dir_vl_pairing_asks_explicitly_in_interactive(tmp_path):
    """互動模式遇到混放目錄:VL 是多候選 → 必答題,選定後唯一 mmproj 自動配對。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_flat_vl_models(tmp_path)
    # main(3 候選選 1)、main GPU、ctx、embed GPU、reranker 唯一自動、reranker GPU、
    # reranker ctx、VL 明確選 [2] media-large、VL GPU、摘要確認。
    proc = run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin="1\n0\n65536\n1\n1\n8192\n2\n1\n\n")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "【VL 模型】 — 偵測到的候選" in proc.stdout
    deployment = read_deployment(tmp_path)
    assert deployment["services"]["vl"]["model"].endswith("media-large-q4.gguf")
    assert deployment["services"]["vl"]["mmproj"].endswith("mmproj-F16.gguf")


# ---------------------------------------------------------------------------
# 併自 tests/test_set_config_cpu_moe.py(2026-08-20):同一個 CPU-MoE 決策鏈,
# 這裡全部是 in-process 單元測試(GGUF tensor table 解析、問答契約、profile schema)。
# ---------------------------------------------------------------------------

def _write_moe_gguf(path: Path, *, expert_bytes: int, dense_bytes: int) -> None:
    """建立只含 tensor table 的 sparse GGUF;不配置 GiB 級實體磁碟內容。"""
    tensors = (
        ("blk.0.ffn_up_exps.weight", 0),
        ("blk.0.attn_q.weight", expert_bytes),
    )
    header = struct.pack("<4sIQQ", b"GGUF", 3, len(tensors), 0)
    table = bytearray()
    for name, offset in tensors:
        encoded = name.encode("utf-8")
        table.extend(struct.pack("<Q", len(encoded)))
        table.extend(encoded)
        table.extend(struct.pack("<I", 1))  # n_dims
        table.extend(struct.pack("<Q", 1))  # dimensions
        table.extend(struct.pack("<I", 0))  # ggml type; parser只需 offset
        table.extend(struct.pack("<Q", offset))
    data_start = (len(header) + len(table) + 31) // 32 * 32
    with path.open("wb") as handle:
        handle.write(header)
        handle.write(table)
        handle.write(b"\0" * (data_start - len(header) - len(table)))
        handle.truncate(data_start + expert_bytes + dense_bytes)


def _write_metadata_only_gguf(path: Path) -> None:
    path.write_bytes(struct.pack("<4sIQQ", b"GGUF", 3, 0, 0))


def _gguf_kv_string(key: str, value: str) -> bytes:
    k, v = key.encode(), value.encode()
    return (struct.pack("<Q", len(k)) + k + struct.pack("<I", 8)
            + struct.pack("<Q", len(v)) + v)


def _gguf_kv_u32(key: str, value: int) -> bytes:
    k = key.encode()
    return struct.pack("<Q", len(k)) + k + struct.pack("<I", 4) + struct.pack("<I", value)


def _write_gguf_with_metadata(
    path: Path, *, architecture: str, expert_count: int | None, tensor_names: tuple[str, ...]
) -> None:
    """帶 general.architecture / <arch>.expert_count 的 sparse GGUF。

    用來釘住「metadata 有讀到」以及 metadata 與 tensor 名稱不一致時的警告。
    """
    metadata = _gguf_kv_string("general.architecture", architecture)
    count = 1
    if expert_count is not None:
        metadata += _gguf_kv_u32(f"{architecture}.expert_count", expert_count)
        count += 1
    header = struct.pack("<4sIQQ", b"GGUF", 3, len(tensor_names), count)
    table = bytearray()
    for index, name in enumerate(tensor_names):
        encoded = name.encode()
        table.extend(struct.pack("<Q", len(encoded)))
        table.extend(encoded)
        table.extend(struct.pack("<I", 1))
        table.extend(struct.pack("<Q", 1))
        table.extend(struct.pack("<I", 0))
        table.extend(struct.pack("<Q", index * sc.GIB))
    metadata_end = len(header) + len(metadata) + len(table)
    data_start = (metadata_end + 31) // 32 * 32
    with path.open("wb") as handle:
        handle.write(header)
        handle.write(metadata)
        handle.write(table)
        handle.write(b"\0" * (data_start - metadata_end))
        handle.truncate(data_start + len(tensor_names) * sc.GIB)


def _write_layered_moe_gguf(
    path: Path, *, layer_expert_bytes: dict[int, int], dense_bytes: int
) -> None:
    """多層 expert tensor 的 sparse GGUF:n_cpu_moe 提問需要 per-layer 編號。"""
    names: list[tuple[str, int]] = []
    offset = 0
    for layer, size in sorted(layer_expert_bytes.items()):
        names.append((f"blk.{layer}.ffn_up_exps.weight", offset))
        offset += size
    if dense_bytes:
        names.append(("blk.0.attn_q.weight", offset))
        offset += dense_bytes
    header = struct.pack("<4sIQQ", b"GGUF", 3, len(names), 0)
    table = bytearray()
    for name, tensor_offset in names:
        encoded = name.encode("utf-8")
        table.extend(struct.pack("<Q", len(encoded)))
        table.extend(encoded)
        table.extend(struct.pack("<I", 1))
        table.extend(struct.pack("<Q", 1))
        table.extend(struct.pack("<I", 0))
        table.extend(struct.pack("<Q", tensor_offset))
    data_start = (len(header) + len(table) + 31) // 32 * 32
    with path.open("wb") as handle:
        handle.write(header)
        handle.write(table)
        handle.write(b"\0" * (data_start - len(header) - len(table)))
        handle.truncate(data_start + offset)


def _build_plan(
    tmp_path: Path,
    *,
    cpu_moe: bool,
    layer_expert_bytes: dict[int, int] | None = None,
    n_cpu_moe: int | None = None,
) -> sc.Plan:
    main_path = tmp_path / "main-moe.gguf"
    if layer_expert_bytes is None:
        _write_moe_gguf(main_path, expert_bytes=8 * sc.GIB, dense_bytes=1 * sc.GIB)
    else:
        _write_layered_moe_gguf(
            main_path, layer_expert_bytes=layer_expert_bytes, dense_bytes=1 * sc.GIB
        )
    candidate = sc.ModelCandidate(main_path, main_path.stat().st_size, 1)
    layout = sc.inspect_model_layout(candidate)

    embed = tmp_path / "embed.gguf"
    reranker = tmp_path / "reranker.gguf"
    vl = tmp_path / "vl.gguf"
    mmproj = tmp_path / "mmproj.gguf"
    for path in (embed, reranker, vl, mmproj):
        path.write_bytes(b"fixture")

    main_gpu = sc.Gpu(0, "Test GPU", 8192, 8192, "GPU-main")
    aux_gpu = sc.Gpu(1, "Aux GPU", 16384, 16384, "GPU-aux")
    return sc.Plan(
        gpus=[main_gpu, aux_gpu],
        main=sc.Selection("main", candidate, main_gpu),
        embedding=sc.Selection(
            "embedding", sc.ModelCandidate(embed, embed.stat().st_size, 1), aux_gpu
        ),
        reranker=sc.Selection(
            "reranker", sc.ModelCandidate(reranker, reranker.stat().st_size, 1), aux_gpu
        ),
        vl=sc.Selection(
            "vl", sc.ModelCandidate(vl, vl.stat().st_size, 1), aux_gpu, mmproj=mmproj
        ),
        main_key="main-moe",
        ctx=65536,
        threads=12,
        batch=0,
        ubatch=0,
        reranker_ctx=8192,
        cpu_moe=cpu_moe,
        n_cpu_moe=n_cpu_moe,
        main_layout=layout,
    )


def test_gguf_tensor_table_detects_expert_storage(tmp_path):
    plan = _build_plan(tmp_path, cpu_moe=True)

    assert plan.main_layout is not None
    assert plan.main_layout.is_moe
    assert plan.main_layout.expert_bytes == 8 * sc.GIB
    assert plan.main_layout.tensor_bytes == 9 * sc.GIB


def test_split_gguf_allows_metadata_only_shard(tmp_path):
    first = tmp_path / "split-moe-00001-of-00002.gguf"
    second = tmp_path / "split-moe-00002-of-00002.gguf"
    _write_metadata_only_gguf(first)
    _write_moe_gguf(second, expert_bytes=2 * sc.GIB, dense_bytes=1 * sc.GIB)
    candidate = sc.ModelCandidate(
        first, first.stat().st_size + second.stat().st_size, 2
    )

    layout = sc.inspect_model_layout(candidate)

    assert layout.is_moe
    assert layout.expert_bytes == 2 * sc.GIB


def test_gguf_parser_collects_per_layer_expert_bytes(tmp_path):
    path = tmp_path / "layered.gguf"
    _write_layered_moe_gguf(
        path,
        layer_expert_bytes={0: 1 * sc.GIB, 1: 2 * sc.GIB, 2: 3 * sc.GIB},
        dense_bytes=1 * sc.GIB,
    )
    candidate = sc.ModelCandidate(path, path.stat().st_size, 1)

    layout = sc.inspect_model_layout(candidate)

    assert layout.expert_bytes == 6 * sc.GIB
    assert layout.expert_layer_bytes == (
        (0, 1 * sc.GIB), (1, 2 * sc.GIB), (2, 3 * sc.GIB)
    )


def test_split_gguf_sums_same_layer_across_shards(tmp_path):
    first = tmp_path / "layered-00001-of-00002.gguf"
    second = tmp_path / "layered-00002-of-00002.gguf"
    _write_layered_moe_gguf(
        first, layer_expert_bytes={3: 1 * sc.GIB}, dense_bytes=1 * sc.GIB
    )
    _write_layered_moe_gguf(
        second, layer_expert_bytes={3: 2 * sc.GIB, 4: 1 * sc.GIB}, dense_bytes=0
    )
    candidate = sc.ModelCandidate(
        first, first.stat().st_size + second.stat().st_size, 2
    )

    layout = sc.inspect_model_layout(candidate)

    assert layout.expert_layer_bytes == ((3, 3 * sc.GIB), (4, 1 * sc.GIB))


def test_build_main_parameters_full_cpu_moe(tmp_path):
    plan = _build_plan(tmp_path, cpu_moe=True)

    parameters, batch, ubatch = sc.build_main_parameters(
        plan.main.candidate, plan.ctx, plan.threads, True, True, plan.notes,
    )

    assert parameters["cpu_moe"] is True
    assert parameters["gpu_layers"] == 99
    assert parameters["fit"] == "off"
    assert "n_cpu_moe" not in parameters
    assert (batch, ubatch) == (2048, 512)
    assert any("expert tensors 固定在 RAM" in note for note in plan.notes)


def test_build_main_parameters_partial_n_cpu_moe(tmp_path):
    plan = _build_plan(
        tmp_path, cpu_moe=True, n_cpu_moe=7,
        layer_expert_bytes={index: 1 * sc.GIB for index in range(8)},
    )

    parameters, batch, ubatch = sc.build_main_parameters(
        plan.main.candidate, plan.ctx, plan.threads, True, True, plan.notes,
        n_cpu_moe=plan.n_cpu_moe,
    )

    assert parameters["n_cpu_moe"] == 7
    assert "cpu_moe" not in parameters
    assert parameters["gpu_layers"] == 99
    assert parameters["fit"] == "off"
    assert (batch, ubatch) == (2048, 512)
    assert any("前 7 層" in note for note in plan.notes)


def test_build_main_parameters_normal_mode_points_to_nvidia_smi(tmp_path):
    """一般模式固定 -ngl 99:不做容量分支,note 指向 nvidia-smi 實測。"""
    plan = _build_plan(tmp_path, cpu_moe=False)

    parameters, batch, ubatch = sc.build_main_parameters(
        plan.main.candidate, plan.ctx, plan.threads, True, False, plan.notes,
    )

    assert parameters["gpu_layers"] == 99
    assert parameters["fit"] == "off"
    assert "cpu_moe" not in parameters
    assert "fit_target" not in parameters
    assert (batch, ubatch) == (2048, 512)
    assert any("nvidia-smi" in note for note in plan.notes)

    # 小 ctx 時 batch 夾到 ctx(schema 要求 ubatch ≤ batch ≤ ctx)
    small_notes: list[str] = []
    _, small_batch, small_ubatch = sc.build_main_parameters(
        plan.main.candidate, 1024, plan.threads, True, False, small_notes,
    )
    assert small_batch == 1024
    assert small_ubatch <= small_batch

    # build 不支援 --fit 時不寫 fit 鍵
    no_fit_notes: list[str] = []
    no_fit, _, _ = sc.build_main_parameters(
        plan.main.candidate, plan.ctx, plan.threads, False, False, no_fit_notes,
    )
    assert "fit" not in no_fit


_FULL_CAPS = {"fit": True, "cpu_moe": True, "n_cpu_moe": True}
_MAIN_FLAGS = ("--cpu-moe", "--no-cpu-moe", "--n-cpu-moe")


def _ask(layout, *, caps=None, notes=None, assume_yes=False,
         cpu_moe_override=None, n_cpu_moe_override=None, label="主聊天模型"):
    return sc.choose_cpu_moe_layers(
        label,
        layout,
        cpu_moe_override=cpu_moe_override,
        n_cpu_moe_override=n_cpu_moe_override,
        assume_yes=assume_yes,
        caps=_FULL_CAPS if caps is None else caps,
        flag_names=_MAIN_FLAGS,
        notes=[] if notes is None else notes,
    )


def test_cpu_moe_question_is_only_the_layer_count(tmp_path, monkeypatch):
    """沒有 y/n 分流:CPU-MoE 直接問層數,0 = 不 offload、≥ 上限 = 全部。"""
    layout = sc.ModelLayout(
        tensor_bytes=9 * sc.GIB,
        expert_bytes=8 * sc.GIB,
        expert_layer_bytes=tuple((index, 1 * sc.GIB) for index in range(8)),
    )
    assert sc.cpu_moe_layer_ceiling(layout) == 8

    prompts: list[str] = []
    answers = iter(["", "4", "8", "2000", "0"])

    def scripted(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(sc, "_input", scripted)
    assert _ask(layout) == (True, 4)        # "" 重問後輸入 4 → 前 4 層
    assert _ask(layout) == (True, None)     # 8 ≥ 上限 8 → 全部留 RAM
    assert _ask(layout) == (False, None)    # 2000 超出 0-1024 重問 → 0 = 不 offload
    assert all(f"(0-{sc.MAX_N_CPU_MOE})" in prompt for prompt in prompts)
    assert all("[y/n]" not in prompt for prompt in prompts)


def test_gguf_metadata_architecture_and_expert_count_are_read(tmp_path):
    """「這顆是 dense」要能被使用者驗證 → architecture / expert_count 必須真的讀進來。"""
    dense = tmp_path / "dense.gguf"
    _write_gguf_with_metadata(
        dense, architecture="qwen3vl", expert_count=None,
        tensor_names=("blk.0.ffn_up.weight", "blk.0.ffn_down.weight"),
    )
    layout = sc.inspect_model_layout(sc.ModelCandidate(dense, dense.stat().st_size, 1))
    assert layout.architecture == "qwen3vl"
    assert layout.expert_count == 0
    assert not layout.is_moe
    assert not layout.metadata_claims_moe

    moe = tmp_path / "moe.gguf"
    _write_gguf_with_metadata(
        moe, architecture="qwen35moe", expert_count=512,
        tensor_names=("blk.0.ffn_up_exps.weight", "blk.1.ffn_up_exps.weight"),
    )
    layout = sc.inspect_model_layout(sc.ModelCandidate(moe, moe.stat().st_size, 1))
    assert (layout.architecture, layout.expert_count) == ("qwen35moe", 512)
    assert layout.is_moe
    assert not layout.metadata_claims_moe


def test_metadata_moe_without_matching_tensors_warns_instead_of_claiming_dense(
    tmp_path, monkeypatch, capsys
):
    """metadata 說有 experts、tensor 名稱卻對不上 llama.cpp 的 offload 規則:
    不能含糊說成「dense 模型」——那會讓使用者以為工具漏判。"""
    odd = tmp_path / "odd-moe.gguf"
    _write_gguf_with_metadata(
        odd, architecture="futurearch", expert_count=64,
        tensor_names=("blk.0.ffn_experts_v2.weight", "blk.1.ffn_experts_v2.weight"),
    )
    layout = sc.inspect_model_layout(sc.ModelCandidate(odd, odd.stat().st_size, 1))
    assert layout.expert_count == 64
    assert not layout.is_moe            # llama.cpp 的 --cpu-moe 抓不到這些名字
    assert layout.metadata_claims_moe

    monkeypatch.setattr(sc, "_input", lambda prompt: pytest.fail("不應詢問"))
    notes: list[str] = []
    assert _ask(layout, notes=notes) == (False, None)
    assert "dense 模型" not in capsys.readouterr().out
    assert any("64 個 experts" in note and "不會有作用" in note for note in notes)


def test_cpu_moe_recommendation_spans_fit_to_full_offload():
    """推薦區間 = [權重剛好放得進 free VRAM 的最小層數, 全部移到 RAM]。

    只算 GGUF 權重 storage,所以三種 GPU 各自落在不同分支。
    """
    layout = sc.ModelLayout(                       # 10 層 × 2 GiB experts + 6 GiB dense
        tensor_bytes=26 * sc.GIB,
        expert_bytes=20 * sc.GIB,
        expert_layer_bytes=tuple((index, 2 * sc.GIB) for index in range(10)),
    )
    roomy = sc.Gpu(0, "roomy", 40960, 40960, "GPU-roomy")   # 40 GiB free → 0 就放得下
    tight = sc.Gpu(1, "tight", 16384, 15000, "GPU-tight")   # 14.6 GiB free → 要移走 6 層
    tiny = sc.Gpu(2, "tiny", 4096, 4096, "GPU-tiny")        # 4 GiB free → dense 都放不下

    assert sc.cpu_moe_recommendation(layout, tight) == "6-10"
    assert sc.cpu_moe_recommendation(layout, roomy).startswith("0(")
    assert sc.cpu_moe_recommendation(layout, tiny).startswith("10(")

    # expert tensors 沒有 blk 編號 / 沒有 GPU 資訊 → 只能推薦「全部移到 RAM」
    flat = sc.ModelLayout(tensor_bytes=26 * sc.GIB, expert_bytes=20 * sc.GIB)
    assert sc.cpu_moe_recommendation(flat, tight) == "1"
    assert sc.cpu_moe_recommendation(layout, None) == "10"

    # 推薦不是限制:區間外的輸入照樣接受(這題只驗證 0-MAX 範圍)
    assert sc.cpu_moe_gpu_bytes(layout, 6) == 14 * sc.GIB
    assert sc.cpu_moe_fit_layers(layout, 14 * sc.GIB) == 6


def test_vl_recommendation_subtracts_the_fit_target_it_reserves():
    """VL 一定帶 --fit-target,那塊 VRAM 是既定保留量,不扣掉會推薦放不下的值。

    這是實機踩到的:35.8 GiB 的 MoE VL 放進 free 15.57 GiB 的卡,
    未扣 fit_target 時下界算出 25(權重就要 15.18 GiB,已超過扣除後的 12.57 GiB)。
    """
    layout = sc.ModelLayout(                       # 10 層 × 2 GiB experts + 6 GiB dense
        tensor_bytes=26 * sc.GIB,
        expert_bytes=20 * sc.GIB,
        expert_layer_bytes=tuple((index, 2 * sc.GIB) for index in range(10)),
    )
    gpu = sc.Gpu(1, "aux", 16384, 15000, "GPU-aux")     # free 14.65 GiB

    assert sc.cpu_moe_recommendation(layout, gpu) == "6-10"                 # main:fit off
    assert sc.cpu_moe_recommendation(layout, gpu, sc.VL_FIT_TARGET_MIB) == "8-10"
    # 保留量大到連 dense 都放不下 → 退回「全部移到 RAM」並註明可能仍放不下
    assert sc.cpu_moe_recommendation(layout, gpu, 15000).startswith("10(")


def test_cpu_moe_question_is_skipped_for_dense_and_unparsable(tmp_path, monkeypatch, capsys):
    """dense / GGUF 解析不出來 → 不問;dense 印出原因,解析失敗留 note。"""
    monkeypatch.setattr(sc, "_input", lambda prompt: pytest.fail("不應詢問"))

    dense = sc.ModelLayout(tensor_bytes=1 * sc.GIB, expert_bytes=0)
    assert _ask(dense) == (False, None)
    assert "略過 CPU-MoE 提問" in capsys.readouterr().out

    none_notes: list[str] = []
    assert _ask(None, notes=none_notes) == (False, None)
    assert any("無法讀取" in note for note in none_notes)


def test_cpu_moe_question_is_skipped_when_build_lacks_the_flags(tmp_path, monkeypatch):
    layout = sc.ModelLayout(
        tensor_bytes=9 * sc.GIB, expert_bytes=8 * sc.GIB,
        expert_layer_bytes=((0, 8 * sc.GIB),),
    )
    monkeypatch.setattr(sc, "_input", lambda prompt: pytest.fail("不應詢問"))
    notes: list[str] = []
    caps = {"fit": True, "cpu_moe": False, "n_cpu_moe": False}
    assert _ask(layout, caps=caps, notes=notes) == (False, None)
    assert any("不支援 --cpu-moe" in note for note in notes)


def test_cpu_moe_flag_overrides_skip_the_question(tmp_path, monkeypatch):
    layout = sc.ModelLayout(
        tensor_bytes=9 * sc.GIB,
        expert_bytes=8 * sc.GIB,
        expert_layer_bytes=tuple((index, 1 * sc.GIB) for index in range(8)),
    )
    monkeypatch.setattr(sc, "_input", lambda prompt: pytest.fail("不應詢問"))

    assert _ask(layout, cpu_moe_override=True) == (True, None)
    assert _ask(layout, cpu_moe_override=False) == (False, None)
    assert _ask(layout, n_cpu_moe_override=3) == (True, 3)
    assert _ask(layout, n_cpu_moe_override=0) == (False, None)
    assert _ask(layout, n_cpu_moe_override=99) == (True, None)   # ≥ 上限 = 全部

    # dense 模型給非 0 的旗標 → 報錯(0 仍然合法,代表「不開」)
    dense = sc.ModelLayout(tensor_bytes=1 * sc.GIB, expert_bytes=0)
    with pytest.raises(sc.SetupError, match="只對 MoE 模型有意義"):
        _ask(dense, cpu_moe_override=True)
    with pytest.raises(sc.SetupError, match="只對 MoE 模型有意義"):
        _ask(dense, n_cpu_moe_override=3)
    assert _ask(dense, n_cpu_moe_override=0) == (False, None)

    # GGUF 解析不出來:--cpu-moe 是逃生門(允許),--n-cpu-moe 不猜層數上限
    assert _ask(None, cpu_moe_override=True) == (True, None)
    with pytest.raises(sc.SetupError, match="只對 MoE 模型有意義"):
        _ask(None, n_cpu_moe_override=3)


def test_yes_mode_requires_a_cpu_moe_flag_for_moe_models(tmp_path):
    layout = sc.ModelLayout(
        tensor_bytes=9 * sc.GIB, expert_bytes=8 * sc.GIB,
        expert_layer_bytes=((0, 8 * sc.GIB),),
    )
    with pytest.raises(sc.SetupError, match="--n-cpu-moe"):
        _ask(layout, assume_yes=True)
    # 非 MoE 不需要旗標(--yes 也不該卡住)
    dense = sc.ModelLayout(tensor_bytes=1 * sc.GIB, expert_bytes=0)
    assert _ask(dense, assume_yes=True) == (False, None)


def test_partial_offload_degrades_when_build_lacks_n_cpu_moe(tmp_path, monkeypatch):
    """舊 build 只有 --cpu-moe:互動輸入的部分層數退成全 CPU-MoE 並留 note。"""
    layout = sc.ModelLayout(
        tensor_bytes=9 * sc.GIB,
        expert_bytes=8 * sc.GIB,
        expert_layer_bytes=tuple((index, 1 * sc.GIB) for index in range(8)),
    )
    monkeypatch.setattr(sc, "_input", lambda prompt: "3")
    notes: list[str] = []
    caps = {"fit": True, "cpu_moe": True, "n_cpu_moe": False}
    assert _ask(layout, caps=caps, notes=notes) == (True, None)
    assert any("不支援 --n-cpu-moe" in note for note in notes)

    # 反過來:只有 --n-cpu-moe 的 build answering「全部」→ 用 --n-cpu-moe <上限>
    monkeypatch.setattr(sc, "_input", lambda prompt: "99")
    only_partial: list[str] = []
    caps = {"fit": True, "cpu_moe": False, "n_cpu_moe": True}
    assert _ask(layout, caps=caps, notes=only_partial) == (True, 8)
    assert any("不支援 --cpu-moe" in note for note in only_partial)


def test_offload_description_shows_manual_n_cpu_moe(tmp_path):
    plan = _build_plan(
        tmp_path, cpu_moe=True, n_cpu_moe=7,
        layer_expert_bytes={index: 1 * sc.GIB for index in range(8)},
    )
    plan.parameters = {"gpu_layers": 99, "n_cpu_moe": 7, "fit": "off"}

    assert "--n-cpu-moe 7" in sc._offload_description(plan)
    assert "--cpu-moe" not in sc._offload_description(plan)


def test_flat_dir_with_mmproj_does_not_mark_every_main_as_vl(tmp_path):
    """所有 GGUF 平鋪同一目錄時,mmproj 歸屬不明:不得把每顆主模型都標成 VL
    (否則會出現「沒有非 VL 的主聊天模型」這種誤導警告)。"""
    models = tmp_path / "models"
    models.mkdir()
    (models / "big-chat-q4.gguf").write_bytes(b"x" * 4096)
    (models / "qwen3-vl-8b-q4.gguf").write_bytes(b"x" * 1024)
    (models / "mmproj-F16.gguf").write_bytes(b"x" * 256)
    (models / "bge-m3-f16.gguf").write_bytes(b"x" * 512)
    (models / "bge-reranker-v2-m3-Q8_0.gguf").write_bytes(b"x" * 512)

    notes: list[str] = []
    candidates, broken = sc.scan_models(models, notes=notes)

    assert not broken
    # 兩顆 main 類模型都不能被標 vl_paired(歸屬不明)
    assert [cand.vl_paired for cand in candidates["main"]] == [False, False]
    # VL 候選仍在(使用者可明確選),qwen3-vl hint 排最前
    assert candidates["vl"][0].path.name == "qwen3-vl-8b-q4.gguf"
    assert any("混放" in note for note in notes)

    # 對照組:一目錄一模型(README 慣例)→ 照舊自動配對 + vl_paired
    tidy = tmp_path / "tidy"
    (tidy / "vl").mkdir(parents=True)
    (tidy / "vl" / "vl-model-q6.gguf").write_bytes(b"x" * 1024)
    (tidy / "vl" / "mmproj-F16.gguf").write_bytes(b"x" * 256)
    (tidy / "chat").mkdir()
    (tidy / "chat" / "chat-q4.gguf").write_bytes(b"x" * 4096)
    tidy_candidates, _ = sc.scan_models(tidy)
    vl_mains = [cand for cand in tidy_candidates["main"] if cand.vl_paired]
    assert len(vl_mains) == 1
    assert vl_mains[0].path.name == "vl-model-q6.gguf"
    assert tidy_candidates["vl"][0].mmproj is not None


def test_scan_orders_main_candidates_by_size_without_model_specific_hint(tmp_path):
    """主模型沒有維護者偏好;候選維持通用的容量降冪排序。"""
    models = tmp_path / "models"
    smaller_dir = models / "smaller-chat"
    larger_dir = models / "larger-chat"
    smaller_dir.mkdir(parents=True)
    larger_dir.mkdir(parents=True)
    smaller = smaller_dir / "smaller-chat-q4.gguf"
    larger = larger_dir / "larger-chat-q4.gguf"
    smaller.write_bytes(b"small")
    larger.write_bytes(b"larger candidate")

    candidates, broken = sc.scan_models(models)

    assert not broken
    assert candidates["main"][0].path == larger
    assert candidates["main"][1].path == smaller


def test_profile_emits_cpu_moe_for_main_and_vl_and_rejects_partial_mix(tmp_path):
    plan = _build_plan(tmp_path, cpu_moe=True)
    plan.batch = 1024
    plan.ubatch = 256
    plan.parameters = {"gpu_layers": 99, "cpu_moe": True, "fit": "off"}
    local_path = tmp_path / ".config" / "codetrail" / "deployment.json"
    local_path.parent.mkdir(parents=True)
    local_path.write_text(json.dumps(sc.build_deployment_config(plan)), encoding="utf-8")
    env = {
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "AICODE_MODEL": str(plan.main.candidate.path),
    }

    profile = load_effective_profile(env)
    command = build_server_command(profile.service("main"), "/opt/llama-server", env)

    assert "--cpu-moe" in command
    assert command[command.index("--fit") + 1] == "off"
    for role in ("embedding", "reranker", "vl"):
        assert "cpu_moe" not in profile.service(role).parameters
        assert profile.service(role).parameters["parallel"] == 1
    vl_command = build_server_command(
        profile.service("vl"), "/opt/llama-server", env
    )
    assert vl_command[vl_command.index("-ngl") + 1] == "auto"
    assert vl_command[vl_command.index("--fit") + 1] == "on"
    assert vl_command[vl_command.index("--fit-target") + 1] == "3072"

    # VL 也可以有 CPU-MoE,但 llama.cpp 的 --fit 會因為 tensor override 而 abort,
    # 所以此時必須輸出 --fit off / -ngl 99(不是 --fit on),且不寫 --fit-target;
    # embedding/reranker 則仍被 schema 拒絕。
    plan.vl_n_cpu_moe = 4
    local_path.write_text(json.dumps(sc.build_deployment_config(plan)), encoding="utf-8")
    vl_profile = load_effective_profile(env)
    vl_command = build_server_command(vl_profile.service("vl"), "/opt/llama-server", env)
    assert vl_command[vl_command.index("--n-cpu-moe") + 1] == "4"
    assert vl_command[vl_command.index("--fit") + 1] == "off"
    assert vl_command[vl_command.index("-ngl") + 1] == "99"
    assert "--fit-target" not in vl_command
    assert vl_command.count("--fit") == 1

    # 既有設定檔(CPU-MoE + fit on + gpu_layers auto)不必重跑 set_config:
    # build_server_command 直接輸出 --fit off 並丟掉不會生效的 --fit-target。
    legacy = sc.build_deployment_config(plan)
    legacy["services"]["vl"]["parameters"].update(
        {"n_cpu_moe": 4, "fit": "on", "fit_target": 3072, "gpu_layers": "auto"}
    )
    local_path.write_text(json.dumps(legacy), encoding="utf-8")
    legacy_service = load_effective_profile(env).service("vl")
    legacy_command = build_server_command(legacy_service, "/opt/llama-server", env)
    assert legacy_command[legacy_command.index("--fit") + 1] == "off"
    assert "--fit-target" not in legacy_command
    assert legacy_command.count("--fit") == 1
    assert cpu_moe_fit_conflict(legacy_service) is not None      # 但仍要提醒使用者
    assert cpu_moe_fit_conflict(vl_profile.service("vl")) is None
    plan.vl_n_cpu_moe = None

    for role in ("embedding", "reranker"):
        rejected = sc.build_deployment_config(plan)
        rejected["services"][role]["parameters"]["cpu_moe"] = True
        local_path.write_text(json.dumps(rejected), encoding="utf-8")
        with pytest.raises(ProfileError, match="not allowed for role"):
            load_effective_profile(env)

    for role in ("main", "vl"):
        bad = sc.build_deployment_config(plan)
        bad["services"][role]["parameters"]["cpu_moe"] = True
        bad["services"][role]["parameters"]["n_cpu_moe"] = 90
        local_path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ProfileError, match="mutually exclusive"):
            load_effective_profile(env)
