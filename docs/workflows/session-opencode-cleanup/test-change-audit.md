# 測試變更的靜態核對與補充

編排者以 `a1682d5` 的原始碼為準，對照目前測試函式（含 decorator 與函式內註解）的原始碼雜湊；沒有 import 測試模組、collect 或執行測試。原始觀察表在本機 `test-changes-W2.json`，最新為 `test-changes-observed.json`。本表已核對至 Fable 修復第 3 輪。

逐條原因表：

| 工作 | 明細 | 核對結果 |
|---|---|---|
| Session | [handoff-S.md](handoff-S.md)、[紅燈](handoff-S-red.md) | 變動 node 名全有記錄，fixture / import 另列 |
| OpenCode 移除與常數 | [handoff-B.md](handoff-B.md) | 含整檔刪除的 63 條測試，變動 node 名全有記錄 |
| 部署 loader | [handoff-C1.md](handoff-C1.md) | 變動 node 名全有記錄，fixture / import 另列 |
| set_config | [handoff-C2.md](handoff-C2.md) | 變動 node 名全有記錄，harness 另列 |
| 啟停腳本／runner | [handoff-C3.md](handoff-C3.md) | 除下列 3 條補充外，變動 node 名全有記錄 |
| Gate／manifest／AGENTS | [handoff-D1.md](handoff-D1.md) | 所有新增、改名、刪除與修改的 node 名均有記錄；SAFETY_MODULES 資料變更另列 |
| 文件與 checker | [handoff-D2.md](handoff-D2.md) | 未改测试檔，checker 消費者交由 D1 同步 |

C3 交接表漏列的 3 條，編排者已逐條看實際 diff 補足如下；測試檔仍由 Claude 修改，編排者只寫這份審查補充。

| 檔案 | 測試名 | 實際變更與理由 |
|---|---|---|
| `tests/test_server_scripts.py` | `test_cpu_moe_without_explicit_fit_still_warns` | `_run_launcher(START_AUX, tmp_path, {}, "--dry-run")` 改為移除空字典的呼叫；helper 已改用 argv，空環境覆寫參數不再存在。原有 fit 警告斷言保持原樣。 |
| `tests/test_server_scripts.py` | `test_legacy_vl_cpu_moe_config_gets_fit_off_and_a_warning` | 同樣移除 `_run_launcher` 的空環境覆寫參數，以配合新的 argv 介面；舊 VL 設定轉換與警告的斷言保持原樣。 |
| `tests/test_server_scripts.py` | `test_set_config_shaped_cpu_moe_config_is_not_warned` | 同樣移除 `_run_launcher` 的空環境覆寫參數；set_config 產生的 CPU-MoE 設定不應觸發警告的行為與斷言保持原樣。 |

這是交付紀錄補足，不是刪除、放寬或改寫測試來取得綠燈。

W3 核對：D1 的 `test_repo_consistency.py` 新增觀察名 5、刪除觀察名 5（內含兩條改名）、修改 10，`test_aicode.py` 刪除 1，全部能在 handoff-D1.md 找到逐條原因；`test_smoke_gate.py` 改的是 SAFETY_MODULES 資料，未改測試函式本體。

Fable 修復第 1 輪：新增 `tests/test_evals.py::test_the_recorder_never_substitutes_a_path_binary_for_the_chosen_one` 與 `tests/test_repo_consistency.py::test_the_checker_shape_exemption_never_covers_an_executable_import`，兩條 smoke regression 的逐項理由、兩個 recorder helper、gate helper / 常數、`tests/test_server_scripts.py::_fake_profile` 與 manifest 資料變更全部記於 [fix-01.md](fix-01.md) §2。沒有再改其他既有測試函式；兩條新測試自紅燈到綠燈原文 SHA-256 完全相同。正式測試執行各一次紅、一次綠，原始 runner exit 由 log 的 `EXIT=1/0` 核對（Claude 使用 shell echo 記錄 exit，外層 shell 的 exit 不作 runner 結果）。

Fable 修復第 3 輪逐項補充（第 2 輪只有文件）：

| 檔案 | 測試名／helper | 變更與行為理由 |
|---|---|---|
| `tests/test_server_scripts.py` | `test_stop_and_status_use_argv_and_constants_not_the_shell`；內部新增 `_fake_nvidia_smi(rows)` | stop 半段守 session 名來源，GPU fixture 應為空；四筆 process 改在 status 半段才提供，繼續守預設 4 與 `--expected 5`。8 個原有 assert 的 AST 完全相同，沒有放寬 returncode 或改 runtime 的基準欄位錯誤。 |
| `tests/test_doctor.py` | `test_explicit_gate_and_implicit_diagnostic_are_separate` | 配合本輪移除 env 模型來源，使用既有 `_write_profile_model` 在 tmp HOME 寫主模型、用 tmp cache；保留殘留 shell 值與空 explicit_model。4 個原有 assert 的 AST 完全相同，explicit／implicit 的離線替身及失敗語意保持原樣。 |
| `tests/test_repo_consistency.py` | `_handoff_markdown`；守它的 `test_the_handoff_markdown_exemption_is_content_only` 本體不變 | helper 的最小深度從 3 收緊為 4，符合必須有任務目錄的內容豁免。該 node 的 12 個 assert、三個來源 walker 的 AST 完全相同；不剪掉走訪、不改副檔名邊界。 |

細節與第一次 smoke 紅燈的對應見 [fix-03.md](fix-03.md)。編排者另以 smoke 當時原始碼為準比對：只有上述三個產品路徑變動，24 個 assert、各 node decorator 與測試名集合皆不變，`SAFETY_MODULES` 與所有 runtime 檔案雜湊不變。這是原始碼／AST 稽核，不是測試通過證據。

最新全量 symbol 稽核共 195 筆新增／刪除／修改觀察，每筆測試名都能在工作者 handoff、fix 或本表找到；fixture／helper／import／manifest 不在 symbol 計數中，仍由各表逐項記錄。第 3 輪沒有追加測試執行，三個 smoke 紅燈仍待必要授權後重驗。

最終驗證補記：使用者之後明示「那你最後smoke一次」，[第二次 smoke](smoke-02.md)的 2395 條全部通過，三個失敗隨整包轉綠；沒有另跑單 node，也沒有再改任何測試碼或斷言。此處前段的「仍待授權」是修復第 3 輪當時的狀態，不是最終結果。
