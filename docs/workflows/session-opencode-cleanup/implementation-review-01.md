本輪：流程第 5 步，實作靜態審核第 1 輪。審核模型 **gpt-6-astra / effort max**。

審核 HEAD：`f8f709d2f38a6f3e41372eb02ec759db8c219cdc`；動工前程式碼：`a1682d5`。HEAD 只有已提交的交接資料；本輪產品修改仍在工作樹。依據 `plan-final.md`、目前 AGENTS.md、全部 S/B/C1/C2/C3/D2/D1 handoff、測試變更清單與執行偏差紀錄，對照實際 diff 及消費者。

審核快照為 `/tmp/codetrail-session-opencode-cleanup-20260906/review-01-tree.json`；其產品 diff SHA256 為 `fa67cb32c76422e267094a702ac58ec22f59d8774edb0555c491cb95544ca260`。產品／測試／正式文件共 **58 個路徑，3892 行新增、5077 行刪除**，沒有未追蹤產品檔。

**本輪未執行任何測試、collect、runtime import／函式探查或服務命令。** 以下兩項測試失敗是依原始碼確定的執行結果，不是聲稱已跑 smoke。現有兩條 session regression 的各一次紅／綠證據已核對，但不代表整包通過；唯一整包 smoke 仍待修復與靜態收斂後由編排者執行。

**B-01：收緊後的模型文字 gate 仍有確定的 offender。**

- 位置：`tests/test_repo_consistency.py:1769` 新的 `allowed`，以及 `codetrail_chat.py:87` 的 `_cli_model` docstring。
- 觸發：`test_model_facing_text_never_teaches_a_removed_environment_knob` 遍歷字串常數時，docstring 首句仍為「`--model` 走跟 `aicode -m` / `AICODE_MODEL` 同一套正規化」。該 token 已不在新 allowlist，該處前後 60 字也沒有 gate 接受的移除說明，必然進入 `offenders`。後段另一次提及是否符合移除說明，不會消掉首句的命中。
- 違反：最終計畫 §6.1 的模型文字 gate 收緊及 §7(c) 靜態檢查通過。這是本次改變 gate 與刪除環境入口後留下的整合失敗，並非對基準未改行為另開問題；D1 handoff 已如實列為待修。
- 最小修復及驗收：由修復 owner 把這段 docstring 改成現在的檔案／argv 正規化語意，保持 gate 的新限制。上述 node 在最終 smoke 不再因這個字串失敗；不得加回 `AICODE_MODEL` 例外來結案。

**B-02：新的 launcher fixture 在建立 profile 時就會 `NameError`。**

- 位置：`tests/test_server_scripts.py:518` 的 `_fake_profile(llama_bin=...)`，尤其 `:520` 的 class body：`llama_bin = str(llama_bin)`。
- 觸發：class body 對 `llama_bin` 的賦值把它定義成 class namespace 的名稱，右側同名查找不會捕捉外層函式參數；此模組也沒有同名 global。因此 `_patch_launch_scaffolding()` 呼叫 `_fake_profile(binary)` 時便會拋出 `NameError: name 'llama_bin' is not defined`，還沒走到 launcher。受影響的是新增的 smoke node `test_the_pane_runs_the_exec_choke_point_with_the_loader_argv`，以及既有的 `test_launch_registers_session_before_start_role_failure`、`test_launch_rolls_back_on_keyboard_interrupt`、`test_ready_message_uses_absolute_status_path`。
- 違反：最終計畫 §5.2 要求 pane argv 契約可執行、§5.3 要求既有替身配合新介面，及 §5.4／AGENTS §1.2 不得留下新失敗。
- 最小修復及驗收：避免在 class body 用同名左值遮蔽外層輸入，保留 fixture 對指定 binary 的傳遞。四個消費者均能建立替身並進入原有斷言；尤其上述 pane smoke node 不得再停在 setup，也不得刪除、skip 或放寬它的契約斷言。

**B-03：recorder 會把失效的指定 binary 無聲換成 PATH 上另一顆，寫出錯誤的 build 出處。**

- 位置：`eval/record_semantic_vectors.py:128–137` 新的 `_llama_build(llama_bin)` 選擇流程；其結果在 `:262` 放入 `model.llama_cpp`，由 `:305–306` 寫入 artifact／manifest。對照 `README_DEV.md:299` 的新指引。
- 觸發：使用者給 `--llama-bin /opt/intended/llama-server`，該檔已移除或路徑打錯，而 PATH 上還有另一版本的 `llama-server`。`:134` 因指定檔不存在而改跑 `shutil.which()` 找到的 binary，若它有 `version:` 就回傳非 `unknown` revision。`deployment.json` 已指定但失效時也走同一路徑。錄製者要求的 binary 與 manifest 宣稱的 build 因而不同，且沒有 drift／fallback 提示。
- 本次差異：基準程式在非空的明確來源路徑不存在時回傳 `unknown`；只有未指定才考慮 PATH。本次把「來源缺席」和「已選來源失效」合併，新增了無聲替換。這裡不把基準已有的其他錄製限制列為 Blocker。
- 違反：最終計畫 I-7 明定 `--llama-bin PATH`、預設 `load_effective_profile().llama_bin`，並維持 unknown revision 的拒絕邊界；錯誤 binary 的非 unknown revision 會繞過這項資料出處契約。
- 最小修復及驗收：一旦 argv／檔案已選出非空 binary，無法使用它就明確失敗或保持 `unknown`，不得悄悄替換為 PATH 上另一顆。以離線替身守住「指定路徑不存在、PATH 有不同版本」這個無聲失敗情境，明確來源可用時仍記錄該來源的 revision；不得使用真模型重錄來驗證。

**B-04：新增的 checker 形狀例外也放行真正的 runtime import，削弱了依賴 gate。**

- 位置：`tests/test_repo_consistency.py:1242` 新增 `opencode_migrate` 例外；`:1295–1298` 在檢查依賴形狀之前，只要同一行符合例外就直接 `continue`。
- 靜態反例：若來源路徑是 `scripts/check_readme_consistency.py`，內容只有 `import opencode_migrate`，便符合新增例外而回傳空 offender 集合。這不是反向檢查用的字串 pattern；它是實際 import。基準的 checker 例外只有 `opencode-ai|npm`，同一行原本會被 `_OPENCODE_DEPENDENCY_SHAPES` 擋下。本輪沒有在產品檔放入此反例，也沒有執行 gate。
- 違反：目前 AGENTS §2 的三條靜態 gate 規範只准保留「反向檢查器的 pattern」，不得 weaken 安全檢查點；最終計畫 §7(b/c) 要求 runtime 依賴移除且反例仍被拒絕。§6.1 所列的 pattern 必須用來豁免反向檢查字串，不能把同名 executable import 也豁免。
- 最小修復及驗收：讓實際 import／可執行依賴與合法的反向檢查字串分開判定。至少上述 checker 路徑的 `import opencode_migrate` 與 `from opencode_migrate import ...` 都必須產生 offender，而現行 checker 的合法 regex／提示字串仍可通過；不能用移出掃描來源或整檔豁免修復。

**B-05：新的完全手動升級路徑在未驗證 ownership 狀態可信度前，就指示把 `prior` 寫回設定。**

- 位置：`docs/troubleshooting.md:634–651` 新增的路徑二，尤其第 2 步 `:639–644` 及第 4 步 `:646–648`。
- 觸發：狀態檔仍存在，config 的兩個 hash 與 `managed.<key>.value` 都吻合，但 `managed.<key>.prior` 已被改動而 digest 失效。文件列出的三條必要條件仍全部成立，讀者會把被改過的 `prior.value` 寫回，最後刪掉狀態檔。另一個同類情境是狀態檔為 symlink／權限不可信；文件同樣沒有要求先停下。第 5 步只要求「不要手改」，並說明舊工具會拒絕不符的 digest，沒有要求完全手動路徑先驗證已存在的檔案。
- 對照安全邊界：`a1682d5` 的 `opencode_migrate.py:620–666` 先做安全開檔、owner／mode 與 state digest 驗證，`:1498–1516` 對存在但不可信的 state fail-loud、零寫入。移除退休模組已獲授權；把不可信的還原資料當成 ownership 證據，並未因此獲得授權。這是本次新增的可操作手動寫入指引造成的退化，不是要求保留遷移模組。
- 違反：仍適用的 owner-only／ownership 安全要求，以及最終計畫 §6.2「無法確認時零寫入、不得改用手動路徑硬刪」的邊界。§6.2 第 3 點的三項值比對不能取代 state 本身的信任前提。
- 最小修復及驗收：在任何手動還原或刪狀態之前，明定可信 state 的前置條件：安全來源／owner-only、schema 與涵蓋 `prior` 的 digest 有效、綁定目標 config，並確認並非另一份仍存在的安裝接管。文件應給可核對的驗證方式；若完全手動路徑無法可靠完成，就保留原值與 state，導向已固定版本的原安裝工具，不指示猜測還原。上述 digest 失效／不可信 state／foreign owner 情境依文件均應零寫入；本次修復只需靜態核對文件與舊版驗證語意，不得碰真實設定或新增 runtime 探查。

本輪已核對 session 同一次受信讀取分出原始 transcript 與 compacted model history、重複 call id 按宣告群組配對、busy／失敗路徑、pane 內最終環境清理、profile／registry／argv 消費者、set_config 交易邊界、共用 durable 檔名與 live／歷史 catalog 欄位相容。沒有因這些未發現新增違約的路徑另列建議。編排者的 AST manifest 紀錄為 31 檔、552 個 exact node，missing 0、unmarked 0；這項靜態結果不能替代測試通過。

流程偏差保留：`execution-notes.md` 記錄 S 在正式紅／綠之外執行的 **4 次未獲授權 runtime 探查**。其中一次 `NoActiveAppError` 不是正式 regression 紅燈，其餘成功也不計為允許的驗證。紀錄已揭露且後續指令已收緊；不能靠修改程式碼抹除歷史，因此本輪不把它算成一個無法驗收的永久 Blocker，也不宣稱全程合規。

後續可驗收條件：Fable 修復上述 5 項並如實更新受影響測試的變更理由；ASTRA 再對修復 diff 作靜態核對至 Blocker 0，之後才由編排者執行預定的唯一一次 smoke。沒有 suite 基線，不以 cache 當基線，任何失敗或 `0 collected` 均不能回報完成；full 仍須使用者明示的 reviewer 角色。本輪沒有程式碼修改、commit 或 push。

**Active Blocker：5。**
