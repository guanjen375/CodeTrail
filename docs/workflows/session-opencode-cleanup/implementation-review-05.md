本輪：流程第 5 步，實作靜態審核第 5 輪；審核模型 **gpt-6-astra / effort max**。只核對 Fable fix-03 對第 4 輪三項失敗的修復。

審核 HEAD：`1ff76156e9d2ee8dcc77f8f4f7661f6f8bf05fc7`；程式碼基準 `a1682d5`。產品改動仍未提交，58 個路徑的凍結快照為 `review-05-tree.json`；完整產品 diff SHA256：`19892f4e664164159396e1bf5c3e9832510eba74cd06cb31e6413814a11a1364`。

**三項修法均靜態接受，本輪沒有仍須修改的靜態缺陷；第一次 smoke 的 3 個失敗仍待追加驗證。** 這不是實測 Blocker 0，也不是整體完成。

已讀 `fix-03.md`、`implementation-review-04.md`、`test-change-audit.md` 最新附錄，並對照 `fix-03-only.diff`、修改前原文、目前三份測試及相關 runtime 呼叫路徑。另自行以原始檔 SHA256 與 AST 比對，沒有執行 repo 函式或測試：

- 修改前的三份原文均吻合第一次 smoke 的 `review-03-tree.json` 雜湊；目前 58 個路徑均吻合 `review-05-tree.json`，完整 diff 檔的雜湊也一致。
- 相對 smoke 快照，只改 `tests/test_server_scripts.py`、`tests/test_doctor.py`、`tests/test_repo_consistency.py`；其餘 55 個路徑一致，runtime 與 `tests/test_smoke_gate.py` 未變。
- 三個失敗 node 的原有 assert AST 分別為 **8、4、12**，合計 **24**，前後完全相同；三個 node 的 decorator 及三份檔案的測試名稱集合未變。
- `_walk_files`、`_repo_sources`、`_iter_text_files` 的 AST 完全相同。以上獨立核對與編排者 `fix-03-static-audit.json` 相符，屬靜態證據，不能計為測試通過。

**1. stop／status fixture：接受。**

位置：`tests/test_server_scripts.py:1109` 的 `_fake_nvidia_smi`，及 `:1136`、`:1146` 的兩次階段設定；node 為 `test_stop_and_status_use_argv_and_constants_not_the_shell`。

stop 前寫入空 GPU process 回覆；fake tmux 仍表示 session 不存在、fake ss 仍表示沒有 listener。依 `scripts/stop_servers.py` 的控制流程，不會建立 tracked PID、進入等待或 signal 分支，最終 GPU 盤點也沒有 rows。stop exit 0、常數 session 名、舊 shell 名不得出現的原有斷言均保留。

完成 stop 斷言後才把同一個假命令改成 `FOUR_LLAMA_SERVERS`。其中四個不同的 llama-server PID 供 status 計數，另一筆 Python process 仍由現行 parser 排除；預設 4、忽略 shell 的 99、`--expected 5` 報不足的原有斷言均保留。空的 `--proc-root` 仍使 role candidates 為空，`inspect_deployment` 不會進入 server reader；假命令與 tmp HOME 的隔離也保留。

此修改修正新契約的情境配置，沒有刪掉 status 資料、接受 stop exit 1 或吞例外。基準已有的 `row.used_gpu_memory` 欄位錯誤保持第 4 輪的範圍決定：不重開為本次產品 Blocker，也沒有趁此輪改 runtime 或加相容屬性。

**2. canary 的檔案模型與隔離：接受。**

位置：`tests/test_doctor.py:1342` 起的 fixture；node 為 `test_explicit_gate_and_implicit_diagnostic_are_separate`。

現有 `_write_profile_model` 在 tmp HOME 實際寫入 `deployment.json`，主模型為 `/models/from-deployment-file.gguf`；`env` 明確提供該 HOME 與 tmp `XDG_CACHE_HOME`，同時保留 `AICODE_MODEL=shell-leftover`、`AICODE_TOOL_CANARY_WARN_ONLY=1`。兩次 `explicit_model=""` 均未變，沒有替換 `_model_selection` 或 resolver。

靜態對照 `_model_selection → resolve_main_model → load_effective_profile`：設定檔由傳入 HOME 定位，絕對 GGUF 路徑在此解析階段不要求模型檔存在，shell 模型不參與選擇。cache 與 fingerprint 所需的使用者檔案位置也由傳入的 tmp 路徑定位。原有 protocol、server props、explicit／implicit attempt 替身保留，不會為此 fixture 啟動模型或 MCP。

四個原有斷言及兩段 attempt 安排完全保留：explicit 成功而 implicit FAIL 仍須回 0、implicit 恰好一次且使用 repo timeout、stderr 含 `status=fail`；explicit 連續兩次失敗仍須回 2，且不得執行 implicit。修復讓原測試取得新介面要求的檔案模型，沒有把 shell fallback 加回或改變 explicit／implicit 的驗收語意。

**3. 交接 Markdown 深度：接受。**

位置：`tests/test_repo_consistency.py:1074`；對應 node 為 `test_the_handoff_markdown_exemption_is_content_only`。

predicate 只將 `len(parts) >= 3` 收緊成 `>= 4`，仍同時要求 `.md`、第一層 `docs`、第二層 `workflows`。因此 `docs/workflows/<任務>/<檔名>.md` 保有內容豁免，`docs/workflows/p.md` 缺少任務層則不符合；其他位置的 Markdown、同目錄的可執行檔及設定檔也不符合。測試本體的 12 個斷言未改，包含真實 walker 仍看見交接檔，以及同目錄 Python import／spawn 仍被 gate 抓到。三個 walker 完全未變，沒有把目錄從安全掃描隱藏。

**後續驗收與權限。**

第一次 smoke 的正式紀錄仍是 **2395 selected、2392 passed、3 failed、runner exit 1**，詳見 `smoke-01.md`。目前上述三個 node 都已有具體、可審核的修復版本，但尚無修復後執行結果。不能把原紅燈記成基線、移到 deferred，或把靜態接受寫成實測解除；沒有分歧需要擱置。前幾輪已解除且未變的路徑沿用其靜態結論。

修復版本已具體可驗，等待編排者取得必要的追加驗證同意。取得同意後，應以獲准的範圍取得上述三個 node 原有斷言通過的實際證據，並記錄對應 HEAD、產品快照、命令與結果，再處理整體驗收。唯一預定 smoke 已用完；本報告不授權追加 node 或第二次 smoke，也不建議默默重跑。

本輪僅讀原始碼、文件與 diff，並執行檔案雜湊／AST 靜態比對；零 pytest、collect、checker、runtime／toy 探查。只新增本審核文件，未改產品、測試、AGENTS 或其他交接檔，未 commit/push。歷史偏差仍以 `execution-notes.md` 如實保留：先前 5 次額外探查及兩個已精確撤回的記憶 Edit 不因本輪修復消失；fix-03 的零命令／零記憶寫入為編排者完整日誌核對的紀錄，不能反推整個流程皆無偏差。

**待改靜態缺陷：0；歷史 smoke 失敗待驗證：3。整體驗收尚未結案。**
