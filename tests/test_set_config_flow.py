"""set_config 的問答流程與旗標契約:使用者題沒有預設值、範圍驗證、preflight 失敗訊息。

從 tests/test_set_config.py 拆出(2026-08-20)。原檔 69 條 10.43s 是全套件第二慢,
拆成 flow / artifacts / models 三塊,並把呼叫層換成 in-process(見 _set_config_harness)。
assertion 內容未變。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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
    write_fake_llama,
    write_fake_nvidia_smi,
)


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
        "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1",
        *NUM_FLAGS, "--models-dir", str(models),
    )
    assert no_vl_gpu.returncode == 2
    assert "--vl-gpu" in no_vl_gpu.stderr

    # 數值也沒有預設:缺 --ctx 就報錯
    no_ctx = run(
        tmp_path, "--yes", "--main-model", "1", "--rerank-model", "1",
        "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1", "--vl-gpu", "1",
        "--rerank-ctx", "8192", "--models-dir", str(models),
    )
    assert no_ctx.returncode == 2
    assert "--ctx" in no_ctx.stderr

    # reranker internal buffer 現在也是使用者題 → 缺 --rerank-ctx 一樣報錯
    no_rerank_ctx = run(
        tmp_path, "--yes", "--main-model", "1", "--rerank-model", "1",
        "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1", "--vl-gpu", "1",
        "--ctx", "65536", "--models-dir", str(models),
    )
    assert no_rerank_ctx.returncode == 2
    assert "--rerank-ctx" in no_rerank_ctx.stderr

def test_interactive_flow_answers_everything_and_validates_ranges(tmp_path):
    """使用者選擇題沒有預設值(Enter 不可過關)、選項外輸入會重問。"""
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)

    # main 先按 Enter(無效)再輸入 3(超出 1-2,無效)才輸入 1;
    # main GPU 先輸入 5(不存在)再輸入 0;其餘照標準作答。
    stdin = "\n3\n1\n5\n0\n65536\n1\n1\n1\n8192\n1\n\n"
    proc = run(tmp_path, "--no-preview", "--models-dir", str(models), stdin=stdin)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "【主聊天模型】 — 偵測到的候選" in proc.stdout
    assert "編號只有 1-2" in proc.stdout           # 選項 1/2 輸入 3 → 重問
    assert "無效的 GPU index" in proc.stdout       # GPU 0/1 輸入 5 → 重問
    assert "只有一個候選,自動選用" in proc.stdout  # embedding / VL 唯一候選
    assert "設定摘要" in proc.stdout
    assert "(預設)" not in proc.stdout             # 不再有任何預設標記
    assert "建議配置" not in proc.stdout           # 不再有建議配置頁
    # 一個角色問完才換下一個(四段標題依序出現)
    for step, title in ((1, "主聊天模型"), (2, "embedding 模型"),
                        (3, "reranker 模型"), (4, "VL 模型")):
        assert f"=== [{step}/4] {title} ===" in proc.stdout
    assert (
        proc.stdout.index("=== [1/4]") < proc.stdout.index("=== [2/4]")
        < proc.stdout.index("=== [3/4]") < proc.stdout.index("=== [4/4]")
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
        ["bash", str(SCRIPT), "--skip-deps-check", "--no-preview", "--models-dir", str(models)],
        cwd=REPO_ROOT,
        env={**build_env(tmp_path), "HOME": str(home2), "USERPROFILE": str(home2)},
        # 全部答完,摘要頁按 q → 不寫入
        input="1\n0\n65536\n1\n1\n1\n8192\n1\nq\n",
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
        stdin="1\n0\n65536\n1\n1\n1\n8192\n1\nzz\nq\n",
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
        "--main-gpu", "1", "--embed-gpu", "0", "--rerank-gpu", "1", "--vl-gpu", "1",
        *NUM_FLAGS,
    )
    assert proc.returncode == 0, proc.stderr
    content = (tmp_path / "home" / "start.sh").read_text(encoding="utf-8")
    assert "export MAIN_GPU=GPU-bbbb-2000" in content
    # aux 三顆不同卡(embed=0、rerank/vl=1)→ 逐 role export
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

def test_llama_without_reranking_support_fails(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    write_fake_llama(tmp_path, help_flags="--fit --mmproj --cache-ram")  # 沒有 --reranking
    proc = run(tmp_path, "--yes", "--models-dir", str(models))
    assert proc.returncode == 2
    assert "--reranking" in proc.stderr
    assert "更新並重新 build" in proc.stderr

def test_cpu_moe_mode_requires_llama_cpu_moe_flag(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    write_fake_llama(tmp_path, help_flags="--fit --reranking --mmproj --cache-ram")

    # big-chat 假檔不是 GGUF → layout 無法解析;--cpu-moe 仍尊重旗標,
    # 但 build 不支援就要硬停。
    proc = run(
        tmp_path, "--yes", "--cpu-moe", "--no-preview", "--models-dir", str(models),
        "--main-model", "1", "--main-gpu", "0", "--ctx", "65536",
    )

    assert proc.returncode == 2
    assert "需要 llama-server 的 --cpu-moe" in proc.stderr
    assert "重新 build" in proc.stderr

def test_generated_vl_safety_requires_llama_fit_flag(tmp_path):
    write_fake_nvidia_smi(tmp_path / "bin", TWO_GPUS)
    models = make_models(tmp_path)
    write_fake_llama(tmp_path, help_flags="--cpu-moe --reranking --mmproj --cache-ram")

    proc = run(
        tmp_path, "--yes", "--no-cpu-moe", "--no-preview", "--models-dir", str(models)
    )

    assert proc.returncode == 2
    assert "安全的 VL placement 需要 llama-server --fit" in proc.stderr
    assert "重新 build" in proc.stderr

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
                stdin="1\n0\n9999999\n65536\n1\n1\n1\n8192\n1\n\n")
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
        "--main-gpu", "0", "--embed-gpu", "1", "--rerank-gpu", "1", "--vl-gpu", "1",
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
    content = (tmp_path / "home" / "start.sh").read_text(encoding="utf-8")
    assert "export MAIN_GPU=GPU-solo" in content
    assert "export AUX_GPU=GPU-solo" in content

def test_dry_run_writes_nothing(tmp_path):
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
    )

    service = sc.build_deployment_config(plan)["services"]["reranker"]

    assert service["ctx"] == 2048
    assert service["batch"] == 2048
    assert service["ubatch"] == 2048
    assert service["parameters"] == {"parallel": 1, "cache_ram": 0}
