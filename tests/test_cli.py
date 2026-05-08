"""CLI smoke tests: 確保最基本的 invoke path 不會 crash。

不需要 Ollama 或任何外部服務即可通過。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(args, timeout=30, stdin_data: str | None = ""):
    """把 main.py 當子行程跑，避免污染當前 process（main.py import 時會改 stdout encoding）。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
        input=stdin_data,
        env=env,
    )


def test_help_exits_zero_with_usage():
    """`python main.py --help` 必須 return 0、印出 usage、不啟動互動模式。"""
    r = _run(["--help"], timeout=15)
    assert r.returncode == 0, f"exit={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    out = r.stdout + r.stderr
    assert ("用法:" in out) or ("usage:" in out.lower()), out
    # 不應該掉進互動 prompt
    assert ">>> " not in r.stdout
    # 沒有未處理 traceback 噴到 stderr
    assert "Traceback (most recent call last)" not in r.stderr, r.stderr


def test_short_help_flag():
    """-h 也要等價。"""
    r = _run(["-h"], timeout=15)
    assert r.returncode == 0
    assert "用法:" in r.stdout or "usage:" in r.stdout.lower()


def test_unknown_flag_warns_but_does_not_crash():
    """未知 flag 該 warn 但 --help 也帶上後仍能正常離開。"""
    r = _run(["--bogus-flag-that-does-not-exist", "--help"], timeout=15)
    assert r.returncode == 0
    # 警告訊息出現在 stdout
    assert "未知參數" in r.stdout


def test_qa_no_question_non_tty_no_crash():
    """非互動環境下沒帶問題的 --qa 應該優雅退出，不該 EOFError 噴 traceback。"""
    r = _run(["--qa"], timeout=20, stdin_data="")
    # exit code 不要求嚴格，只要不 crash 出 traceback
    assert "Traceback (most recent call last)" not in r.stderr, r.stderr
    assert "EOFError" not in r.stderr, r.stderr


def test_main_module_import_does_not_run_interactive():
    """import main 不應該掉進掃描或互動模式（讓 pytest 安全 collect）。"""
    code = (
        "import sys, importlib;"
        f"sys.path.insert(0, {str(REPO_ROOT)!r});"
        "m = importlib.import_module('main');"
        "assert hasattr(m, 'main')"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert ">>> " not in r.stdout
