# handoff C2(W2):set_config —— I-7 的環境值收斂已落檔

只動 `plan-final.md` §3 的 C2 那一列(`scripts/set_config.py`、`tests/test_set_config.py`、
`tests/_set_config_harness.py`)與本檔。沒有 commit / push / stash / checkout;
**沒有執行任何 pytest**(只跑 `python3 -m compileall -q` 與自寫的 AST / regex 靜態複刻);
沒有碰真服務、tmux、`~/.config`、真實 session、真 GPU。

依 `handoff-C1.md` 宣告的 I-5 / I-6 實際符號施工:`DEFAULT_LLAMA_BIN`、`TMUX_SESSIONS`、
`load_effective_profile(deployment_config=…, model_registry_file=…)`、`process_env.run`。

`Tests: smoke only — reviewer owns full execution.`(本輪連 smoke 都不是我跑,交付前那一次由編排者執行。)

## 1. 落檔的介面

### deployment.json 的新內容(I-7 指定的落點)

```json
{"schema_version": 1, "profile": "defaults",
 "llama_bin": "<絕對路徑>",
 "services": {"main": {"model": …, "gpu": "<selector>", …},
              "embedding": {"gpu": …}, "reranker": {"gpu": …}, "vl": {"gpu": …}}}
```

四個角色**各自**寫自己那張卡(不再有「三個附屬共用一個 AUX_GPU」的合併寫法——
那是殼層變數為了少 export 兩行才有的形狀,檔案裡逐 role 記反而更直接;
`--aux-gpu` 那一格由 launcher 的 argv 覆寫層負責,見 C1 的 `_gpu_for`)。

### `scripts/set_config.py` 的符號(§2 沒有逐一規定,列出供 Astra / D1 對照)

```python
class Plan:
    ...
    reranker_ctx: int
    llama_bin: str          # 新增,**沒有預設值**(忘了帶就在建構當下爆)
def _configured_llama_bin(home: Path) -> str | None      # 新增:讀既有 deployment.json 的 llama_bin
def _llama_bin(override: str | None, home: Path) -> Path # 簽名改:--llama-bin > 檔案 > DEFAULT_LLAMA_BIN
def check_llama_binary(binary: Path, skip: bool, notes: list[str]) -> dict[str, bool]  # 新增第一個參數
def _staging_file(content: str, label: str) -> Path      # 新增:0600 暫存檔
def validate_payloads(deployment_json: str, registry_json: str) -> None   # 刪 plan 參數
def preview_start_commands(codetrail_dir: Path) -> int                    # plan → codetrail_dir
def _restart_servers(start_path: Path) -> int            # 簽名不變,內部改 process_env.run
def running_codetrail_sessions() -> list[str]            # 只探測 TMUX_SESSIONS 的兩個名字
```

**刪除**:`_OVERRIDE_ENV_KEYS`、`_SESSION_ENV_KEYS`(連同 `RUNTIME_OVERRIDE_ENV_KEYS` import)、
`_gpu_exports()`、`_fresh_env()`、`_sanitized_subprocess_env()`、`import subprocess`、
`--models-dir` 的 `MODELS_DIR` fallback、`LLAMA_BIN` 讀取、start.sh 產生器的 `unset` 行與
四個 `export`、split-brain 警告(`AICODE_DEPLOYMENT_CONFIG` 等四個名字的偵測)。

**新增旗標**:`--llama-bin PATH`(預設:既有 `deployment.json` 的 `llama_bin` →
`deployment_profile.DEFAULT_LLAMA_BIN`;相對路徑先轉絕對再寫檔,既有行為保留)。

**spawn**:`_detect_python` / `check_llama_binary`(`--help` 與 `ldd`)/ `detect_gpus` /
`_restart_servers` / `preview_start_commands` / `running_codetrail_sessions` 全部走
`process_env.run`,例外型別改 `process_env.TimeoutExpired`,`stdout=process_env.DEVNULL`。
本檔現在 **`os.environ` / `getenv` 零次、`subprocess` 零次**。

**驗證與預覽的交接方式**(I-7):
* `validate_payloads` 把 deployment / registry 兩份 staging 內容各寫一個 0600 暫存檔,
  用 `load_effective_profile(deployment_config=…, model_registry_file=…)` 驗,`finally` 兩個都刪。
* `preview_start_commands` 跑
  `python3 scripts/launch_servers.py --scope all --dry-run --deployment-config <~/.config/codetrail/deployment.json> --model-registry-file <…/models.json>`。

### 介面偏差(3 項,都在 C2 owner 檔內;下游只有測試會碰)

1. **`Plan.llama_bin` 是必填欄位**(§2 沒規定 Plan 的形狀)。給預設值 `""` 的話,
   `build_deployment_config` 會寫出 `"llama_bin": ""`,loader 只會在寫入前的驗證階段
   丟一個看不出成因的錯;必填是在建構當下就爆。代價是測試的兩個 `Plan(...)` 建構點要補一格
   (見 §2)。
2. **`validate_payloads` / `preview_start_commands` 不再收 `plan`**。它們原本只拿 `plan` 去組
   `_fresh_env(plan)`(把 main model 塞進環境);那條路刪掉之後參數就沒有用途了。
   repo 內沒有其他呼叫端(已 grep:只有 `_gather` 與 `run`)。
3. **產生的 start.sh 內 heredoc 標記 `CODETRAIL_USAGE` → `START_SH_USAGE`**。
   D1 收緊 `test_model_facing_text_never_teaches_a_removed_environment_knob` 的 `core_only`
   之後,`scripts/set_config.py` 的字串常數會被掃,而那個 pattern 認的是
   `(AICODE|AI_CODE|CODETRAIL|OPENCODE)_[A-Z0-9_]+` —— heredoc 標記會被當成教一個
   不存在的環境變數。改名比在 gate 加豁免小(它只是 bash heredoc 的分界字)。
   **D1 因此不需要為 set_config 加任何 allowed token。**

## 2. test-changes

### `tests/_set_config_harness.py`(fixture / helper,非測試本體)

| 名稱 | 動作 | 行為為什麼該變 |
|---|---|---|
| `from deployment_profile import RUNTIME_OVERRIDE_ENV_KEYS` | 刪 import | C1 已刪除該符號(計畫 §4 的已知斷點);harness 不能再從 runtime 推導這份清單 |
| `PROFILE_ENV_KEYS`(set) | 改名 `LEGACY_SHELL_OVERRIDES`(dict,字面清單),**語意反轉** | 原用途是「從 `os.environ` 濾掉會影響設定的名字」;現在沒有任何程式讀它們,清單改成**設進**測試環境的壞值 —— 每條測試綠燈就順便證明那些名字無效。名字與 C1 的 `tests/test_deployment.py::LEGACY_OVERRIDE_ENV_KEYS` 對齊,另加 launcher 的兩個 health timeout |
| 同上 | 移除 `OPENCODE_CONFIG` | 沒有任何程式讀它(inventory B.4 指定) |
| `write_fake_tmux(bin_dir)` | 新增 helper,由 `build_env` 呼叫 | `running_codetrail_sessions()` 現在探測固定的 `codetrail-main` / `codetrail-rag`;以前測試是靠把 `MAIN_SESSION` / `SESSION` 設成不存在的名字來閃開開發機真的 tmux,那條路隨環境變數一起消失了。假 tmux 一律回「沒有這個 session」,測試不再看機器上有沒有在跑 CodeTrail |
| `build_env()` | 改寫:不再過濾 `os.environ`;改為 `dict(os.environ)` + `LEGACY_SHELL_OVERRIDES` + HOME/USERPROFILE/PATH;刪掉 `LLAMA_BIN` / `MAIN_SESSION` / `SESSION` 三個 fixture 值;呼叫 `write_fake_tmux` | 同上兩條;`LLAMA_BIN` 不再是入口,llama-server 改由 argv 指定 |
| `llama_bin_args(tmp_path)` | 新增 helper | 預設 binary 是 `~/llama.cpp/build/bin/llama-server`,tmp HOME 裡沒有那一顆;每個入口都要指名 |
| `run()` / `run_subprocess()` | 新增 `pin_llama_bin: bool = True`;argv 前置 `--llama-bin <tmp_path>/llama-server` | 同上。`pin_llama_bin=False` 給「證明預設值來自 deployment.json」的那條新契約用 |
| module docstring | 補「兩個入口自動帶 `--llama-bin`、假執行檔都在 tmp」 | 描述換掉的呼叫慣例 |
| `STDIN_STANDARD` 上方的作答註解 | `2=native / 3=manual` → `2=manual / 3=off` | `client_compaction.MODES` 早已是 codetrail/manual/off;註解教的是一個不存在的選項(inventory B.3「tests 內歷史註解」選做項) |

### `tests/test_set_config.py`

**新增(1 條,`@pytest.mark.smoke`;計畫 §5.2)**

| 測試名 | 守什麼 |
|---|---|
| `test_deployment_json_pins_llama_bin_and_gpus` | ① `--llama-bin` 與四個角色的卡都寫進 `deployment.json`(頂層 `llama_bin` + `services.<role>.gpu`);② **沒給** `--llama-bin` 的重跑沿用檔案裡的值,不靜默退回內建預設;③ 產物餵回 `load_effective_profile` 時 `llama_bin` / `gpu` 逐字相同(schema 收得下) |

**改名(2 條)**

| 舊名 → 新名 | 行為為什麼該變 |
|---|---|
| `test_generated_start_sh_clears_legacy_env_overrides` → `test_generated_start_sh_ignores_legacy_shell_overrides`(**並加 `@pytest.mark.smoke`**) | 「clears」講的是 start.sh 開頭那一行 `unset`,那個機制已經不存在;現在的契約是「產生的檔沒有任何 export / unset 的執行行」加上「殼層殘留的舊名字對啟動指令沒有作用」。計畫 §5.2 把它列為新契約 smoke |
| `test_restart_subprocess_env_is_sanitized` → `test_restart_subprocess_env_goes_through_process_env` | 「sanitized」指的是 set_config 自己維護一份剔除清單(`_sanitized_subprocess_env`);那個函式刪了,剝除只剩 `process_env.child_env()` 一個出口 |

**刪除(2 條)**

| 測試名 | 理由 |
|---|---|
| `test_generated_start_sh_exports_before_subcommand_dispatch` | 它整條測的是「export 必須排在 `case` dispatch 之前」,而 start.sh 現在一個 export 都沒有;它守的那個失效模式(status 路徑拿不到期望 GPU)已經由「status 自己讀 deployment.json」取代 |
| `test_deployment_env_override_split_brain_warns` | 它測的是「殼層設了自訂設定檔路徑就要警告兩邊各讀一份」。設定檔位置不再由環境決定(唯一的自訂路徑是行程之間交暫存檔的旗標),分岔本身不存在了,警告碼也一併刪除 |

**改斷言 / 改輸入機制(9 處)**

| 測試名 / 位置 | 動作 | 行為為什麼該變 |
|---|---|---|
| import 區 | `+llama_bin_args` | 兩個直接組 `bash set_config.sh` argv 的測試也要指名 binary |
| `test_summary_confirm_enter_writes_and_q_aborts` | argv 加 `*llama_bin_args(tmp_path)` | 同上(這條是直接 `subprocess.run(["bash", SCRIPT, …])`,不走 harness 的注入) |
| `test_flags_override_model_and_gpu` | `export MAIN_GPU=` / `export EMBED_GPU=` 斷言 → `deployment.json` 的 `services.<role>.gpu`(四個角色都驗) | GPU 的落點從 start.sh 的 export 換成設定檔 |
| `test_single_gpu_warns_and_shares_one_card` | `export MAIN_GPU=` / `export AUX_GPU=` → 四個角色的 `gpu` 都等於同一張卡 | 同上;「三顆附屬共用一張卡」現在是四個逐 role 的值,不是 `AUX_GPU` 一個變數 |
| `test_missing_llama_binary_fails_with_build_hint` | `assert "LLAMA_BIN" in stderr` → `assert "--llama-bin" in stderr` | 修復指令從 `export LLAMA_BIN=…` 換成 `./set_config.sh --llama-bin …` |
| `test_start_sh_pins_validated_llama_bin` | 斷言 `export LLAMA_BIN=` → `deployment.json` 的 `llama_bin`;docstring 同步 | 「探測旗標用哪顆 binary、啟動就用哪顆」的保證還在,只是釘在設定檔而不是 start.sh |
| `test_relative_models_dir_and_llama_bin_are_stored_absolute` | 輸入 `env["LLAMA_BIN"]="./llama-server"` → argv `--llama-bin ./llama-server`;斷言 → `deployment.json` 的 `llama_bin`;docstring 同步 | 相對路徑轉絕對的理由變強了:真正 exec 它的是 tmux pane 裡的 loader,而 C1 的 loader 對相對 `llama_bin` 直接 fail-loud |
| `test_generated_start_sh_ignores_legacy_shell_overrides`(改名那條的內容) | 加「非註解行不得出現 export / unset」;加「殼層 `LLAMA_BIN` 的壞路徑不得出現在 dry-run 指令」;加「殼層 `CUDA_VISIBLE_DEVICES=7` 不得出現在指令」;保留原本的 `EMBED_MODEL` / `MAIN_CTX` 兩條 | 舊斷言只驗「值沒有生效」,新增的三條驗「機制不存在」——前者在 start.sh 改回 export 時仍可能綠 |
| `test_restart_subprocess_env_goes_through_process_env`(改名那條的內容) | 刪掉對 `_sanitized_subprocess_env()` 的直接呼叫;monkeypatch 目標 `sc.subprocess.run` → **stdlib 的 `subprocess.run`**;斷言改成「四前綴被剝(`AICODE_MODEL` / `CODETRAIL_ANYTHING`)、`KEEP_ME` 保留、`env` 不得是 None」 | 被測函式沒了;patch 最底層才驗得到 `process_env` **真的算出來的**那份環境(patch `process_env.run` 只會證明「我們自己傳了什麼」)。session 名的斷言拿掉是因為那三個變數已經沒有讀者,改由 C3 的 `TMUX_SESSIONS` 常數保證 |
| `test_profile_emits_cpu_moe_for_main_and_vl_and_rejects_partial_mix` | 移除 `env["AICODE_MODEL"]`,改寫一份 `~/.config/codetrail/models.json`(`{plan.main_key: <GGUF 絕對路徑>}`) | 主模型不再能從環境覆寫;`build_deployment_config` 寫的是 registry key,所以這條測試要跟著提供 registry。斷言(cpu_moe / fit / fit-target / 拒絕 partial mix)一字未改 |

**fixture / helper / 模組註解**

| 名稱 | 動作 | 理由 |
|---|---|---|
| `_build_plan()`(helper) | `sc.Plan(..., llama_bin="/opt/llama-server")` | `Plan.llama_bin` 是必填;值與同一條測試已在用的 `build_server_command(..., "/opt/llama-server")` 一致 |
| `test_selected_reranker_ctx_sets_context_and_physical_batch` 內的 `sc.Plan(...)` | 同上 | 同上 |
| module docstring 的壓縮段條列 | 「切回 native 必須精確還原…」→「選 `off` 也要記下來」;`plugin` → `runtime` | 那條講的是已刪除的遷移工具;`MODES` 現在是 codetrail/manual/off,段內也沒有任何 native 還原的測試 |
| 壓縮段上方的區塊註解最後一行 | 「舊 OpenCode 安裝的遷移只在…」→「restore manifest 是兩個世代共用的同一個檔…」 | 同上;換成這一段真的有測試在守的那件事 |

**未改**:本檔沒有 module 層 `pytestmark`(一直是逐條 decorator),既有 21 條 smoke
一條都沒動、node 名都還在;`_offline_fixture` / `_run_yes` / `_run_yes_ctx` / `_manifest*` /
`_client_config` / `_home` 等 helper 未改。改動後 test function 共 103 條(原 104:刪 2 加 1)。

**新增 smoke node 全名(請 D1 登記進 `SAFETY_MODULES["test_set_config.py"]`)**

```
tests/test_set_config.py::test_generated_start_sh_ignores_legacy_shell_overrides
tests/test_set_config.py::test_deployment_json_pins_llama_bin_and_gpus
```

兩個名稱與 `plan-final.md` §5.2 逐字相同。`SAFETY_MODULES` 目前登記的 21 個
`test_set_config.py` node **全部存在且仍帶 smoke**(我刪掉 / 改名的 4 條都不在裡面,
已用 AST 對 manifest 逐條核對:missing 0、unmarked 0)。

## 3. 給其他 owner 的字

### 給 D1(`tests/test_repo_consistency.py`、`tests/test_smoke_gate.py`)

1. `_ENVIRON_ALLOWLIST`:**移除 `scripts/set_config.py`**。它現在 `os.environ` / `getenv`
   **零次**(HOME 走 `os.path.expanduser("~")`,設定走 argv 與 `deployment.json`)。
2. `_SPAWN_CORE`:`scripts/set_config.py` 移除(與計畫的四檔清單一致)。本檔已無
   `import subprocess`、無 `os.environ.copy()`、`process_env` 的出口一個都沒有收 `env=`;
   我用 gate 的 `_ENV_COPY` / `_SPAWN_API` 規則自寫複刻靜態掃過,零 offender。
3. `test_model_facing_text…` 的 `core_only` 拿掉 `scripts/set_config.py` 之後:本檔的字串常數
   已無任何 `AICODE_*` / `CODETRAIL_*` 名字(heredoc 標記已改名,見偏差 3)。同樣用該測試的
   AST + `allowed` 規則自寫複刻掃過,零 offender。
4. OpenCode gate 改掃全文之後:`scripts/set_config.py` 全檔 **`opencode` 零次**
   (原 `:1167` / `:1874` 兩處註解改「舊世代前端」,`:2306` 的使用者可見字串改成指向
   `docs/troubleshooting.md` 的升級段)。本檔不需要進 allowlist。
5. `SAFETY_MODULES` 新增兩個 node:見 §2 末。

### 給 C3(`scripts/launch_servers.py`)

`preview_start_commands` 會這樣叫 launcher(唯一的跨 owner 依賴):

```
python3 scripts/launch_servers.py --scope all --dry-run \
    --deployment-config <abs>/deployment.json --model-registry-file <abs>/models.json
```

`--deployment-config` / `--model-registry-file` 由 `add_loader_arguments` 提供(我讀你施工中的
`_parser()` 時已經掛上去了;若最後形狀有變請在 handoff 說明)。另外 `tests/test_set_config.py`
有三條會真的跑 `bash ~/start.sh --dry-run`
的測試,它們依賴你的 dry-run 仍然:(a) 印 `{role}_command=` 形式的最終 llama-server argv
(`-c 65536` 這種可 grep 的形狀);(b) GPU 前綴取自 `deployment.json` 的
`services.<role>.gpu`(輸出 `env CUDA_VISIBLE_DEVICES=<selector>`);(c) llama-server 路徑取自
`deployment.json` 的 `llama_bin`。

### 給 D2(文件)

* `./set_config.sh` 新旗標:`--llama-bin PATH`(未指定 → 既有 `deployment.json` 的 `llama_bin`
  → `~/llama.cpp/build/bin/llama-server`);`--models-dir` 沒有 `MODELS_DIR` 這條退路了。
* 產生的 `~/start.sh` **不再 export / unset 任何變數**,只轉發子命令與旗標;
  「換 llama-server 位置」的指引從 `export LLAMA_BIN=…` 改成 `./set_config.sh --llama-bin …`。
* `deployment.json` 多了頂層 `llama_bin` 與四個 `services.<role>.gpu`
  (`set_config` 每次重跑都會依作答重寫 `gpu`;`llama_bin` 沒給旗標時沿用既有值)。
* 結尾提示的第 3 層健檢那行已改成 `python3 scripts/doctor.py`(不再帶 `AICODE_MODEL=` 前綴)。
* 「殼層設了自訂設定檔路徑會 split-brain」的警告已刪除,文件若有提到請一併移除。

## 4. 未完成 / 疑點

* **沒有執行任何測試**:兩條新契約 smoke(§2)與所有改過的既有測試都是「只寫不跑」,
  由編排者交付前唯一一次 smoke 驗。靜態上只跑了 `python3 -m compileall -q` 與自寫的
  AST / regex 掃描(gate 規則複刻),沒有 import 本 repo 的 runtime 去試呼叫任何函式。
* 跨 owner 依賴:`preview_start_commands` 與三條 `bash ~/start.sh --dry-run` 測試要等 C3 的
  launcher 收斂才會綠;W2 期間兩邊並行,我以 `plan-final.md` I-7 與 C3 現有的
  `add_loader_arguments(parser)` 為準。
* `llama_bin` 是**唯一**會跨重跑沿用的值(其餘設定「重跑不沿用舊值」)。這是 I-7 指定的預設鏈,
  新契約測試的第 (2) 點就在釘它;`test_rerun_has_no_carryover_current_answers_win` 講的是
  使用者作答那幾格,兩者不衝突。
* `validate_payloads` 現在把 registry JSON 也落成暫存檔(以前是塞進環境變數)。兩個檔都是
  `NamedTemporaryFile` 的 0600、`finally` 立刻刪;內容是模型路徑與參數,沒有對話內容。
* 假 tmux 是「任何子命令都 exit 1」。目前測試只用到 `has-session`;之後若有測試需要
  「session 存在」的分支,要擴充那個 fake 而不是改回讀環境變數。
