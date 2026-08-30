"""run_command 的 timeout 三層契約(workflow D):native schema、executor runtime 驗證、MCP schema。

三層都必須自己擋:llama.cpp 的 JSON-schema→GBNF 只支援子集,client 可能根本不套
schema 約束,所以 executor 端的 1..600 驗證是最後一道;MCP 端的 pydantic strict
則是 client 誤送 true / "60" / 1.0 時的獨立防線。文件寫「server 接受 1..600 秒;
client 可能更早截止」,不宣稱 600 秒必在 OpenCode client timeout 內。
"""
from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest

import config
import container_runner
from agent_tools import ToolExecutor

REPO_ROOT = Path(__file__).resolve().parent.parent

# smoke:安全層(AGENTS.md §1.1 第 2 款「無聲失敗風險的契約」)。
pytestmark = pytest.mark.smoke

TIMEOUT_ERROR_TITLE = "錯誤: timeout 必須是 1..600 的整數"


@pytest.fixture
def runner(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", True)
    # host path:容器旗標由開發機環境在 import 時決定,測試不能吃到它。
    monkeypatch.setattr(container_runner, "CONTAINER_ENABLED", False)
    return ToolExecutor(str(tmp_path))


def _fake_completed(stdout: str = "ok"):
    class R:
        returncode = 0

    r = R()
    r.stdout = stdout
    r.stderr = ""
    return r


def test_config_timeout_bounds_are_1_and_600():
    assert config.RUN_COMMAND_TIMEOUT_MIN == 1
    assert config.RUN_COMMAND_TIMEOUT_MAX == 600
    assert config.RUN_COMMAND_TIMEOUT == 60


@pytest.mark.parametrize("bad", [0, 601, -1, True, "60", 60.0, None])
def test_executor_rejects_timeout_out_of_bounds(runner: ToolExecutor, monkeypatch, bad):
    spawned: list[Any] = []

    def boom(*args, **kwargs):
        spawned.append((args, kwargs))
        raise AssertionError("subprocess.run 不該被呼叫")

    monkeypatch.setattr("agent_tools.subprocess.run", boom)
    out = runner.run_command("pytest -q", timeout=bad)
    assert out.startswith(TIMEOUT_ERROR_TITLE), out
    assert type(bad).__name__ in out, out
    assert spawned == [], "非法 timeout 必須在 spawn 之前拒絕"


def test_timeout_error_message_is_bounded(runner: ToolExecutor, monkeypatch):
    monkeypatch.setattr(
        "agent_tools.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不該 spawn")),
    )
    huge = "x" * 500
    out = runner.run_command("pytest -q", timeout=huge)
    assert out.startswith(TIMEOUT_ERROR_TITLE), out
    assert "str:" in out, out
    assert huge not in out, "500 字元的原值不得整段回送"
    assert "…" in out, out
    assert len(out) < 200, len(out)


@pytest.mark.parametrize("ok", [1, 600])
def test_executor_accepts_bounds_and_forwards_timeout(runner: ToolExecutor, monkeypatch, ok):
    # 這條在舊碼也會原樣轉發;紅燈靠「邊界常數是契約的一部分」這個新斷言。
    assert (config.RUN_COMMAND_TIMEOUT_MIN, config.RUN_COMMAND_TIMEOUT_MAX) == (1, 600)
    seen: list[dict] = []

    def fake_run(cmd_parts, **kwargs):
        seen.append(dict(kwargs))
        return _fake_completed()

    monkeypatch.setattr("agent_tools.subprocess.run", fake_run)
    out = runner.run_command("pytest -q", timeout=ok)
    assert out.startswith("=== ✓ 成功"), out
    assert [k["timeout"] for k in seen] == [ok]


@pytest.mark.parametrize("value,accepted", [(1, True), (600, True), (0, False), (601, False)])
def test_container_path_forwards_timeout_and_rejects_before_container(
    tmp_path: Path, monkeypatch, value, accepted
):
    # 舊碼的容器路徑也會原樣轉發;紅燈靠「邊界常數是契約的一部分」這個新斷言。
    assert (config.RUN_COMMAND_TIMEOUT_MIN, config.RUN_COMMAND_TIMEOUT_MAX) == (1, 600)
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", True)
    monkeypatch.setattr(container_runner, "CONTAINER_ENABLED", True)
    seen: list[int] = []

    def fake_container(**kwargs):
        seen.append(kwargs["timeout"])
        return {"error": None, "stdout": "ok", "stderr": "", "success": True, "returncode": 0}

    monkeypatch.setattr(container_runner, "run_in_container", fake_container)
    runner = ToolExecutor(str(tmp_path))
    out = runner.run_command("pytest -q", timeout=value)
    if accepted:
        assert "容器模式" in out and out.startswith("=== ✓ 成功"), out
        assert seen == [value]
    else:
        assert out.startswith(TIMEOUT_ERROR_TITLE), out
        assert seen == [], "非法 timeout 必須在進入容器前拒絕"


def test_native_tool_schema_declares_timeout_bounds():
    from agent_tools import _RUN_COMMAND_TOOL

    schema = _RUN_COMMAND_TOOL["function"]["parameters"]["properties"]["timeout"]
    assert schema["type"] == "integer"
    assert schema["minimum"] == 1
    assert schema["maximum"] == 600
    assert schema["default"] == 60


def _norm(text: str) -> str:
    """把換行/縮排正規化成單一空白,讓斷言能用完整肯定句而不是裸關鍵字。"""
    return " ".join(str(text).split())


def test_native_tool_description_states_whitelist_tiers_and_timeout():
    from agent_tools import _RUN_COMMAND_TOOL

    description = _norm(_RUN_COMMAND_TOOL["function"]["description"])
    assert "只在 AI_CODE_ENABLE_BUILD_COMMANDS=1" in description
    assert "git 不在白名單" in description
    assert "1..600" in description
    assert "client 可能更早截止" in description
    timeout_desc = _norm(_RUN_COMMAND_TOOL["function"]["parameters"]["properties"]["timeout"]["description"])
    assert "1..600" in timeout_desc
    assert "client 可能更早截止" in timeout_desc


# ---------------------------------------------------------------------------
# MCP 層:pydantic strict + schema。import mcp_server 前把 HOME 系列指到 tmp_path,
# 不吃審核者本機的 ~/.config(SEAMS S-E)。
# ---------------------------------------------------------------------------
@pytest.fixture
def mcp_module(monkeypatch, tmp_path: Path):
    from tests._harness import import_mcp_module

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    root = tmp_path / "proj"
    root.mkdir()
    (root / "README.md").write_text("# proj\n", encoding="utf-8")
    module = import_mcp_module(monkeypatch, root)
    yield module
    import sys

    sys.modules.pop("mcp_server", None)


def _timeout_schema(mcp_module) -> dict:
    tools = asyncio.run(mcp_module.mcp.list_tools())
    run_command = next(t for t in tools if t.name == "run_command")
    return run_command.inputSchema["properties"]["timeout"]


def test_mcp_tools_list_timeout_schema(mcp_module):
    schema = _timeout_schema(mcp_module)
    assert schema["type"] == "integer"
    assert schema["default"] == 60
    assert schema["minimum"] == 1
    assert schema["maximum"] == 600


@pytest.mark.parametrize("bad", [True, 1.0, "60", 0, 601])
def test_mcp_call_tool_rejects_non_strict_timeouts(mcp_module, monkeypatch, bad):
    from mcp.server.fastmcp.exceptions import ToolError

    assert _timeout_schema(mcp_module)["minimum"] == 1  # 舊 schema 沒有 timeout 參數
    called: list[int] = []
    monkeypatch.setattr(mcp_module.EXEC, "run_command",
                        lambda cmd, timeout: called.append(timeout) or "stub")
    with pytest.raises(ToolError):
        asyncio.run(mcp_module.mcp.call_tool("run_command", {"cmd": "pytest -q", "timeout": bad}))
    assert called == [], "FastMCP 必須在呼叫 executor 前就拒絕"


@pytest.mark.parametrize("value", [1, 600, None])
def test_mcp_run_command_forwards_timeout(mcp_module, monkeypatch, value):
    called: list[int] = []
    monkeypatch.setattr(mcp_module.EXEC, "run_command",
                        lambda cmd, timeout: called.append(timeout) or "stub")
    from tests._harness import tool_fn

    signature = inspect.signature(tool_fn(mcp_module, "run_command"))
    assert signature.parameters["timeout"].default == 60
    args = {"cmd": "pytest -q"} if value is None else {"cmd": "pytest -q", "timeout": value}
    asyncio.run(mcp_module.mcp.call_tool("run_command", args))
    assert called == [60 if value is None else value]


def test_mcp_run_command_docstring_declares_tiers_and_timeout():
    """模型看到的 schema description 就是這個 docstring;靜態 AST 讀,不 import server。"""
    tree = ast.parse((REPO_ROOT / "mcp_server.py").read_text(encoding="utf-8"))
    fn = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_command"
    )
    doc = _norm(ast.get_docstring(fn) or "")
    assert "1..600" in doc
    assert "只在 AI_CODE_ENABLE_BUILD_COMMANDS=1" in doc
    assert "git 不在白名單" in doc
    assert "client 可能更早截止" in doc
    assert "timeout" in {a.arg for a in fn.args.args}
