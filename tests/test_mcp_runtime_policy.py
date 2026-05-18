"""mcp_server.py 的 runtime policy:patch / run_command / build commands 都應該尊重 env。

Review 找到的 bug: 舊版 mcp_server.py 無條件 force-on PATCH_ENABLED / RUN_COMMAND_ENABLED,
使用者設 AI_CODE_PATCH=0 也會被吞掉。Build commands (make/cmake/ninja/meson/bazel) 也是無條件
掛白名單,「分析陌生 repo」時模型可一鍵跑 make = 任意程式碼執行。

修正後:
- AI_CODE_PATCH / AI_CODE_RUN_TESTS 預設 ON 但讀 env(設 0 真會關)
- AI_CODE_ENABLE_BUILD_COMMANDS 預設 OFF,要顯式打開
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

mcp = pytest.importorskip("mcp", reason="mcp 套件未安裝;OpenCode + MCP 路線才需要")


def _spawn_mcp(tmp_root: Path, env_overrides: dict[str, str] | None = None) -> subprocess.Popen:
    env = os.environ.copy()
    env["AICODE_ROOT"] = str(tmp_root)
    env["PYTHONIOENCODING"] = "utf-8"
    env["AICODE_OLLAMA_BASE_URL"] = "http://127.0.0.1:1"
    # mcp_server.py 啟動時會 require_main_model(); 沒設會 fail-loud exit 3。
    # 這個 test file 在意的是 patch / run_command policy, 不是 model resolution,
    # 所以給一個合理的假值讓 server 起得來。AICODE_MODEL resolution 邏輯有
    # tests/test_resolve_main_model.py + tests/test_config.py 各自覆蓋。
    env.setdefault("AICODE_MODEL", "example-code-model:30b")
    env["PYTHONPATH"] = os.pathsep.join(
        [p for p in sys.path if p]
        + [env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "mcp_server.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        env=env,
    )


def _wait_for_marker(proc: subprocess.Popen, marker: str, timeout: float = 20.0) -> str:
    end = time.time() + timeout
    buf: list[str] = []
    assert proc.stderr is not None
    os.set_blocking(proc.stderr.fileno(), False)
    while time.time() < end:
        chunk = proc.stderr.read(4096)
        if chunk:
            buf.append(chunk.decode("utf-8", errors="replace"))
            combined = "".join(buf)
            if marker in combined:
                return combined
        elif proc.poll() is not None:
            break
        else:
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


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "fakeproj"
    p.mkdir()
    (p / "README.md").write_text("# fake\n", encoding="utf-8")
    return p


def _drop_env(*names: str) -> dict[str, str]:
    """產生 env_overrides 把指定 env 清掉(避免從父行程繼承)。"""
    return {name: "" for name in names}


def test_defaults_keep_patch_and_run_command_on(project: Path):
    """沒設 env 時,OpenCode runtime 主場景:patch + run_command 預設 ON。"""
    proc = _spawn_mcp(
        project,
        env_overrides=_drop_env(
            "AI_CODE_PATCH", "AI_CODE_RUN_TESTS", "AI_CODE_ENABLE_BUILD_COMMANDS"
        ),
    )
    try:
        out = _wait_for_marker(proc, "server ready, listening on stdio")
        assert "PATCH_ENABLED = True" in out, out[-2000:]
        assert "RUN_COMMAND_ENABLED = True" in out, out[-2000:]
        # build 命令預設不掛
        assert "build 命令未掛白名單" in out, out[-2000:]
    finally:
        _terminate(proc)


def test_explicit_patch_zero_disables_patch(project: Path):
    """AI_CODE_PATCH=0 必須真的關 PATCH_ENABLED(舊版會被 force-on 吞掉)。"""
    proc = _spawn_mcp(
        project,
        env_overrides={
            "AI_CODE_PATCH": "0",
            "AI_CODE_RUN_TESTS": "",
            "AI_CODE_ENABLE_BUILD_COMMANDS": "",
        },
    )
    try:
        out = _wait_for_marker(proc, "server ready, listening on stdio")
        assert "PATCH_ENABLED = False" in out, (
            f"AI_CODE_PATCH=0 沒生效 — 是不是又被 force-on 吞掉?\n{out[-2000:]}"
        )
    finally:
        _terminate(proc)


def test_explicit_run_tests_zero_disables_run_command(project: Path):
    proc = _spawn_mcp(
        project,
        env_overrides={
            "AI_CODE_RUN_TESTS": "0",
            "AI_CODE_PATCH": "",
            "AI_CODE_ENABLE_BUILD_COMMANDS": "",
        },
    )
    try:
        out = _wait_for_marker(proc, "server ready, listening on stdio")
        assert "RUN_COMMAND_ENABLED = False" in out, out[-2000:]
    finally:
        _terminate(proc)


def test_build_commands_off_by_default(project: Path):
    """預設 build 命令不掛白名單。"""
    proc = _spawn_mcp(
        project,
        env_overrides=_drop_env(
            "AI_CODE_PATCH", "AI_CODE_RUN_TESTS", "AI_CODE_ENABLE_BUILD_COMMANDS"
        ),
    )
    try:
        out = _wait_for_marker(proc, "server ready, listening on stdio")
        assert "build 命令未掛白名單" in out, out[-2000:]
        # 反向確認:不應該印「已 append build 命令」
        assert "已 append build 命令" not in out, out[-2000:]
    finally:
        _terminate(proc)


def test_build_commands_opt_in(project: Path):
    """AI_CODE_ENABLE_BUILD_COMMANDS=1 才會掛 make/cmake/ninja/meson/bazel。"""
    proc = _spawn_mcp(
        project,
        env_overrides={
            "AI_CODE_ENABLE_BUILD_COMMANDS": "1",
            "AI_CODE_PATCH": "",
            "AI_CODE_RUN_TESTS": "",
        },
    )
    try:
        out = _wait_for_marker(proc, "server ready, listening on stdio")
        assert "已 append build 命令" in out, out[-2000:]
        # 名單裡至少要有 make 和 cmake
        assert "make" in out and "cmake" in out, out[-2000:]
    finally:
        _terminate(proc)
