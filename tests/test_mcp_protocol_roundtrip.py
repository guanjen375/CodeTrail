"""P0-4：真正的 MCP JSON-RPC round-trip + stdout 純淨度測試。

既有 test_mcp_smoke.py 只驗「啟動到 listening」，抓不到 stdout 污染。

注意：mcp 的 stdio client 對「整行非 JSON」其實是容錯的（parse 失敗會 skip 該行
繼續讀），所以單靠 ClientSession round-trip 成功，並不足以證明 stdout 乾淨。
真正致命的是「log 與 JSON-RPC 黏在同一行」或高頻交錯。因此這裡用兩個測試：

  1. test_mcp_protocol_roundtrip：ClientSession 走 initialize → list_tools →
     call_tool，證明 @_tool 包裝沒弄壞工具註冊/派發，協定功能正常。
  2. test_mcp_stdout_is_pure_jsonrpc：直接抓 server 原始 stdout，斷言「每一非空行
     都是合法 JSON-RPC」——不依賴 client 容錯，任何 print() 落到 stdout 都會被抓到。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

pytest.importorskip("mcp", reason="mcp 套件未安裝；OpenCode + MCP 路線才需要")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


def _server_env(project: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["AICODE_ROOT"] = str(project)
    env["PYTHONIOENCODING"] = "utf-8"
    # 指向一個關著的 port，確保子行程不會真的打 llama-server。
    env["AICODE_LLAMA_BASE_URL"] = "http://127.0.0.1:65535"
    env["AICODE_LLAMA_EMBED_BASE_URL"] = "http://127.0.0.1:65535"
    env["AICODE_MODEL"] = "example-code-model"
    env["AICODE_REQUIRED_MODELS_CHECK_SKIP"] = "1"
    # 讓子行程找得到 mcp / numpy（可能裝在 user site）。
    env["PYTHONPATH"] = os.pathsep.join(
        [p for p in sys.path if p] + [env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return env


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "README.md").write_text("# hi\n", encoding="utf-8")
    (project / "mod.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    return project


# ---------------------------------------------------------------------------
# 1) ClientSession 功能 round-trip
# ---------------------------------------------------------------------------
async def _roundtrip(project: Path):
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(REPO_ROOT / "mcp_server.py")],
        env=_server_env(project),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            listed = await session.call_tool("list_dir", {"path": "."})
            grepped = await session.call_tool("grep_code", {"pattern": "def "})
            return names, listed, grepped


async def _embedding_failure_roundtrip(project: Path):
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(REPO_ROOT / "mcp_server.py")],
        env=_server_env(project),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            code_result = await session.call_tool(
                "code_rag_search", {"query": "hello function"}
            )
            knowledge_result = await session.call_tool(
                "query_knowledge", {"question": "hello behavior"}
            )
            ingest_result = await session.call_tool(
                "ingest_document", {"path": "ingest.md"}
            )
            return code_result, knowledge_result, ingest_result


def _content_text(result) -> str:
    return "".join(getattr(c, "text", "") or "" for c in result.content)


def test_mcp_protocol_roundtrip(tmp_path: Path):
    project = _make_project(tmp_path)
    names, listed, grepped = asyncio.run(
        asyncio.wait_for(_roundtrip(project), timeout=60)
    )

    for expected in ("query_knowledge", "list_dir", "read_file", "grep_code"):
        assert expected in names, f"工具 {expected} 沒註冊成功；實得 {sorted(names)}"

    assert listed.isError is False, _content_text(listed)
    listed_text = _content_text(listed)
    assert "README.md" in listed_text or "mod.py" in listed_text, listed_text

    assert grepped.isError is False, _content_text(grepped)
    assert "hello" in _content_text(grepped)


def test_embedding_failure_is_a_tool_error_with_actionable_url(tmp_path: Path):
    project = _make_project(tmp_path)
    (project / "ingest.md").write_text("# Hello\n\nDocument chunk.\n", encoding="utf-8")
    (project / "knowledge.json").write_text(
        json.dumps(
            {
                "metadata": {"documents": ["manual.md"]},
                "chunks": [
                    {
                        "id": "manual-1",
                        "source": "manual.md",
                        "content": "hello behavior",
                        "embedding": [1.0, 0.0],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    code_result, knowledge_result, ingest_result = asyncio.run(
        asyncio.wait_for(_embedding_failure_roundtrip(project), timeout=60)
    )

    for result in (code_result, knowledge_result, ingest_result):
        error_text = _content_text(result)
        assert result.isError is True, error_text
        assert "http://127.0.0.1:65535" in error_text
        assert "8081 llama-server" in error_text
        assert "AICODE_LLAMA_EMBED_BASE_URL" in error_text


# ---------------------------------------------------------------------------
# 2) stdout 純淨度：每一非空 stdout 行都必須是合法 JSON-RPC
# ---------------------------------------------------------------------------
def test_mcp_stdout_is_pure_jsonrpc(tmp_path: Path):
    project = _make_project(tmp_path)

    # 手動組 JSON-RPC 訊息（newline-delimited），一次餵進 stdin 後關閉 → server
    # 處理完所有訊息、讀到 EOF 後自行結束。我們再檢查原始 stdout。
    msgs = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "teeth-test", "version": "1.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_dir", "arguments": {"path": "."}},
        },
    ]
    stdin_data = "".join(json.dumps(m) + "\n" for m in msgs).encode("utf-8")

    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "mcp_server.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        env=_server_env(project),
    )
    try:
        stdout, stderr = proc.communicate(input=stdin_data, timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        pytest.fail("mcp_server 沒在時限內完成 JSON-RPC 往返")

    stderr_text = stderr.decode("utf-8", errors="replace")
    out_text = stdout.decode("utf-8", errors="replace")

    # 關鍵斷言：stdout 的每一非空行都必須是合法 JSON-RPC，
    # 不能夾雜任何 log / print 輸出（那會直接讓 client parse 失敗）。
    bad_lines = []
    parsed = []
    for line in out_text.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            bad_lines.append(line)
            continue
        if not (isinstance(obj, dict) and obj.get("jsonrpc") == "2.0"):
            bad_lines.append(line)
        else:
            parsed.append(obj)

    assert not bad_lines, (
        "stdout 出現非 JSON-RPC 內容（log 污染了協定通道）:\n"
        + "\n".join(bad_lines[:10])
        + f"\n\n(stderr 摘錄:\n{stderr_text[-800:]})"
    )
    # 至少要拿到 initialize(id=1) 與 tools/call(id=2) 的回應
    ids = {o.get("id") for o in parsed}
    assert 1 in ids, f"沒收到 initialize 回應；stdout=\n{out_text[:800]}"
    assert 2 in ids, f"沒收到 tools/call 回應；stdout=\n{out_text[:800]}"
