# 用真實對話 session 比較主聊天模型

這條 lane 回答的是：「哪顆本地模型比較能完成我的真實工作？」它不把任何歷史
assistant 回答當成標準答案，也不會在 `aicode` 啟動或 CI 中自動執行。

## 資料與評分原則

- 歷史 session 只提供真實問題分布、使用者補充的證據與弱失敗訊號。
- `expected_answer`、`gold_answer`、`reference_answer` 等欄位在 suite schema 中被明確拒絕。
- 正確性只來自 source/tool evidence、repo state、外部 build/硬體結果或匿名人工 A/B。
- 沒有可靠 oracle 的題必須標 `human_pairwise` 或 `unscored`；「session 結束」不算成功。
- 每顆候選模型跑相同 suite。bundle 前會驗證 suite digest 與逐題 project-state digest
  一致，不同現場的回答不能混成一次 A/B。

所有原始 export、NDA prompt、模型回答與盲測 key 預設放在：

```text
<CODETRAIL_REPO>/.codetrail/session_eval/
```

`.codetrail/` 已被 git ignore。writer 另外把目錄收成 `0700`、檔案收成 `0600`，拒絕
既有 symlink；不要把產物另複製到 repo 內可追蹤的位置。

## 1. 匯出選定 session

session id 可由 `aicode` 的 `/sessions`、`codetrail_chat.py sessions` 或
`python3 scripts/session_eval.py` 的輸出取得。匯出是明示動作,只讀你點名的那幾個
session 檔,不會掃整個 session 目錄:

```bash
python3 scripts/session_eval.py export \
  --session <SESSION_ID_1> --session <SESSION_ID_2>
```

原始 export 必然含歷史 assistant 回答，只作來源封存。需要拿去分享時才使用
`--sanitize`；sanitized export 可能失去建立 verifier 所需的 file/tool evidence。

已有一個私人 export 目錄時，可以直接進下一步：

```bash
python3 scripts/session_eval.py mine \
  --source-dir /private/session_exports --glob '*.json'
```

`drafts.json` 只保留 user text。短句糾正（例如只要求「真的用工具」）變成 failure signal；
可以安全去掉直接責備前綴的句子會產生中性 evidence-update 草稿。任何可能依賴舊回答的
指代仍標 `manual_required=true`，不會假裝已自動理解。

## 2. 人工收斂 episode

curated suite 的必要形狀：

```json
{
  "schema_version": 1,
  "name": "private_real_work_v1",
  "source_policy": "user_and_external_evidence_only",
  "cases": [
    {
      "id": "code_target_core",
      "task_type": "code_qa",
      "project_root": "/absolute/private/project",
      "source": {
        "session_hash": "0123456789abcdef",
        "export_digest": "<64 hex>",
        "user_turn_indices": [0]
      },
      "turns": [{"kind": "prompt", "text": "問題文字"}],
      "read_only": true,
      "state_paths": ["relative/file/used/by/the/case"],
      "verifier": {
        "oracle_kind": "human_pairwise",
        "checks": [
          {"type": "terminal"},
          {"type": "required_any_tool", "values": ["read_file", "grep_code"]},
          {"type": "max_identical_tool_call", "value": 2},
          {"type": "no_tool_error"}
        ],
        "human_dimensions": ["結論正確", "證據充分", "沒有忽略限制"]
      }
    }
  ]
}
```

後續 user turn 必須改寫成不依賴舊 assistant 的 `evidence`／`constraint`。例如
「你前面位址算錯了」不能原樣 replay；應改成「新增證據：host address 的位數與轉換規則為…」。
這個中性更新會依序送給每顆候選模型，用來測 context 保持與修正能力。

先做純驗證（不連模型）：

```bash
python3 scripts/session_eval.py validate \
  --suite .codetrail/session_eval/suite.json
```

## 3. Verifier

支援的 deterministic checks：

- `terminal`
- `required_tool` / `required_any_tool` / `forbidden_tool`
- `required_text_all` / `required_text_any` / `forbidden_text`
- `max_identical_tool_call`
- `no_tool_error`

文字 marker 必須來自獨立 source／外部 outcome，不可從舊模型答案抄成 gold。複雜 firmware
診斷、信件與摘要通常保留 `human_pairwise`；auto checks 只守格式、工具與明確事實，不冒充
語意正確率。

## 4. 對候選模型 replay

runner 不會替操作者停／啟 server。先載入候選 GGUF，確認四個 llama-server ready，再跑：

```bash
python3 scripts/session_eval.py run \
  --suite .codetrail/session_eval/suite.json \
  --candidate-label candidate_1 \
  --model <BARE_MODEL> \
  --resume
```

每次 run 都會：

1. 從 `/props` 核對目前載入的 GGUF 路徑與指定 candidate；不一致直接拒絕。
2. 使用相同 suite、tools、n_ctx 與 production sampling。壓縮語意由 eval **自己**釘死
   (臨時 `client.json`:預設 `off`,`--keep-compaction` 才是 `codetrail`),不讀你的
   `~/.config/codetrail/client.json` —— 否則同一份 suite 在兩台機器上量到的不是同一件事。
3. 客戶端走 `--policy readonly`(判準是 `readOnlyHint`,不是寫死名單),MCP server 端另設
   `mcp_server --readonly`(一個 argv 旗標,同時關掉寫入、執行、build 命令與 context metrics)。
4. 每題前後比對 Git 狀態、diff 與 `state_paths` 內容 digest;`.codetrail/`、
   `knowledge.json`、`.aicode_uploads/` 這三個被 gitignore 的路徑**一律**納入(它們正是
   唯讀 replay 最可能被寫到的地方)。read-only replay 改到現場即失敗。
5. 只保存 assistant text、completed tool 名稱／參數 digest、token、latency 與 verifier 結果；
   tool output 不落盤。單輪 case 完全不落 session 檔;多輪 case 必須落檔(模型要看得到上一
   輪),跑完就刪除。
6. 每完成一題就原子覆寫 `result-<candidate>.json` checkpoint；`--resume` 會重新核對
   suite digest、live model fingerprint、case 順序與每個已完成 case 的 project-state digest。
   任一項不同就拒絕續跑，不能把隔天改過的 tree 混進同一候選結果。
7. 單題超時是該模型在固定 SLA 下的失敗，不是整包消失：runner 解析有界的 partial JSON
   stream、標記 `timed_out=true`／`harness_error=true`、清掉暫存 session,checkpoint 後繼續
   下一題。private stderr 與 partial tool output 都不寫入結果。

第一次使用某個 `candidate-label` 時可省略 `--resume`；若同名結果已存在，runner 會拒絕
覆寫。續跑必須明示 `--resume`。兩個候選應使用相同的 `--turn-timeout`，timeout 不可因
看到某顆模型較慢才臨時放寬。

兩顆模型都跑完後建立盲測包：

```bash
python3 scripts/session_eval.py bundle \
  --suite .codetrail/session_eval/suite.json \
  --left .codetrail/session_eval/runs/result-candidate_1.json \
  --right .codetrail/session_eval/runs/result-candidate_2.json
```

使用者填 `review/review.json` 的 `choice`：`a`、`b`、`tie` 或 `both_bad`。閱讀用的
`review.md` 不含模型名；`review-key.json` 是 sealed mapping，完成評分前不要開。

## 邊界

- 這個 runner 只支援 read-only replay；code-change 題要另做隔離 snapshot executor，不能拿
  真專案當沙盒。
- 不自動把 LLM-as-judge 分數當 promotion gate。
- 不在同一個正式 session 中途混用模型。需要救援比較時 fork 成兩個分支，分開歸因。
- `--skip-aux-preflight` 只適用保證不會用 Code-RAG／RAG／VL 的 suite；正常真實題不要跳。
