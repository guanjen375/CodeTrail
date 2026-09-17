"""smoke 包的組成契約:AGENTS.md §2 的安全檢查點一律要在 smoke 裡。

為什麼需要這條:smoke 是交付前唯一必跑的閘。2026-08-20 之前它只有 17 條,
全部集中在最近兩週動過的四個檔——§2 點名的安全層(sandbox / 命令白名單 /
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

── 為什麼一個檔一條測試、不是一個 node 一條 ──────────────────────
manifest 有三百多個 node。先前每個 node 各展開成一條 parametrize,每條都把
檔案重新 read + ast.parse 一次,整個 gate 要 3.6 秒——比它守的大多數安全測試
加起來還久。現在每個檔只解析一次,一條測試把該檔缺的 node 一次列完;失敗訊息
的資訊量沒有變少(缺哪幾個、哪幾個沒標,全部列出),只是不再重複三百次。

新增安全檢查點時,把檔名與要守的 node 一起加進 SAFETY_MODULES。刻意只列
**代表該檢查點的那幾條**,不是整份檔的清單:manifest 要能反映意圖,不是
自動產生的目錄。合併或改名測試檔時,同步改這裡的檔名鍵。
"""
from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent

# AGENTS.md §2「安全相關不要砍」的檢查點 → (守它的說明, 必須存在且帶 smoke 的 node)。
SAFETY_MODULES: dict[str, tuple[str, tuple[str, ...]]] = {
    "test_allow_directory_regression.py": (
        "/allow add 目錄必須寫入 client.json，讓同一 MCP instance 立即執行該目錄工具；"
        "list 顯示內建與目錄授權，管理操作不進模型歷史；帶路徑或 shell 語法的呼叫仍拒絕且不 spawn，"
        "但拒絕訊息指回裸名稱並列出已授權工具",
        (
            "test_allow_add_directory_reaches_running_mcp_without_restart",
            "test_rejected_tool_path_names_bare_tool_grants_and_shell_limits",
        ),
    ),
    "test_allow_directory_runtime.py": (
        "目錄授權即時重讀固定設定來源，只替換經驗證的 executable；不得沿用舊授權或 PATH，"
        "保留 readonly／核准／參數／timeout／容器閘，MCP 快照僅供零副作用列清單",
        (
            "test_directory_executor_uses_fresh_settings_and_absolute_argv_without_path_fallback",
            "test_directory_executor_revalidates_and_fails_closed_without_cached_mapping",
            "test_directory_executor_keeps_bare_names_dangerous_patterns_and_path_containment",
            "test_directory_executor_does_not_load_before_disabled_or_timeout_gates",
            "test_direct_directory_executor_uses_only_config_and_keeps_permission_and_readonly",
            "test_directory_executor_refuses_container_without_host_or_legacy_semantic_changes",
            "test_live_mcp_reloads_only_allow_from_selected_source_and_keeps_runtime_policy",
            "test_mcp_default_allow_loader_pins_startup_path_without_mutating_runtime",
            "test_command_policy_metadata_is_optional_strict_and_never_changes_model_or_readonly",
            "test_command_policy_cache_does_not_start_request_write_or_wait_on_lifecycle_lock",
            "test_command_policy_snapshot_updates_on_handshake_and_normal_respawn",
        ),
    ),
    "test_command_allowlist_dirs.py": (
        "目錄授權只收有界安全讀取的 executable；schema 不碰現場、拒絕 symlink／不可信 owner、"
        "重名／身分漂移／缺安全能力均 fail-closed，失效後不得沿用舊映射",
        (
            "test_directory_syntax_normalization_never_touches_filesystem",
            "test_directory_inspection_accepts_tools_and_excludes_untrusted_entries",
            "test_directory_inspection_rejects_malformed_or_unbounded_elf_headers",
            "test_directory_inspection_allows_group_write_and_root_owned_sticky_ancestor",
            "test_directory_inspection_enforces_owner_and_world_write_boundaries",
            "test_directory_inspection_excludes_foreign_owned_candidate",
            "test_directory_inspection_never_follows_directory_symlinks",
            "test_directory_inspection_reports_conflicts_without_selecting_a_winner",
            "test_directory_inspection_never_reuses_mapping_after_installation_becomes_invalid",
            "test_directory_inspection_enumeration_is_bounded_and_never_partial_success",
            "test_directory_inspection_reads_at_most_the_header_budget",
            "test_directory_inspection_fifo_swap_is_nonblocking_and_fails_closed",
            "test_directory_inspection_revalidates_identity_and_reports_read_failures",
            "test_directory_inspection_missing_safety_capability_fails_before_open",
        ),
    ),
    "test_client_allow_ui.py": (
        "/allow 的本地設定操作不得送模型或改 runtime；忙碌／核准／審查／readonly 拒絕修改，"
        "壞值與寫入失敗必須可見，列出清單零寫入並說明目前 session 後續命令立即生效",
        (
            "test_allow_list_reads_fresh_settings_without_writes_or_model_history",
            "test_allow_updates_preserve_other_settings_runtime_and_idle_queue",
            "test_allow_active_or_readonly_sessions_reject_mutation_but_allow_list",
            "test_allow_invalid_requests_never_partially_write_settings",
            "test_allow_invalid_settings_remain_visible_and_unchanged",
            "test_allow_save_oserror_is_visible_without_success_or_runtime_changes",
            "test_allow_missing_or_invalid_mcp_policy_never_guesses_effective_whitelist",
            "test_allow_list_marks_runtime_execution_restrictions",
            "test_allow_directory_failure_is_visible_and_disables_the_whole_resolved_list",
            "test_allow_inspection_oserror_is_visible_without_side_effects",
        ),
    ),
    "test_client_allow_config.py": (
        "client.json 額外命令 fail-closed、保留命令不可擴權、批次驗證與 owner-only 持久化；"
        "重套與 readonly 清除授權，無效設定不得半套改變 runtime",
        (
            "test_missing_extra_commands_fail_closed_without_creating_config",
            "test_extra_commands_roundtrip_preserves_other_settings",
            "test_extra_commands_loader_rejects_unsafe_or_ambiguous_values",
            "test_extra_commands_never_widen_reserved_executable_roots",
            "test_allow_edits_read_latest_settings_and_noops_do_not_write",
            "test_allow_invalid_batch_is_rejected_before_reading_or_writing",
            "test_allow_rejects_oversized_result_without_partial_save",
            "test_save_validates_all_values_and_byte_budget_before_any_write",
            "test_allow_edits_keep_owner_only_link_defenses",
            "test_allow_refuses_invalid_existing_settings_without_overwriting_them",
            "test_apply_extra_commands_replaces_copies_and_clears_for_readonly",
            "test_invalid_extra_commands_do_not_partially_apply_runtime",
            "test_allow_directory_settings_remain_editable_when_installation_is_missing",
            "test_allow_directory_loader_rejects_unsafe_or_duplicate_paths",
            "test_allow_invalid_directory_syntax_never_partially_applies_runtime",
            "test_allow_directory_add_preserves_latest_settings_and_duplicate_is_read_only",
            "test_allow_directory_add_validates_whole_union_before_any_write",
            "test_allow_directory_bad_candidate_does_not_create_settings",
            "test_allow_directory_limit_is_checked_before_save_or_inspection",
            "test_allow_directory_save_failure_is_a_visible_config_error",
        ),
    ),
    "test_extra_commands.py": (
        "使用者命令授權必須抵達實際 MCP／executor；設定替換與 readonly 不殘留授權，"
        "額外命令仍受精確名稱、參數、timeout、核准與容器防線保護",
        (
            "test_user_toolchain_commands_load_and_run_without_repo_edit",
            "test_extra_commands_are_replaced_and_readonly_clears_authorization",
            "test_extra_commands_keep_exact_names_and_argument_guards",
            "test_extra_commands_do_not_change_tool_permission_or_execution_gates",
            "test_extra_commands_never_fall_back_from_container_to_host",
            "test_explicit_client_config_reaches_live_mcp_and_readonly_still_denies",
        ),
    ),
    "test_review_source.py": (
        "review 的 Git 路由/helper 隔離、index/attrs/EOL 正規化、no-follow 有界來源、"
        "完整 coverage 與 HEAD/index/worktree 快照驗證，取消必回收 Git 行程",
        (
            "test_review_git_ignores_ambient_routing_and_external_helpers",
            "test_review_eol_normalization_does_not_invent_changed_lines",
            "test_review_auto_text_preserves_a_crlf_index",
            "test_review_global_autocrlf_is_read_without_enabling_helpers",
            "test_review_unknown_global_sources_fail_loud",
            "test_review_unsupported_transforms_are_explicit_coverage_gaps",
            "test_review_index_only_changes_and_unborn_head_are_not_clean_repo_claims",
            "test_review_unmerged_index_aborts_collection",
            "test_review_rejects_unsafe_source_paths_without_reading_targets",
            "test_review_parent_replacement_is_detected_before_publication",
            "test_review_binary_large_and_collection_limits_never_become_complete",
            "test_review_snapshot_rejects_source_drift",
            "test_review_supports_linked_worktree_but_refuses_parent_scope",
            "test_review_cancel_before_collection_never_spawns",
            "test_review_git_bounds_reap_the_process",
            "test_review_unchanged_unsupported_entries_do_not_create_coverage_gaps",
            "test_review_clean_submodule_is_not_selected_but_dirty_content_is",
            "test_review_clean_repository_scan_does_not_spend_selected_payload_budget",
            "test_review_untracked_nested_repository_is_an_explicit_gap_not_a_collection_error",
            "test_review_global_config_precedence_matches_git_normalized_changed_lines",
            "test_review_raw_equal_head_still_honors_index_eol_normalization",
            "test_review_uninitialized_empty_submodule_is_not_a_coverage_gap",
            "test_review_global_config_symlinks_preserve_git_semantics",
            "test_review_global_config_symlink_drift_is_rejected",
            "test_review_global_config_symlink_targets_remain_bounded_and_nonexecuting",
            "test_review_empty_global_attributes_path_disables_default_file",
        ),
    ),
    "test_review_core.py": (
        "review finding 必須完整 JSON、精確檔名/side/hunk 行號及逐字 evidence；"
        "來源資料不成為指令，缺口/錯誤不可宣告為完整零問題",
        (
            "test_review_response_rejects_malformed_or_partial_json",
            "test_review_finding_rejects_fabricated_identity_anchor_or_evidence",
            "test_review_deleted_code_requires_exact_old_side_evidence",
            "test_review_evidence_preserves_indentation_bom_and_lf_line_mapping",
            "test_review_report_keeps_all_coverage_gaps_visible",
            "test_review_renderer_revalidates_manufactured_findings",
            "test_review_prompt_keeps_untrusted_full_source_as_data",
        ),
    ),
    "test_client_review.py": (
        "review 的隔離 readonly MCP、受限 JSON true 工具、共享模型鎖與精確 gate；"
        "草稿不外洩、無歷史或 metrics 寫入，來源至 HTTP/工具/發布全程可取消",
        (
            "test_review_isolated_mcp_ephemeral_history_shared_lock_and_exact_gate",
            "test_review_allowlist_and_json_true_guard_schema_and_dispatch",
            "test_review_overflow_refuses_model_request_without_metrics_or_truncation",
            "test_review_invalid_or_tool_failed_draft_is_never_published",
            "test_review_cancellation_covers_worker_source_file_and_publish_gaps",
            "test_review_cancels_stream_and_pending_tool_without_next_file",
            "test_review_queue_rejects_start_and_stale_snapshot_suppresses_findings",
            "test_review_cancels_headers_and_late_connect_before_releasing_model_slot",
        ),
    ),
    "test_deployment_entrypoints.py": (
        "日常入口零參數且固定 dispatch；host 明確 LAN 同意與 live 身分，device 四端點與"
        "KB 分別授權、交易失敗不留半套、啟動仍保留原專案 cwd",
        (
            "test_public_entrypoints_reject_argv_before_python_or_writes",
            "test_deployment_wrappers_follow_symlinks_and_keep_cwd",
            "test_start_wrapper_only_dispatches_empty_or_exact_stop",
            "test_device_displays_four_destinations_and_separate_kb_consent",
            "test_device_without_affirmative_endpoint_consent_never_writes_or_starts",
            "test_client_setup_invalid_manifest_and_failed_transaction_leave_no_partial_configuration",
            "test_existing_device_uses_aicode_without_setup_or_local_model_probes",
            "test_existing_host_checks_live_readiness_without_reconfiguration_or_restart",
            "test_unready_existing_host_is_fail_loud_and_never_restarted",
            "test_absent_host_services_launch_once_then_require_live_readiness",
            "test_host_lan_requires_explicit_consent_and_exports_without_extra_transaction_target",
            "test_role_menu_routes_client_and_restore_without_gpu_setup",
            "test_host_rejects_unusable_transfer_address_before_setup",
        ),
    ),
    "test_chat_token_count.py": (
        "精確計數與 chat body 同源，endpoint/取消契約 fail-closed，不洩漏 NDA 內容",
        (
            "test_exact_count_sends_the_same_complete_body_as_chat",
            "test_exact_count_fails_closed_without_exposing_response_or_request_bodies",
            "test_exact_count_owns_only_its_scoped_cancellation_transport",
            "test_cancelled_count_cannot_publish_a_late_result_or_bypass_endpoint_policy",
        ),
    ),
    "test_compaction_token_regressions.py": (
        "完整 tokens 跨容量觸發，過大摘要分批原子提交，pending tail 原文與可重試失敗不落停用紀錄",
        (
            "test_plain_conversation_compacts_by_full_tokens_across_live_context_sizes",
            "test_an_already_oversized_summary_is_batched_without_losing_turns",
            "test_overflow_recovery_preserves_the_unanswered_turn_verbatim",
            "test_summary_request_errors_are_retryable_without_a_durable_stop",
            "test_a_later_batch_failure_never_installs_a_partial_summary",
            "test_a_replacement_that_still_overflows_never_changes_history",
        ),
    ),
    "test_message_queue.py": (
        "排隊與本輪補充在安全工具邊界交付；取消、核准、session、失敗與pending狀態不混淆",
        (
            "test_queue_fifo_edit_cancel_and_receipts_follow_history",
            "test_queue_is_bounded_and_inspection_cannot_execute_or_cross_sessions",
            "test_late_turn_choice_cannot_supplement_a_different_task",
            "test_supplements_wait_for_entire_tool_batch_and_are_real_user_messages",
            "test_supplement_does_not_interrupt_approval_or_an_active_write",
            "test_late_supplement_defers_without_false_delivery",
            "test_failed_turn_keeps_undelivered_fifo_until_explicit_resume",
            "test_cancel_before_queued_worker_starts_preserves_its_input",
            "test_supplement_cannot_bypass_readonly_or_context_gate",
            "test_tui_busy_choice_keeps_draft_and_pending_inputs_block_exit_and_session",
            "test_approval_message_editor_never_answers_the_tool_and_preserves_unsent_draft",
        ),
    ),
    "test_text_table_review.py": (
        "可信表格literal與OCR覆核版本、來源、CAS、strict/section合併邊界不可失真",
        (
            "test_exact_table_lookup_never_loads_models_and_preserves_literal_provenance",
            "test_table_lookup_never_returns_ambiguous_or_unverified_values",
            "test_evidence_reader_rejects_unsafe_metadata",
            "test_text_confirmation_is_bound_to_content_source_and_quality",
            "test_reingest_preserves_only_exact_review_binding_and_advances_invalidated_revision",
            "test_strict_text_eligibility_checks_every_section_and_merge_member",
            "test_ocr_heading_prefix_cannot_raise_gate_evidence",
            "test_text_correction_confirmation_and_revocation_publish_new_revisions",
            "test_text_review_cas_rejects_a_concurrent_writer",
            "test_text_review_readonly_paths_have_zero_model_calls_or_writes",
            "test_ocr_review_rejects_source_replacement_by_symlink",
            "test_existing_group_writable_project_and_sources_keep_review_safety",
            "test_strict_ocr_notice_discloses_unverified_status_and_revision_locator",
        ),
    ),
    "test_build_context.py": (
        "編譯target的檔案/條件分支/graph同範圍；generated header精確准入且任何未知不冒充active",
        (
            "test_explicit_missing_target_never_falls_back_to_global_search",
            "test_manifest_admits_exact_generated_inputs_and_never_executes_commands",
            "test_selected_target_filters_semantic_graph_and_context_before_candidate_limits",
            "test_unknown_builtin_expression_and_duplicate_tu_never_form_confirmed_path",
            "test_dependency_hash_and_include_precedence_changes_invalidate_context",
            "test_missing_unsupported_and_unsafe_inputs_stay_unknown",
            "test_verbose_log_forced_include_and_imacros_share_graph_evidence",
            "test_inactive_grep_hits_cannot_consume_selected_lexical_budget",
        ),
    ),
    "test_split_deployment.py": (
        "B不依賴本機權重與GPU；四端點精確授權、不經代理或redirect，模型版本與eval保持同一身分",
        (
            "test_client_setup_requires_no_local_inference_artifacts",
            "test_preflight_without_profile_loads_topology_and_keeps_identity_gate",
            "test_ingest_unavailable_model_identity_keeps_actionable_cli_error",
            "test_deployment_url_cannot_authorize_itself_or_another_role",
            "test_split_context_authorization_stays_independent",
            "test_split_endpoint_rejects_indirection_and_credentials",
            "test_split_identity_never_reads_remote_model_paths",
            "test_model_host_alias_and_export_bind_all_shards_and_projector",
            "test_split_http_ignores_proxy_and_refuses_redirect",
            "test_eval_replay_inherits_only_trusted_transport_authorization",
            "test_split_eval_transport_uses_same_endpoint_policy",
            "test_client_status_verifies_live_aliases_without_local_gpu",
            "test_split_session_eval_fingerprints_four_live_roles_without_local_weights",
            "test_model_host_refuses_changed_weight_alias",
        ),
    ),
    "test_ingest_resume.py": (
        "私有持久checkpoint的中斷、單位身分、完整文件重組與覆核CAS；壞來源與未知selector不得提交",
        (
            "test_checkpoint_shared_codetrail_permissions_preserve_private_units",
            "test_embedding_checkpoint_writes_are_linear_and_durable",
            "test_interruption_retains_only_atomically_completed_units",
            "test_checkpoint_corruption_and_links_fail_closed",
            "test_checkpoint_rejects_source_alias_retargeted_after_capture",
            "test_explicit_external_cli_source_keeps_native_ingest_without_checkpoint",
            "test_checkpoint_directory_symlink_and_concurrent_runner_are_rejected",
            "test_source_config_and_actual_model_changes_invalidate_units",
            "test_context_cache_identity_uses_actual_ingest_model",
            "test_unknown_selector_is_rejected_before_checkpoint_mutation",
            "test_selective_page_redo_reconstructs_full_document_and_rejects_partial_commit",
            "test_figure_checkpoint_replays_canonical_payload_and_model_input_bytes",
            "test_figure_extractor_resume_does_not_execute_completed_candidates",
            "test_redo_figure_expands_duplicate_group_and_retry_failed_preserves_success",
            "test_source_and_text_review_cas_prevent_stale_checkpoint_overwrite",
            "test_read_status_never_creates_state_or_probes_models",
            "test_visual_resume_probes_missing_candidates_before_render_and_skips_completed_ones",
        ),
    ),
    "test_feature_integration.py": (
        "public工具的target、literal value、OCR未知證據與readOnly/ASK/busy邊界不能在adapter丟失",
        (
            "test_exact_table_budget_never_exposes_a_shortened_cell_value",
            "test_text_review_json_error_is_not_reported_as_success",
            "test_ocr_exclusion_and_build_unknown_reach_the_model_text_lane",
            "test_every_public_code_mode_uses_the_selected_build_context",
            "test_new_table_and_review_tools_have_distinct_authority",
            "test_ingest_selector_schema_avoids_unsupported_grammar_repetition_and_keeps_limits",
        ),
    ),
    "test_memory_consistency.py": (
        "多來源記憶體核對不混淆VMA/LMA，不把缺失清零或未知格式當通過，所有證據遵守沙箱",
        (
            "test_consistency_full_evidence_and_missing_zero_are_distinct",
            "test_consistency_reports_exact_ranges_and_source_for_bounds",
            "test_consistency_vma_lma_are_not_compared_as_same_address_space",
            "test_consistency_unrecognized_or_partial_input_cannot_pass",
            "test_consistency_secondary_sources_reject_symlink_and_hardlink",
            "test_consistency_preload_declaration_does_not_prove_execution",
            "test_consistency_metaware_inclusive_end_and_wrapped_name",
            "test_consistency_vma_preload_never_creates_an_unsupported_missing_claim",
            "test_consistency_map_only_section_and_reserved_only_region_are_not_missed",
        ),
    ),
    "test_client_activity.py": (
        "活動只取本次 SSE 的有效 prompt 進度,不重加 cache、不冒充回答或摘要、"
        "不進 session/headless 事件與預熱;新 UI 回呼返回後仍須接住取消",
        (
            "test_request_prompt_progress_reaches_activity_callback",
            "test_bad_progress_cannot_become_output_or_stall_the_response",
            "test_activity_callback_failure_preserves_gate_payload_and_history",
            "test_activity_callback_cancellation_prevents_request_or_more_output",
            "test_tool_activity_cancellation_prevents_approval_and_dispatch",
            "test_preparation_gate_failure_releases_turn_without_post",
            "test_activity_callback_does_not_change_zero_event_prime",
            "test_activity_progress_is_private_detached_and_backward_compatible",
            "test_optional_progress_metrics_never_invent_cache_or_time",
            "test_progress_snapshots_do_not_cross_requests_or_sessions",
            "test_generation_and_finish_close_request_progress_observer",
            "test_compaction_activity_is_advisory_and_phase_limited",
            "test_compaction_activity_preserves_cancel_commit_boundary",
            "test_activity_callback_cannot_swallow_turn_cancelled",
        ),
    ),
    "test_code_dependencies.py": (
        "AST與ctags必要能力、快取後端身分、lazy向量與依賴錯誤傳遞",
        (
            "test_primary_code_parser_unavailability_never_uses_regex",
            "test_incompatible_grammar_probe_stays_none_for_advisory_verification",
            "test_incompatible_parser_constructor_probe_reports_unavailable",
            "test_invalid_header_language_does_not_choose_a_different_grammar",
            "test_invalid_parser_response_cannot_become_an_empty_index",
            "test_ctags_missing_language_cannot_reuse_cached_symbols",
            "test_parser_dependency_errors_cannot_publish_or_reuse_indexes",
            "test_missing_numpy_refuses_code_operations_before_cache_changes",
            "test_code_reranker_failure_never_returns_fusion",
            "test_context_propagates_dependency_failures_instead_of_partial_evidence",
            "test_backend_policy_invalidation_does_not_change_semantic_vector_identity",
        ),
    ),
    "test_rag_dependencies.py": (
        "RAG必要NumPy/jieba、reranker/expansion錯誤與安全提交前置",
        (
            "test_missing_jieba_never_returns_regex_tokens",
            "test_broken_jieba_reports_dependency_failure",
            "test_warm_kb_requires_numpy_and_jieba_before_query_work",
            "test_missing_numpy_cannot_run_small_pool_mmr",
            "test_required_dependency_error_is_not_swallowed_by_kb_load",
            "test_unavailable_reranker_never_uses_a_legacy_policy",
            "test_failed_reranker_batch_never_returns_partial_or_original_ranks",
            "test_reranker_nonfinite_scores_are_a_protocol_error",
            "test_enabled_expansion_propagates_service_and_configuration_failures",
            "test_pdf_probe_does_not_import_the_old_fitz_namespace",
            "test_rag_rejects_incomplete_config_instead_of_standalone_defaults",
            "test_context_writer_requires_nofollow_before_any_write",
            "test_ingest_requires_numpy_before_embedding_cache_or_generation",
            "test_ingest_requires_numpy_before_context_generation",
            "test_store_lock_requires_safety_before_lockfile_creation",
            "test_store_lock_missing_backend_is_typed_before_creation",
            "test_failed_store_lock_cannot_be_swallowed_as_cache_write_warning",
            "test_knowledge_load_requires_openat_before_lockfile_creation",
            "test_rag_load_requires_openat_before_lockfile_creation",
            "test_ingest_requires_openat_before_context_or_embedding_cache",
        ),
    ),
    "test_runtime_dependencies.py": (
        "rg/lint/ELF/container/live ctx/停止驗證只用選定實作",
        (
            "test_grep_requires_ripgrep_and_exposes_dependency_failure",
            "test_lint_does_not_try_a_second_tool",
            "test_elf_requires_pyelftools_before_loading_or_using_cache",
            "test_media_elf_cache_cannot_hide_missing_parser",
            "test_disassembly_uses_only_the_selected_objdump",
            "test_demangling_requires_working_cppfilt_without_poisoning_cache",
            "test_container_auto_never_substitutes_docker",
            "test_docker_cannot_omit_the_user_identity",
            "test_container_python_test_command_has_no_second_interpreter",
            "test_eval_requested_container_import_failure_never_runs_agent",
            "test_startup_requires_observed_context_and_safe_verdict",
            "test_headless_observes_live_context_before_mcp_and_forwards_it",
            "test_stop_cannot_claim_release_without_gpu_evidence",
            "test_stop_listener_probe_cannot_report_unknown_port_as_free",
            "test_aicode_requires_python3_even_when_python_is_available",
            "test_stop_requires_valid_pane_pid_evidence",
            "test_stop_proc_permission_failure_is_not_process_exit",
            "test_stop_tmux_execution_failure_keeps_port_cleanup_and_returns_nonzero",
        ),
    ),
    "test_dependency_safety.py": (
        "安全IO能力缺席零寫入、atomic no-clobber回滾與必要設定依賴",
        (
            "test_missing_dirfd_cannot_apply_patch",
            "test_missing_no_clobber_link_rolls_back_the_whole_patch",
            "test_cache_requires_openat_before_creating_directories",
            "test_private_paths_require_nofollow_before_any_directory_creation",
            "test_prompt_read_cannot_drop_nofollow",
            "test_legacy_rerank_substitutes_are_rejected",
            "test_existing_invalid_deployment_does_not_choose_another_binary",
            "test_unidentified_setup_interpreter_is_not_replaced_from_path",
            "test_doctor_reports_missing_primary_packages_as_failures",
            "test_direct_filesystem_entrypoints_reject_missing_safety_before_writes",
            "test_private_paths_require_owner_capabilities_without_creating_state",
            "test_broken_numpy_cache_dependency_is_not_classified_as_corrupt_data",
        ),
    ),
    "test_section_store.py": (
        "章節同 store 身分、完整長節窗口、持久化與失敗回滾",
        (
            "test_sections_preserve_source_occurrences_and_complete_body",
            "test_section_source_identity_does_not_trim_distinct_document_names",
            "test_legacy_section_runs_do_not_merge_repeated_titles_or_guess_prefixes",
            "test_figure_membership_changes_without_reusing_stale_members_or_reembedding",
            "test_ambiguous_figure_is_never_attached_by_zero_char_offset",
            "test_title_only_text_anchor_keeps_its_section_and_unique_figure_member",
            "test_section_windows_cover_the_tail_once_and_use_the_ingest_embedding_hook",
            "test_section_window_failure_never_returns_or_changes_partial_vectors",
            "test_section_identity_roundtrips_without_source_parse_or_second_embedding",
            "test_legacy_sectionless_cache_cannot_be_used_when_rebuild_is_unavailable",
            "test_legacy_sectionless_cache_rebuilds_from_json_with_complete_section_rows",
            "test_mineru_heading_without_ctx_requires_gate_during_cache_rebuild",
            "test_section_cache_identity_or_vector_corruption_never_loads",
            "test_section_metadata_and_window_policy_changes_require_new_vectors",
            "test_section_membership_only_save_and_source_removal_need_no_embedding",
            "test_section_store_atomic_failure_restores_every_matrix",
            "test_section_store_rejects_stale_text_fields_before_replacing_the_pair",
            "test_zero_sections_are_explicit_and_readonly_missing_cache_creates_no_directory",
            "test_section_cache_rebuild_does_not_publish_over_newer_generation",
        ),
    ),
    "test_section_retrieval.py": (
        "章節展開去重、chunk/node 分數隔離、全候選rerank與MinerU strict邊界",
        (
            "test_section_members_expand_completely_without_duplicates_or_filter_leaks",
            "test_section_rank_never_substitutes_for_a_members_own_dense_or_lexical_gate",
            "test_section_contribution_is_added_once_across_direct_and_variant_recall",
            "test_section_full_text_lexical_recall_reaches_a_long_sections_tail",
            "test_query_sends_all_sixty_gate_qualified_members_to_batched_reranker",
            "test_section_rank_cannot_change_expansion_rerank_or_threshold_decisions",
            "test_inline_vectors_explicitly_disable_sections_without_embedding_calls",
            "test_mineru_text_is_excluded_with_page_reasons_and_normal_refs_disclose_lane",
            "test_strict_mineru_text_exclusion_preserves_a_figures_own_verified_evidence",
            "test_strict_neighbor_and_merge_paths_cannot_reintroduce_mineru_text",
            "test_mineru_figure_headings_cannot_alias_gate_when_no_generated_ctx_exists",
            "test_mineru_heading_only_numeric_literal_cannot_pass_strict_lexical_gate",
            "test_section_lexical_corpus_counts_its_persisted_title_once",
        ),
    ),
    "test_mineru_lane.py": (
        "MinerU來源與字元保留、唯一heading/文字/table owner及頁碼幾何",
        (
            "test_source_digest_is_the_callers_generating_pdf_digest",
            "test_revalidation_rejects_changed_bytes_or_replaced_same_bytes",
            "test_symlink_components_are_rejected_without_writes",
            "test_parent_symlink_swap_after_loading_is_rejected",
            "test_bounded_regular_reads_reject_escape_and_do_not_create_directories",
            "test_invalid_page_geometry_heading_or_readable_schema_fails_loud",
            "test_empty_or_wrong_version_artifacts_never_fall_back_to_native",
            "test_page_indices_and_missing_pages_keep_original_pdf_numbers",
            "test_bbox_display_derotation_matches_native_crop_relative_space",
            "test_explicit_same_page_headings_are_the_only_section_owner",
            "test_code_and_file_tree_characters_survive_chunking_and_locators",
            "test_navigation_exclusion_preserves_offsets_asides_and_footnotes",
            "test_first_page_table_uses_one_structured_owner_and_preserves_artifact",
            "test_table_missing_ambiguous_or_excluded_owner_fails_without_mutation",
            "test_partial_prose_overlap_is_never_erased_as_a_whole_block",
            "test_figure_heading_requires_a_unique_block_on_the_same_page",
            "test_multiple_matching_blocks_leave_figure_heading_empty",
            "test_mineru_text_ownership_preserves_native_quality_guards_and_absence",
            "test_native_text_figure_without_mineru_body_fails_loud",
        ),
    ),
    "test_mineru_ingest.py": (
        "MinerU入庫的來源重驗、零寫入preflight、argv sandbox及品質通知",
        (
            "test_short_page_keeps_distinct_heading_spans",
            "test_mineru_invalid_artifact_precedes_any_kb_access",
            "test_mineru_preflight_never_opens_kb_or_converts_text",
            "test_mineru_conversion_failure_precedes_cache_migration",
            "test_mineru_source_swap_at_commit_never_publishes",
            "test_mineru_cli_preserves_source_options_and_rejects_other_modes",
            "test_mineru_mcp_keeps_source_names_and_checks_both_sandbox_paths",
            "test_mineru_replaced_text_owner_keeps_known_quality_repairs",
            "test_mineru_exclusion_survives_every_mcp_query_return",
            "test_mineru_preloaded_artifact_cannot_bind_another_same_named_pdf",
            "test_mineru_missing_pages_survive_summary_normalization_and_log_truncation",
        ),
    ),
    "test_pdf_quality_review.py": (
        "PDF 品質判定、人工確認與內容缺陷分離；manifest/chunk 同值，legacy 可讀，"
        "修正更新品質且保留 duplicate 來源證明",
        (
            "test_known_damage_is_not_presented_as_manual_judgment",
            "test_excluded_payload_remains_available_without_old_run_warning",
            "test_legacy_manifest_quality_defaults_remain_readable",
            "test_fix_refreshes_quality_without_reusing_previous_evidence",
            "test_writer_rejects_human_confirmation_of_damaged_payload",
            "test_list_refuses_conflicting_quality_metadata",
            "test_fix_preserves_duplicate_model_input_provenance",
            "test_source_incomplete_revision_cannot_be_confirmed_without_reverification",
            "test_unknown_coverage_and_semantic_review_allow_same_payload_confirmation",
            "test_source_reverification_requirement_survives_unavailable_artifact",
            "test_quality_uses_only_definite_producer_reason_slugs",
            "test_quality_never_invents_human_confirmation",
            "test_human_confirmation_cannot_launder_payload_damage",
            "test_manifest_human_record_remains_bidirectional_and_current_revision",
            "test_quality_requires_independent_evidence_and_proven_formatting",
            "test_quality_transcription_coverage_requires_a_source_denominator",
            "test_missing_artifact_does_not_become_known_empty_or_confirmed",
            "test_legacy_chunk_quality_fields_compare_as_conservative_defaults",
        ),
    ),
    "test_pdf_structure_regressions.py": (
        "PDF code/tree 先於 table 幾何，原文無可靠位置不送 VL；navigation 不花 OCR 預算，"
        "不得吞正文或破壞真表格／檔案樹，native_lane 仍只認真 bool",
        (
            "test_code_and_file_tree_override_native_table_geometry",
            "test_code_without_pos_is_deferred_instead_of_vl",
            "test_navigation_candidates_do_not_reserve_ocr_budget",
            "test_fenced_heading_lines_do_not_create_sections",
            "test_navigation_title_does_not_swallow_prose_with_trailing_numbers",
            "test_structure_preserving_normalization_keeps_original_code_and_tree_bytes",
            "test_navigation_spans_only_exclude_confirmed_runs_and_keep_source_offsets",
            "test_real_table_with_one_code_or_path_cell_remains_a_table",
            "test_code_boundary_adjacent_to_table_is_protected_without_swallowing_table",
            "test_navigation_budget_exclusion_still_checks_native_lane_bool",
            "test_code_without_pos_cannot_reenter_as_attached_raster",
        ),
    ),
    "test_pdf_transcription_regressions.py": (
        "PDF 空 schema 只能回退逐行轉錄，空轉錄失敗；不得丟後續 tile，"
        "來源覆蓋率需可靠 denominator、duplicate 重算、實際 calls 受共同預算約束",
        (
            "test_empty_schema_uses_bounded_line_transcription",
            "test_empty_transcription_fails_with_all_attempts_and_sent_inputs",
            "test_first_tile_none_transcribes_later_tiles_without_a_second_sample",
            "test_transcription_coverage_measures_source_and_rechecks_duplicate",
            "test_repeated_raster_text_does_not_claim_source_coverage",
            "test_fallback_transport_still_aborts_the_document_with_provenance",
            "test_transcription_rejects_a_different_source_at_the_declared_pos",
            "test_native_structure_transcription_preserves_bytes_without_vl",
            "test_incomplete_native_channels_cannot_supply_a_transcription_denominator",
            "test_known_source_structure_cannot_be_blessed_as_a_raster_table",
            "test_duplicate_fallback_retains_attempts_and_rechecks_the_source",
        ),
    ),
    "test_pdf_ingest_quality.py": (
        "PDF 整合：已知損壞分流 repair，excluded 保留原文與 payload，"
        "目錄不進檢索／section／caption／generated context；品質與人工確認各自可見",
        (
            "test_known_damage_uses_repair_in_normal_and_fallback_summary",
            "test_pdf_navigation_keeps_source_offsets_but_not_retrieval_noise",
            "test_navigation_rows_cannot_become_figure_captions",
            "test_navigation_candidate_is_excluded_before_structured_dispatch",
            "test_terminal_native_span_uses_text_after_code_reclassification",
            "test_excluded_native_payload_keeps_raw_text_and_auditable_payload",
            "test_unavailable_native_channels_are_visible_even_without_vl_candidates",
            "test_human_carryover_does_not_reuse_superseded_extraction_damage",
            "test_pdf_normalization_retains_code_and_tree_bytes",
            "test_builder_cannot_confirm_a_glyph_damaged_header",
            "test_human_carryover_keeps_duplicate_source_provenance",
            "test_quality_is_separate_from_human_confirmation_in_review_and_query",
            "test_context_generation_navigation_filter_reaches_shared_windows_and_summaries",
            "test_failure_sources_and_detected_region_limits_survive_notification_boundary",
            "test_review_header_retains_manual_count_without_counting_repairs",
            "test_non_table_candidate_still_replaces_its_native_table_span",
            "test_strict_exclusion_hint_retains_review_label_and_repair_routing",
        ),
    ),
    "test_aicode.py": (
        "wrapper 只做四件事:定位 checkout(自己可能是 symlink)、找 python3、檢查 argv 並拒絕"
        "沒有終端機的環境(指向 headless,不靜默降級)、exec 唯一的客戶端。日常入口拒絕"
        "所有使用者參數，接續移到 TUI 選單 —— run / status / sessions 這些內部入口不跑"
        "preflight,轉發過去等於開一條略過 profile 驗證 / ctx 容量閘 / 工具健檢的第二入口。"
        "root 一律是 cwd;殼層裡殘留的 AICODE_* / AI_CODE_* / CODETRAIL_* 對它一律無效;"
        "缺 textual 要 fail-loud 印 pip 指令。唯一的 exec 目標由第一條釘住"
        "(`aicode` 也在 test_repo_consistency.py 的內容 gate 掃描範圍內)",
        (
            "test_the_only_exec_target_is_the_client_next_to_the_wrapper",
            "test_the_wrapper_follows_its_own_symlink_to_find_the_checkout",
            "test_the_sandbox_root_is_always_the_current_directory",
            "test_without_a_tty_the_wrapper_refuses_and_points_at_the_headless_entry",
            "test_a_missing_textual_fails_loud_with_the_pip_command",
            "test_a_polluted_shell_changes_nothing",
            "test_the_wrapper_stays_thin",
            "test_the_wrapper_never_reads_configuration_from_the_environment",
            "test_the_wrapper_rejects_all_user_arguments",
            "test_the_wrapper_never_forwards_session_arguments",
        ),
    ),
    "test_client_preflight.py": (
        "preflight 的交接:每一步交給 deployment_profile / model_resolution 的環境只有 HOME"
        "(殼層裡殘留的 AICODE_* 對這一次啟動一律無效——那是跨 branch 混用的真正機制);"
        "aux server 的硬閘與 canary 都問 profile 的 endpoint、驗的是 repo 裡那一份客戶端;"
        "canary 的時限 / TTL 是 repo 常數而快取位置只由 XDG_CACHE_HOME / HOME 推導;"
        "client.json 的位置沒有覆寫變數(它決定互動 session 的工具權限);"
        "transcript 收 stdout **與 stderr**(canary 的 WARNING 只走 stderr),"
        "而且一定帶壓縮狀態行(自動壓縮被停用是使用者唯一會看到的地方);"
        "`banner_lines()` 是進 TUI 的那一份:摘要一行 + 壓縮狀態行 + 警告(含 preflight 期間"
        "所有 stderr 行),進度行不得進畫面,但 `lines` 仍是完整 transcript;"
        "`keep=True` 不得改變 `note()` 印出來的字(改了就是兩份不一致的 transcript)",
        (
            "test_the_profile_environment_is_home_only",
            "test_userprofile_is_only_a_fallback_when_home_is_absent",
            "test_a_polluted_shell_changes_neither_the_model_nor_the_endpoints",
            "test_the_aux_server_gate_asks_the_profile_not_the_shell",
            "test_the_canary_verifies_the_client_next_to_this_repo",
            "test_the_canary_timeouts_and_ttl_are_repo_constants",
            "test_the_canary_cache_has_no_location_override",
            "test_the_client_config_path_has_no_environment_override",
            "test_the_transcript_keeps_stderr_warnings",
            "test_the_transcript_carries_the_compaction_status",
            "test_the_banner_keeps_stderr_warnings_and_compaction_status_but_drops_the_progress_log",
            "test_keep_does_not_change_what_note_prints",
            "test_every_profile_env_helper_has_the_same_home_only_shape",
            "test_the_canary_child_environment_is_stripped",
        ),
    ),
    "test_set_config.py": (
        "set_config 的壓縮模式:--yes 沒給旗標一律不接管、模式寫進 owner-only 的 client.json、"
        "門檻等於 runtime 用的同一條公式、算不出門檻要 fail-loud、dry-run 不留檔;"
        "restore transaction 的整批語意(一半還原比不還原更糟、manifest 壞掉不退回逐檔、"
        "沒有備份路徑不得刪 live 檔、symlink 被改指不得覆寫別處、寫不出 manifest 不得留 stale);"
        "restore manifest 兩個世代共用同一個檔:含這一代不會寫的目標時整份拒絕、一個檔都不動;"
        "產生的 `~/start.sh` 不 export / 不 unset 任何變數(殼層裡的舊名字對啟動指令無效),"
        "GPU 與 llama-server 路徑寫進 `deployment.json` 的 `services.<role>.gpu` / `llama_bin`",
        (
            "test_generated_start_sh_ignores_legacy_shell_overrides",
            "test_generated_start_sh_rejects_removed_commands_before_dispatch",
            "test_removed_log_shorthands_never_dispatch_tail",
            "test_deployment_json_pins_llama_bin_and_gpus",
            "test_yes_without_the_flag_never_takes_over",
            "test_codetrail_mode_writes_the_chosen_mode",
            "test_the_written_threshold_matches_the_runtime_formula",
            "test_off_mode_is_recorded_too",
            "test_the_client_config_is_owner_only",
            "test_yes_without_the_flag_reuses_the_recorded_mode",
            "test_an_existing_permission_override_survives_a_mode_change",
            "test_dry_run_writes_nothing",
            "test_an_unknown_mode_is_rejected",
            "test_a_context_too_small_for_the_formula_is_fail_loud",
            "test_transaction_staging_files_are_private_from_birth",
            "test_the_restore_manifest_only_lists_this_generations_targets",
            "test_a_manifest_with_a_foreign_target_is_refused_whole",
            "test_a_manifest_written_before_the_upgrade_still_restores",
            "test_restore_refuses_to_write_client_json_through_a_symlinked_parent",
            "test_quitting_at_the_summary_writes_nothing",
            "test_restore_reports_failure_when_a_backup_is_missing",
            "test_restore_never_deletes_a_live_file_when_the_manifest_has_no_backup",
            "test_a_corrupt_manifest_does_not_fall_back_to_per_file_backups",
            "test_restore_refuses_when_a_symlinked_config_was_repointed",
            "test_a_manifest_that_cannot_be_written_does_not_survive_stale",
        ),
    ),
    "test_doctor.py": (
        "explicit hard gate 與 implicit 四態 diagnostic 必須分離;"
        "canary 的 fingerprint 必須涵蓋客戶端與 system prompt(換了就不能沿用舊判定);"
        "canary 走的是唯讀、不落 session 的 headless run;缺 textual 是 FAIL 不是 WARN;"
        "殘留的網頁 backend 只唯讀偵測",
        (
            "test_explicit_gate_and_implicit_diagnostic_are_separate",
            "test_fingerprint_covers_live_protocol_template_build_and_prompt",
            "test_fingerprint_changes_with_project_instructions",
            "test_run_model_attempt_passes_explicit_model_and_ignores_private_output",
            "test_the_canary_runs_the_client_the_wrapper_will_actually_exec",
            "test_expected_tool_contract_matches_mcp_server",
            "test_a_missing_textual_is_a_fail_not_a_warn",
            "test_the_canary_cache_filename_carries_the_schema_number",
            "test_a_leftover_web_backend_is_reported_read_only",
        ),
    ),
    "test_compaction_formula.py": (
        "壓縮門檻公式是單一真值(改了就改變什麼時候壓縮、壓完留多少,而且沒有任何錯誤訊息);"
        "摘要格式核對的七個欄位標題必須從 canonical 文件解析,不是再抄一份字面值;"
        "狀態行顯示的模式必須來自 runtime 用的同一份設定,而且永遠不得 raise",
        (
            "test_derive_settings_follows_the_upstream_formula",
            "test_derive_settings_refuses_a_model_too_small_for_the_contract",
            "test_effective_max_output_matches_upstream_transform",
            "test_tool_result_fraction_matches_the_runtime_contract",
            "test_rule_headings_come_from_the_document",
            "test_canonical_block_is_fail_loud_when_the_doc_drifts",
            "test_no_client_config_reads_as_untouched",
            "test_an_unreadable_config_never_shows_a_stale_mode",
            "test_the_status_line_never_raises",
        ),
    ),
    "test_mcp_server.py": (
        "live MCP 19-tool 固定順序、typed schema 與 catalog budget；"
        "省略 max_chars 時結果預算依 call-time n_ctx 的 12% 動態配置；"
        "mcp_server 啟動時的 AICODE_ROOT 驗證與 set_sandbox_root；"
        "PATCH_ENABLED / RUN_COMMAND_ENABLED / build 命令預設；"
        "git 工具的兩種結果不得互換——真的沒有倉庫回跳過通知(不是 retryable error)，"
        "而 GIT_DIR/.git 壞掉時必須是錯誤(誤報成「沒有倉庫」等於放行模型跳過改檔前的 git 檢查)",
        (
            "test_graph_dependency_failure_is_an_mcp_error",
            "test_elf_tool_failures_are_mcp_errors",
            "test_live_catalog_is_bounded_typed_and_ordered",
            "test_default_budget_tracks_n_ctx",
            "test_rejects_empty_root",
            "test_rejects_root_slash",
            "test_rejects_home",
            "test_mcp_server_still_wires_up_root_validation",
            "test_mcp_server_rejects_root_slash",
            "test_defaults_keep_patch_and_run_command_on",
            "test_readonly_closes_every_switch_and_ignores_the_other_inputs",
            "test_no_environment_variable_can_turn_the_switches_back_on",
            "test_startup_banner_reports_the_readonly_policy",
            "test_build_commands_opt_in",
            "test_git_tools_outside_a_repo_return_a_skip_notice_not_a_retryable_error",
            "test_a_broken_git_environment_is_not_reported_as_a_missing_repo",
            "test_import_never_writes_through_a_dangling_symlink_in_the_upload_dir",
            "test_import_reads_the_source_it_validated_not_a_swapped_one",
            "test_a_readonly_server_refuses_every_mutator",
            "test_the_server_accepts_an_explicit_client_config_path",
            "test_the_rag_subprocess_receives_the_same_client_config",
            "test_malformed_startup_argv_fails_loud",
            "test_a_symlinked_source_lands_under_the_name_the_user_approved",
            "test_the_approval_box_and_the_tool_agree_on_the_upload_directory",
            "test_import_refuses_a_parent_directory_swapped_for_a_symlink_after_validation",
            "test_a_rejected_import_does_not_leak_the_source_fd",
            "test_import_refuses_an_allowed_root_whose_ancestor_was_swapped_for_a_symlink",
            "test_import_traverses_an_execute_only_ancestor",
        ),
    ),
    "test_mcp_ingest.py": (
        "ingest 通知的零誤報/零漏報:舊 run 不得重報、身分逐字保留、檔名不得偽造 marker、寫端不得改寫呼叫端交來的 payload；"
        "ingest 的 stdout 不得污染 JSON-RPC 通道、失敗不得回報成功、ingest 期間 KB 工具必須讓路、子行程(含後代)必須收乾淨、19 工具 schema 不變",
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
            "test_a_filesystem_permission_error_is_not_described_as_a_readonly_refusal",
        ),
    ),
    "test_mcp_lease.py": (
        "MCP per-instance lease 的身分判定(SIGKILL 只能到 stale、pid 重用不得判 live)、lease 寫入 fail-open、lease/incident 零內容零路徑,以及發布門檻不得被未量測輸入通過",
        (
            "test_two_instances_get_separate_leases_and_never_overwrite",
            "test_close_lease_marks_exited_but_sigkill_only_reaches_stale",
            "test_reused_pid_is_unknown_not_live",
            "test_lease_write_failure_is_silent",
            "test_record_tool_calls_preserves_sync_async_and_signature",
            "test_record_tool_calls_reads_status_from_a_call_tool_result",
            "test_incident_detail_slugs_match_the_frozen_cross_language_set",
            "test_the_incident_writer_is_the_python_one_and_session_hashing_stays_private",
            "test_lease_and_incident_files_carry_no_content_path_or_raw_session",
            "test_structured_call_rate_is_none_when_the_denominator_was_never_measured",
            "test_release_gate_thresholds_are_never_relaxed",
            "test_structured_call_gate_passes_only_on_structured_evidence",
            "test_aggregate_counts_no_call_only_for_tool_needed_turns",
            "test_live_server_spawners_never_write_into_the_user_state_dir",
            "test_sigterm_still_runs_the_shutdown_cleanup",
            "test_signal_handler_never_touches_a_lock",
            "test_reap_from_signal_takes_no_lock_and_never_waits",
            "test_the_ready_marker_is_printed_only_after_the_lease_and_signal_handlers_are_armed",
        ),
    ),
    "test_client_mcp.py": (
        "客戶端的 MCP 取消契約:SDK 在 timeout / task 取消時不送 notifications/cancelled"
        "(所以客戶端不用它的 ClientSession 生命週期,用契約測試釘住而不是寫在註解裡)、"
        "客戶端自己配發 request id 並在 Ctrl-C 與 timeout 兩種情況都送出取消、取消真的讓 server 收掉"
        "它的子行程 process group、並行呼叫不得綁錯 id、寬限期過仍無回應就 SIGTERM 該 instance 並讓"
        "所有進行中的呼叫回成 error 再重新 spawn;每次呼叫的 read timeout 固定不得由呼叫端放寬;"
        "工具目錄在啟動時就驗;MCP stderr 預設不落檔且尾端有上限;同一 root 只有一個 instance",
        (
            "test_abort_review_start_cancels_without_lifecycle_lock_or_late_process",
            "test_private_review_cancel_escalation_cannot_respawn_or_close_interactive",
            "test_the_sdk_never_sends_cancelled_on_its_own",
            "test_a_client_timeout_sends_cancelled_with_the_real_request_id",
            "test_an_interrupt_cancels_the_in_flight_call",
            "test_cancelling_reclaims_the_servers_child_process_group",
            "test_a_server_that_ignores_cancellation_is_terminated_and_respawned",
            "test_a_server_that_ignores_sigterm_is_killed",
            "test_concurrent_calls_each_cancel_their_own_request",
            "test_the_read_timeout_is_fixed_and_cannot_be_widened_by_callers",
            "test_mcp_stderr_is_not_persisted_by_default",
            "test_the_stderr_tail_is_bounded",
            "test_an_explicit_stderr_log_is_owner_only",
            "test_an_explicit_stderr_log_refuses_a_symlink",
            "test_tools_list_requires_unprefixed_mcp_names",
            "test_the_catalog_contract_pins_order",
            "test_a_server_with_a_drifted_catalog_is_refused_at_startup",
            "test_only_the_text_blocks_reach_the_model",
            "test_a_live_roundtrip_exposes_the_real_catalog",
            "test_one_engine_process_keeps_exactly_one_mcp_instance",
            "test_closing_a_shared_client_finishes_before_a_replacement_starts",
            "test_env_overrides_cannot_reintroduce_a_stripped_prefix",
            "test_child_environment_isolated_from_parent_and_previous_calls",
            "test_process_env_run_is_the_only_spawn_exit_and_never_takes_env",
            "test_process_env_popen_class_is_not_a_raw_spawn_bypass",
            "test_client_config_and_skip_aux_preflight_reach_the_server_argv",
        ),
    ),
    "test_client_progress.py": (
        "工具結果每次重新查證,只存有界digest;同參數變更放行,近似grep需完整同來源/範圍;"
        "banner不洗計數,截斷/無命中/未知格式不誤併,非唯讀dispatch含error清epoch,只認JSON true",
        (
            "test_exact_repeats_ignore_json_key_order_but_use_the_latest_result",
            "test_interleaved_queries_are_tracked_without_crossing_turns",
            "test_new_evidence_anywhere_in_a_batch_prevents_stagnation",
            "test_nearby_grep_patterns_require_two_stagnant_complete_batches",
            "test_same_batch_near_repeats_are_not_new_evidence",
            "test_new_results_override_near_matches_even_when_a_file_is_restored",
            "test_different_no_hit_queries_are_not_merged",
            "test_changed_scope_or_unknown_arguments_are_not_discarded",
            "test_known_grep_defaults_do_not_resolve_path_spellings",
            "test_incomplete_or_unattributed_grep_results_never_merge_patterns",
            "test_match_counts_and_partial_metadata_are_preserved",
            "test_python_grep_line_clipping_is_not_treated_as_complete_evidence",
            "test_exact_repeat_banner_and_adapter_changes_do_not_wash_the_counter",
            "test_banner_like_data_and_unrelated_status_changes_remain_evidence",
            "test_only_literal_readonly_true_avoids_reset_after_a_dispatch",
            "test_non_dispatched_denials_and_invalid_calls_do_not_clear_the_epoch",
            "test_progress_resets_the_near_stagnation_run",
            "test_retained_state_is_bounded_and_contains_only_private_content_digests",
            "test_invalid_tracking_limits_fail_loud_instead_of_disabling_the_guard",
        ),
    ),
    "test_client_engine.py": (
        "送出去的那一份才算數:reasoning 剝除只動 reasoning 欄位、只丟最新真實使用者訊息之前的、"
        "認不出那則訊息就整段不動;prune 只改 payload,session 檔與畫面保留原文;"
        "懸空 tool_call 必須在送出前補齊;權限 policy 的 readonly 全 deny(判準是 readOnlyHint 不是名單)、"
        "互動的七個 ask 沒核准就不得執行且重問有上限、核准框完整顯示參數(`import_external_file` 也在裡面:那個開關授權的是能力,不是每一次的來源與目的);"
        "只有工具結果的 text block 進模型;ingest marker 只認 ingest_document 的行首;"
        "工具停滯後只准一次 tool_choice=none 收斂,同批超額與違規呼叫全補 error 不執行;"
        "收斂指示 gate/HTTP 同源且不污染 session/預熱,取消不寫答案,非唯讀 dispatch 失敗也清比對狀態;"
        "假工具呼叫偵測不得把否定句算成宣稱;基底規則守 1,600 字元;閘對轉換後的 payload 計數;"
        "`load_session` 是唯一一次受信讀取(模型歷史與畫面歷史同源),`adopt` 之前 engine 零改動,"
        "transcript 只以標記呈現 compaction(畫面跟著模型歷史走 = 壓縮過的那段在畫面上永久消失);"
        "`prime_prompt_cache` 是唯一沒有使用者訊息就打主模型的路徑:零寫入(不進 `_begin_turn`、"
        "不 `_record`、不發事件、不動取消旗標)、只送 `next_turn_prefix()`(與下一輪同一套轉換,"
        "以真實後續 `send()` 的 payload 為準)、實送 `max_tokens=1` 且 gate 保留額就是 1、"
        "非 interactive policy 在任何 probe / request 之前就拒絕、回合已開始就讓路且不讀它的歷史、"
        "換 session 會中止進行中的預熱並放掉模型鎖;"
        "預熱的中止涵蓋 `/slots` probe / 取得 headers 前 / 串流中,中止後不再發 POST"
        "(headers 前就登記 socket，先 shutdown 舊 HTTP 才放鎖，一秒內 aborted / priming=False / 鎖可取);"
        "登記與歷史快照原子化，快速完成仍看 abort，遲到 worker 不送 POST，session 轉換空窗不准入舊歷史;"
        "部分完成的工具群組(a 有結果、b 沒有)的預熱 prefix 必須逐字等於下一輪真的 `send()` 減最後那則 user"
        "(補的「已中斷」結果排在該群組既有結果之後);"
        "只有終結 chunk + `timings.prompt_n` 才記成 sent(`incomplete` / `no_timings` 不寫 telemetry)",
        (
            "test_appending_turns_keeps_the_existing_tool_projection_stable",
            "test_summary_head_clones_reuse_the_full_history_pruning_plan",
            "test_summary_projection_rejects_a_nonprefix_clone",
            "test_pruning_plan_rejects_changed_content_at_a_bound_ordinal",
            "test_exact_input_count_blocks_a_prompt_that_the_character_estimate_accepts",
            "test_fresh_exact_counts_cover_the_sent_payload_and_full_input_event",
            "test_a_count_failure_never_falls_back_to_generation",
            "test_exact_prime_count_refuses_an_overflowing_next_turn",
            "test_cancelling_an_exact_count_prevents_later_generation",
            "test_candidate_context_count_uses_its_own_projection_without_installing_it",
            "test_load_session_leaves_the_engine_untouched_and_adopt_switches_atomically",
            "test_the_snapshot_model_history_is_compacted_while_the_transcript_keeps_the_originals",
            "test_a_web_style_cancel_after_a_failed_turn_is_refused",
            "test_a_cancel_during_the_max_step_wrap_up_records_nothing",
            "test_the_final_commit_persists_outside_the_turn_state_lock",
            "test_a_web_style_cancel_in_the_idle_gap_after_a_commit_is_refused",
            "test_a_cancel_after_the_answer_is_committed_is_refused_and_the_turn_ends_as_stop",
            "test_a_cancel_before_the_answer_is_committed_wins_and_nothing_is_recorded",
            "test_a_cancel_while_the_user_message_is_being_persisted_is_honoured",
            "test_cancelling_an_idle_engine_is_a_no_op_that_never_poisons_the_next_turn",
            "test_cancel_racing_the_natural_end_of_a_turn_never_poisons_the_next_one",
            "test_a_turn_waiting_for_a_leased_model_lock_can_still_be_cancelled",
            "test_a_cancelled_request_never_overlaps_the_next_turn",
            "test_a_cancel_while_waiting_for_the_response_headers_ends_the_turn_promptly",
            "test_a_cancel_that_lands_before_the_handle_is_registered_closes_it_without_reading",
            "test_cancel_unblocks_a_stream_stuck_waiting_for_the_next_line",
            "test_the_stream_handle_shuts_the_socket_down_without_touching_the_generator",
            "test_request_cancel_never_waits_for_the_mcp_grace_period",
            "test_a_cancel_while_waiting_for_the_model_lock_never_sends_the_request",
            "test_a_length_cut_tool_call_is_never_executed",
            "test_a_malformed_sse_line_makes_the_answer_an_error_even_if_stop_follows",
            "test_the_openai_stream_iterator_ignores_sse_comment_and_field_lines",
            "test_the_openai_stream_iterator_refuses_a_malformed_line",
            "test_a_malformed_stream_in_a_summary_request_reads_as_unfinished",
            "test_only_reasoning_before_the_latest_user_message_is_dropped",
            "test_a_history_without_a_real_user_message_is_left_alone",
            "test_a_synthetic_user_message_is_not_the_anchor",
            "test_prune_only_fires_past_both_thresholds",
            "test_a_short_history_is_never_pruned",
            "test_the_transforms_never_touch_the_session_file",
            "test_a_dangling_tool_call_is_healed_before_the_next_request",
            "test_readonly_policy_denies_every_mutator",
            "test_readonly_policy_denies_a_new_tool_that_is_not_read_only",
            "test_interactive_policy_asks_for_the_seven_write_tools",
            "test_external_import_is_gated_behind_a_per_call_approval",
            "test_a_denied_ask_never_reaches_the_mcp_server",
            "test_a_repeatedly_denied_tool_stops_asking_the_user",
            "test_the_approval_box_shows_every_argument_in_full",
            "test_only_the_text_block_is_fed_back_to_the_model",
            "test_the_loop_stops_instead_of_spinning",
            "test_repeated_grep_gets_one_evidence_based_final_pass",
            "test_nearby_grep_patterns_with_the_same_sources_converge",
            "test_convergence_refuses_all_returned_tools_and_preserves_group_order",
            "test_tool_call_budget_completes_unexecuted_members_of_one_batch",
            "test_progress_rechecks_changed_results_and_reads_after_partial_writes",
            "test_failed_convergence_preserves_raw_output_without_completing_the_turn",
            "test_convergence_cancellation_never_commits_an_answer_or_posts_again",
            "test_broken_tool_arguments_are_reported_not_executed",
            "test_only_ingest_document_is_trusted_to_emit_the_action_marker",
            "test_a_marker_in_the_middle_of_a_line_never_notifies",
            "test_denials_are_never_counted_as_claims",
            "test_the_base_rules_stay_under_the_hard_budget",
            "test_project_instructions_can_be_turned_off",
            "test_an_oversized_user_instructions_file_is_fail_loud",
            "test_the_gate_counts_the_transformed_payload",
            "test_the_gate_reserve_is_the_max_tokens_we_send",
            "test_a_compaction_that_cannot_be_persisted_does_not_change_the_history",
            "test_a_session_that_never_persisted_can_still_compact",
            "test_a_round_that_never_produced_an_answer_is_not_a_completed_turn",
            "test_a_truncated_answer_is_not_a_completed_turn",
            "test_the_summary_request_sees_the_pruned_history_not_the_raw_one",
            "test_cancel_stops_the_model_stream_without_recording_an_answer",
            "test_cancel_reaches_an_active_mcp_call_and_heals_the_history",
            "test_a_symlinked_instructions_directory_is_refused",
            "test_a_symlinked_instructions_file_is_refused",
            "test_ctrl_c_during_a_tool_call_sends_the_cancel",
            "test_a_cancel_that_arrives_before_the_turn_starts_is_not_lost",
            "test_cancel_while_waiting_for_approval_does_not_record_a_denial",
            "test_store_error_does_not_follow_into_the_next_session",
            "test_an_empty_answer_is_a_visible_error_not_a_silent_success",
            "test_tool_call_fragments_without_an_index_join_the_last_call",
            "test_cancel_during_the_summary_call_stops_it_and_clears_the_flag",
            "test_complete_reports_the_finish_reason",
            "test_a_stream_without_a_finish_reason_is_an_error_not_an_answer",
            "test_a_truncated_tool_call_stream_never_executes_half_arguments",
            "test_a_failed_new_session_keeps_the_current_conversation",
            "test_a_malformed_compaction_record_does_not_half_switch_the_session",
            "test_the_import_approval_cannot_be_overridden_to_allow",
            "test_the_import_approval_shows_where_the_file_will_land",
            "test_fallback_tool_call_ids_are_unique_across_steps",
            "test_priming_sends_the_prefix_the_next_turn_will_send_and_records_nothing",
            "test_priming_refuses_a_readonly_engine_before_any_probe_or_request",
            "test_priming_yields_to_a_turn_that_already_began_and_never_reads_its_history",
            "test_a_turn_submitted_during_priming_waits_for_the_lock_and_gets_the_primed_prefix",
            "test_a_session_switch_aborts_an_in_flight_prime_and_frees_the_lock",
            "test_priming_skips_only_when_no_slot_is_idle_and_never_raises_or_prints",
            "test_priming_gates_the_one_token_it_sends_after_checking_the_next_turn_reserve",
            "test_priming_is_invisible_to_cancel_and_leaves_the_turn_state_untouched",
            "test_priming_matches_the_next_send_when_a_tool_group_is_only_partly_answered",
            "test_priming_only_counts_as_sent_after_a_terminal_chunk_with_timings",
            "test_a_session_switch_aborts_a_prime_that_has_no_stream_yet_and_never_sends_after_the_abort",
            "test_switching_session_shuts_down_pending_headers_before_releasing_the_model_lock",
            "test_abort_between_prime_registration_and_fast_io_cannot_be_lost",
            "test_a_prime_http_worker_scheduled_after_abort_never_starts_the_post",
            "test_a_prime_arriving_during_session_creation_cannot_keep_the_old_history_alive",
            "test_failed_session_creation_does_not_disable_future_priming",
        ),
    ),
    "test_http_cancel.py": (
        "預熱專用 transport 在 HTTP bytes 前登記 socket，取消後才完成 connect 也不得發送;"
        "TLS 最終 socket 仍受取消，保留共用 session / TLS 驗證 / 無 env proxy 與 netrc / 不跟 redirect;"
        "並行取消都等同一次 shutdown 完成，不以 Event 已設冒充舊 HTTP 已關",
        (
            "test_cancellation_closes_a_late_connection_before_any_http_bytes",
            "test_cancellation_owns_the_final_tls_socket_and_preserves_shared_transport",
            "test_concurrent_cancellation_waits_for_the_same_socket_shutdown",
        ),
    ),
    "test_client_cli.py": (
        "事件流是 canary / routing eval / session_eval replay 的共用介面:形狀與解析器只有一份、"
        "tool-calls 的 step 不算終止、thinking 不得進事件流;headless 預設 ephemeral;"
        "readonly 連客戶端自己的 context metrics 也要關;"
        "headless `run` 沒有預熱的呼叫點(多一條就是 headless 在沒有使用者訊息時打模型);"
        "自檢通過後進 TUI 的橫幅不重播進度 LOG(端對端,真的走 `client_preflight.run()`)",
        (
            "test_headless_resumption_compacts_before_send_and_recovers_without_retry",
            "test_a_provider_prefixed_model_is_normalised_like_the_wrapper",
            "test_a_completed_tool_event_is_recognised_by_the_shared_parser",
            "test_a_denied_or_failed_tool_is_not_a_completed_call",
            "test_a_tool_calls_step_is_not_terminal_but_stop_is",
            "test_reasoning_never_reaches_the_event_stream",
            "test_the_shared_parser_reads_our_own_stream",
            "test_headless_defaults_to_ephemeral",
            "test_a_readonly_run_never_writes_context_metrics_into_the_project",
            "test_the_server_actually_honours_readonly",
            "test_headless_run_compacts_after_a_completed_turn_and_reports_it",
            "test_headless_run_never_primes_the_prompt_cache",
            "test_the_tui_banner_after_a_passing_preflight_drops_the_progress_log_and_keeps_warnings",
        ),
    ),
    "test_client_turns.py": (
        "回合協調器:同一個對話一次只跑一輪;取消要涵蓋 engine 自己看不到的三個狀態"
        "(worker 還沒進 send()、阻塞在核准上、取消與收尾互相搶跑),閒置時的取消一律回 False;"
        "慢速的 MCP 取消不得扣住協調器的鎖;核准沒回答就是拒絕、只能回答一次、非 bool 不算核准;"
        "notice 要在終結事件之前送,失敗也要送終結事件;只有答完(finish=stop)才自動壓縮;"
        "`prime_in_background` 不取回合鎖、不動 `_turn_done` / `_cancelled`(取消對預熱是 no-op、"
        "預熱不得讓 `busy` 變真),回合進行中不得排,壓縮換掉歷史後的那一次必須在放掉回合鎖之後;"
        "每一次預熱(含壓縮後協調器自己排的)都經 `on_prime(reason, outcome)` 回報,先 `on_prime` 再 "
        "`on_done`,engine raise 時 outcome 是 None 而不是不叫,回呼的例外不冒出預熱執行緒、也不互相帶走",
        (
            "test_cancel_wakes_a_turn_waiting_for_approval",
            "test_idle_preparation_is_cancellable_and_does_not_widen_automatic_modes",
            "test_a_context_gate_refusal_can_recover_old_history_without_retrying_tools",
            "test_cancel_counts_even_before_the_worker_enters_send",
            "test_a_cancel_arriving_while_the_turn_is_being_started_is_not_lost",
            "test_the_slow_mcp_cancel_does_not_hold_the_coordinator_lock",
            "test_a_cancel_that_races_the_end_of_the_turn_never_poisons_the_next_one",
            "test_a_cancel_while_idle_is_refused",
            "test_an_unanswered_approval_is_a_refusal",
            "test_an_unknown_approval_id_is_rejected",
            "test_an_approval_can_only_be_answered_once",
            "test_a_non_boolean_granted_is_never_an_approval",
            "test_an_approval_registered_after_the_cancel_is_refused_immediately",
            "test_a_cancel_after_the_answer_is_committed_is_refused",
            "test_a_cancel_during_the_manual_compaction_preflight_is_consumed_not_lost",
            "test_notices_are_delivered_before_the_terminal_event",
            "test_a_turn_failure_still_sends_a_terminal_event",
            "test_two_concurrent_turns_are_refused",
            "test_the_durable_stop_notice_is_published_before_the_turn_starts",
            "test_auto_compaction_only_runs_after_a_completed_answer",
            "test_compaction_activity_reaches_the_ui_before_the_terminal_event",
            "test_a_cancel_accepted_during_compaction_ends_with_a_cancelled_terminal",
            "test_a_manual_compaction_is_a_turn_and_can_be_cancelled",
            "test_a_session_change_rebinds_the_compactor_without_consuming_the_notice",
            "test_a_compaction_that_replaced_the_history_primes_after_the_turn_lock_is_released",
            "test_priming_is_invisible_to_busy_and_cancel_and_refused_while_a_turn_runs",
            "test_every_prime_the_coordinator_runs_reports_through_on_prime",
        ),
    ),
    "test_client_app.py": (
        "TUI:核准框完整顯示參數(含整份 patch)且可捲動、只認真的 bool;Esc / Ctrl-D 只拒絕"
        "那個工具、Ctrl-C 中斷整輪(核准框開著時也一樣、送出後立刻按也生效),閒置的 Ctrl-C "
        "不得顯示成「已中斷」,慢速 MCP 取消不得凍住畫面;回合進行中不得離開、不得換 session、"
        "被拒的第二題不得先貼進畫面;工具展開區要顯示未裁切的 structuredContent;沒有 tty 一律"
        "拒絕並指向 headless;輸入歷史逐字含 NDA 問題,正常退出一定要落檔、多行問題要能還原,"
        "讀寫兩端都拒 symlink(含中間目錄)與 hard link;"
        "接續 / 啟動重播必須貼出**原始記錄**(文字、reasoning、工具含未裁切的 structuredContent、"
        "壓縮標記),工具結果按**宣告群組**配對(fallback call id 每個行程從 call_1 起算,以 id "
        "反查會把結果貼到幾十輪前那個 block 上),重播的 block 不登記給即時事件,busy 一律拒絕換,"
        "切換失敗要保持 session 與畫面(先換 engine 再重播 = 模型在新對話、畫面是舊那段),"
        "`/new` 清畫面,選單的 Esc / Ctrl-C 只收選單(算成中斷或離開都是謊報);"
        "預熱一律經協調器排(mount / `/new` / 換 session 各一次、reason 要對),"
        "而且**不進對話區**(它不是回合、不是答案);"
        "`/status` 反映協調器跑的**最近一次**預熱(含壓縮後協調器自己排的那一次)的結果 / 原因 / "
        "時間 / 觸發點(停在 mount 那一次的 sent = 使用者以為 cache 是熱的,其實這一次是 skipped)",
        (
            "test_review_modal_blocks_chat_session_changes_and_closes_through_cancel",
            "test_a_resumed_history_is_compacted_before_startup_prefill",
            "test_context_display_counts_full_tokens_off_the_ui_thread_and_discards_stale_results",
            "test_resume_replays_the_stored_history",
            "test_a_session_resumed_at_startup_is_shown_on_mount",
            "test_the_session_picker_lists_outlines_and_switches",
            "test_escape_and_ctrl_c_only_close_the_picker",
            "test_a_failed_switch_keeps_the_session_and_the_screen",
            "test_replay_pairs_tool_results_by_declaration_group_not_by_id",
            "test_replay_shows_pre_compaction_originals_and_a_summary_marker",
            "test_replayed_tool_blocks_are_not_registered_for_live_events",
            "test_new_clears_the_screen",
            "test_the_approval_box_shows_the_whole_patch",
            "test_the_approval_keys_only_answer_this_one_tool",
            "test_a_dismissed_approval_is_a_refusal",
            "test_ctrl_c_during_a_turn_cancels_the_whole_turn",
            "test_ctrl_c_with_the_approval_box_open_cancels_the_whole_turn",
            "test_a_cancel_right_after_submitting_still_lands",
            "test_ctrl_c_while_idle_needs_two_presses_and_never_claims_a_cancel",
            "test_ctrl_c_does_not_block_the_ui_on_a_slow_mcp_cancel",
            "test_ctrl_d_with_the_approval_box_open_only_refuses_that_tool",
            "test_leaving_is_refused_while_a_turn_is_running",
            "test_switching_sessions_is_refused_while_a_turn_is_running",
            "test_a_second_question_during_a_turn_is_not_shown_and_keeps_the_input",
            "test_a_pipe_is_refused_and_points_at_the_headless_entry",
            "test_slash_commands_never_reach_the_model",
            "test_thinking_only_toggles_the_display",
            "test_a_tool_call_shows_a_summary_and_can_be_expanded",
            "test_saving_history_never_writes_through_a_hard_link",
            "test_saving_history_never_follows_a_symlink",
            "test_a_symlinked_middle_component_is_refused",
            "test_loading_history_never_reads_through_a_symlink",
            "test_history_is_saved_on_a_normal_exit_and_round_trips_multiline",
            "test_a_new_session_forgets_the_old_tool_blocks",
            "test_the_tui_primes_on_mount_new_and_session_switch_through_the_coordinator",
            "test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction",
            "test_prompt_progress_replaces_waiting_and_previous_answer_phase",
            "test_activity_ignores_late_progress_and_resets_each_model_request",
            "test_activity_is_cleared_when_a_turn_stops",
            "test_activity_is_cleared_on_new_and_resumed_sessions",
            "test_manual_compaction_activity_uses_the_worker_bridge_without_answer_output",
            "test_fast_prefill_never_delays_generation_or_leaks_into_the_next_request",
            "test_prompt_snapshots_use_ten_second_samples_without_inventing_initial_speed",
            "test_compaction_commit_phases_clear_prefill_and_reject_late_snapshots",
            "test_slow_prompt_metrics_remain_visible_in_a_narrow_terminal",
        ),
    ),
    "test_client_store.py": (
        "session 檔逐字含 NDA 內容:必須落在 state 目錄而不是被分析的 repo(相對 XDG_STATE_HOME 與"
        "專案內的 state 目錄都要擋),目錄 0700、檔案 0600、讀寫兩端都拒 symlink 與 hard link,"
        "append 不得建出沒有 header 的檔,header 必須綁這個專案與這個 session,"
        "headless 的 ephemeral store 一個 byte 都不寫;"
        "選單的大綱只取本地**真實** user 訊息(摘要 / 工具輸出當大綱是無聲的:每段對話長得一樣),"
        "零 LLM、零寫入",
        (
            "test_the_outline_is_the_first_real_question_never_the_summary_or_tool_output",
            "test_a_missing_anchor_is_created_inside_the_dir_fd_walk",
            "test_containment_holds_without_proc_self_fd",
            "test_containment_is_judged_on_the_directory_actually_opened",
            "test_sessions_live_under_state_home_not_the_project",
            "test_the_directory_is_owner_only_and_files_are_0600",
            "test_a_symlinked_session_directory_is_refused",
            "test_a_symlinked_session_directory_is_refused_on_read",
            "test_a_symlinked_session_file_is_refused",
            "test_a_hard_linked_session_file_is_refused",
            "test_a_relative_state_home_is_refused",
            "test_a_state_dir_inside_the_project_is_refused",
            "test_append_never_creates_a_session",
            "test_a_session_file_from_another_project_is_refused",
            "test_a_session_file_with_a_foreign_header_id_is_refused",
            "test_a_session_id_from_outside_cannot_escape_the_directory",
            "test_the_ephemeral_store_never_touches_the_filesystem",
            "test_a_state_home_reached_through_a_symlink_into_the_project_is_refused",
            "test_a_symlinked_middle_component_under_state_home_is_refused",
            "test_reading_a_missing_session_creates_no_directory",
            "test_an_anchor_repointed_into_the_project_after_construction_is_still_refused",
        ),
    ),
    "test_client_compaction.py": (
        "壓縮的核對與節錄:規則文字必須逐字等於 docs/compaction-rules.md、門檻由三方共用的"
        "同一個 max_tokens 常數推導、空／只有 reasoning／七欄漂移都要停用該對話並寫 ledger"
        "(跨行程保留,只記不可信的那幾種成因)、節錄不得帶工具參數或輸出也不得含 pending／"
        "synthetic／出錯回合、同一錨點不重壓、摘要請求送的是 prune 後的那一份;"
        "client.json 是 owner-only 且 permission override 不得放寬 readonly",
        (
            "test_a_ledger_written_before_the_upgrade_still_disables_that_session",
            "test_the_whole_compaction_is_one_turn_including_the_preflight",
            "test_a_cancel_accepted_before_an_invalid_summary_is_judged_does_not_stop_compaction",
            "test_a_cancel_accepted_before_a_summary_error_does_not_stop_compaction",
            "test_a_cancel_before_the_commit_point_leaves_the_history_untouched",
            "test_a_cancel_after_the_commit_point_is_refused_and_the_summary_lands",
            "test_compaction_activity_preserves_validation_and_persistence_outcomes",
            "test_cancelling_from_compaction_validation_activity_keeps_history_and_ledger_untouched",
            "test_cancelling_from_compaction_persistence_activity_is_refused",
            "test_compaction_observer_cancellation_is_not_a_persistence_error",
            "test_the_rule_text_is_the_canonical_document_verbatim",
            "test_the_threshold_matches_the_documented_worked_example",
            "test_the_client_output_constant_drives_the_threshold",
            "test_an_empty_summary_is_caught",
            "test_the_seven_fields_out_of_order_are_caught",
            "test_a_summary_missing_one_field_is_caught",
            "test_the_upstream_english_template_is_caught",
            "test_the_excerpt_excludes_pending_synthetic_and_failed_turns",
            "test_the_excerpt_never_carries_tool_arguments_or_output",
            "test_the_ledger_is_content_free_and_owner_only",
            "test_only_untrustworthy_causes_are_remembered",
            "test_a_stopped_session_stays_stopped_across_restarts",
            "test_without_the_config_file_the_mode_is_manual",
            "test_a_world_readable_config_is_refused",
            "test_a_symlinked_config_is_refused",
            "test_a_symlinked_config_directory_is_refused",
            "test_a_hard_linked_config_is_refused",
            "test_a_symlinked_ledger_is_never_written_through",
            "test_a_hard_linked_ledger_is_refused",
            "test_a_symlinked_state_directory_is_refused",
            "test_saving_is_owner_only",
            "test_a_permission_override_never_widens_the_readonly_policy",
            "test_a_reasoning_only_summary_is_its_own_failure",
            "test_a_drifted_summary_stops_this_session",
            "test_a_failed_summary_request_is_retryable_without_touching_the_history",
            "test_the_same_anchor_is_never_compacted_twice",
            "test_the_summary_request_never_carries_tool_arguments",
            "test_the_output_constant_is_bounded_by_the_derivation_formula",
            "test_a_new_session_does_not_inherit_the_previous_summary",
            "test_a_new_session_is_not_stopped_by_the_previous_ones_ledger",
            "test_a_resumed_session_picks_up_its_own_durable_stop",
            "test_the_durable_stop_is_announced_before_the_request_not_after",
            "test_a_compaction_that_cannot_be_persisted_is_reported_as_failed",
            "test_the_summary_request_uses_the_pruned_history",
            "test_a_truncated_or_failed_answer_is_never_the_anchor",
            "test_manual_compaction_refuses_to_swallow_an_unanswered_question",
            "test_manual_compaction_needs_at_least_one_completed_answer",
            "test_last_user_answered_ignores_synthetic_and_tool_steps",
            "test_a_truncated_summary_is_untrusted_and_stops_this_session",
            "test_a_summary_stream_that_never_finished_is_untrusted_too",
            "test_a_cancelled_summary_is_not_a_durable_stop",
        ),
    ),
    "test_deployment.py": (
        "啟動核心的設定只有兩個來源:`deployment.json` / `models.json` 與 argv。"
        "殼層裡殘留的舊名字(`AICODE_MODEL`、五個 `*_GPU`、`CUDA_VISIBLE_DEVICES`、"
        "`LLAMA_BIN`、`MODELS_DIR`、session 名)一律無效 —— 讀回來就是「使用者以為在跑 A、"
        "實際在跑 B」,而且完全無聲;GPU 與 llama-server 路徑的優先序是 argv > 檔案 > 預設;"
        "llama-server 真正被 exec 時的環境要剝掉三前綴 + `LLAMA_ARG_*` + `CUDA_VISIBLE_DEVICES`;"
        "主模型解析器沒有環境變數那條分支",
        (
            "test_the_loader_ignores_every_legacy_override_variable",
            "test_gpu_and_llama_bin_come_from_the_deployment_file_then_argv",
            "test_the_server_environment_strips_gpu_selectors_and_llama_settings",
            "test_the_main_model_resolver_has_no_environment_branch",
        ),
    ),
    "test_server_scripts.py": (
        "啟動核心的 pane 邊界:tmux pane 一律經 `deployment_profile.py exec`(pane 內的最終"
        "環境由 `process_env.llama_server_env()` 算,tmux server / session 的全域環境蓋不過"
        "`deployment.json` 指定的卡);GPU / llama-server 路徑 / session 名 / 逾時只來自"
        "`deployment.json`、repo 常數與 argv,殼層裡的舊名字一律無效",
        (
            "test_the_pane_runs_the_exec_choke_point_with_the_loader_argv",
            "test_the_exec_path_hands_llama_server_a_clean_environment",
            "test_stop_and_status_use_argv_and_constants_not_the_shell",
        ),
    ),
    "test_repo_consistency.py": (
        "靜態 gate:使用者文件不得教不存在的介面,沒有任何模組從殼層取 CodeTrail 設定;"
        "spawn 只有 `process_env` 一個出口,llama-server 唯一的 `execvpe` 用的是"
        "`llama_server_env()`。`docs/workflows/**/*.md` 的豁免只給 `.md` 的內容 ——"
        "同目錄的可執行檔照掃(否則就是把東西藏進交接目錄)",
        (
            "test_user_docs_must_not_teach_removed_flags_or_files",
            "test_removed_daily_cli_commands_are_rejected_by_both_doc_gates",
            "test_runtime_repair_hints_use_supported_daily_or_maintenance_commands",
            "test_routing_eval_docs_require_measured_client_support",
            "test_current_cli_help_describes_the_client",
            "test_the_handoff_markdown_exemption_is_content_only",
            "test_the_only_llama_server_exec_hands_over_the_stripped_environment",
            "test_no_module_reads_codetrail_settings_from_the_environment",
            "test_user_docs_never_teach_a_removed_environment_knob_or_interface",
            "test_model_facing_text_never_teaches_a_removed_environment_knob",
            "test_doctor_no_longer_needs_a_model_prefix",
            "test_the_docs_gate_catches_bare_mentions_and_env_prefixes_everywhere",
            "test_doctor_runs_as_a_script_from_the_repo_root",
            "test_the_spawn_gate_bans_the_spawn_api_outside_process_env",
            "test_the_production_spawn_gate_really_scans_the_repo",
            "test_the_spawn_gate_resolves_os_and_asyncio_aliases",
            "test_the_spawn_gate_closes_the_private_exits_and_keeps_every_alias",
            "test_the_gates_also_catch_config_files_and_env_prefixed_commands",
            "test_source_scan_includes_configs_and_the_wrapper",
            "test_the_gates_also_catch_ignore_entries_and_bare_assignments",
            "test_the_gates_are_structural_not_a_list_of_spellings",
            "test_the_docs_gate_catches_printenv_and_indirect_expansions_and_venv_is_pruned",
            "test_the_gates_skip_git_control_files_and_catch_printenv_options",
            "test_only_the_git_control_file_is_skipped_and_env_listings_are_caught",
            "test_the_docs_gate_catches_grep_pipelines_with_command_and_var_prefixes",
            "test_the_docs_gate_catches_sudo_grep_and_redirected_listings",
            "test_the_docs_gate_catches_grep_anywhere_in_a_pipeline",
        ),
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
            "test_file_info_does_not_describe_binary_as_text",
        ),
    ),
    "test_elf_analysis.py": (
        "elf_analysis.safe_regex:analyze_file target 只接受安全子集的 regex(Python re 沒有 timeout、不釋放 GIL,一個災難性回溯的 target 會卡死整個同步 MCP server)",
        (
            "test_target_regex_is_guarded_against_redos",
            "test_target_regex_rejects_optional_quantifier_bomb",
            "test_target_regex_rejects_alternation_chain_bomb",
            "test_filter_deadline_is_checked_even_with_zero_matches",
        ),
    ),
    "test_run_command.py": (
        "agent_tools._validate_command(白名單 + dangerous pattern)；"
        "run_command timeout 1..600 的 executor 與 MCP 兩層邊界",
        (
            "test_run_command_disabled_blocks",
            "test_validate_rejects_non_whitelisted",
            "test_validate_rejects_shell_metacharacters",
            "test_validate_rejects_path_traversal_via_arg",
            "test_path_containment_runs_after_shell_metachar_check",
            "test_executor_rejects_timeout_out_of_bounds",
            "test_mcp_call_tool_rejects_non_strict_timeouts",
        ),
    ),
    "test_apply_patch.py": (
        "apply_patch 的 context 必須匹配 / 全量 preflight＋best-effort rollback；"
        "apply_patch 的 unified-diff parser 與 max files / sandbox 上限；"
        "apply_patch SEARCH/REPLACE 的 sandbox(path escape / symlink)與唯一匹配、不重疊(定位錯就是靜默改錯處)；"
        "patch_engine 的 byte-safe 寫入(UTF-8 strict / CRLF 保留)與 batch 失敗的 best-effort rollback",
        (
            "test_dry_run_reports_context_mismatch",
            "test_multi_file_is_atomic",
            "test_rollback_on_mid_batch_write_failure",
            "test_ambiguous_context_without_hint_is_rejected",
            "test_pure_deletion_mismatch_stays_fail_loud",
            "test_patch_disabled_returns_error",
            "test_apply_patch_rejects_path_outside_sandbox",
            "test_apply_patch_rejects_mismatched_context",
            "test_apply_patch_too_many_files",
            "test_sr_path_escapes_rejected",
            "test_sr_symlink_escape_rejected",
            "test_sr_ambiguous_match_is_rejected_with_zero_writes",
            "test_sr_overlapping_blocks_rejected",
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
            "test_unsupported_suffix_is_skipped_and_reported_incomplete",
            "test_missing_grammar_is_skipped_not_passed",
        ),
    ),
    "test_endpoint_policy.py": (
        "prompt 與文件內容只能送到本機 endpoint",
        (
            "test_split_chat_token_count_is_allowed_only_on_the_main_endpoint",
            "test_model_role_rejects_remote_without_opt_in",
            "test_prompt_bearing_calls_reject_remote_without_opt_in",
            "test_redirect_is_fail_loud_and_body_free",
            "test_shared_session_ignores_environment_proxy",
        ),
    ),
    "test_knowledge_store.py": (
        "kb_cache 的 embeddings 身分驗證(逐列 chunk id / generation / 內容雜湊)與 fail-loud 重建；"
        "KB 文件身分:同 basename、不同來源檔一律 fail-loud(靜默覆蓋 = 靜默錯答)",
        (
            "test_cache_that_cannot_be_rebuilt_fails_loud_instead_of_reusing_old_vectors",
            "test_tampered_cache_identity_never_produces_a_silent_query",
            "test_row_order_alone_is_not_accepted_as_identity",
            "test_overwriting_the_json_with_a_same_sized_kb_rebuilds_instead_of_misaligning",
            "test_legacy_npz_without_core_identity_is_discarded",
            "test_same_basename_from_a_different_directory_is_refused",
            "test_reingesting_the_same_file_still_replaces_in_place",
            "test_removing_the_document_frees_the_name",
            "test_fresh_ingest_clears_previous_identities",
        ),
    ),
    "test_rag_retrieval.py": (
        "strict KB 拒答閘不得把同文件的強檢索誤當成使用者點名欄位的存在證據;"
        "data flywheel 記的檢索路徑(metadata.trace)必須與實際回傳的 refs 逐筆對齊",
        (
            "test_refuse_answer_rejects_explicitly_missing_identifier",
            "test_query_metadata_carries_the_retrieval_path_aligned_with_refs",
            "test_query_trace_keeps_every_candidate_and_every_reranker_score",
            "test_query_exposes_the_partial_trace_when_retrieval_raises",
            "test_strict_all_excluded_records_the_stop_reason_and_the_excluded_figures",
            "test_the_kb_hands_its_loaded_bytes_to_the_snapshot_hook",
        ),
    ),
    "test_figure_review.py": (
        "figure_review.safe_figure_path(.codetrail/figures 邊界 + symlink + atomic write)",
        (
            "test_safe_figure_path_rejects_unsafe_components",
            "test_safe_figure_path_requires_root_to_be_the_sandbox_root",
            "test_symlink_at_any_layer_blocks_every_write",
            "test_hostile_document_id_stays_inside_the_boundary",
            "test_same_basename_documents_do_not_share_artifacts",
            "test_apply_fix_preserves_mineru_provenance_and_separate_gate",
        ),
    ),
    "test_figure_retrieval.py": (
        "review_figures 的文件身分逐位元組比對(通知給的建議命令用的是同一個身分)",
        (
            "test_document_identity_is_matched_byte_for_byte",
            "test_extraction_failure_is_stated_affirmatively",
            "test_docs_never_claim_a_single_bad_figure_blocks_the_whole_document",
        ),
    ),
    "test_rag_ingest.py": (
        "contextual retrieval 的預設 cache 位置是函式,呼叫端要真的呼叫它(不然任何模型呼叫前就 TypeError);"
        "RAG.py 是獨立行程,client.json 的設定要它自己套(給了路徑就讀那份),壞掉一律 fail-loud",
        (
            "test_the_default_context_cache_root_resolves_without_an_explicit_dir",
            "test_rag_cli_applies_the_client_config_it_is_given",
            "test_rag_client_config_flag_is_as_strict_as_the_mcp_parser",
        ),
    ),
    "test_context_budget.py": (
        "knowledge.py 的主模型 prompt 一律過 context gate(超長會被 server 從前面靜默截掉);"
        "客戶端的閘必須對轉換後實際送出的 payload 計數(含 assistant reasoning / reasoning_content),"
        "保留額必須等於本次 request 實送的 max_tokens;"
        "`prompt_tokens_processed`(真正評估的 token 數)只由 `timings.prompt_n` 填,"
        "與 `actual_prompt_eval_count` 語意分離 —— 混在一起就沒有任何欄位能判 prefix 冷熱",
        (
            "test_measured_input_controls_the_gate_across_live_context_sizes",
            "test_invalid_measured_input_never_becomes_an_estimate",
            "test_processed_timings_never_replace_the_full_input_count",
            "test_usage_only_final_chunk_keeps_full_input_and_processed_tokens_separate",
            "test_knowledge_has_exactly_one_ungated_completion_entry",
            "test_gated_completion_refuses_overflow_without_calling_the_server",
            "test_a_payload_with_reasoning_estimates_higher_than_one_without",
            "test_both_reasoning_field_names_are_counted",
            "test_stripping_reasoning_lowers_the_estimate_again",
            "test_the_reserve_is_this_request_max_tokens",
            "test_an_omitted_reserve_still_uses_the_internal_default",
            "test_the_context_gate_has_no_off_switch",
            "test_processed_prompt_tokens_are_recorded_separately_from_the_total",
        ),
    ),
    "test_evals.py": (
        "私人 session eval 不得把歷史模型回答當 oracle、不得經 symlink 外洩 NDA、匯出的角色形狀要與"
        "validator 一致;replay 的壓縮語意由 eval 自己釘(不讀使用者的 client.json)、推不出門檻要"
        "fail-loud、多輪必須真的接得起來而單輪不落檔、gitignore 掉的三個路徑一樣算 project state;"
        "timeout/checkpoint 不得丟資料且匿名 A/B 不得洩漏模型身分;"
        "data flywheel 永久開啟、readonly 是唯一關閉點、舊 collect_data 鍵 fail-loud,"
        "落點在 state 目錄(0700/0600、拒 symlink,被分析的 repo 零新檔),"
        "而且子行程的環境在交出去之前剝掉全部 CodeTrail 設定變數;"
        "catalog 的模型字元成本不包含 UI schema,所有量測都要通過公開工具契約;"
        "錄製器選定的 llama-server 用不了時"
        "不得悄悄換成 PATH 上另一顆(manifest 宣稱的 build 出處會是別顆的)",
        (
            "test_every_direct_model_request_in_the_routing_eval_uses_the_normalised_model",
            "test_the_routing_eval_child_environment_is_stripped",
            "test_catalog_summary_keeps_input_cost_separate_from_ui_payload",
            "test_routing_catalog_requires_the_public_tool_contract",
            "test_the_session_eval_candidate_model_goes_out_as_argv",
            "test_the_collected_data_never_lands_in_the_analysed_repo",
            "test_the_collected_data_file_is_never_read_or_rewritten_through_a_symlink",
            "test_data_collection_is_always_on_and_only_readonly_turns_it_off",
            "test_the_collector_refuses_a_state_home_inside_the_analysed_repo",
            "test_the_collector_snapshots_the_kb_generation_and_referenced_files_once",
            "test_the_collected_file_rotates_before_it_outgrows_the_reader",
            "test_rotation_and_append_hold_an_exclusive_directory_lock",
            "test_snapshots_never_land_inside_the_repo_even_after_a_symlink_swap",
            "test_source_snapshots_never_follow_symlinks_anywhere_in_the_path",
            "test_a_corrupted_snapshot_is_repaired_or_reported_never_trusted",
            "test_the_kb_snapshot_is_the_loaded_generation_not_the_disk_version",
            "test_blob_snapshots_mark_truncation_instead_of_dropping_files_silently",
            "test_source_snapshots_refuse_a_root_replaced_by_a_symlink",
            "test_snapshot_verification_never_blocks_on_a_fifo",
            "test_session_eval_accepts_bare_gguf_and_legacy_models",
            "test_routing_probes_accept_the_same_model_forms_as_aicode",
            "test_the_routing_client_attempt_sends_the_requested_model",
            "test_mined_draft_excludes_assistant_text_and_raw_session_id",
            "test_suite_rejects_historical_model_answer_as_oracle",
            "test_the_store_export_uses_the_role_shape_the_validator_reads",
            "test_private_writer_refuses_symlink_target",
            "test_private_writer_uses_owner_only_modes",
            "test_blind_bundle_hides_candidate_identity",
            "test_a_replay_timeout_is_a_scored_case_failure_not_a_suite_abort",
            "test_the_recorder_never_substitutes_a_path_binary_for_the_chosen_one",
            "test_resume_checkpoint_rejects_project_state_drift",
            "test_the_replay_pins_its_own_compaction_mode",
            "test_two_compaction_semantics_are_not_the_same_candidate",
            "test_keep_compaction_needs_a_derivable_threshold",
            "test_a_multi_turn_case_actually_continues_the_conversation",
            "test_a_single_turn_case_never_writes_a_session_file",
            "test_the_state_digest_covers_the_ignored_paths",
            "test_a_sanitized_export_strips_assistant_text_and_tool_output",
            "test_private_eval_output_never_lands_in_a_tracked_repo_path",
            "test_the_state_digest_still_runs_when_the_replay_child_blows_up",
            "test_resume_checkpoint_rejects_a_different_suite",
            "test_resume_checkpoint_rejects_a_different_live_model",
            "test_resume_checkpoint_rejects_a_reordered_suite",
            "test_resume_checkpoint_rejects_an_inconsistent_completion_marker",
            "test_raw_export_keeps_the_original_conversation_across_a_compaction",
            "test_the_private_directory_guard_refuses_tracked_repo_paths_at_open_time",
            "test_the_global_collector_partitions_by_the_declared_root_not_cwd",
            "test_session_eval_forwards_skip_aux_preflight_to_the_replay_child",
            "test_the_synthetic_kb_is_bound_to_the_synthetic_project_not_the_cli_root",
            "test_delete_sessions_runs_without_the_removed_environment_parameter",
            "test_the_model_identity_is_built_inside_the_synthetic_project_block",
            "test_the_client_identity_is_stable_across_equivalent_synthetic_roots",
        ),
    ),
}


@pytest.mark.smoke
def test_the_manifest_has_no_duplicate_file_keys():
    """dict literal 裡重複的檔名鍵是**靜默**的:後面那個會蓋掉前面那個。

    2026-09-03 真的發生過 —— 新加的 direction-7 節點被同檔名的舊條目蓋掉,
    gate 照樣綠燈,而那四條測試根本沒被守住。所以用 AST 檢查原始碼,不是檢查
    載入後的 dict(載入後重複已經消失了)。
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    keys: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id != "SAFETY_MODULES" or not isinstance(node.value, ast.Dict):
            continue
        keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
    assert keys, "找不到 SAFETY_MODULES 的 dict literal"
    duplicates = sorted({name for name in keys if keys.count(name) > 1})
    assert not duplicates, f"SAFETY_MODULES 有重複的檔名鍵: {duplicates}"


@cache
def _test_functions(filename: str) -> tuple[frozenset[str], frozenset[str], bool]:
    """回傳 (所有 test function 名, 帶 smoke 的 test function 名, 檔內是否有 module 層 smoke)。

    module 層的 `pytestmark = pytest.mark.smoke` 與單條 `@pytest.mark.smoke`
    都算。parametrize 展開後的 node id 帶 `[...]` 後綴,這裡比對的是函式名,
    所以兩種寫法都涵蓋得到。每個檔只解析一次(lru_cache)。
    """
    tree = ast.parse((TESTS_DIR / filename).read_text(encoding="utf-8"))
    module_level = any(
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets)
        and "smoke" in ast.unparse(node.value)
        for node in tree.body
    )
    present: set[str] = set()
    smoke: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        present.add(node.name)
        if module_level or any("smoke" in ast.unparse(d) for d in node.decorator_list):
            smoke.add(node.name)
    return frozenset(present), frozenset(smoke), module_level


@pytest.mark.smoke
@pytest.mark.parametrize("filename", sorted(SAFETY_MODULES))
def test_safety_checkpoints_are_present_and_in_the_smoke_package(filename: str):
    description, nodes = SAFETY_MODULES[filename]
    path = TESTS_DIR / filename
    assert path.is_file(), (
        f"{filename} 不存在了。它守的是 AGENTS.md §2 的「{description}」;"
        f"檔案改名或合併的話要同步更新 SAFETY_MODULES。"
    )
    present, smoke, module_level = _test_functions(filename)

    missing = [node for node in nodes if node not in present]
    assert not missing, (
        f"{filename} 少了這些檢查點 node: {missing}。它們是 AGENTS.md §2"
        f"「{description}」的檢查點。改名或合併測試時要同步更新 SAFETY_MODULES ——"
        f"只留下同檔的其他測試,這些檢查點就靜默地不再被守了。"
    )
    unmarked = [node for node in nodes if node not in smoke]
    assert not unmarked, (
        f"{filename} 這些檢查點沒有 smoke 標記(module 層 pytestmark="
        f"{module_level}): {unmarked}。它們守的是 AGENTS.md §2 的「{description}」;"
        f"交付前只跑 smoke 的話,這些檢查點等於沒被守。"
    )


@pytest.mark.smoke
def test_every_registered_module_names_at_least_one_node():
    """只填檔名不填 node 等於退回舊的弱條件;同一個檔重複列同一個 node 是打錯字。"""
    empty = [name for name, (_, nodes) in SAFETY_MODULES.items() if not nodes]
    assert not empty, f"這些安全模組沒有指定任何 node: {empty}"
    duplicated = {
        name: sorted({node for node in nodes if nodes.count(node) > 1})
        for name, (_, nodes) in SAFETY_MODULES.items()
        if len(set(nodes)) != len(nodes)
    }
    assert not duplicated, f"manifest 裡重複登記的 node: {duplicated}"


@pytest.mark.smoke
def test_smoke_marker_is_registered():
    """`--strict-markers` 開著;marker 沒登記會讓整包 smoke 靜默變成 collect error。"""
    pyproject = (TESTS_DIR.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert '"smoke:' in pyproject, "pyproject.toml 的 markers 少了 smoke"
