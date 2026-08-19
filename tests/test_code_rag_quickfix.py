#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Code RAG 快修包(§5)驗收測試:rerank query-local、cache 世代、TTL 快照、
batch embedding、掃描層副檔名、context end_line。全部離線。"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import code_rag  # noqa: E402
import config  # noqa: E402
import fs_safety  # noqa: E402
import llama_client  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_scan_cache():
    code_rag._INDEX_SCAN_CACHE.clear()
    yield
    code_rag._INDEX_SCAN_CACHE.clear()


def _make_repo(tmp_path: Path, n_files: int = 2, funcs_per_file: int = 2) -> Path:
    for i in range(n_files):
        body = "".join(
            f"def func_{i}_{j}():\n    return {j}\n\n" for j in range(funcs_per_file)
        )
        (tmp_path / f"mod_{i}.py").write_text(body, encoding="utf-8")
    return tmp_path


def _offline_rag(monkeypatch, root: Path) -> code_rag.CodeRAG:
    rag = code_rag.CodeRAG(str(root))
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", False)
    monkeypatch.setattr(rag, "_get_embedding", lambda _text: [1.0, 0.0])
    monkeypatch.setattr(rag, "_embed_texts_batched",
                        lambda texts: [[1.0, 0.0]] * len(texts))
    return rag


# ============================================================
# §5-1 rerank query-local
# ============================================================
def test_rerank_scores_are_mock_values_and_monotonic(monkeypatch, tmp_path):
    rag = _offline_rag(monkeypatch, _make_repo(tmp_path, n_files=3, funcs_per_file=3))
    monkeypatch.setattr(code_rag, "USE_RERANKER", True)
    monkeypatch.setattr(rag, "_check_reranker_available", lambda: True)
    monkeypatch.setattr(rag, "_should_rerank", lambda candidates, top_k: True)

    # 亂序 mock 分數:最後一個 passage 拿最高分 → 排序必須反映 mock 值
    def fake_rerank(*, base_url, query, documents, model="", timeout=60):
        return [0.1 * (i + 1) for i in range(len(documents))]

    monkeypatch.setattr(code_rag.llama_client, "rerank", fake_rerank)

    results = rag.query("func", top_k=3)
    assert results, "rerank 路徑必須有結果"
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True), "回傳 score 必須單調遞減"
    # 分數必須是 mock 的 rerank 值(round 3),不是 fusion combined
    fake_scores = {round(0.1 * (i + 1), 3) for i in range(20)}
    assert all(s in fake_scores for s in scores), f"score 不是 mock rerank 值: {scores}"


def test_consecutive_queries_leave_no_residue(monkeypatch, tmp_path):
    rag = _offline_rag(monkeypatch, _make_repo(tmp_path, n_files=3, funcs_per_file=3))
    monkeypatch.setattr(code_rag, "USE_RERANKER", True)
    monkeypatch.setattr(rag, "_check_reranker_available", lambda: True)
    monkeypatch.setattr(rag, "_should_rerank", lambda candidates, top_k: True)
    monkeypatch.setattr(
        code_rag.llama_client, "rerank",
        lambda *, base_url, query, documents, model="", timeout=60: [9.0] * len(documents),
    )
    keys_before = {id(item): set(item) for item in rag_index_snapshot(rag)}
    first = rag.query("func", top_k=3)
    assert first and first[0]["score"] == 9.0

    # 第二次 query 不 rerank → fusion 分數;第一輪的 9.0 不得殘留
    monkeypatch.setattr(rag, "_should_rerank", lambda candidates, top_k: False)
    second = rag.query("func", top_k=3)
    assert second and all(r["score"] != 9.0 for r in second), "rerank 分數殘留到下一個 query"

    # 嚴禁把 query 分數寫進持久 item:index items 的 key 集合不得改變
    for item in rag_index_snapshot(rag):
        assert not any("rerank" in k for k in item), f"index item 被寫入 rerank 欄位: {sorted(item)}"


def rag_index_snapshot(rag):
    return rag.index


def test_cache_file_has_no_rerank_fields(monkeypatch, tmp_path):
    rag = _offline_rag(monkeypatch, _make_repo(tmp_path))
    monkeypatch.setattr(code_rag, "USE_RERANKER", True)
    monkeypatch.setattr(rag, "_check_reranker_available", lambda: True)
    monkeypatch.setattr(rag, "_should_rerank", lambda candidates, top_k: True)
    monkeypatch.setattr(
        code_rag.llama_client, "rerank",
        lambda *, base_url, query, documents, model="", timeout=60: [1.0] * len(documents),
    )
    rag.build_index(verbose=False)
    rag.query("func", top_k=2)
    rag._save_cache()

    raw = rag.cache_meta_file.read_text(encoding="utf-8")
    assert "rerank" not in raw, "cache 檔不得出現任何 rerank 欄位(query-local 契約)"


# ============================================================
# §5-2 cache 世代一致性 / fail-loud
# ============================================================
def test_torn_generation_is_detected_and_rebuilt(monkeypatch, tmp_path, capsys):
    rag = _offline_rag(monkeypatch, _make_repo(tmp_path))
    rag.build_index(verbose=False)
    assert rag.cache_emb_file.exists()

    # 模擬 kill 於「NPZ 已替換、meta 未替換」:NPZ 內容與 meta.npz_md5 不符
    with open(rag.cache_emb_file, "ab") as f:
        f.write(b"TORN")

    fresh = code_rag.CodeRAG(str(tmp_path))
    assert fresh._load_file_cache() == {}
    assert "md5 不符" in capsys.readouterr().err


def test_row_count_mismatch_is_detected(monkeypatch, tmp_path, capsys):
    rag = _offline_rag(monkeypatch, _make_repo(tmp_path))
    rag.build_index(verbose=False)

    meta = json.loads(rag.cache_meta_file.read_text(encoding="utf-8"))
    meta["row_count"] = meta["row_count"] + 1
    rag.cache_meta_file.write_text(json.dumps(meta), encoding="utf-8")

    fresh = code_rag.CodeRAG(str(tmp_path))
    assert fresh._load_file_cache() == {}
    assert "row_count" in capsys.readouterr().err


def test_corrupt_meta_json_logs_and_rebuilds(monkeypatch, tmp_path, capsys):
    rag = _offline_rag(monkeypatch, _make_repo(tmp_path))
    rag.build_index(verbose=False)
    rag.cache_meta_file.write_text("{not json", encoding="utf-8")

    fresh = code_rag.CodeRAG(str(tmp_path))
    assert fresh._load_file_cache() == {}
    assert "損壞" in capsys.readouterr().err


def test_save_cache_failure_raises(monkeypatch, tmp_path):
    if not code_rag.HAS_NUMPY:
        pytest.skip("需要 numpy")
    rag = _offline_rag(monkeypatch, _make_repo(tmp_path))
    rag.build_index(verbose=False)

    def broken_savez(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(code_rag.np, "savez_compressed", broken_savez)
    with pytest.raises(OSError, match="disk full"):
        rag._save_cache()


def test_cache_lock_symlink_is_rejected(monkeypatch, tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX symlink 防線")
    rag = _offline_rag(monkeypatch, _make_repo(tmp_path))
    victim = tmp_path / "victim.txt"
    victim.write_text("do not clobber", encoding="utf-8")
    os.symlink(victim, rag.cache_lock_file)

    rag.index = [{"path": "mod_0.py", "symbol": "s", "type": "function",
                  "line": 1, "context": "c"}]
    with pytest.raises(fs_safety.FsSafetyError):
        rag._save_cache()
    assert victim.read_text(encoding="utf-8") == "do not clobber"


# ============================================================
# §5-3 TTL snapshot + invalidation
# ============================================================
def test_ttl_second_query_does_zero_walk_and_zero_hash(monkeypatch, tmp_path):
    root = _make_repo(tmp_path)
    monkeypatch.setattr(config, "CODE_RAG_REFRESH_TTL_SECONDS", 30)

    walk_calls = {"n": 0}
    hash_calls = {"n": 0}
    real_walk = code_rag.walk_index_files
    real_hash = code_rag.compute_file_hash

    def counting_walk(scope):
        walk_calls["n"] += 1
        return real_walk(scope)

    def counting_hash(filepath, max_bytes=code_rag.CONTENT_HASH_MAX_BYTES):
        hash_calls["n"] += 1
        return real_hash(filepath, max_bytes)

    monkeypatch.setattr(code_rag, "walk_index_files", counting_walk)
    monkeypatch.setattr(code_rag, "compute_file_hash", counting_hash)

    rag = _offline_rag(monkeypatch, root)
    rag.query("func", top_k=2)  # 首次:build(fresh 掃描)
    walk_after_build = walk_calls["n"]
    hash_after_build = hash_calls["n"]
    assert walk_after_build > 0 and hash_after_build > 0

    rag.query("func", top_k=2)  # TTL 內第二次
    assert walk_calls["n"] == walk_after_build, "TTL 內第二次查詢必須零 walk"
    assert hash_calls["n"] == hash_after_build, "TTL 內第二次查詢必須零 compute_file_hash"


def test_invalidate_scan_cache_forces_fresh_scan(monkeypatch, tmp_path):
    root = _make_repo(tmp_path)
    monkeypatch.setattr(config, "CODE_RAG_REFRESH_TTL_SECONDS", 30)
    rag = _offline_rag(monkeypatch, root)
    rag.query("func", top_k=2)

    # TTL 內外部寫入 + 主動 invalidate → 下一次查詢必須看到新符號
    (root / "fresh.py").write_text("def brand_new_symbol():\n    return 7\n",
                                   encoding="utf-8")
    code_rag.invalidate_scan_cache(root)
    results = rag.query("brand_new_symbol", top_k=3)
    assert any(r["symbol"] == "brand_new_symbol" for r in results)


def test_invalidate_scan_cache_scopes_by_root(tmp_path):
    code_rag._INDEX_SCAN_CACHE[(str(tmp_path.resolve()), "fp")] = {
        "entries": {}, "timestamp": 0}
    code_rag._INDEX_SCAN_CACHE[("/somewhere/else", "fp")] = {
        "entries": {}, "timestamp": 0}
    code_rag.invalidate_scan_cache(tmp_path)
    assert (str(tmp_path.resolve()), "fp") not in code_rag._INDEX_SCAN_CACHE
    assert ("/somewhere/else", "fp") in code_rag._INDEX_SCAN_CACHE
    code_rag._INDEX_SCAN_CACHE.clear()


# ============================================================
# §5-3d MCP 寫入工具 finally invalidation(失敗路徑也要)
# ============================================================
@pytest.fixture
def mcp_module(monkeypatch, tmp_path: Path):
    pytest.importorskip("mcp", reason="mcp 套件未安裝")
    monkeypatch.setenv("AICODE_ROOT", str(tmp_path))
    monkeypatch.setenv("AICODE_MODEL", "example-code-model:30b")
    monkeypatch.setenv("AICODE_LLAMA_BASE_URL", "http://127.0.0.1:65535")
    monkeypatch.setenv("AICODE_REQUIRED_MODELS_CHECK_SKIP", "1")
    monkeypatch.setenv("AI_CODE_PATCH", "")
    monkeypatch.setenv("AI_CODE_RUN_TESTS", "")
    monkeypatch.setenv("AI_CODE_ENABLE_BUILD_COMMANDS", "")

    import config as _config
    monkeypatch.setattr(_config, "PATCH_ENABLED", _config.PATCH_ENABLED)
    monkeypatch.setattr(_config, "RUN_COMMAND_ENABLED", _config.RUN_COMMAND_ENABLED)
    monkeypatch.setattr(_config, "ALLOWED_COMMANDS", list(_config.ALLOWED_COMMANDS))

    sys.modules.pop("mcp_server", None)
    import mcp_server  # type: ignore

    importlib.reload(mcp_server)
    yield mcp_server
    sys.modules.pop("mcp_server", None)


def _seed_scan_cache_for(root: Path) -> tuple:
    key = (str(Path(root).resolve()), "any-fingerprint")
    code_rag._INDEX_SCAN_CACHE[key] = {"entries": {}, "timestamp": 9e18}
    return key


def test_apply_patch_invalidates_even_on_failure(mcp_module, monkeypatch, tmp_path):
    key = _seed_scan_cache_for(tmp_path)

    def exploding(*args, **kwargs):
        raise RuntimeError("write half done then boom")

    monkeypatch.setattr(mcp_module.EXEC, "apply_patch", exploding)
    with pytest.raises(RuntimeError, match="boom"):
        mcp_module.apply_patch("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n", dry_run=False)
    assert key not in code_rag._INDEX_SCAN_CACHE, "失敗路徑也必須 invalidate(finally)"


def test_apply_patch_dry_run_keeps_snapshot(mcp_module, monkeypatch, tmp_path):
    key = _seed_scan_cache_for(tmp_path)
    monkeypatch.setattr(mcp_module.EXEC, "apply_patch",
                        lambda *a, **kw: "[DRY RUN] ok")
    mcp_module.apply_patch("whatever", dry_run=True)
    assert key in code_rag._INDEX_SCAN_CACHE, "dry_run 不寫檔,不必 invalidate"
    code_rag._INDEX_SCAN_CACHE.clear()


def test_run_command_and_lint_fix_invalidate(mcp_module, monkeypatch, tmp_path):
    key = _seed_scan_cache_for(tmp_path)
    monkeypatch.setattr(mcp_module.EXEC, "run_command", lambda *a, **kw: "ok")
    mcp_module.run_command("pytest -q")
    assert key not in code_rag._INDEX_SCAN_CACHE

    key = _seed_scan_cache_for(tmp_path)
    monkeypatch.setattr(mcp_module.EXEC, "run_lint", lambda *a, **kw: "ok")
    mcp_module.run_lint("x.py", fix=False)
    assert key in code_rag._INDEX_SCAN_CACHE, "check-only 不改檔,不必 invalidate"
    mcp_module.run_lint("x.py", fix=True)
    assert key not in code_rag._INDEX_SCAN_CACHE


# ============================================================
# §5-4 batch embedding
# ============================================================
def test_plan_embed_batches_respects_both_budgets():
    texts = ["a" * 10] * 5
    # 筆數上限 2 → [0,1],[2,3],[4]
    assert code_rag.plan_embed_batches(texts, 2, 10_000) == [[0, 1], [2, 3], [4]]
    # chars 上限 25 → 兩筆(20)可以,三筆(30)不行
    assert code_rag.plan_embed_batches(texts, 32, 25) == [[0, 1], [2, 3], [4]]
    # 單筆超過 chars 上限:自成一批,不丟棄
    texts2 = ["x" * 100, "y" * 5, "z" * 5]
    assert code_rag.plan_embed_batches(texts2, 32, 50) == [[0], [1, 2]]
    # 保序、全覆蓋
    flat = [i for b in code_rag.plan_embed_batches(texts, 2, 10_000) for i in b]
    assert flat == list(range(5))


def test_http_batch_count_equals_plan(monkeypatch, tmp_path):
    root = _make_repo(tmp_path, n_files=3, funcs_per_file=2)  # 6 symbols
    monkeypatch.setattr(config, "EMBED_BATCH_SIZE", 4)
    monkeypatch.setattr(config, "EMBED_BATCH_MAX_CHARS", 10_000)
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", False)

    calls: list[list[str]] = []

    def fake_embed_batch(*, base_url, contents, model="", timeout=300):
        calls.append(list(contents))
        return [[1.0, 0.0] for _ in contents]

    monkeypatch.setattr(code_rag.llama_client, "embed_batch", fake_embed_batch)
    rag = code_rag.CodeRAG(str(root))
    rag.build_index(verbose=False)

    total = sum(len(c) for c in calls)
    assert total == 6, "每個 symbol 恰好 embed 一次"
    assert len(calls) == 2, "6 symbols / batch=4 → 2 個 HTTP batch"
    assert all(len(c) <= 4 for c in calls)


def test_embed_batch_restores_out_of_order_indices(monkeypatch):
    class _Resp:
        status_code = 200
        headers: dict = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [
                {"index": 1, "embedding": [3.0, 4.0]},
                {"index": 0, "embedding": [1.0, 2.0]},
            ]}

    class _Sess:
        def post(self, url, **kwargs):
            return _Resp()

    monkeypatch.setattr(llama_client, "get_session", lambda: _Sess())
    out = llama_client.embed_batch(
        base_url="http://127.0.0.1:8081", contents=["a", "b"])
    assert out == [[1.0, 2.0], [3.0, 4.0]], "必須按 data[].index 還原順序"


@pytest.mark.parametrize("payload,match", [
    ({"data": [{"index": 0, "embedding": [1.0]}]}, "cardinality"),          # 2 進 1 出
    ({"data": [{"index": 0, "embedding": [1.0]},
               {"index": 0, "embedding": [2.0]}]}, "duplicate"),            # index 重複
    ({"data": [{"index": 0, "embedding": [1.0]},
               {"index": 5, "embedding": [2.0]}]}, "invalid index"),        # 超界
    ({"data": [{"index": 0, "embedding": [1.0, 2.0]},
               {"index": 1, "embedding": [3.0]}]}, "dimension"),            # 維度不一
    ({"data": [{"index": 0, "embedding": []},
               {"index": 1, "embedding": [3.0]}]}, "empty"),                # 空向量
    ({"nope": True}, "unexpected shape"),
])
def test_embed_batch_strict_contract_failures(monkeypatch, payload, match):
    class _Resp:
        status_code = 200
        headers: dict = {}

        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class _Sess:
        def post(self, url, **kwargs):
            return _Resp()

    monkeypatch.setattr(llama_client, "get_session", lambda: _Sess())
    with pytest.raises(llama_client.EmbeddingContractError, match=match):
        llama_client.embed_batch(base_url="http://127.0.0.1:8081", contents=["a", "b"])


# ============================================================
# §5-5 掃描層副檔名
# ============================================================
def test_txt_md_skipped_from_symbol_scan_but_still_code_extensions(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("# doc\n", encoding="utf-8")
    (tmp_path / "todo.txt").write_text("todo\n", encoding="utf-8")

    rag = code_rag.CodeRAG(str(tmp_path))
    files = rag._scan_code_files(force_refresh=True)
    assert set(files) == {"a.py"}, ".txt/.md 不入 symbol 掃描"

    # 不動 CODE_EXTENSIONS:grep/list_dir 的可見範圍不變
    assert ".md" in config.CODE_EXTENSIONS
    assert ".txt" in config.CODE_EXTENSIONS


# ============================================================
# §5-6 context end_line(兩個 producer)+ passage 常數
# ============================================================
def test_python_ast_context_stops_at_end_line(tmp_path):
    from ast_parser import parse_file

    content = (
        "def short_one():\n"
        "    return 1\n"
        "\n"
        "def neighbor_secret():\n"
        "    return 2\n"
    )
    symbols = parse_file(tmp_path / "m.py", content)
    short = next(s for s in symbols if s.name == "short_one")
    assert short.end_line == 2
    assert "neighbor_secret" not in short.context, "短函式 context 吃到下一個函式"
    assert "return 1" in short.context


def test_tree_sitter_make_symbol_context_stops_at_end_line():
    from ast_parser import TreeSitterParser

    parser = TreeSitterParser.__new__(TreeSitterParser)  # 不需要真 parser
    lines = [
        "int short_one(void) {",
        "    return 1;",
        "}",
        "int neighbor_secret(void) {",
        "    return 2;",
        "}",
    ]
    node = SimpleNamespace(start_point=(0, 0), end_point=(2, 0))
    sym = parser._make_symbol(node, lines, "short_one", "function")
    assert sym.end_line == 3
    assert "neighbor_secret" not in sym.context
    assert "return 1" in sym.context


def test_rerank_passage_constant_is_honest():
    # 常數 = 儲存端真實上限(index entry context[:500]);擴充 passage 必須
    # 同步動三個 producer,本輪不做(§5-6)。
    assert config.CODE_RERANK_PASSAGE_MAX_CHARS == 500
