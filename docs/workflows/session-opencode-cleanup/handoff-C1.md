# handoff C1(W1):啟動核心 loader —— I-5 / I-6 已落檔

只動 `plan-final.md` §3 的 C1 那一列與本檔。沒有 commit / push / stash / checkout;
**沒有執行任何 pytest**(`compileall` 與靜態 AST 檢查除外),沒有碰真服務、tmux、
`~/.config`、真實 session。

**I-5(`process_env.llama_server_env`)與 I-6(loader / overrides / argv / registry 傳遞)
已全部落檔**,C2 / C3 可以照第 1 節的實際簽名開工。

## 1. 落檔的介面

### I-5 `process_env`(與 §2 相同)

```python
STRIPPED_ENV_PREFIXES = ("AICODE_", "AI_CODE_", "CODETRAIL_", "OPENCODE_")   # 不動
SERVER_STRIPPED_ENV_PREFIXES = ("LLAMA_ARG_",)
SERVER_STRIPPED_ENV_KEYS = ("CUDA_VISIBLE_DEVICES",)
def llama_server_env() -> dict[str, str]      # child_env() 再剝上面兩組
```

`child_env()` / `run` / `Popen` / `popen` / `check_output` / `ChildEnvError` / 四前綴清單
**一字未改**。`llama_server_env()` 的唯一消費者是 `deployment_profile.py` 的 `exec`。

### I-6 `deployment_profile`(逐字簽名,C2 / C3 照這份)

```python
DEFAULT_LLAMA_BIN = "~/llama.cpp/build/bin/llama-server"
TMUX_SESSIONS = {"main": "codetrail-main", "aux": "codetrail-rag"}

@dataclass(frozen=True)
class LauncherOverrides:
    main_model: str | None = None
    main_ctx: int | None = None
    main_batch: int | None = None
    main_ubatch: int | None = None
    gpus: Mapping[str, str] = field(default_factory=dict)   # 鍵:main/embedding/reranker/vl/aux
    llama_bin: str | None = None

@dataclass(frozen=True)
class DeploymentProfile:      # 既有七欄不動,新增兩欄(都有預設值,在最後)
    ...
    llama_bin: str = ""              # 絕對路徑:argv > deployment.json 的 llama_bin > DEFAULT_LLAMA_BIN
    registry_file: Path | None = None

def local_override_path(environ=None, *, deployment_config: str | Path | None = None) -> Path | None
def load_effective_profile(environ=None, *, profile: str | None = None,
                           overrides: LauncherOverrides | None = None,
                           deployment_config: str | Path | None = None,
                           model_registry_file: str | Path | None = None) -> DeploymentProfile
def load_model_registry(environ=None, *, registry_file: str | Path | None = None) -> dict[str, str]
def resolve_model_reference(reference, environ=None, *, must_exist: bool = False,
                            registry_file: str | Path | None = None) -> str
def build_server_command(service, llama_bin, environ=None, *, must_exist: bool = False,
                         registry_file: str | Path | None = None) -> list[str]   # 輸出形狀不變
def profile_as_dict(profile, environ=None) -> dict[str, Any]
def add_loader_arguments(parser, *, suppress_defaults: bool = False) -> None
def loader_kwargs(args) -> dict[str, Any]        # → profile= / overrides= / deployment_config= / model_registry_file=
def loader_argv(args) -> list[str]               # 反向
```

`add_loader_arguments` 掛上去的旗標(dest 一併列出,C3 的 launcher 現有旗標名逐字相同):

| 旗標 | dest | 說明 |
|---|---|---|
| `--profile` | `profile` | `"defaults"` 或絕對 JSON 路徑 |
| `--deployment-config` | `deployment_config` | `help=SUPPRESS`;行程間交暫存檔 |
| `--model-registry-file` | `model_registry_file` | `help=SUPPRESS`;同上 |
| `--llama-bin` | `llama_bin` | 絕對路徑(相對路徑 fail-loud) |
| `--main-model` / `--main-ctx` / `--main-batch` / `--main-ubatch` | `main_model` / `main_ctx` / `main_batch` / `main_ubatch` | 後三個 `type=int` |
| `--main-gpu` / `--aux-gpu` / `--embed-gpu` / `--rerank-gpu` / `--vl-gpu` | `main_gpu` / `aux_gpu` / `embed_gpu` / `rerank_gpu` / `vl_gpu` | GPU selector |

`environ` 參數在整個模組裡**只**被讀 `HOME` / `USERPROFILE`(`_home()` 一個出口)。

已刪除:`_ENV_FIELDS`、`RUNTIME_OVERRIDE_ENV_KEYS`、`_environment_overlay`、`_first_env`、
`_parse_env_int`、`_environment_model`、`runtime_environment()`、`env` 子命令、
`MODELS_DIR` 讀取(legacy 路徑固定 `<HOME>/models`)、`local_override_path` 的
`AICODE_DEPLOYMENT_CONFIG` 分支、`cli_env=` 參數。

### I-6 `deployment_status` / `model_resolution`

```python
def query_gpu_processes(run: Callable[..., process_env.CompletedProcess[str]] = process_env.run)
def query_gpu_inventory(run: Callable[..., process_env.CompletedProcess[str]] = process_env.run)
def inspect_deployment(profile, gpu_processes, *, cmdline_reader=read_proc_cmdline,
                       server_reader=query_server, gpu_inventory=None) -> Inspection   # environ= 已刪
def resolve_main_model(env: Mapping[str, str] | None = None, *, profile: str | None = None) -> ModelResolution
```

`deployment_status` 現在**零 `os.environ` / 零 `subprocess`**(registry 走
`profile.registry_file`,spawn 走 `process_env.run`)。

### 介面偏差(3 項,下游請照這份)

1. **`add_loader_arguments(parser, *, suppress_defaults=False)`**(§2 只寫 `(parser)`)。
   `suppress_defaults=True` 時每個旗標帶 `default=argparse.SUPPRESS`,**專供子命令用**。
   理由:pane 的命令是 `exec <role> <loader argv>`(角色在前、設定在後),所以 loader 旗標
   必須同時掛在頂層與每個子命令上;argparse 的 subparser 會把自己的 namespace **整份**
   寫回上層(CPython `_SubParsersAction.__call__`),不 SUPPRESS 的話
   `--profile X exec main` 會被子命令的 `None` 蓋掉。C2 / C3 一律用單參數形式
   `add_loader_arguments(parser)`,行為與 §2 相同。
2. **`_gpu_for` 的內部簽名**改成 `(role, gpu_role, gpus, configured)`(不再收 env);它是私有的,
   §2 只列在刪除清單裡,這裡改成「argv > `--aux-gpu` > 檔案 > 不指定」的純函式。
3. **`llama_bin` 一律要求絕對路徑**(檔案值與 `--llama-bin` 都是),相對路徑丟
   `ProfileError: ... must be an absolute path`。§2 只說「絕對路徑」,這裡把它變成 fail-loud
   而不是靜默相對解析 —— pane 裡 exec 的 cwd 不是使用者打指令的地方。
   set_config 既有行為(把 `--llama-bin ./x` 先轉絕對再寫檔)因此仍然成立。

### 計畫沒列、但一併做了的兩件小事(都在 C1 owner 檔內)

* `profile_as_dict()` 的輸出新增 `"llama_bin"` 鍵(`deployment_profile.py show` 的診斷面);
  repo 內沒有任何消費者解析這份 dict 的鍵集合(已 grep 確認)。
* 刪掉隨 `_environment_overlay` / `_environment_model` 一起失去唯一消費者的
  `_url_with_port()` 與 `_EXTERNAL_PROVIDER_PREFIXES`(私有、repo 內零引用;外部 provider
  的拒絕仍由 `_validate_model_reference` 的形狀檢查與 `model_resolution` 那一份負責)。

## 2. test-changes

### `tests/test_deployment.py`

**新增(4 條,全部 `@pytest.mark.smoke` 單條 decorator;§5.2)**

| 測試名 | 守什麼 |
|---|---|
| `test_the_loader_ignores_every_legacy_override_variable` | 舊 overlay 的每個名字 + `CUDA_VISIBLE_DEVICES` + `LLAMA_BIN` + `MODELS_DIR` + session 名**同時**設進 `os.environ` 與 `environ` 參數,有效 model / ctx / gpu / llama_bin / selected_profile 全部維持檔案值 |
| `test_gpu_and_llama_bin_come_from_the_deployment_file_then_argv` | 缺席 → `DEFAULT_LLAMA_BIN` 展開 + gpu 空字串;檔案 → 檔案值;argv → argv 值(`--aux-gpu` 只套三個附屬角色);相對 `--llama-bin` fail-loud |
| `test_the_server_environment_strips_gpu_selectors_and_llama_settings` | `process_env.llama_server_env()` 剝掉四前綴 + `LLAMA_ARG_*` + `CUDA_VISIBLE_DEVICES`,保留 `GGML_*` / `LLAMA_LOG_*` / `HOME` / 無關變數 |
| `test_the_main_model_resolver_has_no_environment_branch` | 主模型只有 `deployment.json` 一個來源(行為 + 「`model_resolution.py` 原始碼不再出現那個變數名」的靜態半邊 + `resolve_main_model_from_env` 不得存在) |

**改名(3 條)**

| 舊名 → 新名 | 行為為什麼該變 |
|---|---|
| `test_precedence_cli_env_over_local_over_profile_over_defaults` → `test_precedence_cli_overrides_over_local_over_profile_over_defaults` | 優先序這一層裡的「env」不存在了,只剩 argv overrides;層級順序本身不變(§5.3 指定) |
| `test_profile_selector_precedence_cli_then_env_then_local` → `test_profile_selector_precedence_cli_then_local` | profile 選擇的 env 那一格已刪(§5.3 指定);斷言同步改成「沒有 argv 時用 `deployment.json` 的 `profile`」 |
| `test_bind_all_interfaces_via_override_and_env` → `test_bind_all_interfaces_via_the_deployment_file` | §5.3 只要求「去 env 半」;連名字一起改是因為留著 `_and_env` 就是一個描述不存在執行路徑的名字(與 `test_evals.py` 那兩條改名同一個理由)。**這是計畫外的改名,D1 / Astra 請以本表為準。** |

**刪除(1 條)**

| 測試名 | 理由 |
|---|---|
| `test_canonical_n_ctx_override_wins_over_legacy_main_ctx` | 它整條測的是「`AICODE_N_CTX` 勝過 `MAIN_CTX`」——兩個名字都不再被讀,`--main-ctx` 是唯一入口,已由新的 precedence 測試涵蓋(§5.3 指定刪) |

**改輸入機制 / 斷言(逐條)**

| 測試名 | 動作 | 行為為什麼該變 |
|---|---|---|
| `test_absolute_path_profile_inherits_builtin_safe_defaults` | `AICODE_PROFILE=` → `profile=` kwarg | profile 選擇改由 argv 交接 |
| `test_named_profile_references_are_rejected` | 同上 | 同上(錯誤訊息前綴改成 `profile must be ...`,`match="absolute JSON profile path"` 不變) |
| `test_main_model_resolution_uses_selected_profile` | `resolve_main_model_from_env(env(AICODE_PROFILE=…))` → `resolve_main_model(env, profile=…)` | 解析器改名 + profile 走 kwarg |
| `test_explicit_local_override_must_exist` | `AICODE_DEPLOYMENT_CONFIG=` → `deployment_config=`;`match` 由 `AICODE_DEPLOYMENT_CONFIG.*existing file` 改 `deployment-config.*existing file` | 訊息指名的是現在真的存在的旗標;fail-loud 行為本身不變 |
| `test_model_and_mmproj_resolve_from_registry_or_absolute_path` | `AICODE_MODEL_REGISTRY=<json>` + 三個 model env → `models.json` 檔 + `deployment.json` | registry 的唯一來源變成檔案 |
| `test_main_and_aux_gpu_split_and_three_aux_share_one_gpu` | `MAIN_GPU` / `AUX_GPU` env → `LauncherOverrides(gpus=…)` | GPU 來源改 argv / 檔案 |
| `test_per_role_gpu_override_wins_over_aux_gpu` | 同上 | 同上(per-role 勝過 aux 的語意不變) |
| `test_command_builder_uses_only_structured_allowlisted_arguments` | model 改寫進 `deployment.json`、GPU 改 overrides | 同上;`env CUDA_VISIBLE_DEVICES=` 前綴的斷言原樣保留 |
| `test_main_auto_fit_parameters_build_expected_command` | `AICODE_MODEL=` → local override 的 `services.main.model` | 模型來源只剩檔案 |
| `test_auto_fit_parameter_validation_rejects_bad_values` | 拿掉 `AICODE_MODEL=`(該測試不需要 main model) | 同上 |
| `test_cache_ram_is_rejected_for_generating_roles` | 同上 | 同上 |
| `test_bind_defaults_to_loopback_only` | `AICODE_MODEL=` → local override | 同上 |
| `test_bind_rejects_unknown_value_and_preserves_remote_host` | `AICODE_MODEL` / `AICODE_LLAMA_BASE_URL` env → `deployment.json` 的 `model` / `base_url` + `port` | 端點與模型只來自檔案;「非 loopback base_url 不受 bind 預設影響」的斷言不變 |
| `test_status_identifies_all_roles_by_cmdline_port`、`test_status_no_network_still_validates_cmdline_without_health_failure`、`test_status_detects_wrong_gpu_for_aux_role`、`test_status_detects_wrong_loaded_model`、`test_status_requires_observable_vl_mmproj` | 各刪一行 `environ=env,` | `inspect_deployment` 不再收 `environ`;registry 走 `profile.registry_file` |

**fixture / helper / import / 模組常數**

| 名稱 | 動作 | 理由 |
|---|---|---|
| `PROFILE_ENV_KEYS`(模組常數) | 改名 `LEGACY_OVERRIDE_ENV_KEYS`,改成字面 tuple 並補齊 `LLAMA_BIN` / `MODELS_DIR` / session 名 / `AICODE_BIND` / `AICODE_MAIN_CTX` / `MAIN_PORT` | 原本它的用途是「從 `os.environ` 濾掉會影響 loader 的名字」;現在沒有名字會影響 loader,清單改為「證明這些名字無效」的輸入來源(與 C2 的 `_set_config_harness.PROFILE_ENV_KEYS` 同一個轉向) |
| `_env(tmp_path, **values)` | 改寫:不再複製 `os.environ`,只回 `{"HOME", "USERPROFILE", **values}` | loader 只讀這兩個鍵;複製整份環境只會讓「哪些鍵有影響」看不出來 |
| `_write_local(tmp_path, data)` | `mkdir(parents=True)` → `mkdir(parents=True, exist_ok=True)` | 同一個測試現在可能先寫 `models.json` 再寫 `deployment.json` |
| `_write_registry(tmp_path, entries)` | 新增 helper | registry 從 env 搬到 `~/.config/codetrail/models.json` |
| `_fixture(tmp_path)`(status 區) | 改寫:六個 env 值 → 一份 `deployment.json`(含 `services.<role>.gpu`) | 模型與 GPU 只來自檔案 |
| `model_resolution_env` fixture | 刪 `monkeypatch.delenv("OPENCODE_CONFIG")`,docstring 同步 | 沒有任何程式讀它(§5.3 / inventory B.4);`AICODE_MODEL` 的 delenv 保留,底下仍有測試刻意把它設回去 |
| import 區 | `+DEFAULT_LLAMA_BIN`、`+LauncherOverrides`;`resolve_main_model_from_env` → `resolve_main_model`;`+import os`、`+import model_resolution`、`+import process_env` | `_env()` 不再自己 `import os`;新契約要用到模組物件與 `process_env` |

**未改**:module 層沒有 `pytestmark`(本檔一直是逐條 decorator),既有 smoke 標記
(`test_there_is_no_model_flag_and_no_model_environment_variable`、
`test_opencode_json_is_no_longer_a_model_source`、`test_a_broken_opencode_json_never_blocks_startup`、
`test_a_conflicting_opencode_json_is_not_a_conflict_any_more`、ctx / n_ctx / config 那幾條)
一條都沒動,節點名也都還在。

### `tests/test_doctor.py`

| 測試名 | 動作 | 理由 |
|---|---|---|
| `test_doctor_no_network_exits_clean` | 刪 `env.pop("OPENCODE_CONFIG", None)` | 沒有程式讀它(inventory B.4) |
| `test_compaction_mode_absent_state_is_informational` | 刪 `monkeypatch.delenv("OPENCODE_CONFIG", raising=False)` | 同上 |

沒有其他斷言、名稱、標記變動。

### `tests/test_client_preflight.py`

**零改動**(如計畫預期)。`test_every_profile_env_helper_has_the_same_home_only_shape` 仍然
成立:`doctor._profile_env()` 在沒有 `--profile` 時本來就回 HOME-only,現在是**永遠**
HOME-only(`--profile` 改走 `_profile_selection()` kwarg)。

## 3. 給其他 owner 的字

### 給 C3(`scripts/launch_servers.py` / `stop_servers.py` / `check_status.py`、`eval/record_semantic_vectors.py`、`tests/test_server_scripts.py`)

1. `tests/test_server_scripts.py:24` 的 `from deployment_profile import RUNTIME_OVERRIDE_ENV_KEYS` 已無此符號(計畫 §4 的已知斷點);`:648` 的用法一併改。
2. **`scripts/check_status.py:114-121` 會 `TypeError`**:`inspect_deployment(..., environ=os.environ, ...)` 的 `environ=` 已刪,直接把那一行拿掉即可(registry 走 `profile.registry_file`)。這一條計畫沒列,是我這輪造成的第三個跨 owner 斷點。
3. `launch_servers.py` 的 `_cli_environment()` / `env = dict(os.environ)` / `cli_env=` 整組換成
   `kwargs = loader_kwargs(args)` → `load_effective_profile(**kwargs)`;現有 9 個旗標名與
   `add_loader_arguments` 逐字相同,換過去時要**刪掉**原本的 `parser.add_argument`,否則
   argparse 會 `conflicting option string`。
4. `_command_for` / `_print_dry_run` 的 `llama_bin` 改用 `profile.llama_bin`,
   `build_server_command(...)` 與 `resolve_model_reference(...)` 都要帶
   `registry_file=profile.registry_file`(set_config 的 `--model-registry-file` 才會生效)。
5. session 名改 `deployment_profile.TMUX_SESSIONS`(`{"main": "codetrail-main", "aux": "codetrail-rag"}`,值與現行 fallback 逐字相同)。
6. pane 命令:`shlex.join([sys.executable, str(REPO_ROOT / "deployment_profile.py"), "exec", role, *loader_argv(args)])`。`exec` 子命令**吃得下角色後面的 loader 旗標**(見偏差 1)。
7. `eval/record_semantic_vectors.py` 的 `--llama-bin` 預設值可直接用
   `load_effective_profile().llama_bin`(已是絕對路徑字串)。

### 給 C2(`scripts/set_config.py`、`tests/_set_config_harness.py`、`tests/test_set_config.py`)

1. `set_config.py:68` / `tests/_set_config_harness.py:25` 的 `RUNTIME_OVERRIDE_ENV_KEYS` 已刪(計畫 §4 的已知斷點)。
2. 驗證暫存檔改 kwargs:`load_effective_profile(deployment_config=<abs .json>, model_registry_file=<abs .json>)`;`--deployment-config` 仍要求**絕對路徑 + `.json`**,不存在時 fail-loud(訊息改成 `--deployment-config must point to an existing file: …`)。
3. `preview_start_commands` 用 `launch_servers --dry-run --deployment-config … --model-registry-file …`。
4. 寫進 `deployment.json` 的新鍵:頂層 `"llama_bin": "<絕對路徑>"`、`services.<role>.gpu`(`_GPU_RE`:`^[A-Za-z0-9][A-Za-z0-9_.:,\-]{0,255}$`)。**`llama_bin` 必須是絕對路徑**(相對值會讓 loader 在下一次啟動 fail-loud),`--llama-bin ./x` 先轉絕對再寫的既有行為要保留。
5. `TMUX_SESSIONS` 同 C3 第 5 點;`running_codetrail_sessions()` 只看這兩個名字。
6. `set_config.py:2306` 那句使用者可見字串(指向遷移工具)歸 C2 改成指向 `docs/troubleshooting.md` 的升級段。

### 給 D1(`tests/test_repo_consistency.py`、`tests/test_smoke_gate.py`)

1. `_ENVIRON_ALLOWLIST`:**移除 `deployment_status.py`**(現在零 `os.environ` / 零 `getenv`)。
   `deployment_profile.py` 與 `model_resolution.py` 留下,理由都改成「HOME / USERPROFILE(檔案在哪)」。
2. `_SPAWN_CORE`:`deployment_status.py` 也要移除(它不再出現 `subprocess`,改用
   `process_env.run` 當預設 `run`)——與計畫的四檔清單一致。
3. 新的靜態檢查有對象了:`deployment_profile.py` 全檔唯一的
   `os.execvpe(command[0], command, process_env.llama_server_env())` 在 `main()` 的 `exec` 分支。
4. `core` 集合清空後我這邊已核對過:C1 的 10 個檔裡,**字串常數 / docstring 不再出現任何
   已刪的 `AICODE_*` 名字**(用 gate 同一套 AST + `allowed` + 上下文詞的規則靜態跑過,零 offender);
   保留的只有概念名 `AICODE_ROOT`(`config.py` 註解、`scripts/session_eval.py:202` 的字串)。
5. `SAFETY_MODULES["test_deployment.py"]` 的 node:既有三條(`test_opencode_json_is_no_longer_a_model_source`、
   `test_a_broken_opencode_json_never_blocks_startup`、`test_a_conflicting_opencode_json_is_not_a_conflict_any_more`)
   **都還在、名稱未變**,說明可改寫但不必刪 node;**新增登記這四條**:
   * `test_the_loader_ignores_every_legacy_override_variable`
   * `test_gpu_and_llama_bin_come_from_the_deployment_file_then_argv`
   * `test_the_server_environment_strips_gpu_selectors_and_llama_settings`
   * `test_the_main_model_resolver_has_no_environment_branch`
6. 改名 / 刪除的 node(D1 若有他處引用請同步):見第 2 節的改名 3 條與刪除 1 條。

### 給 D2(文件)

* GPU precedence 的正確寫法:`--<role>-gpu` > `--aux-gpu`(只套 embedding / reranker / vl)>
  `deployment.json` 的 `services.<role>.gpu` > 不指定。main 只吃 `--main-gpu` 與檔案。
* `llama_bin`:`--llama-bin` > `deployment.json` 頂層 `llama_bin` > `~/llama.cpp/build/bin/llama-server`,
  **一律絕對路徑**。
* systemd 的 `ExecStart` 就是 `python3 <repo>/deployment_profile.py exec main`(GPU 與 binary 讀檔,
  不帶 `Environment=`);pane 內最終環境由 `process_env.llama_server_env()` 決定。
* `deployment_profile.py env` 子命令已刪除、無替代;`deployment_profile.py show` 的輸出多了
  `llama_bin` 欄位。
* loader 旗標**放在子命令前後都可以**(`--profile X validate` 與 `exec main --llama-bin /abs/x`
  都成立),所以 `docs/deployment-profiles.md:17` 與 `docs/setup.md:76` 現有的寫法不會壞。
* 升級注意(§6.2 第 5 點)成立:舊世代 checkout 的 loader 對 `deployment.json` 的
  `llama_bin` / `services.<role>.gpu` 會 `_unknown_keys` fail-loud。

## 4. 未完成 / 疑點

* **沒有執行任何測試**(本輪 C1 不在紅綠證據的範圍內);四條新契約 smoke 與所有改過的既有測試
  都是「只寫不跑」,由編排者交付前唯一一次 smoke 驗。`Tests: smoke only — reviewer owns full execution.`
* 靜態檢查只跑了 `python3 -m compileall -q` 與自寫的 AST 掃描(gate 規則的複刻);
  `check_eval_consistency.py` / `check_readme_consistency.py` 依計畫由 D1 / D2 收尾時跑。
* 已知的跨 owner 斷點三個(W2 前 repo 不是可執行狀態):`tests/_set_config_harness.py:25`、
  `tests/test_server_scripts.py:24` 的 `RUNTIME_OVERRIDE_ENV_KEYS` import,以及
  `scripts/check_status.py` 的 `inspect_deployment(environ=…)`。
* `scripts/doctor.py` 仍 `import main_model_references_equivalent` 卻沒有呼叫端(a1682d5 就是如此)。
  刻意不動:AGENTS.md §3「不要為了讓 lint 漂亮刪未檢查影響的 import」,而且它不在本任務範圍。
* `config.py` 的 `AICODE_ROOT` 註解(§C.3 指定保留)與 `scripts/session_eval.py:202` 的字串沒動;
  另外順手改掉 `config.py` 裡一句「(含用 `AICODE_*` 環境變數覆寫)」的過期說明(計畫的行號清單
  沒有它,但它教的是一個不存在的機制)。
