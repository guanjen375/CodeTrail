# CodeTrail — llama.cpp 本地 Code-RAG / MCP 工作台

CodeTrail 是一套本地 Code-RAG / RAG / MCP 後端,**以及它自己的聊天客戶端**。
模型可以在受限的專案根目錄內搜尋與讀取程式碼、查已匯入的規格文件、分析圖片與
firmware binary、建立 patch,並只透過白名單執行驗證命令。

使用者入口只有一個:終端客戶端 `aicode`(全螢幕 TUI)。
**部署不需要 Node / npm** —— 客戶端是純 Python,與 MCP server 走
同一份 `requirements.txt`。CodeTrail 定位為**成熟私有部署版**，適合本機、離線、NDA / firmware /
private repo 分析；**不打算公開發布**成 PyPI package、Docker image 或 SaaS。安全
邊界有自動測試保護，但未做公開產品級安全審計。

底層推理引擎使用 [llama.cpp](https://github.com/ggml-org/llama.cpp)
`llama-server`。GPU / CUDA 是實用部署的建議路徑；純 CPU 可運作，但大型模型通常會
很慢。CodeTrail 的 internal LLM、embedding、reranker 與 VL 呼叫都走設定中的
llama-server HTTP endpoint。

四類模型與工作環境分開時，請用 [A／B 分離部署](docs/split-deployment.md)：
A 執行模型；B 執行 aicode、MCP、編輯與 build，不需要 GPU 或模型檔。

## 現有能力與邊界

| 能力 | 現況 | 主要入口 |
|---|---|---|
| 專案探索 | sandboxed 目錄、檔案、grep、git status/diff | `list_dir`、`read_file`、`grep_code` |
| Code-RAG | semantic / bounded context 搜尋；選用 C/C++ / Python call/include graph | `code_rag_search` |
| 文件 RAG | PDF / Markdown / text / 圖片 / ELF / firmware 入庫、rerank、strict answer | `ingest_document`、`query_knowledge*` |
| 多模態 | 截圖、圖表、掃描頁與 PDF 內的**純 raster** 內嵌圖走獨立 VL server | `analyze_file`、`ingest_document` |
| PDF 圖片監督 | **有原生證據**的表格 / 向量文字 log 走結構化抽取，帶驗證狀態；未驗證內容被 strict 查詢排除，可人工覆核 | `ingest_document(preflight_only=…)`、`review_figures` |
| 修改與驗證 | 兩種 patch 格式（SEARCH/REPLACE、unified diff）共用 sandbox、上限與 byte-safe 寫入：最多 5 個檔案、單檔 200 行（udiff 算 added+removed；S/R 算 payload budget = SEARCH+REPLACE 行數）；套用後只做唯讀 syntax check（三態、不回滾）；lint / test 走各自的 ask 閘；`run_command`：timeout 只接受整數 1..600 秒（server 端上限；client 可能更早截止） | `apply_patch`、`run_lint`、`run_command` |
| 行為教訓 | 使用者核准後跨 session 注入，90 天複審 | `record_lesson` |
| Frontend | 唯一入口 `aicode`(全螢幕 Textual TUI) | `aicode` wrapper |

> [!IMPORTANT]
> 「本地優先」不等於無條件保證資料不離機。遠端 llama-server 端點就會改變資料
> 邊界(所以非 loopback 端點需要顯式 opt-in)。
> NDA 場景請保留本文件的 provider / permission 鎖定，並先讀
> [安全邊界](docs/security.md)與 [Responsible Use](RESPONSIBLE_USE.md)。

第一次部署照下方 Quick Start；完成後的日常操作看
[基本操作](docs/basic-usage.md)。遇到錯誤直接查
[常見問題](docs/troubleshooting.md)。要用真實對話 session 比較新舊主模型時，使用
[session model eval](docs/session-model-eval.md)；歷史模型回答不會被當成標準答案。

## 🚀 Quick Start(7 步設定完成)

以下是單機 `local` 部署流程。兩台主機請依 [分離部署指南](docs/split-deployment.md)
分別設定 A 的 `model-host` 與 B 的 `client`，模型安裝與啟動只在 A 進行。

先分清楚兩個路徑：`<CODETRAIL_REPO>` 是本 repo，安裝、設定與 server 管理都在這裡；
`<PROJECT_TO_ANALYZE>` 是要分析的 firmware / NDA / private repo，最後才在那裡啟動
`aicode`。命令以 Ubuntu / Debian shell 為主；Windows 建議使用 WSL2 或遠端 Linux GPU
主機。

前提是 §1 的依賴（含 build llama.cpp）與 §2 的四類 GGUF 模型（主聊天 / embedding /
reranker / VL+mmproj，預設放 `~/models`）都已完成。之後只要：

```bash
cd <CODETRAIL_REPO>                          # 1. 進 CodeTrail repo
chmod +x ./aicode                            # 2. 讓啟動指令可執行
mkdir -p "$HOME/.local/bin"                  # 3. 準備使用者 bin 目錄
ln -sfn "$PWD/aicode" "$HOME/.local/bin/aicode"  # 4. 安裝唯一的使用者指令
export PATH="$HOME/.local/bin:$PATH"         # 5. 讓目前這個 shell 立即看得到使用者 bin
command -v aicode                            #    應顯示 ~/.local/bin/aicode
./set_config.sh                              # 6. 一鍵設定(偵測 GPU/模型 → 互動問答 → 產生所有設定檔)
~/start.sh                                   # 7. 啟動四個 llama-server(tmux 背景)
```

然後就可以到任何要分析的專案直接用:

```bash
cd <PROJECT_TO_ANALYZE>
aicode        # CodeTrail 終端客戶端;/tools 應列出 21 個工具
```

要從別台電腦操作就用 SSH:登入這台機器之後照樣 `cd <PROJECT_TO_ANALYZE> && aicode`。
想讓連線斷了也不中斷,把它跑在 `tmux` 裡(`tmux new -s codetrail`,斷線後 `tmux attach -t codetrail`)。

- 第 5 步的 `export` 只處理目前 shell；§1.2 會把同一條 PATH 寫進 `~/.profile`，讓重新登入後仍生效。
- `set_config.sh` 依 main → embedding → reranker → VL 分組問答；推薦值不是硬限制，
  寫入前會顯示摘要，舊設定有備份。完整問答與非互動旗標見 §3.1。
- 啟動 `aicode` 前四個 server 都必須 ready。`~/start.sh status|stop|logs|help` 是統一管理
  入口；重新啟動前先 stop。完整行為見 §3.2–§3.3。
- 四個 server 預設只綁 `127.0.0.1`。安全細節見 [docs/security.md](docs/security.md)。
- system prompt 由客戶端自組:內建基底規則(硬上限 1,600 字元)＋ MCP 工具路由圖
  ＋ 專案 `AGENTS.md` ＋ `.codetrail/lessons.md` ＋ 選用的
  `~/.config/codetrail/instructions.md`。**不要把完整工具清單或操作手冊貼進去** ——
  工具名稱、參數與用途以本輪 tool schema 為唯一真值,重複一份只會增加每輪 prompt。
  要關掉專案內的那兩份,在 `~/.config/codetrail/client.json` 設
  `"project_instructions": false`。

## 特別注意(首次部署最容易踩的)

> [!WARNING]
> 動手前掃一遍 —— 這幾點踩了通常會卡很久,或踩到 NDA / 安全:
>
> 1. **客戶端與 MCP server 都跑在 `aicode` 當下 PATH 的 `python3` 上。** 依賴若只裝在 venv,每次啟動前仍要 activate。重建 venv 或升級 Python 後要**重跑 `./set_config.sh`**(它會重新偵測並寫進啟動腳本)。
> 2. **四個 llama-server 都要起**:main `8080` + embedding `8081` + reranker `8082` + VL `8083`。三顆副模型是硬性需求,缺一個啟動前 preflight 就擋下;reranker 不提供降級方案。見 §3。
> 3. **不要從 `$HOME` 或 `/` 啟動** —— 沙箱會直接拒絕。先 `cd` 進你要分析的**具體專案目錄**再跑。
> 4. **換模型或主 n_ctx 就重跑 `./set_config.sh` + 重啟 server。** llama-server 一啟動就鎖死一顆模型與一個 `-c`;客戶端只會跟隨它,沒有「在對話裡換模型」這回事。主 n_ctx 只填一次;`set_config.sh` 寫進 deployment / server `-c`,`aicode` 啟動時觀測 `/props` 的實值並讓 CodeTrail 的 context 預算跟著它。
> 5. **啟動後立即 rollback,先看 server log**:`~/start.sh` 前台只會回報 process 已結束,真正根因用 `~/start.sh logs main` 查看;新 GGUF 也可能需要更新並重新 build llama.cpp。詳細判讀與修復見 [docs/troubleshooting.md](docs/troubleshooting.md)。
> 6. **CodeTrail 沙箱鎖在「你啟動的那個資料夾」** —— 綁在 process 上,**不會跟著你切對話而移動**。換專案 = 到那個目錄重新開一個 `aicode`。沒有 `--root`、沒有環境變數可以改它。
> 7. **模型只有那 21 個 MCP 工具** —— 客戶端沒有內建的 `bash` / `read` / `write`,所以沙箱邊界就是 MCP server 的邊界。外部匯入與 lessons 是兩個受限例外,見 [docs/security.md](docs/security.md)。分析不信任 repo 時,那個 repo 自帶的 `AGENTS.md` 與 `.codetrail/lessons.md` 會進 system prompt;不想要就在 `~/.config/codetrail/client.json` 設 `"project_instructions": false`。
> 8. **首次 MoE 對話首字會慢(可能 1–2 分鐘),別按 Esc** —— 它在 page-in expert weights,不是當掉;slot / GPU 在動就是正常。
> 9. **NDA / 衍生資料不要 commit**:`knowledge*.json`、`knowledge_emb.npz`、`*.jsonl`、`.codetrail/`、`data/`、`.aicode_uploads/` 與 Code-RAG cache / graph DB 等已在 `.gitignore`。commit 前同時看 `git status` 與 `git diff`；`.gitignore` 擋不住被改名或複製的內容。
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

(Node.js 只在你另有用途時才需要;CodeTrail 本身不用。)如果 distro 內建
Node.js 不是 LTS，可用 NodeSource 的 LTS 渠道：

```bash
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
sudo apt install -y nodejs
node -v && npm -v    # 確認是目前 Node LTS，npm 可執行
```

已經有 nvm / fnm / volta 的用熟悉的方式裝當前 Node LTS 即可。

### 1.2 CodeTrail 不需要 Node / npm

舊世代的日常入口是另一個 Node 前端的 TUI,所以要先裝它。**現在不用了**:
CodeTrail 有自己的終端客戶端(`codetrail_chat.py`,由 `aicode` 啟動),純 Python、
與 MCP server 共用同一份 `requirements.txt`。上一節裝 Node 只是為了其他用途,
CodeTrail 本身不需要它。

裝過舊前端的機器不必移除它。但**升級前**的 CodeTrail 曾經寫過幾個值進那個前端的
設定檔,本版 runtime 完全不碰、也不再附帶還原工具,所以要照
[docs/troubleshooting.md 的升級段](docs/troubleshooting.md)手動解除一次。
`./set_config.sh` **不會**順帶做這件事。

### 1.3 安裝 CodeTrail Python 依賴

Ubuntu 24.04 啟用 PEP 668,system Python 不允許直接 `pip install`。最省事的方式是裝進**使用者層級 site-packages**(不動系統 Python、也不用每個 shell activate):

```bash
cd <CODETRAIL_REPO>
python3 -m pip install --user --break-system-packages -r requirements.txt
python3 -m pip install --user --break-system-packages "pymupdf4llm==1.28.0"    # 選用:RAG 從 PDF 建知識庫才用;釘本 repo 驗證版(上游 page schema 常變動)
python3 -c "import mcp, numpy, requests; print('deps OK')"
```

`<CODETRAIL_REPO>` 是這個 CodeTrail 的 repo 路徑,不是你要分析的專案路徑。`requirements.txt` 已含 `mcp` / `requests` / `numpy` / `jieba` / `pyelftools` 與 C/C++ grammar。環境缺少主要實作時，受影響操作直接報錯；完整需求與舊設定遷移見[依賴需求](docs/dependencies.md)。部署不需要 Node / npm。

截至 2026-08，MCP Python SDK 2.x 已是 stable；但本 repo 的 runtime 仍使用 v1 `mcp.server.fastmcp.FastMCP`，所以 dependency 刻意固定為 `mcp>=1.28,<2`。乾淨安裝會取維護中的最新 1.x，不會誤升到不相容的 2.x；這也符合 [MCP Python SDK 官方給未遷移 v1 專案的建議](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/get-started/installation.md)。v2 遷移需另案同步處理 import、transport 與 schema，不應只移除 `<2`。若 `doctor` 報版本不符，執行 `python3 -m pip install --upgrade "mcp>=1.28,<2"`。

> 想隔離環境的話也可以用 venv(`python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`,見 [docs/setup.md](docs/setup.md))。**只把依賴裝在 venv 時,跑 `./set_config.sh` 與每次啟動 `aicode` 前都要 activate**:set_config 會把當下 Python 的絕對路徑寫進 MCP command,但 `aicode` 自己的啟動前置仍會使用 PATH 裡的 `python3` / `python` 跑檢查腳本。若之後重建或更換 venv,也要重跑 `./set_config.sh`。

### 1.4 Blackwell GPU 需要 CUDA Toolkit 12.8 以上

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

下面用 CUDA Toolkit 13.0 示範升級流程(Ubuntu 24.04 / noble)；依
[NVIDIA 官方架構支援矩陣](https://docs.nvidia.com/datacenter/tesla/drivers/cuda-toolkit-driver-and-architecture-matrix.html)，
Blackwell 的工具鏈下限是 12.8，不是必須恰好 13.0。

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

固定 clone 到 `~/llama.cpp` —— launcher 預設找 `~/llama.cpp/build/bin/llama-server`
(放別處就在 `./set_config.sh --llama-bin <絕對路徑>` 指定,它會寫進
`~/.config/codetrail/deployment.json` 的 `llama_bin`):

```bash
cd ~
git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp
cd ~/llama.cpp
cmake -B build -DGGML_CUDA=ON -DLLAMA_CURL=OFF
cmake --build build --config Release -j
```

`cmake -B build ...` 跑完先看輸出有沒有:

- `Found CUDAToolkit: ... (found version "13.x")` —— 本範例應是 13.x；Blackwell 的最低需求是 12.8
- `Compiler: /usr/local/cuda-13.0/bin/nvcc` —— 不是 `/usr/bin/nvcc`
- 結尾 `Configuring done` / `Generating done`,沒有 `errors occurred`

第二條 `cmake --build` 編譯 20–40 分鐘。完成後 `~/llama.cpp/build/bin/llama-server` 就是後面要用的執行檔。

> 如果之前 build 失敗過(例如 CUDA 升級之前),**`rm -rf build` 再重來**,CMake 的快取會記住舊 toolkit 路徑。
>
> 建議用**新版** llama.cpp:`set_config.sh` 會探測 `--reranking` / `--mmproj` / `--fit`(VL 啟動必要)、`--cache-ram`(embedding / reranker 必要)與 `--cpu-moe` / `--n-cpu-moe`(MoE 主模型 / MoE VL 模型的可選 offload);缺少必要旗標時會直接提醒升級。

---

## 2. 下載 GGUF

模型統一放 `~/models`(`set_config.sh` 預設掃這裡;放別處用 `./set_config.sh --models-dir <目錄>` 指定)。

### 2.1 安裝 Hugging Face CLI + Xet 加速

下載指令使用 Hugging Face 新版 `hf` CLI。新版 `huggingface_hub` 會一併安裝 `hf_xet`,下載時預設自動使用 Xet 與 adaptive concurrency;舊的 `hf-transfer` 已移除,`HF_HUB_ENABLE_HF_TRANSFER` 也不再生效:

```bash
python3 -m pip install --user --break-system-packages -U huggingface_hub
command -v hf    # 沒輸出代表 ~/.local/bin 不在 PATH(見 Quick Start 第 5 步的修法)
python3 -c "from importlib.metadata import version; print('hf-xet', version('hf-xet'))"
```

下面的大型 GGUF 範例以 `HF_XET_HIGH_PERFORMANCE=1` 啟用 Xet 高效能模式。它會積極使用網路、CPU 與較大的記憶體 buffer;若機器 RAM 少於 64GB,拿掉這段前綴即可使用預設的 adaptive concurrency。

### 2.2 下載主聊天模型(`<CODE_MODEL>`)

CodeTrail 刻意不指定主模型。請依工作負載與硬體選 GGUF;多 shard 模型下載完整目錄即可,
`set_config.sh` 會自動抓 shard 1 當入口、llama.cpp 會接續讀取其餘分片。

### 2.3 下載 RAG 附屬模型

CodeTrail 的 RAG / Code-RAG 預設使用 `bge-m3`(embedding)與 `bge-reranker-v2-m3` Q8_0(reranker)。兩者都是必要副模型:聊天 frontend 啟動前會硬性檢查 embedding / reranker / VL 都 ready,reranker 缺失不再降級成 embedding 排序。這兩個體積很小:

```bash
# embedding:bge-m3 (用 f16,不要量化 — embedding 對量化敏感,Q4 會明顯影響召回)
HF_XET_HIGH_PERFORMANCE=1 hf download \
  CompendiumLabs/bge-m3-gguf bge-m3-f16.gguf \
  --local-dir ~/models/bge-m3

# reranker:bge-reranker-v2-m3 Q8_0
HF_XET_HIGH_PERFORMANCE=1 hf download \
  gpustack/bge-reranker-v2-m3-GGUF bge-reranker-v2-m3-Q8_0.gguf \
  --local-dir ~/models/bge-reranker-v2-m3
```

兩個合計約 2GB 級。

若優先考慮排序精準度，也可以安裝 Qwen3-Reranker 0.6B Q8_0。它在
[官方 retrieval benchmark](https://github.com/QwenLM/Qwen3-Embedding#evaluation) 的整體成績
高於 BGE v2-m3，可作為 accuracy-first 候選；但那不是 CodeTrail 私有程式碼/規格資料的
保證，公開部署仍應用自己的查詢做 A/B eval。

```bash
HF_XET_HIGH_PERFORMANCE=1 hf download \
  ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF qwen3-reranker-0.6b-q8_0.gguf \
  --local-dir ~/models/qwen3-reranker-0.6b
```

0.6B 指的是**權重參數量**，不等於啟動後總顯存。Qwen3-Reranker 是 causal 架構；
llama.cpp 的 embedding/reranking server 又會讓 batch 與 micro-batch 相同，因此除了約
0.6 GiB 權重，還會配置隨 ctx 增長的 KV/compute buffer。在過往量測中，Qwen3 的
`-c/-b/-ub 8192` 合計約 6.25 GiB，改成 2048 約 2 GiB；BGE 在 8192 則約
0.7 GiB。數值會隨 llama.cpp、量化與 GPU 變動；`set_config.sh` 不替 reranker 估容量，
實際值以啟動 log / `nvidia-smi` 為準。

`set_config.sh` 會列出偵測到的 reranker 讓你選(多顆時必選,不自動挑),接著問它的
internal buffer。它是獨立 aux server，`-c/-b/-ub` 屬於內部 buffer，不是主模型 n_ctx；
這一題**必答、沒有預設值**,只把維護者驗證過的 `bge-reranker-v2-m3 @ 8192` 當提示顯示
(非互動用 `--rerank-ctx <128-1048576>`,三個參數會同步)。每筆 `query + passage` 原本就放得下
時，單純放大 buffer 不會讓排序更準,只會更吃顯存、更慢;放不下才會截斷而可能漏證據。

### 2.4 VL 模型

CodeTrail 的內建 VL key 是 `qwen3.5-9b`。Qwen3.5-9B 是原生多模態模型,適合本專案的截圖、UI 錯誤畫面與圖片 ingestion。VL 模型必須跟 mmproj 放在**同一個目錄**,`set_config.sh` 才能自動配對;若你要用別的相容 VL GGUF,設定時選你自己的檔案即可。

> 「圖片 ingestion」就是 **VL + RAG 一起用**:`ingest_document(...)` 餵圖片時會自動呼叫 VL 把圖看成文字、再切 chunk 進知識庫,所以截圖/架構圖/規格頁能變成之後 `query_knowledge(...)` 查得到的內容。一次性看圖用 `analyze_file(...)`,要長期反覆查改用 `ingest_document(...)`;完整串接見 [docs/rag.md](docs/rag.md)。
>
> PDF 裡的表格 / memory map / 終端機 log / 整頁散文 / diagram 走**結構化**抽取:有原生文字或幾何就直接利用;只有像素的 raster / picture 則先由 VL 分成 table、terminal、prose 或 diagram，再產生 canonical JSON、格/行級證據與驗證狀態;判定**不是圖面**（封面、logo、照片）的直接跳過，零抽取、零 chunk。看不清的字元放 `▯` 而不是猜。這些候選都受嚴格模式的證據閘保護，也都能用 `review_figures(...)` 人工覆核。**沒被這條 lane 收的頁與區域就是缺席**:不入庫、也不做自由文字描述,ingest 會逐筆列出頁碼、bbox 與原因(2026-08-30 移除舊的自由文字 VL 相容路徑)。

```bash
HF_XET_HIGH_PERFORMANCE=1 hf download \
  unsloth/Qwen3.5-9B-GGUF \
  Qwen3.5-9B-Q6_K.gguf \
  mmproj-F16.gguf \
  --local-dir ~/models/qwen3.5-9b
```

#### RAM 低標(VRAM 沒有)

**VRAM 沒有硬性低標**。放不進 GPU 的部分 llama.cpp 會留在 CPU,功能完全一樣,差別只有
速度;`set_config.sh` 不會用整體容量估算擋住設定或保證模型一定能載入,能不能放得下仍以
啟動後 `nvidia-smi` 實測為準（CPU-MoE 題目的權重 / free-VRAM 推薦區間只供參考）。

**RAM 有**。四個 server 同時跑,每顆模型「沒放進 VRAM 的那一部分」都必須放得進 RAM:

```text
RAM 低標 ≈ Σ(每顆模型的 GGUF 大小 − 該模型放進 VRAM 的部分) + 數 GB 系統餘裕
```

llama.cpp 的模型載入預設是 `--load-mode auto`;裝置支援 mmap 時會以 mmap 讀 GGUF。
走 mmap 路徑時,RAM 壓力未必會在載入時直接報錯,反而可能反覆從磁碟 page-in,
症狀變成「跑得動、但慢到不像話」——很容易被誤判成模型或設定有問題。用
`--cpu-moe` / `--n-cpu-moe` 把 experts 留在 RAM 時尤其要算清楚:那等於把幾乎整個模型
搬進 RAM。確認方式是啟動後看 `free -g` 與 `ps -o rss= -p <pid>`,不要只看 `nvidia-smi`。

---

## 3. 設定與啟動:`./set_config.sh` + `~/start.sh`

`~/.config` 下的實際檔案是**每台機器的 local state，不進 repo**。設定契約的單一來源是
`set_config.sh`、`deployment_profile.py` 的封閉 schema / 安全預設，以及本節的客戶端
範本；使用者不需要取得維護者的 dotfiles。照本節執行會依自己的模型、GPU 與 Python
產生一套相容設定，而不是複製維護者的私有路徑或 UUID。

正常路徑只讀 `~/.config/codetrail/`。runtime 永遠不讀、不寫、不刪舊世代前端的設定
(見「從舊世代前端升級」那一段,程序在 [docs/troubleshooting.md](docs/troubleshooting.md))。

### 3.1 `./set_config.sh` 做什麼

**純問答式設定**:每一題由你作答,工具不提供預設值,也**不用估算擋你的輸入**——它只驗證輸入在畫面列出的合法範圍(例如選項只有 1/2 卻輸入 3 會重問),以及做結構性檢查(binary 旗標、模型齊全性、schema)。部分數值題會附一句方向(越大越吃什麼)與**推薦值 / 推薦區間**,但推薦不是限制;只要仍在合法輸入範圍內,推薦區間外也照樣接受。VRAM 塞不塞得下仍以啟動後 `nvidia-smi` 實測為準。在 `<CODETRAIL_REPO>` 執行 `./set_config.sh`,它會依序:

1. **前置檢查**:Python 依賴(mcp/numpy/requests)、`tmux`、`nvidia-smi`、`llama-server` 是否存在且支援必要旗標(`--reranking` / `--mmproj` / `--fit` / `--cache-ram`;CPU-MoE 另需 `--cpu-moe` / `--n-cpu-moe`)。缺什麼直接在這一步就擋下並給**可複製的修復指令**(裝哪個套件、跑哪行 build),不會讓你答完所有問題才發現要重來;`llama-server` 因動態庫(如 CUDA lib)跑不起來時,會轉述原始錯誤並指向 `LD_LIBRARY_PATH`,不會誤報成「不支援旗標」。
2. **偵測**:GPU 種類/VRAM、`~/models` 的 GGUF 自動分類成主聊天 / embedding / reranker / VL+mmproj 四類;多 shard 自動聚合並**驗證齊全性**(缺片直接列出檔名)。有 mmproj 的 VL 模型不會被排進 main 清單前面;四類缺一即在初步判定硬停。
3. **互動問答(使用者選擇必答)**:**一個角色問完才換下一個**,每組先列出偵測結果再提問(列出候選,輸入編號或直接貼 .gguf 路徑;**只有一個候選時自動選用**,單卡時 GPU 也自動):

   | 組 | 題目 |
   |---|---|
   | `[1/5]` 主聊天模型 | 模型 → GPU → `ctx` → **CPU-MoE 層數** |
   | `[2/5]` embedding | 模型 → GPU |
   | `[3/5]` reranker | 模型 → GPU → **internal buffer(`-c/-b/-ub`)** |
   | `[4/5]` VL | 模型 → GPU → mmproj → **CPU-MoE 層數** |
   | `[5/5]` 壓縮模式 🧪 | `codetrail` / `manual` / `off`(見 [docs/compaction-rules.md](docs/compaction-rules.md);前兩者仍在測試階段) |

   **`[5/5]` 壓縮模式**決定客戶端什麼時候把長對話換成一段結構化摘要。**🧪 `codetrail` / `manual` 是實驗功能(開發中、仍在測試階段):摘要規則與觸發門檻可能再變。沒有 `~/.config/codetrail/client.json` 就等於沒有接管,`git pull` 不會自己啟用。** `codetrail` 的觸發點在「助理答完、對話進 idle」之後,所以摘要不會插在你的問題前面;`manual` 用同一套規則但只在你按 `/compact` 時執行;`off` 完全不壓縮——context 滿了會是一個**可見的錯誤**,不會自動補救。三種模式都附帶兩個「一路上少放一點進 context」的措施:**舊回合的 assistant reasoning 不送進模型**(最新一則問題之後的照留;要關掉在 `client.json` 設 `"keep_historical_reasoning": true`)、以及**舊工具輸出剪枝**(很舊的工具結果在模型視野裡換成一行,session 檔與畫面上的原文不動)。兩者與代價見 [docs/compaction-rules.md §6](docs/compaction-rules.md)。這一題沒有預設值。**非互動**用 `--compaction-mode {codetrail,manual,off}`;`--yes` 沒給這個旗標時沿用 `client.json` 記錄的選擇,**還沒選過就完全不碰壓縮設定**。

   **CPU-MoE 沒有 y/n 分流**:直接問「幾層 experts 留 RAM」,**`0` = 不 offload(experts 全留 GPU)**、`N` = 前 N 層留 RAM(`--n-cpu-moe N`)、輸入 **≥ 層數上限 = 全部留 RAM**(等同 `--cpu-moe`)。提示只有兩行:**數值越大 GPU 負載越低**,以及一個**推薦區間**——下界是權重剛好放得進這顆 GPU 目前 free VRAM 的層數、上界是全部移到 RAM(例如 `推薦數值:38-43`)。這個估算只算 GGUF 權重,沒有 KV cache / compute buffer / 共卡的附屬服務,所以是起點而不是保證。工具讀 GGUF tensor table 判斷:**不是 MoE(沒有 expert tensors)就不問**,並印出原因(dense 模型 offload 幾層都沒有意義)。main 與 VL 各問一次;embedding / reranker 永遠不套用。**VL 一旦套用 CPU-MoE,llama.cpp 的 `--fit` 就會失效**(它見到 tensor override 已被設定就直接放棄),所以工具會改寫 `-ngl 99 --fit off` 而不是假裝有 `--fit-target` 保護——這種情況沒有自動退讓的安全網,層數填太低會 OOM。

   `threads` **從頭到尾不問**——大部分人也不知道該填多少,所以預設就是 auto:不寫 `-t`,由 llama.cpp 自己偵測(hybrid CPU 只算 P-core,否則用實體核心數、排除 HT siblings),比工具自己數邏輯 CPU 準。真的要釘死才用進階旗標 `--threads N`。工具只驗證輸入範圍(上下限顯示成 `1024-1048576` 這種形式),**推薦值不會擋你**;三個附屬服務固定單 slot,最後啟動的 VL 用 `-ngl auto --fit on --fit-target 3072` 依 embedding/reranker 的實際占用自動配置。答完顯示**設定摘要一頁**:按 **Enter 寫入**;**q** 離開不寫檔。
4. **產生四個 runtime 檔案與一個還原 manifest**（選了壓縮模式時再多一份 owner-only 的
   壓縮狀態檔；runtime 檔採 transaction 寫入：要嘛
   全套完成、要嘛完全不動；既有檔自動備份 `*.bak-setconfig-<時間戳>`，
   `--restore-last-backup` 可整批還原）：

| 產物 | 內容 |
|---|---|
| `~/.config/codetrail/models.json` | 主模型 registry key → GGUF 路徑(合併既有內容) |
| `~/.config/codetrail/deployment.json` | deployment profile local override:四個 role 的模型、GPU(`services.<role>.gpu`)、主模型參數與驗證過的 `llama_bin`(全部來自你的作答);重跑時**保留你手動加的取樣參數**(temperature/top-p/…與 no_mmap),其他未涵蓋鍵會警告已捨棄 |
| `~/.config/codetrail/client.json` | 壓縮模式與權限覆寫（mode `0600`）；**這一題有明確答案時才寫**，沒有這個檔就等於沒有接管 |
| `~/start.sh` | 啟動腳本:把子命令與旗標原樣轉給 `scripts/launch_servers.py` / `stop_servers.py` / `check_status.py`;支援 `status` / `stop` / `logs` / `help` 子命令,打錯子命令會提示而不是誤啟動。它**不 export、不 unset 任何變數** —— GPU、主模型與驗證過的 llama-server 路徑都寫在 `deployment.json` |
| `~/.config/codetrail/setconfig-last-transaction.json` | 只記最近一次 transaction 實際包含的 runtime 檔案，供 `--restore-last-backup` 整批還原；不是另一份設定來源 |

結尾會自動印出**啟動參數**(四個 server 各自完整的 `llama-server` 指令,即 `~/start.sh --dry-run` 的輸出),並標明目前只完成「第 1 層:設定檔驗證」—— 模型能否真的載入,以 `~/start.sh` 實際啟動為準;`~/start.sh` 啟動完成的最後一行也會提醒你用 `nvidia-smi` 稍微監控 GPU/VRAM(例如 `watch -n 1 nvidia-smi`),因為 set_config 不做整體 VRAM 可行性判定,也不會拿容量估算保證一定能啟動。若偵測到 CodeTrail server 正在執行,會提醒(並可選擇自動)重啟才生效。

非互動用法(自動化 / 重跑)是 `./set_config.sh --yes`。它會跳過提問與確認頁，
但**所有使用者選擇題的值必須由旗標提供，缺哪個就報錯**（`--compaction-mode`
是唯一的例外，見下方最後一項）：

- 模型 / GPU：`--main-model` / `--main-gpu`、`--embed-model` / `--embed-gpu`、
  `--rerank-model` / `--rerank-gpu`、`--vl-model` / `--vl-gpu`。模型與 GPU 的編號
  **都從 1 起算**(GPU 編號 = `nvidia-smi` index + 1;互動選單與每張卡的描述行都會
  印出對應的 nvidia-smi index)。VL 配對不唯一時再給 `--vl-mmproj`；單一候選、單卡或
  唯一 mmproj 會自動選用。
- 數值：`--ctx` 與 `--rerank-ctx`。`--threads` 是非必要的進階旗標；不給就是
  auto，不寫 `-t`。
- MoE：main 使用 `--cpu-moe` / `--no-cpu-moe` / `--n-cpu-moe N`；VL 使用
  `--vl-cpu-moe` / `--no-vl-cpu-moe` / `--vl-n-cpu-moe N`。`N=0` 等同不 offload。
- 網路：`--allow-remote` 才會開放區網連線；未指定只綁 `127.0.0.1`。
- 路徑(選填,不給就用預設):`--llama-bin <路徑>` 指定 llama-server 執行檔(會轉成
  絕對路徑寫進 `deployment.json` 的 `llama_bin`;預設沿用該檔既有的值,再退回
  `~/llama.cpp/build/bin/llama-server`)、`--models-dir <目錄>` 指定要掃的 GGUF 目錄
  (預設 `~/models`)。兩個都寫進設定檔,沒有等價的環境變數。
- 壓縮模式：`--compaction-mode {codetrail,manual,off}`。**這一項不給不會報錯**——
  沒給時沿用 `~/.config/codetrail/client.json` 記錄的既有選擇，這台機器還沒選過
  就不寫 `client.json`。理由是「沒有那個檔＝沒有接管」是安全預設：舊的 `--yes`
  自動化腳本重跑一次，不該因此突然開啟自動壓縮。

重跑**不沿用舊選擇**（唯一例外是上面那條 `--compaction-mode`，它沿用狀態檔記錄的
模式）；其餘每次設定都來自本次作答 / 旗標。只有你手動加進 `deployment.json` 的取樣
參數與 port / base_url 會保留。完整旗標見 `./set_config.sh --help`。

既有安裝在 `git pull` 後不會直接覆寫 home dotfiles。要確認舊 local state 仍與新版 repo
相容，先在 `<CODETRAIL_REPO>` 跑以下唯讀檢查：

```bash
python3 deployment_profile.py validate
~/start.sh --dry-run
```

第一條應顯示 profile `valid`;dry-run 應正常列出四個 server command。兩項都符合，就不必
只因檔案日期較舊而重建。（**本版新增** `deployment.json` 的 `llama_bin` 與
`services.<role>.gpu` 兩種鍵:同一台機器上如果還有更舊的 checkout 共用
`~/.config/codetrail/`，它的 loader 會對這兩個未知鍵 fail-loud —— 那是刻意的,不是設定壞掉。）

若檢查要求補新欄位、Python / llama-server 路徑已換、模型 / GPU / 主 n_ctx 要改，才重跑
`./set_config.sh`。重跑會重新詢問硬體選擇；先記下現值或用 `~/start.sh --dry-run` 留存摘要，
不要假設它會沿用上一次答案。

### 3.2 啟動與停止

```bash
~/start.sh              # 啟動 main + embedding + reranker + VL(各自 tmux 視窗,驗 /health 才算 ready)
~/start.sh --dry-run    # 只印出將執行的四條 llama-server 指令,不啟動
~/start.sh status       # 檢查四個 server 狀態(= scripts/check_status.py)
~/start.sh stop         # 關閉全部並等到 VRAM 釋放完畢(主模型 + 三附屬模型;= scripts/stop_servers.py)
~/start.sh logs vl      # 看該 role 的 server log(加 -f 持續追蹤,如 logs main -f)
~/start.sh help         # 子命令說明(打錯子命令會提示,不會誤觸啟動)
```

啟動時的行為(對剛接觸專案者友善):

- **server log 從第一個 byte 就持續寫入** `~/.local/state/codetrail/logs/<role>.log`:launcher 先開好 tmux 視窗、接上 log 管線,才把 llama-server 放進去跑,所以即使因參數或模型錯誤**秒退**,完整錯誤也已在檔案裡;視窗本身也會帶著 exit code 留在原地(remain-on-exit)供檢視,`~/start.sh logs <role>` 直接看。
- **載入進度**:大模型載入要幾分鐘,等待期間每 15 秒回報「載入中,已等待 N 秒(process 存活)」,不會看起來像當機;health 等待上限依主模型大小自動放大。llama-server process 一死就立即失敗,不會空等 timeout。
- **失敗自動清理**:某個 role 啟動失敗時,launcher 自動關閉本次啟動的其他服務並釋放 port,然後告訴你「修正後直接重跑 `~/start.sh`」—— 不會留下半套 tmux 讓下次啟動卡 `session already exist`(要保留現場除錯:`~/start.sh --keep-on-failure`)。
- **綁定**:預設四個 server 只綁 `127.0.0.1`;`--allow-remote` 設定過的才綁 `0.0.0.0`。

| 預設 port | 角色 | 必要 |
|---|---|---|
| 8080 | main(聊天、推理、工具呼叫) | 是 |
| 8081 | embedding(算向量,RAG 搜相似段落) | 是 |
| 8082 | reranker(RAG 結果重排) | 是 |
| 8083 | VL(看截圖 / 圖片) | 是 |

會分四個 `llama-server` 是因為它一次只能載一顆 GGUF,不同角色用不同模式(`--jinja` / `--embedding --pooling cls` / `--embedding --pooling rank --reranking` / `--mmproj`)。`aicode` / `mcp_server.py` 都會硬性檢查三顆副模型已 ready。

只重啟部分角色:`~/start.sh stop --scope aux` + `~/start.sh --scope aux`(只動三顆附屬、不重載主模型),或 `~/start.sh --scope main`(只起主模型)。

要調整行為就加旗標(`~/start.sh` 原樣轉發,**沒有等價的環境變數**):

| 旗標 | 作用 |
|---|---|
| `--keep-on-failure` | 啟動失敗時不自動清理,保留現場除錯 |
| `--health-timeout N` | 覆寫 health 等待秒數(不給就依主模型大小自動放大) |
| `stop --timeout N` | 等 process 退出 / VRAM 釋放的秒數上限(預設 120) |
| `status --expected N` | 預期的 llama-server 數量(預設 4);`--strict` 才用 exit code 擋 |
| `--main-model` / `--main-gpu` / `--aux-gpu` / `--embed-gpu` / `--rerank-gpu` / `--vl-gpu` / `--llama-bin` / `--profile` | 一次性覆寫 `deployment.json` 的對應值(見 §4.1) |

> **tmux 你會用到的 4 個指令**(其他都不用學):
> - `Ctrl-b d` —— 把目前 session 放背景,回到原本 shell
> - `tmux ls` —— 列出所有背景 session
> - `tmux a -t <名字>` —— 接回去看某個 session 的即時 log
> - `Ctrl-b n` —— 同 session 內切換 window(RAG session 內含 embed / rerank / vl 三個 window)
>
> (關 server 不用學 tmux 指令,直接 `~/start.sh stop`。)

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
~/start.sh status

# CI / 自動化需要用 exit code 擋下時:
~/start.sh status --strict
```

`~/start.sh status` 會把 `nvidia-smi` PID 與 `/proc/<PID>/cmdline` 的 `--port` 對上有效 profile,逐 role 顯示 PID、GPU UUID、model、`n_ctx`、health。預設 report-only,即使異常仍 exit 0;`--strict` 遇到缺 service、錯 GPU、錯 model、錯 ctx 或 unhealthy 就失敗。

之後要關掉全部:

```bash
~/start.sh stop
```

偵錯時要看 server log(平常不用):`tmux a -t codetrail-main` 或 `tmux a -t codetrail-rag`(rag 內按 `Ctrl-b n` 切 embed/rerank/vl window,看完 `Ctrl-b d` 退出)。

---

## 4. 手動設定(進階;`set_config.sh` 已自動涵蓋)

`./set_config.sh` 產生的就是本節這些檔案。手動微調、換機部署、或想理解機制時再看這節。

### 4.0 設定在哪裡

CodeTrail 的設定**只有三個來源,全部是檔案**。沒有第四個 —— 客戶端、MCP server
與啟動核心都不從環境變數取任何設定,殼層裡殘留的 `AICODE_*` / `AI_CODE_*` /
`CODETRAIL_*` 對它們一律無效(同一台機器有兩份安裝時,那正是「以為在跑 A、實際在
跑 B」的機制)。

| 來源 | 位置 | 放什麼 | 誰改 |
|---|---|---|---|
| repo 常數 | `config.py` | 所有使用者都該一致的值:context 門檻、輸出上限、逾時、取樣參數、工具預算 | 改 repo(所有人一起變) |
| 每台機器 | `~/.config/codetrail/deployment.json`＋`models.json` | server 與端點、GPU 擺位、主模型、n_ctx、model registry | `./set_config.sh` |
| 每個使用者 | `~/.config/codetrail/client.json` | 壓縮模式、工具權限覆寫、下面 §4.3 那組開關 | `./set_config.sh`(或自己編輯) |

行程之間一律用 **argv** 交接,不用環境變數:
`aicode` → `codetrail_chat.py` → `mcp_server --root/--readonly/--n-ctx`。
子行程的環境在交出去之前會把那三個前綴整組剝掉。

被分析的專案裡的 `.codetrail/` **只放輸出**(`lessons.md`、metrics、cache),
不放任何開關 —— 被分析的 repo 不可信。

**啟動核心走的是同一條線**(`~/start.sh` → `scripts/launch_servers.py` → tmux pane):
主模型、GPU、llama-server 路徑、tmux session 名、逾時與 rollback 都只來自
`deployment.json`、repo 常數與**旗標**;產生的 `~/start.sh` 一行 `export` / `unset`
都沒有。每個 pane 跑的是 `python3 deployment_profile.py exec <role> …`,由它算出
最終環境再 `exec` llama-server:CodeTrail 的三個前綴、llama.cpp 自己的
`LLAMA_ARG_*`,以及繼承來的 GPU 選擇一律剝掉,GPU 只由設定檔驗證過的值重新指定。
所以 tmux server 的全域環境、`.bashrc` 或另一份安裝的殼層殘留都影響不到啟動參數。

### 4.1 Deployment profile

四個 server 共用同一份嚴格 deployment profile(單一事實來源;`aicode`、doctor、啟動前 preflight、status 與所有 launcher 都讀它)。優先序固定為:

```text
launcher 旗標 > ~/.config/codetrail/deployment.json local override > 選用 profile > 安全相容預設
```

安全基底 `safe-defaults` 直接內建在 `deployment_profile.py`(不宣稱硬體的向下相容預設,含 port、base_url 與附屬模型預設);`set_config.sh` 產生的 `~/.config/codetrail/deployment.json` 疊在上面。要做一次性實驗設定,在 `deployment.json` 的 `profile` 欄位填一個絕對路徑 `.json` profile
(用 `"extends": "defaults"` 繼承基底);沒填時就是基底加上你的 local override。

手動啟動範例(等價於 `~/start.sh` 做的事):

```bash
cd <CODETRAIL_REPO>
# 啟動核心的介面就是這些旗標,值的來源是 deployment.json;沒有等價的環境變數。
python3 scripts/launch_servers.py --scope all --dry-run   # 先看最終參數;不啟動、不連網

# 一次性覆寫(不改設定檔):主模型與 GPU 都有自己的旗標
python3 scripts/launch_servers.py --scope all --dry-run \
    --main-model <CODE_MODEL> \
    --main-gpu <主模型_GPU_UUID_或_INDEX> \
    --aux-gpu <附屬模型_GPU_UUID_或_INDEX>   # --embed-gpu / --rerank-gpu / --vl-gpu 可個別指定

python3 scripts/launch_servers.py --scope all             # 啟動四個 tmux server,嚴格驗證 role / GPU / model / ctx / health
python3 scripts/check_status.py --strict
python3 scripts/doctor.py                                 # doctor 讀 ~/.config/codetrail/ 的設定,不吃環境變數
```

`~/.config/codetrail/deployment.json` 可持久做局部覆寫(`profile` 欄位維持 `set_config.sh` 寫入的 `defaults` 即可):

```json
{
  "schema_version": 1,
  "profile": "defaults",
  "llama_bin": "/absolute/path/to/llama-server",
  "services": {
    "main": { "model": "<CODE_MODEL>", "gpu": "<主模型_GPU_UUID_或_INDEX>" }
  }
}
```

頂層 `llama_bin` 是選填的 llama-server 執行檔絕對路徑(不填就是
`~/llama.cpp/build/bin/llama-server`);每個 service 的 `gpu` 也是選填的 GPU selector
(UUID 或 index,不填就不指定卡)。這兩個鍵就是舊版靠殼層變數傳的那兩件事。

所有 service 都有同級 `model`、`port`、`base_url`、`bind`(`local` 預設只綁 127.0.0.1 / `all-interfaces` 綁 0.0.0.0)、`gpu_role`、`gpu`、`ctx`、`batch`、`ubatch`、`parameters`;VL 另外有 `mmproj`。模型欄只接受 registry key 或 GGUF 絕對路徑,參數只接受 schema allowlist(含 embedding / reranker 專用的 `cache_ram` → `--cache-ram`，預設 `0`;main / vl 專用的 `cpu_moe` → `--cpu-moe` 與部分 offload 的 `n_cpu_moe` → `--n-cpu-moe`(同一 role 兩鍵互斥;embedding / reranker 一律拒絕),以及 `gpu_layers: "auto"`、`fit`、`fit_target`、`parallel`),沒有 raw shell `extra_args`;JSON 不會被 `source` / `eval`。schema 與 GPU precedence 詳見 [docs/deployment-profiles.md](docs/deployment-profiles.md)。可離線查看合併結果:

```bash
python3 deployment_profile.py show                        # 目前的有效設定
python3 deployment_profile.py --main-model <CODE_MODEL> show   # 只在這一次覆寫主模型
```

啟用 reranking 時，RAG 與 Code RAG 都只使用專用 reranker；服務缺席、逾時或回應無效即報錯。啟動前 preflight 仍要求 reranker ready。

`client.json` 的 `rerank_fallback_policy` 保留相容鍵名，但唯一合法值為 `"error"`。舊值 `"embedding"` / `"main_model"` 會在設定載入時報錯，請刪除該鍵或改成 `"error"`。repo 常數 `config.RERANK_FALLBACK_POLICY` 也只接受 `error`。明確關閉 reranking 的選項，以及依候選內容決定不需 rerank 的路徑仍照常運作。

**遠端模型端點需要顯式 opt-in(`client.json` 的 `model_remote_ok`)**:CodeTrail 對 llama-server 的所有呼叫(completion / chat / embedding / reranking / props / slots / health)在送出前都會檢查端點——loopback 無條件放行;base_url 指向非 loopback 的機器時,必須先在 `~/.config/codetrail/client.json` 設 `"model_remote_ok": true`,否則呼叫直接報錯(fail-loud,錯誤訊息會印那個鍵名)。這是刻意的安全預設:prompt 可能含 NDA 程式碼與文件內容,不能因為 profile 填了一個遠端 IP 就靜默外送。模型流量同時不讀環境 proxy(`trust_env=False`)、不跟隨任何 HTTP redirect(3xx 一律報錯)。KB chunk 脈絡生成(Contextual Retrieval)另有獨立的 `kb_context_remote_ok`,**兩個鍵不互通**:前者放行的是 prompt,後者等於整份文件離機。`python3 scripts/doctor.py` 會在啟動前檢查這條(非 loopback 端點 + 未 opt-in = FAIL)。

### 4.2 Model registry(短名稱 → GGUF 路徑)

讓 `deployment.json` 的 `main.model` 寫短名稱就能對應到實際 GGUF 路徑,不用每次打絕對路徑:

```bash
mkdir -p ~/.config/codetrail
cat > ~/.config/codetrail/models.json <<'EOF'
{
  "<CODE_MODEL>": "/absolute/path/to/main.gguf"
}
EOF
```

registry value 也可寫 `~`,loader 會展開並要求它解析成絕對 `.gguf` 路徑。多 shard 模型指向第一片即可。也可以跳過 registry 直接把 `services.main.model` 寫成 GGUF 絕對路徑,但 registry 比較好維護。附屬模型不需要 registry:`set_config.sh` 直接把絕對路徑寫進 deployment.json。

### 4.3 客戶端設定(`~/.config/codetrail/client.json`)

`./set_config.sh` 的第 5 題會寫這一份。它是**每個使用者**的開關集中地:

```json
{
  "schema": 1,
  "compaction_mode": "codetrail",
  "permission": {}
}
```

- `compaction_mode`:`codetrail`(助理答完、對話進 idle 之後自動壓縮)/
  `manual`(只有你按 `/compact`)/ `off`(完全不壓縮;context 滿了會是可見的錯誤)。
  **沒有這個檔就等於沒有接管**,客戶端退成 `manual` 並在啟動橫幅講明。
- `permission`:每個工具的核准覆寫(`allow` / `ask` / `deny`),不寫就用預設。

其餘的鍵都有預設值,`set_config.sh` 不會問、也不會刪掉你自己加的:

| 鍵 | 預設 | 作用 |
|---|---|---|
| `model_remote_ok` | `false` | 主模型端點非 loopback 時才放行送出 **prompt** |
| `kb_context_remote_ok` | `false` | KB chunk 脈絡生成非 loopback 時才放行送出**整份文件的窗**。與上面是**兩個鍵**:資料範圍不同的同意不得合併 |
| `external_import` / `external_import_roots` | `false` / `["~/Downloads", "/tmp"]` | 允許 `import_external_file`,以及允許的來源根目錄。開了之後每一次匯入**仍要人工核准** |
| `build_commands` | `false` | 把 make / cmake / ninja / meson / bazel 掛進 `run_command` 白名單。它們會跑專案內的 build script = 任意程式碼執行,所以只在分析自己的專案時開 |
| `rerank_fallback_policy` | `"error"` | 唯一合法值 `error`；專用 reranker 不可用即報錯 |
| `project_instructions` | `true` | 讀不讀被分析專案的 `AGENTS.md` 與 `.codetrail/lessons.md`。分析不信任 repo 時設 `false` |
| `objdump` | `""` | 反組譯用的 objdump 路徑(跨架構韌體時指定 binutils-`<triplet>`) |
| `h_lang` | `"c"` | `.h` 當 C 還是 C++ 解析(`c` / `cpp`) |
| `use_container` | `false` | 在容器裡跑 `run_command` |
| `show_reasoning` | `false` | `/thinking` 的**初始值**,只管畫面 |
| `keep_historical_reasoning` | `false` | 舊回合的 assistant reasoning 要不要送進模型。與上面是**兩個鍵**:`/thinking` 只改畫面,不得動這個 |

未知的鍵一律 fail-loud(拼錯不會靜默失效);布林鍵只收真的 `true` / `false`
(`"false"` 是一個非空字串,不是 false)。

檔案是 0600:它決定寫入工具要不要人工核准,能被別人改就等於能繞過核准。
位置只由 `HOME` 推導,**沒有覆寫變數** —— 一個環境變數就能把 `apply_patch`
從 ask 翻成 allow 的話,「每次寫檔都會問」就不成立了。

問答資料收集(data flywheel)**不是 client.json 的鍵,永久開啟、沒有開關**:
`query_knowledge` / `query_knowledge_strict` / `code_rag_search` 每一次的問答都 append 到
`~/.local/state/codetrail/data/<root 雜湊>/interactions.jsonl`(目錄 0700、檔 0600,
**絕不落進被分析的 repo**,也不出這台機器)。每一筆都帶完整的檢索路徑:候選、各階段分數、
gate / rerank / MMR 決策、最終 REF 與當時生效的設定,`data_flywheel.py trace` 可以攤開來看;
同目錄的 `snapshots/` 留著每一代 KB 與被引用原始檔的內容,重灌之後舊紀錄仍對得回原文。
要撈檔案就去那個目錄,`python3 <CODETRAIL_REPO>/data_flywheel.py where --root <專案>`
會印出確切位置。
唯一不寫的是 readonly 評測 session(canary / eval / replay)。舊版的 `collect_data` 鍵
留在檔裡會 fail-loud,拿掉即可。

#### 工具權限預設

唯讀工具直接執行;下面八個每次都會跳核准框,框裡**完整顯示參數**(含整份 patch):
`apply_patch`、`run_lint`、`run_command`、`remove_document`、`record_lesson`、
`review_figures`、`review_text`、`import_external_file`(框裡另外列出實際落點)。

評測與啟動抽查走的是另一條 policy(`--policy readonly`):凡是 `tools/list` 沒有
標 `readOnlyHint` 的工具一律 deny,而且 MCP server 那一層也會關掉寫入與執行
(`mcp_server --readonly`,一個 argv 旗標)——兩層都在,繞過一層仍然寫不進去,
而且殼層裡殘留的任何變數都翻不回來。

#### MCP 呼叫的 read timeout

每一次 MCP 呼叫的 read timeout 固定 **660 秒**(`config.MCP_CALL_TIMEOUT_SECONDS`),
呼叫端不能放寬。理由是 `ingest_document` 的內部上限是 600 秒:更短的 timeout 會讓
client 在 server 還在寫 `knowledge.json` 的時候放棄。到期時客戶端會送
`notifications/cancelled` 並在寬限期內等 server 回覆,寬限期過就 SIGTERM 該 MCP
instance(server 自己的 handler 會收乾淨 ingest 子行程)。

## 5. 自檢與啟動客戶端

### 5.1 跑 doctor 自檢

```bash
python3 scripts/doctor.py                                 # doctor 讀 ~/.config/codetrail/ 的設定,不吃環境變數
```

(doctor 讀 `~/.config/codetrail/` 的 deployment / models / client 三個檔;主模型設了哪顆看 `set_config.sh` 結尾的設定摘要,或 `~/.config/codetrail/models.json`)

預期結尾看到 `PASS=2x WARN=x FAIL=0`。常見可忽略的 WARN:

- `html2text 沒裝` —— 只有 RAG 抓網頁要,可忽略
- `knowledge.json 不存在` —— RAG 知識庫還沒建立,等用到再說

**有 FAIL 不要跳過**,通常是 PATH、server 沒啟動、GGUF 路徑寫錯。對應修法見 [docs/troubleshooting.md](docs/troubleshooting.md)。

### 5.2 啟動 TUI

切到你要分析或修改的專案目錄(**不要從 `$HOME` 或 `/` 啟動**,沙箱會拒絕):

```bash
cd <PROJECT_TO_ANALYZE>
aicode
```

`aicode` 不用帶參數,而且**只接受**三個:`-c` / `--continue`(接續這個專案最近一次的
對話)、`--session <id>`、`-h` / `--help`。沙箱 root 一律是目前目錄,主模型只來自
`deployment.json` 的 `main.model`(`set_config.sh` 已經設好)——沒有 `-m`、沒有 `--root`、
沒有任何環境變數可以改它。換模型 = 重跑 `./set_config.sh` 再重啟 server。

啟動前置(profile 驗證、主模型、n_ctx 觀測、ctx 容量閘、lessons、附屬 server、工具健檢)
**通過**時,對話區只留一行摘要、壓縮狀態行與警告(含工具健檢寫到 stderr 的行);逐項進度
的完整輸出留在 TUI 接管畫面**之前**的終端,往上捲就看得到。**失敗**時 `aicode` 不會進 TUI,
錯誤原樣留在終端。

接續舊對話時(`-c`、`--session <id>`,或在 TUI 內用 `/session`),畫面會把那一段
**原始記錄**重播出來:你問過的話、模型的回答與 thinking、每一次工具呼叫的參數、狀態與
結果。壓縮過的對話也一樣看得到壓縮**之前**的原文,摘要只在原文之後多一個可展開的
標記(模型看到的仍然是壓縮後的歷史 —— 畫面與模型視野是兩件事)。TUI 內的
`/sessions` 列出這個專案的既有對話、`/session` 開選單挑一段,細節見
[docs/basic-usage.md](docs/basic-usage.md#7-切換與接續對話)。

要讓模型讀專案外的附件(`~/Downloads` 的 log / 截圖 / spec)就多加一個開關:

在 `~/.config/codetrail/client.json` 設:

```json
{ "external_import": true, "external_import_roots": ["~/Downloads", "/tmp"] }
```

開了之後**每一次**匯入仍然要人工核准(核准框會顯示來源與目的路徑)。細節見
[docs/basic-usage.md](docs/basic-usage.md)。第一次先照上面最短的指令跑起來就好。

### 5.3 簡單測試

進到 TUI 後輸入:

```text
請用工具 list_dir 看當前目錄結構,挑出 entry point、主要模組和測試目錄,簡單整理。
```

模型應該會透過 CodeTrail MCP 呼叫 `list_dir` 讀真實目錄,然後回給你整理結果。工具名是**裸名**(沒有 `codetrail_` 前綴——那是舊世代前端加的)。

第一個請求**首字延遲(TTFT)**:

- 用 `--no-mmap` 模式:約 5–15 秒
- 沒設定 `no_mmap`:CodeTrail 會保留 llama.cpp 的預設 `--load-mode auto`;在支援 mmap 的裝置通常會走 mmap 路徑,**第一次可能要 1–2 分鐘**,因為要從 SSD page-in MoE expert weights。畫面上 frontend 可能顯示「`...esc interrupt`」或類似等待狀態,**不要按 Esc**,等就對了

要改成 `--no-mmap`,是在 `~/.config/codetrail/deployment.json` 的 `services.<main|vl>.parameters` 加 `"no_mmap": true`(不是手動改 llama-server 指令 —— argv 每次由 deployment 重新產生)。CodeTrail **不替你決定**這一項,但套了 CPU-MoE 卻沒設時 `set_config.sh` 會警告,而且重跑會保留你的設定。詳見 [docs/troubleshooting.md](docs/troubleshooting.md)。

如果想驗證 MCP transport 有沒有連上:客戶端輸入 `/tools`,應列出 21 個工具。**列得出來只代表 MCP 子行程完成連線,不代表模型在這一輪真的發出 tool call。** 真正執行時畫面上會有一行 `· list_dir(path=.) → completed`;若模型只印出 `<list_dir .../>` 再用文字宣稱成功,那是假工具呼叫 —— 客戶端會偵測到並提示一次,細節照 [troubleshooting 的分層檢查](docs/troubleshooting.md#mcp-connected-but-no-tool-call)處理。

要做「分析、解釋、推導、找原因」時，優先用同一個既有工具的 bounded context 模式：

```text
請用工具 code_rag_search,mode 設 "context",
query 寫 "ISR event never reaching the idle state in sm_transition",
省略 max_chars，依 evidence 的 path:line 分成已證實與仍不確定兩部分回答。
```

**`query` 要寫成一句自然的英文描述,並放進有辨識度的 identifier / 縮寫。** 索引是拿原始碼算的
embedding,查詢跟程式碼同語言時召回率差很多。33 萬符號的真實樹實測,同一個問題三種寫法:

| query 寫法 | 結果 |
|---|---|
| 中文「從設定檔讀 target 的地方」 | 正確答案排 4429 名,候選池收不到 |
| 關鍵字堆 `read target from configuration file: tcf, config parse, properties` | 回一串叫 `read` 的無關符號(裸單字 `read` 在語料裡是 54 個 symbol 的名字,exact-symbol 命中把候選池洗掉了) |
| 自然英文句 `tcf tool configuration file parsing for target core properties` | top-5 有 4 筆是正確答案 |

你仍然可以用中文跟模型對話；`code_rag_search.query` 要用英文時，像上面的範例直接在當次
問題要求模型翻成自然英文即可。不要為了這件事把整段 query 教學複製進每輪載入的
`~/.config/codetrail/instructions.md`。

`mode="context"` 會把 semantic seeds、確定的 1-hop caller/callee/include，以及相關
test/header/config/trace lexical evidence 合併去重後裝進 bounded budget。MCP 省略
`max_chars` 時，結果使用**呼叫當下** `n_ctx` 的 12% token 代理預算；不是固定 12,000。
明示 `max_chars` 的合法範圍是 `2000..30000`，超過預設 12% 時結果會標
`context_risk`。`used_chars` 仍只計 `evidence[].text` 的實際字元，不宣稱 tokenizer token
數；direct/core Python API 為相容舊呼叫才保留 12,000 字元 fallback。歧義與 unresolved
只進 `uncertainties`，不偽裝成確定證據。candidate 數量、graph traversal 與字元 budget
的截斷原因會分開標示。所有 source window 仍由既有 sandboxed `read_file` 路徑讀取。
graph 尚未建立或損壞時，lexical（grep / index）候選仍會參與選取，實際 evidence 仍受既有
candidate 與字元 budget 約束；只有呼叫關係證據缺席，`graph_status` 標示原因，
`uncertainties` 會列出 `呼叫關係證據不可用（relationship evidence unavailable: graph unavailable）；未看到 caller/callee 不代表不存在`（graph 查詢途中出錯時
`graph unavailable` 改為 `graph degraded`）。parser / rg 等必要依賴失敗則整次回錯誤，不能當成證據缺席。

所有 MCP 工具的精簡文字結果第一行固定是 `status: ok|partial|error`；只有 partial/error
或截斷時才接可操作的 `next:`。省略結果上限時都依上述 12% 動態預算，並受各工具既有
safety cap 約束。`read_file` 的 `next:` 會給不漏行的下一個 `start_line`；完整 renderer
與 structured evidence 保留規則見 [MCP 工具清單](docs/mcp-tools.md#結果文字與預算契約)。

想看**跨檔案的呼叫關係**(誰呼叫誰、include 鏈),`code_rag_search` 除了語意搜尋還有 graph 模式:

```text
請用工具 code_rag_search,mode 設 "path",query 寫 "main -> sm_transition",
把呼叫鏈每一步的檔案與行號列出來。
```

graph 會保守解析 C/C++(tree-sitter)與 Python 的 definitions / includes / calls。
只有能由可見性、linkage、qualified identity 與 preprocessor condition 支持的關係才當成
confirmed；同名歧義、function pointer、macro 間接呼叫與條件不足的候選會留在
unresolved / uncertainty，不會硬接成呼叫鏈。首次使用 graph 模式要顯式建立 DB；尚未
建立時的錯誤會附上含實際 interpreter、CodeTrail 路徑與專案 root 的可複製命令。建好後
查詢會偵測變更並選擇增量或完整重建。使用原則：graph 可用時先查 `neighbors`；`graph_status`
為 unavailable 時改用 `mode="context"` / `grep_code`，並把 caller coverage 標為不完整——
不能因為沒看到呼叫者就推論沒有呼叫者。完整 symbol 範圍、header visibility、條件式 include、
response budget 與 schema 說明集中在 [MCP 工具清單](docs/mcp-tools.md)，不在 README 重複維護。

想把**圖片**(截圖、架構圖、規格頁掃描)變成之後查得到的知識,就是「VL + RAG 一起用」—— `ingest_document` 餵圖片時會自動走 VL 把圖抽成文字再進 RAG,跟 PDF 走同一套:

```text
請用工具 ingest_document 匯入 docs/diagram.png,
完成後 reload_knowledge_base,
再用 query_knowledge 查這張圖的重點,回答附 REF。
```

(圖片附件需要 VL server :8083 已啟動。聊天截圖模式、外部圖片匯入、binary/ELF 等完整串接見 [docs/rag.md](docs/rag.md)。)

**PDF 裡的表格 / 終端機畫面**(datasheet、register map、log)另外走一條結構化路徑。圖多的
PDF 先估成本,再入庫,最後覆核:

```text
請用工具 ingest_document 匯入 docs/datasheet.pdf,preflight_only 設 True,
回報候選數、VL 呼叫次數與有沒有超過上限。
```

```text
沒超過的話,請用工具 ingest_document 匯入 docs/datasheet.pdf,
完成後用工具 review_figures,action 設 "list",列出待覆核的圖與原因。
```

preflight 零寫入;它會估算所有結構化候選，包含純 raster 的分類與雙樣本抽取。
沒被收成候選的區域不進預算(它們不會被送出去),報告改在「不會進 KB 的頁 / 區域」
那一段逐筆列出。
原生解析失敗、VL 抽取失敗、轉錄回退與未處理區域分開記錄；VL 可用不代表全 PDF OCR 完整。
程式碼與檔案樹保留符號、縮排和母子關係；章節／圖表目錄只排除確定的導覽行，正文仍可檢索。
品質分為可用、僅排版差異、部分可用、結構錯誤、不可用及未知，人工確認另列。
已知缺字或衝突列為需修復；重要結構錯誤與全不可讀結果不作為新 figure 入庫，原文與 artifacts 保留。
入庫後,**能以獨立證據確認的**表格才會被 `query_knowledge_strict` 拿來回答數值;不能確認的
會標成待覆核並在回傳的 `excluded_figures` 裡列出頁碼與原因(不是「查不到」)。
`review_figures(action="fix", ..., confirm_against_image=True)` 是人工覆核入口,permission 設
`ask`。純 raster 的掃描頁表格、終端機截圖與 diagram 也會保存原圖、bbox、輸入變體與
`▯` / 逐格或逐行證據，並出現在 `review_figures` 裡。完整說明見
[docs/rag.md](docs/rag.md#pdf-內的表格與終端機畫面結構化抽取--人工覆核)。

跨整節的問題現在有**章節召回**補充：每節以標題與整節正文建立一個檢索點，
命中後把該節所有 chunks 併入候選並去重，再逐 chunk 過原本的證據門檻與本地 reranker。
長節分窗涵蓋全文，不截掉 6000 字之後；節點分數只用於召回，不是 strict 的證據分數。
既有 KB 由已存的 chunk 章節資料重建索引，**不用重新解析 PDF**；首次載入舊 cache
需要本地 embedding server 重算向量。缺少原始節界的舊資料只能依保留的標題序列復原。

也可明確選用**本地 MinerU 文字 lane**：把 flat `content_list.json` 放進專案，
用 `ingest_document` 同時提供 `mineru_content_list` 與**產物生成時記錄的**
`mineru_pdf_sha256`。程式只轉換現成產物，不啟動 MinerU；只認 `text_level` 標題，
圖表依頁碼與 bbox 配對。表格仍由既有 structured lane 收錄，沒有唯一 owner 就報錯。
MinerU 文字預設屬未驗證 OCR，normal 查詢標示來源，strict 排除並回報 `excluded_text`；
用 `review_text` 校字並對照來源確認目前版本後，只有來源、內容版本與品質皆有效的段落可進 strict。
未表示頁與既有品質問題仍會揭露。參數與 CLI 用法見
[MinerU 文字 lane](docs/mcp-tools.md#本地-mineru-文字-lane)。

**知識庫只有 `knowledge.json` 一個檔要管。** 向量是它衍生出來的 cache(藏在
`.codetrail/cache/embeddings/`),缺了會自動重建、身分對不上一律丟棄重建、重建不了就
**中止查詢而不是拿舊向量湊合**。備份 / 複製 / 刪除知識庫只要動 `knowledge.json`;刪掉它
就是空知識庫,旁邊不會留下一份舊向量。要把 KB 重建成只有某一份文件,用
`ingest_document(path, fresh=True)`(CLI 是 `--fresh`)——它**不會**刪
`.codetrail/figures/` 的 artifact 檔案;同一份文件再 ingest 時人工修正會沿用,但
**被移出 KB 的其他文件之後重新 ingest 不會自動恢復人工確認**。細節見
[docs/rag.md](docs/rag.md#只有-knowledgejson-要管)。

更多操作模式(夾帶附件、注入 RAG、查 spec)見 [docs/basic-usage.md](docs/basic-usage.md);完整 21 個工具清單見 [docs/mcp-tools.md](docs/mcp-tools.md);被你糾正過的行為怎麼變成之後 session 都遵守的規則,見 [docs/lessons.md](docs/lessons.md)。

## 文件地圖

| 文件 | 內容 |
|---|---|
| [docs/setup.md](docs/setup.md) | 替代安裝方式、進階配置、換機部署 reference |
| [docs/deployment-profiles.md](docs/deployment-profiles.md) | profile schema、precedence、GPU override 與 local override |
| [docs/split-deployment.md](docs/split-deployment.md) | A 跑四類模型、B 跑 aicode／MCP／build；端點授權、模型身分、診斷與評測 |
| [docs/basic-usage.md](docs/basic-usage.md) | TUI 內常用操作:正常對話、夾帶附件、RAG 注入、最小驗收流程 |
| [docs/message-queue.md](docs/message-queue.md) | 排到下一輪或補充目前任務；查看、修改、取消、送達狀態與安全步驟 |
| [docs/rag.md](docs/rag.md) | 讀檔、匯入附件(PDF / 圖片經 VL)、建立知識庫、圖片+RAG 一起用、查 spec |
| [docs/ingest-resume.md](docs/ingest-resume.md) | 中斷續跑、指定頁面／圖表／失敗項目重做、快取有效性與缺漏報告 |
| [docs/text-and-table-review.md](docs/text-and-table-review.md) | `query_table` 精確查值、`review_text` 正文校字與版本確認、strict 採用條件 |
| [docs/memory-consistency.md](docs/memory-consistency.md) | ELF、GNU／MetaWare map、linker script、preload 與 DRAM 區間核對；缺資料標未知 |
| [docs/build-context.md](docs/build-context.md) | 匯入編譯資料；依 target、巨集、include 與 generated headers 搜尋及分析關係 |
| [docs/mcp-tools.md](docs/mcp-tools.md) | CodeTrail 暴露的 21 個 MCP 工具與使用原則 |
| [docs/lessons.md](docs/lessons.md) | lessons(行為教訓):糾正 → 提案 → 核准 → 注入 → 過期複審的完整生命週期與管理指令 |
| [docs/security.md](docs/security.md) | 沙箱邊界、工具權限、外部匯入與 NDA 資料注意事項 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | `/status` / `/mcp`、ctx-safety、server 不可連、Blackwell CUDA、MoE 首字慢 |
| [README_DEV.md](README_DEV.md) | 開發者維護命令、測試、eval、context gate 設計 |
| [AGENTS.md](AGENTS.md) | AI coding agent 修改本 repo 時必讀的安全規範 |
| [RESPONSIBLE_USE.md](RESPONSIBLE_USE.md) | 授權範圍之外的負責任操作、資料邊界與人工驗證建議 |
| [DISCLAIMER.md](DISCLAIMER.md) | 保固、資安審計、AI 輸出、機密資料與第三方權利免責 |

---

## License

本專案以 [MIT License](LICENSE) 授權。MIT 權利與免責條款以 `LICENSE` 為準；
[Disclaimer](DISCLAIMER.md) 是補充說明，不修改授權條件。

## Responsible use

只處理你有權存取的程式碼、文件、模型與設備；NDA 場景要核對 effective endpoint、
provider、permission、project config 與衍生檔案保存位置。模型回答、RAG 引用、圖片辨識、
patch 與命令都必須由人審核，不能把 strict mode 或 sandbox 當成正確性／合規保證。

完整操作原則見 [Responsible Use](RESPONSIBLE_USE.md)；保固、責任、資安審計與第三方
權利界線見 [Disclaimer](DISCLAIMER.md)。
