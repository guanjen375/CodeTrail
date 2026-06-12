# CodeTrail - OpenCode / Codex CLI + llama.cpp 本地 MCP 工作台

CodeTrail 是一個給 OpenCode 與 Codex CLI 使用的本地 MCP 後端。你在任一 frontend TUI 裡提問,模型可以透過 CodeTrail 讀專案、找程式碼、查已匯入的 spec、分析截圖或 binary、產生 patch,並在允許的白名單內跑驗證命令。

Supported frontends:
- OpenCode: aicode
- Codex CLI: aicodex

`aicode` 和 `aicodex` 是 parallel frontends,不是 reviewer / fallback / 輔助工具關係。喜歡 OpenCode 就用 `aicode`;喜歡 Codex CLI 就用 `aicodex`。兩者共用同一套 CodeTrail MCP server、`AICODE_ROOT` sandbox、KnowledgeBase、CodeRAG、patch flow 和 command policy。

CodeTrail 目前定位是**成熟私有部署版**:適合本機、離線、NDA / firmware / private repo 分析;**不打算公開發布**成 PyPI package、Docker image 或 SaaS。安全邊界有自動測試保護,但未做公開產品級安全審計。

底層推理引擎使用 [llama.cpp](https://github.com/ggerganov/llama.cpp) `llama-server`(自己 build,需要 CUDA)。所有 CodeTrail internal LLM / embedding / reranker / VL 走它的 HTTP API。Codex CLI frontend model 可以另外使用你自己的 Codex / OpenAI / ChatGPT / local provider 設定。

讀完這份 README 從零走到能進 OpenCode 或 Codex CLI TUI 跟模型對話,大約需要:
- 一次性安裝 + build:30–60 分鐘
- 下載模型 GGUF:依網速與所選模型,5 分鐘到數小時不等

### 完成標準

本 README 走完只要四件事都成立就算「完成」,任一步卡住就停在那一步排查,不要硬往下走:

1. `AICODE_MODEL=<CODE_MODEL> python scripts/doctor.py` 結尾印 `PASS=... WARN=... FAIL=0`
2. `cd <PROJECT_TO_ANALYZE> && aicode` 進得了 OpenCode TUI(看到綠色輸入框、沒有 fatal 訊息)
3. TUI 內輸入 `/status`,看到 `codetrail Connected`
4. Smoke test:輸入 §5.3 的範例 prompt,模型成功呼叫 `codetrail_list_dir` 並回傳真實目錄結果

Codex CLI frontend 驗收標準:

1. `cd <PROJECT_TO_ANALYZE> && aicodex` 可以開 Codex CLI。
2. Codex TUI 內 `/mcp` 可以看到 `codetrail` connected。
3. `list_dir` works。
4. `read_file` works。
5. `grep_code` 或 `code_rag_search` works。
6. `apply_patch` 需要 approval。
7. `run_command` 需要 approval。
8. `AICODE_ROOT=/` 被拒絕。
9. `AICODE_ROOT=$HOME` 被拒絕。
10. 既有 `aicode` 行為不變。

任一步 FAIL 對應的修法見 [docs/troubleshooting.md](docs/troubleshooting.md)。

---

## 特別注意(剛上手最容易踩的)

> [!WARNING]
> 動手前掃一遍 —— 這幾點踩了通常會卡很久,或踩到 NDA / 安全:
>
> 1. **每個新 shell 都要先 `source <CODETRAIL_REPO>/.venv/bin/activate`** —— 沒 activate venv,`aicode` / `aicodex` / web 起的 CodeTrail MCP 會 `ModuleNotFoundError: No module named 'mcp'`。嫌煩就寫進 `~/.bashrc`(§1.3)。
> 2. **四個 llama-server 都要起**:main `8080` + embedding `8081` + reranker `8082` + VL `8083`。三顆副模型是硬性需求,缺一個啟動前 preflight 就擋下;reranker 預設不降級。見 §3。
> 3. **不要從 `$HOME` 或 `/` 啟動** —— 沙箱會直接拒絕。先 `cd` 進你要分析的**具體專案目錄**再跑。
> 4. **換模型是三件獨立的事**:TUI 按 `/models` 只切 OpenCode 的 model id,**不會 reload llama-server、也不會通知 CodeTrail MCP**。真要換 → 停 server、載新 GGUF、重啟 server、改 `AICODE_MODEL`、重啟 `aicode`。
> 5. **CodeTrail 沙箱鎖在「你啟動的那個資料夾」(`AICODE_ROOT`)** —— 綁在 process 上,**不會跟著你在 UI 切資料夾或切對話而移動**。web UI 那顆「切換資料夾」按鈕對 CodeTrail 無效(切過去還是只讀啟動目錄)。換專案 = 到那個目錄重新啟動一個(TUI 重開 `aicode`;web 另起一個 backend)。
> 6. **web 模式目前是實驗性的(開發中)** —— 穩定、proven 的主力是 standalone TUI(`aicode` / `aicodex`);web 用來瀏覽器續問歷史 session,行為可能還會變。要可靠就用 TUI。
> 7. **CodeTrail 沙箱只蓋它那 17 個 MCP 工具** —— OpenCode 內建的 `bash` / `read` / `write` 不走這層,所以範本把它們全 `deny`,**別放寬那份 permission**。分析不信任 repo 時,連被分析 repo 自帶的 `opencode.json` 都可能翻掉你的鎖定(防法:`OPENCODE_DISABLE_PROJECT_CONFIG=1 aicode`,見 [docs/security.md](docs/security.md))。
> 8. **首次 MoE 對話首字會慢(可能 1–2 分鐘),別按 Esc** —— 它在 page-in expert weights,不是當掉;slot / GPU 在動就是正常。
> 9. **NDA / 衍生資料不要 commit**:`knowledge.json`、`*.jsonl`、`.codetrail/`、`data/`、`.aicode_uploads/` 等已在 `.gitignore`,commit 前自己 `git diff` 看一眼。

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
2. **MoE 模型 GGUF 體積 ≤ 你的 RAM 容量 − 15GB**(留給 OS / page cache)。否則 swap 一發、速度直接歸零。Q4_K_M 體積可用 `總參數(B) × 0.55 GB` 粗估;UD / XL 量化請以實際 HF 檔案大小為準。
3. **你接受生成速度從 pure GPU 的 ~40 tok/s 降到 6–15 tok/s**。Offload 路徑跑出來的東西可用,但延遲明顯高。

換言之,「VRAM 不夠就 offload 大模型」**只在 MoE 模型成立**,而且要看 RAM 多大。常見例子:

| MoE 模型 | Q4 / UD-Q4 體積 | 需要 RAM | 適合 VRAM 段 |
|---|---|---|---|
| Qwen3-235B-A22B-Thinking-2507 | ~140 GB 級 | ≥ 160 GB | 32GB+(`--n-cpu-moe` 部分 offload) |
| Llama 4 Scout(109B-MoE / 10M ctx) | ~60 GB | ≥ 80 GB | 32GB+ |
| Qwen3-Coder 480B-A35B-Instruct | ~265 GB | ≥ 280 GB | 48GB+(實質要 workstation 級 RAM) |
| Qwen3-30B-A3B(小型 MoE) | ~18 GB | ≥ 32 GB | 16GB(`--n-cpu-moe N` 部分 offload 留多數 expert 在 GPU) |
| Qwen3-32B(**dense**) | 19 GB | — | **不適用 offload**,直接選 24GB+ 卡 pure GPU |

下方 §3 走廊範例用 5090 32GB + 170GB RAM + Qwen3-235B-A22B-Thinking-2507 UD-Q4_K_XL(`--n-cpu-moe N --no-mmap`,N 依 VRAM 調整見 §3.1),屬於這裡的**例外路徑**(作者實測過所以拿來示範)。若你硬體 / 模型走的是上面正規 pure GPU 表的某一格,§3.1 server 指令把 `--n-cpu-moe` 與 `--no-mmap` 兩行拿掉即可。

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
- `tmux` —— §3 用來在背景跑四個 llama-server,§3 開頭有 4 行操作教學

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

#### 1.2.2 Codex CLI

```bash
npm install -g @openai/codex
command -v codex       # 確認可被找到
```

### 1.3 安裝 CodeTrail Python 依賴

Ubuntu 24.04 啟用 PEP 668,system Python 不允許直接 `pip install`。**在 CodeTrail repo 內建一個 venv**,後續所有 Python 動作(`scripts/doctor.py`、`aicode` / `aicodex` 啟動的 CodeTrail MCP server、§2.1 的 `hf` CLI)都跑在這個 venv 內:

```bash
cd <CODETRAIL_REPO>
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
pip install pymupdf4llm    # 選用:RAG 從 PDF 建知識庫才用;不做 RAG 可省略
```

`<CODETRAIL_REPO>` 是這個 CodeTrail 的 repo 路徑,不是你要分析的專案路徑。`requirements.txt` 已含 `mcp` / `requests` / `numpy`,不必再單獨 `pip install mcp`。

> **每次開新 shell 都要先 `source <CODETRAIL_REPO>/.venv/bin/activate`** 才能跑 `python scripts/doctor.py`、`aicode` 或 `aicodex` —— 兩個 frontend wrapper 內部都用 PATH 上的 `python3` 拉起 CodeTrail MCP server,venv 沒啟用時會 `ModuleNotFoundError: No module named 'mcp'`。覺得每次手動 activate 太煩,把這行寫進 `~/.bashrc`(把 `<CODETRAIL_REPO>` 換成絕對路徑,例如 `$HOME/CodeTrail`):
>
> ```bash
> echo 'source <CODETRAIL_REPO>/.venv/bin/activate' >> ~/.bashrc
> ```
>
> §3 用 `tmux new -s ...` 開的新 session 是獨立的 shell —— 不過 §3 那四個 `llama-server` process 跑的是二進位、與 venv 無關,不必再 activate。`tmux` 主要影響的是 §5.1 / §5.2 那種需要 Python 的指令所在的 shell。

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

下面用 5090 + 170GB RAM 走廊的 Qwen3-235B-A22B-Thinking-2507 UD-Q4_K_XL 當範例。它在 HuggingFace 上是 **3 個分片檔,約 140GB 級**:

```bash
mkdir -p ~/models

HF_HUB_ENABLE_HF_TRANSFER=1 hf download \
  unsloth/Qwen3-235B-A22B-Thinking-2507-GGUF \
  --include "UD-Q4_K_XL/*" \
  --local-dir ~/models/Qwen3-235B-A22B-Thinking-2507-GGUF
```

下完之後會有:

```
~/models/Qwen3-235B-A22B-Thinking-2507-GGUF/UD-Q4_K_XL/
  Qwen3-235B-A22B-Thinking-2507-UD-Q4_K_XL-00001-of-00003.gguf
  Qwen3-235B-A22B-Thinking-2507-UD-Q4_K_XL-00002-of-00003.gguf
  Qwen3-235B-A22B-Thinking-2507-UD-Q4_K_XL-00003-of-00003.gguf
```

> 啟動 server 時 `-m` **只指 shard 1**,llama.cpp 會自動讀第 2、3 片。

如果你的硬體不是 5090、選了別的 `<CODE_MODEL>`,把上面的 repo 與 `--include` pattern 換成你要的那顆。常見社群 GGUF 來源:`unsloth/...-GGUF`、`bartowski/...-GGUF`、官方 `Qwen/...-GGUF`。

### 2.3 下載 RAG 附屬模型

CodeTrail 的 RAG / Code-RAG 內建固定使用 `bge-m3`(embedding) 與 `bge-reranker-v2-m3`(reranker)。兩者都是必要副模型:聊天 frontend 啟動前會硬性檢查 embedding / reranker / VL 都 ready,reranker 缺失不再降級成 embedding 排序。這兩個體積很小:

```bash
# embedding:bge-m3 (用 f16,不要量化 — embedding 對量化敏感,Q4 會明顯影響召回)
HF_HUB_ENABLE_HF_TRANSFER=1 hf download \
  CompendiumLabs/bge-m3-gguf bge-m3-f16.gguf \
  --local-dir ~/models/bge-m3

# reranker:bge-reranker-v2-m3
HF_HUB_ENABLE_HF_TRANSFER=1 hf download \
  gpustack/bge-reranker-v2-m3-GGUF bge-reranker-v2-m3-Q8_0.gguf \
  --local-dir ~/models/bge-reranker-v2-m3
```

兩個合計約 2GB 級。

### 2.4 VL 模型

CodeTrail 的內建 VL key 是 `qwen3-vl`。目前不需要替換:Qwen3-VL 有官方 GGUF 與 mmproj,適合本專案的截圖、UI 錯誤畫面與圖片 ingestion。預設 launcher 會找 Qwen3-VL 8B Instruct Q4_K_M + F16 mmproj;若你要用別的相容 VL GGUF,啟動前設定 `VL_GGUF` / `VL_MMPROJ`。

```bash
HF_HUB_ENABLE_HF_TRANSFER=1 hf download \
  Qwen/Qwen3-VL-8B-Instruct-GGUF \
  Qwen3VL-8B-Instruct-Q4_K_M.gguf \
  mmproj-Qwen3VL-8B-Instruct-F16.gguf \
  --local-dir ~/models/qwen3-vl
```

---

## 3. 啟動 llama-server(用 tmux 跑在背景)

CodeTrail 會把不同角色拆成不同 `llama-server` instance:main / embedding / reranker / VL 都是必要的。會分開是因為 `llama-server` 一次只能載一顆 GGUF,不同角色用不同模型 / 不同模式(`--jinja` / `--embedding --pooling cls` / `--embedding --pooling rank --reranking` / `--mmproj`),所以必須開不同 process。`aicode` / `aicodex` / `mcp_server.py` 都會硬性檢查三顆副模型已 ready。

| Port | 角色 | 模型 | 必要 |
|---|---|---|---|
| 8080 | main(聊天、推理、工具呼叫) | `<CODE_MODEL>` | 是 |
| 8081 | embedding(算向量,RAG 搜相似段落) | `bge-m3` | 是 |
| 8082 | reranker(RAG 結果重排) | `bge-reranker-v2-m3` | 是 |
| 8083 | VL(看截圖 / 圖片) | `qwen3-vl` 等 | 是 |

下面用 main + 三顆附屬 server 示範。主 server 自己一個 tmux session;embedding / reranker / VL 由 §3.2 script 合在同一個 tmux session 內。流程都一樣:啟動 → 等 `server is listening on ...` / `/health status=ok` → 按 `Ctrl-b d` 退出來放背景。terminal 之後關掉也不會死。

> **tmux 你會用到的 4 個指令**(其他都不用學):
> - `Ctrl-b d` —— 把目前 session 放背景,回到原本 shell
> - `tmux ls` —— 列出所有背景 session
> - `tmux a -t <名字>` —— 接回去看某個 session 的即時 log
> - `tmux kill-session -t <名字>` —— 關掉某個 session
> - bonus:`Ctrl-b n` —— 同 session 內切換 window(§3.2 的 RAG session 內含 embed / rerank / vl 三個 window)

### 3.1 Session 1 — 主 server(:8080)

從你的一般 shell 起 session:

```bash
tmux new -s codetrail-main
```

進去之後(prompt 下方會出現綠色 tmux 狀態列)貼下面這條,5090 + 170GB RAM + Qwen3-235B-A22B-Thinking-2507 UD-Q4_K_XL 實測範例:

```bash
~/llama.cpp/build/bin/llama-server \
  -m ~/models/Qwen3-235B-A22B-Thinking-2507-GGUF/UD-Q4_K_XL/Qwen3-235B-A22B-Thinking-2507-UD-Q4_K_XL-00001-of-00003.gguf \
  --host 0.0.0.0 --port 8080 \
  -c 65536 -ngl 99 --jinja \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --n-cpu-moe 90 \
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
- `--n-cpu-moe 90` —— **MoE 模型才加**;Qwen3-235B 共 94 層,**前 90 層 expert 卸到 CPU RAM、剩 4 層留在 GPU**。這是 5090 同卡掛 main + embedding + reranker + VL 的實測值;若只跑 main 或把附屬模型放另一張卡,可再把 N 調小換速度。**N 數字依你 VRAM 調整,見下方表格**
- `-fa on` —— 啟用 flash attention,省 KV cache VRAM、加速 attention 計算。dense / MoE 都適用
- `-b 2048 -ub 512` —— 加大 prompt processing 的 batch / micro-batch,PP 速度提升 ~30%(code session 實測 prompt eval / ingest 約 74-80 tok/s;這不是輸出生成速度)。代價是啟動時略多 compute buffer
- `-t 12` —— 用 12 條執行緒。9950X 是 16 核 2-CCD,跨 CCD 通訊延遲高,12 是甜蜜點(比 `-t 16` 快 ~10%)。**依你 CPU 調整,見下方說明**
- `--no-mmap` —— **強烈建議搭 `--n-cpu-moe` 一起加**;不加的話用 mmap 懶載入,第一次推理 TTFT 可能 1–2 分鐘(每次觸到新 expert 都要從 SSD page-in);加了之後啟動時把 weights 全讀進 RAM,啟動慢 1–2 分鐘但之後對話穩定

非 MoE 模型(dense 30B / 14B / 7B)**不要加** `--n-cpu-moe` 與 `--no-mmap`,直接拿掉那兩行即可。`-fa on` / `-b 2048 -ub 512` / `-t N` 對 dense 模型一樣有效,可以保留。

#### `--n-cpu-moe N` 與 `-t N` 調整(其他硬體配置)

**`--n-cpu-moe N`**:每多 1 層 expert 從 CPU 搬到 GPU,**多吃 ~1.4 GB VRAM、TG 提升 ~5~8%**。**留至少 3 GB VRAM 緩衝**給 KV cache 動態成長,否則長對話會 OOM 死掉。以 Qwen3-235B(94 層)為例,起手值:

| VRAM | 建議 `--n-cpu-moe` | GPU expert 層數 | VRAM 用量(估) | TG(估) |
|---|---|---|---|---|
| 24GB | `90~92` | 2~4 | ~16-20 GB | 以實測為準 |
| **32GB(5090 + RAG/VL 同卡)** | **`90`** | **4** | **~28 GB**(四 server 實測) | **~7.4 tok/s**(單請求長 decode) |
| 32GB(main only 或附屬模型分到別張卡) | `86` | 8 | ~26 GB(主 server) | 需另測 |
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

### 3.2 附屬 server — embedding + reranker + VL(一鍵啟動 script)

三顆必要副模型(embedding `bge-m3`、reranker `bge-reranker-v2-m3`、VL `qwen3-vl`)由 script 一次啟動,合在同一個 tmux session `codetrail-rag` 的三個 window 內,使用者只需要管理一個 session。script 會從 `AICODE_LLAMA_EMBED_BASE_URL` / `AICODE_LLAMA_RERANK_BASE_URL` / `AICODE_LLAMA_VL_BASE_URL` 解析 host:port(預設 `http://localhost:8081` / `http://localhost:8082` / `http://localhost:8083`),啟動後輪詢 `/health` JSON,直到 `status == "ok"` 才算成功;`status="loading model"` 不算 ready。

```bash
chmod +x scripts/start-rag-servers.sh    # 第一次跑要加 exec bit
./scripts/start-rag-servers.sh
```

常用覆寫:

```bash
# port / host 與 client 端 config 共用同一組 env
AICODE_LLAMA_EMBED_BASE_URL=http://localhost:18081 \
AICODE_LLAMA_RERANK_BASE_URL=http://localhost:18082 \
AICODE_LLAMA_VL_BASE_URL=http://localhost:18083 \
RAG_HEALTH_TIMEOUT=120 \
./scripts/start-rag-servers.sh

# GPU placement:未設定時沿用 CUDA_VISIBLE_DEVICES;單顆可用 EMBED_GPU / RERANK_GPU / VL_GPU 覆寫
CUDA_VISIBLE_DEVICES=0 ./scripts/start-rag-servers.sh
EMBED_GPU=0 RERANK_GPU=1 VL_GPU=1 ./scripts/start-rag-servers.sh
```

`AICODE_RERANK_FALLBACK_POLICY` 只控制啟動後 reranker 呼叫失敗時的行為;啟動前 preflight 仍要求 reranker server ready。

| policy | RAG 知識庫 fallback | Code RAG fallback |
|---|---|---|
| `embedding` | 保留 embedding / hybrid 既有排序,不呼叫主模型 | 同左 |
| `main_model` | 還原舊行為,用主聊天模型做 LLM rerank | 等同 `embedding`(Code RAG 沒有主模型 rerank 路徑) |
| `error` | 直接報錯,不靜默降級 | 直接報錯 |

預設是 `error`:專用 reranker 不可用或呼叫失敗就直接報錯。`main_model` 可能很貴:嚴格模式下每條符合條件的 RAG query 都可能觸發主模型 rerank。只有你明確接受這個成本時才設定 `AICODE_RERANK_FALLBACK_POLICY=main_model`。

預期輸出:

```
[+] 啟動 embedding server (http://localhost:8081) 於 tmux codetrail-rag:embed
[+] embedding health OK: http://localhost:8081/health status=ok
[+] 啟動 reranker server  (http://localhost:8082) 於 tmux codetrail-rag:rerank
[+] reranker health OK: http://localhost:8082/health status=ok
[+] 啟動 VL server        (http://localhost:8083) 於 tmux codetrail-rag:vl
[+] VL health OK: http://localhost:8083/health status=ok

Aux model servers ready。驗證:
  curl -s http://localhost:8081/health
  curl -s http://localhost:8082/health
  curl -s http://localhost:8083/health
...
```

一次關掉三顆 server:

```bash
./scripts/stop-rag-servers.sh
```

(平常不用看 log,真要偵錯才 `tmux a -t codetrail-rag`,session 內 `Ctrl-b n` 切 embed/rerank/vl window,`Ctrl-b d` 退出。)

**模型路徑非預設**:script 先找 `~/models/bge-m3/bge-m3-f16.gguf`、`~/models/bge-reranker-v2-m3/bge-reranker-v2-m3-Q8_0.gguf`、`~/models/qwen3-vl/Qwen3VL-8B-Instruct-Q4_K_M.gguf` 與 `~/models/qwen3-vl/mmproj-Qwen3VL-8B-Instruct-F16.gguf`;找不到時會在對應目錄 glob。若放別處,啟動前 `export MODELS_DIR=/your/path`;若要指定完整檔案,用 `EMBED_MODEL=/path/to/bge-m3*.gguf`、`RERANK_MODEL=/path/to/bge-reranker-v2-m3*.gguf`、`VL_GGUF=/path/to/Qwen3VL*.gguf`、`VL_MMPROJ=/path/to/mmproj*.gguf`。llama-server 不在 `~/llama.cpp/...` 也類似:`export LLAMA_BIN=/your/llama-server`。

### 3.3 驗活與維運

照上面流程跑下來會有 **2 個 tmux session**(main 自己一個、embed+rerank 合在一個):

```bash
tmux ls
# 應該看到:
#   codetrail-main: 1 windows (created ...)
#   codetrail-rag:  3 windows (created ...)    ← 內含 embed + rerank + vl 三個 window
```

驗已啟動的 port 都 ready:

```bash
for p in 8080 8081 8082 8083; do
  echo ":$p → $(curl -s http://localhost:$p/health)"
done
# 應該都含 {"status":"ok"};只有 HTTP 200 不夠,loading model 時也可能是 200
```

之後要關掉全部:

```bash
tmux kill-session -t codetrail-main    # 砍主 server
./scripts/stop-rag-servers.sh          # 砍 embed + rerank + vl
```

偵錯時要看 server log(平常不用):`tmux a -t codetrail-main` 或 `tmux a -t codetrail-rag`(rag 內按 `Ctrl-b n` 切 embed/rerank/vl window,看完 `Ctrl-b d` 退出)。

VRAM 與 RAM 實測占用(2026-06-11,5090 + 170GB RAM + 235B Thinking `--n-cpu-moe 90 --no-mmap`,同卡跑 main + embedding + reranker + VL):

```
VRAM  28083 MiB / 32607 MiB (main 17830 + VL 7952 + embed 1148 + rerank 896)
RAM   122 GiB used / 170 GiB total,48 GiB available,swap 幾乎未用
```

速度以 code tmux session 的 llama-server log 為準:prompt eval / ingest(吃輸入 prompt,不是輸出)約 74-80 tok/s;output decode / generation(實際吐字)單請求約 7.37 tok/s;兩個主模型請求重疊時,其中一個長任務落在約 4.95 tok/s,另一個小任務約 2.20 tok/s。這次主 server 未顯式加 `--parallel`;llama-server log 顯示 auto `n_parallel=4`。

---

## 4. 設定 CodeTrail + frontend

### 4.1 Model registry(短名稱 → GGUF 路徑)

讓 `AICODE_MODEL=<CODE_MODEL>` 這種短名稱自動對應到實際 GGUF 路徑,不用每次打絕對路徑:

```bash
mkdir -p ~/.config/codetrail
cat > ~/.config/codetrail/models.json <<'EOF'
{
  "<CODE_MODEL>": "~/models/<CODE_MODEL_GGUF_RELATIVE_PATH>.gguf",
  "bge-m3": "~/models/bge-m3/bge-m3-f16.gguf",
  "bge-reranker-v2-m3": "~/models/bge-reranker-v2-m3/bge-reranker-v2-m3-Q8_0.gguf"
}
EOF
```

用 5090 走廊的 235B 為例,實際內容會像:

```json
{
  "qwen3-235b-a22b-thinking": "~/models/Qwen3-235B-A22B-Thinking-2507-GGUF/UD-Q4_K_XL/Qwen3-235B-A22B-Thinking-2507-UD-Q4_K_XL-00001-of-00003.gguf",
  "bge-m3": "~/models/bge-m3/bge-m3-f16.gguf",
  "bge-reranker-v2-m3": "~/models/bge-reranker-v2-m3/bge-reranker-v2-m3-Q8_0.gguf"
}
```

也可以跳過 registry 直接把 `AICODE_MODEL` 設絕對路徑,但 registry 比較好維護。

### 4.2 OpenCode config

llama-server 提供 OpenAI 相容 `/v1`,OpenCode 用 openai-compatible provider 即可:

```bash
mkdir -p ~/.config/opencode
${EDITOR:-vi} ~/.config/opencode/opencode.json
```

把下面整段貼進去,**把所有 `<CODE_MODEL>` 換成你 4.1 裡用的 registry key**(例如 `qwen3-235b-a22b-thinking`):

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
- `enabled_providers` 鎖定只啟用本機 provider:設了之後 OpenCode 的 model picker(TUI 與 web)**只會出現你的本機模型**,雲端 provider(OpenCode Zen、Anthropic、OpenAI 等)完全不列出、無法誤選 —— **NDA 場景強烈建議保留**,避免把程式碼送到雲端模型。陣列內字串要跟你的 provider key 一致(這裡是 `llamacpp`)。
- `apiKey` 任意非空值即可,llama-server 預設不檢查。
- `limit.context: 32768` 是 OpenCode 主對話實際塞給 server 的上限。可以等於 server `-c`(64K),但留一半做 output / 不擠爆比較穩。
- `permission` 區段:`*: deny` 是預設拒絕一切,只白名單 `codetrail_*`(經 CodeTrail 沙箱)。OpenCode 內建工具(`bash` / `read` / `write` 等)會繞過 CodeTrail 沙箱,所以這裡明確 `deny`。

貼完先驗 JSON 格式:

```bash
python -m json.tool ~/.config/opencode/opencode.json >/dev/null
```

### 4.3 Codex CLI frontend provider(選用)

`aicodex` 不會自動修改 `~/.codex/config.toml`,也不要求每個 target project 手動放 `.codex/config.toml`。它啟動時會用 Codex CLI 的 runtime `-c` override 注入 CodeTrail MCP server。

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

這只是 Codex frontend provider 設定,和 CodeTrail MCP internal model 設定分開。`--codetrail-model` / `AICODE_MODEL` 控制的是 `mcp_server.py` 與 CodeTrail server-side tools 使用的本地模型;Codex 的 `-m` / `--model` 控制的是 Codex frontend model。如果本機 llama.cpp server 不支援 Codex CLI 需要的 API shape,可以讓 Codex frontend 照常使用自己的 OpenAI / ChatGPT / provider 設定,CodeTrail MCP internal tools 仍然透過 `--codetrail-model` 或 `AICODE_MODEL` 使用本地模型。

### 4.4 安裝 `aicode` 啟動指令

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

### 4.5 安裝 `aicodex` 啟動指令

從 CodeTrail repo 根目錄:

```bash
chmod +x ./aicodex
mkdir -p "$HOME/.local/bin"
ln -sfn "$PWD/aicodex" "$HOME/.local/bin/aicodex"
command -v aicodex    # 應顯示 ~/.local/bin/aicodex
```

`aicodex` 做的事:把目前目錄設成 `AICODE_ROOT`(沙箱根目錄)、拒絕從 `$HOME` 或 `/` 起、在當前 git root 準備 `.codex/run-codetrail-mcp`、解析 CodeTrail MCP internal local model、啟動前跑 ctx safety check、用 Codex CLI runtime `-c` override 注入 `codetrail` MCP server、最後在 target project root 啟動 `codex`。

`aicodex` 不會把 `--codetrail-model` 轉發給 Codex CLI;Codex frontend 的 `-m` / `--model` 會原樣轉發。

---

## 5. 自檢與啟動 TUI

### 5.1 跑 doctor 自檢

```bash
AICODE_MODEL=<CODE_MODEL> python scripts/doctor.py
```

(把 `<CODE_MODEL>` 換成你的 registry key,例如 `qwen3-235b-a22b-thinking`)

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

`<LOCAL_MODEL>` 用 §4.1 registry 裡的 `<CODE_MODEL>` bare name 或 GGUF 路徑。已經 `export AICODE_MODEL=<CODE_MODEL>`(或寫進 `~/.bashrc`)的話,`aicode` 直接打就好、`aicodex` 也不必再帶 `--codetrail-model`;Codex 自己的 frontend model 用 `-m`(跟 CodeTrail 本地模型分開,細節見 §4.3)。

要讓模型讀專案外的附件(`~/Downloads` 的 log / 截圖 / spec)就多加一個開關:

```bash
AI_CODE_ALLOW_EXTERNAL_IMPORT=1 aicode
```

來源白名單(`AI_CODE_IMPORT_ROOTS`)等細節見 [docs/basic-usage.md](docs/basic-usage.md)。第一次先照上面最短的指令跑起來就好。

### 5.3 Smoke test

進到 TUI 後輸入:

```text
請用工具 list_dir 看當前目錄結構,挑出 entry point、主要模組和測試目錄,簡單整理。
```

模型應該會透過 CodeTrail MCP 呼叫 `list_dir`(OpenCode log 裡可能顯示成 `codetrail_list_dir`)讀真實目錄,然後回給你整理結果。

第一個請求**首字延遲(TTFT)**:

- 用 `--no-mmap` 模式:約 5–15 秒
- 用 mmap 模式(沒加 `--no-mmap`):**第一次可能要 1–2 分鐘**,因為要從 SSD page-in MoE expert weights。畫面上 frontend 可能顯示「`...esc interrupt`」或類似等待狀態,**不要按 Esc**,等就對了

如果想驗證工具有沒有連上:OpenCode TUI 輸入 `/status`,應看到 `codetrail Connected`;Codex TUI 輸入 `/mcp`,應看到 `codetrail` connected。

更多操作模式(夾帶附件、注入 RAG、查 spec)見 [docs/basic-usage.md](docs/basic-usage.md);完整 17 個工具清單見 [docs/mcp-tools.md](docs/mcp-tools.md)。

### 5.4 Web 模式(選用)

> 🧪 **web 模式目前是實驗性功能(開發階段)。** 穩定、proven 的主力是 §5.2 的 standalone TUI(`aicode`);web 前端還在補完整(OpenCode 自家的 web UI 也還年輕),行為可能變動、偶有粗糙處。要穩定就用 TUI;想用瀏覽器瀏覽 / 續問歷史 session 再用 web。

standalone TUI 之外,`aicode` 還能開一個 **web backend**:用瀏覽器看歷史 session、點一筆直接續問;TUI 也能接上同一個 backend,兩邊共用同一份對話。**web backend 預設只綁 `127.0.0.1`(本機 loopback)、固定 port `4096`。**

> ⚠️ **CodeTrail 的沙箱綁在「你啟動 backend 的那個資料夾」(`AICODE_ROOT`)—— 綁在 process 上,不會跟著你在 UI 切資料夾、或切對話而移動。** 所以 OpenCode web UI 那顆「切換資料夾 / 開其他專案」按鈕**對 CodeTrail 完全無效**:切過去後 CodeTrail 工具還是只讀**啟動目錄**(讀不到沙箱外,所以不是 escape,但會讓你誤以為切了)。**請無視那顆切換器。** 要分析別的專案,就在那個目錄**另起一個 backend**(換 port,例:`AICODE_WEB_PORT=4097 <CODETRAIL_REPO>/scripts/start-web.sh`)。
>
> (TUI 沒有這顆切換器,你 `cd 專案 && aicode` 在裡面開幾個對話都是鎖在同一個專案,自然不會錯亂;換專案就重開一個 `aicode`。)

兩種情況都一樣的先決條件:這個 shell 先 `source <CODETRAIL_REPO>/.venv/bin/activate`(同 §1.3),且 §3 的 llama-server 都起好了。然後看你的機器有沒有桌面瀏覽器,挑下面一種。

#### 情況 A:這台機器自己有桌面瀏覽器

```bash
cd <PROJECT_TO_ANALYZE>
aicode web
```

啟動後會印出網址並自動開瀏覽器:

```
[aicode] web backend → http://127.0.0.1:4096
```

瀏覽器首頁就是 session 清單,點任一筆續問。沒自動開就手動把那行網址貼到瀏覽器。

#### 情況 B:這台是沒有桌面的遠端 server(常見:GPU 主機)

server 上沒瀏覽器,從你自己的裝置連進去。**推薦走 Tailscale**:給 server 一個固定網址,加到瀏覽器最愛點一下就進,不用每次手動開 SSH tunnel(tunnel 偶爾會斷、或忘了開)。全程維持 loopback、tailnet 內 WireGuard 加密、免設密碼。

**一次性設定:**

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

4. 在你自己的裝置開那個 Tailscale 最愛網址 → 就是 server 上的 session 清單,點一筆續問。

**沒裝 / 不想裝 Tailscale 的 fallback** —— SSH port-forward(每次都要開、斷了要重來):用你平常 SSH 進 server 的指令後面加 `-L`,再開本機 `http://127.0.0.1:4096`:

```bash
ssh -L 4096:127.0.0.1:4096 <你的帳號>@<server 位址>
```

#### (選用)用 TUI 接上同一個 backend

backend 沒關掉的狀態下,**在 server 上**另開一個終端:

```bash
aicode attach        # 預設接 http://127.0.0.1:4096
aicode attach -c     # 接上去並續接上一個 session
```

web 發問 TUI 看得到,TUI 發問 web 也看得到。CodeTrail MCP 只在 backend 起一次,attach 端不會再起第二個;TUI 內 `/status` 應看到 `codetrail Connected`。

#### 換 port / 要對外開放

- **換 port**:`AICODE_WEB_PORT=4097 scripts/start-web.sh`(或 `aicode web`)。記得 attach、Tailscale serve、SSH tunnel 的 `4096` 也要一起改成 `4097`(serve 重設:`tailscale serve --bg --https=4097 4097`)。
- **一般情況用情況 B 的 Tailscale(或 SSH tunnel)就好,不要綁 `0.0.0.0`。** 只有你真的要讓別台機器不透過 tailnet / SSH 直接連時才需要,而且 `aicode web` 會強制先設密碼,否則拒絕啟動:

  ```bash
  export OPENCODE_SERVER_PASSWORD=<強密碼>   # username 預設 opencode,可用 OPENCODE_SERVER_USERNAME 覆寫
  aicode web --hostname 0.0.0.0
  ```

  即使設了密碼,`0.0.0.0` 也只能綁在可信內網 / VPN 介面,不要對公網開放。

排查(版本太舊 / attach 連不上 / port 被占用)見 [docs/troubleshooting.md](docs/troubleshooting.md)。

---

## 必守安全界線

- `AICODE_ROOT` 是本次 frontend 可讀寫的 CodeTrail MCP sandbox 根目錄;不要從 `$HOME` 或 `/` 啟動。
- MCP server 啟動時會拒絕危險 root,並把工具限制在 `AICODE_ROOT` 內。
- `knowledge.json`、`knowledge_emb.npz`、`data/`、`.codetrail/`、`*.jsonl` 和 `.code_rag_cache_*` 通常含 NDA 片段,**不要 commit**。
- `apply_patch(...)` 有 context matching、max files、max lines 限制;不要放寬安全層。要完全關閉改檔,啟動時設 `AI_CODE_PATCH=0`。
- `run_command(...)` 只允許白名單命令,不支援 shell metacharacter。預設白名單只含測試 / lint;`make` / `cmake` / `ninja` / `meson` / `bazel build` 等 build 命令需要顯式 `AI_CODE_ENABLE_BUILD_COMMANDS=1` 才會掛上。要完全關閉命令執行,設 `AI_CODE_RUN_TESTS=0`。
- 遠端 llama-server 會收到 prompt、程式碼片段、spec 摘要與工具輸出,**只能指向可信內網 / VPN 主機**(llama-server 預設不檢查 API key)。
- `aicode web` backend 與 llama-server 同級:未設 `OPENCODE_SERVER_PASSWORD` 時 server 無認證,**只能暴露於可信內網 / VPN**。預設綁 `127.0.0.1`;非 loopback 或 `--mdns` 未設密碼時 `aicode web` 會拒絕啟動。

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
