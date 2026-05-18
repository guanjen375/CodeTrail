# CodeTrail - OpenCode + Ollama 本地 MCP 工作台

CodeTrail 是一個給 OpenCode 使用的本地 MCP 後端。你在 OpenCode TUI 裡提問，模型可以透過 CodeTrail 讀專案、找程式碼、查已匯入的 spec、分析截圖或 binary、產生 patch，並在允許的白名單內跑驗證命令。

CodeTrail 目前定位是**成熟私有部署版**：適合本機、離線、NDA / firmware / private repo 分析；**不打算公開發布**成 PyPI package、Docker image 或 SaaS。安全邊界有自動測試保護，但未做公開產品級安全審計。

---

## 快速開始

先安裝 OpenCode、Ollama、Python 依賴，完整步驟見 [docs/setup.md](docs/setup.md)。最小流程是：

```bash
cd <CODETRAIL_REPO>
pip install -r requirements.txt
pip install mcp pymupdf4llm ollama

ollama pull qwen3-coder:30b
ollama pull bge-m3
ollama pull qllama/bge-reranker-v2-m3

python scripts/doctor.py
```

接著做兩個一次性設定：

```bash
mkdir -p ~/.config/opencode
${EDITOR:-vi} ~/.config/opencode/opencode.json
```

把 [docs/setup.md](docs/setup.md) 裡的建議 `opencode.json` 貼進去；它會設定 Ollama provider、CodeTrail MCP server 和 OpenCode 權限。

再把 `aicode` 放進 PATH：

```bash
chmod +x ./aicode
mkdir -p "$HOME/.local/bin"
ln -sfn "$PWD/aicode" "$HOME/.local/bin/aicode"
command -v aicode
```

完成後，從要分析的專案根目錄啟動：

```bash
cd <PROJECT_TO_ANALYZE>
aicode
```

進入 TUI 後確認：

- 啟動畫面有 `[aicode] AICODE_ROOT=<PROJECT_TO_ANALYZE>`。
- `/status` 顯示 `codetrail Connected`。
- model selector 選的是 Ollama provider 的 coding model。
- 第一輪工具呼叫沒有嘗試讀 `$HOME` 或 `/`。

---

## 快速導覽

進入 OpenCode 後，可以先用幾句話確認工具有接好。下面的路徑請換成你專案裡真的存在的檔案；如果附件在專案外，啟動時先開 `AI_CODE_ALLOW_EXTERNAL_IMPORT=1`。

```text
請用工具 list_dir 看專案結構，找出可能的 entry point、測試目錄和設定檔。
```

```text
請用工具 read_file 讀 README.md 前 80 行，整理這個專案怎麼啟動。
```

```text
請先用工具 import_external_file 匯入 ~/Downloads/error.log，
再用 read_file 讀回傳的新路徑，找出最重要的錯誤訊息。
```

```text
請用工具 ingest_document 匯入 docs/spec.pdf，
完成後 reload_knowledge_base，回報目前載入幾個 chunks。
```

```text
請用工具 query_knowledge_strict 查這份 spec 裡最重要的限制或預設值，
證據不足就拒答，回答要附 REF。
```

附件與 RAG 的完整用法見 [docs/rag.md](docs/rag.md)。

---

## 常用模型

| 模型 | 用途 | 建議 |
|---|---|---|
| `qwen3-coder:30b` | 日常讀 repo、改 code、產 patch | 預設主力，穩定優先 |
| `qwen3.6:35b-a3b-q4_K_M` | 跨檔推理、規格 vs 實作比對 | 32GB VRAM 先用 `AICODE_DYNAMIC_NUM_CTX_MAX=32768` |
| `devstral:24b` | 快速 review、簡單 patch | 快，但工具鏈穩定度要再確認 |
| `gpt-oss:20b` | 快速理解、摘要、初步定位 | 適合探索，不適合作為最終 patch 主力 |
| `bge-m3` | RAG / Code-RAG embedding | 必要模型，不要當聊天模型選 |
| `qllama/bge-reranker-v2-m3` | RAG rerank | 建議安裝，查 spec 排序更穩 |
| `qwen3-vl:30b-a3b` | 截圖、UI error、圖片進 KB | 需要分析圖片時再 pull |

換模型時不要只在 TUI 裡 `/models` 切換。正確方式是退出 `aicode`，用 `AICODE_MODEL=<MODEL>` 重新啟動，讓 OpenCode TUI 與 CodeTrail MCP server 內部呼叫一致。硬體、context、遠端 Ollama 設定見 [docs/models.md](docs/models.md)。

---

## 必守安全界線

- `AICODE_ROOT` 是本次 OpenCode 可讀寫的 sandbox 根目錄；不要從 `$HOME` 或 `/` 啟動。
- MCP server 啟動時會拒絕危險 root，並把工具限制在 `AICODE_ROOT` 內。
- `knowledge.json`、`knowledge_emb.npz`、`data/`、`.codetrail/`、`*.jsonl` 和 `.code_rag_cache_*` 通常含 NDA 片段，不要 commit。
- `apply_patch(...)` 有 context matching、max files、max lines 限制；不要放寬安全層。
- `run_command(...)` 只允許白名單命令，不支援 shell metacharacter。
- 遠端 Ollama 會收到 prompt、程式碼片段、spec 摘要與工具輸出，只能指向可信內網 / VPN 主機。

完整安全說明見 [docs/security.md](docs/security.md)。

---

## 文件地圖

| 文件 | 內容 |
|---|---|
| [docs/setup.md](docs/setup.md) | 安裝、OpenCode config、`aicode`、啟動 |
| [docs/models.md](docs/models.md) | 模型比較、顯卡建議、遠端 Ollama、context / offload 後果 |
| [docs/rag.md](docs/rag.md) | 讀檔、匯入附件、建立知識庫、Code-RAG、查 spec |
| [docs/mcp-tools.md](docs/mcp-tools.md) | CodeTrail 暴露的 17 個 MCP 工具與使用原則 |
| [docs/security.md](docs/security.md) | sandbox、patch、run command、NDA 資料、工作節奏 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | `/status`、模型 404、查 spec、patch / command 被拒絕 |
| [README_DEV.md](README_DEV.md) | 開發者維護命令、測試、eval、context gate 設計 |
| [AGENTS.md](AGENTS.md) | AI coding agent 修改本 repo 時必讀的安全規範 |

---

## License

本專案以 MIT 授權釋出，程式碼以「現狀」（AS IS）提供，不附帶任何明示或默示的保證，
包括但不限於可商用性、特定用途適用性、不侵權、資安、隱私、合規、或 NDA 適用性。
完整法律文字見 [LICENSE](LICENSE)；補充免責說明見 [DISCLAIMER.md](DISCLAIMER.md)。

This project is licensed under the MIT License. See [LICENSE](./LICENSE).

## Responsible use

This project is provided for lawful software development, research, education,
and code reasoning workflows.

Users are solely responsible for how they use, modify, deploy, combine, or
redistribute this software, including compliance with applicable laws,
contracts, licenses, NDAs, platform terms, model-provider terms, and third-party
rights.

The authors do not guarantee that any particular workflow is legally compliant,
NDA-compliant, secure, private, or suitable for a specific use case.

The software is provided "as is", without warranty of any kind. The authors do
not encourage, endorse, or provide support for unlawful use.

See [DISCLAIMER.md](./DISCLAIMER.md) for the full disclaimer.
