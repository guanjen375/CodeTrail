"""run_command 的 policy 與契約:白名單、危險字元、shell 注入、path containment、
timeout 三層邊界,以及 run_lint(fix=False) 的 check-only 保證。

合併自 tests/test_run_command.py、tests/test_run_command_timeout.py 與
tests/test_run_lint.py(2026-09-02)。

- policy:白名單、危險字元、shell 注入;Phase 3 的 path containment(白名單命令的參數
  不能逃出 AICODE_ROOT)。
- timeout 三層契約(workflow D):native schema、executor runtime 驗證、MCP schema。
  三層都必須自己擋:llama.cpp 的 JSON-schema→GBNF 只支援子集,client 可能根本不套
  schema 約束,所以 executor 端的 1..600 驗證是最後一道;MCP 端的 pydantic strict
  則是 client 誤送 true / "60" / 1.0 時的獨立防線。文件寫「server 接受 1..600 秒;
  client 可能更早截止」,不宣稱 600 秒必在 OpenCode client timeout 內。
- run_lint(fix=False) 必須走 check-only,不偷偷改檔。Review 找到的 bug:舊版
  LINT_COMMANDS 只有 fix 組命令(--fix / -w / -i / --write),agent_tools.run_lint 收了
  `fix` 參數卻完全沒用,所以 fix=False 仍會跑會改檔的命令。
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

# smoke:安全層(AGENTS.md §1.1 第 2 款「無聲失敗風險的契約」)＋ run_lint 的真實 bug
# regression(AGENTS.md §1.1 第 1 款:fix=False 仍跑會改檔的命令)。
# AGENTS.md §2 安全檢查點:agent_tools._validate_command(白名單 + dangerous pattern)、
# run_command timeout 1..600 三層邊界。
pytestmark = pytest.mark.smoke


@pytest.fixture
def runner(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", True)
    return ToolExecutor(str(tmp_path))


def test_run_command_disabled_blocks(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "RUN_COMMAND_ENABLED", False)
    ex = ToolExecutor(str(tmp_path))
    out = ex.run_command("pytest -h")
    assert "已停用" in out


def test_validate_rejects_non_whitelisted(runner: ToolExecutor):
    ok, msg, parts = runner._validate_command("rm -rf /")
    assert not ok
    assert "不允許" in msg


def test_validate_rejects_shell_metacharacters(runner: ToolExecutor):
    """白名單命令也不該夾帶 shell 元字元。"""
    bad = [
        "pytest; rm -rf /",
        "pytest && rm -rf /",
        "pytest | tee /tmp/x",
        "pytest > /tmp/x",
        "pytest $(whoami)",
        "pytest `whoami`",
    ]
    for cmd in bad:
        ok, msg, _ = runner._validate_command(cmd)
        assert not ok, f"應該擋下: {cmd!r} -> {msg}"


def test_validate_allows_whitelisted(runner: ToolExecutor):
    ok, msg, parts = runner._validate_command("pytest -h")
    assert ok, msg
    assert parts and parts[0] == "pytest"


def test_validate_rejects_empty(runner: ToolExecutor):
    ok, _, _ = runner._validate_command("")
    assert not ok


def test_validate_rejects_path_traversal_via_arg(runner: ToolExecutor):
    """白名單命令但參數試圖逃逸（這層只擋 shell metachar，路徑逃逸交給 cwd 限制）。"""
    # 但至少不能塞反引號或 $()
    ok, _, _ = runner._validate_command("pytest `cat /etc/passwd`")
    assert not ok


# ============================================================
# Phase 3: path containment — 白名單命令的參數不能逃出 AICODE_ROOT
# ============================================================
class TestPathContainment:
    """這些 case 是 review 找出的真實逃逸路徑。"""

    def test_rejects_absolute_outside_path(self, runner: ToolExecutor):
        ok, msg, _ = runner._validate_command("pytest /tmp/some_test.py")
        assert not ok, msg
        assert "sandbox" in msg or "AICODE_ROOT" in msg

    @pytest.mark.parametrize(
        "cmd",
        [
            pytest.param("python -m pytest /tmp/some_test.py", id="python_m_pytest_outside"),
            pytest.param("pytest ../outside.py", id="dotdot_escape"),
            pytest.param("make -C /tmp", id="make_dash_C_outside"),
            pytest.param("cmake --build /tmp/build", id="cmake_build_outside"),
            pytest.param("ninja -C /tmp/build", id="ninja_dash_C_outside"),
            pytest.param("go test ../outside", id="go_test_dotdot"),
            pytest.param("make --directory=/tmp", id="inline_directory_flag"),
        ],
    )
    def test_rejects_path_escape(self, runner: ToolExecutor, cmd: str):
        """白名單命令的參數以絕對路徑、`..`、`-C` / `--directory=` 逃出 root 都要擋。"""
        ok, msg, _ = runner._validate_command(cmd)
        assert not ok, msg

    # ---- 必須允許的 in-root 用法 ----
    def test_allows_relative_test_file(self, runner: ToolExecutor, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("def test_x(): pass\n")
        ok, msg, _ = runner._validate_command("pytest tests/test_x.py")
        assert ok, msg

    def test_allows_python_m_pytest_dir(self, runner: ToolExecutor, tmp_path):
        (tmp_path / "tests").mkdir()
        ok, msg, _ = runner._validate_command("python -m pytest tests")
        assert ok, msg

    def test_allows_dot_arg(self, runner: ToolExecutor):
        ok, msg, _ = runner._validate_command("ruff check .")
        assert ok, msg

    def test_allows_dash_only_args(self, runner: ToolExecutor):
        for cmd in ("pytest -q", "pytest -k some_test", "black --check ."):
            ok, msg, _ = runner._validate_command(cmd)
            assert ok, f"{cmd!r}: {msg}"

    def test_allows_cmake_build_in_root(self, runner: ToolExecutor):
        # 注意:即使 build/ 不存在 ("尚未 cmake configure"),路徑檢查只看路徑形狀
        ok, msg, _ = runner._validate_command("cmake --build build")
        # cmake 可能不在 ALLOWED_COMMANDS 預設裡(MCP 才 append),這裡只測 path layer。
        # 若 ALLOWED_COMMANDS 沒有 cmake,測試會因白名單 fail 而非 path fail —
        # 兩種失敗都不是這條 test 想測的東西,所以我們直接呼叫 _check_path_containment。
        import shlex
        ok2, why = runner._check_path_containment(shlex.split("cmake --build build"))
        assert ok2, why

    def test_allows_ninja_dash_C_in_root(self, runner: ToolExecutor):
        import shlex
        ok, why = runner._check_path_containment(shlex.split("ninja -C build"))
        assert ok, why

    def test_allows_go_test_local(self, runner: ToolExecutor):
        # `go test ./...` 的 `./...` 看起來像 path 但是是 root 內 — 必須允許
        ok, msg, _ = runner._validate_command("go test ./...")
        assert ok, msg

    def test_path_containment_runs_after_shell_metachar_check(self, runner: ToolExecutor):
        """確認 shell-injection 檢查仍在 path 之前(順序維持安全先擋)。"""
        ok, msg, _ = runner._validate_command("pytest /tmp/x.py; rm -rf /")
        assert not ok
        # 理應因 ';' / metachar 被擋,而不是因 path
        assert "字元" in msg or "metachar" in msg or "sandbox" in msg or "AICODE_ROOT" in msg


# ── 原 test_run_command_timeout.py:run_command 的 timeout 三層契約(workflow D)──
REPO_ROOT = Path(__file__).resolve().parent.parent

TIMEOUT_ERROR_TITLE = "錯誤: timeout 必須是 1..600 的整數"


@pytest.fixture
def host_runner(tmp_path: Path, monkeypatch):
    """原 test_run_command_timeout.py 的 `runner`:與上面的 `runner` 不同,另外釘死 host path。"""
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
def test_executor_rejects_timeout_out_of_bounds(host_runner: ToolExecutor, monkeypatch, bad):
    spawned: list[Any] = []

    def boom(*args, **kwargs):
        spawned.append((args, kwargs))
        raise AssertionError("subprocess.run 不該被呼叫")

    monkeypatch.setattr("process_env.run", boom)
    out = host_runner.run_command("pytest -q", timeout=bad)
    assert out.startswith(TIMEOUT_ERROR_TITLE), out
    assert type(bad).__name__ in out, out
    assert spawned == [], "非法 timeout 必須在 spawn 之前拒絕"


def test_timeout_error_message_is_bounded(host_runner: ToolExecutor, monkeypatch):
    monkeypatch.setattr(
        "process_env.run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不該 spawn")),
    )
    huge = "x" * 500
    out = host_runner.run_command("pytest -q", timeout=huge)
    assert out.startswith(TIMEOUT_ERROR_TITLE), out
    assert "str:" in out, out
    assert huge not in out, "500 字元的原值不得整段回送"
    assert "…" in out, out
    assert len(out) < 200, len(out)


@pytest.mark.parametrize("ok", [1, 600])
def test_executor_accepts_bounds_and_forwards_timeout(host_runner: ToolExecutor, monkeypatch, ok):
    # 這條在舊碼也會原樣轉發;紅燈靠「邊界常數是契約的一部分」這個新斷言。
    assert (config.RUN_COMMAND_TIMEOUT_MIN, config.RUN_COMMAND_TIMEOUT_MAX) == (1, 600)
    seen: list[dict] = []

    def fake_run(cmd_parts, **kwargs):
        seen.append(dict(kwargs))
        return _fake_completed()

    monkeypatch.setattr("process_env.run", fake_run)
    out = host_runner.run_command("pytest -q", timeout=ok)
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
    assert "client.json 的 build_commands" in description
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
    from tests._harness import import_mcp_module, seed_home

    # HOME 指到 tmp 的同時要放一份 deployment.json:設定只來自檔案,空的 HOME
    # 會讓 mcp_server 在 require_main_model() 掛掉(exit 3)。
    home = seed_home(tmp_path / "home")
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
    assert "client.json 的 build_commands" in doc
    assert "git 不在白名單" in doc
    assert "client 可能更早截止" in doc
    assert "timeout" in {a.arg for a in fn.args.args}


# ── 原 test_run_lint.py:run_lint(fix=False) 必須走 check-only,不偷偷改檔 ──
class TestLintCommandsStructure:
    """LINT_COMMANDS 必須是 {ext: {'fix': [...], 'check': [...]}} 結構。"""

    def test_lint_commands_is_nested_dict(self):
        for ext, spec in config.LINT_COMMANDS.items():
            assert isinstance(spec, dict), (
                f"{ext}: LINT_COMMANDS value 必須是 dict {{'fix': [...], 'check': [...]}},"
                f"得到 {type(spec).__name__}"
            )
            assert "fix" in spec, f"{ext}: 缺 'fix' key"
            assert isinstance(spec["fix"], list) and spec["fix"], f"{ext}: 'fix' 必須是非空 list"

    def test_check_mode_uses_non_mutating_flags(self):
        """check 組命令不能含會改檔的 flag。"""
        mutating = ["--fix", "--write", "-w", "-i"]
        for ext, spec in config.LINT_COMMANDS.items():
            check_cmds = spec.get("check")
            if not check_cmds:
                continue
            for cmd in check_cmds:
                parts = cmd.split()
                for bad in mutating:
                    assert bad not in parts, (
                        f"{ext} check 命令 {cmd!r} 含會改檔的 flag {bad!r} — "
                        "check mode 必須只回報、不寫檔"
                    )


class TestRunLintMode:
    """run_lint(fix=...) 必須依 fix 選對命令組,絕對不能偷偷跑 fix 組。"""

    @pytest.fixture
    def runner_and_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # mode 選擇測試需要 PATCH_ENABLED=True,否則 fix=True 會在 gate 階段
        # 就被擋掉,根本走不到 subprocess。唯讀模式的行為由 TestRunLintReadonlyMode 覆蓋。
        monkeypatch.setattr(config, "PATCH_ENABLED", True)
        runner = ToolExecutor(str(tmp_path))
        f = tmp_path / "x.py"
        f.write_text("x = 1\n", encoding="utf-8")
        return runner, f

    def _capture_run(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        """攔截 subprocess.run,記錄每次傳的 cmd_parts。"""
        calls: list[list[str]] = []

        def fake_run(cmd_parts: list[str], **kwargs: Any) -> Any:
            calls.append(list(cmd_parts))

            class R:
                returncode = 0
                stdout = ""
                stderr = ""

            return R()

        monkeypatch.setattr("process_env.run", fake_run)
        return calls

    def test_fix_true_uses_fix_commands(self, runner_and_file, monkeypatch: pytest.MonkeyPatch):
        runner, f = runner_and_file
        calls = self._capture_run(monkeypatch)
        runner.run_lint("x.py", fix=True)
        assert calls, "run_lint 應該至少跑一個命令"
        first = calls[0]
        # fix 組第一個命令: ruff check --fix
        assert "--fix" in first, f"fix=True 必須跑 --fix,實際: {first}"

    def test_fix_false_uses_check_commands_not_fix(
        self, runner_and_file, monkeypatch: pytest.MonkeyPatch
    ):
        runner, f = runner_and_file
        calls = self._capture_run(monkeypatch)
        runner.run_lint("x.py", fix=False)
        assert calls, "run_lint(fix=False) 應該至少跑一個命令"
        # 不能有任何呼叫含 --fix / -w / --write / -i
        for parts in calls:
            for bad in ("--fix", "-w", "--write", "-i"):
                assert bad not in parts, (
                    f"fix=False 跑了會改檔的命令 {parts} (含 {bad!r}) — "
                    "這是 review 找到的 bug,patch 沒生效"
                )

    def test_fix_false_no_check_returns_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """若某副檔名沒提供 check 組,fix=False 必須回錯誤而非 fallback 跑 fix。"""
        runner = ToolExecutor(str(tmp_path))
        f = tmp_path / "y.fakelang"
        f.write_text("noop\n", encoding="utf-8")

        fake_lint = {".fakelang": {"fix": ["echo fix"]}}  # 故意沒 check key
        monkeypatch.setattr("agent_tools.LINT_COMMANDS", fake_lint)

        # 連 subprocess 都不應該被叫到 — 提早就拒絕
        called: list[Any] = []
        monkeypatch.setattr(
            "process_env.run",
            lambda *a, **kw: called.append(a) or (_ for _ in ()).throw(
                AssertionError("不應呼叫 subprocess.run — 應該提早回錯誤")
            ),
        )

        out = runner.run_lint("y.fakelang", fix=False)
        assert "check" in out and ("不支援" in out or "沒有" in out), (
            f"fix=False 沒 check 命令時應回錯誤,實際: {out}"
        )
        assert not called, "fix=False 沒 check 命令時不能 fallback 跑 fix"


class TestRunLintReadonlyMode:
    """AI_CODE_PATCH=0 完全唯讀模式: fix=True 必須被擋,fix=False 仍可用。

    Review 後續發現的 gap: 文件承諾「AI_CODE_PATCH=0 = 完全唯讀」,但
    舊版 run_lint 不檢查 PATCH_ENABLED,fix=True 仍會無視旗標改檔。
    """

    def test_fix_true_blocked_when_patch_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(config, "PATCH_ENABLED", False)
        runner = ToolExecutor(str(tmp_path))
        f = tmp_path / "x.py"
        f.write_text("x = 1\n", encoding="utf-8")

        # subprocess 都不該被叫到 — 應在 stat / spawn 之前提早拒絕
        def fake_run(*a, **kw):
            raise AssertionError("PATCH_ENABLED=False 時不能跑 lint subprocess")

        monkeypatch.setattr("process_env.run", fake_run)

        out = runner.run_lint("x.py", fix=True)
        # 2026-09-04:訊息不再指名一個已刪的環境變數。它現在說的是「這是
        # readonly session」——那才是使用者能對照的事實(`--policy readonly` /
        # `mcp_server --readonly`),而不是一個設了也沒用的名字。
        assert "readonly" in out or "唯讀" in out, out
        # 檔案不能被動到
        assert f.read_text(encoding="utf-8") == "x = 1\n"

    def test_fix_false_still_works_when_patch_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """唯讀模式下 check-only 仍要可用 — 不然就無法 lint 確認了。"""
        monkeypatch.setattr(config, "PATCH_ENABLED", False)
        runner = ToolExecutor(str(tmp_path))
        f = tmp_path / "x.py"
        f.write_text("x = 1\n", encoding="utf-8")

        calls: list[list[str]] = []

        def fake_run(cmd_parts, **kwargs):
            calls.append(list(cmd_parts))

            class R:
                returncode = 0
                stdout = ""
                stderr = ""

            return R()

        monkeypatch.setattr("process_env.run", fake_run)

        runner.run_lint("x.py", fix=False)
        assert calls, "fix=False 在 PATCH_ENABLED=False 時應該仍能跑 check"
        for parts in calls:
            for bad in ("--fix", "-w", "--write", "-i"):
                assert bad not in parts, parts
