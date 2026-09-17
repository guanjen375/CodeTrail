"""`scripts/set_config.py`:問答流程與旗標契約、模型探索與 CPU-MoE 決策、產出物、壓縮模式。

2026-08-20 原 tests/test_set_config.py(69 條 10.43s,全套件第二慢)拆成 flow / artifacts /
models 三塊並把呼叫層換成 in-process(見 tests/_set_config_harness.py);2026-09-02 連同
壓縮模式題一起合回同一個主題檔。assertion 內容未變。四段各自的脈絡:

- 原 test_set_config_flow.py:問答流程與旗標契約 —— 使用者題沒有預設值、範圍驗證、
  preflight 失敗訊息;末段併自原 test_set_config_reranker_ctx.py(reranker internal
  buffer 是一般必答題,屬於問答流程契約)。
- 原 test_set_config_models.py:模型探索與 CPU-MoE 決策 —— shard 齊全性、mmproj 配對、
  VL、n_cpu_moe;末段併自原 test_set_config_cpu_moe.py(GGUF tensor table 解析、問答契約、
  profile schema 的 in-process 單元測試)。
- 原 test_set_config_artifacts.py:產出物、備份與 restore。
- 原 test_set_config_compaction.py:壓縮模式題 —— 接管、還原、與不接管。這一段每條都是
  smoke(原檔是 module 層 pytestmark),會靜默失敗的東西才寫在那裡:

    * `--yes` 沒給 `--compaction-mode`、機器也還沒選過 → **不得** 動壓縮設定。
      弄反的話,舊安裝重跑一次 `--yes` 腳本就會突然開始自動壓縮,而使用者
      沒有要求過任何這種行為。
    * 寫進 client.json 的門檻必須等於同一條公式對這個
      ctx 的推導值。wizard 與 runtime 各算各的,兩邊差一點也不會有錯誤訊息 ——
      只是門檻與保留額對不上。
    * 選 `off` 也要記下來(沒有 client.json = 沒有接管,客戶端退成 manual)。
    * `--dry-run` 與摘要頁按 q 都不得留下狀態檔。
    * restore manifest 兩個世代共用同一個檔:含別人的目標時整份拒絕,不部分還原。

flow 段的 dry-run 測試與壓縮段的 `test_dry_run_writes_nothing`(受保護 node)同名,
改名為 `test_dry_run_writes_nothing_for_the_model_flow`。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import struct
import subprocess
import sys
from pathlib import Path

import pytest

import client_compaction
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
    REPO_ROOT,
    SCRIPT,
    STDIN_STANDARD,
    TWO_GPUS,
    YES_ONE_GPU,
    YES_TWO_GPU,
    build_env,
    llama_bin_args,
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

# ── 原 test_set_config_flow.py:問答流程與旗標契約 ──

def test_help_is_offline_and_exits_zero(tmp_path):
    proc = run(tmp_path, "--help")
    assert proc.returncode == 0, proc.stderr
    assert "--models-dir" in proc.stdout
    assert "--cpu-moe" in proc.stdout
    assert "--n-cpu-moe" in proc.stdout
    assert "--vl-cpu-moe" in proc.stdout
    assert "--vl-n-cpu-moe" in proc.stdout
    assert "--allow-remote" in proc.stdout
    assert "--rerank-ctx" in proc.stdout
    assert "--n-ctx" in proc.stdout
    # 範圍顯示統一用 "-" 連接上下限,不再用 ".."
    assert "1024-1048576" in proc.stdout
    assert "1024..1048576" not in proc.stdout
    # 容量/建議機制已移除:相關旗標不得再出現
    assert "--advanced" not in proc.stdout
    assert "--ignore-capacity" not in proc.stdout
    assert "--fit-target" not in proc.stdout

def test_removed_flags_are_rejected(tmp_path):
    for flag in (("--ignore-capacity",), ("--advanced",), ("--fit-target", "5120")):
        proc = run(tmp_path, *flag)
        assert proc.returncode == 2, flag
        assert "unrecognized arguments" in proc.stderr

def test_yes_missing_value_errors_name_the_flag(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)

    # main 有 2 個候選 → 一開始就要 --main-model
    no_main = run(tmp_path, "--yes", "--models-dir", str(models))
    assert no_main.returncode == 2
    assert "--main-model" in no_main.stderr

    # 兩顆 GPU → 每個 role 都要 GPU 旗標(缺 --vl-gpu 驗證)
    no_vl_gpu = run(
        tmp_path, "--yes", "--main-model", "1", "--rerank-model", "1",
        "--main-gpu", "1", "--embed-gpu", "2", "--rerank-gpu", "2",
        *NUM_FLAGS, "--models-dir", str(models),
    )
    assert no_vl_gpu.returncode == 2
    assert "--vl-gpu" in no_vl_gpu.stderr

    # 數值也沒有預設:缺 --ctx 就報錯
    no_ctx = run(
        tmp_path, "--yes", "--main-model", "1", "--rerank-model", "1",
        "--main-gpu", "1", "--embed-gpu", "2", "--rerank-gpu", "2", "--vl-gpu", "2",
        "--rerank-ctx", "8192", "--models-dir", str(models),
    )
    assert no_ctx.returncode == 2
    assert "--ctx" in no_ctx.stderr

    # reranker internal buffer 現在也是使用者題 → 缺 --rerank-ctx 一樣報錯
    no_rerank_ctx = run(
        tmp_path, "--yes", "--main-model", "1", "--rerank-model", "1",
        "--main-gpu", "1", "--embed-gpu", "2", "--rerank-gpu", "2", "--vl-gpu", "2",
        "--ctx", "65536", "--models-dir", str(models),
    )
    assert no_rerank_ctx.returncode == 2
    assert "--rerank-ctx" in no_rerank_ctx.stderr

def test_interactive_flow_answers_everything_and_validates_ranges(tmp_path):
    """使用者選擇題沒有預設值(Enter 不可過關)、選項外輸入會重問。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)

    # main 先按 Enter(無效)再輸入 3(超出 1-2,無效)才輸入 1;
    # main GPU 先輸入 5(不存在)再輸入 1;其餘照標準作答。
    stdin = "\n3\n1\n5\n1\n65536\n2\n1\n2\n8192\n2\n1\n\n"
    proc = run(tmp_path, "--no-preview", "--models-dir", str(models), stdin=stdin)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "【主聊天模型】 — 偵測到的候選" in proc.stdout
    assert "編號只有 1-2" in proc.stdout           # 選項 1/2 輸入 3 → 重問
    assert "無效的 GPU 編號" in proc.stdout        # GPU 1/2 輸入 5 → 重問
    assert "只有一個候選,自動選用" in proc.stdout  # embedding / VL 唯一候選
    assert "設定摘要" in proc.stdout
    assert "(預設)" not in proc.stdout             # 不再有任何預設標記
    assert "建議配置" not in proc.stdout           # 不再有建議配置頁
    # 一個角色問完才換下一個(五段標題依序出現;第 5 段是壓縮模式)
    for step, title in ((1, "主聊天模型"), (2, "embedding 模型"),
                        (3, "reranker 模型"), (4, "VL 模型"), (5, "壓縮模式")):
        assert f"=== [{step}/5] {title} ===" in proc.stdout
    assert (
        proc.stdout.index("=== [1/5]") < proc.stdout.index("=== [2/5]")
        < proc.stdout.index("=== [3/5]") < proc.stdout.index("=== [4/5]")
        < proc.stdout.index("=== [5/5]")
    )
    assert (tmp_path / "home" / "start.sh").exists()
    deployment = read_deployment(tmp_path)
    assert deployment["services"]["main"]["ctx"] == 65536
    assert "threads" not in deployment["services"]["main"]["parameters"]
    assert deployment["services"]["reranker"]["ctx"] == 8192

def test_summary_confirm_enter_writes_and_q_aborts(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)

    accepted = run(
        tmp_path, "--no-preview", "--models-dir", str(models), stdin=STDIN_STANDARD
    )
    assert accepted.returncode == 0, accepted.stderr + accepted.stdout
    assert "設定摘要" in accepted.stdout
    assert (tmp_path / "home" / "start.sh").exists()

    home2 = tmp_path / "home2"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--skip-deps-check", *llama_bin_args(tmp_path),
         "--no-preview", "--models-dir", str(models)],
        cwd=REPO_ROOT,
        env={**build_env(tmp_path), "HOME": str(home2), "USERPROFILE": str(home2)},
        # 全部答完,摘要頁按 q → 不寫入
        input="1\n1\n65536\n2\n1\n2\n8192\n2\n1\nq\n",
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
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    proc = run(
        tmp_path, "--no-preview", "--models-dir", str(models),
        stdin="1\n1\n65536\n2\n1\n2\n8192\n2\n1\nzz\nq\n",
    )
    assert proc.returncode == 0, proc.stderr
    assert "無效輸入 'zz'" in proc.stdout
    assert "未寫入任何檔案" in proc.stdout
    assert not (tmp_path / "home" / "start.sh").exists()

def test_flags_override_model_and_gpu(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    proc = run(
        tmp_path,
        "--yes", "--no-preview", "--models-dir", str(models),
        "--main-model", "1", "--rerank-model", "1",
        "--main-gpu", "2", "--embed-gpu", "1", "--rerank-gpu", "2", "--vl-gpu", "2",
        *NUM_FLAGS,
    )
    assert proc.returncode == 0, proc.stderr
    services = read_deployment(tmp_path)["services"]
    assert services["main"]["gpu"] == "GPU-bbbb-2000"
    # aux 三顆各自記自己的卡(embed=GPU 1、rerank/vl=GPU 2)
    assert services["embedding"]["gpu"] == "GPU-aaaa-5090"
    assert services["reranker"]["gpu"] == "GPU-bbbb-2000"
    assert services["vl"]["gpu"] == "GPU-bbbb-2000"

def test_no_gpu_notifies_and_fails(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", "", exit_code=1)
    models = make_models(tmp_path)
    proc = run(tmp_path, "--yes", "--models-dir", str(models))
    assert proc.returncode == 2
    assert "偵測失敗" in proc.stderr

def test_missing_model_category_fails_precheck(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path, with_reranker=False)
    proc = run(tmp_path, "--yes", "--models-dir", str(models))
    assert proc.returncode == 2
    assert "reranker" in proc.stderr
    assert "初步判定不通過" in proc.stderr

def test_missing_llama_binary_fails_with_build_hint(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    proc = run(tmp_path, "--yes", "--models-dir", str(models), with_llama=False)
    assert proc.returncode == 2
    assert "llama-server" in proc.stderr
    assert "README §1.5" in proc.stderr
    assert "--llama-bin" in proc.stderr

@pytest.mark.parametrize(
    ("help_flags", "extra_args", "expected_error", "expected_hint"),
    [
        pytest.param(
            "--fit --mmproj --cache-ram",                  # 沒有 --reranking
            (),
            "--reranking",
            "更新並重新 build",
            id="reranking",
        ),
        pytest.param(
            # big-chat 假檔不是 GGUF → layout 無法解析;--cpu-moe 仍尊重旗標,
            # 但 build 不支援就要硬停。
            "--fit --reranking --mmproj --cache-ram",      # 沒有 --cpu-moe
            ("--cpu-moe", "--no-preview", "--main-model", "1", "--main-gpu", "1",
             "--ctx", "65536"),
            "需要 llama-server 的 --cpu-moe",
            "重新 build",
            id="cpu-moe",
        ),
        pytest.param(
            "--cpu-moe --reranking --mmproj --cache-ram",  # 沒有 --fit
            ("--no-cpu-moe", "--no-preview"),
            "安全的 VL placement 需要 llama-server --fit",
            "重新 build",
            id="fit",
        ),
    ],
)
def test_llama_build_missing_a_required_flag_fails_with_rebuild_hint(
    tmp_path, help_flags, extra_args, expected_error, expected_hint
):
    """llama-server build 缺 --reranking / --cpu-moe / --fit 任一個都要硬停,並指向重新 build。

    原本是三條獨立測試(test_llama_without_reranking_support_fails /
    test_cpu_moe_mode_requires_llama_cpu_moe_flag /
    test_generated_vl_safety_requires_llama_fit_flag),只差 --help 輸出、額外旗標與
    期望訊息;每個案例的斷言與原本逐字相同(argparse 不看旗標順序)。
    """
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    write_fake_llama(tmp_path, help_flags=help_flags)

    proc = run(tmp_path, "--yes", *extra_args, "--models-dir", str(models))

    assert proc.returncode == 2
    assert expected_error in proc.stderr
    assert expected_hint in proc.stderr

def test_llama_without_cache_ram_support_fails_before_questions(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    write_fake_llama(
        tmp_path,
        help_flags="--fit --cpu-moe --n-cpu-moe --reranking --mmproj",
    )

    proc = run(tmp_path, "--yes", "--models-dir", str(models))

    assert proc.returncode == 2
    assert "--cache-ram" in proc.stderr
    assert "更新並重新 build" in proc.stderr
    assert "[3/5]" not in proc.stdout

def test_no_capacity_estimation_oversized_configs_pass_through(tmp_path):
    """set_config 完全不做容量估算:遠超 VRAM/RAM 的組合照樣產生設定
    (塞不塞得下由使用者以啟動後 nvidia-smi 實測;start.sh 結尾提醒)。"""
    write_fake_nvidia_smi(tmp_path / "bin", "0, Tiny GPU, 4096, 3500, GPU-tiny")
    models = make_models(tmp_path)
    # 25 GiB dense 主模型 + 8 GiB VL,全部指到 4 GiB 的 GPU。
    sparse_dense_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00001-of-00002.gguf", 13 * GIB
    )
    sparse_dense_gguf(
        models / "big-chat" / "big-chat-ud-q4_k_xl-00002-of-00002.gguf", 12 * GIB
    )
    sparse(models / "vl" / "vl-model-q6.gguf", 8 * GIB)

    proc = run(tmp_path, *YES_ONE_GPU, "--no-preview", "--models-dir", str(models))

    assert proc.returncode == 0, proc.stderr + proc.stdout
    combined = proc.stdout + proc.stderr
    assert "容量判定" not in combined
    assert "容量預估" not in combined
    # 估算只當參考,不當判定:數字仍以啟動後 nvidia-smi 實測為準
    assert "找起點用的粗估" in proc.stdout
    assert "nvidia-smi 實測為準" in proc.stdout
    parameters = read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert parameters["gpu_layers"] == 99      # 不再退 --fit 自動配置
    assert "fit_target" not in parameters
    assert parameters["fit"] == "off"

def test_noninteractive_without_flags_fails_with_hint(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    proc = run(tmp_path, "--models-dir", str(models), stdin="")
    assert proc.returncode == 2
    assert "無互動輸入環境" in proc.stderr
    assert "--yes" in proc.stderr

def test_missing_fit_stops_at_preflight_before_any_questions(tmp_path):
    """--fit 是硬需求(VL placement):缺少時要在前置檢查就擋下,
    不能讓使用者答完所有互動題才發現白忙一場。"""
    write_fake_nvidia_smi(tmp_path / "bin", "0, Small GPU, 24576, 20000, GPU-small")
    models = make_models(tmp_path)
    write_fake_llama(tmp_path, help_flags="--reranking --mmproj --cache-ram")  # 沒有 --fit

    proc = run(tmp_path, "--yes", "--models-dir", str(models))
    assert proc.returncode == 2
    assert "安全的 VL placement 需要 llama-server --fit" in proc.stderr
    assert "重新 build" in proc.stderr
    # 前置(preflight)就失敗:還沒開始掃描模型/互動
    assert "[3/5]" not in proc.stdout

def test_llama_help_loader_failure_is_not_misdiagnosed_as_missing_flags(tmp_path):
    """--help 因動態庫問題跑不起來時,要指向執行環境,不能誤診成缺 --reranking。"""
    write_fake_nvidia_smi(tmp_path / "bin", "0, Small GPU, 24576, 20000, GPU-small")
    models = make_models(tmp_path)
    executable = tmp_path / "llama-server"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'error while loading shared libraries: libcudart.so.13: cannot open' >&2\n"
        "exit 127\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    proc = run(tmp_path, "--yes", "--models-dir", str(models))
    assert proc.returncode == 2
    assert "無法執行" in proc.stderr
    assert "LD_LIBRARY_PATH" in proc.stderr
    assert "libcudart.so.13" in proc.stderr          # 原始錯誤要轉述給使用者
    assert "不支援 --reranking" not in proc.stderr    # 不得誤診成旗標問題

def test_llama_help_exec_failure_hard_stops_without_skip(tmp_path):
    """llama-server --help 連跑都跑不動(OSError):非 --skip-binary-check 必須硬停,
    不得假定支援全部旗標然後顯示 PASS。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    broken = tmp_path / "llama-server"
    broken.write_text("#!/nonexistent-interpreter\n", encoding="utf-8")
    broken.chmod(0o755)

    proc = run(tmp_path, "--yes", "--models-dir", str(models))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "--help 無法執行" in proc.stderr

    skipped = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--skip-binary-check",
                   "--models-dir", str(models))
    assert skipped.returncode == 0, skipped.stderr + skipped.stdout
    assert "跳過 llama-server 執行檢查" in skipped.stdout

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

def test_interactive_main_ctx_rejects_above_maximum(tmp_path):
    """互動主模型 ctx 超過 schema 上限(1048576)也要重問,不得寫入後才被驗證打死。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    proc = run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin="1\n1\n9999999\n65536\n2\n1\n2\n8192\n2\n1\n\n")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "無效輸入:請輸入 1024-1048576 的整數" in proc.stdout
    assert read_deployment(tmp_path)["services"]["main"]["ctx"] == 65536

    cli = run(tmp_path, "--yes", "--ctx", "9999999", "--models-dir", str(models))
    assert cli.returncode == 2
    assert "1048576" in cli.stderr

def test_small_main_ctx_clamps_batch_instead_of_failing_validation(tmp_path):
    """--ctx 1024(CLI 允許的最小值):batch 要夾到 ctx,
    不得產生 batch>ctx 再被自己的 schema 驗證打死。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    proc = run(
        tmp_path,
        "--yes", "--no-preview", "--models-dir", str(models),
        "--main-model", "1", "--rerank-model", "1",
        "--main-gpu", "1", "--embed-gpu", "2", "--rerank-gpu", "2", "--vl-gpu", "2",
        "--ctx", "1024", "--threads", "8", "--rerank-ctx", "8192",
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    deployment = read_deployment(tmp_path)
    main = deployment["services"]["main"]
    assert main["ctx"] == 1024
    assert main["batch"] <= 1024
    assert main["ubatch"] <= main["batch"]

def test_reranker_buffer_defaults_and_advanced_override_validates_range(tmp_path):
    """Reranker buffer 不再提問，預設 8192；進階 CLI override 仍嚴格驗證。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    proc = run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin=STDIN_STANDARD)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "【reranker ctx】" not in proc.stdout
    deployment = read_deployment(tmp_path)
    assert deployment["services"]["reranker"]["ctx"] == 8192

    cli = run(tmp_path, "--yes", "--rerank-ctx", "1", "--models-dir", str(models))
    assert cli.returncode == 2
    assert "128" in cli.stderr

def test_single_gpu_warns_and_shares_one_card(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", "0, NVIDIA GeForce RTX 5090, 32607, 30000, GPU-solo")
    models = make_models(tmp_path)
    proc = run(tmp_path, *YES_ONE_GPU, "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr
    assert "只偵測到 1 顆 GPU" in proc.stdout
    services = read_deployment(tmp_path)["services"]
    assert [services[role]["gpu"] for role in ("main", "embedding", "reranker", "vl")] == [
        "GPU-solo"
    ] * 4

def test_dry_run_writes_nothing_for_the_model_flow(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    proc = run(tmp_path, *YES_TWO_GPU, "--dry-run", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr
    assert "[dry-run]" in proc.stdout
    home = tmp_path / "home"
    assert not (home / ".config").exists()
    assert not (home / "start.sh").exists()


# ---------------------------------------------------------------------------
# 併自 tests/test_set_config_reranker_ctx.py(2026-08-20):reranker internal buffer
# 是一般必答題,屬於問答流程契約。
# ---------------------------------------------------------------------------

def _candidate(tmp_path: Path, name: str, size_mib: int = 610) -> sc.ModelCandidate:
    path = tmp_path / name
    with path.open("wb") as handle:
        handle.truncate(size_mib * sc.MIB)
    return sc.ModelCandidate(path=path, total_bytes=path.stat().st_size, shards=1)


def test_reranker_buffer_is_asked_without_default(tmp_path, monkeypatch, capsys):
    """必答、沒有預設值:Enter 不可過關,維護者驗證值只當提示顯示。"""
    candidate = _candidate(tmp_path, "qwen3-reranker-0.6b-q8_0.gguf")
    prompts: list[str] = []
    answers = iter(["", "4096"])

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(sc, "_input", fake_input)

    ctx = sc.choose_reranker_ctx(candidate, override=None, assume_yes=False)
    output = capsys.readouterr().out

    assert ctx == 4096
    assert len(prompts) == 2                      # Enter 被拒絕後重問
    assert f"({sc.MIN_RERANKER_CTX}-{sc.MAX_RERANKER_CTX})" in prompts[0]
    assert f"推薦數值:{sc.VERIFIED_RERANKER_CTX}" in output   # 只是推薦,仍要自己打


def test_reranker_ctx_flag_and_yes_share_the_same_bounds(tmp_path):
    candidate = _candidate(tmp_path, "bge-reranker-v2-m3-Q8_0.gguf")

    assert sc.choose_reranker_ctx(candidate, override=4096, assume_yes=False) == 4096
    assert sc.choose_reranker_ctx(candidate, override=4096, assume_yes=True) == 4096

    try:
        sc.choose_reranker_ctx(candidate, override=1, assume_yes=True)
    except sc.SetupError as exc:
        assert str(sc.MIN_RERANKER_CTX) in str(exc)
    else:
        raise AssertionError("低於下限的旗標值必須報錯")

    # --yes 沒給旗標 → 報錯並指名旗標(不再靜默採用內建值)
    with pytest.raises(sc.SetupError, match="--rerank-ctx"):
        sc.choose_reranker_ctx(candidate, override=None, assume_yes=True)


def test_selected_reranker_ctx_sets_context_and_physical_batch(tmp_path):
    gpu = sc.Gpu(0, "Test GPU", 24576, 24000, "GPU-test")
    main = _candidate(tmp_path, "main.gguf", size_mib=1)
    embed = _candidate(tmp_path, "bge-m3-f16.gguf", size_mib=1)
    reranker = _candidate(tmp_path, "qwen3-reranker-0.6b-q8_0.gguf", size_mib=1)
    vl = _candidate(tmp_path, "vl.gguf", size_mib=1)
    mmproj = tmp_path / "mmproj.gguf"
    mmproj.write_bytes(b"fixture")
    plan = sc.Plan(
        gpus=[gpu],
        main=sc.Selection("main", main, gpu),
        embedding=sc.Selection("embedding", embed, gpu),
        reranker=sc.Selection("reranker", reranker, gpu),
        vl=sc.Selection("vl", vl, gpu, mmproj=mmproj),
        main_key="main",
        ctx=4096,
        threads=4,
        batch=512,
        ubatch=128,
        reranker_ctx=2048,
        llama_bin="/opt/llama-server",
    )

    service = sc.build_deployment_config(plan)["services"]["reranker"]

    assert service["ctx"] == 2048
    assert service["batch"] == 2048
    assert service["ubatch"] == 2048
    assert service["parameters"] == {"parallel": 1, "cache_ram": 0}


# ── 原 test_set_config_models.py:模型探索與 CPU-MoE 決策 ──

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
        "--main-gpu", "1", "--embed-gpu", "2", "--rerank-gpu", "2", "--vl-gpu", "2",
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
        "--main-gpu", "1", "--embed-gpu", "2", "--rerank-gpu", "2", "--vl-gpu", "2",
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

@pytest.mark.parametrize(
    "flags",
    [
        pytest.param(("--n-cpu-moe", "42"), id="n-cpu-moe-over-max-index"),
        pytest.param(("--cpu-moe",), id="explicit-cpu-moe-flag"),
    ],
)
def test_full_cpu_moe_flags_write_the_boolean_key_without_question(tmp_path, flags):
    """兩種「全部 experts 留 RAM」的非互動寫法,都寫成 cpu_moe 布林鍵、不寫 n_cpu_moe:

    - ``--n-cpu-moe`` 超過最大 blk 編號 = 全部 experts 留 RAM(寫成 cpu_moe 布林鍵)。
    - ``--cpu-moe`` 明確代表全部 experts 放 RAM:不再詢問 n-cpu-moe 檔位。

    原本是 test_n_cpu_moe_flag_over_max_index_means_full_cpu_moe 與
    test_explicit_cpu_moe_flag_means_full_ram_without_question 兩條,斷言逐字相同。
    """
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = moe_models_needing_cpu_moe(tmp_path)

    proc = run(tmp_path, *YES_TWO_GPU, *flags, "--no-preview", "--models-dir", str(models))

    assert proc.returncode == 0, proc.stderr + proc.stdout
    parameters = read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert parameters["cpu_moe"] is True
    assert "n_cpu_moe" not in parameters

def test_interactive_prompt_accepts_typed_n_cpu_moe(tmp_path):
    """互動流程:CPU-MoE 只有「幾層」一題(沒有 y/n),由使用者輸入、只驗證範圍。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = moe_models_needing_cpu_moe(tmp_path)

    # main、main GPU(選 2 = 15000 MiB free)、ctx、CPU-MoE 層數先 abc(無效)再 3、
    # embed GPU、reranker、reranker GPU、reranker ctx、VL GPU、摘要確認。
    proc = run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin="1\n2\n65536\nabc\n3\n2\n1\n2\n8192\n2\n1\n\n")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "主聊天模型 CPU-MoE 留在 RAM 的層數(0-1024)" in proc.stdout
    # 提示只留兩件事:(1) 方向 (2) 依 GGUF 權重 + nvidia-smi free VRAM 算的推薦區間。
    # 權重 26 GiB(10 層 × 2 GiB experts + 6 GiB dense)、GPU 2 free 15000 MiB
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
                stdin="1\n1\n65536\n42\n2\n1\n2\n8192\n2\n1\n\n")

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
                stdin="1\n1\n65536\n0\n2\n1\n2\n8192\n2\n1\n\n")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "0 = 不 offload" in proc.stdout
    parameters = read_deployment(tmp_path)["services"]["main"]["parameters"]
    assert "cpu_moe" not in parameters
    assert "n_cpu_moe" not in parameters
    assert parameters["gpu_layers"] == 99

def test_build_without_n_cpu_moe_support_degrades_to_full_cpu_moe(tmp_path):
    """llama-server 沒有 --n-cpu-moe(舊 build):互動輸入的層數改用全 --cpu-moe
    並提示;--n-cpu-moe 旗標則直接報錯。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    write_fake_llama(tmp_path, help_flags="--fit --cpu-moe --reranking --mmproj --cache-ram")
    models = moe_models_needing_cpu_moe(tmp_path)

    proc = run(tmp_path, "--no-preview", "--models-dir", str(models),
                stdin="1\n1\n65536\n3\n2\n1\n2\n8192\n2\n1\n\n")

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
        "--main-gpu", "1", "--embed-gpu", "2", "--rerank-gpu", "2", "--vl-gpu", "2",
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
        "--main-gpu", "1", "--embed-gpu", "2", "--rerank-gpu", "2", "--vl-gpu", "2",
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
        "--main-gpu", "1", "--embed-gpu", "2", "--rerank-gpu", "2", "--vl-gpu", "2",
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
                stdin="1\n1\n65536\n2\n2\n8192\n2\n2\n1\n\n")
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
        llama_bin="/opt/llama-server",
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
    # 主模型走 registry:deployment.json 記的是 registry key(set_config 寫的形狀),
    # 由 models.json 指到真正的 GGUF。以前這裡是拿環境變數把 model 換成絕對路徑。
    (local_path.parent / "models.json").write_text(
        json.dumps({plan.main_key: str(plan.main.candidate.path)}), encoding="utf-8"
    )
    env = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}

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


# ── 原 test_set_config_artifacts.py:產出物、備份與 restore ──




def test_maintenance_dry_run_pins_gpus_and_binds_loopback(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/launch_servers.py"), "--scope", "all", "--dry-run"],
        env=build_env(tmp_path),
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
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0
    content = (tmp_path / "home" / "start.sh").read_text(encoding="utf-8")
    assert "watch -n 1 nvidia-smi" in content
    assert "稍微監控" in content
    # 只有零 argv 啟動，提醒在 launch 成功後才顯示。
    assert 'launch_servers.py --scope all || rc=$?' in content
    assert 'if [ "$rc" -eq 0 ]' in content
    assert '"$@"' not in content
    assert content.index("launch_servers.py --scope all") < content.index("稍微監控")

def test_allow_remote_binds_all_interfaces_with_warning(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--allow-remote",
                "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr
    assert "0.0.0.0" in proc.stdout  # 警告文字

    deployment = read_deployment(tmp_path)
    for role in ("main", "embedding", "reranker", "vl"):
        assert deployment["services"][role]["bind"] == "all-interfaces"

    dry = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/launch_servers.py"), "--scope", "all", "--dry-run"],
        env=build_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert dry.returncode == 0, dry.stderr
    assert "main_bind_host=0.0.0.0" in dry.stdout

@pytest.mark.smoke
def test_generated_start_sh_ignores_legacy_shell_overrides(tmp_path):
    """~/start.sh 不再設定任何殼層變數,而舊變數對啟動指令也不再有任何作用。

    以前這個檔開頭 `unset` 一長串名字再 `export` 幾個權威值。那個機制只在
    「loader 真的會讀環境」時才有意義,而它的失效方式是無聲的:同一台機器上
    另一份安裝的 start.sh export 同名變數,使用者以為在跑 A、實際在跑 B。
    現在設定只來自 deployment.json 與旗標,所以這裡改成兩件事都要成立:
    產生的檔一行 export / unset 都沒有,而殼層殘留的舊名字照樣無效。
    """
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0

    content = (tmp_path / "home" / "start.sh").read_text(encoding="utf-8")
    # 註解可以講「這裡不再 export」;真正被執行的行一個都不准有。
    offenders = [
        line for line in content.splitlines()
        if not line.lstrip().startswith("#") and ("export" in line or "unset" in line)
    ]
    assert not offenders, offenders

    # build_env 已經把整組舊名字設成壞值(LEGACY_SHELL_OVERRIDES),這裡再加兩個
    # 只在舊 start.sh 的 unset 清單裡出現過的。
    env = build_env(tmp_path)
    env["EMBED_MODEL"] = "/bogus/does-not-exist.gguf"   # 模擬 .bashrc 殘留的舊 override
    env["MAIN_CTX"] = "1234"
    env["AICODE_N_CTX"] = "2048"
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/launch_servers.py"), "--scope", "all", "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "/bogus/does-not-exist.gguf" not in proc.stdout
    assert "/bogus/shell-llama-server" not in proc.stdout   # LLAMA_BIN 也不再是入口
    assert "-c 65536" in proc.stdout  # 不被 MAIN_CTX=1234 蓋掉
    # 卡片由 deployment.json 決定;殼層的 CUDA_VISIBLE_DEVICES=7 進不到指令裡。
    assert "CUDA_VISIBLE_DEVICES=7" not in proc.stdout








def test_start_sh_pins_validated_llama_bin(tmp_path):
    """set_config 探測旗標用的是哪一顆 binary,deployment.json 就得記哪一顆,
    否則啟動的是另一顆(可能沒 --fit 的)llama-server。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0
    assert read_deployment(tmp_path)["llama_bin"] == str(tmp_path / "llama-server")


@pytest.mark.smoke
def test_deployment_json_pins_llama_bin_and_gpus(tmp_path):
    """GPU 與 llama-server 路徑的**唯一**落點是 deployment.json,而且重跑會沿用。

    這兩個值以前住在 ~/start.sh 的 export 裡:啟動器讀得到、systemd 與別的入口
    讀不到,而同名變數被別份安裝蓋掉時完全無聲。所以要釘三件事:
    (1) 四個角色各自記自己的卡、頂層記 binary;(2) 沒給 --llama-bin 的重跑會沿用
    檔案裡的值(不會靜默退回 ~/llama.cpp 的預設);(3) 產物是 loader 收得下的形狀。
    """
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    pinned = tmp_path / "custom-llama-server"
    write_fake_llama(tmp_path)
    shutil.copy2(tmp_path / "llama-server", pinned)

    first = run(tmp_path, "--llama-bin", str(pinned), *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models), pin_llama_bin=False)
    assert first.returncode == 0, first.stderr + first.stdout
    written = read_deployment(tmp_path)
    assert written["llama_bin"] == str(pinned)
    assert [written["services"][role]["gpu"]
            for role in ("main", "embedding", "reranker", "vl")] == [
        "GPU-aaaa-5090", "GPU-bbbb-2000", "GPU-bbbb-2000", "GPU-bbbb-2000",
    ]

    # 沒給旗標的重跑:預設值來自剛剛寫下的那份檔,不是內建的 ~/llama.cpp 路徑。
    again = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
                pin_llama_bin=False)
    assert again.returncode == 0, again.stderr + again.stdout
    assert read_deployment(tmp_path)["llama_bin"] == str(pinned)

    # loader 收得下(schema + 絕對路徑 + GPU selector 形狀),而且看到的是同一組值。
    profile = load_effective_profile({"HOME": str(tmp_path / "home")})
    assert profile.llama_bin == str(pinned)
    assert profile.service("main").gpu == "GPU-aaaa-5090"
    assert profile.service("vl").gpu == "GPU-bbbb-2000"



def test_invalid_registry_entries_are_dropped_with_warning(tmp_path):
    """models.json 有格式非法的手寫項目時要剔除並警告,
    否則啟動時整份 registry 會被 loader 拒絕。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
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

    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
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
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0
    deployment_path = tmp_path / "home" / ".config/codetrail/deployment.json"
    config = json.loads(deployment_path.read_text(encoding="utf-8"))
    config["services"]["main"]["parameters"]["temperature"] = 0.6
    config["services"]["main"]["parameters"]["top_p"] = 0.95
    config["services"]["main"]["parameters"]["no_mmap"] = True
    config["services"]["main"]["parameters"]["n_cpu_moe"] = 90
    config["services"]["main"]["parameters"]["custom_flag"] = 123
    deployment_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    rerun = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert rerun.returncode == 0, rerun.stderr + rerun.stdout
    assert "保留你手動加在 deployment.json 的 main 參數" in rerun.stdout
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

@pytest.mark.smoke
def test_generated_start_sh_rejects_removed_commands_before_dispatch(tmp_path):
    """舊 status/logs/help 及所有 flags 必須在啟動前拒絕，不能變成啟動。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview",
               "--models-dir", str(models)).returncode == 0
    start = tmp_path / "home" / "start.sh"
    for args in (("help",), ("stauts",), ("logs", "gpu"), ("logs", "main"),
                 ("status",), ("--dry-run",), ("stop", "--force"), ("",)):
        proc = subprocess.run(["bash", str(start), *args], env=build_env(tmp_path),
                              capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode == 2, (args, proc.stdout, proc.stderr)
        assert "只接受無參數啟動" in proc.stderr


def test_restart_subprocess_env_goes_through_process_env(monkeypatch):
    """[R] 自動重啟的 stop/start 子程序走 process_env:CodeTrail 的四個設定前綴
    一律剝掉,其餘(PATH / 使用者自己的變數)原樣繼承。

    以前這裡是 set_config 自己維護一份「要剔除的名字」清單;清單漏一個就等於
    讓殼層決定 stop 去殺哪個 tmux session。現在剝除只有 process_env 一個出口,
    而 session 名與設定都不再是環境變數。"""
    from scripts import set_config as sc

    monkeypatch.setenv("AICODE_MODEL", "/bogus/shell-model.gguf")
    monkeypatch.setenv("CODETRAIL_ANYTHING", "1")
    monkeypatch.setenv("KEEP_ME", "1")

    calls = []

    class _Result:
        returncode = 0

    def fake_run(cmd, env=None, **_kwargs):
        calls.append((list(cmd), env))
        return _Result()

    # 換掉最底層的 subprocess.run:這樣 env 是 process_env 真的算出來的那一份
    # (patch 掉 process_env.run 只會證明「我們自己傳了什麼」)。
    monkeypatch.setattr(subprocess, "run", fake_run)
    rc = sc._restart_servers(Path("/fake/home/start.sh"))
    assert rc == 0
    assert len(calls) == 2
    assert calls[0][0][1].endswith("stop_servers.py")
    assert calls[0][0][2:4] == ["--scope", "all"]
    assert calls[1][0][1] == "/fake/home/start.sh"
    for _cmd, env in calls:
        assert env is not None, "子行程不得隱式繼承整份殼層環境"
        assert "AICODE_MODEL" not in env
        assert "CODETRAIL_ANYTHING" not in env
        assert env.get("KEEP_ME") == "1"



def test_relative_models_dir_and_llama_bin_are_stored_absolute(tmp_path):
    """相對路徑立刻轉絕對:--models-dir ./models 不得走到最後 schema 驗證才爆;
    相對 --llama-bin 不得原樣寫進 deployment.json —— 真正 exec 它的是 tmux pane
    裡的 loader,cwd 不是使用者打指令的地方(loader 對相對路徑會直接 fail-loud)。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--skip-deps-check", "--llama-bin", "./llama-server",
         *YES_TWO_GPU, "--no-preview", "--models-dir", "./models"],
        cwd=tmp_path,
        env=build_env(tmp_path),
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
    assert read_deployment(tmp_path)["llama_bin"] == str(tmp_path / "llama-server")


@pytest.mark.smoke
def test_removed_log_shorthands_never_dispatch_tail(tmp_path):
    """舊 log 簡寫連同額外參數都拒絕，不得默默忽略或呼叫 tail。"""
    start = tmp_path / "start.sh"
    start.write_text(sc.render_start_wrapper(), encoding="utf-8")
    marker = tmp_path / "tail-record"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_tail = bindir / "tail"
    fake_tail.write_text(f'#!/usr/bin/env bash\necho called > "{marker}"\n', encoding="utf-8")
    fake_tail.chmod(0o700)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ.get('PATH', '')}")
    for args in (("logs", "3"), ("logs", "-f"), ("logs", "main", "5", "x"), ("logs", "gpu")):
        proc = subprocess.run(["bash", str(start), *args], env=env, capture_output=True,
                              text=True, timeout=30, check=False)
        assert proc.returncode == 2, (args, proc.stderr)
    assert not marker.exists()


# ── 壓縮模式與 client.json(每條都是 smoke) ──
#
# 這一段每條都守「壞掉不會有人發現」的東西:
#   * `--yes` 沒給 `--compaction-mode`、機器也還沒選過 → **不得** 動壓縮設定。
#     弄反的話,舊安裝重跑一次 `--yes` 腳本就會突然開始自動壓縮,而使用者
#     沒有要求過任何這種行為。
#   * 寫進 client.json 的模式必須是使用者選的那一個,而且門檻要與 runtime 用的
#     同一條公式對得起來 —— wizard 與 engine 各算各的,兩邊差一點也不會有錯誤。
#   * client.json 是 0600:它決定寫入工具要不要人工核准。
#   * restore manifest 是兩個世代共用的同一個檔:含這一代不會寫的目標就整份拒絕。


def _home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def _offline_fixture(tmp_path: Path) -> Path:
    """CI 沒有 NVIDIA driver:GPU 一定要來自 fixture,不能落到真的 nvidia-smi。

    `build_env` 只是把 `tmp_path/bin` **前置**到真 PATH,所以少了這行,開發機
    上照樣綠、離線 CI 上整批紅(AGENTS.md §4)。
    """
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = tmp_path / "models"
    if not models.exists():
        models = make_models(tmp_path)
    return models


def _run_yes(tmp_path: Path, *extra: str):
    models = _offline_fixture(tmp_path)
    return run(
        tmp_path, *YES_TWO_GPU, "--no-preview", "--skip-binary-check",
        "--models-dir", str(models), *extra,
    )


def _run_yes_ctx(tmp_path: Path, ctx: str, *extra: str):
    models = _offline_fixture(tmp_path)
    args = [a for a in YES_TWO_GPU]
    args[args.index("--ctx") + 1] = ctx
    return run(
        tmp_path, *args, "--no-preview", "--skip-binary-check",
        "--models-dir", str(models), *extra,
    )


def _manifest_path(tmp_path: Path) -> Path:
    return _home(tmp_path) / ".config" / "codetrail" / "setconfig-last-transaction.json"


def _manifest(tmp_path: Path) -> dict:
    return json.loads(_manifest_path(tmp_path).read_text(encoding="utf-8"))


def _client_config(tmp_path: Path) -> Path:
    return _home(tmp_path) / ".config" / "codetrail" / "client.json"


@pytest.mark.smoke
def test_yes_without_the_flag_never_takes_over(tmp_path):
    """舊的 --yes 腳本重跑不得突然開始自動壓縮。"""
    home = _home(tmp_path)
    result = _run_yes(tmp_path)
    assert result.returncode == 0, result.stdout
    assert not _client_config(tmp_path).exists()


@pytest.mark.smoke
def test_codetrail_mode_writes_the_chosen_mode(tmp_path):
    result = _run_yes(tmp_path, "--compaction-mode", "codetrail")
    assert result.returncode == 0, result.stdout
    written = json.loads(_client_config(tmp_path).read_text(encoding="utf-8"))
    assert written["schema"] == 1
    assert written["compaction_mode"] == "codetrail"
    assert written["permission"] == {}


@pytest.mark.smoke
def test_the_written_threshold_matches_the_runtime_formula(tmp_path):
    """wizard 印出來的門檻必須是 runtime 會用的那一個。"""
    result = _run_yes(tmp_path, "--compaction-mode", "codetrail")
    derived = client_compaction.derive(65536)
    assert f"idle 門檻={derived.idle_threshold}" in result.stdout


@pytest.mark.smoke
def test_off_mode_is_recorded_too(tmp_path):
    """off 也是一個明確選擇 —— 有檔案才分得出「選了不壓縮」與「沒選過」。"""
    result = _run_yes(tmp_path, "--compaction-mode", "off")
    assert result.returncode == 0, result.stdout
    written = json.loads(_client_config(tmp_path).read_text(encoding="utf-8"))
    assert written["compaction_mode"] == "off"


@pytest.mark.smoke
def test_the_client_config_is_owner_only(tmp_path):
    """它決定寫入工具要不要人工核准;能被別人改就等於能繞過核准。"""
    _run_yes(tmp_path, "--compaction-mode", "manual")
    target = _client_config(tmp_path)
    assert oct(target.stat().st_mode & 0o777) == "0o600"


@pytest.mark.smoke
def test_yes_without_the_flag_reuses_the_recorded_mode(tmp_path):
    _run_yes(tmp_path, "--compaction-mode", "manual")
    _run_yes(tmp_path)
    written = json.loads(_client_config(tmp_path).read_text(encoding="utf-8"))
    assert written["compaction_mode"] == "manual"


@pytest.mark.smoke
def test_an_existing_permission_override_survives_a_mode_change(tmp_path):
    """權限覆寫是使用者另外設的,換壓縮模式不得把它清掉。"""
    home = _home(tmp_path)
    target = _client_config(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"schema": 1, "compaction_mode": "manual",
                    "permission": {"run_lint": "allow"}}),
        encoding="utf-8",
    )
    target.chmod(0o600)
    _run_yes(tmp_path, "--compaction-mode", "codetrail")
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["compaction_mode"] == "codetrail"
    assert written["permission"] == {"run_lint": "allow"}


@pytest.mark.smoke
def test_dry_run_writes_nothing(tmp_path):
    result = _run_yes(tmp_path, "--compaction-mode", "codetrail", "--dry-run")
    assert result.returncode == 0, result.stdout
    assert not _client_config(tmp_path).exists()


@pytest.mark.smoke
def test_an_unknown_mode_is_rejected(tmp_path):
    result = _run_yes(tmp_path, "--compaction-mode", "native")
    assert result.returncode != 0
    assert "codetrail" in (result.stdout + result.stderr)


@pytest.mark.smoke
def test_a_context_too_small_for_the_formula_is_fail_loud(tmp_path):
    """算不出門檻時當場報錯,不是寫下一個 runtime 永遠不會用的模式。"""
    result = _run_yes_ctx(tmp_path, "8192", "--compaction-mode", "codetrail")
    assert result.returncode != 0
    assert "壓縮模式" in (result.stdout + result.stderr)
    assert not _client_config(tmp_path).exists()




# ── 產物、備份與 restore──


def test_existing_configs_are_backed_up_and_registry_merged(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    home = tmp_path / "home"
    (home / ".config/codetrail").mkdir(parents=True)
    (home / ".config/codetrail/models.json").write_text(
        json.dumps({"old-key": "/somewhere/old.gguf"}), encoding="utf-8"
    )

    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr

    registry = json.loads((home / ".config/codetrail/models.json").read_text(encoding="utf-8"))
    assert registry["old-key"] == "/somewhere/old.gguf"
    assert "big-chat-ud-q4-k-xl" in registry
    assert list((home / ".config/codetrail").glob("models.json.bak-setconfig-*"))


def test_rerun_has_no_carryover_current_answers_win(tmp_path):
    """新契約:重跑不沿用任何舊值 —— 每次的設定完全來自本次作答/旗標。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    first = run(
        tmp_path, "--yes", "--no-preview", "--models-dir", str(models),
        "--main-model", "1", "--rerank-model", "1",
        "--main-gpu", "1", "--embed-gpu", "2", "--rerank-gpu", "2", "--vl-gpu", "2",
        "--ctx", "32768", "--threads", "12", "--rerank-ctx", "4096",
    )
    assert first.returncode == 0, first.stderr
    assert read_deployment(tmp_path)["services"]["main"]["ctx"] == 32768
    assert read_deployment(tmp_path)["services"]["main"]["parameters"]["threads"] == 12

    rerun = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert rerun.returncode == 0, rerun.stderr + rerun.stdout
    assert "沿用" not in rerun.stdout
    deployment = read_deployment(tmp_path)
    assert deployment["services"]["main"]["ctx"] == 65536      # 本次旗標值,不是舊值
    # 這次沒給 --threads → 舊值不沿用,直接不寫 -t
    assert "threads" not in deployment["services"]["main"]["parameters"]
    assert deployment["services"]["reranker"]["ctx"] == 8192


def test_rerun_keeps_hand_edited_port_and_base_url(tmp_path):
    """port/base_url 屬使用者領域(本工具從不寫)→ 手改過的值重跑要原樣保留。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview",
               "--models-dir", str(models)).returncode == 0
    deployment_path = tmp_path / "home" / ".config/codetrail/deployment.json"
    config = json.loads(deployment_path.read_text(encoding="utf-8"))
    config["services"]["main"]["port"] = 18080
    config["services"]["main"]["base_url"] = "http://127.0.0.1:18080"
    deployment_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    rerun = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert rerun.returncode == 0, rerun.stderr + rerun.stdout
    assert "保留你手動設定的 services.main.port=18080" in rerun.stdout
    merged = json.loads(deployment_path.read_text(encoding="utf-8"))
    assert merged["services"]["main"]["port"] == 18080
    assert merged["services"]["main"]["base_url"] == "http://127.0.0.1:18080"


@pytest.mark.smoke
def test_transaction_staging_files_are_private_from_birth(monkeypatch, tmp_path):
    """含使用者選擇的 config 在 chmod 前也不得以 umask 決定的寬鬆 mode 存在。"""
    creation_modes: list[int] = []
    real_open = sc.os.open

    def capture_open(path, flags, mode=0o777, *, dir_fd=None):
        creation_modes.append(mode)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(sc.os, "open", capture_open)
    public = tmp_path / "deployment.json"
    secret = tmp_path / "client.json"
    sc.commit_files(
        [(public, "{}\n", 0o644), (secret, '{"schema":1}\n', 0o600)],
        notes=[],
        dry_run=False,
    )

    assert creation_modes == [0o600, 0o600]
    assert stat.S_IMODE(public.stat().st_mode) == 0o644
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600


def test_restore_last_backup_dry_run_previews_without_touching_files(tmp_path):
    """--restore-last-backup --dry-run 只預覽會做什麼,絕不動檔案。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    home = tmp_path / "home"
    (home / ".config/codetrail").mkdir(parents=True)
    (home / ".config/codetrail/models.json").write_text('{"marker": "/old.gguf"}', encoding="utf-8")
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview",
               "--models-dir", str(models)).returncode == 0

    after_setup = (home / ".config/codetrail/models.json").read_text(encoding="utf-8")
    proc = run(tmp_path, "--restore-last-backup", "--dry-run")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "[dry-run]" in proc.stdout
    assert "會還原" in proc.stdout
    # 檔案完全沒動:還原目標仍是設定後內容,產物一個都沒消失
    assert (home / ".config/codetrail/models.json").read_text(encoding="utf-8") == after_setup
    assert (home / ".config/codetrail/deployment.json").exists()
    assert (home / "start.sh").exists()


@pytest.mark.smoke
def test_the_restore_manifest_only_lists_this_generations_targets(tmp_path):
    """manifest 只會列這一代會寫的四個檔。多出任何別的目標,restore 就得整份拒絕
    (見下一條)——所以「寫的時候不會多」與「讀的時候不接受多」是同一組保證。"""
    models = _offline_fixture(tmp_path)
    home = _home(tmp_path)
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview",
               "--models-dir", str(models)).returncode == 0
    manifest = _manifest(tmp_path)
    allowed = {str(item) for item in sc.main_restore_targets(home)}
    assert set(manifest["targets"]) <= allowed, manifest["targets"]


@pytest.mark.smoke
def test_a_manifest_written_before_the_upgrade_still_restores(tmp_path):
    """升級承接:用**升級前**那一版寫出的 manifest,新版必須整批還原得回去。

    `setconfig-last-transaction.json` 兩個世代共用同一個檔名。新版換了「哪些目標
    算數」的判準;判錯的話,升級前那一次設定的備份就再也還原不了,而使用者只會
    看到一句「找不到 manifest」。舊 manifest 的形狀在這裡逐字重建。
    """
    models = _offline_fixture(tmp_path)
    home = _home(tmp_path)
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview",
               "--models-dir", str(models)).returncode == 0

    # 現況換成「升級後才有的內容」,備份留著升級前的。
    codetrail = home / ".config" / "codetrail"
    (codetrail / "models.json").write_text('{"after": 1}', encoding="utf-8")
    backup = tmp_path / "models.json.bak-setconfig-old"
    backup.write_text('{"before": 1}', encoding="utf-8")

    manifest_path = _manifest_path(tmp_path)
    manifest_path.write_text(
        json.dumps(
            {
                "transaction": "20260101T000000-legacy",
                "targets": {
                    str(codetrail / "models.json"): {
                        "existed": True,
                        "backup": str(backup),
                        "real": str(codetrail / "models.json"),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads((codetrail / "models.json").read_text(encoding="utf-8")) == {"before": 1}


@pytest.mark.smoke
def test_a_manifest_with_a_foreign_target_is_refused_whole(tmp_path):
    """`setconfig-last-transaction.json` 兩個世代共用同一個檔名、同一個形狀,而且
    沒有寫入者標記。含任何「這一代不會寫」的目標 → **整份**拒絕、一個檔都不動。

    為什麼不是「跳過那幾筆、還原其餘」:那會把不同世代的設定拼在一起,而
    transaction 存在的理由正是不要拼裝。"""
    models = _offline_fixture(tmp_path)
    home = _home(tmp_path)
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview",
               "--models-dir", str(models)).returncode == 0

    # 手工把 manifest 改回「舊世代」的形狀:多一筆別人的設定。
    foreign = home / ".config" / "unrelated-app" / "settings.json"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text(json.dumps({"owner": "user-current"}), encoding="utf-8")
    stale_backup = tmp_path / "foreign.bak-setconfig-old"
    stale_backup.write_text(json.dumps({"owner": "codetrail-old"}), encoding="utf-8")
    manifest_path = _manifest_path(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    before = {
        Path(target): Path(target).read_bytes()
        for target in manifest["targets"] if Path(target).exists()
    }
    manifest["targets"][str(foreign)] = {
        "existed": True, "backup": str(stale_backup), "real": str(foreign),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "另一個世代" in proc.stderr
    assert json.loads(foreign.read_text(encoding="utf-8")) == {"owner": "user-current"}
    for target, content in before.items():
        assert target.read_bytes() == content, f"{target} 被動過了"


# ── 原 test_set_config_restore.py:整批還原的資料安全 ──
# 這幾條守的是 AGENTS.md §2 的 transaction 語意。manifest
# 的目標只接受 client.json / models.json / deployment.json / start.sh,但「一半
# 還原比不還原更糟」的判斷完全沒變,所以測試跟著換目標留下來。

@pytest.mark.smoke
def test_quitting_at_the_summary_writes_nothing(tmp_path):
    models = _offline_fixture(tmp_path)
    proc = run(tmp_path, "--no-preview", "--models-dir", str(models),
               stdin="1\n1\n65536\n2\n1\n2\n8192\n2\n1\nq\n")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "未寫入任何檔案" in proc.stdout
    assert not (_home(tmp_path) / ".config").exists()


@pytest.mark.smoke
def test_restore_reports_failure_when_a_backup_is_missing(tmp_path):
    """只還原一半就回報成功,會讓不同檔案停在不同世代。"""
    models = _offline_fixture(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--skip-binary-check",
            "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    assert run(tmp_path, *base, "--compaction-mode", "manual").returncode == 0

    manifest = _manifest(tmp_path)
    backup = next(
        info["backup"] for path, info in manifest["targets"].items()
        if path.endswith("client.json") and info.get("backup")
    )
    Path(backup).unlink()

    before = {
        path: Path(path).read_bytes()
        for path in manifest["targets"] if Path(path).is_file()
    }
    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 2
    assert "已中止" in proc.stderr
    for path, content in before.items():
        assert Path(path).read_bytes() == content, path


@pytest.mark.smoke
def test_restore_never_deletes_a_live_file_when_the_manifest_has_no_backup(tmp_path):
    """`{"existed": true, "backup": null}` 落到「移除」分支就是資料遺失。"""
    models = _offline_fixture(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--skip-binary-check",
            "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    assert run(tmp_path, *base, "--compaction-mode", "manual").returncode == 0

    manifest_path = _manifest_path(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    key = next(k for k in manifest["targets"] if k.endswith("client.json"))
    manifest["targets"][key] = {"existed": True, "backup": None}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 2
    assert "沒有備份路徑" in proc.stderr
    assert _client_config(tmp_path).is_file()


@pytest.mark.smoke
def test_a_corrupt_manifest_does_not_fall_back_to_per_file_backups(tmp_path):
    """逐檔最新備份會混合不同 transaction 的產物,還原出一組拼裝設定。"""
    models = _offline_fixture(tmp_path)
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview", "--skip-binary-check",
               "--models-dir", str(models)).returncode == 0
    _manifest_path(tmp_path).write_text("{ not json", encoding="utf-8")

    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 2
    assert "不退回逐檔模式" in proc.stderr


@pytest.mark.smoke
def test_restore_refuses_when_a_symlinked_config_was_repointed(tmp_path):
    """設定時 link 指向 A、還原前被改指 B:跟著現在的 link 走會拿 A 的舊內容蓋 B。"""
    models = _offline_fixture(tmp_path)
    home = _home(tmp_path)
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    a = dotfiles / "a.json"
    b = dotfiles / "b.json"
    a.write_text(json.dumps({"marker": "A"}), encoding="utf-8")
    b.write_text(json.dumps({"marker": "B"}), encoding="utf-8")
    link = home / ".config" / "codetrail" / "models.json"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(a)

    assert run(tmp_path, *YES_TWO_GPU, "--no-preview", "--skip-binary-check",
               "--models-dir", str(models)).returncode == 0
    link.unlink()
    link.symlink_to(b)

    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 2
    assert "與設定當時的" in proc.stderr
    assert json.loads(b.read_text(encoding="utf-8")) == {"marker": "B"}


@pytest.mark.smoke
def test_a_manifest_that_cannot_be_written_does_not_survive_stale(tmp_path):
    """舊 manifest 留著比沒有更糟:restore 會照上一次 transaction 還原。"""
    models = _offline_fixture(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--skip-binary-check",
            "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    manifest = _manifest_path(tmp_path)
    first = manifest.read_text(encoding="utf-8")
    manifest.chmod(0o444)
    try:
        proc = run(tmp_path, *base, "--compaction-mode", "manual")
        assert proc.returncode == 0, proc.stderr + proc.stdout
        assert not manifest.exists() or manifest.read_text(encoding="utf-8") != first
    finally:
        if manifest.exists():
            manifest.chmod(0o644)




@pytest.mark.smoke
def test_restore_refuses_to_write_client_json_through_a_symlinked_parent(tmp_path):
    """`client.json` 的父目錄(`~/.config/codetrail`)是 symlink → 整批還原拒絕、零寫入。

    2026-09-04(總審 F1-3):原本這條驗的是「經 symlink 父目錄照樣以 owner-only 寫到
    實體位置」。行為為什麼該變:runtime 的 loader 錨在 `~/.config`、逐層 O_NOFOLLOW,
    父目錄是 symlink 時**根本不會讀**那個檔 —— restore 寫出去的是一份沒有人會讀的
    設定,而且寫在 owner-only 錨點之外(manifest 的 `real` 指到哪就寫到哪)。
    與 `client_paths` 的三件套契約一致:同一個檔,讀寫兩端用同一條防線。
    """
    models = _offline_fixture(tmp_path)
    home = _home(tmp_path)
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "manual").returncode == 0

    logical = _client_config(tmp_path)
    real_dir = tmp_path / "real-config"
    real_dir.mkdir()
    codetrail = home / ".config" / "codetrail"
    for item in codetrail.iterdir():
        item.rename(real_dir / item.name)
    codetrail.rmdir()
    codetrail.symlink_to(real_dir)

    backup = tmp_path / "client.json.bak-setconfig-old"
    backup.write_text(json.dumps({"schema": 1, "compaction_mode": "off", "permission": {}}), encoding="utf-8")
    manifest_path = _manifest_path(tmp_path)
    manifest_path.write_text(json.dumps({
        "transaction": "20260101T000000-symlinked",
        "targets": {str(logical): {"existed": True, "backup": str(backup), "real": str(real_dir / "client.json")}},
    }), encoding="utf-8")
    before = {p.name: p.read_bytes() for p in real_dir.iterdir() if p.is_file()}

    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "symlink" in proc.stdout + proc.stderr
    assert {p.name: p.read_bytes() for p in real_dir.iterdir() if p.is_file()} == before
