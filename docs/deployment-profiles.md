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
位置。

## Service schema

每個 role 的有效資料都有：

- `model`：`models.json` key 或 GGUF 絕對路徑；main 可在基底中為 `null`，但啟動
  main 時一定 fail-loud，直到 `AICODE_MODEL` 或 local profile 明確指定。
- `port` 與 `base_url`：必須一致；URL 只接受無 credentials/path/query 的 HTTP(S)。
- `gpu_role`：只能是 `main` 或 `aux`。
- `ctx`、`batch`、`ubatch`：正整數或明確 `null`；`null` 代表不傳該 llama.cpp flag。
- `parameters`：role-specific allowlist；未知 key 直接拒絕。main 另支援新版
  llama.cpp 的自動 VRAM 配置:`gpu_layers` 可為整數或 `"auto"`(`-ngl auto`)、
  `fit`(`"on"`/`"off"` → `--fit`)、`fit_target`(MiB → `--fit-target`)、
  `parallel`(→ `-np`)。主模型大於 VRAM 時 `set_config.sh` 會自動採用這組。
- VL 的 `mmproj`：同樣只接受 registry key 或 GGUF 絕對路徑。

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
