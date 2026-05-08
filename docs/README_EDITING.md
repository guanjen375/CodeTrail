# README 維護指南

`README.md` 是新手入口,也是事實的「公開臉」 — 改錯一次新手就裝不起來。
這份文件告訴你**改任何相關東西時,還要同步改哪些檔案、跑哪些檢查**。

> 自動化護欄:`scripts/check_readme_consistency.py` + `tests/test_readme_consistency.py`,
> CI 跑不過就會擋。

---

## 1. 改模型(MODEL / EMBEDDING / RERANKER / VL)

來源:`config.py`。

要同步改的地方:

- [ ] `config.py` — `MODEL` / `EMBEDDING_MODEL` / `RERANKER_MODEL` / `VL_MODEL`
- [ ] `README.md` — 模型 pull 指令、`opencode.json` model 列表、§4 工具說明若有引用
- [ ] `examples/opencode.example.json` — 若 OpenCode UI 列出的 model 名要更新
- [ ] `eval/spec_questions.json` / `spec_holdout.json` / `spec_adversarial.json` —
      若任何 `gold_evidence` 對應到舊模型名

跑這些確認沒漏:

```bash
python scripts/check_readme_consistency.py
python scripts/check_eval_consistency.py
python scripts/doctor.py            # 確認 ollama 真的 pull 了新模型
```

---

## 2. 改 MCP tool(增 / 減 / 改 tool)

來源:`mcp_server.py` 的 `@mcp.tool()`。

要同步改的地方:

- [ ] `mcp_server.py` — `@mcp.tool()` 函式定義 + docstring
- [ ] `README.md` §4「暴露的 N 個工具」— 表格行 + **數字 N**
- [ ] 若新工具會碰檔案 / 跑命令 / 修改 KB,在 §10「安全 / NDA 注意」加風險提醒
- [ ] `tests/` — 新增工具的核心測試(sandbox 邊界、錯誤處理)
- [ ] `AGENTS.md` 的「不要砍」清單若需要更新

跑這些:

```bash
python scripts/check_readme_consistency.py     # 工具數字 + 名字必須一致
python -m pytest -q
```

---

## 3. 改 `run_command` 白名單

來源:`config.ALLOWED_COMMANDS`,`mcp_server.py` 內 `_EXTRA_BUILD_COMMANDS`。

要同步改的地方:

- [ ] `config.py` — `ALLOWED_COMMANDS`(基本測試 / 靜態分析)
- [ ] `mcp_server.py` — `_EXTRA_BUILD_COMMANDS`(build 系列;OpenCode + MCP 特有)
- [ ] `README.md` §4「`run_command` 白名單」描述
- [ ] `tests/test_run_command.py` — 為新命令補 happy / 拒絕 path
- [ ] `tests/test_config.py` 內的危險命令黑名單仍然要拒絕 `rm/sudo/curl/...`

**禁忌**:不要加 `rm` / `sudo` / `curl` / `wget` / `bash` / `sh` / `chmod` / `mkfs` / `dd`。

跑:

```bash
python -m pytest tests/test_run_command.py tests/test_config.py
```

---

## 4. 改 RAG ingestion 流程

涉及:`RAG.py`,`mcp_server.py` 的 `ingest_document` / `remove_document` /
`reload_knowledge_base`,`knowledge.py`。

要同步改的地方:

- [ ] `README.md` §6「RAG 知識庫」段落(尤其 ingest → reload 的兩步流程)
- [ ] §11 疑難排解的 `query_knowledge` / `ingest_document` 行
- [ ] `examples/first-prompts.md` 若有 ingestion 範例
- [ ] `tests/` — 若有 KB 邏輯測試

跑:

```bash
python RAG.py --help                           # 必須 0 即返回
python -m pytest -q
```

---

## 5. 改產品狀態(README 頂部那段)

預設文案:**「成熟私有部署版,不打算公開發布」**。

`scripts/check_readme_consistency.py` 會檢查 README 內必須出現以下任一句:

- 「成熟私有部署版」
- 「不打算公開發布」
- 「不公開發布」
- 「未做公開」
- 「公開產品級安全審計」

**不要**改成:

- ✗ "Production-ready"
- ✗ "Enterprise-grade"
- ✗ "Audited"
- ✗ "Battle-tested"

除非你**真的**做了那些事(請先補對應證據 / 報告連結再改)。

---

## 6. 每次改完 README,跑這四條

```bash
python scripts/check_readme_consistency.py
python scripts/check_eval_consistency.py
python scripts/doctor.py --no-network
python -m pytest -q
```

四條都過才算完成。

---

## 7. 漂移歷史(避免再犯)

下面這些都是真實發生過的 drift,後人請以為戒:

- `RERANKER_TOP_N` 改了但 `eval/spec_holdout.json` 的 `gold_evidence` 沒改 → eval 失準。
- `_parse_unified_diff` 從 `agent.py` 搬到 `agent_tools.py`,但 `eval/code_questions.json`
  仍寫 `agent.py` → CodeRAG 評測 false fail。
- `EMBEDDING_MODEL` 從 `mxbai-embed-large` 換成 `bge-m3`,但 `eval/spec_adversarial.json`
  還引用舊模型名。
- 模型 tag 在 README 寫 `qwen3-coder:30b-instruct-q4_K_M`,`config.py` 是
  `qwen3-coder:30b` → 新手 `ollama pull` 出錯。

新功能上線前先想:**這個 fact 有沒有出現在 README / config / eval / mcp_server 任一處?**
有的話一起改。
