"""mcp_server 的啟動閘、runtime policy、工具目錄契約、JSON-RPC 通道、結果預算與外部匯入。

合併自 tests/test_mcp_startup.py、tests/test_mcp_runtime_policy.py、
tests/test_mcp_tool_contract.py、tests/test_mcp_protocol_roundtrip.py、
tests/test_tool_result_budget.py、tests/test_external_import.py(2026-09-02)。
各區段前的分隔註解標了原檔名;每段保留原本「為什麼有這條測試」的說明。

smoke 成員資格與合併前逐條相同:啟動閘與 runtime policy 兩段原本整檔 smoke
(AGENTS.md §2 安全檢查點),合併後逐條標 `@pytest.mark.smoke`;其餘各段照抄
原本的逐條標記(工具目錄契約 1 條、stdout 純淨度 1 條、結果預算 1 條),
外部匯入那段原本就沒有 smoke。

── 啟動閘(原 test_mcp_startup.py;它本身合併自 tests/test_mcp_root_safety.py 與
   tests/test_mcp_smoke.py,2026-08-20)──
root 驗證的實作在 root_safety.py:mcp_server.py 匯入它,scripts/index_stats.py 也
匯入同一份 —— 維護 CLI 不能 import mcp_server(會拉起 FastMCP / KnowledgeBase /
CodeRAG),又不准另寫一套 root 驗證。所以這裡分三層守:
1. 純函式層:validate_aicode_root 的每個拒絕理由(in-process,零成本)。
2. 接線層:mcp_server.py 原始碼真的有匯入且呼叫它(靜態檢查)。
3. 端對端層:真的 spawn 一次 server,確認拒絕路徑會 exit≠0、正常路徑會 listening。

第 3 層原本有四條 subprocess case(root='/'、$HOME、$HOME+override、正常),
其中 $HOME 與 $HOME+override 兩條與第 1 層完全重疊,各花約 0.45s。2026-08-20 移除
那兩條,保留 root='/' 當拒絕路徑的端對端錨點。

── runtime policy(原 test_mcp_runtime_policy.py)──
patch / run_command / build commands 三個開關由**啟動參數**決定。Build commands
(make/cmake/ninja/meson/bazel)會跑專案內的 build script,「分析陌生 repo」時
模型可一鍵跑 make = 任意程式碼執行,所以預設不掛。

去環境變數之後的判準:`--readonly` 一律三個都關(評測邊界,不接受被別的設定
調和)、`--enable-build-commands` 才掛 build 白名單。環境變數**不參與決定** ——
殼層裡殘留的同名變數(來自另一份安裝、另一個專案)不得把 readonly 翻回來。

決策本身在 runtime_policy.py(純函式),所以組合窮舉不必一條 spawn 一次 server;
argv→banner 的接線由兩條端對端錨點守住:一條全預設、一條 `--readonly`。

── 工具目錄契約(原 test_mcp_tool_contract.py)──
Silent-failure gate for the model-visible MCP catalog.

── JSON-RPC round-trip 與 stdout 純淨度(原 test_mcp_protocol_roundtrip.py,P0-4)──
既有 test_mcp_smoke.py 只驗「啟動到 listening」,抓不到 stdout 污染。

注意:mcp 的 stdio client 對「整行非 JSON」其實是容錯的(parse 失敗會 skip 該行
繼續讀),所以單靠 ClientSession round-trip 成功,並不足以證明 stdout 乾淨。
真正致命的是「log 與 JSON-RPC 黏在同一行」或高頻交錯。因此這裡用兩個測試:

  1. test_mcp_protocol_roundtrip:ClientSession 走 initialize → list_tools →
     call_tool,證明 @_tool 包裝沒弄壞工具註冊/派發,協定功能正常。
  2. test_mcp_stdout_is_pure_jsonrpc:直接抓 server 原始 stdout,斷言「每一非空行
     都是合法 JSON-RPC」——不依賴 client 容錯,任何 print() 落到 stdout 都會被抓到。

── 結果預算(原 test_tool_result_budget.py)──
Result budgets must follow the active context, not a frozen char default.

── 外部檔案匯入(原 test_external_import.py)──
外部檔案匯入的安全邊界測試。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import config
from external_import import import_external_file
from mcp_contract import MCP_INSTRUCTIONS, PUBLIC_TOOL_ORDER
from root_safety import validate_aicode_root as _validate
from runtime_policy import EXTRA_BUILD_COMMANDS, READONLY_POLICY, resolve_runtime_policy
from tests._harness import REPO_ROOT, import_mcp_module, spawn_mcp, terminate_proc, wait_for_marker
from tool_result_adapter import ResultBudget, adapt_tool_result, estimate_result_tokens


def _mcp_importable() -> bool:
    try:
        import mcp  # noqa: F401
    except ImportError:
        return False
    return True


# CI 沒裝 mcp 時 skip;日常 OpenCode 路線需要 mcp。
# 啟動閘 / runtime policy / round-trip 三個來源原本都是 module 層
# `pytest.importorskip("mcp")`(整檔 skip)。合併後同一個檔裡多了不需要 mcp 的外部
# 匯入測試,所以改成逐條 skipif:skip 到的仍然是原本那幾條,不多不少。
# (工具目錄契約與結果預算走 `import_mcp_module`,它自己會 importorskip。)
needs_mcp = pytest.mark.skipif(
    not _mcp_importable(), reason="mcp 套件未安裝;OpenCode + MCP 路線才需要"
)


# ═══════════════════════════════════════════════════════════════════════════
# ── 原 test_mcp_startup.py:mcp_server 的啟動閘(AICODE_ROOT 驗證 + 真的能初始化到 listening)──
# smoke:安全層(AGENTS.md §1.1 第 2 款「無聲失敗風險的契約」)
# AGENTS.md §2 安全檢查點:mcp_server 啟動時的 AICODE_ROOT 驗證與 sandbox root 設定。
# 原本整檔 `pytestmark = pytest.mark.smoke`,合併後這段每一條各自標 smoke。
# ═══════════════════════════════════════════════════════════════════════════


# ---- 1. validate_aicode_root 純函式 ---------------------------------------


@needs_mcp
@pytest.mark.smoke
def test_rejects_empty_root():
    resolved, err = _validate(None, "/home/x", allow_home_override=False)
    assert resolved is None
    # root 走 argv(`mcp_server --root`),所以訊息指的是那個旗標,不是環境變數。
    assert err and "--root" in err


@needs_mcp
@pytest.mark.smoke
def test_rejects_root_slash():
    resolved, err = _validate("/", "/home/x", allow_home_override=False)
    assert resolved is None
    assert err and "/" in err


@needs_mcp
@pytest.mark.smoke
def test_rejects_home(tmp_path: Path):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    resolved, err = _validate(str(fake_home), str(fake_home), allow_home_override=False)
    assert resolved is None
    assert err and "$HOME" in err


@needs_mcp
@pytest.mark.smoke
def test_allows_home_when_overridden(tmp_path: Path):
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    resolved, err = _validate(str(fake_home), str(fake_home), allow_home_override=True)
    assert err is None
    assert resolved == str(fake_home.resolve())


@needs_mcp
@pytest.mark.smoke
def test_allows_normal_subdir(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    resolved, err = _validate(str(project), str(tmp_path), allow_home_override=False)
    assert err is None
    assert resolved == str(project.resolve())


@needs_mcp
@pytest.mark.smoke
def test_rejects_nonexistent_dir(tmp_path: Path):
    nope = tmp_path / "nope"
    resolved, err = _validate(str(nope), "/home/x", allow_home_override=False)
    assert resolved is None
    assert err and "不是目錄" in err


# ---- 2. 接線:別讓檢查被靜悄悄拿掉 ----------------------------------------


@needs_mcp
@pytest.mark.smoke
def test_mcp_server_still_wires_up_root_validation():
    """mcp_server.py 必須匯入並實際呼叫 root 檢查 —— 別讓它被靜悄悄拿掉。"""
    src = (REPO_ROOT / "mcp_server.py").read_text(encoding="utf-8")
    assert "from root_safety import validate_aicode_root" in src, (
        "mcp_server.py 沒有匯入 root_safety.validate_aicode_root — root safety 檢查被砍了?"
    )
    assert "_validate_aicode_root(" in src, "mcp_server.py 沒有呼叫 root 檢查"


@needs_mcp
@pytest.mark.smoke
def test_fastmcp_v1_import_contract():
    """requirements 不得解出已移除現行 import path 的 MCP SDK 2.x。"""
    from mcp.server.fastmcp import FastMCP

    assert FastMCP is not None


# ---- 3. 端對端 -------------------------------------------------------------


@needs_mcp
@pytest.mark.smoke
def test_mcp_server_initializes_and_is_listenable(tmp_path: Path):
    """正常 root 下,mcp_server.py 能走完所有初始化並進入 listening 狀態。

    不需要 llama-server、不下載模型、不跑 inference:只看 mcp.run() 前最後一條
    stderr 里程碑,看到就代表 import → root 檢查 → KnowledgeBase / CodeRAG /
    ToolExecutor → FastMCP 全部構造成功。
    """
    project = tmp_path / "fakeproj"
    project.mkdir()
    (project / "README.md").write_text("# fake\n", encoding="utf-8")

    proc = spawn_mcp(project)
    try:
        stderr = wait_for_marker(proc)
        assert "server ready, listening on stdio" in stderr, (
            f"mcp_server.py 沒走到 listening 階段。stderr 摘錄:\n{stderr[-2000:]}"
        )
        assert "Traceback" not in stderr, stderr[-2000:]
        assert "FATAL" not in stderr, stderr[-2000:]
        assert "ModuleNotFoundError" not in stderr, stderr[-2000:]
    finally:
        terminate_proc(proc)


@needs_mcp
@pytest.mark.smoke
def test_mcp_server_rejects_root_slash():
    """root='/' 必須在啟動階段被拒絕(配 stderr [FATAL])。

    拒絕路徑的端對端錨點:證明 root_safety 的判斷真的會讓 server exit≠0,
    而不只是回傳一個沒人理的錯誤字串。
    """
    proc = spawn_mcp(Path("/"))
    try:
        try:
            _, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate()
        out = (stderr or b"").decode("utf-8", errors="replace")
        assert proc.returncode != 0, f"應該 exit≠0,實際 {proc.returncode}\n{out}"
        assert "FATAL" in out and ("/" in out or "AICODE_ROOT" in out)
    finally:
        terminate_proc(proc)


# ═══════════════════════════════════════════════════════════════════════════
# ── 原 test_mcp_runtime_policy.py:patch / run_command / build 命令的預設值與 env 尊重 ──
# smoke:安全層(AGENTS.md §1.1 第 2 款「無聲失敗風險的契約」)
# AGENTS.md §2/§4:patch / run_command / build 命令的預設值與 env 尊重。
# 原本整檔 `pytestmark = pytest.mark.smoke`,合併後這段每一條各自標 smoke。
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "fakeproj"
    p.mkdir()
    (p / "README.md").write_text("# fake\n", encoding="utf-8")
    return p


def _drop_env(*names: str) -> dict[str, str]:
    """產生 env_overrides 把指定 env 清掉(避免從父行程繼承)。"""
    return {name: "" for name in names}


# ---- 決策本身 --------------------------------------------------------------


@needs_mcp
@pytest.mark.smoke
def test_defaults_keep_patch_and_run_command_on():
    """互動主場景:patch + run_command 預設 ON、build 命令預設不掛。"""
    policy = resolve_runtime_policy()
    assert policy.patch_enabled is True
    assert policy.run_command_enabled is True
    assert policy.build_commands_enabled is False
    assert policy.extra_build_commands == ()


@needs_mcp
@pytest.mark.smoke
def test_readonly_closes_every_switch_and_ignores_the_other_inputs():
    """`--readonly` 是評測邊界,不是可以被別的設定調和的偏好:三個開關一律關到底,
    而且連 `build_commands=True` 也翻不動它。"""
    policy = resolve_runtime_policy(readonly=True, build_commands=True)
    assert policy == READONLY_POLICY
    assert policy.patch_enabled is False
    assert policy.run_command_enabled is False
    assert policy.build_commands_enabled is False
    assert policy.extra_build_commands == ()


@needs_mcp
@pytest.mark.smoke
def test_build_commands_opt_in():
    """build 命令要顯式打開才會掛 make/cmake/ninja/meson/bazel。"""
    policy = resolve_runtime_policy(build_commands=True)
    assert policy.build_commands_enabled is True
    assert policy.extra_build_commands == EXTRA_BUILD_COMMANDS
    assert "make" in policy.extra_build_commands
    assert "cmake" in policy.extra_build_commands


@needs_mcp
@pytest.mark.smoke
def test_no_environment_variable_can_turn_the_switches_back_on():
    """舊世代用四個環境變數控制這三個開關。它們現在**不參與決定** —— 殼層裡
    殘留的同名變數(來自另一份安裝、另一個專案)不得把 readonly 翻回來。"""
    import os as _os

    import runtime_policy as _rp

    saved = {name: _os.environ.get(name) for name in
             ("AI_CODE_PATCH", "AI_CODE_RUN_TESTS", "AI_CODE_ENABLE_BUILD_COMMANDS")}
    try:
        for name in saved:
            _os.environ[name] = "1"
        assert _rp.resolve_runtime_policy(readonly=True) == READONLY_POLICY
        assert _rp.resolve_runtime_policy().build_commands_enabled is False
    finally:
        for name, value in saved.items():
            if value is None:
                _os.environ.pop(name, None)
            else:
                _os.environ[name] = value


# ---- 端對端接線 ------------------------------------------------------------


@needs_mcp
@pytest.mark.smoke
def test_startup_banner_reports_default_policy(project: Path):
    """全預設啟動一次:banner 必須反映 patch/run ON、build 未掛。"""
    proc = spawn_mcp(project)
    try:
        out = wait_for_marker(proc)
        assert "PATCH_ENABLED = True" in out, out[-2000:]
        assert "RUN_COMMAND_ENABLED = True" in out, out[-2000:]
        assert "build 命令未掛白名單" in out, out[-2000:]
        # 反向確認:不應該印「已 append build 命令」
        assert "已 append build 命令" not in out, out[-2000:]
    finally:
        terminate_proc(proc)


@needs_mcp
@pytest.mark.smoke
def test_startup_banner_reports_the_readonly_policy(project: Path):
    """`--readonly` 起一次真的 server:banner 必須三個開關都關。

    這條是 argv → runtime_policy → config/ALLOWED_COMMANDS 整條接線的錨點;
    純函式測試證明決策正確,這條證明決策真的被套用。同時把三個舊環境變數設成
    「開」——它們不得把 readonly 翻回來。
    """
    proc = spawn_mcp(
        project,
        server_args=["--readonly"],
        env_overrides={
            "AI_CODE_PATCH": "1",
            "AI_CODE_RUN_TESTS": "1",
            "AI_CODE_ENABLE_BUILD_COMMANDS": "1",
        },
    )
    try:
        out = wait_for_marker(proc)
        assert "PATCH_ENABLED = False" in out, (
            f"--readonly 沒關掉 patch(殘留的環境變數把它翻回來了?)\n{out[-2000:]}"
        )
        assert "RUN_COMMAND_ENABLED = False" in out, out[-2000:]
        assert "build 命令未掛白名單" in out, out[-2000:]
        assert "已 append build 命令" not in out, out[-2000:]
    finally:
        terminate_proc(proc)


@needs_mcp
@pytest.mark.smoke
def test_startup_banner_reports_the_build_commands_opt_in(project: Path):
    """`--enable-build-commands` 才掛 make/cmake/ninja/meson/bazel。"""
    proc = spawn_mcp(project, server_args=["--enable-build-commands"])
    try:
        out = wait_for_marker(proc)
        assert "已 append build 命令" in out, out[-2000:]
        assert "make" in out and "cmake" in out, out[-2000:]
    finally:
        terminate_proc(proc)


# ═══════════════════════════════════════════════════════════════════════════
# ── 原 test_mcp_tool_contract.py:模型看得到的 MCP catalog 的 silent-failure gate ──
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.smoke
def test_live_catalog_is_bounded_typed_and_ordered(monkeypatch, tmp_path: Path):
    mcp_module = import_mcp_module(monkeypatch, tmp_path)
    tools = asyncio.run(mcp_module.mcp.list_tools())

    assert tuple(tool.name for tool in tools) == PUBLIC_TOOL_ORDER
    assert mcp_module.mcp.instructions == MCP_INSTRUCTIONS
    assert len(mcp_module.mcp.instructions) <= 700

    descriptions = {tool.name: tool.description or "" for tool in tools}
    assert sum(map(len, descriptions.values())) <= 12_000
    for name, description in descriptions.items():
        cap = 2_200 if name == "apply_patch" else 1_600
        assert len(description) <= cap, (name, len(description))

    effective_chars = sum(
        len(tool.description or "")
        + len(json.dumps(tool.inputSchema, ensure_ascii=False, sort_keys=True))
        for tool in tools
    )
    assert effective_chars <= 20_000

    for tool in tools:
        for name, schema in tool.inputSchema.get("properties", {}).items():
            assert schema.get("description"), f"{tool.name}.{name} lacks description"

    by_name = {tool.name: tool for tool in tools}
    assert by_name["code_rag_search"].inputSchema["properties"]["mode"]["enum"] == [
        "semantic", "neighbors", "path", "context"
    ]
    assert by_name["analyze_file"].inputSchema["properties"]["view"]["enum"] == [
        "summary", "headers", "sections", "memmap", "symbols", "imports",
        "relocs", "dynamic", "dwarf", "disasm", "strings",
    ]
    timeout = by_name["run_command"].inputSchema["properties"]["timeout"]
    assert (timeout["minimum"], timeout["maximum"]) == (1, 600)

    evidence = {"code_rag_search", "query_knowledge", "query_knowledge_strict"}
    for tool in tools:
        if tool.name not in evidence:
            assert tool.outputSchema is None, tool.name


# ═══════════════════════════════════════════════════════════════════════════
# ── 原 test_mcp_protocol_roundtrip.py:真正的 MCP JSON-RPC round-trip + stdout 純淨度 ──
# (`_server_env` 被 tests/test_mcp_lease.py 的靜態檢查點名:每個真的起 server 的
#  spawn 點都必須把 XDG_STATE_HOME 導到 tmp。)
# ═══════════════════════════════════════════════════════════════════════════


def _server_args(project: Path) -> list[str]:
    """spawn mcp_server 的 argv。root 走 `--root`,不走環境變數。"""
    return [
        str(REPO_ROOT / "mcp_server.py"),
        "--root",
        str(project),
        # 附屬 server 的硬閘走 **argv**:以前是 `AICODE_REQUIRED_MODELS_CHECK_SKIP=1`,
        # 但子行程的環境在交出去之前已經被剝乾淨,用環境變數傳這個意圖必然失效。
        "--skip-aux-preflight",
    ]


def _server_env(project: Path) -> dict[str, str]:
    env = os.environ.copy()
    # 真的起 server 就會真的寫一份 lease(mcp_lease.open_lease())。導到專案底下的
    # tmp 目錄,測試才不會在使用者真正的 `~/.local/state/codetrail/mcp/` 留檔案
    # ——被 kill 的那幾個還會是 `exited: null` 的孤兒,讓 doctor 報出不存在的 instance。
    env["XDG_STATE_HOME"] = str(project / ".state")
    env["PYTHONIOENCODING"] = "utf-8"
    # endpoint 與主模型都來自 conftest 建的 tmp HOME 的 deployment.json
    # (指向必定沒人聽的 port),子行程繼承那個 HOME —— 不需要、也不能再用
    # 環境變數餵它(`AICODE_*` 在交出去之前會被剝掉)。
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
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=_server_args(project),
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
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=_server_args(project),
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


@needs_mcp
def test_mcp_protocol_roundtrip(tmp_path: Path):
    project = _make_project(tmp_path)
    names, code_search_schema, listed, grepped = asyncio.run(
        asyncio.wait_for(_roundtrip(project), timeout=60)
    )

    assert len(names) == 19
    for expected in ("query_knowledge", "list_dir", "read_file", "grep_code"):
        assert expected in names, f"工具 {expected} 沒註冊成功；實得 {sorted(names)}"
    max_chars_schema = code_search_schema["properties"]["max_chars"]
    assert max_chars_schema["default"] is None
    integer_branch = next(
        branch for branch in max_chars_schema["anyOf"] if branch.get("type") == "integer"
    )
    assert integer_branch["minimum"] == 2000
    assert integer_branch["maximum"] == 30000
    assert {branch.get("type") for branch in max_chars_schema["anyOf"]} == {"integer", "null"}

    assert listed.isError is False, _content_text(listed)
    listed_text = _content_text(listed)
    assert len(listed.content) == 1
    assert listed_text.startswith("status: ok\n"), listed_text
    assert "README.md" in listed_text or "mod.py" in listed_text, listed_text

    assert grepped.isError is False, _content_text(grepped)
    assert len(grepped.content) == 1
    assert _content_text(grepped).startswith("status: ok\n")
    assert "hello" in _content_text(grepped)


@needs_mcp
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
        assert error_text.startswith("status: error\nnext:"), error_text
        # endpoint 來自 conftest 的 tmp deployment.json(必定沒人聽的 port)。
        # 以前這裡用 `AICODE_LLAMA_EMBED_BASE_URL` 塞一個 malformed host;那個
        # 入口已經不存在,設定只來自檔案。
        assert "65534" in error_text
        assert "embedding llama-server" in error_text
        assert "deployment.json" in error_text


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
        *_server_args(project),
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


# smoke:stdout 被污染 → JSON-RPC 直接壞掉,而且是無聲的(client 只會看到亂碼)。
# 整份 roundtrip 太貴(約 2.6s),只把這條契約放進 smoke。
@needs_mcp
@pytest.mark.smoke
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


# ═══════════════════════════════════════════════════════════════════════════
# ── 原 test_tool_result_budget.py:結果預算跟著 call-time n_ctx 走,不是凍結的字元預設 ──
# ═══════════════════════════════════════════════════════════════════════════


def _text(result) -> str:
    return "".join(getattr(block, "text", "") or "" for block in result.content)


def _run_tool(mcp_module, name: str, arguments: dict):
    tool = mcp_module.mcp._tool_manager.get_tool(name)
    return asyncio.run(tool.run(arguments, convert_result=True))


@pytest.mark.smoke
def test_default_budget_tracks_n_ctx(monkeypatch, tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "large.txt").write_text("0123456789abcdef\n" * 5_000, encoding="utf-8")
    mcp_module = import_mcp_module(monkeypatch, root)

    monkeypatch.setattr(mcp_module.config, "N_CTX", 1_000)
    small = _text(_run_tool(mcp_module, "read_file", {"path": "large.txt"}))
    assert small.startswith("status: partial\nnext:")
    assert estimate_result_tokens(small) <= int(1_000 * 0.12) + 40

    monkeypatch.setattr(mcp_module.config, "N_CTX", 4_000)
    large = _text(_run_tool(mcp_module, "read_file", {"path": "large.txt"}))
    assert len(large) > len(small)
    assert estimate_result_tokens(large) <= int(4_000 * 0.12) + 40


def test_grep_partial_next_names_every_narrowing_control():
    result = adapt_tool_result(
        "grep_code",
        "match\n... [truncated: too many matches]",
        budget=ResultBudget(
            token_limit=1_000,
            char_limit=3_000,
            explicit=False,
            context_risk=False,
        ),
    )
    assert result.content[0].text.startswith(
        "status: partial\nnext: Narrow path/include/pattern"
    )


def test_code_rag_fastmcp_path_wraps_core_and_injects_dynamic_default(
    monkeypatch, tmp_path: Path
):
    mcp_module = import_mcp_module(monkeypatch, tmp_path)
    monkeypatch.setattr(mcp_module.CODE_RAG, "query_ranked", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(mcp_module.CODE_RAG, "_scan_code_files", lambda: [])
    monkeypatch.setattr(
        mcp_module.code_context,
        "collect_safe_lexical_hits",
        lambda *_args, **_kwargs: [],
    )

    def graph_unavailable():
        raise RuntimeError("synthetic graph unavailable")

    monkeypatch.setattr(mcp_module, "_graph_for_query", graph_unavailable)

    monkeypatch.setattr(mcp_module.config, "N_CTX", 10_000)
    small = _run_tool(
        mcp_module,
        "code_rag_search",
        {"query": "locate startup", "mode": "context"},
    )
    small_core = small.structuredContent["result"]
    assert isinstance(small_core, list) and len(small_core) == 1
    small_budget = small_core[0]["budget_chars"]
    assert 2_000 <= small_budget < int(10_000 * 0.12) * 3

    monkeypatch.setattr(mcp_module.config, "N_CTX", 20_000)
    large = _run_tool(
        mcp_module,
        "code_rag_search",
        {"query": "locate startup", "mode": "context"},
    )
    assert large.structuredContent["result"][0]["budget_chars"] > small_budget


def test_success_content_words_do_not_forge_error_or_partial_status():
    budget = ResultBudget(2_000, 6_000, explicit=False, context_risk=False)
    info = adapt_tool_result("file_info", "error.log: 檔案, 1 行, 9 字元", budget=budget)
    read = adapt_tool_result(
        "read_file",
        "=== notes.txt (行 1-1 / 共 1 行) ===\n   1 | the word truncated is data",
        budget=budget,
    )
    grep = adapt_tool_result(
        "grep_code",
        "=== rg 'truncated' (1 matches) ===\nnotes.txt:1: truncated is data",
        budget=budget,
    )
    for result in (info, read, grep):
        assert result.isError is False
        assert result.content[0].text.startswith("status: ok\n")


@pytest.mark.smoke
def test_git_tools_outside_a_repo_return_a_skip_notice_not_a_retryable_error(tmp_path: Path):
    """2026-09-02 實機:非 git 專案改檔前,模型照 MCP_INSTRUCTIONS 先呼 git_status,
    拿到 `status: error` + 「Correct the reported input or environment problem, then
    retry once」。那句是給「輸入或環境壞了、修好再試」的;「不是 git 倉庫」沒有東西
    可修,回錯誤只會讓模型多繞一圈(這次它沒理,但下次不一定)。非 git 專案要拿到
    status ok 的固定跳過通知,而且 instructions 不能再無條件要求先看 git。"""
    from agent_tools import ToolExecutor

    if shutil.which("git") is None:
        pytest.skip("環境沒有 git")
    root = tmp_path / "proj"
    root.mkdir()
    probe = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-dir"], capture_output=True, text=True
    )
    if probe.returncode == 0:
        pytest.skip("tmp_path 落在某個 git 工作樹內,無法模擬非 git 專案")

    budget = ResultBudget(2_000, 6_000, explicit=False, context_risk=False)
    for name in ("git_status", "git_diff"):
        body = getattr(ToolExecutor(str(root)), name)()
        assert "不是 git 倉庫" in body, (name, body)
        assert not body.startswith("錯誤"), (name, body)
        result = adapt_tool_result(name, body, budget=budget)
        assert result.isError is False, (name, result.content[0].text)
        text = result.content[0].text
        assert text.startswith("status: ok\n"), (name, text)
        assert "retry" not in text, (name, text)
    assert "Inspect git_status/git_diff before apply_patch" not in MCP_INSTRUCTIONS
    assert "non-git" in MCP_INSTRUCTIONS


@pytest.mark.smoke
@pytest.mark.parametrize("broken", ["git-dir", "gitfile"])
def test_a_broken_git_environment_is_not_reported_as_a_missing_repo(
    tmp_path: Path, monkeypatch, broken: str
):
    """git 對兩件不同的事說同一句 "not a git repository"。

    真的不在倉庫裡是 **discovery** 失敗(`(or any of the parent directories)` /
    `(or any parent up to mount point ...)`);而 `GIT_DIR` 指到不存在的路徑、
    或 `.git` 檔指向壞掉的 gitdir 時是 `not a git repository: '<path>'` ——
    那個專案其實在版控裡,只是 git 用不了。

    只比 stderr 子字串的話,後者會拿到「不需要 git 檢查,直接 apply_patch;
    不要重試」的**成功**通知:模型於是跳過改檔前的 git 檢查,現場既有的修改
    完全不會被看到,而畫面上沒有任何異常。

    `git diff` 更沒得比對:兩種情況都印一模一樣的
    `warning: Not a git repository. Use --no-index ...`,所以判斷不能只看
    失敗那條命令自己的訊息。
    """
    from agent_tools import ToolExecutor

    if shutil.which("git") is None:
        pytest.skip("環境沒有 git")
    root = tmp_path / "proj"
    root.mkdir()
    if broken == "git-dir":
        assert subprocess.run(
            ["git", "-C", str(root), "init", "-q"], capture_output=True
        ).returncode == 0
        monkeypatch.setenv("GIT_DIR", str(tmp_path / "gone" / "definitely-not-here"))
    else:
        # worktree metadata 壞掉:`.git` 檔指向一個不存在的 gitdir
        (root / ".git").write_text(f"gitdir: {tmp_path / 'gone'}\n", encoding="utf-8")

    executor = ToolExecutor(str(root))
    for name in ("git_status", "git_diff"):
        body = getattr(executor, name)()
        assert "不需要 git 檢查" not in body, (broken, name, body)
        assert body.startswith("錯誤"), (broken, name, body)


def test_budgeted_evidence_keeps_metadata_before_bulk_text():
    budget = ResultBudget(220, 660, explicit=False, context_risk=False)
    code_payload = [{
        "query": "startup path",
        "evidence": [{
            "path": "src/main.c",
            "start_line": 10,
            "end_line": 200,
            "symbol": "main",
            "reason": "semantic",
            "text": "\n".join(f"{line:4d} | int value_{line} = {line};" for line in range(10, 201)),
        }],
        "uncertainties": [{
            "target": "call graph",
            "reason": "relationship evidence unavailable: graph degraded",
        }],
        "seeds": [{"path": "src/main.c", "line": 10, "symbol": "main"}],
        "graph_status": "degraded: synthetic",
        "truncated": True,
        "budget_chars": 2_000,
        "used_chars": 1_999,
    }]
    code_text = _text(adapt_tool_result("code_rag_search", code_payload, budget=budget))
    assert "graph_status: degraded: synthetic" in code_text
    assert "truncated: true" in code_text
    assert "relationship evidence unavailable" in code_text

    query_payload = {
        "text": "\n".join(f"evidence line {line}" for line in range(500)),
        "display": "duplicate display",
        "refs": [{"source": "spec.pdf", "page": 7}],
        "has_ref": True,
        "excluded_figures": [{
            "source": "spec.pdf",
            "page": 8,
            "verification_status": "needs_review",
        }],
        "review_hint": "inspect figure 8 before asserting the value",
    }
    query_text = _text(adapt_tool_result("query_knowledge", query_payload, budget=budget))
    assert "spec.pdf p.7" in query_text
    assert "excluded_figures: spec.pdf p.8" in query_text
    assert "review: inspect figure 8" in query_text


def test_review_figure_budget_never_keeps_a_partial_canonical_line():
    budget = ResultBudget(120, 360, explicit=False, context_risk=False)
    canonical_line = '   payload: {"address":"0x4000_0100","cells":"' + "x" * 800 + '"}'
    result = adapt_tool_result(
        "review_figures",
        "── figure_id: fig_1\n" + canonical_line,
        budget=budget,
    )
    text = _text(result)
    assert canonical_line not in text
    assert "payload:" not in text
    assert "[result truncated by context budget]" in text


# ═══════════════════════════════════════════════════════════════════════════
# ── 原 test_external_import.py:外部檔案匯入的安全邊界 ──
# ═══════════════════════════════════════════════════════════════════════════


def _enable_import(monkeypatch: pytest.MonkeyPatch, allowed_root: Path) -> None:
    monkeypatch.setattr(config, "EXTERNAL_IMPORT_ENABLED", True)
    monkeypatch.setattr(config, "EXTERNAL_IMPORT_ROOTS", [str(allowed_root)])
    monkeypatch.setattr(config, "EXTERNAL_IMPORT_DEST_DIR", ".aicode_uploads")
    monkeypatch.setattr(config, "EXTERNAL_IMPORT_MAX_BYTES", 1024)
    monkeypatch.setattr(config, "EXTERNAL_IMPORT_ALLOWED_EXTENSIONS", {".png", ".txt", ".pdf", ".bin"})


def test_import_external_file_default_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "project"
    root.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    src = downloads / "error.png"
    src.write_bytes(b"png")

    monkeypatch.setattr(config, "EXTERNAL_IMPORT_ENABLED", False)

    out = import_external_file(str(src), str(root))

    assert "未啟用" in out
    assert not (root / ".aicode_uploads").exists()


def test_import_external_file_copies_allowed_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "project"
    root.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    src = downloads / "error.png"
    src.write_bytes(b"fake-png")
    _enable_import(monkeypatch, downloads)

    out = import_external_file(str(src), str(root))

    dest = root / ".aicode_uploads" / "error.png"
    assert "=== import_external_file" in out
    assert "已匯入: .aicode_uploads/error.png" in out
    assert dest.read_bytes() == b"fake-png"
    assert not (dest.stat().st_mode & 0o111)


def test_import_external_file_rejects_source_outside_allowed_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "project"
    root.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    src = outside / "secret.png"
    src.write_bytes(b"secret")
    _enable_import(monkeypatch, allowed)

    out = import_external_file(str(src), str(root))

    assert "不在允許的匯入來源目錄" in out
    assert not (root / ".aicode_uploads").exists()


def test_import_external_file_rejects_symlink_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "project"
    root.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.png"
    target.write_bytes(b"secret")
    link = allowed / "link.png"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink 在這個平台不支援")
    _enable_import(monkeypatch, allowed)

    out = import_external_file(str(link), str(root))

    assert "不在允許的匯入來源目錄" in out
    assert not (root / ".aicode_uploads").exists()


def test_import_external_file_rejects_unsupported_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "project"
    root.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    src = allowed / "tool.sh"
    src.write_text("echo hi\n", encoding="utf-8")
    _enable_import(monkeypatch, allowed)

    out = import_external_file(str(src), str(root))

    assert "不支援的副檔名" in out
    assert not (root / ".aicode_uploads").exists()


def test_import_external_file_rejects_large_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "project"
    root.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    src = allowed / "big.bin"
    src.write_bytes(b"x" * 2000)
    _enable_import(monkeypatch, allowed)
    monkeypatch.setattr(config, "EXTERNAL_IMPORT_MAX_BYTES", 100)

    out = import_external_file(str(src), str(root))

    assert "檔案太大" in out
    assert not (root / ".aicode_uploads").exists()


def test_import_external_file_rejects_path_dest_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "project"
    root.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    src = allowed / "error.png"
    src.write_bytes(b"png")
    _enable_import(monkeypatch, allowed)

    out = import_external_file(str(src), str(root), dest_name="../evil.png")

    assert "dest_name" in out
    assert not (root / ".aicode_uploads").exists()


def test_import_external_file_avoids_overwriting_existing_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "project"
    upload_dir = root / ".aicode_uploads"
    upload_dir.mkdir(parents=True)
    (upload_dir / "log.txt").write_text("old\n", encoding="utf-8")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    src = allowed / "log.txt"
    src.write_text("new\n", encoding="utf-8")
    _enable_import(monkeypatch, allowed)

    out = import_external_file(str(src), str(root))

    assert "已匯入: .aicode_uploads/log_1.txt" in out
    assert (upload_dir / "log.txt").read_text(encoding="utf-8") == "old\n"
    assert (upload_dir / "log_1.txt").read_text(encoding="utf-8") == "new\n"


def test_import_external_file_reports_already_inside_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "project"
    root.mkdir()
    src = root / "logs" / "build.txt"
    src.parent.mkdir()
    src.write_text("inside\n", encoding="utf-8")
    _enable_import(monkeypatch, tmp_path / "allowed")

    out = import_external_file(str(src), str(root))

    assert "已在 AICODE_ROOT 內" in out
    assert "logs/build.txt" in out


# ═══════════════════════════════════════════════════════════════════════════
# 啟動 argv 的解析:缺值 / 空值 / 吃掉下一個旗標
# ═══════════════════════════════════════════════════════════════════════════
# 這個 parser 是手寫的(整支 server 在 import 期就開始做事,root 必須在
# `set_sandbox_root` 之前定案,來不及用 argparse)。手寫的代價是每一種畸形輸入
# 都得自己想到,而想漏的那幾種**會靜默改變安全語意**:`--root=` 退回 cwd 就綁錯
# 沙箱;`--root --readonly` 把旗標吃成 root 值,於是 root 錯了、readonly 也沒了。


def _parse(argv):
    """只把 `_parse_server_argv` 抽出來跑。

    完整 import `mcp_server` 會拉起 FastMCP / KnowledgeBase / CodeRAG,而這裡
    要驗的只是那個手寫 parser 本身;失敗路徑是 `sys.exit(2)`。
    """
    import ast as _ast

    source = (REPO_ROOT / "mcp_server.py").read_text(encoding="utf-8")
    tree = _ast.parse(source)
    fn = next(
        node
        for node in tree.body
        if isinstance(node, _ast.FunctionDef) and node.name == "_parse_server_argv"
    )
    namespace: dict = {"sys": sys}
    exec(compile(_ast.Module(body=[fn], type_ignores=[]), "<parser>", "exec"), namespace)
    return namespace["_parse_server_argv"](list(argv))


@needs_mcp
@pytest.mark.smoke
def test_the_argv_parser_accepts_the_normal_forms():
    parsed = _parse(["--root", "/tmp/x", "--readonly", "--n-ctx", "65536"])
    assert parsed["root"] == "/tmp/x"
    assert parsed["readonly"] is True
    assert parsed["n_ctx"] == "65536"

    equals = _parse(["--root=/tmp/x", "--n-ctx=8192", "--enable-build-commands"])
    assert equals["root"] == "/tmp/x"
    assert equals["n_ctx"] == "8192"
    assert equals["build_commands"] is True


@needs_mcp
@pytest.mark.smoke
@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--root"], id="root_missing_value"),
        pytest.param(["--root="], id="root_empty_value"),
        pytest.param(["--root", "   "], id="root_blank_value"),
        pytest.param(["--root", "--readonly"], id="root_eats_the_next_flag"),
        pytest.param(["--n-ctx"], id="n_ctx_missing_value"),
        pytest.param(["--n-ctx="], id="n_ctx_empty_value"),
        pytest.param(["--n-ctx", "--readonly"], id="n_ctx_eats_the_next_flag"),
        pytest.param(["--nope"], id="unknown_option"),
    ],
)
def test_malformed_startup_argv_fails_loud(argv):
    """缺值 / 空值 / 值是另一個旗標 → exit 2,不得靜默退回預設。

    `--root --readonly` 特別要擋:吃成 root 值之後,cwd 底下剛好有一個名為
    `--readonly` 的目錄就會讓 server 在**那裡**、而且是 write-enabled 起來。
    """
    with pytest.raises(SystemExit) as excinfo:
        _parse(argv)
    assert excinfo.value.code == 2


# ── 總審 F1-7:匯入不得經 symlink 寫出沙箱 ──


@pytest.mark.smoke
def test_import_never_writes_through_a_dangling_symlink_in_the_upload_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`.aicode_uploads/<name>` 事先被放一個指向沙箱外(尚不存在的檔)的 symlink。

    `Path.exists()` 對 dangling symlink 回 False,於是那個名字被當成可用;
    `shutil.copyfile()` 跟著 symlink 走,在沙箱外**建立**檔案,`chmod` 也作用在
    外部檔上。這是實際的檔案安全邊界破口:一個不信任的 repo 只要 commit 一個
    symlink,使用者核准一次匯入就把 NDA 附件寫到它指定的地方。
    """
    root = tmp_path / "project"
    (root / ".aicode_uploads").mkdir(parents=True)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    src = downloads / "spec.pdf"
    src.write_bytes(b"%PDF-secret")
    outside = tmp_path / "outside-new.pdf"
    (root / ".aicode_uploads" / "spec.pdf").symlink_to(outside)
    _enable_import(monkeypatch, downloads)

    out = import_external_file(str(src), str(root))

    assert not outside.exists(), "沙箱外一個 byte 都不得被寫入"
    assert (root / ".aicode_uploads" / "spec.pdf").is_symlink(), "symlink 本身也不得被換掉"
    # 要嘛拒絕,要嘛落到另一個名字;不管哪一種,內容都要在沙箱內的普通檔裡。
    landed = [p for p in (root / ".aicode_uploads").iterdir() if p.is_file() and not p.is_symlink()]
    if "已匯入" in out:
        assert landed and landed[0].read_bytes() == b"%PDF-secret"
    else:
        assert "錯誤" in out and not landed


@pytest.mark.smoke
def test_import_reads_the_source_it_validated_not_a_swapped_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """來源以 `O_NOFOLLOW` 開一次、用**同一個 fd** 驗與拷:驗完再用路徑重開,
    兩次 lookup 之間換成 symlink 就拷到別的檔。"""
    root = tmp_path / "project"
    root.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(b"%PDF-outside")
    link = downloads / "spec.pdf"
    link.symlink_to(secret)
    _enable_import(monkeypatch, downloads)

    out = import_external_file(str(link), str(root))

    assert "錯誤" in out, out
    assert not (root / ".aicode_uploads").exists() or not any(
        (root / ".aicode_uploads").iterdir()
    )


# ── 總審 F1-16:`--readonly` 的第二層要擋**所有**寫入工具 ──


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("record_lesson", {"rule": "x", "scope": "project"}),
        ("remove_document", {"source": "spec.pdf"}),
        ("ingest_document", {"path": "spec.pdf"}),
        ("review_figures", {"action": "fix", "document_id": "d", "figure_id": "f", "fix": {}}),
        ("import_external_file", {"source_path": "/tmp/x.pdf"}),
    ],
)
@pytest.mark.smoke
def test_a_readonly_server_refuses_every_mutator(mcp_module_factory, tool, arguments, tmp_path):
    """readonly replay 的契約是「前後 project state 不變」。

    只關 `PATCH_ENABLED` / `RUN_COMMAND_ENABLED` 擋得住 apply_patch 與
    run_command,擋不住改 `knowledge.json`、figure state 與全域 lessons 的那幾個。
    客戶端那一層若有 bug、或別的 MCP client 直接連進來,replay 邊界就沒了。
    判準與客戶端一致:`readOnlyHint` 不是 True 的工具一律拒絕。
    """
    module = mcp_module_factory(readonly=True)
    fn = getattr(module, tool).fn
    with pytest.raises(module.ReadonlyToolRefused, match="readonly"):
        fn(**arguments)
    assert not (tmp_path / "proj" / "knowledge.json").exists()
    # 唯讀工具照常可用(這一層只擋 mutator)。
    assert isinstance(module.list_dir.fn(path="."), str)


@pytest.fixture
def mcp_module_factory(monkeypatch, tmp_path):
    """以 tmp_path 當 root import mcp_server;`readonly=True` 時把啟動旗標翻成 readonly。

    `_ARGS` 是 import 期由 argv 定案的;`_tool` 包裝在**呼叫時**讀它,所以 import
    之後改這個 dict 就能在同一個行程裡驗第二層(不必真的 spawn)。
    """
    def make(*, readonly: bool = False):
        from tests._harness import seed_home

        monkeypatch.setenv("HOME", str(seed_home(tmp_path / "home")))
        root = tmp_path / "proj"
        root.mkdir(exist_ok=True)
        module = import_mcp_module(monkeypatch, root)
        module._ARGS["readonly"] = readonly  # noqa: SLF001
        return module

    yield make
    sys.modules.pop("mcp_server", None)


# ── 總審 F1-10 / F1-15:`--client-config` 是 server 的啟動參數,並交給 RAG 子行程 ──


@needs_mcp
@pytest.mark.smoke
def test_the_server_accepts_an_explicit_client_config_path():
    parsed = _parse(["--root", "/tmp/x", "--client-config", "/tmp/c.json"])
    assert parsed["client_config"] == "/tmp/c.json"
    for bad in (["--client-config"], ["--client-config="], ["--client-config", "--readonly"],
                ["--client-config", "/a", "--client-config", "/b"]):
        with pytest.raises(SystemExit) as excinfo:
            _parse(bad)
        assert excinfo.value.code == 2


@pytest.mark.smoke
def test_the_rag_subprocess_receives_the_same_client_config(mcp_module_factory, tmp_path, monkeypatch):
    """MCP 行程內套用的 client.json 設定(例如 objdump)必須跟著進 RAG.py 子行程。

    RAG.py 是新起的行程,`config.OBJDUMP` 在它那邊仍是 repo 預設 —— 不把設定檔路徑
    交過去,`ingest_document(mode="binary")` 就用錯工具。
    """
    home = tmp_path / "home"
    cfg = home / ".config" / "codetrail" / "client.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"schema": 1, "compaction_mode": "manual", "objdump": "/opt/bin/objdump"}), encoding="utf-8")
    cfg.chmod(0o600)
    module = mcp_module_factory()
    assert module.config.OBJDUMP == "/opt/bin/objdump"

    seen: dict = {}

    def fake_run(cmd, *, timeout):
        seen["cmd"] = list(cmd)
        raise RuntimeError("stop here")

    monkeypatch.setattr(module, "_run_rag_subprocess", fake_run)
    doc = tmp_path / "proj" / "spec.txt"
    doc.write_text("hello", encoding="utf-8")
    module.ingest_document.fn(path="spec.txt")
    assert "--client-config" in seen["cmd"], seen
    assert seen["cmd"][seen["cmd"].index("--client-config") + 1] == str(cfg)


# ── 總審 F2-2:核准框與工具對「落點」用同一份輸入(檔名與目錄) ──


@pytest.mark.smoke
def test_a_symlinked_source_lands_under_the_name_the_user_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """核准框用模型給的 `source_path` 的 basename 算落點;工具若改用 resolve 之後的
    `src.name`,使用者核准的是 `alias.pdf`、實際寫進去的是 `real.pdf`。"""
    import client_engine

    root = tmp_path / "project"
    root.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    real = downloads / "real.pdf"
    real.write_bytes(b"%PDF-real")
    alias = downloads / "alias.pdf"
    alias.symlink_to(real)
    _enable_import(monkeypatch, downloads)

    shown = client_engine.ApprovalRequest(
        "s", "import_external_file", {"source_path": str(alias)}
    ).render()
    out = import_external_file(str(alias), str(root))

    assert "✓" in out, out
    landed = sorted(p.name for p in (root / ".aicode_uploads").iterdir())
    assert landed == ["alias.pdf"], landed
    assert ".aicode_uploads/alias.pdf" in shown, shown


@pytest.mark.smoke
def test_the_approval_box_and_the_tool_agree_on_the_upload_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """目的目錄是 `config.EXTERNAL_IMPORT_DEST_DIR`;核准框把 `.aicode_uploads` 寫死,
    改了常數之後人看到的落點就不是真的落點。"""
    import client_engine

    root = tmp_path / "project"
    root.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    spec = downloads / "spec.pdf"
    spec.write_bytes(b"%PDF-spec")
    _enable_import(monkeypatch, downloads)
    monkeypatch.setattr(config, "EXTERNAL_IMPORT_DEST_DIR", "incoming")

    shown = client_engine.ApprovalRequest(
        "s", "import_external_file", {"source_path": str(spec)}
    ).render()
    out = import_external_file(str(spec), str(root))

    assert "✓" in out, out
    assert (root / "incoming" / "spec.pdf").is_file()
    assert "incoming/spec.pdf" in shown and ".aicode_uploads" not in shown, shown


# ── 總審 F2-3:來源的父目錄在驗證之後被換成 symlink ──


@pytest.mark.smoke
def test_import_refuses_a_parent_directory_swapped_for_a_symlink_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`O_NOFOLLOW` 只擋路徑**最後一段**的 symlink。驗證(resolve + 允許目錄比對)
    與 open 之間,中間某一層目錄被換成指向沙箱外的 symlink,path-based 的 open 會
    跟著走進去。來源必須從允許目錄的 dir-fd 一段一段 `O_NOFOLLOW` 開下去。"""
    import external_import

    root = tmp_path / "project"
    root.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    sub = downloads / "sub"
    sub.mkdir()
    (sub / "spec.pdf").write_bytes(b"%PDF-inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "spec.pdf").write_bytes(b"%PDF-OUTSIDE")
    _enable_import(monkeypatch, downloads)

    real_roots = external_import._configured_import_roots

    def swapping_roots():
        roots = real_roots()
        # 驗證用的 resolve 已經做完;模擬另一個行程在 open 之前把父目錄換成 symlink。
        if sub.is_dir() and not sub.is_symlink():
            sub.rename(tmp_path / "sub-moved")
            sub.symlink_to(outside)
        return roots

    monkeypatch.setattr(external_import, "_configured_import_roots", swapping_roots)

    out = import_external_file(str(downloads / "sub" / "spec.pdf"), str(root))

    assert "錯誤" in out, out
    uploads = root / ".aicode_uploads"
    assert not uploads.exists() or not any(uploads.iterdir())


# ── 總審 F2-4:被拒絕的匯入不得洩漏來源 fd ──


@pytest.mark.smoke
def test_a_rejected_import_does_not_leak_the_source_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """來源 fd 開了之後,大小 / dest_name / 副檔名的每一個拒絕分支都要 close。
    長時間跑的 MCP 反覆收到合理的失敗請求就把 fd 耗光(EMFILE),只能重啟。"""
    import os

    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("需要 /proc 才數得出 fd")
    root = tmp_path / "project"
    root.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    big = downloads / "big.pdf"
    big.write_bytes(b"x" * 4096)
    ok = downloads / "ok.pdf"
    ok.write_bytes(b"%PDF-ok")
    _enable_import(monkeypatch, downloads)  # MAX_BYTES = 1024

    def open_fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    before = open_fds()
    for _ in range(8):
        assert "太大" in import_external_file(str(big), str(root))
    assert "錯誤" in import_external_file(str(ok), str(root), dest_name="x.exe")
    assert "錯誤" in import_external_file(str(ok), str(root), dest_name="a/b.pdf")
    assert open_fds() == before


# ── 總審 F3-2:允許目錄**本身**的祖先在驗證之後被換成 symlink ──


@pytest.mark.smoke
def test_import_refuses_an_allowed_root_whose_ancestor_was_swapped_for_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """允許目錄若位於別人可控的父目錄下(`/tmp/drop-owner/Documents`),父目錄在
    resolve 之後被換成指向外面的 symlink,path-based 開 allowed root 的那一步會跟著走,
    之後逐段 openat 全部建立在錯的錨點上。允許目錄自己也要從 `/` 逐段 O_NOFOLLOW 開。"""
    import external_import

    root = tmp_path / "project"
    root.mkdir()
    drop = tmp_path / "drop"
    documents = drop / "Documents"
    documents.mkdir(parents=True)
    (documents / "spec.pdf").write_bytes(b"%PDF-inside")
    outside = tmp_path / "outside"
    (outside / "Documents").mkdir(parents=True)
    (outside / "Documents" / "spec.pdf").write_bytes(b"%PDF-OUTSIDE")
    _enable_import(monkeypatch, documents)

    real_roots = external_import._configured_import_roots

    def swapping_roots():
        roots = real_roots()  # 先用真目錄算好(模擬驗證已完成)
        if drop.is_dir() and not drop.is_symlink():
            drop.rename(tmp_path / "drop-moved")
            drop.symlink_to(outside)
        return roots

    monkeypatch.setattr(external_import, "_configured_import_roots", swapping_roots)

    out = import_external_file(str(documents / "spec.pdf"), str(root))

    assert "錯誤" in out, out
    uploads = root / ".aicode_uploads"
    assert not uploads.exists() or not any(uploads.iterdir())


# ── 總審第 4 輪 NON-BLOCKER:允許目錄的祖先只有 x 沒有 r 也要走得過 ──


@pytest.mark.smoke
def test_import_traverses_an_execute_only_ancestor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """逐段開目錄用 `O_PATH`(只要 execute 權限),不是 `O_RDONLY`:自訂來源若在一個
    只給 x、不給 r 的祖先目錄下(常見的 `/srv/drop/<user>` 佈局),`O_RDONLY` 會 EACCES,
    而舊的 path-based open 本來走得過 —— 那是修 F3-2 引入的回歸。"""
    import os

    if os.geteuid() == 0:
        pytest.skip("root 不受目錄權限限制")
    root = tmp_path / "project"
    root.mkdir()
    gate = tmp_path / "gate"
    downloads = gate / "Downloads"
    downloads.mkdir(parents=True)
    (downloads / "spec.pdf").write_bytes(b"%PDF-spec")
    _enable_import(monkeypatch, downloads)
    gate.chmod(0o311)
    try:
        out = import_external_file(str(downloads / "spec.pdf"), str(root))
    finally:
        gate.chmod(0o755)
    assert "✓" in out, out
    assert (root / ".aicode_uploads" / "spec.pdf").read_bytes() == b"%PDF-spec"
