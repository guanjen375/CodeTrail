# 常見問題

這份文件整理 OpenCode / CodeTrail / llama-server 常見故障排查。

[回到 README](../README.md)。

---

## 常見問題

### `/status` 沒看到 CodeTrail MCP Connected

檢查:

```bash
python -m json.tool ~/.config/opencode/opencode.json >/dev/null
command -v aicode
command -v opencode
```

再確認 `opencode.json` 裡的 MCP command 能找到目前 git root 內的 `.opencode/run-codetrail-mcp`,且 `/status` 裡的名字會跟 `mcp` key 一致;如果 key 是 `codetrail`,應該看到 `codetrail Connected`。

### 啟動時拒絕 `AICODE_ROOT`

你可能在 `$HOME` 或 `/` 執行了 `aicode`。切到具體專案:

```bash
cd ~/work/some-firmware-repo
aicode
```

### `[ctx-safety] refuse to start.` 啟動被擋

代表 `aicode` 讀了主 llama-server 的 `/props`,發現你要求的 `AICODE_DYNAMIC_NUM_CTX_MAX` 大於 server 啟動時的 `-c <N>`。輸出長這樣:

```
[ctx-safety] UNSAFE: model=<CODE_MODEL> requested_ctx=65536
        requested ctx=65536 超過 llama-server 啟動時的 -c 8192 (http://localhost:8080) — 多出來的 prompt 會被截斷
        ...
        建議任一處理:
          (a) export AICODE_DYNAMIC_NUM_CTX_MAX=8192  (對齊 server n_ctx)
          (b) 重啟 llama-server 並提高 `-c 65536` (確認 VRAM 夠)
```

兩條路:

```bash
# 路徑 A: 把 CodeTrail 端的上限降到跟 server 一致
export AICODE_DYNAMIC_NUM_CTX_MAX=8192
aicode

# 路徑 B: 停掉舊 server,用新 -c 重啟,把 server 上限拉大
pkill -f "llama-server.*--port 8080"
llama-server -m ~/models/<MODEL>.gguf --host 0.0.0.0 --port 8080 -c 65536 -ngl 99 &
aicode
```

如果你確認要硬跑(例如想實測 truncation 的影響),用一次性放行:

```bash
AICODE_ACCEPT_CTX_RISK=1 aicode
```

如果不想再看到這個檢查(例如自動化、CI、知道自己在做什麼):

```bash
export AICODE_CTX_SAFETY_DISABLE=1
```

server 沒啟動 / 不可連時會印 `[ctx-safety] UNKNOWN` 並放行,不會擋啟動。手動驗證可以單跑:

```bash
AICODE_MODEL=<CODE_MODEL> python scripts/ctx_safety_check.py
```

`<CODE_MODEL>` 是佔位符,必須替換成實際模型名稱或 GGUF 路徑。

### llama-server 不可連 / 404

代表對應 server 沒啟動,或 port 設錯。先 curl 試:

```bash
curl -s http://localhost:8080/health
curl -s http://localhost:8081/health   # embedding
curl -s http://localhost:8082/health   # reranker
```

回 `{"status": "ok"}` 才算 ready。沒回應就重啟對應 server(見 [docs/setup.md](setup.md))。

啟動 server 後可以看 model_path 確認載對 GGUF:

```bash
curl -s http://localhost:8080/props | jq '.model_path, .default_generation_settings.n_ctx'
```

### `aicode` 拒絕啟動,訊息說「主模型未設定」

CodeTrail 不內建主聊天 / 程式推導模型,沒設好 `aicode` 會 fail-loud。任選一種設定方式(擇一即可):

```bash
# 1) 環境變數 (最優先)
export AICODE_MODEL=<CODE_MODEL>

# 2) per-run CLI 旗標
aicode -m <CODE_MODEL>

# 3) ~/.config/opencode/opencode.json 設 "model": "<provider>/<CODE_MODEL>"
```

`<CODE_MODEL>` 是 MODEL_REGISTRY 裡的 bare name 或 GGUF 絕對路徑。如果你看到「placeholder」相關錯誤,通常是值還停留在 `<CODE_MODEL>` 或 `<MODEL>` 沒換掉;看到「外部 provider prefix」錯誤代表你還在用 `ollama/foo` 那種舊寫法,改成 bare name 或你 opencode.json 裡 custom provider 的 prefix。

### MODEL 解析到 GGUF 路徑但檔案不存在

doctor 報:

```
[FAIL] MODEL=qwen3-coder-32b ... 解析到 ~/models/qwen2.5-coder-32b-instruct-q4_k_m.gguf 但檔案不存在。
```

兩種原因:

1. registry mapping 寫錯路徑 → 修 `~/.config/codetrail/models.json`。
2. registry 沒這個 key,CodeTrail 把 bare name 直接當路徑 → 加 registry 或改用絕對路徑。

### 查 spec 沒結果

先確認文件已經匯入並 reload:

```text
請 reload_knowledge_base,回報目前載入幾個 chunks。
```

如果 chunks 是 0,重新要求:

```text
請 ingest_document docs/spec.pdf,完成後 reload_knowledge_base。
```

如果 embedding server (8081) 不通,reload 會印錯誤;先驗:

```bash
curl -s http://localhost:8081/health
```

### `apply_patch(...)` 被拒絕

常見原因:

- 模型讀到的是舊內容,先 `read_file(...)` 重讀目標區段。
- patch context 不夠或不匹配。
- 一次改超過檔案數或行數限制。

把任務拆小,要求模型一次只改一個行為。

### `run_command(...)` 被拒絕

命令不在白名單,或含 shell metacharacter。請模型改用已允許的最小命令,例如:

```text
請改跑 python -m pytest tests/test_x.py,不要使用 &&、|、; 或 shell script。
```

---
