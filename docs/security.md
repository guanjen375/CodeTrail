# 安全邊界與工作節奏

這份文件整理 sandbox、patch、run command、NDA 資料、資料飛輪與建議工作節奏。

[回到 README](../README.md)。

---

## 安全邊界

### 沙箱

MCP server 啟動時會執行：

```text
set_sandbox_root(AICODE_ROOT, allow_external=False)
```

結果：

- `read_file(...)`、`grep_code(...)`、`list_dir(...)` 只能看 `AICODE_ROOT` 內的檔案。
- `analyze_file(...)`、`ingest_document(...)` 的輸入也必須在 `AICODE_ROOT` 內。
- `import_external_file(...)` 是唯一外部入口，預設關閉；開啟後也只會把允許來源目錄內的檔案複製進 `.aicode_uploads/`。設定方式見 [RAG、附件與知識庫操作](rag.md) 的匯入附件場景。
- `apply_patch(...)` 只能改沙箱內檔案，且 patch context 必須跟現有檔案相符。
- `aicode` 會拒絕把 `/` 或 `$HOME` 當 root。

注意這層沙箱**只蓋 CodeTrail 的 17 個 MCP 工具**。OpenCode 自己內建了 `bash`、`read`、`write` 等工具，這些是 OpenCode 的東西，不走 CodeTrail 的 MCP 沙箱，因此能讀寫整個檔案系統（在目前 user 權限範圍內）。實務上常碰到的場景：CodeTrail 的 `import_external_file` 因為白名單擋下時，模型有時會 fallback 去用 OpenCode 內建的 `$ cp` 把檔案搬進專案目錄，照樣達到目的。要徹底鎖死，得從 OpenCode 設定那邊關掉它的內建工具，CodeTrail 沙箱層面控制不到。

### Patch

`apply_patch(...)` 限制：

- 單次最多 5 個檔案
- 單檔最多 200 行修改
- hunk context 不符會拒絕

這些限制是保護用的，不要為了方便把它拿掉。大型修改請拆小步。

### Run Command

`run_command(...)` 只允許白名單命令。預設白名單：

- Python：`pytest`、`python -m pytest`、`python -m unittest`
- C/C++：`ctest`
- Node：`npm test`、`npm run test`、`yarn test`
- Rust：`cargo test`、`cargo clippy`
- Go：`go test`、`go vet`
- Lint / format：`ruff`、`black`、`isort`、`eslint`、`clang-format`、`gofmt`、`rustfmt`

Build 命令（`make`、`cmake`、`cmake --build`、`ninja`、`meson`、`meson setup`、`meson compile`、`bazel build`）**預設不在白名單**，因為它們會跑專案內的 build script，等於任意程式碼執行；分析陌生 repo 時不該預設可用。要分析自己的專案，啟動 `aicode` 時顯式打開：

```bash
AI_CODE_ENABLE_BUILD_COMMANDS=1 aicode
```

即使有白名單，`make`、`cmake`、`npm test` 仍可能執行專案內腳本。分析不信任的 repo 時保持預設關閉。

### 完全唯讀模式

預設 `apply_patch` 和 `run_command` 都是開的（OpenCode 主流場景）。要把 CodeTrail 切成純分析、不改檔、不跑命令：

```bash
AI_CODE_PATCH=0 AI_CODE_RUN_TESTS=0 aicode
```

兩個變數獨立：

- `AI_CODE_PATCH=0` — 擋下 `apply_patch(...)` 和 `run_lint(..., fix=True)`（兩者都會改檔）。`run_lint(..., fix=False)` 走 check-only 仍可用，所以唯讀模式下還能做 lint 檢查。
- `AI_CODE_RUN_TESTS=0` — 擋下 `run_command(...)`，連 `pytest` / `cargo test` 都關掉。

兩個分開設，可以做出「只看 / 只跑測試不改檔 / 改檔但不跑命令」等不同信任邊界。

### 網路邊界

CodeTrail 的網路對外點都只能暴露於可信內網 / VPN，不要綁到對公網開放的網卡：

- **遠端 llama-server**：main / embedding / reranker / VL server 會收到 prompt、程式碼片段、spec 摘要與工具輸出。llama-server 預設不檢查 API key，所以 `AICODE_LLAMA_*_BASE_URL` 只能指向可信主機。
- **`aicode web` backend**：`aicode web` 啟動的 OpenCode headless server 與 llama-server **同級** —— 未設 `OPENCODE_SERVER_PASSWORD` 時完全無認證，任何能連到該 port 的人都能用你的模型、讀你的專案。

`aicode web` 的硬規則比上游嚴：

- 預設綁 `127.0.0.1`、固定 port `4096`（`AICODE_WEB_PORT` 可覆寫）。
- hostname 非 loopback（`127.0.0.1` / `localhost` / `::1` 以外，例如 `0.0.0.0`)或帶 `--mdns` 時，**必須先設 `OPENCODE_SERVER_PASSWORD`**，否則 `aicode web` 拒絕啟動。
- 即使設了密碼，`0.0.0.0` 也只應綁在可信內網 / VPN 介面。跨機器使用**最推薦 Tailscale `serve`**(backend 維持 loopback、tailnet 內 WireGuard 加密、免設密碼)或 SSH port-forward。**絕不可用 `tailscale funnel`** —— funnel 會把 backend 暴露到整個公網,等於 NDA 外洩。

`aicode attach` 是純 client，本身不開 port、不 spawn MCP，沙箱邊界由它連上的 backend 決定。

### Model picker 鎖定（NDA 必看）

OpenCode 的 model picker（TUI 與 web 都有）預設可能列出**雲端**模型 —— 例如內建的 OpenCode Zen 免費模型（`opencode/deepseek-v4-flash-free` 等），不需登入就能選。一旦手滑切過去，後續對話（含你的程式碼、spec 摘要）就會送到雲端，NDA 直接外洩。

防呆：在 `~/.config/opencode/opencode.json` 設 `enabled_providers`，只允許你的本機 provider：

```json
"enabled_providers": ["llamacpp"]
```

設了之後 model picker **只會出現你的本機模型**，雲端 provider 完全不列出、無法誤選。陣列字串要跟你 opencode.json 裡的 provider key 一致。NDA 場景強烈建議保留；這不影響 CodeTrail MCP（本機 llama-server）的運作。

### 不要 commit 的資料

這些通常含有 NDA 內容或本地快取，應留在 `.gitignore`：

- `knowledge.json`
- `knowledge_emb.npz`
- `.code_rag_cache_*`
- `.rag_embedding_cache.json`
- `.opencode/`
- `.aicode_uploads/`
- `data/`
- `*.jsonl`

### 開發者資料飛輪（選用）

這不是 OpenCode 日常必用功能。只有在你想收集互動樣本、日後做 reranker / fine-tuning / prompt regression 時才開。

設 `AI_CODE_COLLECT_DATA=1` 啟動 `aicode`，KB-shaped 工具（`query_knowledge` / `query_knowledge_strict` / `code_rag_search`）的每次呼叫會 append 一筆到 `data/interactions.jsonl`，含問題、回答（或 `[REFUSED]` / `[SKIPPED_STRICT:...]`）、refs、KB 分數與當下 git commit。預設關閉。

該檔在 NDA 場景必然含敏感片段，已在本文件「不要 commit 的資料」列入禁止 commit。要看統計或匯出訓練語料，跑：

```bash
python data_flywheel.py stats
```

`eval/` 也是開發者用的固定題庫 / 回歸評測，不會自動記錄對話。兩者差異與清理方式見 [README_DEV.md](../README_DEV.md)。

---


## 建議工作節奏

1. `cd <PROJECT_TO_ANALYZE>` 後跑 `aicode`。
2. 第一輪只允許讀取，要求列 file:line 證據。
3. 有 spec 先 `ingest_document(...)` + `reload_knowledge_base()`。
4. 修改前要求模型說明將改哪些檔案與原因。
5. 修改後要求模型跑最小相關驗證。
6. 結束前自己看一次 git diff，確認沒有把 `knowledge.json`、cache、log 或 NDA 衍生資料納入 commit。

---
