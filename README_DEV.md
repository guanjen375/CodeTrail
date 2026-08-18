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
- `tests/test_mcp_root_safety.py` — `root_safety.validate_aicode_root` 拒絕 `/` / `$HOME` /不存在的 root,並守住 mcp_server 真的有接上去
- `tests/test_index_scope.py` — 索引範圍:三態走訪 ≡ `should_index_file` 的不變式、rescue 四測、Layer C loader fail-loud(schema/權限/pattern 衛生)、matcher 方言向量、symlink containment、快取 fingerprint 遷移、`index_stats` root 驗證;全部離線且用合成樹名
- `tests/test_mcp_smoke.py` — MCP server stdio 啟動與基本 tool 呼叫
- `tests/test_rerank_mmr_relevance.py` — MMR 不得蓋掉 cross-encoder 排序:rerank 分數當相關度、embedding 只算多樣性懲罰、min-max 正規化、半套 relevance 退回舊行為、跳過 rerank 時無分數;離線
- `tests/test_contextual_signals.py` — 雙訊號不變式:ctx 不進 evidence/REF/strict 來源、六個決策點逐一讀 gate、`_should_rerank` 三分支、merge 聚合 gate、lexical bypass 走 gate BM25、雙矩陣儲存與 required-schema 對照、旗標四象限;離線
- `tests/test_context_generation.py` — 生成端:loopback 雙棧判定、非 loopback 需顯式同意、3xx/HTTP 錯當傳輸失敗、環境 proxy 隔離、快取檔名/權限/symlink 逃逸、write-through 與零呼叫冪等、指紋涵蓋模型檔 size+mtime、single-writer 鎖、窗公式含 `RESERVED_OUTPUT_TOKENS`、map/reduce 路徑、空回應重試一次、覆蓋率閘;HTTP 全 mock,離線
- `tests/test_extracted_document.py` — `ExtractedDocument` 單一真相:章節 span 連續性、重複標題各自成節、chunk 的 char span 定位、PDF 頁 span/跨頁 page_range、「短頁被歸到上一頁章節」回歸、五個入口的 chunk 形狀一致;離線且用合成語料
- `tests/test_tool_call_canary.py` — `aicode` 的兩層工具健檢：18-tool contract、假 XML 拒絕、completed event、設定指紋／24h cache、retry/FLAKY/fail-loud；所有 OpenCode／MCP／HTTP／LLM 路徑都 mock，pytest 絕不呼叫真模型
- `tests/test_lessons.py` — lessons(行為教訓)store 驗證 fail-loud、20 條上限、scope/review_by 過濾、render 注入、過期停注入+複審提示、管理 CLI 與完整生命週期;純檔案系統,離線
- `tests/test_web_server_scripts.py` — `aicode_web` 的 Tailscale IPv4 鎖定、headless tmux launcher、前景 preflight 擋下、參數防繞過與 stop/help smoke；Tailscale / OpenCode 不連真服務
- `tests/test_gpu_safety.py` — `gpu_safety.py` 的 server /props 觀測、SafetyVerdict 分支;完全離線(nvidia-smi 與 llama-server HTTP 都用 hook 注入 fixture)
- `tests/test_resolve_server_ctx.py` — `/props` n_ctx 自動跟隨；server 缺席／例外時 non-blocking fallback
- `tests/test_check_status_script.py` — `check_status.py` 的 nvidia-smi process 計數、跨 GPU PID 去重、report-only / strict exit code;nvidia-smi 完全用 stub
- `tests/test_deployment_profile.py` — profile schema/precedence、惡意值拒絕、registry/mmproj、main/aux GPU precedence、`-ngl auto --fit` 參數驗證
- `tests/test_deployment_status.py` — port/cmdline role 辨識、錯卡/錯模型;process 與 HTTP 都用 hook
- `tests/test_profile_server_launchers.py` — launch_servers / stop_servers 各 `--scope` 的離線 dry-run 相容性(含壞設定檔時 stop 退路)
- `tests/test_set_config.py` — `set_config.sh` 的前置檢查(llama-server/依賴缺失通知)、GPU/模型偵測分類、shard 齊全性、mmproj 多重配對、純問答契約(使用者選擇題無預設值、四段式一角色一組、reranker internal buffer 必答且 `--yes` 需 `--rerank-ctx`、threads 不提問改 auto、範圍顯示用 `-`、選項外輸入重問、`--yes` 缺旗標指名報錯、超大配置不被容量擋下)、start.sh 結尾 nvidia-smi 提醒、VL 不自動當 main、摘要確認/離開、opencode.json 合併、bind 安全預設、legacy env 清除、transaction restore 與 end-to-end dry-run;I/O 全用 fixture
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
| `context_signals.py` | **檢索訊號的唯一定義**:embedding 組字(retrieval 含 ctx / gate 只看原文)、schema 名稱與 required 對照、內容雜湊、BM25 來源文本、reranker passage。寫入端(`RAG.py`)與載入端(`knowledge.py`)一律 import 這裡——以前兩邊各寫一份同樣的字串,差一個字就變成「內容雜湊不一致」。 |
| `context_generation.py` | chunk 級生成脈絡的產生器:窗策略(整份 / 階層式摘要 + target-centered section window)、prompt 版本、輸出衛生、write-through 快取與指紋、per-KB single-writer 鎖、覆蓋率閘、**專用的受限 HTTP client**(`trust_env=False`、拒絕 3xx、host 必須是 loopback)。 |
| `extracted_document.py` | 文件結構原語與 `ExtractedDocument`(raw_text / sections / chunks)。章節偵測、表格正規化、章節層級、行 offset 都在這裡;RAG.py 只 re-export。**章節與頁碼的單一真相**:chunk 的 `section` / `section_index` / `char_span` 一律由文件級走訪決定,不再由 splitter 的 page-local 追蹤加呼叫端 `last_section` 繼承拼湊。 |
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

## 索引範圍 (index scope)

**只影響 Code RAG 索引。`grep_code` / `list_dir` 完全不受影響** —— 檔案還是找得到、還是
grep 得到,只是不再吃掉語意檢索的名額。這條界線是刻意的:使用者以為檔案不見了比雜訊更糟。

### 不變式

> 檔案是否進索引,**由且僅由** `index_scope.IndexScope.should_index_file(rel)` 決定。
> `decide_dir()` 的三態(`PRUNE` / `TRAVERSE_ONLY` / `INDEX`)只是剪枝優化,必須保守:
> `PRUNE` 僅在「其下不可能存在任何能通過 `should_index_file` 的檔案」時才允許。

`tests/test_index_scope.py::test_tri_state_walk_matches_should_index_file` 對合成樹全量
枚舉逐檔求值當基準,再跑三態走訪比對 —— 任何 `PRUNE` 吃掉應索引檔案就立刻紅。改剪枝
邏輯時先看那條測試。

### 分層

| 層 | 內容 | 誰吃 |
|---|---|---|
| A | `config.IGNORED_DIRS`(19 名,**凍結**)+ dot 目錄規則 | 索引 / grep / list_dir |
| A′ | `config.INDEX_ONLY_IGNORED_DIRS` = `site-packages` / `dist-packages`;段精確、case-insensitive | 只有索引 |
| B | 結構偵測器,**永遠不收專案名**。B1 標記:目錄含 `pyvenv.cfg`、含 `conda-meta/`;B2 段規則:段 `^python\d+(\.\d+)*$` 且父段 ∈ {`lib`,`lib64`}、段以 `.egg-info` 結尾 | 只有索引 |
| C | 部署層 `~/.config/codetrail/index-scope.json`(見下) | 只有索引 |

規則鏈(對檔案路徑求值):hard gates 全過之後,才是
`C.include` > `C.exclude` > `B` > `A′` > 預設索引。hard gates 有七條,語意上全是 AND
(所以評估順序只影響 syscall 成本,不影響結果):dotfile / 索引產物檔名 →
`CODE_EXTENSIONS` → `should_ignore_file` → 祖先段命中 A → containment(realpath 仍在
root 內)→ 不是實際載入的 index-scope.json → 是 regular file。

最後兩條是 review 補的:設定檔要是被放進 root 就會自己進索引(`.json` 在
`CODE_EXTENSIONS` 裡),違反它「永不進 repo/輸出」的契約;而 FIFO / socket / device
一路讀下去會**永久阻塞**建索引(`read_bytes` 在 FIFO 上不會回來),`os.walk` 的
`filenames` 是會列出它們的。

刻意的限制,不要「好心修掉」:

- **A 命中不可用 `C.include` 救回。** 救回等於擴大 committed 行為的索引範圍,違反
  「只做減法」,而且會逼 `list_dir` / grep 連動。
- **root 自身不套 A′ / B。** 使用者的專案根剛好叫 `site-packages`、或根目錄放了
  `pyvenv.cfg`,整棵樹被剪掉是最糟的誤殺(設計原則:寧可漏排,不可誤殺)。

### index-scope.json (Layer C)

`~/.config/codetrail/index-scope.json`,**永不進 repo、永不出現在任何輸出**
(`index_stats` 連 pattern 內容都不印)。`AICODE_INDEX_SCOPE_FILE` 可覆寫路徑(測試用)。

```json
{
  "schema_version": 1,
  "roots": [
    {
      "root": "/abs/path/to/tree",
      "mode": "denylist",
      "detectors": true,
      "exclude": ["vendor_env/**"],
      "include": ["vendor_env/keep.c"]
    }
  ]
}
```

- **檔案不存在 = 正常預設**,不 fail-loud:絕大多數部署者一輩子不需要這個檔。
- **錯誤訊息永遠不含 pattern 內容。** pattern 就是樹狀結構本身
  (`nda_customer_x/...`),而 fatal 訊息會被貼進 issue。一律用
  `roots[i] 的 exclude[j]` 定位;壞 regex 連底層 `re.error` 的訊息都不能轉述
  (它會嵌入出錯的字元),`raise ... from None` 也是必要的,否則 exception chain
  照樣把它印出來。
- **檔案存在但壞掉 = fail-loud**:未知鍵、schema_version 不符、重複 selector、
  非絕對路徑 `root`、pattern 衛生違規(`!` / `..` 段 / 空 / NUL / 每 root >200 條 /
  單條 >512 字元)、POSIX 權限不是 owner-only(訊息附 `chmod 600`)。
- `root` 是**選擇器,不是掃描根**:canonicalize(realpath + normcase)後與當下的
  `AICODE_ROOT` 精確比對。沒匹配到不是錯誤,但 `index_stats` 會印 `C: no matching selector`。
- `mode`:`denylist`(預設)/ `allowlist`(只有 include 列的進索引)。
  **allowlist 下給非空 `exclude` 直接 fail-loud** —— `C.include` 優先於 `C.exclude`,
  在 allowlist 恆為死碼,靜默接受會養出錯誤心智模型。
- `detectors: false` 停用該 root 的 B1+B2(A′ 仍生效)。這是整樹級的鈍器;
  A′/B 的個別誤殺本來就能用 `include` 救。
- Glob 方言:gitwildmatch 子集,in-repo 實作(不引入 `pathspec` 依賴),比對「相對 root
  的 POSIX 路徑」、case-sensitive、`**` globstar、前導 `/` 錨定 root。**目錄比對補尾斜線
  再比** —— 所以 `vendor_env/**` 不匹配 `"vendor_env"` 但匹配 `"vendor_env/"`。
  向量鎖在 `tests/test_index_scope.py::test_matcher_vectors`。

### 快取遷移

`scope_fingerprint`(canonical JSON hash:C 展開後的 include/exclude 含順序、`mode`、
`detectors`、A′/B 的實際規則值、`CODE_EXTENSIONS`、檔案 ignore policy、matcher 版本、
schema 版本)寫進 `.code_rag_cache_meta.json`。

- fingerprint 缺失或不符 → **禁止**整包 fast load 與掃描快取 fast path,強制重算 membership。
- `embedding_model` 相同 → **保留** per-file symbol/embedding cache:scope 改了只重算
  membership delta,**不重 embed**。
- 任何來源的 cached path 一律**重過** `should_index_file`,不得直接信任。
- **dense 模式復用快取時要補算 lazy 留下的空 embedding**(`_backfill_cached_embedding_gaps`)。
  lazy 模式(符號數 > `CODE_RAG_LAZY_EMBED_MAX_SYMBOLS`)把 embedding 存成 `[]`,延後到
  查詢時才算;之後索引縮小到門檻以下就會走 dense,那些空洞會直接觸發
  「refusing zero padding」fail-loud,而且失敗不寫快取 → **重啟照樣失敗,索引永久建不
  起來**。索引縮小正是 index scope 的主要場景,所以這條是必修,不是防禦性程式碼。
  回歸鎖:`test_dense_rebuild_backfills_lazy_embedding_holes` /
  `test_lazy_index_shrunk_by_scope_still_builds`。
- **backfill 失敗要走 `_reset_partial_index()`**:embedding server 中途掛掉時,
  index / embeddings / `_indexed_file_hashes` 三個都得清掉。留任何一個,`query()`
  就會因為 index 非空而不重建(`_refresh_if_stale` 也因為 hashes is None 直接
  return),整個 MCP process 會一路用缺 embedding 的索引降級下去。回歸鎖:
  `test_backfill_failure_leaves_no_partial_index`。
- scope 熱重載是另案;改了設定要重啟 MCP server。

### 標題偵測為什麼收緊過

`is_heading` 原本有三條規則。實測三份真實 spec:

| 規則 | 命中 | 真標題 |
|---|---|---|
| markdown `#{1,6}` | 144 | 大部分（`流程：` 這種是來源文件自己的 `####`，不是誤判） |
| 數字章節 `^\d+\.[\d\.]*\s+[A-Z]` | 73 | **0** |
| 全大寫 `line.isupper()` | 3 | **0** |

數字那條全部命中的是「2. Power on the HAPS system.」「1. L2 CPU selftest: DM, CSM, XM」
這種編號條列項——真的章節標題在這些文件裡全部走 markdown。全大寫那條被中英混排騙了:
CJK 沒有大小寫,所以「PASS 畫面截圖如下：」的拉丁部分是大寫就整行算 ALL CAPS。

規則不能刪(沒有 markdown 結構的純文字文件要靠它們),所以改成:
- 數字:多層編號(`2.3` / `1.1.4`)照舊放行;單層編號要夠短(≤40)、句中無冒號、
  句尾無標點,才算標題。
- 全大寫:不含 CJK、至少兩個詞、至少兩個字母。

效果(同一批文件):雜訊 section 58 → 9,而且**找回 33 個真章節**(`1.1.4. 測試結果`
這種原本被前面的條列項擠掉的),chunk 237 → 180。

**改這條規則會改變切點與 content,既有 KB 必須重灌。**

---

### Reranker 與 MMR 的分工

`USE_MMR` 預設開著。以前的順序是「rerank 排好 → MMR 用 `cosine(query, chunk)` 重算
相關度」,等於把 cross-encoder 的結果整份丟掉——reranker 實際上只剩「篩候選」的作用。
真實 spec 上重現過:reranker 排第一的 chunk 被 MMR 直接剔除,換上一段雜訊。

現在 `_rerank_with_model` 回 `[(score, chunk), ...]`,分數一路帶到 `_mmr_select` 當
相關度(min-max 正規化到 [0,1],才跟餘弦的多樣性懲罰同量級),embedding 只負責算
多樣性懲罰。**沒有走到 cross-encoder 的路徑**(跳過 rerank、reranker 不可用而
fallback)分數是 None,MMR 退回原本的 embedding 相關度,那條路徑行為不變。

實務效果(8 題真題):`PASS 畫面截圖如下：`、章首導言這類雜訊 chunk 被換成真正的
章節;`1.6.4 測試結果是什麼` 這種指名章節的查詢終於撈得到 1.6.4。

---

### Contextual Retrieval(chunk 級生成脈絡)

入庫時替每個 chunk 生成一段 50–100 token 的定位文字(「本節出自 <文件> 的 <章節路徑>,
說明 <主題>」),存進 chunk 的 `ctx` 欄位,只餵檢索訊號。**兩個旗標都預設關閉。**

```bash
# 生成(唯一會生成的路徑;MCP 的 ingest_document 永遠不生成)
AICODE_KB_CONTEXT_GENERATE=1 python RAG.py rebuild --kb knowledge.json spec_a.pdf
python RAG.py rebuild --kb knowledge.json spec_a.pdf --context      # 旗標 > config
python RAG.py rebuild --kb knowledge.json spec_a.pdf --no-context   # 這次不生成

# 查詢端使用(同時是緊急 kill switch:關掉不必重建 KB)
AICODE_KB_CONTEXT_USE=1 aicode
```

| 環境變數 | 預設 | 作用 |
|---|---|---|
| `AICODE_KB_CONTEXT_GENERATE` | off | 入庫時是否生成 ctx |
| `AICODE_KB_CONTEXT_USE` | off | 查詢時是否使用 ctx(kill switch) |
| `AICODE_KB_CONTEXT_REMOTE_OK` | off | main URL 非 loopback 時的顯式同意 |
| `AICODE_KB_CONTEXT_TARGET_TOKENS` | 100 | ctx 長度上限(回應後截斷) |
| `AICODE_KB_CONTEXT_REASONING_TOKENS` | 512 | 請求端額外留給 reasoning 的額度 |
| `AICODE_KB_CONTEXT_WINDOW_SAFETY` | 0.8 | 窗預算的 n_ctx 安全係數 |
| `AICODE_KB_CONTEXT_MAX_ABSENT_RATIO` | 0.20 | 絕跡率超過就中止發布 |
| `AICODE_KB_CONTEXT_CACHE_DIR` | `~/.cache/codetrail/ctx` | ctx 快取(repo 外、per-root、0700) |

**為什麼預設關閉**:(a) standalone 的 `RAG.py` 目前只依賴 embedding server,預設開啟等於
替既有部署新增一條 main-server 硬依賴;(b) 部署允許 main URL 指到非 loopback,預設開啟
等於在沒有明確同意下把整份文件的窗送去遠端(NDA)。

**雙訊號是這個功能的正確性核心**。`ctx` 是 LLM 生成物,只准影響「哪些 chunk 被撈上來、
排第幾」。所有**決策**——拒答閘、信心標記、rerank/expansion 的 skip、數值證據判定、
污染控制的分數門檻——一律讀 content-only 的 gate 訊號:

- NPZ 存兩組矩陣:`embeddings`(retrieval,含 ctx)與 `embeddings_gate`(content-only),
  各帶自己的 schema/hash/維度/列數,同一個 `store_generation` 一次提交。
- BM25 也是兩套索引。gate 那套的來源文本與加入本功能之前逐位元組相同。
- `Candidate` 這個 dataclass 把 `retrieval_*` 與 `gate_*` 分開:哪個分數餵哪個決策在型別層
  看得出來、grep 得到。**看到 `candidates[i][1]` 這種寫法就是退化。**
- KB 有 ctx 卻缺 gate 矩陣 → 拒載,不 fallback 到 contextual 向量。
- gate 向量只留在矩陣裡,不 `.tolist()` 掛回 chunk;決策點用 `chunk_idx` 讀列。

`AICODE_KB_CONTEXT_USE=0` 時查詢端完全退回 content-only:dense 讀 gate 矩陣、BM25 用
content-only 索引、reranker passage 不加 ctx。**同一份 KB 上就能做乾淨的 A/B**,不需要
第二套 KB。

**成本**:每個 chunk 一次主模型呼叫。實測(21 chunk 合成語料、DeepSeek-V4-Flash)約
10 秒/chunk,prompt-cache 重用約 89%。文件沒變 → 全部命中快取 → 零 LLM 呼叫。

---

### scripts/kb_ab_compare.py

知識庫體檢與 A-B 對照。單一 KB 時做離線體檢（NPZ schema 是否現行版本、多少 chunk 帶
`[HEADING]` 前綴 / char span、多少 chunk 的 section 是空的、哪些章節標題重複到連
heading hierarchy 都分不開）；給兩份 KB 再加結構差異（**content 位元組有沒有變** →
決定既有向量還能不能用、section 差在哪幾筆）；加 `--questions` 才會跑檢索，需要
8081 / 8082。

```bash
python scripts/kb_ab_compare.py ~/proj/knowledge.json                       # 體檢
python scripts/kb_ab_compare.py old/knowledge.json new/knowledge.json       # 重建前後對照
python scripts/kb_ab_compare.py old/knowledge.json new/knowledge.json \
    --questions ~/questions.txt                                             # 加跑真題
```

**兩份 KB 一定要放不同目錄**：`knowledge_emb.npz` 是固定檔名，同目錄兩份 JSON 會互相
覆蓋向量檔；工具會直接擋下同目錄的組合，不會靜默比錯。預設只印 metadata 與計數，
不印 chunk 內容（NDA）；要看抽樣前綴得自己加 `--show-content`。問題檔與真實文件都
不進 repo。

重建一份對照 KB 就是把來源文件逐一灌進獨立目錄：

```bash
mkdir -p /tmp/kb-baseline && cd /tmp/kb-baseline
for f in <doc1> <doc2>; do python /path/to/CodeTrail/RAG.py "$f" ./knowledge.json; done
```

（embedding 快取是 CWD 下的 `.rag_embedding_cache.json`；把舊的複製進來可大幅減少
重算，內容沒變的 chunk 會直接命中。）

---

### scripts/index_stats.py

完全唯讀、完全離線的計數工具。**預設輸出只有計數,不含任何路徑** —— 這種輸出會被貼進
issue,路徑本身就是 NDA 內容。

```bash
AICODE_ROOT=/path/to/tree python scripts/index_stats.py
python scripts/index_stats.py --root /path/to/tree --deep        # 真的跑 AST 算符號數
python scripts/index_stats.py --root /path/to/tree --show-paths  # 顯式 opt-in 才印路徑樣本
```

root 只能來自 `--root` 或 `AICODE_ROOT`,都沒有就報錯不猜 cwd;驗證復用
`root_safety.validate_aicode_root`(和 MCP server 同一份,拒絕 `/`、`$HOME`、
不存在 / 非目錄)。root 不合法或 index-scope.json 壞掉都是乾淨的 `[FATAL]` + exit 2,
不吐 traceback。

符號數預設讀既有 cache,標成 `N (cached)`;**只有每個檔案都在快取裡而且 hash 對得上**
才給數字,少一個或過期就印 `unknown` —— 這個數字的用途就是判斷索引範圍對不對,
報一個過期的數字比報 unknown 更糟。hash 用 `code_rag.compute_file_hash` 同一份實作,
不另寫。`--deep` 才真的跑 AST,有檔數與時間預算,超過會標 `truncated`。

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
