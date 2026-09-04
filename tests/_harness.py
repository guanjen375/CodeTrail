"""測試共用 harness：aicode wrapper / MCP server / patch runner 的重型 fixture。

不是 test module(檔名不符 `test_*.py`),pytest 不會 collect。
放在這裡的東西只有一個標準:同一段 setup 被兩個以上 test module 需要。

`aicode` 每次啟動要連開約 10 個 python 子行程(preflight),單次約 0.37s。
測試無法避開那個成本,但可以避開「每條測試重新探測一次 bash / git」——
所以 probe 一律 lru_cache 到整個 pytest 行程。
"""
from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

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



# CodeTrail 客戶端 stub:記錄它被傳了什麼參數,不真的起 engine。
# 這是 `AICODE_CLIENT_ENTRY` 這個 seam 的唯一用途 —— 與舊版「把假的 opencode
# 放進 PATH」是同一種可替換面。
MCP_READY_MARKER = "server ready, listening on stdio"


def spawn_mcp(
    tmp_root: Path,
    env_overrides: dict[str, str] | None = None,
    *,
    server_args: list[str] | None = None,
) -> subprocess.Popen:
    """以 tmp_root 當沙箱 root 啟動 mcp_server.py。

    root 與開關走 **argv**(`--root` / `--readonly` / `--enable-build-commands`),
    不走環境變數 —— 那正是被測的接線。

    - 設定來自 conftest 建的 tmp HOME(`deployment.json` 指向必定沒人聽的 port,
      主模型是 `example-code-model`)。子行程繼承那個 HOME,所以它與這個行程看到
      同一份設定 —— 不需要、也不能再用環境變數餵它。
    - 即使 env_overrides 蓋掉 HOME,也要讓子行程找得到 mcp 套件 → 顯式帶 PYTHONPATH。
    """
    env = os.environ.copy()
    # 真的起一個 MCP server 就會真的寫一份 lease(mcp_lease.open_lease())。
    # 不把 state 目錄導到 tmp 的話,每一條 live-server 測試都會在使用者真正的
    # `~/.local/state/codetrail/mcp/` 留下檔案;被 kill 的那幾個還會留下
    # `exited: null` 的孤兒 lease,讓 doctor 之後報出根本不存在的 instance。
    env["XDG_STATE_HOME"] = str(tmp_root / ".state")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = os.pathsep.join(
        [p for p in sys.path if p] + [env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "mcp_server.py"), "--root", str(tmp_root),
         "--skip-aux-preflight", *(server_args or [])],
        stdin=subprocess.PIPE,          # FastMCP 走 stdio,給它一個關著的 stdin
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        env=env,
    )


def seed_home(home: Path) -> Path:
    """在一個 tmp HOME 裡放一份可用的 `deployment.json`。

    設定只來自檔案,所以任何把 HOME 指到空目錄的測試都會讓真的起 server 的
    子行程在 `require_main_model()` 掛掉(exit 3)——症狀是「沒有寫出 lease」
    之類跟被測邏輯無關的斷言失敗。port 指向必定沒人聽的號碼,離線契約不變。
    """
    cfg_dir = home / ".config" / "codetrail"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "deployment.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "defaults",
                "services": {
                    "main": {
                        "model": "example-code-model",
                        "ctx": 65536,
                        "port": 65535,
                        "base_url": "http://127.0.0.1:65535",
                    },
                    "embedding": {"port": 65534, "base_url": "http://127.0.0.1:65534"},
                    "reranker": {"port": 65533, "base_url": "http://127.0.0.1:65533"},
                    "vl": {"port": 65532, "base_url": "http://127.0.0.1:65532"},
                },
            }
        ),
        encoding="utf-8",
    )
    return home


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
# test_fs_sandbox / test_code_rag_index / test_code_rag_search /
# test_repeat_guard 原本各有一份幾乎一樣的 mcp_module fixture(約 25 行 × 4)。
# 差別只在「先在 root 底下放什麼檔案」,那部分留在各自的 fixture 裡。
# ---------------------------------------------------------------------------


def import_mcp_module(monkeypatch, root: Path):
    """以 root 當沙箱 root 重新 import mcp_server,回傳該模組。

    要注意的四件事:
    1. root 走 **argv**(`--root`)—— 被當成腳本執行時 mcp_server 只認它。這裡是
       `import`,`_parse_server_argv` 拿到的是空清單,所以直接把 cwd 換過去:
       server 的 root 判準是「argv 的 --root,否則 cwd」。
    2. 設定來自 conftest 的 tmp HOME(`deployment.json` 的端點指向關著的 port),
       確保 KB / CodeRAG 初始化不會卡在等 llama-server。
    3. mcp_server 的 module-level code 會 mutate config.PATCH_ENABLED /
       RUN_COMMAND_ENABLED / ALLOWED_COMMANDS。先用 monkeypatch 釘住原值,
       teardown 自動 restore —— 否則會污染其他測試對 config 預設值的斷言。
    4. 先把 mcp_server 從 sys.modules 拔掉才 import,確保拿到 fresh module。
       mcp.run() 只在 __main__ guard 裡呼叫,所以直接 import 是安全的。
    """
    pytest.importorskip("mcp", reason="mcp 套件未安裝;MCP 路線才需要")

    monkeypatch.chdir(root)
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
