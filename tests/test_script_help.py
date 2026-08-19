"""維護腳本的 `--help` / 錯誤路徑 smoke:能 cheap return、不吐 Traceback。

從 test_cli.py 拆出(2026-08-20)。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests._harness import (
    REPO_ROOT,
)


def test_rag_help_exits_zero():
    """`python RAG.py --help` 必須能 cheap return 0。"""
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "RAG.py"), "--help"],
        capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0, f"exit={r.returncode}\n{r.stderr}"
    assert "用法" in r.stdout or "usage" in r.stdout.lower()
    assert "Traceback" not in r.stderr


def test_rag_help_lists_binary_and_image_types():
    """`python RAG.py --help` 要列出 binary/ELF/圖片副檔名,避免使用者誤以為只支援 PDF。"""
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "RAG.py"), "--help"],
        capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0
    out = r.stdout
    assert ".bin" in out, "RAG.py --help should mention .bin support"
    assert ".elf" in out, "RAG.py --help should mention .elf support"
    assert ".png" in out, "RAG.py --help should mention .png support"


def test_rag_rejects_unknown_extension_with_supported_list(tmp_path):
    """副檔名不支援時,error 訊息要列出支援清單(包含 binary/ELF),不能只說 pdf/md/txt。"""
    bad_file = tmp_path / "garbage.xyz"
    bad_file.write_text("hi")
    kb_file = tmp_path / "kb.json"
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "RAG.py"), str(bad_file), str(kb_file)],
        capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "不支援" in out, out
    # error 訊息要提到三類副檔名
    assert ".pdf" in out, out
    assert ".bin" in out, out
    assert ".elf" in out, out
    assert "Traceback" not in r.stderr


def test_index_stats_help_exits_zero():
    """`python scripts/index_stats.py --help` 必須 cheap return 0(唯讀、離線)。"""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "index_stats.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--show-paths" in proc.stdout


def test_kb_ab_compare_help_exits_zero():
    """`python scripts/kb_ab_compare.py --help` 必須 cheap return 0(離線、不載模型)。"""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "kb_ab_compare.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--questions" in proc.stdout
    assert "Traceback" not in proc.stderr


def test_kb_ab_compare_rejects_two_kbs_in_one_directory(tmp_path: Path):
    """同目錄兩份 KB 會互相覆蓋 knowledge_emb.npz，必須擋下（不是靜默比錯）。"""
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    for path in (first, second):
        path.write_text(json.dumps({"metadata": {}, "chunks": []}), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "kb_ab_compare.py"),
            str(first),
            str(second),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert proc.returncode != 0
    assert "同一個目錄" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_run_eval_help_exits_zero():
    """`python eval/run_eval.py --help` 必須能 cheap return 0,不需要 llama-server。"""
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "eval" / "run_eval.py"), "--help"],
        capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0, f"exit={r.returncode}\n{r.stderr}"
    assert "usage" in r.stdout.lower() or "用法" in r.stdout
    assert "Traceback" not in r.stderr


def test_run_retrieval_eval_help_exits_zero():
    """離線 retrieval harness 的 help 不得載入模型或連 server。"""
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "eval" / "run_retrieval_eval.py"), "--help"],
        capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0, f"exit={r.returncode}\n{r.stderr}"
    assert "usage" in r.stdout.lower() or "用法" in r.stdout
    assert "Traceback" not in r.stderr
