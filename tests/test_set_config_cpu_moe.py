"""set_config CPU-MoE 的離線回歸測試(契約:無容量估算、無預設值、只問層數)。

涵蓋:GGUF expert tensor 解析(含 per-layer 編號與 split shard)、
choose_cpu_moe_layers 的純問答行為(0 = 不 offload、N = 前 N 層、
≥ 層數上限 = 全部)、build_main_parameters 的參數組裝,以及 deployment
profile 對 cpu_moe / n_cpu_moe 的 role 約束(main 與 vl 可用,embedding/
reranker 不可)。
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

    # VL 也可以有 CPU-MoE:--fit on 與 --n-cpu-moe 並存(llama.cpp 會把
    # expert override 算進 fit),embedding/reranker 則仍被 schema 拒絕。
    plan.vl_n_cpu_moe = 4
    local_path.write_text(json.dumps(sc.build_deployment_config(plan)), encoding="utf-8")
    vl_profile = load_effective_profile(env)
    vl_command = build_server_command(vl_profile.service("vl"), "/opt/llama-server", env)
    assert vl_command[vl_command.index("--n-cpu-moe") + 1] == "4"
    assert vl_command[vl_command.index("--fit") + 1] == "on"
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
