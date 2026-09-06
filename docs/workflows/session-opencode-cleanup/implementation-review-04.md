本輪：流程第 5 步，實作靜態審核第 4 輪；只分析第一次整包 smoke 的 3 個失敗。審核模型 **gpt-6-astra / effort max**。

審核 HEAD：`df47e6144cde02b977862447704d4a81a85691f6`。smoke 執行時 HEAD 為 `ea6a1b613ee1bd34369fd92591f3cdc94a281fa5`，差異只有交接文件；動工前程式碼為 `a1682d5`。本輪逐檔雜湊確認產品樹仍與 `review-03-tree.json` 完全相同，產品 diff SHA256 仍為 `9a078521198130df68f704231fad73354baee1fa140d60e2a00d6a7dadb9755d`。

已讀 `smoke-01.md`、`smoke-once.request.json`、完整 stdout 中這 3 項失敗的 traceback，並逐項對照目前原始碼與 `git show a1682d5:…`。實際結果為 **2395 selected、2392 passed、3 failed、runner exit 1**，不是 0 collected。第 3 輪的靜態 Blocker 0 不代表此次 smoke 已通過；目前有下列 **3 項待修 Blocker**。

**Blocker 1：stop／status 新契約共用的 GPU fixture 提早啟動了不屬於本契約的既有失敗分支。**

- 失敗 node：`tests/test_server_scripts.py::test_stop_and_status_use_argv_and_constants_not_the_shell`。
- 實際紅燈：`:1126` 的 `assert stop.returncode == 0` 失敗；子行程在 `scripts/stop_servers.py:364` 取 `row.used_gpu_memory`，得到 `AttributeError`。
- 基準核對：`a1682d5:scripts/stop_servers.py:346` 已有完全相同的 `row.used_gpu_memory`；當時 `deployment_status.GpuProcess` 的欄位也已叫 `used_memory_mib`。本輪沒有改變此欄位契約或這段最終盤點。因此不能把該行列成本次新增的產品 bug，也不能趁這輪改它或替 dataclass 加相容屬性。
- 本次造成失敗的改動：新 node 在 `tests/test_server_scripts.py:1108–1115` 就讓假 `nvidia-smi` 回傳 `FOUR_LLAMA_SERVERS`，然後先跑 stop。其 fake tmux 表示兩個 session 均不存在、fake ss 表示沒有 listener，卻在 stop 的最終盤點提供 4 個殘留 GPU process，走入基準已有的孤兒提示分支。這 4 筆資料實際是後半段 status 的「預設 4／`--expected 5`」斷言所需；stop 半段要守的是本次改掉的 session 名稱來源。
- 最小修復：在 **stop 階段**讓同一個離線 GPU fixture 表示沒有 process；完成 stop 的原有斷言後，於 **status 階段**才明確提供 4 筆假 process。可用階段各自的假命令或明確更新同一份 fixture，保留假 tmux／ss、tmp HOME、空的 `--proc-root`，不得碰真實服務。
- 可驗收結果：原有 stop exit 0、兩個常數 session 名、舊 shell 名未出現的斷言全部保留；status 仍看到確實存在的 4 筆假 process，預設數量 4，`--expected 5` 仍報不足。不能刪除 status 資料、接受 stop exit 1、吞 `AttributeError` 或 skip 此 node。修的是新測試的情境配置；已發現的基準欄位錯誤不在此次修復範圍，但本 node 的紅燈必須解除。

**Blocker 2：canary 的既有測試未同步本輪刪除模型環境來源的介面。**

- 失敗 node：`tests/test_doctor.py::test_explicit_gate_and_implicit_diagnostic_are_separate`。
- 實際紅燈：`:1367–1373` 預期第一次 `run_all()` 回 0，實際回 2；stderr 明確是「找不到主模型」，還沒進到此測試要驗的 implicit diagnostic。
- 基準與本次改動：該 node 本體和 `env={"AICODE_TOOL_CANARY_WARN_ONLY": "1", "AICODE_MODEL": "shell-leftover"}` 在 `a1682d5` 就存在，且兩次都傳 `explicit_model=""`。基準的 `resolve_main_model_from_env` 會把 `AICODE_MODEL` 當來源，讓 setup 得到一個模型；本輪 `model_resolution.resolve_main_model` 已正確刪除該分支，`scripts/tool_call_canary.py:976` 也已換成新 resolver。測試沒有提供 tmp HOME 的 deployment 設定，因此新介面下根本沒有模型可選。這是本次 resolver 變更遺漏的測試消費者同步，不是要重開基準未變的 canary 行為。
- 最小修復：替本 node 建立隔離的 tmp HOME／cache，使用現有 `_write_profile_model(home, model)` helper 寫入一個可正規化、與 `shell-leftover` 不同的合成主模型；把檔案位置交進 `env`。保留兩個殘留 shell 值及兩次空的 `explicit_model`，讓模型確實從檔案解析。原有 protocol、server props、explicit／implicit attempt 替身保持離線，不去碰真模型；不得把 runtime 的 `AICODE_MODEL` fallback 加回來。
- 可驗收結果：第一次 explicit 成功、implicit FAIL 時仍回 0，implicit 只跑一次且使用 repo timeout、stderr 有 `status=fail`；第二次 explicit 連續失敗時仍回 2，implicit 不得執行。這些原有斷言全部保留，模型選擇與快取位置不得依賴執行者真實 HOME。違約點是最終計畫的檔案設定來源／canary 消費介面未同步完成，以及現有 smoke 新失敗。

**Blocker 3：交接 Markdown helper 少算一層任務目錄，與自己的安全契約不一致。**

- 失敗 node：`tests/test_repo_consistency.py::test_the_handoff_markdown_exemption_is_content_only`。
- 實際紅燈：`:1975` 要求 `docs/workflows/p.md` 不豁免，實際得到 `True`。
- 本次改動：helper 與此 node 都是本輪新增。`_handoff_markdown` 的 docstring 在 `:1058` 定義 `docs/workflows/<任務>/*.md`，但 `:1072` 用 `len(parts) >= 3`；`docs/workflows/p.md` 恰好就是 3 個 component，因此省略任務目錄也通過。不存在可供沿用的基準豁免行為。
- 最小修復：把 predicate 的路徑深度收緊為必須含任務目錄及 Markdown 檔名（此形狀至少 4 個 component），保留目前副檔名與前兩層名稱條件。不要刪除或反轉失敗的斷言，也不要動 `_walk_files`／`_repo_sources`／`_iter_text_files` 來藏掉來源。
- 可驗收結果：`docs/workflows/x/p.md` 與本交接目錄的 Markdown 仍豁免；`docs/workflows/p.md`、其他位置的 Markdown 及同目錄可執行／設定檔不豁免。原有 walker 及 executable offender 斷言全保留。符合最終計畫 T-0 的內容豁免邊界及目前 AGENTS 的 gate 要求。

三項均有本次 smoke 的正式失敗證據。最小修復範圍可以限於上述三份測試檔的 fixture／gate helper，不需改 runtime、不需刪 node、改 smoke marker、skip／xfail 或放寬原契約斷言。Fable 交接須逐條記錄既有測試 fixture 的變更及行為理由；前幾輪已解除且未變的路徑沿用結論。

**驗證權限不因這份報告擴張。** 預定唯一一次 smoke 已用完；本輪沒有執行測試、collect、runtime／toy 探查，也沒有修改產品或測試。Fable 先完成已授權修復並交由 ASTRA 靜態核對；後續單 node 或第二次整包的追加驗證，由編排者依目前授權限制取得必要同意後安排。本報告只列必須達到的結果，不授權默默重跑。沒有動工前 suite 基線，不能把這 3 個失敗直接記成基線或宣稱完成。

本輪只新增本審核文件，未 commit/push。過往已揭露的流程偏差紀錄保留，不另改寫。

**Active Blocker：3。**
