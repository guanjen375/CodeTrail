"""介面主題設定的安全契約：只收明列名稱、重讀後只改 theme、失敗零寫入、重開帶入。"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

import client_app
import client_config
import codetrail_chat


pytestmark = pytest.mark.smoke

#: 錯誤訊息裡只有驗證失敗才會出現的片段。tmp_path 會帶測試名稱(含 "theme"),
#: 所以不能只比對 "theme"。
_LOAD_REJECTED = r"的 theme 必須是"
_VALUE_REJECTED = r"theme 只接受"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    return SimpleNamespace(home=home, path=client_config.config_path())


def _fingerprint(path):
    stat = path.stat()
    return path.read_bytes(), stat.st_ino, stat.st_mtime_ns


def test_theme_defaults_and_reads_never_create_config(home):
    """沒有設定檔就是 default;讀取與同值更新不建目錄、不建檔,舊檔缺鍵也不改寫。"""
    assert client_config.DEFAULT_THEME == "default"
    assert client_config.THEME_VALUES == ("default", "codex")
    settings = client_config.load_client_settings()
    assert settings.theme == "default" and not settings.present
    unchanged, written = client_config.update_theme("default")
    assert written is False and unchanged == settings
    assert client_config.validate_theme("codex") == "codex"
    assert client_config.load_client_settings().theme == "default"
    assert not (home.home / ".config").exists()

    # 升級前寫的 client.json 沒有 theme 鍵:照樣載入成 default,同值更新零寫入。
    home.path.parent.mkdir(parents=True, mode=0o700)
    home.path.write_text(json.dumps({"schema": 1, "compaction_mode": "manual"}), encoding="utf-8")
    home.path.chmod(0o600)
    before = _fingerprint(home.path)
    legacy = client_config.load_client_settings()
    assert legacy.present and legacy.theme == "default"
    unchanged, written = client_config.update_theme("default")
    assert written is False and unchanged.theme == "default"
    assert _fingerprint(home.path) == before


def test_theme_update_rereads_and_preserves_fresh_settings_and_private_modes(home, tmp_path):
    """同一段 session 先 /allow add、/copykey 才 /theme,不能用啟動快照蓋掉剛存的設定。"""
    client_config.save_client_settings(client_config.ClientSettings(
        path=home.path, permission={"run_command": "ask"}, compaction_mode="off",
        project_instructions=False, extra_allowed_commands=["nsim"],
        keep_historical_reasoning=True, show_reasoning=True,
    ))
    directory = tmp_path / "tool chain" / "bin"
    directory.mkdir(parents=True, mode=0o700)
    executable = directory / "arc-probe"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    _, added = client_config.add_allowed_command_directory(str(directory))
    _, keyed = client_config.update_copy_key("f3")
    assert added and keyed
    latest = client_config.load_client_settings()

    changed, written = client_config.update_theme("codex")
    assert written is True and changed.theme == "codex"
    expected = {**latest.as_json(), "theme": "codex"}
    assert changed.as_json() == expected
    assert client_config.load_client_settings().as_json() == expected
    assert client_config.load_client_settings_from(home.path).as_json() == expected
    assert home.path.stat().st_mode & 0o777 == 0o600
    assert home.path.parent.stat().st_mode & 0o777 == 0o700

    before = _fingerprint(home.path)
    again, written = client_config.update_theme("codex")
    assert written is False and again.as_json() == expected
    assert _fingerprint(home.path) == before

    # 其他寫入者(set_config 的壓縮模式、/allow、/copykey)都不得把 theme 丟回預設。
    client_config.save_client_settings(client_config.load_client_settings().with_compaction("manual"))
    client_config.update_extra_allowed_commands("add", ["mdb"])
    client_config.update_copy_key("f4")
    final = client_config.load_client_settings()
    assert final.theme == "codex"
    assert final.extra_allowed_command_dirs == [str(directory)]
    assert final.extra_allowed_commands == ["nsim", "mdb"] and final.copy_key == "f4"


def test_theme_invalid_values_fail_loud_without_writing(home):
    """大小寫不同、Textual 內建主題名與非字串一律拒絕;載入、保存、更新都不以預設掩蓋。"""
    settings, written = client_config.update_theme("codex")
    assert written is True
    before = _fingerprint(home.path)
    original = before[0]
    invalid = (
        "Codex", "CODEX", " codex", "codex ", "nord", "textual-dark", "ansi-dark", "",
        None, 1, True, [], {}, ["codex"],
    )
    for value in invalid:
        with pytest.raises(client_config.ClientConfigError, match=_LOAD_REJECTED):
            client_config.save_client_settings(replace(settings, theme=value))
        assert home.path.read_bytes() == original

        home.path.write_text(json.dumps({**settings.as_json(), "theme": value}), encoding="utf-8")
        with pytest.raises(client_config.ClientConfigError, match=_LOAD_REJECTED):
            client_config.load_client_settings()
        home.path.write_bytes(original)

        with pytest.raises(client_config.ClientConfigError, match=_VALUE_REJECTED):
            client_config.validate_theme(value)
        with pytest.raises(client_config.ClientConfigError, match=_VALUE_REJECTED):
            client_config.update_theme(value)
        assert home.path.read_bytes() == original
    assert home.path.stat().st_ino == before[1]
    assert client_config.load_client_settings().theme == "codex"


@pytest.mark.parametrize("failure", (
    "symlink", "hardlink", "directory_symlink", "permissions", "owner", "write",
))
def test_theme_owner_only_failures_preserve_the_file(home, tmp_path, monkeypatch, failure):
    """讀寫兩端都走 owner-only 防線;任何失敗都回 ClientConfigError,檔案逐位元組不變。"""
    client_config.update_theme("codex")
    original = home.path.read_bytes()
    preserved = home.path
    if failure in ("symlink", "directory_symlink"):
        outside = tmp_path / "elsewhere"
        outside.mkdir(mode=0o700)
        preserved = outside / "client.json"
        home.path.rename(preserved)
        if failure == "symlink":
            home.path.symlink_to(preserved)
        else:
            home.path.parent.rmdir()
            home.path.parent.symlink_to(outside, target_is_directory=True)
    elif failure == "hardlink":
        os.link(home.path, tmp_path / "other-name.json")
    elif failure == "permissions":
        home.path.chmod(0o644)
    elif failure == "owner":
        owner = os.getuid()
        monkeypatch.setattr(client_config.client_paths.os, "getuid", lambda: owner + 1)
    elif failure == "write":
        def cannot_write(*_args, **_kwargs):
            raise OSError("disk write failed")
        monkeypatch.setattr(client_config.client_paths, "replace_private_file", cannot_write)
    before = preserved.stat()
    with pytest.raises(client_config.ClientConfigError):
        client_config.update_theme("default")
    assert preserved.read_bytes() == original
    assert preserved.stat().st_ino == before.st_ino
    assert preserved.stat().st_mtime_ns == before.st_mtime_ns


def test_theme_startup_passes_saved_setting_explicitly(home, tmp_path, monkeypatch):
    """重開 TUI 一定明確收到保存的主題(沒有設定檔時是 default),不能靠 Textual 自己的預設。"""
    monkeypatch.setattr(codetrail_chat, "_has_tty", lambda: True)
    monkeypatch.setattr(codetrail_chat, "_resolve_root", lambda _raw: tmp_path)
    monkeypatch.setattr(codetrail_chat, "_initial_session", lambda *_args: "")
    monkeypatch.setattr(client_config, "apply_to_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(codetrail_chat.client_preflight, "run", lambda *_args: SimpleNamespace(
        banner_lines=lambda **_kwargs: (),
    ))
    engine = SimpleNamespace(
        tool_specs={}, options=SimpleNamespace(policy=SimpleNamespace(name="interactive")),
    )
    closed = []
    monkeypatch.setattr(codetrail_chat, "_build", lambda *_args, **_kwargs: (
        SimpleNamespace(close=lambda: closed.append(True)), engine,
    ))
    monkeypatch.setattr(codetrail_chat, "_compactor", lambda *_args: SimpleNamespace(mode="manual"))
    monkeypatch.setattr(codetrail_chat.client_store, "sessions_dir", lambda _root: tmp_path / "state")
    seen = []

    class CapturedApp:
        def __init__(self, _engine, **kwargs):
            seen.append(kwargs)

        def run(self):
            return 0

    monkeypatch.setattr(client_app, "CodeTrailApp", CapturedApp)
    args = codetrail_chat.build_parser().parse_args([])
    assert codetrail_chat.command_chat(args) == 0
    assert seen[-1]["theme"] == "default"
    assert not (home.home / ".config").exists()

    client_config.update_theme("codex")
    assert codetrail_chat.command_chat(codetrail_chat.build_parser().parse_args([])) == 0
    assert seen[-1]["theme"] == "codex"
    assert closed == [True, True]
    assert not (tmp_path / "state").exists()
