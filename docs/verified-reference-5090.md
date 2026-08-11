# Verified reference：RTX 5090＋170GB RAM

[回到 README](../README.md)。

這份文件是 `verified-reference` deployment profile 的量測記錄。它是明確選用的
reference，不是 CodeTrail 全域預設，也不是 H200＋RTX 2000 Ada 的推估。

## 已驗證範圍

- GPU：RTX 5090 32GB
- System RAM：170GB
- Main：Qwen3-235B-A22B-Thinking-2507 UD-Q4_K_XL
- Aux：與 `deployment_profiles/defaults.json` 相同
- 量測日期：2026-06-11
- placement：main 與三個 aux 同卡，因此 `MAIN_GPU` 與 `AUX_GPU` 指向同一張 5090

Reference main 可用 Hugging Face CLI 下載；它是三個 GGUF shard，registry 指向第一片：

```bash
HF_XET_HIGH_PERFORMANCE=1 hf download \
  unsloth/Qwen3-235B-A22B-Thinking-2507-GGUF \
  --include "UD-Q4_K_XL/*" \
  --local-dir ~/models/Qwen3-235B-A22B-Thinking-2507-GGUF
```

主模型 registry key 固定在 profile；`models.json` 只放本機路徑，不要把真實私人路徑
或 GPU UUID commit 進 repo：

```json
{
  "qwen3-235b-a22b-thinking-2507-ud-q4-k-xl": "/absolute/path/to/Qwen3-235B-A22B-Thinking-2507-UD-Q4_K_XL-00001-of-00003.gguf"
}
```

啟動：

```bash
export AICODE_PROFILE=verified-reference
export AICODE_MODEL=qwen3-235b-a22b-thinking-2507-ud-q4-k-xl
export MAIN_GPU=<RTX_5090_GPU_UUID_OR_INDEX>
export AUX_GPU="$MAIN_GPU"

./scripts/start-all.sh --dry-run
./scripts/start-all.sh
./scripts/check-status.sh --strict
```

## 保存的 main 參數

profile 會產生下列等價參數；實際 model path、host、port 與 GPU selector 由 loader
安全補上：

```text
-c 65536 -b 2048 -ub 512 -ngl 99 --jinja
--temp 0.6 --top-p 0.95 --top-k 20 --min-p 0 --presence-penalty 1.0
--cache-type-k q8_0 --cache-type-v q8_0
--n-cpu-moe 90 -fa on -t 12 --no-mmap
```

- `-c 65536` 是這次主 n_ctx；`aicode` 會讓 CodeTrail budget 與 OpenCode active
  model 的 `limit.context` 自動跟隨。
- `--n-cpu-moe 90` 是這個 235B MoE＋同卡四服務的實測 placement，不適用於 dense
  模型，也不能當成 H200 數字。
- `-b 2048 -ub 512` 是本 reference 的 prompt-processing 設定。
- `-t 12` 對應當時的 9950X；換 CPU 必須重測。
- `--no-mmap` 避免 CPU-offloaded expert 第一次使用時逐頁從 SSD 載入。
- 取樣值只對這顆 reference model 保存；其他 main model 應依自己的模型說明調整。

若要實驗 `n-cpu-moe` / threads，先以 `start-main-server.sh --dry-run` 保存有效命令，
再用 `llama-bench` 做受控比較。不要把本頁數字直接標成其他硬體 verified。

## 實測資源占用與效能

同卡同時跑 main＋embedding＋reranker＋VL：

```text
VRAM  28083 MiB / 32607 MiB (main 17830 + VL 7952 + embed 1148 + rerank 896)
RAM   122 GiB used / 170 GiB total, 48 GiB available, swap 幾乎未用
```

llama-server log 的觀測值：

- prompt eval / ingest：約 74–80 tok/s；這是吃輸入，不是輸出生成速度。
- 單請求 output decode / generation：約 7.37 tok/s。
- 兩個 main 請求重疊時：長任務約 4.95 tok/s，另一個小任務約 2.20 tok/s。
- main 未顯式設定 `--parallel`；當時 log 顯示自動 `n_parallel=4`。

以上數字只描述該次 reference。模型檔、llama.cpp commit、prompt、溫度、散熱與其他
process 都會影響結果。
