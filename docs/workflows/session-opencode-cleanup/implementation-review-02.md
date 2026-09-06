本輪：流程第 5 步，實作靜態審核第 2 輪。審核模型 **gpt-6-astra / effort max**。

審核 HEAD：`100b4ec69b913dfe7bb93338a504f5b55bc39d41`；動工前程式碼：`a1682d5`。產品修改仍未 commit。固定快照為 `/tmp/codetrail-session-opencode-cleanup-20260906/review-02-tree.json`，完整產品 diff SHA256：`aafc0cb70bf75a593724e477d3a6e4a37e06e4925786ad93e2d4123d22285cd6`。

已讀本輪指示、上輪審核、`fix-01.md`、更新後的 `test-change-audit.md`／`model-log.md`／`execution-notes.md`。相對上輪只變 8 個產品／測試／正式文件路徑：`README_DEV.md`、`codetrail_chat.py`、`docs/troubleshooting.md`、`eval/record_semantic_vectors.py` 及 `tests/test_evals.py`、`tests/test_repo_consistency.py`、`tests/test_server_scripts.py`、`tests/test_smoke_gate.py`。本輪核對這些修復及其消費者；其餘未變路徑沿用上輪結論。

**結果：B-01 至 B-04 靜態解除；B-05 尚未解除。Active Blocker：1。**

| 上輪項目 | 本輪核對及結論 |
|---|---|
| B-01 模型文字 gate | `codetrail_chat.py:87` 已改成現行檔案／argv 的正規化說明，原 offender 已移除；gate 的 `allowed` 沒有放寬。靜態解除，既有 gate 待最終 smoke。 |
| B-02 launcher fixture | `tests/test_server_scripts.py:521` 先在函式層綁 `binary`，class 屬性再取該 closure 名稱，原本的同名遮蔽已消失。四個消費者的斷言保留。靜態解除；未執行 fixture 或四個 node。 |
| B-03 recorder binary | `eval/record_semantic_vectors.py:129–148` 不再查 PATH；明確 argv 檔不存在時 fail-loud，檔案來源失效保持 `unknown` 並指名來源。`:217` 先取得選定 binary 的 build，manifest 使用同一份結果；README_DEV 同步。靜態解除，並有下述新 regression 的正式紅綠證據。 |
| B-04 checker import 例外 | `tests/test_repo_consistency.py:1304` 在形狀例外前先拒絕 executable 依賴；`import`／`from … import` 不再被例外 token 蓋掉，合法 checker 字串仍走後續規則。靜態解除，並有下述正式紅綠證據。 |

**B-05（延續）：新增的核對片段仍以「先 stat、再依路徑讀取」採信 ownership state，沒有保住可信讀取邊界。**

- **本輪位置**：`docs/troubleshooting.md:642–645` 的手動 `stat` 前置檢查、`:686` 的 `state_path.read_text()`，以及 `:704–718` 依核對結果進行還原與刪狀態的後續步驟。
- **具體觸發**：第 0 步的 `stat` 看到正常的 owner／0700 目錄與 owner／0600 檔案後，狀態檔本身或它的父目錄 `~/.config/codetrail` 在 Python 片段讀取前被換成 symlink。`:686` 會跟隨新的路徑；沒有 `O_NOFOLLOW`、dir fd 錨定與對實際開啟檔案的 `fstat`，所以讀到的並非剛才檢查的來源。如果替換來源中的 state 自身 digest、config hash 與 plugin hash 一致，四行仍可全部印出 `True`，文件便允許讀者繼續把這份 state 的 `prior` 寫回及刪除狀態檔。這些未加密的自洽 hash 不能取代來源安全檢查。
- **讀後還原也未綁定**：片段只印出四個布林值，隨後結束。第 2 步仍要求讀者從狀態檔拿 `prior`；文件沒有把後續使用的內容綁定到剛驗過的那份資料，也沒有在來源改變時要求停止。一次核對成功不能授權稍後重新開啟的另一份 state。
- **對照仍適用的安全要求**：上輪 B-05 要求可信 state 才能還原，且不能弱化 owner-only／ownership 邊界。`a1682d5` 的 `_open_state_dir`（`:531` 起）以 `O_DIRECTORY | O_NOFOLLOW` 開父目錄並 `fstat`；`inspect_state`（`:630` 起）以該 dir fd、`O_NOFOLLOW` 開最終檔並對該 fd 驗 owner／mode，再讀取及驗 digest。本輪文件聲稱做的是同一組驗證，但把安全開檔換成兩次獨立的 path lookup。這正是仍適用 AGENTS §2 禁止的 check-then-use 退化，也未完成最終計畫 §6.2「無法確認時零寫入」的要求。
- **最小修復與可驗收結果**：讓文件中可操作的還原流程保住同一次可信讀取，且後續只使用該次驗證的資料；來源或目標在處理途中改變時停止，不還原、不刪 state。若完全手動路徑無法提供這項保障，可將該路徑改為保留設定及 state、導向路徑一的固定舊版原安裝工具，移除這段被當作還原許可的 path-based 片段及其後續手動寫入指示。驗收時，上述 symlink 替換與讀後換 state 情境都不能取得還原／刪除許可。維持文件修復與靜態核對，不執行片段、不碰真實設定，也不要求恢復已刪的 runtime 遷移整合。

本輪對 B-05 新增的 digest 算法、config 雙 hash、JSON 數字／boolean 比較及 `section_present` 說明亦已對照舊版原始碼；上述 Blocker 集中在可信讀取與後續採用邊界。沒有為這些已對齊的細節另列問題。

正式測試證據只核對、不重跑：

| 新 regression | Fable 原始 runner 結果 | 目前原文與紅燈快照 |
|---|---|---|
| `test_the_checker_shape_exemption_never_covers_an_executable_import` | 紅：collected 1／EXIT=1，失敗為 `assert []`；綠：1 passed／EXIT=0 | SHA256 `38787d7b4cd2d60caa6a75735cb7af7d5564618248a4daca52f3e5ddd33853ac`，相同 |
| `test_the_recorder_never_substitutes_a_path_binary_for_the_chosen_one` | 紅：collected 1／EXIT=1，錯取 `version: 4242 (badc0de)`；綠：1 passed／EXIT=0 | SHA256 `c8d8ef05cedaac35f35919ca79e3a6c25998b2d0f99a33979ab866d389d2f5ea`，相同 |

上述來源相等由本輪只讀 AST 擷取與原文字串雜湊核對，包含 decorator、斷言及函式內註解；兩條均帶 `pytest.mark.smoke`，且已分別登記在正確的 `SAFETY_MODULES` 鍵。編排者的 `manifest-audit-fix01.json` 為 31 檔、554 nodes，missing／unmarked／duplicates 皆 0；這是靜態資料，不能稱為 smoke 通過。

流程紀錄維持真實：已知額外探查共 **5 次（S 的 4 次 repo runtime 探查，加 Fable 的 1 次 toy class 探查）**。`fix-01.md` §4 的「全部是允許的命令」已由 `execution-notes.md` 明確更正；本輪不採它作為 fixture 驗收證據，也不以原作者交接概括語句宣稱全程合規。沿用上輪處理，已揭露的歷史偏差不算成無法修復的永久 Blocker。

**本輪未執行任何測試、collect、runtime／toy 探查、服務或遷移命令；只新增本審核文件，未修改產品碼／測試／AGENTS，未 commit/push。** 整包 smoke／full 仍各 0 次。B-05 修復並靜態收斂至 0 後，才由編排者執行預定的唯一一次 smoke；目前不能回報整體驗收完成。

**Static Active Blocker：1（B-05）。**
