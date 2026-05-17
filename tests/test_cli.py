"""Maintenance script smoke tests: 確保基本 help / error path 不會 crash。

不需要 Ollama 或任何外部服務即可通過。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


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


def test_run_eval_help_exits_zero():
    """`python eval/run_eval.py --help` 必須能 cheap return 0,不需要 Ollama。"""
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "eval" / "run_eval.py"), "--help"],
        capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0, f"exit={r.returncode}\n{r.stderr}"
    assert "usage" in r.stdout.lower() or "用法" in r.stdout
    assert "Traceback" not in r.stderr
