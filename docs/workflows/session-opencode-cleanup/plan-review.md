model: `gpt-6-astra` / effort: `max`

reviewed commit: `595b9f9`（第 1 階段凍結的初步計畫與附件）；base code: `a1682d5`。

本輪已完整讀取 `AGENTS.md`、`plan-initial.md`、`inventory.md`、`deferred.md`，並靜態對照下列程式路徑。只審計畫將新增、刪除或改接的行為；不對未變動的程式碼另開修復範圍。使用者最新的完整移除要求取代退休 migrator／stub 的保留要求，仍有效的 session、ownership、durable state 與子行程機密清理要求繼續適用。

未執行任何測試、未讀真實 session 或使用者 OpenCode 設定，未修改 runtime、tests、設定或 `AGENTS.md`，未 commit。本文件的失敗推論是靜態證據，不是已執行的紅燈；沒有把 cached `lastfailed={}` 當成測試基線。

**B-01 — 重播的資料來源無法滿足「完整已存歷史」。**

計畫位置：I-2（105–110 行）、§3.1（205、213 行）、§6／§7a（342、351 行）、D-a3（388 行）。

觸發與證據：一個已做過壓縮的 session 重新接續。`client_engine.py:488–513` 的 `replace_history()` 是追加一筆 compaction，壓縮前的原始 message 記錄仍在檔案中；`client_engine.py:632–653` 的 `resume()` 則刻意以最後一筆 compaction.history 取代模型歷史。計畫指定只重播 `engine.messages`，因此第一次壓縮以前的問答、reasoning、工具輸出會從畫面消失。D-a3 又把顯示原文列為不做／可擱置，與同一份計畫承諾的完整已存歷史、session 檔順序不相容。

違反：§7a 的五條接續路徑驗收，以及 `AGENTS.md` §2 對「送模轉換與畫面／session 原文分離」的保留要求。這不是要求把舊原文重新送進模型。

最小計畫修正：明定畫面歷史與模型歷史的不同資料來源；畫面使用受既有 store 防線保護的原始記錄，保留原始問答、工具與 reasoning，壓縮摘要另有清楚標示；模型仍使用最後 compaction 加 tail。定義多次 compaction 的順序與 tail 不重複呈現方式，撤回 D-a3 的預先擱置。讀取／整理重播資料須納入切換失敗原子性，不能在換掉 engine 或清空舊畫面之後才因補讀原文失敗留下半套狀態。把「壓縮前原文仍可見、送模歷史仍已壓縮」納入既定 smoke 契約。

**B-02 — 以全歷史 call ID 查輸出，會把較新的工具結果貼到較舊的呼叫。**

計畫位置：I-2（99–110 行）、§3.1 的 widget 對映（205 行）、§7a（352 行）。

觸發與證據：同一個持久化 session 在兩次程式啟動中各產生一次 fallback `call_1`，之後再接續。`client_engine.py:1391–1403` 的 fallback ID 只保證全行程遞增，重新啟動會從 1 開始。計畫要求歷史 ToolBlock 的輸出重用現行 `_tool_output(call_id)`；但 `client_app.py:565–577` 從整段 `engine.messages` 反向找到同 ID 的最後一筆結果即回傳，兩個歷史 block 因而都顯示後一次的 content／structured。較早的懸空呼叫也可能被後來同 ID 的結果錯標為已完成。不把重播 block 放進 `_tools` 只解決即時 widget 的碰撞，不能解決這個歷史配對錯誤。

違反：§7a 的完整工具輸出、原始順序與 pending 狀態驗收。

最小計畫修正：歷史呼叫與結果以各次宣告的位置／所屬區段配對；重播使用已配好的 `HistoryEntry.output`／`structured`，共用格式化邏輯即可，不再對完整 engine 歷史做僅憑 ID 的反查。增加同一 session 內重複 ID 與舊 pending 遇上較新同 ID 結果的契約情境。修正限於新重播路徑，不要求順帶改既有 fallback ID 產生器。

**B-03 — 刪除 Python 的 env 讀取仍未切斷 llama／tmux 的 shell 設定來源。**

計畫位置：I-3／I-4（117、141、160 行）、I-6（169 行）、§3.3（244、259–260 行）、D-c2（393 行）。

觸發與證據：deployment 中某個 role 沒有 gpu，啟動 shell 或既有 tmux server 帶有 `CUDA_VISIBLE_DEVICES=1`。`deployment_profile.py:946–948` 在 gpu 為空時不加任何環境前綴；計畫改用的 `process_env.child_env()`（`process_env.py:40–59`）只剝四個指定前綴，仍會保留 CUDA selector。tmux 路徑更明訂維持 `subprocess.run` 原樣：`scripts/launch_servers.py:288–320` 建 window、respawn 均未指定乾淨環境，command 也沒有最終清理。tmux 的本機手冊「GLOBAL AND SESSION ENVIRONMENT」明載新 process 繼承合併後的 server／session 環境，因此只清理 launcher process 也不能排除既有 daemon 保存的值。

同一缺口也涵蓋 launcher 原樣繼承的 `OPENCODE_*` 機密，以及 llama 自己接受的設定。補充靜態證據：本機 `/home/david/llama.cpp/common/arg.cpp:723–748` 先套環境值再套 argv，`:1456–1465` 接受 `LLAMA_ARG_THREADS`；當 profile 未輸出 `-t` 時，這仍是 shell 可以改變實際 server 設定的入口。這些問題無法由「有效 DeploymentProfile 不變」或 dry-run 字串相同證明已排除。

違反：目標 c、§6 新啟動核心條款的「設定只來自檔案／常數／argv」，以及仍適用的子行程機密剝除要求。

最小計畫修正：把 launcher、直接 `deployment_profile exec` 與 tmux 最終 pane／llama process 的環境邊界一併納入 C1／C3。明定 legacy CUDA 輸入與 llama 設定 env 的清理，只有驗證過的 gpu 才重新輸出 selector；缺 gpu 時應真正沒有繼承的 selector。四個機密／設定前綴在實際子行程仍須被剝除，且不能靠修改使用者共用 tmux 的全域設定完成。契約要觀察實際交給子行程的環境，涵蓋已有 tmux 環境的情境；可以用離線替身，不需要真 GPU／模型。

**B-04 — 新升級指引一條會越過 ownership，另一條不能完成它承諾的路徑清理。**

計畫位置：§3.2（225–234 行）、§6（341 行）、§7b（366、370 行）。

觸發與證據一：使用者已自行改過 compaction 值，或仍保有另一份安裝的 plugin。完全手動路徑只按檔名 suffix 刪 plugin，再把 `managed.<key>.prior` 無條件寫回，最後刪掉 ownership 檔。這會覆蓋使用者後來的選擇、移除其他 checkout 的同名 plugin，並刪掉其還原證據。原實作的保護並非只看 prior：`opencode_migrate.py:789–804` 要求 current 與 recorded value 的 JSON 型別嚴格相等；`:847–850` 驗 config binding；`:498–526` 驗含 prior 的 digest；`:1422–1444` 限定完整 plugin 路徑；`:1521–1533` 拒絕別份／無法確認的 owner。把操作搬進文件並沒有讓 ownership 變得不需要。

觸發與證據二：升級後 `opencode.json` 只剩原 checkout 的 `codetrail-notify.js` 路徑，沒有 compaction ownership 檔。在 `/tmp/codetrail-legacy` 執行固定舊版工具時，`:1422–1444` 只匹配該臨時 worktree 的 `PLUGIN_DIR`，無法移除指向原 checkout 的 notify 項；`:1447–1459` 兩個殘留掃描器都走這條。工具可能回報無需變更，但使用者仍保留已不存在的 plugin。這是新文件選錯執行位置造成的結果，不是要求修改已退休的 migrator。

違反：§3.2 自己承諾的 ownership 語意、完整升級指引驗收，以及仍適用的使用者設定／別份安裝／durable ownership state 保護。使用者授權刪除 repo 整合，不等於授權文件無條件還原其他設定。

最小計畫修正：重寫兩條升級路徑。固定舊版操作必須交代原安裝路徑的身分限制，不能把任意臨時 worktree 當成等價的一次性清理；手動路徑只處理已確認屬於被移除安裝的完整路徑，還原值須有可信、綁定正確 config 的 ownership 證據且 current 仍相等，無法證明就保留。取消按 suffix 全刪、無條件套 prior 與無條件刪 state。驗證指引只用隔離的合成設定，不在施工機器上執行真遷移。

**B-05 — 模型解析函式改名漏了 import 期必走的 config 呼叫端。**

計畫位置：I-3（141 行）、§4 B1／C1（273–274 行）、`inventory.md` 的 config 歸屬。

觸發與證據：C1 依 I-3 刪除／改名 `resolve_main_model_from_env`，B1 依 owner 欄只改 `config.py` 註解。`config.py:139` 的 `_resolve_main_model()` 仍呼叫舊函式，`:143` 在 import 期立即執行它，`:148` 的 `require_main_model()` 也仍呼叫舊名。I-3 列出的五個同步消費者沒有 config，其他 task 也未獲分配這兩個程式呼叫；照表施工會在 import config 時得到 `AttributeError`，TUI／MCP 與大量測試都無法正常載入。`compileall` 不會檢查這種失接。

違反：I-3 改名介面、§4 每檔 owner 契約與可啟動／smoke 驗收。

最小計畫修正：把兩個 config 呼叫列入必要改名，指定 `config.py` 的單一 owner，另一 task 的註解修改透過交接處理；同步修正 inventory。交接檢查須枚舉所有舊 callable 的引用，而不是以目前五個消費者清單為完整範圍。

**B-06 — 在共用 walker 剪掉整個交接樹，會一起關掉該處程式的安全 gate。**

計畫位置：§0（22 行）、I-7（173–175 行）、T-0（270 行）、`inventory.md:131`。

觸發與證據：T-0 依計畫在 `_walk_files()` 剪掉 `docs/workflows` 後，該目錄內的 `.py`／`.sh`／設定檔也不再被列舉。`tests/test_repo_consistency.py:1239–1267` 顯示 `_repo_sources()` 與 `_iter_text_files()` 共用這個 walker；`:1652–1658`、`:1705–1711` 的正式 env／spawn 檢查均依賴其結果。於此處放入帶 `import subprocess`、環境設定讀取或 OpenCode runtime 依賴的程式，正式 gate 將完全看不到。只在 Markdown 寫反例並確認不報錯的 T-0 測試，無法守住這個退化。

違反：`AGENTS.md` §2／§3 的安全檢查點不得刪弱、子行程環境與新增 env 讀取守門要求；I-6 與 §7c 的收緊 gate 目標。

最小計畫修正：豁免限於固定交接目錄中用作交接的 Markdown 內容，不在共用 walker 排除整棵可容納程式碼的樹。相同路徑內的程式／設定檔仍走原有檢查。T-0 的 smoke 自測同時守住「交接 Markdown 可通過」和「同目錄的違規程式仍由正式 gate 拒絕」，並登記必要 node。

**B-07 — 新 OpenCode allowlist 排除了仍保留的反向檢查器，驗收會互相打架。**

計畫位置：I-6（168 行）、§7b 的 Python 命中清單（364 行）、`inventory.md:126–128`。

觸發與證據：I-6 與 inventory 的最終 `_OPENCODE_ALLOWLIST` 都沒有 `scripts/check_readme_consistency.py`，但 inventory 同時要求保留這個檔案對 `opencode-ai`／`npm`／`opencode_migrate` 的合法 negative-pattern 豁免。現有 `scripts/check_readme_consistency.py:199` 確實含有必要的 `npm install -g opencode-ai` 檢查字串。`tests/test_repo_consistency.py:1428–1432` 在檔案不屬 allowlist 時先報違規，之後才看 shape exemption，所以只留下 exemption 沒有效果。§7b 又只准三個 Python 檔命中，與保留這個反向檢查器同樣矛盾。

違反：I-6 的完整移除 gate 決策、§7b 命中清單及 §7c 靜態檢查／smoke 綠燈驗收。

最小計畫修正：在計畫、inventory 與 grep 驗收中一致列出反向檢查器的必要例外，限定為檢查被移除介面的字面 pattern；仍拒絕真 import／執行／設定依賴。不要用整檔豁免或刪除防回歸檢查解決自相矛盾。

**B-08 — 驗收執行安排超出目前測試／提交權責，也未隔離真服務操作。**

計畫位置：§0／R-1 的「交接檔與 T-0 一起提交」（22、396 行）、T-0「自測綠」（270 行）、§5.4（333 行）、§7c（375 行）。

觸發與證據：§5.4 指派編排者另在 `a1682d5` 取得動工前測試基線，但本輪沒有 `ROLE=REVIEWER` 或額外基線測試授權；developer 只可跑自己新寫 bug regression 的 red／green 與交付前一次 smoke。交接文件的 commit 授權也不涵蓋 T-0 的測試程式碼修改。另，§7c 所列真實 `~/start.sh status --strict` 會進 `scripts/check_status.py:107–112` 的線上 `/health`／`/props` 路徑，`~/start.sh stop` 會進 `scripts/stop_servers.py:240–255` 的真 tmux kill-session；單把舊變數設進 shell 並不能使這些命令變成離線、無副作用的驗收。

違反：`AGENTS.md` §1.2、§3 的未確認修改不得 commit、§4 預設離線，以及本次工作沒有授權修改真實使用者部署的邊界。

最小計畫修正：刪除額外基線執行指派，明記目前沒有已實測基線，不以 pytest cache 推定；T-0 與一般新契約的執行證據統一留到 D1 唯一一次 smoke，gate 的反例由測試內預期拒絕，不能把單獨執行 gate 當成測試政策例外。程式碼 commit 仍等待使用者確認，交接文件可依既有授權獨立提交。把 status／stop 的驗收限定為暫存 HOME、合成 deployment／proc／snapshot 與 fake tmux／nvidia-smi／signal 的隔離契約，不直接操作真 `~/start.sh` 或真服務；唯讀 `doctor --no-network` 可以保留。全程不執行 full。

Blocker 數量：**8**。
