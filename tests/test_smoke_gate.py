"""smoke 包的組成契約:AGENTS.md §3 的安全檢查點一律要在 smoke 裡。

為什麼需要這條:smoke 是交付前唯一必跑的閘。2026-08-20 之前它只有 17 條,
全部集中在最近兩週動過的四個檔——§3 點名的安全層(sandbox / 命令白名單 /
apply_patch 上限 / root 驗證)一條都沒有。也就是說「smoke 綠燈」當時什麼都
沒保證,而這種缺口是無聲的:沒有人會因為漏標而收到警告。

這條測試用靜態解析(不 import 被測模組、不跑 pytest 子行程)確認每個安全層
檢查點的**指定 node** 都還在,而且帶著 smoke 標記。

── 為什麼要記到 node 而不是只記檔名 ──────────────────────────────
原本這裡只確認「這個檔至少有一個 smoke 標記」。那個條件太弱:
  - 把 module 層的 `pytestmark` 換成單條 decorator,只留一條無關的測試帶 smoke,
    整份檔的安全契約就退出 smoke 了——而這條 gate 仍然綠燈。
  - 直接刪掉 `test_apply_patch_too_many_files`,只要同檔還有別的 smoke 測試,
    這條 gate 也不會叫。
兩種都是靜默的:交付前跑 smoke 會過,而那個檢查點根本沒跑。
所以下面記的是 node 名,少一個就報。

新增安全檢查點時,把檔名與要守的 node 一起加進 SAFETY_MODULES。刻意只列
**代表該檢查點的那幾條**,不是整份檔的清單:manifest 要能反映意圖,不是
自動產生的目錄。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent

# AGENTS.md §3「安全相關不要砍」的檢查點 → (守它的說明, 必須存在且帶 smoke 的 node)。
SAFETY_MODULES: dict[str, tuple[str, tuple[str, ...]]] = {
    "test_aicode_wrapper.py": (
        "aicode 的 direct-MCP 契約與 experimental Code Mode fail-loud 閘",
        ("test_aicode_refuses_experimental_opencode_code_mode",),
    ),
    "test_fs_sandbox.py": (
        "agent_tools.ToolExecutor._safe_path / media._safe_path",
        (
            "test_safe_path_rejects_dotdot_escape",
            "test_safe_path_rejects_absolute_outside",
            "test_safe_path_rejects_symlink_escape",
            "test_media_safe_path_requires_root",
            "test_media_safe_path_blocks_external_when_disabled",
            "test_analyze_file_blocks_dotdot_escape",
        ),
    ),
    "test_run_command.py": (
        "agent_tools._validate_command(白名單 + dangerous pattern)",
        (
            "test_run_command_disabled_blocks",
            "test_validate_rejects_non_whitelisted",
            "test_validate_rejects_shell_metacharacters",
            "test_validate_rejects_path_traversal_via_arg",
            "test_path_containment_runs_after_shell_metachar_check",
        ),
    ),
    "test_patch_apply.py": (
        "apply_patch 的 context 必須匹配 / 全量 preflight＋best-effort rollback",
        (
            "test_dry_run_reports_context_mismatch",
            "test_multi_file_is_atomic",
            "test_rollback_on_mid_batch_write_failure",
            "test_ambiguous_context_without_hint_is_rejected",
            "test_pure_deletion_mismatch_stays_fail_loud",
        ),
    ),
    "test_patch_parser.py": (
        "apply_patch 的 unified-diff parser 與 max files / sandbox 上限",
        (
            "test_patch_disabled_returns_error",
            "test_apply_patch_rejects_path_outside_sandbox",
            "test_apply_patch_rejects_mismatched_context",
            "test_apply_patch_too_many_files",
        ),
    ),
    "test_patch_search_replace.py": (
        "apply_patch SEARCH/REPLACE 的 sandbox(path escape / symlink)與唯一匹配、不重疊(定位錯就是靜默改錯處)",
        (
            "test_sr_path_escapes_rejected",
            "test_sr_symlink_escape_rejected",
            "test_sr_ambiguous_match_is_rejected_with_zero_writes",
            "test_sr_overlapping_blocks_rejected",
        ),
    ),
    "test_patch_byte_safety.py": (
        "patch_engine 的 byte-safe 寫入(UTF-8 strict / CRLF 保留)與 batch 失敗的 best-effort rollback",
        (
            "test_non_utf8_file_is_rejected_and_bytes_untouched",
            "test_crlf_file_keeps_crlf_after_patch",
            "test_nested_new_file_then_batch_failure_removes_file_and_empty_dirs",
        ),
    ),
    "test_patch_verify.py": (
        "patch_verify:apply_patch 的自動驗證不得暗中 spawn subprocess(寫檔核准不得擴張成執行核准)",
        (
            "test_auto_verify_true_spawns_no_subprocess",
            "test_patch_verify_module_import_allowlist_is_exact",
        ),
    ),
    "test_run_command_timeout.py": (
        "run_command timeout 1..600 的 executor 與 MCP 兩層邊界",
        (
            "test_executor_rejects_timeout_out_of_bounds",
            "test_mcp_call_tool_rejects_non_strict_timeouts",
        ),
    ),
    "test_elf_analysis.py": (
        "elf_analysis.safe_regex:analyze_file target 只接受安全子集的 regex"
        "(Python re 沒有 timeout、不釋放 GIL,一個災難性回溯的 target 會卡死整個同步 MCP server)",
        (
            "test_target_regex_is_guarded_against_redos",
            "test_target_regex_rejects_optional_quantifier_bomb",
            "test_target_regex_rejects_alternation_chain_bomb",
            "test_filter_deadline_is_checked_even_with_zero_matches",
        ),
    ),
    "test_mcp_startup.py": (
        "mcp_server 啟動時的 AICODE_ROOT 驗證與 set_sandbox_root",
        (
            "test_rejects_empty_root",
            "test_rejects_root_slash",
            "test_rejects_home",
            "test_mcp_server_still_wires_up_root_validation",
            "test_mcp_server_rejects_root_slash",
        ),
    ),
    "test_mcp_runtime_policy.py": (
        "PATCH_ENABLED / RUN_COMMAND_ENABLED / build 命令預設",
        (
            "test_defaults_keep_patch_and_run_command_on",
            "test_explicit_patch_zero_disables_patch",
            "test_explicit_run_tests_zero_disables_run_command",
            "test_build_commands_opt_in",
        ),
    ),
    "test_endpoint_policy.py": (
        "prompt 與文件內容只能送到本機 endpoint",
        (
            "test_model_role_rejects_remote_without_opt_in",
            "test_prompt_bearing_calls_reject_remote_without_opt_in",
            "test_redirect_is_fail_loud_and_body_free",
            "test_shared_session_ignores_environment_proxy",
        ),
    ),
    "test_figure_review.py": (
        "figure_review.safe_figure_path(.codetrail/figures 邊界 + symlink + atomic write)",
        (
            "test_safe_figure_path_rejects_unsafe_components",
            "test_safe_figure_path_requires_root_to_be_aicode_root",
            "test_symlink_at_any_layer_blocks_every_write",
            "test_hostile_document_id_stays_inside_the_boundary",
            "test_same_basename_documents_do_not_share_artifacts",
        ),
    ),
    "test_kb_cache_lifecycle.py": (
        "kb_cache 的 embeddings 身分驗證(逐列 chunk id / generation / 內容雜湊)與 fail-loud 重建",
        (
            "test_cache_that_cannot_be_rebuilt_fails_loud_instead_of_reusing_old_vectors",
            "test_tampered_cache_identity_never_produces_a_silent_query",
            "test_row_order_alone_is_not_accepted_as_identity",
            "test_overwriting_the_json_with_a_same_sized_kb_rebuilds_instead_of_misaligning",
            "test_legacy_npz_without_core_identity_is_discarded",
        ),
    ),
    "test_kb_document_identity.py": (
        "KB 文件身分:同 basename、不同來源檔一律 fail-loud(靜默覆蓋 = 靜默錯答)",
        (
            "test_same_basename_from_a_different_directory_is_refused",
            "test_reingesting_the_same_file_still_replaces_in_place",
            "test_removing_the_document_frees_the_name",
            "test_fresh_ingest_clears_previous_identities",
        ),
    ),
    "test_context_budget.py": (
        "knowledge.py 的主模型 prompt 一律過 context gate(超長會被 server 從前面靜默截掉)",
        (
            "test_knowledge_has_exactly_one_ungated_completion_entry",
            "test_gated_completion_refuses_overflow_without_calling_the_server",
        ),
    ),
}


def _smoke_nodes(path: Path) -> tuple[set[str], bool]:
    """回傳 (帶 smoke 的 test function 名集合, 檔內是否有 module 層 smoke)。

    module 層的 `pytestmark = pytest.mark.smoke` 與單條 `@pytest.mark.smoke`
    都算。parametrize 展開後的 node id 帶 `[...]` 後綴,這裡比對的是函式名,
    所以兩種寫法都涵蓋得到。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_level = any(
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets)
        and "smoke" in ast.unparse(node.value)
        for node in tree.body
    )
    smoke: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        decorated = any("smoke" in ast.unparse(d) for d in node.decorator_list)
        if module_level or decorated:
            smoke.add(node.name)
    return smoke, module_level


def _all_test_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


_CONTRACT_NODES = [
    (filename, node)
    for filename, (_, nodes) in sorted(SAFETY_MODULES.items())
    for node in nodes
]


@pytest.mark.smoke
@pytest.mark.parametrize("filename", sorted(SAFETY_MODULES))
def test_safety_checkpoint_file_still_exists(filename: str):
    path = TESTS_DIR / filename
    description = SAFETY_MODULES[filename][0]
    assert path.is_file(), (
        f"{filename} 不存在了。它守的是 AGENTS.md §3 的 {description};"
        f"檔案改名的話要同步更新 SAFETY_MODULES。"
    )


@pytest.mark.smoke
@pytest.mark.parametrize("filename,node", _CONTRACT_NODES)
def test_safety_contract_node_is_in_the_smoke_package(filename: str, node: str):
    path = TESTS_DIR / filename
    description = SAFETY_MODULES[filename][0]
    assert path.is_file(), f"{filename} 不存在了(守 {description})"

    present = _all_test_functions(path)
    assert node in present, (
        f"{filename}::{node} 不見了。它是 AGENTS.md §3「{description}」的檢查點之一。"
        f"改名或合併測試時要同步更新 SAFETY_MODULES —— 只留下同檔的其他測試,"
        f"這個檢查點就靜默地不再被守了。"
    )

    smoke, module_level = _smoke_nodes(path)
    assert node in smoke, (
        f"{filename}::{node} 沒有 smoke 標記(module 層 pytestmark="
        f"{module_level})。它守的是 AGENTS.md §3 的「{description}」;"
        f"交付前只跑 smoke 的話,這個檢查點等於沒被守。"
    )


@pytest.mark.smoke
def test_every_registered_module_names_at_least_one_node():
    """只填檔名不填 node 等於退回舊的弱條件。"""
    empty = [name for name, (_, nodes) in SAFETY_MODULES.items() if not nodes]
    assert not empty, f"這些安全模組沒有指定任何 node: {empty}"


@pytest.mark.smoke
def test_smoke_marker_is_registered():
    """`--strict-markers` 開著;marker 沒登記會讓整包 smoke 靜默變成 collect error。"""
    pyproject = (TESTS_DIR.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert '"smoke:' in pyproject, "pyproject.toml 的 markers 少了 smoke"
