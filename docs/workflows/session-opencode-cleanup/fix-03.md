# fix-03(流程第 5 步):smoke-01 三個紅燈的測試 fixture / gate helper 靜態修復

- 修復者身分由編排者記入 `model-log.md`;本檔不自述。本輪開始時 HEAD 為 `c3960dc`(之後只有交接文件
  commit),產品改動仍在工作樹,動工前程式碼為 `a1682d5`。修復範圍與驗收依
  [implementation-review-04.md](implementation-review-04.md) 的 3 個 Blocker,沒有重規劃、沒有擴張。
- 本輪工具只有 Read / Glob / Grep / Write / Edit,沒有 Bash / Agent / MCP。**零測試執行、零 collect、
  零 compile、零 runtime / toy 探查、零 lint、零 CLI、零 git 操作、零真實 HOME / cache / session 讀寫、
  零記憶寫入。** 所有核對都是讀原始碼(含 `/tmp/…/baseline-for-smoke/` 的唯讀基準匯出)與文字比對。
- 只改四個路徑:`tests/test_server_scripts.py`、`tests/test_doctor.py`、`tests/test_repo_consistency.py`
  與新增本檔。**runtime 零改動**;`AGENTS.md`、既有 plan / review / handoff / log、
  `tests/test_smoke_gate.py`(`SAFETY_MODULES`)、smoke marker 一個字都沒動。

## 1. 修了什麼(逐項對應 review-04 Blocker)

| review-04 | 失敗 node(smoke-01) | 本輪修復 | 位置 |
|---|---|---|---|
| Blocker 1 | `tests/test_server_scripts.py::test_stop_and_status_use_argv_and_constants_not_the_shell` | 假 `nvidia-smi` 改成**分階段**寫入:stop 階段 compute-apps 查詢回空(GPU 上沒有任何 process),完成 stop 的原有斷言後,才把同一支假命令切成原有 `FOUR_LLAMA_SERVERS` 四筆給 status 階段用 | `tests/test_server_scripts.py:1109-1122`(nested helper `_fake_nvidia_smi`)、`:1133-1136`(stop 階段)、`:1145-1146`(status 階段) |
| Blocker 2 | `tests/test_doctor.py::test_explicit_gate_and_implicit_diagnostic_are_separate` | 用既有 `_write_profile_model(home, model)` 在 tmp HOME 寫入合成主模型 `/models/from-deployment-file.gguf`;`env` 加 `HOME` 與 `XDG_CACHE_HOME`(只是檔案位置),兩個殘留 shell 值與兩次 `explicit_model=""` 原樣保留 | `tests/test_doctor.py:1342-1354` |
| Blocker 3 | `tests/test_repo_consistency.py::test_the_handoff_markdown_exemption_is_content_only` | `_handoff_markdown` 的深度條件 `len(parts) >= 3` → `>= 4`(`docs / workflows / <任務> / <檔名>`);副檔名與前兩層名稱條件不變 | `tests/test_repo_consistency.py:1072-1074` |

三處都是新測試的情境配置 / 本輪新 helper 的形狀錯誤,不是基準行為的重開;沒有刪 node、沒有 skip / xfail、
沒有改 smoke marker、沒有放寬任何原契約斷言、沒有新增重複測試。

## 2. 逐條測試變更(AGENTS §1.5;理由是「行為為什麼該變」)

### 2.1 `tests/test_server_scripts.py::test_stop_and_status_use_argv_and_constants_not_the_shell`

- **動到什麼**:原本在函式開頭一次寫死的假 `nvidia-smi`(compute-apps 查詢永遠回 `FOUR_LLAMA_SERVERS`)
  改成函式內的 nested helper `_fake_nvidia_smi(rows)`:`case "$1"` 形狀與原來逐字相同,只有
  compute-apps 那一支的回答由 `rows` 決定 —— 非空就 `printf '%s\n' <rows>`(與原本完全相同的字串),
  空字串就 `:`(什麼都不印,exit 0);其餘查詢(`--query-gpu=index,memory.used`、`--query-gpu=index,uuid`)
  照舊 `*) : ;;` 沒有輸出。stop 階段呼叫 `_fake_nvidia_smi("")`,status 階段呼叫
  `_fake_nvidia_smi(FOUR_LLAMA_SERVERS)`。
- **為什麼行為該變**:這條契約的 stop 半段守的是「session 名來自 `TMUX_SESSIONS` 常數、殼層舊名無效」;
  它的假 tmux 說兩個 session 都不存在、假 ss 說沒有 listener,那 GPU 上理應也沒有 llama-server。原 fixture
  卻在 stop 的最終盤點(`scripts/stop_servers.py:357-369`,`scope == "all"` 且 PATH 上有 `nvidia-smi`)提供
  4 筆殘留 process,走進**基準就有**的孤兒提示分支(`a1682d5:scripts/stop_servers.py:346` 同樣是
  `row.used_gpu_memory`,而 `deployment_status.GpuProcess` 當時已叫 `used_memory_mib`)。那 4 筆資料是給
  status 半段「預設 4 / `--expected 5`」數的,不是 stop 的情境。所以 GPU 上有什麼由階段自己決定。
- **保留的斷言(一字未動)**:stop `returncode == 0`;`TMUX_SESSIONS` 兩個常數 session 名的
  `does not exist` 訊息;`shell-main-session` / `shell-generic-session` / `shell-aux-session` 不出現在
  stdout + stderr 也不出現在假 tmux 的呼叫紀錄;status `returncode == 0`、`偵測到 4 個不同的 llama-server
  PID（預期至少 4）`、`預期至少 99` 不出現;`--expected 5` 的 `只偵測到 4 個…（預期至少 5）`。
- **保留的邊界**:假 tmux(記錄 `$*` 後 exit 1)、假 ss(`exit 0`)、tmp HOME 的 `_shell_env`(含整組
  `LEGACY_SHELL_OVERRIDES`)、空的 `--proc-root`、`timeout=15` 全部原樣;不碰真實 tmux / nvidia-smi / ss /
  `/proc`。沒有吞例外、沒有放寬 returncode、沒有 skip、沒有刪 status 資料;**沒有**改
  `scripts/stop_servers.py:364` 那個基準欄位名,也**沒有**替 `GpuProcess` 加相容屬性(review-04 明示不在
  本輪範圍)。
- 另補一行空行(`:1108`),讓 nested def 與前一個 `_write_fake_bin` 呼叫分開;純排版。

### 2.2 `tests/test_doctor.py::test_explicit_gate_and_implicit_diagnostic_are_separate`

- **動到什麼**:`env` 從 `{"AICODE_TOOL_CANARY_WARN_ONLY": "1", "AICODE_MODEL": "shell-leftover"}`
  改成先 `home = tmp_path / "home"`、`_write_profile_model(home, "/models/from-deployment-file.gguf")`,
  再 `env = {"HOME": str(home), "XDG_CACHE_HOME": str(tmp_path / "cache"), 兩個殘留 shell 值}`。
  `_write_profile_model` 是本檔既有 helper(`:30-49`,寫 `services.main.model` 進 tmp HOME 的
  `deployment.json`),沒有新 helper。
- **為什麼行為該變**:基準的 `resolve_main_model_from_env` 會把殼層 `AICODE_MODEL` 當主模型來源,這條
  舊測試因此「剛好」有模型可用;本輪 B-05 把該分支正確刪除(`model_resolution.resolve_main_model`
  只讀 `deployment.json`,`scripts/tool_call_canary.py:976` 已換新 resolver),測試卻沒同步檔案來源,
  於是第一次 `run_all()` 在 `_model_selection` 就以「找不到主模型」回 2,根本沒進到它要驗的
  explicit / implicit 分離。現在模型**只能**從 tmp HOME 的檔案解析出來:`_model_selection` 沒被
  stub、兩次都 `explicit_model=""`、runtime 沒有 env fallback,`shell-leftover` 沒有任何路可以進來。
- **保留的斷言(一字未動)**:第一次 explicit 成功、implicit FAIL 仍回 0;`implicit_calls ==
  [TOOL_CANARY_IMPLICIT_TIMEOUT_SECONDS]`(恰好一次、repo 常數 timeout);stderr 含 `status=fail`;
  第二次 explicit 連續兩次失敗回 2,且 implicit 替身一被呼叫就 `AssertionError`。
- **保留的邊界**:`run_protocol_check` / `fetch_main_server_props` / `run_model_attempt` /
  `run_implicit_model_attempt` 四個離線替身原樣;兩個殘留 shell 值原樣留在 `env` 裡證明無效
  (`AICODE_MODEL` 不是檔案裡的模型;`WARN_ONLY` 沒有把 explicit 失敗放行成 0);**沒有**恢復 runtime
  的 `AICODE_MODEL` fallback;模型與快取位置都在 `tmp_path` 底下,不依賴執行者真實 HOME / cache。

### 2.3 `tests/test_repo_consistency.py::_handoff_markdown`(gate helper;本輪 D1 新增)

- **動到什麼**:`len(parts) >= 3` → `len(parts) >= 4`,加兩行註解說明四層是
  `docs / workflows / <任務> / <檔名>`。`rel.suffix == ".md"`、`parts[0] == "docs"`、
  `parts[1] == "workflows"` 不變。
- **為什麼行為該變**:helper 自己的 docstring(`:1058`)與 plan-final B-06 / T-0 都定義豁免為
  `docs/workflows/<任務>/*.md`;3 層恰好放行 `docs/workflows/p.md`(少了任務目錄那一層),與自己的
  安全契約不一致。這條豁免決定的是 OpenCode 內容 gate 與文件 gate 的 `.md` 來源,放太寬就是多一個
  不被掃的 Markdown 位置。不存在可沿用的基準行為(基準沒有這個 helper;baseline 匯出中亦無此符號)。
- **測試本體零改動**:`test_the_handoff_markdown_exemption_is_content_only`(`:1960`)的所有正 / 負向
  斷言原樣,包含第一次 smoke 釘住的 `assert not _handoff_markdown(Path("docs/workflows/p.md"))`
  (`:1977`);沒有新寫重複測試。
- **保留的邊界**:`_walk_files` / `_repo_sources()` / `_iter_text_files()` 一字未動,走訪仍進
  `docs/workflows/`;同目錄 `.py` / `.sh` / `.json` / `.toml` 照掃;`_opencode_offenders` /
  `_spawn_offenders` 對 `docs/workflows/x/tool.py` 的 offender 斷言原樣。

## 3. 靜態核對(讀碼結論;**不是**任何測試通過的證據)

| 修復 | 讀碼路徑與結論 |
|---|---|
| 2.1 stop 階段 | `stop_servers.main`:假 tmux exit 1 → 兩個 `does not exist`;`tracked` 空 → 不呼叫 `_gpu_compute_pids` / `_wait_released`;profile 從內建 defaults 載入(tmp HOME 無 `deployment.json`,與既有 `_run_check_status` 系列相同);假 ss 無輸出 → 各 port `is free`;`_gpu_memory_line()` 走 `*)` 無輸出 → `None`;`query_gpu_processes()` 收到空 stdout、exit 0 → `parse_gpu_process_csv("")` 回 `[]`、error `""` → `elif rows:` 不成立,不再觸及 `row.used_gpu_memory`;`stuck` 空、`profile_error` None → 回 0 |
| 2.1 status 階段 | 假命令重寫發生在 `subprocess.run` 已返回之後(沒有行程還在執行它);status 階段的 script 內容與修改前逐字相同,`check_status.main` 的路徑與既有 `test_check_status_passes_with_four_unique_llama_server_pids` 一致:4 個不同 PID、空 proc-root → cmdline 讀不到只出 INFO;`--expected` 預設 4 → `[PASS] 偵測到 4 個…（預期至少 4）`;`--expected 5` → stderr `[WARN] 只偵測到 4 個…（預期至少 5）` |
| 2.2 模型解析 | `_write_profile_model` 的 payload `{schema_version: 1, profile: "defaults", services: {main: {model}}}` 走 `load_effective_profile` 的 local override 分支;`_validate_model_reference` 接受絕對 `.gguf` 路徑(`deployment_profile.py:326-329`,不檢查檔案存在);`resolve_main_model` → `normalize_main_model` 的路徑分支(`model_resolution.py:143-144`)→ `ok`;`_model_selection` 回 `(模型, 模型)`,不再 `CanaryError` |
| 2.2 其餘 HOME 相依 | `build_fingerprint` 對缺席檔案用 `_file_digest` 回 `"missing"`;`_client_prompt_digest` / `_compaction_mode_digest` 均包在 `except Exception` 裡;快取寫入 `tmp_path/cache/codetrail/tool-call-canary.v3.json`(與 `_patch_runtime` 系列同一套路徑推導);`force=True` 略過快取讀取。`run_all` 之後的每一步(`run_model_attempt`、`run_implicit_model_attempt`)都是測試替身 |
| 2.3 例子 | `docs/workflows/x/p.md`(4 層)真;`docs/workflows/session-opencode-cleanup/plan-final.md`(4 層)真;`docs/workflows/p.md`(3 層)假;`docs/p.md`、`README.md` 假;`…/tool.py` / `…/ci.yaml` / `…/settings.json` 因副檔名假;真實 repo 目前 `docs/workflows/**/*.md` 全在 `docs/workflows/session-opencode-cleanup/` 底下,都是 4 層 |

上表是讀碼推導。本輪沒有執行任何 node,三條紅燈在紀錄上**仍是紅燈**,轉綠與否只能由編排者依授權安排
重驗後判定。

## 4. 測試執行與未取得的結果

- **零執行。** 預定唯一一次整包 smoke 已在 smoke-01 用完(2395 selected、2392 passed、3 failed、runner
  exit 1);本輪沒有跑 smoke、沒有單跑任何 node、沒有 collect、沒有 compile、沒有 checker。
- 本輪**不宣稱**綠燈、不宣稱 Blocker 0、不宣稱這 3 個失敗已解除;只報「靜態修復完成、3 條先前紅燈仍待
  授權重驗」。單 node 或第二次整包 smoke 由編排者依 AGENTS §1.2 與 plan-final §5.4 另取得同意後安排。
- 這三處不是 §1.3 的 bug fix(沒有新 regression test),而是既有 / 本輪新增測試的 fixture 與 gate helper
  修正;smoke-01 的 traceback 就是它們的紅燈證據。
- `Tests: smoke only — reviewer owns full execution.`(本輪自己零執行。)

## 5. 未改動事實與剩餘

- 未改動:所有 runtime(含 `scripts/stop_servers.py`、`scripts/tool_call_canary.py`、`model_resolution.py`、
  `deployment_status.py`)、`AGENTS.md`、`tests/test_smoke_gate.py`、其他測試檔、`plan-final.md`、四輪審核、
  `smoke-01.md`、`fix-01.md` / `fix-02.md` / `fix-02b.md`、`execution-notes.md`、`test-change-audit.md`、
  `model-log.md`、`deferred.md`、所有 handoff、`.claude/memory`、使用者 HOME 設定、cache、真實 session。
- 歷史偏差紀錄(`execution-notes.md` 的 5 次額外探查與 1 次範圍外記憶寫入)原樣保留,本檔不改寫、
  不抵銷,也不宣稱先前流程全程合規。
- 已知但本輪**刻意未動**的基準缺陷(交編排者決定登記):`scripts/stop_servers.py:364` 的
  `row.used_gpu_memory` 在 `a1682d5:346` 已存在,而 `deployment_status.GpuProcess` 的欄位是
  `used_memory_mib`。只在「scope all 停完 GPU 上仍有 llama-server」時可達,屆時使用者看到的是
  `AttributeError` traceback 而不是孤兒提示、exit 1。review-04 明示不在本輪修復範圍,本輪未改欄位、未加
  alias、未新增 regression。
- 剩餘問題 / 分歧:無。
