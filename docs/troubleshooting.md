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

### 模型 404 或找不到模型

代表 Ollama 沒有該 tag：

```bash
ollama pull qwen3-coder:30b
ollama pull qwen3.6:35b-a3b-q4_K_M
ollama pull devstral:24b
ollama pull gpt-oss:20b
```

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

