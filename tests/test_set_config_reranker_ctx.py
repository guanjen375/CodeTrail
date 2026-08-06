"""set_config reranker ctx 問答與寫入的離線回歸測試(無預設值、只驗證範圍)。"""
from pathlib import Path

from scripts import set_config as sc


def _candidate(tmp_path: Path, name: str, size_mib: int = 610) -> sc.ModelCandidate:
    path = tmp_path / name
    with path.open("wb") as handle:
        handle.truncate(size_mib * sc.MIB)
    return sc.ModelCandidate(path=path, total_bytes=path.stat().st_size, shards=1)


def test_reranker_ctx_question_has_no_default_and_validates_range(
    tmp_path, monkeypatch, capsys
):
    candidate = _candidate(tmp_path, "qwen3-reranker-0.6b-q8_0.gguf")
    prompts: list[str] = []
    answers = iter(["", "1", "2048"])  # Enter 與低於下限都不被接受

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(sc, "_input", fake_input)

    ctx = sc.choose_reranker_ctx(candidate, override=None, assume_yes=False)
    output = capsys.readouterr().out

    assert ctx == 2048
    assert len(prompts) == 3
    assert all(
        prompt == (
            f"reranker context(-c/-b/-ub)({sc.MIN_RERANKER_CTX}.."
            f"{sc.MAX_RERANKER_CTX}): "
        )
        for prompt in prompts
    )
    # 中性的機制說明仍在;不再有模型別建議值
    assert "ctx 設更大不會讓排序更準" in output
    assert "太小導致失敗/截斷時才會漏證據" in output
    assert "更吃顯存" in output
    assert "實際送入更多 token 時也會更慢" in output
    assert "建議" not in output
    assert f"請輸入 {sc.MIN_RERANKER_CTX}..{sc.MAX_RERANKER_CTX} 的整數" in output


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

    # --yes 沒給旗標 → 指名 --rerank-ctx 報錯,不用任何預設值
    try:
        sc.choose_reranker_ctx(candidate, override=None, assume_yes=True)
    except sc.SetupError as exc:
        assert "--rerank-ctx" in str(exc)
    else:
        raise AssertionError("--yes 缺 --rerank-ctx 必須報錯")


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
