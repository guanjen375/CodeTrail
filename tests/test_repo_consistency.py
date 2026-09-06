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
    """新版範本:llama-server / GGUF 都提到、主模型的落點寫在 deployment.json。"""
    readme = '''
本專案使用 llama-server 跑 GGUF 模型。

```json
deployment.json 的 main.model 填 <CODE_MODEL>
```
'''
    docs = readme  # docs_text 包含 readme 本身
    issues: list[str] = []
    _check_code_model_placeholder_contract(readme, docs, issues)
    assert issues == []


def test_code_model_placeholder_contract_reports_missing_bits():
    """空文件應該被報缺 placeholder / llama-server / GGUF / 主模型落點。"""
    issues: list[str] = []
    _check_code_model_placeholder_contract("", "", issues)
    assert any("<CODE_MODEL>" in issue for issue in issues)
    assert any("llama-server" in issue for issue in issues)
    assert any("GGUF" in issue for issue in issues)
    assert any("main.model" in issue for issue in issues)


@pytest.mark.smoke
def test_doctor_no_longer_needs_a_model_prefix():
    """`python3 scripts/doctor.py` 就是完整命令。

    2026-09-04:`_check_doctor_commands_have_explicit_model`(要求文件把 doctor
    命令寫成 `AICODE_MODEL=<CODE_MODEL> python3 scripts/doctor.py`)刪除。
    行為為什麼該變:doctor 現在從 deployment.json 讀主模型,那個前綴既不會生效
    也不再需要 —— 留著檢查等於強迫文件教一個沒有作用的東西。
    """
    from scripts import check_readme_consistency as checker

    assert not hasattr(checker, "_check_doctor_commands_have_explicit_model")
    doctor_source = (REPO_ROOT / "scripts" / "doctor.py").read_text(encoding="utf-8")
    assert "AICODE_MODEL=" not in doctor_source


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
            "執行白名單命令。build 命令只在 client.json 的 build_commands 打開時加入;"
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
    """已經不存在的東西(`--compaction-mode native`、舊 ownership 狀態檔、已刪的
    scripts/opencode_*.py 與根目錄的遷移工具、已併掉的 compaction_status.py、整組移除的
    網頁前端、只能用檔案 / argv 交接的殼層設定形狀)文件不得再教。checker 要抓得到,
    而且現在的文件要乾淨。

    2026-09-06:整包合併掃描換成 `_check_stale_docs_per_file()`。升級段**必須**點名舊的
    ownership 狀態檔與當年那支還原工具(否則使用者不知道要處理什麼),而那個例外是
    **逐檔**的 —— 合併成一大坨之後只能整組放行或整組擋下,等於把 troubleshooting 的
    例外送給所有文件。合成字串仍然不帶 `source=`,所以連例外過的那兩條也照樣要被抓到。
    """
    from scripts import check_readme_consistency as checker

    for stale in (
        "先跑 ./set_config.sh --compaction-mode native 再回報",
        "模式記在 ~/.config/codetrail/compaction.json 裡",
        "python3 scripts/opencode_contract_check.py --fix",
        "python3 scripts/compaction_status.py",
        # 本版不再附帶根目錄那支遷移工具(只有 troubleshooting 的 a1682d5 升級段能教)。
        "python3 opencode_migrate.py --check",
        # 設定只來自 deployment.json / client.json 與 argv:這幾個殼層形狀照做既不會
        # 生效也不會報錯。
        "export LLAMA_BIN=~/llama.cpp/build/bin/llama-server",
        "export MODELS_DIR=~/models",
        "AICODE_TEST_JOBS=1 python3 scripts/run_tests.py",
        "Environment=AICODE_MODEL=<CODE_MODEL>",
        # 網頁前端已整組移除;文件不得再教使用者去跑它。
        "```bash\naicode web --hostname 0.0.0.0\n```",
        "```bash\naicode attach http://127.0.0.1:4096\n```",
        "```bash\naicode_web stop\n```",
        "先 export AICODE_WEB_PASSWORD=<強密碼> 再啟動",
        "用終端客戶端(直接 `aicode`)或 web 介面",
        "web 模式下沙箱綁在啟動 backend 的目錄",
    ):
        issues: list[str] = []
        checker._check_no_stale_client_docs(stale, issues)
        assert issues, stale
    issues = []
    checker._check_stale_docs_per_file(issues)
    assert issues == []


# ── 現行 CLI 不得再講舊世代前端 ──

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


# ═══════════════════════════════════════════════════════════════════════════
# 三條靜態 gate:OpenCode 殘留、os.environ 讀取、文件教的東西
# ═══════════════════════════════════════════════════════════════════════════
# 為什麼是**靜態**:這三件事的失敗都是無聲的。
#   * 多一處 opencode 依賴 = 部署又需要 Node,而測試照樣綠。
#   * 多一處 `os.environ.get("AICODE_...")` = 殼層裡殘留的變數又能靜默蓋過
#     `deployment.json`,而那正是兩份安裝混用時「以為在跑 A、實際在跑 B」的機制。
#   * 文件多教一行 `export AICODE_*` = 使用者照做,然後得到一個不會生效、也不會
#     報錯的設定。
# allowlist 逐條寫原因;要加新的一條就得在這裡說明白。


#: 不是 source 的目錄:VCS / 快取 / venv(README 建議在 repo 內建 `.venv`,掃進
#: site-packages 就是誤報 + 慢)/ 本機狀態。含 `pyvenv.cfg` 的目錄一律視為 venv。
_SKIP_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".eggs",
    "node_modules", ".codetrail", ".venv", "venv", "site-packages",
}
#: 跳過的**檔案**(不是目錄)只有 VCS 控制檔:linked worktree 的 `.git` 是普通檔。
_SKIP_FILES = {".git"}


def _handoff_markdown(rel: Path) -> bool:
    """`docs/workflows/<任務>/*.md`:施工交接紀錄,不是使用者文件。

    這兩件事都由它決定,而且**只有**這兩件事:OpenCode 內容 gate 與文件 gate 的
    `.md` 來源選擇。交接檔逐字引用被移除的變數名、舊工具的命令與 `opencode` 這個字
    (「哪些東西被刪掉了」正是它要記的內容),當成使用者文件掃就是永遠紅燈。

    刻意**不**動 `_walk_files` / `_repo_sources()` / `_iter_text_files()`:把整個目錄
    從走訪剪掉的話,同一個目錄底下的 `.py` / `.sh` / `.json` / `.toml`(真的會被執行
    或被載入的東西)也會一起消失在所有 gate 的視線外 —— 那是把可執行檔藏進豁免裡。
    豁免只給 `.md` 的**內容**。
    """
    parts = rel.parts
    return (
        rel.suffix == ".md"
        # docs / workflows / <任務> / <檔名>:至少 4 層。3 層就是 `docs/workflows/p.md`
        # —— 少了任務目錄那一層,那不是交接紀錄。
        and len(parts) >= 4
        and parts[0] == "docs"
        and parts[1] == "workflows"
    )


def _walk_files(root: Path, *, visited: list[str] | None = None):
    """`os.walk` + **真剪枝**:VCS / 快取 / venv(含 `pyvenv.cfg` 的子目錄)不走進去,
    不是走完再逐檔跳過(大型 `.venv` 那是幾萬個檔)。`visited` 給自測看走了哪些目錄。"""
    import os as _os

    for dirpath, dirnames, filenames in _os.walk(root):
        if visited is not None:
            visited.append(str(Path(dirpath).relative_to(root)))
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIP_DIRS and not (Path(dirpath) / d / "pyvenv.cfg").exists()
        )
        for name in sorted(filenames):
            # linked worktree 的 `.git` 是**檔案**(`gitdir: …/.git/worktrees/x`):VCS 控制檔,
            # 不是 source。**只**跳這一個名字:叫 `venv` / `node_modules` 的無副檔名 script 是 source。
            if name in _SKIP_FILES:
                continue
            yield Path(dirpath) / name


def _repo_sources(suffixes=(".py",), *, include_tests: bool = False, names=()):
    """repo 內要掃的檔案。跳過 .git / __pycache__ / venv / 本機快取產物。

    `names` 是**沒有副檔名也要掃**的檔名(`aicode` wrapper);列在裡面的 dot 檔也照掃。
    """
    for path in _walk_files(REPO_ROOT):
        if not path.is_file():
            continue
        if path.suffix not in suffixes and path.name not in names:
            continue
        rel = path.relative_to(REPO_ROOT)
        if rel.name.startswith(".") and rel.name not in names:
            continue
        if not include_tests and rel.parts[0] == "tests":
            continue
        yield path


def _iter_text_files(root: Path, *, ignored: frozenset[Path] | set[Path] = frozenset(), _visited: list[str] | None = None):
    """`root` 底下**所有**文字檔(檔名、副檔名不限;二進位跳過;tests/ 與 venv / 快取不掃)。

    OpenCode gate 用它:「只掃某幾種副檔名」就是 `Dockerfile.dev` / `.env` / `ci.yaml`
    這種正常檔名一個接一個漏。`ignored` 是 git 認定被忽略的檔(本機快取產物)。
    """
    for path in _walk_files(root, visited=_visited):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root)
        if rel.parts[0] == "tests" or rel in ignored:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:8192]:
            continue
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        yield path


def _git_ignored() -> set[Path]:
    """git 認定被忽略的檔(相對 repo root):本機快取 / 狀態,不是 source。"""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
            capture_output=True, check=False, timeout=30,
            env=__import__("process_env").child_env(),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {Path(item.decode("utf-8", "replace")) for item in out.split(b"\0") if item}


def _code_only(path: Path, *, keep_docstrings: bool = False) -> list[tuple[int, str]]:
    """去掉註解(預設也去 docstring)的 (行號, 內容)。

    這幾條 gate 要擋的是**程式碼裡的依賴**,不是「說明為什麼已經不依賴它」的
    註解 —— 後者正是這次施工留下最多、也最該留下的東西。`keep_docstrings=True`
    給模型看得到的文字那條 gate 用:MCP 工具的 docstring **就是**工具描述。
    """
    return _code_only_source(path.read_text(encoding="utf-8"), path.suffix, keep_docstrings=keep_docstrings)


def _code_only_source(source: str, suffix: str, *, keep_docstrings: bool = False) -> list[tuple[int, str]]:
    """`_code_only` 的純函式版(gate 的自測用合成內容餵它)。"""
    skip: set[int] = set()
    if suffix == ".py" and not keep_docstrings:
        import ast as _ast

        try:
            tree = _ast.parse(source)
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in _ast.walk(tree):
                if not isinstance(
                    node, (_ast.Module, _ast.ClassDef, _ast.FunctionDef, _ast.AsyncFunctionDef)
                ):
                    continue
                doc = node.body[0] if node.body else None
                if (
                    isinstance(doc, _ast.Expr)
                    and isinstance(doc.value, _ast.Constant)
                    and isinstance(doc.value.value, str)
                ):
                    skip.update(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))
    out = []
    for lineno, line in enumerate(source.splitlines(), 1):
        if lineno in skip:
            continue
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        out.append((lineno, line))
    return out


#: `opencode` 這個字允許出現的地方,以及原因。
#: 2026-09-06:遷移工具與兩個 inert stub 已整組刪除,`scripts/doctor.py` 的唯讀偵測
#: 與 `scripts/set_config.py` 的升級提示也不在了 —— 那幾個鍵一起拿掉,留著就是
#: 「指到不存在的檔的豁免」,下一個人在同名檔裡寫什麼都不會被擋。
_OPENCODE_ALLOWLIST = {
    # 子行程環境的**剝除**清單:`OPENCODE_*` 出現在這裡正是為了把它拿掉
    # (核准後的 run_command 會繼承那份環境,裡面可能有升級機器殘留的機密)。
    "process_env.py": "剝除 OPENCODE_* 的清單與 fail-loud",
    # eval 的凍結 baseline 用舊鍵名記錄有效字元數;讀取端要相容,寫入端只寫新鍵。
    "scripts/mcp_catalog.py": "凍結資料的舊欄位名(LEGACY_EFFECTIVE_CHARS_KEY)",
    "scripts/eval_tool_routing.py": "凍結資料的舊欄位名 + era 標記",
    "scripts/check_readme_consistency.py": "反向檢查:文件不得再教安裝 opencode-ai / 跑遷移工具",
    # eval 的 era 標記(純資料檔);eval/ 的 .py 走一般檢查。
    "eval/fixtures/tool_routing/support_matrix.json": "eval 的 era 標記",
    # 文件:升級段(troubleshooting)與歷史量測說明。
    "README.md": "升級段",
    "README_DEV.md": "歷史量測 row 的說明",
    "AGENTS.md": "§2 / §3 對舊世代前端的說明",
    "docs/troubleshooting.md": "a1682d5 升級段",
    "docs/security.md": "升級段",
    "docs/basic-usage.md": "歷史量測資料的說明",
}

#: allowlist 裡的 **code 檔不是整檔豁免**:這些形狀 = runtime 依賴,在哪個檔都算違規。
#: 分兩組。**可執行**的依賴(匯入 opencode 模組、碰 OpenCode 的設定 / plugin 路徑、
#: 呼叫 opencode 命令)—— 任何逐檔形狀例外都蓋不過:`import opencode_migrate` 出現在
#: 反向檢查器裡一樣是 import,不是 pattern。**套件 / 教學**的形狀(`npm` / `npx` /
#: `opencode-ai`)才是反向檢查器的字串會長的樣子,由 `_OPENCODE_SHAPE_EXEMPTIONS`
#: 逐檔放行。這一代**沒有任何整檔豁免**:遷移工具與兩個 stub 都刪掉了。
_OPENCODE_EXECUTABLE_SHAPES = re.compile(
    r"(?:^|[^\w.])(?:import|from)\s+opencode"
    r"|opencode\.json|opencode_plugins|\.config/opencode"
    r"|which\(\s*['\"]opencode|[\[(,]\s*['\"]opencode['\" ]",
    re.IGNORECASE,
)
_OPENCODE_PACKAGING_SHAPES = re.compile(r"\bnpm\b|\bnpx\b|opencode-ai", re.IGNORECASE)
#: 兩組合起來:allowlist 裡的**資料檔**用(那裡沒有形狀例外,兩組都是依賴)。
_OPENCODE_DEPENDENCY_SHAPES = re.compile(
    f"{_OPENCODE_EXECUTABLE_SHAPES.pattern}|{_OPENCODE_PACKAGING_SHAPES.pattern}",
    re.IGNORECASE,
)
#: 逐檔的例外形狀,附原因。`.py` 現在**連註解與 docstring 都掃**(見
#: `_opencode_offenders`),所以例外要涵蓋歷史說明用得到的字。例外只對
#: `_OPENCODE_PACKAGING_SHAPES` 有效 —— 可執行的形狀先判、不看例外。
_OPENCODE_SHAPE_EXEMPTIONS = {
    # 子行程環境的剝除清單與它的說明:`OPENCODE_` 這個前綴就是被剝掉的那個。
    "process_env.py": re.compile(r"OPENCODE_"),
    # 凍結 baseline 的舊欄位名與 era 標記:資料是量過的,讀取端要認得舊拼法。
    "scripts/mcp_catalog.py": re.compile(r'opencode_effective_chars|"era"'),
    "scripts/eval_tool_routing.py": re.compile(r'opencode_effective_chars|"era"'),
    # 它的字串**就是**用來抓「文件教裝 opencode-ai / 教跑遷移工具」的 pattern。
    "scripts/check_readme_consistency.py": re.compile(r"opencode-ai|npm|opencode_migrate|OPENCODE_"),
}
#: `.md` 不得再教使用者跑那支已經不附帶的遷移工具。唯一合法的用法是升級段 ——
#: 判準是**同一 logical line 或所在段落標題**指名 `a1682d5`(那一段講的正是
#: 「把舊安裝路徑固定回 a1682d5 再用當時的工具」),不是「這份文件叫什麼名字」。
_OPENCODE_MIGRATE_COMMAND = re.compile(r"^\s*(?:[$>]\s*)?python3\s+opencode_migrate\.py")
_OPENCODE_UPGRADE_ANCHOR = "a1682d5"


def _opencode_offenders(rel: str, text: str) -> list[str]:
    """一個檔案裡不該出現的 OpenCode 依賴(gate 的純函式半邊,自測餵合成內容)。

    2026-09-06 起掃**全文**(含註解與 docstring):去 OpenCode 化之後,留在註解裡的
    「以前是這樣接 OpenCode 的」會一路漂到沒有人看得懂,而 gate 完全看不到它。
    allowlist 的檔靠逐檔形狀例外放行歷史說明,其餘一律改字成「舊世代前端」。
    """
    suffix = Path(rel).suffix
    listed = rel in _OPENCODE_ALLOWLIST
    offenders: list[str] = []
    if listed and suffix == ".md":
        # allowlist 裡的**文件**不是整檔豁免 —— 但判準不是「這一行有沒有升級的字眼」
        # (那只會產生噪音),而是「有沒有教使用者去用它」:`OPENCODE_*` 變數、
        # `opencode` 命令列形狀、已刪的遷移命令。散文裡提到 OpenCode(升級段、
        # 兩世代並存、eval 的 era 標記)本來就該提到它的名字。
        teach = re.compile(r"(?m)\bOPENCODE_[A-Z_]+\b|^\s*(?:[$>]\s*)?opencode\s|npm\s+install\s+-g\s+opencode")
        for lineno, line, _in_fence, heading in _doc_logical_lines(text):
            if _OPENCODE_MIGRATE_COMMAND.search(line) and not (
                _OPENCODE_UPGRADE_ANCHOR in line or _OPENCODE_UPGRADE_ANCHOR in heading
            ):
                offenders.append(
                    f"{rel}:{lineno}(教使用者跑本版不附帶的遷移工具;只有指名 "
                    f"{_OPENCODE_UPGRADE_ANCHOR} 的升級段能教): {line.strip()[:110]}"
                )
                continue
            if any(word in line for word in ("已刪除", "已移除", "不存在", "不再", "以前", "不會", "不得")):
                continue
            if teach.search(line):
                offenders.append(f"{rel}:{lineno}(教使用者用 OpenCode): {line.strip()[:110]}")
        return offenders
    if listed and suffix not in (".py", ".js", ".sh"):
        # 資料 / 需求檔不是整檔豁免:era 標記(`"era": "opencode"`)可以,
        # 命令列形狀、`opencode-ai`、`npm` 一樣是依賴。
        for lineno, line in enumerate(text.splitlines(), 1):
            if "opencode" in line.lower() and _OPENCODE_DEPENDENCY_SHAPES.search(line):
                offenders.append(f"{rel}:{lineno}(allowlist 資料檔裡的依賴形狀): {line.strip()[:110]}")
        return offenders
    exempt = _OPENCODE_SHAPE_EXEMPTIONS.get(rel)
    for lineno, line in enumerate(text.splitlines(), 1):
        if "opencode" not in line.lower():
            continue  # `npm test` 之類是使用者專案的命令,與 OpenCode 無關
        if not listed:
            offenders.append(f"{rel}:{lineno}: {line.strip()[:120]}")
            continue
        if _OPENCODE_EXECUTABLE_SHAPES.search(line):
            # 可執行的依賴先判,而且不看形狀例外:例外的字(`opencode_migrate`)出現在
            # `import` / `from … import` 裡就是 runtime 依賴,不是反向檢查的 pattern。
            offenders.append(f"{rel}:{lineno}(allowlist 檔裡的 runtime 依賴形狀): {line.strip()[:110]}")
            continue
        if exempt is not None and exempt.search(line):
            continue
        if _OPENCODE_PACKAGING_SHAPES.search(line):
            offenders.append(f"{rel}:{lineno}(allowlist 檔裡的 runtime 依賴形狀): {line.strip()[:110]}")
    return offenders


def _opencode_gate_sources():
    """OpenCode gate 掃 repo 裡**所有**文字檔(`pyproject.toml`、`Dockerfile.dev`、`.env`、
    `ci.yaml`、`aicode` wrapper、`.gitignore` …):列舉不靠副檔名 / 檔名清單。

    唯一的例外是 `docs/workflows/**/*.md` 的**內容**(見 `_handoff_markdown`):同一個
    目錄底下的 `.py` / `.sh` / `.json` / `.toml` 照掃。
    """
    for path in _iter_text_files(REPO_ROOT, ignored=_git_ignored()):
        if _handoff_markdown(path.relative_to(REPO_ROOT)):
            continue
        yield path


@pytest.mark.smoke
def test_the_removed_frontend_only_survives_in_the_strip_list_history_data_and_upgrade_docs():
    """`opencode` 只准出現在四個地方:子行程環境的剝除清單、eval 凍結資料的舊欄位名 /
    era 標記、反向檢查器的 pattern,以及文件的升級段與歷史量測說明。

    多一處 runtime 依賴 = 部署又需要 Node / opencode-ai,而所有測試照樣綠。
    """
    offenders: list[str] = []
    for path in _opencode_gate_sources():
        rel = str(path.relative_to(REPO_ROOT))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        offenders += _opencode_offenders(rel, text)
    assert not offenders, (
        "opencode 只准出現在 allowlist 的檔案裡(剝除清單 / 凍結資料的欄位名 / 反向檢查器 / "
        "文件升級段)。新增一處就得在 _OPENCODE_ALLOWLIST 說明原因:\n" + "\n".join(offenders)
    )


#: `os.environ` / `getenv` 允許出現的檔案,以及原因。
#: 2026-09-06 起**沒有「啟動核心的 env 契約」這一層**:GPU / llama-server 路徑 /
#: tmux session 名 / 逾時全部搬進 `deployment.json` 與 argv,`~/start.sh` 不 export
#: 也不 unset。剩下的每一筆都只讀「檔案在哪 / 行程介面」(HOME / XDG_* / PATH /
#: 寫入 PYTHONIOENCODING / 寫入 PYTEST_*)。
#: 這份清單同時是**豁免**:指到已經零讀取的檔就是把那個檔永久放行 —— 所以同一輪
#: 把 20 筆 stale 條目一起拿掉(含刪除的遷移工具與三個改吃 argv 的啟停腳本)。
_ENVIRON_ALLOWLIST = {
    # loader:只有 `_home()` 一個出口讀 HOME / USERPROFILE(設定檔在哪)。
    "deployment_profile.py": "HOME / USERPROFILE(檔案在哪)",
    "scripts/launch_servers.py": "HOME / XDG_STATE_HOME(log 目錄)",
    # 開發者工具:每個 shard 的子行程環境 + 寫入 PYTEST_*(並行度改吃 `--jobs`)。
    "scripts/run_tests.py": "PYTEST_* 寫入 + 子行程環境",
    # 只讀 HOME / XDG_* / PATH / PYTHONIOENCODING 這類「檔案在哪 / 行程介面」。
    "config.py": "HOME(_file_env)",
    "client_config.py": "HOME",
    "client_preflight.py": "HOME(profile_env)",
    "client_prompt.py": "HOME",
    "client_status.py": "HOME",
    "client_store.py": "HOME / XDG_STATE_HOME",
    "process_env.py": "子行程環境的剝除(唯一出口)",
    "index_scope.py": "HOME",
    "lessons.py": "HOME",
    "mcp_lease.py": "XDG_STATE_HOME",
    "mcp_server.py": "PYTHONIOENCODING / HOME",
    "codetrail_chat.py": "HOME(root_safety)",
    "scripts/doctor.py": "HOME / PATH",
    "scripts/index_stats.py": "HOME",
    "scripts/tool_call_canary.py": "HOME / XDG_CACHE_HOME / 子行程環境",
    "scripts/required_model_servers_check.py": "HOME",
    "scripts/session_eval.py": "HOME / 子行程環境",
    "client_compaction.py": "HOME / XDG_STATE_HOME(ledger 位置)",
    "client_engine.py": "HOME(system prompt 的來源檔)",
    "external_import.py": "HOME",
    "model_resolution.py": "HOME(profile 檔案在哪)",
    "elf_analysis.py": "regex literal(`getenv` 是被比對的符號名,不是讀取)",
    "eval/run_eval.py": "開發者 eval 工具(寫入 PYTHONIOENCODING)",
}

#: 這幾個前綴是 CodeTrail 自己的設定名。客戶端 / MCP 側**一個都不准讀**。
_CODETRAIL_ENV_PREFIXES = ("AICODE_", "AI_CODE_", "CODETRAIL_", "OPENCODE_")


#: 仍然自己 spawn 的四個檔,各有一個不能經 `process_env.run` 的理由:
#: `deployment_profile.py` 是唯一 `os.execvpe` llama-server 的地方(環境由
#: `process_env.llama_server_env()` 算,見下面那條靜態檢查);`scripts/run_tests.py`
#: 要自己 `Popen` 每個 shard 並刻意帶完整環境 + `PYTEST_*`;兩個 eval 工具是開發者
#: 命令(`llama-server --version` / eval 子行程)。啟停腳本與 `set_config.py` 已經
#: 全部改走 `process_env.run`,所以它們**不在**這裡 —— 留著就是永久放行。
_SPAWN_CORE = {
    "deployment_profile.py", "scripts/run_tests.py",
    "eval/run_eval.py", "eval/record_semantic_vectors.py",
}
#: 「複製整份行程環境」的寫法;唯一合法的一份在 `process_env.child_env()`。
#: (`environ = os.environ if env is None else env` 這種**讀 HOME 用的別名**不算:
#: 它不會交給子行程;交給子行程的是 `env= / environ= / environment=` 這些關鍵字形狀。)
#: (`dict(os.environ if env is None else env)` 這種**讀 HOME 用的副本**不算:它只拿來
#: 找檔案位置,交給子行程時 `env=self.env` 這種 Attribute 會被 spawn 檢查擋下。)
_ENV_COPY = re.compile(
    r"os\.environ\.(?:copy|items)\(\)|dict\(\s*os\.environ\s*[,)]|\{\s*\*\*os\.environ"
    r"|copy\(\s*os\.environ\b|os\.environ\s*\||\|\s*os\.environ\b|os\.environb|\breturn\s+os\.environ\b(?!\s*[.\[])"
    r"|(?:[(,]|^)\s*(?:env|environ|environment)\s*=\s*os\.environ\s*(?:[,)]|$)"
)
#: 這些名字在 process_env 與啟動核心以外的 code 行**一律不得出現**。spawn 只有一個出口
#: (`process_env.run/popen/check_output`,環境由它自己用 child_env() 算),所以 gate 不必推導
#: env 從哪來 —— alias、cast、作用域、順序、helper 全部無關,因為沒有東西可以 alias。
_SPAWN_API = re.compile(
    r"\bsubprocess\b|\bpexpect\b|\bpty\b"
    r"|\bos\.(?:exec\w*|spawn\w*|posix_spawn\w*|system|popen|fork\w*)\b"
    r"|\basyncio\.create_subprocess_\w+"
    r"|(?:__import__|import_module)\(\s*['\"](?:subprocess|pexpect|pty)\b"
    r"|\bprocess_env\._\w+"  # 唯一出口的私有成員(_subprocess …)不是出口
)
_OS_SPAWN_NAMES = re.compile(r"^(?:exec\w*|spawn\w*|posix_spawn\w*|system|popen|fork\w*)$")
_SPAWN_MODULES = ("subprocess", "pty", "pexpect")


def _spawn_offenders(rel: str, source: str) -> list[str]:
    """子行程環境沒有經 `child_env()` 的地方(gate 的純函式半邊)。

    判準只有三條、都不用推導:(1) 複製整份 `os.environ`;(2) spawn API(`subprocess` /
    `pty` / `pexpect` / `os.exec*|spawn*|system|popen|fork*` / `asyncio.create_subprocess_*`,
    含 `from os import system`、`getattr(os, "system")`、`__import__("subprocess")`)在
    process_env 與啟動核心以外**出現**;(3) `process_env.run/popen/check_output` 被交了 `env=`
    (它們自己算環境,明確要加的鍵走 `overrides=`)。
    """
    import ast as _ast

    if rel in _SPAWN_CORE or rel == "process_env.py":
        return []
    out: list[str] = []
    for lineno, line in _code_only_source(source, ".py"):
        if _ENV_COPY.search(line):
            out.append(f"{rel}:{lineno}(複製整份 os.environ): {line.strip()[:110]}")
        if _SPAWN_API.search(line):
            out.append(f"{rel}:{lineno}(直接用 spawn API;只能走 process_env.run/popen/check_output): {line.strip()[:100]}")
    try:
        tree = _ast.parse(source)
    except SyntaxError:
        return out
    # `import os as operating_system` / `import asyncio as aio`:alias 之後的 `.system` / `.create_subprocess_*`
    # 一樣是 spawn;`from process_env import run` 之後的裸 `run(..., env=)` 才算出口被交 env。
    module_aliases: dict[str, set[str]] = {}  # 同名多次 import 取**聯集**(最後寫入者勝出會漏)
    exit_names: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for a in node.names:
                if a.name in ("os", "asyncio") and a.asname:
                    module_aliases.setdefault(a.asname, set()).add(a.name)
        elif isinstance(node, _ast.ImportFrom) and node.module == "process_env":
            exit_names.update(a.asname or a.name for a in node.names if a.name in ("run", "popen", "check_output", "Popen"))
            for a in node.names:
                if a.name.startswith("_") or a.name == "*":
                    out.append(f"{rel}:{node.lineno}(from process_env import {a.name}:私有成員不是出口)")

    def aliased_spawn(base: str, attr: str) -> str | None:
        for module in module_aliases.get(base, ()):
            if module == "os" and _OS_SPAWN_NAMES.match(attr):
                return f"os.{attr}"
            if module == "asyncio" and attr.startswith("create_subprocess"):
                return f"asyncio.{attr}"
        return None

    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute) and isinstance(node.value, _ast.Name):
            target = aliased_spawn(node.value.id, node.attr)
            if target:
                out.append(f"{rel}:{node.lineno}({node.value.id}.{node.attr} 是 {target}:spawn API 只能走 process_env)")
        if isinstance(node, _ast.ImportFrom) and node.module in ("os", "asyncio", *_SPAWN_MODULES):
            for a in node.names:
                if (
                    node.module in _SPAWN_MODULES
                    or a.name == "*"
                    or (node.module == "os" and _OS_SPAWN_NAMES.match(a.name))
                    or (node.module == "asyncio" and a.name.startswith("create_subprocess"))
                ):
                    out.append(f"{rel}:{node.lineno}(from {node.module} import {a.name}:spawn API 只能走 process_env)")
        elif isinstance(node, _ast.Call):
            f = node.func
            if (
                isinstance(f, _ast.Name) and f.id == "getattr" and len(node.args) >= 2
                and isinstance(node.args[0], _ast.Name)
                and isinstance(node.args[1], _ast.Constant) and isinstance(node.args[1].value, str)
            ):
                bases = module_aliases.get(node.args[0].id, {node.args[0].id})
                attr = node.args[1].value
                if any(
                    base in _SPAWN_MODULES or (base == "os" and _OS_SPAWN_NAMES.match(attr))
                    or (base == "asyncio" and attr.startswith("create_subprocess"))
                    for base in bases
                ):
                    out.append(f"{rel}:{node.lineno}(getattr({node.args[0].id}, {attr!r}):spawn API 只能走 process_env)")
            is_exit = (
                isinstance(f, _ast.Attribute) and isinstance(f.value, _ast.Name) and f.value.id == "process_env"
                and f.attr in ("run", "popen", "check_output", "Popen")
            ) or (isinstance(f, _ast.Name) and f.id in exit_names)
            if is_exit and any(k.arg == "env" for k in node.keywords):
                out.append(f"{rel}:{node.lineno}(process_env 的 spawn 出口不接受 env=:環境由它自己算,要加的鍵走 overrides=)")
    return out


@pytest.mark.smoke
def test_no_module_reads_codetrail_settings_from_the_environment():
    """客戶端 / MCP 側不得從環境變數取任何 CodeTrail 設定。

    真實觸發:兩份安裝共用一台機器,另一份的 `~/start.sh` export 了
    `AICODE_MODEL` / `AICODE_LLAMA_BASE_URL` / `AICODE_N_CTX`。讀進來就是
    「使用者以為在跑 A、實際在跑 B」,而且完全無聲。

    這條掃的是**字面出現**(不是 allowlist 檔案的豁免):`_ENVIRON_ALLOWLIST` 裡的
    每一筆都只讀「檔案在哪 / 行程介面」,其餘一個都不准。2026-09-06 起連啟動核心
    都沒有例外 —— GPU / 二進位路徑 / session 名 / 逾時改吃 `deployment.json` 與 argv,
    所以下面第二段沒有 `core` 豁免:**任何**檔案讀 `AICODE_*` / `AI_CODE_*` /
    `CODETRAIL_*` / `OPENCODE_*` 都是 offender。
    """
    offenders: list[str] = []
    for path in _repo_sources():
        rel = str(path.relative_to(REPO_ROOT))
        if rel in _ENVIRON_ALLOWLIST:
            continue
        code = "\n".join(line for _, line in _code_only(path))
        if "os.environ" in code or "getenv" in code:
            offenders.append(f"{rel}: 讀了 os.environ / getenv(不在 allowlist 裡)")
    assert not offenders, (
        "新增 os.environ 讀取要嘛加進 _ENVIRON_ALLOWLIST 並寫明只讀什麼,"
        "要嘛改走 client.json 的鍵或 config.py 常數:\n" + "\n".join(offenders)
    )

    # allowlist 內的檔案也不准讀 CodeTrail 自己的設定名 —— **一個豁免都沒有**。
    # 只認**真的讀行程環境**的形狀(`os.environ` / `os.getenv`)。
    # `environ.get(...)` 作用在呼叫端傳進來的 dict 上時,那是 argv / kwargs 交接
    # (`load_effective_profile(environ=…)` 的 HOME 查表),不是從殼層取設定。
    reads = re.compile(
        r"os\.(?:environ(?:\.get)?\(|getenv\()\s*[\"']("
        + "|".join(_CODETRAIL_ENV_PREFIXES)
        + r")"
    )
    leaks: list[str] = []
    for path in _repo_sources():
        rel = str(path.relative_to(REPO_ROOT))
        for lineno, line in _code_only(path):
            if reads.search(line):
                leaks.append(f"{rel}:{lineno}: {line.strip()[:120]}")
    assert not leaks, (
        "沒有任何檔案可以從殼層取 CodeTrail 設定;設定只來自 config.py 常數、"
        "deployment.json / models.json 與 client.json,行程之間用 argv:\n" + "\n".join(leaks)
    )

    # **間接那一半:整份環境被複製給子行程 / 子行程隱式繼承。** 直接的
    # `.get("AICODE_…")` 只是明顯的那一種;`env = os.environ.copy()` 之後把 env
    # 遞下去,下游隨便一個 `.get()` 就把殼層的值撈回來了 —— 而且靜態上看不出來。
    # 判準在 `_spawn_offenders`。
    copies: list[str] = []
    for path in _repo_sources():
        rel = str(path.relative_to(REPO_ROOT))
        copies += _spawn_offenders(rel, path.read_text(encoding="utf-8"))
    assert not copies, (
        "spawn 子行程前必須用 process_env.child_env() 剝掉 CodeTrail 的設定變數,"
        "不能直接複製整份 os.environ、也不能隱式繼承:\n" + "\n".join(copies)
    )
    # 同一件事的第三種寫法:`os.environ["AICODE_…"]` 的 index access。
    indexed = re.compile(r"os\.environ\[\s*[\"'](" + "|".join(_CODETRAIL_ENV_PREFIXES) + r")")
    hits: list[str] = []
    for path in _repo_sources():
        rel = str(path.relative_to(REPO_ROOT))
        for lineno, line in _code_only(path):
            if indexed.search(line):
                hits.append(f"{rel}:{lineno}: {line.strip()[:120]}")
    assert not hits, "index access 也是讀環境:\n" + "\n".join(hits)


@pytest.mark.smoke
def test_the_only_llama_server_exec_hands_over_the_stripped_environment():
    """`deployment_profile.py` 的 `exec` 是 llama-server **唯一**真正被執行的地方,
    所以也是最終環境的唯一決定點:它的第三個參數必須是
    `process_env.llama_server_env()`。

    為什麼要靜態釘住:tmux pane 的環境 = tmux server 的全域環境 + session 環境,
    launcher 的行程環境管不到已經在跑的 daemon。這一行改成 `os.environ` 或
    `process_env.child_env()`,殼層裡殘留的 `CUDA_VISIBLE_DEVICES` / `LLAMA_ARG_*`
    就會蓋掉 `deployment.json` 指定的卡與參數 —— 而且完全無聲:server 起得來、
    status 也綠,只是跑在別張卡上。`_SPAWN_CORE` 放行了這個檔的 spawn API,
    這一條就是那個放行的對價。
    """
    import ast as _ast

    source = (REPO_ROOT / "deployment_profile.py").read_text(encoding="utf-8")
    calls = [
        node for node in _ast.walk(_ast.parse(source))
        if isinstance(node, _ast.Call)
        and isinstance(node.func, _ast.Attribute)
        and node.func.attr.startswith("exec")
        and isinstance(node.func.value, _ast.Name)
        and node.func.value.id == "os"
    ]
    assert len(calls) == 1, f"deployment_profile.py 的 os.exec* 應該只有一個: {len(calls)}"
    call = calls[0]
    assert call.func.attr == "execvpe", _ast.unparse(call.func)
    assert len(call.args) == 3, "execvpe(path, argv, env) 的三個參數缺一不可"
    assert _ast.unparse(call.args[2]) == "process_env.llama_server_env()", (
        f"pane 裡 exec llama-server 的環境必須是 process_env.llama_server_env(),"
        f"實際是 {_ast.unparse(call.args[2])}"
    )


#: 文件不得再教的**介面**:每一條都是「照做之後不會生效、也不會報錯」。
#: (變數名不在這裡列:任何 CodeTrail 變數名都由 `_doc_offenders` 一律抓。)
_FORBIDDEN_DOC_PATTERNS = (
    (r"\baicode\s+web\b", "網頁前端已整組移除"),
    (r"\baicode\s+attach\b", "attach 已移除"),
    (r"\baicode_web\b", "背景 launcher 已移除"),
)

#: 文件裡合法的名字:路徑 placeholder、概念名、ingest 通知的協定標記。
#: 它們不是環境變數,沒有人會拿去 export。**這是唯一的白名單** —— 2026-09-06 起
#: 沒有「啟動核心的變數」這一類:GPU / 二進位路徑 / session 名 / 逾時 / 並行度全部
#: 改吃 `deployment.json` 與 argv,所以文件裡任何 `AICODE_*` / `AI_CODE_*` /
#: `CODETRAIL_*` / `OPENCODE_*` 名字(下面這六個概念名除外)都是在教一個沒有作用
#: 也不會報錯的東西。逐段落 / 逐命令的例外一併移除:段落白名單就是
#: 「docs/setup.md 整段教 `Environment=AICODE_*` 卻沒有人發現」的那條路。
_DOC_ALLOWED_TOKENS = (
    "CODETRAIL_REPO", "AICODE_ROOT",
    "CODETRAIL_ACTION_REQUIRED", "CODETRAIL_INGEST_SUMMARY", "CODETRAIL_INGEST_FAILED", "CODETRAIL_ZERO_WRITE",
)

_DOC_REMOVAL_WORDS = ("不存在", "已移除", "已刪除", "已經沒有", "以前", "刪除、無替代")


#: `TOKEN=value … <命令>`(含 `env` 前綴、多個指派、`\\` 續行合併後)。
_DOC_ENV_PREFIXED = re.compile(
    r"(?:^|[\s`$>])(?:env\s+)?((?:(?:AICODE|AI_CODE|CODETRAIL|OPENCODE)_\w+[+:?]?=\S*\s+)+)(\S[^`]*)"
)
#: 「當環境變數用」的形狀:`$TOKEN` / `${TOKEN}` 讀它,`TOKEN=` / `+=` / `:=` / `?=` 指派它。
#: `env | grep X`、`set | grep X`:列出整份環境再撈名字,一樣是在讀它。
_DOC_ENV_LISTING = re.compile(
    # 列出整份環境之後,管線裡任何地方出現 grep 類(`| sudo -n grep`、`| tee f | grep`)
    r"\b(?:env|set|printenv|export|declare)\b[^`]*\|[^`]*\b(?:grep|egrep|fgrep|rg|ag|awk|sed)\b"
    r"|\b(?:env|set|printenv|export|declare)\b[^;&|`]*>\s*\S+"
)
_DOC_VAR_USE = re.compile(
    r"(\$\{?[#!]?|(?:printenv|declare|typeset|export|readonly|local)\b(?:\s+-\S+)*\s+)?"
    r"\b((?:AICODE|AI_CODE|CODETRAIL|OPENCODE)_\w+)\b(\s*[+:?.]?=(?!=))?"
)
_DOC_EXPORT = re.compile(r"\bexport\s+((?:AICODE|AI_CODE|CODETRAIL|OPENCODE)_\w+)")


def _doc_logical_lines(text: str):
    """(起始行號, 內容, 是否在 code fence 內, 目前標題):`\\` 續行合併成一行看 ——
    `AICODE_MODEL=bogus \\` 換行再接 `aicode`,逐行看就漏了。標題只認 fence 外的 `#`。"""
    in_fence = False
    heading = ""
    pending: list[str] = []
    start = 0
    for lineno, line in enumerate(text.splitlines(), 1):
        bare = line.strip()
        if not pending:
            if bare.startswith(("```", "~~~")):
                in_fence = not in_fence
                continue
            if not in_fence and bare.startswith("#"):
                heading = bare
        if bare.endswith("\\"):
            if not pending:
                start = lineno
            pending.append(re.sub(r"\\+\s*$", "", line).rstrip())
            continue
        if pending:
            pending.append(line)
            yield start, " ".join(pending), in_fence, heading
            pending = []
            continue
        yield lineno, line, in_fence, heading
    if pending:
        yield start, " ".join(pending), in_fence, heading


def _doc_offenders(rel: str, text: str) -> list[str]:
    """一份文件裡「教了已經不存在的東西」的行(gate 的純函式半邊)。

    判準:任何 `AICODE_*` / `AI_CODE_*` / `CODETRAIL_*` / `OPENCODE_*` 名字都算(不必有
    `=`:「把 `AICODE_N_CTX` 設大」一樣是在教),除了:協定標記 / placeholder 的**裸提及**
    (拿它 `export` / 當環境前綴就不是裸提及);「它已經不存在」的句子;`unset` 舊變數。

    2026-09-06:段落白名單(`_DOC_CORE_SECTIONS`)、變數白名單(`_DOC_CORE_VARIABLES`)
    與「環境前綴接啟動核心命令算契約」(`_LAUNCHER_SCRIPTS`)三個例外整組刪除 ——
    設定不再有環境變數這一層,連 `~/start.sh` 都不 export,所以文件裡沒有一個位置
    是「教它仍然正確」的。
    """
    offenders: list[str] = []
    allowed_tokens = re.compile(r"\b(?:" + "|".join(map(re.escape, _DOC_ALLOWED_TOKENS)) + r")\b")
    any_var = re.compile(r"\b(?:AICODE|AI_CODE|CODETRAIL|OPENCODE)_\w+")

    for lineno, line, _in_fence, _heading in _doc_logical_lines(text):
        bare = line.strip()
        if any(word in line for word in _DOC_REMOVAL_WORDS) or re.match(r"^\s*(?:[$>]\s*)?unset\s", line):
            continue
        m = _DOC_ENV_PREFIXED.search(line)
        if m:
            assigned = re.findall(r"((?:AICODE|AI_CODE|CODETRAIL|OPENCODE)_\w+)[+:?]?=", m.group(1))
            offenders.append(
                f"{rel}:{lineno}: {bare[:110]} — 環境前綴 {'/'.join(assigned)} 掛在命令前面"
                "(設定只來自 deployment.json / client.json 與 argv,沒有任何命令會讀它)"
            )
            continue
        ex = _DOC_EXPORT.search(line)
        if ex:
            offenders.append(f"{rel}:{lineno}: {bare[:110]} — export {ex.group(1)}(設定不經環境交接)")
            continue
        listing = _DOC_ENV_LISTING.search(line) is not None
        used_as_var = [
            um.group(2)
            for um in _DOC_VAR_USE.finditer(line)
            if (um.group(1) or um.group(3) or listing)
        ]
        if used_as_var:
            offenders.append(
                f"{rel}:{lineno}: {bare[:110]} — {used_as_var[0]} 被當環境變數讀 / 指派"
                "(協定標記 / 概念名不是環境變數;沒有程式從殼層取設定)"
            )
            continue
        candidate = allowed_tokens.sub("", line)
        hit = any_var.search(candidate)
        if hit:
            offenders.append(f"{rel}:{lineno}: {bare[:110]} — 文件提到 {hit.group(0)}(沒有程式從殼層取設定)")
            continue
        for pattern, why in _FORBIDDEN_DOC_PATTERNS:
            if re.search(pattern, candidate):
                offenders.append(f"{rel}:{lineno}: {bare[:110]} — {why}")
                break
    return offenders


@pytest.mark.smoke
def test_user_docs_never_teach_a_removed_environment_knob_or_interface():
    """文件不得教 `export AICODE_*` / `aicode web` / `attach` / `aicode_web`。

    這些照做之後既不會生效也不會報錯 —— 使用者只會得到一個「設了但沒用」的
    設定,或一個不存在的子指令。

    `docs/workflows/**/*.md` 是施工交接紀錄不是使用者文件(見 `_handoff_markdown`):
    它逐字引用被移除的變數名,當使用者文件掃就是永遠紅燈。同目錄的可執行檔照掃。
    """
    offenders: list[str] = []
    docs = [
        path for path in _repo_sources(suffixes=(".md",))
        if not _handoff_markdown(path.relative_to(REPO_ROOT))
    ]
    for path in docs:
        offenders += _doc_offenders(str(path.relative_to(REPO_ROOT)), path.read_text(encoding="utf-8"))
    assert not offenders, "文件教了已經不存在的東西:\n" + "\n".join(offenders)


@pytest.mark.smoke
def test_model_facing_text_never_teaches_a_removed_environment_knob():
    """**模型看得到的**工具描述與錯誤訊息也不得教已刪的環境變數。

    這比文件更糟:使用者會照著模型的建議去 export 一個沒有作用的變數,而模型
    每一輪都會再建議一次。工具描述進 system prompt,錯誤訊息進工具結果。
    """
    offenders: list[str] = []
    # 掃**字串常數**(含 docstring:MCP 工具的 docstring 就是工具描述),不只 `VAR=`
    # 的形狀 —— 「請設定 AICODE_X」「見 AI_CODE_Y」一樣是在教一個不存在的東西。
    pattern = re.compile(r"\b((?:AICODE|AI_CODE|CODETRAIL|OPENCODE)_[A-Z0-9_]+)\b")
    core_only = {
        # 文件一致性檢查器:它的字串**就是**用來抓這些名字的 pattern。這一代只剩它
        # 一個 —— 啟動核心那幾個腳本的設定改吃 `deployment.json` 與 argv,字串常數
        # 裡不該再出現任何已刪的變數名。
        "scripts/check_readme_consistency.py",
    }
    #: 字串裡允許出現的名字**只剩概念名與協定標記**:sandbox root 的概念名仍叫
    #: `AICODE_ROOT`,ingest 通知的四個標記是協定的一部分。它們不是環境變數,
    #: 不會有人拿去 export。啟動核心的那幾個名字全部刪掉了,留著就等於允許
    #: 模型繼續建議一個沒有作用的變數。
    allowed = re.compile(
        r"AICODE_ROOT\b|CODETRAIL_ACTION_REQUIRED\b|CODETRAIL_INGEST_SUMMARY\b|CODETRAIL_INGEST_FAILED\b"
        r"|CODETRAIL_ZERO_WRITE\b"
    )
    import ast as _ast
    for path in _repo_sources():
        rel = str(path.relative_to(REPO_ROOT))
        if rel in core_only:
            continue
        try:
            tree = _ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in _ast.walk(tree):
            if not (isinstance(node, _ast.Constant) and isinstance(node.value, str)):
                continue
            for match in pattern.finditer(node.value):
                name = match.group(1)
                if allowed.search(name):
                    continue
                context = node.value[max(0, match.start() - 60): match.end() + 60].replace("\n", " ")
                if any(word in context for word in ("已刪除", "已移除", "不存在", "以前", "不再", "刪掉", "刪:", "一起刪", "剝掉", "剝除", "不得", "無效", "殘留", "蓋過", "交進去")):
                    continue
                offenders.append(f"{rel}:{node.lineno}: …{context.strip()[:110]}… — {name}")
    assert not offenders, (
        "模型看得到的文字教了已刪的環境變數;改成指名 client.json 的鍵或 "
        "deployment.json 的欄位:\n" + "\n".join(offenders)
    )


# ── 總審 F1-4 / F2-1:三條 gate 各自用「會穿過去的反例」自測 ──


@pytest.mark.smoke
def test_the_docs_gate_catches_bare_mentions_and_env_prefixes_everywhere():
    """false-green 的形狀:不帶 `=` 的教學、環境前綴掛在任何命令前面、`export`。

    2026-09-06:段落例外整組刪除,所以「核心段落 + 啟動核心命令」不再是放行條件 ——
    同一組字串在 README 的哪一節都是 offender。原本綠的那個例子(核心段落 +
    `scripts/launch_servers.py`)刻意留著並改成紅:段落白名單一旦被加回來,它會先變綠。
    """
    assert _doc_offenders("docs/x.md", "把 `AICODE_N_CTX` 設大一點就好。\n")
    assert _doc_offenders("docs/x.md", "設定 AICODE_FIGURE_MAX_VL_CALLS_PER_DOC 可以放寬\n")
    assert _doc_offenders(
        "README.md", "### 4.1 Deployment profile\n\n```bash\nAICODE_MODEL=<X> \\\npython3 scripts/launch_servers.py\n```\n"
    )
    assert _doc_offenders("README.md", "### 5.1 跑 doctor 自檢\n\nAICODE_MODEL=<X> python3 scripts/doctor.py\n")
    assert _doc_offenders("README.md", "### 4.1 Deployment profile\n\nAICODE_MODEL=<X> python3 scripts/doctor.py\n")
    assert _doc_offenders("README.md", "### 4.1 Deployment profile\n\n`AICODE_MODEL` 與 `AICODE_LLAMA_BASE_URL` 一起設\n")
    assert _doc_offenders("README_DEV.md", "## 維護命令索引\n\nAICODE_TEST_JOBS=1 python3 scripts/run_tests.py\n")
    assert not _doc_offenders("docs/x.md", "`AICODE_N_CTX` 已刪除、無替代。\n")
    assert not _doc_offenders("docs/x.md", "cd <CODETRAIL_REPO> 之後看 `[CODETRAIL_ACTION_REQUIRED]` 那一段\n")
    assert not _doc_offenders("docs/x.md", "unset AICODE_NUM_CTX  # 舊版殘留\n")


@pytest.mark.smoke
def test_the_opencode_gate_checks_allowlisted_files_for_dependency_shapes():
    """allowlist 不是整檔豁免:code 檔要看形狀(含註解與 docstring),非 code 檔
    (需求檔 / 資料)一樣要看;不在 allowlist 的檔連提一下都不行。"""
    assert _opencode_offenders("process_env.py", 'proc = subprocess.run(["opencode", "--version"])\n')
    # 形狀例外只放行它自己那一種:剝除清單的 `OPENCODE_` 前綴。
    assert not _opencode_offenders("process_env.py", 'STRIPPED = ("AICODE_", "OPENCODE_")\n')
    assert _opencode_offenders("process_env.py", "import opencode_migrate\n")
    # 掃全文:註解與 docstring 裡的依賴形狀也算(以前 `_code_only_source` 看不到)。
    assert _opencode_offenders("scripts/mcp_catalog.py", '# 以前是從 ~/.config/opencode/opencode.json 讀的\n')
    assert not _opencode_offenders("scripts/mcp_catalog.py", 'LEGACY = "opencode_effective_chars"\n')
    assert _opencode_offenders("requirements.txt", "opencode-ai>=1.0\n")
    assert _opencode_offenders("eval/fixtures/tool_routing/support_matrix.json", '{"cmd": ["opencode", "run"]}\n')
    assert not _opencode_offenders("eval/fixtures/tool_routing/support_matrix.json", '{"era": "opencode"}\n')
    assert _opencode_offenders("some_new_module.py", "import opencode_migrate\n")
    # 遷移工具與兩個 stub 已刪除:同名檔重新出現不再有整檔豁免。
    assert _opencode_offenders("opencode_migrate.py", "import json  # opencode.json\n")
    assert _opencode_offenders("opencode_plugins/codetrail-notify.js", 'export const x = "opencode.json";\n')


@pytest.mark.smoke
def test_the_checker_shape_exemption_never_covers_an_executable_import():
    """`scripts/check_readme_consistency.py` 的形狀例外(`opencode_migrate` / `OPENCODE_` …)
    是給**反向檢查的 pattern 與提示字串**用的;同一個字出現在 `import` / `from … import`
    裡就是 runtime 依賴,任何逐檔例外都蓋不過。

    少了這條,把 `import opencode_migrate` 寫進 checker 會被例外放行 —— 部署又需要那支
    已刪的模組,而 gate 全綠。可執行的形狀(匯入、碰設定 / plugin 路徑、呼叫命令)
    與例外分開判定;例外只剩下套件 / 教學的形狀(`npm` / `opencode-ai`)可以放行。
    """
    checker = "scripts/check_readme_consistency.py"
    # 真正的 runtime 依賴:形狀例外不得放行(同一行帶著例外的字也一樣)。
    assert _opencode_offenders(checker, "import opencode_migrate\n")
    assert _opencode_offenders(checker, "from opencode_migrate import managed_values\n")
    assert _opencode_offenders(checker, "import opencode_migrate as cm  # 反向檢查 OPENCODE_ 用\n")
    assert _opencode_offenders("process_env.py", "from opencode_migrate import OPENCODE_PREFIX\n")
    # 反向檢查器現有的合法字串:pattern、提示、逐檔例外的 key、教學反例的字面值。
    legitimate = (
        '    (r"(?m)^\\s*(?:[$>]\\s*)?python3\\s+opencode_migrate\\.py",\n',
        '     "`python3 opencode_migrate.py`(本版不附帶;只有 docs/troubleshooting.md 的 a1682d5 升級段能教)"),\n',
        '    r"(?m)^\\s*(?:[$>]\\s*)?python3\\s+opencode_migrate\\.py": frozenset({"docs/troubleshooting.md"}),\n',
        '    (r"\\bOPENCODE_[A-Z_]+\\b", "`OPENCODE_*`(runtime 永遠不讀寫舊世代前端的設定)"),\n',
        '    if "npm install -g opencode-ai" in readme_text:\n',
    )
    for line in legitimate:
        assert not _opencode_offenders(checker, line), line
    # 對真實檔案也成立:現行 checker 全檔零 offender。
    assert not _opencode_offenders(checker, (REPO_ROOT / checker).read_text(encoding="utf-8"))


@pytest.mark.smoke
def test_the_migration_command_only_survives_in_the_pinned_upgrade_section():
    """本版不附帶 `opencode_migrate.py`。文件唯一能教它的地方是**指名 `a1682d5`** 的
    升級段(那一段講的正是「把舊安裝路徑固定回那個 commit 再用當時的工具」)。

    判準是行內或段落標題有沒有那個 commit,不是「這份文件叫什麼名字」:少了這條,
    任何 allowlist 文件都能貼一行 `python3 opencode_migrate.py`,而使用者照抄只會
    得到 `No such file or directory`。
    """
    pinned = (
        "### 從舊世代前端升級:用 `a1682d5` 解除舊的設定接管\n\n"
        "```bash\npython3 opencode_migrate.py --check\n```\n"
    )
    assert not _opencode_offenders("docs/troubleshooting.md", pinned)
    inline = "```bash\npython3 opencode_migrate.py --check   # a1682d5 的工具\n```\n"
    assert not _opencode_offenders("docs/troubleshooting.md", inline)
    loose = "### 清掉舊設定\n\n```bash\npython3 opencode_migrate.py\n```\n"
    assert _opencode_offenders("docs/troubleshooting.md", loose)
    assert _opencode_offenders("README.md", "```bash\n$ python3 opencode_migrate.py --check\n```\n")




@pytest.mark.smoke
def test_doctor_runs_as_a_script_from_the_repo_root():
    """`python3 scripts/doctor.py` 是 README 教的用法:repo 模組的 import 必須在把 repo
    root 加進 sys.path **之後**。放錯位置 = 第一行就 ModuleNotFoundError,而 pytest
    走 import 的路徑完全看不到(它的 sys.path 本來就含 repo root)。"""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "scripts/doctor.py", "--help"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
        env=__import__("process_env").child_env(),
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "Traceback" not in proc.stderr, proc.stderr[-800:]


@pytest.mark.smoke
def test_the_gates_also_catch_config_files_and_env_prefixed_commands():
    """`.toml` 依賴、`export AICODE_ROOT=`(協定 / 概念名也不准當環境變數用)、
    `AICODE_MODEL=bogus aicode`(環境前綴掛在任何命令前面,含 `\\` 續行)都要抓。

    2026-09-06:`~/start.sh` / launcher / systemd 的 `Environment=` 三種寫法從
    「契約」變成 offender —— `~/start.sh` 不再 export、loader 只讀
    `deployment.json` 與 argv,照著文件 export 只會得到一個沒有作用的殼層變數。
    """
    assert _opencode_offenders("pyproject.toml", 'dependencies = ["opencode-ai>=1"]\n')
    assert _opencode_offenders("Makefile", "\tnpm install -g opencode-ai\n")
    assert _doc_offenders("docs/x.md", "```bash\nexport AICODE_ROOT=/tmp\n```\n")
    assert _doc_offenders("docs/x.md", "AICODE_ROOT=/tmp python3 mcp_server.py\n")
    assert _doc_offenders("README.md", "### 4.1 Deployment profile\n\n```bash\nAICODE_MODEL=bogus aicode\n```\n")
    assert _doc_offenders("README.md", "### 4.1 Deployment profile\n\n```bash\nAICODE_MODEL=bogus \\\\\naicode\n```\n")
    assert _doc_offenders("README.md", "### 4.1 Deployment profile\n\n```bash\nenv AICODE_MODEL=bogus python3 scripts/doctor.py\n```\n")
    assert _doc_offenders("README.md", "### 4.1 Deployment profile\n\n```bash\nAICODE_MODEL=<X> ~/start.sh\n```\n")
    assert _doc_offenders("README.md", "### 4.1 Deployment profile\n\n```bash\nAICODE_MODEL=<X> \\\\\nMAIN_GPU=0 \\\\\npython3 scripts/launch_servers.py --scope all\n```\n")
    assert _doc_offenders("docs/setup.md", "### systemd unit(永久部署)\n\nEnvironment=AICODE_MODEL=<CODE_MODEL>\n")


@pytest.mark.smoke
def test_the_opencode_gate_scans_config_files_and_the_wrapper():
    """掃的檔案集合本身也是契約:`pyproject.toml` 與沒有副檔名的 `aicode` wrapper 都得在裡面。"""
    scanned = {str(p.relative_to(REPO_ROOT)) for p in _opencode_gate_sources()}
    assert "pyproject.toml" in scanned
    assert "aicode" in scanned


@pytest.mark.smoke
def test_the_handoff_markdown_exemption_is_content_only():
    """`docs/workflows/**/*.md` 的豁免只給 **`.md` 的內容**,不是把目錄從走訪剪掉。

    交接紀錄逐字引用被移除的變數名與舊工具的命令(那正是它要記的事),當使用者
    文件掃就是永遠紅燈;但把整個目錄從 `_walk_files` 剪掉的話,同一個目錄底下的
    `.py` / `.sh` / `.json` / `.toml` 也會一起消失在**所有** gate 的視線外 —— 那等於
    開一個「把可執行檔藏進交接目錄」的後門。所以:
      1. `_handoff_markdown` 只對 `docs/workflows/**` 的 `.md` 為真;
      2. 真實 repo 的 `_walk_files` 仍然走進那個目錄(檔案還在集合裡);
      3. 同目錄的 `.py` 照樣被 OpenCode gate 與 spawn gate 判為 offender。
    """
    assert _handoff_markdown(Path("docs/workflows/x/p.md"))
    assert _handoff_markdown(Path("docs/workflows/session-opencode-cleanup/plan-final.md"))
    assert not _handoff_markdown(Path("docs/workflows/x/tool.py"))
    assert not _handoff_markdown(Path("docs/workflows/x/ci.yaml"))
    assert not _handoff_markdown(Path("docs/workflows/x/settings.json"))
    assert not _handoff_markdown(Path("docs/p.md"))
    assert not _handoff_markdown(Path("docs/workflows/p.md"))  # 直接放在 workflows/ 下的不算
    assert not _handoff_markdown(Path("README.md"))

    walked = {str(p.relative_to(REPO_ROOT)) for p in _walk_files(REPO_ROOT)}
    handoffs = sorted(p for p in walked if p.startswith("docs/workflows/") and p.endswith(".md"))
    assert handoffs, "走訪不得把 docs/workflows/ 整個剪掉(那會連可執行檔一起藏起來)"
    assert all(
        _handoff_markdown(Path(rel)) for rel in handoffs
    ), handoffs

    # 內容 gate 對同目錄的可執行檔一視同仁。
    assert _opencode_offenders("docs/workflows/x/tool.py", "import opencode_migrate\n")
    assert _spawn_offenders("docs/workflows/x/tool.py", "import subprocess\nsubprocess.run(cmd)\n")


@pytest.mark.smoke
def test_the_gates_also_catch_ignore_entries_and_bare_assignments():
    """`.gitignore` 的 `.opencode/` 條目(dot 檔沒被掃)與文件裡單獨一行
    `AICODE_ROOT=/tmp`(概念名當環境變數指派)都要抓;單獨一行的指派在哪一節都要抓。"""
    scanned = {str(p.relative_to(REPO_ROOT)) for p in _opencode_gate_sources()}
    assert ".gitignore" in scanned
    assert _opencode_offenders(".gitignore", ".opencode/\n")
    assert _doc_offenders("docs/x.md", "```bash\nAICODE_ROOT=/tmp\n```\n")
    assert _doc_offenders("docs/x.md", "AICODE_ROOT=/tmp\n")
    assert _doc_offenders("README.md", "### 4.1 Deployment profile\n\nAICODE_ROOT=/tmp\n")
    assert _doc_offenders(
        "README.md", "### 4.1 Deployment profile\n\n```bash\nAICODE_MODEL=<X>\npython3 scripts/launch_servers.py\n```\n"
    )


@pytest.mark.smoke
def test_the_gates_are_structural_not_a_list_of_spellings(tmp_path: Path):
    """OpenCode gate 掃**所有文字檔**(檔名不限、二進位跳過、venv / tests 不掃);docs gate 把 `$TOKEN` / `${TOKEN}` / `TOKEN+=` / `TOKEN :=` / `TOKEN ?=` 一律當環境變數用。"""
    (tmp_path / "Dockerfile.dev").write_text("RUN npm install -g opencode-ai\n", encoding="utf-8")
    (tmp_path / "ci.yaml").write_text("x: 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "site.py").write_text("import opencode\n", encoding="utf-8")
    (tmp_path / "venv2").mkdir()
    (tmp_path / "venv2" / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    (tmp_path / "venv2" / "site.py").write_text("import opencode\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text("opencode\n", encoding="utf-8")
    scanned = {str(p.relative_to(tmp_path)) for p in _iter_text_files(tmp_path)}
    assert {"Dockerfile.dev", "ci.yaml", ".env"} <= scanned
    assert "logo.png" not in scanned
    assert not any(rel.startswith((".venv", "venv2", "tests")) for rel in scanned), scanned
    assert _opencode_offenders("Dockerfile.dev", "RUN npm install -g opencode-ai\n")


    assert _doc_offenders("docs/x.md", "AICODE_ROOT+=/tmp aicode\n")
    assert _doc_offenders("docs/x.md", "AICODE_ROOT := /tmp\n")
    assert _doc_offenders("docs/x.md", "AICODE_ROOT ?= /tmp\n")
    assert _doc_offenders("docs/x.md", "echo $AICODE_ROOT\n")
    assert _doc_offenders("docs/x.md", "ls ${CODETRAIL_REPO}/x\n")
    assert _doc_offenders("README.md", "### 4.1 Deployment profile\n\nAICODE_MODEL+=x aicode\n")
    assert _doc_offenders("README.md", "### 4.1 Deployment profile\n\necho $AICODE_MODEL\n")
    assert not _doc_offenders("docs/x.md", "cd <CODETRAIL_REPO> 看 `[CODETRAIL_ACTION_REQUIRED]`\n")


@pytest.mark.smoke
def test_the_docs_gate_catches_printenv_and_indirect_expansions_and_venv_is_pruned(tmp_path: Path):
    """docs gate:`printenv X` / `${#X}` / `${!X}` 是讀環境變數;走訪對 venv 目錄真剪枝(不走進 `.venv/`)。"""
    assert _doc_offenders("docs/x.md", "printenv AICODE_ROOT\n")
    assert _doc_offenders("docs/x.md", "echo ${#AICODE_ROOT}\n")
    assert _doc_offenders("docs/x.md", "echo ${!AICODE_ROOT}\n")
    (tmp_path / ".venv" / "deep" / "deeper").mkdir(parents=True)
    (tmp_path / ".venv" / "deep" / "deeper" / "x.py").write_text("opencode\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    visited: list[str] = []
    assert [str(p.relative_to(tmp_path)) for p in _iter_text_files(tmp_path, _visited=visited)] == ["ok.py"]
    assert not any(".venv" in d and d != ".venv" for d in visited), visited  # 沒走進 .venv 底下


@pytest.mark.smoke
def test_the_gates_skip_git_control_files_and_catch_printenv_options(tmp_path: Path):
    """linked worktree 的 `.git` 是檔案(`gitdir: …`),走訪不得把它當文字檔掃;docs gate 的 `printenv -0 X` / `declare -p X` 帶選項也算。"""
    (tmp_path / ".git").write_text("gitdir: /home/x/opencode/.git/worktrees/y\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    assert [str(p.relative_to(tmp_path)) for p in _iter_text_files(tmp_path)] == ["ok.py"]

    assert _doc_offenders("docs/x.md", "printenv -0 AICODE_ROOT\n")
    assert _doc_offenders("docs/x.md", "declare -p AICODE_ROOT\n")


@pytest.mark.smoke
def test_only_the_git_control_file_is_skipped_and_env_listings_are_caught(tmp_path: Path):
    """跳過的檔案只有 `.git`(叫 `venv` / `node_modules` 的無副檔名 script 照掃、照抓 opencode);docs gate 的 `env | grep X` 也是讀。"""
    (tmp_path / ".git").write_text("gitdir: /home/x/opencode/.git/worktrees/y\n", encoding="utf-8")
    (tmp_path / "venv").write_text('#!/bin/sh\nexec opencode "$@"\n', encoding="utf-8")
    (tmp_path / "node_modules").write_text("x\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    assert [str(p.relative_to(tmp_path)) for p in _iter_text_files(tmp_path)] == ["node_modules", "ok.py", "venv"]
    assert _opencode_offenders("venv", 'exec opencode "$@"\n')

    assert _doc_offenders("docs/x.md", "env | grep AICODE_ROOT\n")
    assert _doc_offenders("docs/x.md", "set | grep -i AICODE_ROOT\n")


@pytest.mark.smoke
def test_the_docs_gate_catches_grep_pipelines_with_command_and_var_prefixes():
    """docs gate:`env | command grep X`、`env | LC_ALL=C grep X`(pipe 後的 `command` / `VAR=` 前綴)也是讀環境變數。"""
    assert _doc_offenders("docs/x.md", "env | command grep AICODE_ROOT\n")
    assert _doc_offenders("docs/x.md", "env | LC_ALL=C grep AICODE_ROOT\n")


@pytest.mark.smoke
def test_the_docs_gate_catches_sudo_grep_and_redirected_listings():
    """docs gate:`env | sudo grep X`、`env > f; grep X f`(先重導到檔)也是讀環境變數。"""
    assert _doc_offenders("docs/x.md", "env | sudo grep AICODE_ROOT\n")
    assert _doc_offenders("docs/x.md", "env > /tmp/e; grep AICODE_ROOT /tmp/e\n")


@pytest.mark.smoke
def test_the_docs_gate_catches_grep_anywhere_in_a_pipeline():
    """docs gate:列出整份環境之後,管線裡任何地方出現 grep 類(`| sudo -n grep`、`| tee f | grep`)都算。"""
    assert _doc_offenders("docs/x.md", "env | sudo -n grep AICODE_ROOT\n")
    assert _doc_offenders("docs/x.md", "env | tee /tmp/e | grep AICODE_ROOT\n")


@pytest.mark.smoke
def test_the_spawn_gate_bans_the_spawn_api_outside_process_env():
    """spawn 只有一個出口(`process_env.run/popen/check_output`,環境由它自己算),所以 gate
    是一條**不用推導**的禁令:`subprocess` / `pty` / `pexpect` / `os.exec*|spawn*|system|popen|
    fork*` / `asyncio.create_subprocess_*` 在 process_env 與啟動核心以外出現就是 offender ——
    不管是 import、alias、cast、getattr、`__import__`、還是拿來當型別。"""
    for snippet in (
        "import subprocess\n",
        "import subprocess as sp\n",
        "from subprocess import run\n",
        "from subprocess import *\n",
        "from os import system\n",
        "from os import *\n",
        "from asyncio import create_subprocess_exec\n",
        "import os\nos.system(cmd)\n",
        "import os\nos.execvpe(c[0], c, e)\n",
        "import os\nos.fork()\n",
        "import os\nspawn = getattr(os, 'system')\n",
        "getattr(subprocess, 'run')\n",
        "import pty\n",
        "import pexpect\n",
        "asyncio.create_subprocess_exec(*argv)\n",
        "m = __import__('subprocess')\n",
        "import importlib\nm = importlib.import_module('subprocess')\n",
        "x: subprocess.Popen | None = None\n",
        "process_env.run(cmd, env=os.environ)\n",
        "process_env.popen(cmd, env={})\n",
        "from process_env import run\nrun(cmd, env={})\n",
        "from process_env import Popen\nPopen(cmd, env={})\n",
        "env = dict(os.environ)\n",
        "env = os.environ | {'A': '1'}\n",
    ):
        assert _spawn_offenders("x.py", snippet), snippet
    for snippet in (
        "import process_env\nprocess_env.run(cmd, capture_output=True)\n",
        "import process_env\nprocess_env.run(cmd, overrides={'LC_ALL': 'C'})\n",
        "import process_env\nproc = process_env.popen(cmd, stdin=process_env.PIPE)\n",
        "import process_env\ntry:\n    pass\nexcept process_env.TimeoutExpired:\n    pass\n",
        "def home():\n    return os.environ['HOME']\n",
        "environ = os.environ if env is None else env\n",
        "# subprocess 在註解裡沒關係\n",
        "def run(cmd, env=None):\n    return cmd\nrun(cmd, env={})\n",
    ):
        assert not _spawn_offenders("x.py", snippet), snippet
    assert not _spawn_offenders("deployment_profile.py", "import subprocess\nsubprocess.run(cmd)\n")
    assert not _spawn_offenders("process_env.py", "import subprocess as _subprocess\n")


@pytest.mark.smoke
def test_the_production_spawn_gate_really_scans_the_repo(monkeypatch, tmp_path: Path):
    """總審 F12-1:純函式自測綠不代表正式 gate 有接上。把 `_repo_sources` 換成一個放了
    `import subprocess` 的檔,正式 gate(`test_no_module_reads_codetrail_settings_from_the_environment`)
    必須紅。"""
    # 探針檔放 tmp(不碰真 repo):REPO_ROOT 與 _repo_sources 一起換掉,正式 gate 只看到它。
    bad = tmp_path / "sneaky.py"
    bad.write_text("import subprocess\nsubprocess.run(cmd)\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "_repo_sources", lambda *a, **k: iter([bad]))
    with pytest.raises(AssertionError) as excinfo:
        test_no_module_reads_codetrail_settings_from_the_environment()
    assert "sneaky.py" in str(excinfo.value)


@pytest.mark.smoke
def test_the_spawn_gate_resolves_os_and_asyncio_aliases():
    """總審 F12-3:`import os as operating_system; operating_system.system(cmd)`、
    `import asyncio as aio; aio.create_subprocess_exec(...)`、`getattr(operating_system, "system")`
    都是 spawn。"""
    assert _spawn_offenders("x.py", "import os as operating_system\noperating_system.system(cmd)\n")
    assert _spawn_offenders("x.py", "import asyncio as aio\nasync def f():\n    await aio.create_subprocess_exec(*argv)\n")
    assert _spawn_offenders("x.py", "import os as operating_system\nspawn = getattr(operating_system, 'system')\n")
    assert _spawn_offenders("x.py", "import process_env\nprocess_env.Popen(cmd, env={})\n")
    assert not _spawn_offenders("x.py", "import os as operating_system\noperating_system.path.join('a', 'b')\n")


@pytest.mark.smoke
def test_the_spawn_gate_closes_the_private_exits_and_keeps_every_alias():
    """總審第 13 輪 NON-BLOCKER:`process_env` 的私有成員(`_subprocess` 這類)不是出口,
    別的檔拿它就是繞過;alias 表同名多次 import 取聯集(`import os as x … import asyncio as x`
    之後 `x.system` 仍是 spawn)。"""
    assert _spawn_offenders("x.py", "from process_env import _subprocess\n_subprocess.run(cmd)\n")
    assert _spawn_offenders("x.py", "import process_env\nprocess_env._subprocess.run(cmd)\n")
    assert _spawn_offenders("x.py", "import os as x\nx.system(cmd)\nimport asyncio as x\n")
    assert not _spawn_offenders("x.py", "import process_env\nprocess_env.run(cmd)\n")
