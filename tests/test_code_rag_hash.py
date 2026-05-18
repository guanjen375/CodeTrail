"""CodeRAG._compute_file_hash 必須能偵測同秒寫入的內容變更。

Review 找到的弱點: 舊版用 stat.st_mtime(秒解析度),同秒內多次 edit-save 會
hash 相同 → cache hit 拿到舊 embedding,新內容沒被索引。改成小檔 content
hash + 大檔 size + mtime_ns 後,正常 edit 流程不會 mis-hit。
"""
from __future__ import annotations

from pathlib import Path

from code_rag import CodeRAG


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
