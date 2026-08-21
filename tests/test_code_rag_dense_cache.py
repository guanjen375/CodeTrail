#!/usr/bin/env python3
"""CodeRAG dense cache 的三個真實 bug regression(2026-08-21,真實樹實測)。

D2:``_lazy_embed`` 只看符號數,不看 embedding 是否已備齊。一份**已經完整
    embed** 的索引每次載入仍被判為 lazy(``code_rag.py`` 的
    ``total_symbols > CODE_RAG_LAZY_EMBED_MAX_SYMBOLS``),於是 dense 矩陣
    不建、查詢時再跑一次 ``_materialize_dense_index()``。

D3:``build_index()`` 結尾無條件 ``_save_cache()``,即使 0 檔變更、0 檔刪除。

D4:``_load_file_cache`` 用 ``except Exception`` 把 ``MemoryError`` 也接住,
    印成「cache meta 損壞,安全重建」。見該測試的 docstring —— 後果是永久
    刪掉全部向量。

D2+D3 疊加的實測後果:330270 個符號的樹上,meta JSON 是 22.9GB,單次查詢會
觸發 **2 次**全量回寫,每次約 100 分鐘(實測寫入速率 3.6MB/s)。
"""
from __future__ import annotations

import json
import math
import sys
import zlib
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import code_rag  # noqa: E402

pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def _clean_scan_cache():
    code_rag._INDEX_SCAN_CACHE.clear()
    yield
    code_rag._INDEX_SCAN_CACHE.clear()


def _make_repo(tmp_path: Path, n_files: int = 3, funcs_per_file: int = 3) -> Path:
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


def test_fully_embedded_index_is_not_treated_as_lazy(monkeypatch, tmp_path):
    """D2:cache 裡每個符號都有 embedding 時,重新載入不得再判為 lazy。"""
    root = _make_repo(tmp_path)

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


def test_unchanged_index_does_not_rewrite_cache(monkeypatch, tmp_path):
    """D3:0 檔變更 0 檔刪除時,build_index 不得回寫 cache。"""
    root = _make_repo(tmp_path)

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


def test_changed_file_still_writes_cache(monkeypatch, tmp_path):
    """D3 的反向防線:真的有變更時仍必須回寫,不能為了省 IO 而漏存。"""
    root = _make_repo(tmp_path)

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
    root = _make_repo(tmp_path)
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


def test_dense_save_keeps_vectors_out_of_meta_json(monkeypatch, tmp_path):
    """D5:dense 模式下向量已經在 .npz 裡,不該又以 JSON 文字存一份。

    實測:330270 符號的樹上,同一批向量在 .npz 是 1.25GB、在 meta JSON 是
    22.9GB(18 倍)。而且 .npz 從頭到尾沒有被讀回過 —— 全檔沒有 np.load,
    它唯一的用途是被算 md5 當世代 token。載入實際走的是那份 22.9GB JSON,
    光 json.load 就要 100GB 以上位址空間。
    """
    root = _make_repo(tmp_path)
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


def test_dense_cache_reloads_vectors_from_npz(monkeypatch, tmp_path):
    """D5 的另一半:既然 JSON 不再存向量,載入就必須真的從 .npz 讀回來。"""
    root = _make_repo(tmp_path)
    seed = _dense_seed(monkeypatch, root)
    expected = seed.embeddings.copy()

    reloaded = _rag(monkeypatch, root)
    reloaded.build_index(verbose=False)

    assert reloaded.embeddings is not None, "重新載入後應該直接有 dense 矩陣"
    assert reloaded.embeddings.shape == expected.shape
    assert reloaded.embeddings == pytest.approx(expected)


def test_legacy_cache_with_inline_vectors_still_loads(monkeypatch, tmp_path):
    """相容:既有的舊 cache 仍夾帶向量,不得因為新格式而失效(重建要 55 分鐘)。"""
    root = _make_repo(tmp_path)
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
    root = _make_repo(tmp_path)
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
    root = _make_repo(tmp_path)
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
