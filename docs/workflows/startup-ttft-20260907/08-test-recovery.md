# R3 獲准補測交接 — Fable 5.1 MAX reviewer

使用者已回覆「好」，批准只補測未完成的 447 條，與既有 3121 條合併验收；原問答及授權邊界見 `00-intake.md` 最新段。這是同一 R3 的測試收尾，不重新規劃或重審已通過的產品。

## Dependency、owner、工具與命令

| 階段 | Dependency | 工作與可寫檔案 |
|---|---|---|
| D0 | 使用者授權已取得、R3 靜態 Blocker 0 | root：本檔、`00-intake.md`、`00-model-log.md`、`07-deferred.md` 與交接 commit；唯讀比對保存的 nodes，製作精確參數清單 |
| D1 | D0 交接提交、產品仍凍結 | Fable reviewer：唯讀核對本檔、`05-review-fable-r3.md`、`05-review-fable-r3-execution.md`、manifest／nodes／JUnit；核對 HEAD、index、status、cached／worktree digest。只寫 `08-test-results.md` |
| D2 | D1 身分與清單一致 | Fable reviewer：唯一一次下列補測命令，前景等待完成；完整 log／JUnit 寫私有目錄 |
| D3 | D2 完整收尾 | Fable reviewer：核對精確 node 集合、實際 exit、與 14 個既有 JUnit 合併、測後同一 digest；交付 `08-test-results.md`。有新失敗集中交 Astra 修，不改產品、測試或把未知失敗當基線 |
| D4 | D3 報告落檔 | root：核對 metadata、更新模型紀錄／擱置清單並提交交接；產品不 commit／push |

可用工具：Read／Glob／Grep、Bash 的 git 唯讀命令、Python 僅作 AST／XML／JSON／雜湊資料核對，以及 D2 唯一測試命令；Write 只准 `08-test-results.md`。零安裝、零 live HTTP／生成、零私人 session、零全域設定／memory 變更；不委派其他模型，不執行額外 tests／smoke／full／collect-only。Astra 保持產品 writer；root 不代跑測試。

## 凍結與精確選取

- base HEAD：`f200f697ba54d38a102e8ef66dead652c4002e5f`。
- D0 開始 HEAD：`fc11a4e8519c944647790f5fb2c72022c81451a2`；index tree：`3400fe757af2d1f314b1415a3377421313dbdddc`。D0 只提交交接 markdown，提交後 actual HEAD／index 由 reviewer 重新記錄。
- 唯一被審產品 digest：`61fda568fce324d31691892f51a01aa7f17831e4e2c3815d2c0e16868dac9b4a`。23 個產品路徑 staged（21 M、2 A）、零未暫存／未追蹤產品；HEAD 僅含交接 commit。
- 舊證據：`/tmp/codetrail-startup-ttft-20260907/full-fable-r3-partial/`。14 份完整 JUnit＝3121 passed，原 shard-4／shard-9 的 node 清單合計 447。
- 參數檔：`/tmp/codetrail-startup-ttft-20260907/remaining-447.nodes.txt`，SHA256=`50e63db1e085dc1c7a7906e77e367eecb2f594ae57b89ae900f99f4e17727027`。每行一個原始 node ID。root 純資料核對：447 個唯一 node、與3121個完整結果零交集，聯集3568。
- **更正 R3 報告的命令形狀**：不能傳八個整檔，因為其中三檔分跨已完成 shard，會重跑部分已通過 node。pytest 的 `@參數檔` 在本 repo runner 自己的 shard 呼叫也使用；下列命令帶 pytest 參數，runner 單行程逐字轉發，選取只含授權的447條。JUnit 旗標只保存證據，不改選取。

## 唯一補測命令與等候規則

```text
python3 scripts/run_tests.py @/tmp/codetrail-startup-ttft-20260907/remaining-447.nodes.txt --junit-xml=/tmp/codetrail-startup-ttft-20260907/remaining-447.junit.xml > /tmp/codetrail-startup-ttft-20260907/remaining-447.txt 2>&1
```

Bash 呼叫必須 `timeout=600000`、`run_in_background=false`，等工具回實際完整結果再往下，禁止在測試進行中結束 CLI。不得自行加 `&`／nohup／背景 subprocess，也不得因等候或讀取輸出問題重新啟動第二次。stdout／stderr 第一次即完整存檔，不用 pipeline 隱藏 exit。若意外收到 background task，必須保住該次行程並等待同一 task 收尾，不能像前次結束 CLI 再期待續行。

前後各核對：`git rev-parse HEAD`、`git write-tree`、`git status --porcelain=v1 --untracked-files=all`；兩種 digest 命令均等於上列值：

```text
git diff --cached --binary f200f697ba54d38a102e8ef66dead652c4002e5f -- . ':(exclude)docs/workflows' | sha256sum
git diff --binary f200f697ba54d38a102e8ef66dead652c4002e5f -- . ':(exclude)docs/workflows' | sha256sum
```

補測須 collected 447（不是 exit 5／0 collected），JUnit node 集合精確等於參數檔，與既有3121條無重複且聯集覆蓋原3568個node。通過時稱「依使用者授權合併兩段結果，3568條全部通過」，不可將前次被 killed 的 full 改記為 exit0。失敗時列全部 node ID、真實 exit、內容身分，回 Astra 修；不修改／skip／xfail 測試，不擴修無關既有問題。

原 T0 實機 TTFT 仍由 David 執行，未量測收益不得宣稱。產品、測試及歷史審核報告本輪不改，只有補測結果與相關交接落檔。
