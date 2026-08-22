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
python3 deployment_profile.py show
python3 deployment_profile.py --profile /absolute/path/to/experiment.json validate
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
  (綁 `0.0.0.0`,對其他機器開放 —— CodeTrail 目前產生的 server 指令未啟用
  認證,慎用)。env 覆寫:
  `MAIN_BIND` / `EMBED_BIND` / `RERANK_BIND` / `VL_BIND`,或 `AICODE_BIND` 一次
  套用四個 role。非 loopback 的 base_url host 不受影響、照原樣綁定。
- `gpu_role`：只能是 `main` 或 `aux`。
- `ctx`、`batch`、`ubatch`：正整數或明確 `null`；`null` 代表不傳該 llama.cpp flag。
- `parameters`：role-specific allowlist；未知 key 直接拒絕。完整清單以
  `deployment_profile.py::_ROLE_PARAMETERS` 為單一事實來源，目前是：
  - 四個 role 共用：`gpu_layers`、`flash_attention`、`no_mmap`、`parallel`。
  - main：另有 `jinja`、`temperature`、`top_p`、`top_k`、`min_p`、
    `presence_penalty`、`cache_type_k`、`cache_type_v`、`cpu_moe`、`n_cpu_moe`、
    `threads`、`fit`、`fit_target`。
  - embedding：另有固定角色旗標 `embedding` / `pooling` 與 `cache_ram`。
  - reranker：另有固定角色旗標 `embedding` / `pooling` / `reranking` 與
    `cache_ram`。
  - VL：另有 `fit`、`fit_target`、`cpu_moe`、`n_cpu_moe`。

  主要映射包括 `gpu_layers` → `-ngl`、`flash_attention` → `-fa`、`no_mmap` →
  `--no-mmap`、`parallel` → `-np`；main 的 sampling / KV cache 欄位也會逐參數轉成
  llama-server argv。`gpu_layers` 可為整數或 `"auto"`(`-ngl auto`)，`fit`
  (`"on"`/`"off"` → `--fit`)、`fit_target`(MiB → `--fit-target`)與
  `cpu_moe: true`(→ `--cpu-moe`)用於 VRAM / CPU-MoE 配置。VL 也支援
  `fit` / `fit_target`，讓最後
  啟動的 VL 依其他 aux 實際占用保留 VRAM。`cpu_moe` 與部分 offload 的
  `n_cpu_moe`(→ `--n-cpu-moe`)**只允許 main 與 vl**(embedding / reranker
  拒絕),且同一個 role 不可同時設定這兩鍵。**`--fit` 與 CPU-MoE 互斥**:llama.cpp 的
  `common_params_fit_impl` 一看到 `tensor_buft_overrides` 已被使用者設定就直接 abort
  (只印一行 WARN 就繼續載入,而 `-ngl auto` 的語意是「全部層上 GPU」)。因此
  `set_config.sh` 在 VL 套用 CPU-MoE 時會改寫 `gpu_layers: 99` + `fit: "off"`,
  不寫不會生效的 `fit_target`——沒有自動退讓的安全網,層數要自己抓。
  **既有設定檔不必重跑也會被矯正**:`build_server_command` 在偵測到 CPU-MoE 時一律
  輸出 `--fit off`(`--fit` 的預設值是 `on`,不輸出等同 on 一樣會 abort)並丟掉
  `--fit-target`;**每一條會真的啟動 server 的路徑**都會先印警告
  (`launch_servers.py` 與文件支援的 systemd `deployment_profile.py exec`),
  不做靜默矯正。警告條件涵蓋「明寫 `fit: "on"`」「**省略 fit**(llama.cpp 預設即
  `on`)」「只留 `fit_target`」「`gpu_layers: "auto"`」;
  `set_config.sh` 產生的形狀(`fit: "off"` + 明確 `gpu_layers` + 無 `fit_target`)
  沒有衝突,不會每次啟動噴警告。
  刻意不在 schema 層拒絕:`config.py` 在 import 期就載入 effective profile,
  硬拒會讓整個 CodeTrail(含 MCP server)無法啟動。
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
- embedding 與 reranker 另支援 `cache_ram`(整數 `0..262144` MiB，映射為
  `--cache-ram N`)；內建與 `set_config.sh` 預設都固定為 `0`。這兩種非生成服務的
  prompt cache 無法重用，保留預設 8192 MiB 上限只會讓不同輸入逐步累積 host RAM。
  main 與 VL 不接受這個 profile key，main 的生成 prompt cache 保持原行為。
  `set_config.sh` 會先探測 build 是否支援 `--cache-ram`，舊 build 直接 fail-loud，
  不會靜默省略安全預設；也不另暴露可能互相矛盾的 `cache_idle_slots`。
- `no_mmap`(→ `--no-mmap`)屬**使用者領域**,`set_config.sh` 從不自動決定:代價是啟動時要把整份
  權重讀進 RAM,換來 MoE 首次推論不必從 SSD 逐頁 page-in(TTFT 1–2 分鐘 → 5–15 秒)。
  套了 CPU-MoE 卻沒設時 `set_config.sh` 會警告(llama.cpp 自己也會印
  `tensor overrides to CPU are used with mmap enabled`);**手動加在 main 或 vl 的設定,重跑
  `set_config.sh` 會保留**(`_PRESERVED_KEYS_BY_ROLE`),不會被當成「未涵蓋鍵」丟掉。
  截至 2026-08，上游 [server 參數文件](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
  已將 `--no-mmap` 標為 deprecated，建議未來轉向 `--load-mode`。CodeTrail 仍保留
  `no_mmap` 來相容目前驗證過的 build；上游若移除旗標，要同步遷移 profile
  schema、launcher、preflight 與文件，不要只在 JSON 自行改鍵名。
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

## 遠端 endpoint 的雙重同意

`bind: "all-interfaces"` / `./set_config.sh --allow-remote` 控制的是「其他機器能不能連進
llama-server」；profile 的 `base_url` 控制的是「CodeTrail 把 request 送去哪」。這是兩個
不同方向的資料邊界。

任何 role 的 effective `base_url` 不是 loopback 時，CodeTrail 還要求顯式設定：

```bash
export AICODE_MODEL_REMOTE_OK=1
```

沒有這個值，completion、chat、embedding、reranking、health、props 與 slots 都會
fail-loud。設定它只表示接受資料送到該 endpoint，不會自動提供 TLS、認證、防火牆或
VPN。Contextual Retrieval 的生成路徑另用 `AICODE_KB_CONTEXT_REMOTE_OK=1`，兩個 opt-in
不互通。完整威脅邊界見 [security.md](security.md)。

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
