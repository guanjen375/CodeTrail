# 安全邊界與工作節奏

這份文件整理 CodeTrail 在 OpenCode TUI 裡的安全邊界。重點是:CodeTrail 有自己的沙箱,但它只包住 CodeTrail MCP 工具;OpenCode 內建工具要另外用 permission 鎖住。

[回到 README](../README.md)。

---

## 一句話版本

分析 NDA / 不信任 repo 時,建議用這個入口:

```bash
cd <PROJECT_TO_ANALYZE>
OPENCODE_DISABLE_PROJECT_CONFIG=1 aicode
```

並保留 README §4.2 範本裡的 OpenCode permission:只允許 `codetrail_*`,把 OpenCode 內建 `bash` / `read` / `write` / `edit` / `apply_patch` 等全部 `deny`。

---

## 沙箱真正保護什麼

`aicode` 啟動時會把當前目錄設成 `AICODE_ROOT`。CodeTrail 的 17 個 MCP 工具只能在這個根目錄裡讀寫;從 `$HOME` 或 `/` 啟動會直接被拒絕。

受 CodeTrail 沙箱保護的典型工具包含:

- 讀取與搜尋:`list_dir(...)`、`read_file(...)`、`grep_code(...)`、`code_rag_search(...)`
- 附件與知識庫:`import_external_file(...)`、`analyze_file(...)`、`ingest_document(...)`、`query_knowledge(...)`
- 修改與驗證:`git_status(...)`、`git_diff(...)`、`apply_patch(...)`、`run_lint(...)`、`run_command(...)`

OpenCode 內建的 `bash` / `read` / `write` / `edit` 不經過 CodeTrail,所以 README 的 `opencode.json` 範本把它們設成 `deny`。不要為了方便把這些打開,除非你清楚知道該 repo 與目前 session 的風險。

---

## 不信任 repo 的額外防線

OpenCode 可能讀取專案內的 `opencode.json`,而專案層級 config 可能覆蓋你的全域 permission。分析不信任 repo 時,用:

```bash
OPENCODE_DISABLE_PROJECT_CONFIG=1 aicode
```

web 模式也一樣:

```bash
OPENCODE_DISABLE_PROJECT_CONFIG=1 <CODETRAIL_REPO>/scripts/start-web.sh
```

這會讓 OpenCode 忽略專案層級設定,避免 repo 自帶 config 把 `bash` / `read` / `write` 等內建工具重新放開。

---

## 外部檔案匯入

預設不能讀專案外路徑。要匯入 `~/Downloads` 或 `/tmp` 的 log / 截圖 / spec,啟動時才打開:

```bash
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 aicode
```

若要指定來源白名單:

```bash
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 \
AI_CODE_IMPORT_ROOTS="$HOME/Downloads:/tmp:$HOME/specs" \
aicode
```

匯入後檔案會複製到專案底下 `.aicode_uploads/`。不要把整個 `$HOME` 加進白名單,除非你確認裡面沒有 SSH key、憑證、客戶資料或其他敏感檔。

---

## 會真的改東西的工具

`apply_patch(...)` 會寫檔,`run_lint(...)` 可能格式化檔案,`run_command(...)` 會跑白名單命令。建議工作節奏:

1. 先要求模型用 `git_status(...)` / `git_diff(...)` 看目前工作樹。
2. 要分析時明講「不要改檔」。
3. 要改檔時要求先列出會改哪些檔案,再套最小 patch。
4. 修改後只跑最小相關測試或 lint。

`run_command(...)` 本身還有命令白名單與 dangerous-pattern 過濾。不要把 `rm` / `sudo` / `curl` / `bash` 加進白名單;真的需要人工操作時,讓模型列出建議命令,由人自己判斷後在 shell 執行。

---

## 不要 commit 的資料

以下資料可能含 NDA 內容、使用者提問、模型回答或文件切片,都不該進 commit:

- `knowledge.json`、`knowledge*.json`、`*.knowledge.json`
- `knowledge_emb.npz`
- `data/`、`*.jsonl`
- `.code_rag_cache_*`、`.rag_cache/`、`.rag_embedding_cache.json`
- `.codetrail/`
- `.aicode_uploads/`
- `.opencode/`

這個 repo 的 `.gitignore` 已經忽略上述主要路徑。若你在另一個 target project 使用 CodeTrail,也建議在那個 project 的 `.gitignore` 補上同樣項目。

---

## Web 模式曝光面

`aicode web` 預設只綁 `127.0.0.1`。跨機器使用時推薦 Tailscale `serve` 或 SSH port-forward。

不要用 `tailscale funnel`,因為它會把 OpenCode web backend 暴露到公網。若你刻意綁 `0.0.0.0` 或開 `--mdns`,必須先設定 `OPENCODE_SERVER_PASSWORD`,否則 `aicode web` 會拒絕啟動。

---

## 快速檢查表

- 從具體專案目錄跑 `aicode`,不要從 `$HOME` 或 `/`。
- `/status` 看到 `codetrail Connected` 後再開始工作。
- 不信任 repo 時加 `OPENCODE_DISABLE_PROJECT_CONFIG=1`。
- 保留 README §4.2 的 `permission` 鎖定。
- 需要外部附件才打開 `AI_CODE_ALLOW_EXTERNAL_IMPORT=1`。
- commit 前跑 `git status` / `git diff`,確認沒有知識庫、上傳附件、jsonl 或 session 快取。
