# Fable R3 執行記錄：靜態歸零，full 被 CLI 結束中斷

本檔由 root 維護執行 metadata，不冒充 reviewer 審核報告。日期 2026-09-07。

- reviewer：`claude-fable-5-1`，argv 明確 `--effort max`。init 與全部 82 個主 assistant frame 均為該型號，沒有 fallback event。CLI 2.1.263，session `1cbd8d21-3c8a-4a1d-8e42-ebdc6e65f675`。
- 該次 reviewer 最後可見回覆明說靜態審核為 **zero Blockers**，並已啟動獲准的 full；這是 Fable 的裁定，root 沒有另行判定產品。
- 審核／full 啟動時 actual HEAD=`b7edcd7b52dba7b5f5c00b12976e139191b4e49f`，index tree=`91fe953c1034944af4fabb7338715f975d10993b`，product digest=`61fda568fce324d31691892f51a01aa7f17831e4e2c3815d2c0e16868dac9b4a`。產品未 commit；root 在 CLI 結束後再次核對 cached／worktree digest 都相同，無未暫存／未追蹤產品。

## full 的真實狀態

唯一一次測試命令：

```text
python3 scripts/run_tests.py > /tmp/codetrail-startup-ttft-20260907/full-fable-r3.txt 2>&1
```

Fable 的 Bash 呼叫設 `timeout=600000`、`run_in_background=true`，取得 task ID `bml0umksx`。其後 Fable 結束 CLI 回合，文字稱等背景任務完成再繼續；CLI 實際隨結束清理該 task，task output 留下 `[killed]`。目前已無該次 runner／shard 行程。**CLI process exit 0／result success 不是 full exit 0。沒有收到 full 的完整 exit code，不可推測為通過。**

原始 task output：`/tmp/claude-1000/-home-david-CodeTrail/1cbd8d21-3c8a-4a1d-8e42-ebdc6e65f675/tasks/bml0umksx.output`。完整 CLI stream 留在 private `05-review-fable-r3.stream.jsonl`，不 commit 原始 stream。

runner 選中 3568 條、42 檔、16 shards。root 唯讀盤點留下的 log／JUnit，保存到 owner-only `/tmp/codetrail-startup-ttft-20260907/full-fable-r3-partial/`（只保留 logs、node 清單、JUnit、manifest，不保留測試暫存內容）：

| 證據 | 結果 |
|---|---|
| 14 個完整 shard | 3121 passed；0 failed／errors／skipped；每個皆有完整 JUnit 與 passed 尾行 |
| shard 4 | 選中 232，沒有完整 JUnit；進度停在 `tests/test_set_config.py` |
| shard 9 | 選中 215，沒有完整 JUnit；進度停在 `tests/test_client_app.py` |
| 全部 full | **未完成**；447 條所在的兩個 shard 無完整結果，不能用進度點推導個別 node 通過／失敗 |

root 沒有補跑任何測試，沒有把未知失敗當基線或擱置；Fable 尚未寫完 `05-review-fable-r3.md`。後續由同型號 MAX 補齊書面審核與實際測試狀態，不回寫第一次執行為成功。

## 後續測試權責

AGENTS.md §1.2 明訂「程式碼收斂後對目前 HEAD 執行一次 full」與「程式碼未變時不得重跑已通過的測試」。本版已有 3121 條完整通過證據，因此 root 不自行重跑整包；若要再執行一次 full，須取得使用者針對這次 CLI 中斷的明確補跑授權。補跑時保持原產品 digest，Fable 使用前景 Bash 等到完整結果，禁止 `run_in_background=true` 或提前結束 CLI；前後再次記錄 HEAD／index／digest。

此處是測試執行未完成，沒有新的靜態產品 Blocker，沒有模型技術分歧，也不是正式 deferred。T0 實機 TTFT 仍未量測；任務尚未符合完成條件。

## 書面交付已補齊（後續狀態）

同型號 MAX 的 report-only CLI 已交付 `05-review-fable-r3.md`，零測試、唯一 repo 寫入為該報告。正式結論：R2 兩項關閉、靜態 Blocker 0，full 仍未完成；前後產品 digest 與上列相同。報告 §8.6 建議只補兩個未完成 shard 的 447 條，也列出再跑一次 full 的形狀；兩者都須使用者接受本次中斷後的補測安排，不能把等待當授權。root 已詢問 full 補跑授權，尚未收到答覆。模型 metadata 見 `00-model-log.md` 的書面補齊段。
