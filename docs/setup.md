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

- **其他 Ubuntu 版本(22.04 / 20.04)**:apt repo URL 把 `ubuntu2404` 換成 `ubuntu2204` / `ubuntu2004`,其餘相同
- **不能 apt(離線、非 Ubuntu、container 內)**:從 [developer.nvidia.com/cuda-downloads](https://developer.nvidia.com/cuda-downloads) 下載 runfile installer,執行時**取消勾選 Driver**(避免覆蓋現有驅動),只裝 toolkit。安裝完手動 export `PATH` / `LD_LIBRARY_PATH` 指到對應路徑

### CodeTrail Python 依賴用 venv(隔離環境)

如果不想用 `--user` 全域裝套件,可以用 venv:

```bash
cd <CODETRAIL_REPO>
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
pip install "pymupdf4llm==1.28.0"    # 選用:RAG 從 PDF 建知識庫才用;釘本 repo 驗證版
```

`set_config.sh` 會把當下 venv Python 的絕對路徑寫進 OpenCode 的 MCP command；不過
`aicode` 本身仍會從目前 PATH 選 `python3` / `python` 執行 deployment、endpoint 與 server
preflight。因此若依賴只裝在 venv，每次跑 `set_config.sh`、`aicode` 或 `aicode_web` 前都要
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
CodeTrail 會讀 server 實值，`aicode` 也會同步 OpenCode active model 的
`limit.context`。連線方式選下面其中一種，不要混用。

### 路徑 A:可信 VPN / 內網直連

GPU 主機必須明確開放監聽：執行 `./set_config.sh --allow-remote`，或在 profile 設
`AICODE_BIND=all-interfaces`；完整設定見
[deployment-profiles.md](deployment-profiles.md)。工作機端:

```bash
AICODE_LLAMA_BASE_URL=http://<GPU_HOST>:8080 \
AICODE_LLAMA_EMBED_BASE_URL=http://<GPU_HOST>:8081 \
AICODE_LLAMA_RERANK_BASE_URL=http://<GPU_HOST>:8082 \
AICODE_LLAMA_VL_BASE_URL=http://<GPU_HOST>:8083 \
AICODE_MODEL_REMOTE_OK=1 \
AICODE_MODEL=<CODE_MODEL> \
aicode
```

(不用另設 ctx max —— `aicode` 會讀 `AICODE_LLAMA_BASE_URL` 指到的遠端 server `/props`，把主 `n_ctx` 傳給 CodeTrail 與 OpenCode。)

把 `~/.config/opencode/opencode.json` 的 provider `baseURL` 改成 `http://<GPU_HOST>:8080/v1`；active model 的 `limit.context` 會在第一次 `aicode` 啟動時同步(上例是 32768，原檔會備份)。

`AICODE_MODEL_REMOTE_OK=1` 是必要的明確同意：沒有它，CodeTrail 對非 loopback endpoint 的
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

```bash
AICODE_LLAMA_BASE_URL=http://127.0.0.1:18080 \
AICODE_LLAMA_EMBED_BASE_URL=http://127.0.0.1:18081 \
AICODE_LLAMA_RERANK_BASE_URL=http://127.0.0.1:18082 \
AICODE_LLAMA_VL_BASE_URL=http://127.0.0.1:18083 \
AICODE_MODEL=<CODE_MODEL> \
aicode
```

同時把 OpenCode provider `baseURL` 設為 `http://127.0.0.1:18080/v1`。有效 endpoint
仍是工作機 loopback，所以這條路徑不需 `AICODE_MODEL_REMOTE_OK`；prompt 與 retrieved
content 會經 SSH 加密隧道送到 GPU 主機。

---

## `aicode` wrapper 詳細行為

`aicode` 是一個 shell wrapper,啟動 `opencode` 之前做十四件事:

1. 把目前目錄設成 `AICODE_ROOT`(沙箱根)
2. 拒絕 `AICODE_ROOT=/` 或 `AICODE_ROOT=$HOME`(可能誤刪 / 誤改大量檔案)
3. 在目前 git root 準備 `.opencode/run-codetrail-mcp`,讓 OpenCode config 裡的 MCP command 能找到 CodeTrail server 入口
4. 驗證 deployment profile，安全匯入四個 base URL 與 aux model ID；不 `source` / `eval` JSON
5. 用 `scripts/resolve_main_model.py` 解析主模型；若 `AICODE_MODEL` 和 opencode.json 同時存在且沒傳 CLI `-m/--model`,兩者必須一致（不同 registry alias 解析到同一個 GGUF 也視為一致）
6. 讀主 llama-server `/props` 取得真實 `n_ctx`，再跑 ctx capacity gate
7. 安全同步 OpenCode active model 的 `limit.context` 為主 n_ctx(原子寫入 + 備份)
8. 安全同步既有 `mcp.codetrail.timeout`
9. 安全補齊既有 OpenCode config 缺少的 permission ask 覆寫與 lessons
   `instructions` contract；只新增缺鍵、保留使用者值，原檔備份
10. 把 active [lessons(行為教訓)](lessons.md) render 進 `.codetrail/lessons.md` 供 OpenCode 注入,並提示已過 `review_by` 的待複審清單
11. 對三個 aux server 跑 hard preflight
12. 直接對實際 `mcp.codetrail.command` 做 `initialize → tools/list → list_dir`,確認完整 18-tool contract 與只讀工具派發都正常
13. 用 fresh `opencode run --format json` 驗 active model 真的產生 completed 的結構化 `codetrail_list_dir` event；依 model/config/chat-template/project 指紋快取成功結果 24 小時
14. 啟動 `opencode` 並原樣轉發使用者的 `-m / --model`

第 12 項每次啟動都實跑，不靠模型自述；第 13 項首次、快取過期或指紋變動才實跑，所以不必每次手動問「列出 18 個工具」。第 13 項實跑（本地推理，通常數十秒起）前會先印出原因與單次上限，執行中每 15 秒回報進度——不是當機。`aicode web` 與委派它的 `aicode_web` 也會跑兩層檢查；`aicode attach` 是接既有 backend 的薄 client，不重跑。完整 PASS / FAIL、快取與緊急 override 說明見 [troubleshooting](troubleshooting.md#mcp-connected-but-no-tool-call)。

---

## 維運常用命令

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
curl -s http://localhost:8080/props | python -m json.tool | head -20

# slot 是否在處理請求?
curl -s http://localhost:8080/slots | python -m json.tool

# VRAM 占用
nvidia-smi --query-gpu=memory.used,memory.free,memory.total --format=csv
```

### reload OpenCode / `aicode` 設定

`aicode` 啟動時讀一次 `~/.config/opencode/opencode.json` 與 `~/.config/codetrail/models.json`,**之後改檔不會自動生效**。要大改配置(換模型 / 換 GPU / 換 ctx)最省事的是重跑 `<CODETRAIL_REPO>/set_config.sh`(會重生成全部設定並備份舊檔)。手動改的話,要套用新設定:

```bash
# 退出 TUI(Ctrl-D 或在 TUI 內輸入 /exit)
# 改設定
# 重新 aicode
```

llama-server 端的 `-c <N>` 也是啟動旗標,改完要重啟 server,不能熱 reload。

---

## 後續

`aicode` 啟動之後的 TUI 操作流程見 [docs/basic-usage.md](basic-usage.md)。RAG / 知識庫見
[docs/rag.md](rag.md)；Code-RAG / graph 見
[MCP 工具清單](mcp-tools.md#code_rag_search-四種模式)。
