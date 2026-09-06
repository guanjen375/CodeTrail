目前已完成三項需求的實作與 Fable 修復；**整體驗證尚未完成，不宣稱 Blocker 0。** [ASTRA 第 5 輪](implementation-review-05.md)已靜態接受三項修法：待改靜態缺陷 0，第一次 smoke 的 3 個失敗仍待追加驗證。修復版本已具體可審核，等待必要授權。

| 需求 | 已實作結果 | 操作／交接 |
|---|---|---|
| TUI session | `/session` 可用上下鍵選擇、Enter 直接切換。每列顯示時間、輪數及第一個真實使用者問題的大綱；大綱由本地記錄取得。切換和啟動接續均重播原始訊息、reasoning、工具結果及壓縮標記，包含壓縮前原文。 | `/session <id>`、`/resume <id>`、`aicode -c`、`aicode --session <id>` 共用接續行為；[Session 交接](handoff-S.md)、[使用說明](../../basic-usage.md#7-切換與接續對話)。 |
| 移除 OpenCode | 刪除 repo 的遷移模組、plugin stub 與專用測試；仍在使用的壓縮公式和常數留在 `compaction_formula.py`，eval 舊欄位僅保留歷史資料讀取相容。 | [移除交接](handoff-B.md)。runtime 不接觸使用者的舊 OpenCode 設定；固定舊版原安裝工具的升級路徑見 [troubleshooting](../../troubleshooting.md)。 |
| 設定來源 | 啟動核心、set_config、stop/status、eval recorder 與 test runner 的功能設定由環境變數移往檔案、repo 常數與 argv。`start.sh` 只轉發 argv；tmux pane 最終 exec 會清除殘留設定環境。 | [Loader](handoff-C1.md)、[set_config](handoff-C2.md)、[啟停與 runner](handoff-C3.md)、[部署設定說明](../../deployment-profiles.md)。 |

新增 `deployment.json.llama_bin` 與 `services.<role>.gpu`。共用同一設定目錄的舊世代 loader 不認識新鍵時會明確拒絕啟動，升級相容限制已記入正式文件。施工沒有操作真實模型服務、沒有執行遷移；第一次 smoke 後觀察的 46 個舊設定路徑 metadata／目錄項目均與動工前一致。

驗證證據與範圍：

- Session 的兩條新 regression 已各有原始碼未修前紅燈與同 node 綠燈，見 [紅燈](handoff-S-red.md)、[綠燈](handoff-S.md)。Fable 第一輪另兩條 regression 的紅綠見 [fix-01.md](fix-01.md)，由編排者核對實際 runner exit 及原文雜湊。
- [第一次 smoke](smoke-01.md)：2395 selected，2392 passed／3 failed，exit 1；執行時 HEAD `ea6a1b613ee1bd34369fd92591f3cdc94a281fa5`，產品 diff SHA256 `9a078521198130df68f704231fad73354baee1fa140d60e2a00d6a7dadb9755d`。沒有 full、沒有動工前 suite 基線、沒有 0 collected。
- [第 4 輪集中審核](implementation-review-04.md)與 [Fable 修復](fix-03.md)：三處 fixture／gate 修正已落檔；24 個原有 assert、smoke decorator 與來源 walker 不變，所有 runtime 在這輪未變。這份靜態比對不能替代重驗。
- [第 5 輪靜態接受](implementation-review-05.md)核對 HEAD `1ff76156e9d2ee8dcc77f8f4f7661f6f8bf05fc7`，修復後產品 diff SHA256 `19892f4e664164159396e1bf5c3e9832510eba74cd06cb31e6413814a11a1364`；審核期間產品雜湊未變。沒有增加擱置項或改寫第一次紅燈結果。
- [逐條測試變更與理由](test-change-audit.md)已核對 195 筆 symbol 變更，另列 fixture／helper／manifest 變更。
- 預定一次 smoke 已用完。追加跑三個失敗 node 與第二次整包 smoke 尚未執行；依 [AGENTS §1.2](../../../AGENTS.md#12-執行權責)及 [最終計畫 §5.4](plan-final.md#54-執行邊界)，待修復具體可審核後取得必要同意。

流程使用 FABLE5.1 MAX 初稿 → ASTRA MAX 計畫審核 → FABLE5.1 MAX 最終計畫 → OPUS5 MAX 依 DAG 平行實作 → ASTRA 審核／FABLE5.1 修復。Claude 呼叫皆明指 model 與 effort，回傳身分與實際工具範圍記於 [model-log.md](model-log.md)。ASTRA 沒有修改產品／測試碼。

[擱置清單](deferred.md)仍為空。既有的 5 次額外探查及 2 個範圍外記憶 Edit 已揭露；後者已精確撤回，詳見 [execution-notes.md](execution-notes.md)，不宣稱流程全程無偏差。

只有本固定路徑下的交接文件提交。58 個產品／測試／正式文件路徑仍在工作樹，未提交，沒有 push。實作基準為 `a1682d5`；完整產品 diff 可用下列唯讀命令檢視：

```bash
git diff a1682d5 -- . ':(exclude)docs/workflows/session-opencode-cleanup/**'
```

`Tests: smoke only — reviewer owns full execution.`
