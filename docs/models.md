# 模型設定與硬體取捨

這份文件整理如何挑 `<CODE_MODEL>`、固定附屬模型、context,以及換顯卡或遠端 llama-server 時的取捨。

[回到 README](../README.md)。

---

## 如何挑 `<CODE_MODEL>`(CodeTrail 不內建預設)

CodeTrail **不替你決定主聊天 / 程式推導模型**,也不會 fallback 任何固定 baseline。你必須自己下載一顆 GGUF 並啟動 llama-server,然後用下列任一方式告訴 CodeTrail(這幾個都沒設、或值是 `<CODE_MODEL>` 之類的 placeholder 時,`aicode` 會直接 fail-loud 拒絕啟動):

1. `AICODE_MODEL` 環境變數(最優先,例如 `export AICODE_MODEL=<CODE_MODEL>`)。
2. `aicode -m <CODE_MODEL>` / `--model <provider>/<CODE_MODEL>` CLI 旗標(custom provider prefix 會自動 strip;`openai/`、`anthropic/`、`ollama/` 這類已知外部 provider 會被拒)。
3. `~/.config/opencode/opencode.json` 的 `"model"` 欄位(`<provider>/<CODE_MODEL>` 形式,例如 `"llamacpp/qwen3-coder-32b"`)。

`<CODE_MODEL>` 可以是:

- **MODEL_REGISTRY 裡登記的 bare name**(推薦,例如 `qwen3-coder-32b`)
- **GGUF 絕對路徑**(例如 `/home/you/models/foo.gguf`)

Registry 維護在 `~/.config/codetrail/models.json`,格式:

```json
{
  "qwen3-coder-32b": "~/models/qwen2.5-coder-32b-instruct-q4_k_m.gguf",
  "qwen3-coder-30b": "~/models/qwen3-coder-30b-q4_k_m.gguf"
}
```

或用 env 直接塞 JSON: `AICODE_MODEL_REGISTRY='{"foo": "/path/foo.gguf"}'`。

下面只把用途分開,不是主模型推薦清單。請依硬體與任務自己挑:

| 模型 / 設定 | 用途 | 取捨 |
|---|---|---|
| `<CODE_MODEL>` | 主聊天 / 程式推導,掛在 llama-server :8080 | 由你自行選擇並下載;CodeTrail 不內建、不推薦、不 fallback |
| VL (例如 qwen3-vl, llava) | `analyze_file(...)` 處理截圖、UI error;`ingest_document(...)` 把圖片切 chunk 進 KB 也用它 | 掛在 :8083,需要 GGUF 主檔 + `mmproj-*.gguf`;不分析圖片就不用啟動 |
| `bge-m3` (RAG embedding) | `query_knowledge(...)` / `code_rag_search(...)` 的 embedding | 掛在 :8081,server 啟動旗標 `--embedding --pooling cls` |
| `bge-reranker-v2-m3` (RAG reranker) | RAG rerank cross-encoder | 掛在 :8082,server 啟動旗標 `--embedding --pooling rank --reranking`;沒掛時依 `AICODE_RERANK_FALLBACK_POLICY` 處理,預設 `embedding` 不打主模型 |

挑選方向(自己判斷):

- 要穩定完成「查證 → patch → test」:選一顆你已下載並實測過的 coding 模型。
- 任務跨很多檔、要比對規格或做設計判斷:選較大的模型,但先用較小 context 驗證 VRAM。
- 只想先看懂 repo 或做初步 review:選較小模型,確認 latency 與工具格式穩定後再固定下來。
- 要讀截圖或把圖片進 KB:啟動 VL server,讓 `analyze_file(...)` / `ingest_document(...)` 接過去。

Context 建議:

`AICODE_DYNAMIC_NUM_CTX_MAX` 控制 CodeTrail 內部每次能塞給模型的文字量上限(單位是 token,1 token 大約 3–4 個字元)。值越大可以一次給越多檔案內容或對話歷史,但**真正生效要 llama-server 啟動時的 `-c <N>` 也夠大** — server 端 `-c` 才是物理上限,CodeTrail 的 dynamic max 只是邏輯預算。

> 註:早期文件叫使用者調 `AICODE_NUM_CTX`,但那個變數在 dynamic mode 開啟(預設)時只是 banner 顯示與 dynamic-off fallback,不會真正影響 per-call ctx。要實際改 CodeTrail 端上限請用 `AICODE_DYNAMIC_NUM_CTX_MAX`;要改 server 端上限請重啟 llama-server 並調整 `-c <N>`。

```bash
# 30B 以下模型,server 開 -c 65536 對應:
AICODE_DYNAMIC_NUM_CTX_MAX=65536 aicode

# 35B 級模型先 32K 跑過再升:
AICODE_MODEL=<CODE_MODEL> AICODE_DYNAMIC_NUM_CTX_MAX=32768 aicode
```

判斷要不要升:

- 開新的視窗跑 `nvidia-smi` 看 VRAM 還剩多少。
- 跑 `curl -s http://localhost:8080/props | jq .default_generation_settings.n_ctx`,確認 server 真實 ctx。
- 跑 `curl -s http://localhost:8080/slots | jq` 觀察 slot 狀態。

128K(131072)需要 server 啟動時對應 `-c 131072` + VRAM 夠 + KV cache q8_0 / q4_0 量化。35B 級模型在 5090(32GB)上開 128K 通常會 OOM,先不要。

### 啟動時自動安全檢查

`aicode` 啟動時會跑 `scripts/ctx_safety_check.py`,讀主 llama-server `/props`,拿到 server 真實 `n_ctx`,跟 `AICODE_DYNAMIC_NUM_CTX_MAX` 比對。判定:

1. server 沒啟動 / 不可連 → `UNKNOWN`,放行,只 warn。
2. requested ≤ server n_ctx → `SAFE`。
3. requested > server n_ctx → `UNSAFE`,`exit 2` 拒絕啟動,並印對齊辦法。

刻意保守的設計:server 不可連時 graceful 放行,不會卡住 CI / 遠端 server。

三個 escape env var 用途:

| 變數 | 用途 | 何時用 |
|---|---|---|
| `AICODE_DYNAMIC_NUM_CTX_MAX=<N>` | 顯式指定 ctx 上限 | 安全閘建議的數字直接照用 |
| `AICODE_ACCEPT_CTX_RISK=1` | 知道會 truncate 也要跑 | 確認影響可接受、或要實測對比時用,一次性 |
| `AICODE_CTX_SAFETY_DISABLE=1` | 永久關掉檢查 | 你已經非常清楚自己在幹嘛、或在 CI 環境裡 |

`dynamic_num_ctx` 是「per-call 動態縮小」,server `-c` 是「物理上限」,兩者不互斥:dynamic 會根據實際 prompt 大小選 16K–`MAX` 之間的值(避免浪費 VRAM),但只要 prompt 夠大就會撞到 `MAX`,所以 `MAX` 必須 ≤ server `-c`。

---

## MoE 模型 + CPU offload(Qwen3-235B-A22B-Instruct-2507)

5090 32GB + 128GB+ RAM 可以跑 **Qwen3-235B-A22B-Instruct-2507 Q4_K_M(總 ~142GB)**,作法是把 MoE expert tensors offload 到 CPU,attention / KV cache / router 留在 GPU。`llama-server` 從 2025 中加入兩個關鍵旗標:

| 旗標 | 行為 | 適用情境 |
|---|---|---|
| `--cpu-moe` | 把**所有** layer 的 MoE expert tensors 放 CPU | 預設,VRAM 最省、最不會 OOM |
| `--n-cpu-moe N` | 把**前 N 層**的 expert 放 CPU(其餘留 GPU) | VRAM 還有空間時調小 N 換速度 |

5090 32GB 在 `--cpu-moe`(全 offload) 下,實測 VRAM 約 15-18GB(主 server only),剩下空間可同時掛 bge-m3 + bge-reranker-v2-m3。若不跑 embedding/reranker,可改 `--n-cpu-moe 84` 之類把 ~10 層 expert 留在 GPU,生成速度通常多 1.5-2x。

### 啟動範例

CodeTrail 沒有附 launcher script — 怎麼啟動 server fleet(tmux / systemd / 自己寫 shell wrapper)留給你自己。下面是一個 5090 + 235B Q4_K_M 的 reference 命令:

```bash
llama-server \
  -m ~/models/Qwen3-235B-A22B-Instruct-2507-GGUF/Q4_K_M/Qwen3-235B-A22B-Instruct-2507-Q4_K_M-00001-of-00003.gguf \
  --host 0.0.0.0 --port 8080 \
  -c 32768 \
  -ngl 999 \
  --cpu-moe \
  --no-mmap \
  --jinja \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  -fa on \
  --temp 0.7 --top-p 0.8 --top-k 20 --min-p 0.0
```

關鍵旗標:

- `-m ...-00001-of-00003.gguf` — 三檔切片只要指第一個,llama.cpp 自動接後續
- `--cpu-moe` — 全 offload。要換 `--n-cpu-moe N`(留越多 expert 在 GPU 越快),5090 32GB 加掛 embedding+reranker 同顆 GPU 時 `N=84` 左右是常見起點
- `--no-mmap` — **強烈建議搭 `--cpu-moe` 一起加**(見下方「mmap vs no-mmap 取捨」)
- `--jinja` — 使用模型內建 chat_template,tool calling 才會正確
- `-fa on` — flash attention(新版 llama.cpp 語法)
- `--cache-type-k q8_0 --cache-type-v q8_0` — KV cache 量化,32K ctx 約 1.5GB
- `--temp 0.7 --top-p 0.8 --top-k 20 --min-p 0.0` — Qwen 官方對 Instruct-2507 的建議 sampling

> `--cpu-moe` 與 `-fa on` 都是 2025 年中以後加入的 llama.cpp 旗標。如果 server 報 unknown option,先 `git pull && cmake --build build` 升級 llama.cpp。

### mmap vs no-mmap 取捨(MoE 必看)

預設 llama.cpp 用 `mmap` 把 GGUF 檔對應到 process 位址空間,**懶載入** —— 啟動很快(6 秒就 listening),但 weights 還沒實際讀進 RAM。

對 dense 模型沒差;對 MoE 模型搭 `--cpu-moe` 後**會在第一次推理觸到新 expert 時從 SSD page-in**,造成首字延遲(TTFT)爆炸,Qwen3-235B-A22B 實測首次 TTFT 可達 120 秒以上。llama-server 啟動時也會自己警告:

```
W llama_model_loader: tensor overrides to CPU are used with mmap enabled —
                      consider using --no-mmap for better performance
```

| | `mmap`(預設) | `--no-mmap` |
|---|---|---|
| 啟動時間 | ~6 秒(假載入) | 1.5–2.5 分鐘(真把 weights 全讀進 RAM) |
| 首字 TTFT | 60–120 秒(第一次) | 5–15 秒 |
| 後續對話 | 偶爾卡頓(冷 expert page-in) | 穩定 |
| RAM 占用 | ~5GB + page cache | ~135GB(235B Q4_K_M 體積) |

判斷:有 ≥ 150GB RAM 就加 `--no-mmap`,啟動慢一次換之後永久穩定。RAM 不到 150GB 就保持 mmap 接受偶爾卡頓,或換較小模型。

### 預期效能與資源(5090 32GB + DDR5 RAM 參考)

| 指標 | `--cpu-moe`(全 offload) | `--n-cpu-moe 84`(留 10 層 GPU) |
|---|---|---|
| 主 server VRAM | 15-18 GB | 28-30 GB |
| 系統 RAM | 130-140 GB(mmap)| 115-125 GB |
| Prompt ingest | 50-150 tok/s | 100-250 tok/s |
| 生成速度 | 6-12 tok/s | 12-20 tok/s |

實際數字會被 RAM 頻寬、PCIe gen、context 大小、batch size 拉動,以上是大致量級。第一次 prompt 會慢(冷 cache 載入 expert weights),之後 page cache 暖了就會快。

### Context 上限

- 模型原生支援 262K,但 KV cache 會吃 GPU。32K q8_0 KV cache 約 1.5GB,64K 約 3GB,128K 約 6GB。
- CodeTrail 內部 ctx 預算(`AICODE_DYNAMIC_NUM_CTX_MAX`)要 ≤ server `-c`,否則 `aicode` 啟動時 ctx safety check 會 `exit 2` 拒絕。
- 建議起點 32K,確認穩定後再升 64K(server `-c` 與 `AICODE_DYNAMIC_NUM_CTX_MAX` 必須一起改,後者不可超過前者,否則 `aicode` 的 ctx safety check 會 `exit 2`)。

> `--cpu-moe` 偏慢主要在 expert 經 PCIe / RAM bus 取值。改用 `--n-cpu-moe N`(N < 94)留越多 expert 在 GPU 越快;94 是 Qwen3-235B-A22B 的層數,N=94 等價於 `--cpu-moe`,N=0 等價於不 offload。

---

## Context overflow vs server truncation

模型「看不到」想看的內容,通常是兩種不同問題:

| 現象 | 性質 | 怎麼看 |
|---|---|---|
| CodeTrail 內部 hard gate 觸發 | 預先拒絕。Prompt 估算 > `MAX` 就拒送。 | `[CTX_OVERFLOW]` 訊息、`.codetrail/context_metrics.jsonl` |
| server 端 truncation | server `-c` 不夠,prompt 進到 server 之後被截掉前面 | server stderr 會印 `n_past > n_ctx`、模型回答忽略掉前段 |

兩條管線各自獨立:

| 路徑 | 控制方式 | 影響 |
|---|---|---|
| CodeTrail 內部 native call | `AICODE_DYNAMIC_NUM_CTX_MAX` (dynamic 預設開啟) | RAG strict、agent 工具迴圈、`query_knowledge_strict` |
| OpenCode TUI 主對話 | llama-server 啟動時的 `-c <N>` + OpenCode model limit.context | TUI 裡看到的主對話 |

要調 server-side context,需要**重啟 llama-server**(`-c` 是啟動旗標,改 env 沒用):

```bash
# 停掉舊的 server
pkill -f "llama-server.*--port 8080" || true

# 用新的 -c 啟動
llama-server -m ~/models/qwen2.5-coder-32b-instruct-q4_k_m.gguf \
  --host 0.0.0.0 --port 8080 \
  -c 131072 \
  -ngl 99 \
  --cache-type-k q8_0 --cache-type-v q8_0
```

`scripts/doctor.py` 會檢查 context 設定是否打架(opencode.json limit vs CodeTrail internal cap);它只報告,不會自動改設定。

`scripts/ctx_safety_check.py` 是另一個更窄的入口:`aicode` 啟動時自動跑,只看「主 server n_ctx vs requested ctx」,不夠就直接 `exit 2`。它需要 `AICODE_MODEL` 已設定;沒設也會直接 `exit 2`,不會 fallback。可以手動跑來測:

```bash
AICODE_MODEL=<CODE_MODEL> python scripts/ctx_safety_check.py
```

---

## 換顯卡 / 換模型

CodeTrail 的模型選擇以環境變數 + registry 為主,不需要改 source code。換到其他顯卡或其他機器時,先在那台主機 build / 部署 llama.cpp、下載 GGUF、啟動 4 個 server,再用同一個 `AICODE_MODEL` 啟動 `aicode`:

```bash
# 把新模型加進 registry
vi ~/.config/codetrail/models.json

# 重啟主 server (新 GGUF + 新 ctx)
llama-server -m ~/models/<NEW_MODEL>.gguf --host 0.0.0.0 --port 8080 -c 65536 -ngl 99 &

AICODE_MODEL=<NEW_MODEL_NAME> \
AICODE_DYNAMIC_NUM_CTX_MAX=32768 \
aicode
```

不要只在 OpenCode TUI 裡用 `/models` 換模型。那只會換前台 model id,不會 reload server,也不會通知 CodeTrail MCP server;後台的 `query_knowledge_strict` 等內部流程仍會用啟動時的 `AICODE_MODEL`。正確流程是退出 `aicode`、停掉舊的 server、用新 GGUF 重啟 server、改 `AICODE_MODEL`、重啟 `aicode`。

硬體起點(請把 `<CODE_MODEL>` 換成你實際下載的那顆 GGUF):

```bash
# 5090 32GB:32B Q4_K_M @ 64K + bge-m3 + bge-reranker 同時跑大概剛好
#   要塞更大 MoE 模型(例如 235B-A22B)可看「MoE 模型 + CPU offload」段落把 expert offload 到 RAM
AICODE_MODEL=<CODE_MODEL> AICODE_DYNAMIC_NUM_CTX_MAX=65536 aicode

# 24GB VRAM:30B 或 20B,context 32K
AICODE_MODEL=<CODE_MODEL> AICODE_DYNAMIC_NUM_CTX_MAX=32768 aicode

# 16GB 以下:自己選 14B / 20B Q4 或更小,不要硬開大 context
AICODE_MODEL=<CODE_MODEL> AICODE_DYNAMIC_NUM_CTX_MAX=16384 aicode
```

VRAM 不夠時的調整順序(由小痛到大痛):

1. 降低 `AICODE_DYNAMIC_NUM_CTX_MAX`。
2. 重啟 server 用更小的 `-c <N>`。
3. server 啟動加 `--cache-type-k q8_0 --cache-type-v q8_0`(KV cache 量化到 1 byte/element,容量減半)。
4. 降到 Q4 量化的 GGUF。
5. 換更小的模型。

這些後果要分清楚:

| 變更 | 主要後果 |
|---|---|
| 換小顯卡 | server 啟動就會 OOM 或退到部分 CPU offload(`-ngl` 調小) |
| 換小模型 | 速度快、硬體壓力小,但跨檔推理、工具呼叫和 patch 穩定度可能下降 |
| 換大模型 | 推理上限較高,但更慢、更吃 VRAM;context 不宜一開始就開太大 |
| context 開太大 | server 啟動 OOM 或 KV cache 太占 VRAM,sampling 變慢 |
| context 開太小 | 是正確性問題,長 spec / 大 repo 可能塞不進 prompt;CodeTrail 會用 `[CTX_OVERFLOW]` 阻止 silent truncation |

如果 llama-server 跑在另一台 GPU 主機上,CodeTrail 這台可以把 URL 指過去。`scripts/start-rag-servers.sh` 也讀同一組 `AICODE_LLAMA_EMBED_BASE_URL` / `AICODE_LLAMA_RERANK_BASE_URL`,所以 client 和 launcher 不需要兩套 port 設定:

```bash
AICODE_LLAMA_BASE_URL=http://<GPU_HOST>:8080 \
AICODE_LLAMA_EMBED_BASE_URL=http://<GPU_HOST>:8081 \
AICODE_LLAMA_RERANK_BASE_URL=http://<GPU_HOST>:8082 \
AICODE_LLAMA_VL_BASE_URL=http://<GPU_HOST>:8083 \
AICODE_MODEL=<CODE_MODEL> \
AICODE_DYNAMIC_NUM_CTX_MAX=32768 \
aicode
```

同時也要把 `~/.config/opencode/opencode.json` 裡 provider 的 `baseURL` 改到同一台主機:

```json
"baseURL": "http://<GPU_HOST>:8080/v1"
```

Reranker 不可用時的行為由 `AICODE_RERANK_FALLBACK_POLICY` 控制:

| policy | 行為 |
|---|---|
| `embedding`(預設) | 保留 embedding / hybrid 既有排序,不呼叫主模型 |
| `main_model` | RAG 知識庫還原舊行為,用主聊天模型 rerank;Code RAG 沒有這條路徑,等同 `embedding` |
| `error` | 專用 reranker 不可用或呼叫失敗時直接報錯 |

`main_model` 有成本風險:嚴格模式下每條符合條件的 query 都可能打主模型做 rerank。公開 / 多硬體部署建議維持預設 `embedding`,除非使用者明確選擇舊行為。

NDA 場景要特別注意:遠端 server 會收到 prompt、程式碼片段、spec 摘要與工具輸出。只把它指向你信任的內網 / VPN 主機,不要把 llama-server 暴露到公開網路(llama-server 預設不檢查 API key)。

RAG 相關模型也要在新機器上準備好 GGUF 並啟動對應 server。若用 repo 內建 launcher,可用 `MODELS_DIR` 指模型根目錄,或用 `EMBED_MODEL` / `RERANK_MODEL` 指完整 GGUF 路徑;找不到預設檔名時,launcher 會 glob `bge-m3*.gguf` / `bge-reranker-v2-m3*.gguf`。`RAG_HEALTH_TIMEOUT` 控制 `/health` 等待秒數(預設 60)。GPU placement 可用既有 `CUDA_VISIBLE_DEVICES`,或每顆 server 分別用 `EMBED_GPU` / `RERANK_GPU`。

```bash
# embedding (必要) — embedding 模型不要量化,Q4 會明顯影響召回,用 f16
llama-server -m ~/models/bge-m3/bge-m3-f16.gguf \
  --host 0.0.0.0 --port 8081 -c 8192 --embedding --pooling cls -ngl 99 &

# reranker (建議啟動;沒掛時預設保留 embedding 排序,不呼叫主模型)
llama-server -m ~/models/bge-reranker-v2-m3/bge-reranker-v2-m3-Q4_K_M.gguf \
  --host 0.0.0.0 --port 8082 -c 8192 --embedding --pooling rank --reranking -ngl 99 &
```

如果要分析截圖、UI error 或把圖片匯入 KB,再啟動 VL server:

```bash
# VL (選用) — 需要 main GGUF + mmproj
llama-server -m ~/models/<VL_MODEL>.gguf \
  --mmproj ~/models/mmproj-<VL_MODEL>.gguf \
  --host 0.0.0.0 --port 8083 -c 8192 -ngl 99 &
```

---
