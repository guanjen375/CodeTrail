# CodeTrail 開發者備忘

這份文件說明 OpenCode 日常使用以外的開發者基礎設施。專案首頁主要看 `README.md`；
AI agent 改 repo 前看 `AGENTS.md`（安全紅線與禁止事項）；這裡只放維護命令與內部工具。

---

## 常用命令

```bash
# Help（永遠不該 crash 或進互動模式）
python main.py --help

# 本地驗證（不需要 Ollama）
python -m compileall -q .
python scripts/run_tests.py
python scripts/check_eval_consistency.py
python scripts/check_readme_consistency.py
python scripts/doctor.py

# Lint（advisory，CI 不擋）
ruff check tests scripts

# 真的跑 QA / agent（需要 Ollama 在 localhost:11434）
python main.py --qa "解釋 segfault"
python main.py /path/to/repo "整體架構"
```

---

## 加新功能的標準流程

1. 改程式碼。
2. 新加或更新 tests（至少：CLI smoke、安全邏輯、edge case）。新功能 → 新 test 檔；不要塞舊 test 檔。
3. 跑 `python scripts/run_tests.py`、`python scripts/check_eval_consistency.py`、
   `python -m compileall -q .` — 三個都過才送 PR / 提交。
4. 如果改了 `config.py` / MCP tool schema / `README.md`，再跑一次 eval / readme consistency。

---

## 測試指南

- `tests/test_cli.py` — CLI 不該 crash 的最低保證
- `tests/test_config.py` — config 數值的範圍與型別 sanity
- `tests/test_sandbox.py` — `_safe_path` 不會被 `..` / 絕對路徑 / symlink 騙過
- `tests/test_patch.py` — apply_patch 的 happy path、逃逸、context 不符、max 限制
- `tests/test_run_command.py` — 白名單 + shell 元字元 + 注入防護
- `tests/test_eval_consistency.py` — eval ↔ config / source 不漂移
- `tests/test_readme_consistency.py` — README ↔ mcp_server.py / config.py 不漂移
- `tests/test_doctor.py` — doctor 各 check 的 happy / fail / skip 路徑(含 context/offload)
- `tests/test_context_budget.py` — token 估算、hard gate、metrics 解析、telemetry 隱私
- `tests/test_trim.py` — per-tool trim 策略、`[CTX_TRIMMED]`/`[TOOL_SUMMARY]` 標記、優先級
- `tests/test_external_import.py` — `import_external_file` 白名單、副檔名、大小限制
- `tests/test_mcp_root_safety.py` — MCP 啟動拒絕 `/` 或 `$HOME` 當 root
- `tests/test_mcp_smoke.py` — MCP server stdio 啟動與基本 tool 呼叫

---

## 改 config / docs / eval 時要同步檢查

`config.py`、`README.md`、`eval/*.json`、`mcp_server.py` 的工具清單必須對齊。
改這些任一處都要跑：

```bash
python scripts/check_eval_consistency.py
python scripts/check_readme_consistency.py
python scripts/run_tests.py tests/test_eval_consistency.py tests/test_readme_consistency.py
```

漂移範例（已修，避免再犯）：
- 改 `RERANKER_TOP_N` → 對應的 `eval/spec_holdout.json` gold_evidence 也要改
- `_parse_unified_diff` 從 `agent.py` 搬到 `agent_tools.py` → `eval/code_questions.json` 的 `file` 要改
- 換 `EMBEDDING_MODEL` → `eval/spec_adversarial.json` 也要改

如果你在 eval 裡放 line number，**只當作 hint，誤差 ±20 行內視為正確**；
不要把 line number 當成嚴格契約。

---

## eval 是什麼

`eval/` 是固定題庫與離線回歸評測，不會記錄使用者對話，也不會被 `main.py` 或 OpenCode/MCP runtime 自動使用。

主要檔案：

- `eval/run_eval.py`：手動評測 runner，會呼叫模型，適合調 RAG / agent / prompt 後做回歸。
- `eval/spec_questions.json`、`eval/spec_holdout.json`、`eval/spec_adversarial.json`：規格/RAG 題庫。
- `eval/code_questions.json`：程式碼定位題庫。
- `eval/bug_questions.json`：bug 類問題題庫。
- `scripts/check_eval_consistency.py`：不跑 LLM，只檢查 eval expected 是否和 `config.py` / source code 漂移。
- `tests/test_eval_consistency.py`：把 consistency check 接進 pytest。

常用命令：

```bash
python scripts/check_eval_consistency.py
python scripts/run_tests.py tests/test_eval_consistency.py
python eval/run_eval.py --test-set all --verbose
```

前兩個命令不需要 Ollama；`eval/run_eval.py` 需要本機 Ollama 與相關模型。

---

## data flywheel 是什麼

`data_flywheel.py` 才是互動資料收集器。它預設關閉，只有設定環境變數才會寫資料：

```bash
AI_CODE_COLLECT_DATA=1 aicode
```

預設輸出：

```text
data/interactions.jsonl
```

記錄內容包含 question、answer、refs、code snippets、mode、KB score、repo commit、model tag、agent tool calls、files read。這些資料在 NDA 場景通常含敏感內容，已由 `.gitignore` 排除。

OpenCode/MCP server 端只記 KB-shaped tools：

- `query_knowledge`
- `query_knowledge_strict`
- `code_rag_search`

一般 plumbing tools，例如 `read_file`、`grep_code`、`apply_patch`，不會在 MCP 端逐一記完整對話。

常用命令：

```bash
python data_flywheel.py stats
python data_flywheel.py rate --file data/interactions.jsonl
python data_flywheel.py export --file data/interactions.jsonl --output data/training.jsonl
```

---

## 兩者差異

| 項目 | eval | data flywheel |
|---|---|---|
| 會自動記錄對話 | 不會 | 會，但必須設 `AI_CODE_COLLECT_DATA=1` |
| 用途 | 固定題庫回歸測試 | 收集真實互動樣本 |
| 日常 OpenCode 是否需要 | 不需要 | 不需要 |
| 是否適合成熟產品 | 適合做 regression gate | 適合做資料閉環，但要更嚴格處理隱私 |

---

## context_budget.py / trim.py 設計

CodeTrail 自己對 Ollama native `/api/generate` 與 `/api/chat` 發送的每一個 prompt
都會先經過 `context_budget` 的「估算 → soft warn → hard refuse → telemetry」流程。
OpenCode TUI 走 `/v1` OpenAI-compatible API,**不會** 經過這個模組,所以它的 context
仍然要靠 Ollama server 的 `OLLAMA_CONTEXT_LENGTH` 與 OpenCode `model.limit.context`
對齊。`scripts/doctor.py` 會掃這兩條管線是否打架,但不會自動改 OpenCode 的設定。

### 模組分工

| 模組 | 責任 |
|---|---|
| `context_budget.py` | token 估算(prompt / messages parts / tools schema)、`ContextUsage` dataclass、hard gate (`enforce_gate` → `ContextOverflowError`)、Ollama usage metrics 解析(streaming + non-streaming)、JSONL telemetry。**不寫 prompt / 檔案內容** 進 log,只寫 count + metadata。 |
| `trim.py` | 對 `role=tool` 訊息做 priority-aware trim,加入明確 `[CTX_TRIMMED]` / `[TOOL_SUMMARY]` 標記。`role=system` / `role=user` 訊息**完全不動**(REF metadata 因此被保留)。run_command 保留 tail + error line;read_file 保留 header + window;舊輪 tool output 摘要成 deterministic facts(file:line 錨點、error 行)。 |
| `utils.py` / `agent.py` 內呼叫點 | 在送 Ollama 前 `context_budget.build_usage(...)` → 觸發 soft 時 `_pre_send_trim_if_needed(...)` → `enforce_gate(...)` → 送出 → `parse_usage_from_response(...)` → `log_metrics(...)`。 |

### Telemetry 隱私政策

`.codetrail/context_metrics.jsonl` 每行 metadata:`model`、`source`、`requested/effective num_ctx`、估算的 input/output token、`utilization_pct`、`did_trim` + `trim_summary` (counts only)、`actual_prompt_eval_count`、`actual_eval_count`、`prompt_tokens_per_second`、`output_tokens_per_second`、`error_type`、`timestamp`。

**絕不寫入**: 完整 prompt、tool output、檔案內容、user question 文字。
`trim.py` 回的 `TrimSummary.to_dict()` 也只是 count 與 action label。
`tests/test_context_budget.py::test_log_writes_metadata_only_no_prompt` 與
`tests/test_trim.py::test_trim_messages_emits_telemetry_metadata_only` 是
強制這條 invariant 的 fail-fast 測試。

`*.jsonl` 已在 `.gitignore`;`.codetrail/` 目錄也另外列出。

### 加新的 LLM call site 時怎麼接 gate

任何新增的 `requests.post(OLLAMA_GENERATE_URL, ...)` 或 `requests.post(OLLAMA_CHAT_URL, ...)`,**送出前** 都要:

```python
import context_budget

try:
    usage = context_budget.check_and_log(
        source="my_new_call_site",  # 用任意短字串標記,給 telemetry 看
        requested_num_ctx=num_ctx,
        prompt=prompt,            # 或 messages=messages, tools=tools
        model=MODEL,
    )
except context_budget.ContextOverflowError as exc:
    return str(exc)               # 訊息已經包含 [CTX_OVERFLOW] + how-to-fix

# ... 送 request ...

# Non-streaming:
context_budget.parse_usage_from_response(resp.json(), usage)

# Streaming: 每個 chunk 都呼叫(只在 done=true 時抓到 metrics):
context_budget.parse_usage_from_stream_chunk(chunk, usage)

context_budget.emit_post_call_line(usage)
context_budget.log_metrics(usage)
```

如果你的 call site 也會累積 messages(像 agent loop),記得也接 `_pre_send_trim_if_needed`(或自己呼 `trim.trim_messages`)以便 soft warning 觸發時可以自動降載,而不是直接 hard refuse。低風險 / 一次性 prompt(如 RAG embedding query 之類)可以省略 trim,但**不能省略 gate**。

### 沒有解的事(刻意留)

- 估算還是 `CHARS_PER_TOKEN` heuristic。`actual_prompt_eval_count` 已蒐集,之後可以做 per-model 校正,但這次不引入 tokenizer 依賴。
- `code_rag.py` / `knowledge.py` / `media.py` 內的 LLM call site 還沒接 gate;它們各自有 chunk 大小限制,通常不會吃滿 ctx,但若哪一天出 silent truncation 就要補。
- OpenCode TUI 主對話完全在 CodeTrail 視線外,doctor 只能驗 config 對齊,不能驗實際 prompt 是否爆。

---

## 可以刪嗎

可以，但要有系統地刪，不要只刪一半。

若刪 eval，至少同步處理：

- `eval/`
- `scripts/check_eval_consistency.py`
- `tests/test_eval_consistency.py`
- `tests/test_cli.py` 裡 `eval/run_eval.py --help` 的 smoke test
- `README.md`、`README_DEV.md` 裡的 eval 說明

若刪 data flywheel，至少同步處理：

- `data_flywheel.py`
- `main.py` 裡 `record_interaction` / `DATA_COLLECT_ENABLED` 接線
- `mcp_server.py` 裡 `_record_kb_interaction` 接線
- `README.md`、`README_DEV.md` 裡的資料飛輪說明

目前建議先保留：它們不影響 OpenCode 日常使用，但對之後把工具做成更成熟的私有產品有價值。
