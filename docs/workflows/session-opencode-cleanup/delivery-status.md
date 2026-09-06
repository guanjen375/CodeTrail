三項需求的實作、Fable 修復與 ASTRA 審核已完成。使用者明示「那你最後smoke一次」後，[最後一次 smoke](smoke-02.md)取得 **2395 passed、0 failed／errors／skipped，exit 0，實際 10.391 秒**。產品雜湊與靜態接受的版本相同；[ASTRA 最終核對](implementation-review-06.md)確認 **Active Blocker = 0，無分歧擱置**。developer 階段的 smoke-only 驗證完成，full 未執行。

| 需求 | 已實作結果 | 操作／交接 |
|---|---|---|
| TUI session | `/session` 可用上下鍵選擇、Enter 直接切換。每列顯示時間、輪數及第一個真實使用者問題的大綱；大綱由本地記錄取得。切換和啟動接續均重播原始訊息、reasoning、工具結果及壓縮標記，包含壓縮前原文。 | `/session <id>`、`/resume <id>`、`aicode -c`、`aicode --session <id>` 共用接續行為；[Session 交接](handoff-S.md)、[使用說明](../../basic-usage.md#7-切換與接續對話)。 |
| 移除 OpenCode | 刪除 repo 的遷移模組、plugin stub 與專用測試；仍在使用的壓縮公式和常數留在 `compaction_formula.py`，eval 舊欄位僅保留歷史資料讀取相容。 | [移除交接](handoff-B.md)。runtime 不接觸使用者的舊 OpenCode 設定；固定舊版原安裝工具的升級路徑見 [troubleshooting](../../troubleshooting.md)。 |
| 設定來源 | 啟動核心、set_config、stop/status、eval recorder 與 test runner 的功能設定由環境變數移往檔案、repo 常數與 argv。`start.sh` 只轉發 argv；tmux pane 最終 exec 會清除殘留設定環境。 | [Loader](handoff-C1.md)、[set_config](handoff-C2.md)、[啟停與 runner](handoff-C3.md)、[部署設定說明](../../deployment-profiles.md)。 |

新增 `deployment.json.llama_bin` 與 `services.<role>.gpu`。共用同一設定目錄的舊世代 loader 不認識新鍵時會明確拒絕啟動，升級相容限制已記入正式文件。施工沒有操作真實模型服務、沒有執行遷移；最後一次 smoke 後觀察的 46 個舊設定路徑 metadata／目錄項目均與動工前一致。

驗證證據與範圍：

- [最後一次 smoke](smoke-02.md)：2395 selected／41 個檔／16 shards 全數通過，執行 HEAD `0619b08b9697d406817988c9748ccd200487235f`；產品修改仍在工作樹，diff SHA256 `19892f4e664164159396e1bf5c3e9832510eba74cd06cb31e6413814a11a1364`。第一次的三個失敗隨整包轉綠，沒有另跑單 node；測試前後 58 個產品路徑雜湊不變。
- [ASTRA 最終審核](implementation-review-06.md)獨立核對授權、HEAD 差異、產品／stdout hash 與 16 份 shard 小計，解除三項待驗證失敗，Active Blocker 0。審核沒有重跑測試或修改產品。
- Session 的兩條新 regression 已各有原始碼未修前紅燈與同 node 綠燈，見 [紅燈](handoff-S-red.md)、[綠燈](handoff-S.md)。Fable 第一輪另兩條 regression 的紅綠見 [fix-01.md](fix-01.md)，由編排者核對實際 runner exit 及原文雜湊。
- [第一次 smoke](smoke-01.md)：2395 selected，2392 passed／3 failed，exit 1；執行時 HEAD `ea6a1b613ee1bd34369fd92591f3cdc94a281fa5`，產品 diff SHA256 `9a078521198130df68f704231fad73354baee1fa140d60e2a00d6a7dadb9755d`。沒有 full、沒有動工前 suite 基線、沒有 0 collected。
- [第 4 輪集中審核](implementation-review-04.md)與 [Fable 修復](fix-03.md)：三處 fixture／gate 修正已落檔；24 個原有 assert、smoke decorator 與來源 walker 不變，所有 runtime 在這輪未變。這份靜態比對不能替代重驗。
- [第 5 輪靜態接受](implementation-review-05.md)核對 HEAD `1ff76156e9d2ee8dcc77f8f4f7661f6f8bf05fc7`，修復後產品 diff SHA256 `19892f4e664164159396e1bf5c3e9832510eba74cd06cb31e6413814a11a1364`；審核期間產品雜湊未變。沒有增加擱置項或改寫第一次紅燈結果。
- [逐條測試變更與理由](test-change-audit.md)已核對 195 筆 symbol 變更，另列 fixture／helper／manifest 變更。
- [AGENTS §1.2](../../../AGENTS.md#12-執行權責)與 [最終計畫 §5.4](plan-final.md#54-執行邊界)的預定一次 smoke 用完後，先完成三項修復及靜態審核，再取得使用者明示的最後一次 smoke 授權。本次只按該範圍執行一包，沒有另跑三個 node；總計兩次 smoke、零 full，不再追加測試。

流程使用 FABLE5.1 MAX 初稿 → ASTRA MAX 計畫審核 → FABLE5.1 MAX 最終計畫 → OPUS5 MAX 依 DAG 平行實作 → ASTRA 審核／FABLE5.1 修復。Claude 呼叫皆明指 model 與 effort，回傳身分與實際工具範圍記於 [model-log.md](model-log.md)。ASTRA 沒有修改產品／測試碼。

[擱置清單](deferred.md)仍為空。既有的 5 次額外探查及 2 個範圍外記憶 Edit 已揭露；後者已精確撤回，詳見 [execution-notes.md](execution-notes.md)，不宣稱流程全程無偏差。

只有本固定路徑下的交接文件提交。58 個產品／測試／正式文件路徑仍在工作樹，未提交，沒有 push。實作基準為 `a1682d5`；完整產品 diff 可用下列唯讀命令檢視：

```bash
git diff a1682d5 -- . ':(exclude)docs/workflows/session-opencode-cleanup/**'
```

`Tests: smoke only — reviewer owns full execution.`
