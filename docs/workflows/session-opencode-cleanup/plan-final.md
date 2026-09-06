# 最終計畫(流程第 3 步):session 選單與重播、移除 OpenCode 整合、環境變數收斂

- 身分由編排者以 JSON metadata 記入 `model-log.md`;本檔不自述。基準碼 `a1682d5`,初稿 `595b9f9`,Astra 審核 `c6052b2`。本輪未改碼、未跑測試、未讀真實 session 或 `~/.config`。
- **本檔是唯一實作真值**;`inventory.md`(最終修訂)是逐檔歸宿附件;`handoff-execution.md` 是工作者統一說明;`deferred.md` 為空。初稿與 Astra 審核不再修改。
- 角色:全程 developer(無 `ROLE=REVIEWER`)。實作者 = Opus(`--model claude-opus-5 --effort max`,最多 3 個並行);審核 = Astra 只報 Blocker;修復 = Fable 直到 0。

## 0. B-01..B-08 對照(每項:決定 → 落點 → 守它的 node)

| # | 決定 | 落點(owner) | 守它的 smoke node |
|---|---|---|---|
| B-01 | 畫面用**原始記錄**、模型用 compacted history;一次受信讀取 `Engine.load_session()` 同時產出兩份,`Engine.adopt()` 原子切換;compaction 只以標記呈現(不重繪 tail);讀取失敗時 engine / 畫面 / compactor 三者都不動;撤回 D-a3 | `client_engine.py`(S)、`client_app.py`(S) | `test_client_engine.py::test_the_snapshot_model_history_is_compacted_while_the_transcript_keeps_the_originals`、`::test_load_session_leaves_the_engine_untouched_and_adopt_switches_atomically`;`test_client_app.py::test_replay_shows_pre_compaction_originals_and_a_summary_marker`、`::test_a_failed_switch_keeps_the_session_and_the_screen` |
| B-02 | 工具結果按**宣告群組**配對:每則帶 `tool_calls` 的 assistant 開新群組,結果只配同群組內未回答的同 id;舊群組的懸空呼叫永遠 pending;沒有群組的結果是 `tool_orphan`。不再以 id 反查整段 engine 歷史;fallback id 產生器不動 | `client_app.history_entries`(S) | `test_client_app.py::test_replay_pairs_tool_results_by_declaration_group_not_by_id` |
| B-03 | tmux pane 改跑 `deployment_profile.py exec <role> <loader argv>`,由 `exec` 以 `process_env.llama_server_env()` 算最終環境後 `execvpe`:剝四前綴 + `LLAMA_ARG_*` + `CUDA_VISIBLE_DEVICES`,GPU 只由 `env CUDA_VISIBLE_DEVICES=<驗證過的 gpu>` 前綴重新輸出;launcher / stop / status / set_config 的 tmux、nvidia-smi、ss 全走 `process_env.run`;不碰使用者 tmux 的全域環境 | `process_env.py`、`deployment_profile.py`(C1);`scripts/launch_servers.py`、`stop_servers.py`、`check_status.py`(C3);`scripts/set_config.py`(C2) | `test_deployment.py::test_the_server_environment_strips_gpu_selectors_and_llama_settings`;`test_server_scripts.py::test_the_exec_path_hands_llama_server_a_clean_environment`、`::test_the_pane_runs_the_exec_choke_point_with_the_loader_argv` |
| B-04 | 升級指引重寫:固定舊版必須在**原安裝路徑**執行(同路徑 detach 到 `a1682d5`;路徑已不存在則 `git worktree add <原路徑> a1682d5`;是別份安裝就用它自己的工具);手動路徑只刪**完整路徑**相等的 plugin 項、只在有 ownership 證據且現值 JSON 嚴格相等時還原、無法證明就保留、狀態檔最後才刪;驗證只用合成設定,施工機器零遷移 | `docs/troubleshooting.md`、`README.md`、`docs/security.md`(D2) | 靜態:`check_readme_consistency` 綠 + D1 的 docs gate;Astra 對照 §6.2 |
| B-05 | `resolve_main_model_from_env` → `resolve_main_model(env=None, *, profile=None)`,刪 `AICODE_MODEL` 分支;呼叫端全表:`config.py:139,148`、`client_preflight.py:140`、`scripts/doctor.py:56,465`、`scripts/tool_call_canary.py:53,976`、`tests/test_deployment.py:59,157`——**全部 C1**(`config.py` 整檔含註解歸 C1) | `model_resolution.py`、`config.py` 等(C1) | `test_deployment.py::test_the_main_model_resolver_has_no_environment_branch`;`compileall` |
| B-06 | 不動 `_walk_files`;新 helper `_handoff_markdown(rel)`(`docs/workflows/**/*.md`)只用在 OpenCode 內容 gate 與 docs gate 的**來源選擇**;同目錄 `.py/.sh/.json/.toml` 照掃 | `tests/test_repo_consistency.py`(D1) | `test_repo_consistency.py::test_the_handoff_markdown_exemption_is_content_only` |
| B-07 | `_OPENCODE_ALLOWLIST` 收錄 `scripts/check_readme_consistency.py`,shape exemption `opencode-ai\|npm\|opencode_migrate\|OPENCODE_`(它的字串就是反向 pattern);grep 驗收准四個 `.py` 命中 | `tests/test_repo_consistency.py`(D1)、`scripts/check_readme_consistency.py`(D2) | 改名後的 `test_the_removed_frontend_only_survives_in_the_strip_list_history_data_and_upgrade_docs` |
| B-08 | 沒有動工前基線、不以 pytest cache 推定、不新增基線命令;T-0 與一般契約只由交付前唯一一次 smoke 驗;status / stop / exec 契約全用 tmp HOME + fake tmux / nvidia-smi / ss + `--proc-root` / `--snapshot`,不碰 `~/start.sh` 與真服務;doctor 不列入驗收;交接文件獨立提交,程式碼等使用者確認 | 編排者、D1 | §8 |

## 1. 讀碼結論(只列改變計畫的新事實)

- `Engine.resume()`(`client_engine.py:632-658`)以最後一筆 compaction.history 取代;session 檔仍保有壓縮前的所有 `message` 記錄與每筆 `{"type":"compaction","history":[摘要,+tail 複本]}`。畫面要的原文與計數都能從同一次 `store.read()` 推出:`dropped = 模型歷史長度 − len(tail)`,與 `Compactor._replace` 的 `len(head)` 相同。
- heal 的「已中斷」結果由 `run_tool_loop` 的 `heal_pending_tool_calls()` 追加為 `message` 記錄(`client_engine.py:921-934, 949-953`),永遠落在宣告它的 assistant 之後、下一則 assistant 之前 → 「宣告群組」規則成立;crash 後未 heal 的懸空呼叫在下一個群組出現同 id 時仍是 pending(B-02 情境)。
- `_FALLBACK_CALL_IDS` 每個行程從 1 起算(`:1391-1403`),同一 session 內 `call_1` 會重複 → 只能群組配對。
- llama.cpp `common/arg.cpp:723-748` 先套 env 再套 argv,142 個 `LLAMA_ARG_*`;tmux 3.6 pane 環境 = server 全域環境 + session 環境,launcher 的行程環境管不到既有 daemon → 最終邊界只能放在 pane 內真正 exec 的那個 Python 行程。
- `scripts/session_eval.py`、`required_model_servers_check.py` **不是** `resolve_main_model_from_env` 的呼叫端(初稿有誤);`tests/test_client_preflight.py` 也不直接呼叫它。
- `check_readme_consistency._documentation_text()` 只 `glob("*.md")` 不遞迴,交接目錄不進它;`test_user_facing_python_commands_use_python3` 會 `rglob` docs,交接檔一律寫 `python3`。
- `opencode_effective_chars` = description + input schema 的字元數(`scripts/mcp_catalog.py:284`),凍結在 `support_matrix.json:101`(digest 涵蓋);live 程式與新輸出可以改名,讀舊資料要相容。

## 2. 介面(施工照這份;改動要回寫本節與 inventory)

### I-1 `client_store`(S)
```python
@dataclass(frozen=True)
class SessionInfo:  # 既有六欄不動,新增有預設值的欄位
    first_prompt: str = ""; last_prompt: str = ""; messages: int = 0; tool_calls: int = 0; compactions: int = 0
OUTLINE_MAX_CHARS = 80
def session_outline(records: Sequence[Mapping]) -> dict   # 純函式;只認 role=="user" 且無 synthetic 的 message;\n 折空白、超長加 …;零 LLM、零寫入
def list_sessions(self, limit: int | None = None) -> list[SessionInfo]   # 排序不變;Ephemeral 仍回 []
```
讀寫防線(`read_bytes` / `_append_raw` / `_open_private_dir` / `_validate_header`)一行不改。

### I-2 `client_engine`:一次受信讀取 + 原子切換(S)
```python
@dataclass(frozen=True)
class SessionSnapshot:
    session_id: str
    messages: tuple[dict, ...]     # 模型歷史:最後一筆 compaction.history + 之後的 message(語意 = 今日 resume)
    transcript: tuple[dict, ...]   # 畫面歷史:全部 message 記錄原文(去 type;保留 structured / tool_status / reasoning_content / time)
                                   #   + 每筆 compaction 的標記 {"type":"compaction","time":float,"summary":str,"dropped":int,"kept":int}
    compactions: int
def load_session(self, session_id: str) -> SessionSnapshot   # 唯一一次 store.read();壞 compaction 記錄 → ValueError(同今日);不改 engine 任何狀態
def adopt(self, snapshot: SessionSnapshot) -> None            # 原子換 session_id / messages / store_error=None / resumed_snapshot
def resume(self, session_id: str) -> SessionSnapshot          # = adopt(load_session(id)) 並回傳;既有呼叫端不用改
Engine.resumed_snapshot: SessionSnapshot | None               # adopt 設、new_session 清;on_mount 據此重播啟動時接續的對話
```
`summary` = `history[0]` 為 synthetic user 時去掉 `SUMMARY_PREFIX` 的內容,否則 `""`;`kept = len(history) − (1 if summary else 0)`;`dropped = max(0, 當時模型歷史長度 − kept)`。

### I-3 `client_app`:重播純函式與 widget(S)
```python
@dataclass(frozen=True)
class HistoryEntry:
    kind: Literal["user","summary","assistant","assistant_error","reasoning","tool","tool_orphan"]
    text: str = ""; tool: str = ""; call_id: str = ""; arguments: dict = {}; status: str = ""; output: str = ""; structured: Any = None; dropped: int = 0
def history_entries(transcript: Sequence[Mapping]) -> list[HistoryEntry]   # 純函式,不碰 Textual
def format_tool_output(message: Mapping) -> str   # content + 「structuredContent(未裁切)」區塊;live 的 _tool_output(call_id) 找到訊息後也呼叫它
```
規則:順序 = transcript 順序。assistant 有 `reasoning_content` → 先 `reasoning`;`tool_status=="error"` → `assistant_error`;`content is None` 且無 tool_calls → 不產生條目。**配對(B-02)**:遇到帶 `tool_calls` 的 assistant 就開新群組(`open = {id: entry}`),舊群組未回答的條目維持 `pending`;`tool` 記錄配 `open.pop(tool_call_id)`,配不到 → `tool_orphan`;compaction 標記也關閉群組。`pending` 的 output 固定字串「這次呼叫沒有結果;下一題送出前會標成已中斷」。`summary` → `SummaryBlock(Collapsible, collapsed)`,標題「壓縮摘要:先前 {dropped} 則已壓縮、{kept} 則逐字保留(模型只看得到摘要)」;壓縮前原文仍在它上方逐字顯示,tail 不重繪。

### I-4 切換與啟動(S)
- `_switch_session(session_id)`:① `_busy_notice` 擋 → ② `snapshot = engine.load_session(id)`(任何例外 → `ErrorLine("無法接續:…")`,engine / 畫面 / `_tools` / compactor 全不動)→ ③ `entries = history_entries(snapshot.transcript)` 先把 widget 建好(未 mount)→ ④ `engine.adopt(snapshot)`;`coordinator.session_changed()`;`_tools.clear()`;`_assistant = _reasoning = None`;`_turn_started = None` → ⑤ `#log.remove_children()`、mount、`NoticeLine("已接續 <id>(畫面 N 則、模型歷史 M 則、壓縮 K 次)")`、`_recount_context()`、`_refresh_status()`。`/resume <id>`、`/session <id>`、選單選定都走它;`/new` 成功後同樣清畫面再貼「新對話」。
- 啟動:`on_mount` 在 banner 之後,若 `engine.resumed_snapshot` 且其 `session_id == engine.session_id` → `_replay_history(clear=False)` + 同一句 notice。`codetrail_chat._build` / `command_chat` 不改形狀(`_build` 內的 `engine.resume()` 已把 snapshot 留在 engine 上)。
- 重播出來的 `ToolBlock` **不登記進 `_tools`**;`reasoning` 條目 `display = show_reasoning`。
- `SessionPickerScreen(ModalScreen[str | None])`:`OptionList`,每列 `<updated 本地時間>  <turns> 輪  <first_prompt>`,最多 50 筆(`list_sessions(limit=50)`),Enter 選 / Esc 取消;`action_interrupt` / `action_leave` 先收選單(不算中斷、不算離開)。`COMMANDS` 在 `/sessions` 之後加 `("/session", "選一個既有對話切換(/session <id> 直接指定)")`,`/help` 仍第一。`/sessions` 每列 `<id>  <時間>  <turns> 輪  <first_prompt>`;CLI `sessions` 輸出 `<id>\tturns=<n>\tupdated=<ISO 本地>\t<first_prompt>`。

### I-5 `process_env`(C1)
```python
SERVER_STRIPPED_ENV_PREFIXES = ("LLAMA_ARG_",)          # llama.cpp 自己的設定入口
SERVER_STRIPPED_ENV_KEYS = ("CUDA_VISIBLE_DEVICES",)     # 不再當輸入;輸出只由 build_server_command 的 env 前綴
def llama_server_env() -> dict[str, str]                 # child_env() 再剝上面兩組;PATH / HOME / LD_LIBRARY_PATH / GGML_* 保留
```
`child_env()` / `run` / `Popen` / `check_output` 與四前綴清單一字不改。消費者:`deployment_profile.py exec`(唯一 `execvpe`)。

### I-6 `deployment_profile`(C1;C2、C3、D2 消費)
```python
DEFAULT_LLAMA_BIN = "~/llama.cpp/build/bin/llama-server"
TMUX_SESSIONS = {"main": "codetrail-main", "aux": "codetrail-rag"}
# deployment.json(local override):頂層 "llama_bin": "<絕對路徑>"(選填);"services".<role>."gpu": "<selector>"(選填,_GPU_RE;缺席 = 不指定)
# _LOCAL_TOP_LEVEL_KEYS += {"llama_bin"};_SERVICE_KEYS += {"gpu"};schema_version 仍 1;loader 只驗形狀,存在性由 launcher / exec 驗
@dataclass(frozen=True)
class LauncherOverrides:
    main_model: str | None = None; main_ctx: int | None = None; main_batch: int | None = None; main_ubatch: int | None = None
    gpus: Mapping[str, str] = {}   # main / embedding / reranker / vl / aux(aux 套到三個附屬缺席者)
    llama_bin: str | None = None
DeploymentProfile.llama_bin: str            # 絕對路徑:argv > 檔案 > DEFAULT_LLAMA_BIN(expanduser)
DeploymentProfile.registry_file: Path | None  # 明確給的 registry 暫存檔;None = ~/.config/codetrail/models.json
def add_loader_arguments(parser) -> None    # --profile;--deployment-config / --model-registry-file(SUPPRESS);--llama-bin;--main-model/-ctx/-batch/-ubatch;--main-gpu/--aux-gpu/--embed-gpu/--rerank-gpu/--vl-gpu
def loader_kwargs(args) -> dict             # → profile= / overrides= / deployment_config= / model_registry_file=
def loader_argv(args) -> list[str]          # 反向:同一組值變回 argv(launcher 轉給 pane 的 exec)
def load_effective_profile(environ=None, *, profile=None, overrides=None, deployment_config=None, model_registry_file=None)  # environ 只讀 HOME / USERPROFILE;cli_env 刪
def load_model_registry(environ=None, *, registry_file=None)
def resolve_model_reference(reference, environ=None, *, must_exist=False, registry_file=None)   # 呼叫端傳 profile.registry_file
def build_server_command(service, llama_bin, environ=None, *, must_exist=False, registry_file=None)  # 輸出形狀不變(含 env CUDA_VISIBLE_DEVICES= 前綴)
```
刪除:`_ENV_FIELDS`、`_environment_overlay`、`RUNTIME_OVERRIDE_ENV_KEYS`、`_gpu_for`、`_first_env`、`_parse_env_int`、`_environment_model`、`runtime_environment`、`env` 子命令、`MODELS_DIR` 讀取(legacy 固定 `~/models`)、`local_override_path` 的 `AICODE_DEPLOYMENT_CONFIG` 分支。CLI:頂層掛 `add_loader_arguments`;`exec <role>` = `build_server_command(service, profile.llama_bin, must_exist=True, registry_file=profile.registry_file)` → `os.execvpe(command[0], command, process_env.llama_server_env())`。`deployment_status.inspect_deployment(profile, gpu_processes, *, cmdline_reader, server_reader, gpu_inventory)` 刪 `environ`,registry 走 `profile.registry_file`;`query_gpu_processes` / `query_gpu_inventory` 預設 `run=process_env.run`。`model_resolution.resolve_main_model(env=None, *, profile=None)`。

### I-7 argv(C3 / C2;`~/start.sh` 只轉發 `"$@"`)
| 腳本 | 旗標 | 取代 |
|---|---|---|
| `scripts/launch_servers.py` | `add_loader_arguments` 全組 + `--scope` `--dry-run` `--health-timeout N` `--keep-on-failure`;`--main-session` / `--aux-session`(SUPPRESS) | `AICODE_MODEL`、五個 `*_GPU`、`CUDA_VISIBLE_DEVICES`、`LLAMA_BIN`、`MAIN/RAG_HEALTH_TIMEOUT`、`AICODE_NO_ROLLBACK`、三個 session 名、`AICODE_DEPLOYMENT_CONFIG` / `AICODE_MODEL_REGISTRY(_FILE)` |
| 同上 pane 命令 | `shlex.join([sys.executable, <repo>/deployment_profile.py, "exec", role, *loader_argv(args)])`(dry-run 印的 `{role}_command=` 仍是最終 llama-server argv) | 直接 respawn llama-server argv |
| `scripts/stop_servers.py` | `--timeout N`(預設 120)、loader args、`--main-session` / `--aux-session`(SUPPRESS);session 名來自 `TMUX_SESSIONS` | `AICODE_STOP_TIMEOUT`、session 三個 |
| `scripts/check_status.py` | `--expected N`(預設 4)、`--proc-root` / `--snapshot`(SUPPRESS)、loader args | `EXPECTED_LLAMA_SERVERS`、`AICODE_STATUS_*` |
| `scripts/set_config.py` | `--llama-bin PATH`(預設:既有 `deployment.json.llama_bin` → `DEFAULT_LLAMA_BIN`);`--models-dir` 無 env fallback;寫 `llama_bin` 與四個 `services.<role>.gpu`;`validate_payloads` 走 kwargs 兩個暫存檔;`preview_start_commands` 以 `--deployment-config` / `--model-registry-file` 跑 `launch_servers --dry-run`;`_restart_servers` 等全部 spawn 走 `process_env.run`;`running_codetrail_sessions()` 只看 `TMUX_SESSIONS` | `LLAMA_BIN`、`MODELS_DIR`、`_OVERRIDE_ENV_KEYS`、`_SESSION_ENV_KEYS`、`_fresh_env`、`_sanitized_subprocess_env`、split-brain 警告、start.sh 全部 `unset` / `export` |
| `scripts/run_tests.py` | `--jobs N`(1..16,轉發前吃掉) | `AICODE_TEST_JOBS` |
| `eval/record_semantic_vectors.py` | `--llama-bin PATH`(預設 `load_effective_profile().llama_bin`;`unknown` revision 仍拒絕) | `LLAMA_BIN` |
刪除不替代:`AICODE_RERANK_FALLBACK_POLICY`(dry-run 那一行一併刪)、`deployment_profile.py env`。

### I-8 `compaction_formula` 與 eval 欄位(B)
- `UPSTREAM_COMPACTION_BUFFER → COMPACTION_RESERVE_TOKENS`、`UPSTREAM_OUTPUT_TOKEN_MAX → OUTPUT_TOKEN_MAX`、`UPSTREAM_MIN_PRESERVE_RECENT_TOKENS → MIN_PRESERVE_RECENT_TOKENS`;值與算式不變;`combine_settings` 與 `:51-53` 懸空註解刪;docstring 改「沿用 2026-09 定案公式(來源見 git 歷史)」。`client_compaction.py:94-98` 綁 `OUTPUT_TOKEN_MAX`。
- `scripts/mcp_catalog.py`:欄位 `opencode_effective_chars → catalog_effective_chars`(live 內部名與 `summary()` 新輸出);`LEGACY_EFFECTIVE_CHARS_KEY = "opencode_effective_chars"`;`effective_chars(mapping) -> int` 先讀新鍵、退回舊鍵。`scripts/eval_tool_routing.py` 的凍結契約比對用 `effective_chars()` 兩邊取值;`_FROZEN_CATALOG_FIELDS` 對 `era: "opencode"` 的 row 仍以舊鍵名比對。`support_matrix.json` 與 `era: "opencode"` 標記**零改動**(它是量過的資料,repo_consistency 釘住六臂)。相容邊界:讀取端接受兩個鍵名;新寫入只用新鍵;不重造歷史 schema。
- `scripts/eval_tool_routing.py:1588` 字面前綴 tuple 改 import `process_env.STRIPPED_ENV_PREFIXES`。

## 3. Owner 表(一檔一 owner,含測試與 fixture;零重疊)

| Owner | 檔案 |
|---|---|
| **S** session | `client_engine.py`(含 `:47` 註解改字)、`client_app.py`、`client_store.py`、`codetrail_chat.py`、`tests/test_client_app.py`、`tests/test_client_engine.py`、`tests/test_client_store.py` |
| **B** 移除 OpenCode | 刪 `opencode_migrate.py`、`opencode_plugins/`、`tests/test_opencode_migrate.py`;`compaction_formula.py`、`client_compaction.py`、`scripts/mcp_catalog.py`、`scripts/eval_tool_routing.py`(前綴 import、effective_chars、註解)、`client_mcp.py` / `client_policy.py` / `context_budget.py` / `mcp_server.py` / `mcp_lease.py`(只改註解)、`tests/test_compaction_formula.py`、`tests/test_client_compaction.py`、`tests/test_evals.py` |
| **C1** 啟動核心 loader | `deployment_profile.py`、`deployment_status.py`、`model_resolution.py`、`process_env.py`、`config.py`(runtime 呼叫 + 全部註解:56、61、136、406、439、512-514、1074、1127)、`client_preflight.py`、`scripts/doctor.py`(刪 `check_legacy_opencode_install` + `:1312`、`_profile_env` 改 `profile=` kwarg、`:739`)、`scripts/tool_call_canary.py`、`scripts/session_eval.py`(`:448` 註解)、`scripts/required_model_servers_check.py`、`tests/test_deployment.py`(含 `:691` delenv)、`tests/test_doctor.py`(`:69`、`:735`)、`tests/test_client_preflight.py`(預期零改動;動了要列) |
| **C2** set_config | `scripts/set_config.py`、`tests/test_set_config.py`、`tests/_set_config_harness.py` |
| **C3** 啟停腳本 | `scripts/launch_servers.py`、`scripts/stop_servers.py`、`scripts/check_status.py`、`scripts/run_tests.py`、`eval/record_semantic_vectors.py`、`tests/test_server_scripts.py`、`tests/test_test_runner.py` |
| **D2** 文件 | `README.md`、`README_DEV.md`、`docs/*.md`(troubleshooting / security / basic-usage / setup / deployment-profiles / session-model-eval 視需要;`docs/compaction-rules.md` 與 `docs/mcp-tools.md` 不動)、`scripts/check_readme_consistency.py`、`eval/spec_questions.json` |
| **D1** gate 與 manifest | `tests/test_repo_consistency.py`、`tests/test_smoke_gate.py`、`tests/test_aicode.py`(刪一條)、`AGENTS.md` |
| 不動 | `eval/fixtures/tool_routing/support_matrix.json`、`tests/conftest.py`、`tests/test_client_mcp.py`、`aicode`、`set_config.sh`、共用檔名(`compaction-stopped.jsonl`、`setconfig-last-transaction.json`)、`~/.config/*` |

## 4. DAG 與 worker 分工(同時最多 3 個 Opus CLI;每個獨立 CLI、只動 owner 表的檔)

```
W0  S-red ──────────────────────────────────────────┐   (只寫兩條 regression、單跑取紅、寫 handoff-S-red.md;其餘 worker 等它)
W1  S(green + 其餘 a) ‖ B ‖ C1                        │
W2  C2(需 C1) ‖ C3(需 C1) ‖ D2(需 B、C1 的名字;可用本計畫)│
W3  D1(需全部;T-0、三 gate、manifest、AGENTS、靜態檢查) │
→  Astra 靜態審核(只對 a1682d5 之後的本任務改動)→ Fable 修復直到 Blocker=0(同一分歧兩輪以上才登記 deferred)
→  編排者執行唯一一次 `python3 scripts/run_tests.py -m smoke` → Astra 核對結果與最終 diff → 回報使用者、告知後才可能 push
```
每個 worker 依賴的**已落檔** API:S 只依賴自己;B 不依賴他人;C1 不依賴他人;C2 / C3 依賴 C1 的 I-5 / I-6(以 `handoff-C1.md` 宣告落檔為準);D2 依賴 I-7 / I-8 的名字(本計畫已定);D1 依賴所有 handoff 的 test-changes 與 node 名。名稱若與本計畫不同,handoff 必須明列,D1 以 handoff 為準登記。已知的跨 owner 斷點:C1 刪 `RUNTIME_OVERRIDE_ENV_KEYS` 後 `tests/_set_config_harness.py:25` 與 `tests/test_server_scripts.py:24` 的 import 會斷,分別由 C2 / C3 在 W2 修(W1 期間沒有人跑測試,所以不會被觀察到)。

## 5. 測試計畫(developer 權責)

### 5.1 真實 bug 的 regression(S;`@pytest.mark.smoke`;先在未改實作上單跑貼紅,改完同 node 單跑貼綠)
| node(`tests/test_client_app.py`) | 情境 | 未改碼時的紅 |
|---|---|---|
| `test_resume_replays_the_stored_history` | `_Engine` 替身實作 `load_session` / `adopt` / `resume`(resume = adopt(load_session)),snapshot.transcript = [user q, assistant a, assistant(tool_calls c1), tool c1(content+structured)];`app._command("/resume <id>")` | 現行 app 只呼叫 `resume` 並貼一行 notice:`users == []`、`assistant == []`、`tools == []` |
| `test_a_session_resumed_at_startup_is_shown_on_mount` | 替身預設 `messages` 與 `resumed_snapshot`(模擬 `_build` 已 resume)再 `run_test()` | 現行 `on_mount` 只有 banner / help notice |
命令:`python3 scripts/run_tests.py tests/test_client_app.py::<node>`(單條、逐字轉發)。兩條各跑紅一次、綠一次,證據貼進 `handoff-S-red.md` / `handoff-S.md`。

### 5.2 新契約 smoke(無聲失敗風險;D1 登記進 `SAFETY_MODULES`;途中不單跑)
- `tests/test_client_app.py`:`test_the_session_picker_lists_outlines_and_switches`、`test_a_failed_switch_keeps_the_session_and_the_screen`(load_session 丟例外 → session / widget / `_tools` / rebind 次數全不變)、`test_escape_and_ctrl_c_only_close_the_picker`、`test_replay_pairs_tool_results_by_declaration_group_not_by_id`(兩個 `call_1` 群組 + 更早的懸空 `call_1` 保持 pending)、`test_replay_shows_pre_compaction_originals_and_a_summary_marker`(兩次 compaction;原文各出現一次、tail 不重複、標記 dropped 正確)、`test_replayed_tool_blocks_are_not_registered_for_live_events`、`test_new_clears_the_screen`。
- `tests/test_client_engine.py`:`test_load_session_leaves_the_engine_untouched_and_adopt_switches_atomically`、`test_the_snapshot_model_history_is_compacted_while_the_transcript_keeps_the_originals`。
- `tests/test_client_store.py`:`test_the_outline_is_the_first_real_question_never_the_summary_or_tool_output`。
- `tests/test_deployment.py`:`test_the_loader_ignores_every_legacy_override_variable`(舊 overlay 全部名字 + `CUDA_VISIBLE_DEVICES` + `LLAMA_BIN` + `MODELS_DIR` + session 名同時設進 `os.environ` 與 environ 參數,有效 profile、`gpu`、`llama_bin` 不變)、`test_gpu_and_llama_bin_come_from_the_deployment_file_then_argv`、`test_the_server_environment_strips_gpu_selectors_and_llama_settings`、`test_the_main_model_resolver_has_no_environment_branch`。
- `tests/test_server_scripts.py`:`test_the_pane_runs_the_exec_choke_point_with_the_loader_argv`(fake tmux 記錄 respawn 命令列)、`test_the_exec_path_hands_llama_server_a_clean_environment`(tmp HOME 的 deployment.json 指到會 dump 環境的假 llama-server;以含 `CUDA_VISIBLE_DEVICES=7`、`LLAMA_ARG_THREADS=3`、`OPENCODE_API_KEY`、`AICODE_MODEL`、`MARK=keep` 的環境跑 `deployment_profile.py exec main` 與 `exec embedding`:main 得到檔案裡的 gpu、embedding 沒有任何 selector、四者皆無、`MARK` 仍在)、`test_stop_and_status_use_argv_and_constants_not_the_shell`(fake tmux / nvidia-smi 於 PATH;殼層 `MAIN_SESSION` / `SESSION` / `EXPECTED_LLAMA_SERVERS` 全部無效)。
- `tests/test_set_config.py`:`test_generated_start_sh_ignores_legacy_shell_overrides`(既有改名續用,斷言改為「沒有任何 `export ` / `unset ` 行」)、`test_deployment_json_pins_llama_bin_and_gpus`。
- `tests/test_repo_consistency.py`:`test_the_handoff_markdown_exemption_is_content_only`(T-0:`_handoff_markdown(Path("docs/workflows/x/p.md"))` 真、`…/tool.py` / `…/ci.yaml` / `docs/p.md` 假;真實 repo 的 `_walk_files` 仍列出本目錄;`_opencode_offenders("docs/workflows/x/tool.py", "import opencode_migrate")` 與 `_spawn_offenders(同路徑, "import subprocess")` 仍非空)。

### 5.3 既有測試變更(實作者逐條列入 handoff 的 test-changes;理由必須是「行為為什麼該變」)
| 檔(owner) | 測試 | 動作 / 理由 |
|---|---|---|
| `test_opencode_migrate.py`(B) | 63 條 | 刪:被測模組整個移除,它守的路徑本版不存在 |
| `test_compaction_formula.py`(B) | `test_combining_two_models_keeps_the_single_model_relationships` | 刪:`combine_settings` 唯一消費者是被刪的遷移工具;其餘只改常數名與 docstring,數字不變 |
| `test_client_compaction.py`(B) | `:85` | 常數改名 |
| `test_evals.py`(B) | `test_opencode_timeout_…` → `test_a_replay_timeout_is_a_scored_case_failure_not_a_suite_abort`(`cmd=["python3","codetrail_chat.py","run"]`)、`test_model_probe_endpoint_must_match_effective_opencode_provider` → `…_effective_profile`、`:562/:584` 改用 `mcp_catalog.effective_chars` | 名稱描述不存在的執行路徑;欄位改名但值相同 |
| `test_deployment.py`(C1) | `model_resolution_env` fixture 去 `OPENCODE_CONFIG`;`test_precedence_cli_env_…` → `test_precedence_cli_overrides_over_local_over_profile_over_defaults`;`test_canonical_n_ctx_override_wins_over_legacy_main_ctx` 刪;`test_explicit_local_override_must_exist` 改 kwarg;`test_profile_selector_precedence_cli_then_env_then_local` → `…_cli_then_local`;所有以 `AICODE_PROFILE` / `AICODE_MODEL` / `MAIN_*` env 表達輸入的測試(`:134-163`、`:295-335`、`:461`、`:515`、`:551-552`)改 `profile=` / overrides / 檔案;`test_bind_all_interfaces_via_override_and_env` 去 env 半;`:59/:157` 改名 | env overlay 這一層不存在了;其餘層級優先序不變 |
| `test_doctor.py`(C1) | `:69`、`:735` | 刪 `OPENCODE_CONFIG` 清理:沒有程式讀它 |
| `test_set_config.py`(C2) | `test_generated_start_sh_clears_legacy_env_overrides` → `…_ignores_legacy_shell_overrides`;`test_generated_start_sh_exports_before_subcommand_dispatch`、`test_deployment_env_override_split_brain_warns` 刪;`test_start_sh_pins_validated_llama_bin` 改斷言 `deployment.json.llama_bin`;`test_restart_subprocess_env_is_sanitized` 改斷言四前綴被剝、`KEEP_ME` 保留、去 SESSION 斷言;`:220/:222/:470/:471` 的 `export *_GPU` 斷言改成 `services.<role>.gpu`;`:1939-1962` 相對 `LLAMA_BIN` 改 `--llama-bin ./llama-server`;spawn 改走 `process_env.run` 後 monkeypatch 目標同步 | 真值搬到檔案;機制從 unset 變成沒有人讀 |
| `_set_config_harness.py`(C2) | `PROFILE_ENV_KEYS` | 改成字面清單(舊名字仍設進去證明無效);去 `OPENCODE_CONFIG` |
| `test_server_scripts.py`(C3) | `_clean_env` / `_run_launcher` 改寫成「寫 deployment.json + argv」;`test_start_all_routes_…`、`test_quit_still_kills_sessions_…`(`--main-session`)、`test_health_timeout_scales_with_model_size`、`test_rollback_respects_no_rollback_env` → `…_keep_on_failure_flag`、`test_start_rag_servers_dry_run_uses_base_url_ports`(去 `rerank_fallback_policy`)、`test_check_status_*`(`--expected` / `--proc-root` / `--snapshot`)、`test_stop_timeout_env_override_and_fallback` → argv;`test_start_role_pipes_server_output_to_persistent_log` 的 `respawn[-1]` 斷言改成 exec 形式;`test_legacy_aux_launcher_env_names_still_override_profile` 刪(它守的正是要移除的行為);`_patch_launch_scaffolding` 的 `_command_for` 簽名同步 | 介面由 env 換成檔案 / argv;pane 改跑 exec 是 B-03 的行為變更 |
| `test_test_runner.py`(C3) | `test_parallel_job_resolution*` | 改 `--jobs` |
| `test_client_app.py`(S) | `test_switching_sessions_is_refused_while_a_turn_is_running` 參數化加 `/session`、`/session <id>`;`_Engine` 替身加 `load_session` / `adopt` / `resumed_snapshot` | 新指令走同一條 busy 拒絕 |
| `test_aicode.py`(D1) | `test_aicode_never_execs_opencode` | 刪:`test_the_only_exec_target_…` 已釘唯一 exec 目標,OpenCode 內容 gate 亦掃 `aicode` |
| `test_repo_consistency.py`(D1) | 刪 `test_the_opencode_plugin_stubs_are_inert`、`test_the_stub_gate_rejects_initialisation_side_effects`、`test_the_js_comment_stripper_is_lexically_aware`(含 `_assert_inert_stub`、`_strip_js_comments`);`test_opencode_only_survives_…` 改名並依 I-6/§6.1 收緊;例子測試(`:2000-2064`、`:1983`)依新規則改;`test_user_docs_must_not_teach_removed_flags_or_files` 加 `export LLAMA_BIN=` 例子 | stub 已刪;核心段落豁免不存在了 |
| `test_smoke_gate.py`(D1) | `SAFETY_MODULES` | 刪 `test_opencode_migrate.py` 鍵、`test_aicode.py::test_aicode_never_execs_opencode`、`test_compaction_formula.py::test_combining_…`、三條 stub node;改名 `test_evals.py` 與 gate node;`test_deployment.py` 說明改寫;新增 §5.1 / §5.2 全部 node |

### 5.4 執行邊界
- 沒有動工前 suite 基線;`.pytest_cache` 的 `lastfailed={}` 不是基線;不新增基線命令。最終判定 = 唯一一次 smoke 的失敗集合為空;非空就是未完成,由 Fable 逐 node 修(該 node 已是紅燈證據、修後單跑轉綠),再跑第二次 smoke **必須先向使用者報告並取得同意**。`0 collected` 不是通過。
- 途中允許:`python3 -m compileall -q .`、`python3 scripts/check_eval_consistency.py`、`python3 scripts/check_readme_consistency.py`(D1 / D2 在各自收尾時跑)。禁止:full、collect-only、單跑既有 test、`--lf`、真 `~/start.sh` / doctor / servers。
- 最終由編排者以 AST diff 對照 `/tmp/codetrail-session-opencode-cleanup-20260906/baseline-test-symbols.json` 核對每份 handoff 的 test-changes。交付註明 `Tests: smoke only — reviewer owns full execution.`

## 6. 文件、gate 與 AGENTS 條款(D1 / D2)

### 6.1 三個靜態 gate 的最終形狀(D1)
- OpenCode gate:掃全文(含註解與 docstring);`_OPENCODE_ALLOWLIST` = `process_env.py`、`scripts/mcp_catalog.py`、`scripts/eval_tool_routing.py`、`scripts/check_readme_consistency.py`、`eval/fixtures/tool_routing/support_matrix.json`、`README.md`、`README_DEV.md`、`docs/troubleshooting.md`、`docs/security.md`、`docs/basic-usage.md`、`AGENTS.md`;`_OPENCODE_SHAPE_EXEMPTIONS` = `process_env.py: OPENCODE_`;兩個 eval 腳本:`opencode_effective_chars|"era"`;`check_readme_consistency.py: opencode-ai|npm|opencode_migrate|OPENCODE_`;`_OPENCODE_WHOLE_FILE` 清空。`.md` 的「教使用者用」判準另加形狀 `^\s*(?:[$>]\s*)?python3\s+opencode_migrate\.py`,**除非同一 logical line 或所在段落標題含 `a1682d5`**(升級段唯一合法用法)。
- environ gate:`_ENVIRON_ALLOWLIST` 剪掉零讀取的 10 筆與 `opencode_migrate.py`、`eval/record_semantic_vectors.py`、`deployment_status.py`;`core` 集合清空;`_SPAWN_CORE` 縮成 `{"deployment_profile.py", "scripts/run_tests.py", "eval/run_eval.py", "eval/record_semantic_vectors.py"}`;新增一條:`deployment_profile.py` 唯一的 `os.execvpe` 其 env 參數是 `process_env.llama_server_env()`(靜態 AST)。
- docs gate:`_DOC_CORE_VARIABLES` / `_DOC_CORE_SECTIONS` / `_LAUNCHER_SCRIPTS` 清空並移除分支;`_DOC_ALLOWED_TOKENS` 不變;`test_model_facing_text…` 的 `allowed` 只留概念名與協定標記,`core_only` 只留 `scripts/check_readme_consistency.py`。
- T-0:`_handoff_markdown(rel: Path) -> bool` 只用於 `_opencode_gate_sources()` 與 docs gate 的 `.md` 來源;`_walk_files` / `_repo_sources()` / `_iter_text_files()` 不動。
- `scripts/check_readme_consistency.py`(D2):第 5 條改成 README 必須講 `deployment.json` 的 `main.model`;`_STALE_DOC_PATTERNS` 加 `export LLAMA_BIN=` / `export MODELS_DIR=` / `AICODE_TEST_JOBS=` / `Environment=AICODE_` 形狀;既有 `~/.config/codetrail/compaction.json` 條目改成只對 `docs/troubleshooting.md` 以外的文件生效(升級段必須點名這個狀態檔;`test_user_docs_must_not_teach_removed_flags_or_files` 餵的 README 字串仍要被抓);`opencode-ai` 反向檢查保留。
- `core_only` 收緊的後果:啟動核心腳本的字串常數也不得再提 `AICODE_*`(`deployment_profile.py:462, 496-503, 782, 1023`、`launch_servers.py:361, 411`、`stop_servers.py:51, 281`、`check_status.py:31-33, 100`、`run_tests.py:128, 131`、`set_config.py:2805-2816`)——各 owner 改字,D1 不代改。

### 6.2 升級指引(D2;`docs/troubleshooting.md` 合併成一段,README §1.2 / 升級 bullet / `docs/security.md:135` 只留一句指向它)
1. 前提:本版不再附帶 `opencode_migrate.py` 與 `opencode_plugins/`;runtime 永不讀寫 `~/.config/opencode/*` 與 `~/.config/codetrail/compaction.json`。先看 `~/.config/opencode/opencode.json` 的 `plugin` 陣列:CodeTrail 註冊的項一定是 `<某個 checkout>/opencode_plugins/codetrail-notify.js` / `codetrail-compaction.js` 的**完整路徑**;那個 checkout 就是「原安裝路徑」。
2. 固定舊版(`a1682d5` 是最後一個附帶遷移工具的 commit;這一段的標題必須含 `a1682d5`,那是 gate 放行 `python3 opencode_migrate.py` 的唯一條件)三種情況擇一:(a) 原安裝路徑 = 這份 checkout:`git status --porcelain` 必須為空 → `git checkout --detach a1682d5` → `python3 opencode_migrate.py --check` → `python3 opencode_migrate.py` → `git checkout -`;(b) 原安裝路徑已不存在:`git worktree add <原安裝路徑> a1682d5` 在**同一個路徑**重建,於該目錄執行同兩條命令,再 `git worktree remove <原安裝路徑>`;(c) 原安裝路徑是另一份仍存在的安裝:到那份 checkout 用它自己的工具。限制:工具只認 `PLUGIN_DIR` = 執行它的 checkout 路徑,換路徑執行會回報「無需變更」而殘留仍在;`--check` 回報 foreign / 無法確認時零寫入,不得改用手動路徑硬刪。
3. 完全手動(不做 git 操作時):(a) 只刪 `plugin` 中路徑**逐字等於** `<原安裝路徑>/opencode_plugins/codetrail-{notify,compaction}.js` 的項,其他同名項是別人的;(b) `compaction.*` 只在 `~/.config/codetrail/compaction.json` 存在、其 `config.path_hash` / `real_path_hash` 對應的是正在編輯的這份 `opencode.json`(state 綁單一 config)、且該鍵現值與 `managed.<key>.value` **JSON 型別嚴格相等**時,才依 `managed.<key>.prior` 還原(`present: true` 寫回 `prior.value`,`false` 刪鍵);任一條件不成立就保留現值;(c) `mcp.codetrail` 與 `permission` 不動;(d) 只有 (b) 對每個受管鍵都處理完(還原或確認保留)才刪狀態檔;無法判斷就留著它。
4. 驗證只准用合成設定(tmp HOME 下自建 `opencode.json` 與狀態檔);本次施工不在施工機器執行遷移、不 checkout 舊版、不動外部設定。
5. 升級注意:`deployment.json` 新增 `llama_bin` / `services.<role>.gpu` 後,共用 `~/.config/codetrail/` 的舊世代 checkout 之 loader 會對未知鍵 fail-loud(`_unknown_keys`),重跑本版 `./set_config.sh` 也會覆寫 `~/start.sh`。

### 6.3 AGENTS.md / README_DEV(D1 / D2)
- §2 刪三條(`opencode_migrate`、ownership 狀態檔、`opencode_plugins/*.js`);`compaction_formula` 條改常數名;「兩份安裝並存」條末句改「main runtime 永不讀、寫、刪 `~/.config/codetrail/compaction.json`、`~/.config/opencode/*` 與任何舊 plugin 路徑;舊安裝的還原只走 docs/troubleshooting.md 的固定舊版 / 手動路徑」。
- §2 新增:`client_engine` 條加「`load_session` 是唯一一次受信讀取,模型歷史與畫面歷史同源;`adopt` 之前 engine 零改動;transcript 只以標記呈現 compaction」;`client_app` 條加「切換 / 啟動接續必須重播原始記錄(文字、工具含 structured、reasoning、壓縮標記),工具結果按宣告群組配對,busy 或核准中不得換,失敗保持 session 與畫面,重播 block 不登記給即時事件」;`client_store` 條加「大綱只取本地真實 user 訊息,零 LLM、零寫入」;新增「啟動核心」條:「GPU、llama-server 路徑、tmux session 名、逾時與 rollback 只來自 `deployment.json`、repo 常數與 argv;tmux pane 一律經 `deployment_profile.py exec`,最終環境由 `process_env.llama_server_env()` 決定(四前綴 + `LLAMA_ARG_*` + `CUDA_VISIBLE_DEVICES` 剝除、只重新輸出驗證過的 gpu);`~/start.sh` 不得 export / unset」。
- §3「不要新增 `os.environ` 讀取」刪掉啟動核心的例外句;允許清單維持 `HOME` / `XDG_*` / `PATH` / `PYTHONIOENCODING`(寫入)/ `PYTEST_*`(寫入)。
- README_DEV:維護命令索引刪 `opencode_migrate.py` 兩行;`AICODE_TEST_JOBS=1 …` → `--jobs 1`;測試指南刪 `test_opencode_migrate.py`;刪 `MANAGED_COMPACTION_KEYS` bullet;eval 段 `LLAMA_BIN=…` → `--llama-bin`。README §3.2 / §3.3 / §4.0-4.2、`docs/setup.md` systemd(只有 `ExecStart=… deployment_profile.py exec main`,GPU 與 binary 來自 `deployment.json`)、`docs/deployment-profiles.md`(env 覆寫段刪;GPU precedence = `--<role>-gpu` > `--aux-gpu` > `services.<role>.gpu` > 不指定)、`eval/spec_questions.json` `spec_001` / `spec_003` gold 改 `main.model` / `PATCH_ENABLED`(與 `config.py:1074/1127` 註解刪除同步)。

## 7. 驗收清單(交付逐條打勾;a/b/c 對應使用者三項任務)
- [ ] a:`/resume <id>`、`/session <id>`、選單、`aicode -c`、`aicode --session <id>` 五條路都重播原始記錄;壓縮過的 session 原文仍見、標記正確、模型歷史仍 compacted;工具區塊有 summary / 狀態 / content + structured、pending 字樣、orphan 標示;`/thinking` 對重播 reasoning 生效;busy 拒絕;失敗零改動;`/new` 清畫面;`_tools` 不含重播 block;大綱零寫入;`client_store` 防線 diff 為零;§5.1 紅綠證據附上。
- [ ] b:`git ls-files | grep -i opencode` 只剩 `support_matrix.json` 與本交接目錄;`grep -rin opencode --include=*.py --include=*.js --include=*.sh .`(排除 tests/、docs/workflows/)只命中四個 `.py` 的允許形狀;`doctor.py` 無 `check_legacy_opencode_install`;三常數改名值不變且 import 期 fail-loud 續在;`STRIPPED_ENV_PREFIXES` 仍含 `OPENCODE_`;共用檔名未改;施工機器 `~/.config/opencode` 與 `~/.config/codetrail/compaction.json` 零寫入(以 `ls -la --time-style=full-iso` 前後對照)。
- [ ] c:`grep -rn "os.environ\|getenv" --include=*.py .`(排除 tests/)每一處都是 HOME / XDG / PATH / 寫入 PYTEST_* / PYTHONIOENCODING 或 `process_env`;產生的 `~/start.sh` 無 `export` / `unset`;`deployment.json` 含 `llama_bin` 與四個 `gpu`;exec 契約證明 pane 環境乾淨;README / docs 無任何 `export AICODE_*` / `MAIN_GPU=` / `LLAMA_BIN=` / `Environment=` 教學;三個靜態檢查綠;三個 gate 的反例自測仍紅;`SAFETY_MODULES` 沒有指到不存在的 node。

## 8. 決策紀錄
| ID | 決定 | 理由 |
|---|---|---|
| D-a3 | **撤回**初稿的「不附原文」;畫面永遠顯示原始記錄 | B-01 |
| D-a4 | 啟動重播靠 `Engine.resumed_snapshot`,不改 `_build` / `command_chat` 形狀 | 同一次讀取共享;同一條 regression 在紅與綠之間不必改 |
| D-b3 / D-b4 | 刪 `combine_settings`;`opencode_effective_chars` 只改 live 名稱與新輸出,讀取相容,資料檔不動 | I-8;使用者要求「仍用變數移歸宿」但不重造歷史 schema |
| D-c1 | `deployment.json` 加鍵(不另開 `launcher.json`);舊世代 fail-loud 寫進升級注意 | 使用者指定落點;Astra 未列 Blocker |
| D-c2 / D-c5 | `CUDA_VISIBLE_DEVICES` 只當輸出;`LLAMA_ARG_*` 一併剝;`GGML_*` / `LLAMA_LOG_*` 刻意保留(非 CodeTrail 設定、非 argv 可替代的入口) | B-03;邊界要小而明確 |
| D-c3 / D-c4 | tmux session 名常數 + SUPPRESS argv;`AICODE_RERANK_FALLBACK_POLICY` 直接刪 | 初稿決定,Astra 未反對 |
| D-c6 | pane 經 `deployment_profile.py exec` 而不是在 launcher 組 `env -u …` | tmux 全域 / session 環境列舉脆弱且不得改使用者共用 tmux;exec 是 systemd 已用的同一條路 |
| D-g1 | OpenCode gate 掃註解全文;allowlist 外的歷史註解改字為「舊世代前端」 | 初稿 §9-2,Astra 未反對;歷史說明不再與現況漂移 |

## 9. 執行與提交
- 編排者只 commit `docs/workflows/session-opencode-cleanup/`(使用者已授權);runtime / tests / AGENTS 留在工作樹供 review,未經使用者確認不 commit;**禁止 push**。
- Astra 的 code review 只對 `a1682d5` 之後本任務的改動提 Blocker;Fable 修復不得擴張到未動的碼;同一分歧在兩輪審核 / 修復後未解才登記 `deferred.md`,push 前告知使用者,不得預先把未達驗收的項目擱置。
