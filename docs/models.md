# 模型與硬體建議

這份文件整理主模型、embedding、reranker、視覺模型、context，以及換顯卡或遠端 Ollama 時的建議。

[回到 README](../README.md)。

---

## 模型比較

下面比較的是這個 repo 的 OpenCode 設定檔已列出的模型，以及 CodeTrail 內部會用到的 RAG / 視覺模型。

| 模型 | 建議用途 | 優點 | 注意事項 |
|---|---|---|---|
| `qwen3-coder:30b` | 預設主力；讀 repo、改 code、產 patch、跑驗證閉環 | coding 能力和工具使用穩定度最均衡；適合作為日常預設 | 比 20B 模型慢；長工具鏈任務建議拆成「先查證、再修改」 |
| `qwen3.6:35b-a3b-q4_K_M` | 跨檔推理、規格 vs 實作比對、較複雜重構 | 推理上限較高；大 context 任務表現較好 | 顯卡 32GB 的話第一次跑先設 `AICODE_DYNAMIC_NUM_CTX_MAX=32768`，用一陣子沒問題再升到 `65536`。**不要**用 `qwen3.6:35b-a3b-coding-nvfp4`（macOS 限定的版本，Linux 拉會報錯 412） |
| `devstral:24b` | 快速 code review、找 bug、簡單 patch | 速度和 coding 能力平衡；回答通常直接 | 工具呼叫格式不一定比 Qwen Coder 穩；大型修改前建議切回 Qwen |
| `gpt-oss:20b` | 快速理解陌生 repo、摘要、初步定位 | 輕量、啟動快、硬體壓力低 | 複雜改檔與長工具鏈較弱；適合探索，不適合作為最終 patch 主力 |
| `qwen3-vl:30b-a3b` | `analyze_file(...)` 處理截圖、UI error；`ingest_document(...)` 把圖片切 chunk 進 KB 也用它 | 讀圖中文字與畫面資訊較好 | 不是主要 coding model；不分析圖片、也不把圖片進 KB 就不用 pull |
| `bge-m3` | `query_knowledge(...)` / `code_rag_search(...)` 的 embedding | 多語檢索穩定；中文 spec 與英文程式碼混用時有幫助 | 不是聊天模型，不要在 OpenCode model selector 裡選 |
| `qllama/bge-reranker-v2-m3` | RAG rerank | 能改善 spec 查詢排序，降低抓到弱相關 chunk 的機率 | 會增加查詢延遲；模型未 pull 時 RAG 品質會下降或報錯 |

實務選法：

- 要穩定完成「查證 -> patch -> test」：用 `qwen3-coder:30b`。
- 任務跨很多檔、要比對規格或做設計判斷：用 `qwen3.6:35b-a3b-q4_K_M`（Linux 上能用的版本；`qwen3.6:35b-a3b-coding-nvfp4` 是 macOS 限定，不要用）。
- 只想先看懂 repo 或做初步 review：用 `gpt-oss:20b` 或 `devstral:24b`。
- 要讀截圖或把圖片進 KB：保留主聊天模型不變，讓 `analyze_file(...)` / `ingest_document(...)` 使用 `qwen3-vl:30b-a3b`。

Context 建議：

`AICODE_DYNAMIC_NUM_CTX_MAX` 控制每次能塞給模型的文字量上限（單位是 token，1 token 大約 3–4 個字元）。值越大可以一次給越多檔案內容或對話歷史，但模型在 VRAM 裡要額外佔的空間也越大。

> 註：早期文件叫使用者調 `AICODE_NUM_CTX`，但那個變數在 dynamic mode 開啟（預設）時只是 banner 顯示與 dynamic-off fallback，不會真正影響 per-call ctx。要實際改上限請用 `AICODE_DYNAMIC_NUM_CTX_MAX`。

```bash
# 30B 以下模型：直接 64K 通常最穩
AICODE_DYNAMIC_NUM_CTX_MAX=65536 aicode

# 35B 級的模型（如 qwen3.6:35b-a3b-q4_K_M）：先用 32K 跑一次，確認沒問題再升
AICODE_MODEL=qwen3.6:35b-a3b-q4_K_M AICODE_DYNAMIC_NUM_CTX_MAX=32768 aicode
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

`scripts/ctx_safety_check.py` 是另一個更窄的入口：`aicode` 啟動時自動跑，只看「目前模型 + 目前 GPU + 要求的 ctx 上限」會不會 offload，不安全會直接 `exit 2` 擋下啟動。可以手動跑來測：

```bash
AICODE_MODEL=qwen3.6:35b-a3b-q4_K_M python scripts/ctx_safety_check.py
```

---


## 換顯卡 / 換模型建議

CodeTrail 的模型選擇以環境變數為主，不需要改 source code。換到其他顯卡或其他機器時，先在那台 Ollama 主機下載模型，再用同一個 `AICODE_MODEL` 啟動 `aicode`，讓 OpenCode TUI 與 MCP server 內部呼叫保持一致：

```bash
ollama pull <MODEL>

AICODE_MODEL=<MODEL> \
AICODE_DYNAMIC_NUM_CTX_MAX=32768 \
aicode
```

不要只在 OpenCode TUI 裡用 `/models` 換模型。那只會換前台對話模型，不會通知 CodeTrail MCP server；後台的 `query_knowledge_strict` 等內部流程仍會使用啟動時的 `AICODE_MODEL`。正確流程是退出 `aicode`，改環境變數，重新啟動。

常見組合：

```bash
# 5090 / 32GB VRAM：35B 先用 32K，穩定後再試 64K
AICODE_MODEL=qwen3.6:35b-a3b-q4_K_M AICODE_DYNAMIC_NUM_CTX_MAX=32768 aicode

# 24GB VRAM：優先用 30B / 24B，或把 35B context 降低
AICODE_MODEL=qwen3-coder:30b AICODE_DYNAMIC_NUM_CTX_MAX=32768 aicode

# 16GB 以下：建議用 20B / 24B 或更小模型，不要硬開大 context
AICODE_MODEL=gpt-oss:20b AICODE_DYNAMIC_NUM_CTX_MAX=16384 aicode
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
AICODE_MODEL=<MODEL> \
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
