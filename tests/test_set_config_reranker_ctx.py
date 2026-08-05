"""set_config reranker ctx 選擇、說明與容量估算的離線回歸測試。"""
from pathlib import Path

from scripts import set_config as sc


def _candidate(tmp_path: Path, name: str, size_mib: int = 610) -> sc.ModelCandidate:
    path = tmp_path / name
    with path.open("wb") as handle:
        handle.truncate(size_mib * sc.MIB)
    return sc.ModelCandidate(path=path, total_bytes=path.stat().st_size, shards=1)


def test_qwen_ctx_default_and_explanation_are_accuracy_safe(tmp_path, monkeypatch, capsys):
    candidate = _candidate(tmp_path, "qwen3-reranker-0.6b-q8_0.gguf")
    prompts: list[str] = []

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.setattr(sc, "_input", fake_input)

    ctx = sc.choose_reranker_ctx(candidate, override=None, assume_yes=False)
    output = capsys.readouterr().out

    assert ctx == 2048
    assert prompts == ["reranker context(-c/-b/-ub)(Enter 用預設 2048): "]
    assert "ctx 設更大不會讓排序更準" in output
    assert "太小導致失敗/截斷時才會漏證據" in output
    assert "更吃顯存" in output
    assert "實際送入更多 token 時也會更慢" in output


def test_qwen_capacity_estimate_scales_with_selected_ctx(tmp_path):
    gpu = sc.Gpu(1, "Aux GPU", 16380, 15000, "GPU-aux")
    selection = sc.Selection(
        "reranker",
        _candidate(tmp_path, "qwen3-reranker-0.6b-q8_0.gguf"),
        gpu,
    )

    assert sc._aux_overhead_mib(selection, 2048) == 1536
    assert sc._aux_overhead_mib(selection, 4096) == 3072
    assert sc._aux_overhead_mib(selection, 8192) == 6144
    assert sc._aux_need_mib(selection, 2048) < sc._aux_need_mib(selection, 8192)


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
    assert service["parameters"] == {"parallel": 1}
