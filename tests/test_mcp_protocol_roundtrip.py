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
    # 使用 requests 會立即拒絕的 malformed host，確保不會碰到真 server；也不必
    # 為三條 error path 各等一次 production retry/backoff。
    env["AICODE_LLAMA_BASE_URL"] = "http://%zz:8081"
    env["AICODE_LLAMA_EMBED_BASE_URL"] = "http://%zz:8081"
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
            code_search_schema = next(
                t.inputSchema for t in tools.tools if t.name == "code_rag_search"
            )
            listed = await session.call_tool("list_dir", {"path": "."})
            grepped = await session.call_tool("grep_code", {"pattern": "def "})
            return names, code_search_schema, listed, grepped


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
    names, code_search_schema, listed, grepped = asyncio.run(
        asyncio.wait_for(_roundtrip(project), timeout=60)
    )

    assert len(names) == 18
    for expected in ("query_knowledge", "list_dir", "read_file", "grep_code"):
        assert expected in names, f"工具 {expected} 沒註冊成功；實得 {sorted(names)}"
    max_chars_schema = code_search_schema["properties"]["max_chars"]
    assert max_chars_schema["type"] == "integer"
    assert max_chars_schema["default"] == 12000
    assert max_chars_schema["minimum"] == 2000
    assert max_chars_schema["maximum"] == 30000

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
        assert "http://%zz:8081" in error_text
        assert "8081 llama-server" in error_text
        assert "AICODE_LLAMA_EMBED_BASE_URL" in error_text


# ---------------------------------------------------------------------------
# 2) stdout 純淨度：每一非空 stdout 行都必須是合法 JSON-RPC
# ---------------------------------------------------------------------------
async def _raw_protocol_roundtrip(project: Path, msgs: list[dict]) -> tuple[bytes, bytes]:
    """逐階段送 raw JSON-RPC，確認回應後才關 stdin。

    一次 ``communicate(input=...)`` 會立刻送 EOF；FastMCP 忙碌或新版 anyio
    排程下，shutdown 可能在已排入的 tools/call 寫回前取消它，形成與產品協定
    無關的 load-dependent flake。這裡保持 stdin 開啟到 id=2 已收到。
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(REPO_ROOT / "mcp_server.py"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(REPO_ROOT),
        env=_server_env(project),
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    stderr_task = asyncio.create_task(proc.stderr.read())
    captured: list[bytes] = []

    async def send(message: dict) -> None:
        proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def read_through(expected_id: int) -> None:
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise AssertionError(
                    f"mcp_server 在回覆 id={expected_id} 前結束；"
                    f"stdout={b''.join(captured)[:800]!r}"
                )
            captured.append(line)
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(obj, dict) and obj.get("id") == expected_id:
                return

    try:
        await send(msgs[0])
        await read_through(1)
        for message in msgs[1:]:
            await send(message)
        await read_through(2)

        proc.stdin.close()
        await proc.stdin.wait_closed()
        await proc.wait()
        captured.append(await proc.stdout.read())
        return b"".join(captured), await stderr_task
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        if not stderr_task.done():
            stderr_task.cancel()
        raise


def test_mcp_stdout_is_pure_jsonrpc(tmp_path: Path):
    project = _make_project(tmp_path)

    # 手動組 JSON-RPC 訊息（newline-delimited），依 initialize → initialized
    # → tools/call 順序送入；收到 call 回應後才關 stdin，再檢查原始 stdout。
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
    try:
        stdout, stderr = asyncio.run(
            asyncio.wait_for(_raw_protocol_roundtrip(project, msgs), timeout=60)
        )
    except TimeoutError:
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
