# 測試變更的靜態核對與補充

編排者以 `a1682d5` 的原始碼為準，對照目前測試函式（含 decorator 與函式內註解）的原始碼雜湊；沒有 import 測試模組、collect 或執行測試。原始觀察表在本機 `test-changes-W2.json`。本表已核對至 W3；Fable 修復後仍須更新。

逐條原因表：

| 工作 | 明細 | W2 核對結果 |
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
