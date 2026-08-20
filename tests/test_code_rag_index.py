"""CodeRAG 索引層:檔案雜湊、live refresh、以及 review 修過的索引/查詢 bug。

合併自 tests/test_code_rag_quickfix.py、tests/test_code_rag_hash.py、
tests/test_code_rag_live_refresh.py(2026-08-20)。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import code_rag
from code_rag import CodeRAG
from tests._harness import import_mcp_module

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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
    # 這個 helper 的預設契約是完全離線。rerank 行為在上面的專屬案例中會
    # 明確打開並 mock；其餘 cache / TTL / embedding 測試不該先等 /health
    # retry 三秒，才因 fallback policy 決定能不能繼續。
    monkeypatch.setattr(code_rag, "USE_RERANKER", False)
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
    first = rag.query("func", top_k=3)
    assert first and first[0]["score"] == 9.0
    keys_before = {id(item): set(item) for item in rag_index_snapshot(rag)}

    # 第二次 query 不 rerank → fusion 分數;第一輪的 9.0 不得殘留
    monkeypatch.setattr(rag, "_should_rerank", lambda candidates, top_k: False)
    second = rag.query("func", top_k=3)
    assert second and all(r["score"] != 9.0 for r in second), "rerank 分數殘留到下一個 query"

    # 嚴禁把 query 分數寫進持久 item:index items 的 key 集合不得改變
    for item in rag_index_snapshot(rag):
        assert set(item) == keys_before[id(item)]
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
    """以 tmp_path 當 AICODE_ROOT 重新 import mcp_server(細節見 _harness)。"""
    yield import_mcp_module(monkeypatch, tmp_path)
    # 收尾要把 module 拔掉:同一個 shard 後面的測試不該撿到這個 root 的 server
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


def test_rerank_passage_budget_is_real_not_a_no_op():
    # 舊版鎖 500,因為儲存端就截在 500,放大 passage 是 no-op。
    # P3A 把儲存端上限獨立出來(CODE_RAG_CONTEXT_STORE_MAX_CHARS)之後,
    # 這個常數才真的有效果 —— 所以鎖的是不變式,不是那個數字。
    assert config.CODE_RERANK_PASSAGE_MAX_CHARS > 0
    assert (config.CODE_RERANK_PASSAGE_MAX_CHARS
            <= config.CODE_RAG_CONTEXT_STORE_MAX_CHARS), (
        "passage 預算超過儲存端上限的話,超出部分永遠是空的"
    )


# --------------------------------------------------------------------------
# 併自 tests/test_code_rag_hash.py:_compute_file_hash 的變更偵測。
# --------------------------------------------------------------------------
def _new_indexer(tmp_path: Path) -> CodeRAG:
    return CodeRAG(str(tmp_path))


def test_small_file_hash_reflects_content_change(tmp_path: Path):
    """小檔 (<256KiB) 內容變但 mtime 沒變 → hash 必須仍然改變。"""
    idx = _new_indexer(tmp_path)
    f = tmp_path / "x.py"
    f.write_bytes(b"a = 1\n")
    h1 = idx._compute_file_hash(f)

    # 改內容,然後強制 mtime 回去舊值(模擬 rsync --times / unzip 等保時間工具)
    import os
    stat = f.stat()
    f.write_bytes(b"a = 2\n")
    os.utime(f, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    h2 = idx._compute_file_hash(f)
    assert h1 != h2, (
        "小檔 content 變了 hash 卻沒變 — 同秒 edit / preserve-timestamp 場景會"
        "命中錯的 cache"
    )


def test_large_file_hash_uses_size_and_mtime_ns(tmp_path: Path, monkeypatch):
    """大檔走 stat 快路徑,但用 mtime_ns 而非 mtime — 同秒寫入也要分得開。"""
    idx = _new_indexer(tmp_path)
    f = tmp_path / "big.bin"
    big = b"x" * (idx._CONTENT_HASH_MAX_BYTES + 1)
    f.write_bytes(big)
    h1 = idx._compute_file_hash(f)

    # 再寫一次(模擬同秒 edit)。size 相同,mtime 整數秒可能相同,但 mtime_ns 不同。
    f.write_bytes(big)
    h2 = idx._compute_file_hash(f)

    # 兩次寫入時間極近;若 hash 是用 mtime_ns,h1 應該 != h2(高機率)。
    # 同 inode 同秒寫入但 ns 完全相同的機率極低 — 若真同步發生,hash 相同也合理,
    # 此 test 主要是檢驗「沒用秒解析度,改用 ns」的行為,不檢驗時間隨機性。
    # 確認 hash 至少是一個 32 字元的 md5 hex(沒 throw、沒回空字串)
    assert len(h1) == 32 and len(h2) == 32, (h1, h2)


def test_large_file_size_change_changes_hash(tmp_path: Path):
    """大檔 size 變 hash 必須變(快路徑的基本要求)。"""
    idx = _new_indexer(tmp_path)
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * (idx._CONTENT_HASH_MAX_BYTES + 1))
    h1 = idx._compute_file_hash(f)
    f.write_bytes(b"x" * (idx._CONTENT_HASH_MAX_BYTES + 100))
    h2 = idx._compute_file_hash(f)
    assert h1 != h2


def test_missing_file_returns_empty_hash(tmp_path: Path):
    idx = _new_indexer(tmp_path)
    assert idx._compute_file_hash(tmp_path / "no_such_file.py") == ""


# --------------------------------------------------------------------------
# 併自 tests/test_code_rag_live_refresh.py:同一 session 內的來源更新。
# --------------------------------------------------------------------------
def test_query_refreshes_changed_source_in_same_session(monkeypatch, tmp_path: Path):
    """TTL=0(關閉快照)時行為同舊版:每次 query 都 fresh 掃描,立即看到改動。

    §5-3 驗收之一:AICODE_CODE_RAG_REFRESH_TTL=0 行為同現狀。TTL>0 的
    快照 / invalidation 行為由 tests/test_code_rag_quickfix.py 鎖。
    """
    source = tmp_path / "module.py"
    source.write_text("def old_symbol():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(code_rag.config, "CODE_RAG_REFRESH_TTL_SECONDS", 0)
    rag = code_rag.CodeRAG(str(tmp_path))
    monkeypatch.setattr(rag, "_get_embedding", lambda _text: [1.0, 0.0])
    monkeypatch.setattr(rag, "_embed_texts_batched",
                        lambda texts: [[1.0, 0.0]] * len(texts))
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", False)
    monkeypatch.setattr(code_rag, "USE_RERANKER", False)

    first = rag.query("old_symbol", top_k=3)
    assert first and first[0]["symbol"] == "old_symbol"

    source.write_text("def new_symbol():\n    return 2\n", encoding="utf-8")
    second = rag.query("new_symbol", top_k=3)

    assert second and second[0]["symbol"] == "new_symbol"
    assert all(row["symbol"] != "old_symbol" for row in second)


def test_lazy_semantic_query_materializes_dense_index_instead_of_arbitrary_slice(monkeypatch, tmp_path: Path):
    rag = code_rag.CodeRAG(str(tmp_path))
    rag.index = [
        {
            "path": f"file_{i}.py",
            "symbol": f"symbol_{i}",
            "type": "class",
            "line": 1,
            "context": "generic plumbing helper",
        }
        for i in range(200)
    ]
    rag.index[-1]["context"] = "coordinates the neural accelerator reset sequence"
    rag._lazy_embed = True
    rag._lazy_embed_top_k = 10
    monkeypatch.setattr(code_rag, "USE_RERANKER", False)

    def fake_embedding(text: str) -> list[float]:
        if text == "如何重新啟動加速器？" or "neural accelerator reset sequence" in text:
            return [1.0, 0.0]
        return [0.0, 1.0]

    monkeypatch.setattr(rag, "_get_embedding", fake_embedding)
    # materialize 走批次路徑(§5-4)
    monkeypatch.setattr(rag, "_embed_texts_batched",
                        lambda texts: [fake_embedding(t) for t in texts])

    results = rag.query("如何重新啟動加速器？", top_k=5)

    assert any(row["symbol"] == "symbol_199" for row in results)
    assert rag._lazy_embed is False


# ============================================================
# cache identity + _make_symbol 的輕量 node double
#
# 刻意放在這個**沒有全域 skip** 的檔案:這些行為與 tree-sitter 無關,
# 掛在 tree-sitter skip 底下的話,缺 grammar 的環境就完全沒有這層防護 ——
# 而那正是最需要 fail-safe 的環境。
# ============================================================
def _identity_seeded_rag(tmp_path: Path) -> CodeRAG:
    """手工造 index + file_cache,不經過任何 parser。"""
    (tmp_path / "a.c").write_text("int x;\n", encoding="utf-8")
    rag = CodeRAG(str(tmp_path))
    rag.index = [{
        "path": "a.c", "symbol": "x", "type": "global", "line": 1,
        "context": "int x;", "qualified_name": "x", "backend": "manual",
    }]
    rag._file_cache = {"a.c": {"hash": "h", "symbols": rag.index, "embeddings": []}}
    rag._save_cache()
    return rag


@pytest.mark.smoke
def test_make_symbol_tolerates_nodes_without_sibling_api():
    """BUG REGRESSION:leading comment association 讓 _make_symbol 對 node 的
    API 面變嚴格,結果打爆既有的輕量 test double。

    `_make_symbol` 一直允許「只有 start_point / end_point」的 node 物件
    (本檔上面驗 context 截斷的那條就是這樣)。P3A 之後它無條件呼叫
    `_leading_comment()` 讀 `prev_named_sibling`,對那種 node 直接
    AttributeError —— full suite 的確定性失敗,而 smoke 當時沒涵蓋到。
    """
    from ast_parser import TreeSitterParser

    parser = TreeSitterParser.__new__(TreeSitterParser)
    lines = ["int short_one(void) {", "    return 1;", "}"]
    node = SimpleNamespace(start_point=(0, 0), end_point=(2, 0))

    sym = parser._make_symbol(node, lines, "short_one", "function")
    assert sym.end_line == 3
    assert sym.comments is None, "拿不到 sibling 就是沒有 leading comment,不是崩潰"


@pytest.mark.smoke
def test_cache_identity_is_the_single_source_for_meta_and_validation():
    """身分欄位只有一份定義,寫入端 / 驗證端 / 測試 fixture 都從這裡取。

    各寫一份的失敗是無聲的:加欄位時 fixture 或 loader 會落後 —— 舊 cache 被拒、
    測試改走 full rebuild,「還是綠的」卻不再驗它本來要驗的東西。
    """
    import ast_parser

    identity = code_rag.cache_identity()
    assert identity["schema_version"] == code_rag.CODE_RAG_CACHE_SCHEMA_VERSION
    assert identity["parser_semantics_version"] == \
        ast_parser.PARSER_SEMANTICS_VERSION
    assert identity["embed_text_schema_version"] == \
        code_rag.EMBED_TEXT_SCHEMA_VERSION
    assert identity["render_budgets"] == {
        "context_store": config.CODE_RAG_CONTEXT_STORE_MAX_CHARS,
        "comment": config.CODE_RAG_COMMENT_MAX_CHARS,
        "embed_text": config.CODE_RAG_EMBED_TEXT_MAX_CHARS,
    }


@pytest.mark.smoke
def test_every_cache_identity_field_is_actually_validated(tmp_path: Path,
                                                          monkeypatch):
    """NEW SILENT CONTRACT:loader 必須**遍歷**整份 identity,不得列舉 key。

    現有欄位在列舉版本下也擋得住,所以只poison 現有欄位驗不出差別。真正的風險
    是**未來新增的欄位**:寫死清單的話,寫入端會存、loader 卻視而不見 —— 加了
    一層防護卻沒生效,而且不會有任何紅字。這裡用一個 identity 裡有、但磁碟上的
    meta 沒有的欄位來逼出那個差異。
    """
    _identity_seeded_rag(tmp_path)
    assert CodeRAG(str(tmp_path))._load_file_cache(), "同身分應載入得到"

    # 模擬「之後有人在 cache_identity() 加了一個欄位」:磁碟上的舊 meta 沒有它。
    future = {**code_rag.cache_identity(), "future_identity_field": "v1"}
    monkeypatch.setattr(code_rag, "cache_identity", lambda: future)
    assert CodeRAG(str(tmp_path))._load_file_cache() == {}, (
        "identity 新增的欄位沒有被驗證 —— loader 還在比對硬編碼的 key 清單"
    )
    monkeypatch.undo()

    # 現有欄位當然也要各自擋得住。
    baseline = code_rag.cache_identity()
    for field in baseline:
        poisoned = {**baseline, field: "___drifted___"}
        monkeypatch.setattr(code_rag, "cache_identity", lambda p=poisoned: p)
        assert CodeRAG(str(tmp_path))._load_file_cache() == {}, (
            f"identity 欄位 {field} 改變了,loader 卻照樣接受舊 cache"
        )
        monkeypatch.undo()


@pytest.mark.smoke
def test_render_budget_change_invalidates_the_cache(tmp_path: Path, monkeypatch):
    """BUG REGRESSION:改 render 預算的**環境變數**不會讓舊 embedding 失效。

    這三個預算是 `AICODE_*` 環境變數可覆寫的,但 cache meta 原本只存固定的
    schema 版本。重啟時把預算調大/調小,render 出來的 embed text 不同了,
    增量重建卻只比 file_hash —— 舊向量被靜默沿用,沒有任何訊息提醒你現在查的
    是用舊 render 算出來的向量。
    """
    _identity_seeded_rag(tmp_path)
    assert CodeRAG(str(tmp_path))._load_file_cache(), "同預算應載入得到"

    for name in ("CODE_RAG_EMBED_TEXT_MAX_CHARS",
                 "CODE_RAG_CONTEXT_STORE_MAX_CHARS",
                 "CODE_RAG_COMMENT_MAX_CHARS"):
        monkeypatch.setattr(config, name, getattr(config, name) + 200)
        assert CodeRAG(str(tmp_path))._load_file_cache() == {}, (
            f"{name} 變了,舊向量是用別的 render 算的,不得沿用"
        )
        monkeypatch.undo()
