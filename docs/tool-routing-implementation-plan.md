# MCP 工具使用可靠性：實作切分與介面凍結

本文件是 `/home/david/workflow.md` 的開工契約。它只定義子任務邊界、對外介面、
驗收條件與審核流程；規劃 commit 完成前不修改產品程式碼。

## 基線與角色

- 規劃基準：`53c8386a746bf8760acee0efeda9b08801018871`。
- 實作者角色：developer。開發途中不跑 pytest；收斂後只跑一次 smoke。
- 靜態審核者：Claude Code 2.1.246。審核者只讀檔、只報問題，不修改 code。
- full test 不在本工作執行；只有使用者明示 `ROLE=REVIEWER` 才能執行。
- 不執行真模型 routing eval，除非使用者另行明示授權。

## 全域不可變條件

1. 公開工具仍是 19 個，名稱、一般有效輸入與 ask 權限保持相容；不新增 gateway。
2. `AGENTS.md` §3 的 sandbox、command、patch、KB identity、completion gate、safe regex
   全部保留，不放寬。
3. `tests/test_repo_consistency.py` 的既有安全 assertion 不修改、不刪除、不改弱；
   不用 skip、xfail 或容忍值放寬過關。
4. `tests/test_code_rag_search.py` 的 core payload shape 與 `CONTEXT_KEYS` 不變。
5. CI、smoke 與 full 不依賴 OpenCode、llama-server、GPU、網路或 NDA 資料。
6. eval fixture 只含合成資料；持久結果不得含 prompt、root/path、tool args、tool output、
   session id 或其他可能辨識專案的內容。
7. 不升級 MCP SDK major；本次以 `mcp>=1.28,<2` 的 direct native-tool contract 為準。

## 共用對外介面

### 公開工具順序

新增 `mcp_contract.py`，提供唯一來源：

```python
PUBLIC_TOOL_ORDER: tuple[str, ...]
PUBLIC_TOOL_NAMES: frozenset[str]
```

`PUBLIC_TOOL_ORDER` 固定為：

```text
list_dir, read_file, grep_code, code_rag_search, file_info,
query_knowledge, query_knowledge_strict, git_status, git_diff,
apply_patch, run_lint, run_command, analyze_file, ingest_document,
remove_document, reload_knowledge_base, review_figures,
import_external_file, record_lesson
```

MCP server、canary、eval 與文件 consistency checker 都只能消費這份常數，不各寫一份。

### closed-set 與數值 schema

model-visible 參數全部使用 `Annotated[..., Field(description="...")]`；description 是一行
英文 machine-level 說明。closed-set 使用下列 `Literal`：

- `code_rag_search.mode`: `semantic|neighbors|path|context`
- `analyze_file.view`: `summary|headers|sections|memmap|symbols|imports|relocs|dynamic|dwarf|disasm|strings`
- `ingest_document.mode`: `auto|document|image|chat|binary`
- `review_figures.action`: `list|fix`
- `record_lesson.scope`: `project|global`

數值 min/max 採「既有有效邊界」而不是任意發明新容量：

- `hops=1..2`、`grep.context=0..5`、`list_dir.depth=0..MAX_LIST_DEPTH`
- `code_rag_search.max_chars=2000..30000`
- `analyze_file.limit=0..BIN_ELF_VIEW_MAX_LIMIT`
- `run_command.timeout=1..600` 且 `strict=True`
- `read_file.start_line/end_line` 與 `review_figures.expected_revision` 以正整數語意加上
  JSON integer 的保守上界；`expected_revision=0` 仍保留給 `action=list`
- `top_k` 的公開範圍固定 `1..50`
- `read_file/list_dir` 的 explicit `max_chars` 分別受既有 50,000／20,000 字元安全上限

超出既有有效範圍而過去會被內部 clamp 的值，改由 schema fail-loud；參數名稱、型別與
正常範圍內的呼叫保持相容。

### MCP result adapter

新增 `tool_result_adapter.py`：

```python
@dataclass(frozen=True)
class ResultBudget:
    token_limit: int
    char_limit: int
    explicit: bool
    context_risk: bool

def estimate_result_tokens(text: str) -> int: ...
def resolve_result_budget(*, n_ctx: int, requested_max_chars: int | None,
                          safety_max_chars: int) -> ResultBudget: ...
def adapt_tool_result(tool_name: str, core_payload: object, *,
                      budget: ResultBudget) -> CallToolResult: ...
```

估算固定為 `ceil(ASCII / 3) + ceil(non-ASCII * 1.5)`；省略 `max_chars` 時 token budget
固定為 call-time `config.N_CTX` 的 12%。`config.N_CTX` 必須經 `import config` 動態取值。
明示較大值仍受既有 tool safety cap，並在文字結果標示 context risk。

為辨識省略與明示，model-visible `max_chars` 預設改為 `None`；direct/core Python 函式仍
保留既有預設與 payload。`_tool` 註冊 transport wrapper，但 decorator 回傳 raw core
callable，避免改變 core test 與內部呼叫。

MCP `content` 恰好一個 `TextContent`：首行是 `status: ok|partial|error`；只有
partial/error/truncated 才有第二行 `next:`。錯誤修復指引不得只存在
`structuredContent`。三個 evidence tools 為 `code_rag_search`、`query_knowledge`、
`query_knowledge_strict`；它們保留既有 structured core，其他文字工具設
`structured_output=False` 並移除 `{"result": string}` outputSchema。

### OpenCode build prompt

新增 `scripts/opencode_build_prompt.py` 與 `docs/opencode-build-prompt.md`，提供：

```python
BUILD_PROMPT_MAX_CHARS = 1500
BUILD_PROMPT_RELATIVE_PATH = Path(".config/codetrail/opencode-build-prompt.md")
def extract_build_prompt(text: str) -> str: ...
def build_prompt_path(home: Path) -> Path: ...
def build_prompt_reference(path: Path) -> str: ...
def apply_build_prompt_contract(data: dict[str, Any], reference: str
                                ) -> tuple[list[str], list[str], list[str]]: ...
```

設定值使用 OpenCode 的 `"{file:/absolute/path}"` reference。缺值時補上；canonical managed
reference 可更新；其他既有值視為使用者自訂，只警告、不覆寫。錯誤型別 fail-loud。
prompt file 納入 `set_config` 同一 transaction／backup／restore，mode 0644；
`opencode.json` 維持 0600。

### direct-tool compatibility

新增 `scripts/opencode_direct_contract.py`：

```python
MIN_OPENCODE_VERSION = (1, 17, 0)
class DirectToolContractError(RuntimeError): ...
def parse_opencode_version(raw: str) -> tuple[int, int, int]: ...
def require_direct_tool_contract(version_raw: str,
                                 effective_config: Mapping[str, Any]) -> None: ...
```

OpenCode `<1.17.0`、major `>=2`、版本不可解析、effective config 出現 `mcp.servers` 或
`codemode` 都 fail-loud，且發生在任何 config `--fix`、MCP subprocess 或 model canary
之前。訊息必須說明 direct `codetrail_*`、支援範圍、V2 的
`mcp.servers.codetrail`／`codemode:false`／`disabled`／execution timeout 差異，以及
Code Mode 不在本規格。

### explicit／implicit canary

`scripts/tool_call_canary.py` 對外資料型別：

```python
class ImplicitStatus(str, Enum):
    OPTIMAL = "optimal"
    SUBOPTIMAL = "suboptimal"
    FAIL = "fail"
    TIMEOUT = "timeout"

@dataclass(frozen=True)
class ProtocolEvidence:
    tools_digest: str
    instructions_digest: str

@dataclass(frozen=True)
class ImplicitEvidence:
    status: ImplicitStatus
    session_ids: tuple[str, ...] = ()
```

explicit timeout 預設 120 秒，仍要求 completed
`codetrail_list_dir(path=".", depth=1)`，失敗硬擋；可重試一次，第二次成功仍為 flaky 且
不快取。implicit timeout 預設 180 秒，只跑一次，prompt 不含工具名；完成 list_dir 且
path 為 `.`／空字串／`./` 是 optimal，其他明確 allowlist 的唯讀 CodeTrail tool 是
suboptimal，否則 fail；suboptimal/fail/timeout 只警告。

cache schema 2 分成 `explicit` 與 `implicit`，每筆只存 fingerprint、status、時間與版本。
fingerprint 納入完整 `chat_template_caps`、`build_info`、canonical live `tools/list` digest、
MCP instructions、effective build prompt、AGENTS 與 lessons digest。schema 1 視為 miss。
`chat_template_caps.supports_tools is False` 時，在 model subprocess 前立即失敗；欄位缺失
才繼續 explicit probe。

### routing eval CLI

新增：

```text
python3 scripts/eval_tool_routing.py --root ROOT --matrix-row ROW_ID \
  --arm ARM --output RESULT.json [--model provider/model] [--catalog-only]
```

以及 `eval/fixtures/tool_routing/cases.json`、`support_matrix.json`。真模型路徑必須顯式給
`--matrix-row`；catalog-only 不呼叫模型。live catalog 由 effective stdio MCP 的
`initialize/tools/list` 取得，CI 則可用 in-process `mcp.list_tools()`，不得用 AST 量 chars。

## 平行子任務

### T1 — live catalog、routing eval 與 support matrix

Ownership：

- `scripts/mcp_catalog.py`（新增）
- `scripts/eval_tool_routing.py`（新增）
- `eval/fixtures/tool_routing/cases.json`（新增）
- `eval/fixtures/tool_routing/support_matrix.json`（新增）
- `tests/test_evals.py`（新增；純合成 classifier/privacy contract）

驗收：

1. 重測並保存 live baseline：description、inputSchema、outputSchema、catalog、OpenCode
   effective chars 與 instructions；schema canonical JSON 計數規則固定。
2. tools/no-tools 使用相同最小 message 與 `max_tokens=1`；優先取
   `usage.prompt_tokens` 差，缺 usage 才走 `/apply-template` + `/tokenize`。
3. 中英文 fixture 覆蓋 directory、exact grep、read、cross-file、spec、missing-data refusal、
   no-tool，以及中文問題＋英文 identifier 且 CodeRAG query ASCII ≥90%。
4. classifier 只接受 completed structured event；七類事件、marker leak、第三次相同呼叫、
   terminal retry 與 `harness_invalid` 分母排除都可 deterministic 重播。
5. result JSON 只留 case id、classification、聚合值、token/latency/compaction 與相容性身分。
6. `measured` 不會自動升 `supported`；每列依自己的 baseline 套 workflow 全部 gate。

依賴：可先完成 harness；after 與 gate 必須等 T2/T3 完成。真模型執行另需使用者授權。

### T2 — MCP catalog、typed schema 與 compact result

Ownership：

- `mcp_contract.py`（新增）
- `tool_result_adapter.py`（新增）
- `mcp_server.py`
- `docs/mcp-tools.md`
- `scripts/check_readme_consistency.py`
- `tests/test_mcp_server.py`（新增）
- `tests/test_mcp_server.py`（新增）
- 經明示核准後：`tests/test_figure_retrieval.py`、
  `tests/test_mcp_server.py`

驗收：

1. live 19-tool 名稱與順序精確；description 合計 ≤12,000、單項 ≤1,600、apply_patch
   ≤2,200、description + inputSchema ≤20,000；FastMCP instructions ≤700。
2. 每個參數有英文 Field description；closed-set 有 enum；數值有 min/max。
3. apply_patch 與 run_command 的既有安全句、限制與最小例完整保留。
4. 三個 evidence adapter 同時回單一 compact TextContent 與未變的 structured core；
   string tools 沒有 outputSchema。
5. query renderer 不重複 text/display/refs，但保留 source/page、excluded figures、
   review hint、uncertainties、truncated；read_file continuation 行號精確；grep 提供可操作的
   path/include/pattern 縮窄提示；repeat guard 仍可見且不破壞 status 首行。
6. 指定 smoke nodes 存在：
   `test_live_catalog_is_bounded_typed_and_ordered`、`test_default_budget_tracks_n_ctx`。

### T3 — build prompt 與 config contract

Ownership：

- `docs/opencode-build-prompt.md`（新增）
- `scripts/opencode_build_prompt.py`（新增）
- `scripts/set_config.py`
- `scripts/opencode_contract_check.py`
- `tests/test_set_config.py`
- `tests/test_opencode_checks.py`

驗收：

1. canonical prompt ≤1,500 chars，保留簡潔回答、平行唯讀查詢、先查再改、無證據拒答；
   不教授 bare bash/read/grep/glob/edit/task，且不誤傷 `codetrail_read_file` 等 schema 名。
2. 新裝、重跑、custom prompt、型別錯誤、symlink、permission 與 transaction 行為明確。
3. synthetic request log 證明 build prompt 取代而非附加 OpenCode default prompt。
4. `tests/test_opencode_checks.py::test_build_prompt_never_teaches_denied_tools` 標 smoke。
5. `todowrite: deny` 與 build prompt 是否成為預設，只能依授權後的 A/B gate 決定；未達 gate
   就保留現況且不宣稱 supported。

### T4 — compatibility、兩層 canary、launcher 與 doctor

Ownership：

- `scripts/opencode_direct_contract.py`（新增）
- `scripts/tool_call_canary.py`
- `scripts/doctor.py`
- `aicode`
- `tests/test_doctor.py`
- `tests/test_doctor.py`
- `tests/test_aicode.py`

驗收：

1. 不相容 client 在任何 writer/MCP/model subprocess 前停止；standalone TUI/web 都走 gate，
   attach 保持薄 client。
2. explicit failure exit 2；implicit 四態不擋啟動，doctor 對 current fingerprint 顯示
   optimal/suboptimal/fail/timeout，未知資料不拿別列冒充。
3. cache 與 fingerprint 符合前述 privacy／identity contract。
4. `supports_tools=false` 不執行 model attempt；缺欄位才繼續探針。
5. `tests/test_doctor.py::test_explicit_gate_and_implicit_diagnostic_are_separate`
   標 smoke。

### T5 — 文件與 acceptance integration

T1–T4 收斂後才開始；避免平行改同一文件。

Ownership：

- `README.md`
- `README_DEV.md`
- `docs/basic-usage.md`
- `docs/troubleshooting.md`
- `docs/security.md`
- `docs/opencode-agents-template.md`（只改 fenced block 外 manifest/說明）
- `tests/test_repo_consistency.py`（只新增 assertion，既有 assertion 不動）
- `tests/test_smoke_gate.py`（精確登記本次新增的安全/契約 smoke nodes）

驗收：

1. README、MCP docs、troubleshooting、permission template、canary contract 與 live schema
   一致；全域 AGENTS 安裝範本仍 ≤1,600 chars 且沒有完整工具目錄。
2. static consistency 與 compile checks 通過。
3. developer 交付前只執行一次 `python3 scripts/run_tests.py -m smoke`；零收集不算通過。
4. live after、A/B arm 決策與 gate 結果齊全；未授權或未達 gate時明確列為未完成，
   不宣稱 supported。

## Claude Code 審核契約

每個 T1–T5 都走一次且只有一次子任務審核：

1. 實作者完成該 task 的 code 與靜態檢查。
2. 以 Claude Code non-interactive 模式審核，工具限制為 read/grep/glob；不給 edit/write。
3. prompt 指定 `/home/david/workflow.md`、本文件、task ownership 與 acceptance，要求只報問題，
   每條標 `BLOCKER` 或 `NON_BLOCKER`，不得改檔。
4. 實作者只修 `BLOCKER`；`NON_BLOCKER` 原樣記錄，不順手處理，也不做第二次 task review。
5. 每次審核回報 reviewer 版本、task、verdict、BLOCKER 清單與實作者處置。

全部 task 完成後，由 Claude Code 做 N 輪全案總審：每輪都立即回報；只修 BLOCKER，
有修改才進下一輪，直到某輪沒有 BLOCKER。總審不取代子任務審核，也不取代 pytest reviewer。

## 開工前必須取得的明示核准

### A. workflow 已列的 10 個既有 test node

- `tests/test_figure_retrieval.py::test_query_knowledge_carries_excluded_figures`
- `tests/test_figure_retrieval.py::test_query_knowledge_not_loaded_still_has_the_key`
- `tests/test_figure_retrieval.py::test_strict_return_paths_all_carry_excluded_figures`
- `tests/test_figure_retrieval.py::test_legacy_raster_exclusion_is_not_sent_to_review_figures`
- `tests/test_figure_retrieval.py::test_structured_and_legacy_exclusions_are_reported_separately`
- `tests/test_figure_retrieval.py::test_strict_kb_not_loaded_still_has_the_key`
- `tests/test_doctor.py::test_cache_contains_only_fingerprint_metadata_and_is_private`
- `tests/test_doctor.py::test_successful_model_canary_is_cached_and_skips_second_call`
- `tests/test_doctor.py::test_retry_success_is_reported_flaky_and_not_cached`
- `tests/test_doctor.py::test_two_model_failures_block_by_default_and_warn_only_can_continue`

核准後仍只能把原 assertion 移到 core payload／新 lane 契約，維持同等或更強；不得刪弱。

### B. 盤點新增發現的第 11 個既有 test node

- `tests/test_mcp_server.py::test_mcp_protocol_roundtrip`

理由：它目前鎖死 public `code_rag_search.max_chars` 為 integer、default `12000`、min 2000、
max 30000。FastMCP 1.28 會把預設值補入 kwargs；要真正分辨「省略」並套 n_ctx 12%，
public default 必須改 `None`。核准後保留 min/max 強度，並新增 transport-level dynamic
default assertion。

### C. 真模型 support-matrix eval

需要使用者明示允許執行本機 OpenCode／llama-server routing A/B。未取得授權前只重測 live
MCP catalog baseline、完成離線 harness 與合成 fixture，不啟動真模型 eval，也不把任何 arm
升為預設或把 matrix row 標為 supported。

