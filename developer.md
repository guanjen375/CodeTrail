# CodeTrail 開發與維運

本文件集中進階部署、完整設定、安全邊界、維護、評測與故障排解。
初次安裝與日常啟停看 [README](README.md)，聊天與 RAG 操作看
[使用指南](docs/usage.md)，工具參數看 [MCP 契約](docs/mcp-tools.md)。
修改原始碼前先讀 [貢獻與測試規範](AGENTS.md)；此處列命令不代表授權執行測試或實機評測。

- [進階部署](#advanced-deployment)：[A／B](#split-deployment)、[DSpark](#dspark)、[設定檔](#configuration)、[維護 argv](#maintenance-cli)
- [完整依賴](#dependencies)、[安全邊界](#security)
- [維護與開發](#maintenance)：[資料保存](#review-artifacts)、[索引](#index-scope)、[context](#context-budget)、[資料飛輪](#data-flywheel)
- [評測](#eval)：[私人 session-model eval](#session-model-eval)
- [故障排解](#troubleshooting)：[CUDA](#cuda-build)、[假工具呼叫](#mcp-connected-but-no-tool-call)、[剪貼簿](#clipboard-ssh-tmux)、[MCP incident](#mcp-incidents)

<a id="advanced-deployment"></a>

## 進階部署

`./set_config.sh` 無參數直接設定本機。需要角色切換、還原或附加操作時，在 checkout 執行：

```bash
scripts/configure-advanced.sh
```

| 選項 | 操作 |
|---|---|
| 1 | 重設目前部署角色 |
| 2 | 本機 local |
| 3 | 模型主機 A，model-host |
| 4 | 工作主機 B，client |
| 5 | 還原最近一次設定交易 |
| 6 | 顯示 A 的 endpoint manifest |
| 7 | 獨立調整 DSpark |

以上 wrapper、`set_config.sh`、`aicode`、host/device 入口都不接受參數；
`~/start.sh` 只接受無參數或單一 `stop`。維護 argv 只交給內部 Python 入口。
產生的 start wrapper 標明 checkout 絕對路徑、產生時間與版本，啟動和停止都先顯示來源，
不要因檔名相同就以為它屬於目前所在的 checkout。

<a id="split-deployment"></a>

### A／B 分離部署

A（host）執行 main、embedding、reranker、VL 四個 llama-server；B（device）執行
`aicode`、MCP、原始碼編輯、build 與 KB。B 不需要 GPU、GGUF、mmproj、tmux 或 llama-server。
未設定 `mode` 的既有部署保持 `local`；另外兩種模式是 `model-host`（A）與 `client`（B）。

#### A：設定並啟動模型

在 A 的 CodeTrail checkout 執行：

```sh
scripts/codetrail-host.sh
```

首次會引導模型、GPU、context 與網路設定。開放區網需要明確同意；再輸入 A 對 B 的
literal 私有 IPv4（服務綁定 `0.0.0.0`），供產生四個角色的端點交接資料。腳本使用既有設定與 launcher 啟動服務，
已運作的服務先核對狀態，不為了再次執行腳本而盲目重啟。

依終端顯示的方式保存完整 client manifest，透過組織允許的方式移到 B。
manifest 含目的地、模型版本 ID 與主模型的 `thinking_kwarg` 能力，不含 GPU、本機模型路徑或任何授權。
四個 port 來自 A 的 deployment profile。A 的防火牆應只允許 B 的來源位址連線；
程式端白名單不能代替網路 ACL。原生 HTTP 不提供加密，需使用組織管理的安全直連網路，
或已正確配置憑證的 HTTPS endpoint；TLS 驗證不會關閉。

model-host 設定會串流計算每個 GGUF（含所有 shard）及 VL projector 的 SHA-256，
建立 `identity_alias`，透過 llama-server `--alias` 提供 live `model_alias`。
這會讀取完整模型檔，耗時取決於模型大小與磁碟。換權重、shard 或 projector 後，
用 `scripts/configure-advanced.sh` 選 3 重新設定、重啟 A 並重新交接 B 設定；啟動會拒絕與 alias 不符的權重。
主模型的 thinking 能力也會依新 GGUF chat template 重新偵測；B 匯入 manifest 時保留它。
沒有能力欄位的舊 manifest 或逐角色手動輸入的設定視為未知，`/think on` 不可用，
所有請求仍明確送出 off。這不是 B 的永久聊天偏好。

[DSpark 推測解碼](#dspark) 可在 A 的主模型設定中選擇，或由附加入口選單 7 獨立調整；
B 不設定本地 draft。A 開啟、關閉或換 draft 後，重啟服務，再以附加入口選單 6 匯出新的 manifest 交給 B
重新匯入，因為 main 的版本 alias 會改變。manifest 不攜帶 A 的 draft 檔案路徑。

監看與停止沿用直接命令：

```sh
nvidia-smi
tmux attach -t codetrail-main
~/start.sh stop
```

#### B：授權端點並開始對話

先進入要分析的專案，執行 B 上 CodeTrail checkout 的 device 腳本：

```sh
cd <PROJECT_TO_ANALYZE>
<CODETRAIL_REPO>/scripts/codetrail-device.sh
```

首次依提示提供 manifest 的本地路徑。精靈會完整顯示四角色 URL 與 alias，
明確確認後才把目的地寫入 `deployment.json`，把精確授權寫入 owner-only
`client.json.model_endpoints`。KB 脈絡生成可能傳送較大範圍文件，需獨立同意
`kb_context_remote_ok`，一般模型端點授權不包含這項授權。

B 只寫上述兩個設定檔，不建立或覆寫 `models.json`、`~/start.sh`。
設定完成後會在原專案目錄啟動 `aicode`；已有 client 設定時直接走同一個入口，
包含正常 preflight、四個 live alias 與主模型 live n_ctx 核對。
之後也可以在專案目錄直接輸入 `aicode`。重新設定或還原最近一次交易，執行
`scripts/configure-advanced.sh` 依選單操作。

端點限原生 llama-server 根 URL、literal 私有 IP，四角色 origin 必須不同。
拒絕 hostname、credentials、query/fragment、未核准 scheme/IP/port/path、第三方 provider、
環境 proxy、netrc 與 redirect。修改 `deployment.json` 不會擴大 `client.json` 的授權；
client 模式的 `model_remote_ok: true` 也不能繞過精確白名單。
編譯仍使用既有 `build_commands`、interactive 核准、sandbox/container 與 readonly 規則。

#### 診斷、身分與評測

`set_config.sh`、`scripts/configure-advanced.sh`、host/device 腳本與 `aicode` 均不接受參數；
`~/start.sh` 只接受無參數啟動與單一 `stop`。內部 Python 自動化與診斷入口見
[開發與維運](#maintenance-cli)。

B 的 strict status 與啟動觀測 live health、alias、capabilities 與 main 的 live n_ctx，
不以本機檔案、GPU/PID 或設定 ctx 猜測服務狀態。client 模式的 launch 拒絕執行；
stop 說明須在 A 操作，不終止 B 的其他行程。離線設定檢查不能宣稱四模型已驗證。

`model_identity.capture_model_identity(role)` 只發 `/props` GET。
local 身分核對 live model_path 並 hash 本機模型；client 身分核對版本 alias 與 live 資料，
記為 `declared-runtime-alias`、`artifact_sha256: null`。這表示核對 A 宣告的版本，
不表示 B 讀過或獨立驗證 A 的權重內容。缺失或不符的 live 身分不會重用舊 cache。
checkpoint 與私人 session eval 保留這個差別，eval 同時綁定四角色身分；replay
只繼承 owner-only 端點授權，壓縮與工具權限等評測行為仍固定。

離線 smoke 使用合成模型與 mock HTTP；未在兩台真實主機完成操作時，不能視為雙機驗收。
<a id="dspark"></a>

### DSpark 推測解碼

DSpark 使用配對的 draft 提出候選 token，再由主模型驗證；CodeTrail 使用 llama.cpp 的
`draft-dspark`。是否可用與是否加速取決於主模型、draft 及 build，不由硬體或檔名推定。
機制與模型來源見 [llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/docs/speculative.md)
及 [DeepSpec](https://github.com/deepseek-ai/DeepSpec)。CodeTrail 不下載、訓練或轉換 draft。

本機精靈的主模型組已包含 DSpark，與模型、GPU、容量一起在最後確認並保存。本機入口沿用壓縮模式，
沒有 client.json 就不建立；附加入口的完整重設與內部 Python 介面仍可選壓縮模式。
預設 off；同一個 resolved 主模型 artifact 的舊配對可輸入 `keep` 明示沿用，Enter 選 off。
換主模型不得沿用舊 draft。使用者最後的答案是唯一權威，舊設定不會覆蓋本次 off。

只調 DSpark 時，執行 `scripts/configure-advanced.sh` 選 7。`on` 指定配對的本地 draft GGUF
與 1–64 的 token 上限（提示值 3），`off` 關閉，`q` 取消。獨立調整只更新 deployment，
沿用交易備份；保存後主動提供 `R` 立即套用／重啟或 `S` 稍後處理。
關閉不要求 draft 存在，也不做 draft／GPU／DSpark binary 能力探測。

`services.main.dspark` 省略或 `null` 為 off；開啟形狀：

```json
{ "draft_model": "/absolute/path/to/matching-draft.gguf", "draft_n_max": 3 }
```

這個物件只允許 local／model-host 的 main，採整值替換，不合併舊配對的局部欄位。
profile 的 draft 可用 registry key；精靈維護旗標要求本地 GGUF 路徑。
啟用後由驗證欄位產生 `--spec-type draft-dspark`、`--spec-draft-model`、`--spec-draft-n-max`。
只在 main 生效，`/think` 仍獨立控制聊天 thinking。

設定保存只代表 schema 與相依性通過。啟動 health OK 後還需非空 `/slots`，每個 slot 的
`speculative` 必須是真正 JSON `true`；未知、缺席或失敗會報錯並按既有規則 rollback。
host readiness 與 `python3 scripts/check_status.py --strict` 也檢查這條，strict 另核對
process 的 draft 路徑／類型／上限。關閉後還在跑舊 DSpark process 也會報錯；
離線／snapshot 檢查不發 live 請求，不得宣稱已確認啟用。

A 的最終 main identity alias 包含啟用時的 draft 內容與設定。改變後要重啟 A，
再由附加入口選 6 重新交接 manifest，B 重新匯入；舊授權與身分不能直接沿用。
<a id="configuration"></a>

### 設定檔、交易與客戶端開關

設定只有 repo 常數 `config.py`、每台機器的 `deployment.json`／`models.json`、
每個使用者的 `client.json` 三類來源。位置預設 `~/.config/codetrail/`；
被分析專案的 `.codetrail/` 是輸出與匯入資料，不能提供 runtime 開關。
行程間用 argv 交接；不用環境變數傳模型、GPU、容量、授權或 rollback。

本機精靈將 deployment、models、必要的 client 與 `~/start.sh` 放在同一筆交易，
既有檔備份為 `*.bak-setconfig-<時間戳>`，取消零寫入。還原 manifest
`setconfig-last-transaction.json` 只記最近一次交易，是還原資料，不是設定來源。
兩個世代共用該檔時，只接受全部目標都在本世代四檔白名單的 manifest；任一外來目標
就整批拒絕，不部分還原。壓縮停用 ledger 同樣維持既有檔名，以 schema 與 session hash 隔離。

重跑精靈重新詢問本次模型與 GPU，不將舊答案自動當作新答案；手動加入且允許保留的
取樣／`no_mmap` 等參數會保留，捨棄的未知項目會明列。從 client 轉本機／model-host 時
不保留 B 的遠端 `base_url`／port，重建本機端點並清掉先前 B 的精確授權。
主模型固定 `parallel=1`，不能因重設遺失單 slot 契約。

```bash
python3 deployment_profile.py validate
python3 deployment_profile.py show
python3 scripts/launch_servers.py --scope all --dry-run
```

上述離線命令核對合併設定與 argv，不代表模型已成功載入。實機以 start、strict status 和
客戶端 preflight 為準。`git pull` 不會自己重寫 home 設定；換路徑、模型或缺少必要欄位時再設定。

#### 客戶端設定

`client.json` 由 owner-only IO 讀寫，目錄 0700、檔案 0600，拒絕 symlink／hard link。
不存在時不因讀取建立目錄。未知鍵與不是真正 JSON boolean 的布林值會 fail-loud。
最小形狀為 `{"schema":1,"compaction_mode":"manual","permission":{}}`。

只調整壓縮時，在此檔修改 `compaction_mode` 為 `codetrail`、`manual` 或 `off`，
保留其他欄位，並重開 `aicode`；這適用所有部署角色，不需重設模型或切換角色。
缺檔時可依上面的最小形狀建立並維持上述權限；不可信的設定須先修正格式、
擁有者、權限或連結問題，不能靠重跑模型精靈略過。

| 鍵 | 預設／作用 |
|---|---|
| `compaction_mode` | 未接管時 `manual`；可選 `codetrail`／`manual`／`off`，前兩者仍實驗中 |
| `permission` | `{}`；逐工具 `allow`／`ask`／`deny`，不能放寬 readonly |
| `copy_key` | `f2`；TUI `/copykey` 當場更新並持久化，可用鍵見[使用指南](docs/usage.md#copy) |
| `model_remote_ok` | `false`；local／model-host 的非 loopback 模型流量需明示同意 |
| `model_endpoints` | client 模式四角色的精確端點授權，由匯入流程建立；不能由上一鍵繞過 |
| `kb_context_remote_ok` | `false`；Contextual Retrieval 文件窗外送的獨立同意 |
| `external_import`／`external_import_roots` | `false`／`["~/Downloads","/tmp"]`；每次匯入仍核准 |
| `build_commands` | `false`；明示開啟 make／cmake／ninja／meson／bazel，意味可能執行專案程式 |
| `extra_allowed_commands` | `[]`；相容 PATH 裸命令授權 |
| `extra_allowed_command_dirs` | `[]`；`/allow add` 管理的本機工具目錄 |
| `rerank_fallback_policy` | 唯一值 `"error"`；舊 `embedding`／`main_model` 值要移除或改正 |
| `project_instructions` | `true`；是否讀專案 `AGENTS.md`／`.codetrail/lessons.md` |
| `objdump` | `""` 選 PATH objdump；跨架構時指定適合的執行檔 |
| `h_lang` | `"c"`；`.h` 可選 `c`／`cpp` |
| `use_container` | `false`；run_command 使用容器，指定後不得退回 host |
| `show_reasoning` | `false`；只控制 reasoning 本文顯示與重播 |
| `keep_historical_reasoning` | `false`；是否將舊 reasoning 送回模型，與 `/think` 獨立 |

多數設定在啟動時讀取，改完需重開客戶端；`/copykey` 與 `/allow` 是明確的局部即時更新。
它們呼叫時重讀 client.json，只改自己的欄位，不覆蓋彼此剛保存的設定。
`collect_data` 已移除且不能出現在 client.json；資料飛輪的保存政策見[下文](#data-flywheel)。
<a id="deployment-profiles"></a>

### Deployment profile schema

`deployment_profile.py` 是 main、embedding、reranker、VL 的共同設定入口。它只用
Python 3.10 stdlib 讀 JSON，採封閉 schema/參數 allowlist，不執行 JSON 內容。

#### 選擇與優先序

安全基底 `safe-defaults` 直接內建在 `deployment_profile.py`,不宣稱硬體;正常使用不必選
profile,`set_config.sh` 產生的 local override 疊在基底上就是有效設定。要做一次性實驗
設定,CLI 的 `--profile` 可指向**絕對路徑** `.json` profile,檔內可用
`"extends": "defaults"` 繼承基底:

```bash
python3 deployment_profile.py show
python3 deployment_profile.py --profile /absolute/path/to/experiment.json validate
```

合併順序：

```text
launcher 旗標(--profile / --llama-bin / --main-model / --main-ctx / --<role>-gpu …)
  > ~/.config/codetrail/deployment.json local override
  > --profile 選用 profile(絕對路徑 .json,選用)
  > safe-defaults(內建)
```

**設定沒有環境變數這一層。** loader、launcher、stop、status 與 `deployment_profile.py`
自己都掛同一組旗標(`add_loader_arguments`),殼層裡的殘留值一律無效;`~/start.sh` 也不
export / unset 任何東西；它只接受無參數啟動與單一 `stop`，以固定 argv 呼叫核心。
上述覆寫旗標保留在內部 Python 維護介面。tmux pane 內跑的是
`python3 deployment_profile.py exec <role> <同一組旗標>`,由它算出最終環境再 `exec`
llama-server —— CodeTrail 的三個設定前綴、llama.cpp 自己的 `LLAMA_ARG_*` 與繼承來的
GPU 選擇都在那一步剝掉,GPU 只由本檔驗證過的值重新指定。

#### Service schema

最上層 `mode` 接受 `local`（省略時的預設）、`model-host`（A）及 `client`（B）。
以下 GPU／模型檔／啟動參數屬於本機模型主機設定；client 使用四角色的 `base_url`、
版本化 `model`／`identity_alias` 與 main 的 `thinking_kwarg`，不需本機 GGUF、mmproj、GPU 或 llama-server。
`identity_alias` 在 model-host 由權重與 projector 的完整 SHA-256 產生並傳給 `--alias`。
client 的目的地仍須獨立通過 owner-only `client.json.model_endpoints`；profile 不授權連線。
設定、匯出與驗證流程見 [A／B 分離部署](#split-deployment)。

主模型另有選用的 `services.main.dspark`：省略或 `null` 為關閉，開啟時為
`{"draft_model": "/absolute/path/to/draft.gguf", "draft_n_max": 3}`。
這是 service 層的設定，不是 `parameters` 的任意旗標；只允許 local/model-host 的
main，draft 支援既有 registry key，token 上限為 1–64 的整數。設定物件整值替換，
更換主模型而未明示新配對時會清除它。日常操作使用主模型精靈或 `scripts/configure-advanced.sh` 選單 7；
相依檢查、重啟與 live 驗證詳見 [DSpark](#dspark)。

`local`／`model-host` 的每個 role 使用以下資料；`client` 的設定請依上方分離部署指南：

- `model`：`models.json` key 或 GGUF 絕對路徑；main 可在基底中為 `null`，但啟動
  main 時一定 fail-loud，直到 local override 的 `services.main.model`(或一次性的
  `--main-model`)明確指定。
- `thinking_kwarg`（只有 main）：`null`、`"enable_thinking"` 或 `"thinking"`。
  `set_config.sh` 有界讀取選中 GGUF 的 `tokenizer.chat_template` 與 `tool_use` variant，
  用 Jinja2 只解析 AST，不執行模板，不靠模型名稱、註解或文字猜測。只有可確認的
  外部布林開關才寫入名稱；DeepSeek 的 `thinking`／`enable_thinking` fallback alias
  可被辨識。缺失、未知或無法解析的模板寫 `null` 並列出原因，舊設定省略此欄也視為
  未知，不能開啟 `/think on`。重跑設定重新偵測並覆寫舊值；模型覆寫未提供新能力時
  不繼承上一顆模型的值。分離部署會將這欄隨 manifest 傳到 B。
  這欄是模型能力，不是聊天開關；每次聊天預設 off。客戶端送出的
  `chat_template_kwargs` 會將兩個名稱設成同一個 JSON boolean，讓 llama.cpp 的 parser
  與模板使用相同模式；內部生成固定 off。
- `port` 與 `base_url`：必須一致；URL 只接受無 credentials/path/query 的 HTTP(S)。
- `bind`：`local`(預設,loopback base_url 只綁 `127.0.0.1`)或 `all-interfaces`
  (綁 `0.0.0.0`,對其他機器開放 —— CodeTrail 目前產生的 server 指令未啟用
  認證,慎用)。要開放就寫進 local override(或 `python3 scripts/set_config.py --allow-remote`
  一次寫好四個 role);非 loopback 的 base_url host 不受影響、照原樣綁定。
- `gpu_role`：只能是 `main` 或 `aux`。
- `gpu`(選填)：這個 role 要用的 GPU selector,UUID 或 `nvidia-smi` index;缺席 =
  不指定卡。`build_server_command` 只把驗證過的值輸出成 `env CUDA_VISIBLE_DEVICES=<值>`
  前綴,繼承來的同名變數在 `exec` 那一步已經被剝掉。
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
  四個角色固定 `-np 1`,VL 使用 `-ngl auto --fit on --fit-target 3072`
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
- main 的 `threads`(→ `-t`)**從來不是設定時的問題**,只有 `python3 scripts/set_config.py --threads N`
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


#### GPU precedence

```text
main:      --main-gpu   > services.main.gpu      > 不指定
embedding: --embed-gpu  > --aux-gpu > services.embedding.gpu > 不指定
reranker:  --rerank-gpu > --aux-gpu > services.reranker.gpu  > 不指定
VL:        --vl-gpu     > --aux-gpu > services.vl.gpu        > 不指定
```

`--aux-gpu` 只套用到三個附屬 role(`gpu_role: "aux"`),不會影響 main。四個角色都沒有
指定時就不輸出 `CUDA_VISIBLE_DEVICES`,由 llama.cpp 自己決定;殼層或 tmux 裡繼承來的
同名變數不是輸入,`exec` 之前就剝掉了。

GPU UUID 比 index 穩定，因為 PCI enumeration 次序可能在重開機或硬體變更後改變。

#### Local override 範例

```json
{
  "schema_version": 1,
  "profile": "defaults",
  "llama_bin": "/absolute/path/to/llama-server",
  "services": {
    "main": {
      "model": "<CODE_MODEL>",
      "gpu": "<MAIN_GPU_UUID_OR_INDEX>",
      "ctx": 65536
    }
  }
}
```

頂層 `llama_bin` 是選填的 llama-server 執行檔絕對路徑,argv 的 `--llama-bin` 優先;
兩個都沒有時用 `~/llama.cpp/build/bin/llama-server`。**這兩個鍵(`llama_bin` 與
`services.<role>.gpu`)是本版新增的**:共用同一份 `~/.config/codetrail/` 的舊世代
checkout 會對未知鍵 fail-loud,那是封閉 schema 的預期行為。

不要把真實 UUID、私有模型路徑或 NDA 名稱 commit 進 repo；這類值留在使用者 home config。
<a id="maintenance-cli"></a>

### 內部設定與啟動 argv

日常入口 `aicode`、`set_config.sh`、`scripts/codetrail-host.sh`、
`scripts/codetrail-device.sh`、`scripts/configure-advanced.sh` 均無參數；`~/start.sh` 只接受空 argv 與單一 `stop`。
下列 Python argv 供維護、自動化與離線測試 harness 使用，不由 shell wrapper 透傳。

日常 `./set_config.sh` 不詢問模型目錄與 binary 路徑：掃描 `~/models`，沿用
`deployment.json` 的 `llama_bin`，未指定才用 `~/llama.cpp/build/bin/llama-server`。
需要其他位置或修復不合法的 deployment 設定時，執行 `./scripts/configure-advanced.sh`
並選 `2. local`，即可指定路徑並在確認後重建；日常入口遇到不可信設定會明確報錯。

內部非互動設定入口是 `python3 scripts/set_config.py --yes`。它會跳過提問與確認頁，
但**所有使用者選擇題的值必須由旗標提供，缺哪個就報錯**（保留值與 DSpark 的例外見下方）：

- 模型 / GPU：`--main-model` / `--main-gpu`、`--embed-model` / `--embed-gpu`、
  `--rerank-model` / `--rerank-gpu`、`--vl-model` / `--vl-gpu`。模型與 GPU 的編號
  **都從 1 起算**(GPU 編號 = `nvidia-smi` index + 1;互動選單與每張卡的描述行都會
  印出對應的 nvidia-smi index)。VL 配對不唯一時再給 `--vl-mmproj`；單一候選、單卡或
  唯一 mmproj 會自動選用。
- 數值：`--ctx` 與 `--rerank-ctx`。`--threads` 是非必要的進階旗標；不給就是
  auto，不寫 `-t`。
- MoE：main 使用 `--cpu-moe` / `--no-cpu-moe` / `--n-cpu-moe N`；VL 使用
  `--vl-cpu-moe` / `--no-vl-cpu-moe` / `--vl-n-cpu-moe N`。`N=0` 等同不 offload。
- 網路：`--allow-remote` 才會開放區網連線；未指定只綁 `127.0.0.1`。
- 路徑(選填,不給就用預設):`--llama-bin <路徑>` 指定 llama-server 執行檔(會轉成
  絕對路徑寫進 `deployment.json` 的 `llama_bin`;預設沿用該檔既有的值,再退回
  `~/llama.cpp/build/bin/llama-server`)、`--models-dir <目錄>` 指定要掃的 GGUF 目錄
  (預設 `~/models`)。兩個都寫進設定檔,沒有等價的環境變數。
- 壓縮模式：`--compaction-mode {codetrail,manual,off}`。**這一項不給不會報錯**——
  沒給時沿用 `~/.config/codetrail/client.json` 記錄的既有選擇，這台機器還沒選過
  就不寫 `client.json`。理由是「沒有那個檔＝沒有接管」是安全預設：舊的 `--yes`
  自動化腳本重跑一次，不該因此突然開啟自動壓縮。

- DSpark：`--dspark {off,on,keep}`。`--yes` 未指定即 off；keep 只接受與既有配對相同的
  resolved 主模型 artifact。on 使用 `--dspark-draft /absolute/path/to/draft.gguf` 與
  `--dspark-draft-n-max 3`（1–64）。本次選擇先定案，再計算 model-host identity alias。

模型、GPU 與容量仍取本次作答／旗標。壓縮沿用規則與 DSpark 明示 keep 是例外；
手動取樣參數可保留，client 轉回本機時遠端端點不能保留。完整旗標見
`python3 scripts/set_config.py --help`。

```bash
python3 scripts/set_config.py --help
python3 scripts/launch_servers.py --scope all --dry-run
python3 scripts/check_status.py --strict
python3 scripts/stop_servers.py --scope aux
python3 scripts/launch_servers.py --scope aux
python3 scripts/launch_servers.py --scope all --keep-on-failure
```

`--health-timeout N` 屬於 launcher；`--timeout N` 屬於 stop。所有值仍由
部署檔、repo 常數或明示 argv 決定，不讀 shell 的模型設定。
測試 harness 直接呼叫 `scripts/set_config.py`，避免將舊公開旗標重新放回 wrapper。
<a id="service-management"></a>

### 替代安裝與服務管理

Python 依賴也可安裝到使用者 site-packages；有 PEP 668 的系統使用：

```bash
python3 -m pip install --user --break-system-packages -r requirements.txt
python3 -m pip install --user --break-system-packages "pymupdf4llm==1.28.0"
```

這會在使用者層覆寫套件選擇，隔離需求較高時使用 README 的 venv。純 CPU 編譯
llama.cpp 時省略 `-DGGML_CUDA=ON`，profile 按實際硬體設定；大型模型的速度與容量需另行驗收。

每個 server 一個 unit。不要在 unit 重抄模型、GPU 與 tuning 旗標:主模型、
`services.<role>.gpu` 與 `llama_bin` 都在 `~/.config/codetrail/deployment.json`,
直接讓 profile loader `exec` 該 role 就好。範例
`~/.config/systemd/user/codetrail-main.service`:

```ini
[Unit]
Description=CodeTrail main llama-server
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /absolute/path/to/CodeTrail/deployment_profile.py exec main
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

`exec` 是 llama-server 唯一真正被啟動的地方,所以最終環境也在那裡決定:CodeTrail 自己的
三個設定前綴、llama.cpp 的 `LLAMA_ARG_*` 與繼承來的 GPU 選擇一律剝掉,GPU 只由設定檔裡
驗證過的值重新指定。所以**不要**用 `Environment=` 傳 CodeTrail 的設定(主模型、GPU、
llama-server 路徑)—— 寫了不會生效,只會讓下一個人以為設定有兩個來源;要一次性換設定
就加旗標(見下面)。系統層的變數(例如 CUDA 的 `LD_LIBRARY_PATH`)不是 CodeTrail 的
設定,照常設即可,不會被剝掉。

啟用 + 開機自啟:

```bash
systemctl --user daemon-reload
systemctl --user enable --now codetrail-main
systemctl --user status codetrail-main
journalctl --user -u codetrail-main -f    # 看 log
```

embedding / reranker / VL 各複製一份，只把 `exec main` 改成對應 role；所有 unit 讀的是
同一份 `deployment.json`。要一次性換設定就在 `ExecStart` 後面加 loader 旗標(例如
`--profile /absolute/path/experiment.json`、`--main-model <CODE_MODEL>`、
`--llama-bin /absolute/path/to/llama-server`)，與內部 Python launcher 使用同一個 loader。
systemd 不會展開 `<...>` placeholder，啟用前必須換成實值。

使用其他 process manager 時，每個角色仍經 `deployment_profile.py exec <role>`，
不能手抄會繞過環境清理的 llama-server 命令。systemd 停止以對應 unit 操作；tmux 部署
使用 `~/start.sh stop`。只重啟附屬服務可用：

```bash
python3 scripts/stop_servers.py --scope aux
python3 scripts/launch_servers.py --scope aux
python3 scripts/check_status.py --strict
```
<a id="dependencies"></a>

## 主要依賴與環境錯誤

CodeTrail 的每項操作只使用選定的主要實作。必要套件、工具、服務或安全能力缺席、版本不相容或執行失敗時，該操作直接回錯誤，修復環境後才能重試。功能依賴在使用時檢查，快取命中也不能繞過檢查。`python3 scripts/doctor.py --no-network` 可先做離線健檢；不帶 `--no-network` 另檢查服務。

| 操作 | 必要實作與修復方式 |
|---|---|
| 啟動 `aicode` | PATH 的 `python3` 與 `requirements.txt`。只安裝名為 `python` 的執行檔不足以啟動。部署不需要 Node / npm。 |
| `set_config` 偵測主模型 thinking 控制 | Jinja2；安裝 `requirements.txt`。只解析所選 GGUF 的模板 AST，不執行模板；缺少 parser 直接拒絕設定，不改用文字比對。無法確認模板能力時明列不支援，並寫入 `thinking_kwarg: null`。 |
| 已啟用的 [DSpark](#dspark) | 配對的本地 draft GGUF（含完整 shards）與支援 `draft-dspark` 的 llama-server。設定與啟動驗證相依性；啟動、host readiness 與 strict status 必須由 `/slots` 確認真正啟用，缺席／失敗不退回普通解碼後宣稱成功。關閉不要求 draft 或 DSpark binary 能力。 |
| RAG / Code RAG 向量運算、MMR、KB ingest | NumPy；安裝 `requirements.txt`。缺少或壞掉的套件不會被當成 cache 損壞，也不改用另一套運算。 |
| 中文 BM25 | jieba；安裝 `requirements.txt`。不改用逐字分詞。 |
| Python / C / C++ 解析 | Python 用 stdlib `ast`；C/C++ 用 requirements 釘版的 tree-sitter 與 grammar，缺席或 ABI 不相容即報錯。 |
| JavaScript / TypeScript / Go / Rust 解析 | 使用 tree-sitter，需自行安裝與本 repo tree-sitter 相容的 `tree-sitter-javascript` / `tree-sitter-typescript` / `tree-sitter-go` / `tree-sitter-rust` grammar。doctor 列出可用狀態；缺席時不改用 regex。 |
| 其他受支援的 Ctags 語言 | Universal Ctags 必須啟用且包含該語言。缺少執行檔、JSON 能力或語言支援就報錯；不改用 regex。未定義 AST/Ctags backend 的副檔名仍使用其原有通用文字表示。 |
| 程式搜尋 | ripgrep 的 `rg` 必須在 PATH 且能成功執行；不改用 Python 全檔掃描。 |
| `run_lint` | 使用 `config.LINT_COMMANDS` 對該副檔名、模式指定的唯一命令（如 Python 的 ruff、C/C++ 的 clang-format）。缺席或失敗就報錯。專案語言工具需另備，CodeTrail 部署本身不需 Node。 |
| ELF 分析 / ingest | pyelftools；需要反組譯時必須能執行選定的 objdump，有 C++ mangled symbol 時需要 c++filt。不改用 readelf / Capstone；跨架構 objdump 請在 `client.json` 明確指定。 |
| 專用 reranking | 專用 reranker 服務。缺席、逾時、HTTP 或回應格式錯誤即報錯，不改用 embedding 排序或主模型。 |
| 已啟用的 query expansion / multi-query | 設定正確且可用的模型服務；服務或協定失敗會傳出錯誤。模型合法地未產生額外查詢仍是有效資料結果。 |
| TUI / headless `run` | 主 server `/props` 必須回正整數 n_ctx；同值交給 Engine 與 MCP。取不到就拒絕啟動回合，不改用設定檔估值。 |
| 聊天容量檢查與自動壓縮 | 主 llama-server 必須支援 `/v1/chat/completions/input_tokens`，接受完整 chat payload 並回非負整數 `input_tokens`。每次容量決策重新計算 system、工具 schema、reasoning 與模板標記；端點缺席、逾時、HTTP 或格式錯誤會中止該次操作，修復服務後可重試。需升級到支援此端點的 server；不退回字元估算，也不發生成／prefill 作計數探針。 |
| 明確要求容器隔離的評測 | `auto` 只選 Podman；若明確選 Docker，必須可讀 uid/gid 並傳入容器。缺少指定 runtime 或容器模組即報錯，不改在 host 執行。 |
| session / 設定 / 提示來源 / cache / patch 等安全 IO | 支援 dir-fd、`O_NOFOLLOW`、`O_DIRECTORY` 的 POSIX Python；承諾 owner-only 的操作還需 uid 與權限設定能力。能力缺席時拒絕操作，讀取也不建立 state 目錄。 |
| patch 建立新檔 | 檔案系統必須支援 atomic no-clobber hard link；失敗時清理暫存並盡力回滾整批，保留競爭者的檔案。 |
| KB 讀寫鎖 | 必須具備可用的行程鎖；鎖模組缺席、無法取得 flock 時直接報錯，不能把失敗當成可略過的 cache 保存通知。 |
| `~/start.sh stop` 驗證服務停止 | 需要可用的 listener 查詢與所宣告的 GPU 釋放證據。缺少必要工具或無法取得結果會非零結束，不宣稱已確認停止。 |

`client.json` 的 `rerank_fallback_policy` 目前只接受 `"error"`。舊的 `"embedding"` / `"main_model"` 必須刪除或改為 `"error"`；設定載入會提供遷移錯誤。`objdump` 未設定時選 PATH 的 `objdump`，有設定時就只使用那個路徑。`set_config.sh` 遇到損壞的既有 deployment 設定會報錯，請先修復檔案。

本規則針對環境造成的實作替換。明確關閉某功能、來源資料缺席、安全 regex 改字面比對、驗證過身分的 cache 重建、PDF 原生文字與逐行轉錄、取消與非必要預熱的狀態處理仍遵守各自契約。`apply_patch` 的附帶 syntax verification 仍為 passed / failed / skipped；缺 grammar 只會是 skipped，結果標示驗證不完整，不能宣稱通過。
<a id="security"></a>

## 安全邊界

### 操作入口

分析 NDA / 不信任 repo 時,建議用這個入口:

```bash
cd <PROJECT_TO_ANALYZE>
aicode
```

並且先在 `~/.config/codetrail/client.json` 設 `"project_instructions": false`
(見下面「不信任 repo 的安全模式」)。

客戶端**只**暴露 CodeTrail 的 21 個 MCP 工具:沒有第二套內建的 shell / 檔案 / web 工具
可以繞過沙箱。要更嚴的話,`~/.config/codetrail/client.json` 的 `permission` 可以把任何
工具改成 `ask` 或 `deny`(只能收緊,不能放寬 readonly policy)。

---

### 沙箱真正保護什麼

`aicode` 啟動時會把當前目錄設成 sandbox root(以 `mcp_server --root` 交給 MCP)。一般檔案讀寫都限制在這個根目錄；
從 `$HOME` 或 `/` 啟動會直接被拒絕。兩個刻意而受限的例外是:

- `import_external_file(...)` 在你顯式開啟後,可從指定來源白名單**讀取並複製**單一檔案到
  `<SANDBOX_ROOT>/.aicode_uploads/`;後續工具仍只處理沙箱內副本。
- `record_lesson(...)` 經 permission `ask` 核准後,只可寫固定的
  `~/.config/codetrail/lessons.json`,不能由模型指定其他外部路徑。

受 CodeTrail 沙箱保護的典型工具包含:

- 讀取與搜尋:`list_dir(...)`、`read_file(...)`、`grep_code(...)`、`code_rag_search(...)`
- 附件與知識庫:`import_external_file(...)`、`analyze_file(...)`、`ingest_document(...)`、`query_knowledge(...)`
- 修改與驗證:`git_status(...)`、`git_diff(...)`、`apply_patch(...)`、`run_lint(...)`、`run_command(...)`

模型能呼叫的就只有上面這些:客戶端把 `tools/list` 的結果原樣交給模型,沒有另一組不經過
CodeTrail 的內建工具。互動模式下 `apply_patch` / `run_lint` / `run_command` /
`remove_document` / `record_lesson` / `review_figures` / `review_text` / `import_external_file` 八個必須人工核准,核准框**完整顯示
參數**(含整份 patch)。

`query_table` 是唯讀證據工具；`review_text` 因包含修改與確認操作而採 ask，readonly server
會拒絕。入庫進行中兩者都回 busy，避免查到正在替換的版本。OCR 確認綁內容與來源身分，
新內容不能沿用舊確認。checkpoint 保存在 repo 的私有 `.codetrail/ingest/`，不受 figure
retention 刪除影響；其中含文件內容，應按知識庫相同方式管理存取。

A／B 模式的四個目的地必須另獲 owner-only `client.json` 精確授權；不使用第三方 provider、
環境 proxy／netrc 或 HTTP redirect。KB 脈絡生成另受 `kb_context_remote_ok` 控制。
網路隔離與 A 只允許 B 連線的 ACL 仍由部署者設定，見 [分離部署](#split-deployment)。

---

### 不信任 repo 的額外防線

不信任的 repo 影響得到的是**送進模型的指示**:專案根目錄的 `AGENTS.md` 與
`.codetrail/lessons.md` 每一輪都會進 system prompt。分析不信任 repo 時,用:

在 `~/.config/codetrail/client.json` 設:

```json
{ "project_instructions": false }
```

這會讓客戶端完全不讀專案內的 `AGENTS.md` 與 `.codetrail/lessons.md`。它**只**關閉專案來源;
`~/.config/codetrail/instructions.md`(你自己的)與內建基底規則照常載入 —— 那是刻意的,你的
規則不該被分析對象關掉。

兩個此模式的副作用/防線要知道:

- [lessons](docs/usage.md#lessons) 該 session **不會注入** —— TUI 接管前的終端會明講，並清掉先前
  render 殘留的 `.codetrail/lessons.md`,不會謊報「已注入」。(這個鍵只收真的
  `false`;`"false"` 是一個非空字串,不是 false。)
- 不信任 repo 可能把 `.codetrail` 換成指向專案外的 symlink/junction,誘導 lessons render 把檔案寫出沙箱;`aicode` 啟動時偵測到會直接拒絕啟動,一個 byte 都不寫。

---

### system prompt 與 permission 分工

system prompt 由客戶端組,順序固定:內建基底規則(`client_prompt.BASE_RULES`,上限 1,600
字元)→ MCP routing 指示 → 專案 `AGENTS.md` → `.codetrail/lessons.md` →
`~/.config/codetrail/instructions.md`。每一個來源檔都以 `O_NOFOLLOW` + `fstat` 讀,而且
**父目錄**被 symlink 重導就 fail-loud —— 只驗最終檔案擋不住「把 `.codetrail` 換成 symlink」。

system prompt 不是 permission:它不會讓被 `deny` 的工具變成可用,也不會繞過八個 ask 工具的
人工核准。權限的唯一來源是 `client_policy` 加上 `client.json` 的 `permission` 覆寫,
而 readonly session 另有第二層(MCP server 自己以 `--readonly` 起 —— 一個 argv 旗標,
`client.json` 把 `build_commands` 開起來也翻不回來)。

---

### 會真的改東西的工具

`apply_patch(...)` 會寫檔、`run_lint(...)`（`fix=True`）會格式化檔案、`run_command(...)` 會跑白名單命令——這是**三個不同的 ask**,每一個都要你分別核准。`apply_patch` 套用後只做同一 process、唯讀的 syntax check(advisory、失敗不回滾),不會執行 lint／typecheck／test,也不會呼叫會改檔的 formatter;核准「寫檔」不會暗中擴張成「執行專案程式碼」。建議工作節奏:

1. 先要求模型用 `git_status(...)` / `git_diff(...)` 看目前工作樹（非 git 專案會回跳過通知，這步略過）。
2. 要分析時明講「不要改檔」。
3. 要改檔時要求先列出會改哪些檔案,再套最小 patch(先 `dry_run` 預覽)。
4. 修改後由你決定是否用 `run_lint(fix=False)` / `run_command(...)` 跑最小相關檢查(各自核准)。

`run_command(...)` 本身還有命令白名單與 dangerous-pattern 過濾。timeout 只接受整數 1..600 秒（server 端上限；client 可能更早截止），不是這個範圍的整數會在執行前被拒絕。不要把 `rm` / `sudo` / `curl` / `bash` 加進白名單;真的需要人工操作時,讓模型列出建議命令,由人自己判斷後在 shell 執行。

個人工具鏈在 TUI 用 `/allow list` 查看，用 `/allow add <絕對目錄>` 新增；一次只收
一個目錄，空白可加引號。驗證後只更新 `client.json` 的 `extra_allowed_command_dirs`，
不改工具層 `permission`，不另問確認；重複加入仍驗證現場且不寫檔。兩個額外授權欄位
在每次 `run_command` 重新讀取，同一 session 後續命令立即生效，不重啟 MCP。
只熱載入授權，不套用其他 runtime 設定。設定讀寫沿用 owner-only 防線；壞值、
未知鍵或不安全檔案會拒絕執行，不回退到上一次授權。

工具目錄從 `/` 逐層以 dir-fd／`O_NOFOLLOW` 驗證，拒絕 symlink。祖先只接受 root
或目前 uid 擁有，且不得 world-writable（root-owned sticky 祖先如 `/tmp` 除外）；
最終目錄必須由目前 uid 擁有且不得 world-writable。允許 group-write，這是對
使用者信任的安裝位置授權，並不隔離同群組使用者。最多 32 個目錄，各有界枚舉
4096 個直接子項，不遞迴。候選須是目前 uid 擁有、非 symlink、非 world-writable、
帶 owner execute bit 的普通檔，且名稱通過既有規則。最多讀檔頭 64 KiB，只收
`#!` 腳本、可核對的 ELF `ET_EXEC` 或帶 `PT_INTERP` 的 `ET_DYN`；依內容判斷，
一般資料與共享函式庫不算工具。保留命令、內建／build 根名稱與不合格項目列排除。

每次 list／執行都重驗目錄、檔案及身分，不承諾固定 binary hash。零合格工具、
目錄失效或與其他目錄／legacy 名稱衝突都是明確錯誤：add 不寫檔，該次所有
`run_command` 都不執行。list 即使有部分結果，也會明示整體解析失敗；MCP 未回報
白名單時不拿本地設定猜測。授權工具仍可能缺動態 linker、架構支援或 license。

既有 `extra_allowed_commands` JSON 相容，例如 `["nsim", "mdb"]`，只授權 PATH
裸 executable 名稱。目錄工具也以裸名稱呼叫，實際執行使用已驗證絕對路徑，
不改 PATH、不接受絕對 argv[0] 或 `./tool`。保留名稱（含 `rm`／`sudo`／`curl`／
`bash`）、內建與 build 參數限制不能藉任一欄位擴大。readonly 一律清空額外授權
且停用執行；原有工具核准、危險字元、資料路徑規則與 timeout 仍適用。

授權代表你信任該程式可執行程式碼；工作目錄不是 OS sandbox，既有路徑規則也不會
辨識每個工具的私有旗標（例如 `-tcf=/abs/x.tcf`）。容器模式不掛入本機工具目錄，
目錄授權工具一律拒絕；legacy PATH 命令須在容器內可用，不會改到 host 執行。

`record_lesson(...)` 是唯一會寫到 sandbox root 之外的工具,而且只寫一個固定路徑:`~/.config/codetrail/lessons.json`(per-deployment 的行為教訓 store,與 `deployment.json` 同層;不能被模型指到別的路徑)。它被 permission 設成 `ask`:模型只能「提案」,你會在核准框看到完整 rule 內容,核准後才落地。沒有無審核的自動寫入路徑;細節見 [使用指南](docs/usage.md#lessons)。

人工核准由 `client_policy.ASK_TOOLS` 與客戶端 policy 執行；核准框會顯示完整參數。

tool canary 的 explicit hard gate 與 implicit diagnostic 分開使用 cache schema 2。cache 只存
fingerprint hash、lane status、檢查時間與版本，不存 prompt、專案路徑、檔名、模型輸出、
tool arguments 或 tool result；兩條 lane 不會互借另一列狀態。implicit 的
`optimal|suboptimal|fail|timeout` 是部署診斷，不是資料或正確性保證。

---

### 不要 commit 的資料

以下資料可能含 NDA 內容、使用者提問、模型回答或文件切片,都不該進 commit:

- `knowledge.json`、`knowledge*.json`、`*.knowledge.json`
- `knowledge_emb.npz`(舊版本的 companion 向量檔;新版向量在 `.codetrail/` 底下)
- `data/`、`*.jsonl`
- `.code_rag_cache_*`、`.rag_cache/`、`.rag_embedding_cache.json`
- `.code_rag_graph.sqlite3*`、`.code_rag_graph.lock`
- `.codetrail/`
- `.aicode_uploads/`

這個 repo 的 `.gitignore` 已經忽略上述主要路徑。若你在另一個 target project 使用
CodeTrail,也建議在那個 project 的 `.gitignore` 補上同樣項目。`.gitignore` 不能保護
被重新命名、複製或手動 export 的內容；commit / 分享前仍要看 `git status` 與實際 diff。

---

### 模型 API(llama-server)曝光面

四個 CodeTrail 產生的 llama-server(8080–8083)**預設只綁
`127.0.0.1`，且未啟用認證**。上游 llama-server 目前有 `--api-key` /
`--api-key-file` 與 TLS 選項，但 CodeTrail 的 profile allowlist 與內部 HTTP client 尚未
接上這些 credential。因此以目前支援的路徑來看，綁
`0.0.0.0` 就等於讓可抵達該 port 的機器都能呼叫模型 API。

要讓其他機器連線，必須在 host 設定精靈中明確同意開放區網（見[分離部署](#split-deployment)），
或在 deployment.json 各 service 設定 `"bind": "all-interfaces"`，而且只該在可信內網 / VPN 使用，必要時加防火牆規則。
如要開發 credential 支援，必須同步改 profile schema、所有 `llama_client`
call site、doctor / preflight 與 secret redaction，不能只手動在單一 server 加旗標。
[上游 server 選項](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
可供查證。

### 模型流量的 outbound policy(prompt 外送防線)

上面講的是「誰能連進來」;這一段是「CodeTrail 自己會把 prompt 送去哪」。所有經 `llama_client` 的模型呼叫共用同一套 transport policy(`endpoint_policy.py`):

- **local／model-host 的非 loopback 端點需要顯式 opt-in**:`deployment.json` 的某個 `base_url` 指到別台機器時,必須在 `~/.config/codetrail/client.json` 設 `"model_remote_ok": true`,否則每個呼叫(completion / chat / embedding / reranking,連 health/props/slots 探測也一樣)都會 fail-loud,錯誤訊息印出確切的鍵名與檔案位置。prompt 可能含 NDA 程式碼與文件內容——填一個遠端 IP 不等於同意外送。
- **不讀環境 proxy**:共用 HTTP session `trust_env=False`,`HTTP(S)_PROXY` / `NO_PROXY` / `.netrc` 一律無視,prompt-bearing POST 不會被環境變數帶去別的 host。
- **不跟隨 redirect**:任何 3xx 一律報錯(訊息含 status 與 Location host,絕不含 request body),拒絕把已送出的 POST 重送到別處。
- KB chunk 脈絡生成(Contextual Retrieval)有獨立的 `"kb_context_remote_ok"`（見[生成脈絡](#contextual-retrieval)）。**兩個鍵,不是一個**:前者放行的是 prompt,後者等於整份文件的窗離開這台機器;合併會把前者的同意無聲擴大成後者。
- `python3 scripts/doctor.py` 啟動前就會檢查:端點非 loopback 且未設對應 opt-in → FAIL。

這些規則涵蓋 CodeTrail 經 `llama_client` 發出的**所有**請求 —— 客戶端的聊天迴圈與壓縮
摘要都走這條路,沒有第二個 provider stack 會繞過它。同一台機器上的其他 process 當然不受
這裡管;NDA 場景仍要確認 `~/.config/codetrail/deployment.json` 的端點全是 loopback。

---

### 快速檢查表

- 從具體專案目錄跑 `aicode`,不要從 `$HOME` 或 `/`。
- 確認啟動前有 `MCP PASS — 21 tools + list_dir round-trip`；implicit 非 optimal 只代表
  routing 診斷警告，explicit failure 則會拒絕啟動。
- 不信任 repo 時在 `client.json` 設 `"project_instructions": false`。
- 要更嚴的權限時,用 `~/.config/codetrail/client.json` 的 `permission`(只能收緊)。
- 需要外部附件才在 `client.json` 打開 `"external_import": true`(每次匯入仍要人工核准)。
- remote endpoint 只在明確接受資料外送時設定對應 opt-in。
- commit 前跑 `git status` / `git diff`,確認沒有知識庫、上傳附件、jsonl 或 session 快取。

### Session、複製鍵與取消

Session 與輸入歷史含完整問題及回答，落在專案外的 state 目錄，目錄 0700、檔 0600，
讀寫都驗普通檔／owner／單 hard link 並以 dir-fd／nofollow 錨定。啟動不先建空 session；
採用歷史的唯一可信讀取同時供模型歷史與原始畫面，失敗保留舊 session。
`/copykey` 保存前重讀設定，只改 `copy_key`；合法鍵以明列集合檢查整條 Textual binding chain。
動態派發不累加舊綁定，忙碌／modal 可複製，Ctrl-C 的取消仍獨立。
滑鼠左鍵選取完成後自動複製，只接受仍有效的手勢來源畫面／輸入框；程式更新、重播、
捲動條或被遮住的選取不能觸發。複製不顯示常駐提示或成功通知，手動複製鍵仍可使用。

MCP SDK 不會在 read timeout／task cancel 自動送取消通知，客戶端自行配發 request id。
Ctrl-C 與固定 660 秒 read timeout 都送取消；寬限期過則 SIGTERM 該 instance，所有進行中
呼叫回 error 再重建。MCP stderr 預設不落檔，僅留有界記憶體尾端。
<a id="maintenance"></a>

## 維護與開發

本節是命令索引，執行權責以 [AGENTS.md §1](AGENTS.md#1-測試-policy) 為準。
預設 developer 在開發途中不跑測試，只在交付前跑一次 smoke；真實 bug 可依 red-before-green
單跑自己新增的 regression node。只有當次 prompt 明示 `ROLE=REVIEWER`，才在程式碼收斂後
對目前 HEAD 跑一次 full；程式未變不重跑已通過的測試。新失敗未處理不能交付，0 collected 不算過。

```bash
# 純靜態，不收集 pytest
python3 scripts/check_eval_consistency.py
python3 scripts/check_readme_consistency.py
python3 scripts/doctor.py --no-network
python3 deployment_profile.py validate
python3 codetrail_chat.py status

# 測試的執行時機以角色政策為準
python3 scripts/run_tests.py -m smoke
python3 scripts/run_tests.py
```

統一測試入口 `scripts/run_tests.py` 的 `--jobs N` 由 runner 自己處理（1–16），其餘支援形狀
見腳本。`--changed[=REF]` 不取代交付 smoke 或 reviewer full，也不授權開發途中額外跑測試。
需要新測試的情境限真實 regression 與無聲失敗契約；安全 node 必須帶 smoke 並登記
`tests/test_smoke_gate.py` 的 `SAFETY_MODULES`。改既有測試要列檔名、node 與行為理由。
不要因為放寬斷言或 skip 才變綠。未經使用者確認不要 commit。

### 變更後的同步點

- 工具名稱／順序改 `mcp_contract.PUBLIC_TOOL_ORDER`，同步 README、MCP 工具契約、canary／eval。
- config、文件與 eval expected 要一致；line number 只是 ±20 行 hint，不是硬契約。
- README、根目錄 developer 與 docs 全部接受文件 drift gate；安全與故障排解各自核對完整章節，
  一處缺句不能由另一處補足。runtime 的 `docs/compaction-rules.md` 保留原位。
- 壓縮規則與門檻從 `compaction_formula`／compaction-rules 的 canonical 文字取得，不能另抄一份。
- 新 incident kind/detail 必須同步封閉 slug 集合與契約測試；模型／render／parser 改動核對 cache 身分。
- repo 設定不新增環境讀取；spawn 經 `process_env`，pane 最終 exec 使用 `llama_server_env()`。

### 啟動與客戶端分工

`aicode` 只解析自己的 checkout／symlink、找到 python3、拒絕 argv／非 TTY、確認 Textual 後
exec `codetrail_chat.py`。`client_preflight` 負責 sandbox、deployment、live n_ctx、四服務能力、
lessons 與 MCP／模型 canary，結果以 argv／物件交給 Engine 和 MCP。正常啟動畫面空白，
所有摘要與警告留 `/status`；沒有 hidden web backend 或第二組模型工具。
<a id="review-artifacts"></a>

### Review artifacts 與資料保存

PDF 結構化圖片抽取會在專案內留下覆核用的檔案:

```
<專案>/.codetrail/figures/<document_slug>/<run_id>/
├── manifest.json          canonical manifest
├── assets/                原始 asset(從 PDF 抽出來的原圖)
├── variants/              **實際送給模型的**每一張圖(crop / tile)
├── review_assets/         **只為覆核 render、從未送給模型**的圖
├── review.md              給人看的摘要
└── revisions/<n>/         人工 fix 後的 canonical payload
```

原圖就是規格書的一塊,**可能含 NDA 內容**。這個 repo 的 `.gitignore` 已含 `.codetrail/`;
在別的 target project 用 CodeTrail 時,請在那個 project 的 `.gitignore` 也補上同一行。
`variants/` 與 `review_assets/` 的差別很重要:前者是**實際送給模型的**圖,後者只是為了
讓人覆核而 render、**從未送給模型**;`review_figures(action="list")` 每一張都會標
`crop_is_model_input`,只有標「模型輸入」的才是模型真的看過的那張。

`FIGURE_REVIEW_MAX_RUNS_PER_DOC`(預設 5,repo 常數)是 **soft retention target,不是硬上限**:被 KB `evidence_ref` 引用、`created_at`
判讀不出來、或清理失敗的 run 一律 fail-closed 保留,實際份數可能超過 5。
**不要拿它當「機敏影像最多留幾份」的保證** —— 要確定清掉就照下面顯式刪除並自己確認結果。

KB 的文件身分是 basename,artifacts 的身分是含路徑 hash 的 `document_id`。
不同目錄下的同名 PDF 現在會在**入庫時**就 fail-loud(列出兩邊完整路徑、零寫入),
所以不會再因為靜默覆蓋而產生孤兒 run 目錄。
**升級前的舊 KB 可能已經有**:那時候的覆蓋沒有留下紀錄,只能靠 run 數回收或下面的
顯式刪除清掉。

手動清除就是刪掉整個目錄:

```bash
rm -rf <專案>/.codetrail/figures/<document_slug>
```

**清掉之後的兩個後果要分清楚**:

- **查詢完全不受影響**。KB(`knowledge.json`,向量是它衍生出來的 cache)是 revision 的
  唯一真相,已入庫的 chunk 與向量都還在。
- **覆核能力會壞掉一半**。`review_figures(action="list")` 對那幾張會降級成 `payload: (讀不到)`,
  而沒有 canonical payload 就**無法做 `fix`**。要恢復,只能 `remove_document` 之後重新
  `ingest_document` 那份 PDF。

`remove_document`(從 KB 移除文件)與清除 artifacts 是**兩件獨立的事**:前者讓查詢查不到,
後者讓覆核做不了。要徹底清掉一份 NDA 文件的痕跡,兩邊都要做。

其他含內容的保存位置也要各自管理：

| 位置 | 內容與清除影響 |
|---|---|
| 專案 `knowledge.json` | 入庫文字與來源；刪除後該 KB 為空，cache 可重建 |
| 專案 `.aicode_uploads/` | 外部來源的副本；移除 KB 不會刪它或外部原檔 |
| 專案 `.codetrail/ingest/` | 續跑 checkpoint／抽取內容；不受 figure retention 清除 |
| `~/.local/state/codetrail/sessions/` | 專案分組的完整對話；實際檔位由 `/status` 查 |
| `~/.local/state/codetrail/data/<root hash>/` | 檢索紀錄與 KB／source snapshots，見資料飛輪 |

上表與原始文件都可能含 NDA。`.gitignore` 不保護重新命名、複製或匯出的內容；
分享 issue／eval 前核對實際 bytes，不將私有輸出放進 checked-in `eval/`。
<a id="code-index-design"></a>

### Code graph、語意表示與檔案種類

#### C/C++ 保守解析

`GRAPH_SCHEMA_VERSION=3` 保存 definition linkage/condition、function declarations、
include edge 的 preprocessor condition，以及 edge 的 `resolution_basis`/condition。

C/C++ 的 **definition 語意**(`ast_parser.PARSER_SEMANTICS_VERSION`)只認
translation-unit / namespace scope,並且逐個 declarator 判斷:

| 寫法 | 結果 |
|---|---|
| `uint32_t g_error_counter;` | 1 個 `global`(C tentative definition) |
| `static int a, *b;` | 2 個 internal-linkage `global` |
| `extern int only_declared;` | **0 個**(純宣告) |
| `extern int defined_here = 1;` | 1 個(有 initializer) |
| `int prototype_only(int);` | **0 個**(函式原型) |
| `static int (*handler)(int);` | 1 個 `global`(function pointer 是**物件**) |
| `typedef int count_t, *count_ptr_t;` | 2 個 `typedef` |
| `typedef enum { A, B } state_t;` | `typedef state_t` + `enum state_t`(用 alias)+ 2 個 `enum_constant` |
| `enum class State { Idle };` | enumerator 的 qualified_name 是 `State::Idle`(scoped) |
| `enum Plain { PlainA };` | enumerator 的 qualified_name 是 `PlainA`(unscoped,本來就在外層 scope) |
| `struct driver_ops;` | **0 個**(forward tag 不是定義) |
| `struct S { int x; };` | 1 個 `struct`(**要有 body** 才算型別定義) |

stable kind 寫死在 `ast_parser`:`macro` / `macro_function` / `typedef` / `enum` /
`enum_constant` / `global`。只有 `global` 有 linkage;macro / typedef / enum /
enum_constant 在 C/C++ 語意上沒有 linkage,graph 誠實標 `not_applicable`,不硬掰。
C 的 linkage 精確處理(`static`→internal、其餘 file scope→external);**C++ 縮限**:
anonymous namespace 與 `static` 是 internal,namespace-scope 的 non-volatile
`const`/`constexpr` 是 internal,`inline`/`extern` 是 external,template 內或帶未建模
specifier(如 `thread_local`)一律標 `unknown` —— **不猜 external**。

版本消費矩陣:`PARSER_SEMANTICS_VERSION` 進 CodeRAG cache meta、graph 的
`_parser_versions()` 指紋與 eval vector manifest 三處。它是**語意**版本,不是 table
shape —— 改它時**不要**順手 bump `GRAPH_SCHEMA_VERSION`。

cache 身分只有一份定義:`code_rag.cache_identity()`。它除了 schema / parser /
embed-text 版本,還帶**實際的 render 預算值**(清單以 `render_budgets`
為準,別另外記個數)—— 那些預算是 `config.py` 的常數,只鎖 schema
version 的話,改一個常數再重啟就會靜默沿用「用另一組 render
算出來的」embedding。寫入端、驗證端與測試 fixture 都從 `cache_identity()` 取:各寫一份
的失敗一樣無聲 —— 加了欄位而 fixture 沒跟上,舊 cache 被拒、那條測試改走 full rebuild,
「還是綠的」卻不再驗它本來要驗的東西。
既有舊版 DB（v1/v2）由同一條顯式 build command 在單一 SQLite transaction 中原地
升級；升級失敗會 rollback。真正損壞、無法由 SQLite
開啟的 DB 不宣稱能原地重建：錯誤會要求先移出/刪除 graph DB，再執行 build command。
C/C++ call 只按下列證據順序解析：同檔定義、C++ exact qualified name、實際 included
header 的 static-inline 定義、direct/transitive repo-header 可見且 qualified identity 相符的
prototype 對應唯一 external 定義；其餘維持 ambiguous 或 unresolved。候選不再由第一個
condition-incompatible stage 截斷：可證明是同一 preprocessor chain 的互斥 branch 才排除，
其餘跨 stage 合併成明示 ambiguity。bare call 不會配到別的 C++ scope/method；`static` 與
anonymous namespace definition 不跨 translation unit，function pointer/macro 不猜。quote
include 沿用 repo resolution；angle include 只有帶 namespace path、非絕對且唯一 suffix
命中才進 visibility closure。bare `<stdint.h>` 的單一 repo basename 不會誤配，多個同名
repo candidate 會留下 ambiguity edge；絕對 angle path 不做 suffix 配對。`.hh` / `.hxx`
已在 `CODE_EXTENSIONS`、index scope 與 tree-sitter parser 三層按 C++ header 接通。

C/C++ 任一檔案 add/change/delete 都把檔案 hash 當作完整 visibility fingerprint 並走
full rebuild；這是刻意的保守 invalidation，避免 linkage/declaration/include closure 的
partial cone 與 fresh build 漂移。Python 仍走既有增量路徑；body-only edit 因 callable
node-id catalog 沒變，不會只因同名 C call 就 fan-out。只有名稱、qualified identity 或
overload identity 改變且牽動 C/C++ caller，才會在寫 DB 前切換成 full rebuild。相關
pytest gate 是 `tests/test_code_graph.py`（tree-sitter 解析、graph、C++ 可見性都在這一個
檔）；reviewer 由收斂後的 full 統一涵蓋。developer 修 bug 時只依 AGENTS.md §1.3 單跑自己
新增的 regression node 取得 red / green，不另跑整個 module。`python3 eval/run_code_smoke_eval.py` 也只在本次任務明示要檢查 code-inference
品質時執行。

---

#### 語意表示式與預算

三個消費者共用**同一組 canonical 欄位**(`code_rag.CANONICAL_SEMANTIC_FIELDS`),
但各有各的預算:

| 消費者 | 預算常數 | 預設 |
|---|---|---:|
| index entry 儲存的 context | `CODE_RAG_CONTEXT_STORE_MAX_CHARS` | 1800 |
| index entry 儲存的 leading comment | `CODE_RAG_COMMENT_MAX_CHARS` | 400 |
| index entry 儲存的 docstring | `CODE_RAG_DOCSTRING_MAX_CHARS` | 300 |
| dense embedding document text | `CODE_RAG_EMBED_TEXT_MAX_CHARS` | 1200 |
| lexical scorer 掃描文字 / identifier 數 | `CODE_RAG_LEXICAL_SCAN_MAX_CHARS` / `CODE_RAG_LEXICAL_MAX_IDENTIFIERS` | 1200 / 80 |
| reranker passage | `CODE_RERANK_PASSAGE_MAX_CHARS` | 1800 |

**儲存端是最上游的截斷**:`CODE_RAG_CONTEXT_STORE_MAX_CHARS` 比下游任何預算小的話,
調大下游全部是 no-op(這是 2026-08-20 之前的真實狀況:context 在 index entry 就被截到
500,所以「把 embed text 從 400 調大」完全沒有效果)。`tests/test_code_rag_search.py`
靜態守住這條不變式。

C 的 `/** ... */` 寫在定義行**之上**,而 context 從定義行往下取,結構上永遠拿不到 ——
所以 leading comment 是獨立欄位(`Symbol.comments`),邊界有四條:同 scope(只走
sibling)、不跨空行、不跨 preprocessor 或其他節點、不吃檔頭 license。三個消費者
**都**看得到它;只加進 embed text 而 lexical 還在掃舊 context 的話,那條 lane 會靜默
看不到註解訊號。

`EMBED_TEXT_SCHEMA_VERSION` 的維護規則(權威定義在 `code_rag` 該常數的宣告處)
寫成**白名單**:

> **免 bump 的只有「已經列在 `cache_identity()` 的 `render_budgets` 裡的預算數值」**
> 。其他任何會改變 render 輸出的修改 —— 欄位集合、欄位順序、
> label 文字、分隔方式、截斷演算法,以及**任何還沒進 `render_budgets` 的截斷數字**
> —— 一律「bump,或先把它納入 identity」。

寫成白名單而不是「預算免 bump」,是因為後者會被讀成「這個數字也是預算,所以不必
bump」,而沒進 identity 的數字改了又不會讓任何東西失效 —— 兩邊都不動,舊向量就被
靜默沿用。踩過的例子是 docstring 的 `[:300]`:它確實是預算,但沒有名字也沒進
identity;現在它是 `CODE_RAG_DOCSTRING_MAX_CHARS`,規則因此變成機械可判定。
上游那一刀不歸這條規則管:`ast_parser` 建 `Symbol` 時就先截過 docstring /
signature / condition,也決定 leading comment 取幾行 —— 那些屬
`PARSER_SEMANTICS_VERSION`(同樣在 `cache_identity()` 裡)。
兩條機制都必要:增量重建只比 file_hash,少了任何一邊都會靜默沿用用舊 render
算出來的向量。

實測(fixture corpus,per_repo / runtime_hybrid):leading comment 讓 macro-average
file recall 0.6683 → 0.7783、MRR 0.900 → 0.950,context coverage(1.000)、evidence
precision(0.293)與 used chars(76514)不變。**1200 與 1800 的 A/B 在這份 fixture 上
分不出差異** —— 最長的 document 表示式只有 536 chars,兩個上限都不會截到任何東西。
要調這個數字必須拿真實 firmware repo 重測,不能拿 fixture 的結果當依據。

#### FileKindPolicy

`file_kind_policy.py` 是 `CODE_EXTENSIONS` 與 `GREP_DEFAULT_EXTENSIONS` 的**共同來源**。
以前兩份手寫清單已經漂了:grep 那份比索引窄,連 `.cc` / `.cxx` / `.pyi` / `.pyx` /
`.bash` / `.txt` / `.mk` / `.cfg` / `.cmake` / `.ini` / `.conf` / `.tcl` 都搜不到。

三個投影:grep glob、index scope 成員資格、symbol parser route。canonical suffix 一律
小寫(比對走 `Path.suffix.lower()`),但 **grep glob 是 case-sensitive**,所以 `.S` 會
另外產一條 `*.S`;`Makefile` / `Makefile.*` / `Kconfig` / `Kconfig.*` 走 basename 規則,
規則本身也進 index scope fingerprint(否則規則改了成員資格會靜默漂移)。

新增的韌體類型:`.s` / `.asm` / `.ld` / `.lds` / `.dts` / `.dtsi` / `.inc` / `.def`。
**這是 Level 1,只承諾 grep / search discoverability** —— 它們**不會**進 dense symbol
retrieval(沒有 parser,進 symbol 掃描只有零 symbol 的 walk / hash 成本)。ASM 與
linker script 的 symbol 抽取是 Level 2,整包延後:ASM 要 two-pass(先收 `.globl` /
`.global`,再只配對相應 label,排除 `.L*`),linker script 要抽 MEMORY region /
output section / `ENTRY()` / symbol assignment。寧可延後,也不要用粗 regex 製造大量
假 symbol。`.md` / `.txt` 維持既有分工:留在可見範圍供 grep / list_dir 使用,但不進
symbol 掃描。`.cfg` / `.json` / `.sh` / `.mk` 這些設定檔**仍在** symbol 掃描範圍 ——
`_scan_code_files()` 的輸出同時是 bounded context 的 `allowed_paths`,把它們排掉會讓
`config/*.cfg` 這類 gold evidence 變成讀不到。
<a id="patch-design"></a>

### Patch 與工具契約

`agent_tools.ToolExecutor.apply_patch` 是唯一的寫檔管線；兩種輸入格式（SEARCH/REPLACE、unified diff）
在 `patch_engine.py` 正規化成同一種 per-file plan 後，走同一條 sandbox → 上限 → preflight →
journaled 寫入 → best-effort rollback。

- `patch_engine.py`（不 import config / agent_tools；上限由呼叫端傳入）：格式偵測與嚴格 S/R
  狀態機、path 規則、`FileSnapshot`（UTF-8 strict、BOM、LF/CRLF、mode/ino/dev）、exact+rstrip
  定位、結構化 mismatch record 與單一 renderer（整次回覆的 mismatch 預覽合計 40 行／2000 字元）、
  寫入層與 `WriteJournal`。POSIX 上以 dir_fd 錨定實作（逐層 `O_DIRECTORY|O_NOFOLLOW`、同目錄
  temp、既有檔 `os.replace`、新檔以 hard link 不覆蓋發布）；缺少 dir_fd / nofollow 時拒絕操作，
  hard link 不可用時中止並回滾。已驗證的行為契約（`tests/test_apply_patch.py`）：preflight 後
  preimage 被改 → 中止並回滾（`test_preimage_changed_after_preflight_aborts_and_rolls_back`）、
  新檔目標在 preflight 後被競爭者建立 → 中止且不覆蓋
  （`test_new_target_created_by_competitor_after_preflight_aborts`）、第一檔已寫入後被第三方修改
  → rollback 保留現況並回報 conflict（`test_rollback_does_not_overwrite_third_party_modification`）、
  dir_fd 不可用時零寫入（`test_missing_dirfd_refuses_patch_and_keeps_targets_untouched`）。
  這些競態防線是以 monkeypatch 在既定時點注入變更來驗證，不是真實併發測試。
- `patch_verify.py`：套用後的自動驗證只做同 process、無 subprocess、唯讀的 syntax check
  （`.py`/`.pyi` 用 ast；C/C++ 只在釘版 tree-sitter grammar 載入時檢查 ERROR 與零寬 MISSING
  node）；三態 passed / failed / skipped，任何 skipped 都渲染成「驗證不完整」，失敗不回滾。
  import 集合由 `tests/test_patch_verify.py::test_patch_verify_module_import_allowlist_is_exact`
  用 ast 釘成 allowlist，`test_auto_verify_true_spawns_no_subprocess` 守住不 spawn；lint / test
  由 `run_lint(fix=False)` / `run_command` 顯式呼叫，各自經人工核准。
- `run_command` 的 `timeout` 三層同值（native schema、executor、MCP
  `Annotated[int, Field(strict=True, ge=1, le=600)]`），常數在 `config.RUN_COMMAND_TIMEOUT{,_MIN,_MAX}`；
  `scripts/check_readme_consistency.py` 第 9–11 條把 5／200、1..600、dry_run 七欄位與驗證分層
  宣稱釘在 MCP docstring、native description 與文件上。

#### Tool contract、build prompt 與部署 canary

| 模組／入口 | 契約 |
|---|---|
| `mcp_contract.py` | `PUBLIC_TOOL_ORDER` 是 live 21-tool 名稱與順序唯一來源；同檔也定義 bounded FastMCP instructions 與 evidence-tool 集合。 |
| `tool_result_adapter.py` | 每個 tool call 都產生單一 compact text block；首行 `status: ok|partial|error`，需要修復／續讀時才有 `next:`。省略 `max_chars` 時以 call-time `config.N_CTX` 的 12% token proxy 配置，明示過大值標 `context_risk`；evidence tools 保留 core structured payload。 |
| `scripts/tool_call_canary.py` | live MCP protocol；explicit 點名工具 hard gate（retry 一次）；implicit 未點名工具單次診斷，四態 `optimal/suboptimal/fail/timeout` 不擋啟動。schema 2 分離 cache lane 只存 hash/status/time/version；`supports_tools=false` 在 model attempt 前 fail。 |
| `scripts/mcp_catalog.py`／`scripts/eval_tool_routing.py` | effective stdio catalog、privacy-safe routing classification/gates 與 frozen historical baseline replay；harness 永不自行把 matrix row 升級成 supported。 |

部署 live-after 不進 CI，也不是所有開發環境必綠。受授權且相容的乾淨部署才執行
`python3 scripts/tool_call_canary.py --root <PROJECT> --force` 與 routing eval 真模型 arm，記錄客戶端／MCP SDK、
模型／chat template／effective config、explicit 與 implicit 結果。環境不可得時逐字回報
`not run: environment unavailable`；未授權、未跑或 gate 未通過都保持 incomplete，不能阻擋
離線驗收，也不能宣稱 `supported`。
<a id="context-budget"></a>

### Context、thinking、預熱與取消

聊天 Engine 每次把正式的完整 payload（system、tools、tool choice、reasoning、工具結果與模板）
交給 `/v1/chat/completions/input_tokens`，精確計數後才過 gate。保留額等於實送 `max_tokens`，
計數失敗就拒絕；不退回字元估算、舊計數或一次生成探針。一般回答、收斂、摘要與 prime
都走同一條線。既有 native 內部呼叫才保留 heuristic；`knowledge.py` 的主模型
`/completion` 唯一出口是 `_gated_completion`。

| 模組 | 責任 |
|---|---|
| `context_budget.py` | 完整輸入計數、hard gate、usage 解析及只有 counts／metadata 的 telemetry |
| `trim.py` | 只剪送模的舊 tool 訊息，保留 file:line／error facts 和明確標記；不改 system／user 原文 |
| `code_context.py` | deterministic 程式證據選取與字元裝箱；不是 LLM 容量 gate |
| `context_signals.py` | retrieval 與 content-only gate 的組字、hash、BM25、reranker 單一來源 |
| `extracted_document.py` | raw_text、章節、頁碼、chunk span 的單一來源 |
| `llama_client.py` | 共用 chat body builder 與受限制 HTTP transport；token 計數只接受非 bool 非負整數 |
| `gpu_safety.py`／`client_preflight` | 觀測 live `/props` 的正整數 n_ctx，以同值交 Engine／MCP；無 live 值拒絕回合 |

主聊天 `/think` 每次啟動 off；能力只來自 GGUF Jinja AST 實際偵測的
`services.main.thinking_kwarg`。每一步快照一次，計數／生成共用 extra；
`enable_thinking` 與 `thinking` 一律送相同 JSON boolean。摘要、review、prime、canary、
RAG 等內部生成固定 off。`show_reasoning` 只改顯示，`keep_historical_reasoning` 只改送模轉換。

新增主模型 call site 在呼叫當下使用 `config.require_main_model()`，不取 import-time snapshot。
聊天沿用 `Engine.count_input_tokens()`，將同份 payload 的計數與實送輸出保留額交給 gate。
native 內部呼叫在 HTTP 前 `context_budget.check_and_log()`，成功後解析 usage 並 log metrics。
embedding／reranking 使用自己的輸入預算，VL 的 image token 也不能假裝用文字估算驗過。

#### Telemetry 的計數與隱私

`.codetrail/context_metrics.jsonl` 只收模型／來源／容量／counts／耗時／固定 error type，
不保存 prompt、tool output、檔案內容或問題。`count_method="llama_cpp_chat"` 表示送出前量到
完整輸入；舊 `heuristic` 紀錄才是估算。`usage.prompt_tokens` 可填完整輸入，
`timings.prompt_n` 只填 `prompt_tokens_processed`（本次重算量），兩者不能互補。
TUI 晚到且 session／history／thinking 已變的 context 計數要丟棄，未知顯示 `?`。

#### 預熱、歷史與取消

互動啟動用 `Engine(defer_session=True)`，不建 session；mount 可以對空 prefix 預熱。
首問 `bind_new_session()` 只有 create 成功才綁 id，不換 history／epoch、不 abort 相同 prefix；
失敗留草稿與空白狀態。未綁定時寫入／送出／替換 history 與 queue add／resume 都先拒絕。
採用歷史先完成可信讀取及原始 transcript，成功才 adopt；session 切換不得留下半換狀態。

固定 pruning plan 在 new／adopt／replace 建立，append 不移切點。送模順序是 plan → heal →
reasoning 剝除；heal 補的 tool result 緊接宣告 assistant 群組的既有結果。最新真實 user
之前才剝 reasoning，認不出 user 就不動；session 原文與畫面完整保留。

| 介面 | 不可放寬的契約 |
|---|---|
| `next_turn_prefix()` | 下一輪 payload 的同一份轉換，佔位 user 不落檔也不送出 |
| `prime_prompt_cache()` | interactive／thinking off 才可；零 session／history／事件寫入，非阻塞取共用模型鎖後快照、slots、計數、gate，再送 `max_tokens=1`，保留額也是 1 |
| `abort_prime()` | 先作廢世代／Event，shutdown 已登記 socket 再放鎖；取消後才連上的 socket 先關閉，不能補送 HTTP |
| `http_cancel.RequestCancellation` | 專用 transport，涵蓋 headers 前／DNS／connect／TLS；並行取消等待 shutdown，保留 TLS 驗證，不改共用 session／pool |
| `prepare_idle()` | codetrail 接續歷史超門檻時先可取消壓縮，完成收尾才排 prime；manual／off 不自動摘要 |
| `prime_in_background()` | 不取回合鎖、不動取消狀態；每次 outcome 先 `on_prime` 再 `on_done` |

prime 只有終結 chunk 且帶 `timings.prompt_n` 才算 sent；EOF／no_timings 不寫 telemetry。
new／adopt 切換期間不准舊 history 預熱，create／adopt 失敗恢復原准入；中止需在一秒內完成
且模型鎖已放。主聊天 thinking on 跳過 prime，切換 thinking 先 abort 再作廢計數。

過大摘要按完整 user-turn 分批，每批精確 gate／七欄格式驗證，replacement 也驗容量；
全部成功才先落檔再換記憶體。網路／計數故障可重試，空／reasoning-only／格式漂移等不可信
產出才 durable 停用。最後未回答 user 逐字留 tail，取消回合不是可恢復待辦；詳見
[runtime 壓縮規則](docs/compaction-rules.md)。

同 MCP instance 的所有 Engine 共用模型鎖。回合取消涵蓋 worker 尚未 send、等待核准、
串流及 MCP call，核准只回答一次且只接受真 bool；閒置取消回 False。取消不是答案，
不得追加假 assistant final；必須送 `step_finish(reason=cancelled)`，並以 append-only 身分記錄
綁定真實 user 與已接收 supplement。收斂只用本輪實際工具證據，最多一次，先 gate 再用
`tool_choice=none` 請求；錯誤或再次工具宣告不能冒充答案。
<a id="index-scope"></a>

### 索引範圍與快取

**只影響 Code RAG 索引。`grep_code` / `list_dir` 完全不受影響** —— 檔案還是找得到、還是
grep 得到,只是不再吃掉語意檢索的名額。這條界線是刻意的:使用者以為檔案不見了比雜訊更糟。

#### 不變式

> 檔案是否進索引,**由且僅由** `index_scope.IndexScope.should_index_file(rel)` 決定。
> `decide_dir()` 的三態(`PRUNE` / `TRAVERSE_ONLY` / `INDEX`)只是剪枝優化,必須保守:
> `PRUNE` 僅在「其下不可能存在任何能通過 `should_index_file` 的檔案」時才允許。

`tests/test_code_rag_index.py::test_tri_state_walk_matches_should_index_file` 對合成樹全量
枚舉逐檔求值當基準,再跑三態走訪比對 —— 任何 `PRUNE` 吃掉應索引檔案就立刻紅。改剪枝
邏輯時先看那條測試。

#### 分層

| 層 | 內容 | 誰吃 |
|---|---|---|
| A | `config.IGNORED_DIRS`(19 名,**凍結**)+ dot 目錄規則 | 索引 / grep / list_dir |
| A′ | `config.INDEX_ONLY_IGNORED_DIRS` = `site-packages` / `dist-packages`;段精確、case-insensitive | 只有索引 |
| B | 結構偵測器,**永遠不收專案名**。B1 標記:目錄含 `pyvenv.cfg`、含 `conda-meta/`;B2 段規則:段 `^python\d+(\.\d+)*$` 且父段 ∈ {`lib`,`lib64`}、段以 `.egg-info` 結尾 | 只有索引 |
| C | 部署層 `~/.config/codetrail/index-scope.json`(見下) | 只有索引 |

規則鏈(對檔案路徑求值):hard gates 全過之後,才是
`C.include` > `C.exclude` > `B` > `A′` > 預設索引。hard gates 有七條,語意上全是 AND
(所以評估順序只影響 syscall 成本,不影響結果):dotfile / 索引產物檔名 →
`CODE_EXTENSIONS` → `should_ignore_file` → 祖先段命中 A → containment(realpath 仍在
root 內)→ 不是實際載入的 index-scope.json → 是 regular file。

最後兩條是 review 補的:設定檔要是被放進 root 就會自己進索引(`.json` 在
`CODE_EXTENSIONS` 裡),違反它「永不進 repo/輸出」的契約;而 FIFO / socket / device
一路讀下去會**永久阻塞**建索引(`read_bytes` 在 FIFO 上不會回來),`os.walk` 的
`filenames` 是會列出它們的。

刻意的限制,不要「好心修掉」:

- **A 命中不可用 `C.include` 救回。** 救回等於擴大 committed 行為的索引範圍,違反
  「只做減法」,而且會逼 `list_dir` / grep 連動。
- **root 自身不套 A′ / B。** 使用者的專案根剛好叫 `site-packages`、或根目錄放了
  `pyvenv.cfg`,整棵樹被剪掉是最糟的誤殺(設計原則:寧可漏排,不可誤殺)。

#### index-scope.json (Layer C)

`~/.config/codetrail/index-scope.json`,**永不進 repo、永不出現在任何輸出**
(`index_stats` 連 pattern 內容都不印)。位置只由 `HOME` 推導,**沒有覆寫變數**
—— 一個「檔案在哪」的環境變數只會讓 runtime 與 `index_stats` 各自看到不同的檔,
而使用者以為它們在講同一份。

```json
{
  "schema_version": 1,
  "roots": [
    {
      "root": "/abs/path/to/tree",
      "mode": "denylist",
      "detectors": true,
      "exclude": ["vendor_env/**"],
      "include": ["vendor_env/keep.c"]
    }
  ]
}
```

- **檔案不存在 = 正常預設**,不 fail-loud:絕大多數部署者一輩子不需要這個檔。
- **錯誤訊息永遠不含 pattern 內容。** pattern 就是樹狀結構本身
  (`nda_customer_x/...`),而 fatal 訊息會被貼進 issue。一律用
  `roots[i] 的 exclude[j]` 定位;壞 regex 連底層 `re.error` 的訊息都不能轉述
  (它會嵌入出錯的字元),`raise ... from None` 也是必要的,否則 exception chain
  照樣把它印出來。
- **檔案存在但壞掉 = fail-loud**:未知鍵、schema_version 不符、重複 selector、
  非絕對路徑 `root`、pattern 衛生違規(`!` / `..` 段 / 空 / NUL / 每 root >200 條 /
  單條 >512 字元)、POSIX 權限不是 owner-only(訊息附 `chmod 600`)。
- `root` 是**選擇器,不是掃描根**:canonicalize(realpath + normcase)後與當下的
  sandbox root(`mcp_server --root`,客戶端以 cwd 決定)精確比對。沒匹配到不是錯誤,
  但 `index_stats` 會印 `C: no matching selector`。
- `mode`:`denylist`(預設)/ `allowlist`(只有 include 列的進索引)。
  **allowlist 下給非空 `exclude` 直接 fail-loud** —— `C.include` 優先於 `C.exclude`,
  在 allowlist 恆為死碼,靜默接受會養出錯誤心智模型。
- `detectors: false` 停用該 root 的 B1+B2(A′ 仍生效)。這是整樹級的鈍器;
  A′/B 的個別誤殺本來就能用 `include` 救。
- Glob 方言:gitwildmatch 子集,in-repo 實作(不引入 `pathspec` 依賴),比對「相對 root
  的 POSIX 路徑」、case-sensitive、`**` globstar、前導 `/` 錨定 root。**目錄比對補尾斜線
  再比** —— 所以 `vendor_env/**` 不匹配 `"vendor_env"` 但匹配 `"vendor_env/"`。
  向量鎖在 `tests/test_code_rag_index.py::test_matcher_vectors`。

#### 快取遷移

`scope_fingerprint`(canonical JSON hash:C 展開後的 include/exclude 含順序、`mode`、
`detectors`、A′/B 的實際規則值、`CODE_EXTENSIONS`、檔案 ignore policy、matcher 版本、
schema 版本)寫進 `.code_rag_cache_meta.json`。

- fingerprint 缺失或不符 → **禁止**整包 fast load 與掃描快取 fast path,強制重算 membership。
- `embedding_model` 相同 → **保留** per-file symbol/embedding cache:scope 改了只重算
  membership delta,**不重 embed**。
- 任何來源的 cached path 一律**重過** `should_index_file`,不得直接信任。
- **dense 模式復用快取時要補算 lazy 留下的空 embedding**(`_backfill_cached_embedding_gaps`)。
  lazy 模式(符號數 > `CODE_RAG_LAZY_EMBED_MAX_SYMBOLS`)把 embedding 存成 `[]`,延後到
  查詢時才算;之後索引縮小到門檻以下就會走 dense,那些空洞會直接觸發
  「refusing zero padding」fail-loud,而且失敗不寫快取 → **重啟照樣失敗,索引永久建不
  起來**。索引縮小正是 index scope 的主要場景,所以這條是必修,不是防禦性程式碼。
  回歸鎖:`test_dense_rebuild_backfills_lazy_embedding_holes` /
  `test_lazy_index_shrunk_by_scope_still_builds`。
- **backfill 失敗要走 `_reset_partial_index()`**:embedding server 中途掛掉時,
  index / embeddings / `_indexed_file_hashes` 三個都得清掉。留任何一個,`query()`
  就會因為 index 非空而不重建(`_refresh_if_stale` 也因為 hashes is None 直接
  return),整個 MCP process 會一路用缺 embedding 的索引降級下去。回歸鎖:
  `test_backfill_failure_leaves_no_partial_index`。
- scope 熱重載是另案;改了設定要重啟 MCP server。
<a id="contextual-retrieval"></a>

### Contextual Retrieval 與 KB 對照

入庫時替每個 chunk 生成一段 50–100 token 的定位文字(「本節出自 <文件> 的 <章節路徑>,
說明 <主題>」),存進 chunk 的 `ctx` 欄位,只餵檢索訊號。**兩個旗標都預設關閉。**

```bash
# 生成(唯一會生成的路徑;MCP 的 ingest_document 永遠不生成)
python3 RAG.py rebuild --kb knowledge.json spec_a.pdf --context      # 旗標 > config
python3 RAG.py rebuild --kb knowledge.json spec_a.pdf --no-context   # 這次不生成
```

生成與查詢的開關是 `config.py` 的常數(改 repo,所有使用者一致);
遠端同意是 `client.json` 的鍵(每個使用者自己決定要不要讓文件離機)。

| 設定 | 位置 | 預設 | 作用 |
|---|---|---|---|
| `KB_CONTEXT_GENERATE` | `config.py` | off | 入庫時是否生成 ctx |
| `KB_CONTEXT_USE` | `config.py` | off | 查詢時是否使用 ctx(kill switch) |
| `kb_context_remote_ok` | `client.json` | off | main URL 非 loopback 時的顯式同意 |
| `KB_CONTEXT_TARGET_TOKENS` | `config.py` | 100 | ctx 長度上限(回應後截斷) |
| `KB_CONTEXT_REASONING_TOKENS` | `config.py` | 512 | 請求端額外留給 reasoning 的額度 |
| `KB_CONTEXT_WINDOW_SAFETY` | `config.py` | 0.8 | 窗預算的 n_ctx 安全係數 |
| `KB_CONTEXT_MAX_ABSENT_RATIO` | `config.py` | 0.20 | 絕跡率超過就中止發布 |
| `KB_CONTEXT_CACHE_DIR` | `config.py` | `~/.cache/codetrail/ctx` | ctx 快取(repo 外、per-root、0700) |

**為什麼預設關閉**:(a) standalone 的 `RAG.py` 目前只依賴 embedding server,預設開啟等於
替既有部署新增一條 main-server 硬依賴;(b) 部署允許 main URL 指到非 loopback,預設開啟
等於在沒有明確同意下把整份文件的窗送去遠端(NDA)。

**雙訊號是這個功能的正確性核心**。`ctx` 是 LLM 生成物,只准影響「哪些 chunk 被撈上來、
排第幾」。所有**決策**——拒答閘、信心標記、rerank/expansion 的 skip、數值證據判定、
污染控制的分數門檻——一律讀 content-only 的 gate 訊號:

- NPZ 存兩組矩陣:`embeddings`(retrieval,含 ctx)與 `embeddings_gate`(content-only),
  各帶自己的 schema/hash/維度/列數,同一個 `store_generation` 一次提交。
- BM25 也是兩套索引。gate 那套的來源文本與加入本功能之前逐位元組相同。
- `Candidate` 這個 dataclass 把 `retrieval_*` 與 `gate_*` 分開:哪個分數餵哪個決策在型別層
  看得出來、grep 得到。**看到 `candidates[i][1]` 這種寫法就是退化。**
- KB 有 ctx 卻缺 gate 矩陣 → 拒載,不 fallback 到 contextual 向量。
- gate 向量只留在矩陣裡,不 `.tolist()` 掛回 chunk;決策點用 `chunk_idx` 讀列。

`config.KB_CONTEXT_USE = False` 時查詢端完全退回 content-only:dense 讀 gate 矩陣、BM25 用
content-only 索引、reranker passage 不加 ctx。**同一份 KB 上就能做乾淨的 A/B**,不需要
第二套 KB。

**成本**:每個 chunk 一次主模型呼叫。實測(21 chunk 合成語料、DeepSeek-V4-Flash)約
10 秒/chunk,prompt-cache 重用約 89%。文件沒變 → 全部命中快取 → 零 LLM 呼叫。

---

#### scripts/kb_ab_compare.py

知識庫體檢與 A-B 對照。單一 KB 時做離線體檢（NPZ schema 是否現行版本、多少 chunk 帶
`[HEADING]` 前綴 / char span、多少 chunk 的 section 是空的、哪些章節標題重複到連
heading hierarchy 都分不開）；給兩份 KB 再加結構差異（**content 位元組有沒有變** →
決定既有向量還能不能用、section 差在哪幾筆）；加 `--questions` 才會跑檢索，需要
8081 / 8082。

```bash
python3 scripts/kb_ab_compare.py ~/proj/knowledge.json                       # 體檢
python3 scripts/kb_ab_compare.py old/knowledge.json new/knowledge.json       # 重建前後對照
python3 scripts/kb_ab_compare.py old/knowledge.json new/knowledge.json \
    --questions ~/questions.txt                                             # 加跑真題
```

**兩份 KB 一定要放不同目錄**：工具會直接擋下同目錄的組合，不會靜默比錯。（2026-08-24
起 embeddings cache 依 KB 檔名分目錄，同目錄兩份 JSON 其實已經不會互相覆蓋向量了；
這條限制留著只是保守，兩份 KB 分開放本來就比較好對照。）另外體檢一律以
`allow_rebuild=False` 載入：它要報告的是「這份 KB 現在的磁碟狀態能不能直接載入」，
自動重建會把答案改掉。預設只印 metadata 與計數，
不印 chunk 內容（NDA）；要看抽樣前綴得自己加 `--show-content`。問題檔與真實文件都
不進 repo。

重建一份對照 KB 就是把來源文件逐一灌進獨立目錄：

```bash
mkdir -p /tmp/kb-baseline && cd /tmp/kb-baseline
for f in <doc1> <doc2>; do python3 /path/to/CodeTrail/RAG.py "$f" ./knowledge.json; done
```

（embedding 快取是 CWD 下的 `.rag_embedding_cache.json`；把舊的複製進來可大幅減少
重算，內容沒變的 chunk 會直接命中。）
<a id="index-stats"></a>

### 離線索引統計

完全唯讀、完全離線的計數工具。**預設輸出只有計數,不含任何路徑** —— 這種輸出會被貼進
issue,路徑本身就是 NDA 內容。

```bash
python3 scripts/index_stats.py --root /path/to/tree
python3 scripts/index_stats.py --root /path/to/tree --deep        # 真的跑 AST 算符號數
python3 scripts/index_stats.py --root /path/to/tree --show-paths  # 顯式 opt-in 才印路徑樣本
```

root 只能來自 `--root`,沒給就報錯不猜 cwd(掃錯樹是靜默的);驗證復用
`root_safety.validate_aicode_root`(和 MCP server 同一份,拒絕 `/`、`$HOME`、
不存在 / 非目錄)。root 不合法或 index-scope.json 壞掉都是乾淨的 `[FATAL]` + exit 2,
不吐 traceback。

符號數預設讀既有 cache,標成 `N (cached)`;**只有每個檔案都在快取裡而且 hash 對得上**
才給數字,少一個或過期就印 `unknown` —— 這個數字的用途就是判斷索引範圍對不對,
報一個過期的數字比報 unknown 更糟。hash 用 `code_rag.compute_file_hash` 同一份實作,
不另寫。`--deep` 才真的跑 AST,有檔數與時間預算,超過會標 `truncated`。
<a id="data-flywheel"></a>

### 私人檢索紀錄與 data flywheel

`data_flywheel.py` 才是互動資料收集器。它**永久開啟、沒有開關**：以前是 `client.json` 的
`collect_data`，那個鍵已移除（留在檔裡會 fail-loud，拿掉即可）。唯一不寫的是 readonly session
（canary / eval / replay 跑的是合成題目）：`client_config.apply_to_config(readonly=True)` 把
`config.COLLECT_DATA` 關到底，routing eval 自己的行程也一律關，不靠使用者先改設定。

輸出位置**固定**（沒有覆寫鍵），要撈檔案就去這個目錄：

```text
~/.local/state/codetrail/data/<root 雜湊>/interactions.jsonl
```

`python3 data_flywheel.py where --root <專案>` 印出某個專案的確切目錄
（雜湊算的是 resolve 後的絕對路徑，同一個專案永遠同一個目錄）。

每一筆紀錄的 `metadata.trace` 是那一次檢索的**完整路徑**，由 `KnowledgeBase.query()` 產生：
模型送進來的查詢與 expansion 擴寫、hybrid 候選（chunk id / 來源 / 頁 / 章節 / RRF / 檢索分 /
gate 分 / BM25）、門檻與 margin 決策、通過 gate 的清單、reranker 有沒有真的跑與各分數、
MMR 選了誰、污染控制、最終 REF（成員 id、分數、截斷、前 200 字）與信心結論；`stage` 記到
哪一步就是在哪一步結束。同一筆也記這次生效的檢索設定與 KB 的 `store_generation`，所以改了
RAG 之後可以拿舊紀錄的查詢重跑、逐階段比對。候選**不設上限**，reranker 評過分的每一個都
留（`rerank.scores`），提早結束的紀錄帶 `stopped` 原因與 strict 排除的完整清單。
`code_rag_search` 的紀錄則記整個候選池（`pool`，combined 排序前 200、含落選者）的
embedding / lexical / 融合 / rerank 分數與過門檻、最終旗標，以及 embedding / reranker 設定，
不帶程式碼文字。檢索途中炸掉（reranker timeout 之類）也記一筆：`metadata.failed=true`、
`error_type`、`error`，trace 停在炸掉的那一步。
MCP 端的 `question` 是模型送進工具的查詢字串、`answer` 只是 REF 標頭（MCP 沒有回合邊界，
看不到使用者原話與最後的回答）；要對回整段對話，用 session 檔的時間戳與工具參數對上。

同一個目錄底下的 `snapshots/`：每個 KB generation 存一份 `kb-<store_generation>.json`，
內容是 KB **載入時真正解析的那份 bytes**（`KnowledgeBase(on_loaded=…)` 交給收集器，MCP 啟動與
自動重載都掛這個 hook；紀錄時只認 `store_generation` / `file_sha256`，不再事後讀磁碟——那時
可能已經是別的 generation）。被引用的原始檔存成 `blob-<sha256>`；各只存一次，既有快照每次都
重驗（普通檔、owner、nlink、0600、內容雜湊），壞了能修就修、修不了在該筆寫 `snapshot_error`，
絕不把壞名字寫進紀錄。紀錄裡 `trace.kb.snapshot` + `snapshot_sha256`、`trace.blobs[path] =
{snapshot, sha256, matches_index}` 指向它們。`trace.files[].index_hash_algorithm` 明示比對算法：
build target 使用 `sha256`，一般索引使用 `code_rag_md5_v1`（小檔內容、大檔 size+mtime）。
舊 trace 的 32／64 hex hash 仍可辨識；未知算法保留 `matches_index=null` 並附錯誤。
（`matches_index=false` 代表索引之後檔案改過，
快照不是搜尋時那一版）——重灌 KB 或改了程式碼之後，舊紀錄的 chunk id 與路徑 / 行號仍對得回
當時的文字。來源檔一律從 `/` 逐層 `O_NOFOLLOW` 開到 root 再往下讀（root 的 dev/ino 必須是
收集器 init 時那一個，被改名換成 symlink 就 `skipped: root_replaced`），路徑上任何一段是
symlink 就略過（`skipped: symlink`）；所有開檔都先 lstat 再帶 `O_NONBLOCK`，被換成 FIFO 的
快照或來源不會把 server 卡住（`skipped: not_regular`，快照則原子重寫）；快照的每一次開目錄都
與 append 一樣重判「不在被分析的 repo 內」。
現用檔超過 32 MiB 會先歸檔成 `interactions-<UTC 時間>.jsonl` 再寫，歸檔與 append 在收集目錄
`.lock` 的 flock 底下（兩個 server 同時服務同一專案也不會把歸檔蓋掉；一筆不丟；`stats` /
`rate` / `trace` 只看現用檔，舊檔用 `--file` 指定）。收集落點若在被分析的 repo 之內
（`XDG_STATE_HOME` 指進去，含經 symlink），收集器與 session store 一樣拒絕啟動。

與 session 檔同一套 root 雜湊與 `client_paths` 防線：目錄 0700、檔 0600、拒 symlink
與 hard link、dir-fd append。**絕不落進被分析的 repo** —— 以前預設是相對路徑
`data/interactions.jsonl`，而 MCP 以被分析專案為 cwd，所以那些含 question / answer /
程式片段的內容實際上寫在客戶的 repo 裡，還是普通的 `open(..., 'a')`。
讀取端（eval 工具）以明確的路徑參數讀。

記錄內容包含 question、answer、refs、code snippets、mode、KB score、repo commit、model tag、agent tool calls、files read。這些資料在 NDA 場景通常含敏感內容；它們只在本機 state 目錄，不進 repo、不出機器。

MCP server 端只記 KB-shaped tools：

- `query_knowledge`
- `query_knowledge_strict`
- `code_rag_search`

一般 plumbing tools，例如 `read_file`、`grep_code`、`apply_patch`，不會在 MCP 端逐一記完整對話。

常用命令：

```bash
python3 data_flywheel.py where  --root <專案>                      # 印出收集目錄
python3 data_flywheel.py trace  --file <目錄>/interactions.jsonl --last 5   # 攤開最近 5 筆的檢索路徑
python3 data_flywheel.py stats  --file <目錄>/interactions.jsonl
python3 data_flywheel.py rate   --file <目錄>/interactions.jsonl
python3 data_flywheel.py export --file <目錄>/interactions.jsonl --output <輸出>.jsonl
```
<a id="eval"></a>

## 評測

`eval/` 是固定題庫與離線回歸評測，不會記錄使用者對話，也不會被 runtime 自動使用。

主要檔案：

- `eval/run_eval.py`：手動評測 runner，會呼叫模型，適合調 RAG / agent / prompt 後做回歸。
- `eval/run_retrieval_eval.py`：完全離線的 retrieval-only gate；只跑 `_hybrid_search`，
  query/chunk embedding 從 checked-in fixture cache 讀取，cache miss 直接失敗，不呼叫四台 server。
- `eval/retrieval_fixture.json`、`eval/retrieval_embedding_cache.json`：30 個 NDA-safe 合成
  register facts，展開成 92 個可回答題（62 個數值/hex/version）+ 5 個拒答題。
- `eval/spec_questions.json`、`eval/spec_holdout.json`、`eval/spec_adversarial.json`：規格/RAG 題庫。
- `eval/code_questions.json`：程式碼定位題庫。
- `eval/bug_questions.json`：bug 類問題題庫。
- `eval/run_code_smoke_eval.py`：全離線 code inference gate。保留歷史 16 題
  regression floor，另跑 20 題 blocking core(code2test / trace2code / edit2ripple /
  firmware semantics / selective retrieval，各 4 題)與最多 4 題 stretch；目前 fixture
  是 20 + 4(stretch 含 2 題 `comment2context`,那是 **provisional diagnostic
  family**,不是 blocking core)。CI structural lane 使用 parser/lexical/graph 與 HTTP
  poison，固定檢查 `12000` / `28000` 的純 `budget_chars`，不輸出 tokenizer token 單位。
  SHA-256 pseudo embedding 只叫 deterministic plumbing stub，不代表真 semantic 品質；
  `--with-servers` 才是手動 real-model lane，永不成為 CI 必要條件。
- `eval/semantic_retrieval.py` + `eval/fixtures/code_smoke/semantic_vectors.{json,f32}`：
  **real semantic lane**。向量是 checked-in 的 float32 artifact(bge-m3、1024 維、
  cls pooling、L2),document 與 query **兩種都錄**;manifest 記 model / pooling /
  dimension / parser / render / scorer 版本與 corpus digest,不含本機絕對路徑。
  正常執行完全離線,**任何 cache miss、render 不符、corpus digest 漂移或 checksum
  不符一律 fail closed**(non-zero exit),絕不退回合成向量。
  四條 lane:`lexical`(可比較基準)、`dense`(純 cached cosine)、
  `runtime_hybrid`(**主 gate**;直接呼叫 `code_rag.hybrid_symbol_score` 與
  `select_scored_candidates`,跑的是 production 那份 scoring 與 cutoff)、
  `rrf_experimental`(選配診斷,**不得**冒充 production hybrid)。
  scope:`per_repo` 是主 gate lane(對齊 runtime 每次只有一個 sandbox root),
  `union` 只是 cross-repo distractor 的 stress 診斷;12k/28k context gate 走 `per_repo`。
  file metric 先依 `repo_id:path` **聚合去重**再截 k,並對**完整 `gold_files`** 計分;
  `seed_files` 另報 `seed_recall`,不取代主指標。
- `eval/record_semantic_vectors.py`：**唯一**可以碰 loopback llama-server 的入口,
  而且要顯式帶 `--record-vectors`。錄製前對 `/props` 與 effective profile 核對 role /
  pooling / GGUF identity,錄製頭尾各 embed 一次 sentinel,漂移超容差就拒絕寫檔。
  corpus、parser 語意、render schema 任一變更都要重錄(manifest digest 會自己擋)。
- `eval/fixtures/code_smoke/semantic_retrieval_baseline.json`：semantic baseline,
  含 corpus digest、render / scorer / model 版本與 per-family 數字。pipeline 版本或
  corpus digest 一變就**跳過**no-regression 比較並印出原因 —— 不同 corpus 的數字
  本來就不可比,硬比才是假訊號。**只有 blocking family 能擋 gate**,而且成員資格是從
  case 的 `blocking` 欄位推出來的,不寫死清單;`comment2context` /
  `low_lexical_overlap` 這類 provisional diagnostic 照常報數字,但退步不擋 gate ——
  無差別比較等於偷偷把 stretch 升格成 blocking。
- `eval/fixtures/tool_routing/cases.json`：隔離 synthetic root 的 9 個檔案、1 份 KB 文件與
  15 個中英／混合 routing cases；結果只保存分類、aggregate、token/latency/compaction
  計數，不保存 prompt、assistant text、tool args/result、session id 或專案路徑。
- `eval/fixtures/tool_routing/support_matrix.json`：只保存現行客戶端的 `client_baseline`。
  catalog 契約已量，routing 指標尚未完成量測，所以狀態維持 `unsupported`。
  harness 絕不改 matrix；gate 通過只輸出 `manual_status_change_required=true`，
  仍需人工審核後明示改狀態。`--arm` 只選擇／記錄 arm id，
  **不會替操作者切換 tool schema 或客戶端規則**；contract digest 綁定
  `client_prompt` 的規則檔與工具 catalog，不相符就 fail-loud。
- `session_eval.py`／`scripts/session_eval.py`：明示 opt-in 的私人 session-model eval。
  session store 的匯出只負責來源封存；mined draft 排除所有歷史 assistant text，curated suite
  禁止 `expected_answer`／`gold_answer` 類欄位，只接受外部 verifier、`human_pairwise` 或
  `unscored`。原始 prompt／candidate answer 只寫 `.codetrail/session_eval` 的 0700/0600
  私有產物，永不放進 checked-in `eval/`；runner 核對 live GGUF 與 project-state digest，
  並雙層關閉寫入／執行工具。每題原子 checkpoint；resume 必須重驗 suite／模型／現場，
  單題 timeout 記為該題失敗而不丟掉先前結果。完整流程見 [私人 session eval](#session-model-eval)。
  **壓縮語意**：replay 一律以 `replay_client_config()` 跑客戶端,預設壓縮模式 `off`
  ——長案例撞到 context 上限就是該題失敗,不會靜靜壓縮掉一半題目。以壓縮本身為題的
  suite 才用 `--keep-compaction`,它讓 replay 用 `codetrail` 模式(客戶端自己的
  `client_compaction`),而且 `_compaction_identity()` 會把模式、live `n_ctx` 推導出的
  門檻與保留額一起寫進結果 identity——不同模式 / 不同 n_ctx 的結果不可比。
  `scripts/eval_tool_routing.py` 走的是**使用者目前有效的**客戶端設定,語意就是使用者
  目前的模式;`effective_config_digest` 已經涵蓋這一點。
  摘要**品質**（七條規則、五輪權重、舊結論淘汰）只走這條私人 eval，不進 smoke / full——
  用 mock 驗模型輸出品質等於沒驗。
- `scripts/eval_tool_routing.py`：量測現行客戶端的工具路由與 live MCP catalog。
  catalog 必須符合目前公開工具契約。catalog-only 結果的 support gate 明確是
  `passed=false`，不可能靠 catalog aggregate 升級 support status。
  真模型 event parser 把 reasoning token／text 與 assistant text 分開；reasoning 永不參與
  promise／marker 分類，也不會被保存成 assistant output。
- `scripts/check_eval_consistency.py`：不跑 LLM，只檢查 eval expected 是否和 `config.py` / source code 漂移。
- `tests/test_repo_consistency.py`：把 consistency check 接進 pytest。

下列命令是 eval 工具目錄，不是每次改碼的交付 checklist。developer 途中可跑第一條靜態
drift check；其他 runner 只在任務明示要做 eval / benchmark 時使用，pytest 仍依本文開頭與
AGENTS.md 的角色規則執行。

```bash
python3 scripts/check_eval_consistency.py
python3 eval/run_retrieval_eval.py
python3 eval/run_code_smoke_eval.py                      # 全離線 gate
python3 eval/run_code_smoke_eval.py --report-json /tmp/report.json   # A/B 用的完整 summary
python3 eval/run_eval.py --test-set all --verbose

# routing catalog-only：先由維護者外部套用並凍結 <ARM>，CLI 不會代為切 variant
python3 scripts/eval_tool_routing.py --root <SYNTHETIC_ROOT> \
    --matrix-row <ROW_ID> --arm <ARM> \
    --output /tmp/tool-routing-catalog.json --catalog-only

# 只有這兩條會連 8081。改了 corpus / parser 語意 / render schema,或 bump 了
# RETRIEVAL_SCORER_VERSION 之後都要重錄(pipeline 不符時 eval gate 會 FAIL,
# tests/test_evals.py 也會紅)。
# `--llama-bin` 指到真的那支執行檔(預設值來自 deployment.json 的 llama_bin)。
# 指定的檔不存在會直接失敗,**不會**改拿 PATH 上另一顆;deployment.json 那顆在這台
# 主機上不存在時 artifact 的 llama_cpp.revision 會記成 "unknown" 並指名來源,那份
# checked-in fixture 就失去可驗證的 build 出處。
python3 eval/record_semantic_vectors.py --record-vectors \
    --llama-bin ~/llama.cpp/build/bin/llama-server
python3 eval/run_code_smoke_eval.py --record-semantic-baseline
```

前三個命令不需要 llama-server；retrieval runner 固定回報 Recall@5、MRR、nDCG@5 與
數值證據精確率。加 `--predictions <json>` 時才另外計算 citation entailment、數值答案
精確率、拒答率/拒答正確率。`eval/run_eval.py` 才需要本機 4 個 llama-server 與對應 GGUF。

`scripts/eval_tool_routing.py` 不加 `--catalog-only` 才走真模型；這條只可在既有**明示授權**下，
選定 matrix row/arm、isolated synthetic root 與停用其他 MCP server
執行。`--arm` 不做 variant composition；執行前還必須由維護者凍結並核對該 arm 的 exact
config／artifact／contract digest。完整介面是：

```bash
python3 scripts/eval_tool_routing.py --root ROOT --matrix-row ROW_ID --arm ARM \
    --output RESULT.json [--model MODEL] [--catalog-only]
```

`--catalog-source in-process` 是 CI/testing 隱藏選項，而且只允許搭配 `--catalog-only`；日常
量測不得用它取代 effective stdio MCP。主 tools/no-tools token probe 保持相同 minimal
message/model/`max_tokens=1`/stream，只差 tools；任何一側 usage 缺失就整組 fallback。
FastMCP instructions 另以對稱 apply-template/tokenize marginal delta 計入。沒有授權、沒有
live-after 結果或 gate 未通過，都要明列未完成，不能把 `measured` 改寫成 `supported`。

目前 `client_baseline` 尚未完成 routing 量測，`status` 維持 `unsupported`，
`supported_arm=null`。catalog 計數不能當成模型支援宣告。
<a id="session-model-eval"></a>

### 使用真實對話比較主聊天模型

這條 lane 回答的是：「哪顆本地模型比較能完成我的真實工作？」它不把任何歷史
assistant 回答當成標準答案，也不會在 `aicode` 啟動或 CI 中自動執行。

#### 資料與評分原則

- 歷史 session 只提供真實問題分布、使用者補充的證據與弱失敗訊號。
- `expected_answer`、`gold_answer`、`reference_answer` 等欄位在 suite schema 中被明確拒絕。
- 正確性只來自 source/tool evidence、repo state、外部 build/硬體結果或匿名人工 A/B。
- 沒有可靠 oracle 的題必須標 `human_pairwise` 或 `unscored`；「session 結束」不算成功。
- 每顆候選模型跑相同 suite。bundle 前會驗證 suite digest 與逐題 project-state digest
  一致，不同現場的回答不能混成一次 A/B。

所有原始 export、NDA prompt、模型回答與盲測 key 預設放在：

```text
<CODETRAIL_REPO>/.codetrail/session_eval/
```

`.codetrail/` 已被 git ignore。writer 另外把目錄收成 `0700`、檔案收成 `0600`，拒絕
既有 symlink；不要把產物另複製到 repo 內可追蹤的位置。

#### 1. 匯出選定 session

session id 可由 `aicode` 的 `/session` 選單、`codetrail_chat.py sessions` 或
`python3 scripts/session_eval.py` 的輸出取得。匯出是明示動作,只讀你點名的那幾個
session 檔,不會掃整個 session 目錄:

```bash
python3 scripts/session_eval.py export \
  --session <SESSION_ID_1> --session <SESSION_ID_2>
```

原始 export 必然含歷史 assistant 回答，只作來源封存。需要拿去分享時才使用
`--sanitize`；sanitized export 可能失去建立 verifier 所需的 file/tool evidence。

已有一個私人 export 目錄時，可以直接進下一步：

```bash
python3 scripts/session_eval.py mine \
  --source-dir /private/session_exports --glob '*.json'
```

`drafts.json` 只保留 user text。短句糾正（例如只要求「真的用工具」）變成 failure signal；
可以安全去掉直接責備前綴的句子會產生中性 evidence-update 草稿。任何可能依賴舊回答的
指代仍標 `manual_required=true`，不會假裝已自動理解。

#### 2. 人工收斂 episode

curated suite 的必要形狀：

```json
{
  "schema_version": 1,
  "name": "private_real_work_v1",
  "source_policy": "user_and_external_evidence_only",
  "cases": [
    {
      "id": "code_target_core",
      "task_type": "code_qa",
      "project_root": "/absolute/private/project",
      "source": {
        "session_hash": "0123456789abcdef",
        "export_digest": "<64 hex>",
        "user_turn_indices": [0]
      },
      "turns": [{"kind": "prompt", "text": "問題文字"}],
      "read_only": true,
      "state_paths": ["relative/file/used/by/the/case"],
      "verifier": {
        "oracle_kind": "human_pairwise",
        "checks": [
          {"type": "terminal"},
          {"type": "required_any_tool", "values": ["read_file", "grep_code"]},
          {"type": "max_identical_tool_call", "value": 2},
          {"type": "no_tool_error"}
        ],
        "human_dimensions": ["結論正確", "證據充分", "沒有忽略限制"]
      }
    }
  ]
}
```

後續 user turn 必須改寫成不依賴舊 assistant 的 `evidence`／`constraint`。例如
「你前面位址算錯了」不能原樣 replay；應改成「新增證據：host address 的位數與轉換規則為…」。
這個中性更新會依序送給每顆候選模型，用來測 context 保持與修正能力。

先做純驗證（不連模型）：

```bash
python3 scripts/session_eval.py validate \
  --suite .codetrail/session_eval/suite.json
```

#### 3. Verifier

支援的 deterministic checks：

- `terminal`
- `required_tool` / `required_any_tool` / `forbidden_tool`
- `required_text_all` / `required_text_any` / `forbidden_text`
- `max_identical_tool_call`
- `no_tool_error`

文字 marker 必須來自獨立 source／外部 outcome，不可從舊模型答案抄成 gold。複雜 firmware
診斷、信件與摘要通常保留 `human_pairwise`；auto checks 只守格式、工具與明確事實，不冒充
語意正確率。

#### 4. 對候選模型 replay

runner 不會替操作者停／啟 server。先載入候選 GGUF，確認四個 llama-server ready，再跑：

```bash
python3 scripts/session_eval.py run \
  --suite .codetrail/session_eval/suite.json \
  --candidate-label candidate_1 \
  --model <BARE_MODEL> \
  --resume
```

每次 run 都會：

1. 從 `/props` 核對目前載入的 GGUF 路徑與指定 candidate；不一致直接拒絕。
2. 使用相同 suite、tools、n_ctx 與 production sampling。壓縮語意由 eval **自己**釘死
   (臨時 `client.json`:預設 `off`,`--keep-compaction` 才是 `codetrail`),不讀你的
   `~/.config/codetrail/client.json` —— 否則同一份 suite 在兩台機器上量到的不是同一件事。
3. 客戶端走 `--policy readonly`(判準是 `readOnlyHint`,不是寫死名單),MCP server 端另設
   `mcp_server --readonly`(一個 argv 旗標,同時關掉寫入、執行、build 命令與 context metrics)。
4. 每題前後比對 Git 狀態、diff 與 `state_paths` 內容 digest;`.codetrail/`、
   `knowledge.json`、`.aicode_uploads/` 這三個被 gitignore 的路徑**一律**納入(它們正是
   唯讀 replay 最可能被寫到的地方)。read-only replay 改到現場即失敗。
5. 只保存 assistant text、completed tool 名稱／參數 digest、token、latency 與 verifier 結果；
   tool output 不落盤。單輪 case 完全不落 session 檔;多輪 case 必須落檔(模型要看得到上一
   輪),跑完就刪除。
6. 每完成一題就原子覆寫 `result-<candidate>.json` checkpoint；`--resume` 會重新核對
   suite digest、live model fingerprint、case 順序與每個已完成 case 的 project-state digest。
   任一項不同就拒絕續跑，不能把隔天改過的 tree 混進同一候選結果。
7. 單題超時是該模型在固定 SLA 下的失敗，不是整包消失：runner 解析有界的 partial JSON
   stream、標記 `timed_out=true`／`harness_error=true`、清掉暫存 session,checkpoint 後繼續
   下一題。private stderr 與 partial tool output 都不寫入結果。

第一次使用某個 `candidate-label` 時可省略 `--resume`；若同名結果已存在，runner 會拒絕
覆寫。續跑必須明示 `--resume`。兩個候選應使用相同的 `--turn-timeout`，timeout 不可因
看到某顆模型較慢才臨時放寬。

兩顆模型都跑完後建立盲測包：

```bash
python3 scripts/session_eval.py bundle \
  --suite .codetrail/session_eval/suite.json \
  --left .codetrail/session_eval/runs/result-candidate_1.json \
  --right .codetrail/session_eval/runs/result-candidate_2.json
```

使用者填 `review/review.json` 的 `choice`：`a`、`b`、`tie` 或 `both_bad`。閱讀用的
`review.md` 不含模型名；`review-key.json` 是 sealed mapping，完成評分前不要開。

#### 邊界

- 這個 runner 只支援 read-only replay；code-change 題要另做隔離 snapshot executor，不能拿
  真專案當沙盒。
- 不自動把 LLM-as-judge 分數當 promotion gate。
- 不在同一個正式 session 中途混用模型。需要救援比較時 fork 成兩個分支，分開歸因。
- `--skip-aux-preflight` 只適用保證不會用 Code-RAG／RAG／VL 的 suite；正常真實題不要跳。
<a id="troubleshooting"></a>

## 故障排解

先保留確切錯誤與目前 profile，再依下表分流；包含來源文字的 log、session 和工具輸出不貼到公開 issue。

| 症狀 | 入口 |
|---|---|
| CUDA／build／啟動 rollback | [CUDA 與 loader](#cuda-build) |
| 服務還在但首字很慢 | [prefill、reasoning 與快取](#first-token-latency) |
| `/tools` 有工具但沒有實際呼叫 | [模型工具呼叫](#mcp-connected-but-no-tool-call) |
| 已選取但本機貼不上 | [SSH／tmux 剪貼簿](#clipboard-ssh-tmux) |
| MCP 中途失效 | [lease／incident](#mcp-incidents) |
| context／摘要拒絕 | [壓縮](#compaction-errors) |
| MCP 初始化或 KB 載入錯誤 | [MCP／KB](#mcp-kb-errors) |
| 圖片、PDF 入庫失敗 | [PDF／VL](#pdf-errors) |
| patch／command 拒絕 | [修改工具](#patch-errors) |

<a id="cuda-build"></a>

### CUDA、llama.cpp build 與啟動 rollback

`compute_120a` 不支援通常表示 Toolkit 不認得 GPU 架構；Blackwell 需 12.8 以上。
`nvidia-smi` 的 CUDA Version 是 driver 支援上限，`nvcc --version` 才是已安裝 Toolkit。
依 [NVIDIA 安裝文件](https://docs.nvidia.com/cuda/cuda-installation-guide-linux/) 選對 OS／版本；
只安裝所需 Toolkit，避免無意替換現用 driver。以 Ubuntu 24.04、CUDA 13.0 為例：

```bash
cd /tmp
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt update
sudo apt install -y cuda-toolkit-13-0
export PATH=/usr/local/cuda-13.0/bin:$PATH
export LD_LIBRARY_PATH=/usr/local/cuda-13.0/lib64:$LD_LIBRARY_PATH
nvcc --version
```

將相同 PATH／library 路徑寫入自己的 shell 設定。其他 OS 用對應 repo 或 runfile，
runfile 只選 Toolkit。Ubuntu 20.04 不能照抄 13.0 步驟，改用仍支援該 OS 的版本或升級 OS。
`ptxas ... sm_52`／新舊 nvcc 混用時先檢查 `command -v nvcc`，CMake 可明示 compiler：

```bash
cd ~/llama.cpp
cmake -B build -DGGML_CUDA=ON -DLLAMA_CURL=OFF \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda-13.0/bin/nvcc
cmake --build build --config Release -j
```

切換 Toolkit 後若 CMake 還記著舊路徑，移除確認屬於此 checkout 的 build cache 再 configure。
確認輸出的 CUDAToolkit 路徑、版本與 compiler 一致。

啟動秒退／rollback 的前台只提供摘要；真正原因在 role log，先看第一個明確錯誤：

```bash
tail -n 120 ~/.local/state/codetrail/logs/main.log
python3 scripts/launch_servers.py --scope all --dry-run
~/llama.cpp/build/bin/llama-server --version
```

`unknown model architecture` 通常需要支援該 GGUF 的 llama.cpp build；不應修改 GGUF metadata
偽裝架構。更新前先確認 llama.cpp 工作樹的自己的修改，再更新與 build：

```bash
git -C ~/llama.cpp status --short
git -C ~/llama.cpp pull --ff-only
cmake -S ~/llama.cpp -B ~/llama.cpp/build -DGGML_CUDA=ON -DLLAMA_CURL=OFF
cmake --build ~/llama.cpp/build --config Release -j
~/start.sh
```

rollback 只清理本次啟動，log 仍保存。需要保留失敗現場可用內部
`python3 scripts/launch_servers.py --scope all --keep-on-failure`。
<a id="first-token-latency"></a>

### 首字延遲、prefill、reasoning 與 cache

先看工作階段：prefill 是處理輸入，reasoning 是模型正在生成推理，工具則可能在執行 MCP。
狀態列的 SSE `prompt_progress` 是本次快照，UI 每 10 秒更新不代表 server 每 10 秒必定推送。
缺計數只顯示階段，不應由等待時間猜剩餘 token。完整顯示規則見[使用指南](docs/usage.md#chat)。

```bash
curl -s http://localhost:8080/slots | python3 -m json.tool
curl -s http://localhost:8080/props | python3 -m json.tool
nvidia-smi
```

MoE 加 CPU offload 但使用 mmap 時，首次請求可能從磁碟 page-in 大量 experts。
RAM 足夠才考慮在該 role `parameters` 加 `"no_mmap": true`，validate 後重啟；
這會增加啟動時間與 RAM 占用，set_config 不替你決定，但會保留手動設定。
先前量到的秒數不能當其他模型／硬體的效能保證。RAM 壓力要一起看 `free -g` 與 process RSS。

主聊天 `/think` 預設 off；on 可能延長首字，且會跳過 prefix 預熱。
預熱只把 prefill 移到輸入問題的時間，不會加速硬體。`/status` 可看最近 prime outcome，
不能將 no_timings／incomplete 當成已送。判 cache 熱度比較完整輸入與重算量，
`timings.prompt_n` 不是完整 prompt token。換 system、工具 catalog、session 或規則也會改 prefix。

不要為了首字慢擅改 SSE buffering；現行 chunked SSE 每個 HTTP chunk 即可返回。
延遲與取消的技術契約見 [Context](#context-budget)。
<a id="mcp-connected-but-no-tool-call"></a>

### 工具列得出，但模型沒有真實呼叫

**典型症狀**:

- `/tools` 明明列得出 21 個工具,模型卻回答「沒有 CodeTrail 工具」。
- 明確要求 `list_dir(path=".", depth=1)` 後,模型只輸出 `<list_dir path="." depth="1"/>`,接著用自然語言宣稱「已成功取得目錄」,畫面上沒有 `· list_dir → completed`、也沒有真實目錄內容。
- 模型每輪都回答「我現在呼叫」「讓我直接使用工具」，但訊息隨即結束；使用者催促後只換句話重複，始終沒有工具卡。

**先講結論:模型對「自己有哪些工具」的文字回答不是診斷資料,XML 長得像 tool call 也不代表執行過。** 要把三層狀態分開看:

| 層次 | 能證明什麼 | 怎麼驗證 |
|---|---|---|
| MCP 連線 | 客戶端已啟動 CodeTrail 子行程並完成 initialize | 客戶端的 `/tools` |
| 工具註冊 | client 收到 CodeTrail 的工具 schema | 客戶端的 `/tools`;完整名稱見 [MCP 工具清單](docs/mcp-tools.md) |
| 本輪實際執行 | 模型真的發出結構化 tool call,client 執行後把結果送回模型 | TUI 的 `· <工具> → completed`,或 JSON event 的 `type: "tool_use"`、`part.state.status: "completed"` |

`aicode` 把 transport、explicit hard gate 與 implicit diagnostic 分開，不需要
每次先叫模型背 21 個名字：

- `MCP PASS — 21 tools + list_dir round-trip`：每次啟動都另起一個 MCP server 子行程，完成
  `initialize`、依固定順序精確比對 21 個名稱，擷取完整 typed schemas／instructions digest，
  再執行無副作用的 `list_dir(path=".", depth=1)`。這層完全不問 LLM；schema 的
  bounds/description/budget 由 static contract 驗證，routing catalog 另保存逐工具
  counts/digests 與 token measurement。
- `MODEL live canary — <原因>`／`MODEL PASS — ...`：explicit prompt **點名**
  `list_dir`，只接受 JSON stream 裡 completed 的結構化 `tool_use`。純文字／XML
  和模型自行宣稱成功都不算。首次失敗 retry 一次；第二次才成功印 `MODEL FLAKY`、本次
  放行但不快取；連續兩次失敗 exit 2。
  逾時會另列「時限內未完成驗證」：MCP PASS 仍有效，但模型忙碌、單 slot 排程與處理速度
  尚未排除，不能直接歸因工具契約損壞。explicit 必須真正通過才能啟動，沒有逾時放行。
- `IMPLICIT live diagnostic ...` 之後只會是 `status=optimal|suboptimal|fail|timeout`。
  這一輪 prompt 不含工具名，只跑一次：exact completed root `list_dir` 是 `optimal`；選到
  其他 allowlisted CodeTrail 唯讀工具是 `suboptimal`；沒有合格 call 是 `fail`；超時是
  `timeout`。後三態會印 `IMPLICIT WARN`，但**四態都不擋啟動**。

explicit 與 implicit 是兩條獨立 cache lane，不會互借另一列結果。fingerprint
包含 selected/runtime identity、客戶端檔案(`client_engine` / `client_prompt` /
`codetrail_chat`)與 system prompt 的 digest、
live tools/instructions、專案 AGENTS、lessons 與 server `/props`（含 chat template、
capabilities、build/取樣資訊）。cache 只存 hash、lane status、檢查時間與版本，不存 prompt、
模型輸出、tool args/result、檔名、目錄內容、session id 或專案路徑；抽查用的對話
也會刪除。`/props` 明確回 `chat_template_caps.supports_tools=false` 時，在任何 model attempt
前直接 fail-loud；缺欄位才繼續探針。

這是啟動抽查，不是「往後每個生成 token 都保證正確」。如果 TUI 裡稍後又碰到偶發失手，
可直接退出後強制重測兩條 model lane：

```bash
rm -f ~/.cache/codetrail/tool-call-canary.v3.json   # 或
python3 <CODETRAIL_REPO>/scripts/tool_call_canary.py --root "$PWD" --force
```

**沒有略過用的環境變數。** 以前有三個(`SKIP` / `FORCE` / `WARN_ONLY`)與一個位置
覆寫(`CACHE`),全部刪除、無替代:要跳過某個檢查 = 修那個檢查;要強制重測 = 刪那個
快取檔或 `--force`。時限與快取期是 `config.py` 的常數(`TOOL_CANARY_*`,預設 TTL
86400 秒),改它是改 repo,所有使用者一致。FAIL 訊息會刻意區分 MCP/catalog、
explicit model/chat-template 與 non-blocking implicit routing，避免再把它們混為一談。

`python3 scripts/doctor.py --project "$PWD"` 會重建 current fingerprint，只回報該列
的 implicit `optimal/suboptimal/fail/timeout` 與 fresh/stale；資料不足顯示 `unknown`，不會拿
另一個模型／設定／專案的 cache row 冒充現況。
<a id="clipboard-ssh-tmux"></a>

### SSH／tmux：已選取但本機貼不上

用滑鼠左鍵拖選，放開就會送出複製請求，不需按鍵。需要再次送出時，也可按手動複製鍵
（預設 **F2**，`/copykey` 可更換）；閒置主畫面另可按 **Ctrl+C**。回合、核准或審查進行中，
Ctrl+C 仍是中斷。完整操作見[選取、複製與中斷](docs/usage.md#copy)。

CodeTrail 透過 Textual 向終端送出 OSC 52 複製請求，SSH 另一端的終端決定是否寫入
本機剪貼簿，請以實際貼上結果確認。請在你用來連線的終端
設定中確認 OSC 52／應用程式寫入剪貼簿的支援與權限。這條路徑不需要在遠端安裝
`xclip`、`pbcopy`，也不需要 X11 forwarding。[Textual 複製介面](https://textual.textualize.io/api/app/#textual.app.App.copy_to_clipboard)
有終端相容性限制；請以下面的實際貼上結果確認。

**先驗證直接 SSH。** 開一個未進 tmux 的 SSH shell，在 TUI 外執行固定無敏感內容的
probe。每次 probe 前先在本機複製另一段文字（例如 `before-probe`），避免把上一次
殘留的內容誤認成這次成功；執行後在本機文字編輯器貼上，應得到 `codetrail-clipboard-check`：

```bash
printf '\033]52;c;Y29kZXRyYWlsLWNsaXBib2FyZC1jaGVjaw==\a'
```

如果直接 SSH 就貼不上，先處理客戶端終端的支援／權限。只有把 `aicode` 跑在 tmux
裡才需要下面的設定。進 tmux 後，先在本機複製 `before-probe`，再於 tmux shell 執行
同一個 probe，最後回本機貼上比對。外面成功、裡面失敗時，在該 tmux session 檢查：

```bash
tmux show -s set-clipboard
tmux info | rg 'Ms:'
```

`set-clipboard` 必須是 `on`，`external` 會阻擋 tmux 內部應用程式的複製請求。
可在遠端 `~/.tmux.conf` 加入以下設定；目前的 tmux server 可用
`tmux set -s set-clipboard on` 套用同一選項：

```tmux
set -s set-clipboard on
```

`Ms` 應顯示 escape sequence。若為 `[missing]`，先回到 **tmux 外**的 SSH shell
查看實際終端名稱：

```bash
printf '%s\n' "$TERM"
```

只有在直接 SSH 的 probe 已確認該終端支援 OSC 52 時，才於 `~/.tmux.conf` 補上
下列 tmux 3.2+ 設定，將 `OUTER_TERM` 換成剛查到的完整名稱；不要用 tmux 內的
`TERM`，也不要用 `*` 宣告所有終端都支援：

```tmux
set -as terminal-features ',OUTER_TERM:clipboard'
```

這項能力設定請在既有對話／服務可結束、重新啟動 tmux server 後，再檢查 `Ms` 並
重做內部 probe。舊版或巢狀 tmux 的設定見 [tmux 官方剪貼簿文件](https://github.com/tmux/tmux/wiki/Clipboard)。

終端不支援或不允許 OSC 52 時，可改用該終端的原生滑鼠選取與複製功能。TUI 會接收
滑鼠事件，終端可能要求按住修飾鍵才啟用原生選取；修飾鍵及複製快捷鍵依終端設定，
沒有通用的一組按法。
<a id="compaction-errors"></a>

### 壓縮、context 與待完成問題

先確認你選了哪個壓縮模式：TUI 的 `/status` 保留 `[aicode] 壓縮模式=...` 啟動診斷，
`python3 scripts/doctor.py` 的 `-- 壓縮模式 --` 一段則會再印出有效設定跟它一不一致。
三種模式的完整說明、門檻公式與取捨在 [使用指南](docs/compaction-rules.md)。

**`codetrail` / `manual` 還在測試階段**(兩處顯示都會標 🧪):行為與受管值可能再變。
遇到下表以外的怪狀況，先執行 `scripts/configure-advanced.sh` 選 1，在壓縮模式問答選擇 `off` 再回報
——`off` 不會動你的對話,只是不再自動摘要;context 滿了會是一個可見的錯誤。

| 畫面 | 意思 | 怎麼辦 |
|---|---|---|
| 接續舊對話後先跑摘要，尚未開始預熱 | `codetrail` 模式量到既有歷史已超過門檻，先壓縮再預熱 | 等壓縮完成，或 Ctrl-C 中止；原始畫面紀錄保留，manual / off 不會自動做這一步 |
| 畫面說「壓縮產生了空摘要」或只有 thinking | 實際收到的摘要沒有可信正文，摘要**沒有落地**，原始歷史保留 | 這個對話的自動壓縮已停用且跨行程保留；開新對話，帶入必要問題與狀態 |
| 畫面說「壓縮沒有生效」，並指出計數或模型服務錯誤 | 計數端點、HTTP 或摘要請求失敗；沒有可驗證的摘要產出，不能據此判定摘要永久不可信 | 原始歷史與壓縮狀態保留；修復模型服務後可 `/compact` 重試，自動壓縮未永久停用 |
| 畫面說「壓縮請求出錯」且自動壓縮已停用 | 舊版本留下的 `summary_error` durable 紀錄仍受尊重 | 開新對話帶入必要狀態；升級不會清除舊停用檔 |
| 畫面說「摘要沒有照七欄格式輸出」 | 模型照了別套欄位(實測看過整份換成英文五欄)。「已確定事實 vs 未確認」的分離沒了 | 摘要沒有落地,對話可以繼續;要繼續用結構化壓縮就開新對話,同一個模型一直不遵守就改 `off` |
| 過 gate 後說「歷史已壓縮；這次問題尚未完成」 | 只摘要更早的已完成內容，最後問題及其後訊息逐字保留；工具迴圈沒有自動重跑 | 查看這次已完成的工具結果，再重送問題；不會把未回答的問題標成已答 |
| 壓縮仍說單一回合或尾段超過容量 | 分批摘要也不能安全放下不可分割回合，或「摘要 + 原始 tail」仍過 gate | 原始歷史不變且可重試；可用更大的 live n_ctx 接續，或開新對話帶入必要內容 |
| 壓縮完緊接著又壓一次 | `tail_turns = 1` 讓最新一輪逐字留著;那一輪很長時,壓完的 context 是「摘要 + 長 tail」 | 正常,不是迴圈(同一則助理訊息不會被當第二次的錨點)。把超長單輪拆小或把 `n_ctx` 調大 |
| 畫面說「這個 n_ctx 推不出可用的壓縮門檻」 | ctx 太小,公式算出來的 threshold / tail_cap 低於下限 | 把 `n_ctx` 調大重跑 `./set_config.sh`,或把模式切成 `off` |
| `/compact` 沒有作用 | headless `run` 沒有互動指令 | 在 `aicode` 裡面下,不要用 `codetrail_chat.py run` |

過大的摘要會按完整 user-turn 分批，每批仍過精確 gate、驗證七欄格式；所有批次
與 replacement 容量檢查完成後才一次寫入新模型歷史，中途取消或失敗不會留下
半份摘要。未回答的單輪或沒有更早可信 head 時仍不能恢復壓縮。原始逐字紀錄
保留，摘要不會重新帶回已剪掉的舊工具輸出。

`codetrail_chat.py run`(headless)只輸出事件流,上面的提示不會出現。停用會留一筆
`compaction_stopped` 的零內容 incident(與 MCP incident 同一個檔)——
`python3 scripts/doctor.py` 會統計並印出最近幾筆的 `kind/detail`。

**壓縮設定改了要重開客戶端才生效**；`/allow` 與 `/copykey` 的局部即時更新不包含壓縮模式。
<a id="mcp-incidents"></a>

### MCP lease 與 incident

啟動抽查只證明「那一刻是好的」。session 開了幾小時之後才失手時,要分的是三種
完全不同的原因,而它們在畫面上長得一模一樣:

| 現象 | 真正發生的事 | 看哪裡 |
|---|---|---|
| MCP server 已經不在了 | 子行程被 SIGKILL / OOM / client 收掉 | lease 是 `stale` |
| server 活著,但模型沒發 structured call | 模型只用文字宣稱「我呼叫了工具」 | lease 是 `live`,`incident kind=promise_without_call` |
| 呼叫發出去了但 client 端失敗 | 子行程沒回應、逾時被取消 | `incident kind=client_mcp_failed` / `structured_call_failed` |

每個 MCP server 行程啟動時會在 `~/.local/state/codetrail/mcp/<boot_id>.json`
(遵守 `XDG_STATE_HOME`)開一份自己的 **lease**。一份行程一個檔,不是共用一個心跳檔
——canary、`aicode`、headless `run` 各起一個 MCP 子行程,共用一個檔只會互相覆寫。
lease 裡只有 pid / ppid / 開始與更新時間 / `tools/list` 次數 / 最後一個工具名與狀態,
**沒有**工具參數、結果、檔名或路徑,權限 0600。

`python3 scripts/doctor.py` 的 `-- MCP lease / incidents --`
那一段會把它們攤開,四種狀態的意思是:

- `live` —— pid 還在,而且該行程的啟動時刻(`/proc/<pid>/stat` 第 22 欄)與 lease
  開檔當下記下的那一格**逐值相同**。這是精確身分比對,不是時間窗:pid 會被重用,
  重用出來的行程本來就落在任何合理的時間窗裡面。
- `exited` —— server 自己正常收尾寫下的退出時間與原因。**只有正常關閉才會有**。
- `stale` —— lease 停在最後一次寫入、pid 已經不在。SIGKILL / OOM 會長這樣,
  client 正常收掉子行程也會長這樣,所以單獨出現不代表故障。
- `unknown` —— pid 還在但啟動時刻對不上(pid 被重用)、這台機器讀不到行程資訊,
  或那是一份還沒有身分欄位的舊 lease。這裡刻意**不猜**:寧可說不知道,
  也不要宣稱一個早就死掉的 server 還活著。

壓縮的停用紀錄是**另一個**檔:`~/.local/state/codetrail/compaction-stopped.jsonl`
(0600,超過 256 KiB 轉存 `.1`)。每行只有 `schema`、時間、session 雜湊與固定 slug,
用來讓「已對這個 session 停用」跨行程重開仍然有效(檔案與目錄都拒 symlink 與 hard link)。

incident 由客戶端與 MCP server 寫在 `~/.local/state/codetrail/incidents.jsonl`
(0600,超過 1 MiB 轉存 `incidents.jsonl.1`,只留一份)。每行只有時間、`kind`、
固定 slug 的 `detail`、來源,以及 **session id 的 sha256 前 16 碼**——沒有原始
session id、沒有訊息內容、沒有路徑。doctor 印的「共 N 筆」與「最近 7 天 N 筆」
都是掃完兩個檔算出來的完整數字,不是尾巴取樣;最近 7 天有紀錄時印 WARN。
要清除診斷只處理 `incidents.jsonl` 與其 `.1`；不要刪除前述 durable 壓縮停用 ledger。

真的碰上時的處置順序:lease 是 `stale` → 重開 session(server 已經不在,重試沒有用);
lease 是 `live` 而 incident 是 `promise_without_call` → 退出後
刪掉 `~/.cache/codetrail/tool-call-canary.v3.json`(或跑 `python3 <CODETRAIL_REPO>/scripts/tool_call_canary.py --root "$PWD" --force`)強制重測兩條 model lane。
<a id="tool-convergence"></a>

### 工具反覆查同一內容

看到 `⚠ [重複呼叫偵測]` 只代表 MCP server 在結果前加了提醒，**不代表客戶端已停止這一輪**。
客戶端會另行判斷同一回合的唯讀查詢是否還有新證據。以下次數以每步一個呼叫為例：

| 查詢情況 | 何時整理答案 |
|---|---|
| 同工具、同參數、同結果，整批沒有新證據 | 兩次相同觀察後 |
| `grep_code` 只換 pattern，path/include/context 相同，完整結果的命中數、檔名／行號與內容都相同 | 三次相同觀察後 |

不同的無命中查詢、截斷的 grep 結果不會因 pattern 近似就被合併。同批只要有新證據或狀態改變，
便可繼續查證。判定停滯後，客戶端只再請模型整理一次答案（`tool_choice=none`），不再執行新工具。

預設單輪工具呼叫額度是 **64 次**，包含參數非法或遭拒的呼叫；單輪模型步數上限是 **24 步**。
用過工具後會在步數額度內預留最後一步整理答案，呼叫額度用完也會進入同一流程；整理本身計入
原上限。程式呼叫若把步數上限設為 1，仍維持用完即停止，不額外補一步。

工具每次仍實際查詢，客戶端不會把本輪「查無結果」快取到下一輪。寫入／執行工具一旦實際送出，
會重新開始比對，即使回報失敗也一樣，因為可能已部分改動；遭拒或參數非法而未執行則不會重置。
回合進行中仍可用 **Ctrl-C** 中斷整輪。

整理時會要求模型附具體證據及來源、分開已證實／推測／未知，並給一個可執行的下一步。
這個保護限制循環，回答品質仍需核對來源；沒有讀到兩個 firmware 的實際內容，就不能宣稱已找到
根因。收斂失敗會以 `[已停止]` 明確回報 error，不把停止提示當成已完成的答案。
<a id="result-budget"></a>

### partial、next 與 context_risk

所有工具結果的 compact text lane 第一行固定是 `status: ok|partial|error`。`partial` 不是
工具失敗，通常代表 core result 自己標示 truncated、repeat guard 提醒重複呼叫，或整份文字
超過 context result budget。第二行 `next:` 是續讀／縮小範圍的動作：`read_file` 請直接用
它給的下一個 `start_line`，`grep_code` 縮小 path/include/pattern，`list_dir` 縮小 path/depth。

省略 `max_chars` 時，budget 依**呼叫當下**主模型 `n_ctx` 的 12% token proxy 計算，不再固定
為 12,000 字元；結果仍受工具 safety cap。明示 `max_chars` 大於該 12% 預設時，回傳會加
`context_risk: explicit max_chars exceeds the default 12% context budget`，提醒這次可能擠壓
後續對話；不是靜默 clamp，也不是 server error。若看到 `[result truncated by context budget]`，
照 `next:` 分頁／縮窄，不要用同參數連續重送。`code_rag_search` 的 `used_chars` 仍只計
evidence text 字元，不是 tokenizer token。

**「剛 ingest 文件就失憶」不等於整份 RAG 塞爆 context。** `ingest_document` 把全文切 chunk 後寫進 `knowledge.json`,它送回目前對話的只有有長度上限的執行摘要;`reload_knowledge_base` 只更新 MCP process 內的 KB singleton。只有之後呼叫 `query_knowledge` 時,召回的少量 REF 才會以 tool result 進入那個 session。新 session 不會因為 KB 裡文件變多就自動攜帶全文。同一個舊 session 累積很多 tool result 時仍可能變長,但要看實際 token / compaction,不能只看 ingest 發生過就下結論。
<a id="model-grounding"></a>

### 模型編造具體事實：補來源與核對引用

模型可能產生未出現在來源的條號、日期、金額、API 或檔案名；降低溫度與換模型都不能證明
這些內容是真的。先提供或匯入原始來源，再要求工具查證並逐項附 file:line／REF。
規格數字用 strict；無來源時明確標未知，推測另列。

客戶端每次請求已明示 `temperature`／`top_p`／`top_k`／`min_p`，來源為 repo 設定與
EngineOptions；server defaults 只適用沒有明示值的請求。不要因改了 deployment 的
sampling 就以為客戶端所有請求都變了。用 `/props` 看 server 實際預設，改 server 參數需重啟。

自訂規則寫在自己的 `~/.config/codetrail/instructions.md`，保持短小且避免重抄工具清單。
專案 `AGENTS.md` 是另一個來源；不信任專案時關閉 `project_instructions`。回答可信度仍以
實際工具結果與獨立來源核對，不能用模型自述、純文字 XML 或「降溫後看起來穩」當證據。
<a id="mcp-kb-errors"></a>

### MCP 初始化、依賴與 KB 身分錯誤

`aicode` 的 preflight 會直接起一次 MCP server;它在完成 `initialize` 前退出時,畫面上只會
說連不上,真正的原因在子行程的 stderr(**預設不落檔**——它含查詢原文與絕對路徑,只在記憶體
留有上限的尾端)。要看到完整輸出,在同一個 target project 目錄手動跑一次同一條命令:

```bash
cd <PROJECT_TO_ANALYZE>
python3 <CODETRAIL_REPO>/mcp_server.py --root "$PWD"
```

(root 走 **argv**,不走環境變數 —— 客戶端也是這樣叫它的。主模型由 server 自己從
`deployment.json` 解析。如果 CodeTrail 依賴裝在 venv,先 activate 再跑——客戶端是用
**啟動它的那顆 Python** 去 spawn server 的。)

若看到 `[MCP] server ready, listening on stdio.`,server 本身正常,按 Ctrl-C 結束。
若它退出,最後一段 stderr 才是根因。常見分流:

- `[MCP][model-preflight] FAIL` → 對應的 embedding / reranker / VL server 沒 ready;
  先跑 `python3 <CODETRAIL_REPO>/scripts/required_model_servers_check.py`。
- `ModuleNotFoundError` → 你用來啟動 `aicode` 的那顆 Python 缺依賴;用**同一顆
  Python** 安裝 `requirements.txt`,或重跑 `set_config.sh`。
- `[FATAL] sandbox root ...` → 必須從具體 project 目錄走 `aicode`,不可把 `/` 或 `$HOME`
  當 sandbox root。
- `KnowledgeStoreError` → 既有 `knowledge.json` 與程式自管的 embeddings cache 不相容
  或不完整;依下一段處理。

KB 的 cache 身分錯誤不會降級或沿用舊向量，重建不了就停止查詢。
日常備份與 fresh 語意見 [KB 維護](docs/usage.md#knowledge-maintenance)。

#### `KnowledgeStoreError: ... embedding model mismatch`

這表示知識庫向量是由另一個 embedding model id 建立,例如錯誤會列出
`saved=<OLD_EMBED_MODEL>, configured=<CURRENT_EMBED_MODEL>`。CodeTrail 會 fail-loud,
避免拿不同模型的向量混算;因為失敗發生在 MCP initialize 前,畫面上只看得到「連不上」,
真正的訊息在 server stderr(見上一節怎麼手動跑一次看它)。

不要只手改 `knowledge.json` 的 `metadata.embedding_model`:那個欄位是 KB 對「自己是
哪個模型建的」的宣告,硬改標籤不能證明 chunk 切法與向量相容。先退出客戶端,把舊
store **保留到不會被 commit 的備份目錄**:

```bash
cd <PROJECT_TO_ANALYZE>
mkdir -p .codetrail/kb-backup-<TIMESTAMP>
mv knowledge.json .codetrail/kb-backup-<TIMESTAMP>/
```

只要搬走 `knowledge.json` 就夠了 —— embeddings cache 是可重建資料,下一次載入時會
自己被清掉；`.rag_embedding_cache.json`（文字→向量的增量快取）同理,留著只會讓之後
的重建更快。

如果暫時不需要文件 RAG,此時重跑 `aicode` 即可;沒有 `knowledge.json` 只代表空知識庫,
不會阻止 MCP 連線。如果仍要查原本文件,先從備份列出來源,再用**目前設定的同一顆
embedding model** 全量重建:

```bash
jq -r '.metadata.documents[]?' \
  .codetrail/kb-backup-<TIMESTAMP>/knowledge.json

# PDF / Markdown / text / binary / ELF
python3 <CODETRAIL_REPO>/RAG.py <SOURCE_FILE> knowledge.json

# 技術圖片；聊天截圖則改用 --chat
python3 <CODETRAIL_REPO>/RAG.py <SOURCE_IMAGE> knowledge.json --image -y
```

每個舊來源都重建完成後再啟動 `aicode`,用 `/status` 確認 `codetrail Connected`。
`knowledge.json`、`.codetrail/`（含 embeddings cache 與 figure artifacts）、embedding
cache 與備份都可能含 NDA 衍生資料,不可 commit。
<a id="ctx-safety"></a>

### Live n_ctx 與啟動容量

TUI 與 headless 回合都必須從主 server `/props` 取得正整數 n_ctx，不能以設定估值代替。
同一 live 值交給 Engine 與 MCP；`requested > server n_ctx` 會拒絕，避免前端截斷 prompt。
缺少 `/props` 或 chat `input_tokens` 計數端點先修服務／升級 server，沒有跳過開關。

更改容量走設定並重啟：

```bash
cd <CODETRAIL_REPO>
./set_config.sh
~/start.sh stop
~/start.sh
```

設定成功不等於實機能容納 weights、KV cache 與 compute buffer，仍需查看啟動 log 與 VRAM。
<a id="pdf-errors"></a>

### VL、PDF 預算、抽取失敗與 strict 排除

每一次 MCP 呼叫的 read timeout 是**固定的** `config.MCP_CALL_TIMEOUT_SECONDS`(660 秒,
略高於 `ingest_document` 的 10 分鐘內部上限),呼叫端不得放寬也不得調小 —— 調小就等於
ingest 還在寫 `knowledge.json` 時客戶端已經放棄。

超時的處理是完整的取消契約:客戶端送 `notifications/cancelled`、等一個寬限期,還不收手就
`SIGTERM` 該 instance、把**所有**進行中的呼叫回成 error,再重新 spawn 一個。若之後連 `list_dir` 也失敗，需核對 MCP instance 是否依契約重建，再查下層服務。

真的看到連鎖失敗時,先確認是不是 VL server 本身沒 ready:

若 timeout 已正確，但圖片回答像是在描述一張不存在的通用終端畫面，跑：

```bash
python3 scripts/required_model_servers_check.py
```

新版 CodeTrail 走 llama.cpp 的 `/v1/chat/completions` `image_url` 多模態格式；舊版
top-level `image_data` 可能被新版 llama.cpp 靜默忽略，造成模型只看提示詞猜圖。

### PDF ingest 說 preflight 超過上限

`ingest_document(path, preflight_only=True)`(或 `python3 RAG.py <pdf> knowledge.json --preflight`)
報告超出上限時,**還沒有呼叫任何 VL、沒有算 embedding、沒有動 `knowledge.json`** —— 零寫入,
不需要善後。報告會指出是哪一項超出:

| 報告欄位 | 上限常數(`config.py`;沒有環境變數可以覆寫) | 預設 |
|---|---|---|
| `candidates` | `FIGURE_MAX_CANDIDATES_PER_DOC` / `FIGURE_MAX_CANDIDATES_PER_PAGE` | 200 / 12 |
| `tiles` | `FIGURE_MAX_TILES_PER_CANDIDATE` | 8 |
| `vl_calls_max` | `FIGURE_MAX_VL_CALLS_PER_DOC` | 200 |
| `image_tokens_est` | `FIGURE_MAX_IMAGE_TOKENS_PER_DOC` / `FIGURE_MAX_IMAGE_TOKENS_PER_CALL` | 400000 / 4096 |

> 這些欄位涵蓋所有結構化候選，包含純 raster 的分類、雙樣本抽取與 image-token 估算。
> 沒被收成候選的區域不進預算——它們不會被送出去，報告改在「預計未收為結構化圖面的頁 / 區域」
> 那一段逐筆列出頁碼、bbox 與原因。

三種處理方式:

1. 把 PDF 拆成較小的檔案分批入庫(通常最省事,也讓失敗範圍變小)。
2. 調高 `config.py` 的對應上限(例如 `FIGURE_MAX_VL_CALLS_PER_DOC`)—— 它是 repo 常數,改它是改 repo。
   這些是**成本上限**,調高的代價是更慢、更吃資源。它們也會改變實際送進模型的東西
   (image token 上限影響解析度、tile 上限影響怎麼切、candidate 上限影響哪些框被抽),
   甚至可能超出你的 server / model 能吃的範圍 —— **不要假設調高之後結果一定一樣或更好**。
   調完請重跑一次 preflight,並依 `verification_status` 與人工覆核判斷結果。
3. 在終端機直接跑(沒有 MCP 的單次呼叫 timeout):

```bash
python3 RAG.py docs/datasheet.pdf knowledge.json
```

### PDF ingest 失敗說「structured 抽取失敗(truncated)」

`finish_reason="length"` 代表那張圖的結構化輸出**比輸出預算長**,不是模型答錯。
**那一張缺席,其餘照常入庫**(契約:抽壞的不得以半套內容入庫,但不牽連整份文件)。
結果會列出是哪幾張,`review_figures(action="list")` 也看得到(`in_kb=false`)。
仍然整份零寫入的是「剩下的圖也不能信」那幾種:VL 連不上 / 逾時、預算超限、
capability probe 未過、來源檔中途被換掉。

正常情況下你不需要做任何事:第一次抽取用 `config.VL_INGEST_MAX_TOKENS`(預設 2048),
撞頂之後那一次重試會**自動**把預算加大到 VL server 的 context 還放得下的程度
(`n_ctx - 實際 prompt tokens - 128`),上限是 `config.FIGURE_VL_MAX_TOKENS_CEILING`
(預設 8192)。實測 `example1.pdf` p3 的 block diagram:2048 撞頂 → 自動升到 6385 →
只用 2161 就寫完,17 個 component、33 條 relation 全部入庫。

`max_tokens` 是**上限不是目標**,所以放大它不會讓短輸出變貴;天花板存在只是為了擋住
「模型陷入重複、把整個 context 生滿」。

真的看到這個錯誤時,訊息會帶出當時的數字(用了多少 max_tokens、prompt 多長、
server n_ctx 多少)。依序試:

1. 把 **VL server 的 `-c`** 開大 —— 8192 的 context 扣掉一張 1400x900 的圖之後,
   輸出只剩約 6.4k token。這是最常見的真兇。
2. 提高 `config.FIGURE_VL_MAX_TOKENS_CEILING`(repo 常數)(只有在 `-c` 已經很大時才會是瓶頸)。
3. 提高 `config.VL_INGEST_MAX_TOKENS`,讓**第一次**就給夠 —— 省掉那次注定撞頂的
   呼叫(一張大圖大約 60 秒)。
4. 調小 `config.FIGURE_MAX_IMAGE_TOKENS_PER_CALL` 讓圖切成多個 tile,每個 tile 的
   輸出自然變短(代價:更多次呼叫,而且跨 tile 要接合)。

> 已經頂到 context 上限時**不會**再送一次相同的請求 —— 那只是多花一分鐘拿到同一個
> `length`。所以這種失敗只會看到一次呼叫。

### ingest 逾時,但看得到中途輸出

MCP 端是逐行讀子行程輸出的,所以逾時時**已經收到的每一行都會保留在回傳裡**(看得到卡在
第幾張圖、第幾頁)。同時子行程連同它的 process group 會被 SIGTERM → SIGKILL 收掉,
而且會**確認收屍**才敢說「已確認終止」。

如果回傳寫的是「**無法確認子行程已終止**」,那就照它附的 `ps` / `kill` 命令自己確認一次 ——
那代表 RAG.py 可能還在背景跑、**仍可能寫入 knowledge.json**,這次呼叫不能當成零寫入。

正常情況下入庫是**原子提交**,逾時中止通常代表零寫入;要確認就呼叫 `reload_knowledge_base`
看 chunk 數。
接下來照回傳裡附的那條命令在終端機跑(它已經幫你把路徑做好 quoting,含空白也能直接複製)。
PDF 的話回傳還會多附一條 `--preflight` 版本,先估成本再決定。

如果回傳寫的是「輸出不完整」而不是逾時,那代表讀取子行程輸出的執行緒出了問題 —— 這種情況
**不會**回報成功,因為手上的輸出不足以判斷入庫結果;一樣改用 CLI 重跑並看 chunk 數。

### 查得到那張表,但嚴格模式拒絕用它回答數值

這是設計行為,不是 bug。`query_knowledge_strict` 在 **code 層**排除未通過驗證的圖片內容
(`needs_review` / `unverified` / `legacy_unverified`),所以它不會用沒被獨立證據佐證的圖片
數值回答 register、bit range 或規格數字。被擋下的那些會出現在回傳的 `excluded_figures`
(帶 source、頁碼、`figure_id`、kind、狀態與原因)與 `review_hint` —— **四條回傳路徑都有**,
所以「全部候選都被擋」時你仍看得到「哪一頁、哪一張圖可用但待覆核」,不會被誤導成「查不到」。

診斷順序:

```text
請用工具 review_figures,action 設 "list",列出待覆核的圖與原因。
```

看 `reasons`:

- `▯` / glyph 衝突 → 該字元在原圖上就分不出來(例如 `8` 與 `B`、`0` 與 `O`)。這種只能人看原圖。
- 缺 row/line、tile 縫合不確定、截斷 → 抽取沒能覆蓋完整,同樣要人工確認。
- `legacy_unverified` → 這張是**舊 KB** 或**純 raster 路徑**的 chunk。舊 KB 的圖片 chunk 在
  載入時一律補成這個狀態(只在記憶體內,不改你的 `knowledge.json`)。

處理方式(先看那一筆有沒有 `figure_id`:有才是 structured、才進得了 `review_figures`):

- **structured figure(有 `figure_id`)** → 可以用 `review_figures(action="fix", ...,
  confirm_against_image=True)` 人工覆核(它會改知識庫,permission 是 `ask`,你會在核准框
  看到完整參數)。
- **原生表格(PDF 裡可以選取文字)** → `remove_document` 之後重新 `ingest_document`,
  它會走結構化 lane,**可能**拿到可信狀態。但「有原生文字」不保證 `native_verified`:
  那需要兩個一致的原生 evidence channel;只有一個通道時是 `unverified`,通道矛盾時是
  `needs_review`。native lane 不呼叫 VL,所以也**不會**產生 `corroborated`。實際結果以
  重 ingest 後 `review_figures(action="list")` 顯示的為準。
- **新版 ingest 的掃描版／拍照版純 raster** → 先分類成 table／terminal／prose／diagram
  並產生 structured figure，所以會出現在 `review_figures`；被判定「不是圖面」（封面、logo、
  照片）的則零抽取、零 chunk，只出現在 ingest 的缺席清單與 review artifact 的
  「判定不是圖面」一節。沒有獨立原生證據時仍是
  `unverified`／`needs_review`，strict 查詢不會採用；只有人對原圖以
  `confirm_against_image=True` 核准指定 revision 後才可能成為 `human_verified`。
- **舊 KB 的 legacy VL chunk（沒有 `figure_id`）** → 沒有 canonical payload 可 fix；要走
  structured review 必須用新版重新 ingest 原始文件。沒有原圖或人工確認時，不得把純
  raster 數字宣稱為 strict-trusted。
- 覆核時如果 `list` 回 `payload: (讀不到)`,代表那份 review artifact 已經被清掉了;
  `fix` 需要 canonical payload,只能重新 ingest 該文件。清除的影響見
  [setup 的清除 PDF review artifacts](#review-artifacts)。

**升級注意**:`review_figures` 的人工核准閘寫死在 `client_policy.ASK_TOOLS`,不依賴任何
外部設定檔——舊安裝升級後不需要為了這件事改任何東西。
<a id="server-errors"></a>

### Server、embedding buffer 與舊部署

`/health` 不通或 404 時，檢查 deployment 的四角色 URL、port 與各 role log。

```bash
python3 scripts/check_status.py --strict
curl -s http://localhost:8080/health
curl -s http://localhost:8081/health
curl -s http://localhost:8082/health
curl -s http://localhost:8083/health
```

health OK 只是基本可達；VL、工具 calling、DSpark 與模型身分仍各自驗證。
embedding／reranker 的 `cache_ram: 0` 是安全預設，不應因升級就刪掉；main 的生成 cache
與這項 workaround 分開。缺 `--cache-ram` 需更新 build。

短 embedding 成功、真實 ingest 卻回 `input ... too large`，查看 physical batch `-ub`。
embedding 產生設定為 `-c/-b/-ub 8192`；reranker 用精靈必答的 buffer 同步三個值。
Qwen3 causal reranker 的 KV／compute 占用可遠超 GGUF 大小，降低 buffer 前先確認最長
query + passage 能放下，不用單次短 curl 宣稱完整入庫可行。

`主模型未設定`／registry 路徑不存在時，確認 `services.main.model` 與 models.json 實際路徑，
不能保留 `<CODE_MODEL>` placeholder。local 用 set_config 重設；client 用附加入口重新匯入
A 的 manifest，不手填 B 本機 GGUF。`chunks=0` 先確認 ingest 成功，strict 排除看
`excluded_figures`／`excluded_text`，不是一律「查不到」。

已移除的舊介面可能留下 `codetrail-web` tmux／aicode_web symlink。先確認是舊部署後
個別停止或移除，不能殺掉其他工作 session；現在遠端操作用 SSH 加 `aicode`。
<a id="patch-errors"></a>

### `apply_patch(...)` 被拒絕

`apply_patch` 同一次只接受一種格式(SEARCH/REPLACE 或 unified diff),參數已是字串、不要包 Markdown fence;混用、孤立 marker、fence、marker 外的說明文字都會被拒絕。兩種格式共用的拒絕原因:非 UTF-8 檔案、CR-only／mixed newline、目標或路徑上有 symlink、路徑不是 repo-relative POSIX(絕對路徑、Windows drive／UNC、`..`)——這些都是整份 patch 拒絕、零寫入。一次改超過 5 個檔案或單檔 200 行也會被拒。

「驗證不完整」或「驗證未通過」**不是拒絕**:patch 已套用、未回滾,請看回覆裡的 syntax 診斷,或另行呼叫 `run_lint(fix=False)`。

#### SEARCH/REPLACE 被拒絕

- SEARCH 與檔案現況不逐字相同(縮排不同也算)→ 工具不會拿相似位置代套;先 `read_file(...)`
  重讀,從現況逐字重建 SEARCH(錯誤訊息會附最接近位置的檔案現況與第一個差異)。
- SEARCH 在檔案中出現多處 → S/R 沒有行號提示,一律拒絕;多帶幾行讓它唯一。
- 多個區塊互相重疊、或第二個區塊要靠第一個區塊套用後才存在 → 拒絕(全部區塊都對同一份原始檔定位)。
- 空 SEARCH 只能建立不存在的新檔;檔案已存在(含 0 byte)就會被拒。

#### unified diff 被拒絕

先知道什麼**不會**造成拒絕(只適用 unified diff):hunk 的行號/行數錯誤無害——定位靠 context
內容,`@@` 可以完全不帶行號;已套用過的 hunk 會自動跳過(重試安全)。

常見原因:

- 模型讀到的是舊內容,context 行與檔案現況不符 → 先 `read_file(...)`
  重讀目標區段(錯誤訊息會附上最接近位置的期望/實際對照)。
- context 行太少,在檔案中多處出現、無法消歧 → 增加 context 行數,
  或在 `@@` 標大約行號當提示。

把任務拆小,要求模型一次只改一個行為。

### `run_command(...)` 被拒絕

命令不在白名單或含 shell metacharacter。timeout 只接受整數 1..600 秒（server 端上限；client 可能更早截止），不是這個範圍的整數會在執行前被拒絕。請模型改用已允許的最小命令,例如:

```text
請改跑 pytest tests/test_x.py,不要使用 &&、|、; 或 shell script。
```

---
