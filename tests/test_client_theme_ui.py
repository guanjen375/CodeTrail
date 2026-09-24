"""/theme 與主題註冊表的介面契約。

守六件事:

* **設定與外觀同一份名單**,每個主題都能在執行中互切(含 modal 開著)—— ANSI 主題少了
  Textual 需要的變數,切過去整個 App 會崩潰;
* **啟動一定套上主題**:Textual 的初值與設定同名時(殼層的 TEXTUAL_THEME),只賦值不會
  觸發 watcher,-theme- class 與 ANSI filter 都不會套上;
* **保存成功才算數**:/theme 與選單的 Enter 先寫 owner-only client.json,失敗畫面不變;
  選單預覽不寫檔,Esc／Ctrl-C／Ctrl-D 還原而且不算中斷或離開;回合／核准中拒絕;
* **主題只改呈現**:codex 的「› 」「• 」「└ 」不進選取與剪貼簿,default 的選取範圍
  (含三擊 = 整個對話區)與加入主題前相同;狀態資訊兩主題一致;
* **沒有第二個入口**:Textual 指令面板停用,換不到註冊表以外的主題;
* **requirements 允許的 Textual 8 都能用**:8.2.5 前沒有 Theme(ansi=…)、不加 -theme- class,
  以前 import 就 TypeError,整個 aicode 起不來。
"""
from __future__ import annotations

import asyncio
import os

import pytest
from textual import events
from textual.app import App
from textual.command import CommandPalette
from textual.filter import ANSIToTruecolor
from textual.theme import BUILTIN_THEMES
from textual.widgets import OptionList

import client_app
import client_config
import client_events
import client_theme
from tests.test_client_app import _AsksApproval, _Blocks, _Engine, _wait_for_approval
from tests.test_client_auto_copy import _drag, _mouse, _point


pytestmark = pytest.mark.smoke


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    return home


def _run(body):
    return asyncio.run(body())


async def _settle(pilot, times=10):
    for _ in range(times):
        await pilot.pause()


def _lines(app, kind):
    return [widget.message for widget in app.query_one("#log").children if isinstance(widget, kind)]


def _truecolor_filter_enabled(app) -> bool:
    return any(isinstance(item, ANSIToTruecolor) for item in app.get_line_filters())


def _assert_theme_applied(app, name):
    spec = client_theme.THEMES[name]
    assert app.theme == name
    assert [item for item in app.classes if item.startswith("-theme-")] == [f"-theme-{name}"]
    assert app.native_ansi_color is spec.textual.ansi
    # ANSI 主題用終端自己的顏色:ANSI→RGB 的轉換必須關掉,否則 codex 其實是固定 RGB。
    assert _truecolor_filter_enabled(app) is (not spec.textual.ansi)


def test_theme_registry_matches_config_and_every_theme_switches_at_runtime():
    assert tuple(client_theme.THEMES) == client_config.THEME_VALUES
    assert client_config.DEFAULT_THEME in client_theme.THEMES
    default = client_theme.THEMES["default"].textual
    builtin = BUILTIN_THEMES["textual-dark"]
    # default 主題的每一個 CSS 變數都與加入主題前的 textual-dark 相同。
    assert default.to_color_system().generate() == builtin.to_color_system().generate()
    assert default.variables == builtin.variables and default.ansi is builtin.ansi

    async def body():
        for start in client_config.THEME_VALUES:
            app = client_app.CodeTrailApp(_Engine(), theme=start)
            async with app.run_test(size=(100, 30)) as pilot:
                await _settle(pilot)
                _assert_theme_applied(app, start)
                modal = client_app.QueueChoiceScreen("draft")
                app.push_screen(modal)
                await _settle(pilot)
                for name in (*client_config.THEME_VALUES, start):
                    app.theme = name
                    await _settle(pilot)
                    _assert_theme_applied(app, name)
                    assert app.screen is modal
                    shown = bool(client_theme.THEMES[name].glyphs["composer"][0])
                    composer = app.screen_stack[0].query_one("#composer").query_one(client_theme.ThemeGlyph)
                    assert composer.display is shown

    # 任何一次切換讓 Textual 丟例外,run_test 結束時都會把它拋出來。
    _run(body)


@pytest.mark.parametrize("name", client_config.THEME_VALUES)
def test_theme_startup_applies_even_when_textual_default_has_the_same_name(monkeypatch, name):
    # 等同 Textual 讀到同名的 TEXTUAL_THEME:App.theme 的初值已經是設定值。
    monkeypatch.setattr(App.theme, "_default", name)

    async def body():
        app = client_app.CodeTrailApp(_Engine(), theme=name)
        async with app.run_test(size=(80, 20)) as pilot:
            await _settle(pilot)
            _assert_theme_applied(app, name)
            spec = client_theme.THEMES[name]
            glyph = app.query_one("#composer").query_one(client_theme.ThemeGlyph)
            assert glyph.display is bool(spec.glyphs["composer"][0])
            assert app.query_one("#prompt", client_app.PromptInput).placeholder == spec.placeholder

    _run(body)


def test_theme_command_saves_before_applying_and_failures_keep_the_screen(monkeypatch, private_home):
    path = client_config.config_path()
    order = []
    real_update = client_config.update_theme

    async def body():
        app = client_app.CodeTrailApp(_Engine())

        def watched_update(name, env=None):
            result = real_update(name, env)
            # 寫檔當下:畫面上的主題、檔案裡的主題。
            order.append((name, app.theme, client_config.load_client_settings().theme))
            return result

        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)
            # 預設值、沒有設定檔:零寫入,連目錄都不建。
            app._command("/theme default")
            await _settle(pilot)
            assert app.theme == "default" and not (private_home / ".config").exists()
            assert any("未寫入檔案" in note for note in _lines(app, client_app.NoticeLine))
            # 註冊表以外的名字:拒絕、零寫入、畫面不變。
            app._command("/theme nord")
            await _settle(pilot)
            assert app.theme == "default" and not (private_home / ".config").exists()
            assert any("nord" in error for error in _lines(app, client_app.ErrorLine))
            # 先存後套:寫檔的當下畫面仍是舊主題,檔案已經是新值。
            monkeypatch.setattr(client_config, "update_theme", watched_update)
            app._command("/theme Codex")
            await _settle(pilot)
            assert order == [("codex", "default", "codex")]
            _assert_theme_applied(app, "codex")
            assert client_config.load_client_settings().theme == "codex"
            assert oct(path.stat().st_mode & 0o777) == "0o600"
            # 相同值:零寫入。
            before = path.stat()
            app._command("/theme codex")
            await _settle(pilot)
            after = path.stat()
            assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
            # 保存失敗:畫面與檔案都不動。
            saved = path.read_bytes()

            def failing(_name, env=None):
                raise client_config.ClientConfigError("injected-save-failure")

            monkeypatch.setattr(client_config, "update_theme", failing)
            app._command("/theme default")
            await _settle(pilot)
            _assert_theme_applied(app, "codex")
            assert path.read_bytes() == saved
            assert any("injected-save-failure" in error for error in _lines(app, client_app.ErrorLine))

    _run(body)


def test_theme_picker_previews_restores_and_only_enter_persists(monkeypatch, private_home):
    config_dir = private_home / ".config"

    async def open_picker(app, pilot):
        app._command("/theme")
        await _settle(pilot)
        assert isinstance(app.screen, client_app.ThemePickerScreen)
        return app.screen

    async def body():
        engine = _Engine()
        app = client_app.CodeTrailApp(engine)
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)
            picker = await open_picker(app, pilot)
            assert picker.query_one("#theme-picker-list", OptionList).highlighted == 0
            # 移動反白 = 即時預覽,不寫檔。
            await pilot.press("down")
            await _settle(pilot)
            _assert_theme_applied(app, "codex")
            assert not config_dir.exists()
            # Esc:還原、零寫入。
            await pilot.press("escape")
            await _settle(pilot)
            assert not isinstance(app.screen, client_app.ThemePickerScreen)
            _assert_theme_applied(app, "default")
            assert not config_dir.exists()
            # Ctrl-C / Ctrl-D:只收選單並還原;不是中斷、不武裝連按兩次離開、也不離開。
            for key in ("ctrl+c", "ctrl+d"):
                await open_picker(app, pilot)
                await pilot.press("down")
                await _settle(pilot)
                assert app.theme == "codex"
                await pilot.press(key)
                await _settle(pilot)
                assert not isinstance(app.screen, client_app.ThemePickerScreen)
                _assert_theme_applied(app, "default")
            notices = _lines(app, client_app.NoticeLine)
            assert not any("已中斷" in note or "再按一次" in note for note in notices)
            assert app.return_value is None and not engine.cancelled
            assert not config_dir.exists()
            # Enter:保存後才算數。
            await open_picker(app, pilot)
            await pilot.press("down")
            await _settle(pilot)
            await pilot.press("enter")
            await _settle(pilot)
            assert not isinstance(app.screen, client_app.ThemePickerScreen)
            _assert_theme_applied(app, "codex")
            assert client_config.load_client_settings().theme == "codex"
            assert any("已儲存至" in note for note in _lines(app, client_app.NoticeLine))
            # Enter 但保存失敗:回到開選單前的主題,檔案不動。
            saved = client_config.config_path().read_bytes()

            def failing(_name, env=None):
                raise client_config.ClientConfigError("injected-picker-failure")

            monkeypatch.setattr(client_config, "update_theme", failing)
            await open_picker(app, pilot)
            await pilot.press("up")
            await _settle(pilot)
            assert app.theme == "default"
            await pilot.press("enter")
            await _settle(pilot)
            _assert_theme_applied(app, "codex")
            assert client_config.config_path().read_bytes() == saved
            assert any("injected-picker-failure" in error for error in _lines(app, client_app.ErrorLine))

    _run(body)


def test_theme_changes_are_refused_while_work_is_running(private_home):
    async def body():
        engine = _Blocks()
        app = client_app.CodeTrailApp(engine)
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)
            app.submit("hi")
            assert engine.entered.wait(5)
            for line in ("/theme", "/theme codex"):
                app._command(line)
                await _settle(pilot, 5)
                assert not isinstance(app.screen, client_app.ThemePickerScreen)
                _assert_theme_applied(app, "default")
            refusals = [note for note in _lines(app, client_app.NoticeLine) if "不能切換主題" in note]
            assert len(refusals) == 2
            engine.release.set()
            await _settle(pilot, 60)

        engine = _AsksApproval()
        app = client_app.CodeTrailApp(engine)
        async with app.run_test(size=(100, 30)) as pilot:
            app.submit("改一下")
            screen = await _wait_for_approval(app, pilot)
            for line in ("/theme", "/theme codex"):
                app._command(line)
                await _settle(pilot, 5)
                assert app.screen is screen
                _assert_theme_applied(app, "default")
            await pilot.press("n")
            await _settle(pilot, 60)
            assert engine.granted == [False]
        assert not (private_home / ".config").exists()

    _run(body)


#: 以今天(加入主題前)的行為為準:雙擊選本文,三擊選整個對話區(NoticeLine 的「• 」是內容)。
_USER_TEXT = "問題 中文\n  縮排"
_ALL_TEXT = "• before\n問題 中文\n  縮排\n回答 粗體"


@pytest.mark.parametrize("name", client_config.THEME_VALUES)
def test_theme_dom_keeps_default_selection_scope_and_excludes_decorations(monkeypatch, name):
    async def body():
        app = client_app.CodeTrailApp(_Engine(), theme=name)
        app.CLICK_CHAIN_TIME_THRESHOLD = 60
        copies = []
        original_copy = app.copy_to_clipboard

        def record(text):
            copies.append(text)
            original_copy(text)

        async with app.run_test(size=(100, 30)) as pilot:
            monkeypatch.setattr(app, "copy_to_clipboard", record)
            log = app.query_one("#log")
            answer = client_app.AssistantBlock()
            answer.finish("回答 **粗體**")
            user = client_app.UserMessage(_USER_TEXT)
            await log.mount(client_app.NoticeLine("before"), user, answer)
            await _settle(pilot)
            body_widget = user.query_one(".user-body")
            glyphs = user.query(client_theme.ThemeGlyph)
            assert [glyph.display for glyph in glyphs] == [name == "codex"]
            point = _point(body_widget, 1)
            # 雙擊複製本文,三擊複製整個對話區(select_container 跳過 UserMessage 包裝)。
            for _chain in range(3):
                await _mouse(app, pilot, events.MouseDown, point)
                await _mouse(app, pilot, events.MouseUp, point)
            assert copies == [_USER_TEXT, _ALL_TEXT]
            assert app.screen.get_selected_text() == _ALL_TEXT
            # 手動複製鍵與閒置 Ctrl-C 讀同一份選取,裝飾符號不得混進來。
            await pilot.press(app.copy_key)
            await _settle(pilot)
            await pilot.press("ctrl+c")
            await _settle(pilot)
            assert copies[2:] == [_ALL_TEXT, _ALL_TEXT]
            assert not any("再按一次" in note for note in _lines(app, client_app.NoticeLine))
            # 拖選本文兩行:使用者自己的縮排保留,「› 」不在裡面。
            await _drag(app, pilot, _point(body_widget), _point(body_widget, 6, 1))
            assert copies[-1] == _USER_TEXT
            # 展開的工具輸出:「└ 」不在裡面。
            tool = client_app.ToolBlock("read_file", {"path": "README.md"}, client_events.STATUS_COMPLETED)
            tool.set_output("工具輸出 line")
            await log.mount(tool)
            tool.collapsed = False
            await _settle(pilot)
            output = tool.query_one(".tool-output")
            await _drag(app, pilot, _point(output), _point(output, 13))
            assert copies[-1] == "工具輸出 line"
            for text in copies:
                assert "› " not in text and "└ " not in text and "\n\n" not in text

    _run(body)


def test_status_information_is_the_same_in_both_themes():
    async def run(name):
        engine = _Blocks()
        app = client_app.CodeTrailApp(engine, theme=name)
        seen = {}
        async with app.run_test(size=(200, 30)) as pilot:

            async def sample():
                app._refresh_status()
                await _settle(pilot, 3)  # class 改了之後 CSS 要到下一輪才重算 display
                return app.status_text, app.activity_text, app.query_one("#activity").display

            await _settle(pilot)
            seen["idle"] = await sample()
            app.submit("hi")
            assert engine.entered.wait(5)
            await _settle(pilot, 3)
            seen["busy"] = await sample()
            engine.release.set()
            await _settle(pilot, 60)
            seen["after"] = await sample()
        return seen

    default = _run(lambda: run("default"))
    codex = _run(lambda: run("codex"))
    idle = default["idle"][0]
    # 閒置:兩主題完全同一行資訊;有 think=on|off,沒有權限／專案指示／舊 reasoning。
    assert codex["idle"] == default["idle"] == (idle, "", False)
    assert "think=off" in idle.split(" · ")
    for forbidden in ("permission", "interactive", "專案指示", "reasoning"):
        assert forbidden not in idle
    # 回合中:default 照舊把秒數與階段插在最前面;codex 移到活動列,footer 不變。
    busy_status, busy_activity, busy_shown = default["busy"]
    head, phase, *rest = busy_status.split(" · ")
    assert head[0] in client_app.SPINNER_FRAMES and head.endswith("s(Ctrl-C 中斷)")
    assert " · ".join(rest) == idle and busy_activity == "" and busy_shown is False
    codex_status, codex_activity, codex_shown = codex["busy"]
    assert codex_status == idle and codex_shown is True
    assert codex_activity[0] in "•◦" and phase in codex_activity
    assert "Ctrl-C" in codex_activity and "中斷" in codex_activity
    # 回合結束:活動列收起。
    assert codex["after"] == default["after"] == (idle, "", False)


def test_the_command_palette_cannot_bypass_the_theme_registry():
    assert client_app.CodeTrailApp.ENABLE_COMMAND_PALETTE is False

    async def body():
        app = client_app.CodeTrailApp(_Engine())
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)
            stack = list(app.screen_stack)
            await pilot.press("ctrl+p")
            await _settle(pilot)
            assert list(app.screen_stack) == stack
            assert not isinstance(app.screen, CommandPalette)
            assert app.theme in client_config.THEME_VALUES
            assert "ctrl+p" not in app.active_bindings

    _run(body)


def test_theme_switch_leaves_engine_session_and_live_blocks_intact(private_home):
    engine = _Engine()

    async def body():
        app = client_app.CodeTrailApp(engine)
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)
            app.handle_event(client_events.tool_event(
                engine.session_id, tool="read_file", call_id="call_1",
                status=client_events.STATUS_COMPLETED, arguments={"path": "README.md"},
            ))
            await _settle(pilot)
            block = app._tools["call_1"]
            classic = block.title
            assert classic == "✓ read_file(path=README.md) → completed"
            before = (
                engine.session_id, list(engine.messages), list(engine.sent), list(engine.primes),
                engine.store.created, app.coordinator.busy,
            )
            prompt = app.query_one("#prompt", client_app.PromptInput)
            app._command("/theme codex")
            await _settle(pilot)
            assert app._tools == {"call_1": block}
            assert str(block.title) == "Called read_file(path=README.md)"
            title = block.query_one("CollapsibleTitle")
            assert (title.collapsed_symbol, title.expanded_symbol) == ("•", "•")
            assert prompt.placeholder == client_theme.THEMES["codex"].placeholder
            app._command("/theme default")
            await _settle(pilot)
            assert block.title == classic
            assert (title.collapsed_symbol, title.expanded_symbol) == ("▶", "▼")
            assert prompt.placeholder == ""
            after = (
                engine.session_id, list(engine.messages), list(engine.sent), list(engine.primes),
                engine.store.created, app.coordinator.busy,
            )
            assert after == before

    _run(body)
    # 主題寫在設定檔,不寫 session。
    assert client_config.load_client_settings().theme == "default"
    assert os.path.exists(client_config.config_path())


def test_themes_work_on_textual_releases_without_native_ansi_themes(monkeypatch):
    """requirements 允許 textual>=8,<9;8.0–8.2.4 的 Theme 沒有 ansi 參數,_watch_theme 也
    不加 -theme-<name> class,而且每次都把 ansi_color 設回 False。這些版本的 aicode 必須照樣
    啟動(以前 import client_theme 就 TypeError),codex 仍用終端原生色、主題 CSS 仍生效。"""
    import importlib

    import textual.theme

    real_theme = textual.theme.Theme

    class LegacyTheme(real_theme):
        """8.0–8.2.4 的建構子:不認得 ansi。"""

        def __init__(self, *args, **kwargs):
            if "ansi" in kwargs:
                raise TypeError("Theme.__init__() got an unexpected keyword argument 'ansi'")
            super().__init__(*args, **kwargs)

    real_watch = App._watch_theme

    def legacy_watch_theme(self, theme_name):
        real_watch(self, theme_name)
        # 8.0–8.2.4:不加 -theme-<name>;ANSI 直通只給名為 textual-ansi 的主題。
        for name in [item for item in self.classes if item.startswith("-theme-")]:
            self.remove_class(name)
        self.ansi_color = theme_name == "textual-ansi"

    monkeypatch.setattr(textual.theme, "Theme", LegacyTheme)
    try:
        importlib.reload(client_theme)
        assert client_theme.NATIVE_ANSI_THEMES is False
        monkeypatch.setattr(App, "_watch_theme", legacy_watch_theme)

        async def body():
            for start in client_config.THEME_VALUES:
                app = client_app.CodeTrailApp(_Engine(), theme=start)
                async with app.run_test(size=(80, 20)) as pilot:
                    await _settle(pilot)
                    others = [name for name in client_config.THEME_VALUES if name != start]
                    for name in (start, *others, start):
                        app.theme = name
                        await _settle(pilot)
                        wants = client_theme.THEMES[name].ansi
                        assert [item for item in app.classes if item.startswith("-theme-")] == [f"-theme-{name}"]
                        assert app.ansi_color is wants
                        assert _truecolor_filter_enabled(app) is (not wants)

        _run(body)
    finally:
        monkeypatch.setattr(textual.theme, "Theme", real_theme)
        importlib.reload(client_theme)
