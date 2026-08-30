"""smoke 包的組成契約:AGENTS.md §2 的安全檢查點一律要在 smoke 裡。

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

# AGENTS.md §2「安全相關不要砍」的檢查點 → (守它的說明, 必須存在且帶 smoke 的 node)。
SAFETY_MODULES: dict[str, tuple[str, tuple[str, ...]]] = {
    "test_aicode_wrapper.py": (
        "aicode 的 direct-MCP 契約與 experimental Code Mode fail-loud 閘",
        ("test_aicode_refuses_experimental_opencode_code_mode",),
    ),
    "test_mcp_tool_contract.py": (
        "live MCP 19-tool 固定順序、typed schema 與 catalog budget",
        ("test_live_catalog_is_bounded_typed_and_ordered",),
    ),
    "test_tool_result_budget.py": (
        "省略 max_chars 時結果預算依 call-time n_ctx 的 12% 動態配置",
        ("test_default_budget_tracks_n_ctx",),
    ),
    "test_opencode_checks.py": (
        "受管 build prompt 不得教授被 permission deny 的 bare OpenCode 工具",
        ("test_build_prompt_never_teaches_denied_tools",),
    ),
    "test_set_config_artifacts.py": (
        "未通過完整 routing gate 的 build prompt 不得成為新安裝預設",
        ("test_yes_run_keeps_unmeasured_build_prompt_out_of_default_artifacts",),
    ),
    "test_tool_call_canary.py": (
        "explicit hard gate 與 implicit 四態 diagnostic 必須分離",
        ("test_explicit_gate_and_implicit_diagnostic_are_separate",),
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
    "test_contextual_signals.py": (
        "strict KB 拒答閘不得把同文件的強檢索誤當成使用者點名欄位的存在證據",
        ("test_refuse_answer_rejects_explicitly_missing_identifier",),
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
    "test_ingest_notify.py": (
        "ingest 通知的零誤報/零漏報:舊 run 不得重報、身分逐字保留、"
        "檔名不得偽造 marker、寫端不得改寫呼叫端交來的 payload",
        (
            "test_old_run_failures_never_reach_the_payload",
            "test_review_block_points_at_review_figures",
            "test_unfixable_block_points_at_remove_and_reingest",
            "test_failed_block_offers_accept_or_reingest",
            "test_empty_payload_renders_nothing",
            "test_all_trusted_payload_is_silent",
            "test_unverified_and_legacy_alone_are_silent",
            "test_injected_document_name_keeps_identity_but_cannot_forge_a_failure",
            "test_summary_line_is_single_line_without_rewriting_the_identity",
            "test_summary_survives_names_with_exotic_line_separators",
            "test_format_is_verbatim_and_never_downgrades_a_future_schema",
            "test_incomplete_output_is_an_error_not_ok",
            "test_document_name_is_escaped_inside_the_suggested_commands",
            "test_format_refuses_to_coerce_a_non_json_identity",
            "test_every_marker_we_emit_is_also_stripped",
            "test_busy_detection_is_scoped_to_the_tools_that_can_return_it",
            "test_fallback_still_lists_figures_that_need_review",
            "test_fallback_never_lists_unverified_or_legacy",
            "test_normal_path_also_never_lists_unverified_or_legacy",
            "test_fix_template_carries_confirm_against_image",
        ),
    ),
    "test_mcp_ingest_async.py": (
        "ingest 的 stdout 不得污染 JSON-RPC 通道、失敗不得回報成功、"
        "ingest 期間 KB 工具必須讓路、子行程(含後代)必須收乾淨、19 工具 schema 不變",
        (
            "test_overlapping_read_file_never_pollutes_stdout",
            "test_timeout_is_classified_as_error",
            "test_incomplete_output_is_classified_as_error",
            "test_busy_gate_covers_exactly_the_kb_tools",
            "test_busy_reply_is_partial_not_ok",
            "test_pgid_falls_back_to_pid_when_getpgid_races",
            "test_pgid_is_registered_before_anything_can_fail",
            "test_signal_pgid_snapshot_tracks_liveness_exactly",
            "test_late_child_confirmed_dead_leaves_no_stale_pgid",
            "test_late_child_reaped_on_the_second_try_leaves_nothing_behind",
            "test_reader_stuck_honours_the_reap_result_but_trusts_the_reader",
            "test_reader_stuck_but_reaped_clean_is_not_reported_as_a_survivor",
            "test_sweep_keeps_a_child_while_the_reader_still_holds_the_pipe",
            "test_shutdown_sweeps_again_for_children_registered_during_the_sweep",
            "test_closing_flip_during_the_out_of_lock_reap_reports_the_right_pgid",
            "test_confirmed_dead_group_leaves_the_signal_snapshot_even_while_busy_is_held",
            "test_shutdown_does_not_claim_clean_while_a_pipe_writer_is_still_held",
            "test_hold_child_attaches_even_while_the_reaper_holds_the_record",
            "test_closing_flip_during_reap_leaves_no_second_owner",
            "test_reader_stuck_with_a_confirmed_dead_group_drops_the_pgid_at_once",
            "test_sweep_drops_a_confirmed_empty_group_even_while_the_reader_holds_it",
            "test_holder_attached_after_the_verdict_still_keeps_the_child",
            "test_orphan_warning_never_hands_out_a_kill_for_a_possibly_reused_pid",
            "test_an_interrupt_during_reaping_does_not_lose_the_remaining_children",
            "test_preflight_over_budget_never_claims_content_is_in_the_kb",
            "test_preflight_report_keeps_both_ends_under_a_small_budget",
            "test_shutdown_warns_about_every_child_it_could_not_confirm",
            "test_shell_escape_is_single_line_and_restores_byte_for_byte",
            "test_second_begin_reports_busy_without_deadlocking",
            "test_child_spawned_after_a_reap_is_killed_instead_of_orphaned",
            "test_cancelling_a_request_reaps_the_child_but_keeps_the_server_open",
            "test_surviving_descendants_are_killed_and_never_reported_as_reaped",
            "test_shutdown_escalates_and_never_claims_a_survivor_is_dead",
            "test_catalog_and_ingest_schema_are_unchanged",
            "test_timeout_path_never_claims_termination_while_descendants_live",
            "test_offload_always_keeps_a_timer_alive",
            "test_normal_exit_still_confirms_the_group_before_unregistering",
            "test_orphan_after_shutdown_is_reported_loudly",
            "test_leftover_sweep_waits_for_the_whole_group_not_just_the_leader",
            "test_undecidable_group_is_never_treated_as_reaped",
            "test_a_survivor_is_re_registered_instead_of_being_forgotten",
            "test_late_child_that_cannot_be_reaped_is_kept_not_dropped",
            "test_worker_cancelled_before_reading_its_call_record_never_starts_rag",
            "test_cancelling_one_call_never_reaps_another_calls_child",
            "test_a_cancelled_calls_late_child_is_still_refused",
            "test_busy_is_rearmed_when_a_survivor_is_refilled_after_begin_ended",
            "test_a_long_cancelled_call_is_still_refused_after_many_later_calls",
            "test_marker_shaped_input_never_leaks_into_the_result",
            "test_summary_stripping_never_leaves_half_a_json_line",
            "test_suggested_command_is_the_command_we_actually_ran",
            "test_preflight_suggestion_drops_the_mutually_exclusive_fresh_flag",
            "test_busy_exception_tells_the_model_to_wait_not_to_retry_now",
            "test_timeout_bounds_are_frozen",
        ),
    ),
    "test_mcp_figure_tools.py": (
        "review_figures 的文件身分逐位元組比對(通知給的建議命令用的是同一個身分)",
        ("test_document_identity_is_matched_byte_for_byte",
         "test_extraction_failure_is_stated_affirmatively",
         "test_docs_never_claim_a_single_bad_figure_blocks_the_whole_document"),
    ),
    "test_mcp_lease.py": (
        "MCP per-instance lease 的身分判定(SIGKILL 只能到 stale、pid 重用不得判 live)、"
        "lease 寫入 fail-open、lease/incident 零內容零路徑,以及發布門檻不得被未量測輸入通過",
        (
            "test_two_instances_get_separate_leases_and_never_overwrite",
            "test_close_lease_marks_exited_but_sigkill_only_reaches_stale",
            "test_reused_pid_is_unknown_not_live",
            "test_lease_write_failure_is_silent",
            "test_record_tool_calls_preserves_sync_async_and_signature",
            "test_record_tool_calls_reads_status_from_a_call_tool_result",
            "test_incident_detail_slugs_match_the_frozen_cross_language_set",
            "test_lease_and_incident_files_carry_no_content_path_or_raw_session",
            "test_structured_call_rate_is_none_when_the_denominator_was_never_measured",
            "test_release_gate_thresholds_are_never_relaxed",
            "test_structured_call_gate_passes_only_on_structured_evidence",
            "test_aggregate_counts_no_call_only_for_tool_needed_turns",
            "test_live_server_spawners_never_write_into_the_user_state_dir",
            "test_sigterm_still_runs_the_shutdown_cleanup",
            "test_signal_handler_never_touches_a_lock",
            "test_reap_from_signal_takes_no_lock_and_never_waits",
        ),
    ),
    "test_opencode_notify_plugin.py": (
        "codetrail-notify 的跨語言字面契約、唯一 export、"
        "plugin 失敗不得改動工具結果,以及註冊不得寫進被分析的 repo",
        (
            "test_marker_literal_is_the_frozen_contract",
            "test_incident_constants_are_the_frozen_contract",
            "test_state_dir_resolution_matches_python_exactly",
            "test_incident_line_carries_exactly_the_contract_fields",
            "test_plugin_exports_only_the_factory",
            "test_plugin_stays_silent_offline_and_spawns_nothing",
            "test_plugin_failure_never_touches_the_tool_result",
            "test_marker_outside_the_result_text_never_toasts",
            "test_kill_probe_is_never_allowed_to_claim_alive",
            "test_only_ingest_document_is_trusted_to_emit_the_action_marker",
            "test_claim_detection_ignores_denials",
            "test_lease_classification_never_guesses_dead",
            "test_lease_classification_agrees_across_languages",
            "test_incident_lines_are_content_free",
            "test_project_scoped_config_is_never_given_the_plugin",
            "test_config_inside_any_git_repo_is_never_given_the_plugin",
            "test_config_deep_inside_a_repo_is_still_project_scoped",
            "test_duplicate_local_entries_converge_to_exactly_one",
            "test_unrelated_ancestor_git_never_blocks_the_global_config",
            "test_same_named_remote_plugin_is_never_overwritten",
            "test_absent_plugin_file_is_never_registered",
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
        f"{filename} 不存在了。它守的是 AGENTS.md §2 的 {description};"
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
        f"{filename}::{node} 不見了。它是 AGENTS.md §2「{description}」的檢查點之一。"
        f"改名或合併測試時要同步更新 SAFETY_MODULES —— 只留下同檔的其他測試,"
        f"這個檢查點就靜默地不再被守了。"
    )

    smoke, module_level = _smoke_nodes(path)
    assert node in smoke, (
        f"{filename}::{node} 沒有 smoke 標記(module 層 pytestmark="
        f"{module_level})。它守的是 AGENTS.md §2 的「{description}」;"
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
