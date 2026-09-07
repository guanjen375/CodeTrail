# Step 8 — R3 補測結果：447 條前景補跑，與既有 3121 條合併驗收

- 審核者：Claude Fable 5.1（harness 自報 model ID `claude-fable-5-1`，argv 明確 `--effort max`），`ROLE=REVIEWER`。日期 2026-09-07。角色依 `00-intake.md`：Astra MAX 修復、Fable 5.1 MAX 審核。
- 本檔性質：**只收尾 R3 的測試執行**。靜態 Blocker 0 的裁定與書面在 `05-review-fable-r3.md`，本 CLI 沒有重審產品、沒有重新規劃。使用者已對「只補測 447 條並與既有結果合併驗收」回覆「好」（`00-intake.md` 最新段），授權範圍與命令形狀依 `08-test-recovery.md`。
- 本 CLI 的執行：**唯一一次**測試命令（§4），前景等到完整結束；零額外 smoke／full／collect-only／試跑，零重跑已通過的 3121 條，零產品／測試修改，零 live HTTP／生成／安裝／私人 session／memory／全域設定變更，零 stage／commit／push。唯一 repo 寫入是本檔。

**結論一句話：447 條全部通過（collected 447、447 passed、exit 0、JUnit 0 failures／0 errors／0 skipped），依使用者授權與既有 14 個完整 shard 的 3121 條合併，同一 product digest 上 3568 條全部通過；第一次 full 仍記為被 killed、沒有 exit code；T0 實機 TTFT 仍未量測，不宣稱首字延遲收益。**

## 1. 凍結身分（執行前、執行後各核對一次，本檔寫入後再核對一次）

| 欄位 | 值 |
|---|---|
| base HEAD | `f200f697ba54d38a102e8ef66dead652c4002e5f` |
| 本 CLI 開始／執行前／執行後 actual HEAD | `8ba70c58ceddba852ec994081540a11e04de51f5`（三次相同） |
| index tree（`git write-tree`） | `d2d0fc3529444daf2fd696c5cb28a3ef137ce158`（三次相同） |
| product digest，cached 命令 | `61fda568fce324d31691892f51a01aa7f17831e4e2c3815d2c0e16868dac9b4a`（執行前後相同） |
| product digest，worktree 命令 | `61fda568fce324d31691892f51a01aa7f17831e4e2c3815d2c0e16868dac9b4a`（執行前後相同） |
| `git status --porcelain=v1 --untracked-files=all` | 23 條產品／測試路徑全部第二欄空白（21 `M `、2 `A `：`http_cancel.py`、`tests/test_http_cancel.py`）；沒有未暫存或未追蹤產品（執行前後相同） |

- 兩個 digest 命令與 `08-test-recovery.md` 逐字相同：`git diff --cached --binary <base> -- . ':(exclude)docs/workflows' | sha256sum` 與不帶 `--cached` 的同命令。git 直接在 repo cwd 執行，沒有 `git -C`，沒有 `&&`／`echo` 拼串。
- 與 `05-review-fable-r3.md` §1 相同的 digest；HEAD 只多了 D0 的交接 commit，產品仍未 commit。
- 執行前與執行後 `pgrep -af "run_tests|pytest"` 都只回到本 CLI 自己的殼層與查詢本身，沒有任何 runner／shard／pytest 行程殘留。

## 2. 授權與唯讀核對範圍

1. 交接文件：`AGENTS.md`、`08-test-recovery.md` 全文、`00-intake.md`「最新角色調整」與「使用者批准剩餘 447 條補測並合併驗收」兩段、`05-review-fable-r3.md` §1（凍結）、§8（full 真實狀態）、§10（結論）、`05-review-fable-r3-execution.md`、`07-deferred.md` §B 的 B-4／B-5／B-6 列。
2. 保存證據：`full-fable-r3-partial/manifest.json`（selected 3568、completed 3121、incomplete `shard-4`／`shard-9`、`task_output="[killed]"`）、16 個 shard 的 `nodes.txt`、14 個完整 shard 的 `junit.xml`、`shard-4.log`／`shard-9.log`（都停在進度點、沒有 summary 行）、`full-fable-r3.txt`（只有選取與分配行）。
3. runner 只讀：`scripts/run_tests.py` 的 `parse_selection`（只認 `-m`／`--changed`，argv 有別的東西就回 `None`）、`main`（Linux 上 `selection is None` → `subprocess.call([python, "-m", "pytest", *argv])`，exit code 原樣回傳）、`_junit_node_id`（classname 以 `.` 切開，取「最長且對應真實 `.py`」的前綴當路徑，其餘是 class，與 `name` 用 `::` 接回 node id）、`COMPLETED_SESSION_CODES = {0, 1, 5}` 與 exit 5＝沒收到測試的註解。這是本檔 §3／§6 對齊 classname／name 的依據；沒有 import 該 script、沒有執行任何測試函式。

## 3. 參數檔與集合的純資料核對（執行前，不跑 collect-only）

- `/tmp/codetrail-startup-ttft-20260907/remaining-447.nodes.txt`：`sha256sum` 得 `50e63db1e085dc1c7a7906e77e367eecb2f594ae57b89ae900f99f4e17727027`，與 `08-test-recovery.md` 相同。
- 檔案結構：447 行非空 node id、結尾一個換行、沒有空行、沒有前後空白；447 個全部唯一。
- 與原始清單逐字一致：內容等於 `shard-4/nodes.txt`（232）接 `shard-9/nodes.txt`（215），**順序與逐字都相同**，集合也相同。
- 與已完成結果零交集：14 個完整 shard 的 `nodes.txt` 合計 3121 條、彼此無重複；與 447 條交集 0；聯集 3568，等於 16 個 shard `nodes.txt` 的聯集。
- 每檔分布（與 `05-review-fable-r3.md` §8.3 相同）：`tests/test_aicode.py` 27、`tests/test_repo_consistency.py` 64、`tests/test_set_config.py` 106、`tests/test_smoke_gate.py` 35（shard-4）；`tests/test_client_app.py` 46、`tests/test_code_rag_search.py` 93、`tests/test_http_cancel.py` 4、`tests/test_mcp_ingest.py` 72（shard-9）。
- 為什麼不能跑 R3 §8.6 的八個整檔：`tests/test_code_rag_search.py` 另有 94 條、`tests/test_repo_consistency.py` 另有 65 條、`tests/test_mcp_ingest.py` 另有 72 條落在已完成 shard 的 JUnit 裡；整檔會重跑這 231 條已通過 node，違反 AGENTS §1.2 與授權範圍。`@參數檔` 是 runner 自己派 shard 時用的同一種選取（`_run_parallel` 的 `@{nodes_file}`）。

## 4. 執行（唯一一次）

```text
python3 scripts/run_tests.py @/tmp/codetrail-startup-ttft-20260907/remaining-447.nodes.txt --junit-xml=/tmp/codetrail-startup-ttft-20260907/remaining-447.junit.xml > /tmp/codetrail-startup-ttft-20260907/remaining-447.txt 2>&1
```

- 與 `08-test-recovery.md` 逐字相同。Bash 工具 `timeout=600000`、`run_in_background=false`，前景等到工具回完整結果；沒有 `&`／nohup／背景 subprocess，沒有收到 background task，沒有第二次啟動。
- 工具回傳成功、無輸出（stdout／stderr 全部 redirect 進 log）。exit code **0**：Bash 工具對非零 exit 會回報錯誤與代碼，本次沒有；runner 的 `subprocess.call` 把 pytest 的 exit 原樣回傳，log 尾行 `447 passed` 就是 pytest exit 0 的語意。命令依授權逐字使用，沒有另外接 `echo $?`。
- runner 實際轉發（log 最後一行）：`[run_tests] PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /usr/bin/python3 -m pytest @/tmp/codetrail-startup-ttft-20260907/remaining-447.nodes.txt --junit-xml=/tmp/codetrail-startup-ttft-20260907/remaining-447.junit.xml`——單行程逐字轉發，不是並行 shard。
- 執行前 `remaining-447.txt` 與 `remaining-447.junit.xml` 都不存在；執行後兩檔 mtime 皆 17:34:33（+08:00），JUnit `timestamp` 為 `2026-09-07T17:32:48+08:00`。

## 5. 結果

| 項目 | 值 |
|---|---|
| exit code | 0 |
| collected | 447（log 第 5 行 `collected 447 items`；不是 exit 5／0 collected） |
| passed／failed／errors／skipped | 447／0／0／0（log 尾行 `447 passed in 104.93s (0:01:44)`） |
| JUnit `<testsuite>` 屬性 | `tests=447`、`failures=0`、`errors=0`、`skipped=0`，單一 suite，`time=104.929`，`hostname=david-ubuntu` |
| JUnit `<testcase>` | 447 個，經 `_junit_node_id` 規則歸一後 447 個唯一 node id、0 個無法對應；**集合與參數檔完全相同**（只在 JUnit／只在清單皆為空），且順序與參數檔相同 |
| JUnit 內 `<failure>`／`<error>`／`<skipped>` 標籤 | 0／0／0 |
| 每檔通過數 | `test_aicode.py` 27、`test_client_app.py` 46、`test_code_rag_search.py` 93、`test_http_cancel.py` 4、`test_mcp_ingest.py` 72、`test_repo_consistency.py` 64、`test_set_config.py` 106、`test_smoke_gate.py` 35 |
| 失敗 node 清單 | **無** |

- 執行環境（log 表頭）：`platform linux -- Python 3.14.4, pytest-9.1.1, pluggy-1.6.0`、`rootdir: /home/david/CodeTrail`、`configfile: pyproject.toml`，與第一次 full 的 shard log 相同。
- R3 §8.4 列為「沒有完整結果」的 node 這次全部在通過集合內：`tests/test_http_cancel.py` 的三條 transport 契約（其中一條 http／https 兩個 node，共 4 條，這是它們**第一次**有執行證據）；`tests/test_client_app.py` 的 `test_the_tui_primes_on_mount_new_and_session_switch_through_the_coordinator`、`test_the_status_line_reports_the_latest_prime_including_the_ones_after_compaction` 與 R1-B04 的 app 側 regression `test_a_cancel_right_after_submitting_still_lands`；`tests/test_smoke_gate.py` 全部 35 條（含 `[test_client_engine.py]`、`[test_http_cancel.py]` 兩個 gate 案例）；`tests/test_repo_consistency.py` 的 environ／spawn／docs gate（含 `test_no_module_reads_codetrail_settings_from_the_environment`、`test_the_only_llama_server_exec_hands_over_the_stripped_environment`、`test_the_handoff_markdown_exemption_is_content_only`）。

## 6. 與既有 14 份 JUnit 合併（純資料，同一歸一規則）

| 來源 | shard | tests | failures／errors／skipped 屬性 | 標籤 | node 集合 vs `nodes.txt` |
|---|---|---|---|---|---|
| 第一次 full（killed） | 1／2／3／5／6／7／8／10／11／12／13／14／15／16 | 232／232／233／225／230／225／218／217／219／218／218／220／217／217＝**3121** | 全部 0／0／0 | 0 | 14 個 shard **各自**與自己的 `nodes.txt` 集合相等、無重複、0 個無法對應 |
| 本次補測 | 單行程 | **447** | 0／0／0 | 0 | 與參數檔集合相等 |

- 舊 3121 與新 447 交集 **0**；聯集 **3568**，與 16 個 shard `nodes.txt` 的聯集**相等**（缺漏 0、多出 0、testcase 總數 3121 + 447 = 3568）。
- 3121 條是第一次 full 對同一 digest `61fda568…` 的真實 per-node 證據（`05-review-fable-r3.md` §8.2／§8.5 已核），本次沒有重跑它們；第一次 full 的 runner 沒有 summary 行、task output 只有 `[killed]`，**不改記為 exit 0**。

## 7. 判定

1. **補測通過**：collected 447、447 passed、exit 0、JUnit 集合精確等於授權清單。沒有任何失敗 node，沒有要交 Astra 的項目。
2. **兩段合併**：依使用者批准的安排，同一 product digest `61fda568fce324d31691892f51a01aa7f17831e4e2c3815d2c0e16868dac9b4a` 上 **3568 條全部通過**（3121 來自第一次 full 的 14 個完整 shard，447 來自本次前景補測）。這是「兩段執行合併的完整 per-node 結果」，不是「一次 full exit 0」。
3. **基線規則**：本版沒有 full 基線（R3 §8.5）；本次 0 個失敗，所以沒有「未知失敗是否既有」的問題需要裁定，也沒有把任何失敗排除。
4. **AGENTS §1.2 reviewer 交付**：命令＝§4 那一條；結果＝§5；HEAD＝`8ba70c58ceddba852ec994081540a11e04de51f5`、index tree `d2d0fc35…`、產品 digest 同上。
5. **驗收狀態**：程式／離線驗收（靜態 Blocker 0 ＋ 3568 條通過）到此完成；**T0 實機 TTFT 仍未量測**，見 §8。

## 8. 未變、未做與誠實揭露

- **既有測試變動**：本輪零新增；先前對既有測試的修改與理由由 `05-review-fable-r3.md` §6 與 `06-fix-r2.md` 鏈結，本檔不重述、不新增。沒有 skip／xfail。
- **deferred**：沒有新增正式擱置；`07-deferred.md` 由 root 於 D4 更新。供 root 參考的事實：B-4「本版沒有 smoke／full 證據」現在被同一 digest 的 3568 條完整 per-node 結果取代；B-6 擔心的 app 測試背景預熱副作用所在的 `tests/test_client_app.py` 46 條這次全部通過，**只證明測試斷言與畫面在這 46 條的範圍內沒有受影響**，不證明更多；B-5（`docs/basic-usage.md:39`）本輪未動，非安全項。
- **T0**：實機 TTFT 依原 plan 由 David 執行，本輪沒有任何 `prompt_tokens_processed` 或首字延遲數字。固定句：reasoning 與硬體 prefill 成本未變；預熱只把下一輪 prefix 的 prefill 搬到打字之前；是否真的被重用只由 T0 判定。**合併測試通過不等於真實首字延遲改善已測出。**
- **本 CLI 的其他事實**：執行測試時並行送出的一段純資料核對 Python（讀 14 份舊 JUnit）被 Bash 守門以「brace with quote」拒絕，沒有執行；改寫後在測試結束後執行（§6）。這段守門拒絕與測試執行無關，測試只跑了一次。
- **不做的事**：產品未 commit／push（仍待使用者），沒有動 `.pytest_cache`／weights 以外的任何 repo 檔案（runner 單行程模式不寫 weights；本次沒有 `_run_parallel`）。

## 9. 證據清單（private，owner-only `/tmp/codetrail-startup-ttft-20260907/`，不 commit）

| 檔 | 內容 | SHA256 |
|---|---|---|
| `remaining-447.nodes.txt` | 授權的 447 個 node id（= `shard-4/nodes.txt` + `shard-9/nodes.txt`） | `50e63db1e085dc1c7a7906e77e367eecb2f594ae57b89ae900f99f4e17727027` |
| `remaining-447.txt` | 本次 stdout＋stderr 完整 log（22 行：表頭、`collected 447 items`、八檔進度、`447 passed in 104.93s`、runner 轉發行） | `ffabc54959a58a2203b9692c181ad3fc646ab1d75abcc51e41a0723c847d33b9` |
| `remaining-447.junit.xml` | 本次 JUnit（447 testcase、0 failure／error／skipped） | `b831cfd24d80406dbace071ddd53f8bbeae7fc5e2e51fc6d3acada0b244d8768` |
| `full-fable-r3-partial/` | 第一次 full 的 manifest、16 個 `nodes.txt`、14 個 `junit.xml`、16 個 shard log（未動） | 見 `manifest.json` |

## 10. 交付後核對（本檔寫入之後）

- `git rev-parse HEAD`＝`8ba70c58ceddba852ec994081540a11e04de51f5`；`git write-tree`＝`d2d0fc3529444daf2fd696c5cb28a3ef137ce158`；cached／worktree 兩個 digest 均為 `61fda568fce324d31691892f51a01aa7f17831e4e2c3815d2c0e16868dac9b4a`。全部與執行前後相同。
- `git status --porcelain=v1 --untracked-files=all`：與執行前後**完全相同**，只有那 23 條產品／測試路徑。本檔**不出現**在 status：交接目錄被 gitignore（`git status --ignored` 對本檔回 `!!`），既有交接檔是被明確加入追蹤的；本檔也排除在 digest 之外。本檔已在磁碟（owner-only 0600），不 stage、不 commit；是否明確加入追蹤由 root 於 D4 決定。
