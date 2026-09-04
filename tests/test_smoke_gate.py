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
        "wrapper 只做四件事:定位 checkout(自己可能是 symlink)、找 python3、檢查 argv 並拒絕"
        "沒有終端機的環境(指向 headless,不靜默降級)、exec 唯一的客戶端。使用者參數只有"
        "-c/--continue、--session <id>、-h/--help —— run / status / sessions 這些內部入口不跑"
        "preflight,轉發過去等於開一條略過 profile 驗證 / ctx 容量閘 / 工具健檢的第二入口。"
        "root 一律是 cwd;殼層裡殘留的 AICODE_* / AI_CODE_* / CODETRAIL_* 對它一律無效;"
        "缺 textual 要 fail-loud 印 pip 指令;正常路徑不得 exec 任何 opencode 二進位",
        (
            "test_the_only_exec_target_is_the_client_next_to_the_wrapper",
            "test_the_wrapper_follows_its_own_symlink_to_find_the_checkout",
            "test_the_sandbox_root_is_always_the_current_directory",
            "test_without_a_tty_the_wrapper_refuses_and_points_at_the_headless_entry",
            "test_a_missing_textual_fails_loud_with_the_pip_command",
            "test_a_polluted_shell_changes_nothing",
            "test_aicode_never_execs_opencode",
            "test_the_wrapper_stays_thin",
            "test_the_wrapper_never_reads_configuration_from_the_environment",
            "test_the_wrapper_accepts_only_the_three_user_flags",
            "test_the_wrapper_forwards_the_three_user_flags",
        ),
    ),
    "test_client_preflight.py": (
        "preflight 的交接:每一步交給 deployment_profile / model_resolution 的環境只有 HOME"
        "(殼層裡殘留的 AICODE_* 對這一次啟動一律無效——那是跨 branch 混用的真正機制);"
        "aux server 的硬閘與 canary 都問 profile 的 endpoint、驗的是 repo 裡那一份客戶端;"
        "canary 的時限 / TTL 是 repo 常數而快取位置只由 XDG_CACHE_HOME / HOME 推導;"
        "client.json 的位置沒有覆寫變數(它決定互動 session 的工具權限);"
        "transcript 收 stdout **與 stderr**(canary 的 WARNING 只走 stderr),"
        "而且一定帶壓縮狀態行(自動壓縮被停用是使用者唯一會看到的地方)",
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
            "test_every_profile_env_helper_has_the_same_home_only_shape",
            "test_the_canary_child_environment_is_stripped",
        ),
    ),
    "test_set_config.py": (
        "set_config 的壓縮模式:--yes 沒給旗標一律不接管、模式寫進 owner-only 的 client.json、"
        "門檻等於 runtime 用的同一條公式、算不出門檻要 fail-loud、dry-run 不留檔;"
        "restore transaction 的整批語意(一半還原比不還原更糟、manifest 壞掉不退回逐檔、"
        "沒有備份路徑不得刪 live 檔、symlink 被改指不得覆寫別處、寫不出 manifest 不得留 stale);"
        "restore manifest 兩個世代共用同一個檔:含這一代不會寫的目標時整份拒絕、一個檔都不動",
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
            "test_combining_two_models_keeps_the_single_model_relationships",
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
            "test_env_overrides_cannot_reintroduce_a_stripped_prefix",
            "test_process_env_run_is_the_only_spawn_exit_and_never_takes_env",
            "test_process_env_popen_class_is_not_a_raw_spawn_bypass",
            "test_client_config_and_skip_aux_preflight_reach_the_server_argv",
        ),
    ),
    "test_client_engine.py": (
        "送出去的那一份才算數:reasoning 剝除只動 reasoning 欄位、只丟最新真實使用者訊息之前的、"
        "認不出那則訊息就整段不動;prune 只改 payload,session 檔與畫面保留原文;"
        "懸空 tool_call 必須在送出前補齊;權限 policy 的 readonly 全 deny(判準是 readOnlyHint 不是名單)、"
        "互動的七個 ask 沒核准就不得執行且重問有上限、核准框完整顯示參數(`import_external_file` 也在裡面:那個開關授權的是能力,不是每一次的來源與目的);"
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
            "test_interactive_policy_asks_for_the_seven_write_tools",
            "test_external_import_is_gated_behind_a_per_call_approval",
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
            "test_the_import_approval_cannot_be_overridden_to_allow",
            "test_the_import_approval_shows_where_the_file_will_land",
            "test_fallback_tool_call_ids_are_unique_across_steps",
        ),
    ),
    "test_client_cli.py": (
        "事件流是 canary / routing eval / session_eval replay 的共用介面:形狀與解析器只有一份、"
        "tool-calls 的 step 不算終止、thinking 不得進事件流;headless 預設 ephemeral;"
        "readonly 連客戶端自己的 context metrics 也要關",
        (
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
        ),
    ),
    "test_client_turns.py": (
        "回合協調器:同一個對話一次只跑一輪;取消要涵蓋 engine 自己看不到的三個狀態"
        "(worker 還沒進 send()、阻塞在核准上、取消與收尾互相搶跑),閒置時的取消一律回 False;"
        "慢速的 MCP 取消不得扣住協調器的鎖;核准沒回答就是拒絕、只能回答一次、非 bool 不算核准;"
        "notice 要在終結事件之前送,失敗也要送終結事件;只有答完(finish=stop)才自動壓縮",
        (
            "test_cancel_wakes_a_turn_waiting_for_approval",
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
            "test_a_cancel_accepted_during_compaction_ends_with_a_cancelled_terminal",
            "test_a_manual_compaction_is_a_turn_and_can_be_cancelled",
            "test_a_session_change_rebinds_the_compactor_without_consuming_the_notice",
        ),
    ),
    "test_client_app.py": (
        "TUI:核准框完整顯示參數(含整份 patch)且可捲動、只認真的 bool;Esc / Ctrl-D 只拒絕"
        "那個工具、Ctrl-C 中斷整輪(核准框開著時也一樣、送出後立刻按也生效),閒置的 Ctrl-C "
        "不得顯示成「已中斷」,慢速 MCP 取消不得凍住畫面;回合進行中不得離開、不得換 session、"
        "被拒的第二題不得先貼進畫面;工具展開區要顯示未裁切的 structuredContent;沒有 tty 一律"
        "拒絕並指向 headless;輸入歷史逐字含 NDA 問題,正常退出一定要落檔、多行問題要能還原,"
        "讀寫兩端都拒 symlink(含中間目錄)與 hard link",
        (
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
            "test_a_ledger_written_before_the_upgrade_still_disables_that_session",
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
    "test_opencode_migrate.py": (
        "唯一會寫使用者 OpenCode 設定的路徑(而且只有使用者手動執行時才跑):"
        "只還原現值仍等於 CodeTrail 寫入值的鍵、只移除 path 對得上的 plugin 項、"
        "mcp.codetrail 與 permission 不動、沒有狀態檔也沒有我們的 plugin 項的機器零寫入、"
        "狀態檔在設定寫成功之後才刪、--check 不寫任何東西;"
        "**別份安裝的接管不動**(狀態檔記的 plugin 路徑還在且不是本 repo → 零寫入只提示),"
        "但本 repo 搬過家不得被誤判成別份安裝;"
        "ownership 狀態檔本身:owner-only 權限與 symlink 防線、digest 涵蓋 prior、綁定單一 config、"
        "「沒有狀態檔 = 沒有接管」的 fail-closed、受管鍵與契約鍵是兩組、plugin 項的認領與去重",
        (
            "test_a_machine_that_never_took_over_is_untouched",
            "test_only_values_we_still_own_are_restored",
            "test_a_same_named_plugin_from_elsewhere_is_still_not_ours",
            "test_mcp_and_permission_are_never_touched",
            "test_check_mode_writes_nothing",
            "test_a_state_file_that_cannot_be_removed_is_fail_loud",
            "test_another_installs_takeover_is_left_alone",
            "test_a_moved_repo_is_not_mistaken_for_another_install",
            "test_the_managed_values_come_from_the_formula",
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
            "test_an_unverifiable_foreign_takeover_is_left_alone",
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
    "test_repo_consistency.py": (
        "使用者文件不得再教去 OpenCode 化之後不存在的旗標 / 檔案 / 腳本",
        (
            "test_the_stub_gate_rejects_initialisation_side_effects",
            "test_the_js_comment_stripper_is_lexically_aware",
            "test_user_docs_must_not_teach_removed_flags_or_files",
            "test_the_opencode_plugin_stubs_are_inert",
            "test_current_cli_help_never_mentions_opencode",
            "test_opencode_only_survives_in_the_migration_path_and_docs",
            "test_no_module_reads_codetrail_settings_from_the_environment",
            "test_user_docs_never_teach_a_removed_environment_knob_or_interface",
            "test_model_facing_text_never_teaches_a_removed_environment_knob",
            "test_doctor_no_longer_needs_a_model_prefix",
            "test_the_docs_gate_catches_bare_mentions_and_scopes_core_names_to_their_sections",
            "test_the_opencode_gate_checks_allowlisted_files_for_dependency_shapes",
            "test_doctor_runs_as_a_script_from_the_repo_root",
            "test_the_spawn_gate_bans_the_spawn_api_outside_process_env",
            "test_the_production_spawn_gate_really_scans_the_repo",
            "test_the_spawn_gate_resolves_os_and_asyncio_aliases",
            "test_the_spawn_gate_closes_the_private_exits_and_keeps_every_alias",
            "test_the_gates_also_catch_config_files_and_env_prefixed_commands",
            "test_the_opencode_gate_scans_config_files_and_the_wrapper",
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
            "test_safe_figure_path_requires_root_to_be_the_sandbox_root",
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
        "保留額必須等於本次 request 實送的 max_tokens",
        (
            "test_knowledge_has_exactly_one_ungated_completion_entry",
            "test_gated_completion_refuses_overflow_without_calling_the_server",
            "test_a_payload_with_reasoning_estimates_higher_than_one_without",
            "test_both_reasoning_field_names_are_counted",
            "test_stripping_reasoning_lowers_the_estimate_again",
            "test_the_reserve_is_this_request_max_tokens",
            "test_an_omitted_reserve_still_uses_the_internal_default",
            "test_the_context_gate_has_no_off_switch",
        ),
    ),
    "test_evals.py": (
        "私人 session eval 不得把歷史模型回答當 oracle、不得經 symlink 外洩 NDA、匯出的角色形狀要與"
        "validator 一致;replay 的壓縮語意由 eval 自己釘(不讀使用者的 client.json)、推不出門檻要"
        "fail-loud、多輪必須真的接得起來而單輪不落檔、gitignore 掉的三個路徑一樣算 project state;"
        "timeout/checkpoint 不得丟資料且匿名 A/B 不得洩漏模型身分;"
        "data flywheel 的落點在 state 目錄(0700/0600、拒 symlink,被分析的 repo 零新檔),"
        "而且子行程的環境在交出去之前剝掉全部 CodeTrail 設定變數",
        (
            "test_every_direct_model_request_in_the_routing_eval_uses_the_normalised_model",
            "test_the_routing_eval_child_environment_is_stripped",
            "test_the_session_eval_candidate_model_goes_out_as_argv",
            "test_the_collected_data_never_lands_in_the_analysed_repo",
            "test_the_collected_data_file_is_never_read_or_rewritten_through_a_symlink",
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
