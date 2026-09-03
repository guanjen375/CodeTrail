"""repo 一致性:README 的工具清單/旗標、eval/ 對 config 及原始碼的漂移,以及維護
腳本的 `--help` / 錯誤路徑 smoke。

合併自 tests/test_readme_consistency.py 與 tests/test_eval_consistency.py(2026-08-20)。
兩者都是把 scripts/check_*_consistency.py 暴露成 pytest,失敗時直接看到 drift list。
併入 tests/test_script_help.py(2026-09-02;它本身是 2026-08-20 從 test_cli.py 拆出):
RAG.py / scripts/*.py / eval/*.py 的 `--help` 與錯誤路徑要能 cheap return、不吐
Traceback、不載模型不連 server——這些是使用者照 README 打第一個命令就會碰到的面。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_eval_consistency import check_all as eval_check_all
from scripts.check_readme_consistency import (
    _check_code_model_placeholder_contract,
    _check_default_aux_models_documented,
    _check_doctor_commands_have_explicit_model,
    _check_forbidden_main_model_tokens,
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




def test_code_model_placeholder_contract_passes_with_llamacpp_setup():
    """新版範本:llama-server / GGUF 都提到、model 有 custom-provider prefix。"""
    readme = '''
本專案使用 llama-server 跑 GGUF 模型。

```json
export AICODE_MODEL=<CODE_MODEL>
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
    assert any("AICODE_MODEL" in issue for issue in issues)


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
    "MCP_CALL_TIMEOUT_SECONDS = 660\n"
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
    lint / typecheck / test 不會自動執行——請另行呼叫 `run_lint(fix=False)` 與
    `run_command(...)`,它們各自需要獨立核准。

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
        int, Field(strict=True, ge=RUN_COMMAND_TIMEOUT_MIN, le=RUN_COMMAND_TIMEOUT_MAX,
                   description='Server timeout in seconds; strict integer 1..600; client may stop earlier.')
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
        "lint / typecheck / test 不會自動執行——請另行呼叫 `run_lint(fix=False)` 與 `run_command(...)`,它們各自需要獨立核准。",
        "請另行呼叫 `run_lint(fix=False)`", "x", issues,
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
    ("verification", "mcp", "——請另行呼叫 `run_lint(fix=False)`", "——不必遵守「請另行呼叫 `run_lint(fix=False)`」", "mcp_server.apply_patch docstring"),
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


# ---------------------------------------------------------------------------
# Tool-routing integration:these assertions pin the user-facing surfaces to the
# converged T1–T4 interfaces without weakening any historical consistency gate.
# ---------------------------------------------------------------------------

def test_public_tool_order_and_the_base_rules_budget():
    """工具順序與基底規則的 1,600 字元硬上限。

    以前基底規則住在 `docs/opencode-agents-template.md` 的 fenced block(由安裝程式
    抽出來寫進使用者的全域 AGENTS.md);現在它是 `client_prompt.BASE_RULES`,由客戶端
    直接組進 system prompt。上限的理由沒變:2026-08-24 的真實 regression 裡,一份
    4,869 字元的全域規則讓模型只反覆說「現在呼叫工具」並以 stop 結束。
    """
    import client_prompt
    from mcp_contract import PUBLIC_TOOL_ORDER

    mcp_doc = (REPO_ROOT / "docs/mcp-tools.md").read_text(encoding="utf-8")
    order_sentence = "、".join(f"`{name}`" for name in PUBLIC_TOOL_ORDER)
    assert order_sentence in mcp_doc.replace("\n", "")

    assert len(client_prompt.BASE_RULES) <= client_prompt.BASE_RULES_MAX_CHARS
    # 基底規則不得變成第二份工具目錄:工具名以本輪 tool schema 為唯一真值。
    listed = [name for name in PUBLIC_TOOL_ORDER if name in client_prompt.BASE_RULES]
    assert len(listed) <= 3, listed


@pytest.mark.smoke
def test_result_budget_and_status_lane_docs_match_the_runtime_contract():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    basic = (REPO_ROOT / "docs/basic-usage.md").read_text(encoding="utf-8")
    troubleshooting = (REPO_ROOT / "docs/troubleshooting.md").read_text(
        encoding="utf-8"
    )
    mcp_doc = (REPO_ROOT / "docs/mcp-tools.md").read_text(encoding="utf-8")
    joined = "\n".join((readme, basic, troubleshooting, mcp_doc))

    assert "status: ok|partial|error" in readme
    assert "status: ok|partial|error" in basic
    assert "status: ok|partial|error" in mcp_doc
    assert "n_ctx" in joined and "12%" in joined and "context_risk" in joined
    assert "call-time `config.N_CTX`" in (REPO_ROOT / "README_DEV.md").read_text(
        encoding="utf-8"
    )
    assert not re.search(
        r"max_chars[^\n]{0,100}(?:預設|default)[^\n]{0,20}(?:12000|12,000)",
        "\n".join((readme, basic, troubleshooting)),
        re.IGNORECASE,
    )




@pytest.mark.smoke
def test_routing_eval_docs_preserve_frozen_baseline_and_manual_support_status():
    import json

    developer = (REPO_ROOT / "README_DEV.md").read_text(encoding="utf-8")
    matrix = json.loads(
        (REPO_ROOT / "eval/fixtures/tool_routing/support_matrix.json").read_text(
            encoding="utf-8"
        )
    )

    assert "--catalog-only" in developer
    assert "--arm baseline --frozen-contract" in developer
    assert "--catalog-source in-process" in developer
    assert "manual_status_change_required=true" in developer
    assert "不能把 `measured` 改寫成 `supported`" in developer
    assert "`--arm` 只選擇／記錄 arm id" in developer
    assert "`--arm` 不做 variant composition" in developer
    assert "凍結 config/artifact digest" in developer
    assert "selected_combo_v3_stop_on_evidence" in developer
    assert "supported_arm=null" in developer
    assert "runtime `todowrite` 維持 `allow`" in developer
    # OpenCode 時代的六臂量測原樣保留(它是真的量過的數字),另外多一個
    # client 時代的臂。兩個世代**不得混用**:歷史 row 的身分永遠對不上現行
    # 客戶端,所以它不能當現在的基準,而新 row 在重量之前是 fail-closed。
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", arm["contract_digest"])
        for arm in matrix["arms"].values()
    )
    eras = {name: arm["era"] for name, arm in matrix["arms"].items()}
    assert len([name for name, era in eras.items() if era == "opencode"]) == 6
    assert eras["client_baseline"] == "client"

    rows = {row["id"]: row for row in matrix["rows"]}
    historical = [row for row in rows.values() if row["era"] == "opencode"]
    client_rows = [row for row in rows.values() if row["era"] == "client"]
    assert len(historical) == 1 and len(client_rows) == 1
    assert client_rows[0]["status"] == "unsupported"     # 還沒重量 = 不得放行
    assert client_rows[0]["arms"] == ["client_baseline"]
    assert client_rows[0]["decision"]["supported_arm"] is None
    assert "routing" not in client_rows[0]["baseline"]

    measured_arms = {name for name, era in eras.items() if era == "opencode"} - {"baseline"}
    for row in historical:
        assert row["status"] == "measured"
        assert row["compatibility"]["client_version"] == "1.18.21"
        assert row["baseline"]["routing"]["tool_needed"]["recall"] == 0.384615
        assert row["decision"] == {
            "supported_arm": None,
            "build_prompt_default": False,
            "todowrite_permission": "allow",
            "reason": "No fully evaluated arm passed every support gate",
        }
        assert set(row["measurements"]) == measured_arms
        assert all(
            measurement["evaluation_scope"] == "full"
            and measurement["support_gate_passed"] is False
            for measurement in row["measurements"].values()
        )
        assert row["exploratory_candidates"]["selected_combo_v3_stop_on_evidence"][
            "support_gate_passed"
        ] is False


# ── 原 test_script_help.py:維護腳本的 --help / 錯誤路徑 smoke(能 cheap return、不吐 Traceback) ──

def test_rag_help_exits_zero():
    """`python RAG.py --help` 必須能 cheap return 0。"""
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "RAG.py"), "--help"],
        capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0, f"exit={r.returncode}\n{r.stderr}"
    assert "用法" in r.stdout or "usage" in r.stdout.lower()
    assert "Traceback" not in r.stderr


def test_rag_help_lists_binary_and_image_types():
    """`python RAG.py --help` 要列出 binary/ELF/圖片副檔名,避免使用者誤以為只支援 PDF。"""
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "RAG.py"), "--help"],
        capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0
    out = r.stdout
    assert ".bin" in out, "RAG.py --help should mention .bin support"
    assert ".elf" in out, "RAG.py --help should mention .elf support"
    assert ".png" in out, "RAG.py --help should mention .png support"


def test_rag_rejects_unknown_extension_with_supported_list(tmp_path):
    """副檔名不支援時,error 訊息要列出支援清單(包含 binary/ELF),不能只說 pdf/md/txt。"""
    bad_file = tmp_path / "garbage.xyz"
    bad_file.write_text("hi")
    kb_file = tmp_path / "kb.json"
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "RAG.py"), str(bad_file), str(kb_file)],
        capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "不支援" in out, out
    # error 訊息要提到三類副檔名
    assert ".pdf" in out, out
    assert ".bin" in out, out
    assert ".elf" in out, out
    assert "Traceback" not in r.stderr


def test_index_stats_help_exits_zero():
    """`python scripts/index_stats.py --help` 必須 cheap return 0(唯讀、離線)。"""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "index_stats.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--show-paths" in proc.stdout


def test_kb_ab_compare_help_exits_zero():
    """`python scripts/kb_ab_compare.py --help` 必須 cheap return 0(離線、不載模型)。"""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "kb_ab_compare.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--questions" in proc.stdout
    assert "Traceback" not in proc.stderr


def test_kb_ab_compare_rejects_two_kbs_in_one_directory(tmp_path: Path):
    """同目錄兩份 KB 會互相覆蓋 knowledge_emb.npz，必須擋下（不是靜默比錯）。"""
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    for path in (first, second):
        path.write_text(json.dumps({"metadata": {}, "chunks": []}), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "kb_ab_compare.py"),
            str(first),
            str(second),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert proc.returncode != 0
    assert "同一個目錄" in proc.stderr
    assert "Traceback" not in proc.stderr


@pytest.mark.parametrize(
    "script",
    ["run_eval.py", "run_retrieval_eval.py"],
    ids=["run_eval", "run_retrieval_eval"],
)
def test_eval_script_help_exits_zero(script):
    """`python eval/<script> --help` 必須能 cheap return 0。

    run_eval:不需要 llama-server。
    run_retrieval_eval:離線 retrieval harness 的 help 不得載入模型或連 server。
    """
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "eval" / script), "--help"],
        capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0, f"exit={r.returncode}\n{r.stderr}"
    assert "usage" in r.stdout.lower() or "用法" in r.stdout
    assert "Traceback" not in r.stderr


# ── 客戶端時代的一致性檢查(取代原 OpenCode 範本三條)──


def test_the_mcp_timeout_contract_is_read_from_config():
    from scripts.check_readme_consistency import _check_mcp_timeout_contract

    issues: list[str] = []
    _check_mcp_timeout_contract("每次呼叫固定 660 秒", "MCP_CALL_TIMEOUT_SECONDS = 660\n", issues)
    assert issues == []

    issues = []
    _check_mcp_timeout_contract("每次呼叫固定 60 秒", "MCP_CALL_TIMEOUT_SECONDS = 660\n", issues)
    assert issues and "660" in issues[0]


def test_the_permission_contract_comes_from_client_policy():
    """README 少列一個需要核准的工具,使用者就會以為那個工具不會問。"""
    from scripts.check_readme_consistency import _check_permission_contract

    policy = 'ASK_TOOLS: frozenset[str] = frozenset(\n    {\n        "apply_patch",\n        "run_lint",\n    }\n)'
    issues: list[str] = []
    _check_permission_contract("`apply_patch` 與 `run_lint` 都會問", policy, issues)
    assert issues == []

    issues = []
    _check_permission_contract("只有 `apply_patch` 會問", policy, issues)
    assert issues and "run_lint" in issues[0]


def test_the_readme_no_longer_teaches_installing_opencode():
    from scripts.check_readme_consistency import _check_client_entry_documented

    issues: list[str] = []
    _check_client_entry_documented("用 aicode 啟動", issues)
    assert issues == []

    issues = []
    _check_client_entry_documented("npm install -g opencode-ai@latest 然後 aicode", issues)
    assert issues and "opencode-ai" in issues[0]


@pytest.mark.smoke
def test_user_docs_must_not_teach_removed_flags_or_files():
    """去 OpenCode 化之後不存在的東西(`--compaction-mode native`、舊 ownership 狀態檔、
    已刪的 scripts/opencode_*.py、已併掉的 compaction_status.py)文件不得再教。
    checker 要抓得到,而且現在的文件要乾淨。"""
    from scripts import check_readme_consistency as checker

    for stale in (
        "先跑 ./set_config.sh --compaction-mode native 再回報",
        "模式記在 ~/.config/codetrail/compaction.json 裡",
        "python3 scripts/opencode_contract_check.py --fix",
        "python3 scripts/compaction_status.py",
    ):
        issues: list[str] = []
        checker._check_no_stale_client_docs(stale, issues)
        assert issues, stale
    issues = []
    checker._check_no_stale_client_docs(checker._documentation_text(), issues)
    assert issues == []


# ── 總審第 2 輪回修:JS stub 的靜態契約;現行 CLI 不得再講 OpenCode ──

@pytest.mark.smoke
def test_the_opencode_plugin_stubs_are_inert():
    """使用者的 opencode.json 可能還註冊著這兩個路徑(升級到跑 set_config 之間):
    它們只能是一個 export、只掛 `event` hook、只在 session.created toast 一次,
    不得 import / 讀檔 / 打網路 / 動工具結果。"""
    for name in ("codetrail-compaction.js", "codetrail-notify.js"):
        source = (REPO_ROOT / "opencode_plugins" / name).read_text(encoding="utf-8")
        _assert_inert_stub(source, name)


def _strip_js_comments(text: str) -> str:
    """只拿掉**真正的**註解:逐字元走,字串 literal(`"` / `'` / 反引號,含跳脫)裡的
    `/*`、`*/`、`//` 都不是註解。以前用 regex 直接砍 `/*...*/`,兩個字串之間的實際程式碼
    會被錯當成註解拿掉,重建出一行完全合法的 hook。"""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in ("\"", "'", "`"):
            quote = ch
            j = i + 1
            while j < n and text[j] != quote:
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j            # 保留換行,行結構不變
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _assert_inert_stub(source: str, name: str) -> None:
    """inert stub 的完整契約(靜態):

    - 恰好一個 export,export 的函式主體**只能**由三樣東西組成:`let told = false;`、
      `const tell = async () => {...};` 的定義、`return { event: ... };`——其餘任何敘述
      (例如載入時就 `await tell()`)都是初始化副作用,不管字串計數多漂亮都不放行;
    - 回傳物件只有 `event` 這一個 hook,且只在 `session.created` 呼叫 `tell()`;
    - toast 恰好一次:唯一的副作用、有 told 閂、包 try/catch(fail-open);
    - 不 import / 不讀檔 / 不打網路 / 不動工具結果 / 不排程。
    """
    assert source.count("export ") == 1, name
    # 整個檔案去掉真註解之後,export 函式**以外**不得有任何東西:頂層 `throw` /
    # `process.exit()` / `console.log()` 這種模組載入時就執行的敘述,以前完全不在 gate 的
    # 視線內(regex 只擷取 export 本體)。
    stripped = _strip_js_comments(source)
    body_match = re.search(r"export const \w+ = async \(\{ client \}\) => \{\n(.*)\n\};", stripped, re.S)
    assert body_match, (name, "export shape")
    outside = stripped[:body_match.start()] + stripped[body_match.end():]
    assert not outside.strip(), (name, "top-level code outside the export", outside.strip()[:80])
    body = body_match.group(1)
    returned = re.search(r"\n  return \{\n(.*?)\n  \};", body, re.S)
    assert returned, name
    hooks = re.findall(r"^\s{4}([\w.\"']+)\s*:", returned.group(1), re.M)
    assert hooks == ["event"], (name, hooks)
    assert "session.created" in returned.group(1), name
    assert returned.group(1).count("tell()") == 1, (name, "tell() must be called once, inside event")
    tell_def = re.search(r"\n  const tell = async \(\) => \{\n(.*?)\n  \};", body, re.S)
    assert tell_def, (name, "tell definition")
    remainder = body.replace(returned.group(0), "").replace(tell_def.group(0), "")
    leftover = [line for line in remainder.splitlines() if line.strip()]
    assert leftover == ["  let told = false;"], (name, "initialisation side effect", leftover)
    # event hook 本體只能是「session.created 時 tell() 一次」,沒有別的敘述。
    hook_lines = [line.strip() for line in returned.group(1).splitlines() if line.strip()]
    assert hook_lines == [
        "event: async ({ event }) => {",
        'if (event?.type === "session.created") await tell();',
        "},",
    ], (name, "event hook body", hook_lines)
    # tell() 本體白名單:閂 → try { 一次 showToast } catch {}(空)。把 showToast 的
    # 參數物件抽掉之後,剩下的敘述必須逐行等於這個序列——多任何一行(例如
    # `await client.session.create({})`)都是 stub 以外的副作用。
    tell_body = tell_def.group(1)
    call = re.search(r"await client\.tui\.showToast\((\{.*?\})\);", tell_body, re.S)
    assert call, (name, "showToast call")
    toast_arg = re.sub(r"\s+", "", call.group(1))
    assert re.fullmatch(r'\{body:\{message:("[^"]*"\+?)+,?variant:"warning",?\},?\}', toast_arg), (
        name, "toast payload must be message strings + variant only", toast_arg
    )
    tell_lines = [line.strip() for line in tell_body.replace(call.group(0), "await client.tui.showToast(...);").splitlines() if line.strip()]
    assert tell_lines == [
        "if (told) return;",
        "told = true;",
        "try {",
        "await client.tui.showToast(...);",
        "} catch {",
        "}",
    ], (name, "tell body", tell_lines)
    assert source.count("showToast") == 1, name
    assert "if (told) return;" in source and "told = true;" in source, name
    assert source.count("try {") == 1 and "catch" in source, name
    for forbidden in ("import ", "require(", "fetch(", "readFile", "writeFile",
                      "tool.execute", "chat.message", "chat.params", "experimental",
                      "child_process", "process.env", "WebSocket", "XMLHttpRequest",
                      "eval(", "Function(", "Deno.", "Bun.", "globalThis", "setTimeout",
                      "setInterval", "http.", "https.", "net.", "fs.", "dns."):
        assert forbidden not in source, (name, forbidden)


@pytest.mark.smoke
def test_the_stub_gate_rejects_initialisation_side_effects():
    """gate 本身要能抓到「載入時就 toast」與「多掛一個 hook」這兩種回歸——以前的
    字串計數斷言對 `await tell();` 塞在 return 之前這種寫法照樣綠。"""
    source = (REPO_ROOT / "opencode_plugins" / "codetrail-compaction.js").read_text(encoding="utf-8")
    _assert_inert_stub(source, "baseline")
    eager = source.replace("\n  return {\n", "\n  await tell();\n  return {\n", 1)
    with pytest.raises(AssertionError):
        _assert_inert_stub(eager, "eager-toast")
    extra_hook = source.replace("\n  return {\n", "\n  return {\n    config: async () => {},\n", 1)
    with pytest.raises(AssertionError):
        _assert_inert_stub(extra_hook, "extra-hook")
    twice = source.replace("if (event?.type === \"session.created\") await tell();",
                           "await tell(); if (event?.type === \"session.created\") await tell();", 1)
    with pytest.raises(AssertionError):
        _assert_inert_stub(twice, "tell-twice")
    # tell() 本體裡多一個副作用(第 5 輪的 mutation):外層結構完全不變,以前照樣綠。
    inside_tell = source.replace("    told = true;\n", "    told = true;\n    await client.session.create({});\n", 1)
    assert inside_tell != source
    with pytest.raises(AssertionError):
        _assert_inert_stub(inside_tell, "side-effect-inside-tell")
    in_catch = source.replace("    } catch {\n", "    } catch {\n      await client.session.create({});\n", 1)
    assert in_catch != source
    with pytest.raises(AssertionError):
        _assert_inert_stub(in_catch, "side-effect-in-catch")
    payload = source.replace('variant: "warning",', 'variant: "warning", onClick: async () => client.session.create({}),', 1)
    assert payload != source
    with pytest.raises(AssertionError):
        _assert_inert_stub(payload, "side-effect-in-toast-payload")
    # 第 6 輪的 mutation:字串 literal 裡的 /* 與 */ 不是註解,但不懂詞法的 regex 會把兩個
    # 字串之間的真程式碼當註解砍掉,重建出一行合法的 hook。
    fake_comment = source.replace(
        'if (event?.type === "session.created") await tell();',
        'if (event?.type === "session.created/*" || client.session.create({}) || "*/") await tell();', 1,
    )
    assert fake_comment != source
    with pytest.raises(AssertionError):
        _assert_inert_stub(fake_comment, "fake-block-comment-in-strings")
    fake_line_comment = source.replace(
        'if (event?.type === "session.created") await tell();',
        'if (event?.type === "session.created//") await tell(); client.session.create({});', 1,
    )
    assert fake_line_comment != source
    with pytest.raises(AssertionError):
        _assert_inert_stub(fake_line_comment, "fake-line-comment-in-string")
    # 第 7 輪的 mutation:export **之前**的頂層敘述(載入時就執行)——以前 gate 完全看不到。
    for label, top in (
        ("top-level-throw", 'throw new Error("plugin disabled");\n'),
        ("top-level-process-exit", "process.exit(23);\n"),
        ("top-level-console", 'console.log("loaded");\n'),
        ("top-level-after-export", None),
    ):
        if top is None:
            mutated = source.rstrip("\n") + '\nconsole.log("after");\n'
        else:
            mutated = source.replace("export const ", top + "export const ", 1)
        assert mutated != source, label
        with pytest.raises(AssertionError):
            _assert_inert_stub(mutated, label)


@pytest.mark.smoke
def test_the_js_comment_stripper_is_lexically_aware():
    """`/*`、`*/`、`//` 出現在字串 literal 裡時不是註解;真註解(含跨行 block)要拿掉。"""
    src = 'a = "x/*y"; /* real\ncomment */ b = \'p//q\'; // tail\nc = `t/*u*/`;\n'
    assert _strip_js_comments(src) == 'a = "x/*y";  b = \'p//q\'; \nc = `t/*u*/`;\n'


@pytest.mark.smoke
def test_current_cli_help_never_mentions_opencode():
    """去 OpenCode 化之後,現行 CLI 的 --help / 說明不得再把 session、provider 講成 OpenCode 的。"""
    for script in ("scripts/session_eval.py", "scripts/tool_call_canary.py",
                   "scripts/eval_tool_routing.py", "scripts/set_config.py", "codetrail_chat.py"):
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / script), "--help"],
            capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        assert proc.returncode == 0, (script, proc.stderr[-400:])
        text = (proc.stdout + proc.stderr).lower()
        assert "opencode" not in text, script
        # 字面沒有 opencode 不夠:「provider/model」「compaction plugin」這種 OpenCode
        # 時代的語意也不得留在現行 help 裡。
        for stale in ("provider/model", "plugin", "provider prefix"):
            assert stale not in text, (script, stale)
    from scripts import set_config as sc

    assert "native" not in (sc.__doc__ or "")
    assert "plugin" not in (sc.__doc__ or "")
