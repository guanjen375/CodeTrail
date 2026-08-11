"""README / docs ↔ mcp_server.py / config.py 一致性測試。"""
from __future__ import annotations

from scripts.check_readme_consistency import (
    _check_code_model_placeholder_contract,
    _check_doctor_commands_have_explicit_model,
    _check_default_aux_models_documented,
    _check_forbidden_main_model_tokens,
    _check_agents_template_tools,
    _check_opencode_timeout_contract,
    _check_permission_template_contract,
    _config_int_constant,
    _config_model_values,
    _mcp_tool_names,
    _readme_claimed_tool_count,
    _readme_tool_names_in_table,
    check_all,
)


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
