# 主要依賴與環境錯誤

CodeTrail 的每項操作只使用選定的主要實作。必要套件、工具、服務或安全能力缺席、版本不相容或執行失敗時，該操作直接回錯誤，修復環境後才能重試。功能依賴在使用時檢查，快取命中也不能繞過檢查。`python3 scripts/doctor.py --no-network` 可先做離線健檢；不帶 `--no-network` 另檢查服務。

| 操作 | 必要實作與修復方式 |
|---|---|
| 啟動 `aicode` | PATH 的 `python3` 與 `requirements.txt`。只安裝名為 `python` 的執行檔不足以啟動。部署不需要 Node / npm。 |
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
| 明確要求容器隔離的評測 | `auto` 只選 Podman；若明確選 Docker，必須可讀 uid/gid 並傳入容器。缺少指定 runtime 或容器模組即報錯，不改在 host 執行。 |
| session / 設定 / 提示來源 / cache / patch 等安全 IO | 支援 dir-fd、`O_NOFOLLOW`、`O_DIRECTORY` 的 POSIX Python；承諾 owner-only 的操作還需 uid 與權限設定能力。能力缺席時拒絕操作，讀取也不建立 state 目錄。 |
| patch 建立新檔 | 檔案系統必須支援 atomic no-clobber hard link；失敗時清理暫存並盡力回滾整批，保留競爭者的檔案。 |
| KB 讀寫鎖 | 必須具備可用的行程鎖；鎖模組缺席、無法取得 flock 時直接報錯，不能把失敗當成可略過的 cache 保存通知。 |
| `~/start.sh stop` 驗證服務停止 | 需要可用的 listener 查詢與所宣告的 GPU 釋放證據。缺少必要工具或無法取得結果會非零結束，不宣稱已確認停止。 |

`client.json` 的 `rerank_fallback_policy` 目前只接受 `"error"`。舊的 `"embedding"` / `"main_model"` 必須刪除或改為 `"error"`；設定載入會提供遷移錯誤。`objdump` 未設定時選 PATH 的 `objdump`，有設定時就只使用那個路徑。`set_config.sh` 遇到損壞的既有 deployment 設定會報錯，請先修復檔案。

本規則針對環境造成的實作替換。明確關閉某功能、來源資料缺席、安全 regex 改字面比對、驗證過身分的 cache 重建、PDF 原生文字與逐行轉錄、取消與非必要預熱的狀態處理仍遵守各自契約。`apply_patch` 的附帶 syntax verification 仍為 passed / failed / skipped；缺 grammar 只會是 skipped，結果標示驗證不完整，不能宣稱通過。
