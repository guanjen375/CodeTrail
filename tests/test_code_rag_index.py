"""CodeRAG 索引層:索引建置、檔案雜湊、live refresh、dense cache、索引範圍(index_scope)
與檔案種類政策(FileKindPolicy)。

合併自(2026-09-02):

- tests/test_code_rag_index.py —— 本身是 2026-08-20 合併 test_code_rag_quickfix /
  test_code_rag_hash / test_code_rag_live_refresh 的產物:rerank query-local、cache 世代
  一致性 / fail-loud、TTL snapshot + invalidation、MCP 寫入工具的 finally invalidation、
  batch embedding、掃描層副檔名、context end_line、cache identity。
- tests/test_code_rag_dense_cache.py —— dense cache 的真實 bug regression(2026-08-21,
  真實樹實測)。D2:``_lazy_embed`` 只看符號數,不看 embedding 是否已備齊,一份已經完整
  embed 的索引每次載入仍被判為 lazy,dense 矩陣不建、查詢時再跑一次
  ``_materialize_dense_index()``。D3:``build_index()`` 結尾無條件 ``_save_cache()``,即使
  0 檔變更、0 檔刪除。D4:``_load_file_cache`` 用 ``except Exception`` 把 ``MemoryError``
  也接住,印成「cache meta 損壞,安全重建」—— 後果是永久刪掉全部向量。D2+D3 疊加的實測
  後果:330270 個符號的樹上,meta JSON 是 22.9GB,單次查詢會觸發 2 次全量回寫,每次約
  100 分鐘(實測寫入速率 3.6MB/s)。第二輪(真實樹上實跑 9fb1efa)再補 D5(dense 向量不得
  又以 JSON 存一份)、D6(一個檔案變更不得把整份 dense 索引打回 lazy)、D7(``(path, symbol,
  line)`` 不是唯一鍵,npz 還原要走位置對映)。
- tests/test_index_scope.py —— 索引範圍(index_scope):成員資格、三態剪枝、Layer C loader、
  快取遷移、scripts/index_stats.py。fixture 一律用合成樹名(toolchain_x / vendor_env / ...):
  真實專案名永遠不進 repo。最重要的一條是 test_tri_state_walk_matches_should_index_file:
  三態走訪的結果必須等於「對全樹逐檔跑 should_index_file」。任何 PRUNE 吃掉了應該進索引的
  檔案,那條就會紅 —— 這是整份設計的防呆核心。
- tests/test_file_kind_policy.py —— FileKindPolicy 的契約(施工規格 §6 P3B,Level 1)。
  NEW SILENT CONTRACT:兩份手寫清單漂掉是這個 repo 已經發生過的事,``GREP_DEFAULT_EXTENSIONS``
  比 ``CODE_EXTENSIONS`` 還窄,連 ``.cc`` / ``.cxx`` / ``.pyi`` / ``.mk`` / ``.cmake`` /
  ``.tcl`` 這些既有格式都搜不到,而且沒有任何測試會因此變紅。同時鎖住 Level 1 的承諾邊界:
  新檔案類型只保證 grep / search 找得到,不保證進 dense symbol retrieval;宣稱過頭跟漏做一樣糟。

smoke:本檔不用 module 層 pytestmark,逐條標記。原 dense_cache 與 file_kind_policy 是整檔
smoke,折進來後每一條各自帶 @pytest.mark.smoke;其餘照原本的逐條標記。

各來源檔原本各自一份 autouse 的 ``_clean_scan_cache`` / ``_fresh_scan_cache``(清
``code_rag._INDEX_SCAN_CACHE``),已由 tests/conftest.py 的全域 autouse
``_isolate_code_rag_scan_cache`` 接手,本檔不再重複。
"""
from __future__ import annotations

import fnmatch
import json
import math
import os
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._harness import import_mcp_module

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import code_rag  # noqa: E402
import config  # noqa: E402
import file_kind_policy as policy  # noqa: E402
import fs_safety  # noqa: E402
import index_scope  # noqa: E402
import llama_client  # noqa: E402
from code_rag import CodeRAG  # noqa: E402
from index_scope import (  # noqa: E402
    INDEX,
    PRUNE,
    TRAVERSE_ONLY,
    IndexScope,
    IndexScopeError,
    compile_pattern,
    literal_prefix,
    load_index_scope,
    load_scope_config,
    walk_index_files,
)

# ── 原 test_code_rag_index.py:索引層(rerank query-local / cache 世代 / TTL snapshot / batch embedding / cache identity) ──

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
        "docstring": config.CODE_RAG_DOCSTRING_MAX_CHARS,
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

    這些預算是 `AICODE_*` 環境變數可覆寫的,但 cache meta 原本只存固定的
    schema 版本。重啟時把預算調大/調小,render 出來的 embed text 不同了,
    增量重建卻只比 file_hash —— 舊向量被靜默沿用,沒有任何訊息提醒你現在查的
    是用舊 render 算出來的向量。
    """
    _identity_seeded_rag(tmp_path)
    assert CodeRAG(str(tmp_path))._load_file_cache(), "同預算應載入得到"

    for name in ("CODE_RAG_EMBED_TEXT_MAX_CHARS",
                 "CODE_RAG_CONTEXT_STORE_MAX_CHARS",
                 "CODE_RAG_COMMENT_MAX_CHARS",
                 "CODE_RAG_DOCSTRING_MAX_CHARS"):
        monkeypatch.setattr(config, name, getattr(config, name) + 200)
        assert CodeRAG(str(tmp_path))._load_file_cache() == {}, (
            f"{name} 變了,舊向量是用別的 render 算的,不得沿用"
        )
        monkeypatch.undo()


@pytest.mark.smoke
def test_no_render_affecting_cap_is_left_out_of_cache_identity():
    """NEW SILENT CONTRACT:render 路徑上不得再有「沒進 identity 的截斷數字」。

    規則寫成白名單(只有 render_budgets 裡的預算免 bump)之後,這條就是它的
    機械檢查:code_rag 的 render 路徑不得出現硬編碼的 `[:數字]` 截斷 —— 那種
    數字改了不會讓任何 cache 失效,而「預算不必 bump」又會被讀成它也不必 bump,
    兩邊都不動,舊向量就被靜默沿用。docstring 的 `[:300]` 就是這樣漏掉的。
    """
    import re

    source = (REPO_ROOT / "code_rag.py").read_text(encoding="utf-8")
    # 只看真的會進 index entry / embed text 的欄位截斷。
    offenders = re.findall(
        r"\['(?:docstring|context|comments|signature|type_hints)'\]\[:\d+\]",
        source,
    ) + re.findall(
        r"sym\.(?:docstring|context|comments|signature)\[:\d+\]", source
    )
    assert offenders == [], (
        f"render 路徑上還有硬編碼截斷 {offenders} —— 要嘛改成具名預算並放進 "
        "cache_identity() 的 render_budgets,要嘛 bump EMBED_TEXT_SCHEMA_VERSION"
    )


# ── 原 test_code_rag_dense_cache.py:dense cache 的真實 bug regression(D2–D7) ──

def _make_dense_repo(tmp_path: Path, n_files: int = 3, funcs_per_file: int = 3) -> Path:
    for i in range(n_files):
        body = "".join(
            f"def func_{i}_{j}():\n    return {j}\n\n" for j in range(funcs_per_file)
        )
        (tmp_path / f"mod_{i}.py").write_text(body, encoding="utf-8")
    return tmp_path


def _rag(monkeypatch, root: Path, *, lazy_max: int = 1) -> code_rag.CodeRAG:
    """離線 CodeRAG,且 lazy 門檻壓到必定觸發。"""
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", True)
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED_MAX_SYMBOLS", lazy_max)
    monkeypatch.setattr(code_rag, "USE_RERANKER", False)
    rag = code_rag.CodeRAG(str(root))
    monkeypatch.setattr(rag, "_get_embedding", lambda _text: [1.0, 0.0])
    monkeypatch.setattr(rag, "_embed_texts_batched",
                        lambda texts: [[1.0, 0.0]] * len(texts))
    return rag


@pytest.mark.smoke
def test_fully_embedded_index_is_not_treated_as_lazy(monkeypatch, tmp_path):
    """D2:cache 裡每個符號都有 embedding 時,重新載入不得再判為 lazy。"""
    root = _make_dense_repo(tmp_path)

    first = _rag(monkeypatch, root)
    first.build_index(verbose=False)
    assert first._lazy_embed is True, "前提:符號數必須超過 lazy 門檻"
    first._materialize_dense_index()          # 補齊 embedding 並落盤
    assert first._lazy_embed is False

    second = _rag(monkeypatch, root)
    second.build_index(verbose=False)
    assert second._lazy_embed is False, (
        "embedding 已全部在 cache 裡,不該再被當成 lazy —— 否則每次查詢都會"
        "重跑 _materialize_dense_index() 並全量回寫 cache"
    )
    assert second.embeddings is not None, "非 lazy 就該直接建好 dense 矩陣"


@pytest.mark.smoke
def test_unchanged_index_does_not_rewrite_cache(monkeypatch, tmp_path):
    """D3:0 檔變更 0 檔刪除時,build_index 不得回寫 cache。"""
    root = _make_dense_repo(tmp_path)

    first = _rag(monkeypatch, root)
    first.build_index(verbose=False)
    first._materialize_dense_index()

    second = _rag(monkeypatch, root)
    calls: list[int] = []
    monkeypatch.setattr(second, "_save_cache", lambda: calls.append(1))
    second.build_index(verbose=False)
    assert calls == [], (
        f"完全未變更的索引不該回寫 cache,實際呼叫 {len(calls)} 次"
    )


@pytest.mark.smoke
def test_changed_file_still_writes_cache(monkeypatch, tmp_path):
    """D3 的反向防線:真的有變更時仍必須回寫,不能為了省 IO 而漏存。"""
    root = _make_dense_repo(tmp_path)

    first = _rag(monkeypatch, root)
    first.build_index(verbose=False)
    first._materialize_dense_index()

    (root / "mod_new.py").write_text(
        "def brand_new_symbol():\n    return 1\n", encoding="utf-8"
    )

    second = _rag(monkeypatch, root)
    calls: list[int] = []
    original = code_rag.CodeRAG._save_cache

    def counting_save():
        calls.append(1)
        return original(second)

    monkeypatch.setattr(second, "_save_cache", counting_save)
    second.build_index(verbose=False)

    assert calls, "有新增檔案時必須回寫 cache"
    assert any(item.get("symbol") == "brand_new_symbol" for item in second.index)


@pytest.mark.smoke
def test_memory_error_is_not_reported_as_corruption(monkeypatch, tmp_path):
    """記憶體不足 != cache 壞掉,不得靜默丟棄一份有效的 cache。

    實測(2026-08-21):330270 符號的樹上,meta JSON 是 22.9GB,光 json.load
    就要 100GB 以上位址空間。記憶體不足時 MemoryError 會被
    ``except Exception`` 接住並印成「cache meta 損壞,安全重建」,接著:
      1. 整棵樹重建(該樹實測 55 分鐘);
      2. 重建後 _lazy_embed=True,_save_cache 的 lazy 分支會 unlink 掉
         既有的 .npz,並以無 embedding 的 meta 覆蓋原檔。
    也就是一次暫態記憶體不足就永久刪掉全部向量。必須 fail-loud。
    """
    root = _make_dense_repo(tmp_path)
    seed = _rag(monkeypatch, root)
    seed.build_index(verbose=False)
    seed._materialize_dense_index()
    assert seed.cache_meta_file.exists(), "前提:必須先有一份 cache"

    victim = _rag(monkeypatch, root)

    def out_of_memory(*_args, **_kwargs):
        raise MemoryError()

    monkeypatch.setattr(code_rag.json, "load", out_of_memory)

    with pytest.raises(MemoryError):
        victim._load_file_cache()


def _dense_seed(monkeypatch, root: Path) -> code_rag.CodeRAG:
    rag = _rag(monkeypatch, root)
    rag.build_index(verbose=False)
    rag._materialize_dense_index()
    assert rag.embeddings is not None
    return rag


@pytest.mark.smoke
def test_dense_save_keeps_vectors_out_of_meta_json(monkeypatch, tmp_path):
    """D5:dense 模式下向量已經在 .npz 裡,不該又以 JSON 文字存一份。

    實測:330270 符號的樹上,同一批向量在 .npz 是 1.25GB、在 meta JSON 是
    22.9GB(18 倍)。而且 .npz 從頭到尾沒有被讀回過 —— 全檔沒有 np.load,
    它唯一的用途是被算 md5 當世代 token。載入實際走的是那份 22.9GB JSON,
    光 json.load 就要 100GB 以上位址空間。
    """
    root = _make_dense_repo(tmp_path)
    seed = _dense_seed(monkeypatch, root)

    meta = json.loads(seed.cache_meta_file.read_text(encoding="utf-8"))
    carriers = [
        rel for rel, cached in meta.get("file_cache", {}).items()
        if cached.get("embeddings")
    ]
    assert not carriers, (
        f"dense 模式下 meta JSON 仍夾帶向量(檔案:{carriers});"
        "向量應該只存在 .npz"
    )


@pytest.mark.smoke
def test_dense_cache_reloads_vectors_from_npz(monkeypatch, tmp_path):
    """D5 的另一半:既然 JSON 不再存向量,載入就必須真的從 .npz 讀回來。"""
    root = _make_dense_repo(tmp_path)
    seed = _dense_seed(monkeypatch, root)
    expected = seed.embeddings.copy()

    reloaded = _rag(monkeypatch, root)
    reloaded.build_index(verbose=False)

    assert reloaded.embeddings is not None, "重新載入後應該直接有 dense 矩陣"
    assert reloaded.embeddings.shape == expected.shape
    assert reloaded.embeddings == pytest.approx(expected)


@pytest.mark.smoke
def test_legacy_cache_with_inline_vectors_still_loads(monkeypatch, tmp_path):
    """相容:既有的舊 cache 仍夾帶向量,不得因為新格式而失效(重建要 55 分鐘)。"""
    root = _make_dense_repo(tmp_path)
    seed = _dense_seed(monkeypatch, root)

    # 把向量塞回 JSON,重現舊格式
    meta = json.loads(seed.cache_meta_file.read_text(encoding="utf-8"))
    rows = [list(map(float, row)) for row in seed.embeddings]
    by_symbol = {
        (it.get("path"), it.get("symbol"), it.get("line")): row
        for it, row in zip(meta["index"], rows)
    }
    for cached in meta["file_cache"].values():
        cached["embeddings"] = [
            by_symbol.get((s.get("path"), s.get("symbol"), s.get("line")), [])
            for s in cached.get("symbols", [])
        ]
    seed.cache_meta_file.write_text(json.dumps(meta, ensure_ascii=False),
                                    encoding="utf-8")

    legacy = _rag(monkeypatch, root)
    legacy.build_index(verbose=False)
    assert legacy.embeddings is not None, "舊格式 cache 應該仍能載入"
    assert legacy.embeddings == pytest.approx(seed.embeddings)


# ============================================================
# 2026-08-21 第二輪:真實樹上實跑 9fb1efa 才浮出來的兩個
# ============================================================
def _distinct_vec(text: str, dim: int = 8) -> list[float]:
    """每段文字一個可辨識的單位向量 —— 錯配才看得出來。"""
    h = zlib.crc32(text.encode("utf-8"))
    raw = [((h >> (3 * k)) & 7) + 1 for k in range(dim)]
    norm = math.sqrt(sum(v * v for v in raw))
    return [v / norm for v in raw]


def _rag_distinct(monkeypatch, root: Path, *, lazy_max: int = 1) -> code_rag.CodeRAG:
    rag = _rag(monkeypatch, root, lazy_max=lazy_max)
    monkeypatch.setattr(rag, "_get_embedding", _distinct_vec)
    monkeypatch.setattr(rag, "_embed_texts_batched",
                        lambda texts: [_distinct_vec(t) for t in texts])
    return rag


@pytest.mark.smoke
def test_one_changed_file_must_not_drop_a_dense_index_back_to_lazy(monkeypatch, tmp_path):
    """D6:一個檔案變更就把整份 dense 索引打回 lazy,並且刪掉 .npz。

    真實樹實測(2026-08-21,HEAD 9fb1efa):樹裡只有一個檔變動(39 個符號),``all(embeddings_list)`` 因此為 False → ``_lazy_embed``
    維持 True → ``self.embeddings = None`` → ``_save_cache`` 走 lazy 分支
    ``unlink`` 掉既有的 1.25GB ``.npz``,並回寫 22.9GB 的舊格式 meta。
    量到的是 build_index 578.5s / 寫入 21.34GB。

    ``_backfill_cached_embedding_gaps`` 本來就是為了補這種空洞而存在,但它在
    ``not self._lazy_embed`` 分支裡,這條路根本走不到。lazy 的判準必須是
    「**還缺幾個**向量」而不是「總共幾個符號」。
    """
    root = _make_dense_repo(tmp_path)
    seed = _rag_distinct(monkeypatch, root)
    seed.build_index(verbose=False)
    seed._materialize_dense_index()
    assert seed.cache_emb_file.exists(), "前提:必須先有一份 dense .npz"

    (root / "mod_new.py").write_text(
        "def only_one_new_symbol():\n    return 1\n", encoding="utf-8"
    )

    second = _rag_distinct(monkeypatch, root)
    second.build_index(verbose=False)

    assert second._lazy_embed is False, (
        "只缺 1 個向量就把整份索引打回 lazy —— 既有向量會被 unlink,"
        "而且下次查詢要重算全部"
    )
    assert second.embeddings is not None, "非 lazy 就該直接建好 dense 矩陣"
    assert second.embeddings.shape[0] == len(second.index)
    assert second.cache_emb_file.exists(), ".npz 被刪掉了 —— 既有向量永久遺失"

    meta = json.loads(second.cache_meta_file.read_text(encoding="utf-8"))
    carriers = [rel for rel, cached in meta.get("file_cache", {}).items()
                if cached.get("embeddings")]
    assert not carriers, f"回寫成夾帶向量的舊格式(檔案:{carriers})"


@pytest.mark.smoke
def test_npz_restore_keeps_one_row_per_symbol_when_keys_collide(monkeypatch, tmp_path):
    """D7:``(path, symbol, line)`` 不是唯一鍵,用它查表會無聲錯配向量。

    真實樹實測(2026-08-21):``typedef enum {...} Boolean;`` 一行會產生
    typedef 與 enum 兩個同名同行的符號。全樹 213 組碰撞 / 476 個符號,
    ``_restore_embeddings_from_npz`` 的 dict 後寫覆蓋前寫 → 263 個符號拿到
    別人的向量;其中 46 組 embed text 真的不同。筆數對得上、shape 檢查過得了,
    完全無聲。

    npz 的列序就是 ``meta["index"]`` 的序,而 ``meta["index"]`` 就是各檔
    ``symbols`` 的串接 —— 位置對映不需要任何鍵。
    """
    root = _make_dense_repo(tmp_path)
    seed = _rag_distinct(monkeypatch, root)
    seed.build_index(verbose=False)
    seed._materialize_dense_index()

    meta = json.loads(seed.cache_meta_file.read_text(encoding="utf-8"))
    index = meta["index"]
    flat = [s for cached in meta["file_cache"].values()
            for s in cached.get("symbols", [])]
    assert len(flat) == len(index) >= 2, "前提:index 是各檔 symbols 的串接"

    # 重現真實碰撞:第 1 筆偽裝成與第 0 筆同 path/symbol/line(type 仍不同)
    for row in (index, flat):
        row[1]["path"] = row[0]["path"]
        row[1]["symbol"] = row[0]["symbol"]
        row[1]["line"] = row[0]["line"]
    seed.cache_meta_file.write_text(json.dumps(meta, ensure_ascii=False),
                                    encoding="utf-8")

    import numpy as np

    with np.load(seed.cache_emb_file) as data:
        rows = data["embeddings"].tolist()
    assert rows[0] != pytest.approx(rows[1]), "前提:兩列向量本來就不同"

    restored = _rag_distinct(monkeypatch, root)._load_file_cache()
    got = [emb for cached in restored.values()
           for emb in cached.get("embeddings", [])]
    assert len(got) == len(rows)
    assert got[0] == pytest.approx(rows[0]), "第 0 筆拿到別人的向量"
    assert got[1] == pytest.approx(rows[1]), (
        "第 1 筆被同鍵的第 0 筆覆蓋 —— key 查表把兩個不同符號壓成同一個向量"
    )


# ── 原 test_index_scope.py:索引範圍(成員資格 / 三態剪枝 / Layer C loader / 快取遷移 / index_stats) ──

# ============================================================
# 合成樹
# ============================================================

TREE_FILES = {
    "src/core/engine.c": "int engine(void) { return 0; }\n",
    "src/core/engine.h": "int engine(void);\n",
    "docs/notes.md": "# notes\n",
    "vendor_env/keep.c": "int keep(void) { return 1; }\n",
    "vendor_env/junk.c": "int junk(void) { return 2; }\n",
    "vendor_env/deep/more.c": "int more(void) { return 3; }\n",
    "toolchain_x/arc/lib/src/stl/vector.h": "template<class T> struct vec {};\n",
    "toolchain_x/arc/lldbac/lib/registers/regs.c": "int regs(void) { return 4; }\n",
    "lib/python3.11/stdlib_mod.py": "def stdlib_mod(): pass\n",
    "lib/python3.11/custom_patch.py": "def custom_patch(): pass\n",
    "lib/python3_tools/helper.py": "def helper(): pass\n",
    "site-packages/mypkg/core.py": "def core(): pass\n",
    "build_env/pyvenv.cfg": "home = /usr\n",
    "build_env/lib/runtime_mod.py": "def runtime_mod(): pass\n",
    "conda_env/conda-meta/history": "",
    "conda_env/runtime_pkg.py": "def runtime_pkg(): pass\n",
    "pkg_meta.egg-info/entry.py": "def entry(): pass\n",
    "vendor/keep.c": "int vendored(void) { return 5; }\n",
    ".hidden/secret.py": "def secret(): pass\n",
    ".github/workflows/ci.yml": "name: ci\n",
    "notes.txt": "plain\n",
}


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "project_tree"
    for rel, content in TREE_FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def _write_scope(tmp_path: Path, monkeypatch, root: Path, **entry) -> Path:
    """寫一份 index-scope.json 並讓 loader 指過去(0600)。"""
    payload = {"schema_version": 1, "roots": [{"root": str(root), **entry}]}
    return _write_raw_scope(tmp_path, monkeypatch, payload)


def _write_raw_scope(tmp_path: Path, monkeypatch, payload) -> Path:
    path = tmp_path / "index-scope.json"
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    monkeypatch.setenv("AICODE_INDEX_SCOPE_FILE", str(path))
    return path


def _indexed(scope: IndexScope) -> set[str]:
    return {rel for _fp, rel in walk_index_files(scope)}


def _brute_force(scope: IndexScope, root: Path) -> set[str]:
    """不剪枝、全樹枚舉,逐檔問 should_index_file —— 不變式的基準集合。"""
    out = set()
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for filename in filenames:
            abs_path = Path(dirpath) / filename
            rel = abs_path.relative_to(root).as_posix()
            if scope.should_index_file(rel):
                out.add(rel)
    return out


def test_cpp_header_extensions_are_in_default_index_scope(tmp_path):
    root = tmp_path / "headers"
    root.mkdir()
    (root / "device.hh").write_text("int read_device(void);\n", encoding="utf-8")
    (root / "registers.hxx").write_text("int read_register(void);\n", encoding="utf-8")
    scope = IndexScope(root)

    assert scope.should_index_file("device.hh") is True
    assert scope.should_index_file("registers.hxx") is True
    assert _indexed(scope) == {"device.hh", "registers.hxx"}


# ============================================================
# 不變式(整份設計的防呆核心)
# ============================================================


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"exclude": ["vendor_env/**"], "include": ["vendor_env/keep.c"]},
        {"exclude": ["toolchain_x/arc/lib/src/stl/**"]},
        {"include": ["**/registers/**"], "exclude": ["toolchain_x/**"]},
        {"detectors": False},
        {"mode": "allowlist", "include": ["src/**", "site-packages/mypkg/core.py"]},
    ],
)
def test_tri_state_walk_matches_should_index_file(tree, tmp_path, monkeypatch, entry):
    _write_scope(tmp_path, monkeypatch, tree, **entry)
    baseline = _brute_force(load_index_scope(tree), tree)
    actual = _indexed(load_index_scope(tree))
    assert actual == baseline, (
        "三態走訪與 should_index_file 不一致 —— PRUNE 吃掉了應該進索引的檔案:"
        f"漏 {sorted(baseline - actual)} / 多 {sorted(actual - baseline)}"
    )


# ============================================================
# Rescue 四測
# ============================================================


def test_rescue_explicit_file_under_excluded_dir(tree, tmp_path, monkeypatch):
    """1. exclude 整個目錄 + include 單檔 → 只有那一檔進得來。"""
    _write_scope(tmp_path, monkeypatch, tree,
                 exclude=["vendor_env/**"], include=["vendor_env/keep.c"])
    indexed = _indexed(load_index_scope(tree))
    assert "vendor_env/keep.c" in indexed
    assert "vendor_env/junk.c" not in indexed
    assert "vendor_env/deep/more.c" not in indexed


def test_rescue_direct_file_under_b_detected_dir(tree, tmp_path, monkeypatch):
    """2. B 命中的目錄底下,explicit include 的檔案救得回來。"""
    _write_scope(tmp_path, monkeypatch, tree,
                 include=["lib/python3.11/custom_patch.py"])
    indexed = _indexed(load_index_scope(tree))
    assert "lib/python3.11/custom_patch.py" in indexed
    assert "lib/python3.11/stdlib_mod.py" not in indexed


def test_rescue_direct_file_under_a_prime_dir(tree, tmp_path, monkeypatch):
    """3. A′ 命中的目錄底下,explicit include 的檔案救得回來。"""
    _write_scope(tmp_path, monkeypatch, tree,
                 include=["site-packages/mypkg/core.py"])
    indexed = _indexed(load_index_scope(tree))
    assert "site-packages/mypkg/core.py" in indexed


def test_hard_gate_layer_a_cannot_be_rescued(tree, tmp_path, monkeypatch):
    """4a. Layer A(IGNORED_DIRS)是 hard gate,include 救不回來。"""
    _write_scope(tmp_path, monkeypatch, tree, include=["vendor/keep.c"])
    scope = load_index_scope(tree)
    assert scope.should_index_file("vendor/keep.c") is False
    assert "vendor/keep.c" not in _indexed(scope)


def test_hard_gate_containment_cannot_be_rescued(tree, tmp_path, monkeypatch):
    """4b. containment 逃逸的檔案,include 也救不回來。"""
    outside = tmp_path / "outside_tree"
    outside.mkdir()
    (outside / "leak.c").write_text("int leak(void) { return 6; }\n", encoding="utf-8")
    link = tree / "escaped.c"
    try:
        link.symlink_to(outside / "leak.c")
    except (OSError, NotImplementedError):
        pytest.skip("這個環境不能建 symlink")

    _write_scope(tmp_path, monkeypatch, tree, include=["escaped.c"])
    scope = load_index_scope(tree)
    assert scope.should_index_file("escaped.c") is False
    assert "escaped.c" not in _indexed(scope)


# ============================================================
# 預設層(A′ / B)
# ============================================================


def test_default_layers_exclude_third_party_runtimes(tree):
    indexed = _indexed(load_index_scope(tree))
    assert "src/core/engine.c" in indexed
    assert "docs/notes.md" in indexed
    assert "lib/python3_tools/helper.py" in indexed, "python3_tools 不是 pythonX.Y,不該被誤殺"
    assert ".github/workflows/ci.yml" in indexed, "ALLOWED_DOT_DIRS 行為必須不變"

    assert "site-packages/mypkg/core.py" not in indexed          # A'
    assert "lib/python3.11/stdlib_mod.py" not in indexed         # B2
    assert "build_env/lib/runtime_mod.py" not in indexed         # B1 pyvenv.cfg
    assert "conda_env/runtime_pkg.py" not in indexed             # B1 conda-meta
    assert "pkg_meta.egg-info/entry.py" not in indexed           # B2 .egg-info
    assert "vendor/keep.c" not in indexed                        # A
    assert ".hidden/secret.py" not in indexed                    # dot 目錄


def test_root_level_python_version_dir_needs_lib_parent(tmp_path):
    root = tmp_path / "tree"
    (root / "python3.11").mkdir(parents=True)
    (root / "python3.11" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (root / "lib" / "python3.11").mkdir(parents=True)
    (root / "lib" / "python3.11" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    indexed = _indexed(load_index_scope(root))
    assert "python3.11/mod.py" in indexed, "沒有 lib/lib64 父段就不是 stdlib 佈局"
    assert "lib/python3.11/mod.py" not in indexed


def test_nested_python_tools_dir_is_not_killed(tmp_path):
    """`x/lib/python3_tools` 不是 pythonX.Y —— 不能被 B2 誤殺。"""
    root = tmp_path / "tree"
    for rel in ("x/lib/python3_tools/helper.py", "x/lib/python3.11/stdlib_mod.py"):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")
    indexed = _indexed(load_index_scope(root))
    assert indexed == {"x/lib/python3_tools/helper.py"}


def test_egg_info_at_root_and_deep(tmp_path):
    root = tmp_path / "tree"
    for rel in ("thing.egg-info/a.py", "src/other.egg-info/b.py", "src/real.py"):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")
    indexed = _indexed(load_index_scope(root))
    assert indexed == {"src/real.py"}


def test_detectors_false_disables_b_but_keeps_a_prime(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, detectors=False)
    scope = load_index_scope(tree)
    indexed = _indexed(scope)
    assert "lib/python3.11/stdlib_mod.py" in indexed          # B2 關掉
    assert "build_env/lib/runtime_mod.py" in indexed          # B1 關掉
    assert "pkg_meta.egg-info/entry.py" in indexed            # B2 關掉
    assert "site-packages/mypkg/core.py" not in indexed, "A′ 是通名,detectors 關不掉"


def test_detectors_toggle_changes_fingerprint(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, detectors=True)
    on = load_index_scope(tree).fingerprint
    _write_scope(tmp_path, monkeypatch, tree, detectors=False)
    off = load_index_scope(tree).fingerprint
    assert on != off


# ============================================================
# 三態 / 可達性
# ============================================================


def test_excluded_parent_with_include_becomes_traverse_only(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree,
                 exclude=["vendor_env/**"], include=["vendor_env/keep.c"])
    scope = load_index_scope(tree)
    assert scope.decide_dir("vendor_env") == TRAVERSE_ONLY
    assert scope.decide_dir("vendor_env/deep") == PRUNE
    assert scope.decide_dir("src") == INDEX


def test_excluded_dir_without_include_is_pruned(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, exclude=["vendor_env/**"])
    scope = load_index_scope(tree)
    assert scope.decide_dir("vendor_env") == PRUNE


def test_leading_wildcard_include_degrades_to_traverse_only(tree, tmp_path, monkeypatch):
    """空 literal prefix:保守 —— 所有被排除目錄降級 TRAVERSE_ONLY,並且要有警告。"""
    _write_scope(tmp_path, monkeypatch, tree,
                 exclude=["toolchain_x/**"], include=["**/registers/**"])
    scope = load_index_scope(tree)
    assert scope.decide_dir("toolchain_x") == TRAVERSE_ONLY
    assert scope.decide_dir("toolchain_x/arc/lib/src/stl") == TRAVERSE_ONLY
    assert scope.warnings, "空 prefix include 必須有警告"
    assert any("prefix" in line or "TRAVERSE_ONLY" in line for line in scope.stats_lines())
    indexed = _indexed(scope)
    assert "toolchain_x/arc/lldbac/lib/registers/regs.c" in indexed
    assert "toolchain_x/arc/lib/src/stl/vector.h" not in indexed


def test_layer_a_dir_is_pruned_even_with_include(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, include=["vendor/keep.c"])
    assert load_index_scope(tree).decide_dir("vendor") == PRUNE


# ============================================================
# allowlist
# ============================================================


def test_allowlist_indexes_only_listed_paths(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, mode="allowlist",
                 include=["src/**", "notes.txt"])
    scope = load_index_scope(tree)
    indexed = _indexed(scope)
    assert indexed == {"src/core/engine.c", "src/core/engine.h", "notes.txt"}


def test_allowlist_ancestor_chain_is_traverse_only(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, mode="allowlist",
                 include=["toolchain_x/arc/lldbac/lib/registers/**"])
    scope = load_index_scope(tree)
    assert scope.decide_dir("toolchain_x") == TRAVERSE_ONLY
    assert scope.decide_dir("toolchain_x/arc") == TRAVERSE_ONLY
    assert scope.decide_dir("toolchain_x/arc/lldbac/lib/registers") == TRAVERSE_ONLY
    assert scope.decide_dir("docs") == PRUNE


def test_allowlist_with_exclude_fails_loud(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, mode="allowlist",
                 include=["src/**"], exclude=["src/core/**"])
    with pytest.raises(IndexScopeError) as exc:
        load_scope_config(tree)
    assert "allowlist" in str(exc.value) and "denylist" in str(exc.value)


# ============================================================
# Loader:存在/不存在、schema、權限、衛生
# ============================================================


def test_missing_scope_file_is_normal_default(tree, tmp_path, monkeypatch):
    monkeypatch.setenv("AICODE_INDEX_SCOPE_FILE", str(tmp_path / "nope.json"))
    cfg = load_scope_config(tree)
    assert cfg.mode == "denylist" and cfg.detectors is True
    assert cfg.include == () and cfg.exclude == ()
    assert cfg.scope_file_present is False and cfg.selector_matched is False
    assert "C: no matching selector" not in load_index_scope(tree).stats_lines()


def test_broken_json_fails_loud(tree, tmp_path, monkeypatch):
    _write_raw_scope(tmp_path, monkeypatch, "{not json")
    with pytest.raises(IndexScopeError):
        load_scope_config(tree)


@pytest.mark.parametrize("payload", [
    {"schema_version": 2, "roots": []},
    {"schema_version": 1, "roots": [], "extra_key": 1},
    {"schema_version": 1, "roots": {}},
    {"schema_version": 1, "roots": [{"root": "relative/path"}]},
    {"schema_version": 1, "roots": [{"root": "/abs", "mode": "whitelist"}]},
    {"schema_version": 1, "roots": [{"root": "/abs", "detectors": "yes"}]},
    {"schema_version": 1, "roots": [{"root": "/abs", "unknown": 1}]},
])
def test_schema_violations_fail_loud(tree, tmp_path, monkeypatch, payload):
    _write_raw_scope(tmp_path, monkeypatch, payload)
    with pytest.raises(IndexScopeError):
        load_scope_config(tree)


def test_duplicate_selector_fails_loud(tree, tmp_path, monkeypatch):
    _write_raw_scope(tmp_path, monkeypatch, {
        "schema_version": 1,
        "roots": [{"root": str(tree)}, {"root": str(tree) + "/."}],
    })
    with pytest.raises(IndexScopeError) as exc:
        load_scope_config(tree)
    assert "重複" in str(exc.value)


def test_no_matching_selector_is_not_an_error(tree, tmp_path, monkeypatch):
    _write_raw_scope(tmp_path, monkeypatch, {
        "schema_version": 1,
        "roots": [{"root": str(tmp_path / "some_other_tree"), "exclude": ["src/**"]}],
    })
    scope = load_index_scope(tree)
    assert scope.selector_matched is False
    assert "C: no matching selector" in scope.stats_lines()
    assert "src/core/engine.c" in _indexed(scope), "沒匹配到就不該套用那組規則"


@pytest.mark.parametrize("pattern", [
    "!keep.c", "../escape/**", "  ", "nul\x00byte",
    "[z-a]/**", "docs/[9-0]*.md",
], ids=[
    "hygiene-negation-bang", "hygiene-dotdot-escape", "hygiene-whitespace-only",
    "hygiene-nul-byte", "uncompilable-reversed-range", "uncompilable-reversed-digit-range",
])
def test_bad_patterns_fail_as_index_scope_error(tree, tmp_path, monkeypatch, pattern):
    """壞 pattern 一律以 IndexScopeError fail-loud,不得讓底層例外漏出去。

    hygiene-*(原 test_pattern_hygiene_fails_loud):衛生檢查 —— `!` 否定、`..` 逃逸、
    純空白、NUL byte。
    uncompilable-*(原 test_uncompilable_patterns_fail_as_index_scope_error):pathspec
    編不過的 pattern(反向字元範圍)也必須包成 IndexScopeError;訊息不得帶 pattern 內容,
    那一條由 test_uncompilable_pattern_error_never_leaks_pattern_content 守。
    """
    _write_scope(tmp_path, monkeypatch, tree, exclude=[pattern])
    with pytest.raises(IndexScopeError):
        load_scope_config(tree)


def test_pattern_length_limit_fails_loud(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, exclude=["a" * 513])
    with pytest.raises(IndexScopeError) as exc:
        load_scope_config(tree)
    assert "過長" in str(exc.value)


def test_pattern_count_limit_fails_loud(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree,
                 exclude=[f"dir_{i}/**" for i in range(201)])
    with pytest.raises(IndexScopeError) as exc:
        load_scope_config(tree)
    assert "太多" in str(exc.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 權限實檢")
def test_world_readable_scope_file_fails_loud(tree, tmp_path, monkeypatch):
    path = _write_scope(tmp_path, monkeypatch, tree, exclude=["src/**"])
    os.chmod(path, 0o644)
    with pytest.raises(IndexScopeError) as exc:
        load_scope_config(tree)
    assert "chmod 600" in str(exc.value)


def test_selector_matching_uses_normcase(tree, tmp_path, monkeypatch):
    """Windows 上 selector 比對要吃 normcase;POSIX 上大小寫仍然有意義。"""
    _write_raw_scope(tmp_path, monkeypatch, {
        "schema_version": 1,
        "roots": [{"root": str(tree).upper(), "exclude": ["src/**"]}],
    })
    cfg = load_scope_config(tree)
    expected = os.path.normcase(str(tree)) == os.path.normcase(str(tree).upper())
    assert cfg.selector_matched is expected


# ============================================================
# Matcher 方言
# ============================================================


@pytest.mark.parametrize("pattern,path,expected", [
    # 實測 pathspec 的目錄尾斜線行為 —— 這三條是方言的定義向量
    ("vendor_env/**", "vendor_env", False),
    ("vendor_env/**", "vendor_env/", True),
    ("vendor_env/**", "vendor_env/keep.c", True),
    ("vendor_env/keep.c", "vendor_env/keep.c", True),
    ("vendor_env/keep.c", "vendor_env/keepXc", False),
    ("toolchain_x/arc/lib/src/stl/**", "toolchain_x/arc/lib/src/stl/vector.h", True),
    ("toolchain_x/arc/lib/src/stl/**", "toolchain_x/arc/lib/src/other.h", False),
    ("**/registers/**", "a/b/registers/regs.c", True),
    ("**/registers/**", "registers/regs.c", True),
    ("*.tmp", "deep/nested/x.tmp", True),
    ("/src/**", "src/core/engine.c", True),
    ("/src/**", "other/src/core/engine.c", False),
    ("src", "src/core/engine.c", True),
    ("src", "src/", True),
    ("SRC/**", "src/core/engine.c", False),          # case-sensitive
])
def test_matcher_vectors(pattern, path, expected):
    assert bool(compile_pattern(pattern).match(path)) is expected


def test_backslash_patterns_are_normalized(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, exclude=["src\\core\\**"])
    indexed = _indexed(load_index_scope(tree))
    assert "src/core/engine.c" not in indexed
    assert "docs/notes.md" in indexed


@pytest.mark.parametrize("pattern,expected", [
    ("vendor_env/**", "vendor_env"),
    ("vendor_env/keep.c", "vendor_env/keep.c"),
    ("a/b/c/**/d", "a/b/c"),
    ("**/registers/**", None),
    ("*.tmp", None),
    ("/src/**", "src"),
])
def test_literal_prefix(pattern, expected):
    assert literal_prefix(pattern) == expected


# ============================================================
# Symlink / containment
# ============================================================


def test_symlinked_dir_escape_is_dropped_and_counted(tree, tmp_path):
    outside = tmp_path / "outside_tree"
    (outside / "pkg").mkdir(parents=True)
    (outside / "pkg" / "leak.c").write_text("int leak(void);\n", encoding="utf-8")
    try:
        (tree / "linked_pkg").symlink_to(outside / "pkg", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("這個環境不能建 symlink")

    scope = load_index_scope(tree)
    indexed = _indexed(scope)
    assert not any(rel.startswith("linked_pkg") for rel in indexed)
    assert "symlink-escape skipped: 1" in scope.stats_lines()


def test_symlink_inside_root_is_not_counted_as_escape(tree, tmp_path):
    """指向 root 內的 symlink 不算逃逸(不進 escape 計數)。

    內容仍然不會被索引 —— os.walk(followlinks=False) 本來就不遞迴進 symlink
    目錄,committed 版本也是這個行為。這裡守的是「別把它誤判成逃逸」。
    """
    (tree / "src" / "shared").mkdir()
    (tree / "src" / "shared" / "shared_mod.c").write_text("int s(void);\n", encoding="utf-8")
    try:
        (tree / "alias_dir").symlink_to(tree / "src" / "shared", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("這個環境不能建 symlink")
    scope = load_index_scope(tree)
    indexed = _indexed(scope)
    assert "src/shared/shared_mod.c" in indexed
    assert "symlink-escape skipped: 0" in scope.stats_lines()


# ============================================================
# list_dir / grep 不受影響
# ============================================================


def test_list_dir_still_shows_index_excluded_dirs(tree):
    from agent_tools import ToolExecutor

    listing = ToolExecutor(str(tree)).list_files(".", depth=1)
    assert "site-packages" in listing
    assert "build_env" in listing
    indexed = _indexed(load_index_scope(tree))
    assert not any(rel.startswith("site-packages/") for rel in indexed)


def test_index_scope_is_not_wired_into_grep_or_list_dir():
    for name in ("agent_tools.py", "utils.py"):
        src = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "index_scope" not in src, f"{name} 不該碰索引範圍 —— grep/list_dir 要保持不變"


def test_index_artifacts_never_enter_the_index(tree):
    from config import CODE_RAG_CACHE_FILE

    scope = load_index_scope(tree)
    for name in index_scope.INDEX_ARTIFACT_FILES:
        (tree / name).write_text("{}", encoding="utf-8")
        assert scope.should_index_file(name) is False, name
    assert CODE_RAG_CACHE_FILE in index_scope.INDEX_ARTIFACT_FILES
    assert not any(rel in index_scope.INDEX_ARTIFACT_FILES for rel in _indexed(scope))


def test_layer_a_frozen_at_nineteen_names():
    assert len(config.IGNORED_DIRS) == 19, "Layer A 是凍結的:一字不加"


def test_stats_lines_can_hide_zero_counts(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, exclude=["vendor_env/**"])
    scope = load_index_scope(tree)
    _indexed(scope)
    quiet = scope.stats_lines(include_zero=False)
    assert "traverse-only dirs: 0" not in quiet
    assert "C#1 dirs: 1" in quiet
    assert "traverse-only dirs: 0" in scope.stats_lines()


def test_build_index_summary_is_counts_only(tree, tmp_path, monkeypatch, capsys,
                                            clean_scan_cache):
    code_rag = clean_scan_cache
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", False)
    _write_scope(tmp_path, monkeypatch, tree, exclude=["vendor_env/**"])
    rag = code_rag.CodeRAG(str(tree))
    monkeypatch.setattr(rag, "_get_embedding", lambda _text: [1.0, 0.0])
    monkeypatch.setattr(
        rag, "_embed_texts_batched", lambda texts: [[1.0, 0.0]] * len(texts)
    )
    rag.build_index(verbose=True)
    out = capsys.readouterr().out
    assert "index scope" in out
    for secret in ("vendor_env", "site-packages", "engine.c", str(tree)):
        assert secret not in out, f"建索引摘要洩漏路徑: {secret}"


def test_build_index_summary_silent_on_default_deployment(tmp_path, monkeypatch, capsys,
                                                          clean_scan_cache):
    code_rag = clean_scan_cache
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", False)
    root = tmp_path / "plain_tree"
    (root / "src").mkdir(parents=True)
    (root / "src" / "mod.py").write_text("def mod(): pass\n", encoding="utf-8")
    rag = code_rag.CodeRAG(str(root))
    monkeypatch.setattr(rag, "_get_embedding", lambda _text: [1.0, 0.0])
    monkeypatch.setattr(
        rag, "_embed_texts_batched", lambda texts: [[1.0, 0.0]] * len(texts)
    )
    rag.build_index(verbose=True)
    assert "index scope" not in capsys.readouterr().out


# ============================================================
# fingerprint
# ============================================================


def test_fingerprint_changes_with_patterns(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, exclude=["a/**"])
    first = load_index_scope(tree).fingerprint
    _write_scope(tmp_path, monkeypatch, tree, exclude=["b/**"])
    assert load_index_scope(tree).fingerprint != first


def test_fingerprint_is_order_sensitive(tree, tmp_path, monkeypatch):
    _write_scope(tmp_path, monkeypatch, tree, exclude=["a/**", "b/**"])
    first = load_index_scope(tree).fingerprint
    _write_scope(tmp_path, monkeypatch, tree, exclude=["b/**", "a/**"])
    assert load_index_scope(tree).fingerprint != first


def test_fingerprint_tracks_code_extensions(tree, monkeypatch):
    first = load_index_scope(tree).fingerprint
    monkeypatch.setattr(index_scope, "CODE_EXTENSIONS", set(index_scope.CODE_EXTENSIONS) | {".zig"})
    assert load_index_scope(tree).fingerprint != first


# ============================================================
# 快取遷移(§7)
# ============================================================


@pytest.fixture()
def clean_scan_cache():
    """回傳 code_rag 模組並在前後清掉掃描快取(index_scope 區段的測試以參數取用)。"""
    code_rag._INDEX_SCAN_CACHE.clear()
    yield code_rag
    code_rag._INDEX_SCAN_CACHE.clear()


def test_scan_cache_fast_path_requires_matching_fingerprint(tree, clean_scan_cache):
    import time as _time

    code_rag = clean_scan_cache
    rag = code_rag.CodeRAG(str(tree))
    code_rag._INDEX_SCAN_CACHE[(str(rag.folder), "fingerprint-from-another-scope")] = {
        "entries": {"vendor/keep.c": "stale-hash"},
        "timestamp": _time.time(),
    }
    files = rag._scan_code_files()
    assert "vendor/keep.c" not in files
    assert "src/core/engine.c" in files, "fingerprint 不符就該重掃,不是回空的"


def test_cached_paths_are_refiltered_through_should_index_file(tree, clean_scan_cache):
    import time as _time

    code_rag = clean_scan_cache
    rag = code_rag.CodeRAG(str(tree))
    code_rag._INDEX_SCAN_CACHE[(str(rag.folder), rag.scope.fingerprint)] = {
        "entries": {
            "vendor/keep.c": "h1",
            "site-packages/mypkg/core.py": "h2",
            "src/core/engine.c": "h3",
        },
        "timestamp": _time.time(),
    }
    files = rag._scan_code_files()
    assert set(files) == {"src/core/engine.c"}
    # §5-3:TTL 內 fast path 直接回快照 hash,零 compute_file_hash
    assert files["src/core/engine.c"]["hash"] == "h3"


def _write_legacy_meta(rag, *, fingerprint, paths):
    np = pytest.importorskip("numpy")
    meta = {
        "embedding_model": code_rag.EMBEDDING_MODEL,
        "folder_hash": "legacy-folder-hash",  # pre-v2 格式;schema bump 後值不再被讀
        "index": [
            {"path": p, "symbol": f"sym_{i}", "type": "function", "line": 1}
            for i, p in enumerate(paths)
        ],
    }
    if fingerprint is not None:
        meta["scope_fingerprint"] = fingerprint
    rag.cache_meta_file.write_text(json.dumps(meta), encoding="utf-8")
    rows = np.array([[float(i), 0.0] for i in range(len(paths))], dtype="float32")
    np.savez_compressed(rag.cache_emb_file, embeddings=rows)


def test_legacy_bundle_load_refused_without_fingerprint(tree, clean_scan_cache):
    code_rag = clean_scan_cache
    rag = code_rag.CodeRAG(str(tree))
    _write_legacy_meta(rag, fingerprint=None, paths=["src/core/engine.c"])

    fresh = code_rag.CodeRAG(str(tree))
    assert fresh._load_cache() is False, "沒有 scope_fingerprint 就不准整包 fast load"
    assert fresh.index == []


def test_legacy_bundle_load_refused_on_fingerprint_mismatch(tree, clean_scan_cache):
    code_rag = clean_scan_cache
    rag = code_rag.CodeRAG(str(tree))
    _write_legacy_meta(rag, fingerprint="stale", paths=["src/core/engine.c"])

    fresh = code_rag.CodeRAG(str(tree))
    assert fresh._load_cache() is False


def test_pre_v2_cache_is_rebuilt_with_stderr_reason(tree, clean_scan_cache, capsys):
    """schema v2 起舊快取一律安全重建(§5-2 + §6.2-6 合併 bump,只重建一次)。

    fingerprint 相符也不例外:pre-v2 index entry 缺 qualified_name / backend /
    generation 欄位,留著會變混血索引。stderr 必須講明原因,不得 silent。
    """
    code_rag = clean_scan_cache
    rag = code_rag.CodeRAG(str(tree))
    _write_legacy_meta(
        rag,
        fingerprint=rag.scope.fingerprint,
        paths=["vendor/keep.c", "src/core/engine.c"],
    )

    fresh = code_rag.CodeRAG(str(tree))
    assert fresh._load_cache() is False, "pre-v2 快取必須重建,不得 fast load"
    assert fresh.index == []
    err = capsys.readouterr().err
    assert "schema" in err, "重建原因必須寫到 stderr"


def test_scope_change_recomputes_membership_without_reembedding(tree, tmp_path, monkeypatch,
                                                                clean_scan_cache):
    code_rag = clean_scan_cache
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", False)
    calls: list[str] = []

    def fake_embedding(texts: list[str]) -> list[list[float]]:
        # 新契約(§5-4):build 走 _embed_texts_batched(批次)
        calls.extend(texts)
        return [[1.0, 0.0]] * len(texts)

    _write_scope(tmp_path, monkeypatch, tree, detectors=True)
    first = code_rag.CodeRAG(str(tree))
    monkeypatch.setattr(first, "_embed_texts_batched", fake_embedding)
    first.build_index(verbose=False)
    first_paths = {item["path"] for item in first.index}
    baseline_calls = len(calls)
    assert baseline_calls > 0
    assert "lib/python3.11/stdlib_mod.py" not in first_paths

    # detectors 關掉 → membership 變大,但既有檔案不該重 embed
    _write_scope(tmp_path, monkeypatch, tree, detectors=False)
    second = code_rag.CodeRAG(str(tree))
    monkeypatch.setattr(second, "_embed_texts_batched", fake_embedding)
    calls.clear()
    second.build_index(verbose=False)
    second_paths = {item["path"] for item in second.index}

    assert "lib/python3.11/stdlib_mod.py" in second_paths
    assert first_paths < second_paths
    assert 0 < len(calls) < baseline_calls, "只有新進來的檔案該付 embedding 成本"

    # 再切回來 → 多出來的檔案要退出索引
    _write_scope(tmp_path, monkeypatch, tree, detectors=True)
    third = code_rag.CodeRAG(str(tree))
    monkeypatch.setattr(third, "_embed_texts_batched", fake_embedding)
    third.build_index(verbose=False)
    assert {item["path"] for item in third.index} == first_paths


# ============================================================
# scripts/index_stats.py
# ============================================================


def _run_stats(args, env_extra=None):
    import subprocess

    env = {**os.environ, **(env_extra or {})}
    env.pop("AICODE_ROOT", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "index_stats.py"), *args],
        capture_output=True, text=True, timeout=120, env=env, check=False,
    )


def test_index_stats_requires_explicit_root():
    proc = _run_stats([])
    assert proc.returncode == 2
    assert "AICODE_ROOT" in proc.stderr


@pytest.mark.parametrize("root", ["/", "__nonexistent__"])
def test_index_stats_rejects_unsafe_roots(tmp_path, root):
    target = root if root == "/" else str(tmp_path / "nope")
    proc = _run_stats(["--root", target])
    assert proc.returncode == 2, proc.stdout


def test_index_stats_rejects_non_directory(tmp_path):
    plain = tmp_path / "plain.txt"
    plain.write_text("x\n", encoding="utf-8")
    proc = _run_stats(["--root", str(plain)])
    assert proc.returncode == 2
    assert "不是目錄" in proc.stderr


def test_index_stats_rejects_home(tmp_path):
    home = tmp_path / "fakehome"
    home.mkdir()
    proc = _run_stats(["--root", str(home)], env_extra={"HOME": str(home)})
    assert proc.returncode == 2
    assert "$HOME" in proc.stderr


def test_index_stats_default_output_is_counts_only(tree):
    proc = _run_stats(["--root", str(tree)])
    assert proc.returncode == 0, proc.stderr
    assert "indexed: " in proc.stdout
    for secret in ("site-packages/mypkg", "vendor_env", "engine.c", str(tree)):
        assert secret not in proc.stdout, f"預設輸出洩漏路徑: {secret}"


def test_index_stats_show_paths_is_opt_in(tree):
    proc = _run_stats(["--root", str(tree), "--show-paths"])
    assert proc.returncode == 0, proc.stderr
    assert "src/core/engine.c" in proc.stdout


def test_index_stats_deep_counts_symbols(tree):
    proc = _run_stats(["--root", str(tree), "--deep"])
    assert proc.returncode == 0, proc.stderr
    line = proc.stdout.splitlines()[0]
    assert line.startswith("indexed: ") and "unknown" not in line


def test_index_stats_reports_rule_hits(tree, tmp_path):
    scope_file = tmp_path / "stats-scope.json"
    scope_file.write_text(json.dumps({
        "schema_version": 1,
        "roots": [{"root": str(tree), "exclude": ["vendor_env/**"]}],
    }), encoding="utf-8")
    os.chmod(scope_file, 0o600)
    proc = _run_stats(["--root", str(tree)],
                      env_extra={"AICODE_INDEX_SCOPE_FILE": str(scope_file)})
    assert proc.returncode == 0, proc.stderr
    assert "A' dirs: 1" in proc.stdout
    assert "B1 dirs: 2" in proc.stdout
    assert "B2 dirs: 2" in proc.stdout
    assert "C#1 dirs: 1" in proc.stdout
    assert "vendor_env" not in proc.stdout


# ============================================================
# Review 修復的回歸鎖
# ============================================================


def _persisted_embeddings(rag):
    """讀回**持久化**的 per-file 向量,走 loader 而不是直接讀 meta JSON。

    dense 存檔時向量只在 .npz 裡,meta JSON 的 ``embeddings`` 鍵會被剝掉
    (同一批 330270 個向量:.npz 1.25GB vs JSON 22.9GB),直接讀 JSON 會
    KeyError。斷言要驗的是「快取裡不再有空洞」,那就得從 loader 的視角看 ——
    順帶把 .npz 還原這條路也一起驗進去。
    """
    probe = code_rag.CodeRAG(str(rag.folder))
    return probe._load_file_cache()


def _seed_cache_with_lazy_holes(rag, rel_paths, *, holes):
    """手動寫一份 per-file 快取,holes 裡的檔案 embedding 全是 []（lazy 模式的產物）。

    直接構造狀態,不依賴「第幾個檔案剛好跨過 lazy 門檻」——那個順序由 os.walk 決定,
    當回歸測試不可靠。
    """
    file_cache = {}
    for rel in rel_paths:
        path = rag.folder / rel
        symbols = [
            {"path": rel, "symbol": f"sym_{rel}_{i}", "type": "function",
             "line": i + 1, "context": "ctx"}
            for i in range(2)
        ]
        file_cache[rel] = {
            "hash": code_rag.compute_file_hash(path),
            "symbols": symbols,
            "embeddings": [[] for _ in symbols] if rel in holes else [[1.0, 0.0]] * len(symbols),
        }
    # 身分欄位從 production 的單一來源取(code_rag.cache_identity())。
    # 手抄一份的話,新增欄位時這裡會靜默落後 → cache 被拒 → 這條 regression
    # 改走 full rebuild,「還是綠的」卻不再驗 lazy embedding hole 的 backfill。
    rag.cache_meta_file.write_text(json.dumps({
        **code_rag.cache_identity(),
        "scope_fingerprint": rag.scope.fingerprint,
        "row_count": 0,
        "index": [],
        "file_cache": file_cache,
    }), encoding="utf-8")

    # fail-open 防線。少了這條,身分欄位一漂 loader 就拒絕這份 seeded cache →
    # build_index 改走 full rebuild,而 full rebuild 同樣會算出 embeddings、
    # 清掉 holes、restart 也不炸 —— 底下每一條 assertion 都還是綠的,卻完全沒有
    # 驗到 lazy embedding hole 的 backfill。這正是這條 regression 曾經退化的方式。
    loaded = rag._load_file_cache()
    assert set(loaded) == set(rel_paths), (
        "seeded cache 被 loader 拒絕了(身分欄位漂移?);"
        "這條 regression 會靜默退化成 full rebuild"
    )
    return file_cache


@pytest.mark.smoke
def test_seeded_lazy_cache_is_actually_reused_not_rebuilt(tree, monkeypatch,
                                                          clean_scan_cache):
    """焦點版:證明 build_index 真的**復用**了 seeded cache,不是重新 parse。

    seeded symbol 的名字是合成的(`sym_<path>_<i>`),真的去 parse fixture 檔案
    永遠不會產出這種名字 —— 所以它出現在 index 裡,就是「這份 cache 被採用了」
    的直接證據。整條 backfill regression 的前提就是這個,前提沒被驗證的話,
    後面測什麼都不算數。
    """
    code_rag = clean_scan_cache
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", True)
    rag = code_rag.CodeRAG(str(tree))
    kept = sorted(_indexed(rag.scope))
    _seed_cache_with_lazy_holes(rag, kept, holes=set(kept))

    monkeypatch.setattr(rag, "_embed_texts_batched",
                        lambda texts: [[1.0, 0.0]] * len(texts))
    rag.build_index(verbose=False)

    seeded = {item["symbol"] for item in rag.index if item["symbol"].startswith("sym_")}
    assert seeded, (
        "index 裡沒有任何 seeded symbol —— cache 沒被復用,這條測試已經退化成 "
        "full rebuild,不再驗 backfill"
    )


def test_dense_rebuild_backfills_lazy_embedding_holes(tree, monkeypatch, clean_scan_cache):
    """scope 縮小 → dense 模式復用 lazy 快取,空 embedding 必須被補算而不是 fail-loud。

    回歸:原本會拋 "refusing zero padding",而且失敗不寫快取 → 重啟照樣失敗,
    索引永久建不起來。索引縮小正是 index scope 的主要場景。
    """
    code_rag = clean_scan_cache
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", True)
    rag = code_rag.CodeRAG(str(tree))
    kept = sorted(_indexed(rag.scope))
    _seed_cache_with_lazy_holes(rag, kept, holes=set(kept))

    calls: list[str] = []

    def fake_batch(texts: list[str]) -> list[list[float]]:
        calls.extend(texts)
        return [[1.0, 0.0]] * len(texts)

    monkeypatch.setattr(rag, "_embed_texts_batched", fake_batch)
    rag.build_index(verbose=False)

    assert rag._lazy_embed is False
    assert rag.embeddings is not None and rag.embeddings.shape[0] == len(rag.index)
    assert calls, "空洞應該被補算"

    # 快取要被修好,否則重啟又炸一次
    persisted = _persisted_embeddings(rag)
    assert persisted, "持久化的快取讀不回來"
    holes_left = [
        rel for rel, entry in persisted.items()
        for emb in entry["embeddings"] if not emb
    ]
    assert not holes_left, f"快取仍留著空 embedding: {sorted(set(holes_left))}"

    code_rag._INDEX_SCAN_CACHE.clear()
    restart = code_rag.CodeRAG(str(tree))
    monkeypatch.setattr(restart, "_embed_texts_batched",
                        lambda texts: [[1.0, 0.0]] * len(texts))
    restart.build_index(verbose=False)          # 不得再拋


def test_lazy_index_shrunk_by_scope_still_builds(tmp_path, monkeypatch, clean_scan_cache):
    """端到端:大索引跑 lazy → 用 Layer C 縮小 → dense 重建必須成功(含重啟)。"""
    code_rag = clean_scan_cache
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED", True)
    monkeypatch.setattr(code_rag, "CODE_RAG_LAZY_EMBED_MAX_SYMBOLS", 20)

    root = tmp_path / "big_tree"
    (root / "keep").mkdir(parents=True)
    (root / "vendor_env").mkdir(parents=True)
    (root / "keep" / "a.py").write_text(
        "".join(f"def keep_{i}(): pass\n" for i in range(5)), encoding="utf-8")
    for i in range(10):
        (root / "vendor_env" / f"v{i}.py").write_text(
            "".join(f"def vend_{i}_{j}(): pass\n" for j in range(10)), encoding="utf-8")

    monkeypatch.setenv("AICODE_INDEX_SCOPE_FILE", str(tmp_path / "absent.json"))
    fake_batch = lambda texts: [[1.0, 0.0]] * len(texts)  # noqa: E731
    first = code_rag.CodeRAG(str(root))
    monkeypatch.setattr(first, "_embed_texts_batched", fake_batch)
    first.build_index(verbose=False)
    assert first._lazy_embed is True, "fixture 沒有真的觸發 lazy 模式,這條就沒在測東西"

    _write_scope(tmp_path, monkeypatch, root, exclude=["vendor_env/**"])
    code_rag._INDEX_SCAN_CACHE.clear()
    second = code_rag.CodeRAG(str(root))
    monkeypatch.setattr(second, "_embed_texts_batched", fake_batch)
    second.build_index(verbose=False)           # 回歸點:這裡原本會拋
    assert second._lazy_embed is False
    assert {item["path"] for item in second.index} == {"keep/a.py"}

    code_rag._INDEX_SCAN_CACHE.clear()
    third = code_rag.CodeRAG(str(root))
    monkeypatch.setattr(third, "_embed_texts_batched", fake_batch)
    third.build_index(verbose=False)            # 重啟也不能炸


def test_scope_file_inside_root_never_enters_index(tree, monkeypatch):
    """index-scope.json 放進 root 也不准進索引 ——「永不進 repo/輸出」是它的契約。"""
    path = tree / "index-scope.json"
    path.write_text(json.dumps(
        {"schema_version": 1, "roots": [{"root": str(tree), "exclude": ["nothing/**"]}]}
    ), encoding="utf-8")
    os.chmod(path, 0o600)
    monkeypatch.setenv("AICODE_INDEX_SCOPE_FILE", str(path))

    scope = load_index_scope(tree)
    assert scope.should_index_file("index-scope.json") is False
    assert "index-scope.json" not in _indexed(scope)

    proc = _run_stats(["--root", str(tree), "--show-paths"],
                      env_extra={"AICODE_INDEX_SCOPE_FILE": str(path)})
    assert proc.returncode == 0, proc.stderr
    assert "index-scope.json" not in proc.stdout


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="需要 POSIX FIFO")
def test_non_regular_files_are_never_read(tmp_path):
    """FIFO / socket 讀下去會永久阻塞 —— 掃描與 --deep 都不准碰。"""
    root = tmp_path / "fifo_tree"
    root.mkdir()
    (root / "real.py").write_text("def real(): pass\n", encoding="utf-8")
    os.mkfifo(root / "blocked.py")

    scope = load_index_scope(root)
    assert scope.should_index_file("blocked.py") is False
    assert _indexed(scope) == {"real.py"}

    import subprocess

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "index_stats.py"),
         "--root", str(root), "--deep"],
        capture_output=True, text=True, timeout=30, check=False,
        env={**os.environ, "AICODE_INDEX_SCOPE_FILE": str(tmp_path / "absent.json")},
    )
    assert proc.returncode == 0, proc.stderr
    assert "indexed: 1 files" in proc.stdout


def test_index_stats_refuses_stale_cached_symbol_count(tmp_path, clean_scan_cache):
    """快取過期時要印 unknown,不能報舊符號數。"""
    root = tmp_path / "stale_tree"
    root.mkdir()
    target = root / "m.py"
    target.write_text("def one(): pass\ndef two(): pass\n", encoding="utf-8")
    rag = code_rag.CodeRAG(str(root))
    _seed_cache_with_lazy_holes(rag, ["m.py"], holes=set())

    fresh = _run_stats(["--root", str(root)])
    assert "2 (cached) symbols" in fresh.stdout, fresh.stdout

    target.write_text("def one(): pass\ndef two(): pass\ndef three(): pass\n", encoding="utf-8")
    stale = _run_stats(["--root", str(root)])
    assert "unknown symbols" in stale.stdout, stale.stdout


def test_non_utf8_scope_file_fails_as_index_scope_error(tree, tmp_path, monkeypatch):
    path = tmp_path / "index-scope.json"
    path.write_bytes('{"schema_version":1,"roots":[]}'.encode("utf-16"))
    os.chmod(path, 0o600)
    monkeypatch.setenv("AICODE_INDEX_SCOPE_FILE", str(path))
    with pytest.raises(IndexScopeError):
        load_scope_config(tree)


def test_index_stats_exits_two_on_bad_scope_file(tree, tmp_path):
    bad = tmp_path / "bad-scope.json"
    bad.write_text(json.dumps(
        {"schema_version": 1, "roots": [{"root": str(tree), "exclude": ["[z-a]/**"]}]}
    ), encoding="utf-8")
    os.chmod(bad, 0o600)
    proc = _run_stats(["--root", str(tree)], env_extra={"AICODE_INDEX_SCOPE_FILE": str(bad)})
    assert proc.returncode == 2, proc.stdout
    assert "[FATAL]" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_backfill_failure_leaves_no_partial_index(tree, monkeypatch, clean_scan_cache):
    """backfill 途中 embedding server 掛掉:不能留下半成品索引。

    回歸:原本 backfill 在清理 partial index 的 try/except 之外,失敗後 self.index
    仍非空 → query() 不重建、_refresh_if_stale 也因為 _indexed_file_hashes is None
    直接 return,整個 MCP process 一路用缺 embedding 的索引降級下去。
    """
    code_rag = clean_scan_cache
    rag = code_rag.CodeRAG(str(tree))
    kept = sorted(_indexed(rag.scope))
    _seed_cache_with_lazy_holes(rag, kept, holes=set(kept))

    outage = {"on": True}

    def flaky_batch(texts):
        if outage["on"]:
            raise RuntimeError("embedding server unreachable at test URL")
        return [[1.0, 0.0]] * len(texts)

    monkeypatch.setattr(rag, "_embed_texts_batched", flaky_batch)
    with pytest.raises(RuntimeError, match="embedding server unreachable"):
        rag.build_index(verbose=False)

    assert rag.index == [], "半成品索引沒清掉 → query() 不會重建"
    assert rag.embeddings is None
    assert rag._indexed_file_hashes is None, "_refresh_if_stale 會被舊 hash 卡住"

    # server 恢復:同一個物件必須能重建,而且快取的洞要補好
    outage["on"] = False
    code_rag._INDEX_SCAN_CACHE.clear()
    rag.build_index(verbose=False)
    assert rag.index and rag.embeddings is not None
    persisted = _persisted_embeddings(rag)
    assert persisted, "持久化的快取讀不回來"
    assert not [
        rel for rel, entry in persisted.items()
        for emb in entry["embeddings"] if not emb
    ]


def test_uncompilable_pattern_error_never_leaks_pattern_content(tree, tmp_path, monkeypatch):
    """壞 pattern 的 fatal 訊息不得帶 pattern 內容 —— pattern 就是樹狀結構本身。"""
    secret = "customer_tree_delta"
    _write_scope(tmp_path, monkeypatch, tree, exclude=[f"{secret}/[z-a]/**"])

    with pytest.raises(IndexScopeError) as exc:
        load_scope_config(tree)

    message = str(exc.value)
    assert secret not in message
    assert "bad character range" not in message, "底層 re.error 的訊息也帶片段"
    assert "roots[0]" in message and "exclude[0]" in message, "要能靠位置定位"
    # from None:exception chain 上掛著 re.error 一樣會被 traceback 印出來
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__ is True

    scope_file = tmp_path / "index-scope.json"
    proc = _run_stats(["--root", str(tree)],
                      env_extra={"AICODE_INDEX_SCOPE_FILE": str(scope_file)})
    assert proc.returncode == 2
    assert secret not in proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr


# ── 原 test_file_kind_policy.py:FileKindPolicy 契約(§6 P3B,Level 1) ──

# ============================================================
# 單一來源:兩個 consumer 不得再各寫一份
# ============================================================
@pytest.mark.smoke
def test_grep_globs_cover_every_indexable_suffix():
    """grep 清單不得比索引清單窄 —— 那正是漂掉的方向。"""
    globs = set(config.GREP_DEFAULT_EXTENSIONS.split(","))
    for suffix in config.CODE_EXTENSIONS:
        assert f"*{suffix}" in globs, (
            f"{suffix} 進得了索引卻搜不到:兩份清單又漂了"
        )


@pytest.mark.smoke
def test_previously_missing_existing_formats_are_restored():
    """修新格式不能反而繼續漏舊格式(§6 P3B 明列的那一批)。"""
    globs = set(config.GREP_DEFAULT_EXTENSIONS.split(","))
    for suffix in (".cc", ".cxx", ".pyi", ".pyx", ".bash", ".txt",
                   ".mk", ".cfg", ".cmake", ".ini", ".conf", ".tcl"):
        assert f"*{suffix}" in globs, f"{suffix} 是既有格式,不得漏掉"


@pytest.mark.smoke
def test_firmware_file_kinds_are_indexable():
    for name in ("boot.s", "boot.S", "startup.asm", "link.ld", "layout.lds",
                 "soc.dts", "soc.dtsi", "regs.inc", "table.def"):
        assert policy.is_indexable(name), name


@pytest.mark.smoke
def test_uppercase_assembly_needs_its_own_case_sensitive_glob():
    """``rg -g`` 與 fallback 的 fnmatch 在 Linux 上都區分大小寫。

    canonical suffix 是小寫(``Path.suffix.lower()``),所以只產 ``*.s`` 的話,
    韌體最常見的 ``startup.S`` 會完全搜不到。
    """
    globs = config.GREP_DEFAULT_EXTENSIONS.split(",")
    assert "*.s" in globs and "*.S" in globs

    assert any(fnmatch.fnmatch("startup.S", g) for g in globs)
    assert any(fnmatch.fnmatch("startup.s", g) for g in globs)
    # 反證:只有小寫 glob 時大寫檔真的匹配不到(這條測試才有意義)。
    assert not fnmatch.fnmatch("startup.S", "*.s")


@pytest.mark.smoke
def test_extensionless_build_files_match_by_basename_rule():
    for name in ("Makefile", "makefile", "GNUmakefile", "Makefile.local",
                 "Kconfig", "Kconfig.debug"):
        assert policy.is_indexable(name), name
        assert policy.matches_basename_rule(name), name

    globs = config.GREP_DEFAULT_EXTENSIONS.split(",")
    for name in ("Makefile", "Kconfig", "Makefile.local", "Kconfig.debug"):
        assert any(fnmatch.fnmatch(name, g) for g in globs), name

    assert not policy.is_indexable("Makefilezzz")
    assert not policy.is_indexable("notes.bin")


# ============================================================
# Level 1 的承諾邊界
# ============================================================
@pytest.mark.smoke
def test_new_file_kinds_are_searchable_but_not_symbol_scanned():
    """Level 1 = grep discoverability。不宣稱它們進了 dense symbol retrieval。"""
    for name in ("startup.S", "link.ld", "soc.dts", "Makefile", "Kconfig"):
        assert policy.is_indexable(name), name
        assert not policy.enters_symbol_scan(name), (
            f"{name} 沒有 symbol parser,不該付 symbol 掃描成本"
        )


@pytest.mark.smoke
def test_doc_visibility_split_is_preserved():
    """.md/.txt 刻意留在可見範圍、排除於 symbol 掃描 —— 既有分工不得弄丟。"""
    assert ".md" in config.CODE_EXTENSIONS
    assert ".txt" in config.CODE_EXTENSIONS
    assert not policy.enters_symbol_scan("notes.md")
    assert not policy.enters_symbol_scan("todo.txt")


@pytest.mark.smoke
def test_config_formats_stay_in_the_symbol_scan():
    """.cfg/.json/.sh 本來就在掃描範圍。

    把它們一起排掉會縮小 ``_scan_code_files()``,而那份輸出同時是 bounded
    context 的 allowed_paths —— ``config/*.cfg`` 這類 gold evidence 會突然
    變成讀不到。這是成本最佳化換來檢索黑洞,不接受。
    """
    for name in ("layout.cfg", "app.json", "build.sh", "rules.mk"):
        assert policy.enters_symbol_scan(name), name


@pytest.mark.smoke
def test_index_scope_membership_uses_the_policy(tmp_path: Path):
    (tmp_path / "startup").mkdir()
    (tmp_path / "startup" / "vectors.S").write_text(".global x\n", encoding="utf-8")
    (tmp_path / "link.ld").write_text("ENTRY(reset)\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("all:\n\techo hi\n", encoding="utf-8")
    (tmp_path / "image.bin").write_bytes(b"\x00\x01")

    scope = index_scope.load_index_scope(tmp_path)
    assert scope.should_index_file("startup/vectors.S")
    assert scope.should_index_file("link.ld")
    assert scope.should_index_file("Makefile")
    assert not scope.should_index_file("image.bin")


@pytest.mark.smoke
def test_policy_rules_enter_the_scope_fingerprint(tmp_path: Path, monkeypatch):
    """規則(含 basename 規則)改了,舊 scope 快照就不能再被沿用。"""
    before = index_scope.load_index_scope(tmp_path).fingerprint

    monkeypatch.setattr(
        policy, "BASENAME_EXACT",
        frozenset(policy.BASENAME_EXACT | {"Kbuild"}),
    )
    after = index_scope.load_index_scope(tmp_path).fingerprint
    assert before != after, (
        "basename 規則改了 fingerprint 卻沒動 —— 成員資格會靜默漂移"
    )


@pytest.mark.smoke
def test_new_file_kinds_do_not_bypass_the_sandbox(tmp_path: Path):
    """沒有 parser 的檔案仍要走 _safe_path,不得因為「只是全文檢索」就放行。"""
    from agent_tools import ToolExecutor

    (tmp_path / "link.ld").write_text("ENTRY(reset_handler)\n", encoding="utf-8")
    executor = ToolExecutor(str(tmp_path))

    assert executor._safe_path("link.ld") is not None
    # containment 逃逸回 None(不是丟例外);grep/read 端據此拒絕。
    assert executor._safe_path("../outside.ld") is None
    assert executor._safe_path("/etc/passwd") is None
