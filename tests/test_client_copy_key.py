"""複製鍵的安全契約：不占用既有操作、不遺失授權、寫入成功後才切換。"""
from __future__ import annotations

import ast
import asyncio
import base64
import inspect
import json
import os
import shlex
import textwrap
from dataclasses import replace
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.command import CommandPalette
from textual.screen import ModalScreen, Screen
from textual.selection import SELECT_ALL
from textual.widgets import Button, Collapsible, Input, Markdown, OptionList, Static, TextArea
from textual.widgets.text_area import Selection

import client_app
import client_config
import codetrail_chat
from tests.test_client_app import _AsksApproval, _Blocks, _Engine, _wait_for_approval


pytestmark = pytest.mark.smoke


@pytest.fixture
def ui(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    engine = _Engine()
    app = client_app.CodeTrailApp(engine)
    widgets = []
    monkeypatch.setattr(app, "_append", widgets.append)

    def dispatch(line):
        widgets.clear()
        assert app._command(line)
        assert len(widgets) == 1
        return widgets[0]

    return SimpleNamespace(
        app=app, engine=engine, home=home, path=client_config.config_path(), dispatch=dispatch,
    )


def test_copy_key_choices_do_not_collide_with_any_binding_chain():
    """繼承自 Textual 的 priority/palette/輸入框/modal 按鍵都不能被覆寫。"""
    choices = set(client_config.COPY_KEY_VALUES)
    assert client_config.DEFAULT_COPY_KEY == "f2" and "f2" in choices
    assert choices <= {f"f{number}" for number in range(1, 13)} - {"f6", "f7"}
    classes = (
        App, Screen, ModalScreen, CommandPalette, Input, TextArea, Button, OptionList,
        Collapsible, Static, Markdown,
        client_app.VerticalScroll, client_app.CodeTrailApp, client_app.PromptInput,
        client_app.ApprovalScreen, client_app.SessionPickerScreen,
        client_app.QueueChoiceScreen, client_app.ReviewScreen,
    )
    for cls in classes:
        assert not choices.intersection(cls._merged_bindings.key_to_bindings), cls.__name__
    app = client_app.CodeTrailApp(_Engine())
    assert not choices.intersection(app._bindings.key_to_bindings)
    assert not choices.intersection(app.COMMAND_PALETTE_BINDING.split(","))
    # PromptInput 另有事件攔截，不能只檢查 BINDINGS 而漏掉這條路。
    code = ast.parse(textwrap.dedent(inspect.getsource(client_app.PromptInput._on_key)))
    intercepted = {node.value for node in ast.walk(code)
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert not choices.intersection(intercepted)


def test_copy_key_defaults_and_read_only_commands_do_not_create_config(ui):
    """沒有設定檔維持預設 F2；查詢／help／預設 reset 都不建立檔案或目錄。"""
    settings = client_config.load_client_settings()
    assert settings.copy_key == "f2" and not settings.present
    for line in ("/copykey", "/help", "/copykey reset"):
        notice = ui.dispatch(line)
        assert isinstance(notice, client_app.NoticeLine)
        assert "F2" in notice.message
    assert not (ui.home / ".config").exists()
    assert ui.engine.messages == [] and ui.engine.sent == [] and ui.engine.primes == []


def test_copy_key_startup_passes_saved_setting_explicitly(ui, tmp_path, monkeypatch):
    """重開 TUI 必須收到使用者儲存的鍵，不能只在同一個 App 記憶體中生效。"""
    client_config.update_copy_key("f10")
    monkeypatch.setattr(codetrail_chat, "_has_tty", lambda: True)
    monkeypatch.setattr(codetrail_chat, "_resolve_root", lambda _raw: tmp_path)
    monkeypatch.setattr(codetrail_chat, "_initial_session", lambda *_args: "")
    monkeypatch.setattr(client_config, "apply_to_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(codetrail_chat.client_preflight, "run", lambda *_args: SimpleNamespace(
        banner_lines=lambda **_kwargs: (),
    ))
    closed = []
    monkeypatch.setattr(codetrail_chat, "_build", lambda *_args, **_kwargs: (
        SimpleNamespace(close=lambda: closed.append(True)), ui.engine,
    ))
    monkeypatch.setattr(codetrail_chat, "_compactor", lambda *_args: SimpleNamespace(mode="manual"))
    monkeypatch.setattr(codetrail_chat.client_store, "sessions_dir", lambda _root: tmp_path / "state")
    seen = {}

    class CapturedApp:
        def __init__(self, _engine, **kwargs):
            seen.update(kwargs)

        def run(self):
            return 0

    monkeypatch.setattr(client_app, "CodeTrailApp", CapturedApp)
    args = codetrail_chat.build_parser().parse_args([])
    assert codetrail_chat.command_chat(args) == 0
    assert seen["copy_key"] == "f10" and closed == [True]
    assert not (tmp_path / "state").exists()


def test_copy_key_edits_preserve_fresh_allow_settings_and_private_modes(ui, tmp_path, monkeypatch):
    """同一個 App 先 /allow add 再改鍵，不能用啟動快照覆蓋剛新增的授權。"""
    settings = client_config.ClientSettings(
        path=ui.path, permission={"run_command": "ask"}, compaction_mode="off",
        project_instructions=False, extra_allowed_commands=["nsim"],
        keep_historical_reasoning=True, show_reasoning=True,
    )
    client_config.save_client_settings(settings)
    directory = tmp_path / "tool chain" / "bin"
    directory.mkdir(parents=True, mode=0o700)
    executable = directory / "arc-probe"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    notice = ui.dispatch(f"/allow add {shlex.quote(str(directory))}")
    assert isinstance(notice, client_app.NoticeLine) and "已加入" in notice.message
    latest = client_config.load_client_settings()

    def forbidden(*_args, **_kwargs):
        pytest.fail("copy_key 不得重新套用 runtime 設定、寫 session 或要求額外核准")

    monkeypatch.setattr(client_config, "apply_to_config", forbidden)
    monkeypatch.setattr(ui.engine.store, "append", forbidden)
    monkeypatch.setattr(ui.app, "push_screen", forbidden)
    notice = ui.dispatch("/copykey F3")
    assert isinstance(notice, client_app.NoticeLine) and "已儲存" in notice.message
    expected = {**latest.as_json(), "copy_key": "f3"}
    assert client_config.load_client_settings().as_json() == expected
    assert client_config.load_client_settings_from(ui.path).as_json() == expected
    assert ui.app.copy_key == "f3"
    assert ui.path.stat().st_mode & 0o777 == 0o600
    assert ui.path.parent.stat().st_mode & 0o777 == 0o700
    # set_config 的模式更新仍須保留新鍵；授權編輯也不能把新鍵丟掉。
    saved = client_config.load_client_settings()
    client_config.save_client_settings(saved.with_compaction("manual"))
    client_config.update_extra_allowed_commands("add", ["mdb"])
    assert client_config.load_client_settings().copy_key == "f3"
    assert ui.engine.messages == [] and ui.engine.sent == []


def test_copy_key_invalid_settings_and_commands_fail_without_writing(ui):
    """拒絕退出／中斷／送出／導覽／redo 等既有鍵與壞 schema，不以預設掩蓋。"""
    settings, _ = client_config.update_copy_key("f3")
    ui.app.copy_key = settings.copy_key
    before = ui.path.read_bytes()
    stat = ui.path.stat()
    invalid = (
        "ctrl+c", "ctrl+d", "ctrl+q", "ctrl+p", "ctrl+y", "ctrl+z", "ctrl+j",
        "escape", "enter", "alt+enter", "tab", "up", "down", "f6", "f7", "f13",
        "f3,f4", "f3 f4", "F3", "", None, True, [], {},
    )
    for value in invalid:
        with pytest.raises(client_config.ClientConfigError, match="copy_key"):
            client_config.save_client_settings(replace(settings, copy_key=value))
        payload = {**settings.as_json(), "copy_key": value}
        ui.path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(client_config.ClientConfigError, match="copy_key"):
            client_config.load_client_settings()
        ui.path.write_bytes(before)
        if isinstance(value, str) and value and value != "F3":
            error = ui.dispatch(f"/copykey {value}")
            assert isinstance(error, client_app.ErrorLine)
            assert ui.app.copy_key == "f3"
            assert ui.path.read_bytes() == before
    assert ui.path.stat().st_ino == stat.st_ino
    assert ui.engine.messages == [] and ui.engine.sent == []


@pytest.mark.parametrize("failure", (
    "symlink", "hardlink", "directory_symlink", "permissions", "owner", "write",
))
def test_copy_key_owner_only_failures_preserve_file_and_active_key(ui, tmp_path, monkeypatch, failure):
    """讀寫兩端繼續走 owner-only 防線；任何失敗不能切換 UI 或顯示成功。"""
    settings, _ = client_config.update_copy_key("f3")
    ui.app.copy_key = settings.copy_key
    original = ui.path.read_bytes()
    preserved = ui.path
    if failure in ("symlink", "directory_symlink"):
        outside = tmp_path / "elsewhere"
        outside.mkdir(mode=0o700)
        preserved = outside / "client.json"
        ui.path.rename(preserved)
        if failure == "symlink":
            ui.path.symlink_to(preserved)
        else:
            ui.path.parent.rmdir()
            ui.path.parent.symlink_to(outside, target_is_directory=True)
    elif failure == "hardlink":
        os.link(ui.path, tmp_path / "other-name.json")
    elif failure == "permissions":
        ui.path.chmod(0o644)
    elif failure == "owner":
        owner = os.getuid()
        monkeypatch.setattr(client_config.client_paths.os, "getuid", lambda: owner + 1)
    elif failure == "write":
        def cannot_write(*_args, **_kwargs):
            raise OSError("disk write failed")
        monkeypatch.setattr(client_config.client_paths, "replace_private_file", cannot_write)
    before = preserved.stat()
    error = ui.dispatch("/copykey f4")
    assert isinstance(error, client_app.ErrorLine) and "/copykey" in error.message
    assert "目前仍使用 F3" in error.message and "已儲存" not in error.message
    assert ui.app.copy_key == "f3"
    assert preserved.read_bytes() == original
    assert preserved.stat().st_ino == before.st_ino
    assert preserved.stat().st_mtime_ns == before.st_mtime_ns


def test_copy_key_switch_removes_old_dispatch_and_failed_save_keeps_previous_key(tmp_path, monkeypatch):
    """手動備用鍵與 OSC52 仍即時切換；改鍵不能重新掛出已移除的複製提示列。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = _Engine()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test(size=(100, 25)) as pilot:
            prompt = app.query_one("#prompt", client_app.PromptInput)
            assert not app.query("#copy-hint")
            bindings = {key: list(values) for key, values in app._bindings.key_to_bindings.items()}
            writes = []
            monkeypatch.setattr(app._driver, "write", writes.append)
            for old, requested, new in (("f2", "F3", "f3"), ("f3", "f4", "f4"), ("f4", "reset", "f2")):
                app._command(f"/copykey {requested}")
                assert app.copy_key == new and not app.query("#copy-hint")
                assert client_config.load_client_settings().copy_key == new
                prompt.text = "draft 中文"
                prompt.selection = Selection((0, 0), (0, 8))
                app.copy_to_clipboard("unchanged")
                writes.clear()
                await pilot.press(old)
                assert app.clipboard == "unchanged"
                assert not any("\x1b]52;" in written for written in writes)
                await pilot.press(new)
                assert app.clipboard == "draft 中文"
                encoded = base64.b64encode("draft 中文".encode()).decode()
                assert f"\x1b]52;c;{encoded}\a" in writes
                prompt.move_cursor(prompt.document.end)
                await pilot.press(new)
                assert app.clipboard == "draft 中文", "空選取不能清空剪貼簿"
                assert app._bindings.key_to_bindings == bindings, "改鍵累加了綁定"
            path = client_config.config_path()
            before = path.read_bytes()
            with monkeypatch.context() as guard:
                def cannot_write(*_args, **_kwargs):
                    raise OSError("disk write failed")
                guard.setattr(client_config, "save_client_settings", cannot_write)
                app._command("/copykey f5")
                assert app.copy_key == "f2" and not app.query("#copy-hint")
                prompt.selection = Selection((0, 0), (0, 8))
                app.copy_to_clipboard("unchanged")
                await pilot.press("f5")
                assert app.clipboard == "unchanged"
                await pilot.press("f2")
                assert app.clipboard == "draft 中文"
            assert path.read_bytes() == before
            assert engine.messages == [] and engine.sent == [] and not engine.cancelled

    asyncio.run(body())


@pytest.mark.parametrize("state", ("busy", "approval"))
def test_copy_key_changed_key_works_during_turn_and_modal_without_stealing_ctrl_c(tmp_path, monkeypatch, state):
    """改鍵後仍走 App priority 路徑；核准與中斷不得被選取狀態吞掉。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = _Blocks() if state == "busy" else _AsksApproval()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test() as pilot:
            app._command("/copykey f3")
            assert app.submit("read this selected text")
            try:
                if state == "approval":
                    screen = await _wait_for_approval(app, pilot)
                    detail = screen.query_one("#approval-detail")
                    screen.selections = {detail: SELECT_ALL}
                    selected = screen.ticket.request.render().rstrip("\n")
                else:
                    assert engine.entered.wait(5)
                    screen = app.screen
                    detail = Static(client_app.Text("in progress 中文"))
                    await app.query_one("#log").mount(detail)
                    screen.selections = {detail: SELECT_ALL}
                    selected = "in progress 中文"
                await pilot.press("f2")
                assert app.clipboard == ""
                await pilot.press("f3")
                assert app.clipboard == selected and app.screen is screen
                assert app.coordinator.busy and not engine.cancelled
                if state == "approval":
                    assert engine.granted == []
                await pilot.press("ctrl+c")
                if state == "busy":
                    assert engine.cancelled
                else:
                    for _ in range(30):
                        if not app.coordinator.busy:
                            break
                        await pilot.pause()
                    assert engine.granted == [False] and not engine.finished.is_set()
            finally:
                if state == "busy":
                    engine.release.set()
                for _ in range(30):
                    if not app.coordinator.busy:
                        break
                    await pilot.pause()
            assert not app.coordinator.busy

    asyncio.run(body())
