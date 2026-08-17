"""Reranker internal buffer is a normal (mandatory) question with no default."""
from pathlib import Path

import pytest

from scripts import set_config as sc


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
    assert service["parameters"] == {"parallel": 1}
