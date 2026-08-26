"""repo 一致性:README 的工具清單/旗標,與 eval/ 對 config 及原始碼的漂移。

合併自 tests/test_readme_consistency.py 與 tests/test_eval_consistency.py(2026-08-20)。
兩者都是把 scripts/check_*_consistency.py 暴露成 pytest,失敗時直接看到 drift list。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.check_eval_consistency import check_all as eval_check_all
from scripts.check_readme_consistency import (
    _check_agents_template_tools,
    _check_code_model_placeholder_contract,
    _check_default_aux_models_documented,
    _check_doctor_commands_have_explicit_model,
    _check_forbidden_main_model_tokens,
    _check_opencode_timeout_contract,
    _check_permission_template_contract,
    _config_int_constant,
    _config_model_values,
    _mcp_tool_names,
    _readme_claimed_tool_count,
    _readme_tool_names_in_table,
    check_all,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.smoke
def test_user_facing_python_commands_use_python3():
    """基底安裝只保證 python3；文件與 CLI 提示不可要求額外的 python alias。"""
    user_facing_sources = [
        *sorted(REPO_ROOT.glob("*.md")),
        *sorted((REPO_ROOT / "docs").rglob("*.md")),
        REPO_ROOT / "RAG.py",
        REPO_ROOT / "data_flywheel.py",
        REPO_ROOT / "figure_candidates.py",
        REPO_ROOT / "knowledge.py",
        REPO_ROOT / "lessons.py",
        REPO_ROOT / "mcp_server.py",
        REPO_ROOT / "scripts" / "check_readme_consistency.py",
        REPO_ROOT / "scripts" / "doctor.py",
        REPO_ROOT / "scripts" / "index_stats.py",
        REPO_ROOT / "scripts" / "kb_ab_compare.py",
        REPO_ROOT / "scripts" / "lessons_check.py",
        REPO_ROOT / "scripts" / "opencode_contract_check.py",
        REPO_ROOT / "scripts" / "run_tests.py",
        REPO_ROOT / "scripts" / "set_config.py",
    ]
    stale: list[str] = []
    command = re.compile(
        r"(?<![\w])python(?=[ \t]+(?:-m[ \t]+[A-Za-z0-9_.-]+|"
        r"[A-Za-z0-9_./-]+\.py\b))"
    )

    for path in user_facing_sources:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if command.search(line):
                stale.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")

    assert not stale, "user-facing commands must use python3:\n" + "\n".join(stale)


def test_no_readme_drift():
    issues = check_all()
    assert not issues, "README/docs drift:\n" + "\n".join(f"  - {i}" for i in issues)


def test_mcp_tool_names_extraction_works_on_a_known_pattern():
    sample = """
@mcp.tool()
def query_knowledge(question: str) -> dict:
    pass

@mcp.tool()
def code_rag_search(query: str, top_k: int = 5):
    pass
"""
    assert _mcp_tool_names(sample) == ["query_knowledge", "code_rag_search"]


def test_readme_tool_count_recognises_phrases():
    assert _readme_claimed_tool_count("暴露的 11 個工具") == 11
    assert _readme_claimed_tool_count("9 個 MCP 工具") == 9
    assert _readme_claimed_tool_count("沒提到工具數") is None


def test_readme_tool_table_extracts_backtick_calls():
    sample = "| `query_knowledge(question)` | 查 KB | `code_rag_search(query, top_k=5)` | RAG | foo() | bar"
    names = _readme_tool_names_in_table(sample)
    assert "query_knowledge" in names
    assert "code_rag_search" in names


def test_config_model_values_parse_literals_and_env_defaults():
    sample = '''
EMBEDDING_MODEL = _os.environ.get("AICODE_EMBED_MODEL", "bge-m3")
RERANKER_MODEL = _os.environ.get("AICODE_RERANK_MODEL", "qwen3-reranker-0.6b")
VL_MODEL = _os.environ.get("AICODE_VL_MODEL", "qwen3.5-9b")
MODEL = _resolve_main_model()
'''
    assert _config_model_values(sample) == {
        "EMBEDDING_MODEL": "bge-m3",
        "RERANKER_MODEL": "qwen3-reranker-0.6b",
        "VL_MODEL": "qwen3.5-9b",
    }


def test_opencode_timeout_contract_matches_runtime_constant():
    config_text = "OPENCODE_MCP_TIMEOUT_MIN_MS = 660_000\n"
    assert (
        _config_int_constant(config_text, "OPENCODE_MCP_TIMEOUT_MIN_MS") == 660_000
    )

    issues: list[str] = []
    _check_opencode_timeout_contract(
        '{"mcp":{"codetrail":{"timeout": 660000}}}',
        config_text,
        issues,
    )
    assert issues == []

    _check_opencode_timeout_contract(
        '{"mcp":{"codetrail":{"timeout": 10000}}}',
        config_text,
        issues,
    )
    assert any("660000" in issue for issue in issues)


def test_code_model_placeholder_contract_passes_with_llamacpp_setup():
    """新版範本:llama-server / GGUF 都提到、model 有 custom-provider prefix。"""
    readme = '''
本專案使用 llama-server 跑 GGUF 模型。

```json
{
  "model": "llamacpp/<CODE_MODEL>",
  "provider": {
    "llamacpp": {
      "models": {
        "<CODE_MODEL>": { "name": "<CODE_MODEL>" }
      }
    }
  }
}
```
'''
    docs = readme  # docs_text 包含 readme 本身
    issues: list[str] = []
    _check_code_model_placeholder_contract(readme, docs, issues)
    assert issues == []


def test_code_model_placeholder_contract_reports_missing_bits():
    """空文件應該被報缺 placeholder / llama-server / GGUF / opencode model 範本。"""
    issues: list[str] = []
    _check_code_model_placeholder_contract("", "", issues)
    assert any("<CODE_MODEL>" in issue for issue in issues)
    assert any("llama-server" in issue for issue in issues)
    assert any("GGUF" in issue for issue in issues)
    assert any('"model"' in issue for issue in issues)


def test_doctor_commands_must_have_explicit_model_on_same_line():
    issues: list[str] = []

    _check_doctor_commands_have_explicit_model(
        "python scripts/doctor.py\n"
        "AICODE_MODEL=<CODE_MODEL> python scripts/doctor.py\n",
        issues,
    )

    assert len(issues) == 1
    assert "AICODE_MODEL=<CODE_MODEL>" in issues[0]


def test_default_aux_models_must_be_documented():
    services = {
        "embedding": {"model": "embed-model"},
        "reranker": {"model": "rerank-model"},
        "vl": {"model": "vl-model"},
    }
    issues: list[str] = []

    _check_default_aux_models_documented(
        services,
        "下載 embed-model 與 vl-model。",
        issues,
    )

    assert issues == ["README/docs 未提到預設 reranker 模型 'rerank-model'"]


def test_forbidden_main_model_tokens_are_detected_without_flagging_placeholders():
    bad_tokens = "\n".join(
        [
            "DEFAULT" + "_MODEL",
            "RECOMMENDED" + "_MODEL",
            "<" + "default" + ">",
            "qwen3" + "-coder:30b",
        ]
    )
    issues: list[str] = []

    _check_forbidden_main_model_tokens("<CODE_MODEL>\n" + bad_tokens, issues)

    assert len(issues) == 4


def test_permission_template_contract_passes_when_identical():
    readme = """
```json
{
  "permission": {
    "*": "deny",
    "plan_enter": "allow",
    "codetrail_*": "allow",
    "codetrail_apply_patch": "ask"
  }
}
```
"""
    set_config = (
        '_OPENCODE_PERMISSION_TEMPLATE = {\n'
        '    "*": "deny",\n'
        '    "plan_enter": "allow",\n'
        '    "codetrail_*": "allow",\n'
        '    "codetrail_apply_patch": "ask",\n'
        '}\n'
    )
    issues: list[str] = []
    _check_permission_template_contract(readme, set_config, issues)
    assert issues == []


def test_permission_template_contract_reports_drift_and_bad_order():
    # README 少了一鍵、且 ask 覆寫排在 wildcard 之前(last-match-wins 下會失效)
    readme = '"permission": { "*": "deny", "codetrail_apply_patch": "ask", "codetrail_*": "allow" }'
    set_config = (
        '_OPENCODE_PERMISSION_TEMPLATE = {"*": "deny", "codetrail_*": "allow", '
        '"codetrail_apply_patch": "ask", "codetrail_remove_document": "ask"}'
    )
    issues: list[str] = []
    _check_permission_template_contract(readme, set_config, issues)
    assert any("不一致" in i for i in issues)
    assert any("排在 codetrail_* 之前" in i for i in issues)


def test_agents_template_tool_list_contract():
    issues: list[str] = []
    _check_agents_template_tools(
        "CodeTrail 工具共 2 個:`codetrail_read_file`、`codetrail_git_diff`。",
        ["read_file", "git_diff"],
        issues,
    )
    assert issues == []

    # 缺一個真工具、多一個假工具、數量寫錯,三種都要抓到
    issues = []
    _check_agents_template_tools(
        "CodeTrail 工具共 3 個:`codetrail_read_file`、`codetrail_ghost_tool`。",
        ["read_file", "git_diff"],
        issues,
    )
    assert any("缺少" in i for i in issues)
    assert any("沒有的工具" in i for i in issues)
    assert any("實際有 2 個" in i for i in issues)

    # 範本檔不存在也要報
    issues = []
    _check_agents_template_tools("", ["read_file"], issues)
    assert any("不存在" in i for i in issues)


# --------------------------------------------------------------------------
# 併自 tests/test_eval_consistency.py。
# --------------------------------------------------------------------------
def test_eval_consistency():
    issues = eval_check_all()
    assert not issues, "eval drift:\n" + "\n".join(f"  - {i}" for i in issues)


# ---------------------------------------------------------------------------
# 2026-08 workflow A–D 的文件契約:apply_patch 上限 / run_command timeout /
# 驗證分層。docstring 與 native schema description 是模型實際看到的 schema;
# 數字或宣稱跟 config / 實作漂移是無聲失敗(模型照舊文件行動,工具卻拒絕),
# 所以這批單元測試標 smoke。每條測試在函式內 import 新 checker,讓紅燈落在
# 具名 node(ImportError)而不是整個 module collection error。
# 合成 fixture 逐字沿用 w4 合併後 mcp_server.py / agent_tools.py 與文件的契約句,
# 每個 surface 只有一個副本;mutation 表每次只破壞一個 surface 的一個契約。
# ---------------------------------------------------------------------------

_CONFIG_SAMPLE = (
    "PATCH_MAX_FILES = 5              # 單次 patch 最多修改 5 個檔案\n"
    "PATCH_MAX_LINES_PER_FILE = 200   # 單一檔案最多修改 200 行\n"
    "RUN_COMMAND_TIMEOUT = 60\n"
    "RUN_COMMAND_TIMEOUT_MIN = 1\n"
    "RUN_COMMAND_TIMEOUT_MAX = 600\n"
    "OPENCODE_MCP_TIMEOUT_MIN_MS = 660_000\n"
)

_MCP_SAMPLE = '''
@_tool()
def apply_patch(diff: str, dry_run: bool = False) -> str:
    """Apply a patch. 兩種格式擇一:SEARCH/REPLACE 或 unified diff。

    `diff` 參數已是字串,**不要再包 Markdown fence**(```)。
    上限:最多 5 個檔案;udiff 單檔 added+removed ≤ 200 行;S/R 單檔 payload budget = sum(SEARCH 行數 + REPLACE 行數) ≤ 200
    (兩者不是同一種計數)。

    套用後只做同一 process、唯讀的 syntax check;它是 advisory,失敗**不回滾**,
    結果會明說「patch 已套用、未回滾」;`PATCH_AUTO_VERIFY=False` 時連 syntax check 也不做。
    lint / typecheck / test 不會自動執行——請另行呼叫 `codetrail_run_lint(fix=False)` 與
    `codetrail_run_command(...)`,它們各自需要獨立核准。

    Args:
        diff: patch 內容字串(格式 A 或 B;不要包 fence)。
        dry_run: True 時只做 preflight 並逐檔回報七個欄位:format、檔案清單（每個 parsed
                 file 一行,含失敗的）、blocks、payload budget、locations（定位行）、new_file
                 （是否新建）,全部通過才顯示 `would apply`;零副作用(不建目錄、不留 temp、
                 不跑驗證)。

    Returns:
        逐檔結果。
    """
    return ""


@_tool()
def run_command(
    cmd: str,
    timeout: Annotated[
        int, Field(strict=True, ge=RUN_COMMAND_TIMEOUT_MIN, le=RUN_COMMAND_TIMEOUT_MAX)
    ] = RUN_COMMAND_TIMEOUT,
) -> str:
    """Run a whitelisted command (server-side timeout 1..600 s).

    Args:
        timeout: 秒,整數 1..600,預設 60。這是 server 端接受的上限;MCP client
                 可能更早截止,不保證 600 秒必在 client timeout 內。
    """
    return ""
'''

_AGENT_TOOLS_SAMPLE = '''
_RUN_COMMAND_TOOL = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "執行白名單命令。build 命令只在 AI_CODE_ENABLE_BUILD_COMMANDS=1 時加入;"
            "git 不在白名單(用 git_status / git_diff)。"
            "timeout 1..600 秒(server 端上限;client 可能更早截止)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "命令"},
                "timeout": {
                    "type": "integer",
                    "minimum": RUN_COMMAND_TIMEOUT_MIN,
                    "maximum": RUN_COMMAND_TIMEOUT_MAX,
                    "default": RUN_COMMAND_TIMEOUT,
                    "description": (
                        f"超時秒數,{RUN_COMMAND_TIMEOUT_MIN}..{RUN_COMMAND_TIMEOUT_MAX},"
                        f"預設 {RUN_COMMAND_TIMEOUT}(server 端上限;client 可能更早截止)"
                    ),
                },
            },
            "required": ["command"],
        },
    },
}

_APPLY_PATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "apply_patch",
        "description": (
            "套用程式碼修改,兩種格式擇一:SEARCH/REPLACE(建議)或 unified diff。"
            "參數已是字串,不要包 Markdown fence。修改會直接寫入檔案;"
            "最多 5 個檔案、單檔 200 行(udiff 算 added+removed;S/R 算 SEARCH+REPLACE 行數)。"
            "套用後只做唯讀 syntax check(advisory,不回滾);lint / test 請另外呼叫 run_lint(fix=False) / run_command。"
            "dry_run=true 時只做 preflight,逐檔回報 format / 檔案清單 / blocks / payload budget / "
            "locations(定位行) / new_file(是否新建),全部通過才顯示 would apply。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": (
                        "SEARCH/REPLACE 格式:第一行是 repo 相對路徑,接著三個 marker 各自獨佔一行。"
                        "unified diff 格式:--- a/file / +++ b/file / @@(行號選填,靠 context 定位)。"
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "若為 true,只做 preflight 並逐檔回報 format、檔案清單、blocks、payload budget、"
                        "locations(定位行)、new_file(是否新建),全部通過才顯示 would apply;"
                        "不寫檔、零副作用（預設 false）"
                    ),
                },
            },
            "required": ["patch"],
        },
    },
}
'''

_TIMEOUT_SENTENCE = "timeout 只接受整數 1..600 秒（server 端上限；client 可能更早截止）"
_README_SAMPLE = (
    "| 修改與驗證 | 兩種 patch 格式（SEARCH/REPLACE、unified diff）：最多 5 個檔案、單檔 200 行"
    "（udiff 算 added+removed；S/R 算 payload budget = SEARCH+REPLACE 行數）；`run_command`："
    + _TIMEOUT_SENTENCE + " | `apply_patch` |\n"
)
_MCP_TOOLS_DOC_SAMPLE = (
    "| `run_command(cmd, timeout=60)` | 跑白名單命令；" + _TIMEOUT_SENTENCE + "，預設 60。 |\n"
    "`apply_patch` 套 SEARCH/REPLACE 或 unified diff。\n"
    "**上限**：最多 5 個檔案；udiff 單檔 200 行（added+removed）；"
    "S/R 單檔 payload budget = SEARCH 行數 + REPLACE 行數（同檔所有區塊合計）≤ 200——兩者不是同一種計數。\n"
    "**dry_run**：只做 preflight、零副作用，逐檔固定回報 `format`、檔案清單、`blocks`（區塊數）、"
    "`budget`（payload 用量／上限）、`locations`（定位行）、`new_file`（是否新建）；"
    "全部通過才顯示唯一的一行 `would apply`。\n"
    "syntax 是 advisory gate，失敗不回滾。apply_patch 不會自動執行 lint / typecheck / test，"
    "也不會呼叫會改檔的 formatter。\n"
)
_SECURITY_SAMPLE = (
    "`apply_patch(...)` 會寫檔、`run_lint(...)` 會格式化、`run_command(...)` 會跑命令——"
    "這是**三個不同的 ask**,每一個都要你分別核准。\n"
    "`run_command(...)` 本身還有命令白名單。" + _TIMEOUT_SENTENCE + "，不是這個範圍的整數會被拒絕。\n"
)
_TROUBLESHOOTING_SAMPLE = (
    "### `apply_patch(...)` 被拒絕\n\n"
    "非 UTF-8、mixed newline、symlink、路徑不合法——這些都是整份 patch 拒絕、零寫入。"
    "一次改超過 5 個檔案或單檔 200 行也會被拒。\n\n"
    "「驗證不完整」或「驗證未通過」**不是拒絕**:patch 已套用、未回滾。\n\n"
    "#### SEARCH/REPLACE 被拒絕\n\n- SEARCH 多處匹配一律拒絕。\n\n"
    "#### unified diff 被拒絕\n\n- context 對不上。\n\n"
    "### `run_command(...)` 被拒絕\n\n"
    "命令不在白名單。" + _TIMEOUT_SENTENCE + "，不是這個範圍的整數會在執行前被拒絕。\n"
)


def _consistent_surfaces() -> dict[str, str]:
    return {
        "mcp": _MCP_SAMPLE,
        "agent_tools": _AGENT_TOOLS_SAMPLE,
        "config": _CONFIG_SAMPLE,
        "readme": _README_SAMPLE,
        "mcp_tools_doc": _MCP_TOOLS_DOC_SAMPLE,
        "security": _SECURITY_SAMPLE,
        "troubleshooting": _TROUBLESHOOTING_SAMPLE,
    }


def _limits_issues(s: dict[str, str]) -> list[str]:
    from scripts.check_readme_consistency import _check_apply_patch_limits_contract

    issues: list[str] = []
    _check_apply_patch_limits_contract(
        s["mcp"], s["agent_tools"], s["config"], s["readme"], s["mcp_tools_doc"], s["troubleshooting"], issues
    )
    return issues


def _timeout_issues(s: dict[str, str]) -> list[str]:
    from scripts.check_readme_consistency import _check_run_command_timeout_contract

    issues: list[str] = []
    _check_run_command_timeout_contract(
        s["mcp"], s["agent_tools"], s["config"], s["readme"], s["mcp_tools_doc"], s["security"],
        s["troubleshooting"], issues,
    )
    return issues


def _verification_issues(s: dict[str, str]) -> list[str]:
    from scripts.check_readme_consistency import _check_verification_layer_claims

    issues: list[str] = []
    _check_verification_layer_claims(
        s["mcp"], s["agent_tools"], s["mcp_tools_doc"], s["security"], s["troubleshooting"], issues
    )
    return issues


def _mutate(s: dict[str, str], surface: str, before: str, after: str) -> dict[str, str]:
    assert before in s[surface], f"fixture 缺少要突變的片段: {before!r}"
    s[surface] = s[surface].replace(before, after)
    return s


def _assert_single_surface(issues: list[str], surface: str, artifact: str) -> None:
    assert issues, "突變後應該報 drift"
    if surface == "config":
        # config 是唯一真值:改它等於所有引用它的 surface 一起漂移,至少要點名指定 artifact
        assert any(issue.startswith(artifact) for issue in issues), issues
        assert len({issue.split(":")[0] for issue in issues}) >= 2, issues
    else:
        assert all(issue.startswith(artifact) for issue in issues), issues
    assert all("expected" in issue and "observed" in issue for issue in issues), issues


@pytest.mark.smoke
def test_tool_docstring_is_taken_from_the_named_function_only():
    """多行簽名要能抽到;目標沒 docstring 時不得偷後面函式的字串。"""
    from scripts.check_readme_consistency import _tool_docstring

    multiline = _tool_docstring(_MCP_SAMPLE, "run_command")
    assert multiline is not None and multiline.startswith("Run a whitelisted command")

    no_doc = '''
def first(a,
          b):
    return a

def second():
    """second has the only docstring"""
'''
    assert _tool_docstring(no_doc, "first") is None
    assert _tool_docstring(no_doc, "missing") is None


@pytest.mark.smoke
def test_native_tool_description_renders_implicit_concatenation_and_fstrings():
    from scripts.check_readme_consistency import (
        _native_tool_description,
        _native_tool_param_bound,
        _native_tool_param_description,
    )

    names = {"RUN_COMMAND_TIMEOUT_MIN": 1, "RUN_COMMAND_TIMEOUT_MAX": 600, "RUN_COMMAND_TIMEOUT": 60}
    top = _native_tool_description(_AGENT_TOOLS_SAMPLE, "_RUN_COMMAND_TOOL")
    assert top is not None and "git 不在白名單" in top and "timeout 1..600 秒" in top
    param = _native_tool_param_description(_AGENT_TOOLS_SAMPLE, "_RUN_COMMAND_TOOL", "timeout", names)
    assert param == "超時秒數,1..600,預設 60(server 端上限;client 可能更早截止)"
    assert _native_tool_param_bound(_AGENT_TOOLS_SAMPLE, "_RUN_COMMAND_TOOL", "timeout", "minimum") == "RUN_COMMAND_TIMEOUT_MIN"
    assert _native_tool_param_bound(_AGENT_TOOLS_SAMPLE, "_RUN_COMMAND_TOOL", "timeout", "default") == "RUN_COMMAND_TIMEOUT"
    assert _native_tool_description(_AGENT_TOOLS_SAMPLE, "_NO_SUCH_TOOL") is None


@pytest.mark.smoke
def test_require_sentence_rejects_prefix_negation_but_accepts_sentence_start():
    """句首檢查:「不能保證 X」含 X 的子字串但不是肯定句;「。X」才算。"""
    from scripts.check_readme_consistency import _require_sentence

    clause = "apply_patch 不會自動執行 lint / typecheck / test"
    issues: list[str] = []
    _require_sentence("前一句。" + clause + "。", clause, "x", issues)
    assert issues == []
    _require_sentence("我們不能保證 " + clause + "。", clause, "x", issues)
    assert len(issues) == 1 and issues[0].startswith("x: expected sentence")


@pytest.mark.smoke
def test_apply_patch_limits_contract_passes_when_every_surface_agrees():
    assert _limits_issues(_consistent_surfaces()) == []


@pytest.mark.smoke
@pytest.mark.parametrize(
    "surface,before,after,artifact",
    [
        # config 是唯一真值
        ("config", "PATCH_MAX_LINES_PER_FILE = 200 ", "PATCH_MAX_LINES_PER_FILE = 999 ", "mcp_server.apply_patch docstring"),
        ("config", "PATCH_MAX_FILES = 5 ", "PATCH_MAX_FILES = 6 ", "mcp_server.apply_patch docstring"),
        # MCP docstring:200→999、兩種計數互換、刪掉 S/R 計數、dry_run 反向
        ("mcp", "udiff 單檔 added+removed ≤ 200 行", "udiff 單檔 added+removed ≤ 999 行", "mcp_server.apply_patch docstring"),
        ("mcp", "udiff 單檔 added+removed ≤ 200 行;S/R 單檔 payload budget = sum(SEARCH 行數 + REPLACE 行數) ≤ 200",
         "udiff 單檔 payload budget = sum(SEARCH 行數 + REPLACE 行數) ≤ 200;S/R 單檔 added+removed ≤ 200 行", "mcp_server.apply_patch docstring"),
        ("mcp", ";S/R 單檔 payload budget = sum(SEARCH 行數 + REPLACE 行數) ≤ 200", "", "mcp_server.apply_patch docstring"),
        ("mcp", "最多 5 個檔案", "最多 4 個檔案", "mcp_server.apply_patch docstring"),
        ("mcp", "只做 preflight 並逐檔回報七個欄位:format、檔案清單", "不回報下列七個欄位:format、檔案清單", "mcp_server.apply_patch docstring dry_run"),
        ("mcp", "全部通過才顯示 `would apply`", "不保證 `would apply`", "mcp_server.apply_patch docstring dry_run"),
        ("mcp", "、new_file\n                 （是否新建）", "\n                 （是否新建）", "mcp_server.apply_patch docstring dry_run"),
        # native 頂層 description
        ("agent_tools", "最多 5 個檔案、單檔 200 行(udiff 算 added+removed;S/R 算 SEARCH+REPLACE 行數)", "最多 5 個檔案、單檔 999 行(udiff 算 added+removed;S/R 算 SEARCH+REPLACE 行數)", "_APPLY_PATCH_TOOL.description"),
        ("agent_tools", "(udiff 算 added+removed;S/R 算 SEARCH+REPLACE 行數)", "(udiff 算 SEARCH+REPLACE 行數;S/R 算 added+removed)", "_APPLY_PATCH_TOOL.description"),
        ("agent_tools", "(udiff 算 added+removed;S/R 算 SEARCH+REPLACE 行數)", "", "_APPLY_PATCH_TOOL.description"),
        ("agent_tools", "dry_run=true 時只做 preflight,逐檔回報 format / 檔案清單", "dry_run=true 時不回報下列欄位 format / 檔案清單", "_APPLY_PATCH_TOOL.description dry_run"),
        ("agent_tools", "new_file(是否新建),全部通過才顯示 would apply。", "new_file(是否新建),不保證 would apply。", "_APPLY_PATCH_TOOL.description dry_run"),
        # native patch / dry_run 參數
        ("agent_tools", "unified diff 格式:--- a/file / +++ b/file / @@", "只有 SEARCH/REPLACE", "_APPLY_PATCH_TOOL.patch.description"),
        ("agent_tools", "若為 true,只做 preflight 並逐檔回報 format、檔案清單、blocks、payload budget、", "若為 true,只回報 format、blocks、", "_APPLY_PATCH_TOOL.dry_run.description"),
        # README
        ("readme", "單檔 200 行（udiff 算 added+removed；S/R 算 payload budget = SEARCH+REPLACE 行數）", "單檔 999 行（udiff 算 added+removed；S/R 算 payload budget = SEARCH+REPLACE 行數）", "README.md"),
        ("readme", "（udiff 算 added+removed；S/R 算 payload budget = SEARCH+REPLACE 行數）", "（udiff 算 payload budget = SEARCH+REPLACE 行數；S/R 算 added+removed）", "README.md"),
        ("readme", "（udiff 算 added+removed；S/R 算 payload budget = SEARCH+REPLACE 行數）", "", "README.md"),
        ("readme", "SEARCH/REPLACE、unified diff", "unified diff", "README.md"),
        # docs/mcp-tools.md
        ("mcp_tools_doc", "udiff 單檔 200 行（added+removed）", "udiff 單檔 999 行（added+removed）", "docs/mcp-tools.md"),
        ("mcp_tools_doc", "udiff 單檔 200 行（added+removed）；S/R 單檔 payload budget = SEARCH 行數 + REPLACE 行數（同檔所有區塊合計）≤ 200",
         "udiff 單檔 payload budget = SEARCH 行數 + REPLACE 行數（同檔所有區塊合計）≤ 200；S/R 單檔 200 行（added+removed）", "docs/mcp-tools.md"),
        ("mcp_tools_doc", "；S/R 單檔 payload budget = SEARCH 行數 + REPLACE 行數（同檔所有區塊合計）≤ 200", "", "docs/mcp-tools.md"),
        ("mcp_tools_doc", "全部通過才顯示唯一的一行 `would apply`", "不保證 `would apply`", "docs/mcp-tools.md dry_run"),
        # docs/troubleshooting.md
        ("troubleshooting", "#### unified diff 被拒絕", "#### diff 被拒絕", "docs/troubleshooting.md"),
        ("troubleshooting", "整份 patch 拒絕、零寫入", "只拒絕該檔", "docs/troubleshooting.md"),
        ("troubleshooting", "一次改超過 5 個檔案或單檔 200 行也會被拒", "一次改超過 5 個檔案或單檔 999 行也會被拒", "docs/troubleshooting.md"),
    ],
)
def test_apply_patch_limits_contract_reports_the_single_drifting_surface(surface, before, after, artifact):
    s = _mutate(_consistent_surfaces(), surface, before, after)
    _assert_single_surface(_limits_issues(s), surface, artifact)


@pytest.mark.smoke
def test_run_command_timeout_contract_passes_when_every_surface_agrees():
    assert _timeout_issues(_consistent_surfaces()) == []


@pytest.mark.smoke
@pytest.mark.parametrize(
    "surface,before,after,artifact",
    [
        ("config", "RUN_COMMAND_TIMEOUT_MAX = 600", "RUN_COMMAND_TIMEOUT_MAX = 500", "mcp_server.run_command docstring"),
        ("mcp", "le=RUN_COMMAND_TIMEOUT_MAX", "le=600", "mcp_server.run_command signature"),
        ("mcp", "] = RUN_COMMAND_TIMEOUT,", "] = 60,", "mcp_server.run_command signature"),
        ("mcp", "秒,整數 1..600,預設 60。", "秒,整數 1..600,預設 30。", "mcp_server.run_command docstring"),
        ("agent_tools", '"maximum": RUN_COMMAND_TIMEOUT_MAX', '"maximum": 600', "_RUN_COMMAND_TOOL.timeout.maximum"),
        ("agent_tools", "timeout 1..600 秒(server 端上限;client 可能更早截止)。", "timeout 1..300 秒(server 端上限;client 可能更早截止)。", "_RUN_COMMAND_TOOL.description"),
        ("agent_tools", "timeout 1..600 秒(server 端上限;client 可能更早截止)。", "不保證 timeout 1..600 秒(server 端上限;client 可能更早截止)。", "_RUN_COMMAND_TOOL.description"),
        ("agent_tools", 'f"超時秒數,{RUN_COMMAND_TIMEOUT_MIN}..{RUN_COMMAND_TIMEOUT_MAX},"', 'f"超時秒數,"', "_RUN_COMMAND_TOOL.timeout.description"),
        ("readme", _TIMEOUT_SENTENCE, "timeout：server 不接受 1..600 秒", "README.md"),
        ("mcp_tools_doc", _TIMEOUT_SENTENCE, "timeout 只接受整數 1..900 秒（server 端上限；client 可能更早截止）", "docs/mcp-tools.md"),
        ("security", _TIMEOUT_SENTENCE, "server 不接受 1..600 秒", "docs/security.md"),
        ("troubleshooting", "。" + _TIMEOUT_SENTENCE, "。我們不能保證 " + _TIMEOUT_SENTENCE, "docs/troubleshooting.md"),
    ],
)
def test_run_command_timeout_contract_reports_the_single_drifting_surface(surface, before, after, artifact):
    s = _mutate(_consistent_surfaces(), surface, before, after)
    issues = _timeout_issues(s)
    _assert_single_surface(issues, surface, artifact)
    # 這條契約是 run_command 的「秒」級 server 上限,不得把既有 660000 ms client 契約混進來
    assert not any("660000" in issue for issue in issues), issues


@pytest.mark.smoke
def test_verification_layer_claims_pass_and_tolerate_correct_negations():
    assert _verification_issues(_consistent_surfaces()) == []
    # 正確的否定句(反向文案)不得被報錯
    negated = _mutate(
        _consistent_surfaces(), "mcp",
        "lint / typecheck / test 不會自動執行",
        "不會自動跑 lint / typecheck / 相關測試。lint / typecheck / test 不會自動執行",
    )
    assert _verification_issues(negated) == []


@pytest.mark.smoke
@pytest.mark.parametrize(
    "surface,before,after,artifact",
    [
        # 逐一反轉每個契約句
        ("mcp", "lint / typecheck / test 不會自動執行", "lint / typecheck / test 會自動執行", "mcp_server.apply_patch docstring"),
        ("mcp", "失敗**不回滾**", "失敗**會回滾**", "mcp_server.apply_patch docstring"),
        ("mcp", "「patch 已套用、未回滾」", "「patch 已回滾」", "mcp_server.apply_patch docstring"),
        ("mcp", "    Args:", "    套用後會自動跑 lint / typecheck / 相關測試。\n    Args:", "mcp_server.apply_patch docstring"),
        ("agent_tools", "套用後只做唯讀 syntax check(advisory,不回滾);", "✓ 所有驗證通過。", "_APPLY_PATCH_TOOL.description"),
        ("agent_tools", "\"套用後只做唯讀 syntax check(advisory,不回滾);", "\"不能保證套用後只做唯讀 syntax check(advisory,不回滾);", "_APPLY_PATCH_TOOL.description"),
        ("mcp_tools_doc", "。apply_patch 不會自動執行 lint / typecheck / test", "。不能保證 apply_patch 不會自動執行 lint / typecheck / test", "docs/mcp-tools.md"),
        ("mcp_tools_doc", "apply_patch 不會自動執行 lint / typecheck / test", "apply_patch 會自動執行 lint / typecheck / test", "docs/mcp-tools.md"),
        ("security", "這是**三個不同的 ask**", "並不是三個不同的 ask", "docs/security.md"),
        ("troubleshooting", "「驗證不完整」或「驗證未通過」**不是拒絕**", "「驗證不完整」或「驗證未通過」**是拒絕**", "docs/troubleshooting.md"),
    ],
)
def test_verification_layer_claims_report_each_reversed_contract(surface, before, after, artifact):
    s = _mutate(_consistent_surfaces(), surface, before, after)
    issues = _verification_issues(s)
    assert issues, "反轉契約句後應該報 drift"
    assert all(issue.startswith(artifact) for issue in issues), issues


# ---------------------------------------------------------------------------
# final-review 回修:前綴否定(保留原子字串、只在前面加否定)必須被報成 drift。
# 每個 case 的 before 都是 fixture 內逐字存在的片段,after 只多了否定前綴。
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_require_sentence_sees_negation_through_quotes_and_openers():
    """「結果不會明說『X』」:X 前面是引號(合法邊界),但引號前的子句含否定 → 報 issue。"""
    from scripts.check_readme_consistency import _require_sentence

    issues: list[str] = []
    _require_sentence("它是 advisory,失敗不回滾,結果會明說「patch 已套用、未回滾」;", "patch 已套用、未回滾", "x", issues)
    assert issues == []
    _require_sentence("它是 advisory,失敗不回滾,結果不會明說「patch 已套用、未回滾」;", "patch 已套用、未回滾", "x", issues)
    assert len(issues) == 1 and issues[0].startswith("x: expected sentence")
    issues = []
    _require_sentence("X 之前有句界;不必遵守「lint / test 請另外呼叫 run_lint(fix=False)」。", "lint / test 請另外呼叫 run_lint(fix=False)", "x", issues)
    assert len(issues) == 1


@pytest.mark.smoke
@pytest.mark.parametrize("text", [
    "不保證：全部通過才顯示 `would apply`",
    "不保證，全部通過才顯示 `would apply`",
    "並非——全部通過才顯示 `would apply`",
    "不必遵守：「全部通過才顯示 `would apply`」",
])
def test_require_sentence_sees_negation_through_introducer_punctuation(text):
    """冒號 / 逗號 / 破折號是「引介」標點:緊接在否定詞之後時,否定作用域延伸到被引介的子句,
    不得被當成句界切斷(final #4 的假陰性)。"""
    from scripts.check_readme_consistency import _require_sentence

    issues: list[str] = []
    _require_sentence(text, "全部通過才顯示 `would apply`", "x", issues)
    assert len(issues) == 1 and issues[0].startswith("x: expected sentence"), issues


@pytest.mark.smoke
def test_negated_predicate_before_dash_does_not_negate_the_introduced_clause():
    """「X 不會自動執行——請另行呼叫 Y」:否定只管到「自動執行」,破折號引介的 Y 是肯定句(真實 docstring 樣態)。"""
    from scripts.check_readme_consistency import _require_sentence

    issues: list[str] = []
    _require_sentence(
        "lint / typecheck / test 不會自動執行——請另行呼叫 `codetrail_run_lint(fix=False)` 與 `codetrail_run_command(...)`,它們各自需要獨立核准。",
        "請另行呼叫 `codetrail_run_lint(fix=False)`", "x", issues,
    )
    assert issues == []


_PREFIX_NEGATION_CASES = [
    # (契約, surface, before, after, artifact)
    # final #4:否定詞 + 引介標點(冒號 / 逗號 / 破折號),原子字串原樣保留
    ("limits", "mcp", ",全部通過才顯示 `would apply`", ",不保證：全部通過才顯示 `would apply`", "mcp_server.apply_patch docstring dry_run"),
    ("limits", "mcp", ",全部通過才顯示 `would apply`", ",不保證，全部通過才顯示 `would apply`", "mcp_server.apply_patch docstring dry_run"),
    ("verification", "mcp", "失敗**不回滾**", "並非：失敗**不回滾**", "mcp_server.apply_patch docstring"),
    ("verification", "mcp", "失敗**不回滾**", "並非——失敗**不回滾**", "mcp_server.apply_patch docstring"),
    ("verification", "agent_tools", ';lint / test 請另外呼叫 run_lint(fix=False) / run_command。', ';不必遵守：lint / test 請另外呼叫 run_lint(fix=False) / run_command。', "_APPLY_PATCH_TOOL.description"),
    ("limits", "mcp", "        dry_run: True 時只做 preflight", "        不保證 dry_run: True 時只做 preflight", "mcp_server.apply_patch docstring dry_run"),
    ("limits", "mcp", ",全部通過才顯示 `would apply`", ",不保證全部通過才顯示 `would apply`", "mcp_server.apply_patch docstring dry_run"),
    ("limits", "mcp", "上限:最多 5 個檔案", "上限:不保證最多 5 個檔案", "mcp_server.apply_patch docstring"),
    ("limits", "agent_tools", '"dry_run=true 時只做 preflight,', '"不保證 dry_run=true 時只做 preflight,', "_APPLY_PATCH_TOOL.description dry_run"),
    ("limits", "agent_tools", '"若為 true,只做 preflight 並逐檔回報', '"若為 true,不保證只做 preflight 並逐檔回報', "_APPLY_PATCH_TOOL.dry_run.description"),
    ("limits", "mcp_tools_doc", "零副作用，逐檔固定回報 `format`", "零副作用，不保證逐檔固定回報 `format`", "docs/mcp-tools.md dry_run"),
    ("limits", "troubleshooting", "——這些都是整份 patch 拒絕、零寫入", "——並非這些都是整份 patch 拒絕、零寫入", "docs/troubleshooting.md"),
    ("verification", "mcp", "失敗**不回滾**", "並非失敗**不回滾**", "mcp_server.apply_patch docstring"),
    ("verification", "mcp", "結果會明說「patch 已套用、未回滾」", "結果不會明說「patch 已套用、未回滾」", "mcp_server.apply_patch docstring"),
    ("verification", "mcp", "——請另行呼叫 `codetrail_run_lint(fix=False)`", "——不必遵守「請另行呼叫 `codetrail_run_lint(fix=False)`」", "mcp_server.apply_patch docstring"),
    ("verification", "agent_tools", ';lint / test 請另外呼叫 run_lint(fix=False) / run_command。', ';不必遵守「lint / test 請另外呼叫 run_lint(fix=False) / run_command」。', "_APPLY_PATCH_TOOL.description"),
    ("timeout", "mcp", "timeout: 秒,整數 1..600,預設 60。", "timeout: 不保證秒,整數 1..600,預設 60。", "mcp_server.run_command docstring"),
    ("timeout", "mcp", ";MCP client", ";並非 MCP client", "mcp_server.run_command docstring"),
    ("timeout", "agent_tools", 'f"超時秒數,{RUN_COMMAND_TIMEOUT_MIN}', 'f"不保證超時秒數,{RUN_COMMAND_TIMEOUT_MIN}', "_RUN_COMMAND_TOOL.timeout.description"),
]


@pytest.mark.smoke
@pytest.mark.parametrize("contract,surface,before,after,artifact", _PREFIX_NEGATION_CASES)
def test_prefix_negations_keeping_the_substring_are_reported(contract, surface, before, after, artifact):
    s = _mutate(_consistent_surfaces(), surface, before, after)
    issues = {"limits": _limits_issues, "timeout": _timeout_issues, "verification": _verification_issues}[contract](s)
    assert issues, f"只加否定前綴({after!r})仍應報 drift"
    assert all(issue.startswith(artifact) for issue in issues), issues
