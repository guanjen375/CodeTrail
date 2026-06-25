# 常見問題

這份文件整理 OpenCode / Codex CLI / CodeTrail / llama-server 常見故障排查。

[回到 README](../README.md)。

---

## 常見問題

### Build llama.cpp 時 `nvcc fatal : Unsupported gpu architecture 'compute_120a'`

你的 GPU 是 Blackwell(RTX 50 系列、RTX 6000 Ada Blackwell 等),但本機 CUDA Toolkit 太舊,不認識 `sm_120` / `compute_120a`。Ubuntu 24.04 的 `nvidia-cuda-toolkit` 套件停在 12.0,**Blackwell 需要 12.8+**。

驗證:

```bash
nvidia-smi | grep "CUDA Version"   # 驅動上限,>= 12.8 才有救
nvcc --version                      # 已安裝 toolkit
```

修法見 [README §1.4](../README.md#14-僅-blackwell-gpu-需要升級-cuda-toolkit-到-13)。重點順序:

1. 從 NVIDIA apt repo 裝 `cuda-toolkit-13-0`(**不要**裝 `cuda` 或 `cuda-13-0`,那兩個會連驅動拉下來打架)
2. `sudo apt remove --purge nvidia-cuda-toolkit ...` 移除 Ubuntu 內建舊的(避免 `/usr/bin/nvcc` 被當第一順位)
3. `export PATH=/usr/local/cuda-13.0/bin:$PATH`
4. `rm -rf build && cmake -B build ...` 重來(CMake 快取會記住舊 toolkit 路徑)

驗證 CMake 確實切到新版:輸出要有 `Found CUDAToolkit: ... (found version "13.x")` 和 `Compiler: /usr/local/cuda-13.0/bin/nvcc`,不是 `/usr/bin/nvcc`。

### CMake configure 時 `ptxas fatal : Value 'sm_52' is not defined for option 'gpu-name'`

升級 CUDA 13 之後 CMake 還是抓到舊 nvcc 路徑,新舊 toolkit 二進位混用。代表 step 2 的 purge 沒跑、或 PATH 順序錯了。

```bash
which nvcc                          # 應該是 /usr/local/cuda-13.0/bin/nvcc
echo $PATH | tr ':' '\n' | head     # /usr/local/cuda-13.0/bin 要在 /usr/bin 之前
```

不想移除舊 toolkit 的話,可以在 CMake 階段直接點名:

```bash
cmake -B build -DGGML_CUDA=ON -DLLAMA_CURL=OFF \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda-13.0/bin/nvcc
```

### MoE 模型第一次對話 TTFT(首字時間)1–2 分鐘

例如 Qwen3-235B-A22B 用 `--cpu-moe` 但沒加 `--no-mmap`,llama-server 啟動會看到:

```
W llama_model_loader: tensor overrides to CPU are used with mmap enabled — consider using --no-mmap for better performance
```

mmap 模式下 expert weights 是懶載入,第一次推理時要從 SSD page-in 大量 expert 到 RAM,TTFT 容易破 60 秒。OpenCode TUI 會卡在「`...esc interrupt`」很久,**不要按 Esc** —— 它有在跑,只是慢。

驗證是「慢」不是「卡」:

```bash
curl -s http://localhost:8080/slots | python3 -m json.tool   # 看主 server 是否 is_processing
nvidia-smi -l 1                                              # GPU 是否在動
```

若 slot 都 idle、GPU 0% 連續超過 30 秒,代表請求**沒打到 server**(問題在 OpenCode / MCP 層,不是 llama-server)。看 OpenCode log:`ls -t ~/.local/share/opencode/log/*.log | head -1`。

長期解法:重啟主 server 加 `--no-mmap`,前期載入慢 1.5–2.5 分鐘(把 ~135GB weights 全讀進 RAM),之後 TTFT 穩定在 5–15 秒。RAM 不夠 135GB 的就保持 mmap 接受偶爾卡頓,或換較小模型。

### 模型編造不存在的具體事實(條號 / 日期 / ticket 號 / 金額)—— 幻覺 / confabulation

**症狀**:問一個你沒提供來源的問題(例如「對某廠商發 ticket 施壓」),模型回了看似可執行的細節 —— 引用「合約第 7.2 條」、「每日延遲成本 \$25K」、「3 日內回應」 —— 但這些數字 / 條號**從來沒出現在你給它的任何資料裡**,是模型自己補的。

**先講結論:這不是模型壞掉,也不是 Q4 量化的鍋,換模型解決不了。** 模型甚至能正確診斷自己的這個現象,代表它很健康。根因有兩個:

1. **沒有 grounding(來源)**。你要它引合約條款,卻沒把合約貼給它。沒有來源時,任何模型、任何精度都**不可能**猜中真實條號 —— 它只能依「訓練語料裡最常見的 `第 X.Y 條` 模式」補一個最像的數字。這是機率預測的副作用,不是故意騙人。
2. **取樣太放飛 + 走錯路徑**。純聊天走的是 **OpenCode TUI → llama-server**,**完全繞過 CodeTrail** 的 temp 0.0 + RAG + strict mode(見 [context_budget.py](../context_budget.py) 註解、`config.py` 的 `STRICT_MODE_TEMPERATURE`)。而 llama-server 啟動若沒帶 sampling 旗標,內建預設 `temp 0.8 / top_k 40 / min_p 0.05` —— 對 `Qwen3-235B-A22B-Thinking-2507`(官方建議 `temp 0.6 / top_p 0.95 / top_k 20 / min_p 0`)偏高,更容易自由發揮。

**三個修法(按效果排序)**:

**① 要它講具體事實 → 先給它來源。** 想引合約就把合約貼進 prompt;程式碼問題走 CodeTrail 工具(`codetrail_*` / `aicode`)讓 RAG 把真實程式碼接進 context。沒來源的「具體數字 / 條號 / ticket 號」一律是擲骰子。

**② 在 llama-server 啟動旗標釘住取樣(這條同時修好 OpenCode 純聊天路徑)。** 主 server 啟動指令(README §3.1)加上:

```bash
  --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0 --presence-penalty 1.0 \
```

為什麼一定要在 server 旗標釘、而不是寫在 `opencode.json`:OpenCode 的 openai-compatible provider 對自訂 provider 有已知問題,`temperature` 會被丟掉不送進 request body([opencode#25755](https://github.com/anomalyco/opencode/issues/25755)),`top_k` / `min_p` 又不在它的 schema 裡。所以 server 旗標是唯一可靠的釘法。**改完要重啟 server 才生效。**

**③ 在 `~/.config/opencode/AGENTS.md` 加一條防杜撰規則。** OpenCode 會把全域 `~/.config/opencode/AGENTS.md` 自動載入每一段對話(含純聊天)。加入類似:

```markdown
## 事實準確性
- 不要杜撰未提供的具體事實:合約條號、日期、ticket 編號、金額、API 名稱、檔案路徑、引用出處。
- 沒有來源可佐證時,直接說「我手上沒有這項資訊」或輸出佔位符(如 `{待填}`),不要補一個看似合理的數字。
- 區分「推測」與「事實」:要推測就明講這是推測,不要當成已知條件輸出。
```

(注意:這份 `~/.config/opencode/AGENTS.md` 是 OpenCode runtime 的全域規則,跟本 repo 根目錄那份「給修改 CodeTrail 原始碼的 AI agent 看的」`AGENTS.md` 不是同一個東西。)

**換不換模型?** 不用。換更大 / 更高精度的模型幻覺會少一點但不會消失 —— 它一樣會編沒給它的東西。真正要調的是「來源 + 取樣 + 規則」,不是模型。

> CodeTrail 自己的內部呼叫(agent loop / 全文分析 / strict 自我複查)除了 temp 0.0/0.2,也已經把 `top_p / top_k / min_p` 釘在 Qwen 建議值(`config.py` 的 `CHAT_TOP_P` / `CHAT_TOP_K` / `CHAT_MIN_P`,可用 `AICODE_CHAT_TOP_P` / `AICODE_CHAT_TOP_K` / `AICODE_CHAT_MIN_P` env 覆寫),所以即使 server 忘了帶旗標,**CodeTrail 路徑仍然是穩的**。會吃到 server 預設、需要靠上面 ② 修的,只有 OpenCode 純聊天路徑。

### `pip install hf-transfer` 報 `error: externally-managed-environment`

Ubuntu 24.04(PEP 668)的 Python 拒絕 system-wide pip install。加 `--user --break-system-packages`:

```bash
pip install --user --break-system-packages hf-transfer
```

`--user` 把套件裝進 `~/.local/lib/pythonX.Y/site-packages`,不會動到系統 Python。

### `/status` 沒看到 CodeTrail MCP Connected

檢查:

```bash
python -m json.tool ~/.config/opencode/opencode.json >/dev/null
command -v aicode
command -v opencode
```

再確認 `opencode.json` 裡的 MCP command 能找到目前 git root 內的 `.opencode/run-codetrail-mcp`,且 `/status` 裡的名字會跟 `mcp` key 一致;如果 key 是 `codetrail`,應該看到 `codetrail Connected`。

### `aicode web`: 「這個 opencode 不支援 'web' 子指令(版本太舊)」

`aicode web` 啟動前會偵測 opencode 是否真的支援 web 子指令。看到這個訊息代表你的 opencode 太舊、還沒內建 web backend。升級:

```bash
npm install -g opencode-ai@latest
opencode web --help    # 應印出 opencode web 的說明(含 --port / --hostname)
```

偵測刻意不只看 exit code —— `opencode <任何字> --help` 在 yargs 下一律 exit 0,舊版會把 `web` 當成專案 positional,所以 `aicode web` 會額外檢查 `opencode web --help` 輸出裡有沒有 web 指令本身的 synopsis。升級後再跑一次 `aicode web` 即可。

### `aicode attach`: 連不上 backend

`aicode attach` 是純 client,連不上通常代表 backend 沒在跑、或 url / port 不對。逐項確認:

```bash
# 1) backend 有在跑嗎?(另一個終端應該有 aicode web 沒被關掉)
curl -sS http://127.0.0.1:4096/ -o /dev/null -w '%{http_code}\n'   # 有回 HTTP 碼(200/401 等)代表 backend 活著

# 2) port 對嗎?attach 預設接 4096;web 端若用 AICODE_WEB_PORT 換過 port,attach 也要對齊
aicode attach http://127.0.0.1:<PORT>
```

如果 web backend 啟動時設了 `OPENCODE_SERVER_PASSWORD`,attach 端要帶同一組認證:

```bash
aicode attach http://127.0.0.1:4096 -p <密碼>     # username 預設 opencode,可用 -u 覆寫
```

curl 回 401 代表 backend 活著但需要密碼;完全沒回應才是 backend 沒起來、或 port / host 寫錯。

### `aicode web`: port 被占用

`aicode web` 刻意固定 port(預設 4096),被占用時不會自動換 port,讓 opencode 直接報錯。先看誰占用:

```bash
ss -ltnp 'sport = :4096' 2>/dev/null || lsof -i :4096
```

兩種處理:

```bash
# A) 占用的是上一個沒關掉的 aicode web —— 直接 attach 上去就好,不必另開
aicode attach http://127.0.0.1:4096

# B) 真的要換 port(web 與 attach 都要對齊同一個)
AICODE_WEB_PORT=4097 aicode web
AICODE_WEB_PORT=4097 aicode attach      # 或 aicode attach http://127.0.0.1:4097
```

### web UI 切了資料夾,CodeTrail 還是讀啟動時那個目錄

CodeTrail 的沙箱根(`AICODE_ROOT`)是**啟動 `aicode web` / `start-web.sh` 當下那個目錄**,backend 起來時就釘死。OpenCode web UI 的「切換 WORK DIR / 開其他資料夾」只換 OpenCode 自己的 view,**不會 re-scope CodeTrail 的 MCP 沙箱** —— 所以你在 UI 切到別的資料夾後,`list_dir` / `read_file` 還是讀**啟動那個目錄**。

這不是 escape(CodeTrail 讀不到沙箱外的資料夾,只是還停在原本那個),但會誤導。**CodeTrail web 是一個 backend 一個專案**:要分析另一個專案,在那個專案目錄**另起一個 backend**(換 port):

```bash
cd ~/other-project
AICODE_WEB_PORT=4097 <CODETRAIL_REPO>/scripts/start-web.sh
```

OpenCode 目前沒有關掉那個切換器的設定,所以請直接**無視 UI 的資料夾切換**。

### 分析不信任的 repo:擋 `opencode.json` 覆蓋你的鎖定

被分析的 repo 如果自帶 `opencode.json`(根目錄或往上到 git root),它會**覆蓋你的全域鎖定設定** —— 可能把 `permission` 的 `bash` / `read` / `write` 從 `deny` 翻成 `allow`,讓 OpenCode 內建工具繞過 CodeTrail 沙箱;整個過程靜默無提示。分析**不信任 repo** 時前面加一個 env,讓 OpenCode 忽略專案層級 config:

```bash
OPENCODE_DISABLE_PROJECT_CONFIG=1 aicode
# web 也一樣:OPENCODE_DISABLE_PROJECT_CONFIG=1 <CODETRAIL_REPO>/scripts/start-web.sh
```

細節與實測見 [docs/security.md](security.md)。

### Codex CLI: `codex not found`

`aicodex` 啟動前會先檢查 `codex` 是否在 PATH。沒有的話安裝 Codex CLI:

```bash
npm install -g @openai/codex
command -v codex
```

安裝後如果 `command -v codex` 還是空,檢查 npm global bin 是否在 PATH。

### Codex CLI: `/mcp` 看不到 `codetrail`

先確認 `aicodex` 是從 target project 裡啟動,而且有產生 wrapper:

```bash
ls -l .codex/run-codetrail-mcp
```

正常情況下 wrapper 內容會包含 `generated by CodeTrail aicodex`,並 export `AICODE_ROOT`、`CODETRAIL_HOME`、`AICODE_MODEL`。`aicodex` 不會修改 `~/.codex/config.toml`;它用 Codex runtime `-c` override 注入 `mcp_servers.codetrail.*`。如果 `/mcp` 沒看到 `codetrail`,重跑一次 `aicodex --codetrail-model <LOCAL_MODEL>` 並看啟動時 `[aicodex]` 訊息是否有錯。

### Codex CLI: `AICODE_MODEL` 未設定 / local model 解析失敗

`aicodex -m gpt-5.5` 只是在選 Codex frontend model,不會設定 CodeTrail MCP internal local model。CodeTrail MCP internal model 是 `mcp_server.py` 與 CodeTrail tools 用來呼叫 llama-server 的模型,請用其中一種方式設定:

```bash
aicodex --codetrail-model <LOCAL_MODEL>
# 或
AICODE_MODEL=<LOCAL_MODEL> aicodex
```

如果同時設定 `AICODE_MODEL=foo` 又傳 `--codetrail-model bar`,兩者不同會 fail loud。這是刻意設計,避免 Codex frontend 與 CodeTrail MCP server-side tools 用錯模型。

### Codex CLI: `.codex/run-codetrail-mcp` 已存在但不是 CodeTrail wrapper

`aicodex` 只會覆蓋包含 `generated by CodeTrail aicodex` marker 的 wrapper。如果該檔案已存在但不是 CodeTrail 產生的,會拒絕覆蓋,避免 silent overwrite 使用者自訂檔案。

處理方式:先檢查該檔案是不是你自己放的 wrapper。若不需要,手動移走或改名後再跑 `aicodex`。

### Codex CLI: MCP config override quoting 問題

`aicodex` 會用 Python `json.dumps()` 產生 Codex `-c key=value` 的 TOML/JSON-safe 字串與陣列,路徑含空白也應該可用。若 Codex 回報 `mcp_servers.codetrail.command` parse 失敗,請先確認你跑的是 repo 內最新 `aicodex`,再看啟動輸出的 wrapper path 是否存在且可執行。

### Codex CLI frontend local provider 是選用

你可以讓 Codex frontend 繼續使用自己的 OpenAI / ChatGPT / provider 設定,同時讓 CodeTrail MCP internal tools 透過 `--codetrail-model` 或 `AICODE_MODEL` 使用本地 llama.cpp 模型。若想讓 Codex frontend 也走本地 llama.cpp,可選用 `~/.codex/config.toml`:

```toml
model = "<LOCAL_MODEL>"
model_provider = "llamacpp"

[model_providers.llamacpp]
name = "llama.cpp local"
base_url = "http://localhost:8080/v1"
wire_api = "responses"
```

這只是 Codex frontend provider 設定,和 CodeTrail MCP internal model 設定分開。如果 local llama.cpp server 不支援 Codex CLI 需要的 API shape,保留 Codex 原本 provider 也可以。

### 啟動時拒絕 `AICODE_ROOT`

你可能在 `$HOME` 或 `/` 執行了 `aicode` / `aicodex`。切到具體專案:

```bash
cd ~/work/some-firmware-repo
aicode
# 或 aicodex --codetrail-model <LOCAL_MODEL>
```

### `[ctx-safety] refuse to start.` 啟動被擋

> **一條規則就夠**:`AICODE_DYNAMIC_NUM_CTX_MAX`、llama-server 的 `-c <N>`、OpenCode active model 的 `limit.context` **三個數字必須完全相同**。aicode 啟動時的兩道檢查——`[ctx-safety]` 比 `AICODE_DYNAMIC_NUM_CTX_MAX` 跟 server `-c`、`[ctx-align]` 比 `AICODE_DYNAMIC_NUM_CTX_MAX` 跟 `limit.context`——任一不等就拒絕。**修法永遠一樣:挑一個數字,三處都改成它。**
>
> 為什麼非得一致:OpenCode TUI 的主對話**不經過 CodeTrail**、直接打 llama-server,它的真實 ctx 只由 server `-c` 和 `limit.context` 決定;`AICODE_DYNAMIC_NUM_CTX_MAX` 只管 CodeTrail 自己的 MCP / RAG 呼叫。三個不一致,TUI 和 MCP 就在不同 ctx 預算下各做各的。

這道 `[ctx-safety]` 比的是 `AICODE_DYNAMIC_NUM_CTX_MAX` 跟 server 真實 `-c`,兩個方向都擋:

- `AICODE_DYNAMIC_NUM_CTX_MAX` **大於** server `-c` → 標 `UNSAFE`:超過 server 真實上限,prompt 會被截斷(真正危險)。
- `AICODE_DYNAMIC_NUM_CTX_MAX` **小於** server `-c` → 標 `MISMATCH`:不會截斷,但 server 多出來的 ctx 用不到,且 OpenCode TUI / CodeTrail MCP 兩邊預算容易各走各的。

`UNSAFE` 輸出長這樣:

```
[ctx-safety] UNSAFE: model=<CODE_MODEL> requested_ctx=65532
        requested ctx=65532 超過 llama-server 啟動時的 -c 8192 (http://localhost:8080) — 多出來的 prompt 會被截斷
        ...
        建議任一處理:
          (a) export AICODE_DYNAMIC_NUM_CTX_MAX=8192  (對齊 server n_ctx)
          (b) 重啟 llama-server 並提高 `-c 65532` (確認 VRAM 夠)
```

兩條路:

```bash
# 路徑 A: 把 CodeTrail 端的上限降到跟 server 一致
export AICODE_DYNAMIC_NUM_CTX_MAX=8192
aicode
# 或 aicodex --codetrail-model <LOCAL_MODEL>

# 路徑 B: 停掉舊 server,用新 -c 重啟,把 server 上限拉大
pkill -f "llama-server.*--port 8080"
llama-server -m ~/models/<MODEL>.gguf --host 0.0.0.0 --port 8080 -c 65532 -ngl 99 &
aicode
```

`MISMATCH`(小於 server `-c`)反方向修即可:把 `AICODE_DYNAMIC_NUM_CTX_MAX` 提高到 server `-c`,或用較小的 `-c` 重啟 server。工具印出的 `(a)/(b)` 已帶好數字。

如果你確認要硬跑(例如想實測 truncation 的影響),用一次性放行:

```bash
AICODE_ACCEPT_CTX_RISK=1 aicode
# 或 AICODE_ACCEPT_CTX_RISK=1 aicodex --codetrail-model <LOCAL_MODEL>
```

如果不想再看到這個檢查(例如自動化、CI、知道自己在做什麼):

```bash
export AICODE_CTX_SAFETY_DISABLE=1
```

server 沒啟動 / 不可連時會印 `[ctx-safety] UNKNOWN` 並放行,不會擋啟動。手動驗證可以單跑:

```bash
AICODE_MODEL=<CODE_MODEL> python scripts/ctx_safety_check.py
```

`<CODE_MODEL>` 是佔位符,必須替換成實際模型名稱或 GGUF 路徑。

### `[ctx-align] MISMATCH` 啟動被擋

同一條規則的另一道檢查:OpenCode active model 的 `limit.context` 跟 `AICODE_DYNAMIC_NUM_CTX_MAX` 不一致。典型情況是 server / CodeTrail 已經是 64K,但 opencode.json 還留在 32K,TUI 會提早 compact。

一樣把三個數字對齊:

```bash
# server 真實上限
curl -s http://localhost:8080/props | jq '.default_generation_settings.n_ctx'

# CodeTrail MCP / RAG 上限
export AICODE_DYNAMIC_NUM_CTX_MAX=65532

# OpenCode TUI 上限
# ~/.config/opencode/opencode.json:
#   provider.<你的 provider>.models.<active model>.limit.context = 65532
```

若只是一次性實驗,可以用 `AICODE_ACCEPT_CTX_RISK=1 aicode` 放行,但不建議長期這樣跑。

### llama-server 不可連 / 404

代表對應 server 沒啟動,或 port 設錯。先 curl 試:

```bash
curl -s http://localhost:8080/health
curl -s http://localhost:8081/health   # embedding
curl -s http://localhost:8082/health   # reranker
curl -s http://localhost:8083/health   # VL
```

回 `{"status": "ok"}` 才算 ready。沒回應就重啟對應 server(見 [docs/setup.md](setup.md))。

啟動 server 後可以看 model_path 確認載對 GGUF:

```bash
curl -s http://localhost:8080/props | jq '.model_path, .default_generation_settings.n_ctx'
```

### `aicode` 拒絕啟動,訊息說「主模型未設定」

CodeTrail 不內建主聊天 / 程式推導模型,沒設好 `aicode` 會 fail-loud。任選一種設定方式:

```bash
# 1) 環境變數 (最優先)
export AICODE_MODEL=<CODE_MODEL>

# 2) per-run CLI 旗標
aicode -m <CODE_MODEL>

# 3) ~/.config/opencode/opencode.json 設 "model": "<provider>/<CODE_MODEL>"
```

`<CODE_MODEL>` 是 MODEL_REGISTRY 裡的 bare name 或 GGUF 絕對路徑。如果你看到「placeholder」相關錯誤,通常是值還停留在 `<CODE_MODEL>` 或 `<MODEL>` 沒換掉;看到「外部 provider prefix」錯誤代表你還在用 `ollama/foo` 那種舊寫法,改成 bare name 或你 opencode.json 裡 custom provider 的 prefix。

若 `AICODE_MODEL` 和 opencode.json 同時存在,且啟動時沒有傳 `-m/--model`,兩者必須指向同一顆 bare model。這是刻意 fail-loud,避免 OpenCode TUI 用 A 模型、CodeTrail MCP tools 用 B 模型。

### MODEL 解析到 GGUF 路徑但檔案不存在

doctor 報:

```
[FAIL] MODEL=qwen3-coder-32b ... 解析到 ~/models/qwen2.5-coder-32b-instruct-q4_k_m.gguf 但檔案不存在。
```

兩種原因:

1. registry mapping 寫錯路徑 → 修 `~/.config/codetrail/models.json`。
2. registry 沒這個 key,CodeTrail 把 bare name 直接當路徑 → 加 registry 或改用絕對路徑。

### 查 spec 沒結果

先確認文件已經匯入並 reload:

```text
請 reload_knowledge_base,回報目前載入幾個 chunks。
```

如果 chunks 是 0,重新要求:

```text
請 ingest_document docs/spec.pdf,完成後 reload_knowledge_base。
```

如果 embedding server (8081) 不通,reload 會印錯誤;先驗:

```bash
curl -s http://localhost:8081/health
```

### `apply_patch(...)` 被拒絕

常見原因:

- 模型讀到的是舊內容,先 `read_file(...)` 重讀目標區段。
- patch context 不夠或不匹配。
- 一次改超過檔案數或行數限制。

把任務拆小,要求模型一次只改一個行為。

### `run_command(...)` 被拒絕

命令不在白名單,或含 shell metacharacter。請模型改用已允許的最小命令,例如:

```text
請改跑 python -m pytest tests/test_x.py,不要使用 &&、|、; 或 shell script。
```

---
