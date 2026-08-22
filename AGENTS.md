# AGENTS.md — 給 AI coding agent 的工作規範

這個 repo 是一個 **本地 RAG / Code-RAG / MCP 工具集**。終端使用者透過 OpenCode TUI
和 `aicode` wrapper（或薄的 `aicode_web` 背景 launcher）連到這個專案，用本地 llama.cpp `llama-server` 跑模型,
分析 NDA / 內部 firmware repo。

如果你是 AI coding agent（Codex / OpenCode 等）正在改這個 repo，請先把這份檔讀完。
維護命令、eval 漂移檢查見 [README_DEV.md](README_DEV.md)——那份檔是**閱讀用參考**，
裡面的測試命令誰能執行由角色決定（見 §2）。

---

## 1. 這個 repo 的定位

- **不是** library，不是要 publish 到 PyPI。
- **是** 個人工程工具，重視可修改性、可測試性，但**安全層的東西不要砍**。
- 核心 wrapper 只保留一個：
  - `aicode` — 從目前目錄啟動 OpenCode 並設定 `AICODE_ROOT`
  - `aicode_web` 只是跨機瀏覽器便利入口（背景 tmux 包裝內建於同一支腳本），backend 最終仍走 `aicode web` 的完整安全前置
- Runtime entry point：
  - `mcp_server.py` — MCP server（OpenCode / MCP client 用 stdio 接）

---

## 2. 測試 policy

### 2.1 兩包制

- **smoke** ＝ 標 `@pytest.mark.smoke` 的測試：真實發生過的 bug 的 regression ＋ 無聲失敗風險的契約檢查。
  §3 的每個安全檢查點都必須在裡面（由 `tests/test_smoke_gate.py` 靜態守住）。整包目標 10 秒內。
- **full** ＝ 整個 `tests/`。
- 統一入口 `python3 scripts/run_tests.py`（無參數＝full，最多 8-shard 並行；帶任何 pytest 參數＝單行程逐字轉發）：
  - smoke：`python3 scripts/run_tests.py -m smoke`
  - full：`python3 scripts/run_tests.py`

### 2.2 執行權責

- **開發者**（預設角色）：改碼過程**不執行測試**。只允許兩種執行：
  1. 交付前跑一次 smoke。
  2. 修 bug 時單跑自己新寫的那條 regression test（見 §2.3）。
  除此之外禁止執行測試及任何會間接觸發測試的命令。交付時註明：
  `Tests: smoke only — reviewer owns full execution.`
- **審核者**（僅限使用者在本次 prompt 明示 `ROLE=REVIEWER`）：先完成靜態審核並集中提出問題；
  程式碼收斂後對目前 HEAD 執行一次 full，回報命令、結果與 HEAD。程式碼未變時不得重跑已通過的測試。
- **判定規則**：失敗 node ID 集合不得大於動工前基線
  （缺 tty / `llama-server` 執行檔的環境，環境相依測試的既有失敗屬於基線）。
  基線外任何新失敗＝未完成，不得回報成功。
  `0 tests collected`（pytest exit code 5）不是通過，必須回報異常。

### 2.3 修 bug 鐵則：red-before-green

1. 先寫 regression test，在**未修改**的程式碼上單跑它
   （`python3 scripts/run_tests.py tests/test_x.py::test_y`），貼出紅燈輸出節錄。
2. 再修程式碼，同一條測試轉綠，貼出綠燈節錄。
3. 交付內容＝紅燈證據＋綠燈證據＋diff。缺紅燈證據的 bug fix 一律視為未驗證。

這類 regression test 一律標 `@pytest.mark.smoke`。

### 2.4 什麼時候寫新測試

只有兩種情況：
1. 真實發生過的 bug → regression（走 §2.3）。
2. 無聲失敗風險的契約，含 §3 安全層檢查點的防護測試。

其餘一律不寫：不追 coverage 數字、不為新功能寫儀式性測試、不為 parser 寫 parser。

### 2.5 既有測試不得改弱

- 既有 assertion 不得修改或刪除；不得用 `skip` / `xfail` / 放寬容忍值讓測試通過。
- 確屬刻意的行為變更：逐條列出動到的測試檔、測試名與理由，交由使用者決定；確認前不得動手改。

---

## 3. 安全相關不要砍

- `agent_tools.ToolExecutor._safe_path` — 所有檔案讀寫的 sandbox 入口
- `media._safe_path` — 圖片/ELF/binary 的 sandbox 入口
- `agent_tools._validate_command` — run_command 白名單 + dangerous-pattern 過濾
- `apply_patch` 的「context 必須匹配」、「max files / max lines」邏輯
- `mcp_server.py` 啟動時 `set_sandbox_root(AICODE_ROOT, allow_external=False)`

任何重構碰到上面這些東西，**新加測試**（開發者寫測試檔，執行依 §2.2 權責），
不要直接刪 / weaken / 移除檢查點。

新增安全檢查點時，守它的測試檔要標 smoke 並登記進 `tests/test_smoke_gate.py`
的 `SAFETY_MODULES`；漏標是無聲的（smoke 綠燈但那個檢查點根本沒跑）。

---

## 4. 不要做的事

- 不要把 `from config import X`（snapshot）混 `import config; config.X = ...`（mutation）— 動態值只用 `import config`。
- 不要為了讓 lint 漂亮，刪未檢查影響的 unused import — 有些是 side-effect import。
- 不要把 ALLOWED_COMMANDS 加 `rm` / `sudo` / `curl` / `bash`。
- 不要把 `RUN_COMMAND_ENABLED` / `PATCH_ENABLED` 在 `config.py` 的預設改成 `True`。OpenCode runtime 若要開，必須維持在 `mcp_server.py` 這類明確啟動點。
- 不要在 `mcp_server.py` 加新 tool 卻沒同步更新 `README.md` 工具清單 — 模型會誤用，使用者也會困惑（`aicode` 健檢會要求工具集合與文件精確一致）。使用者機器上依 README 建議建立的 `~/.config/opencode/AGENTS.md` 若列了工具清單，也要提醒一併更新。
- 不要 `git commit` 沒被使用者確認過的修改。

---

## 5. 預設離線

- CI 不可以依賴 llama-server / GPU / 大型 GGUF 下載。
- 任何測試用到 LLM 都要 mock 或 graceful skip（`pytest.importorskip` 或 `pytest.skip`）——
  這是**撰寫**測試的規範，執行權責見 §2.2。

---

## 6. NDA / 機敏資料

- `knowledge.json`、`data/`、`*.jsonl`、`.code_rag_cache_*` 全部在 `.gitignore` 裡。
- 任何 PR 都不能 commit 這些檔。
- 如果你 grep 到 NDA 客戶名 / 規格書檔名 hardcode 在程式碼裡，**那是 bug**，要報告。
