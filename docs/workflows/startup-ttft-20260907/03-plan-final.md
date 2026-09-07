# startup-ttft-20260907 — Step 3 最終計畫(Fable 5.1, effort MAX)

- 規劃者:Claude Fable 5.1。本 session 的 harness 自報 model ID `claude-fable-5-1`(CLI init / modelUsage 由主代理記進 `00-model-log.md`)。施工者 Opus 5.0 的 ID 已核對為 `claude-opus-5`(同檔)。
- 日期:2026-09-07。動工 base HEAD:`f200f697ba54d38a102e8ef66dead652c4002e5f`(產品與測試未改)。
- 本步只寫本檔。沒有跑測試、沒有對任何 server 發請求、沒有讀私人 session、沒有 commit。
- 輸入:`00-intake.md`、`01-plan-initial.md`(初稿)、`02-plan-review.md`(Astra 審核,B01–B10)。初稿已查證的 file:line 事實可直接引用;本檔只寫變更後的決策。
- 本機 llama.cpp 原始碼在本 session 的讀取權限之外;凡引用 server / 模板行為(4 slots、LCP 挑 slot、`usage.prompt_tokens` vs `timings.prompt_n`、reasoning 影響渲染)皆**引用 `02-plan-review.md` 的查核**,標示為「審核已查」,本檔未重驗。

## 0. 一句話結論

| 需求 | 定案 |
|---|---|
| a. 自檢成功後不重播自檢 LOG | `Preflight.lines`(完整 transcript)逐字不變;新增分類清單 `warnings` / `status` 與 `banner_lines()`,TUI 只拿後者:一行摘要 + 壓縮狀態行 + 警告(含 preflight 期間**所有 stderr 行**)。失敗路徑不動(TUI 之前 exit 2)。 |
| b. server 長駐仍有首字延遲 | (1) **預熱**:TUI 就緒 / `/new` / 換 session / 壓縮後,用「下一輪真的會送的 prefix」(同一套轉換算出)送一個 `max_tokens=1` 的請求,把可避免的 prefill 搬到打字之前;准入在 engine 內、持模型鎖後判定,換 session 會中止進行中的預熱。(2) **量測訊號**:telemetry 新增 `prompt_tokens_processed`(真正評估的 token 數),冷 / 熱與收益只由它判讀。(3) **相位顯示**:狀態列分「等待首個 token / thinking / 回答中」,讓 reasoning 時間看得見。**不宣稱**消除 reasoning 與硬體 prefill 成本;收益數字只來自 David 的 T0。 |

## 1. 現況修正(對初稿 §1 的更正;B03)

1. **多 slot 事實**:主 server 4 slots、每 slot n_ctx 131072(intake:17;審核 B03 已查 build `b10276-6ea215d17`:server 在可用 slot 間依最長共同前綴挑選、必要時 LRU / host cache,SWA / hybrid checkpoint 可能讓已匹配的 prefix 重算)。初稿「上一個請求不是聊天 prefix 就整份重算」的**單 slot 因果推論撤回**:KB expansion / 壓縮摘要 / 另一個客戶端的請求多半落在別的 idle slot,聊天 slot 只在被 LRU 逐出時才丟。
2. 因此「冷 prefix」可信的來源只剩:server 剛啟動且 canary 未跑;換專案 / AGENTS.md / lessons / instructions / 工具目錄變動;4 個 slot 全被其他前綴換掉;以及 **checkpoint 條件下的結構性重算**(能否重用要靠 T0 的 `prompt_tokens_processed` 證明,規劃階段不能斷言)。
3. 使用者感受到的「首字延遲」還有第二個來源:reasoning 模型在第一個可見字之前的 thinking(`show_reasoning` 預設 False,畫面只有 spinner)。這段本計畫**不能縮短**,只能顯示出來(§2 D9)。
4. 初稿 §1.2 其餘查證(每步恰好一個請求、`cache_prompt` 恆為 True、prefix 組成與穩定性、SSE 客戶端 buffering 不是原因、Retry / gate / 鎖排除)維持有效,直接引用。

## 2. 決策

- D1 `Preflight.lines` 逐字不變(既有 gate node 不動)。分類**不改任何 print**:`note(message, *, keep=False)` 印出的字串與現在完全相同,`keep=True` 只把同一個 `message` 追加進 `warnings`;不新增 `warn()`、不加前綴、不重印(B07)。
- D2 成功後 TUI 第一屏 = `banner_lines()`:`("自檢通過:model=<m> n_ctx=<n> tools=<k> permission=<p> compaction=<mode>",) + tuple(status) + tuple(warnings)`。`status` 是 `compaction_status()` 產生的**全部**行(§2 契約:停用警告是使用者唯一會看到的地方,不做二次篩選);`warnings` 依實際發生順序含:`note(keep=True)` 的訊息(可多行,一則一個元素)與 preflight 期間寫到 stderr 的每一個非空行(逐字)。
- D3 失敗路徑不動:`PreflightError` 在 TUI 之前 exit 2,transcript 留在終端。
- D4 預熱送的是 **`Engine.next_turn_prefix()`**:對目前歷史「模擬追加一則 user」後走與 `payload_messages()` 同一套 heal → reasoning 剝除 → prune,再拿掉那則佔位訊息,得到 `[system] + to_wire(轉換後歷史)`。下一輪真實 `send(q)` 的 payload 恆等於 `prefix + [{"role":"user","content":q}]`;契約測試比對的是**真實後續 send() 擷取到的 payload**,不是同函式自我比對(B01)。
- D5 預熱請求形狀:`stream=True`、`tools`/`tool_choice="auto"`/取樣參數與 `_one_model_step` 相同、`extra={"max_tokens": 1}`;gate 保留額 = 1(就是實送的 max_tokens),另在送出前用下一輪的 `options.max_output_tokens` 做一次**不落 log** 的適用性檢查(B05)。
- D6 准入與身分(B02):預熱**不進** `_begin_turn`、不 `_record`、不發事件、不動 `_cancel` / `_armed`;它先非阻塞取模型鎖,再在 `_turn_state` 內判 `_in_turn == 0` 並在同一臨界區 snapshot 歷史;歷史身分取自**執行當下**,不是排程當下。`new_session()` / `adopt()` 會中止進行中的預熱(關 socket、等它放鎖,上限 1 秒)。`Engine.priming` 只由真的持鎖在送的那一次設定 / 清除。
- D7 多 slot 准入(B03):`/slots` 讀得到且**沒有任何 idle slot** 才跳過;有 idle slot(含 intake 觀測的 1 忙 3 閒)照送;讀不到視為未知、照送。
- D8 readonly / headless(B06):`prime_prompt_cache()` 對 `policy.name != "interactive"` 的 engine 在任何 GET / POST 之前回 skipped;headless `run` 沒有呼叫點(協調器只由 TUI 建),並以契約測試釘住。開關只有 repo 常數 `config.CLIENT_PRIME_PROMPT_CACHE = True`(不加 client.json 鍵、不加環境變數)。
- D9 相位顯示(取代初稿 T5,B08):狀態列相位只用 app 自己的計數(收到幾段 reasoning、有沒有 content),**不讀 engine 的任何新屬性**;`/status` 多一行「上次預熱」來自協調器回呼,替身沒有 `prime_prompt_cache` 時顯示「尚未」。初稿的 `/status` prefix 大小行**刪除**。
- D10 telemetry(B04):`ContextUsage` 新增 `prompt_tokens_processed`,只由 `timings.prompt_n`(native 與 OpenAI 兩種回應、串流最後一個 chunk)填,與既有 `actual_prompt_eval_count` 語意分離、不改後者。T0 與 B9 只讀新欄位。
- D11 不做:prefix 瘦身、`id_slot`、KB expansion / 壓縮摘要 payload 變更、SSE 路徑、部署參數(全列 deferred)。
- D12 測試分工(B09):唯一的 red-before-green node 是需求 a 的 `command_chat` 端對端 regression;其餘全部是新增契約,只寫不單跑,由整合者一次 smoke、審核者一次 full 執行。

## 3. B01–B10 處理結果

| # | 處理 | 落點 |
|---|---|---|
| B01 | 預熱改送 `next_turn_prefix()`(D4);契約測試用「reasoning 在最後一則 assistant + 三個 user 回合、兩筆各 30k tokens 的工具輸出(只有加了下一則 user 才會 prune 到第一筆)+ 一個懸空 tool_call」的合成歷史,先 prime、再真的 `send("q4")`,斷言 prime 的 messages == send 擷取 payload[:-1] 且 payload[-1] 是 q4;session 檔 / `engine.messages` / UI 原文不動(既有轉換契約不變)。B7 / B9 / 文件 / AGENTS 文字一律改口為「下一輪的 prefix」。 | Lane B §4.2、§5 B1 |
| B02 | D6:鎖後判 `_in_turn`、同臨界區 snapshot、執行當下取身分、`abort_prime()`、`priming` 由持鎖者擁有;協調器 `prime_in_background()` 不取回合鎖、不動 `_turn_done` / `_cancelled`,取消對預熱是 no-op。契約覆蓋:排程後回合才開始(store append 阻塞時 prime 讓路且不讀該回合歷史)、prestart 空窗(prime 先送、回合在鎖上等、之後拿到同一份 prefix)、壓縮收尾後立刻送、換 session 中止、重複 prime 第二個 skipped 且不碰 `priming`、取消契約不受影響。 | Lane B、Lane C |
| B03 | §1 更正;D7 的 idle-slot 判準;§5 C 把結果分成 skipped / sent / **reuse-verified**(只能由下一個請求的 `prompt_tokens_processed` 判定),寫出前提與失效判準;「不變差」改成明確的成本帳(一次 `/slots` GET + 一個 token 的 decode + 使用者在鎖上等的剩餘 prefill);收益維持未量測。 | §1、§5 C、§8 |
| B04 | D10 新欄位;契約測試涵蓋「同時有 `usage.prompt_tokens=20000` 與 `timings.prompt_n=7`」→ `actual_prompt_eval_count==20000`、`prompt_tokens_processed==7`,以及只有 timings 的串流 chunk。T0 改讀新欄位。 | Lane B、§8 |
| B05 | D5:`source="prime"` 那一列的 `reserved_output_tokens == 1 == extra["max_tokens"]`;下一輪適用性用 `build_usage(reserved=options.max_output_tokens).hard_overflow` 判,不 log;`CLIENT_MAX_OUTPUT_TOKENS` 的 import-time 上限與壓縮公式真值不動。 | Lane B |
| B06 | D8:engine 內 policy 判定在任何 probe / request 之前;契約測試對 `ReadOnlyPolicy` engine 呼叫 → skipped、HTTP / `/slots` 替身未被呼叫、零事件、store append 計數 0;headless 測試 monkeypatch `Engine.prime_prompt_cache` 為會 raise 的函式(`raising=False`)跑 `command_run` 全程;兩條登記 SAFETY_MODULES。 | Lane B、Lane A |
| B07 | D1:print 逐字不變;契約測試以 capsys 釘 `note("x", keep=True)` 與 `note("x")` 印出相同字串、多行訊息(`⚠ a\n  b`)原樣一則進 `warnings`、`lines` 不含重複 `⚠`;既有 `test_the_transcript_*` 兩條照舊。 | Lane A |
| B08 | D9:刪 `/status` prefix 行;相位只用 app 內部計數;`/status` 新行對沒有預熱能力的替身顯示「尚未」。既有 `_Engine` 替身**不需要**新增 `system_prompt.text` / `openai_tools()`。 | Lane C |
| B09 | D12;`tests/test_lessons.py` 指定 owner = Lane A(只改 402–403 註解措辭);每個 lane 的交付表逐條列「新增 node / 動到的既有測試 / 理由」。 | §6 |
| B10 | §7 凍結協定:固定 base HEAD、每次實際 HEAD、`git write-tree` 與**產品 digest**(全部產品 / 測試變更先 `git add -A` 排除 `docs/workflows`,任何未暫存的產品變更 = 未凍結,直接拒絕);審核者跑 full 前後各算一次,兩次都等於整合者記錄的值才算數。交接 markdown commit 不改變產品 digest。 | §7 |

## 4. 介面凍結(各 lane 只實作自己那一段;跨 lane 只依賴本節文字)

### 4.1 Lane A — `client_preflight.py` / `codetrail_chat.py`

```python
@dataclass
class Preflight:
    root: Path
    model: str = ""
    n_ctx: int = 0
    lines: list[str] = field(default_factory=list)      # 完整 transcript,語意與內容不變
    warnings: list[str] = field(default_factory=list)   # 成功後仍要進 TUI 的訊息(逐字,可多行)
    status: list[str] = field(default_factory=list)     # compaction_status() 的每一行(逐字)

    def note(self, message: str, *, keep: bool = False) -> None:
        print(f"[aicode] {message}", flush=True)         # 與現在完全相同
        if keep:
            self.warnings.append(message)

    def banner_lines(self, *, tools: int, permission: str, compaction: str) -> tuple[str, ...]:
        head = (f"自檢通過:model={self.model} n_ctx={self.n_ctx} tools={tools} "
                f"permission={permission} compaction={compaction}")
        return (head, *self.status, *self.warnings)
```

- `keep=True` 的呼叫點(只有這五處,其餘 `note()` 不動):`observe_n_ctx` 的 profile fallback(`client_preflight.py:181`)、`check_ctx_safety` UNKNOWN(197)、`render_lessons` 過期(266)、`_drop_rendered` 失敗(281)、`run()` 裡的 legacy web hint(402)。`compaction_status` 每一行 `note(line)` 之外同時 `status.append(line)`。
- `_Tee(stream, sink, *, on_line=None)`:stderr 那份 tee 帶 `on_line`,以行為單位(自行緩衝到 `\n`)把每個非空行逐字交給 `result.warnings.append`;`run()` 結束時把尾端未換行的殘片也交出。stdout 那份不帶 `on_line`。`captured` 與 `lines` 的計算不變。
- `codetrail_chat.command_chat`:`banner = checks.banner_lines(tools=len(engine.tool_specs), permission=engine.options.policy.name, compaction=compactor.mode)`。`CodeTrailApp` 的 `banner` 參數與 `on_mount` 顯示方式不變;`command_run` 不變。
- docstring 同步:`client_preflight.py` 模組 docstring 15–18 行、`_Tee` docstring、`Preflight` docstring、`run()` docstring 中「給 TUI 當對話區第一則」改成「完整 transcript 留在終端;TUI 只拿 `banner_lines()`」。

### 4.2 Lane B — `client_engine.py` / `config.py` / `llama_client.py` / `context_budget.py`

```python
# config.py(緊接 CLIENT_MAX_OUTPUT_TOKENS 的 import guard 之後)
CLIENT_PRIME_PROMPT_CACHE = True   # TUI 就緒 / 換 session / 壓縮後,用下一輪的 prefix 送 max_tokens=1 預熱 llama-server 的 prompt cache;headless / readonly 永不

# llama_client.py
def get_slots(base_url: str, *, timeout: int = 5, quiet: bool = False) -> list[dict] | None
    # quiet=True:失敗不呼叫 _log_probe_failure(Textual 接管後零 stderr);其餘不變

# context_budget.py
class ContextUsage:
    ...
    prompt_tokens_processed: int | None = None   # 只由 timings.prompt_n 填;不影響 actual_prompt_eval_count
# parse_usage_from_response():timings.prompt_n 為數值時一律填 prompt_tokens_processed(不看 pec 是否已有值)

# client_engine.py
PRIME_SOURCE = "prime"
PRIME_MAX_TOKENS = 1
PRIME_SLOTS_TIMEOUT = 2
PRIME_ABORT_WAIT = 1.0

class PrimeOutcome(NamedTuple):
    sent: bool
    reason: str          # "" | disabled | tools_not_loaded | policy | model_busy | turn_in_progress
                         # | server_busy | next_turn_would_overflow | gate | aborted | error:<ExcType>
    processed_tokens: int | None

class Engine:
    priming: bool                                    # property;只由持鎖在送的那次設 / 清
    def next_turn_prefix(self) -> list[dict[str, Any]]
    def prime_prompt_cache(self, *, reason: str = "") -> PrimeOutcome
    def abort_prime(self, *, wait: float = PRIME_ABORT_WAIT) -> bool   # new_session() / adopt() 呼叫
```

`prime_prompt_cache()` 規範順序(任何一步失敗都回 `PrimeOutcome(False, reason, None)`,不 raise、不 print、不發事件):

1. `not config.CLIENT_PRIME_PROMPT_CACHE` → `disabled`;`not self._loaded_tools` → `tools_not_loaded`;`self.options.policy.name != client_policy.InteractivePolicy.name` → `policy`(`OverridePolicy` 沿用 base 的 name,所以 client.json 覆寫後仍是 interactive)。**以上在任何 I/O 之前。**
2. `self.model_lock.acquire(blocking=False)` 失敗 → `model_busy`。之後全部在 `try/finally` 內,`finally` 一定 release、清 `_prime_stream`、`_priming=False`、set `_prime_done`。
3. `with self._turn_state:` 若 `self._in_turn > 0` → `turn_in_progress`(release 後回);否則 `history = list(self.messages)`、`self._priming = True`(同一臨界區)。
4. `llama_client.get_slots(self.options.base_url, timeout=PRIME_SLOTS_TIMEOUT, quiet=True)`:回 list 且非空、而且**每個** slot 都忙(`is_processing is True` 或 `isinstance(state, int) and state != 0`)→ `server_busy`;None / 非 list / 空 list / 有 idle → 繼續。
5. `payload = self._prefix_from(history)`(與 `next_turn_prefix()` 同一實作,吃 snapshot)。`context_budget.build_usage(source=PRIME_SOURCE, requested_num_ctx=n_ctx, messages=payload, tools=self._openai_tools, model=model, reserved_output_tokens=self.options.max_output_tokens).hard_overflow` → `next_turn_would_overflow`(不 log)。
6. `usage = context_budget.check_and_log(source=PRIME_SOURCE, …, reserved_output_tokens=PRIME_MAX_TOKENS, emit=False)`;`ContextOverflowError` → `gate`。
7. `stream = llama_client.chat_completions(base_url, messages=payload, model, temperature=self.options.temperature, top_p=config.CHAT_TOP_P, top_k=config.CHAT_TOP_K, min_p=config.CHAT_MIN_P, tools=self._openai_tools, tool_choice="auto", stream=True, extra={"max_tokens": PRIME_MAX_TOKENS}, timeout=self.options.request_timeout)`;登記 `self._prime_stream = stream`(獨立的 `_prime_guard` 鎖,不用 `_active_stream`);若登記前 `abort_prime()` 已到,直接關掉並回 `aborted`。
8. 逐 chunk `context_budget.parse_usage_from_stream_chunk(chunk, usage)`,內容丟棄;正常結束 → `context_budget.log_metrics(usage)`、回 `PrimeOutcome(True, "", usage.prompt_tokens_processed)`;被 `abort_prime()` 關掉 → `aborted`(不 log);其他例外 → `error:<Type>`。
9. 不碰:`_begin_turn` / `_end_turn` / `_record` / `_cancel` / `_armed` / `_turn_completed` / `_active_stream` / `_active_call` / store / `on_event`。

`next_turn_prefix()`:`working = heal_in_place(list(self.messages)) + [placeholder_user]`;`keep_reasoning` False 時 `strip_historical_reasoning`;`prune` True 時 `prune_old_tool_outputs`;確認最後一則仍是 placeholder(以 `is` 比對)後移除;回 `[{"role":"system","content": self.system_prompt.text}] + to_wire(working)`。docstring 必須寫明:與 `payload_messages()` 是同一套轉換,差別只在「以佔位 user 決定 reasoning 與 prune 的邊界」;placeholder 永不落檔、永不送出。

`abort_prime()`:在 `_prime_guard` 內取 `_prime_stream` 並 `close()`(關的是 socket,同 `OpenAIStream.close()`);`priming` 為 True 時等 `_prime_done`(上限 `wait` 秒),回「預熱是否已結束」。`new_session()` 與 `adopt()` 的**第一行**呼叫它。

### 4.3 Lane C — `client_turns.py` / `client_app.py`

```python
class TurnCoordinator:
    def prime_in_background(self, reason: str, *, on_done: Callable[[Any], None] | None = None) -> bool
        # engine 沒有 prime_prompt_cache(替身)或 self.busy → False,不 spawn
        # 否則 daemon thread 呼叫 engine.prime_prompt_cache(reason=reason);例外一律吞掉;
        # 結束後(有給 on_done 時)on_done(outcome)。不取 _turn_lock、不動 _turn_done / _cancelled。
```

- `_run_turn`:`_auto_compact` 改回傳 outcome(或 None);在 `finally: self.finish_turn()` **之後**(try 陳述式外)`if compacted: self.prime_in_background("compaction")`。`_run_compaction`(`/compact`)同樣在 `finish_turn()` 之後對 `status == "compacted"` 呼叫。
- `client_app.CodeTrailApp`:
  - `_prime(reason)` = `self.coordinator.prime_in_background(reason, on_done=lambda o: self._from_worker(self._note_prime, o))`;`_note_prime` 只記 `self._last_prime = (time.time(), outcome)`,不動對話區。
  - 呼叫點:`on_mount` 在 `_replay_startup_session()` 之後、`_recount_context()` 之前 → `"mount"`;`_cmd_new` 在 `session_changed()` 之後 → `"new"`;`_switch_session` 在 `session_changed()` 之後 → `"session"`。
  - 狀態列(`_refresh_status`,**只追加 parts**):`getattr(self.engine, "priming", False)` 為 True 時加 `prompt cache 預熱中`;`_turn_started` 非 None 時在 spinner 那段後接相位:尚無 reasoning 也無 content → `等待首個 token`;有 reasoning、無 content → `thinking {n} 段`;有 content → `回答中`;`/compact` 那一輪固定 `壓縮中`。計數只由 `_on_reasoning` / `TYPE_TEXT_DELTA` / `TYPE_TEXT` 累加,`submit()` / `_cmd_compact` 歸零,終結 `step_finish` 清除;閒置時**不**顯示相位,所以既有 `status_text` 斷言(含 `/thinking` 那條)不受影響。
  - `/status` 追加一行:`prompt cache 預熱=尚未` 或 `prompt cache 預熱=<sent|skipped(reason)> <HH:MM:SS>`。
  - 不讀 `engine.system_prompt.text`、不呼叫 `engine.openai_tools()`(B08)。

### 4.4 Lane D — 文件與 AGENTS

- `README.md:706-707`、`docs/setup.md:248`:改為「成功時對話區只留一行摘要、壓縮狀態行與警告(含工具健檢寫到 stderr 的行);完整自檢輸出留在 TUI 之前的終端畫面;失敗時 `aicode` 不進 TUI,錯誤原樣留在終端」。
- `docs/troubleshooting.md`:在「MoE 模型第一次對話 TTFT」節之後新增「開新對話首字慢:先分辨 prefill、reasoning 與 cache 冷熱」:每步一個請求、`cache_prompt` 恆開、prefix 五段 + 工具 schema、4 slots 下 server 依共同前綴挑 slot(寫成「本機 build 的行為」不是保證)、什麼時候會冷、預熱做什麼 / 不做什麼(只送下一輪 prefix、1 token、readonly / headless 永不、換 session 會中止、無法縮短 thinking)、`.codetrail/context_metrics.jsonl` 的讀法(`source`、`message_count`、`estimated_input_tokens`、`prompt_tokens_processed`)、狀態列相位與 `/thinking` 只影響顯示。**不得出現任何環境變數**;不得引用本交接目錄。
- `README_DEV.md`:模組分工表加 `Engine.next_turn_prefix` / `prime_prompt_cache` / `abort_prime`、`TurnCoordinator.prime_in_background`、`config.CLIENT_PRIME_PROMPT_CACHE`、`ContextUsage.prompt_tokens_processed`;加一段「SSE 客戶端 buffering 已查證不是首字延遲來源(urllib3 chunked 逐 chunk 回傳、`: ` keep-alive 行已略過),不要再修」;測試指南段補「預熱只有契約測試,沒有 red-before-green」。
- `AGENTS.md` §2 只加三句:`client_engine` 條 —「`prime_prompt_cache` 是唯一沒有使用者訊息就打主模型的路徑:零寫入(不進 `_begin_turn`、不 `_record`、不發事件、不動取消旗標)、只送 `next_turn_prefix()`(與下一輪同一套轉換)、實送 `max_tokens=1` 且 gate 保留額就是 1、非 interactive policy 一律拒絕、headless 沒有呼叫點」;`client_turns` 條 —「`prime_in_background` 不取回合鎖、不動 `_turn_done` / `_cancelled`,取消對預熱是 no-op」;`client_app` 條 —「啟動橫幅只拿 `Preflight.banner_lines()`:摘要一行、壓縮狀態行、警告(含 preflight 期間所有 stderr 行);進度行不進畫面,但 `Preflight.lines` 仍是完整 transcript」。

## 5. 驗收

### A. 自檢 LOG(Lane A)

- A1 `Preflight` 介面如 §4.1;`lines` 語意與內容逐字不變;`note()` 印出的字串不變。
- A2 `banner_lines()` 逐項 = D2;**不含**:`root=`、`deployment profile=`、獨立 `model=` / `n_ctx=…(來自主 server)` 行、`ctx safety=SAFE`、`lessons:… 已注入` / `沒有 active lessons`、`已移除先前 render`、`[model-preflight] PASS`、`[tool-health]` 的 stdout 行(`MCP PASS` / `MODEL PASS` / `IMPLICIT … optimal` / 心跳)。
- A3 `warnings` 至少涵蓋:preflight 期間每一個非空 stderr 行(canary 的 `[tool-health] WARNING …` 等只走 stderr,`tool_call_canary.py:167-168`)、`⚠ … lessons 已過 review_by`(含第二行 `複審:…`)、`⚠ 無法移除舊的 lessons.md`、`⚠ 升級前啟動的網頁 backend 還在跑`(三行一則)、`ctx safety=UNKNOWN(…);放行`、`n_ctx=…(來自 deployment profile;server 尚無法觀測)`。
- A4 `status` = `compaction_status()` 產生的全部行,順序不變。
- A5 `command_chat` 用 `banner_lines(...)`;`CodeTrailApp`、`command_run` 不變。
- A6 失敗路徑不變(exit 2、stderr、TUI 未啟動)。
- A7 測試(見 §6 Lane A)。
- A8 文件同步(§4.4 前兩項)+ `client_preflight.py` 四處 docstring + `tests/test_client_preflight.py` 模組 docstring 15–18 行 + `tests/test_lessons.py:402-403` 註解。

### B. 預熱(Lane B / C)

- B1 payload 等價:契約測試以真實後續 `send()` 為準(§3 B01)。
- B2 零寫入:`engine.messages`、store append 計數、`resumed_snapshot`、`_cancel`、`request_cancel().accepted`(prime 期間為 False)在前後逐字相同;不呼叫 `on_event`。
- B3 跳過條件與順序 = §4.2;每一種都不 raise、不 print(capsys 空)。
- B4 鎖:非阻塞取、`finally` 放;真回合在 `_ModelSlot.__enter__` 排隊時 Ctrl-C 仍可中斷(既有 `test_a_turn_waiting_for_a_leased_model_lock_can_still_be_cancelled` 不動)。
- B5 telemetry:`source="prime"` 一列,`reserved_output_tokens == 1`、`prompt_tokens_processed` 有值;readonly 永不預熱,所以 `CTX_METRICS_ENABLED=False` 的邊界不變。
- B6 呼叫點:TUI `on_mount` / `/new` / 換 session / 壓縮成功後(回合鎖外);`command_run` 零呼叫點。
- B7 換 session 中止:`new_session()` / `adopt()` 之後 1 秒內舊預熱結束(`aborted`)、鎖已放、`priming` False。
- B8 `get_slots(quiet=True)` 失敗零 stderr;既有呼叫端不變。
- B9 誠實揭露(交付報告固定句):「reasoning 與硬體 prefill 成本未變;預熱只把**下一輪 prefix** 的 prefill 搬到打字之前;熱 prefix 時無感;是否真的被重用只由 T0 的 `prompt_tokens_processed` 判定,規劃與施工階段沒有數字」。

### C. live 驗收前提與失效判準(David 的 T0;施工與審核**不執行**)

| 結果類別 | 判定依據(`.codetrail/context_metrics.jsonl`) | 意義 |
|---|---|---|
| skipped | 沒有 `source=prime` 列;TUI `/status` 顯示 `skipped(<reason>)` | 沒有預熱;第一題照舊 |
| sent | 有 `source=prime` 列;`prompt_tokens_processed ≈ estimated_input_tokens` = 當時是冷的、已搬前;`≪` = 本來就熱 | 請求成功,尚未證明可重用 |
| reuse-verified | 同一 session 緊接的 `source=client` 列(`message_count` = prime 的 +1)`prompt_tokens_processed` < 1024 且遠小於它自己的 `estimated_input_tokens` | prefix 真的被重用,b 的機制成立 |
| reuse-failed | 上述 `client` 列 `prompt_tokens_processed ≈ estimated_input_tokens`,而中間沒有其他請求(`/slots` 無他人、另一份安裝的客戶端未使用) | 結構性重算(checkpoint / SWA)或 build 差異:預熱在此 build 無益,把 `config.CLIENT_PRIME_PROMPT_CACHE` 改 False 並記入 `07-deferred.md`;這是可能發生的分支,不是失敗的施工 |

成本帳(取代「不變差」):每次預熱最多多出一次 `/slots` GET(2 秒上限)+ 一個 token 的 decode;使用者在預熱中送出時,在模型鎖上等預熱剩下的 prefill(reuse-verified 時那段本來就要算;reuse-failed 時是純多算,見上表)。

## 6. Lane 分工、Dependency、可寫檔案、工具與測試

工作方式:每個 lane 在自己的 worktree(`git worktree add ../wt-<lane> f200f697ba54d38a102e8ef66dead652c4002e5f`),交付 `git diff` patch + `04-impl-<lane>.md`。四個 lane 可**同時開工**(介面由 §4 凍結,跨 lane 只用 `getattr` 或 `raising=False` 的 monkeypatch);整合順序 B → C → A → D(檔案兩兩不重疊,順序只影響閱讀)。所有 lane 共同禁止:對 :8080/:8081/:8082/:8083 發任何請求(所有 HTTP 一律 monkeypatch `llama_client`)、`pytest` 直呼、`run_tests.py` 不帶單一 node、`-m smoke` / full、動別人的檔、新增 `os.environ` 讀取、`process_env` 以外的 spawn、`git commit` / `push`、改 `.gitignore`。允許:Read / Glob / Grep / Edit / Write、`git diff` / `git status`、`python3 -m py_compile <自己的檔>`(不收集 pytest)。

### Lane A(Opus)— 需求 a

- 可寫:`client_preflight.py`、`codetrail_chat.py`、`tests/test_client_preflight.py`、`tests/test_client_cli.py`、`tests/test_lessons.py`(只有 402–403 註解)。
- 依賴:無。
- **唯一 red-before-green node**:`tests/test_client_cli.py::test_the_tui_banner_after_a_passing_preflight_drops_the_progress_log_and_keeps_warnings`。
  - 走真的 `client_preflight.run()`:HOME 指到 tmp 並寫最小 `deployment.json`(可複製 `test_client_preflight._write_deployment` 的 10 行),monkeypatch `check_tool_health`(印一行 stdout `MODEL PASS — cached` + 一行 stderr `[tool-health] WARNING — implicit 診斷降級`)、`check_required_servers`、`check_ctx_safety`、`observe_n_ctx`(同 `test_the_transcript_keeps_stderr_warnings` 樣式)。
  - `command_chat` 的其餘替身:`_has_tty`→True、`_resolve_root`→tmp root、`_settings`→`client_config.ClientSettings(path=tmp/"client.json")`、`client_config.apply_to_config`→no-op、`_build`→(有 `close()` 的假 mcp, 有 `tool_specs` 19 項與 `options.policy.name="interactive"` 的假 engine)、`_compactor`→`SimpleNamespace(mode="manual")`、`client_app.CodeTrailApp`→擷取 `banner` kwarg、`run()` 回 0 的假類別。
  - 斷言:banner[0] 以 `自檢通過:` 開頭並含 `tools=19`;banner 含 `[tool-health] WARNING — implicit 診斷降級` 與某行含 `壓縮模式`;banner **不含**任何以 `root=` / `deployment profile=` / `MODEL PASS` 開頭或含 `[aicode]` 前綴的行;exit 0。基線樹上紅燈原因:banner 含 `[aicode] root=…`(行為紅燈,不是缺方法)。交付貼紅 / 綠節錄各一段。
- 只寫不單跑的契約(帶 smoke;檔案已有 module 層 `pytestmark`):
  - `tests/test_client_preflight.py::test_the_banner_keeps_stderr_warnings_and_compaction_status_but_drops_the_progress_log`(A2 / A3 / A4 的集合斷言 + `result.lines` 仍含 `MODEL PASS`)。
  - `tests/test_client_preflight.py::test_keep_does_not_change_what_note_prints`(B07:capsys 釘 `note("x")` 與 `note("x", keep=True)` 輸出相同、多行訊息一則進 `warnings`、`lines` 無重複 `⚠`)。
  - `tests/test_client_cli.py::test_headless_run_never_primes_the_prompt_cache`(B06:`monkeypatch.setattr(client_engine.Engine, "prime_prompt_cache", boom, raising=False)`,沿用 `test_headless_defaults_to_ephemeral` 的替身跑 `command_run` 全程,exit 0、事件流形狀不變)。
- 動到既有測試:只有 `tests/test_client_preflight.py` 模組 docstring 與 `tests/test_lessons.py:402-403` 註解措辭;零既有斷言變更。

### Lane B(Opus)— engine 預熱與 telemetry

- 可寫:`client_engine.py`、`config.py`(只加一個常數與註解)、`llama_client.py`(只加 `quiet`)、`context_budget.py`(只加欄位與填值)、`tests/test_client_engine.py`、`tests/test_context_budget.py`。
- 依賴:無。
- 零 red-before-green(功能 + 契約)。契約 node(全部 `@pytest.mark.smoke`;沿用 `engine_factory` / `FakeMcp` / `_stream` / `_text_chunk` / `_BlockingStream`;HTTP 一律 monkeypatch `llama_client.chat_completions` 與 `llama_client.get_slots`):
  1. `test_priming_sends_the_prefix_the_next_turn_will_send_and_records_nothing`(B01 合成歷史 §3;store 用會計數 append 的替身;斷言 messages / tools / tool_choice / stream=True / extra={"max_tokens":1} / 取樣參數與後續 `send` 相同;`engine.messages` 前後相等;append 計數 0;`config.CLIENT_PRIME_PROMPT_CACHE=False` 時回 `disabled` 且零 HTTP)。
  2. `test_priming_refuses_a_readonly_engine_before_any_probe_or_request`(B06)。
  3. `test_priming_yields_to_a_turn_that_already_began_and_never_reads_its_history`(B02:store `append` 對 user 訊息阻塞在 Event 上 → 此時 prime 拿到鎖但 `_in_turn==1` → `turn_in_progress`、零 HTTP、鎖已放;釋放後回合正常送出且 payload 含該 user;另一半:回合已持鎖時 prime → `model_busy`)。
  4. `test_a_turn_submitted_during_priming_waits_for_the_lock_and_gets_the_primed_prefix`(prestart 空窗:prime 的 HTTP 替身阻塞;另一執行緒 `send("q")` 的 HTTP 在 prime 釋放前不得發生;之後 `send` payload == prime.messages + [user q])。
  5. `test_a_session_switch_aborts_an_in_flight_prime_and_frees_the_lock`(`_BlockingStream`;`new_session()` 後 2 秒內 prime 回 `aborted`、`closed_from` 長度 1、鎖可取、`priming` False、`adopt()` 同樣)。
  6. `test_priming_skips_only_when_no_slot_is_idle_and_never_raises_or_prints`(B03:`[{"id":0,"is_processing":True},{"id":1,"state":0}]` → 送;全忙 → `server_busy`;None → 送;`chat_completions` raise → `error:RuntimeError`、鎖已放、capsys 空)。
  7. `test_priming_gates_the_one_token_it_sends_after_checking_the_next_turn_reserve`(B05:擷取 `check_and_log` kwargs 與送出的 extra → 兩者都是 1;把 `n_ctx` 調到「prefix+8192 溢位、prefix+1 不溢位」→ `next_turn_would_overflow` 且零 HTTP、零 log)。
  8. `test_priming_is_invisible_to_cancel_and_leaves_the_turn_state_untouched`(prime 阻塞中:`request_cancel().accepted is False`、`cancel() is False`、`_cancel` 未設;結束後 `clear_cancel()` 狀態如初)。
  9. `tests/test_context_budget.py::test_processed_prompt_tokens_are_recorded_separately_from_the_total`(B04 三種回應形狀)。
- 動到既有測試:無。

### Lane C(Opus)— 協調器與 TUI

- 可寫:`client_turns.py`、`client_app.py`、`tests/test_client_turns.py`、`tests/test_client_app.py`。
- 依賴:只依賴 §4.2 的介面**名稱**(`prime_prompt_cache(reason=)`、`priming`、`PrimeOutcome` 形狀);對 engine 一律 `getattr`,替身沒有就跳過,所以可與 Lane B 同時施工。
- 零 red-before-green。契約 node(smoke):
  - `tests/test_client_turns.py::test_a_compaction_that_replaced_the_history_primes_after_the_turn_lock_is_released`(`_Engine` 替身加 `prime_prompt_cache(reason)` 記 `(reason, coordinator.busy)`;`_Compactor(outcome=_Outcome("compacted","已壓縮"))` → 終結事件後等到記錄 `("compaction", False)`;`skipped` / `stopped` / `failed` 不呼叫;`/compact` 路徑同)。
  - `tests/test_client_turns.py::test_priming_is_invisible_to_busy_and_cancel_and_refused_while_a_turn_runs`(閒置時 `prime_in_background("x")` → True、`busy` 仍 False、`cancel()` False;`_Blocks` 進行中 → False 且不 spawn;替身沒有方法 → False)。
  - `tests/test_client_app.py::test_the_tui_primes_on_mount_new_and_session_switch_through_the_coordinator`(`_Engine` 替身加記錄 reasons 的 `prime_prompt_cache`;mount → `mount`;`/new` → `new`;`/session <id>` → `session`;回合進行中 `/new` 本來就被擋,reasons 不增加;替身在呼叫中把 `priming=True` 並等 Event 時,`status_text` 含 `預熱`,釋放後不含)。
  - 相位顯示零測試(UI 便利);但**不得**讓既有 `status_text` 斷言變動。
- 動到既有測試:只在 `_Engine` 替身**新增**方法(`prime_prompt_cache`、`priming=False`),不改任何既有斷言;交付表列出。

### Lane D(Opus)— 文件

- 可寫:`README.md`、`docs/setup.md`、`docs/troubleshooting.md`、`README_DEV.md`、`AGENTS.md`(§2 只加 §4.4 那三句)。
- 依賴:措辭等 A–C 的交付表確認介面名稱未變;可先起草。
- 可執行:`python3 scripts/check_readme_consistency.py`(靜態,不收集 pytest)。零測試。

### 整合者(Claude 側;Step 5 起由 Fable 修 Blocker)

- 可寫:`tests/test_smoke_gate.py`(把 §6 各 lane 的 smoke node 登記到既有檔名鍵下:`test_client_preflight.py` 2 條、`test_client_cli.py` 2 條、`test_client_engine.py` 8 條、`test_context_budget.py` 1 條、`test_client_turns.py` 2 條、`test_client_app.py` 1 條;不新增檔名鍵)、`docs/workflows/startup-ttft-20260907/*.md`。
- 順序:套 B → C → A → D 的 patch → `python3 -m compileall -q .` → `python3 scripts/check_readme_consistency.py` → **唯一一次** `python3 scripts/run_tests.py -m smoke` → §7 凍結。smoke 有任何本次引入的紅燈就回給該 lane,不自行擴 owner(整合者只能改自己那兩處)。
- 每個 lane 的 `04-impl-<lane>.md` 必含:改動檔案清單、新增 node 名、Lane A 的紅 / 綠節錄、動到的既有測試逐條(檔、node、理由)、未做 / 偏離之處。

## 7. 凍結、識別與審核(B10)

```bash
BASE_HEAD=f200f697ba54d38a102e8ef66dead652c4002e5f
# 1. 全部產品 / 測試變更進 index(交接目錄除外;staging 不是 commit)
git add -A -- . ':(exclude)docs/workflows'
# 2. 凍結檢查:每一行不是 docs/workflows/ 底下,就必須是第二欄空白的 staged 條目;
#    任何 ' M' / 'MM' / '??' 的產品路徑 = 未凍結 → 停下來,不得計算 digest
git status --porcelain=v1 --untracked-files=all
# 3. 識別三件套
git rev-parse HEAD                                                      # 每次實際 HEAD(交接 commit 後會前進)
git write-tree                                                          # index 的完整樹(含已 staged 的交接檔)
git diff --cached --binary "$BASE_HEAD" -- . ':(exclude)docs/workflows' | sha256sum   # 產品 digest(主識別)
```

- 報告固定寫三行:`base HEAD = f200f697…`、`actual HEAD = <rev-parse>`、`product digest = <sha256>`(附 `git diff --cached --stat`)。**不得**寫「HEAD 含修正」;產品不 commit、不 push。
- 審核者(Astra,`ROLE=REVIEWER`):先靜態審核只列 Blocker;歸零後對凍結內容執行一次 `python3 scripts/run_tests.py`(full),**執行前後**各跑一次步驟 2–3,兩次 digest 都等於整合者記錄的值才算對同一份內容;回報命令、結果、三件套。判定:失敗 node 集合不得大於基線(缺 tty / `llama-server` 的環境相依失敗屬基線);`0 collected`(exit 5)必報異常。
- Step 5 每一輪回修都重做步驟 1–3,並在 `06-fix-rN.md` 記三件套;交接 markdown 以 `git add -f docs/workflows/startup-ttft-20260907/` commit(使用者已授權),不影響產品 digest。
- 分歧超過兩輪才可擱置,擱置事項寫進 `07-deferred.md` 並在 push 前告知使用者。

## 8. T0 量測(唯讀;執行者 David,在真實使用 aicode 的專案目錄;施工 / 審核不執行)

```bash
# (1) 靜態事實(GET,零副作用)
curl -s http://localhost:8080/props | python3 -c '
import json,sys; p=json.load(sys.stdin); g=p.get("default_generation_settings",{})
print("n_ctx",g.get("n_ctx"),"n_parallel",p.get("n_parallel") or p.get("total_slots"),"build",p.get("build_info"))
t=p.get("chat_template") or ""
print("template_len",len(t),"tools_idx",t.find("tools"),"system_idx",t.find("system"))'

# (2) 每個請求真正評估的 prompt token(新欄位;舊列會是 n/a)
python3 - <<'EOF'
import json, pathlib
rows=[json.loads(l) for l in pathlib.Path(".codetrail/context_metrics.jsonl").read_text().splitlines() if l.strip()]
rows=[r for r in rows if r.get("source") in ("client","compaction","prime")]
for r in rows[-60:]:
    proc=r.get("prompt_tokens_processed"); tps=r.get("prompt_tokens_per_second") or 0
    print(f'{r["timestamp"]:.0f} {r["source"]:10s} msgs={r["message_count"]:3d} est_in={r["estimated_input_tokens"]:6d} '
          f'processed={proc if proc is not None else "n/a":>6} prefill_s={(proc/tps if (proc and tps) else 0):6.1f} out={r.get("actual_eval_count")}')
EOF

# (3) prefix 穩定性:同一專案跑兩次 digest 必須相同
PYTHONPATH=/home/david/CodeTrail python3 -c 'import client_prompt as c; p=c.build_system_prompt("."); print(p.chars, p.digest)'
```

判讀依 §5 C 表。額外兩條:`tools_idx < system_idx` 或工具段在訊息之後 → 預熱只涵蓋 system 段,寫進報告;預熱請求若被 server 以 4xx 拒絕(payload 以 system / assistant 結尾)→ `/status` 會顯示 `error:HTTPError`,列 deferred(候選修法:佔位空 user 訊息),**不得**在施工階段擅自改 payload 形狀。

## 9. 既有呼叫端相容性(維持初稿 §6,補三點)

- `Preflight.note()` 的輸出、`run()` 簽名、`lines` 不變;`tests/test_lessons.py` 用 capsys 收 `note` 的用法不變。
- `Engine` 只新增方法 / property;`EngineOptions`、`client_compaction.EngineLike`、事件流、`command_run` 不變;`new_session()` / `adopt()` 多一個對 `abort_prime()` 的呼叫,沒有預熱時是 no-op。
- `ContextUsage` 多一欄,`to_log_dict` 經 `asdict` 自動帶出;既有欄位語意不變。

## 10. Deferred(揭露,不在本次)

1. prefix 瘦身(工具 schema / AGENTS.md 注入預算):最大槓桿,但動路由真值與 eval catalog digest。
2. `id_slot` 釘 slot:4 slots + LCP 之下暫無必要;T0 若證明聊天 slot 常被逐出再議(部署決策)。
3. KB 端 `USE_QUERY_EXPANSION` / multi-query 預設:retrieval 品質決策。
4. 壓縮摘要請求帶同一份 system prompt / 換 slot:摘要契約另案;本次以「壓縮後預熱」補回。
5. `gpu_safety.runtime_offload_check` 只認 `state`,新 build 的 `is_processing` 會被算成 idle:doctor 維護項。
6. `keep_historical_reasoning=True` 換跨回合 cache 命中:使用者 client.json 選擇,不改預設。
7. 部署調參(`ubatch` / `n_cpu_moe` 對 prefill 吞吐):不可動 live 部署,David 決定。
8. reuse-failed 分支(§5 C):若 T0 證明此 build 不重用,`CLIENT_PRIME_PROMPT_CACHE` 改 False 並記錄;自動偵測「預熱後下一請求仍全量重算」並自動停用,列為後續。
9. 使用者送出時是否中止進行中的預熱:取決於 llama-server 中止時保不保留已處理的 KV(本 session 無法查證),先不做;現行行為是回合在鎖上等預熱完成(可 Ctrl-C)。
10. 預熱 payload 被 server 拒絕時的佔位 user 形狀(§8 末段)。

## 11. 給 Astra 的 Step 5 審核重點(只審 Blocker)

1. A2 / A3 / A4 集合精確;`lines` 與 `note()` 輸出逐字不變(B07)。
2. B01 契約真的比對後續 `send()`,且合成歷史同時觸發 reasoning 剝除、prune 邊界、heal。
3. 准入順序(policy → 鎖 → `_in_turn` + snapshot 同臨界區 → `/slots` → 兩次 gate → 送)與 `finally` 釋放;`abort_prime` 由 `new_session` / `adopt` 呼叫;`priming` 只由持鎖者設清。
4. gate 保留額 == 實送 `max_tokens` == 1;`prompt_tokens_processed` 只由 `timings.prompt_n` 填。
5. readonly 在任何 I/O 前拒絕;`command_run` 零呼叫點且測試釘住;協調器只由 TUI 建。
6. `SAFETY_MODULES` 登記齊全、node 存在、帶 smoke;沒有新 `os.environ` 讀取、沒有 `process_env` 以外 spawn、文件零環境變數。
7. 交付措辭:不宣稱消除 reasoning / 硬體成本;數字只來自 T0 或標「未量測」;三件套齊全且 full 前後 digest 一致。
