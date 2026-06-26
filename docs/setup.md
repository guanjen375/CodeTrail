# 替代安裝、進階配置與維運

[README §1–§5](../README.md) 已涵蓋 RTX 5090 / Blackwell + Qwen3-235B 走廊的安裝、下載、啟動 server、設定、進 TUI 完整流程。**這份文件不重複那條走廊**,只補充:

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

每個 server 一個 unit。範例 `~/.config/systemd/user/codetrail-main.service`:

```ini
[Unit]
Description=CodeTrail main llama-server
After=network.target

[Service]
Type=simple
ExecStart=/home/%u/llama.cpp/build/bin/llama-server \
  -m /home/%u/models/.../shard-00001-of-00003.gguf \
  --host 0.0.0.0 --port 8080 \
  -c 65536 -ngl 99 --jinja \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --n-cpu-moe 90 --no-mmap
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

embedding / reranker / VL 各複製一份,改 ExecStart 即可。

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

GPU 主機端:四個 server 照 [README §3](../README.md#3-啟動-llama-server用-tmux-跑在背景) 啟動,但有兩點要改:① `--host 0.0.0.0` 必須保留(否則只 listen on `127.0.0.1`,外網連不到);② 主 server 的 `-c` 決定 ctx 上限(本例用 `-c 32768`,不是 §3 的 65536)。CodeTrail 端會自動跟隨遠端 server 的真實 `n_ctx`,你只要再把 opencode `limit.context` 也設成同一個值即可;不一致時 `aicode` 會拒絕啟動。

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

`aicode` 是一個 shell wrapper,啟動 `opencode` 之前做八件事:

1. 把目前目錄設成 `AICODE_ROOT`(沙箱根)
2. 拒絕 `AICODE_ROOT=/` 或 `AICODE_ROOT=$HOME`(可能誤刪 / 誤改大量檔案)
3. 在目前 git root 準備 `.opencode/run-codetrail-mcp`,讓 OpenCode config 裡的 MCP command 能找到 CodeTrail server 入口
4. 用 `scripts/resolve_main_model.py` 解析主模型；若 `AICODE_MODEL` 和 opencode.json 同時存在且沒傳 CLI `-m/--model`,兩者必須指向同一顆
5. 讀主 llama-server `/props` 拿真實 `n_ctx`,在使用者沒手動設時自動 export 成 `AICODE_DYNAMIC_NUM_CTX_MAX`(`scripts/resolve_server_ctx.py`)—— CodeTrail 的 ctx 上限就此自動跟隨 server。接著 `scripts/ctx_safety_check.py` 當容量閘:requested 只要不超過 server `n_ctx` 就放行,只有「使用者手動把它設得比 server 大」才 `exit 2`(prompt 會被截斷)。server 不可連時 graceful 放行只 warn
6. 跑 `scripts/opencode_ctx_check.py`,確認 OpenCode active model 的 `limit.context` 等於 server `-c`(= CodeTrail 已自動跟隨的上限)—— 這是唯一要你手動對齊的數字,避免 TUI 32K compact 但 CodeTrail MCP 以為自己有 64K
7. 啟動 `opencode`,讓子行程繼承同一個沙箱根目錄
8. 把使用者傳入的 `-m / --model` 原樣轉發給 OpenCode;沒傳就讓 OpenCode 自己讀 `opencode.json` 的 `"model"` 欄位

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
tmux kill-session -t codetrail-main
./scripts/stop-rag-servers.sh
```

systemd:`systemctl --user stop codetrail-{main,embed,rerank,vl}`

通用:

```bash
pkill -INT -f "llama-server"
```

### 看 server 狀態

```bash
# 四個 port 都通?
for p in 8080 8081 8082 8083; do
  echo ":$p → $(curl -s -o /dev/null -w '%{http_code}' http://localhost:$p/health)"
done

# 主 server 載入的是哪顆 GGUF、ctx 多少?
curl -s http://localhost:8080/props | python -m json.tool | head -20

# slot 是否在處理請求?
curl -s http://localhost:8080/slots | python -m json.tool

# VRAM 占用
nvidia-smi --query-gpu=memory.used,memory.free,memory.total --format=csv
```

### reload OpenCode / `aicode` 設定

`aicode` 啟動時讀一次 `~/.config/opencode/opencode.json` 與 `~/.config/codetrail/models.json`,**之後改檔不會自動生效**。要套用新設定:

```bash
# 退出 TUI(Ctrl-D 或在 TUI 內輸入 /exit)
# 改設定
# 重新 aicode
```

llama-server 端的 `-c <N>` 也是啟動旗標,改完要重啟 server,不能熱 reload。

---

## 後續

`aicode` 啟動之後的 TUI 操作流程見 [docs/basic-usage.md](basic-usage.md)。RAG / 知識庫 / 程式碼語意搜尋見 [docs/rag.md](rag.md)。
