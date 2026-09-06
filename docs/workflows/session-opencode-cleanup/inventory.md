# 盤點:檔案 / 符號 / 環境值的最終歸宿(第 3 步最終修訂;**取代 `595b9f9` 的初稿**)

- 身分由編排者以 JSON metadata 記入 `model-log.md`。基準碼 `a1682d5`;行號是該 commit 的行號,施工以符號名為準。
- owner 欄對應 `plan-final.md` §3:**S**(session)、**B**(移除 OpenCode)、**C1**(loader)、**C2**(set_config)、**C3**(啟停腳本)、**D2**(文件)、**D1**(gate / manifest / AGENTS)。一檔一 owner;初稿的 A1 / A2 合併為 S,初稿把 `config.py` 拆給 B1 的註解改動全數歸 C1。
- 與初稿不同的地方以 **[修訂]** 標示。

---

## A. 目標 a:session 選單與重播(全部 S)

| 位置 | 現況 | 歸宿 |
|---|---|---|
| `client_store.py:97-104, 347-380` `SessionInfo` / `info()` / `list_sessions()` | 無大綱;`title` 永遠空 | 加五個有預設值的欄位、`OUTLINE_MAX_CHARS`、`session_outline()` 純函式、`list_sessions(limit=None)`;讀寫防線零改動 |
| `client_engine.py:632-658` `resume()` | 正確、原子,但只回模型歷史 | **[修訂]** 拆成 `load_session()`(唯一一次 `store.read()`,同時產出 `messages` 與 `transcript`,零狀態改動)+ `adopt()`(原子換 `session_id` / `messages` / `store_error` / `resumed_snapshot`);`resume()` = 兩者串接並回傳 `SessionSnapshot` |
| `client_engine.py:488-513` `replace_history()` | 追加 `compaction` 記錄 | 不改;transcript 的標記 `dropped` 由「模型歷史長度 − len(tail)」推得,與 `Compactor._replace` 的 `len(head)` 一致 |
| `client_engine.py:921-934, 949-953` heal | 懸空呼叫補「已中斷」結果並落檔 | 不改;證明結果永遠落在宣告它的 assistant 之後、下一則 assistant 之前 |
| `client_engine.py:1391-1403` `_FALLBACK_CALL_IDS` | 每行程從 1 起算 | **不改**(B-02 只修重播的配對) |
| `client_engine.py:47` 註解「上游 compaction.prune」 | 歷史說明 | 改字「舊世代前端的 prune 等價實作」 |
| `client_app.py:558-578` `_tool_output` | 以 id 反查 `engine.messages` 的最後一筆 | **[修訂]** 抽出 `format_tool_output(message)`;live 路徑找到訊息後呼叫它;重播不再反查 |
| `client_app.py:745-760` `_cmd_resume`、`:720-733` `_cmd_new`、`:445-461` `on_mount`、`:735-743` `_cmd_sessions`、`COMMANDS` | 不重播;不清畫面 | `_switch_session()`、`_replay_history()`、`history_entries()`、`HistoryEntry`、`SummaryBlock`、`SessionPickerScreen`、`/session`、`action_interrupt` / `action_leave` 先收選單 |
| `codetrail_chat.py:378-383` `command_sessions` | 印 `id / turns / title` | 加 `updated` 與 `first_prompt`;`_build` / `command_chat` **不改形狀**(靠 `engine.resumed_snapshot`) |
| `tests/test_client_app.py` `_Engine` / `_Store` 替身 | `resume()` 只換 id | 加 `load_session` / `adopt` / `resumed_snapshot`;§5.1 兩條 regression 用它 |

---

## B. 目標 b:OpenCode 殘留逐檔歸宿

### B.1 刪除
| 路徑 | owner |
|---|---|
| `opencode_migrate.py`(1,743 行,含 `EXPERIMENTAL_*` / `mode_tag` 等本檔外無消費者的符號) | B |
| `opencode_plugins/codetrail-compaction.js`、`opencode_plugins/codetrail-notify.js` | B |
| `tests/test_opencode_migrate.py`(63 條) | B |
| `compaction_formula.py:301-354` `combine_settings`、`:51-53` 懸空註解 | B |
| `scripts/doctor.py:793-825` `check_legacy_opencode_install` + `:1312` 呼叫 | **C1**(doctor 整檔歸 C1) |
| `tests/test_repo_consistency.py:997-1170` 三條 stub 測試 + `_strip_js_comments` + `_assert_inert_stub` | D1 |
| `tests/test_aicode.py:310-316` `test_aicode_never_execs_opencode` | D1 |
| `tests/test_compaction_formula.py:86-115` `test_combining_two_models_keeps_the_single_model_relationships` | B |

### B.2 改名 / 改字但保留功能
| 路徑 | 歸宿 | owner |
|---|---|---|
| `compaction_formula.py:33,37,39` 三常數 | `COMPACTION_RESERVE_TOKENS` / `OUTPUT_TOKEN_MAX` / `MIN_PRESERVE_RECENT_TOKENS`;docstring 9-17、141-142、202-205、221-249 去 OpenCode / `overflow.ts` 引用 | B |
| `client_compaction.py:94-98`、`:3` | 綁 `OUTPUT_TOKEN_MAX`;docstring 改字 | B |
| `config.py:406` 註解 | 常數名同步 | **C1 [修訂]** |
| `tests/test_client_compaction.py:85`、`tests/test_compaction_formula.py:17-18, 54-55, 140-146` | 新名 / 刪引用 | B |
| `scripts/eval_tool_routing.py:1588` 字面前綴 tuple | `process_env.STRIPPED_ENV_PREFIXES` | **B [修訂]**(該檔整檔歸 B) |
| `scripts/mcp_catalog.py:106-108, 128, 284`、`scripts/eval_tool_routing.py:373`、`tests/test_evals.py:562, 584` `opencode_effective_chars` | **[修訂]** live 欄位與 `summary()` 新輸出改 `catalog_effective_chars`;`LEGACY_EFFECTIVE_CHARS_KEY` + `effective_chars(mapping)` 讀取相容;凍結契約對 `era: "opencode"` row 仍以舊鍵比對 | B |
| `eval/fixtures/tool_routing/support_matrix.json`(`era: "opencode"` 六臂、`:101` 舊鍵) | **不動** | — |
| `process_env.py:10, 33` `OPENCODE_` 剝除 | **不動** | — |
| `tests/test_evals.py:237, 1462, 1494` | 改名 `…_effective_profile`、`test_a_replay_timeout_is_a_scored_case_failure_not_a_suite_abort`、`cmd=["python3","codetrail_chat.py","run"]` | B |

### B.3 只剩歷史說明的註解 / docstring(gate 改掃全文後,不在 allowlist 的檔一律改字為「舊世代前端」或刪句)
| 檔:行 | owner |
|---|---|
| `model_resolution.py:4-15, 112-113, 247-248, 265-267` | C1 |
| `client_mcp.py:260, 413`、`client_policy.py:77`、`context_budget.py:11`、`mcp_server.py:34`、`mcp_lease.py:604` | B |
| `client_engine.py:47` | **S [修訂]** |
| `scripts/tool_call_canary.py:55, 253, 413, 519`、`scripts/session_eval.py:448`、`scripts/doctor.py:739` | C1 |
| `scripts/eval_tool_routing.py:1096, 1221, 1290, 1301, 1333, 1532, 1672` | **B [修訂]** |
| `scripts/set_config.py:1167, 1874, 2306`(2306 是使用者可見字串 → 指向 troubleshooting 升級段) | C2 |
| `scripts/check_readme_consistency.py:14, 63-65, 152, 174-175, 196-206, 644, 759, 775-776, 856-860` | D2(檔在 allowlist,反向 pattern 保留) |
| `tests/` 內的歷史註解 | 選做,不列驗收(tests/ 不在 gate 範圍) |

### B.4 測試 fixture 裡的 `OPENCODE_CONFIG` / `OPENCODE_API_KEY`
| 檔:行 | 處理 | owner |
|---|---|---|
| `tests/test_deployment.py:691` | 刪 | C1 |
| `tests/test_doctor.py:69, 735` | 刪 | **C1 [修訂]** |
| `tests/_set_config_harness.py:42` | 刪 | C2 |
| `tests/test_client_preflight.py:52`、`tests/test_client_mcp.py:451-522`、`tests/test_evals.py:1927-1930`、`tests/conftest.py:81`、`tests/test_set_config.py:2358` | **保留**(它們證明殘留變數無效 / 剝除契約 / 外來目標例子) | — |

### B.5 文件(全部 D2)
| 檔:行 | 處理 |
|---|---|
| `README.md:8` | 「不需要 Node / npm」 |
| `README.md:85-97`、`:146-154`、`:353-355`、`:421-427` | 升級 bullet / §1.2 / 唯一會碰 opencode 的工具 / 升級檢查:一律改成一句指向 `docs/troubleshooting.md` 升級段;不再出現 `python3 opencode_migrate.py` |
| `README.md:715` | 「舊世代前端加的」 |
| `README_DEV.md:20, 25-27, 114, 178-182` | 刪 |
| `README_DEV.md:248, 337-342` | 保留(歷史量測說明) |
| `docs/troubleshooting.md:158-189` 與 `:623-648` | 合併成一段,內容依 `plan-final.md` §6.2(原安裝路徑身分、三種固定舊版情況、手動路徑的四個條件、合成設定驗證、`deployment.json` 新鍵對舊世代的 fail-loud) |
| `docs/security.md:81, 135` | 改字 / 指向升級段 |
| `docs/basic-usage.md:47` | 保留 |
| `AGENTS.md` | D1,依 `plan-final.md` §6.3 |

### B.6 gate 現況 → 目標(`tests/test_repo_consistency.py`,D1)
| 項目 | 現況 | 目標 |
|---|---|---|
| `_OPENCODE_ALLOWLIST`(1342-1370) | 21 筆,含不存在的 `docs/opencode-agents-template.md` | **[修訂]** `process_env.py`、`scripts/mcp_catalog.py`、`scripts/eval_tool_routing.py`、`scripts/check_readme_consistency.py`、`eval/fixtures/tool_routing/support_matrix.json`、`README.md`、`README_DEV.md`、`docs/troubleshooting.md`、`docs/security.md`、`docs/basic-usage.md`、`AGENTS.md` |
| `_OPENCODE_WHOLE_FILE`(1382) | 三檔整檔豁免 | 空 |
| `_OPENCODE_SHAPE_EXEMPTIONS`(1388) | doctor / check_readme | `process_env.py: OPENCODE_`;兩個 eval 腳本:`opencode_effective_chars\|"era"`;`check_readme_consistency.py: opencode-ai\|npm\|opencode_migrate\|OPENCODE_` |
| 掃描範圍 | `.py` 先剝註解 | 含註解與 docstring 的全文(docs 走「教使用者用」判準,另加 `python3 opencode_migrate.py` 不在 `a1682d5` 段落的形狀) |
| `_ENVIRON_ALLOWLIST:1497`、`core_only:1940` | 含 `opencode_migrate.py` | 刪 |
| `_walk_files` | 不跳 `docs/workflows` | **[修訂]** 不動;新 `_handoff_markdown(rel)` 只用於兩個內容 gate 的 `.md` 來源選擇 |
| `_SPAWN_CORE`(1526) | 九個檔 | `deployment_profile.py`、`scripts/run_tests.py`、`eval/run_eval.py`、`eval/record_semantic_vectors.py`;另加「`deployment_profile.py` 的 `execvpe` env 必是 `process_env.llama_server_env()`」靜態檢查 |

`tests/test_smoke_gate.py`:刪 `test_opencode_migrate.py` 鍵(565-609)、`test_aicode.py:56`、`test_compaction_formula.py:139`、`test_repo_consistency.py` 的三條 stub node(622-625);改名 `:627`、`test_evals.py:809`;`test_deployment.py:610-618` 說明改寫並加新 node;新增 `plan-final.md` §5.1 / §5.2 全部 node。

---

## C. 目標 c:環境值逐項歸宿

### C.1 保留(檔案位置 / 行程介面 / 安全剝除)
| 名稱 | 讀取處(非 tests) | 處理 |
|---|---|---|
| `HOME`(`USERPROFILE` 只在缺席時) | `config.py`、`client_*`、`codetrail_chat.py`、`mcp_server.py`、`mcp_lease.py`、`lessons.py`、`index_scope.py`、`external_import.py`、`deployment_profile.py`、`scripts/*`、`client_preflight.py` | 保留;`load_effective_profile(environ)` 的 `environ` 從此**只**讀這兩個鍵 |
| `XDG_STATE_HOME` / `XDG_CACHE_HOME` | `client_store.py`、`client_compaction.py`、`mcp_lease.py`、`scripts/launch_servers.py`(log 目錄)、`scripts/tool_call_canary.py`、start.sh `logs` | 保留 |
| `PATH` | `shutil.which`(tmux / objdump / llama-server) | 保留 |
| `PYTHONIOENCODING`(寫入)、`PYTEST_DISABLE_PLUGIN_AUTOLOAD` / `PYTEST_DEBUG_TEMPROOT`(寫入) | `mcp_server.py:122`、`eval/run_eval.py:25`、`scripts/run_tests.py:817-838` | 保留 |
| `process_env.child_env()` 剝 `AICODE_ / AI_CODE_ / CODETRAIL_ / OPENCODE_` | 所有 spawn | **保留,不動** |
| `process_env.llama_server_env()`(**新增**,C1) | `deployment_profile.py exec` 的 `execvpe` | `child_env()` 再剝 `LLAMA_ARG_*` 與 `CUDA_VISIBLE_DEVICES`;`PATH` / `LD_LIBRARY_PATH` / `GGML_*` / `LLAMA_LOG_*` 保留 |
| `CUDA_VISIBLE_DEVICES`(**輸出**) | `deployment_profile.build_server_command:947` | 保留輸出;輸入移除(C.2) |

### C.2 啟動核心:改檔案 / 常數 / argv
| 名稱 | 現在誰讀(行) | 歸宿 | owner |
|---|---|---|---|
| `AICODE_MODEL` | `deployment_profile._ENV_FIELDS:546`;`model_resolution.py:244`;`scripts/launch_servers.py:71`;`set_config.py:1768, 2508` | `deployment.json services.main.model`(已存在)+ `--main-model`(`LauncherOverrides.main_model`);`resolve_main_model()` 刪分支;start.sh 不 export | C1 / C2 / C3 |
| `MAIN_GPU` `AUX_GPU` `EMBED_GPU` `RERANK_GPU` `VL_GPU` | `_gpu_for:667-678`、`RUNTIME_OVERRIDE_ENV_KEYS:595-599`;`launch_servers.py:72-76`;`set_config.py:1673-1682, 2558-2561` | `services.<role>.gpu`(新鍵)+ `--<role>-gpu` / `--aux-gpu`(`LauncherOverrides.gpus`);exec 轉發 | C1 / C2 / C3 |
| `CUDA_VISIBLE_DEVICES`(輸入) | `_gpu_for:675`;tmux 全域 / session 環境 | 刪讀取;**pane 最終環境由 `llama_server_env()` 剝除**(B-03) | C1 / C3 |
| `LLAMA_ARG_*`(llama.cpp 自己讀) | 無(殼層 / tmux 直達 llama-server) | `llama_server_env()` 剝除 | C1 |
| `LLAMA_BIN` | `set_config.py:348`;`launch_servers.py:421`;`deployment_profile.py:1023`;`eval/record_semantic_vectors.py:121` | `deployment.json llama_bin`(新鍵)+ `--llama-bin`(set_config / launcher / exec / record_semantic_vectors);`DEFAULT_LLAMA_BIN` 常數 | C1 / C2 / C3 |
| `MODELS_DIR` | `set_config.py:2772`;`deployment_profile.py:796` | `--models-dir`(已存在,刪 env fallback);legacy 固定 `~/models` | C1 / C2 |
| `MAIN_SESSION` `AUX_SESSION` `SESSION` | `launch_servers.py:90-91`;`stop_servers.py:40-41`;`set_config.py:2594-2595` | `deployment_profile.TMUX_SESSIONS` 常數 + `--main-session` / `--aux-session`(SUPPRESS) | C1 / C2 / C3 |
| `AICODE_NO_ROLLBACK` | `launch_servers.py:359, 361, 411, 482` | `--keep-on-failure` | C3 |
| `AICODE_STOP_TIMEOUT` | `stop_servers.py:46, 281` | `--timeout`(預設 120) | C3 |
| `EXPECTED_LLAMA_SERVERS`、`AICODE_STATUS_PROC_ROOT`、`AICODE_STATUS_SNAPSHOT` | `check_status.py:31-33, 98, 104-105` | `--expected`、`--proc-root` / `--snapshot`(SUPPRESS) | C3 |
| `AICODE_RERANK_FALLBACK_POLICY` | `launch_servers.py:256-259`(只印) | 刪;`client.json.rerank_fallback_policy` 唯一來源 | C3 |
| `MAIN_HEALTH_TIMEOUT` `RAG_HEALTH_TIMEOUT` | `launch_servers.py:118-128` | `--health-timeout` | C3 |
| `AICODE_PROFILE` | `deployment_profile.py:703`;`scripts/doctor.py:446` | `--profile` argv → `profile=` kwarg;doctor 的 `_profile_env` 不再塞它 | C1 |
| `AICODE_DEPLOYMENT_CONFIG` | `deployment_profile.py:496-503, 693-701`;`set_config.py:2547` | `deployment_config=` kwarg + `--deployment-config`(SUPPRESS);set_config 用它交暫存檔 | C1 / C2 / C3 |
| `AICODE_MODEL_REGISTRY` / `_FILE` | `deployment_profile.py:744-751`;`set_config.py:2540` | `model_registry_file=` kwarg + `--model-registry-file`(SUPPRESS);`DeploymentProfile.registry_file` 一路交到 `resolve_model_reference` / `build_server_command` / `inspect_deployment`;set_config 改寫暫存檔 | C1 / C2 / C3 |
| `_ENV_FIELDS` 其餘約 35 個 | `_environment_overlay:605-630` | 刪 overlay;值只來自 `deployment.json`;main 的 model / ctx / batch / ubatch 由 `LauncherOverrides` 覆寫 | C1 |
| `runtime_environment()` / `env` 子命令 | `deployment_profile.py:990-1005, 1015-1017, 1041-1048` | 刪 | C1 |
| `AICODE_TEST_JOBS` | `scripts/run_tests.py:122` | `--jobs N` | C3 |
| `set_config._fresh_env` / `_sanitized_subprocess_env`(`2506-2522`)、`_restart_servers` 的 `env=` | 剔除 override / session 名 | 刪;spawn 走 `process_env.run` | C2 |
| `launch_servers.main:512` `dict(os.environ)` | 交給 loader | 刪;loader 只讀 HOME;tmux spawn 走 `process_env.run`;pane 命令改 `deployment_profile.py exec <role> <loader argv>` | C3 |
| `deployment_profile.py:1059-1062` `exec` 的 `os.environ` | registry 查表 + `execvpe` | 查表走 `profile.registry_file`;`execvpe(..., process_env.llama_server_env())` | C1 |
| `deployment_status.inspect_deployment(environ=)`(`:189`)、`query_gpu_processes(run=subprocess.run)` | 只拿去 `resolve_model_reference` | 參數刪;`run` 預設 `process_env.run` | C1 |

### C.3 殘留文字(不讀,但教 / 提到已刪或將刪的變數)
| 位置 | 處理 | owner |
|---|---|---|
| `config.py:56, 61, 136, 439, 512-514, 1074, 1127` | `AICODE_ROOT` 是概念名(保留);其餘改字 / 刪句(`AI_CODE_PATCH` / `AI_CODE_RUN_TESTS` 兩句刪) | **C1 [修訂]** |
| `scripts/check_readme_consistency.py:10, 220-221` | 第 5 條改成要求 README 講 `deployment.json` 的 `main.model` | D2 |
| `eval/spec_questions.json` spec_001 / spec_003 | gold 改 `main.model` / `deployment.json`、`PATCH_ENABLED` / `client.json`;與 `config.py:1074/1127` 同步 | D2 |
| `README.md:223, 249, 388, 429, 448, 520-560, 572, 600` | 改成 argv / 檔案鍵 | D2 |
| `README_DEV.md:48, 309-314` | `--jobs 1`、`--llama-bin` | D2 |
| `docs/setup.md:73-76` | 刪三行 `Environment=`;`ExecStart` 不帶 `--llama-bin`(讀檔) | D2 |
| `docs/deployment-profiles.md:29-34, 40-47, 150-157` | 刪 env 覆寫段;GPU precedence 改 argv > 檔案 | D2 |
| `scripts/stop_servers.py:30, 281` | `--timeout` | C3 |
| `deployment_profile.py:782`(「set AICODE_MODEL …」)等字串常數 | 改指 `deployment.json` / `./set_config.sh` | C1 |
| `scripts/set_config.py:364, 2306, 2633, 3154` | `--llama-bin`、升級段、`--models-dir`、`python3 scripts/doctor.py` | C2 |
| `tests/test_repo_consistency.py:1737-1773, 1933-1952` | `_DOC_CORE_*` / `_LAUNCHER_SCRIPTS` 清空;`allowed` / `core_only` 收緊 | D1 |

### C.4 `_ENVIRON_ALLOWLIST` 逐筆核對(1467-1519)
| 檔 | 實際有無讀取 | 處理 |
|---|---|---|
| `deployment_profile.py`、`scripts/launch_servers.py` / `stop_servers.py` / `check_status.py` / `set_config.py` | 有(HOME / XDG) | 保留,理由改「HOME / XDG」 |
| `deployment_status.py`、`eval/record_semantic_vectors.py` | 參數刪 / 改 argv 後無 | 移除 |
| `scripts/run_tests.py` | `PYTEST_*` 寫入(`TAIL_ARGS` 不存在) | 保留,理由改「PYTEST_* 寫入」 |
| `config.py` `client_config.py` `client_prompt.py` `client_status.py` `client_store.py` `client_compaction.py` `client_engine.py` `client_preflight.py` `codetrail_chat.py` `mcp_server.py` `mcp_lease.py` `lessons.py` `index_scope.py` `external_import.py` `model_resolution.py` `scripts/doctor.py` `scripts/index_stats.py` `scripts/tool_call_canary.py` `scripts/required_model_servers_check.py` `scripts/session_eval.py` `scripts/eval_tool_routing.py` `eval/run_eval.py` `root_safety.py` | 有 | 保留;`model_resolution.py` 理由改「HOME(profile)」 |
| `process_env.py` | 有 | 保留 |
| `client_mcp.py`、`scripts/mcp_catalog.py` | 只有註解提到 | D1 以 gate 實跑確認 `_code_only` 看不到註解 → 移除;看得到就保留並改理由 |
| `client_paths.py` `http_client.py` `media.py` `data_flywheel.py` `session_eval.py` `agent_tools.py` `container_runner.py` `client_app.py` `scripts/kb_ab_compare.py` `scripts/check_eval_consistency.py` | **無** | 移除(stale 豁免) |
| `elf_analysis.py` | `:573` regex 字面含 `getenv` | 保留,理由改「regex literal」 |
| `opencode_migrate.py` | 檔案刪除 | 移除 |

---

## D. 測試影響總表(細目見 `plan-final.md` §5.3)
| 檔 | 現有 | 動作 | owner |
|---|---|---|---|
| `tests/test_opencode_migrate.py` | 63 | 整檔刪 | B |
| `tests/test_repo_consistency.py` | — | 刪 3、改名 1、改例子 5、加 1(T-0)、`_SPAWN_CORE` / allowlist / `_DOC_CORE_*` 調整 | D1 |
| `tests/test_smoke_gate.py` | — | manifest 同步 | D1 |
| `tests/test_aicode.py` | — | 刪 1 | D1 |
| `tests/test_compaction_formula.py` | — | 刪 1、改常數名 | B |
| `tests/test_client_compaction.py` | — | 改常數名 1 處 | B |
| `tests/test_evals.py` | — | 改名 2 + fixture + `effective_chars` 2 處 | B |
| `tests/test_deployment.py` | 109 | fixture 改 1;env 優先序 / GPU 測試改寫或刪約 8;加 4 契約 | C1 |
| `tests/test_doctor.py` | — | 刪 2 處 env pop | C1 |
| `tests/test_client_preflight.py` | — | 預期零改動 | C1 |
| `tests/test_set_config.py` | 104 | 改名 1、刪 2、改斷言約 6、monkeypatch 目標同步;加 1 契約 | C2 |
| `tests/_set_config_harness.py` | — | 常數改字面清單 | C2 |
| `tests/test_server_scripts.py` | 36 | fixture 改寫;改約 10;刪 1;加 3 契約 | C3 |
| `tests/test_test_runner.py` | 29 | 改 2(`--jobs`) | C3 |
| `tests/test_client_app.py` | 29 | 加 2 regression + 7 契約;參數化擴 1;替身擴充 | S |
| `tests/test_client_engine.py` | 83 | 加 2 契約 | S |
| `tests/test_client_store.py` | 25 | 加 1 契約 | S |
