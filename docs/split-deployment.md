# A/B 分離部署

[回到 README](../README.md)。

A 執行 main、embedding、reranker、VL 四個 llama-server；B 執行 aicode、MCP、原始碼編輯、build、KB 與私人評測。B 不需要 GPU、GGUF、mmproj、tmux 或 llama-server。未設定 `mode` 的既有部署保持 `local`；另外兩種模式是 `model-host`（A）與 `client`（B）。

## 在 A 設定及啟動

```sh
./set_config.sh --mode model-host --allow-remote
~/start.sh
python3 deployment_profile.py export-client --server-url http://10.20.30.40 > ~/codetrail-client.json
```

`10.20.30.40` 請換成 A 對 B 的私有直連 IP。四個 port 來自 A 的 deployment profile。`--allow-remote` 維持既有明確開放行為；請在 A 的防火牆只允許 B 的來源位址連這四個 port。程式端白名單不能代替網路 ACL。原生 HTTP 不提供加密；需加密時使用組織管理的安全直連網路，或已正確配置憑證的 HTTPS endpoint，TLS 驗證不會關閉。

model-host 設定會串流計算每個 GGUF（含所有 shard）及 VL projector 的 SHA-256，建立 `identity_alias`，並透過 llama-server `--alias` 提供 live `model_alias`。這會讀取完整模型檔，時間取決於模型大小與磁碟。換權重、shard 或 projector 後必須重跑設定、重啟 A、重新匯出 B 設定；啟動會拒絕與自動產生 alias 不符的權重。A 的 GPU、ctx、offload 與 transaction/restore 機制沿用既有流程。

匯出的檔案只含目的地與模型 ID，不含 GPU、本機模型路徑或任何授權。

## 在 B 授權四端點

將 A 匯出的 JSON 透過組織允許的方式移到 B，再執行：

```sh
./set_config.sh --mode client --endpoint-manifest ~/codetrail-client.json
python3 scripts/check_status.py --strict
python3 scripts/doctor.py
aicode
```

設定精靈先完整列出四角色 URL 與 alias，確認後把目的地寫入 `deployment.json`，把四角色的精確授權寫入 owner-only `client.json` 的 `model_endpoints`。自動化可加 `--yes`；`--dry-run` 顯示內容而不提交。B 只寫上述兩個檔案，不建立或覆寫 `models.json`、`~/start.sh`。既有 `--restore-last-backup` 仍可原子還原該交易。

不用 manifest 時，必須明列四組 `--main-url/--main-model`、`--embed-url/--embed-model`、`--rerank-url/--rerank-model`、`--vl-url/--vl-model`；model 值是 A 公開的版本 alias。手動管理 alias 時，管理員必須保證該 alias 的版本不可重新指到別的權重。

端點限定原生 llama-server 根 URL 與 literal 私有 IP；四角色必須有不同 origin。拒絕 hostname、credentials、query/fragment、未核准的 scheme/IP/port/path、第三方 provider、環境 proxy、netrc 與 redirect。`deployment.json` 改 URL 不會擴大 `client.json` 的授權；client 模式的 `model_remote_ok: true` 也不能繞過精確白名單。

KB 脈絡生成會傳送較大範圍的文件內容，另需 `kb_context_remote_ok: true`，或在 B 設定時明確加 `--kb-context-remote-ok`。主模型端點授權不包含這項授權。編譯仍使用既有 `build_commands`、interactive 核准、sandbox/container 與 readonly 規則。

## 診斷、身分與評測

B 的啟動與 strict status 觀測每個 endpoint 的 live health、alias、capabilities，以及 main 的 live n_ctx；不以 B 本機檔案、GPU/PID 或設定 ctx 猜測服務狀態。B 的 launch 會拒絕執行，stop 說明需在 A 操作，不會終止 B 上的其他行程。`--no-network` 只能檢查設定，不能宣稱四模型已驗證。

`model_identity.capture_model_identity(role)` 只發 `/props` GET。local 身分核對 live model_path 並 hash 本機模型；client 身分核對版本 alias 與 live 資料，記為 `declared-runtime-alias`、`artifact_sha256: null`。這表示已核對 A 宣告的版本，**不表示 B 讀過或獨立驗證 A 的權重內容**。缺失／不符的 live 身分不會重用舊 cache。checkpoint 與私人 session eval 會記錄這個差別，eval 同時綁定四角色身分。既有 canary、KB A/B 比對與 eval 使用相同端點政策；replay 只繼承 owner-only 端點授權，壓縮、工具權限等評測行為仍固定。

本功能的離線 smoke contracts 使用合成模型與 mock HTTP。未在兩台真實主機完成部署時，離線檢查不能視為雙機驗收。
