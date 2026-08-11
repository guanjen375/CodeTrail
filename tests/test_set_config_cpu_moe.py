"""set_config 主模型 CPU-MoE 分流的離線回歸測試(新契約:無容量估算、無預設值)。

涵蓋:GGUF expert tensor 解析(含 per-layer 編號與 split shard)、
choose_cpu_moe_mode / choose_n_cpu_moe 的純問答行為、build_main_parameters
的參數組裝,以及 deployment profile 對 cpu_moe / n_cpu_moe 的 main-only 約束。
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from deployment_profile import ProfileError, build_server_command, load_effective_profile
from scripts import set_config as sc


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


def test_choose_cpu_moe_mode_is_pure_question(tmp_path, monkeypatch):
    """模式問題沒有預設答案:必須明確 y/n,亂打會重問;dense/解析失敗不詢問。"""
    plan = _build_plan(tmp_path, cpu_moe=False)
    layout = plan.main_layout
    candidate = plan.main.candidate

    prompts: list[str] = []
    answers = iter(["", "x", "y"])

    def scripted(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(sc, "_input", scripted)
    notes: list[str] = []
    selected = sc.choose_cpu_moe_mode(None, False, candidate, layout, True, notes)
    assert selected is True
    assert len(prompts) == 3          # Enter 與亂打都不被接受
    assert "[y/n]" in prompts[0]
    assert not notes

    # 旗標 override 直接生效,不提問
    monkeypatch.setattr(sc, "_input", lambda prompt: pytest.fail("不應詢問"))
    assert sc.choose_cpu_moe_mode(True, False, candidate, layout, True, []) is True
    assert sc.choose_cpu_moe_mode(False, False, candidate, layout, True, []) is False

    # dense 模型:不詢問,直接一般模式
    dense = sc.ModelLayout(tensor_bytes=1 * sc.GIB, expert_bytes=0)
    assert sc.choose_cpu_moe_mode(None, False, candidate, dense, True, []) is False

    # layout 解析失敗:不詢問,一般模式 + 提示
    none_notes: list[str] = []
    assert sc.choose_cpu_moe_mode(None, False, candidate, None, True, none_notes) is False
    assert any("無法讀取" in note for note in none_notes)

    # build 不支援 --cpu-moe:不詢問,一般模式 + 提示
    unsupported_notes: list[str] = []
    assert sc.choose_cpu_moe_mode(
        None, False, candidate, layout, False, unsupported_notes
    ) is False
    assert any("不支援 --cpu-moe" in note for note in unsupported_notes)

    # --yes 且是 MoE:必須用旗標指定模式
    with pytest.raises(sc.SetupError, match="--cpu-moe / --no-cpu-moe / --n-cpu-moe"):
        sc.choose_cpu_moe_mode(None, True, candidate, layout, True, [])


def test_choose_n_cpu_moe_validates_range_without_recommendation(monkeypatch):
    layout = sc.ModelLayout(
        tensor_bytes=9 * sc.GIB,
        expert_bytes=8 * sc.GIB,
        expert_layer_bytes=tuple((index, 1 * sc.GIB) for index in range(8)),
    )

    # 旗標覆寫:照用;超過最大 block 編號 → None(等同 --cpu-moe)。
    assert sc.choose_n_cpu_moe(3, True, layout) == 3
    assert sc.choose_n_cpu_moe(99, True, layout) is None

    # 互動:沒有預設值(Enter 無效)、只驗證 0..1024 範圍;
    # 輸入超過最大 blk 編號(7)→ None。
    answers = iter(["", "4", "8", "2000", "0"])
    prompts: list[str] = []

    def scripted(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(sc, "_input", scripted)
    assert sc.choose_n_cpu_moe(None, False, layout) == 4     # "" 重問後輸入 4
    assert sc.choose_n_cpu_moe(None, False, layout) is None  # 8 > blk 上限 7
    assert sc.choose_n_cpu_moe(None, False, layout) == 0     # 2000 超出 0..1024 重問
    assert all(f"(0..{sc.MAX_N_CPU_MOE})" in prompt for prompt in prompts)


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


def test_profile_emits_cpu_moe_only_for_main_and_rejects_partial_mix(tmp_path):
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

    bad = sc.build_deployment_config(plan)
    bad["services"]["main"]["parameters"]["n_cpu_moe"] = 90
    local_path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ProfileError, match="mutually exclusive"):
        load_effective_profile(env)
