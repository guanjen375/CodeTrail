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
    """把 main.py 當子行程跑。

    - 強制 stdin 為 PIPE 並餵 stdin_data，避免繼承 tty 而卡在 input()。
    - 強制 capture stdout/stderr，避免測試 runner 卡在 pipe buffer。
    - 帶 timeout，永遠不會 hang。
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # 防止子行程意外連到 Ollama 卡住（測試本身應該都是 cheap path）。
    env["AICODE_OLLAMA_BASE_URL"] = env.get("AICODE_OLLAMA_BASE_URL", "http://127.0.0.1:1")
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
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr


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


def test_qa_with_question_when_ollama_down_shows_error_not_traceback():
    """Ollama 不可連時,使用者必須看到清楚的 error,不是 traceback,不是 silent。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["AICODE_OLLAMA_BASE_URL"] = "http://127.0.0.1:1"  # 確定關閉的 port
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), "--qa", "測試"],
        capture_output=True, text=True, timeout=30,
        stdin=subprocess.DEVNULL,
        cwd=str(REPO_ROOT),
        env=env,
    )
    assert "Traceback (most recent call last)" not in r.stderr, r.stderr
    combined = r.stdout + r.stderr
    # 應該看到我們新的 ERROR 訊息(對使用者交代得很清楚)
    assert "[ERROR]" in combined
    assert "Ollama" in combined
    assert ">>> " not in r.stdout
