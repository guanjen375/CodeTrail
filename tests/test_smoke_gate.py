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
    "test_aicode.py": (
        "aicode 的正常路徑不得 exec 任何 opencode 二進位;web 的密碼硬規則(非 loopback / mDNS)"
        "與經驗證的 Tailscale 例外必須擋得住 env 偽造;舊安裝的遷移只警告不寫檔",
        (
            "test_a_model_before_web_is_not_forwarded_raw",
            "test_global_policy_before_web_stays_in_front_of_the_subcommand",
            "test_a_global_session_before_attach_is_forwarded",
            "test_aicode_never_execs_opencode",
            "test_aicode_warns_about_a_pending_migration_without_blocking",
            "test_aicode_web_non_local_hostname_without_password_refused",
            "test_aicode_web_non_local_hostname_with_password_allowed",
            "test_aicode_web_mdns_without_password_refused",
            "test_aicode_web_verified_tailscale_ip_without_password_allowed",
            "test_aicode_web_forwards_the_verified_tailscale_hostname",
            "test_aicode_web_tailscale_exception_is_not_bypassable",
            "test_aicode_web_rejects_unsafe_root",
            "test_a_bad_flag_is_rejected_before_any_preflight",
            "test_a_positional_project_directory_becomes_the_root",
            "test_aicode_web_rejects_a_flag_the_client_does_not_have",
            "test_root_flag_moves_the_preflight_too",
            "test_global_options_before_attach_still_route_to_the_thin_client",
            "test_global_options_before_web_keep_the_wrapper_gate",
            "test_a_password_gate_cannot_be_skipped_by_putting_options_before_web",
        ),
    ),
    "test_set_config.py": (
        "set_config 的壓縮模式:--yes 沒給旗標一律不接管、模式寫進 owner-only 的 client.json、"
        "門檻等於 runtime 用的同一條公式、算不出門檻要 fail-loud、dry-run 不留檔;"
        "restore transaction 的整批語意(一半還原比不還原更糟、manifest 壞掉不退回逐檔、"
        "沒有備份路徑不得刪 live 檔、symlink 被改指不得覆寫別處、寫不出 manifest 不得留 stale);"
        "restore 不得再寫使用者的 opencode.json;遷移不得碰沒接管過的機器",
        (
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
            "test_the_restore_manifest_no_longer_lists_opencode_json",
            "test_an_old_manifest_never_writes_back_the_users_opencode_config",
            "test_the_migration_never_touches_a_machine_that_never_took_over",
            "test_quitting_at_the_summary_writes_nothing",
            "test_restore_reports_failure_when_a_backup_is_missing",
            "test_restore_never_deletes_a_live_file_when_the_manifest_has_no_backup",
            "test_a_corrupt_manifest_does_not_fall_back_to_per_file_backups",
            "test_restore_refuses_when_a_symlinked_config_was_repointed",
            "test_a_manifest_that_cannot_be_written_does_not_survive_stale",
            "test_set_config_refuses_to_write_when_the_migration_state_cannot_be_judged",
            "test_set_config_refuses_to_write_when_the_ownership_state_is_untrusted",
        ),
    ),
    "test_doctor.py": (
        "explicit hard gate 與 implicit 四態 diagnostic 必須分離;"
        "canary 的 fingerprint 必須涵蓋客戶端與 system prompt(換了就不能沿用舊判定);"
        "canary 走的是唯讀、不落 session 的 headless run",
        (
            "test_explicit_gate_and_implicit_diagnostic_are_separate",
            "test_fingerprint_covers_live_protocol_template_build_and_prompt",
            "test_fingerprint_changes_with_project_instructions",
            "test_run_model_attempt_passes_explicit_model_and_ignores_private_output",
            "test_the_canary_runs_the_client_the_wrapper_will_actually_exec",
            "test_expected_tool_contract_matches_mcp_server",
        ),
    ),
    "test_compaction_mode.py": (
        "壓縮模式的 ownership 狀態檔:owner-only 權限與 symlink 防線、digest 涵蓋 prior、綁定單一 config、以及「沒有狀態檔 = 沒有接管」的 fail-closed;受管鍵與契約鍵是兩組(prune 會寫會還原但改了不算漂移),新增受管鍵不得讓舊狀態檔失效",
        (
            "test_combining_two_models_keeps_the_single_model_relationships",
            "test_save_state_is_owner_only_and_atomic",
            "test_save_state_refuses_a_symlink_target",
            "test_save_state_refuses_a_symlinked_state_directory",
            "test_load_state_is_fail_closed",
            "test_load_state_refuses_a_world_readable_state_file",
            "test_state_with_a_tampered_prior_is_rejected",
            "test_state_from_another_config_is_refused",
            "test_native_leaves_values_the_user_changed_after_takeover",
            "test_ownership_is_json_type_strict",
            "test_state_digest_survives_an_integral_float_in_prior",
            "test_config_identity_needs_both_hashes",
            "test_a_moved_repo_converges_to_exactly_one_plugin_entry",
            "test_native_keeps_a_pre_existing_plugin_without_calling_it_drift",
            "test_a_same_named_plugin_we_never_registered_is_not_hijacked",
            "test_a_replaced_entry_is_not_hijacked_even_after_we_registered_once",
            "test_switching_to_native_after_a_repo_move_removes_the_old_entry",
            "test_a_native_baseline_is_recomputed_from_the_current_config",
            "test_prune_is_taken_over_and_restored_but_never_called_drift",
            "test_native_leaves_a_prune_value_the_user_set_before_takeover",
            "test_unmanaged_keys_names_what_an_older_state_file_never_took_over",
            "test_state_refuses_values_the_two_languages_serialise_differently",
            "test_a_pre_existing_plugin_is_not_claimed_by_a_repo_move",
            "test_an_entry_we_added_after_a_move_is_still_ours_at_native",
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
            "test_live_catalog_is_bounded_typed_and_ordered",
            "test_default_budget_tracks_n_ctx",
            "test_rejects_empty_root",
            "test_rejects_root_slash",
            "test_rejects_home",
            "test_mcp_server_still_wires_up_root_validation",
            "test_mcp_server_rejects_root_slash",
            "test_defaults_keep_patch_and_run_command_on",
            "test_explicit_patch_zero_disables_patch",
            "test_explicit_run_tests_zero_disables_run_command",
            "test_build_commands_opt_in",
            "test_git_tools_outside_a_repo_return_a_skip_notice_not_a_retryable_error",
            "test_a_broken_git_environment_is_not_reported_as_a_missing_repo",
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
            "test_the_client_never_accepts_the_opencode_tool_prefix",
            "test_the_catalog_contract_pins_order",
            "test_a_server_with_a_drifted_catalog_is_refused_at_startup",
            "test_only_the_text_blocks_reach_the_model",
            "test_a_live_roundtrip_exposes_the_real_catalog",
            "test_one_engine_process_keeps_exactly_one_mcp_instance",
            "test_closing_a_shared_client_finishes_before_a_replacement_starts",
        ),
    ),
    "test_client_engine.py": (
        "送出去的那一份才算數:reasoning 剝除只動 reasoning 欄位、只丟最新真實使用者訊息之前的、"
        "認不出那則訊息就整段不動;prune 只改 payload,session 檔與畫面保留原文;"
        "懸空 tool_call 必須在送出前補齊;權限 policy 的 readonly 全 deny(判準是 readOnlyHint 不是名單)、"
        "互動的六個 ask 沒核准就不得執行且重問有上限、核准框完整顯示參數;"
        "只有工具結果的 text block 進模型;ingest marker 只認 ingest_document 的行首;"
        "假工具呼叫偵測不得把否定句算成宣稱;基底規則守 1,600 字元;閘對轉換後的 payload 計數",
        (
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
            "test_interactive_policy_asks_for_the_six_write_tools",
            "test_a_denied_ask_never_reaches_the_mcp_server",
            "test_a_repeatedly_denied_tool_stops_asking_the_user",
            "test_the_approval_box_shows_every_argument_in_full",
            "test_only_the_text_block_is_fed_back_to_the_model",
            "test_the_loop_stops_instead_of_spinning",
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
        ),
    ),
    "test_client_cli.py": (
        "事件流是 canary / routing eval / session_eval replay 的共用介面:形狀與解析器只有一份、"
        "tool-calls 的 step 不算終止、thinking 不得進事件流;headless 預設 ephemeral;"
        "TUI 的核准框完整顯示參數,EOF 一律視為拒絕",
        (
            "test_a_provider_prefixed_model_is_normalised_like_the_wrapper",
            "test_a_global_session_reaches_the_attach_command",
            "test_a_completed_tool_event_is_recognised_by_the_shared_parser",
            "test_a_denied_or_failed_tool_is_not_a_completed_call",
            "test_a_tool_calls_step_is_not_terminal_but_stop_is",
            "test_reasoning_never_reaches_the_event_stream",
            "test_the_shared_parser_reads_our_own_stream",
            "test_headless_defaults_to_ephemeral",
            "test_the_approval_prompt_shows_the_whole_patch",
            "test_an_eof_at_the_approval_prompt_is_a_refusal",
            "test_a_readonly_run_never_writes_context_metrics_into_the_project",
            "test_changing_session_rebinds_without_consuming_the_stop_notice",
            "test_new_session_resets_store_error_through_the_tui",
            "test_a_failed_new_keeps_the_repl_and_the_current_session",
            "test_saving_history_never_writes_through_a_hard_link",
            "test_saving_history_never_follows_a_symlink",
            "test_loading_history_never_reads_through_a_symlink",
        ),
    ),
    "test_client_store.py": (
        "session 檔逐字含 NDA 內容:必須落在 state 目錄而不是被分析的 repo(相對 XDG_STATE_HOME 與"
        "專案內的 state 目錄都要擋),目錄 0700、檔案 0600、讀寫兩端都拒 symlink 與 hard link,"
        "append 不得建出沒有 header 的檔,header 必須綁這個專案與這個 session,"
        "headless 的 ephemeral store 一個 byte 都不寫",
        (
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
            "test_the_whole_compaction_is_one_turn_including_the_preflight",
            "test_a_cancel_accepted_before_an_invalid_summary_is_judged_does_not_stop_compaction",
            "test_a_cancel_accepted_before_a_summary_error_does_not_stop_compaction",
            "test_a_cancel_before_the_commit_point_leaves_the_history_untouched",
            "test_a_cancel_after_the_commit_point_is_refused_and_the_summary_lands",
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
            "test_a_failed_summary_request_stops_without_touching_the_history",
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
    "test_client_web.py": (
        "web 的存取邊界:沒有密碼就不准離開 loopback(而且是 server 自己擋)、Tailscale 例外要"
        "env／CIDR／CLI 三方一致、每個端點都要密碼、cookie 不是密碼本身、密碼不得進 log、"
        "比對是常數時間(非 ASCII 密碼也要能登入,不是 500);approval 沒回答就是拒絕、"
        "只能回答一次、非 bool 不算核准;跨站頁面與 text/plain simple POST 不得驅動這個 "
        "server;訂閱之前的事件要重播、失敗也要送終結事件、同一 session 不得並行;"
        "resume 不得留下孤兒 session;mDNS 不得廣播 loopback",
        (
            "test_a_cancel_arriving_while_the_turn_is_being_started_is_not_lost",
            "test_the_slow_mcp_cancel_does_not_hold_the_app_lock",
            "test_a_web_started_with_a_session_resumes_it_by_default",
            "test_a_cancel_during_the_manual_compaction_preflight_is_consumed_not_lost",
            "test_a_cancel_after_the_answer_is_committed_but_before_compaction_is_refused",
            "test_a_cancel_accepted_during_compaction_ends_with_a_cancelled_terminal",
            "test_a_manual_compaction_can_be_cancelled_from_the_api",
            "test_a_cancel_that_races_the_end_of_the_turn_never_poisons_the_next_one",
            "test_a_browser_reconnect_prefers_last_event_id_over_the_url_cursor",
            "test_a_cors_front_end_can_log_in_and_stream_with_the_returned_token",
            "test_loopback_without_a_password_is_allowed",
            "test_a_non_loopback_bind_without_a_password_is_refused",
            "test_mdns_on_loopback_still_needs_a_password",
            "test_env_alone_cannot_fake_the_tailscale_exception",
            "test_a_lan_address_is_never_accepted_as_tailscale",
            "test_a_verified_tailscale_address_is_the_documented_exception",
            "test_mdns_has_no_tailscale_exception",
            "test_every_endpoint_requires_the_password",
            "test_the_login_cookie_is_httponly_and_not_the_password",
            "test_the_password_never_reaches_the_log",
            "test_password_comparison_is_constant_time",
            "test_an_unanswered_approval_is_a_refusal",
            "test_an_unknown_approval_id_is_rejected",
            "test_an_approval_can_only_be_answered_once",
            "test_a_non_boolean_granted_is_never_an_approval",
            "test_a_cross_origin_page_cannot_drive_the_server",
            "test_a_text_plain_simple_post_is_refused",
            "test_a_non_ascii_password_can_log_in",
            "test_events_published_before_the_subscription_are_replayed",
            "test_a_turn_failure_still_sends_a_terminal_event",
            "test_two_concurrent_turns_on_one_session_are_refused",
            "test_the_turn_notices_reach_the_client",
            "test_resuming_never_leaves_an_orphan_session_behind",
            "test_an_unknown_session_id_creates_nothing",
            "test_broadcasting_a_loopback_address_is_refused",
            "test_the_web_password_never_reaches_the_mcp_child",
            "test_the_streamed_deltas_never_duplicate_the_turn_text",
            "test_manual_compaction_is_reachable_from_the_web_api",
            "test_auto_compaction_runs_after_a_web_turn",
            "test_attach_never_uses_an_environment_proxy",
            "test_attach_refuses_a_cross_host_redirect",
            "test_a_running_turn_can_be_cancelled_from_the_api",
            "test_notices_are_delivered_before_the_terminal_event",
            "test_a_turn_cursor_lets_the_next_subscription_skip_old_events",
            "test_cancel_wakes_a_turn_waiting_for_approval",
            "test_cancel_counts_even_before_the_worker_enters_send",
            "test_auto_compaction_only_runs_after_a_completed_answer",
            "test_the_durable_stop_notice_is_published_before_the_turn_starts",
            "test_resuming_never_creates_a_session_file_first",
            "test_a_rebound_host_header_is_refused_even_when_origin_matches",
            "test_attach_follows_a_turn_from_its_cursor",
            "test_cors_origins_are_an_explicit_allowlist",
            "test_the_message_api_returns_the_cursor",
            "test_host_aliases_cover_the_loopback_spellings",
            "test_a_cancel_during_the_compaction_phase_reaches_the_engine_and_ends_with_the_turn",
            "test_an_approval_registered_after_the_cancel_is_refused_immediately",
            "test_wildcard_binds_do_not_restrict_the_host_header",
            "test_a_password_protected_wildcard_bind_accepts_the_real_lan_host",
            "test_sse_events_carry_ids_and_reconnects_resume_from_last_event_id",
            "test_sse_carries_cors_headers_for_an_allowed_origin",
            "test_the_browser_resume_api_loads_the_session_and_returns_a_cursor",
            "test_a_store_error_on_resume_is_a_404_not_a_traceback",
            "test_the_stop_notice_is_not_duplicated_in_the_post_body",
        ),
    ),
    "test_opencode_migrate.py": (
        "唯一會寫使用者 OpenCode 設定的路徑:只還原現值仍等於 CodeTrail 寫入值的鍵、"
        "只移除 path 對得上的 plugin 項、mcp.codetrail 與 permission 不動、"
        "沒有狀態檔也沒有我們的 plugin 項的機器零寫入、狀態檔在設定寫成功之後才刪、"
        "--check 不寫任何東西",
        (
            "test_a_machine_that_never_took_over_is_untouched",
            "test_a_machine_without_any_opencode_config_is_untouched",
            "test_only_values_we_still_own_are_restored",
            "test_a_value_the_user_changed_after_takeover_is_left_alone",
            "test_only_our_plugin_entries_are_removed",
            "test_mcp_and_permission_are_never_touched",
            "test_a_backup_is_left_behind",
            "test_the_state_file_is_deleted_only_after_the_config_was_written",
            "test_check_mode_writes_nothing",
            "test_a_state_file_for_another_config_is_refused",
            "test_a_leftover_compaction_plugin_alone_triggers_the_migration",
            "test_a_same_named_plugin_from_elsewhere_is_still_not_ours",
            "test_the_custom_config_path_is_honoured",
            "test_the_config_keeps_its_permissions",
            "test_a_state_file_that_cannot_be_removed_is_fail_loud",
            "test_an_unreadable_opencode_config_is_a_problem_not_a_no",
            "test_an_untrusted_ownership_state_file_is_a_problem_not_a_no",
        ),
    ),
    "test_deployment.py": (
        "主模型解析鏈:opencode.json 已經不在鏈上——沿用那份設定裡的模型等於「使用者以為"
        "在跑 A、實際在跑 B」,而一份壞掉的殘留檔更不得讓沒在用 OpenCode 的機器無法啟動",
        (
            "test_opencode_json_is_no_longer_a_model_source",
            "test_a_broken_opencode_json_never_blocks_startup",
            "test_a_conflicting_opencode_json_is_not_a_conflict_any_more",
        ),
    ),
    "test_server_scripts.py": (
        "aicode_web 的 tmux 指令列不得帶任何密碼(新的與升級機器殘留的舊 OpenCode 變數都算)",
        (
            "test_aicode_web_never_exports_legacy_opencode_secrets_into_the_launch_line",
        ),
    ),
    "test_repo_consistency.py": (
        "使用者文件不得再教去 OpenCode 化之後不存在的旗標 / 檔案 / 腳本",
        (
            "test_the_stub_gate_rejects_initialisation_side_effects",
            "test_the_js_comment_stripper_is_lexically_aware",
            "test_user_docs_must_not_teach_removed_flags_or_files",
            "test_the_opencode_plugin_stubs_are_inert",
            "test_current_cli_help_never_mentions_opencode",
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
    "test_figure_retrieval.py": (
        "review_figures 的文件身分逐位元組比對(通知給的建議命令用的是同一個身分)",
        (
            "test_document_identity_is_matched_byte_for_byte",
            "test_extraction_failure_is_stated_affirmatively",
            "test_docs_never_claim_a_single_bad_figure_blocks_the_whole_document",
        ),
    ),
    "test_context_budget.py": (
        "knowledge.py 的主模型 prompt 一律過 context gate(超長會被 server 從前面靜默截掉);"
        "客戶端的閘必須對轉換後實際送出的 payload 計數(含 assistant reasoning / reasoning_content),"
        "保留額必須等於本次 request 實送的 max_tokens",
        (
            "test_knowledge_has_exactly_one_ungated_completion_entry",
            "test_gated_completion_refuses_overflow_without_calling_the_server",
            "test_a_payload_with_reasoning_estimates_higher_than_one_without",
            "test_both_reasoning_field_names_are_counted",
            "test_stripping_reasoning_lowers_the_estimate_again",
            "test_the_reserve_is_this_request_max_tokens",
            "test_an_omitted_reserve_still_uses_the_internal_default",
        ),
    ),
    "test_evals.py": (
        "私人 session eval 不得把歷史模型回答當 oracle、不得經 symlink 外洩 NDA、匯出的角色形狀要與"
        "validator 一致;replay 的壓縮語意由 eval 自己釘(不讀使用者的 client.json)、推不出門檻要"
        "fail-loud、多輪必須真的接得起來而單輪不落檔、gitignore 掉的三個路徑一樣算 project state;"
        "timeout/checkpoint 不得丟資料且匿名 A/B 不得洩漏模型身分",
        (
            "test_every_direct_model_request_in_the_routing_eval_uses_the_normalised_model",
            "test_session_eval_accepts_bare_gguf_and_legacy_models",
            "test_routing_probes_accept_the_same_model_forms_as_aicode",
            "test_the_routing_client_attempt_sends_the_requested_model",
            "test_mined_draft_excludes_assistant_text_and_raw_session_id",
            "test_suite_rejects_historical_model_answer_as_oracle",
            "test_the_store_export_uses_the_role_shape_the_validator_reads",
            "test_private_writer_refuses_symlink_target",
            "test_private_writer_uses_owner_only_modes",
            "test_blind_bundle_hides_candidate_identity",
            "test_opencode_timeout_is_a_scored_case_failure_not_a_suite_abort",
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
