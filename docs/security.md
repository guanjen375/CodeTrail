# 安全邊界與工作節奏

這份文件整理 CodeTrail 在 CodeTrail 客戶端 / web backend 裡的安全邊界。重點是:
CodeTrail 有自己的沙箱,但它只包住 CodeTrail MCP 工具；客戶端內建工具(已不存在)、provider、
plugin 與專案設定仍要另外限制。操作責任與人工驗證原則見
[Responsible Use](../RESPONSIBLE_USE.md)，保固與審計界線見
[Disclaimer](../DISCLAIMER.md)。

[回到 README](../README.md)。

---

## 一句話版本

分析 NDA / 不信任 repo 時,建議用這個入口:

```bash
cd <PROJECT_TO_ANALYZE>
CODETRAIL_DISABLE_PROJECT_INSTRUCTIONS=1 aicode
```

客戶端**只**暴露 CodeTrail 的 19 個 MCP 工具:沒有第二套內建的 shell / 檔案 / web 工具
可以繞過沙箱。要更嚴的話,`~/.config/codetrail/client.json` 的 `permission` 可以把任何
工具改成 `ask` 或 `deny`(只能收緊,不能放寬 readonly policy)。

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

模型能呼叫的就只有上面這些:客戶端把 `tools/list` 的結果原樣交給模型,沒有另一組不經過
CodeTrail 的內建工具。互動模式下 `apply_patch` / `run_lint` / `run_command` /
`remove_document` / `record_lesson` / `review_figures` 六個必須人工核准,核准框**完整顯示
參數**(含整份 patch)。

---

## 不信任 repo 的額外防線

不信任的 repo 影響得到的是**送進模型的指示**:專案根目錄的 `AGENTS.md` 與
`.codetrail/lessons.md` 每一輪都會進 system prompt。分析不信任 repo 時,用:

```bash
CODETRAIL_DISABLE_PROJECT_INSTRUCTIONS=1 aicode
```

web 模式也一樣:

```bash
CODETRAIL_DISABLE_PROJECT_INSTRUCTIONS=1 aicode_web
```

這會讓客戶端完全不讀專案內的 `AGENTS.md` 與 `.codetrail/lessons.md`。它**只**關閉專案來源;
`~/.config/codetrail/instructions.md`(你自己的)與內建基底規則照常載入 —— 那是刻意的,你的
規則不該被分析對象關掉。

兩個此模式的副作用/防線要知道:

- [lessons](lessons.md) 該 session **不會注入** —— `aicode` 啟動輸出會明講,並清掉先前
  render 殘留的 `.codetrail/lessons.md`,不會謊報「已注入」。(這個 env 是「非空即真」,
  `=0` 也算開啟。)
- 不信任 repo 可能把 `.codetrail` 換成指向專案外的 symlink/junction,誘導 lessons render 把檔案寫出沙箱;`aicode` 啟動時偵測到會直接拒絕啟動,一個 byte 都不寫。

---

## 客戶端就是唯一的前端

`aicode` 啟動的是 CodeTrail 自己的 `codetrail_chat.py`,不再需要 Node / npm /
opencode-ai,也沒有第二個 client 的版本相容閘要顧。模型看到什麼由三件事決定,全部在這個
repo 裡:`client_prompt`(system prompt)、`mcp_contract`(工具目錄與 routing 指示)、
`client_policy`(哪些工具要核准)。

`aicode attach` 是只連既有 backend 的薄 client,不重跑 backend preflight。

## system prompt 與 permission 分工

system prompt 由客戶端組,順序固定:內建基底規則(`client_prompt.BASE_RULES`,上限 1,600
字元)→ MCP routing 指示 → 專案 `AGENTS.md` → `.codetrail/lessons.md` →
`~/.config/codetrail/instructions.md`。每一個來源檔都以 `O_NOFOLLOW` + `fstat` 讀,而且
**父目錄**被 symlink 重導就 fail-loud —— 只驗最終檔案擋不住「把 `.codetrail` 換成 symlink」。

system prompt 不是 permission:它不會讓被 `deny` 的工具變成可用,也不會繞過六個 ask 工具的
人工核准。權限的唯一來源是 `client_policy` 加上 `client.json` 的 `permission` 覆寫,
而 readonly session 另有第二層(MCP server 自己收到 `AI_CODE_PATCH=0` /
`AI_CODE_RUN_TESTS=0`)。

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

`apply_patch(...)` 會寫檔、`run_lint(...)`（`fix=True`）會格式化檔案、`run_command(...)` 會跑白名單命令——這是**三個不同的 ask**,每一個都要你分別核准。`apply_patch` 套用後只做同一 process、唯讀的 syntax check(advisory、失敗不回滾),不會執行 lint／typecheck／test,也不會呼叫會改檔的 formatter;核准「寫檔」不會暗中擴張成「執行專案程式碼」。建議工作節奏:

1. 先要求模型用 `git_status(...)` / `git_diff(...)` 看目前工作樹（非 git 專案會回跳過通知，這步略過）。
2. 要分析時明講「不要改檔」。
3. 要改檔時要求先列出會改哪些檔案,再套最小 patch(先 `dry_run` 預覽)。
4. 修改後由你決定是否用 `run_lint(fix=False)` / `run_command(...)` 跑最小相關檢查(各自核准)。

`run_command(...)` 本身還有命令白名單與 dangerous-pattern 過濾。timeout 只接受整數 1..600 秒（server 端上限；client 可能更早截止），不是這個範圍的整數會在執行前被拒絕。不要把 `rm` / `sudo` / `curl` / `bash` 加進白名單;真的需要人工操作時,讓模型列出建議命令,由人自己判斷後在 shell 執行。

`record_lesson(...)` 是唯一會寫到 `AICODE_ROOT` 之外的工具,而且只寫一個固定路徑:`~/.config/codetrail/lessons.json`(per-deployment 的行為教訓 store,與 `deployment.json` 同層;不能被模型指到別的路徑)。它被 permission 設成 `ask`:模型只能「提案」,你會在核准框看到完整 rule 內容,核准後才落地。沒有無審核的自動寫入路徑;細節見 [docs/lessons.md](lessons.md)。

升級防護：核准閘不再依賴任何外部設定檔。哪些工具要人工核准寫死在
`client_policy.ASK_TOOLS`,所以「新加的寫入工具被舊 wildcard 靜默放行」這個問題由構造
消失。舊安裝升級後仍請跑一次 `./set_config.sh`,它會把 CodeTrail 曾經寫進你 OpenCode
設定的東西還原並撤銷註冊(見 [README §3.1](../README.md))。

tool canary 的 explicit hard gate 與 implicit diagnostic 分開使用 cache schema 2。cache 只存
fingerprint hash、lane status、檢查時間與版本，不存 prompt、專案路徑、檔名、模型輸出、
tool arguments 或 tool result；兩條 lane 不會互借另一列狀態。implicit 的
`optimal|suboptimal|fail|timeout` 是部署診斷，不是資料或正確性保證。

---

## 不要 commit 的資料

以下資料可能含 NDA 內容、使用者提問、模型回答或文件切片,都不該進 commit:

- `knowledge.json`、`knowledge*.json`、`*.knowledge.json`
- `knowledge_emb.npz`(舊版本的 companion 向量檔;新版向量在 `.codetrail/` 底下)
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
接上這些 credential。因此以目前支援的路徑來看，綁
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
- `python3 scripts/doctor.py` 啟動前就會檢查:端點非 loopback 且未設對應 opt-in → FAIL。

這些規則涵蓋 CodeTrail 經 `llama_client` 發出的**所有**請求 —— 客戶端的聊天迴圈與壓縮
摘要都走這條路,沒有第二個 provider stack 會繞過它。同一台機器上的其他 process 當然不受
這裡管;NDA 場景仍要確認 `~/.config/codetrail/deployment.json` 的端點全是 loopback。

## Web 模式曝光面

`aicode web` 預設只綁 `127.0.0.1`。A/B 機跨機器使用時推薦 `aicode_web`:它每次向本機 `tailscale ip -4` 取值,只綁該 `100.64.0.0/10` virtual interface，絕不綁 `0.0.0.0`。A 機可完全沒有 GUI，B 機開 launcher 印出的 `http://100.x.y.z:4096/` 即可；HTTP 封包仍包在 Tailscale 的加密 tunnel 內。

`aicode_web` 預設沒有應用層密碼,因此 **tailnet ACL 是存取邊界**；共享 / 多人 tailnet 應限制哪些裝置或使用者能連 A 機的 4096 port。wrapper 傳入值、hostname、Tailscale CLI 當下 IP 只要有一項不一致就拒絕。普通 `aicode web` 若刻意綁 LAN IP / `0.0.0.0` 或開 `--mdns`(對區網廣播這個服務),仍必須先
設定 `AICODE_WEB_PASSWORD`,而且是 **server 自己**擋 —— 直接叫
`codetrail_chat.py web --hostname 0.0.0.0` 一樣被拒。設了密碼時它不會出現在 tmux 指令列、
pane scrollback,也不會傳給 MCP 子行程(核准後的 `run_command` 會繼承那份環境)。

無密碼的 loopback backend 也不接受跨站請求:server 比對 `Origin`/`Referer` 與 `Host`,而且
API 端點只收 `application/json`(`text/plain` 的 simple POST 是跨站頁面唯一免 preflight 的
形狀)。同一個 session 一次只跑一輪,核准只能回答一次且必須是真的 boolean。

不要用 `tailscale funnel`,因為它會把 web backend 暴露到公網。想維持純 loopback 也可使用 SSH port-forward；這兩條都不會放寬 CodeTrail MCP sandbox。

---

## 快速檢查表

- 從具體專案目錄跑 `aicode` / `aicode_web`,不要從 `$HOME` 或 `/`。
- 確認啟動前有 `MCP PASS — 19 tools + list_dir round-trip`；implicit 非 optimal 只代表
  routing 診斷警告，explicit failure 則會拒絕啟動。
- 不信任 repo 時加 `CODETRAIL_DISABLE_PROJECT_INSTRUCTIONS=1`。
- 要更嚴的權限時,用 `~/.config/codetrail/client.json` 的 `permission`(只能收緊)。
- 需要外部附件才打開 `AI_CODE_ALLOW_EXTERNAL_IMPORT=1`。
- remote endpoint 只在明確接受資料外送時設定對應 opt-in。
- commit 前跑 `git status` / `git diff`,確認沒有知識庫、上傳附件、jsonl 或 session 快取。
