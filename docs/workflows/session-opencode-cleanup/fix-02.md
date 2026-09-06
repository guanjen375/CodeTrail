# fix-02(流程第 5 步):Astra 第 2 輪 Blocker 修復

- 修復者身分由編排者記入 `model-log.md`;本檔不自述。審核對象 `implementation-review-02.md`
  (審核 HEAD `100b4ec`);本輪開始時的 HEAD 依編排者提供的 git 狀態為 `6e2111a`
  (只有交接資料的 commit,產品改動仍在工作樹)。
- 本輪工具只有 Read / Glob / Grep / Write / Edit,沒有 Bash / Agent / MCP。**零測試執行、零 checker
  執行、零 compileall、零 runtime / toy 探查、零 git 操作、零真實設定讀寫**(沒有讀
  `~/.config/*`,沒有碰任何服務)。所有核對都是讀原始碼與文字比對。
- 只改兩個路徑:`docs/troubleshooting.md` 的 `a1682d5` 升級段,與新增本檔。runtime / tests /
  AGENTS.md / README / docs 其他檔 / 歷史計畫 / 審核 / 交接 / model-log / execution-notes /
  deferred / test-change-audit 全部未動。
- 上輪唯一未解除的是 B-05;B-01 至 B-04 已由 Astra 靜態解除,本輪不重開、不擴張。

## 1. B-05 → 修法 → 為什麼解除

### 1.1 Astra 第 2 輪指出的退化

新增的路徑二把「狀態檔可不可信」做成兩次獨立的 path lookup:先 `stat`(修改前
`docs/troubleshooting.md:642-645`),再由 Python 片段以路徑 `read_text()`(修改前 `:686`),
四行 `True` 之後允許讀者逐鍵寫回 `prior`、刪 plugin 項、刪狀態檔(修改前 `:704-718`)。
兩次 lookup 之間把狀態檔或 `~/.config/codetrail` 換成 symlink,片段讀到的就不是剛才檢查的
來源;片段印完四個布林就結束,第 2 步再要讀者從狀態檔拿 `prior`,後續使用也沒有綁定到驗過
的那份資料。這是 AGENTS §2 禁止的 check-then-use,而且未達最終計畫 §6.2「無法確認時零寫入」。

### 1.2 採用的修法(Astra 明確接受的最小修法)

完全手動路徑**做不到**「驗過的那份就是用到的那份」,所以整條路徑拿掉,改為保留設定與狀態檔、
導向路徑一 `a1682d5` 的原安裝工具。具體改動(全部在 `docs/troubleshooting.md:591-667`):

| 修改前 | 修改後 |
|---|---|
| `:607` 「下面兩條路都不必走」 | `:607` 「下面的步驟都不必做」(只剩一種做法) |
| `:610` 「路徑一(建議)…再跑它自己的工具」 | `:610` 「**解除只有一種做法:把原安裝路徑固定回 `a1682d5`,跑它自己的工具。**」 |
| `:631-632` 「`--check` 回報…不要改用下面的手動路徑硬刪」 | `:631-644` `--check` 就是演練;工具在寫入前自己確認的四件事(只描述工具行為,**沒有**任何手動對應命令);停下來的兩類處置:「另一份安裝的接管」→ 情況 (c) 到那份 checkout 用它的工具;「無法確認」/ 狀態檔存在但無法信任 → 設定與狀態檔原樣留著、不硬刪 |
| `:634-722` 路徑二整段:第 0 步(來源安全 `stat`、形狀與 digest、綁定、foreign owner 的手動核對 + 標準函式庫 heredoc)、第 1 步刪 plugin 項、第 2 步逐鍵寫回 `prior` / `section_present`、第 3 步、第 4 步刪狀態檔、第 5 步 | **全部刪除**,換成 `:646-652` 「**沒有完全手動的路徑。**」:說明 `a1682d5` 的工具是在同一次開檔裡(目錄 fd 錨定、`O_NOFOLLOW`、對 fd 驗 owner / 權限、從同一個 fd 讀出內容驗 digest)完成驗證與讀取,再用那一次的內容規劃還原;`stat` / `cat` / 編輯器分步做每一步都重新以路徑找檔,中間被換掉沒有任何錯誤訊息;因此本文件不提供手動核對命令、不提供逐鍵寫回 / 刪 plugin 項 / 刪狀態檔的步驟,也叫讀者不要照別處片段自己拼 |
| `:724-725` 「要先演練的話,用合成設定…」(為手動編輯而設的演練) | 刪除;演練改為 `--check`(零寫入、只列出會做什麼),寫在 `:631` 與 `:662` |
| (無) | `:654-662` 「**現在不能做 git 操作時:保留設定、保留狀態檔,之後再解除。**」:`opencode.json` 的 `compaction.*` / `plugin` / `mcp.codetrail` / `permission` 一個字都不動;`~/.config/codetrail/compaction.json` 不刪、不改(唯一能證明原值的東西,digest 涵蓋原值);留著的代價只有本節開頭兩個症狀且只影響舊前端,本版 CodeTrail 不讀這兩份檔;能在原安裝路徑跑固定舊版時回到三選一,先 `--check` 再實際執行 |

沒有把遷移演算法重寫成另一支文件內腳本;沒有恢復被刪的 runtime 遷移整合;沒有新增任何
未經前兩輪審核的工具行為聲稱(見 §4)。

### 1.3 對照 Astra 的驗收情境

- **`stat` 之後、讀取之前把狀態檔或父目錄換成 symlink**:文件已沒有任何「先 stat 再依路徑讀」
  的步驟,也沒有任何依核對結果進行的寫入或刪除;讀者從文件拿不到還原 / 刪除許可。
  唯一會讀狀態檔的是 `a1682d5` 工具的同一次受信讀取(dir fd + `O_NOFOLLOW` + fstat)。
- **一次核對成功後重新開啟另一份 state**:文件不再要求讀者從狀態檔取 `prior`,沒有任何
  「讀後使用」;工具內部的驗證與使用在同一次開檔完成。
- **digest 失效 / 不可信 state / foreign owner**:三種都落在「工具停下來報錯、零寫入」,文件的
  處置一律是保留設定與狀態檔(foreign 另指向情況 (c));沒有任何文字允許手動硬刪。
- **不碰真實設定、不執行片段、不恢復 runtime 遷移整合**:本輪連片段都不存在了;沒有讀寫
  `~/.config/*`;runtime 零改動。

## 2. 保留的固定舊版流程(一字未動的安全界線)

- 段落標題(`:591`,「從舊世代前端升級:用 a1682d5 解除舊的設定接管」,gate 放行
  `python3 opencode_migrate.py` 的錨點)、快速分流表那一列(`:24`)、開頭的症狀說明
  (`:593-602`)、「先找出原安裝路徑」(`:604-608`,只改一個詞)。
- 三選一 (a)(b)(c) 的命令區塊(`:614-629`)逐字保留:(a) 同路徑 `git status --porcelain` 為空 →
  `git checkout --detach a1682d5` → `--check` → 實際執行 → `git checkout -`;(b) 原安裝路徑不在了
  就 `git worktree add <原安裝路徑> a1682d5` 在**同一個路徑**重建、做完 `git worktree remove`;
  (c) 別份仍存在的安裝到那份 checkout 用它自己的工具。
- 「工具只認 `PLUGIN_DIR` = 執行它的那個 checkout,換地方執行只會回報無需變更」(`:611-612`)。
- foreign owner:`--check` 回報另一份安裝的接管 → 不在這裡動手,到那份 checkout(`:641-642`)。
- 無法確認 / 無法信任 → 零寫入、不硬刪、原樣留著(`:642-644`);狀態檔 digest 涵蓋原值、
  改過就對不上、工具會停下、再也證明不了原值(`:657-659`)。
- 「升級注意」(`:664-667`)未動。

## 3. 相對 `plan-final.md` 的安全修訂

- §6.2 第 3 點(完全手動四條:只刪逐字相等的 plugin 項、JSON 嚴格相等才依 `prior` 還原、
  `mcp.codetrail` / `permission` 不動、每鍵處理完才刪狀態檔)**不再以可操作步驟呈現**。
  理由是 Astra 第 2 輪 B-05:手動路徑無法在同一次可信讀取內完成驗證並把後續使用綁定到那份
  資料,而 Astra 在同一項的「最小修復」裡明確接受「改為保留設定及 state、導向路徑一的固定舊版
  原安裝工具,移除 path-based 片段及其後續手動寫入指示」這個 fallback。四條裡的安全意圖
  (不動別人的 plugin 項、不動 `mcp.codetrail` / `permission`、判不出來就留著狀態檔)以
  「原樣留著」的形式保留(`:656-659`)。
- §6.2 第 4 點的「合成設定演練」是為手動編輯而設,隨路徑二一起拿掉;演練改為工具自己的
  `--check`。第 4 點對施工的約束(本次施工不在施工機器執行遷移、不 checkout 舊版、不動外部
  設定)本輪照樣遵守。
- §6.2 第 1、2、5 點(前提、固定舊版三情況與 `a1682d5` 標題要求、新鍵對舊世代 fail-loud)不變。
- 這不是兩方分歧:Astra 已預先接受,**不登記 `deferred.md`**。

## 4. 事實來源與本輪限制

- 本輪無法讀 `a1682d5` 的原始碼(工作樹中 `opencode_migrate.py` 已刪除,且沒有 git 存取)。
  文件保留的工具行為描述全部取自已審核的紀錄:`implementation-review-01.md` B-05
  (`:620-666` 安全開檔、owner / mode 與 state digest 驗證;`:1498-1516` 對存在但不可信的
  state fail-loud、零寫入)、`implementation-review-02.md` B-05(`_open_state_dir` `:531` 以
  `O_DIRECTORY | O_NOFOLLOW` 開父目錄並 `fstat`;`inspect_state` `:630` 以該 dir fd、`O_NOFOLLOW`
  開最終檔並對該 fd 驗 owner / mode,再讀取及驗 digest),以及 `fix-01.md` 中 Astra 已對照過的
  四項前置條件(來源安全、形狀與 digest、綁定這份設定、不是別份安裝接管)。本輪只把這四項改寫成
  「工具自己確認的四件事」,移除所有需要讀者手動執行的對應物;沒有加入任何新的行為聲稱。
- `--check` 「零寫入、只列出會做什麼」與「回報另一份安裝的接管 / 無法確認時零寫入」是
  修改前文件已有、且經兩輪審核的敘述,原樣沿用。

## 5. 靜態核對(只讀 + 文字比對;**沒有執行**任何 checker 或測試)

| 判準 | 讀碼結果 |
|---|---|
| `tests/test_repo_consistency.py::_opencode_offenders` 的 `.md` 分支(`:1256-1289`) | `python3 opencode_migrate.py` 只出現在 `docs/troubleshooting.md:619`、`:620`、`:625`,三行行內都含 `a1682d5`,所在標題 `:591` 也含;全文沒有行首 `opencode ` 命令、`OPENCODE_*`、`npm install -g opencode` |
| `_doc_offenders`(`:1686-1739`) | 全文 `(AICODE\|AI_CODE\|CODETRAIL\|OPENCODE)_` 命中只有 `CODETRAIL_REPO` / `CODETRAIL_ACTION_REQUIRED` 的裸提及(`_DOC_ALLOWED_TOKENS`),且全在本輪未改動的行;新文字沒有 `export` / `Environment=` / `aicode web` 等禁用形狀 |
| `scripts/check_readme_consistency.py::_STALE_DOC_PATTERNS`(`:758-799`) | `~/.config/codetrail/compaction.json` 與 `python3 opencode_migrate.py` 對 `docs/troubleshooting.md` 逐檔豁免;其餘 pattern(`scripts/opencode_*.py`、`export LLAMA_BIN=` / `MODELS_DIR=`、`AICODE_TEST_JOBS=`、`Environment=…`、`web 介面` / `web 模式`、`OPENCODE_*`)全文零命中;它對 troubleshooting 鎖的四句(`#### SEARCH/REPLACE 被拒絕` 等)與 run_command timeout 句都不在改動範圍 |
| `test_user_facing_python_commands_use_python3`(`:38-67`,含 `docs/**/*.md` 與本檔) | 全文沒有 `python ` 後接 `.py` / `-m` 的命令形狀;本檔亦然 |
| README / security 指向 | `README.md:85-88`、`:141-144` 與 `docs/security.md:135-137` 只說「升級段(標題含 `a1682d5`)」與「一次性的手動程序」;標題未動,「手動程序」指使用者自己執行固定舊版工具,仍成立 |
| 段內殘留字樣 | 全文搜尋「路徑一 / 路徑二 / 第 0 步 / 手動路徑 / `stat -c` / `python3 - <<` / 合成設定 / 兩條路」零命中 |

上表是讀碼結論,不是 gate 通過的證據;`python3 scripts/check_readme_consistency.py` 與唯一一次
smoke 由編排者執行,本檔不宣稱任何一項通過。

## 6. 測試與未改動事實

- **測試:零新增、零修改、零執行。** 這是文件修正,沒有真實 bug regression 或新契約要守
  (§1.4);`SAFETY_MODULES` 未動。`Tests: smoke only — reviewer owns full execution.`
  (本輪自己零執行;交付前唯一一次 smoke 由編排者在 Astra 收斂至 Blocker=0 後執行。)
- 未改動:runtime、`tests/`、`AGENTS.md`、`README.md`、`docs/security.md`、其他 docs、
  `plan-final.md`、兩輪審核、`fix-01.md`、`execution-notes.md`、`test-change-audit.md`、
  `model-log.md`、`deferred.md`、所有 handoff。
- 相鄰措辭(**不是** deferred、不是分歧,列出供編排者知悉):`AGENTS.md` §2 「舊安裝的還原只走
  `docs/troubleshooting.md` 的固定舊版 / 手動路徑」中的「手動路徑」四字,現在指向文件已不再提供
  的路徑;那句的約束本體(runtime 永不讀、寫、刪那些檔;還原只走 troubleshooting 的指引)仍然
  成立。AGENTS.md 不在本輪授權範圍,未動。
- 剩餘問題 / 分歧:無。
