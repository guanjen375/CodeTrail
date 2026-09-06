# fix-01(流程第 5 步):Astra 第 1 輪 Blocker 修復

- 修復者身分由編排者記入 `model-log.md`;本檔不自述。審核對象 `implementation-review-01.md`
  (審核 HEAD `f8f709d`);本輪工作樹基於 HEAD `e6a8a62`(只有交接資料,產品改動仍在工作樹)。
- 沒有 commit / push / stash / checkout / reset;沒有改初稿、plan-review、
  implementation-review-01、別人的 handoff、model-log、execution-notes、test-change-audit。
- **本輪沒有跑 smoke / full / collect-only / `--lf`,沒有單跑既有測試**;跑過的測試只有
  §1.3 要求的兩條**新** regression 各一次紅、一次綠(§3)。沒有 import repo runtime 後用合成
  資料呼叫函式 / widget:靜態複刻掃描只 `import ast / re / os / subprocess(git ls-files)`
  (§4)。`Tests: smoke only — reviewer owns full execution.`(交付前唯一一次 smoke 由編排者執行,
  本檔不宣稱它通過。)

## 1. 逐項:Blocker → 修法 → 實際驗證及限制

### B-01 `codetrail_chat.py:87` 的 docstring 仍教 `AICODE_MODEL`

- **修法**:重寫 `_cli_model` 的 docstring,不再出現那個名字;改成現在成立的敘述 ——
  `--model`(只掛在 headless `run` 上)與 `deployment.json` 的 `services.main.model` 走同一個
  `model_resolution.normalize_main_model`;「wrapper 與客戶端各自解析」那段標成「以前」。
  **沒有**加回任何 `allowed` 例外,gate 的收緊原樣保留。
- **驗證**:stdlib 複刻 `test_model_facing_text_never_teaches_a_removed_environment_knob` 的判準
  (同一個 pattern / `allowed` / 上下文詞,掃非 tests 的全部 `.py` 字串常數)→ **0 offender**。
  `compileall` 過。**限制**:那條既有 gate 沒有單跑(政策),由交付前的 smoke 判。
- 沒有新測試:既有 gate 已覆蓋,不為文案同步加儀式性測試。

### B-02 `tests/test_server_scripts.py::_fake_profile` 在 class body 自我遮蔽 → `NameError`

- **修法**:先在函式層綁 `binary = str(llama_bin)`,class body 寫 `llama_bin = binary`。
  class body 對外層函式的**未在 class 內指派**的名字走 closure 查找,對自己指派過的名字走
  `LOAD_NAME`(class ns → globals → builtins,跳過外層函式),所以原寫法必炸。指定 binary 的
  傳遞保留;四個消費者(`test_the_pane_runs_the_exec_choke_point_with_the_loader_argv`、
  `test_launch_registers_session_before_start_role_failure`、`test_launch_rolls_back_on_keyboard_interrupt`、
  `test_ready_message_uses_absolute_status_path`)的斷言**一字未動**,沒有 skip / 放寬 / 刪除。
- **驗證**:純 Python 語意核對(不是 repo 碼):同形狀的舊寫法 `NameError: name 'llama_bin' is not defined`,
  新寫法回傳的物件 `.llama_bin` 等於傳入值;`compileall` 過。**限制**:那四個 node 沒有單跑
  (它們是既有 / 契約測試,政策禁止途中單跑),由交付前的 smoke 判。
- 沒有新測試:這是 fixture 的 setup 錯誤,契約斷言已存在。

### B-03 `eval/record_semantic_vectors.py::_llama_build` 把失效的指定 binary 無聲換成 PATH 上另一顆

- **修法**(`_llama_build` + `record()`):
  - binary 只有兩個來源:`--llama-bin`,否則 `load_effective_profile().llama_bin`。**PATH 那一層整個
    拿掉**:C1 之後 loader 的 `llama_bin` 永遠非空(缺席也展開成 `DEFAULT_LLAMA_BIN`),「未指定」
    這個狀態已不存在,PATH 只剩「選定的用不了就換一顆」這一種用途,而那正是 Blocker。
  - `--llama-bin` 指到的不是檔 → `RecordError("--llama-bin does not point to a file: …")`
    (使用者打錯,fail-loud);`deployment.json` 那顆在這台主機上不是檔 → 回
    `{"revision": "unknown", "reason": "deployment.json llama_bin is not a file on this host"}`
    並在 stderr 印出路徑(路徑本身不進 manifest:它多半在 HOME 底下,會被既有的「本機絕對路徑」閘擋下)。
  - `record()` 改成**一開始**就呼叫 `_llama_build(llama_bin)` 存成 `llama_cpp`,再連 server / embed;
    manifest 的 `model.llama_cpp` 用那份。原本放在 embed 完之後,`--llama-bin` 打錯會把幾分鐘的 embed 丟掉。
  - `README_DEV.md` 錄製段那三行註解同步(指定的檔不存在會直接失敗、不會改拿 PATH;deployment.json
    那顆不存在才記 unknown 並指名來源)。
- **新 smoke regression**(§1.3,紅→綠證據在 §3):
  `tests/test_evals.py::test_the_recorder_never_substitutes_a_path_binary_for_the_chosen_one`。
  離線替身:tmp 目錄放一支只會 `echo "version: …"` 的假 `llama-server` 前置到 PATH,tmp HOME 的
  `deployment.json` 只釘 `llama_bin`。三段:① argv 指到不存在的路徑 → 不得回 PATH 那顆的版本
  (紅燈就是這一行:`{'revision': 'version: 4242 (badc0de)'}`),且要 fail-loud 指名 `--llama-bin`;
  ② 檔案指到不存在的路徑 → `unknown` + reason 含 `deployment.json`、輸出不含 PATH 那顆的版本;
  ③ argv / 檔案指到可用的假 binary → 記**它的**版本。沒有用真模型、真 server、真 GPU。
- **限制**:`_llama_build` 的 `ProfileError` 分支(profile 讀不到 → unknown + reason)沒有改,
  而且在 `record()` 裡實際到不了(`_resolve_model_identity` 先 `load_effective_profile()`,
  壞 profile 在那裡就丟出);沒有動 `unknown` 的下游消費。

### B-04 checker 的形狀例外放行真正的 `import opencode_migrate`

- **修法**(`tests/test_repo_consistency.py`):把 `_OPENCODE_DEPENDENCY_SHAPES` 拆成兩組 ——
  `_OPENCODE_EXECUTABLE_SHAPES`(`import|from … opencode`、`opencode.json`、`opencode_plugins`、
  `.config/opencode`、`which("opencode`、argv 裡的 `"opencode"`)與 `_OPENCODE_PACKAGING_SHAPES`
  (`npm` / `npx` / `opencode-ai`);`_OPENCODE_DEPENDENCY_SHAPES` 保留為兩組合併,只給 allowlist
  **資料檔**那個分支用。`_opencode_offenders` 的 code 分支改成:**可執行形狀先判、不看例外**
  → 再看逐檔例外 → 再判套件 / 教學形狀。`_OPENCODE_SHAPE_EXEMPTIONS` 的內容(含
  `check_readme_consistency.py` 的 `opencode-ai|npm|opencode_migrate|OPENCODE_`)照 §6.1 不動,
  但從此只可能放行套件 / 教學形狀:`opencode_migrate` 這個字出現在 `import` 裡就是 import。
  沒有移出掃描來源、沒有整檔豁免。
- **新 smoke regression**(§1.3):
  `tests/test_repo_consistency.py::test_the_checker_shape_exemption_never_covers_an_executable_import`
  —— 一條 node 同時釘:checker 路徑的 `import opencode_migrate`、`from opencode_migrate import …`、
  同一行帶著例外字的 `import … # OPENCODE_`、`process_env.py` 路徑的 `from opencode_migrate import OPENCODE_PREFIX`
  都是 offender;checker 現有的五種合法字串(兩條 pattern、提示字串、逐檔例外的 key、
  `npm install -g opencode-ai` 的比對字面值)與**真實的整份 checker** 都是零 offender。
- **驗證**:紅→綠(§3);既有自測 `test_the_opencode_gate_checks_allowlisted_files_for_dependency_shapes`
  的 11 條斷言逐條靜態核對仍成立(`STRIPPED = ("AICODE_", "OPENCODE_")` 不命中任一組;
  `["opencode", "--version"]` / `.config/opencode/opencode.json` 命中可執行組;`opencode-ai>=1.0` 在
  非 allowlist 檔照樣 offender)。stdlib 複刻整個 OpenCode gate(160 個文字檔,`docs/workflows/**/*.md`
  內容豁免,git-ignored 跳過)→ **0 offender**;複刻在 checker 路徑餵 `import` / `from … import`
  → 兩者皆 offender。**限制**:既有 gate node 與自測沒有單跑。

### B-05 `docs/troubleshooting.md` 完全手動路徑在未驗 state 可信度前就寫回 `prior`

- **修法**(只改文件,不動 runtime、不跑舊版工具、不碰真實設定):路徑二改成「先驗狀態檔可不可信,
  再動設定」,新增第 0 步,四項前置條件逐條對應 `a1682d5` 的 `opencode_migrate.py`(靜態讀
  `git show a1682d5:opencode_migrate.py`,行號為該版):
  1. **來源安全**(`_open_state_dir` / `inspect_state`,`:531-669`):目錄是真目錄、屬於你、`0700`;
     狀態檔是常規檔、非 symlink、屬於你、`0600`;給 `stat -c '%F %A %U'` 的核對方式。
  2. **形狀與 digest**(`validate_state` `:497-528`、`_state_digest` `:449-466`):schema 1、mode 三選一、
     `managed` 只含四鍵、每鍵有 `value` 與 `prior.present`、digest 重算相符;明講 digest 涵蓋 `prior`。
  3. **綁定這份設定**(`config_identity` `:329-349`、`state_matches_config` `:351-369`):
     `path_hash` / `real_path_hash` **兩個都要**相符。
  4. **不是另一份仍存在的安裝接管的**(`_foreign_plugin_owner` `:1574-1608`、
     `_recorded_plugin_is_not_ours` / `_config_names_recorded_plugin` `:1614-1639`):
     `plugin.path_hash` 對 `<原安裝路徑>/opencode_plugins/codetrail-compaction.js`;對不上而路徑還在
     → 別份活著的安裝,到那份用它的工具;對不上也找不到 → 無法確認,零寫入。
  - 給一段**只用標準函式庫**的 `python3 - <<'EOF'` 核對片段,算法逐字取自 `_digest_normalise`(`:423-441`)、
    `digest_managed`(`:442-447`)、`_state_digest`、`config_identity`、`plugin_path_hash`(`:1041-1043`):
    四行任一不是 `True` → 停,設定現值與狀態檔都不動,走路徑一。
  - 第 2 步的相等判準改成對應 `json_equal()` / `owns()`(`:164-193`, `:789-805`):`true` 與 `1` 不同、
    `false` 與 `"false"` 不同、**`1` 與 `1.0` 相同**(原文寫成不同,與舊版工具相反);補上
    `section_present` 的規則(`_restore_native` `:962-970`:區塊是接管時建的才整個刪)。
  - 第 5 步把「工具看到對不上的狀態檔會當成沒有接管」改成事實:`plan_migration`(`:1498-1516`)
    對「狀態檔存在但無法信任」是 `MigrationError` fail-loud、零寫入。
  - 路徑一、標題(含 `a1682d5`)、快速分流表、README / security 指向它的那一句都沒動。
- **驗證**:`python3 scripts/check_readme_consistency.py` **OK**(逐檔 stale pattern 含 troubleshooting
  的兩條例外);stdlib 複刻 OpenCode `.md` 分支(teach regex + `a1682d5` 錨點,logical line / 標題追蹤)
  與 docs gate(`_doc_offenders` 全部規則)對全部使用者 `.md` → **0 offender**;`python3` 命令契約
  → 0 offender。**限制**:片段沒有在任何機器上執行過(不得碰真實設定、不得跑舊版工具),正確性靠與
  舊版原始碼逐字對照;digest 失效 / 不可信 state / foreign owner 三種情境依新文字都停在第 0 步、零寫入。

## 2. 測試與 fixture 變更(完整名字與理由)

| 檔 | 名稱 | 動作 | 行為為什麼該變 |
|---|---|---|---|
| `tests/test_evals.py` | `test_the_recorder_never_substitutes_a_path_binary_for_the_chosen_one` | **新增**,`@pytest.mark.smoke` 單條 decorator(該檔無 module 層 mark) | B-03 的真實 bug:指定 binary 失效時 manifest 的 build 出處靜默變成 PATH 上另一顆;離線替身釘住「不替換、argv fail-loud、檔案 unknown 指名來源、可用時記自己的版本」 |
| `tests/test_evals.py` | `_fake_llama_server(directory, revision)`、`_pin_llama_bin(home, llama_bin)` | **新增 helper**(非 node) | 上面那條的假 binary 與 tmp HOME `deployment.json`;`json` / `os` 已是既有 import,無新 import |
| `tests/test_repo_consistency.py` | `test_the_checker_shape_exemption_never_covers_an_executable_import` | **新增**,`@pytest.mark.smoke` 單條 decorator | B-04:形狀例外不得放行可執行的依賴;同一 node 覆蓋 import / from-import 與合法 checker 字串對照 |
| `tests/test_repo_consistency.py` | `_OPENCODE_EXECUTABLE_SHAPES`、`_OPENCODE_PACKAGING_SHAPES` | **新增模組常數**;`_OPENCODE_DEPENDENCY_SHAPES` 改為兩者合併(只給資料檔分支) | gate 本體的修法(B-04);pattern 內容逐字取自原本那一條,只是拆組 |
| `tests/test_repo_consistency.py` | `_opencode_offenders` | **改邏輯**(code 分支:可執行形狀先判、不看例外) | 同上 |
| `tests/test_server_scripts.py` | `_fake_profile(llama_bin=…)` | **改 fixture**(函式層先綁 `binary`) | B-02:class body 同名遮蔽 → `NameError`;fixture 對指定 binary 的傳遞保留 |
| `tests/test_smoke_gate.py` | `SAFETY_MODULES["test_repo_consistency.py"]`、`["test_evals.py"]` | **資料變更**:各登記 1 個新 node,說明各補一句 | 新 checkpoint 必須進 manifest(漏標是無聲的);測試函式本體零改動 |

沒有刪除、改名、改斷言、skip / xfail 任何既有測試;沒有動 `tests/conftest.py`、任何 import 區。
`test-change-audit.md` 是編排者的檔,未動;本表即本輪的 test-changes。

## 3. 紅綠證據(§1.3;各一次,命令逐字)

**B-04 紅**(gate 未修改;`tests/test_repo_consistency.py` 只多了那條新 node):

```
python3 scripts/run_tests.py tests/test_repo_consistency.py::test_the_checker_shape_exemption_never_covers_an_executable_import
```

exit **1**,`collected 1 item`:

```
tests/test_repo_consistency.py:1856: in test_the_checker_shape_exemption_never_covers_an_executable_import
    assert _opencode_offenders(checker, "import opencode_migrate\n")
E   AssertionError: assert []
E    +  where [] = _opencode_offenders('scripts/check_readme_consistency.py', 'import opencode_migrate\n')
FAILED tests/test_repo_consistency.py::test_the_checker_shape_exemption_never_covers_an_executable_import
============================== 1 failed in 0.07s ===============================
```

**B-03 紅**(`eval/record_semantic_vectors.py` 未修改;`tests/test_evals.py` 只多了那條新 node 與兩個 helper):

```
python3 scripts/run_tests.py tests/test_evals.py::test_the_recorder_never_substitutes_a_path_binary_for_the_chosen_one
```

exit **1**,`collected 1 item`:

```
tests/test_evals.py:1242: in test_the_recorder_never_substitutes_a_path_binary_for_the_chosen_one
    assert build is None or build.get("revision") == "unknown", build
E   AssertionError: {'revision': 'version: 4242 (badc0de)'}
E   assert ({'revision': 'version: 4242 (badc0de)'} is None or 'version: 4242 (badc0de)' == 'unknown'
FAILED tests/test_evals.py::test_the_recorder_never_substitutes_a_path_binary_for_the_chosen_one
============================== 1 failed in 0.38s ===============================
```

兩條都紅在**真正的 bug 斷言**(例外放行了 import;PATH 那顆的版本被寫進 build 出處),不是 setup / 替身錯誤;
都是 `collected 1 item`,不是 exit code 5。

**B-04 綠**(修完 gate 後,同一條、斷言未動):同一命令,exit **0**:

```
tests/test_repo_consistency.py .                                         [100%]
============================== 1 passed in 0.06s ===============================
```

**B-03 綠**(修完 recorder 後,同一條、斷言未動):同一命令,exit **0**:

```
tests/test_evals.py .                                                    [100%]
============================== 1 passed in 0.32s ===============================
```

紅與綠之間兩條測試的原始碼零改動(斷言、替身、fixture 都沒有為了轉綠而放寬)。

## 4. 靜態驗證(全部是允許的命令;沒有 pytest 之外的 repo runtime 呼叫)

| 命令 / 判準 | 結果 |
|---|---|
| `python3 -m compileall -q codetrail_chat.py eval/record_semantic_vectors.py tests/test_server_scripts.py tests/test_repo_consistency.py tests/test_evals.py tests/test_smoke_gate.py` | exit 0 |
| `python3 scripts/check_readme_consistency.py` | OK(exit 0) |
| `python3 scripts/check_eval_consistency.py` | OK(exit 0) |
| 複刻:模型可見字串 gate(全部非 tests `.py` 的字串常數;同 pattern / `allowed` / 上下文詞) | 0 offender(B-01) |
| 複刻:OpenCode gate 三個分支 + 新的可執行 / 套件分組(160 個文字檔;`docs/workflows/**/*.md` 內容豁免;git-ignored 跳過) | 0 offender;checker 路徑餵 `import` / `from … import` 皆 offender(B-04) |
| 複刻:docs gate `_doc_offenders` 全部規則(全部使用者 `.md`) | 0 offender(B-05) |
| 複刻:`python3` 命令契約(`*.md` + `docs/**/*.md`) | 0 offender |
| 複刻:`SAFETY_MODULES` ↔ 各測試檔的 AST(module 層 `pytestmark` + 單條 decorator) | 31 檔、**554** node(552 + 2),missing 0、unmarked 0、重複 0;兩條新 node 各登記在對的檔名鍵下 |
| 純 Python 語意(非 repo 碼):B-02 舊 / 新 class body 形狀 | 舊形狀 `NameError`;新形狀綁得到參數 |

複刻腳本以 heredoc 執行、只 `import ast / re / os / subprocess(一次 `git ls-files` 取 ignored 集合)/ pathlib`,
不 import 本 repo 任何模組、不呼叫本 repo 任何函式;gate 判準是重寫一份,不是 import `tests/` 再呼叫。
另外只讀過 `git show a1682d5:opencode_migrate.py` / `a1682d5:eval/record_semantic_vectors.py`(靜態)。
沒有起任何服務、沒有碰 tmux / GPU / `~/.config/*` / 真實 session。

## 5. 介面偏差(本輪新增,下游以本節為準)

| 位置 | 偏差 | 理由 |
|---|---|---|
| `tests/test_repo_consistency.py` | 新常數 `_OPENCODE_EXECUTABLE_SHAPES` / `_OPENCODE_PACKAGING_SHAPES`;`_OPENCODE_DEPENDENCY_SHAPES` 語意縮成「資料檔分支用的合併組」 | B-04 要求可執行依賴與反向檢查字串分開判定;§6.1 的例外 regex 內容不變 |
| `eval/record_semantic_vectors.py::_llama_build` | 來源鏈 `--llama-bin` > `deployment.json`,**無 PATH**;argv 失效 `RecordError`,檔案失效 `unknown` + 指名來源 + stderr 路徑;`record()` 在 embed 前先算 | B-03;`plan-final.md` I-7 本來就只列這兩個來源,PATH 是 C3 加的第三層 |
| `docs/troubleshooting.md` 路徑二 | 新增第 0 步(可信 state 前置條件 + 核對片段);第 2 步相等判準改成 `json_equal` 語意;第 5 步的舊工具行為改成 fail-loud | B-05;與 `a1682d5` 原始碼逐字對照 |

## 6. 剩餘問題 / 分歧

- 對五項 Blocker **沒有分歧**;沒有登記 `deferred.md`。
- **修 B-05 時順手改正的一處**(在 Astra 指出的同一段內、屬於同一個「手動路徑不得比舊版工具寬鬆」的問題):
  原文「`1` 與 `1.0` 都算不同」與舊版 `json_equal` 相反(JSON 只有一種數字,`1.0` 與 `1` 相等)。照原文做,
  使用者會把自己合法寫成 `1.0` 的值當成「不是我們寫的」而保留 —— 方向是安全的(不寫入),但敘述是錯的。
  已改成對應舊版語意,列在這裡供 Astra 核對是否越界。
- B-03 的 PATH 層是**整個拿掉**而不是「只在未指定時才看」:C1 之後不存在「未指定」的狀態
  (loader 的 `llama_bin` 永遠非空),留著 PATH 就只剩無聲替換這一種用途。若 Astra 認為應保留
  「profile 讀不到時退 PATH」,那是可逆的一行,但 `record()` 實際到不了那個分支(見 B-03 限制)。
- `_OPENCODE_SHAPE_EXEMPTIONS["scripts/check_readme_consistency.py"]` 仍是 §6.1 的
  `opencode-ai|npm|opencode_migrate|OPENCODE_`;修完之後後兩個 token 只可能對套件 / 教學形狀生效,
  對現行 checker 全檔是零用途(那幾行本來就不命中任何依賴形狀)。刻意不收,照計畫。
- 交付前唯一一次 `python3 scripts/run_tests.py -m smoke` 由編排者執行;本檔沒有任何一條 node 宣稱通過
  smoke,只宣稱 §3 的兩次單跑與 §4 的靜態結果。
