# 安全邊界與工作節奏

這份文件整理 CodeTrail 在 OpenCode TUI / web backend 裡的安全邊界。重點是:
CodeTrail 有自己的沙箱,但它只包住 CodeTrail MCP 工具；OpenCode 內建工具、provider、
plugin 與專案設定仍要另外限制。操作責任與人工驗證原則見
[Responsible Use](../RESPONSIBLE_USE.md)，保固與審計界線見
[Disclaimer](../DISCLAIMER.md)。

[回到 README](../README.md)。

---

## 一句話版本

分析 NDA / 不信任 repo 時,建議用這個入口:

```bash
cd <PROJECT_TO_ANALYZE>
OPENCODE_DISABLE_PROJECT_CONFIG=1 aicode
```

並保留 [README §4.3](../README.md#43-opencode-config) 範本裡的 OpenCode
permission:只允許 `codetrail_*`,把 OpenCode 內建 `bash` / `read` / `write` / `edit` /
`apply_patch` 等全部 `deny`。

---

## 沙箱真正保護什麼

`aicode` 啟動時會把當前目錄設成 `AICODE_ROOT`。一般檔案讀寫都限制在這個根目錄；
從 `$HOME` 或 `/` 啟動會直接被拒絕。兩個刻意而受限的例外是:

- `import_external_file(...)` 在你顯式開啟後,可從指定來源白名單**讀取並複製**單一檔案到
  `<AICODE_ROOT>/.aicode_uploads/`;後續工具仍只處理沙箱內副本。
- `record_lesson(...)` 經 permission `ask` 核准後,只可寫固定的
  `~/.config/codetrail/lessons.json`,不能由模型指定其他外部路徑。

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
OPENCODE_DISABLE_PROJECT_CONFIG=1 aicode_web
```

這會讓 OpenCode 忽略專案層級設定,避免 repo 自帶 config 把 `bash` / `read` / `write` 等內建工具重新放開。這個 env **只**關閉 project config，不會自動清掉 OpenCode 的全域、remote/custom、inline、managed 設定或已安裝 plugin。依 [OpenCode 的 config 合併與優先順序](https://dev.opencode.ai/docs/config/)，處理機密資料前仍要用 `opencode debug config` 檢查最終設定，並盤點已安裝 plugin。

兩個此模式的副作用/防線要知道:

- OpenCode 此模式改從全域設定目錄解析相對 instructions,不讀專案內檔案,所以 [lessons](lessons.md) 該 session **不會注入** —— `aicode` 啟動輸出會明講,並清掉先前 render 殘留的 `.codetrail/lessons.md`,不會謊報「已注入」。(OpenCode 對這個 env 是「非空即真」,`=0` 也算開啟。)
- 不信任 repo 可能把 `.codetrail` 換成指向專案外的 symlink/junction,誘導 lessons render 把檔案寫出沙箱;`aicode` 啟動時偵測到會直接拒絕啟動,一個 byte 都不寫。

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

匯入後檔案會複製到專案底下 `.aicode_uploads/`。白名單應只放實際需要的最窄目錄；
不要加入整個 `$HOME`、憑證目錄、共享根目錄或其他無關資料樹。來源檔與沙箱內副本都要
依資料擁有者的保存與刪除政策處理。

---

## 會真的改東西的工具

`apply_patch(...)` 會寫檔,`run_lint(...)` 可能格式化檔案,`run_command(...)` 會跑白名單命令。建議工作節奏:

1. 先要求模型用 `git_status(...)` / `git_diff(...)` 看目前工作樹。
2. 要分析時明講「不要改檔」。
3. 要改檔時要求先列出會改哪些檔案,再套最小 patch。
4. 修改後只跑最小相關測試或 lint。

`run_command(...)` 本身還有命令白名單與 dangerous-pattern 過濾。不要把 `rm` / `sudo` / `curl` / `bash` 加進白名單;真的需要人工操作時,讓模型列出建議命令,由人自己判斷後在 shell 執行。

`record_lesson(...)` 是唯一會寫到 `AICODE_ROOT` 之外的工具,而且只寫一個固定路徑:`~/.config/codetrail/lessons.json`(per-deployment 的行為教訓 store,與 `deployment.json` 同層;不能被模型指到別的路徑)。它被 permission 設成 `ask`:模型只能「提案」,你會在核准框看到完整 rule 內容,核准後才落地。沒有無審核的自動寫入路徑;細節見 [docs/lessons.md](lessons.md)。

升級防護:舊安裝 `git pull` 後,舊 opencode.json 的 `codetrail_*: allow` wildcard 會放行還沒有 ask 覆寫的新工具。`aicode` 每次啟動會自動把缺少的 ask 核准閘(與 lessons 的 instructions 項)補進全域 opencode.json(`scripts/opencode_contract_check.py --fix`,原檔備份、你明確設過的值一律尊重);不經 `aicode` 直接開 `opencode` 的話請先重跑 `./set_config.sh`。

---

## 不要 commit 的資料

以下資料可能含 NDA 內容、使用者提問、模型回答或文件切片,都不該進 commit:

- `knowledge.json`、`knowledge*.json`、`*.knowledge.json`
- `knowledge_emb.npz`
- `data/`、`*.jsonl`
- `.code_rag_cache_*`、`.rag_cache/`、`.rag_embedding_cache.json`
- `.code_rag_graph.sqlite3*`、`.code_rag_graph.lock`
- `.codetrail/`
- `.aicode_uploads/`
- `.opencode/`

這個 repo 的 `.gitignore` 已經忽略上述主要路徑。若你在另一個 target project 使用
CodeTrail,也建議在那個 project 的 `.gitignore` 補上同樣項目。`.gitignore` 不能保護
被重新命名、複製或手動 export 的內容；commit / 分享前仍要看 `git status` 與實際 diff。

---

## 模型 API(llama-server)曝光面

四個 CodeTrail 產生的 llama-server(8080–8083)**預設只綁
`127.0.0.1`，且未啟用認證**。上游 llama-server 目前有 `--api-key` /
`--api-key-file` 與 TLS 選項，但 CodeTrail 的 profile allowlist 與內部 HTTP client 尚未
接上這些 credential；README OpenCode 範本的 `apiKey: "local"` 只是 provider 所需的
非空值，不是 CodeTrail 部署的存取控制。因此以目前支援的路徑來看，綁
`0.0.0.0` 就等於讓可抵達該 port 的機器都能呼叫模型 API。

要讓其他機器連線必須明確選擇 `./set_config.sh --allow-remote`、
`AICODE_BIND=all-interfaces`，或 deployment.json 各 service 的
`"bind": "all-interfaces"`，而且只該在可信內網 / VPN 使用，必要時加防火牆規則。
如要開發 credential 支援，必須同步改 profile schema、所有 `llama_client`
call site、doctor / preflight 與 secret redaction，不能只手動在單一 server 加旗標。
[上游 server 選項](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
可供查證。

## 模型流量的 outbound policy(prompt 外送防線)

上面講的是「誰能連進來」;這一段是「CodeTrail 自己會把 prompt 送去哪」。所有經 `llama_client` 的模型呼叫共用同一套 transport policy(`endpoint_policy.py`):

- **非 loopback 端點需要顯式 opt-in**:`AICODE_LLAMA_*_BASE_URL` / deployment profile 指到別台機器時,必須 `export AICODE_MODEL_REMOTE_OK=1`,否則每個呼叫(completion / chat / embedding / reranking,連 health/props/slots 探測也一樣)都會 fail-loud,錯誤訊息印出確切 env 名。prompt 可能含 NDA 程式碼與文件內容——填一個遠端 IP 不等於同意外送。
- **不讀環境 proxy**:共用 HTTP session `trust_env=False`,`HTTP(S)_PROXY` / `NO_PROXY` / `.netrc` 一律無視,prompt-bearing POST 不會被環境變數帶去別的 host。
- **不跟隨 redirect**:任何 3xx 一律報錯(訊息含 status 與 Location host,絕不含 request body),拒絕把已送出的 POST 重送到別處。
- KB chunk 脈絡生成(Contextual Retrieval)沿用獨立的 `AICODE_KB_CONTEXT_REMOTE_OK`(見 docs/rag.md);兩個 opt-in 不互通,各自守各自要外送的內容。
- `python scripts/doctor.py` 啟動前就會檢查:端點非 loopback 且未設對應 opt-in → FAIL。

這些規則只涵蓋 CodeTrail 經 `llama_client` 發出的請求。OpenCode 自己的 provider、內建
web 工具、plugin 或其他 process 不會自動繼承 CodeTrail 的 endpoint policy。NDA 場景要
同時保留 `enabled_providers` 與 permission 鎖定，並檢查 effective OpenCode config。

## Web 模式曝光面

`aicode web` 預設只綁 `127.0.0.1`。A/B 機跨機器使用時推薦 `aicode_web`:它每次向本機 `tailscale ip -4` 取值,只綁該 `100.64.0.0/10` virtual interface，絕不綁 `0.0.0.0`。A 機可完全沒有 GUI，B 機開 launcher 印出的 `http://100.x.y.z:4096/` 即可；HTTP 封包仍包在 Tailscale 的加密 tunnel 內。

`aicode_web` 沒有應用層密碼,因此 **tailnet ACL 是存取邊界**；共享 / 多人 tailnet 應限制哪些裝置或使用者能連 A 機的 4096 port。wrapper 傳入值、hostname、Tailscale CLI 當下 IP 只要有一項不一致就拒絕。普通 `aicode web` 若刻意綁 LAN IP / `0.0.0.0` 或開 `--mdns`,仍必須先設定 `OPENCODE_SERVER_PASSWORD`。

不要用 `tailscale funnel`,因為它會把 OpenCode web backend 暴露到公網。想維持純 loopback 也可使用 SSH port-forward；這兩條都不會放寬 CodeTrail MCP sandbox。

---

## 快速檢查表

- 從具體專案目錄跑 `aicode` / `aicode_web`,不要從 `$HOME` 或 `/`。
- `/status` 看到 `codetrail Connected` 後再開始工作。
- 不信任 repo 時加 `OPENCODE_DISABLE_PROJECT_CONFIG=1`。
- 保留 [README §4.3](../README.md#43-opencode-config) 的 `enabled_providers` 與
  `permission` 鎖定。
- 需要外部附件才打開 `AI_CODE_ALLOW_EXTERNAL_IMPORT=1`。
- remote endpoint 只在明確接受資料外送時設定對應 opt-in。
- commit 前跑 `git status` / `git diff`,確認沒有知識庫、上傳附件、jsonl 或 session 快取。
