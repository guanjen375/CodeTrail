# AGENTS.md — 給 AI coding agent 的工作規範

這個 repo 是一個 **本地 RAG / Code-RAG / MCP 工具集**。終端使用者透過 OpenCode TUI
或 `python main.py` CLI 連到這個專案，用本地 Ollama 模型分析 NDA / 內部 firmware repo。

如果你是 AI agent（Claude Code / Codex / OpenCode 等）正在改這個 repo，請先把這份檔讀完。

---

## 1. 這個 repo 的定位

- **不是** library，不是要 publish 到 PyPI。
- **是** 個人工程工具，重視可修改性、可測試性，但**安全層的東西不要砍**。
- 主要 entry point 兩個：
  - `main.py` — CLI（`--qa`, `--agent`, `--full`, `--mcp`, `--web`）
  - `mcp_server.py` — MCP server（OpenCode / Claude Code 用 stdio 接）

---

## 2. 常用命令

```bash
# Help（永遠不該 crash 或進互動模式）
python main.py --help

# 本地驗證（不需要 Ollama）
python -m compileall -q .
python scripts/run_tests.py
python scripts/check_eval_consistency.py

# Lint（advisory，CI 不擋）
ruff check tests scripts

# 真的跑 QA / agent（需要 Ollama 在 localhost:11434）
python main.py --qa "解釋 segfault"
python main.py /path/to/repo "整體架構"
```

---

## 3. 改 config / docs / eval 時要同步檢查

`config.py`、`README.md`、`eval/*.json`、`mcp_server.py` 的工具清單必須對齊。

**改這些任一處都要跑：**

```bash
python scripts/check_eval_consistency.py
python scripts/run_tests.py tests/test_eval_consistency.py
```

漂移範例（已修，避免再犯）：
- 改 `RERANKER_TOP_N` → 對應的 `eval/spec_holdout.json` gold_evidence 也要改
- `_parse_unified_diff` 從 `agent.py` 搬到 `agent_tools.py` → `eval/code_questions.json` 的 `file` 要改
- 換 `EMBEDDING_MODEL` → `eval/spec_adversarial.json` 也要改

如果你在 eval 裡放 line number，**只當作 hint，誤差 ±20 行內視為正確**；
不要把 line number 當成嚴格契約。

---

## 4. 禁止事項

### 4.1 安全相關不要砍
- `agent_tools.ToolExecutor._safe_path` — 所有檔案讀寫的 sandbox 入口
- `media._safe_path` — 圖片/ELF/binary 的 sandbox 入口
- `agent_tools._validate_command` — run_command 白名單 + dangerous-pattern 過濾
- `apply_patch` 的「context 必須匹配」、「max files / max lines」邏輯
- `mcp_server.py` 啟動時 `set_sandbox_root(AICODE_ROOT, allow_external=False)`

任何重構碰到上面這些東西，**新加測試**，不要直接刪 / weaken / 移除檢查點。

### 4.2 不要做的事
- 不要在 `main.py` import 階段做掃專案、init 模型這類重活。
- 不要把 `from config import X`（snapshot）混 `import config; config.X = ...`（mutation）— 動態值只用 `import config`。
- 不要為了讓 lint 漂亮，刪未檢查影響的 unused import — 有些是 side-effect import。
- 不要把 ALLOWED_COMMANDS 加 `rm` / `sudo` / `curl` / `bash`。
- 不要把 `RUN_COMMAND_ENABLED` / `PATCH_ENABLED` 預設改成 `True`。它們必須要靠 CLI flag 或 env 才開。
- 不要 `git commit` 沒被使用者確認過的修改。

### 4.3 預設離線
- CI 不可以依賴 ollama / GPU / 大型模型下載。
- 任何測試用到 LLM 都要 mock 或 graceful skip（`pytest.importorskip` 或 `pytest.skip`）。

---

## 5. 加新功能的標準流程

1. 改程式碼。
2. 新加或更新 tests（至少：CLI smoke、安全邏輯、edge case）。
3. 跑 `python scripts/run_tests.py`、`python scripts/check_eval_consistency.py`、
   `python -m compileall -q .` — 三個都過才送 PR / 提交。
4. 如果改了 `config.py` / MCP tool schema / `README.md`，再跑一次 eval consistency。

---

## 6. 測試指南

- `tests/test_cli.py` — CLI 不該 crash 的最低保證
- `tests/test_config.py` — config 數值的範圍與型別 sanity
- `tests/test_sandbox.py` — `_safe_path` 不會被 `..` / 絕對路徑 / symlink 騙過
- `tests/test_patch.py` — apply_patch 的 happy path、逃逸、context 不符、max 限制
- `tests/test_run_command.py` — 白名單 + shell 元字元 + 注入防護
- `tests/test_eval_consistency.py` — eval ↔ config / source 不漂移

新功能 → 新 test 檔；不要塞舊 test 檔。

---

## 7. 知道你在改什麼

如果你不確定一個檔的角色，先讀 `README.md` 的 §1（系統架構）和 §4（11 個 MCP 工具）。
不要在 mcp_server.py 加新 tool 卻沒同步更新 README 工具清單 — 模型會誤用，使用者也會困惑。

---

## 8. NDA / 機敏資料

- `knowledge.json`、`data/`、`*.jsonl`、`.code_rag_cache_*` 全部在 `.gitignore` 裡。
- 任何 PR 都不能 commit 這些檔。
- 如果你 grep 到 NDA 客戶名 / 規格書檔名 hardcode 在程式碼裡，**那是 bug**，要報告。

---

## 9. Generated section（未來）

目前沒有 auto-generated docs。如果你之後加了 `scripts/generate_docs.py`，
請在 README.md 對應段落用 markdown comment 包起來：

```
<!-- BEGIN_GENERATED:tool-table -->
... 自動生成內容 ...
<!-- END_GENERATED:tool-table -->
```

agent **不要** 手動修這些 marker 之間的內容。
