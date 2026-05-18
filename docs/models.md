# 模型設定與硬體取捨

這份文件整理如何挑 `<CODE_MODEL>`、固定附屬模型、context，以及換顯卡或遠端 Ollama 時的取捨。

[回到 README](../README.md)。

---

## 如何挑 `<CODE_MODEL>`（CodeTrail 不內建預設）

CodeTrail **不替你決定主聊天 / 程式推導模型**，也不會 fallback 任何固定 baseline。你必須自己 `ollama pull` 一顆 Ollama 模型，然後用下列任一方式告訴 CodeTrail（這幾個都沒設、或值是 `<CODE_MODEL>` 之類的 placeholder 時，`aicode` 會直接 fail-loud 拒絕啟動）：

1. `AICODE_MODEL` 環境變數（最優先，例如 `export AICODE_MODEL=<CODE_MODEL>`）。
2. `aicode -m <CODE_MODEL>` / `--model ollama/<CODE_MODEL>` CLI 旗標（接受 `ollama/<MODEL>` 或 bare Ollama model name；非 Ollama provider 會被拒）。
3. `~/.config/opencode/opencode.json` 的 `"model"` 欄位（必須是 `ollama/<CODE_MODEL>`）。

下面只把用途分開，不是主模型推薦清單。請依硬體與任務自己挑：

| 模型 / 設定 | 用途 | 取捨 |
|---|---|---|
| `<CODE_MODEL>` | 主聊天 / 程式推導模型 | 由你自行選擇並 pull；CodeTrail 不內建、不推薦、不 fallback |
| `qwen3-vl:30b-a3b` | `analyze_file(...)` 處理截圖、UI error；`ingest_document(...)` 把圖片切 chunk 進 KB 也用它 | 不是主要 coding model；不分析圖片、也不把圖片進 KB 就不用 pull |
| `bge-m3` (CodeTrail 內部固定附屬模型) | `query_knowledge(...)` / `code_rag_search(...)` 的 embedding | 不是聊天模型，不要在 OpenCode model selector 裡選 |
| `qllama/bge-reranker-v2-m3` (CodeTrail 內部固定附屬模型) | RAG rerank | 同上；模型未 pull 時 RAG 品質會下降或報錯 |

挑選方向（自己判斷）：

- 要穩定完成「查證 → patch → test」：選一顆你已 pull 並實測過的 coding 模型。
- 任務跨很多檔、要比對規格或做設計判斷：選較大的模型，但先用較小 context 驗證 VRAM。
- 只想先看懂 repo 或做初步 review：選較小模型，確認 latency 與工具格式穩定後再固定下來。
- 要讀截圖或把圖片進 KB：保留主聊天模型不變，讓 `analyze_file(...)` / `ingest_document(...)` 使用 `qwen3-vl:30b-a3b`。

Context 建議：

`AICODE_DYNAMIC_NUM_CTX_MAX` 控制每次能塞給模型的文字量上限（單位是 token，1 token 大約 3–4 個字元）。值越大可以一次給越多檔案內容或對話歷史，但模型在 VRAM 裡要額外佔的空間也越大。

> 註：早期文件叫使用者調 `AICODE_NUM_CTX`，但那個變數在 dynamic mode 開啟（預設）時只是 banner 顯示與 dynamic-off fallback，不會真正影響 per-call ctx。要實際改上限請用 `AICODE_DYNAMIC_NUM_CTX_MAX`。

```bash
# 30B 以下模型：直接 64K 通常最穩
AICODE_DYNAMIC_NUM_CTX_MAX=65536 aicode

# 35B 級模型：先用 32K 跑一次，確認沒問題再升
AICODE_MODEL=<CODE_MODEL> AICODE_DYNAMIC_NUM_CTX_MAX=32768 aicode
```

判斷要不要升上去：

- 開新的視窗跑 `ollama ps`，看載入中模型那行的 `PROCESSOR` 欄位。
- 顯示 `100% GPU`：模型完全放在顯卡裡，速度正常，可以考慮把 `AICODE_DYNAMIC_NUM_CTX_MAX` 升到 65536 再跑一輪。
- 顯示 `xx% CPU / xx% GPU`：顯卡記憶體不夠，模型有一部分被搬到一般記憶體跑，回應會明顯變慢（首字出來特別久）。這時把 `AICODE_DYNAMIC_NUM_CTX_MAX` 改小一點再啟動。

128K（131072）需要顯卡記憶體 + 系統記憶體都很充裕才合理，35B 級的模型不建議直接開到 128K。

### 啟動時自動安全檢查

`aicode` 啟動時會跑 `scripts/ctx_safety_check.py`，事先預估「目前模型 + 目前 GPU + 要求的 ctx 上限」會不會把 Ollama 推到 CPU offload。預估方式：

1. 跑 `nvidia-smi` 拿到 GPU 總 VRAM。
2. 查 Ollama `/api/show` 取得模型架構（層數、attention heads、KV head 數、key/value length、quantization、SSM hybrid 比例）。
3. 用標準 transformer KV cache 公式算 `weights_bytes + kv_per_token × requested_ctx + 2GB headroom`，跟總 VRAM 比。
4. 超過就 `UNSAFE`、`exit 2` 拒絕啟動；同時印出建議的安全 cap 數字。

刻意保守的設計：拿不到 `nvidia-smi` / Ollama / 模型 metadata 任一項 → 一律回 `UNKNOWN` 放行，只 warn 不擋（CI、遠端 Ollama、新模型出來時不會被卡住）。

三個 escape env var 用途：

| 變數 | 用途 | 何時用 |
|---|---|---|
| `AICODE_DYNAMIC_NUM_CTX_MAX=<N>` | 顯式指定 ctx 上限 | 安全閘建議的數字直接照用，這是預期解法 |
| `AICODE_ACCEPT_CTX_RISK=1` | 知道會 offload 也要跑 | 確認 offload 影響可接受、或要實測對比時用，一次性 |
| `AICODE_CTX_SAFETY_DISABLE=1` | 永久關掉檢查 | 你已經非常清楚自己在幹嘛、或在 CI / 自動化環境裡，不想看到 banner |

`dynamic_num_ctx` 是「per-call 動態縮小」，安全檢查 cap 是「物理上限」，兩者不互斥：dynamic 會根據實際 prompt 大小選 16K–`MAX` 之間的值（避免浪費 VRAM），但只要 prompt 夠大就會撞到 `MAX`，所以 `MAX` 必須 fit 在 VRAM 裡，這就是安全檢查在守的東西。

---

## Context / Offload 判斷

模型「看不到」想看的內容，通常是兩種不同問題：

| 現象 | 性質 | 怎麼看 |
|---|---|---|
| Context overflow | 正確性問題。Prompt 太大，模型實際沒讀到完整內容。 | CodeTrail 的 `[CTX]` log、`.codetrail/context_metrics.jsonl`、`[CTX_OVERFLOW]` |
| CPU/GPU offload | 速度問題。權重或 KV cache 被搬到 RAM。 | 啟動時 `[ctx-safety] UNSAFE` 預測；對話途中 `[CTX] runtime: ... → 模型已 offload` 實測；隨時 `ollama ps` 的 `PROCESSOR` 欄 |

CodeTrail 與 OpenCode TUI 的 context 也不是同一條管線：

| 路徑 | 控制方式 | 影響 |
|---|---|---|
| CodeTrail 內部 native call | `AICODE_DYNAMIC_NUM_CTX_MAX`（dynamic 預設開啟） | RAG strict、agent 工具迴圈、`query_knowledge_strict` |
| OpenCode TUI 主對話 | Ollama server 的 `OLLAMA_CONTEXT_LENGTH` + OpenCode model limit | TUI 裡看到的主對話 |

要調 OpenCode TUI 的 server-side context，需要重啟 Ollama：

```bash
OLLAMA_CONTEXT_LENGTH=65536 \
OLLAMA_FLASH_ATTENTION=1 \
OLLAMA_KV_CACHE_TYPE=q8_0 \
OLLAMA_NUM_PARALLEL=1 \
ollama serve
```

systemd 版本：

```bash
sudo systemctl edit ollama.service
```

```ini
[Service]
Environment="OLLAMA_CONTEXT_LENGTH=65536"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_NUM_PARALLEL=1"
```

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
ollama ps
```

`scripts/doctor.py` 會檢查 context 設定是否打架，以及 `ollama ps` 上的模型是否 CPU/GPU split；它只報告，不會自動改設定。

`scripts/ctx_safety_check.py` 是另一個更窄的入口：`aicode` 啟動時自動跑，只看「目前模型 + 目前 GPU + 要求的 ctx 上限」會不會 offload，不安全會直接 `exit 2` 擋下啟動。它需要 `AICODE_MODEL` 已設定（CodeTrail 不假定任何預設主模型）；沒設也會直接 `exit 2`，不會 fallback。可以手動跑來測：

```bash
AICODE_MODEL=<CODE_MODEL> python scripts/ctx_safety_check.py
```

---


## 換顯卡 / 換模型

CodeTrail 的模型選擇以環境變數為主，不需要改 source code。換到其他顯卡或其他機器時，先在那台 Ollama 主機下載模型，再用同一個 `AICODE_MODEL` 啟動 `aicode`，讓 OpenCode TUI 與 MCP server 內部呼叫保持一致：

```bash
ollama pull <CODE_MODEL>

AICODE_MODEL=<CODE_MODEL> \
AICODE_DYNAMIC_NUM_CTX_MAX=32768 \
aicode
```

不要只在 OpenCode TUI 裡用 `/models` 換模型。那只會換前台對話模型，不會通知 CodeTrail MCP server；後台的 `query_knowledge_strict` 等內部流程仍會使用啟動時的 `AICODE_MODEL`。正確流程是退出 `aicode`，改環境變數，重新啟動。

硬體起點（請把 `<CODE_MODEL>` 換成你實際 pull 的那顆 tag）：

```bash
# 32GB VRAM：35B 先用 32K，穩定後再試 64K (示意 — 自己挑模型)
AICODE_MODEL=<CODE_MODEL> AICODE_DYNAMIC_NUM_CTX_MAX=32768 aicode

# 24GB VRAM：優先用 30B / 24B，或把 35B context 降低
AICODE_MODEL=<CODE_MODEL> AICODE_DYNAMIC_NUM_CTX_MAX=32768 aicode

# 16GB 以下：自己選 20B / 24B 或更小模型，不要硬開大 context
AICODE_MODEL=<CODE_MODEL> AICODE_DYNAMIC_NUM_CTX_MAX=16384 aicode
```

換硬體或模型後，用另一個終端機看 Ollama 實際載入狀態：

```bash
ollama ps
```

`PROCESSOR` 欄位的判斷：

- `100% GPU`：模型與 KV cache 都放在顯卡，速度正常。
- `xx% CPU / xx% GPU`：VRAM 不夠，部分資料被搬到系統 RAM，通常首 token 會明顯變慢。先把 `AICODE_DYNAMIC_NUM_CTX_MAX` 從 `65536` 降到 `32768`，再不行降到 `16384`。

這些後果要分清楚：

| 變更 | 主要後果 |
|---|---|
| 換小顯卡 | 可能 CPU/GPU split、變慢、甚至 OOM；先降低 `AICODE_DYNAMIC_NUM_CTX_MAX` |
| 換小模型 | 速度快、硬體壓力小，但跨檔推理、工具呼叫和 patch 穩定度可能下降 |
| 換大模型 | 推理上限較高，但更慢、更吃 VRAM；context 不宜一開始就開太大 |
| context 開太大 | 多半是速度問題，容易 offload 到 RAM；用 `ollama ps` 看得出來 |
| context 開太小 | 是正確性問題，長 spec / 大 repo 可能塞不進 prompt；CodeTrail 會用 `[CTX_OVERFLOW]` 阻止 silent truncation |

如果 Ollama 跑在另一台 GPU 主機上，CodeTrail 這台可以把 API 指過去：

```bash
AICODE_OLLAMA_BASE_URL=http://<GPU_HOST>:11434 \
AICODE_MODEL=<CODE_MODEL> \
AICODE_DYNAMIC_NUM_CTX_MAX=32768 \
aicode
```

同時也要把 `~/.config/opencode/opencode.json` 裡 Ollama provider 的 `baseURL` 改到同一台主機：

```json
"baseURL": "http://<GPU_HOST>:11434/v1"
```

NDA 場景要特別注意：遠端 Ollama 會收到 prompt、程式碼片段、spec 摘要與工具輸出。只把它指向你信任的內網 / VPN 主機，不要把 Ollama API 暴露到公開網路。

RAG 相關模型也要在新機器上準備好：

```bash
ollama pull bge-m3
ollama pull qllama/bge-reranker-v2-m3
```

如果要分析截圖、UI error 或把圖片匯入 KB，再下載視覺模型：

```bash
ollama pull qwen3-vl:30b-a3b
```

---
