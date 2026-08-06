# Deployment profiles

[回到 README](../README.md)。

`deployment_profile.py` 是 main、embedding、reranker、VL 的共同設定入口。它只用
Python 3.10 stdlib 讀 JSON，採封閉 schema/參數 allowlist，不執行 JSON 內容。

## 選擇與優先序

```bash
export AICODE_PROFILE=maintainer-target
python deployment_profile.py show
python deployment_profile.py validate
```

可選 profile：

- `maintainer-target`：H200＋448GB RAM main、RTX 2000 Ada aux；尚未 verified。
- `verified-reference`：RTX 5090＋170GB RAM reference；量測見
  [verified-reference-5090.md](verified-reference-5090.md)。

兩者都必須明確選用。沒選時只載入不宣稱硬體的 `safe-defaults`。合併順序：

```text
launcher CLI / environment
  > ~/.config/codetrail/deployment.json local override
  > AICODE_PROFILE 選用 profile
  > safe-defaults
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
  啟動的 VL 依其他 aux 實際占用保留 VRAM。`cpu_moe` 只允許 main，且不可與
  部分 offload 的 `n_cpu_moe`(→ `--n-cpu-moe`,同樣 main-only)同時設定。
  `set_config.sh` 只在偵測到 MoE expert tensors 時詢問 CPU-MoE / 一般模式
  (無預設答案,不做容量估算);三個 aux 固定 `-np 1`,VL 使用 `-ngl auto --fit on
  --fit-target 3072`(VL 的啟動機制)。`n_cpu_moe` 由 `set_config.sh` 在 CPU-MoE
  模式詢問並寫入:值完全由使用者輸入(互動題或 `--n-cpu-moe N` 旗標),工具只驗證
  0..1024 範圍;輸入超過最大 blk 編號、或 build 不支援 `--n-cpu-moe` 時退回
  `cpu_moe: true`。放不放得下 VRAM 以啟動後 `nvidia-smi` 實測為準。
- VL 的 `mmproj`：同樣只接受 registry key 或 GGUF 絕對路徑。

safe-defaults 的 reranker 是 `bge-reranker-v2-m3` Q8_0,保留
`-c 8192 -b 8192 -ub 8192`。`set_config.sh` 會讓使用者輸入 reranker ctx
(128..1048576,無預設值),並把所選值同步寫入這三個欄位;手動只改 `ctx`、
仍留著 8192 physical batch 的話,並不能解決 Qwen3 的 buffer 壓力。

Qwen3-Reranker 是支援的 accuracy-first 選項(過往量測可參考 ctx 2048 約 2 GiB、
8192 約 6.25 GiB;BGE 在 8192 約 0.7 GiB)。只要每筆 `query + passage` 沒超過上限,
單純增大 ctx 不會提高排序精準度;超過時請求可能失敗或上游必須截斷,才可能漏證據。
較大 ctx 可容納較長輸入,但配置更多顯存,實際處理更多 token 時延遲也會增加。
Qwen3 是 causal 架構,除 compute buffer 外還有 KV cache,所以增幅遠高於 GGUF
權重大小。`set_config.sh` 不做容量估算:啟動後用 `nvidia-smi` 實測,不夠時
降低 reranker ctx、換 BGE、換較小 VL 或分卡,不應關閉 VL `--fit`。

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
  "profile": "maintainer-target",
  "services": {
    "main": {
      "model": "<CODE_MODEL>",
      "ctx": 65536
    }
  }
}
```

不要把真實 UUID、私有模型路徑或 NDA 名稱放進 repo profile；留在使用者 home config。
