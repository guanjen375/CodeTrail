# 替代安裝、進階配置與維運

[README Quick Start](../README.md) 的 `./set_config.sh` + `~/start.sh` 已涵蓋主流程
(手動 profile 流程見 README §4)。這份文件只補充:

- README 沒涵蓋的安裝替代路徑(其他 distro、runfile installer、conda env)
- tmux 以外的 process manager(systemd / screen / nohup + disown)
- 多機部署(CodeTrail 跟 GPU server 分開)
- `aicode` wrapper 詳細行為
- 維運常用命令(重啟、reload、kill 所有 server)

---

## 安裝替代路徑

### CUDA Toolkit 用 runfile 安裝(非 Ubuntu / 不能 apt)

[README §1.4](../README.md#14-blackwell-gpu-需要-cuda-toolkit-128-以上) 的 apt 流程只覆蓋 Ubuntu 24.04。其他情境:

- **Ubuntu 22.04**:README 的 CUDA 13.0 apt 流程可把 repo URL 的 `ubuntu2404` 換成 `ubuntu2204`,其餘同版套件步驟相同
- **Ubuntu 20.04**:[CUDA 13.0 的原生 Linux 支援表](https://docs.nvidia.com/cuda/archive/13.0.0/cuda-installation-guide-linux/index.html#system-requirements)已不含 20.04,不能只換成 `ubuntu2004` 後仍照裝 `cuda-toolkit-13-0`。請升級到受支援的 Ubuntu,或改選仍支援 20.04 的封存版本(例如 [CUDA 12.8](https://docs.nvidia.com/cuda/archive/12.8.0/cuda-installation-guide-linux/index.html#system-requirements),也符合 Blackwell 的 12.8 低標),並全程採用該版本對應的 repo、套件名與路徑
- **不能 apt(離線、非 Ubuntu、container 內)**:從 [developer.nvidia.com/cuda-downloads](https://developer.nvidia.com/cuda-downloads) 下載 runfile installer,執行時**取消勾選 Driver**(避免覆蓋現有驅動),只裝 toolkit。安裝完手動 export `PATH` / `LD_LIBRARY_PATH` 指到對應路徑

### CodeTrail Python 依賴用 venv(隔離環境)

如果不想用 `--user` 全域裝套件,可以用 venv:

```bash
cd <CODETRAIL_REPO>
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
pip install "pymupdf4llm==1.28.0"    # 選用:RAG 從 PDF 建知識庫才用;釘本 repo 驗證版
```

`set_config.sh` 會把當下 venv Python 的絕對路徑寫進 deployment profile;不過
`aicode` 本身仍會從目前 PATH 選 `python3` / `python` 執行 deployment、endpoint 與 server
preflight。因此若依賴只裝在 venv，每次跑 `set_config.sh` 或 `aicode` 前都要
先 `source <CODETRAIL_REPO>/.venv/bin/activate`。不建議修改 venv 自己的 activate 腳本；
可在自己的 shell 設一個明確 alias / function。重建 venv 後要再跑一次 `set_config.sh`，
更新寫進設定檔的 Python 絕對路徑。

### `llama.cpp` 不用 GPU(純 CPU)

把 `-DGGML_CUDA=ON` 拿掉:

```bash
cmake -B build -DLLAMA_CURL=OFF
cmake --build build --config Release -j
```

啟動 server 時拿掉 `-ngl 99`。MoE 模型在純 CPU 上速度會很慢,適合純測試流程或極低成本部署。

---

## tmux 以外的 process manager

README 用 tmux 是因為它**最直觀、最不依賴系統服務**。其他選擇:

### systemd unit(永久部署)

每個 server 一個 unit。不要在 unit 重抄模型與 tuning 旗標；直接讓 profile loader
`exec` 該 role。範例 `~/.config/systemd/user/codetrail-main.service`:

```ini
[Unit]
Description=CodeTrail main llama-server
After=network.target

[Service]
Type=simple
Environment=AICODE_MODEL=<CODE_MODEL>
Environment=MAIN_GPU=<MAIN_GPU_UUID_OR_INDEX>
Environment=AUX_GPU=<AUX_GPU_UUID_OR_INDEX>
ExecStart=/usr/bin/python3 /absolute/path/to/CodeTrail/deployment_profile.py exec main --llama-bin /absolute/path/to/llama-server
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

啟用 + 開機自啟:

```bash
systemctl --user daemon-reload
systemctl --user enable --now codetrail-main
systemctl --user status codetrail-main
journalctl --user -u codetrail-main -f    # 看 log
```

embedding / reranker / VL 各複製一份，只把 `exec main` 改成對應 role；所有 unit 要用
同一組 profile/env。systemd 不會展開 `<...>` placeholder，啟用前必須換成實值。

### screen(類 tmux)

```bash
screen -S codetrail
# Ctrl-a c   新視窗
# Ctrl-a n / p  下/上一個視窗
# Ctrl-a d   detach
screen -r codetrail   # reattach
```

### nohup + disown(快速臨時方案)

```bash
nohup ~/llama.cpp/build/bin/llama-server -m ... --port 8080 ... > ~/main.log 2>&1 &
disown
```

`disown` 把 process 從目前 shell job table 脫離,關 terminal 不會送 SIGHUP。優點簡單,缺點要自己 `kill <PID>` 收尾,沒有自動重啟。

---

## 多機部署:CodeTrail 與 GPU 主機分開

CodeTrail repo 跑在你工作機(CPU 即可),llama-server 跑在另一台 GPU 主機。CodeTrail 透過 HTTP 呼叫對方的 8080 / 8081 / 8082 / 8083。

先在 GPU 主機照 [README §3](../README.md)(`./set_config.sh` + `~/start.sh`)建立四個
server。主 server 的 `-c` 決定主 n_ctx(`set_config.sh` 沒有預設值,由你輸入)；
CodeTrail 會讀 server 實值，`aicode` 也會把它傳給客戶端當 context gate 與壓縮門檻的
依據。連線方式選下面其中一種，不要混用。

### 路徑 A:可信 VPN / 內網直連

GPU 主機必須明確開放監聽：執行 `./set_config.sh --allow-remote`，或在 deployment.json
各 service 設 `"bind": "all-interfaces"`；完整設定見
[deployment-profiles.md](deployment-profiles.md)。

工作機端**改設定檔,不是環境變數** —— 客戶端與 MCP 不從環境取任何設定。
在 `~/.config/codetrail/deployment.json` 把四個 `base_url` 指到 GPU 主機:

```json
{
  "schema_version": 1,
  "profile": "defaults",
  "services": {
    "main":      {"base_url": "http://<GPU_HOST>:8080", "port": 8080, "model": "<CODE_MODEL>"},
    "embedding": {"base_url": "http://<GPU_HOST>:8081", "port": 8081},
    "reranker":  {"base_url": "http://<GPU_HOST>:8082", "port": 8082},
    "vl":        {"base_url": "http://<GPU_HOST>:8083", "port": 8083}
  }
}
```

再在 `~/.config/codetrail/client.json` 明確同意把 prompt 送出這台機器:

```json
{ "schema": 1, "model_remote_ok": true }
```

然後 `cd <PROJECT> && aicode`。沒有那個同意鍵時每一個模型呼叫都會 fail-loud ——
prompt 可能含 NDA 內容,填一個遠端 IP 不等於同意外送。

(不用另設 ctx max —— `aicode` 會讀 `deployment.json` 的 `main.base_url` 指到的遠端 server `/props`,把主 `n_ctx` 以 argv 交給客戶端與 MCP server。)

遠端 endpoint **只**由 deployment profile 的 `main.base_url` 決定;`n_ctx` 在每次 `aicode` 啟動時從該 server 的 `/props` 讀,不需要另外抄一份。

`client.json` 的 `"model_remote_ok": true` 是必要的明確同意：沒有它，CodeTrail 對非 loopback endpoint 的
health / props / completion / embedding / reranking 呼叫都會 fail-loud。這個 opt-in 不會提供
加密或認證，只表示你接受 prompt / retrieved content 送到該 endpoint。

**安全提醒**:CodeTrail 產生的 llama-server 指令未啟用認證，等於任何能連到
GPU 主機 8080–8083 的人都能使用模型。上游雖有 `--api-key` 與 TLS 選項，
CodeTrail 目前的 profile 與內部 HTTP client 並未支援傳遞這些 credential，不要只在
server 端手動加 key 後就假設四條 CodeTrail 呼叫路徑仍可用。**只能指向可信
內網 / VPN 主機**，不要暴露公網。Profile URL 也不接受內嵌 credentials。

### 路徑 B:SSH tunnel(建議)

GPU 主機保持預設 loopback 綁定，**不要**加 `--allow-remote`。在工作機建立 tunnel；
這裡刻意用 18080–18083 當本機埠，避免撞到本機既有 server:

```bash
ssh -N \
  -L 18080:127.0.0.1:8080 \
  -L 18081:127.0.0.1:8081 \
  -L 18082:127.0.0.1:8082 \
  -L 18083:127.0.0.1:8083 \
  user@<GPU_HOST>
```

保持 tunnel terminal 開著，另開一個 terminal 啟動:

同樣改 `~/.config/codetrail/deployment.json`(tunnel 的本機 port),然後 `aicode`:

```json
{
  "schema_version": 1,
  "profile": "defaults",
  "services": {
    "main":      {"base_url": "http://127.0.0.1:18080", "port": 18080, "model": "<CODE_MODEL>"},
    "embedding": {"base_url": "http://127.0.0.1:18081", "port": 18081},
    "reranker":  {"base_url": "http://127.0.0.1:18082", "port": 18082},
    "vl":        {"base_url": "http://127.0.0.1:18083", "port": 18083}
  }
}
```

端點是 loopback(tunnel 這一端),所以不需要 `model_remote_ok`。

同時把 deployment profile 的 `main.base_url` 設為 `http://127.0.0.1:18080`。有效 endpoint
仍是工作機 loopback，所以這條路徑不需要在 client.json 開 `model_remote_ok`；prompt 與 retrieved
content 會經 SSH 加密隧道送到 GPU 主機。

---

## `aicode` wrapper 詳細行為

`aicode` 這支 shell wrapper 刻意只做四件 Python 做不了或做起來很難看的事:

1. 找到 CodeTrail 的 checkout(它自己可能是一個 symlink)
2. 找到 `python3`
3. 檢查 argv(只接受 `-c` / `--continue`、`--session <id>`、`-h` / `--help`)、
   拒絕沒有終端機的環境(TUI 會接管整個畫面,pipe 過去只會得到控制碼)、
   確認 `textual` 裝了
4. `exec` 唯一的客戶端 `codetrail_chat.py`

其餘全部在**客戶端行程裡**(`client_preflight`),而且結果以參數交給 Engine 與 MCP,
**不經環境**:

1. 沙箱根 = 目前目錄。`/` 與 `$HOME` 一律拒絕,沒有 opt-in
2. 驗證 deployment profile(壞掉不得靜默退回預設值再啟動)
3. 解析主模型:只從 `deployment.json` 的 `main.model` 與 `models.json`。
   沒有 `-m`、沒有環境變數 —— 換模型 = 重跑 `./set_config.sh`
4. 讀主 llama-server `/props` 取得真實 `n_ctx`,再跑 ctx capacity gate
   (`requested > server n_ctx` 就拒絕啟動,**沒有逃生口**)
5. 把 active [lessons(行為教訓)](lessons.md) render 進 `.codetrail/lessons.md`,並提示已過
   `review_by` 的待複審清單
6. 對三個 aux server 跑 hard preflight
7. 直接起一次 MCP server 做 `initialize → tools/list → list_dir`,確認完整 19-tool contract
   與唯讀工具派發都正常
8. 用 fresh `codetrail_chat.py run --policy readonly --format json` 驗 active model 真的產生
   completed 的結構化 `list_dir` event;依 model / 客戶端檔案 / system prompt / project 指紋
   快取成功結果 24 小時
9. 壓縮模式狀態行(門檻、reasoning 開關、durable 停用警告、權限覆寫)
10. 啟動 TUI。上面每一行都留在對話區第一則,所以清屏之後仍然看得到

第 7 項每次啟動都實跑，不靠模型自述；第 8 項首次、快取過期或指紋變動才實跑，所以不必每次手動問「列出 19 個工具」。第 8 項實跑（本地推理，通常數十秒起）前會先印出原因與單次上限，執行中每 15 秒回報進度——不是當機。要強制重測就刪掉 `~/.cache/codetrail/tool-call-canary.v3.json`(沒有略過用的環境變數)。完整 PASS / FAIL 說明見 [troubleshooting](troubleshooting.md#mcp-connected-but-no-tool-call)。

---

## 維運常用命令

### 清除 PDF review artifacts(可能含 NDA)

PDF 結構化圖片抽取會在專案內留下覆核用的檔案:

```
<專案>/.codetrail/figures/<document_slug>/<run_id>/
├── manifest.json          canonical manifest
├── assets/                原始 asset(從 PDF 抽出來的原圖)
├── variants/              **實際送給模型的**每一張圖(crop / tile)
├── review_assets/         **只為覆核 render、從未送給模型**的圖
├── review.md              給人看的摘要
└── revisions/<n>/         人工 fix 後的 canonical payload
```

原圖就是規格書的一塊,**可能含 NDA 內容**。這個 repo 的 `.gitignore` 已含 `.codetrail/`;
在別的 target project 用 CodeTrail 時,請在那個 project 的 `.gitignore` 也補上同一行。
`variants/` 與 `review_assets/` 的差別很重要:前者是**實際送給模型的**圖,後者只是為了
讓人覆核而 render、**從未送給模型**;`review_figures(action="list")` 每一張都會標
`crop_is_model_input`,只有標「模型輸入」的才是模型真的看過的那張。

`FIGURE_REVIEW_MAX_RUNS_PER_DOC`(預設 5,repo 常數)是 **soft retention target,不是硬上限**:被 KB `evidence_ref` 引用、`created_at`
判讀不出來、或清理失敗的 run 一律 fail-closed 保留,實際份數可能超過 5。
**不要拿它當「機敏影像最多留幾份」的保證** —— 要確定清掉就照下面顯式刪除並自己確認結果。

KB 的文件身分是 basename,artifacts 的身分是含路徑 hash 的 `document_id`。
不同目錄下的同名 PDF 現在會在**入庫時**就 fail-loud(列出兩邊完整路徑、零寫入),
所以不會再因為靜默覆蓋而產生孤兒 run 目錄。
**升級前的舊 KB 可能已經有**:那時候的覆蓋沒有留下紀錄,只能靠 run 數回收或下面的
顯式刪除清掉。

手動清除就是刪掉整個目錄:

```bash
rm -rf <專案>/.codetrail/figures/<document_slug>
```

**清掉之後的兩個後果要分清楚**:

- **查詢完全不受影響**。KB(`knowledge.json`,向量是它衍生出來的 cache)是 revision 的
  唯一真相,已入庫的 chunk 與向量都還在。
- **覆核能力會壞掉一半**。`review_figures(action="list")` 對那幾張會降級成 `payload: (讀不到)`,
  而沒有 canonical payload 就**無法做 `fix`**。要恢復,只能 `remove_document` 之後重新
  `ingest_document` 那份 PDF。

`remove_document`(從 KB 移除文件)與清除 artifacts 是**兩件獨立的事**:前者讓查詢查不到,
後者讓覆核做不了。要徹底清掉一份 NDA 文件的痕跡,兩邊都要做。

### 重啟單一 server(換模型 / 換 ctx / 加旗標)

```bash
# 1. 找出 PID
pgrep -fa "llama-server.*--port 8080"

# 2. 終止(送 SIGINT,讓它優雅關掉)
pkill -INT -f "llama-server.*--port 8080"

# 3. 等個 2-3 秒讓 KV cache / prompt cache flush
sleep 3

# 4. 用新參數重啟(在 tmux session 內貼新指令,或 systemd 直接 restart)
```

systemd 版本:

```bash
systemctl --user restart codetrail-main
```

### 全部停掉

tmux:

```bash
~/start.sh stop
```

systemd:`systemctl --user stop codetrail-{main,embed,rerank,vl}`

### 看 server 狀態

```bash
~/start.sh status --strict

# 主 server 載入的是哪顆 GGUF、ctx 多少?
curl -s http://localhost:8080/props | python3 -m json.tool | head -20

# slot 是否在處理請求?
curl -s http://localhost:8080/slots | python3 -m json.tool

# VRAM 占用
nvidia-smi --query-gpu=memory.used,memory.free,memory.total --format=csv
```

### reload `aicode` 設定

`aicode` 啟動時讀一次 `~/.config/codetrail/deployment.json`、`models.json` 與
`client.json`,**之後改檔不會自動生效**。要大改配置(換模型 / 換 GPU / 換 ctx)最省事的是
重跑 `<CODETRAIL_REPO>/set_config.sh`(會重生成全部設定並備份舊檔)。手動改的話,要套用
新設定:

```bash
# 退出 TUI(Ctrl-D 或在 TUI 內輸入 /exit)
# 改設定
# 重新 aicode
```

llama-server 端的 `-c <N>` 也是啟動旗標,改完要重啟 server,不能熱 reload。

**壓縮接管仍是實驗功能**(🧪 開發中、仍在測試階段):`codetrail` / `manual` 的摘要規則、
觸發門檻與受管值都可能再變。

**壓縮模式同理**:`~/.config/codetrail/client.json`(0600)的 `compaction_mode` 在客戶端
**啟動時** 讀,所以 `./set_config.sh --compaction-mode ...` 之後必須退出再重開。
沒有這個檔 = 沒有接管:模式退成 `manual`,啟動橫幅會講明。三種模式的取捨見
[compaction-rules.md](compaction-rules.md)。

想加一份跨專案通用的自訂規則,寫進 `~/.config/codetrail/instructions.md` —— 客戶端每一輪
都會把它接在內建基底規則後面。專案層級的規則放專案根目錄的 `AGENTS.md`(不信任的 repo
在 `client.json` 設 `"project_instructions": false` 完全不讀它)。

有待人工覆核的匯入(`ingest_document` 回傳 `[CODETRAIL_ACTION_REQUIRED]`)、以及「模型說要
呼叫工具卻沒真的呼叫」時,客戶端會在該輪結束後直接顯示提醒;headless `run` 收到的是同一個
`notice` 事件。行為與 incident 檔見 [troubleshooting](troubleshooting.md)。

---

## 後續

`aicode` 啟動之後的 TUI 操作流程見 [docs/basic-usage.md](basic-usage.md)。RAG / 知識庫見
[docs/rag.md](rag.md)；Code-RAG / graph 見
[MCP 工具清單](mcp-tools.md#code_rag_search-四種模式)。
