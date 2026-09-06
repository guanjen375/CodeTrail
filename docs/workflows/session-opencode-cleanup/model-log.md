# 模型呼叫紀錄

基準程式碼 HEAD：`a1682d5`。主模型身分取自 Claude Code JSON 的 `system.init.model` 與 `assistant.message.model`，不採模型自述。effort 記錄實際傳入的 CLI 旗標；回傳 metadata 沒有另提供 effort 欄位時不宣稱已由伺服器回報。完整串流 log 留在本機暫存目錄，不包含使用者既有 session。

| 階段 | 明確請求 model | 明確 effort | init model | assistant model | 狀態 |
| --- | --- | --- | --- | --- | --- |
| 1 初步規劃 | `claude-fable-5-1[1m]` | `--effort max` | `claude-fable-5-1` | `claude-fable-5-1` | 進行中 |
| 實作模型可用性探測 | `claude-opus-5` | `--effort max` | `claude-opus-5` | `claude-opus-5` | success |

Astra 審核將透過明指 `model=gpt-6-astra`、`reasoning_effort=max` 的 Codex 子代理執行，僅允許寫審核文件，禁止改 runtime / tests。

本次沒有 `ROLE=REVIEWER`，全流程仍遵守 developer 測試權責：bug regression red/green，交付前一次 smoke；不執行 full。
