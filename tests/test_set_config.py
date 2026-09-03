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
- 原 test_set_config_artifacts.py:產出物、build prompt、opencode.json 合併、備份與 restore。
- 原 test_set_config_compaction.py:壓縮模式題 —— 接管、還原、與不接管。這一段每條都是
  smoke(原檔是 module 層 pytestmark),會靜默失敗的東西才寫在那裡:

    * `--yes` 沒給 `--compaction-mode`、機器也還沒選過 → **不得** 動壓縮設定。
      弄反的話,舊安裝重跑一次 `--yes` 腳本就會突然多一個壓縮 plugin 與
      `compaction.auto=false`,而使用者沒有要求過任何這種行為。
    * 寫進 opencode.json 的 `preserve_recent_tokens` 必須等於同一條公式對這個
      ctx 的推導值。wizard 與 plugin 各算各的,兩邊差一點也不會有錯誤訊息 ——
      只是門檻與保留額對不上。
    * 切回 native 必須精確還原,而且只還原有 ownership 證據的值。
    * `--dry-run` 與摘要頁按 q 都不得留下狀態檔。
    * 狀態檔記錄的是另一份 opencode.json 時不得靜默覆蓋(那是對方唯一的還原依據)。

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
from pathlib import Path

import pytest

import compaction_mode as cm
from deployment_profile import (
    ProfileError,
    build_server_command,
    cpu_moe_fit_conflict,
    load_effective_profile,
)
from scripts import opencode_build_prompt as build_prompt
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
    assert (tmp_path / "home" / "start_opencode.sh").exists()
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
    assert (tmp_path / "home" / "start_opencode.sh").exists()

    home2 = tmp_path / "home2"
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--skip-deps-check", "--no-preview", "--models-dir", str(models)],
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
    assert not (home2 / "start_opencode.sh").exists()

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
    assert not (tmp_path / "home" / "start_opencode.sh").exists()

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
    content = (tmp_path / "home" / "start_opencode.sh").read_text(encoding="utf-8")
    assert "export MAIN_GPU=GPU-bbbb-2000" in content
    # aux 三顆不同卡(embed=GPU 1、rerank/vl=GPU 2)→ 逐 role export
    assert "export EMBED_GPU=GPU-aaaa-5090" in content

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
    assert "LLAMA_BIN" in proc.stderr

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
    (塞不塞得下由使用者以啟動後 nvidia-smi 實測;start_opencode.sh 結尾提醒)。"""
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
    content = (tmp_path / "home" / "start_opencode.sh").read_text(encoding="utf-8")
    assert "export MAIN_GPU=GPU-solo" in content
    assert "export AUX_GPU=GPU-solo" in content

def test_dry_run_writes_nothing_for_the_model_flow(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    proc = run(tmp_path, *YES_TWO_GPU, "--dry-run", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr
    assert "[dry-run]" in proc.stdout
    home = tmp_path / "home"
    assert not (home / ".config").exists()
    assert not (home / "start_opencode.sh").exists()


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
    start_sh = (tmp_path / "home" / "start_opencode.sh").read_text(encoding="utf-8")
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
    assert not (tmp_path / "home" / "start_opencode.sh").exists()  # 未寫入任何設定

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


# ── 原 test_set_config_artifacts.py:產出物、opencode.json 合併、備份與 restore ──

@pytest.mark.smoke
def test_yes_run_keeps_unmeasured_build_prompt_out_of_default_artifacts(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    proc = run(tmp_path, *YES_TWO_GPU, "--models-dir", str(models))

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
    # 沒給 --threads → 不寫 -t,交給 llama.cpp 自己的預設
    assert "threads" not in services["main"]["parameters"]
    assert services["main"]["parameters"]["gpu_layers"] == 99
    assert services["embedding"]["model"] == str(models / "bge-m3" / "bge-m3-f16.gguf")
    assert services["reranker"]["model"].endswith("bge-reranker-v2-m3-Q8_0.gguf")
    assert services["vl"]["model"] == str(models / "vl" / "vl-model-q6.gguf")
    assert services["vl"]["mmproj"] == str(models / "vl" / "mmproj-F16.gguf")
    assert services["embedding"]["parameters"] == {"parallel": 1, "cache_ram": 0}
    assert services["reranker"]["ctx"] == 8192
    assert services["reranker"]["batch"] == 8192
    assert services["reranker"]["ubatch"] == 8192
    assert services["reranker"]["parameters"] == {"parallel": 1, "cache_ram": 0}
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
    assert not build_prompt.build_prompt_path(home).exists()
    assert "agent" not in opencode

    start = home / "start_opencode.sh"
    content = start.read_text(encoding="utf-8")
    assert "generated by CodeTrail set_config.sh" in content
    assert "export MAIN_GPU=GPU-aaaa-5090" in content
    assert "export AUX_GPU=GPU-bbbb-2000" in content
    assert 'launch_servers.py --scope all "$@" || rc=$?' in content
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


def test_experimental_build_prompt_requires_explicit_opt_in(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    proc = run(
        tmp_path,
        *YES_TWO_GPU,
        "--enable-experimental-build-prompt",
        "--no-preview",
        "--models-dir",
        str(models),
    )

    assert proc.returncode == 0, proc.stderr + proc.stdout
    home = tmp_path / "home"
    prompt_path = build_prompt.build_prompt_path(home)
    prompt_body = build_prompt.extract_build_prompt(
        build_prompt.BUILD_PROMPT_DOC.read_text(encoding="utf-8")
    )
    opencode = json.loads(
        (home / ".config/opencode/opencode.json").read_text(encoding="utf-8")
    )
    assert prompt_path.read_text(encoding="utf-8") == prompt_body
    assert stat.S_IMODE(prompt_path.stat().st_mode) == 0o644
    assert opencode["agent"]["build"]["prompt"] == (
        build_prompt.build_prompt_reference(prompt_path)
    )
    assert "不是 supported 預設" in proc.stdout

def test_generated_start_sh_dry_run_pins_gpus_and_binds_loopback(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0

    proc = subprocess.run(
        ["bash", str(tmp_path / "home" / "start_opencode.sh"), "--dry-run"],
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
    content = (tmp_path / "home" / "start_opencode.sh").read_text(encoding="utf-8")
    assert "watch -n 1 nvidia-smi" in content
    assert "稍微監控" in content
    # 提醒在啟動流程之後、只在成功(rc=0)且非 --dry-run 時印出
    assert 'launch_servers.py --scope all "$@" || rc=$?' in content
    assert '*" --dry-run "*' in content
    assert content.index('launch_servers.py --scope all "$@"') < content.index("稍微監控")

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
        ["bash", str(tmp_path / "home" / "start_opencode.sh"), "--dry-run"],
        env=build_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert dry.returncode == 0, dry.stderr
    assert "main_bind_host=0.0.0.0" in dry.stdout

def test_generated_start_sh_clears_legacy_env_overrides(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0

    env = build_env(tmp_path)
    env["EMBED_MODEL"] = "/bogus/does-not-exist.gguf"   # 模擬 .bashrc 殘留的舊 override
    env["MAIN_CTX"] = "1234"
    env["AICODE_N_CTX"] = "2048"
    proc = subprocess.run(
        ["bash", str(tmp_path / "home" / "start_opencode.sh"), "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "/bogus/does-not-exist.gguf" not in proc.stdout
    assert "-c 65536" in proc.stdout  # 不被 MAIN_CTX=1234 蓋掉

def test_existing_configs_are_backed_up_and_registry_merged(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    home = tmp_path / "home"
    (home / ".config/codetrail").mkdir(parents=True)
    (home / ".config/codetrail/models.json").write_text(
        json.dumps({"old-key": "/somewhere/old.gguf"}), encoding="utf-8"
    )
    (home / ".config/opencode").mkdir(parents=True)
    (home / ".config/opencode/opencode.json").write_text("{}", encoding="utf-8")

    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr

    registry = json.loads((home / ".config/codetrail/models.json").read_text(encoding="utf-8"))
    assert registry["old-key"] == "/somewhere/old.gguf"
    assert "big-chat-ud-q4-k-xl" in registry
    assert list((home / ".config/codetrail").glob("models.json.bak-setconfig-*"))
    assert list((home / ".config/opencode").glob("opencode.json.bak-setconfig-*"))

def test_opencode_merge_preserves_user_config_and_respects_permission(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
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
        "agent": {"build": {"prompt": "Keep my private build instructions."}},
    }
    (home / ".config/opencode/opencode.json").write_text(json.dumps(existing), encoding="utf-8")

    proc = run(
        tmp_path,
        *YES_TWO_GPU,
        "--enable-experimental-build-prompt",
        "--no-preview",
        "--models-dir",
        str(models),
    )
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
    assert merged["agent"]["build"]["prompt"] == "Keep my private build instructions."
    assert "user-customised" in proc.stdout
    assert "llamacpp" in merged["enabled_providers"]
    assert "openrouter" in merged["enabled_providers"]


def test_opencode_build_prompt_wrong_type_aborts_before_transaction(tmp_path):
    """agent/build/prompt 型別錯誤不得被重建，也不得留下半套 prompt artifact。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    home = tmp_path / "home"
    opencode_path = home / ".config/opencode/opencode.json"
    opencode_path.parent.mkdir(parents=True)
    original = {
        "theme": "keep-me",
        "agent": {"build": {"prompt": ["not", "a", "string"]}},
    }
    opencode_path.write_text(json.dumps(original), encoding="utf-8")

    proc = run(
        tmp_path,
        *YES_TWO_GPU,
        "--enable-experimental-build-prompt",
        "--no-preview",
        "--models-dir",
        str(models),
    )

    assert proc.returncode == 2
    assert "agent.build.prompt" in proc.stderr
    assert json.loads(opencode_path.read_text(encoding="utf-8")) == original
    assert not build_prompt.build_prompt_path(home).exists()
    assert not (home / ".config/codetrail/models.json").exists()

def test_restore_last_backup_round_trips_whole_transaction(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    home = tmp_path / "home"
    (home / ".config/codetrail").mkdir(parents=True)
    (home / ".config/codetrail/models.json").write_text('{"marker": "/old.gguf"}', encoding="utf-8")

    assert run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0
    registry = json.loads((home / ".config/codetrail/models.json").read_text(encoding="utf-8"))
    assert "big-chat-ud-q4-k-xl" in registry
    assert (home / ".config/codetrail/setconfig-last-transaction.json").is_file()

    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 0, proc.stderr
    # manifest 整批還原:當時存在的檔案回到備份內容…
    restored = json.loads((home / ".config/codetrail/models.json").read_text(encoding="utf-8"))
    assert restored == {"marker": "/old.gguf"}
    # …當時不存在的檔案被移除,不會殘留半套設定
    assert not (home / ".config/codetrail/deployment.json").exists()
    assert not build_prompt.build_prompt_path(home).exists()
    assert not (home / ".config/opencode/opencode.json").exists()
    assert not (home / "start_opencode.sh").exists()


def test_prompt_and_config_roll_back_as_one_transaction(monkeypatch, tmp_path):
    """config replace 失敗時，已替換的 prompt 必須回到原內容與 mode。"""
    home = tmp_path / "home"
    prompt = build_prompt.build_prompt_path(home)
    config = home / ".config/opencode/opencode.json"
    prompt.parent.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    prompt.write_text("old prompt\n", encoding="utf-8")
    prompt.chmod(0o600)
    config.write_text('{"old": true}\n', encoding="utf-8")
    config.chmod(0o600)

    real_replace = sc.os.replace
    calls = 0

    def fail_second_replace(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic config replace failure")
        return real_replace(source, target)

    monkeypatch.setattr(sc.os, "replace", fail_second_replace)
    try:
        sc.commit_files(
            [
                (prompt, "new prompt\n", 0o644),
                (config, '{"new": true}\n', 0o600),
            ],
            [],
            False,
            home=home,
        )
    except OSError as exc:
        assert "synthetic config replace failure" in str(exc)
    else:
        raise AssertionError("synthetic replace failure was not propagated")

    assert prompt.read_text(encoding="utf-8") == "old prompt\n"
    assert stat.S_IMODE(prompt.stat().st_mode) == 0o600
    assert config.read_text(encoding="utf-8") == '{"old": true}\n'
    assert not (home / ".config/codetrail/setconfig-last-transaction.json").exists()

def test_start_sh_pins_validated_llama_bin(tmp_path):
    """set_config 用 LLAMA_BIN 驗證旗標 → 產生的 start_opencode.sh 必須寫死同一顆 binary,
    否則新 shell 啟動的是另一顆(可能沒 --fit 的)llama-server。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0
    content = (tmp_path / "home" / "start_opencode.sh").read_text(encoding="utf-8")
    assert f"export LLAMA_BIN={tmp_path / 'llama-server'}" in content

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

    opencode = json.loads(
        (tmp_path / "home" / ".config/opencode/opencode.json").read_text(encoding="utf-8")
    )
    limit = opencode["provider"]["llamacpp"]["models"]["big-chat-ud-q4-k-xl"]["limit"]
    assert limit["context"] == 65536

def test_symlinked_config_files_are_written_through(tmp_path):
    """dotfiles 的 config/prompt symlink 都寫穿到目標並保留連結。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    home = tmp_path / "home"
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "opencode.json"
    real.write_text("{}", encoding="utf-8")
    real_prompt = dotfiles / "opencode-build-prompt.md"
    real_prompt.write_text("old managed prompt\n", encoding="utf-8")
    (home / ".config/opencode").mkdir(parents=True)
    (home / ".config/opencode/opencode.json").symlink_to(real)
    (home / ".config/codetrail").mkdir(parents=True)
    prompt_link = build_prompt.build_prompt_path(home)
    prompt_link.symlink_to(real_prompt)

    proc = run(
        tmp_path,
        *YES_TWO_GPU,
        "--enable-experimental-build-prompt",
        "--no-preview",
        "--models-dir",
        str(models),
    )
    assert proc.returncode == 0, proc.stderr
    link = home / ".config/opencode/opencode.json"
    assert link.is_symlink()  # 連結還在,沒被換成一般檔
    merged = json.loads(real.read_text(encoding="utf-8"))
    assert merged["model"] == "llamacpp/big-chat-ud-q4-k-xl"  # 內容寫到目標
    assert prompt_link.is_symlink()
    assert real_prompt.read_text(encoding="utf-8") == build_prompt.extract_build_prompt(
        build_prompt.BUILD_PROMPT_DOC.read_text(encoding="utf-8")
    )
    assert stat.S_IMODE(real_prompt.stat().st_mode) == 0o644
    assert "symlink" in proc.stdout

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

def test_generated_start_sh_subcommand_guard_and_logs_validation(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0
    start = tmp_path / "home" / "start_opencode.sh"

    def run_start(*args: str):
        return subprocess.run(
            ["bash", str(start), *args],
            env=build_env(tmp_path),
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
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0
    content = (tmp_path / "home" / "start_opencode.sh").read_text(encoding="utf-8")
    dispatch = content.index('case "${1:-}"')
    assert content.index("unset ") < content.index("export AICODE_MODEL=") < dispatch
    assert content.index("export MAIN_GPU=") < dispatch
    assert content.index("export LLAMA_BIN=") < dispatch

def test_opencode_json_written_owner_only_and_dry_run_redacts_api_keys(tmp_path):
    """opencode.json 合併後帶著使用者 provider 的 apiKey:檔案必須 0600
    (不得把原本的 0600 重跑成 0644),--dry-run 印出的內容也不得出現金鑰原文。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    home = tmp_path / "home"
    (home / ".config/opencode").mkdir(parents=True)
    secret = "sk-live-super-secret-123"
    opencode_path = home / ".config/opencode/opencode.json"
    opencode_path.write_text(
        json.dumps({"provider": {"openrouter": {"options": {"apiKey": secret}}}}),
        encoding="utf-8",
    )
    opencode_path.chmod(0o600)

    dry = run(tmp_path, *YES_TWO_GPU, "--dry-run", "--models-dir", str(models))
    assert dry.returncode == 0, dry.stderr + dry.stdout
    assert secret not in dry.stdout
    assert "***redacted***" in dry.stdout
    assert "憑證類欄位值已遮罩" in dry.stdout
    assert stat.S_IMODE(opencode_path.stat().st_mode) == 0o600  # dry-run 不動檔案

    real = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert real.returncode == 0, real.stderr + real.stdout
    assert stat.S_IMODE(opencode_path.stat().st_mode) == 0o600
    assert not build_prompt.build_prompt_path(home).exists()
    merged = json.loads(opencode_path.read_text(encoding="utf-8"))
    # 遮罩只影響顯示,實際寫入的金鑰原樣保留
    assert merged["provider"]["openrouter"]["options"]["apiKey"] == secret


def test_transaction_staging_files_are_private_from_birth(monkeypatch, tmp_path):
    """含憑證的 config 在 chmod 前也不得以 umask 決定的寬鬆 mode 存在。"""
    creation_modes: list[int] = []
    real_open = sc.os.open

    def capture_open(path, flags, mode=0o777, *, dir_fd=None):
        creation_modes.append(mode)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(sc.os, "open", capture_open)
    public = tmp_path / "prompt.md"
    secret = tmp_path / "opencode.json"
    sc.commit_files(
        [(public, "public\n", 0o644), (secret, '{"apiKey":"synthetic"}\n', 0o600)],
        notes=[],
        dry_run=False,
    )

    assert creation_modes == [0o600, 0o600]
    assert stat.S_IMODE(public.stat().st_mode) == 0o644
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600

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
    rc = sc._restart_servers(Path("/fake/home/start_opencode.sh"))
    assert rc == 0
    assert len(calls) == 2
    assert calls[0][0][1].endswith("stop_servers.py")
    assert calls[0][0][2:4] == ["--scope", "all"]
    assert calls[1][0][1] == "/fake/home/start_opencode.sh"
    for _cmd, env in calls:
        assert env is not None
        assert "SESSION" not in env and "MAIN_GPU" not in env

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
    # OpenCode 的 baseURL 必須跟著保留下來的 main endpoint,不能仍寫死 8080
    opencode = json.loads(
        (tmp_path / "home" / ".config/opencode/opencode.json").read_text(encoding="utf-8")
    )
    options = opencode["provider"]["llamacpp"]["options"]
    assert options["baseURL"] == "http://127.0.0.1:18080/v1"
    assert "對齊 deployment 的 main endpoint" in rerun.stdout

def test_opencode_config_env_var_is_honored(tmp_path):
    """OpenCode 與 config.py/aicode_opencode 都先讀 OPENCODE_CONFIG;set_config 寫死預設
    路徑會做出「顯示 PASS 但完全沒生效」的設定。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    custom = tmp_path / "custom" / "oc.json"
    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
                env_overrides={"OPENCODE_CONFIG": str(custom)})
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "OPENCODE_CONFIG 已設定" in proc.stdout
    written = json.loads(custom.read_text(encoding="utf-8"))
    assert written["model"] == "llamacpp/big-chat-ud-q4-k-xl"
    assert not (tmp_path / "home" / ".config/opencode/opencode.json").exists()

def test_relative_models_dir_and_llama_bin_are_stored_absolute(tmp_path):
    """相對路徑立刻轉絕對:--models-dir ./models 不得走到最後 schema 驗證才爆;
    相對 LLAMA_BIN 不得原樣寫進 ~/start_opencode.sh(換目錄執行就找不到)。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    env = build_env(tmp_path)
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
    content = (tmp_path / "home" / "start_opencode.sh").read_text(encoding="utf-8")
    assert f"export LLAMA_BIN={tmp_path / 'llama-server'}" in content
    assert "export LLAMA_BIN=./llama-server" not in content

def test_opencode_merge_rebuilds_wrong_typed_sections_without_traceback(tmp_path):
    """合法 JSON 但 provider/mcp/models 型別錯誤:降級重建+變更說明,不得 traceback。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    home = tmp_path / "home"
    (home / ".config/opencode").mkdir(parents=True)
    opencode_path = home / ".config/opencode/opencode.json"
    opencode_path.write_text(
        json.dumps({"provider": [], "mcp": "not-an-object", "theme": "dark"}),
        encoding="utf-8",
    )
    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
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
    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr + proc.stdout
    merged = json.loads(opencode_path.read_text(encoding="utf-8"))
    assert "big-chat-ud-q4-k-xl" in merged["provider"]["llamacpp"]["models"]

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
    assert "會移除" in proc.stdout
    # 檔案完全沒動:還原目標仍是設定後內容,產物一個都沒消失
    assert (home / ".config/codetrail/models.json").read_text(encoding="utf-8") == after_setup
    assert (home / ".config/codetrail/deployment.json").exists()
    assert (home / ".config/opencode/opencode.json").exists()
    assert (home / "start_opencode.sh").exists()

def test_deployment_env_override_split_brain_warns(tmp_path):
    """AICODE_DEPLOYMENT_CONFIG 等 override 有設時要警告:aicode_opencode 會讀自訂檔、
    ~/start_opencode.sh 卻刻意 unset,兩邊將各用一份設定。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    proc = run(
        tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
        env_overrides={"AICODE_DEPLOYMENT_CONFIG": str(tmp_path / "custom-deploy.json")},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "偵測到環境變數 AICODE_DEPLOYMENT_CONFIG" in proc.stdout
    assert "各用一份設定" in proc.stdout

def test_logs_accepts_count_and_follow_shorthand(tmp_path):
    """logs 依說明允許省略 role:logs 3 / logs -f 都要能用;多餘參數不得靜默忽略。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    assert run(tmp_path, *YES_TWO_GPU, "--no-preview",
                "--models-dir", str(models)).returncode == 0
    home = tmp_path / "home"
    start = home / "start_opencode.sh"
    state = home / ".local" / "state"
    log_dir = state / "codetrail" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "main.log").write_text("line1\nline2\n", encoding="utf-8")
    env = build_env(tmp_path)
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

    tail_args = tmp_path / "tail_args.txt"
    fake_tail = tmp_path / "bin" / "tail"
    fake_tail.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"$AICODE_TEST_TAIL_ARGS\"\n",
        encoding="utf-8",
    )
    fake_tail.chmod(0o700)
    env["AICODE_TEST_TAIL_ARGS"] = str(tail_args)
    follow = run_start("logs", "-f")
    assert follow.returncode == 0, follow.stderr
    assert tail_args.read_text(encoding="utf-8").splitlines()[0] == "-f"


# ── 原 test_set_config_compaction.py:壓縮模式(每條都是 smoke) ──

def _home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def _opencode(tmp_path: Path) -> dict:
    path = _home(tmp_path) / ".config" / "opencode" / "opencode.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _state_path(tmp_path: Path) -> Path:
    return _home(tmp_path) / ".config" / "codetrail" / "compaction.json"


def _setup(tmp_path: Path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    return make_models(tmp_path)


@pytest.mark.smoke
def test_yes_without_the_flag_never_takes_over(tmp_path):
    """舊的 --yes 腳本重跑不得突然多一個壓縮 plugin。"""
    models = _setup(tmp_path)
    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert proc.returncode == 0, proc.stderr + proc.stdout
    config = _opencode(tmp_path)
    assert "compaction" not in config
    assert "plugin" not in config
    assert not _state_path(tmp_path).exists()
    assert "這次不碰" in proc.stdout


@pytest.mark.smoke
def test_codetrail_mode_writes_the_derived_managed_values(tmp_path):
    models = _setup(tmp_path)
    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode == 0, proc.stderr + proc.stdout

    config = _opencode(tmp_path)
    # YES_TWO_GPU 用 --ctx 65536;受管值必須等於同一條公式的推導結果
    derived = cm.derive_settings(context_limit=65536, output_limit=8192)
    assert config["compaction"] == derived.config_values
    assert config["compaction"]["auto"] is False
    assert config["compaction"]["tail_turns"] == 1
    assert config["plugin"] == [str(cm.PLUGIN_PATH)]

    state_path = _state_path(tmp_path)
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    state = cm.load_state(path=state_path)
    assert state is not None and state["mode"] == "codetrail"
    assert state["managed"]["auto"]["prior"] == {"present": False}
    assert cm.state_matches_config(
        state, _home(tmp_path) / ".config" / "opencode" / "opencode.json"
    )
    assert cm.effective_drift(config, state=state, plugin_path=cm.PLUGIN_PATH) == []


@pytest.mark.smoke
def test_manual_mode_registers_the_plugin_without_auto(tmp_path):
    models = _setup(tmp_path)
    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "manual")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    config = _opencode(tmp_path)
    assert config["compaction"]["auto"] is False
    assert config["plugin"] == [str(cm.PLUGIN_PATH)]
    assert cm.load_state(path=_state_path(tmp_path))["mode"] == "manual"


@pytest.mark.smoke
def test_switching_back_to_native_restores_and_deregisters(tmp_path):
    models = _setup(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    assert _opencode(tmp_path)["compaction"]["auto"] is False

    proc = run(tmp_path, *base, "--compaction-mode", "native")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    config = _opencode(tmp_path)
    assert "compaction" not in config          # 接管前沒有這個區塊
    assert "plugin" not in config              # 我們註冊的那一筆被移除,陣列空了
    state = cm.load_state(path=_state_path(tmp_path))
    assert state["mode"] == "native" and state["managed"] == {}


@pytest.mark.smoke
def test_native_keeps_a_value_the_user_set_before_takeover(tmp_path):
    """使用者原本就有 compaction 設定時,切回 native 要一模一樣還原。"""
    models = _setup(tmp_path)
    opencode = _home(tmp_path) / ".config" / "opencode" / "opencode.json"
    opencode.parent.mkdir(parents=True, exist_ok=True)
    opencode.write_text(
        json.dumps({"compaction": {"auto": True, "prune": True, "tail_turns": 4}}),
        encoding="utf-8",
    )
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    assert _opencode(tmp_path)["compaction"]["tail_turns"] == 1

    assert run(tmp_path, *base, "--compaction-mode", "native").returncode == 0
    assert _opencode(tmp_path)["compaction"] == {
        "auto": True, "prune": True, "tail_turns": 4,
    }


@pytest.mark.smoke
def test_first_run_native_never_touches_the_config(tmp_path):
    """第一次就選 native:壓縮相關的設定必須跟這個功能不存在時一模一樣。

    這是「選原本的壓縮 = 原本的行為」那條保證,而它很容易在重構時被破壞:
    native 不需要推導受管值,所以順手補一句「至少寫上預設」看起來人畜無害
    —— 實際上是把使用者原本的 OpenCode 壓縮行為改掉,而畫面上只會顯示
    「壓縮模式:native」。plugin 同理:多註冊一筆就等於接管了。
    """
    models = _setup(tmp_path)
    opencode = _home(tmp_path) / ".config" / "opencode" / "opencode.json"
    opencode.parent.mkdir(parents=True, exist_ok=True)
    before = {"compaction": {"auto": True, "reserved": 4096}, "plugin": ["./mine.js"]}
    opencode.write_text(json.dumps(before), encoding="utf-8")

    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    proc = run(tmp_path, *base, "--compaction-mode", "native")
    assert proc.returncode == 0, proc.stderr + proc.stdout

    config = _opencode(tmp_path)
    assert config["compaction"] == before["compaction"]   # 一個鍵都沒動
    assert config["plugin"] == ["./mine.js"]              # 沒有塞壓縮 plugin
    state = cm.load_state(path=_state_path(tmp_path))
    assert state["mode"] == "native" and state["managed"] == {}


@pytest.mark.smoke
def test_rerunning_the_same_mode_keeps_the_original_prior(tmp_path):
    """重跑 set_config 不得把「接管前原值」換成 CodeTrail 自己寫的值。"""
    models = _setup(tmp_path)
    opencode = _home(tmp_path) / ".config" / "opencode" / "opencode.json"
    opencode.parent.mkdir(parents=True, exist_ok=True)
    opencode.write_text(json.dumps({"compaction": {"auto": True}}), encoding="utf-8")
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    state = cm.load_state(path=_state_path(tmp_path))
    assert state["managed"]["auto"]["prior"] == {"present": True, "value": True}

    assert run(tmp_path, *base, "--compaction-mode", "native").returncode == 0
    assert _opencode(tmp_path)["compaction"] == {"auto": True}


@pytest.mark.smoke
def test_yes_without_the_flag_reuses_the_recorded_mode(tmp_path):
    models = _setup(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    assert run(tmp_path, *base).returncode == 0
    assert _opencode(tmp_path)["compaction"]["auto"] is False
    assert cm.load_state(path=_state_path(tmp_path))["mode"] == "codetrail"


@pytest.mark.smoke
def test_dry_run_writes_nothing(tmp_path):
    models = _setup(tmp_path)
    proc = run(tmp_path, *YES_TWO_GPU, "--dry-run", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "compaction.json" in proc.stdout      # 內容有被預覽
    assert not _state_path(tmp_path).exists()
    assert not (_home(tmp_path) / ".config" / "opencode").exists()


@pytest.mark.smoke
def test_quitting_at_the_summary_writes_nothing(tmp_path):
    models = _setup(tmp_path)
    proc = run(tmp_path, "--no-preview", "--models-dir", str(models),
               stdin="1\n1\n65536\n2\n1\n2\n8192\n2\n1\nq\n")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "未寫入任何檔案" in proc.stdout
    assert not (_home(tmp_path) / ".config").exists()


@pytest.mark.smoke
def test_interactive_question_has_no_default(tmp_path):
    """其餘使用者選擇題都沒有預設值;這題按 Enter 也不能過關。"""
    models = _setup(tmp_path)
    proc = run(tmp_path, "--no-preview", "--models-dir", str(models),
               stdin="1\n1\n65536\n2\n1\n2\n8192\n2\n\n9\n3\n\n")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "=== [5/5] 壓縮模式 ===" in proc.stdout
    assert proc.stdout.count("編號只有 1-3") == 2     # 空白與 9 各重問一次
    assert cm.load_state(path=_state_path(tmp_path))["mode"] == "manual"


@pytest.mark.smoke
def test_a_context_too_small_for_the_contract_is_fail_loud(tmp_path):
    """推不出門檻時必須明講,不能寫一個算不出來的受管值。"""
    models = _setup(tmp_path)
    proc = run(tmp_path, "--yes", "--main-model", "1", "--rerank-model", "1",
               "--main-gpu", "1", "--embed-gpu", "2", "--rerank-gpu", "2",
               "--vl-gpu", "2", "--ctx", "16384", "--rerank-ctx", "8192",
               "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode != 0
    assert "壓縮模式 codetrail 無法套用" in proc.stderr
    assert not _state_path(tmp_path).exists()


@pytest.mark.smoke
def test_a_state_file_for_another_config_is_refused(tmp_path):
    """那份紀錄是另一份 opencode.json 唯一的還原依據,不得靜默覆蓋。"""
    models = _setup(tmp_path)
    home = _home(tmp_path)
    state_dir = home / ".config" / "codetrail"
    state_dir.mkdir(parents=True, exist_ok=True)
    config: dict = {"compaction": {"auto": True}}
    _, _, errors, state = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL,
        derived=cm.derive_settings(context_limit=65536, output_limit=8192),
        prior_state=None, config_path=Path("/somewhere/else/opencode.json"),
    )
    assert errors == []
    cm.save_state(state, path=state_dir / "compaction.json")

    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "native")
    assert proc.returncode != 0
    assert "另一份 opencode.json" in proc.stderr
    assert cm.load_state(path=_state_path(tmp_path))["config"] == state["config"]


@pytest.mark.smoke
def test_restore_last_backup_puts_the_ownership_record_back(tmp_path):
    """狀態檔必須跟 opencode.json 同一個 transaction 進退。

    只還原 opencode.json 而留著新的狀態檔,ownership 紀錄就會描述一份已經
    不存在的接管 —— 下一次切 native 會把錯的值寫回去。
    """
    models = _setup(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    first = cm.load_state(path=_state_path(tmp_path))
    assert first["mode"] == "codetrail"

    assert run(tmp_path, *base, "--compaction-mode", "native").returncode == 0
    assert cm.load_state(path=_state_path(tmp_path))["mode"] == "native"

    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    restored = cm.load_state(path=_state_path(tmp_path))
    assert restored == first
    assert _opencode(tmp_path)["compaction"]["auto"] is False


@pytest.mark.smoke
def test_the_plugin_accepts_exactly_what_set_config_wrote(tmp_path):
    """set_config 寫出來的狀態檔,plugin 必須真的採信。

    這是兩份實作唯一會對不起來的地方:狀態檔路徑、目標 config 身分雜湊、
    digest、以及檔案權限,四樣任一不同都會讓 plugin **靜默** 停用自動壓縮 ——
    使用者只會覺得「壓縮怎麼沒發生」,而兩邊各自的單元測試都是綠的。
    """
    runtime = shutil.which("node") or shutil.which("bun")
    if runtime is None:
        pytest.skip("需要 node 或 bun")
    models = _setup(tmp_path)
    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode == 0, proc.stderr + proc.stdout

    home = _home(tmp_path)
    plugin = tmp_path / "plugin.mjs"
    shutil.copyfile(cm.PLUGIN_PATH, plugin)
    script = tmp_path / "accepts.mjs"
    script.write_text(
        "import { CodetrailCompaction } from './plugin.mjs';\n"
        "import { readFileSync } from 'node:fs';\n"
        "const I = CodetrailCompaction.internals;\n"
        "const config = JSON.parse(readFileSync(process.argv[2], 'utf8'));\n"
        "const state = await I.readModeState(process.env.HOME, process.env);\n"
        "process.stdout.write(JSON.stringify({\n"
        "  accepted: state !== null,\n"
        "  mode: state && state.mode,\n"
        "  drift: state ? I.effectiveDrift(config, state) : ['no state'],\n"
        "}));\n",
        encoding="utf-8",
    )
    env = {**os.environ, "HOME": str(home)}
    env.pop("OPENCODE_CONFIG", None)
    env.pop("XDG_STATE_HOME", None)
    completed = subprocess.run(
        [runtime, str(script),
         str(home / ".config" / "opencode" / "opencode.json")],
        cwd=tmp_path, capture_output=True, text=True, timeout=90, env=env,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {"accepted": True, "mode": "codetrail", "drift": []}


@pytest.mark.smoke
def test_a_symlinked_state_file_is_refused(tmp_path):
    """狀態檔決定「切回 native 要把什麼寫回設定」;跟著連結過去就是把寫入導到別的檔。"""
    models = _setup(tmp_path)
    home = _home(tmp_path)
    state_dir = home / ".config" / "codetrail"
    state_dir.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "victim.json"
    victim.write_text("untouched", encoding="utf-8")
    (state_dir / "compaction.json").symlink_to(victim)

    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode != 0
    assert "symlink" in proc.stderr
    assert victim.read_text(encoding="utf-8") == "untouched"
    # 整批中止:opencode.json 也不得被寫入
    assert not (home / ".config" / "opencode" / "opencode.json").exists()


@pytest.mark.smoke
def test_a_symlinked_state_directory_is_refused(tmp_path):
    models = _setup(tmp_path)
    home = _home(tmp_path)
    (home / ".config").mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / ".config" / "codetrail").symlink_to(elsewhere, target_is_directory=True)

    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode != 0
    assert "symlink" in proc.stderr
    assert list(elsewhere.iterdir()) == []


@pytest.mark.smoke
def test_the_state_directory_ends_up_owner_only(tmp_path):
    """umask 022 的全新安裝會建出 0755;讀取端一律拒絕對其他帳號開放的目錄。"""
    models = _setup(tmp_path)
    home = _home(tmp_path)
    state_dir = home / ".config" / "codetrail"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_dir.chmod(0o755)

    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert "700" in proc.stdout
    assert cm.load_state(path=_state_path(tmp_path)) is not None


@pytest.mark.smoke
def test_a_project_scoped_config_never_gets_the_plugin_path(tmp_path):
    """<project>/.opencode/opencode.json 可能被 commit 進客戶 repo。"""
    models = _setup(tmp_path)
    project = tmp_path / "customer-repo"
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    config_path = project / ".opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}", encoding="utf-8")

    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail",
               env_overrides={"OPENCODE_CONFIG": str(config_path)})
    assert proc.returncode != 0
    assert "專案內的設定" in proc.stderr
    assert str(cm.PLUGIN_PATH) not in config_path.read_text(encoding="utf-8")


@pytest.mark.smoke
def test_restore_reports_failure_when_a_backup_is_missing(tmp_path):
    """只還原一半就回報成功,會讓 ownership 紀錄與 config 描述不同世代。"""
    models = _setup(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    assert run(tmp_path, *base, "--compaction-mode", "native").returncode == 0

    manifest = json.loads(
        (_home(tmp_path) / ".config" / "codetrail"
         / "setconfig-last-transaction.json").read_text(encoding="utf-8")
    )
    backup = next(
        info["backup"] for path, info in manifest["targets"].items()
        if path.endswith("compaction.json") and info.get("backup")
    )
    Path(backup).unlink()

    before = {
        path: Path(path).read_bytes()
        for path in manifest["targets"]
        if Path(path).is_file()
    }
    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 2
    assert "已中止" in proc.stderr
    # 中止就是中止:一個檔案都不能先被改掉,否則不同檔案會停在不同世代
    for path, content in before.items():
        assert Path(path).read_bytes() == content, path
    assert cm.load_state(path=_state_path(tmp_path))["mode"] == "native"


@pytest.mark.smoke
def test_restore_still_works_for_a_symlinked_opencode_config(tmp_path):
    """`opencode.json` 是 dotfiles symlink 是被支援的設定,還原不得因此中止。"""
    models = _setup(tmp_path)
    home = _home(tmp_path)
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "opencode.json"
    real.write_text(json.dumps({"compaction": {"auto": True}}), encoding="utf-8")
    (home / ".config" / "opencode").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "opencode" / "opencode.json").symlink_to(real)

    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    assert json.loads(real.read_text(encoding="utf-8"))["compaction"]["auto"] is False

    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert json.loads(real.read_text(encoding="utf-8"))["compaction"]["auto"] is True
    assert (home / ".config" / "opencode" / "opencode.json").is_symlink()


@pytest.mark.smoke
def test_restore_never_deletes_a_live_file_when_the_manifest_has_no_backup(tmp_path):
    """`{"existed": true, "backup": null}` 落到「移除」分支就是資料遺失,不是還原。"""
    models = _setup(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    assert run(tmp_path, *base, "--compaction-mode", "native").returncode == 0

    manifest_path = (_home(tmp_path) / ".config" / "codetrail"
                     / "setconfig-last-transaction.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    state_key = next(k for k in manifest["targets"] if k.endswith("compaction.json"))
    manifest["targets"][state_key] = {"existed": True, "backup": None}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 2
    assert "沒有備份路徑" in proc.stderr
    assert _state_path(tmp_path).is_file()               # live 檔案沒有被刪掉


@pytest.mark.smoke
def test_a_corrupt_manifest_does_not_fall_back_to_per_file_backups(tmp_path):
    """逐檔最新備份會混合不同 transaction 的產物,還原出一組拼裝設定。"""
    models = _setup(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    manifest_path = (_home(tmp_path) / ".config" / "codetrail"
                     / "setconfig-last-transaction.json")
    manifest_path.write_text("{ not json", encoding="utf-8")

    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 2
    assert "不退回逐檔模式" in proc.stderr


@pytest.mark.smoke
def test_restore_puts_the_state_file_back_owner_only(tmp_path):
    """還原出一份 plugin 必定拒絕的 state,等於還原完就不接管了。"""
    models = _setup(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    assert run(tmp_path, *base, "--compaction-mode", "native").returncode == 0
    # 備份的權限被放寬(例如從別處複製回來)
    manifest = json.loads(
        (_home(tmp_path) / ".config" / "codetrail"
         / "setconfig-last-transaction.json").read_text(encoding="utf-8")
    )
    backup = next(
        info["backup"] for path, info in manifest["targets"].items()
        if path.endswith("compaction.json") and info.get("backup")
    )
    Path(backup).chmod(0o644)

    assert run(tmp_path, "--restore-last-backup").returncode == 0
    assert stat.S_IMODE(_state_path(tmp_path).stat().st_mode) == 0o600
    assert cm.load_state(path=_state_path(tmp_path))["mode"] == "codetrail"


@pytest.mark.smoke
def test_restore_refuses_when_a_symlinked_config_was_repointed(tmp_path):
    """設定時 link 指向 A、還原前被改指 B:跟著現在的 link 走會拿 A 的舊內容蓋 B。"""
    models = _setup(tmp_path)
    home = _home(tmp_path)
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    a = dotfiles / "a.json"
    b = dotfiles / "b.json"
    a.write_text(json.dumps({"compaction": {"auto": True}}), encoding="utf-8")
    b.write_text(json.dumps({"marker": "B"}), encoding="utf-8")
    link = home / ".config" / "opencode" / "opencode.json"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(a)

    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    link.unlink()
    link.symlink_to(b)

    proc = run(tmp_path, "--restore-last-backup")
    assert proc.returncode == 2
    assert "與設定當時的" in proc.stderr
    assert json.loads(b.read_text(encoding="utf-8")) == {"marker": "B"}


@pytest.mark.smoke
def test_a_manifest_that_cannot_be_written_does_not_survive_stale(tmp_path):
    """舊 manifest 留著比沒有更糟:restore 會照上一次 transaction 還原。"""
    models = _setup(tmp_path)
    base = (*YES_TWO_GPU, "--no-preview", "--models-dir", str(models))
    assert run(tmp_path, *base, "--compaction-mode", "codetrail").returncode == 0
    manifest = (_home(tmp_path) / ".config" / "codetrail"
                / "setconfig-last-transaction.json")
    first = manifest.read_text(encoding="utf-8")
    manifest.chmod(0o444)
    try:
        proc = run(tmp_path, *base, "--compaction-mode", "native")
        assert proc.returncode == 0, proc.stderr + proc.stdout
        assert not manifest.exists() or manifest.read_text(encoding="utf-8") != first
    finally:
        if manifest.exists():
            manifest.chmod(0o644)


@pytest.mark.smoke
def test_a_compaction_agent_model_without_limits_is_fail_loud(tmp_path):
    """設了 agent.compaction.model 卻查不到它的 limit 時,靜默用主模型公式的話,
    設定寫完的第一個 idle 就會被 runtime 判成 config_drift。
    """
    models = _setup(tmp_path)
    opencode = _home(tmp_path) / ".config" / "opencode" / "opencode.json"
    opencode.parent.mkdir(parents=True, exist_ok=True)
    opencode.write_text(
        json.dumps({"agent": {"compaction": {"model": "llamacpp/other"}}}),
        encoding="utf-8",
    )
    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode != 0
    assert "agent.compaction.model" in proc.stderr
    assert not _state_path(tmp_path).exists()


@pytest.mark.smoke
def test_the_writer_derives_from_the_compaction_agent_model(tmp_path):
    """受管值必須用 compaction agent 實際會用的模型推導。"""
    models = _setup(tmp_path)
    opencode = _home(tmp_path) / ".config" / "opencode" / "opencode.json"
    opencode.parent.mkdir(parents=True, exist_ok=True)
    opencode.write_text(
        json.dumps({
            "agent": {"compaction": {"model": "llamacpp/small"}},
            "provider": {"llamacpp": {"models": {
                "small": {"limit": {"context": 32768, "output": 8192}},
            }}},
        }),
        encoding="utf-8",
    )
    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    expected = cm.derive_settings(context_limit=32768, output_limit=8192)
    assert _opencode(tmp_path)["compaction"] == expected.config_values
    assert "agent.compaction.model" in proc.stdout


@pytest.mark.smoke
def test_a_bigger_compaction_model_does_not_raise_the_main_model_threshold(tmp_path):
    """摘要模型 context 比主模型大時,受管值仍要受主模型限制。

    只按摘要模型推導的話,門檻會高過主模型裝得下的量:觸發之前那整段對話壓
    的是主模型,而 `compaction.auto=false` 已經把上游的 overflow 自動回復關掉,
    使用者拿到的是 context error。
    """
    models = _setup(tmp_path)
    opencode = _home(tmp_path) / ".config" / "opencode" / "opencode.json"
    opencode.parent.mkdir(parents=True, exist_ok=True)
    opencode.write_text(
        json.dumps({
            "agent": {"compaction": {"model": "llamacpp/huge"}},
            "provider": {"llamacpp": {"models": {
                "huge": {"limit": {"context": 1048576, "output": 8192}},
            }}},
        }),
        encoding="utf-8",
    )
    proc = run(tmp_path, *YES_TWO_GPU, "--no-preview", "--models-dir", str(models),
               "--compaction-mode", "codetrail")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    config = _opencode(tmp_path)
    main_ref = config["model"]
    provider_id, model_id = main_ref.split("/", 1)
    main_limit = config["provider"][provider_id]["models"][model_id]["limit"]
    summariser = cm.derive_settings(context_limit=1048576, output_limit=8192)
    live = cm.derive_settings(
        context_limit=main_limit["context"], output_limit=main_limit["output"]
    )
    expected = cm.combine_settings(summariser, live)
    assert config["compaction"] == expected.config_values
    # 主模型真的把它壓下來了(不然這條測試證明不了任何事)。
    assert (
        expected.preserve_recent_tokens < summariser.preserve_recent_tokens
    ), (expected.preserve_recent_tokens, summariser.preserve_recent_tokens)
