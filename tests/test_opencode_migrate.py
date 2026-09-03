"""opencode_migrate 的契約:遷移是唯一會寫使用者 OpenCode 設定的路徑。

還原錯值等於**靜默**改掉使用者的設定 —— opencode.json 裡沒有任何欄位事後分辨
得出「這是 CodeTrail 還原的」還是「這是我自己設的」。所以這裡守的是:

  * 只還原**現值仍等於 CodeTrail 寫入值**的鍵(既有 ownership 語意);
  * 只移除 path 對得上的 plugin 項,同名但指向別處的一律不碰;
  * `mcp.codetrail` 與 `permission` 一個字都不動;
  * 沒有狀態檔、也沒有我們的 plugin 項的機器:**零寫入**;
  * 自訂 instructions 只報告,絕不自動搬。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import compaction_mode as cm  # noqa: E402
import opencode_migrate as migrate  # noqa: E402

pytestmark = pytest.mark.smoke


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
