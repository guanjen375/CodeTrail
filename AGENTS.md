# AGENTS.md — 給 AI coding agent 的工作規範

這個 repo 是一個 **本地 RAG / Code-RAG / MCP 工具集**。終端使用者透過 OpenCode TUI
和 `aicode` wrapper 連到這個專案，用本地 Ollama 模型分析 NDA / 內部 firmware repo。

如果你是 AI agent（Claude Code / Codex / OpenCode 等）正在改這個 repo，請先把這份檔讀完。
維護命令、測試流程、eval 漂移檢查見 [README_DEV.md](README_DEV.md)。

---

## 1. 這個 repo 的定位

- **不是** library，不是要 publish 到 PyPI。
- **是** 個人工程工具，重視可修改性、可測試性，但**安全層的東西不要砍**。
- 使用者 entry point 只保留一個：
  - `aicode` — wrapper，從目前目錄啟動 OpenCode 並設定 `AICODE_ROOT`
- Runtime entry point：
  - `mcp_server.py` — MCP server（OpenCode / Claude Code 用 stdio 接）

---

## 2. 禁止事項

### 2.1 安全相關不要砍
- `agent_tools.ToolExecutor._safe_path` — 所有檔案讀寫的 sandbox 入口
- `media._safe_path` — 圖片/ELF/binary 的 sandbox 入口
- `agent_tools._validate_command` — run_command 白名單 + dangerous-pattern 過濾
- `apply_patch` 的「context 必須匹配」、「max files / max lines」邏輯
- `mcp_server.py` 啟動時 `set_sandbox_root(AICODE_ROOT, allow_external=False)`

任何重構碰到上面這些東西，**新加測試**，不要直接刪 / weaken / 移除檢查點。

### 2.2 不要做的事
- 不要把 `from config import X`（snapshot）混 `import config; config.X = ...`（mutation）— 動態值只用 `import config`。
- 不要為了讓 lint 漂亮，刪未檢查影響的 unused import — 有些是 side-effect import。
- 不要把 ALLOWED_COMMANDS 加 `rm` / `sudo` / `curl` / `bash`。
- 不要把 `RUN_COMMAND_ENABLED` / `PATCH_ENABLED` 在 `config.py` 的預設改成 `True`。OpenCode runtime 若要開，必須維持在 `mcp_server.py` 這類明確啟動點。
- 不要在 `mcp_server.py` 加新 tool 卻沒同步更新 `README.md` 工具清單 — 模型會誤用，使用者也會困惑。
- 不要 `git commit` 沒被使用者確認過的修改。

### 2.3 預設離線
- CI 不可以依賴 ollama / GPU / 大型模型下載。
- 任何測試用到 LLM 都要 mock 或 graceful skip（`pytest.importorskip` 或 `pytest.skip`）。

---

## 3. NDA / 機敏資料

- `knowledge.json`、`data/`、`*.jsonl`、`.code_rag_cache_*` 全部在 `.gitignore` 裡。
- 任何 PR 都不能 commit 這些檔。
- 如果你 grep 到 NDA 客戶名 / 規格書檔名 hardcode 在程式碼裡，**那是 bug**，要報告。
