# A/B 分離部署

[回到 README](../README.md)。

A（host）執行 main、embedding、reranker、VL 四個 llama-server；B（device）執行
`aicode`、MCP、原始碼編輯、build 與 KB。B 不需要 GPU、GGUF、mmproj、tmux 或 llama-server。
未設定 `mode` 的既有部署保持 `local`；另外兩種模式是 `model-host`（A）與 `client`（B）。

## A：設定並啟動模型

在 A 的 CodeTrail checkout 執行：

```sh
scripts/codetrail-host.sh
```

首次會引導模型、GPU、context 與網路設定。開放區網需要明確同意；再輸入 A 對 B 的
literal 私有 IPv4（服務綁定 `0.0.0.0`），供產生四個角色的端點交接資料。腳本使用既有設定與 launcher 啟動服務，
已運作的服務先核對狀態，不為了再次執行腳本而盲目重啟。

依終端顯示的方式保存完整 client manifest，透過組織允許的方式移到 B。
manifest 只含目的地與模型版本 ID，不含 GPU、本機模型路徑或任何授權。
四個 port 來自 A 的 deployment profile。A 的防火牆應只允許 B 的來源位址連線；
程式端白名單不能代替網路 ACL。原生 HTTP 不提供加密，需使用組織管理的安全直連網路，
或已正確配置憑證的 HTTPS endpoint；TLS 驗證不會關閉。

model-host 設定會串流計算每個 GGUF（含所有 shard）及 VL projector 的 SHA-256，
建立 `identity_alias`，透過 llama-server `--alias` 提供 live `model_alias`。
這會讀取完整模型檔，耗時取決於模型大小與磁碟。換權重、shard 或 projector 後，
用 `./set_config.sh` 重新設定、重啟 A 並重新交接 B 設定；啟動會拒絕與 alias 不符的權重。

監看與停止沿用直接命令：

```sh
nvidia-smi
tmux attach -t codetrail-main
~/start.sh stop
```

## B：授權端點並開始對話

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
`./set_config.sh` 依選單操作。

端點限原生 llama-server 根 URL、literal 私有 IP，四角色 origin 必須不同。
拒絕 hostname、credentials、query/fragment、未核准 scheme/IP/port/path、第三方 provider、
環境 proxy、netrc 與 redirect。修改 `deployment.json` 不會擴大 `client.json` 的授權；
client 模式的 `model_remote_ok: true` 也不能繞過精確白名單。
編譯仍使用既有 `build_commands`、interactive 核准、sandbox/container 與 readonly 規則。

## 診斷、身分與評測

`set_config.sh`、host/device 腳本與 `aicode` 均不接受參數；
`~/start.sh` 只接受無參數啟動與單一 `stop`。內部 Python 自動化與診斷入口見
[README_DEV.md](../README_DEV.md#內部設定與啟動介面)。

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
