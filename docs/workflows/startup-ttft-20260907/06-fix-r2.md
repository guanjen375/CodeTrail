# Step 5 R2 回修 — Astra MAX developer

- 日期：2026-09-07。使用者最新指示「你改成 astra max 修復 fable5.1 max 審核」；本輪 Astra (`gpt-6-astra`, effort `max`) 為唯一產品 writer，Fable 5.1 MAX 接任 reviewer。
- 範圍：只修 `05-review-astra-r2.md` 的 R2-B01 / R2-B02 與直接受影響的呼叫端。`03-plan-final.md` 驗收不變；R1 其他已關項不重開，歷史交接不回寫。
- 動工 base HEAD：`f200f697ba54d38a102e8ef66dead652c4002e5f`。
- 動工 actual HEAD：`500169a6d0d14ec206104bf6964403210d73d025`（只有交接 commit，產品未 commit）。
- 動工 product digest：`16bda189fe8d6d99edfd1ebc86b59a373ac2a1816389d98d5c59ab41731f9349`；現有 staged 產品修改全部保留。

## 1. Dependency、owner 與准許工具（產品編輯前登記）

目前沒有需要獨立平行施工的 READY lane。transport、生命週期與 regression 彼此依賴，由同一 writer 順序完成；root 不需等待其他 lane。

| 次序 | 工作 | Dependency | owner / 可寫檔案 |
|---|---|---|---|
| D0 | 讀規則與前輪證據、寫本交接 | 無 | Astra：本檔 |
| D1 | 離線 regression 釘住 pending headers 的 B7 與三處取消競態，先取得行為紅燈 | D0；對应產品仍未修 | Astra：`tests/test_client_engine.py`；必要的 transport regression 登記為新檔 `tests/test_http_cancel.py` |
| D2 | 預熱專用可取消 transport：在 HTTP bytes 送出前登記 socket，abort shutdown / close 已登記連線；取消後新取得的 socket 先關再拒絕送出。沿用 requests 的 TLS 驗證與 endpoint policy，不改共用 session | D1 | Astra：新檔 `http_cancel.py`（封裝 scoped requests adapter / socket cancellation）；`llama_client.py`（只為預熱提供選用 cancellation，既有預設路徑不變） |
| D3 | 原子登記預熱 token / 歷史、完整觀察 abort、封住 session 轉換空窗；保留模型鎖直到 HTTP 已關閉或未曾送出且取消後不可能再送 | D1、D2 | Astra：`client_engine.py`、`tests/test_client_engine.py` |
| D4 | 同一新 regression 轉綠、登記安全 gate、同步契約與文件 | D2、D3 | Astra：`tests/test_smoke_gate.py`、`AGENTS.md`（只相關 §2）、`README_DEV.md`、`docs/troubleshooting.md` |
| D5 | 靜態整合、stage / freeze、完整交付 | D4 | Astra：本檔、`07-deferred.md`；root：`00-intake.md`、`00-model-log.md` 與交接 markdown commit |
| D6 | Fable 靜態 Blocker 審核，歸零後同一 freeze full | D5 | Fable reviewer；Astra 不執行 full |

工具：`rg` / `sed` / `cat` / Python AST 與雜湊唯讀核查、`apply_patch` 編輯；`git diff/status/rev-parse/write-tree` 唯讀識別；只准 stage、不 commit / push / reset / stash / checkout。

本輪可執行的測試**僅**自己新增的 regression 單 node：

```text
python3 scripts/run_tests.py tests/<file>.py::<new_regression_node> > /tmp/codetrail-startup-ttft-20260907/astra-r2-<case>-red.txt 2>&1
python3 scripts/run_tests.py tests/<file>.py::<same_new_regression_node> > /tmp/codetrail-startup-ttft-20260907/astra-r2-<case>-green.txt 2>&1
```

先標 smoke，在未修的對應產品上跑紅；紅燈必須是本次錯誤行為。完整 stdout / stderr 第一次就 redirect，實際 exit 由 exec 工具記錄；不使用 pipeline 隱藏 exit，不為收回輸出重跑。其餘新增安全契約只寫不單跑，逐條登記 `SAFETY_MODULES`。既有已綠 node 不重跑。**不跑 smoke / full / 整檔 pytest / 直接 pytest / 間接測試**；唯一整合 smoke 額度已使用。

允許靜態檢查：`python3 -m compileall -q .`、`python3 scripts/check_readme_consistency.py`；必要時限定檔案的 `py_compile`。所有測試 HTTP 都是離線替身；零 live HTTP、零私人 session、零部署操作、零新增環境設定讀取、零 `process_env` 外 subprocess。

## 2. 修復要求與實作邊界

1. R2-B01：`new_session()` / `adopt()` 中止 pending headers 的預熱，1 秒內舊預熱 `aborted`、`priming=False` 且鎖可取得；舊連線必須先 shutdown，不能用提早放仍在飛的請求鎖換取測試綠燈。
2. R2-B02：job identity / abort Event / history snapshot 一起登記；完成得很快的 GET / POST 也觀察 abort；取消後尚未排到的 worker 不得送 HTTP；session 切換期間不得新登記舊歷史預熱，既有 job 在切換前中止。
3. 不動正常回合 `_cancel` / `_armed`、session append、事件流、共享模型鎖、payload / heal / gate / telemetry 成功條件；新 transport 預設不影響正常模型呼叫。HTTP cancellation 必須維持 TLS 驗證、endpoint policy、無 env proxy / netrc 與不跟 redirect。
4. 原 B7 不降級、不改計畫或以測試放寬消除 Blocker。TTFT 的 T0 仍未量測，不宣稱真實速度收益。

## 3. 執行結果

進行中。最終交付會補完整 diff、各紅綠 node / 命令 / exit / log 與節錄、動到的既有測試及行為理由、靜態檢查、凍結三件套與限制。

`Tests: smoke only — reviewer owns full execution.` 本輪實際只允許新 regression 單 node，未再跑 smoke；full 由 Fable reviewer 在靜態收斂後執行。
