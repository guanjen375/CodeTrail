# CodeTrail 開發者備忘

這份文件說明 OpenCode 日常使用以外的開發者基礎設施。專案首頁主要看 `README.md`；
AI agent 改 repo 前看 `AGENTS.md`（安全紅線與禁止事項）；這裡只放維護命令與內部工具。

---

## 常用命令

```bash
# 本地驗證（不需要 llama-server）
python -m compileall -q .
python scripts/run_tests.py
python scripts/check_eval_consistency.py
python scripts/check_readme_consistency.py
AICODE_MODEL=test-model:latest python scripts/doctor.py --no-network
python deployment_profile.py validate

# Lint（advisory，CI 不擋）
ruff check tests scripts
```

核心日常入口是 OpenCode TUI；跨機 web 另有薄 launcher（兩者共用 `aicode` 安全前置）：

```bash
cd <PROJECT_TO_ANALYZE>
aicode
aicode_web  # A/B 機已加入同一 tailnet 時
```

---

## 加新功能的標準流程

1. 改程式碼。
2. 新加或更新 tests（至少：MCP smoke、安全邏輯、edge case）。新功能 → 新 test 檔；不要塞舊 test 檔。
3. 跑 `python scripts/run_tests.py`、`python scripts/check_eval_consistency.py`、
   `python -m compileall -q .` — 三個都過才送 PR / 提交。
4. 如果改了 `config.py` / MCP tool schema / `README.md`，再跑一次 eval / readme consistency。

---

## 測試指南

- `tests/test_cli.py` — 維護用腳本 help / error path smoke test
- `tests/test_config.py` — config 數值的範圍與型別 sanity
- `tests/test_sandbox.py` — `_safe_path` 不會被 `..` / 絕對路徑 / symlink 騙過
- `tests/test_patch.py` — apply_patch 的 happy path、逃逸、context 不符、max 限制
- `tests/test_run_command.py` — 白名單 + shell 元字元 + 注入防護
- `tests/test_eval_consistency.py` — eval ↔ config / source 不漂移
- `tests/test_readme_consistency.py` — README / docs ↔ mcp_server.py / config.py 不漂移
- `tests/test_doctor.py` — doctor 各 check 的 happy / fail / skip 路徑(含 context/offload)
- `tests/test_context_budget.py` — token 估算、hard gate、metrics 解析、telemetry 隱私
- `tests/test_trim.py` — per-tool trim 策略、`[CTX_TRIMMED]`/`[TOOL_SUMMARY]` 標記、優先級
- `tests/test_external_import.py` — `import_external_file` 白名單、副檔名、大小限制
- `tests/test_mcp_root_safety.py` — MCP 啟動拒絕 `/` 或 `$HOME` 當 root
- `tests/test_mcp_smoke.py` — MCP server stdio 啟動與基本 tool 呼叫
- `tests/test_tool_call_canary.py` — `aicode` 的兩層工具健檢：18-tool contract、假 XML 拒絕、completed event、設定指紋／24h cache、retry/FLAKY/fail-loud；所有 OpenCode／MCP／HTTP／LLM 路徑都 mock，pytest 絕不呼叫真模型
- `tests/test_lessons.py` — lessons(行為教訓)store 驗證 fail-loud、20 條上限、scope/review_by 過濾、render 注入、過期停注入+複審提示、管理 CLI 與完整生命週期;純檔案系統,離線
- `tests/test_web_server_scripts.py` — `aicode_web` 的 Tailscale IPv4 鎖定、headless tmux launcher、前景 preflight 擋下、參數防繞過與 stop/help smoke；Tailscale / OpenCode 不連真服務
- `tests/test_gpu_safety.py` — `gpu_safety.py` 的 server /props 觀測、SafetyVerdict 分支;完全離線(nvidia-smi 與 llama-server HTTP 都用 hook 注入 fixture)
- `tests/test_resolve_server_ctx.py` — `/props` n_ctx 自動跟隨；server 缺席／例外時 non-blocking fallback
- `tests/test_check_status_script.py` — `check_status.py` 的 nvidia-smi process 計數、跨 GPU PID 去重、report-only / strict exit code;nvidia-smi 完全用 stub
- `tests/test_deployment_profile.py` — profile schema/precedence、惡意值拒絕、registry/mmproj、main/aux GPU precedence、`-ngl auto --fit` 參數驗證
- `tests/test_deployment_status.py` — port/cmdline role 辨識、錯卡/錯模型;process 與 HTTP 都用 hook
- `tests/test_profile_server_launchers.py` — launch_servers / stop_servers 各 `--scope` 的離線 dry-run 相容性(含壞設定檔時 stop 退路)
- `tests/test_set_config.py` — `set_config.sh` 的前置檢查(llama-server/依賴缺失通知)、GPU/模型偵測分類、shard 齊全性、mmproj 多重配對、純問答契約(使用者選擇題無預設值、reranker internal buffer 不提問、選項外輸入重問、`--yes` 缺旗標指名報錯、超大配置不被容量擋下)、start.sh 結尾 nvidia-smi 提醒、VL 不自動當 main、摘要確認/離開、opencode.json 合併、bind 安全預設、legacy env 清除、transaction restore 與 end-to-end dry-run;I/O 全用 fixture
- `tests/test_set_config_cpu_moe.py` — CPU-MoE 只問層數(MoE 才詢問、dense 印原因略過、dense 給非 0 旗標要報錯)、GGUF expert tensor 解析(含 per-layer 編號與 split shard)、choose_cpu_moe_layers 純輸入驗證(0-1024;0=不 offload、≥ 層數上限=全放 RAM、build 缺旗標時的降級)、build_main_parameters 參數組裝、`cpu_moe`/`n_cpu_moe` 的 main+vl schema/argv(embedding/reranker 拒絕);完全離線且大檔用 sparse fixture
- `tests/test_launch_rollback.py` — launcher 啟動失敗的 rollback(pipe-pane 持久 log、清理本次 tmux session、`AICODE_NO_ROLLBACK`)與依模型大小放大的 health timeout;tmux 用 monkeypatch

---

## 改 config / docs / eval 時要同步檢查

`config.py`、`README.md`、`docs/*.md`、`eval/*.json`、`mcp_server.py` 的工具清單必須對齊。
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

`eval/` 是固定題庫與離線回歸評測，不會記錄使用者對話，也不會被 OpenCode/MCP runtime 自動使用。

主要檔案：

- `eval/run_eval.py`：手動評測 runner，會呼叫模型，適合調 RAG / agent / prompt 後做回歸。
- `eval/run_retrieval_eval.py`：完全離線的 retrieval-only gate；只跑 `_hybrid_search`，
  query/chunk embedding 從 checked-in fixture cache 讀取，cache miss 直接失敗，不呼叫四台 server。
- `eval/retrieval_fixture.json`、`eval/retrieval_embedding_cache.json`：30 個 NDA-safe 合成
  register facts，展開成 92 個可回答題（62 個數值/hex/version）+ 5 個拒答題。
- `eval/spec_questions.json`、`eval/spec_holdout.json`、`eval/spec_adversarial.json`：規格/RAG 題庫。
- `eval/code_questions.json`：程式碼定位題庫。
- `eval/bug_questions.json`：bug 類問題題庫。
- `scripts/check_eval_consistency.py`：不跑 LLM，只檢查 eval expected 是否和 `config.py` / source code 漂移。
- `tests/test_eval_consistency.py`：把 consistency check 接進 pytest。

常用命令：

```bash
python scripts/check_eval_consistency.py
python scripts/run_tests.py tests/test_eval_consistency.py
python eval/run_retrieval_eval.py
python eval/run_eval.py --test-set all --verbose
```

前三個命令不需要 llama-server；retrieval runner 固定回報 Recall@5、MRR、nDCG@5 與
數值證據精確率。加 `--predictions <json>` 時才另外計算 citation entailment、數值答案
精確率、拒答率/拒答正確率。`eval/run_eval.py` 才需要本機 4 個 llama-server 與對應 GGUF。

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

CodeTrail 自己對 llama-server `/completion` 與 `/v1/chat/completions` 發送的每一
個 prompt 都會先經過 `context_budget` 的「估算 → soft warn → hard refuse →
telemetry」流程。OpenCode TUI 也走 `/v1/chat/completions` 但走的是它自己的 client
(`@ai-sdk/openai-compatible`),**不會** 經過這個模組,所以它的 context 仍然要靠
llama-server 啟動時 `-c <N>` 與 OpenCode `model.limit.context` 對齊。`scripts/doctor.py`
只掃描、絕不寫檔；正常 `aicode` preflight 則會針對 active model 原子同步這個鏡像欄位並留備份。

### 模組分工

| 模組 | 責任 |
|---|---|
| `context_budget.py` | token 估算(prompt / messages parts / tools schema)、`ContextUsage` dataclass、hard gate (`enforce_gate` → `ContextOverflowError`)、llama-server usage metrics 解析(支援 native `tokens_evaluated/tokens_predicted` 與 OpenAI `usage{}`,streaming + non-streaming)、JSONL telemetry。**不寫 prompt / 檔案內容** 進 log,只寫 count + metadata。 |
| `trim.py` | 對 `role=tool` 訊息做 priority-aware trim,加入明確 `[CTX_TRIMMED]` / `[TOOL_SUMMARY]` 標記。`role=system` / `role=user` 訊息**完全不動**(REF metadata 因此被保留)。run_command 保留 tail + error line;read_file 保留 header + window;舊輪 tool output 摘要成 deterministic facts(file:line 錨點、error 行)。 |
| `llama_client.py` | 對 llama-server 4 個端點的薄 HTTP wrapper:`/completion` / `/v1/chat/completions` / `/embedding` / `/reranking` / `/props` / `/slots` / `/health`。stream / non-stream 雙模式,native / OpenAI usage 萃取統一接口。 |
| `utils.py` / `agent.py` 內呼叫點 | 在送 server 前 `context_budget.build_usage(...)` → 觸發 soft 時 `_pre_send_trim_if_needed(...)` → `enforce_gate(...)` → 走 `llama_client.native_completion(...)` 或 `chat_completions(...)` → `parse_usage_from_response(...)` → `log_metrics(...)`。 |

### Telemetry 隱私政策

`.codetrail/context_metrics.jsonl` 每行 metadata:`model`、`source`、`requested/effective num_ctx`、估算的 input/output token、`utilization_pct`、`did_trim` + `trim_summary` (counts only)、`actual_prompt_eval_count`、`actual_eval_count`、`prompt_tokens_per_second`、`output_tokens_per_second`、`error_type`、`timestamp`。

**絕不寫入**: 完整 prompt、tool output、檔案內容、user question 文字。
`trim.py` 回的 `TrimSummary.to_dict()` 也只是 count 與 action label。
`tests/test_context_budget.py::test_log_writes_metadata_only_no_prompt` 與
`tests/test_trim.py::test_trim_messages_emits_telemetry_metadata_only` 是
強制這條 invariant 的 fail-fast 測試。

`*.jsonl` 已在 `.gitignore`;`.codetrail/` 目錄也另外列出。

### 加新的 LLM call site 時怎麼接 gate

任何新增的 `llama_client.native_completion(...)` 或 `chat_completions(...)`,**送出前** 都要:

```python
import context_budget
import config
import llama_client
from config import LLAMA_BASE_URL

model = config.require_main_model()

try:
    usage = context_budget.check_and_log(
        source="my_new_call_site",  # 任意短字串標記,給 telemetry 看
        requested_num_ctx=num_ctx,
        prompt=prompt,              # 或 messages=messages, tools=tools
        model=model,
    )
except context_budget.ContextOverflowError as exc:
    return str(exc)                 # 訊息已包含 [CTX_OVERFLOW] + how-to-fix

# Non-streaming:
data = llama_client.native_completion(base_url=LLAMA_BASE_URL, prompt=prompt, ...)
context_budget.parse_usage_from_response(data, usage)

# Streaming: 每個 chunk 都呼叫(只在最終 chunk 抓到 metrics):
for chunk in llama_client.native_completion(..., stream=True):
    context_budget.parse_usage_from_stream_chunk(chunk, usage)

context_budget.emit_post_call_line(usage)
context_budget.log_metrics(usage)
```

如果你的 call site 也會累積 messages(像 agent loop),記得也接 `_pre_send_trim_if_needed`(或自己呼 `trim.trim_messages`)以便 soft warning 觸發時可以自動降載,而不是直接 hard refuse。低風險 / 一次性 prompt(如 RAG embedding query 之類)可以省略 trim,但**不能省略 gate**。

新增主模型 call site 時,必須在送出前用 call-time `config.require_main_model()` 取值;不要使用 import-time `config.MODEL` 或 `from config import MODEL` 當 runtime model source。

---

## gpu_safety.py / ctx_safety_check.py 設計

`context_budget.py` 守的是「prompt 會不會超出 ctx 上限」(正確性);
`gpu_safety.py` 守的是「使用者要求的 ctx 上限會不會超過 llama-server 啟動時的 `-c <N>`」
(會被 server 端 truncation)。兩者不重疊；容量閘只拒絕 `requested > server n_ctx`，較小值雖未用滿容量但不會截斷。

llama-server 啟動時 `-c <N>` 已經把 ctx + KV cache 鎖死,所以 doctor / safety check
**不再做 VRAM / weights / KV cache 預測計算** — 改成「server 自己說 n_ctx 是多少」
這個 ground truth 觀測。如果 server 啟動 OOM 那是 server 自己會崩,不用我們預測。

### 模組分工

| 模組 / 入口 | 責任 |
|---|---|
| `gpu_safety.py` | 純 library:`query_gpu_info()` 跑 nvidia-smi 拿 GPU info(純診斷)、`query_server_info()` 打 llama-server `/props` 抓 `default_generation_settings.n_ctx` + `model_path`、`check_safety(requested_ctx, base_url)` 比對後包成 `SafetyVerdict`。所有 I/O 都用 hook 參數注入,測試可完全離線 mock。 |
| `n_ctx.py` / `config.py::N_CTX` | 主模型 n_ctx 的集中解析。正常設定入口是 `set_config.sh --ctx`；runtime 以 `AICODE_N_CTX` 傳遞 server 實值。`NUM_CTX` / `DYNAMIC_NUM_CTX_MAX` 只保留程式碼相容 alias，永遠等於 `N_CTX`。舊 `AICODE_DYNAMIC_NUM_CTX_MAX` 只暫時相容讀取並警告 deprecated。 |
| `scripts/resolve_server_ctx.py` | CLI 取值器。讀主 llama-server `/props` 拿真實 `n_ctx`，只把整數印到 stdout(讀不到就印空字串、永遠 exit 0)。`aicode` 將實值 export 成 `AICODE_N_CTX`；讀不到時回到 deployment profile 的 `services.main.ctx`。 |
| `scripts/ctx_safety_check.py` | CLI 入口(容量閘)。讀 `AICODE_MODEL` / 主 n_ctx / `AICODE_LLAMA_BASE_URL`，呼 `gpu_safety.check_safety()`；requested `<=` server n_ctx 放行，只有 `>` 才 refuse。安全 gate、`AICODE_ACCEPT_CTX_RISK` 與 `AICODE_CTX_SAFETY_DISABLE` 仍保留。 |
| `opencode_context.py` / `scripts/opencode_ctx_check.py` | 解析 OpenCode active model 的 `provider.*.models.*.limit.context`。純檢查模式不寫檔；`aicode` 使用 `--fix`，只同步 active model 的該欄、保留其他 JSON、原子替換並建立 `.codetrail.bak`。無法唯一定位、解析或寫入時 fail-loud；`AICODE_ACCEPT_CTX_RISK=1` 可維持不一致而不寫入。 |
| `scripts/opencode_mcp_timeout_check.py` | OpenCode MCP client timeout 契約。純檢查模式供診斷；`aicode` 使用 `--fix`，只在既有 `mcp.codetrail` entry 內將缺漏、無效或過短的 `timeout` 提升到 `config.OPENCODE_MCP_TIMEOUT_MIN_MS`。修復會保留其他 JSON 欄位、原子替換並建立 `.codetrail.bak`；設定無法解析/寫入則 fail-loud。 |
| `context_budget.py::_emit_runtime_offload_check_once` | runtime 觀測 hook:`[CTX] WARNING` 或 `[CTX_OVERFLOW]` 觸發時順手查一次 `/slots` + `/props`,把 server 真實 n_ctx / 忙碌 slot 數 黏在 log 後面。每個 process 只跑一次,任何錯誤靜默吞掉。 |

### 設計守則

- **fail-loud,不偷偷 clamp**:`ctx_safety_check` 遇到 `UNSAFE`(requested > server)一定 print verdict + 對齊方案然後 `exit 2`,**不會為了避開 UNSAFE 自動把 requested 改小**。(這跟 aicode 啟動時「從 server 讀 n_ctx 自動設成 budget」是兩回事:後者是拿 source of truth 當預設值,不是為了掩蓋失敗而 clamp。)
- **UNKNOWN 一律放行**:server 不可連 / `/props` 沒給 n_ctx → 只 warn 不擋。否則 CI、遠端 server、新版 server 改 schema 時會被卡住。
- **server 是 source of truth**:不再做 KV cache 公式預測;server `-c` 就是答案。

### 進階 / escape 設定

| Env | 行為 | 何時用 |
|---|---|---|
| `AICODE_N_CTX=<N>` | 單次覆寫主 n_ctx；仍須通過 server capacity gate | 測試 / 特殊 launcher；正常使用改跑 `set_config.sh` |
| 重跑 `set_config.sh` 並重啟 server | 更新主 n_ctx | 一般使用者唯一需要的設定方式 |
| `AICODE_ACCEPT_CTX_RISK=1` | UNSAFE 也 exit 0,但仍印完整 verdict | 一次性實測 truncation 影響 |
| `AICODE_CTX_SAFETY_DISABLE=1` | 整個 check 跳過,連 verdict 都不算 | CI / 自動化、緊急逃生 |

`AICODE_DYNAMIC_NUM_CTX_MAX` 與 `AICODE_NUM_CTX` 已 deprecated；不要再寫進 shell profile。

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
- `mcp_server.py` 裡 `_record_kb_interaction` 接線
- `README.md`、`README_DEV.md` 裡的資料飛輪說明

目前建議先保留：它們不影響 OpenCode 日常使用，但對之後把工具做成更成熟的私有產品有價值。
