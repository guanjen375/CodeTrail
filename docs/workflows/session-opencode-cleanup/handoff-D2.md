# handoff D2(W2):文件、`check_readme_consistency`、spec gold

只動 `plan-final.md` §3 的 **D2** 那一列與本檔:`README.md`、`README_DEV.md`、
`docs/troubleshooting.md`、`docs/security.md`、`docs/setup.md`、`docs/deployment-profiles.md`、
`docs/basic-usage.md`、`scripts/check_readme_consistency.py`、`eval/spec_questions.json`。
**`docs/compaction-rules.md` 與 `docs/mcp-tools.md` 一個字都沒改**(計畫指定不動),
`docs/rag.md`、`docs/lessons.md`、`docs/session-model-eval.md` 核對後零改動(沒有殘留的
環境變數 / 舊前端教學)。

沒有 commit / push / stash / checkout;沒有碰 `~/.config/*`、真實 session、tmux、
llama-server、GPU;**沒有執行任何 pytest**(不 full、不 smoke、不 collect-only、不 `--lf`、
不單跑既有測試)。`Tests: smoke only — reviewer owns full execution.`

依賴:`handoff-B.md`、`handoff-C1.md`、`handoff-S.md` 都已落檔並逐條核對過(C1 給 D2 的
四點 —— GPU precedence、`llama_bin` 優先序、systemd `ExecStart`、`env` 子命令刪除 ——
與本檔寫的一致;`/session` 段的字串對照 S 的落檔程式碼)。C2 / C3 的 handoff 收尾時還沒
出現,但它們的碼已進工作樹,argv 名稱與預設值我用靜態 `grep` 對過一次(見第 4 節第 2 項)。

---

## 0. 實際修改一覽(9 個檔)

| 檔 | 改了什麼 |
|---|---|
| `README.md` | 首段去 `opencode-ai`;Quick Start 的升級 bullet 與 §1.2、§3 開頭改成一句指向 troubleshooting 升級段;§1.5 `LLAMA_BIN` → `--llama-bin` / `deployment.json` 的 `llama_bin`;§2 `MODELS_DIR` → `--models-dir`;§3.1 產物表(`deployment.json` 多 `gpu` / `llama_bin`、`~/start.sh` 不 export/unset)+ 非互動旗標多一條「路徑」+ 唯讀檢查移除遷移那行並補「新鍵對舊世代 fail-loud」;§3.2 `AICODE_NO_ROLLBACK=1` → `--keep-on-failure`,新增旗標表(`--health-timeout` / `stop --timeout` / `status --expected` / loader 全組);§4.0 刪「唯一的例外是啟動核心」改寫成 `exec` 那條線;§4.1 優先序去 env、手動啟動範例改旗標、local override 範例加 `llama_bin` / `gpu`、`deployment_profile.py show` 去前綴;§4.2 `AICODE_MODEL` → `services.main.model`;§5.2 新增「接續舊對話會重播原始記錄」導引;§5.3 `codetrail_` 前綴那句改「舊世代前端」 |
| `README_DEV.md` | 維護命令索引刪兩行遷移命令;`AICODE_TEST_JOBS=1` → `--jobs 1`(附 1..16 說明);測試指南刪 `test_opencode_migrate.py`、`test_client_app.py` 補「session 選單與原始記錄重播」;刪 `MANAGED_COMPACTION_KEYS` bullet;eval 錄製段 `LLAMA_BIN=` 前綴 → `--llama-bin`(並拿掉 `run_code_smoke_eval.py` 那條多餘前綴);`:238` / `:327-342` 歷史量測段保留 |
| `docs/troubleshooting.md` | 刪「`aicode` 說偵測到舊安裝」整節(C1 已刪掉那個偵測);把兩段升級指引**合併成一段**、標題含 `a1682d5`,內容照 §6.2(原安裝路徑身分、固定舊版三情況、手動路徑四條 + 狀態檔 digest、合成設定驗證、新鍵對舊世代 fail-loud);快速分流表加一列指向 `a1682d5`;ctx-safety 修法刪 `unset AICODE_*` 那行;PDF preflight 表頭「env 同名加 `AICODE_` 前綴」→「`config.py`;沒有環境變數可以覆寫」 |
| `docs/security.md` | lessons 那條的「這個 env 是非空即真」→ client.json 布林鍵語意;「不再需要 Node / npm / opencode-ai」改字;升級防護那段改成指向 troubleshooting 升級段 |
| `docs/setup.md` | systemd 範例刪三行 `Environment=`、`ExecStart` 不帶 `--llama-bin`;補「最終環境由 `exec` 決定、不要用 `Environment=` 傳 CodeTrail 設定(系統層變數照常設)」與「要一次性換設定就加 loader 旗標」 |
| `docs/deployment-profiles.md` | `AICODE_PROFILE` → `--profile`;合併順序改旗標;刪 `AICODE_DEPLOYMENT_CONFIG` 那整段、改寫成「設定沒有環境變數這一層 + pane 走 `exec`」;`model` 的 fail-loud 說明改 `services.main.model` / `--main-model`;`bind` 的四個 env 覆寫改設定檔 / `--allow-remote`;新增 `gpu` 欄位說明;GPU precedence 改 argv > 檔案 > 不指定;local override 範例加 `llama_bin` / `gpu` 與新鍵的升級提醒 |
| `docs/basic-usage.md` | 新增 `## 7. 切換與接續對話`(`-c` / `--session`、`/sessions` / `/session` / `/resume` / `/new`、大綱零 LLM 零寫入、重播原始記錄與壓縮標記、pending、`/thinking`、busy 拒絕與失敗零改動、CLI `sessions`) |
| `scripts/check_readme_consistency.py` | 第 5 條改成要求 README 講 `main.model`;`_STALE_DOC_PATTERNS` 加 5 條;新增逐檔例外與 `_check_stale_docs_per_file` / `_stale_doc_sources`;`_check_no_stale_client_docs` 加 `source=` kwarg;OpenCode 相關的歷史註解 / docstring 改字(見 §3 給 D1 的 (b)) |
| `eval/spec_questions.json` | `spec_001` 的 `AICODE_MODEL` → `main.model` / `deployment.json`;`spec_003` 的 `AI_CODE_PATCH` → `PATCH_ENABLED`(gold_evidence 的 `預設停用` / `False` 未動) |

驗證(全部是允許的靜態命令,逐條見 §4 第 6 項):
`python3 scripts/check_readme_consistency.py` **OK**、`python3 scripts/check_eval_consistency.py` **OK**、
`python3 -m compileall -q scripts/check_readme_consistency.py` **OK**、改過的 JSON 仍是合法 JSON。

---

## 1. 落檔的介面

文件本身沒有程式介面。`scripts/check_readme_consistency.py` 有四項 D1 會碰到的符號變動:

```python
# 新增
_STALE_DOC_EXEMPT_SOURCES: dict[str, frozenset[str]]     # pattern → 允許出現它的文件(逐檔例外)
def _stale_doc_sources() -> list[tuple[str, str]]        # (repo 相對路徑, 內容);與 _documentation_text() 同一組檔,不合併
def _check_stale_docs_per_file(issues: list[str]) -> None  # 逐檔跑下面那條 + 例外一致性 fail-loud

# 簽名擴充(相容:舊的兩參數呼叫行為不變)
def _check_no_stale_client_docs(docs_text: str, issues: list[str], *, source: str = "") -> None
#   source="" → 沒有任何例外,每一條 pattern 都適用(合成內容自測走這條)
#   source="docs/troubleshooting.md" → 套用 _STALE_DOC_EXEMPT_SOURCES 的兩條例外
```

`check_all()` 內 `_check_no_stale_client_docs(docs_text, issues)` 改成
`_check_stale_docs_per_file(issues)`;`_documentation_text()`、`_HISTORICAL_DOCS` 與其餘
檢查(1–4、6–11)的簽名與邏輯一字未動。

`_STALE_DOC_PATTERNS` 新增 5 條(§6.1 指定 4 條 + 1 條同源的延伸,理由見 §3 給 D1 的字):

| pattern | 取代它的東西 | 逐檔例外 |
|---|---|---|
| `export\s+LLAMA_BIN=` | `deployment.json` 的 `llama_bin` / `--llama-bin` | 無 |
| `export\s+MODELS_DIR=` | `./set_config.sh --models-dir` | 無 |
| `AICODE_TEST_JOBS=` | `scripts/run_tests.py --jobs N` | 無 |
| `Environment=(?:AICODE_\|AI_CODE_\|CODETRAIL_\|OPENCODE_)` | systemd 只寫 `ExecStart=… deployment_profile.py exec <role>` | 無 |
| `(?m)^\s*(?:[$>]\s*)?python3\s+opencode_migrate\.py` | 本版不附帶那支工具 | `docs/troubleshooting.md` |
| (既有)`~/\.config/codetrail/compaction\.json` | 壓縮模式記在 `client.json` | `docs/troubleshooting.md`(**新增的例外**) |

第 5 條檢查規則變更:`if "AICODE_MODEL" not in readme_text` → `if "main.model" not in readme_text`,
issue 文字改成「README 必須說明主模型怎麼指定(deployment.json 的 `main.model` + models.json
registry)」。理由:那個變數這一代不存在,舊檢查等於**強迫 README 教一個沒有作用的東西**。

---

## 2. test-changes

**D2 沒有新增、改名、改斷言或刪除任何測試**:`plan-final.md` §3 的 D2 清單裡沒有 `tests/**`
的檔,§5.1 / §5.2 也沒有指派給 D2 的 node。因此:

* 新增測試檔:無。
* 改動既有測試檔:無(`tests/` 底下一個 byte 都沒動,含 fixture、import、module-level mark)。
* 新增的 smoke node(供 D1 登記 `SAFETY_MODULES`):**無**。

我改的 `scripts/check_readme_consistency.py` 由 **D1 擁有的三條既有測試**覆蓋,它們必須跟著
改(逐條寫在 §3),但那三條的檔案是 `tests/test_repo_consistency.py`,**不是我能動的檔**,
所以不列在這裡當成我的 test-changes,而是列成給 D1 的待辦。

**未執行的誠實註記**:本輪新寫的檢查邏輯(逐檔例外、5 條新 pattern、第 5 條規則)
**沒有被任何 pytest 跑過**;只跑了 `python3 scripts/check_readme_consistency.py`(整包 12 條
檢查對真實 README/docs 的實跑,綠)與 `python3 scripts/check_eval_consistency.py`(綠)。
「合成反例會不會被抓」這件事我**沒有跑過**(那要動 D1 的測試,而且屬於 pytest),只做了
逐行閱讀:`source=""` 時 `if source and …` 短路成 False → 每一條 pattern 都適用。

---

## 3. 給其他 owner 的字

### D1(`tests/test_repo_consistency.py`、`tests/test_smoke_gate.py`、`AGENTS.md`)

**(a) 三條既有測試必須跟著我改的 `check_readme_consistency.py` 改**(不改就是紅燈,而且是
真的行為變了,不是「這樣才會綠」):

1. `test_user_docs_must_not_teach_removed_flags_or_files`(`:968-992`)
   * `:991` 的 `checker._check_no_stale_client_docs(checker._documentation_text(), issues)`
     → 改成 `checker._check_stale_docs_per_file(issues)`。
     **為什麼該變**:升級段現在必須點名 `~/.config/codetrail/compaction.json` 與
     `python3 opencode_migrate.py`(§6.2 要求),例外是**逐檔**的;把 README 與 docs 合併成
     一大坨之後只能整組放行或整組擋下,等於把 troubleshooting 的例外送給所有文件。
   * `:974-986` 餵合成字串的那一段**不必改**:`_check_no_stale_client_docs(stale, issues)`
     沒有 `source`,所以連 `~/.config/codetrail/compaction.json` 那條也照樣抓得到
     (計畫要求的「README 字串仍要被抓」成立)。
   * 計畫要 D1 加的 `export LLAMA_BIN=` 例子可以直接放進同一個 tuple;`export MODELS_DIR=`、
     `AICODE_TEST_JOBS=1 python3 scripts/run_tests.py`、
     `Environment=AICODE_MODEL=<CODE_MODEL>`、
     `python3 opencode_migrate.py --check` 這四種形狀也都會被抓(前提是不帶 `source`)。
2. `test_code_model_placeholder_contract_passes_with_llamacpp_setup`(`:117-129`)
   * 合成 README 目前是 `export AICODE_MODEL=<CODE_MODEL>`,現在會回一條 issue。
     改成含 `main.model` 的寫法即可,例如
     `'本專案使用 llama-server 跑 GGUF 模型。deployment.json 的 main.model 填 <CODE_MODEL>。'`。
   * **為什麼該變**:第 5 條檢查的目標從「README 有沒有提那個環境變數」換成「README 有沒有
     講主模型填在 `deployment.json` 的 `main.model`」。
3. `test_code_model_placeholder_contract_reports_missing_bits`(`:132-139`)
   * `assert any("AICODE_MODEL" in issue for issue in issues)` → `any("main.model" in issue …)`。
   * 同上;issue 文字裡已經沒有那個變數名。

**(b) OpenCode gate 的形狀豁免**:`_OPENCODE_SHAPE_EXEMPTIONS["scripts/check_readme_consistency.py"]`
目前是 `re.compile(r"opencode-ai|npm")`,**必須照 §6.1 擴成 `opencode-ai|npm|opencode_migrate|OPENCODE_`**。
我已用該 regex 對全檔(含註解與 docstring)逐行核對:11 行提到 `opencode`,全部落在這四種形狀內,
其中 4 行只靠 `opencode_migrate`(新的 stale pattern、它的 label、逐檔例外的 key、與上面那條註解)、
3 行只靠 `OPENCODE_`。少了任何一半,這個檔在收緊後的 gate 下會紅。

**(c) allowlist 現況**(供 D1 決定要不要再收):照 §6.1 的清單即可,以下是實測數字 ——
`README.md` 與 `docs/security.md` 改完之後 `opencode` 出現 **0 次**(留在 allowlist 無害);
仍會用到 allowlist 的 `.md` 只有 `README_DEV.md`(歷史量測 row)、`docs/basic-usage.md:47`
(同一份歷史資料)、`docs/troubleshooting.md`(升級段)與 `AGENTS.md`(D1 自己)。
`docs/setup.md`、`docs/compaction-rules.md`、`docs/mcp-tools.md` 現在都是 0 次,**不需要**
留在 allowlist。

**(d) `.md` 的「教使用者用」判準新增形狀**(§6.1 已寫,這裡給我這邊的實況):升級段的兩行
`python3 opencode_migrate.py …` **同時**滿足兩種豁免 —— 行內有 `a1682d5`、所在 `###` 段落
標題也含 `a1682d5`(`### 從舊世代前端升級:用 \`a1682d5\` 解除舊的設定接管`)。第三處
(`cd <原安裝路徑> && python3 …`)不是行首形狀,但也刻意帶了 `a1682d5` 行內註解。所以 D1
不論用「同一 logical line」還是「段落標題」實作都會放行,而其他文件一行都沒有這個形狀。
提醒:段落標題若用 `_doc_logical_lines` 之外的樸素 `#` 追蹤,fence 內的 bash 註解
(`# (a) 原安裝路徑就是這份 checkout`)會被誤當成標題 —— 那條路仍由行內 `a1682d5` 兜住。

**(e) docs gate 收緊之後的實測**:我用**獨立複刻的 regex**(不 import gate,也不 import
tests)照「`_DOC_CORE_VARIABLES` / `_DOC_CORE_SECTIONS` 清空 + `_DOC_ALLOWED_TOKENS` 不變 +
`_DOC_REMOVAL_WORDS` / `unset` 略過 + `_FORBIDDEN_DOC_PATTERNS`」的判準掃
`README.md` / `README_DEV.md` / `docs/*.md`,**零 offender**(唯一還會出現的 `AICODE_*`
名字都在含「已刪除 / 以前」的句子裡,或是 `CODETRAIL_REPO` / `AICODE_ROOT` / 四個協定標記)。
`_DOC_ALLOWED_TOKENS` 不必動。這只是複刻,真正的判定仍以 D1 的 gate 在 smoke 裡的結果為準。

**(f) `AGENTS.md` §6.3**(D1 的檔,我這邊的對應事實):README_DEV 的
`MANAGED_COMPACTION_KEYS` bullet 已刪、維護命令索引的兩行遷移命令已刪、
`AICODE_TEST_JOBS=1 …` 已換成 `--jobs 1`、測試指南的 `test_opencode_migrate.py` 已刪、
eval 段的 `LLAMA_BIN=…` 前綴已換成 `--llama-bin`。

### C2(`scripts/set_config.py`)

文件已經照 I-7 寫成事實,請確認落檔一致(不一致就是文件說謊):

* `--llama-bin <路徑>`:README §1.5 / §3.1 說它「會轉成絕對路徑寫進 `deployment.json` 的
  `llama_bin`」,預設沿用該檔既有值、再退回 `~/llama.cpp/build/bin/llama-server`。
* `--models-dir <目錄>`:README §2 / §3.1 說它是**唯一**指定掃描目錄的方式(預設 `~/models`),
  沒有環境變數 fallback。
* 產生的 `~/start.sh`:README §3.1 的產物表與 §4.0 都明寫「**不 export、不 unset 任何變數**」,
  只把子命令與旗標原樣轉給 `launch_servers.py` / `stop_servers.py` / `check_status.py`。
  §3.2 新增的旗標表(`--keep-on-failure`、`--health-timeout N`、`stop --timeout N`、
  `status --expected N`、loader 全組)也預設 `~/start.sh` 會把它們透過去。
* `deployment.json` 現在多寫 `llama_bin` 與四個 `services.<role>.gpu`(README §3.1 產物表、
  §4.1 範例、`docs/deployment-profiles.md` 的 Local override 範例都這樣寫)。
* `set_config.py:2306` 那句指向遷移工具的使用者可見字串:**C2 已改成**「舊世代前端留下的
  設定看 docs/troubleshooting.md 的升級段。」(現行 `:2316`),與我寫的那一段對得上
  (標題含 `a1682d5`,快速分流表也有一列指向它)。無待辦。

### C3(`scripts/launch_servers.py` / `stop_servers.py` / `check_status.py` / `run_tests.py` / `eval/record_semantic_vectors.py`)

README / README_DEV 已經逐字教這些旗標,名字不一致就是文件教錯:

* `launch_servers.py`:`--scope`、`--dry-run`、`--keep-on-failure`、`--health-timeout N`,
  加上 loader 全組(`--main-model` / `--main-gpu` / `--aux-gpu` / `--embed-gpu` /
  `--rerank-gpu` / `--vl-gpu` / `--llama-bin` / `--profile`)。README §4.1 的手動啟動範例
  用的就是 `--main-model` + `--main-gpu` + `--aux-gpu`。
* `stop_servers.py`:`--timeout N`(README 寫預設 **120**)。
* `check_status.py`:`--expected N`(README 寫預設 **4**)、`--strict` 維持原意。
* `run_tests.py`:`--jobs N`,README_DEV 寫「收 1..16、由 runner 自己吃掉、不轉發給 pytest」。
* `eval/record_semantic_vectors.py`:`--llama-bin PATH`,README_DEV 的錄製範例改成
  `python3 eval/record_semantic_vectors.py --record-vectors --llama-bin ~/llama.cpp/build/bin/llama-server`。
  同一段的 `run_code_smoke_eval.py --record-semantic-baseline` 我**把前綴整個拿掉**了 ——
  已確認那支腳本從來沒讀過 `LLAMA_BIN`、也不 spawn 錄製器,舊文件的前綴是多餘的。

### B / S

* B:`README_DEV.md:238` 與 `:327-342` 的歷史量測段落照 `inventory.md` B.5 **保留**;
  新產出的欄位名(`catalog_effective_chars`)沒有出現在使用者文件裡,不需要文件同步。
* S:`docs/basic-usage.md` 新增 `## 7. 切換與接續對話`,README §5.2 有一段導引連到它
  (`docs/basic-usage.md#7-切換與接續對話`)。裡面逐字引用的行為(選單 50 / `/sessions` 20、
  「畫面 N 則、模型歷史 M 則、壓縮 K 次」、pending 的字樣、壓縮標記、busy 拒絕、失敗零改動、
  `/thinking` 只管畫面、CLI `sessions` 的 tab 輸出)都對照 `handoff-S.md` 與落檔程式碼核過;
  這些字串再改就要一起改文件。

---

## 4. 未完成 / 疑點

1. **未執行任何 pytest**(政策要求);本輪新寫的 checker 邏輯只由
   `python3 scripts/check_readme_consistency.py`(綠)與 `python3 scripts/check_eval_consistency.py`
   (綠)實跑覆蓋,合成反例沒有跑過 —— 見 §2 的誠實註記。
2. **文件寫的 argv 已與 C2 / C3 的落檔逐條核對過**(它們在我收尾時已進工作樹;純靜態
   `grep`,沒有執行任何腳本):`launch_servers.py` 的 `--scope` / `--dry-run` /
   `--health-timeout` / `--keep-on-failure` + loader 全組、`stop_servers.py --timeout`
   (`DEFAULT_STOP_TIMEOUT = 120`)、`check_status.py --expected`(`default=4`)、
   `run_tests.py --jobs`(`MAX_PARALLEL_JOBS = 16`)、
   `eval/record_semantic_vectors.py --llama-bin`(預設讀 `deployment.json` 的 `llama_bin`)、
   `set_config.py --llama-bin`(help 逐字寫「相對路徑會先轉成絕對路徑再寫入」)與
   `--models-dir`(「未指定時用 `~/models`」)全部相符;產生的 `~/start.sh` 以
   `"$@"` 轉發給三個腳本,檔內沒有任何 `export` / `unset`。C2 / C3 的 handoff 落檔後
   若又改名,文件要跟著改。
3. **計畫外的一條 stale pattern**:`python3 opencode_migrate.py` 的行首形狀(逐檔例外
   `docs/troubleshooting.md`)。§6.1 只列了 4 條新形狀,這是第 5 條。加它的兩個理由:
   (a) 那支工具在這一代確實不存在,`_STALE_DOC_PATTERNS` 的定義就是「文件不得教已刪的檔案 /
   旗標 / 腳本」;(b) `scripts/opencode_[a-z_]+\.py` 那一行原本只寫 `scripts/` 底下的舊腳本,
   而根目錄那支才是使用者真的可能照抄的。**若 Astra 認為超出計畫**,把這一條與
   `_STALE_DOC_EXEMPT_SOURCES` 裡對應的那一項一起刪掉即可,其餘不受影響(D1 的 gate 仍然
   會擋住同一件事)。
4. **`docs/troubleshooting.md` 的升級段沒有在任何機器上實跑過**:`a1682d5` 沒有 checkout、
   `git worktree` 沒有建、`opencode_migrate.py` 一次都沒有執行、`~/.config/opencode` 與
   `~/.config/codetrail/compaction.json` **零讀寫**。段落裡的狀態檔 schema
   (`config.path_hash` / `real_path_hash`、`managed.<key>.value` / `.prior.present` / `.prior.value`、
   `digest` 涵蓋 `prior`)、四個受管鍵名(`auto` / `tail_turns` / `preserve_recent_tokens` / `prune`)
   與「工具只認執行它的那個 checkout 的 `PLUGIN_DIR`」都是**從 `git show HEAD:opencode_migrate.py`
   讀出來的**,不是回憶或推測。
5. `docs/security.md:48-50` 寫「六個必須人工核准」,而 `client_policy.ASK_TOOLS` 是七個
   (`import_external_file` 是第七個)。**這是 `a1682d5` 就存在的偏差,不是本輪造成的**,
   也不在本任務範圍(改它會動到與 OpenCode / 環境變數無關的內容),所以**沒有改**。
   `check_readme_consistency` 的第 7 條只驗 README(README 那一份是七個、綠)。要修的話
   是另一件事,建議獨立處理。
6. 本輪跑過的**非 pytest** 命令(逐條揭露,對照編排者的串流核對):
   `python3 scripts/check_readme_consistency.py` ×3、`python3 scripts/check_eval_consistency.py` ×3、
   `python3 -m compileall -q scripts/check_readme_consistency.py` ×2、
   `python3 -c "import json; json.load(...)"` ×1(驗我改過的 `eval/spec_questions.json` 仍是合法 JSON)、
   以及 6 個 `python3 - <<EOF` heredoc。**那 6 個 heredoc 只 import `re` / `pathlib`,對文字檔
   跑 regex(等同 grep):**掃 docs 有沒有殘留的環境變數名、複刻 OpenCode gate 與 docs gate 的
   regex 看我的文件會不會被抓、核對 `check_readme_consistency.py` 每一行(含註解)是否落在
   形狀豁免內、以及 python3 命令契約。
   **沒有 import 任何 repo runtime 模組、沒有用合成資料呼叫 repo 的函式或 widget**
   (`execution-notes.md` 第 18 行禁止的那種探查,本輪零次)。
