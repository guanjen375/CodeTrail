# handoff-B(W1:移除 OpenCode)

- 基準碼 `a1682d5`;只動 `plan-final.md` §3 的 **B** 那一列。角色 developer。
- **本輪未執行任何 pytest**(B 不在 handoff-execution 的可執行名單內):不 full、不 smoke、
  不 collect-only、不單跑既有測試。新增的契約測試寫完未跑,留給交付前唯一一次 smoke。
  只跑了靜態檢查:`python3 -m compileall -q <本輪動到的檔>`(全綠)與 AST 掃描
  (「還有沒有 `import opencode*`」「還有沒有 `opencode` 字樣」)。
  `check_eval_consistency.py` / `check_readme_consistency.py` 依規定留給 D1 / D2;
  `ruff` 不在允許命令內,未跑(import 順序按 ruff isort 預設 `order-by-type`:
  常數 → 類別 → 函式,與該檔既有排法一致)。
- 刪除是**工作樹刪除,未 stage / 未 commit**(禁止 commit)。`git ls-files | grep -i opencode`
  在編排者 stage 之前仍會列出那四個路徑,這是預期的。

---

## 1. 落檔的介面

同 `plan-final.md` §2 的 I-8,無偏差。實際符號:

### `compaction_formula.py`
```python
COMPACTION_RESERVE_TOKENS = 20_000      # 原 UPSTREAM_COMPACTION_BUFFER
OUTPUT_TOKEN_MAX = 32_000               # 原 UPSTREAM_OUTPUT_TOKEN_MAX
MIN_PRESERVE_RECENT_TOKENS = 2_000      # 原 UPSTREAM_MIN_PRESERVE_RECENT_TOKENS
```
- **值與算式一字不變**:`effective_max_output`、`derive_settings` 的分支、下界判斷、
  `TOOL_RESULT_CONTEXT_FRACTION = 0.12`、`TAIL_TURNS = 1` 都沒動。
- 刪除:`combine_settings()`(`:301-354`)、`:51-53` 懸空註解(「最低 OpenCode 版本」)。
- docstring 改字:模組頭、`DerivedSettings`、`tail_holds_a_full_headroom_turn`、`_as_int`、
  `effective_max_output`、`derive_settings`、`canonical_block`、`rule_headings` ——
  去掉 `overflow.ts` / `compaction.ts` / `transform.ts` / `splitTurn` / JS plugin /
  `opencode_migrate.managed_values()` 的引用,改成「沿用 2026-09 定案公式(來源見 git 歷史)」。
- 保留未動:`RULES_DOC`、`doc_text_blocks`、`canonical_block`、`RULE_HEADING_COUNT`、
  `rule_headings`、`CompactionModeError`、`DerivedSettings.__slots__` / `as_dict`。

### `client_compaction.py`
```python
if config.CLIENT_MAX_OUTPUT_TOKENS_CAP != compaction_formula.OUTPUT_TOKEN_MAX:
    raise RuntimeError("CLIENT_MAX_OUTPUT_TOKENS_CAP 必須等於 compaction_formula.OUTPUT_TOKEN_MAX")
```
import 期 fail-loud 仍在原位(`:94-99`),只換常數名與訊息字串;模組 docstring 改字。

### `scripts/mcp_catalog.py`
```python
EFFECTIVE_CHARS_KEY = "catalog_effective_chars"
LEGACY_EFFECTIVE_CHARS_KEY = "opencode_effective_chars"
CatalogSnapshot.catalog_effective_chars: int          # 原 opencode_effective_chars
CatalogSnapshot.summary() -> {..., EFFECTIVE_CHARS_KEY: <int>, ...}   # 新輸出只寫新鍵
def effective_chars(summary: Mapping[str, Any]) -> int   # 先新鍵、退回舊鍵;兩者皆無 → CatalogError
```
`effective_chars()` 對非 int(含 bool)也 raise `CatalogError` —— 缺值不得靜靜當 0。

### `scripts/eval_tool_routing.py`
```python
from scripts.mcp_catalog import EFFECTIVE_CHARS_KEY, LEGACY_EFFECTIVE_CHARS_KEY, effective_chars, ...
_FROZEN_CATALOG_FIELDS                       # 拿掉 "opencode_effective_chars" 這一格
def _frozen_effective_chars_key(row) -> str  # row["era"] == "opencode" → 舊鍵名,否則新鍵名
assert_frozen_catalog_contract(snapshot, row)
#   ① 完整性:_FROZEN_CATALOG_FIELDS + 該 row 該有的那個鍵名,缺任一 → EvalError
#   ② 逐欄比對不變
#   ③ 有效字元數用 effective_chars() 兩邊取值再比,不符 → EvalError(訊息字串未變)
```
`_prepare_synthetic_knowledge` 的字面前綴 tuple → `process_env.STRIPPED_ENV_PREFIXES`。
`eval/fixtures/tool_routing/support_matrix.json` **零改動**(`era: "opencode"` 六臂與
`:101` 的舊鍵原樣保留);`arm_contract_digest` 的輸入不含這一格,沒有 digest 漂移。

### 刪除的檔
`opencode_migrate.py`(1,743 行)、`opencode_plugins/codetrail-compaction.js`、
`opencode_plugins/codetrail-notify.js`、`opencode_plugins/`(空目錄一併移除)、
`tests/test_opencode_migrate.py`(1,194 行)。

### 只改註解 / docstring(行為零改動)
`client_mcp.py:260-265, 413`、`client_policy.py:77`、`context_budget.py:11`、
`mcp_server.py:34`、`mcp_lease.py:604` —— 一律改成「舊世代前端」或指向
`process_env.STRIPPED_ENV_PREFIXES`,不再出現 `OPENCODE_` 字樣。

**改完之後 `opencode` 這個字在 B 的檔案裡只剩兩處**,都落在 §6.1 規劃的形狀豁免內:
`scripts/mcp_catalog.py:43`(`LEGACY_EFFECTIVE_CHARS_KEY = "opencode_effective_chars"`)、
`scripts/eval_tool_routing.py:392`(`row.get("era") == "opencode"`,同一行含 `"era"`)。

---

## 2. test-changes

### 2.1 `tests/test_opencode_migrate.py` —— 整檔刪除(63 個 node)

**共同理由**:被測模組 `opencode_migrate` 本版整個移除,這 63 條守的執行路徑在這一代
不存在;留著就是「測一個不存在的模組」= collection error,不是保護。它們守的行為
沒有被搬到別處(唯一還會寫使用者 OpenCode 設定的路徑改成 `docs/troubleshooting.md`
的「固定舊版 / 手動」人工程序,見 `plan-final.md` §6.2),所以也沒有等價的新測試。
以下逐條列出原 node 名與它守的行為(刪除前以 AST 取出,順序即原檔順序):

| # | node | 它守的行為 |
|---|---|---|
| 1 | `test_another_installs_takeover_is_left_alone` | 別份安裝寫的接管紀錄:零寫入只提示 |
| 2 | `test_a_moved_repo_is_not_mistaken_for_another_install` | 本 repo 搬過家不得被誤判成別份安裝 |
| 3 | `test_a_machine_that_never_took_over_is_untouched` | 沒接管過的機器零寫入 |
| 4 | `test_a_machine_without_any_opencode_config_is_untouched` | 沒有 OpenCode 設定的機器零寫入 |
| 5 | `test_only_values_we_still_own_are_restored` | 只還原現值仍等於我們寫入值的鍵 |
| 6 | `test_a_value_the_user_changed_after_takeover_is_left_alone` | 接管後被使用者改過的值保留 |
| 7 | `test_a_compaction_section_we_created_is_removed_again` | 我們建的 `compaction` 段要移除 |
| 8 | `test_only_our_plugin_entries_are_removed` | 只移除 path 對得上的 plugin 項 |
| 9 | `test_mcp_and_permission_are_never_touched` | `mcp.codetrail` 與 `permission` 一個字不動 |
| 10 | `test_custom_instructions_are_reported_not_moved` | 自訂 instructions 只報告不自動搬 |
| 11 | `test_a_backup_is_left_behind` | 寫入前留備份 |
| 12 | `test_the_state_file_is_deleted_only_after_the_config_was_written` | 設定寫成功之後才刪狀態檔 |
| 13 | `test_a_successful_migration_removes_the_state_file` | 成功遷移後狀態檔要消失 |
| 14 | `test_check_mode_writes_nothing` | `--check` 零寫入 |
| 15 | `test_a_state_file_for_another_config_is_refused` | 狀態檔綁單一 config |
| 16 | `test_a_leftover_compaction_plugin_alone_triggers_the_migration` | 只剩 plugin 項的機器也要遷移 |
| 17 | `test_a_same_named_plugin_from_elsewhere_is_still_not_ours` | 同名但指向別處的 plugin 不碰 |
| 18 | `test_the_custom_config_path_is_honoured` | `--config` 指定的目標才是被檢查的那份 |
| 19 | `test_the_config_keeps_its_permissions` | 含 API key 的設定不得被寫成 0644 |
| 20 | `test_a_state_file_that_cannot_be_removed_is_fail_loud` | 刪不掉狀態檔要 fail-loud |
| 21 | `test_an_unreadable_opencode_config_is_a_problem_not_a_no` | 讀不到設定 ≠ 不需要遷移 |
| 22 | `test_an_untrusted_ownership_state_file_is_a_problem_not_a_no` | 狀態檔在卻讀不了要報問題 |
| 23 | `test_the_managed_values_come_from_the_formula` | 受管值由 `compaction_formula` 推導 |
| 24 | `test_save_state_is_owner_only_and_atomic` | 狀態檔 0600 / 目錄 0700 / 原子寫入 |
| 25 | `test_save_state_refuses_a_symlink_target` | 寫入端拒 symlink 目標 |
| 26 | `test_save_state_refuses_a_symlinked_state_directory` | 寫入端拒 symlink 父目錄 |
| 27 | `test_load_state_is_fail_closed` | 沒有狀態檔 = 沒有接管 |
| 28 | `test_state_with_a_tampered_prior_is_rejected` | digest 必須涵蓋 `prior` |
| 29 | `test_load_state_ignores_a_symlinked_state_file` | 讀取端拒 symlink 狀態檔 |
| 30 | `test_load_state_refuses_a_world_readable_state_file` | 讀取端拒 world-readable |
| 31 | `test_load_state_refuses_a_group_writable_state_directory` | 讀取端拒 group-writable 目錄 |
| 32 | `test_state_dir_follows_home` | 狀態目錄跟著 HOME |
| 33 | `test_takeover_records_prior_values_and_registers_the_plugin` | 接管要記原值並註冊 plugin |
| 34 | `test_reapplying_the_same_mode_keeps_the_original_prior` | 重跑不得覆寫「接管前原值」 |
| 35 | `test_state_from_another_config_is_refused` | A 的紀錄不得拿去動 B |
| 36 | `test_native_restores_exactly_what_was_taken_over` | native 只還原接管過的東西 |
| 37 | `test_native_removes_a_section_codetrail_created` | native 移除我們建的段 |
| 38 | `test_native_keeps_an_empty_section_the_user_already_had` | 使用者原有的空段保留 |
| 39 | `test_native_leaves_values_the_user_changed_after_takeover` | 改過的值不還原 |
| 40 | `test_ownership_is_json_type_strict` | ownership 比對 JSON 型別嚴格相等 |
| 41 | `test_native_never_removes_a_plugin_entry_the_user_had_first` | 使用者先有的項不刪 |
| 42 | `test_native_keeps_an_entry_the_user_added_options_to` | 被加上 options 的項不再是我們的 |
| 43 | `test_plugin_entry_is_deduplicated_and_keeps_option_pairs` | plugin 項去重且保留 option pair |
| 44 | `test_plugin_entry_recognises_the_file_url_form` | `file:///…` 形式也算同一項 |
| 45 | `test_wrong_types_and_json_null_are_blocking_errors` | 型別錯 / JSON null 是阻斷錯誤 |
| 46 | `test_effective_drift_reports_every_managed_key_and_the_plugin` | drift 報告涵蓋每個受管鍵與 plugin |
| 47 | `test_effective_drift_is_silent_without_state` | 沒有狀態檔就不算漂移 |
| 48 | `test_prune_is_taken_over_and_restored_but_never_called_drift` | 受管鍵與契約鍵是兩組 |
| 49 | `test_native_leaves_a_prune_value_the_user_set_before_takeover` | 還原回使用者接管前的值 |
| 50 | `test_unmanaged_keys_names_what_an_older_state_file_never_took_over` | 舊狀態檔的缺口由 `unmanaged_keys` 報出 |
| 51 | `test_native_mode_flags_a_still_registered_plugin` | native 模式下仍註冊的 plugin 要被指出 |
| 52 | `test_state_digest_survives_an_integral_float_in_prior` | `1.0` 不得被判成竄改 |
| 53 | `test_config_identity_needs_both_hashes` | 身分要 path_hash + real_path_hash 兩個 |
| 54 | `test_a_moved_repo_converges_to_exactly_one_plugin_entry` | 搬家後收斂成剛好一筆 |
| 55 | `test_a_same_named_plugin_we_never_registered_is_not_hijacked` | 沒註冊過的同名項不劫持 |
| 56 | `test_native_keeps_a_pre_existing_plugin_without_calling_it_drift` | 接管前就有的 plugin 不算漂移 |
| 57 | `test_a_replaced_entry_is_not_hijacked_even_after_we_registered_once` | 註冊過 ≠ 現在那筆是我們的 |
| 58 | `test_switching_to_native_after_a_repo_move_removes_the_old_entry` | 搬家後切 native 要清掉舊那筆 |
| 59 | `test_a_native_baseline_is_recomputed_from_the_current_config` | native baseline 由現況重算 |
| 60 | `test_state_refuses_values_the_two_languages_serialise_differently` | 兩語言序列化不同的值一律拒絕 |
| 61 | `test_a_pre_existing_plugin_is_not_claimed_by_a_repo_move` | 搬家不得認領使用者原有的項 |
| 62 | `test_an_entry_we_added_after_a_move_is_still_ours_at_native` | 搬家後我們加的那筆 native 仍要還原 |
| 63 | `test_an_unverifiable_foreign_takeover_is_left_alone`(`@pytest.mark.smoke` + 3 參數) | 無法確認的外來接管零寫入 |

**同檔一併刪除的 fixture / helper / import / module mark**(不是 test node,但都隨檔消失,
D1 的 AST 對照會看到):
- module 層 `pytestmark = pytest.mark.smoke`(第 35 行)—— 全檔 63 條都是 smoke,
  所以刪檔等於 smoke 包少 63 個 node(其中第 63 條另有自己的 `@pytest.mark.smoke` decorator)。
- fixture:`home`(`@pytest.fixture()`)。
- 模組層 helper:`_derived`、`_config_path`、`_write_config`、`_take_over`、`_fresh_state`、
  `_owned_plugin_state`。
- import:`json`、`os`、`stat`、`sys`、`pathlib.Path`、`pytest`、
  `import opencode_migrate as cm`、`import opencode_migrate as migrate`、`REPO_ROOT` sys.path 注入。
- 參數化:`test_wrong_types_and_json_null_are_blocking_errors`(4 組)、
  `test_state_refuses_values_the_two_languages_serialise_differently`(6 組)、
  `test_an_unverifiable_foreign_takeover_is_left_alone`(3 組)。

### 2.2 `tests/test_compaction_formula.py`

| 測試 / 位置 | 動作 | 行為為什麼該變 |
|---|---|---|
| `test_combining_two_models_keeps_the_single_model_relationships` | **刪除** | 它是 `combine_settings()` 的唯一測試,而 `combine_settings` 的唯一消費者是被刪掉的遷移工具(`agent.compaction.model` 指到另一個模型是舊世代前端的設定形狀)。函式沒了,測試守的行為在這一代不存在 |
| `test_effective_max_output_matches_upstream_transform` | **改斷言**(4 處常數名)+ docstring 改字 | 常數改名,數字與關係式一字未動:`OUTPUT_TOKEN_MAX` / `COMPACTION_RESERVE_TOKENS` 就是原本那兩個值。名字必須跟著改,否則 `AttributeError`,不是行為改變 |
| `test_derive_settings_follows_the_upstream_formula` | 只刪兩行註解 / 改一行行尾註解 | 註解指向 `overflow.ts` 與已刪除的 `tests/test_opencode_migrate.py::test_the_managed_values_come_from_the_formula`;斷言與數字零改動 |
| `test_rule_headings_come_from_the_document` | docstring 改字(`plugin 的 summary_format` → `client_compaction` 的) | 做格式核對的是 Python 那一份,JS plugin 已刪除;斷言零改動 |
| 模組 docstring | 改字 | 刪掉「`combine_settings`」與指向已刪測試檔的段落 |
| 模組 mark / fixture / import | **未動** | `pytestmark = pytest.mark.smoke`、`_derived()`、`import client_status as status` / `compaction_formula as cm` 全部保留 |

註:`_derived()` 在 `a1682d5` 就已經沒有呼叫端(已核對 baseline),不是本輪造成的,故未刪。

### 2.3 `tests/test_client_compaction.py`

| 測試 | 動作 | 行為為什麼該變 |
|---|---|---|
| `test_the_output_constant_is_bounded_by_the_derivation_formula`(`:85`) | **改斷言**:`compaction_formula.UPSTREAM_OUTPUT_TOKEN_MAX` → `compaction_formula.OUTPUT_TOKEN_MAX` | 同一個常數改名,值仍是 32000;這條守的「實送 max_tokens 與門檻推導 max_output 是同一個數」完全不變 |
| 模組 docstring | 改字 | 「plugin 對 OpenCode 的膠水」→「JS plugin 對舊世代前端的膠水」 |
| 其餘 62 條 / mark / fixture | **未動** | — |

### 2.4 `tests/test_evals.py`

| 測試 / 位置 | 動作 | 行為為什麼該變 |
|---|---|---|
| `test_model_probe_endpoint_must_match_effective_opencode_provider` → `test_model_probe_endpoint_must_match_effective_profile` | **改名**(內容零改動) | 名字裡的 provider 是舊世代前端的設定概念;這條實際綁的是 deployment profile 的 `config.LLAMA_BASE_URL`。名字描述一個不存在的執行路徑會誤導下一個讀它的人 |
| `test_opencode_timeout_is_a_scored_case_failure_not_a_suite_abort` → `test_a_replay_timeout_is_a_scored_case_failure_not_a_suite_abort` | **改名** + **改 fixture 資料**:`cmd=["opencode", "run"]` → `cmd=["python3", "codetrail_chat.py", "run"]` | replay 跑的是我們自己的客戶端;`TimeoutExpired.cmd` 是這條假造的輸入,寫舊命令等於在描述一條不存在的 timeout 來源。斷言(timed_out / harness_error / terminal / assistant_text / 私密輸出不落檔)零改動 |
| `test_saved_baseline_reproduces_pre_t2_live_measurement_and_stays_measured`(`:562`) | **改斷言**:`catalog["opencode_effective_chars"] == 30_891` → `mcp_catalog.effective_chars(catalog) == 30_891` | 凍結資料仍是舊鍵,但讀取端從此只有一個。直接用字面鍵名的話,哪天資料改成新鍵這條會 `KeyError` 而不是照樣比對;數字 30,891 未變 |
| `test_frozen_contract_accepts_only_the_exact_saved_historical_catalog`(`:584`) | **改斷言 / 建構參數**:`opencode_effective_chars=saved[...]` → `catalog_effective_chars=mcp_catalog.effective_chars(saved)` | dataclass 欄位改名;值仍取自同一份凍結資料。其餘四個 `pytest.raises` 斷言零改動 |
| `test_the_effective_chars_field_survives_its_rename_across_the_frozen_data` | **新增**,`@pytest.mark.smoke` | 契約測試(無聲失敗風險):live 只寫新鍵、凍結列只有舊鍵,讀取端少了退回舊鍵那一段的話,凍結契約會拿兩個「都不存在」的欄位互比,報告照樣說「與歷史 baseline 相符」而那一格根本沒比。這條釘住:資料檔仍只有舊鍵、`summary()` 不再產生舊鍵、兩種拼法解到同一個數字、缺兩個鍵 fail-loud(不是 0)、值漂了仍 `EvalError`、歷史列少了舊鍵算契約不完整 |
| `_snapshot_from_frozen_row(saved)` | **新增 helper**(非 test node) | 新測試要把凍結 baseline 還原成 snapshot;與既有 `test_frozen_contract_…` 的建構相同,但不改既有那條的本文 |
| 模組 docstring | 改字 3 處 | 「不起 OpenCode / MCP 子行程」→「不起客戶端 / MCP 子行程」;「私人 OpenCode session-model eval lane」→「私人 session-model eval lane」;smoke 成員資格那句補上新增的第二條(否則那句話當場就過期) |
| `test_the_routing_eval_child_environment_is_stripped`(`:1927/1930` 的 `OPENCODE_API_KEY`) | **未動** | `inventory.md` B.4 明列保留:它證明殘留變數確實被剝掉,是 `OPENCODE_` 仍在 `STRIPPED_ENV_PREFIXES` 的守門人 |
| `:1641` 附近的歷史註解(「去 OpenCode 化之後 replay 跑的是我們自己的客戶端」) | **未動** | `tests/` 不在 OpenCode gate 範圍(`_iter_text_files` 跳過 `tests/`),而該句敘述仍為真;`inventory.md` B.3 把 tests/ 註解列為選做 |

### 2.5 新增的 smoke node(給 D1 登記 `SAFETY_MODULES` 用,全名)

```
tests/test_evals.py::test_the_effective_chars_field_survives_its_rename_across_the_frozen_data
```

---

## 3. 給其他 owner 的字

### D1(`tests/test_smoke_gate.py`、`tests/test_repo_consistency.py`、`AGENTS.md`)
1. `tests/test_smoke_gate.py`
   - 刪整個 `"test_opencode_migrate.py"` 鍵(`:565-609`,說明 + 節點 tuple)—— 檔案已不存在。
   - `"test_compaction_formula.py"` 的 node tuple 刪 `"test_combining_two_models_keeps_the_single_model_relationships"`(`:139`);同鍵其餘 node 名未變。
   - `"test_evals.py"` 的 node tuple:`"test_opencode_timeout_is_a_scored_case_failure_not_a_suite_abort"`(`:809`)
     → `"test_a_replay_timeout_is_a_scored_case_failure_not_a_suite_abort"`;
     **新增** `"test_the_effective_chars_field_survives_its_rename_across_the_frozen_data"`。
     說明字串若要補一句,建議:「凍結 baseline 的有效字元數改了鍵名,兩邊仍要真的在比對」。
   - `"test_client_compaction.py"` 的 node 名沒有變動(只改了斷言內的常數名)。
2. `tests/test_repo_consistency.py`
   - `_OPENCODE_ALLOWLIST` 依 §6.1 收斂後,B 的檔案不需要任何條目:
     `compaction_formula.py` / `client_compaction.py` / `client_mcp.py` / `client_policy.py` /
     `context_budget.py` / `mcp_server.py` / `mcp_lease.py` 現在 `opencode` 出現 **0 次**(已逐檔核對)。
   - `scripts/mcp_catalog.py` 與 `scripts/eval_tool_routing.py` 各剩 **1 行**,分別是
     `LEGACY_EFFECTIVE_CHARS_KEY = "opencode_effective_chars"` 與
     `return LEGACY_EFFECTIVE_CHARS_KEY if row.get("era") == "opencode" else EFFECTIVE_CHARS_KEY`
     —— 兩行都落在計畫寫的形狀豁免 `opencode_effective_chars|"era"` 內(第二行含字面 `"era"`)。
   - `_OPENCODE_ALLOWLIST` / `_OPENCODE_WHOLE_FILE` / `_ENVIRON_ALLOWLIST` / `core_only` 裡
     `opencode_migrate.py`、`opencode_plugins/codetrail-*.js` 三個鍵指向的檔案已不存在。
   - 三條 stub 測試(`test_the_opencode_plugin_stubs_are_inert`、
     `test_the_stub_gate_rejects_initialisation_side_effects`、
     `test_the_js_comment_stripper_is_lexically_aware`)與 `_assert_inert_stub` / `_strip_js_comments`
     現在會在讀 `opencode_plugins/…` 時 `FileNotFoundError`,依計畫刪除。
3. `AGENTS.md`(§6.3 已規劃,這裡給精確字串)
   - `:116` `compaction_formula.UPSTREAM_OUTPUT_TOKEN_MAX` → `compaction_formula.OUTPUT_TOKEN_MAX`
   - `:141` 「與 `UPSTREAM_*` 常數的單一真值」→ 「與 `OUTPUT_TOKEN_MAX` / `COMPACTION_RESERVE_TOKENS` /
     `MIN_PRESERVE_RECENT_TOKENS` 常數的單一真值」;同條的「**不得 import `opencode_migrate`**」整句刪
     (那個模組不存在了)
   - `:143` `UPSTREAM_OUTPUT_TOKEN_MAX` → `OUTPUT_TOKEN_MAX`
   - `:174` 那一整條(`MANAGED_COMPACTION_KEYS` / `CONTRACT_COMPACTION_KEYS`)隨 ownership 狀態檔條刪除

### D2(`README_DEV.md`、`docs/*`)
- `README_DEV.md:179` 提到 `opencode_migrate.MANAGED_COMPACTION_KEYS` / `CONTRACT_COMPACTION_KEYS`
  的 bullet 要刪(§6.3 已列),那個模組已不存在。
- 文件若要點名常數,新名字是 `COMPACTION_RESERVE_TOKENS` / `OUTPUT_TOKEN_MAX` /
  `MIN_PRESERVE_RECENT_TOKENS`(值 20000 / 32000 / 2000,未變)。
- 升級段(§6.2)要寫的 plugin 完整路徑仍是
  `<原安裝路徑>/opencode_plugins/codetrail-notify.js` 與 `…/codetrail-compaction.js` ——
  本版已不再附帶這兩個檔,路徑字串只出現在文件裡。
- eval 報告的欄位名:新產出的 `catalog.summary()` 寫 `catalog_effective_chars`;
  歷史 `support_matrix.json` 仍是 `opencode_effective_chars`(資料檔零改動)。

### C1(`scripts/doctor.py`、`config.py`)
- 刪檔當下唯一還 import 它的是 `scripts/doctor.py`
  (`check_legacy_opencode_install` 裡的 function-local `import opencode_migrate` 與 `:1312` 呼叫點),
  屬 `plan-final.md` §4 預期的 W1 跨 owner 斷點。**交付前重新掃過:C1 已在共用工作樹落檔,
  該函式與呼叫點都不在了**,repo 內 `import opencode*` 的 AST 掃描結果為空,無待辦。
- `config.py` **不需要**為這次改名做任何事:`config.py` 全檔沒有 `UPSTREAM_` 字樣
  (`:406` 的註解指的是 `compaction_formula.effective_max_output`,函式名未改)。
  inventory C.2 的「`config.py:406` 常數名同步」這一項因此是零改動,請 C1 據此回報。

### C2(`scripts/set_config.py`)
- `:2306` 那句使用者可見字串仍寫著 `python3 opencode_migrate.py`;該檔已不存在,
  依 §6.2 改成指向 `docs/troubleshooting.md` 的升級段。
- `set_config` 沒有用到 `compaction_formula.combine_settings`(已核對,只用 `derive_settings` /
  `CompactionModeError`),常數改名對它沒有影響。

---

## 4. 未完成 / 疑點

1. 未執行任何測試(依 handoff-execution:只有 S 可跑,而且只跑它那兩條 regression)。
   `tests/test_evals.py::test_the_effective_chars_field_survives_its_rename_across_the_frozen_data`
   與所有改動過的既有測試都**沒有跑過**,只做過靜態編譯與 AST 核對。
2. 刪檔只在工作樹,未 stage;驗收條 §7(b) 的 `git ls-files | grep -i opencode` 要等編排者 stage 之後才會成立。
3. `compaction_formula.DerivedSettings.as_dict()` 在刪掉 `combine_settings` 與那條測試之後
   已無任何呼叫端。**沒有刪**:不在 B 的計畫範圍,而且它是無副作用的兩行 accessor;
   要刪的話請當成獨立決定。
4. `tests/test_compaction_formula.py::_derived()` 在 `a1682d5` 就已無呼叫端(非本輪造成),未動。
5. 施工機器的 `~/.config/opencode`、`~/.config/codetrail/compaction.json` **零讀寫**:
   本輪沒有執行 `opencode_migrate.py`、沒有讀任何 `~/.config/*`、沒有起任何服務。
6. `ruff` 未跑(不在允許命令內)。新增的 `from scripts.mcp_catalog import (...)` 名單按
   ruff isort 預設的 `order-by-type`(常數 → 類別 → 函式)插入,與該檔既有排列一致;
   若 D1 / Astra 那邊有跑 lint,這是唯一可能被挑的格式點。
