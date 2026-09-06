# handoff D1(W3):三條靜態 gate、T-0、smoke manifest 與 AGENTS.md

只動 `plan-final.md` §3 的 **D1** 那一列(`tests/test_repo_consistency.py`、
`tests/test_smoke_gate.py`、`tests/test_aicode.py`、`AGENTS.md`)與本檔。
沒有 commit / push / stash / checkout;**沒有執行任何 pytest**(不 full、不 smoke、
不 collect-only、不單跑既有測試、不 `--lf`、不呼叫任何測試函式);沒有碰真服務、tmux、
`~/.config/*`、真實 session、llama-server、GPU。

`Tests: smoke only — reviewer owns full execution.`(這一輪連 smoke 都不是我跑,
交付前那唯一一次由編排者執行。)

先讀完 `handoff-S.md` / `handoff-S-red.md` / `handoff-B.md` / `handoff-C1.md` /
`handoff-C2.md` / `handoff-C3.md` / `handoff-D2.md`、`execution-notes.md`、
編排者的 `/tmp/codetrail-session-opencode-cleanup-20260906/test-changes-W2.json`,
以及六個 owner 的**實際 diff**(不只 handoff)才施工;下面每個 allowlist / manifest
條目都是靜態掃過**現在的工作樹**得出來的,不是照抄計畫。

---

## 1. 落檔的介面

### 1.1 T-0:`_handoff_markdown`(`tests/test_repo_consistency.py`,同 §2 B-06)

```python
def _handoff_markdown(rel: Path) -> bool
#   True 只給 docs/workflows/<任務>/*.md;`docs/workflows/p.md`(少一層)為 False
#   消費者恰好兩個:_opencode_gate_sources() 與 test_user_docs_never_teach_… 的 .md 來源
```

`_walk_files` / `_repo_sources()` / `_iter_text_files()` **一行未改**(照 B-06)。
豁免只給 `.md` 的**內容**:同目錄的 `.py` / `.sh` / `.json` / `.toml` 照掃,由新的
T-0 測試靜態釘住(否則就是「把可執行檔藏進交接目錄」)。

### 1.2 OpenCode gate(§6.1 收緊)

| 符號 | 動作 |
|---|---|
| `_OPENCODE_ALLOWLIST` | 20 → **11**,逐條照 §6.1;刪掉 9 筆(`opencode_migrate.py`、兩個 `opencode_plugins/*.js`、`scripts/doctor.py`、`scripts/set_config.py`、`docs/setup.md`、`docs/compaction-rules.md`、`docs/mcp-tools.md`、`docs/opencode-agents-template.md`) |
| `_OPENCODE_WHOLE_FILE` | **整個刪除**(它只服務被刪掉的遷移工具與兩個 stub);這一代沒有任何整檔豁免 |
| `_OPENCODE_SHAPE_EXEMPTIONS` | 2 → **4**:`process_env.py: OPENCODE_`、兩個 eval 腳本 `opencode_effective_chars\|"era"`、`scripts/check_readme_consistency.py: opencode-ai\|npm\|opencode_migrate\|OPENCODE_`(D2 的 (b)) |
| `_OPENCODE_MIGRATE_COMMAND` / `_OPENCODE_UPGRADE_ANCHOR` | **新增**:`.md` 的 `^\s*(?:[$>]\s*)?python3\s+opencode_migrate\.py` 形狀,除非同一 logical line 或**所在段落標題**含 `a1682d5` |
| `_opencode_offenders` | 掃**全文**(含註解與 docstring;不再過 `_code_only_source`、不再切掉 `#` 之後);`.md` 分支改用 `_doc_logical_lines`(標題只認 fence 外的 `#`,所以 fence 內的 bash 註解不會被誤當標題 —— D2 的 (d)) |
| `_opencode_gate_sources()` | 改成 generator,跳過 `_handoff_markdown` 的 `.md` |

### 1.3 environ / spawn gate(§6.1 收緊)

| 符號 | 動作 |
|---|---|
| `_ENVIRON_ALLOWLIST` | 46 → **26**,現在**逐檔等於實際讀取者**(靜態核對:stale 0、missing 0) |
| `test_no_module_reads_codetrail_settings_from_the_environment` 內的 `core` 集合 | **整個刪除**;第二段(`os.environ.get("AICODE_…")`)與第三段(index access)改掃**全部** `_repo_sources()`,沒有任何檔案有豁免 |
| `_SPAWN_CORE` | 9 → **4**(`deployment_profile.py`、`scripts/run_tests.py`、`eval/run_eval.py`、`eval/record_semantic_vectors.py`);刪掉 `deployment_status.py`、`scripts/launch_servers.py`、`stop_servers.py`、`check_status.py`、`set_config.py` |
| `test_the_only_llama_server_exec_hands_over_the_stripped_environment` | **新增**(靜態 AST):`deployment_profile.py` 全檔只有一個 `os.exec*`,而且是 `os.execvpe(command[0], command, process_env.llama_server_env())` |

### 1.4 docs gate(§6.1 收緊)

`_DOC_CORE_VARIABLES`、`_DOC_CORE_SECTIONS`、`_LAUNCHER_SCRIPTS`、`_DOC_LAUNCHER_COMMAND`
**四個常數與 `_doc_offenders` 內對應的分支整組刪除**(§6.1 只列前三個;第四個是前三個的
唯一消費者,留著就是 dead code)。`_DOC_ALLOWED_TOKENS` 六個概念名 / 協定標記**未動**。
`test_model_facing_text_never_teaches_a_removed_environment_knob` 的 `core_only` 只剩
`scripts/check_readme_consistency.py`;`allowed` 只剩 `AICODE_ROOT` 與四個
`CODETRAIL_INGEST*` / `CODETRAIL_ACTION_REQUIRED` / `CODETRAIL_ZERO_WRITE` 標記。

### 1.5 介面偏差(3 項,計畫沒列到這個層級;下游 / Astra 請以本節為準)

1. **兩條新 smoke node 的名字是我取的**。§6.1 指定了兩項檢查但沒有給 node 名:
   `test_the_migration_command_only_survives_in_the_pinned_upgrade_section`(`.md` 的
   `a1682d5` 形狀規則)與 `test_the_only_llama_server_exec_hands_over_the_stripped_environment`
   (`execvpe` 的 env 參數)。兩條都已登記進 `SAFETY_MODULES["test_repo_consistency.py"]`。
2. **多改一個既有 node 名**:`test_the_docs_gate_catches_bare_mentions_and_scopes_core_names_to_their_sections`
   → `test_the_docs_gate_catches_bare_mentions_and_env_prefixes_everywhere`。理由與 B / C1 / C3
   的改名一致:段落白名單已刪除,名字裡的「scopes core names to their sections」描述一條
   不存在的執行路徑。`SAFETY_MODULES` 同步改。
3. **`_ENVIRON_ALLOWLIST` 比計畫多刪 2 筆**:`root_safety.py` 與 `scripts/eval_tool_routing.py`。
   `inventory.md` C.4 把它們列在「有讀取」那一組,但**在 `a1682d5` 就已經是零讀取**
   (`git show a1682d5:<檔> | grep -c "os\.environ\|getenv"` 兩者都是 0),與那 10 筆 stale
   豁免同性質。同理 `client_mcp.py` 與 `scripts/mcp_catalog.py`(inventory 交給 D1 判定)也
   移除:兩檔各只剩一行**註解**提到 `os.environ`,`_code_only` 看不到它。

---

## 2. test-changes

三個檔的 smoke 標記來源:`tests/test_aicode.py` 是 module 層 `pytestmark = pytest.mark.smoke`;
`tests/test_repo_consistency.py` 與 `tests/test_smoke_gate.py` 是逐條 decorator(未改)。

### 2.1 `tests/test_repo_consistency.py`

**新增(3 條,全部 `@pytest.mark.smoke` 單條 decorator)**

| 測試名 | 守什麼 / 行為為什麼該有這條 |
|---|---|
| `test_the_handoff_markdown_exemption_is_content_only` | T-0。`_handoff_markdown` 只對 `docs/workflows/**` 的 `.md` 為真(`…/tool.py`、`…/ci.yaml`、`…/settings.json`、`docs/p.md`、`docs/workflows/p.md`、`README.md` 全部為假);真實 repo 的 `_walk_files` **仍然列出**交接目錄的 `.md`;同目錄的 `.py` 照樣被 `_opencode_offenders` 與 `_spawn_offenders` 判為 offender。少了這條,把整個目錄從走訪剪掉的「省事寫法」不會被擋 —— 那等於開一條「把可執行檔藏進交接目錄」的後門 |
| `test_the_migration_command_only_survives_in_the_pinned_upgrade_section` | 本版不附帶 `opencode_migrate.py`。允許它出現在文件裡的條件是**指名 `a1682d5`**(行內或段落標題),不是「這份文件叫 troubleshooting」。少了這條,任何 allowlist 文件都能貼一行 `python3 opencode_migrate.py`,使用者照抄只會得到 `No such file or directory`;而 `.md` 的 teach 判準(`OPENCODE_*` / 行首 `opencode ` / `npm install -g opencode`)本來就抓不到這個形狀 |
| `test_the_only_llama_server_exec_hands_over_the_stripped_environment` | `_SPAWN_CORE` 放行 `deployment_profile.py` 的 spawn API,這條是那個放行的對價:全檔唯一的 `os.exec*` 必須是 `execvpe(..., process_env.llama_server_env())`。改成 `os.environ` / `child_env()` 是**無聲**的回歸 —— server 起得來、status 也綠,只是跑在殼層 `CUDA_VISIBLE_DEVICES` 指的那張卡上 |

**刪除(3 條 node + 2 個 helper)**

| 名稱 | 動作 | 理由 |
|---|---|---|
| `test_the_opencode_plugin_stubs_are_inert` | 刪(smoke) | 它讀 `opencode_plugins/codetrail-*.js`,B 已整組刪除該目錄;留著是 `FileNotFoundError`,不是保護 |
| `test_the_stub_gate_rejects_initialisation_side_effects` | 刪(smoke) | 同上(它是前一條那個 gate 的自測) |
| `test_the_js_comment_stripper_is_lexically_aware` | 刪(smoke) | 同上(`_strip_js_comments` 只服務 stub gate) |
| `_assert_inert_stub`、`_strip_js_comments` | 刪(helper,非 node) | 隨上面三條一起,repo 內零引用 |
| 區塊註解「總審第 2 輪回修:JS stub 的靜態契約;…」 | 改字 | 只剩後半句成立 |

**改名(2 條)**

| 舊名 → 新名 | 行為為什麼該變 |
|---|---|
| `test_opencode_only_survives_in_the_migration_path_and_docs` → `test_the_removed_frontend_only_survives_in_the_strip_list_history_data_and_upgrade_docs` | 計畫 B-07 指定。「遷移路徑」在這一代不存在了,剩下的四個位置是剝除清單、eval 凍結資料的舊欄位名 / era 標記、反向檢查器的 pattern、文件升級段 |
| `test_the_docs_gate_catches_bare_mentions_and_scopes_core_names_to_their_sections` → `test_the_docs_gate_catches_bare_mentions_and_env_prefixes_everywhere` | 見 §1.5 偏差 2:段落白名單已刪,舊名字描述一條不存在的規則 |

**改斷言 / 改輸入(9 條)**

| 測試名 | 動作 | 行為為什麼該變 |
|---|---|---|
| `test_code_model_placeholder_contract_passes_with_llamacpp_setup` | 合成 README 由 `export AICODE_MODEL=<CODE_MODEL>` 改成 `deployment.json 的 main.model 填 <CODE_MODEL>`;docstring 同步 | D2 把 checker 第 5 條的判準從「README 有沒有提那個環境變數」換成「有沒有講主模型填在 `deployment.json` 的 `main.model`」。舊的合成內容現在會回一條 issue,而它應該回 |
| `test_code_model_placeholder_contract_reports_missing_bits` | `any("AICODE_MODEL" in issue …)` → `any("main.model" in issue …)`;docstring 同步 | 同上:issue 文字裡已經沒有那個變數名 |
| `test_user_docs_must_not_teach_removed_flags_or_files` | ① 真實文件那半 `checker._check_no_stale_client_docs(checker._documentation_text(), issues)` → `checker._check_stale_docs_per_file(issues)`;② 合成清單新增 5 條(`python3 opencode_migrate.py --check`、`export LLAMA_BIN=`、`export MODELS_DIR=`、`AICODE_TEST_JOBS=1 …`、`Environment=AICODE_MODEL=<CODE_MODEL>`);docstring 補說明 | ① 升級段**必須**點名舊 ownership 狀態檔與那支還原工具,而例外是**逐檔**的;整包合併只能整組放行或整組擋下,等於把 troubleshooting 的例外送給所有文件(D2 的 (a))。② 五個新 stale pattern 是 D2 這輪加的,合成反例是它們唯一的自測(合成字串不帶 `source=`,所以連例外過的兩條也照樣要被抓到) |
| `test_the_opencode_gate_checks_allowlisted_files_for_dependency_shapes` | 例子整組重寫:`scripts/set_config.py` → `process_env.py`(前者已不在 allowlist);新增「註解裡的 `.config/opencode/opencode.json` 也要抓」與「形狀例外只放行它自己那一種」;新增「同名檔重新出現不再有整檔豁免」(`opencode_migrate.py` / `opencode_plugins/codetrail-notify.js`) | allowlist 換人了,舊例子測的是一個不在名單上的檔;掃全文與刪掉 `_OPENCODE_WHOLE_FILE` 是本輪的兩個行為變更,各需要一個會穿過去的反例 |
| `test_the_docs_gate_catches_bare_mentions_and_env_prefixes_everywhere` | 1 條由 `assert not` 翻成 `assert`(核心段落 + `python3 scripts/launch_servers.py`);新增 1 條(`README_DEV.md` 的 `AICODE_TEST_JOBS=1 python3 scripts/run_tests.py`) | `~/start.sh` 不再 export、loader 只讀 `deployment.json` 與 argv,所以「核心段落 + 啟動核心命令」不再是契約;並行度改吃 `--jobs`,那一行是現成的回歸來源 |
| `test_the_gates_also_catch_config_files_and_env_prefixed_commands` | 3 條由 `assert not` 翻成 `assert`(`AICODE_MODEL=<X> ~/start.sh`、`AICODE_MODEL=<X> \ MAIN_GPU=0 \ python3 scripts/launch_servers.py`、`docs/setup.md` 的 `Environment=AICODE_MODEL=<CODE_MODEL>`);docstring 同步 | 三者都是「照做之後不會生效、也不會報錯」:start.sh 只轉發 `"$@"`、launcher 只讀檔案與 argv、systemd 的 `Environment=` 對 loader 沒有作用 |
| `test_the_gates_also_catch_ignore_entries_and_bare_assignments` | 1 條翻成 `assert`(核心段落裡 `AICODE_MODEL=<X>` 單獨一行後接 launcher);docstring 同步 | 同上 |
| `test_the_gates_are_structural_not_a_list_of_spellings` | 1 條翻成 `assert`(`### 4.1` 段落裡的 `echo $AICODE_MODEL`) | 同上;`$VAR` 讀取在哪一節都是在教一個不存在的東西 |
| `test_no_module_reads_codetrail_settings_from_the_environment` | 刪掉函式內的 `core` 集合;第二段與第三段改掃全部 `_repo_sources()`;docstring 同步 | 啟動核心不再讀任何 `AICODE_*`(靜態掃過:全 repo 非測試 `.py` 命中 0),豁免留著就是替未來的回歸開門 |
| `test_user_docs_never_teach_a_removed_environment_knob_or_interface` | `.md` 來源加 `_handoff_markdown` 過濾;docstring 補說明 | T-0;交接檔逐字引用被移除的變數名,當使用者文件掃是永遠紅燈(實測:不過濾的話這個目錄會產生 98 個 doc offender、204 個 opencode offender) |

**模組層常數 / helper(非 node,但 AST 對照會看到)**

新增 `_handoff_markdown`、`_OPENCODE_MIGRATE_COMMAND`、`_OPENCODE_UPGRADE_ANCHOR`;
改寫 `_OPENCODE_ALLOWLIST`(20→11)、`_OPENCODE_SHAPE_EXEMPTIONS`(2→4)、
`_ENVIRON_ALLOWLIST`(46→26)、`_SPAWN_CORE`(9→4)、`_opencode_offenders`、
`_opencode_gate_sources`、`_doc_offenders`;刪除 `_OPENCODE_WHOLE_FILE`、
`_DOC_CORE_VARIABLES`、`_DOC_CORE_SECTIONS`、`_LAUNCHER_SCRIPTS`、`_DOC_LAUNCHER_COMMAND`、
`_assert_inert_stub`、`_strip_js_comments`。
**import 區一行未改**(`_code_only_source` 仍被 `_code_only` 與 `_spawn_offenders` 使用)。

### 2.2 `tests/test_aicode.py`

| 測試名 | 動作 | 行為為什麼該變 |
|---|---|---|
| `test_aicode_never_execs_opencode` | **刪除**(module 層 `pytestmark` 使它原本是 smoke) | 計畫 §5.3 指定。它守的「正常路徑不得 exec 任何 opencode 二進位」已經被兩層蓋住:`test_the_only_exec_target_is_the_client_next_to_the_wrapper` 釘住唯一的 exec 目標,而收緊後的 OpenCode 內容 gate 也掃 `aicode` wrapper 全文(`test_the_opencode_gate_scans_config_files_and_the_wrapper` 靜態釘住它在掃描集合裡) |

其餘 14 條測試、module 層 `pytestmark`、fixture、import **一律未動**。

### 2.3 `tests/test_smoke_gate.py`

`SAFETY_MODULES` manifest:**31 個檔名鍵**(數量不變:刪 1 加 1)、node 由 **565 → 552**。
測試函式本體(`test_the_manifest_has_no_duplicate_file_keys`、`_test_functions`、
`test_safety_checkpoints_are_present_and_in_the_smoke_package`、
`test_every_registered_module_names_at_least_one_node`、`test_smoke_marker_is_registered`)
**一行未改**。

| 檔名鍵 | 動作 |
|---|---|
| `test_opencode_migrate.py` | **整個鍵刪除**(說明 + 33 個 node):被測模組與測試檔都不存在了 |
| `test_aicode.py` | 刪 node `test_aicode_never_execs_opencode`;說明末句改寫 |
| `test_compaction_formula.py` | 刪 node `test_combining_two_models_keeps_the_single_model_relationships`(B 刪了該測試) |
| `test_repo_consistency.py` | 刪 3 個 stub node;改名 1(gate)、改名 1(docs gate 自測);**新增 3**(見下);說明整段改寫成三條 gate 的現況 |
| `test_evals.py` | node 改名 `test_opencode_timeout_…` → `test_a_replay_timeout_is_a_scored_case_failure_not_a_suite_abort`;**新增** `test_the_effective_chars_field_survives_its_rename_across_the_frozen_data`;說明補一句 |
| `test_deployment.py` | 說明改寫(從「opencode.json 不在鏈上」擴成「啟動核心的設定只有檔案與 argv」);**新增 4** |
| `test_server_scripts.py` | **新增檔名鍵**(說明照 C3 的建議),3 個 node |
| `test_set_config.py` | **新增 2**;說明補 start.sh / deployment.json 那兩句 |
| `test_client_app.py` | **新增 9**;說明補重播 / 宣告群組配對 / busy / 失敗零改動 / `/new` / 選單 Esc |
| `test_client_engine.py` | **新增 2**;說明補 `load_session` / `adopt` / transcript 標記 |
| `test_client_store.py` | **新增 1**;說明補「大綱只取真實 user 訊息、零 LLM 零寫入」 |

**本輪登記進 `SAFETY_MODULES` 的新 smoke node 全名(25 條)**

```
tests/test_repo_consistency.py::test_the_handoff_markdown_exemption_is_content_only
tests/test_repo_consistency.py::test_the_migration_command_only_survives_in_the_pinned_upgrade_section
tests/test_repo_consistency.py::test_the_only_llama_server_exec_hands_over_the_stripped_environment
tests/test_deployment.py::test_the_loader_ignores_every_legacy_override_variable
tests/test_deployment.py::test_gpu_and_llama_bin_come_from_the_deployment_file_then_argv
tests/test_deployment.py::test_the_server_environment_strips_gpu_selectors_and_llama_settings
tests/test_deployment.py::test_the_main_model_resolver_has_no_environment_branch
tests/test_server_scripts.py::test_the_pane_runs_the_exec_choke_point_with_the_loader_argv
tests/test_server_scripts.py::test_the_exec_path_hands_llama_server_a_clean_environment
tests/test_server_scripts.py::test_stop_and_status_use_argv_and_constants_not_the_shell
tests/test_set_config.py::test_generated_start_sh_ignores_legacy_shell_overrides
tests/test_set_config.py::test_deployment_json_pins_llama_bin_and_gpus
tests/test_evals.py::test_the_effective_chars_field_survives_its_rename_across_the_frozen_data
tests/test_client_app.py::test_resume_replays_the_stored_history
tests/test_client_app.py::test_a_session_resumed_at_startup_is_shown_on_mount
tests/test_client_app.py::test_the_session_picker_lists_outlines_and_switches
tests/test_client_app.py::test_escape_and_ctrl_c_only_close_the_picker
tests/test_client_app.py::test_a_failed_switch_keeps_the_session_and_the_screen
tests/test_client_app.py::test_replay_pairs_tool_results_by_declaration_group_not_by_id
tests/test_client_app.py::test_replay_shows_pre_compaction_originals_and_a_summary_marker
tests/test_client_app.py::test_replayed_tool_blocks_are_not_registered_for_live_events
tests/test_client_app.py::test_new_clears_the_screen
tests/test_client_engine.py::test_load_session_leaves_the_engine_untouched_and_adopt_switches_atomically
tests/test_client_engine.py::test_the_snapshot_model_history_is_compacted_while_the_transcript_keeps_the_originals
tests/test_client_store.py::test_the_outline_is_the_first_real_question_never_the_summary_or_tool_output
```

改名後登記的 node(2 條,不是新增):
`tests/test_repo_consistency.py::test_the_removed_frontend_only_survives_in_the_strip_list_history_data_and_upgrade_docs`、
`tests/test_repo_consistency.py::test_the_docs_gate_catches_bare_mentions_and_env_prefixes_everywhere`;
以及 `tests/test_evals.py::test_a_replay_timeout_is_a_scored_case_failure_not_a_suite_abort`。

**manifest 的靜態核對(不是執行測試)**:用獨立寫的 AST 腳本(只 `import ast`,
不 import 本 repo 任何模組、不呼叫本 repo 任何函式)複刻 `_test_functions()` 的
判定(module 層 `pytestmark` + 單條 decorator 都算),對 552 個 node 逐一核對:
**檔案存在 31/31、node missing 0、unmarked 0、重複檔名鍵 0、重複 node 0**,
`pyproject.toml` 的 `smoke` marker 仍登記。

### 2.4 `AGENTS.md`(不是測試,但屬本節的「動到的東西」)

§2 刪三條(`opencode_migrate`、它的 ownership 狀態檔、`opencode_plugins/*.js` stub 契約);
新增兩條(「啟動核心的設定來源」、「三條靜態 gate」);`compaction_formula` 條改常數名並
刪掉「不得 import `opencode_migrate`」;`config.CLIENT_MAX_OUTPUT_TOKENS` 條的
`UPSTREAM_OUTPUT_TOKEN_MAX` → `OUTPUT_TOKEN_MAX`;「兩份安裝並存」條末句改寫;
`client_engine` / `client_app` / `client_store` 三條各補 S 落檔的契約。
§3 的「不要新增 `os.environ` 讀取」刪掉啟動核心的例外句,允許清單改成
`HOME` / `XDG_*` / `PATH` / 寫入 `PYTHONIOENCODING` / 寫入 `PYTEST_*`。
§1.1 補一句 `--jobs N` 是唯一不轉發給 pytest 的旗標(C3 的第 7 點交給 D1 判斷;
不補的話那一句在 `AICODE_TEST_JOBS` 刪除之後就是錯的)。

---

## 3. 給其他 owner 的字

### 給 S(`codetrail_chat.py`)——**唯一一個我不能自己修的紅燈**

`codetrail_chat.py:86-92`(`_cli_model` 的 docstring)還在教 `AICODE_MODEL`:

```
87:    """`--model` 走跟 `aicode -m` / `AICODE_MODEL` 同一套正規化。
89:    wrapper 會把 `llamacpp/foo` 剝成 `foo` 再寫進 AICODE_MODEL;直接執行
```

收緊後的 `test_model_facing_text_never_teaches_a_removed_environment_knob`
(`allowed` 只剩概念名與協定標記)會把 **`:87` 這一處**判為 offender
(`:89` 那一處因為上下文有「以前」被既有的說明句規則放行)。這段敘述本身也已經不成立:
`aicode` wrapper 現在沒有 `-m` 這個旗標、也不寫任何環境變數
(`grep -n "AICODE_MODEL\|-m\b" aicode` 命中 0)。

建議改法(語意不變、只拿掉那個名字;請 S / 修復者確認):

```python
    """`--model` 走跟 wrapper 同一套正規化(`model_resolution.normalize_main_model`)。

    以前 wrapper 會把 `llamacpp/foo` 剝成 `foo` 再交給客戶端,而直接執行
    `codetrail_chat.py --model llamacpp/foo` 拿到的是 raw 字串,於是兩邊用了
    兩個不同的模型名稱。外部 provider(openai/ 等)一律拒絕:CodeTrail 只跑本地
    llama-server。
    """
```

**這是 D1 已知會紅的唯一一條**(見 §4 第 1 項);`plan-final.md` §6.1 明寫
「各 owner 改字,D1 不代改」,所以我沒有動 S 的檔。

### 給 C3(`tests/test_server_scripts.py`)

`test_aux_models_and_gpus_come_from_the_deployment_file`(你補位的那條)**沒有 smoke 標記**
(該檔沒有 module 層 `pytestmark`,那條也沒有單條 decorator),所以**沒有**登記進
`SAFETY_MODULES` —— 登記沒有 smoke 的 node 會讓整個 gate 紅。你 handoff 指定的三條
都已登記。要不要給它補一條 `@pytest.mark.smoke` 是你的檔的決定:它現在是
「三顆附屬模型 + 三張卡真的被解析出來」的唯一覆蓋,而被它取代的
`test_legacy_aux_launcher_env_names_still_override_profile` 本來也不在 manifest 裡,
所以**不補也不會讓 manifest 說謊**,只是那件事只在 full 裡跑。

`tests/test_test_runner.py` 依你的建議**沒有**新增檔名鍵(計畫沒要求)。

### 給編排者 / Astra(驗收條 §7 的靜態部分,本輪實測)

用**獨立複刻**的靜態腳本(只 `import ast` / `re` / `pathlib` / `subprocess` 的 `git ls-files`,
不 import 本 repo 任何模組)跑過收緊後的三條 gate 判準,對現在的工作樹:

| 判準 | 結果 |
|---|---|
| OpenCode gate(160 個文字檔,含 `pyproject.toml` / `aicode` / `.gitignore`;`docs/workflows/**/*.md` 內容豁免) | offender **0** |
| docs gate(`_DOC_CORE_*` / `_LAUNCHER_SCRIPTS` 清空後) | offender **0** |
| environ gate:allowlist ↔ 實際讀取者 | 26 ↔ 26,stale 0、missing 0 |
| environ gate:全 repo 非測試 `.py` 的 `os.environ.get("AICODE_…")` / index access | 命中 **0** |
| spawn gate(行 regex + AST alias / getattr / `env=` 三種),`_SPAWN_CORE` 只剩 4 檔 | offender **0** |
| 模型可見字串 gate(`core_only` 只剩 checker、`allowed` 只剩概念名) | offender **1**,就是上面給 S 的 `codetrail_chat.py:87` |
| `python3 scripts/check_readme_consistency.py` | **OK**(exit 0) |
| `python3 scripts/check_eval_consistency.py` | **OK**(exit 0) |
| `python3 -m compileall -q`(我改的三個 `.py`) | **OK** |
| 文件的 `python3` 契約(`*.md` + `docs/**/*.md` 的複刻掃描) | 0 offender |

`git ls-files \| grep -i opencode` 仍會列出 B 刪掉但尚未 stage 的四個路徑
(`opencode_migrate.py`、兩個 plugin、`tests/test_opencode_migrate.py`)——
`handoff-B.md` §4 第 2 項已註明,那是 stage 之前的預期狀態。

---

## 4. 未完成 / 疑點

1. **已知會紅的 1 條**:`tests/test_repo_consistency.py::test_model_facing_text_never_teaches_a_removed_environment_knob`
   —— offender 是 `codetrail_chat.py:87`(S 的檔,見 §3)。收緊 `allowed` 是 `plan-final.md`
   §6.1 明文指定的,而那句 docstring 教的機制確實不存在;修法在 §3,**我沒有越權改別人的檔**。
   這是本輪唯一一個我預期在交付前那次 smoke 會失敗的 node。
2. **本輪沒有執行任何測試**:三條新契約 smoke、所有改斷言 / 改名的既有測試都是「只寫不跑」。
   上面第 3 節那張表是**獨立複刻**的靜態掃描(不 import repo runtime、不呼叫 repo 函式、
   不用 pytest),只能證明「判準對現在的樹沒有 offender」,**不代表任何測試通過**。
   真正的判定是編排者交付前那唯一一次 `python3 scripts/run_tests.py -m smoke`。
3. **一併靜態核對過、但沒跑的兩條既有 smoke**:
   `test_current_cli_help_never_mentions_opencode`(它會 spawn 五個 `--help` 子行程,
   我沒有執行;靜態上五個檔的 `opencode` / `plugin` / `provider/model` 命中分別是
   1 / 0 / 1,而那兩處都在**函式 docstring** 裡、不進 argparse 的 `--help` 輸出,
   `scripts/set_config.py` 的模組 docstring 也已無 `native` / `plugin`)、
   `test_doctor_runs_as_a_script_from_the_repo_root`(同樣會 spawn,未執行)。
4. **`_OPENCODE_ALLOWLIST` 裡有兩筆目前是「零命中」的條目**:`README.md` 與
   `docs/security.md` —— D2 改完之後這兩份文件 `opencode` 出現 **0 次**(我複核過)。
   `plan-final.md` §6.1 明列它們,所以我**照計畫保留**(移除它們會更嚴,但那是改計畫)。
   要再收一格的話,刪這兩個鍵不影響現況,由 Astra / 使用者決定。
5. `AGENTS.md` §2 的 `client_policy` 條仍寫「互動模式的**六個** ask 工具」,而
   `client_policy.ASK_TOOLS` 是七個(`import_external_file` 是第七個),同一份檔的
   §3 與 `test_client_engine.py::test_interactive_policy_asks_for_the_seven_write_tools`
   都寫七。**這是 `a1682d5` 就存在的偏差、與本任務無關**(`docs/security.md:48-50` 有同一個
   偏差,`handoff-D2.md` §4 第 5 項也選擇不改),所以我沒有順手改 —— 兩個 owner 的處理
   一致,要修建議當獨立一件事。
6. `tests/test_repo_consistency.py` 的 `from scripts.check_readme_consistency import _config_int_constant`
   在 `a1682d5` 就已經沒有使用端(不是本輪造成的)。依 AGENTS.md §3「不要為了讓 lint 漂亮刪
   未檢查影響的 unused import」未動。
7. 交接目錄現在**只有 `.md`**,所以 T-0 測試裡「同目錄的 `.py` 照樣被抓」那兩條斷言餵的是
   合成路徑(`docs/workflows/x/tool.py`),不是真的檔案 —— 這是刻意的:它釘的是規則,
   不是現況。真實目錄那一半由 `_walk_files` 的斷言涵蓋。
8. **本輪跑過的命令(逐條揭露,對照編排者的串流核對)**:
   `python3 -m compileall -q <我的三個 .py>`、
   `python3 scripts/check_readme_consistency.py`(×3,全綠)、
   `python3 scripts/check_eval_consistency.py`(×2,全綠)、
   `git show a1682d5:<檔>` / `git diff` / `git status`(讀歷史,不執行 repo 的碼)、
   `grep` / `sed` 讀檔,以及**自己寫在 `/tmp/codetrail-session-opencode-cleanup-20260906/`
   之外的 `/tmp/d1_*.py`** 靜態腳本(`d1_scan` / `d1_docs` / `d1_oc` / `d1_nodes` /
   `d1_manifest` / `d1_model_text` / `d1_spawn_ast`)。那七個腳本**只 import
   `ast` / `re` / `os` / `sys` / `subprocess` / `pathlib`**,不 import 本 repo 的任何模組、
   不呼叫本 repo 的任何函式(gate 的判準是**重寫**一份,不是 import `tests/` 再呼叫它)。
   沒有跑 pytest / smoke / full / collect-only / `--lf`,沒有起任何服務、沒有讀 `~/.config/*`。
