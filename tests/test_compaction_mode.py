"""壓縮模式狀態與受管值的決定性契約(docs/compaction-rules.md)。

這裡守的都是「壞掉不會有人發現」的東西:

  * 受管值的推導公式 —— 算錯只是門檻不對,壓縮照樣會發生,沒有任何錯誤訊息。
  * ownership 紀錄 —— 記錯就是切回 native 時把 CodeTrail 自己寫的值當成
    使用者原值還原回去(或反過來,把使用者的值刪掉),而 opencode.json 裡
    沒有任何欄位能事後分辨。
  * 狀態檔綁定的 config 身分與 digest —— 不核對就會拿 A 設定的接管紀錄去
    改 B 設定,而且被手改過的 `prior` 會原樣被信任。
  * owner-only 狀態檔的權限與 symlink 防線 —— 放寬就是把接管紀錄交給別人寫。
  * 「沒有狀態檔 = 沒有接管」的 fail-closed 預設 —— 弄反的話舊安裝
    git pull 之後會突然多一個壓縮 plugin。
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import compaction_mode as cm  # noqa: E402

pytestmark = pytest.mark.smoke


def _derived():
    return cm.derive_settings(context_limit=131072, output_limit=8192)


# ---------------------------------------------------------------------------
# 1. 受管值推導
# ---------------------------------------------------------------------------
def test_derive_settings_follows_the_upstream_formula():
    """131072 / 8192 是本機實際的 limit;數字寫死才看得出公式被改過。"""
    derived = cm.derive_settings(context_limit=131072, output_limit=8192)
    assert derived.usable == 131072 - 8192              # overflow.ts usable()
    assert derived.tool_result_budget == 15728          # floor(131072 * 0.12)
    assert derived.headroom == 15728 + 8192
    assert derived.idle_threshold == 122880 - 23920
    assert derived.tail_cap == 98960 - 8192
    assert derived.preserve_recent_tokens == 23920
    assert derived.tail_holds_a_full_headroom_turn is True
    assert derived.config_values == {
        "auto": False,
        "tail_turns": 1,
        "preserve_recent_tokens": 23920,
    }


def test_derive_settings_uses_limit_input_and_reserved_when_present():
    """有 limit.input 時 usable 走另一條分支,reserved 才會被扣掉。"""
    derived = cm.derive_settings(
        context_limit=131072, output_limit=8192, input_limit=100_000
    )
    assert derived.reserved == 8192                     # min(20000, 8192)
    assert derived.usable == 100_000 - 8192
    explicit = cm.derive_settings(
        context_limit=131072, output_limit=8192, input_limit=100_000, reserved=20_000
    )
    assert explicit.usable == 100_000 - 20_000


def test_preserve_recent_tokens_is_capped_by_a_derived_budget_not_a_percentage():
    """tail 上限是「門檻 − 一次摘要輸出」,不是 context 的固定百分比。

    小 context 的模型會落在 tail_cap 這一側:此時 tail 裝不下一整個
    headroom 大小的回合,呼叫端要看得到這件事(而不是以為有保證)。
    """
    derived = cm.derive_settings(context_limit=32768, output_limit=8192)
    assert derived.tail_cap == derived.idle_threshold - derived.output_limit
    assert derived.preserve_recent_tokens == min(derived.headroom, derived.tail_cap)
    assert derived.preserve_recent_tokens == 4260
    assert derived.tail_holds_a_full_headroom_turn is False
    # 壓縮完之後「摘要 + tail」必須仍低於門檻,否則下一次 idle 立刻再壓一次
    assert derived.preserve_recent_tokens + derived.output_limit <= derived.idle_threshold


def test_combining_two_models_keeps_the_single_model_relationships():
    """合併值不是逐欄取 min/max 拼出來的,同一組關係式必須仍然成立。

    `limit.output` 不同時逐欄取值會讓 tail_cap 算得比實際寬:主模型 32768/8192
    配 131072/32000 的摘要模型,一份 25K 的合法摘要就已經塞不回主模型,而
    `compaction.auto=false` 已經把上游的自動回復關掉了。這種組合必須被拒絕。
    """
    live = cm.derive_settings(context_limit=65536, output_limit=8192)
    summariser = cm.derive_settings(context_limit=1048576, output_limit=8192)
    combined = cm.combine_settings(summariser, live)
    assert combined.headroom == combined.tool_result_budget + combined.output_limit
    assert combined.tail_cap == combined.idle_threshold - combined.output_limit
    assert combined.preserve_recent_tokens == min(combined.headroom, combined.tail_cap)
    # 壓縮完之後「摘要 + tail」仍要低於門檻 —— 摘要那一份用的是較大的 output。
    assert (
        combined.preserve_recent_tokens + combined.output_limit
        <= combined.idle_threshold
    )
    assert combined.idle_threshold <= live.idle_threshold
    assert combined.preserve_recent_tokens <= live.preserve_recent_tokens

    # 兩個模型相同時逐欄與單一模型完全一樣。
    assert cm.combine_settings(live, live).as_dict() == live.as_dict()

    # 摘要模型的 output 大到 tail 不存在 → fail-loud,不給一個算不出來的值。
    with pytest.raises(cm.CompactionModeError):
        cm.combine_settings(
            cm.derive_settings(context_limit=131072, output_limit=32000),
            cm.derive_settings(context_limit=32768, output_limit=8192),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"context_limit": 0, "output_limit": 8192},
        {"context_limit": 131072, "output_limit": -1},
        {"context_limit": 131072, "output_limit": True},
        {"context_limit": "131072", "output_limit": 8192},
        {"context_limit": 8192, "output_limit": 8192},
    ],
)
def test_derive_settings_refuses_unusable_limits(kwargs):
    with pytest.raises(cm.CompactionModeError):
        cm.derive_settings(**kwargs)


def test_effective_max_output_matches_upstream_transform():
    """`min(limit.output, 32000) || 32000` —— 包含 `|| 32000` 那一段。

    照抄很重要:`limit.output` 是 0 或很大時,上游算出來的有效輸出額跟
    `limit.output` 本身完全不同,門檻也就跟著不同。
    """
    assert cm.effective_max_output(8192) == 8192
    assert cm.effective_max_output(65536) == cm.UPSTREAM_OUTPUT_TOKEN_MAX
    assert cm.effective_max_output(0) == cm.UPSTREAM_OUTPUT_TOKEN_MAX
    # 大 output 的模型:usable / reserved 都要用 cap 過的值
    derived = cm.derive_settings(context_limit=200_000, output_limit=64_000)
    assert derived.output_limit == cm.UPSTREAM_OUTPUT_TOKEN_MAX
    assert derived.usable == 200_000 - cm.UPSTREAM_OUTPUT_TOKEN_MAX
    assert derived.reserved == cm.UPSTREAM_COMPACTION_BUFFER


@pytest.mark.parametrize(
    "context_limit,output_limit",
    [
        (16384, 8192),   # threshold 直接是負的
        (32768, 10000),  # threshold 正的,但 tail_cap 塌掉
    ],
)
def test_derive_settings_refuses_a_model_too_small_for_the_contract(
    context_limit, output_limit
):
    """推不出門檻或 tail 上限時只能報錯 —— 回一個假的門檻等於每次 idle 都壓縮。"""
    with pytest.raises(cm.CompactionModeError):
        cm.derive_settings(context_limit=context_limit, output_limit=output_limit)


def test_tool_result_fraction_matches_the_runtime_contract():
    """12% 是 tool_result_adapter 的契約;兩邊漂移就是門檻與實際預算對不上。"""
    import tool_result_adapter

    assert cm.TOOL_RESULT_CONTEXT_FRACTION == tool_result_adapter.DEFAULT_CONTEXT_FRACTION


# ---------------------------------------------------------------------------
# 2. canonical 規則文字
# ---------------------------------------------------------------------------
def test_canonical_rule_block_has_exactly_seven_numbered_rules():
    block = cm.canonical_block(cm.RULES_BLOCK_MARKER)
    numbered = [
        line for line in block.splitlines() if line[:2] in {f"{i}." for i in range(1, 10)}
    ]
    assert len(numbered) == 7, block
    assert cm.canonical_block(cm.RECONCILIATION_BLOCK_MARKER).startswith(
        cm.RECONCILIATION_BLOCK_MARKER
    )


def test_rule_headings_come_from_the_document(tmp_path):
    """七個欄位標題必須是從文件解析出來的,不是另外抄一份。

    格式核對(plugin 的 summary_format)拿這七個去驗模型產出的摘要。抄一份的話,
    改了規則卻沒改核對,合法摘要會被判成漂移、漂移摘要會被放行 —— 兩種都是靜默的。
    """
    headings = cm.rule_headings()
    assert len(headings) == cm.RULE_HEADING_COUNT
    block = cm.canonical_block(cm.RULES_BLOCK_MARKER)
    for name in headings:
        assert f"## {name}" in block

    doc = tmp_path / "rules.md"
    doc.write_text(
        "```text\n[CodeTrail 壓縮規則]\n1. 固定欄位:## 任務、## 只有兩個。\n"
        "2. 其他\n```\n",
        encoding="utf-8",
    )
    with pytest.raises(cm.CompactionModeError):
        cm.rule_headings(doc=doc)


def test_canonical_block_is_fail_loud_when_the_doc_drifts(tmp_path):
    doc = tmp_path / "rules.md"
    doc.write_text("# 沒有規則區塊\n", encoding="utf-8")
    with pytest.raises(cm.CompactionModeError):
        cm.canonical_block(cm.RULES_BLOCK_MARKER, doc=doc)


# ---------------------------------------------------------------------------
# 3. 狀態檔
# ---------------------------------------------------------------------------
def _fresh_state(tmp_path: Path, config_path: Path | None = None):
    config: dict = {}
    _, _, errors, state = cm.apply_mode(
        config,
        mode=cm.MODE_CODETRAIL,
        derived=_derived(),
        prior_state=None,
        config_path=config_path or (tmp_path / "opencode.json"),
    )
    assert errors == []
    return config, state


def test_save_state_is_owner_only_and_atomic(tmp_path):
    target = tmp_path / "cfgdir" / "compaction.json"
    _, state = _fresh_state(tmp_path)
    cm.save_state(state, path=target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert cm.load_state(path=target) == state
    # 暫存檔不得留在目錄裡
    assert sorted(p.name for p in target.parent.iterdir()) == ["compaction.json"]


def test_save_state_refuses_a_symlink_target(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "compaction.json"
    link.symlink_to(real)
    _, state = _fresh_state(tmp_path)
    with pytest.raises(cm.CompactionModeError):
        cm.save_state(state, path=link)
    assert real.read_text(encoding="utf-8") == "{}"


def test_save_state_refuses_a_symlinked_state_directory(tmp_path):
    """父目錄被換成 symlink 時,對其下的一般檔案做 is_symlink() 仍是 False。

    少了這道防線,程式會跟過去 chmod 對方的目錄、並在對方目錄裡 replace 檔案。
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link_dir = tmp_path / "codetrail"
    link_dir.symlink_to(elsewhere, target_is_directory=True)
    _, state = _fresh_state(tmp_path)
    with pytest.raises(cm.CompactionModeError):
        cm.save_state(state, path=link_dir / "compaction.json")
    assert list(elsewhere.iterdir()) == []


def test_load_state_is_fail_closed(tmp_path):
    target = tmp_path / "compaction.json"
    assert cm.load_state(path=target) is None                      # 不存在
    target.write_text("not json", encoding="utf-8")
    target.chmod(0o600)
    assert cm.load_state(path=target) is None                      # 壞 JSON
    target.write_bytes(b"\xff\xfe not utf-8")
    target.chmod(0o600)
    assert cm.load_state(path=target) is None                      # 非 UTF-8 也不能 raise
    for payload in (
        {"schema": 99, "mode": "codetrail"},
        {"schema": 1, "mode": "nope", "managed": {}, "plugin": {}, "config": {},
         "section_present": False, "digest": "x"},
        {"schema": 1, "mode": "codetrail", "managed": {"bogus": {"value": 1}},
         "plugin": {}, "config": {}, "section_present": False, "digest": "x"},
    ):
        target.write_text(json.dumps(payload), encoding="utf-8")
        target.chmod(0o600)
        assert cm.load_state(path=target) is None


def test_state_with_a_tampered_prior_is_rejected(tmp_path):
    """`prior` 才是還原時會被寫回 config 的東西,所以 digest 必須涵蓋它。"""
    target = tmp_path / "compaction.json"
    _, state = _fresh_state(tmp_path)
    cm.save_state(state, path=target)
    forged = json.loads(target.read_text(encoding="utf-8"))
    forged["managed"]["auto"]["prior"] = {"present": True, "value": "攻擊者的值"}
    target.write_text(json.dumps(forged), encoding="utf-8")
    target.chmod(0o600)
    assert cm.load_state(path=target) is None
    state_only, reason = cm.inspect_state(path=target)
    assert state_only is None and reason and "digest" in reason


def test_load_state_ignores_a_symlinked_state_file(tmp_path):
    real = tmp_path / "real.json"
    _, state = _fresh_state(tmp_path)
    real.write_text(cm.state_payload(state), encoding="utf-8")
    real.chmod(0o600)
    link = tmp_path / "compaction.json"
    link.symlink_to(real)
    assert cm.load_state(path=link) is None


def test_load_state_refuses_a_world_readable_state_file(tmp_path):
    """別的帳號改得動的接管紀錄不能被採信 —— 它決定要把什麼寫回設定。"""
    target = tmp_path / "compaction.json"
    _, state = _fresh_state(tmp_path)
    cm.save_state(state, path=target)
    target.chmod(0o644)
    value, reason = cm.inspect_state(path=target)
    assert value is None and reason and "其他帳號" in reason


def test_load_state_refuses_a_group_writable_state_directory(tmp_path):
    target = tmp_path / "cfgdir" / "compaction.json"
    _, state = _fresh_state(tmp_path)
    cm.save_state(state, path=target)
    target.parent.chmod(0o777)
    value, reason = cm.inspect_state(path=target)
    assert value is None and reason and "其他帳號" in reason


def test_state_dir_follows_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cm.state_path(os.environ) == tmp_path / ".config" / "codetrail" / "compaction.json"


# ---------------------------------------------------------------------------
# 4. 接管與還原
# ---------------------------------------------------------------------------
def test_takeover_records_prior_values_and_registers_the_plugin(tmp_path):
    plugin = tmp_path / "codetrail-compaction.js"
    config = {"compaction": {"auto": True, "prune": True}, "plugin": ["npm:other"]}
    changes, warnings, errors, state = cm.apply_mode(
        config,
        mode=cm.MODE_CODETRAIL,
        derived=_derived(),
        prior_state=None,
        config_path=tmp_path / "opencode.json",
        plugin_path=plugin,
    )
    assert errors == []
    assert config["compaction"] == {
        "auto": False, "prune": True, "tail_turns": 1, "preserve_recent_tokens": 23920,
    }
    assert config["plugin"] == ["npm:other", str(plugin)]
    assert state["managed"]["auto"]["prior"] == {"present": True, "value": True}
    assert state["managed"]["tail_turns"]["prior"] == {"present": False}
    assert state["section_present"] is True
    assert state["plugin"]["registered"] is True
    assert state["plugin"]["prior_present"] is False
    assert any("auto" in warning for warning in warnings)
    assert changes


def test_reapplying_the_same_mode_keeps_the_original_prior(tmp_path):
    """重跑 set_config 不得把「接管前原值」換成 CodeTrail 自己寫的值。

    換掉的話切回 native 會把 auto 還原成 false —— 也就是永遠回不去原生。
    """
    config_path = tmp_path / "opencode.json"
    config = {"compaction": {"auto": True}}
    _, _, _, first = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(),
        prior_state=None, config_path=config_path,
    )
    _, _, _, second = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(),
        prior_state=first, config_path=config_path,
    )
    assert second["managed"]["auto"]["prior"] == {"present": True, "value": True}


def test_state_from_another_config_is_refused(tmp_path):
    """拿 A 設定的接管紀錄去動 B 設定,會把 A 的原值寫進 B。"""
    config_a = {"compaction": {"auto": True}}
    _, _, _, state_a = cm.apply_mode(
        config_a, mode=cm.MODE_CODETRAIL, derived=_derived(),
        prior_state=None, config_path=tmp_path / "a.json",
    )
    config_b = {"compaction": {"auto": False}}
    _, _, errors, _ = cm.apply_mode(
        config_b, mode=cm.MODE_NATIVE, derived=None,
        prior_state=state_a, config_path=tmp_path / "b.json",
    )
    assert errors and "另一份" in errors[0]
    assert config_b == {"compaction": {"auto": False}}
    assert cm.state_matches_config(state_a, tmp_path / "a.json") is True
    assert cm.state_matches_config(state_a, tmp_path / "b.json") is False


def test_native_restores_exactly_what_was_taken_over(tmp_path):
    plugin = tmp_path / "codetrail-compaction.js"
    config_path = tmp_path / "opencode.json"
    config = {"compaction": {"auto": True}, "plugin": []}
    _, _, _, state = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=None,
        config_path=config_path, plugin_path=plugin,
    )
    changes, _, errors, restored = cm.apply_mode(
        config, mode=cm.MODE_NATIVE, derived=None, prior_state=state,
        config_path=config_path, plugin_path=plugin,
    )
    assert errors == []
    # native 也留一份狀態:「明確選了 native」與「從來沒設定過」對 contract
    # check 是兩件事,而且 section_present 要沿用接管前的事實。
    assert restored is not None
    assert restored["mode"] == cm.MODE_NATIVE and restored["managed"] == {}
    assert restored["section_present"] is state["section_present"]
    assert config["compaction"] == {"auto": True}          # 原本沒有的兩個鍵被移除
    assert "plugin" not in config                          # 空陣列一併移除
    assert changes


def test_native_removes_a_section_codetrail_created(tmp_path):
    config_path = tmp_path / "opencode.json"
    config, state = _fresh_state(tmp_path, config_path)
    assert state["section_present"] is False
    cm.apply_mode(
        config, mode=cm.MODE_NATIVE, derived=None, prior_state=state,
        config_path=config_path,
    )
    assert "compaction" not in config


def test_native_keeps_an_empty_section_the_user_already_had(tmp_path):
    config_path = tmp_path / "opencode.json"
    config: dict = {"compaction": {}}
    _, _, _, state = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=None,
        config_path=config_path,
    )
    cm.apply_mode(
        config, mode=cm.MODE_NATIVE, derived=None, prior_state=state,
        config_path=config_path,
    )
    assert config["compaction"] == {}


def test_native_leaves_values_the_user_changed_after_takeover(tmp_path):
    """ownership 證據 = 現在的值還是我們寫的那個。改過就不再是我們的。"""
    config_path = tmp_path / "opencode.json"
    config = {"compaction": {"auto": True}}
    _, _, _, state = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=None,
        config_path=config_path,
    )
    config["compaction"]["tail_turns"] = 4                 # 使用者事後手改
    _, warnings, _, _ = cm.apply_mode(
        config, mode=cm.MODE_NATIVE, derived=None, prior_state=state,
        config_path=config_path,
    )
    assert config["compaction"]["tail_turns"] == 4
    assert config["compaction"]["auto"] is True            # 這個仍是我們的,照樣還原
    assert any("tail_turns" in warning for warning in warnings)


def test_ownership_is_json_type_strict(tmp_path):
    """Python 的 `True == 1` 會讓 boolean 手改值被當成「還是我們寫的 1」。"""
    config_path = tmp_path / "opencode.json"
    config: dict = {}
    _, _, _, state = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=None,
        config_path=config_path,
    )
    config["compaction"]["tail_turns"] = True               # JSON boolean,不是 1
    assert cm.owns(state, "tail_turns", config) is False
    _, _, _, _ = cm.apply_mode(
        config, mode=cm.MODE_NATIVE, derived=None, prior_state=state,
        config_path=config_path,
    )
    assert config["compaction"]["tail_turns"] is True
    assert cm.json_equal(True, 1) is False
    # 1 與 1.0 在 JSON 裡是同一個數字,JS 端 parse 完就分不出來。判成不同的話
    # plugin 會說「沒漂移」而 Python 說「漂移了」,兩邊對同一份設定講相反的話。
    assert cm.json_equal(1, 1.0) is True
    assert cm.json_equal(1, "1") is False


def test_native_never_removes_a_plugin_entry_the_user_had_first(tmp_path):
    plugin = tmp_path / "codetrail-compaction.js"
    config_path = tmp_path / "opencode.json"
    config = {"plugin": [str(plugin)]}
    _, _, _, state = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=None,
        config_path=config_path, plugin_path=plugin,
    )
    assert state["plugin"]["prior_present"] is True
    _, warnings, _, _ = cm.apply_mode(
        config, mode=cm.MODE_NATIVE, derived=None, prior_state=state,
        config_path=config_path, plugin_path=plugin,
    )
    assert config["plugin"] == [str(plugin)]
    assert any("接管前就存在" in warning for warning in warnings)


def test_native_keeps_an_entry_the_user_added_options_to(tmp_path):
    """CodeTrail 寫下去的是裸字串;變成 [path, options] 就不再是我們的形狀。"""
    plugin = tmp_path / "codetrail-compaction.js"
    config_path = tmp_path / "opencode.json"
    config: dict = {}
    _, _, _, state = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=None,
        config_path=config_path, plugin_path=plugin,
    )
    config["plugin"] = [[str(plugin), {"user_option": 1}]]
    _, warnings, _, _ = cm.apply_mode(
        config, mode=cm.MODE_NATIVE, derived=None, prior_state=state,
        config_path=config_path, plugin_path=plugin,
    )
    assert config["plugin"] == [[str(plugin), {"user_option": 1}]]
    assert any("options" in warning for warning in warnings)


def test_plugin_entry_is_deduplicated_and_keeps_option_pairs(tmp_path):
    plugin = tmp_path / "codetrail-compaction.js"
    config = {"plugin": [str(plugin), [str(plugin), {"x": 1}], "npm:keep"]}
    changes, _, errors, entry = cm.apply_plugin_entry(
        config, plugin_path=plugin, prior_state=None, register=True
    )
    assert errors == []
    assert config["plugin"] == [[str(plugin), {"x": 1}], "npm:keep"]
    assert entry["prior_present"] is True
    assert entry["entry"] == "list"
    assert changes


def test_plugin_entry_recognises_the_file_url_form(tmp_path):
    """OpenCode 會把裸絕對路徑正規化成 file:///…;只認一種就會註冊兩份。"""
    plugin = tmp_path / "codetrail-compaction.js"
    config = {"plugin": [f"file://{plugin}"]}
    changes, _, errors, _ = cm.apply_plugin_entry(
        config, plugin_path=plugin, prior_state=None, register=True
    )
    assert errors == []
    assert config["plugin"] == [f"file://{plugin}"]
    assert not changes


def test_plugin_local_path_matches_the_contract_check_normaliser(tmp_path):
    """兩個 writer 對「同一個 plugin 檔」的判準必須一致。"""
    from scripts import opencode_contract_check as occ

    cases = [
        "/abs/path/plugin.js",
        "file:///abs/path/plugin.js",
        "file://localhost/abs/path/plugin.js",
        "file://remote/abs/path/plugin.js",
        "npm:some-plugin",
        "https://example.invalid/p.js",
        "relative/plugin.js",
        "~/plugin.js",
    ]
    for case in cases:
        assert cm.plugin_local_path(case) == occ._plugin_local_path(case), case


@pytest.mark.parametrize(
    "config,needle",
    [
        ({"plugin": "codetrail-compaction.js"}, "plugin"),
        ({"plugin": None}, "plugin"),
        ({"compaction": []}, "compaction"),
        ({"compaction": None}, "compaction"),
    ],
)
def test_wrong_types_and_json_null_are_blocking_errors(tmp_path, config, needle):
    """JSON `null` 是使用者設過的值,不是「這個鍵不存在」。"""
    _, _, errors, _ = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=None,
        config_path=tmp_path / "opencode.json",
        plugin_path=tmp_path / "codetrail-compaction.js",
    )
    assert errors and needle in errors[0]
    assert config == config  # in-place 沒有被改壞


# ---------------------------------------------------------------------------
# 5. 有效設定漂移
# ---------------------------------------------------------------------------
def test_effective_drift_reports_every_managed_key_and_the_plugin(tmp_path):
    plugin = tmp_path / "codetrail-compaction.js"
    config_path = tmp_path / "opencode.json"
    config: dict = {}
    _, _, _, state = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=None,
        config_path=config_path, plugin_path=plugin,
    )
    assert cm.effective_drift(config, state=state, plugin_path=plugin) == []

    drifted = json.loads(json.dumps(config))
    drifted["compaction"]["auto"] = True                   # 專案層 override 翻掉它
    drifted["plugin"] = []
    drift = cm.effective_drift(drifted, state=state, plugin_path=plugin)
    assert any("auto" in item for item in drift)
    assert any("plugin" in item for item in drift)


def test_effective_drift_is_silent_without_state(tmp_path):
    """沒有狀態檔 = 沒有接管。這時任何設定都不算漂移。"""
    assert cm.effective_drift({"compaction": {"auto": True}}, state=None) == []


def test_native_mode_flags_a_still_registered_plugin(tmp_path):
    plugin = tmp_path / "codetrail-compaction.js"
    state = cm.build_state(
        mode=cm.MODE_NATIVE, config_path=tmp_path / "opencode.json",
        managed={}, plugin={"registered": False, "prior_present": False},
        section_present=False,
    )
    drift = cm.effective_drift(
        {"plugin": [str(plugin)]}, state=state, plugin_path=plugin
    )
    assert drift and "native" in drift[0]


def test_state_digest_survives_an_integral_float_in_prior(tmp_path):
    """使用者原本合法寫成 `"tail_turns": 1.0` 時,狀態不得被判成被竄改。

    JS 端 `JSON.parse` 完只剩 `1`,那個資訊救不回來;Python 這端不讓步的話,
    plugin 會把 set_config 寫出的**正確**狀態當成損壞,然後靜默停用。
    """
    config = {"compaction": {"auto": True, "tail_turns": 1.0}}
    _, _, errors, state = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=None,
        config_path=tmp_path / "opencode.json",
    )
    assert errors == []
    assert state["managed"]["tail_turns"]["prior"] == {"present": True, "value": 1.0}
    assert cm.validate_state(json.loads(json.dumps(state))) is not None
    # JS 讀到的形狀(整數 float 變整數)必須算出同一個 digest
    as_js = json.loads(json.dumps(state))
    as_js["managed"]["tail_turns"]["prior"]["value"] = 1
    assert cm._state_digest(as_js) == state["digest"]


def test_config_identity_needs_both_hashes(tmp_path):
    """只比 path_hash:symlink 改指到另一份設定,路徑沒變就照樣通過。"""
    real_a = tmp_path / "a.json"
    real_b = tmp_path / "b.json"
    real_a.write_text("{}", encoding="utf-8")
    real_b.write_text("{}", encoding="utf-8")
    link = tmp_path / "opencode.json"
    link.symlink_to(real_a)

    _, _, errors, state = cm.apply_mode(
        {}, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=None,
        config_path=link,
    )
    assert errors == []
    assert cm.state_matches_config(state, link) is True
    link.unlink()
    link.symlink_to(real_b)                      # 同一個路徑,指到別份設定
    assert cm.state_matches_config(state, link) is False


def _owned_plugin_state(tmp_path: Path, old_path: str, mode: str = cm.MODE_CODETRAIL):
    """一份「CodeTrail 之前註冊過 old_path」的狀態(搬家前的世界)。"""
    return cm.build_state(
        mode=mode, config_path=tmp_path / "opencode.json",
        managed={}, plugin={
            "registered": True, "prior_present": False, "entry": "string",
            "path_hash": cm.plugin_path_hash(old_path),
        },
        section_present=False,
    )


def test_a_moved_repo_converges_to_exactly_one_plugin_entry(tmp_path):
    """repo 搬家後只 append 新路徑,會留下舊那筆:兩個 instance 或載入失敗。"""
    plugin = tmp_path / "new" / "opencode_plugins" / cm.PLUGIN_FILENAME
    plugin.parent.mkdir(parents=True)
    plugin.write_text("//", encoding="utf-8")
    old_path = str(tmp_path / "old" / "opencode_plugins" / cm.PLUGIN_FILENAME)
    state = _owned_plugin_state(tmp_path, old_path)
    config = {"plugin": [old_path, "npm:keep"]}

    changes, _, errors, entry = cm.apply_plugin_entry(
        config, plugin_path=plugin, prior_state=state, register=True
    )
    assert errors == []
    assert config["plugin"] == [str(plugin), "npm:keep"]
    assert entry["registered"] is True
    assert any("搬家" in item for item in changes)

    # 帶 options 的舊路徑:options 要跟著搬過去,不能被丟掉
    moved = {"plugin": [[old_path, {"user": 1}]]}
    cm.apply_plugin_entry(moved, plugin_path=plugin, prior_state=state, register=True)
    assert moved["plugin"] == [[str(plugin), {"user": 1}]]


def test_a_same_named_plugin_we_never_registered_is_not_hijacked(tmp_path):
    """使用者自己 fork 的同名 plugin 不是「搬家前的我們」。

    沒有 ownership 證據就把它改寫成本 repo 路徑,等於接管一個不屬於我們的項目,
    而且切回 native 之後也還不回去。
    """
    plugin = tmp_path / "repo" / "opencode_plugins" / cm.PLUGIN_FILENAME
    plugin.parent.mkdir(parents=True)
    plugin.write_text("//", encoding="utf-8")
    fork = "/opt/custom/" + cm.PLUGIN_FILENAME
    config = {"plugin": [fork]}

    _, warnings, errors, entry = cm.apply_plugin_entry(
        config, plugin_path=plugin, prior_state=None, register=True
    )
    assert errors == []
    assert config["plugin"] == [fork, str(plugin)]        # 他的那筆原封不動
    assert any("沒有註冊過它" in item for item in warnings)

    # 切回 native 只移除我們自己那一筆
    _, _, _, _ = cm.apply_plugin_entry(
        config, plugin_path=plugin,
        prior_state=cm.build_state(
            mode=cm.MODE_CODETRAIL, config_path=tmp_path / "opencode.json",
            managed={}, plugin=entry, section_present=False,
        ),
        register=False,
    )
    assert config["plugin"] == [fork]


def test_native_keeps_a_pre_existing_plugin_without_calling_it_drift(tmp_path):
    """接管前使用者自己就載入它時,native 模式下它還在陣列裡是**正確**的。

    報成漂移的話,每個 session 都會跳一次錯誤 toast 並寫一筆 incident。
    """
    plugin = tmp_path / "codetrail-compaction.js"
    config_path = tmp_path / "opencode.json"
    config = {"plugin": [str(plugin)]}
    _, _, _, taken = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=None,
        config_path=config_path, plugin_path=plugin,
    )
    _, _, _, restored = cm.apply_mode(
        config, mode=cm.MODE_NATIVE, derived=None, prior_state=taken,
        config_path=config_path, plugin_path=plugin,
    )
    assert config["plugin"] == [str(plugin)]        # 保留
    assert restored["plugin"]["prior_present"] is True
    assert cm.effective_drift(config, state=restored, plugin_path=plugin) == []


def test_a_replaced_entry_is_not_hijacked_even_after_we_registered_once(tmp_path):
    """我們註冊過,不代表**現在**陣列裡那筆同名項就是我們的。

    使用者刪掉 CodeTrail 那筆、換上自己的 /custom/... 之後,下一次 --fix 若把
    它改寫成本 repo 路徑,等於接管一個沒有任何證據屬於我們的項目。
    """
    plugin = tmp_path / "repo" / "opencode_plugins" / cm.PLUGIN_FILENAME
    plugin.parent.mkdir(parents=True)
    plugin.write_text("//", encoding="utf-8")
    old_path = str(tmp_path / "old" / "opencode_plugins" / cm.PLUGIN_FILENAME)
    state = _owned_plugin_state(tmp_path, old_path)
    custom = "/custom/" + cm.PLUGIN_FILENAME
    config = {"plugin": [[custom, {"user": 1}]]}

    _, warnings, errors, _ = cm.apply_plugin_entry(
        config, plugin_path=plugin, prior_state=state, register=True
    )
    assert errors == []
    assert config["plugin"] == [[custom, {"user": 1}], str(plugin)]
    assert any("沒有註冊過它" in item for item in warnings)


def test_switching_to_native_after_a_repo_move_removes_the_old_entry(tmp_path):
    """搬家後直接切 native:只認新 target 的話,舊那筆會永遠留在設定裡。"""
    plugin = tmp_path / "new" / "opencode_plugins" / cm.PLUGIN_FILENAME
    plugin.parent.mkdir(parents=True)
    plugin.write_text("//", encoding="utf-8")
    old_path = str(tmp_path / "old" / "opencode_plugins" / cm.PLUGIN_FILENAME)
    state = _owned_plugin_state(tmp_path, old_path)
    config = {"plugin": [old_path, "npm:keep"]}

    _, _, errors, _ = cm.apply_plugin_entry(
        config, plugin_path=plugin, prior_state=state, register=False
    )
    assert errors == []
    assert config["plugin"] == ["npm:keep"]


def test_a_native_baseline_is_recomputed_from_the_current_config(tmp_path):
    """native 期間使用者新加的東西是**他的**,不能被第一次接管前的舊事實刪掉。"""
    plugin = tmp_path / "codetrail-compaction.js"
    config_path = tmp_path / "opencode.json"
    config: dict = {}
    _, _, _, taken = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=None,
        config_path=config_path, plugin_path=plugin,
    )
    _, _, _, restored = cm.apply_mode(
        config, mode=cm.MODE_NATIVE, derived=None, prior_state=taken,
        config_path=config_path, plugin_path=plugin,
    )
    assert "compaction" not in config and "plugin" not in config

    # native 期間:使用者自己加了同一個 plugin 與一個空的 compaction 區塊
    config["plugin"] = [str(plugin)]
    config["compaction"] = {}

    _, _, _, again = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=restored,
        config_path=config_path, plugin_path=plugin,
    )
    assert again["section_present"] is True
    assert again["plugin"]["prior_present"] is True
    cm.apply_mode(
        config, mode=cm.MODE_NATIVE, derived=None, prior_state=again,
        config_path=config_path, plugin_path=plugin,
    )
    assert config["compaction"] == {}                    # 他加的區塊還在
    assert config["plugin"] == [str(plugin)]             # 他加的 plugin 還在


@pytest.mark.parametrize("bad", [1e-7, 2**53, 0.5, {"a": 1}, [1], "中文"])
def test_state_refuses_values_the_two_languages_serialise_differently(tmp_path, bad):
    """digest 兩端算不出同一個值時,plugin 會把正確的狀態當成被竄改而靜默停用。

    在**寫入當下**擋掉,錯誤訊息才指得到是哪個鍵、哪個值。
    """
    with pytest.raises(cm.CompactionModeError):
        cm.build_state(
            mode=cm.MODE_CODETRAIL, config_path=tmp_path / "opencode.json",
            managed={"tail_turns": {"prior": {"present": True, "value": bad}, "value": 1}},
            plugin={}, section_present=True,
        )


def test_a_pre_existing_plugin_is_not_claimed_by_a_repo_move(tmp_path):
    """接管前就存在的項是使用者的:記了它的路徑雜湊,搬家後就會被改寫或刪掉。"""
    plugin = tmp_path / "old" / "opencode_plugins" / cm.PLUGIN_FILENAME
    plugin.parent.mkdir(parents=True)
    plugin.write_text("//", encoding="utf-8")
    config_path = tmp_path / "opencode.json"
    config = {"plugin": [str(plugin)]}
    _, _, errors, state = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=None,
        config_path=config_path, plugin_path=plugin,
    )
    assert errors == []
    assert state["plugin"]["prior_present"] is True
    assert state["plugin"]["path_hash"] is None          # 不是我們寫的,不留證據

    moved = tmp_path / "new" / "opencode_plugins" / cm.PLUGIN_FILENAME
    moved.parent.mkdir(parents=True)
    moved.write_text("//", encoding="utf-8")
    _, _, _, _ = cm.apply_plugin_entry(
        config, plugin_path=moved, prior_state=state, register=True
    )
    assert str(plugin) in config["plugin"]                # 他那筆原封不動
    assert str(moved) in config["plugin"]


def test_an_entry_we_added_after_a_move_is_still_ours_at_native(tmp_path):
    """接管前使用者已有舊路徑那筆;搬家後我們另外加了一筆。

    `prior_present` 與 ownership 混在一起的話,切 native 時兩筆都不敢刪,
    native 模式仍然載入 CodeTrail 後來加的 plugin。
    """
    old_plugin = tmp_path / "old" / "opencode_plugins" / cm.PLUGIN_FILENAME
    old_plugin.parent.mkdir(parents=True)
    old_plugin.write_text("//", encoding="utf-8")
    config_path = tmp_path / "opencode.json"
    config = {"plugin": [str(old_plugin)]}
    _, _, errors, taken = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=None,
        config_path=config_path, plugin_path=old_plugin,
    )
    assert errors == []
    assert taken["plugin"]["prior_present"] is True
    assert taken["plugin"]["path_hash"] is None           # 那筆是他的

    moved = tmp_path / "new" / "opencode_plugins" / cm.PLUGIN_FILENAME
    moved.parent.mkdir(parents=True)
    moved.write_text("//", encoding="utf-8")
    _, _, errors, after = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=taken,
        config_path=config_path, plugin_path=moved,
    )
    assert errors == []
    assert config["plugin"] == [str(old_plugin), str(moved)]
    assert after["plugin"]["path_hash"] == cm.plugin_path_hash(str(moved))

    _, _, errors, _ = cm.apply_mode(
        config, mode=cm.MODE_NATIVE, derived=None, prior_state=after,
        config_path=config_path, plugin_path=moved,
    )
    assert errors == []
    assert config["plugin"] == [str(old_plugin)]          # 我們加的那筆被移除
