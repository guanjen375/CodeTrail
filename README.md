# CodeTrail - OpenCode + llama.cpp 本地 MCP 工作台

CodeTrail 是一個給 OpenCode 使用的本地 MCP 後端。你在 OpenCode TUI 裡提問,模型可以透過 CodeTrail 讀專案、找程式碼、查已匯入的 spec、分析截圖或 binary、產生 patch,並在允許的白名單內跑驗證命令。

CodeTrail 目前定位是**成熟私有部署版**:適合本機、離線、NDA / firmware / private repo 分析;**不打算公開發布**成 PyPI package、Docker image 或 SaaS。安全邊界有自動測試保護,但未做公開產品級安全審計。

底層推理引擎使用 [llama.cpp](https://github.com/ggerganov/llama.cpp) `llama-server`(自己 build,需要 CUDA)。所有 LLM / embedding / reranker / VL 走它的 HTTP API。

讀完這份 README 從零走到能進 OpenCode TUI 跟模型對話,大約需要:
- 一次性安裝 + build:30–60 分鐘
- 下載模型 GGUF:依網速與所選模型,5 分鐘到數小時不等

---

## 硬體與模型對照速查

`<CODE_MODEL>` 是 README / docs 全文使用的**佔位符**,代表你**自己選的主聊天 / 程式推導模型**。CodeTrail 不內建、不推薦、也不 fallback —— 你看完下表自己挑一顆 GGUF,後面所有步驟把 `<CODE_MODEL>` 換成你選的那顆。

| 你的 VRAM | 可選 `<CODE_MODEL>` 量級 | 量化建議 | llama-server 額外旗標 |
|---|---|---|---|
| 48GB+(A6000 / RTX 6000 Ada) | Qwen3-235B-A22B-Instruct-2507 整顆塞 GPU,或 Qwen3-Coder-32B Q5/Q6 | Q4_K_M / Q5_K_M | `--jinja` |
| 32GB(RTX 5090) | Qwen3-235B-A22B-Instruct-2507(MoE,135GB)+ CPU offload | Q4_K_M | `--jinja --cpu-moe --no-mmap` |
| 24GB(RTX 4090 / 3090) | 30B 級 dense 模型,或 Qwen3-30B-A3B(MoE)+ 部分 offload | Q4_K_M | `--jinja`(若選 MoE 加 `--cpu-moe`) |
| 16GB(RTX 4080 / 5070) | 14B / 20B 級 dense | Q4_K_M | `--jinja` |
| 12GB 以下 | 7B / 8B / 14B 級,可能要 Q3 或部分 CPU offload | Q4 / Q3_K | `--jinja`(必要時 `-ngl <N>` 控制 GPU 層數) |

下方步驟以 **RTX 5090 32GB + 170GB DDR5 RAM + Qwen3-235B-A22B-Instruct-2507 Q4_K_M(`--cpu-moe`)** 為走廊範例。其他硬體照同樣流程,把 `<CODE_MODEL>` 對應的 GGUF 名稱與 server 旗標換掉即可。額外的硬體取捨、context 大小、KV cache 量化、遠端 GPU 主機等深入內容見 [docs/models.md](docs/models.md)。

---

## 1. 安裝依賴

### 1.1 系統工具

- **Python 3.10+**(後續用 `python` 代表你的 Python 3,系統若只有 `python3` 把指令改掉即可)
- **Node.js LTS + npm**(裝 OpenCode 需要)
- **git**
- **ripgrep** `rg`(建議但非必要,搜尋會快很多)
- **tmux**(本文件用它在背景跑 3 個 llama-server;不熟可在這一步先 `sudo apt install tmux`,後面有 5 行教學)

### 1.2 安裝 OpenCode

```bash
npm install -g opencode-ai
command -v opencode    # 確認可被找到
```

### 1.3 安裝 CodeTrail Python 依賴

```bash
cd <CODETRAIL_REPO>
pip install -r requirements.txt
pip install mcp pymupdf4llm
```

`<CODETRAIL_REPO>` 是這個 CodeTrail 的 repo 路徑,不是你要分析的專案路徑。

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

```bash
git clone https://github.com/ggerganov/llama.cpp
cd llama.cpp
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

### 2.1 安裝 hf-transfer 加速

預設 `huggingface-cli download` 走單連線,實測 ~12 MB/s;裝 `hf-transfer` 後可以拉到 ~270 MB/s(視網路與 HF CDN 上限):

```bash
pip install --user --break-system-packages hf-transfer
python -c "import hf_transfer; print('hf-transfer', hf_transfer.__version__)"
```

`--break-system-packages` 是 Ubuntu 24.04 (PEP 668) 必要的旗標;不用害怕,只是用 `--user` 安裝到家目錄,不會動到系統 Python。

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

### 2.3 下載 RAG 必要模型

CodeTrail 的 RAG / Code-RAG 內建固定使用 `bge-m3`(embedding)和 `bge-reranker-v2-m3`(reranker),這兩個體積很小:

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

## 3. 啟動 3 個 llama-server(用 tmux 跑在背景)

CodeTrail 預期 4 個角色各自一個 `llama-server` instance,**main / embedding / reranker 三個是 RAG + 程式對話必要的,VL 選用**。會分開是因為 `llama-server` 一次只能載一顆 GGUF,不同角色用不同模型 / 不同模式(`--embedding` / `--reranking` / `--jinja` / `--mmproj`),所以必須開不同 process。

| Port | 角色 | 模型 | 必要 |
|---|---|---|---|
| 8080 | main(聊天、推理、工具呼叫) | `<CODE_MODEL>` | 是 |
| 8081 | embedding(算向量,RAG 搜相似段落) | `bge-m3` | 是 |
| 8082 | reranker(RAG 結果重排) | `bge-reranker-v2-m3` | 是 |
| 8083 | VL(看截圖 / 圖片) | `qwen3-vl` 等 | 否 |

下面用 tmux 一個 session 開 3 個 pane,3 個 server 各跑一個,detach 之後 server 留在背景,terminal 關掉也不會死。

### 3.1 開 tmux session

```bash
tmux new -s codetrail
```

進去之後 tmux 最小操作:

- `Ctrl-b "` 把目前 pane 水平切成兩半
- `Ctrl-b %` 垂直切
- `Ctrl-b ↑ / ↓ / ← / →` 在 pane 間移動
- `Ctrl-b d` detach(server 繼續活著)
- `tmux attach -t codetrail` 之後接回來
- `tmux kill-session -t codetrail` 全部停掉

按 `Ctrl-b "` 再 `Ctrl-b "` 切出 3 個 pane,接下來每個 pane 跑一個 server。

### 3.2 Pane 0 — 主 server(:8080)

5090 + Qwen3-235B-A22B-Instruct-2507 Q4_K_M(`--cpu-moe` MoE expert 卸到 CPU RAM):

```bash
~/llama.cpp/build/bin/llama-server \
  -m ~/models/Qwen3-235B-A22B-Instruct-2507-GGUF/Q4_K_M/Qwen3-235B-A22B-Instruct-2507-Q4_K_M-00001-of-00003.gguf \
  --host 0.0.0.0 --port 8080 \
  -c 65536 -ngl 99 --jinja \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --cpu-moe \
  --no-mmap
```

旗標說明:

- `-m ...-00001-of-00003.gguf` —— 只指 shard 1,llama.cpp 自動接後續
- `-c 65536` —— context 上限 64K token(模型原生 256K,KV cache 會吃 VRAM,先從 64K 起)
- `-ngl 99` —— 嘗試把所有層放 GPU(MoE expert 之後會被 `--cpu-moe` 拉回 CPU)
- `--jinja` —— 啟用模型內建 chat template,tool calling 才會走對格式
- `--cache-type-k/v q8_0` —— KV cache 量化到 8-bit,64K ctx 約省一半 VRAM
- `--cpu-moe` —— **MoE 模型才加**;把 expert tensors 卸到 CPU RAM(吃 ~130GB RAM)
- `--no-mmap` —— **強烈建議搭 `--cpu-moe` 一起加**;不加的話用 mmap 懶載入,第一次推理 TTFT 可能 1–2 分鐘(每次觸到新 expert 都要從 SSD page-in);加了之後啟動時把 weights 全讀進 RAM,啟動慢 1–2 分鐘但之後對話穩定

非 MoE 模型(dense 30B / 14B / 7B)**不要加** `--cpu-moe` 與 `--no-mmap`,直接拿掉那兩行即可。

啟動後等到看到這行才算成功:

```
srv  llama_server: server is listening on http://0.0.0.0:8080
```

`--no-mmap` 模式下大約 1.5–2.5 分鐘(看 SSD 速度),期間會看到一堆 `load_tensors:` 滾過。

### 3.3 Pane 1 — embedding server(:8081)

`Ctrl-b ↓` 切到下一個 pane:

```bash
~/llama.cpp/build/bin/llama-server \
  -m ~/models/bge-m3/bge-m3-f16.gguf \
  --host 0.0.0.0 --port 8081 \
  -c 8192 --embedding --pooling cls -ngl 99
```

5–10 秒內 `server is listening on http://0.0.0.0:8081`。

### 3.4 Pane 2 — reranker server(:8082)

再切到第三個 pane:

```bash
~/llama.cpp/build/bin/llama-server \
  -m ~/models/bge-reranker-v2-m3/bge-reranker-v2-m3-Q4_K_M.gguf \
  --host 0.0.0.0 --port 8082 \
  -c 8192 --reranking -ngl 99
```

同樣 5–10 秒就 listening。

### 3.5 Detach 並驗活

按 `Ctrl-b d` 把 tmux session detach,terminal 回到原本的 shell,3 個 server 留在背景。隨時 `tmux attach -t codetrail` 接回去看 log。

驗 3 個 server 都通:

```bash
for p in 8080 8081 8082; do
  echo ":$p → $(curl -s -o /dev/null -w '%{http_code}' http://localhost:$p/health)"
done
# 應該都印 200
```

VRAM 與 RAM 預期占用(5090 + 235B `--cpu-moe --no-mmap`):

```
VRAM  ~14 GB / 32 GB    (主 ~12.5 + embed ~1 + rerank ~0.4)
RAM   ~135 GB / 170 GB  (主要是 235B MoE expert 整個讀進 RAM)
```

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

## 換模型 / 換顯卡

把新 GGUF 加進 `~/.config/codetrail/models.json`,停掉舊的主 server,用新 GGUF 重啟 server(對應修改 `-c` 與 server 旗標),退出 `aicode`,用新的 `AICODE_MODEL` 重啟 `aicode`。**不要只在 TUI 裡用 `/models` 切換** —— 那只會換 OpenCode 前台 model id,不會 reload server,也不會同步 CodeTrail 內部呼叫。

如果 llama-server 跑在另一台 GPU 主機上:

```bash
AICODE_LLAMA_BASE_URL=http://<GPU_HOST>:8080 \
AICODE_LLAMA_EMBED_BASE_URL=http://<GPU_HOST>:8081 \
AICODE_LLAMA_RERANK_BASE_URL=http://<GPU_HOST>:8082 \
AICODE_LLAMA_VL_BASE_URL=http://<GPU_HOST>:8083 \
AICODE_MODEL=<CODE_MODEL> \
AICODE_DYNAMIC_NUM_CTX_MAX=32768 \
aicode
```

同時把 `~/.config/opencode/opencode.json` 的 provider `baseURL` 改成 `http://<GPU_HOST>:8080/v1`。完整換機 / 多 GPU / context 調整見 [docs/models.md](docs/models.md)。

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
