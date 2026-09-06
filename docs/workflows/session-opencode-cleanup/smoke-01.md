交付前第 1 次整包 smoke 已執行，結果未通過。

命令：`python3 scripts/run_tests.py -m smoke`。HEAD：`ea6a1b613ee1bd34369fd92591f3cdc94a281fa5`；產品改動仍在工作樹，基準碼 `a1682d5`，diff SHA256：`9a078521198130df68f704231fad73354baee1fa140d60e2a00d6a7dadb9755d`。

實際選取 2395 條／41 個檔／16 shards。**2392 passed、3 failed、0 errors、0 skipped；runner exit 1。** 不是 0 collected。各 shard 報告耗時總和 111.32s，最長 shard 66.14s；總和不是牆鐘時間。本次沒有 full，也沒有動工前 suite 基線；不能把 3 個失敗視為基線或忽略。

| 失敗 node | 原始紅燈節錄 |
|---|---|
| `tests/test_server_scripts.py::test_stop_and_status_use_argv_and_constants_not_the_shell` | `assert stop.returncode == 0`；子行程在 `scripts/stop_servers.py:364` 取 `row.used_gpu_memory`，得到 `AttributeError: 'GpuProcess' object has no attribute 'used_gpu_memory'` |
| `tests/test_doctor.py::test_explicit_gate_and_implicit_diagnostic_are_separate` | `AssertionError: assert 2 == 0`；stderr `找不到主模型(deployment profile 沒有 main.model),且呼叫端未指定模型` |
| `tests/test_repo_consistency.py::test_the_handoff_markdown_exemption_is_content_only` | `assert not _handoff_markdown(Path("docs/workflows/p.md"))` 得到 `AssertionError: assert not True` |

完整 stdout 留在本機 `/tmp/codetrail-session-opencode-cleanup-20260906/smoke.stdout`；命令／HEAD／diff 與 stdout hash／失敗 node 集合在 `smoke-once.request.json`。測試完成後，58 個產品路徑與審核快照一致，未有來源變動。

接續由 ASTRA 只做集中靜態核對，Fable 修復。已使用交付前允許的一次 smoke；後續測試安排必須遵守 AGENTS §1.2 與 plan-final §5.4，不默默重跑。本檔不宣稱整體完成或 Blocker 0。
