"""測試共用 harness：aicode wrapper / MCP server / patch runner 的重型 fixture。

不是 test module(檔名不符 `test_*.py`),pytest 不會 collect。
放在這裡的東西只有一個標準:同一段 setup 被兩個以上 test module 需要。

`aicode` 每次啟動要連開約 10 個 python 子行程(preflight),單次約 0.37s。
測試無法避開那個成本,但可以避開「每條測試重新探測一次 bash / git」——
所以 probe 一律 lru_cache 到整個 pytest 行程。
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Wrapper 的 server n_ctx 自動偵測由 tests/test_ctx_resolution.py 單獨覆蓋。
# 這批測試關心 CLI 轉發、root safety、設定合併與 wrapper 生成；給定明確 ctx 可避免
# 每個 case 都對離線的 localhost:8080 重複等待同一個 HTTP timeout。
OFFLINE_CTX = "65536"


@functools.lru_cache(maxsize=1)
def _probe_bash() -> tuple[str | None, str]:
    """探測一次可用的 bash;回傳 (path, skip 理由)。

    原本每條 aicode 測試都跑一次 `bash -lc true`(login shell,約 35ms)。
    行為完全相同,只是整個行程共用同一次探測結果。
    """
    bash = shutil.which("bash")
    if not bash:
        return None, "bash is required for the aicode wrapper tests"
    probe = subprocess.run(
        [bash, "-lc", "true"],
        capture_output=True,
        text=True,
        timeout=10,
        stdin=subprocess.DEVNULL,
    )
    if probe.returncode != 0:
        return None, f"bash is not usable in this environment: {probe.stderr.strip()}"
    return bash, ""


@functools.lru_cache(maxsize=1)
def _probe_git() -> str | None:
    return shutil.which("git")


def require_git() -> None:
    if not _probe_git():
        pytest.skip("git is required for the aicode wrapper tests")


def require_working_bash() -> str:
    bash, reason = _probe_bash()
    if bash is None:
        pytest.skip(reason)
    return bash


def bash_compatible_path(bash: str, path: Path) -> str:
    if os.name != "nt":
        return str(path)
    converted = subprocess.run(
        [bash, "-lc", 'cygpath -u "$1"', "_", str(path)],
        capture_output=True,
        text=True,
        timeout=10,
        stdin=subprocess.DEVNULL,
    )
    if converted.returncode == 0 and converted.stdout.strip():
        return converted.stdout.strip()
    return str(path)


def run_aicode_with_stub(
    tmp_path: Path,
    args: list[str],
    env_extra: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    require_git()
    bash = require_working_bash()
    aicode_script = bash_compatible_path(bash, REPO_ROOT / "aicode")

    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subdir = project / "src"
    subdir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    stub_opencode = bin_dir / "opencode"
    stub_opencode.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        ": > opencode_args.txt\n"
        "for arg in \"$@\"; do\n"
        "  printf '%s\\n' \"$arg\" >> opencode_args.txt\n"
        "done\n",
        encoding="utf-8",
    )
    stub_opencode.chmod(0o700)

    env = os.environ.copy()
    for key in ("AICODE_MODEL", "AICODE_ROOT", "OPENCODE_CONFIG"):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONIOENCODING": "utf-8",
            "AICODE_N_CTX": OFFLINE_CTX,
            "AICODE_CTX_SAFETY_DISABLE": "1",
            "AICODE_REQUIRED_MODELS_CHECK_SKIP": "1",
            # CLI forwarding tests are offline.  The canary has dedicated
            # mocked tests and must never contact a real model from pytest.
            "AICODE_TOOL_CANARY_SKIP": "1",
        }
    )
    if env_extra:
        env.update(env_extra)

    result = subprocess.run(
        [bash, aicode_script, *args],
        cwd=subdir,
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    return result, subdir / "opencode_args.txt"


def read_stub_args(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()

def contains_subsequence(items: list[str], expected: list[str]) -> bool:
    if not expected:
        return True
    width = len(expected)
    return any(items[i : i + width] == expected for i in range(len(items) - width + 1))


# ---------------------------------------------------------------------------
# aicode web / aicode attach 子指令
# ---------------------------------------------------------------------------

# web-capable opencode:`web --help` 印出 web 指令自己的 synopsis 行並 exit 0;
# 其他呼叫(含真正的 exec)記錄 args 到 cwd 的 opencode_args.txt。
OPENCODE_WEB_CAPABLE_STUB = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    'if [ "${1:-}" = "web" ] && [ "${2:-}" = "--help" ]; then\n'
    "  printf 'opencode web\\n\\nstart opencode server and open web interface\\n'\n"
    "  exit 0\n"
    "fi\n"
    ": > opencode_args.txt\n"
    'for arg in "$@"; do\n'
    "  printf '%s\\n' \"$arg\" >> opencode_args.txt\n"
    "done\n"
)

# 舊版 opencode:沒有 web 子指令,yargs 把 web 當專案 positional,`--help` 短路
# 印預設說明(**沒有** `opencode web` synopsis)且一樣 exit 0 —— 真實模擬舊版,
# 用來證明能力偵測不靠 exit code。
OPENCODE_WEB_OLD_STUB = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    'if [ "${1:-}" = "web" ] && [ "${2:-}" = "--help" ]; then\n'
    "  printf 'opencode [project]\\n\\nstart opencode tui\\n'\n"
    "  exit 0\n"
    "fi\n"
    ": > opencode_args.txt\n"
    'for arg in "$@"; do\n'
    "  printf '%s\\n' \"$arg\" >> opencode_args.txt\n"
    "done\n"
)


def run_aicode_subcmd_with_stub(
    tmp_path: Path,
    args: list[str],
    *,
    opencode_stub: str = OPENCODE_WEB_CAPABLE_STUB,
    env_extra: dict[str, str] | None = None,
    set_model: bool = True,
    tailscale_ip: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """跑 `aicode <args>`,用可注入的 opencode stub。回傳 (result, args_file)。

    跟 `run_aicode_with_stub` 不同處:預設會設好 AICODE_MODEL(web 路徑沿用模型
    解析,沒設會 fail),並允許注入 opencode stub 與 OPENCODE_SERVER_PASSWORD /
    AICODE_WEB_PORT / AICODE_ROOT 等環境變數。
    """
    require_git()
    bash = require_working_bash()
    aicode_script = bash_compatible_path(bash, REPO_ROOT / "aicode")

    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subdir = project / "src"
    subdir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    stub_opencode = bin_dir / "opencode"
    stub_opencode.write_text(opencode_stub, encoding="utf-8")
    stub_opencode.chmod(0o700)
    if tailscale_ip is not None:
        stub_tailscale = bin_dir / "tailscale"
        stub_tailscale.write_text(
            "#!/usr/bin/env bash\n"
            "if [ \"${1:-}\" = ip ] && [ \"${2:-}\" = -4 ]; then\n"
            f"  printf '%s\\n' {tailscale_ip!r}\n"
            "  exit 0\n"
            "fi\n"
            "exit 2\n",
            encoding="utf-8",
        )
        stub_tailscale.chmod(0o700)

    env = os.environ.copy()
    for key in (
        "AICODE_MODEL",
        "AICODE_ROOT",
        "OPENCODE_CONFIG",
        "AICODE_WEB_PORT",
        "AICODE_WEB_TAILSCALE_IP",
        "OPENCODE_SERVER_PASSWORD",
        "OPENCODE_SERVER_USERNAME",
    ):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONIOENCODING": "utf-8",
            "AICODE_N_CTX": OFFLINE_CTX,
            "AICODE_CTX_SAFETY_DISABLE": "1",
            "AICODE_REQUIRED_MODELS_CHECK_SKIP": "1",
            "AICODE_TOOL_CANARY_SKIP": "1",
        }
    )
    if set_model:
        env["AICODE_MODEL"] = "example-code-model:30b"
    if env_extra:
        for key, value in env_extra.items():
            env[key] = str(home) if value == "__HOME__" else value

    result = subprocess.run(
        [bash, aicode_script, *args],
        cwd=subdir,
        capture_output=True,
        text=True,
        timeout=20,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    return result, subdir / "opencode_args.txt"


# ---------------------------------------------------------------------------
# mcp_server.py 子行程:啟動 → 等 stderr 里程碑 → 收屍
#
# test_mcp_startup.py 與 test_mcp_runtime_policy.py 原本各有一份一模一樣的
# _spawn_mcp / _terminate。
# ---------------------------------------------------------------------------

MCP_READY_MARKER = "server ready, listening on stdio"


def spawn_mcp(tmp_root: Path, env_overrides: dict[str, str] | None = None) -> subprocess.Popen:
    """以 tmp_root 當 AICODE_ROOT 啟動 mcp_server.py。

    - 指向一個必定沒人聽的 llama base URL,確保子行程不會真的去打模型。
    - 給假的 AICODE_MODEL:mcp_server 啟動會 require_main_model(),沒設會 exit 3;
      主模型解析本身有 tests/test_model_resolution.py 覆蓋。
    - 即使 env_overrides 蓋掉 HOME,也要讓子行程找得到 mcp 套件 → 顯式帶 PYTHONPATH。
    """
    env = os.environ.copy()
    env["AICODE_ROOT"] = str(tmp_root)
    env["PYTHONIOENCODING"] = "utf-8"
    env["AICODE_LLAMA_BASE_URL"] = "http://127.0.0.1:65535"
    env["AICODE_MODEL"] = "example-code-model"
    env["AICODE_REQUIRED_MODELS_CHECK_SKIP"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        [p for p in sys.path if p] + [env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "mcp_server.py")],
        stdin=subprocess.PIPE,          # FastMCP 走 stdio,給它一個關著的 stdin
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        env=env,
    )


def wait_for_marker(proc: subprocess.Popen, marker: str = MCP_READY_MARKER,
                    timeout: float = 20.0) -> str:
    """讀 stderr 直到看到 marker、行程結束或 timeout;回傳累積的 stderr。"""
    end = time.time() + timeout
    buf: list[str] = []
    assert proc.stderr is not None
    os.set_blocking(proc.stderr.fileno(), False)   # 讀不到時不要整個 hang
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


def terminate_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


# ---------------------------------------------------------------------------
# in-process 重新 import mcp_server
#
# test_fs_sandbox / test_code_rag_index / test_code_rag_search_contract /
# test_repeat_guard 原本各有一份幾乎一樣的 mcp_module fixture(約 25 行 × 4)。
# 差別只在「先在 root 底下放什麼檔案」,那部分留在各自的 fixture 裡。
# ---------------------------------------------------------------------------


def import_mcp_module(monkeypatch, root: Path):
    """以 root 當 AICODE_ROOT 重新 import mcp_server,回傳該模組。

    要注意的三件事:
    1. AICODE_LLAMA_BASE_URL 指向一個關著的 port,確保 KB / CodeRAG 初始化
       不會卡在等 llama-server。
    2. mcp_server 的 module-level code 會 mutate config.PATCH_ENABLED /
       RUN_COMMAND_ENABLED / ALLOWED_COMMANDS。先用 monkeypatch 釘住原值,
       teardown 自動 restore —— 否則會污染其他測試對 config 預設值的斷言。
    3. 先把 mcp_server 從 sys.modules 拔掉才 import,確保拿到 fresh module。
       mcp.run() 只在 __main__ guard 裡呼叫,所以直接 import 是安全的。
    """
    pytest.importorskip("mcp", reason="mcp 套件未安裝;OpenCode + MCP 路線才需要")

    monkeypatch.setenv("AICODE_ROOT", str(root))
    monkeypatch.setenv("AICODE_MODEL", "example-code-model:30b")
    monkeypatch.setenv("AICODE_LLAMA_BASE_URL", "http://127.0.0.1:65535")
    monkeypatch.setenv("AICODE_REQUIRED_MODELS_CHECK_SKIP", "1")
    # 避免無關設定干擾啟動 log
    monkeypatch.setenv("AI_CODE_PATCH", "")
    monkeypatch.setenv("AI_CODE_RUN_TESTS", "")
    monkeypatch.setenv("AI_CODE_ENABLE_BUILD_COMMANDS", "")

    import config as _config

    monkeypatch.setattr(_config, "PATCH_ENABLED", _config.PATCH_ENABLED)
    monkeypatch.setattr(_config, "RUN_COMMAND_ENABLED", _config.RUN_COMMAND_ENABLED)
    monkeypatch.setattr(_config, "ALLOWED_COMMANDS", list(_config.ALLOWED_COMMANDS))

    sys.modules.pop("mcp_server", None)
    import mcp_server  # type: ignore

    return mcp_server


def tool_fn(mcp_module, name: str):
    """取出 FastMCP @mcp.tool() 包裝後的原函式。"""
    tool = getattr(mcp_module, name)
    return getattr(tool, "fn", tool)
