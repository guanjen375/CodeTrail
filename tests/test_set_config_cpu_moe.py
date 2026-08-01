"""set_config 主模型 CPU-MoE 分流與容量 gate 的離線回歸測試。"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from deployment_profile import ProfileError, build_server_command, load_effective_profile
from scripts import set_config as sc


def _write_moe_gguf(path: Path, *, expert_bytes: int, dense_bytes: int) -> None:
    """建立只含 tensor table 的 sparse GGUF；不配置 GiB 級實體磁碟內容。"""
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


def _build_plan(tmp_path: Path, *, cpu_moe: bool) -> sc.Plan:
    main_path = tmp_path / "main-moe.gguf"
    _write_moe_gguf(main_path, expert_bytes=8 * sc.GIB, dense_bytes=1 * sc.GIB)
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
        cpu_moe=cpu_moe,
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


def test_cpu_moe_mode_splits_gpu_and_ram_budget_and_generates_flag(tmp_path, monkeypatch):
    plan = _build_plan(tmp_path, cpu_moe=True)
    monkeypatch.setattr(sc, "_mem_info_mib", lambda: (64 * 1024, 60 * 1024))

    fits_whole, fit_target = sc.plan_capacity(plan, True, 5120, False, plan.notes)
    parameters, batch, ubatch = sc.recommend_main(
        plan.main.gpu,
        plan.main.candidate,
        plan.ctx,
        plan.threads,
        fit_target,
        True,
        fits_whole,
        True,
        plan.notes,
    )

    assert fits_whole is False
    assert parameters["cpu_moe"] is True
    assert parameters["gpu_layers"] == 99
    assert parameters["fit"] == "off"
    assert (batch, ubatch) == (1024, 256)
    assert any("experts" in note and "dense" in note for note in plan.notes)


def test_general_mode_refuses_oversized_moe_even_when_fit_exists(tmp_path, monkeypatch):
    plan = _build_plan(tmp_path, cpu_moe=False)
    monkeypatch.setattr(sc, "_mem_info_mib", lambda: (416 * 1024, 400 * 1024))

    with pytest.raises(sc.SetupError, match="一般模式.*CPU-MoE"):
        sc.plan_capacity(plan, True, 5120, False, plan.notes)


def test_cpu_moe_mode_refuses_insufficient_system_ram(tmp_path, monkeypatch):
    plan = _build_plan(tmp_path, cpu_moe=True)
    # 保留 32 GiB 後只剩 4 GiB，不足以容納約 8.8 GiB expert resident。
    monkeypatch.setattr(sc, "_mem_info_mib", lambda: (36 * 1024, 36 * 1024))

    with pytest.raises(sc.SetupError, match="CPU-MoE expert resident.*RAM"):
        sc.plan_capacity(plan, True, 5120, False, plan.notes)


def test_mode_prompt_defaults_to_recommendation_and_only_asks_once(tmp_path, monkeypatch):
    plan = _build_plan(tmp_path, cpu_moe=False)
    prompts: list[str] = []

    def accept_default(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.setattr(sc, "_input", accept_default)
    selected = sc.choose_cpu_moe_mode(
        None, False, True, plan.main.candidate, plan.main_layout
    )

    assert selected is True
    assert len(prompts) == 1
    assert "--cpu-moe" in prompts[0]


def test_scan_prefers_verified_235b_over_larger_main_candidate(tmp_path):
    models = tmp_path / "models"
    verified_dir = models / "Qwen3-235B-A22B-Thinking-2507-GGUF"
    larger_dir = models / "GLM-5.2-GGUF"
    verified_dir.mkdir(parents=True)
    larger_dir.mkdir(parents=True)
    verified = verified_dir / "Qwen3-235B-A22B-Thinking-2507-UD-Q4_K_XL.gguf"
    larger = larger_dir / "GLM-5.2-UD-Q4_K_XL.gguf"
    verified.write_bytes(b"verified")
    larger.write_bytes(b"larger candidate")

    candidates, broken = sc.scan_models(models)

    assert not broken
    assert candidates["main"][0].path == verified
    assert candidates["main"][1].path == larger


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
