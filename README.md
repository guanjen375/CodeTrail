# CodeTrail - OpenCode / Codex CLI + llama.cpp 本地 MCP 工作台

CodeTrail 是一個給 OpenCode 與 Codex CLI 使用的本地 MCP 後端。你在任一 frontend TUI 裡提問,模型可以透過 CodeTrail 讀專案、找程式碼、查已匯入的 spec、分析截圖或 binary、產生 patch,並在允許的白名單內跑驗證命令。

主線使用方式:
- OpenCode TUI: `aicode`(README 的主流程以這條為準)
- 選用/測試: Codex CLI `aicodex`
- 選用/測試: OpenCode web via `aicode web`

CodeTrail 目前定位是**成熟私有部署版**:適合本機、離線、NDA / firmware / private repo 分析;**不打算公開發布**成 PyPI package、Docker image 或 SaaS。安全邊界有自動測試保護,但未做公開產品級安全審計。

底層推理引擎使用 [llama.cpp](https://github.com/ggerganov/llama.cpp) `llama-server`(自己 build,需要 CUDA)。所有 CodeTrail internal LLM / embedding / reranker / VL 走它的 HTTP API。Codex CLI frontend model 可以另外使用你自己的 Codex / OpenAI / ChatGPT / local provider 設定。

## 🚀 Quick Start(7 步設定完成)

前提:§1 的依賴(含 build llama.cpp)與 §2 的四類 GGUF 模型(主聊天 / embedding / reranker / VL+mmproj,放在 `~/models`)都已完成。之後只要:

```bash
cd <CODETRAIL_REPO>                          # 1. 進 CodeTrail repo
chmod +x ./aicode                            # 2. 讓啟動指令可執行
mkdir -p "$HOME/.local/bin"                  # 3. 準備使用者 bin 目錄
ln -sfn "$PWD/aicode" "$HOME/.local/bin/aicode"   # 4. 建立 aicode symlink
command -v aicode                            # 5. 應顯示 ~/.local/bin/aicode
./set_config.sh                              # 6. 一鍵設定(偵測 GPU/模型 → 互動選擇 → 產生所有設定檔)
~/start.sh                                   # 7. 啟動四個 llama-server(tmux 背景)
```

然後就可以到任何要分析的專案直接用:

```bash
cd <PROJECT_TO_ANALYZE>
aicode        # OpenCode TUI;/status 應顯示 codetrail Connected
```

- 第 5 步沒輸出,代表 `~/.local/bin` 不在 PATH:`echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc` 後再試。
- `./set_config.sh` 預設只要**確認一頁建議配置**(Enter 採用);做的事與產物見 §3。要改配置(換模型 / 換 GPU / 換 ctx)隨時重跑它,舊設定自動備份、可用 `--restore-last-backup` 還原。
- 安全預設:四個模型 server **只綁 `127.0.0.1`**(僅本機可連);要讓區網其他機器使用要明確 `./set_config.sh --allow-remote`(見 [docs/security.md](docs/security.md))。
- 管理指令都掛在 `~/start.sh` 上:`~/start.sh status`(檢查四個 server)、`~/start.sh stop`(全部關閉,= `scripts/quit.sh`,關掉主模型 + 三顆附屬模型的所有 tmux 視窗)、`~/start.sh logs [role]`(看啟動失敗保存的 log)。
- 重新啟動前要先 `~/start.sh stop`,tmux session 還在時 `~/start.sh` 會拒絕重複啟動;若是啟動中途失敗,launcher 會自動清理本次啟動的服務,修正後直接重跑即可。

## 0. OpenCode TUI 部署路線圖

如果你的目標只是把 **OpenCode TUI** 布置起來,先照這條走。Codex CLI 與 web 模式都可以先跳過,等 TUI 穩了再看。

先分清楚兩個路徑:

- `<CODETRAIL_REPO>`:這個 repo,例如 `~/CodeTrail`。安裝 Python 依賴、跑 `set_config.sh`、建立 `aicode` symlink 都在這裡做。
- `<PROJECT_TO_ANALYZE>`:你要分析的 firmware / NDA / private repo。最後啟動 TUI 時才 `cd` 到這裡跑 `aicode`。

OpenCode TUI 的完成條件是:

1. `opencode` 在 PATH 上:§1.2.1。
2. `python3` 能 import CodeTrail 依賴(`mcp` / `numpy` / `requests`):§1.3;`set_config.sh` 會偵測並把正確的 Python 路徑寫進設定。
3. `llama-server` 已 build 完:§1.5。
4. 主模型、embedding、reranker、VL GGUF 都已下載:§2.2–§2.4。
5. `aicode` 已安裝到 PATH、`./set_config.sh` 跑完:Quick Start 步驟 2–6(產物說明見 §3、§4)。
6. `~/start.sh` 啟動後四個 server 都是 `status=ok`:§3.2–§3.3。
7. 在 `<CODETRAIL_REPO>` 跑 `AICODE_MODEL=<CODE_MODEL> python scripts/doctor.py` 結尾是 `FAIL=0`:§5.1。
8. 在 `<PROJECT_TO_ANALYZE>` 跑 `aicode`,TUI 內 `/status` 看到 `codetrail Connected`:§5.2–§5.3。

README 的命令範例以 Ubuntu / Debian shell 為主。`aicode` 是 bash wrapper;Windows 使用者建議在 WSL2 或遠端 Linux GPU 主機上跑這條流程。

## 特別注意(首次部署最容易踩的)

> [!WARNING]
> 動手前掃一遍 —— 這幾點踩了通常會卡很久,或踩到 NDA / 安全:
>
> 1. **CodeTrail MCP server 跑在 `set_config.sh` 當下偵測到的那顆 Python 上**(路徑會寫死進 `~/.config/opencode/opencode.json`),所以新開 shell 不必 activate 任何環境。如果你之後換了 Python 環境(重建 venv、升級系統 Python),**重跑一次 `./set_config.sh`** 讓它重新偵測。
> 2. **四個 llama-server 都要起**:main `8080` + embedding `8081` + reranker `8082` + VL `8083`。三顆副模型是硬性需求,缺一個啟動前 preflight 就擋下;reranker 預設不降級。見 §3。
> 3. **不要從 `$HOME` 或 `/` 啟動** —— 沙箱會直接拒絕。先 `cd` 進你要分析的**具體專案目錄**再跑。
> 4. **換模型就重跑 `./set_config.sh` + 重啟 server**:TUI 按 `/models` 只切 OpenCode 的 model id,**不會 reload llama-server、也不會通知 CodeTrail MCP**。`set_config.sh` 會一次對齊 registry、deployment、opencode.json 的 `model` / `limit.context` 與啟動參數,之後 `scripts/quit.sh` → `~/start.sh` 重啟即可(ctx 上限 CodeTrail 會自動跟隨 server,不用手動調)。
> 5. **CodeTrail 沙箱鎖在「你啟動的那個資料夾」(`AICODE_ROOT`)** —— 綁在 process 上,**不會跟著你在 UI 切資料夾或切對話而移動**。web UI 那顆「切換資料夾」按鈕對 CodeTrail 無效(切過去還是只讀啟動目錄)。換專案 = 到那個目錄重新啟動一個(TUI 重開 `aicode`;web 另起一個 backend)。
> 6. **web 模式目前是實驗性的(開發中)** —— 穩定、proven 的主力是 standalone TUI(`aicode` / `aicodex`);web 用來瀏覽器續問歷史 session,行為可能還會變。要可靠就用 TUI。
> 7. **CodeTrail 沙箱只蓋它那 17 個 MCP 工具** —— OpenCode 內建的 `bash` / `read` / `write` 不走這層,所以範本把它們全 `deny`,**別放寬那份 permission**。分析不信任 repo 時,連被分析 repo 自帶的 `opencode.json` 都可能翻掉你的鎖定(防法:`OPENCODE_DISABLE_PROJECT_CONFIG=1 aicode`,見 [docs/security.md](docs/security.md))。
> 8. **首次 MoE 對話首字會慢(可能 1–2 分鐘),別按 Esc** —— 它在 page-in expert weights,不是當掉;slot / GPU 在動就是正常。
> 9. **NDA / 衍生資料不要 commit**:`knowledge.json`、`*.jsonl`、`.codetrail/`、`data/`、`.aicode_uploads/` 等已在 `.gitignore`,commit 前自己 `git diff` 看一眼。
> 10. **任一步 FAIL 對應的修法見 [docs/troubleshooting.md](docs/troubleshooting.md)。**

---

## 1. 安裝依賴

### 1.1 系統工具

Ubuntu / Debian 乾淨機器一行裝齊基底工具:

```bash
sudo apt update
sudo apt install -y \
  git curl wget \
  build-essential cmake pkg-config \
  python3 python3-venv python3-pip \
  ripgrep tmux
```

另外裝 **Node.js LTS + npm**(§1.2 裝 OpenCode / Codex CLI 用)。Ubuntu 24.04 內建 nodejs 太舊,建議用 NodeSource 官方源裝 LTS:

```bash
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
sudo apt install -y nodejs
node -v && npm -v    # 確認 node ≥ 18 / 20 LTS、npm 可執行
```

已經有 nvm / fnm / volta 的用熟悉的方式裝 Node LTS 即可(版本 ≥ 18)。

### 1.2 安裝 frontend CLI

#### 1.2.1 OpenCode

```bash
npm install -g opencode-ai
command -v opencode    # 確認可被找到
```

#### 1.2.2 Codex CLI(選用)

```bash
npm install -g @openai/codex
command -v codex       # 確認可被找到
```

### 1.3 安裝 CodeTrail Python 依賴

Ubuntu 24.04 啟用 PEP 668,system Python 不允許直接 `pip install`。最省事的方式是裝進**使用者層級 site-packages**(不動系統 Python、也不用每個 shell activate):

```bash
cd <CODETRAIL_REPO>
python3 -m pip install --user --break-system-packages -r requirements.txt
python3 -m pip install --user --break-system-packages pymupdf4llm    # 選用:RAG 從 PDF 建知識庫才用
python3 -c "import mcp, numpy, requests; print('deps OK')"
```

`<CODETRAIL_REPO>` 是這個 CodeTrail 的 repo 路徑,不是你要分析的專案路徑。`requirements.txt` 已含 `mcp` / `requests` / `numpy`,不必再單獨 `pip install mcp`。

> 想隔離環境的話也可以用 venv(`python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`,見 [docs/setup.md](docs/setup.md))。**用 venv 的話,跑 `./set_config.sh` 前要先 activate** —— set_config 會把「當下這顆 Python」的絕對路徑寫進 OpenCode 的 MCP 設定,之後新 shell 不 activate 也能啟動;但也因此換環境後要重跑一次 `./set_config.sh`。

### 1.4 僅 Blackwell GPU 需要升級 CUDA Toolkit 到 13

Ubuntu 24.04 的 `nvidia-cuda-toolkit` 套件停在 CUDA **12.0**,**不認識 Blackwell 的 `sm_120` / `compute_120a`**。如果你用 RTX 50 系列(5070 / 5080 / 5090)或 RTX PRO 6000 Blackwell,build llama.cpp 時會看到:

```
nvcc fatal : Unsupported gpu architecture 'compute_120a'
```

非 Blackwell(RTX 30/40、Ampere、Hopper)可直接跳到 1.5。

驗證需不需要升級:

```bash
nvidia-smi | grep "CUDA Version"   # 驅動支援的最高 CUDA(只要 >= 12.8 就有救)
nvcc --version                      # 目前已安裝的 toolkit 版本
```

升級流程(Ubuntu 24.04 / noble):

```bash
# (a) 加 NVIDIA 官方 apt repo
cd /tmp
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt update

# (b) 只裝 toolkit,不裝驅動(避免跟你現有 driver 打架)
sudo apt install -y cuda-toolkit-13-0

# (c) 移除 Ubuntu 內建舊 toolkit(避免 /usr/bin/nvcc 還是被當第一順位)
sudo apt remove --purge nvidia-cuda-toolkit nvidia-cuda-toolkit-doc nvidia-cuda-dev
sudo apt autoremove

# (d) 把新 toolkit 加進 PATH 並寫進 ~/.bashrc
echo 'export PATH=/usr/local/cuda-13.0/bin:$PATH' >> ~/.bashrc
echo 'export LD_LIBRARY_PATH=/usr/local/cuda-13.0/lib64:$LD_LIBRARY_PATH' >> ~/.bashrc
source ~/.bashrc

# (e) 確認新版本生效
hash -r
which nvcc          # 應為 /usr/local/cuda-13.0/bin/nvcc
nvcc --version      # 應顯示 release 13.x
```

若 `apt install cuda-toolkit-13-0` 想升級 / 移除你現有的 `nvidia-driver-*`,**停下來檢查**,通常不該發生;直接 `y` 可能會把 GPU 驅動換掉。

### 1.5 Build llama.cpp(CUDA)

固定 clone 到 `~/llama.cpp` —— launcher 預設找 `~/llama.cpp/build/bin/llama-server`(放別處要設 `LLAMA_BIN`):

```bash
cd ~
git clone https://github.com/ggerganov/llama.cpp ~/llama.cpp
cd ~/llama.cpp
cmake -B build -DGGML_CUDA=ON -DLLAMA_CURL=OFF
cmake --build build --config Release -j
```

`cmake -B build ...` 跑完先看輸出有沒有:

- `Found CUDAToolkit: ... (found version "13.x")` —— Blackwell 用戶要 13.x;其他卡 12.x 也行
- `Compiler: /usr/local/cuda-13.0/bin/nvcc` —— 不是 `/usr/bin/nvcc`
- 結尾 `Configuring done` / `Generating done`,沒有 `errors occurred`

第二條 `cmake --build` 編譯 20–40 分鐘。完成後 `~/llama.cpp/build/bin/llama-server` 就是後面要用的執行檔。

> 如果之前 build 失敗過(例如 CUDA 升級之前),**`rm -rf build` 再重來**,CMake 的快取會記住舊 toolkit 路徑。
>
> 建議用**新版** llama.cpp:主模型比 VRAM 大時,`set_config.sh` 產生的推薦參數會用新版的 `-ngl auto --fit on` 自動 VRAM 配置;太舊的 build 沒有 `--fit`,set_config 會提醒你升級。

---

## 2. 下載 GGUF

模型統一放 `~/models`(`set_config.sh` 預設掃這裡;放別處用 `MODELS_DIR` 或 `--models-dir` 指定)。

### 2.1 安裝 Hugging Face CLI + hf-transfer 加速

下載指令使用 Hugging Face 新版 `hf` CLI;`hf-transfer` 只負責加速下載,不提供 `hf` 命令本身。預設下載走單連線,實測 ~12 MB/s;裝 `hf-transfer` 後可以拉到 ~270 MB/s(視網路與 HF CDN 上限):

```bash
python3 -m pip install --user --break-system-packages -U "huggingface_hub[cli]" hf-transfer
command -v hf    # 沒輸出代表 ~/.local/bin 不在 PATH(見 Quick Start 第 5 步的修法)
python3 -c "import hf_transfer; print('hf-transfer', hf_transfer.__version__)"
```

啟用方式:下載指令前面加 `HF_HUB_ENABLE_HF_TRANSFER=1`。

### 2.2 下載主聊天模型(`<CODE_MODEL>`)

CodeTrail 刻意不指定主模型。請依工作負載與硬體選 GGUF;多 shard 模型下載完整目錄即可,`set_config.sh` 會自動抓 shard 1 當入口、llama.cpp 會接續讀取其餘分片。RTX 5090 reference 的特定模型、量化與下載資訊見 [docs/verified-reference-5090.md](docs/verified-reference-5090.md)。

### 2.3 下載 RAG 附屬模型

CodeTrail 的 RAG / Code-RAG 預設使用 `bge-m3`(embedding)與 `qwen3-reranker-0.6b`(reranker)。兩者都是必要副模型:聊天 frontend 啟動前會硬性檢查 embedding / reranker / VL 都 ready,reranker 缺失不再降級成 embedding 排序。這兩個體積很小:

```bash
# embedding:bge-m3 (用 f16,不要量化 — embedding 對量化敏感,Q4 會明顯影響召回)
HF_HUB_ENABLE_HF_TRANSFER=1 hf download \
  CompendiumLabs/bge-m3-gguf bge-m3-f16.gguf \
  --local-dir ~/models/bge-m3

# reranker:Qwen3-Reranker 0.6B Q8_0
HF_XET_HIGH_PERFORMANCE=1 hf download \
  ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF qwen3-reranker-0.6b-q8_0.gguf \
  --local-dir ~/models/qwen3-reranker-0.6b
```

兩個合計約 2GB 級。

### 2.4 VL 模型

CodeTrail 的內建 VL key 是 `qwen3.5-9b`。Qwen3.5-9B 是原生多模態模型,適合本專案的截圖、UI 錯誤畫面與圖片 ingestion。VL 模型必須跟 mmproj 放在**同一個目錄**,`set_config.sh` 才能自動配對;若你要用別的相容 VL GGUF,設定時選你自己的檔案即可。

> 「圖片 ingestion」就是 **VL + RAG 一起用**:`ingest_document(...)` 餵圖片時會自動呼叫 VL 把圖看成文字、再切 chunk 進知識庫,所以截圖/架構圖/規格頁能變成之後 `query_knowledge(...)` 查得到的內容。一次性看圖用 `analyze_file(...)`,要長期反覆查改用 `ingest_document(...)`;完整串接見 [docs/rag.md](docs/rag.md)。

```bash
HF_XET_HIGH_PERFORMANCE=1 hf download \
  unsloth/Qwen3.5-9B-GGUF \
  Qwen3.5-9B-Q6_K.gguf \
  mmproj-F16.gguf \
  --local-dir ~/models/qwen3.5-9b
```

---

## 3. 設定與啟動:`./set_config.sh` + `~/start.sh`

### 3.1 `./set_config.sh` 做什麼

設計給**剛接觸專案者**:預設不需要懂 embedding / reranker / mmproj 的差別,只要確認一頁建議配置。在 `<CODETRAIL_REPO>` 執行 `./set_config.sh`,它會依序:

1. **前置檢查**:Python 依賴(mcp/numpy/requests)、`tmux`、`nvidia-smi`、`llama-server` 是否存在且支援必要旗標(`--reranking` / `--mmproj`;`--fit` 沒有只警告)。缺什麼直接給**可複製的修復指令**(裝哪個套件、跑哪行 build),修完重跑即可。
2. **偵測與判定**:GPU 種類/VRAM、`~/models` 的 GGUF 自動分類成主聊天 / embedding / reranker / VL+mmproj 四類;多 shard 自動聚合並**驗證齊全性**(缺片、零大小、殘留 `.incomplete` 都會列出檔名與補救指令)。缺任何一類直接列出並指到 §2;單 GPU 可以跑(全部共用一顆卡)但會提示建議雙卡分工。
3. **容量可行性規劃 + 建議配置一頁確認**:主模型放 VRAM 最大的 GPU、三顆附屬模型共用另一顆,並做**整機容量預估**(每張 GPU 預算 = 總 VRAM − 保留;先扣附屬模型與 KV/buffer 概估,再判主模型):附屬模型自己就塞不下 → 直接失敗;主模型塞不下且 llama-server 沒有 `--fit` → **拒絕產生必定 OOM 的設定**(`--ignore-capacity` 可強制);與附屬模型共卡時 `--fit-target` 自動抬高。有 mmproj 的 VL 模型**不會被自動選成主模型**(只剩 VL 可選時會明確警告/確認)。`ctx`(預設 65536)、`threads`、`--no-mmap`(RAM 夠才開)、OpenCode `limit.context` 對齊、MCP timeout、MCP 用的 Python 路徑全部自動補好。看完按 **Enter 採用**;要逐項自選按 **a**(或直接 `./set_config.sh --advanced`);**q** 離開不寫任何檔案。
4. **產生四個檔案**(transaction 寫入:要嘛全套完成、要嘛完全不動;既有檔自動備份 `*.bak-setconfig-<時間戳>`,`--restore-last-backup` 可整批還原):

| 產物 | 內容 |
|---|---|
| `~/.config/codetrail/models.json` | 主模型 registry key → GGUF 路徑(合併既有內容) |
| `~/.config/codetrail/deployment.json` | deployment profile local override:四個 role 的模型與主模型推薦參數 |
| `~/.config/opencode/opencode.json` | **合併**而非重建:只更新 CodeTrail 管的欄位(model / provider.llamacpp / mcp.codetrail / 缺少的 permission 鍵),你原本的 provider、主題、其他 MCP server 都保留;與安全範本衝突的 permission 會尊重你的值但明確警告 |
| `~/start.sh` | 啟動腳本:寫死你的 GPU 配置與主模型,呼叫 `scripts/start-all.sh`;支援 `status` / `stop` / `logs` 子命令 |

結尾會自動印出**推薦啟動參數**(四個 server 各自完整的 `llama-server` 指令,即 `~/start.sh --dry-run` 的輸出),並標明目前只完成「第 1 層:設定檔驗證」—— 模型能否真的載入,以 `~/start.sh` 實際啟動為準。若偵測到 CodeTrail server 正在執行,會提醒(並可選擇自動)重啟才生效。

非互動用法(自動化 / 重跑):`./set_config.sh --yes` 全用建議值;`--allow-remote` 開放區網連線(預設只綁 127.0.0.1);`./set_config.sh --help` 看完整旗標(可直接指定各模型與 GPU)。

### 3.2 啟動與停止

```bash
~/start.sh              # 啟動 main + embedding + reranker + VL(各自 tmux 視窗,驗 /health 才算 ready)
~/start.sh --dry-run    # 只印出將執行的四條 llama-server 指令,不啟動
~/start.sh status       # 檢查四個 server 狀態(= scripts/check-status.sh)
~/start.sh stop         # 關閉全部 tmux 視窗(主模型 + 三附屬模型;= scripts/quit.sh)
~/start.sh logs vl      # 看啟動失敗時自動保存的該 role server log
```

啟動時的行為(對剛接觸專案者友善):

- **server log 從啟動第一刻就持續寫入** `~/.local/state/codetrail/logs/<role>.log`(tmux pipe-pane)—— 即使 llama-server 因參數或模型錯誤秒退、tmux 視窗消失,完整錯誤訊息也已經在檔案裡,`~/start.sh logs <role>` 直接看。
- **載入進度**:大模型載入要幾分鐘,等待期間每 15 秒回報「載入中,已等待 N 秒(process 存活)」,不會看起來像當機;health 等待上限依主模型大小自動放大。llama-server process 一死就立即失敗,不會空等 timeout。
- **失敗自動清理**:某個 role 啟動失敗時,launcher 自動關閉本次啟動的其他服務並釋放 port,然後告訴你「修正後直接重跑 `~/start.sh`」—— 不會留下半套 tmux 讓下次啟動卡 `session already exist`(要保留現場除錯:`AICODE_NO_ROLLBACK=1`)。
- **綁定**:預設四個 server 只綁 `127.0.0.1`;`--allow-remote` 設定過的才綁 `0.0.0.0`。

| 預設 port | 角色 | 必要 |
|---|---|---|
| 8080 | main(聊天、推理、工具呼叫) | 是 |
| 8081 | embedding(算向量,RAG 搜相似段落) | 是 |
| 8082 | reranker(RAG 結果重排) | 是 |
| 8083 | VL(看截圖 / 圖片) | 是 |

會分四個 `llama-server` 是因為它一次只能載一顆 GGUF,不同角色用不同模式(`--jinja` / `--embedding --pooling cls` / `--embedding --pooling rank --reranking` / `--mmproj`)。`aicode` / `aicodex` / `mcp_server.py` 都會硬性檢查三顆副模型已 ready。

只重啟部分角色:`scripts/stop-rag-servers.sh` + `scripts/start-rag-servers.sh`(只動三顆附屬),或 `scripts/start-main-server.sh`(只起主模型)。

> **tmux 你會用到的 4 個指令**(其他都不用學):
> - `Ctrl-b d` —— 把目前 session 放背景,回到原本 shell
> - `tmux ls` —— 列出所有背景 session
> - `tmux a -t <名字>` —— 接回去看某個 session 的即時 log
> - `Ctrl-b n` —— 同 session 內切換 window(RAG session 內含 embed / rerank / vl 三個 window)
>
> (關 server 不用學 tmux 指令,直接 `scripts/quit.sh`。)

### 3.3 驗活與維運

照上面流程跑下來會有 **2 個 tmux session**(main 自己一個、三顆附屬合在一個):

```bash
tmux ls
# 應該看到:
#   codetrail-main: 1 windows (created ...)
#   codetrail-rag:  3 windows (created ...)    ← 內含 embed + rerank + vl 三個 window
```

查看四個 role 是否都正確跑在指定 GPU 上:

```bash
./scripts/check-status.sh

# CI / 自動化需要用 exit code 擋下時:
./scripts/check-status.sh --strict
```

`check-status.sh` 會把 `nvidia-smi` PID 與 `/proc/<PID>/cmdline` 的 `--port` 對上有效 profile,逐 role 顯示 PID、GPU UUID、model、`n_ctx`、health。預設 report-only,即使異常仍 exit 0;`--strict` 遇到缺 service、錯 GPU、錯 model、錯 ctx 或 unhealthy 就失敗。

之後要關掉全部:

```bash
./scripts/quit.sh
```

偵錯時要看 server log(平常不用):`tmux a -t codetrail-main` 或 `tmux a -t codetrail-rag`(rag 內按 `Ctrl-b n` 切 embed/rerank/vl window,看完 `Ctrl-b d` 退出)。

RTX 5090 reference 的 VRAM/RAM、prompt processing、decode 與 concurrency 實測數字統一放在 [docs/verified-reference-5090.md](docs/verified-reference-5090.md)。

---

## 4. 手動設定(進階;`set_config.sh` 已自動涵蓋)

`./set_config.sh` 產生的就是本節這些檔案。手動微調、換機部署、或想理解機制時再看這節。

### 4.1 Deployment profile

四個 server 共用同一份嚴格 deployment profile(單一事實來源;`aicode`、doctor、啟動前 preflight、status 與所有 launcher 都讀它)。優先序固定為:

```text
launcher CLI / env > ~/.config/codetrail/deployment.json local override > 選用 profile > 安全相容預設
```

`deployment_profiles/defaults.json` 是向下相容的安全基底;硬體 profile 必須明確選用,不會自動套上:

- `maintainer-target`:H200＋448GB RAM 跑 main,RTX 2000 Ada 跑三個 aux;`verification=unverified`。
- `verified-reference`:RTX 5090＋170GB RAM 的既有實測設定;完整 tuning 與 benchmark 在 [docs/verified-reference-5090.md](docs/verified-reference-5090.md)。

手動啟動範例(等價於 `~/start.sh` 做的事):

```bash
cd <CODETRAIL_REPO>
export AICODE_MODEL=<CODE_MODEL>
export MAIN_GPU=<主模型_GPU_UUID_或_INDEX>
export AUX_GPU=<附屬模型_GPU_UUID_或_INDEX>   # EMBED_GPU / RERANK_GPU / VL_GPU 可個別覆寫

./scripts/start-all.sh --dry-run    # 先看最終參數;不啟動、不連網
./scripts/start-all.sh              # 啟動四個 tmux server,嚴格驗證 role / GPU / model / ctx / health
./scripts/check-status.sh --strict
AICODE_MODEL=<CODE_MODEL> python scripts/doctor.py
```

`~/.config/codetrail/deployment.json` 可持久選 profile 並做局部覆寫:

```json
{
  "schema_version": 1,
  "profile": "maintainer-target",
  "services": {
    "main": { "model": "<CODE_MODEL>" }
  }
}
```

所有 service 都有同級 `model`、`port`、`base_url`、`bind`(`local` 預設只綁 127.0.0.1 / `all-interfaces` 綁 0.0.0.0)、`gpu_role`、`ctx`、`batch`、`ubatch`、`parameters`;VL 另外有 `mmproj`。模型欄只接受 registry key 或 GGUF 絕對路徑,參數只接受 schema allowlist(含新版 llama.cpp 的 `gpu_layers: "auto"`、`fit`、`fit_target`、`parallel`),沒有 raw shell `extra_args`;JSON 不會被 `source` / `eval`。schema 與 GPU precedence 詳見 [docs/deployment-profiles.md](docs/deployment-profiles.md)。可離線查看合併結果:

```bash
AICODE_MODEL=<CODE_MODEL> python deployment_profile.py show
```

`AICODE_RERANK_FALLBACK_POLICY` 只控制啟動後 reranker 呼叫失敗時的行為;啟動前 preflight 仍要求 reranker server ready:

| policy | RAG 知識庫 fallback | Code RAG fallback |
|---|---|---|
| `embedding` | 保留 embedding / hybrid 既有排序,不呼叫主模型 | 同左 |
| `main_model` | 還原舊行為,用主聊天模型做 LLM rerank | 等同 `embedding`(Code RAG 沒有主模型 rerank 路徑) |
| `error` | 直接報錯,不靜默降級 | 直接報錯 |

預設是 `error`:專用 reranker 不可用或呼叫失敗就直接報錯。`main_model` 可能很貴:嚴格模式下每條符合條件的 RAG query 都可能觸發主模型 rerank。只有你明確接受這個成本時才設定 `AICODE_RERANK_FALLBACK_POLICY=main_model`。

### 4.2 Model registry(短名稱 → GGUF 路徑)

讓 `AICODE_MODEL=<CODE_MODEL>` 這種短名稱自動對應到實際 GGUF 路徑,不用每次打絕對路徑:

```bash
mkdir -p ~/.config/codetrail
cat > ~/.config/codetrail/models.json <<'EOF'
{
  "<CODE_MODEL>": "/absolute/path/to/main.gguf"
}
EOF
```

registry value 也可寫 `~`,loader 會展開並要求它解析成絕對 `.gguf` 路徑。多 shard 模型指向第一片即可。也可以跳過 registry 直接把 `AICODE_MODEL` 設絕對路徑,但 registry 比較好維護。附屬模型不需要 registry:`set_config.sh` 直接把絕對路徑寫進 deployment.json。

### 4.3 OpenCode config

llama-server 提供 OpenAI 相容 `/v1`,OpenCode 用 openai-compatible provider 即可。`set_config.sh` 產生的 `~/.config/opencode/opencode.json` 就是下面這份範本(把所有 `<CODE_MODEL>` 換成你 4.2 裡用的 registry key):

```json
{
  "$schema": "https://opencode.ai/config.json",

  "share": "disabled",
  "autoupdate": false,

  "enabled_providers": ["llamacpp"],

  "model": "llamacpp/<CODE_MODEL>",
  "small_model": "llamacpp/<CODE_MODEL>",

  "provider": {
    "llamacpp": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "llama.cpp local",
      "options": {
        "baseURL": "http://localhost:8080/v1",
        "apiKey": "dummy"
      },
      "models": {
        "<CODE_MODEL>": {
          "name": "<CODE_MODEL>",
          "limit": { "context": 65536, "output": 8192 }
        }
      }
    }
  },

  "mcp": {
    "codetrail": {
      "type": "local",
      "command": [
        "bash",
        "-lc",
        "root=$(git rev-parse --show-toplevel 2>/dev/null || pwd -P); exec \"$root/.opencode/run-codetrail-mcp\""
      ],
      "enabled": true,
      "timeout": 660000
    }
  },

  "permission": {
    "*": "deny",

    "question": "allow",
    "todowrite": "allow",

    "codetrail_*": "allow",
    "codetrail_apply_patch": "ask",
    "codetrail_run_lint": "ask",
    "codetrail_run_command": "ask",
    "codetrail_import_external_file": "allow",

    "webfetch": "deny",
    "websearch": "deny",
    "bash": "deny",
    "read": "deny",
    "grep": "deny",
    "glob": "deny",
    "edit": "deny",
    "write": "deny",
    "apply_patch": "deny",
    "external_directory": "deny",
    "task": "deny",
    "skill": "deny",
    "lsp": "deny"
  }
}
```

(`set_config.sh` 產生的版本只差一處:MCP `command` 直接寫死偵測到的 Python 絕對路徑執行 `mcp_server.py`,不經 `.opencode/run-codetrail-mcp` wrapper,所以新 shell 不必 activate venv。上面範本用 wrapper 的寫法對手動設定者較通用 —— `aicode` 會在專案 git root 自動產生該 wrapper。)

`mcp.codetrail.timeout` 的單位是毫秒,而且套用到每一次 MCP tool call。圖片 VL 分析通常超過 10 秒,`ingest_document` 的內部上限則是 10 分鐘,因此範本使用 660000 ms(11 分鐘)。若沿用 OpenCode 常見的 `10000`,第一個圖片呼叫會在剛好 10 秒被 client 切斷,後續 `file_info` / `list_dir` 也可能排在尚未結束的圖片請求後面,看起來像整個 MCP server 一起超時。

`aicode` 啟動時會把**既有** `mcp.codetrail` entry 中缺漏、型別錯誤或小於 660000 的 `timeout` 自動同步為專案常數,保留其餘 OpenCode JSON 設定,並在同目錄留下 `opencode.json.codetrail.bak`(若已存在則加數字後綴)。寫入採原子替換;設定檔格式錯誤或無法寫入時會 fail-loud,不會帶著已知錯誤啟動 OpenCode。只有緊急測試才用 `AICODE_MCP_TIMEOUT_CHECK_SKIP=1 aicode` 跳過。

說明:

- `llamacpp` 是 provider key,可改名(`local`、`llmcpp`、隨意),但要跟 `"model"` 那段的 prefix 對齊。
- `enabled_providers` 鎖定只啟用本機 provider:設了之後 OpenCode 的 model picker(TUI 與 web)**只會出現你的本機模型**,雲端 provider(OpenCode Zen、Anthropic、OpenAI 等)完全不列出、無法誤選 —— **NDA 場景強烈建議保留**,避免把程式碼送到雲端模型。陣列內字串要跟你的 provider key 一致(這裡是 `llamacpp`)。
- `apiKey` 任意非空值即可,llama-server 預設不檢查。
- **取樣參數(temperature / top_p / top_k / min_p)不要寫在這裡指望它生效**。OpenCode 的 openai-compatible provider 對自訂 provider 有已知問題;應在 deployment profile 的 allowlisted `parameters` 內設定,由 server launcher 釘住。特定 5090/Qwen reference 值見 [docs/verified-reference-5090.md](docs/verified-reference-5090.md),不要套到未知模型。
- **要壓「模型杜撰不存在的具體事實」(條號 / 日期 / 數字),在 `~/.config/opencode/AGENTS.md` 加一條防杜撰規則**(OpenCode 會自動把它載入每一段對話,含純聊天)。範例與原理見 [docs/troubleshooting.md](docs/troubleshooting.md)。注意這個 `~/.config/opencode/AGENTS.md` 是 OpenCode runtime 的全域規則檔,跟本 repo 根目錄那份「給修改 CodeTrail 原始碼的 agent 看的」`AGENTS.md` 是兩回事。
- `limit.context: 65536` 是 OpenCode 主對話實際塞給 server 的上限。它必須等於 llama-server 啟動時的 `-c <N>`(server 是 ctx 上限的唯一真值);CodeTrail 端的 ctx 上限會自動跟隨 server,所以你只要顧好「`limit.context` == server `-c`」這一個對齊就好(`set_config.sh` 產生時已對齊)。`aicode` 啟動時會檢查,不一致就拒絕啟動。
- `permission` 區段:`*: deny` 是預設拒絕一切,只白名單 `codetrail_*`(經 CodeTrail 沙箱)。OpenCode 內建工具(`bash` / `read` / `write` 等)會繞過 CodeTrail 沙箱,所以這裡明確 `deny`。

手動貼完先驗 JSON 格式:

```bash
python -m json.tool ~/.config/opencode/opencode.json >/dev/null
```

### 4.4 Codex CLI frontend(選用)

`aicodex` 的安裝同 `aicode`(Quick Start 步驟 2–4,把 `aicode` 換成 `aicodex`)。它不會自動修改 `~/.codex/config.toml`,啟動時用 Codex CLI 的 runtime `-c` override 注入 CodeTrail MCP server:

```bash
cd <PROJECT_TO_ANALYZE>
aicodex --codetrail-model <LOCAL_MODEL>
```

如果你想讓 **Codex frontend** 也走本機 llama.cpp provider,可以自行在 `~/.codex/config.toml` 加類似設定(選用):

```toml
# ~/.codex/config.toml
model = "<LOCAL_MODEL>"
model_provider = "llamacpp"

[model_providers.llamacpp]
name = "llama.cpp local"
base_url = "http://localhost:8080/v1"
wire_api = "responses"
```

這只是 Codex frontend provider 設定,和 CodeTrail MCP internal model 設定分開。`--codetrail-model` / `AICODE_MODEL` 控制的是 `mcp_server.py` 與 CodeTrail server-side tools 使用的本地模型;Codex 的 `-m` / `--model` 控制的是 Codex frontend model。`aicodex` 不會把 `--codetrail-model` 轉發給 Codex CLI;Codex frontend 的 `-m` / `--model` 會原樣轉發。

---

## 5. 自檢與啟動 TUI

### 5.1 跑 doctor 自檢

```bash
AICODE_MODEL=<CODE_MODEL> python scripts/doctor.py
```

(把 `<CODE_MODEL>` 換成你的 registry key —— `set_config.sh` 結尾的設定摘要有印,或看 `~/.config/codetrail/models.json`)

預期結尾看到 `PASS=2x WARN=x FAIL=0`。常見可忽略的 WARN:

- `html2text 沒裝` —— 只有 RAG 抓網頁要,可忽略
- `knowledge.json 不存在` —— RAG 知識庫還沒建立,等用到再說

**有 FAIL 不要跳過**,通常是 PATH、server 沒啟動、GGUF 路徑寫錯。對應修法見 [docs/troubleshooting.md](docs/troubleshooting.md)。

### 5.2 啟動 TUI

切到你要分析或修改的專案目錄(**不要從 `$HOME` 或 `/` 啟動**,沙箱會拒絕)。兩個 frontend 是並列入口,選一個用即可。

OpenCode frontend:

```bash
cd <PROJECT_TO_ANALYZE>
aicode
```

Codex CLI frontend:

```bash
cd <PROJECT_TO_ANALYZE>
aicodex --codetrail-model <LOCAL_MODEL>
```

`<LOCAL_MODEL>` 用 §4.2 registry 裡的 `<CODE_MODEL>` bare name 或 GGUF 路徑。`aicode` 不用帶參數:主模型會依「env `AICODE_MODEL` > `-m` 旗標 > deployment.json > opencode.json」解析,`set_config.sh` 已把後兩者設好。

要讓模型讀專案外的附件(`~/Downloads` 的 log / 截圖 / spec)就多加一個開關:

```bash
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 aicode
```

來源白名單(`AI_CODE_IMPORT_ROOTS`)等細節見 [docs/basic-usage.md](docs/basic-usage.md)。第一次先照上面最短的指令跑起來就好。

### 5.3 簡單測試

進到 TUI 後輸入:

```text
請用工具 list_dir 看當前目錄結構,挑出 entry point、主要模組和測試目錄,簡單整理。
```

模型應該會透過 CodeTrail MCP 呼叫 `list_dir`(OpenCode log 裡可能顯示成 `codetrail_list_dir`)讀真實目錄,然後回給你整理結果。

第一個請求**首字延遲(TTFT)**:

- 用 `--no-mmap` 模式:約 5–15 秒
- 用 mmap 模式(沒加 `--no-mmap`):**第一次可能要 1–2 分鐘**,因為要從 SSD page-in MoE expert weights。畫面上 frontend 可能顯示「`...esc interrupt`」或類似等待狀態,**不要按 Esc**,等就對了

如果想驗證工具有沒有連上:OpenCode TUI 輸入 `/status`,應看到 `codetrail Connected`;Codex TUI 輸入 `/mcp`,應看到 `codetrail` connected。

想把**圖片**(截圖、架構圖、規格頁掃描)變成之後查得到的知識,就是「VL + RAG 一起用」—— `ingest_document` 餵圖片時會自動走 VL 把圖抽成文字再進 RAG,跟 PDF 走同一套:

```text
請用工具 ingest_document 匯入 docs/diagram.png,
完成後 reload_knowledge_base,
再用 query_knowledge 查這張圖的重點,回答附 REF。
```

(圖片附件需要 VL server :8083 已啟動。聊天截圖模式、外部圖片匯入、binary/ELF 等完整串接見 [docs/rag.md](docs/rag.md)。)

更多操作模式(夾帶附件、注入 RAG、查 spec)見 [docs/basic-usage.md](docs/basic-usage.md);完整 17 個工具清單見 [docs/mcp-tools.md](docs/mcp-tools.md)。

### 5.4 Web 模式(目前測試中)

> ⚠️ **CodeTrail 的沙箱綁在「你啟動 backend 的那個資料夾」(`AICODE_ROOT`)—— 綁在 process 上,不會跟著你在 UI 切資料夾、或切對話而移動。** 所以 OpenCode web UI 那顆「切換資料夾 / 開其他專案」按鈕**對 CodeTrail 完全無效**:切過去後 CodeTrail 工具還是只讀**啟動目錄**(讀不到沙箱外,所以不是 escape,但會讓你誤以為切了)。**請無視那顆切換器。** 要分析別的專案,就在那個目錄**另起一個 backend**(換 port,例:`AICODE_WEB_PORT=4097 <CODETRAIL_REPO>/scripts/start-web.sh`)。
>
> (TUI 沒有這顆切換器,你 `cd 專案 && aicode` 在裡面開幾個對話都是鎖在同一個專案,自然不會錯亂;換專案就重開一個 `aicode`。)

1. server 和你要瀏覽的裝置都裝 [Tailscale](https://tailscale.com/) 並登入**同一個 tailnet**。
2. 在 server 把 loopback 的 web port 掛上 tailnet(常駐、跨重開機):

   ```bash
   tailscale serve --bg --https=4096 4096
   tailscale serve status     # 看到 https://<你的-server>.<tailnet>.ts.net:4096 → 127.0.0.1:4096 就對了
   ```

   把印出來的 `https://<你的-server>.<tailnet>.ts.net:4096/` 加到瀏覽器最愛。

   > ⚠️ **一定用 `tailscale serve`(只限 tailnet 內)。絕不可用 `tailscale funnel`** —— funnel 會把 backend 暴露到**整個公網**,NDA 直接外洩。
   > (server 的 443 沒被占用的話,也可用 `tailscale serve --bg 4096` 拿到沒 port 的短網址 `https://<你的-server>.<tailnet>.ts.net/`。)

**每次使用:**

3. 在 server 啟動 backend(背景,起完就回到提示字元):

   ```bash
   cd <PROJECT_TO_ANALYZE>
   <CODETRAIL_REPO>/scripts/start-web.sh     # 背景啟動(tmux);停止用 stop-web.sh
   ```
   `start-web.sh` 起來後若偵測到 tailscale serve,會直接把那個 ts.net 網址印給你。

**沒裝 / 不想裝 Tailscale 的 fallback** —— SSH port-forward(每次都要開、斷了要重來):用你平常 SSH 進 server 的指令後面加 `-L`,再開本機 `http://127.0.0.1:4096`:

```bash
ssh -L 4096:127.0.0.1:4096 <你的帳號>@<server 位址>
```

## 文件地圖

| 文件 | 內容 |
|---|---|
| [docs/setup.md](docs/setup.md) | 替代安裝方式、進階配置、換機部署 reference |
| [docs/deployment-profiles.md](docs/deployment-profiles.md) | profile schema、precedence、GPU override 與 local override |
| [docs/verified-reference-5090.md](docs/verified-reference-5090.md) | RTX 5090＋170GB RAM 已驗證 tuning / benchmark |
| [docs/basic-usage.md](docs/basic-usage.md) | TUI 內常用操作:正常對話、夾帶附件、RAG 注入、最小驗收流程 |
| [docs/rag.md](docs/rag.md) | 讀檔、匯入附件(PDF / 圖片經 VL)、建立知識庫、圖片+RAG 一起用、Code-RAG、查 spec |
| [docs/mcp-tools.md](docs/mcp-tools.md) | CodeTrail 暴露的 17 個 MCP 工具與使用原則 |
| [docs/security.md](docs/security.md) | 沙箱邊界、OpenCode permission、外部匯入與 NDA 資料注意事項 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | `/status` / `/mcp`、ctx-safety、server 不可連、Blackwell CUDA、MoE 首字慢 |
| [README_DEV.md](README_DEV.md) | 開發者維護命令、測試、eval、context gate 設計 |
| [AGENTS.md](AGENTS.md) | AI coding agent 修改本 repo 時必讀的安全規範 |

---

## License

本專案以 MIT 授權釋出,程式碼以「現狀」(AS IS)提供,不附帶任何明示或默示的保證,
包括但不限於可商用性、特定用途適用性、不侵權、資安、隱私、合規、或 NDA 適用性。
完整法律文字見 [LICENSE](LICENSE);補充免責說明見 [DISCLAIMER.md](DISCLAIMER.md)。

This project is licensed under the MIT License. See [LICENSE](./LICENSE).

## Responsible use

This project is provided for lawful software development, research, education,
and code reasoning workflows.

Users are solely responsible for how they use, modify, deploy, combine, or
redistribute this software, including compliance with applicable laws,
contracts, licenses, NDAs, platform terms, model-provider terms, and third-party
rights.

The authors do not guarantee that any particular workflow is legally compliant,
NDA-compliant, secure, private, or suitable for a specific use case.

The software is provided "as is", without warranty of any kind. The authors do
not encourage, endorse, or provide support for unlawful use.

See [DISCLAIMER.md](./DISCLAIMER.md) for the full disclaimer.
