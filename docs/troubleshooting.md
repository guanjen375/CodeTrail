# 常見問題

這份文件整理 OpenCode / CodeTrail / Ollama 常見故障排查。

[回到 README](../README.md)。

---

## 常見問題

### `/status` 沒看到 CodeTrail MCP Connected

檢查：

```bash
python -m json.tool ~/.config/opencode/opencode.json >/dev/null
command -v aicode
command -v opencode
```

再確認 `opencode.json` 裡的 MCP command 能找到目前 git root 內的 `.opencode/run-codetrail-mcp`，且 `/status` 裡的名字會跟 `mcp` key 一致；如果 key 是 `codetrail`，應該看到 `codetrail Connected`。

### 啟動時拒絕 `AICODE_ROOT`

你可能在 `$HOME` 或 `/` 執行了 `aicode`。切到具體專案：

```bash
cd ~/work/some-firmware-repo
aicode
```

### `[ctx-safety] refuse to start.` 啟動被擋

代表啟動時的安全檢查預估：你選的模型 + 目前 GPU + 要求的 ctx 上限，會把模型推到 CPU offload（會嚴重變慢）。輸出長這樣：

```
[ctx-safety] UNSAFE: model=<CODE_MODEL> ctx=65536
        Requested ctx=65536 → est VRAM needed ≈ 33.3GB (vs total 31.8GB)
        Computed safe ctx cap ≈ 55296
```

照建議 cap 設環境變數重新啟動：

```bash
export AICODE_DYNAMIC_NUM_CTX_MAX=55296
aicode
```

如果你確認要硬跑（例如想實測 offload 的影響），用一次性放行：

```bash
AICODE_ACCEPT_CTX_RISK=1 aicode
```

如果不想再看到這個檢查（例如自動化、CI、知道自己在做什麼）：

```bash
export AICODE_CTX_SAFETY_DISABLE=1
```

檢查跑不準的情況也會發生 —— 拿不到 `nvidia-smi`、跑遠端 Ollama、模型架構特殊 —— 這時會印 `[ctx-safety] UNKNOWN` 並放行，不會擋啟動。手動驗證可以單跑：

```bash
AICODE_MODEL=<CODE_MODEL> python scripts/ctx_safety_check.py
```

`<CODE_MODEL>` 是佔位符，必須替換成實際 Ollama tag。

### 模型 404 或找不到模型

代表 Ollama 沒有該 tag。先 `ollama list` 看裝了什麼，沒有的話再 pull（CodeTrail 不會替你預設任何主模型；自己挑要用的那顆）：

```bash
ollama pull <CODE_MODEL>            # 自己選的主模型
```

### `aicode` 拒絕啟動,訊息說「主模型未設定」

CodeTrail 不內建主聊天 / 程式推導模型，沒設好 `aicode` 會 fail-loud。任選一種設定方式（擇一即可）：

```bash
# 1) 環境變數 (最優先)
export AICODE_MODEL=<CODE_MODEL>

# 2) per-run CLI 旗標
aicode -m ollama/<CODE_MODEL>

# 3) ~/.config/opencode/opencode.json 設 "model": "ollama/<CODE_MODEL>"
```

`<CODE_MODEL>` 是佔位符，必須替換成實際模型名稱。如果你看到「placeholder」相關錯誤，通常是值還停留在 `<CODE_MODEL>` 或 `<MODEL>` 沒換掉。

### 查 spec 沒結果

先確認文件已經匯入並 reload：

```text
請 reload_knowledge_base，回報目前載入幾個 chunks。
```

如果 chunks 是 0，重新要求：

```text
請 ingest_document docs/spec.pdf，完成後 reload_knowledge_base。
```

### `apply_patch(...)` 被拒絕

常見原因：

- 模型讀到的是舊內容，先 `read_file(...)` 重讀目標區段。
- patch context 不夠或不匹配。
- 一次改超過檔案數或行數限制。

把任務拆小，要求模型一次只改一個行為。

### `run_command(...)` 被拒絕

命令不在白名單，或含 shell metacharacter。請模型改用已允許的最小命令，例如：

```text
請改跑 python -m pytest tests/test_x.py，不要使用 &&、|、; 或 shell script。
```

---

