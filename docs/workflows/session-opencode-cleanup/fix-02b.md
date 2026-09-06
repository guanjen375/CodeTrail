# fix-02b(流程第 5 步延伸):B-05 文件消費者同步 —— `AGENTS.md` 相鄰措辭

- 修復者身分由編排者記入 `model-log.md`;本檔不自述。本輪是 `fix-02.md` §6「相鄰措辭」那一項的
  延伸(同一個 B-05 文件消費者同步),不是新功能、不開新問題;本輪開始時的 HEAD 依編排者提供的
  git 狀態仍為 `6e2111a`(產品改動仍在工作樹)。
- 本輪工具只有 Read / Glob / Grep / Write / Edit,沒有 Bash / Agent / MCP。**零測試執行、零 checker
  執行、零 runtime / toy 探查、零 git 操作、零真實設定讀寫**。所有核對都是讀原始碼與文字比對。
- 只改兩個路徑:`AGENTS.md` §2 的一句,與新增本檔。

## 1. 唯一的文字修改

`AGENTS.md` §2「兩份安裝並存的共用檔」那一條的末句(修改前 `:160`,修改後 `:160-162`):

| 修改前 | 修改後 |
|---|---|
| 舊安裝的還原只走 `docs/troubleshooting.md` 的固定舊版 / 手動路徑 | 舊安裝的還原只走 `docs/troubleshooting.md` 升級段的唯一做法:把原安裝路徑固定回 `a1682d5`、跑它自己的工具(沒有完全手動的路徑;工具無法確認就零寫入,設定與狀態檔原樣留著) |

同一條的前半(「main runtime 永不讀、寫、刪 … 與任何舊 plugin 路徑」)、§2 其他各條與
§1 / §3 / §4 一字未動。

## 2. 為什麼這樣改

- fix-02 依 Astra 第 2 輪 B-05 把 `docs/troubleshooting.md` 的完全手動路徑整條拿掉
  (`:646-652`「沒有完全手動的路徑」),只剩「把原安裝路徑固定回 `a1682d5`,跑它自己的工具」
  (`:610`)。`AGENTS.md` 那句的「手動路徑」四字因此指向一條文件已不再提供的路徑;fix-02 §6
  把它列為相鄰措辭交給編排者,因為 `AGENTS.md` 當時不在授權範圍。
- 新句逐字對齊 troubleshooting 的三處:`:610` 唯一做法;`:631-632` 與 `:641-644` 工具無法確認
  就零寫入、設定與狀態檔原樣留著(不硬刪);`:646`「沒有完全手動的路徑」。那一條的約束本體
  (runtime 永不讀、寫、刪那些檔;還原只走 troubleshooting 的指引)不變。
- `README.md:85-88`、`:143` 與 `docs/security.md:136` 都只說「升級段(標題含 `a1682d5`)」與
  「手動解除一次」,「手動」指使用者自己執行固定舊版工具;與新句不衝突,未動。

## 3. 靜態核對(只讀 + 文字比對;**沒有執行**任何 checker 或測試)

| 判準 | 讀碼結果 |
|---|---|
| `tests/test_repo_consistency.py::_opencode_offenders` 的 `.md` 分支(`:1270-1289`;`AGENTS.md` 在 allowlist `:1217`) | 新句沒有遷移命令形狀、沒有 `OPENCODE_` 變數、沒有行首 `opencode` 命令、沒有教裝套件的形狀。`a1682d5` 只是遷移命令出現時的錨點(`:1256-1257`),純提及既不觸發也不需要 |
| `scripts/check_readme_consistency.py::_stale_doc_sources`(`:818-826`) | 只掃 `README.md` 與 `docs/*.md`;`AGENTS.md` 與 `docs/workflows/**` 都不在其中 |
| `test_user_facing_python_commands_use_python3`(含 `docs/**/*.md`) | 兩個改動檔都沒有 `python ` 後接 `.py` / `-m` 的命令形狀 |
| 有沒有測試或 checker 釘住舊句 | 全 repo 搜「手動路徑 / 固定舊版」:`tests/` 與 `scripts/` 零命中;命中只在 `AGENTS.md:160`(本輪改掉)、`docs/troubleshooting.md:662`(未動)與 workflow 交接文件 |

上表是讀碼結論,不是 gate 通過的證據;靜態一致性檢查與唯一一次 smoke 由編排者執行,
本檔不宣稱任何一項通過。

## 4. 測試與未改動事實

- **測試:零新增、零修改、零執行。** 純文案同步,沒有真實 bug regression 或新契約要守(§1.4);
  `SAFETY_MODULES` 未動。`Tests: smoke only — reviewer owns full execution.`(本輪自己零執行。)
- 未改動:runtime、`tests/`、`README.md`、`docs/troubleshooting.md`、`docs/security.md`、其他 docs、
  `plan-final.md`、兩輪審核、`fix-01.md`、`fix-02.md`、`execution-notes.md`、`test-change-audit.md`、
  `model-log.md`、`deferred.md`、所有 handoff。
- 剩餘問題 / 分歧:無。
