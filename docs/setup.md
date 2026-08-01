# 替代安裝、進階配置與維運

[README Quick Start](../README.md) 的 `./set_config.sh` + `~/start.sh` 已涵蓋主流程
(手動 profile 流程見 README §4);RTX 5090 實測另見
[verified-reference-5090.md](verified-reference-5090.md)。這份文件只補充:

- README 沒涵蓋的安裝替代路徑(其他 distro、runfile installer、conda env)
- tmux 以外的 process manager(systemd / screen / nohup + disown)
- 多機部署(CodeTrail 跟 GPU server 分開)
- `aicode` wrapper 詳細行為
- 維運常用命令(重啟、reload、kill 所有 server)

---

## 安裝替代路徑

### CUDA Toolkit 用 runfile 安裝(非 Ubuntu / 不能 apt)

[README §1.4](../README.md#14-僅-blackwell-gpu-需要升級-cuda-toolkit-到-13) 的 apt 流程只覆蓋 Ubuntu 24.04。其他情境:

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
pip install pymupdf4llm    # 選用:RAG 從 PDF 建知識庫才用
```

注意 `aicode` 啟動時會以 `python3` 跑 `scripts/doctor.py` 等,需要對應 venv 已啟用。建議在 `.venv/bin/activate` 內或 `~/.bashrc` 裡加上 `source <CODETRAIL_REPO>/.venv/bin/activate`,避免不同 shell 撞不到 venv。

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
Environment=AICODE_PROFILE=maintainer-target
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

GPU 主機端:四個 server 照 [README §3](../README.md)(`./set_config.sh` + `~/start.sh`)啟動,但有兩點要改:① 預設只綁 `127.0.0.1`,遠端連線要明確開放 —— `./set_config.sh --allow-remote`(或 `AICODE_BIND=all-interfaces`,詳見 [deployment-profiles.md](deployment-profiles.md));② 主 server 的 `-c` 決定 ctx 上限(本例用 `-c 32768`,不是預設的 65536)。CodeTrail 端會自動跟隨遠端 server 的真實 `n_ctx`,你只要再把 opencode `limit.context` 也設成同一個值即可;不一致時 `aicode` 會拒絕啟動。

CodeTrail 端:

```bash
AICODE_LLAMA_BASE_URL=http://<GPU_HOST>:8080 \
AICODE_LLAMA_EMBED_BASE_URL=http://<GPU_HOST>:8081 \
AICODE_LLAMA_RERANK_BASE_URL=http://<GPU_HOST>:8082 \
AICODE_LLAMA_VL_BASE_URL=http://<GPU_HOST>:8083 \
AICODE_MODEL=<CODE_MODEL> \
aicode
```

(不用設 `AICODE_DYNAMIC_NUM_CTX_MAX` —— `aicode` 會讀 `AICODE_LLAMA_BASE_URL` 指到的遠端 server `/props`,自動把 CodeTrail 的 ctx 上限對齊成它的 `n_ctx`。)

同時把 `~/.config/opencode/opencode.json` 的 provider `baseURL` 改成 `http://<GPU_HOST>:8080/v1`,並把 active model 的 `limit.context` 設成同一個值(上例是 32768)。

**安全提醒**:llama-server 預設不檢查 API key,等於任何能連到 GPU 主機 8080 的人都能用你的模型。**只能指向可信內網 / VPN 主機**,不要把 8080 暴露公網。需要鎖住的話加反向代理(nginx / caddy)做 basic auth,或用 SSH tunnel:

```bash
ssh -L 8080:localhost:8080 -L 8081:localhost:8081 -L 8082:localhost:8082 -L 8083:localhost:8083 \
    user@<GPU_HOST>
# 然後本機 AICODE_LLAMA_*_BASE_URL 全用 http://localhost:80xx
```

---

## `aicode` wrapper 詳細行為

`aicode` 是一個 shell wrapper,啟動 `opencode` 之前做十件事:

1. 把目前目錄設成 `AICODE_ROOT`(沙箱根)
2. 拒絕 `AICODE_ROOT=/` 或 `AICODE_ROOT=$HOME`(可能誤刪 / 誤改大量檔案)
3. 在目前 git root 準備 `.opencode/run-codetrail-mcp`,讓 OpenCode config 裡的 MCP command 能找到 CodeTrail server 入口
4. 驗證 deployment profile，安全匯入四個 base URL 與 aux model ID；不 `source` / `eval` JSON
5. 用 `scripts/resolve_main_model.py` 解析主模型；若 `AICODE_MODEL` 和 opencode.json 同時存在且沒傳 CLI `-m/--model`,兩者必須一致（不同 registry alias 解析到同一個 GGUF 也視為一致）
6. 讀主 llama-server `/props` 取得真實 `n_ctx`，再跑 ctx capacity gate
7. 確認 OpenCode active model 的 `limit.context` 等於 server `-c`
8. 安全同步既有 `mcp.codetrail.timeout`
9. 對三個 aux server 跑 hard preflight
10. 啟動 `opencode` 並原樣轉發使用者的 `-m / --model`

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
./scripts/quit.sh
```

systemd:`systemctl --user stop codetrail-{main,embed,rerank,vl}`

### 看 server 狀態

```bash
./scripts/check-status.sh --strict

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

`aicode` 啟動之後的 TUI 操作流程見 [docs/basic-usage.md](basic-usage.md)。RAG / 知識庫 / 程式碼語意搜尋見 [docs/rag.md](rag.md)。
