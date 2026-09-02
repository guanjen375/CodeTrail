# CodeTrail 開發者備忘

這份文件說明 OpenCode 日常使用以外的開發者基礎設施。專案首頁主要看 `README.md`；
AI agent 改 repo 前看 `AGENTS.md`（安全紅線與禁止事項）；這裡只放維護命令與內部工具。

---

## 維護命令索引

下面是命令目錄，不代表每個角色都能在每次修改中全部執行。實際執行權責以
[AGENTS.md §1](AGENTS.md#2-測試-policy) 為準：預設 developer 在開發途中不跑測試，
交付前只跑一次 smoke；只有本次 prompt 明示 `ROLE=REVIEWER` 才在程式碼收斂後跑一次
full。靜態 consistency / compile 檢查不會收集 pytest，可在相關檔案變更後使用。

```bash
# 靜態檢查（不收集 pytest）
python3 -m compileall -q .
python3 scripts/check_eval_consistency.py
python3 scripts/check_readme_consistency.py
python3 scripts/opencode_contract_check.py            # 全域 opencode.json / AGENTS.md / 壓縮 plugin 漂移
python3 scripts/compaction_status.py          # 目前的壓縮模式(aicode 橫幅那一行;純讀取)
python3 scripts/doctor.py --no-network        # 用機器上實際設定的模型；不要塞假 model 名
python3 deployment_profile.py validate

# 部署唯讀相容檢查（需要本機 OpenCode；不寫設定、不跑 MCP/model）
python3 scripts/opencode_direct_contract.py --root <PROJECT_TO_ANALYZE>

# 測試入口（何時能跑見 AGENTS.md §1）
python3 scripts/run_tests.py -m smoke
python3 scripts/run_tests.py

# Lint（advisory，CI 不擋）
ruff check tests scripts
```

截至 2026-08，MCP Python SDK 2.x 已是 stable；本 repo 仍刻意留在維護中的
v1：`requirements.txt` 使用官方給未遷移專案的 `mcp>=1.28,<2`，因為程式
仍 import `mcp.server.fastmcp.FastMCP`。SDK 2.x migration 必須另案同步處理 import、
transport、schema 與 OpenCode 相容性，不能只移除 `<2`。`doctor` 會把缺少
MCP、低於 1.28 或 2.x 都列為 FAIL。

`python3 scripts/run_tests.py` 無參數時先用真的 `pytest --collect-only` 收一次，再以標準庫把
**test node**（不是檔案）分成最多 16 個隔離 shard 並行執行，不需要 `pytest-xdist`。
收集交給 pytest 自己，所以並行與序列收到的永遠是同一組測試。小檔整檔留在同一個
shard；比一個 shard 平均負載一半還重的檔才切開——以檔案為單位分片時，一個 8 秒的檔
就是整包的牆鐘下限，而合併測試檔之後這種檔只會更多。
資源較小或要重現序列順序時用 `AICODE_TEST_JOBS=1 python3 scripts/run_tests.py`。

**`-m <expr>`（例如交付前的 `-m smoke`）走同一套分片**：它跟無參數一樣只是「選
整個 `tests/` 的一個子集」，分片不改變任何一條測試的語意。collect 階段一條都沒選中
就直接回 exit 5 並明講「這不是通過」——AGENTS.md §1.2 的 0 collected 規則；不會啟動
任何 shard。每個 shard 拿到的都是 collect 真的選中的 node，所以任何 shard 非 0（含 5）
都算失敗。只要傳的是 `-k`、`-x`、node id、`--lf` 或其他組合，就維持原本的單一 pytest
行程與逐字轉發語意：`-x` 的 exitfirst、node id 的順序、`--lf` 依賴的共享 cache 在分片
下都不再等價。

**`--changed[=REF]` 只跑改動波及的測試檔**（可加 `-m smoke`）：改動 = git 工作樹的
修改／staged／untracked，`--changed=main` 再加上 `main...HEAD` 的提交差異。對應規則：
改到 repo 模組 → import 圖上（直接或間接）用到它的測試檔，加上文字上提到
`<name>.py` 的測試檔（用 subprocess 跑 script 的測試不會 import）；改到 `aicode`、
`set_config.sh`、plugin `.js` 等非 Python 檔 → 文字上提到檔名的測試檔；改到測試檔
→ 它自己加 smoke gate。它是 **fail-closed** 的：任何一個改動對不到測試檔（或動到
conftest／harness／pyproject／requirements／`tests/fixtures/`）就退回完整測試並講明
原因。它是開發途中的加速，不取代交付前的 smoke 與審核者的 full。

分片權重（`.pytest_cache/shard_weights*.json`）記的是**每條 node** 上一輪的實測秒數，
**每次選取各記一份**：`-m smoke` 量到的寫進 `shard_weights.smoke.json`，不污染完整
測試用的 `shard_weights.json`（`--changed` 沿用對應 marker 的那份；同一條 node 的耗時
不因選檔而變）。沒有紀錄的 node（第一次跑／剛新增）用同檔已知 node 的中位數，整檔都
沒紀錄才用預設值。權重的更新條件是「這個 shard 的 pytest session 有跑完」（exit 0/1/5），
不是「有沒有全綠」——紅燈期正是最常重跑的時候。沒跑完的 shard（exit 2/3/4、訊號終止）
其 junit 是殘缺的，那些 node 保留上一輪的值。每次並行執行結束會多印合計「選中幾條／
各 shard 內耗時總和」與最慢的幾條（≥0.5s），給 §1.1 的 smoke 10 秒目標做趨勢觀察；
**不設硬秒數閾值**，不同機器差好幾倍，拿秒數當 gate 只會製造假紅燈。

核心日常入口是 OpenCode TUI；跨機 web 另有薄 launcher（兩者共用 `aicode` 安全前置）：

```bash
cd <PROJECT_TO_ANALYZE>
aicode
aicode_web  # A/B 機已加入同一 tailnet 時
```

---

## 修改流程

1. 先確認本次角色與 [AGENTS.md](AGENTS.md) 的安全紅線，再做最小修改。
2. 只有兩類情況新增測試：真實 bug 的 regression，或會無聲失敗的契約／安全檢查點。
   新功能本身不自動等於要補儀式性測試。
3. bug fix 必須走 red-before-green：先新增帶 `@pytest.mark.smoke` 的 regression，單跑取得
   紅燈，再改實作並單跑同一 node 轉綠。這是 developer 開發途中唯一允許的測試例外。
4. 依變更內容跑不會收集 pytest 的靜態 consistency / compile 檢查。
5. developer 交付前只跑一次 `python3 scripts/run_tests.py -m smoke`；reviewer 才在收斂後對
   目前 HEAD 跑一次 full。任何新失敗都要先處理，`0 tests collected` 不算通過。
6. 不要自行 commit；使用者確認後才可提交。

---

## 測試指南

測試在 2026-09 依主題合併成 35 個檔（原本 95 個）；一個檔 = 一個被測主題，檔頭
docstring 說明它涵蓋哪些原始檔與為什麼：

- launcher / config：`test_aicode.py`（aicode wrapper、web、attach）、`test_set_config.py`
  （set_config.sh 的問答、模型、artifacts、壓縮段）、`test_server_scripts.py`
  （launch／stop／check_status、aicode_web）、`test_deployment.py`（deployment profile、
  模型解析、GPU 與 ctx 安全、config）、`test_doctor.py`（doctor + tool-call canary）、
  `test_opencode_checks.py`、`test_lessons.py`。
- OpenCode 壓縮 / plugin：`test_compaction_mode.py`（ownership 狀態檔 + status 行）、
  `test_opencode_plugins.py`（codetrail-compaction.js 與 codetrail-notify.js 的跨語言契約）。
- MCP / sandbox / mutation：`test_mcp_server.py`（啟動、runtime policy、工具目錄、
  JSON-RPC roundtrip、結果預算、external import）、`test_mcp_ingest.py`（ingest 子行程、
  stream、通知）、`test_mcp_lease.py`、`test_fs_sandbox.py`（read／list／grep／read_pdf 的
  sandbox 與預算）、`test_elf_analysis.py`、`test_run_command.py`（白名單、timeout、lint）、
  `test_apply_patch.py`（udiff parser、apply、S/R、byte-safe）、`test_patch_verify.py`、
  `test_endpoint_policy.py`。
- Code-RAG / graph：`test_code_graph.py`（tree-sitter 解析、graph、C++ 可見性、metadata）、
  `test_code_rag_index.py`（索引、dense cache、index scope、檔案種類）、
  `test_code_rag_search.py`（檢索契約、CJK、code_context、repeat guard、semantic）。
- RAG / KB / 圖面：`test_knowledge_store.py`（文件身分、kb_cache、embedding fail-loud、npz）、
  `test_rag_ingest.py`（chunking、PDF ingest、extracted_document、context generation）、
  `test_rag_retrieval.py`（reranker／MMR、檢索回歸、context signals）、
  `test_figure_candidates.py`、`test_figure_ingest.py`、`test_figure_review.py`、
  `test_figure_verify.py`、`test_figure_retrieval.py`（payload、檢索、review_figures 工具）、
  `test_vision_pipeline.py`。
- inference / budgets / eval：`test_context_budget.py`（context gate + trim）、
  `test_evals.py`（code smoke／retrieval／semantic／tool routing／flywheel／session eval）。
- repo infrastructure：`test_repo_consistency.py`（含 scripts `--help`）、
  `test_test_runner.py`、`test_smoke_gate.py`。

`tests/_harness.py` 與 `tests/_set_config_harness.py` 是共用 harness，不是 pytest test
module。smoke 的安全組成由 `tests/test_smoke_gate.py` 靜態守住；不要以手動檔案清單取代。

`test_smoke_gate.py` 的 `SAFETY_MODULES` 記的是「檔名 →（說明, 必須存在且帶 smoke 的
node 名）」，對照 [AGENTS.md §2](AGENTS.md#3-安全相關不要砍) 的檢查點清單。只驗「這個檔
至少有一個 smoke 標記」是不夠的：刪掉那條檢查點測試、或把 module 層 `pytestmark` 換成
單條 decorator，gate 都還是綠的，而缺口是無聲的。**新增 §3 檢查點時，AGENTS.md 的條目與
這裡的 node 清單要一起改**。

---

## 改 config / docs / eval 時要同步檢查

`config.py`、`README.md`、`docs/*.md`、`eval/*.json`、`mcp_server.py` 的工具清單必須對齊。
修改途中先跑兩個不收集 pytest 的靜態檢查：

```bash
python3 scripts/check_eval_consistency.py
python3 scripts/check_readme_consistency.py
```

pytest 部分仍依角色執行：developer 不另外單跑 `test_repo_consistency.py`，由交付前唯一一次
smoke 涵蓋；`ROLE=REVIEWER` 則在程式碼收斂後由 full 涵蓋。不要因本節把同一組測試重跑。

漂移範例（已修，避免再犯）：
- 改 `RERANKER_TOP_N` → 對應的 `eval/spec_holdout.json` gold_evidence 也要改
- `_parse_unified_diff` 從 `agent.py` 搬到 `agent_tools.py` → `eval/code_questions.json` 的 `file` 要改
- 換 `EMBEDDING_MODEL` → `eval/spec_adversarial.json` 也要改
- 在 `knowledge.py` 這類檔案大量增刪行 → `eval/code_questions.json` 釘的 `line` 會漂出 ±20(實際發生過:`query` 從 2172 移到 2810),要更新
- 新增／移除／重排 MCP 工具 → 先改 `mcp_contract.PUBLIC_TOOL_ORDER`，再同步
  `docs/mcp-tools.md` 與 `docs/opencode-agents-template.md` fenced block **外**的固定順序
  manifest；可安裝的全域 prompt 只保留 `codetrail_*` schema anchor，禁止把完整清單搬回去。
  使用者還在用舊版固定清單時，`aicode` 會提示 `⚠ STALE`
- 壓縮接管(`codetrail` / `manual`)**還在測試階段**:標示的單一來源是
  `compaction_mode.EXPERIMENTAL_TAG` / `EXPERIMENTAL_NOTICE`,由 set_config 的問答與
  摘要頁、`scripts/compaction_status.py`(aicode 啟動橫幅)、`scripts/doctor.py` 與
  `docs/compaction-rules.md` / `README.md` 共用。要拿掉「實驗中」是一次全域決定,
  不是改其中一處——`tests/test_compaction_mode.py` 釘住問答與文件都還帶著它
- 改壓縮的七條摘要規則或門檻公式 → `docs/compaction-rules.md` 的兩個 ```text 區塊是
  **唯一來源**，`opencode_plugins/codetrail-compaction.js` 的 `RULES_TEXT` /
  `RECONCILIATION_HEADER` 逐字沿用它們，`compaction_mode.derive_settings` 與 plugin 的
  `deriveSettings()` 是同一條公式的兩份實作。三處任一改了另外兩處沒跟上，只會讓門檻與
  保留額對不上——沒有任何錯誤訊息。`tests/test_opencode_plugins.py` 逐字比對
- 新增 incident kind / detail slug → `mcp_lease.py`、`opencode_plugins/codetrail-notify.js`、
  `opencode_plugins/codetrail-compaction.js` 與 `tests/test_opencode_plugins.py` 的
  凍結 tuple 必須一起改（跨語言封閉集合；一端沒跟上就把另一端寫的合法值正規化成
  `unknown`，那些事件在 doctor 的統計裡等於憑空消失）

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
- `eval/run_code_smoke_eval.py`：全離線 code inference gate。保留歷史 16 題
  regression floor，另跑 20 題 blocking core(code2test / trace2code / edit2ripple /
  firmware semantics / selective retrieval，各 4 題)與最多 4 題 stretch；目前 fixture
  是 20 + 4(stretch 含 2 題 `comment2context`,那是 **provisional diagnostic
  family**,不是 blocking core)。CI structural lane 使用 parser/lexical/graph 與 HTTP
  poison，固定檢查 `12000` / `28000` 的純 `budget_chars`，不輸出 tokenizer token 單位。
  SHA-256 pseudo embedding 只叫 deterministic plumbing stub，不代表真 semantic 品質；
  `--with-servers` 才是手動 real-model lane，永不成為 CI 必要條件。
- `eval/semantic_retrieval.py` + `eval/fixtures/code_smoke/semantic_vectors.{json,f32}`：
  **real semantic lane**。向量是 checked-in 的 float32 artifact(bge-m3、1024 維、
  cls pooling、L2),document 與 query **兩種都錄**;manifest 記 model / pooling /
  dimension / parser / render / scorer 版本與 corpus digest,不含本機絕對路徑。
  正常執行完全離線,**任何 cache miss、render 不符、corpus digest 漂移或 checksum
  不符一律 fail closed**(non-zero exit),絕不退回合成向量。
  四條 lane:`lexical`(可比較基準)、`dense`(純 cached cosine)、
  `runtime_hybrid`(**主 gate**;直接呼叫 `code_rag.hybrid_symbol_score` 與
  `select_scored_candidates`,跑的是 production 那份 scoring 與 cutoff)、
  `rrf_experimental`(選配診斷,**不得**冒充 production hybrid)。
  scope:`per_repo` 是主 gate lane(對齊 runtime 每次只有一個 `AICODE_ROOT`),
  `union` 只是 cross-repo distractor 的 stress 診斷;12k/28k context gate 走 `per_repo`。
  file metric 先依 `repo_id:path` **聚合去重**再截 k,並對**完整 `gold_files`** 計分;
  `seed_files` 另報 `seed_recall`,不取代主指標。
- `eval/record_semantic_vectors.py`：**唯一**可以碰 loopback llama-server 的入口,
  而且要顯式帶 `--record-vectors`。錄製前對 `/props` 與 effective profile 核對 role /
  pooling / GGUF identity,錄製頭尾各 embed 一次 sentinel,漂移超容差就拒絕寫檔。
  corpus、parser 語意、render schema 任一變更都要重錄(manifest digest 會自己擋)。
- `eval/fixtures/code_smoke/semantic_retrieval_baseline.json`：semantic baseline,
  含 corpus digest、render / scorer / model 版本與 per-family 數字。pipeline 版本或
  corpus digest 一變就**跳過**no-regression 比較並印出原因 —— 不同 corpus 的數字
  本來就不可比,硬比才是假訊號。**只有 blocking family 能擋 gate**,而且成員資格是從
  case 的 `blocking` 欄位推出來的,不寫死清單;`comment2context` /
  `low_lexical_overlap` 這類 provisional diagnostic 照常報數字,但退步不擋 gate ——
  無差別比較等於偷偷把 stretch 升格成 blocking。
- `eval/fixtures/tool_routing/cases.json`：隔離 synthetic root 的 9 個檔案、1 份 KB 文件與
  15 個中英／混合 routing cases；結果只保存分類、aggregate、token/latency/compaction
  計數，不保存 prompt、assistant text、tool args/result、session id 或專案路徑。
- `eval/fixtures/tool_routing/support_matrix.json`：6 個明示 arm 與逐列 compatibility／gate
  真值。目前唯一一列仍是 `measured`；2026-08-27 已把逐 arm 凍結 digest、routing baseline
  與完整 privacy-safe aggregates 寫回，但沒有任何 arm 通過全部 gate，所以**沒有任何列可
  宣稱 supported**。harness 絕不改 matrix；gate 通過只輸出
  `manual_status_change_required=true`，仍需人工審核後明示改狀態。特別注意：目前
  `--arm` 只選擇／記錄 arm id，**不會替操作者切換 OpenCode config、tool schema、build
  prompt 或 todowrite permission**。每個真模型 arm 必須先由維護者在隔離環境套用 exact
  variant，核對凍結 config/artifact digest 與 matrix 中的 contract digest；不相符就
  fail-loud。
- `session_eval.py`／`scripts/session_eval.py`：明示 opt-in 的私人 session-model eval。
  `opencode export` 只負責來源封存；mined draft 排除所有歷史 assistant text，curated suite
  禁止 `expected_answer`／`gold_answer` 類欄位，只接受外部 verifier、`human_pairwise` 或
  `unscored`。原始 prompt／candidate answer 只寫 `.codetrail/session_eval` 的 0700/0600
  私有產物，永不放進 checked-in `eval/`；runner 核對 live GGUF 與 project-state digest，
  並雙層關閉寫入／執行工具。每題原子 checkpoint；resume 必須重驗 suite／模型／現場，
  單題 timeout 記為該題失敗而不丟掉先前結果。完整流程見 `docs/session-model-eval.md`。
  **壓縮語意**：replay config 一律同時拿掉 CodeTrail plugin **與**受管的 `compaction.*`
  ——只拿掉其中一邊會形成「上游 auto 關閉、idle 觸發那一端又不在」的混合語意，長案例
  會變成互動端不會發生的 provider 錯誤，而 `compaction_events` 靜靜讀到 0。以壓縮本身
  為題的 suite 才用 `--keep-compaction`,它會**同時**保留受管設定、把壓縮 plugin 加回
  replay config、在 0700 暫存目錄寫一份綁定該臨時 config 的拋棄式 ownership state
  (`AICODE_COMPACTION_STATE`),並量測 OpenCode 版本傳給 plugin 的版本閘。這四樣任一
  不成立(plugin 檔不在、global 缺受管鍵、受管值不是候選/compaction agent 模型推導的、
  版本低於 1.18.17)就 `SessionEvalError` —— 少了這些前置檢查,結果會是「完全沒有壓縮」
  而沒有人知道。`scripts/eval_tool_routing.py`
  走的是**有效**全域設定（plugin 與 `compaction.*` 都在），所以那條路徑的語意就是
  使用者目前的模式；不同模式的結果不可比，`effective_config_digest` 已經涵蓋這一點。
  摘要**品質**（七條規則、五輪權重、舊結論淘汰）只走這條私人 eval，不進 smoke / full——
  用 mock 驗模型輸出品質等於沒驗。
- `scripts/mcp_catalog.py`／`scripts/eval_tool_routing.py`：runtime catalog 預設從 effective
  stdio MCP 實跑 `initialize/tools/list`；current arm 精確對照
  `mcp_contract.PUBLIC_TOOL_ORDER`，任何名稱或順序 drift 都 fail-loud；完整 schema 只留
  privacy-safe count/digest，typed bounds 另由 static contract gate 驗證。
  `--catalog-only` 強制零模型並跳過 auxiliary model preflight；歷史 baseline 只能顯式用
  `--arm baseline --frozen-contract`，逐欄 exact match 該 row 保存的
  order/per-tool/digest/count，不能拿 current contract 冒充歷史資料。catalog-only 結果的
  support gate 明確是 `passed=false`，不可能靠 catalog aggregate 升級 support status。
  真模型 event parser 把 reasoning token／text 與 assistant text 分開；reasoning 永不參與
  promise／marker 分類，也不會被保存成 assistant output。
- `scripts/check_eval_consistency.py`：不跑 LLM，只檢查 eval expected 是否和 `config.py` / source code 漂移。
- `tests/test_repo_consistency.py`：把 consistency check 接進 pytest。

下列命令是 eval 工具目錄，不是每次改碼的交付 checklist。developer 途中可跑第一條靜態
drift check；其他 runner 只在任務明示要做 eval / benchmark 時使用，pytest 仍依本文開頭與
AGENTS.md 的角色規則執行。

```bash
python3 scripts/check_eval_consistency.py
python3 eval/run_retrieval_eval.py
python3 eval/run_code_smoke_eval.py                      # 全離線 gate
python3 eval/run_code_smoke_eval.py --report-json /tmp/report.json   # A/B 用的完整 summary
python3 eval/run_eval.py --test-set all --verbose

# routing catalog-only：先由維護者外部套用並凍結 <ARM>，CLI 不會代為切 variant
python3 scripts/eval_tool_routing.py --root <SYNTHETIC_ROOT> \
    --matrix-row <ROW_ID> --arm <ARM> \
    --output /tmp/tool-routing-catalog.json --catalog-only

# 歷史 baseline replay：--frozen-contract 只能和 baseline arm 併用
python3 scripts/eval_tool_routing.py --root <SYNTHETIC_ROOT> \
    --matrix-row <ROW_ID> --arm baseline --frozen-contract \
    --output /tmp/tool-routing-baseline.json --catalog-only

# 只有這兩條會連 8081。改了 corpus / parser 語意 / render schema,或 bump 了
# RETRIEVAL_SCORER_VERSION 之後都要重錄(pipeline 不符時 eval gate 會 FAIL,
# tests/test_evals.py 也會紅)。
# LLAMA_BIN 一定要設 —— 沒設的話 artifact 的 llama_cpp.revision 會靜默記成
# "unknown",那份 checked-in fixture 就失去可驗證的 build 出處。
LLAMA_BIN=~/llama.cpp/build/bin/llama-server \
    python3 eval/record_semantic_vectors.py --record-vectors
LLAMA_BIN=~/llama.cpp/build/bin/llama-server \
    python3 eval/run_code_smoke_eval.py --record-semantic-baseline
```

前三個命令不需要 llama-server；retrieval runner 固定回報 Recall@5、MRR、nDCG@5 與
數值證據精確率。加 `--predictions <json>` 時才另外計算 citation entailment、數值答案
精確率、拒答率/拒答正確率。`eval/run_eval.py` 才需要本機 4 個 llama-server 與對應 GGUF。

`scripts/eval_tool_routing.py` 不加 `--catalog-only` 才走真模型；這條只可在既有**明示授權**下，
使用相容 OpenCode、選定 matrix row/arm、isolated synthetic root 與停用其他 MCP server
執行。`--arm` 不做 variant composition；執行前還必須由維護者凍結並核對該 arm 的 exact
config／artifact／contract digest。完整介面是：

```bash
python3 scripts/eval_tool_routing.py --root ROOT --matrix-row ROW_ID --arm ARM \
    --output RESULT.json [--model provider/model] [--catalog-only] [--frozen-contract]
```

`--catalog-source in-process` 是 CI/testing 隱藏選項，而且只允許搭配 `--catalog-only`；日常
量測不得用它取代 effective stdio MCP。主 tools/no-tools token probe 保持相同 minimal
message/model/`max_tokens=1`/stream，只差 tools；任何一側 usage 缺失就整組 fallback。
FastMCP instructions 另以對稱 apply-template/tokenize marginal delta 計入。沒有授權、沒有
live-after 結果或 gate 未通過，都要明列未完成，不能把 `measured` 改寫成 `supported`。

本次 checked-in 狀態是 2026-08-27 經明示授權、在同一個
DeepSeek-V4-Flash UD-Q8_K_XL／Unsloth／OpenCode 1.18.21 row 完成的 privacy-safe aggregate。
六個 arm 都先凍結 exact contract digest，再各跑完整 15-case fixture；這是量測證據，仍不是
支援宣告：

| arm | catalog tokens | tool recall | evidence adoption | 主要失敗／決策 |
|---|---:|---:|---:|---|
| `baseline` | 13,369 | 38.5% | 69.2% | bait=1、promise=1；row-local baseline |
| `schema_only` | 14,446 | 38.5% | 69.2% | token、recall、grounding、guards 未過 |
| `schema_plus_mcp_instructions` | 14,585 | 46.2% | 76.9% | bait=0，但 promise=1 且 recall／grounding 未過 |
| `schema_instructions_build_prompt` | 14,585 | 38.5% | 76.9% | promise=0，但 recall／grounding／bait 未過 |
| `schema_instructions_build_prompt_todowrite_deny` | 14,585 | 38.5% | 76.9% | 未改善；淘汰，runtime `todowrite` 維持 `allow` |
| `english_descriptions` | 4,280 | 38.5% | 69.2% | 只過 token gate；bait=2、promise=2 |

後續完整組合中，最佳 `selected_combo_v3_stop_on_evidence` 是 4,331 catalog tokens、61.5%
tool recall（比 baseline +23.1 個百分點），但 evidence adoption 反降至 61.5%，且 bait=2、
promise=1，所以仍淘汰。`selected_combo_v2_exact_routes` 另有 1 個 harness-invalid case；再後面的
strict canary 仍無法穩定產生中英文規格工具呼叫，因此停止 prompt tuning，沒有拿部分 canary
冒充 full gate。

結果是 matrix row 保持 `measured`、`supported_arm=null`，每個正式 result 都是
`manual_status_change_required=false`，人工升級條件未觸發。build prompt 預設為 false，只能用
`--enable-experimental-build-prompt` 明確 opt-in；`todowrite` 維持 `allow`。所有 checked-in
measurement 都是統計與相容性 digest，不含 prompt、專案路徑、工具參數或輸出。

### Code graph 的 C/C++ 保守解析

`GRAPH_SCHEMA_VERSION=3` 保存 definition linkage/condition、function declarations、
include edge 的 preprocessor condition，以及 edge 的 `resolution_basis`/condition。

C/C++ 的 **definition 語意**(`ast_parser.PARSER_SEMANTICS_VERSION`)只認
translation-unit / namespace scope,並且逐個 declarator 判斷:

| 寫法 | 結果 |
|---|---|
| `uint32_t g_error_counter;` | 1 個 `global`(C tentative definition) |
| `static int a, *b;` | 2 個 internal-linkage `global` |
| `extern int only_declared;` | **0 個**(純宣告) |
| `extern int defined_here = 1;` | 1 個(有 initializer) |
| `int prototype_only(int);` | **0 個**(函式原型) |
| `static int (*handler)(int);` | 1 個 `global`(function pointer 是**物件**) |
| `typedef int count_t, *count_ptr_t;` | 2 個 `typedef` |
| `typedef enum { A, B } state_t;` | `typedef state_t` + `enum state_t`(用 alias)+ 2 個 `enum_constant` |
| `enum class State { Idle };` | enumerator 的 qualified_name 是 `State::Idle`(scoped) |
| `enum Plain { PlainA };` | enumerator 的 qualified_name 是 `PlainA`(unscoped,本來就在外層 scope) |
| `struct driver_ops;` | **0 個**(forward tag 不是定義) |
| `struct S { int x; };` | 1 個 `struct`(**要有 body** 才算型別定義) |

stable kind 寫死在 `ast_parser`:`macro` / `macro_function` / `typedef` / `enum` /
`enum_constant` / `global`。只有 `global` 有 linkage;macro / typedef / enum /
enum_constant 在 C/C++ 語意上沒有 linkage,graph 誠實標 `not_applicable`,不硬掰。
C 的 linkage 精確處理(`static`→internal、其餘 file scope→external);**C++ 縮限**:
anonymous namespace 與 `static` 是 internal,namespace-scope 的 non-volatile
`const`/`constexpr` 是 internal,`inline`/`extern` 是 external,template 內或帶未建模
specifier(如 `thread_local`)一律標 `unknown` —— **不猜 external**。

版本消費矩陣:`PARSER_SEMANTICS_VERSION` 進 CodeRAG cache meta、graph 的
`_parser_versions()` 指紋與 eval vector manifest 三處。它是**語意**版本,不是 table
shape —— 改它時**不要**順手 bump `GRAPH_SCHEMA_VERSION`。

cache 身分只有一份定義:`code_rag.cache_identity()`。它除了 schema / parser /
embed-text 版本,還帶**實際的 render 預算值**(清單以 `render_budgets`
為準,別另外記個數)—— 那些預算是 `AICODE_*` 環境變數可覆寫的,只鎖 schema
version 的話,重啟時改一個環境變數就會靜默沿用「用另一組 render
算出來的」embedding。寫入端、驗證端與測試 fixture 都從 `cache_identity()` 取:各寫一份
的失敗一樣無聲 —— 加了欄位而 fixture 沒跟上,舊 cache 被拒、那條測試改走 full rebuild,
「還是綠的」卻不再驗它本來要驗的東西。
既有舊版 DB（v1/v2）由同一條顯式 build command 在單一 SQLite transaction 中原地
升級；升級失敗會 rollback。真正損壞、無法由 SQLite
開啟的 DB 不宣稱能原地重建：錯誤會要求先移出/刪除 graph DB，再執行 build command。
C/C++ call 只按下列證據順序解析：同檔定義、C++ exact qualified name、實際 included
header 的 static-inline 定義、direct/transitive repo-header 可見且 qualified identity 相符的
prototype 對應唯一 external 定義；其餘維持 ambiguous 或 unresolved。候選不再由第一個
condition-incompatible stage 截斷：可證明是同一 preprocessor chain 的互斥 branch 才排除，
其餘跨 stage 合併成明示 ambiguity。bare call 不會配到別的 C++ scope/method；`static` 與
anonymous namespace definition 不跨 translation unit，function pointer/macro 不猜。quote
include 沿用 repo resolution；angle include 只有帶 namespace path、非絕對且唯一 suffix
命中才進 visibility closure。bare `<stdint.h>` 的單一 repo basename 不會誤配，多個同名
repo candidate 會留下 ambiguity edge；絕對 angle path 不做 suffix 配對。`.hh` / `.hxx`
已在 `CODE_EXTENSIONS`、index scope 與 tree-sitter parser 三層按 C++ header 接通。

C/C++ 任一檔案 add/change/delete 都把檔案 hash 當作完整 visibility fingerprint 並走
full rebuild；這是刻意的保守 invalidation，避免 linkage/declaration/include closure 的
partial cone 與 fresh build 漂移。Python 仍走既有增量路徑；body-only edit 因 callable
node-id catalog 沒變，不會只因同名 C call 就 fan-out。只有名稱、qualified identity 或
overload identity 改變且牽動 C/C++ caller，才會在寫 DB 前切換成 full rebuild。相關
pytest gate 是 `tests/test_code_graph.py`（tree-sitter 解析、graph、C++ 可見性都在這一個
檔）；reviewer 由收斂後的 full 統一涵蓋。developer 修 bug 時只依 AGENTS.md §1.3 單跑自己
新增的 regression node 取得 red / green，不另跑整個 module。`python3 eval/run_code_smoke_eval.py` 也只在本次任務明示要檢查 code-inference
品質時執行。

---

### Code RAG 的語意表示式與預算

三個消費者共用**同一組 canonical 欄位**(`code_rag.CANONICAL_SEMANTIC_FIELDS`),
但各有各的預算:

| 消費者 | 預算常數 | 預設 |
|---|---|---:|
| index entry 儲存的 context | `CODE_RAG_CONTEXT_STORE_MAX_CHARS` | 1800 |
| index entry 儲存的 leading comment | `CODE_RAG_COMMENT_MAX_CHARS` | 400 |
| index entry 儲存的 docstring | `CODE_RAG_DOCSTRING_MAX_CHARS` | 300 |
| dense embedding document text | `CODE_RAG_EMBED_TEXT_MAX_CHARS` | 1200 |
| lexical scorer 掃描文字 / identifier 數 | `CODE_RAG_LEXICAL_SCAN_MAX_CHARS` / `CODE_RAG_LEXICAL_MAX_IDENTIFIERS` | 1200 / 80 |
| reranker passage | `CODE_RERANK_PASSAGE_MAX_CHARS` | 1800 |

**儲存端是最上游的截斷**:`CODE_RAG_CONTEXT_STORE_MAX_CHARS` 比下游任何預算小的話,
調大下游全部是 no-op(這是 2026-08-20 之前的真實狀況:context 在 index entry 就被截到
500,所以「把 embed text 從 400 調大」完全沒有效果)。`tests/test_code_rag_search.py`
靜態守住這條不變式。

C 的 `/** ... */` 寫在定義行**之上**,而 context 從定義行往下取,結構上永遠拿不到 ——
所以 leading comment 是獨立欄位(`Symbol.comments`),邊界有四條:同 scope(只走
sibling)、不跨空行、不跨 preprocessor 或其他節點、不吃檔頭 license。三個消費者
**都**看得到它;只加進 embed text 而 lexical 還在掃舊 context 的話,那條 lane 會靜默
看不到註解訊號。

`EMBED_TEXT_SCHEMA_VERSION` 的維護規則(權威定義在 `code_rag` 該常數的宣告處)
寫成**白名單**:

> **免 bump 的只有「已經列在 `cache_identity()` 的 `render_budgets` 裡的預算數值」**
> (含 `AICODE_*` 覆寫)。其他任何會改變 render 輸出的修改 —— 欄位集合、欄位順序、
> label 文字、分隔方式、截斷演算法,以及**任何還沒進 `render_budgets` 的截斷數字**
> —— 一律「bump,或先把它納入 identity」。

寫成白名單而不是「預算免 bump」,是因為後者會被讀成「這個數字也是預算,所以不必
bump」,而沒進 identity 的數字改了又不會讓任何東西失效 —— 兩邊都不動,舊向量就被
靜默沿用。踩過的例子是 docstring 的 `[:300]`:它確實是預算,但沒有名字也沒進
identity;現在它是 `CODE_RAG_DOCSTRING_MAX_CHARS`,規則因此變成機械可判定。
上游那一刀不歸這條規則管:`ast_parser` 建 `Symbol` 時就先截過 docstring /
signature / condition,也決定 leading comment 取幾行 —— 那些屬
`PARSER_SEMANTICS_VERSION`(同樣在 `cache_identity()` 裡)。
兩條機制都必要:增量重建只比 file_hash,少了任何一邊都會靜默沿用用舊 render
算出來的向量。

實測(fixture corpus,per_repo / runtime_hybrid):leading comment 讓 macro-average
file recall 0.6683 → 0.7783、MRR 0.900 → 0.950,context coverage(1.000)、evidence
precision(0.293)與 used chars(76514)不變。**1200 與 1800 的 A/B 在這份 fixture 上
分不出差異** —— 最長的 document 表示式只有 536 chars,兩個上限都不會截到任何東西。
要調這個數字必須拿真實 firmware repo 重測,不能拿 fixture 的結果當依據。

### FileKindPolicy(檔案類型的單一來源)

`file_kind_policy.py` 是 `CODE_EXTENSIONS` 與 `GREP_DEFAULT_EXTENSIONS` 的**共同來源**。
以前兩份手寫清單已經漂了:grep 那份比索引窄,連 `.cc` / `.cxx` / `.pyi` / `.pyx` /
`.bash` / `.txt` / `.mk` / `.cfg` / `.cmake` / `.ini` / `.conf` / `.tcl` 都搜不到。

三個投影:grep glob、index scope 成員資格、symbol parser route。canonical suffix 一律
小寫(比對走 `Path.suffix.lower()`),但 **grep glob 是 case-sensitive**,所以 `.S` 會
另外產一條 `*.S`;`Makefile` / `Makefile.*` / `Kconfig` / `Kconfig.*` 走 basename 規則,
規則本身也進 index scope fingerprint(否則規則改了成員資格會靜默漂移)。

新增的韌體類型:`.s` / `.asm` / `.ld` / `.lds` / `.dts` / `.dtsi` / `.inc` / `.def`。
**這是 Level 1,只承諾 grep / search discoverability** —— 它們**不會**進 dense symbol
retrieval(沒有 parser,進 symbol 掃描只有零 symbol 的 walk / hash 成本)。ASM 與
linker script 的 symbol 抽取是 Level 2,整包延後:ASM 要 two-pass(先收 `.globl` /
`.global`,再只配對相應 label,排除 `.L*`),linker script 要抽 MEMORY region /
output section / `ENTRY()` / symbol assignment。寧可延後,也不要用粗 regex 製造大量
假 symbol。`.md` / `.txt` 維持既有分工:留在可見範圍供 grep / list_dir 使用,但不進
symbol 掃描。`.cfg` / `.json` / `.sh` / `.mk` 這些設定檔**仍在** symbol 掃描範圍 ——
`_scan_code_files()` 的輸出同時是 bounded context 的 `allowed_paths`,把它們排掉會讓
`config/*.cfg` 這類 gold evidence 變成讀不到。

## data flywheel 是什麼

`data_flywheel.py` 才是互動資料收集器。它預設關閉，只有設定環境變數才會寫資料：

```bash
AI_CODE_COLLECT_DATA=1 aicode
```

預設輸出：

```text
data/interactions.jsonl
```

可用 `AI_CODE_DATA_FILE=/absolute/path/interactions.jsonl` 覆寫位置。這個變數只在
`AI_CODE_COLLECT_DATA=1` 時有意義；自訂到 repo 外的路徑不受本 repo `.gitignore` 保護，
檔案可能含 NDA prompt、回答與程式片段，應放在私有目錄並限制檔案權限。

記錄內容包含 question、answer、refs、code snippets、mode、KB score、repo commit、model tag、agent tool calls、files read。這些資料在 NDA 場景通常含敏感內容；預設的 repo 內輸出已由 `.gitignore` 排除。

OpenCode/MCP server 端只記 KB-shaped tools：

- `query_knowledge`
- `query_knowledge_strict`
- `code_rag_search`

一般 plumbing tools，例如 `read_file`、`grep_code`、`apply_patch`，不會在 MCP 端逐一記完整對話。

常用命令：

```bash
python3 data_flywheel.py stats
python3 data_flywheel.py rate --file data/interactions.jsonl
python3 data_flywheel.py export --file data/interactions.jsonl --output data/training.jsonl
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

## apply_patch 架構（patch_engine.py / patch_verify.py）

`agent_tools.ToolExecutor.apply_patch` 是唯一的寫檔管線；兩種輸入格式（SEARCH/REPLACE、unified diff）
在 `patch_engine.py` 正規化成同一種 per-file plan 後，走同一條 sandbox → 上限 → preflight →
journaled 寫入 → best-effort rollback。

- `patch_engine.py`（不 import config / agent_tools；上限由呼叫端傳入）：格式偵測與嚴格 S/R
  狀態機、path 規則、`FileSnapshot`（UTF-8 strict、BOM、LF/CRLF、mode/ino/dev）、exact+rstrip
  定位、結構化 mismatch record 與單一 renderer（整次回覆的 mismatch 預覽合計 40 行／2000 字元）、
  寫入層與 `WriteJournal`。POSIX 上以 dir_fd 錨定實作（逐層 `O_DIRECTORY|O_NOFOLLOW`、同目錄
  temp、既有檔 `os.replace`、新檔以不覆蓋既存檔的方式發布）；沒有 dir_fd 的平台退回逐層 lstat
  重驗，並在結果第一段明示。已驗證的行為契約（`tests/test_apply_patch.py`）：preflight 後
  preimage 被改 → 中止並回滾（`test_preimage_changed_after_preflight_aborts_and_rolls_back`）、
  新檔目標在 preflight 後被競爭者建立 → 中止且不覆蓋
  （`test_new_target_created_by_competitor_after_preflight_aborts`）、第一檔已寫入後被第三方修改
  → rollback 保留現況並回報 conflict（`test_rollback_does_not_overwrite_third_party_modification`）、
  dir_fd 退回路徑仍擋 symlink（`test_dirfd_fallback_reports_degraded_note_and_still_blocks_symlinks`）。
  這些競態防線是以 monkeypatch 在既定時點注入變更來驗證，不是真實併發測試。
- `patch_verify.py`：套用後的自動驗證只做同 process、無 subprocess、唯讀的 syntax check
  （`.py`/`.pyi` 用 ast；C/C++ 只在釘版 tree-sitter grammar 載入時檢查 ERROR 與零寬 MISSING
  node）；三態 passed / failed / skipped，任何 skipped 都渲染成「驗證不完整」，失敗不回滾。
  import 集合由 `tests/test_patch_verify.py::test_patch_verify_module_import_allowlist_is_exact`
  用 ast 釘成 allowlist，`test_auto_verify_true_spawns_no_subprocess` 守住不 spawn；lint / test
  由 `codetrail_run_lint(fix=False)` / `codetrail_run_command` 顯式呼叫，各自經 OpenCode ask。
- `run_command` 的 `timeout` 三層同值（native schema、executor、MCP
  `Annotated[int, Field(strict=True, ge=1, le=600)]`），常數在 `config.RUN_COMMAND_TIMEOUT{,_MIN,_MAX}`；
  `scripts/check_readme_consistency.py` 第 9–11 條把 5／200、1..600、dry_run 七欄位與驗證分層
  宣稱釘在 MCP docstring、native description 與文件上。

### Tool contract、build prompt 與部署 canary

| 模組／入口 | 契約 |
|---|---|
| `mcp_contract.py` | `PUBLIC_TOOL_ORDER` 是 live 19-tool 名稱與順序唯一來源；同檔也定義 bounded FastMCP instructions 與 evidence-tool 集合。 |
| `tool_result_adapter.py` | 每個 tool call 都產生單一 compact text block；首行 `status: ok|partial|error`，需要修復／續讀時才有 `next:`。省略 `max_chars` 時以 call-time `config.N_CTX` 的 12% token proxy 配置，明示過大值標 `context_risk`；三個 evidence tool 保留未改 core structured payload。 |
| `scripts/opencode_build_prompt.py` | 從 `docs/opencode-build-prompt.md` 唯一 fenced block 抽 canonical body；一般安裝不新增 prompt，只有 `--enable-experimental-build-prompt` 明確 opt-in。之後舊 managed reference 才同步，custom string 保留、型別錯誤 fail-loud。prompt `0644` 與 config `0600` 同 transaction／symlink write-through／rollback。synthetic composition 只證明取代 default；完整 routing A/B 已失敗，不能宣稱 supported。 |
| `scripts/opencode_direct_contract.py` | 在任何 writer/MCP/model 前只讀驗證 OpenCode `>=1.17,<2` direct contract；V2 `mcp.servers`、任何 `codemode` 或無法解析版本都 exit 2。 |
| `scripts/tool_call_canary.py` | live MCP protocol；explicit 點名工具 hard gate（retry 一次）；implicit 未點名工具單次診斷，四態 `optimal/suboptimal/fail/timeout` 不擋啟動。schema 2 分離 cache lane 只存 hash/status/time/version；`supports_tools=false` 在 model attempt 前 fail。 |
| `scripts/mcp_catalog.py`／`scripts/eval_tool_routing.py` | effective stdio catalog、privacy-safe routing classification/gates 與 frozen historical baseline replay；harness 永不自行把 matrix row 升級成 supported。 |

部署 live-after 不進 CI，也不是所有開發環境必綠。受授權且相容的乾淨部署才執行
`AICODE_TOOL_CANARY_FORCE=1 aicode` 與 routing eval 真模型 arm，記錄 OpenCode／MCP SDK、
模型／chat template／effective config、explicit 與 implicit 結果。環境不可得時逐字回報
`not run: environment unavailable`；未授權、未跑或 gate 未通過都保持 incomplete，不能阻擋
離線驗收，也不能宣稱 `supported`。

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
| `code_context.py` | `code_rag_search(mode="context")` 的 deterministic 程式證據選取/overlap merge/content dedupe/字元裝箱。它不是 LLM context hard gate；source I/O 由呼叫端注入既有 `ToolExecutor.grep/read_file`，本模組不裸讀檔。明示 `max_chars` 的合法範圍固定為 `2000..30000`；MCP 省略時由 transport wrapper 依 call-time `n_ctx` 的 12% 配置（direct core 才保留歷史 12,000 fallback）。`used_chars` 只加總 `evidence[].text`；candidate cap、graph traversal cap 與 character budget 各自如實標示，uncertainties 去重且有顯式上限。 |
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
`tests/test_context_budget.py::test_trim_messages_emits_telemetry_metadata_only` 是
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
- `knowledge.py` 的三個主模型 call site(query expansion / multi-query / LLM rerank)
  已經走 `_gated_completion`,那是它們的唯一出口。**不要**再從那個檔直接呼
  `llama_client.native_completion`——`_rerank_with_llm` 會把 15 個候選各 500 字塞進
  prompt,超了之後 llama-server 是從前面截掉,模型看到半份清單照樣回一組 DOC_n。
- `code_rag.py` 與 `media.py` / `RAG.py` / `figure_verify.py` **刻意不接**這個 gate,
  理由不是「還沒做」:
  - `code_rag.py` 只打 `/embedding` 與 `/reranking`,那是另外兩台 server 的 input
    limit,不是主模型 n_ctx。它們各自有明確的字元預算
    (`CODE_RAG_EMBED_TEXT_MAX_CHARS` / `CODE_RERANK_PASSAGE_MAX_CHARS` /
    `EMBED_BATCH_MAX_CHARS`)。拿主模型的 n_ctx 去擋這幾條是錯的閘。
  - VL 那幾條的 prompt 全是固定模板字串(唯一的變數是 table 的 grid hint,幾百字),
    真正吃 ctx 的是影像 token,而 `CHARS_PER_TOKEN` heuristic 量不到影像。在那裡
    掛 gate 只會產生「已檢查」的假象。VL 的 ctx 由 server 啟動的 `-c` 與
    `deployment_profile` 的 `services.vl.ctx` 管。
- OpenCode TUI 主對話完全在 CodeTrail 視線外,doctor 只能驗 config 對齊,不能驗實際 prompt 是否爆。

---

## 索引範圍 (index scope)

**只影響 Code RAG 索引。`grep_code` / `list_dir` 完全不受影響** —— 檔案還是找得到、還是
grep 得到,只是不再吃掉語意檢索的名額。這條界線是刻意的:使用者以為檔案不見了比雜訊更糟。

### 不變式

> 檔案是否進索引,**由且僅由** `index_scope.IndexScope.should_index_file(rel)` 決定。
> `decide_dir()` 的三態(`PRUNE` / `TRAVERSE_ONLY` / `INDEX`)只是剪枝優化,必須保守:
> `PRUNE` 僅在「其下不可能存在任何能通過 `should_index_file` 的檔案」時才允許。

`tests/test_code_rag_index.py::test_tri_state_walk_matches_should_index_file` 對合成樹全量
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
  向量鎖在 `tests/test_code_rag_index.py::test_matcher_vectors`。

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
AICODE_KB_CONTEXT_GENERATE=1 python3 RAG.py rebuild --kb knowledge.json spec_a.pdf
python3 RAG.py rebuild --kb knowledge.json spec_a.pdf --context      # 旗標 > config
python3 RAG.py rebuild --kb knowledge.json spec_a.pdf --no-context   # 這次不生成

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
python3 scripts/kb_ab_compare.py ~/proj/knowledge.json                       # 體檢
python3 scripts/kb_ab_compare.py old/knowledge.json new/knowledge.json       # 重建前後對照
python3 scripts/kb_ab_compare.py old/knowledge.json new/knowledge.json \
    --questions ~/questions.txt                                             # 加跑真題
```

**兩份 KB 一定要放不同目錄**：工具會直接擋下同目錄的組合，不會靜默比錯。（2026-08-24
起 embeddings cache 依 KB 檔名分目錄，同目錄兩份 JSON 其實已經不會互相覆蓋向量了；
這條限制留著只是保守，兩份 KB 分開放本來就比較好對照。）另外體檢一律以
`allow_rebuild=False` 載入：它要報告的是「這份 KB 現在的磁碟狀態能不能直接載入」，
自動重建會把答案改掉。預設只印 metadata 與計數，
不印 chunk 內容（NDA）；要看抽樣前綴得自己加 `--show-content`。問題檔與真實文件都
不進 repo。

重建一份對照 KB 就是把來源文件逐一灌進獨立目錄：

```bash
mkdir -p /tmp/kb-baseline && cd /tmp/kb-baseline
for f in <doc1> <doc2>; do python3 /path/to/CodeTrail/RAG.py "$f" ./knowledge.json; done
```

（embedding 快取是 CWD 下的 `.rag_embedding_cache.json`；把舊的複製進來可大幅減少
重算，內容沒變的 chunk 會直接命中。）

---

### scripts/index_stats.py

完全唯讀、完全離線的計數工具。**預設輸出只有計數,不含任何路徑** —— 這種輸出會被貼進
issue,路徑本身就是 NDA 內容。

```bash
AICODE_ROOT=/path/to/tree python3 scripts/index_stats.py
python3 scripts/index_stats.py --root /path/to/tree --deep        # 真的跑 AST 算符號數
python3 scripts/index_stats.py --root /path/to/tree --show-paths  # 顯式 opt-in 才印路徑樣本
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
- `tests/test_repo_consistency.py`（含 `eval/run_eval.py --help` 的 smoke test）
- `README.md`、`README_DEV.md` 裡的 eval 說明

若刪 data flywheel，至少同步處理：

- `data_flywheel.py`
- `mcp_server.py` 裡 `_record_kb_interaction` 接線
- `README.md`、`README_DEV.md` 裡的資料飛輪說明

目前建議先保留：它們不影響 OpenCode 日常使用，但對之後把工具做成更成熟的私有產品有價值。
