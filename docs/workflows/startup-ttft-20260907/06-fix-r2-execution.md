# Step 5 R2 — 執行受阻紀錄

作者：root，僅記錄流程、工具回傳與內容身分。這不是 Fable 的修復交付；`06-fix-r2.md` 尚未產生。

## 現況

Astra R2 的 **R2-B01、R2-B02 仍是 active Blocker**，詳見 `05-review-astra-r2.md`。一項是舊預熱在等待 HTTP headers 時未滿足原 B7 的一秒內放鎖要求；另一項是取消登記、snapshot、POST 准入與 session 切換之間的三種 lost-abort 交錯。

使用者要求回修由 Fable 5.1 MAX 執行、Astra 只審不改。兩次明確指定 Fable 的執行都被模型安全審查拒絕，尚未交付產品修改。root 不替換成其他模型續寫，也不把模型拒絕當成技術審核分歧已達擱置門檻。

## 兩次執行

| 項目 | 首次 R2 | 同模型重試 |
|---|---|---|
| 明確模型／effort | `claude-fable-5-1`／`max` | `claude-fable-5-1`／`max` |
| 主要事件 | `model_refusal_fallback`，Fable 5.1 → Opus 4.8，scope=session | `model_refusal_no_fallback` |
| 回傳分類 | `cyber`，explanation=null | `cyber`；API 表示觸發 Usage Policy 的 cyber 限制 |
| 收尾 | root 發現換型後中止；result `is_error=true` | process exit 1；result `is_error=true` |
| 產品／測試 Write/Edit | 0 | 0 |
| 新測試執行 | 0 | 0 |
| private log | `06-fix-r2.stream.jsonl` | `06-fix-r2-retry.stream.jsonl` |

完整模型用量及回傳身分列在 `00-model-log.md`。首次收尾用量另包含 Opus 5，不能將該次冒稱全程 Fable；重試只有主 Fable 與既有輔助 Haiku 用量。重試的 `<synthetic>` 是錯誤訊息標記，不是模型。

重試只對該 CLI 行程設定 `CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK=1`，使模型拒絕直接回報、不自動換型；原模型審查與工具權限都保留。沒有改全域 Claude 設定或 repo 設定。沒有繼續嘗試繞過模型拒絕。

## 產品身分與測試

- base HEAD：`f200f697ba54d38a102e8ef66dead652c4002e5f`
- 核對時 actual HEAD：`cb1f58c2964953203dfa7b84281919e27f85e174`
- 核對時 index tree：`a7c7b5903b6b57f5efbbeccc881fdcf54c490ed9`
- cached 與 worktree product diff SHA-256 均為：`16bda189fe8d6d99edfd1ebc86b59a373ac2a1816389d98d5c59ab41731f9349`

全部 21 個非交接產品／測試路徑仍為完全 staged 的 `M `，沒有未暫存或新未追蹤產品；與 Astra R2 審核的內容相同。HEAD 只有交接 commit，產品未 commit、未 push。後續提交本紀錄只會前進交接 HEAD，不改產品 digest。

這兩次沒有執行任何 regression、smoke 或 full；沒有新增或修改測試。前版 smoke 的一個失敗與 R1 四條 regression 的紅／綠證據按原交接保留，不能將它們當成本版 full 通過。靜態 Blocker 未歸零，因此仍未進入 reviewer full。

## 後續交接

需要使用者決定是否授權另一個 Claude 型號接手這兩項回修。取得授權後，該 Claude writer 先在 `06-fix-r2.md` 列 Dependency、可寫檔案、共享 owner 與工具／命令，再按原計畫與 AGENTS 執行必要 regression 紅／綠、修復、重新凍結。Astra 對新 digest 審核；靜態歸零後才對同份內容執行一次 full。

目前不符合使用者的完成定義，也沒有新增正式擱置。原案的 T0 實機收益仍未量測。R1 自動 memory 的本任務副作用已精確清理，詳見模型 log；其他 memory 未動。
