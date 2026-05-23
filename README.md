# CodeTrail - OpenCode + llama.cpp 本地 MCP 工作台

CodeTrail 是一個給 OpenCode 使用的本地 MCP 後端。你在 OpenCode TUI 裡提問,模型可以透過 CodeTrail 讀專案、找程式碼、查已匯入的 spec、分析截圖或 binary、產生 patch,並在允許的白名單內跑驗證命令。

CodeTrail 目前定位是**成熟私有部署版**:適合本機、離線、NDA / firmware / private repo 分析;**不打算公開發布**成 PyPI package、Docker image 或 SaaS。安全邊界有自動測試保護,但未做公開產品級安全審計。

底層推理引擎使用 [llama.cpp](https://github.com/ggerganov/llama.cpp) `llama-server`(自己 build,需要 CUDA)。所有 LLM / embedding / reranker / VL 走它的 HTTP API。

讀完這份 README 從零走到能進 OpenCode TUI 跟模型對話,大約需要:
- 一次性安裝 + build:30–60 分鐘
- 下載模型 GGUF:依網速與所選模型,5 分鐘到數小時不等

### 完成標準

本 README 走完只要四件事都成立就算「完成」,任一步卡住就停在那一步排查,不要硬往下走:

1. `AICODE_MODEL=<CODE_MODEL> python scripts/doctor.py` 結尾印 `PASS=... WARN=... FAIL=0`
2. `cd <PROJECT_TO_ANALYZE> && aicode` 進得了 OpenCode TUI(看到綠色輸入框、沒有 fatal 訊息)
3. TUI 內輸入 `/status`,看到 `codetrail Connected`
4. Smoke test:輸入 §5.3 的範例 prompt,模型成功呼叫 `codetrail_list_dir` 並回傳真實目錄結果

任一步 FAIL 對應的修法見 [docs/troubleshooting.md](docs/troubleshooting.md)。

---

## 硬體與模型對照速查

`<CODE_MODEL>` 是 README / docs 全文使用的**佔位符**,代表你**自己選的主聊天 / 程式推導模型**。CodeTrail 不內建、不推薦、也不 fallback —— 你看完下表自己挑一顆 GGUF,後面所有步驟把 `<CODE_MODEL>` 換成你選的那顆。

下表以「**模型 weights 完整塞 VRAM,無 CPU offload**」為預設推薦。Pure GPU 路徑速度最快、最穩定,是絕大多數情境的正確選擇。要跑超過 VRAM 容量的模型(例如 235B / Llama 4 Scout 等),走下方「CPU offload 例外情形」。

| 你的 VRAM | 2025–2026 代表卡 | 純 GPU 模型範例(完整塞 VRAM) |
|---|---|---|
| 96GB | RTX PRO 6000 Blackwell | Llama-3.3-70B Q8;Mistral Medium 3.5;Qwen3-Coder-Next(80B-A3B MoE)Q8;或多模型同卡並跑 |
| 48GB | RTX PRO 5000 Blackwell | 70B 級 dense Q4–Q5;**Qwen3-Coder-Next(80B-A3B MoE)Q4 整顆塞**(~44GB) |
| 32GB | **RTX 5090** / RTX PRO 4500 Blackwell | **Qwen3-32B Q5_K_M / Q6_K**;Gemma 4 26B-A4B Q8;Mistral Small 3 14B Q8(寬裕) |
| 24GB | RTX PRO 4000 Blackwell | Qwen3-32B Q4_K_M;Qwen3-14B Q8;Gemma 4 26B-A4B Q4 |
| 16GB | RTX 5080 / 5070 Ti / 5060 Ti 16GB | **Gemma 4 26B-A4B Q4**(MoE,~14GB);Qwen3-14B Q5;Mistral Small 3 14B Q5;Phi-4 14B Q4 |
| 12GB | RTX 5070 | Qwen3-8B Q5/Q6;Llama 3.x 8B Q5;Phi-4-mini Q8 |
| 8GB | RTX 5060 / RTX 5060 Ti 8GB | Qwen3-4B Q5/Q8;Phi-4-mini Q5;Gemma 3 4B Q5 |

> 全部模型啟動 server 時都加 `--jinja`(啟用模型內建 chat template,tool calling 才會走對格式);其餘 `--cpu-moe` / `--no-mmap` 等旗標僅在下方例外情形才用。

> **資訊統計日期:2026-05-23**。主流家族快照:Qwen3(2507 Instruct/Thinking 變體、Coder、Coder-Next 80B-A3B 走 sparse-MoE)、Qwen3.5(397B-A17B 旗艦 MoE)、Llama 4 Scout(17B 活躍 / 109B 總、10M ctx)/Maverick(17B / 400B、1M ctx)/Behemoth(~2T,訓練中尚未釋出 weights)、Gemma 4(26B-A4B MoE,~14GB GGUF)、Mistral Small 3(14B dense)/ Medium 3.5、DeepSeek V3.2(671B-MoE,Q4 ≈ 370GB)/ V4 Pro(1.6T-MoE)/ V4 Flash(284B-MoE)、GLM-4.6、Phi-4 / Phi-4-mini。本表只列以**單卡 pure GPU** 能跑的範例;超過 VRAM 容量者見下方例外段落,或考慮多卡 NVLink / 遠端 GPU 主機([docs/models.md](docs/models.md))。

### CPU offload(`--cpu-moe` / `--n-cpu-moe`)—— 例外情形,不是預設

只在以下三條件**同時**成立才考慮:

1. **模型必須是 MoE 架構**(每 token 只啟用部分 expert,例如 Qwen3-235B-A22B 每 token 只跑 22B / 235B = 9% 參數)。Dense 模型不要嘗試 offload —— 每 token 都要用全部參數,跨 PCIe / RAM bus 取值會嚴重吞速度。
2. **MoE 模型 Q4_K_M 體積 ≤ 你的 RAM 容量 − 15GB**(留給 OS / page cache)。否則 swap 一發、速度直接歸零。Q4_K_M 體積估算公式:`總參數(B) × 0.55 GB`。
3. **你接受生成速度從 pure GPU 的 ~40 tok/s 降到 6–15 tok/s**。Offload 路徑跑出來的東西可用,但延遲明顯高。

換言之,「VRAM 不夠就 offload 大模型」**只在 MoE 模型成立**,而且要看 RAM 多大。常見例子:

| MoE 模型 | Q4_K_M 體積 | 需要 RAM | 適合 VRAM 段 |
|---|---|---|---|
| Qwen3-235B-A22B-Instruct-2507 | ~135 GB | ≥ 150 GB | 32GB+(`--cpu-moe` 全 offload) |
| Llama 4 Scout(109B-MoE / 10M ctx) | ~60 GB | ≥ 80 GB | 32GB+ |
| Qwen3-Coder 480B-A35B-Instruct | ~265 GB | ≥ 280 GB | 48GB+(實質要 workstation 級 RAM) |
| Qwen3-30B-A3B(小型 MoE) | ~18 GB | ≥ 32 GB | 16GB(`--n-cpu-moe N` 部分 offload 留多數 expert 在 GPU) |
| Qwen3-32B(**dense**) | 19 GB | — | **不適用 offload**,直接選 24GB+ 卡 pure GPU |

下方 §3 走廊範例用 5090 32GB + Qwen3-235B-A22B-Instruct-2507(`--n-cpu-moe N --no-mmap`,N 依 VRAM 調整見 §3.1),屬於這裡的**例外路徑**(作者實測過所以拿來示範)。若你硬體 / 模型走的是上面正規 pure GPU 表的某一格,§3.1 server 指令把 `--n-cpu-moe` 與 `--no-mmap` 兩行拿掉即可。

完整 `--cpu-moe` vs `--n-cpu-moe`、mmap vs no-mmap、context / KV cache 量化、遠端 GPU 主機等深入內容見 [docs/models.md](docs/models.md)。

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

各套件對應後續哪一節用到:

- `build-essential` + `cmake` + `pkg-config` —— §1.5 build llama.cpp 必備,缺一不可
- `python3` (≥ 3.10) + `python3-venv` —— §1.3 建立 venv 安裝 CodeTrail Python deps;系統若 `python` 指令不存在,後續文件中的 `python` 自行改成 `python3`
- `git` —— clone llama.cpp / 一般版本控管
- `ripgrep` (`rg`) —— 加速程式碼搜尋(建議但非必要)
- `tmux` —— §3 用來在背景跑三個 llama-server,§3 開頭有 4 行操作教學

另外裝 **Node.js LTS + npm**(§1.2 裝 OpenCode 用)。Ubuntu 24.04 內建 nodejs 太舊,建議用 NodeSource 官方源裝 LTS:

```bash
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
sudo apt install -y nodejs
node -v && npm -v    # 確認 node ≥ 18 / 20 LTS、npm 可執行
```

已經有 nvm / fnm / volta 的用熟悉的方式裝 Node LTS 即可(版本 ≥ 18)。

### 1.2 安裝 OpenCode

```bash
npm install -g opencode-ai
command -v opencode    # 確認可被找到
```

### 1.3 安裝 CodeTrail Python 依賴

Ubuntu 24.04 啟用 PEP 668,system Python 不允許直接 `pip install`。**在 CodeTrail repo 內建一個 venv**,後續所有 Python 動作(`scripts/doctor.py`、`aicode` 啟動的 CodeTrail MCP server、§2.1 的 `hf` CLI)都跑在這個 venv 內:

```bash
cd <CODETRAIL_REPO>
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
pip install pymupdf4llm    # 選用:RAG 從 PDF 建知識庫才用;不做 RAG 可省略
```

`<CODETRAIL_REPO>` 是這個 CodeTrail 的 repo 路徑,不是你要分析的專案路徑。`requirements.txt` 已含 `mcp` / `requests` / `numpy`,不必再單獨 `pip install mcp`。

> **每次開新 shell 都要先 `source <CODETRAIL_REPO>/.venv/bin/activate`** 才能跑 `python scripts/doctor.py` 或 `aicode` —— `aicode` 內部用 PATH 上的 `python3` 拉起 MCP server,venv 沒啟用時會 `ModuleNotFoundError: No module named 'mcp'`。覺得每次手動 activate 太煩,把這行寫進 `~/.bashrc`(把 `<CODETRAIL_REPO>` 換成絕對路徑,例如 `$HOME/CodeTrail`):
>
> ```bash
> echo 'source <CODETRAIL_REPO>/.venv/bin/activate' >> ~/.bashrc
> ```
>
> §3 用 `tmux new -s ...` 開的新 session 是獨立的 shell —— 不過 §3 那三個 session 跑的是 `llama-server` 二進位、與 venv 無關,不必再 activate。`tmux` 主要影響的是 §5.1 / §5.2 那種需要 Python 的指令所在的 shell。

### 1.4 (僅 Blackwell GPU 需要)升級 CUDA Toolkit 到 13

Ubuntu 24.04 的 `nvidia-cuda-toolkit` 套件停在 CUDA **12.0**,**不認識 Blackwell 的 `sm_120` / `compute_120a`**。如果你用 RTX 50 系列(5070 / 5080 / 5090 / 6000 Ada Blackwell),build llama.cpp 時會看到:

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

固定 clone 到 `~/llama.cpp` —— §3 啟動 server 直接寫死這個路徑,放別處後面要逐行改:

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

---

## 2. 下載 GGUF

### 2.1 安裝 Hugging Face CLI + hf-transfer 加速

下載指令使用 Hugging Face 新版 `hf` CLI;`hf-transfer` 只負責加速下載,不提供 `hf` 命令本身。預設下載走單連線,實測 ~12 MB/s;裝 `hf-transfer` 後可以拉到 ~270 MB/s(視網路與 HF CDN 上限):

承 §1.3 venv 已啟用的狀態下,直接裝:

```bash
pip install -U "huggingface_hub[cli]" hf-transfer
command -v hf
python -c "import hf_transfer; print('hf-transfer', hf_transfer.__version__)"
```

如果 `command -v hf` 沒輸出,通常代表 venv 沒啟用 —— 回去執行 `source <CODETRAIL_REPO>/.venv/bin/activate` 再重試。

(若你刻意跳過 §1.3 venv,改成 `pip install --user --break-system-packages -U "huggingface_hub[cli]" hf-transfer`,會安裝到 `~/.local/bin`,需要 `~/.local/bin` 在 PATH 上。)

啟用方式:下載指令前面加 `HF_HUB_ENABLE_HF_TRANSFER=1`。

### 2.2 下載主聊天模型(`<CODE_MODEL>`)

下面用 5090 走廊的 Qwen3-235B-A22B-Instruct-2507 Q4_K_M 當範例。它在 HuggingFace 上是 **3 個分片檔,共 ~134GB**:

```bash
mkdir -p ~/models

HF_HUB_ENABLE_HF_TRANSFER=1 hf download \
  unsloth/Qwen3-235B-A22B-Instruct-2507-GGUF \
  --include "Q4_K_M/*" \
  --local-dir ~/models/Qwen3-235B-A22B-Instruct-2507-GGUF
```

下完之後會有:

```
~/models/Qwen3-235B-A22B-Instruct-2507-GGUF/Q4_K_M/
  Qwen3-235B-A22B-Instruct-2507-Q4_K_M-00001-of-00003.gguf
  Qwen3-235B-A22B-Instruct-2507-Q4_K_M-00002-of-00003.gguf
  Qwen3-235B-A22B-Instruct-2507-Q4_K_M-00003-of-00003.gguf
```

> 啟動 server 時 `-m` **只指 shard 1**,llama.cpp 會自動讀第 2、3 片。

如果你的硬體不是 5090、選了別的 `<CODE_MODEL>`,把上面的 repo 與 `--include` pattern 換成你要的那顆。常見社群 GGUF 來源:`unsloth/...-GGUF`、`bartowski/...-GGUF`、官方 `Qwen/...-GGUF`。

### 2.3 下載 RAG 附屬模型

CodeTrail 的 RAG / Code-RAG 內建固定使用 `bge-m3`(embedding)。`bge-reranker-v2-m3`(reranker)建議一起下載與啟動;沒啟動時 RAG 仍可用,會 fallback 到主模型做排序。這兩個體積很小:

```bash
# embedding:bge-m3 (用 f16,不要量化 — embedding 對量化敏感,Q4 會明顯影響召回)
HF_HUB_ENABLE_HF_TRANSFER=1 hf download \
  CompendiumLabs/bge-m3-gguf bge-m3-f16.gguf \
  --local-dir ~/models/bge-m3

# reranker:bge-reranker-v2-m3
HF_HUB_ENABLE_HF_TRANSFER=1 hf download \
  gpustack/bge-reranker-v2-m3-GGUF bge-reranker-v2-m3-Q4_K_M.gguf \
  --local-dir ~/models/bge-reranker-v2-m3
```

兩個合計約 1.5GB。

### 2.4 (選用)VL 模型

只在你會用 `analyze_file(...)` 處理截圖、UI 錯誤畫面、或把圖片 ingest 進 RAG 知識庫時才需要。CodeTrail 的內建 VL key 是 `qwen3-vl`,但任何相容的 VL GGUF 都可以(需要 mmproj 副檔)。沒打算分析圖片的話這一步直接跳過。

---

## 3. 啟動 llama-server(用 tmux 跑在背景)

CodeTrail 會把不同角色拆成不同 `llama-server` instance:main / embedding 是必要的;reranker 建議啟動,沒啟動時 RAG 會 fallback 到主模型排序;VL 選用。會分開是因為 `llama-server` 一次只能載一顆 GGUF,不同角色用不同模型 / 不同模式(`--jinja` / `--embedding --pooling cls` / `--embedding --pooling rank --reranking` / `--mmproj`),所以必須開不同 process。

| Port | 角色 | 模型 | 必要 |
|---|---|---|---|
| 8080 | main(聊天、推理、工具呼叫) | `<CODE_MODEL>` | 是 |
| 8081 | embedding(算向量,RAG 搜相似段落) | `bge-m3` | 是 |
| 8082 | reranker(RAG 結果重排) | `bge-reranker-v2-m3` | 建議(可選) |
| 8083 | VL(看截圖 / 圖片) | `qwen3-vl` 等 | 否 |

下面用推薦的 main / embedding / reranker 三個 server 示範,**一個 server 開一個獨立的 tmux session**。流程都一樣:`tmux new -s <名字>` 進去 → 貼指令 → 等 `server is listening on ...` → 按 `Ctrl-b d` 退出來放背景。terminal 之後關掉也不會死。主 server(§3.1)有硬體調整需要,embedding / reranker(§3.2)參數固定。

> **tmux 你會用到的 4 個指令**(其他都不用學):
> - `Ctrl-b d` —— 把目前 session 放背景,回到原本 shell
> - `tmux ls` —— 列出所有背景 session
> - `tmux a -t <名字>` —— 接回去看某個 session 的即時 log
> - `tmux kill-session -t <名字>` —— 關掉某個 session
> - bonus:`Ctrl-b n` —— 同 session 內切換 window(§3.2 的 RAG session 內含 embed / rerank 兩個 window)

### 3.1 Session 1 — 主 server(:8080)

從你的一般 shell 起 session:

```bash
tmux new -s codetrail-main
```

進去之後(prompt 下方會出現綠色 tmux 狀態列)貼下面這條,5090 + Qwen3-235B-A22B-Instruct-2507 Q4_K_M 範例:

```bash
~/llama.cpp/build/bin/llama-server \
  -m ~/models/Qwen3-235B-A22B-Instruct-2507-GGUF/Q4_K_M/Qwen3-235B-A22B-Instruct-2507-Q4_K_M-00001-of-00003.gguf \
  --host 0.0.0.0 --port 8080 \
  -c 65536 -ngl 99 --jinja \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --n-cpu-moe 86 \
  -fa on \
  -b 2048 -ub 512 \
  -t 12 \
  --no-mmap
```

旗標說明:

- `-m ...-00001-of-00003.gguf` —— 只指 shard 1,llama.cpp 自動接後續
- `-c 65536` —— context 上限 64K token(模型原生 256K,KV cache 會吃 VRAM,先從 64K 起)
- `-ngl 99` —— 嘗試把所有層放 GPU(MoE expert 之後會被 `--n-cpu-moe` 拉回 CPU)
- `--jinja` —— 啟用模型內建 chat template,tool calling 才會走對格式
- `--cache-type-k/v q8_0` —— KV cache 量化到 8-bit,64K ctx 約省一半 VRAM
- `--n-cpu-moe 86` —— **MoE 模型才加**;Qwen3-235B 共 94 層,**前 86 層 expert 卸到 CPU RAM、剩 8 層留在 GPU**。比 `--cpu-moe`(= 全 94 層都在 CPU)的純 offload 路徑快 ~50%(實測 5090 上 TG 從 ~5 升到 7.5 tok/s),代價是多吃 ~12 GB VRAM。**N 數字依你 VRAM 調整,見下方表格**
- `-fa on` —— 啟用 flash attention,省 KV cache VRAM、加速 attention 計算。dense / MoE 都適用
- `-b 2048 -ub 512` —— 加大 prompt processing 的 batch / micro-batch,PP 速度提升 ~30%(實測 5090 PP 從 ~50 升到 ~80 tok/s)。代價是啟動時略多 compute buffer
- `-t 12` —— 用 12 條執行緒。9950X 是 16 核 2-CCD,跨 CCD 通訊延遲高,12 是甜蜜點(比 `-t 16` 快 ~10%)。**依你 CPU 調整,見下方說明**
- `--no-mmap` —— **強烈建議搭 `--n-cpu-moe` 一起加**;不加的話用 mmap 懶載入,第一次推理 TTFT 可能 1–2 分鐘(每次觸到新 expert 都要從 SSD page-in);加了之後啟動時把 weights 全讀進 RAM,啟動慢 1–2 分鐘但之後對話穩定

非 MoE 模型(dense 30B / 14B / 7B)**不要加** `--n-cpu-moe` 與 `--no-mmap`,直接拿掉那兩行即可。`-fa on` / `-b 2048 -ub 512` / `-t N` 對 dense 模型一樣有效,可以保留。

#### `--n-cpu-moe N` 與 `-t N` 調整(其他硬體配置)

**`--n-cpu-moe N`**:每多 1 層 expert 從 CPU 搬到 GPU,**多吃 ~1.4 GB VRAM、TG 提升 ~5~8%**。**留至少 3 GB VRAM 緩衝**給 KV cache 動態成長,否則長對話會 OOM 死掉。以 Qwen3-235B(94 層)為例,起手值:

| VRAM | 建議 `--n-cpu-moe` | GPU expert 層數 | VRAM 用量(估) | TG(估) |
|---|---|---|---|---|
| 24GB | `90` | 4 | ~18 GB | ~6 tok/s |
| **32GB** | **`86`** | **8** | **~26 GB**(5090 實測) | **~7.5 tok/s**(實測) |
| 48GB | `74` | 20 | ~42 GB | ~10 tok/s |
| 96GB | `42` | 52 | ~88 GB | ~15~18 tok/s |

啟動完跑 `nvidia-smi` 看主 server VRAM,**留 3 GB 緩衝為目標**:用太少 → 浪費頻寬;用太滿 → 後續對話 KV cache 一脹就 OOM。從上表起手值開始,觀察一兩次對話的 VRAM 高點再 ±2 微調(N 越小越快越吃 VRAM)。

**`-t N`** 規則:**P-core 物理核數 −2~−4**(避開 hyperthread、避開跨 CCD)。常見組合:

| CPU | 物理結構 | 建議 `-t` |
|---|---|---|
| Ryzen 9 9950X / 9950X3D | 16 核 2-CCD | `12` |
| Ryzen 9 9900X / 9900X3D | 12 核 2-CCD | `10` |
| Ryzen 7 9700X / 9800X3D | 8 核單 CCD | `6~8` |
| Core Ultra 9 285K | 8 P + 16 E(無 HT) | `8`(只用 P-core) |
| Core Ultra 7 265K | 8 P + 12 E(無 HT) | `8` |
| Core Ultra 5 245K | 6 P + 8 E(無 HT) | `6` |

不確定甜蜜點可以用 `llama-bench -m <model.gguf> --n-cpu-moe N -t 8,12,16` 實測,挑 TG 數字最大那組。

`--no-mmap` 模式下大約 1.5–2.5 分鐘(看 SSD 速度),期間會看到一堆 `load_tensors:` 滾過。**等到下面這行出現才算成功**:

```
srv  llama_server: server is listening on http://0.0.0.0:8080
```

看到之後按 `Ctrl-b d` 退出來,回到一般 shell。server 留在背景跑。

### 3.2 RAG server — embedding + reranker(一鍵啟動 script)

兩顆 RAG 副模型(embedding `bge-m3`、reranker `bge-reranker-v2-m3`)體積小、啟動秒開、**參數不需要依硬體調整**,所以包成一個 script 一次啟動,合在同一個 tmux session `codetrail-rag` 的兩個 window 內,使用者只需要管理一個 session。

```bash
chmod +x scripts/start-rag-servers.sh    # 第一次跑要加 exec bit
./scripts/start-rag-servers.sh
```

預期輸出:

```
[+] 啟動 embedding server (:8081) 於 tmux codetrail-rag:embed
[+] 啟動 reranker server  (:8082) 於 tmux codetrail-rag:rerank

兩顆 server 大約 5–10 秒後 listening。驗證:
  curl -s -o /dev/null -w 'embed  :8081 -> %{http_code}\n' http://localhost:8081/health
  curl -s -o /dev/null -w 'rerank :8082 -> %{http_code}\n' http://localhost:8082/health
...
```

一次關掉兩顆 server:

```bash
./scripts/stop-rag-servers.sh
```

(平常不用看 log,真要偵錯才 `tmux a -t codetrail-rag`,session 內 `Ctrl-b n` 切 embed/rerank window,`Ctrl-b d` 退出。)

**不跑 reranker 的話**(例如還沒下載):script 偵測到 `bge-reranker-v2-m3-Q4_K_M.gguf` 不存在會自動跳過,只啟動 embedding。doctor 會把 8082 列為 WARN 但不擋啟動。

**模型路徑非預設**:script 找的是 `~/models/bge-m3/...` 與 `~/models/bge-reranker-v2-m3/...`。若放別處,啟動前 `export MODELS_DIR=/your/path`。llama-server 不在 `~/llama.cpp/...` 也類似:`export LLAMA_BIN=/your/llama-server`。

### 3.3 驗活與維運

照上面流程跑下來會有 **2 個 tmux session**(main 自己一個、embed+rerank 合在一個):

```bash
tmux ls
# 應該看到:
#   codetrail-main: 1 windows (created ...)
#   codetrail-rag:  2 windows (created ...)    ← 內含 embed + rerank 兩個 window
```

驗已啟動的 port 都通(若跳過 reranker,把 8082 拿掉):

```bash
for p in 8080 8081 8082; do
  echo ":$p → $(curl -s -o /dev/null -w '%{http_code}' http://localhost:$p/health)"
done
# 應該都印 200
```

之後要關掉全部:

```bash
tmux kill-session -t codetrail-main    # 砍主 server
./scripts/stop-rag-servers.sh          # 砍 embed + rerank
```

偵錯時要看 server log(平常不用):`tmux a -t codetrail-main` 或 `tmux a -t codetrail-rag`(rag 內按 `Ctrl-b n` 切 embed/rerank window,看完 `Ctrl-b d` 退出)。

VRAM 與 RAM 預期占用(5090 + 235B `--n-cpu-moe 86 --no-mmap`):

```
VRAM  ~26 GB / 32 GB    (主 ~24 + embed ~1 + rerank ~0.4;8 層 expert 在 GPU)
RAM   ~125 GB / 170 GB  (其餘 86 層 expert 在 RAM)
```

對比純 `--cpu-moe`(94 層全在 CPU)為 VRAM ~14 GB / RAM ~135 GB,差異是把 8 層 expert(~12 GB)從 RAM 搬到 VRAM,換來 ~50% TG 速度提升。

---

## 4. 設定 CodeTrail + OpenCode

### 4.1 Model registry(短名稱 → GGUF 路徑)

讓 `AICODE_MODEL=<CODE_MODEL>` 這種短名稱自動對應到實際 GGUF 路徑,不用每次打絕對路徑:

```bash
mkdir -p ~/.config/codetrail
cat > ~/.config/codetrail/models.json <<'EOF'
{
  "<CODE_MODEL>": "~/models/<CODE_MODEL_GGUF_RELATIVE_PATH>.gguf",
  "bge-m3": "~/models/bge-m3/bge-m3-f16.gguf",
  "bge-reranker-v2-m3": "~/models/bge-reranker-v2-m3/bge-reranker-v2-m3-Q4_K_M.gguf"
}
EOF
```

用 5090 走廊的 235B 為例,實際內容會像:

```json
{
  "qwen3-235b-a22b-instruct": "~/models/Qwen3-235B-A22B-Instruct-2507-GGUF/Q4_K_M/Qwen3-235B-A22B-Instruct-2507-Q4_K_M-00001-of-00003.gguf",
  "bge-m3": "~/models/bge-m3/bge-m3-f16.gguf",
  "bge-reranker-v2-m3": "~/models/bge-reranker-v2-m3/bge-reranker-v2-m3-Q4_K_M.gguf"
}
```

也可以跳過 registry 直接把 `AICODE_MODEL` 設絕對路徑,但 registry 比較好維護。

### 4.2 OpenCode config

llama-server 提供 OpenAI 相容 `/v1`,OpenCode 用 openai-compatible provider 即可:

```bash
mkdir -p ~/.config/opencode
${EDITOR:-vi} ~/.config/opencode/opencode.json
```

把下面整段貼進去,**把所有 `<CODE_MODEL>` 換成你 4.1 裡用的 registry key**(例如 `qwen3-235b-a22b-instruct`):

```json
{
  "$schema": "https://opencode.ai/config.json",

  "share": "disabled",
  "autoupdate": false,

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
          "limit": { "context": 32768, "output": 8192 }
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
      "timeout": 10000
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

說明:

- `llamacpp` 是 provider key,可改名(`local`、`llmcpp`、隨意),但要跟 `"model"` 那段的 prefix 對齊。
- `apiKey` 任意非空值即可,llama-server 預設不檢查。
- `limit.context: 32768` 是 OpenCode 主對話實際塞給 server 的上限。可以等於 server `-c`(64K),但留一半做 output / 不擠爆比較穩。
- `permission` 區段:`*: deny` 是預設拒絕一切,只白名單 `codetrail_*`(經 CodeTrail 沙箱)。OpenCode 內建工具(`bash` / `read` / `write` 等)會繞過 CodeTrail 沙箱,所以這裡明確 `deny`。

貼完先驗 JSON 格式:

```bash
python -m json.tool ~/.config/opencode/opencode.json >/dev/null
```

### 4.3 安裝 `aicode` 啟動指令

從 CodeTrail repo 根目錄:

```bash
chmod +x ./aicode
mkdir -p "$HOME/.local/bin"
ln -sfn "$PWD/aicode" "$HOME/.local/bin/aicode"
command -v aicode    # 應顯示 ~/.local/bin/aicode
```

如果 `command -v aicode` 沒輸出,代表 `~/.local/bin` 不在你目前 shell 的 PATH:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

`aicode` 做的事:把目前目錄設成 `AICODE_ROOT`(沙箱根目錄)、拒絕從 `$HOME` 或 `/` 起、在當前 git root 準備 `.opencode/run-codetrail-mcp` 讓 OpenCode 的 MCP command 找得到、啟動前跑 ctx safety check 確認 `AICODE_DYNAMIC_NUM_CTX_MAX` 沒超過 server `-c`、最後啟 `opencode`。

---

## 5. 自檢與啟動 TUI

### 5.1 跑 doctor 自檢

```bash
AICODE_MODEL=<CODE_MODEL> python scripts/doctor.py
```

(把 `<CODE_MODEL>` 換成你的 registry key,例如 `qwen3-235b-a22b-instruct`)

預期結尾看到 `PASS=2x WARN=x FAIL=0`。常見可忽略的 WARN:

- `html2text 沒裝` —— 只有 RAG 抓網頁要,可忽略
- `VL server (8083) 不可連` —— 沒啟動 VL 就會這樣,可忽略
- `knowledge.json 不存在` —— RAG 知識庫還沒建立,等用到再說

**有 FAIL 不要跳過**,通常是 PATH、server 沒啟動、GGUF 路徑寫錯。對應修法見 [docs/troubleshooting.md](docs/troubleshooting.md)。

### 5.2 啟動 TUI

切到你要分析或修改的專案目錄(**不要從 `$HOME` 或 `/` 啟動**,沙箱會拒絕):

```bash
cd <PROJECT_TO_ANALYZE>
aicode
```

(`AICODE_MODEL` 已經寫進 `~/.bashrc` 或上一行的話就不用再 `AICODE_MODEL=... aicode`)

如果要讓模型讀專案外的附件(`~/Downloads` 的 log、截圖、spec、firmware blob):

```bash
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 aicode
```

預設允許 `~/Downloads` 和 `/tmp`。其他目錄要加白名單:

```bash
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 \
AI_CODE_IMPORT_ROOTS="$HOME/Downloads:/tmp:$HOME/specs" \
aicode
```

| 變數 | 用途 |
|---|---|
| `AI_CODE_ALLOW_EXTERNAL_IMPORT=1` | 外部附件匯入總開關 |
| `AI_CODE_IMPORT_ROOTS="..."` | 外部附件來源白名單(設了就取代預設) |

### 5.3 Smoke test

進到 TUI 後輸入:

```text
請用工具 list_dir 看當前目錄結構,挑出 entry point、主要模組和測試目錄,簡單整理。
```

模型應該會呼叫 `codetrail_list_dir` 工具讀真實目錄,然後回給你整理結果。

第一個請求**首字延遲(TTFT)**:

- 用 `--no-mmap` 模式:約 5–15 秒
- 用 mmap 模式(沒加 `--no-mmap`):**第一次可能要 1–2 分鐘**,因為要從 SSD page-in MoE expert weights。畫面上 OpenCode 會顯示「`...esc interrupt`」等待中,**不要按 Esc**,等就對了

如果想驗證工具有沒有連上,在 TUI 裡輸入 `/status`,應看到 `codetrail Connected`。

更多操作模式(夾帶附件、注入 RAG、查 spec)見 [docs/basic-usage.md](docs/basic-usage.md);完整 17 個工具清單見 [docs/mcp-tools.md](docs/mcp-tools.md)。

---

## 必守安全界線

- `AICODE_ROOT` 是本次 OpenCode 可讀寫的 sandbox 根目錄;不要從 `$HOME` 或 `/` 啟動。
- MCP server 啟動時會拒絕危險 root,並把工具限制在 `AICODE_ROOT` 內。
- `knowledge.json`、`knowledge_emb.npz`、`data/`、`.codetrail/`、`*.jsonl` 和 `.code_rag_cache_*` 通常含 NDA 片段,**不要 commit**。
- `apply_patch(...)` 有 context matching、max files、max lines 限制;不要放寬安全層。要完全關閉改檔,啟動時設 `AI_CODE_PATCH=0`。
- `run_command(...)` 只允許白名單命令,不支援 shell metacharacter。預設白名單只含測試 / lint;`make` / `cmake` / `ninja` / `meson` / `bazel build` 等 build 命令需要顯式 `AI_CODE_ENABLE_BUILD_COMMANDS=1` 才會掛上。要完全關閉命令執行,設 `AI_CODE_RUN_TESTS=0`。
- 遠端 llama-server 會收到 prompt、程式碼片段、spec 摘要與工具輸出,**只能指向可信內網 / VPN 主機**(llama-server 預設不檢查 API key)。

完整安全說明見 [docs/security.md](docs/security.md)。

---

## 文件地圖

| 文件 | 內容 |
|---|---|
| [docs/setup.md](docs/setup.md) | 替代安裝方式、進階配置、換機部署 reference |
| [docs/basic-usage.md](docs/basic-usage.md) | TUI 內常用操作:正常對話、夾帶附件、RAG 注入、最小驗收流程 |
| [docs/models.md](docs/models.md) | 模型挑選邏輯、MoE / `--cpu-moe` / `--n-cpu-moe`、context、KV cache 量化、遠端 server |
| [docs/rag.md](docs/rag.md) | 讀檔、匯入附件、建立知識庫、Code-RAG、查 spec |
| [docs/mcp-tools.md](docs/mcp-tools.md) | CodeTrail 暴露的 17 個 MCP 工具與使用原則 |
| [docs/security.md](docs/security.md) | sandbox、patch、run command、NDA 資料、工作節奏 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | `/status`、ctx-safety、server 不可連、Blackwell CUDA、MoE 首字慢 |
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
