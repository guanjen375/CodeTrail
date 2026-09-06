# 工作者統一說明(給每一個 Opus 實作 CLI 與 Fable 修復者)

真值只有一份:`plan-final.md`(介面 §2、owner §3、DAG §4、測試 §5、文件 / gate §6、驗收 §7)。逐檔歸宿看 `inventory.md`(最終修訂)。初稿與 Astra 審核只供背景。

## 你是誰、能動什麼
- 你的 ID 是編排者在 prompt 裡給的(S / B / C1 / C2 / C3 / D2 / D1)。**只動 `plan-final.md` §3 你那一列的檔案**,含測試與 fixture;需要別人的檔改字,寫進你的 handoff,不要自己動。
- 介面名稱照 §2;非改不可時在 handoff 的「介面偏差」段列出舊名 → 新名與理由,下游以 handoff 為準。
- 不要 `git commit`、不要 `git push`、不要 stash / checkout 別的 commit。工作樹保持可 review。

## 測試權責(AGENTS.md §1;本輪沒有 `ROLE=REVIEWER`)
- 只有 S 在 W0 可以跑測試,而且只跑自己新寫的兩條 regression:先在**未改實作**上 `python3 scripts/run_tests.py tests/test_client_app.py::<node>` 取紅,貼節錄進 `handoff-S-red.md`;W1 改完實作後同一 node 單跑取綠,貼進 `handoff-S.md`。
- 其餘所有人:**不跑任何 pytest**(不 full、不 collect、不單跑既有測試、不 `--lf`)。新契約測試寫完不跑,交付前唯一一次 smoke 由編排者執行。
- 允許的靜態命令:`python3 -m compileall -q .`、`python3 scripts/check_eval_consistency.py`、`python3 scripts/check_readme_consistency.py`(D1 / D2 收尾時)。
- 禁止碰真服務與真設定:不跑 `~/start.sh`、`scripts/launch_servers.py`(除 `--help` 與 dry-run 進 tmp HOME 的測試)、`stop_servers.py`、`check_status.py`、`doctor.py`(`--help` 除外)、不讀寫 `~/.config/*`、不動 tmux。契約一律 tmp HOME + fake tmux / nvidia-smi / ss / 假 llama-server。
- 沒有動工前基線;`.pytest_cache` 不是基線。

## 順序(同時最多 3 個 CLI)
W0 S-red → W1 S ‖ B ‖ C1 → W2 C2 ‖ C3 ‖ D2(C2 / C3 等 `handoff-C1.md` 宣告 I-5 / I-6 落檔)→ W3 D1 → Astra 靜態審核 → Fable 修復至 Blocker=0 → 編排者跑唯一一次 `python3 scripts/run_tests.py -m smoke` → Astra 核對結果與最終 diff。

## 交付:每人一份 `docs/workflows/session-opencode-cleanup/handoff-<ID>.md`
固定四段,寫短:
1. **落檔的介面**:實際符號名與簽名(與 §2 相同就寫「同 §2」;不同就列偏差)。
2. **test-changes**:逐條 `檔 | 測試 / fixture 名 | 新增 / 改名 / 改斷言 / 刪除 | 行為為什麼該變`。理由不得是「這樣才會綠」。新測試要標 `@pytest.mark.smoke` 的就寫明,D1 據此登記 `SAFETY_MODULES`。
3. **給其他 owner 的字**:你不能動的檔要改哪一段、改成什麼(例如 B 給 D2 的 docs 段落、C1 給 D1 的 allowlist 條目)。
4. **未完成 / 疑點**:一行一項;沒有就寫「無」。
交接檔一律寫 `python3`(docs 有 `python3` 契約);不要貼 NDA 內容或真實路徑以外的機器資訊。

## 給 Fable 修復者
- 只修 Astra 對 `a1682d5` 之後本任務改動提出的 Blocker;不擴張到未動的碼;ID / 驗收條件不得改。
- 修 Blocker 時若涉及真實 bug,照 §1.3 紅綠(單跑那一條);契約不另跑。
- 同一分歧兩輪以上仍未解才寫進 `deferred.md`,並在回報裡明說。
