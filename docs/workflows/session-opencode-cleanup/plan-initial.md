# 初步計畫(流程第 1 步):TUI session 選單與重播、移除 OpenCode 整合、環境變數收斂

- requested model: `claude-fable-5-1[1m]`,effort `max`。這一行是提示詞裡指定的請求值與模型自述,**不是** API 回傳的身分證據;編排者另以 Claude Code JSON metadata(`system.init.model` / `assistant.message.model`)記錄於 `model-log.md`。
- 基準:`main` @ `a1682d5`(2026-09-04),工作樹 clean。角色 developer(本輪 prompt 沒有 `ROLE=REVIEWER`)。
- 本檔只規劃。未改 runtime / tests / 設定 / AGENTS.md,未跑任何測試,未 commit。實地讀碼範圍只有本 repo,沒有讀真實 session 檔或 `~/.config` 內容。
- 配套檔:`inventory.md`(檔案 / 符號 / 環境值逐項歸宿與測試影響)、`deferred.md`(擱置表,初始為空)。

---

## 0. 一頁結論

| 目標 | 落點 | 一句話 |
|---|---|---|
| a. session 選單 / 重播 | `client_app.py`、`client_store.py`、`codetrail_chat.py` | `engine.resume()` 早就正確重建歷史;壞的是 TUI 只貼一行「已接續」。新增 `/session` 選單,`/resume` 與啟動 `-c` / `--session` 三條路都走同一個重播函式;大綱由本地訊息摘錄。 |
| b. 移除 OpenCode 整合 | 刪 `opencode_migrate.py`(1,743 行)、`opencode_plugins/`(2 個 JS stub)、`tests/test_opencode_migrate.py`(63 條);`doctor` 的殘留偵測;公式常數改名留在 `compaction_formula` | 仍在用的東西只有門檻公式的三個常數與 `OPENCODE_` 的子行程剝除;其餘全是「寫使用者 OpenCode 設定」的邏輯,本版一個都不再碰。舊安裝的還原改成文件上的**固定舊版 + 手動**路徑。 |
| c. 環境變數收斂 | `deployment_profile.py`、`scripts/{launch_servers,stop_servers,check_status,set_config,run_tests}.py`、`eval/record_semantic_vectors.py`、docs、三個靜態 gate | 客戶端 / MCP 側早已只讀 HOME / XDG;剩下的全在**啟動核心**:一個約 45 個名字的 env overlay 加 13 個獨立變數。全部改成 `deployment.json` 新鍵、repo 常數或 argv;`~/start.sh` 不再 `export` / `unset` 任何東西。 |

施工順序(細節 §4):**Wave 0** `T-0`(gate 先放行本目錄)→ **Wave 1** 平行 `A1` `A2` `B1` `C1` → **Wave 2** 平行 `C2` `C3`(等 `C1`)→ **Wave 3** `D1` 整合(docs / AGENTS / gate / smoke manifest / 唯一一次 smoke)。

**先講兩件會咬人的事**

1. **交接檔本身會讓 smoke 紅。** `tests/test_repo_consistency.py` 的 OpenCode gate 掃 repo 內**所有**文字檔(`_iter_text_files` → `_walk_files`,只跳 `_SKIP_DIRS` 與 `tests/`),docs gate 掃所有 `.md`(`_repo_sources(suffixes=(".md",))`)。`docs/workflows/` 不在跳過清單,而本檔必然提到 OpenCode 與被移除的變數名。所以 `T-0` 必須第一個做:在 `_walk_files` 以**相對路徑**剪掉 `docs/workflows`(不是把 `workflows` 這個目錄名加進 `_SKIP_DIRS`——那會跳過任何同名目錄),並加一條 gate 自測。交接檔要與 `T-0` 一起(或之後)提交。`scripts/check_readme_consistency.py` 只 `glob("*.md")` 不遞迴,不受影響;`test_user_facing_python_commands_use_python3` 會 `rglob` docs,所以交接檔裡的命令一律寫 `python3`。
2. **`deployment.json` 加鍵會讓舊世代 checkout fail-loud。** 目標 c 要把 GPU selector 與 llama-server 路徑寫進 `deployment.json`(新鍵 `llama_bin`、`services.<role>.gpu`)。舊世代的 loader 對未知鍵是 `_unknown_keys` → `ProfileError`。同一台機器兩份安裝共用 `~/.config/codetrail/` 時,從本版跑過 `./set_config.sh` 之後,舊世代的 `~/start.sh` 會拒絕載入(fail-loud,不是靜默走錯)。這與既有事實一致(記憶:在本 checkout 跑 set_config 本來就會拆掉另一份的接管),但要在 README 升級段講明。替代方案(另開 `launcher.json`)列為待審決策 D-c1。

---

## 1. 實地讀碼結論

### 1.1 目標 a:歷史不顯示的根因與現況

- `client_engine.Engine.resume()`(`client_engine.py:632-658`)正確:讀 store、尊重最後一筆 `compaction` 記錄、最後才原子換 `session_id / messages / store_error`。中途壞掉不會半換(`test_a_malformed_compaction_record_does_not_half_switch_the_session` 守著)。
- TUI 的 `/resume`(`client_app.py:745-760`)只做 `engine.resume()` → `coordinator.session_changed()` → `_tools.clear()` → 貼一行 `NoticeLine("已接續 …(N 則訊息)")`。**沒有任何一行把 `engine.messages` 變成 widget**。啟動時 `aicode -c` / `--session` 走 `codetrail_chat._build()` 在 App 建立前 `engine.resume()`(`codetrail_chat.py:176-184`),而 `CodeTrailApp.on_mount()`(`client_app.py:445-461`)只印 banner 與「輸入 /help」,同樣不重播。這是真實 bug,依 §1.3 走 red-before-green。
- `/sessions`(`client_app.py:735-743`)列 `session_id  turns=N  title`;`SessionInfo.title` 來自 header,而所有 `store.create()` 呼叫都沒帶 title(`client_engine.py:443, 615`),所以永遠是空字串。CLI `codetrail_chat.py sessions`(`codetrail_chat.py:378-383`)同樣。「大綱」目前不存在。
- session 檔的每一則記錄形狀(重播要認的全部欄位,來自 `client_engine._record` / `_tool_reply` / `_one_model_step` / `heal_pending_tool_calls` / `replace_history`):
  - `{"type":"message","role":"user","content":str,"time":float}`;壓縮摘要是 `{"role":"user","content":"[先前對話摘要]\n…","synthetic":true}`(`client_compaction.SUMMARY_PREFIX`)。
  - `{"role":"assistant","content":str|null,"reasoning_content"?:str,"tool_calls"?:[{"id","type":"function","function":{"name","arguments":<JSON 字串>}}],"tool_status"?:"error"}`。
  - `{"role":"tool","tool_call_id","name","content":str,"tool_status":"completed|error|denied","structured":any|null}`;heal 補的「已中斷」結果沒有 `structured`。
  - `{"type":"compaction","time","history":[…]}`:之後的歷史整段取代之前的。
  - `_INTERNAL_KEYS = {time, tool_status, synthetic, structured, call_index}` 不送模型;畫面可以用。
- 既有安全邊界(不得放寬):`client_store.read_bytes()` dir-fd + `O_NOFOLLOW` + `fstat`(普通檔、`nlink==1`、owner)+ 64 MiB 上限;`list_sessions()` 只列 `<id>.jsonl`、path-based `is_symlink()` 只是預篩,真正的讀取仍走 `read_bytes()`;`_refuse_inside_root()` 每次開目錄都重判。選單只會呼叫 `list_sessions()` 與 `engine.resume()`,不新增任何檔案系統路徑。
- 回合協調:`_busy_notice()`(`client_app.py:707-718`)在 `coordinator.busy` 時擋 `/new` `/resume`;核准框是 `ModalScreen`,開著時輸入框收不到 `/` 指令;`coordinator.session_changed()` 會 `compactor.rebind()`(`client_turns.py:379-389`)。這三條全部沿用。
- Textual 8.2.8 已裝,`OptionList` / `ListView` 都可用(本機確認)。

### 1.2 目標 b:OpenCode 殘留的完整範圍

repo 內 `opencode`(不分大小寫)共 461 處 / 47 檔。分四類(逐檔清單在 `inventory.md` §B):

1. **整組刪除**:`opencode_migrate.py`(全部是 opencode.json 的 ownership / 還原 / plugin 項邏輯;`main()` 是唯一入口)、`opencode_plugins/codetrail-compaction.js`、`opencode_plugins/codetrail-notify.js`(inert stub,存在理由只剩「使用者的 opencode.json 可能還註冊著這個路徑」)、`tests/test_opencode_migrate.py`(63 條)、`scripts/doctor.py::check_legacy_opencode_install`(`793-825` + 呼叫 `1312`,唯一 import 遷移工具的 runtime 路徑)。
2. **仍在用、要改名留在 CodeTrail 模組**:`compaction_formula.py` 的 `UPSTREAM_COMPACTION_BUFFER`(20000)、`UPSTREAM_OUTPUT_TOKEN_MAX`(32000)、`UPSTREAM_MIN_PRESERVE_RECENT_TOKENS`(2000)——它們是門檻公式的一部分,消費者是 `client_compaction.py:94-98`(import 期 fail-loud 綁 `config.CLIENT_MAX_OUTPUT_TOKENS_CAP`)、`tests/test_client_compaction.py:85`、`config.py:406` 註解、AGENTS.md §2。改名為 `COMPACTION_RESERVE_TOKENS` / `OUTPUT_TOKEN_MAX` / `MIN_PRESERVE_RECENT_TOKENS`,數值與公式**一個字都不改**。`combine_settings()`(`compaction_formula.py:301-354`)只被 `opencode_migrate.derive_for_config` 與一條測試用,客戶端沒有第二個摘要模型 → 死碼,刪(D-b3)。`compaction_formula.py:51-53` 是一段沒有主體的懸空註解(常數已搬走)→ 刪。
3. **保留但改字**:`process_env.STRIPPED_ENV_PREFIXES` 裡的 `OPENCODE_`(**保留**:它剝掉升級機器殘留的 API key,是安全清理,使用者明講不能刪)、`scripts/mcp_catalog.py` / `scripts/eval_tool_routing.py` 的歷史欄位名 `opencode_effective_chars` 與 `eval/fixtures/tool_routing/support_matrix.json` 的 `era: "opencode"` 列(凍結的歷史量測資料,改名等於丟資料 → 保留,D-b4)、約 40 處只剩歷史說明的註解 / docstring(逐行改成「舊世代前端」或直接刪)。
4. **文件與 gate**:README 六段、README_DEV 四段、`docs/troubleshooting.md` **兩段重複**的升級指引(`158-185`、`623-650`)、`docs/security.md` 兩處、`docs/basic-usage.md` 一處;`tests/test_repo_consistency.py` 的 `_OPENCODE_ALLOWLIST`(21 筆,其中 `docs/opencode-agents-template.md` 已不存在、`docs/compaction-rules.md` / `docs/setup.md` / `docs/mcp-tools.md` 實際上零命中)、三條 JS stub 契約測試、`tests/test_smoke_gate.py` 的六個相關條目;`scripts/check_readme_consistency.py` 的 stale 樣式。
5. **不動**:使用者的 `~/.config/opencode/*`、`~/.config/codetrail/compaction.json`(舊世代 ownership 狀態檔)、`compaction-stopped.jsonl`、`setconfig-last-transaction.json`(兩世代共用、不改名;`set_config.main_restore_targets()` 的四檔白名單照舊,含外來目標的 manifest 整份拒絕)。

### 1.3 目標 c:環境變數盤點結論

- **客戶端 / MCP 側**已經乾淨:只讀 `HOME` / `USERPROFILE`(HOME 缺席時)/ `XDG_STATE_HOME` / `XDG_CACHE_HOME`,以及 `PYTHONIOENCODING` 的寫入;spawn 全走 `process_env`。`_ENVIRON_ALLOWLIST` 有 10 筆檔案**根本沒有讀 environ**(stale 豁免 = 之後有人加讀取 gate 不會叫),要剪掉。
- **啟動核心**是全部殘留所在(`inventory.md` §C 逐變數):
  - `deployment_profile._ENV_FIELDS` overlay:4 個 role × 7 欄,約 40 個名字(`AICODE_MODEL`、`AICODE_LLAMA_*_BASE_URL`、`*_PORT`、`*_BIND` / `AICODE_BIND`、`*_CTX` / `AICODE_N_CTX` / `AICODE_MAIN_CTX`、`*_BATCH`、`*_UBATCH`、`EMBED_MODEL` / `RERANK_MODEL` / `VL_GGUF` / `VL_MMPROJ` …),加 `AICODE_PROFILE` / `AICODE_DEPLOYMENT_CONFIG` / `AICODE_MODEL_REGISTRY` / `AICODE_MODEL_REGISTRY_FILE` / 五個 GPU selector / `CUDA_VISIBLE_DEVICES`(合起來就是 `RUNTIME_OVERRIDE_ENV_KEYS`,`set_config` 產生的 `~/start.sh` 靠 `unset` 一整行 + 三行 `export` 維持一致)。
  - 獨立變數:`LLAMA_BIN`、`MODELS_DIR`、`MAIN_SESSION` / `AUX_SESSION` / `SESSION`、`AICODE_NO_ROLLBACK`、`AICODE_STOP_TIMEOUT`、`EXPECTED_LLAMA_SERVERS`、`AICODE_STATUS_PROC_ROOT`、`AICODE_STATUS_SNAPSHOT`、`AICODE_RERANK_FALLBACK_POLICY`、`MAIN_HEALTH_TIMEOUT` / `RAG_HEALTH_TIMEOUT`、`AICODE_TEST_JOBS`,以及 `deployment_profile.py env` 子命令會**輸出** `AICODE_*` 指派(`runtime_environment()`,repo 內沒有消費者)。
  - `model_resolution.resolve_main_model_from_env()` 仍有 `AICODE_MODEL` 分支(`model_resolution.py:244`),客戶端側靠「只交 HOME」把它關掉;六個 `_profile_env()` / `_file_env()` helper 的存在理由就是這個 overlay。
- 「多 branch 互相干擾」的真正機制正是上面這些:另一份安裝的 `~/start.sh` export 出來的值被本份的 launcher / status 讀走。收斂之後 `~/start.sh` 變成純 dispatcher,啟動核心的三條路(launch / stop / status)看到的設定只有 `deployment.json` + argv。
- 附帶發現(要一起修,否則靜態檢查會斷):`scripts/check_readme_consistency.py` 第 5 條要求 README 提到 `AICODE_MODEL`;`eval/spec_questions.json` 的 `spec_001` gold 是 `AICODE_MODEL`、`spec_003` 是 `AI_CODE_PATCH`(後者早已移除,現在只因 `config.py:1074/1127` 兩行 stale 註解還含那個字串才過 `check_eval_consistency`)。

---

## 2. 共同介面(先定義,施工照這份做;改動要回寫本節)

### I-1 `client_store`:session 大綱(A1 擁有;A2、CLI 消費)

```python
@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    path: Path
    created: float
    updated: float
    title: str                 # 既有欄位,照舊(header.title)
    turns: int                 # 既有:真實 user 訊息數(不含 synthetic)
    first_prompt: str = ""     # 第一則真實 user 訊息,單行、去空白、≤ OUTLINE_MAX_CHARS(80)
    last_prompt: str = ""      # 最後一則真實 user 訊息,同上
    messages: int = 0          # type == "message" 的記錄數(原始,不看 compaction)
    tool_calls: int = 0        # role == "tool" 的記錄數
    compactions: int = 0       # type == "compaction" 的記錄數

def session_outline(records: Sequence[Mapping]) -> dict   # 純函式,info() 用它;大綱只取自本地記錄,零 LLM、零網路、零寫入
def list_sessions(self, limit: int | None = None) -> list[SessionInfo]   # limit 給選單用(預設 None = 全部);排序仍以 updated 由新到舊
```

規則:`first_prompt` / `last_prompt` 只認 `role == "user"` 且沒有 `synthetic` 的記錄;**絕不**取工具輸出、assistant 文字或摘要內容;`\n` 折成空白;超長截斷加 `…`。`EphemeralSessionStore.list_sessions()` 照舊回 `[]`。

### I-2 `client_app`:重播的純函式(A2 擁有)

```python
@dataclass(frozen=True)
class HistoryEntry:
    kind: Literal["user", "summary", "assistant", "assistant_error", "reasoning", "tool"]
    text: str = ""                      # user / summary / assistant / assistant_error / reasoning
    tool: str = ""                      # tool
    call_id: str = ""
    arguments: dict = field(default_factory=dict)   # 由 tool_calls[i].function.arguments 的 JSON 字串解析;壞 JSON → {"arguments": <原字串>}
    status: str = ""                    # completed / error / denied / pending
    output: str = ""                    # tool 訊息的 content;沒有結果 → "(這次呼叫沒有結果;下一題送出前會標成已中斷)"
    structured: Any = None              # tool 訊息的 structured(給展開區,與 _tool_output 同一種呈現)

def history_entries(messages: Sequence[Mapping]) -> list[HistoryEntry]
```

規則:順序 = 訊息順序;assistant 有 `reasoning_content` → 先一則 `reasoning` 再 assistant;assistant 有 `tool_calls` → 每個 call 一則 `tool`,狀態與輸出由**之後**同 `tool_call_id` 的 tool 訊息補上(沒有就是 `pending`);assistant `tool_status == "error"` → `assistant_error`;`synthetic` user → `summary`(去掉 `SUMMARY_PREFIX` 前綴);`content is None` 且沒有 tool_calls 的 assistant 不產生條目。這個函式不碰 Textual,可以在沒有 pilot 的情況下測。

App 端:`_replay_history(*, clear: bool)`:`clear=True` 先 `#log.remove_children()`;重播出來的 `ToolBlock` **不登記進 `_tools`**(它們永遠不會再收到事件;登記會讓舊 session 的 `call_1` 撞到本行程 `_FALLBACK_CALL_IDS` 產生的同名 id);`reasoning` 條目的 `display = self.show_reasoning`;`summary` 用新的 `SummaryBlock(Collapsible)`,標題「壓縮摘要(先前 N 則訊息已壓縮)」。

### I-3 `deployment_profile`:檔案鍵與載入簽名(C1 擁有;C2、C3、docs 消費)

```python
# deployment.json(local override)新增,兩個都是選填、缺席 = 現行預設:
#   頂層 "llama_bin": "<絕對路徑>"                      # set_config 驗證過的 llama-server
#   "services"."<role>"."gpu": "<GPU-UUID 或 index>"    # 通過 _GPU_RE;缺席 = 不指定(不 env CUDA_VISIBLE_DEVICES)
# schema_version 維持 1(新 loader 讀舊檔:缺鍵走預設;舊 loader 讀新檔:unknown key fail-loud,見 D-c1)

@dataclass(frozen=True)
class LauncherOverrides:                # 全部來自 argv,沒有 env 版本
    main_model: str | None = None
    main_ctx: int | None = None
    main_batch: int | None = None
    main_ubatch: int | None = None
    gpus: Mapping[str, str] = field(default_factory=dict)   # role → selector;"aux" 鍵套到 embedding / reranker / vl 缺席者
    llama_bin: str | None = None

TMUX_SESSIONS = {"main": "codetrail-main", "aux": "codetrail-rag"}   # repo 常數;測試隔離用 argv --main-session / --aux-session

def load_effective_profile(
    environ: Mapping[str, str] | None = None,     # 只用來取 HOME / USERPROFILE(定位 ~/.config/codetrail);其餘鍵一律忽略
    *,
    profile: str | None = None,                   # --profile
    overrides: LauncherOverrides | None = None,
    deployment_config: Path | None = None,        # 隱藏 argv --deployment-config(set_config 預覽 / 驗證用暫存檔)
    model_registry_file: Path | None = None,      # 隱藏 argv --model-registry-file(同上)
) -> DeploymentProfile
```

刪除:`_ENV_FIELDS`、`_environment_overlay`、`RUNTIME_OVERRIDE_ENV_KEYS`、`_gpu_for(env)`(改成讀檔 + overrides)、`runtime_environment()` 與 `env` 子命令、`resolve_model_reference` 的 `MODELS_DIR` 讀取(legacy 路徑固定 `~/models`)。`DeploymentProfile` 新增 `llama_bin: Path`(檔案值 > 預設 `~/llama.cpp/build/bin/llama-server`;launcher argv 再覆寫)。`model_resolution.resolve_main_model_from_env(env)` → `resolve_main_model(env)`,刪 `AICODE_MODEL` 分支(五個呼叫端同步:`client_preflight`、`scripts/doctor.py`、`scripts/tool_call_canary.py`、`scripts/session_eval.py`、`scripts/required_model_servers_check.py`)。

### I-4 argv 清單(各腳本;`~/start.sh` 只轉發 `"$@"`)

| 腳本 | 新旗標 | 取代的環境變數 | 顯示 |
|---|---|---|---|
| `scripts/launch_servers.py` | `--llama-bin PATH` | `LLAMA_BIN` | 公開 |
| | `--health-timeout SECONDS`(套用到本次 scope 的每個 role;預設仍依模型大小自動) | `MAIN_HEALTH_TIMEOUT` / `RAG_HEALTH_TIMEOUT` | 公開 |
| | `--keep-on-failure` | `AICODE_NO_ROLLBACK` | 公開 |
| | `--main-session NAME` / `--aux-session NAME` | `MAIN_SESSION` / `AUX_SESSION` / `SESSION` | `argparse.SUPPRESS`(測試隔離) |
| | `--deployment-config PATH` / `--model-registry-file PATH` | `AICODE_DEPLOYMENT_CONFIG` / `AICODE_MODEL_REGISTRY(_FILE)` | SUPPRESS(set_config 預覽用) |
| | 既有 `--main-model/--main-gpu/--aux-gpu/--embed-gpu/--rerank-gpu/--vl-gpu/--main-ctx/--main-batch/--main-ubatch/--profile` | (原本轉成 env 再讀)| 改成直接填 `LauncherOverrides` |
| `scripts/stop_servers.py` | `--timeout SECONDS`(預設常數 120)、`--main-session` / `--aux-session`(SUPPRESS) | `AICODE_STOP_TIMEOUT`、session 三個 | |
| `scripts/check_status.py` | `--expected N`(預設 4)、`--proc-root PATH`(SUPPRESS)、`--snapshot PATH`(SUPPRESS)、`--deployment-config`(SUPPRESS) | `EXPECTED_LLAMA_SERVERS`、`AICODE_STATUS_PROC_ROOT`、`AICODE_STATUS_SNAPSHOT` | |
| `deployment_profile.py` | `exec --llama-bin` 預設改讀 profile 的 `llama_bin`;`--deployment-config` / `--model-registry-file`(SUPPRESS);刪 `env` 子命令 | `LLAMA_BIN`、`AICODE_DEPLOYMENT_CONFIG`… | |
| `scripts/set_config.py` | `--llama-bin PATH`(預設:既有 `deployment.json.llama_bin` > `~/llama.cpp/build/bin/llama-server`);既有 `--models-dir` 不再有 env fallback | `LLAMA_BIN`、`MODELS_DIR` | 公開 |
| `scripts/run_tests.py` | `--jobs N`(1..16;在轉發給 pytest 之前吃掉) | `AICODE_TEST_JOBS` | 公開(README_DEV) |
| `eval/record_semantic_vectors.py` | `--llama-bin PATH`(`--record-vectors` 時必填;仍拒絕 `unknown` revision) | `LLAMA_BIN` | 公開(README_DEV) |

刪除不替代:`AICODE_RERANK_FALLBACK_POLICY`(`client.json.rerank_fallback_policy` 是唯一來源;launcher dry-run 的 `rerank_fallback_policy=` 那一行一併移除)、`CUDA_VISIBLE_DEVICES` 作為**輸入**(輸出端 `env CUDA_VISIBLE_DEVICES=<gpu>` 前綴保留)、`deployment_profile.py env` 子命令。

### I-5 `compaction_formula` 常數改名(B1 擁有)

`UPSTREAM_COMPACTION_BUFFER → COMPACTION_RESERVE_TOKENS`、`UPSTREAM_OUTPUT_TOKEN_MAX → OUTPUT_TOKEN_MAX`、`UPSTREAM_MIN_PRESERVE_RECENT_TOKENS → MIN_PRESERVE_RECENT_TOKENS`。值不變;`derive_settings` / `effective_max_output` 的算式不變;docstring 改成「公式沿用 2026-09 定案的推導(來源見 git 歷史)」。`combine_settings` 刪除(D-b3)。

### I-6 三個靜態 gate 的最終形狀(D1 擁有,施工中其他人不改 `tests/test_repo_consistency.py`)

- OpenCode gate:改為掃**含註解**的全文(現在 `.py` 會先剝註解,所以 40 處歷史註解 gate 看不到);allowlist 只剩 `process_env.py`(允許形狀 `OPENCODE_`)、`scripts/mcp_catalog.py` 與 `scripts/eval_tool_routing.py`(允許形狀 `opencode_effective_chars`、`era`)、`eval/fixtures/tool_routing/support_matrix.json`(資料)、`README.md` / `README_DEV.md` / `docs/troubleshooting.md` / `docs/security.md` / `docs/basic-usage.md` / `AGENTS.md`(散文可提名字;「教使用者用」的形狀照舊是違規,另加 `python3 opencode_migrate.py` 這種本 repo 相對路徑的形狀);`_OPENCODE_WHOLE_FILE` 清空;三條 JS stub 契約與兩個 helper 刪除。
- environ gate:`_ENVIRON_ALLOWLIST` 剪掉沒有讀取的 10 筆與已刪檔;`core`(准讀 CodeTrail 設定名的檔案)清空;`_SPAWN_CORE` 不動(launcher 仍直接 spawn tmux,那是啟動核心的 spawn 豁免,與讀設定無關)。
- docs gate:`_DOC_CORE_VARIABLES` / `_DOC_CORE_SECTIONS` / `_LAUNCHER_SCRIPTS` 清空並移除對應分支;`_STALE_DOC_PATTERNS` 新增 `export`/指派形狀的 `LLAMA_BIN` / `MODELS_DIR` / `MAIN_GPU` / `AUX_GPU` / `EMBED_GPU` / `RERANK_GPU` / `VL_GPU` / `MAIN_SESSION` / `AUX_SESSION` / `EXPECTED_LLAMA_SERVERS` / `MAIN_HEALTH_TIMEOUT` / `RAG_HEALTH_TIMEOUT`(這些沒有 CodeTrail 前綴,現有 gate 抓不到);`test_model_facing_text_never_teaches_a_removed_environment_knob` 的 `allowed` 只留概念名與協定標記,`core_only` 只留 `scripts/check_readme_consistency.py`。
- gate 自測(`test_the_docs_gate_catches_bare_mentions_and_scopes_core_names_to_their_sections` 等)依新規則改例子:核心段落不再是豁免。

### I-7 交接目錄的 gate 排除(T-0)

`_walk_files()` 在 `dirnames` 剪枝時加一條:`Path(dirpath, d).relative_to(root) == Path("docs/workflows")` 就剪掉;新增 smoke 自測 `test_the_workflow_handoff_directory_is_not_scanned`(在 `docs/workflows/x.md` 寫入 `opencode` 與 `export AICODE_MODEL=1`,三個 gate 都不得報)。

---

## 3. 設計

### 3.1 目標 a:`/session` 選單、重播、大綱

**指令面(`COMMANDS` 表,`/help` 與補全共用同一份)**

| 指令 | 行為 |
|---|---|
| `/session` | 開選單(`SessionPickerScreen`,`ModalScreen[str \| None]`,內容 `OptionList`,每列 `<updated 本地時間>  <turns> 輪  <first_prompt>`;Enter 選、Esc 取消;最多列 50 筆,由新到舊) |
| `/session <id>` | 等同 `/resume <id>` |
| `/resume <id>` | 保留(既有相容) |
| `/sessions` | 保留;每列改印大綱:`<id>  <時間>  <turns> 輪  <first_prompt>`(最多 20 筆) |
| `/new` | 行為改一處:成功後**清空對話區**再貼「新對話」(不清的話上一段對話的畫面留在新對話上方) |

`/session` 放在 `COMMANDS` 的 `/sessions` 之後;`/help` 仍是第一個(`test_typing_a_slash_offers_completions` 靠 Tab 補到 `/help`)。

**切換流程(單一函式 `_switch_session(session_id)`,`/session <id>`、選單選定、`/resume` 都走它)**

1. `_busy_notice("/session")` → busy 就拒絕(文字同現行)。核准框開著時輸入框收不到指令;選單本身是 modal,開著時不可能開始新回合(只有 UI 執行緒會 `start_turn`)。
2. `engine.resume(session_id)` — 失敗(`SessionStoreError` / `ValueError` / 任何例外)→ `ErrorLine("無法接續:…")`,**engine 與畫面一個都不動**(engine 端本來就是原子換;畫面端只在成功後才清空)。
3. `coordinator.session_changed()`(compactor `rebind()`;上一段的 `previous_summary` / `last_anchor` / 停用狀態不得跟過來,新 session 自己的 ledger 停用要生效——既有契約,由 `test_client_compaction` 守)。
4. `_tools.clear()`;`_assistant = _reasoning = None`;`_turn_started = None`。
5. `_replay_history(clear=True)`;貼 `NoticeLine("已接續 <id>(N 則訊息)")`;`_recount_context()`;`_refresh_status()`。

**啟動重播**:`on_mount()` 在 banner 與「輸入 /help」之後,`if self.engine.messages: self._replay_history(clear=False)`。`codetrail_chat.command_chat` 不用改(`_build()` 已經在 App 建立前 `resume`)。

**重播呈現(I-2 → widget)**:`user` → `UserMessage`;`summary` → `SummaryBlock`(Collapsible,預設收合,標題含「已壓縮」與則數);`reasoning` → `ReasoningBlock`(`display=show_reasoning`,`/thinking` 切換時一併生效——它用 `query(ReasoningBlock)`);`assistant` → `AssistantBlock.finish(text)`(Markdown);`assistant_error` → `ErrorLine`;`tool` → `ToolBlock(tool, arguments, status)` + `set_output(...)`,輸出用現行 `_tool_output(call_id)`(從 `engine.messages` 取 content + `structuredContent`,與即時路徑同一份文字),`pending` 用固定字串。壓縮後的 session:`engine.messages` 就是「摘要 + 逐字 tail」,重播的是模型現在看得到的那一份;摘要區塊標題寫「先前 N 則訊息已壓縮」(N = 原始記錄中最後一筆 compaction 之前的 message 數,由 `SessionInfo.messages` 與 tail 長度算,或直接由 store 記錄數)。要不要把壓縮前原文另放一個收合區,列為 D-a3。

**Ctrl-C / Ctrl-D 與選單**:`action_interrupt` 先看 `isinstance(self.screen, SessionPickerScreen)` → `dismiss(None)` 並 return(不算「中斷」、不算離開的第一按);`action_leave` 同樣先收選單。

**大綱來源(I-1)**:只讀本地 session 檔;不呼叫模型、不寫任何檔、不進事件流、不進 MCP stderr。`list_sessions()` 本來就逐檔 `read()`,成本不變;選單只取前 50 筆。

**CLI `codetrail_chat.py sessions`**:輸出改為 `<id>\tturns=<n>\tupdated=<ISO 本地時間>\t<first_prompt>`(D-a2:它是內部入口、印到使用者自己的終端;`docs/session-model-eval.md` 教使用者用它找 id,大綱能幫助辨認)。

**不做的事**:不改 `client_engine.resume()`、不改 store 的讀寫防線、不改 `client_turns`、不加 LLM 摘要、不改 session 檔 schema(`SCHEMA_VERSION` 仍 1)。

### 3.2 目標 b:移除與舊安裝的手動還原路徑

**刪**:§1.2 第 1 類全部。`scripts/doctor.py` 少掉一個 `check`(`check_legacy_web_backend` 留著,它偵測的是網頁後端殘留)。

**改名留下**:§1.2 第 2 類(I-5)。`client_compaction.py:94-98` 的 import 期 fail-loud 改成綁 `OUTPUT_TOKEN_MAX`;`config.py:406` 註解跟著改。

**保留並在 gate 裡註明理由**:`process_env.STRIPPED_ENV_PREFIXES` 的 `OPENCODE_`;`scripts/eval_tool_routing.py:1588` 目前是一份字面複製的前綴 tuple → 改成 import `process_env.STRIPPED_ENV_PREFIXES`(消掉一個會漂移的副本);`opencode_effective_chars` 欄位名與 `support_matrix.json` 的歷史列。

**舊安裝升級 → 本版不再內建任何遷移;文件提供兩條手動路徑**(寫進 `docs/troubleshooting.md`,把現在重複的兩段合併成一段「從舊世代(OpenCode 前端)升級後的手動還原」;README §1.2 與升級 bullet 只留一句指向它):

1. **固定舊版執行一次性遷移**:`a1682d5` 是最後一個附帶 `opencode_migrate.py` 與兩個 plugin stub 的 commit。
   ```bash
   cd <CODETRAIL_REPO>
   git worktree add /tmp/codetrail-legacy a1682d5
   python3 /tmp/codetrail-legacy/opencode_migrate.py --check    # 零寫入,先看
   python3 /tmp/codetrail-legacy/opencode_migrate.py            # 有備份;只還原現值仍等於當年寫入值的鍵
   git worktree remove /tmp/codetrail-legacy
   ```
   舊工具的 ownership 語意(`mcp.codetrail` 與 `permission` 不動、別份安裝的接管不動、狀態檔在設定寫成功之後才刪)全部沿用,因為跑的就是那一版的程式。機器上若還有舊世代 checkout,用它自己的工具亦可。
2. **完全手動**(不想 checkout 舊版時):打開 `~/.config/opencode/opencode.json`,(a) `plugin` 陣列中路徑以 `opencode_plugins/codetrail-compaction.js` 或 `opencode_plugins/codetrail-notify.js` 結尾的項目刪掉——**本版已不再附帶這兩個檔,留著的話你在任何專案開 OpenCode 都會因載不到 plugin 而失敗**;(b) `compaction` 物件的 `auto` / `tail_turns` / `preserve_recent_tokens` / `prune` 依 `~/.config/codetrail/compaction.json` 的 `managed.<key>.prior` 還原:`prior.present == true` 就寫回 `prior.value`,`false` 就刪掉那個鍵;(c) 兩步都完成後刪除 `~/.config/codetrail/compaction.json`。本版的任何程式都**不讀、不寫、不刪**這三個檔(既有 §2 條款保留)。

**跨世代共用檔不變**:`compaction-stopped.jsonl`、`setconfig-last-transaction.json`、canary 快取檔名帶 schema 號——一個檔名都不改;restore manifest 的「目標全部落在本代四檔」白名單照舊,`test_a_manifest_with_a_foreign_target_is_refused_whole` 仍以 `.config/opencode/opencode.json` 當外來目標的例子(那只是例子,不是依賴)。

### 3.3 目標 c:啟動核心改吃檔案與 argv

**`~/start.sh`(set_config 產生)變薄**:保留註解區、`case` 子命令(`status` / `stop` / `logs` / `help`)與 `exec python3 <repo>/scripts/launch_servers.py --scope all "$@"`;刪掉整段 `unset …`、`export AICODE_MODEL=`、`export LLAMA_BIN=`、`export MAIN_GPU=` 等。`logs` 子命令裡的 `${XDG_STATE_HOME:-$HOME/.local/state}` 是檔案位置,保留。

**`set_config`**:`--llama-bin` argv(預設順序:既有 `deployment.json.llama_bin` → `~/llama.cpp/build/bin/llama-server`),驗證旗標能力後把絕對路徑寫進 `deployment.json.llama_bin`;每個 role 的 GPU selector 寫進 `services.<role>.gpu`;`validate_payloads()` 改呼叫 `load_effective_profile(deployment_config=<暫存檔>, model_registry_file=<暫存檔>)`(不再組 env);`preview_start_commands()` 改以 `--deployment-config` / `--model-registry-file` argv 跑 `launch_servers --dry-run`;`_fresh_env()` / `_sanitized_subprocess_env()` 收斂成 `process_env.child_env()`;`running_codetrail_sessions()` 只看 `TMUX_SESSIONS` 常數;刪 `_OVERRIDE_ENV_KEYS` / `_SESSION_ENV_KEYS` 與「split-brain」警告(`2802-2816`);摘要頁最後那行 `AICODE_MODEL=… python3 scripts/doctor.py` 改成 `python3 scripts/doctor.py`。

**launcher / stop / status**:依 I-3 / I-4。`launch_servers.main()` 不再 `dict(os.environ)`,loader 只拿 HOME;tmux 的 `subprocess.run` 照舊(啟動核心的 spawn 豁免不變,見 I-6)。`deployment_profile.py exec` 的 `os.execvpe` 改用 `process_env.child_env()`(llama-server 不需要任何 CodeTrail 設定名;剝掉才不會把另一份安裝的殘留帶進去)。`deployment_status.inspect_deployment(environ=)` 參數刪除(它只拿去 `resolve_model_reference` 查 registry,改交 HOME-only)。

**客戶端側順帶簡化(選做,不列驗收)**:`config._file_env()`、`client_preflight.profile_env()`、`scripts/doctor._profile_env()` 等六個 HOME-only helper 在 overlay 消失後仍正確,保留即可;`scripts/doctor._profile_env()` 目前把 `--profile` 塞進 env 的 `AICODE_PROFILE` 鍵 → 改成 `load_effective_profile(profile=…)` kwarg(C1 順手,因為 `AICODE_PROFILE` 鍵不再被讀)。

**文件**:README §3.2 / §3.3 / §4.0 / §4.1 / §4.2、`docs/setup.md` systemd 範例(改成 `ExecStart=… deployment_profile.py exec main`,不再有 `Environment=`)、`docs/deployment-profiles.md`(「env 覆寫」段刪、GPU precedence 改成「`--<role>-gpu` argv > `--aux-gpu` argv > `services.<role>.gpu`」)、README_DEV(`--jobs`、`--llama-bin`);`scripts/stop_servers.py:281` 給使用者看的訊息「等待上限可用 AICODE_STOP_TIMEOUT 調整」改成 `--timeout`(C3);`scripts/check_readme_consistency.py` 第 5 條改成要求 README 講 `deployment.json` 的 `main.model`;`eval/spec_questions.json` `spec_001` gold 改成 `main.model` / `deployment.json`,`spec_003` 改成 `PATCH_ENABLED` / `client.json` 相關 gold(`config.py:1074/1127` 的 stale 註解同時刪掉,所以必須一起改,否則 `check_eval_consistency` 紅)。

**保留的環境項目與理由**(最終答案,寫進 AGENTS §3):

| 項目 | 誰讀 | 理由 |
|---|---|---|
| `HOME`(`USERPROFILE` 只在 HOME 缺席時) | config / client_* / lessons / index_scope / external_import / deployment_profile / scripts | 「檔案在哪」;`~/.config/codetrail`、`~/.local/state`、`~/.cache` 的根 |
| `XDG_STATE_HOME` / `XDG_CACHE_HOME` | client_store / client_compaction / mcp_lease / tool_call_canary / launch_servers(log 目錄) | 同上,且是 XDG 規格 |
| `PATH` | `shutil.which`(tmux / objdump / llama-server 探測) | 行程介面 |
| `PYTHONIOENCODING`(**寫入**,不讀) | mcp_server / eval/run_eval | 行程介面 |
| `PYTEST_DISABLE_PLUGIN_AUTOLOAD` / `PYTEST_DEBUG_TEMPROOT`(寫入) | scripts/run_tests | 給 pytest 子行程的介面 |
| `process_env.child_env()` 對 `AICODE_` / `AI_CODE_` / `CODETRAIL_` / `OPENCODE_` 的**剝除** | 所有 spawn | 安全清理:殼層殘留的設定名與升級機器的機密不得進子行程;不是「讀取」 |
| `CUDA_VISIBLE_DEVICES`(**輸出**到 llama-server 命令前綴) | build_server_command | 對 CUDA 的行程介面;不再當輸入 |

---

## 4. Task DAG

每個子任務由一個獨立的 Opus CLI 執行(`model=claude-opus-5`,`--effort max`),只動自己 owner 欄的檔案;共用檔(`tests/test_repo_consistency.py`、`tests/test_smoke_gate.py`、`AGENTS.md`、`README.md`、`README_DEV.md`、`docs/*.md`、`eval/spec_questions.json`、`scripts/check_readme_consistency.py`)一律由 `D1` 整合,其他任務只在自己的交接檔(`docs/workflows/session-opencode-cleanup/handoff-<task>.md`)寫下要 D1 改的段落與理由。

| ID | 依賴 | owner 檔案 | 交付 | 驗收(可勾) |
|---|---|---|---|---|
| **T-0** gate 放行交接目錄 | — | `tests/test_repo_consistency.py`(只有 `_walk_files` + 一條自測) | I-7 | 自測綠;本目錄三個 `.md` 存在時三個 gate 不報 |
| **A1** session 大綱 | — | `client_store.py`、`tests/test_client_store.py`、`codetrail_chat.py`(只有 `command_sessions`) | I-1;新 smoke 契約 `test_the_outline_is_the_first_real_question_never_the_summary_or_tool_output` | `SessionInfo` 新欄位有值;synthetic / tool / assistant 內容不進大綱;超長截斷;`list_sessions(limit=)`;既有 store 測試全部不動 |
| **A2** TUI 重播 + `/session` | A1 的介面(I-1,可先用 stub 開工) | `client_app.py`、`tests/test_client_app.py` | I-2;regression red/green(§5.1);契約 smoke(§5.2) | `/resume` 與啟動 resume 都重播完整歷史(文字 / 工具 / 摘要 / reasoning);`/session` 選單可選可取消;busy 拒絕;失敗保持 session 與畫面;`/new` 清畫面;`_tools` 不含重播 block;`/help` 補全仍第一 |
| **B1** 移除 OpenCode 整合 | — | 刪 `opencode_migrate.py`、`opencode_plugins/`、`tests/test_opencode_migrate.py`;改 `compaction_formula.py`、`client_compaction.py`、`config.py`(註解)、`client_engine.py` / `client_mcp.py` / `client_policy.py` / `context_budget.py` / `mcp_server.py` / `mcp_lease.py`(只改註解)、`scripts/mcp_catalog.py`(註解)、`tests/test_compaction_formula.py`、`tests/test_client_compaction.py:85`、`tests/test_evals.py`(兩個改名 + `cmd` fixture)、`tests/test_deployment.py`(**只有** fixture 的 `OPENCODE_CONFIG` delenv 那一行;其餘歸 C1)、`tests/test_doctor.py`(兩處 pop)、`tests/_set_config_harness.py`(`OPENCODE_CONFIG`) | I-5;handoff 給 D1 的 gate / docs / AGENTS 清單 | `python3 -m compileall -q .` 綠;grep `-i opencode` 在 owner 檔案只剩 `process_env.py` 與 `scripts/mcp_catalog.py` 的歷史欄位名;`combine_settings` 的唯一消費者已刪 |
| **C1** 啟動核心 loader | — | `deployment_profile.py`、`deployment_status.py`、`model_resolution.py`(`resolve_main_model` 改名 + 註解)、`client_preflight.py`、`scripts/doctor.py`(含刪 `check_legacy_opencode_install`、`_profile_env` 改 kwarg、`:739` 字串)、`scripts/required_model_servers_check.py`、`scripts/session_eval.py`(呼叫端 + `:448` 註解)、`scripts/tool_call_canary.py`(呼叫端 + 註解)、`scripts/eval_tool_routing.py`(`:1588` 前綴 tuple 改 import `process_env.STRIPPED_ENV_PREFIXES` + 註解)、`tests/test_deployment.py`(env 優先序測試 + 新契約)、`tests/test_client_preflight.py`(呼叫端改名) | I-3;新 smoke 契約 `test_the_loader_ignores_every_legacy_override_variable` | 舊 overlay 的每一個名字設在環境裡對有效 profile 零影響;`llama_bin` / `gpu` 由檔案讀出;缺鍵走預設;字串常數不再教任何 `AICODE_*` |
| **C2** set_config + start.sh | C1 | `scripts/set_config.py`、`tests/test_set_config.py`、`tests/_set_config_harness.py` | `--llama-bin`;寫 `llama_bin` / `gpu`;start.sh 零 export / unset;預覽與驗證走 argv / kwarg;B1 交代的三個字串 | 產生的 start.sh 不含 `export` / `unset`;`deployment.json` 含 `llama_bin` 與四個 `gpu`;殼層設舊 override 對 `--dry-run` 輸出零影響(既有測試 `test_generated_start_sh_clears_legacy_env_overrides` 換名續用) |
| **C3** launcher / stop / status / run_tests / eval 腳本 | C1 | `scripts/launch_servers.py`、`scripts/stop_servers.py`、`scripts/check_status.py`、`scripts/run_tests.py`、`eval/record_semantic_vectors.py`、`tests/test_server_scripts.py`、`tests/test_test_runner.py` | I-4;新 smoke 契約 `test_the_launcher_reads_gpus_and_llama_bin_from_the_deployment_file_not_the_shell` | `grep -n "os.environ\|getenv"` 在這五個腳本只剩 HOME / XDG / PYTEST_* 寫入;`--help` 各自可跑(`test_repo_consistency` 的 `--help` smoke) |
| **D1** 整合 | A1 A2 B1 C1 C2 C3 | `AGENTS.md`、`README.md`、`README_DEV.md`、`docs/*.md`、`scripts/check_readme_consistency.py`、`eval/spec_questions.json`、`tests/test_repo_consistency.py`、`tests/test_smoke_gate.py` | I-6;§6 的條款;§5.2 的 gate 登記;`python3 scripts/check_eval_consistency.py` 與 `python3 scripts/check_readme_consistency.py` 綠;**唯一一次** `python3 scripts/run_tests.py -m smoke`;red/green 證據彙整 | smoke 綠(或失敗集合 ⊆ 動工前基線);三個 gate 的自測反例仍紅;`SAFETY_MODULES` 沒有指到不存在的 node |

Wave 1 四個任務彼此零檔案重疊(已逐檔核對;`tests/test_deployment.py` 是唯一由 B1 與 C1 都碰的檔,B1 只刪 fixture 裡一行 `delenv("OPENCODE_CONFIG")`——為了避免衝突,**這一行也交給 C1 做**,B1 的交接檔註明即可)。owner 欄與 `inventory.md` §B / §C 表一致;不一致時以本表為準並回寫 inventory。

---

## 5. 測試計畫(developer 權責:只跑自己新寫的 regression 的 red / green,最後一次 smoke 由 D1 跑)

### 5.1 真實 bug 的 regression(標 `@pytest.mark.smoke`;先在**未改實作**上單跑貼紅燈,再改實作單跑貼綠燈)

| node(`tests/test_client_app.py`) | 情境 | 預期紅燈 |
|---|---|---|
| `test_resume_replays_the_stored_history` | `_Engine.resume` stub 把 `messages` 設成 `[user q, assistant a, assistant(tool_calls c1), tool c1]`;`app._command("/resume <id>")` | 現在 snapshot 的 `users == []`、`assistant == []`、`tools == []`(只有一行 notice) |
| `test_a_session_resumed_at_startup_is_shown_on_mount` | `engine.messages` 預先有內容再 `run_test()` | 現在 `on_mount` 只有 banner / help notice |

執行命令(單條):`python3 scripts/run_tests.py tests/test_client_app.py::test_resume_replays_the_stored_history`。

### 5.2 新的契約 smoke(無聲失敗風險;登記進 `tests/test_smoke_gate.py::SAFETY_MODULES`)

- `tests/test_client_app.py`:`test_the_session_picker_lists_outlines_and_switches`、`test_a_failed_resume_keeps_the_current_session_and_screen`、`test_escape_closes_the_picker_without_switching`、`test_ctrl_c_with_the_picker_open_closes_it_and_never_leaves`、`test_replay_renders_tools_reasoning_and_compaction_summaries`(純函式 `history_entries` + widget 對映)、`test_replayed_tool_blocks_are_not_registered_for_live_events`。
- `tests/test_client_store.py`:`test_the_outline_is_the_first_real_question_never_the_summary_or_tool_output`。
- `tests/test_deployment.py`:`test_the_loader_ignores_every_legacy_override_variable`(把舊 overlay 的每個名字與 `LLAMA_BIN` / `MODELS_DIR` / session 名全部設進環境,有效 profile 與 `llama_bin` 不變)。
- `tests/test_server_scripts.py`:`test_the_launcher_reads_gpus_and_llama_bin_from_the_deployment_file_not_the_shell`。
- `tests/test_repo_consistency.py`:`test_the_workflow_handoff_directory_is_not_scanned`(T-0)。

### 5.3 既有測試的變更(交付時逐條列出;理由必須是「行為為什麼該變」)

| 檔 | 測試 | 動作 | 理由 |
|---|---|---|---|
| `tests/test_opencode_migrate.py` | 全部 63 條 | 刪除 | 被測模組整個移除;它們守的是「寫使用者 OpenCode 設定」的正確性,本版沒有這條路徑 |
| `tests/test_repo_consistency.py` | `test_the_opencode_plugin_stubs_are_inert`、`test_the_stub_gate_rejects_initialisation_side_effects`、`test_the_js_comment_stripper_is_lexically_aware` | 刪除(含 `_assert_inert_stub`、`_strip_js_comments`) | stub 檔已刪,沒有東西可守 |
| 同上 | `test_opencode_only_survives_in_the_migration_path_and_docs` | 改名 `test_the_removed_frontend_only_survives_in_the_strip_list_history_data_and_upgrade_docs` + 依 I-6 收緊 | 遷移路徑不存在了;gate 從「豁免遷移工具」變成「只豁免安全剝除清單與歷史資料」 |
| 同上 | `test_the_opencode_gate_checks_allowlisted_files_for_dependency_shapes`、`test_the_opencode_gate_scans_config_files_and_the_wrapper`、`test_the_gates_also_catch_ignore_entries_and_bare_assignments`、`test_the_docs_gate_catches_bare_mentions_and_scopes_core_names_to_their_sections` | 改例子 | 例子引用 `scripts/set_config.py` import 遷移工具、核心段落豁免——兩者都不再存在;核心變數不再有合法段落 |
| 同上 | `test_user_docs_must_not_teach_removed_flags_or_files` | 加 stale 例子 `python3 opencode_migrate.py --check`、`export LLAMA_BIN=` | 文件不得再教本 repo 已刪的工具與 env 契約 |
| 同上 | `test_model_facing_text_never_teaches_a_removed_environment_knob` | `allowed` / `core_only` 收緊 | 啟動核心的變數已刪,模型看得到的字串不得再提 |
| `tests/test_smoke_gate.py` | `SAFETY_MODULES` | 刪 `test_opencode_migrate.py` 鍵;刪 `test_aicode.py::test_aicode_never_execs_opencode`、`test_compaction_formula.py::test_combining_two_models_keeps_the_single_model_relationships`、上述三條 stub node;改名 gate node 與 `test_evals.py` node;新增 §5.1 / §5.2 node;改 `test_deployment.py` 的說明文字 | manifest 要指向存在的 node;新增的安全檢查點要被 smoke 守 |
| `tests/test_aicode.py` | `test_aicode_never_execs_opencode` | 刪除 | 與 `test_the_only_exec_target_is_the_client_next_to_the_wrapper` 重複(後者已釘住唯一的 exec 目標) |
| `tests/test_compaction_formula.py` | `test_combining_two_models_keeps_the_single_model_relationships` | 刪除 | `combine_settings` 是死碼(客戶端沒有第二個摘要模型)(D-b3) |
| 同上 | `test_derive_settings_follows_the_upstream_formula` 等 | 只改 docstring / 常數名 | 數字與斷言不變 |
| `tests/test_evals.py` | `test_opencode_timeout_is_a_scored_case_failure_not_a_suite_abort` → `test_a_replay_timeout_is_a_scored_case_failure_not_a_suite_abort`;`cmd=["opencode","run"]` → `["python3","codetrail_chat.py","run"]`;`test_model_probe_endpoint_must_match_effective_opencode_provider` → `..._effective_profile` | 改名 + fixture | 名稱描述的是不存在的執行路徑;斷言不變 |
| `tests/test_deployment.py` | `model_resolution_env` fixture | 拿掉 `OPENCODE_CONFIG` delenv | 沒有程式讀它 |
| 同上 | `test_opencode_json_is_no_longer_a_model_source` 等三條 | **保留不改** | 它們守的是「runtime 永不讀 `~/.config/opencode`」,這條在移除後更重要 |
| 同上 | `test_precedence_cli_env_over_local_over_profile_over_defaults` → `test_precedence_cli_overrides_over_local_over_profile_over_defaults`;`test_canonical_n_ctx_override_wins_over_legacy_main_ctx` 刪;`test_explicit_local_override_must_exist` 改 kwarg;`test_profile_selector_precedence_cli_then_env_then_local` → `_cli_then_local`;`test_main_and_aux_gpu_split_and_three_aux_share_one_gpu` / `test_per_role_gpu_override_wins_over_aux_gpu` 改成檔案 + overrides;`test_bind_all_interfaces_via_override_and_env` 去掉 env 那一半 | 改寫 / 刪除 | env overlay 這一層不存在了;其餘層級的優先序不變 |
| `tests/test_set_config.py` | `test_generated_start_sh_clears_legacy_env_overrides` → `test_generated_start_sh_ignores_legacy_shell_overrides`(斷言不變) | 改名 | 機制從 `unset` 變成「沒有人讀」,要守的行為相同 |
| 同上 | `test_generated_start_sh_exports_before_subcommand_dispatch`、`test_deployment_env_override_split_brain_warns` | 刪除 | start.sh 不再 export;override env 不存在 |
| 同上 | `test_start_sh_pins_validated_llama_bin` | 改成斷言 `deployment.json.llama_bin` 等於驗證過的 binary 且 start.sh 不含 `export LLAMA_BIN` | llama-server 路徑的真值搬到檔案 |
| 同上 | `test_restart_subprocess_env_is_sanitized` | 改成斷言 `AICODE_*` 被剝、`KEEP_ME` 保留;去掉 SESSION 三個斷言 | session 名是常數 / argv,環境裡的值無關 |
| `tests/_set_config_harness.py` | `PROFILE_ENV_KEYS` | 刪 `OPENCODE_CONFIG`、改引用 | 來源常數已刪 |
| `tests/test_server_scripts.py` | `_clean_env` / `_run_launcher` fixture 改寫成「寫 deployment.json + argv」;`test_start_all_routes_main_and_all_aux_to_their_shared_gpus`、`test_quit_still_kills_sessions_when_deployment_config_is_broken`(`--main-session/--aux-session`)、`test_health_timeout_scales_with_model_size`、`test_rollback_respects_no_rollback_env`(`--keep-on-failure`)、`test_start_rag_servers_dry_run_uses_base_url_ports`(base_url 進檔案;拿掉 `rerank_fallback_policy` 相關)、`test_check_status_*`(`--proc-root` / `--snapshot` / `--expected`)、`test_stop_timeout_env_override_and_fallback`(argv) | 改寫 | 介面由 env 換成檔案 / argv,被守的行為(GPU 路由、壞設定仍能 stop、逾時、rollback)不變 |
| 同上 | `test_legacy_aux_launcher_env_names_still_override_profile` | 刪除;由 §5.2 的新契約(設了也無效)取代 | 這條守的正是要移除的行為 |
| `tests/test_test_runner.py` | `test_parallel_job_resolution*` | 改成 `--jobs` | 介面搬到 argv |
| `tests/test_client_app.py` | `test_switching_sessions_is_refused_while_a_turn_is_running` | 參數化多加 `/session` 與 `/session <id>` 兩組 | 新指令走同一條 busy 拒絕路徑,要一起被守;既有兩組斷言不變 |
| `tests/test_client_preflight.py` | 呼叫 `resolve_main_model_from_env` 的地方 | 改名 | 函式改名;HOME-only 的斷言不變 |

### 5.4 最後一次 smoke

由 D1 在整合完成後對當時的 tree 跑一次 `python3 scripts/run_tests.py -m smoke`,回報命令、結果與 HEAD;交付註明 `Tests: smoke only — reviewer owns full execution.`。動工前基線由編排者在 `a1682d5` 上另行取得(本階段不跑)。

---

## 6. AGENTS.md / README_DEV.md 同步條款(D1;使用者已授權更新對應條款)

- §2 刪除:「`opencode_migrate` ——唯一會寫使用者 OpenCode 設定的路徑…」整條;「`opencode_migrate` 的壓縮模式 ownership 狀態檔…」整條;「`opencode_plugins/*.js` 現在是 inert stub…」整條。
- §2 改寫:`compaction_formula` 條 → 「門檻公式、canonical 規則文字與 `OUTPUT_TOKEN_MAX` / `COMPACTION_RESERVE_TOKENS` / `MIN_PRESERVE_RECENT_TOKENS` 的單一真值;`config.CLIENT_MAX_OUTPUT_TOKENS` 的上限 fail-loud 綁 `OUTPUT_TOKEN_MAX`」;`config.CLIENT_MAX_OUTPUT_TOKENS` 條同步改常數名。
- §2 保留並改字:「兩份安裝並存的共用檔」條 → 保留三個檔名不改、restore 白名單語意不變、canary 快取帶 schema 號;末句改成「main runtime 永不讀、寫、刪 `~/.config/codetrail/compaction.json`、`~/.config/opencode/*` 與任何舊 plugin 路徑;舊安裝的還原只走 docs/troubleshooting.md 的固定舊版 / 手動路徑」。
- §2 新增:`client_app` 條加「切換 session(`/session` / `/resume` / 啟動 `-c`)必須重播完整已存歷史(文字、工具結果、壓縮摘要、reasoning);busy 或核准中不得換;失敗保持目前 session 與畫面;重播的工具 block 不得登記給即時事件」;`client_store` 條加「大綱只取自本地真實 user 訊息、零 LLM、零寫入」;新增「啟動核心」條:「GPU selector、llama-server 路徑、tmux session 名、逾時與 rollback 開關只來自 `deployment.json`、repo 常數與 argv;`~/start.sh` 不得 export / unset 任何設定名」。
- §3 改寫「不要新增 `os.environ` 讀取」:刪掉「與**啟動核心**(...)」那個例外句;允許清單維持 `HOME`、`XDG_*`、`PATH`、`PYTHONIOENCODING`(寫入);`tests/test_repo_consistency.py` 的 allowlist 現在對啟動核心一樣生效。
- README_DEV:維護命令索引刪 `opencode_migrate.py` 兩行;`AICODE_TEST_JOBS=1 …` → `python3 scripts/run_tests.py --jobs 1`;測試指南刪 `test_opencode_migrate.py`;「改 config / docs / eval」段刪 `MANAGED_COMPACTION_KEYS` bullet;eval 段 `LLAMA_BIN=… python3 eval/record_semantic_vectors.py --record-vectors` → `--llama-bin`。

---

## 7. 驗收清單(最終交付要逐條打勾)

**a**
- [ ] `/resume <id>`、`/session <id>`、`/session` 選單選定、`aicode -c`、`aicode --session <id>` 五條路都顯示完整歷史;順序與 session 檔一致。
- [ ] 工具區塊:摘要行(名稱 + 參數)、狀態符號、展開後有 content 與 `structuredContent`;懸空呼叫顯示 pending 字樣。
- [ ] 壓縮過的 session:摘要以收合區塊呈現並標示則數;tail 逐字顯示。
- [ ] reasoning 隨 `/thinking` 顯示 / 隱藏;預設依 `client.json.show_reasoning`。
- [ ] 回合進行中 `/session` / `/resume` / `/session <id>` 都被拒且 session 不變;核准框開著時指令進不去。
- [ ] resume 失敗(不存在 / 別專案 / 壞 compaction 記錄)→ 錯誤一行,session、畫面、`_tools` 全部不變。
- [ ] 切換後 `compactor.rebind()` 發生(既有 `test_a_session_change_rebinds_the_compactor_without_consuming_the_notice` 續守)。
- [ ] `/sessions` 與 `codetrail_chat.py sessions` 顯示大綱;大綱不含工具輸出 / 摘要 / assistant 文字;不寫任何檔。
- [ ] `client_store` 的讀寫防線一行都沒改(diff 只有 `SessionInfo` 欄位、`session_outline`、`list_sessions(limit)`)。
- [ ] §5.1 兩條 regression 的紅燈 / 綠燈輸出節錄附在交付。

**b**
- [ ] `git ls-files | grep -i opencode` 只剩 `eval/fixtures/tool_routing/support_matrix.json` 與本交接目錄。
- [ ] `grep -rn -i opencode --include=*.py --include=*.js --include=*.sh .`(排除 tests/、docs/workflows/)只命中 `process_env.py`、`scripts/mcp_catalog.py`、`scripts/eval_tool_routing.py` 的允許形狀。
- [ ] `python3 scripts/doctor.py --no-network` 不再有「舊 OpenCode 安裝」檢查行。
- [ ] `docs/troubleshooting.md` 只有一段升級指引,含固定舊版命令與手動三步驟,並明講 plugin 檔已不存在的後果。
- [ ] `compaction_formula` 三個常數改名、值不變;`client_compaction` import 期 fail-loud 仍在(`test_the_output_constant_is_bounded_by_the_derivation_formula` 續守)。
- [ ] `process_env.STRIPPED_ENV_PREFIXES` 仍含 `OPENCODE_`;`test_env_overrides_cannot_reintroduce_a_stripped_prefix` 續守。
- [ ] 三個跨世代共用檔名未改;restore 白名單語意未改。
- [ ] 沒有任何程式碼碰 `~/.config/opencode`;施工機器上該目錄未被修改(D1 交付時以 `ls -la --time-style=full-iso` 對照施工前後 mtime,零寫入)。

**c**
- [ ] `grep -rn "os.environ\|getenv" --include=*.py .`(排除 tests/)每一處都落在 §3.3 保留表;`_ENVIRON_ALLOWLIST` 每一筆都有真實讀取。
- [ ] 產生的 `~/start.sh` 不含 `export` / `unset`;`deployment.json` 含 `llama_bin` 與 `services.*.gpu`。
- [ ] 把舊 overlay 的全部名字設進殼層,`~/start.sh --dry-run` / `status --strict` / `stop` 的行為零差異(§5.2 兩條契約)。
- [ ] README / docs 沒有任何 `export AICODE_*` / `MAIN_GPU=` / `LLAMA_BIN=` 教學;systemd 範例只有 `ExecStart`。
- [ ] `python3 scripts/check_readme_consistency.py`、`python3 scripts/check_eval_consistency.py`、`python3 -m compileall -q .` 綠。
- [ ] `tests/test_repo_consistency.py` 的三個 gate 各自的反例自測仍紅(gate 沒有被放寬到失效)。

---

## 8. 風險與待審決策

| ID | 決策 / 風險 | 我的建議 | 影響 |
|---|---|---|---|
| D-a1 | 大綱長度與欄位(80 字、first / last prompt、四個計數) | 照 I-1 | 純 UI |
| D-a2 | `codetrail_chat.py sessions` 是否也印 `first_prompt` | 印(內部入口,輸出到使用者自己的終端;沒有其他程式解析它) | 若不印,`session_eval` 文件教的找 id 方式仍可用 |
| D-a3 | 壓縮後的重播是否另附「已壓縮的原文」收合區 | 不附(重播的是模型現在看得到的那一份,收合區容易被誤讀成模型知道的內容);摘要區塊標題寫「先前 N 則已壓縮」 | 若使用者要「完整原文」,A2 多一個 Collapsible 即可,列入 deferred |
| D-b3 | 刪 `combine_settings` 與其唯一測試 | 刪(死碼;保留等於守一條沒有人走的路) | `SAFETY_MODULES` 少一個 node;AGENTS 未點名該函式 |
| D-b4 | `opencode_effective_chars` 欄位名與 `support_matrix.json` 歷史列 | 保留(凍結量測資料;改名等於丟資料) | gate 以允許形狀豁免 |
| D-b5 | 三條 `test_opencode_json_is_no_longer_a_model_source` 類測試與 `test_the_client_never_accepts_the_opencode_tool_prefix` 保留原名 | 保留(名稱描述的是仍有效的守門行為;tests/ 不在 gate 範圍) | 零 |
| D-c1 | `deployment.json` 加鍵 vs 另開 `launcher.json` | 加鍵(使用者指定的落點;舊世代 loader fail-loud 而非靜默;README 升級段講明「兩份安裝共用 `~/.config/codetrail` 時,跑過本版 set_config 之後舊世代的 start.sh 會拒絕載入」) | 若選 `launcher.json`,C1 / C2 / docs 改落點,介面 I-3 不變 |
| D-c2 | `CUDA_VISIBLE_DEVICES` 不再當輸入 | 是(它是 launcher 從殼層取 GPU 的最後一條路;輸出端保留) | 沒設 `gpu` 且沒給 argv 的 role 就不指定 GPU(現行「空字串 = 不加前綴」語意) |
| D-c3 | tmux session 名固定常數 + 隱藏 argv | 是(桌面 `SESSION` 撞名的 bug 由構造消失;測試隔離走 argv) | `stop --scope aux` 語意不變 |
| D-c4 | `AICODE_RERANK_FALLBACK_POLICY` 直接刪 | 是(client.json 已是唯一來源;launcher 只在 dry-run 印它) | dry-run 少一行 |
| R-1 | 交接檔讓 gate 紅(§0 第 1 點) | T-0 先做;交接檔與 T-0 同一批提交 | 未做的話 smoke 紅兩條,而且紅在「文件寫了 opencode」 |
| R-2 | `check_readme_consistency` 第 5 條 / `spec_questions` gold 與 `config.py` stale 註解互相支撐 | D1 一起改(§3.3) | 漏一邊 → `check_eval_consistency` 或 readme 檢查紅 |
| R-3 | 使用者機器上仍註冊著已刪的 plugin 路徑 | 文件明講後果與手動移除步驟;本版不再偵測(使用者授權) | 只影響仍在用 OpenCode 的機器 |
| R-4 | Textual `OptionList` 在 pilot 測試裡的鍵盤行為 | A2 用 `app._command("/session")` + `pilot.press("enter")` / `"escape"`,不依賴滑鼠;若 OptionList 行為不穩改 `ListView` | 測試穩定性 |
| R-5 | `list_sessions()` 大綱要讀完整檔(64 MiB 上限) | 選單 `limit=50`;既有 `/sessions` 就是這樣讀的 | 無新增成本類型 |
| R-6 | 測試數量下降(刪 63 + 約 8 條) | 交付逐條列理由(§5.3);判定規則看失敗集合不看數量 | 審核者 full 的基線比對 |

---

## 9. 給 Astra(第 2 步)的審核焦點

1. I-3 的 `deployment.json` 加鍵與舊世代 fail-loud(D-c1)是否可接受;若不可,`launcher.json` 方案是否符合「不另建大套設定系統」。
2. I-6 把 OpenCode gate 改成掃註解全文,是否過嚴(代價:約 40 處註解要改字;收益:歷史說明不再與現況漂移)。
3. §3.2 的固定舊版路徑(`git worktree add … a1682d5`)是否足夠;是否要求把手動三步驟寫成可複製的 `python3 - <<'EOF'` 片段(我傾向不要:那又是一段會讀寫使用者 OpenCode 設定的程式,只是搬到文件裡)。
4. §5.3 的既有測試變更清單是否有「為了綠燈」而非「行為該變」的項目。
5. §5.2 新契約是否遺漏任何 §2 應登記的檢查點(特別是「重播的工具 block 不登記給即時事件」與「大綱零寫入」)。
