這個 repo 是一個 **本地 RAG / Code-RAG / MCP 工具集**。終端使用者透過 OpenCode TUI
和 `aicode_opencode` wrapper（或薄的 `aicode_opencode_web` 背景 launcher）連到這個專案，用本地 llama.cpp `llama-server` 跑模型,
分析 NDA / 內部 firmware repo。

如果你是 AI coding agent（Codex / OpenCode 等）正在改這個 repo，請先把這份檔讀完。
維護命令、eval 漂移檢查見 [README_DEV.md](README_DEV.md)——那份檔是**閱讀用參考**，
裡面的測試命令誰能執行由角色決定（見 §1）。

---

## 1. 測試 policy

### 1.1 兩包制

- **smoke** ＝ 標 `@pytest.mark.smoke` 的測試：真實發生過的 bug 的 regression ＋ 無聲失敗風險的契約檢查。
  §2 的每個安全檢查點都必須在裡面（由 `tests/test_smoke_gate.py` 靜態守住）。整包目標 10 秒內。
- **full** ＝ 整個 `tests/`。
- 統一入口 `python3 scripts/run_tests.py`（無參數＝full，最多 8-shard 並行；帶任何 pytest 參數＝單行程逐字轉發）：
  - smoke：`python3 scripts/run_tests.py -m smoke`
  - full：`python3 scripts/run_tests.py`

### 1.2 執行權責

- **開發者**（預設角色）：改碼過程**不執行測試**。只允許兩種執行：
  1. 交付前跑一次 smoke。
  2. 修 bug 時單跑自己新寫的那條 regression test（見 §1.3）。
  除此之外禁止執行測試及任何會間接觸發測試的命令。交付時註明：
  `Tests: smoke only — reviewer owns full execution.`
- **審核者**（僅限使用者在本次 prompt 明示 `ROLE=REVIEWER`）：先完成靜態審核並集中提出問題；
  程式碼收斂後對目前 HEAD 執行一次 full，回報命令、結果與 HEAD。程式碼未變時不得重跑已通過的測試。
- **判定規則**：失敗 node ID 集合不得大於動工前基線
  （缺 tty / `llama-server` 執行檔的環境，環境相依測試的既有失敗屬於基線）。
  基線外任何新失敗＝未完成，不得回報成功。
  `0 tests collected`（pytest exit code 5）不是通過，必須回報異常。

### 1.3 修 bug 鐵則：red-before-green

1. 先寫 regression test，在**未修改**的程式碼上單跑它
   （`python3 scripts/run_tests.py tests/test_x.py::test_y`），貼出紅燈輸出節錄。
2. 再修程式碼，同一條測試轉綠，貼出綠燈節錄。
3. 交付內容＝紅燈證據＋綠燈證據＋diff。缺紅燈證據的 bug fix 一律視為未驗證。

這類 regression test 一律標 `@pytest.mark.smoke`。

### 1.4 什麼時候寫新測試

只有兩種情況：
1. 真實發生過的 bug → regression（走 §1.3）。
2. 無聲失敗風險的契約，含 §2 安全層檢查點的防護測試。

其餘一律不寫：不追 coverage 數字、不為新功能寫儀式性測試、不為 parser 寫 parser。

### 1.5 動到既有測試就要講

改既有測試不必事先請示，但**交付時要逐條列出動到哪些測試檔、測試名與理由**——
包含改斷言、刪測試、放寬容忍值。理由要說得出「行為為什麼該變」，不能是「這樣才會綠」。

`skip` / `xfail` 仍然只用在環境相依（缺 tty、缺 `llama-server`）的情境，不用來繞過失敗。

---

## 2. 安全相關不要砍

- `agent_tools.ToolExecutor._safe_path` — 所有檔案讀寫的 sandbox 入口
- `media._safe_path` — 圖片/ELF/binary 的 sandbox 入口
- `agent_tools._validate_command` — run_command 白名單 + dangerous-pattern 過濾
- `apply_patch` 的「context／SEARCH 必須逐字匹配（S/R 絕不用相似度代套）」、「max files / max lines
  （udiff added+removed；S/R payload budget）」邏輯；`patch_engine` 的 byte-safe 寫入（UTF-8 strict、
  BOM/CRLF 保留、symlink／dir-fd 防線、best-effort rollback）；`patch_verify` 的「驗證層不得 spawn
  subprocess、skipped 不得算 passed」；`run_command` 的 timeout 1..600 三層邊界
- `mcp_server.py` 啟動時 `set_sandbox_root(AICODE_ROOT, allow_external=False)`
- `kb_cache` 的 embeddings 身分驗證（逐列 chunk id / generation / 內容雜湊 / model）
  與「重建不了就 fail-loud、絕不沿用舊向量」——放寬它就是靜默錯答
- `knowledge_store` 的文件身分驗證（`metadata["document_sources"]`）與
  `DocumentIdentityConflict`——KB 用 basename 當文件識別，所以 `a/spec.pdf` 與
  `b/spec.pdf` 是同一個身分；拿掉這道閘，後灌的那份會把前一份整份換掉，訊息
  跟正常更新一字不差，查詢照樣回答但答的是別份文件
- `knowledge._gated_completion`——knowledge.py 所有主模型 `/completion` 的唯一
  出口。繞過它等於沒有 context gate，超長 prompt 由 llama-server 從前面靜默截掉
- `elf_analysis.safe_regex` / `_regex_is_safe`——`analyze_file` 的 `target` 只接受正面表列的安全
  regex 子集（不收任何群組、`|` 只在最上層且 ≤ 8 分支、`*`/`+` 合計 ≤ 1、`?` ≤ 3、不接受 `{n,m}` /
  backreference / lookaround / inline flag），其餘改字面比對；比對主體只看前 300 字元。Python `re`
  沒有 timeout 也不釋放 GIL，放寬它就是讓一個 target 卡死整個同步的 MCP server
- `session_eval` 的私人 session 評測邊界——mined/curated 資料不得把歷史 assistant 回答當
  oracle；private writer 必須維持目錄 0700、檔案 0600、拒絕 symlink；read-only replay 必須
  deny 寫入／執行工具並以前後 project-state digest 偵測現場變動；checkpoint/resume 必須綁定
  suite digest、live model fingerprint、case 順序與逐題 project-state digest，單題 timeout 不得
  讓已完成結果無聲消失。原始 NDA prompt、工具輸出、candidate answer 不得寫入 checked-in
  `eval/` 或 privacy-safe aggregate
- `compaction_mode` 的壓縮模式 ownership 狀態檔——owner-only(目錄 0700／檔 0600、拒
  symlink 與 symlink 父目錄、dir-fd 原子寫入)、`digest` 必須涵蓋 `prior`(還原時會被
  寫回設定的正是它)、狀態綁定單一目標 config、以及「沒有狀態檔 = 沒有接管」的
  fail-closed 預設。切回 native 只能還原**仍有 ownership 證據**的值(JSON 型別嚴格
  相等),放寬任何一條就是靜默改掉或刪掉使用者的 OpenCode 設定;
  `MANAGED_COMPACTION_KEYS`(接管/還原)與 `CONTRACT_COMPACTION_KEYS`(值不符就
  停用自動壓縮)是兩組,不得合併——把只影響 context 用量的鍵(`prune`)併進契約集合,
  等於為它停掉整個 session 的壓縮,而且每次新增受管鍵都會讓舊狀態檔的安裝在
  升級當天全部跳 config_drift;新增受管鍵時 **不得**由 runtime 或 contract check
  自己補寫(沒有 ownership 紀錄就還原不回去),只能由 `unmanaged_keys` 報出來、
  使用者重跑 set_config
- `opencode_plugins/codetrail-compaction.js` 的壓縮契約——七條規則只能經
  `experimental.session.compacting` 的 `context` **附加**(改用 `prompt` 取代會讓
  `previousSummary` 從此不進摘要器,而且完全無聲);`autocontinue` 一律 `false`;
  壓縮後必須核對 summary parent 帶 compaction part、最新真實 user 已被回答、摘要非空
  非 reasoning-only 且無 error,不符就停止並要求重送(不自動續答、不自動 revert);
  pending / synthetic / 出錯回合 / 子 session 不得進狀態校正節錄,節錄不得帶工具參數或
  輸出;incident 與 application log 只放固定 slug 與 session 雜湊;停用必須寫進
  `compaction-stopped.jsonl` 才能跨行程(只記摘要/競態那幾種成因,`config_drift` 與
  `version_unsupported` 每個 idle 重算所以不得記),`chat.message` 只讀不改且整段包
  try/catch(上游是 `yield* trigger(...)`,reject 會讓使用者的訊息送不出去);整個 hook
  必須 fail-open(事件用 `void hook.event(...)` 派送,reject 出去就是 unhandled rejection);
  `experimental.chat.messages.transform` 只准拿掉「最新一則真實使用者訊息之前」的
  assistant `reasoning` part(`stripHistoricalReasoning`)——不新增、不重排、不動其他
  part、認不出那則使用者訊息就整段不動,而且必須就地換陣列元素(上游 trigger 之後
  用的是原本那個陣列參考,換掉 `output.messages` 完全無效);它同樣包 try/catch,
  上游是 `yield* trigger(...)` 且以 `Effect.promise` 呼叫,reject 是 defect,會讓
  整個請求掛掉

任何重構碰到上面這些東西，**新加測試**（開發者寫測試檔，執行依 §1.2 權責），
不要直接刪 / weaken / 移除檢查點。

新增安全檢查點時，守它的測試檔要標 smoke 並登記進 `tests/test_smoke_gate.py`
的 `SAFETY_MODULES`；漏標是無聲的（smoke 綠燈但那個檢查點根本沒跑）。
`SAFETY_MODULES` 記的是「檔名 → (說明, 必須存在且帶 smoke 的 node 名)」，不是
只記檔名：只驗「這個檔至少有一個 smoke」的話，刪掉那條檢查點測試、或把
module 層 `pytestmark` 換成單條 decorator，gate 都還是綠的。

---

## 3. 不要做的事

- 不要把 `from config import X`（snapshot）混 `import config; config.X = ...`（mutation）— 動態值只用 `import config`。
- 不要為了讓 lint 漂亮，刪未檢查影響的 unused import — 有些是 side-effect import。
- 不要把 ALLOWED_COMMANDS 加 `rm` / `sudo` / `curl` / `bash`。
- 不要把 `RUN_COMMAND_ENABLED` / `PATCH_ENABLED` 在 `config.py` 的預設改成 `True`。OpenCode runtime 若要開，必須維持在 `mcp_server.py` 這類明確啟動點。
- 不要在 `mcp_server.py` 加新 tool 卻沒同步更新 `README.md` 工具清單 — 模型會誤用，使用者也會困惑（`aicode_opencode` 健檢會要求工具集合與文件精確一致）。使用者機器上依 README 建議建立的 `~/.config/opencode/AGENTS.md` 若列了工具清單，也要提醒一併更新。
- 不要 `git commit` 沒被使用者確認過的修改。

---

## 4. 預設離線

- CI 不可以依賴 llama-server / GPU / 大型 GGUF 下載。
- 任何測試用到 LLM 都要 mock 或 graceful skip（`pytest.importorskip` 或 `pytest.skip`）——
  這是**撰寫**測試的規範，執行權責見 §1.2。
