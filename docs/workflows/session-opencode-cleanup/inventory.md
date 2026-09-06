# 盤點:檔案 / 符號 / 環境值的歸宿(第 1 步附件)

- requested model: `claude-fable-5-1[1m]`,effort `max`(自述,非 API 身分證據;以 `model-log.md` 的 JSON metadata 為準)。
- 基準 `a1682d5`。行號是該 commit 的行號,施工時以符號名為準。
- owner 欄對應 `plan-initial.md` §4 的 task ID;`D1` = 整合者。

---

## A. 目標 a:session 選單與重播

### A.1 現況(讀碼證據)

| 位置 | 現況 | 結論 |
|---|---|---|
| `client_app.py:735-743` `_cmd_sessions` | 印 `id / turns / title`,title 永遠空 | 大綱不存在 |
| `client_app.py:745-760` `_cmd_resume` | `engine.resume` → `session_changed` → `_tools.clear()` → 一行 notice | **歷史不重播(bug)** |
| `client_app.py:445-461` `on_mount` | banner + help notice | 啟動 resume 也不重播(同一 bug) |
| `client_app.py:720-733` `_cmd_new` | 不清畫面 | 舊對話畫面留在新對話上方 |
| `client_app.py:558-578` `_tool_output` | 從 `engine.messages` 取 tool content + `structured` | 重播可直接重用 |
| `client_app.py:707-718` `_busy_notice` | `coordinator.busy` 時拒絕 | 重用 |
| `client_engine.py:632-658` `resume` | 正確、原子 | 不改 |
| `client_store.py:97-104, 347-380` | `SessionInfo` / `info()` / `list_sessions()` | 加欄位與純函式 |
| `codetrail_chat.py:176-184, 189-211, 378-383` | `_build` 先 resume;`--session ""` 拒絕;CLI `sessions` | CLI 加大綱 |
| `client_turns.py:379-389` `session_changed` | `compactor.rebind()` | 重用 |
| `tests/test_client_app.py` `_Store` / `_Engine` 替身 | `resume()` 只換 id | regression 用它 |

### A.2 新增 / 修改的符號

| 符號 | 檔 | owner | 說明 |
|---|---|---|---|
| `SessionInfo.first_prompt / last_prompt / messages / tool_calls / compactions` | `client_store.py` | A1 | I-1 |
| `OUTLINE_MAX_CHARS = 80`、`session_outline(records)` | `client_store.py` | A1 | 純函式 |
| `SessionStore.list_sessions(limit=None)` | `client_store.py` | A1 | 排序不變 |
| `command_sessions` 輸出格式 | `codetrail_chat.py` | A1 | `id\tturns=\tupdated=\tfirst_prompt` |
| `HistoryEntry`、`history_entries(messages)` | `client_app.py` | A2 | I-2,純函式 |
| `SummaryBlock(Collapsible)` | `client_app.py` | A2 | 壓縮摘要 |
| `SessionPickerScreen(ModalScreen[str \| None])` | `client_app.py` | A2 | `OptionList`;Esc 取消 |
| `CodeTrailApp._switch_session(session_id)`、`_replay_history(*, clear)`、`_cmd_session` | `client_app.py` | A2 | 三條路共用 |
| `COMMANDS` 加 `/session` | `client_app.py` | A2 | `/help` 仍第一 |
| `action_interrupt` / `action_leave` 先收選單 | `client_app.py` | A2 | |

---

## B. 目標 b:OpenCode 殘留逐檔清單(461 處 / 47 檔)

### B.1 刪除

| 路徑 | 內容 | owner |
|---|---|---|
| `opencode_migrate.py`(1,743 行) | 全部:`MODE_*` / `PLUGIN_*` / `MANAGED_COMPACTION_KEYS` / `CONTRACT_COMPACTION_KEYS` / `PRUNE_OLD_TOOL_OUTPUT` / `MIN_COMPACTION_OPENCODE_VERSION` / `json_equal` / `managed_values` / `compaction_model_limits` / `model_limit` / `derive_for_config` / 狀態檔 (`state_dir` … `clear_state`) / `owns` / `unmanaged_keys` / `apply_mode` / `_restore_native` / plugin entry 系列 / `effective_drift` / `opencode_config_candidates` / `MigrationPlan` / `plan_migration` / `apply_migration` / `_write_config` / `migration_problem` / `migration_needed` / `main`;`EXPERIMENTAL_*` / `is_experimental` / `mode_tag` 在本檔之外**沒有消費者**(`client_status.py:125`、`scripts/doctor.py:1211`、`scripts/set_config.py:1187-1188` 各自寫字面「🧪 實驗中」),一起刪 | B1 |
| `opencode_plugins/codetrail-compaction.js`、`opencode_plugins/codetrail-notify.js` | inert stub | B1 |
| `tests/test_opencode_migrate.py`(63 條) | 遷移契約 | B1 |
| `scripts/doctor.py:793-825` `check_legacy_opencode_install` + `:1312` 呼叫 | 唯一 import 遷移工具的 runtime | **C1**(doctor.py 整檔歸 C1) |
| `compaction_formula.py:301-354` `combine_settings` | 死碼(D-b3) | B1 |
| `compaction_formula.py:51-53` | 懸空註解 | B1 |
| `tests/test_repo_consistency.py:997-1170` 三條 stub 測試 + `_strip_js_comments` + `_assert_inert_stub` | | D1 |
| `tests/test_aicode.py:310-316` `test_aicode_never_execs_opencode` | 與唯一 exec 目標測試重複 | D1(同檔只此一處,交 D1 併 manifest 改) |
| `tests/test_compaction_formula.py` `test_combining_two_models_keeps_the_single_model_relationships` | | B1 |

### B.2 改名 / 改字但保留功能

| 路徑 | 現況 | 歸宿 | owner |
|---|---|---|---|
| `compaction_formula.py:33,37,39` `UPSTREAM_COMPACTION_BUFFER` / `UPSTREAM_OUTPUT_TOKEN_MAX` / `UPSTREAM_MIN_PRESERVE_RECENT_TOKENS` | 公式常數 | `COMPACTION_RESERVE_TOKENS` / `OUTPUT_TOKEN_MAX` / `MIN_PRESERVE_RECENT_TOKENS`;docstring 9-17、141-142、202-205、221-249 去掉 OpenCode / `overflow.ts` 引用,改「沿用 2026-09 定案公式」 | B1 |
| `client_compaction.py:94-98` | 綁 `UPSTREAM_OUTPUT_TOKEN_MAX` | 綁 `OUTPUT_TOKEN_MAX`;`:3` docstring「從舊前端的 JS plugin 搬進」改字 | B1 |
| `config.py:406` 註解 | 提 `compaction_formula.effective_max_output` | 常數名同步 | B1 |
| `tests/test_client_compaction.py:85` | `cm.UPSTREAM_OUTPUT_TOKEN_MAX` | 新名 | B1 |
| `tests/test_compaction_formula.py:17-18, 54-55` 與各 docstring | 指向遷移測試 | 刪引用 | B1 |
| `scripts/eval_tool_routing.py:1588` | 字面 tuple `("AICODE_","AI_CODE_","CODETRAIL_","OPENCODE_")` | `process_env.STRIPPED_ENV_PREFIXES` | C1(該檔歸 C1) |
| `scripts/eval_tool_routing.py:373, scripts/mcp_catalog.py:106-108,128,284` `opencode_effective_chars` | 歷史欄位名 | **保留**(D-b4);註解改「舊世代量測欄位」 | C1 / B1(`mcp_catalog.py` 歸 B1) |
| `eval/fixtures/tool_routing/support_matrix.json` | `era: "opencode"` 列與說明 | **不動** | — |
| `process_env.py:10, 33` | `OPENCODE_` 剝除 | **不動** | — |
| `tests/test_evals.py:1462` `test_opencode_timeout_is_a_scored_case_failure_not_a_suite_abort`、`:1494 cmd=["opencode","run"]`、`:237 test_model_probe_endpoint_must_match_effective_opencode_provider` | 名稱描述已不存在的執行路徑 | 改名 + fixture(§5.3) | B1 |

### B.3 只剩歷史說明的註解 / docstring(逐處改字為「舊世代前端」或刪句;gate 改掃全文後由它守)

| 檔:行 | owner |
|---|---|
| `model_resolution.py:6-12, 248, 265-267` | C1 |
| `client_mcp.py:260, 413` | B1 |
| `client_policy.py:77` | B1 |
| `context_budget.py:11`(「OpenCode TUI 直接打 llama-server,不會經過這裡」——已失真,客戶端有自己的 gate) | B1 |
| `mcp_server.py:34` | B1 |
| `mcp_lease.py:604` | B1 |
| `client_engine.py:47`(「上游 compaction.prune 的等價實作」) | B1 |
| `scripts/tool_call_canary.py:55, 253, 413, 519` | C1 |
| `scripts/session_eval.py:448` | C1 |
| `scripts/eval_tool_routing.py:1096, 1221, 1290, 1301, 1333, 1532, 1672` | C1 |
| `scripts/set_config.py:1167, 1874, 2306`(2306 是使用者看得到的字串:「OpenCode 設定則用 `python3 opencode_migrate.py`」→ 改成指向 troubleshooting 升級段) | C2 |
| `scripts/check_readme_consistency.py:14, 63-65, 152, 174-175, 196-206, 644, 759, 775-776, 856-860` | D1 |
| `scripts/doctor.py:739`(「不再需要 Node / npm / opencode-ai」) | C1 |
| `tests/_harness.py:81`、`tests/test_lessons.py:592-607, 651`、`tests/test_mcp_lease.py:98, 451, 849`、`tests/test_fs_sandbox.py:18`、`tests/test_run_command.py:13`、`tests/test_mcp_server.py:88, 94`、`tests/test_figure_retrieval.py:3375`、`tests/test_client_cli.py:225`、`tests/test_set_config.py:2035, 2180, 2383`、`tests/test_deployment.py:12, 698-702, 756` | 選做(tests/ 不在 gate 範圍;順手改字不列驗收) |

### B.4 測試 fixture 裡的 `OPENCODE_CONFIG` 與 `OPENCODE_API_KEY`

| 檔:行 | 處理 | owner |
|---|---|---|
| `tests/test_deployment.py:691` `delenv("OPENCODE_CONFIG")` | 刪 | C1(該檔整檔歸 C1,B1 交接檔註明) |
| `tests/test_doctor.py:69, 735` | 刪 | B1 |
| `tests/_set_config_harness.py:42` | 刪 | B1 |
| `tests/test_client_preflight.py:52` 污染字典的 `OPENCODE_CONFIG` | **保留**(它證明殘留變數無效) | — |
| `tests/test_client_mcp.py:451-522`、`tests/test_evals.py:1927-1930`、`tests/conftest.py:81` `OPENCODE_API_KEY` / 前綴 | **保留**(剝除契約) | — |
| `tests/test_set_config.py:2358` `.config/opencode/opencode.json` 當外來 restore 目標 | **保留**(只是例子) | — |

### B.5 文件

| 檔:行 | 現況 | 處理 | owner |
|---|---|---|---|
| `README.md:8` | 「不需要 Node / npm / opencode-ai」 | 「不需要 Node / npm」 | D1 |
| `README.md:85-97` | 升級 bullet 教 `opencode_migrate.py` | 一句指向 troubleshooting 升級段 | D1 |
| `README.md:146-154` §1.2 | 同上 | 同上 | D1 |
| `README.md:353-355` | runtime 不讀 `~/.config/opencode`;唯一會碰的是遷移工具 | 只留前半句 | D1 |
| `README.md:421-427` | 升級檢查含 `opencode_migrate.py --check` | 刪該行與說明 | D1 |
| `README.md:715` | 「那是 OpenCode 加的」 | 「舊世代前端加的」 | D1 |
| `README_DEV.md:20, 25-27, 114, 178-182` | 命令 / 測試檔 / 受管鍵 bullet | 刪 | D1 |
| `README_DEV.md:248, 337-342` | 歷史量測說明 | 保留(歷史) | — |
| `docs/troubleshooting.md:158-185` 與 `:623-650` | 兩段重複 | 合併成一段,內容依 plan §3.2 | D1 |
| `docs/security.md:81, 135` | | 改字 / 指向升級段 | D1 |
| `docs/basic-usage.md:47` | 歷史 row 說明 | 保留 | — |
| `AGENTS.md` | §2 三條 + `compaction_formula` 條 + 共用檔條 + §3 | 依 plan §6 | D1 |

### B.6 gate 現況 → 目標(`tests/test_repo_consistency.py`)

| 項目 | 現況 | 目標 |
|---|---|---|
| `_OPENCODE_ALLOWLIST`(1342-1370) | 21 筆,含不存在的 `docs/opencode-agents-template.md`、零命中的 `docs/compaction-rules.md` / `docs/setup.md` / `docs/mcp-tools.md` | `process_env.py`、`scripts/mcp_catalog.py`、`scripts/eval_tool_routing.py`、`eval/fixtures/tool_routing/support_matrix.json`、`README.md`、`README_DEV.md`、`docs/troubleshooting.md`、`docs/security.md`、`docs/basic-usage.md`、`AGENTS.md` |
| `_OPENCODE_WHOLE_FILE`(1382) | 三個檔整檔豁免 | 空 |
| `_OPENCODE_SHAPE_EXEMPTIONS`(1388) | doctor / check_readme | `process_env.py: OPENCODE_`;兩個 eval 腳本:`opencode_effective_chars\|"era"`;`scripts/check_readme_consistency.py: opencode-ai\|npm\|opencode_migrate`(它的字串就是抓這些的 pattern) |
| 掃描範圍 | `.py` 先剝註解 | 含註解全文(docs 除外) |
| `_ENVIRON_ALLOWLIST:1497`、`core_only:1940` | 含 `opencode_migrate.py` | 刪 |
| `_walk_files` | 不跳 `docs/workflows` | 以相對路徑剪掉(T-0) |

`tests/test_smoke_gate.py` 現有相關條目:`test_opencode_migrate.py`(565-609,刪)、`test_deployment.py`(610-618,保留三 node、改說明)、`test_repo_consistency.py`(619-650,刪三 node、改名一 node、加 T-0 node)、`test_evals.py:809`(改名)、`test_aicode.py:56`(刪)、`test_client_mcp.py:309`(保留)、`test_compaction_formula.py:139`(刪)。

---

## C. 目標 c:環境值逐項歸宿

### C.1 保留(檔案位置 / 行程介面 / 安全剝除)

| 名稱 | 讀取處(非 tests) | 用途 | 處理 |
|---|---|---|---|
| `HOME`(`USERPROFILE` 只在缺席時) | `config.py:26,31,338,676`、`client_config.py:158`、`client_store.py:73`、`client_prompt.py:106`、`client_compaction.py:255`、`client_status.py:99`、`codetrail_chat.py:77`、`mcp_server.py:195`、`mcp_lease.py:124`、`lessons.py:85`、`index_scope.py:279`、`external_import.py:33`、`deployment_profile.py:504,753`、`scripts/{doctor,session_eval,required_model_servers_check,tool_call_canary,index_stats,launch_servers,set_config}.py`、`scripts/session_eval.py:198,395`、`client_preflight.py:123,128` | 檔案位置 | 保留 |
| `XDG_STATE_HOME` | `client_store.py:64`、`client_compaction.py:254`、`mcp_lease.py:122`、`scripts/launch_servers.py:325`、start.sh `logs` | 檔案位置 | 保留 |
| `XDG_CACHE_HOME` | `scripts/tool_call_canary.py:531`(經 `resolve_cache_path(os.environ)`) | 檔案位置 | 保留 |
| `PATH` | `shutil.which`(tmux / objdump / llama-server) | 行程介面 | 保留 |
| `PYTHONIOENCODING`(寫入) | `mcp_server.py:122`、`eval/run_eval.py:25` | 行程介面 | 保留 |
| `PYTEST_DISABLE_PLUGIN_AUTOLOAD` / `PYTEST_DEBUG_TEMPROOT`(寫入) | `scripts/run_tests.py:817-838` | pytest 介面 | 保留 |
| `process_env.child_env()` 剝除 `AICODE_ / AI_CODE_ / CODETRAIL_ / OPENCODE_` | 所有 spawn | 安全清理 | **保留,不動** |
| `CUDA_VISIBLE_DEVICES`(**輸出**) | `deployment_profile.build_server_command:947` | 對 CUDA 的介面 | 保留輸出;**移除輸入**(C.2) |

### C.2 啟動核心:改檔案 / 常數 / argv

| 名稱 | 現在誰讀(行) | 現在誰寫 | 歸宿 | owner |
|---|---|---|---|---|
| `AICODE_MODEL` | `deployment_profile._ENV_FIELDS:546`;`model_resolution.py:244`;`scripts/launch_servers.py:71`(argv→env) | `set_config.py:1768,2508`(start.sh export / `_fresh_env`) | `deployment.json services.main.model`(已存在)+ `launch_servers --main-model`(`LauncherOverrides.main_model`);`model_resolution.resolve_main_model()` 刪分支;start.sh 不 export | C1 / C2 / C3 |
| `MAIN_GPU` `AUX_GPU` `EMBED_GPU` `RERANK_GPU` `VL_GPU` | `deployment_profile._gpu_for:667-678`、`RUNTIME_OVERRIDE_ENV_KEYS:595-599`;`launch_servers.py:72-76` | `set_config.py:1673-1682, 2558-2561` | `deployment.json services.<role>.gpu`(新鍵)+ `--<role>-gpu` / `--aux-gpu` argv(`LauncherOverrides.gpus`) | C1 / C2 / C3 |
| `CUDA_VISIBLE_DEVICES`(輸入) | `deployment_profile._gpu_for:675` | — | 刪讀取(D-c2) | C1 |
| `LLAMA_BIN` | `set_config.py:348`;`launch_servers.py:421`;`deployment_profile.py:1023`;`eval/record_semantic_vectors.py:121` | `set_config.py:1771` | `deployment.json llama_bin`(新鍵)+ `set_config --llama-bin` + `launch_servers --llama-bin` + `deployment_profile exec --llama-bin`(預設讀檔)+ `record_semantic_vectors --llama-bin` | C1 / C2 / C3 |
| `MODELS_DIR` | `set_config.py:2772`;`deployment_profile.py:796`(legacy 路徑) | — | `set_config --models-dir`(已存在,刪 env fallback);legacy 固定 `~/models` | C1 / C2 |
| `MAIN_SESSION` `AUX_SESSION` `SESSION` | `launch_servers.py:90-91`;`stop_servers.py:40-41`;`set_config.py:2594-2595` | start.sh `unset`(`_SESSION_ENV_KEYS:122`) | `deployment_profile.TMUX_SESSIONS` 常數 + `--main-session` / `--aux-session`(SUPPRESS) | C1 / C2 / C3 |
| `AICODE_NO_ROLLBACK` | `launch_servers.py:359`(+ 訊息 361, 411, 482) | — | `launch_servers --keep-on-failure` | C3 |
| `AICODE_STOP_TIMEOUT` | `stop_servers.py:46, 281` | — | `stop_servers --timeout`(預設 120) | C3 |
| `EXPECTED_LLAMA_SERVERS` | `check_status.py:98` | — | `check_status --expected`(預設 4) | C3 |
| `AICODE_STATUS_PROC_ROOT` `AICODE_STATUS_SNAPSHOT` | `check_status.py:31-33, 104-105` | — | `--proc-root` / `--snapshot`(SUPPRESS) | C3 |
| `AICODE_RERANK_FALLBACK_POLICY` | `launch_servers.py:256-259`(只印) | — | 刪;`client.json.rerank_fallback_policy` 是唯一來源(D-c4) | C3 |
| `MAIN_HEALTH_TIMEOUT` `RAG_HEALTH_TIMEOUT` | `launch_servers.py:118-128` | — | `launch_servers --health-timeout` | C3 |
| `AICODE_PROFILE` | `deployment_profile.py:703`;`scripts/doctor.py:446`(把 `--profile` 塞進 env) | — | `--profile` argv(已存在)→ `load_effective_profile(profile=)`;doctor 改 kwarg | C1 |
| `AICODE_DEPLOYMENT_CONFIG` | `deployment_profile.py:496-503, 693-701` | `set_config.py:2547`(暫存檔) | `load_effective_profile(deployment_config=)` + 隱藏 argv `--deployment-config` | C1 / C2 / C3 |
| `AICODE_MODEL_REGISTRY` `AICODE_MODEL_REGISTRY_FILE` | `deployment_profile.py:744-751` | `set_config.py:2540`(JSON 字串) | `load_effective_profile(model_registry_file=)` + 隱藏 argv;set_config 改寫暫存檔 | C1 / C2 / C3 |
| `_ENV_FIELDS` 其餘約 35 個(`AICODE_LLAMA_*_BASE_URL`、`MAIN/EMBED/RERANK/VL_{PORT,BIND,CTX,BATCH,UBATCH}`、`AICODE_{BIND,N_CTX,MAIN_CTX,…}`、`EMBED_MODEL` `RERANK_MODEL` `VL_GGUF` `VL_MMPROJ`、`AICODE_{EMBED,RERANK,VL}_MODEL`、`AICODE_VL_MMPROJ`) | `deployment_profile._environment_overlay:605-630` | start.sh `unset` | 刪 overlay;值只來自 `deployment.json`;main 的 model / ctx / batch / ubatch 由既有 launcher argv 覆寫 | C1 |
| `runtime_environment()` / `deployment_profile.py env` 子命令(**輸出** `AICODE_*`) | `deployment_profile.py:990-1005, 1015-1017, 1041-1048` | — | 刪(repo 內無消費者) | C1 |
| `AICODE_TEST_JOBS` | `scripts/run_tests.py:122`(+ docstring 10) | — | `run_tests --jobs N` | C3 |
| `set_config._fresh_env` / `_sanitized_subprocess_env`(`2506-2522`) | 剔除 override / session 名 | — | `process_env.child_env()` | C2 |
| `launch_servers.main:512` `env = dict(os.environ)` | 交給 loader | — | loader 只拿 HOME;tmux spawn 照舊 | C3 |
| `deployment_profile.py:1059-1062` `exec` 的 `os.environ` | registry 查表 + `execvpe` | — | 查表交 HOME-only;`execvpe` 用 `process_env.child_env()` | C1 |
| `deployment_status.inspect_deployment(environ=)`(`:189`) | 只拿去 `resolve_model_reference` | — | 參數刪,內部 HOME-only | C1 |

### C.3 殘留文字(不讀,但教 / 提到已刪或將刪的變數)

| 位置 | 內容 | 處理 | owner |
|---|---|---|---|
| `config.py:1074, 1127` | 「可透過環境變數 AI_CODE_PATCH=1 / AI_CODE_RUN_TESTS=1 啟用」(早已移除) | 刪句 | B1 |
| `config.py:56, 61, 136, 439, 512-514` | 提 `AICODE_MODEL` / `AICODE_MODEL_REGISTRY` / `AICODE_TOOL_CANARY_*` / `AICODE_ROOT` | `AICODE_ROOT` 是概念名(保留);其餘改字 | B1 |
| `scripts/check_readme_consistency.py:10, 220-221` | 第 5 條要求 README 提 `AICODE_MODEL` | 改成要求提 `deployment.json` 的 `main.model` | D1 |
| `eval/spec_questions.json` spec_001 / spec_003 | gold 含 `AICODE_MODEL`、`AI_CODE_PATCH` | 改成 `main.model` / `deployment.json`、`PATCH_ENABLED` / `client.json`;跑 `python3 scripts/check_eval_consistency.py` | D1 |
| `README.md:223, 249, 388, 429, 448, 520-560, 572, 600` | `LLAMA_BIN` / `MODELS_DIR` / `AICODE_MODEL` / `MAIN_GPU` / `AICODE_NO_ROLLBACK` 教學 | 改成 argv / 檔案鍵 | D1 |
| `README_DEV.md:48, 309-314` | `AICODE_TEST_JOBS=1`、`LLAMA_BIN=…` | `--jobs 1`、`--llama-bin` | D1 |
| `docs/setup.md:60-80` | systemd `Environment=` 三行 | 刪;`ExecStart` 不帶 `--llama-bin`(讀檔) | D1 |
| `docs/deployment-profiles.md:35-45, 40-60, 145-160` | `AICODE_MODEL`、env 覆寫段、GPU precedence | 改字 / 刪段 / 改成檔案 + argv 優先序 | D1 |
| `scripts/stop_servers.py:281`(使用者看得到的訊息)、`:30` 註解 | 「等待上限可用 AICODE_STOP_TIMEOUT 調整」 | `--timeout` | C3 |
| `deployment_profile.py:782`(「set AICODE_MODEL or a registry-backed profile model」)與其他字串常數 | 模型看得到的字串 | 改成指 `deployment.json` / `./set_config.sh`(gate `test_model_facing_text…` 會抓) | C1 |
| `scripts/set_config.py:364, 2633, 3154` | 「export LLAMA_BIN=… 後重跑」、「$MODELS_DIR 或 ~/models」、「AICODE_MODEL=… doctor」 | `--llama-bin`、`--models-dir`、`python3 scripts/doctor.py` | C2 |
| `tests/test_repo_consistency.py:1737-1773` `_DOC_CORE_VARIABLES` / `_DOC_CORE_SECTIONS` / `_LAUNCHER_SCRIPTS` | 核心變數的段落豁免 | 清空並移除分支 | D1 |
| `tests/test_repo_consistency.py:1947-1952` `allowed`、`1933-1943` `core_only` | 放行 `AICODE_MODEL` 等 | 只留概念名與協定標記;`core_only` 只留 `scripts/check_readme_consistency.py` | D1 |

### C.4 `_ENVIRON_ALLOWLIST` 逐筆核對(1467-1519)

| 檔 | 允許理由(現) | 實際有無讀取 | 處理 |
|---|---|---|---|
| `deployment_profile.py` | 啟動核心 overlay | 有(HOME、overlay) | 保留,理由改「HOME」 |
| `deployment_status.py` | 啟動核心 | `:189` `os.environ` fallback | 參數刪後可移除 |
| `scripts/launch_servers.py` / `stop_servers.py` / `check_status.py` / `set_config.py` | 啟動核心 | 有 | 保留,理由改「HOME / XDG(log 目錄)」 |
| `scripts/run_tests.py` | `AICODE_TEST_JOBS / TAIL_ARGS` | 有(`AICODE_TEST_JOBS`、`PYTEST_*` 寫入;`TAIL_ARGS` 不存在) | 保留,理由改「PYTEST_* 寫入」 |
| `config.py` `client_config.py` `client_prompt.py` `client_status.py` `client_store.py` `client_compaction.py` `client_engine.py` `client_preflight.py` `codetrail_chat.py` `mcp_server.py` `mcp_lease.py` `lessons.py` `index_scope.py` `external_import.py` `model_resolution.py` `scripts/doctor.py` `scripts/index_stats.py` `scripts/tool_call_canary.py` `scripts/required_model_servers_check.py` `scripts/session_eval.py` `scripts/eval_tool_routing.py` `eval/run_eval.py` `eval/record_semantic_vectors.py` | HOME / XDG / 子行程 | 有 | 保留;`model_resolution.py` 理由改「HOME(profile)」;`eval/record_semantic_vectors.py` 改 argv 後移除 |
| `process_env.py` `client_mcp.py` `scripts/mcp_catalog.py` | 剝除 | 有(`process_env`);`client_mcp.py:263` 與 `mcp_catalog.py:337` 只有**註解**提到 `os.environ` | 兩者移除;D1 以 gate 實跑確認 `_code_only` 看不到註解,看得到就保留並把理由改成「註解字面」 |
| `client_paths.py` `http_client.py` `media.py` `data_flywheel.py` `session_eval.py` `agent_tools.py` `container_runner.py` `client_app.py` `scripts/kb_ab_compare.py` `scripts/check_eval_consistency.py` | 各種 | **無**(grep 零命中) | 移除(stale 豁免) |
| `elf_analysis.py` | PATH(objdump) | 只有 `:573` regex 字面含 `getenv` | 保留,理由改「regex literal 含 getenv 字串,非讀取」 |
| `opencode_migrate.py` | HOME / XDG | 檔案刪除 | 移除 |

---

## D. 測試影響總表(檔 → 數量 / 動作;細目見 plan §5.3)

| 檔 | 現有測試數 | 動作 |
|---|---|---|
| `tests/test_opencode_migrate.py` | 63 | 整檔刪 |
| `tests/test_repo_consistency.py` | — | 刪 3、改名 1、改例子 4、加 1(T-0) |
| `tests/test_smoke_gate.py` | — | manifest 同步(plan §5.3) |
| `tests/test_aicode.py` | — | 刪 1 |
| `tests/test_compaction_formula.py` | — | 刪 1、改常數名 |
| `tests/test_client_compaction.py` | — | 改常數名 1 處 |
| `tests/test_evals.py` | — | 改名 2 + fixture |
| `tests/test_deployment.py` | 109 | fixture 改 1;env 優先序測試改寫 / 刪約 7;加 1 契約 |
| `tests/test_set_config.py` | 104 | 改名 1、刪 2、改斷言 2 |
| `tests/_set_config_harness.py` | — | 常數改 |
| `tests/test_server_scripts.py` | 36 | fixture 改寫;改約 10;刪 1;加 1 契約 |
| `tests/test_test_runner.py` | 29 | 改 2(`--jobs`) |
| `tests/test_client_preflight.py` | — | 呼叫端改名 |
| `tests/test_client_app.py` | — | 加 2 regression + 6 契約;參數化擴 1(`/session`) |
| `tests/test_client_store.py` | — | 加 1 契約 |
| `tests/test_doctor.py` | — | 刪 2 處 env pop |
