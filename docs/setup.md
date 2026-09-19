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
`aicode` 本身仍會從目前 PATH 選 `python3` 執行 deployment、endpoint 與 server
preflight。因此若依賴只裝在 venv，每次跑 `set_config.sh` 或 `aicode` 前都要
先 `source <CODETRAIL_REPO>/.venv/bin/activate`。不建議修改 venv 自己的 activate 腳本；
可在自己的 shell 設一個明確 alias / function。重建 venv 後要再跑一次 `set_config.sh`，
更新寫進設定檔的 Python 絕對路徑。

主要套件、語言 grammar、外部工具與檔案系統能力的完整要求見[依賴需求](dependencies.md)。
缺少依賴時必須修復環境後重試。`aicode` 不會改用 `python`；設定工具若無法辨識當前
interpreter，或既有 `deployment.json` 格式損壞，也會直接報錯。

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

每個 server 一個 unit。不要在 unit 重抄模型、GPU 與 tuning 旗標:主模型、
`services.<role>.gpu` 與 `llama_bin` 都在 `~/.config/codetrail/deployment.json`,
直接讓 profile loader `exec` 該 role 就好。範例
`~/.config/systemd/user/codetrail-main.service`:

```ini
[Unit]
Description=CodeTrail main llama-server
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /absolute/path/to/CodeTrail/deployment_profile.py exec main
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

`exec` 是 llama-server 唯一真正被啟動的地方,所以最終環境也在那裡決定:CodeTrail 自己的
四個設定前綴、llama.cpp 的 `LLAMA_ARG_*` 與繼承來的 GPU 選擇一律剝掉,GPU 只由設定檔裡
驗證過的值重新指定。所以**不要**用 `Environment=` 傳 CodeTrail 的設定(主模型、GPU、
llama-server 路徑)—— 寫了不會生效,只會讓下一個人以為設定有兩個來源;要一次性換設定
就加旗標(見下面)。系統層的變數(例如 CUDA 的 `LD_LIBRARY_PATH`)不是 CodeTrail 的
設定,照常設即可,不會被剝掉。

啟用 + 開機自啟:

```bash
systemctl --user daemon-reload
systemctl --user enable --now codetrail-main
systemctl --user status codetrail-main
journalctl --user -u codetrail-main -f    # 看 log
```

embedding / reranker / VL 各複製一份，只把 `exec main` 改成對應 role；所有 unit 讀的是
同一份 `deployment.json`。要一次性換設定就在 `ExecStart` 後面加 loader 旗標(例如
`--profile /absolute/path/experiment.json`、`--main-model <CODE_MODEL>`、
`--llama-bin /absolute/path/to/llama-server`)，與內部 Python launcher 使用同一個 loader。
systemd 不會展開 `<...>` placeholder，啟用前必須換成實值。

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

使用 [A／B 分離部署指南](split-deployment.md)完成設定與驗證：

- A 使用 `model-host`，執行 main、embedding、reranker、VL 四類模型，產生版本化模型身分並匯出 client manifest。
- B 使用 `client`，執行 aicode、MCP、編輯、build、KB 與評測；安裝 Python 依賴及工作所需工具即可，不需要 GPU、GGUF、mmproj、tmux 或 llama-server。
- B 匯入 manifest 時逐一授權四個精確端點；`deployment.json` 指定目的地，owner-only `client.json.model_endpoints` 控制允許連線的範圍。KB 脈絡生成另需明確授權。

直連 IP、port、防火牆 ACL、模型更新與診斷均依該指南操作。只修改 `base_url` 或沿用舊版
`model_remote_ok`，不足以完成 `client` 模式的端點授權及 live 模型身分驗證。
主 n_ctx 每次從 A 的 `/props` 讀取，B 無須另外設定或維護模型檔路徑。

---

## `aicode` wrapper 詳細行為

`aicode` 這支 shell wrapper 刻意只做四件 Python 做不了或做起來很難看的事:

1. 找到 CodeTrail 的 checkout(它自己可能是一個 symlink)
2. 找到 `python3`
3. 拒絕所有使用者參數、
   拒絕沒有終端機的環境(TUI 會接管整個畫面,pipe 過去只會得到控制碼)、
   確認 `textual` 裝了
4. `exec` 唯一的客戶端 `codetrail_chat.py`

其餘全部在**客戶端行程裡**(`client_preflight`),而且結果以參數交給 Engine 與 MCP,
**不經環境**:

1. 沙箱根 = 目前目錄。`/` 與 `$HOME` 一律拒絕,沒有 opt-in
2. 驗證 deployment profile(壞掉不得靜默退回預設值再啟動)
3. 依 deployment mode 解析主模型：local／model-host 使用 `main.model` 與 `models.json`；
   client 核對 A 公開的版本 alias，不讀 B 本機權重。沒有 `-m` 或環境變數覆寫；
   換模型需重新設定，分離部署另須更新 B 的 manifest
4. 讀主 llama-server `/props` 取得真實 `n_ctx`,再跑 ctx capacity gate
   (`requested > server n_ctx` 就拒絕啟動,**沒有逃生口**)
5. 把 active [lessons(行為教訓)](lessons.md) render 進 `.codetrail/lessons.md`,並提示已過
   `review_by` 的待複審清單
6. 對三個 aux server 跑 hard preflight
7. 直接起一次 MCP server 做 `initialize → tools/list → list_dir`,確認完整 21-tool contract
   與唯讀工具派發都正常
8. 用 fresh `codetrail_chat.py run --policy readonly --format json` 驗 active model 真的產生
   completed 的結構化 `list_dir` event;依 model / 客戶端檔案 / system prompt / project 指紋
   快取成功結果 24 小時
9. 記錄壓縮模式診斷(門檻、歷史 reasoning 設定、durable 停用警告、權限覆寫)
10. 啟動 TUI。正常啟動的對話區完全空白，摘要、說明與 WARN 都不放進去；啟動摘要、
    壓縮狀態與所有警告(含第 7、8 項寫到 stderr 的工具健檢警告)保留在 `/status`。
    逐項進度的完整輸出仍留在 TUI 前終端。失敗時不進 TUI，錯誤留在終端

第 7 項每次啟動都實跑，不靠模型自述；第 8 項首次、快取過期或指紋變動才實跑，所以不必每次手動問「列出 21 個工具」。第 8 項實跑（本地推理，通常數十秒起）前會先印出原因與單次上限，執行中每 15 秒回報進度；這些完整記錄留在 TUI 前終端。要強制重測就刪掉 `~/.cache/codetrail/tool-call-canary.v3.json`(沒有略過用的環境變數)。完整 PASS / FAIL 說明見 [troubleshooting](troubleshooting.md#mcp-connected-but-no-tool-call)。

一般 `aicode` 啟動不建立 session，只有 `/new` 或第一則直接送出的問題才建立；
`/session` 選單與 `/session <id>` 接續既有對話。維護用 Python 入口的 `--session` /
`--continue` 仍可明示載入歷史，不會先建一個空白 session。

`set_config.sh` 會從主模型 GGUF 的 chat template 偵測 thinking 控制鍵並寫入
`services.main.thinking_kwarg`。主聊天每次啟動預設 off，偵測確認支援後才能用 `/think on`
開啟；舊設定缺少能力欄位時需重跑設定。`show_reasoning` 仍是 `client.json` 的純顯示設定。

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
python3 scripts/check_status.py --strict

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
**啟動時** 讀，所以執行 `./set_config.sh`、在問答中修改壓縮模式後，必須退出再重開。
沒有這個檔 = 沒有接管：模式退成 `manual`，`/status` 會列出有效設定。三種模式的取捨見
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
