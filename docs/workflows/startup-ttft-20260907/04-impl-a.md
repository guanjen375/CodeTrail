# startup-ttft-20260907 — Lane A 施工報告(需求 a:自檢通過後不重播進度 LOG)

- 施工者:Claude Opus 5.0(CLI 指定 `claude-opus-5`,effort max)。模型身分由主代理在 `00-model-log.md` 以 CLI init / modelUsage 核對。
- 日期:2026-09-07。worktree:`/tmp/codetrail-startup-ttft-20260907/worktrees/a`,base HEAD `f200f697ba54d38a102e8ef66dead652c4002e5f`(未 commit、未 push、未動 `.gitignore`)。
- Dependency:**無**。本 lane 不依賴 B / C / D 的任何符號;唯一與 Lane B 有名稱交集的是新增的契約測試
  `test_headless_run_never_primes_the_prompt_cache`,它用 `monkeypatch.setattr(..., raising=False)`,
  Lane B 落地前後都成立。
- 未對任何 live server 發請求、未動部署、未讀私人 session、未跑 smoke / full、未直呼 pytest。

## 1. 產品變更

### `client_preflight.py`

| 位置 | 變更 |
|---|---|
| `Preflight` | 新增 `warnings: list[str]`、`status: list[str]`;`note(message, *, keep=False)`;`banner_lines(*, tools, permission, compaction) -> tuple[str, ...]`。`lines` 的語意與內容不變(完整 transcript)。 |
| `note()` | `print(f"[aicode] {message}", flush=True)` **逐字不變**;`keep=True` 只多一個 `self.warnings.append(message)`。不新增 `warn()`、不加前綴、不重印。 |
| `banner_lines()` | `("自檢通過:model=<m> n_ctx=<n> tools=<k> permission=<p> compaction=<mode>", *status, *warnings)`。 |
| `_Tee` | 新增 keyword-only `on_line`;以**行**為單位(自行緩衝到 `\n`)把每個非空行逐字交出去,新增 `drain()` 交出尾端未換行的殘片。stdout 那份不帶 `on_line`,`captured` / `lines` 的計算不變。 |
| `run()` | stderr 那份 tee 帶 `on_line=result.warnings.append`;`finally` 先 `errors.drain()` 再算 `lines`。legacy web hint 改 `note(hint, keep=True)`。步驟順序、失敗路徑、簽名不變。 |
| `keep=True` 的五處 | `observe_n_ctx` profile fallback、`check_ctx_safety` UNKNOWN、`render_lessons` 過期 lessons、`_drop_rendered` 失敗、`run()` 的 legacy web hint。其餘 `note()` 一律不動。 |
| `compaction_status()` | 每一行 `note(line)` **之外**多一個 `result.status.append(line)`(全部行、順序不變、不做二次篩選)。 |
| docstring | 模組(§4.1 指的 15–18 行那段)、`_Tee`、`Preflight`、`run()` 四處同步:「完整 transcript 留在終端;TUI 只拿 `banner_lines()`」。 |

### `codetrail_chat.py`

`command_chat` 的 banner 改成
`checks.banner_lines(tools=len(engine.tool_specs), permission=engine.options.policy.name, compaction=compactor.mode)`,
並加註「失敗路徑不走這裡」。`CodeTrailApp` 的 `banner` 參數形狀(`Sequence[str]`)與 `command_run` 不變。

計畫與實際程式**沒有**不合之處;§4.1 的凍結介面逐字實作。

## 2. red-before-green(唯一一條)

node:`tests/test_client_cli.py::test_the_tui_banner_after_a_passing_preflight_drops_the_progress_log_and_keeps_warnings`

命令(本 lane 唯一被授權執行的測試命令,共執行**三次**:一紅兩綠 —— 第二次綠是把測試區段
註解移到正確位置之後的重跑,與 §5 的三列紀錄一致。由整合者於 Step 4 更正,原文誤寫「兩次」;
regression 未重跑):
`python3 scripts/run_tests.py tests/test_client_cli.py::test_the_tui_banner_after_a_passing_preflight_drops_the_progress_log_and_keeps_warnings`

**紅(產品未改,只有測試檔)**:

```
collected 1 item
tests/test_client_cli.py F                                               [100%]
=================================== FAILURES ===================================
tests/test_client_cli.py:438: in test_the_tui_banner_after_a_passing_preflight_drops_the_progress_log_and_keeps_warnings
    assert not progress, f"自檢進度 LOG 進了 banner:{progress}"
E   AssertionError: 自檢進度 LOG 進了 banner:['[aicode] root=/tmp/pytest-of-david/pytest-1419/test_the_tui_banner_after_a_pa0/project',
    '[aicode] deployment profile=defaults verification=unverified', '[aicode] model=/models/x.gguf',
    '[aicode] lessons:沒有 active lessons(已寫空的 .codetrail/lessons.md)', 'MODEL PASS — cached',
    '[aicode] 壓縮模式=manual(沒有 …/client.json;CodeTrail 未接管)', '[aicode] 跑 ./set_config.sh 選一次才會有自動壓縮',
    '[aicode] 舊回合 reasoning=不進模型(省 context;要保留就在 client.json 設 "keep_historical_reasoning": true)']
=========================== short test summary info ============================
FAILED tests/test_client_cli.py::test_the_tui_banner_after_a_passing_preflight_drops_the_progress_log_and_keeps_warnings
============================== 1 failed in 0.35s ===============================
```

是**行為紅燈**(進度 LOG 真的被重播進 banner),不是 fixture / 缺方法的錯。

**綠(產品改完,同一條、同一個命令)**:

```
collected 1 item
tests/test_client_cli.py .                                               [100%]
============================== 1 passed in 0.28s ===============================
[run_tests] PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /usr/bin/python3 -m pytest tests/test_client_cli.py::test_the_tui_banner_after_a_passing_preflight_drops_the_progress_log_and_keeps_warnings
```

## 3. 新增的 node(全部 `@pytest.mark.smoke`;除上面那條外**只寫不跑**)

| 檔 | node | 守什麼 |
|---|---|---|
| `tests/test_client_cli.py` | `test_the_tui_banner_after_a_passing_preflight_drops_the_progress_log_and_keeps_warnings` | 端對端:真的 `client_preflight.run()` + `command_chat`,擷取傳給 `CodeTrailApp` 的 `banner`。摘要行、stderr WARNING、壓縮狀態行都在;`[aicode]` / `root=` / `deployment profile=` / `MODEL PASS` 一行都沒有;進度 LOG 仍印在終端(`capsys`)。 |
| `tests/test_client_cli.py` | `test_headless_run_never_primes_the_prompt_cache` | B06:headless `run` 零預熱呼叫點(`Engine.prime_prompt_cache` 換成會炸的替身,`raising=False`),exit 0 且事件流形狀 `["session","text","step_finish"]` 不變。 |
| `tests/test_client_preflight.py` | `test_the_banner_keeps_stderr_warnings_and_compaction_status_but_drops_the_progress_log` | A2 / A3 / A4 集合斷言:banner[0] 逐字、`status == client_status.status_lines(n_ctx)` 且緊接在摘要之後、五個 keep 點與兩個 stderr 行都在且**依實際發生順序**、過期 lessons 的兩行是**一則**、進度行(9 種前綴)一個都不在、`lines` 仍是完整 transcript 且警告只出現一次;另外反向驗 `ctx safety=SAFE` 與 `n_ctx=…(來自主 server)` **不**進 warnings。 |
| `tests/test_client_preflight.py` | `test_keep_does_not_change_what_note_prints` | B07:`note("x")` 與 `note("x", keep=True)` 的 capsys 輸出逐字相同;多行訊息原樣一則進 `warnings`;`status` 不被 `note` 碰。 |

`tests/test_smoke_gate.py` 的 `SAFETY_MODULES` **未動**(整合者負責登記上面四條;檔名鍵
`test_client_preflight.py` / `test_client_cli.py` 都已存在,不需新增鍵)。

## 4. 動到的既有測試(逐條)

| 檔 | 位置 | 動作 | 理由 |
|---|---|---|---|
| `tests/test_client_preflight.py` | 模組 docstring 第 15–18 行 | 改措辭 | 契約變了:transcript 仍是逐字完整記錄,但「通過之後進 TUI 的」只有 `banner_lines()`。docstring 說的是本檔在守什麼,不改就是教錯的契約。**零斷言變更**。 |
| `tests/test_lessons.py` | `_render()` docstring(402–403 行) | 改措辭 | 同上:原文寫「tee 進畫面與對話區第一則」,現在對話區第一則不再是整段 transcript;過期提示是 banner 的警告之一。**零斷言變更、零行為變更**(該 helper 仍只用 capsys 收 `note` 的輸出)。 |

既有斷言、既有 node 名、共用替身(`_FakeMcp` / `_FakeMcpClient` / `_write_deployment` 等)一律未改;
`tests/test_client_cli.py` 新增了一個檔內 helper `_write_deployment`(新的,沒有覆寫任何既有名稱)。
`test_the_transcript_keeps_stderr_warnings` / `test_the_transcript_carries_the_compaction_status`
兩條照舊(`lines` 的計算路徑逐字不變)。

## 5. 已執行的命令與結果

| 命令 | 結果 |
|---|---|
| `python3 scripts/run_tests.py tests/…::test_the_tui_banner_…`(改產品**前**) | 1 failed(上面的紅燈節錄) |
| `python3 scripts/run_tests.py tests/…::test_the_tui_banner_…`(改產品**後**) | 1 passed |
| 同上(把測試檔的區段註解移到正確位置之後再跑一次) | 1 passed |
| `python3 -m py_compile client_preflight.py codetrail_chat.py tests/test_client_preflight.py tests/test_client_cli.py tests/test_lessons.py` | ok |
| `git status --porcelain=v1 --untracked-files=all` | 只有本 lane 的五個檔 ` M`,無 untracked |
| `git diff --binary > /tmp/codetrail-startup-ttft-20260907/patches/lane-a.patch` | 31113 bytes,sha256 `d25ebcccdbc9013d7faeafd43353511b476b3875cfad8c89ccc9c16814cf6aea`,只含上面那五個檔 |

`git diff --stat`:

```
 client_preflight.py            | 119 +++++++++++++++++++++----
 codetrail_chat.py              |  11 ++-
 tests/test_client_cli.py       | 143 ++++++++++++++++++++++++++++++
 tests/test_client_preflight.py | 193 ++++++++++++++++++++++++++++++++++++++++-
 tests/test_lessons.py          |   3 +-
 5 files changed, 445 insertions(+), 24 deletions(-)
```

`Tests: smoke only — reviewer owns full execution.`(本 lane 連 smoke 都沒跑:依本次 prompt,
只跑那一條 regression;smoke 由整合者、full 由審核者執行。)

## 6. 未做 / 偏離

1. **A8 的文件同步(`README.md:706-707`、`docs/setup.md:248`)沒做** —— 那兩個檔在 §6 是
   **Lane D** 的可寫清單,本 lane 的可寫檔不含它們。`README.md:707` 目前仍寫「的輸出會留在
   對話區第一則,所以 TUI 接管畫面之後仍然看得到」,需要 Lane D 改成「只留摘要 + 壓縮狀態行 +
   警告;完整輸出留在 TUI 之前的終端」。A8 的其餘四項(`client_preflight.py` 四處 docstring、
   `tests/test_client_preflight.py` 模組 docstring、`tests/test_lessons.py` 註解)已完成。
2. **沒有新增 `os.environ` 讀取、沒有 `process_env` 以外的 spawn**;`legacy_web_backend_hint()`
   既有的 `process_env.run(["tmux", …])` 原樣未動。
3. 沒有跑 smoke / full / 任何其他測試 node;`tests/test_smoke_gate.py` 未動(整合者 owned)。
4. §5 的驗收 B 系列(預熱)全部屬於 Lane B / C,本 lane 只出了 B06 在 `command_run` 這一側的
   契約測試(§6 Lane A 指定的那條)。
