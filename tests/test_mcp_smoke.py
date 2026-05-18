"""mcp_server.py 啟動 smoke test。

驗證主產品路線(OpenCode + Ollama + MCP)的 server entry 至少能初始化:
import 成功 → root 檢查通過 → KnowledgeBase / CodeRAG / ToolExecutor 構造好 →
FastMCP 實例就緒。**不**需要 Ollama、不下載模型、不跑 inference。

實作方式:subprocess 啟動,看 stderr 印出我們已知的初始化里程碑 log,然後 terminate。

mcp 套件未安裝時 skip(CI 會單獨裝)。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# CI 沒裝 mcp 時 skip；日常 OpenCode 路線需要 mcp。
mcp = pytest.importorskip("mcp", reason="mcp 套件未安裝;OpenCode + MCP 路線才需要")


def _spawn_mcp(tmp_root: Path, env_overrides: dict[str, str] | None = None) -> subprocess.Popen:
    env = os.environ.copy()
    env["AICODE_ROOT"] = str(tmp_root)
    env["PYTHONIOENCODING"] = "utf-8"
    # 確保子行程不會跑到 Ollama 卡住(雖然 smoke 階段不會 LLM call,但保險起見)
    env["AICODE_OLLAMA_BASE_URL"] = "http://127.0.0.1:1"
    # mcp_server.py 啟動時會 require_main_model(); 沒設會 fail-loud exit 3。
    # smoke test 不在意主模型實際存不存在, 給個合理假值即可。
    env.setdefault("AICODE_MODEL", "qwen3-coder:30b")
    # 即使 env_overrides 覆寫 HOME,也要讓子行程能找到 mcp 套件 — 把當前
    # Python 的 sys.path 顯式塞進 PYTHONPATH(包含 user site-packages)
    env["PYTHONPATH"] = os.pathsep.join(
        [p for p in sys.path if p and p != ""]
        + [env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "mcp_server.py")],
        stdin=subprocess.PIPE,         # FastMCP 走 stdio,給它一個關著的 stdin
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        env=env,
    )


def _wait_for_stderr(proc: subprocess.Popen, marker: str, timeout: float = 12.0) -> str:
    """讀 stderr 直到看到 marker 或 timeout。回傳累積 stderr 內容。"""
    end = time.time() + timeout
    buf = []
    assert proc.stderr is not None
    # 把 stderr 設成 non-blocking,避免讀不到時整個 hang
    os.set_blocking(proc.stderr.fileno(), False)
    while time.time() < end:
        chunk = proc.stderr.read(4096)
        if chunk:
            text = chunk.decode("utf-8", errors="replace")
            buf.append(text)
            combined = "".join(buf)
            if marker in combined:
                return combined
        else:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
    return "".join(buf)


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


def test_mcp_server_initializes_and_is_listenable(tmp_path: Path):
    """正常 root 下,mcp_server.py 能走完所有初始化並進入 listening 狀態。"""
    project = tmp_path / "fakeproj"
    project.mkdir()
    (project / "README.md").write_text("# fake\n", encoding="utf-8")

    proc = _spawn_mcp(project)
    try:
        # mcp_server.py 在 mcp.run() 前的最後一條 stderr 是 "[MCP] server ready, listening on stdio."
        # 看到那條就代表全部 init OK。
        stderr = _wait_for_stderr(proc, "server ready, listening on stdio", timeout=20.0)
        assert "server ready, listening on stdio" in stderr, (
            f"mcp_server.py 沒走到 listening 階段。stderr 摘錄:\n{stderr[-2000:]}"
        )
        # 不該有 traceback / FATAL / ImportError
        assert "Traceback" not in stderr, stderr[-2000:]
        assert "FATAL" not in stderr, stderr[-2000:]
        assert "ModuleNotFoundError" not in stderr, stderr[-2000:]
    finally:
        _terminate(proc)


def test_mcp_server_rejects_root_slash():
    """root='/' 必須在啟動階段被拒絕(配 stderr [FATAL])。"""
    proc = _spawn_mcp(Path("/"))
    try:
        # 不需要 wait listening — 期待它直接 sys.exit(2)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        out = (stderr or b"").decode("utf-8", errors="replace")
        assert proc.returncode != 0, f"應該 exit≠0,實際 {proc.returncode}\n{out}"
        assert "FATAL" in out and ("/" in out or "AICODE_ROOT" in out)
    finally:
        _terminate(proc)


def test_mcp_server_rejects_home_root(tmp_path: Path):
    """root=$HOME 預設拒絕。"""
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    proc = _spawn_mcp(fake_home, env_overrides={"HOME": str(fake_home)})
    try:
        try:
            _, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate()
        out = (stderr or b"").decode("utf-8", errors="replace")
        assert proc.returncode != 0, out
        assert "$HOME" in out or "拒絕" in out
    finally:
        _terminate(proc)


def test_mcp_server_home_override_allows_startup(tmp_path: Path):
    """設了 AI_CODE_ALLOW_HOME_ROOT=1 後,$HOME 可以啟動到 listening。"""
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    (fake_home / "x.txt").write_text("x\n", encoding="utf-8")
    proc = _spawn_mcp(
        fake_home,
        env_overrides={"HOME": str(fake_home), "AI_CODE_ALLOW_HOME_ROOT": "1"},
    )
    try:
        stderr = _wait_for_stderr(proc, "server ready, listening on stdio", timeout=20.0)
        assert "server ready, listening on stdio" in stderr, stderr[-2000:]
    finally:
        _terminate(proc)
