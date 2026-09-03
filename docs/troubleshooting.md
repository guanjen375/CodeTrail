# 常見問題

這份文件整理 CodeTrail 客戶端 / MCP / llama-server 常見故障排查。

[回到 README](../README.md)。

---

## 快速分流

這份文件按「安裝 / GPU → MCP / 模型行為 → KB → web → context → server →
patch / command」排列。內容很長時可先用頁面搜尋找下列關鍵字：

| 畫面或症狀 | 先搜尋 |
|---|---|
| CUDA / build 失敗 | `compute_120a`、`sm_52`、`rollback` |
| MCP Connected 但沒有真工具呼叫 | `假工具 XML` |
| MCP 連不上 / server 啟動就退出 | `initialize 前就退出` |
| 圖片或 ingest 逾時 | `超時`、`image_url` |
| web / attach 連不上 | `Tailscale`、`attach`、`port 被占用` |
| context 啟動閘擋下 | `ctx-safety` |
| server / RAG 異常 | `llama-server 不可連`、`embedding`、`查 spec 沒結果` |
| 修改工具被拒 | `apply_patch`、`run_command` |
| 送出新問題卻先跑出一段摘要 / 壓縮停住要你重送 | `壓縮` |

### Build llama.cpp 時 `nvcc fatal : Unsupported gpu architecture 'compute_120a'`

你的 GPU 是 Blackwell(RTX 50 系列或 RTX PRO 6000 Blackwell),但本機 CUDA Toolkit 太舊,不認識 `sm_120` / `compute_120a`。Ubuntu 24.04 的 `nvidia-cuda-toolkit` 套件停在 12.0,**Blackwell 需要 12.8+**。

驗證:

```bash
nvidia-smi | grep "CUDA Version"   # 驅動上限,>= 12.8 才有救
nvcc --version                      # 已安裝 toolkit
```

修法見 [README §1.4](../README.md#14-blackwell-gpu-需要-cuda-toolkit-128-以上)。重點順序:

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

mmap 模式下 expert weights 是懶載入,第一次推理時要從 SSD page-in 大量 expert 到 RAM,TTFT 容易破 60 秒。CodeTrail 客戶端 會卡在「`...esc interrupt`」很久,**不要按 Esc** —— 它有在跑,只是慢。

驗證是「慢」不是「卡」:

```bash
curl -s http://localhost:8080/slots | python3 -m json.tool   # 看主 server 是否 is_processing
nvidia-smi -l 1                                              # GPU 是否在動
```

若 slot 都 idle、GPU 0% 連續超過 30 秒,代表請求**沒打到 server**(問題在客戶端 / MCP 層,不是 llama-server)。

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

### `aicode` 說「偵測到舊 OpenCode 安裝留下的 CodeTrail 設定」

CodeTrail 已不再啟動 OpenCode。但舊版曾經把幾個值寫進你的
`~/.config/opencode/opencode.json`:壓縮的四個受管 `compaction.*` 鍵,以及兩個
指向本 repo 的 plugin 項。

留著不管的後果不是「多幾個沒用的設定」:

- `compaction.auto = false` 會一直生效——那是 CodeTrail 接管時關掉的,不還原的話
  你的 OpenCode 從此不再自動壓縮,而且沒有人負責。
- plugin 項指向本 repo 的檔案路徑。那兩個檔一旦被刪,你在**其他專案**開 OpenCode
  都會因為載不到 plugin 而起不來,而錯誤訊息不會提到 CodeTrail。

```bash
python3 <CODETRAIL_REPO>/opencode_migrate.py --check   # 先看會做什麼(不寫檔)
./set_config.sh                                        # 寫完自己的設定後接著遷移(失敗 exit 2)
```

遷移只還原**現值仍等於 CodeTrail 寫入值**的鍵(你自己改過的一律不動)、只移除
path 對得上的 plugin 項;`mcp.codetrail` 與 `permission` **不動**——`mcp_server.py`
仍然可以被任何 MCP client 用。有備份。

過渡期那兩個 plugin 檔留成 inert stub:只在 session 建立時 toast 一次「請執行遷移」,
不掛任何其他 hook、不改任何工具結果。遷移完成後註冊項會被移除,它們就再也不會被載入。

<a id="mcp-connected-but-no-tool-call"></a>
### `/tools` 列得出 19 個,但模型說沒有 CodeTrail 或只印出假工具 XML

**典型症狀**:

- `/tools` 明明列得出 19 個工具,模型卻回答「沒有 CodeTrail 工具」。
- 明確要求 `list_dir(path=".", depth=1)` 後,模型只輸出 `<list_dir path="." depth="1"/>`,接著用自然語言宣稱「已成功取得目錄」,畫面上沒有 `· list_dir → completed`、也沒有真實目錄內容。
- 模型每輪都回答「我現在呼叫」「讓我直接使用工具」，但訊息隨即結束；使用者催促後只換句話重複，始終沒有工具卡。

**先講結論:模型對「自己有哪些工具」的文字回答不是診斷資料,XML 長得像 tool call 也不代表執行過。** 要把三層狀態分開看:

| 層次 | 能證明什麼 | 怎麼驗證 |
|---|---|---|
| MCP 連線 | 客戶端已啟動 CodeTrail 子行程並完成 initialize | 客戶端的 `/tools` |
| 工具註冊 | client 收到 CodeTrail 的工具 schema | 客戶端的 `/tools`;完整名稱見 [MCP 工具清單](mcp-tools.md) |
| 本輪實際執行 | 模型真的發出結構化 tool call,client 執行後把結果送回模型 | TUI 的 `· <工具> → completed`,或 JSON event 的 `type: "tool_use"`、`part.state.status: "completed"` |

`aicode` 把 transport、explicit hard gate 與 implicit diagnostic 分開，不需要
每次先叫模型背 19 個名字：

- `MCP PASS — 19 tools + list_dir round-trip`：每次啟動都另起一個 MCP server 子行程，完成
  `initialize`、依固定順序精確比對 19 個名稱，擷取完整 typed schemas／instructions digest，
  再執行無副作用的 `list_dir(path=".", depth=1)`。這層完全不問 LLM；schema 的
  bounds/description/budget 由 static contract 驗證，routing catalog 另保存逐工具
  counts/digests 與 token measurement。
- `MODEL live canary — <原因>`／`MODEL PASS — ...`：explicit prompt **點名**
  `list_dir`，只接受 JSON stream 裡 completed 的結構化 `tool_use`。純文字／XML
  和模型自行宣稱成功都不算。首次失敗 retry 一次；第二次才成功印 `MODEL FLAKY`、本次
  放行但不快取；連續兩次失敗 exit 2。
- `IMPLICIT live diagnostic ...` 之後只會是 `status=optimal|suboptimal|fail|timeout`。
  這一輪 prompt 不含工具名，只跑一次：exact completed root `list_dir` 是 `optimal`；選到
  其他 allowlisted CodeTrail 唯讀工具是 `suboptimal`；沒有合格 call 是 `fail`；超時是
  `timeout`。後三態會印 `IMPLICIT WARN`，但**四態都不擋啟動**。

explicit 與 implicit 是兩條獨立 cache lane，不會互借另一列結果。fingerprint
包含 selected/runtime identity、客戶端檔案(`client_engine` / `client_prompt` /
`codetrail_chat`,含 `AICODE_CLIENT_ENTRY` 覆寫後的那一份)與 system prompt 的 digest、
live tools/instructions、專案 AGENTS、lessons 與 server `/props`（含 chat template、
capabilities、build/取樣資訊）。cache 只存 hash、lane status、檢查時間與版本，不存 prompt、
模型輸出、tool args/result、檔名、目錄內容、session id 或專案路徑；抽查用的對話
也會刪除。`/props` 明確回 `chat_template_caps.supports_tools=false` 時，在任何 model attempt
前直接 fail-loud；缺欄位才繼續探針。

這是啟動抽查，不是「往後每個生成 token 都保證正確」。如果 TUI 裡稍後又碰到偶發失手，
可直接退出後強制重測兩條 model lane：

```bash
AICODE_TOOL_CANARY_FORCE=1 aicode
```

`AICODE_TOOL_CANARY_WARN_ONLY` 已**不能**略過 MCP／explicit hard gate；只有 implicit
本來就不擋。`SKIP` 會連 MCP 與兩條模型檢查都不執行，只能做緊急救援，不能當驗收：

```bash
AICODE_TOOL_CANARY_SKIP=1 aicode
```

預設 cache TTL 是 86400 秒；需要更頻繁抽查可設
`AICODE_TOOL_CANARY_TTL_SECONDS=<SECONDS>`（`0` 等同每次 live）。FAIL 訊息會刻意區分
MCP/catalog、explicit model/chat-template 與 non-blocking implicit routing，
避免再把它們混為一談。

`AICODE_MODEL=<CODE_MODEL> python3 scripts/doctor.py` 會重建 current fingerprint，只回報該列
的 implicit `optimal/suboptimal/fail/timeout` 與 fresh/stale；資料不足顯示 `unknown`，不會拿
另一個模型／設定／專案的 cache row 冒充現況。

### TUI 跳出 CodeTrail 的 toast(待處理項目 / 沒有真的呼叫工具)

ingest 的待辦通知現在由客戶端自己處理(`client_notify.py`),不再需要註冊任何 plugin。

- **只認 `ingest_document` 的結果**,而且 marker 只認**行首**。任何工具都能回傳含
  marker 的文字(`read_file` 讀到一份講 marker 的文件就會);不限定工具名等於讓任何
  檔案內容偽造一則待辦。marker 用 `in` 判也會誤觸——結果裡那條「可以直接複製的 CLI
  命令」必須逐字保留檔名,而檔名可以合法地叫 `[CODETRAIL_ACTION_REQUIRED].pdf`。
- **失敗優先**:兩個 marker 同時出現時提示的是「這次 ingest 失敗」,不是「有待覆核的
  圖」——後者會把你導去修一份根本沒進 KB 的文件。
- **假工具呼叫偵測**:助理用純文字宣稱「我現在來呼叫某工具」卻沒有任何結構化 tool
  call 時,客戶端提示一次並寫一筆零內容的 incident(`promise_without_call`)。判準只認
  明確的宣告句,而且動詞與「工具」之間不得出現否定詞——「我沒有呼叫任何工具」不會命中。

### 送出新問題卻先跑出一段摘要,或壓縮停住要你重送

先確認你選了哪個壓縮模式:`aicode` 啟動橫幅有一行 `[aicode] 壓縮模式=...`,
`python3 scripts/doctor.py` 的 `-- 壓縮模式 --` 一段則會再印出有效設定跟它一不一致。
三種模式的完整說明、門檻公式與取捨在 [compaction-rules.md](compaction-rules.md)。

**`codetrail` / `manual` 還在測試階段**(兩處顯示都會標 🧪):行為與受管值可能再變。
遇到下表以外的怪狀況,先 `./set_config.sh --compaction-mode off` 關掉自動壓縮再回報
——`off` 不會動你的對話,只是不再自動摘要;context 滿了會是一個可見的錯誤。

| 畫面 | 意思 | 怎麼辦 |
|---|---|---|
| 送出新問題,畫面先跑一段摘要才回答 | 這台機器還沒選過壓縮模式(沒有 `client.json`),或選的是 `manual` | 重跑 `./set_config.sh` 選 `codetrail`,再重開 `aicode` |
| 畫面說「壓縮產生了空摘要」/「壓縮請求出錯」 | 摘要是空的、只有 thinking、或請求掛了。摘要**沒有落地**,對話原封不動 | 這個對話的自動壓縮已停用(跨行程保留)。開一個新對話,把還看得到的問題與必要狀態重送 |
| 畫面說「摘要沒有照七欄格式輸出」 | 模型照了別套欄位(實測看過整份換成英文五欄)。「已確定事實 vs 未確認」的分離沒了 | 摘要沒有落地,對話可以繼續;要繼續用結構化壓縮就開新對話,同一個模型一直不遵守就改 `off` |
| 壓縮完緊接著又壓一次 | `tail_turns = 1` 讓最新一輪逐字留著;那一輪很長時,壓完的 context 是「摘要 + 長 tail」 | 正常,不是迴圈(同一則助理訊息不會被當第二次的錨點)。把超長單輪拆小或把 `n_ctx` 調大 |
| 畫面說「這個 n_ctx 推不出可用的壓縮門檻」 | ctx 太小,公式算出來的 threshold / tail_cap 低於下限 | 把 `n_ctx` 調大重跑 `./set_config.sh`,或把模式切成 `off` |
| `/compact` 沒有作用 | headless `run` 沒有互動指令 | 用終端客戶端(直接 `aicode`)或 web 介面 |


`codetrail_chat.py run`(headless)只輸出事件流,上面的提示不會出現。停用會留一筆
`compaction_stopped` 的零內容 incident(與 MCP incident 同一個檔)——
`python3 scripts/doctor.py` 會統計並印出最近幾筆的 `kind/detail`。

**壓縮設定改了要重開客戶端才生效**:`client.json` 只在啟動時讀一次。

### MCP lease 與 incident:session 中途「模型說沒工具」到底是哪一層掉的

啟動抽查只證明「那一刻是好的」。session 開了幾小時之後才失手時,要分的是三種
完全不同的原因,而它們在畫面上長得一模一樣:

| 現象 | 真正發生的事 | 看哪裡 |
|---|---|---|
| MCP server 已經不在了 | 子行程被 SIGKILL / OOM / client 收掉 | lease 是 `stale` |
| server 活著,但模型沒發 structured call | 模型只用文字宣稱「我呼叫了工具」 | lease 是 `live`,`incident kind=promise_without_call` |
| 呼叫發出去了但 client 端失敗 | 子行程沒回應、逾時被取消 | `incident kind=client_mcp_failed` / `structured_call_failed` |

每個 MCP server 行程啟動時會在 `~/.local/state/codetrail/mcp/<boot_id>.json`
(遵守 `XDG_STATE_HOME`)開一份自己的 **lease**。一份行程一個檔,不是共用一個心跳檔
——canary、TUI、web、headless `run` 各起一個 MCP 子行程,共用一個檔只會互相覆寫。
lease 裡只有 pid / ppid / 開始與更新時間 / `tools/list` 次數 / 最後一個工具名與狀態,
**沒有**工具參數、結果、檔名或路徑,權限 0600。

`AICODE_MODEL=<CODE_MODEL> python3 scripts/doctor.py` 的 `-- MCP lease / incidents --`
那一段會把它們攤開,四種狀態的意思是:

- `live` —— pid 還在,而且該行程的啟動時刻(`/proc/<pid>/stat` 第 22 欄)與 lease
  開檔當下記下的那一格**逐值相同**。這是精確身分比對,不是時間窗:pid 會被重用,
  重用出來的行程本來就落在任何合理的時間窗裡面。
- `exited` —— server 自己正常收尾寫下的退出時間與原因。**只有正常關閉才會有**。
- `stale` —— lease 停在最後一次寫入、pid 已經不在。SIGKILL / OOM 會長這樣,
  client 正常收掉子行程也會長這樣,所以單獨出現不代表故障。
- `unknown` —— pid 還在但啟動時刻對不上(pid 被重用)、這台機器讀不到行程資訊,
  或那是一份還沒有身分欄位的舊 lease。這裡刻意**不猜**:寧可說不知道,
  也不要宣稱一個早就死掉的 server 還活著。

壓縮的停用紀錄是**另一個**檔:`~/.local/state/codetrail/compaction-stopped.jsonl`
(0600,超過 256 KiB 轉存 `.1`)。每行只有 `schema`、時間、session 雜湊與固定 slug,
用來讓「已對這個 session 停用」跨行程重開仍然有效(檔案與目錄都拒 symlink 與 hard link)。

incident 由客戶端與 MCP server 寫在 `~/.local/state/codetrail/incidents.jsonl`
(0600,超過 1 MiB 轉存 `incidents.jsonl.1`,只留一份)。每行只有時間、`kind`、
固定 slug 的 `detail`、來源,以及 **session id 的 sha256 前 16 碼**——沒有原始
session id、沒有訊息內容、沒有路徑。doctor 印的「共 N 筆」與「最近 7 天 N 筆」
都是掃完兩個檔算出來的完整數字,不是尾巴取樣;最近 7 天有紀錄時印 WARN。
要重來一次直接刪掉這兩個檔即可,它們純粹是診斷資料。

真的碰上時的處置順序:lease 是 `stale` → 重開 session(server 已經不在,重試沒有用);
lease 是 `live` 而 incident 是 `promise_without_call` → 退出後
`AICODE_TOOL_CANARY_FORCE=1 aicode` 強制重測兩條 model lane。

### 什麼情況才算「這個模型可以發布」

`scripts/eval_tool_routing.py` 的 support gate 每一項都要過,其中兩項就是為了不讓
「模型說它呼叫了工具」變成證據:

- `explicit_canary_100_percent` —— 點名 `list_dir` 的 explicit canary
  必須**全部**成功(`scripts/tool_call_canary.py` 的 hard gate,純文字或假 XML 不算)。
- `structured_call_success` —— routing eval 的 **structured-call 成功率**要達到
  `gates.structured_call_success_min`(預設 1.0)。這個比率由既有量測相乘得到:
  `schema.valid_rate` ×(1 − 沒有 structured call 的回合佔比)。
  後面那一項用的是**直接計數** `failure_guards.no_structured_call`:
  「該呼叫工具(`tool_needed`)、卻一個 structured call 都沒有」的回合數,
  分母是 `tool_needed.count`。
  - 不用「把 `promise_without_call` / `empty_turn` / `marker_leak` 三種分類相加」
    當代理值:只要有一種「沒發呼叫」的情況被歸到別的分類,代理值就漏算,
    成功率會虛報成 1.0。
  - 也**不能**把正確的 no-tool 案例算進去(它們必然沒有 call):算進去的話
    一次完美路由也達不到 1.0,門檻變成不可達。所以分子分母都限定在
    `tool_needed` 這個母體,`model_denominator`(全部 valid 案例)不當退路。
  - 量不到一律當**不通過**——沒有 schema 量測,或 `tool_needed.count` 缺席 / 為 0
    都算量不到,這時不會退回去用 `schema.valid_rate` 頂替(那個數字只說
    「已經送達的 call 有多少合格」,一次 call 都沒送出去的 run 它照樣是 1.0)。

兩項都過才有資格談發布;`measured` 不會自動升 `supported`,狀態一律人工決定。

### 工具結果第一行是 `status: partial` 或出現 `context_risk`

所有工具結果的 compact text lane 第一行固定是 `status: ok|partial|error`。`partial` 不是
工具失敗，通常代表 core result 自己標示 truncated、repeat guard 中止重複呼叫，或整份文字
超過 context result budget。第二行 `next:` 是續讀／縮小範圍的動作：`read_file` 請直接用
它給的下一個 `start_line`，`grep_code` 縮小 path/include/pattern，`list_dir` 縮小 path/depth。

省略 `max_chars` 時，budget 依**呼叫當下**主模型 `n_ctx` 的 12% token proxy 計算，不再固定
為 12,000 字元；結果仍受工具 safety cap。明示 `max_chars` 大於該 12% 預設時，回傳會加
`context_risk: explicit max_chars exceeds the default 12% context budget`，提醒這次可能擠壓
後續對話；不是靜默 clamp，也不是 server error。若看到 `[result truncated by context budget]`，
照 `next:` 分頁／縮窄，不要用同參數連續重送。`code_rag_search` 的 `used_chars` 仍只計
evidence text 字元，不是 tokenizer token。

**「剛 ingest 文件就失憶」不等於整份 RAG 塞爆 context。** `ingest_document` 把全文切 chunk 後寫進 `knowledge.json`,它送回目前對話的只有有長度上限的執行摘要;`reload_knowledge_base` 只更新 MCP process 內的 KB singleton。只有之後呼叫 `query_knowledge` 時,召回的少量 REF 才會以 tool result 進入那個 session。新 session 不會因為 KB 裡文件變多就自動攜帶全文。同一個舊 session 累積很多 tool result 時仍可能變長,但要看實際 token / compaction,不能只看 ingest 發生過就下結論。

這次實際失敗案例是全新 session:`step_finish.tokens.total=9001`、模型上限 131072,且沒有 compaction;其中 `input=522`、`cache.read=8355`,因此 RAG overflow 可直接排除。`--format json` 的 `step_finish` event 可用來看重現請求的 tokens;本例的 cache read 是可重用的 system / tool schema prefix,而 `cache.read` 數值本身也不能當成「整份 KB 已注入」的證據。

若要繞過 wrapper 做更底層的手動重現,用一個全新 session(不要沿用已經多次回答「工具不
存在」的舊對話——舊上下文本身可能讓模型繼續模仿錯誤答案):

```bash
cd <PROJECT_TO_ANALYZE>
python3 <CODETRAIL_REPO>/codetrail_chat.py --policy readonly run --format json \
  '請立即呼叫 list_dir，path="."、depth=1。必須實際呼叫工具。'
```

真的呼叫時,JSON stream 會出現 `type: "tool_use"`、`part.tool: "list_dir"` 與
`part.state.status: "completed"`,step 結束原因是 `tool-calls`;只看到 assistant 的 XML /
純文字且以 `stop` 結束,就是模型模擬了呼叫。不要以「模型說 retrieved successfully」當成功
證據。

這種情況常見於本機模型的 tool-call 格式不穩。客戶端送出的取樣值是固定的
(`config.py` 的 `CHAT_TOP_P` / `CHAT_TOP_K` / `CHAT_MIN_P` 與 `EngineOptions.temperature`
預設 0.2),所以**不需要**再去改任何前端設定;要動就動 llama-server 端的預設,把下面的鍵
**合併進既有** `~/.config/codetrail/deployment.json`(保留其他 service / model / port):

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

改 server 設定後執行 `~/start.sh stop` → `~/start.sh` 重啟才會生效。`set_config.sh` 重跑時會
保留手動加入的 allowlisted 取樣參數。重啟後不要只看 JSON,直接確認 server 實際預設已變成
`0.0`:

```bash
curl -s http://localhost:8080/props \
  | jq '.default_generation_settings.params.temperature'
```

若模型輸出的格式名稱跟目前 chat template 完全不同,可再確認 llama-server 載入的 template:

```bash
curl -s http://localhost:8080/props | jq -r '.chat_template' \
  | rg 'tool_calls|invoke|DSML'
```

例如模型只寫出自創的 `<list_dir .../>`,不會因為看起來像 XML 就被客戶端當成結構化呼叫 ——
判準是 `/v1/chat/completions` 回來的 `tool_calls` 欄位。不要靠 prompt 手寫 / 猜測底層
tool-call markup;那是 llama.cpp chat template 的工作。

若 server 已降溫但模型仍反覆承諾呼叫,檢查**進了 system prompt 的東西有多少**。
歷史教訓:舊版曾把 19 個工具、RAG、graph、figure 與 lessons 操作全部塞進每一輪都載入的
全域規則,實測結果是以 `stop` 結束且零 tool call;只把那份範本拿掉、其餘不變,就正確呼叫
`list_dir`。所以這種症狀是**提示過載**,不是 MCP transport 壞掉。

客戶端的內建基底規則有 1,600 字元硬上限(`client_prompt.BASE_RULES_MAX_CHARS`,超過就
import 失敗),完整工具名稱留在 [工具清單](mcp-tools.md),不再注入每一輪。會讓 system
prompt 變長的是你自己的三個來源,依序檢查:

```bash
wc -c <PROJECT>/AGENTS.md <PROJECT>/.codetrail/lessons.md \
      ~/.config/codetrail/instructions.md 2>/dev/null
```

要一次全部排除,用 `CODETRAIL_DISABLE_PROJECT_INSTRUCTIONS=1 aicode`(不讀專案那兩份)。

改完重開一個新 session,再執行 `AICODE_TOOL_CANARY_FORCE=1 aicode` 略過舊 cache。驗收時直接
要求一次真實 `list_dir`;必須出現 `· list_dir → completed`,只有文字承諾不算。精簡、降溫與
新 session 都完成後仍反覆失敗,才判定這顆模型 / template 組合的工具呼叫能力不穩,改用已量測
支援 tool calling 的組合。不要把 `tool_choice=required` 當萬用補丁;完整 prompt A/B 中它輸出
到長度上限仍沒有 tool call。

### 模型編造不存在的具體事實(條號 / 日期 / ticket 號 / 金額)—— 幻覺 / confabulation

**症狀**:問一個你沒提供來源的問題(例如「對某廠商發 ticket 施壓」),模型回了看似可執行的細節 —— 引用「合約第 7.2 條」、「每日延遲成本 \$25K」、「3 日內回應」 —— 但這些數字 / 條號**從來沒出現在你給它的任何資料裡**,是模型自己補的。

**先講結論:這不是模型壞掉,也不是 Q4 量化的鍋,換模型解決不了。** 模型甚至能正確診斷自己的這個現象,代表它很健康。根因有兩個:

1. **沒有 grounding(來源)**。你要它引合約條款,卻沒把合約貼給它。沒有來源時,任何模型、任何精度都**不可能**猜中真實條號 —— 它只能依「訓練語料裡最常見的 `第 X.Y 條` 模式」補一個最像的數字。這是機率預測的副作用,不是故意騙人。
2. **取樣太放飛 + 走錯路徑**。純聊天走的是 **CodeTrail 客戶端 → llama-server**,**完全繞過 CodeTrail** 的 temp 0.0 + RAG + strict mode(見 [context_budget.py](../context_budget.py) 註解、`config.py` 的 `STRICT_MODE_TEMPERATURE`)。llama-server 沒帶 sampling 旗標時會使用該 build 的預設,不同版本不可硬猜;本次實測 `/props` 是 `temp 1.0 / top_k 40 / top_p 1.0 / min_p 0.05`。對 `Qwen3-235B-A22B-Thinking-2507`(官方建議 `temp 0.6 / top_p 0.95 / top_k 20 / min_p 0`)會偏高,更容易自由發揮。先用上面的 `/props` 指令看自己正在跑的真值。

**三個修法(按效果排序)**:

**① 要它講具體事實 → 先給它來源。** 想引合約就把合約貼進 prompt;程式碼問題走 CodeTrail 工具(`codetrail_*` / `aicode`)讓 RAG 把真實程式碼接進 context。沒來源的「具體數字 / 條號 / ticket 號」一律是擲骰子。

**② 在 llama-server 啟動旗標釘住取樣。** 在 `~/.config/codetrail/deployment.json` 的 `services.main.parameters` 加上取樣參數(README §4.1),launcher 會轉成對應旗標:

```json
{ "temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.0 }
```

(等價於 server 旗標 `--temp 0.6 --top-p 0.95 --top-k 20 --min-p 0 --presence-penalty 1.0`;改完 `~/start.sh stop` → `~/start.sh` 重啟生效。)

為什麼是 server 旗標而不是客戶端設定:客戶端每一次請求都已經明示帶上
`temperature` / `top_p` / `top_k` / `min_p`,所以它那條路徑本來就是釘死的。server 參數管的是
**所有未明示取樣值的 request**(例如你自己拿 `curl` 打 `/v1/chat/completions` 試東西),
那是這一節真正要修的東西。**改完 server 設定要重啟才生效。**

**③ 只保留一條短防杜撰規則。** 客戶端的內建基底規則已內建等價約束，不要再加。
真的要補充時,寫進 `~/.config/codetrail/instructions.md`,最多類似這一行：

```markdown
- 不要杜撰未提供的條號、日期、數字、API、路徑或引用；沒有證據就明說沒有，推測必須標明。
```

不要把每種幻覺各寫一段規則或附大量反例；全域檔會進入每一輪，提示過載本身也會降低工具
呼叫遵循率。

(注意:`~/.config/codetrail/instructions.md` 是**你自己的**跨專案規則;專案根目錄的
`AGENTS.md` 是那個專案的規則;本 repo 根目錄那份 `AGENTS.md` 則是「給修改 CodeTrail
原始碼的 AI agent 看的」,三者不是同一個東西。)

**換不換模型?** 不用。換更大 / 更高精度的模型幻覺會少一點但不會消失 —— 它一樣會編沒給它的東西。真正要調的是「來源 + 取樣 + 規則」,不是模型。

> CodeTrail 自己的內部呼叫(agent loop / 全文分析 / strict 自我複查)除了 temp 0.0/0.2,也已經把 `top_p / top_k / min_p` 釘在 Qwen 建議值(`config.py` 的 `CHAT_TOP_P` / `CHAT_TOP_K` / `CHAT_MIN_P`,可用 `AICODE_CHAT_TOP_P` / `AICODE_CHAT_TOP_K` / `AICODE_CHAT_MIN_P` env 覆寫),所以即使 server 忘了帶旗標,**CodeTrail 路徑仍然是穩的**。會吃到 server 預設、需要靠上面 ② 修的,是不經客戶端的直接呼叫(例如你自己的 `curl`)。

### `pip install huggingface_hub` 報 `error: externally-managed-environment`

Ubuntu 24.04(PEP 668)的 Python 拒絕 system-wide pip install。加 `--user --break-system-packages`:

```bash
python3 -m pip install --user --break-system-packages -U huggingface_hub
```

`--user` 把套件裝進 `~/.local/lib/pythonX.Y/site-packages`,不會動到系統 Python。新版 `huggingface_hub` 會同時提供 `hf` CLI 與 `hf_xet`;不要再另外安裝已移除的 `hf-transfer`。

### 啟動時 MCP 連不上 / `MCP FAIL` / server 在 initialize 前就退出

`aicode` 的 preflight 會直接起一次 MCP server;它在完成 `initialize` 前退出時,畫面上只會
說連不上,真正的原因在子行程的 stderr(**預設不落檔**——它含查詢原文與絕對路徑,只在記憶體
留有上限的尾端)。要看到完整輸出,在同一個 target project 目錄手動跑一次同一條命令:

```bash
cd <PROJECT_TO_ANALYZE>
AICODE_ROOT="$PWD" AICODE_MODEL=<CODE_MODEL> \
  python3 <CODETRAIL_REPO>/mcp_server.py
```

(`<CODE_MODEL>` 用 `aicode` 啟動時印出的 bare model name;如果 CodeTrail 依賴裝在 venv,
先 activate 再跑——客戶端是用**啟動它的那顆 Python** 去 spawn server 的。)

若看到 `[MCP] server ready, listening on stdio.`,server 本身正常,按 Ctrl-C 結束。
若它退出,最後一段 stderr 才是根因。常見分流:

- `[MCP][model-preflight] FAIL` → 對應的 embedding / reranker / VL server 沒 ready;
  先跑 `python3 <CODETRAIL_REPO>/scripts/required_model_servers_check.py`。
- `ModuleNotFoundError` → 你用來啟動 `aicode` 的那顆 Python 缺依賴;用**同一顆
  Python** 安裝 `requirements.txt`,或重跑 `set_config.sh`。
- `[FATAL] AICODE_ROOT ...` → 必須從具體 project 目錄走 `aicode`,不可把 `/` 或 `$HOME`
  當 sandbox root。
- `KnowledgeStoreError` → 既有 `knowledge.json` 與程式自管的 embeddings cache 不相容
  或不完整;依下一段處理。

#### KB 只有 `knowledge.json` 要管

向量不是使用者要維護的檔案。它住在 `.codetrail/cache/embeddings/<kb-id>/`,由程式
自己管:

| 狀況 | 行為 |
| --- | --- |
| cache 不存在 | 依 `knowledge.json` 自動重建（`[INFO] embeddings cache 不存在或已過期…`） |
| cache 的 generation / 內容雜湊 / 逐列 chunk id / model 對不上 | 舊 cache 一律丟棄並重建 |
| 重建不了（例如 embedding server 連不上） | `[FATAL] embeddings cache 無法重建,未使用舊向量;查詢已中止。` |
| `knowledge.json` 不存在 | KB 視為空,並清掉無主的 cache（`[INFO] knowledge.json 不存在…`） |
| 舊版本留在 KB 旁邊的 `knowledge_emb.npz` | 完整驗證通過就遷移進 cache 再收掉;驗不過就淘汰並重建 |

所以：**備份 / 複製 / 刪除知識庫只需要動 `knowledge.json`**。刪掉它就是空知識庫,
不會有一份舊向量在旁邊讓人以為知識庫還在;把另一份 `knowledge.json` 複製過來覆蓋,
即使 chunk 數剛好一樣也不會拿舊向量硬配（逐列 chunk id 會抓到）。cache 目錄可以隨時
刪,下一次載入會自己長回來。

要「一步到位重建成只有某一份文件」時用 fresh 模式:

```bash
python3 <CODETRAIL_REPO>/RAG.py <SOURCE_FILE> knowledge.json --fresh
python3 <CODETRAIL_REPO>/RAG.py rebuild --kb knowledge.json <SOURCE_FILE>... --fresh
```

MCP 端對應 `ingest_document(path, fresh=True)`。它清空既有 chunks、讓舊 cache 失效、
只留這一份文件,而且不會因 fresh 整批刪除 `.codetrail/figures/` artifact。同一份文件
重新 ingest 時,只有來源像素、頁碼與正規化 bbox 都相同的 `human_verified` 人工覆核才會
沿用;被移出 KB 的其他文件即使 artifact 還在,之後重新 ingest 也不會自動恢復人工確認,
revision 會回到 1。中途失敗會整批回滾,不會留下「新 JSON 配舊向量」。

#### `KnowledgeStoreError: ... embedding model mismatch`

這表示知識庫向量是由另一個 embedding model id 建立,例如錯誤會列出
`saved=<OLD_EMBED_MODEL>, configured=<CURRENT_EMBED_MODEL>`。CodeTrail 會 fail-loud,
避免拿不同模型的向量混算;因為失敗發生在 MCP initialize 前,畫面上只看得到「連不上」,
真正的訊息在 server stderr(見上一節怎麼手動跑一次看它)。

不要只手改 `knowledge.json` 的 `metadata.embedding_model`:那個欄位是 KB 對「自己是
哪個模型建的」的宣告,硬改標籤不能證明 chunk 切法與向量相容。先退出客戶端,把舊
store **保留到不會被 commit 的備份目錄**:

```bash
cd <PROJECT_TO_ANALYZE>
mkdir -p .codetrail/kb-backup-<TIMESTAMP>
mv knowledge.json .codetrail/kb-backup-<TIMESTAMP>/
```

只要搬走 `knowledge.json` 就夠了 —— embeddings cache 是可重建資料,下一次載入時會
自己被清掉；`.rag_embedding_cache.json`（文字→向量的增量快取）同理,留著只會讓之後
的重建更快。

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
`knowledge.json`、`.codetrail/`（含 embeddings cache 與 figure artifacts）、embedding
cache 與備份都可能含 NDA 衍生資料,不可 commit。

### `aicode_web`: Tailscale 尚未連線 / IP 無效

`aicode_web` 不猜 LAN 位址，也不 fallback 到 `0.0.0.0`。A 機必須先登入 Tailscale，且 `tailscale ip -4` 要回報一個 `100.64.0.0/10` 位址:

```bash
tailscale status
tailscale ip -4
```

看到 `NeedsLogin` / `Stopped` 時先完成 Tailscale 登入。A、B 機都 online 後，回到**要分析的專案目錄**重跑 `aicode_web`。若使用自訂 tailnet ACL，還要允許 B 機連 A 機的 web port(預設 4096)。launcher 不會操作 Tailscale Serve / Funnel，也不需要 A 機有 GUI。

### `aicode attach`: 連不上 backend

`aicode attach` 是純 client,連不上通常代表 backend 沒在跑、或 url / port 不對。逐項確認:

```bash
# 1) loopback backend 有在跑嗎?(aicode_web 模式請改用它印出的 100.x URL)
curl -sS http://127.0.0.1:4096/ -o /dev/null -w '%{http_code}\n'   # 有回 HTTP 碼(200/401 等)代表 backend 活著

# 2) port 對嗎?attach 預設接 4096;web 端若用 AICODE_WEB_PORT 換過 port,attach 也要對齊
aicode attach http://127.0.0.1:<PORT>
```

如果 web backend 啟動時設了 `AICODE_WEB_PASSWORD`,attach 端要用**同一個環境變數**
(它不是 CLI 參數:打在命令列會留在 shell history 與 `ps` 輸出裡):

```bash
AICODE_WEB_PASSWORD=<密碼> aicode attach http://127.0.0.1:4096
```

接上之後,`/sessions` 列出這個專案已保存的對話、`/resume <id>` 接續其中一筆、
`-c` 直接接續最近一次(`aicode attach -c`)。

curl 回 401 代表 backend 活著但需要密碼;完全沒回應才是 backend 沒起來、或 port / host 寫錯。

### `aicode web` / `aicode_web`: port 被占用

`aicode web` 刻意固定 port(預設 4096),被占用時不會自動換 port,直接報錯。先看誰占用:

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

CodeTrail 的沙箱根(`AICODE_ROOT`)是**啟動 `aicode_web` / `aicode web` 當下那個目錄**,
backend 起來時就釘死,web 介面沒有切換它的方法。**CodeTrail web 是一個 backend 一個專案**:
要分析另一個專案,在那個專案目錄**另起一個 backend**(換 port):

```bash
cd ~/other-project
aicode_web stop
aicode_web
```

### 分析不信任的 repo:擋專案自帶的指示

被分析的 repo 影響得到的是**送進模型的指示**:根目錄的 `AGENTS.md` 與
`.codetrail/lessons.md` 每一輪都會進 system prompt(權限不受它們影響——那由
`client_policy` 與 `client.json` 決定)。分析**不信任 repo** 時前面加一個 env,
讓客戶端完全不讀專案內那兩份:

```bash
CODETRAIL_DISABLE_PROJECT_INSTRUCTIONS=1 aicode
# web 也一樣:CODETRAIL_DISABLE_PROJECT_INSTRUCTIONS=1 aicode_web
```

細節與實測見 [docs/security.md](security.md)。

### 啟動時拒絕 `AICODE_ROOT`

你可能在 `$HOME` 或 `/` 執行了 `aicode`。切到具體專案:

```bash
cd ~/work/some-firmware-repo
aicode
```

### `[ctx-safety] refuse to start.` 啟動被擋

主模型現在只有一個 `n_ctx`：正常在 `./set_config.sh` 輸入一次，產生 deployment 的 `services.main.ctx` 與 server `-c`。`aicode` 啟動時會讀 server `/props` 的實值,直接傳給客戶端與 MCP server;沒有第二份設定要對齊。

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
AICODE_MODEL=<CODE_MODEL> python3 scripts/ctx_safety_check.py
```

`<CODE_MODEL>` 是佔位符,必須替換成實際模型名稱或 GGUF 路徑。

### 圖片工具超時，接著連小工具也跟著超時

每一次 MCP 呼叫的 read timeout 是**固定的** `config.MCP_CALL_TIMEOUT_SECONDS`(660 秒,
略高於 `ingest_document` 的 10 分鐘內部上限),呼叫端不得放寬也不得調小 —— 調小就等於
ingest 還在寫 `knowledge.json` 時客戶端已經放棄。

超時的處理是完整的取消契約:客戶端送 `notifications/cancelled`、等一個寬限期,還不收手就
`SIGTERM` 該 instance、把**所有**進行中的呼叫回成 error,再重新 spawn 一個。所以「一張圖
超時之後連 `list_dir` 也壞掉」在新客戶端上不會發生——那是舊 client 沒有取消契約時的症狀。

真的看到連鎖失敗時,先確認是不是 VL server 本身沒 ready:

若 timeout 已正確，但圖片回答像是在描述一張不存在的通用終端畫面，跑：

```bash
python3 scripts/required_model_servers_check.py
```

新版 CodeTrail 走 llama.cpp 的 `/v1/chat/completions` `image_url` 多模態格式；舊版
top-level `image_data` 可能被新版 llama.cpp 靜默忽略，造成模型只看提示詞猜圖。

### PDF ingest 說 preflight 超過上限

`ingest_document(path, preflight_only=True)`(或 `python3 RAG.py <pdf> knowledge.json --preflight`)
報告超出上限時,**還沒有呼叫任何 VL、沒有算 embedding、沒有動 `knowledge.json`** —— 零寫入,
不需要善後。報告會指出是哪一項超出:

| 報告欄位 | 上限常數(env 同名加 `AICODE_` 前綴) | 預設 |
|---|---|---|
| `candidates` | `FIGURE_MAX_CANDIDATES_PER_DOC` / `FIGURE_MAX_CANDIDATES_PER_PAGE` | 200 / 12 |
| `tiles` | `FIGURE_MAX_TILES_PER_CANDIDATE` | 8 |
| `vl_calls_max` | `FIGURE_MAX_VL_CALLS_PER_DOC` | 200 |
| `image_tokens_est` | `FIGURE_MAX_IMAGE_TOKENS_PER_DOC` / `FIGURE_MAX_IMAGE_TOKENS_PER_CALL` | 400000 / 4096 |

> 這些欄位涵蓋所有結構化候選，包含純 raster 的分類、雙樣本抽取與 image-token 估算。
> 沒被收成候選的區域不進預算——它們不會被送出去，報告改在「不會進 KB 的頁 / 區域」
> 那一段逐筆列出頁碼、bbox 與原因。

三種處理方式:

1. 把 PDF 拆成較小的檔案分批入庫(通常最省事,也讓失敗範圍變小)。
2. 調高對應上限,例如 `AICODE_FIGURE_MAX_VL_CALLS_PER_DOC=300 aicode`。
   這些是**成本上限**,調高的代價是更慢、更吃資源。它們也會改變實際送進模型的東西
   (image token 上限影響解析度、tile 上限影響怎麼切、candidate 上限影響哪些框被抽),
   甚至可能超出你的 server / model 能吃的範圍 —— **不要假設調高之後結果一定一樣或更好**。
   調完請重跑一次 preflight,並依 `verification_status` 與人工覆核判斷結果。
3. 在終端機直接跑(沒有 MCP 的單次呼叫 timeout):

```bash
python3 RAG.py docs/datasheet.pdf knowledge.json
```

### PDF ingest 失敗說「structured 抽取失敗(truncated)」

`finish_reason="length"` 代表那張圖的結構化輸出**比輸出預算長**,不是模型答錯。
**那一張缺席,其餘照常入庫**(契約:抽壞的不得以半套內容入庫,但不牽連整份文件)。
結果會列出是哪幾張,`review_figures(action="list")` 也看得到(`in_kb=false`)。
仍然整份零寫入的是「剩下的圖也不能信」那幾種:VL 連不上 / 逾時、預算超限、
capability probe 未過、來源檔中途被換掉。

正常情況下你不需要做任何事:第一次抽取用 `AICODE_VL_INGEST_MAX_TOKENS`(預設 2048),
撞頂之後那一次重試會**自動**把預算加大到 VL server 的 context 還放得下的程度
(`n_ctx - 實際 prompt tokens - 128`),上限是 `AICODE_FIGURE_VL_MAX_TOKENS_CEILING`
(預設 8192)。實測 `example1.pdf` p3 的 block diagram:2048 撞頂 → 自動升到 6385 →
只用 2161 就寫完,17 個 component、33 條 relation 全部入庫。

`max_tokens` 是**上限不是目標**,所以放大它不會讓短輸出變貴;天花板存在只是為了擋住
「模型陷入重複、把整個 context 生滿」。

真的看到這個錯誤時,訊息會帶出當時的數字(用了多少 max_tokens、prompt 多長、
server n_ctx 多少)。依序試:

1. 把 **VL server 的 `-c`** 開大 —— 8192 的 context 扣掉一張 1400x900 的圖之後,
   輸出只剩約 6.4k token。這是最常見的真兇。
2. 提高 `AICODE_FIGURE_VL_MAX_TOKENS_CEILING`(只有在 `-c` 已經很大時才會是瓶頸)。
3. 提高 `AICODE_VL_INGEST_MAX_TOKENS`,讓**第一次**就給夠 —— 省掉那次注定撞頂的
   呼叫(一張大圖大約 60 秒)。
4. 調小 `AICODE_FIGURE_MAX_IMAGE_TOKENS_PER_CALL` 讓圖切成多個 tile,每個 tile 的
   輸出自然變短(代價:更多次呼叫,而且跨 tile 要接合)。

> 已經頂到 context 上限時**不會**再送一次相同的請求 —— 那只是多花一分鐘拿到同一個
> `length`。所以這種失敗只會看到一次呼叫。

### ingest 逾時,但看得到中途輸出

MCP 端是逐行讀子行程輸出的,所以逾時時**已經收到的每一行都會保留在回傳裡**(看得到卡在
第幾張圖、第幾頁)。同時子行程連同它的 process group 會被 SIGTERM → SIGKILL 收掉,
而且會**確認收屍**才敢說「已確認終止」。

如果回傳寫的是「**無法確認子行程已終止**」,那就照它附的 `ps` / `kill` 命令自己確認一次 ——
那代表 RAG.py 可能還在背景跑、**仍可能寫入 knowledge.json**,這次呼叫不能當成零寫入。

正常情況下入庫是**原子提交**,逾時中止通常代表零寫入;要確認就呼叫 `reload_knowledge_base`
看 chunk 數。
接下來照回傳裡附的那條命令在終端機跑(它已經幫你把路徑做好 quoting,含空白也能直接複製)。
PDF 的話回傳還會多附一條 `--preflight` 版本,先估成本再決定。

如果回傳寫的是「輸出不完整」而不是逾時,那代表讀取子行程輸出的執行緒出了問題 —— 這種情況
**不會**回報成功,因為手上的輸出不足以判斷入庫結果;一樣改用 CLI 重跑並看 chunk 數。

### 查得到那張表,但嚴格模式拒絕用它回答數值

這是設計行為,不是 bug。`query_knowledge_strict` 在 **code 層**排除未通過驗證的圖片內容
(`needs_review` / `unverified` / `legacy_unverified`),所以它不會用沒被獨立證據佐證的圖片
數值回答 register、bit range 或規格數字。被擋下的那些會出現在回傳的 `excluded_figures`
(帶 source、頁碼、`figure_id`、kind、狀態與原因)與 `review_hint` —— **四條回傳路徑都有**,
所以「全部候選都被擋」時你仍看得到「哪一頁、哪一張圖可用但待覆核」,不會被誤導成「查不到」。

診斷順序:

```text
請用工具 review_figures,action 設 "list",列出待覆核的圖與原因。
```

看 `reasons`:

- `▯` / glyph 衝突 → 該字元在原圖上就分不出來(例如 `8` 與 `B`、`0` 與 `O`)。這種只能人看原圖。
- 缺 row/line、tile 縫合不確定、截斷 → 抽取沒能覆蓋完整,同樣要人工確認。
- `legacy_unverified` → 這張是**舊 KB** 或**純 raster 路徑**的 chunk。舊 KB 的圖片 chunk 在
  載入時一律補成這個狀態(只在記憶體內,不改你的 `knowledge.json`)。

處理方式(先看那一筆有沒有 `figure_id`:有才是 structured、才進得了 `review_figures`):

- **structured figure(有 `figure_id`)** → 可以用 `review_figures(action="fix", ...,
  confirm_against_image=True)` 人工覆核(它會改知識庫,permission 是 `ask`,你會在核准框
  看到完整參數)。
- **原生表格(PDF 裡可以選取文字)** → `remove_document` 之後重新 `ingest_document`,
  它會走結構化 lane,**可能**拿到可信狀態。但「有原生文字」不保證 `native_verified`:
  那需要兩個一致的原生 evidence channel;只有一個通道時是 `unverified`,通道矛盾時是
  `needs_review`。native lane 不呼叫 VL,所以也**不會**產生 `corroborated`。實際結果以
  重 ingest 後 `review_figures(action="list")` 顯示的為準。
- **新版 ingest 的掃描版／拍照版純 raster** → 先分類成 table／terminal／prose／diagram
  並產生 structured figure，所以會出現在 `review_figures`；被判定「不是圖面」（封面、logo、
  照片）的則零抽取、零 chunk，只出現在 ingest 的缺席清單與 review artifact 的
  「判定不是圖面」一節。沒有獨立原生證據時仍是
  `unverified`／`needs_review`，strict 查詢不會採用；只有人對原圖以
  `confirm_against_image=True` 核准指定 revision 後才可能成為 `human_verified`。
- **舊 KB 的 legacy VL chunk（沒有 `figure_id`）** → 沒有 canonical payload 可 fix；要走
  structured review 必須用新版重新 ingest 原始文件。沒有原圖或人工確認時，不得把純
  raster 數字宣稱為 strict-trusted。
- 覆核時如果 `list` 回 `payload: (讀不到)`,代表那份 review artifact 已經被清掉了;
  `fix` 需要 canonical payload,只能重新 ingest 該文件。清除的影響見
  [setup 的清除 PDF review artifacts](setup.md#清除-pdf-review-artifacts可能含-nda)。

**升級注意**:`review_figures` 的人工核准閘寫死在 `client_policy.ASK_TOOLS`,不依賴任何
外部設定檔——舊安裝升級後不需要為了這件事改任何東西。

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

### embedding / reranker 長時間服務後 host RAM 持續成長

CodeTrail 的 safe-defaults 與 `set_config.sh` 產物都會對 embedding、reranker 傳
`--cache-ram 0`。這兩個角色不會生成可重用的 prompt prefix；開著 llama.cpp 預設的
host prompt cache，反而可能讓每筆不同輸入持續佔用 RAM。`cache_ram: 0` 只作用在
embedding / reranker；main 仍保留生成用 prompt cache，VL 不在這個 workaround 範圍。

這是針對 llama.cpp [#26293](https://github.com/ggml-org/llama.cpp/issues/26293) 的
隔離措施；規劃時修復 [#26893](https://github.com/ggml-org/llama.cpp/pull/26893)
仍未合併，另有跨 slot cache 還原正確性的 [#27148](https://github.com/ggml-org/llama.cpp/issues/27148)
需獨立追蹤。因此單純升級 build 不能取代這個非生成服務預設，也不代表應順手關掉
main cache。若 `set_config.sh` 報 build 缺 `--cache-ram`，請更新並重 build llama.cpp；
不要在 generated launcher 手動刪旗標，否則下次重產設定會漂移。

2026-08-19 曾用 build 10276 在隔離 port、CPU、單 slot 各送 40 筆全合成輸入做
A/B。embedding 在第 10→40 筆的 RSS 是 `2699356→2707396 KiB`(`8192`)與
`2699320→2707360 KiB`(`0`)；reranker 是 `2101252→2109412 KiB` 與
`2101212→2109372 KiB`，兩組各自約 `268` / `272 KiB/request` 且輸出逐組一致。
第一筆另有約 0.9 GiB 的一次性 compute allocation。因為無 GPU 且兩條軌跡幾乎
相同，這次本機量測結論是 **inconclusive**，不可拿來宣稱已重現或修復線性 cache
成長；安全預設仍依上游 issue/PR 與 schema/argv 離線測試保留。

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
# 0) 最省事:重跑一鍵設定,registry / deployment / client 設定一次寫齊
cd <CODETRAIL_REPO> && ./set_config.sh

# 1) 環境變數 (最優先)
export AICODE_MODEL=<CODE_MODEL>

# 2) per-run CLI 旗標
aicode -m <CODE_MODEL>

# 3) ~/.config/codetrail/deployment.json 設 profile + services.main.model
```

`<CODE_MODEL>` 是 MODEL_REGISTRY 裡的 bare name 或 GGUF 絕對路徑。如果你看到「placeholder」相關錯誤,通常是值還停留在 `<CODE_MODEL>` 或 `<MODEL>` 沒換掉;看到「外部 provider prefix」錯誤代表你還在用 `ollama/foo` 那種舊寫法,改成 bare name。

`AICODE_MODEL` 與 `-m/--model` 同時存在時,兩者必須指向同一顆模型;名稱不同但 registry 解析到同一個 canonical GGUF 路徑時視為一致,其餘情況 fail-loud。殘留的 `~/.config/opencode/opencode.json` **不再參與解析** —— 它壞掉也不會擋住啟動,裡面的模型也不會被靜默沿用。

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

chunks 大於 0、一般 `query_knowledge` 也查得到,但 `query_knowledge_strict` 就是不用那張表的數字 —— 那不是「沒結果」,見上面[查得到那張表,但嚴格模式拒絕用它回答數值](#查得到那張表但嚴格模式拒絕用它回答數值)。

### `apply_patch(...)` 被拒絕

`apply_patch` 同一次只接受一種格式(SEARCH/REPLACE 或 unified diff),參數已是字串、不要包 Markdown fence;混用、孤立 marker、fence、marker 外的說明文字都會被拒絕。兩種格式共用的拒絕原因:非 UTF-8 檔案、CR-only／mixed newline、目標或路徑上有 symlink、路徑不是 repo-relative POSIX(絕對路徑、Windows drive／UNC、`..`)——這些都是整份 patch 拒絕、零寫入。一次改超過 5 個檔案或單檔 200 行也會被拒。

「驗證不完整」或「驗證未通過」**不是拒絕**:patch 已套用、未回滾,請看回覆裡的 syntax 診斷,或另行呼叫 `run_lint(fix=False)`。

#### SEARCH/REPLACE 被拒絕

- SEARCH 與檔案現況不逐字相同(縮排不同也算)→ 工具不會拿相似位置代套;先 `read_file(...)`
  重讀,從現況逐字重建 SEARCH(錯誤訊息會附最接近位置的檔案現況與第一個差異)。
- SEARCH 在檔案中出現多處 → S/R 沒有行號提示,一律拒絕;多帶幾行讓它唯一。
- 多個區塊互相重疊、或第二個區塊要靠第一個區塊套用後才存在 → 拒絕(全部區塊都對同一份原始檔定位)。
- 空 SEARCH 只能建立不存在的新檔;檔案已存在(含 0 byte)就會被拒。

#### unified diff 被拒絕

先知道什麼**不會**造成拒絕(只適用 unified diff):hunk 的行號/行數錯誤無害——定位靠 context
內容,`@@` 可以完全不帶行號;已套用過的 hunk 會自動跳過(重試安全)。

常見原因:

- 模型讀到的是舊內容,context 行與檔案現況不符 → 先 `read_file(...)`
  重讀目標區段(錯誤訊息會附上最接近位置的期望/實際對照)。
- context 行太少,在檔案中多處出現、無法消歧 → 增加 context 行數,
  或在 `@@` 標大約行號當提示。

把任務拆小,要求模型一次只改一個行為。

### `run_command(...)` 被拒絕

命令不在白名單或含 shell metacharacter。timeout 只接受整數 1..600 秒（server 端上限；client 可能更早截止），不是這個範圍的整數會在執行前被拒絕。請模型改用已允許的最小命令,例如:

```text
請改跑 pytest tests/test_x.py,不要使用 &&、|、; 或 shell script。
```

---
