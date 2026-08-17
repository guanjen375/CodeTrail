# 常見問題

這份文件整理 OpenCode / CodeTrail / llama-server 常見故障排查。

[回到 README](../README.md)。

---

## 常見問題

### Build llama.cpp 時 `nvcc fatal : Unsupported gpu architecture 'compute_120a'`

你的 GPU 是 Blackwell(RTX 50 系列或 RTX PRO 6000 Blackwell),但本機 CUDA Toolkit 太舊,不認識 `sm_120` / `compute_120a`。Ubuntu 24.04 的 `nvidia-cuda-toolkit` 套件停在 12.0,**Blackwell 需要 12.8+**。

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

### `~/start.sh` 只顯示 process 已結束並 rollback

**使用者前台通常只會看到摘要**:

```text
[+] started main server (...) in tmux codetrail-main:main
[rollback] 啟動失敗:main 的 llama-server process 已結束(模型載入失敗或參數錯誤)
[rollback] server log 已保存:.../logs/(main).log
ERROR: main 的 llama-server process 已結束(模型載入失敗或參數錯誤)
```

這段訊息**不是 server 回報的真正根因**。launcher 只觀察到 tmux window 已消失,
因此只能統一回報「process 已結束」;真正的模型 loader、參數、CUDA 或記憶體錯誤
是 `llama-server` 寫在後台 log 裡的。launcher 隨後會自動清掉本次建立的 session,
避免下一次啟動卡在 `session already exists`;所以 rollback 後 `tmux ls` 顯示沒有
server 是預期行為,不代表錯誤紀錄也消失了。

畫面中的 `/(main).log` 是「已保存 main role」的摘要顯示,實際檔名是
`~/.local/state/codetrail/logs/main.log`。最簡單的讀法是:

```bash
~/start.sh logs main
```

不要只憑前台的 generic rollback 訊息就重裝 CUDA、重抓模型或刪 tmux session;
先依後台 log 的第一個明確 error 判斷。若後台出現類似下面這行:

```text
llama_model_load: error loading model: unknown model architecture: '<architecture>'
```

才表示這次的具體原因通常是 GGUF 使用的架構比本機 `llama-server` build 新;
模型檔不一定損壞,CodeTrail 的 tmux 啟動與 rollback 也仍在正常運作。先確認目前
實際執行的 binary 與版本:

```bash
~/start.sh --dry-run | grep 'llama-server'
~/llama.cpp/build/bin/llama-server --version
git -C ~/llama.cpp log -1 --oneline
```

若 launcher 使用預設的 `~/llama.cpp/build/bin/llama-server`,更新原始碼並重新 build:

```bash
cd ~/llama.cpp
git status --short                 # 有自己的修改時先處理,不要直接覆蓋
git pull --ff-only
cmake -B build -DGGML_CUDA=ON -DLLAMA_CURL=OFF
cmake --build build --config Release -j
~/llama.cpp/build/bin/llama-server --version
~/start.sh
```

更新後若仍是同一個 `unknown model architecture`,先查該架構是否已進 llama.cpp
上游;尚未支援時只能暫時換回已支援的 GGUF,或等待支援合併後再 build。不要為了
繞過錯誤修改 GGUF metadata:loader 還是缺少真正的模型實作。

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

長期解法:把 `no_mmap` 加進該 role 的 deployment 參數。**不要手動改 llama-server 指令** ——
`~/start.sh` 的 argv 每次都由 `~/.config/codetrail/deployment.json` 重新產生,手改會被下次啟動蓋掉。

```bash
# 編輯 ~/.config/codetrail/deployment.json,在該 role 的 parameters 加一行:
#   "services": { "main": { "parameters": { ..., "no_mmap": true } } }
python3 deployment_profile.py validate     # 確認 schema 過

# 改 main 的:整組重啟
~/start.sh stop && ~/start.sh
# 只改 vl 的:不必動主模型,重啟三顆附屬即可
~/start.sh stop --scope aux && ~/start.sh --scope aux
```

**main 與 vl 都適用** —— VL 一旦套用 CPU-MoE(`--vl-n-cpu-moe` / `--vl-cpu-moe`)就會踩到同一個坑。
`set_config.sh` 偵測到「開了 CPU-MoE 卻沒設 no_mmap」時會直接警告並附上這個做法;它**不會替你決定**
(代價是啟動時要把整份權重讀進 RAM),但重跑 `set_config.sh` 會**保留**你手動加的 `no_mmap`。

代價:前期載入慢 1.5–2.5 分鐘(把整份 weights 讀進 RAM),之後 TTFT 穩定在 5–15 秒。
RAM 不夠的就保持 mmap 接受偶爾卡頓,或換較小模型 / 調高 CPU-MoE 層數。

<a id="mcp-connected-but-no-tool-call"></a>

### `/status` 是 Connected,但模型說沒有 CodeTrail 或只印出假工具 XML

**典型症狀**:

- `/status` 或 `opencode mcp list` 明明顯示 `codetrail Connected`,模型卻回答「沒有 CodeTrail 工具」,甚至改口說只有 `todos`、`web_search` 等別的工具。
- 明確要求 `list_dir(path=".", depth=1)` 後,模型只輸出 `<codetrail_list_dir path="." depth="1"/>`,接著用自然語言宣稱「已成功取得目錄」,畫面上沒有工具卡、也沒有真實目錄內容。

**先講結論:模型對「自己有哪些工具」的文字回答不是診斷資料,XML 長得像 tool call 也不代表執行過。** 要把三層狀態分開看:

| 層次 | 能證明什麼 | 怎麼驗證 |
|---|---|---|
| MCP 連線 | OpenCode 已啟動 CodeTrail 子行程並完成 initialize | `/status`、`opencode mcp list` |
| 工具註冊 | client 收到 CodeTrail 的工具 schema | OpenCode 的 tools / MCP 檢視;完整名稱見 [MCP 工具清單](mcp-tools.md) |
| 本輪實際執行 | 模型真的發出結構化 tool call,client 執行後把結果送回模型 | TUI 工具卡,或 JSON event 的 `type: "tool_use"`、`state.status: "completed"` |

新版 `aicode` 已把這兩個容易漏做的檢查接到啟動流程，不需要每次先叫模型背 18 個名字：

- `MCP PASS — 18 tools + list_dir round-trip`：每次啟動都另起實際設定的 MCP command，完成 `initialize`、精確比對 18 個 schema，再真的執行無副作用的 `list_dir(path=".", depth=1)`。這一層完全不問 LLM。
- `MODEL PASS — structured codetrail_list_dir completed`：用 fresh headless session 跑 active model，只接受 JSON stream 裡 completed 的結構化 `tool_use`。純文字／XML 和模型自行宣稱成功都不會通過。
- `MODEL live canary — <原因>`：第二層 cache 未命中（新專案、設定變動、快取過期或 `--force`）時，實跑前會先印出原因與單次上限秒數，執行中每 15 秒回報一次「仍在執行」。本地推理通常需要數十秒到數分鐘——看得到心跳就不是當機，完全靜默才是異常。
- `MODEL PASS — cached ...`：相同專案、模型、OpenCode 設定、全域／專案 AGENTS、server `/props`（含 chat template 與取樣預設）曾在 24 小時內通過；MCP 第一層仍是本次 live 檢查。指紋任一部分改變會自動重測。
- `MODEL FLAKY`：第一次失敗、retry 才成功。本次可進入，但不寫 PASS cache，所以下次 `aicode` 仍會再驗。連續兩次失敗預設拒絕進 TUI。

模型 canary 會刪除自己建立的臨時 OpenCode session；本身的 cache 只存 hash、PASS 時間與版本，不存 prompt、模型輸出、目錄內容或專案路徑。這是啟動抽查，不是「往後每個生成 token 都保證正確」；如果 TUI 裡稍後又碰到偶發失手，可直接退出後強制重測：

```bash
AICODE_TOOL_CANARY_FORCE=1 aicode
```

只在排查／救援時才用 override。`WARN_ONLY` 仍執行檢查並顯示失敗，但允許進 TUI；`SKIP` 連兩層都不執行：

```bash
AICODE_TOOL_CANARY_WARN_ONLY=1 aicode
AICODE_TOOL_CANARY_SKIP=1 aicode
```

預設 cache TTL 是 86400 秒；需要更頻繁抽查可設 `AICODE_TOOL_CANARY_TTL_SECONDS=<SECONDS>`（設 `0` 等同每次 live model canary）。第一層或第二層 FAIL 時，訊息會刻意區分「MCP/config/18-tool contract」和「MCP 已通但 model/provider/chat-template 沒產生 tool call」，避免再把兩者混為一談。

**「剛 ingest 文件就失憶」不等於整份 RAG 塞爆 context。** `ingest_document` 把全文切 chunk 後寫進 `knowledge.json`,它送回目前對話的只有有長度上限的執行摘要;`reload_knowledge_base` 只更新 MCP process 內的 KB singleton。只有之後呼叫 `query_knowledge` 時,召回的少量 REF 才會以 tool result 進入那個 session。新 session 不會因為 KB 裡文件變多就自動攜帶全文。同一個舊 session 累積很多 tool result 時仍可能變長,但要看實際 token / compaction,不能只看 ingest 發生過就下結論。

這次實際失敗案例是全新 session:`step_finish.tokens.total=9001`、模型上限 131072,且沒有 compaction;其中 `input=522`、`cache.read=8355`,因此 RAG overflow 可直接排除。`--format json` 的 `step_finish` event 可用來看重現請求的 tokens;本例的 cache read 是可重用的 system / tool schema prefix,而 `cache.read` 數值本身也不能當成「整份 KB 已注入」的證據。

若要繞過 wrapper 做更底層的手動重現,用一個全新 session,不要沿用已經多次回答「工具不存在」的舊對話(舊上下文本身可能讓模型繼續模仿錯誤答案):

```bash
opencode mcp list
opencode run --dir <PROJECT_TO_ANALYZE> --agent build --format json \
  '請立即呼叫 codetrail_list_dir，path="."、depth=1。必須實際呼叫工具。'
```

真的呼叫時,JSON stream 會出現 `type: "tool_use"`、`tool: "codetrail_list_dir"` 和完成狀態,step 結束原因通常是 `tool-calls`;只看到 assistant 的 XML / 純文字且以 `stop` 結束,就是模型模擬了呼叫。也可從 OpenCode log 交叉檢查:

```bash
rg -n 'codetrail_list_dir|evaluated permission|tool_use' \
  ~/.local/share/opencode/log/*.log | tail -50
```

真呼叫通常會留下 tool / permission evaluation 紀錄;假 XML 只有普通 assistant text。不要以「模型說 retrieved successfully」當成功證據。

這種情況常見於本機模型的 tool-call 格式不穩。先在既有 `~/.config/opencode/opencode.json` **合併**下面區塊(不要整份覆蓋):

```json
{
  "agent": {
    "build": {
      "temperature": 0
    }
  }
}
```

驗證 JSON 與 OpenCode 實際解析到的 agent 設定,然後完全退出 OpenCode、重開並建立新 session:

```bash
python3 -m json.tool ~/.config/opencode/opencode.json >/dev/null
opencode debug agent build | rg '"temperature": 0'
```

`temperature: 0` 是降低隨機格式漂移的建議,不是保證任何模型都能正確 tool call。[OpenCode agent 設定](https://opencode.ai/docs/agents/)雖正式支援 agent-level `temperature`,custom `@ai-sdk/openai-compatible` provider 仍有版本相關的傳遞問題([opencode#25755](https://github.com/anomalyco/opencode/issues/25755));所以 `opencode debug agent build` 只能證明設定已解析,不能單獨證明 request body 一定帶了它。要釘住所有未明示取樣值的請求,再把下面的鍵**合併進既有** `~/.config/codetrail/deployment.json`(保留其他 service / model / port):

```json
{
  "services": {
    "main": {
      "parameters": {
        "temperature": 0
      }
    }
  }
}
```

改 server 設定後執行 `~/start.sh stop` → `~/start.sh` 重啟才會生效。`set_config.sh` 重跑時會保留手動加入的 allowlisted 取樣參數。重啟後不要只看 JSON,直接確認 server 實際預設已變成 `0.0`:

```bash
curl -s http://localhost:8080/props \
  | jq '.default_generation_settings.params.temperature'
```

若模型輸出的格式名稱跟目前 chat template 完全不同,可再確認 llama-server 載入的 template:

```bash
curl -s http://localhost:8080/props | jq -r '.chat_template' \
  | rg 'tool_calls|invoke|DSML'
```

例如模型只寫出自創的 `<codetrail_list_dir .../>`,不會因為看起來像 XML 就被 frontend 當成結構化呼叫。不要靠 prompt 手寫 / 猜測底層 tool-call markup;應讓 OpenCode、provider adapter 與 llama.cpp chat template 處理。

若 server 已降溫但模型仍會否認工具,把 [OpenCode 全域 AGENTS.md 範本](opencode-agents-template.md) 合併進 `~/.config/opencode/AGENTS.md`,至少要「CodeTrail 工具存在性與真實呼叫」一段。完整列名是刻意的:只寫一句「優先用 `codetrail_*`」仍可能被較弱的本機模型忽略;新增或移除 MCP tool 時要同步 [工具清單](mcp-tools.md)與該範本(consistency check 會抓)。

改全域規則後完全退出並重開 OpenCode,用新 session 分別測「列出所有 CodeTrail 工具」與強制 `codetrail_list_dir`。前者只列清單、不出現工具卡是正常的;後者必須出現結構化 `tool_use`。降溫與規則都完成後仍反覆失敗,才表示這顆模型 / template 組合的工具呼叫能力不穩,應換成已驗證支援 tool calling 的模型或版本。

### 模型編造不存在的具體事實(條號 / 日期 / ticket 號 / 金額)—— 幻覺 / confabulation

**症狀**:問一個你沒提供來源的問題(例如「對某廠商發 ticket 施壓」),模型回了看似可執行的細節 —— 引用「合約第 7.2 條」、「每日延遲成本 \$25K」、「3 日內回應」 —— 但這些數字 / 條號**從來沒出現在你給它的任何資料裡**,是模型自己補的。

**先講結論:這不是模型壞掉,也不是 Q4 量化的鍋,換模型解決不了。** 模型甚至能正確診斷自己的這個現象,代表它很健康。根因有兩個:

1. **沒有 grounding(來源)**。你要它引合約條款,卻沒把合約貼給它。沒有來源時,任何模型、任何精度都**不可能**猜中真實條號 —— 它只能依「訓練語料裡最常見的 `第 X.Y 條` 模式」補一個最像的數字。這是機率預測的副作用,不是故意騙人。
2. **取樣太放飛 + 走錯路徑**。純聊天走的是 **OpenCode TUI → llama-server**,**完全繞過 CodeTrail** 的 temp 0.0 + RAG + strict mode(見 [context_budget.py](../context_budget.py) 註解、`config.py` 的 `STRICT_MODE_TEMPERATURE`)。llama-server 沒帶 sampling 旗標時會使用該 build 的預設,不同版本不可硬猜;本次實測 `/props` 是 `temp 1.0 / top_k 40 / top_p 1.0 / min_p 0.05`。對 `Qwen3-235B-A22B-Thinking-2507`(官方建議 `temp 0.6 / top_p 0.95 / top_k 20 / min_p 0`)會偏高,更容易自由發揮。先用上面的 `/props` 指令看自己正在跑的真值。

**三個修法(按效果排序)**:

**① 要它講具體事實 → 先給它來源。** 想引合約就把合約貼進 prompt;程式碼問題走 CodeTrail 工具(`codetrail_*` / `aicode`)讓 RAG 把真實程式碼接進 context。沒來源的「具體數字 / 條號 / ticket 號」一律是擲骰子。

**② 在 llama-server 啟動旗標釘住取樣(這條同時修好 OpenCode 純聊天路徑)。** 在 `~/.config/codetrail/deployment.json` 的 `services.main.parameters` 加上取樣參數(README §4.1),launcher 會轉成對應旗標:

```json
{ "temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.0 }
```

(等價於 server 旗標 `--temp 0.6 --top-p 0.95 --top-k 20 --min-p 0 --presence-penalty 1.0`;改完 `~/start.sh stop` → `~/start.sh` 重啟生效。)

為什麼這裡仍建議在 server 旗標釘:OpenCode 官方支援 `agent.<name>.temperature`,但 custom openai-compatible provider 有版本相關的已知問題,可能解析了設定卻沒有把 `temperature` 送進 request body([opencode#25755](https://github.com/anomalyco/opencode/issues/25755));`top_k` / `min_p` 又不一定在 provider schema 裡。agent override 適合針對 Build agent 降溫,server 參數則是所有未明示取樣值之 request 的共同 fallback。**改完 server 設定要重啟才生效。**

**③ 在 `~/.config/opencode/AGENTS.md` 加一條防杜撰規則([全域範本](opencode-agents-template.md)已內建「事實準確性」段,裝過範本就不用再加)。** OpenCode 會把全域 `~/.config/opencode/AGENTS.md` 自動載入每一段對話(含純聊天)。加入類似:

```markdown
## 事實準確性
- 不要杜撰未提供的具體事實:合約條號、日期、ticket 編號、金額、API 名稱、檔案路徑、引用出處。
- 沒有來源可佐證時,直接說「我手上沒有這項資訊」或輸出佔位符(如 `{待填}`),不要補一個看似合理的數字。
- 區分「推測」與「事實」:要推測就明講這是推測,不要當成已知條件輸出。
```

(注意:這份 `~/.config/opencode/AGENTS.md` 是 OpenCode runtime 的全域規則,跟本 repo 根目錄那份「給修改 CodeTrail 原始碼的 AI agent 看的」`AGENTS.md` 不是同一個東西。)

**換不換模型?** 不用。換更大 / 更高精度的模型幻覺會少一點但不會消失 —— 它一樣會編沒給它的東西。真正要調的是「來源 + 取樣 + 規則」,不是模型。

> CodeTrail 自己的內部呼叫(agent loop / 全文分析 / strict 自我複查)除了 temp 0.0/0.2,也已經把 `top_p / top_k / min_p` 釘在 Qwen 建議值(`config.py` 的 `CHAT_TOP_P` / `CHAT_TOP_K` / `CHAT_MIN_P`,可用 `AICODE_CHAT_TOP_P` / `AICODE_CHAT_TOP_K` / `AICODE_CHAT_MIN_P` env 覆寫),所以即使 server 忘了帶旗標,**CodeTrail 路徑仍然是穩的**。會吃到 server 預設、需要靠上面 ② 修的,只有 OpenCode 純聊天路徑。

### `pip install huggingface_hub` 報 `error: externally-managed-environment`

Ubuntu 24.04(PEP 668)的 Python 拒絕 system-wide pip install。加 `--user --break-system-packages`:

```bash
python3 -m pip install --user --break-system-packages -U huggingface_hub
```

`--user` 把套件裝進 `~/.local/lib/pythonX.Y/site-packages`,不會動到系統 Python。新版 `huggingface_hub` 會同時提供 `hf` CLI 與 `hf_xet`;不要再另外安裝已移除的 `hf-transfer`。

### `/status` 顯示 `codetrail MCP error -32000: Connection closed`

`-32000` 不是根因,只表示 OpenCode 啟動的 MCP 子行程在完成 initialize 前退出。先完全
退出 OpenCode,在 target project 目錄檢查 client 設定與狀態:

```bash
python3 -m json.tool ~/.config/opencode/opencode.json >/dev/null
jq '.mcp.codetrail | {type, command, enabled, timeout}' ~/.config/opencode/opencode.json
command -v aicode
command -v opencode
opencode mcp list
```

`mcp` key 若是 `codetrail`,`/status` 正常應顯示 `codetrail Connected`。MCP command 有
兩種正常形式:`set_config.sh` 產生的設定會用偵測到的 Python 絕對路徑直接執行
`mcp_server.py`;手動設定則可指向目前 project git root 內由 `aicode` 產生的
`.opencode/run-codetrail-mcp`。不要因為沒看到其中某一種形式就判定設定壞掉。

OpenCode log 往往只記 `server unavailable`,不會保留子行程的完整 traceback。要看到真正
原因,在同一個 target project 目錄直接跑一次 MCP command(以下兩個絕對路徑取自上面的
`jq` 輸出；`<CODE_MODEL>` 用 `aicode` 啟動時印出的 bare model name):

```bash
cd <PROJECT_TO_ANALYZE>
AICODE_ROOT="$PWD" AICODE_MODEL=<CODE_MODEL> \
  /ABS/PYTHON /ABS/CODETRAIL/mcp_server.py
```

若看到 `[MCP] server ready, listening on stdio.`,server 本身正常,按 Ctrl-C 結束後再查
OpenCode command / wrapper。若它退出,最後一段 stderr 才是根因。常見分流:

- `[MCP][model-preflight] FAIL` → 對應的 embedding / reranker / VL server 沒 ready;
  先跑 `python3 <CODETRAIL_REPO>/scripts/required_model_servers_check.py`。
- `ModuleNotFoundError` → `mcp.codetrail.command` 指到的那顆 Python 缺依賴;用**同一顆
  Python** 安裝 `requirements.txt`,或重跑 `set_config.sh`。
- `[FATAL] AICODE_ROOT ...` → 必須從具體 project 目錄走 `aicode`,不可把 `/` 或 `$HOME`
  當 sandbox root。
- `KnowledgeStoreError` → 既有 `knowledge.json` / `knowledge_emb.npz` 不相容或不完整;
  依下一段處理。

#### `KnowledgeStoreError: ... embedding model mismatch`

這表示知識庫向量是由另一個 embedding model id 建立,例如錯誤會列出
`saved=<OLD_EMBED_MODEL>, configured=<CURRENT_EMBED_MODEL>`。CodeTrail 會 fail-loud,
避免拿不同模型的向量混算;因為失敗發生在 MCP initialize 前,OpenCode 表面只看得到
`-32000`。

不要只手改 `knowledge.json` 的 `metadata.embedding_model`:伴隨的 `knowledge_emb.npz`
也綁定 model、維度、content hash 與 generation,硬改標籤不能證明向量相容。先退出
OpenCode,把舊 store 與 embedding cache **一起保留到不會被 commit 的備份目錄**:

```bash
cd <PROJECT_TO_ANALYZE>
mkdir -p .codetrail/kb-backup-<TIMESTAMP>
mv knowledge.json .codetrail/kb-backup-<TIMESTAMP>/
mv knowledge_emb.npz .codetrail/kb-backup-<TIMESTAMP>/        # 若存在
mv .rag_embedding_cache.json .codetrail/kb-backup-<TIMESTAMP>/ # 若存在
```

如果暫時不需要文件 RAG,此時重跑 `aicode` 即可;沒有 `knowledge.json` 只代表空知識庫,
不會阻止 MCP 連線。如果仍要查原本文件,先從備份列出來源,再用**目前設定的同一顆
embedding model** 全量重建:

```bash
jq -r '.metadata.documents[]?' \
  .codetrail/kb-backup-<TIMESTAMP>/knowledge.json

# PDF / Markdown / text / binary / ELF
python3 <CODETRAIL_REPO>/RAG.py <SOURCE_FILE> knowledge.json

# 技術圖片；聊天截圖則改用 --chat
python3 <CODETRAIL_REPO>/RAG.py <SOURCE_IMAGE> knowledge.json --image -y
```

每個舊來源都重建完成後再啟動 `aicode`,用 `/status` 確認 `codetrail Connected`。
`knowledge.json`、`knowledge_emb.npz`、embedding cache 與備份都可能含 NDA 衍生資料,
不可 commit。

### `aicode_web`: Tailscale 尚未連線 / IP 無效

`aicode_web` 不猜 LAN 位址，也不 fallback 到 `0.0.0.0`。A 機必須先登入 Tailscale，且 `tailscale ip -4` 要回報一個 `100.64.0.0/10` 位址:

```bash
tailscale status
tailscale ip -4
```

看到 `NeedsLogin` / `Stopped` 時先完成 Tailscale 登入。A、B 機都 online 後，回到**要分析的專案目錄**重跑 `aicode_web`。若使用自訂 tailnet ACL，還要允許 B 機連 A 機的 web port(預設 4096)。launcher 不會操作 Tailscale Serve / Funnel，也不需要 A 機有 GUI。

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
# 1) loopback backend 有在跑嗎?(aicode_web 模式請改用它印出的 100.x URL)
curl -sS http://127.0.0.1:4096/ -o /dev/null -w '%{http_code}\n'   # 有回 HTTP 碼(200/401 等)代表 backend 活著

# 2) port 對嗎?attach 預設接 4096;web 端若用 AICODE_WEB_PORT 換過 port,attach 也要對齊
aicode attach http://127.0.0.1:<PORT>
```

如果 web backend 啟動時設了 `OPENCODE_SERVER_PASSWORD`,attach 端要帶同一組認證:

```bash
aicode attach http://127.0.0.1:4096 -p <密碼>     # username 預設 opencode,可用 -u 覆寫
```

curl 回 401 代表 backend 活著但需要密碼;完全沒回應才是 backend 沒起來、或 port / host 寫錯。

### `aicode web` / `aicode_web`: port 被占用

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

# Tailscale 背景模式(會印出新 port 的 B 機 URL)
AICODE_WEB_PORT=4097 aicode_web
```

### web UI 切了資料夾,CodeTrail 還是讀啟動時那個目錄

CodeTrail 的沙箱根(`AICODE_ROOT`)是**啟動 `aicode_web` / `aicode web` 當下那個目錄**,backend 起來時就釘死。OpenCode web UI 的「切換 WORK DIR / 開其他資料夾」只換 OpenCode 自己的 view,**不會 re-scope CodeTrail 的 MCP 沙箱** —— 所以你在 UI 切到別的資料夾後,`list_dir` / `read_file` 還是讀**啟動那個目錄**。

這不是 escape(CodeTrail 讀不到沙箱外的資料夾,只是還停在原本那個),但會誤導。**CodeTrail web 是一個 backend 一個專案**:要分析另一個專案,在那個專案目錄**另起一個 backend**(換 port):

```bash
cd ~/other-project
aicode_web stop
aicode_web
```

OpenCode 目前沒有關掉那個切換器的設定,所以請直接**無視 UI 的資料夾切換**。

### 分析不信任的 repo:擋 `opencode.json` 覆蓋你的鎖定

被分析的 repo 如果自帶 `opencode.json`(根目錄或往上到 git root),它會**覆蓋你的全域鎖定設定** —— 可能把 `permission` 的 `bash` / `read` / `write` 從 `deny` 翻成 `allow`,讓 OpenCode 內建工具繞過 CodeTrail 沙箱;整個過程靜默無提示。分析**不信任 repo** 時前面加一個 env,讓 OpenCode 忽略專案層級 config:

```bash
OPENCODE_DISABLE_PROJECT_CONFIG=1 aicode
# web 也一樣:OPENCODE_DISABLE_PROJECT_CONFIG=1 aicode_web
```

細節與實測見 [docs/security.md](security.md)。

### 啟動時拒絕 `AICODE_ROOT`

你可能在 `$HOME` 或 `/` 執行了 `aicode`。切到具體專案:

```bash
cd ~/work/some-firmware-repo
aicode
```

### `[ctx-safety] refuse to start.` 啟動被擋

主模型現在只有一個 `n_ctx`：正常在 `./set_config.sh` 輸入一次，產生 deployment 的 `services.main.ctx` 與 server `-c`。`aicode` 啟動時會讀 server `/props` 的實值，供 CodeTrail 使用並同步 OpenCode active model 的 `limit.context`；不需要另設 max。

`[ctx-safety]` 仍是必要的容量閘：如果本次 `AICODE_N_CTX`／profile 值大於 server 真正啟動的 `-c`，prompt 可能被截斷，因此會標 `UNSAFE` 並拒絕啟動。較小值不會截斷，仍可放行。

`UNSAFE` 輸出長這樣:

```
[ctx-safety] UNSAFE: model=<CODE_MODEL> requested_ctx=65536
        requested ctx=65536 超過 llama-server 啟動時的 -c 8192 (http://localhost:8080) — 多出來的 prompt 會被截斷
        ...
        建議任一處理:
          (a) 重跑 ./set_config.sh 設定主模型 n_ctx，然後重啟 server
          (b) 或把本次 AICODE_N_CTX 設成 <= 8192
          (c) 或重啟 llama-server 用 `-c 65536` (確認 VRAM 夠)
```

一般修法就是重跑設定並重啟，讓同一個主 n_ctx 重新展開到所有 consumer：

```bash
unset AICODE_DYNAMIC_NUM_CTX_MAX AICODE_NUM_CTX  # 清掉舊版 shell 設定(若有)
cd <CODETRAIL_REPO>
./set_config.sh                                  # 主 n_ctx 只填這一次
~/start.sh stop
~/start.sh
cd <PROJECT_TO_ANALYZE>
aicode
```

如果你確認要硬跑(例如想實測 truncation 的影響),用一次性放行:

```bash
AICODE_ACCEPT_CTX_RISK=1 aicode
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

新版 `aicode` 遇到單純數值漂移會直接印 `[ctx-align] FIXED`，只更新 active model 的 `limit.context`、保留其他 JSON，並建立 `opencode.json.codetrail.bak`；不再要求手動對齊。

仍看到 `FIX_FAILED`／refuse，代表設定檔損壞、無法寫入，或 active model 無法唯一定位。先確認 JSON 與 model entry：

```bash
python3 -m json.tool ~/.config/opencode/opencode.json >/dev/null
jq '{model, provider}' ~/.config/opencode/opencode.json
```

修好 JSON／model id 後重跑 `aicode` 即會再次同步。若只是一次性實驗，可以用 `AICODE_ACCEPT_CTX_RISK=1 aicode` 保留不一致且不寫檔，但不建議長期使用。

### 圖片工具剛好 10 秒超時，接著連小工具也超時

先看 `~/.config/opencode/opencode.json`：

```bash
jq '.mcp.codetrail.timeout' ~/.config/opencode/opencode.json
```

這個值的單位是毫秒，而且是每次 MCP tool call 的 client timeout。若仍是
`10000`，VL 圖片分析一超過 10 秒，OpenCode 就會先放棄等待；原本的同步圖片請求
此時可能還在 MCP server 內收尾，接下來送出的 `file_info` / `list_dir` 也會排隊，
所以表面上會像所有工具同時壞掉。

正常入口直接重新執行 `aicode`：新版 wrapper 會在 OpenCode 啟動前把既有
`mcp.codetrail.timeout` 自動同步為 660000（11 分鐘，略高於
`ingest_document` 的 10 分鐘內部上限），並備份原設定。也可單獨執行：

```bash
python3 <CODETRAIL_REPO>/scripts/opencode_mcp_timeout_check.py --fix
```

同步後的欄位會是：

```json
{
  "mcp": {
    "codetrail": {
      "timeout": 660000
    }
  }
}
```

若你是直接啟動 `opencode`、不是使用 `aicode`，同步後要完全退出並重開，已啟動的
OpenCode 不會重新讀設定。CodeTrail 自己仍會用較短的單次 VL HTTP timeout，且圖片
生成有有限 token 預算；660000 只是讓 OpenCode 不要比工具本身更早切斷。設定檔
無法解析或寫入時，`aicode` 會 fail-loud；只有緊急測試才用
`AICODE_MCP_TIMEOUT_CHECK_SKIP=1 aicode` 跳過。

若 timeout 已正確，但圖片回答像是在描述一張不存在的通用終端畫面，跑：

```bash
python3 scripts/required_model_servers_check.py
```

新版 CodeTrail 走 llama.cpp 的 `/v1/chat/completions` `image_url` 多模態格式；舊版
top-level `image_data` 可能被新版 llama.cpp 靜默忽略，造成模型只看提示詞猜圖。

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

### embedding `/health` 正常、短 `curl` 成功,但 `ingest_document` 回 500

先看 embedding server log:

```bash
tmux capture-pane -p -t codetrail-rag:embed -S -100
```

若看到 `input (...) is too large to process` 和
`increase the physical batch size (current batch size: 512)`,代表 server 雖然
ready,但 llama.cpp 的預設 physical batch `-ub 512` 放不下真實 RAG chunk。
短字串 curl 會成功,不能排除這個設定錯誤。

內建 safe-defaults 會把 embedding 與 BGE reranker 都設成
`-c 8192 -b 8192 -ub 8192`。`set_config.sh` 產生的設定維持 embedding 8192，
reranker 的 buffer 則是設定時的必答題(互動輸入或 `--rerank-ctx`),你填的值會同步
套到它的 `-c/-b/-ub`。重啟三顆附屬 server 套用:

```bash
~/start.sh stop --scope aux
~/start.sh --scope aux
```

若是手動啟動 embedding / reranker，也要讓 `-b`、`-ub` 至少容納最長輸入；
llama.cpp 的 embedding/reranking server 會要求單一輸入序列放得進 physical batch。
Qwen3-Reranker 若在 8192 buffer OOM，可重跑
`./set_config.sh --rerank-ctx 2048`(互動時在 reranker 那一組直接輸入 2048);
輸入原本就小於 2048 時不會因縮小上限而降低排序精準度。

### `aicode` 拒絕啟動,訊息說「主模型未設定」

CodeTrail 不內建主聊天 / 程式推導模型,沒設好 `aicode` 會 fail-loud。任選一種設定方式:

```bash
# 0) 最省事:重跑一鍵設定,registry / deployment / opencode.json 一次寫齊
cd <CODETRAIL_REPO> && ./set_config.sh

# 1) 環境變數 (最優先)
export AICODE_MODEL=<CODE_MODEL>

# 2) per-run CLI 旗標
aicode -m <CODE_MODEL>

# 3) ~/.config/codetrail/deployment.json 設 profile + services.main.model

# 4) ~/.config/opencode/opencode.json 設 "model": "<provider>/<CODE_MODEL>"
```

`<CODE_MODEL>` 是 MODEL_REGISTRY 裡的 bare name 或 GGUF 絕對路徑。如果你看到「placeholder」相關錯誤,通常是值還停留在 `<CODE_MODEL>` 或 `<MODEL>` 沒換掉;看到「外部 provider prefix」錯誤代表你還在用 `ollama/foo` 那種舊寫法,改成 bare name 或你 opencode.json 裡 custom provider 的 prefix。

若 `AICODE_MODEL` 和 opencode.json 同時存在,且啟動時沒有傳 `-m/--model`,兩者必須指向同一顆模型。名稱不同但 registry 解析到同一個 canonical GGUF 路徑時視為一致；其餘情況仍會 fail-loud，避免 OpenCode TUI 用 A 模型、CodeTrail MCP tools 用 B 模型。

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
