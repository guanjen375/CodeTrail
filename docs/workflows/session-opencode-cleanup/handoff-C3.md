# handoff C3(W2):啟停腳本 / run_tests / eval binary —— I-7 已落檔

只動 `plan-final.md` §3 的 C3 那一列(`scripts/launch_servers.py`、`scripts/stop_servers.py`、
`scripts/check_status.py`、`scripts/run_tests.py`、`eval/record_semantic_vectors.py`、
`tests/test_server_scripts.py`、`tests/test_test_runner.py`)與本檔。沒有 commit / push /
stash / checkout;**沒有執行任何 pytest**(`compileall` 與自寫的靜態 AST 掃描除外);
沒有碰真服務、tmux、`~/.config`、`~/start.sh`、真實 session。

依 `handoff-C1.md` 宣告的 I-5 / I-6 實際簽名施工(含它的三項偏差:
`add_loader_arguments(parser, *, suppress_defaults=False)`、`_gpu_for` 私有簽名、
`llama_bin` 一律絕對路徑)。C1 列的三個跨 owner 斷點裡屬於我的兩個已修:
`tests/test_server_scripts.py:24` 的 `RUNTIME_OVERRIDE_ENV_KEYS` import、
`scripts/check_status.py` 的 `inspect_deployment(environ=…)`。

## 1. 落檔的介面

### `scripts/launch_servers.py`

```python
def _positive_int(value: str) -> int                      # argparse type;0 / 負數 / 非數字 → ArgumentTypeError
def _scope_roles(scope) -> tuple[str, ...]                # 不變
def _sessions(args: argparse.Namespace) -> dict[str, str] # TMUX_SESSIONS + 隱藏 --main-session/--aux-session
def _pane_command(role: str, args) -> str                 # shlex.join([sys.executable, <repo>/deployment_profile.py, "exec", role, *loader_argv(args)])
def _health_timeout(role, explicit: int | None = None, artifact_bytes: int = 0) -> int
def _command_for(service, profile: DeploymentProfile, *, must_exist: bool) -> list[str]   # 只給 dry-run
def _print_dry_run(profile, roles, args) -> None
def _start_role(service, command_line: str, session, *, first_in_session, log_dir=None)   # 第二個參數改成「已組好的命令列字串」
def _state_log_dir(environ: Mapping[str, str] | None = None) -> Path                      # 預設讀 os.environ 的 XDG_STATE_HOME / HOME
def _rollback_started(reason, started_roles, created_sessions, sessions, *, log_dir: Path, keep_on_failure: bool = False) -> None
def launch(profile: DeploymentProfile, roles: Sequence[str], args: argparse.Namespace) -> None
def _parser() -> argparse.ArgumentParser
def main(argv=None) -> int
```

argv(I-7):`--scope`(必填)、`--dry-run`、`--health-timeout N`、`--keep-on-failure`、
`--main-session` / `--aux-session`(`SUPPRESS`)、加上 `add_loader_arguments(parser)` 全組。
`main()` = `load_effective_profile(**loader_kwargs(args))` → `launch(profile, roles, args)`。
刪除:`_cli_environment`、`dict(os.environ)`、`LLAMA_BIN` / 五個 `*_GPU` / `AICODE_MODEL` /
`MAIN_CTX|BATCH|UBATCH` / `MAIN_SESSION|SESSION|AUX_SESSION` / `MAIN_HEALTH_TIMEOUT` /
`RAG_HEALTH_TIMEOUT` / `AICODE_NO_ROLLBACK` / `AICODE_RERANK_FALLBACK_POLICY` 讀取。
所有 spawn 改 `process_env.run`(`subprocess` 這個名字整檔消失,連 `CalledProcessError` /
`DEVNULL` 都改用 `process_env.` 的 re-export)。

### `scripts/stop_servers.py`

```python
DEFAULT_STOP_TIMEOUT = 120                                  # 原 _DEFAULT_STOP_TIMEOUT,改公開(launcher rollback 用)
def _positive_int(value: str) -> int
def _sessions(scope: str, args: argparse.Namespace) -> tuple[str, ...]
def _parser() -> argparse.ArgumentParser                    # 新增(從 main() 抽出來,讓 argv 契約不必啟動 main 就能驗)
def main(argv=None) -> int
```

argv:`--scope`(必填)、`--force`、`--timeout N`(預設 120)、`--main-session` /
`--aux-session`(`SUPPRESS`)、`add_loader_arguments(parser)`(取代原本自己那個 `--profile`)。
刪除:`_stop_timeout()` 與 `AICODE_STOP_TIMEOUT` / 三個 session 環境變數。
spawn 全部改 `process_env.run`;`os` 只剩 `os.kill`。**整檔零 `os.environ`**。

### `scripts/check_status.py`

```python
def _positive_int(value: str) -> int
def _snapshot_reader(path: Path) -> Callable[...]           # 錯誤訊息改指 --snapshot
def main(argv=None) -> int
```

argv:`--strict`、`--no-network`、`--expected N`(預設 4)、`--proc-root` / `--snapshot`
(`SUPPRESS`)、`add_loader_arguments(parser)`。刪除 `EXPECTED_LLAMA_SERVERS` /
`AICODE_STATUS_PROC_ROOT` / `AICODE_STATUS_SNAPSHOT`,以及 `inspect_deployment(environ=…)`
那個已被 C1 刪掉的參數。**整檔零 `os.environ`、零 `import os`**。

### `scripts/run_tests.py`

```python
def _validate_jobs(raw: str) -> int                         # 1..16,否則 ValueError("--jobs 必須是 1..16 的整數…")
def parse_jobs(argv: Sequence[str]) -> tuple[int | None, list[str]]   # 抽掉 --jobs N / --jobs=N,其餘原封不動回傳
def _resolve_parallel_jobs(jobs: int | None = None, *, cpu_count: int | None = None) -> int
```

`main()` 先 `parse_jobs(argv)` **再** `parse_selection(argv)`,所以 `--jobs 1 -m smoke` 仍是
「純 `-m`」那個並行形狀,而 `--jobs 2 tests/x.py::y` 的 `--jobs` 不會被當成 pytest 參數轉發過去。
壞值(非整數 / 0 / >16 / 給兩次 / 少了值)一律 exit 2 並印原因。刪除 `AICODE_TEST_JOBS`。

### `eval/record_semantic_vectors.py`

```python
def _llama_build(llama_bin: str | None = None) -> dict
def record(argv_model: str | None, llama_bin: str | None = None) -> int
```

argv 新增 `--llama-bin PATH`。路徑優先序:`--llama-bin` > `load_effective_profile().llama_bin`
> `PATH` 上的 `llama-server`;都找不到仍是既有的 `{"revision": "unknown", "reason": …}`
(不猜版本號)。刪除 `LLAMA_BIN` 讀取與那一行 `import os`。**整檔零 `os.environ`**。

### 介面偏差(3 項,對照 §2 / I-7)

1. **`launch(profile, roles, args)`**(§2 未指定簽名;原本是 `launch(profile, roles, environ, *, dry_run)`)。
   `--dry-run` / `--health-timeout` / `--keep-on-failure` / session 名 / `loader_argv` 都要
   同一份 namespace 才轉發得出去,再拆成 5 個關鍵字參數只是把同一組值抄兩遍。
   `_rollback_started` 同理改成 `*, log_dir, keep_on_failure`(`log_dir` 交進去而不是自己
   從環境算,測試才不必動 HOME)。
2. **`_start_role` 的第二個參數由 `Sequence[str]` 改成 `str`**。pane 跑的是
   `deployment_profile.py exec …`,由 `_pane_command()` 用 `shlex.join` 組好;
   `_start_role` 不再自己 join。
3. **`stop_servers._parser()` 是新符號**(§2 沒有列)。`--timeout` 的三個邊界
   (預設 / 合法值 / 壞值 fail-loud)不該為了驗證而去跑會關 tmux session 的 `main()`。
   `_DEFAULT_STOP_TIMEOUT` → `DEFAULT_STOP_TIMEOUT`(公開,launcher rollback import 它)。

### 計畫沒列、但一併做了的一件小事

`~/start.sh --dry-run` 的輸出**多一行 `llama_bin=<絕對路徑>`**(在 `profile_hardware=` 之後)。
二進位路徑從此只來自檔案 / argv,dry-run 是使用者唯一看得到「這次會執行哪一顆 llama-server」
的地方。沒有任何程式解析這份輸出(`set_config.preview_start_commands` 只是原樣印出)。
反向:`rerank_fallback_policy=` 那一行連同 `AICODE_RERANK_FALLBACK_POLICY` 一起刪(I-7 指定)。

## 2. test-changes

### `tests/test_server_scripts.py`

**新增(3 條契約,全部 `@pytest.mark.smoke` 單條 decorator;§5.2)**

| 測試名 | 守什麼 |
|---|---|
| `test_the_pane_runs_the_exec_choke_point_with_the_loader_argv` | tmux respawn 的命令列**逐字**等於 `shlex.join([python, <repo>/deployment_profile.py, exec, main, --profile …, --llama-bin …, --main-gpu GPU-7])`;session 名取自 `TMUX_SESSIONS`;pane 命令裡不得出現 llama-server 的 argv(`-ngl` / `CUDA_VISIBLE_DEVICES`)。fake tmux = 換掉 `process_env.run` 的錄影替身 |
| `test_the_exec_path_hands_llama_server_a_clean_environment` | tmp HOME 的 `deployment.json` 指到一支會 `env` dump 的假 llama-server;以含 `CUDA_VISIBLE_DEVICES=7`、`LLAMA_ARG_THREADS=3`、`OPENCODE_API_KEY`、`AICODE_MODEL`、`MARK=keep` 的殼層跑 `deployment_profile.py exec main` 與 `exec embedding`:main 拿到檔案裡的 `GPU-FILE`、embedding 完全沒有 selector、四者皆不見、`MARK` 與 `HOME` 仍在 |
| `test_stop_and_status_use_argv_and_constants_not_the_shell` | PATH 前置假 tmux / nvidia-smi / ss;殼層的 `MAIN_SESSION` / `SESSION` / `AUX_SESSION` / `EXPECTED_LLAMA_SERVERS=99` 全部無效(stop 只碰 `codetrail-main` / `codetrail-rag`,status 預設仍是「預期至少 4」),`--expected 5` 才會改判 |

**新增(1 條,補上被刪測試留下的缺口)**

| 測試名 | 為什麼要有 |
|---|---|
| `test_aux_models_and_gpus_come_from_the_deployment_file` | 取代下面被刪的 `test_legacy_aux_launcher_env_names_still_override_profile`:那條是唯一覆蓋「三顆附屬模型 + 三張卡真的被解析出來」的測試。行為的來源從環境變數換成 `deployment.json`,但**這件事本身仍要有人守**,否則 `services.<role>.gpu` 這個新鍵被忽略是靜默的(dry-run 照樣成功,只是四顆 server 全擠在同一張卡)。順帶斷言輸出裡沒有任何 `/shell/` 值,證明 `EMBED_MODEL` / `LLAMA_BIN` / `MODELS_DIR` 這些名字已經沒有作用 |

**刪除(3 條)**

| 測試名 | 理由 |
|---|---|
| `test_legacy_aux_launcher_env_names_still_override_profile` | 它守的正是要移除的行為(`EMBED_MODEL` / `RERANK_MODEL` / `VL_GGUF` / `VL_MMPROJ` / 三個 `*_GPU` 覆寫 profile)。§5.3 指定刪;覆蓋由上面那條新測試接手 |
| `test_rollback_respects_no_rollback_env` | 改名 → 見下(機制從環境變數換成旗標,名字裡的 `env` 會描述一條不存在的路徑) |
| `test_stop_timeout_env_override_and_fallback` | 改名 → 見下(同上;而且「壞值只印警告然後退回預設」這個行為本身也變了:argparse 直接 fail-loud) |

**改名(2 條)**

| 舊名 → 新名 | 行為為什麼該變 |
|---|---|
| `test_rollback_respects_no_rollback_env` → `test_rollback_respects_the_keep_on_failure_flag` | 「保留現場」的來源從 `AICODE_NO_ROLLBACK=1` 變成 `--keep-on-failure`:同一台機器上另一份安裝的 `~/start.sh` export 了它,就會讓這一份啟動失敗後留下卡住下一次啟動的 session。新測試同時斷言旗標的預設值是 `False` |
| `test_stop_timeout_env_override_and_fallback` → `test_stop_timeout_comes_from_argv_with_a_fixed_default` | 等 VRAM 釋放的上限現在只有 `--timeout` 與 repo 常數兩個來源;壞值改成解析階段 fail-loud,而不是印一行警告後靜靜用預設值(那行警告在 `~/start.sh` 的輸出裡很容易被略過) |

**改輸入機制 / 斷言(逐條)**

| 測試名 | 動作 | 行為為什麼該變 |
|---|---|---|
| `test_start_all_routes_main_and_all_aux_to_their_shared_gpus` | `AICODE_PROFILE` / `AICODE_MODEL` / `MAIN_GPU` / `AUX_GPU` 四個 env → `--profile` / `--main-model` / `--main-gpu` / `--aux-gpu`;加斷言:殼層那三個值(`SHELL-MAIN-GPU`、`SHELL-AUX-GPU`、`CUDA_VISIBLE_DEVICES=9`)不得出現在輸出裡 | GPU 與模型只來自檔案 / argv;「殼層無效」要有人守,不然回歸是靜默的 |
| `test_start_main_fails_loud_when_no_main_model_is_set` | 加斷言 `never-used.gguf`(殼層 `AICODE_MODEL` 的值)不得出現 | 同上:沒設就是要失敗,不能從殼層撿一個 |
| `test_quit_still_kills_sessions_when_deployment_config_is_broken` | session 名由 `MAIN_SESSION` / `SESSION` env → `--main-session` / `--aux-session` | 測試仍然不能碰開發機真的 session,而它們已經是隱藏旗標 |
| `test_launcher_rejects_duplicate_service_ports` | `AICODE_DEPLOYMENT_CONFIG` / `AICODE_MODEL` → `--deployment-config` / `--main-model` | 兩者都改成行程之間交檔案的 argv |
| `test_health_timeout_scales_with_model_size` | `_health_timeout(role, environ, bytes)` → `(role, explicit, bytes)`;加三行 argparse 斷言(預設 `None`、`--health-timeout 42`、`--health-timeout 0` 被擋) | 覆寫來源從兩個環境變數變成一個旗標;0 不再是「靜默當沒設」 |
| `test_start_rag_servers_dry_run_uses_base_url_ports` | 三個 `AICODE_LLAMA_*_BASE_URL` + 三個 `*_GPU` env → `deployment.json` 的 `port` / `base_url` / `gpu`;加斷言 `rerank_fallback_policy` 不得再出現在 dry-run | endpoint / GPU 只剩檔案一個來源;`AICODE_RERANK_FALLBACK_POLICY` 已刪除、唯一來源是 `client.json` |
| `test_start_rag_servers_noncausal_models_use_full_physical_batch` | 改用共用的 `_run_launcher`(tmp HOME) | `_isolated_env` 依賴已刪的 `RUNTIME_OVERRIDE_ENV_KEYS` |
| `test_start_role_pipes_server_output_to_persistent_log` | `_start_role` 第二參數改成 `_pane_command("main", …)` 的字串;`respawn[-1]` 斷言從 `"llama-server -m x"` 改成「等於那個 pane 命令」且含 `deployment_profile.py exec main` | pane 改跑 exec choke point 是 B-03 的行為變更 |
| `test_start_role_warns_when_pipe_pane_fails`、`test_start_role_warns_when_log_dir_unwritable` | 同上改字串;monkeypatch 目標 `launch_servers.subprocess` → `launch_servers.process_env` | spawn 的唯一出口換了 |
| `test_rollback_saves_logs_and_kills_created_sessions` | `environ={"HOME": …}` → `log_dir=_state_log_dir({"HOME": …})`;fake 改走 `_fake_tmux` | log 目錄改成交進去的參數(rollback 不再自己讀環境) |
| `test_rollback_noop_when_nothing_created`、`test_rollback_waits_for_vram_release` | 同上加 `log_dir=`;後者的等待上限斷言改成 `stop_servers.DEFAULT_STOP_TIMEOUT`(並保留 `== 120` 的字面斷言) | 上限來自 repo 常數而不是 `AICODE_STOP_TIMEOUT` |
| `test_launch_registers_session_before_start_role_failure`、`test_launch_rolls_back_on_keyboard_interrupt`、`test_ready_message_uses_absolute_status_path` | `launch(profile, roles, environ, dry_run=False)` → `launch(profile, roles, args)`;`_rollback_started` 的替身簽名補 `**_kw`;`subprocess.CalledProcessError` → `process_env.CalledProcessError`(同一個類別) | `launch()` 的第三個參數改成解析後的 argv |
| `test_main_returns_130_on_keyboard_interrupt` | `load_effective_profile` 的替身簽名 `(_env, profile=None)` → `(**_kwargs)` | 呼叫端改成 `load_effective_profile(**loader_kwargs(args))` |
| `test_pane_pids_parses_tmux_output`、`test_pane_pids_empty_when_session_missing` | monkeypatch 目標 `stop_servers.subprocess` → `stop_servers.process_env` | 同上 |
| `test_check_status_passes_with_four_unique_llama_server_pids`、`test_check_status_report_only_mode_does_not_fail_the_shell`、`test_check_status_strict_mode_fails_for_too_few_unique_pids` | 只動共用的 `_run_check_status`:改用 tmp HOME 的殼層並固定帶 `--proc-root <空目錄>` | `AICODE_STATUS_PROC_ROOT` 沒了;而且這三條本來會去讀真的 `/proc`(PID 101-104 撞上真實 process 就是環境相依的假紅燈) |
| `test_systemd_exec_path_also_warns_before_launching` | `_clean_env` → `_shell_env` | helper 改名(見下) |

**fixture / helper / import / 模組常數**

| 名稱 | 動作 | 理由 |
|---|---|---|
| `_clean_env(tmp_path)` | 刪,改成 `_shell_env(tmp_path, **extra)` | 以前是「把會影響 loader 的名字濾掉」;現在沒有名字會影響 loader,helper 的用途反過來變成「把它們統統設進去,證明無效」 |
| `LEGACY_SHELL_OVERRIDES`(新模組常數) | 新增:29 個已死的名字 → 假值(`AICODE_*`、五個 `*_GPU`、`CUDA_VISIBLE_DEVICES`、`LLAMA_BIN`、`MODELS_DIR`、三個 session 名、兩個 health timeout、`EXPECTED_LLAMA_SERVERS` …) | 每個子行程測試都自動帶著這組污染;漏改的讀取會立刻讓某條測試拿到 `/shell/...` 而失敗 |
| `_write_deployment(tmp_path, services=None, **top)` | 新增 | 設定的唯一來源變成 `~/.config/codetrail/deployment.json`,四個測試共用 |
| `_launch_args(*argv)` | 新增(`launch_servers._parser().parse_args`) | `launch()` 現在收 namespace;測試照使用者同一條路產生它 |
| `_run_launcher(entry, tmp_path, env_extra, *args)` | 刪掉 `env_extra` 參數 | 改完之後每個呼叫端都傳 `{}`;留著只會讓人以為還有環境入口 |
| `_isolated_env(tmp_path)` | 刪 | 依賴已刪的 `deployment_profile.RUNTIME_OVERRIDE_ENV_KEYS`(§4 已知斷點);兩個呼叫端改用 `_run_launcher` |
| `_fake_tmux(monkeypatch, calls, *, stdout="")` | 新增 | 三個 rollback / start_role 測試各自抄一份 fake `subprocess.run`;spawn 出口改成 `process_env.run` 之後統一成一個 |
| `_write_fake_bin(directory, name, body)` | 新增 | PATH 上的假 tmux / nvidia-smi / ss;`_write_fake_nvidia_smi` 改成呼叫它 |
| `_fake_profile()` → `_fake_profile(llama_bin="/bin/true")` | 加 `llama_bin` / `registry_file` 兩個屬性 | `launch()` 現在從 profile 拿二進位路徑與 registry |
| `_patch_launch_scaffolding(monkeypatch, tmp_path, *argv)` | 回傳 `(profile, args)`(原本回傳一份 environ dict);`resolve_model_reference` 替身簽名改成 `(value, environ=None, *, must_exist=False, registry_file=None)`;不再 patch `_command_for`(真實路徑已經不呼叫它) | 對齊 C1 的新簽名與「pane 命令不再由 launcher 組 llama argv」 |
| import 區 | `+import argparse`、`+import process_env`;`from deployment_profile import RUNTIME_OVERRIDE_ENV_KEYS, ServiceProfile` → `TMUX_SESSIONS, ServiceProfile` | 新 helper 要 namespace 與 `CalledProcessError`;`RUNTIME_OVERRIDE_ENV_KEYS` 已刪 |

**未改**:module 層沒有 `pytestmark`(本檔一直是逐條 decorator,改完仍然只有新增那 3 條帶
smoke);`_wait_released` 那五條、`_Clock`、`_service`、`_write_profile_fixture`、
`_write_vl_cpu_moe_config`、三條 CPU-MoE 警告測試、`test_artifact_bytes_sums_all_shards`、
`test_launcher_help_paths_are_offline` 的斷言一字未動。

### `tests/test_test_runner.py`

| 測試 / fixture | 動作 | 行為為什麼該變 |
|---|---|---|
| `test_parallel_job_resolution` | 參數化的第一欄 `env: dict` → `jobs: int | None`;呼叫改 `_resolve_parallel_jobs(jobs, cpu_count=…)` | shard 數的來源從 `AICODE_TEST_JOBS` 換成 `--jobs`;`(None, 1, 1)` / `(None, 64, 16)` 兩格(cpu 推導)一字未改 |
| `test_parallel_job_resolution_rejects_invalid_values` | 改成 `parse_jobs(["--jobs", value])`,`match` 由 `AICODE_TEST_JOBS` 改 `--jobs`;四個壞值(`0` / `17` / `many` / `1.5`)原樣保留 | 驗證點搬到真正的入口:壞值要在轉發之前就被擋下,而不是只有並行模式才檢查 |
| `test_jobs_is_eaten_before_the_shape_is_decided`(新增) | — | `--jobs` 必須在形狀判斷**之前**被抽掉:漏抽的話 `--jobs 1 -m smoke` 會掉進逐字轉發(整包序列跑,沒人會發現),而 `--jobs 2 tests/x.py::y` 會把 `--jobs` 丟給 pytest 變成 usage error |
| `test_jobs_rejects_ambiguous_argv`(新增) | — | `--jobs` 少了值 / 給兩次 / `--jobs=` 都不能用猜的(「哪一個生效」猜錯 = 靜默改變並行度) |

module 層 `pytestmark = pytest.mark.smoke` 未動,所以新增的兩條自動是 smoke。

## 3. 給其他 owner 的字

### 給 D1(`tests/test_repo_consistency.py`、`tests/test_smoke_gate.py`、`AGENTS.md`)

1. `_ENVIRON_ALLOWLIST`:**移除 `scripts/stop_servers.py`、`scripts/check_status.py`、
   `eval/record_semantic_vectors.py`**(三個檔現在零 `os.environ` / 零 `getenv`,靜態掃過)。
   保留 `scripts/launch_servers.py`(理由改「HOME / XDG_STATE_HOME(log 目錄)」——
   唯一一處是 `_state_log_dir`)與 `scripts/run_tests.py`(理由改「PYTEST_* 寫入 + 子行程環境」;
   `TAIL_ARGS` 從來不存在,`AICODE_TEST_JOBS` 已刪)。
2. `_SPAWN_CORE`:**`scripts/launch_servers.py`、`scripts/stop_servers.py`、
   `scripts/check_status.py` 都可以移除**(三個檔的 spawn 全走 `process_env.run`,
   `subprocess` / `pty` / `os.exec*` 這些名字在 code 行完全不出現,我用 gate 同一套
   `_code_only_source` + `_SPAWN_API` / `_ENV_COPY` 規則靜態複跑過,零 offender)。
   **`scripts/run_tests.py` 與 `eval/record_semantic_vectors.py` 必須留著**:前者要自己
   `Popen` 每個 shard(且刻意複製整份 `os.environ` 再設 `PYTEST_*`),後者要跑
   `llama-server --version`。這與計畫的四檔清單一致。
3. `core` / `core_only` 清空之後我這邊已核對:C3 的 5 個檔裡**字串常數與 docstring 不再出現
   任何 `AICODE_*` / `AI_CODE_*` / `CODETRAIL_*` / `OPENCODE_*` 名字**(AST 掃過,零命中)。
   `_sessions` 的 docstring 提到 `MAIN_SESSION` / `SESSION` / `AUX_SESSION`,那不在
   `(AICODE|AI_CODE|CODETRAIL|OPENCODE)_` 這個 pattern 上,而且句子本身是「以前是…」。
4. `SAFETY_MODULES` 新增一個檔名鍵 **`test_server_scripts.py`**,說明建議寫「啟動核心的
   pane 邊界:tmux pane 一律經 `deployment_profile.py exec`(環境在 pane 內由
   `process_env.llama_server_env()` 算);GPU / llama-server 路徑 / session 名 / 逾時只來自
   `deployment.json`、repo 常數與 argv,殼層裡的舊名字一律無效」,node 是這三條:
   * `test_the_pane_runs_the_exec_choke_point_with_the_loader_argv`
   * `test_the_exec_path_hands_llama_server_a_clean_environment`
   * `test_stop_and_status_use_argv_and_constants_not_the_shell`
5. 我刪 / 改名的 node(其他地方若有引用請同步):刪
   `test_legacy_aux_launcher_env_names_still_override_profile`;改名
   `test_rollback_respects_no_rollback_env` → `test_rollback_respects_the_keep_on_failure_flag`、
   `test_stop_timeout_env_override_and_fallback` → `test_stop_timeout_comes_from_argv_with_a_fixed_default`。
   目前 `SAFETY_MODULES` 沒有 `test_server_scripts.py` / `test_test_runner.py` 鍵,所以這三個
   名字不在既有 manifest 裡(我 grep 過)。
6. 可做可不做:`tests/test_test_runner.py` 整檔是 module 層 smoke,但不在 `SAFETY_MODULES`;
   計畫沒有要求登記,我不擅自加。
7. `AGENTS.md` §1.1 目前寫「帶任何 pytest 參數＝單行程逐字轉發」——`--jobs N` 是唯一一個
   **不是** pytest 參數、會被吃掉的旗標。要不要補一句由 D1 判斷(§6.3 沒有列這條)。

### 給 D2(文件)

* `scripts/launch_servers.py`:`--scope main|aux|all`(必填)、`--dry-run`、
  `--health-timeout N`(取代 `MAIN_HEALTH_TIMEOUT` / `RAG_HEALTH_TIMEOUT`;預設 main 依模型
  大小 300..1800、附屬 60)、`--keep-on-failure`(取代 `AICODE_NO_ROLLBACK=1`),
  再加 loader 全組(`--profile` / `--llama-bin` / `--main-model|-ctx|-batch|-ubatch` /
  `--main-gpu|--aux-gpu|--embed-gpu|--rerank-gpu|--vl-gpu`)。`~/start.sh` 的 `"$@"` 會原樣
  轉發,所以文件可以直接寫 `~/start.sh --keep-on-failure`。
* `scripts/stop_servers.py`:`--scope aux|all`、`--force`、`--timeout N`(預設 120,
  取代 `AICODE_STOP_TIMEOUT`)+ loader 全組。
* `scripts/check_status.py`:`--strict`、`--no-network`、`--expected N`(預設 4,取代
  `EXPECTED_LLAMA_SERVERS`)+ loader 全組。`--proc-root` / `--snapshot` 是隱藏的測試旗標,
  **不要寫進使用者文件**。
* `scripts/run_tests.py`:`--jobs N`(1..16;取代 `AICODE_TEST_JOBS`)。README_DEV 的
  `AICODE_TEST_JOBS=1 python3 scripts/run_tests.py` → `python3 scripts/run_tests.py --jobs 1`。
* `eval/record_semantic_vectors.py`:`--llama-bin PATH`(取代 `LLAMA_BIN`;預設讀
  `deployment.json` 的 `llama_bin`)。
* 直接刪除、沒有替代:`AICODE_RERANK_FALLBACK_POLICY` 與 `~/start.sh --dry-run` 輸出裡的
  `rerank_fallback_policy=` 那一行(唯一來源是 `client.json` 的 `rerank_fallback_policy`)。
* `~/start.sh --dry-run` 的輸出**多一行 `llama_bin=<絕對路徑>`**;`{role}_command=` 仍然是
  最終的 llama-server argv(不是 pane 的 exec 命令)。
* tmux session 名(`codetrail-main` / `codetrail-rag`)現在是 repo 常數
  `deployment_profile.TMUX_SESSIONS`,文件不要再教用環境變數改名。

### 給 C2(`scripts/set_config.py`)

0. `handoff-C2.md`「給 C3」列的三個依賴**全部成立、形狀沒變**:(a) `{role}_command=` 仍是
   最終 llama-server argv(`-c 65536` 這種形狀可 grep);(b) GPU 前綴仍是
   `env CUDA_VISIBLE_DEVICES=<services.<role>.gpu>`;(c) 二進位路徑取自 `deployment.json` 的
   `llama_bin`(經 `profile.llama_bin`)。`--deployment-config` / `--model-registry-file` 由
   `add_loader_arguments` 掛在 launcher 頂層,`preview_start_commands` 那一行可以照原樣用。
1. 唯一的輸出差異:dry-run **多一行 `llama_bin=<絕對路徑>`**、**少一行
   `rerank_fallback_policy=`**——如果 `test_set_config.py` 有斷言預覽輸出的行集合請同步。
2. `_restart_servers` 的 `stop_servers.py --scope all` 不必再交任何環境;要縮短等待可以帶
   `--timeout N`(預設 120)。
3. 產生的 `~/start.sh` 幫助文字若要提到新旗標:啟動端是 `--health-timeout N` /
   `--keep-on-failure`,停止端是 `--timeout N`(`--scope` / `--dry-run` 不變)。
4. `running_codetrail_sessions()` 用 `deployment_profile.TMUX_SESSIONS`(值與現行 fallback
   逐字相同);launcher / stop 兩邊都只認這兩個名字,start.sh 不要再 export session 名。

## 4. 未完成 / 疑點

* **沒有執行任何測試**(C3 不在紅綠證據的範圍內):3 條新契約 smoke、1 條補位測試與所有改過的
  既有測試都是「只寫不跑」,由編排者交付前唯一一次 smoke 驗。
  `Tests: smoke only — reviewer owns full execution.`
* 靜態檢查只跑了 `python3 -m compileall -q <我的 7 個檔>` 與自寫的 AST 掃描(spawn gate /
  environ gate / 模型可見字串 gate 的規則複刻,以及 unused-import 掃描),全部零 offender。
  `check_eval_consistency.py` / `check_readme_consistency.py` 依計畫由 D1 / D2 收尾時跑。
* 新增的 3 條 smoke 裡有 2 條會 spawn python3 子行程(`exec` 契約 2 次、stop/status 契約 3 次),
  估計替 smoke 整包多加約 1 秒。若編排者量到 smoke 逼近 §1.1 的 10 秒目標,先看這裡。
* `test_quit_still_kills_sessions_when_deployment_config_is_broken` 仍然依賴機器上**有** tmux
  (缺 tmux 時 stop 會印 `tmux not found` 而不是 `does not exist`)——這是動工前就有的環境相依,
  我沒有改變它;新的 `test_stop_and_status_use_argv_and_constants_not_the_shell` 則自備假 tmux,
  不受影響。
* `launch_servers.launch()` 的真實路徑不再呼叫 `_command_for`(pane 由 `exec` 自己組指令),
  所以 `_command_for` 現在只服務 dry-run。刻意留著:dry-run 印的必須就是 pane 裡最後會被
  執行的那一份 argv,合併掉它等於讓「預覽」與「實際」各走一條路。
