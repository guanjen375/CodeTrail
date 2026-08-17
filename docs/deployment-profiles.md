# Deployment profiles

[回到 README](../README.md)。

`deployment_profile.py` 是 main、embedding、reranker、VL 的共同設定入口。它只用
Python 3.10 stdlib 讀 JSON，採封閉 schema/參數 allowlist，不執行 JSON 內容。

## 選擇與優先序

安全基底 `safe-defaults` 直接內建在 `deployment_profile.py`,不宣稱硬體;正常使用不必選
profile,`set_config.sh` 產生的 local override 疊在基底上就是有效設定。要做一次性實驗
設定,`AICODE_PROFILE`(或 CLI `--profile`)可指向**絕對路徑** `.json` profile,檔內可用
`"extends": "defaults"` 繼承基底:

```bash
python deployment_profile.py show
python deployment_profile.py --profile /absolute/path/to/experiment.json validate
```

合併順序：

```text
launcher CLI / environment
  > ~/.config/codetrail/deployment.json local override
  > AICODE_PROFILE 選用 profile(絕對路徑 .json,選用)
  > safe-defaults(內建)
```

`AICODE_DEPLOYMENT_CONFIG=/absolute/path.json` 可在測試或多帳號環境改 local override
位置。注意:產生的 `~/start.sh` 啟動時會刻意 unset 這批 runtime override(防
`.bashrc` 殘留覆寫),`set_config.sh` 也只寫入預設路徑——長期設定這個變數會讓
aicode/doctor 與 `~/start.sh` 各讀一份設定(set_config 偵測到會警告)。它適合
一次性測試,不適合當常駐設定。

## Service schema

每個 role 的有效資料都有：

- `model`：`models.json` key 或 GGUF 絕對路徑；main 可在基底中為 `null`，但啟動
  main 時一定 fail-loud，直到 `AICODE_MODEL` 或 local profile 明確指定。
- `port` 與 `base_url`：必須一致；URL 只接受無 credentials/path/query 的 HTTP(S)。
- `bind`：`local`(預設,loopback base_url 只綁 `127.0.0.1`)或 `all-interfaces`
  (綁 `0.0.0.0`,對其他機器開放 —— llama-server 無認證,慎用)。env 覆寫:
  `MAIN_BIND` / `EMBED_BIND` / `RERANK_BIND` / `VL_BIND`,或 `AICODE_BIND` 一次
  套用四個 role。非 loopback 的 base_url host 不受影響、照原樣綁定。
- `gpu_role`：只能是 `main` 或 `aux`。
- `ctx`、`batch`、`ubatch`：正整數或明確 `null`；`null` 代表不傳該 llama.cpp flag。
- `parameters`：role-specific allowlist；未知 key 直接拒絕。四個 role 都支援
  `parallel`(→ `-np`)；main 另支援新版
  llama.cpp 的自動 VRAM 配置:`gpu_layers` 可為整數或 `"auto"`(`-ngl auto`)、
  `fit`(`"on"`/`"off"` → `--fit`)、`fit_target`(MiB → `--fit-target`)、
  以及 `cpu_moe: true`(→ `--cpu-moe`)；VL 也支援 `fit` / `fit_target`，讓最後
  啟動的 VL 依其他 aux 實際占用保留 VRAM。`cpu_moe` 與部分 offload 的
  `n_cpu_moe`(→ `--n-cpu-moe`)**只允許 main 與 vl**(embedding / reranker
  拒絕),且同一個 role 不可同時設定這兩鍵。VL 的 `--fit on` 與 CPU-MoE 可以並存:
  llama.cpp 的 `--fit` 只調整「未指定」的參數,並會把 expert 的 buffer override
  一併算進 fit 計算。
  `set_config.sh` 只在偵測到 MoE expert tensors 時詢問 CPU-MoE(main 與 VL 各一題,
  沒有 y/n 分流,直接問「幾層 experts 留 RAM」;無預設答案,只給一個推薦區間
  (下界 = 權重剛好放得進該 role 所選 GPU 目前 free VRAM 的層數,上界 = 全部移到
  RAM),例如 `推薦數值:38-43`。估算只含 GGUF 權重 storage(未計 KV cache /
  compute buffer / 共卡的附屬服務),是起點而非保證,也不限制輸入);
  三個 aux 固定 `-np 1`,VL 使用 `-ngl auto --fit on --fit-target 3072`
  (VL 的啟動機制)。層數的值完全由使用者輸入(互動題或
  `--n-cpu-moe N` / `--vl-n-cpu-moe N` 旗標),工具只驗證 0-1024 範圍:
  `0` = 不 offload(不寫任何 CPU-MoE 鍵)、`N` = `n_cpu_moe: N`、
  輸入超過最大 blk 編號(或 build 不支援 `--n-cpu-moe`)→ `cpu_moe: true`。
  放不放得下 VRAM 以啟動後 `nvidia-smi` 實測為準。
- main 的 `threads`(→ `-t`)**從來不是設定時的問題**,只有 `set_config.sh --threads N`
  明確指定時才會寫入。未指定 = auto:不傳 `-t`,llama.cpp 的預設 `-1` 會自己偵測
  (x86_64 Linux 上 hybrid CPU 只算 P-core,否則用實體核心數、排除 HT siblings),
  比工具自己數邏輯 CPU 準。
- VL 的 `mmproj`：同樣只接受 registry key 或 GGUF 絕對路徑。

safe-defaults 的 reranker 是 `bge-reranker-v2-m3` Q8_0,保留
`-c 8192 -b 8192 -ub 8192`。`set_config.sh` 會把這個 internal buffer 當成一般問題問
(必答、沒有預設值,只把 8192 當「維護者驗證過的組合」提示);非互動用
`--rerank-ctx`(128-1048576)提供,三個欄位同步。手動只改 `ctx`、
仍留著 8192 physical batch 的話,並不能解決 Qwen3 的 buffer 壓力。

Qwen3-Reranker 是支援的 accuracy-first 選項(過往量測可參考 ctx 2048 約 2 GiB、
8192 約 6.25 GiB;BGE 在 8192 約 0.7 GiB)。只要每筆 `query + passage` 沒超過上限,
單純增大 ctx 不會提高排序精準度;超過時請求可能失敗或上游必須截斷,才可能漏證據。
較大 ctx 可容納較長輸入,但配置更多顯存,實際處理更多 token 時延遲也會增加。
Qwen3 是 causal 架構,除 compute buffer 外還有 KV cache,所以增幅遠高於 GGUF
權重大小。`set_config.sh` 不替 reranker 估容量:啟動後用 `nvidia-smi` 實測,不夠時
用 `--rerank-ctx` 降低 buffer、換 BGE、換較小 VL 或分卡,不應關閉 VL `--fit`。

禁止 `extra_args`、shell 字串、相對 artifact path、帶控制字元的值。launcher 由驗證後
欄位建立 argv，再逐參數 quote 給 tmux。

## GPU precedence

```text
main:      MAIN_GPU > CUDA_VISIBLE_DEVICES
embedding: EMBED_GPU > AUX_GPU > CUDA_VISIBLE_DEVICES
reranker:  RERANK_GPU > AUX_GPU > CUDA_VISIBLE_DEVICES
VL:        VL_GPU > AUX_GPU > CUDA_VISIBLE_DEVICES
```

GPU UUID 比 index 穩定，因為 PCI enumeration 次序可能在重開機或硬體變更後改變。

## Local override 範例

```json
{
  "schema_version": 1,
  "profile": "defaults",
  "services": {
    "main": {
      "model": "<CODE_MODEL>",
      "ctx": 65536
    }
  }
}
```

不要把真實 UUID、私有模型路徑或 NDA 名稱 commit 進 repo；這類值留在使用者 home config。
