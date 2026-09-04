"""opencode_migrate 的契約:遷移是唯一會寫使用者 OpenCode 設定的路徑。

還原錯值等於**靜默**改掉使用者的設定 —— opencode.json 裡沒有任何欄位事後分辨
得出「這是 CodeTrail 還原的」還是「這是我自己設的」。所以這裡守的是:

  * 只還原**現值仍等於 CodeTrail 寫入值**的鍵(既有 ownership 語意);
  * 只移除 path 對得上的 plugin 項,同名但指向別處的一律不碰;
  * `mcp.codetrail` 與 `permission` 一個字都不動;
  * 沒有狀態檔、也沒有我們的 plugin 項的機器:**零寫入**;
  * 自訂 instructions 只報告,絕不自動搬。

ownership 狀態檔本身的契約(owner-only 權限與 symlink 防線、digest 涵蓋 prior、
綁定單一 config、「沒有狀態檔 = 沒有接管」的 fail-closed、受管鍵與契約鍵是兩組、
plugin 項的認領與去重)也在這裡 —— 原本在 tests/test_compaction_mode.py,
隨著那半個模組一起搬進 opencode_migrate。門檻公式那一半在
tests/test_compaction_formula.py。
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

import opencode_migrate as cm  # noqa: E402
import opencode_migrate as migrate  # noqa: E402

pytestmark = pytest.mark.smoke


def test_another_installs_takeover_is_left_alone(home, monkeypatch, tmp_path):
    """同一台機器上的另一份 CodeTrail 寫的接管紀錄:零寫入、只提示。

    拿這裡的路徑去還原,會把另一份寫進去的值判成「使用者手改過」而一個鍵都不還原,
    然後照樣把狀態檔刪掉 —— 那一份從此還原不回去,而且沒有任何訊息。
    """
    other = tmp_path / "other-checkout" / "opencode_plugins"
    other.mkdir(parents=True)
    other_plugin = other / migrate.COMPACTION_PLUGIN
    other_plugin.write_text("// 另一份安裝的 plugin\n", encoding="utf-8")

    config_path = _config_path(home)
    config = {"plugin": [str(other_plugin)], "compaction": {"auto": False}}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    state = cm.build_state(
        mode=cm.MODE_CODETRAIL,
        config_path=config_path,
        managed={"auto": {"prior": {"present": False, "value": None}, "value": False}},
        plugin={
            "registered": True,
            "prior_present": False,
            "entry": "string",
            "path_hash": cm.plugin_path_hash(str(other_plugin)),
        },
        section_present=False,
    )
    cm.save_state(state, path=home / ".config" / "codetrail" / "compaction.json")

    plan = migrate.plan_migration({"HOME": str(home)})
    assert plan.foreign_owner == str(other_plugin)
    assert plan.needed is False
    assert any("另一份 CodeTrail 安裝" in line for line in plan.render())

    before = config_path.read_bytes()
    applied = migrate.apply_migration({"HOME": str(home)})
    assert applied.needed is False
    assert config_path.read_bytes() == before
    assert (home / ".config" / "codetrail" / "compaction.json").exists()


def test_a_moved_repo_is_not_mistaken_for_another_install(home, tmp_path):
    """本 repo 搬過家時記的也是舊路徑。舊路徑**已經不在**,所以照舊走搬家那條路,
    不得被當成別份安裝而拒絕。"""
    old_plugin = tmp_path / "old-checkout" / "opencode_plugins" / migrate.COMPACTION_PLUGIN
    config_path = _config_path(home)
    config = {"plugin": [str(old_plugin)], "compaction": {"auto": False}}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    state = cm.build_state(
        mode=cm.MODE_CODETRAIL,
        config_path=config_path,
        managed={"auto": {"prior": {"present": False, "value": None}, "value": False}},
        plugin={
            "registered": True,
            "prior_present": False,
            "entry": "string",
            "path_hash": cm.plugin_path_hash(str(old_plugin)),
        },
        section_present=False,
    )
    cm.save_state(state, path=home / ".config" / "codetrail" / "compaction.json")

    plan = migrate.plan_migration({"HOME": str(home)})
    assert plan.foreign_owner == ""
    assert plan.needed is True


def _derived():
    return cm.derive_settings(context_limit=131072, output_limit=8192)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    (root / ".config" / "opencode").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return root


def _config_path(home: Path) -> Path:
    return home / ".config" / "opencode" / "opencode.json"


def _write_config(home: Path, value: dict) -> Path:
    path = _config_path(home)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _take_over(home: Path, config: dict, *, prior: dict | None = None,
               path: Path | None = None) -> Path:
    """演出「CodeTrail 接管過」的世界:寫進受管值 + plugin 項 + 狀態檔。"""
    if path is None:
        path = _write_config(home, config)
    else:
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    derived = cm.derive_settings(context_limit=131072, output_limit=8192)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    _changes, _warnings, errors, state = cm.apply_mode(
        loaded,
        mode=cm.MODE_CODETRAIL,
        derived=derived,
        prior_state=None,
        config_path=path,
        plugin_path=cm.PLUGIN_PATH,
    )
    assert errors == [], errors
    path.write_text(json.dumps(loaded, ensure_ascii=False, indent=2), encoding="utf-8")
    cm.save_state(state, path=cm.state_path({"HOME": str(home)}))
    return path


# ============================================================
# 零寫入
# ============================================================
def test_a_machine_that_never_took_over_is_untouched(home):
    path = _write_config(home, {"model": "local/x", "mcp": {"codetrail": {"type": "local"}}})
    before = path.read_bytes()
    plan = migrate.apply_migration()
    assert plan.needed is False
    assert path.read_bytes() == before
    assert not list(home.glob("**/*.bak"))


def test_a_machine_without_any_opencode_config_is_untouched(home):
    _config_path(home).unlink(missing_ok=True)
    plan = migrate.apply_migration()
    assert plan.needed is False


# ============================================================
# 還原
# ============================================================
def test_only_values_we_still_own_are_restored(home):
    _take_over(home, {"model": "local/x", "compaction": {"auto": True, "tail_turns": 9}})
    path = _config_path(home)
    migrate.apply_migration()
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["compaction"]["auto"] is True        # 接管前的值
    assert after["compaction"]["tail_turns"] == 9
    assert "preserve_recent_tokens" not in after["compaction"]  # 接管前不存在
    assert "prune" not in after["compaction"]


def test_a_value_the_user_changed_after_takeover_is_left_alone(home):
    path = _take_over(home, {"model": "local/x"})
    config = json.loads(path.read_text(encoding="utf-8"))
    config["compaction"]["preserve_recent_tokens"] = 12345      # 使用者自己改的
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    plan = migrate.apply_migration()
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["compaction"]["preserve_recent_tokens"] == 12345
    assert any("不是 CodeTrail 寫的" in warning for warning in plan.warnings)


def test_a_compaction_section_we_created_is_removed_again(home):
    path = _take_over(home, {"model": "local/x"})
    migrate.apply_migration()
    after = json.loads(path.read_text(encoding="utf-8"))
    assert "compaction" not in after


# ============================================================
# plugin
# ============================================================
def test_only_our_plugin_entries_are_removed(home):
    other = home / "other" / "codetrail-notify.js"
    other.parent.mkdir(parents=True)
    other.write_text("//", encoding="utf-8")
    path = _take_over(home, {"model": "local/x"})
    config = json.loads(path.read_text(encoding="utf-8"))
    config["plugin"] = list(config.get("plugin", [])) + [
        str(migrate.PLUGIN_DIR / migrate.NOTIFY_PLUGIN),
        str(other),                      # 同名但指向別處 —— 不是我們的
        "npm:someone-elses-plugin",
    ]
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    migrate.apply_migration()
    after = json.loads(path.read_text(encoding="utf-8"))
    assert str(other) in after["plugin"]
    assert "npm:someone-elses-plugin" in after["plugin"]
    assert not any(
        str(migrate.PLUGIN_DIR) in str(item) for item in after["plugin"]
    ), after["plugin"]


# ============================================================
# 不動的東西
# ============================================================
def test_mcp_and_permission_are_never_touched(home):
    path = _take_over(
        home,
        {
            "model": "local/x",
            "mcp": {"codetrail": {"type": "local", "command": ["run"], "timeout": 660000}},
            "permission": {"codetrail_apply_patch": "ask", "bash": "deny"},
        },
    )
    before = json.loads(path.read_text(encoding="utf-8"))
    migrate.apply_migration()
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["mcp"] == before["mcp"]
    assert after["permission"] == before["permission"]


def test_custom_instructions_are_reported_not_moved(home):
    path = _take_over(home, {"model": "local/x", "instructions": [".codetrail/lessons.md", "MY_RULES.md"]})
    plan = migrate.apply_migration()
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["instructions"] == [".codetrail/lessons.md", "MY_RULES.md"]
    assert any("MY_RULES.md" in note for note in plan.notes)
    assert any("不會**自動搬" in note or "不會" in note for note in plan.notes)


# ============================================================
# 交易性
# ============================================================
def test_a_backup_is_left_behind(home):
    path = _take_over(home, {"model": "local/x", "compaction": {"auto": True}})
    migrate.apply_migration()
    backup = path.with_name(path.name + migrate.BACKUP_SUFFIX)
    assert backup.is_file()
    assert json.loads(backup.read_text(encoding="utf-8"))["compaction"]["auto"] is False


def test_the_state_file_is_deleted_only_after_the_config_was_written(home, monkeypatch):
    path = _take_over(home, {"model": "local/x", "compaction": {"auto": True}})
    state_file = cm.state_path({"HOME": str(home)})
    assert state_file.is_file()

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(migrate.shutil, "copy2", _boom)
    with pytest.raises(migrate.MigrationError):
        migrate.apply_migration()
    assert state_file.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["compaction"]["auto"] is False


def test_a_successful_migration_removes_the_state_file(home):
    _take_over(home, {"model": "local/x"})
    state_file = cm.state_path({"HOME": str(home)})
    assert state_file.is_file()
    migrate.apply_migration()
    assert not state_file.exists()


def test_check_mode_writes_nothing(home):
    path = _take_over(home, {"model": "local/x", "compaction": {"auto": True}})
    before = path.read_bytes()
    state_file = cm.state_path({"HOME": str(home)})
    plan = migrate.apply_migration(dry_run=True)
    assert plan.needed is True
    assert path.read_bytes() == before
    assert state_file.is_file()
    assert migrate.migration_needed() is True


def test_a_state_file_for_another_config_is_refused(home, monkeypatch, tmp_path):
    _take_over(home, {"model": "local/x"})
    other = tmp_path / "elsewhere.json"
    other.write_text(json.dumps({"model": "local/x"}), encoding="utf-8")
    monkeypatch.setenv("OPENCODE_CONFIG", str(other))
    with pytest.raises(migrate.MigrationError, match="另一份"):
        migrate.apply_migration()


# ============================================================
# 觸發條件與寫入語意(S4 審核回修)
# ============================================================
def test_a_leftover_compaction_plugin_alone_triggers_the_migration(home):
    """沒有狀態檔、只剩壓縮 plugin 項的機器也要被遷移。

    `apply_mode` 走「沒有狀態檔 = 沒有接管」的 fail-closed,所以**值**不會被
    動;但 plugin 項是 path 對得上的,那就是我們註冊的。少了這條,那個 plugin
    會一直留在使用者的 OpenCode 裡跑,而且永遠不會被提示。
    """
    _write_config(home, {"plugin": [str(cm.PLUGIN_PATH)], "model": "local/x"})
    plan = migrate.plan_migration({"HOME": str(home)})
    assert plan.needed is True
    assert plan.removed_plugins == [str(cm.PLUGIN_PATH)]

    migrate.apply_migration({"HOME": str(home)})
    after = json.loads(_config_path(home).read_text(encoding="utf-8"))
    assert "plugin" not in after or after["plugin"] == []
    assert after["model"] == "local/x"


def test_a_same_named_plugin_from_elsewhere_is_still_not_ours(home):
    other = home / "elsewhere" / cm.PLUGIN_FILENAME
    other.parent.mkdir(parents=True)
    other.write_text("export default {}\n", encoding="utf-8")
    _write_config(home, {"plugin": [str(other)]})
    plan = migrate.plan_migration({"HOME": str(home)})
    assert plan.removed_plugins == []
    assert plan.needed is False


def test_the_custom_config_path_is_honoured(home, monkeypatch):
    """只帶 HOME 會讓 plan 看錯檔案,卻仍然刪掉唯一那份 ownership 狀態檔。"""
    custom = home / "custom-opencode.json"
    monkeypatch.setenv("OPENCODE_CONFIG", str(custom))
    _take_over(home, {"compaction": {"auto": True}}, path=custom)

    env = {"HOME": str(home), "OPENCODE_CONFIG": str(custom)}
    plan = migrate.plan_migration(env)
    assert plan.config_path == custom
    assert plan.needed is True
    migrate.apply_migration(env)
    restored = json.loads(custom.read_text(encoding="utf-8"))
    assert restored.get("compaction", {}).get("auto") is not False


def test_the_config_keeps_its_permissions(home):
    """這份設定可能含 provider API key;temp file 在 umask 022 下是 0644。"""
    import os
    import stat

    path = _take_over(home, {"compaction": {"auto": True}})
    os.chmod(path, 0o600)
    migrate.apply_migration({"HOME": str(home)})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_state_file_that_cannot_be_removed_is_fail_loud(home, monkeypatch):
    """狀態檔還在 = 下一次還會判定成「仍在接管」,而設定已經還原了。"""
    _take_over(home, {"compaction": {"auto": True}})
    monkeypatch.setattr(cm, "clear_state", lambda *_a, **_k: False)
    with pytest.raises(migrate.MigrationError, match="狀態檔"):
        migrate.apply_migration({"HOME": str(home)})


def test_an_unreadable_opencode_config_is_a_problem_not_a_no(home):
    """判不出「要不要遷移」不等於「不需要」:舊值與 plugin 項可能還在那裡沒人管。"""
    _config_path(home).write_text("{ not json", encoding="utf-8")
    assert migrate.migration_problem({"HOME": str(home)})
    assert migrate.migration_needed({"HOME": str(home)}) is True
    assert migrate.migration_problem({"HOME": str(home / "nowhere")}) == ""


def test_an_untrusted_ownership_state_file_is_a_problem_not_a_no(home, monkeypatch):
    """狀態檔**在**卻讀不了(壞掉 / 權限 / symlink):它記的是還原用的原值,
    吞成 warning 等於讓那些舊值永遠沒人還原,而且 aicode 從此不再提示。"""
    _take_over(home, {"compaction": {"auto": True}})
    state_path = cm.state_path({"HOME": str(home)})
    state_path.write_text("{ corrupt", encoding="utf-8")
    env = {"HOME": str(home)}
    assert migrate.migration_problem(env)
    assert migrate.migration_needed(env) is True
    with pytest.raises(migrate.MigrationError, match="狀態檔"):
        migrate.apply_migration(env)


def test_the_managed_values_come_from_the_formula():
    """寫進 opencode.json 的那四個鍵。公式在 `compaction_formula`,鍵名在這裡:
    runtime 只要門檻與 tail 保留額,不需要知道 OpenCode 的 schema。"""
    assert cm.managed_values(_derived()) == {
        "auto": False,
        "tail_turns": 1,
        "preserve_recent_tokens": 23920,
        "prune": True,
    }


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
    assert config["compaction"] == {"auto": True}          # 原本沒有的三個鍵被移除
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


def test_prune_is_taken_over_and_restored_but_never_called_drift(tmp_path):
    """`prune` 是受管鍵(會寫、會還原)但不是契約鍵(改了不算漂移)。

    兩件事都要守:
      * 不寫/不還原的話,切回 native 會把 `prune: true` 永遠留在使用者的設定裡。
      * 併進契約集合的話,使用者把它關掉就換來一則錯誤 toast 與「這個 session
        從此不壓縮」—— 它只影響 context 用量,壓縮本身照樣正確。
    """
    plugin = tmp_path / "codetrail-compaction.js"
    config_path = tmp_path / "opencode.json"
    config: dict = {}
    _, _, errors, state = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=None,
        config_path=config_path, plugin_path=plugin,
    )
    assert errors == []
    assert config["compaction"]["prune"] is True
    assert state["managed"]["prune"]["prior"] == {"present": False}

    flipped = json.loads(json.dumps(config))
    flipped["compaction"]["prune"] = False
    assert cm.effective_drift(flipped, state=state, plugin_path=plugin) == []

    cm.apply_mode(config, mode=cm.MODE_NATIVE, derived=None, prior_state=state,
                  config_path=config_path, plugin_path=plugin)
    assert "compaction" not in config                      # 接管前這個區塊不存在


def test_native_leaves_a_prune_value_the_user_set_before_takeover(tmp_path):
    """接管前使用者自己設過 `prune: false` 時,還原要回到他的值。"""
    plugin = tmp_path / "codetrail-compaction.js"
    config_path = tmp_path / "opencode.json"
    config = {"compaction": {"prune": False}}
    _, warnings, _, state = cm.apply_mode(
        config, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=None,
        config_path=config_path, plugin_path=plugin,
    )
    assert config["compaction"]["prune"] is True
    assert any("prune" in warning for warning in warnings)
    cm.apply_mode(config, mode=cm.MODE_NATIVE, derived=None, prior_state=state,
                  config_path=config_path, plugin_path=plugin)
    assert config["compaction"] == {"prune": False}


def test_unmanaged_keys_names_what_an_older_state_file_never_took_over(tmp_path):
    """受管鍵的集合會長大;舊狀態檔只記得接管當下那幾個。

    CodeTrail 對新鍵沒有 ownership 證據,所以不會自己補(補了就還原不回去)。
    但也不能靜靜當作沒這回事 —— 使用者升級之後永遠拿不到新受管值而且沒有訊息。
    """
    config_path = tmp_path / "opencode.json"
    legacy = cm.build_state(
        mode=cm.MODE_CODETRAIL, config_path=config_path,
        managed={key: {"prior": {"present": False},
                       "value": cm.managed_values(_derived())[key]}
                 for key in cm.CONTRACT_COMPACTION_KEYS},
        plugin={"registered": True, "prior_present": False},
        section_present=False,
    )
    assert cm.validate_state(legacy) is not None           # 舊狀態檔仍然合法
    assert cm.unmanaged_keys(legacy) == ("prune",)

    config = {"compaction": dict(cm.managed_values(_derived()))}
    del config["compaction"]["prune"]
    assert cm.effective_drift(config, state=legacy) == []  # 沒接管的鍵不算漂移

    _, _, _, current = cm.apply_mode(
        {}, mode=cm.MODE_CODETRAIL, derived=_derived(), prior_state=None,
        config_path=config_path,
    )
    assert cm.unmanaged_keys(current) == ()
    assert cm.unmanaged_keys(None) == ()


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


# ── 總審 F1-2:別份安裝的接管,在 OpenCode 設定裡找不到它的 plugin 項時也不得刪狀態 ──


@pytest.mark.smoke
@pytest.mark.parametrize("config_shape", ["missing", "empty", "no_plugin_entry"])
def test_an_unverifiable_foreign_takeover_is_left_alone(home, tmp_path, config_shape):
    """狀態檔記的 plugin 雜湊不是本 repo 的,而 OpenCode 設定暫時不在 / 是空物件 /
    plugin 項被人工移除:反推不出擁有者的路徑。

    以前這裡會因為「設定沒東西」提早回傳、又因為 `state_present` 被判成 needed,
    最後 `apply_migration()` 把 ownership 狀態刪掉 —— 真正擁有者永久失去還原
    prior 值的證據。反推不出來就是**無法確認**,無法確認就零寫入。
    """
    other = tmp_path / "other-checkout" / "opencode_plugins"
    other.mkdir(parents=True)
    other_plugin = other / migrate.COMPACTION_PLUGIN
    other_plugin.write_text("// 另一份安裝的 plugin\n", encoding="utf-8")
    config_path = _config_path(home)
    if config_shape == "missing":
        config_path.unlink(missing_ok=True)
    elif config_shape == "empty":
        config_path.write_text("{}", encoding="utf-8")
    else:
        config_path.write_text(json.dumps({"compaction": {"auto": False}}), encoding="utf-8")
    state = cm.build_state(
        mode=cm.MODE_CODETRAIL,
        config_path=config_path,
        managed={"auto": {"prior": {"present": False, "value": None}, "value": False}},
        plugin={
            "registered": True,
            "prior_present": False,
            "entry": "string",
            "path_hash": cm.plugin_path_hash(str(other_plugin)),
        },
        section_present=False,
    )
    state_path = home / ".config" / "codetrail" / "compaction.json"
    cm.save_state(state, path=state_path)
    before_state = state_path.read_bytes()
    before_config = config_path.read_bytes() if config_path.exists() else None

    plan = migrate.plan_migration({"HOME": str(home)})
    assert plan.needed is False
    assert plan.foreign_owner, "反推不出擁有者要講出來,而不是靜默走還原"
    applied = migrate.apply_migration({"HOME": str(home)})
    assert applied.needed is False
    assert state_path.read_bytes() == before_state, "狀態檔一個 byte 都不得動"
    assert (config_path.read_bytes() if config_path.exists() else None) == before_config
