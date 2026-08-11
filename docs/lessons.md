# lessons(行為教訓)— 讓「你對模型行為的糾正」延續到之後的 session

你在 TUI 裡糾正過模型的做事方式(「migration 前要先確認 backward compatibility」「不要每次都重跑整套測試」),下一個 session 它又忘了 —— lessons 機制把這類糾正變成**經你核准**的行為規則,每個 session 自動注入,直到過期複審。

跟知識庫嚴格分離:

| | knowledge.json(RAG) | lessons.json |
|---|---|---|
| 內容 | 客觀知識(spec / datasheet / 手冊) | 主觀行為教訓(你糾正過的做事方式) |
| 寫入 | `ingest_document` | `record_lesson` 提案 + 你核准(permission `ask`) |
| 取用 | 檢索(embedding + reranker) | 全量注入(上限 20 條,不做 embedding 檢索) |
| 位置 | `<AICODE_ROOT>/knowledge.json` | `~/.config/codetrail/lessons.json`(per-deployment,不跨部署共享) |

[回到 README](../README.md)。

---

## 生命週期

```
你糾正模型行為
  → 模型呼叫 record_lesson 提案(rule 必須是單行祈使句行為規則)
  → permission ask:你在核准框看到 rule,核准才寫入 lessons.json
  → 下個 session:aicode 啟動時把 active lessons render 進
    <AICODE_ROOT>/.codetrail/lessons.md,OpenCode 經 opencode.json 的
    "instructions" 連同 AGENTS.md 一起載入 context
  → 90 天後過 review_by:該條停止注入,aicode 啟動時醒目列出待複審清單
  → 你複審:renew(再延 90 天)或 delete
```

每條 lesson 的欄位:

```json
{
  "id": "L-001",
  "rule": "migration 前先確認 backward compatibility",
  "scope": "project",
  "project": "/path/to/that/project",
  "created": "2026-08-11",
  "review_by": "2026-11-09",
  "hit_count": 0,
  "last_triggered": null
}
```

- `scope: "project"`(預設)只注入到記錄它的那個專案;`"global"` 注入到此部署的所有專案。跨專案皆適用的工作習慣才用 global。
- `rule` 必須是可執行的祈使句行為規則,單行、≤200 字元;事件敘述(「上次 migration 壞了」)與 error log 會被拒絕。真正的品質把關是你的核准框 —— 內容不對就拒絕,讓模型改寫再提。

## 什麼會觸發、什麼不會

模型只該在「**你糾正它的做事方式**」之後提案。以下都不是觸發條件(全域 AGENTS.md 範本與 tool 說明都有明訂):

- 工具執行失敗 / exception / lint 錯 —— 那是環境或程式問題;
- 答案內容錯誤被指正 —— 客觀知識修正請 `ingest_document` 進 KB;
- 模型自己覺得「這樣做比較好」—— 沒有人的糾正就不記。

被你拒絕的提案就結束,模型不該換句話重試。**沒有任何無審核的自動寫入路徑。**

## 上限與 fail-loud

可注入的 active lessons 上限 **20 條**(每個專案看到的 global + 該專案 project 條目合計)。滿了之後 `record_lesson` 與 `renew` 都會拒絕並要求人工整併 —— 不會靜默丟掉舊的、也不會只注入前 20 條。條數就 20,所以全量注入、不做 embedding 檢索與自動 decay / 衝突解決。

session start(`aicode`)時:

- lessons store 損壞 → **拒絕啟動**(fail-loud),修復或 `AICODE_LESSONS_SKIP=1 aicode` 緊急跳過(該 session 不注入);
- active 超過 20(只可能手改 JSON 造成)→ 拒絕啟動,要求整併;
- 有條目過 review_by → 照常啟動,但該條停止注入,並醒目列出待複審清單與 renew / delete 指令。

## 管理指令

在 CodeTrail checkout 目錄執行:

```bash
python lessons.py list              # 全部條目(含 EXPIRED 標記、hit_count)
python lessons.py renew L-001      # 複審通過:review_by = 今天 + 90 天(--days 可調)
python lessons.py delete L-001     # 淘汰
python lessons.py hit L-001        # 人工記一次命中(見下)
```

進階:`--file` 或 `AICODE_LESSONS_FILE` 可指定 store 路徑(預設 `~/.config/codetrail/lessons.json`)。

## hit_count 的誠實說明

注入的 lessons.md 會要求模型:套用某條規則時在回覆中標註 `[L-003]` 這樣的編號,讓你**看得到規則有沒有生效**。但 OpenCode 端的對話輸出 CodeTrail 看不到,所以 `hit_count` 不會自動累計 —— 欄位保留給人工判斷:在對話裡看到模型標註了某條,想留下紀錄就 `python lessons.py hit L-003`。複審時 `hit_count` / `last_triggered` 是「這條還有沒有用」的參考,不是自動 decay 的依據(本機制刻意不做自動 decay)。

## 驗證注入有生效

1. `aicode` 啟動輸出應有一行 `[lessons] N 條 active lessons 已注入 .codetrail/lessons.md`。
2. 開新 session 問模型:「目前 context 裡有哪些 CodeTrail lessons?」它應能列出編號與內容。
3. 改用 `cat <AICODE_ROOT>/.codetrail/lessons.md` 直接看注入內容(此檔自動產生,勿手改;`.codetrail/` 已在 .gitignore)。

注意:寫入當下的 session 其 context 已載入完成,新 lesson 於**下一個** session 才注入(tool 回覆會提醒模型本 session 先直接遵守)。
